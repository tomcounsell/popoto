"""Database 0 must stay untouched when POPOTO_MEMORY_URL points elsewhere.

This is the invariant that a real bug in this package already violated once.
The first `bind_connection` replaced `redis_db.POPOTO_REDIS_DB` with a new
client, but most of Popoto imports that object at module load, so those
references kept the old target and every hook write landed in database 0 --
the developer's real data -- while `doctor` cheerfully reported the
configured database as healthy.

Two properties are asserted here, and neither was covered before:

1. A full read-and-write hook cycle in a subprocess, with an explicit
   `POPOTO_MEMORY_URL`, leaves database 0's `DBSIZE` exactly unchanged.
2. Constructing a `MemoryService` is what binds the connection. That is the
   single chokepoint, so the paths that do not go through the CLI --
   `popoto-memory demo`, `examples/harness_memory/seed.py`,
   `examples/harness_memory/verify.py`, `plugins/hermes/handler.py` -- are
   covered by construction rather than by remembering to call it.

**These tests deliberately do not use the pytest plugin's DB-0 tripwire.**
That tripwire disables itself when database 0 is non-idle
(`pytest_plugin.py`, "DB 0 is non-idle ... tripwire SKIPPED"), which is to
say it is dead on exactly the machines that have something to lose.

The guard compares the *set of keys this integration could have written*
before and after, rather than `DBSIZE`. A raw count is not usable here: a
developer's database 0 is a live database, and it was measured drifting by
three keys in sixty idle seconds on the machine this was written on, which
would make a count-based assertion flake for reasons that have nothing to do
with Popoto. Matching on our own key signatures is both drift-immune and a
more direct statement of the invariant.

Nothing here ever writes to database 0. It is opened read-only, for `SCAN`,
and only to prove that nothing of ours appeared in it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import redis

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = Path(SCRIPT_DIR).parent
sys.path.append(str(REPO_ROOT))

from popoto.redis_db import POPOTO_REDIS_DB, sibling_client_kwargs  # noqa: E402

FIXTURES = Path(SCRIPT_DIR) / "fixtures" / "harness_payloads"
AGENT = "test-db0-isolation"

TEST_DB = os.environ.get("POPOTO_TEST_DB") or "15"
"""``or``, not a ``get`` default: an explicitly-empty ``POPOTO_TEST_DB=``
would otherwise yield ``""`` and build ``redis://localhost:6379/``, a URL
whose database ``parse_url`` drops -- resolving to database 0, which is the
precise accident this module exists to catch."""

MEMORY_URL = f"redis://localhost:6379/{TEST_DB}"


def _db0():
    """A read-only sibling client on database 0, on the same host and auth."""
    return redis.Redis(
        **sibling_client_kwargs(POPOTO_REDIS_DB.connection_pool.connection_kwargs, db=0)
    )


OUR_KEY_PATTERNS = (
    "*DefaultMemory*",
    "$popoto_memory:*",
    f"*{AGENT}*",
)
"""Every key shape this integration can create: the memory model's records
and all its index, BM25, confidence and registry keys; the counter and
pending-handoff keys; and anything carrying this module's agent id."""


def _our_keys(client):
    found = set()
    for pattern in OUR_KEY_PATTERNS:
        for key in client.scan_iter(match=pattern, count=1000):
            found.add(key if isinstance(key, bytes) else key.encode())
    return found


@pytest.fixture
def db0_unchanged():
    """Fail if anything this integration could write appears in database 0."""
    client = _db0()
    try:
        before = _our_keys(client)
    except Exception as exc:  # pragma: no cover - no server, nothing to guard
        pytest.skip(f"cannot reach database 0 to guard it: {exc}")
    yield
    added = _our_keys(client) - before
    assert not added, (
        f"{len(added)} Popoto key(s) appeared in database 0 while "
        f"POPOTO_MEMORY_URL pointed at {MEMORY_URL}: "
        f"{sorted(k.decode('utf-8', 'replace') for k in added)[:10]}. "
        "Something in the integration is writing to the default connection "
        "instead of the configured one."
    )


