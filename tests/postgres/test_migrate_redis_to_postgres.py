"""The one-off Redis -> Postgres migration (#756), end to end.

Each test builds a realistic source the way an operator would hand it over:
records are written through popoto on the suite's scratch Redis database
(never 0), copied key for key into a private ``redis-server`` on a random
port, ``SAVE``d to an RDB in a temp dir, and that server is stopped. The copy
is by type (``HGETALL`` -> ``HSET`` and so on) rather than ``DUMP``/``RESTORE``,
because a ``DUMP`` payload only restores into a server at least as new as the
one that wrote it, and CI's Redis service and its ``redis-server`` binary are
different builds. The tool then gets only the RDB file and a copy of the
content directory -- never a Redis URL.

Needs the ``redis-server`` binary (skipped with a reason when absent) and
``POSTGRES_URL`` (the ``pg`` fixture skips without it).
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
psycopg = pytest.importorskip("psycopg")

import popoto  # noqa: E402
import redis  # noqa: E402
from popoto.backends import set_backend  # noqa: E402
from popoto import migrate_redis_to_postgres as mig  # noqa: E402
from popoto.migrate_redis_to_postgres import (  # noqa: E402
    ForbiddenCommand,
    InventoryStop,
    LiveRedisRefused,
    MigrationConfig,
    ReadOnlyRedis,
    TargetNotEmpty,
    ThrowawayRedis,
    assert_bound_to_throwaway,
    run_migration,
)

from . import migrate_fixtures as fx  # noqa: E402

REDIS_SERVER = shutil.which("redis-server")
#: Set in CI's Postgres job, which installs the binary: there a missing
#: binary is a failure, never a silent skip.
REQUIRED = bool(os.environ.get("POPOTO_REQUIRE_REDIS_SERVER"))
if REDIS_SERVER is None and REQUIRED:  # pragma: no cover - CI misconfiguration
    raise RuntimeError(
        "POPOTO_REQUIRE_REDIS_SERVER is set but no redis-server binary is on PATH"
    )
needs_redis_server = pytest.mark.skipif(
    REDIS_SERVER is None,
    reason="the redis-server binary is not installed; the migration serves its "
    "snapshot from a private redis-server (install redis, e.g. apt-get install "
    "redis-server or brew install redis)",
)
pytestmark = needs_redis_server

MODEL_PATTERNS = ("*Mig*", "stream:mig_*")


# -- building a snapshot ----------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _FixtureServer:
    """A private redis-server the source is copied into and SAVEd from."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.port = _free_port()
        self.process = subprocess.Popen(
            [
                REDIS_SERVER,
                "--port",
                str(self.port),
                "--bind",
                "127.0.0.1",
                "--dir",
                str(directory),
                "--dbfilename",
                "dump.rdb",
                "--save",
                "",
                "--appendonly",
                "no",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.client = redis.Redis(host="127.0.0.1", port=self.port)
        deadline = time.monotonic() + 30
        while True:
            try:
                self.client.ping()
                break
            except redis.ConnectionError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def save_and_stop(self) -> Path:
        self.client.save()
        self.client.close()
        self.process.terminate()
        self.process.wait(timeout=10)
        return self.directory / "dump.rdb"


def _copy_key(src, dst, key) -> None:
    kind = src.type(key).decode()
    if kind == "hash":
        dst.hset(key, mapping=src.hgetall(key))
    elif kind == "set":
        dst.sadd(key, *src.smembers(key))
    elif kind == "zset":
        dst.zadd(key, dict(src.zrange(key, 0, -1, withscores=True)))
    elif kind == "list":
        dst.rpush(key, *src.lrange(key, 0, -1))
    elif kind == "string":
        dst.set(key, src.get(key))
    elif kind == "stream":
        for entry_id, fields in src.xrange(key):
            dst.xadd(key, fields, id=entry_id)
    else:  # pragma: no cover - the fixture writes no other type
        raise AssertionError(f"cannot copy {key} of type {kind}")
    ttl = src.pttl(key)
    if ttl > 0:
        dst.pexpire(key, ttl)


def _scratch_client():
    client = popoto.get_redis()
    db = client.connection_pool.connection_kwargs.get("db")
    assert db not in (0, "0", None), "the source is only ever built on a scratch DB"
    return client


def _wipe_models():
    client = _scratch_client()
    for pattern in MODEL_PATTERNS:
        for key in list(client.scan_iter(match=pattern, count=1000)):
            client.delete(key)


def build_snapshot(
    tmp_path, monkeypatch, *, variant="a", mutate=None, name="snap", now=None
):
    """Seed on the scratch DB, copy into a fixture server, SAVE an RDB.

    Returns ``(rdb_path, content_dir, facts)``."""
    content = tmp_path / f"{name}-content"
    monkeypatch.setenv("POPOTO_CONTENT_PATH", str(content))
    from popoto.fields import content_field
    from popoto.stores.filesystem import FilesystemStore

    monkeypatch.setattr(
        content_field, "_default_content_store", FilesystemStore(str(content))
    )
    previous = set_backend("redis")
    try:
        _wipe_models()
        facts = fx.seed_source(variant=variant, now=now)
        client = _scratch_client()
        directory = tmp_path / f"{name}-fixture-server"
        directory.mkdir()
        server = _FixtureServer(directory)
        try:
            for pattern in MODEL_PATTERNS:
                for key in client.scan_iter(match=pattern, count=1000):
                    _copy_key(client, server.client, key)
            if mutate is not None:
                mutate(server.client, facts)
            rdb = server.save_and_stop()
        except BaseException:
            server.process.kill()
            raise
    finally:
        _wipe_models()
        set_backend(previous)
    snapshot = tmp_path / f"{name}.rdb"
    shutil.copyfile(rdb, snapshot)
    _damage_embeddings(content, facts)
    return snapshot, content, facts


def _npy_path(content: Path, key: str) -> Path:
    import hashlib

    return (
        content
        / ".embeddings"
        / "MigMemory"
        / (hashlib.sha256(key.encode()).hexdigest() + ".npy")
    )


def _damage_embeddings(content: Path, facts) -> None:
    """One record whose ``.npy`` is gone, one whose vector has the wrong
    dimension: both are re-embedded by the backfill after cutover."""
    missing = _npy_path(content, facts["missing_npy_key"])
    assert missing.exists()
    missing.unlink()
    wrong = _npy_path(content, facts["wrong_dim_key"])
    np.save(str(wrong), np.arange(8, dtype=np.float32))


def _orphan_and_dangling(client, facts):
    """An orphan hash (out of the class set) and a class member whose hash is
    gone: the shapes ``SMEMBERS``-driven export alone gets wrong."""
    client.srem("$Class:MigMemory", facts["by_id"][2])
    client.sadd("$Class:MigMemory", "MigMemory:valor:" + "f" * 32 + ":ai")


def _config(tmp_path, pg, rdb, content, **kwargs):
    defaults = dict(
        rdb_path=rdb,
        run_dir=tmp_path / "run",
        source_id="laptop-a",
        mappings=fx.MAPPINGS,
        content_dir=content,
        postgres_dsn=pg.dsn,
        postgres_schema=pg.schema,
        redis_server=REDIS_SERVER,
        batch_size=4,
        operator="pytest",
    )
    defaults.update(kwargs)
    return MigrationConfig(**defaults)


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    fx.PROVIDER.calls = []
    for model in fx.MODELS:
        model._meta.backend = None
    yield
    mig._crash_hook = None


@pytest.fixture
def started(monkeypatch):
    """Every ThrowawayRedis the tool starts, to prove it is gone afterwards."""
    servers = []
    real_start = ThrowawayRedis.start

    def start(self):
        servers.append(self)
        real_start(self)

    monkeypatch.setattr(ThrowawayRedis, "start", start)
    return servers


def _gone(server) -> bool:
    if server.pid is None:
        return True
    try:
        os.kill(server.pid, 0)
    except ProcessLookupError:
        return True
    # A zombie of an already-reaped child cannot exist (wait() reaped it);
    # anything still answering is a leak.
    return False


def _rows(admin, schema, table, *cols):
    columns = ", ".join(f'"{c}"' for c in cols)
    return {
        row[0]: row[1:]
        for row in admin.execute(
            f'SELECT "_pk", {columns} FROM "{schema}"."{table}" ORDER BY "_pk"'
        ).fetchall()
    }


# -- end to end ---------------------------------------------------------------------

EXPECTED_LOSSY = {
    # Every record's creation and last-update time is an estimate (Redis
    # stores neither); MigTtl records have no evidence at all.
    "created_at_estimated": 15,
    "updated_at_estimated": 15,
    "created_at_from_snapshot_time": 2,
    "updated_at_from_snapshot_time": 2,
    # Postgres keeps no confirmed access log; the 3 confirmed reads' counters
    # cross, their timestamps do not.
    "access_log_entries_dropped": 3,
    "staged_reads_carried": 2,
    # Re-embedded by the backfill after cutover.
    "embedding_file_missing_reembed": 1,
    "embedding_dimension_mismatch_reembed": 1,
    # Gap-list families.
    "per_record_ttl_not_carried": 2,
    # Each MigLongTail record's declared cycle carries a baseline slot,
    # which is deployment-local and dropped on import (#698).
    "cycle_baselines_dropped": 2,
    "frequency_sketch_keys_reset": 1,
    "write_filter_priority_keys_not_stored": 1,
    # SMEMBERS-blind state the raw inventory accounts for.
    "orphan_hashes_recovered": 1,
    "class_members_without_hash": 1,
}


def test_a_snapshot_migrates_with_a_clean_signed_report(
    tmp_path, monkeypatch, pg, admin, started
):
    rdb, content, facts = build_snapshot(
        tmp_path, monkeypatch, mutate=_orphan_and_dangling
    )
    fx.PROVIDER.calls = []  # seeding embedded; the migration must not
    report = run_migration(_config(tmp_path, pg, rdb, content))

    assert report.verdict == "clean", report.summary()
    lossy = dict(report.lossy)
    streams = lossy.pop("event_stream_entries_not_carried")
    assert streams > 0
    assert lossy == EXPECTED_LOSSY, report.summary()

    data = report.data
    assert data["models"]["MigMemory"]["decisions"] == {"inserted": 11}
    assert data["models"]["MigLongTail"]["decisions"] == {"inserted": 2}
    assert data["models"]["MigMemory"]["sentinels"] == {"dismissal-prune": 1}
    checks = data["verification"]["MigMemory"]
    assert checks["records"]["compared"] == 11
    assert checks["decay_order:relevance"]["partitions"] == 2
    assert checks["bm25:bm25"]["mode"] == "strict"
    assert checks["bm25:bm25"]["queries"] > 0
    assert data["verification"]["MigLongTail"]["records"]["compared"] == 2

    # Signed off, and both artifacts written.
    run_dir = tmp_path / "run"
    on_disk = json.loads((run_dir / "report.json").read_text())
    assert mig.verify_report(on_disk)
    assert "hmac_sha256" not in on_disk["sign_off"]
    assert "CLEAN" in (run_dir / "report.txt").read_text()
    for name in ("inventory.json", "run.json"):
        assert (run_dir / name).exists()

    # Provenance and estimates on every row; nothing embedded on the way in.
    rows = _rows(
        admin,
        pg.schema,
        "mig_memory",
        "_migrated_from",
        "_estimated_fields",
        "_staged_reads",
        "embedding__vec",
        "embedding__hash",
    )
    assert set(rows) == set(facts["memory_keys"])
    for key, (provenance, estimated, staged, vec, digest) in rows.items():
        assert provenance["source"] == "laptop-a"
        assert provenance["run_id"] == report.run_id
        assert estimated == ["_created_at", "_updated_at"]
        if key in (facts["missing_npy_key"], facts["wrong_dim_key"]):
            assert vec is None
        else:
            assert vec is not None and digest is not None
    assert rows[facts["staged_key"]][2] == 2
    assert fx.PROVIDER.calls == []

    # The throwaway is gone, and popoto is bound back to the scratch DB.
    assert started and all(_gone(s) and s.directory is None for s in started)
    assert popoto.get_redis().connection_pool.connection_kwargs["db"] not in (0, None)


def test_a_rerun_of_the_same_snapshot_changes_nothing(tmp_path, monkeypatch, pg, admin):
    rdb, content, _ = build_snapshot(tmp_path, monkeypatch)
    first = run_migration(_config(tmp_path, pg, rdb, content))
    before = _rows(admin, pg.schema, "mig_memory", "_migrated_from", "_updated_at")
    again = run_migration(
        _config(tmp_path, pg, rdb, content, run_dir=tmp_path / "run2", merge=True)
    )
    assert first.clean and again.clean, again.summary()
    assert again.data["models"]["MigMemory"]["decisions"] == {"unchanged": 11}
    assert (
        _rows(admin, pg.schema, "mig_memory", "_migrated_from", "_updated_at") == before
    )


def test_an_interrupted_load_resumes_and_converges(
    tmp_path, monkeypatch, pg, admin, started
):
    rdb, content, facts = build_snapshot(tmp_path, monkeypatch)

    def crash(model, batch):
        if model == "MigMemory" and batch == 1:
            raise RuntimeError("power cut between the import and its provenance")

    mig._crash_hook = crash
    with pytest.raises(RuntimeError, match="power cut"):
        run_migration(_config(tmp_path, pg, rdb, content))
    mig._crash_hook = None
    assert started and all(_gone(s) for s in started)

    # The crashed batch landed through popoto's save (so _migrated_from is
    # NULL, like a native row) but its ledger rows are pending.
    rows = _rows(admin, pg.schema, "mig_memory", "_migrated_from")
    half_written = [k for k, (mf,) in rows.items() if mf is None]
    assert len(half_written) == 4
    pending = admin.execute(
        f'SELECT count(*) FROM "{pg.schema}".popoto_migration_ledger '
        "WHERE state = 'pending'"
    ).fetchone()[0]
    assert pending == 4

    # Without --resume the half-written schema is refused, never merged.
    with pytest.raises(mig.MigrationRefused):
        run_migration(_config(tmp_path, pg, rdb, content))

    report = run_migration(_config(tmp_path, pg, rdb, content, resume=True))
    assert report.clean, report.summary()
    decisions = report.data["models"]["MigMemory"]["decisions"]
    assert decisions == {"inserted": 7, "resumed": 4}
    rows = _rows(admin, pg.schema, "mig_memory", "_migrated_from")
    assert all(mf is not None for (mf,) in rows.values())
    assert set(rows) == set(facts["memory_keys"])


def test_two_stores_merge_by_the_reconciliation_rule(
    tmp_path, monkeypatch, pg, pg_schema, admin
):
    # One instant for both machines, so the memory synced to both really has
    # an equal payload.
    now = time.time()
    rdb_a, content_a, facts_a = build_snapshot(tmp_path, monkeypatch, name="a", now=now)
    rdb_b, content_b, facts_b = build_snapshot(
        tmp_path, monkeypatch, variant="b", name="b", now=now
    )
    first = run_migration(_config(tmp_path, pg, rdb_a, content_a, source_id="alpha"))
    assert first.clean, first.summary()

    # A row Valor wrote natively on Postgres after cutover, under a key the
    # second store also holds: it must survive the merge untouched.
    native_key = facts_b["by_id"][101]
    native = fx.MigMemory(
        agent_id="valor",
        memory_id=f"{101:032x}",
        project_key="ai",
        content="written natively on postgres",
    )
    with mig._no_embedding([fx.MigMemory]):
        native.save()

    second = run_migration(
        _config(
            tmp_path,
            pg,
            rdb_b,
            content_b,
            source_id="beta",
            run_dir=tmp_path / "run-b",
            mappings=[fx.VALOR_MEMORY_MAPPING],
            merge=True,
        )
    )
    decisions = second.data["models"]["MigMemory"]["decisions"]
    assert decisions == {
        "conflict_native": 1,
        "deduplicated": 1,
        "inserted": 8,
        "rejected": 2,
        "won_merge": 1,
    }, second.summary()
    assert second.lossy["rejected_nul_bytes"] == 1
    assert second.lossy["rejected_id_pattern"] == 1
    # Corpora differ (two stores in one table), so BM25 compares match sets
    # and every strict check is clean.
    checks = second.data["verification"]["MigMemory"]
    assert checks["bm25:bm25"]["mode"].startswith("accepted subset")
    assert second.clean, second.summary()

    rows = _rows(admin, pg.schema, "mig_memory", "_migrated_from", "content")
    shared_equal = facts_a["by_id"][0x10]
    shared_diff = facts_a["by_id"][0x11]
    # Equal payload: deduplicated, the second source recorded.
    assert rows[shared_equal][0]["source"] == "alpha"
    assert [d["source"] for d in rows[shared_equal][0]["duplicates"]] == ["beta"]
    # Differing payload: the later _updated_at (beta's, 6 days old) wins, and
    # the loser is logged.
    assert rows[shared_diff][0]["source"] == "beta"
    assert rows[shared_diff][1] == "shared memory edited on machine b"
    assert [loser["source"] for loser in rows[shared_diff][0]["losers"]] == ["alpha"]
    # The native row is untouched; rejected records never landed.
    assert rows[native_key][0] is None
    assert rows[native_key][1] == "written natively on postgres"
    for key in facts_b["rejected_keys"]:
        assert key not in rows
    # The source id is provenance only: never part of a key or a scope.
    assert not any("alpha" in k or "beta" in k for k in rows)


# -- refusals -----------------------------------------------------------------------


def _no_commands(client, monkeypatch):
    def refuse(*args, **kwargs):  # pragma: no cover - reaching it is the failure
        raise AssertionError(f"a command reached the server: {args}")

    monkeypatch.setattr(client, "execute_command", refuse)


def test_a_client_bound_to_a_live_server_is_refused_before_any_command(
    tmp_path, monkeypatch
):
    rdb, _, _ = build_snapshot(tmp_path, monkeypatch)
    live = _scratch_client()
    kwargs = live.connection_pool.connection_kwargs
    db0 = popoto.redis_db.GuardedRedis(host=kwargs["host"], port=kwargs["port"], db=0)
    with ThrowawayRedis(rdb, redis_server=REDIS_SERVER, work_parent=tmp_path) as server:
        for client in (live, db0):
            _no_commands(client, monkeypatch)
            with pytest.raises(LiveRedisRefused, match="never reads a live store"):
                assert_bound_to_throwaway(client, server)
    assert not server.running and server.directory is None


def test_the_cli_has_no_source_url_option(capsys):
    for flag in ("--redis-url", "--source-url", "--url", "--host", "--port"):
        with pytest.raises(SystemExit) as exc:
            mig.main(
                [
                    "--rdb",
                    "x.rdb",
                    "--run-dir",
                    "r",
                    "--source-id",
                    "s",
                    flag,
                    "redis://127.0.0.1:1/0",
                ]
            )
        assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_a_non_empty_target_is_refused_without_merge(
    tmp_path, monkeypatch, pg, admin, started
):
    rdb, content, _ = build_snapshot(tmp_path, monkeypatch)
    with mig._no_embedding([fx.MigMemory]):
        fx.MigMemory(
            agent_id="valor", memory_id="a" * 32, project_key="ai", content="native"
        ).save()
    with pytest.raises(TargetNotEmpty, match="--merge"):
        run_migration(_config(tmp_path, pg, rdb, content))
    assert started == []  # refused before the snapshot was even served
    assert (
        admin.execute(f'SELECT count(*) FROM "{pg.schema}".mig_memory').fetchone()[0]
        == 1
    )


def test_an_expected_empty_family_stops_the_run_before_any_write(
    tmp_path, monkeypatch, pg, admin, started
):
    def tombstones(client, facts):
        _orphan_and_dangling(client, facts)
        client.hset("$TOMBPRIOR:MigMemory:burials", "x", 1)

    rdb, content, _ = build_snapshot(tmp_path, monkeypatch, mutate=tombstones)
    with pytest.raises(InventoryStop, match="expected-empty") as exc:
        run_migration(_config(tmp_path, pg, rdb, content))
    assert any("$TOMBPRIOR" in r for r in exc.value.reasons)
    assert all(_gone(s) for s in started)
    tables = {
        r[0]
        for r in admin.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s", (pg.schema,)
        ).fetchall()
    }
    assert "mig_memory" not in tables
    inventory = json.loads((tmp_path / "run" / "inventory.json").read_text())
    families = inventory["models"]["MigMemory"]["families"]
    assert families["$TOMBPRIOR"]["disposition"] == "expected_empty"
    assert inventory["models"]["MigMemory"]["orphan_hashes"] == [
        fx.MigMemory._meta.db_class_key.redis_key + ":valor:" + f"{2:032x}" + ":ai"
    ]


WRITE_COMMANDS = [
    ("SET", "k", "v"),
    ("HSET", "h", "f", "v"),
    ("DEL", "k"),
    ("SREM", "$Class:MigMemory", "x"),
    ("ZREM", "z", "m"),
    ("EXPIRE", "k", "1"),
    ("RPUSH", "l", "x"),
    ("FLUSHDB",),
    ("FLUSHALL",),
    ("CONFIG", "SET", "save", "1 1"),
    ("SHUTDOWN", "NOSAVE"),
    ("EVAL", "return 1", "0"),
    ("EVALSHA", "0" * 40, "0"),
]


@pytest.mark.parametrize("command", WRITE_COMMANDS, ids=[c[0] for c in WRITE_COMMANDS])
def test_the_read_only_client_never_sends_a_write(tmp_path, command):
    empty = tmp_path / "empty"
    empty.mkdir()
    fixture = _FixtureServer(empty)
    fixture.client.set("seed", "1")
    rdb = fixture.save_and_stop()
    with ThrowawayRedis(rdb, redis_server=REDIS_SERVER, work_parent=tmp_path) as server:
        client = ReadOnlyRedis(server)
        with pytest.raises(ForbiddenCommand):
            client.execute_command(*command)
        with pytest.raises(ForbiddenCommand):
            pipe = client.pipeline()
            pipe.execute_command(*command)
        stats = server.client().info("commandstats")
        assert f"cmdstat_{command[0].lower()}" not in stats
        assert client.get("seed") == b"1"
        client.close()


def test_the_read_only_client_only_attaches_to_its_own_server(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    rdb = _FixtureServer(empty).save_and_stop()
    server = ThrowawayRedis(rdb, redis_server=REDIS_SERVER, work_parent=tmp_path)
    with pytest.raises(LiveRedisRefused):
        ReadOnlyRedis(server)  # not started
    with server:
        server.run_id = "0" * 40  # as if another process held the port
        with pytest.raises(LiveRedisRefused, match="run_id"):
            ReadOnlyRedis(server)


def test_a_dry_run_reads_and_transforms_but_writes_nothing(
    tmp_path, monkeypatch, pg, admin
):
    rdb, content, _ = build_snapshot(tmp_path, monkeypatch)
    report = run_migration(_config(tmp_path, pg, rdb, content, dry_run=True))
    assert report.verdict == "dry-run"
    assert report.lossy["created_at_estimated"] == 15
    assert (tmp_path / "run" / "transform" / "MigMemory.jsonl").exists()
    tables = admin.execute(
        "SELECT count(*) FROM pg_tables WHERE schemaname = %s", (pg.schema,)
    ).fetchone()[0]
    assert tables == 0


def test_the_report_summary_names_every_lossy_count():
    data = {
        "verdict": "clean",
        "run_id": "r",
        "source_id": "s",
        "snapshot": {"rdb_sha256": "0" * 64},
        "target": {"schema": "popoto"},
        "mode": "load",
        "models": {"MigMemory": {"exported": 2, "decisions": {"inserted": 2}}},
        "lossy": {"access_log_entries_dropped": 3, "created_at_estimated": 2},
        "sign_off": {"operator": "op", "at": "now", "sha256": "abc"},
    }
    text = mig.render_summary(data)
    assert "access_log_entries_dropped: 3" in text
    assert "created_at_estimated: 2" in text
    assert "CLEAN" in text and "Signed off by op" in text


def test_verification_catches_a_row_that_did_not_land_as_exported(
    tmp_path, monkeypatch, pg, admin
):
    """Non-vacuity: a value and a confidence state changed on Postgres after
    the import are both named in the report, and the verdict is not clean."""
    from popoto.transfer import import_ as import_module

    rdb, content, facts = build_snapshot(tmp_path, monkeypatch)
    target = facts["sentinel_key"]  # ranked last in its partition
    real = import_module.import_records

    def corrupting(model, stream, **kwargs):
        report = real(model, stream, **kwargs)
        if model is fx.MigMemory:
            admin.execute(
                f'UPDATE "{pg.schema}".mig_memory SET "importance" = 99.0, '
                '"confidence__conf" = 0.123 WHERE "_pk" = %s',
                (target,),
            )
            admin.commit()
        return report

    monkeypatch.setattr(import_module, "import_records", corrupting)
    report = run_migration(_config(tmp_path, pg, rdb, content))
    assert report.verdict == "mismatch"
    records = report.data["verification"]["MigMemory"]["records"]
    assert records["mismatched"] == 1
    (detail,) = records["first_mismatches"]
    assert detail["key"] == target
    assert set(detail["parts"]) == {"values.importance", "state.confidence"}
    # The importance is the decay base score: the ranking check sees it too.
    decay = report.data["verification"]["MigMemory"]["decay_order:relevance"]
    assert not decay["ok"]
    assert "MISMATCH" in report.summary()


def test_the_cli_runs_a_mapping_end_to_end(tmp_path, monkeypatch, pg, capsys):
    rdb, content, _ = build_snapshot(tmp_path, monkeypatch)
    monkeypatch.setenv("POPOTO_POSTGRES_URL", pg.dsn)
    monkeypatch.setenv("POPOTO_POSTGRES_SCHEMA", pg.schema)
    args = [
        "--rdb",
        str(rdb),
        "--run-dir",
        str(tmp_path / "cli-run"),
        "--source-id",
        "laptop-a",
        "--mapping",
        "tests.postgres.migrate_fixtures:MAPPINGS",
        "--redis-server",
        REDIS_SERVER,
    ]
    code = mig.main(args + ["--content-dir", str(content), "--operator", "cli-test"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "CLEAN" in out and "Signed off by cli-test" in out
    # A second run into the same directory without --resume is refused.
    assert mig.main(args) == 2
    assert "REFUSED" in capsys.readouterr().err


# -- #792 review: the CLI as documented ------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
#: Popoto's global client in a CLI subprocess is bound here, an address
#: nothing listens on: if the tool ever used it instead of its throwaway,
#: the run would fail loudly rather than read some other store.
UNREACHABLE_REDIS_URL = "redis://127.0.0.1:9/0"


def _cli_env(**extra):
    env = dict(os.environ)
    env.pop("POPOTO_TEST_DB", None)
    env["REDIS_URL"] = UNREACHABLE_REDIS_URL
    # The mapping module lives in the test tree, importable from the repo
    # root (``-m`` puts the cwd on sys.path; a script by path does not).
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if p
    )
    env.update(extra)
    return env


def test_the_documented_module_command_runs_a_mapping_end_to_end(
    tmp_path, monkeypatch, pg
):
    """The runbook's exact shape, ``python -m popoto.migrate_redis_to_postgres
    ... --mapping module:MAPPINGS``, in a real subprocess. Under ``-m`` the
    tool once ran as a second copy of itself, so every ModelMapping failed
    its isinstance check (exit 2); calling ``main()`` in-process hid it."""
    rdb, content, _ = build_snapshot(tmp_path, monkeypatch)
    command = [
        sys.executable,
        "-m",
        "popoto.migrate_redis_to_postgres",
        "--rdb",
        str(rdb),
        "--content-dir",
        str(content),
        "--run-dir",
        str(tmp_path / "cli-run"),
        "--source-id",
        "laptop-1",
        "--mapping",
        "tests.postgres.migrate_fixtures:MAPPINGS",
        "--redis-server",
        REDIS_SERVER,
        "--operator",
        "subprocess",
    ]
    done = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=_cli_env(POPOTO_POSTGRES_URL=pg.dsn, POPOTO_POSTGRES_SCHEMA=pg.schema),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "not a ModelMapping" not in done.stderr
    assert "CLEAN" in done.stdout and "Signed off by subprocess" in done.stdout
    report = json.loads((tmp_path / "cli-run" / "report.json").read_text())
    assert report["models"]["MigMemory"]["decisions"] == {"inserted": 11}
    assert report["snapshot"]["throwaway"]["tcp_port"] == 0


# -- #792 review: nothing is left serving after a kill ---------------------------

_STALL_DRIVER = """
import json, os, sys, time
from popoto import migrate_redis_to_postgres as mig

marker = sys.argv[1]
started = []
real_start = mig.ThrowawayRedis.start


def start(self):
    started.append(self)
    real_start(self)


def stall(client, mappings, content_dir):
    server = started[-1]
    facts = {
        "tool": os.getpid(),
        "server": server.pid,
        "watchdog": server.supervisor.pid,
        "socket": server.socket_path,
        "password": server.password,
        "dirs": [
            str(p)
            for p in (server.directory, server.socket_directory, server.work_parent)
            if p is not None
        ],
    }
    with open(marker + ".part", "w") as handle:
        json.dump(facts, handle)
    os.replace(marker + ".part", marker)
    while True:
        time.sleep(0.1)


mig.ThrowawayRedis.start = start
mig.run_inventory = stall
sys.exit(mig.main(sys.argv[2:]))
"""


def _alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _tcp_sockets(pid) -> str:
    done = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return done.stdout.strip()


@pytest.mark.parametrize(
    "signum",
    [signal.SIGKILL, signal.SIGHUP, signal.SIGINT, signal.SIGTERM],
    ids=["SIGKILL", "SIGHUP", "SIGINT", "SIGTERM"],
)
def test_a_killed_run_leaves_no_server_and_no_copy(tmp_path, monkeypatch, signum):
    """Kill the tool mid-run (stalled inside the inventory, with the snapshot
    loaded and served). Within seconds the throwaway server, its watchdog and
    every private copy are gone. While it ran, the server listened on no TCP
    port: only on a password-protected unix socket in a 0700 directory."""
    rdb, content, _ = build_snapshot(tmp_path, monkeypatch)
    driver = tmp_path / "driver.py"
    driver.write_text(_STALL_DRIVER)
    marker = tmp_path / "running.json"
    tool = subprocess.Popen(
        [
            sys.executable,
            str(driver),
            str(marker),
            "--rdb",
            str(rdb),
            "--content-dir",
            str(content),
            "--run-dir",
            str(tmp_path / "run"),
            "--source-id",
            "laptop-1",
            "--mapping",
            "tests.postgres.migrate_fixtures:MAPPINGS",
            "--redis-server",
            REDIS_SERVER,
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        env=_cli_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    facts = None
    try:
        deadline = time.monotonic() + 120
        while not marker.exists():
            assert tool.poll() is None, tool.stderr.read().decode()
            assert time.monotonic() < deadline, "the run never reached the inventory"
            time.sleep(0.05)
        facts = json.loads(marker.read_text())
        assert _alive(facts["server"]) and _alive(facts["watchdog"])
        assert all(Path(d).exists() for d in facts["dirs"])

        # Reachable only through the private socket, and only with the password.
        socket_dir = Path(facts["socket"]).parent
        assert stat.S_IMODE(socket_dir.stat().st_mode) == 0o700
        conf = Path(facts["dirs"][0]) / "redis.conf"
        assert stat.S_IMODE(conf.stat().st_mode) == 0o600
        probe = redis.Redis(unix_socket_path=facts["socket"])
        with pytest.raises(redis.AuthenticationError):
            probe.ping()
        probe.close()
        probe = redis.Redis(
            unix_socket_path=facts["socket"], password=facts["password"]
        )
        assert int(probe.info("server")["tcp_port"]) == 0
        probe.close()
        if shutil.which("lsof"):
            assert _tcp_sockets(facts["server"]) == ""
        if shutil.which("ps"):  # the password is never on a command line
            argv = subprocess.run(
                ["ps", "-ww", "-o", "command=", "-p", str(facts["server"])],
                capture_output=True,
                text=True,
            ).stdout
            assert argv and facts["password"] not in argv

        os.kill(tool.pid, signum)
        tool.wait(timeout=60)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and (
            _alive(facts["server"])
            or _alive(facts["watchdog"])
            or any(Path(d).exists() for d in facts["dirs"])
        ):
            time.sleep(0.1)
        assert not _alive(facts["server"]), "the throwaway server outlived the tool"
        assert not _alive(facts["watchdog"])
        assert not any(Path(d).exists() for d in facts["dirs"]), facts["dirs"]
        assert not Path(facts["socket"]).exists()
    finally:
        if tool.poll() is None:
            tool.kill()
            tool.wait()
        if facts is not None:
            for pid in (facts["server"], facts["watchdog"]):
                if _alive(pid):  # pragma: no cover - the failure path
                    os.kill(pid, signal.SIGKILL)


# -- #792 review: a merge never overwrites a native row ---------------------------


def _key_fields(key):
    _, agent_id, memory_id, project_key = key.split(":")
    return {"agent_id": agent_id, "memory_id": memory_id, "project_key": project_key}


def test_a_merge_never_adopts_another_runs_pending_rows(
    tmp_path, monkeypatch, pg, admin
):
    """The reviewer's sequence: run A crashes between import and provenance,
    popoto then saves one of those rows natively (importance 42), and a
    different run merges a store holding the same key. Another run's pending
    rows are foreign: the native value stays and the merge reports the
    conflict. A later resume of run A does not overwrite it either."""
    rdb, content, _ = build_snapshot(tmp_path, monkeypatch)

    def crash(model, batch):
        if model == "MigMemory" and batch == 1:
            raise RuntimeError("crash between import and provenance")

    mig._crash_hook = crash
    with pytest.raises(RuntimeError, match="crash between"):
        run_migration(_config(tmp_path, pg, rdb, content, source_id="laptop-a"))
    mig._crash_hook = None
    pending = sorted(
        r[0]
        for r in admin.execute(
            f'SELECT _pk FROM "{pg.schema}".popoto_migration_ledger '
            "WHERE state = 'pending' AND model = 'MigMemory'"
        ).fetchall()
    )
    assert len(pending) == 4
    target = pending[1]

    with mig._no_embedding([fx.MigMemory]):
        native = fx.MigMemory.query.get(**_key_fields(target))
        native.importance = 42.0
        native.save()

    merged = run_migration(
        _config(
            tmp_path,
            pg,
            rdb,
            content,
            source_id="laptop-z",
            run_dir=tmp_path / "run-z",
            merge=True,
        )
    )
    rows = _rows(admin, pg.schema, "mig_memory", "importance", "_migrated_from")
    assert rows[target] == (42.0, None), merged.summary()
    decisions = merged.data["models"]["MigMemory"]["decisions"]
    # Every row run A left pending looks native to laptop-z; none is adopted.
    assert decisions.get("conflict_native") == 4, merged.summary()
    assert "resumed" not in decisions
    (decision,) = admin.execute(
        f'SELECT decision FROM "{pg.schema}".popoto_migration_ledger '
        "WHERE run_id = %s AND _pk = %s",
        (merged.run_id, target),
    ).fetchone()
    assert decision == "conflict_native"
    # Run A's pending rows are still its own to resume.
    still = admin.execute(
        f'SELECT count(*) FROM "{pg.schema}".popoto_migration_ledger '
        "WHERE state = 'pending' AND run_id <> %s",
        (merged.run_id,),
    ).fetchone()[0]
    assert still == 4

    # Run A resumes: it adopts the three rows that still hold what it
    # imported, and leaves the natively saved one alone.
    resumed = run_migration(
        _config(
            tmp_path, pg, rdb, content, source_id="laptop-a", resume=True, merge=True
        )
    )
    rows = _rows(admin, pg.schema, "mig_memory", "importance", "_migrated_from")
    assert rows[target] == (42.0, None), resumed.summary()
    decisions = resumed.data["models"]["MigMemory"]["decisions"]
    assert decisions["resumed"] == 3 and decisions["conflict_native"] == 1
    assert resumed.clean, resumed.summary()


def test_a_second_concurrent_run_is_refused_cleanly(
    tmp_path, monkeypatch, pg, admin, started
):
    rdb, content, _ = build_snapshot(tmp_path, monkeypatch)
    lock = (mig._LOCK_CLASS, pg.schema)
    admin.execute("SELECT pg_advisory_lock(%s, hashtext(%s))", lock)
    try:
        with pytest.raises(mig.MigrationRefused, match="another migration run"):
            run_migration(_config(tmp_path, pg, rdb, content))
        assert started == []  # refused before the snapshot was served
    finally:
        admin.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", lock)
    report = run_migration(_config(tmp_path, pg, rdb, content, resume=True))
    assert report.clean, report.summary()
    # The run released its lock: a fresh session can take it.
    taken = admin.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s))", lock)
    assert taken.fetchone()[0]
    admin.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", lock)


# -- #792 review: non-blocking notes -----------------------------------------------


def test_edges_over_max_edges_are_counted_and_verify_clean(tmp_path, monkeypatch, pg):
    extra = 7

    def many_edges(client, facts):
        field = fx.MigLongTail._meta.fields["links"]
        key = field.get_edge_key(fx.MigLongTail, "MigLongTail:alpha")
        assert client.zcard(key) == 2  # non-vacuity: this is the edge zset
        client.zadd(
            key,
            {
                f"MigLongTail:extra-{i:02d}": 0.05
                for i in range(field.max_edges + extra - 2)
            },
        )

    rdb, content, _ = build_snapshot(tmp_path, monkeypatch, mutate=many_edges)
    report = run_migration(_config(tmp_path, pg, rdb, content))
    assert report.lossy["co_occurrence_edges_truncated"] == extra
    assert report.clean, report.summary()


def test_bm25_still_fails_when_some_records_are_rejected(
    tmp_path, monkeypatch, pg, admin
):
    """One rejected record makes the corpora differ, which used to turn the
    BM25 check informational (it could not fail). It now compares, per
    query, which of this source's records match on each side."""
    rdb, content, facts = build_snapshot(tmp_path, monkeypatch)
    reject_five = dataclasses.replace(
        fx.VALOR_MEMORY_MAPPING,
        id_patterns={"memory_id": r"^(?!0{31}5$)[0-9a-f]{32}$"},
    )
    mappings = [reject_five, *fx.MAPPINGS[1:]]
    clean = run_migration(_config(tmp_path, pg, rdb, content, mappings=mappings))
    bm25 = clean.data["verification"]["MigMemory"]["bm25:bm25"]
    assert clean.lossy["rejected_id_pattern"] == 1
    assert bm25["mode"].startswith("accepted subset"), clean.summary()
    assert bm25["ok"] and clean.clean, clean.summary()

    target = facts["by_id"][1]
    real = mig.verify

    def corrupt_then_verify(*args, **kwargs):
        admin.execute(
            f'DELETE FROM "{pg.schema}".mig_memory__bm25__post WHERE _pk = %s',
            (target,),
        )
        admin.commit()
        return real(*args, **kwargs)

    monkeypatch.setattr(mig, "verify", corrupt_then_verify)
    again = run_migration(
        _config(
            tmp_path,
            pg,
            rdb,
            content,
            mappings=mappings,
            run_dir=tmp_path / "run2",
            merge=True,
        )
    )
    bm25 = again.data["verification"]["MigMemory"]["bm25:bm25"]
    assert bm25["mode"].startswith("accepted subset")
    assert not bm25["ok"] and again.verdict == "mismatch", again.summary()
    assert any(target in m["only_redis"] for m in bm25["first_mismatches"])


@pytest.mark.parametrize(
    "column, corruption",
    [
        ("_created_at", '"_created_at" = to_timestamp(1)'),
        ("_updated_at", '"_updated_at" = "_updated_at" + interval \'1 hour\''),
        ("_estimated_fields", "\"_estimated_fields\" = ARRAY['_updated_at']"),
        ("embedding__hash", "\"embedding__hash\" = 'not-the-hash'"),
    ],
)
def test_verification_checks_the_columns_the_tool_writes(
    tmp_path, monkeypatch, pg, admin, column, corruption
):
    rdb, content, facts = build_snapshot(tmp_path, monkeypatch)
    target = facts["accessed_key"]  # it has a vector, so its hash is checked
    real = mig.verify

    def corrupt_then_verify(*args, **kwargs):
        admin.execute(
            f'UPDATE "{pg.schema}".mig_memory SET {corruption} WHERE "_pk" = %s',
            (target,),
        )
        admin.commit()
        return real(*args, **kwargs)

    monkeypatch.setattr(mig, "verify", corrupt_then_verify)
    report = run_migration(_config(tmp_path, pg, rdb, content))
    check = report.data["verification"]["MigMemory"]["tool_columns"]
    assert report.verdict == "mismatch", report.summary()
    assert not check["ok"] and check["mismatched"] == 1
    assert check["first_mismatches"] == [{"key": target, "parts": [column]}]


def test_a_rerun_records_each_duplicate_and_loser_once(
    tmp_path, monkeypatch, pg, admin
):
    """``_migrated_from.duplicates`` and ``.losers`` used to grow on every
    re-run of the same source (each entry carried its run_id). Re-running the
    same snapshot now leaves every row's provenance exactly as it was."""
    now = time.time()
    rdb_a, content_a, facts_a = build_snapshot(tmp_path, monkeypatch, name="a", now=now)
    rdb_b, content_b, _ = build_snapshot(
        tmp_path, monkeypatch, variant="b", name="b", now=now
    )
    memory_only = [fx.VALOR_MEMORY_MAPPING]
    first = run_migration(
        _config(
            tmp_path,
            pg,
            rdb_b,
            content_b,
            source_id="beta",
            run_dir=tmp_path / "run-b",
            mappings=memory_only,
        )
    )
    assert first.clean, first.summary()
    reports = []
    snapshots = []
    for attempt in (1, 2):
        reports.append(
            run_migration(
                _config(
                    tmp_path,
                    pg,
                    rdb_a,
                    content_a,
                    source_id="alpha",
                    run_dir=tmp_path / f"run-a{attempt}",
                    merge=True,
                    mappings=memory_only,
                )
            )
        )
        snapshots.append(_rows(admin, pg.schema, "mig_memory", "_migrated_from"))
    assert all(r.clean for r in reports), reports[-1].summary()
    before, after = snapshots
    shared_equal = before[facts_a["by_id"][0x10]][0]
    shared_diff = before[facts_a["by_id"][0x11]][0]
    assert [d["source"] for d in shared_equal["duplicates"]] == ["alpha"]
    assert [loser["source"] for loser in shared_diff["losers"]] == ["alpha"]
    assert after == before
