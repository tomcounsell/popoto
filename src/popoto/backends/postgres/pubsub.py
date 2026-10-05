"""Pub/sub on Postgres: ``PUBLISH`` as ``pg_notify``, ``SUBSCRIBE`` as
``LISTEN`` (#759 M5).

One notification channel per schema (``popoto_pubsub_<digest>``) carries
every popoto channel: the payload names the channel and carries the message,
and each subscriber keeps what its subscriptions match. So a channel name is
not bound by Postgres's 63-byte identifier limit, and pattern subscriptions
(``PSUBSCRIBE``) are matched **client-side**, with Redis's glob rules
(``*``, ``?``, ``[...]``, ``[^...]``, ``\\`` escapes) translated to a regular
expression. The cost: every subscriber of the schema receives every message
and drops what it did not ask for.

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
connect. The ``LISTEN`` session is dedicated, never pooled (§3 Topology), and
must reach the same server as the backend's DSN.
"""

from __future__ import annotations

import base64
import binascii
import collections
import hashlib
import logging
import re
import uuid
import weakref
from typing import Any, Callable, Iterator, Optional, Union

from ...exceptions import PublisherException
from ..types import UnitOfWork
from .events import _close_quietly, _text, encode_value

__all__ = [
    "NOTIFY_PAYLOAD_LIMIT",
    "PostgresPubSub",
    "PubSubPayloadTooLarge",
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
    ``[abc]``/``[^abc]``/``[a-z]`` a class, ``\\x`` a literal ``x``."""
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
                elif j + 2 < n and pattern[j + 1] == "-" and pattern[j + 2] != "]":
                    lo, hi = c, pattern[j + 2]
                    if lo > hi:
                        lo, hi = hi, lo
                    items.append(f"{lit(lo)}-{lit(hi)}")
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


def _encode_payload(channel: str, data: bytes) -> str:
    return (
        base64.b64encode(channel.encode("utf-8")).decode("ascii")
        + "."
        + base64.b64encode(data).decode("ascii")
    )


def _decode_payload(payload: str) -> Optional[tuple[bytes, bytes]]:
    try:
        channel, _, data = payload.partition(".")
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
        head = len(_encode_payload(ch, b""))
        room = max(((NOTIFY_PAYLOAD_LIMIT - 1 - head) // 4) * 3, 0)
        raise PubSubPayloadTooLarge(
            f"publish to {ch!r}: a Postgres NOTIFY payload must be shorter than "
            f"{NOTIFY_PAYLOAD_LIMIT} bytes, and this message encodes to {size} "
            f"(the channel name and the {len(data)}-byte message, base64-encoded; "
            f"at most {room} message bytes fit on this channel). Publish a smaller "
            "message, or the key of a record that holds it."
        )
    t = backend._events_ready()
    from . import _pg_uow

    rows, _ = backend._run(
        "SELECT pg_notify(%s, %s), (SELECT count(*) FROM "
        f"{t['popoto_pubsub_listener']} l WHERE l.pid IN (SELECT pid FROM "
        "pg_stat_activity) AND ((NOT l.pattern AND l.name = %s) OR (l.pattern AND "
        "%s ~ ('^(' || l.regex || ')$'))))",
        [pubsub_channel(backend.schema), payload, ch, ch],
        uow=_pg_uow(uow),
        write=True,
    )
    return int(rows[0][1])


class PostgresPubSub:
    """redis-py's ``PubSub`` on a dedicated ``LISTEN`` session.

    ``subscribe``/``psubscribe``/``unsubscribe``/``punsubscribe``,
    ``get_message(ignore_subscribe_messages=, timeout=)`` (``0.0`` polls,
    ``None`` waits), ``listen()`` and ``close()``, with the same message
    dicts (``type``, ``pattern``, ``channel``, ``data``; ``bytes`` values).
    A subscription is live when the call returns. If the session drops it is
    reopened, and its subscriptions re-registered, on the next call;
    messages published meanwhile are lost, as on a Redis reconnect."""

    def __init__(self, backend: Any, ignore_subscribe_messages: bool = False) -> None:
        self.backend = backend
        self.ignore_subscribe_messages = ignore_subscribe_messages
        self.channels: dict[bytes, Optional[Callable[[dict[str, Any]], Any]]] = {}
        self.patterns: dict[bytes, Optional[Callable[[dict[str, Any]], Any]]] = {}
        self._compiled: dict[bytes, re.Pattern[str]] = {}
        self._pending: collections.deque[dict[str, Any]] = collections.deque()
        self.token = uuid.uuid4().hex
        self.conn: Any = None
        self.pid: Optional[int] = None
        self._finalizer: Any = None

    # -- the session ----------------------------------------------------------------

    @property
    def subscribed(self) -> bool:
        return bool(self.channels or self.patterns)

    def _ensure(self) -> Any:
        conn = self.conn
        if conn is not None and not conn.closed and not conn.broken:
            return conn
        _close_quietly(conn)
        from . import _import_psycopg
        from ...fields.constants import Defaults

        psycopg = _import_psycopg()
        conn = psycopg.connect(
            self.backend.listen_dsn,
            autocommit=True,
            connect_timeout=max(
                1, int(round(float(Defaults.PG_CONNECT_TIMEOUT_SECONDS)))
            ),
        )
        conn.execute(f'LISTEN "{pubsub_channel(self.backend.schema)}"')
        self.conn = conn
        self.pid = int(conn.info.backend_pid)
        if self._finalizer is not None:
            self._finalizer.detach()
        self._finalizer = weakref.finalize(self, _close_quietly, conn)
        t = self.backend._events_ready()
        # Sweep registrations whose session is gone, then (re)register ours.
        self.backend._run(
            f"DELETE FROM {t['popoto_pubsub_listener']} WHERE pid NOT IN "
            "(SELECT pid FROM pg_stat_activity)",
            write=True,
        )
        for name in self.channels:
            self._register(name, pattern=False)
        for name in self.patterns:
            self._register(name, pattern=True)
        return conn

    def _register(self, name: bytes, *, pattern: bool) -> None:
        t = self.backend._events_ready()
        text = name.decode("utf-8")
        self.backend._run(
            f"INSERT INTO {t['popoto_pubsub_listener']} (pid, token, pattern, name, "
            "regex) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            [
                self.pid,
                self.token,
                pattern,
                text,
                glob_to_regex(text) if pattern else None,
            ],
            write=True,
        )

    def _unregister(self, name: bytes, *, pattern: bool) -> None:
        if self.pid is None:
            return
        t = self.backend._events_ready()
        self.backend._run(
            f"DELETE FROM {t['popoto_pubsub_listener']} WHERE pid = %s AND token = %s "
            "AND pattern = %s AND name = %s",
            [self.pid, self.token, pattern, name.decode("utf-8")],
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
                    glob_to_regex(name.decode("utf-8")), re.DOTALL
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
            text = channel.decode("utf-8", "replace")
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
            conn = self._ensure()
            for notify in conn.notifies(timeout=timeout, stop_after=1):
                self._deliver(notify.payload)
        except psycopg.OperationalError as exc:
            logger.warning("popoto pub/sub LISTEN session dropped (%s); reopening", exc)
            _close_quietly(self.conn)

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
        if self.pid is not None:
            try:
                t = self.backend._events_ready()
                self.backend._run(
                    f"DELETE FROM {t['popoto_pubsub_listener']} WHERE pid = %s AND "
                    "token = %s",
                    [self.pid, self.token],
                    write=True,
                )
            except Exception as exc:
                logger.warning(
                    "pub/sub close could not drop its registrations: %s", exc
                )
        if self._finalizer is not None:
            self._finalizer.detach()
            self._finalizer = None
        _close_quietly(self.conn)
        self.conn = None
        self.channels.clear()
        self.patterns.clear()
        self._compiled.clear()

    reset = close

    def __enter__(self) -> "PostgresPubSub":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