def _env(**extra):
    env = dict(os.environ)
    env.pop("POPOTO_TEST_DB", None)
    env["POPOTO_MEMORY_URL"] = MEMORY_URL
    env["POPOTO_MEMORY_AGENT_ID"] = AGENT
    env.update(extra)
    return env


def _run(argv, env, stdin=None):
    return subprocess.run(
        [sys.executable, *argv],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=180,
    )


@pytest.fixture(autouse=True)
def purge_test_agent():
    yield
    from popoto.recipes import DefaultMemory

    target = redis.Redis(
        **sibling_client_kwargs(
            POPOTO_REDIS_DB.connection_pool.connection_kwargs, db=int(TEST_DB)
        )
    )
    for pattern in (f"*{AGENT}*", f"*{DefaultMemory.__name__}*{AGENT}*"):
        for key in target.scan_iter(match=pattern, count=500):
            target.delete(key)


# --- the invariant ---------------------------------------------------------


def test_full_hook_cycle_leaves_db0_untouched(db0_unchanged, tmp_path):
    """Write then read, in subprocesses, with an explicit memory URL."""
    env = _env(POPOTO_MEMORY_LOG=str(tmp_path / "memory.log"))

    write = _run(
        ["-m", "popoto.integrations.cli", "hook"],
        env,
        stdin=(FIXTURES / "claude_code_stop.json").read_text(),
    )
    assert write.returncode == 0, write.stderr

    read = _run(
        ["-m", "popoto.integrations.cli", "hook"],
        env,
        stdin=(FIXTURES / "claude_code_user_prompt_submit.json").read_text(),
    )
    assert read.returncode == 0, read.stderr
    # The cycle must actually have done work, or the guard is vacuous.
    assert "additionalContext" in read.stdout


