"""Audit and migration for ``KeyField(type=datetime)`` rows (#537, #538).

Two things put a datetime KeyField row on a non-canonical key:

- It was written before 1.9.0, when identity was ``str(value)`` and therefore
  carried the UTC offset only when the value happened to be aware.
- It was written before 1.8.2 *and* duplicated by the bug #521 fixed: an aware
  value reloaded naive, so a re-save derived a different key, wrote a second
  hash and orphaned the first. Both hashes are real records and they may have
  diverged, because writes could land on either.

``audit_datetime_keys`` finds both. It is strictly read-only.
``migrate_datetime_keys`` moves the unambiguous rows onto their canonical key
and **refuses to touch a duplicate pair**, because choosing which copy survives
is not a decision a library can make for an operator -- see
:class:`DatetimeKeyCollision`.

Both are exposed as ``Model`` classmethods; this module holds the
implementation because ``base.py`` is already large. See migration cookbook
recipes 19 and 20 for the operator procedure.
"""

import datetime
import logging
import re
from collections.abc import Iterator

from redis.exceptions import ResponseError

from ..redis_db import POPOTO_REDIS_DB, ENCODING
from .canonical_key import canonical_key_str
from .db_key import DB_key

logger = logging.getLogger("POPOTO.datetime_key_migration")

#: Canonical: ``2026-08-07T05:00:00.123456Z``. ``T``-separated, ``Z``-terminated.
SHAPE_CANONICAL = "canonical"

#: Pre-1.9.0, value decoded naive: ``2026-08-07 05:00:00.123456``. Space
#: separator, no offset. This is what ``str()`` produced for a naive datetime,
#: and (before 1.8.2) for an aware one whose offset had been dropped on read.
SHAPE_LEGACY_NAIVE = "legacy-naive"

#: Pre-1.9.0, value decoded aware: ``2026-08-07 12:00:00.123456+07:00``.
SHAPE_LEGACY_OFFSET = "legacy-offset"

#: Not a datetime rendering this module recognizes. Reported, never touched.
SHAPE_UNPARSABLE = "unparsable"

_CANONICAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_LEGACY_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d{1,6})?"
    r"(?P<offset>[+-]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?)?$"
)


def classify_key_value(rendered: str) -> str:
    """Classify a stored key partial by shape alone, without loading its hash.

    Shape is sufficient because the three forms are mutually exclusive by
    construction: canonical is the only one with a ``T`` separator and a
    trailing ``Z``, and the two legacy forms differ by the presence of an
    offset. That is a deliberate property of the canonical format, not a
    coincidence -- it is what makes the audit cheap.
    """
    if _CANONICAL_RE.match(rendered):
        return SHAPE_CANONICAL
    match = _LEGACY_RE.match(rendered)
    if match:
        return SHAPE_LEGACY_OFFSET if match.group("offset") else SHAPE_LEGACY_NAIVE
    return SHAPE_UNPARSABLE


def parse_key_value(rendered: str) -> "datetime.datetime | None":
    """Parse a stored key partial back to the datetime it denotes.

    Returns ``None`` for anything unrecognized rather than guessing. A legacy
    offset-free rendering is read as UTC, which is #537's assumption and the
    same doctrine ``convert_to_numeric`` has applied to scores since #519 -- and
    which is safe to apply *here* precisely because the canonical key does not
    encode awareness, so adopting it moves no key byte.
    """
    shape = classify_key_value(rendered)
    if shape == SHAPE_UNPARSABLE:
        return None
    # ``fromisoformat`` does not accept a trailing "Z" on the 3.10 floor.
    normalized = rendered[:-1] + "+00:00" if shape == SHAPE_CANONICAL else rendered
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed


class DatetimeKeyRow:
    """One scanned row: where it lives, what shape it is, where it belongs."""

    __slots__ = ("redis_key", "rendered_value", "shape", "canonical_redis_key")

    def __init__(
        self,
        redis_key: str,
        rendered_value: str,
        shape: str,
        canonical_redis_key: "str | None",
    ) -> None:
        self.redis_key = redis_key
        self.rendered_value = rendered_value
        self.shape = shape
        self.canonical_redis_key = canonical_redis_key

    @property
    def is_canonical(self) -> bool:
        return self.redis_key == self.canonical_redis_key

    def __repr__(self) -> str:
        return f"<DatetimeKeyRow {self.redis_key} ({self.shape})>"


