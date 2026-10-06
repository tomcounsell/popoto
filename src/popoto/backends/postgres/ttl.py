"""Record expiry on Postgres: ``Meta.ttl`` (#759 M5, #755 Q3).

On Redis a TTL is ``EXPIRE``/``EXPIREAT`` on the record's hash: the server
drops the hash when it expires, and the index entries that pointed at it stay
behind until a hydrating read purges them (``Model._purge_orphan_keys``) or
``clean_indexes`` runs. Postgres has no per-row expiry, so a model that
declares ``Meta.ttl`` gets three things here, and a model that does not gets
none of them -- its table, its plans and its writes are exactly what they
were (plan §3, "``_expires_at`` only with ``Meta.ttl``"):

* **A column.** ``_expires_at double precision`` (epoch seconds, ``NULL`` =
  never), the clock decision M2a made for every engine clock, with a partial
  B-tree on the rows that carry one. A save writes it from the instance's
  ``_ttl`` (``now + ttl``, server clock) or ``_expire_at``
  (``int(_expire_at.timestamp())``, the value Redis's ``EXPIREAT`` gets), and
  leaves it alone when the instance carries neither -- as ``HSET`` leaves a
  key's TTL alone.
* **A read filter.** Every read of the model -- ``load``, ``select``,
  ``count``, ``exists``, ranking, search, ``recall``, the assembler's arms,
  membership, validity -- sees a row only while ``_expires_at`` is ``NULL``
  or later than *now*. An expired row is gone to every reader the instant it
  expires, whether or not anything has deleted it yet.
* **An automatic reaper.** After a record write on the model commits, at most
  ``Defaults.PG_REAPER_BATCH`` expired rows are deleted in a transaction of
  their own (their side rows cascade), at most once per
  ``Defaults.PG_REAPER_INTERVAL_SECONDS`` per table and process unless the
  last run found a backlog. No cron, no CLI, no manual job.

*Now* is the server's ``statement_timestamp()``: one central database serves
every agent and machine (architect decision 1), so expiry is judged by one
clock, as Redis judges it by its own. :func:`frozen_clock` pins it to a value
for tests and the parity probe (the only controllable clock; it changes the
SQL's clock term, nothing else).
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import threading
import time
from typing import Any, Iterator, Optional, Sequence

from ...fields.constants import Defaults
from ..types import BackendCapabilityError, Expiry, ModelSpec
from .schema import Column, TableSpec, quote_ident

__all__ = [
    "EXPIRES_COL",
    "and_live",
    "clock",
    "expired_pks_sql",
    "expiry_sql",
    "frozen_clock",
    "live_sql",
    "now_sql",
    "reap",
    "ttl_columns",
    "ttl_indexes",
    "ttl_remaining",
]

logger = logging.getLogger("POPOTO.postgres")

EXPIRES_COL = "_expires_at"
"""The engine column holding a row's expiry instant (epoch seconds)."""

# -- the clock ---------------------------------------------------------------

_override: Optional[float] = None
_override_lock = threading.Lock()


def now_sql() -> str:
    """The SQL term for *now*: the server's statement timestamp in epoch
    seconds, or the frozen instant while :func:`frozen_clock` is active.
    ``statement_timestamp()`` is fixed for a statement (so a read and the
    expiry it compares against agree within it) and, unlike ``now()``,
    advances between the statements of one long ``transaction()``."""
    pinned = _override
    if pinned is None:
        return "extract(epoch from statement_timestamp())::float8"
    return f"{float(pinned)!r}::float8"


def clock() -> float:
    """*Now* on the Python side (the reaper's schedule): the frozen instant,
    else ``time.time()``."""
    pinned = _override
    return time.time() if pinned is None else float(pinned)


