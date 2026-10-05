"""One-off migration of agent memory from a Redis snapshot into Postgres (#756).

Run it as a module::

    python -m popoto.transfer.migrate_redis_to_postgres \\
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
the tool then copies that RDB into a private temporary directory, starts its
own ``redis-server`` on a random loopback port with persistence switched off,
and reads only from that process, identified by its ``run_id``. The process
is always stopped and its directory removed, on success and on error alike.

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
``_updated_at`` (and the staged-read columns) in a transaction of its own,
together with its ledger and resume marker. A crash between the two leaves a
``pending`` ledger row, which is how a resumed run tells its own half-written
rows from rows popoto wrote natively.

**Merge rule** (v2 plan, "Reconciliation with #755 / #756 / #758"): several
per-machine stores load into the one central database, one run per store,
each with its own ``--source-id``. Within a key, an equal payload is
deduplicated (the source is recorded under ``_migrated_from.duplicates``);
a differing payload keeps the later ``_updated_at``, ties going to the
greater source id, and the loser is logged under ``_migrated_from.losers``.
The same source re-run with a newer snapshot replaces its own rows (the
delta mechanism). A row popoto wrote natively (``_migrated_from IS NULL``)
is never overwritten. The source id lives only in ``_migrated_from``; it
never becomes part of a key or a scope.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import datetime
import getpass
import hashlib
import importlib
import io
import json
import logging
import os
import re
import shutil
import signal
import socket
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
    "run_migration",
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
_LOOPBACK = "127.0.0.1"


def _free_port() -> int:
    """A random high loopback port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_LOOPBACK, 0))
        return int(sock.getsockname()[1])


def _rdb_magic_ok(path: Path) -> bool:
    with open(path, "rb") as handle:
        return handle.read(5) == b"REDIS"


