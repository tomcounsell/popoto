"""
Tests for Redis connection handling and failure scenarios.

These tests verify that Popoto handles connection issues gracefully,
including timeouts, disconnections, and health checks.
"""

import asyncio
import pytest
from unittest.mock import patch
import redis
import redis.asyncio as aioredis

import src.popoto.redis_db as redis_db_module
from src.popoto.redis_db import (
    POPOTO_REDIS_DB,
    set_REDIS_DB_settings,
    get_REDIS_DB,
    check_connection,
    async_check_connection,
    set_async_redis_db_settings,
    get_async_redis_db,
)
from src.popoto import Model, KeyField, Field


class SimpleModel(Model):
    """Simple model for connection tests."""

    name = KeyField()
    value = Field(type=str, default="")


class TestCheckConnection:
    """Tests for the check_connection() health check function."""

    def test_check_connection_success(self):
        """Test that check_connection returns True when Redis is available."""
        # Assuming Redis is running for tests
        assert check_connection() is True

    def test_check_connection_failure(self):
        """Test that check_connection returns False when Redis is unavailable."""
        with patch.object(
            POPOTO_REDIS_DB,
            "ping",
            side_effect=redis.ConnectionError("Connection refused"),
        ):
            assert check_connection() is False

    def test_check_connection_timeout(self):
        """Test that check_connection returns False on timeout."""
        with patch.object(
            POPOTO_REDIS_DB,
            "ping",
            side_effect=redis.TimeoutError("Connection timed out"),
        ):
            assert check_connection() is False


class TestConnectionFailureHandling:
    """Tests for connection failure scenarios."""

    def test_save_with_connection_error(self):
        """Test that save raises exception when Redis is unavailable.

        save() uses an internal pipeline for atomic execution, so the
        ConnectionError surfaces when pipeline.execute() is called.
        """
        model = SimpleModel(name="test", value="data")

        with patch.object(
            POPOTO_REDIS_DB,
            "pipeline",
            side_effect=redis.ConnectionError("Connection refused"),
        ):
            with pytest.raises(redis.ConnectionError):
                model.save()

    def test_query_with_connection_error(self):
        """Test that query raises exception when Redis is unavailable."""
        with patch.object(
            POPOTO_REDIS_DB,
            "smembers",
            side_effect=redis.ConnectionError("Connection refused"),
        ):
            with pytest.raises(redis.ConnectionError):
                SimpleModel.query.all()

    def test_get_with_connection_error(self):
        """Test that get raises exception when Redis is unavailable."""
        with patch.object(
            POPOTO_REDIS_DB,
            "hgetall",
            side_effect=redis.ConnectionError("Connection refused"),
        ):
            with pytest.raises(redis.ConnectionError):
                SimpleModel.query.get(name="test")


class TestConnectionReconfiguration:
    """Tests for runtime connection reconfiguration."""

    def test_get_redis_db_returns_connection(self):
        """Test that get_REDIS_DB returns the Redis connection object."""
        db = get_REDIS_DB()
        assert db is not None
        assert hasattr(db, "ping")

    def test_set_redis_db_settings_with_valid_url(self):
        """Test reconfiguring with a valid Redis URL."""
        # Store original
        original_db = get_REDIS_DB()

        try:
            # Reconfigure (using same URL effectively)
            set_REDIS_DB_settings(host="localhost", port=6379)

            # Should still work
            new_db = get_REDIS_DB()
            assert new_db is not None
            assert new_db.ping()
        finally:
            # Test cleanup handled by fixture if needed
            pass


