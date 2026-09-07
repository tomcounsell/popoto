"""Shared fixtures for the Popoto Kitchen smoke test.

The Redis binding is deliberately NOT set here. Popoto ships a `pytest11`
entry-point plugin whose `pytest_configure` hook imports `popoto.redis_db`
before pytest collects any test module, and `REDIS_URL` is read at that import.
By the time a fixture runs, the connection is already bound — setting the
variable here would be a no-op that reads as a safety net. So this module only
*asserts* the binding, and the caller (CI job env, or your shell) must provide
it:

    REDIS_URL=redis://localhost:6379/13 pytest

Database 0 is refused outright: on a developer machine it is a live store, and
this suite seeds and clears data.
"""

import os
from urllib.parse import urlparse

import pytest


def _redis_db_from_env() -> int | None:
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    path = urlparse(url).path.lstrip("/")
    if not path:
        return 0  # a db-less URL resolves to database 0
    try:
        return int(path)
    except ValueError:
        return None


@pytest.fixture(scope="session", autouse=True)
def require_isolated_redis() -> int:
    """Fail loudly unless REDIS_URL names a non-zero database."""
    db = _redis_db_from_env()
    if db is None:
        pytest.fail(
            "REDIS_URL must be set to an explicit test database before pytest "
            "starts, e.g. REDIS_URL=redis://localhost:6379/13 pytest. "
            "It cannot be set from a fixture: popoto's pytest plugin binds the "
            "connection during pytest_configure, before collection."
        )
    if db == 0:
        pytest.fail(
            f"REDIS_URL points at database 0 ({os.environ['REDIS_URL']!r}). "
            "This suite seeds and clears data; refusing to touch database 0."
        )
    return db


@pytest.fixture(scope="session")
def seeded_kitchen(require_isolated_redis: int) -> dict[str, int]:
    """Seed a small, deterministic dataset and return the expected counts.

    Small on purpose: the smoke test asserts row counts, and the demo's default
    seed (100/500/50/2000) makes a slow test with nothing extra to prove.
    """
    import random

    from popoto_kitchen.seed import clear_database, seed_database

    random.seed(611)
    clear_database()
    counts = {
        "num_restaurants": 6,
        "num_customers": 8,
        "num_drivers": 4,
        "num_orders": 12,
    }
    seed_database(**counts)
    yield counts
    clear_database()
