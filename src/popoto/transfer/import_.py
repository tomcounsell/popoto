"""Import records from a JSON Lines export produced by :mod:`popoto.transfer`.

Keys are preserved by default. Records are saved one at a time -- deliberately
not through ``bulk_create`` -- because an external pipeline makes ``save()``
return the pipeline for every record and destroys per-record observability,
which the reconciliation ledger depends on.

``preserve_keys=False`` is the opt-out: every record gets a freshly minted key
and every field that *declares* a reference to another record is rewritten to
point at the new one. That mode reads the record stream twice (see
``_spool_and_mint``), because a reference on record 3 can point at record 4000
and the complete old-to-new map must exist before the first write. The default
path is untouched by it: one forward pass, no buffering, no map.
"""

from __future__ import annotations

import logging
import tempfile
from typing import TYPE_CHECKING, Any, Protocol, TextIO, cast

from ..exceptions import ModelException
from ..redis_db import get_REDIS_DB
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
    pipeline = get_REDIS_DB().pipeline()
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
    apply_remapped_references: bool = False,
) -> None:
    """Conflict-check, save, restore state, and reconcile one batch.

    ``apply_remapped_references`` is set only by the key-regenerating path,
    which stashes each record's rewritten reference strings under
    :data:`REMAPPED_REFERENCES_KEY`; see :func:`_remap_record` for why the
    re-assignment after construction is required.
    """
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
        remapped_refs = record.pop(REMAPPED_REFERENCES_KEY, None)

        # --- stage 1a: construction -------------------------------------
        try:
            instance = model_class(**values)
        except Exception as exc:
            report.add(
                key, REJECTED, f"construction failed: {type(exc).__name__}: {exc}"
            )
            continue

        # Re-assert the remapped reference strings that construction resolved
        # away. Not a no-op: see _remap_record.
        if apply_remapped_references and isinstance(remapped_refs, dict):
            for field_name, reference in remapped_refs.items():
                setattr(instance, field_name, reference)

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


_NO_KEY_REASON = (
    "record has no usable 'key'; a record without one can neither be placed "
    "nor have references remapped onto it"
)

REMAPPED_REFERENCES_KEY = "__remapped_references__"
"""In-memory slot on a record dict carrying its rewritten reference strings.

Set by :func:`_remap_record` and consumed by :func:`_process_batch` only when
that batch was produced by the regenerating path. Export never writes this
name, and the preserving path pops it and throws it away, so a file that
happens to carry one cannot use it to set relationship values.
"""


def _auto_key_field_name(model_class: "type[Model]") -> str:
    """Return the name of the model's single auto key field, or refuse.

    The predicate is two conditions, not one.
    ``_get_auto_key_field_name()`` reports the lone ``auto=True`` field and
    never consults ``key_field_names``, while ``_meta.add_field`` populates
    the two sets independently -- so a model declaring both
    ``slug = KeyField()`` and ``id = AutoKeyField()`` yields ``"id"`` from the
    helper while its key is ``{slug, id}``. Minting only ``id`` there would
    carry ``slug`` through verbatim and write a record whose key is half
    regenerated, which is why the key set must equal exactly the auto field.

    Raises:
        ModelException: If the model's key is anything other than exactly one
            ``auto=True`` field. Raised before a byte is written, like
            ``_validate_manifest``, so a refused import is a no-op.
    """
    auto_name = model_class._get_auto_key_field_name()
    key_names = set(getattr(model_class._meta, "key_field_names", ()) or ())
    if auto_name is not None and key_names == {auto_name}:
        return auto_name

    declared = ", ".join(sorted(key_names)) or "(none)"
    raise ModelException(
        f"preserve_keys=False cannot regenerate keys for "
        f"{model_class.__name__}: its key field(s) are {declared}, and "
        f"regeneration requires the key to be exactly one auto=True field "
        f"(an AutoKeyField, explicit or the implicit _auto_key). A declared "
        f"KeyField holds an application-chosen value with nothing to mint "
        f"from, and a key mixing an auto field with declared ones would only "
        f"be half regenerated. Import with preserve_keys=True instead."
    )


