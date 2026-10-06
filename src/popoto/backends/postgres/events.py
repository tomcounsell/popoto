"""Event streams and consumer groups on Postgres (#759 M5).

``EventStreamMixin``'s mutation log and ``StreamConsumer``'s consumer groups,
on engine tables instead of a Redis stream -- written **in the save's own
transaction**, so the record and its mutation entry commit or roll back
together (on Redis the ``XADD`` rides the save's ``MULTI``/``EXEC``, which
applies queued commands without rollback).

Tables (one set per schema, created on first use like ``popoto_counter``):

``popoto_stream``
    One row per stream key: the last generated id (``last_ms``,
    ``last_seq``), ``length``, ``entries_added`` and the greatest id
    ``XDEL`` removed (``max_del_ms``, ``max_del_seq``). The row *is* the
    key's existence: an empty stream (``MKSTREAM``, or trimmed to nothing)
    keeps it and its last id, as Redis keeps an empty stream key. Its row
    lock is the stream's append lock, held to the end of the appending
    transaction, so ids commit in id order and every snapshot sees an id
    prefix of the stream -- what lets a group cursor advance without ever
    skipping an entry that commits later.
``popoto_stream_entry``
    ``(stream, ms, seq, fields bytea[], created_at)``: one row per entry,
    ``fields`` the flat ``k1, v1, k2, v2`` array in insertion order, the
    bytes ``XADD`` would have sent.
``popoto_stream_group`` / ``popoto_stream_consumer`` / ``popoto_stream_pending``
    A consumer group's cursor and logical read counter, its consumers, and
    its pending entries list (owner, last delivery time, delivery count).
``popoto_stream_attempt``
    ``StreamConsumer``'s handler-attempt counter (the ``_hattempts:`` hash).
``popoto_pubsub_listener``
    The live ``Subscriber`` registrations ``publish`` counts (:mod:`.pubsub`).

Ids are Redis's ``<ms>-<seq>``: ``ms`` is the server's millisecond clock and
``seq`` restarts at 0 for each new millisecond, monotonic per stream (a clock
that steps back keeps the last ``ms`` and bumps ``seq``). Commands answer in
redis-py's shapes -- ``bytes`` ids, ``{bytes: bytes}`` fields, the same
dicts for ``XPENDING`` and ``XINFO GROUPS`` -- and fail with the same message
text, as :class:`StreamCommandError` (the server's errors) or
:class:`StreamDataError` (redis-py's client-side checks).

Blocking reads (``XREADGROUP ... BLOCK``) wait on ``LISTEN``: every append
sends ``pg_notify`` on the schema's events channel inside its transaction, so
the notification arrives when the entry is visible. A ``LISTEN`` needs a
session, so the wait runs on a dedicated connection opened from
``POPOTO_POSTGRES_LISTEN_URL`` (else the backend's DSN) -- never a pooled
one, which PgBouncer in transaction mode would hand to another client between
statements. The wait is capped at :data:`STREAM_WAIT_POLL_SECONDS`, after
which the read runs again whether or not a notification came (the fallback
poll), and a dropped ``LISTEN`` connection is reopened on the next wait.

Never imports ``redis``; ``psycopg`` only through the backend.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import os
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union

from ..types import UnitOfWork

__all__ = [
    "EventsMixin",
    "EventListener",
    "AsyncStreamStore",
    "StreamAppend",
    "StreamCommandError",
    "StreamDataError",
    "StreamError",
    "StreamIdOutOfRange",
    "StreamStore",
    "LISTEN_URL_ENV",
    "STREAM_WAIT_POLL_SECONDS",
]

logger = logging.getLogger("POPOTO.postgres.events")

LISTEN_URL_ENV = "POPOTO_POSTGRES_LISTEN_URL"
"""A DSN for the dedicated ``LISTEN`` sessions (blocking stream reads and
``Subscriber``). Point it past a transaction-mode pooler at the server, or a
session-mode pool; unset, the backend's own DSN is used."""

STREAM_WAIT_POLL_SECONDS = 1.0
"""The fallback poll: a blocking read waits at most this long for a
notification before it reads again anyway. Bounds the delay a lost
notification (a ``LISTEN`` connection dropped and reopened) can cause."""

LISTEN_RECONNECT_BACKOFF_SECONDS = 0.2
"""Pause after a ``LISTEN`` connection fails, before the read retries."""

MAX_ID_PART = 2**63 - 1
"""``bigint``'s ceiling. Redis ids are unsigned 64-bit; ids past 2**63 - 1
are refused here with :class:`StreamIdOutOfRange` (no clock reaches them)."""

MAX_REDIS_ID_PART = 2**64 - 1
"""Redis's own ceiling: an id part past it is ``Invalid stream ID``."""

NOW_MS = "floor(extract(epoch from clock_timestamp()) * 1000)::bigint"

STREAM_TABLES: tuple[str, ...] = (
    "popoto_stream",
    "popoto_stream_entry",
    "popoto_stream_group",
    "popoto_stream_consumer",
    "popoto_stream_pending",
    "popoto_stream_attempt",
    "popoto_pubsub_listener",
)


def _table_bodies(q: Callable[[str], str]) -> dict[str, str]:
    """Column DDL per engine table; ``q`` qualifies a table name."""
    return {
        "popoto_stream": (
            "stream text PRIMARY KEY, last_ms bigint NOT NULL DEFAULT 0, "
            "last_seq bigint NOT NULL DEFAULT 0, length bigint NOT NULL DEFAULT 0, "
            "entries_added bigint NOT NULL DEFAULT 0, "
            "max_del_ms bigint NOT NULL DEFAULT 0, "
            "max_del_seq bigint NOT NULL DEFAULT 0, "
            "created_at timestamptz NOT NULL DEFAULT now()"
        ),
        "popoto_stream_entry": (
            f"stream text NOT NULL REFERENCES {q('popoto_stream')} (stream) "
            "ON DELETE CASCADE, ms bigint NOT NULL, seq bigint NOT NULL, "
            "fields bytea[] NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), "
            "PRIMARY KEY (stream, ms, seq)"
        ),
        "popoto_stream_group": (
            f"stream text NOT NULL REFERENCES {q('popoto_stream')} (stream) "
            "ON DELETE CASCADE, grp text NOT NULL, last_ms bigint NOT NULL, "
            "last_seq bigint NOT NULL, entries_read bigint, "
            "PRIMARY KEY (stream, grp)"
        ),
        "popoto_stream_consumer": (
            "stream text NOT NULL, grp text NOT NULL, consumer text NOT NULL, "
            "seen_ms bigint NOT NULL, active_ms bigint, "
            "PRIMARY KEY (stream, grp, consumer), FOREIGN KEY (stream, grp) "
            f"REFERENCES {q('popoto_stream_group')} (stream, grp) ON DELETE CASCADE"
        ),
        "popoto_stream_pending": (
            "stream text NOT NULL, grp text NOT NULL, ms bigint NOT NULL, "
            "seq bigint NOT NULL, consumer text NOT NULL, "
            "delivered_ms bigint NOT NULL, deliveries bigint NOT NULL, "
            "PRIMARY KEY (stream, grp, ms, seq), FOREIGN KEY (stream, grp) "
            f"REFERENCES {q('popoto_stream_group')} (stream, grp) ON DELETE CASCADE"
        ),
        "popoto_stream_attempt": (
            "stream text NOT NULL, grp text NOT NULL, entry text NOT NULL, "
            "attempts bigint NOT NULL, PRIMARY KEY (stream, grp, entry)"
        ),
        "popoto_pubsub_listener": (
            "pid integer NOT NULL, token text NOT NULL, pattern boolean NOT NULL, "
            "name text NOT NULL, regex text, "
            "PRIMARY KEY (pid, token, pattern, name)"
        ),
    }


# -- errors -----------------------------------------------------------------------


class StreamError(Exception):
    """Base of the stream and pub/sub errors the Postgres backend raises."""


class StreamCommandError(StreamError):
    """What the server would have replied with an error: the Postgres twin of
    redis-py's ``ResponseError``, with Redis's message text (``BUSYGROUP
    Consumer Group name already exists``, ``NOGROUP No such key ...``), so
    code matching on the text keeps working. The type differs: the Postgres
    backend never imports ``redis`` (a documented divergence)."""


class StreamIdOutOfRange(StreamCommandError):
    """An id whose part lies in ``(2**63 - 1, 2**64 - 1]``: a valid Redis id
    (Redis ids are unsigned 64-bit) that Postgres ``bigint`` cannot hold.
    Every stream command refuses one with this error, where Redis accepts it
    -- a documented divergence (``docs/features/postgres-backend.md``, "Event
    streams and pub/sub"). An id past ``2**64 - 1`` is not a Redis id at all
    and gets Redis's own ``Invalid stream ID`` text instead."""


class StreamDataError(StreamError, ValueError):
    """What redis-py refuses client-side before sending (``DataError``): a
    ``bool`` field value, an empty field mapping, a non-positive ``count``."""


# -- ids and values ---------------------------------------------------------------

StreamId = tuple[int, int]


def render_id(ms: int, seq: int) -> bytes:
    return f"{ms}-{seq}".encode()


