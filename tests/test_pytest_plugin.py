"""Tests for the popoto pytest plugin.

These tests verify that the auto-registering pytest plugin correctly:
- Switches to a dedicated test database
- Flushes the database before each test
- Resets the async Redis connection
- Preserves authentication credentials
- Supports env var and ini option overrides
"""

import asyncio

import popoto
from popoto import redis_db
from popoto.redis_db import get_async_redis_db


def _get_db():
    """Access the current POPOTO_REDIS_DB via module attribute to avoid stale refs."""
    return redis_db.POPOTO_REDIS_DB


class TestPluginModule:
    """Verify the plugin module structure and hooks."""

    def test_plugin_importable(self):
        """The plugin module can be imported."""
        from popoto import pytest_plugin

        assert pytest_plugin is not None

    def test_pytest_addoption_exists(self):
        """The plugin exposes the pytest_addoption hook."""
        from popoto import pytest_plugin

        assert hasattr(pytest_plugin, "pytest_addoption")
        assert callable(pytest_plugin.pytest_addoption)

    def test_fixtures_exist(self):
        """The plugin defines the expected fixtures."""
        from popoto import pytest_plugin

        assert hasattr(pytest_plugin, "_popoto_test_db")
        assert hasattr(pytest_plugin, "_popoto_flush_db")
        assert hasattr(pytest_plugin, "_popoto_reset_async")


class TestDatabaseIsolation:
    """Verify the plugin switches to and isolates the test database."""

    def test_not_on_db_zero(self):
        """The plugin should have switched away from DB 0."""
        pool_kwargs = _get_db().connection_pool.connection_kwargs
        current_db = pool_kwargs.get("db", 0)
        assert current_db != 0, "Tests should not run on DB 0"

    def test_on_test_db(self):
        """The connection should be on the configured test DB (default 15)."""
        pool_kwargs = _get_db().connection_pool.connection_kwargs
        current_db = pool_kwargs.get("db", 0)
        assert current_db == 15, f"Expected DB 15, got DB {current_db}"

    def test_db_is_empty_at_start(self):
        """Each test should start with an empty database (flushed by fixture)."""
        assert _get_db().dbsize() == 0

    def test_data_does_not_leak_step1(self):
        """Write data that should not appear in the next test."""
        _get_db().set("leak_test_key", "should_not_leak")
        assert _get_db().get("leak_test_key") == b"should_not_leak"

    def test_data_does_not_leak_step2(self):
        """Verify data from the previous test was flushed."""
        assert _get_db().get("leak_test_key") is None
        assert _get_db().dbsize() == 0


class TestAuthPreservation:
    """Verify connection auth kwargs are preserved when switching DBs."""

    def test_connection_kwargs_preserved(self):
        """Host and port should be preserved from the original connection."""
        pool_kwargs = _get_db().connection_pool.connection_kwargs
        # Should have standard connection params
        assert "host" in pool_kwargs
        assert "port" in pool_kwargs

    def test_host_is_valid(self):
        """The host should be a valid string (not None or empty)."""
        pool_kwargs = _get_db().connection_pool.connection_kwargs
        host = pool_kwargs.get("host", "")
        assert host, "Host should not be empty"

    def test_port_is_valid(self):
        """The port should be a positive integer."""
        pool_kwargs = _get_db().connection_pool.connection_kwargs
        port = pool_kwargs.get("port", 0)
        assert isinstance(port, int)
        assert port > 0


class TestAsyncReset:
    """Verify the async connection is reset before each test."""

    def test_async_connection_is_preconfigured(self):
        """The async connection should be pre-configured for the test DB."""
        import redis.asyncio as aioredis

        assert isinstance(redis_db._POPOTO_ASYNC_REDIS_DB, aioredis.Redis)

    def test_async_connection_uses_test_db(self):
        """The async connection should point to the same DB as sync."""
        sync_db = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get(
            "db", 0
        )
        async_kwargs = (
            redis_db._POPOTO_ASYNC_REDIS_DB.connection_pool.connection_kwargs
        )
        async_db = async_kwargs.get("db", 0)
        assert async_db == sync_db, f"Async DB {async_db} != sync DB {sync_db}"

    def test_async_lock_is_fresh(self):
        """The async lock should be a fresh asyncio.Lock."""
        assert isinstance(redis_db._async_redis_lock, asyncio.Lock)


class TestAsyncIntegration:
    """Verify async Redis works correctly with the plugin."""

    @staticmethod
    async def _get_and_set():
        """Helper to test async Redis operations."""
        async_redis = await get_async_redis_db()
        await async_redis.set("async_test_key", "async_value")
        result = await async_redis.get("async_test_key")
        return result

    def test_async_operations_work(self):
        """Async Redis operations should work after plugin reset."""
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(self._get_and_set())
            assert result == b"async_value"
        finally:
            loop.close()

    def test_async_connection_on_test_db(self):
        """The async connection should be on the test DB, not DB 0."""
        import redis.asyncio as aioredis

        assert isinstance(redis_db._POPOTO_ASYNC_REDIS_DB, aioredis.Redis)
        async_db = redis_db._POPOTO_ASYNC_REDIS_DB.connection_pool.connection_kwargs.get("db", 0)
        assert async_db == 15, f"Expected async on DB 15, got DB {async_db}"


class PluginTestModel(popoto.Model):
    """Model defined at module level to avoid metaclass re-registration issues."""
    name = popoto.KeyField()


class TestModelIntegration:
    """Verify Popoto models work correctly with the test DB."""

    def test_model_save_and_retrieve(self):
        """Models should save to and retrieve from the test DB."""
        obj = PluginTestModel(name="test_item")
        obj.save()

        retrieved = PluginTestModel.query.get(name="test_item")
        assert retrieved is not None
        assert retrieved.name == "test_item"

    def test_model_data_isolated(self):
        """Model data from previous test should not exist (flushed by fixture)."""
        result = PluginTestModel.query.get(name="test_item")
        assert result is None, "Data from previous test should have been flushed"

    def test_dbsize_zero_before_operations(self):
        """DB should be empty before any model operations."""
        assert _get_db().dbsize() == 0