@contextlib.contextmanager
def frozen_clock(at: float) -> Iterator[None]:
    """Pin *now* to ``at`` (epoch seconds) for every TTL decision -- writes
    computing ``now + ttl``, every read filter, the reaper and its schedule --
    for the duration of the block. Process-wide; for tests and the parity
    probe. Nesting restores the outer value."""
    global _override
    with _override_lock:
        previous = _override
        _override = float(at)
    try:
        yield
    finally:
        with _override_lock:
            _override = previous


# -- DDL ----------------------------------------------------------------------


def ttl_columns(spec: ModelSpec) -> list[Column]:
    """``_expires_at`` for a ``Meta.ttl`` model, nothing otherwise. Role
    ``aux``: an engine column, never hydrated into a field."""
    if spec.ttl is None:
        return []
    return [Column(EXPIRES_COL, "double precision", None, role="aux")]


def ttl_indexes(spec: ModelSpec, index_name: Any) -> list[tuple[str, str, bool]]:
    """A partial B-tree on ``_expires_at`` over the rows that carry one: the
    reaper's ``<= now`` probe and the expired-key anti-join of the side
    tables both read it, and a row that never expires costs it nothing."""
    if spec.ttl is None:
        return []
    col = quote_ident(EXPIRES_COL)
    return [(index_name("expires"), f"({col}) WHERE {col} IS NOT NULL", False)]


# -- the read filter ----------------------------------------------------------


def live_sql(ts: TableSpec, alias: str = "") -> str:
    """The predicate a row passes while it has not expired, or ``""`` for a
    model without ``Meta.ttl``. ``alias`` qualifies the column (``"t"``)."""
    if not ts.ttl:
        return ""
    col = (f"{alias}." if alias else "") + quote_ident(EXPIRES_COL)
    return f"({col} IS NULL OR {col} > {now_sql()})"


def expired_pks_sql(ts: TableSpec) -> str:
    """A subquery listing the expired rows' keys, for the side tables
    (postings, lengths, vectors, membership rows) that have no
    ``_expires_at`` of their own: ``"_pk" NOT IN <this>``. ``""`` for a model
    without ``Meta.ttl``. Served by the partial index, and small, because the
    reaper keeps it so."""
    if not ts.ttl:
        return ""
    col = quote_ident(EXPIRES_COL)
    return f'(SELECT "_pk" FROM {ts.qualified} WHERE {col} <= {now_sql()})'


def and_live(ts: TableSpec, where_sql: str, alias: str = "") -> str:
    """``where_sql`` (``" WHERE …"`` or ``""``) with the read filter ANDed
    on."""
    live = live_sql(ts, alias)
    if not live:
        return where_sql
    if not where_sql:
        return " WHERE " + live
    return f" WHERE ({where_sql[len(' WHERE '):]}) AND {live}"


# -- writes -------------------------------------------------------------------


def _ttl_seconds(value: Any) -> int:
    """The instance ``_ttl`` as whole seconds -- what ``EXPIRE`` accepts.
    ``EXPIRE`` refuses anything else after ``MULTI``/``EXEC`` has written the
    hash; Postgres refuses it before writing anything."""
    if isinstance(value, datetime.timedelta):
        return int(value.total_seconds())
    if isinstance(value, bool) or not isinstance(value, int):
        from ...exceptions import ModelException

        raise ModelException(
            f"ttl must be a whole number of seconds (int), got {value!r}: "
            "value is not an integer or out of range"
        )
    return value


