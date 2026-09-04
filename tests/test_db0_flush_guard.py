"""Tests for the destructive-flush guard in ``popoto.redis_db``.

Covers the pure predicate (``_flush_refusal_reason``), the db-resolution
helper (``_bound_db``), and the guarded sync/async clients and pipelines
(``GuardedRedis``, ``GuardedPipeline``, ``GuardedAsyncRedis``,
``GuardedAsyncPipeline``).

Safety: no test here connects a client bound to database 0. Database-0
coverage is proven with a real ``ConnectionPool``/``connection_kwargs`` whose
transport is monkeypatched to fail loudly if a command ever reaches it, or
through the ``_flush_refusal_reason`` predicate directly, which issues no
Redis command. Nothing in this file runs ``FLUSHDB``/``FLUSHALL`` against
database 0, and ``REDIS_URL`` is never set to a URL ending in ``/0``.
"""

import logging

import pytest
import redis

import popoto  # noqa: F401  (ensures popoto.redis_db is the canonical module)
from popoto import redis_db
from popoto.redis_db import (
    ALLOW_DB0_FLUSH_ENV,
    Db0FlushRefusedError,
    GuardedRedis,
    _bound_db,
    _check_flush,
    _flush_refusal_reason,
)
from popoto.pytest_plugin import _swap_db


class _FakeTransport:
    """A stand-in for a redis-py connection that fails loudly if touched."""

    def __getattr__(self, name):
        raise AssertionError(
            f"transport method {name!r} was called: a guarded command reached "
            "the socket instead of being refused before dispatch"
        )


def _never_dispatch(monkeypatch, client):
    """Make ``client`` raise loudly if it ever tries to talk to a server."""

    def _boom(*args, **kwargs):
        raise AssertionError(
            "Connection.send_command was called: a guarded command reached "
            "the transport instead of being refused before dispatch"
        )

    monkeypatch.setattr(
        client.connection_pool,
        "get_connection",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError(
                "connection_pool.get_connection was called: a guarded command "
                "reached the transport instead of being refused before dispatch"
            )
        ),
    )
    monkeypatch.setattr(client, "connection", None, raising=False)


def _db0_guarded_client():
    """A GuardedRedis bound to db 0, backed by a real (unconnected) pool."""
    pool = redis.ConnectionPool(host="127.0.0.1", port=6379, db=0)
    return GuardedRedis(connection_pool=pool)


# ---------------------------------------------------------------------------
# 1. Client-level refusal on db 0, without a live connection.
# ---------------------------------------------------------------------------


class TestClientLevelDb0Refusal:
    def test_flushdb_never_reaches_server(self, monkeypatch):
        client = _db0_guarded_client()
        _never_dispatch(monkeypatch, client)
        with pytest.raises(Db0FlushRefusedError):
            client.flushdb()

    def test_execute_command_str_flushdb_never_reaches_server(self, monkeypatch):
        client = _db0_guarded_client()
        _never_dispatch(monkeypatch, client)
        with pytest.raises(Db0FlushRefusedError):
            client.execute_command("FLUSHDB")

    def test_execute_command_bytes_flushdb_never_reaches_server(self, monkeypatch):
        client = _db0_guarded_client()
        _never_dispatch(monkeypatch, client)
        with pytest.raises(Db0FlushRefusedError):
            client.execute_command(b"FLUSHDB")


# ---------------------------------------------------------------------------
# 2. Sync pipeline off a db-0-bound GuardedRedis refuses at queue time.
# ---------------------------------------------------------------------------


class TestSyncPipelineDb0Refusal:
    def test_pipeline_flushdb_refused_at_queue_time_never_reaches_server(
        self, monkeypatch
    ):
        client = _db0_guarded_client()
        _never_dispatch(monkeypatch, client)
        pipe = client.pipeline()
        with pytest.raises(Db0FlushRefusedError):
            pipe.flushdb()

    def test_pipeline_execute_command_flushdb_refused_at_queue_time(self, monkeypatch):
        client = _db0_guarded_client()
        _never_dispatch(monkeypatch, client)
        pipe = client.pipeline()
        with pytest.raises(Db0FlushRefusedError):
            pipe.execute_command("FLUSHDB")


# ---------------------------------------------------------------------------
# 3 & 4. FLUSHALL refused / FLUSHDB permitted on a real db-4 client.
# ---------------------------------------------------------------------------


