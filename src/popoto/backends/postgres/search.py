"""Search on Postgres: BM25, pgvector, exact membership, recall (#759 M2b).

What the Redis side keeps in ``$BM25:``/``$EF:``/``$FS:`` structures and
``.npy`` files, Postgres keeps in typed columns and companion tables, written
in the **same statement** as the record (plan §5 M2):

``BM25Field``
    ``<table>__<f>__post (scope, term, _pk, tf)`` -- one posting per distinct
    term per record, primary key ``(scope, term, _pk)`` (#758 D4) -- and
    ``<table>__<f>__dl (_pk, scope, len)``, one row per record with at least
    one token (Redis's ``dl`` set: a record with no tokens is not in ``N``).
    Both reference the record ``ON DELETE CASCADE``. Tokens come from
    :func:`popoto.fields._tokenizer.tokenize` -- the same Python tokenizer as
    Redis, never Postgres text search -- and the score is BM25_SEARCH_LUA's
    formula, term for term (see :func:`bm25_ctes`). Statistics are live: no
    stats row to contend on (#758 spike-4).
``EmbeddingField``
    ``<f> bigint`` (the dimension count, the value Redis stores in the hash),
    plus ``<f>__vec vector(d)``, ``<f>__model text`` and ``<f>__hash text``
    (the md5 of the text the vector was made from), and an HNSW index
    (``vector_cosine_ops``). The vector arm scans exactly at or below
    ``Defaults.PG_VECTOR_EXACT_MAX`` rows-with-a-vector in scope, ordering by
    ``(v <=> q) + 0`` (an expression no index serves), and uses HNSW with
    ``hnsw.iterative_scan = relaxed_order`` above it; when HNSW returns fewer
    rows than ``min(limit, rows with a vector)`` the arm is re-run exactly
    (the recall guard, #758 D5).
``ExistenceFilter`` / ``FrequencySketch``
    ``<table>__<f>__tok (token, _pk)`` and ``<table>__<f>__cnt (token,
    count)``: **exact**. No false positives and no over-counting where
    Redis's bloom and count-min sketch allow both (a documented strictness,
    plan §1.1); the token table forgets a record on delete (cascade), the
    count table never decrements (as the sketch never does).

The **scope** of a model is the ``partition_by`` of its ``DecayingSortedField``
(else of its first partitioned ``SortedField``); with none it is ``''``. It
leads the postings key, filters the vector arm and is what ``recall(scope=)``
names (plan §3 Scoping). A save that changes a scope column moves the
record's postings in the same statement.

Never imports ``redis``.
"""

from __future__ import annotations

import collections
import hashlib
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from ...fields.constants import Defaults
from ..types import (
    And,
    BackendCapabilityError,
    Cond,
    ModelSpec,
    Op,
    Or,
    Predicate,
    RecordId,
    Scored,
    UnitOfWork,
)
from .schema import RELATIONSHIP_KINDS, Column, TableSpec, _bounded, quote_ident
from .ttl import expired_pks_sql, live_sql

__all__ = [
    "SCOPE_SEPARATOR",
    "BM25Layout",
    "EmbeddingLayout",
    "MembershipLayout",
    "SearchLayout",
    "SearchMixin",
    "SavePlan",
    "compile_search",
    "prepare_save",
    "record_lock_keys",
    "record_lock_sql",
    "require_extensions",
    "scope_text",
]

logger = logging.getLogger("POPOTO.postgres.search")

SCOPE_SEPARATOR = "\x1f"
"""Joins the values of a multi-column scope into one ``scope`` text. NUL is
refused by ``text``; the unit separator cannot appear in a key string."""

VEC_SUFFIX = "__vec"
MODEL_SUFFIX = "__model"
HASH_SUFFIX = "__hash"

#: pgvector's HNSW index takes at most 2,000 dimensions for ``vector``.
HNSW_MAX_DIMENSIONS = 2000

#: The widest vector the narrow table stores ``STORAGE PLAIN``: 4 bytes a
#: dimension plus an 8-byte header must fit an 8 kB heap page beside the
#: ``_pk`` and ``scope`` text (``MaxHeapTupleSize`` is 8,160 bytes). Wider
#: vectors keep the default ``EXTENDED`` storage (TOAST) there too.
PLAIN_MAX_DIMENSIONS = 1900

#: The Lua script returns ``tostring(score)``: Lua's ``%.14g``. The parity
#: path (``BM25Field.search``) hands back the same representation.
LUA_NUMBER_FORMAT = "%.14g"

_SCOPE_KINDS = ("DecayingSortedField", "SortedField", "SortedKeyField")
_FILTER_KINDS = frozenset(
    {
        "KeyField",
        "UniqueKeyField",
        "AutoKeyField",
        "SortedKeyField",
        "IndexedField",
        "UniqueField",
        "Relationship",
    }
)


# -- layout -------------------------------------------------------------------


@dataclass(frozen=True)
class BM25Layout:
    field: str
    source: str
    post: str
    """Qualified postings table."""
    dl: str
    """Qualified document-length table."""
    field_ref: Any


@dataclass(frozen=True)
class EmbeddingLayout:
    field: str
    source: Optional[str]
    vec: str
    model: str
    hash: str
    dims: Optional[int]
    """``None`` when no provider was known at first use: the column is then
    an unconstrained ``vector`` with no HNSW index, and every search is
    exact."""
    field_ref: Any
    narrow: str = ""
    """Qualified narrow vector table ``(_pk, scope, v)``: the exact path's
    copy of the vector, kept un-TOASTed (``STORAGE PLAIN``) so a scope's
    vectors are read straight from the heap (see :func:`compile_search`)."""

    @property
    def hnsw(self) -> bool:
        return self.dims is not None and self.dims <= HNSW_MAX_DIMENSIONS


@dataclass(frozen=True)
class MembershipLayout:
    field: str
    kind: str
    table: str
    field_ref: Any


@dataclass(frozen=True)
class SearchLayout:
    """What :func:`compile_search` adds to a model's table."""

    table: str
    """The qualified record table."""
    columns: tuple[Column, ...]
    indexes: tuple[tuple[str, str, bool], ...]
    companions: tuple[tuple[str, tuple[str, ...]], ...]
    scope_columns: tuple[str, ...]
    bm25: Mapping[str, BM25Layout]
    embedding: Mapping[str, EmbeddingLayout]
    membership: Mapping[str, MembershipLayout]


def scope_columns(spec: ModelSpec) -> tuple[str, ...]:
    """The model's scope: the ``partition_by`` of its ``DecayingSortedField``
    (Valor's ``project_key``), else of its first partitioned sorted field,
    else ``()`` -- scope ``''`` (plan §3 Scoping)."""
    for kind in _SCOPE_KINDS:
        for name in sorted(spec.fields):
            fs = spec.fields[name]
            if fs.kind == kind and fs.options.get("partition_by"):
                return tuple(fs.options["partition_by"])
    return ()


def scope_text(values: Sequence[Any]) -> str:
    """The ``scope`` text for the scope columns' values: ``''`` for none or
    ``None`` (so ``NULL`` is never dropped), ``str(v)`` otherwise."""
    return SCOPE_SEPARATOR.join("" if v is None else str(v) for v in values)


def _provider_dims(field_ref: Any) -> Optional[int]:
    try:
        provider = field_ref.provider
        dims = provider.dimensions if provider is not None else None
        return int(dims) if dims else None
    except Exception:  # pragma: no cover - a provider that cannot say
        return None


