"""One shared ``LISTEN`` session per process and DSN (#799).

Every Postgres ``LISTEN`` in a process -- each ``Subscriber``'s
:class:`~.pubsub.PostgresPubSub` and each blocking stream read's
:class:`~.events.EventListener`, sync or async -- rides **one** dedicated
session per DSN, owned by a :class:`ListenHub`. The hub's thread holds that
connection (outside the pool, as a ``LISTEN`` must be: §3 Topology),
``LISTEN``\\ s the union of the channels its sinks want, and hands each
notification to every sink of its channel, **in arrival order**, on that one
thread. A channel is ``UNLISTEN``\\ ed when its last sink detaches, and the
session is closed when no sink is left, so an idle process holds none.

**Attaching.** :meth:`ListenHub.attach` returns once the sink is live: the
first sink of a channel is live from the moment its ``LISTEN`` runs; a later
one from a *barrier* -- a ``pg_notify`` the hub sends itself on that channel
and swallows when it comes back. Postgres delivers notifications in commit
order, so the barrier splits the stream exactly: what committed before it is
never handed to the new sink (as a fresh session's ``LISTEN`` never sees an
earlier ``NOTIFY``), what commits after it always is. Stream waiters skip the
barrier (``barrier=False``): an extra wake-up only costs them one read.

**Reconnecting.** When the session drops (``pg_terminate_backend``, a
restart, a network fault) the hub reconnects at once, then with backoff
(:data:`LISTEN_RECONNECT_BACKOFF_SECONDS` doubling to
:data:`LISTEN_RECONNECT_MAX_BACKOFF_SECONDS`), re-``LISTEN``\\ s every
channel, and tells each sink (:meth:`Sink.reconnected`). Notifications sent
while it was down are **lost** -- ``NOTIFY`` is not stored for a session that
is not listening. Stream readers lose nothing by it: entries are rows, and a
reconnect wakes every waiter to read again. Pub/sub loses what was published
in the gap, as a Redis subscriber loses what was published while its
connection was down.

**Dead connections** (#803). A peer that vanished without a FIN or RST (a
NAT entry expiring) never makes the socket readable, so the session sets
TCP keepalives (``Defaults.PG_LISTEN_KEEPALIVES*``, unless the DSN names
them) and the hub's wait is bounded: after
``Defaults.PG_LISTEN_LIVENESS_INTERVAL_SECONDS`` without traffic it sends
``SELECT 1`` and drops the session -- reconnecting as above -- when no reply
comes within ``Defaults.PG_LISTEN_LIVENESS_TIMEOUT_SECONDS``.

**Undecodable payloads.** A notification is decoded with the session's
encoding. Bytes it cannot decode -- possible only on a ``SQL_ASCII``
database, where the server stores bytes unchecked, so any non-ASCII
``NOTIFY`` (even valid UTF-8) fails the ``ascii`` codec -- are decoded as
UTF-8 with ``errors="replace"`` (U+FFFD for each invalid sequence) and
delivered as ``str``, the sink contract; the first is logged at WARNING,
once per hub. popoto's own payloads are ASCII and never take that path, and
pub/sub drops a foreign payload anyway, so this only keeps a stranger's
``NOTIFY`` from stopping the hub's thread. A replaced payload is measured
by its decoded form, so its byte size may differ from the server's.

**Fork.** Hubs are per ``(dsn, pid)``: a child process never touches the
parent's socket (it never even closes it -- closing would end the parent's
session), and opens its own on first use. Sinks re-attach when they notice
the pid changed.

**Threads.** Every hub method is safe from any thread; only the hub's own
thread touches its connection. Sinks are called on the hub's thread and must
not block -- :meth:`Sink.resume` included, which is why pub/sub re-registers
from a snapshot rather than behind a subscriber's own write lock.
"""

from __future__ import annotations

import collections
import logging
import os
import select
import socket
import threading
import time
import uuid
from typing import Any, Optional

__all__ = [
    "LISTEN_RECONNECT_MAX_BACKOFF_SECONDS",
    "ListenHub",
    "QueueSink",
    "Sink",
    "WakeSink",
    "close_hubs",
    "hub_for",
]

logger = logging.getLogger("POPOTO.postgres.listen")

