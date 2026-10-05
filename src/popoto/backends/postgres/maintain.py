"""Index maintenance and transfer state on Postgres (#759 M5; plan §2 H).

``Model.check_indexes`` / ``clean_indexes`` / ``rebuild_indexes`` (and their
``async_`` twins, which run them on the async backend through #784's
bridge) reach :meth:`MaintainOpsMixin.maintain` on a Postgres-bound model;
``Model.raw_update`` and the carried state of ``ConfidenceField``,
``EmbeddingField`` and ``AccessTrackerMixin`` (``export_state`` /
``import_state``, read by :mod:`popoto.transfer`) reach the ``field_call``
adapters below.

What "index drift" means here
-----------------------------
On Redis an index is a separate key -- the ``$Class:`` set, a key-field set,
a sorted set, a geo set, a composite-index hash -- written by a second
command after the record's ``HSET``, so it can point at a hash that is gone
(an orphan) or miss one that exists. On Postgres those five are the record
table itself and B-trees on it, maintained by the server in the row's own
transaction: there is no second write to lose, so their counts are zero by
construction (``class_set``, ``key_fields``, ``sorted_fields``,
``geo_fields``, ``composite_indexes`` keep their Redis keys, always ``0``).
The one way a B-tree goes wrong is corruption, which ``rebuild`` repairs with
``REINDEX``.

What *can* drift is the state kept beside the row in companion tables, which
popoto writes rather than the server (``.search``, ``.validity``):

* **orphans** -- a side row whose record row is gone. Every companion that
  names a record has ``REFERENCES … ON DELETE CASCADE``, so a popoto delete
  can never leave one; a bulk load or restore run with triggers disabled
  (``session_replication_role = replica``, ``pg_restore
  --disable-triggers`` -- the shape a #756 copy may take) can. Counted per
  side row. ``CoOccurrenceField`` edges are not in this list: they have no
  foreign key (Redis links any two key strings, records or not), and Redis
  keeps an edge whose record is gone (never saved, expired, deleted outside
  popoto) and never counts it. So do ``clean`` and ``rebuild`` here, and
  ``check`` reports them apart, as ``side_tables["graph_edges"]["dangling"]``,
  outside ``total`` (:func:`_edge_dangling_sql`).
* **invalid indexes** -- the INVALID ``*_ccnew`` / ``*_ccold`` indexes a
  failed ``REINDEX ... CONCURRENTLY`` leaves (a lock timeout, a cancel).
  ``check`` counts them (``invalid_indexes``); ``clean`` and ``rebuild``
  drop them.
* **missing** -- a live record whose derived side rows are absent: a BM25
  field with tokens but no postings or document length, an embedding with a
  vector but no narrow vector row, an ``ExistenceFilter`` without its
  fingerprint tokens. Counted per record.
* **stale** -- a live record whose derived side rows disagree with it:
  postings or a length that are not the tokenization of the source text
  (``Model.raw_update`` of a source column leaves exactly this), side rows
  under another scope than the record's, a narrow vector row that is not the
  record's vector. Counted per record.

``partial_writes`` keeps its Redis meaning for a single-``AutoKeyField``
model: a live row whose auto-key column is ``NULL`` or ``''`` (only direct
SQL can write one; ``clean`` deletes it, as Redis ``DEL``\\ s the hash).

Not drift: an edge whose endpoint has no record (never saved, expired,
deleted outside popoto) -- Redis keeps those, so ``clean``/``rebuild`` do;
``check`` only reports them apart (``graph_edges.dangling``);
``FrequencySketch`` counts (never decremented, as the sketch never is); the
engine tables keyed by ``(model, member)`` with no foreign key --
``popoto_tombstone``, ``popoto_embedding_cache``, ``popoto_recall_proposal``
and the prediction ledger / error tables -- which outlive the record on
Redis too and are not checked there either; the cycles / confidence / access
/ validity *columns* (they are the row); and the open-claim pointers'
content (carried state; only their orphans count).

``rebuild`` refuses to run while a ``transaction()`` or ``popoto.batch()``
is open in the calling task or thread, and runs ``REINDEX`` on a dedicated
connection with a bounded ``lock_timeout``/``statement_timeout``; a REINDEX
that stops early raises :class:`~popoto.backends.MaintenanceIncompleteError`
(not an outage) -- see :meth:`MaintainOpsMixin._reindex_analyze`.

Every pass reads live rows only: an expired row (M5 TTL) is neither checked
nor rebuilt, and its side rows are the reaper's, never counted as orphans.
Work is bounded: rows go in ``batch_size`` keyset pages (``_pk COLLATE
"C"``), each rebuild page one transaction that takes the page's record-key
locks first (:meth:`PostgresBackend._record_locked`, the backend's one lock
order), so a concurrent save of a record either commits before the page reads
it or waits for the page.

Never imports ``redis``.
"""

from __future__ import annotations

import base64
import collections
import io
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from ...fields.constants import Defaults
from ..types import (
    BackendCapabilityError,
    MaintenanceIncompleteError,
    ModelSpec,
    RecordId,
    UnitOfWork,
)
from .graph import edge_table, graph_fields
from .memory import (
    ACCESS_FIELD,
    ACCESS_MIXIN,
    CONF_SUFFIXES,
    CONFIDENCE_KIND,
    NOT_HANDLED,
)
from .schema import TableSpec, quote_ident
from .search import (
    _model_identity,
    _source_text,
    _vector_literal,
    scope_text,
)
from .ttl import live_sql
from .validity import pointer_table, validity_field_names

__all__ = [
    "MAINTAIN_FIELD",
    "MaintainOpsMixin",
    "SideDrift",
    "npy_bytes",
    "parse_npy",
]

logger = logging.getLogger("POPOTO.postgres.maintain")

MAINTAIN_FIELD = "_maintain"
"""The ``field_call`` pseudo-field for model-level maintenance ops
(``raw_update``), like ``.memory``'s ``_access`` / ``_observe``."""

EMBEDDING_KIND = "EmbeddingField"


def npy_bytes(vector: Sequence[float]) -> bytes:
    """``vector`` as the ``.npy`` file the Redis backend stores (float32): the
    one wire shape ``EmbeddingField.export_state`` carries on both backends."""
    import numpy as np

    buffer = io.BytesIO()
    np.save(buffer, np.asarray(vector, dtype=np.float32))
    return buffer.getvalue()


