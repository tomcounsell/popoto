"""The real ``popoto-memory`` hook and ``doctor`` subprocesses on Redis (#814).

The Redis leg of ``tests/postgres/test_postgres_harness.py``'s end-to-end
test: the same three hook turns and the same ``doctor`` calls, against the
pytest plugin's isolated database named through ``REDIS_URL`` (which is how a
harness passes it), so the Postgres work is shown not to have moved the Redis
path. The child's report must still be the Redis one, byte for byte in the
lines a user reads.
"""

import json
import uuid

from popoto.redis_db import get_REDIS_DB
from tests import harness_e2e


def _redis_harness(tmp_path):
    db = int(get_REDIS_DB().connection_pool.connection_kwargs.get("db", 0) or 0)
    assert db != 0, "the e2e test must run on the plugin's isolated database"
    return harness_e2e.Harness(
        tmp_path,
        {"REDIS_URL": f"redis://localhost:6379/{db}"},
        agent=f"e2e-redis-{uuid.uuid4().hex[:8]}",
        guard_redis=False,
    )


def test_the_hook_and_doctor_run_on_redis_unchanged(tmp_path):
    h = _redis_harness(tmp_path)
    turns = harness_e2e.drive_turns(h)
    for run in turns["runs"]:
        assert run.returncode == 0, run.stderr
        assert "failed" not in run.stderr, run.stderr
    assert turns["context"] is not None
    assert harness_e2e.DEPLOY_FACT in turns["context"]

    doctor = h.run(["doctor", "--json"])
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    info = json.loads(doctor.stdout)
    assert info["backend"] == "redis"
    assert info["redis_reachable"] is True and info["reachable"] is True
    assert info["url_source"] == "REDIS_URL"
    assert info["record_count"] == 2
    assert info["counters"] == {"capture_ok": 2, "assemble_ok": 1}
    assert "postgres" not in info

    text = h.run(["doctor"])
    assert text.returncode == 0, text.stdout
    lines = text.stdout.splitlines()
    assert lines[2].startswith("  status         enabled")
    assert lines[3].startswith("  redis url      redis://localhost:6379/")
    assert lines[4] == "  url source     REDIS_URL"
    assert lines[5].startswith("  redis          reachable, ")
    assert "backend" not in text.stdout


def test_the_redis_connect_guard_is_live(tmp_path):
    """Control for the Postgres leg's "zero Redis connect attempts": with the
    guard installed, a Redis-bound child's first command is caught and
    logged. Without this, an absent log could mean the guard never loaded."""
    h = harness_e2e.Harness(
        tmp_path, {"REDIS_URL": "redis://127.0.0.1:1/15"}, agent="e2e-guard"
    )
    run = h.hook(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "x y"}
    )
    assert run.returncode == 0
    assert "redis connect attempt" in h.redis_attempts()


def test_the_hook_survives_redis_down_and_says_which_backend(tmp_path):
    h = harness_e2e.Harness(
        tmp_path,
        {"REDIS_URL": "redis://127.0.0.1:1/15"},
        agent="e2e-redis-down",
        guard_redis=False,
    )
    run = h.hook(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s",
            "prompt": "how do deploys roll back?",
        }
    )
    assert run.returncode == 0
    assert run.stdout == ""
    assert "(backend: redis)" in run.stderr
    doctor = h.run(["doctor"])
    assert doctor.returncode == 1
    assert "  redis          UNREACHABLE\n" in doctor.stdout