def expiry_sql(
    spec: ModelSpec, obj: Any, expiry: Optional[Expiry]
) -> Optional[tuple[str, list[Any]]]:
    """``(value SQL, params)`` for ``_expires_at`` on this save, or ``None``
    to leave the stored expiry alone (no TTL on the instance: ``HSET`` leaves
    a key's TTL alone too).

    ``expiry`` (the protocol's per-call override) wins; otherwise the
    instance's ``_ttl``, then ``_expire_at`` -- the order the Redis save path
    issues ``EXPIRE``/``EXPIREAT`` in. A TTL on a model without ``Meta.ttl``
    raises :class:`BackendCapabilityError`: that model's table has no
    ``_expires_at`` and its reads no filter (plan §3)."""
    if expiry is not None:
        ttl: Any = expiry.ttl
        at: Any = expiry.expire_at
    else:
        ttl = getattr(obj, "_ttl", None)
        at = getattr(obj, "_expire_at", None)
    if ttl is None and at is None:
        return None
    if spec.ttl is None:
        raise BackendCapabilityError(
            f"{spec.name}: an instance TTL (_ttl / _expire_at / expiry=) needs "
            "Meta.ttl on a Postgres model -- only a Meta.ttl model has the "
            "_expires_at column and the read filter, so models that never "
            "expire keep plans that never check. Declare Meta.ttl (an "
            "instance can still opt out with _ttl = None)."
        )
    if ttl is not None:
        return f"({now_sql()} + %s::float8)", [float(_ttl_seconds(ttl))]
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        # The protocol's Expiry.expire_at is epoch seconds already.
        return "%s::float8", [float(int(at))]
    # Model._expire_at is a datetime; EXPIREAT gets whole seconds.
    return "%s::float8", [float(int(at.timestamp()))]


def purge_expired_sql(ts: TableSpec) -> str:
    """The statement a save of a TTL model runs (after the record's key lock,
    before its upsert): drop the row under this key if it has expired, so
    the save writes a fresh record -- as ``HSET`` on a key Redis has expired
    creates a new hash -- rather than reviving the expired row's unwritten
    columns, side rows and engine state. ``""`` without ``Meta.ttl``. Takes
    the key as its one parameter; the side rows cascade."""
    if not ts.ttl:
        return ""
    col = quote_ident(EXPIRES_COL)
    return f'DELETE FROM {ts.qualified} WHERE "_pk" = %s AND {col} <= {now_sql()}; '


# -- the reaper ---------------------------------------------------------------


def _reap_sql(ts: TableSpec, batch: int) -> tuple[str, list[Any]]:
    """Delete up to ``batch`` expired rows, in the backend's lock order but
    never waiting: each candidate's record-key lock (the one every writer
    takes first, ``record_lock_sql``) is a *try*-lock, so a record a writer
    holds is skipped; the surviving rows are locked ``SKIP LOCKED`` in
    ``_pk`` byte order and re-checked (a save that refreshed the TTL in
    between keeps its row); then deleted, their side rows cascading.
    Candidates come off the partial ``_expires_at`` index, oldest first."""
    col = quote_ident(EXPIRES_COL)
    now = now_sql()
    t = ts.qualified
    sql = (
        f'WITH "_cand" AS MATERIALIZED (SELECT "_pk" FROM {t} '
        f"WHERE {col} <= {now} ORDER BY {col} LIMIT %s), "
        '"_keyed" AS MATERIALIZED (SELECT c."_pk" FROM "_cand" c WHERE '
        'pg_try_advisory_xact_lock(hashtextextended(%s || c."_pk", 0))), '
        f'"_rows" AS MATERIALIZED (SELECT r."_pk" FROM {t} r JOIN "_keyed" k '
        f'ON k."_pk" = r."_pk" WHERE r.{col} <= {now} '
        'ORDER BY r."_pk" COLLATE "C" FOR UPDATE OF r SKIP LOCKED) '
        f'DELETE FROM {t} d USING "_rows" x WHERE d."_pk" = x."_pk" '
        'RETURNING d."_pk"'
    )
    return sql, [int(batch), f"popoto:rec:{ts.qualified}:"]


class Reaper:
    """Per-backend reaper state: when each table may next be reaped."""

    def __init__(self) -> None:
        self._due: dict[str, float] = {}
        self._lock = threading.Lock()

    def forget(self) -> None:
        with self._lock:
            self._due.clear()

    def due(self, table: str) -> bool:
        return clock() >= self._due.get(table, float("-inf"))

    def schedule(self, table: str, *, backlog: bool) -> None:
        interval = 0.0 if backlog else float(Defaults.PG_REAPER_INTERVAL_SECONDS)
        with self._lock:
            self._due[table] = clock() + interval