def _text(value: Any, what: str = "key") -> str:
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StreamDataError(
                f"a stream {what} must be valid UTF-8 on Postgres: {value!r}"
            ) from exc
    elif not isinstance(value, str):
        value = str(value)
    if "\x00" in value:
        raise StreamDataError(
            f"a stream {what} cannot contain NUL (\\x00) on Postgres: {value!r}"
        )
    return value


def encode_value(value: Any) -> bytes:
    """What redis-py's ``Encoder.encode`` sends for one field or value."""
    if isinstance(value, (bytes, memoryview)):
        return bytes(value)
    if isinstance(value, bool):
        raise StreamDataError(
            "Invalid input of type: 'bool'. Convert to a bytes, string, int or "
            "float first."
        )
    if isinstance(value, (int, float)):
        return repr(value).encode()
    if isinstance(value, str):
        return value.encode("utf-8")
    raise StreamDataError(
        f"Invalid input of type: '{type(value).__name__}'. Convert to a bytes, "
        "string, int or float first."
    )


def flatten_fields(fields: Mapping[Any, Any]) -> list[bytes]:
    if not isinstance(fields, dict) or len(fields) == 0:
        raise StreamDataError("XADD fields must be a non-empty dict")
    flat: list[bytes] = []
    for key, value in fields.items():
        flat.append(encode_value(key))
        flat.append(encode_value(value))
    return flat


def _fields_dict(flat: Optional[Sequence[Any]]) -> dict[bytes, bytes]:
    if flat is None:
        return {}
    items = [bytes(x) for x in flat]
    return {items[i]: items[i + 1] for i in range(0, len(items) - 1, 2)}


def _invalid_id() -> StreamCommandError:
    return StreamCommandError("Invalid stream ID specified as stream command argument")


def _parse_parts(raw: str, missing_seq: Optional[int]) -> StreamId:
    if "-" in raw:
        ms_s, seq_s = raw.split("-", 1)
    else:
        if missing_seq is None:
            raise _invalid_id()
        ms_s, seq_s = raw, str(missing_seq)
    if not ms_s.isdigit() or not seq_s.isdigit():
        raise _invalid_id()
    ms, seq = int(ms_s), int(seq_s)
    if ms > MAX_REDIS_ID_PART or seq > MAX_REDIS_ID_PART:
        raise _invalid_id()  # past u64: Redis's strtoull refuses it too
    if ms > MAX_ID_PART or seq > MAX_ID_PART:
        raise StreamIdOutOfRange(
            "stream ids past 2**63 - 1 do not fit Postgres bigint (a documented "
            "divergence)"
        )
    return ms, seq


def parse_strict_id(value: Any) -> StreamId:
    """An id ``XACK``/``XCLAIM``/``XDEL`` accept: ``<ms>-<seq>`` or ``<ms>``."""
    return _parse_parts(_text(value, "id").strip(), 0)


def _incr(sid: StreamId) -> Optional[StreamId]:
    ms, seq = sid
    if seq < MAX_ID_PART:
        return ms, seq + 1
    if ms < MAX_ID_PART:
        return ms + 1, 0
    return None


def _decr(sid: StreamId) -> Optional[StreamId]:
    ms, seq = sid
    if seq > 0:
        return ms, seq - 1
    if ms > 0:
        return ms - 1, MAX_ID_PART
    return None


def parse_range_bound(value: Any, *, start: bool) -> Optional[StreamId]:
    """An ``XRANGE``/``XPENDING`` bound; ``None`` when an exclusive bound
    leaves nothing (``(0-0`` as an end)."""
    raw = _text(value, "id").strip()
    if raw == "-":
        return (0, 0)
    if raw == "+":
        return (MAX_ID_PART, MAX_ID_PART)
    exclusive = raw.startswith("(")
    if exclusive:
        raw = raw[1:]
        if raw in ("-", "+"):
            raise _invalid_id()
    sid = _parse_parts(raw, 0 if start else MAX_ID_PART)
    if not exclusive:
        return sid
    moved = _incr(sid) if start else _decr(sid)
    if moved is None:
        raise StreamCommandError(
            "invalid start ID for the interval"
            if start
            else "invalid end ID for the interval"
        )
    return moved


def _positive_count(count: Any, command: str) -> Optional[int]:
    if count is None:
        return None
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise StreamDataError(f"{command} count must be a positive integer")
    return count


# -- appends ------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamAppend:
    """One entry to append: what ``EventStreamMixin`` builds for a save,
    a delete or a custom event, handed to the backend so the append runs in
    the write's own transaction."""

    stream: str
    fields: tuple[bytes, ...]
    maxlen: Optional[int] = None
    approximate: bool = True

    @classmethod
    def of(
        cls,
        stream: Any,
        fields: Mapping[Any, Any],
        *,
        maxlen: Optional[int] = None,
        approximate: bool = True,
    ) -> "StreamAppend":
        return cls(_text(stream), tuple(flatten_fields(fields)), maxlen, approximate)


def stream_deletes(objs: Iterable[Any]) -> list[StreamAppend]:
    """The ``delete`` entry of each ``EventStreamMixin`` instance in
    ``objs``. Built strictly, as the Redis path builds it on the delete's
    pipeline: a model whose stream key cannot be built raises, and nothing is
    deleted."""
    from ...fields.event_stream import EventStreamMixin

    return [
        obj._stream_append("delete")
        for obj in objs
        if isinstance(obj, EventStreamMixin)
    ]


def _stream_hash(stream: str) -> str:
    """The ``pg_notify`` payload naming a stream: a fixed-size digest, so a
    key of any length fits the 8000-byte payload limit."""
    return hashlib.md5(stream.encode("utf-8")).hexdigest()


# -- the LISTEN session ---------------------------------------------------------------


class EventListener:
    """One dedicated ``LISTEN`` session (never a pooled connection) on a
    notification channel, reopened when it drops.

    :meth:`wait` returns ``True`` when a notification with one of the
    wanted payloads arrived, or when the connection failed (the caller
    should read again: notifications sent while it was down are lost, which
    is what the fallback poll covers), ``False`` on timeout."""

    def __init__(self, dsn: str, channel: str) -> None:
        self.dsn = dsn
        self.channel = channel
        self.conn: Any = None
        self.reconnects = 0
        self._finalizer: Any = None

    def _connect(self) -> None:
        from . import _import_psycopg
        from ...fields.constants import Defaults

        psycopg = _import_psycopg()
        conn = psycopg.connect(
            self.dsn,
            autocommit=True,
            connect_timeout=max(
                1, int(round(float(Defaults.PG_CONNECT_TIMEOUT_SECONDS)))
            ),
        )
        conn.execute(f'LISTEN "{self.channel}"')
        if self.conn is not None:
            self.reconnects += 1
        self.conn = conn
        if self._finalizer is not None:
            self._finalizer.detach()
        self._finalizer = weakref.finalize(self, _close_quietly, conn)

    def ensure(self) -> bool:
        """Open (or reopen) the session; ``True`` when it had to."""
        conn = self.conn
        if conn is not None and not conn.closed and not conn.broken:
            return False
        if conn is not None:
            _close_quietly(conn)
        self._connect()
        return True

    def wait(self, payloads: Iterable[str], timeout: float) -> bool:
        from . import _import_psycopg

        psycopg = _import_psycopg()
        wanted = set(payloads)
        try:
            if self.ensure():
                return True
            for notify in self.conn.notifies(timeout=max(timeout, 0.0)):
                if notify.payload in wanted:
                    return True
            return False
        except psycopg.OperationalError as exc:
            logger.warning(
                "popoto Postgres LISTEN session dropped (%s); reopening on the "
                "next wait",
                exc,
            )
            _close_quietly(self.conn)
            time.sleep(min(max(timeout, 0.0), LISTEN_RECONNECT_BACKOFF_SECONDS))
            return True

    def close(self) -> None:
        if self._finalizer is not None:
            self._finalizer.detach()
            self._finalizer = None
        _close_quietly(self.conn)
        self.conn = None


def _close_quietly(conn: Any) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:  # pragma: no cover - closing a dead socket
        pass


# -- the store --------------------------------------------------------------------------