def _mint_key(model_class: "type[Model]", auto_key_name: str) -> "tuple[str, str]":
    """Mint one new key, returning ``(field_value, redis_key)``.

    The minter is an instance method on the ``Field`` object the metaclass
    already registered, so no throwaway ``Model`` instance is constructed per
    record. The redis_key is composed through ``DB_key`` rather than
    hand-formatted, and is correct here only because the caller has already
    established that this field is the model's whole key.
    """
    from ..models.db_key import DB_key

    model_field = model_class._meta.fields[auto_key_name]
    value = model_field.get_new_auto_key_value()
    return value, DB_key(model_class._meta.db_class_key, [value]).redis_key


def _reference_fields(model_class: "type[Model]") -> "dict[str, Any]":
    """Return the fields that declare a reference, by name.

    Duck-typed on the field's class overriding ``remap_references``, never on
    membership of a named class, so a third-party field that declares a
    reference participates with no change here.
    """
    from ..fields.field import Field

    base = getattr(Field.remap_references, "__func__", Field.remap_references)
    declared: "dict[str, Any]" = {}
    for field_name, model_field in model_class._meta.fields.items():
        hook = getattr(type(model_field), "remap_references", None)
        if hook is None:
            continue
        if getattr(hook, "__func__", hook) is not base:
            declared[field_name] = model_field
    return declared


def _spool_and_mint(
    model_class: "type[Model]",
    lines: "Any",
    auto_key_name: str,
    key_map: "dict[str, str]",
) -> "tuple[Any, list[int], dict[str, tuple[str, str]]]":
    """Pass 1: copy the record lines aside and mint a key for each.

    The lines are spooled to a ``TemporaryFile`` unconditionally -- there is
    no ``seek()``/``tell()`` fast path on the caller's stream, and the
    omission is deliberate. ``iter_lines`` advances the stream through the
    iteration protocol, and CPython's ``TextIOWrapper`` disables positional
    reporting once a file has been driven by ``__next__``: measured on 3.12,
    ``tell()`` after the manifest line raises ``OSError: telling position
    disabled by next() call`` on a real file while ``seekable()`` still
    reports True. ``io.StringIO`` has no decode buffer and is unaffected,
    which is what makes the bug invisible to a test suite written against it.

    Only the key map is held resident (short strings, one pair per record);
    the records themselves live in the temp file. Lines that cannot yield a
    key are spooled but not minted -- pass 2 reports them exactly as the
    preserving path does, which is why the original line numbers are carried
    alongside rather than recomputed from the temp file.

    Returns:
        ``(spool, line_numbers, minted)`` where ``spool`` is rewound to 0,
        ``line_numbers`` holds the source line number of each spooled line in
        order, and ``minted`` maps each old redis_key to its new
        ``(field_value, redis_key)`` pair.
    """
    spool = tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="")
    try:
        return _fill_spool(model_class, lines, auto_key_name, key_map, spool)
    except BaseException:
        # The caller's try/finally does not cover this function's own body:
        # get_new_auto_key_value() raises ImportError for the ulid/ksuid
        # strategies when the package is absent, which would escape with the
        # handle left to refcounting.
        spool.close()
        raise