class TestRealDb4Client:
    def test_flushall_refused_on_db4_client_never_reaches_server(self, monkeypatch):
        pool = redis.ConnectionPool(host="127.0.0.1", port=6379, db=4)
        client = GuardedRedis(connection_pool=pool)
        _never_dispatch(monkeypatch, client)
        with pytest.raises(Db0FlushRefusedError):
            client.flushall()

    def test_flushdb_succeeds_on_real_db4_client(self):
        pool = redis.ConnectionPool(host="127.0.0.1", port=6379, db=4)
        client = GuardedRedis(connection_pool=pool)
        try:
            client.ping()
        except redis.exceptions.ConnectionError:
            pytest.skip("no live Redis on localhost:6379 for db-4 flushdb check")
        # Must not raise: db 4 is not database 0.
        assert client.flushdb() is True


# ---------------------------------------------------------------------------
# 5. Async client and pipeline, bound to db 0, without a live connection.
# ---------------------------------------------------------------------------


class TestAsyncDb0Refusal:
    @pytest.mark.asyncio
    async def test_async_flushdb_refused_never_reaches_server(self, monkeypatch):
        import redis.asyncio as aioredis
        from popoto.redis_db import GuardedAsyncRedis

        pool = aioredis.ConnectionPool(host="127.0.0.1", port=6379, db=0)
        client = GuardedAsyncRedis(connection_pool=pool)

        async def _boom(*args, **kwargs):
            raise AssertionError(
                "connection_pool.get_connection was awaited: a guarded async "
                "command reached the transport instead of being refused"
            )

        monkeypatch.setattr(client.connection_pool, "get_connection", _boom)
        with pytest.raises(Db0FlushRefusedError):
            await client.flushdb()
        await client.connection_pool.disconnect()

    @pytest.mark.asyncio
    async def test_async_pipeline_flushall_refused_at_queue_time(self, monkeypatch):
        import redis.asyncio as aioredis
        from popoto.redis_db import GuardedAsyncRedis

        pool = aioredis.ConnectionPool(host="127.0.0.1", port=6379, db=0)
        client = GuardedAsyncRedis(connection_pool=pool)

        async def _boom(*args, **kwargs):
            raise AssertionError(
                "connection_pool.get_connection was awaited: a guarded async "
                "pipeline command reached the transport instead of being "
                "refused at queue time"
            )

        monkeypatch.setattr(client.connection_pool, "get_connection", _boom)
        pipe = client.pipeline()
        with pytest.raises(Db0FlushRefusedError):
            pipe.flushall()
        await client.connection_pool.disconnect()


# ---------------------------------------------------------------------------
# 6. Predicate unit tests.
# ---------------------------------------------------------------------------


class TestPredicateUnitTests:
    def test_opt_in_permits_flushdb_and_flushall(self, monkeypatch):
        monkeypatch.setenv(ALLOW_DB0_FLUSH_ENV, "1")
        assert _flush_refusal_reason("FLUSHDB", 0) is None
        assert _flush_refusal_reason("FLUSHALL", 0) is None

    def test_opt_in_read_at_call_time(self, monkeypatch):
        # No env var set yet: refused.
        monkeypatch.delenv(ALLOW_DB0_FLUSH_ENV, raising=False)
        assert _flush_refusal_reason("FLUSHDB", 0) is not None
        # Set after import (this module was imported long before this test
        # runs): the predicate re-reads it at call time.
        monkeypatch.setenv(ALLOW_DB0_FLUSH_ENV, "true")
        assert _flush_refusal_reason("FLUSHDB", 0) is None

    def test_pool_with_no_db_key_treated_as_db_0(self):
        class _FakePool:
            connection_kwargs = {}

        class _FakeClient:
            connection_pool = _FakePool()

        assert _bound_db(_FakeClient()) == 0

    def test_execute_command_no_args_does_not_raise_index_error(self):
        client = _db0_guarded_client()
        assert _check_flush(client, ()) is None


# ---------------------------------------------------------------------------
# 7. Permitted-branch predicate checks.
# ---------------------------------------------------------------------------


class TestPredicatePermittedBranch:
    def test_flushdb_on_db4_is_permitted(self):
        assert _flush_refusal_reason("FLUSHDB", 4) is None

    def test_get_on_db0_is_permitted(self):
        assert _flush_refusal_reason("GET", 0) is None