class DatetimeKeyCollision:
    """Two or more distinct hashes that belong on one canonical key.

    This is #538's duplicate pair. The orphan is a real record with real field
    values, and because writes could land on either hash after they diverged,
    neither copy is knowably the right one. ``differing_fields`` is the whole
    point of the report: an empty set means the copies agree and the choice is
    free, a non-empty set means data will be lost whichever copy is kept.
    """

    __slots__ = ("canonical_redis_key", "rows", "contents", "key_field_name")

    def __init__(
        self,
        canonical_redis_key: str,
        rows: "list[DatetimeKeyRow]",
        contents: "dict[str, dict]",
        key_field_name: "str | None" = None,
    ) -> None:
        self.canonical_redis_key = canonical_redis_key
        self.rows = rows
        #: ``{redis_key: {field_name: value}}`` for every colliding hash.
        self.contents = contents
        self.key_field_name = key_field_name

    @property
    def differing_fields(self) -> set:
        """Field names whose value is not identical across every copy.

        The datetime KeyField itself is excluded. The copies necessarily store
        different renderings of it -- that is what a duplicate pair *is*, one
        hash holding the aware value and one the naive one -- but they denote
        the same instant, which is why they collided. Reporting it as a
        difference would bury the differences that actually cost data.
        """
        differing = set()
        all_fields = set()
        for fields in self.contents.values():
            all_fields |= set(fields)
        all_fields.discard(self.key_field_name)
        for field_name in all_fields:
            seen = [fields.get(field_name) for fields in self.contents.values()]
            if any(value != seen[0] for value in seen[1:]):
                differing.add(field_name)
        return differing

    def __repr__(self) -> str:
        return (
            f"<DatetimeKeyCollision {self.canonical_redis_key} "
            f"({len(self.rows)} hashes, {len(self.differing_fields)} differing)>"
        )


#: Field types whose per-instance data is keyed by the *instance's* redis_key
#: through a path this module's ``_side_keys`` does not cover -- unlike
#: ``IndexedFieldMixin`` pointers and capped ``ListField`` list keys, which
#: are derivable from the model's declared fields and are renamed alongside
#: the hash. Renaming the hash without also moving these leaves them pointing
#: at a key that no longer holds the row, with no detection and no
#: disclosure: e.g. ``ConfidenceField`` keys a ``:data`` hash off
#: ``base_key.redis_key``, ``EmbeddingField`` derives its ``.npy`` path from a
#: hash of ``redis_key``, and ``BM25Field``/``CoOccurrenceField``/
#: ``ContentField`` all store or index data the same way. Imported lazily and
#: each entry optional, because some of these are gated behind extras
#: (``EmbeddingField`` needs numpy) and may not be importable.
def _per_instance_field_types() -> "tuple[type, ...]":
    resolved: "list[type]" = []
    try:
        from ..fields.bm25_field import BM25Field

        resolved.append(BM25Field)
    except ImportError:  # pragma: no cover - always installed today
        pass
    try:
        from ..fields.confidence_field import ConfidenceField

        resolved.append(ConfidenceField)
    except ImportError:  # pragma: no cover - always installed today
        pass
    try:
        from ..fields.co_occurrence_field import CoOccurrenceField

        resolved.append(CoOccurrenceField)
    except ImportError:  # pragma: no cover - always installed today
        pass
    try:
        from ..fields.content_field import ContentField

        resolved.append(ContentField)
    except ImportError:  # pragma: no cover - always installed today
        pass
    try:
        from ..fields.embedding_field import EmbeddingField

        resolved.append(EmbeddingField)
    except ImportError:  # pragma: no cover - optional extra not installed
        pass
    return tuple(resolved)