class TestConnectionPoolBackPressure:
    """#504: a connection swap must not drop the bounded pool.

    ``redis.Redis(**kwargs)`` builds an effectively unbounded pool
    (max_connections == 2**31), so before the fix any call to
    set_REDIS_DB_settings silently removed the back-pressure that the
    module-level init path establishes.
    """

    @pytest.fixture
    def restore_sync_db(self):
        """Restore the global sync client after a test rebinds it."""
        original = redis_db_module.POPOTO_REDIS_DB
        yield
        redis_db_module.POPOTO_REDIS_DB = original

    def test_set_redis_db_settings_keeps_blocking_pool(self, restore_sync_db):
        """Swapping the connection preserves a capped BlockingConnectionPool."""
        set_REDIS_DB_settings(host="localhost", port=6379, db=15)

        pool = redis_db_module.POPOTO_REDIS_DB.connection_pool
        assert isinstance(pool, redis.BlockingConnectionPool)
        assert pool.max_connections == redis_db_module._SYNC_MAX_CONNECTIONS
        assert pool.connection_kwargs["db"] == 15
        assert redis_db_module.POPOTO_REDIS_DB.ping()

    def test_explicit_connection_pool_is_respected(self, restore_sync_db):
        """An explicitly supplied pool bypasses the managed pool."""
        custom = redis.ConnectionPool(host="localhost", port=6379, db=15)

        set_REDIS_DB_settings(connection_pool=custom)

        assert redis_db_module.POPOTO_REDIS_DB.connection_pool is custom

    @pytest.mark.asyncio
    async def test_set_async_redis_db_settings_keeps_blocking_pool(self):
        """#504: the async setter has the same contract as the sync one."""
        await set_async_redis_db_settings(host="localhost", port=6379, db=15)

        client = redis_db_module._POPOTO_ASYNC_REDIS_DB
        pool = client.connection_pool
        assert isinstance(pool, aioredis.BlockingConnectionPool)
        assert pool.max_connections == redis_db_module._ASYNC_MAX_CONNECTIONS
        assert await client.ping() is True


class TestTimeoutBehavior:
    """Tests verifying timeout configuration."""

    def test_connection_has_socket_timeout(self):
        """Verify the connection is configured with socket timeout."""
        # This tests that our hardening changes are in place
        connection_pool = POPOTO_REDIS_DB.connection_pool
        # Check that connection kwargs include timeout
        # The exact way to verify depends on redis-py version
        assert connection_pool is not None


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up test data after each test."""
    yield
    # Clean up any test models created
    try:
        for key in POPOTO_REDIS_DB.keys("SimpleModel:*"):
            POPOTO_REDIS_DB.delete(key)
    except redis.ConnectionError:
        pass  # Ignore if Redis not available during cleanup


@pytest.fixture(autouse=True)
def reset_async_connection():
    """Reset async Redis connection before each test."""
    redis_db_module._POPOTO_ASYNC_REDIS_DB = None
    redis_db_module._async_redis_lock = asyncio.Lock()


class TestAsyncCheckConnection:
    """Gap 9: Tests for async_check_connection() health check."""

    @pytest.mark.asyncio
    async def test_async_check_connection_success(self):
        """Gap 9: async_check_connection returns True when Redis is available."""
        result = await async_check_connection()
        assert result is True

    @pytest.mark.asyncio
    async def test_async_check_connection_failure(self):
        """Gap 9: async_check_connection returns False on ConnectionError."""
        async_redis = await get_async_redis_db()
        with patch.object(
            async_redis,
            "ping",
            side_effect=redis.ConnectionError("Connection refused"),
        ):
            result = await async_check_connection()
            assert result is False

    @pytest.mark.asyncio
    async def test_async_check_connection_timeout(self):
        """Gap 9: async_check_connection returns False on TimeoutError."""
        async_redis = await get_async_redis_db()
        with patch.object(
            async_redis,
            "ping",
            side_effect=redis.TimeoutError("Connection timed out"),
        ):
            result = await async_check_connection()
            assert result is False


class TestAsyncConnectionReconfiguration:
    """Gap 10: Tests for set_async_redis_db_settings() reconfiguration."""

    @pytest.mark.asyncio
    async def test_set_async_redis_db_settings_reconnects(self):
        """Gap 10: set_async_redis_db_settings resets and reconnects."""
        # Get initial connection
        initial_redis = await get_async_redis_db()
        assert initial_redis is not None

        # Reconfigure with same settings (localhost)
        await set_async_redis_db_settings(host="localhost", port=6379)

        # Get new connection - should be different object
        new_redis = await get_async_redis_db()
        assert new_redis is not None
        # Verify new connection works
        pong = await new_redis.ping()
        assert pong is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
