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
from .schema import Column, TableSpec, _bounded, quote_ident

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
            layout = EmbeddingLayout(
                field=name,
                source=fs.options.get("source"),
                vec=name + VEC_SUFFIX,
                model=name + MODEL_SUFFIX,
                hash=name + HASH_SUFFIX,
                dims=dims,
                field_ref=ref,
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
    BM25: naming its *source* re-indexes too, and naming only a *scope*
    column moves the postings to the new scope (Redis's hooks run only for
    the listed field, so its index goes stale there).
    """
    layout: SearchLayout = ts.search
    listed = set(fields) if fields is not None else None
    ctes: list[str] = []
    params: list[Any] = []
    embedded: list[EmbeddingLayout] = []
    scope = scope_text([getattr(obj, c, None) for c in layout.scope_columns])
    n = 0

    def tag() -> str:
        nonlocal n
        n += 1
        return f'"_s{n}"'

    for emb in layout.embedding.values():
        if listed is not None and emb.field not in listed:
            continue
        made = _embed_for_save(obj, emb)
        if made is None:
            continue
        vector, dims = made
        values[emb.field] = dims
        values[emb.vec] = _vector_literal(vector)
        values[emb.model] = _model_identity(emb.field_ref)
        values[emb.hash] = _md5(str(_source_text(obj, emb.source or "")))
        setattr(obj, emb.field, dims)
        embedded.append(emb)

    from ...fields._tokenizer import tokenize

    for bm in layout.bm25.values():
        content = listed is None or bm.field in listed or bm.source in listed
        moved = listed is not None and any(c in listed for c in layout.scope_columns)
        if content:
            text = _source_text(obj, bm.source)
            tokens = tokenize(str(text if text is not None else ""), unique=False)
            counts = collections.Counter(tokens)
            terms = list(counts)
            ctes.append(
                f'{tag()} AS (DELETE FROM {bm.post} WHERE "_pk" = %s AND '
                f'("scope" <> %s OR NOT ("term" = ANY(%s::text[]))))'
            )
            params += [pk, scope, terms]
            if terms:
                ctes.append(
                    f'{tag()} AS (INSERT INTO {bm.post} ("scope", "term", "_pk", '
                    '"tf") SELECT %s, u.t, %s, u.n FROM unnest(%s::text[], '
                    "%s::int[]) AS u(t, n) ORDER BY u.t "
                    'ON CONFLICT ("scope", "term", "_pk") DO UPDATE SET '
                    '"tf" = EXCLUDED."tf")'
                )
                params += [scope, pk, terms, [counts[t] for t in terms]]
                ctes.append(
                    f'{tag()} AS (INSERT INTO {bm.dl} ("_pk", "scope", "len") '
                    'VALUES (%s, %s, %s) ON CONFLICT ("_pk") DO UPDATE SET '
                    '"scope" = EXCLUDED."scope", "len" = EXCLUDED."len")'
                )
                params += [pk, scope, len(tokens)]
            else:
                ctes.append(f'{tag()} AS (DELETE FROM {bm.dl} WHERE "_pk" = %s)')
                params += [pk]
        elif moved:
            ctes.append(
                f'{tag()} AS (UPDATE {bm.post} SET "scope" = %s '
                'WHERE "_pk" = %s AND "scope" <> %s)'
            )
            params += [scope, pk, scope]
            ctes.append(
                f'{tag()} AS (UPDATE {bm.dl} SET "scope" = %s WHERE "_pk" = %s)'
            )
            params += [scope, pk]

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
        vectors = provider.embed([text], input_type="document")
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
        if not done.wait(max(0.0, deadline - time.monotonic())):
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
        _, written = backend._run(
            f"UPDATE {ts.qualified} AS t SET {quote_ident(emb.vec)} = "
            f"u.v::vector, {quote_ident(emb.model)} = %s, "
            f"{quote_ident(emb.hash)} = u.h, {quote_ident(emb.field)} = u.d "
            "FROM unnest(%s::text[], %s::text[], %s::text[], %s::int[]) "
            "AS u(pk, v, h, d) "
            f'WHERE t."_pk" = u.pk AND md5(t.{src}::text) = u.h',
            [
                model,
                [g[0] for g in good],
                [g[1] for g in good],
                [g[2] for g in good],
                [g[3] for g in good],
            ],
            write=True,
        )
        return int(written or 0)
    except Exception as exc:  # never fail the save that triggered it
        logger.warning("popoto backfill skipped: %s: %s", type(exc).__name__, exc)
        return 0
    finally:
        if not released:
            _backfill_busy.release()


# -- BM25 SQL ------------------------------------------------------------------


def bm25_ctes(
    bm: BM25Layout,
    *,
    stats_scope: Optional[str],
    candidate_scope: Optional[str],
    k1: float,
    b: float,
    prefix: str = "b",
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
    """
    q, st, df, sc = (f'"{prefix}{x}"' for x in ("q", "st", "df", "sc"))
    params: list[Any] = []
    ctes = [f'{q}("term", "ord") AS (SELECT * FROM unnest(%s::text[]) WITH ORDINALITY)']
    stats_where = ' WHERE "scope" = %s' if stats_scope is not None else ""
    ctes.append(
        f'{st} AS (SELECT count(*)::float8 AS "n", '
        f'coalesce(avg("len"), 0)::float8 AS "avgdl" FROM {bm.dl}{stats_where})'
    )
    if stats_scope is not None:
        params.append(stats_scope)
    df_where = ' WHERE p."scope" = %s' if stats_scope is not None else ""
    ctes.append(
        f'{df} AS (SELECT p."term", count(*)::float8 AS "df" FROM {bm.post} p '
        f'JOIN {q} ON {q}."term" = p."term"{df_where} GROUP BY p."term")'
    )
    if stats_scope is not None:
        params.append(stats_scope)
    cand_where = ' WHERE p."scope" = %s' if candidate_scope is not None else ""
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
            bm, stats_scope=stats_scope, candidate_scope=scope, k1=k1, b=b
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
            fetch_cap=SCOPED_SEARCH_FETCH_CAP if allowed is not None else None,
        )
        return [(rid.canonical, _lua_number(score)) for rid, score in scored]

    def bm25_corpus(self, spec: ModelSpec, field: str) -> tuple[int, float]:
        """``(N, avgdl)`` corpus-wide: records with at least one token, and
        their mean length (``(0, 0.0)`` for none)."""
        ts, bm = self._bm25(spec, field)
        rows, _ = self._run(
            f'SELECT count(*), coalesce(avg("len"), 0)::float8 FROM {bm.dl}'
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
        rows, _ = self._run(
            f"SELECT (SELECT count(*) FROM {bm.dl}), u.t, "
            f'(SELECT count(*) FROM {bm.post} p WHERE p."term" = u.t) '
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
        scored, _info = self._vector_arm(spec, field, query, limit=limit, where=where)
        if min_score is not None:
            scored = [(rid, s) for rid, s in scored if s >= min_score]
        return scored

    def _vector_arm(
        self,
        spec: ModelSpec,
        field: str,
        query: Sequence[float],
        *,
        limit: int,
        where: Optional[Predicate] = None,
        force: Optional[str] = None,
    ) -> tuple[Scored, dict[str, Any]]:
        """The vector arm and how it ran: ``{"path": "exact"|"hnsw",
        "guard": bool, "embedded": n}``. ``force`` pins the path (tests)."""
        import numpy as np

        ts, emb = self._embedding(spec, field)
        info: dict[str, Any] = {"path": None, "guard": False, "embedded": 0}
        q = np.asarray(query, dtype=np.float32)
        if limit is None or limit <= 0 or not q.size or not np.linalg.norm(q):
            return [], info
        literal = _vector_literal(q)
        cond, cond_params = _cond_text(ts, spec, where)
        vec = quote_ident(emb.vec)
        rows, _ = self._run(
            f"SELECT count(*) FROM {ts.qualified} WHERE {vec} IS NOT NULL AND {cond}",
            cond_params,
        )
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
        exact_sql = (
            f'{base} ORDER BY ({vec} <=> %s::vector) + 0, "_pk" COLLATE "C" ' "LIMIT %s"
        )
        exact_params = [literal] + cond_params + [literal, int(limit)]
        if path == "hnsw":
            rows, _ = self._run(
                self._hnsw_prefix()
                + f'SELECT "_pk", "d" FROM ({base} ORDER BY {vec} <=> %s::vector '
                'LIMIT %s) AS h ORDER BY "d", "_pk" COLLATE "C"',
                exact_params,
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
        ef = int(Defaults.PG_HNSW_EF_SEARCH)
        return (
            f"SET LOCAL hnsw.ef_search = {ef}; "
            "SET LOCAL hnsw.iterative_scan = relaxed_order; "
        )

    def load_vectors(
        self, spec: ModelSpec, field: str
    ) -> tuple[list[str], list[list[float]]]:
        """Every stored vector of ``field``: ``(keys, vectors)`` in ``_pk``
        bytewise order (``EmbeddingField.load_embeddings`` on Postgres)."""
        ts, emb = self._embedding(spec, field)
        vec = quote_ident(emb.vec)
        rows, _ = self._run(
            f'SELECT "_pk", {vec}::text FROM {ts.qualified} WHERE {vec} IS NOT NULL '
            'ORDER BY "_pk" COLLATE "C"'
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
            self._run(
                f'INSERT INTO {mb.table} ("token", "_pk") SELECT u.t, %s FROM '
                "unnest(%s::text[]) AS u(t) ON CONFLICT DO NOTHING",
                [id.canonical, tokens],
                uow=uow,
                write=True,
            )
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
        if mode == "any":
            if mb.kind != "ExistenceFilter":
                raise BackendCapabilityError("mode='any' is for an ExistenceFilter")
            rows, _ = self._run(
                f'SELECT EXISTS (SELECT 1 FROM {mb.table} WHERE "token" = '
                "ANY(%s::text[]))",
                [tokens],
            )
            return bool(rows[0][0])
        if mode == "each":
            if mb.kind != "ExistenceFilter":
                raise BackendCapabilityError("mode='each' is for an ExistenceFilter")
            rows, _ = self._run(
                f'SELECT EXISTS (SELECT 1 FROM {mb.table} m WHERE m."token" = '
                "u.t) FROM unnest(%s::text[]) WITH ORDINALITY AS u(t, i) "
                "ORDER BY u.i",
                [tokens],
            )
            return [bool(r[0]) for r in rows]
        if mode == "count":
            if mb.kind == "ExistenceFilter":
                rows, _ = self._run(
                    f'SELECT (SELECT count(*) FROM {mb.table} m WHERE m."token" '
                    "= u.t) FROM unnest(%s::text[]) WITH ORDINALITY AS u(t, i) "
                    "ORDER BY u.i",
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
        rows, _ = self._run(f'SELECT count(DISTINCT "token") FROM {mb.table}')
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
    ) -> list[tuple[dict[str, Any], float]]:
        """BM25 and vector arms fused by weighted RRF (``Σ w / (k + rank)``)
        in one statement that also returns the fused records' full rows:
        ``[(row, score)]``, best first, ties by ``_pk`` bytewise. See
        :meth:`popoto.models.query.Query.recall` for the arguments.

        ``extra_arms`` are already-ranked key lists (``{name: [key, …]}``)
        fused beside the SQL arms -- the decay arm, which ``rank_decayed``
        computes -- with ``weights[name]``.
        """
        from ...fields._tokenizer import tokenize

        if bm25_stats not in ("scope", "corpus"):
            raise ValueError(
                f"bm25_stats must be 'scope' or 'corpus', got {bm25_stats!r}"
            )
        ts, layout = self._layout(spec)
        weights = dict(weights or {})
        info: dict[str, Any] = {"vector": None, "bm25": False}
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
        extra_arms = dict(extra_arms or {})
        decay = next(
            (
                n
                for n in sorted(spec.fields)
                if spec.fields[n].kind == "DecayingSortedField"
            ),
            None,
        )
        if (
            decay is not None
            and "decay" not in extra_arms
            and float(weights.get("decay", 1.0)) > 0
        ):
            # The decay x confidence arm is rank_decayed's ranking over the
            # same scope and filters, fused beside the SQL arms.
            confidence = next(
                (
                    n
                    for n in sorted(spec.fields)
                    if spec.fields[n].kind == "ConfidenceField"
                ),
                None,
            )
            try:
                ranked = self.rank_decayed(  # type: ignore[attr-defined]
                    spec,
                    decay,
                    now=time.time(),
                    n=depth,
                    where=where,
                    confidence_field=confidence,
                )
                extra_arms["decay"] = [rid.canonical for rid, _s in ranked]
            except BackendCapabilityError:
                pass

        ctes: list[str] = []
        params: list[Any] = []
        arms: list[tuple[str, float]] = []
        cond, cond_params = _cond_text(ts, spec, where)

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
                )
                ctes += bctes
                params += [tokens] + bparams
                ctes.append(
                    '"a_bm25" AS (SELECT s."_pk", row_number() OVER (ORDER BY '
                    's."score" DESC, s."_pk" COLLATE "C") AS "rank" FROM '
                    f'{sc} s WHERE s."_pk" IN (SELECT "_pk" FROM {ts.qualified} '
                    f'WHERE {cond}) ORDER BY "rank" LIMIT %s)'
                )
                params += cond_params + [depth]
                arms.append(('"a_bm25"', weight))
                info["bm25"] = True

        prefix = ""
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
                    literal = _vector_literal(arr)
                    vctes, vparams = self._vector_ctes(
                        ts, emb, literal, cond, cond_params, depth
                    )
                    ctes += vctes
                    params += vparams
                    arms.append(('"a_vec"', weight))
                    info["vector"] = "sql"
                    if emb.hnsw:
                        prefix = self._hnsw_prefix()

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
        guard = ""
        if info["vector"] == "sql":
            guard = ', (SELECT count(*) FROM "vg") > 0, (SELECT n FROM "vn")'
        sql = (
            prefix
            + "WITH "
            + ", ".join(ctes)
            + f' SELECT {col_sql}, f."score"{guard} FROM "fused" f JOIN '
            f'{ts.qualified} t ON t."_pk" = f."_pk" '
            'ORDER BY f."score" DESC, t."_pk" COLLATE "C"'
        )
        rows, _ = self._run(sql, params)
        out = []
        width = len(cols)
        for row in rows:
            decoded = self._decode(ts, cols, row[:width])
            out.append((decoded, float(row[width])))
        if rows and info["vector"] == "sql":
            info["guard"] = bool(rows[0][width + 1])
            info["embedded"] = int(rows[0][width + 2])
        return out

    def _vector_ctes(
        self,
        ts: TableSpec,
        emb: EmbeddingLayout,
        literal: str,
        cond: str,
        cond_params: list[Any],
        depth: int,
    ) -> tuple[list[str], list[Any]]:
        """The vector arm as CTEs, choosing its path inside the statement:
        ``vn`` counts the rows with a vector in scope; ``vx`` (exact, ordered
        by ``(v <=> q) + 0``) runs only at or below the threshold, ``vh``
        (HNSW) only above it, and ``vg`` (exact again) only when ``vh`` came
        back short -- the recall guard. Each gate is a pseudo-constant
        condition, so the planner puts it on a one-time filter and a gated
        branch is never executed."""
        vec = quote_ident(emb.vec)
        exact_max = int(Defaults.PG_VECTOR_EXACT_MAX)
        sel = (
            f'SELECT "_pk", ({vec} <=> %s::vector) AS "d" FROM {ts.qualified} '
            f"WHERE {vec} IS NOT NULL AND ({cond})"
        )
        ctes = [
            f'"vn"("n") AS (SELECT count(*) FROM {ts.qualified} WHERE {vec} IS NOT '
            f"NULL AND ({cond}))"
        ]
        params: list[Any] = list(cond_params)
        ctes.append(
            f'"vx" AS ({sel} AND (SELECT "n" FROM "vn") <= %s '
            f'ORDER BY ({vec} <=> %s::vector) + 0, "_pk" COLLATE "C" LIMIT %s)'
        )
        params += (
            [literal] + cond_params + [exact_max if emb.hnsw else 2**62, literal, depth]
        )
        if emb.hnsw:
            ctes.append(
                f'"vh" AS MATERIALIZED ({sel} AND (SELECT "n" FROM "vn") > %s '
                f"ORDER BY {vec} <=> %s::vector LIMIT %s)"
            )
            params += [literal] + cond_params + [exact_max, literal, depth]
            short = '(SELECT count(*) FROM "vh") < least(%s, (SELECT "n" FROM "vn"))'
            ctes.append(
                f'"vg" AS ({sel} AND (SELECT "n" FROM "vn") > %s AND {short} '
                f'ORDER BY ({vec} <=> %s::vector) + 0, "_pk" COLLATE "C" LIMIT %s)'
            )
            params += [literal] + cond_params + [exact_max, depth, literal, depth]
            ctes.append(
                '"vall" AS (SELECT * FROM "vx" UNION ALL SELECT * FROM "vh" WHERE '
                'NOT ((SELECT count(*) FROM "vh") < least(%s, (SELECT "n" FROM '
                '"vn"))) UNION ALL SELECT * FROM "vg")'
            )
            params += [depth]
        else:
            ctes.append('"vg" AS (SELECT * FROM "vx" WHERE false)')
            ctes.append('"vall" AS (SELECT * FROM "vx")')
        ctes.append(
            '"a_vec" AS (SELECT "_pk", row_number() OVER (ORDER BY "d", '
            '"_pk" COLLATE "C") AS "rank" FROM "vall" WHERE "d" < 1)'
        )
        return ctes, params

    @staticmethod
    def _embed_query(emb: EmbeddingLayout, text: str) -> Optional[list[float]]:
        if not text or not text.strip():
            return None
        provider = emb.field_ref.provider if emb.field_ref is not None else None
        if provider is None:
            return None
        try:
            vectors = provider.embed([text], input_type="query")
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