def _fill_spool(
    model_class: "type[Model]",
    lines: "Any",
    auto_key_name: str,
    key_map: "dict[str, str]",
    spool: "Any",
) -> "tuple[Any, list[int], dict[str, tuple[str, str]]]":
    """Body of :func:`_spool_and_mint`, split out so the spool has an owner."""
    line_numbers: "list[int]" = []
    minted: "dict[str, tuple[str, str]]" = {}

    for line_number, raw in lines:
        spool.write(raw if raw.endswith("\n") else raw + "\n")
        line_numbers.append(line_number)
        try:
            record = parse_line(raw)
        except ValueError:
            continue
        old_key = record.get("key")
        if not isinstance(old_key, str) or not old_key:
            continue
        if old_key in minted:
            # A duplicated key in one file is degenerate -- export never
            # emits one. Reusing the first mint makes the second record
            # collide on the destination and fall to on_conflict, which is
            # what the preserving path does with the same file.
            continue
        value, new_key = _mint_key(model_class, auto_key_name)
        minted[old_key] = (value, new_key)
        key_map[old_key] = new_key

    spool.seek(0)
    return spool, line_numbers, minted


def _remap_record(
    record: "dict[str, Any]",
    reference_fields: "dict[str, Any]",
    key_map: "dict[str, str]",
) -> "tuple[int, int, list[str]]":
    """Rewrite a record's reference values in place.

    Also stashes the rewritten string values under
    :data:`REMAPPED_REFERENCES_KEY` so ``_process_batch`` can re-apply them
    after construction. That step is not redundant: ``Model.__init__``
    *eagerly* resolves a ``Relationship``'s redis_key string through
    ``query.get()`` (``models/base.py``) and silently substitutes ``None``
    when the target is not on the destination yet. Records are written one at
    a time in file order, so the first half of any cycle -- and every forward
    reference -- points at a key that does not exist at its own construction
    time and would lose the reference we just remapped. Re-assigning the
    string after construction bypasses that resolution and stores it verbatim,
    which is what the lazy-loading contract promises in the first place.

    Returns:
        ``(remapped, unmapped, targets)`` -- how many reference values this
        record pointed at a regenerated key, how many pointed at a key the map
        does not cover (those keep their old value and dangle), and the
        rewritten targets themselves so the caller can re-check them against
        the mints that turn out not to have landed.
    """
    values = record.get("values") or {}
    remapped = 0
    unmapped = 0
    targets: "list[str]" = []
    carried: "dict[str, str]" = {}
    for field_name, model_field in reference_fields.items():
        if field_name not in values:
            continue
        before = values[field_name]
        after = model_field.remap_references(field_name, before, key_map)
        values[field_name] = after
        if isinstance(after, str) and after:
            carried[field_name] = after
        if isinstance(before, str) and before:
            if after != before:
                remapped += 1
                if isinstance(after, str):
                    targets.append(after)
            else:
                unmapped += 1
    record["values"] = values
    # Assigned unconditionally so a value of this name read off the file is
    # replaced rather than honored.
    record[REMAPPED_REFERENCES_KEY] = carried
    return remapped, unmapped, targets


