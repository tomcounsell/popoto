"""User-facing docs must not teach a client built from a db-less Redis URL.

``docs/features/confidence-field.md`` used to show
``redis.from_url("redis://localhost:6379")``. That URL names no database, so it
resolves to database 0 (redis-py leaves ``db`` out of ``connection_kwargs`` and
``AbstractConnection`` defaults it to 0) — and it opens a *second* connection
unrelated to the one Popoto is bound to, so the read silently misses the write
on any other database. Issue #645; the same hazard class as #635 (``ci-local.sh``)
and #639 (``tests.yml``), but in the docs that teach the pattern.

The remedy the docs now teach is ``popoto.get_redis()``, which has no database
in it at all. This module fails if a db-less client construction comes back.

Scope, and why two directories are excluded:

* ``docs/plans/`` is a historical archive. Several plans quote the db-less URL
  precisely *because* it is the defect under discussion; rewriting them would
  falsify the record of why those decisions were made.
* ``docs/sdlc/`` is process documentation for this repo's own pipeline, not
  user-facing API teaching.

Known limits, stated rather than implied: the matcher reads the URL literal out
of a ``from_url(...)`` call, so a URL bound to a variable first
(``url = "redis://h:6379"; redis.from_url(url)``) or assembled from fragments is
not detected. This guard stops an editor from copying the old shape back in; it
is not an adversarial control, and claiming otherwise would invite the false
confidence that made PR #643's first guard vacuously green.

Standard library only — no Redis connection, no new test dependency, and no
``tomllib`` (the project's ``requires-python`` floor is 3.10).
"""

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
EXCLUDED_DIRS = (DOCS_DIR / "plans", DOCS_DIR / "sdlc")

# Files the docs fix for #645 actually touched. Asserting these are in the
# scanned set is what stops the whole module passing because a rename or a
# directory move quietly left it scanning nothing.
REQUIRED_FILES = (
    DOCS_DIR / "features" / "confidence-field.md",
    DOCS_DIR / "configuration.md",
)

# Not line-anchored: a call split across lines is still matched.
_FROM_URL = re.compile(r"""from_url\(\s*["'](?P<url>redis[s]?://[^"']*)["']""")


def _scanned_files() -> list[Path]:
    """Every user-facing doc, excluding the archive and process directories."""
    paths = [
        p
        for p in DOCS_DIR.rglob("*.md")
        if not any(p.is_relative_to(d) for d in EXCLUDED_DIRS)
    ]
    paths.append(REPO_ROOT / "README.md")
    return sorted(p for p in paths if p.is_file())


def _dbless_from_url_matches(text: str) -> list[re.Match]:
    """``from_url`` calls whose URL literal names no database."""
    return [
        m
        for m in _FROM_URL.finditer(text)
        if not urlsplit(m.group("url")).path.lstrip("/")
    ]


def _dbless_from_url_calls(text: str) -> list[str]:
    """The db-less URLs themselves, for assertions that only need the text."""
    return [m.group("url") for m in _dbless_from_url_matches(text)]


def test_no_user_facing_doc_constructs_a_dbless_client() -> None:
    """No doc a reader copies from may build a client on a db-less URL."""
    offenders: list[str] = []
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8")
        for match in _dbless_from_url_matches(text):
            line = text.count("\n", 0, match.start()) + 1
            url = match.group("url")
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}: {url!r}")

    assert not offenders, (
        "these docs construct a Redis client from a URL that names no "
        "database, which resolves to database 0 and opens a connection "
        "unrelated to Popoto's own (#645):\n  "
        + "\n  ".join(offenders)
        + "\nUse popoto.get_redis() instead, or name a database in the URL."
    )


def test_the_guard_scans_a_nonempty_doc_set() -> None:
    """The scan must actually cover the files this guard was written for.

    Without this, a rename or a moved directory leaves ``_scanned_files()``
    empty and the check above passes while inspecting nothing — the vacuity
    mode PR #643's reviewer caught in the sibling guard.
    """
    scanned = _scanned_files()
    assert len(scanned) > 1, (
        f"only {len(scanned)} doc file(s) found under {DOCS_DIR}; the guard "
        "would pass without inspecting the documentation"
    )

    missing = [p for p in REQUIRED_FILES if p not in scanned]
    assert not missing, (
        "the docs this guard exists for are not in the scanned set: "
        + ", ".join(str(p.relative_to(REPO_ROOT)) for p in missing)
        + ". If they moved, update REQUIRED_FILES; do not delete the check."
    )


def test_the_matcher_detects_a_dbless_url() -> None:
    """Positive control: the pattern fires on the exact shape #645 removed.

    A subtly wrong regex is indistinguishable from a clean tree, so the matcher
    is exercised against a known-bad and a known-good snippet here rather than
    trusted because the scan came back empty.
    """
    bad = 'r = redis.from_url("redis://localhost:6379")'
    assert _dbless_from_url_calls(bad) == ["redis://localhost:6379"]

    # A trailing slash still names no database.
    assert _dbless_from_url_calls('from_url("redis://h:6379/")') == ["redis://h:6379/"]

    # Split across lines — the reason the pattern is not line-anchored.
    split = 'redis.from_url(\n    "redis://localhost:6379"\n)'
    assert _dbless_from_url_calls(split) == ["redis://localhost:6379"]

    # Named databases are fine, and must not be reported.
    assert _dbless_from_url_calls('from_url("redis://h:6379/15")') == []
    assert _dbless_from_url_calls('from_url("rediss://h:6379/0")') == []


def test_the_excluded_directories_are_excluded_on_purpose() -> None:
    """The archive is skipped by design, and still contains the old shape.

    If ``docs/plans/`` ever stops containing a db-less URL, the exclusion has
    become untestable rather than merely unnecessary — and a future reader
    should not have to guess whether skipping it was deliberate.
    """
    if not (DOCS_DIR / "plans").is_dir():
        pytest.skip("no docs/plans/ archive in this checkout")

    scanned = _scanned_files()
    assert not any(p.is_relative_to(DOCS_DIR / "plans") for p in scanned)
