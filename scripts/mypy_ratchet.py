#!/usr/bin/env python3
"""Mypy ratchet gate for src/popoto (#506).

The contract, in three lines:

1. The total mypy error count must be at or below the baseline recorded in
   ``scripts/mypy_baseline.json``. Over baseline fails; under baseline passes and
   emits a GitHub Actions warning naming the ``--update`` command.
2. Every package listed in the baseline's ``clean`` allowlist must be at exactly
   zero errors. A regression there fails even when the total is on baseline.
3. The running environment must match the baseline's recorded one, because
   ``setup.cfg`` sets ``ignore_missing_imports = True`` and the count moves with
   the mypy version, the redis-py major, and which optional packages are
   installed. On a mismatch the script refuses to compare and exits 0, or exits 1
   under ``--strict-env``.

Mypy is invoked exactly once. Per-package counts are derived by filtering that
single run's output by path prefix, never by N scoped runs: ``follow_imports =
silent`` means a scoped run silences diagnostics in modules the scope pulls in,
so its count is not a subset of the full run's.

A parse failure is never reported as zero errors. A gate that passes because the
checker did not run is worse than no gate.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "scripts" / "mypy_baseline.json"
SRC_TARGET = "src/"
PACKAGE_PREFIX = "src/popoto/"
ROOT_BUCKET = "(root)"

# "src/popoto/models/base.py:12: error: message  [code]"
ERROR_LINE_RE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+):(?:\d+:)? error: ")
# "Found 12 errors in 3 files (checked 98 source files)"
FOUND_RE = re.compile(r"^Found (?P<count>\d+) errors? in \d+ files?")
SUCCESS_RE = re.compile(r"^Success: no issues found")


class RatchetError(Exception):
    """A condition that must never be silently treated as zero errors."""


def _tool_version(module: str) -> str:
    """Return an installed distribution's version, or 'missing'."""
    try:
        from importlib.metadata import version

        return version(module)
    except Exception:
        return "missing"


def current_environment() -> dict[str, str]:
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "mypy": _tool_version("mypy"),
        "redis": _tool_version("redis"),
    }


