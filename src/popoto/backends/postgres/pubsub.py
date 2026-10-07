"""Pub/sub on Postgres: ``PUBLISH`` as ``pg_notify``, ``SUBSCRIBE`` as
``LISTEN`` (#759 M5).

One notification channel per schema (``popoto_pubsub_<digest>``) carries
every popoto channel: the payload names the channel and carries the message,
and each subscriber keeps what its subscriptions match. So a channel name is
not bound by Postgres's 63-byte identifier limit, and pattern subscriptions
(``PSUBSCRIBE``) are matched **client-side**, with Redis's glob rules
(``*``, ``?``, ``[...]``, ``[^...]``, ``\\`` escapes) translated to a regular
expression and matched byte-wise, as Redis's ``stringmatchlen`` matches
(:func:`glob_match`). The cost: every subscriber of the schema receives every
message and drops what it did not ask for.

Each payload starts with a per-publish nonce, stripped before delivery:
Postgres folds identical notifications sent in one transaction into one, so
without it a message published twice in a transaction would arrive once.

``NOTIFY`` is transactional: a message published inside the backend's unit
of work (``pipeline=uow``) is delivered when that transaction commits and
never if it rolls back -- the Postgres reading of "publish atomically with the
save". It is also bounded: a payload must be shorter than **8000 bytes**. The
message travels base64-encoded beside the channel name, so a message of
roughly 5.9 KB is the most one notification holds; a larger one is refused
with :class:`PubSubPayloadTooLarge` before anything is sent, rather than
split (a split message could be half-delivered to a subscriber that joins
between its parts). Publish a key and keep the body in a record.

``publish`` returns the number of live subscriptions the message reaches,
like ``PUBLISH``: each subscriber registers its subscriptions in
``popoto_pubsub_listener`` under its session's backend pid, and the count
keeps the rows whose pid is a live session (``pg_stat_activity``). A
subscriber that closes deletes its rows; one that dies leaves rows that stop
counting when its session ends and are swept by the next subscriber to
connect. The ``LISTEN`` session is never pooled (§3 Topology): every
subscriber in a process shares one per DSN (:mod:`.listen`, #799), and it
must reach the same server as the backend's DSN.
"""

from __future__ import annotations

import base64
import binascii
import collections
import contextlib
import hashlib
import itertools
import logging
import os
import re
import threading
import uuid
import weakref
from typing import Any, Callable, Iterator, Optional, Union

from ...exceptions import PublisherException
from ..types import UnitOfWork
from .events import _text, encode_value
from .listen import QueueSink

__all__ = [
    "NOTIFY_PAYLOAD_LIMIT",
    "PostgresPubSub",
    "PubSubPayloadTooLarge",
    "as_bytes_text",
    "glob_match",
    "glob_to_regex",
    "publish",
]

logger = logging.getLogger("POPOTO.postgres.pubsub")

NOTIFY_PAYLOAD_LIMIT = 8000
"""Postgres's ``NOTIFY`` payload must be shorter than this many bytes (the
server's default build)."""


class PubSubPayloadTooLarge(PublisherException, ValueError):
    """A message too large for one ``NOTIFY`` on the Postgres backend."""


def pubsub_channel(schema: str) -> str:
    return "popoto_pubsub_" + hashlib.md5(schema.encode()).hexdigest()[:20]