LISTEN_RECONNECT_BACKOFF_SECONDS = 0.2
"""The pause after a failed reconnect attempt; doubled after each further
failure (the first attempt after a drop is immediate)."""

LISTEN_RECONNECT_MAX_BACKOFF_SECONDS = 5.0
"""The ceiling the reconnect backoff doubles to."""

LISTEN_APPLICATION_NAME = "popoto_listen"
"""The ``application_name`` the shared session reports in
``pg_stat_activity`` when its DSN names none."""

WAKE_SINK_MAX_PAYLOADS = 4096
"""Distinct stream digests a blocking read remembers between waits
(:class:`WakeSink`); past it, the next wait wakes at once."""

_BARRIER_PREFIX = "~popoto-listen-barrier:"
"""A barrier payload. Never a pub/sub payload (those start with an
8-character hex nonce) nor a stream digest (32 hex characters), so every
other listener of the channel drops it as foreign."""


class Sink:
    """Something a :class:`ListenHub` delivers to. Both methods run on the
    hub's thread: they must be quick and must not raise."""

    dropped = 0
    """Payloads this sink discarded unread (:class:`QueueSink`'s caps); the
    hub adds every increase to :attr:`ListenHub.dropped`."""

    def push(self, payload: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def resume(self, conn: Any, backend_pid: int) -> None:
        """After a reconnect, before :meth:`reconnected`: re-establish any
        server-side state tied to the session, on the hub's own ``conn``
        (autocommit; only this call may use it). Pub/sub re-registers its
        subscriptions under the new pid here, so ``publish``'s count is
        right as soon as the session is back."""

    def reconnected(self) -> None:  # pragma: no cover - interface
        """The session dropped and was reopened; notifications sent in
        between are lost."""


def _nbytes(payload: str) -> int:
    """``payload``'s size in UTF-8 bytes, as the server counts it."""
    return len(payload) if payload.isascii() else len(payload.encode("utf-8"))


class QueueSink(Sink):
    """Every payload, in arrival order, for one reader (a ``PubSub``).

    Bounded by count (``Defaults.PG_LISTEN_QUEUE_MAX_MESSAGES``) and by
    payload size (``Defaults.PG_LISTEN_QUEUE_MAX_BYTES``, Redis's 32 MiB
    pub/sub output-buffer hard limit), both read when the sink is made: a
    subscriber that stops reading loses its oldest messages rather than
    growing without limit -- the analogue of Redis disconnecting a pub/sub
    client past its ``client-output-buffer-limit``. Each drop is counted
    (:attr:`dropped`, :attr:`dropped_bytes`, and the hub's
    :attr:`ListenHub.dropped`) and logged at WARNING, on the first and then
    every 10 000th."""

    def __init__(
        self, maxlen: Optional[int] = None, maxbytes: Optional[int] = None
    ) -> None:
        from ...fields.constants import Defaults

        self._cond = threading.Condition()
        self._items: collections.deque[str] = collections.deque()
        self._maxlen = (
            int(Defaults.PG_LISTEN_QUEUE_MAX_MESSAGES) if maxlen is None else maxlen
        )
        self._maxbytes = (
            int(Defaults.PG_LISTEN_QUEUE_MAX_BYTES) if maxbytes is None else maxbytes
        )
        self._reconnected = False
        self.queued_bytes = 0
        """UTF-8 bytes of the payloads queued unread now. popoto's own are
        ASCII (base64 and hex), but anyone may ``NOTIFY`` the channel with
        any text, so a payload is measured in bytes, not characters."""
        self.dropped = 0
        self.dropped_bytes = 0

    def push(self, payload: str) -> None:
        size = _nbytes(payload)
        with self._cond:
            while self._items and (
                len(self._items) >= self._maxlen
                or self.queued_bytes + size > self._maxbytes
            ):
                old = _nbytes(self._items.popleft())
                self.queued_bytes -= old
                self.dropped += 1
                self.dropped_bytes += old
                if self.dropped == 1 or self.dropped % 10000 == 0:
                    logger.warning(
                        "popoto pub/sub subscriber is not reading: %d message(s) "
                        "(%d bytes) dropped (PG_LISTEN_QUEUE_MAX_MESSAGES = %d, "
                        "PG_LISTEN_QUEUE_MAX_BYTES = %d)",
                        self.dropped,
                        self.dropped_bytes,
                        self._maxlen,
                        self._maxbytes,
                    )
            self._items.append(payload)
            self.queued_bytes += size
            self._cond.notify_all()

    def reconnected(self) -> None:
        with self._cond:
            self._reconnected = True
            self._cond.notify_all()

    def take(self, timeout: Optional[float]) -> tuple[list[str], bool]:
        """Everything queued, waiting up to ``timeout`` (``None``: until
        something arrives) for the first; and whether the session was
        reopened since the last take."""
        with self._cond:
            if not self._items and not self._reconnected and timeout != 0:
                self._cond.wait_for(
                    lambda: bool(self._items) or self._reconnected, timeout
                )
            items = list(self._items)
            self._items.clear()
            self.queued_bytes = 0
            reconnected, self._reconnected = self._reconnected, False
            return items, reconnected


class WakeSink(Sink):
    """The payloads seen since the last wait, for a blocking stream read:
    it only needs to know *whether* one it waits for arrived. Bounded at
    :data:`WAKE_SINK_MAX_PAYLOADS` distinct payloads, past which every wait
    answers "read again"."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._seen: set[str] = set()
        self._overflow = False
        self._reconnected = False
        self._interrupted = False

    def push(self, payload: str) -> None:
        with self._cond:
            if len(self._seen) >= WAKE_SINK_MAX_PAYLOADS:
                self._overflow = True
            else:
                self._seen.add(payload)
            self._cond.notify_all()

    def reconnected(self) -> None:
        with self._cond:
            self._reconnected = True
            self._cond.notify_all()

    def interrupt(self) -> None:
        """Wake a waiter now (an async read that was cancelled)."""
        with self._cond:
            self._interrupted = True
            self._cond.notify_all()

    def clear(self) -> None:
        with self._cond:
            self._seen.clear()
            self._overflow = self._reconnected = self._interrupted = False

    def take_reconnected(self) -> bool:
        with self._cond:
            was, self._reconnected = self._reconnected, False
            return was

    def wait(self, wanted: set[str], timeout: float) -> tuple[bool, bool]:
        """Wait up to ``timeout`` for a wanted payload, a reconnect or an
        interrupt: ``(woken, reconnected)``. Forgets what it saw."""

        def ready() -> bool:
            return (
                self._overflow
                or self._reconnected
                or self._interrupted
                or not self._seen.isdisjoint(wanted)
            )

        with self._cond:
            woken = ready() or self._cond.wait_for(ready, max(timeout, 0.0))
            reconnected = self._reconnected
            self._seen.clear()
            self._overflow = self._reconnected = self._interrupted = False
            return bool(woken), reconnected


class _Command:
    __slots__ = ("kind", "channel", "sink", "barrier", "done", "error", "token")

    def __init__(
        self, kind: str, channel: str, sink: Optional[Sink], barrier: bool
    ) -> None:
        self.kind = kind
        self.channel = channel
        self.sink = sink
        self.barrier = barrier
        self.done = threading.Event()
        self.error: Optional[BaseException] = None
        self.token: Optional[str] = None


def _quote(channel: str) -> str:
    return '"' + channel.replace('"', '""') + '"'


class ListenHub:
    """The process's one ``LISTEN`` session on ``dsn``: see the module
    docstring. Get it with :func:`hub_for`."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pid = os.getpid()
        self._cond = threading.Condition()
        self._sinks: dict[str, list[Sink]] = {}
        self._listening: set[str] = set()
        self._commands: collections.deque[_Command] = collections.deque()
        self._barriers: dict[str, _Command] = {}
        self._thread: Optional[threading.Thread] = None
        self._conn: Any = None
        self.backend_pid: Optional[int] = None
        self.generation = 0
        """Bumped by every successful connect."""
        self.reconnects = 0
        self.connects = 0
        self.dropped = 0
        """Messages every sink of this hub has discarded unread (the
        :class:`QueueSink` caps), summed over the hub's life."""
        self._warned_undecodable = False
        self._heard = 0.0
        """``time.monotonic()`` when the session last proved alive (connect,
        a command's reply, readable data, a liveness reply)."""
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)

    # -- the public face (any thread) ----------------------------------------------

    def attach(
        self,
        channel: str,
        sink: Sink,
        *,
        barrier: bool = True,
        timeout: Optional[float] = None,
    ) -> None:
        """Deliver ``channel``'s notifications to ``sink`` from now on.
        Returns once the sink is live (see the module docstring); raises the
        connection's error when the session cannot be opened."""
        self._own()
        from ...fields.constants import Defaults

        cmd = _Command("attach", channel, sink, barrier)
        with self._cond:
            self._commands.append(cmd)
            self._start()
        self._wake()
        limit = (
            timeout
            if timeout is not None
            else 2 * float(Defaults.PG_CONNECT_TIMEOUT_SECONDS) + 5.0
        )
        if not cmd.done.wait(limit):
            with self._cond:
                if cmd in self._commands:
                    self._commands.remove(cmd)
                if cmd.token is not None:
                    self._barriers.pop(cmd.token, None)
            self.detach(channel, sink)
            raise TimeoutError(
                f"popoto LISTEN on {channel!r} was not live after {limit:.0f}s"
            )
        if cmd.error is not None:
            raise cmd.error

    def detach(self, channel: str, sink: Sink) -> None:
        """Stop delivering to ``sink``; ``UNLISTEN`` the channel when it was
        the last. Never blocks."""
        if self.pid != os.getpid():
            return  # the parent's hub, inherited across a fork: not ours
        with self._cond:
            sinks = self._sinks.get(channel)
            if sinks is not None and sink in sinks:
                sinks.remove(sink)
            for token, cmd in list(self._barriers.items()):
                if cmd.sink is sink and cmd.channel == channel:
                    del self._barriers[token]
                    cmd.done.set()
            if sinks is not None and not sinks:
                del self._sinks[channel]
                if self._thread is not None:
                    self._commands.append(_Command("unlisten", channel, None, False))
            if self._thread is None:
                return
        self._wake()

    def sink_count(self, channel: Optional[str] = None) -> int:
        with self._cond:
            if channel is not None:
                return len(self._sinks.get(channel, ()))
            return sum(len(s) for s in self._sinks.values())

    def listening(self) -> set[str]:
        """The channels the hub has ``LISTEN``\\ ed (its own record)."""
        with self._cond:
            return set(self._listening)

    @property
    def connected(self) -> bool:
        conn = self._conn
        return conn is not None and not conn.closed

    def close(self) -> None:
        """Drop every sink and close the session (tests, shutdown)."""
        if self.pid != os.getpid():
            return
        with self._cond:
            for cmd in self._barriers.values():
                cmd.done.set()
            self._barriers.clear()
            for channel in list(self._sinks):
                del self._sinks[channel]
                if self._thread is not None:
                    self._commands.append(_Command("unlisten", channel, None, False))
            thread = self._thread
        self._wake()
        if thread is not None and thread is not threading.current_thread():
            thread.join(5.0)

    # -- plumbing ------------------------------------------------------------------

    def _own(self) -> None:
        if self.pid != os.getpid():
            raise RuntimeError(
                "a ListenHub belongs to the process that made it; use hub_for()"
            )

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"x")
        except (BlockingIOError, OSError):
            pass  # already pending, or shutting down

    def _drain_wake(self) -> None:
        try:
            while self._wake_r.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def _start(self) -> None:
        """Start the hub's thread (under ``_cond``) unless it runs."""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name=f"popoto-listen-{self.pid}", daemon=True
            )
            self._thread.start()

    # -- the hub's thread ------------------------------------------------------------

    def _connect(self) -> None:
        from . import _import_psycopg
        from ...fields.constants import Defaults

        psycopg = _import_psycopg()
        from psycopg.conninfo import conninfo_to_dict

        try:
            named = conninfo_to_dict(self.dsn)
        except Exception:  # pragma: no cover - libpq parses it below anyway
            named = {}
        extra: dict[str, Any] = {}
        if "application_name" not in named:
            extra["application_name"] = LISTEN_APPLICATION_NAME
        # TCP keepalives, so the OS notices a peer that vanished without a
        # FIN (a NAT entry expiring); a DSN that names one keeps its own.
        for key, value in (
            ("keepalives", Defaults.PG_LISTEN_KEEPALIVES),
            ("keepalives_idle", Defaults.PG_LISTEN_KEEPALIVES_IDLE_SECONDS),
            ("keepalives_interval", Defaults.PG_LISTEN_KEEPALIVES_INTERVAL_SECONDS),
            ("keepalives_count", Defaults.PG_LISTEN_KEEPALIVES_COUNT),
        ):
            if key not in named:
                extra[key] = int(value)
        conn = psycopg.connect(
            self.dsn,
            autocommit=True,
            connect_timeout=max(
                1, int(round(float(Defaults.PG_CONNECT_TIMEOUT_SECONDS)))
            ),
            **extra,
        )
        try:
            # The hub decodes notifications itself (``_decode``) rather than
            # through ``add_notify_handler``: psycopg decodes strictly, so one
            # undecodable payload arriving during an ``execute`` would raise
            # out of it (and lose the command). Replacing the libpq-level
            # handler also bypasses psycopg's own backlog, which the hub never
            # reads.
            enc = conn.info.encoding
            conn.pgconn.notify_handler = lambda n: self._on_notify(
                self._decode(n.relname, enc), self._decode(n.extra, enc)
            )
            with self._cond:
                channels = set(self._sinks) | {
                    c.channel for c in self._barriers.values()
                }
            for channel in sorted(channels):
                conn.execute(f"LISTEN {_quote(channel)}")
        except BaseException:
            _close_quietly(conn)
            raise
        reconnect = bool(channels)  # sinks were live on the session that dropped
        with self._cond:
            self._conn = conn
            self._heard = time.monotonic()
            self.backend_pid = int(conn.info.backend_pid)
            self._listening = channels
            self.generation += 1
            self.connects += 1
            if reconnect:
                self.reconnects += 1
            # Attaches waiting on a barrier the old session never returned:
            # the channel is listened afresh, so they are live now.
            for cmd in self._barriers.values():
                if cmd.sink is not None:
                    self._sinks.setdefault(cmd.channel, []).append(cmd.sink)
                cmd.done.set()
            self._barriers.clear()
            sinks = [s for group in self._sinks.values() for s in group]
        if reconnect:
            logger.info(
                "popoto LISTEN session reopened (backend pid %s), %d channel(s)",
                self.backend_pid,
                len(channels),
            )
            for sink in sinks:
                _call_quietly(sink.resume, conn, self.backend_pid)
            for sink in sinks:
                _call_quietly(sink.reconnected)

    def _drop(self, exc: BaseException) -> None:
        logger.warning("popoto LISTEN session dropped (%s); reconnecting", exc)
        _close_quietly(self._conn)
        with self._cond:
            self._conn = None
            self.backend_pid = None
            self._listening = set()

    def _on_notify(self, channel: str, payload: str) -> None:
        if payload.startswith(_BARRIER_PREFIX):
            token = payload[len(_BARRIER_PREFIX) :]
            with self._cond:
                cmd = self._barriers.pop(token, None)
                if cmd is not None and cmd.sink is not None:
                    self._sinks.setdefault(cmd.channel, []).append(cmd.sink)
            if cmd is not None:
                cmd.done.set()
            return
        with self._cond:
            sinks = list(self._sinks.get(channel, ()))
        for sink in sinks:
            before = sink.dropped
            _call_quietly(sink.push, payload)
            if sink.dropped != before:
                self.dropped += sink.dropped - before

    def _run_command(self, cmd: _Command) -> None:
        conn = self._conn
        channel = cmd.channel
        if cmd.kind == "unlisten":
            with self._cond:
                if channel in self._sinks or any(
                    c.channel == channel for c in self._barriers.values()
                ):
                    return  # re-attached meanwhile
                listening = channel in self._listening
            if listening:
                conn.execute(f"UNLISTEN {_quote(channel)}")
                with self._cond:
                    self._listening.discard(channel)
            return
        # attach
        with self._cond:
            listening = channel in self._listening
        if not listening:
            conn.execute(f"LISTEN {_quote(channel)}")
            with self._cond:
                self._listening.add(channel)
                if cmd.sink is not None:
                    self._sinks.setdefault(channel, []).append(cmd.sink)
            cmd.done.set()
            return
        if not cmd.barrier:
            with self._cond:
                if cmd.sink is not None:
                    self._sinks.setdefault(channel, []).append(cmd.sink)
            cmd.done.set()
            return
        token = uuid.uuid4().hex
        cmd.token = token
        with self._cond:
            self._barriers[token] = cmd
        conn.execute("SELECT pg_notify(%s, %s)", [channel, _BARRIER_PREFIX + token])

    def _idle(self) -> bool:
        """Under ``_cond``: nothing to deliver to and nothing to do."""
        return not self._sinks and not self._commands and not self._barriers

    def _loop(self) -> None:
        from . import _import_psycopg

        psycopg = _import_psycopg()
        backoff = 0.0
        try:
            while True:
                with self._cond:
                    if self._idle():
                        self._thread = None
                        conn, self._conn = self._conn, None
                        self.backend_pid = None
                        self._listening = set()
                        _close_quietly(conn)
                        return
                if self._conn is None:
                    if backoff:
                        self._sleep(backoff)
                        with self._cond:
                            if self._idle():
                                continue  # everyone left while we waited
                    try:
                        self._connect()
                        backoff = 0.0
                    except Exception as exc:
                        backoff = min(
                            max(backoff * 2, LISTEN_RECONNECT_BACKOFF_SECONDS),
                            LISTEN_RECONNECT_MAX_BACKOFF_SECONDS,
                        )
                        self._fail_attaches(exc)
                        logger.warning(
                            "popoto LISTEN session could not connect (%s); "
                            "retrying in %.1fs",
                            exc,
                            backoff,
                        )
                        continue
                try:
                    while True:
                        with self._cond:
                            if not self._commands:
                                break
                            cmd = self._commands.popleft()
                        try:
                            self._run_command(cmd)
                            self._heard = time.monotonic()
                        except psycopg.OperationalError:
                            with self._cond:
                                self._commands.appendleft(cmd)
                                if cmd.token is not None:
                                    self._barriers.pop(cmd.token, None)
                                    cmd.token = None
                            raise
                        except Exception as exc:
                            cmd.error = exc
                            cmd.done.set()
                    self._pump(psycopg)
                except psycopg.OperationalError as exc:
                    self._drop(exc)
                    continue
                with self._cond:
                    if self._idle():
                        continue
                    if self._commands:
                        continue
                wait = self._until_liveness_check()
                if wait is not None and wait <= 0:
                    try:
                        self._check_liveness(psycopg)
                    except psycopg.OperationalError as exc:
                        self._drop(exc)
                    continue
                self._select(wait)
        except BaseException as exc:  # pragma: no cover - a bug, not an outage
            logger.exception("popoto LISTEN hub stopped: %s", exc)
            with self._cond:
                self._thread = None
                for cmd in list(self._commands) + list(self._barriers.values()):
                    cmd.error = exc
                    cmd.done.set()
                self._commands.clear()
                self._barriers.clear()
            raise

    def _fail_attaches(self, exc: BaseException) -> None:
        """A connect failed: fail every attach waiting on it (the caller's
        ``subscribe`` raises, as a direct connect would)."""
        with self._cond:
            failed = [c for c in self._commands if c.kind == "attach"]
            for cmd in failed:
                self._commands.remove(cmd)
                cmd.error = exc
                cmd.done.set()

    def _pump(self, psycopg: Any) -> None:
        """Deliver what the socket holds: everything libpq has buffered,
        then whatever more is readable now."""
        conn = self._conn
        pgconn = conn.pgconn
        pgconn.consume_input()
        self._deliver_notifies(pgconn, conn.info.encoding)
        if pgconn.status != psycopg.pq.ConnStatus.OK:
            raise psycopg.OperationalError("the LISTEN session is not OK")

    def _deliver_notifies(self, pgconn: Any, enc: str) -> None:
        while True:
            n = pgconn.notifies()
            if n is None:
                break
            self._on_notify(self._decode(n.relname, enc), self._decode(n.extra, enc))

    def _decode(self, raw: bytes, enc: str) -> str:
        """``raw`` in the session's encoding; when it is not valid there,
        UTF-8 with replacement characters (module docstring), logged once."""
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError) as exc:
            if not self._warned_undecodable:
                self._warned_undecodable = True
                logger.warning(
                    "popoto LISTEN session received a notification it cannot "
                    "decode as %s (%s); delivering it with replacement "
                    "characters (logged once per session hub)",
                    enc,
                    exc,
                )
            return raw.decode("utf-8", errors="replace")

    def _until_liveness_check(self) -> Optional[float]:
        """Seconds until the session is due a liveness check (``<= 0``: due
        now); ``None`` when the check is disabled (interval ``<= 0``)."""
        from ...fields.constants import Defaults

        interval = float(Defaults.PG_LISTEN_LIVENESS_INTERVAL_SECONDS)
        if interval <= 0:
            return None
        return self._heard + interval - time.monotonic()

    def _check_liveness(self, psycopg: Any) -> None:
        """``SELECT 1`` on the session, waiting at most
        ``Defaults.PG_LISTEN_LIVENESS_TIMEOUT_SECONDS`` for the reply; raises
        ``OperationalError`` (so the session is dropped and reopened) when
        none comes. Run on the hub's thread, between commands, through libpq
        directly so the wait is bounded: a blocking ``execute`` on a
        connection whose peer silently vanished would wait for the kernel's
        retransmission timeout -- many minutes. Notifications that arrive
        meanwhile are delivered in order."""
        from ...fields.constants import Defaults

        timeout = float(Defaults.PG_LISTEN_LIVENESS_TIMEOUT_SECONDS)
        conn = self._conn
        pgconn = conn.pgconn
        enc = conn.info.encoding
        fd = pgconn.socket
        deadline = time.monotonic() + timeout

        def wait(write: bool) -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise psycopg.OperationalError(
                    f"no reply to the LISTEN session's liveness check within "
                    f"{timeout:g}s (PG_LISTEN_LIVENESS_TIMEOUT_SECONDS)"
                )
            try:
                select.select(
                    [] if write else [fd], [fd] if write else [], [], remaining
                )
            except (OSError, ValueError) as exc:
                raise psycopg.OperationalError(str(exc)) from exc

        pgconn.send_query(b"SELECT 1")
        while pgconn.flush():  # 1 while output remains queued (non-blocking)
            wait(write=True)
        while True:
            pgconn.consume_input()
            self._deliver_notifies(pgconn, enc)
            if not pgconn.is_busy():
                break
            wait(write=False)
        while pgconn.get_result() is not None:
            pass  # any reply proves the peer is there
        self._deliver_notifies(pgconn, enc)
        if pgconn.status != psycopg.pq.ConnStatus.OK:
            raise psycopg.OperationalError("the LISTEN session is not OK")
        self._heard = time.monotonic()

    def _select(self, timeout: Optional[float] = None) -> None:
        """Wait for the server's traffic or a wake-up, at most ``timeout``
        seconds (``None``: indefinitely)."""
        conn = self._conn
        try:
            fd = conn.fileno()
        except Exception:  # closed under us
            return
        try:
            readable, _, _ = select.select([fd, self._wake_r], [], [], timeout)
        except (OSError, ValueError):
            return
        if fd in readable:
            self._heard = time.monotonic()
        self._drain_wake()

    def _sleep(self, seconds: float) -> None:
        try:
            select.select([self._wake_r], [], [], seconds)
        except (OSError, ValueError):  # pragma: no cover
            pass
        self._drain_wake()