def _import_regenerating(
    model_class: "type[Model]",
    lines: "Any",
    report: ImportReport,
    on_conflict: str,
    on_write_gate: str,
    drop_state: "set[str]",
    key_map: "dict[str, str]",
) -> None:
    """Run the two-pass, key-regenerating import.

    Pass 1 mints; pass 2 re-reads the spooled lines, rewrites each record's
    key and reference values, and hands the result to the same
    ``_process_batch`` the preserving path uses -- construction, write gate,
    save and carried-state restoration are identical, only the key and the
    references differ.
    """
    auto_key_name = _auto_key_field_name(model_class)
    reference_fields = _reference_fields(model_class)

    spool, line_numbers, minted = _spool_and_mint(
        model_class, lines, auto_key_name, key_map
    )

    # (referencing record's new key, remapped, unmapped, rewritten targets)
    # per record. The counts are tallied at the end rather than as they are
    # produced, because pass 2 cannot know which records will survive the
    # write gate, a conflict, or a save error -- and a reference on a record
    # that never landed is in no destination at all. One short tuple per
    # record: the same order of residency as the key map, which is already
    # held.
    accounting: "list[tuple[str, int, int, list[str]]]" = []
    batch: "list[dict[str, Any]]" = []
    try:
        spooled = zip(iter_lines(spool), line_numbers, strict=True)
        for (_spool_line, raw), line_number in spooled:
            try:
                record = parse_line(raw)
            except ValueError as exc:
                report.add(
                    f"line {line_number}", ERRORED, f"malformed JSON line: {exc}"
                )
                continue
            old_key = record.get("key")
            if not isinstance(old_key, str) or not old_key:
                report.add(f"line {line_number}", ERRORED, _NO_KEY_REASON)
                continue

            new_value, new_key = minted[old_key]
            remapped, unmapped, targets = _remap_record(
                record, reference_fields, key_map
            )
            accounting.append((new_key, remapped, unmapped, targets))
            record["values"][auto_key_name] = new_value
            record["key"] = new_key

            batch.append(record)
            if len(batch) >= BATCH_SIZE:
                _process_batch(
                    model_class,
                    batch,
                    report,
                    on_conflict,
                    on_write_gate,
                    drop_state,
                    apply_remapped_references=True,
                )
                batch = []

        if batch:
            _process_batch(
                model_class,
                batch,
                report,
                on_conflict,
                on_write_gate,
                drop_state,
                apply_remapped_references=True,
            )
    finally:
        spool.close()
        # In the finally so the prune and the summary happen on every exit
        # path, rather than only when pass 2 runs to completion.
        in_storage, dropped_keys = _prune_unlanded_mints(report, minted, key_map)
        dropped = len(dropped_keys)
        # Only records that reached storage have references in the
        # destination at all. Of those, a reference rewritten to a sibling
        # that then failed to write is not remapped -- it dangles.
        remapped_total = 0
        unmapped_total = 0
        stale = 0
        for owner, remapped, unmapped, targets in accounting:
            if owner not in in_storage:
                continue
            owner_stale = sum(1 for target in targets if target in dropped_keys)
            stale += owner_stale
            remapped_total += remapped - owner_stale
            unmapped_total += unmapped + owner_stale
        report.warnings.append(
            f"key regeneration: {len(minted)} key(s) minted; {remapped_total} "
            f"reference value(s) remapped; {unmapped_total} reference value(s) "
            f"left pointing at keys absent from the key map (those references "
            f"dangle). Both counts cover the records that reached storage. "
            f"Only fields that declare a reference are remapped -- an "
            f"application-level pointer stored in a plain Field is never "
            f"rewritten."
            + (
                f" {dropped} minted key(s) were dropped from the key map "
                f"because their record did not land"
                + (
                    f"; {stale} of the dangling reference value(s) point at "
                    f"one of those."
                    if stale
                    else "."
                )
                if dropped
                else ""
            )
        )


def _prune_unlanded_mints(
    report: ImportReport,
    minted: "dict[str, tuple[str, str]]",
    key_map: "dict[str, str]",
) -> "tuple[set[str], set[str]]":
    """Drop mints whose record never reached storage.

    Pass 1 mints a key for every line that carries one, before anything is
    written, so the raw mint log over-reports: a record that conflicted, was
    rejected by the write gate or raised on save still has an entry. Handing
    that map to the next model's run would point its references at keys that
    do not exist. Only ``landed`` and ``partial`` records are in storage, so
    everything else is pruned here -- after pass 2, since pass 2's own
    reference remapping legitimately reads the full map.

    A seeded entry is only removed when this run overwrote it with its own
    mint; an untouched seed is left alone.

    Returns:
        ``(in_storage, dropped_keys)`` -- the new keys that reached storage,
        and the minted new keys that did not and were pruned. The caller uses
        the second set to move references aimed at a dropped mint out of the
        remapped count and into the dangling one.
    """
    in_storage = {
        outcome.key
        for outcome in report.outcomes
        if outcome.category in (LANDED, PARTIAL)
    }
    dropped_keys: "set[str]" = set()
    for old_key, (_new_value, new_key) in minted.items():
        if new_key in in_storage:
            continue
        if key_map.get(old_key) == new_key:
            del key_map[old_key]
            dropped_keys.add(new_key)
    return in_storage, dropped_keys


