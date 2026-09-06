"""Drift guard between CI's Redis environment and ``popoto_test_db``.

``.github/workflows/tests.yml`` pins ``REDIS_URL`` to an explicit database so a
test that binds a connection from the environment lands on the same database
the pytest plugin isolates onto (issue #639). That constant duplicates
``popoto_test_db`` in ``pyproject.toml``, so this module fails loudly if the two
ever disagree — in either direction:

1. every ``REDIS_URL`` in ``.github/workflows/`` names a database, and it is the
   one ``pyproject.toml`` declares;
2. if a workflow ever sets ``POPOTO_TEST_DB`` (none does today), it names the
   same database too — ``_swap_db`` makes that variable the real authority for
   the plugin's connection, so a disagreement there is the same split-brain.

Both files are parsed from disk with the standard library only: no new test
dependency, and no Redis connection. ``tomllib`` is deliberately not used —
the project's ``requires-python`` floor is 3.10, where it does not exist.
"""

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
PYPROJECT = REPO_ROOT / "pyproject.toml"

_PYPROJECT_TEST_DB = re.compile(
    r"""^\s*popoto_test_db\s*=\s*["'](?P<value>[^"']*)["']""", re.MULTILINE
)


def _env_assignment(name: str) -> re.Pattern[str]:
    """Match ``NAME: value`` / ``NAME=value`` lines in a workflow file."""
    return re.compile(
        rf"^\s*{re.escape(name)}\s*[:=]\s*(?P<value>[^\s#]+)", re.MULTILINE
    )


def _expected_db() -> str:
    """The database ``pyproject.toml`` declares for the test suite."""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = _PYPROJECT_TEST_DB.search(text)
    if match is None:
        pytest.fail(
            f'no `popoto_test_db = "..."` setting found in {PYPROJECT}; '
            "the CI workflows pin a database number that must match it"
        )
    return match.group("value").strip()


def _workflow_files() -> list[Path]:
    return sorted(
        p
        for p in WORKFLOW_DIR.iterdir()
        if p.suffix in {".yml", ".yaml"} and p.is_file()
    )


def _assignments(name: str) -> list[tuple[Path, int, str]]:
    """Every ``name`` assignment across the workflows, as (file, line, value)."""
    found: list[tuple[Path, int, str]] = []
    pattern = _env_assignment(name)
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            value = match.group("value").strip().strip("\"'")
            found.append((path, line, value))
    return found


def test_workflow_redis_url_names_the_pyproject_test_db() -> None:
    """Every workflow ``REDIS_URL`` names ``popoto_test_db``'s database."""
    expected = _expected_db()

    for path, line, url in _assignments("REDIS_URL"):
        where = f"{path.relative_to(REPO_ROOT)}:{line}"
        database = urlsplit(url).path.lstrip("/")
        assert database, (
            f"{where}: REDIS_URL is {url!r}, which names no database; "
            f"pyproject.toml says popoto_test_db = {expected!r}. "
            "A db-less URL resolves to database 0, which the test guard "
            "refuses (#584/#639)."
        )
        assert database == expected, (
            f"{where}: REDIS_URL is {url!r}, so the workflow says database "
            f"{database!r}, but pyproject.toml says popoto_test_db = "
            f"{expected!r}. Update whichever is stale so both name one "
            "database."
        )


def test_workflow_popoto_test_db_matches_pyproject() -> None:
    """If a workflow sets ``POPOTO_TEST_DB``, it agrees with pyproject.

    Vacuous today — no workflow sets it — and deliberately so: it exists to
    fire the moment one does, because ``POPOTO_TEST_DB`` overrides
    ``popoto_test_db`` for the pytest plugin while ``REDIS_URL`` stays pinned.
    """
    expected = _expected_db()

    for path, line, value in _assignments("POPOTO_TEST_DB"):
        where = f"{path.relative_to(REPO_ROOT)}:{line}"
        assert value == expected, (
            f"{where}: the workflow says POPOTO_TEST_DB = {value!r}, but "
            f"pyproject.toml says popoto_test_db = {expected!r}. The plugin "
            "would isolate onto one database while REDIS_URL points at the "
            "other."
        )
