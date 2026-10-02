"""The first-connection DDL bootstrap is race-safe (#631 WS3b; PR #737 review).

``PostgresBackend._connection()`` runs ``SCHEMA_DDL`` -- a block of ``CREATE
TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` -- the first time an
instance opens a connection. Postgres's ``IF NOT EXISTS`` is *not* race-safe:
two sessions that both find the table absent both try to create it, and the
loser fails on the catalog's unique index (``UniqueViolation:
pg_type_typname_nsp_index``) or with ``DuplicateTable``. WS3a's reviewer saw
2 of 50 first connections fail that way on a fresh schema. The harness never
hits it (the fixture's first instance bootstraps serially) but a
multi-process deployment on an empty schema would.

The fix is one transaction-scoped advisory lock around the block, keyed per
schema, so concurrent bootstraps queue and every later one finds the tables
already there. The test below opens N first connections through a barrier
against a schema that has *no* tables yet. Measured before the fix on this
tree: 7 of 12 threads raised ``UniqueViolation``. After: 0, and the catalog
holds each table and the index exactly once.

Postgres-leg only; it builds its own empty ``popoto_test_<hex>`` schema (the
session schema is already bootstrapped) and drops it before returning, so the
"0 ``popoto_test_%`` schemas afterwards" gate still holds. Never touches
``public``.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from popoto.backends.postgres import SCHEMA_DDL, PostgresBackend
from popoto.pytest_plugin import POSTGRES_TEST_SCHEMA_PREFIX, PostgresTestSchema

pytestmark = pytest.mark.conformance

EXPECTED_TABLES = {
    "popoto_record",
    "popoto_numeric",
    "popoto_set",
    "popoto_sorted",
    "popoto_map",
    "popoto_pointer",
}


@pytest.fixture
def empty_schema(request, backend, backend_is_redis):
    """A fresh schema with no tables, dropped on teardown (Postgres leg)."""
    if backend_is_redis:
        pytest.skip("Postgres-leg assertion: the DDL bootstrap is Postgres's")
    session_schema = request.getfixturevalue("popoto_postgres_schema")
    schema = PostgresTestSchema(
        name=f"{POSTGRES_TEST_SCHEMA_PREFIX}{uuid.uuid4().hex}",
        url=session_schema.url,
    )
    schema.create()
    try:
        yield schema
    finally:
        schema.drop()


def test_schema_ddl_names_every_table_once():
    """The DDL block the race test exercises creates exactly the tables the
    assertions below look for; a new table needs both lists updated."""
    created = {
        statement.split("IF NOT EXISTS")[1].split("(")[0].strip()
        for statement in SCHEMA_DDL
        if "CREATE TABLE" in statement
    }
    assert created == EXPECTED_TABLES


def test_concurrent_first_connections_bootstrap_one_schema(empty_schema):
    n = 12
    backends = [PostgresBackend(empty_schema.backend_url) for _ in range(n)]
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def open_first_connection(instance: PostgresBackend) -> None:
        barrier.wait(timeout=10.0)
        try:
            instance._connection()
        except BaseException as exc:  # surfaced by the assertion below
            errors.append(exc)

    threads = [
        threading.Thread(target=open_first_connection, args=(b,), name=f"boot-{i}")
        for i, b in enumerate(backends)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30.0)
        assert not any(t.is_alive() for t in threads), "a bootstrap thread hung"
        assert not errors, (
            f"{len(errors)} of {n} first connections failed: "
            f"{sorted({type(e).__name__ for e in errors})}; first: {errors[0]!r}"
        )
        for instance in backends:
            assert instance._conn is not None and not instance._conn.closed
        # Every connection is usable afterwards, and they all see one schema.
        for instance in backends:
            assert instance.sorted_count("boot:idx") == 0
        with empty_schema.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT tablename FROM pg_catalog.pg_tables "
                    "WHERE schemaname = %s",
                    (empty_schema.name,),
                )
            }
            indexes = [
                row[0]
                for row in conn.execute(
                    "SELECT indexname FROM pg_catalog.pg_indexes "
                    "WHERE schemaname = %s AND indexname = 'popoto_sorted_idx_score'",
                    (empty_schema.name,),
                )
            ]
        assert tables == EXPECTED_TABLES
        assert indexes == ["popoto_sorted_idx_score"]
    finally:
        for instance in backends:
            instance.close()


def test_a_failed_bootstrap_leaves_no_connection_behind(empty_schema, monkeypatch):
    """When the DDL block raises, the connection it was opened on is closed
    and ``_conn`` stays unset, so the next call retries from scratch instead
    of leaving a connection for ``weakref.finalize`` (PR #737 review)."""
    import psycopg

    from popoto.backends import postgres as postgres_module

    instance = PostgresBackend(empty_schema.backend_url)
    opened: list[psycopg.Connection] = []
    real_connect = psycopg.connect

    def recording_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(psycopg, "connect", recording_connect)
    monkeypatch.setattr(
        postgres_module, "SCHEMA_DDL", ("CREATE TABLE popoto_record (",) + SCHEMA_DDL
    )
    with pytest.raises(psycopg.errors.SyntaxError):
        instance._connection()
    assert instance._conn is None
    assert len(opened) == 1 and opened[0].closed
    monkeypatch.setattr(postgres_module, "SCHEMA_DDL", SCHEMA_DDL)
    try:
        assert instance.sorted_count("boot:idx") == 0
        assert len(opened) == 2 and not opened[1].closed
    finally:
        instance.close()
