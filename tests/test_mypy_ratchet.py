"""Unit tests for scripts/mypy_ratchet.py (#506).

These tests never invoke mypy and never import popoto. They drive the parser and
the comparator from synthetic mypy output, so they run in milliseconds and cannot
be made to pass by an environment that happens to have a low error count.

The gate's whole value is that it fails when it should. Every failure path below
is a direction the gate is claimed to fail in: over baseline, an allowlisted
package regressing at a flat total, a stale allowlist name, mypy exiting 2, output
that carries no summary line, empty output, a parser/summary disagreement, and a
missing or unparseable baseline file.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "mypy_ratchet.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("mypy_ratchet", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mypy_ratchet"] = module
    spec.loader.exec_module(module)
    return module


ratchet = _load_module()


def make_output(paths, extra_lines=()):
    """Build synthetic mypy stdout from a list of file paths."""
    lines = [f"{p}:{i + 1}: error: synthetic  [misc]" for i, p in enumerate(paths)]
    lines.extend(extra_lines)
    if paths:
        lines.append(
            f"Found {len(paths)} errors in {len(set(paths))} files (checked 1 source file)"
        )
    else:
        lines.append("Success: no issues found in 1 source file")
    return "\n".join(lines) + "\n"


BASE_ENV = {"python": "9.9", "mypy": "0.0.0", "redis": "0.0.0"}


@pytest.fixture
def baseline_file(tmp_path, monkeypatch):
    """Write a baseline whose environment matches whatever the test process is."""
    monkeypatch.setattr(ratchet, "current_environment", lambda: dict(BASE_ENV))

    def _write(**overrides):
        data = {
            "total": 3,
            "clean": [],
            "environment": dict(BASE_ENV),
            "packages": {"fields": 2, "models": 1},
        }
        data.update(overrides)
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps(data))
        return path

    return _write


def run_gate(stdout, baseline_path, *flags, monkeypatch=None):
    monkeypatch.setattr(ratchet, "run_mypy", lambda: stdout)
    return ratchet.main(["--baseline", str(baseline_path), *flags])


# --------------------------------------------------------------------------
# parse_output
# --------------------------------------------------------------------------


def test_parse_buckets_by_top_level_package():
    stdout = make_output(
        [
            "src/popoto/fields/base.py",
            "src/popoto/fields/geo.py",
            "src/popoto/models/query.py",
            "src/popoto/redis_db.py",
        ]
    )
    total, packages, error_lines = ratchet.parse_output(stdout)
    assert total == 4
    assert packages == {"fields": 2, "models": 1, ratchet.ROOT_BUCKET: 1}
    assert len(error_lines) == 4


def test_parse_nested_subpackage_rolls_up_to_top_level():
    stdout = make_output(["src/popoto/recipes/memory/store.py"])
    _, packages, _ = ratchet.parse_output(stdout)
    assert packages == {"recipes": 1}


def test_parse_success_line_is_zero_not_a_parse_failure():
    total, packages, error_lines = ratchet.parse_output(make_output([]))
    assert total == 0
    assert packages == {}
    assert error_lines == []


def test_parse_ignores_notes_and_column_numbers():
    stdout = (
        "src/popoto/fields/base.py:12:5: error: synthetic  [misc]\n"
        "src/popoto/fields/base.py:12:5: note: see docs\n"
        "Found 1 error in 1 file (checked 1 source file)\n"
    )
    total, packages, _ = ratchet.parse_output(stdout)
    assert total == 1
    assert packages == {"fields": 1}


def test_parse_empty_output_is_a_failure_not_zero():
    with pytest.raises(ratchet.RatchetError, match="parse failure, not zero errors"):
        ratchet.parse_output("   \n")


def test_parse_missing_summary_line_is_a_failure():
    with pytest.raises(ratchet.RatchetError, match="no 'Found N errors'"):
        ratchet.parse_output("src/popoto/fields/base.py:1: error: synthetic  [misc]\n")


def test_parse_disagreement_between_parser_and_summary_is_a_failure():
    stdout = (
        "src/popoto/fields/base.py:1: error: synthetic  [misc]\n"
        "Found 7 errors in 1 file (checked 1 source file)\n"
    )
    with pytest.raises(ratchet.RatchetError, match="parser and mypy disagree"):
        ratchet.parse_output(stdout)


# --------------------------------------------------------------------------
# run_mypy exit codes
# --------------------------------------------------------------------------


def test_mypy_exit_2_is_never_treated_as_errors_found(monkeypatch):
    class Proc:
        returncode = 2
        stdout = "usage: mypy"
        stderr = "boom"

    monkeypatch.setattr(ratchet.subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(ratchet.RatchetError, match="mypy exited 2"):
        ratchet.run_mypy()


@pytest.mark.parametrize("code", [0, 1])
def test_mypy_exit_0_and_1_are_normal(monkeypatch, code):
    class Proc:
        returncode = code
        stdout = "out"
        stderr = ""

    monkeypatch.setattr(ratchet.subprocess, "run", lambda *a, **k: Proc())
    assert ratchet.run_mypy() == "out"


# --------------------------------------------------------------------------
# load_baseline
# --------------------------------------------------------------------------


def test_missing_baseline_file_fails(tmp_path):
    with pytest.raises(ratchet.RatchetError, match="baseline file not found"):
        ratchet.load_baseline(tmp_path / "nope.json")


def test_unparseable_baseline_fails(tmp_path):
    path = tmp_path / "b.json"
    path.write_text("{not json")
    with pytest.raises(ratchet.RatchetError, match="not valid JSON"):
        ratchet.load_baseline(path)


@pytest.mark.parametrize("bad", ["12", -1, True, None])
def test_bad_total_fails(tmp_path, bad):
    path = tmp_path / "b.json"
    path.write_text(json.dumps({"total": bad}))
    with pytest.raises(ratchet.RatchetError, match="non-negative integer"):
        ratchet.load_baseline(path)


def test_bad_clean_type_fails(tmp_path):
    path = tmp_path / "b.json"
    path.write_text(json.dumps({"total": 1, "clean": [1, 2]}))
    with pytest.raises(ratchet.RatchetError, match="'clean' must be a list"):
        ratchet.load_baseline(path)


# --------------------------------------------------------------------------
# comparator
# --------------------------------------------------------------------------


def test_on_baseline_passes(baseline_file, monkeypatch, capsys):
    stdout = make_output(
        ["src/popoto/fields/a.py", "src/popoto/fields/b.py", "src/popoto/models/c.py"]
    )
    assert run_gate(stdout, baseline_file(), monkeypatch=monkeypatch) == 0
    assert "at or below baseline 3" in capsys.readouterr().out


def test_above_baseline_fails_and_names_the_package(baseline_file, monkeypatch, capsys):
    stdout = make_output(
        [
            "src/popoto/fields/a.py",
            "src/popoto/fields/b.py",
            "src/popoto/models/c.py",
            "src/popoto/models/d.py",
        ]
    )
    assert run_gate(stdout, baseline_file(), monkeypatch=monkeypatch) == 1
    err = capsys.readouterr().err
    assert "is ABOVE baseline 3 (+1)" in err
    assert "package 'models': 1 -> 2 (+1)" in err
    assert "src/popoto/models/d.py" in err


def test_below_baseline_passes_with_an_actions_warning(
    baseline_file, monkeypatch, capsys
):
    stdout = make_output(["src/popoto/fields/a.py", "src/popoto/fields/b.py"])
    assert run_gate(stdout, baseline_file(), monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert "::warning::" in out
    assert "--update" in out


def test_below_baseline_fails_under_strict_ratchet(baseline_file, monkeypatch):
    stdout = make_output(["src/popoto/fields/a.py", "src/popoto/fields/b.py"])
    code = run_gate(
        stdout, baseline_file(), "--strict-ratchet", monkeypatch=monkeypatch
    )
    assert code == 1


def test_allowlisted_package_regression_fails_at_a_flat_total(
    baseline_file, monkeypatch, capsys, tmp_path
):
    """The total is exactly on baseline; only the allowlist catches this."""
    monkeypatch.setattr(ratchet, "check_allowlist_names", lambda clean: [])
    stdout = make_output(
        ["src/popoto/fields/a.py", "src/popoto/models/c.py", "src/popoto/privacy/x.py"]
    )
    path = baseline_file(clean=["privacy"])
    assert run_gate(stdout, path, monkeypatch=monkeypatch) == 1
    err = capsys.readouterr().err
    assert "'privacy' is pinned at zero but has 1 error" in err
    assert "src/popoto/privacy/x.py" in err


def test_allowlisted_package_at_zero_passes(baseline_file, monkeypatch):
    monkeypatch.setattr(ratchet, "check_allowlist_names", lambda clean: [])
    stdout = make_output(
        ["src/popoto/fields/a.py", "src/popoto/fields/b.py", "src/popoto/models/c.py"]
    )
    path = baseline_file(clean=["privacy"])
    assert run_gate(stdout, path, monkeypatch=monkeypatch) == 0


def test_stale_allowlist_name_fails(baseline_file, monkeypatch, capsys):
    stdout = make_output(
        ["src/popoto/fields/a.py", "src/popoto/fields/b.py", "src/popoto/models/c.py"]
    )
    path = baseline_file(clean=["no_such_package"])
    assert run_gate(stdout, path, monkeypatch=monkeypatch) == 1
    assert "Stale allowlist entries" in capsys.readouterr().err


def test_check_allowlist_names_accepts_real_packages():
    assert ratchet.check_allowlist_names(["privacy", "integrations"]) == []
    assert ratchet.check_allowlist_names(["not_a_package"]) == ["not_a_package"]


# --------------------------------------------------------------------------
# environment guard
# --------------------------------------------------------------------------


def test_environment_mismatch_refuses_to_compare(baseline_file, monkeypatch, capsys):
    path = baseline_file(environment={**BASE_ENV, "redis": "7.1.1"})
    stdout = make_output(["src/popoto/fields/a.py"] * 99)
    assert run_gate(stdout, path, monkeypatch=monkeypatch) == 0
    captured = capsys.readouterr()
    assert "Environment does not match" in captured.err
    assert "does not compare against baseline 3" in captured.err
    assert "at or below baseline" not in captured.out


def test_environment_mismatch_fails_under_strict_env(baseline_file, monkeypatch):
    path = baseline_file(environment={**BASE_ENV, "redis": "7.1.1"})
    stdout = make_output(["src/popoto/fields/a.py"] * 99)
    assert run_gate(stdout, path, "--strict-env", monkeypatch=monkeypatch) == 1


# --------------------------------------------------------------------------
# --update
# --------------------------------------------------------------------------


def test_update_rewrites_total_and_packages_and_preserves_clean(
    baseline_file, monkeypatch
):
    monkeypatch.setattr(ratchet, "check_allowlist_names", lambda clean: [])
    path = baseline_file(clean=["privacy"], total=99)
    stdout = make_output(["src/popoto/fields/a.py", "src/popoto/models/c.py"])
    assert run_gate(stdout, path, "--update", monkeypatch=monkeypatch) == 0
    data = json.loads(path.read_text())
    assert data["total"] == 2
    assert data["packages"] == {"fields": 1, "models": 1}
    assert data["clean"] == ["privacy"]
    assert data["environment"] == BASE_ENV


def test_update_does_not_compare_or_fail_when_over_baseline(baseline_file, monkeypatch):
    """--update must never be gated by the comparison it is meant to reset."""
    monkeypatch.setattr(ratchet, "check_allowlist_names", lambda clean: [])
    path = baseline_file(total=0, clean=["privacy"])
    stdout = make_output(["src/popoto/privacy/x.py"] * 5)
    assert run_gate(stdout, path, "--update", monkeypatch=monkeypatch) == 0
    assert json.loads(path.read_text())["total"] == 5


# --------------------------------------------------------------------------
# the repo's committed baseline
# --------------------------------------------------------------------------


def test_committed_baseline_is_well_formed():
    data = ratchet.load_baseline(ratchet.BASELINE_PATH)
    assert data["total"] > 0
    assert ratchet.check_allowlist_names(data["clean"]) == []
    assert set(data["environment"]) >= {"python", "mypy", "redis"}
    assert sum(data["packages"].values()) == data["total"]
