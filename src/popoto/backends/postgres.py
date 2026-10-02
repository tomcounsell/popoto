"""Postgres implementation of the storage backend seam (#631).

WS3a implements family B (records), C (atomic increment) and J's
``purge_orphan``, plus the unit of work they need. Every other method still
raises :class:`NotImplementedError` naming itself; WS3b-e fill them in family
by family behind the WS2 conformance harness.

``psycopg`` is deliberately **not** imported at module scope: selection in
:func:`popoto.backends.get_backend` must be able to import this module without
the driver, and importing ``popoto`` must never dial Postgres. The import sits
inside :meth:`PostgresBackend._connection`, the one place a connection opens.

Schema
------
Generic tables (issue #631's POC decision), created with ``CREATE TABLE IF NOT
EXISTS`` on the first connection a backend instance opens, into whatever
schema the URL's ``search_path`` names (the conformance harness hands every
instance a ``popoto_test_<hex>`` schema that way; the harness refuses
``public`` before any statement runs).

* ``popoto_record(key, field bytea, value bytea)`` -- **one row per field**,
  not the issue's ``data jsonb``: the field layer hands :meth:`save_record`
  per-field *msgpack bytes* keyed by *bytes* field names (plan finding 2, WS0
  deviation 1), and ``jsonb`` cannot hold either. Per-field rows give the
  ``HSET`` merge semantics for free -- a save touches only the fields it names
  -- so :meth:`save_record` is one ``INSERT ... ON CONFLICT DO UPDATE`` over
  ``unnest`` and :meth:`load_record` is one ``SELECT``. ``field`` is ``bytea``
  rather than ``text`` so a name containing ``\\x00`` (popoto's legacy
  in-hash pointer fields) round-trips and ``HGETALL``'s ``bytes`` keys come
  back as they went in.
* ``popoto_numeric(key, field text, value double precision)`` -- the numeric
  side-map (architect decision 2): the decoded value of every numeric field,
  written from :meth:`save_record`'s ``numeric`` argument and refreshed by
  :meth:`increment_field`, so WS3d's ``decayed_rank`` can ``ORDER BY`` a typed
  column instead of a msgpack blob.
* ``popoto_set(idx, member)`` and ``popoto_sorted(idx, member, score)`` --
  the set and sorted indexes :meth:`purge_orphan` prunes and the class set
  :meth:`save_record` registers in. WS3b owns their remaining operations; the
  one-``idx``-column layout is the plan's convention (an index name is opaque
  to the protocol, so the issue's ``(model, field)`` pair collapses into it).

Connections
-----------
One ``psycopg`` connection per backend instance, opened lazily on first use
in autocommit mode: every executed-now method runs inside its own
``conn.transaction()`` block, so a multi-statement operation such as
:meth:`save_record` is atomic the way its Redis ``MULTI`` pipeline is, and a
:class:`PostgresUnitOfWork` runs its whole queue inside one. The plan allows
the Postgres backend to hold a connection because there is no rebind protocol
to honour (``set_REDIS_DB_settings`` is a Redis concern). :meth:`close`
releases it; a ``weakref.finalize`` closes it when the instance is collected,
which is what keeps the conformance fixture -- a fresh instance per test,
never closed -- from leaking a connection per test. The connection is not
shared across threads: psycopg serialises calls on it, but two threads'
transactions would interleave.

Locking
-------
A transaction alone is not the Redis single thread: two *instances* (two
connections) can interleave at statement granularity, and a read-modify-write
on a row that does not exist yet has nothing for ``FOR UPDATE`` to lock. So
every operation that writes a record key -- :meth:`save_record`,
:meth:`delete_record`, :meth:`increment_field` and :meth:`purge_orphan` --
takes ``pg_advisory_xact_lock(hashtext(key))`` as its *first* statement
(:func:`_lock_record_keys`), held to the end of the transaction. On the
``uow=`` path that is the unit of work's transaction, taken at ``commit()``
rather than when the method was called. Without the lock on the writers, an
increment on an absent field could read "no row", lose to a concurrent
``save_record`` committing ``n = 100`` on another instance, and upsert
``0 + 1`` over it: a stored ``1`` that no serial order produces (PR #737
review, B1). The one operation that names two keys, a rename through
``obsolete_key``, locks both ascending by lock id so two instances renaming in
opposite directions take them in the same order.

The atomic-increment envelope
-----------------------------
``ATOMIC_INCREMENT_LUA`` reads the field, ``cmsgpack.unpack``s it, adds the
delta in Lua's double arithmetic, re-packs and returns ``tostring(new)``.
:meth:`increment_field` reproduces that *bit for bit* so the two backends
store and return the same thing: Lua 5.1's ``tostring`` is ``%.14g``
(:func:`_lua_tostring`), its truthiness and ``tonumber`` rules decide what
counts as the current value (:func:`_current_value`), and ``cmsgpack`` packs
a Lua number as a msgpack integer when it is integral, else as a float32 when
that is lossless and a float64 otherwise (:func:`_cmsgpack_pack_number`). The
``Decimal`` envelope is the same tagged map the Lua builds,
``{"__Decimal__": True, "as_encodable": "%.14g"}``. Behaviour on an absent
record or field is the Lua's: the current value is ``0``, and the write
creates the field (and so the record) without touching any class set.
"""

