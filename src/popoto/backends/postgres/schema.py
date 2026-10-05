"""The Postgres schema compiler (#759 M1b, plan §3).

A :class:`~popoto.backends.types.ModelSpec` compiles to one typed table:

* ``_pk text PRIMARY KEY`` holds the canonical key string -- the same
  ``ClassName:…`` string Redis uses as the hash key, so ``Model.pk`` is the
  same on both backends.
* one typed column per field (the type map below); a ``DatetimeField`` adds
  ``<f>__utcoff integer`` (offset seconds, ``NULL`` = naive), which keeps the
  #521 round-trip contract that ``timestamptz`` alone drops.
* engine-owned ``_created_at`` / ``_updated_at`` (``[PG-only]``).
* ``UNIQUE`` over the key fields, a B-tree per further key field, and a B-tree
  ``(partition cols…, f)`` per ``SortedField``.
* M1.1 (plain-field breadth): an ``IndexedField`` / ``Relationship`` column
  gets a B-tree (``UNIQUE`` for a unique field), a ``TagField`` is ``text[]``
  with a GIN index, the collection fields are ``jsonb`` (:mod:`.codec`),
  ``BytesField`` is ``bytea``, and ``Meta.indexes`` compiles to composite
  B-tree / ``UNIQUE`` indexes.

The compiled shape is fingerprinted and recorded in ``popoto_schema`` the
first time a process binds the model. Create and additive changes (a new
nullable column, a new index) apply automatically under
``pg_advisory_xact_lock``; anything else -- a dropped or retyped column, a key
change, a newer format version, a schema some *other* client extended --
raises :class:`SchemaDriftError` rather than writing (§3 Migrations).

Pure except for :func:`ensure_table`, which takes an open connection. Never
imports ``redis``.
"""

from __future__ import annotations

import datetime
import decimal
import hashlib
import json
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Optional, Sequence

from ..types import BackendCapabilityError, ModelSpec, SchemaDriftError

__all__ = [
    "ENGINE_COLUMNS",
    "SCHEMA_FORMAT_VERSION",
    "Column",
    "TableSpec",
    "compile_table",
    "ensure_table",
    "quote_ident",
    "table_name_for",
]

SCHEMA_FORMAT_VERSION = 1
"""Bumped when the compiler's output shape changes incompatibly. A client
that finds a newer version in ``popoto_schema`` refuses to write."""

ENGINE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("_created_at", "timestamptz NOT NULL DEFAULT now()"),
    ("_updated_at", "timestamptz NOT NULL DEFAULT now()"),
    # #756's import contract (plan §3, M2 ``[PG-only]``): an importer records
    # where a row came from and which values it inferred. Every native write
    # sets _migrated_from to NULL, so a delta re-load can guard with
    # "WHERE _migrated_from IS NOT NULL" and never overwrite a row popoto has
    # since written; _estimated_fields is never touched by the engine.
    ("_migrated_from", "jsonb"),
    ("_estimated_fields", "text[]"),
)
"""Engine-owned columns on every table (plan §1.1, ``[PG-only]``)."""

UTCOFF_SUFFIX = "__utcoff"

#: Python type -> column type (plan §3, "Field → column type mapping").
SQL_TYPES: dict[type, str] = {
    str: "text",
    int: "bigint",
    float: "double precision",
    decimal.Decimal: "numeric",
    bool: "boolean",
    datetime.datetime: "timestamptz",
    datetime.date: "date",
    datetime.time: "time",
    bytes: "bytea",
    # Collections are jsonb, never msgpack (plan §3, §8; see .codec).
    list: "jsonb",
    tuple: "jsonb",
    set: "jsonb",
    dict: "jsonb",
}

#: Python types whose column is ``jsonb`` (encoded by :mod:`.codec`).
JSON_TYPES = frozenset({list, tuple, set, dict})

#: Field kinds with a B-tree on their own column (``UNIQUE`` when unique).
INDEXED_KINDS = frozenset({"IndexedField", "UniqueField"})
#: Field kinds stored as ``text[]`` with a GIN index (multi-value tags).
TAG_KINDS = frozenset({"TagField"})
#: Field kinds holding a related record's ``_pk`` (lazy, as on Redis).
RELATIONSHIP_KINDS = frozenset({"Relationship"})