def parse_npy(raw: bytes) -> list[float]:
    """The vector in a carried ``.npy`` payload (any float dtype, 1-D)."""
    import numpy as np

    array = np.load(io.BytesIO(raw), allow_pickle=False)
    return [float(x) for x in np.asarray(array, dtype=np.float64).ravel()]


@dataclass
class SideDrift:
    """One field's side-table drift: orphan side rows, and the live records
    whose derived side rows are missing or stale (their keys, for rebuild)."""

    orphans: int = 0
    missing: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "orphans": self.orphans,
            "missing": len(self.missing),
            "stale": len(self.stale),
        }


def _q(ts: TableSpec, bare: str) -> str:
    return f"{quote_ident(ts.schema)}.{quote_ident(bare)}"


def _fk_side_tables(ts: TableSpec, spec: ModelSpec) -> list[tuple[str, str, str]]:
    """``(field, qualified table, record-key column)`` for every companion
    table whose rows name a record through a foreign key."""
    out: list[tuple[str, str, str]] = []
    layout = ts.search
    if layout is not None:
        for bm in layout.bm25.values():
            out.append((bm.field, bm.post, "_pk"))
            out.append((bm.field, bm.dl, "_pk"))
        for emb in layout.embedding.values():
            out.append((emb.field, emb.narrow, "_pk"))
        for mb in layout.membership.values():
            if mb.kind == "ExistenceFilter":
                out.append((mb.field, mb.table, "_pk"))
    for name in validity_field_names(spec):
        out.append((name, pointer_table(ts, name), "member"))
    return out


def _edge_tables(ts: TableSpec, spec: ModelSpec) -> list[tuple[str, str, bool]]:
    """``(field, qualified edge table, symmetric)`` per ``CoOccurrenceField``.
    Edge rows have no foreign key (Redis links any two key strings), so
    a dangling one is found by :func:`_edge_dangling_sql`, not an FK join."""
    return [
        (
            name,
            edge_table(ts, name),
            bool(spec.fields[name].options.get("symmetric", True)),
        )
        for name in graph_fields(spec)
    ]


def _edge_dangling_sql(ts: TableSpec, symmetric: bool) -> tuple[str, int]:
    """The ``WHERE`` condition (on alias ``e``) for a dangling edge row, and
    how many ``%s`` (the model's key prefix) it takes. Informational only:
    ``check`` counts these, nothing deletes them (Redis keeps them too).

    An endpoint is *gone* when it is in this model's key space
    (``<Model>:``) and no row of the table has it as ``_pk``. A ``src`` that
    is gone dangles in every field; a ``dst`` that is gone only in a
    symmetric field (an asymmetric delete leaves edges *to* the record by
    design, on Redis and here alike). An endpoint outside the model's key
    space (``link`` takes any string) is never counted."""

    def gone(col: str) -> str:
        return (
            f'(starts_with(e."{col}", %s) AND NOT EXISTS '
            f'(SELECT 1 FROM {ts.qualified} t WHERE t."_pk" = e."{col}"))'
        )

    if symmetric:
        return f"({gone('src')} OR {gone('dst')})", 2
    return gone("src"), 1


def _side_fields(ts: TableSpec, spec: ModelSpec) -> list[str]:
    """Every field with side tables that can drift, sorted."""
    return sorted({f for f, _t, _c in _fk_side_tables(ts, spec)})


def _key_prefix(model: Any) -> str:
    """The model's record-key prefix, ``<Model>:`` (every ``_pk`` has it)."""
    return str(model._meta.db_class_key.redis_key) + ":"


#: Postgres names the transient index of a ``REINDEX ... CONCURRENTLY``
#: ``<index>_ccnew`` (``_ccnew1``... on a clash) and the swapped-out one
#: ``<index>_ccold``; a failed run leaves either behind, INVALID.
_CONCURRENT_LEFTOVER = "_cc(new|old)[0-9]*$"

#: SQLSTATEs of a failed maintenance statement that mean the server or the
#: link is gone (an outage), not that the statement stopped early.
_OUTAGE_SQLSTATE_PREFIXES = ("08", "57P01", "57P02", "57P03")


def _is_outage(exc: BaseException) -> bool:
    """A connection-level failure: no server error at all (the reply was
    lost), a connection exception (class 08), or an admin/crash shutdown.
    Everything else -- a lock timeout (55P03), a statement timeout or cancel
    (57014), a deadlock, any SQL error -- is the statement stopping early."""
    import psycopg

    if not isinstance(exc, psycopg.OperationalError):
        return False
    sqlstate = getattr(exc, "sqlstate", None)
    return sqlstate is None or sqlstate.startswith(_OUTAGE_SQLSTATE_PREFIXES)


def _all_side_tables(ts: TableSpec, spec: ModelSpec) -> list[str]:
    """Every companion table of the model (for ``REINDEX``/``ANALYZE``)."""
    tables = [_q(ts, name) for name, _stmts in ts.companions]
    tables += [pointer_table(ts, name) for name in validity_field_names(spec)]
    return tables


@dataclass
class _Expected:
    """What a record's derived side rows must be, from its stored values."""

    bm25: dict[str, tuple[str, dict[str, int], int]] = field(default_factory=dict)
    """field -> (scope, {term: tf}, document length)"""
    embedding: dict[str, str] = field(default_factory=dict)
    """field -> scope"""
    tokens: dict[str, set[str]] = field(default_factory=dict)
    """ExistenceFilter field -> its fingerprint tokens"""


def _expected(ts: TableSpec, instance: Any) -> _Expected:
    """The side rows a save of ``instance`` writes (``.search.prepare_save``),
    derived from the stored values -- the rebuild's source of truth."""
    from ...fields._tokenizer import tokenize
    from ...fields.existence_filter import _compute_fingerprint_impl

    out = _Expected()
    layout = ts.search
    if layout is None:
        return out
    scope = scope_text([getattr(instance, c, None) for c in layout.scope_columns])
    for bm in layout.bm25.values():
        text = _source_text(instance, bm.source)
        tokens = tokenize(str(text if text is not None else ""), unique=False)
        out.bm25[bm.field] = (scope, dict(collections.Counter(tokens)), len(tokens))
    for emb in layout.embedding.values():
        out.embedding[emb.field] = scope
    for mb in layout.membership.values():
        if mb.kind != "ExistenceFilter":
            continue
        fingerprint = _compute_fingerprint_impl(mb.field_ref, instance)
        out.tokens[mb.field] = set(tokenize(fingerprint) or [fingerprint.lower()])
    return out