class PerInstanceFieldRisk:
    """A field on the model being migrated whose per-instance data a rename orphans.

    Declaration is the signal, not stored usage: unlike an inbound
    ``Relationship`` (declared on some *other* model and only a problem when a
    row actually points here), one of these field types on the model *being
    migrated* creates its per-instance structure the first time any row sets
    it, so waiting for evidence of use would still miss every row that has not
    written one yet. Reported, never rewritten -- same posture as
    :class:`InboundRelationship`.
    """

    __slots__ = ("field_name", "field_type_name")

    def __init__(self, field_name: str, field_type_name: str) -> None:
        self.field_name = field_name
        self.field_type_name = field_type_name

    def __repr__(self) -> str:
        return f"<PerInstanceFieldRisk {self.field_name} ({self.field_type_name})>"


def _find_per_instance_field_risks(model_class) -> "list[PerInstanceFieldRisk]":
    """Fields declared on *model_class* whose per-instance data a rename would orphan."""
    risky_types = _per_instance_field_types()
    if not risky_types:
        return []
    return [
        PerInstanceFieldRisk(field_name, type(field).__name__)
        for field_name, field in model_class._meta.fields.items()
        if isinstance(field, risky_types)
    ]


class InboundRelationship:
    """A ``Relationship`` field on another model pointing at the model migrated.

    A Relationship stores the target's ``redis_key`` as a *value* inside another
    model's hash, so renaming a hash silently breaks every inbound reference.
    Reported, never rewritten: a Relationship value is indistinguishable from an
    ordinary string field's content, and a blind rewrite could corrupt unrelated
    data.
    """

    __slots__ = ("model_name", "field_name", "reference_count")

    def __init__(self, model_name: str, field_name: str, reference_count: int) -> None:
        self.model_name = model_name
        self.field_name = field_name
        self.reference_count = reference_count

    def __repr__(self) -> str:
        return (
            f"<InboundRelationship {self.model_name}.{self.field_name} "
            f"({self.reference_count} refs)>"
        )


class DatetimeKeyAudit:
    """Read-only report. Nothing in this class writes to Redis."""

    def __init__(
        self,
        model_name: str,
        field_name: "str | None",
        rows: "list[DatetimeKeyRow] | None" = None,
        collisions: "list[DatetimeKeyCollision] | None" = None,
        inbound_relationships: "list[InboundRelationship] | None" = None,
        per_instance_field_risks: "list[PerInstanceFieldRisk] | None" = None,
        applicable: bool = True,
    ) -> None:
        self.model_name = model_name
        self.field_name = field_name
        self.rows = rows or []
        self.collisions = collisions or []
        self.inbound_relationships = inbound_relationships or []
        self.per_instance_field_risks = per_instance_field_risks or []
        #: False when the model has no datetime KeyField at all -- a clean
        #: "nothing to do here", not an error.
        self.applicable = applicable

    @property
    def scanned(self) -> int:
        return len(self.rows)

    @property
    def canonical_rows(self) -> "list[DatetimeKeyRow]":
        return [row for row in self.rows if row.shape == SHAPE_CANONICAL]

    @property
    def unparsable_rows(self) -> "list[DatetimeKeyRow]":
        return [row for row in self.rows if row.shape == SHAPE_UNPARSABLE]

    @property
    def migratable_rows(self) -> "list[DatetimeKeyRow]":
        """Rows that can move on their own: non-canonical and uncontested."""
        colliding = {
            row.redis_key for collision in self.collisions for row in collision.rows
        }
        return [
            row
            for row in self.rows
            if not row.is_canonical
            and row.shape != SHAPE_UNPARSABLE
            and row.redis_key not in colliding
        ]

    @property
    def is_clean(self) -> bool:
        """True when nothing needs migrating and nothing collides."""
        return not self.migratable_rows and not self.collisions

    def __repr__(self) -> str:
        return (
            f"<DatetimeKeyAudit {self.model_name}.{self.field_name} "
            f"scanned={self.scanned} migratable={len(self.migratable_rows)} "
            f"collisions={len(self.collisions)}>"
        )

    def __str__(self) -> str:
        if not self.applicable:
            return (
                f"{self.model_name}: no KeyField(type=datetime) declared. "
                f"Nothing to audit."
            )
        lines = [
            f"Datetime KeyField audit: {self.model_name}.{self.field_name}",
            f"  scanned:     {self.scanned}",
            f"  canonical:   {len(self.canonical_rows)}",
            f"  migratable:  {len(self.migratable_rows)}",
            f"  collisions:  {len(self.collisions)}",
            f"  unparsable:  {len(self.unparsable_rows)}",
        ]
        for collision in self.collisions:
            lines.append("")
            lines.append(f"  COLLISION -> {collision.canonical_redis_key}")
            differing = collision.differing_fields
            for row in collision.rows:
                lines.append(f"    {row.redis_key}  ({row.shape})")
                for field_name in sorted(collision.contents.get(row.redis_key, {})):
                    marker = "*" if field_name in differing else " "
                    value = collision.contents[row.redis_key][field_name]
                    lines.append(f"      {marker} {field_name} = {value!r}")
            if differing:
                lines.append(
                    f"    copies DIFFER on: {', '.join(sorted(differing))}. "
                    f"Keeping either loses data; reconcile by hand."
                )
            else:
                lines.append("    copies are identical; either may be kept.")
        if self.inbound_relationships:
            lines.append("")
            lines.append("  INBOUND RELATIONSHIPS (renaming breaks these):")
            for inbound in self.inbound_relationships:
                lines.append(
                    f"    {inbound.model_name}.{inbound.field_name}: "
                    f"{inbound.reference_count} reference(s)"
                )
        if self.per_instance_field_risks:
            lines.append("")
            lines.append(
                "  PER-INSTANCE FIELD RISK (renaming orphans this field's data):"
            )
            for risk in self.per_instance_field_risks:
                lines.append(f"    {risk.field_name}: {risk.field_type_name}")
        if self.unparsable_rows:
            lines.append("")
            lines.append("  UNPARSABLE (left untouched):")
            for row in self.unparsable_rows:
                lines.append(f"    {row.redis_key}")
        return "\n".join(lines)