def glob_to_regex(pattern: str) -> str:
    """Redis's ``stringmatchlen`` glob as a regular expression body that
    means the same in Python ``re`` (with ``DOTALL``) and in Postgres's
    ``~``; the caller anchors it. ``*`` any run, ``?`` one character,
    ``[abc]``/``[^abc]``/``[a-z]`` a class, ``\\x`` a literal ``x``.

    Redis matches **bytes**. To mean what it means on a non-ASCII channel,
    translate and match the *byte* forms (:func:`as_bytes_text`: UTF-8,
    then each byte as one character) -- then ``?`` and ``[^...]`` consume
    one byte, and a range compares bytes as ``stringmatchlen`` does (as
    signed ``char``, :func:`_range_items`). :func:`glob_match` does both."""
    out: list[str] = []
    i, n = 0, len(pattern)
    special = set(".^$*+?()[]{}|\\-/")

    def lit(ch: str) -> str:
        return "\\" + ch if ch in special else ch

    while i < n:
        ch = pattern[i]
        if ch == "*":
            while i + 1 < n and pattern[i + 1] == "*":
                i += 1
            out.append(".*")
        elif ch == "?":
            out.append(".")
        elif ch == "\\" and i + 1 < n:
            i += 1
            out.append(lit(pattern[i]))
        elif ch == "[":
            j = i + 1
            negate = j < n and pattern[j] == "^"
            if negate:
                j += 1
            items: list[str] = []
            closed = False
            while j < n:
                c = pattern[j]
                if c == "\\" and j + 1 < n:
                    j += 1
                    items.append(lit(pattern[j]))
                elif c == "]":
                    closed = True
                    break
                elif j + 2 < n and pattern[j + 1] == "-":
                    # stringmatchlen: any "x-y" is a range, "]" included as
                    # its end ("[a-]" ranges a..] and leaves the class open).
                    items.extend(_range_items(c, pattern[j + 2], lit))
                    j += 2
                else:
                    items.append(lit(c))
                j += 1
            body = "".join(items)
            if not body:
                # "[]" matches nothing; "[^]" anything.
                out.append("." if negate else "(?!)")
            else:
                out.append(("[^" if negate else "[") + body + "]")
            i = j if closed else n - 1
        else:
            out.append(lit(ch))
        i += 1
    return "".join(out)


def _signed(ch: str) -> int:
    """A byte-character's value as Redis compares it: ``stringmatchlen``
    reads the range ends and the string byte as C ``char``, which is signed
    on the x86-64 and Apple-silicon builds Redis ships for, so bytes
    0x80-0xFF sort *below* 0x00. Characters past U+00FF (a pattern not in
    byte form) keep their code point."""
    code = ord(ch)
    return code - 256 if 0x80 <= code <= 0xFF else code


def _range_items(lo: str, hi: str, lit: Callable[[str], str]) -> list[str]:
    """The class items for the range ``lo-hi`` in Redis's comparison order
    (:func:`_signed`; the ends swapped when reversed). A signed range that
    crosses zero is two byte ranges: ``0x80+..0xFF`` and ``0x01..`` (a
    channel never holds NUL, and Postgres ``text`` -- where the listener
    table stores the regex -- cannot)."""
    a, b = _signed(lo), _signed(hi)
    if a > b:
        a, b = b, a
    spans = (
        [(a + 256, 0xFF), (1, b)]
        if a < 0 <= b
        else [(a % 256, b % 256)] if -128 <= a and b <= 0xFF else [(a, b)]
    )
    return [f"{lit(chr(x))}-{lit(chr(y))}" for x, y in spans]


def as_bytes_text(value: Union[str, bytes]) -> str:
    """``value``'s UTF-8 bytes, one character per byte (``latin-1``): the
    form a glob and a channel are matched in, so the match is byte-wise like
    Redis's. Every byte maps to a character Postgres ``text`` can hold
    (U+0001..U+00FF; a channel has no NUL)."""
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return raw.decode("latin-1")


def glob_match(pattern: Union[str, bytes], channel: Union[str, bytes]) -> bool:
    """Whether ``channel`` matches the ``PSUBSCRIBE`` glob ``pattern`` as
    Redis's ``stringmatchlen`` decides it, byte for byte."""
    regex = re.compile(glob_to_regex(as_bytes_text(pattern)), re.DOTALL)
    return regex.fullmatch(as_bytes_text(channel)) is not None


_NONCE_WIDTH = 8
_nonce = itertools.count()


