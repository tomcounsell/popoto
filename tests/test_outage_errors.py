"""The backend-neutral outage tuple, ``popoto.backends.OUTAGE_ERRORS`` (#816).

``BackendUnavailableError`` subclasses the *builtin* ``ConnectionError``, not
redis-py's, so the Redis pair in ``popoto.redis_db.OUTAGE_ERRORS`` never
matched a Postgres outage. Every "is this an outage?" decision in ``src/`` now
reads one tuple; the drift guard at the bottom keeps it that way.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import redis

import popoto
from popoto import backends
from popoto.backends import types as backend_types
from popoto.backends.types import (
    BackendBusyError,
    BackendRetryableError,
    BackendUnavailableError,
)
from popoto.recipes import context_assembler
from popoto.redis_db import OUTAGE_ERRORS as REDIS_OUTAGE_ERRORS

SRC_ROOT = Path(popoto.__file__).resolve().parent


@pytest.mark.parametrize(
    "error",
    [
        redis.exceptions.ConnectionError("refused"),
        redis.exceptions.TimeoutError("timed out"),
        BackendUnavailableError("postgres is down"),
    ],
    ids=["redis-connection", "redis-timeout", "backend-unavailable"],
)
def test_every_backend_outage_is_a_member(error):
    assert isinstance(error, backends.OUTAGE_ERRORS)


@pytest.mark.parametrize(
    "error",
    [
        BackendRetryableError("deadlock"),
        BackendBusyError("pool exhausted"),
        ValueError("bad query"),
    ],
    ids=["retryable", "busy", "value-error"],
)
def test_contention_and_bad_queries_are_not_outages(error):
    # Contention is safe to retry and is not an outage: recipes must keep
    # degrading on it, not re-raise it.
    assert not isinstance(error, backends.OUTAGE_ERRORS)


def test_redis_tuple_value_is_unchanged():
    assert REDIS_OUTAGE_ERRORS == (
        redis.exceptions.ConnectionError,
        redis.exceptions.TimeoutError,
    )
    assert not isinstance(BackendUnavailableError("down"), REDIS_OUTAGE_ERRORS)


def test_neutral_tuple_is_one_object_everywhere_it_is_importable():
    assert backends.OUTAGE_ERRORS is backend_types.OUTAGE_ERRORS
    assert context_assembler.OUTAGE_ERRORS is backends.OUTAGE_ERRORS
    assert "OUTAGE_ERRORS" in backends.__all__
    assert "OUTAGE_ERRORS" in backend_types.__all__


def _imports_redis_outage_tuple(tree: ast.AST) -> bool:
    """True when ``tree`` imports ``OUTAGE_ERRORS`` from ``redis_db``, in any
    spelling (``from ..redis_db``, ``from popoto.redis_db``, at any scope)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module.split(".")[-1] != "redis_db":
            continue
        if any(alias.name == "OUTAGE_ERRORS" for alias in node.names):
            return True
    return False


def test_no_src_module_but_backend_types_reads_the_redis_tuple():
    """Drift guard: the per-module widening is what let #816 happen.

    Only ``backends/types.py`` may build on the Redis pair; every other
    consumer imports the neutral tuple, so a new outage type is added once.
    """
    offenders = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path == SRC_ROOT / "backends" / "types.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _imports_redis_outage_tuple(tree):
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == []


def test_drift_guard_sees_the_shape_it_forbids():
    # The guard above passes vacuously if the matcher never fires; prove it
    # recognises both import spellings.
    for source in (
        "from ..redis_db import OUTAGE_ERRORS\n",
        "def f():\n    from popoto.redis_db import OUTAGE_ERRORS as X\n",
    ):
        assert _imports_redis_outage_tuple(ast.parse(source))
    assert not _imports_redis_outage_tuple(
        ast.parse("from ..backends.types import OUTAGE_ERRORS\n")
    )
