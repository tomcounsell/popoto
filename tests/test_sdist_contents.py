"""Tests for ``scripts/check_sdist_contents.py`` and its release.yml wiring (#678).

No test here builds an sdist or touches the network: the rules are exercised
against synthetic tarballs constructed in ``tmp_path``, and the wiring test
reads the workflow file from disk (the shape of
``tests/test_ci_workflow_redis_url.py``).
"""

from __future__ import annotations

import importlib.util
import io
import pathlib
import subprocess
import sys
import tarfile

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_sdist_contents.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_sdist_contents", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_module()


def _make_sdist(path: pathlib.Path, names: list[str], *, link: str | None = None):
    """Build a tarball rooted at ``popoto-1.9.0/`` containing ``names``."""
    with tarfile.open(path, "w:gz") as tar:
        for name in names:
            info = tarfile.TarInfo(f"popoto-1.9.0/{name}")
            info.size = 0
            tar.addfile(info, io.BytesIO(b""))
        if link is not None:
            info = tarfile.TarInfo(f"popoto-1.9.0/{link}")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return path


CLEAN = ["PKG-INFO", "README.md", "pyproject.toml", "src/popoto/__init__.py"]


def _members(path: pathlib.Path):
    with tarfile.open(path) as tar:
        return tar.getmembers()


def test_clean_sdist_passes(tmp_path):
    sdist = _make_sdist(tmp_path / "clean.tar.gz", CLEAN)
    failures, warnings = checker.check_members(_members(sdist))
    assert failures == []
    assert warnings == []


def test_non_ascii_member_fails(tmp_path):
    sdist = _make_sdist(tmp_path / "nfc.tar.gz", CLEAN + ["src/popoto/café.py"])
    failures, _ = checker.check_members(_members(sdist))
    assert any("non-ASCII" in f for f in failures)


def test_dotfile_at_any_depth_fails(tmp_path):
    sdist = _make_sdist(tmp_path / "dot.tar.gz", CLEAN + ["src/.env"])
    failures, _ = checker.check_members(_members(sdist))
    assert any("dotfile" in f for f in failures)


def test_parent_traversal_member_fails(tmp_path):
    sdist = _make_sdist(tmp_path / "dots.tar.gz", CLEAN + ["../outside.txt"])
    failures, _ = checker.check_members(_members(sdist))
    assert any("parent-traversing" in f for f in failures)


def test_symlink_member_fails(tmp_path):
    """A link's target escapes even when its own name is clean ASCII."""
    sdist = _make_sdist(tmp_path / "link.tar.gz", CLEAN, link="src/shortcut")
    failures, _ = checker.check_members(_members(sdist))
    assert any("symlink/hardlink" in f for f in failures)


def test_unexpected_top_level_warns_without_failing(tmp_path):
    """The severity split is the thing most likely to be 'simplified' away.

    A legitimate packaging addition must never block a release, so an entry
    outside the allowlist is reported and the run still exits 0.
    """
    sdist = _make_sdist(tmp_path / "extra.tar.gz", CLEAN + ["docs/index.md"])
    failures, warnings = checker.check_members(_members(sdist))
    assert failures == []
    assert any("docs" in w for w in warnings)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(sdist)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING" in result.stdout


def test_failing_sdist_exits_non_zero(tmp_path):
    sdist = _make_sdist(tmp_path / "bad.tar.gz", CLEAN + ["src/.env"])
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(sdist)], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "FAIL" in result.stdout


@pytest.mark.parametrize("argv_tail", [[], ["a.tar.gz", "b.tar.gz"]])
def test_wrong_argument_count_exits_non_zero(argv_tail):
    """Zero and two shell-expanded arguments must both be hard errors.

    Two matches is a stale artifact from a prior run; it must not be silently
    narrowed to one (which is what re-globbing inside the script would do).
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *argv_tail], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "expected exactly one sdist" in result.stderr


def test_unexpanded_glob_reports_zero_matches(tmp_path):
    """An unmatched glob reaches the script literally -- name the remedy."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "dist/*.tar.gz"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode != 0
    assert "no sdist found matching" in result.stderr


def test_release_workflow_invokes_the_check_before_publishing():
    """A check anyone can delete in a one-line diff is not a check."""
    text = WORKFLOW.read_text()
    invocation = "python scripts/check_sdist_contents.py dist/*.tar.gz"
    assert invocation in text, f"release.yml no longer runs: {invocation}"

    build_at = text.index("python -m build")
    check_at = text.index(invocation)
    publish_at = text.index("pypa/gh-action-pypi-publish")
    assert build_at < check_at < publish_at, (
        "the sdist check must run between `python -m build` and the publish "
        "action -- that is the only point where a real sdist exists and "
        "failing still keeps it off PyPI"
    )


def test_build_system_floor_is_at_least_83():
    """setuptools>=83 is the first release with the MANIFEST.in bypass fix."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    build_system = text.split("[project]", 1)[0]
    assert 'requires = ["setuptools>=83", "wheel"]' in build_system