class DatetimeKeyMigrationReport:
    """What ``migrate_datetime_keys`` did, or would have done under ``dry_run``."""

    def __init__(
        self,
        model_name: str,
        field_name: "str | None",
        dry_run: bool,
        audit: "DatetimeKeyAudit",
    ) -> None:
        self.model_name = model_name
        self.field_name = field_name
        self.dry_run = dry_run
        self.audit = audit
        #: ``[(old_key, new_key), ...]`` moved, or that would move.
        self.moved: "list[tuple[str, str]]" = []
        #: ``[(old_key, new_key, reason), ...]`` attempted and declined.
        self.skipped: "list[tuple[str, str, str]]" = []
        #: True when the run stopped before moving anything.
        self.refused = False
        self.refusal_reason: "str | None" = None

    @property
    def moved_count(self) -> int:
        return len(self.moved)

    def __repr__(self) -> str:
        return (
            f"<DatetimeKeyMigrationReport {self.model_name} "
            f"dry_run={self.dry_run} moved={self.moved_count} "
            f"skipped={len(self.skipped)} refused={self.refused}>"
        )

    def __str__(self) -> str:
        if self.refused:
            return (
                f"{self.model_name}.{self.field_name}: migration REFUSED, "
                f"nothing was changed.\n  {self.refusal_reason}\n\n{self.audit}"
            )
        verb = "would move" if self.dry_run else "moved"
        lines = [
            f"{self.model_name}.{self.field_name}: {verb} {self.moved_count} row(s)"
        ]
        for old_key, new_key in self.moved:
            lines.append(f"  {old_key}\n    -> {new_key}")
        for old_key, new_key, reason in self.skipped:
            lines.append(f"  SKIPPED {old_key} -> {new_key}: {reason}")
        if self.dry_run:
            lines.append("")
            lines.append("Dry run: nothing was written. Re-run with dry_run=False.")
        return "\n".join(lines)


def _as_str(value) -> str:
    """Decode a Redis-returned key or field name to ``str``."""
    return value.decode(ENCODING) if isinstance(value, bytes) else str(value)


