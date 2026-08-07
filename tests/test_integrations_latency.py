"""Read-hook latency budget, measured at the subprocess level.

The read hook is synchronous and sits on the critical path of every turn. If
it adds a felt pause before each response, users uninstall regardless of how
good the recall is. Redis is not the risk -- assembly is single-digit
milliseconds -- Python interpreter startup plus ``import popoto`` is.

Budget: **400 ms p95 end to end** for ``popoto-memory hook`` on the read
path, measured as full subprocess wall time including interpreter startup.
Marked ``slow`` because it spawns 25 processes.

If this test fails, do not reach for a daemon first. The escalation order is
(a) trim what the read path imports, (b) confirm the write hook is
``"async": true`` so only the read path is exposed, (c) only then add a
local HTTP daemon and switch the Claude Code recipe to ``"type": "http"``.
A daemon is a second process users must manage.
"""

import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

FIXTURES = Path(SCRIPT_DIR) / "fixtures" / "harness_payloads"
AGENT = "test-integrations-latency"
BUDGET_MS = 400.0
RUNS = 25

pytestmark = pytest.mark.slow


def _env():
    env = dict(os.environ)
    db = env.pop("POPOTO_TEST_DB", "15")
    env["POPOTO_MEMORY_URL"] = f"redis://localhost:6379/{db}"
    env["POPOTO_MEMORY_AGENT_ID"] = AGENT
    return env


@pytest.fixture
def seeded():
    from popoto.recipes import DefaultMemory

    records = [
        DefaultMemory(
            agent_id=AGENT,
            content=f"Deploy note {i}: health checks gate the blue-green cutover",
            importance=0.8,
        )
        for i in range(50)
    ]
    for record in records:
        record.save()
    yield
    for record in DefaultMemory.query.filter(agent_id=AGENT):
        try:
            record.delete()
        except Exception:
            pass


def _time_reads(payload, env, runs=RUNS):
    import time

    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        subprocess.run(
            [sys.executable, "-m", "popoto.integrations.cli", "hook"],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        samples.append((time.perf_counter() - start) * 1000)
    return sorted(samples)


def test_read_hook_p95_is_within_budget(seeded):
    payload = json.dumps(
        json.loads((FIXTURES / "claude_code_user_prompt_submit.json").read_text())
    )
    samples = _time_reads(payload, _env())
    p50 = statistics.median(samples)
    p95 = samples[int(0.95 * len(samples)) - 1]
    report = (
        f"read hook over {RUNS} subprocesses: "
        f"p50 {p50:.0f} ms, p95 {p95:.0f} ms, max {samples[-1]:.0f} ms "
        f"(budget {BUDGET_MS:.0f} ms p95)"
    )
    print("\n" + report)
    assert p95 < BUDGET_MS, report


def test_the_read_hook_actually_injected_during_the_measurement(seeded):
    """Guards against measuring a fast no-op instead of a real assembly."""
    payload = (FIXTURES / "claude_code_user_prompt_submit.json").read_text()
    result = subprocess.run(
        [sys.executable, "-m", "popoto.integrations.cli", "hook"],
        input=payload,
        capture_output=True,
        text=True,
        env=_env(),
        timeout=120,
    )
    assert result.returncode == 0
    assert "additionalContext" in result.stdout


def test_import_of_the_integrations_package_stays_light():
    """`import popoto.integrations` must pull in no submodule of its own.

    Cold start is the whole reason the package uses a module-level
    ``__getattr__``; this asserts the property that makes it worth having.
    (``popoto.recipes`` is not checked: ``popoto/__init__.py`` imports it,
    so it is already loaded before this package is reached.)
    """
    heavy = ("service", "hooks", "mcp_server", "cli", "demo")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, popoto.integrations as p;"
            "assert 'MemoryService' in p.__all__;"
            "print(sorted(n for n in sys.modules "
            "if n.startswith('popoto.integrations.')))",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    loaded = result.stdout.strip()
    for name in heavy:
        assert f"popoto.integrations.{name}" not in loaded, loaded


def test_the_mcp_sdk_is_not_needed_by_the_hook_path():
    """The hook path must work on a bare `pip install popoto`."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys;"
            "sys.modules['mcp'] = None;"
            "from popoto.integrations import hooks, service, cli;"
            "print('ok')",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
