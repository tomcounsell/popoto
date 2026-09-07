#!/usr/bin/env python3
"""Import every third-party package the root ``uv.lock`` provides via an extra.

``uv lock --check`` proves the lock is *consistent* with ``pyproject.toml``.
It does not prove the locked set is *installable*, and nothing else in CI
installs it: ``tests.yml`` and ``lint.yml`` both ``pip install -e`` from
``pyproject.toml`` floors, so every ``lockfile-only`` Dependabot PR touches a
file no job exercises (#669). ``lock-check.yml`` now runs ``uv sync --locked``
and then this script, which is what turns a green check on such a PR into a
statement about the diff under review.

Three things about this file are load-bearing:

1. **It imports the third-party packages directly, and never imports popoto.**
   Every popoto module that uses an extra guards the import
   (``try: import anthropic / except ImportError``), so it imports cleanly
   whether the package is present, absent, or broken. Probing popoto's own
   modules would therefore be green by construction. Because popoto is never
   imported, no Redis client is ever bound and no ``REDIS_URL`` discipline
   applies here.

2. **PACKAGES is written out by hand, not derived from ``pyproject.toml``.**
   A parser would turn "someone added an extra and forgot this list" from a
   visible diff into silence, and would still need the dist-name/import-name
   mapping below. Nothing in CI can catch an omission here, so **treat a
   mismatch between this list and ``[project.optional-dependencies]`` as
   review-blocking.**

3. **The ``benchmark`` extra is deliberately absent.** ``lock-check.yml``
   syncs with ``--no-extra benchmark`` to avoid pulling torch-sized wheels;
   those packages are already installed from floors by both ``tests.yml`` jobs,
   so their installability is covered there.

What a green run does NOT prove: that a bumped package still *works*. The
``anthropic`` 0.120.2 -> 1.2.0 major bump imports cleanly on both sides, and
``anthropic.Anthropic`` and ``client.messages.create`` both still exist under
1.2.0. Detecting that class of break needs a live API call. See CLAUDE.md.
"""

from __future__ import annotations

import importlib
import sys

# (import name, owning extra). Import names differ from distribution names for
# several of these: ulid-py -> ulid, sentry-sdk -> sentry_sdk,
# msgpack-numpy -> msgpack_numpy, voyageai is one word.
PACKAGES: list[tuple[str, str]] = [
    ("pandas", "dataframe"),
    ("numpy", "dataframe"),
    ("msgpack_numpy", "dataframe"),
    ("ulid", "ulid"),
    ("cyksuid", "ksuid"),
    ("voyageai", "voyage"),
    ("openai", "openai"),
    ("anthropic", "anthropic"),
    ("sentry_sdk", "monitoring"),
    ("mcp", "mcp"),
]


def check(specs: list[tuple[str, str]]) -> list[str]:
    """Import each ``(module, extra)`` pair; return one message per failure.

    Every spec is attempted even after a failure, so one broken extra does not
    hide the rest.
    """
    failures: list[str] = []
    for module_name, extra in specs:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # ImportError, but a broken package can raise anything
            failures.append(f"{module_name} (extra: {extra}) -- {exc!r}")
            continue
        version = getattr(module, "__version__", "unknown")
        print(f"  ok  {module_name:<16} {version:<12} (extra: {extra})")
    return failures


def main() -> int:
    print(f"Importing {len(PACKAGES)} locked packages on {sys.version.split()[0]}:")
    failures = check(PACKAGES)
    if failures:
        print(f"\nFAILED to import {len(failures)} package(s):")
        for failure in failures:
            print(f"  !!  {failure}")
        return 1
    print("\nAll locked extras import.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
