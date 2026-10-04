"""Fixtures for the ``[PG-only]`` tests (#759 M1b, plan §5 "[PG-only] tests").

These have no Redis leg. A test that needs a server takes ``pg``: the
session's ``popoto_test_<hex>`` schema (from the conformance harness, so it
needs ``POSTGRES_URL`` and skips without it), emptied of tables, with its
``PostgresBackend`` bound as the process default and as the instance
``Meta.backend = "postgres"`` resolves to, for the duration of the test.
"""

import pytest


@pytest.fixture
def pg(request):
    schema = request.getfixturevalue("popoto_postgres_schema")
    schema.drop_tables()
    backend = schema.backend()
    backend.forget_tables()
    from popoto.backends import _swap_instance, set_backend

    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)
    try:
        yield backend
    finally:
        _swap_instance("postgres", previous_instance)
        set_backend(previous)


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
