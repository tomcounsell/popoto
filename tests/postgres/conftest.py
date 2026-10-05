"""Fixtures for the ``[PG-only]`` tests (#759 M1b, plan §5 "[PG-only] tests").

These have no Redis leg. A test that needs a server takes ``pg``: the
session's ``popoto_test_<hex>`` schema (from the conformance harness, so it
needs ``POSTGRES_URL`` and skips without it), emptied of tables, with its
``PostgresBackend`` bound as the process default and as the instance
``Meta.backend = "postgres"`` resolves to, for the duration of the test.

The session backend is shared across tests (one pool per session), and so,
before #759's CI fix, was its ``health`` record: one test's outage -- a
genuine one, or a contention wait misread as one -- left ``dropped_writes``
at 1 for every later test asserting ``== 0``, and one failure on CI became
thirteen. ``pg`` hands each test a fresh :class:`Health` record, so a test's
assertions about health count its own operations and nothing else, in any
order.
"""

import pytest


@pytest.fixture
def pg(request):
    schema = request.getfixturevalue("popoto_postgres_schema")
    schema.drop_tables()
    backend = schema.backend()
    backend.forget_tables()
    from popoto.backends import _swap_instance, set_backend
    from popoto.backends.postgres import Health

    session_health = backend.health
    backend.health = Health()  # this test's own record (see the docstring)
    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)
    try:
        yield backend
    finally:
        _swap_instance("postgres", previous_instance)
        set_backend(previous)
        backend.health = session_health


@pytest.fixture
def pg_schema(request):
    """The harness schema object (admin ``connect()``, ``name``)."""
    return request.getfixturevalue("popoto_postgres_schema")


@pytest.fixture
def admin(pg_schema):
    conn = pg_schema.connect()
    try:
        yield conn
    finally:
        conn.close()
