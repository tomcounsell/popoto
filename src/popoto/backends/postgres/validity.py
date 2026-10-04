"""Validity intervals and supersession on Postgres (#759 M3; plan §2 group D).

This module is the Postgres half of :class:`~popoto.fields.validity_field.
ValidityField` and :class:`~popoto.fields.supersession.SupersessionProtocol`:
the interval columns, ``supersede`` (``SUPERSEDE_LUA`` phase for phase),
``chain`` (``WITH RECURSIVE``), the open-claim pointers, the exclusion rule
every retrieval gate applies, and the ``field_call`` adapters the field layer
reads through. :class:`PostgresValidityOps` is mixed into
:class:`~popoto.backends.postgres.PostgresBackend` (through
:class:`~.memory.PostgresMemoryOps`).

Storage (plan §3, "Field → column type mapping"; a recorded departure)
---------------------------------------------------------------------
The plan proposed ``<f> tstzrange``. It cannot hold what the Redis indexes
hold, measured before this module was written (PostgreSQL 18.6):

* **Precision.** ``timestamptz`` keeps microseconds, so the epoch
  ``1700000000.1234567`` comes back ``1700000000.123457``: a close instant
  one ulp after an ``as_of`` reads as one at it, and the gate's bound is no
  longer bit-exact (``invalid_at <= as_of`` flips). The Redis score is the
  full ``double`` -- the M2a clock decision, for the same reason.
* **Partial intervals.** A member can sit in one index and not the other
  (an ``import_state`` shape, or ``filter(validity__current=False)``'s "every
  member of either index"). The exclusion rule treats an absent end as
  "never excludes" and ``+inf`` as "open", and the two differ at
  ``as_of = +inf`` (``+inf <= +inf`` closes an open member; an absent one is
  never closed). One range value has no spelling for "no upper bound
  recorded" beside "upper bound ``infinity``".

So the interval is three ``double precision`` columns beside the field's own
column, which keeps the declared value as the Redis hash does:

* ``<f>__valid_from``  -- the ``valid_from`` ZSET score
* ``<f>__invalid_at``  -- the ``invalid_at`` ZSET score (``'Infinity'`` = open)
* ``<f>__ingested_at`` -- the ``ingested_at`` ZSET score
* ``<f>__supersedes`` / ``<f>__superseded_by`` (``text``) -- the ``chain:rev``
  / ``chain:fwd`` HASH entries for this record

``NULL`` is "absent from that index". B-trees on the two gate columns serve
``filter(validity__as_of=…)``; a supersede rewrites them only on close.

The open-claim pointers are a companion table ``<table>__<f>__open (digest
text PRIMARY KEY, member text REFERENCES <table>(_pk) ON DELETE CASCADE)``
with an index on ``member``, not the plan's partial ``UNIQUE`` on an identity
column: a record can be the open claim of several identities at once, and an
``invalidate`` leaves the pointer naming the record it closed (the next
supersede on that identity reads it as "already closed"), neither of which a
per-row identity column can say. The cascade is ``on_delete``'s pointer
cleanup, matched on the exact key, so ``a`` never takes ``ab``'s pointer with
it (#750's ``drop_validity`` prefix over-match). The table is created on first
use, like ``popoto_recall_proposal``.

Lock order (plan §2 ``supersede``, §6 TD-2)
-------------------------------------------
Every ``supersede`` on one ``(model, field)`` takes
``pg_advisory_xact_lock(hashtext('popoto:validity:<schema>.<table>.<f>'))``
first -- Redis's single thread, per model and field -- and then locks the
rows it reads ``FOR UPDATE`` in ``_pk`` order (``COLLATE "C"``). Two
supersedes therefore never interleave: two pointer writers, two explicit
writers, a pointer and an explicit writer, and the crossing chains that
deadlocked the POC (#750 B1: ``d1 -> X`` superseded by ``Y`` while ``d2 ->
Y`` is superseded by ``X``) all run one after the other. A save does not take
the advisory lock; it meets a supersede on the row lock, and its upsert
re-reads the row it waited on, so it cannot reopen a record the supersede
closed (plan Race 2). What remains is a caller's own ``transaction()`` that
locks a row before a supersede on it: Postgres detects that cycle and the
loser raises :class:`~popoto.backends.BackendRetryableError` (in the caller's
transaction at once; an owned transaction retries first).

Never imports ``redis``.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ..types import BackendCapabilityError, ModelSpec, RecordId, UnitOfWork
from .schema import Column, TableSpec, quote_ident

__all__ = [
    "NOT_HANDLED",
    "VALIDITY_KIND",
    "VALIDITY_SUFFIXES",
    "PostgresValidityOps",
    "ensure_validity_tables",
    "excluded_sql",
    "included_sql",
    "refuse_valid_from_conflict",
    "save_parts",
    "validity_columns",
    "validity_cond_sql",
    "validity_field_names",
    "validity_indexes",
]

VALIDITY_KIND = "ValidityField"

VALID_FROM = "__valid_from"
INVALID_AT = "__invalid_at"
INGESTED_AT = "__ingested_at"
SUPERSEDES = "__supersedes"
SUPERSEDED_BY = "__superseded_by"

#: State columns beside a ``ValidityField``'s own column, in DDL order.
VALIDITY_SUFFIXES: tuple[tuple[str, str], ...] = (
    (VALID_FROM, "double precision"),
    (INVALID_AT, "double precision"),
    (INGESTED_AT, "double precision"),
    (SUPERSEDES, "text"),
    (SUPERSEDED_BY, "text"),
)

INF = float("inf")

NOT_HANDLED = object()
"""What :meth:`PostgresValidityOps._validity_field_call` returns for an op
it does not register."""


# -- helpers --------------------------------------------------------------------


def _sort_key(key: str) -> bytes:
    """``COLLATE "C"`` order: the UTF-8 bytes."""
    return key.encode("utf-8", "surrogateescape")


def _lit(value: float) -> str:
    """An exact ``double precision`` literal (``repr`` round-trips)."""
    value = float(value)
    if math.isnan(value):
        return "'NaN'::float8"
    if math.isinf(value):
        return "'Infinity'::float8" if value > 0 else "'-Infinity'::float8"
    return f"({value!r}::float8)"


def _col(field: str, suffix: str, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return prefix + quote_ident(field + suffix)


def _lua_number(value: float) -> str:
    """``value`` as Lua 5.1's ``tostring`` prints it (``%.14g``): the text
    ``SUPERSEDE_LUA`` puts in its ``VALID_FROM_CONFLICT`` reply."""
    return "%.14g" % float(value)


def validity_field_names(spec: ModelSpec) -> list[str]:
    """The model's ``ValidityField`` names, in declaration-sorted order."""
    return sorted(n for n, fs in spec.fields.items() if fs.kind == VALIDITY_KIND)


