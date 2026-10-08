"""Shared driver for the end-to-end harness tests (#814).

Runs the real ``popoto-memory`` console script (``hook`` and ``doctor``) as
subprocesses, exactly as a harness would, and installs a Redis connect guard
in each child through a ``sitecustomize`` module on ``PYTHONPATH``: every
attempt to open a Redis connection is written to a log file and refused. A
Postgres-bound child must leave that log absent.

Not a test module (no ``test_`` prefix): imported by
``tests/test_integrations_e2e.py`` (the Redis leg) and
``tests/postgres/test_postgres_harness.py`` (the Postgres leg).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

GUARD = """\
import os
import traceback

_LOG = os.environ.get("POPOTO_E2E_REDIS_GUARD_LOG")
if _LOG:
    import redis.connection as _rc

    def _refuse(self, *args, **kwargs):
        with open(_LOG, "a") as fh:
            fh.write("redis connect attempt\\n")
            fh.write("".join(traceback.format_stack(limit=16)) + "\\n")
        raise _rc.ConnectionError("e2e guard: Redis connections are refused")

    _rc.AbstractConnection.connect = _refuse
"""

#: Variables a child must not inherit from the test process: they would bind
#: it somewhere other than where the test points it.
SCRUBBED = (
    "POPOTO_BACKEND",
    "POPOTO_POSTGRES_URL",
    "POPOTO_POSTGRES_SCHEMA",
    "POPOTO_POSTGRES_MAINTENANCE_URL",
    "POPOTO_MEMORY_URL",
    "POPOTO_MEMORY_ENABLED",
    "POPOTO_MEMORY_INGEST",
    "POPOTO_MEMORY_TURN_KEYED",
    "REDIS_URL",
)

DEPLOY_FACT = "Deploys are blue-green with automatic rollback on failed health checks"


def command() -> List[str]:
    """The installed console script, else the module entry point."""
    script = shutil.which("popoto-memory", path=os.path.dirname(sys.executable))
    if script:
        return [script]
    return [sys.executable, "-m", "popoto.integrations.cli"]


class Harness:
    """One isolated child environment: its own log, guard and agent id."""

    def __init__(
        self,
        tmp_path: Path,
        env: Dict[str, str],
        agent: str,
        guard_redis: bool = True,
    ):
        guard_dir = tmp_path / "guard"
        guard_dir.mkdir(exist_ok=True)
        (guard_dir / "sitecustomize.py").write_text(GUARD)
        self.guard_log = tmp_path / "redis-guard.log"
        self.memory_log = tmp_path / "memory.log"
        base = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
        base["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(guard_dir), base.get("PYTHONPATH", "")) if p
        )
        if guard_redis:
            base["POPOTO_E2E_REDIS_GUARD_LOG"] = str(self.guard_log)
        else:
            base.pop("POPOTO_E2E_REDIS_GUARD_LOG", None)
        base["POPOTO_MEMORY_LOG"] = str(self.memory_log)
        base["POPOTO_MEMORY_AGENT_ID"] = agent
        base.update(env)
        self.env = base

    def run(self, args: List[str], stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(
            command() + args,
            input=stdin,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def hook(self, payload: Dict[str, Any]) -> subprocess.CompletedProcess:
        return self.run(["hook"], json.dumps(payload))

    def redis_attempts(self) -> str:
        return self.guard_log.read_text() if self.guard_log.exists() else ""


def drive_turns(h: Harness) -> Dict[str, Any]:
    """Capture one turn, read it back on the next, report the outcome.

    Returns the injected context (or ``None``) and every child's result.
    """
    write0 = h.hook(
        {
            "hook_event_name": "Stop",
            "session_id": "e2e-session",
            "prompt_id": "p0",
            "last_assistant_message": DEPLOY_FACT,
        }
    )
    read1 = h.hook(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "e2e-session",
            "prompt_id": "p1",
            "prompt": "how do deploys roll back after a failed health check?",
        }
    )
    write1 = h.hook(
        {
            "hook_event_name": "Stop",
            "session_id": "e2e-session",
            "prompt_id": "p1",
            "last_assistant_message": "They roll back to the previous green stack.",
        }
    )
    context: Optional[str] = None
    if read1.stdout.strip():
        context = json.loads(read1.stdout)["hookSpecificOutput"]["additionalContext"]
    return {"context": context, "runs": [write0, read1, write1]}
