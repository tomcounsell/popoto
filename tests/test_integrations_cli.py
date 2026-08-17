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


def test_demo_prints_the_rejection_instead_of_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("POPOTO_MEMORY_URL", BAD_URL)

    exit_code = main(["demo"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "no database number" in out
    assert "Traceback" not in out