def compile_search(
    spec: ModelSpec, schema: str, table: str, index_name: Callable[..., str]
) -> Optional[SearchLayout]:
    """The auxiliary columns, indexes and companion tables of ``spec``'s
    search fields, or ``None`` when it has none. Pure (reads the live field
    for a provider's dimensions, never the network)."""
    main = f"{quote_ident(schema)}.{quote_ident(table)}"
    columns: list[Column] = []
    indexes: list[tuple[str, str, bool]] = []
    companions: list[tuple[str, tuple[str, ...]]] = []
    bm25: dict[str, BM25Layout] = {}
    embedding: dict[str, EmbeddingLayout] = {}
    membership: dict[str, MembershipLayout] = {}
    ref_pk = f'REFERENCES {main} ("_pk") ON DELETE CASCADE'

    def companion(suffix: str) -> tuple[str, str]:
        name = _bounded(f"{table}__{suffix}")
        return name, f"{quote_ident(schema)}.{quote_ident(name)}"

    for name in sorted(spec.fields):
        fs = spec.fields[name]
        ref = fs.options.get("field_ref")
        if fs.kind == "BM25Field":
            post_name, post = companion(f"{name}__post")
            dl_name, dl = companion(f"{name}__dl")
            companions.append(
                (
                    post_name,
                    (
                        f"CREATE TABLE IF NOT EXISTS {post} ("
                        '"scope" text NOT NULL, "term" text NOT NULL, '
                        f'"_pk" text NOT NULL {ref_pk}, "tf" integer NOT NULL, '
                        'PRIMARY KEY ("scope", "term", "_pk"))',
                        f"CREATE INDEX IF NOT EXISTS "
                        f'{quote_ident(_bounded(post_name + "__pk__idx"))} '
                        f'ON {post} ("_pk")',
                    ),
                )
            )
            companions.append(
                (
                    dl_name,
                    (
                        f"CREATE TABLE IF NOT EXISTS {dl} ("
                        f'"_pk" text PRIMARY KEY {ref_pk}, '
                        '"scope" text NOT NULL, "len" integer NOT NULL)',
                        f"CREATE INDEX IF NOT EXISTS "
                        f'{quote_ident(_bounded(dl_name + "__scope__idx"))} '
                        f'ON {dl} ("scope") INCLUDE ("len")',
                    ),
                )
            )
            bm25[name] = BM25Layout(
                field=name,
                source=str(fs.options.get("source") or ""),
                post=post,
                dl=dl,
                field_ref=ref,
            )
        elif fs.kind == "EmbeddingField":
            dims = _provider_dims(ref)
            narrow_name, narrow = companion(f"{name}__vec")
            layout = EmbeddingLayout(
                field=name,
                source=fs.options.get("source"),
                vec=name + VEC_SUFFIX,
                model=name + MODEL_SUFFIX,
                hash=name + HASH_SUFFIX,
                dims=dims,
                field_ref=ref,
                narrow=narrow,
            )
            vec_type = f"vector({dims})" if dims else "vector"
            columns.append(Column(layout.vec, vec_type, name, role="aux"))
            columns.append(Column(layout.model, "text", name, role="aux"))
            columns.append(Column(layout.hash, "text", name, role="aux"))
            if layout.hnsw:
                body = f"USING hnsw ({quote_ident(layout.vec)} vector_cosine_ops)"
                indexes.append((index_name("hnsw", name), body, False))
            # The backfill's probe runs after every embedding save, so it must
            # not scan the table: rows with no vector sit in a partial index
            # (normally empty), and "made by another model" is two range
            # scans of the model column's B-tree.
            indexes.append(
                (
                    index_name("unembedded", name),
                    f'("_pk") WHERE {quote_ident(layout.vec)} IS NULL',
                    False,
                )
            )
            indexes.append(
                (index_name("model", name), f"({quote_ident(layout.model)})", False)
            )
            # The narrow vector table (#774 review): the exact arm's copy of
            # each vector, keyed by record, with the record's scope. In the
            # record table a 1536-d vector (6 kB) is TOASTed, so the exact
            # arm read two heaps and de-TOASTed every row of the scope; here
            # it is stored inline (PLAIN) in a row of its own, and a scope's
            # vectors are one index range plus their heap pages. Written in
            # the save statement beside the record's own vector column (which
            # the HNSW index keeps serving), and cascaded on delete.
            narrow_stmts = [
                f"CREATE TABLE IF NOT EXISTS {narrow} ("
                f'"_pk" text PRIMARY KEY {ref_pk}, "scope" text NOT NULL, '
                f'"v" {vec_type} NOT NULL)',
                f"CREATE INDEX IF NOT EXISTS "
                f'{quote_ident(_bounded(narrow_name + "__scope__idx"))} '
                f'ON {narrow} ("scope")',
            ]
            if dims is not None and dims <= PLAIN_MAX_DIMENSIONS:
                narrow_stmts.append(
                    f'ALTER TABLE {narrow} ALTER COLUMN "v" SET STORAGE PLAIN'
                )
            companions.append((narrow_name, tuple(narrow_stmts)))
            embedding[name] = layout
        elif fs.kind == "ExistenceFilter":
            tok_name, tok = companion(f"{name}__tok")
            companions.append(
                (
                    tok_name,
                    (
                        f"CREATE TABLE IF NOT EXISTS {tok} ("
                        f'"token" text NOT NULL, "_pk" text NOT NULL {ref_pk}, '
                        'PRIMARY KEY ("token", "_pk"))',
                        f"CREATE INDEX IF NOT EXISTS "
                        f'{quote_ident(_bounded(tok_name + "__pk__idx"))} '
                        f'ON {tok} ("_pk")',
                    ),
                )
            )
            membership[name] = MembershipLayout(name, fs.kind, tok, ref)
        elif fs.kind == "FrequencySketch":
            cnt_name, cnt = companion(f"{name}__cnt")
            companions.append(
                (
                    cnt_name,
                    (
                        f"CREATE TABLE IF NOT EXISTS {cnt} ("
                        '"token" text PRIMARY KEY, "count" bigint NOT NULL)',
                    ),
                )
            )
            membership[name] = MembershipLayout(name, fs.kind, cnt, ref)
    if not (bm25 or embedding or membership):
        return None
    return SearchLayout(
        table=main,
        columns=tuple(columns),
        indexes=tuple(indexes),
        companions=tuple(companions),
        scope_columns=scope_columns(spec),
        bm25=bm25,
        embedding=embedding,
        membership=membership,
    )


VECTOR_EXTENSION_SQL = (
    "SELECT n.nspname, n.nspname = ANY(current_schemas(false)) "
    "FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
    "WHERE e.extname = 'vector'"
)
"""Where pgvector is installed, and whether that schema is on the
``search_path``; no row when it is not installed."""


def require_extensions(conn: Any, ts: TableSpec) -> None:
    """Raise :class:`BackendCapabilityError` when ``ts`` needs pgvector and
    the database does not have it (plan §3, "Extensions and capabilities").

    popoto never creates or drops an extension: that is the operator's
    (``CREATE EXTENSION vector``, as a role allowed to). The extension's
    schema must be on the connection's ``search_path`` (the default,
    ``public``, is), because the column type and ``<=>`` are named
    unqualified."""
    layout = ts.search
    if layout is None or not layout.embedding:
        return
    row = conn.execute(VECTOR_EXTENSION_SQL).fetchone()
    fields = ", ".join(sorted(layout.embedding))
    if row is None:
        raise BackendCapabilityError(
            f"{ts.model}.{fields} (EmbeddingField) needs the pgvector extension, "
            "which this database does not have. Run CREATE EXTENSION vector "
            "(as a role allowed to create extensions) in this database; popoto "
            "never creates or drops extensions."
        )
    if not row[1]:
        raise BackendCapabilityError(
            f"{ts.model}.{fields} (EmbeddingField): the pgvector extension is "
            f"installed in schema {row[0]!r}, which is not on this connection's "
            "search_path; add it (popoto names the vector type and the <=> "
            "operator unqualified)."
        )


# -- helpers ------------------------------------------------------------------


def _vector_literal(vector: Any) -> str:
    """pgvector's text form, with each value rounded to float32 first -- the
    precision Redis's ``.npy`` files store, and pgvector's own."""
    import numpy as np

    arr = np.asarray(vector, dtype=np.float32)
    return "[" + ",".join(repr(float(x)) for x in arr) + "]"


def _parse_vector(text: Any) -> list[float]:
    if text is None:
        return []
    if isinstance(text, (list, tuple)):
        return [float(x) for x in text]
    body = str(text).strip()[1:-1]
    return [float(x) for x in body.split(",")] if body else []


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _source_text(obj: Any, source: str) -> Optional[str]:
    """The source field's text, as ``on_save`` reads it on Redis (a
    ``ContentField`` reference resolved through its store)."""
    value = getattr(obj, source, None)
    if isinstance(value, str) and value.startswith("$CF:"):
        source_field = obj._meta.fields.get(source)
        store = getattr(source_field, "store", None)
        if store is not None:
            try:
                return store.load(value).decode("utf-8")
            except Exception:
                return None
    return value


def _model_identity(field_ref: Any) -> str:
    from ...fields.embedding_field import EmbeddingField

    p = EmbeddingField._provider_provenance(field_ref)
    return f"{p['provider']}:{p['model']}"


def _strip_where(where_sql: str) -> str:
    return where_sql[len(" WHERE ") :] if where_sql else ""


def _kinds(spec: ModelSpec) -> dict[str, str]:
    return {name: fs.kind for name, fs in spec.fields.items()}


def _cond_text(
    ts: TableSpec, spec: ModelSpec, where: Optional[Predicate]
) -> tuple[str, list[Any]]:
    """``(sql, params)`` for a predicate on the record table, unqualified
    columns, ``("TRUE", [])`` for none."""
    from .plan import render_where

    sql, params = render_where(ts, _kinds(spec), where)
    return (_strip_where(sql) or "TRUE"), params


# -- writes -------------------------------------------------------------------


def record_lock_sql(ts: TableSpec, pks: Sequence[str]) -> tuple[str, list[Any]]:
    """A statement taking the record-key advisory lock of each of ``pks``
    (``pg_advisory_xact_lock``, held to the end of the transaction), in
    ``_pk`` byte order, to run **before** a statement that rewrites those
    records' companion rows -- with a trailing ``"; "``.

    Why (#774 review): the data-modifying CTEs of a save share one snapshot,
    so under READ COMMITTED a second save of the same record that started
    before the first committed cannot see the rows the first one inserted,
    and its ``DELETE … NOT term = ANY(new)`` leaves them behind. Serialising
    the writers of one record on its key, in a statement of its own, gives
    the rewrite a snapshot taken after the previous writer committed (and
    covers two racing first inserts, which have no row to lock yet).

    Lock order (plan §6, TD-2), one for every writer of a record row --
    save, delete, ``increment``, a capped push, ``touch``,
    ``update_confidence``, the access-tracker writes, ``on_context_used``'s
    ``FOR UPDATE`` and the backfill (``PostgresBackend._record_locked``):
    any ``(model, field)`` lock, then these record-key locks in ``_pk`` byte
    order, then the row locks in ``_pk`` order. A record's key lock is
    therefore always its first lock, and a transaction that row-locked a
    record holds its key lock already. The key names the schema and table,
    so equal ``_pk`` strings in two schemas never contend."""
    if not pks:
        return "", []
    ordered = sorted(set(pks), key=lambda k: k.encode("utf-8", "surrogateescape"))
    keys = [f"popoto:rec:{ts.qualified}:{pk}" for pk in ordered]
    if len(keys) == 1:
        return _LOCK_ONE, keys
    return _LOCK_MANY, [keys]


_LOCK_ONE = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0)); "
_LOCK_MANY = (
    "SELECT count(pg_advisory_xact_lock(hashtextextended(u.k, 0))) "
    "FROM unnest(%s::text[]) WITH ORDINALITY AS u(k, i); "
)


def record_lock_keys(sql: str, params: Sequence[Any]) -> list[str]:
    """The record-key lock names a statement built on :func:`record_lock_sql`
    takes first, or ``[]``. Read back from the statement rather than passed
    alongside it, so every writer -- whatever builds its statement -- is seen
    by the one check in ``PostgresBackend._run`` (#783: a lock this thread
    already holds in another open transaction)."""
    if sql.startswith(_LOCK_ONE):
        return [params[0]]
    if sql.startswith(_LOCK_MANY):
        return list(params[0])
    if sql.startswith(PREFIXED_LOCK_MANY):
        # graph_delete_lock_sql: the key prefix, then the deleted keys (the
        # partners it also locks are found by the statement, not named).
        return [params[0] + k for k in params[1]]
    return []


