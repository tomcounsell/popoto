"""The harness integration on a Postgres-bound process (#814, ``[PG-only]``).

``tests/test_integrations_backends.py`` runs the service's contract on both
legs. What only holds on Postgres lives here: the whole loop -- capture,
assemble, suppression, the turn-keyed claim, feedback, search, status and
``doctor`` -- sends Redis nothing; the session tables expire with the session;
a turn is claimed exactly once under concurrency; ``doctor`` reports the
Postgres server instead of Redis; and the real ``popoto-memory`` hook and
``doctor`` subprocesses work with Redis unreachable and never dial it.
"""

import json
import logging
import threading
import time

import pytest

from popoto.backends import _swap_instance, set_backend
from popoto.backends.postgres import PostgresBackend, close_pools
from popoto.fields.constants import Defaults
from popoto.integrations import hooks
from popoto.integrations.cli import main
from popoto.integrations.config import MemoryConfig
from popoto.integrations.service import MemoryService
from tests import harness_e2e
from tests.postgres.test_postgres_recipes import _RedisRecorder

AGENT = "pg-harness"


def make_service(tmp_path, **overrides):
    options = dict(agent_id=AGENT, log_path=tmp_path / "memory.log")
    options.update(overrides)
    return MemoryService(MemoryConfig(**options))


class _Rec:
    def __init__(self, key):
        self.db_key = type("K", (), {"redis_key": key})()


UNREACHABLE = "postgresql://127.0.0.1:1/nowhere"


@pytest.fixture
def down(pg, monkeypatch):
    """A Postgres backend on a refused port, bound over ``pg`` for the test
    (as ``test_postgres_outage.py`` installs one), with a short timeout."""
    monkeypatch.setattr(Defaults, "PG_CONNECT_TIMEOUT_SECONDS", 0.3)
    backend = PostgresBackend(dsn=UNREACHABLE, schema=pg.schema)
    set_backend(backend)
    previous = _swap_instance("postgres", backend)
    try:
        yield backend
    finally:
        _swap_instance("postgres", previous)
        set_backend(pg)
        close_pools()


def _expire_all(admin, schema, table):
    admin.execute(f'UPDATE "{schema}"."{table}" SET expires_at = 0')


# -- zero Redis --------------------------------------------------------------------


def test_the_whole_loop_sends_redis_nothing(pg, tmp_path, monkeypatch, capsys):
    recorder = _RedisRecorder(monkeypatch)
    # The ambient REDIS_URL names a server; a Postgres-bound service must not
    # bind it, refuse database 0 on it, or probe it.
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    service = make_service(tmp_path)
    assert service.backend_name == "postgres"
    service.capture("Deploys are blue-green with automatic rollback", "s1")
    context = service.assemble("how do deploys roll back?", "s1", turn_id="t1")
    assert "blue-green" in context
    assert service.assemble("how do deploys roll back?", "s1", turn_id="t2") == ""
    assert service.feedback("s1", turn_id="t1") == 1
    assert service.feedback("s1", turn_id="t-none") == 0
    assert service.search("deploys")[0]["content"].startswith("Deploys")
    info = service.status()
    assert info["reachable"] is True, info["errors"]
    assert info["counters"]["capture_ok"] == 1
    assert info["counters"]["assemble_ok"] == 2
    monkeypatch.setenv("POPOTO_MEMORY_AGENT_ID", AGENT)
    monkeypatch.setenv("POPOTO_MEMORY_LOG", str(tmp_path / "memory.log"))
    assert main(["doctor"]) == 0
    assert main(["doctor", "--json"]) == 0
    assert recorder.calls == []
    assert "redis" not in capsys.readouterr().out.split("log            ")[0]


# -- doctor ------------------------------------------------------------------------


