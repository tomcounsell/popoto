"""Source-shape guard: no test may set ``FORGET_IDLE_SECONDS`` to a
non-negative literal.

Forget eligibility is a strict inequality --
``src/popoto/recipes/memory_lifecycle.py``::

    idle = _get_idle_seconds(record)
    if idle <= lifecycle.FORGET_IDLE_SECONDS:
        return False

A record a test just saved reports an idle time of essentially ``0.0``, so
``FORGET_IDLE_SECONDS = 0.0`` -- which *reads* as "no idle requirement" -- in
fact means "no freshly-written record can ever qualify". Every guard downstream
of that early return becomes unreachable, and a test aimed at one of them passes
without running it. Five tests in ``tests/test_memory_lifecycle.py`` were in that
state; all five stayed green with the guard they targeted deleted outright
(#674).

The value that means "any idle time qualifies" is ``-1.0``.

This cannot be caught by a behavioral test. A vacuous test and a passing test are
indistinguishable at runtime -- that indistinguishability *is* the defect. So the
check is on source shape, like ``tests/test_type_checking_guard.py`` and
``tests/test_docs_redis_url.py``.

**Opt-out.** A test that deliberately wants a non-negative floor -- one that
sleeps to make a record genuinely idle, say, and wants to pin the boundary
itself -- marks the line with a trailing ``# noqa: forget-idle`` comment. Use it
only where the test proves the record clears the gate by some other means.
"""

import ast
import pathlib

OPT_OUT_MARKER = "# noqa: forget-idle"

TESTS_DIR = pathlib.Path(__file__).parent


def _offending_assignments(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return (lineno, source-line) for each non-negative FORGET_IDLE_SECONDS set."""
    source = path.read_text()
    lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        # Only attribute assignments, e.g. `lifecycle.FORGET_IDLE_SECONDS = ...`
        targets = [
            t
            for t in node.targets
            if isinstance(t, ast.Attribute) and t.attr == "FORGET_IDLE_SECONDS"
        ]
        if not targets:
            continue

        value = node.value
        # A unary minus makes the value negative, which is the correct shape.
        if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.USub):
            continue
        # Anything not a plain numeric literal (a name, a call, an expression)
        # is beyond static reach -- leave it alone rather than guess.
        if not isinstance(value, ast.Constant) or not isinstance(
            value.value, (int, float)
        ):
            continue
        if value.value < 0:
            continue

        line = lines[node.lineno - 1]
        if OPT_OUT_MARKER in line:
            continue
        found.append((node.lineno, line.strip()))

    return found


def test_no_test_sets_forget_idle_seconds_non_negative():
    """A non-negative FORGET_IDLE_SECONDS makes a fresh record ineligible."""
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        for lineno, line in _offending_assignments(path):
            offenders.append(f"{path.relative_to(TESTS_DIR.parent)}:{lineno}: {line}")

    assert not offenders, (
        "FORGET_IDLE_SECONDS set to a non-negative literal. The condition is "
        "`idle > FORGET_IDLE_SECONDS`, so a freshly-saved record (idle 0) does "
        "not qualify and the forget path is never entered -- the test passes "
        "without running what it targets (#674). Use -1.0, or mark the line "
        f"`{OPT_OUT_MARKER}` if the record is made genuinely idle by other "
        "means.\n  " + "\n  ".join(offenders)
    )


def test_guard_detects_a_planted_zero(tmp_path):
    """The scan itself is not vacuous: it flags 0.0 and accepts -1.0."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "lifecycle.FORGET_IDLE_SECONDS = 0.0\n"
        "other.FORGET_IDLE_SECONDS = -1.0\n"
        "opted.FORGET_IDLE_SECONDS = 0.0  # noqa: forget-idle\n"
        "lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0\n"
    )
    found = _offending_assignments(planted)
    assert [lineno for lineno, _ in found] == [1], found