#: A ``count(pg_advisory_xact_lock(...))`` over ``prefix || key`` -- the head of
#: :func:`.graph.graph_delete_lock_sql`, shared so :func:`record_lock_keys`
#: recognises it.
PREFIXED_LOCK_MANY = (
    "SELECT count(pg_advisory_xact_lock(hashtextextended(%s || u.k, 0))) "
)


#: Column types whose SQL text is exactly ``str()`` of the decoded value, so a
#: save can render a scope part from the stored row itself.
_SCOPE_SQL_TEXT: dict[type, str] = {
    str: "{c}",
    int: "{c}::text",
    bool: "CASE WHEN {c} THEN 'True' ELSE 'False' END",
}


def _save_scope(
    ts: TableSpec, obj: Any, listed: Optional[set[str]], pk: str
) -> tuple[Optional[str], list[Any], str, list[Any]]:
    """The scope a save writes its postings, document length and narrow
    vector row under: ``(cte, cte_params, ref, ref_params)``. ``ref`` (with
    ``ref_params``) is what each statement names it by: a ``%s`` bound to
    the instance's scope, or a reference to ``cte``, which then defines it
    (``None`` otherwise).

    The side tables follow the row that is *stored* after the save (#774
    review). A scope column the save writes -- every column on a full save,
    the listed ones on an ``update_fields`` save -- takes the instance's
    value. One it leaves alone keeps the stored row's value, which may differ
    from the instance's: an unsaved assignment, or a concurrent writer that
    moved the record after this instance was loaded. That part is read from
    the record row in the statement's own snapshot (taken after the
    record-key lock, so after any concurrent writer of the record
    committed); a record that has no row yet falls back to the instance."""
    layout: SearchLayout = ts.search
    cols = layout.scope_columns
    instance = scope_text([getattr(obj, c, None) for c in cols])
    stored = [c for c in cols if listed is not None and c not in listed]
    if not stored:
        return None, [], "%s", [instance]
    existing = _existing_scope_sql(layout)
    parts: list[str] = []
    params: list[Any] = []
    for i, col in enumerate(cols, start=1):
        if col not in stored:
            value = getattr(obj, col, None)
            parts.append("%s::text")
            params.append("" if value is None else str(value))
            continue
        py_type = ts.field_types.get(col)
        render = _SCOPE_SQL_TEXT.get(py_type) if py_type is not None else None
        if render is not None and ts.kind(col) not in RELATIONSHIP_KINDS:
            parts.append(f"COALESCE({render.format(c='t.' + quote_ident(col))}, '')")
        else:
            # No exact SQL spelling of str(value) for this type: the side
            # rows already hold the stored scope (every write keeps them
            # there), so take this part from them.
            parts.append(f"COALESCE(split_part({existing}, %s, {i}), %s)")
            value = getattr(obj, col, None)
            params += [SCOPE_SEPARATOR, "" if value is None else str(value)]
    joined = parts[0] if len(parts) == 1 else f"concat_ws(%s, {', '.join(parts)})"
    if len(parts) > 1:
        params.insert(0, SCOPE_SEPARATOR)
    cte = (
        f'"_scope" AS (SELECT COALESCE((SELECT {joined} FROM {ts.qualified} t '
        'WHERE t."_pk" = %s), %s) AS "s")'
    )
    return cte, params + [pk, instance], '(SELECT "s" FROM "_scope")', []


def _existing_scope_sql(layout: SearchLayout) -> str:
    """The ``scope`` the record's side rows hold now (``t`` is the record
    row), or ``NULL`` when it has none."""
    subs: list[str] = []
    for bm in layout.bm25.values():
        subs.append(f'(SELECT "scope" FROM {bm.dl} WHERE "_pk" = t."_pk")')
        subs.append(f'(SELECT "scope" FROM {bm.post} WHERE "_pk" = t."_pk" LIMIT 1)')
    for emb in layout.embedding.values():
        subs.append(f'(SELECT "scope" FROM {emb.narrow} WHERE "_pk" = t."_pk")')
    if not subs:
        return "NULL::text"
    return subs[0] if len(subs) == 1 else f"COALESCE({', '.join(subs)})"


@dataclass
class SavePlan:
    """What a save adds to its statement: data-modifying CTEs (run in the
    same statement as the record's upsert, so one round trip and one
    transaction) and what to run after it commits."""

    ctes: list[str]
    params: list[Any]
    after: Optional[Callable[[], None]] = None


def prepare_save(
    backend: Any,
    ts: TableSpec,
    obj: Any,
    fields: Optional[Sequence[str]],
    values: dict[str, Any],
    pk: str,
) -> SavePlan:
    """Compute what ``obj``'s search fields write, before the statement runs.

    Embeds the source text (the provider call happens here, outside any
    transaction; a provider failure raises ``RuntimeError`` as ``on_save``
    does on Redis and nothing is written), adds the vector columns to
    ``values``, and returns the CTEs that rewrite the postings, document
    length and membership rows.

    As on Redis, a field's work runs on a full save, or on an
    ``update_fields`` save that names it -- with one deliberate addition for
    BM25 and embeddings: naming the *source* re-indexes and re-embeds too,
    and naming only a *scope* column moves the postings and the narrow
    vector row to the new scope (Redis's hooks run only for the listed
    field, so its index and vector go stale there).

    The scope those rows are written under is the one the record row holds
    *after* the save (:func:`_save_scope`): a scope column the save does not
    write is read from the stored row, never from the instance, so an
    unsaved assignment to it cannot move the postings away from the row.
    """
    layout: SearchLayout = ts.search
    listed = set(fields) if fields is not None else None
    ctes: list[str] = []
    params: list[Any] = []
    embedded: list[EmbeddingLayout] = []
    # The side rows follow the stored row: a scope column this save does not
    # write keeps the stored value, whatever the instance holds (#774 review).
    scope_cte, scope_cte_params, sc, sp = _save_scope(ts, obj, listed, pk)
    n = 0

    def tag() -> str:
        nonlocal n
        n += 1
        return f'"_s{n}"'

    moved = listed is None or any(c in listed for c in layout.scope_columns)

    for emb in layout.embedding.values():
        made = None
        if listed is None or emb.field in listed or (emb.source or "") in listed:
            made = _embed_for_save(obj, emb)
        if made is None:
            if moved:
                # The vector stays; its narrow row follows the record's scope.
                ctes.append(
                    f'{tag()} AS (UPDATE {emb.narrow} SET "scope" = {sc} '
                    f'WHERE "_pk" = %s AND "scope" <> {sc})'
                )
                params += [*sp, pk, *sp]
            continue
        vector, dims = made
        literal = _vector_literal(vector)
        values[emb.field] = dims
        values[emb.vec] = literal
        values[emb.model] = _model_identity(emb.field_ref)
        values[emb.hash] = _md5(str(_source_text(obj, emb.source or "")))
        setattr(obj, emb.field, dims)
        embedded.append(emb)
        ctes.append(
            f'{tag()} AS (INSERT INTO {emb.narrow} ("_pk", "scope", "v") '
            f'VALUES (%s, {sc}, %s::vector) ON CONFLICT ("_pk") DO UPDATE SET '
            '"scope" = EXCLUDED."scope", "v" = EXCLUDED."v")'
        )
        params += [pk, *sp, literal]

    from ...fields._tokenizer import tokenize

    for bm in layout.bm25.values():
        content = listed is None or bm.field in listed or bm.source in listed
        if content:
            text = _source_text(obj, bm.source)
            tokens = tokenize(str(text if text is not None else ""), unique=False)
            counts = collections.Counter(tokens)
            terms = list(counts)
            ctes.append(
                f'{tag()} AS (DELETE FROM {bm.post} WHERE "_pk" = %s AND '
                f'("scope" <> {sc} OR NOT ("term" = ANY(%s::text[]))))'
            )
            params += [pk, *sp, terms]
            if terms:
                ctes.append(
                    f'{tag()} AS (INSERT INTO {bm.post} ("scope", "term", "_pk", '
                    f'"tf") SELECT {sc}, u.t, %s, u.n FROM unnest(%s::text[], '
                    "%s::int[]) AS u(t, n) ORDER BY u.t "
                    'ON CONFLICT ("scope", "term", "_pk") DO UPDATE SET '
                    '"tf" = EXCLUDED."tf")'
                )
                params += [*sp, pk, terms, [counts[t] for t in terms]]
                ctes.append(
                    f'{tag()} AS (INSERT INTO {bm.dl} ("_pk", "scope", "len") '
                    f'VALUES (%s, {sc}, %s) ON CONFLICT ("_pk") DO UPDATE SET '
                    '"scope" = EXCLUDED."scope", "len" = EXCLUDED."len")'
                )
                params += [pk, *sp, len(tokens)]
            else:
                ctes.append(f'{tag()} AS (DELETE FROM {bm.dl} WHERE "_pk" = %s)')
                params += [pk]
        elif moved:
            ctes.append(
                f'{tag()} AS (UPDATE {bm.post} SET "scope" = {sc} '
                f'WHERE "_pk" = %s AND "scope" <> {sc})'
            )
            params += [*sp, pk, *sp]
            ctes.append(
                f'{tag()} AS (UPDATE {bm.dl} SET "scope" = {sc} WHERE "_pk" = %s)'
            )
            params += [*sp, pk]

    from ...fields.existence_filter import _compute_fingerprint_impl

    for mb in layout.membership.values():
        if listed is not None and mb.field not in listed:
            continue
        fingerprint = _compute_fingerprint_impl(mb.field_ref, obj)
        tokens = tokenize(fingerprint) or [fingerprint.lower()]
        if mb.kind == "ExistenceFilter":
            ctes.append(
                f'{tag()} AS (DELETE FROM {mb.table} WHERE "_pk" = %s AND '
                'NOT ("token" = ANY(%s::text[])))'
            )
            params += [pk, tokens]
            ctes.append(
                f'{tag()} AS (INSERT INTO {mb.table} ("token", "_pk") '
                "SELECT u.t, %s FROM unnest(%s::text[]) AS u(t) ORDER BY u.t "
                "ON CONFLICT DO NOTHING)"
            )
            params += [pk, tokens]
        else:
            # Shared rows: take their locks in token order, so two saves
            # counting overlapping tokens cannot deadlock.
            ctes.append(
                f'{tag()} AS (INSERT INTO {mb.table} AS c ("token", "count") '
                "SELECT u.t, 1 FROM unnest(%s::text[]) AS u(t) ORDER BY u.t "
                'ON CONFLICT ("token") DO UPDATE SET "count" = c."count" + 1)'
            )
            params += [sorted(set(tokens))]

    if scope_cte is not None and any(sc in cte for cte in ctes):
        ctes.insert(0, scope_cte)
        params[:0] = scope_cte_params

    after = None
    if embedded:

        def after() -> None:
            for emb in embedded:
                backfill(backend, ts, emb, obj, pk)

    return SavePlan(ctes=ctes, params=params, after=after)


