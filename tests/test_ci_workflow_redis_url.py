"""Drift guard between CI's Redis environment and ``popoto_test_db``.

``.github/workflows/tests.yml`` pins ``REDIS_URL`` to an explicit database so a
test that binds a connection from the environment lands on the same database
the pytest plugin isolates onto (issue #639). That constant duplicates
``popoto_test_db`` in ``pyproject.toml``, so this module fails loudly if the two
ever disagree — in either direction:

1. every ``REDIS_URL`` assignment in ``.github/workflows/`` names a database,
   and it is the one ``pyproject.toml`` declares;
2. if a workflow ever sets ``POPOTO_TEST_DB`` (none does today), it names the
   same database too — ``_swap_db`` makes that variable the real authority for
   the plugin's connection, so a disagreement there is the same split-brain;
3. every job in ``tests.yml`` still sets ``REDIS_URL`` at all. Without this the
   first two checks pass vacuously when the variable is deleted, which restores
   the very database-0 hazard #639 closed.

Scope note: these checks read ``NAME: value`` / ``NAME=value`` assignments at
the start of a line, which is how the workflows declare their environment. A
value injected at runtime (``echo "REDIS_URL=..." >> $GITHUB_ENV`` inside a
``run:`` block) is not covered — if that pattern is ever introduced, this
module needs extending.

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

# Workflows that do NOT run the library suite, and so have no reason to agree
# with ``popoto_test_db``. Exactly one qualifies: examples.yml runs the kitchen
# demo, a separate uv project under ``examples/`` that disables the popoto
# pytest plugin outright (``addopts = "-p no:popoto"``) and manages its own data
# with model-scoped deletes. With the plugin off there is no isolated database
# for a from-env bind to agree with, and pinning it to 15 would put the demo's
# seed data in the path of the library suite's flush on a developer machine.
# The exclusion is not a hole: test_examples_workflow_uses_its_own_database
# below pins what examples.yml must do instead.
_NON_LIBRARY_WORKFLOWS = frozenset({"examples.yml"})


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
        if path.name in _NON_LIBRARY_WORKFLOWS:
            continue
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


def test_examples_workflow_uses_its_own_database() -> None:
    """examples.yml is excluded above, so pin what it must do instead.

    The kitchen demo seeds and clears real records. It must therefore name a
    database explicitly (never inherit database 0 from a db-less URL) and it
    must not be the library suite's, whose flush would delete the demo's data
    mid-run when both are run on one developer machine.
    """
    library_db = _expected_db()
    assignments = [
        (path, line, url)
        for path, line, url in _assignments("REDIS_URL")
        if path.name in _NON_LIBRARY_WORKFLOWS
    ]
    assert assignments, (
        "no REDIS_URL assignment found in "
        f"{sorted(_NON_LIBRARY_WORKFLOWS)}; either the workflow lost its "
        "database binding (the demo suite fails closed without one) or the "
        "exclusion list in this module is stale."
    )

    for path, line, url in assignments:
        where = f"{path.relative_to(REPO_ROOT)}:{line}"
        database = urlsplit(url).path.lstrip("/")
        assert database and database != "0", (
            f"{where}: REDIS_URL is {url!r}, which resolves to database 0. "
            "The demo suite seeds and clears data; database 0 is refused."
        )
        assert database != library_db, (
            f"{where}: REDIS_URL is {url!r}, the same database as the library "
            f"suite's popoto_test_db = {library_db!r}. The library suite "
            "flushes it, which would delete the demo's seed data mid-run."
        )


TESTS_WORKFLOW = WORKFLOW_DIR / "tests.yml"

_JOB_HEADER = re.compile(r"^  (?P<name>[A-Za-z_][\w-]*):\s*$", re.MULTILINE)


def _tests_workflow_jobs() -> list[tuple[str, int, int]]:
    """Each job in ``tests.yml`` as (name, first line, last line).

    Scanning starts at the ``jobs:`` key: two-space keys also appear under
    ``on:`` (``pull_request``, ``push``, ``workflow_dispatch``), and treating
    those as jobs would fail the guard on a correct workflow.
    """
    text = TESTS_WORKFLOW.read_text(encoding="utf-8")
    jobs_key = re.search(r"^jobs:\s*$", text, re.MULTILINE)
    if jobs_key is None:
        pytest.fail(f"no top-level `jobs:` key found in {TESTS_WORKFLOW}")
    starts = [
        (m.group("name"), text.count("\n", 0, m.start()) + 1)
        for m in _JOB_HEADER.finditer(text, jobs_key.end())
    ]
    if not starts:
        pytest.fail(
            f"no jobs found in {TESTS_WORKFLOW}; this guard cannot confirm "
            "that CI still pins a test database"
        )
    last_line = text.count("\n") + 1
    bounds = [end for _, end in starts[1:]] + [last_line + 1]
    return [(name, start, end - 1) for (name, start), end in zip(starts, bounds)]


def test_every_tests_workflow_job_sets_redis_url() -> None:
    """No job may drop ``REDIS_URL``, which would silently restore DB 0.

    The other two checks only constrain assignments that exist. Deleting
    ``REDIS_URL`` outright satisfies them vacuously while putting a from-env
    consumer back on ``DEFAULT_URL``'s database 0 — the hazard #639 closed, and
    the one remedy #635 chose for ``ci-local.sh`` that does not transfer here.
    Anchoring on jobs rather than a fixed count means a future third job has to
    pin the database too.
    """
    assignments = [
        (line, url)
        for path, line, url in _assignments("REDIS_URL")
        if path == TESTS_WORKFLOW
    ]

    for name, start, end in _tests_workflow_jobs():
        covered = [url for line, url in assignments if start <= line <= end]
        assert covered, (
            f"{TESTS_WORKFLOW.relative_to(REPO_ROOT)}: job {name!r} "
            f"(lines {start}-{end}) sets no REDIS_URL. Deleting it does not "
            "leave the variable unset — popoto's DEFAULT_URL resolves to "
            "database 0, which the #584 guard refuses. Pin the database "
            "instead (see #639)."
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
