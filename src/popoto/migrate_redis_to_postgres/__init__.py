"""One-off migration of agent memory from a Redis snapshot into Postgres (#756).

Run it as a module::

    python -m popoto.migrate_redis_to_postgres \\
        --rdb /archive/run1/dump.rdb --content-dir /archive/run1/content \\
        --run-dir /archive/run1/migration --source-id laptop-1 \\
        --mapping myapp.memory_migration:MAPPINGS

The runbook is ``docs/features/redis-to-postgres-migration.md``. This
docstring is the safety model, because it is the part a later editor must not
erode.

**Snapshot isolation.** The tool never connects to a Redis an operator names.
It has no source-URL, host or port parameter at all. The operator freezes the
writers, runs ``BGSAVE`` on the live server and copies the RDB file (and the
content directory that holds ``.npy`` embeddings and ``ContentField`` files);
the tool then copies that RDB into a private (``0700``) temporary directory,
starts its own ``redis-server`` with persistence switched off, and reads only
from that process, identified by its ``run_id``. The server listens on **no
TCP port** (``port 0``): only on a unix socket inside that private directory,
behind a random ``requirepass`` written to a ``0600`` config file (never the
command line, where ``ps`` shows it).

**Nothing outlives the run.** Before any copy of the store exists, the tool
starts a watchdog (``_watchdog.py``, standard library only, in a session of
its own). The watchdog spawns the server and is told every private directory
(the RDB copy, the content copy, the socket). When the tool exits for any
reason -- success, an exception, ``SIGTERM``/``SIGHUP``/``SIGINT`` (all three
unwind normally), or a ``SIGKILL`` nothing can catch -- its end of the
watchdog's pipe closes, and the watchdog stops the server and removes the
directories. A dropped SSH session therefore never leaves the agent's memory
being served.

**The CLI entry is** ``__main__.py`` **and nothing else.** It imports
:func:`main` from this package, so the ``ModelMapping`` an operator's
mapping module imports is the same class the CLI checks against.

**Two read paths, both pinned to the throwaway.** The raw inventory pass goes
through :class:`ReadOnlyRedis`, whose only constructor takes a
:class:`ThrowawayRedis` and which refuses every command outside a read-only
allowlist *before* it is sent (:class:`ForbiddenCommand`). Record export
reuses :func:`popoto.transfer.export_records`, which reads through popoto's
global client; the tool rebinds that client to the throwaway and refuses to
continue (:class:`LiveRedisRefused`) unless the bound client's host, port and
``run_id`` are the throwaway's -- checked on the connection parameters before
any command, so a client still bound to the default database 0 of a live
server is refused without a byte reaching it. Popoto read paths that write
(``get_many_objects``' orphan purge, staged reads) therefore land in a
disposable process.

**Postgres is written only through popoto, plus the tool's own provenance.**
Records land through :func:`popoto.transfer.import_records` on a
``PostgresBackend`` -- the save path, so every derived table (BM25 postings,
narrow vector rows, membership tokens, indexes) is built by the engine that
will maintain it. Every native write sets ``_migrated_from = NULL``; the tool
then writes ``_migrated_from``, ``_estimated_fields``, ``_created_at`` and
``_updated_at`` (and the staged-read columns), its ledger and its resume
marker. **Each batch is one Postgres transaction**: the saves, every
carried-state restore (``import_records(uow=...)``), the provenance, the
ledger rows and the marker commit together, so a crash leaves none of a
batch or all of it (:func:`_load_model`). A ``pending`` ledger row can only
be left by a run from before batches were atomic; it is how a resumed run
tells its own half-written rows from rows popoto wrote natively. Only the
SAME run (``run_id``) adopts its pending rows, and only while each still
holds exactly what it imported; a row another run left pending, or one
popoto saved after the crash, is treated as native. Verification never
trusts that alone: every row this run queued a save for must hold the
snapshot's carried state (``carried_state``). A session advisory lock on
the target schema keeps two runs from loading into it at once.

**Merge rule** (v2 plan, "Reconciliation with #755 / #756 / #758"): several
per-machine stores load into the one central database, one run per store,
each with its own ``--source-id``. Within a key, an equal payload is
deduplicated (the source is recorded under ``_migrated_from.duplicates``);
a differing payload keeps the later ``_updated_at``, ties going to the
greater source id, and the loser is logged under ``_migrated_from.losers``.
The same source re-run with a newer snapshot replaces its own rows (the
delta mechanism). A row popoto wrote natively (``_migrated_from IS NULL``)
is never overwritten. The source id lives only in ``_migrated_from``; it
never becomes part of a key or a scope. A run resumed after another store
merged over some of its committed rows verifies those rows against their
winner (:func:`_superseded_by_merge`), not its own snapshot -- but only
where the winner's own run and ledger rows back the claim.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import datetime
import getpass
import hashlib
import hmac
import importlib
import io
import json
import logging
import os
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

import redis

logger = logging.getLogger("popoto.migrate")

__all__ = [
    "ForbiddenCommand",
    "InventoryStop",
    "LiveRedisRefused",
    "MigrationConfig",
    "MigrationError",
    "MigrationRefused",
    "MigrationReport",
    "ModelMapping",
    "READ_ONLY_COMMANDS",
    "ReadOnlyRedis",
    "TargetNotEmpty",
    "ThrowawayRedis",
    "main",
    "read_report_key",
    "run_migration",
    "seal_report",
    "verify_report",
]

# -- errors --------------------------------------------------------------------


class MigrationError(Exception):
    """Base class for every refusal and stop the tool raises."""


class MigrationRefused(MigrationError):
    """A preflight refused the run before anything was read or written."""


class LiveRedisRefused(MigrationRefused):
    """The Redis client the tool would read from is not its own throwaway
    server (wrong host, port or ``run_id``): most likely a live store, or
    popoto's default database-0 binding."""


class TargetNotEmpty(MigrationRefused):
    """The target schema already holds rows for a model being migrated, and
    neither ``--merge`` nor a resume of the same run explains them."""


class ForbiddenCommand(MigrationError):
    """A command outside :data:`READ_ONLY_COMMANDS` was refused before it was
    sent."""


class InventoryStop(MigrationError):
    """The inventory found state the tool cannot account for (an
    expected-empty key family that is not empty, or a key family it does not
    know). Nothing has been written to Postgres."""

    def __init__(self, message: str, reasons: Sequence[str]) -> None:
        super().__init__(message)
        self.reasons = list(reasons)


# -- the throwaway source ------------------------------------------------------

READ_ONLY_COMMANDS = frozenset(
    {
        "DBSIZE",
        "EXISTS",
        "GET",
        "HGET",
        "HGETALL",
        "HKEYS",
        "HLEN",
        "HSCAN",
        "INFO",
        "LLEN",
        "LRANGE",
        "PING",
        "PTTL",
        "SCAN",
        "SCARD",
        "SISMEMBER",
        "SMEMBERS",
        "SSCAN",
        "STRLEN",
        "TYPE",
        "XLEN",
        "ZCARD",
        "ZRANGE",
        "ZSCAN",
        "ZSCORE",
    }
)
"""The only commands :class:`ReadOnlyRedis` sends."""

_STARTUP_TIMEOUT_SECONDS = 60.0
_STOP_TIMEOUT_SECONDS = 10.0
_SUPERVISOR_REPLY_SECONDS = 30.0
_WATCHDOG = Path(__file__).with_name("_watchdog.py")
#: A unix socket path must fit ``sockaddr_un.sun_path`` (104 bytes on macOS,
#: 108 on Linux), with room for the terminating NUL.
_MAX_SOCKET_PATH = 100


def _rdb_magic_ok(path: Path) -> bool:
    with open(path, "rb") as handle:
        return handle.read(5) == b"REDIS"