class MaintainOpsMixin:
    """``maintain`` and the transfer-state / ``raw_update`` adapters, mixed
    into :class:`~popoto.backends.postgres.PostgresBackend`."""

    # Provided by PostgresBackend (declared for the type checker only).
    schema: str
    _table: Callable[..., TableSpec]
    _run: Callable[..., tuple[list[tuple[Any, ...]], int]]
    _record_locked: Callable[..., tuple[str, list[Any]]]
    _select_cols: Callable[..., list[str]]
    _decode: Callable[..., dict[str, Any]]
    _row_values: Callable[..., dict[str, Any]]
    _after_write: Callable[..., None]
    _connection: Callable[..., Any]
    _maintenance_connection: Callable[..., Any]
    _unit_open_here: Callable[[], bool]
    _fail: Callable[..., Exception]
    transaction: Callable[..., Any]
    delete: Callable[..., int]

    # -- protocol method ------------------------------------------------------

    def maintain(
        self,
        spec: ModelSpec,
        op: str,
        *,
        batch_size: int = 1000,
        model: Any = None,
    ) -> Any:
        """``check`` -> the ``check_indexes`` dict; ``clean`` -> entries
        removed (``int``); ``rebuild`` -> ``(records indexed, diverged
        keys)``. ``model`` is the ``Model`` class (needed to derive the side
        rows from its fields; ``Model``'s methods always pass it)."""
        if model is None:
            raise BackendCapabilityError(
                "PostgresBackend.maintain needs model=<the Model class>"
            )
        batch_size = max(1, int(batch_size))
        if op == "check":
            return self._maintain_check(spec, model, batch_size)
        if op == "clean":
            return self._maintain_clean(spec, model, batch_size)
        if op == "rebuild":
            return self._maintain_rebuild(spec, model, batch_size)
        raise ValueError(f"maintain op must be check/clean/rebuild, got {op!r}")

    # -- shared scans ---------------------------------------------------------

    def _live_pages(self, ts: TableSpec, batch_size: int) -> Iterator[list[str]]:
        """The live records' keys in ``_pk`` byte order, ``batch_size`` at a
        time (keyset pagination: each page is one bounded statement)."""
        live = live_sql(ts)
        last: Optional[str] = None
        while True:
            where = []
            params: list[Any] = []
            if last is not None:
                where.append('"_pk" COLLATE "C" > %s')
                params.append(last)
            if live:
                where.append(live)
            sql = f'SELECT "_pk" FROM {ts.qualified}'
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += ' ORDER BY "_pk" COLLATE "C" LIMIT %s'
            rows, _ = self._run(sql, params + [batch_size])
            if not rows:
                return
            keys = [r[0] for r in rows]
            yield keys
            if len(keys) < batch_size:
                return
            last = keys[-1]

    def _instances(
        self,
        ts: TableSpec,
        model: Any,
        keys: Sequence[str],
        uow: Optional[UnitOfWork] = None,
    ) -> list[tuple[str, Any]]:
        """``(stored key, hydrated instance)`` for the live rows of ``keys``,
        read on ``uow`` when given (after its locks)."""
        from ...models.encoding import hydrate_decoded_row

        cols = self._select_cols(ts, None)
        col_sql = ", ".join(quote_ident(c) for c in cols)
        sql = f'SELECT {col_sql} FROM {ts.qualified} WHERE "_pk" = ANY(%s::text[])'
        live = live_sql(ts)
        if live:
            sql += " AND " + live
        rows, _ = self._run(sql, [list(keys)], uow=uow)
        out = []
        for row in sorted(rows, key=lambda r: str(r[0]).encode("utf-8")):
            decoded = self._decode(ts, cols, row)
            out.append((row[0], hydrate_decoded_row(model, decoded)))
        return out

    def _actual(
        self, ts: TableSpec, keys: Sequence[str], uow: Optional[UnitOfWork] = None
    ) -> dict[str, Any]:
        """The side rows the records ``keys`` have now, per field."""
        layout = ts.search
        out: dict[str, Any] = {}
        if layout is None:
            return out
        k = [list(keys)]
        for bm in layout.bm25.values():
            post: dict[str, dict[str, int]] = collections.defaultdict(dict)
            post_scopes: dict[str, set[str]] = collections.defaultdict(set)
            rows, _ = self._run(
                f'SELECT "_pk", "term", "tf", "scope" FROM {bm.post} '
                'WHERE "_pk" = ANY(%s::text[])',
                k,
                uow=uow,
            )
            for pk, term, tf, scope in rows:
                post[pk][term] = int(tf)
                post_scopes[pk].add(scope)
            rows, _ = self._run(
                f'SELECT "_pk", "scope", "len" FROM {bm.dl} '
                'WHERE "_pk" = ANY(%s::text[])',
                k,
                uow=uow,
            )
            dl = {pk: (scope, int(n)) for pk, scope, n in rows}
            out[bm.field] = (post, post_scopes, dl)
        for emb in layout.embedding.values():
            rows, _ = self._run(
                f'SELECT t."_pk", t.{quote_ident(emb.vec)}::text, n."v"::text, '
                f'n."scope" FROM {ts.qualified} t LEFT JOIN {emb.narrow} n '
                'ON n."_pk" = t."_pk" WHERE t."_pk" = ANY(%s::text[])',
                k,
                uow=uow,
            )
            out[emb.field] = {pk: (vec, v, scope) for pk, vec, v, scope in rows}
        for mb in layout.membership.values():
            if mb.kind != "ExistenceFilter":
                continue
            toks: dict[str, set[str]] = collections.defaultdict(set)
            rows, _ = self._run(
                f'SELECT "_pk", "token" FROM {mb.table} WHERE "_pk" = ANY(%s::text[])',
                k,
                uow=uow,
            )
            for pk, token in rows:
                toks[pk].add(token)
            out[mb.field] = toks
        return out

    @staticmethod
    def _classify(
        ts: TableSpec,
        pk: str,
        want: _Expected,
        have: Mapping[str, Any],
        drift: dict[str, SideDrift],
    ) -> None:
        """Add ``pk`` to each field's ``missing`` / ``stale`` list where its
        side rows are not what ``want`` derives."""
        layout = ts.search
        if layout is None:
            return
        for bm in layout.bm25.values():
            scope, terms, length = want.bm25[bm.field]
            post, post_scopes, dl = have[bm.field]
            got_terms = post.get(pk, {})
            got_dl = dl.get(pk)
            if not terms:
                if got_terms or got_dl is not None:
                    drift[bm.field].stale.append(pk)
                continue
            if not got_terms and got_dl is None:
                drift[bm.field].missing.append(pk)
            elif (
                got_terms != terms
                or post_scopes.get(pk, set()) != {scope}
                or got_dl != (scope, length)
            ):
                drift[bm.field].stale.append(pk)
        for emb in layout.embedding.values():
            scope = want.embedding[emb.field]
            vec, v, got_scope = have[emb.field].get(pk, (None, None, None))
            if vec is None:
                if v is not None:
                    drift[emb.field].stale.append(pk)
            elif v is None:
                drift[emb.field].missing.append(pk)
            elif v != vec or got_scope != scope:
                drift[emb.field].stale.append(pk)
        for name, tokens in want.tokens.items():
            if not tokens <= have[name].get(pk, set()):
                drift[name].missing.append(pk)

    def _orphan_count(self, ts: TableSpec, table: str, col: str) -> int:
        c = quote_ident(col)
        rows, _ = self._run(
            f"SELECT count(*) FROM {table} s WHERE NOT EXISTS "
            f'(SELECT 1 FROM {ts.qualified} t WHERE t."_pk" = s.{c})'
        )
        return int(rows[0][0])

    def _drift(
        self, spec: ModelSpec, model: Any, batch_size: int
    ) -> tuple[TableSpec, dict[str, SideDrift], int]:
        """Every side field's drift, paging over the live records, and the
        count of dangling edges (informational, never drift)."""
        ts = self._table(spec)
        drift = {name: SideDrift() for name in _side_fields(ts, spec)}
        for name, table, col in _fk_side_tables(ts, spec):
            drift[name].orphans += self._orphan_count(ts, table, col)
        prefix = _key_prefix(model)
        dangling = 0
        for _name, table, symmetric in _edge_tables(ts, spec):
            cond, uses = _edge_dangling_sql(ts, symmetric)
            rows, _ = self._run(
                f"SELECT count(*) FROM {table} e WHERE {cond}", [prefix] * uses
            )
            dangling += int(rows[0][0])
        if ts.search is None:
            return ts, drift, dangling
        for keys in self._live_pages(ts, batch_size):
            have = self._actual(ts, keys)
            for pk, instance in self._instances(ts, model, keys):
                self._classify(ts, pk, _expected(ts, instance), have, drift)
        return ts, drift, dangling

    # -- check ----------------------------------------------------------------

    def _maintain_check(
        self, spec: ModelSpec, model: Any, batch_size: int
    ) -> dict[str, Any]:
        """``check_indexes``' dict: the Redis keys (index kinds that are
        transactional here, always ``0``), ``partial_writes``, and
        ``side_tables`` -- ``{field: {"orphans", "missing", "stale"}}`` for
        every field with companion tables, plus ``"graph_edges": {"dangling":
        n}`` for a model with ``CoOccurrenceField``s: edges naming a record
        that does not exist, which Redis keeps and ``total`` leaves out.
        Read-only; no locks."""
        meta = model._meta
        ts, drift, dangling = self._drift(spec, model, batch_size)
        result: dict[str, Any] = {
            "class_set": 0,
            "partial_writes": self._partial_write_count(ts, model),
            "key_fields": {
                name: 0
                for name in meta.key_field_names
                if not getattr(meta.fields[name], "auto", False)
            },
            "sorted_fields": {name: 0 for name in meta.sorted_field_names},
            "geo_fields": {name: 0 for name in meta.geo_field_names},
            "composite_indexes": {
                meta.get_index_key(tuple(names)): 0 for names, _u in meta.indexes
            },
            "side_tables": {name: d.counts() for name, d in drift.items()},
            "invalid_indexes": len(self._invalid_indexes(ts, spec)),
        }
        result["total"] = (
            result["partial_writes"]
            + result["invalid_indexes"]
            + sum(sum(counts.values()) for counts in result["side_tables"].values())
        )
        if _edge_tables(ts, spec):
            result["side_tables"]["graph_edges"] = {"dangling": dangling}
        return result

    # -- partial writes -------------------------------------------------------

    @staticmethod
    def _auto_key_column(ts: TableSpec, model: Any) -> Optional[str]:
        name = model._get_auto_key_field_name()
        if name is None or ts.field_types.get(name) is not str:
            return None
        return name

    def _partial_write_count(self, ts: TableSpec, model: Any) -> int:
        col = self._auto_key_column(ts, model)
        if col is None:
            return 0
        live = live_sql(ts)
        c = quote_ident(col)
        rows, _ = self._run(
            f"SELECT count(*) FROM {ts.qualified} WHERE ({c} IS NULL OR {c} = '')"
            + (f" AND {live}" if live else "")
        )
        return int(rows[0][0])

    # -- clean ----------------------------------------------------------------

    def _maintain_clean(self, spec: ModelSpec, model: Any, batch_size: int) -> int:
        """Delete orphan side rows and partial-write rows; return how many.
        Missing and stale side rows are ``rebuild``'s (they need the record's
        values, as ``rebuild_indexes`` does on Redis)."""
        ts = self._table(spec, write=True)
        removed = 0
        for _name, table, col in _fk_side_tables(ts, spec):
            removed += self._delete_orphans(ts, table, col, batch_size)
        if not self._unit_open_here():
            # A DROP INDEX CONCURRENTLY would wait on the caller's own open
            # transaction; inside one, the leftovers wait for the next clean.
            found = self._invalid_indexes(ts, spec)
            if found:
                removed += len(found) - len(self._drop_invalid(ts, spec))
        auto = self._auto_key_column(ts, model)
        if auto is not None:
            c = quote_ident(auto)
            live = live_sql(ts)
            while True:
                rows, _ = self._run(
                    f'SELECT "_pk" FROM {ts.qualified} WHERE ({c} IS NULL OR '
                    f"{c} = '')" + (f" AND {live}" if live else "") + " LIMIT %s",
                    [batch_size],
                )
                if not rows:
                    break
                n = self.delete(spec, [RecordId(spec.name, (), r[0]) for r in rows])
                removed += n
                if n == 0:
                    break
        return removed

    def _delete_orphans(
        self, ts: TableSpec, table: str, col: str, batch_size: int
    ) -> int:
        """Delete ``table``'s rows naming no record, a page at a time, each
        behind the named keys' record locks and re-checked in the statement
        (a record saved under that key meanwhile keeps its rows)."""
        c = quote_ident(col)
        absent = (
            f"NOT EXISTS (SELECT 1 FROM {ts.qualified} t WHERE t." f'"_pk" = s.{c})'
        )
        removed = 0
        while True:
            rows, _ = self._run(
                f"SELECT DISTINCT s.{c} FROM {table} s WHERE {absent} LIMIT %s",
                [batch_size],
            )
            if not rows:
                return removed
            keys = [r[0] for r in rows]
            sql, params = self._record_locked(
                ts,
                keys,
                f"DELETE FROM {table} s WHERE s.{c} = ANY(%s::text[]) AND {absent}",
                [keys],
            )
            _, n = self._run(sql, params, write=True)
            removed += int(n or 0)
            if not n:
                return removed

    # -- leftovers of a failed CONCURRENTLY -----------------------------------

    def _invalid_indexes(self, ts: TableSpec, spec: ModelSpec) -> list[str]:
        """The INVALID ``*_ccnew`` / ``*_ccold`` indexes a failed ``REINDEX
        ... CONCURRENTLY`` left on the model's tables (their TOAST tables
        included), as qualified names. Each one is still maintained by every
        write -- an HNSW one at real cost -- and never used by a read, and a
        later ``REINDEX`` does not remove it (#788 review).

        None is reported while any of those tables has an index build in
        progress (``pg_stat_progress_create_index``): a concurrent rebuild's
        transient index is INVALID until it finishes, and dropping it would
        fail that rebuild. (That view hides another role's builds unless the
        caller has ``pg_read_all_stats``; run maintenance as the owning
        role.)"""
        tables = [ts.qualified] + _all_side_tables(ts, spec)
        rows, _ = self._run(
            "WITH m AS (SELECT to_regclass(x) AS oid FROM unnest(%s::text[]) AS x), "
            "rel AS (SELECT oid FROM m WHERE oid IS NOT NULL UNION "
            "SELECT c.reltoastrelid FROM pg_class c JOIN m ON c.oid = m.oid "
            "WHERE c.reltoastrelid <> 0) "
            "SELECT quote_ident(n.nspname) || '.' || quote_ident(ic.relname) "
            "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = ic.relnamespace "
            "WHERE NOT i.indisvalid AND i.indrelid IN (SELECT oid FROM rel) "
            "AND ic.relname ~ %s AND NOT EXISTS (SELECT 1 FROM "
            "pg_stat_progress_create_index p WHERE p.relid IN (SELECT oid FROM rel))",
            [tables, _CONCURRENT_LEFTOVER],
        )
        return sorted((str(r[0]) for r in rows), key=lambda n: n.encode("utf-8"))

    def _drop_invalid(
        self, ts: TableSpec, spec: ModelSpec, lock_timeout_ms: Optional[int] = None
    ) -> list[str]:
        """Best effort: ``DROP INDEX CONCURRENTLY`` each of
        :meth:`_invalid_indexes` on a dedicated connection, waiting at most
        ``lock_timeout_ms`` (default ``PG_MAINTAIN_CLEANUP_LOCK_TIMEOUT_MS``)
        for any lock. Returns the ones still there. A statement that stops
        early is logged, not raised: the leftovers are reported by
        ``check_indexes()`` and dropped by the next rebuild or clean. An
        outage still raises (``BackendUnavailableError``)."""
        found = self._invalid_indexes(ts, spec)
        if not found:
            return []
        if lock_timeout_ms is None:
            lock_timeout_ms = int(Defaults.PG_MAINTAIN_CLEANUP_LOCK_TIMEOUT_MS)
        left = list(found)
        try:
            with self._maintenance_connection(lock_timeout_ms) as conn:
                for name in found:
                    conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                    left.remove(name)
        except Exception as exc:
            if _is_outage(exc):
                raise self._fail(exc, write=False) from exc
            logger.warning(
                "%s: could not drop %d INVALID index(es) left by a failed "
                "REINDEX CONCURRENTLY (%s: %s); the next rebuild_indexes() or "
                "clean_indexes() drops them: %s",
                spec.name,
                len(left),
                type(exc).__name__,
                exc,
                ", ".join(left),
            )
        return left

    # -- rebuild --------------------------------------------------------------

    def _maintain_rebuild(
        self, spec: ModelSpec, model: Any, batch_size: int
    ) -> tuple[int, list[str]]:
        """Recompute every derived side row from the live records, remove
        orphans, then ``REINDEX TABLE CONCURRENTLY`` + ``ANALYZE`` the table
        and its companions. Returns ``(records indexed, diverged keys)``: a
        row whose key columns no longer produce its stored ``_pk`` (direct
        SQL on a key column) is skipped and reported, as Redis skips a hash
        whose derived key differs from the one it is stored under."""
        if self._unit_open_here():
            raise BackendCapabilityError(
                f"{spec.name}.rebuild_indexes() cannot run while a Postgres "
                "transaction() or popoto.batch() is open in this task or thread "
                "(or in a parent task awaiting it): its REINDEX TABLE "
                "CONCURRENTLY waits for every open transaction on the table to "
                "end, including that one, which cannot end while this call "
                "waits. Run it after the batch is executed or the transaction "
                "has committed"
            )
        ts = self._table(spec, write=True)
        # A previous failed CONCURRENTLY's leftovers first: REINDEX never
        # removes them, and every write meanwhile maintains them.
        self._drop_invalid(ts, spec)
        count = 0
        diverged: list[str] = []
        for keys in self._live_pages(ts, batch_size):
            n, skipped = self._rebuild_page(ts, model, keys)
            count += n
            diverged.extend(skipped)
        done = ["side_rows"]
        for _name, table, col in _fk_side_tables(ts, spec):
            self._delete_orphans(ts, table, col, batch_size)
        done.append("orphans")
        self._reindex_analyze(ts, spec, done, count, diverged)
        if diverged:
            logger.warning(
                "%s.rebuild_indexes() skipped %d Postgres row(s) whose key "
                "columns do not produce their stored _pk; first: %s",
                spec.name,
                len(diverged),
                diverged[0],
            )
        return count, diverged

    def _rebuild_page(
        self, ts: TableSpec, model: Any, keys: Sequence[str]
    ) -> tuple[int, list[str]]:
        """One transaction: the page's record-key locks (``_pk`` byte order),
        then its rows read under them, then each drifted record's side rows
        rewritten."""
        from .search import record_lock_sql

        with self.transaction() as uow:
            lock, lock_params = record_lock_sql(ts, list(keys))
            # The lock statement alone; the reads and writes follow on the
            # same transaction, so they all run after every lock is held.
            self._run(lock + "SELECT 1", lock_params, uow=uow, write=True)
            pairs = self._instances(ts, model, keys, uow)
            good: list[tuple[str, Any]] = []
            diverged: list[str] = []
            for pk, instance in pairs:
                try:
                    derived = instance.db_key.redis_key
                except Exception:
                    derived = None
                if derived != pk:
                    diverged.append(pk)
                else:
                    good.append((pk, instance))
            if ts.search is not None and good:
                have = self._actual(ts, [pk for pk, _ in good], uow)
                drift: dict[str, SideDrift] = collections.defaultdict(SideDrift)
                wants = {pk: _expected(ts, inst) for pk, inst in good}
                for pk, want in wants.items():
                    self._classify(ts, pk, want, have, drift)
                self._rewrite(ts, wants, drift, uow)
        return len(good), diverged

    def _rewrite(
        self,
        ts: TableSpec,
        wants: Mapping[str, _Expected],
        drift: Mapping[str, SideDrift],
        uow: UnitOfWork,
    ) -> None:
        """Rewrite the drifted records' side rows (inside ``uow``, whose
        record-key locks are already held)."""
        layout = ts.search
        assert layout is not None
        for bm in layout.bm25.values():
            d = drift.get(bm.field)
            redo = sorted(set(d.missing + d.stale)) if d else []
            if not redo:
                continue
            self._run(
                f'DELETE FROM {bm.post} WHERE "_pk" = ANY(%s::text[])', [redo], uow=uow
            )
            self._run(
                f'DELETE FROM {bm.dl} WHERE "_pk" = ANY(%s::text[])', [redo], uow=uow
            )
            scopes, terms, pks, tfs = [], [], [], []
            dl_pks, dl_scopes, dl_lens = [], [], []
            for pk in redo:
                scope, counts, length = wants[pk].bm25[bm.field]
                if not counts:
                    continue
                for term in sorted(counts):
                    scopes.append(scope)
                    terms.append(term)
                    pks.append(pk)
                    tfs.append(counts[term])
                dl_pks.append(pk)
                dl_scopes.append(scope)
                dl_lens.append(length)
            if pks:
                self._run(
                    f'INSERT INTO {bm.post} ("scope", "term", "_pk", "tf") '
                    "SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], "
                    "%s::int[])",
                    [scopes, terms, pks, tfs],
                    uow=uow,
                )
                self._run(
                    f'INSERT INTO {bm.dl} ("_pk", "scope", "len") '
                    "SELECT * FROM unnest(%s::text[], %s::text[], %s::int[])",
                    [dl_pks, dl_scopes, dl_lens],
                    uow=uow,
                )
        for emb in layout.embedding.values():
            d = drift.get(emb.field)
            redo = sorted(set(d.missing + d.stale)) if d else []
            if not redo:
                continue
            self._run(
                f'DELETE FROM {emb.narrow} WHERE "_pk" = ANY(%s::text[])',
                [redo],
                uow=uow,
            )
            self._run(
                f'INSERT INTO {emb.narrow} ("_pk", "scope", "v") '
                f'SELECT t."_pk", u.s, t.{quote_ident(emb.vec)} FROM '
                f"unnest(%s::text[], %s::text[]) AS u(pk, s) JOIN {ts.qualified} t "
                f'ON t."_pk" = u.pk WHERE t.{quote_ident(emb.vec)} IS NOT NULL',
                [redo, [wants[pk].embedding[emb.field] for pk in redo]],
                uow=uow,
            )
        for mb in layout.membership.values():
            d = drift.get(mb.field)
            if mb.kind != "ExistenceFilter" or not d or not d.missing:
                continue
            toks, pks = [], []
            for pk in sorted(set(d.missing)):
                for token in sorted(wants[pk].tokens[mb.field]):
                    toks.append(token)
                    pks.append(pk)
            self._run(
                f'INSERT INTO {mb.table} ("token", "_pk") SELECT * FROM '
                "unnest(%s::text[], %s::text[]) ON CONFLICT DO NOTHING",
                [toks, pks],
                uow=uow,
            )

    def _reindex_analyze(
        self,
        ts: TableSpec,
        spec: ModelSpec,
        done: list[str],
        indexed: int,
        diverged: Sequence[str],
    ) -> None:
        """``REINDEX TABLE CONCURRENTLY`` (a B-tree's only drift is
        corruption; ``CONCURRENTLY`` keeps writes flowing) and ``ANALYZE``,
        on the record table and every companion, on a dedicated autocommit
        connection (``CONCURRENTLY`` cannot run in a transaction block) with
        a bounded ``lock_timeout`` and ``statement_timeout``
        (``Defaults.PG_MAINTAIN_*``).

        A statement that stops early -- a lock wait past the timeout (an
        idle-in-transaction session holding the table), a statement timeout,
        a cancel, a deadlock, any SQL error -- raises
        :class:`~popoto.backends.MaintenanceIncompleteError` naming what
        completed, after a best-effort drop of the INVALID indexes it left.
        It is not an outage: ``health`` is untouched and no write is counted
        as dropped (every side-row repair has committed). A lost connection
        or a server shutdown is still an outage."""
        tables = [ts.qualified] + _all_side_tables(ts, spec)
        step = "connect"
        try:
            with self._maintenance_connection(
                int(Defaults.PG_MAINTAIN_LOCK_TIMEOUT_MS)
            ) as conn:
                # Notice a client that went away (a cancelled task, a killed
                # process) between REINDEX's phases instead of finishing it.
                conn.execute("SET client_connection_check_interval = 1000")
                for table in tables:
                    step = f"reindex {table}"
                    conn.execute(f"REINDEX TABLE CONCURRENTLY {table}")
                    done.append(step)
                step = "analyze"
                conn.execute(f"ANALYZE {', '.join(tables)}")
                done.append(step)
        except Exception as exc:
            import psycopg

            if not isinstance(exc, psycopg.Error):
                raise
            if _is_outage(exc):
                raise self._fail(exc, write=False) from exc
            try:
                left = self._drop_invalid(ts, spec)
            except Exception:  # noqa: BLE001 - report the original failure
                left = []
            sqlstate = getattr(exc, "sqlstate", None) or "?"
            raise MaintenanceIncompleteError(
                f"{spec.name}.rebuild_indexes() stopped at {step!r} "
                f"(SQLSTATE {sqlstate}: {type(exc).__name__}: {exc}) -- not an "
                f"outage. Completed: {', '.join(done)}; {indexed} record(s) "
                "checked and every side-row repair committed. "
                + (
                    f"{len(left)} INVALID index(es) left by the failed REINDEX "
                    f"could not be dropped yet: {', '.join(left)}. "
                    if left
                    else ""
                )
                + "Rerun rebuild_indexes() once the blocking session has ended "
                "(see pg_stat_activity, state 'idle in transaction')",
                indexed=indexed,
                diverged_keys=diverged,
                completed=done,
                failed_step=step,
                invalid_indexes=left,
            ) from exc

    # -- field_call adapters ---------------------------------------------------

    def _maintain_field_call(
        self,
        spec: ModelSpec,
        field: str,
        op: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        uow: Optional[UnitOfWork],
    ) -> Any:
        """``raw_update``, and ``export_state`` / ``import_state`` for the
        carried fields whose state is columns of the record row; ``NOT_HANDLED``
        for anything else."""
        if field == MAINTAIN_FIELD and op == "raw_update":
            return self._raw_update(spec, *args, uow=uow, **kwargs)
        if op not in ("export_state", "import_state"):
            return NOT_HANDLED
        fs = spec.fields.get(field)
        kind = fs.kind if fs is not None else None
        export = op == "export_state"
        if kind == CONFIDENCE_KIND:
            if export:
                return self._confidence_export(spec, field, *args)
            return self._confidence_import(spec, field, *args, uow=uow)
        if kind == EMBEDDING_KIND:
            if export:
                return self._embedding_export(spec, field, *args)
            return self._embedding_import(spec, field, *args, uow=uow, **kwargs)
        if field == ACCESS_FIELD and ACCESS_MIXIN in spec.mixins:
            if export:
                return self._access_export(spec, *args)
            return self._access_import(spec, *args, uow=uow)
        return NOT_HANDLED

    def _raw_update(
        self,
        spec: ModelSpec,
        keys: Sequence[str],
        values: Mapping[str, Any],
        *,
        model: Any,
        batch_size: int = 1000,
        uow: Optional[UnitOfWork] = None,
    ) -> int:
        """``Model.raw_update``: write ``values`` into the record rows of
        ``keys`` with no hooks, ``auto_now`` or side-table work -- an
        ``UPDATE`` of those columns, as the Redis method is an ``HSET``. The
        companion rows are left as they were, so a raw update of a BM25 or
        embedding source leaves them stale until ``rebuild_indexes()``
        (Postgres B-trees, unlike Redis sorted sets, follow the row at once).
        Returns the rows updated: a key with no live row is not created (an
        ``HSET`` would create a partial hash; documented in §1.1)."""
        ts = self._table(spec, write=True)
        names = list(values)
        for name in names:
            if name not in ts.field_types:
                raise BackendCapabilityError(
                    f"{spec.name}.raw_update: {name!r} is not a stored column on "
                    "Postgres (unknown name, or a field with no value column)"
                )
        holder: Any = object.__new__(model)
        holder.__dict__.update(values)
        cols = self._row_values(ts, holder, names)
        assignments = ", ".join(f"{quote_ident(c)} = %s" for c in cols)
        live = live_sql(ts)
        keys = [k.decode() if isinstance(k, bytes) else str(k) for k in keys]
        size = max(1, int(batch_size))
        total = 0
        for start in range(0, len(keys), size):
            page = keys[start : start + size]
            sql, params = self._record_locked(
                ts,
                page,
                f"UPDATE {ts.qualified} SET {assignments}, "
                '"_updated_at" = now(), "_migrated_from" = NULL '
                'WHERE "_pk" = ANY(%s::text[])' + (f" AND {live}" if live else ""),
                list(cols.values()) + [page],
            )
            _, n = self._run(sql, params, uow=uow, write=True)
            total += int(n or 0)
        self._after_write(ts, uow)
        return total

    # ConfidenceField ---------------------------------------------------------

    def _confidence_export(
        self, spec: ModelSpec, field: str, id: RecordId
    ) -> Optional[dict[str, Any]]:
        """The four values ``ConfidenceField.export_state`` carries, or
        ``None`` with no live record (a saved record always has them, as a
        saved record on Redis always has its companion-hash entry). The
        read is ``.memory``'s ``state`` adapter (``NULL`` columns are the
        seed)."""
        # Looked up rather than declared: PostgresMemoryOps defines it, and
        # a Callable declaration here would clash with that definition.
        state: Optional[dict[str, Any]] = getattr(self, "_confidence_state")(
            spec, field, id
        )
        return state

    def _confidence_import(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        state: Mapping[str, Any],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        """Overwrite the four state columns with the carried values (Redis
        overwrites the companion-hash entry ``on_save`` seeded)."""
        ts = self._table(spec, write=True)
        ic = float(spec.fields[field].options.get("initial_confidence", 0.5))
        cols = [quote_ident(field + s) for s in CONF_SUFFIXES]
        values = [
            float(state.get("confidence", ic)),
            int(state.get("evidence_count", 0) or 0),
            int(state.get("corroborations", 0) or 0),
            int(state.get("contradictions", 0) or 0),
        ]
        live = live_sql(ts)
        sql, params = self._record_locked(
            ts,
            [id.canonical],
            f"UPDATE {ts.qualified} SET "
            + ", ".join(f"{c} = %s" for c in cols)
            + ' WHERE "_pk" = %s'
            + (f" AND {live}" if live else ""),
            values + [id.canonical],
        )
        self._run(sql, params, uow=uow, write=True)

    # EmbeddingField ----------------------------------------------------------

    def _embedding_layout(self, spec: ModelSpec, field: str) -> tuple[TableSpec, Any]:
        ts = self._table(spec)
        layout = ts.search
        if layout is None or field not in layout.embedding:
            raise BackendCapabilityError(
                f"{spec.name}.{field} is not an EmbeddingField"
            )
        return ts, layout.embedding[field]

    def _embedding_export(
        self, spec: ModelSpec, field: str, id: RecordId
    ) -> Optional[dict[str, Any]]:
        """``{"vector_npy_b64", "provenance"}`` -- the stored vector as the
        float32 ``.npy`` bytes the Redis backend keeps on disk, so one export
        shape serves both backends -- or ``None`` with no vector."""
        from ...fields.embedding_field import EmbeddingField

        ts, emb = self._embedding_layout(spec, field)
        live = live_sql(ts)
        rows, _ = self._run(
            f"SELECT {quote_ident(emb.vec)}::text FROM {ts.qualified} "
            'WHERE "_pk" = %s' + (f" AND {live}" if live else ""),
            [id.canonical],
        )
        if not rows or rows[0][0] is None:
            return None
        from .search import _parse_vector

        vector = _parse_vector(rows[0][0])
        return {
            "vector_npy_b64": base64.b64encode(npy_bytes(vector)).decode("ascii"),
            "provenance": EmbeddingField._provider_provenance(emb.field_ref),
        }

    def _embedding_import(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        state: Mapping[str, Any],
        *,
        instance: Any = None,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        """Write the carried vector into the record row (dimension count,
        vector, the identity of the model that made it) and its narrow
        vector row under the scope of ``instance`` (the record just saved,
        so the scope its save wrote), in one statement behind the record's
        key lock. The source-text hash the save wrote is kept."""
        encoded = state.get("vector_npy_b64")
        if not encoded:
            return
        ts, emb = self._embedding_layout(spec, field)
        layout = ts.search
        scope = scope_text(
            [getattr(instance, c, None) for c in layout.scope_columns]
            if instance is not None and layout is not None
            else []
        )
        vector = parse_npy(base64.b64decode(encoded))
        if emb.dims is not None and len(vector) != emb.dims:
            raise ValueError(
                f"{spec.name}.{field}: carried vector has {len(vector)} "
                f"dimensions; the destination column is vector({emb.dims})"
            )
        provenance = state.get("provenance") or {}
        if provenance:
            identity = f"{provenance.get('provider')}:{provenance.get('model')}"
        else:
            identity = _model_identity(emb.field_ref)
        literal = _vector_literal(vector)
        live = live_sql(ts)
        sql, params = self._record_locked(
            ts,
            [id.canonical],
            f'WITH "_u" AS (UPDATE {ts.qualified} SET {quote_ident(emb.field)} = %s, '
            f"{quote_ident(emb.vec)} = %s::vector, {quote_ident(emb.model)} = %s "
            'WHERE "_pk" = %s' + (f" AND {live}" if live else "") + ' RETURNING "_pk") '
            f'INSERT INTO {emb.narrow} ("_pk", "scope", "v") '
            'SELECT "_pk", %s, %s::vector FROM "_u" ON CONFLICT ("_pk") DO UPDATE '
            'SET "scope" = EXCLUDED."scope", "v" = EXCLUDED."v"',
            [len(vector), literal, identity, id.canonical, scope, literal],
        )
        self._run(sql, params, uow=uow, write=True)

    # AccessTrackerMixin ------------------------------------------------------

    def _access_export(self, spec: ModelSpec, id: RecordId) -> Optional[dict[str, Any]]:
        """``{"access_count", "last_accessed"}`` -- what Redis keeps in the
        meta hash -- or ``None`` for a record never confirmed-read. Postgres
        keeps no confirmed access log (§1.1), so none is exported; staged
        reads are never carried, on either backend."""
        ts = self._table(spec)
        live = live_sql(ts)
        rows, _ = self._run(
            f'SELECT "_access_count", "_last_accessed" FROM {ts.qualified} '
            'WHERE "_pk" = %s' + (f" AND {live}" if live else ""),
            [id.canonical],
        )
        if not rows:
            return None
        count, last = rows[0]
        state: dict[str, Any] = {}
        if count is not None:
            state["access_count"] = int(count)
        if last is not None:
            state["last_accessed"] = float(last)
        return state or None

    def _access_import(
        self,
        spec: ModelSpec,
        id: RecordId,
        state: Mapping[str, Any],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        """Write the carried counters (a carried ``access_log`` -- a Redis
        export's -- has no column here and is dropped, §1.1)."""
        sets: list[str] = []
        values: list[Any] = []
        if state.get("access_count") is not None:
            sets.append('"_access_count" = %s')
            values.append(int(state["access_count"]))
        if state.get("last_accessed") is not None:
            sets.append('"_last_accessed" = %s')
            values.append(float(state["last_accessed"]))
        if not sets:
            return
        ts = self._table(spec, write=True)
        live = live_sql(ts)
        sql, params = self._record_locked(
            ts,
            [id.canonical],
            f"UPDATE {ts.qualified} SET {', '.join(sets)} "
            'WHERE "_pk" = %s' + (f" AND {live}" if live else ""),
            values + [id.canonical],
        )
        self._run(sql, params, uow=uow, write=True)