def _datetime_key_field_name(model_class) -> "str | None":
    """The model's single ``KeyField(type=datetime)`` name, or ``None``.

    Returns ``None`` for a model with two of them rather than guessing which
    one to key the audit on; that shape has never been exercised and silently
    auditing half of it would be worse than declining.
    """
    names = [
        field_name
        for field_name in model_class._meta.key_field_names
        if getattr(model_class._meta.fields[field_name], "type", None)
        is datetime.datetime
    ]
    if len(names) == 1:
        return names[0]
    if len(names) > 1:
        logger.warning(
            "%s declares %d KeyField(type=datetime) fields; the datetime key "
            "audit handles exactly one and is declining to guess.",
            model_class._meta.model_name,
            len(names),
        )
    return None


def _iter_instance_keys(model_class) -> "Iterator[str]":
    """Yield every stored instance key for *model_class*, as ``str``."""
    pattern = model_class._meta.db_class_key.redis_key + ":*"
    expected_length = model_class._meta.db_key_length
    for raw_key in POPOTO_REDIS_DB.scan_iter(match=pattern, count=1000):
        redis_key = _as_str(raw_key)
        # Same filter rebuild_indexes uses: side keys and other models' keys
        # can match the glob, real instance keys have an exact segment count.
        if len(redis_key.split(":")) != expected_length:
            continue
        yield redis_key


def _find_inbound_relationships(model_class) -> "list[InboundRelationship]":
    """Find Relationship fields on other models actually pointing at *model_class*.

    Counts *stored references*, not declarations. A model that declares a
    Relationship but holds no row pointing at this one breaks nothing when keys
    move, and reporting it would make the migration refuse to run over a
    hypothetical.
    """
    from .base import Model
    from .encoding import decode_popoto_model_hashmap

    inbound = []
    key_prefix = model_class._meta.db_class_key.redis_key + ":"

    def walk(cls) -> "Iterator[type]":
        for subclass in cls.__subclasses__():
            yield subclass
            yield from walk(subclass)

    for other in walk(Model):
        if other is model_class:
            continue
        meta = getattr(other, "_meta", None)
        if meta is None:
            continue
        pointing_fields = [
            field_name
            for field_name, field in meta.fields.items()
            if getattr(field, "model", None) is model_class
        ]
        if not pointing_fields:
            continue

        counts = {field_name: 0 for field_name in pointing_fields}
        for redis_key in _iter_instance_keys(other):
            redis_hash = POPOTO_REDIS_DB.hgetall(redis_key)
            if not redis_hash:
                continue
            fields = decode_popoto_model_hashmap(other, redis_hash, fields_only=True)
            decoded = {_as_str(name): value for name, value in (fields or {}).items()}
            for field_name in pointing_fields:
                # A Relationship stores the target's redis_key as a plain
                # string, so a live reference is one that starts with this
                # model's class-key prefix.
                raw = decoded.get(field_name)
                value = _as_str(raw) if raw is not None else None
                if value is not None and value.startswith(key_prefix):
                    counts[field_name] += 1

        for field_name, count in counts.items():
            if count:
                inbound.append(InboundRelationship(meta.model_name, field_name, count))
    return inbound


def _decode_contents(model_class, redis_key: str) -> dict:
    """Decode a hash to ``{field_name: value}`` for the collision report."""
    from .encoding import decode_popoto_model_hashmap

    redis_hash = POPOTO_REDIS_DB.hgetall(redis_key)
    if not redis_hash:
        return {}
    fields = decode_popoto_model_hashmap(model_class, redis_hash, fields_only=True)
    return {_as_str(name): value for name, value in (fields or {}).items()}


