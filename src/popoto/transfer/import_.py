"""Import records from a JSON Lines export produced by :mod:`popoto.transfer`.

Keys are always preserved. Records are saved one at a time -- deliberately not
through ``bulk_create`` -- because an external pipeline makes ``save()`` return
the pipeline for every record and destroys per-record observability, which the
reconciliation ledger depends on.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, TextIO, cast

from ..exceptions import ModelException
from ..redis_db import POPOTO_REDIS_DB
from .export import collect_embedding_provenance
from .format import (
    FORMAT_VERSION,
    MANIFEST_KEY,
    from_jsonable,
    iter_lines,
    parse_line,
)
from .results import (
    ERRORED,
    ImportReport,
    LANDED,
    PARTIAL,
    RecordOutcome,
    REJECTED,
    SKIPPED,
)

if TYPE_CHECKING:  # pragma: no cover - types only, never imported at runtime
    from ..models.base import Model

logger = logging.getLogger("popoto")


class _ImportsState(Protocol):
    """The duck-typed model-level contract :mod:`popoto.transfer.export` describes.

    Naming it lets the ``klass.import_state(...)`` call in ``_restore_state``
    be *checked* rather than suppressed. The cast is applied inside the
    ``by_name`` comprehension, on the same expression as the
    ``"import_state" in klass.__dict__`` filter that makes it sound.
    """

    __name__: str

    @staticmethod
    def import_state(instance: Any, carried: Any) -> Any: ...

BATCH_SIZE = 500
"""Records per conflict-check / reconciliation batch."""

_ON_CONFLICT = ("error", "skip", "overwrite")
_ON_WRITE_GATE = ("reject", "bypass")
_ON_EMBEDDING_MISMATCH = ("error", "carry", "regenerate")


def _check_choice(name: str, value: str, allowed: "tuple[str, ...]") -> None:
    if value not in allowed:
        raise ValueError(
            f"{name}={value!r} is not one of {', '.join(repr(a) for a in allowed)}"
        )


def _validate_manifest(
    model_class: "type[Model]", manifest: "dict[str, Any] | None"
) -> "dict[str, Any]":
    """Validate the manifest before a single byte is written.

    Raises:
        ModelException: If the file has no manifest, was written by an
            incompatible format version, or was exported from a different
            model. All three are refused up front rather than part-way
            through, so a wrong file never leaves half an import behind.
    """
    if manifest is None or MANIFEST_KEY not in manifest:
        raise ModelException(
            "export file has no manifest line; the first line must be a "
            f"JSON object carrying {MANIFEST_KEY!r}"
        )

    version = manifest.get(MANIFEST_KEY)
    if version != FORMAT_VERSION:
        raise ModelException(
            f"export format version {version!r} is not supported by this "
            f"popoto (expected {FORMAT_VERSION}). Cross-version import is "
            f"not promised; re-export from the source instead."
        )

    source_model = manifest.get("model")
    if source_model != model_class.__name__:
        raise ModelException(
            f"export was taken from model {source_model!r} but is being "
            f"imported into {model_class.__name__!r}; one model per file"
        )
    return manifest


def _resolve_embedding_provenance(
    model_class: "type[Model]",
    manifest: "dict[str, Any]",
    on_embedding_mismatch: str,
    report: ImportReport,
) -> "set[str]":
    """Compare exported provider fingerprints against the destination's.

    Returns:
        The set of field names whose carried state must be dropped so that
        ``on_save`` regenerates it (only under ``"regenerate"``).

    Raises:
        ModelException: On a mismatch under the default ``"error"`` policy,
            naming the field and both provider fingerprints. Carrying a vector
            into a different vector space is silent corruption; this converts
            it into a loud, cheap check.
    """
    source = manifest.get("embedding_provenance") or {}
    if not source:
        return set()

    destination = collect_embedding_provenance(model_class)
    regenerate: "set[str]" = set()

    for field_name, source_print in source.items():
        destination_print = destination.get(field_name)
        if destination_print == source_print:
            continue
        detail = (
            f"embedding provenance mismatch on field {field_name!r}: "
            f"source={source_print} destination={destination_print}"
        )
        if on_embedding_mismatch == "error":
            raise ModelException(
                detail + "; pass on_embedding_mismatch='carry' to import the vectors "
                "anyway, or 'regenerate' to re-embed on the destination"
            )
        if on_embedding_mismatch == "regenerate":
            regenerate.add(field_name)
            report.warnings.append(detail + "; carried state dropped, re-embedding")
        else:
            report.warnings.append(detail + "; carrying source vectors anyway")

    return regenerate


def _restore_state(
    model_class: "type[Model]",
    instance: "Model",
    record: "dict[str, Any]",
    drop: "set[str]",
) -> None:
    """Restore carried state after the save.

    Runs after ``save()`` on purpose: companion hashes are keyed by redis_key
    and ``on_save`` seeds or clobbers them, so restoring first would be undone.
    Any raise here propagates to the caller, which classifies the record as
    ``partial`` -- the record exists, its auxiliary state does not.
    """
    for field_name, carried in (record.get("state") or {}).items():
        if field_name in drop:
            continue
        model_field = model_class._meta.fields.get(field_name)
        if model_field is None:
            raise ModelException(
                f"carried state names field {field_name!r}, which the "
                f"destination model does not define"
            )
        importer = getattr(model_field, "import_state", None)
        if importer is None:
            continue
        importer(instance, field_name, from_jsonable(carried))

    model_state = record.get("model_state") or {}
    if not model_state:
        return
    # The __dict__ membership filter is what makes the cast sound: the class
    # declares import_state as its own attribute. _ImportsState names that
    # duck-typed contract, so the call below is checked, not suppressed.
    by_name: "dict[str, _ImportsState]" = {
        klass.__name__: cast("_ImportsState", klass)
        for klass in type(instance).__mro__
        if "import_state" in klass.__dict__
    }
    for class_name, carried in model_state.items():
        klass = by_name.get(class_name)
        if klass is None:
            raise ModelException(
                f"carried state names {class_name!r}, which is not in the "
                f"destination model's MRO or defines no import_state"
            )
        klass.import_state(instance, from_jsonable(carried))


def _exists_many(keys: "list[str]") -> "dict[str, bool]":
    """Pipelined EXISTS over a batch of redis keys."""
    if not keys:
        return {}
    pipeline = POPOTO_REDIS_DB.pipeline()
    for key in keys:
        pipeline.exists(key)
    return {key: bool(value) for key, value in zip(keys, pipeline.execute())}


def _process_batch(
    model_class: "type[Model]",
    batch: "list[dict[str, Any]]",
    report: ImportReport,
    on_conflict: str,
    on_write_gate: str,
    drop_state: "set[str]",
) -> None:
    """Conflict-check, save, restore state, and reconcile one batch."""
    keys = [record["key"] for record in batch]
    existing = _exists_many(keys)

    bypass = on_write_gate == "bypass"
    landed: "list[RecordOutcome]" = []

    for record in batch:
        key = record["key"]
        if existing.get(key):
            if on_conflict == "error":
                raise ModelException(
                    f"key {key!r} already exists on the destination. Import is "
                    f"not atomic across records, so records before this one "
                    f"are already written. Re-run with on_conflict='overwrite' "
                    f"to converge, or on_conflict='skip' to merge without "
                    f"disturbing existing records."
                )
            if on_conflict == "skip":
                report.add(key, SKIPPED, "key already exists on the destination")
                continue

        values = from_jsonable(record.get("values") or {})

        # --- stage 1a: construction -------------------------------------
        try:
            instance = model_class(**values)
        except Exception as exc:
            report.add(
                key, REJECTED, f"construction failed: {type(exc).__name__}: {exc}"
            )
            continue

        # --- stage 1b: save ---------------------------------------------
        try:
            result = instance.save(skip_auto_now=True, skip_write_filter=bypass)
        except Exception as exc:
            report.add(key, ERRORED, f"save failed: {type(exc).__name__}: {exc}")
            continue

        # Rejection detection. NEVER truthiness: on the success path save()
        # returns the HSET reply count, and HSET returns 0 when every field
        # already existed -- so a successful overwrite returns 0 and a
        # truthiness test would misreport it as rejected. Only the two
        # sentinels below mean "refused".
        #
        # Precedence rule: this return value is authoritative for
        # classification. The batch EXISTS below is a corroborating check that
        # may only downgrade landed -> missing; it may NEVER upgrade
        # rejected -> landed. Under on_conflict="overwrite" a write-gate
        # rejection returns False before any HSET and leaves the destination's
        # OLD hash intact, so a post-write EXISTS returns 1 for a record none
        # of whose imported values were written. That is why records on the
        # collision path are excluded from the EXISTS reconciliation entirely:
        # EXISTS was already true before the write, so its value afterwards
        # carries no information.
        if result is False or result is None:
            report.add(
                key,
                REJECTED,
                "save() returned falsy: the destination's write gate refused "
                "the record (pass on_write_gate='bypass' to override) or an "
                "application-level save() override rejected it",
            )
            continue

        if bypass:
            report.write_gate_bypassed += 1

        # --- stage 2: carried state -------------------------------------
        try:
            _restore_state(model_class, instance, record, drop_state)
        except Exception as exc:
            report.add(
                key,
                PARTIAL,
                f"saved, but restoring carried state failed: "
                f"{type(exc).__name__}: {exc}. The record exists with "
                f"rebuild-default auxiliary state.",
            )
            continue

        outcome = report.add(key, LANDED)
        if not existing.get(key):
            landed.append(outcome)

    # Reconciliation: ground-truth the landed records that were NOT on the
    # collision path. Downgrade only -- never an upgrade.
    confirmed = _exists_many([outcome.key for outcome in landed])
    for outcome in landed:
        if not confirmed.get(outcome.key):
            outcome.category = ERRORED
            outcome.reason = (
                "save() reported success but the key is absent from the "
                "destination afterwards; the write did not survive"
            )


def import_records(
    model_class: "type[Model]",
    stream: TextIO,
    on_conflict: str = "error",
    on_write_gate: str = "reject",
    on_embedding_mismatch: str = "error",
) -> ImportReport:
    """Import records from a JSON Lines export into ``model_class``.

    Keys are always preserved, so re-running an import converges rather than
    duplicating, and every ``Relationship`` value and application-level key
    pointer keeps pointing at the same record.

    Args:
        model_class: The destination Model class.
        stream: A text file-like object positioned at the manifest line.
        on_conflict: What to do when the destination already holds a key.
            ``"error"`` (default) refuses -- the only mode that cannot
            clobber; ``"skip"`` leaves the existing record untouched;
            ``"overwrite"`` replaces it, which makes a re-run idempotent.
        on_write_gate: ``"reject"`` (default) honors the destination model's
            ``WriteFilterMixin`` gate and reports every refusal;
            ``"bypass"`` writes around it. Bypass only disables Popoto's own
            gate -- an application's ``save()`` override returning falsy still
            surfaces as a rejection.
        on_embedding_mismatch: ``"error"`` (default) refuses when the export's
            provider fingerprint differs from the destination's; ``"carry"``
            imports the vectors anyway; ``"regenerate"`` drops them so
            ``on_save`` re-embeds.

    Returns:
        An :class:`ImportReport` accounting for every record line as landed,
        skipped, rejected, errored, or partial, with a reason per non-landed
        record.

    Raises:
        ValueError: If a policy argument is not one of its allowed values.
        ModelException: If the manifest is missing, the format version is
            unsupported, the model name does not match, an embedding
            provenance mismatch is refused, or ``on_conflict="error"`` hits a
            collision. The first three raise before anything is written.

    Note:
        Import is not atomic across records and assumes the destination is not
        under concurrent write for this model. The recovery path for an
        interrupted run is to re-run with ``on_conflict="overwrite"``.

    Example:
        with open("memories.jsonl") as fh:
            report = Memory.import_records(fh, on_conflict="overwrite")
        print(report.summary())
    """
    _check_choice("on_conflict", on_conflict, _ON_CONFLICT)
    _check_choice("on_write_gate", on_write_gate, _ON_WRITE_GATE)
    _check_choice(
        "on_embedding_mismatch", on_embedding_mismatch, _ON_EMBEDDING_MISMATCH
    )

    report = ImportReport(model=model_class.__name__)

    lines = iter_lines(stream)
    manifest = None
    for _line_number, raw in lines:
        try:
            manifest = parse_line(raw)
        except ValueError as exc:
            raise ModelException(
                f"first line of the export is not valid JSON: {exc}"
            ) from exc
        break

    manifest = _validate_manifest(model_class, manifest)
    report.source_matched_count = manifest.get("matched_count")
    report.fidelity = {
        **(manifest.get("fields") or {}),
        **(manifest.get("mixins") or {}),
    }
    drop_state = _resolve_embedding_provenance(
        model_class, manifest, on_embedding_mismatch, report
    )

    batch: "list[dict[str, Any]]" = []
    for line_number, raw in lines:
        try:
            record = parse_line(raw)
        except ValueError as exc:
            report.add(f"line {line_number}", ERRORED, f"malformed JSON line: {exc}")
            continue
        key = record.get("key")
        if not isinstance(key, str) or not key:
            report.add(
                f"line {line_number}",
                ERRORED,
                "record has no usable 'key'; keys are always preserved, so a "
                "record without one cannot be placed",
            )
            continue
        batch.append(record)
        if len(batch) >= BATCH_SIZE:
            _process_batch(
                model_class, batch, report, on_conflict, on_write_gate, drop_state
            )
            batch = []

    if batch:
        _process_batch(
            model_class, batch, report, on_conflict, on_write_gate, drop_state
        )

    return report