def run_mypy() -> str:
    """Run ``mypy src/`` once and return stdout.

    Mypy's exit codes: 0 clean, 1 errors found, 2 internal error / bad usage.
    Only 0 and 1 are valid input to the comparison; 2 is a hard failure and must
    never be conflated with "errors found".
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "mypy", SRC_TARGET],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
    except OSError as exc:  # pragma: no cover - environment failure
        raise RatchetError(f"could not start mypy: {exc}") from exc

    if proc.returncode not in (0, 1):
        raise RatchetError(
            f"mypy exited {proc.returncode} (internal error or bad usage), "
            f"not a normal 'errors found' exit.\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc.stdout


def parse_output(stdout: str) -> tuple[int, dict[str, int], list[str]]:
    """Parse mypy stdout into (total, per-package counts, error lines).

    The total comes from mypy's own summary line, not from counting matched
    error lines, so a parser that silently stops matching cannot report a low
    number. The two are cross-checked and disagreement is a hard failure.
    """
    if not stdout.strip():
        raise RatchetError(
            "mypy produced no output. This is a parse failure, not zero errors."
        )

    lines = stdout.splitlines()
    error_lines: list[str] = []
    packages: dict[str, int] = {}
    summary_total: int | None = None

    for line in lines:
        if SUCCESS_RE.match(line):
            summary_total = 0
            continue
        found = FOUND_RE.match(line)
        if found:
            summary_total = int(found.group("count"))
            continue
        match = ERROR_LINE_RE.match(line)
        if not match:
            continue
        error_lines.append(line)
        path = match.group("path")
        if path.startswith(PACKAGE_PREFIX):
            rest = path[len(PACKAGE_PREFIX) :]
            bucket = rest.split("/")[0] if "/" in rest else ROOT_BUCKET
        else:
            bucket = ROOT_BUCKET
        packages[bucket] = packages.get(bucket, 0) + 1

    if summary_total is None:
        raise RatchetError(
            "mypy output carried no 'Found N errors' or 'Success' summary line. "
            "Refusing to infer a count."
        )
    if summary_total != len(error_lines):
        raise RatchetError(
            f"parsed {len(error_lines)} error lines but mypy's summary says "
            f"{summary_total}. The parser and mypy disagree; refusing to compare."
        )
    return summary_total, packages, error_lines


def load_baseline(path: Path) -> dict:
    if not path.exists():
        raise RatchetError(f"baseline file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise RatchetError(f"baseline file is not valid JSON: {exc}") from exc

    total = data.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise RatchetError(
            f"baseline 'total' must be a non-negative integer, got {total!r}"
        )
    clean = data.get("clean", [])
    if not isinstance(clean, list) or not all(isinstance(c, str) for c in clean):
        raise RatchetError("baseline 'clean' must be a list of package names")
    if not isinstance(data.get("environment", {}), dict):
        raise RatchetError("baseline 'environment' must be an object")
    return data


def check_allowlist_names(clean: list[str]) -> list[str]:
    """Return allowlist entries that name no real package under src/popoto/."""
    return [
        name for name in clean if not (REPO_ROOT / "src" / "popoto" / name).is_dir()
    ]


def format_table(packages: dict[str, int], clean: list[str]) -> str:
    rows = sorted(packages.items(), key=lambda kv: (-kv[1], kv[0]))
    for name in clean:
        if name not in packages:
            rows.append((name, 0))
    width = max((len(n) for n, _ in rows), default=10)
    out = []
    for name, count in rows:
        mark = "  [clean]" if name in clean else ""
        out.append(f"  {name.ljust(width)}  {count:>5}{mark}")
    return "\n".join(out)


def write_baseline(
    path: Path, total: int, clean: list[str], packages: dict[str, int], data: dict
) -> None:
    data["total"] = total
    data["clean"] = sorted(clean)
    data["packages"] = dict(sorted(packages.items()))
    data["environment"] = {**data.get("environment", {}), **current_environment()}
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite the baseline from the current measurement",
    )
    parser.add_argument(
        "--strict-env",
        action="store_true",
        help="fail instead of refusing when the environment does not match",
    )
    parser.add_argument(
        "--strict-ratchet",
        action="store_true",
        help="also fail when the count is UNDER baseline (exact equality)",
    )
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    args = parser.parse_args(argv)

    try:
        baseline = load_baseline(args.baseline)
    except RatchetError as exc:
        if args.update and not args.baseline.exists():
            baseline = {"clean": [], "environment": {}}
        else:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    env_now = current_environment()
    env_base = baseline.get("environment", {})
    mismatch = {
        k: (env_base.get(k), v) for k, v in env_now.items() if env_base.get(k) != v
    }

    try:
        stdout = run_mypy()
        total, packages, error_lines = parse_output(stdout)
    except RatchetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    clean = baseline.get("clean", [])
    print(
        f"mypy errors by package (mypy {env_now['mypy']}, "
        f"redis-py {env_now['redis']}, python {env_now['python']}):"
    )
    print(format_table(packages, clean))
    print(f"\n  total: {total}")

    if args.update:
        write_baseline(args.baseline, total, clean, packages, baseline)
        print(f"\nbaseline updated: {args.baseline} -> total {total}")
        return 0

    if mismatch:
        detail = ", ".join(
            f"{k}: baseline {b!r}, running {r!r}" for k, (b, r) in mismatch.items()
        )
        print(
            f"\nEnvironment does not match the baseline ({detail}).\n"
            f"Counts measured under different versions are not comparable, so "
            f"this run does not compare against baseline {baseline['total']}.",
            file=sys.stderr,
        )
        if args.strict_env:
            print("--strict-env was passed: failing.", file=sys.stderr)
            return 1
        print("Refusing to compare (pass --strict-env to make this fatal).")
        return 0

    failed = False

    stale = check_allowlist_names(clean)
    if stale:
        print(
            f"\nStale allowlist entries name no package under src/popoto/: "
            f"{', '.join(sorted(stale))}",
            file=sys.stderr,
        )
        failed = True

    regressed = {p: packages.get(p, 0) for p in clean if packages.get(p, 0) > 0}
    if regressed:
        for pkg, count in sorted(regressed.items()):
            print(
                f"\nAllowlisted package '{pkg}' is pinned at zero but has "
                f"{count} error(s):",
                file=sys.stderr,
            )
            for line in error_lines:
                if line.startswith(f"{PACKAGE_PREFIX}{pkg}/"):
                    print(f"  {line}", file=sys.stderr)
        failed = True

    base_total = baseline["total"]
    if total > base_total:
        delta = total - base_total
        print(
            f"\nmypy error count {total} is ABOVE baseline {base_total} (+{delta}).",
            file=sys.stderr,
        )
        base_pkgs = baseline.get("packages", {})
        grew = {
            p: (base_pkgs.get(p, 0), c)
            for p, c in packages.items()
            if c > base_pkgs.get(p, 0)
        }
        if grew:
            for pkg, (was, now) in sorted(grew.items()):
                print(
                    f"  package '{pkg}': {was} -> {now} (+{now - was})",
                    file=sys.stderr,
                )
            print("\nError lines in the packages that grew:", file=sys.stderr)
            prefixes = tuple(
                f"{PACKAGE_PREFIX}{p}/" if p != ROOT_BUCKET else PACKAGE_PREFIX
                for p in grew
            )
            for line in error_lines:
                if line.startswith(prefixes):
                    print(f"  {line}", file=sys.stderr)
        else:
            print(
                "  (baseline records no per-package counts to attribute the "
                "increase; re-run with --update on a clean tree)",
                file=sys.stderr,
            )
        failed = True
    elif total < base_total:
        delta = base_total - total
        msg = (
            f"mypy error count {total} is below baseline {base_total} (-{delta}) "
            f"— run scripts/mypy_ratchet.py --update and commit "
            f"scripts/mypy_baseline.json"
        )
        # The ::warning:: prefix surfaces as a GitHub Actions annotation.
        print(f"\n::warning::{msg}")
        if args.strict_ratchet:
            print(
                "--strict-ratchet was passed: an unbanked improvement is a failure.",
                file=sys.stderr,
            )
            failed = True

    if failed:
        return 1
    print(f"\nOK: {total} errors, at or below baseline {base_total}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