def _embed_for_save(obj: Any, emb: EmbeddingLayout) -> Optional[tuple[Any, int]]:
    """``EmbeddingField.on_save``'s decisions, minus the ``.npy`` file:
    ``(vector, dims)``, or ``None`` when it would skip."""
    field_ref = emb.field_ref
    if field_ref is None or not field_ref.auto_embed:
        return None
    provider = field_ref.provider
    if provider is None or not emb.source:
        return None
    text = _source_text(obj, emb.source)
    if text is None or text == "":
        return None
    try:
        from . import _blocking

        # Off the event loop inside the async bridge (#759 M5): a provider
        # is a sync API, often a network call.
        vectors = _blocking(provider.embed, [text], input_type="document")
        if not vectors or not vectors[0]:
            return None
        vector = vectors[0]
    except Exception as e:
        raise RuntimeError(f"Embedding provider failed for {emb.field}: {e}") from e
    if len(vector) != provider.dimensions:
        raise ValueError(
            f"Provider returned {len(vector)} dimensions, "
            f"expected {provider.dimensions}"
        )
    return vector, len(vector)


# -- embedding backfill (#758 D7) ---------------------------------------------

_backfill_busy = threading.Lock()


def _scope_predicate(
    ts: TableSpec, columns: Sequence[str], values: Sequence[Any]
) -> Optional[Predicate]:
    """The record-table predicate for one scope: ``col = v``, or ``NULL``
    (and ``''`` for a text column) for an empty value -- the rows whose
    ``scope`` text is ``''``."""
    parts: list[Predicate] = []
    for col, value in zip(columns, values):
        if value is None or value == "":
            empty: Predicate = Cond(col, Op.ISNULL, True)
            if ts.field_types.get(col) is str:
                empty = Or((empty, Cond(col, Op.EXACT, "")))
            parts.append(empty)
        else:
            parts.append(Cond(col, Op.EXACT, value))
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else And(tuple(parts))


def backfill(
    backend: Any, ts: TableSpec, emb: EmbeddingLayout, obj: Any, pk: str
) -> int:
    """Embed up to ``Defaults.PG_BACKFILL_BATCH`` rows of ``obj``'s scope
    whose vector is missing or was made by another model, within
    ``Defaults.PG_BACKFILL_BUDGET_SECONDS`` of wall clock. Runs after the
    save committed, outside its transaction, and swallows its own errors: a
    failing or slow provider costs at most the budget, never the save.

    Each row is written only while its source still hashes to the text that
    was embedded, so a concurrent edit is never overwritten with a stale
    vector. Returns how many rows it wrote."""
    budget = float(Defaults.PG_BACKFILL_BUDGET_SECONDS)
    batch = int(Defaults.PG_BACKFILL_BATCH)
    source = emb.source
    if batch <= 0 or budget <= 0 or not source or source not in ts.field_types:
        return 0
    if not _backfill_busy.acquire(blocking=False):
        return 0  # a previous backfill's provider call is still out
    released = False
    try:
        deadline = time.monotonic() + budget
        provider = emb.field_ref.provider
        if provider is None or not emb.field_ref.auto_embed:
            return 0
        model = _model_identity(emb.field_ref)
        spec = obj._meta.spec
        layout: SearchLayout = ts.search
        scope = _scope_predicate(
            ts,
            layout.scope_columns,
            [getattr(obj, c, None) for c in layout.scope_columns],
        )
        cond, cond_params = _cond_text(ts, spec, scope)
        src = quote_ident(source)
        mcol = quote_ident(emb.model)
        # "IS DISTINCT FROM" spelled as what the two indexes serve: the
        # partial index (no vector), IS NULL and two range scans (the model).
        rows, _ = backend._run(
            f'SELECT "_pk", {src}::text FROM {ts.qualified} WHERE '
            f"({quote_ident(emb.vec)} IS NULL OR {mcol} IS NULL OR {mcol} < %s "
            f"OR {mcol} > %s) "
            f"AND {src} IS NOT NULL AND {src}::text <> '' AND \"_pk\" <> %s "
            f'AND {cond} ORDER BY "_pk" COLLATE "C" LIMIT %s',
            [model, model, pk] + cond_params + [batch],
        )
        if not rows:
            return 0
        texts = [text for _pk, text in rows]
        box: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["vectors"] = provider.embed(texts, input_type="document")
            except BaseException as exc:  # noqa: BLE001 - reported below
                box["error"] = exc
            finally:
                done.set()
                _backfill_busy.release()

        worker = threading.Thread(target=work, name="popoto-backfill", daemon=True)
        worker.start()
        released = True  # the worker releases the lock when its call returns
        from . import _blocking

        if not _blocking(done.wait, max(0.0, deadline - time.monotonic())):
            logger.info(
                "popoto backfill: provider did not answer within %.2fs; "
                "abandoned (the save already committed)",
                budget,
            )
            return 0
        if "error" in box:
            logger.warning("popoto backfill: provider failed: %s", box["error"])
            return 0
        vectors = box.get("vectors") or []
        good = [
            (row_pk, _vector_literal(v), _md5(text), len(v))
            for (row_pk, text), v in zip(rows, vectors)
            if v and (emb.dims is None or len(v) == emb.dims)
        ]
        if not good:
            return 0
        # Locked like a save (record_lock_sql), so a concurrent save of one of
        # these rows cannot interleave with this rewrite of its narrow vector
        # row; a row whose source changed (hash) or that left the scope since
        # the probe is skipped. Clears _migrated_from like every native write
        # (#756's import contract), bumps _updated_at, and takes the field off
        # _estimated_fields: the vector is now derived natively, not imported.
        lock_sql, lock_params = record_lock_sql(ts, [g[0] for g in good])
        scope_key = scope_text([getattr(obj, c, None) for c in layout.scope_columns])
        _, written = backend._run(
            lock_sql + f'WITH "_bf" AS (UPDATE {ts.qualified} SET '
            f"{quote_ident(emb.vec)} = u._bf_v::vector, "
            f"{quote_ident(emb.model)} = %s, {quote_ident(emb.hash)} = u._bf_h, "
            f"{quote_ident(emb.field)} = u._bf_d, "
            '"_updated_at" = now(), "_migrated_from" = NULL, '
            '"_estimated_fields" = array_remove("_estimated_fields", %s) '
            "FROM unnest(%s::text[], %s::text[], %s::text[], %s::int[]) "
            "AS u(_bf_pk, _bf_v, _bf_h, _bf_d) "
            f'WHERE {ts.qualified}."_pk" = u._bf_pk AND md5({src}::text) = u._bf_h '
            f"AND ({cond}) RETURNING u._bf_pk, u._bf_v) "
            f'INSERT INTO {emb.narrow} ("_pk", "scope", "v") '
            'SELECT b._bf_pk, %s, b._bf_v::vector FROM "_bf" b '
            'ON CONFLICT ("_pk") DO UPDATE SET "scope" = EXCLUDED."scope", '
            '"v" = EXCLUDED."v"',
            lock_params
            + [
                model,
                emb.field,
                [g[0] for g in good],
                [g[1] for g in good],
                [g[2] for g in good],
                [g[3] for g in good],
            ]
            + cond_params
            + [scope_key],
            write=True,
        )
        return int(written or 0)
    except Exception as exc:  # never fail the save that triggered it
        logger.warning("popoto backfill skipped: %s: %s", type(exc).__name__, exc)
        return 0
    finally:
        if not released:
            _backfill_busy.release()


def _embedded_count(
    ts: TableSpec,
    emb: EmbeddingLayout,
    cond: str,
    cond_params: list[Any],
    scope_key: Optional[str] = None,
) -> tuple[str, list[Any]]:
    """How many rows matching ``cond`` hold a vector, as an expression the
    indexes serve.

    ``scope_key`` (``cond`` is exactly that scope, nothing more): the narrow
    table's rows in the scope, an index-only count on its ``scope`` index.
    ``cond`` ``TRUE``: the narrow table's rows. Otherwise every row matching
    ``cond`` (an index-only scan on the scope's sorted index) minus those in
    the no-vector partial index -- the plain ``count(*) ... WHERE v IS NOT
    NULL`` reads the heap of every row in scope, which at 20k rows was most
    of the 5% scope's budget."""
    if scope_key is not None:
        return f'(SELECT count(*) FROM {emb.narrow} WHERE "scope" = %s)', [scope_key]
    if cond == "TRUE" and not cond_params:
        return f"(SELECT count(*) FROM {emb.narrow})", []
    vec = quote_ident(emb.vec)
    sql = (
        f"((SELECT count(*) FROM {ts.qualified} WHERE ({cond})) - "
        f"(SELECT count(*) FROM {ts.qualified} WHERE {vec} IS NULL AND ({cond})))"
    )
    return sql, list(cond_params) + list(cond_params)


