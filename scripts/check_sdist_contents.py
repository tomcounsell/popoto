#!/usr/bin/env python3
"""Assert what a built sdist actually contains, before it reaches PyPI.

Run between ``python -m build`` and the publish step (see
``.github/workflows/release.yml``)::

    python scripts/check_sdist_contents.py dist/*.tar.gz

The glob is expanded by the *shell*, not by this script: ``sys.argv[1:]`` is
the authoritative, already-resolved list and is never re-globbed here.
Re-globbing an already-resolved filename is what silently narrows two stale
artifacts to one, since each literal path glob-matches only itself.

What a green run proves: no unexpected file reached the sdist's *member list*.
It says nothing about whether the packaged code works, whether the right code
was packaged, or whether the wheel — which is what almost every consumer
installs, and which this script does not inspect — is correct.

Rule severities are deliberately split (#678):

Hard failures (exit 1)
    * a member path with a non-ASCII character -- the one rule that maps to the
      setuptools advisory this script was written for: a filename with a single
      normalization form cannot collide with itself.
    * a dotfile at any depth (``.env``, ``.git``, CI config, credentials).
    * an absolute path or a ``..`` component.
    * a symlink or hardlink member. Checked on the member metadata, never by
      extracting: a link's target can escape the extraction root while the
      member's own name is clean ASCII with no ``..``.

Warning only (exit 0)
    * a top-level entry outside the known set. This is a hand-maintained list
      with nothing to enforce correspondence against. A ``MANIFEST.in`` now
      exists (#689), but it does not close that gap: it is a list of
      prune/include *directives*, not an enumeration of intended top-level
      membership, so there is still nothing to parse this set against. A
      legitimate packaging addition must never block a release under release
      pressure, so this prints and continues.

The non-ASCII rule became **load-bearing** in #689. When this script was
written there was no ``MANIFEST.in`` at all, so precondition 1 of the
setuptools advisory was absent and the rule was defense-in-depth against its
return. #689 added one (``prune tests``) because setuptools exposes no
pyproject.toml equivalent, which spends that trade deliberately. Do not
downgrade that rule to a warning: it is now the check that stands between an
exclusion bypass and PyPI.
"""

from __future__ import annotations

import pathlib
import sys
import tarfile

# The top-level entries an sdist built from this tree has. Hand-maintained by
# design -- see the module docstring for why this warns rather than fails.
#
# Recalibrated in #689, which added MANIFEST.in: `tests` left (the suite is no
# longer shipped) and `MANIFEST.in` arrived (a MANIFEST.in ships itself). Net
# membership went from 297 members to 131, all 166 of the removed ones under
# tests/; src/ is untouched at 123. Measured on setuptools 84.0.0.
EXPECTED_TOP_LEVEL = frozenset(
    {
        "LICENSE",
        "MANIFEST.in",
        "PKG-INFO",
        "README.md",
        "pyproject.toml",
        "setup.cfg",
        "src",
    }
)


def check_members(members: list[tarfile.TarInfo]) -> tuple[list[str], list[str]]:
    """Return ``(failures, warnings)`` for an sdist's member list.

    Split out from ``main`` so the rules are unit-testable against synthetic
    tarballs without building anything.
    """
    failures: list[str] = []
    warnings: list[str] = []
    seen_top_level: set[str] = set()

    for member in members:
        name = member.name

        if not name.isascii():
            failures.append(
                f"{name!r}: non-ASCII path -- a normalization-collision "
                f"candidate (see setuptools MANIFEST.in bypass advisory)"
            )

        parts = [p for p in name.split("/") if p]
        if any(p.startswith(".") for p in parts):
            failures.append(f"{name!r}: dotfile members are not permitted in the sdist")

        if name.startswith("/") or ".." in parts:
            failures.append(f"{name!r}: absolute or parent-traversing path")

        if member.issym() or member.islnk():
            failures.append(
                f"{name}: symlink/hardlink members are not permitted in the sdist"
            )

        if parts:
            seen_top_level.add(parts[0])

    # The archive root is the version-stamped directory (popoto-1.9.0/); the
    # entries we care about live one level in. Strip the root when there is
    # exactly one, which is the shape `python -m build` always produces.
    if len(seen_top_level) == 1:
        root = next(iter(seen_top_level))
        seen_top_level = {
            parts[1]
            for parts in (m.name.split("/") for m in members)
            if len(parts) > 1 and parts[0] == root and parts[1]
        }

    for entry in sorted(seen_top_level - EXPECTED_TOP_LEVEL):
        warnings.append(
            f"{entry!r}: unexpected top-level entry (not in the known set: "
            f"{', '.join(sorted(EXPECTED_TOP_LEVEL))})"
        )

    return failures, warnings


USAGE = """usage: check_sdist_contents.py <sdist.tar.gz>

Assert what a built sdist contains, before it reaches PyPI. Run between
`python -m build` and the publish step:

    python scripts/check_sdist_contents.py dist/*.tar.gz

The glob is expanded by the shell; this script never re-globs its arguments,
and exits non-zero on anything other than exactly one existing file.

Hard failures (exit 1):
  1. a member path with a non-ASCII character -- the rule that maps to the
     setuptools MANIFEST.in normalization-collision advisory (#678)
  2. a dotfile at any depth (.env, .git, CI config, credentials)
  3. an absolute path or a '..' component
  4. a symlink or hardlink member, checked on tar metadata, never extracted

Warning only (exit 0):
  5. a top-level entry outside the known set: %s

A green run proves the tarball's member list is clean. It does not prove the
packaged code works, that the right code was packaged, or that the wheel --
which this does not inspect -- is correct.
""" % ", ".join(sorted(EXPECTED_TOP_LEVEL))


def main(argv: list[str]) -> int:
    if any(arg in ("-h", "--help") for arg in argv[1:]):
        print(USAGE)
        return 0

    paths = argv[1:]

    # Two argument-resolution failures, both hard errors, never a vacuous pass.
    if len(paths) != 1:
        raise SystemExit(f"expected exactly one sdist, found {len(paths)}: {paths!r}")
    if not pathlib.Path(paths[0]).is_file():
        raise SystemExit(
            f"no sdist found matching {paths[0]!r} (dist/ may be empty or misnamed)"
        )

    with tarfile.open(paths[0]) as tar:
        members = tar.getmembers()

    failures, warnings = check_members(members)

    for warning in warnings:
        print(f"WARNING: {warning}")

    if failures:
        print(f"\nFAIL: {paths[0]} ({len(members)} members)")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"OK: {paths[0]} ({len(members)} members, {len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