def import_records(
    model_class: "type[Model]",
    stream: TextIO,
    on_conflict: str = "error",
    on_write_gate: str = "reject",
    on_embedding_mismatch: str = "error",
    preserve_keys: bool = True,
    key_map: "dict[str, str] | None" = None,
) -> ImportReport:
    """Import records from a JSON Lines export into ``model_class``.

    By default keys are preserved, so re-running an import converges rather
    than duplicating, and every ``Relationship`` value and application-level
    key pointer keeps pointing at the same record.

    ``preserve_keys=False`` regenerates every key instead, and rewrites the
    references it can. Three properties of that mode matter before choosing
    it:

    - **It is not idempotent.** Every other mode converges on a re-run; this
      one mints fresh keys each time, so running it twice against the same
      destination leaves two copies of every record.
    - **The remap is partial, by design.** Only fields that *declare* a
      reference are rewritten -- ``Relationship`` is the only one Popoto
      ships. An application-level pointer stored in a plain
      ``Field(type=str)`` is indistinguishable from ordinary text, is never
      rewritten, and will dangle. Popoto does not guess: a heuristic that
      scanned strings for key-shaped values would turn a documented
      limitation into occasional silent corruption. The report names both
      counts so a dangling reference is visible in the run that created it.
    - **It requires a mintable key.** The destination model's key must be
      exactly one ``auto=True`` field; anything else is refused before a byte
      is written.

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
        preserve_keys: ``True`` (default) carries each record's key verbatim.
            ``False`` mints a new key per record and remaps declared
            references onto it -- see the three caveats above. Under
            ``False``, ``on_conflict`` still applies but covers only the
            vanishing-probability case of a minted key colliding with one
            already on the destination, not the merge semantics it has when
            keys are preserved.
        key_map: Only valid with ``preserve_keys=False``: a seed of old-to-new
            ``redis_key`` mappings from a previous import of *another* model,
            so a multi-model migration can carry cross-model references
            across runs. Feed run N's ``ImportReport.key_map`` in as run
            N+1's seed. A reference whose target is in no seed and not in
            this file keeps its old key and is counted as dangling.

    Returns:
        An :class:`ImportReport` accounting for every record line as landed,
        skipped, rejected, errored, or partial, with a reason per non-landed
        record. Under ``preserve_keys=False`` its ``key_map`` holds every
        mapping this run actually wrote, merged over the seed -- mints whose
        record did not land are pruned; see :class:`ImportReport`.

    Raises:
        ValueError: If a policy argument is not one of its allowed values, or
            if ``key_map`` is passed with ``preserve_keys=True`` (there is
            nothing to remap when no key changes).
        ModelException: If the manifest is missing, the format version is
            unsupported, the model name does not match, an embedding
            provenance mismatch is refused, ``preserve_keys=False`` is asked
            of a model whose key is not exactly one ``auto=True`` field, or
            ``on_conflict="error"`` hits a collision. Every one of those but
            the last raises before anything is written.

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
    if preserve_keys and key_map is not None:
        raise ValueError(
            "key_map is only meaningful with preserve_keys=False; when keys "
            "are preserved no reference needs remapping"
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

    if not preserve_keys:
        working_map: "dict[str, str]" = dict(key_map or {})
        report.key_map = working_map
        _import_regenerating(
            model_class,
            lines,
            report,
            on_conflict,
            on_write_gate,
            drop_state,
            working_map,
        )
        return report

    batch: "list[dict[str, Any]]" = []
    for line_number, raw in lines:
        try:
            record = parse_line(raw)
        except ValueError as exc:
            report.add(f"line {line_number}", ERRORED, f"malformed JSON line: {exc}")
            continue
        key = record.get("key")
        if not isinstance(key, str) or not key:
            report.add(f"line {line_number}", ERRORED, _NO_KEY_REASON)
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
