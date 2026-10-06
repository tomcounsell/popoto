"""The recipe-layer ``field_call`` adapters on Postgres (#759 M4; plan §2 H).

The recipes reach storage through field and model methods (#630), and a few
of those methods keep state outside any model's hash on Redis: a counter
string, the tombstone archive and negative prior, the question queue's token
bucket and propose lock, ``OBJECT IDLETIME``, and the ``ZCARD`` / ``ZRANGE``
/ ``ZSCORE`` reads of a sorted field's partition. On a Postgres-bound model
each is a ``field_call`` adapter here, keyed by a pseudo-field (``_counter``,
``_tomb``, ``_tombprior``, ``_qq``, ``_idle``) or, for the sorted reads, by
the field itself:

=============  =============================================================
adapter        Postgres
=============  =============================================================
``_idle``      ``idle_seconds``: whole seconds since the later of the row's
               ``_updated_at`` (its last write) and ``_last_accessed`` (its
               last confirmed read, ``AccessTrackerMixin``); ``None`` with no
               row. Redis's ``OBJECT IDLETIME`` also resets on an unconfirmed
               read (any ``HGETALL``) -- a documented divergence.
``sorted``     ``count`` / ``members`` / ``score`` on a sorted field: the
               partition's rows with a value, ordered by ``(value, _pk COLLATE
               "C")`` -- the sorted set's ``(score, member)`` order --
               ``ZRANGE``'s inclusive and negative ranks included.
``_counter``   ``counters.increment`` / ``read`` on one engine table,
               ``popoto_counter (key, value)``: ``INSERT … ON CONFLICT DO
               UPDATE … RETURNING`` is ``INCRBY``.
``_tomb``      ``TombstoneStore``: ``popoto_tombstone (model, member, entry,
               ts)``, the data hash and recency index as one row per member.
``_tombprior`` ``TombstonePriorStore``: ``popoto_tombstone_prior (model,
               digest, burials, ts)`` and ``popoto_tombstone_stats``.
``_qq``        the question queue: ``deliver`` (``_DELIVER_LUA``), ``claim``
               (``_CLAIM_LUA``), ``lock`` / ``release`` (the propose lock's
               ``SET NX PX`` and ``_RELEASE_LUA``) over the candidate table,
               ``popoto_question_bucket (agent, last_turn, expires_at)`` and
               ``popoto_lease (key, token, expires_at)``.
=============  =============================================================

The engine tables are created on first use under ``pg_advisory_xact_lock``,
like ``popoto_recall_proposal``: the schema's DDL lock first, as
``ensure_table`` takes it, then the table's (``schema.engine_table_ddl``), so
concurrent first uses serialise on ``CREATE SCHEMA``. They carry the same
caveat on a shared schema: they are keyed by model *name*.

**Delivery.** ``_DELIVER_LUA`` grants the agent's one ask per ``K`` turns
*and* claims a candidate in one atomic step. Here that step is one
statement behind the agent's bucket advisory lock (so two deliveries for one
agent serialize: the budget is exact under any concurrency); the candidates
are tried in the caller's preference order with ``FOR UPDATE SKIP LOCKED``,
the first still deliverable and past its cooldown is flipped to
``delivered``, and the bucket is written only when that claim succeeds. The
statement waits on nothing but the bucket lock, which no record writer
takes, so it cannot join a deadlock cycle; the cost is that a candidate a
concurrent writer holds at that instant is skipped, as a candidate another
worker claimed is skipped on Redis (a documented divergence: on Redis the
script would wait its turn and see it).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional, Sequence

from ..types import ModelSpec, RecordId, UnitOfWork
from .memory import _NOT_HANDLED
from .schema import engine_table_ddl, quote_ident

__all__ = [
    "COUNTER_FIELD",
    "IDLE_FIELD",
    "QQ_FIELD",
    "RecipeOpsMixin",
    "TOMB_FIELD",
    "TOMBPRIOR_FIELD",
]

logger = logging.getLogger("POPOTO.postgres")

IDLE_FIELD = "_idle"
COUNTER_FIELD = "_counter"
TOMB_FIELD = "_tomb"
TOMBPRIOR_FIELD = "_tombprior"
QQ_FIELD = "_qq"
NEVER_RECORD_FIELD = "_never_record"
EMBED_CACHE_FIELD = "_embed_cache"

SORTED_KINDS = frozenset(
    {"SortedField", "SortedKeyField", "DecayingSortedField", "CyclicDecayField"}
)

#: Engine tables (name -> column DDL), created on first use per process.
ENGINE_TABLES: dict[str, str] = {
    "popoto_counter": ("key text PRIMARY KEY, value bigint NOT NULL"),
    "popoto_tombstone": (
        "model text NOT NULL, member text NOT NULL, entry bytea NOT NULL, "
        "ts double precision NOT NULL, PRIMARY KEY (model, member)"
    ),
    "popoto_tombstone_prior": (
        "model text NOT NULL, digest text NOT NULL, burials bigint NOT NULL, "
        "ts double precision NOT NULL, PRIMARY KEY (model, digest)"
    ),
    "popoto_tombstone_stats": (
        "model text PRIMARY KEY, penalized bigint NOT NULL, "
        "drawdown_total double precision NOT NULL"
    ),
    "popoto_question_bucket": (
        "agent text PRIMARY KEY, last_turn bigint NOT NULL, "
        "expires_at double precision NOT NULL"
    ),
    "popoto_lease": (
        "key text PRIMARY KEY, token text NOT NULL, "
        "expires_at double precision NOT NULL"
    ),
    "popoto_embedding_cache": (
        "model text NOT NULL, member text NOT NULL, vector jsonb NOT NULL, "
        "PRIMARY KEY (model, member)"
    ),
    "popoto_never_record_count": (
        "model text NOT NULL, reason text NOT NULL, count bigint NOT NULL, "
        "PRIMARY KEY (model, reason)"
    ),
    "popoto_never_record_log": (
        "model text NOT NULL, seq bigint GENERATED ALWAYS AS IDENTITY, "
        "entry text NOT NULL, PRIMARY KEY (model, seq)"
    ),
}

_NOW = "extract(epoch from clock_timestamp())"


class RecipeOpsMixin:
    """The adapters above, for
    :class:`~popoto.backends.postgres.PostgresBackend`."""

    schema: str
    _table: Callable[..., Any]
    _run: Callable[..., tuple[list[tuple[Any, ...]], int]]
    _record_locked: Callable[..., tuple[str, list[Any]]]
    _ensure_engine_table: Callable[..., None]

    # -- engine tables ------------------------------------------------------------

    def _engine(self, name: str) -> str:
        """``<schema>.<name>``, created on first use in this process (and
        again after :meth:`forget_tables`)."""
        qualified = f"{quote_ident(self.schema)}.{quote_ident(name)}"
        ready: set[str] = self.__dict__.setdefault("_engine_ready", set())
        if name in ready:
            return qualified
        # First-use DDL never takes a pooled connection inside a unit of
        # work (#776, PostgresBackend._ensure_engine_table). The schema lock
        # comes first in the DDL, as ensure_table takes it: a per-table lock
        # alone lets two first uses race on CREATE SCHEMA (B1).
        self._ensure_engine_table(
            "SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename = %s",
            [self.schema, name],
            bool,
            lambda: engine_table_ddl(self.schema, name, ENGINE_TABLES[name]),
            f"{self.schema}.{name} does not exist",
        )
        ready.add(name)
        return qualified

    # -- dispatch -----------------------------------------------------------------

    def _recipe_field_call(
        self,
        spec: ModelSpec,
        field: str,
        op: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        uow: Optional[UnitOfWork],
    ) -> Any:
        handlers: Mapping[str, Callable[..., Any]] = {}
        if field == IDLE_FIELD:
            handlers = {"seconds": self._idle_seconds}
        elif field == COUNTER_FIELD:
            handlers = {
                "increment": self._counter_increment,
                "read": self._counter_read,
            }
        elif field == TOMB_FIELD:
            handlers = {
                "archive": self._tomb_archive,
                "count": self._tomb_count,
                "oldest": self._tomb_oldest,
                "newest": self._tomb_newest,
                "evict": self._tomb_evict,
                "entries": self._tomb_entries,
                "purge": self._tomb_purge,
                "purge_all": self._tomb_purge_all,
            }
        elif field == TOMBPRIOR_FIELD:
            handlers = {
                "bury": self._prior_bury,
                "burials": self._prior_burials,
                "penalty": self._prior_penalty,
                "stats": self._prior_stats,
                "count": self._prior_count,
                "purge_all": self._prior_purge_all,
            }
        elif field == EMBED_CACHE_FIELD:
            handlers = {
                "get": self._embed_cache_get,
                "set": self._embed_cache_set,
                "drop": self._embed_cache_drop,
            }
        elif field == NEVER_RECORD_FIELD:
            handlers = {
                "drop": self._nr_drop,
                "counts": self._nr_counts,
                "log": self._nr_log,
            }
        elif field == QQ_FIELD:
            handlers = {
                "deliver": self._qq_deliver,
                "claim": self._qq_claim,
                "lock": self._qq_lock,
                "release": self._qq_release,
            }
        else:
            fs = spec.fields.get(field)
            if fs is not None and fs.kind in SORTED_KINDS:
                handlers = {
                    "count": self._sorted_count,
                    "members": self._sorted_members,
                    "score": self._sorted_score,
                }
        handler = handlers.get(op)
        if handler is None:
            return _NOT_HANDLED
        if field in (
            COUNTER_FIELD,
            TOMB_FIELD,
            TOMBPRIOR_FIELD,
            NEVER_RECORD_FIELD,
            EMBED_CACHE_FIELD,
        ):
            # Model-level stores: the field name carries no information.
            return handler(spec, *args, uow=uow, **kwargs)
        return handler(spec, field, *args, uow=uow, **kwargs)

    # -- idle_seconds -------------------------------------------------------------

    def _idle_seconds(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[float]:
        ts = self._table(spec)
        last = (
            'greatest(extract(epoch from "_updated_at"), coalesce("_last_accessed", '
            "'-Infinity'::float8))"
            if "_last_accessed" in {c.name for c in ts.columns}
            else 'extract(epoch from "_updated_at")'
        )
        from .ttl import live_sql

        # M5: an expired record has no idle time, as OBJECT IDLETIME of a key
        # Redis expired is nil.
        live = live_sql(ts)
        rows, _ = self._run(
            f"SELECT floor(greatest({_NOW} - {last}, 0))::float8 "
            f'FROM {ts.qualified} WHERE "_pk" = %s' + (f" AND {live}" if live else ""),
            [id.canonical],
            uow=uow,
        )
        return None if not rows else float(rows[0][0])

    # -- a sorted field's partition --------------------------------------------------

    def _partition_sql(
        self, ts: Any, spec: ModelSpec, field: str, partition: Mapping[str, Any]
    ) -> tuple[str, list[Any]]:
        from .memory import partition_where
        from .plan import render_where

        kinds = {name: fs.kind for name, fs in spec.fields.items()}
        sql, params = render_where(ts, kinds, partition_where(partition))
        col = f"t.{quote_ident(field)}"
        where = f" WHERE {col} IS NOT NULL"
        if sql:
            where += " AND " + sql[len(" WHERE ") :]
        return where, params

    def _sorted_count(
        self,
        spec: ModelSpec,
        field: str,
        partition: Mapping[str, Any],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> int:
        """``ZCARD`` of the partition: rows holding a value."""
        ts = self._table(spec)
        where, params = self._partition_sql(ts, spec, field, partition)
        rows, _ = self._run(
            f"SELECT count(*) FROM {ts.qualified} AS t{where}", params, uow=uow
        )
        return int(rows[0][0])

    def _sorted_members(
        self,
        spec: ModelSpec,
        field: str,
        partition: Mapping[str, Any],
        start: int,
        stop: int,
        reverse: bool,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> list[str]:
        """``ZRANGE`` / ``ZREVRANGE start stop``: inclusive ranks, negative
        ones counted from the end, ``(value, _pk)`` order (reversed: both
        descending)."""
        start, stop = int(start), int(stop)
        if start < 0 or stop < 0:
            n = self._sorted_count(spec, field, partition, uow=uow)
            start = max(start + n, 0) if start < 0 else start
            stop = stop + n if stop < 0 else stop
        if stop < start or stop < 0:
            return []
        ts = self._table(spec)
        where, params = self._partition_sql(ts, spec, field, partition)
        direction = "DESC" if reverse else "ASC"
        col = f"t.{quote_ident(field)}"
        rows, _ = self._run(
            f'SELECT t."_pk" FROM {ts.qualified} AS t{where} '
            f'ORDER BY {col} {direction}, t."_pk" COLLATE "C" {direction} '
            f"OFFSET {start} LIMIT {stop - start + 1}",
            params,
            uow=uow,
        )
        return [row[0] for row in rows]

    def _sorted_score(
        self,
        spec: ModelSpec,
        field: str,
        partition: Optional[Mapping[str, Any]],
        id: RecordId,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[Any]:
        """``ZSCORE``: the member's stored value, when the record is in the
        partition (``partition=None`` for the unpartitioned key, which a
        partitioned field's member is never in -- as on Redis). The caller
        converts it to the sorted set's score."""
        if partition is None and spec.fields[field].options.get("partition_by"):
            return None
        ts = self._table(spec)
        where, params = self._partition_sql(ts, spec, field, partition or {})
        rows, _ = self._run(
            f"SELECT t.{quote_ident(field)} FROM {ts.qualified} AS t{where} "
            'AND t."_pk" = %s',
            params + [id.canonical],
            uow=uow,
        )
        return rows[0][0] if rows else None

    # -- counters -------------------------------------------------------------------

    def _counter_increment(
        self, spec: ModelSpec, key: str, delta: int, *, uow: Optional[UnitOfWork] = None
    ) -> int:
        table = self._engine("popoto_counter")
        rows, _ = self._run(
            f"INSERT INTO {table} AS c (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = c.value + EXCLUDED.value "
            "RETURNING c.value",
            [key, int(delta)],
            uow=uow,
            write=True,
        )
        return int(rows[0][0])

    def _counter_read(
        self, spec: ModelSpec, key: str, *, uow: Optional[UnitOfWork] = None
    ) -> int:
        table = self._engine("popoto_counter")
        rows, _ = self._run(f"SELECT value FROM {table} WHERE key = %s", [key], uow=uow)
        return int(rows[0][0]) if rows else 0

    # -- TombstoneStore ---------------------------------------------------------------

    def _tomb_archive(
        self,
        spec: ModelSpec,
        member: str,
        entry: bytes,
        ts: float,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        table = self._engine("popoto_tombstone")
        self._run(
            f"INSERT INTO {table} (model, member, entry, ts) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (model, member) DO UPDATE SET entry = EXCLUDED.entry, "
            "ts = EXCLUDED.ts",
            [spec.name, member, bytes(entry), float(ts)],
            uow=uow,
            write=True,
        )

    def _tomb_count(self, spec: ModelSpec, *, uow: Optional[UnitOfWork] = None) -> int:
        table = self._engine("popoto_tombstone")
        rows, _ = self._run(
            f"SELECT count(*) FROM {table} WHERE model = %s", [spec.name], uow=uow
        )
        return int(rows[0][0])

    def _tomb_ranked(self, spec: ModelSpec, n: int, desc: bool) -> list[str]:
        if n <= 0:
            return []
        table = self._engine("popoto_tombstone")
        order = "DESC" if desc else "ASC"
        rows, _ = self._run(
            f"SELECT member FROM {table} WHERE model = %s "
            f'ORDER BY ts {order}, member COLLATE "C" {order} LIMIT {int(n)}',
            [spec.name],
        )
        return [r[0] for r in rows]

    def _tomb_oldest(
        self, spec: ModelSpec, n: int, *, uow: Optional[UnitOfWork] = None
    ) -> list[str]:
        """``ZRANGE index 0 n-1``."""
        return self._tomb_ranked(spec, int(n), False)

    def _tomb_newest(
        self, spec: ModelSpec, stop: int, *, uow: Optional[UnitOfWork] = None
    ) -> list[str]:
        """``ZREVRANGE index 0 stop`` (``stop=-1``: all)."""
        stop = int(stop)
        if stop < 0:
            n = self._tomb_count(spec) + stop + 1
        else:
            n = stop + 1
        return self._tomb_ranked(spec, n, True)

    def _tomb_evict(
        self,
        spec: ModelSpec,
        members: Sequence[str],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> int:
        table = self._engine("popoto_tombstone")
        _, count = self._run(
            f"DELETE FROM {table} WHERE model = %s AND member = ANY(%s::text[])",
            [spec.name, list(members)],
            uow=uow,
            write=True,
        )
        return int(count or 0)

    def _tomb_entries(
        self,
        spec: ModelSpec,
        members: Sequence[str],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> list[Optional[bytes]]:
        """``HMGET``: entries in ``members`` order, ``None`` for a hole."""
        members = list(members)
        if not members:
            return []
        table = self._engine("popoto_tombstone")
        rows, _ = self._run(
            f"SELECT member, entry FROM {table} WHERE model = %s "
            "AND member = ANY(%s::text[])",
            [spec.name, members],
            uow=uow,
        )
        found = {m: bytes(e) for m, e in rows}
        return [found.get(m) for m in members]

    def _tomb_purge(
        self, spec: ModelSpec, member: str, *, uow: Optional[UnitOfWork] = None
    ) -> bool:
        return bool(self._tomb_evict(spec, [member], uow=uow))

    def _tomb_purge_all(
        self, spec: ModelSpec, *, uow: Optional[UnitOfWork] = None
    ) -> int:
        table = self._engine("popoto_tombstone")
        _, count = self._run(
            f"DELETE FROM {table} WHERE model = %s", [spec.name], uow=uow, write=True
        )
        return int(count or 0)

    # -- TombstonePriorStore ----------------------------------------------------------

    def _prior_bury(
        self,
        spec: ModelSpec,
        digest: str,
        ts: float,
        limit: int,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> int:
        """``HINCRBY`` + ``ZADD``, then the retention sweep (the oldest
        digests past ``limit``); returns how many were aged out."""
        table = self._engine("popoto_tombstone_prior")
        self._run(
            f"INSERT INTO {table} AS p (model, digest, burials, ts) "
            "VALUES (%s, %s, 1, %s) ON CONFLICT (model, digest) DO UPDATE "
            "SET burials = p.burials + 1, ts = EXCLUDED.ts",
            [spec.name, digest, float(ts)],
            uow=uow,
            write=True,
        )
        _, count = self._run(
            f"DELETE FROM {table} WHERE model = %s AND digest IN ("
            f"SELECT digest FROM {table} WHERE model = %s "
            'ORDER BY ts, digest COLLATE "C" LIMIT greatest(('
            f"SELECT count(*) FROM {table} WHERE model = %s) - %s, 0))",
            [spec.name, spec.name, spec.name, int(limit)],
            uow=uow,
            write=True,
        )
        return int(count or 0)

    def _prior_burials(
        self, spec: ModelSpec, digest: str, *, uow: Optional[UnitOfWork] = None
    ) -> int:
        table = self._engine("popoto_tombstone_prior")
        rows, _ = self._run(
            f"SELECT burials FROM {table} WHERE model = %s AND digest = %s",
            [spec.name, digest],
            uow=uow,
        )
        return int(rows[0][0]) if rows else 0

    def _prior_penalty(
        self,
        spec: ModelSpec,
        drawdown: float,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        table = self._engine("popoto_tombstone_stats")
        self._run(
            f"INSERT INTO {table} AS s (model, penalized, drawdown_total) "
            "VALUES (%s, 1, %s) ON CONFLICT (model) DO UPDATE SET "
            "penalized = s.penalized + 1, "
            "drawdown_total = s.drawdown_total + EXCLUDED.drawdown_total",
            [spec.name, float(drawdown)],
            uow=uow,
            write=True,
        )

    def _prior_stats(
        self, spec: ModelSpec, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[tuple[int, float]]:
        table = self._engine("popoto_tombstone_stats")
        rows, _ = self._run(
            f"SELECT penalized, drawdown_total FROM {table} WHERE model = %s",
            [spec.name],
            uow=uow,
        )
        return (int(rows[0][0]), float(rows[0][1])) if rows else None

    def _prior_count(self, spec: ModelSpec, *, uow: Optional[UnitOfWork] = None) -> int:
        table = self._engine("popoto_tombstone_prior")
        rows, _ = self._run(
            f"SELECT count(*) FROM {table} WHERE model = %s", [spec.name], uow=uow
        )
        return int(rows[0][0])

    def _prior_purge_all(
        self, spec: ModelSpec, *, uow: Optional[UnitOfWork] = None
    ) -> int:
        prior = self._engine("popoto_tombstone_prior")
        stats = self._engine("popoto_tombstone_stats")
        rows, _ = self._run(
            f"WITH d AS (DELETE FROM {prior} WHERE model = %s RETURNING 1), "
            f"s AS (DELETE FROM {stats} WHERE model = %s) SELECT count(*) FROM d",
            [spec.name, spec.name],
            uow=uow,
            write=True,
        )
        return int(rows[0][0])

    # -- reconciliation's embedding cache ---------------------------------------------

    def _embed_cache_get(
        self, spec: ModelSpec, member: str, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[list[float]]:
        """``HGET POPOTO:M5:embedding_cache <member>``: the cached vector."""
        table = self._engine("popoto_embedding_cache")
        rows, _ = self._run(
            f"SELECT vector FROM {table} WHERE model = %s AND member = %s",
            [spec.name, member],
            uow=uow,
        )
        return [float(v) for v in rows[0][0]] if rows else None

    def _embed_cache_set(
        self,
        spec: ModelSpec,
        member: str,
        vector: Sequence[float],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        table = self._engine("popoto_embedding_cache")
        self._run(
            f"INSERT INTO {table} (model, member, vector) VALUES (%s, %s, %s) "
            "ON CONFLICT (model, member) DO UPDATE SET vector = EXCLUDED.vector",
            [spec.name, member, Jsonb([float(v) for v in vector])],
            uow=uow,
            write=True,
        )

    def _embed_cache_drop(
        self, spec: ModelSpec, member: str, *, uow: Optional[UnitOfWork] = None
    ) -> int:
        table = self._engine("popoto_embedding_cache")
        _, count = self._run(
            f"DELETE FROM {table} WHERE model = %s AND member = %s",
            [spec.name, member],
            uow=uow,
            write=True,
        )
        return int(count or 0)

    # -- NeverRecordMixin's audit log -----------------------------------------------

    def _nr_drop(
        self,
        spec: ModelSpec,
        reason: str,
        entry: str,
        keep: int,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        """``HINCRBY counts <reason> 1``, ``LPUSH drops <entry>``, ``LTRIM``
        to the newest ``keep``: one message. The entry is the same
        content-free JSON the Redis list holds.

        A database error :meth:`_run` leaves unclassified (an ``UndefinedTable``
        after the audit tables were dropped under a warm memo, a permission
        error) is raised as :class:`~popoto.backends.BackendError`, never as
        raw psycopg (PR #793 review): the refused save's caller sees popoto's
        error family whether or not a unit of work carried the tombstone."""
        import psycopg

        from ..types import BackendError

        try:
            counts = self._engine("popoto_never_record_count")
            log = self._engine("popoto_never_record_log")
            self._run(
                f"INSERT INTO {counts} AS c (model, reason, count) VALUES (%s, %s, 1) "
                "ON CONFLICT (model, reason) DO UPDATE SET count = c.count + 1; "
                f"INSERT INTO {log} (model, entry) VALUES (%s, %s); "
                f"DELETE FROM {log} WHERE model = %s AND seq NOT IN ("
                f"SELECT seq FROM {log} WHERE model = %s ORDER BY seq DESC LIMIT %s)",
                [spec.name, reason, spec.name, entry, spec.name, spec.name, int(keep)],
                uow=uow,
                write=True,
            )
        except psycopg.Error as exc:
            raise BackendError(
                f"popoto could not write the never-record tombstone for "
                f"{spec.name} (SQLSTATE {getattr(exc, 'sqlstate', None)}: "
                f"{type(exc).__name__}: {exc}); the save was refused regardless"
            ) from exc

    def _nr_counts(
        self, spec: ModelSpec, *, uow: Optional[UnitOfWork] = None
    ) -> dict[str, int]:
        counts = self._engine("popoto_never_record_count")
        rows, _ = self._run(
            f"SELECT reason, count FROM {counts} WHERE model = %s",
            [spec.name],
            uow=uow,
        )
        return {reason: int(count) for reason, count in rows}

    def _nr_log(
        self, spec: ModelSpec, limit: int, *, uow: Optional[UnitOfWork] = None
    ) -> list[str]:
        """``LRANGE drops 0 limit-1``: newest first, with ``LRANGE``'s
        negative stop (``limit=0`` is the whole log). The log is capped at
        ``Defaults.NR_TOMBSTONE_LOG_MAX`` rows, so it is read whole."""
        log = self._engine("popoto_never_record_log")
        rows, _ = self._run(
            f"SELECT entry FROM {log} WHERE model = %s ORDER BY seq DESC",
            [spec.name],
            uow=uow,
        )
        entries = [row[0] for row in rows]
        stop = int(limit) - 1
        if stop < 0:
            stop += len(entries)
        return entries[: max(stop + 1, 0)]

    # -- the question queue -------------------------------------------------------------

    def _qq_deliver(
        self,
        spec: ModelSpec,
        field: str,
        agent: str,
        keys: Sequence[str],
        turn: int,
        budget_turns: int,
        ttl_seconds: float,
        allowed: Sequence[str],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[tuple[int, int]]:
        """``_DELIVER_LUA`` (module docstring). Returns ``(index, ask_count)``
        (the 1-based index into ``keys``) on a delivery, else ``None``."""
        if not keys:
            return None
        ts = self._table(spec, write=True)
        bucket = self._engine("popoto_question_bucket")
        lock = f"popoto:qq:{quote_ident(self.schema)}:{agent}"
        sql = (
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0)); "
            f"WITH b AS (SELECT last_turn FROM {bucket} WHERE agent = %s "
            f"AND expires_at > {_NOW}), "
            'pick AS (SELECT t."_pk", coalesce(t."ask_count", 0) + 1 AS n, k.ord '
            "FROM unnest(%s::text[]) WITH ORDINALITY AS k(pk, ord) "
            f'JOIN {ts.qualified} AS t ON t."_pk" = k.pk '
            "WHERE NOT EXISTS (SELECT 1 FROM b WHERE %s < b.last_turn + %s) "
            'AND t."status" = ANY(%s::text[]) '
            'AND (t."cooldown_until" IS NULL OR NOT (%s < t."cooldown_until")) '
            "ORDER BY k.ord LIMIT 1 FOR UPDATE OF t SKIP LOCKED), "
            f"upd AS (UPDATE {ts.qualified} AS t SET \"status\" = 'delivered', "
            '"ask_count" = pick.n, "delivered_turn" = %s, "_updated_at" = now(), '
            '"_migrated_from" = NULL FROM pick WHERE t."_pk" = pick."_pk" '
            "RETURNING pick.ord, pick.n), "
            f"bk AS (INSERT INTO {bucket} AS q (agent, last_turn, expires_at) "
            f"SELECT %s, %s, {_NOW} + %s FROM upd "
            "ON CONFLICT (agent) DO UPDATE SET last_turn = EXCLUDED.last_turn, "
            "expires_at = EXCLUDED.expires_at) "
            "SELECT ord, n FROM upd"
        )
        params = [
            lock,
            agent,
            list(keys),
            int(turn),
            int(budget_turns),
            list(allowed),
            int(turn),
            int(turn),
            agent,
            int(turn),
            float(ttl_seconds),
        ]
        rows, _ = self._run(sql, params, uow=uow, write=True)
        if not rows:
            return None
        return int(rows[0][0]), int(rows[0][1])

    def _qq_claim(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        allowed: Sequence[str],
        updates: Mapping[str, Any],
        guard: Mapping[str, Any],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> bool:
        """``_CLAIM_LUA``: compare-and-set on ``status`` (and the guard
        fields, an absent value comparing as ``NULL``), then the updates.
        A record writer: its key lock first, then the row."""
        from .plan import to_column_value

        ts = self._table(spec, write=True)
        sets = []
        params: list[Any] = []
        for name, value in updates.items():
            sets.append(f"{quote_ident(name)} = %s")
            params.append(to_column_value(ts, name, value))
        sets.append('"_updated_at" = now(), "_migrated_from" = NULL')
        conds = ['"_pk" = %s', '"status" = ANY(%s::text[])']
        params += [id.canonical, list(allowed)]
        for name, value in guard.items():
            conds.append(f"{quote_ident(name)} IS NOT DISTINCT FROM %s")
            params.append(to_column_value(ts, name, value))
        sql, params = self._record_locked(
            ts,
            [id.canonical],
            f"UPDATE {ts.qualified} SET {', '.join(sets)} "
            f"WHERE {' AND '.join(conds)} RETURNING 1",
            params,
        )
        rows, _ = self._run(sql, params, uow=uow, write=True)
        return bool(rows)

    def _qq_lock(
        self,
        spec: ModelSpec,
        field: str,
        key: str,
        token: str,
        ttl_ms: int,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> bool:
        """``SET key token NX PX ttl``: an expired lease is absent."""
        table = self._engine("popoto_lease")
        rows, _ = self._run(
            f"INSERT INTO {table} AS l (key, token, expires_at) "
            f"VALUES (%s, %s, {_NOW} + %s) ON CONFLICT (key) DO UPDATE SET "
            "token = EXCLUDED.token, expires_at = EXCLUDED.expires_at "
            f"WHERE l.expires_at <= {_NOW} RETURNING 1",
            [key, token, float(ttl_ms) / 1000.0],
            uow=uow,
            write=True,
        )
        return bool(rows)

    def _qq_release(
        self,
        spec: ModelSpec,
        field: str,
        key: str,
        token: str,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> int:
        """``_RELEASE_LUA``: delete the lease only while this token owns it."""
        table = self._engine("popoto_lease")
        _, count = self._run(
            f"DELETE FROM {table} WHERE key = %s AND token = %s",
            [key, token],
            uow=uow,
            write=True,
        )
        return int(count or 0)
