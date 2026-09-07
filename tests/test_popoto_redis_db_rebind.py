"""``popoto.POPOTO_REDIS_DB`` must be the current connection, not a snapshot.

``src/popoto/__init__.py`` used to do ``from .redis_db import POPOTO_REDIS_DB``,
which copies the *name* into the package namespace at import time.
``set_REDIS_DB_settings()`` rebinds ``redis_db``'s own module global, and Python
does not propagate that rebind back to the already-imported copy. So after any
reconfiguration ``popoto.POPOTO_REDIS_DB`` pointed at the *previous* database
while ``redis_db.get_REDIS_DB()`` pointed at the new one, and a caller using the
package attribute wrote to one database and read from another in silence (#651).

The name is now served by a PEP 562 module ``__getattr__`` instead of a real
binding, so it resolves live on every access. That is the same defect and the
same remedy as #645, one level down: there ``popoto.get_redis()`` was the stale
accessor, and ``tests/test_get_redis_rebind.py`` covers it.

Never uses database 0: it is a live store on developer machines.

Restoration note: the rebind is undone by re-assigning the *original client
object* to ``redis_db.POPOTO_REDIS_DB``, not by calling
``set_REDIS_DB_settings()`` a second time. That function builds a fresh pool from
the kwargs it is given, so a restore call passing only ``db=`` would silently
drop host/port and leave the session's global connection subtly different from
the one the pytest plugin set up — which broke
``test_pytest_plugin.py::TestAuthPreservation`` when the sibling #645 test ran
first.
"""

import popoto
import pytest
from popoto import redis_db


def _bound_db(client):
    return client.connection_pool.connection_kwargs.get("db")


def test_popoto_redis_db_follows_a_set_redis_db_settings_rebind():
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
        assert _bound_db(popoto.POPOTO_REDIS_DB) == target
    finally:
        redis_db.POPOTO_REDIS_DB = original_client

    assert popoto.POPOTO_REDIS_DB is original_client
    assert _bound_db(popoto.POPOTO_REDIS_DB) == original


def test_popoto_redis_db_agrees_with_the_redis_db_accessor():
    assert popoto.POPOTO_REDIS_DB is redis_db.get_REDIS_DB()
    assert popoto.POPOTO_REDIS_DB is popoto.get_redis()


def test_the_name_is_not_a_real_module_attribute():
    """The hook only fires while nothing binds the name in the namespace.

    A future edit re-adding ``from .redis_db import POPOTO_REDIS_DB`` to
    ``__init__.py`` would shadow ``__getattr__`` permanently and silently
    restore #651, without failing any behavioural assertion above as long as
    the test session never reconfigures. This asserts the mechanism directly.
    """
    assert "POPOTO_REDIS_DB" not in vars(popoto)


def test_import_surfaces_are_unchanged():
    """PEP 562 must not regress the introspection the plain re-export gave.

    ``dir()`` is the one surface that does not come for free: a name served by
    ``__getattr__`` lives in no module ``__dict__``, so ``popoto.__dir__`` has
    to list it explicitly.
    """
    assert hasattr(popoto, "POPOTO_REDIS_DB")
    assert "POPOTO_REDIS_DB" in dir(popoto)

    from popoto import POPOTO_REDIS_DB

    assert POPOTO_REDIS_DB is redis_db.get_REDIS_DB()

    # Never exported by ``from popoto import *`` before this change either, and
    # this fix must not widen the declared public surface.
    assert "POPOTO_REDIS_DB" not in popoto.__all__


def test_unknown_attribute_still_raises_attribute_error():
    """The hook must re-raise, not swallow, so typos stay loud."""
    with pytest.raises(AttributeError, match="no attribute 'NoSuchName'"):
        popoto.NoSuchName