def audit_datetime_keys(model_class) -> DatetimeKeyAudit:
    """Report every non-canonical and every duplicated datetime-keyed row.

    Strictly read-only: this issues SCAN and HGETALL and nothing else. Safe to
    run against a live keyspace, and cheap enough to re-run as verification
    after a migration.
    """
    field_name = _datetime_key_field_name(model_class)
    model_name = model_class._meta.model_name
    if field_name is None:
        return DatetimeKeyAudit(model_name, None, applicable=False)

    position = model_class._meta.get_db_key_index_position(field_name)
    rows = []
    by_target: "dict[str, list[DatetimeKeyRow]]" = {}

    for redis_key in _iter_instance_keys(model_class):
        partials = DB_key.from_redis_key(redis_key)
        rendered = _as_str(partials[position])
        shape = classify_key_value(rendered)
        parsed = parse_key_value(rendered)

        if parsed is None:
            rows.append(DatetimeKeyRow(redis_key, rendered, SHAPE_UNPARSABLE, None))
            continue

        canonical_partials = list(partials)
        canonical_partials[position] = canonical_key_str(parsed)
        canonical_redis_key = DB_key(*canonical_partials).redis_key

        row = DatetimeKeyRow(redis_key, rendered, shape, canonical_redis_key)
        rows.append(row)
        by_target.setdefault(canonical_redis_key, []).append(row)

    collisions = []
    for canonical_redis_key, target_rows in by_target.items():
        if len(target_rows) < 2:
            continue
        contents = {
            row.redis_key: _decode_contents(model_class, row.redis_key)
            for row in target_rows
        }
        collisions.append(
            DatetimeKeyCollision(
                canonical_redis_key, target_rows, contents, key_field_name=field_name
            )
        )

    inbound = _find_inbound_relationships(model_class) if rows else []
    per_instance_field_risks = _find_per_instance_field_risks(model_class)

    return DatetimeKeyAudit(
        model_name,
        field_name,
        rows,
        collisions,
        inbound,
        per_instance_field_risks,
    )


def _side_keys(model_class, redis_key: str) -> "list[str]":
    """Every auxiliary key whose name embeds *redis_key*.

    Derived from the model's declared fields rather than found by globbing:
    a stored key contains ``clean()``-escaped characters, so a glob built from
    it would be both wrong and dangerous.
    """
    from ..fields.indexed_field_mixin import IndexedFieldMixin
    from ..fields.shortcuts import ListField

    keys = []
    for field_name, field in model_class._meta.fields.items():
        if isinstance(field, IndexedFieldMixin):
            keys.append((IndexedFieldMixin._pointer_side_key(redis_key, field_name),))
        if isinstance(field, ListField) and getattr(field, "_capped", False):
            keys.append((f"{redis_key}::{field_name}",))
    return [key for (key,) in keys]


def migrate_datetime_keys(
    model_class,
    dry_run: bool = True,
    allow_inbound_relationships: bool = False,
    allow_orphaned_per_instance_fields: bool = False,
) -> DatetimeKeyMigrationReport:
    """Move datetime-keyed rows onto their canonical key. Dry run by default.

    Idempotent and resumable: a row already on its canonical key is skipped, so
    re-running after a crash costs one scan and changes nothing.

    **Collisions are never resolved automatically.** When two hashes belong on
    one canonical key (#538's duplicate pair) this refuses to move anything at
    all and returns the audit, because the copies may have diverged and no
    default -- last-write-wins, newest-by-timestamp, field-wise merge -- is
    right often enough to be safe. Reconcile the pair by hand using the audit's
    per-field diff, then re-run. This is a deliberate omission, not a gap.

    Args:
        dry_run: When True (the default) nothing is written; the report lists
            what would move.
        allow_inbound_relationships: Renaming a hash breaks ``Relationship``
            values on other models that point at the old key, and this refuses
            to run when any exist. Pass True to acknowledge and proceed --
            the inbound references still are not rewritten, so repairing them
            is yours.
        allow_orphaned_per_instance_fields: A ``BM25Field``, ``EmbeddingField``,
            ``ConfidenceField``, ``CoOccurrenceField``, or ``ContentField`` on
            *this* model keys its own per-instance data off the instance's
            redis_key through a path this migration does not know how to
            rename, and this refuses to run when the model declares any of
            them. Pass True to acknowledge and proceed -- that field's data is
            not moved and is left keyed by the old redis_key, so repairing it
            (or confirming it does not need repair for your storage backend)
            is yours.

    Returns:
        A :class:`DatetimeKeyMigrationReport`.
    """
    audit = audit_datetime_keys(model_class)
    report = DatetimeKeyMigrationReport(
        audit.model_name, audit.field_name, dry_run, audit
    )

    if not audit.applicable:
        return report

    if audit.collisions:
        report.refused = True
        report.refusal_reason = (
            f"{len(audit.collisions)} canonical key(s) have more than one hash "
            f"(issue #538). The copies can have diverged, so choosing which one "
            f"survives is an operator decision. Reconcile them by hand -- the "
            f"audit below marks differing fields with '*' -- then re-run."
        )
        return report

    if audit.inbound_relationships and not allow_inbound_relationships:
        report.refused = True
        report.refusal_reason = (
            f"{len(audit.inbound_relationships)} model field(s) hold "
            f"Relationship references to {audit.model_name}. Renaming a hash "
            f"breaks them and they are not rewritten automatically. Re-run with "
            f"allow_inbound_relationships=True to proceed anyway."
        )
        return report

    if audit.per_instance_field_risks and not allow_orphaned_per_instance_fields:
        field_list = ", ".join(
            f"{risk.field_name} ({risk.field_type_name})"
            for risk in audit.per_instance_field_risks
        )
        report.refused = True
        report.refusal_reason = (
            f"{audit.model_name} declares {len(audit.per_instance_field_risks)} "
            f"field(s) whose per-instance data is keyed by the instance's "
            f"redis_key and would be orphaned by a rename: {field_list}. "
            f"Re-run with allow_orphaned_per_instance_fields=True to proceed "
            f"anyway -- that field's data is not moved."
        )
        return report

    if dry_run:
        for row in audit.migratable_rows:
            if row.canonical_redis_key is not None:
                report.moved.append((row.redis_key, row.canonical_redis_key))
        return report

    _apply_moves(model_class, audit, report)

    if report.moved:
        # One rebuild at the end re-derives $KeyF:, sorted, geo, IndexedField
        # and composite indexes from the hashes in their new locations. Doing
        # it per row would be O(n) full rebuilds.
        model_class.rebuild_indexes()

    return report