class _Reply(Exception):
    """A ``SUPERSEDE_LUA``-shaped error reply: ``str()`` is the token line, so
    :func:`~popoto.fields.validity_field.map_lua_error` builds the same typed
    exception, with the same text, as it does from Redis's ``ResponseError``."""


def _refuse(line: str) -> BaseException:
    from ...fields.validity_field import map_lua_error

    return map_lua_error(_Reply(line))


# -- schema ---------------------------------------------------------------------


def validity_columns(spec: ModelSpec) -> list[Column]:
    """The interval and chain-link columns for each ``ValidityField``."""
    out: list[Column] = []
    for name in validity_field_names(spec):
        for suffix, sql_type in VALIDITY_SUFFIXES:
            out.append(Column(name + suffix, sql_type, name, "state"))
    return out


def validity_indexes(
    spec: ModelSpec, index_name: Callable[..., str]
) -> list[tuple[str, str, bool]]:
    """A B-tree on each gate column: ``filter(validity__as_of=…)`` and the
    exclusion reads (``invalid_at <= t``, ``valid_from > t``) are range
    scans on them."""
    out = []
    for name in validity_field_names(spec):
        for suffix in (VALID_FROM, INVALID_AT):
            col = name + suffix
            out.append((index_name(col), f"({quote_ident(col)})", False))
    return out


def pointer_table(ts: TableSpec, field: str) -> str:
    """``<schema>.<table>__<f>__open``: the open-claim pointers (digest ->
    member) of one ``ValidityField``."""
    from .schema import _bounded

    name = _bounded(f"{ts.table}__{field}__open")
    return f"{quote_ident(ts.schema)}.{quote_ident(name)}"


