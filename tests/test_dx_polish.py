"""Tests for DX polish improvements."""

import pytest
import popoto
from popoto import Model, KeyField, Field
from popoto.testing import use_test_db, flush_test_db


class SimpleModel(Model):
    key = KeyField(type=str)
    value = Field(type=int, default=0)


class TestLast:
    """Test QueryBuilder.last() method."""

    def setup_method(self):
        SimpleModel.delete_all()

    def teardown_method(self):
        SimpleModel.delete_all()

    def test_last_returns_last_result(self):
        """last() should return the last matching result."""
        SimpleModel.create(key="a", value=1)
        SimpleModel.create(key="b", value=2)
        SimpleModel.create(key="c", value=3)

        # Use filter() to get QueryBuilder, then call last()
        results = SimpleModel.query.filter().all()
        last = SimpleModel.query.filter().last()

        assert last is not None
        assert last.key == results[-1].key

    def test_last_returns_none_when_empty(self):
        """last() should return None when no results."""
        result = SimpleModel.query.filter(key="nonexistent").last()
        assert result is None

    def test_last_with_filter(self):
        """last() should work with filters."""
        SimpleModel.create(key="a", value=1)
        SimpleModel.create(key="b", value=2)

        # Filter to single result
        result = SimpleModel.query.filter(key="a").last()
        assert result is not None
        assert result.key == "a"

    def test_last_with_order_by(self):
        """last() should respect order_by."""
        SimpleModel.create(key="a", value=1)
        SimpleModel.create(key="b", value=2)
        SimpleModel.create(key="c", value=3)

        # Order by key ascending, last should be "c"
        result = SimpleModel.query.filter().order_by("key").last()
        assert result is not None
        assert result.key == "c"


class TestGetRedis:
    """Test popoto.get_redis() function."""

    def test_get_redis_returns_connection(self):
        """get_redis() should return a Redis connection."""
        redis = popoto.get_redis()
        assert redis is not None
        # Should be able to ping
        assert redis.ping() is True

    def test_get_redis_can_do_operations(self):
        """get_redis() connection should work for Redis operations."""
        redis = popoto.get_redis()

        # Test set operations
        redis.sadd("test_set", "value1", "value2")
        assert redis.sismember("test_set", "value1")
        redis.delete("test_set")

        # Test list operations
        redis.rpush("test_list", "item1", "item2")
        assert redis.llen("test_list") == 2
        redis.delete("test_list")


class TestTestingModule:
    """Test popoto.testing module."""

    def test_use_test_db_import(self):
        """use_test_db should be importable."""
        assert callable(use_test_db)

    def test_flush_test_db_import(self):
        """flush_test_db should be importable."""
        assert callable(flush_test_db)