def _scope_key_of(ts: TableSpec, where: Optional[Predicate]) -> Optional[str]:
    """The ``scope`` text when ``where`` is exactly one scope -- an ``EXACT``
    on every scope column and nothing else -- so the vector arm can filter
    the narrow table on its own ``scope`` column instead of ``_pk IN`` the
    record table (#774 review: ``vector_search(where=scope)`` at a 5% scope
    measured p50 9.6 ms that way). ``None`` for anything else, which keeps
    the record-table filter. Only non-empty ``str`` values on ``str``
    columns qualify: ``scope_text`` is ``str(value)``, and a value of
    another type need not spell the stored row's scope (``1`` vs ``1.0``)."""
    layout: Optional[SearchLayout] = ts.search
    if where is None or layout is None or not layout.scope_columns or ts.ttl:
        # A Meta.ttl model's read filter is on the record table (M5), which
        # the narrow-table shortcut would bypass.
        return None
    items = where.items if isinstance(where, And) else (where,)
    values: dict[str, str] = {}
    for item in items:
        if not (
            isinstance(item, Cond)
            and item.op is Op.EXACT
            and item.field in layout.scope_columns
            and item.field not in values
            and ts.field_types.get(item.field) is str
            and isinstance(item.value, str)
            and item.value != ""
        ):
            return None
        values[item.field] = item.value
    if set(values) != set(layout.scope_columns):
        return None
    return scope_text([values[c] for c in layout.scope_columns])


def _exact_sql(
    ts: TableSpec,
    emb: EmbeddingLayout,
    cond: str,
    cond_params: list[Any],
    scope_key: Optional[str] = None,
) -> tuple[str, list[Any]]:
    """The exact vector arm over the narrow table: ``SELECT "_pk", "d"``
    best first (distance, then ``_pk`` bytewise). The ``OFFSET 0`` fence
    keeps the distance computed once per row, in the scan, and leaves no
    path an index could serve (the HNSW index is on the record table
    anyway). Returns the SQL and the filter's parameters; the caller binds
    ``[q] + filter params + [limit]``.

    ``scope_key`` filters on the narrow row's own ``scope`` (``cond`` is
    exactly that scope); any other ``cond`` keeps the records matching it in
    the record table."""
    if scope_key is not None:
        filt, params = ' WHERE n."scope" = %s', [scope_key]
    elif cond == "TRUE" and not cond_params:
        filt, params = "", []
    else:
        filt = f' WHERE n."_pk" IN (SELECT "_pk" FROM {ts.qualified} WHERE {cond})'
        params = list(cond_params)
    sql = (
        f'SELECT x."_pk", x."d" FROM (SELECT n."_pk", (n."v" <=> %s::vector) '
        f'AS "d" FROM {emb.narrow} n{filt} OFFSET 0) AS x '
        'ORDER BY x."d", x."_pk" COLLATE "C" LIMIT %s'
    )
    return sql, params


# -- BM25 SQL ------------------------------------------------------------------


def bm25_ctes(
    bm: BM25Layout,
    *,
    stats_scope: Optional[str],
    candidate_scope: Optional[str],
    k1: float,
    b: float,
    prefix: str = "b",
    expired: str = "",
) -> tuple[list[str], list[Any], str]:
    """CTEs scoring every record that holds a query term; the last one,
    ``"<prefix>sc"(_pk, score)``, is the result. The query terms are the
    first parameter (``%s::text[]``, in query order).

    The arithmetic is BM25_SEARCH_LUA's, operation for operation, in double
    precision: ``idf = ln((N - df + 0.5) / (df + 0.5) + 1)``, ``tf_norm =
    (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))``, and a record's
    score is the sum of ``idf * tf_norm`` over the query terms **in query
    order** (``sum(… ORDER BY ord)``), the order the script adds them in --
    so two records with the same term statistics score bit-identically and
    tie, as they do on Redis.

    ``stats_scope`` ``None`` = corpus-wide ``N``/``avgdl``/``df`` (Redis's,
    and ``BM25Field.search``'s); a scope text = that scope's. A
    ``candidate_scope`` restricts which records are scored.

    ``expired`` (M5, :func:`.ttl.expired_pks_sql`): on a ``Meta.ttl`` model,
    the expired records' keys, left out of the statistics and the candidates
    alike -- an expired record is no document, reaped or not.
    """
    q, st, df, sc = (f'"{prefix}{x}"' for x in ("q", "st", "df", "sc"))
    params: list[Any] = []
    ctes = [f'{q}("term", "ord") AS (SELECT * FROM unnest(%s::text[]) WITH ORDINALITY)']
    gone = f'"_pk" NOT IN {expired}' if expired else ""
    stats_parts = (['"scope" = %s'] if stats_scope is not None else []) + (
        [gone] if gone else []
    )
    stats_where = f" WHERE {' AND '.join(stats_parts)}" if stats_parts else ""
    ctes.append(
        f'{st} AS (SELECT count(*)::float8 AS "n", '
        f'coalesce(avg("len"), 0)::float8 AS "avgdl" FROM {bm.dl}{stats_where})'
    )
    if stats_scope is not None:
        params.append(stats_scope)
    df_parts = (['p."scope" = %s'] if stats_scope is not None else []) + (
        ["p." + gone] if gone else []
    )
    df_where = f" WHERE {' AND '.join(df_parts)}" if df_parts else ""
    ctes.append(
        f'{df} AS (SELECT p."term", count(*)::float8 AS "df" FROM {bm.post} p '
        f'JOIN {q} ON {q}."term" = p."term"{df_where} GROUP BY p."term")'
    )
    if stats_scope is not None:
        params.append(stats_scope)
    cand_parts = (['p."scope" = %s'] if candidate_scope is not None else []) + (
        ["p." + gone] if gone else []
    )
    cand_where = f" WHERE {' AND '.join(cand_parts)}" if cand_parts else ""
    ctes.append(
        f'{sc} AS (SELECT p."_pk", sum('
        f'ln(({st}."n" - {df}."df" + 0.5) / ({df}."df" + 0.5) + 1) * '
        '((p."tf"::float8 * (%s::float8 + 1)) / (p."tf"::float8 + %s::float8 * '
        f'(1 - %s::float8 + %s::float8 * d."len"::float8 / {st}."avgdl"))) '
        f'ORDER BY {q}."ord") AS "score" '
        f'FROM {bm.post} p JOIN {q} ON {q}."term" = p."term" '
        f'JOIN {df} ON {df}."term" = p."term" '
        f'JOIN {bm.dl} d ON d."_pk" = p."_pk" CROSS JOIN {st}{cand_where} '
        'GROUP BY p."_pk")'
    )
    params += [k1, k1, b, b]
    if candidate_scope is not None:
        params.append(candidate_scope)
    return ctes, params, sc


def _lua_number(value: float) -> float:
    return float(LUA_NUMBER_FORMAT % value)


# -- the backend's search methods ----------------------------------------------


