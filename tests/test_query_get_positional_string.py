"""Tests for Query.get() accepting a plain redis_key string positionally.

Covers the fix for issue #317: passing a string positionally to get() should
be interpreted as redis_key, not db_key.
"""

import pytest

from popoto import Model, KeyField, Field


class PositionalGetModel(Model):
    key = KeyField(type=str)
    value = Field(type=str, null=True)


class MultiKeyPositionalModel(Model):
    key1 = KeyField(type=str)
    key2 = KeyField(type=str)
    value = Field(type=str, null=True)


class TestQueryGetPositionalString:
    """Test that Query.get() handles a positional string as redis_key."""

    def setup_method(self):
        PositionalGetModel.delete_all()
        MultiKeyPositionalModel.delete_all()

    def teardown_method(self):
        PositionalGetModel.delete_all()
        MultiKeyPositionalModel.delete_all()

    def test_positional_string_retrieves_instance(self):
        """A plain string passed positionally should be treated as redis_key."""
        instance = PositionalGetModel.create(key="alpha", value="hello")
        redis_key = instance.db_key.redis_key

        result = PositionalGetModel.query.get(redis_key)
        assert result is not None
        assert result.key == "alpha"
        assert result.value == "hello"

    def test_positional_db_key_still_works(self):
        """Passing an actual DB_key positionally must continue to work."""
        instance = PositionalGetModel.create(key="beta", value="world")
        db_key = instance.db_key

        result = PositionalGetModel.query.get(db_key)
        assert result is not None
        assert result.key == "beta"
        assert result.value == "world"

    def test_keyword_redis_key_still_works(self):
        """Explicit redis_key= keyword argument must still work."""
        instance = PositionalGetModel.create(key="gamma", value="test")
        redis_key = instance.db_key.redis_key

        result = PositionalGetModel.query.get(redis_key=redis_key)
        assert result is not None
        assert result.key == "gamma"

    def test_nonexistent_key_returns_none(self):
        """A positional string for a nonexistent key should return None."""
        result = PositionalGetModel.query.get("PositionalGetModel:nonexistent")
        assert result is None

    def test_kwargs_lookup_unchanged(self):
        """Keyword-based field lookup must still work as before."""
        instance = PositionalGetModel.create(key="delta", value="unchanged")

        result = PositionalGetModel.query.get(key="delta")
        assert result is not None
        assert result.value == "unchanged"

    def test_both_positional_string_and_keyword_redis_key(self):
        """When both positional string and redis_key= are given, keyword wins."""
        inst_a = PositionalGetModel.create(key="aaa", value="first")
        inst_b = PositionalGetModel.create(key="bbb", value="second")
        key_a = inst_a.db_key.redis_key
        key_b = inst_b.db_key.redis_key

        # Positional string is discarded when redis_key= is explicit
        result = PositionalGetModel.query.get(key_a, redis_key=key_b)
        assert result is not None
        assert result.key == "bbb"

    def test_multi_key_model_positional_string(self):
        """Positional string lookup should work with multi-key models too."""
        instance = MultiKeyPositionalModel.create(
            key1="x", key2="y", value="multi"
        )
        redis_key = instance.db_key.redis_key

        result = MultiKeyPositionalModel.query.get(redis_key)
        assert result is not None
        assert result.key1 == "x"
        assert result.key2 == "y"
        assert result.value == "multi"