def test_doctor_reports_the_postgres_server(pg, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POPOTO_MEMORY_AGENT_ID", AGENT)
    monkeypatch.setenv("POPOTO_MEMORY_LOG", str(tmp_path / "memory.log"))
    make_service(tmp_path).capture("The staging database resets nightly", None)
    assert main(["doctor", "--no-latency"]) == 0
    out = capsys.readouterr().out
    assert "  backend        postgres\n" in out
    assert "dbname=" in out and "password" not in out
    assert "  url source     POPOTO_POSTGRES_URL\n" in out
    assert "  postgres       reachable, postgresql 18" in out
    assert f"  schema         {pg.schema} (" in out
    assert "  pgvector       " in out
    assert "  health         ok, dropped_writes=0\n" in out
    assert "  records        1\n" in out
    assert "  successes      capture=1\n" in out

    assert main(["doctor", "--json", "--no-latency"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["backend"] == "postgres"
    assert info["reachable"] is True
    assert info["postgres"]["schema"] == pg.schema
    assert info["postgres"]["schema_exists"] is True
    assert info["postgres"]["supported"] is True
    assert info["postgres"]["server_version"].startswith("18")
    assert info["postgres"]["health"]["ok"] is True
    # Present as null, never absent: a consumer that indexes them gets None,
    # not a KeyError.
    assert info["redis_url"] is None and info["redis_reachable"] is None
    assert info["server"].startswith("postgresql 18")


def test_doctor_fails_on_an_unreachable_server(down, tmp_path, monkeypatch, capsys):
    recorder = _RedisRecorder(monkeypatch)
    monkeypatch.setenv("POPOTO_MEMORY_LOG", str(tmp_path / "memory.log"))
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "  postgres       UNREACHABLE\n" in out
    assert "host=127.0.0.1 port=1 dbname=nowhere" in out
    assert "POPOTO_POSTGRES_URL=" in out
    assert main(["doctor", "--json"]) == 1
    info = json.loads(capsys.readouterr().out)
    assert info["reachable"] is False and info["errors"]
    assert info["redis_url"] is None and info["redis_reachable"] is None
    assert info["backend"] == "postgres" and info["server"] is None
    assert info["postgres"]["health"] is not None or info["postgres"]["dsn"]
    assert recorder.calls == []


def test_doctor_refuses_a_server_below_the_floor(pg, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POPOTO_MEMORY_LOG", str(tmp_path / "memory.log"))
    real = pg.describe

    def old_server():
        facts = real()
        facts.update(server_version="17.4", server_version_num=170004)
        facts["supported"] = False
        return facts

    monkeypatch.setattr(pg, "describe", old_server)
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "  postgres       UNSUPPORTED, postgresql 17.4\n" in out
    assert "PostgreSQL 18 or newer" in out


def test_describe_reports_server_schema_and_pgvector(pg, admin):
    facts = pg.describe()
    assert facts["server_version_num"] >= 180000
    assert facts["supported"] is True
    assert facts["schema"] == pg.schema
    assert facts["dsn"] == pg.dsn_summary and "password" not in facts["dsn"]
    (installed,) = admin.execute(
        "SELECT count(*) FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()
    assert (facts["pgvector"] is not None) == bool(installed)


# -- the hook's failure mode --------------------------------------------------------


def test_a_hook_on_an_unreachable_server_names_the_backend(
    down, tmp_path, monkeypatch, caplog
):
    recorder = _RedisRecorder(monkeypatch)
    service = make_service(tmp_path)
    with caplog.at_level(logging.WARNING, logger="POPOTO.integrations"):
        out = hooks.run(
            json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "anything",
                    "session_id": "s1",
                }
            ),
            service=service,
        )
    assert out is None
    assert "failed (backend: postgres)" in caplog.text
    assert recorder.calls == []
    # One outage short-circuits the rest of the process, as on Redis.
    assert service._redis_down is True


# -- session state on its tables ---------------------------------------------------


def test_the_session_state_expires_with_the_session(pg, admin, tmp_path):
    service = make_service(tmp_path)
    service._push_pending("s1", [_Rec("k:a")], turn_id="t1")
    service._mark_injected("s1", [_Rec("k:a")])
    assert service._injected_keys("s1") == {"k:a"}
    _expire_all(admin, pg.schema, "popoto_harness_pending")
    _expire_all(admin, pg.schema, "popoto_harness_injected")
    assert service._injected_keys("s1") is None
    assert service._pop_pending("s1", turn_id="t1") == []
    # A new mark after expiry starts a fresh set, as SADD on an expired key.
    service._mark_injected("s1", [_Rec("k:b")])
    assert service._injected_keys("s1") == {"k:b"}


def test_a_write_reaps_abandoned_sessions(pg, admin, tmp_path):
    service = make_service(tmp_path)
    for i in range(5):
        service._push_pending(f"dead-{i}", [_Rec("k")])
    _expire_all(admin, pg.schema, "popoto_harness_pending")
    service._push_pending("live", [_Rec("k")])
    (left,) = admin.execute(
        f'SELECT count(*) FROM "{pg.schema}".popoto_harness_pending'
    ).fetchone()
    assert left == 1


def test_a_turn_is_claimed_exactly_once_under_concurrency(pg, tmp_path):
    service = make_service(tmp_path)
    service._push_pending("s1", [_Rec("k:a")], turn_id="t1")
    results = []
    barrier = threading.Barrier(6)

    def claim():
        mine = make_service(tmp_path)
        barrier.wait()
        results.append(mine._pop_pending("s1", turn_id="t1"))

    threads = [threading.Thread(target=claim) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results, key=len) == [[]] * 5 + [["k:a"]]


def test_counters_share_the_eviction_counter_table(pg, tmp_path):
    """The DefaultMemory eviction counter and the integration counters are
    one prefix in ``popoto_counter``, so doctor reads both, as on Redis."""
    from popoto import counters
    from popoto.recipes import DefaultMemory

    service = make_service(tmp_path)
    counters.increment(
        f"$popoto_memory:counter:{AGENT}:evicted", 3, model=DefaultMemory
    )
    service.capture("one fact", None)
    assert service.status()["counters"] == {"evicted": 3, "capture_ok": 1}


# -- end to end: the real console script, Redis unreachable --------------------------


def _pg_harness(tmp_path, pg_schema, url=None):
    return harness_e2e.Harness(
        tmp_path,
        {
            "POPOTO_BACKEND": "postgres",
            "POPOTO_POSTGRES_URL": url or pg_schema.url,
            "POPOTO_POSTGRES_SCHEMA": pg_schema.name,
            # Unreachable on purpose: a single dial would fail loudly.
            "REDIS_URL": "redis://127.0.0.1:1/15",
        },
        agent="e2e-pg",
    )


def test_the_hook_and_doctor_run_on_postgres_with_redis_unreachable(
    pg, pg_schema, admin, tmp_path
):
    h = _pg_harness(tmp_path, pg_schema)
    turns = harness_e2e.drive_turns(h)
    for run in turns["runs"]:
        assert run.returncode == 0, run.stderr
        assert "failed" not in run.stderr, run.stderr
        # Start to exit. psycopg_pool's threads once held every process for
        # ~20 s in interpreter shutdown (5 s per thread), after the hook's
        # work was done; atexit now closes the pools first.
        assert run.elapsed < 4.0, f"hook took {run.elapsed:.1f}s to exit"
        assert "couldn't stop thread" not in run.stderr, run.stderr
    assert turns["context"] is not None
    assert harness_e2e.DEPLOY_FACT in turns["context"]

    doctor = h.run(["doctor", "--json"])
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    info = json.loads(doctor.stdout)
    assert info["backend"] == "postgres"
    assert info["reachable"] is True
    assert info["record_count"] == 2
    # Rendered before doctor's own latency probe assembles.
    assert info["counters"] == {"capture_ok": 2, "assemble_ok": 1}
    assert set(info["last_success"]) == {"capture", "assemble"}

    text = h.run(["doctor"])
    assert text.returncode == 0, text.stdout
    assert "  backend        postgres\n" in text.stdout
    assert "  postgres       reachable, postgresql 18" in text.stdout

    (rows,) = admin.execute(
        f'SELECT count(*) FROM "{pg_schema.name}".default_memory '
        "WHERE agent_id = 'e2e-pg'"
    ).fetchone()
    assert rows == 2
    assert h.redis_attempts() == ""


def test_the_hook_survives_postgres_down_and_says_which_backend(
    pg, pg_schema, tmp_path
):
    h = _pg_harness(tmp_path, pg_schema, url=UNREACHABLE)
    started = time.monotonic()
    run = h.hook(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s",
            "prompt": "how do deploys roll back?",
        }
    )
    elapsed = time.monotonic() - started
    assert run.returncode == 0
    # The hook caps the Postgres connect wait at its 1 s budget; the library
    # default (Defaults.PG_CONNECT_TIMEOUT_SECONDS, 5 s) alone exceeds this.
    assert elapsed < 4.5, f"{elapsed:.1f}s on the prompt path"
    assert run.stdout == ""
    assert "(backend: postgres)" in run.stderr
    doctor = h.run(["doctor"])
    assert doctor.returncode == 1
    assert "  postgres       UNREACHABLE\n" in doctor.stdout
    assert h.redis_attempts() == ""


def test_the_hook_fails_open_within_its_budget_on_a_silent_postgres(
    pg, pg_schema, tmp_path
):
    """A server that accepts TCP and never answers holds libpq for the whole
    ``connect_timeout``, unlike a refused port, which fails in microseconds
    and so cannot tell the hook's 1 s cap from the 5 s library default.

    The lower bound proves the listener really made the hook wait; the upper
    bound proves the hook's cap (``HOOK_SOCKET_TIMEOUT_SECONDS``), not
    ``Defaults.PG_CONNECT_TIMEOUT_SECONDS`` (5 s), set that wait.
    """
    import socket

    silent = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    silent.bind(("127.0.0.1", 0))
    silent.listen(64)  # never accept(): the kernel completes the handshake
    port = silent.getsockname()[1]
    try:
        h = _pg_harness(tmp_path, pg_schema, url=f"postgresql://127.0.0.1:{port}/x")
        started = time.monotonic()
        run = h.hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s",
                "prompt": "how do deploys roll back?",
            }
        )
        elapsed = time.monotonic() - started
    finally:
        silent.close()
    assert run.returncode == 0
    assert run.stdout == ""
    assert "(backend: postgres)" in run.stderr
    assert elapsed >= 0.9, f"{elapsed:.2f}s: the silent server never held the hook"
    assert elapsed < 4.5, f"{elapsed:.1f}s on the prompt path"
    assert h.redis_attempts() == ""