from __future__ import annotations

import math
import struct
import weakref
from decimal import Decimal
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence

import msgpack

from ..redis_db import ENCODING
from . import UnitOfWork

__all__ = ["PostgresBackend", "PostgresUnitOfWork", "SCHEMA_DDL"]

#: Executed in order, once per connection, before any other statement.
SCHEMA_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS popoto_record (
        key   text  NOT NULL,
        field bytea NOT NULL,
        value bytea NOT NULL,
        PRIMARY KEY (key, field)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS popoto_numeric (
        key   text NOT NULL,
        field text NOT NULL,
        value double precision NOT NULL,
        PRIMARY KEY (key, field)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS popoto_set (
        idx    text NOT NULL,
        member text NOT NULL,
        PRIMARY KEY (idx, member)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS popoto_sorted (
        idx    text NOT NULL,
        member text NOT NULL,
        score  double precision NOT NULL,
        PRIMARY KEY (idx, member)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS popoto_sorted_idx_score
        ON popoto_sorted (idx, score)
    """,
)

#: Lua 5.1's ``LUAI_NUMFMT``: what ``tostring(number)`` renders inside Redis.
_LUA_NUMBER_FORMAT = "%.14g"

#: An operation on a cursor; the unit of work queues these and runs them in
#: one transaction, the executed-now path runs one in its own.
Op = Callable[[Any], Any]


def _todo(name: str) -> NotImplementedError:
    return NotImplementedError(
        f"PostgresBackend.{name} is not implemented in the backend-seam POC "
        "(see docs/plans/sdlc-631.md WS3)"
    )


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # pragma: no cover - best effort at interpreter exit
        pass


def _field_bytes(name: Any) -> bytes:
    """Field names are ``bytes`` on the wire (WS0 deviation 1); a ``str`` is
    encoded the way redis-py would encode it."""
    if isinstance(name, bytes):
        return name
    return str(name).encode(ENCODING)


def _lock_record_keys(cur: Any, keys: Sequence[str]) -> None:
    """Take the per-key transaction advisory lock for every key in ``keys``,
    in one global order (see "Locking" in the module docstring).

    Runs as the *first* statement of every operation that writes a record
    key, so two instances' read-modify-write sequences on the same key are
    serialised the way Redis's single thread serialises them. Lock ids are
    ``hashtext(key)``; when an operation names more than one key (a rename
    with ``obsolete_key``) they are taken ascending by lock id, ``DISTINCT``
    so a hash collision locks once, so two instances renaming in opposite
    directions cannot deadlock on each other. The ordered subquery is the
    documented Postgres idiom for acquiring advisory locks in order: the
    volatile call is evaluated per row as the sorted rows stream.
    """
    cur.execute(
        "SELECT pg_advisory_xact_lock(h) FROM ("
        "  SELECT DISTINCT hashtext(k) AS h FROM unnest(%s::text[]) AS t(k)"
        "  ORDER BY h"
        ") AS locks",
        (list(keys),),
    )


# -- Lua parity helpers for increment_field ------------------------------------


def _lua_tostring(value: float) -> str:
    """``tostring(n)`` in Redis's Lua 5.1: ``%.14g``."""
    return _LUA_NUMBER_FORMAT % value


def _lua_tonumber(value: Any) -> float:
    """``tonumber(x)`` on what ``cmsgpack.unpack`` produced for the envelope's
    ``as_encodable``; a value Lua could not convert makes the script fail on
    ``nil + delta``, so this raises ``ValueError`` in the same cases."""
    if isinstance(value, bool):
        raise ValueError("tonumber(boolean) is nil")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, bytes):
        value = value.decode(ENCODING)
    if isinstance(value, str):
        return float(value)
    raise ValueError(f"tonumber({type(value).__name__}) is nil")


def _current_value(packed: bytes | None, is_decimal: bool) -> float:
    """The Lua's ``current_val``: ``0`` when the field is absent, the
    envelope's ``as_encodable`` for a Decimal increment on a tagged map, the
    number itself for a numeric payload, and ``0`` for anything else (a
    string, boolean, nil, list, or a map without the tag)."""
    if packed is None:
        return 0.0
    decoded = msgpack.unpackb(packed, raw=True, strict_map_key=False)
    if is_decimal and isinstance(decoded, dict):
        tagged = decoded.get(b"as_encodable")
        # Lua truthiness: only nil and false fail the ``and``.
        if tagged is not None and tagged is not False:
            return _lua_tonumber(tagged)
    if isinstance(decoded, (int, float)) and not isinstance(decoded, bool):
        return float(decoded)
    return 0.0


def _cmsgpack_pack_number(value: float) -> bytes:
    """Pack a Lua number as Redis's ``cmsgpack`` does: a msgpack integer when
    the double is integral and fits ``int64``, else ``float32`` when the
    narrowing is lossless, else ``float64``.

    One documented, un-emulated hole at exactly ``2**63``: cmsgpack decides
    "fits ``int64``" with a C ``(int64_t)d`` cast, which is undefined
    behaviour for ``9223372036854775808.0``. On aarch64 it saturates, so an
    arm64 Redis stores ``cf 7fffffffffffffff`` (``int64`` max); an x86-64
    Redis and this function store the float32 ``ca 5f000000``. The *returned*
    value (``%.14g``) is identical either way; only a later decode differs
    (``int`` ``2**63 - 1`` against ``float`` ``2**63``). Emulating one
    platform's UB would be wrong on the other, so the deviation is recorded
    here and in ``docs/features/postgres-backend.md`` instead.
    """
    if math.isfinite(value) and value.is_integer() and -(2**63) <= value < 2**63:
        return msgpack.packb(int(value))
    if not math.isnan(value):
        try:
            narrowed = struct.unpack(">f", struct.pack(">f", value))[0]
        except OverflowError:
            narrowed = None
        if narrowed == value:
            return struct.pack(">Bf", 0xCA, value)
    return struct.pack(">Bd", 0xCB, value)


def _pack_increment_result(new_val: float, is_decimal: bool) -> bytes:
    if is_decimal:
        return msgpack.packb(
            {"__Decimal__": True, "as_encodable": _lua_tostring(new_val)}
        )
    return _cmsgpack_pack_number(new_val)


# -- Unit of work ---------------------------------------------------------------


class PostgresUnitOfWork:
    """The Postgres :class:`~popoto.backends.UnitOfWork`: a queue of
    operations run inside one transaction on :meth:`commit`.

    Mirrors the Redis pipeline's shape rather than an open transaction:
    nothing reaches the server until ``commit()``, every ``uow=`` method
    returns ``None`` when queued (WS0 deviation 5), ``commit()`` returns one
    entry per queued *operation* (the value the executed-now call would have
    returned) and leaves the queue empty, so a second ``commit()`` returns
    ``[]`` as a re-executed pipeline does. Leaving the ``with`` block without
    committing discards the queue, as ``Pipeline.__exit__`` resets.
    """

    def __init__(self, backend: PostgresBackend) -> None:
        self._backend = backend
        self._ops: list[Op] = []

    def queue(self, op: Op) -> None:
        self._ops.append(op)

    def __len__(self) -> int:
        return len(self._ops)

    def commit(self) -> list[Any]:
        ops, self._ops = self._ops, []
        if not ops:
            return []
        return self._backend._run_all(ops)

    def reset(self) -> None:
        """Discard everything queued (the pipeline's ``reset()``)."""
        self._ops = []

    rollback = reset

    def __enter__(self) -> PostgresUnitOfWork:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.reset()


# -- Backend --------------------------------------------------------------------


class PostgresBackend:
    """The Postgres :class:`~popoto.backends.Backend`; see the module
    docstring for the schema, the connection policy and the increment
    envelope. Families D-I still raise :class:`NotImplementedError`."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._conn: Any = None
        self._finalizer: Any = None

    # -- connection ----------------------------------------------------------

    def _connection(self) -> Any:
        """The instance's connection, opened (and the schema bootstrapped) on
        first use. The only place ``psycopg`` is imported."""
        if self._conn is None or self._conn.closed:
            import psycopg

            conn = psycopg.connect(self.url, autocommit=True)
            self._finalizer = weakref.finalize(self, _close_quietly, conn)
            for statement in SCHEMA_DDL:
                conn.execute(statement)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        """Close the connection; the next call reopens one."""
        if self._finalizer is not None:
            self._finalizer.detach()
            self._finalizer = None
        if self._conn is not None:
            _close_quietly(self._conn)
            self._conn = None

    def _run_all(self, ops: Sequence[Op]) -> list[Any]:
        """Run ``ops`` in order inside one transaction."""
        conn = self._connection()
        with conn.transaction():
            with conn.cursor() as cur:
                return [op(cur) for op in ops]

    def _run(self, op: Op, uow: UnitOfWork | None) -> Any:
        """Execute ``op`` now in its own transaction, or queue it on ``uow``
        and return ``None``."""
        if uow is None:
            return self._run_all([op])[0]
        if not isinstance(uow, PostgresUnitOfWork):
            raise TypeError(
                "PostgresBackend methods take the unit of work from "
                f"PostgresBackend.begin(), not {type(uow).__name__}"
            )
        uow.queue(op)
        return None

    def _query(self, sql: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
        """A read, outside any transaction block (autocommit)."""
        return list(self._connection().execute(sql, params).fetchall())

    # -- A. Unit of work ---------------------------------------------------

    def begin(self) -> UnitOfWork:
        return PostgresUnitOfWork(self)

    def native(self) -> Any:
        raise NotImplementedError(
            "native() is Redis-only in the backend-seam POC: the out-of-scope "
            "mixin code it serves has no Postgres path"
        )

    # -- B. Records ---------------------------------------------------------

    def save_record(
        self,
        key: str,
        fields: Mapping[Any, bytes],
        *,
        class_set: str,
        obsolete_key: str | None = None,
        ttl: int | None = None,
        expire_at: float | None = None,
        numeric: Mapping[str, float] | None = None,
        uow: UnitOfWork | None = None,
    ) -> Any:
        if ttl is not None or expire_at is not None:
            raise NotImplementedError(
                "record TTL (ttl=/expire_at=) is not implemented in the "
                "backend-seam POC: Postgres has no key expiry"
            )
        names = [_field_bytes(name) for name in fields]
        values = [bytes(value) for value in fields.values()]
        numeric_names = list(numeric) if numeric else []
        numeric_values = (
            [float(numeric[name]) for name in numeric_names] if numeric else []
        )

        locked = [key]
        if obsolete_key and obsolete_key != key:
            locked.append(obsolete_key)

        def op(cur: Any) -> Any:
            # First statement: serialise against increment_field / delete_record
            # on these keys (B1 in the #737 review). On the ``uow=`` path this
            # runs at commit(), inside the unit of work's transaction.
            _lock_record_keys(cur, locked)
            # Mirrors the Redis pipeline's reply: the HSET count of newly
            # created fields when any were given, else the SADD reply.
            reply = 0
            if names:
                rows = cur.execute(
                    "INSERT INTO popoto_record (key, field, value) "
                    "SELECT %s, f, v FROM unnest(%s::bytea[], %s::bytea[]) AS t(f, v) "
                    "ON CONFLICT (key, field) DO UPDATE SET value = EXCLUDED.value "
                    "RETURNING (xmax = 0)",
                    (key, names, values),
                ).fetchall()
                reply = sum(1 for (inserted,) in rows if inserted)
            if numeric_names:
                cur.execute(
                    "INSERT INTO popoto_numeric (key, field, value) "
                    "SELECT %s, f, v FROM unnest(%s::text[], %s::float8[]) AS t(f, v) "
                    "ON CONFLICT (key, field) DO UPDATE SET value = EXCLUDED.value",
                    (key, numeric_names, numeric_values),
                )
            cur.execute(
                "INSERT INTO popoto_set (idx, member) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (class_set, key),
            )
            if not names:
                reply = cur.rowcount
            if obsolete_key and obsolete_key != key:
                cur.execute(
                    "DELETE FROM popoto_set WHERE idx = %s AND member = %s",
                    (class_set, obsolete_key),
                )
                cur.execute("DELETE FROM popoto_record WHERE key = %s", (obsolete_key,))
                cur.execute(
                    "DELETE FROM popoto_numeric WHERE key = %s", (obsolete_key,)
                )
            return reply

        return self._run(op, uow)

    def load_record(self, key: str) -> dict[Any, bytes] | None:
        rows = self._query(
            "SELECT field, value FROM popoto_record WHERE key = %s", (key,)
        )
        if not rows:
            return None
        return {bytes(field): bytes(value) for field, value in rows}

    def load_records(self, keys: Sequence[str]) -> list[dict[Any, bytes] | None]:
        if not keys:
            return []
        rows = self._query(
            "SELECT key, field, value FROM popoto_record WHERE key = ANY(%s)",
            (list(dict.fromkeys(keys)),),
        )
        found: dict[str, dict[Any, bytes]] = {}
        for key, field, value in rows:
            found.setdefault(key, {})[bytes(field)] = bytes(value)
        # A fresh dict per position, as a pipelined HGETALL replies.
        return [dict(found[key]) if key in found else None for key in keys]

    def load_fields(self, key: str, names: Sequence[str]) -> list[bytes | None]:
        if not names:
            raise ValueError("load_fields() requires at least one field name")
        wanted = [_field_bytes(name) for name in names]
        rows = self._query(
            "SELECT field, value FROM popoto_record "
            "WHERE key = %s AND field = ANY(%s)",
            (key, wanted),
        )
        got = {bytes(field): bytes(value) for field, value in rows}
        return [got.get(name) for name in wanted]

    def record_exists(self, key: str) -> bool:
        rows = self._query(
            "SELECT EXISTS (SELECT 1 FROM popoto_record WHERE key = %s)", (key,)
        )
        return bool(rows[0][0])

    def records_exist(self, keys: Sequence[str]) -> list[bool]:
        raise _todo("records_exist")

    def delete_record(
        self, key: str, *, class_set: str, uow: UnitOfWork | None = None
    ) -> Any:
        def op(cur: Any) -> Any:
            _lock_record_keys(cur, [key])
            cur.execute("DELETE FROM popoto_record WHERE key = %s", (key,))
            existed = cur.rowcount > 0
            cur.execute("DELETE FROM popoto_numeric WHERE key = %s", (key,))
            cur.execute(
                "DELETE FROM popoto_set WHERE idx = %s AND member = %s",
                (class_set, key),
            )
            return existed

        return self._run(op, uow)

    def list_keys(self, class_set: str) -> set[str]:
        rows = self._query("SELECT member FROM popoto_set WHERE idx = %s", (class_set,))
        return {member for (member,) in rows}

    def count_records(self, class_set: str) -> int:
        rows = self._query(
            "SELECT count(*) FROM popoto_set WHERE idx = %s", (class_set,)
        )
        return int(rows[0][0])

    # -- C. Atomic increment -------------------------------------------------

    def increment_field(
        self,
        key: str,
        field: str,
        delta: int | float | Decimal,
        *,
        kind: Literal["int", "float", "decimal"],
        uow: UnitOfWork | None = None,
    ) -> int | float | Decimal | None:
        if kind not in ("int", "float", "decimal"):
            raise ValueError(
                f"increment_field kind must be int/float/decimal, got {kind!r}"
            )
        is_decimal = kind == "decimal"
        field_bytes = field.encode(ENCODING)
        # The Redis path sends ``str(delta)`` and the script ``tonumber``s it:
        # a double either way, so the arithmetic below is float arithmetic.
        delta_val = float(str(float(delta) if isinstance(delta, Decimal) else delta))

        def op(cur: Any) -> Any:
            # Serialise read-modify-write on this record (``FOR UPDATE`` cannot
            # lock a row that does not exist yet) -- against other increments
            # *and* against save_record / delete_record, which take the same
            # lock first.
            _lock_record_keys(cur, [key])
            row = cur.execute(
                "SELECT value FROM popoto_record WHERE key = %s AND field = %s "
                "FOR UPDATE",
                (key, field_bytes),
            ).fetchone()
            current = _current_value(bytes(row[0]) if row else None, is_decimal)
            new_val = current + delta_val
            cur.execute(
                "INSERT INTO popoto_record (key, field, value) VALUES (%s, %s, %s) "
                "ON CONFLICT (key, field) DO UPDATE SET value = EXCLUDED.value",
                (key, field_bytes, _pack_increment_result(new_val, is_decimal)),
            )
            cur.execute(
                "INSERT INTO popoto_numeric (key, field, value) VALUES (%s, %s, %s) "
                "ON CONFLICT (key, field) DO UPDATE SET value = EXCLUDED.value",
                (key, field, new_val),
            )
            return _lua_tostring(new_val)

        result_str = self._run(op, uow)
        if result_str is None:
            return None
        if kind == "int":
            return int(float(result_str))
        if kind == "decimal":
            return Decimal(result_str)
        return float(result_str)

    # -- D. Side maps --------------------------------------------------------

    def map_get(self, idx: str, member: str) -> bytes | None:
        raise _todo("map_get")

    def map_set(
        self,
        idx: str,
        member: str,
        value: bytes,
        *,
        only_if_absent: bool = False,
        uow: UnitOfWork | None = None,
    ) -> bool | None:
        raise _todo("map_set")

    def map_delete(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> int | None:
        raise _todo("map_delete")

    def map_scan(self, idx: str, pattern: str = "*") -> dict[str, bytes]:
        raise _todo("map_scan")

    # -- E. Set indexes ------------------------------------------------------

    def index_add(self, idx: str, member: str, *, uow: UnitOfWork | None = None) -> Any:
        raise _todo("index_add")

    def index_remove(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> Any:
        raise _todo("index_remove")

    def index_members(self, idx: str) -> set[str]:
        raise _todo("index_members")

    def index_union(self, idxs: Sequence[str]) -> set[str]:
        raise _todo("index_union")

    def index_intersection(self, idxs: Sequence[str]) -> set[str]:
        raise _todo("index_intersection")

    def scan_index_names(self, pattern: str) -> list[str]:
        raise _todo("scan_index_names")

    def scan_record_keys(self, pattern: str) -> list[str]:
        raise _todo("scan_record_keys")

    # -- F. Sorted indexes ---------------------------------------------------

    def sorted_add(
        self, idx: str, member: str, score: float, *, uow: UnitOfWork | None = None
    ) -> Any:
        raise _todo("sorted_add")

    def sorted_remove(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> Any:
        raise _todo("sorted_remove")

    def sorted_score(self, idx: str, member: str) -> float | None:
        raise _todo("sorted_score")

    def sorted_count(self, idx: str) -> int:
        raise _todo("sorted_count")

    def sorted_members(
        self, idx: str, start: int = 0, stop: int = -1, *, reverse: bool = False
    ) -> list[str]:
        raise _todo("sorted_members")

    def sorted_range(
        self,
        idx: str,
        lo: float,
        hi: float,
        *,
        lo_inclusive: bool = True,
        hi_inclusive: bool = True,
        reverse: bool = False,
        limit: int | None = None,
    ) -> list[str]:
        raise _todo("sorted_range")

    def sorted_increment(
        self, idx: str, member: str, delta: float, *, uow: UnitOfWork | None = None
    ) -> float | None:
        raise _todo("sorted_increment")

    # -- G. Atomic swaps -----------------------------------------------------

    def swap_index(
        self,
        record_key: str,
        field: str,
        new_idx: str,
        value: bytes,
        *,
        unique: bool,
        legacy_old_idx: str = "",
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("swap_index")

    def drop_index_entry(
        self,
        record_key: str,
        field: str,
        *,
        fallback_idx: str,
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("drop_index_entry")

    def swap_tags(
        self,
        record_key: str,
        field: str,
        new_idxs: Sequence[str],
        value: bytes,
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("swap_tags")

    def drop_tag_entries(
        self,
        record_key: str,
        field: str,
        *,
        fallback_idxs: Sequence[str],
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("drop_tag_entries")

    # -- H. Decay and confidence ---------------------------------------------

    def decayed_rank(
        self,
        idx: str,
        *,
        now: float,
        decay_rate: float,
        limit: int | None,
        base_score_field: str = "",
        confidence: tuple[str, float, float] | None = None,
        validity: tuple[str, str, float] | None = None,
        pretrim_max_ratio: float,
    ) -> list[Any]:
        raise _todo("decayed_rank")

    def confidence_update(
        self,
        idx: str,
        member: str,
        signal: float,
        *,
        initial: float,
        cap: int,
        require_record: str | None = None,
        uow: UnitOfWork | None = None,
    ) -> tuple[float, int, int, int] | None:
        raise _todo("confidence_update")

    # -- I. Validity ---------------------------------------------------------

    def supersede(
        self,
        model_prefix: str,
        field: str,
        *,
        mode: str,
        new_member: str,
        old_member: str = "",
        now: float,
        valid_from: float | None,
        ingested_at: float | None,
        close_at: float | None,
        assert_valid_from: bool,
        pointer_digest: str | None,
        uow: UnitOfWork | None = None,
    ) -> str | None:
        raise _todo("supersede")

    def interval_of(
        self, valid_idx: str, invalid_idx: str, member: str
    ) -> tuple[float | None, float | None]:
        raise _todo("interval_of")

    def interval_members(
        self,
        valid_idx: str,
        invalid_idx: str,
        as_of: float,
        *,
        select: Literal["valid", "excluded"],
    ) -> set[str]:
        raise _todo("interval_members")

    def drop_validity(
        self,
        model_prefix: str,
        field: str,
        member: str,
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("drop_validity")

    def open_pointer(self, model_prefix: str, field: str, digest: str) -> str | None:
        raise _todo("open_pointer")

    # -- J. Orphan purge and maintenance ------------------------------------

    def purge_orphan(
        self,
        record_key: str,
        refs: Sequence[tuple[str, Literal["sorted", "set"]]],
        *,
        uow: UnitOfWork | None = None,
    ) -> int | None:
        sorted_idxs = [idx for idx, kind in refs if kind == "sorted"]
        set_idxs = [idx for idx, kind in refs if kind != "sorted"]

        def op(cur: Any) -> Any:
            # The record key's advisory lock first, so a save_record of the
            # same key in flight on another instance commits before the gate
            # below reads (and the gate then sees the record and purges
            # nothing), rather than racing it. Then one statement, one
            # snapshot: the EXISTS gate and both DELETEs see the same state,
            # which is the Lua's "only if its record is still gone" rule.
            _lock_record_keys(cur, [record_key])
            row = cur.execute(
                "WITH gone AS ("
                "  SELECT NOT EXISTS (SELECT 1 FROM popoto_record WHERE key = %(key)s)"
                "  AS ok"
                "), sorted_removed AS ("
                "  DELETE FROM popoto_sorted"
                "  WHERE member = %(key)s AND idx = ANY(%(sorted)s)"
                "    AND (SELECT ok FROM gone)"
                "  RETURNING 1"
                "), set_removed AS ("
                "  DELETE FROM popoto_set"
                "  WHERE member = %(key)s AND idx = ANY(%(sets)s)"
                "    AND (SELECT ok FROM gone)"
                "  RETURNING 1"
                ") SELECT (SELECT count(*) FROM sorted_removed)"
                "       + (SELECT count(*) FROM set_removed)",
                {"key": record_key, "sorted": sorted_idxs, "sets": set_idxs},
            ).fetchone()
            return int(row[0])

        return self._run(op, uow)

    def scan_index_members(
        self, idx: str, kind: Literal["sorted", "set"]
    ) -> Iterator[str]:
        raise _todo("scan_index_members")

    def drop_index(
        self,
        idx: str,
        kind: Literal["sorted", "set", "map"],
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("drop_index")