class ThrowawayRedis:
    """A private ``redis-server`` serving a copy of an RDB snapshot.

    Started on a random loopback port, in a fresh temporary directory, with
    ``--save ""`` and ``--appendonly no`` so it never writes anything back.
    :meth:`stop` (and leaving the ``with`` block) kills it and removes the
    directory; :meth:`stop` is idempotent and also registered against
    ``SIGTERM`` for the duration of a CLI run.
    """

    def __init__(
        self,
        rdb_path: "str | os.PathLike[str]",
        *,
        redis_server: str = "redis-server",
        work_parent: "str | os.PathLike[str] | None" = None,
    ) -> None:
        self.rdb_path = Path(rdb_path)
        self.redis_server = redis_server
        self.work_parent = Path(work_parent) if work_parent is not None else None
        self.port: int = 0
        self.pid: Optional[int] = None
        self.run_id: str = ""
        self.directory: Optional[Path] = None
        self._process: Optional[subprocess.Popen[bytes]] = None

    # context manager ------------------------------------------------------
    def __enter__(self) -> "ThrowawayRedis":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

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
        parent = str(self.work_parent) if self.work_parent is not None else None
        self.directory = Path(tempfile.mkdtemp(prefix="popoto-migrate-", dir=parent))
        try:
            shutil.copyfile(self.rdb_path, self.directory / "dump.rdb")
            last_error: Optional[str] = None
            for _attempt in range(5):
                self.port = _free_port()
                self._process = subprocess.Popen(
                    [
                        binary,
                        "--port",
                        str(self.port),
                        "--bind",
                        _LOOPBACK,
                        "--protected-mode",
                        "yes",
                        "--dir",
                        str(self.directory),
                        "--dbfilename",
                        "dump.rdb",
                        "--save",
                        "",
                        "--appendonly",
                        "no",
                        "--daemonize",
                        "no",
                        "--logfile",
                        str(self.directory / "redis.log"),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.pid = self._process.pid
                last_error = self._wait_ready()
                if last_error is None:
                    return
                self._kill()
            raise MigrationRefused(
                f"throwaway redis-server did not start: {last_error}; see the log "
                f"excerpt above"
            )
        except BaseException:
            self.stop()
            raise

    def _wait_ready(self) -> Optional[str]:
        """``None`` once the server answers and has finished loading the
        snapshot, else the reason it never did."""
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        client = redis.Redis(host=_LOOPBACK, port=self.port, socket_timeout=5)
        try:
            while time.monotonic() < deadline:
                if self._process is not None and self._process.poll() is not None:
                    return f"exited with {self._process.returncode}: {self._log_tail()}"
                try:
                    info = client.info()
                except (redis.ConnectionError, redis.BusyLoadingError):
                    time.sleep(0.05)
                    continue
                if int(info.get("loading", 0)):
                    time.sleep(0.05)
                    continue
                if int(info.get("process_id", -1)) != self.pid:
                    return "another process answered on the chosen port"
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
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)

    def stop(self) -> None:
        self._kill()
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None


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
    build one is from a running throwaway server, and it re-checks that
    server's ``run_id`` on construction."""

    def __init__(self, server: ThrowawayRedis, db: int = 0) -> None:
        if not server.running or not server.run_id:
            raise LiveRedisRefused("the throwaway redis-server is not running")
        super().__init__(host=_LOOPBACK, port=server.port, db=db, socket_timeout=30)
        info = self.info("server")
        if str(info.get("run_id")) != server.run_id:
            raise LiveRedisRefused(
                f"port {server.port} is answered by run_id {info.get('run_id')}, "
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

    The host and port are compared from the connection parameters first, so
    a client bound anywhere else -- popoto's default ``localhost`` database 0
    included -- is refused without a command reaching it. Only then is the
    server's ``run_id`` read and compared."""
    kwargs = dict(getattr(client.connection_pool, "connection_kwargs", {}) or {})
    host = str(kwargs.get("host", ""))
    port = kwargs.get("port")
    if kwargs.get("unix_socket_path") or kwargs.get("path"):
        raise LiveRedisRefused("popoto's client is bound to a unix socket")
    if host not in (_LOOPBACK, "localhost") or port != server.port:
        raise LiveRedisRefused(
            f"popoto's Redis client is bound to {host}:{port}/db{kwargs.get('db')}, "
            f"not the throwaway snapshot server on {_LOOPBACK}:{server.port}. The "
            "migration never reads a live store."
        )
    run_id = str(client.info("server").get("run_id"))
    if run_id != server.run_id:
        raise LiveRedisRefused(
            f"{host}:{port} reports run_id {run_id}, not the throwaway server's "
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
"""Records per load batch: one ledger commit, one ``import_records`` call
and one provenance transaction each."""

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


@dataclasses.dataclass
class MigrationReport:
    """The signed-off result of a run (``report.json`` / ``report.txt``)."""

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
            if int(client.pttl(key)) > 0:
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


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)


def _export_model(
    mapping: ModelMapping, orphans: Sequence[str]
) -> tuple[str, dict[str, Any]]:
    """The model's JSONL: ``export_records`` over the class set, then the
    orphan hashes ``SMEMBERS`` cannot see, hydrated the same way."""
    from ..models.query import Query
    from .export import _field_state, _model_state, _record_values, export_records
    from .format import dump_line, to_jsonable
    from .results import ExportResult

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
    from ..fields.content_field import ContentField
    from ..fields.cyclic_decay_field import CyclicDecayField
    from ..fields.datetime_field import DatetimeField
    from ..fields.decaying_sorted_field import DecayingSortedField
    from ..fields.embedding_field import EmbeddingField
    from .format import from_jsonable

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

    # Long-tail state Postgres does not keep.
    for field_name, field in model._meta.fields.items():
        carried = state.get(field_name)
        if isinstance(field, CyclicDecayField) and isinstance(carried, dict):
            for cycle in carried.get("cycles") or []:
                if isinstance(cycle, list) and len(cycle) >= 4 and cycle[3] is not None:
                    count("cycle_baselines_dropped")
        if isinstance(field, CoOccurrenceField) and isinstance(carried, list):
            over = len(carried) - int(field.max_edges)
            if over > 0:
                count("co_occurrence_edges_truncated", over)
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
                raise TargetNotEmpty(
                    f"{schema}.{table} already holds {total} row(s) for "
                    f"{mapping.name}. Pass --merge to merge this store into them "
                    "(the merge rule decides per key), or --resume with the same "
                    "--run-dir to continue an interrupted run."
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


def _provenance(
    meta: Mapping[str, Any],
    run: Mapping[str, Any],
    previous: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    losers = list((previous or {}).get("losers") or [])
    if previous is not None and previous.get("source") not in (None, run["source_id"]):
        losers.append(
            {
                k: previous.get(k)
                for k in ("source", "run_id", "snapshot", "payload_sha", "updated_at")
            }
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


_crash_hook: Optional[Callable[[str, int], None]] = None
"""Test seam: called with ``(model, batch number)`` after a batch's records
landed and before its provenance commits -- the window a crash must
survive."""


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
    from ..backends.postgres.search import record_lock_sql
    from .format import dump_line
    from .import_ import import_records

    model = mapping.model
    schema = backend.schema
    ts = backend._table(model._meta.spec, write=True)
    table = ts.qualified
    ledger = f"{_qi(schema)}.{_qi(LEDGER_TABLE)}"
    runs = f"{_qi(schema)}.{_qi(RUN_TABLE)}"
    layout = ts.search
    embedding_layouts = list(layout.embedding.values()) if layout is not None else []
    has_access = "AccessTrackerMixin" in {k.__name__ for k in model.__mro__}

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

    for number, start in enumerate(range(0, len(todo), max(1, batch_size))):
        batch = todo[start : start + batch_size]
        keys = [r["key"] for r in batch]
        found = conn.execute(
            f'SELECT "_pk", "_migrated_from" FROM {table} WHERE "_pk" = ANY(%s)',
            (keys,),
        ).fetchall()
        existing = {pk: mf for pk, mf in found}
        pending: dict[str, Optional[dict[str, Any]]] = {}
        for pk, detail in conn.execute(
            f"SELECT _pk, detail FROM {ledger} WHERE model = %s AND _pk = ANY(%s) "
            "AND state = 'pending' ORDER BY at",
            (mapping.name, keys),
        ).fetchall():
            pending[pk] = json.loads(detail) if detail else None
        conn.commit()

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

        writes = [row for row, d in plan if d in _WRITES]
        if writes:
            conn.cursor().executemany(
                f"INSERT INTO {ledger} (run_id, model, _pk, state, decision, payload_sha, "
                "detail) VALUES (%s, %s, %s, 'pending', 'write', %s, %s) ON CONFLICT "
                "(run_id, model, _pk) DO UPDATE SET state = 'pending', decision = "
                "'write', payload_sha = EXCLUDED.payload_sha, detail = EXCLUDED.detail, "
                "at = now()",
                [
                    (
                        run["run_id"],
                        mapping.name,
                        r["key"],
                        r["meta"]["payload_sha"],
                        # The provenance this write replaces: a resume after
                        # a crash finds the row's _migrated_from already
                        # nulled by the save, and reads it back from here.
                        json.dumps(previous_of(r["key"])),
                    )
                    for r in writes
                ],
            )
            conn.commit()
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

        # The provenance transaction: one commit per batch.
        landed = [row for row, d in plan if d in _WRITES]
        if landed:
            lock_sql, lock_params = record_lock_sql(ts, [r["key"] for r in landed])
            if lock_sql:
                conn.execute(lock_sql.rstrip().rstrip(";"), lock_params)
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
                conn.execute(
                    f'UPDATE {table} SET {", ".join(sets)} WHERE "_pk" = %s',
                    params + [key],
                )
            elif decision == DEDUPLICATED and previous is not None:
                merged = dict(previous)
                duplicates = list(merged.get("duplicates") or [])
                entry = {
                    "source": run["source_id"],
                    "run_id": run["run_id"],
                    "snapshot": run["rdb_sha256"],
                }
                if entry not in duplicates:
                    duplicates.append(entry)
                merged["duplicates"] = duplicates
                conn.execute(
                    f'UPDATE {table} SET "_migrated_from" = %s::jsonb WHERE "_pk" = %s '
                    'AND "_migrated_from" IS NOT NULL',
                    (json.dumps(merged), key),
                )
            elif decision == LOST_MERGE and previous is not None:
                merged = dict(previous)
                losers = list(merged.get("losers") or [])
                entry = {
                    "source": run["source_id"],
                    "run_id": run["run_id"],
                    "snapshot": run["rdb_sha256"],
                    "payload_sha": meta["payload_sha"],
                    "updated_at": meta["updated_at"],
                }
                if entry not in losers:
                    losers.append(entry)
                merged["losers"] = losers
                conn.execute(
                    f'UPDATE {table} SET "_migrated_from" = %s::jsonb WHERE "_pk" = %s '
                    'AND "_migrated_from" IS NOT NULL',
                    (json.dumps(merged), key),
                )
            decisions[key] = decision
        conn.cursor().executemany(
            f"INSERT INTO {ledger} (run_id, model, _pk, state, decision, payload_sha, detail) "
            "VALUES (%s, %s, %s, 'done', %s, %s, %s) ON CONFLICT (run_id, model, _pk) "
            "DO UPDATE SET state = 'done', decision = EXCLUDED.decision, "
            "payload_sha = EXCLUDED.payload_sha, detail = EXCLUDED.detail, at = now()",
            [
                (
                    run["run_id"],
                    mapping.name,
                    r["key"],
                    d,
                    r["meta"]["payload_sha"],
                    r["meta"]["reject"],
                )
                for r, d in plan
            ],
        )
        # Rows of other runs left pending on these keys are settled too: this
        # run has now written or judged each of them.
        conn.execute(
            f"UPDATE {ledger} SET state = 'superseded' WHERE model = %s AND "
            "_pk = ANY(%s) AND state = 'pending' AND run_id <> %s",
            (mapping.name, keys, run["run_id"]),
        )
        conn.execute(
            f"UPDATE {runs} SET progress = jsonb_set(progress, ARRAY[%s], to_jsonb(%s::text)) "
            "WHERE run_id = %s",
            (mapping.name, keys[-1], run["run_id"]),
        )
        conn.commit()


# -- verification ----------------------------------------------------------------


def _export_index(model: Any) -> dict[str, dict[str, Any]]:
    from .export import export_records

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
    from .format import from_jsonable

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
    from .format import from_jsonable

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
            staged_bad = []
            for key in owned:
                if not staged_cols:
                    break
                meta = by_key[key]["meta"]
                n, at = staged_cols.get(key, (None, None))
                if int(n or 0) != int(meta["staged_reads"] or 0) or (
                    meta["staged_at"] is not None
                    and abs(float(at or 0) - float(meta["staged_at"])) > 1e-6
                ):
                    staged_bad.append(key)
            expected_owned = sum(
                1
                for k, r in by_key.items()
                if not r["meta"]["reject"]
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
                bad = []
                overlap: list[float] = []
                for text in queries:
                    with _redis_side(models):
                        on_redis = [
                            k
                            for k, _ in BM25Field.search(
                                model, field_name, text, limit=VERIFY_BM25_TOP_K
                            )
                        ]
                    with _postgres_side(models, backend):
                        on_pg = [
                            k
                            for k, _ in BM25Field.search(
                                model, field_name, text, limit=VERIFY_BM25_TOP_K
                            )
                        ]
                    if strict:
                        if on_redis != on_pg:
                            bad.append(
                                {"query": text, "redis": on_redis, "postgres": on_pg}
                            )
                    else:
                        a = [k for k in on_redis if k in owned_set]
                        b = [k for k in on_pg if k in owned_set]
                        overlap.append(
                            len(set(a) & set(b)) / max(1, len(set(a) | set(b)))
                        )
                model_checks[f"bm25:{field_name}"] = {
                    "queries": len(queries),
                    "mode": "strict" if strict else "informational (corpora differ)",
                    "mismatched": len(bad),
                    "first_mismatches": bad[:5],
                    "mean_overlap": (sum(overlap) / len(overlap)) if overlap else None,
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


def _sign(data: dict[str, Any]) -> str:
    unsigned = {k: v for k, v in data.items() if k != "sign_off"}
    return _sha(_canonical(unsigned))


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
        f"Signed off by {sign.get('operator')} at {sign.get('at')}; report sha256 {sign.get('sha256')}",
    ]
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
    redis_db.set_REDIS_DB_settings(host=_LOOPBACK, port=server.port, db=db)
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
    run_dir = Path(config.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    rdb_sha = _file_sha(rdb)
    run_record = _load_run_record(run_dir, rdb_sha, config.source_id, config.resume)
    run_id = str(run_record["run_id"])
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
        "tool": "popoto.transfer.migrate_redis_to_postgres",
        "issue": 756,
        "run_id": run_id,
        "source_id": config.source_id,
        "mode": "dry-run" if config.dry_run else ("merge" if config.merge else "load"),
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
    work = Path(tempfile.mkdtemp(prefix="popoto-migrate-work-"))
    try:
        content_copy: Optional[Path] = None
        if config.content_dir is not None:
            content_copy = work / "content"
            shutil.copytree(config.content_dir, content_copy, symlinks=False)
        with (
            ThrowawayRedis(
                rdb, redis_server=config.redis_server, work_parent=work
            ) as server,
            _bound_to(server, config.source_db),
            _content_root(content_copy),
        ):
            report["snapshot"]["throwaway"] = {
                "port": server.port,
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
                    text, export_facts = _export_model(mapping, facts["orphan_hashes"])
                    _atomic_write(run_dir / "export" / f"{mapping.name}.jsonl", text)
                    lines = [
                        json.loads(line) for line in text.splitlines() if line.strip()
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
                        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                    )
                    _count_inventory_lossy(mapping, facts, lossy)
                    if export_facts["errors"]:
                        lossy["export_errors"] = lossy.get("export_errors", 0) + len(
                            export_facts["errors"]
                        )
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
                        "orphan_hashes_recovered": export_facts["orphans_recovered"],
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
                    "now": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
                decisions: dict[str, dict[str, str]] = {m.name: {} for m in mappings}
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

    report["duration_seconds"] = round(time.time() - started, 3)
    report["sign_off"] = {
        "operator": config.operator or _operator(),
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    report["sign_off"]["sha256"] = _sign(report)
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
        prog="python -m popoto.transfer.migrate_redis_to_postgres",
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
    )

    def _terminate(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        config.mappings = _mappings_from_args(args.model, args.mapping)
        report = run_migration(config)
    except InventoryStop as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return 3
    except MigrationRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous)
    sys.stdout.write(report.summary())
    return 0 if report.clean or report.verdict == "dry-run" else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