def reap(backend: Any, ts: TableSpec, *, force: bool = False) -> list[str]:
    """Run the reaper for ``ts`` if it is due (or ``force``): delete up to
    ``Defaults.PG_REAPER_BATCH`` expired rows in a transaction of its own,
    on a pooled connection it waits at most
    ``Defaults.PG_REAPER_LOCK_TIMEOUT_MS`` for. Returns the deleted keys.

    Called after a write has committed, never inside a caller's
    transaction, and it never raises: a busy pool, a lock it would have to
    wait for, or an outage skips this run (logged), and the next write
    tries again. It does not touch the backend's health record -- a reaper
    run is not the caller's write."""
    if not ts.ttl:
        return []
    reaper: Reaper = backend._reaper
    table = ts.qualified
    if not force and not reaper.due(table):
        return []
    batch = int(Defaults.PG_REAPER_BATCH)
    if batch <= 0:
        return []
    from . import _checkout, _import_psycopg, _pool_for

    psycopg = _import_psycopg()
    wait_ms = max(1, int(Defaults.PG_REAPER_LOCK_TIMEOUT_MS))
    sql, params = _reap_sql(ts, batch)
    prefix = backend._statement_prefix() + f"SET LOCAL lock_timeout = {wait_ms}; "
    backend._guard_second_checkout("the TTL reaper")  # #776: never swallowed
    try:
        pool = _pool_for(backend.dsn)
        with _checkout(pool, timeout=wait_ms / 1000.0) as conn:
            cur = conn.execute(prefix + sql, params)
            while cur.nextset():
                pass
            rows = cur.fetchall() if cur.description else []
    except Exception as exc:  # noqa: BLE001 - never fail the write
        level = (
            logging.DEBUG
            if isinstance(exc, psycopg.errors.LockNotAvailable)
            or type(exc).__name__ == "PoolTimeout"
            else logging.WARNING
        )
        logger.log(
            level,
            "popoto TTL reaper skipped a run on %s: %s: %s",
            table,
            type(exc).__name__,
            exc,
        )
        # Back off for the interval: under contention or an outage, retrying
        # on every write would charge each one the wait.
        reaper.schedule(table, backlog=False)
        return []
    deleted = [row[0] for row in rows]
    reaper.schedule(table, backlog=len(deleted) >= batch)
    if deleted:
        logger.debug("popoto TTL reaper deleted %d row(s) of %s", len(deleted), table)
    return deleted


# -- the remaining-TTL read ---------------------------------------------------


def ttl_remaining(backend: Any, spec: ModelSpec, keys: Sequence[str]) -> list[int]:
    """Redis's ``TTL`` reply for each key: ``-2`` for no live record (absent
    or expired), ``-1`` for a record with no expiry, else the seconds left,
    rounded as Redis rounds its milliseconds (``(ms + 500) // 1000``). For
    tests and tools that read a remaining TTL on both backends."""
    if not keys:
        return []
    ts = backend._table(spec)
    if not ts.ttl:
        rows, _ = backend._run(
            f'SELECT "_pk" FROM {ts.qualified} WHERE "_pk" = ANY(%s::text[])',
            [list(keys)],
        )
        found = {r[0] for r in rows}
        return [-1 if k in found else -2 for k in keys]
    col = quote_ident(EXPIRES_COL)
    rows, _ = backend._run(
        f'SELECT "_pk", ({col} - {now_sql()}) * 1000 FROM {ts.qualified} '
        f'WHERE "_pk" = ANY(%s::text[]) AND {live_sql(ts)}',
        [list(keys)],
    )
    left = {pk: ms for pk, ms in rows}
    out = []
    for key in keys:
        if key not in left:
            out.append(-2)
        elif left[key] is None:
            out.append(-1)
        else:
            out.append(int((int(left[key]) + 500) // 1000))
    return out