def _next_nonce() -> str:
    """A per-publish tag, fixed width so the payload limit does not move.
    Postgres folds identical ``(channel, payload)`` notifications sent in one
    transaction into one; the tag makes every publish distinct, so a message
    published twice in a transaction is delivered twice, as a Redis
    ``MULTI`` delivers it. It only has to differ within one transaction (one
    session, one process), so a process-wide counter mod 16**8 suffices."""
    return format(next(_nonce) % (16**_NONCE_WIDTH), "0%dx" % _NONCE_WIDTH)


def _encode_payload(channel: str, data: bytes, nonce: Optional[str] = None) -> str:
    """``<nonce>:<base64 channel>.<base64 message>``. The nonce is stripped
    on delivery; it is never part of the message."""
    return (
        (nonce if nonce is not None else _next_nonce())
        + ":"
        + base64.b64encode(channel.encode("utf-8")).decode("ascii")
        + "."
        + base64.b64encode(data).decode("ascii")
    )


def _decode_payload(payload: str) -> Optional[tuple[bytes, bytes]]:
    nonce, sep, body = payload.partition(":")
    if not sep or len(nonce) != _NONCE_WIDTH:
        return None
    try:
        channel, _, data = body.partition(".")
        return base64.b64decode(channel), base64.b64decode(data)
    except (binascii.Error, ValueError):
        return None