def ensure_validity_tables(conn: Any, ts: TableSpec, spec: ModelSpec) -> None:
    """Create each ``ValidityField``'s pointer table if it is missing, on
    ``conn`` and in its own transaction, under the DDL advisory lock: run at
    the model's first use, right after its table is created or checked, so
    no supersede ever has to create one inside a transaction that already
    holds the model table's locks. ``CREATE … IF NOT EXISTS`` is idempotent,
    so a process that finds the table does nothing."""
    from . import _schema_auto
    from .schema import _bounded

    for field in validity_field_names(spec):
        qualified = pointer_table(ts, field)
        name = _bounded(f"{ts.table}__{field}__open")
        with conn.transaction():
            cur = conn.cursor()
            exists = cur.execute(
                "SELECT to_regclass(%s) IS NOT NULL", (qualified,)
            ).fetchone()[0]
            if exists:
                continue
            if not _schema_auto():
                from ..types import SchemaDriftError

                raise SchemaDriftError(
                    f"{qualified} does not exist and POPOTO_SCHEMA_AUTO=0"
                )
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"popoto:ddl:{ts.schema}.{ts.table}",),
            )
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {qualified} ("
                "digest text PRIMARY KEY, member text NOT NULL "
                f'REFERENCES {ts.qualified} ("_pk") ON DELETE CASCADE)'
            )
            index = quote_ident(_bounded(f"{name}__member"))
            cur.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {qualified} (member)")


# -- the exclusion rule -----------------------------------------------------------


def excluded_sql(field: str, as_of: float, alias: str = "t") -> str:
    """``invalid_at <= as_of OR valid_from > as_of``: the rule every gate
    applies (``DECAY_SCORE_LUA``'s, the composite mask's,
    ``resolve_excluded_keys``'). An absent end (``NULL``) never excludes; an
    open record (``+inf``) is closed only at ``as_of = +inf``."""
    a = _lit(as_of)
    ia = _col(field, INVALID_AT, alias)
    vf = _col(field, VALID_FROM, alias)
    return f"(coalesce({ia} <= {a}, false) OR coalesce({vf} > {a}, false))"


def included_sql(field: str, as_of: Optional[float], alias: str = "t") -> str:
    """The gate as a ``WHERE`` term: ``TRUE`` when there is no gate (no
    as-of, or a NaN one -- which excludes nothing in the Lua, where every
    comparison with NaN is false), else ``NOT`` the exclusion rule."""
    if as_of is None or math.isnan(float(as_of)):
        return "TRUE"
    return f"NOT {excluded_sql(field, float(as_of), alias)}"


def range_bound(as_of: Any) -> float:
    """``as_of`` as a range-read bound, refused as Redis refuses it: a NaN
    bound makes ``ZRANGEBYSCORE``/``ZRANGESTORE`` reply ``min or max is not
    a float``, so the reads that are range reads on Redis (the filters, the
    resolvers, the composite mask) raise ``QueryException`` with that text
    here -- the same text, a different class (M1's divergence (v)). The decay
    ranking's gate is a Lua comparison instead, and a NaN there excludes
    nothing on both (:func:`included_sql`)."""
    t = float(as_of)
    if math.isnan(t):
        from ...models.query import QueryException

        raise QueryException("min or max is not a float")
    return t


def validity_cond_sql(field: str, value: Any) -> str:
    """``filter(validity__as_of=t)`` / ``validity__current=…``, compiled by
    :mod:`popoto.backends.planning` to ``Cond(field, VALID_AT, (t, valid))``.

    ``valid=True``: ``valid_from <= t AND invalid_at > t`` -- both ends
    present, the two ``ZRANGEBYSCORE``s ``ValidityField._members_valid_at``
    intersects. ``valid=False`` (``__current=False``): the members of either
    index that are not valid at ``t``, the Redis complement."""
    t, valid = value
    a = _lit(range_bound(t))
    vf = _col(field, VALID_FROM)
    ia = _col(field, INVALID_AT)
    is_valid = f"({vf} <= {a} AND {ia} > {a})"
    if valid:
        return is_valid
    return (
        f"(({vf} IS NOT NULL OR {ia} IS NOT NULL) AND NOT coalesce({is_valid}, false))"
    )


# -- save -------------------------------------------------------------------------