def _close_quietly(conn: Any) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:  # pragma: no cover - closing a dead socket
        pass


def _call_quietly(fn: Any, *args: Any) -> None:
    try:
        fn(*args)
    except Exception as exc:  # pragma: no cover - a sink is a bug if it raises
        logger.warning("popoto LISTEN sink raised: %s", exc)


_hubs: dict[tuple[str, int], ListenHub] = {}
_hubs_lock = threading.Lock()


def _reset_lock_in_child() -> None:
    """A lock another thread held at ``fork()`` stays held in the child."""
    global _hubs_lock
    _hubs_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_lock_in_child)


def hub_for(dsn: str) -> ListenHub:
    """This process's hub for ``dsn`` (created on first use, and again in a
    forked child, which never reuses the parent's)."""
    key = (dsn, os.getpid())
    hub = _hubs.get(key)
    if hub is not None:
        return hub
    with _hubs_lock:
        hub = _hubs.get(key)
        if hub is None:
            hub = ListenHub(dsn)
            _hubs[key] = hub
        return hub


def close_hubs() -> None:
    """Close every hub this process opened (tests, shutdown). Hubs a forked
    child inherited are left alone: their sessions are the parent's."""
    with _hubs_lock:
        mine = [h for (_, pid), h in _hubs.items() if pid == os.getpid()]
    for hub in mine:
        hub.close()