def publish(
    backend: Any,
    channel: Any,
    message: Union[bytes, str, int, float],
    *,
    uow: Optional[UnitOfWork] = None,
) -> int:
    """``PUBLISH channel message`` on ``backend``: see the module docstring."""
    ch = _text(channel, "channel")
    data = encode_value(message)
    payload = _encode_payload(ch, data)
    size = len(payload)
    if size >= NOTIFY_PAYLOAD_LIMIT:
        head = len(_encode_payload(ch, b"", "0" * _NONCE_WIDTH))
        room = max(((NOTIFY_PAYLOAD_LIMIT - 1 - head) // 4) * 3, 0)
        raise PubSubPayloadTooLarge(
            f"publish to {ch!r}: a Postgres NOTIFY payload must be shorter than "
            f"{NOTIFY_PAYLOAD_LIMIT} bytes, and this message encodes to {size} "
            f"(the channel name and the {len(data)}-byte message, base64-encoded; "
            f"at most {room} message bytes fit on this channel). Publish a smaller "
            "message, or the key of a record that holds it."
        )
    from . import _pg_uow

    pg = _pg_uow(uow)
    if pg is not None and _async_unit_outside_bridge(pg):
        # A popoto.batch() an async_* call opened (#784): its connection
        # belongs to the event loop, so this sync call cannot send now. The
        # NOTIFY is delivered at COMMIT either way, so it is sent then, from
        # inside the transaction that ``await pipe.async_execute()`` commits
        # (and never after a rollback). The count is not knowable yet: 0.
        pg.before_commit(lambda: publish(backend, ch, data, uow=pg))
        return 0
    t = backend._events_ready()
    rows, _ = backend._run(
        "SELECT pg_notify(%s, %s), (SELECT count(*) FROM "
        f"{t['popoto_pubsub_listener']} l WHERE l.pid IN (SELECT pid FROM "
        "pg_stat_activity) AND ((NOT l.pattern AND l.name = %s) OR (l.pattern AND "
        "%s ~ ('^(' || l.regex || ')$'))))",
        [pubsub_channel(backend.schema), payload, ch, as_bytes_text(ch)],
        uow=pg,
        write=True,
    )
    return int(rows[0][1])


def _async_unit_outside_bridge(uow: Any) -> bool:
    """Whether ``uow``'s connection belongs to an event loop (an async_*
    call opened it) while this code runs outside the bridge that drives it."""
    if not hasattr(uow.conn, "async_connection"):
        return False
    from .aio import _in_bridge

    return not _in_bridge()


class _PubSubSink(QueueSink):
    """A subscriber's queue, plus the registrations ``publish`` counts.

    ``popoto_pubsub_listener`` rows name the shared session's backend pid,
    so they stop counting when it drops. :meth:`resume` re-inserts them
    under the new pid on the hub's own connection the moment it reconnects
    (before any subscriber polls).

    Two locks. ``lock`` serialises the subscriber's own registration writes
    (its thread's I/O on the pool) and is held across them. ``state`` guards
    only ``names`` and ``pid`` and is never held across I/O, so
    :meth:`resume` -- run on the hub's thread for every subscriber in turn --
    takes ``state`` alone and works from a snapshot: one subscriber's slow
    write never stalls the reconnect, or anyone else's re-registration
    (#803).

    Only the snapshot's *insert* runs without ``lock``. Removing a row the
    snapshot re-inserted for a channel unsubscribed meanwhile needs it: the
    channel may have been subscribed again since, and its fresh row looks
    exactly like the stale one. So :meth:`resume` records the snapshot's
    keys in ``stale`` and :meth:`sweep`\\ s them under ``lock``, taken
    without waiting; when a subscriber's write holds it, that subscriber
    sweeps as it lets go (:meth:`PostgresPubSub._locked`). Either way the
    check "is this channel still subscribed?" and its DELETE run with no
    subscriber write in between. The same sweep deletes this subscriber's
    rows under any pid but the current one: a write that read the old pid
    can land after ``resume``'s own cleanup, and only a sweep under ``lock``
    is sure to come after it.

    ``owner`` is the process that made the sink. A forked child that
    inherits it never deletes its rows: they are the parent's, still counted
    for a parent that is still subscribed (#803)."""

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()
        self.state = threading.Lock()
        self.owner = os.getpid()
        self.pid: Optional[int] = None
        self.token = uuid.uuid4().hex
        self.table: Optional[str] = None
        self.names: dict[tuple[bool, str], Optional[str]] = {}
        self.stale: set[tuple[bool, str]] = set()
        """Keys :meth:`resume` re-inserted that :meth:`sweep` has not yet
        checked against ``names`` (guarded by ``state``)."""

    @property
    def inherited(self) -> bool:
        """Made by another process (this one is a forked child)."""
        return self.owner != os.getpid()

    def rows(
        self, pid: int, names: Optional[dict[tuple[bool, str], Optional[str]]] = None
    ) -> list[list[Any]]:
        source = self.names if names is None else names
        return [
            [pid, self.token, pattern, name, regex]
            for (pattern, name), regex in source.items()
        ]

    def resume(self, conn: Any, backend_pid: int) -> None:
        with self.state:
            if self.table is None or self.pid == backend_pid or self.inherited:
                return
            # From here a concurrent _register reads the new pid and inserts
            # its own row; one it added before is in the snapshot.
            snapshot = dict(self.names)
            self.pid = backend_pid
            # Re-armed with the pid, in the same block: if a write below
            # raises, the next locked operation still sweeps these names
            # instead of seeing ``pid`` current and skipping them.
            self.stale.update(snapshot)
        if snapshot:
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {self.table} (pid, token, pattern, name, "
                    "regex) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    self.rows(backend_pid, snapshot),
                )
        with self.state:
            # Unsubscribed (or closed) while the snapshot was inserted, the
            # subscriber's own DELETE may have run before our INSERT. Which
            # keys that happened to is only decidable under ``lock``.
            self.stale.update(snapshot)
        conn.execute(
            f"DELETE FROM {self.table} WHERE token = %s AND pid <> %s",
            [self.token, backend_pid],
        )
        # Never wait: a subscriber holding ``lock`` sweeps when it releases
        # it, and it sees ``stale`` set above (PostgresPubSub._locked). The
        # DELETE above is not enough on its own: a holder's write that read
        # the old pid may land after it, and only a sweep under ``lock``
        # (which also deletes this token's rows under any other pid) is
        # guaranteed to run after every such write.
        if self.lock.acquire(blocking=False):
            try:
                self.sweep(conn.execute)
            finally:
                self.lock.release()

    def sweep(self, execute: Callable[[str, list[Any]], Any]) -> None:
        """Make this subscriber's rows exactly ``names`` under the current
        ``pid``. The caller holds ``lock``, so no subscriber write can
        re-register a key, or land a row, between the check and the writes
        (#803):

        - delete the rows :meth:`resume` re-inserted for keys no longer
          subscribed (a key still in ``names`` keeps its row, which may be
          the subscriber's own);
        - insert the current pid's row for every key in ``names`` (``ON
          CONFLICT DO NOTHING``), which a concurrent reconnect's cleanup may
          have removed while it named a pid that was current a moment ago;
        - delete every row of this subscriber under any *other* pid. A write
          that read the pid before a reconnect and landed after its cleanup
          leaves one (a ``subscribe`` whose INSERT was in flight, or a
          re-registration a second reconnect overtook); left alone it would
          be counted twice for as long as the superseded backend stays in
          ``pg_stat_activity`` -- hours, for a partitioned peer.

        When a reconnect moves ``pid`` while this runs, it marks ``stale``
        again before trying ``lock``, so the sweep runs once more with the
        new pid (:meth:`PostgresPubSub._locked`'s loop, or ``resume``
        itself). On failure the marks are restored, with every subscribed
        key added, so a sweep that never reached its last two writes runs
        again on the subscriber's next locked write or poll."""
        with self.state:
            marked = set(self.stale)
            gone = [key for key in marked if key not in self.names]
            self.stale.clear()
            pid = self.pid
            names = dict(self.names)
        try:
            for pattern, name in gone:
                execute(
                    f"DELETE FROM {self.table} WHERE token = %s AND pattern = %s "
                    "AND name = %s",
                    [self.token, pattern, name],
                )
            if pid is None:
                return  # closed, or not registered yet: no rows to keep
            if names:
                keys = list(names)
                execute(
                    f"INSERT INTO {self.table} (pid, token, pattern, name, regex) "
                    "SELECT %s, %s, u.p, u.n, u.r FROM unnest(%s::boolean[], "
                    "%s::text[], %s::text[]) AS u(p, n, r) ON CONFLICT DO NOTHING",
                    [
                        pid,
                        self.token,
                        [pattern for pattern, _ in keys],
                        [name for _, name in keys],
                        [names[key] for key in keys],
                    ],
                )
            execute(
                f"DELETE FROM {self.table} WHERE token = %s AND pid <> %s",
                [self.token, pid],
            )
        except BaseException:
            with self.state:
                self.stale.update(marked)
                self.stale.update(names)
            raise


class PostgresPubSub:
    """redis-py's ``PubSub`` on the process's shared ``LISTEN`` session.

    ``subscribe``/``psubscribe``/``unsubscribe``/``punsubscribe``,
    ``get_message(ignore_subscribe_messages=, timeout=)`` (``0.0`` polls,
    ``None`` waits), ``listen()`` and ``close()``, with the same message
    dicts (``type``, ``pattern``, ``channel``, ``data``; ``bytes`` values).
    A subscription is live when the call returns.

    Every subscriber in a process shares one session per DSN
    (:mod:`.listen`, #799): each is a sink on the schema's channel, attached
    on its first subscription and detached when it has none left. If the
    session drops it is reopened at once, re-``LISTEN``\\ s and re-registers
    every subscription under its new pid; messages published while it was
    down are lost, as on a Redis reconnect."""

    def __init__(self, backend: Any, ignore_subscribe_messages: bool = False) -> None:
        self.backend = backend
        self.ignore_subscribe_messages = ignore_subscribe_messages
        self.channels: dict[bytes, Optional[Callable[[dict[str, Any]], Any]]] = {}
        self.patterns: dict[bytes, Optional[Callable[[dict[str, Any]], Any]]] = {}
        self._compiled: dict[bytes, re.Pattern[str]] = {}
        self._pending: collections.deque[dict[str, Any]] = collections.deque()
        self._sink = _PubSubSink()
        self._hub: Any = None
        self._finalizer: Any = None

    # -- the session ----------------------------------------------------------------

    @property
    def subscribed(self) -> bool:
        return bool(self.channels or self.patterns)

    @property
    def pid(self) -> Optional[int]:
        """The shared session's server pid this subscriber's
        ``popoto_pubsub_listener`` rows are registered under."""
        return self._sink.pid

    @property
    def token(self) -> str:
        return self._sink.token

    @property
    def dropped(self) -> int:
        """Messages discarded unread because this subscriber fell behind
        (``Defaults.PG_LISTEN_QUEUE_MAX_MESSAGES`` /
        ``PG_LISTEN_QUEUE_MAX_BYTES``). The process-wide total is the
        shared session's ``hub.dropped``."""
        return self._sink.dropped

    @property
    def dropped_bytes(self) -> int:
        return self._sink.dropped_bytes

    @property
    def queued_bytes(self) -> int:
        """Payload bytes received but not yet read."""
        return self._sink.queued_bytes

    @property
    def hub(self) -> Any:
        return self._hub

    def _channel(self) -> str:
        return pubsub_channel(self.backend.schema)

    def _attached(self) -> bool:
        return self._hub is not None and self._hub.pid == os.getpid()

    def _ensure(self) -> None:
        """Attach to the shared session (again in a forked child, which
        never shares the parent's socket, with a fresh token so it never
        touches the parent's registrations), then make sure the
        registrations name the session's current pid."""
        from .listen import hub_for

        if not self._attached():
            if self._hub is not None:  # inherited across a fork: start clean
                if self._finalizer is not None:
                    self._finalizer.detach()
                    self._finalizer = None
                # No lock: one another thread held at fork() stays held here.
                names = dict(self._sink.names)
                self._sink = _PubSubSink()
                self._sink.names = names
            self._sink.table = self.backend._events_ready()["popoto_pubsub_listener"]
            hub = hub_for(self.backend.listen_dsn)
            hub.attach(self._channel(), self._sink, barrier=True)
            self._hub = hub
            self._finalizer = weakref.finalize(
                self, hub.detach, self._channel(), self._sink
            )
        self._sync_registration()

    def _detach(self) -> None:
        if self._finalizer is not None:
            self._finalizer.detach()
            self._finalizer = None
        if self._attached():
            self._hub.detach(self._channel(), self._sink)
        self._hub = None
        if self._sink.inherited:
            # A forked child: start from a sink of its own rather than touch
            # the inherited one's lock (the parent's hub thread may have held
            # it at fork()). What it had queued was the parent's anyway.
            fresh = _PubSubSink()
            fresh.names = dict(self._sink.names)
            self._sink = fresh
        else:
            self._sink.take(0)

    @contextlib.contextmanager
    def _locked(self, sink: _PubSubSink) -> Iterator[None]:
        """``sink.lock`` for a registration write, then the sweep a
        reconnect's :meth:`_PubSubSink.resume` left to this subscriber
        because the lock was busy. ``resume`` marks ``stale`` before it
        tries the lock, so whenever its try fails the holder sees the mark
        on release (#803).

        A write that raises skips the sweep: the exception leaves at
        ``yield``, so nothing below it runs. That is deliberate -- the caller
        sees its own error, not a second one from the sweep -- and harmless:
        ``stale`` stays set, so the subscriber's next locked write sweeps,
        and every poll makes one (``_poll`` -> ``_ensure`` ->
        :meth:`_sync_registration`). Until then a superseded pid's row may
        be counted; the count is advisory."""
        with sink.lock:
            yield
        while sink.stale and not sink.inherited:
            try:
                with sink.lock:
                    sink.sweep(self._execute)
            except Exception as exc:  # the count is advisory; never fail on it
                logger.warning("pub/sub registration sweep failed: %s", exc)
                return

    def _execute(self, sql: str, params: list[Any]) -> None:
        self.backend._run(sql, params, write=True)

    def _sync_registration(self) -> None:
        """Register every subscription under the shared session's current
        pid when they are not (first use; a reconnect that this subscriber
        noticed before :meth:`_PubSubSink.resume` reached it), sweeping
        registrations whose session is gone.

        The re-registration itself is :meth:`_PubSubSink.sweep`, so it also
        deletes this subscriber's rows under the pid it replaces: that
        backend may still be listed in ``pg_stat_activity`` (a frozen or
        partitioned peer), where the dead-session DELETE below misses it.
        When a second reconnect overtakes these writes, its ``resume`` marks
        ``stale`` and :meth:`_locked` sweeps again under the newer pid."""
        sink = self._sink
        with self._locked(sink):
            with sink.state:
                current = None if self._hub is None else self._hub.backend_pid
                if current is None or current == sink.pid:
                    return
                sink.pid = current
                # Same block as the pid move: a raising write below leaves
                # the names marked, so the next locked operation re-sweeps.
                sink.stale.update(sink.names)
            t = self.backend._events_ready()
            self.backend._run(
                f"DELETE FROM {t['popoto_pubsub_listener']} WHERE pid NOT IN "
                "(SELECT pid FROM pg_stat_activity)",
                write=True,
            )
            sink.sweep(self._execute)

    def _insert(self, row: list[Any]) -> None:
        t = self.backend._events_ready()
        self.backend._run(
            f"INSERT INTO {t['popoto_pubsub_listener']} (pid, token, pattern, name, "
            "regex) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            row,
            write=True,
        )

    def _register(self, name: bytes, *, pattern: bool) -> None:
        sink = self._sink
        text = name.decode("utf-8")
        regex = glob_to_regex(as_bytes_text(text)) if pattern else None
        with self._locked(sink):
            with sink.state:
                sink.names[(pattern, text)] = regex
                pid = sink.pid
            if pid is not None:
                self._insert([pid, sink.token, pattern, text, regex])

    def _unregister(self, name: bytes, *, pattern: bool) -> None:
        sink = self._sink
        text = name.decode("utf-8")
        if sink.inherited:
            # A forked child: the row is the parent's, and the parent may
            # still be subscribed. Forget it locally, touch no lock (one held
            # by another thread at fork() stays held) and no row.
            sink.names.pop((pattern, text), None)
            return
        with self._locked(sink):
            with sink.state:
                sink.names.pop((pattern, text), None)
                pid = sink.pid
            if pid is None:
                return
            t = self.backend._events_ready()
            self.backend._run(
                f"DELETE FROM {t['popoto_pubsub_listener']} WHERE token = %s "
                "AND pattern = %s AND name = %s",
                [sink.token, pattern, text],
                write=True,
            )

    # -- subscriptions --------------------------------------------------------------

    @staticmethod
    def _key(name: Any) -> bytes:
        return _text(name, "channel").encode("utf-8")

    def _confirm(self, kind: str, name: Optional[bytes]) -> None:
        self._pending.append(
            {
                "type": kind,
                "pattern": None,
                "channel": name,
                "data": len(self.channels) + len(self.patterns),
            }
        )

    def _subscribe(
        self, args: tuple[Any, ...], kwargs: dict[str, Any], *, pattern: bool
    ) -> None:
        table = self.patterns if pattern else self.channels
        wanted: list[tuple[bytes, Optional[Callable[[dict[str, Any]], Any]]]] = [
            (self._key(a), None) for a in args
        ] + [(self._key(k), v) for k, v in kwargs.items()]
        self._ensure()
        for name, handler in wanted:
            fresh = name not in table
            table[name] = handler
            if pattern:
                self._compiled[name] = re.compile(
                    glob_to_regex(as_bytes_text(name)), re.DOTALL
                )
            if fresh:
                self._register(name, pattern=pattern)
            self._confirm("psubscribe" if pattern else "subscribe", name)

    def subscribe(self, *args: Any, **kwargs: Any) -> None:
        self._subscribe(args, kwargs, pattern=False)

    def psubscribe(self, *args: Any, **kwargs: Any) -> None:
        self._subscribe(args, kwargs, pattern=True)

    def _unsubscribe(self, args: tuple[Any, ...], *, pattern: bool) -> None:
        table = self.patterns if pattern else self.channels
        kind = "punsubscribe" if pattern else "unsubscribe"
        names = [self._key(a) for a in args] if args else list(table)
        if not names:
            self._confirm(kind, None)
            return
        for name in names:
            if name in table:
                del table[name]
                self._compiled.pop(name, None)
                try:
                    self._unregister(name, pattern=pattern)
                except Exception as exc:  # the count is advisory; never fail on it
                    logger.warning("pub/sub unregister of %r failed: %s", name, exc)
            self._confirm(kind, name)
        if not self.subscribed:
            # Nothing left to match: stop receiving (and queueing) the
            # schema's traffic until the next subscription.
            self._detach()

    def unsubscribe(self, *args: Any) -> None:
        self._unsubscribe(args, pattern=False)

    def punsubscribe(self, *args: Any) -> None:
        self._unsubscribe(args, pattern=True)

    # -- messages ---------------------------------------------------------------------

    def _deliver(self, payload: str) -> None:
        decoded = _decode_payload(payload)
        if decoded is None:
            return
        channel, data = decoded
        if channel in self.channels:
            self._pending.append(
                {"type": "message", "pattern": None, "channel": channel, "data": data}
            )
        if self.patterns:
            text = as_bytes_text(channel)
            for name, regex in list(self._compiled.items()):
                if regex.fullmatch(text):
                    self._pending.append(
                        {
                            "type": "pmessage",
                            "pattern": name,
                            "channel": channel,
                            "data": data,
                        }
                    )

    def _poll(self, timeout: Optional[float]) -> None:
        from . import _import_psycopg

        psycopg = _import_psycopg()
        try:
            self._ensure()
        except (psycopg.OperationalError, TimeoutError) as exc:
            logger.warning("popoto pub/sub LISTEN session unavailable (%s)", exc)
            return
        payloads, reconnected = self._sink.take(timeout)
        if reconnected:
            try:
                self._sync_registration()
            except Exception as exc:  # the count is advisory; never fail on it
                logger.warning("pub/sub re-registration failed: %s", exc)
        for payload in payloads:
            self._deliver(payload)

    def _handle(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        kind = message["type"]
        if kind in ("message", "pmessage"):
            table = self.channels if kind == "message" else self.patterns
            key = message["channel"] if kind == "message" else message["pattern"]
            handler = table.get(key)
            if handler is not None:
                handler(message)
                return None
        return message

    def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: Optional[float] = 0.0
    ) -> Optional[dict[str, Any]]:
        ignore = ignore_subscribe_messages or self.ignore_subscribe_messages
        while True:
            while self._pending:
                message = self._pending.popleft()
                if ignore and message["type"] not in ("message", "pmessage"):
                    continue
                return self._handle(message)
            if not self.subscribed:
                return None
            before = len(self._pending)
            self._poll(timeout)
            if len(self._pending) == before:
                if timeout is None:
                    continue
                return None
            timeout = 0.0

    def listen(self) -> Iterator[dict[str, Any]]:
        while self.subscribed or self._pending:
            message = self.get_message(timeout=None)
            if message is not None:
                yield message

    def close(self) -> None:
        sink = self._sink
        if sink.inherited:
            # A forked child: the rows are the parent's, and the parent may
            # still be subscribed (#803). Forget them locally, touching no
            # lock (one held by another thread at fork() stays held here).
            sink.names.clear()
            sink.pid = None
        else:
            self._close_registrations(sink)
        self._detach()
        self.channels.clear()
        self.patterns.clear()
        self._compiled.clear()

    def _close_registrations(self, sink: _PubSubSink) -> None:
        with self._locked(sink):
            # Forget first, then delete: a reconnect's resume() that inserted
            # meanwhile sees the names gone and deletes its rows itself.
            with sink.state:
                pid, sink.pid = sink.pid, None
                sink.names.clear()
            if pid is not None:
                try:
                    t = self.backend._events_ready()
                    self.backend._run(
                        f"DELETE FROM {t['popoto_pubsub_listener']} WHERE token = %s",
                        [sink.token],
                        write=True,
                    )
                except Exception as exc:
                    logger.warning(
                        "pub/sub close could not drop its registrations: %s", exc
                    )

    reset = close

    def __enter__(self) -> "PostgresPubSub":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