# ---------------------------------------------------------------------------
# 8. Class persistence across the various construction paths.
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_popoto_redis_db():
    """set_REDIS_DB_settings/_swap_db mutate the module global; restore it."""
    original = redis_db.POPOTO_REDIS_DB
    yield
    redis_db.POPOTO_REDIS_DB = original


class TestClassPersistence:
    def test_kwargs_branch_is_guarded(self, restore_popoto_redis_db):
        redis_db.set_REDIS_DB_settings(host="127.0.0.1", port=6379, db=4)
        assert isinstance(redis_db.POPOTO_REDIS_DB, GuardedRedis)

    def test_positional_args_branch_is_guarded(self, restore_popoto_redis_db):
        redis_db.set_REDIS_DB_settings(
            "", "127.0.0.1", 6379, 4  # host, port, db positionally
        )
        assert isinstance(redis_db.POPOTO_REDIS_DB, GuardedRedis)

    def test_explicit_connection_pool_branch_is_guarded(self, restore_popoto_redis_db):
        pool = redis.ConnectionPool(host="127.0.0.1", port=6379, db=4)
        redis_db.set_REDIS_DB_settings(connection_pool=pool)
        assert isinstance(redis_db.POPOTO_REDIS_DB, GuardedRedis)

    def test_swap_db_preserves_guarded_class(self, restore_popoto_redis_db):
        _swap_db(4)
        assert isinstance(redis_db.POPOTO_REDIS_DB, GuardedRedis)


# ---------------------------------------------------------------------------
# 9. Message content.
# ---------------------------------------------------------------------------


class TestMessageContent:
    def test_message_names_command_db_and_env_var(self):
        msg = _flush_refusal_reason("FLUSHDB", 0, suggest=False)
        assert msg is not None
        assert "FLUSHDB" in msg
        assert "database 0" in msg
        assert ALLOW_DB0_FLUSH_ENV in msg

    def test_message_names_free_db_when_suggested(self, monkeypatch):
        import popoto.integrations.config as config_module

        monkeypatch.setattr(config_module, "suggest_free_db", lambda: 7)
        msg = _flush_refusal_reason("FLUSHDB", 0, suggest=True)
        assert msg is not None
        assert "/7" in msg

    def test_message_usable_when_suggest_free_db_returns_none(self, monkeypatch):
        import popoto.integrations.config as config_module

        monkeypatch.setattr(config_module, "suggest_free_db", lambda: None)
        msg = _flush_refusal_reason("FLUSHDB", 0, suggest=True)
        assert msg is not None
        assert "FLUSHDB" in msg
        assert ALLOW_DB0_FLUSH_ENV in msg

    def test_message_usable_when_suggest_free_db_raises(self, monkeypatch):
        import popoto.integrations.config as config_module

        def _raise():
            raise RuntimeError("boom")

        monkeypatch.setattr(config_module, "suggest_free_db", _raise)
        msg = _flush_refusal_reason("FLUSHDB", 0, suggest=True)
        assert msg is not None
        assert "FLUSHDB" in msg
        assert ALLOW_DB0_FLUSH_ENV in msg


# ---------------------------------------------------------------------------
# 10 & 11. Logging and exception hierarchy.
# ---------------------------------------------------------------------------


class TestExceptionBehavior:
    def test_construction_logs_error(self, caplog):
        with caplog.at_level(logging.ERROR, logger="POPOTO-REDIS_DB"):
            Db0FlushRefusedError("refused for testing")
        records = [
            r
            for r in caplog.records
            if r.name == "POPOTO-REDIS_DB" and r.levelno == logging.ERROR
        ]
        assert any("refused for testing" in r.message for r in records)

    def test_catchable_as_value_error(self):
        with pytest.raises(ValueError):
            raise Db0FlushRefusedError("refused for testing")


# ---------------------------------------------------------------------------
# 12. Documented limit: EVAL is not caught by the guard.
# ---------------------------------------------------------------------------


class TestDocumentedLimit:
    def test_eval_is_not_covered_by_the_guard(self):
        # Documented behavior only, verified via the predicate. Never
        # actually EVAL a script that flushes anything.
        assert _flush_refusal_reason("EVAL", 0) is None