class StreamStore:
    """The stream commands, redis-py shaped, on a Postgres backend's tables.

    Method names, arguments and replies follow redis-py's (``xadd``,
    ``xrange``, ``xreadgroup``, ``xpending_range``, ``xautoclaim`` ...), so
    code written against the Redis client reads the same stream on Postgres:
    ``popoto.streams.stream_client()`` returns the Redis client on Redis and
    this on Postgres. ``delete``/``exists`` act on stream keys only.
    """

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self._local = threading.local()

    # -- plumbing ------------------------------------------------------------------

    @property
    def t(self) -> dict[str, str]:
        return self.backend._events_ready()

    def _run(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        uow: Any = None,
        write: bool = False,
    ) -> list[tuple[Any, ...]]:
        rows, _ = self.backend._run(sql, params, uow=uow, write=write)
        return rows

    def _tx(self, work: Callable[[UnitOfWork], Any]) -> Any:
        return self.backend._atomically(work)

    def _listener(self) -> EventListener:
        listener = getattr(self._local, "listener", None)
        if listener is None:
            listener = self.backend.events_listener()
            self._local.listener = listener
        return listener

    # -- appends --------------------------------------------------------------------

    def append(self, item: StreamAppend, *, uow: Any = None) -> Optional[bytes]:
        """Append one entry with an auto id (``XADD key * ...``) in one
        message: lock the stream row, trim ahead of the append, notify,
        then upsert the stream row and insert the entry. ``uow`` runs it in
        that transaction; otherwise the message is its own."""
        t = self.t
        s = item.stream
        sql = [f"SELECT 1 FROM {t['popoto_stream']} WHERE stream = %s FOR UPDATE; "]
        params: list[Any] = [s]
        if item.maxlen is not None and item.maxlen >= 1:
            trim_sql, trim_params = self._trim_sql(
                s, keep_after_append=int(item.maxlen)
            )
            sql.append(trim_sql + "; ")
            params += trim_params
        sql.append("SELECT pg_notify(%s, %s); ")
        params += [self.backend.events_channel(), _stream_hash(s)]
        sql.append(
            f"WITH m AS (INSERT INTO {t['popoto_stream']} AS s "
            "(stream, last_ms, last_seq, length, entries_added) "
            f"SELECT %s, n.t, 0, 1, 1 FROM (SELECT {NOW_MS} AS t) n "
            "ON CONFLICT (stream) DO UPDATE SET "
            "last_ms = greatest(s.last_ms, EXCLUDED.last_ms), "
            "last_seq = CASE WHEN EXCLUDED.last_ms > s.last_ms THEN 0 "
            "ELSE s.last_seq + 1 END, "
            "length = s.length + 1, entries_added = s.entries_added + 1 "
            "RETURNING last_ms, last_seq) "
            f"INSERT INTO {t['popoto_stream_entry']} (stream, ms, seq, fields) "
            "SELECT %s, last_ms, last_seq, %s FROM m RETURNING ms, seq"
        )
        params += [s, s, list(item.fields)]
        if item.maxlen is not None and item.maxlen < 1:
            # MAXLEN 0: the entry is added (and counted) and trimmed at once;
            # the reply is still its id, read back from the trim.
            sql.append(
                f"; WITH d AS (DELETE FROM {t['popoto_stream_entry']} WHERE stream = %s "
                "RETURNING ms, seq), "
                f"u AS (UPDATE {t['popoto_stream']} SET length = 0 WHERE stream = %s) "
                "SELECT ms, seq FROM d ORDER BY ms DESC, seq DESC LIMIT 1"
            )
            params += [s, s]
        rows = self._run("".join(sql), params, uow=uow, write=True)
        return render_id(*rows[0]) if rows else None

    def _trim_sql(
        self,
        stream: str,
        *,
        keep_after_append: Optional[int] = None,
        maxlen: Optional[int] = None,
    ) -> tuple[str, list[Any]]:
        """Delete the oldest entries so that ``maxlen`` remain (or so that
        ``keep_after_append`` remain once one more is appended), keeping
        ``length`` in step. Exact, where Redis's ``~`` trims whole radix
        nodes and may keep more (a documented divergence)."""
        t = self.t
        extra = 1 if keep_after_append is not None else 0
        keep = keep_after_append if keep_after_append is not None else maxlen
        return (
            f"WITH d AS (DELETE FROM {t['popoto_stream_entry']} e WHERE e.stream = %s "
            "AND (e.ms, e.seq) IN (SELECT ms, seq FROM "
            f"{t['popoto_stream_entry']} WHERE stream = %s ORDER BY ms, seq LIMIT "
            f"greatest(coalesce((SELECT length FROM {t['popoto_stream']} WHERE "
            f"stream = %s), 0) + {extra} - %s, 0)) RETURNING 1) "
            f"UPDATE {t['popoto_stream']} SET length = length - (SELECT count(*) FROM d) "
            "WHERE stream = %s RETURNING (SELECT count(*) FROM d)",
            [stream, stream, stream, int(keep or 0), stream],
        )

    def xadd(
        self,
        name: Any,
        fields: Mapping[Any, Any],
        id: Any = "*",
        maxlen: Optional[int] = None,
        approximate: bool = True,
        nomkstream: bool = False,
        minid: Any = None,
        limit: Optional[int] = None,
    ) -> Optional[bytes]:
        """``XADD``. ``maxlen`` trims exactly, ``~`` or not (Redis's ``~``
        keeps whole radix nodes, so at least ``maxlen``); ``minid`` and
        ``limit`` are refused."""
        if minid is not None or limit is not None:
            raise StreamDataError(
                "XADD MINID/LIMIT is not supported on the Postgres backend"
            )
        if maxlen is not None and (not isinstance(maxlen, int) or maxlen < 0):
            raise StreamDataError("XADD maxlen must be non-negative")
        item = StreamAppend.of(name, fields, maxlen=maxlen, approximate=approximate)
        raw = _text(id, "id").strip()
        if raw != "*":
            # The id is checked before the key is looked up, as on Redis.
            if raw.endswith("-*"):
                if not raw[:-2].isdigit():
                    raise _invalid_id()
            elif _parse_parts(raw, 0) == (0, 0):
                raise StreamCommandError(
                    "The ID specified in XADD must be greater than 0-0"
                )
        if raw == "*" and not nomkstream:
            return self.append(item)
        return self._tx(lambda tx: self._append_explicit(item, raw, nomkstream, tx))

    def _append_explicit(
        self, item: StreamAppend, raw: str, nomkstream: bool, tx: UnitOfWork
    ) -> Optional[bytes]:
        t = self.t
        s = item.stream
        if not nomkstream:
            self._run(
                f"INSERT INTO {t['popoto_stream']} (stream) VALUES (%s) "
                "ON CONFLICT (stream) DO NOTHING",
                [s],
                uow=tx,
                write=True,
            )
        rows = self._run(
            f"SELECT last_ms, last_seq, {NOW_MS} FROM {t['popoto_stream']} "
            "WHERE stream = %s FOR UPDATE",
            [s],
            uow=tx,
        )
        if not rows:
            return None  # NOMKSTREAM on a missing key: nil, nothing created
        last: StreamId = (int(rows[0][0]), int(rows[0][1]))
        now = int(rows[0][2])
        if raw == "*":
            new: StreamId = (now, 0) if now > last[0] else (last[0], last[1] + 1)
        elif raw.endswith("-*"):
            ms_s = raw[:-2]
            if not ms_s.isdigit():
                raise _invalid_id()
            ms = int(ms_s)
            if ms > last[0]:
                new = (ms, 0)
            elif ms == last[0] and last != (0, 0):
                new = (ms, last[1] + 1)
            elif ms == 0 and last == (0, 0):
                new = (0, 1)
            else:
                raise StreamCommandError(
                    "The ID specified in XADD is equal or smaller than the target "
                    "stream top item"
                )
        else:
            new = _parse_parts(raw, 0)
            if new == (0, 0):
                raise StreamCommandError(
                    "The ID specified in XADD must be greater than 0-0"
                )
            if new <= last:
                raise StreamCommandError(
                    "The ID specified in XADD is equal or smaller than the target "
                    "stream top item"
                )
        sql = []
        params: list[Any] = []
        if item.maxlen is not None and item.maxlen >= 1:
            trim_sql, trim_params = self._trim_sql(
                s, keep_after_append=int(item.maxlen)
            )
            sql.append(trim_sql + "; ")
            params += trim_params
        sql.append("SELECT pg_notify(%s, %s); ")
        params += [self.backend.events_channel(), _stream_hash(s)]
        sql.append(
            f"UPDATE {t['popoto_stream']} SET last_ms = %s, last_seq = %s, "
            "length = length + 1, entries_added = entries_added + 1 WHERE stream = %s; "
            f"INSERT INTO {t['popoto_stream_entry']} (stream, ms, seq, fields) "
            "VALUES (%s, %s, %s, %s)"
        )
        params += [new[0], new[1], s, s, new[0], new[1], list(item.fields)]
        if item.maxlen is not None and item.maxlen < 1:
            sql.append(
                f"; DELETE FROM {t['popoto_stream_entry']} WHERE stream = %s; "
                f"UPDATE {t['popoto_stream']} SET length = 0 WHERE stream = %s"
            )
            params += [s, s]
        self._run("".join(sql), params, uow=tx, write=True)
        return render_id(*new)

    # -- reads -------------------------------------------------------------------------

    def _range(
        self,
        name: Any,
        lo: Optional[StreamId],
        hi: Optional[StreamId],
        count: Optional[int],
        reverse: bool,
    ) -> list[tuple[bytes, dict[bytes, bytes]]]:
        if lo is None or hi is None or lo > hi:
            return []
        t = self.t
        order = "DESC" if reverse else "ASC"
        sql = (
            f"SELECT ms, seq, fields FROM {t['popoto_stream_entry']} WHERE stream = %s "
            "AND (ms, seq) >= (%s, %s) AND (ms, seq) <= (%s, %s) "
            f"ORDER BY ms {order}, seq {order}"
        )
        params: list[Any] = [_text(name), lo[0], lo[1], hi[0], hi[1]]
        if count is not None:
            sql += " LIMIT %s"
            params.append(int(count))
        rows = self._run(sql, params)
        return [(render_id(ms, seq), _fields_dict(f)) for ms, seq, f in rows]

    def xrange(
        self, name: Any, min: Any = "-", max: Any = "+", count: Optional[int] = None
    ) -> list[tuple[bytes, dict[bytes, bytes]]]:
        count = _positive_count(count, "XRANGE")
        lo = parse_range_bound(min, start=True)
        hi = parse_range_bound(max, start=False)
        return self._range(name, lo, hi, count, reverse=False)

    def xrevrange(
        self, name: Any, max: Any = "+", min: Any = "-", count: Optional[int] = None
    ) -> list[tuple[bytes, dict[bytes, bytes]]]:
        count = _positive_count(count, "XREVRANGE")
        hi = parse_range_bound(max, start=False)
        lo = parse_range_bound(min, start=True)
        return self._range(name, lo, hi, count, reverse=True)

    def xlen(self, name: Any) -> int:
        rows = self._run(
            f"SELECT length FROM {self.t['popoto_stream']} WHERE stream = %s",
            [_text(name)],
        )
        return int(rows[0][0]) if rows else 0

    def exists(self, *names: Any) -> int:
        keys = [_text(n) for n in names]
        rows = self._run(
            f"SELECT count(*) FROM unnest(%s::text[]) AS u(k) "
            f"JOIN {self.t['popoto_stream']} s ON s.stream = u.k",
            [keys],
        )
        return int(rows[0][0])

    # -- deletes and trims ----------------------------------------------------------

    def delete(self, *names: Any) -> int:
        """``DEL`` of stream keys: the stream, its entries, groups, consumers
        and pending lists (cascaded). The handler-attempt counters are a key
        of their own on Redis and stay, as there."""
        if not names:
            return 0
        rows = self._run(
            f"WITH d AS (DELETE FROM {self.t['popoto_stream']} WHERE stream = "
            "ANY(%s::text[]) RETURNING 1) SELECT count(*) FROM d",
            [[_text(n) for n in names]],
            write=True,
        )
        return int(rows[0][0])

    def xdel(self, name: Any, *ids: Any) -> int:
        if not ids:
            return 0
        t = self.t
        s = _text(name)
        if not self.exists(s):
            return 0  # Redis answers a missing key before parsing the ids
        parsed = [parse_strict_id(i) for i in ids]
        rows = self._run(
            f"SELECT 1 FROM {t['popoto_stream']} WHERE stream = %s FOR UPDATE; "
            f"WITH d AS (DELETE FROM {t['popoto_stream_entry']} e USING "
            "unnest(%s::bigint[], %s::bigint[]) AS u(ms, seq) WHERE e.stream = %s "
            "AND e.ms = u.ms AND e.seq = u.seq RETURNING e.ms, e.seq), "
            "x AS (SELECT count(*) AS n, (SELECT ms FROM d ORDER BY ms DESC, seq DESC "
            "LIMIT 1) AS ms, (SELECT seq FROM d ORDER BY ms DESC, seq DESC LIMIT 1) "
            "AS seq FROM d) "
            f"UPDATE {t['popoto_stream']} s SET length = s.length - x.n, "
            "max_del_ms = CASE WHEN x.n > 0 AND (x.ms, x.seq) > (s.max_del_ms, "
            "s.max_del_seq) THEN x.ms ELSE s.max_del_ms END, "
            "max_del_seq = CASE WHEN x.n > 0 AND (x.ms, x.seq) > (s.max_del_ms, "
            "s.max_del_seq) THEN x.seq ELSE s.max_del_seq END "
            "FROM x WHERE s.stream = %s RETURNING x.n",
            [s, [p[0] for p in parsed], [p[1] for p in parsed], s, s],
            write=True,
        )
        return int(rows[0][0]) if rows else 0

    def xtrim(
        self,
        name: Any,
        maxlen: Optional[int] = None,
        approximate: bool = True,
        minid: Any = None,
        limit: Optional[int] = None,
    ) -> int:
        """``XTRIM``: exact on Postgres (``~`` or not; see :meth:`xadd`)."""
        if (maxlen is None) == (minid is None):
            raise StreamDataError(
                "Only one of ``maxlen`` or ``minid`` may be specified"
            )
        if limit is not None:
            raise StreamDataError(
                "XTRIM LIMIT is not supported on the Postgres backend"
            )
        t = self.t
        s = _text(name)
        if maxlen is not None:
            trim_sql, trim_params = self._trim_sql(s, maxlen=int(maxlen))
            rows = self._run(
                f"SELECT 1 FROM {t['popoto_stream']} WHERE stream = %s FOR UPDATE; "
                + trim_sql,
                [s] + trim_params,
                write=True,
            )
            return int(rows[0][0]) if rows else 0
        floor = parse_strict_id(minid)
        rows = self._run(
            f"SELECT 1 FROM {t['popoto_stream']} WHERE stream = %s FOR UPDATE; "
            f"WITH d AS (DELETE FROM {t['popoto_stream_entry']} WHERE stream = %s AND "
            "(ms, seq) < (%s, %s) RETURNING 1) "
            f"UPDATE {t['popoto_stream']} SET length = length - (SELECT count(*) FROM d) "
            "WHERE stream = %s RETURNING (SELECT count(*) FROM d)",
            [s, s, floor[0], floor[1], s],
            write=True,
        )
        return int(rows[0][0]) if rows else 0

    # -- consumer groups ------------------------------------------------------------

    def xgroup_create(
        self,
        name: Any,
        groupname: Any,
        id: Any = "$",
        mkstream: bool = False,
        entries_read: Optional[int] = None,
    ) -> bool:
        s, g = _text(name), _text(groupname, "group")
        t = self.t

        def work(tx: UnitOfWork) -> bool:
            if mkstream:
                self._run(
                    f"INSERT INTO {t['popoto_stream']} (stream) VALUES (%s) "
                    "ON CONFLICT (stream) DO NOTHING",
                    [s],
                    uow=tx,
                    write=True,
                )
            rows = self._run(
                f"SELECT last_ms, last_seq FROM {t['popoto_stream']} WHERE stream = %s "
                "FOR UPDATE",
                [s],
                uow=tx,
            )
            if not rows:
                raise StreamCommandError(
                    "The XGROUP subcommand requires the key to exist. Note that for "
                    "CREATE you may want to use the MKSTREAM option to create an "
                    "empty stream automatically."
                )
            raw = _text(id, "id").strip()
            start = (
                (int(rows[0][0]), int(rows[0][1]))
                if raw == "$"
                else parse_strict_id(raw)
            )
            created = self._run(
                f"INSERT INTO {t['popoto_stream_group']} (stream, grp, last_ms, "
                "last_seq, entries_read) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (stream, grp) DO NOTHING RETURNING 1",
                [s, g, start[0], start[1], entries_read],
                uow=tx,
                write=True,
            )
            if not created:
                raise StreamCommandError("BUSYGROUP Consumer Group name already exists")
            return True

        return bool(self._tx(work))

    def _require_key(self, s: str, tx: Any = None) -> None:
        rows = self._run(
            f"SELECT 1 FROM {self.t['popoto_stream']} WHERE stream = %s", [s], uow=tx
        )
        if not rows:
            raise StreamCommandError(
                "The XGROUP subcommand requires the key to exist. Note that for "
                "CREATE you may want to use the MKSTREAM option to create an "
                "empty stream automatically."
            )

    def xgroup_destroy(self, name: Any, groupname: Any) -> int:
        s = _text(name)
        self._require_key(s)
        rows = self._run(
            f"WITH d AS (DELETE FROM {self.t['popoto_stream_group']} WHERE stream = %s "
            "AND grp = %s RETURNING 1) SELECT count(*) FROM d",
            [s, _text(groupname, "group")],
            write=True,
        )
        return int(rows[0][0])

    def xgroup_setid(
        self, name: Any, groupname: Any, id: Any, entries_read: Optional[int] = None
    ) -> bool:
        s, g = _text(name), _text(groupname, "group")
        t = self.t
        raw = _text(id, "id").strip()

        def work(tx: UnitOfWork) -> bool:
            rows = self._run(
                f"SELECT last_ms, last_seq FROM {t['popoto_stream']} WHERE stream = %s",
                [s],
                uow=tx,
            )
            if not rows:
                raise StreamCommandError(
                    "The XGROUP subcommand requires the key to exist. Note that for "
                    "CREATE you may want to use the MKSTREAM option to create an "
                    "empty stream automatically."
                )
            sid = (
                (int(rows[0][0]), int(rows[0][1]))
                if raw == "$"
                else parse_strict_id(raw)
            )
            done = self._run(
                f"UPDATE {t['popoto_stream_group']} SET last_ms = %s, last_seq = %s, "
                "entries_read = %s WHERE stream = %s AND grp = %s RETURNING 1",
                [sid[0], sid[1], entries_read, s, g],
                uow=tx,
                write=True,
            )
            if not done:
                raise StreamCommandError(
                    f"NOGROUP No such consumer group '{g}' for key name '{s}'"
                )
            return True

        return bool(self._tx(work))

    def xgroup_createconsumer(
        self, name: Any, groupname: Any, consumername: Any
    ) -> int:
        s, g, c = (
            _text(name),
            _text(groupname, "group"),
            _text(consumername, "consumer"),
        )
        t = self.t

        def work(tx: UnitOfWork) -> int:
            self._require_key(s, tx)
            self._require_group(tx, s, g, "XGROUP")
            rows = self._run(
                f"INSERT INTO {t['popoto_stream_consumer']} (stream, grp, consumer, "
                f"seen_ms) VALUES (%s, %s, %s, {NOW_MS}) ON CONFLICT DO NOTHING "
                "RETURNING 1",
                [s, g, c],
                uow=tx,
                write=True,
            )
            return 1 if rows else 0

        return int(self._tx(work))

    def xgroup_delconsumer(self, name: Any, groupname: Any, consumername: Any) -> int:
        """Removes the consumer and its pending entries; returns how many it
        had pending."""
        s, g, c = (
            _text(name),
            _text(groupname, "group"),
            _text(consumername, "consumer"),
        )
        t = self.t

        def work(tx: UnitOfWork) -> int:
            self._require_key(s, tx)
            self._require_group(tx, s, g, "XGROUP")
            rows = self._run(
                f"WITH p AS (DELETE FROM {t['popoto_stream_pending']} WHERE stream = %s "
                "AND grp = %s AND consumer = %s RETURNING 1), "
                f"c AS (DELETE FROM {t['popoto_stream_consumer']} WHERE stream = %s AND "
                "grp = %s AND consumer = %s) SELECT count(*) FROM p",
                [s, g, c, s, g, c],
                uow=tx,
                write=True,
            )
            return int(rows[0][0])

        return int(self._tx(work))

    def _require_group(
        self, tx: Any, s: str, g: str, command: str, *, lock: bool = False
    ) -> tuple[Any, ...]:
        t = self.t
        rows = self._run(
            f"SELECT last_ms, last_seq, entries_read, {NOW_MS} FROM "
            f"{t['popoto_stream_group']} WHERE stream = %s AND grp = %s"
            + (" FOR UPDATE" if lock else ""),
            [s, g],
            uow=tx,
        )
        if not rows:
            if command == "XREADGROUP":
                raise StreamCommandError(
                    f"NOGROUP No such key '{s}' or consumer group '{g}' in XREADGROUP "
                    "with GROUP option"
                )
            if command == "XGROUP":
                raise StreamCommandError(
                    f"NOGROUP No such consumer group '{g}' for key name '{s}'"
                )
            raise StreamCommandError(
                f"NOGROUP No such key '{s}' or consumer group '{g}'"
            )
        return rows[0]

    def _stream_meta(self, tx: Any, s: str) -> Optional[dict[str, Any]]:
        t = self.t
        rows = self._run(
            "SELECT s.last_ms, s.last_seq, s.length, s.entries_added, s.max_del_ms, "
            "s.max_del_seq, f.ms, f.seq "
            f"FROM {t['popoto_stream']} s LEFT JOIN LATERAL (SELECT ms, seq FROM "
            f"{t['popoto_stream_entry']} e WHERE e.stream = s.stream ORDER BY ms, seq "
            "LIMIT 1) f ON TRUE WHERE s.stream = %s",
            [s],
            uow=tx,
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "last": (int(r[0]), int(r[1])),
            "length": int(r[2]),
            "added": int(r[3]),
            "max_del": (int(r[4]), int(r[5])),
            "first": (int(r[6]), int(r[7])) if r[6] is not None else (0, 0),
        }

    @staticmethod
    def _has_tombstones(meta: dict[str, Any], start: StreamId) -> bool:
        """Redis 8's ``streamRangeHasTombstones(s, start, NULL)``."""
        max_del = meta["max_del"]
        if meta["length"] == 0 or max_del == (0, 0):
            return False
        return start <= max_del

    @staticmethod
    def _estimate_read(meta: dict[str, Any], sid: StreamId) -> Optional[int]:
        """Redis's ``streamEstimateDistanceFromFirstEverEntry``: the logical
        read counter of ``sid``, or ``None`` when it cannot be known."""
        added, length = meta["added"], meta["length"]
        if not added:
            return 0
        if not length and sid <= meta["last"]:
            return added
        if sid != (0, 0) and sid < meta["max_del"]:
            return None
        if sid == meta["last"]:
            return added
        if sid > meta["last"]:
            return None
        max_del, first = meta["max_del"], meta["first"]
        if max_del == (0, 0) or max_del < first:
            if sid < first:
                return added - length
            if sid == first:
                return added - length + 1
        return None

    def xreadgroup(
        self,
        groupname: Any,
        consumername: Any,
        streams: Mapping[Any, Any],
        count: Optional[int] = None,
        block: Optional[int] = None,
        noack: bool = False,
        claim_min_idle_time: Optional[int] = None,
        *,
        listener: Optional[EventListener] = None,
    ) -> list[list[Any]]:
        """``XREADGROUP``: ``>`` delivers entries past the group's cursor
        (advanced under the group row's lock, so two consumers never get one
        entry) and adds them to the pending list; an explicit id re-reads
        this consumer's own pending entries after it. ``block`` (ms; ``0``
        waits forever) waits for an append's notification when a ``>`` read
        found nothing, re-reading at least every
        :data:`STREAM_WAIT_POLL_SECONDS`."""
        if claim_min_idle_time is not None:
            raise StreamDataError(
                "XREADGROUP CLAIM is not supported on the Postgres backend"
            )
        if not isinstance(streams, dict) or len(streams) == 0:
            raise StreamDataError("XREADGROUP streams must be a non empty dict")
        count = _positive_count(count, "XREADGROUP")
        g = _text(groupname, "group")
        c = _text(consumername, "consumer")
        targets = [(_text(k), _text(v, "id").strip()) for k, v in streams.items()]

        def read() -> list[list[Any]]:
            return self._tx(
                lambda tx: self._read_group(tx, g, c, targets, count, noack)
            )

        result = read()
        if result or block is None or any(sid != ">" for _, sid in targets):
            return result
        wait_for = listener or self._listener()
        payloads = [_stream_hash(k) for k, _ in targets]
        deadline = None if int(block) == 0 else time.monotonic() + int(block) / 1000.0
        # The session is listening from here on, so an append that commits
        # between the empty read above and the wait below is not missed.
        if wait_for.ensure():
            result = read()
            if result:
                return result
        while True:
            remaining = (
                STREAM_WAIT_POLL_SECONDS
                if deadline is None
                else deadline - time.monotonic()
            )
            if remaining <= 0:
                return []
            wait_for.wait(payloads, min(remaining, STREAM_WAIT_POLL_SECONDS))
            result = read()
            if result:
                return result

    def _read_group(
        self,
        tx: Any,
        g: str,
        c: str,
        targets: Sequence[tuple[str, str]],
        count: Optional[int],
        noack: bool,
    ) -> list[list[Any]]:
        t = self.t
        groups = {
            s: self._require_group(tx, s, g, "XREADGROUP", lock=True)
            for s, _ in targets
        }
        out: list[list[Any]] = []
        for s, sid in targets:
            last_ms, last_seq, entries_read, now = groups[s]
            now = int(now)
            self._run(
                f"INSERT INTO {t['popoto_stream_consumer']} AS c (stream, grp, consumer, "
                "seen_ms) VALUES (%s, %s, %s, %s) ON CONFLICT (stream, grp, consumer) "
                "DO UPDATE SET seen_ms = EXCLUDED.seen_ms",
                [s, g, c, now],
                uow=tx,
                write=True,
            )
            if sid == ">":
                sql = (
                    f"SELECT ms, seq, fields FROM {t['popoto_stream_entry']} WHERE "
                    "stream = %s AND (ms, seq) > (%s, %s) ORDER BY ms, seq"
                )
                params: list[Any] = [s, last_ms, last_seq]
                if count:
                    sql += " LIMIT %s"
                    params.append(count)
                rows = self._run(sql, params, uow=tx)
                if not rows:
                    continue
                meta = self._stream_meta(tx, s) or {}
                read_counter = None if entries_read is None else int(entries_read)
                cursor: StreamId = (int(last_ms), int(last_seq))
                for ms, seq, _ in rows:
                    # Redis 8's streamReplyWithRange, entry by entry.
                    sid_t = (int(ms), int(seq))
                    if (
                        read_counter is not None
                        and cursor >= meta["first"]
                        and not self._has_tombstones(meta, cursor)
                    ):
                        read_counter += 1
                    elif meta.get("added"):
                        read_counter = self._estimate_read(meta, sid_t)
                    cursor = sid_t
                newest = (int(rows[-1][0]), int(rows[-1][1]))
                stmts = (
                    f"UPDATE {t['popoto_stream_group']} SET last_ms = %s, last_seq = %s, "
                    "entries_read = %s WHERE stream = %s AND grp = %s; "
                    f"UPDATE {t['popoto_stream_consumer']} SET active_ms = %s WHERE "
                    "stream = %s AND grp = %s AND consumer = %s"
                )
                params = [newest[0], newest[1], read_counter, s, g, now, s, g, c]
                if not noack:
                    stmts += (
                        f"; INSERT INTO {t['popoto_stream_pending']} AS p (stream, grp, "
                        "ms, seq, consumer, delivered_ms, deliveries) SELECT %s, %s, "
                        "u.ms, u.seq, %s, %s, 1 FROM unnest(%s::bigint[], %s::bigint[]) "
                        "AS u(ms, seq) ON CONFLICT (stream, grp, ms, seq) DO UPDATE SET "
                        "consumer = EXCLUDED.consumer, delivered_ms = "
                        "EXCLUDED.delivered_ms, deliveries = 1"
                    )
                    params += [
                        s,
                        g,
                        c,
                        now,
                        [int(r[0]) for r in rows],
                        [int(r[1]) for r in rows],
                    ]
                self._run(stmts, params, uow=tx, write=True)
                out.append(
                    [
                        s.encode(),
                        [(render_id(ms, seq), _fields_dict(f)) for ms, seq, f in rows],
                    ]
                )
            else:
                # History: this consumer's own pending entries *after* the id.
                lo = _incr(parse_strict_id(sid)) or (MAX_ID_PART, MAX_ID_PART)
                sql = (
                    "SELECT p.ms, p.seq, e.fields FROM "
                    f"{t['popoto_stream_pending']} p LEFT JOIN {t['popoto_stream_entry']} "
                    "e ON e.stream = p.stream AND e.ms = p.ms AND e.seq = p.seq WHERE "
                    "p.stream = %s AND p.grp = %s AND p.consumer = %s AND (p.ms, p.seq) "
                    ">= (%s, %s) ORDER BY p.ms, p.seq"
                )
                params = [s, g, c, lo[0], lo[1]]
                if count:
                    sql += " LIMIT %s"
                    params.append(count)
                rows = self._run(sql, params, uow=tx)
                # A pending entry deleted from the stream comes back with no
                # fields and, as on Redis, is not counted as delivered again.
                live = [r for r in rows if r[2] is not None]
                if live:
                    self._run(
                        f"UPDATE {t['popoto_stream_pending']} p SET delivered_ms = %s, "
                        "deliveries = p.deliveries + 1 FROM unnest(%s::bigint[], "
                        "%s::bigint[]) AS u(ms, seq) WHERE p.stream = %s AND p.grp = %s "
                        "AND p.ms = u.ms AND p.seq = u.seq",
                        [
                            now,
                            [int(r[0]) for r in live],
                            [int(r[1]) for r in live],
                            s,
                            g,
                        ],
                        uow=tx,
                        write=True,
                    )
                out.append(
                    [
                        s.encode(),
                        [(render_id(ms, seq), _fields_dict(f)) for ms, seq, f in rows],
                    ]
                )
        return out

    def xack(self, name: Any, groupname: Any, *ids: Any) -> int:
        if not ids:
            raise StreamDataError("XACK requires at least one message ID")
        found = self._run(
            f"SELECT 1 FROM {self.t['popoto_stream_group']} WHERE stream = %s AND "
            "grp = %s",
            [_text(name), _text(groupname, "group")],
        )
        if not found:
            return 0  # no such key or group: 0, before the ids are parsed
        parsed = [parse_strict_id(i) for i in ids]
        rows = self._run(
            f"WITH d AS (DELETE FROM {self.t['popoto_stream_pending']} p USING "
            "unnest(%s::bigint[], %s::bigint[]) AS u(ms, seq) WHERE p.stream = %s AND "
            "p.grp = %s AND p.ms = u.ms AND p.seq = u.seq RETURNING 1) "
            "SELECT count(*) FROM d",
            [
                [p[0] for p in parsed],
                [p[1] for p in parsed],
                _text(name),
                _text(groupname, "group"),
            ],
            write=True,
        )
        return int(rows[0][0])

    def xpending(self, name: Any, groupname: Any) -> dict[str, Any]:
        s, g = _text(name), _text(groupname, "group")
        t = self.t

        def work(tx: UnitOfWork) -> dict[str, Any]:
            self._require_group(tx, s, g, "XPENDING")
            rows = self._run(
                f"SELECT ms, seq, consumer FROM {t['popoto_stream_pending']} WHERE "
                "stream = %s AND grp = %s ORDER BY ms, seq",
                [s, g],
                uow=tx,
            )
            return _pending_summary(rows)

        return dict(self._tx(work))

    def xpending_range(
        self,
        name: Any,
        groupname: Any,
        min: Any,
        max: Any,
        count: int,
        consumername: Any = None,
        idle: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        s, g = _text(name), _text(groupname, "group")
        lo = parse_range_bound(min, start=True)
        hi = parse_range_bound(max, start=False)
        t = self.t

        def work(tx: UnitOfWork) -> list[dict[str, Any]]:
            group = self._require_group(tx, s, g, "XPENDING")
            if lo is None or hi is None or lo > hi or not count or count <= 0:
                return []
            now = int(group[3])
            sql = (
                f"SELECT ms, seq, consumer, delivered_ms, deliveries FROM "
                f"{t['popoto_stream_pending']} WHERE stream = %s AND grp = %s AND "
                "(ms, seq) >= (%s, %s) AND (ms, seq) <= (%s, %s)"
            )
            params: list[Any] = [s, g, lo[0], lo[1], hi[0], hi[1]]
            if consumername is not None:
                sql += " AND consumer = %s"
                params.append(_text(consumername, "consumer"))
            if idle is not None:
                sql += " AND %s - delivered_ms >= %s"
                params += [now, int(idle)]
            sql += " ORDER BY ms, seq LIMIT %s"
            params.append(int(count))
            rows = self._run(sql, params, uow=tx)
            return [
                {
                    "message_id": render_id(ms, seq),
                    "consumer": consumer.encode(),
                    "time_since_delivered": max_(now - int(delivered), 0),
                    "times_delivered": int(deliveries),
                }
                for ms, seq, consumer, delivered, deliveries in rows
            ]

        return list(self._tx(work))

    def xclaim(
        self,
        name: Any,
        groupname: Any,
        consumername: Any,
        min_idle_time: int,
        message_ids: Sequence[Any],
        idle: Optional[int] = None,
        time: Optional[int] = None,
        retrycount: Optional[int] = None,
        force: bool = False,
        justid: bool = False,
    ) -> list[Any]:
        """``XCLAIM``: each listed pending entry idle at least
        ``min_idle_time`` ms moves to ``consumername``; an entry deleted
        from the stream leaves the pending list and is not returned. The
        rows are taken ``FOR UPDATE SKIP LOCKED``: one another claimer holds
        right now is passed over, as if already claimed."""
        if force:
            raise StreamDataError(
                "XCLAIM FORCE is not supported on the Postgres backend"
            )
        s, g = _text(name), _text(groupname, "group")
        c = _text(consumername, "consumer")
        t = self.t

        def work(tx: UnitOfWork) -> list[Any]:
            now = int(self._require_group(tx, s, g, "XCLAIM")[3])
            parsed = []
            for raw in message_ids:
                try:
                    parsed.append(parse_strict_id(raw))
                except StreamIdOutOfRange:
                    # A Redis id Postgres cannot hold: the bigint refusal,
                    # never "Unrecognized XCLAIM option" (the id is one).
                    raise
                except StreamCommandError:
                    # XCLAIM reads ids until one does not parse, then options.
                    raise StreamCommandError(
                        f"Unrecognized XCLAIM option '{_text(raw, 'id')}'"
                    ) from None
            self._touch_consumer(tx, s, g, c, now)
            if not parsed:
                return []
            rows = self._run(
                "SELECT p.ms, p.seq, p.delivered_ms, e.fields IS NULL, e.fields FROM "
                f"{t['popoto_stream_pending']} p JOIN unnest(%s::bigint[], %s::bigint[]) "
                "AS u(ms, seq) ON p.ms = u.ms AND p.seq = u.seq LEFT JOIN "
                f"{t['popoto_stream_entry']} e ON e.stream = p.stream AND e.ms = p.ms "
                "AND e.seq = p.seq WHERE p.stream = %s AND p.grp = %s "
                "ORDER BY p.ms, p.seq FOR UPDATE OF p SKIP LOCKED",
                [[p[0] for p in parsed], [p[1] for p in parsed], s, g],
                uow=tx,
            )
            found = {(int(r[0]), int(r[1])): r for r in rows}
            out: list[Any] = []
            gone: list[StreamId] = []
            claimed: list[StreamId] = []
            when = now if idle is None else now - int(idle)
            if time is not None:
                when = int(time)
            delivered_at = {sid: int(row[2]) for sid, row in found.items()}
            for sid in parsed:
                if sid not in found or sid in gone:
                    continue
                _, _, _, deleted, fields = found[sid]
                if deleted:
                    gone.append(sid)
                    continue
                if min_idle_time and now - delivered_at[sid] < int(min_idle_time):
                    continue
                claimed.append(sid)
                delivered_at[sid] = when
                out.append(
                    render_id(*sid)
                    if justid
                    else (render_id(*sid), _fields_dict(fields))
                )
            self._claim_rows(tx, s, g, c, claimed, when, justid, retrycount, gone)
            if claimed:
                self._run(
                    f"UPDATE {t['popoto_stream_consumer']} SET active_ms = %s WHERE "
                    "stream = %s AND grp = %s AND consumer = %s",
                    [now, s, g, c],
                    uow=tx,
                    write=True,
                )
            return out

        return list(self._tx(work))

    def _touch_consumer(self, tx: Any, s: str, g: str, c: str, now: int) -> None:
        self._run(
            f"INSERT INTO {self.t['popoto_stream_consumer']} (stream, grp, consumer, "
            "seen_ms) VALUES (%s, %s, %s, %s) ON CONFLICT (stream, grp, consumer) "
            "DO UPDATE SET seen_ms = EXCLUDED.seen_ms",
            [s, g, c, now],
            uow=tx,
            write=True,
        )

    def _claim_rows(
        self,
        tx: Any,
        s: str,
        g: str,
        c: str,
        claimed: Sequence[StreamId],
        when: int,
        justid: bool,
        retrycount: Optional[int],
        gone: Sequence[StreamId],
    ) -> None:
        t = self.t
        if gone:
            self._run(
                f"DELETE FROM {t['popoto_stream_pending']} p USING unnest(%s::bigint[], "
                "%s::bigint[]) AS u(ms, seq) WHERE p.stream = %s AND p.grp = %s AND "
                "p.ms = u.ms AND p.seq = u.seq",
                [[x[0] for x in gone], [x[1] for x in gone], s, g],
                uow=tx,
                write=True,
            )
        if not claimed:
            return
        # An id listed twice is claimed twice, as XCLAIM does: each claim
        # counts a delivery.
        times: dict[StreamId, int] = {}
        for sid in claimed:
            times[sid] = times.get(sid, 0) + 1
        if retrycount is not None:
            deliveries = "%s"
            extra: list[Any] = [int(retrycount)]
        else:
            deliveries = "p.deliveries" if justid else "p.deliveries + u.n"
            extra = []
        self._run(
            f"UPDATE {t['popoto_stream_pending']} p SET consumer = %s, delivered_ms = "
            f"%s, deliveries = {deliveries} FROM unnest(%s::bigint[], %s::bigint[], "
            "%s::bigint[]) AS u(ms, seq, n) WHERE p.stream = %s AND p.grp = %s AND "
            "p.ms = u.ms AND p.seq = u.seq",
            [c, when]
            + extra
            + [
                [x[0] for x in times],
                [x[1] for x in times],
                list(times.values()),
                s,
                g,
            ],
            uow=tx,
            write=True,
        )

    def xautoclaim(
        self,
        name: Any,
        groupname: Any,
        consumername: Any,
        min_idle_time: int,
        start_id: Any = "0-0",
        count: Optional[int] = None,
        justid: bool = False,
    ) -> list[Any]:
        """``XAUTOCLAIM``, scanning the pending list from ``start_id`` as
        Redis does (at most ``count * 10`` entries looked at, ``count``
        claimed or reported deleted). Replies ``[next_cursor, claimed,
        deleted]`` (``claimed`` ids alone with ``justid``, as redis-py
        returns it). Rows are taken ``FOR UPDATE SKIP LOCKED``."""
        s, g = _text(name), _text(groupname, "group")
        c = _text(consumername, "consumer")
        limit = 100 if count is None else int(count)
        if limit < 1:
            raise StreamCommandError("COUNT must be > 0")
        start = parse_range_bound(start_id, start=True) or (0, 0)
        attempts = limit * 10
        t = self.t

        def work(tx: UnitOfWork) -> list[Any]:
            now = int(self._require_group(tx, s, g, "XAUTOCLAIM")[3])
            self._touch_consumer(tx, s, g, c, now)
            rows = self._run(
                "SELECT p.ms, p.seq, p.delivered_ms, e.fields IS NULL, e.fields FROM "
                f"{t['popoto_stream_pending']} p LEFT JOIN {t['popoto_stream_entry']} e "
                "ON e.stream = p.stream AND e.ms = p.ms AND e.seq = p.seq WHERE "
                "p.stream = %s AND p.grp = %s AND (p.ms, p.seq) >= (%s, %s) ORDER BY "
                "p.ms, p.seq LIMIT %s FOR UPDATE OF p SKIP LOCKED",
                [s, g, start[0], start[1], attempts + 1],
                uow=tx,
            )
            remaining = limit
            budget = attempts
            claimed: list[StreamId] = []
            gone: list[StreamId] = []
            out: list[Any] = []
            index = 0
            while budget > 0 and remaining > 0 and index < len(rows):
                ms, seq, delivered, deleted, fields = rows[index]
                index += 1
                budget -= 1
                sid = (int(ms), int(seq))
                if deleted:
                    gone.append(sid)
                    remaining -= 1
                    continue
                if min_idle_time and now - int(delivered) < int(min_idle_time):
                    continue
                claimed.append(sid)
                out.append(
                    render_id(*sid)
                    if justid
                    else (render_id(*sid), _fields_dict(fields))
                )
                remaining -= 1
            cursor = (
                render_id(int(rows[index][0]), int(rows[index][1]))
                if index < len(rows)
                else b"0-0"
            )
            self._claim_rows(tx, s, g, c, claimed, now, justid, None, gone)
            if claimed:
                self._run(
                    f"UPDATE {t['popoto_stream_consumer']} SET active_ms = %s WHERE "
                    "stream = %s AND grp = %s AND consumer = %s",
                    [now, s, g, c],
                    uow=tx,
                    write=True,
                )
            if justid:
                return out
            return [cursor, out, [render_id(*x) for x in gone]]

        return list(self._tx(work))

    def xinfo_groups(self, name: Any) -> list[dict[str, Any]]:
        s = _text(name)
        t = self.t

        def work(tx: UnitOfWork) -> list[dict[str, Any]]:
            meta = self._stream_meta(tx, s)
            if meta is None:
                raise StreamCommandError("no such key")
            rows = self._run(
                "SELECT g.grp, g.last_ms, g.last_seq, g.entries_read, "
                f"(SELECT count(*) FROM {t['popoto_stream_consumer']} c WHERE c.stream = "
                "g.stream AND c.grp = g.grp), "
                f"(SELECT count(*) FROM {t['popoto_stream_pending']} p WHERE p.stream = "
                "g.stream AND p.grp = g.grp) "
                f"FROM {t['popoto_stream_group']} g WHERE g.stream = %s "
                'ORDER BY g.grp COLLATE "C"',
                [s],
                uow=tx,
            )
            out = []
            for grp, last_ms, last_seq, entries_read, consumers, pending in rows:
                last = (int(last_ms), int(last_seq))
                out.append(
                    {
                        "name": grp.encode(),
                        "consumers": int(consumers),
                        "pending": int(pending),
                        "last-delivered-id": render_id(*last),
                        "entries-read": (
                            None if entries_read is None else int(entries_read)
                        ),
                        "lag": self._lag(meta, last, entries_read),
                    }
                )
            return out

        return list(self._tx(work))

    def _lag(
        self, meta: dict[str, Any], last: StreamId, entries_read: Any
    ) -> Optional[int]:
        """Redis 8's ``XINFO GROUPS`` lag rule (``streamReplyWithCGLag``)."""
        if not meta["added"] or not meta["length"]:
            return 0
        if last < meta["first"] and meta["max_del"] < meta["first"]:
            # The trimming overtook the group: every entry left is unread.
            return meta["length"]
        if entries_read is not None and not self._has_tombstones(meta, last):
            return meta["added"] - int(entries_read)
        estimate = self._estimate_read(meta, last)
        return None if estimate is None else meta["added"] - estimate

    def xinfo_consumers(self, name: Any, groupname: Any) -> list[dict[str, Any]]:
        s, g = _text(name), _text(groupname, "group")
        t = self.t

        def work(tx: UnitOfWork) -> list[dict[str, Any]]:
            # Redis's two texts: "no such key" for a missing stream, then
            # NOGROUP worded as XGROUP words it for a missing group.
            if self._stream_meta(tx, s) is None:
                raise StreamCommandError("no such key")
            now = int(self._require_group(tx, s, g, "XGROUP")[3])
            rows = self._run(
                "SELECT c.consumer, c.seen_ms, c.active_ms, (SELECT count(*) FROM "
                f"{t['popoto_stream_pending']} p WHERE p.stream = c.stream AND p.grp = "
                "c.grp AND p.consumer = c.consumer) FROM "
                f"{t['popoto_stream_consumer']} c WHERE c.stream = %s AND c.grp = %s "
                'ORDER BY c.consumer COLLATE "C"',
                [s, g],
                uow=tx,
            )
            return [
                {
                    "name": consumer.encode(),
                    "pending": int(pending),
                    "idle": max_(now - int(seen), 0),
                    "inactive": -1 if active is None else max_(now - int(active), 0),
                }
                for consumer, seen, active, pending in rows
            ]

        return list(self._tx(work))

    # -- StreamConsumer's handler-attempt counter --------------------------------------

    def attempts_get(self, stream: Any, group: Any, entry: Any) -> Optional[int]:
        rows = self._run(
            f"SELECT attempts FROM {self.t['popoto_stream_attempt']} WHERE stream = %s "
            "AND grp = %s AND entry = %s",
            [_text(stream), _text(group, "group"), _text(entry, "id")],
        )
        return int(rows[0][0]) if rows else None

    def attempts_incr(self, stream: Any, group: Any, entry: Any, by: int = 1) -> int:
        rows = self._run(
            f"INSERT INTO {self.t['popoto_stream_attempt']} AS a (stream, grp, entry, "
            "attempts) VALUES (%s, %s, %s, %s) ON CONFLICT (stream, grp, entry) DO "
            "UPDATE SET attempts = a.attempts + EXCLUDED.attempts RETURNING attempts",
            [_text(stream), _text(group, "group"), _text(entry, "id"), int(by)],
            write=True,
        )
        return int(rows[0][0])

    def attempts_drop(self, stream: Any, group: Any, *entries: Any) -> int:
        if not entries:
            return 0
        rows = self._run(
            f"WITH d AS (DELETE FROM {self.t['popoto_stream_attempt']} WHERE stream = %s "
            "AND grp = %s AND entry = ANY(%s::text[]) RETURNING 1) SELECT count(*) FROM d",
            [_text(stream), _text(group, "group"), [_text(e, "id") for e in entries]],
            write=True,
        )
        return int(rows[0][0])

    def close(self) -> None:
        listener = getattr(self._local, "listener", None)
        if listener is not None:
            listener.close()
            self._local.listener = None


def max_(a: int, b: int) -> int:
    return a if a >= b else b


def _pending_summary(rows: Sequence[tuple[Any, ...]]) -> dict[str, Any]:
    if not rows:
        return {"pending": 0, "min": None, "max": None, "consumers": []}
    per: dict[str, int] = {}
    for _, _, consumer in rows:
        per[consumer] = per.get(consumer, 0) + 1
    return {
        "pending": len(rows),
        "min": render_id(int(rows[0][0]), int(rows[0][1])),
        "max": render_id(int(rows[-1][0]), int(rows[-1][1])),
        "consumers": [
            {"name": name.encode(), "pending": per[name]}
            for name in sorted(per, key=lambda n: n.encode())
        ],
    }


# -- the async face (StreamConsumer) ------------------------------------------------------


class AsyncStreamStore:
    """The awaitable twin of :class:`StreamStore` that ``StreamConsumer``
    drives: each command runs the sync one in a worker thread
    (``asyncio.to_thread``), and blocking reads wait on this instance's own
    ``LISTEN`` session, so two consumers never share one."""

    is_postgres_stream_store = True

    def __init__(self, store: StreamStore) -> None:
        self.store = store
        self.listener: Optional[EventListener] = None

    def _listener(self) -> EventListener:
        if self.listener is None:
            self.listener = self.store.backend.events_listener()
        return self.listener

    async def xreadgroup(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("listener", self._listener())
        return await asyncio.to_thread(self.store.xreadgroup, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.store, name)
        if not callable(method):
            return method

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(method, *args, **kwargs)

        return call

    def close(self) -> None:
        if self.listener is not None:
            self.listener.close()
            self.listener = None


# -- the backend mixin ------------------------------------------------------------------


class EventsMixin:
    """Stream and pub/sub plumbing for
    :class:`~popoto.backends.postgres.PostgresBackend`."""

    schema: str
    dsn: str
    _run: Callable[..., tuple[list[tuple[Any, ...]], int]]
    _atomically: Callable[..., Any]

    def _events_ready(self) -> dict[str, str]:
        """The engine tables' qualified names, created on first use in this
        process (and again after ``forget_tables``)."""
        from . import _schema_auto
        from .schema import engine_table_ddl, quote_ident

        q = lambda name: f"{quote_ident(self.schema)}.{quote_ident(name)}"  # noqa: E731
        names = {name: q(name) for name in STREAM_TABLES}
        ready: set[str] = self.__dict__.setdefault("_engine_ready", set())
        if "popoto_stream" in ready:
            return names
        # First-use DDL commits on its own connection (#776).
        with self.second_connection_ok():  # type: ignore[attr-defined]
            rows, _ = self._run(
                "SELECT count(*) FROM pg_tables WHERE schemaname = %s AND "
                "tablename = ANY(%s::text[])",
                [self.schema, list(STREAM_TABLES)],
            )
            if int(rows[0][0]) < len(STREAM_TABLES):
                if not _schema_auto():
                    from ..types import SchemaDriftError

                    raise SchemaDriftError(
                        f"{self.schema}.popoto_stream tables do not exist and "
                        "POPOTO_SCHEMA_AUTO=0"
                    )
                bodies = _table_bodies(q)
                sql, params = engine_table_ddl(
                    self.schema, STREAM_TABLES[0], bodies[STREAM_TABLES[0]]
                )
                for name in STREAM_TABLES[1:]:
                    sql += f"; CREATE TABLE IF NOT EXISTS {q(name)} ({bodies[name]})"
                sql += (
                    f"; CREATE INDEX IF NOT EXISTS {quote_ident('popoto_stream_pending_owner')} "
                    f"ON {q('popoto_stream_pending')} (stream, grp, consumer)"
                )
                self._run(sql, params, write=True)
        ready.add("popoto_stream")
        return names

    def streams(self) -> StreamStore:
        """The redis-py-shaped stream commands on this backend."""
        store = self.__dict__.get("_stream_store")
        if store is None:
            store = StreamStore(self)
            self.__dict__["_stream_store"] = store
        return store

    def async_streams(self) -> AsyncStreamStore:
        """A fresh awaitable stream client with its own ``LISTEN`` session."""
        return AsyncStreamStore(self.streams())

    @property
    def listen_dsn(self) -> str:
        return os.environ.get(LISTEN_URL_ENV, "").strip() or self.dsn

    def events_channel(self) -> str:
        return "popoto_events_" + hashlib.md5(self.schema.encode()).hexdigest()[:20]

    def events_listener(self) -> EventListener:
        return EventListener(self.listen_dsn, self.events_channel())

    # -- appends from the model layer ---------------------------------------------------

    def _append_entries(self, entries: Sequence[StreamAppend], uow: Any) -> None:
        """Append ``entries`` now, in ``uow``, sorted by stream key (stably):
        the same global lock order :meth:`PostgresUnitOfWork.defer_stream_append`
        keeps, so a multi-stream write cannot deadlock another."""
        store = self.streams()
        for item in sorted(entries, key=lambda e: e.stream):
            store.append(item, uow=uow)

    def _defer_entries(self, entries: Sequence[StreamAppend], pg: Any) -> None:
        """Queue ``entries`` for ``pg``'s ``COMMIT``, where they are appended
        in stream-key order after every record lock the unit takes."""
        store = self.streams()
        for item in entries:
            pg.defer_stream_append(
                item.stream, functools.partial(store.append, item, uow=pg)
            )

    def stream_append(
        self, item: StreamAppend, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[bytes]:
        """Append ``item`` now, or -- inside the backend's unit of work --
        just before that transaction commits
        (:meth:`PostgresUnitOfWork.defer_stream_append`), so the stream locks
        are the last the transaction takes, in stream-key order."""
        from . import _pg_uow

        self._events_ready()
        pg = _pg_uow(uow)
        if pg is not None:
            self._defer_entries([item], pg)
            return None
        return self.streams().append(item)

    def stream_append_all(
        self, entries: Sequence[StreamAppend], *, uow: Optional[UnitOfWork] = None
    ) -> None:
        for item in entries:
            self.stream_append(item, uow=uow)

    def _write_with_stream(
        self,
        sql: str,
        params: Sequence[Any],
        uow: Optional[UnitOfWork],
        entries: Sequence[StreamAppend],
    ) -> tuple[list[tuple[Any, ...]], int]:
        """Run a record write and append its stream entries in **one**
        transaction: the caller's (the entries go in just before its
        ``COMMIT``), or one of its own (the write, then the appends). With no
        entries this is ``_run``."""
        if not entries:
            return self._run(sql, params, uow=uow, write=True)
        from . import _pg_uow

        self._events_ready()
        pg = _pg_uow(uow)
        if pg is not None:
            out = self._run(sql, params, uow=pg, write=True)
            self._defer_entries(entries, pg)
            return out

        def work(tx: UnitOfWork) -> tuple[list[tuple[Any, ...]], int]:
            out = self._run(sql, params, uow=tx, write=True)
            self._append_entries(entries, tx)
            return out

        return self._atomically(work)

    # -- pub/sub ---------------------------------------------------------------------------

    def pubsub(self) -> Any:
        """A redis-py-shaped ``PubSub`` on a dedicated ``LISTEN`` session."""
        from .pubsub import PostgresPubSub

        self._events_ready()
        return PostgresPubSub(self)

    def publish(
        self,
        channel: Any,
        message: Union[bytes, str],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> int:
        """``PUBLISH``: one ``pg_notify`` (delivered at commit -- the
        caller's, inside its unit of work) and the number of live
        subscriptions it reaches."""
        from .pubsub import publish

        return publish(self, channel, message, uow=uow)
