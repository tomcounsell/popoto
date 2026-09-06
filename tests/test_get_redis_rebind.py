"""``popoto.get_redis()`` must return the current connection, not a snapshot.

``src/popoto/__init__.py`` does ``from .redis_db import POPOTO_REDIS_DB`` at
import time, which copies the *name* into the package namespace.
``set_REDIS_DB_settings()`` rebinds ``redis_db``'s own module global, and Python
does not propagate that rebind back to the already-imported copy. Before #645
``get_redis()`` returned the stale copy, so a caller who reconfigured got a
client pointed at the previous database — which is exactly the "reads land on a
different database than the writes" failure that
``docs/features/confidence-field.md`` now tells readers ``get_redis()`` prevents.

Never uses database 0: it is a live store on developer machines.

Restoration note: the rebind is undone by re-assigning the *original client
object*, not by calling ``set_REDIS_DB_settings()`` a second time. That function
builds a fresh pool from the kwargs it is given, so a restore call that passed
only ``db=`` would silently drop host/port and leave the session's global
connection subtly different from the one the pytest plugin set up — which broke
``test_pytest_plugin.py::TestAuthPreservation`` when this test ran first.
"""

import popoto
from popoto import redis_db


def _bound_db(client):
    return client.connection_pool.connection_kwargs.get("db")


def test_get_redis_follows_a_set_redis_db_settings_rebind():
    original_client = redis_db.get_REDIS_DB()
    original_kwargs = original_client.connection_pool.connection_kwargs
    original = _bound_db(original_client)
    assert original != 0, (
        "refusing to run against database 0; the pytest plugin should have "
        f"swapped to a test database but the client reports db={original!r}"
    )

    # Pick a target that is definitely a change, and never 0.
    target = 7 if original != 7 else 8
    # Carry host/port/auth across so the temporary client differs only in db.
    kwargs = redis_db.sibling_client_kwargs(original_kwargs, db=target)

    try:
        redis_db.set_REDIS_DB_settings(**kwargs)
        assert _bound_db(popoto.get_redis()) == target
    finally:
        redis_db.POPOTO_REDIS_DB = original_client

    assert popoto.get_redis() is original_client
    assert _bound_db(popoto.get_redis()) == original


def test_get_redis_agrees_with_the_redis_db_accessor():
    assert popoto.get_redis() is redis_db.get_REDIS_DB()