def _apply_moves(
    model_class, audit: DatetimeKeyAudit, report: DatetimeKeyMigrationReport
) -> DatetimeKeyMigrationReport:
    """Rename each migratable row onto its canonical key. Writes.

    Split out from :func:`migrate_datetime_keys` so the concurrency paths can
    be driven against a deliberately stale audit in tests.
    """
    for row in audit.migratable_rows:
        old_key, new_key = row.redis_key, row.canonical_redis_key
        if new_key is None:
            # Unreachable: migratable_rows excludes unparsable rows, which are
            # the only ones without a target. Asserted rather than assumed so a
            # future filter change cannot turn a missing target into a rename
            # against the string "None".
            raise AssertionError(f"migratable row with no canonical target: {old_key}")

        # RENAMENX, never RENAME: a target that already exists is a collision
        # the audit did not see (a concurrent writer), and it must fail loudly
        # rather than clobber. Redis *raises* on a missing source rather than
        # returning 0, so both non-success paths have to be caught.
        try:
            renamed = POPOTO_REDIS_DB.renamenx(old_key, new_key)
        except ResponseError:
            renamed = False

        if not renamed:
            if not POPOTO_REDIS_DB.exists(old_key) and POPOTO_REDIS_DB.exists(new_key):
                # Race 2: a live process loaded this row and saved it, which
                # renames it onto the same canonical target we computed. The
                # loser of that race is a no-op on an already-correct row, so
                # this is success, not an error.
                report.moved.append((old_key, new_key))
            elif not POPOTO_REDIS_DB.exists(old_key):
                report.skipped.append(
                    (old_key, new_key, "source key vanished during migration")
                )
            else:
                report.skipped.append(
                    (old_key, new_key, "target key already exists (concurrent write?)")
                )
            continue

        POPOTO_REDIS_DB.srem(model_class._meta.db_class_set_key.redis_key, old_key)
        POPOTO_REDIS_DB.sadd(model_class._meta.db_class_set_key.redis_key, new_key)

        # Side keys embed the hash key in their own name, so they orphan on
        # rename. Their names are derivable, so the migration owns them.
        for old_side_key, new_side_key in zip(
            _side_keys(model_class, old_key), _side_keys(model_class, new_key)
        ):
            if POPOTO_REDIS_DB.exists(old_side_key):
                POPOTO_REDIS_DB.rename(old_side_key, new_side_key)

        report.moved.append((old_key, new_key))

    return report
