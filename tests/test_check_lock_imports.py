"""The lock import smoke must be able to fail.

`scripts/check_lock_imports.py` is the only thing standing between a
`lockfile-only` Dependabot PR and a green check that proves nothing (#669). A
smoke that cannot fail is worth less than no smoke at all, because it reads as
coverage. The bogus-package case below is the assertion that matters; the
stdlib case only pins the clean path.

These tests import no popoto module and need no Redis.
"""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_lock_imports.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_lock_imports", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_reports_no_failures_for_an_importable_module():
    module = _load_module()
    # `json` is stdlib, so this case does not itself depend on an optional
    # extra being installed in whatever environment runs the suite.
    assert module.check([("json", "stdlib")]) == []


def test_check_reports_a_failure_for_a_missing_module():
    module = _load_module()
    failures = module.check([("popoto_no_such_package_xyz", "bogus")])
    assert len(failures) == 1
    assert "popoto_no_such_package_xyz" in failures[0]
    assert "bogus" in failures[0]


def test_check_continues_past_a_failure():
    module = _load_module()
    failures = module.check(
        [("popoto_no_such_package_xyz", "bogus"), ("json", "stdlib")]
    )
    assert len(failures) == 1


def test_package_list_excludes_benchmark():
    module = _load_module()
    # `lock-check.yml` syncs with `--no-extra benchmark`; a package from that
    # extra in this list would fail the job on a set the sync never installs.
    assert "benchmark" not in {extra for _, extra in module.PACKAGES}
