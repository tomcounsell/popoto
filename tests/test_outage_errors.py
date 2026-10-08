"""One backend-neutral outage tuple (#816).

``popoto.backends.OUTAGE_ERRORS`` is the single answer to "is this an outage?":
Redis's ``ConnectionError``/``TimeoutError`` plus the backend-neutral
``BackendUnavailableError`` a Postgres-bound model raises. Before #816 only
``ContextAssembler`` widened the Redis pair; ``SubconsciousMemory``, the
integrations service and ``popoto-transfer`` used the Redis-only tuple, so a
Postgres outage read as "no memories". The drift guard below fails if any
module goes back to importing the Redis pair for that decision.
"""

from __future__ import annotations

import ast
import pathlib

import redis

import popoto.backends
import popoto.backends.types
from popoto import redis_db
from popoto.backends import (
    BackendBusyError,
    BackendRetryableError,
    BackendUnavailableError,
    MaintenanceIncompleteError,
    OUTAGE_ERRORS,
)

SRC = pathlib.Path(popoto.backends.__file__).resolve().parents[1]
HOME = SRC / "backends" / "types.py"


def test_neutral_tuple_is_the_redis_pair_plus_backend_unavailable():
    assert OUTAGE_ERRORS == (
        redis.exceptions.ConnectionError,
        redis.exceptions.TimeoutError,
        BackendUnavailableError,
    )
    assert OUTAGE_ERRORS is popoto.backends.types.OUTAGE_ERRORS
    assert "OUTAGE_ERRORS" in popoto.backends.__all__
    assert "OUTAGE_ERRORS" in popoto.backends.types.__all__
    assert isinstance(BackendUnavailableError("down"), OUTAGE_ERRORS)


def test_redis_tuple_value_is_unchanged():
    assert redis_db.OUTAGE_ERRORS == (
        redis.exceptions.ConnectionError,
        redis.exceptions.TimeoutError,
    )
    assert not isinstance(BackendUnavailableError("down"), redis_db.OUTAGE_ERRORS)


def test_contention_is_not_an_outage():
    for exc_type in (
        BackendRetryableError,
        BackendBusyError,
        MaintenanceIncompleteError,
    ):
        assert not issubclass(exc_type, OUTAGE_ERRORS), exc_type


def test_every_consumer_shares_the_one_tuple():
    from popoto.integrations import service
    from popoto.recipes import context_assembler, subconscious_memory

    for module in (context_assembler, subconscious_memory, service):
        assert module.OUTAGE_ERRORS is OUTAGE_ERRORS, module.__name__


def _imports_redis_outage_tuple(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if node.module.split(".")[-1] != "redis_db":
            continue
        if any(alias.name == "OUTAGE_ERRORS" for alias in node.names):
            return True
    return False


def test_no_module_but_the_home_imports_the_redis_tuple():
    """Drift guard: the Redis pair is imported by ``backends/types.py`` (to
    build the neutral tuple) and by no other module under ``src/popoto``."""
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if path != HOME
        and _imports_redis_outage_tuple(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []
    assert _imports_redis_outage_tuple(ast.parse(HOME.read_text(encoding="utf-8")))


def test_drift_guard_detects_the_old_import_shape():
    """Non-vacuity: the detector flags the shape the consumers used to have."""
    assert _imports_redis_outage_tuple(
        ast.parse("from ..redis_db import OUTAGE_ERRORS\n")
    )
    assert _imports_redis_outage_tuple(
        ast.parse("from popoto.redis_db import OUTAGE_ERRORS as X\n")
    )
    assert not _imports_redis_outage_tuple(
        ast.parse("from ..backends.types import OUTAGE_ERRORS\n")
    )