def save_parts(
    ts: TableSpec, spec: ModelSpec, obj: Any, names: Sequence[str]
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """What ``ValidityField.on_save`` does on Redis, as parts of the save
    upsert: ``SUPERSEDE_LUA`` in mode ``'open'``.

    Returns ``(insert columns, ON CONFLICT overrides, DO UPDATE guards)``.
    A new row opens at the declared ``valid_from`` (else the save clock),
    ingested now, ``invalid_at = +inf``. An existing row keeps its interval
    (``NX``): an absent end is filled, and a closed record is never reopened
    -- the guard is the row the upsert re-read after waiting on any
    concurrent supersede (plan Race 2). A *declared* ``valid_from`` that
    disagrees with the stored start fails the guard: nothing is written and
    the caller raises ``ValidityValidFromConflictError``, the script's
    ``ARGV[8]`` check.
    """
    cols: dict[str, Any] = {}
    overrides: dict[str, str] = {}
    guards: list[str] = []
    fields = [n for n in validity_field_names(spec) if n in names]
    if not fields:
        return cols, overrides, guards
    now = time.time()
    for name in fields:
        declared = getattr(obj, name, None)
        valid_from = now
        asserted = False
        if declared is not None:
            try:
                valid_from = float(declared)
                asserted = True
            except (TypeError, ValueError):
                valid_from = now
        vf, ia, ig = name + VALID_FROM, name + INVALID_AT, name + INGESTED_AT
        cols[vf] = valid_from
        cols[ig] = now
        cols[ia] = INF
        t = quote_ident(ts.table)
        qvf, qia, qig = quote_ident(vf), quote_ident(ia), quote_ident(ig)
        is_open = f"({t}.{qia} IS NULL OR {t}.{qia} = 'Infinity'::float8)"
        overrides[vf] = (
            f"CASE WHEN {is_open} THEN coalesce({t}.{qvf}, EXCLUDED.{qvf}) "
            f"ELSE {t}.{qvf} END"
        )
        overrides[ig] = (
            f"CASE WHEN {is_open} THEN coalesce({t}.{qig}, EXCLUDED.{qig}) "
            f"ELSE {t}.{qig} END"
        )
        overrides[ia] = f"coalesce({t}.{qia}, 'Infinity'::float8)"
        if asserted:
            guards.append(f"({t}.{qvf} IS NULL OR {t}.{qvf} = EXCLUDED.{qvf})")
    return cols, overrides, guards


def refuse_valid_from_conflict(
    backend: Any, spec: ModelSpec, obj: Any, *, uow: Optional[UnitOfWork] = None
) -> None:
    """Raise the ``VALID_FROM_CONFLICT`` error for a save whose upsert guard
    refused it (:func:`save_parts`), with the numbers the script's reply
    carries: the stored start, then the declared one, each as Lua's
    ``tostring`` prints it."""
    from ...fields.validity_field import VALID_FROM_CONFLICT_ERROR

    key = obj.db_key.redis_key
    for name in validity_field_names(spec):
        declared = getattr(obj, name, None)
        try:
            requested = float(declared)
        except (TypeError, ValueError):
            continue
        rows, _ = backend._run(
            f"SELECT {_col(name, VALID_FROM)} FROM "
            f'{backend._table(spec).qualified} WHERE "_pk" = %s',
            [key],
            uow=uow,
        )
        stored = rows[0][0] if rows else None
        if stored is not None and stored != requested:
            raise _refuse(
                f"{VALID_FROM_CONFLICT_ERROR} {_lua_number(stored)} "
                f"{_lua_number(requested)}"
            )
    raise _refuse(f"{VALID_FROM_CONFLICT_ERROR} {key}")  # pragma: no cover


# -- the backend half ------------------------------------------------------------


class PostgresValidityOps:
    """``supersede``, ``chain`` and the validity ``field_call`` adapters for
    :class:`~popoto.backends.postgres.PostgresBackend`. Relies on the
    backend's ``_table``, ``_run`` and ``_atomically``."""

    # Provided by PostgresBackend / PostgresMemoryOps.
    schema: str
    _table: Callable[..., TableSpec]
    _run: Callable[..., Any]
    _atomically: Callable[..., Any]

    # -- plumbing ---------------------------------------------------------------

    def _validity_field(self, spec: ModelSpec, field: str) -> None:
        fs = spec.fields.get(field)
        if fs is None or fs.kind != VALIDITY_KIND:
            raise BackendCapabilityError(f"{spec.name}.{field} is not a ValidityField")

    def _validity_lock(
        self, ts: TableSpec, fields: Iterable[str], uow: Optional[UnitOfWork]
    ) -> None:
        """The ``(model, field)`` advisory locks, in name order: the first
        lock any supersede on the field takes."""
        for field in sorted(fields):
            self._run(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                [f"popoto:validity:{ts.schema}.{ts.table}.{field}"],
                uow=uow,
                write=True,
            )

    # -- D. supersede -------------------------------------------------------------

    def supersede(
        self,
        spec: ModelSpec,
        field: str,
        *,
        successor: Optional[RecordId],
        incumbent: Optional[RecordId],
        identity: Optional[str],
        mode: str,
        valid_from: Optional[float],
        invalid_at: Optional[float],
        now: float,
        uow: Optional[UnitOfWork] = None,
        ingested_at: Optional[float] = None,
        assert_valid_from: bool = False,
        **options: Any,
    ) -> Optional[str]:
        """``SUPERSEDE_LUA``, phase for phase, in one transaction.

        ``mode`` is ``'open'`` (open ``successor``; close nothing),
        ``'supersede'`` (close the incumbent -- ``incumbent``, else the record
        ``identity``'s pointer names -- chain it to ``successor`` and open
        that) or ``'invalidate'`` (as ``'supersede'``; without a successor
        nothing is chained or opened). ``invalid_at`` is the close instant,
        and every instant the caller leaves out is ``now``.

        Phases, after the ``(model, field)`` lock and the ``_pk``-ordered row
        locks: **validation** (reads and refusals only) -- a successor that
        does not exist, an *asserted* incumbent that does not exist (a
        pointer-resolved one that does not is "no incumbent"), a close
        before the incumbent's own start, an asserted ``valid_from`` that
        disagrees with the stored one -- raises the typed error the script's
        reply maps to, with the same text, having written nothing; then
        **mutation** -- close (only an open incumbent: closing is
        idempotent), both chain links, the ``NX`` open of an open successor,
        and the pointer repoint. Returns the closed record's key, or ``None``.

        A record that does not exist holds no interval here (the interval is
        its row), so mode ``'open'`` on an absent ``successor`` writes
        nothing, where the script's ``ZADD NX`` would index a member with no
        record (only a direct ``execute_supersede`` call can ask for that).
        """
        from ...fields.validity_field import (
            CLOSE_BEFORE_START_ERROR,
            MEMBER_ABSENT_ERROR,
            VALID_FROM_CONFLICT_ERROR,
            VALID_MODES,
        )

        self._validity_field(spec, field)
        if mode not in VALID_MODES:
            raise ValueError(
                f"ValidityField mode must be one of {sorted(VALID_MODES)}, got {mode!r}"
            )
        clock = float(now)
        start = clock if valid_from is None else float(valid_from)
        ingest = clock if ingested_at is None else float(ingested_at)
        close_at = clock if invalid_at is None else float(invalid_at)
        for label, value in (
            ("now", clock),
            ("valid_from", start),
            ("ingested_at", ingest),
            ("close_at", close_at),
        ):
            if math.isnan(value):
                raise ValueError(
                    f"ValidityField: {label} is NaN, which no interval can hold"
                )
        new = successor.canonical if successor is not None else ""
        named_old = incumbent.canonical if incumbent is not None else ""
        ts = self._table(spec, write=True)
        vf, ia, ig = (_col(field, s) for s in (VALID_FROM, INVALID_AT, INGESTED_AT))
        sup, supby = _col(field, SUPERSEDES), _col(field, SUPERSEDED_BY)

        def work(tx: UnitOfWork) -> Optional[str]:
            self._validity_lock(ts, [field], tx)
            old = named_old
            if mode != "open" and not old and identity:
                old = self._pointer_get(ts, field, identity, uow=tx) or ""
            keys = sorted({k for k in (new, old) if k}, key=_sort_key)
            rows: dict[str, tuple[Any, ...]] = {}
            if keys:
                found, _ = self._run(
                    f'SELECT "_pk", {vf}, {ia}, {ig}, {sup}, {supby} '
                    f'FROM {ts.qualified} WHERE "_pk" = ANY(%s::text[]) '
                    'ORDER BY "_pk" COLLATE "C" FOR UPDATE',
                    [keys],
                    uow=tx,
                    write=True,
                )
                rows = {row[0]: tuple(row[1:]) for row in found}

            # -- VALIDATION PHASE: reads and refusals only --------------------
            will_close = False
            if mode != "open":
                if new and new not in rows:
                    raise _refuse(f"{MEMBER_ABSENT_ERROR} successor {new}")
                if old and old not in rows:
                    if named_old:
                        raise _refuse(f"{MEMBER_ABSENT_ERROR} incumbent {old}")
                    old = ""  # a pointer naming a deleted record: a hint
                if old and old != new:
                    old_start, old_close = rows[old][0], rows[old][1]
                    if old_close is not None and old_close == INF:
                        if old_start is not None and close_at < old_start:
                            raise _refuse(CLOSE_BEFORE_START_ERROR)
                        will_close = True
            if new and assert_valid_from and new in rows:
                stored = rows[new][0]
                if stored is not None and stored != start:
                    raise _refuse(
                        f"{VALID_FROM_CONFLICT_ERROR} {_lua_number(stored)} "
                        f"{_lua_number(start)}"
                    )

            # -- MUTATION PHASE: every check above has passed -----------------
            updates: dict[str, dict[str, Any]] = {}
            if will_close:
                updates[old] = {"ia": close_at}
                if new:
                    updates[old]["supby"] = new
                    updates.setdefault(new, {})["sup"] = old
            new_open = False
            if new and new in rows:
                state = rows[new]
                if state[1] is None or state[1] == INF:
                    new_open = True
                    target = updates.setdefault(new, {})
                    target["vf"] = state[0] if state[0] is not None else start
                    target["ig"] = state[2] if state[2] is not None else ingest
                    target["ia"] = INF if state[1] is None else state[1]
            if updates:
                self._write_states(ts, field, rows, updates, tx)
            if new_open and identity:
                self._run(
                    f"INSERT INTO {pointer_table(ts, field)} (digest, member) "
                    "VALUES (%s, %s) ON CONFLICT (digest) DO UPDATE "
                    "SET member = EXCLUDED.member",
                    [identity, new],
                    uow=tx,
                    write=True,
                )
            return old if will_close else None

        return self._atomically(work, uow=uow)

    def _write_states(
        self,
        ts: TableSpec,
        field: str,
        rows: Mapping[str, tuple[Any, ...]],
        updates: Mapping[str, Mapping[str, Any]],
        uow: UnitOfWork,
    ) -> None:
        """One ``UPDATE … FROM (VALUES …)`` with each locked row's final
        state; columns a row does not change keep the value read under its
        lock."""
        order = ("vf", "ia", "ig", "sup", "supby")
        values = []
        params: list[Any] = []
        for pk in sorted(updates, key=_sort_key):
            current = dict(zip(order, rows[pk]))
            current.update(updates[pk])
            values.append(
                "(%s::text, %s::float8, %s::float8, %s::float8, %s::text, %s::text)"
            )
            params.extend([pk] + [current[k] for k in order])
        cols = {
            "vf": quote_ident(field + VALID_FROM),
            "ia": quote_ident(field + INVALID_AT),
            "ig": quote_ident(field + INGESTED_AT),
            "sup": quote_ident(field + SUPERSEDES),
            "supby": quote_ident(field + SUPERSEDED_BY),
        }
        sets = ", ".join(f"{cols[k]} = v.{k}" for k in order)
        self._run(
            f'UPDATE {ts.qualified} AS t SET {sets}, "_updated_at" = now() '
            f"FROM (VALUES {', '.join(values)}) AS v(pk, {', '.join(order)}) "
            f'WHERE t."_pk" = v.pk',
            params,
            uow=uow,
            write=True,
        )

    def _pointer_get(
        self,
        ts: TableSpec,
        field: str,
        digest: str,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[str]:
        rows, _ = self._run(
            f"SELECT member FROM {pointer_table(ts, field)} WHERE digest = %s",
            [digest],
            uow=uow,
        )
        return rows[0][0] if rows else None

    # -- D. chain -----------------------------------------------------------------

    def chain(self, spec: ModelSpec, field: str, id: RecordId) -> list[RecordId]:
        """The supersession chain through ``id``, oldest first, in one
        ``WITH RECURSIVE``: back along ``supersedes``, then forward along
        ``superseded_by``. ``[]`` when ``id`` has no ``valid_from`` (unsaved,
        or never opened). A walk stops at a missing link, at a record it has
        already visited (a cycle -- the forward walk counts the backward
        walk's records as visited, as the Redis walk's shared ``seen`` set
        does), and at a link naming a record with no ``valid_from`` (a hard
        delete leaves the neighbour's link naming it)."""
        self._validity_field(spec, field)
        ts = self._table(spec)
        vf = _col(field, VALID_FROM, "n")
        sup = _col(field, SUPERSEDES, "s")
        supby = _col(field, SUPERSEDED_BY, "s")
        anchor_vf = _col(field, VALID_FROM)
        t = ts.qualified
        sql = (
            "WITH RECURSIVE "
            f'anchor AS (SELECT "_pk" AS pk FROM {t} WHERE "_pk" = %s '
            f"AND {anchor_vf} IS NOT NULL), "
            "older(pk, depth, path) AS ("
            f'SELECT n."_pk", 1, ARRAY[a.pk, n."_pk"] FROM anchor a '
            f'JOIN {t} s ON s."_pk" = a.pk JOIN {t} n ON n."_pk" = {sup} '
            f'WHERE {vf} IS NOT NULL AND n."_pk" <> a.pk '
            f'UNION ALL SELECT n."_pk", o.depth + 1, o.path || n."_pk" '
            f'FROM older o JOIN {t} s ON s."_pk" = o.pk '
            f'JOIN {t} n ON n."_pk" = {sup} '
            f'WHERE {vf} IS NOT NULL AND n."_pk" <> ALL(o.path)), '
            "seen AS (SELECT a.pk AS pk FROM anchor a UNION ALL "
            "SELECT pk FROM older), "
            "newer(pk, depth, path) AS ("
            f'SELECT n."_pk", 1, '
            '(SELECT array_agg(pk) FROM seen) || n."_pk" FROM anchor a '
            f'JOIN {t} s ON s."_pk" = a.pk JOIN {t} n ON n."_pk" = {supby} '
            f'WHERE {vf} IS NOT NULL AND n."_pk" <> ALL(SELECT pk FROM seen) '
            f'UNION ALL SELECT n."_pk", w.depth + 1, w.path || n."_pk" '
            f'FROM newer w JOIN {t} s ON s."_pk" = w.pk '
            f'JOIN {t} n ON n."_pk" = {supby} '
            f'WHERE {vf} IS NOT NULL AND n."_pk" <> ALL(w.path)) '
            "SELECT pk, -depth FROM older UNION ALL SELECT pk, 0 FROM anchor "
            "UNION ALL SELECT pk, depth FROM newer ORDER BY 2"
        )
        rows, _ = self._run(sql, [id.canonical])
        return [RecordId(spec.name, (), pk, native=pk) for pk, _depth in rows]

    # -- field_call adapters ---------------------------------------------------

    def _validity_field_call(
        self,
        spec: ModelSpec,
        field: str,
        op: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        uow: Optional[UnitOfWork],
    ) -> Any:
        """The ``ValidityField`` adapters; :data:`NOT_HANDLED` when ``op`` is
        not one of them."""
        handlers: dict[str, Callable[..., Any]] = {
            "interval": self._interval_of,
            "members": self._interval_members,
            "pointers": self._pointers_naming,
            "pointer": self._pointer_of,
            "export": self._export_state,
            "import": self._import_state,
            "dump": self._dump_state,
        }
        handler = handlers.get(op)
        if handler is None:
            return NOT_HANDLED
        self._validity_field(spec, field)
        return handler(spec, field, *args, uow=uow, **kwargs)

    def _interval_of(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[dict[str, Any]]:
        """The record's ``valid_from``/``invalid_at``/``ingested_at`` and
        chain links (``None`` for a record that does not exist): the five
        ZSET scores and HASH entries Redis keeps for the member."""
        ts = self._table(spec)
        cols = ", ".join(_col(field, s) for s, _t in VALIDITY_SUFFIXES)
        rows, _ = self._run(
            f'SELECT {cols} FROM {ts.qualified} WHERE "_pk" = %s',
            [id.canonical],
            uow=uow,
        )
        if not rows:
            return None
        valid_from, invalid_at, ingested_at, supersedes, superseded_by = rows[0]
        return {
            "valid_from": valid_from,
            "invalid_at": invalid_at,
            "ingested_at": ingested_at,
            "supersedes": supersedes,
            "superseded_by": superseded_by,
        }

    def _dump_state(
        self, spec: ModelSpec, field: str, *, uow: Optional[UnitOfWork] = None
    ) -> dict[str, list[tuple[str, Any]]]:
        """Every interval score, chain link and pointer of the field, as the
        six Redis keys would list them: ``{index: sorted [(member, value)]}``
        for ``valid_from``, ``invalid_at``, ``ingested_at``, ``chain_fwd``
        and ``chain_rev``, and ``pointers`` as ``[(digest, member)]``. For
        tests and the parity probe (an admin read: it scans the table)."""
        ts = self._table(spec)
        cols = ", ".join(_col(field, s) for s, _t in VALIDITY_SUFFIXES)
        rows, _ = self._run(f'SELECT "_pk", {cols} FROM {ts.qualified}', [], uow=uow)
        out: dict[str, list[tuple[str, Any]]] = {
            "valid_from": [],
            "invalid_at": [],
            "ingested_at": [],
            "chain_fwd": [],
            "chain_rev": [],
        }
        for pk, vf, ia, ig, sup, supby in rows:
            for name, value in (
                ("valid_from", vf),
                ("invalid_at", ia),
                ("ingested_at", ig),
                ("chain_fwd", supby),
                ("chain_rev", sup),
            ):
                if value is not None:
                    out[name].append((pk, value))
        pointers, _ = self._run(
            f"SELECT digest, member FROM {pointer_table(ts, field)}", [], uow=uow
        )
        out["pointers"] = [(digest, member) for digest, member in pointers]
        return {name: sorted(items) for name, items in out.items()}

    def _interval_members(
        self,
        spec: ModelSpec,
        field: str,
        as_of: float,
        *,
        select: str,
        uow: Optional[UnitOfWork] = None,
    ) -> set[str]:
        """``resolve_valid_keys`` (``select="valid"``: ``valid_from <= t AND
        invalid_at > t``, both present) or ``resolve_excluded_keys``
        (``select="excluded"``: the exclusion rule)."""
        ts = self._table(spec)
        t = range_bound(as_of)
        if select == "valid":
            clause = validity_cond_sql(field, (t, True))
        elif select == "excluded":
            clause = excluded_sql(field, t, alias="")
        else:
            raise ValueError(f"select must be 'valid' or 'excluded', got {select!r}")
        rows, _ = self._run(
            f'SELECT "_pk" FROM {ts.qualified} WHERE {clause}', [], uow=uow
        )
        return {row[0] for row in rows}

    def _pointers_naming(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> list[str]:
        """The identity digests whose open-claim pointer names ``id``."""
        ts = self._table(spec)
        rows, _ = self._run(
            f"SELECT digest FROM {pointer_table(ts, field)} WHERE member = %s "
            'ORDER BY digest COLLATE "C"',
            [id.canonical],
            uow=uow,
        )
        return [row[0] for row in rows]

    def _pointer_of(
        self,
        spec: ModelSpec,
        field: str,
        digest: str,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[str]:
        """The record ``digest``'s open-claim pointer names, or ``None``."""
        return self._pointer_get(self._table(spec), field, digest, uow=uow)

    def _export_state(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[dict[str, Any]]:
        """``ValidityField.export_state``'s dict, from the row."""
        state = self._interval_of(spec, field, id, uow=uow)
        if state is None or (
            state["valid_from"] is None
            and state["invalid_at"] is None
            and state["ingested_at"] is None
        ):
            return None
        invalid_at = state["invalid_at"]
        return {
            "valid_from": state["valid_from"],
            "invalid_at": (
                "+inf" if invalid_at is None or invalid_at == INF else invalid_at
            ),
            "ingested_at": state["ingested_at"],
            "chain_fwd": state["superseded_by"],
            "chain_rev": state["supersedes"],
            "open_pointers": self._pointers_naming(spec, field, id, uow=uow),
        }

    def _import_state(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        state: Mapping[str, Any],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        """``ValidityField.import_state``: overwrite (never ``NX``) the
        carried scores and links, and repoint each carried identity."""
        ts = self._table(spec, write=True)
        sets: list[str] = []
        params: list[Any] = []
        for key, suffix in (
            ("valid_from", VALID_FROM),
            ("invalid_at", INVALID_AT),
            ("ingested_at", INGESTED_AT),
        ):
            value = state.get(key)
            if value is None:
                continue
            if key == "invalid_at" and value == "+inf":
                value = INF
            sets.append(f"{_col(field, suffix)} = %s")
            params.append(float(value))
        for key, suffix in (("chain_fwd", SUPERSEDED_BY), ("chain_rev", SUPERSEDES)):
            if state.get(key):
                sets.append(f"{_col(field, suffix)} = %s")
                params.append(str(state[key]))
        if sets:
            self._run(
                f"UPDATE {ts.qualified} SET {', '.join(sets)}, "
                '"_updated_at" = now() WHERE "_pk" = %s',
                params + [id.canonical],
                uow=uow,
                write=True,
            )
        digests = [str(d) for d in state.get("open_pointers") or []]
        if digests:
            self._run(
                f"INSERT INTO {pointer_table(ts, field)} (digest, member) "
                "SELECT d, %s FROM unnest(%s::text[]) AS d "
                "ON CONFLICT (digest) DO UPDATE SET member = EXCLUDED.member",
                [id.canonical, digests],
                uow=uow,
                write=True,
            )