def test_the_zero_key_example_leaves_db0_untouched(db0_unchanged, tmp_path):
    """`examples/harness_memory/verify.py` was one of the leaking paths."""
    result = _run(
        [str(REPO_ROOT / "examples" / "harness_memory" / "verify.py")],
        _env(POPOTO_MEMORY_LOG=str(tmp_path / "memory.log")),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All checks passed" in result.stdout


def test_the_demo_leaves_db0_untouched(db0_unchanged, tmp_path):
    """`popoto-memory demo` was another."""
    result = _run(
        ["-m", "popoto.integrations.cli", "demo"],
        _env(POPOTO_MEMORY_LOG=str(tmp_path / "memory.log")),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_seed_example_leaves_db0_untouched(db0_unchanged, tmp_path):
    """And `seed.py` was the third."""
    result = _run(
        [
            str(REPO_ROOT / "examples" / "harness_memory" / "seed.py"),
            "--agent-id",
            AGENT,
        ],
        _env(POPOTO_MEMORY_LOG=str(tmp_path / "memory.log")),
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- the chokepoint that makes the above true --------------------------------


def _resolved_db(script, env):
    result = _run(["-c", script], env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


RESOLVE = (
    "import json;"
    "from popoto.integrations import MemoryService;"
    "s = MemoryService();"
    "from popoto.redis_db import POPOTO_REDIS_DB as c;"
    "print(json.dumps(c.connection_pool.connection_kwargs.get('db')))"
)


def test_constructing_a_service_binds_the_configured_database():
    """The single chokepoint: no caller has to remember bind_connection."""
    assert _resolved_db(RESOLVE, _env()) == int(TEST_DB)


def test_the_hermes_handler_binds_too():
    """The handler builds its service lazily; that still has to bind."""
    script = (
        "import json, sys;"
        f"sys.path.insert(0, {str(REPO_ROOT / 'plugins' / 'hermes')!r});"
        "import handler;"
        "handler._service();"
        "from popoto.redis_db import POPOTO_REDIS_DB as c;"
        "print(json.dumps(c.connection_pool.connection_kwargs.get('db')))"
    )
    assert _resolved_db(script, _env()) == int(TEST_DB)


def test_without_an_explicit_url_the_existing_connection_is_kept():
    """The in-process contract: never rebind a connection we did not choose.

    A host application, or the pytest plugin, may have already pointed
    Popoto somewhere. Constructing a MemoryService must not move it.
    """
    env = dict(os.environ)
    env.pop("POPOTO_TEST_DB", None)
    env.pop("POPOTO_MEMORY_URL", None)
    env["POPOTO_MEMORY_AGENT_ID"] = AGENT
    env["REDIS_URL"] = MEMORY_URL
    assert _resolved_db(RESOLVE, env) == int(TEST_DB)


# --- the URL that silently meant database 0 ------------------------------------


def test_a_url_with_no_database_is_rejected():
    """`redis://localhost:6379/` parses with no `db` key at all.

    Honouring it would leave writes on whatever database Popoto was already
    using while the caller believed they had been redirected. That is how a
    trailing-slash typo becomes a write to production.
    """
    from popoto.integrations.config import MemoryConfig, bind_connection

    config = MemoryConfig(url="redis://localhost:6379/", url_is_explicit=True)
    with pytest.raises(ValueError, match="no database number"):
        bind_connection(config)


def test_the_rejection_reaches_the_hook_as_exit_zero_plus_a_log(tmp_path):
    """A bad URL must not break the turn, and must not be silent either."""
    log = tmp_path / "bad-url.log"
    env = _env(POPOTO_MEMORY_URL="redis://localhost:6379/", POPOTO_MEMORY_LOG=str(log))
    result = _run(
        ["-m", "popoto.integrations.cli", "hook"],
        env,
        stdin=(FIXTURES / "claude_code_user_prompt_submit.json").read_text(),
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert log.exists(), "a rejected URL must leave a trace for doctor"
    assert "no database number" in log.read_text()


def test_redis_url_naming_db0_is_refused_when_it_would_bind(db0_unchanged):
    """Zero-config REDIS_URL on database 0 must refuse, like POPOTO_MEMORY_URL.

    In a fresh process Popoto's import-time resolution reads the same
    ``REDIS_URL``, so the live connection and the URL agree on database 0
    and ``bind_connection`` would bind there. The old guard read only the
    live connection, which is the same thing here -- but the refusal must
    also hold on the parsed-URL side of the decision (PR #594 blocker 2):
    judging the live connection let a swapped pool mask a REDIS_URL naming
    database 0 while the rebind below moved the pool there anyway.
    """
    env = dict(os.environ)
    for key in ("POPOTO_TEST_DB", "POPOTO_MEMORY_URL", "POPOTO_MEMORY_ALLOW_DB0"):
        env.pop(key, None)
    env["REDIS_URL"] = "redis://localhost:6379/0"
    env["POPOTO_MEMORY_AGENT_ID"] = AGENT
    script = (
        "from popoto.integrations import MemoryService\n"
        "from popoto.integrations.config import Db0RefusedError\n"
        "try:\n"
        "    MemoryService()\n"
        "except Db0RefusedError as exc:\n"
        "    print('REFUSED')\n"
        "    print(str(exc))\n"
        "else:\n"
        "    print('BOUND')\n"
    )
    result = _run(["-c", script], env)
    assert result.returncode == 0, result.stderr
    assert "REFUSED" in result.stdout, result.stdout
    assert (
        "REDIS_URL" in result.stdout
    ), "the refusal must name the variable that put Popoto on database 0"


def test_redis_url_does_not_rebind_a_swapped_in_process_connection():
    """REDIS_URL must not move a pool an in-process caller already swapped.

    The pytest plugin (or a host application) deliberately points the shared
    pool at an isolated database; ``REDIS_URL`` still names another one --
    possibly database 0. The verified escape (PR #594 blocker 2): the DB 0
    guard read the live connection (isolated, non-zero, passes) while the
    rebind parsed ``config.url`` (REDIS_URL, database 0) and silently moved
    the shared pool there mid-test. The rule: a live connection that
    diverges from REDIS_URL was swapped on purpose -- keep it, bind nothing,
    and refuse nothing, because writes stay on the swapped database.
    """
    from popoto.integrations.config import MemoryConfig, bind_connection

    live_db = POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db")
    assert int(live_db) != 0, "the pytest plugin should have us on an isolated db"

    config = MemoryConfig.from_env(
        {"REDIS_URL": "redis://localhost:6379/0"}, cwd="/tmp/swap-guard"
    )
    assert config.url_source == "REDIS_URL"
    assert config.url_is_explicit is False

    assert bind_connection(config) is False, "a swapped connection is kept"
    assert (
        POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db") == live_db
    ), "bind_connection moved the shared pool despite the in-process swap"


def test_mcp_dispatch_reports_the_refusal_instead_of_raising(db0_unchanged):
    """A DB 0 refusal must reach the agent as a tool error, not a traceback.

    ``dispatch`` constructs its own ``MemoryService`` when the caller passes
    none, and construction is what binds the connection -- so it is where
    ``Db0RefusedError`` is raised. Built above ``dispatch``'s ``try``, that
    exception left the function uncaught and crashed the tool call, which is
    the one thing an MCP tool must not do: the shipped contract is "errors
    come back as MCP error results with a readable message, never a
    traceback rendered as tool output".

    No service reaches Redis here -- the refusal fires before any connection
    is opened -- so database 0 stays untouched, as ``db0_unchanged`` asserts.
    """
    from popoto.integrations import mcp_server

    saved = {
        k: os.environ.get(k) for k in ("POPOTO_MEMORY_URL", "POPOTO_MEMORY_ALLOW_DB0")
    }
    os.environ["POPOTO_MEMORY_URL"] = "redis://localhost:6379/0"
    os.environ.pop("POPOTO_MEMORY_ALLOW_DB0", None)
    try:
        result = mcp_server.dispatch("memory_search", {"query": "anything"})
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert result["is_error"] is True
    assert "Db0RefusedError" in result["text"]
    assert (
        "POPOTO_MEMORY_ALLOW_DB0" in result["text"]
    ), "the error result must carry the remediation, not just the failure"


def test_the_kill_switch_is_not_broken_by_the_db0_refusal(db0_unchanged):
    """``POPOTO_MEMORY_ENABLED=0`` must stay a clean no-op on a DB 0 host.

    ``MemoryService.__init__`` binds unconditionally while ``config.enabled``
    is consulted inside the operation methods, so a guard that does not check
    ``enabled`` first turns the documented kill switch into a crash --
    ``MemoryConfig``'s own contract is "when False every operation is a no-op
    that still exits cleanly, so the kill switch never breaks a turn". The
    operator this hits is the one who disabled memory *because* the machine
    is on database 0.
    """
    from popoto.integrations.config import MemoryConfig, bind_connection
    from popoto.integrations.service import MemoryService

    env = {
        "POPOTO_MEMORY_URL": "redis://localhost:6379/0",
        "POPOTO_MEMORY_ENABLED": "0",
    }
    config = MemoryConfig.from_env(env, cwd="/tmp/kill-switch-db0")
    assert config.enabled is False

    assert bind_connection(config) is False, "a disabled layer binds nothing"
    service = MemoryService(config)
    assert service.assemble("anything") == ""
    assert service.status()["enabled"] is False

    enabled = MemoryConfig.from_env(
        {"POPOTO_MEMORY_URL": "redis://localhost:6379/0"}, cwd="/tmp/kill-switch-db0"
    )
    with pytest.raises(ValueError, match="refuses to write agent memory"):
        MemoryService(enabled)


def test_the_disabled_hook_stays_silent_on_a_db0_host(tmp_path, db0_unchanged):
    """End to end: kill switch on a DB 0 machine writes no log line either.

    Exit 0 with empty stdout was already true (the hook's blanket handler
    catches anything), but a refusal reaching that handler logs one line per
    turn -- a log that grows forever for a user who turned the feature off.
    """
    log = tmp_path / "disabled.log"
    env = _env(
        POPOTO_MEMORY_URL="redis://localhost:6379/0",
        POPOTO_MEMORY_ENABLED="0",
        POPOTO_MEMORY_LOG=str(log),
    )
    result = _run(
        ["-m", "popoto.integrations.cli", "hook"],
        env,
        stdin=(FIXTURES / "claude_code_user_prompt_submit.json").read_text(),
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert (
        not log.exists()
    ), f"a disabled memory layer must not log: {log.read_text() if log.exists() else ''}"
