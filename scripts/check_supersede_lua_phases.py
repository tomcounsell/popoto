#!/usr/bin/env python
"""Executable anti-criterion for SUPERSEDE_LUA's validation/mutation split (#588).

Redis Lua has no rollback, so ``SUPERSEDE_LUA`` gets its all-or-nothing property
from *ordering*: every check that can return an ``error_reply`` runs before the
first write command. This script pins that ordering as a machine-checkable rule
rather than a comment nobody re-reads.

It prints ``BAD: <reason>`` and exits 1 when:

* the ``-- MUTATION PHASE`` marker is missing;
* any writing ``redis.call`` (``ZADD``/``HSET``/``SET``/``ZREM``/``HDEL``/``DEL``)
  appears above the marker;
* the first ``EXISTS`` is not inside the ``mode ~= 'open'`` block -- gating mode
  ``'open'`` would fail every save on a model whose hash is written after the
  field hooks (plan Risk 1).

It prints ``OK`` and exits 0 otherwise.

Run from the repo root: ``python scripts/check_supersede_lua_phases.py``
"""

import pathlib
import re
import sys

SOURCE = pathlib.Path(__file__).resolve().parent.parent / (
    "src/popoto/fields/validity_field.py"
)

MARKER = "-- MUTATION PHASE"
WRITE_COMMANDS = ("ZADD", "HSET", "SET", "ZREM", "HDEL", "DEL")


def extract_script(text: str) -> str:
    """Return the SUPERSEDE_LUA body, or raise if it cannot be found."""
    marker = 'SUPERSEDE_LUA = """'
    start = text.index(marker) + len(marker)
    return text[start : text.index('"""', start)]


def check(body: str) -> "list[str]":
    """Return a list of violation strings; empty means the rule holds."""
    problems = []
    if MARKER not in body:
        return ["no MUTATION PHASE marker"]

    head, _, _tail = body.partition(MARKER)

    for lineno, line in enumerate(head.splitlines(), 1):
        code = line.split("--", 1)[0]
        for command in WRITE_COMMANDS:
            if re.search(r"redis\.call\(\s*'%s'" % command, code):
                problems.append(
                    f"write command {command} at line {lineno} of the "
                    "validation phase (above the MUTATION PHASE marker)"
                )

    exists_match = re.search(r"redis\.call\(\s*'EXISTS'", body)
    if exists_match is None:
        problems.append("no EXISTS membership guard in the script")
    else:
        mode_block = body.find("if mode ~= 'open' then")
        if mode_block == -1:
            problems.append("no `mode ~= 'open'` block")
        elif exists_match.start() < mode_block:
            problems.append(
                "the first EXISTS is outside the `mode ~= 'open'` block; "
                "gating mode 'open' would fail every save (Risk 1)"
            )
    return problems


def main() -> int:
    problems = check(extract_script(SOURCE.read_text()))
    if problems:
        for problem in problems:
            print(f"BAD: {problem}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
