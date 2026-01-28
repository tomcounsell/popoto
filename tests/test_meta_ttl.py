"""
Tests for Model Meta.ttl feature
"""

import pytest
import time
from src.popoto import Model, KeyField, Field
from src.popoto.redis_db import POPOTO_REDIS_DB


# Flush Redis before tests
@pytest.fixture(autouse=True)
def flush_redis():
    POPOTO_REDIS_DB.flushdb()


class CachedData(Model):
    key = KeyField()
    value = Field()

    class Meta:
        ttl = 2  # Expires after 2 seconds


class PermanentData(Model):
    key = KeyField()
    value = Field()


def test_meta_ttl_sets_expiration():
    """Test that Meta.ttl sets expiration on saved models."""
    data = CachedData.create(key="test1", value="data1")

    # Check that TTL is set
    ttl = POPOTO_REDIS_DB.ttl(data.db_key.redis_key)
    assert ttl > 0 and ttl <= 2  # TTL should be set and <= 2 seconds


def test_meta_ttl_expires_after_timeout():
    """Test that models with Meta.ttl expire after the specified time."""
    data = CachedData.create(key="test2", value="data2")

    # Verify it exists immediately
    assert CachedData.query.get(key="test2") is not None

    # Wait for TTL to expire (2 seconds + buffer)
    time.sleep(2.5)

    # Verify it no longer exists
    assert CachedData.query.get(key="test2") is None


def test_no_meta_ttl():
    """Test that models without Meta.ttl don't expire."""
    data = PermanentData.create(key="test3", value="data3")

    # Check that no TTL is set (-1 means no expiration)
    ttl = POPOTO_REDIS_DB.ttl(data.db_key.redis_key)
    assert ttl == -1

    # Wait a bit and verify it still exists
    time.sleep(1)
    assert PermanentData.query.get(key="test3") is not None


def test_instance_ttl_override():
    """Test that instance-level _ttl overrides Meta.ttl."""
    # Create with instance-level TTL override
    data = CachedData(key="test4", value="data4")
    data._ttl = 5  # Override Meta.ttl (which is 2)
    data.save()

    # Check that TTL is 5 seconds, not 2
    ttl = POPOTO_REDIS_DB.ttl(data.db_key.redis_key)
    assert ttl > 2 and ttl <= 5


def test_no_ttl_on_permanent_model():
    """Test that _ttl=None doesn't set expiration even with Meta.ttl."""
    data = CachedData(key="test5", value="data5")
    data._ttl = None  # Explicitly set to None to disable TTL
    data.save()

    # Check that no TTL is set
    ttl = POPOTO_REDIS_DB.ttl(data.db_key.redis_key)
    assert ttl == -1


def test_invalid_meta_ttl_raises():
    """Test that invalid Meta.ttl raises ModelException."""
    from src.popoto.models.base import ModelException

    # Negative TTL
    with pytest.raises(ModelException, match="must be a positive integer"):

        class BadModel1(Model):
            key = KeyField()

            class Meta:
                ttl = -1

    # Zero TTL
    with pytest.raises(ModelException, match="must be a positive integer"):

        class BadModel2(Model):
            key = KeyField()

            class Meta:
                ttl = 0

    # String TTL
    with pytest.raises(ModelException, match="must be a positive integer"):

        class BadModel3(Model):
            key = KeyField()

            class Meta:
                ttl = "10"


def test_meta_ttl_with_update():
    """Test that TTL is refreshed on update."""
    data = CachedData.create(key="test6", value="data6")

    # Wait 1 second
    time.sleep(1)

    # Update the model
    data.value = "updated"
    data.save()

    # TTL should be refreshed to ~2 seconds
    ttl = POPOTO_REDIS_DB.ttl(data.db_key.redis_key)
    assert ttl > 1 and ttl <= 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