def test_a_positional_pop_waits_for_a_held_session_rather_than_missing(
    pg, admin, tmp_path
):
    """While another transaction holds the session's entries -- a concurrent
    push's expiry refresh row-locks every live one -- the pop waits and returns
    the entry. A ``SKIP LOCKED`` pop returned nothing here, leaving the turn's
    entry for the next turn to claim (the #574 misattribution)."""
    service = make_service(tmp_path)
    service._push_pending("s1", [_Rec("k:a")])
    result = []
    popper = threading.Thread(
        target=lambda: result.append(make_service(tmp_path)._pop_pending("s1"))
    )
    with admin.transaction():
        admin.execute(
            f'SELECT seq FROM "{pg.schema}".popoto_harness_pending '
            "WHERE agent = %s AND session = %s FOR UPDATE",
            [AGENT, "s1"],
        )
        popper.start()
        popper.join(0.5)
        waited = popper.is_alive()
    popper.join(5)
    assert waited, "the pop did not wait for the held entry"
    assert result == [["k:a"]]


def test_the_hook_fails_open_within_its_budget_on_a_locked_table(
    pg, pg_schema, admin, tmp_path
):
    """A reachable server that never answers a statement (here, a table held
    under ``ACCESS EXCLUSIVE``) is bounded by the hook's statement-timeout cap,
    not by the library default of no timeout at all."""
    h = _pg_harness(tmp_path, pg_schema)
    first = harness_e2e.drive_turns(h)
    assert all(run.returncode == 0 for run in first["runs"])
    with admin.transaction():
        admin.execute(
            f'LOCK TABLE "{pg_schema.name}".popoto_harness_injected '
            "IN ACCESS EXCLUSIVE MODE"
        )
        started = time.monotonic()
        run = h.hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "e2e-session",
                "prompt_id": "p2",
                "prompt": "how do deploys roll back?",
            }
        )
        elapsed = time.monotonic() - started
    assert run.returncode == 0, run.stderr
    assert elapsed >= 0.9, f"{elapsed:.2f}s: the lock never held the hook"
    assert elapsed < 4.5, f"{elapsed:.1f}s on the prompt path"
    assert "(backend: postgres)" in run.stderr
    assert h.redis_attempts() == ""