#: Field kinds that are key fields (part of ``_pk``).
KEY_KINDS = frozenset({"KeyField", "UniqueKeyField", "AutoKeyField", "SortedKeyField"})
#: Field kinds that keep a sorted index.
SORTED_KINDS = frozenset({"SortedField", "SortedKeyField"})
#: Side-effect fields with no column of their own (M2b): their state lives
#: in companion tables (``.search``).
COLUMNLESS_KINDS = frozenset(
    {"BM25Field", "ExistenceFilter", "FrequencySketch", "CoOccurrenceField"}
)
"""(M4 adds ``CoOccurrenceField``: its edges live in ``.graph``'s table.)"""

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str) -> str:
    """Double-quote an identifier. Names reach here from field and model
    names, which Python already restricts; anything else is refused."""
    if not _IDENT.fullmatch(name):
        raise BackendCapabilityError(f"{name!r} is not a usable Postgres identifier")
    return f'"{name}"'


def _snake(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


def _bounded(name: str) -> str:
    """Postgres truncates identifiers past 63 bytes; keep long names unique
    by replacing the tail with a short hash instead."""
    if len(name) <= 63:
        return name
    digest = hashlib.sha1(name.encode()).hexdigest()[:8]
    return f"{name[:54]}_{digest}"


def table_name_for(model_name: str) -> str:
    """``popoto.<model_snake>`` (plan §3); the schema is the backend's."""
    return _bounded(_snake(model_name))


@dataclass(frozen=True)
class Column:
    """One stored column. ``field`` is the popoto field it belongs to (``None``
    for ``_pk``); ``role`` is ``"value"``, ``"utcoff"`` or ``"pk"``."""

    name: str
    sql_type: str
    field: Optional[str]
    role: str = "value"
    py_type: Optional[type] = None


@dataclass(frozen=True)
class TableSpec:
    """What one model compiles to. ``indexes`` maps index name -> the
    ``CREATE [UNIQUE] INDEX`` body after ``ON <table>``; both are part of the
    fingerprint."""

    model: str
    schema: str
    table: str
    columns: tuple[Column, ...]
    indexes: tuple[tuple[str, str, bool], ...]
    key_fields: tuple[str, ...]
    field_types: dict[str, type]
    unique_indexes: dict[str, tuple[str, ...]] = dataclass_field(default_factory=dict)
    """Unique index name -> the fields it covers (to name a 23505)."""
    field_kinds: dict[str, str] = dataclass_field(default_factory=dict)
    """Field name -> its popoto kind (``FieldSpec.kind``)."""
    capped_fields: frozenset[str] = frozenset()
    """``ListField(max_length=N)`` columns: elements are type-tagged (.codec)."""
    meta_indexes: dict[str, tuple[str, ...]] = dataclass_field(default_factory=dict)
    """``Meta.indexes`` unique index name -> its fields (to word a 23505)."""
    companions: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Companion tables (M2b): ``(name, DDL statements)``, each statement
    idempotent (``IF NOT EXISTS``), created with the table and on an additive
    migration. Part of the fingerprint when present."""
    search: Any = None
    """The compiled search layout (:class:`.search.SearchLayout`) for a model
    with BM25 / embedding / membership fields, else ``None``."""

    def kind(self, name: str) -> str:
        return self.field_kinds.get(name, "Field")

    def is_tag(self, name: str) -> bool:
        return self.kind(name) in TAG_KINDS

    def is_json(self, name: str) -> bool:
        return self.field_types.get(name) in JSON_TYPES and not self.is_tag(name)

    @property
    def qualified(self) -> str:
        return f"{quote_ident(self.schema)}.{quote_ident(self.table)}"

    @property
    def datetime_fields(self) -> tuple[str, ...]:
        """Fields stored as ``timestamptz`` + ``<f>__utcoff``."""
        return tuple(
            name for name, t in self.field_types.items() if t is datetime.datetime
        )

    def column_map(self) -> dict[str, str]:
        return {c.name: c.sql_type for c in self.columns}

    def fingerprint(self) -> str:
        shape: dict[str, Any] = {
            "format": SCHEMA_FORMAT_VERSION,
            "columns": sorted(self.column_map().items()),
            "indexes": sorted([list(i) for i in self.indexes]),
            "engine": [name for name, _ in ENGINE_COLUMNS],
        }
        if self.companions:
            shape["companions"] = [[n, list(s)] for n, s in self.companions]
        payload = json.dumps(shape, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def companion_sql(self) -> list[str]:
        return [stmt for _name, stmts in self.companions for stmt in stmts]

    def create_sql(self) -> list[str]:
        cols = ['"_pk" text PRIMARY KEY']
        cols += [
            f"{quote_ident(c.name)} {c.sql_type}"
            for c in self.columns
            if c.role != "pk"
        ]
        cols += [f"{quote_ident(name)} {decl}" for name, decl in ENGINE_COLUMNS]
        stmts = [f"CREATE TABLE {self.qualified} ({', '.join(cols)})"]
        stmts += [self.index_sql(i) for i in self.indexes]
        stmts += self.companion_sql()
        return stmts

    def index_sql(self, index: tuple[str, str, bool]) -> str:
        name, body, unique = index
        return (
            f"CREATE {'UNIQUE ' if unique else ''}INDEX IF NOT EXISTS "
            f"{quote_ident(name)} ON {self.qualified} {body}"
        )


def _field_type(spec: ModelSpec, name: str) -> type:
    fs = spec.fields[name]
    if fs.kind in RELATIONSHIP_KINDS:
        # The column holds the related record's _pk: a key string.
        return str
    py_type = fs.py_type or str
    if py_type not in SQL_TYPES:
        raise BackendCapabilityError(
            f"{spec.name}.{name}: type {getattr(py_type, '__name__', py_type)} has "
            "no Postgres column mapping yet"
        )
    return py_type


def compile_table(spec: ModelSpec, schema: str) -> TableSpec:
    """Compile ``spec`` to a :class:`TableSpec`. Pure."""
    table = table_name_for(spec.name)
    columns: list[Column] = [Column("_pk", "text", None, role="pk")]
    field_types: dict[str, type] = {}
    reserved = {"_pk"} | {name for name, _ in ENGINE_COLUMNS}
    for name in sorted(spec.fields):
        if name in reserved:
            raise BackendCapabilityError(
                f"{spec.name}.{name} collides with an engine-owned column"
            )
        if spec.fields[name].kind in COLUMNLESS_KINDS:
            # Side-effect fields (M2b): no value of their own, only companion
            # tables, which compile_search adds below.
            continue
        py_type = _field_type(spec, name)
        field_types[name] = py_type
        sql_type = SQL_TYPES[py_type]
        if spec.fields[name].kind in TAG_KINDS:
            sql_type = "text[]"
        columns.append(Column(name, sql_type, name, py_type=py_type))
        if py_type is datetime.datetime:
            columns.append(Column(name + UTCOFF_SUFFIX, "integer", name, role="utcoff"))
    # M2a: state columns the memory fields and mixins keep beside the field
    # values (confidence state, access tracking), from .memory.
    from .memory import memory_columns, memory_indexes

    taken = {c.name for c in columns}
    for state_col in memory_columns(spec):
        if state_col.name in taken or state_col.name in reserved:
            raise BackendCapabilityError(
                f"{spec.name}: column {state_col.name} collides with a field"
            )
        columns.append(state_col)

    indexes: list[tuple[str, str, bool]] = []
    unique_indexes: dict[str, tuple[str, ...]] = {}

    def index_name(*parts: str) -> str:
        return _bounded("__".join((table,) + parts + ("idx",)))

    key_fields = tuple(spec.key_fields)  # DB_key order
    if key_fields:
        cols = ", ".join(quote_ident(k) for k in key_fields)
        indexes.append((index_name("keys"), f"({cols})", True))
        unique_indexes[index_name("keys")] = key_fields
        for extra in key_fields[1:]:
            indexes.append((index_name(extra), f"({quote_ident(extra)})", False))
    for name, fs in sorted(spec.fields.items()):
        # A unique field (UniqueKeyField, unique=True) gets its own UNIQUE --
        # Redis enforces it in pre_save; here the index does, and a 23505 on
        # it becomes the same ModelException. A sole key field is already
        # unique through the key index.
        if fs.options.get("unique") and key_fields != (name,):
            indexes.append((index_name("uniq", name), f"({quote_ident(name)})", True))
            unique_indexes[index_name("uniq", name)] = (name,)
    for name, fs in sorted(spec.fields.items()):
        # IndexedField / Relationship: a B-tree on the column (a unique one
        # already has the UNIQUE above, which serves equality). TagField: GIN,
        # which serves @> (contains/all) and && (any).
        if fs.kind in TAG_KINDS:
            body = f"USING gin ({quote_ident(name)})"
            indexes.append((index_name(name), body, False))
        elif (
            fs.kind in INDEXED_KINDS or fs.kind in RELATIONSHIP_KINDS
        ) and not fs.options.get("unique"):
            indexes.append((index_name(name), f"({quote_ident(name)})", False))
    meta_indexes: dict[str, tuple[str, ...]] = {}
    unique_meta = {tuple(i) for i in spec.unique_indexes}
    for names in spec.indexes:
        # Meta.indexes: a composite B-tree, UNIQUE for is_unique=True. NULLs
        # stay distinct, as Redis skips a tuple with a None in it.
        unique = tuple(names) in unique_meta
        cols = ", ".join(quote_ident(n) for n in names)
        meta_name = index_name("meta", *names)
        indexes.append((meta_name, f"({cols})", unique))
        if unique:
            unique_indexes[meta_name] = tuple(names)
            meta_indexes[meta_name] = tuple(names)
    for name, fs in sorted(spec.fields.items()):
        if fs.kind not in SORTED_KINDS:
            continue
        partition = tuple(fs.options.get("partition_by", ()) or ())
        # The trailing _pk COLLATE "C" is the ORDER BY tie-break (Redis's
        # bytewise member order), so a range read with LIMIT is index-ordered.
        cols = ", ".join(quote_ident(c) for c in partition + (name,))
        body = f'({cols}, "_pk" COLLATE "C")'
        indexes.append((index_name("sort", name), body, False))
    # M2b: BM25 / embedding / membership fields add auxiliary columns, an HNSW
    # index and companion tables (imported here: search imports this module).
    from .search import compile_search

    search = compile_search(spec, schema, table, index_name)
    if search is not None:
        columns.extend(search.columns)
        indexes.extend(search.indexes)
    indexes.extend(memory_indexes(spec, index_name))
    # M4: one edge table per CoOccurrenceField (.graph).
    from .graph import compile_graph

    companions = (search.companions if search is not None else ()) + compile_graph(
        spec, schema, table
    )
    return TableSpec(
        model=spec.name,
        schema=schema,
        table=table,
        columns=tuple(columns),
        indexes=tuple(indexes),
        key_fields=key_fields,
        field_types=field_types,
        unique_indexes=unique_indexes,
        field_kinds={name: fs.kind for name, fs in spec.fields.items()},
        capped_fields=frozenset(
            name for name, fs in spec.fields.items() if fs.options.get("capped")
        ),
        meta_indexes=meta_indexes,
        companions=companions,
        search=search,
    )


# -- applying it --------------------------------------------------------------

POPOTO_SCHEMA_TABLE = "popoto_schema"


def _registry_sql(schema: str) -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS {quote_ident(schema)}.{POPOTO_SCHEMA_TABLE} ("
        "table_name text PRIMARY KEY, model text NOT NULL, "
        "fingerprint text NOT NULL, columns jsonb NOT NULL, "
        "indexes jsonb NOT NULL, ddl text NOT NULL, "
        "popoto_version text NOT NULL, format_version integer NOT NULL, "
        "applied_at timestamptz NOT NULL DEFAULT now())"
    )


def _popoto_version() -> str:
    try:
        from importlib.metadata import version

        return version("popoto")
    except Exception:  # pragma: no cover - uninstalled checkout
        return "unknown"


def _record(cur: Any, ts: TableSpec, ddl: Sequence[str], *, insert: bool) -> None:
    params = (
        ts.table,
        ts.model,
        ts.fingerprint(),
        json.dumps(ts.column_map()),
        json.dumps([list(i) for i in ts.indexes]),
        ";\n".join(ddl),
        _popoto_version(),
        SCHEMA_FORMAT_VERSION,
    )
    registry = f"{quote_ident(ts.schema)}.{POPOTO_SCHEMA_TABLE}"
    if insert:
        cur.execute(
            f"INSERT INTO {registry} (table_name, model, fingerprint, columns, "
            "indexes, ddl, popoto_version, format_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            params,
        )
    else:
        cur.execute(
            f"UPDATE {registry} SET model = %s, fingerprint = %s, columns = %s, "
            "indexes = %s, ddl = %s, popoto_version = %s, format_version = %s, "
            "applied_at = now() WHERE table_name = %s",
            params[1:] + (params[0],),
        )


def ensure_table(conn: Any, ts: TableSpec, *, auto: bool) -> str:
    """Create or check ``ts`` on ``conn`` inside one transaction, under an
    advisory transaction lock (PgBouncer transaction-mode safe). Returns what
    it did: ``"created"``, ``"migrated"`` or ``"current"``.

    Raises :class:`SchemaDriftError` for every case it will not reconcile.
    """
    schema_q = quote_ident(ts.schema)
    registry = f"{schema_q}.{POPOTO_SCHEMA_TABLE}"
    with conn.transaction():
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"popoto:ddl:{ts.schema}",)
        )
        exists = cur.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = %s", (ts.schema,)
        ).fetchone()
        if not exists:
            if not auto:
                raise SchemaDriftError(
                    f"schema {ts.schema!r} does not exist and POPOTO_SCHEMA_AUTO=0"
                )
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_q}")
        cur.execute(_registry_sql(ts.schema))
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"popoto:ddl:{ts.schema}.{ts.table}",),
        )
        row = cur.execute(
            f"SELECT model, fingerprint, columns, indexes, format_version, "
            f"popoto_version FROM {registry} WHERE table_name = %s",
            (ts.table,),
        ).fetchone()
        table_exists = cur.execute(
            "SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename = %s",
            (ts.schema, ts.table),
        ).fetchone()

        if row is None:
            if table_exists:
                raise SchemaDriftError(
                    f"{ts.schema}.{ts.table} exists but popoto_schema has no record "
                    f"for it; popoto will not adopt a table it did not create "
                    f"(model {ts.model})"
                )
            if not auto:
                raise SchemaDriftError(
                    f"{ts.schema}.{ts.table} does not exist and POPOTO_SCHEMA_AUTO=0 "
                    f"(model {ts.model})"
                )
            ddl = ts.create_sql()
            for stmt in ddl:
                cur.execute(stmt)
            _record(cur, ts, ddl, insert=True)
            return "created"

        stored_model, fingerprint, stored_cols, stored_idx, fmt, version = row
        if fmt > SCHEMA_FORMAT_VERSION:
            raise SchemaDriftError(
                f"{ts.schema}.{ts.table} was written by popoto {version} with schema "
                f"format {fmt}; this client understands format "
                f"{SCHEMA_FORMAT_VERSION} and will not write to it. Upgrade popoto."
            )
        if stored_model != ts.model:
            raise SchemaDriftError(
                f"{ts.schema}.{ts.table} belongs to model {stored_model!r}, not "
                f"{ts.model!r}: two models map to one table"
            )
        if fingerprint == ts.fingerprint():
            return "current"

        ours = ts.column_map()
        theirs = dict(stored_cols)
        our_idx = {tuple(i) for i in ts.indexes}
        their_idx = {tuple(i) for i in stored_idx}
        problems = []
        for name, sql_type in theirs.items():
            if name not in ours:
                problems.append(
                    f"column {name} ({sql_type}) exists in the database but not in "
                    "this model: a field was removed, or a newer client extended "
                    "the table"
                )
            elif ours[name] != sql_type:
                problems.append(
                    f"column {name} is {sql_type}, model wants {ours[name]}"
                )
        for idx in their_idx - our_idx:
            problems.append(
                f"index {idx[0]} ({idx[1]}) exists in the database but not in this "
                "model (key or sorted-field change)"
            )
        if problems:
            raise SchemaDriftError(
                f"{ts.schema}.{ts.table} (model {ts.model}) differs from the stored "
                f"schema (written by popoto {version}) in ways popoto will not "
                "change automatically; migrate it by hand:\n  " + "\n  ".join(problems)
            )
        if not auto:
            raise SchemaDriftError(
                f"{ts.schema}.{ts.table} needs an additive change and "
                "POPOTO_SCHEMA_AUTO=0"
            )
        ddl = [
            f"ALTER TABLE {ts.qualified} ADD COLUMN IF NOT EXISTS "
            f"{quote_ident(c.name)} {c.sql_type}"
            for c in ts.columns
            if c.name not in theirs
        ]
        ddl += [ts.index_sql(i) for i in ts.indexes if tuple(i) not in their_idx]
        # Engine columns added since the table was created (M2b's
        # _migrated_from / _estimated_fields), and companion tables: both
        # idempotent, so re-issuing an existing one is a no-op.
        ddl += [
            f"ALTER TABLE {ts.qualified} ADD COLUMN IF NOT EXISTS "
            f"{quote_ident(name)} {decl}"
            for name, decl in ENGINE_COLUMNS
        ]
        ddl += ts.companion_sql()
        for stmt in ddl:
            cur.execute(stmt)
        _record(cur, ts, ddl, insert=False)
        return "migrated"