def _conf_quote(value: str) -> str:
    """A redis.conf string argument, double-quoted with escapes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _private_dir(prefix: str, parent: "str | None" = None) -> Path:
    """A fresh directory only this user can enter (``mkdtemp`` is ``0700``;
    the ``chmod`` states it rather than trusting the umask-free default)."""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    os.chmod(path, 0o700)
    return path


class _Supervisor:
    """The tool's handle on its watchdog process (``_watchdog.py``).

    Started before any copy of the store exists. It owns the throwaway
    ``redis-server`` (it is the server's parent) and the run's private
    directories, and stops and removes them when the tool exits -- normally,
    on an exception, on a signal, or killed with ``SIGKILL``. It runs in a
    session of its own, so the signals a terminal sends the tool's process
    group never reach it or the server."""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen[bytes]] = None

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self._process is not None else None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> "_Supervisor":
        self._process = subprocess.Popen(
            [sys.executable, "-I", str(_WATCHDOG), str(os.getpid())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        reply = self._read()
        if not reply.get("ready"):
            self.close()
            raise MigrationRefused(f"the watchdog did not start: {reply}")
        return self

    def _read(self) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise MigrationError("the watchdog is not running")
        readable, _, _ = select.select(
            [process.stdout], [], [], _SUPERVISOR_REPLY_SECONDS
        )
        line = process.stdout.readline() if readable else b""
        if not line:
            raise MigrationError("the watchdog stopped answering")
        return dict(json.loads(line))

    def _call(self, message: Mapping[str, Any]) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise MigrationError("the watchdog is not running")
        process.stdin.write((json.dumps(dict(message)) + "\n").encode())
        process.stdin.flush()
        reply = self._read()
        if "error" in reply:
            raise MigrationError(f"watchdog: {reply['error']}")
        return reply

    def watch(self, path: Path) -> None:
        """Have ``path`` removed when the run ends, however it ends."""
        self._call({"op": "watch", "path": str(path)})

    def spawn(self, argv: Sequence[str]) -> int:
        return int(self._call({"op": "spawn", "argv": list(argv)})["pid"])

    def stop_child(self, pid: int) -> None:
        self._call({"op": "stop", "pid": pid})

    def close(self) -> None:
        """End of run: the watchdog stops what it spawned, removes what it
        watches, and exits."""
        process = self._process
        if process is None:
            return
        self._process = None
        with contextlib.suppress(OSError):
            if process.stdin is not None:
                process.stdin.close()
        try:
            process.wait(timeout=_STOP_TIMEOUT_SECONDS * 3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        if process.stdout is not None:
            process.stdout.close()


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - a reused pid of another user
        return False
    return True


class ThrowawayRedis:
    """A private ``redis-server`` serving a copy of an RDB snapshot.

    Nothing about it is reachable from outside the run:

    * it listens on **no TCP port** (``port 0``) -- only on a unix socket in
      a fresh ``0700`` directory, ``unixsocketperm 700``;
    * it requires a random password (``requirepass``), passed in a ``0600``
      config file rather than on the command line, where ``ps`` would show
      it; ``protected-mode yes``;
    * persistence is off (``save ""``, ``appendonly no``), so it never writes
      anything back.

    It is spawned by the run's watchdog (:class:`_Supervisor`), which stops
    it and removes its directories when the tool exits for any reason --
    ``SIGKILL`` included. :meth:`stop` (and leaving the ``with`` block) does
    the same at once, and is idempotent.
    """

    def __init__(
        self,
        rdb_path: "str | os.PathLike[str]",
        *,
        redis_server: str = "redis-server",
        work_parent: "str | os.PathLike[str] | None" = None,
        supervisor: Optional[_Supervisor] = None,
    ) -> None:
        self.rdb_path = Path(rdb_path)
        self.redis_server = redis_server
        self.work_parent = Path(work_parent) if work_parent is not None else None
        self.socket_path: str = ""
        self.password: str = ""
        self.pid: Optional[int] = None
        self.run_id: str = ""
        self.directory: Optional[Path] = None
        self.socket_directory: Optional[Path] = None
        self._supervisor = supervisor
        self._owns_supervisor = False

    # context manager ------------------------------------------------------
    def __enter__(self) -> "ThrowawayRedis":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def supervisor(self) -> Optional[_Supervisor]:
        return self._supervisor

    def start(self) -> None:
        binary = shutil.which(self.redis_server)
        if binary is None:
            raise MigrationRefused(
                f"redis-server binary {self.redis_server!r} not found; install the "
                "same Redis major version the snapshot was written by"
            )
        if not self.rdb_path.is_file():
            raise MigrationRefused(f"RDB snapshot {self.rdb_path} does not exist")
        if not _rdb_magic_ok(self.rdb_path):
            raise MigrationRefused(f"{self.rdb_path} is not an RDB file")
        try:
            if self._supervisor is None:
                self._supervisor = _Supervisor().start()
                self._owns_supervisor = True
            supervisor = self._supervisor
            parent = str(self.work_parent) if self.work_parent is not None else None
            self.directory = _private_dir("popoto-migrate-", parent)
            supervisor.watch(self.directory)
            socket_dir = self.directory
            if len(str(socket_dir / "redis.sock")) > _MAX_SOCKET_PATH:
                socket_dir = _private_dir("pmig-")
                if len(str(socket_dir / "redis.sock")) > _MAX_SOCKET_PATH:
                    shutil.rmtree(socket_dir, ignore_errors=True)
                    socket_dir = _private_dir("pmig-", "/tmp")
                self.socket_directory = socket_dir
                supervisor.watch(socket_dir)
            self.socket_path = str(socket_dir / "redis.sock")
            self.password = secrets.token_hex(32)
            shutil.copyfile(self.rdb_path, self.directory / "dump.rdb")
            conf = self.directory / "redis.conf"
            descriptor = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(
                    "\n".join(
                        [
                            "port 0",
                            "bind 127.0.0.1",
                            "protected-mode yes",
                            f"unixsocket {_conf_quote(self.socket_path)}",
                            "unixsocketperm 700",
                            f"requirepass {_conf_quote(self.password)}",
                            f"dir {_conf_quote(str(self.directory))}",
                            "dbfilename dump.rdb",
                            'save ""',
                            "appendonly no",
                            "daemonize no",
                            f"logfile {_conf_quote(str(self.directory / 'redis.log'))}",
                        ]
                    )
                    + "\n"
                )
            self.pid = supervisor.spawn([binary, str(conf)])
            error = self._wait_ready()
            if error is not None:
                raise MigrationRefused(f"throwaway redis-server did not start: {error}")
        except BaseException:
            self.stop()
            raise

    def client(self, **kwargs: Any) -> redis.Redis:
        """A plain client of this server (its socket and password)."""
        return redis.Redis(
            unix_socket_path=self.socket_path, password=self.password, **kwargs
        )

    def _wait_ready(self) -> Optional[str]:
        """``None`` once the server answers and has finished loading the
        snapshot, else the reason it never did."""
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        client = self.client(socket_timeout=5)
        try:
            while time.monotonic() < deadline:
                if not _pid_alive(self.pid):
                    return f"exited: {self._log_tail()}"
                try:
                    info = client.info()
                except (redis.ConnectionError, redis.BusyLoadingError):
                    time.sleep(0.05)
                    continue
                if int(info.get("loading", 0)):
                    time.sleep(0.05)
                    continue
                if int(info.get("process_id", -1)) != self.pid:
                    return "another process answered on the private socket"
                if int(info.get("tcp_port", -1)) != 0:
                    return f"it listens on TCP port {info.get('tcp_port')}"
                self.run_id = str(info["run_id"])
                return None
            return "timed out waiting for the snapshot to load"
        finally:
            client.close()

    def _log_tail(self) -> str:
        if self.directory is None:
            return ""
        try:
            text = (self.directory / "redis.log").read_text(errors="replace")
        except OSError:
            return ""
        return " | ".join(text.strip().splitlines()[-5:])

    def _kill(self) -> None:
        pid, self.pid = self.pid, None
        if not pid:
            return
        supervisor = self._supervisor
        if supervisor is not None and supervisor.alive:
            with contextlib.suppress(MigrationError, OSError, ValueError):
                supervisor.stop_child(pid)
                return
        # The watchdog is gone (killed on its own): stop the server directly.
        if _pid_alive(pid):  # pragma: no cover - needs the watchdog killed
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)

    def stop(self) -> None:
        self._kill()
        for directory in (self.directory, self.socket_directory):
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
        self.directory = None
        self.socket_directory = None
        if self._owns_supervisor and self._supervisor is not None:
            self._supervisor.close()
            self._supervisor = None
            self._owns_supervisor = False

    @property
    def running(self) -> bool:
        return bool(self.pid) and _pid_alive(self.pid)


class _ReadOnlyPipeline(redis.client.Pipeline):
    def execute_command(self, *args: Any, **kwargs: Any) -> Any:
        _check_read_only(args)
        return super().execute_command(*args, **kwargs)


def _check_read_only(args: Sequence[Any]) -> None:
    if not args:
        raise ForbiddenCommand("empty command")
    name = args[0]
    if isinstance(name, bytes):
        name = name.decode()
    name = str(name).upper().split(" ")[0]
    if name not in READ_ONLY_COMMANDS:
        raise ForbiddenCommand(
            f"{name} is not a read-only command; the migration tool sends only "
            f"{', '.join(sorted(READ_ONLY_COMMANDS))}"
        )


class ReadOnlyRedis(redis.Redis):
    """A client of a :class:`ThrowawayRedis` that refuses, before sending,
    any command outside :data:`READ_ONLY_COMMANDS`.

    There is deliberately no URL, host or port constructor: the only way to
    build one is from a running throwaway server (its private socket and
    password), and it re-checks that server's ``run_id`` on construction."""

    def __init__(self, server: ThrowawayRedis, db: int = 0) -> None:
        if not server.running or not server.run_id:
            raise LiveRedisRefused("the throwaway redis-server is not running")
        super().__init__(
            unix_socket_path=server.socket_path,
            password=server.password,
            db=db,
            socket_timeout=30,
        )
        info = self.info("server")
        if str(info.get("run_id")) != server.run_id:
            raise LiveRedisRefused(
                f"{server.socket_path} is answered by run_id {info.get('run_id')}, "
                f"not the throwaway server's {server.run_id}"
            )

    def execute_command(self, *args: Any, **options: Any) -> Any:
        _check_read_only(args)
        return super().execute_command(*args, **options)

    def pipeline(self, transaction: bool = False, shard_hint: Any = None) -> Any:
        return _ReadOnlyPipeline(
            self.connection_pool, self.response_callbacks, False, shard_hint
        )


def assert_bound_to_throwaway(client: Any, server: ThrowawayRedis) -> None:
    """Refuse unless ``client`` talks to ``server``.

    The connection parameters are compared first: the throwaway listens only
    on its private unix socket, so a client with a host and port -- popoto's
    default ``localhost`` database 0 included -- or any other socket is
    refused without a command reaching it. Only then is the server's
    ``run_id`` read and compared."""
    kwargs = dict(getattr(client.connection_pool, "connection_kwargs", {}) or {})
    path = kwargs.get("path") or kwargs.get("unix_socket_path")
    if not server.socket_path or str(path or "") != server.socket_path:
        where = (
            f"unix socket {path}"
            if path
            else f"{kwargs.get('host')}:{kwargs.get('port')}/db{kwargs.get('db')}"
        )
        raise LiveRedisRefused(
            f"popoto's Redis client is bound to {where}, not the throwaway "
            f"snapshot server's private socket {server.socket_path or '(none)'}. "
            "The migration never reads a live store."
        )
    run_id = str(client.info("server").get("run_id"))
    if run_id != server.run_id:
        raise LiveRedisRefused(
            f"{path} reports run_id {run_id}, not the throwaway server's "
            f"{server.run_id}"
        )


# -- mapping -------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ModelMapping:
    """How one popoto model crosses.

    The record itself crosses field for field: the target is the same model
    class bound to Postgres, so the column set is the one popoto compiles
    (``docs/features/postgres-backend.md``). What a mapping adds is the
    evidence the tool needs and Redis never stored.

    Attributes:
        model: The popoto ``Model`` class.
        created_at_paths: Extra timestamp paths for the ``_created_at``
            estimate, as ``field.key[].key`` (``[]`` iterates a list). The
            estimate is the minimum of every timestamp found here, in the
            access log, in the staged reads and in the decay clocks.
        updated_at_paths: Extra timestamp paths for the ``_updated_at``
            estimate (the maximum, together with the decay clocks and any
            ``auto_now`` datetime field).
        sentinel_values: ``{field: values}`` that are carried verbatim but
            counted in the report (Valor's retirement markers in
            ``superseded_by``).
        id_patterns: ``{field: regex}`` every record must match; a record
            that does not is rejected and counted.
    """

    model: Any
    created_at_paths: tuple[str, ...] = ()
    updated_at_paths: tuple[str, ...] = ()
    sentinel_values: Mapping[str, tuple[str, ...]] = dataclasses.field(
        default_factory=dict
    )
    id_patterns: Mapping[str, str] = dataclasses.field(default_factory=dict)

    @property
    def name(self) -> str:
        return str(self.model.__name__)


def _walk_path(value: Any, parts: Sequence[str]) -> Iterator[Any]:
    if not parts:
        yield value
        return
    head, rest = parts[0], parts[1:]
    iterate = head.endswith("[]")
    key = head[:-2] if iterate else head
    if not isinstance(value, Mapping) or key not in value:
        return
    inner = value[key]
    if iterate:
        if isinstance(inner, (list, tuple)):
            for item in inner:
                yield from _walk_path(item, rest)
        return
    yield from _walk_path(inner, rest)


def _timestamps_at(values: Mapping[str, Any], path: str) -> list[float]:
    found: list[float] = []
    for item in _walk_path(values, path.split(".")):
        stamp = _as_epoch(item)
        if stamp is not None:
            found.append(stamp)
    return found


def _as_epoch(item: Any) -> Optional[float]:
    if isinstance(item, bool):
        return None
    if isinstance(item, (int, float)):
        return float(item) if item > 0 else None
    if isinstance(item, datetime.datetime):
        if item.tzinfo is None:
            item = item.replace(tzinfo=datetime.timezone.utc)
        return item.timestamp()
    if isinstance(item, str):
        try:
            return _as_epoch(datetime.datetime.fromisoformat(item))
        except ValueError:
            return None
    return None


# -- configuration and report --------------------------------------------------

DEFAULT_BATCH_SIZE = 200
"""Records per load batch: one Postgres transaction each (its saves,
carried state, provenance, ledger rows and resume marker)."""

VERIFY_DECAY_TOP_N = 50
"""Records compared per partition in the decay-ranking check."""

VERIFY_BM25_TOP_K = 10
"""Results compared per sample query in the BM25 check."""

VERIFY_MISMATCH_DETAIL = 20
"""Mismatching keys listed (with their differing parts) per model."""


@dataclasses.dataclass
class MigrationConfig:
    """One run: one snapshot of one store into one Postgres schema.

    ``postgres_dsn`` and ``postgres_schema`` default to the library's own
    variables, ``POPOTO_POSTGRES_URL`` and ``POPOTO_POSTGRES_SCHEMA``. There is
    no Redis URL: the source is ``rdb_path``."""

    rdb_path: Path
    run_dir: Path
    source_id: str
    mappings: Sequence[ModelMapping]
    content_dir: Optional[Path] = None
    postgres_dsn: Optional[str] = None
    postgres_schema: Optional[str] = None
    source_db: int = 0
    merge: bool = False
    resume: bool = False
    dry_run: bool = False
    allow_empty: bool = False
    accept_unclassified: bool = False
    batch_size: int = DEFAULT_BATCH_SIZE
    redis_server: str = "redis-server"
    verify_sample: int = 25
    operator: str = ""
    report_key: Optional[Path] = None


@dataclasses.dataclass
class MigrationReport:
    """The sealed result of a run (``report.json`` / ``report.txt``): a
    checksum always, an HMAC with an operator key (:func:`seal_report`)."""

    run_id: str
    source_id: str
    data: dict[str, Any]

    @property
    def verdict(self) -> str:
        return str(self.data.get("verdict", ""))

    @property
    def clean(self) -> bool:
        return self.verdict == "clean"

    @property
    def lossy(self) -> dict[str, int]:
        return dict(self.data.get("lossy", {}))

    def summary(self) -> str:
        return render_summary(self.data)


# -- inventory -----------------------------------------------------------------

IRREPLACEABLE = "irreplaceable"
REBUILDABLE = "rebuildable"
NOT_CARRIED = "not_carried"
EXPECTED_EMPTY = "expected_empty"
UNCLASSIFIED = "unclassified"

#: Key-family prefix -> (disposition, what happens to it).
FAMILY_DISPOSITIONS: dict[str, tuple[str, str]] = {
    "record": (IRREPLACEABLE, "the record hash; carried field for field"),
    "$ConfidencF": (
        IRREPLACEABLE,
        "confidence evidence; carried to <f>__conf/n/corr/contra",
    ),
    "$AT:meta": (
        IRREPLACEABLE,
        "access counters; carried to _access_count/_last_accessed",
    ),
    "$AT:staged": (
        IRREPLACEABLE,
        "staged reads; carried by the tool to _staged_reads/_staged_at",
    ),
    "$AT:access_log": (
        NOT_CARRIED,
        "confirmed access log; Postgres keeps none (lossy, counted)",
    ),
    "$CoOccurrencF": (IRREPLACEABLE, "graph edges; carried (truncated to max_edges)"),
    "$CyclicDecayF:cycles": (
        IRREPLACEABLE,
        "cycles; carried (declared baseline dropped)",
    ),
    "$CyclicDecayF:pressure": (IRREPLACEABLE, "pressure; carried"),
    "$CyclicDecayF": (REBUILDABLE, "the decay clock index; rebuilt from the record"),
    "$ValidityF": (IRREPLACEABLE, "validity intervals and supersession chain; carried"),
    "$PL": (IRREPLACEABLE, "prediction ledger; carried"),
    "$Class": (REBUILDABLE, "the class set; becomes the table"),
    "$KeyF": (REBUILDABLE, "key-field index; a B-tree"),
    "$UniqueKeyF": (REBUILDABLE, "unique key index; a UNIQUE index"),
    "$AutoKeyF": (REBUILDABLE, "auto key index; a B-tree"),
    "$IndexedF": (REBUILDABLE, "indexed field; a B-tree"),
    "$UniqueF": (REBUILDABLE, "unique field; a UNIQUE index"),
    "$SortedF": (REBUILDABLE, "sorted index; a B-tree"),
    "$SortedKeyF": (REBUILDABLE, "sorted key index; a B-tree"),
    "$DecayingSortF": (REBUILDABLE, "decay index; rebuilt from the record's clock"),
    "$TagF": (REBUILDABLE, "tag index; a GIN index"),
    "$TagPtr": (REBUILDABLE, "tag pointers; a GIN index"),
    "$RelationshipF": (REBUILDABLE, "relationship index; a B-tree"),
    "$GeoF": (REBUILDABLE, "geo index; rebuilt by the save"),
    "$Index": (REBUILDABLE, "composite index; a B-tree"),
    "$BM25": (REBUILDABLE, "BM25 postings; rebuilt by the save"),
    "$EF": (REBUILDABLE, "existence filter; rebuilt exact by the save"),
    "$FS": (NOT_CARRIED, "frequency sketch; reset to one count per record (lossy)"),
    "$WF": (NOT_CARRIED, "write-filter priority tier; not stored on Postgres"),
    "stream": (NOT_CARRIED, "event stream; records cross, the stream does not"),
    "$TOMBPRIOR": (EXPECTED_EMPTY, "tombstone priors; no carry path"),
    "$IdxPtr": (EXPECTED_EMPTY, "legacy index pointers"),
}

_SUBKIND_PREFIXES = ("$AT", "$CyclicDecayF")


def _family_of(
    key: str, model_names: Mapping[str, str], streams: Mapping[str, str]
) -> tuple[Optional[str], str]:
    """``(model name or None, family)`` for one key."""
    if key in streams:
        return streams[key], "stream"
    head, _, rest = key.partition(":")
    if head in model_names:
        return head, "record"
    if not head.startswith("$"):
        return None, head
    model, _, tail = rest.partition(":")
    if model not in model_names:
        return None, head
    if head in _SUBKIND_PREFIXES:
        if head == "$AT":
            sub = tail.split(":", 1)[0]
            return model, f"$AT:{sub}"
        parts = tail.split(":")
        if len(parts) >= 2 and parts[1] in ("cycles", "pressure"):
            return model, f"{head}:{parts[1]}"
    return model, head


def _stream_keys(mappings: Sequence[ModelMapping]) -> dict[str, str]:
    out: dict[str, str] = {}
    for mapping in mappings:
        name = getattr(mapping.model, "_stream_name", None)
        if name:
            out[f"stream:{name}"] = mapping.name
    return out


def run_inventory(
    client: ReadOnlyRedis,
    mappings: Sequence[ModelMapping],
    content_dir: Optional[Path],
) -> dict[str, Any]:
    """Account for every key in the snapshot.

    Returns per-model family counts with their disposition, the record-level
    facts the transform needs (orphan hashes, staged reads, per-record TTLs,
    decay-index cross-checks, embedding files), the keys outside the migrated
    models, and the stop reasons."""
    names = {m.name: m.name for m in mappings}
    streams = _stream_keys(mappings)
    families: dict[str, dict[str, int]] = {m.name: {} for m in mappings}
    hashes: dict[str, set[str]] = {m.name: set() for m in mappings}
    out_of_scope: dict[str, int] = {}
    ttl_records: dict[str, list[str]] = {m.name: [] for m in mappings}
    meta_ttl_records: dict[str, list[str]] = {m.name: [] for m in mappings}
    meta_ttl_ms = {
        m.name: int(getattr(m.model._meta, "ttl", None) or 0) * 1000 for m in mappings
    }
    staged: dict[str, dict[str, dict[str, Any]]] = {m.name: {} for m in mappings}
    stream_lengths: dict[str, int] = {}
    nul_fields = 0
    expected_empty_found: list[str] = []
    for raw in client.scan_iter(count=1000):
        key = (
            raw.decode("utf-8", "surrogateescape")
            if isinstance(raw, bytes)
            else str(raw)
        )
        model, family = _family_of(key, names, streams)
        if model is None:
            out_of_scope[family] = out_of_scope.get(family, 0) + 1
            continue
        families[model][family] = families[model].get(family, 0) + 1
        if family == "record":
            kind = client.type(key)
            kind = kind.decode() if isinstance(kind, bytes) else str(kind)
            if kind != "hash":
                families[model]["record:not_a_hash"] = (
                    families[model].get("record:not_a_hash", 0) + 1
                )
                continue
            hashes[model].add(key)
            remaining = int(client.pttl(key))
            if remaining > 0:
                # Meta.ttl expires every record of the model, so a record
                # with no more than Meta.ttl left is the class TTL ticking
                # down -- Postgres re-applies Meta.ttl from the import, so
                # it is not a per-record TTL. Only a TTL on a model without
                # Meta.ttl, or one longer than Meta.ttl, was set per record.
                # (A per-record TTL SHORTER than Meta.ttl cannot be told
                # from the class TTL in a snapshot; it is counted there.)
                if meta_ttl_ms[model] and remaining <= meta_ttl_ms[model]:
                    meta_ttl_records[model].append(key)
                else:
                    ttl_records[model].append(key)
            for field_name in client.hkeys(key):
                if b"\x00" in (field_name if isinstance(field_name, bytes) else b""):
                    nul_fields += 1
        elif family == "$AT:staged":
            member = key.split(":", 3)[3] if key.count(":") >= 3 else ""
            entries = client.lrange(key, 0, -1)
            stamps = [_as_epoch(float(e)) for e in entries if _is_number(e)]
            valid = [s for s in stamps if s is not None]
            staged[model][member] = {
                "count": len(entries),
                "latest": max(valid) if valid else None,
                "pttl": int(client.pttl(key)),
            }
        elif family == "stream":
            stream_lengths[model] = int(client.xlen(key))
        if FAMILY_DISPOSITIONS.get(family, (UNCLASSIFIED, ""))[0] == EXPECTED_EMPTY:
            expected_empty_found.append(key)

    per_model: dict[str, Any] = {}
    stops: list[str] = []
    for mapping in mappings:
        name = mapping.name
        class_key = mapping.model._meta.db_class_set_key.redis_key
        members = {
            m.decode() if isinstance(m, bytes) else str(m)
            for m in client.smembers(class_key)
        }
        orphans = sorted(hashes[name] - members)
        dangling = sorted(members - hashes[name])
        decay = _decay_crosscheck(client, mapping, hashes[name])
        embeddings = _embedding_inventory(client, mapping, hashes[name], content_dir)
        fam = {
            family: {
                "keys": count,
                "disposition": FAMILY_DISPOSITIONS.get(family, (UNCLASSIFIED, ""))[0],
                "note": FAMILY_DISPOSITIONS.get(family, ("", "unknown key family"))[1],
            }
            for family, count in sorted(families[name].items())
        }
        unclassified = sorted(
            f for f, v in fam.items() if v["disposition"] == UNCLASSIFIED
        )
        for family in unclassified:
            stops.append(f"{name}: unclassified key family {family!r}")
        if families[name].get("record:not_a_hash"):
            stops.append(f"{name}: keys in the record namespace that are not hashes")
        per_model[name] = {
            "families": fam,
            "record_hashes": len(hashes[name]),
            "class_set_members": len(members),
            "orphan_hashes": orphans,
            "class_members_without_hash": dangling,
            "ttl_records": sorted(ttl_records[name]),
            "meta_ttl_records": sorted(meta_ttl_records[name]),
            "staged": staged[name],
            "event_stream_entries": stream_lengths.get(name, 0),
            "decay_index": decay,
            "embeddings": embeddings,
        }
    if expected_empty_found:
        stops.append(
            "expected-empty key families are not empty: "
            + ", ".join(sorted(expected_empty_found)[:10])
        )
    if nul_fields:
        stops.append(f"{nul_fields} legacy NUL-byte index-pointer hash field(s)")
    return {
        "dbsize": int(client.dbsize()),
        "models": per_model,
        "out_of_scope": dict(sorted(out_of_scope.items())),
        "stops": stops,
    }


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _decay_crosscheck(
    client: ReadOnlyRedis, mapping: ModelMapping, record_keys: set[str]
) -> dict[str, Any]:
    """Each decay index's score against the record hash's clock: the two
    independent encodings of the same number (#757 Risk 5)."""
    from ..fields.decaying_sorted_field import DecayingSortedField
    from ..models.encoding import decode_popoto_model_hashmap

    out: dict[str, Any] = {}
    model = mapping.model
    for field_name, field in model._meta.fields.items():
        if not isinstance(field, DecayingSortedField):
            continue
        prefix = field.__class__.get_sortedset_db_key(model, field_name).redis_key
        indexed: dict[str, float] = {}
        for raw in client.scan_iter(match=prefix + "*", count=1000):
            zkey = raw.decode() if isinstance(raw, bytes) else str(raw)
            if client.type(zkey) not in (b"zset", "zset"):
                continue
            pairs: list[Any] = list(client.zrange(zkey, 0, -1, withscores=True))
            for member, score in pairs:
                name = member.decode() if isinstance(member, bytes) else str(member)
                indexed[name] = float(score)
        mismatched = 0
        missing = 0
        for key in record_keys:
            raw_hash = client.hgetall(key)
            try:
                instance = decode_popoto_model_hashmap(
                    model, raw_hash, source_redis_key=key
                )
            except Exception:  # decoding failures surface in the export
                continue
            clock = getattr(instance, field_name, None)
            if key not in indexed:
                missing += clock is not None
                continue
            if clock is None or abs(float(clock) - float(indexed[key])) > 1e-6:
                mismatched += 1
        out[field_name] = {
            "indexed": len(indexed),
            "score_mismatches": mismatched,
            "records_missing_from_index": missing,
        }
    return out


def _embedding_inventory(
    client: ReadOnlyRedis,
    mapping: ModelMapping,
    record_keys: set[str],
    content_dir: Optional[Path],
) -> dict[str, Any]:
    from ..fields.embedding_field import EmbeddingField

    out: dict[str, Any] = {}
    model = mapping.model
    for field_name, field in model._meta.fields.items():
        if not isinstance(field, EmbeddingField):
            continue
        directory = (
            content_dir / ".embeddings" / mapping.name
            if content_dir is not None
            else None
        )
        files = (
            {p.name for p in directory.glob("*.npy")}
            if directory is not None and directory.is_dir()
            else set()
        )
        expected = {
            os.path.basename(EmbeddingField._embedding_path(mapping.name, key))
            for key in record_keys
        }
        out[field_name] = {
            "files": len(files),
            "records": len(record_keys),
            "records_without_file": len(expected - files),
            "files_without_record": len(files - expected),
        }
    return out


# -- export --------------------------------------------------------------------


@contextlib.contextmanager
def _redis_side(models: Sequence[Any]) -> Iterator[None]:
    """Bind ``models`` to the Redis backend: the process default, and any
    ``Meta.backend`` a post-cutover model declares, for the block."""
    from ..backends import reset_bindings, set_backend

    saved = {m: getattr(m._meta, "backend", None) for m in models}
    previous = set_backend("redis")
    try:
        for model in models:
            model._meta.backend = None
        reset_bindings(list(models))
        yield
    finally:
        for model, value in saved.items():
            model._meta.backend = value
        set_backend(previous)
        reset_bindings(list(models))


@contextlib.contextmanager
def _postgres_side(models: Sequence[Any], backend: Any) -> Iterator[None]:
    from ..backends import reset_bindings, set_backend

    saved = {m: getattr(m._meta, "backend", None) for m in models}
    previous = set_backend(backend)
    try:
        for model in models:
            model._meta.backend = None
        reset_bindings(list(models))
        yield
    finally:
        for model, value in saved.items():
            model._meta.backend = value
        set_backend(previous)
        reset_bindings(list(models))


def _atomic_write(path: Path, text: str, *, private: bool = False) -> None:
    """Write ``path`` through a ``.part`` file and a rename.

    ``private``: the file holds a plaintext copy of the memory (``export/``,
    ``transform/``), so its directory is ``0700`` and the file is created
    ``0600`` -- with that mode from the first byte, never chmod-ed after the
    content is already readable."""
    if private:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    if not private:
        part.write_text(text, encoding="utf-8")
    else:
        with contextlib.suppress(FileNotFoundError):
            part.unlink()
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
    os.replace(part, path)


def _export_model(
    mapping: ModelMapping, orphans: Sequence[str]
) -> tuple[str, dict[str, Any]]:
    """The model's JSONL: ``export_records`` over the class set, then the
    orphan hashes ``SMEMBERS`` cannot see, hydrated the same way."""
    from ..models.query import Query
    from ..transfer.export import (
        _field_state,
        _model_state,
        _record_values,
        export_records,
    )
    from ..transfer.format import dump_line, to_jsonable
    from ..transfer.results import ExportResult

    model = mapping.model
    buffer = io.StringIO()
    result = export_records(model, stream=buffer)
    recovered = 0
    if orphans:
        extra = ExportResult(model=model.__name__)
        for instance in Query.get_many_objects(model, set(orphans)):
            record = {
                "key": instance.db_key.redis_key,
                "values": to_jsonable(_record_values(model, instance)),
                "state": _field_state(model, instance, extra),
                "model_state": _model_state(instance, extra),
            }
            buffer.write(dump_line(record))
            recovered += 1
        result.warnings.extend(extra.warnings)
        result.errors.extend(extra.errors)
    facts = {
        "matched": result.matched_count,
        "exported": result.record_count,
        "vanished": result.vanished,
        "orphans_recovered": recovered,
        "warnings": list(result.warnings),
        "errors": list(result.errors),
    }
    return buffer.getvalue(), facts


# -- transform -----------------------------------------------------------------


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _vector_digest(state: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """An embedding's carried ``.npy`` as ``{dims, sha256}`` of its float32
    values, so a vector compares bit for bit whatever ``.npy`` header wrote
    it."""
    encoded = state.get("vector_npy_b64") if isinstance(state, Mapping) else None
    if not encoded:
        return None
    import numpy as np

    array = np.load(io.BytesIO(base64.b64decode(encoded)), allow_pickle=False)
    values = np.ascontiguousarray(np.asarray(array, dtype=np.float32).ravel())
    return {
        "dims": int(values.size),
        "sha256": hashlib.sha256(values.tobytes()).hexdigest(),
    }


def normalize_record(model: Any, record: Mapping[str, Any]) -> dict[str, Any]:
    """The comparable form of an export record: what a Postgres re-export
    must reproduce. Drops what Postgres does not keep by design (the access
    log, a cycle's declared baseline) and reduces vectors to a float32
    digest."""
    from ..fields.cyclic_decay_field import CyclicDecayField
    from ..fields.embedding_field import EmbeddingField

    values = json.loads(json.dumps(record.get("values") or {}))
    state = json.loads(json.dumps(record.get("state") or {}))
    model_state = json.loads(json.dumps(record.get("model_state") or {}))
    for field_name, field in model._meta.fields.items():
        if isinstance(field, EmbeddingField) and field_name in state:
            state[field_name] = {"vector": _vector_digest(state[field_name])}
        if isinstance(field, CyclicDecayField) and isinstance(
            state.get(field_name), dict
        ):
            cycles = state[field_name].get("cycles")
            if isinstance(cycles, list):
                state[field_name]["cycles"] = [list(c)[:3] for c in cycles]
    access = model_state.get("AccessTrackerMixin")
    if isinstance(access, dict):
        access.pop("access_log", None)
        if not access:
            model_state.pop("AccessTrackerMixin")
    return {"values": values, "state": state, "model_state": model_state}


def payload_sha(model: Any, record: Mapping[str, Any]) -> str:
    return _sha(_canonical(normalize_record(model, record)))


def _content_store(field: Any, content_dir: Optional[Path]) -> Any:
    from ..stores.filesystem import FilesystemStore

    if getattr(field, "_store", None) is not None:
        return field.store
    return FilesystemStore(base_path=str(content_dir) if content_dir else None)


def transform_record(
    mapping: ModelMapping,
    record: dict[str, Any],
    *,
    staged: Mapping[str, Any],
    snapshot_time: float,
    content_dir: Optional[Path],
    lossy: dict[str, int],
    info: dict[str, Any],
) -> dict[str, Any]:
    """One export record -> what lands on Postgres, plus its provenance.

    Returns ``{"key", "record", "meta"}``. ``meta["reject"]`` is set when the
    record cannot land (it is then counted, never written)."""
    from ..fields.co_occurrence_field import CoOccurrenceField
    from ..fields.confidence_field import ConfidenceField
    from ..fields.constants import Defaults
    from ..fields.content_field import ContentField
    from ..fields.cyclic_decay_field import CyclicDecayField
    from ..fields.datetime_field import DatetimeField
    from ..fields.decaying_sorted_field import DecayingSortedField
    from ..fields.embedding_field import EmbeddingField
    from ..transfer.format import from_jsonable

    model = mapping.model
    key = str(record["key"])
    record = json.loads(json.dumps(record))
    values = record.setdefault("values", {}) or {}
    state = record.setdefault("state", {}) or {}
    model_state = record.setdefault("model_state", {}) or {}
    reject: Optional[str] = None

    def count(name: str, n: int = 1) -> None:
        lossy[name] = lossy.get(name, 0) + n

    # ContentField: carry the text inline, resolved from the copied store.
    for field_name, field in model._meta.fields.items():
        if not isinstance(field, ContentField):
            continue
        value = values.get(field_name)
        if isinstance(value, str) and value.startswith("$CF:"):
            try:
                values[field_name] = (
                    _content_store(field, content_dir).load(value).decode("utf-8")
                )
            except (FileNotFoundError, ValueError, UnicodeDecodeError) as exc:
                reject = f"content file for {field_name} unreadable: {exc}"
                count("rejected_content_file_missing")

    # NUL bytes: Postgres text refuses them; the record is rejected, never
    # silently stripped.
    if reject is None and _has_nul(values):
        reject = "a text value contains a NUL byte (Postgres text refuses NUL)"
        count("rejected_nul_bytes")
    for field_name, pattern in mapping.id_patterns.items():
        value = values.get(field_name)
        if reject is None and not (
            isinstance(value, str) and re.fullmatch(pattern, value)
        ):
            reject = f"{field_name}={value!r} does not match {pattern}"
            count("rejected_id_pattern")

    # Embeddings: wrong dimension -> dropped, re-embedded by the backfill.
    for field_name, field in model._meta.fields.items():
        if not isinstance(field, EmbeddingField):
            continue
        declared = values.get(field_name)
        digest = _vector_digest(state.get(field_name) or {})
        try:
            want = (
                int(field.provider.dimensions) if field.provider is not None else None
            )
        except Exception:
            want = None
        if digest is None:
            if declared:
                count("embedding_file_missing_reembed")
            continue
        if want is not None and digest["dims"] != want:
            state.pop(field_name, None)
            count("embedding_dimension_mismatch_reembed")

    # ConfidenceField: a record with no companion-hash entry (never saved
    # through popoto, or the entry was lost) exports no state. On Redis it
    # reads as the field's seed; on Postgres it lands with that seed too
    # (initial confidence, no evidence). Counted, as information, and the
    # seed is written into the carried state so the verified payload is
    # what lands -- the same normalization the co-occurrence edges get.
    for field_name, field in model._meta.fields.items():
        if isinstance(field, ConfidenceField) and field_name not in state:
            count("confidence_companion_missing")
            state[field_name] = {
                "confidence": float(field.initial_confidence),
                "evidence_count": 0,
                "corroborations": 0,
                "contradictions": 0,
            }

    # Long-tail state Postgres does not keep.
    for field_name, field in model._meta.fields.items():
        carried = state.get(field_name)
        if isinstance(field, CyclicDecayField) and isinstance(carried, dict):
            for cycle in carried.get("cycles") or []:
                if isinstance(cycle, list) and len(cycle) >= 4 and cycle[3] is not None:
                    count("cycle_baselines_dropped")
        if isinstance(field, CoOccurrenceField) and isinstance(carried, dict):
            # The carried state is {"edges": {target: weight}, "max_edges"}.
            # Apply the destination's import normalization here, exactly as
            # CoOccurrenceField.import_state does, so the counted loss is
            # the real one and the verified payload is what lands.
            edges = carried.get("edges")
            if isinstance(edges, dict) and edges:
                cap = Defaults.CO_OCCURRENCE_WEIGHT_CAP
                clamped = sum(1 for w in edges.values() if float(w) > cap)
                ranked = sorted(
                    ((str(t), min(float(w), cap)) for t, w in edges.items()),
                    key=lambda pair: pair[1],
                    reverse=True,
                )
                keep = ranked[: max(1, int(field.max_edges))]
                if clamped:
                    count("co_occurrence_weights_clamped", clamped)
                if len(ranked) > len(keep):
                    count("co_occurrence_edges_truncated", len(ranked) - len(keep))
                if clamped or len(ranked) > len(keep):
                    carried["edges"] = dict(keep)
                    carried["max_edges"] = int(field.max_edges)
    access = model_state.get("AccessTrackerMixin")
    access_log: list[float] = []
    if isinstance(access, dict) and access.get("access_log"):
        access_log = [
            s for s in (_as_epoch(_maybe_float(x)) for x in access["access_log"]) if s
        ]
        count("access_log_entries_dropped", len(access["access_log"]))

    for field_name, values_set in mapping.sentinel_values.items():
        value = values.get(field_name)
        if value in values_set:
            sentinels = info.setdefault("sentinels", {})
            sentinels[str(value)] = sentinels.get(str(value), 0) + 1

    # Timestamp evidence.
    decoded = from_jsonable(values)
    clocks: list[float] = []
    auto_now: list[float] = []
    for field_name, field in model._meta.fields.items():
        if isinstance(field, DecayingSortedField):
            stamp = _as_epoch(decoded.get(field_name))
            if stamp is not None:
                clocks.append(stamp)
        elif isinstance(field, DatetimeField) and getattr(field, "auto_now", False):
            stamp = _as_epoch(decoded.get(field_name))
            if stamp is not None:
                auto_now.append(stamp)
    created_evidence = list(clocks) + list(auto_now) + access_log
    for path in mapping.created_at_paths:
        created_evidence += _timestamps_at(decoded, path)
    updated_evidence = list(clocks) + list(auto_now)
    for path in mapping.updated_at_paths:
        updated_evidence += _timestamps_at(decoded, path)
    stage = staged.get(key) or {}
    if stage.get("latest"):
        created_evidence.append(float(stage["latest"]))
    estimated: list[str] = ["_created_at", "_updated_at"]
    created_at = min(created_evidence) if created_evidence else snapshot_time
    updated_at = max(updated_evidence) if updated_evidence else snapshot_time
    if not created_evidence:
        count("created_at_from_snapshot_time")
    if not updated_evidence:
        count("updated_at_from_snapshot_time")
    count("created_at_estimated")
    count("updated_at_estimated")
    if updated_at < created_at:
        updated_at = created_at

    meta: dict[str, Any] = {
        "payload_sha": payload_sha(model, record),
        "created_at": created_at,
        "updated_at": updated_at,
        "estimated_fields": estimated,
        "staged_reads": int(stage.get("count") or 0),
        "staged_at": stage.get("latest"),
        "reject": reject,
    }
    if meta["staged_reads"]:
        count("staged_reads_carried", meta["staged_reads"])
    return {"key": key, "record": record, "meta": meta}


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, Mapping):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_nul(v) for v in value)
    return False