class SearchMixin:
    """Protocol groups E (``vector_search``, ``keyword_search``) and G
    (``membership_*``) for :class:`~popoto.backends.postgres.PostgresBackend`,
    plus the search helpers the field classes route to on Postgres and the
    ``[PG-only]`` :meth:`recall`."""

    # Provided by PostgresBackend.
    _table: Any
    _run: Any
    _record_locked: Any
    _decode: Any
    _select_cols: Any

    # -- layout access --------------------------------------------------------

    def _layout(self, spec: ModelSpec) -> tuple[TableSpec, SearchLayout]:
        ts = self._table(spec)
        if ts.search is None:
            raise BackendCapabilityError(
                f"{spec.name} has no BM25Field, EmbeddingField, ExistenceFilter "
                "or FrequencySketch"
            )
        return ts, ts.search

    def _bm25(self, spec: ModelSpec, field: str) -> tuple[TableSpec, BM25Layout]:
        ts, layout = self._layout(spec)
        if field not in layout.bm25:
            raise BackendCapabilityError(f"{spec.name}.{field} is not a BM25Field")
        return ts, layout.bm25[field]

    def _embedding(
        self, spec: ModelSpec, field: str
    ) -> tuple[TableSpec, EmbeddingLayout]:
        ts, layout = self._layout(spec)
        if field not in layout.embedding:
            raise BackendCapabilityError(
                f"{spec.name}.{field} is not an EmbeddingField"
            )
        return ts, layout.embedding[field]

    def _membership(
        self, spec: ModelSpec, field: str
    ) -> tuple[TableSpec, MembershipLayout]:
        ts, layout = self._layout(spec)
        if field not in layout.membership:
            raise BackendCapabilityError(
                f"{spec.name}.{field} is not an ExistenceFilter or FrequencySketch"
            )
        return ts, layout.membership[field]

    # -- E. keyword_search ---------------------------------------------------------

    def keyword_search(
        self,
        spec: ModelSpec,
        field: str,
        tokens: Sequence[str],
        *,
        limit: int,
        where: Optional[Predicate] = None,
        allowed: Optional[Any] = None,
        stats: str = "corpus",
        scope: Optional[str] = None,
        fetch_cap: Optional[int] = None,
    ) -> Scored:
        """BM25 over the postings, best first, ties by ``_pk`` bytewise.

        ``stats="corpus"`` (the default, ``BM25Field.search``'s) uses
        corpus-wide ``N``/``avgdl``/``df`` as Redis does; ``"scope"`` uses
        ``scope``'s. ``scope`` restricts the scored records to one scope;
        ``where`` to the records matching a predicate. ``allowed`` (record
        ids or key strings) keeps only those records, from the best
        ``max(limit, fetch_cap)`` overall when ``fetch_cap`` is given -- the
        window ``BM25Field.search(allowed_keys=)``'s widening loop reaches on
        Redis before it stops (``SCOPED_SEARCH_FETCH_CAP``).
        """
        ts, bm = self._bm25(spec, field)
        terms = list(dict.fromkeys(tokens))
        if not terms or limit is None or limit <= 0:
            return []
        if allowed is not None:
            allowed_keys = [getattr(a, "canonical", a) for a in allowed]
            if not allowed_keys:
                return []
        k1, b = self._bm25_params(bm)
        stats_scope = scope if stats == "scope" else None
        ctes, params, sc = bm25_ctes(
            bm,
            stats_scope=stats_scope,
            candidate_scope=scope,
            k1=k1,
            b=b,
            expired=expired_pks_sql(ts),
        )
        params = [terms] + params
        filt = ""
        if where is not None:
            cond, cond_params = _cond_text(ts, spec, where)
            filt = f' WHERE s."_pk" IN (SELECT "_pk" FROM {ts.qualified} WHERE {cond})'
            params += cond_params
        ctes.append(
            f'"ranked" AS (SELECT s."_pk", s."score", row_number() OVER '
            f'(ORDER BY s."score" DESC, s."_pk" COLLATE "C") AS "rn" '
            f"FROM {sc} s{filt})"
        )
        sql = "WITH " + ", ".join(ctes) + ' SELECT "_pk", "score" FROM "ranked"'
        if allowed is not None:
            sql += ' WHERE "_pk" = ANY(%s::text[])'
            params.append(allowed_keys)
            if fetch_cap is not None:
                sql += ' AND "rn" <= %s'
                params.append(max(int(limit), int(fetch_cap)))
        sql += ' ORDER BY "rn" LIMIT %s'
        params.append(int(limit))
        rows, _ = self._run(sql, params)
        return [(RecordId(spec.name, (), pk), float(score)) for pk, score in rows]

    @staticmethod
    def _bm25_params(bm: BM25Layout) -> tuple[float, float]:
        ref = bm.field_ref
        return float(getattr(ref, "BM25_K1", 1.2)), float(getattr(ref, "BM25_B", 0.75))

    def bm25_search(
        self,
        spec: ModelSpec,
        field: str,
        tokens: Sequence[str],
        *,
        limit: int,
        allowed: Optional[Any] = None,
    ) -> list[tuple[str, float]]:
        """``BM25Field.search`` on Postgres: ``[(key, score)]`` with Redis's
        corpus statistics, order and score representation (``%.14g``)."""
        from ...fields.bm25_field import SCOPED_SEARCH_FETCH_CAP

        scored = self.keyword_search(
            spec,
            field,
            tokens,
            limit=limit,
            allowed=allowed,
            # Named, not left to the default: the hybrid path's parity rests
            # on corpus statistics (architect decision 3).
            stats="corpus",
            fetch_cap=SCOPED_SEARCH_FETCH_CAP if allowed is not None else None,
        )
        return [(rid.canonical, _lua_number(score)) for rid, score in scored]

    def bm25_corpus(self, spec: ModelSpec, field: str) -> tuple[int, float]:
        """``(N, avgdl)`` corpus-wide: records with at least one token, and
        their mean length (``(0, 0.0)`` for none)."""
        ts, bm = self._bm25(spec, field)
        expired = expired_pks_sql(ts)
        rows, _ = self._run(
            f'SELECT count(*), coalesce(avg("len"), 0)::float8 FROM {bm.dl}'
            + (f' WHERE "_pk" NOT IN {expired}' if expired else "")
        )
        return int(rows[0][0]), float(rows[0][1])

    def bm25_idf(
        self, spec: ModelSpec, field: str, tokens: Sequence[str]
    ) -> dict[str, float]:
        """``BM25Field.get_idf`` on Postgres: corpus ``N``, ``df`` per token,
        the same formula, computed in Python as the Redis path does."""
        ts, bm = self._bm25(spec, field)
        if not tokens:
            return {}
        expired = expired_pks_sql(ts)
        n_where = f' WHERE "_pk" NOT IN {expired}' if expired else ""
        df_and = f' AND p."_pk" NOT IN {expired}' if expired else ""
        rows, _ = self._run(
            f"SELECT (SELECT count(*) FROM {bm.dl}{n_where}), u.t, "
            f'(SELECT count(*) FROM {bm.post} p WHERE p."term" = u.t{df_and}) '
            "FROM unnest(%s::text[]) WITH ORDINALITY AS u(t, i) ORDER BY u.i",
            [list(tokens)],
        )
        if not rows or int(rows[0][0]) == 0:
            return {token: 0.0 for token in tokens}
        n = int(rows[0][0])
        df = {t: float(c) for _n, t, c in rows}
        return {
            token: math.log((n - df[token] + 0.5) / (df[token] + 0.5) + 1)
            for token in tokens
        }

    # -- E. vector_search ----------------------------------------------------------

    def vector_search(
        self,
        spec: ModelSpec,
        field: str,
        query: Sequence[float],
        *,
        limit: int,
        where: Optional[Predicate] = None,
        min_score: Optional[float] = None,
    ) -> Scored:
        """Cosine similarity ``1 - (v <=> q)``, best first, positive only
        (Redis's ``score > 0`` filter), ties by ``_pk`` bytewise. Exact at or
        below ``Defaults.PG_VECTOR_EXACT_MAX`` rows with a vector among those
        ``where`` matches, HNSW above it with the recall guard."""
        ts, _layout = self._layout(spec)
        scored, _info = self._vector_arm(
            spec,
            field,
            query,
            limit=limit,
            where=where,
            scope_key=_scope_key_of(ts, where),
        )
        if min_score is not None:
            scored = [(rid, s) for rid, s in scored if s >= min_score]
        return scored

    def _vector_arm(
        self,
        spec: ModelSpec,
        field: str,
        query: Any,
        *,
        limit: int,
        where: Optional[Predicate] = None,
        force: Optional[str] = None,
        embedded: Optional[int] = None,
        scope_key: Optional[str] = None,
    ) -> tuple[Scored, dict[str, Any]]:
        """The vector arm and how it ran: ``{"path": "exact"|"hnsw",
        "guard": bool, "embedded": n}``. ``force`` pins the path (tests).
        ``scope_key``: ``where`` is exactly that scope (lets the exact path
        and the count filter the narrow table on its own column)."""
        import numpy as np

        ts, emb = self._embedding(spec, field)
        info: dict[str, Any] = {"path": None, "guard": False, "embedded": 0}
        q = np.asarray(query, dtype=np.float32)
        if limit is None or limit <= 0 or not q.size or not np.linalg.norm(q):
            return [], info
        literal = _vector_literal(q)
        cond, cond_params = _cond_text(ts, spec, where)
        vec = quote_ident(emb.vec)
        if embedded is None:
            count_sql, count_params = _embedded_count(
                ts, emb, cond, cond_params, scope_key
            )
            rows, _ = self._run(f"SELECT {count_sql}", count_params)
            embedded = int(rows[0][0])
        info["embedded"] = embedded
        if not embedded:
            return [], info
        path = force or (
            "hnsw"
            if emb.hnsw and embedded > int(Defaults.PG_VECTOR_EXACT_MAX)
            else "exact"
        )
        info["path"] = path
        base = (
            f'SELECT "_pk", ({vec} <=> %s::vector) AS "d" FROM {ts.qualified} '
            f"WHERE {vec} IS NOT NULL AND {cond}"
        )
        exact_sql, filt_params = _exact_sql(ts, emb, cond, cond_params, scope_key)
        exact_params = [literal] + filt_params + [int(limit)]
        if path == "hnsw":
            rows, _ = self._run(
                self._hnsw_prefix()
                + f'SELECT "_pk", "d" FROM ({base} ORDER BY {vec} <=> %s::vector '
                'LIMIT %s) AS h ORDER BY "d", "_pk" COLLATE "C"',
                [literal] + cond_params + [literal, int(limit)],
            )
            if len(rows) < min(int(limit), embedded):
                info["guard"] = True
                rows, _ = self._run(exact_sql, exact_params)
        else:
            rows, _ = self._run(exact_sql, exact_params)
        scored = [
            (RecordId(spec.name, (), pk), 1.0 - float(d))
            for pk, d in rows
            if d is not None and not math.isnan(d) and float(d) < 1.0
        ]
        return scored, info

    @staticmethod
    def _hnsw_prefix() -> str:
        """Settings for a statement on the HNSW path (never an exact one).

        The planner prices a filtered HNSW scan above reading the scope's
        rows and sorting them by distance once the filter keeps most of the
        table: measured on 20k 1536-d rows, a 60% scope went bitmap heap scan
        + top-N sort, 77 ms, where the index answers in a few. So the
        statement prices out sequential and bitmap scans and explicit sorts;
        what is left that can produce rows in distance order is the HNSW
        index. The settings are statement-wide, which is why the HNSW query
        always runs as a statement of its own: in a recall statement they
        would push the BM25 arm onto a full scan of the postings' ``_pk``
        index (measured: 300 ms). The guard's exact re-run is a separate
        statement too, with no settings."""
        ef = int(Defaults.PG_HNSW_EF_SEARCH)
        return (
            f"SET LOCAL hnsw.ef_search = {ef}; "
            "SET LOCAL hnsw.iterative_scan = relaxed_order; "
            "SET LOCAL enable_seqscan = off; SET LOCAL enable_bitmapscan = off; "
            "SET LOCAL enable_sort = off; "
        )

    def load_vectors(
        self, spec: ModelSpec, field: str
    ) -> tuple[list[str], list[list[float]]]:
        """Every stored vector of ``field``: ``(keys, vectors)`` in ``_pk``
        bytewise order (``EmbeddingField.load_embeddings`` on Postgres)."""
        ts, emb = self._embedding(spec, field)
        vec = quote_ident(emb.vec)
        live = live_sql(ts)
        rows, _ = self._run(
            f'SELECT "_pk", {vec}::text FROM {ts.qualified} WHERE {vec} IS NOT NULL '
            + (f"AND {live} " if live else "")
            + 'ORDER BY "_pk" COLLATE "C"'
        )
        return [r[0] for r in rows], [_parse_vector(r[1]) for r in rows]

    # -- G. membership ---------------------------------------------------------

    def membership_add(
        self,
        spec: ModelSpec,
        field: str,
        tokens: Sequence[str],
        *,
        uow: Optional[UnitOfWork] = None,
        id: Optional[RecordId] = None,
    ) -> None:
        """Record ``tokens``: for an ``ExistenceFilter`` against record ``id``
        (required: the exact table forgets a record when it is deleted), for
        a ``FrequencySketch`` by adding one to each token's count."""
        ts, mb = self._membership(spec, field)
        tokens = sorted(set(tokens))
        if not tokens:
            return
        if mb.kind == "ExistenceFilter":
            if id is None:
                raise BackendCapabilityError(
                    "membership_add on an ExistenceFilter needs the record id= "
                    "(Postgres keeps exact (token, record) rows)"
                )
            # A companion row of the record (its foreign key share-locks the
            # record row): the record's key lock first, as every writer does.
            sql, params = self._record_locked(
                ts,
                [id.canonical],
                f'INSERT INTO {mb.table} ("token", "_pk") SELECT u.t, %s FROM '
                "unnest(%s::text[]) AS u(t) ON CONFLICT DO NOTHING",
                [id.canonical, tokens],
            )
            self._run(sql, params, uow=uow, write=True)
            return
        self._run(
            f'INSERT INTO {mb.table} AS c ("token", "count") SELECT u.t, 1 FROM '
            'unnest(%s::text[]) AS u(t) ORDER BY u.t ON CONFLICT ("token") DO '
            'UPDATE SET "count" = c."count" + 1',
            [tokens],
            uow=uow,
            write=True,
        )

    def membership_query(
        self,
        spec: ModelSpec,
        field: str,
        tokens: Sequence[str],
        *,
        mode: str,
    ) -> Any:
        """``"any"``: whether any token is present (``bool``); ``"each"``:
        one ``bool`` per token; ``"count"``: one exact count per token."""
        ts, mb = self._membership(spec, field)
        tokens = list(tokens)
        if not tokens:
            return False if mode == "any" else []
        # M5: an expired record's tokens are not seen (its rows go with it
        # when the reaper deletes it, as a delete's do).
        expired = expired_pks_sql(ts) if mb.kind == "ExistenceFilter" else ""
        gone = f' AND m."_pk" NOT IN {expired}' if expired else ""
        if mode == "any":
            if mb.kind != "ExistenceFilter":
                raise BackendCapabilityError("mode='any' is for an ExistenceFilter")
            rows, _ = self._run(
                f'SELECT EXISTS (SELECT 1 FROM {mb.table} m WHERE m."token" = '
                f"ANY(%s::text[]){gone})",
                [tokens],
            )
            return bool(rows[0][0])
        if mode == "each":
            if mb.kind != "ExistenceFilter":
                raise BackendCapabilityError("mode='each' is for an ExistenceFilter")
            rows, _ = self._run(
                f'SELECT EXISTS (SELECT 1 FROM {mb.table} m WHERE m."token" = '
                f"u.t{gone}) FROM unnest(%s::text[]) WITH ORDINALITY AS u(t, i) "
                "ORDER BY u.i",
                [tokens],
            )
            return [bool(r[0]) for r in rows]
        if mode == "count":
            if mb.kind == "ExistenceFilter":
                rows, _ = self._run(
                    f'SELECT (SELECT count(*) FROM {mb.table} m WHERE m."token" '
                    f"= u.t{gone}) FROM unnest(%s::text[]) WITH ORDINALITY AS "
                    "u(t, i) ORDER BY u.i",
                    [tokens],
                )
            else:
                rows, _ = self._run(
                    f'SELECT coalesce(c."count", 0) FROM unnest(%s::text[]) '
                    f"WITH ORDINALITY AS u(t, i) LEFT JOIN {mb.table} c ON "
                    'c."token" = u.t ORDER BY u.i',
                    [tokens],
                )
            return [int(r[0]) for r in rows]
        raise ValueError(f"membership_query mode must be any/each/count, got {mode!r}")

    def membership_size(self, spec: ModelSpec, field: str) -> int:
        """Distinct tokens recorded (``ExistenceFilter.fill_ratio``)."""
        ts, mb = self._membership(spec, field)
        expired = expired_pks_sql(ts) if mb.kind == "ExistenceFilter" else ""
        rows, _ = self._run(
            f'SELECT count(DISTINCT "token") FROM {mb.table}'
            + (f' WHERE "_pk" NOT IN {expired}' if expired else "")
        )
        return int(rows[0][0])

    # -- [PG-only] recall -----------------------------------------------------

    def recall(
        self,
        spec: ModelSpec,
        query_text: str,
        *,
        scope: Any = None,
        filters: Optional[Mapping[str, Any]] = None,
        tags: Any = None,
        tags_mode: str = "any",
        weights: Optional[Mapping[str, float]] = None,
        limit: int = 10,
        bm25_stats: str = "scope",
        k: int = 60,
        extra_arms: Optional[Mapping[str, Sequence[str]]] = None,
        query_vector: Optional[Sequence[float]] = None,
        decay: Any = None,
        validity_field: Optional[str] = None,
        as_of: Optional[float] = None,
    ) -> list[tuple[dict[str, Any], float]]:
        """BM25, vector and decay arms fused by weighted RRF (``Σ w / (k +
        rank)``) in a statement that also returns the fused records' full
        rows: ``[(row, score)]``, best first, ties by ``_pk`` bytewise. An
        index-only count picks the vector path first; on the HNSW path the
        vector arm is its own statement (see ``_hnsw_prefix``). See
        :meth:`popoto.models.query.Query.recall` for the arguments.

        The decay arm is ``rank_decayed``'s ranking -- the ``DECAY_SCORE_LUA``
        expression over the M2a columns (the ``<f>`` clock, the base-score
        column, ``<f>__conf`` modulation) -- computed in the fused statement
        itself, over the same scope and filters. ``decay`` names it as
        ``(decay_field, confidence_field_or_None)`` (``Query.recall`` resolves
        both the way ``top_by_decay`` does), ``False`` leaves it out, and
        ``None`` picks the model's one ``DecayingSortedField`` with its one
        ``ConfidenceField``, if any.

        ``extra_arms`` are already-ranked key lists (``{name: [key, …]}``)
        fused beside the SQL arms with ``weights[name]``, re-filtered by the
        scope and filters in the fused statement.
        """
        from ...fields._tokenizer import tokenize

        if bm25_stats not in ("scope", "corpus"):
            raise ValueError(
                f"bm25_stats must be 'scope' or 'corpus', got {bm25_stats!r}"
            )
        ts, layout = self._layout(spec)
        weights = dict(weights or {})
        info: dict[str, Any] = {
            "vector": None,
            "bm25": False,
            "decay": False,
            "guard": False,
            "embedded": 0,
        }
        self._last_recall = info
        if limit is None or limit <= 0:
            return []
        depth = max(int(limit), int(Defaults.PG_RECALL_ARM_DEPTH))
        scope_cols = layout.scope_columns
        scope_values: Optional[list[Any]] = None
        if scope is not None:
            scope_values = self._scope_values(spec, scope_cols, scope)
        where = self._recall_where(
            ts, spec, scope_cols, scope_values, filters, tags, tags_mode
        )
        scope_key = scope_text(scope_values) if scope_values is not None else None
        # The narrow vector table filters on its own scope column only when
        # the scope is the whole predicate -- and never on a Meta.ttl model,
        # whose read filter lives on the record table (M5).
        scope_only = (
            scope_key if (not filters and tags is None and not ts.ttl) else None
        )

        ctes: list[str] = []
        params: list[Any] = []
        arms: list[tuple[str, float]] = []
        cond, cond_params = _cond_text(ts, spec, where)
        if validity_field:
            # The validity gate (M3): the exclusion rule every ranking applies,
            # at ``as_of`` (``None`` = now), ANDed onto every arm's domain. The
            # narrow-table scope shortcut cannot see the interval columns.
            from .validity import included_sql

            gate = included_sql(
                validity_field, time.time() if as_of is None else float(as_of), ""
            )
            if gate != "TRUE":
                cond = gate if cond == "TRUE" else f"({cond}) AND {gate}"
                scope_only = None
        in_scope = f'"_pk" IN (SELECT "_pk" FROM {ts.qualified} WHERE {cond})'

        tokens = tokenize(query_text or "")
        if tokens and layout.bm25:
            bm = next(iter(layout.bm25.values()))
            weight = float(weights.get("bm25", 1.0))
            if weight > 0:
                k1, b = self._bm25_params(bm)
                stats_scope = scope_key if bm25_stats == "scope" else None
                bctes, bparams, sc = bm25_ctes(
                    bm,
                    stats_scope=stats_scope,
                    candidate_scope=scope_key,
                    k1=k1,
                    b=b,
                    expired=expired_pks_sql(ts),
                )
                ctes += bctes
                params += [tokens] + bparams
                ctes.append(
                    '"a_bm25" AS (SELECT s."_pk", row_number() OVER (ORDER BY '
                    's."score" DESC, s."_pk" COLLATE "C") AS "rank" FROM '
                    f'{sc} s WHERE s.{in_scope} ORDER BY "rank" LIMIT %s)'
                )
                params += cond_params + [depth]
                arms.append(('"a_bm25"', weight))
                info["bm25"] = True

        if layout.embedding:
            emb = next(iter(layout.embedding.values()))
            weight = float(weights.get("vector", 1.0))
            vector = query_vector
            if vector is None and weight > 0:
                vector = self._embed_query(emb, query_text)
            if vector is not None and weight > 0:
                import numpy as np

                arr = np.asarray(vector, dtype=np.float32)
                if arr.size and np.linalg.norm(arr):
                    count_sql, count_params = _embedded_count(
                        ts, emb, cond, cond_params, scope_only
                    )
                    (embedded,) = self._run(f"SELECT {count_sql}", count_params)[0][0]
                    info["embedded"] = int(embedded)
                    hnsw = emb.hnsw and embedded > int(Defaults.PG_VECTOR_EXACT_MAX)
                    if embedded and hnsw:
                        # The HNSW plan needs statement-wide planner settings
                        # (_hnsw_prefix) that would push the BM25 arm onto a
                        # full index scan, so on this path the vector arm is
                        # its own statement -- with the recall guard -- and
                        # its ranking joins the fusion as a ranked list.
                        scored, vinfo = self._vector_arm(
                            spec,
                            emb.field,
                            arr,
                            limit=depth,
                            where=where,
                            force="hnsw",
                            embedded=int(embedded),
                            scope_key=scope_only,
                        )
                        info["vector"] = "hnsw"
                        info["guard"] = vinfo["guard"]
                        ranked_vector = [rid.canonical for rid, _s in scored]
                        if ranked_vector:
                            # Ranked in an earlier statement: re-apply the
                            # scope and filters here, so a record that left
                            # the scope in between is not returned (#774
                            # review); a deleted one drops out at the join.
                            arm = '"a_vec"'
                            ctes.append(
                                f'{arm} AS (SELECT u.pk AS "_pk", u.r AS "rank" '
                                "FROM unnest(%s::text[]) WITH ORDINALITY AS "
                                f'u(pk, r) WHERE u.pk IN (SELECT "_pk" FROM '
                                f"{ts.qualified} WHERE {cond}))"
                            )
                            params += [ranked_vector] + cond_params
                            arms.append((arm, weight))
                    elif embedded:
                        vctes, vparams = self._vector_ctes(
                            ts,
                            emb,
                            _vector_literal(arr),
                            cond,
                            cond_params,
                            depth,
                            scope_only,
                        )
                        ctes += vctes
                        params += vparams
                        arms.append(('"a_vec"', weight))
                        info["vector"] = "exact"

        decay_arm = self._recall_decay(spec, decay)
        if decay_arm is not None and float(weights.get("decay", 1.0)) > 0:
            dctes, dparams = self._decay_ctes(ts, spec, decay_arm, cond, depth)
            ctes += dctes
            params += cond_params + dparams
            arms.append(('"a_dec"', float(weights.get("decay", 1.0))))
            info["decay"] = True

        for name, keys in (extra_arms or {}).items():
            weight = float(weights.get(name, 1.0))
            if weight <= 0 or not keys:
                continue
            arm = f'"a_x{len(arms)}"'
            ctes.append(
                f'{arm} AS (SELECT u.pk AS "_pk", u.r AS "rank" FROM '
                "unnest(%s::text[]) WITH ORDINALITY AS u(pk, r) WHERE u.pk IN "
                f'(SELECT "_pk" FROM {ts.qualified} WHERE {cond}))'
            )
            params += [list(keys)[:depth]] + cond_params
            arms.append((arm, weight))

        if not arms:
            return []
        union = " UNION ALL ".join(
            f'SELECT "_pk", %s::float8 / (%s + "rank") AS "s" FROM {arm}'
            for arm, _w in arms
        )
        for _arm, weight in arms:
            params += [weight, int(k)]
        ctes.append(
            f'"fused" AS (SELECT "_pk", sum("s") AS "score" FROM ({union}) AS a '
            'GROUP BY "_pk" ORDER BY "score" DESC, "_pk" COLLATE "C" LIMIT %s)'
        )
        params.append(int(limit))
        cols = self._select_cols(ts)
        col_sql = ", ".join(f"t.{quote_ident(c)}" for c in cols)
        sql = (
            "WITH "
            + ", ".join(ctes)
            + f' SELECT {col_sql}, f."score" FROM "fused" f JOIN '
            f'{ts.qualified} t ON t."_pk" = f."_pk" '
            'ORDER BY f."score" DESC, t."_pk" COLLATE "C"'
        )
        rows, _ = self._run(sql, params)
        out = []
        width = len(cols)
        for row in rows:
            decoded = self._decode(ts, cols, row[:width])
            out.append((decoded, float(row[width])))
        return out

    @staticmethod
    def _recall_decay(
        spec: ModelSpec, decay: Any
    ) -> Optional[tuple[str, Optional[str]]]:
        """``(decay_field, confidence_field)`` for recall's decay arm, or
        ``None`` (see :meth:`recall`'s ``decay``)."""
        if decay is False:
            return None
        if decay is not None:
            field, confidence = decay
            return str(field), (str(confidence) if confidence else None)
        decays = sorted(
            n for n, f in spec.fields.items() if f.kind == "DecayingSortedField"
        )
        if len(decays) != 1:
            return None
        confidences = sorted(
            n for n, f in spec.fields.items() if f.kind == "ConfidenceField"
        )
        modulate = (
            len(confidences) == 1
            and bool(Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED)
            and bool(Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH)
        )
        return decays[0], (confidences[0] if modulate else None)

    @staticmethod
    def _decay_ctes(
        ts: TableSpec,
        spec: ModelSpec,
        decay: tuple[str, Optional[str]],
        cond: str,
        depth: int,
    ) -> tuple[list[str], list[Any]]:
        """The decay arm as a CTE ``"a_dec"(_pk, rank)``: ``rank_decayed``'s
        score and order (NaN last, then score descending, then ``_pk``
        bytewise) over the records matching ``cond``, ``depth`` deep. The
        caller binds ``cond``'s parameters, then the returned ones."""
        from .memory import decay_score_sql

        field, confidence = decay
        fs = spec.fields[field]
        expr = decay_score_sql(
            ts,
            spec,
            field,
            now=time.time(),
            rate=float(fs.options.get("decay_rate", Defaults.DECAY_RATE)),
            base_field=fs.options.get("base_score_field") or None,
            confidence_field=confidence,
            strength=float(Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH),
        )
        # OFFSET 0 fences the scored subquery, as in rank_decayed: the score
        # is evaluated once per row, not once per sort key.
        cte = (
            '"a_dec" AS (SELECT r."_pk", row_number() OVER (ORDER BY '
            '(r."_score" = \'NaN\'::float8), r."_score" DESC, '
            'r."_pk" COLLATE "C") AS "rank" FROM (SELECT t."_pk", '
            f'{expr} AS "_score" FROM {ts.qualified} AS t WHERE '
            f"t.{quote_ident(field)} IS NOT NULL AND ({cond}) OFFSET 0) AS r "
            'ORDER BY "rank" LIMIT %s)'
        )
        return [cte], [int(depth)]

    def _vector_ctes(
        self,
        ts: TableSpec,
        emb: EmbeddingLayout,
        literal: str,
        cond: str,
        cond_params: list[Any],
        depth: int,
        scope_key: Optional[str] = None,
    ) -> tuple[list[str], list[Any]]:
        """The exact vector arm as CTEs ending in ``"a_vec"(_pk, rank)``,
        over the narrow table (:func:`_exact_sql`): the scope's rows and a
        top-N sort, whatever indexes exist. (The HNSW path
        runs as its own statement: :meth:`recall`.)"""
        sql, params = _exact_sql(ts, emb, cond, cond_params, scope_key)
        ctes = [
            f'"vall" AS ({sql})',
            '"a_vec" AS (SELECT "_pk", row_number() OVER (ORDER BY "d", '
            '"_pk" COLLATE "C") AS "rank" FROM "vall" WHERE "d" < 1)',
        ]
        return ctes, [literal] + params + [depth]

    @staticmethod
    def _embed_query(emb: EmbeddingLayout, text: str) -> Optional[list[float]]:
        if not text or not text.strip():
            return None
        provider = emb.field_ref.provider if emb.field_ref is not None else None
        if provider is None:
            return None
        from . import _blocking

        try:
            vectors = _blocking(provider.embed, [text], input_type="query")
        except Exception as exc:
            logger.warning("recall: query embedding failed (%s); vector arm off", exc)
            return None
        if not vectors or not vectors[0]:
            return None
        return list(vectors[0])

    @staticmethod
    def _scope_values(
        spec: ModelSpec, scope_cols: tuple[str, ...], scope: Any
    ) -> list[Any]:
        if not scope_cols:
            raise ValueError(
                f"{spec.name} has no scope: declare partition_by on its "
                "DecayingSortedField (or a SortedField) to use recall(scope=)"
            )
        if isinstance(scope, Mapping):
            unknown = set(scope) - set(scope_cols)
            if unknown:
                raise ValueError(
                    f"recall(scope=) names {sorted(unknown)}; {spec.name}'s scope "
                    f"is {list(scope_cols)}"
                )
            return [scope.get(c) for c in scope_cols]
        if len(scope_cols) != 1:
            raise ValueError(
                f"{spec.name}'s scope is {list(scope_cols)}; pass scope= as a mapping"
            )
        return [scope]

    @staticmethod
    def _recall_where(
        ts: TableSpec,
        spec: ModelSpec,
        scope_cols: tuple[str, ...],
        scope_values: Optional[list[Any]],
        filters: Optional[Mapping[str, Any]],
        tags: Any,
        tags_mode: str,
    ) -> Optional[Predicate]:
        parts: list[Predicate] = []
        if scope_values is not None:
            scope_pred = _scope_predicate(ts, scope_cols, scope_values)
            if scope_pred is not None:
                parts.append(scope_pred)
        for name, value in (filters or {}).items():
            fs = spec.fields.get(name)
            if fs is None or fs.kind not in _FILTER_KINDS:
                raise ValueError(
                    f"recall(filters=) takes KeyField/IndexedField names; "
                    f"{name!r} is {fs.kind if fs else 'not a field'} on {spec.name}"
                )
            if isinstance(value, (list, tuple, set, frozenset)):
                parts.append(Cond(name, Op.IN, list(value)))
            elif value is None:
                parts.append(Cond(name, Op.ISNULL, True))
            else:
                parts.append(Cond(name, Op.EXACT, value))
        if tags is not None:
            if tags_mode not in ("any", "all"):
                raise ValueError(f"tags_mode must be 'any' or 'all', got {tags_mode!r}")
            tag_fields = [n for n, fs in spec.fields.items() if fs.kind == "TagField"]
            if isinstance(tags, Mapping):
                items = list(tags.items())
            elif len(tag_fields) == 1:
                items = [(tag_fields[0], tags)]
            else:
                raise ValueError(
                    f"recall(tags=) needs a mapping on {spec.name}, which has "
                    f"{len(tag_fields)} TagFields"
                )
            op = Op.ANY if tags_mode == "any" else Op.ALL
            for name, values in items:
                if name not in tag_fields:
                    raise ValueError(f"{name!r} is not a TagField on {spec.name}")
                parts.append(Cond(name, op, list(values)))
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else And(tuple(parts))
