"""``popoto-memory doctor`` and ``demo`` must diagnose misconfiguration, not
crash on it.

``bind_connection`` (config.py) raises ``ValueError`` for a
``POPOTO_MEMORY_URL`` with no parseable database number -- deliberately, so
a write never silently lands on database 0. But ``doctor`` and ``demo`` are
the surfaces built to explain misconfiguration to a human; letting that
``ValueError`` propagate as a traceback defeats the point. Reproduces the
exact repro from the PR #546 review: ``POPOTO_MEMORY_URL=redis://localhost:6379/``.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from popoto.integrations.cli import main  # noqa: E402
from popoto.integrations.service import COUNTER_KEY_PREFIX  # noqa: E402
from popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

BAD_URL = "redis://localhost:6379/"


def test_doctor_prints_the_rejection_instead_of_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("POPOTO_MEMORY_URL", BAD_URL)

    exit_code = main(["doctor"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "no database number" in out
    assert "Traceback" not in out


def test_doctor_json_mode_prints_the_rejection_instead_of_a_traceback(
    monkeypatch, capsys
):
    monkeypatch.setenv("POPOTO_MEMORY_URL", BAD_URL)

    exit_code = main(["doctor", "--json"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "no database number" in out
    assert "Traceback" not in out


def test_doctor_reports_evictions_as_data_loss_not_failures(monkeypatch, capsys):
    """A non-zero ``evicted`` counter is a data-loss report (#596).

    Doctor must render it on its own ``DATA LOSS`` line naming the count and
    ``POPOTO_DEFAULT_MEMORY_MAX_RECORDS`` -- and must *not* bucket it under
    ``FAILURES``, which would mislabel deliberate cap enforcement as an
    integration error.
    """
    agent = "test-integrations-cli-evicted"
    # Clear BOTH url vars so the doctor judges the live (pytest-swapped)
    # connection: CI exports a db-less REDIS_URL=redis://localhost:6379,
    # which the #584 refusal rejects with "URL names no database" (exit 1).
    monkeypatch.delenv("POPOTO_MEMORY_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("POPOTO_MEMORY_AGENT_ID", agent)
    counter_key = f"{COUNTER_KEY_PREFIX}:{agent}:evicted"
    POPOTO_REDIS_DB.set(counter_key, 12)
    try:
        exit_code = main(["doctor", "--no-latency"])
    finally:
        POPOTO_REDIS_DB.delete(counter_key)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "DATA LOSS" in out
    assert "12 records selected for eviction" in out
    assert "POPOTO_DEFAULT_MEMORY_MAX_RECORDS" in out
    # The report line is the ONLY place the counter surfaces: no FAILURES
    # bucket appears for an agent whose sole counter is `evicted`.
    assert "FAILURES" not in out
    assert "failures       none" in out


def test_demo_prints_the_rejection_instead_of_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("POPOTO_MEMORY_URL", BAD_URL)

    exit_code = main(["demo"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "no database number" in out
    assert "Traceback" not in out