def _maybe_float(value: Any) -> Any:
    if isinstance(value, (bytes, str)) and _is_number(value):
        return float(value)
    return value


# -- Postgres side ---------------------------------------------------------------

RUN_TABLE = "popoto_migration_run"
LEDGER_TABLE = "popoto_migration_ledger"

#: Ledger decisions.
INSERTED = "inserted"
RESUMED = "resumed"
UPDATED_DELTA = "updated_delta"
WON_MERGE = "won_merge"
LOST_MERGE = "lost_merge"
DEDUPLICATED = "deduplicated"
UNCHANGED = "unchanged"
CONFLICT_NATIVE = "conflict_native"
REJECTED = "rejected"
LOAD_ERROR = "load_error"

_WRITES = (INSERTED, RESUMED, UPDATED_DELTA, WON_MERGE)


def _qi(name: str) -> str:
    from ..backends.postgres.schema import quote_ident

    return str(quote_ident(name))


def _connect(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn, autocommit=False)


def _ensure_tool_tables(conn: Any, schema: str) -> None:
    s = _qi(schema)
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {s}.{_qi(RUN_TABLE)} ("
        "run_id text PRIMARY KEY, source_id text NOT NULL, rdb_sha256 text NOT NULL, "
        "started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz, "
        "status text NOT NULL, progress jsonb NOT NULL DEFAULT '{}'::jsonb, "
        "report jsonb)"
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {s}.{_qi(LEDGER_TABLE)} ("
        "run_id text NOT NULL, model text NOT NULL, _pk text NOT NULL, "
        "state text NOT NULL, decision text NOT NULL, payload_sha text NOT NULL, "
        "detail text, at timestamptz NOT NULL DEFAULT now(), "
        "PRIMARY KEY (run_id, model, _pk))"
    )
    # When this run first queued the key's write (kept across a resume of
    # the same run): the start of the window the stream check counts the
    # key's save events in.
    conn.execute(
        f"ALTER TABLE {s}.{_qi(LEDGER_TABLE)} "
        "ADD COLUMN IF NOT EXISTS write_started_at timestamptz"
    )
    # What a write decision put in the columns the tool writes itself
    # (``_updated_at``, ``_created_at``, ``_estimated_fields``, staged
    # reads): the record a later run's verification checks a row this run
    # won from it against (:func:`_superseded_by_merge`), because the row's
    # own ``_migrated_from`` cannot vouch for itself.
    conn.execute(
        f"ALTER TABLE {s}.{_qi(LEDGER_TABLE)} ADD COLUMN IF NOT EXISTS wrote jsonb"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {_qi(LEDGER_TABLE + '__pending')} ON "
        f"{s}.{_qi(LEDGER_TABLE)} (model, _pk) WHERE state = 'pending'"
    )
    conn.commit()


def _table_exists(conn: Any, schema: str, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname = %s AND tablename = %s",
        (schema, table),
    ).fetchone()
    return row is not None


def _table_name(model: Any) -> str:
    from ..backends.postgres.schema import table_name_for

    return str(table_name_for(model._meta.model_name))


_LOCK_CLASS = 756
"""First key of the per-schema advisory lock (the issue number); the second
is ``hashtext`` of the schema name."""


def _lock_target_schema(dsn: str, schema: str) -> Any:
    """Hold a session advisory lock on the target schema for the whole run.

    A second run into the same schema -- another machine's store, or the
    same command started twice -- is refused here, before preflight, instead
    of racing the first through ``CREATE SCHEMA`` and the merge decisions.
    The lock lives as long as the returned connection; closing it (or the
    process dying) releases it."""
    conn = _connect_autocommit(dsn)
    try:
        held = conn.execute(
            "SELECT pg_try_advisory_lock(%s, hashtext(%s))", (_LOCK_CLASS, schema)
        ).fetchone()[0]
    except BaseException:
        conn.close()
        raise
    if not held:
        conn.close()
        raise MigrationRefused(
            f"another migration run holds the lock on schema {schema!r}; run the "
            "stores one after another (each later one with --merge)"
        )
    return conn


def _connect_autocommit(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def preflight_target(
    dsn: str,
    schema: str,
    mappings: Sequence[ModelMapping],
    *,
    run_id: str,
    merge: bool,
    resume: bool,
) -> dict[str, Any]:
    """Refuse a target schema that already holds rows for a migrated model,
    unless ``merge`` is set, or the rows are this run's own (a resume)."""
    facts: dict[str, Any] = {}
    conn = _connect(dsn)
    try:
        ledger = _table_exists(conn, schema, LEDGER_TABLE)
        for mapping in mappings:
            table = _table_name(mapping.model)
            if not _table_exists(conn, schema, table):
                facts[mapping.name] = {"rows": 0}
                continue
            q = f"{_qi(schema)}.{_qi(table)}"
            total = conn.execute(f"SELECT count(*) FROM {q}").fetchone()[0]
            ours = conn.execute(
                f"SELECT count(*) FROM {q} WHERE _migrated_from->>'run_id' = %s",
                (run_id,),
            ).fetchone()[0]
            pending = 0
            if ledger:
                pending = conn.execute(
                    f"SELECT count(*) FROM {q} t JOIN {_qi(schema)}.{_qi(LEDGER_TABLE)} l "
                    "ON l._pk = t._pk AND l.model = %s AND l.run_id = %s "
                    "AND l.state = 'pending' WHERE t._migrated_from IS NULL",
                    (mapping.name, run_id),
                ).fetchone()[0]
            facts[mapping.name] = {"rows": total, "this_run": ours + pending}
            if total and not merge and not (resume and total == ours + pending):
                if resume:
                    # #794: the operator already passed --resume; naming it
                    # again sent them round in a circle.
                    raise TargetNotEmpty(
                        f"{schema}.{table} holds {total} row(s) for "
                        f"{mapping.name}, of which only {ours + pending} are "
                        "this run's. The others are rows this run did not "
                        "write: another store's rows, which were already there "
                        "if this run began with --merge, or rows written since "
                        "-- popoto saving a row natively (which clears its "
                        "_migrated_from), or another store's --merge. A plain "
                        "--resume continues only into rows that are all its "
                        "own; pass --resume --merge with the same --run-dir to "
                        "continue under the merge rule, which never overwrites "
                        "a natively saved row and decides every other key per "
                        "key."
                    )
                raise TargetNotEmpty(
                    f"{schema}.{table} already holds {total} row(s) for "
                    f"{mapping.name}. Pass --merge to merge this store into them "
                    "(the merge rule decides per key), or --resume with the same "
                    "--run-dir to continue an interrupted run (--resume --merge "
                    "if anything else has written to the table since)."
                )
    finally:
        conn.close()
    return facts


@contextlib.contextmanager
def _no_embedding(models: Sequence[Any]) -> Iterator[None]:
    """Switch off ``auto_embed`` while records land: the vector is the
    carried one (``import_state``), and a save must never call the provider
    (Valor's is a paid network API)."""
    from ..fields.embedding_field import EmbeddingField

    saved: list[tuple[Any, bool]] = []
    for model in models:
        for field in model._meta.fields.values():
            if isinstance(field, EmbeddingField):
                saved.append((field, field.auto_embed))
                field.auto_embed = False
    try:
        yield
    finally:
        for field, value in saved:
            field.auto_embed = value


def _decide(
    meta: Mapping[str, Any],
    existing: Optional[Mapping[str, Any]],
    row_exists: bool,
    pending: bool,
    source_id: str,
) -> str:
    """The merge rule for one key (module docstring)."""
    if not row_exists:
        return INSERTED
    if existing is None:
        return RESUMED if pending else CONFLICT_NATIVE
    if existing.get("payload_sha") == meta["payload_sha"]:
        return UNCHANGED if existing.get("source") == source_id else DEDUPLICATED
    if existing.get("source") == source_id:
        return UPDATED_DELTA
    theirs = (
        float(existing.get("updated_at") or 0.0),
        str(existing.get("source") or ""),
    )
    ours = (float(meta["updated_at"]), source_id)
    return WON_MERGE if ours > theirs else LOST_MERGE


def _record_once(
    entries: Any, entry: Mapping[str, Any], identity: Sequence[str]
) -> list[dict[str, Any]]:
    """``entries`` with ``entry`` recorded once per source.

    An entry already there with the same identity (source and snapshot, and
    for a loser the payload) is kept as it is, so re-running the same
    snapshot leaves ``_migrated_from`` byte for byte unchanged; an older
    entry of the same source (a superseded snapshot) is replaced."""
    current = [dict(e) for e in (entries or []) if isinstance(e, Mapping)]
    for existing in current:
        if all(existing.get(k) == entry.get(k) for k in identity):
            return current
    return [e for e in current if e.get("source") != entry.get("source")] + [
        dict(entry)
    ]


def _provenance(
    meta: Mapping[str, Any],
    run: Mapping[str, Any],
    previous: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    # This source is the winner now: it is no longer one of the losers.
    losers = [
        dict(e)
        for e in ((previous or {}).get("losers") or [])
        if isinstance(e, Mapping) and e.get("source") != run["source_id"]
    ]
    if previous is not None and previous.get("source") not in (None, run["source_id"]):
        losers = _record_once(
            losers,
            {
                k: previous.get(k)
                for k in ("source", "run_id", "snapshot", "payload_sha", "updated_at")
            },
            ("source", "snapshot", "payload_sha"),
        )
    return {
        "source": run["source_id"],
        "run_id": run["run_id"],
        "snapshot": run["rdb_sha256"],
        "payload_sha": meta["payload_sha"],
        "updated_at": meta["updated_at"],
        "migrated_at": run["now"],
        "duplicates": (
            list((previous or {}).get("duplicates") or [])
            if previous is not None
            and previous.get("payload_sha") == meta["payload_sha"]
            else []
        ),
        "losers": losers,
    }


def _wrote(meta: Mapping[str, Any]) -> dict[str, Any]:
    """The tool-written column values of a write, as its ``done`` ledger row
    records them (``wrote``)."""
    return {
        "updated_at": meta["updated_at"],
        "created_at": meta["created_at"],
        "estimated_fields": list(meta["estimated_fields"]),
        "staged_reads": meta["staged_reads"],
        "staged_at": meta["staged_at"],
    }


def _adopted_keys(
    plan: Sequence[tuple[Mapping[str, Any], str]], pending_sha: Mapping[str, str]
) -> set[str]:
    """The ``RESUMED`` keys whose row is NOT saved again.

    An adopted row already holds exactly what this run imported:
    :func:`_native_since_crash` checked its re-export against the ledger's
    ``payload_sha``, and here that is also this record's payload. Its save
    committed together with everything the save derives -- the
    ``EventStreamMixin`` entry and its ``pg_notify``, BM25 postings, vector
    rows, membership tokens -- and its carried state was restored after it.
    Saving it again would append a second stream event and fire every other
    per-save side effect twice (#756 review: 1600 events for 1500 records),
    so only its provenance is written; every derived table stays exactly
    what a clean load leaves. A pending row whose payload differs from this
    record (the transform changed under the same snapshot) is imported
    again, like any other write."""
    return {
        str(row["key"])
        for row, decision in plan
        if decision == RESUMED
        and pending_sha.get(str(row["key"])) == row["meta"]["payload_sha"]
    }


def _native_since_crash(
    mapping: ModelMapping, conn: Any, table: str, ledger: str, run: Mapping[str, Any]
) -> set[str]:
    """Keys this run left ``pending`` whose row popoto has saved since.

    A resume adopts its own half-written rows -- but only while each still
    holds exactly what this run imported (its re-export hashes to the
    ledger's ``payload_sha``). A row saved natively after the crash no
    longer does, and is then treated as native: never overwritten."""
    rows = conn.execute(
        f"SELECT l._pk, l.payload_sha FROM {ledger} l JOIN {table} t "
        "ON t._pk = l._pk WHERE l.run_id = %s AND l.model = %s "
        "AND l.state = 'pending' AND t._migrated_from IS NULL",
        (run["run_id"], mapping.name),
    ).fetchall()
    conn.commit()
    if not rows:
        return set()
    exported = _export_index(mapping.model)
    return {
        pk
        for pk, sha in rows
        if pk not in exported or payload_sha(mapping.model, exported[pk]) != sha
    }


_crash_hook: Optional[Callable[[str, int], None]] = None
"""Test seam: called with ``(model, batch number)`` after a batch's records
landed and before its provenance is written -- inside the batch's
transaction, so a crash there leaves nothing of the batch."""

_step_hook: Optional[Callable[[str, int, str], None]] = None
"""Test seam: called with ``(model, batch number, step)`` between the steps
of a batch's transaction: ``locked``, ``ledger_pending``, ``imported``,
``provenance`` and ``ledger_done`` (the last just before ``COMMIT``)."""


def _step(model: str, number: int, step: str) -> None:
    if _step_hook is not None:
        _step_hook(model, number, step)


def _batch_unit(backend: Any) -> Any:
    """The unit of work one batch runs in: ``backend.transaction()``. A
    named seam so the revert check can swap in per-statement commits (the
    pre-atomic behaviour) and show the verification catches what that
    leaves behind."""
    return backend.transaction()


def _load_model(
    mapping: ModelMapping,
    rows: Sequence[dict[str, Any]],
    manifest: Mapping[str, Any],
    *,
    backend: Any,
    conn: Any,
    run: dict[str, Any],
    batch_size: int,
    decisions: dict[str, str],
    errors: list[str],
) -> None:
    """Load ``rows`` batch by batch, each batch ONE Postgres transaction.

    A batch's record saves, every carried-state writer (confidence, access
    counters, prediction ledger, edges, cycles, validity, vector), its
    ``_migrated_from`` provenance, its ledger rows and the resume marker all
    commit together (:func:`_load_batch`). A crash -- an exception, or
    ``kill -9`` at any instant -- therefore leaves either none of the batch
    or all of it, never a saved row whose carried state or provenance is
    missing (#756 review: such a row could not be told from a native write
    on resume, was reported ``conflict_native`` and kept its defaults under
    a CLEAN verdict). A deadlock or serialization failure rolls the batch
    back and runs it again, up to ``Defaults.PG_TRANSACTION_RETRIES`` times."""
    from ..backends.types import BackendRetryableError
    from ..fields.constants import Defaults

    model = mapping.model
    schema = backend.schema
    ts = backend._table(model._meta.spec, write=True)
    table = ts.qualified
    ledger = f"{_qi(schema)}.{_qi(LEDGER_TABLE)}"
    runs = f"{_qi(schema)}.{_qi(RUN_TABLE)}"

    progress = conn.execute(
        f"SELECT progress->>%s FROM {runs} WHERE run_id = %s",
        (mapping.name, run["run_id"]),
    ).fetchone()
    marker = progress[0] if progress and progress[0] else None
    conn.commit()

    todo = [r for r in rows if marker is None or r["key"] > marker]
    if marker is not None:
        for pk, decision in conn.execute(
            f"SELECT _pk, decision FROM {ledger} WHERE run_id = %s AND model = %s "
            "AND state = 'done' AND _pk COLLATE \"C\" <= %s",
            (run["run_id"], mapping.name, marker),
        ).fetchall():
            decisions[pk] = decision
        conn.commit()
    native_since_crash = _native_since_crash(mapping, conn, table, ledger, run)

    attempts = int(Defaults.PG_TRANSACTION_RETRIES) + 1
    for number, start in enumerate(range(0, len(todo), max(1, batch_size))):
        batch = todo[start : start + batch_size]
        for attempt in range(1, attempts + 1):
            try:
                batch_decisions, batch_errors = _load_batch(
                    mapping,
                    batch,
                    number,
                    manifest,
                    backend=backend,
                    ts=ts,
                    run=run,
                    native_since_crash=native_since_crash,
                )
            except BackendRetryableError:
                if attempt == attempts:
                    raise
                time.sleep(0.05 * attempt)
                continue
            break
        decisions.update(batch_decisions)
        errors.extend(batch_errors)


def _load_batch(
    mapping: ModelMapping,
    batch: Sequence[dict[str, Any]],
    number: int,
    manifest: Mapping[str, Any],
    *,
    backend: Any,
    ts: Any,
    run: Mapping[str, Any],
    native_since_crash: set[str],
) -> tuple[dict[str, str], list[str]]:
    """One batch, in one ``backend.transaction()``; returns its decisions
    and load errors once it has committed.

    Lock order (plan §6, TD-2, the backend's one order): the batch first
    takes every ``(model, field)`` validity lock the model has, then the
    record-key advisory locks of ALL its keys in ``_pk`` byte order, in two
    statements, before it reads or writes anything. Every later lock a save
    or a carried-state writer asks for is one of those (re-taking a held
    advisory lock is a no-op), and the stream rows are locked last, at
    ``COMMIT`` (``defer_stream_append``). Holding the keys before the reads
    also means a native save of a key cannot slip between this batch's merge
    decision and its write."""
    from ..fields.validity_field import ValidityField
    from ..transfer.format import dump_line
    from ..transfer.import_ import import_records

    model = mapping.model
    schema = backend.schema
    table = ts.qualified
    ledger = f"{_qi(schema)}.{_qi(LEDGER_TABLE)}"
    runs = f"{_qi(schema)}.{_qi(RUN_TABLE)}"
    layout = ts.search
    embedding_layouts = list(layout.embedding.values()) if layout is not None else []
    has_access = "AccessTrackerMixin" in {k.__name__ for k in model.__mro__}
    validity = sorted(
        name
        for name, field in model._meta.fields.items()
        if isinstance(field, ValidityField)
    )
    keys = [r["key"] for r in batch]
    decisions: dict[str, str] = {}
    errors: list[str] = []

    with _batch_unit(backend) as uow:
        tx = uow.conn
        if validity:
            backend._validity_lock(ts, validity, uow)
        lock_sql, lock_params = backend._record_locked(ts, keys, "SELECT 1", [])
        backend._run(lock_sql, lock_params, uow=uow, write=True)
        _step(mapping.name, number, "locked")

        found = tx.execute(
            f'SELECT "_pk", "_migrated_from" FROM {table} WHERE "_pk" = ANY(%s)',
            (keys,),
        ).fetchall()
        existing = {pk: mf for pk, mf in found}
        # Only THIS run's pending rows are adopted. A row another run left
        # pending (it crashed between its import and its provenance, before
        # batches were atomic) is indistinguishable from a native row -- and
        # may have become one, if popoto saved it since -- so it is treated
        # as native: never overwritten, reported as conflict_native.
        pending: dict[str, Optional[dict[str, Any]]] = {}
        pending_sha: dict[str, str] = {}
        for pk, detail, sha in tx.execute(
            f"SELECT _pk, detail, payload_sha FROM {ledger} WHERE run_id = %s "
            "AND model = %s AND _pk = ANY(%s) AND state = 'pending'",
            (run["run_id"], mapping.name, keys),
        ).fetchall():
            if pk not in native_since_crash:
                pending[pk] = json.loads(detail) if detail else None
                pending_sha[pk] = sha

        def previous_of(key: str) -> Optional[dict[str, Any]]:
            if key in existing and existing[key] is not None:
                return dict(existing[key])
            return pending.get(key)

        plan: list[tuple[dict[str, Any], str]] = []
        for row in batch:
            key = row["key"]
            if row["meta"]["reject"]:
                plan.append((row, REJECTED))
                continue
            plan.append(
                (
                    row,
                    _decide(
                        row["meta"],
                        existing.get(key),
                        key in existing,
                        key in pending,
                        run["source_id"],
                    ),
                )
            )

        adopted = _adopted_keys(plan, pending_sha)
        writes = [row for row, d in plan if d in _WRITES and row["key"] not in adopted]
        if writes:
            tx.cursor().executemany(
                f"INSERT INTO {ledger} AS l (run_id, model, _pk, state, decision, "
                "payload_sha, detail, write_started_at) VALUES (%s, %s, %s, 'pending', "
                "'write', %s, %s, now()) ON CONFLICT (run_id, model, _pk) DO UPDATE SET "
                "state = 'pending', decision = 'write', payload_sha = "
                "EXCLUDED.payload_sha, detail = EXCLUDED.detail, at = now(), "
                # A key this run left pending before a crash (a run that
                # predates atomic batches) keeps its window start: the
                # crashed import's event belongs to it.
                "write_started_at = CASE WHEN l.state = 'pending' THEN "
                "coalesce(l.write_started_at, now()) ELSE now() END",
                [
                    (
                        run["run_id"],
                        mapping.name,
                        r["key"],
                        r["meta"]["payload_sha"],
                        # The provenance this write replaces: the save nulls
                        # the row's _migrated_from, and the provenance step
                        # below reads it back from here.
                        json.dumps(previous_of(r["key"])),
                    )
                    for r in writes
                ],
            )
        _step(mapping.name, number, "ledger_pending")
        if writes:
            stream = io.StringIO()
            stream.write(dump_line(dict(manifest)))
            for r in writes:
                stream.write(dump_line(r["record"]))
            stream.seek(0)
            report = import_records(
                model,
                stream,
                on_conflict="overwrite",
                on_write_gate="bypass",
                on_embedding_mismatch="carry",
                uow=uow,
            )
            failed = {o.key: o for o in report.outcomes if o.category != "landed"}
            if failed:
                for key, outcome in failed.items():
                    errors.append(
                        f"{mapping.name} {key}: {outcome.category}: {outcome.reason}"
                    )
                plan = [
                    (row, LOAD_ERROR if row["key"] in failed and d in _WRITES else d)
                    for row, d in plan
                ]

        if _crash_hook is not None:
            _crash_hook(mapping.name, number)
        _step(mapping.name, number, "imported")

        for row, decision in plan:
            key, meta = row["key"], row["meta"]
            previous = previous_of(key)
            if decision in _WRITES:
                provenance = _provenance(meta, run, previous)
                sets = [
                    '"_migrated_from" = %s::jsonb',
                    '"_estimated_fields" = %s',
                    '"_created_at" = to_timestamp(%s)',
                    '"_updated_at" = to_timestamp(%s)',
                ]
                params: list[Any] = [
                    json.dumps(provenance),
                    list(meta["estimated_fields"]),
                    meta["created_at"],
                    meta["updated_at"],
                ]
                if has_access:
                    sets += ['"_staged_reads" = %s', '"_staged_at" = %s']
                    params += [meta["staged_reads"] or None, meta["staged_at"]]
                for emb in embedding_layouts:
                    if emb.source:
                        sets.append(
                            f"{_qi(emb.hash)} = CASE WHEN {_qi(emb.vec)} IS NULL THEN "
                            f"{_qi(emb.hash)} ELSE md5({_qi(emb.source)}::text) END"
                        )
                updated = tx.execute(
                    f'UPDATE {table} SET {", ".join(sets)} WHERE "_pk" = %s',
                    params + [key],
                ).rowcount
                if updated != 1:
                    # The save ran in this transaction: its row must be here.
                    raise MigrationError(
                        f"{mapping.name} {key}: the row this batch saved is not "
                        "in its own transaction; the batch is rolled back"
                    )
            elif decision in (DEDUPLICATED, LOST_MERGE) and previous is not None:
                merged = dict(previous)
                if decision == DEDUPLICATED:
                    merged["duplicates"] = _record_once(
                        merged.get("duplicates"),
                        {
                            "source": run["source_id"],
                            "run_id": run["run_id"],
                            "snapshot": run["rdb_sha256"],
                        },
                        ("source", "snapshot"),
                    )
                else:
                    merged["losers"] = _record_once(
                        merged.get("losers"),
                        {
                            "source": run["source_id"],
                            "run_id": run["run_id"],
                            "snapshot": run["rdb_sha256"],
                            "payload_sha": meta["payload_sha"],
                            "updated_at": meta["updated_at"],
                        },
                        ("source", "snapshot", "payload_sha"),
                    )
                if merged != previous:
                    tx.execute(
                        f'UPDATE {table} SET "_migrated_from" = %s::jsonb '
                        'WHERE "_pk" = %s AND "_migrated_from" IS NOT NULL',
                        (json.dumps(merged), key),
                    )
            decisions[key] = decision
        _step(mapping.name, number, "provenance")

        tx.cursor().executemany(
            f"INSERT INTO {ledger} (run_id, model, _pk, state, decision, payload_sha, "
            "detail, wrote) VALUES (%s, %s, %s, 'done', %s, %s, %s, %s::jsonb) "
            "ON CONFLICT (run_id, model, _pk) DO UPDATE SET state = 'done', "
            "decision = EXCLUDED.decision, payload_sha = EXCLUDED.payload_sha, "
            "detail = EXCLUDED.detail, wrote = EXCLUDED.wrote, at = now()",
            [
                (
                    run["run_id"],
                    mapping.name,
                    r["key"],
                    d,
                    r["meta"]["payload_sha"],
                    r["meta"]["reject"],
                    json.dumps(_wrote(r["meta"])) if d in _WRITES else None,
                )
                for r, d in plan
            ],
        )
        # Rows another run left pending are settled only where this run has
        # actually written the key since (it could not have adopted them).
        landed = [row["key"] for row, d in plan if d in _WRITES]
        if landed:
            tx.execute(
                f"UPDATE {ledger} SET state = 'superseded' WHERE model = %s AND "
                "_pk = ANY(%s) AND state = 'pending' AND run_id <> %s",
                (mapping.name, landed, run["run_id"]),
            )
        tx.execute(
            f"UPDATE {runs} SET progress = jsonb_set(progress, ARRAY[%s], to_jsonb(%s::text)) "
            "WHERE run_id = %s",
            (mapping.name, keys[-1], run["run_id"]),
        )
        _step(mapping.name, number, "ledger_done")
    return decisions, errors


# -- verification ----------------------------------------------------------------


def _export_index(model: Any) -> dict[str, dict[str, Any]]:
    from ..transfer.export import export_records

    text = export_records(model).data or ""
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    return {str(r["key"]): r for r in lines[1:]}


def _diff_parts(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    parts: list[str] = []
    for section in ("values", "state", "model_state"):
        left, right = a.get(section) or {}, b.get(section) or {}
        for name in sorted(set(left) | set(right)):
            if left.get(name) != right.get(name):
                parts.append(f"{section}.{name}")
    return parts


def _partitions(
    mapping: ModelMapping, field: Any, rows: Sequence[Mapping[str, Any]], limit: int
) -> list[dict[str, Any]]:
    from ..transfer.format import from_jsonable

    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        values = from_jsonable(row["record"].get("values") or {})
        part = {pf: values.get(pf) for pf in field.partition_by}
        seen.setdefault(_canonical(json.loads(json.dumps(part, default=str))), part)
    return [seen[k] for k in sorted(seen)][: max(1, limit)]


def _decay_order(
    model: Any, field_name: str, partition: Mapping[str, Any], n: int
) -> list[str]:
    # no_track(): a verification read must not stage access-tracker reads,
    # on the throwaway or (worse) on the migrated rows.
    builder = model.query.filter(**dict(partition)).no_track()
    return [str(o.db_key.redis_key) for o in builder.top_by_decay(field_name, n=n)]


def _sample_queries(
    mapping: ModelMapping, source: str, rows: Sequence[Mapping[str, Any]], limit: int
) -> list[str]:
    from ..transfer.format import from_jsonable

    queries: list[str] = []
    for row in sorted(rows, key=lambda r: r["meta"]["payload_sha"]):
        text = from_jsonable(row["record"].get("values") or {}).get(source)
        if isinstance(text, str):
            words = [w for w in re.findall(r"[A-Za-z]{4,}", text)][:3]
            if words:
                queries.append(" ".join(words))
        if len(queries) >= limit:
            break
    return queries


def _verify_tool_columns(
    conn: Any,
    ts: Any,
    by_key: Mapping[str, Mapping[str, Any]],
    provenance: Mapping[str, Any],
    run: Mapping[str, Any],
) -> tuple[int, dict[str, list[str]]]:
    """The columns the tool writes itself, outside popoto's save path:
    ``_created_at``, ``_updated_at``, ``_estimated_fields`` and each
    embedding's ``<f>__hash`` (``md5`` of its source text wherever a vector
    landed). Compared for every row THIS run wrote; a row another run wrote
    carries that run's estimates. Returns ``(compared, {key: parts})``."""
    mine = sorted(
        k
        for k, row in by_key.items()
        if not row["meta"]["reject"]
        and (provenance.get(k) or {}).get("run_id") == run["run_id"]
    )
    if not mine:
        return 0, {}
    bad: dict[str, list[str]] = {}
    for pk, values in _read_tool_columns(conn, ts, mine).items():
        parts = _tool_column_parts(values, by_key[pk]["meta"])
        if parts:
            bad[pk] = parts
    return len(mine), bad


def _read_tool_columns(
    conn: Any, ts: Any, keys: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """``{key: values}`` of the columns the tool writes itself, for ``keys``:
    ``created_at`` and ``updated_at`` (epoch seconds), ``estimated_fields``,
    and ``bad_hashes``, the embedding ``<f>__hash`` columns that do not hold
    ``md5`` of their source text although a vector landed."""
    layout = ts.search
    embeddings = [
        e for e in (layout.embedding.values() if layout is not None else []) if e.source
    ]
    columns = [
        'extract(epoch FROM "_created_at")::float8',
        'extract(epoch FROM "_updated_at")::float8',
        '"_estimated_fields"',
    ] + [
        f"({_qi(e.vec)} IS NOT NULL AND {_qi(e.hash)} IS DISTINCT FROM "
        f"md5({_qi(e.source)}::text))"
        for e in embeddings
    ]
    out: dict[str, dict[str, Any]] = {}
    for pk, created, updated, estimated, *hash_bad in conn.execute(
        f'SELECT "_pk", {", ".join(columns)} FROM {ts.qualified} WHERE "_pk" = ANY(%s)',
        (list(keys),),
    ).fetchall():
        out[pk] = {
            "created_at": created,
            "updated_at": updated,
            "estimated_fields": list(estimated or []),
            "bad_hashes": [e.hash for e, wrong in zip(embeddings, hash_bad) if wrong],
        }
    conn.commit()
    return out


def _close(have: Any, want: Any, tolerance: float = 1e-3) -> bool:
    try:
        return abs(float(have) - float(want)) <= tolerance
    except (TypeError, ValueError):
        return False


def _tool_column_parts(values: Mapping[str, Any], want: Mapping[str, Any]) -> list[str]:
    """The tool-written columns in ``values`` (:func:`_read_tool_columns`)
    that differ from ``want`` (a record's ``meta``, or a ledger's ``wrote``)."""
    parts: list[str] = []
    if not _close(values["created_at"], want.get("created_at")):
        parts.append("_created_at")
    if not _close(values["updated_at"], want.get("updated_at")):
        parts.append("_updated_at")
    if list(values["estimated_fields"]) != list(want.get("estimated_fields") or []):
        parts.append("_estimated_fields")
    return parts + list(values["bad_hashes"])


def _staged_differs(staged: Any, want: Mapping[str, Any]) -> bool:
    """Whether a row's ``(_staged_reads, _staged_at)`` differ from what was
    written (a record's ``meta``, or a ledger's ``wrote``)."""
    n, at = staged if staged is not None else (None, None)
    if int(n or 0) != int(want.get("staged_reads") or 0):
        return True
    return want.get("staged_at") is not None and not _close(
        at or 0, want["staged_at"], 1e-6
    )


_CARRIED_SECTIONS = ("state", "model_state")

WINNER_DOES_NOT_BEAT = "<winner does not beat this run>"
NOT_WINNERS_PAYLOAD = "<not the winner's payload>"
NOT_WINNERS_STAGED_READS = "<not the winner's staged reads>"


def _winner_evidence(
    conn: Any,
    schema: str,
    model_name: str,
    claims: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, tuple[str, str]], dict[tuple[str, str], tuple[Any, ...]]]:
    """What the tool's own tables say about the winners ``claims`` name:
    ``({run_id: (source_id, rdb_sha256)}, {(run_id, key): (state, decision,
    payload_sha, wrote)})``. Neither depends on the ``_migrated_from`` under
    test."""
    run_ids = sorted({str(p.get("run_id")) for p in claims.values()})
    s = _qi(schema)
    runs = {
        rid: (src, sha)
        for rid, src, sha in conn.execute(
            f"SELECT run_id, source_id, rdb_sha256 FROM {s}.{_qi(RUN_TABLE)} "
            "WHERE run_id = ANY(%s)",
            (run_ids,),
        ).fetchall()
    }
    ledger = {
        (rid, pk): (state, decision, sha, wrote)
        for rid, pk, state, decision, sha, wrote in conn.execute(
            f"SELECT run_id, _pk, state, decision, payload_sha, wrote FROM "
            f"{s}.{_qi(LEDGER_TABLE)} WHERE model = %s AND run_id = ANY(%s) "
            "AND _pk = ANY(%s)",
            (model_name, run_ids, sorted(claims)),
        ).fetchall()
    }
    conn.commit()
    return runs, ledger


def _unconfirmed_reasons(
    prov: Mapping[str, Any],
    run_row: Optional[tuple[str, str]],
    ledger_row: Optional[tuple[Any, ...]],
    row_updated_at: Any,
) -> list[str]:
    """Why the winner a row's ``_migrated_from`` claims is not backed by
    the claimed run's own records (empty when it is)."""
    if run_row is None:
        return ["<winner's run is not in popoto_migration_run>"]
    reasons: list[str] = []
    source_id, rdb_sha256 = run_row
    if source_id != prov.get("source"):
        reasons.append("<winner's run has another source>")
    if rdb_sha256 != prov.get("snapshot"):
        reasons.append("<winner's run has another snapshot>")
    if ledger_row is None:
        return reasons + ["<no ledger row for the key in the winner's run>"]
    state, decision, sha, wrote = ledger_row
    if state != "done" or decision not in _WRITES:
        reasons.append(f"<winner's ledger says {state}/{decision}>")
    if sha != prov.get("payload_sha"):
        reasons.append("<winner's ledger has another payload_sha>")
    if not isinstance(wrote, Mapping):
        return reasons + ["<winner's ledger records no written columns>"]
    if not _close(wrote.get("updated_at"), prov.get("updated_at")):
        reasons.append("<winner's ledger has another updated_at>")
    if not _close(row_updated_at, wrote.get("updated_at")):
        reasons.append("<_updated_at is not the winner's>")
    return reasons


def _superseded_by_merge(
    conn: Any,
    schema: str,
    mapping: ModelMapping,
    ts: Any,
    run: Mapping[str, Any],
    by_key: Mapping[str, Mapping[str, Any]],
    provenance: Mapping[str, Any],
    decisions: Mapping[str, str],
    pg_records: Mapping[str, Mapping[str, Any]],
    staged_cols: Mapping[str, Any],
) -> tuple[set[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Rows this run wrote that ANOTHER source has since won under the merge
    rule (#794): ``(keys, mismatches, unconfirmed)``.

    A run that crashed after committing some batches can be resumed after
    another store merged into the same schema. That store sees the crashed
    run's committed rows as ordinary migrated rows and, where its copy is
    newer, replaces them (``won_merge``), recording this run among the row's
    ``_migrated_from.losers``. The row is then correct, but it no longer
    holds this run's snapshot, so comparing it with that snapshot reported a
    mismatch that was not one. Such a row is verified against the WINNER
    instead.

    A candidate is a row whose provenance names another source and lists
    THIS source's snapshot of exactly this payload among its losers -- the
    trace the winning run leaves. The provenance cannot vouch for itself
    (#796 review: a self-consistent forged ``_migrated_from`` turned a wrong
    row CLEAN), so a candidate counts as superseded only when the tool's own
    records back the claimed winner independently: its run is in
    ``popoto_migration_run`` with the claimed source and snapshot; that
    run's ledger row for the key is ``done`` with a write decision and the
    claimed ``payload_sha``; the ledger's recorded ``updated_at`` is the
    claimed one; and the row's ``_updated_at`` column holds it. A candidate
    that fails any of these is ``unconfirmed``: it is NOT superseded, so the
    ``records`` and ``carried_state`` checks compare it with this run's
    snapshot, as for any other row this run wrote.

    A superseded row must then hold exactly the winner's payload (values and
    carried state), the winner must beat this run's copy under the merge
    rule -- decided on the winner's LEDGER values (recorded ``updated_at``,
    its run's ``source_id``), never the provenance's -- and the columns the
    tool writes outside the save path (``_created_at``,
    ``_estimated_fields``, embedding hashes, staged reads) must be what the
    winner's ledger says it wrote."""
    model = mapping.model
    claims: dict[str, Mapping[str, Any]] = {}
    for key in sorted(by_key):
        meta = by_key[key]["meta"]
        if meta["reject"] or decisions.get(key) not in _WRITES:
            continue
        prov = provenance.get(key)
        if not isinstance(prov, Mapping):
            continue
        if prov.get("source") in (None, run["source_id"]):
            continue
        if prov.get("payload_sha") == meta["payload_sha"]:
            continue  # the same payload: this run's rows hold it already
        lost = any(
            isinstance(e, Mapping)
            and e.get("source") == run["source_id"]
            and e.get("snapshot") == run["rdb_sha256"]
            and e.get("payload_sha") == meta["payload_sha"]
            for e in (prov.get("losers") or [])
        )
        if lost:
            claims[key] = prov
    if not claims:
        return set(), [], []
    runs, ledger = _winner_evidence(conn, schema, mapping.name, claims)
    columns = _read_tool_columns(conn, ts, sorted(claims))

    keys: set[str] = set()
    bad: list[dict[str, Any]] = []
    unconfirmed: list[dict[str, Any]] = []
    for key, prov in claims.items():
        meta = by_key[key]["meta"]
        run_id = str(prov.get("run_id"))
        run_row = runs.get(run_id)
        ledger_row = ledger.get((run_id, key))
        values = columns.get(key)
        reasons = _unconfirmed_reasons(
            prov, run_row, ledger_row, values["updated_at"] if values else None
        )
        if reasons or run_row is None or ledger_row is None or values is None:
            unconfirmed.append(
                {
                    "key": key,
                    "claimed_winner": prov.get("source"),
                    "claimed_run_id": prov.get("run_id"),
                    "reasons": reasons or ["<missing on Postgres>"],
                }
            )
            continue
        keys.add(key)
        wrote = ledger_row[3]
        parts: list[str] = []
        theirs = (float(wrote["updated_at"]), str(run_row[0]))
        if not theirs > (float(meta["updated_at"]), run["source_id"]):
            parts.append(WINNER_DOES_NOT_BEAT)
        got = pg_records.get(key)
        if got is None:
            parts.append("<missing on Postgres>")
        elif payload_sha(model, got) != ledger_row[2]:
            parts.append(NOT_WINNERS_PAYLOAD)
        # _updated_at already matched the ledger above, or the row would
        # not be superseded.
        parts += [p for p in _tool_column_parts(values, wrote) if p != "_updated_at"]
        if staged_cols and _staged_differs(staged_cols.get(key), wrote):
            parts.append(NOT_WINNERS_STAGED_READS)
        if parts:
            bad.append(
                {
                    "key": key,
                    "winner": run_row[0],
                    "winner_run_id": run_id,
                    "parts": parts,
                }
            )
    return keys, bad, unconfirmed


def _verify_carried_state(
    conn: Any,
    schema: str,
    mapping: ModelMapping,
    ts: Any,
    run: Mapping[str, Any],
    by_key: Mapping[str, Mapping[str, Any]],
    pg_records: Mapping[str, Mapping[str, Any]],
    superseded: "set[str] | frozenset[str]" = frozenset(),
) -> dict[str, Any]:
    """Every row THIS run wrote holds the snapshot's carried state.

    "Wrote" is read from the ledger, not from ``_migrated_from``: every key
    this run ever queued a save for (``write_started_at`` set), whatever its
    final decision -- including ``conflict_native``, which is how a row whose
    save committed without its carried state used to hide (#756 review: a
    crash between the save and the restore left seed confidence, counters
    and vector, the resume could not tell the row from a native write, and
    the verdict was CLEAN). The carried state is the export's ``state`` and
    ``model_state`` (confidence, access counters, prediction ledger, edges,
    cycles, validity, the vector's dims and float32 hash), normalized as the
    ``records`` check normalizes it. Only a key whose final decision lost
    the merge, deduplicated, or failed to load is left out: its row is
    another source's, or nothing was written. So is a row another source
    has won since (``superseded``, :func:`_superseded_by_merge`): it is
    verified against its winner's provenance instead.

    A row this run wrote and the application then changed natively (after
    a crash, before the resume) fails here too: the operator inspects it."""
    s = _qi(schema)
    written = {
        pk: decision
        for pk, decision in conn.execute(
            f"SELECT _pk, decision FROM {s}.{_qi(LEDGER_TABLE)} WHERE run_id = %s "
            "AND model = %s AND write_started_at IS NOT NULL",
            (run["run_id"], mapping.name),
        ).fetchall()
        if decision not in (LOST_MERGE, DEDUPLICATED, UNCHANGED, LOAD_ERROR, REJECTED)
        and pk not in superseded
    }
    conn.commit()
    bad: list[dict[str, Any]] = []
    for key in sorted(written):
        row = by_key.get(key)
        if row is None:
            continue
        got = pg_records.get(key)
        if got is None:
            bad.append({"key": key, "decision": written[key], "parts": ["<missing>"]})
            continue
        want = normalize_record(mapping.model, row["record"])
        have = normalize_record(mapping.model, got)
        parts = [
            part
            for part in _diff_parts(have, want)
            if part.split(".", 1)[0] in _CARRIED_SECTIONS
        ]
        if parts:
            bad.append({"key": key, "decision": written[key], "parts": parts})
    return {
        "compared": len(written),
        "mismatched": len(bad),
        "first_mismatches": bad[:VERIFY_MISMATCH_DETAIL],
        "ok": not bad,
    }


_SAVE_EVENT_OPS = frozenset({"create", "update"})


def _like_prefix(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _verify_event_stream(
    conn: Any, schema: str, mapping: ModelMapping, ts: Any, run: Mapping[str, Any]
) -> Optional[dict[str, Any]]:
    """Save events on an ``EventStreamMixin`` model's Postgres stream: every
    key THIS run wrote (and still owns) must hold exactly one save event
    (``create``/``update``) appended since this run first queued its write
    -- what a clean load leaves. A resume that saved an adopted row again
    shows up here as a key with two (#756 review: 1600 events for 1500
    records was reported CLEAN). A key with none is a mismatch too, unless
    the stream was trimmed (``MAXLEN``) or had entries deleted, which can
    remove the event legitimately. ``None`` for a model without a stream."""
    from ..fields.event_stream import EventStreamMixin

    model = mapping.model
    if not (isinstance(model, type) and issubclass(model, EventStreamMixin)):
        return None
    s = _qi(schema)
    written = {
        pk: int(start_ms)
        for pk, start_ms in conn.execute(
            "SELECT l._pk, floor(extract(epoch FROM coalesce(l.write_started_at, "
            "r.started_at)) * 1000)::bigint "
            f"FROM {s}.{_qi(LEDGER_TABLE)} l "
            f"JOIN {s}.{_qi(RUN_TABLE)} r ON r.run_id = l.run_id "
            f'JOIN {ts.qualified} t ON t."_pk" = l._pk '
            "WHERE l.run_id = %s AND l.model = %s AND l.state = 'done' "
            "AND l.decision = ANY(%s) AND t._migrated_from->>'run_id' = %s",
            (run["run_id"], mapping.name, list(_WRITES), run["run_id"]),
        ).fetchall()
    }
    base = f"stream:{model._stream_name}"
    counts = {pk: 0 for pk in written}
    events = 0
    trimmed = False
    if written and _table_exists(conn, schema, "popoto_stream_entry"):
        where = "(stream = %s OR stream LIKE %s ESCAPE '\\')"
        pattern = _like_prefix(base + ":") + "%"
        trimmed = bool(
            conn.execute(
                f"SELECT coalesce(bool_or(entries_added > length), false) "
                f"FROM {s}.popoto_stream WHERE {where}",
                (base, pattern),
            ).fetchone()[0]
        )
        for ms, fields in conn.execute(
            f"SELECT ms, fields FROM {s}.popoto_stream_entry WHERE {where} "
            "AND ms >= %s",
            (base, pattern, min(written.values())),
        ):
            flat = [bytes(f) for f in fields]
            entry = dict(zip(flat[0::2], flat[1::2]))
            pk = entry.get(b"pk", b"").decode("utf-8", "replace")
            op = entry.get(b"op", b"").decode("utf-8", "replace")
            if pk in counts and op in _SAVE_EVENT_OPS and int(ms) >= written[pk]:
                counts[pk] += 1
                events += 1
    conn.commit()
    doubled = sorted(pk for pk, n in counts.items() if n > 1)
    missing = sorted(pk for pk, n in counts.items() if n == 0)
    return {
        "written": len(written),
        "save_events": events,
        "keys_with_more_than_one": len(doubled),
        "keys_without_one": len(missing),
        "stream_trimmed": trimmed,
        "first_mismatches": [
            {"key": pk, "save_events": counts[pk]}
            for pk in (doubled + ([] if trimmed else missing))[:VERIFY_MISMATCH_DETAIL]
        ],
        "ok": not doubled and (trimmed or not missing),
    }


def verify(
    mappings: Sequence[ModelMapping],
    transformed: Mapping[str, Sequence[dict[str, Any]]],
    decisions: Mapping[str, Mapping[str, str]],
    *,
    backend: Any,
    dsn: str,
    run: Mapping[str, Any],
    sample: int,
) -> dict[str, Any]:
    """Read back through popoto on Postgres and compare semantic state with
    the throwaway Redis (which is still running)."""
    from ..fields.bm25_field import BM25Field
    from ..fields.decaying_sorted_field import DecayingSortedField

    models = [m.model for m in mappings]
    checks: dict[str, Any] = {}
    conn = _connect(dsn)
    try:
        for mapping in mappings:
            model = mapping.model
            rows = transformed[mapping.name]
            by_key = {r["key"]: r for r in rows}
            ts = backend._table(model._meta.spec)
            provenance = dict(
                conn.execute(
                    f'SELECT "_pk", "_migrated_from" FROM {ts.qualified} WHERE "_pk" = ANY(%s)',
                    (list(by_key),),
                ).fetchall()
            )
            staged_cols = {}
            if "AccessTrackerMixin" in {k.__name__ for k in model.__mro__}:
                staged_cols = {
                    pk: (n, at)
                    for pk, n, at in conn.execute(
                        f'SELECT "_pk", "_staged_reads", "_staged_at" FROM {ts.qualified} '
                        'WHERE "_pk" = ANY(%s)',
                        (list(by_key),),
                    ).fetchall()
                }
            conn.commit()
            owned = sorted(
                k
                for k, r in by_key.items()
                if not r["meta"]["reject"]
                and (provenance.get(k) or {}).get("payload_sha")
                == r["meta"]["payload_sha"]
            )
            with _postgres_side(models, backend):
                pg_records = _export_index(model)
                index_total = int(model.check_indexes()["total"])
            mismatches: list[dict[str, Any]] = []
            for key in owned:
                got = pg_records.get(key)
                want = by_key[key]["record"]
                if got is None:
                    mismatches.append({"key": key, "parts": ["<missing on Postgres>"]})
                    continue
                if payload_sha(model, got) != by_key[key]["meta"]["payload_sha"]:
                    mismatches.append(
                        {
                            "key": key,
                            "parts": _diff_parts(
                                normalize_record(model, got),
                                normalize_record(model, want),
                            ),
                        }
                    )
            staged_bad = [
                key
                for key in owned
                if staged_cols
                and _staged_differs(staged_cols.get(key), by_key[key]["meta"])
            ]
            column_compared, column_bad = _verify_tool_columns(
                conn, ts, by_key, provenance, run
            )
            superseded, superseded_bad, unconfirmed = _superseded_by_merge(
                conn,
                backend.schema,
                mapping,
                ts,
                run,
                by_key,
                provenance,
                decisions.get(mapping.name, {}),
                pg_records,
                staged_cols,
            )
            expected_owned = sum(
                1
                for k, r in by_key.items()
                if not r["meta"]["reject"]
                and k not in superseded
                and decisions.get(mapping.name, {}).get(k)
                not in (LOST_MERGE, CONFLICT_NATIVE, LOAD_ERROR)
            )
            model_checks: dict[str, Any] = {
                "records": {
                    "compared": len(owned),
                    "expected": expected_owned,
                    "mismatched": len(mismatches),
                    "first_mismatches": mismatches[:VERIFY_MISMATCH_DETAIL],
                    "ok": not mismatches and len(owned) == expected_owned,
                },
                "check_indexes": {"total": index_total, "ok": index_total == 0},
            }
            model_checks["tool_columns"] = {
                "compared": column_compared,
                "mismatched": len(column_bad),
                "first_mismatches": [
                    {"key": k, "parts": parts}
                    for k, parts in sorted(column_bad.items())[:VERIFY_MISMATCH_DETAIL]
                ],
                "ok": not column_bad,
            }
            if superseded or unconfirmed:
                # Rows another source won after this run wrote them (#794):
                # each holds its winner's payload and tool columns, and that
                # winner beats this run's copy under the merge rule. A row
                # whose claimed winner its own run and ledger do not back
                # (``unconfirmed``) is compared with this run's snapshot by
                # ``records`` and ``carried_state``, and listed here too.
                model_checks["superseded_by_merge"] = {
                    "compared": len(superseded),
                    "mismatched": len(superseded_bad),
                    "first_mismatches": superseded_bad[:VERIFY_MISMATCH_DETAIL],
                    "unconfirmed": len(unconfirmed),
                    "first_unconfirmed": unconfirmed[:VERIFY_MISMATCH_DETAIL],
                    "ok": not superseded_bad and not unconfirmed,
                }
            model_checks["carried_state"] = _verify_carried_state(
                conn, backend.schema, mapping, ts, run, by_key, pg_records, superseded
            )
            stream_check = _verify_event_stream(conn, backend.schema, mapping, ts, run)
            if stream_check is not None:
                model_checks["event_stream"] = stream_check
            if staged_cols:
                model_checks["staged_reads"] = {
                    "mismatched": len(staged_bad),
                    "ok": not staged_bad,
                }
            owned_set = set(owned)
            # Decay ranking order, per sampled partition.
            for field_name, field in model._meta.fields.items():
                if not isinstance(field, DecayingSortedField):
                    continue
                compared = 0
                bad: list[dict[str, Any]] = []
                for partition in _partitions(
                    mapping, field, [by_key[k] for k in owned], sample
                ):
                    with _redis_side(models):
                        on_redis = [
                            k
                            for k in _decay_order(
                                model, field_name, partition, VERIFY_DECAY_TOP_N
                            )
                            if k in owned_set
                        ]
                    with _postgres_side(models, backend):
                        on_pg = [
                            k
                            for k in _decay_order(
                                model, field_name, partition, VERIFY_DECAY_TOP_N
                            )
                            if k in owned_set
                        ]
                    width = min(len(on_redis), len(on_pg))
                    compared += 1
                    if on_redis[:width] != on_pg[:width]:
                        bad.append(
                            {
                                "partition": json.loads(
                                    json.dumps(partition, default=str)
                                ),
                                "redis": on_redis[:width],
                                "postgres": on_pg[:width],
                            }
                        )
                model_checks[f"decay_order:{field_name}"] = {
                    "partitions": compared,
                    "mismatched": len(bad),
                    "first_mismatches": bad[:5],
                    "ok": not bad,
                }
            # BM25 search on sample queries.
            for field_name, field in model._meta.fields.items():
                if not isinstance(field, BM25Field):
                    continue
                queries = _sample_queries(
                    mapping, field.source, [by_key[k] for k in owned], sample
                )
                with _redis_side(models):
                    redis_docs = {
                        m.decode() if isinstance(m, bytes) else str(m)
                        for m in _redis_bm25_docs(model, field_name)
                    }
                with _postgres_side(models, backend):
                    pg_docs = {str(o.db_key.redis_key) for o in model.query.all()}
                strict = redis_docs == pg_docs
                # With equal corpora the ranked top-k must match exactly.
                # Otherwise (rejected records, other stores in the table)
                # scores differ by corpus statistics, but WHICH of this
                # source's records match a query does not: every IDF is
                # positive, so a record matches exactly when it holds a
                # query term. That set is compared, in full, on both sides.
                width = len(redis_docs | pg_docs) + 1
                limit = VERIFY_BM25_TOP_K if strict else width
                bad = []
                overlap: list[float] = []
                for text in queries:
                    with _redis_side(models):
                        on_redis = [
                            k
                            for k, _ in BM25Field.search(
                                model, field_name, text, limit=limit
                            )
                        ]
                    with _postgres_side(models, backend):
                        on_pg = [
                            k
                            for k, _ in BM25Field.search(
                                model, field_name, text, limit=limit
                            )
                        ]
                    if strict:
                        if on_redis != on_pg:
                            bad.append(
                                {"query": text, "redis": on_redis, "postgres": on_pg}
                            )
                        continue
                    a = {k for k in on_redis if k in owned_set}
                    b = {k for k in on_pg if k in owned_set}
                    if a != b:
                        bad.append(
                            {
                                "query": text,
                                "only_redis": sorted(a - b)[:10],
                                "only_postgres": sorted(b - a)[:10],
                            }
                        )
                    ranked_a = [k for k in on_redis if k in owned_set]
                    ranked_b = [k for k in on_pg if k in owned_set]
                    top_a = set(ranked_a[:VERIFY_BM25_TOP_K])
                    top_b = set(ranked_b[:VERIFY_BM25_TOP_K])
                    overlap.append(len(top_a & top_b) / max(1, len(top_a | top_b)))
                model_checks[f"bm25:{field_name}"] = {
                    "queries": len(queries),
                    "mode": (
                        "strict"
                        if strict
                        else "accepted subset (corpora differ): match sets compared"
                    ),
                    "mismatched": len(bad),
                    "first_mismatches": bad[:5],
                    "mean_top_k_overlap": (
                        (sum(overlap) / len(overlap)) if overlap else None
                    ),
                    "ok": not bad,
                }
            checks[mapping.name] = model_checks
    finally:
        conn.close()
    return checks


def _redis_bm25_docs(model: Any, field_name: str) -> list[Any]:
    """The documents a Redis BM25 index scores against (its length set)."""
    from ..redis_db import get_REDIS_DB

    prefix = f"$BM25:{model.__name__}:{field_name}:dl"
    return list(get_REDIS_DB().zrange(prefix, 0, -1))


# -- report ----------------------------------------------------------------------


_SEAL_FIELDS = ("checksum_sha256", "hmac_sha256")


def _sealed_text(data: Mapping[str, Any]) -> str:
    """The canonical JSON a seal covers: the whole report, sign-off operator
    and time included, minus the seal values themselves."""
    unsealed = dict(data)
    sign_off = {
        k: v
        for k, v in dict(data.get("sign_off") or {}).items()
        if k not in _SEAL_FIELDS
    }
    unsealed["sign_off"] = sign_off
    return _canonical(json.loads(json.dumps(unsealed, default=str)))


def seal_report(data: Mapping[str, Any], key: Optional[bytes] = None) -> dict[str, str]:
    """The report's seal values.

    ``checksum_sha256`` is a plain checksum: it catches an accidental edit or
    a truncated copy, but anyone can recompute it after changing the report,
    so it is NOT evidence against tampering. ``hmac_sha256`` (only with an
    operator key, ``--report-key``) is: without the key it cannot be
    recomputed. Keep the key file off the machine that holds the report."""
    text = _sealed_text(data).encode("utf-8")
    seal = {"checksum_sha256": hashlib.sha256(text).hexdigest()}
    if key:
        seal["hmac_sha256"] = hmac.new(key, text, hashlib.sha256).hexdigest()
    return seal


def verify_report(data: Mapping[str, Any], key: Optional[bytes] = None) -> bool:
    """``True`` when ``data``'s seal matches its content (and, given ``key``,
    when its HMAC does too -- a report without one then fails)."""
    sign_off = dict(data.get("sign_off") or {})
    expected = seal_report(data, key)
    if not hmac.compare_digest(
        str(sign_off.get("checksum_sha256", "")), expected["checksum_sha256"]
    ):
        return False
    if key:
        return hmac.compare_digest(
            str(sign_off.get("hmac_sha256", "")), expected["hmac_sha256"]
        )
    return True


def read_report_key(path: "str | os.PathLike[str]") -> bytes:
    """The operator's HMAC key: the file's bytes, surrounding whitespace
    stripped. Refused when missing, empty, or shorter than 16 bytes."""
    try:
        key = Path(path).read_bytes().strip()
    except OSError as exc:
        raise MigrationRefused(f"--report-key {path}: {exc}") from exc
    if len(key) < 16:
        raise MigrationRefused(
            f"--report-key {path} holds {len(key)} byte(s); use at least 16 "
            "random bytes (e.g. openssl rand -hex 32 > key)"
        )
    return key


def render_summary(data: Mapping[str, Any]) -> str:
    lines = [
        f"popoto Redis -> Postgres migration (#756): {str(data.get('verdict', '?')).upper()}",
        f"run {data.get('run_id')}  source {data.get('source_id')}  "
        f"snapshot sha256 {str(data.get('snapshot', {}).get('rdb_sha256', ''))[:16]}",
        f"target {data.get('target', {}).get('schema')}  mode {data.get('mode')}",
        "",
        "Records:",
    ]
    for name, facts in (data.get("models") or {}).items():
        decisions = facts.get("decisions") or {}
        rendered = ", ".join(f"{k} {v}" for k, v in sorted(decisions.items()))
        lines.append(
            f"  {name}: exported {facts.get('exported')}, {rendered or 'not loaded'}"
        )
    lossy = data.get("lossy") or {}
    lines += ["", "Lossy and estimated (every non-zero count):"]
    if lossy:
        for name, value in sorted(lossy.items()):
            lines.append(f"  {name}: {value}")
    else:
        lines.append("  none")
    checks = data.get("verification") or {}
    if checks:
        lines += ["", "Verification:"]
        for name, model_checks in checks.items():
            for check, result in model_checks.items():
                state = "ok" if result.get("ok") else "MISMATCH"
                detail = {
                    k: v
                    for k, v in result.items()
                    if k not in ("ok", "first_mismatches")
                }
                lines.append(
                    f"  {name} {check}: {state} {json.dumps(detail, default=str)}"
                )
    if data.get("load_errors"):
        lines += ["", "Load errors:"] + [f"  {e}" for e in data["load_errors"][:20]]
    sign = data.get("sign_off") or {}
    lines += [
        "",
        f"Signed off by {sign.get('operator')} at {sign.get('at')}.",
        f"Checksum sha256 {sign.get('checksum_sha256')} (catches accidental "
        "change only: anyone can recompute it).",
    ]
    if sign.get("hmac_sha256"):
        lines.append(
            f"HMAC-SHA256 {sign.get('hmac_sha256')} under the operator's "
            "--report-key (tamper evidence for anyone without the key)."
        )
    else:
        lines.append("No HMAC: the run had no --report-key.")
    return "\n".join(lines) + "\n"


# -- orchestration ----------------------------------------------------------------


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_run_record(
    run_dir: Path, rdb_sha: str, source_id: str, resume: bool
) -> dict[str, Any]:
    path = run_dir / "run.json"
    if path.exists():
        record = json.loads(path.read_text())
        if record.get("rdb_sha256") != rdb_sha or record.get("source_id") != source_id:
            raise MigrationRefused(
                f"{run_dir} belongs to another run (snapshot {record.get('rdb_sha256', '')[:12]}, "
                f"source {record.get('source_id')}); use a new --run-dir"
            )
        if not resume:
            raise MigrationRefused(
                f"{run_dir} already holds run {record.get('run_id')}; pass --resume to "
                "continue it, or use a new --run-dir"
            )
        return dict(record)
    if resume:
        raise MigrationRefused(f"--resume given but {run_dir} holds no run.json")
    record = {
        "run_id": uuid.uuid4().hex,
        "source_id": source_id,
        "rdb_sha256": rdb_sha,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _atomic_write(path, json.dumps(record, indent=2) + "\n")
    return record


@contextlib.contextmanager
def _bound_to(server: ThrowawayRedis, db: int) -> Iterator[None]:
    """Point popoto's global client at the throwaway for the block, then put
    back exactly the client object that was bound before."""
    from .. import redis_db

    previous = redis_db.POPOTO_REDIS_DB
    redis_db.set_REDIS_DB_settings(
        connection_class=redis.UnixDomainSocketConnection,
        path=server.socket_path,
        password=server.password,
        db=db,
    )
    try:
        assert_bound_to_throwaway(redis_db.get_REDIS_DB(), server)
        yield
    finally:
        current = redis_db.POPOTO_REDIS_DB
        redis_db.POPOTO_REDIS_DB = previous
        if current is not previous:
            current.close()


@contextlib.contextmanager
def _content_root(path: Optional[Path]) -> Iterator[None]:
    """``POPOTO_CONTENT_PATH`` and the default content store pointed at the
    private copy of the operator's content directory."""
    from ..fields import content_field
    from ..stores.filesystem import FilesystemStore

    saved_env = os.environ.get("POPOTO_CONTENT_PATH")
    saved_store = content_field._default_content_store
    if path is not None:
        os.environ["POPOTO_CONTENT_PATH"] = str(path)
        content_field.set_default_store(FilesystemStore(base_path=str(path)))
    try:
        yield
    finally:
        if saved_env is None:
            os.environ.pop("POPOTO_CONTENT_PATH", None)
        else:
            os.environ["POPOTO_CONTENT_PATH"] = saved_env
        content_field._default_content_store = saved_store


def _check_models(mappings: Sequence[ModelMapping]) -> None:
    from ..backends import BackendCapabilityError, validate_spec

    if not mappings:
        raise MigrationRefused("no models to migrate; pass --model or --mapping")
    names = [m.name for m in mappings]
    if len(set(names)) != len(names):
        raise MigrationRefused(f"a model is listed twice: {sorted(names)}")
    for mapping in mappings:
        try:
            validate_spec(mapping.model._meta.spec, "postgres")
        except BackendCapabilityError as exc:
            raise MigrationRefused(
                f"{mapping.name} cannot be stored on Postgres: {exc}. Keep it on Redis."
            ) from exc


def run_migration(config: MigrationConfig) -> MigrationReport:
    """Run one snapshot end to end; return the signed-off report.

    Raises :class:`MigrationRefused` (nothing read or written),
    :class:`InventoryStop` (the snapshot was read, nothing written), or the
    underlying error of a failed load -- after which ``--resume`` with the
    same run directory continues where the last committed batch ended."""
    from ..backends.postgres import PostgresBackend

    started = time.time()
    mappings = list(config.mappings)
    _check_models(mappings)
    models = [m.model for m in mappings]
    dsn = config.postgres_dsn or os.environ.get("POPOTO_POSTGRES_URL", "")
    schema = (
        config.postgres_schema
        or os.environ.get("POPOTO_POSTGRES_SCHEMA", "")
        or "popoto"
    )
    if not config.dry_run and not dsn:
        raise MigrationRefused("POPOTO_POSTGRES_URL is not set (or pass --dry-run)")
    if not config.source_id or not re.fullmatch(
        r"[A-Za-z0-9_.@-]{1,100}", config.source_id
    ):
        raise MigrationRefused("--source-id must be 1-100 of [A-Za-z0-9_.@-]")
    rdb = Path(config.rdb_path)
    if not rdb.is_file() or not _rdb_magic_ok(rdb):
        raise MigrationRefused(f"{rdb} is not a readable RDB snapshot")
    if config.content_dir is not None and not Path(config.content_dir).is_dir():
        raise MigrationRefused(f"--content-dir {config.content_dir} is not a directory")
    report_key = (
        read_report_key(config.report_key) if config.report_key is not None else None
    )
    run_dir = Path(config.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    rdb_sha = _file_sha(rdb)
    run_record = _load_run_record(run_dir, rdb_sha, config.source_id, config.resume)
    run_id = str(run_record["run_id"])
    lock = _lock_target_schema(dsn, schema) if not config.dry_run else None
    try:
        target_facts: dict[str, Any] = {}
        if not config.dry_run:
            target_facts = preflight_target(
                dsn,
                schema,
                mappings,
                run_id=run_id,
                merge=config.merge,
                resume=config.resume,
            )
        snapshot_time = os.path.getmtime(rdb)

        report: dict[str, Any] = {
            "tool": "popoto.migrate_redis_to_postgres",
            "issue": 756,
            "run_id": run_id,
            "source_id": config.source_id,
            "mode": (
                "dry-run" if config.dry_run else ("merge" if config.merge else "load")
            ),
            "snapshot": {
                "rdb_path": str(rdb),
                "rdb_sha256": rdb_sha,
                "rdb_mtime": snapshot_time,
            },
            "target": {"schema": schema, "preflight": target_facts},
            "models": {},
            "lossy": {},
        }
        lossy: dict[str, int] = {}
        supervisor = _Supervisor().start()
        try:
            work = _private_dir("popoto-migrate-work-")
            supervisor.watch(work)
            try:
                content_copy: Optional[Path] = None
                if config.content_dir is not None:
                    content_copy = work / "content"
                    shutil.copytree(config.content_dir, content_copy, symlinks=False)
                with (
                    ThrowawayRedis(
                        rdb,
                        redis_server=config.redis_server,
                        work_parent=work,
                        supervisor=supervisor,
                    ) as server,
                    _bound_to(server, config.source_db),
                    _content_root(content_copy),
                ):
                    report["snapshot"]["throwaway"] = {
                        "tcp_port": 0,
                        "unix_socket": True,
                        "run_id": server.run_id,
                    }
                    client = ReadOnlyRedis(server, db=config.source_db)
                    try:
                        with _redis_side(models):
                            inventory = run_inventory(client, mappings, content_copy)
                    finally:
                        client.close()
                    _atomic_write(
                        run_dir / "inventory.json",
                        json.dumps(inventory, indent=2, default=str) + "\n",
                    )
                    report["inventory"] = {
                        "dbsize": inventory["dbsize"],
                        "out_of_scope_families": len(inventory["out_of_scope"]),
                        "stops": inventory["stops"],
                    }
                    stops = [
                        s
                        for s in inventory["stops"]
                        if not (config.accept_unclassified and "unclassified" in s)
                    ]
                    if stops:
                        raise InventoryStop(
                            "the inventory found state the tool cannot account for: "
                            + "; ".join(stops),
                            stops,
                        )

                    transformed: dict[str, list[dict[str, Any]]] = {}
                    manifests: dict[str, dict[str, Any]] = {}
                    with _redis_side(models):
                        for mapping in mappings:
                            facts = inventory["models"][mapping.name]
                            text, export_facts = _export_model(
                                mapping, facts["orphan_hashes"]
                            )
                            _atomic_write(
                                run_dir / "export" / f"{mapping.name}.jsonl",
                                text,
                                private=True,
                            )
                            lines = [
                                json.loads(line)
                                for line in text.splitlines()
                                if line.strip()
                            ]
                            manifests[mapping.name] = lines[0]
                            info: dict[str, Any] = {}
                            rows = [
                                transform_record(
                                    mapping,
                                    record,
                                    staged=facts["staged"],
                                    snapshot_time=snapshot_time,
                                    content_dir=content_copy,
                                    lossy=lossy,
                                    info=info,
                                )
                                for record in lines[1:]
                            ]
                            rows.sort(key=lambda r: r["key"])
                            transformed[mapping.name] = rows
                            _atomic_write(
                                run_dir / "transform" / f"{mapping.name}.jsonl",
                                "".join(
                                    json.dumps(r, sort_keys=True) + "\n" for r in rows
                                ),
                                private=True,
                            )
                            _count_inventory_lossy(mapping, facts, lossy)
                            if export_facts["errors"]:
                                lossy["export_errors"] = lossy.get(
                                    "export_errors", 0
                                ) + len(export_facts["errors"])
                            report["models"][mapping.name] = {
                                "exported": len(rows),
                                "export": export_facts,
                                "rejected": sum(1 for r in rows if r["meta"]["reject"]),
                                "rejects": [
                                    {"key": r["key"], "reason": r["meta"]["reject"]}
                                    for r in rows
                                    if r["meta"]["reject"]
                                ][:VERIFY_MISMATCH_DETAIL],
                                "sentinels": info.get("sentinels", {}),
                                "orphan_hashes_recovered": export_facts[
                                    "orphans_recovered"
                                ],
                            }
                    total = sum(len(r) for r in transformed.values())
                    if total == 0 and not config.allow_empty:
                        raise MigrationRefused(
                            "the snapshot holds no records for the listed models; on a "
                            "production machine that almost always means the wrong RDB or "
                            "--source-db. Pass --allow-empty if it really is empty."
                        )
                    for name, value in list(lossy.items()):
                        if not value:
                            del lossy[name]
                    report["lossy"] = dict(sorted(lossy.items()))

                    if config.dry_run:
                        report["verdict"] = "dry-run"
                    else:
                        backend = PostgresBackend(dsn=dsn, schema=schema)
                        run = {
                            "run_id": run_id,
                            "source_id": config.source_id,
                            "rdb_sha256": rdb_sha,
                            "now": datetime.datetime.now(
                                datetime.timezone.utc
                            ).isoformat(),
                        }
                        decisions: dict[str, dict[str, str]] = {
                            m.name: {} for m in mappings
                        }
                        errors: list[str] = []
                        conn = _connect(dsn)
                        try:
                            _ensure_tool_tables(conn, schema)
                            conn.execute(
                                f"INSERT INTO {_qi(schema)}.{_qi(RUN_TABLE)} (run_id, source_id, "
                                "rdb_sha256, status) VALUES (%s, %s, %s, 'loading') ON CONFLICT "
                                "(run_id) DO UPDATE SET status = 'loading'",
                                (run_id, config.source_id, rdb_sha),
                            )
                            conn.commit()
                            with _postgres_side(models, backend), _no_embedding(models):
                                for mapping in mappings:
                                    _load_model(
                                        mapping,
                                        transformed[mapping.name],
                                        manifests[mapping.name],
                                        backend=backend,
                                        conn=conn,
                                        run=run,
                                        batch_size=config.batch_size,
                                        decisions=decisions[mapping.name],
                                        errors=errors,
                                    )
                        finally:
                            conn.close()
                        for mapping in mappings:
                            tally: dict[str, int] = {}
                            for decision in decisions[mapping.name].values():
                                tally[decision] = tally.get(decision, 0) + 1
                            report["models"][mapping.name]["decisions"] = dict(
                                sorted(tally.items())
                            )
                        report["load_errors"] = errors
                        checks = verify(
                            mappings,
                            transformed,
                            decisions,
                            backend=backend,
                            dsn=dsn,
                            run=run,
                            sample=config.verify_sample,
                        )
                        report["verification"] = checks
                        clean = not errors and all(
                            result.get("ok")
                            for model_checks in checks.values()
                            for result in model_checks.values()
                        )
                        report["verdict"] = "clean" if clean else "mismatch"
            finally:
                shutil.rmtree(work, ignore_errors=True)
        finally:
            supervisor.close()

        report["duration_seconds"] = round(time.time() - started, 3)
        report["sign_off"] = {
            "operator": config.operator or _operator(),
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        report["sign_off"].update(seal_report(report, report_key))
        _atomic_write(
            run_dir / "report.json", json.dumps(report, indent=2, default=str) + "\n"
        )
        _atomic_write(run_dir / "report.txt", render_summary(report))
        if not config.dry_run:
            conn = _connect(dsn)
            try:
                conn.execute(
                    f"UPDATE {_qi(schema)}.{_qi(RUN_TABLE)} SET status = %s, finished_at = now(), "
                    "report = %s::jsonb WHERE run_id = %s",
                    (report["verdict"], json.dumps(report, default=str), run_id),
                )
                conn.commit()
            finally:
                conn.close()
        return MigrationReport(run_id=run_id, source_id=config.source_id, data=report)
    finally:
        if lock is not None:
            lock.close()


def _operator() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _count_inventory_lossy(
    mapping: ModelMapping, facts: Mapping[str, Any], lossy: dict[str, int]
) -> None:
    def add(name: str, n: int) -> None:
        if n:
            lossy[name] = lossy.get(name, 0) + n

    add("per_record_ttl_not_carried", len(facts["ttl_records"]))
    add("meta_ttl_restarted", len(facts.get("meta_ttl_records") or ()))
    add("event_stream_entries_not_carried", int(facts["event_stream_entries"]))
    add("orphan_hashes_recovered", len(facts["orphan_hashes"]))
    add("class_members_without_hash", len(facts["class_members_without_hash"]))
    families = facts["families"]
    add("frequency_sketch_keys_reset", int(families.get("$FS", {}).get("keys", 0)))
    add(
        "write_filter_priority_keys_not_stored",
        int(families.get("$WF", {}).get("keys", 0)),
    )
    for field_name, emb in facts["embeddings"].items():
        add("embedding_files_without_record", int(emb["files_without_record"]))
    for field_name, decay in facts["decay_index"].items():
        add("decay_index_score_mismatches", int(decay["score_mismatches"]))


# -- CLI ---------------------------------------------------------------------------


def _import_object(spec: str) -> Any:
    module_name, _, attribute = spec.partition(":")
    if not attribute:
        raise MigrationRefused(f"{spec!r}: expected module.path:Name")
    module = importlib.import_module(module_name)
    obj: Any = module
    for part in attribute.split("."):
        obj = getattr(obj, part)
    return obj


def _mappings_from_args(
    models: Sequence[str], mappings: Sequence[str]
) -> list[ModelMapping]:
    out: list[ModelMapping] = []
    for spec in mappings:
        obj = _import_object(spec)
        items = obj if isinstance(obj, (list, tuple)) else [obj]
        for item in items:
            if not isinstance(item, ModelMapping):
                raise MigrationRefused(
                    f"{spec!r} is not a ModelMapping (or a list of them)"
                )
            out.append(item)
    for spec in models:
        out.append(ModelMapping(model=_import_object(spec)))
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m popoto.migrate_redis_to_postgres",
        description=(
            "One-off copy of popoto models from a Redis RDB snapshot into Postgres "
            "(#756). Reads only from a private redis-server it starts on the "
            "snapshot; there is deliberately no Redis URL option. The target is "
            "POPOTO_POSTGRES_URL / POPOTO_POSTGRES_SCHEMA."
        ),
    )
    parser.add_argument(
        "--rdb",
        required=True,
        type=Path,
        help="copy of dump.rdb taken after BGSAVE with writers frozen",
    )
    parser.add_argument(
        "--content-dir",
        type=Path,
        help="copy of $POPOTO_CONTENT_PATH (holds .embeddings/ and ContentField files)",
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help="archive directory for this run's artifacts and report",
    )
    parser.add_argument(
        "--source-id",
        required=True,
        help="this store's id (e.g. the machine name); recorded in _migrated_from only",
    )
    parser.add_argument(
        "--model", action="append", default=[], help="module.path:Model (repeatable)"
    )
    parser.add_argument(
        "--mapping",
        action="append",
        default=[],
        help="module.path:MAPPING, a ModelMapping or a list of them (repeatable)",
    )
    parser.add_argument(
        "--source-db",
        type=int,
        default=0,
        help="database index inside the snapshot (default 0)",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="merge into a target that already holds rows (merge rule per key)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="continue an interrupted run in the same --run-dir",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="inventory, export and transform only; write nothing to Postgres",
    )
    parser.add_argument(
        "--allow-empty", action="store_true", help="accept a snapshot with no records"
    )
    parser.add_argument(
        "--accept-unclassified",
        action="store_true",
        help="do not stop on unknown key families of a migrated model",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--redis-server",
        default="redis-server",
        help="redis-server binary (same major version as the snapshot)",
    )
    parser.add_argument(
        "--verify-sample",
        type=int,
        default=25,
        help="partitions and queries sampled by verification",
    )
    parser.add_argument(
        "--operator", default="", help="name recorded in the report's sign-off"
    )
    parser.add_argument(
        "--report-key",
        type=Path,
        help=(
            "file holding a secret key; the report then carries an HMAC-SHA256 "
            "(tamper evidence). Without it the report carries only a checksum."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = MigrationConfig(
        rdb_path=args.rdb,
        run_dir=args.run_dir,
        source_id=args.source_id,
        mappings=[],
        content_dir=args.content_dir,
        source_db=args.source_db,
        merge=args.merge,
        resume=args.resume,
        dry_run=args.dry_run,
        allow_empty=args.allow_empty,
        accept_unclassified=args.accept_unclassified,
        batch_size=args.batch_size,
        redis_server=args.redis_server,
        verify_sample=args.verify_sample,
        operator=args.operator,
        report_key=args.report_key,
    )

    def _terminate(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    # SIGHUP is what an SSH drop sends; SIGINT is Ctrl-C. Each unwinds the
    # run through its finally blocks (stopping the throwaway server and
    # removing its copies). SIGKILL cannot be caught: the watchdog covers it.
    handled = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    previous = {s: signal.signal(s, _terminate) for s in handled}
    try:
        config.mappings = _mappings_from_args(args.model, args.mapping)
        report = run_migration(config)
    except InventoryStop as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return 3
    except MigrationRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt as exc:
        print(
            f"INTERRUPTED ({exc or 'signal 2'}): the throwaway server is stopped "
            "and its copies removed. Continue with --resume and the same "
            "--run-dir.",
            file=sys.stderr,
        )
        return 130
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    sys.stdout.write(report.summary())
    return 0 if report.clean or report.verdict == "dry-run" else 1