def test_doctor_creates_nothing_in_a_schema_that_does_not_exist(
    pg, pg_schema, admin, tmp_path
):
    """``doctor`` pointed at an unused schema reports it absent and leaves it
    absent: a model read would otherwise create the schema and its tables,
    and the next doctor run would report them present."""
    fresh = f"{pg_schema.name}_fresh"
    admin.execute(f'DROP SCHEMA IF EXISTS "{fresh}" CASCADE')
    h = harness_e2e.Harness(
        tmp_path,
        {
            "POPOTO_BACKEND": "postgres",
            "POPOTO_POSTGRES_URL": pg_schema.url,
            "POPOTO_POSTGRES_SCHEMA": fresh,
            "REDIS_URL": "redis://127.0.0.1:1/15",
        },
        agent="e2e-pg",
    )

    def present():
        (exists,) = admin.execute(
            "SELECT to_regnamespace(%s) IS NOT NULL", [fresh]
        ).fetchone()
        return exists

    try:
        doctor = h.run(["doctor", "--json"])
        info = json.loads(doctor.stdout)
        assert info["reachable"] is True
        assert info["postgres"]["schema_exists"] is False
        assert info["record_count"] == 0
        assert present() is False
        h.run(["doctor"])  # the text report, with its latency probe
        assert present() is False
        assert h.redis_attempts() == ""
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{fresh}" CASCADE')
