"""
Tests for Model Meta.ttl feature

Backend conformance (#759 M5, plan §5 M5 gate (b)): every test runs once per
configured backend from the same code. A remaining TTL is read through each
backend's own means -- ``TTL`` on Redis, ``ttl_remaining`` (the same reply
shape and rounding) on Postgres -- by :func:`_remaining_ttl`.
"""

import pytest
import time
from src.popoto import Model, KeyField, Field
from src.popoto.backends import RecordId, get_backend
from src.popoto.redis_db import get_REDIS_DB

pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]


# Flush Redis before tests
@pytest.fixture(autouse=True)
def flush_redis():
    get_REDIS_DB().flushdb()


class CachedData(Model):
    key = KeyField()
    value = Field()

    class Meta:
        ttl = 2  # Expires after 2 seconds


class PermanentData(Model):
    key = KeyField()
    value = Field()


def _remaining_ttl(instance):
    """``TTL key``: seconds left, ``-1`` for no expiry, ``-2`` for no key."""
    model = type(instance)
    backend = get_backend(model)
    key = instance.db_key.redis_key
    if backend.name == "redis":
        return get_REDIS_DB().ttl(key)
    return backend.ttl_remaining(
        model._meta.spec, [RecordId.from_key(model._meta.model_name, key)]
    )[0]


def test_meta_ttl_sets_expiration():
    """Test that Meta.ttl sets expiration on saved models."""
    data = CachedData.create(key="test1", value="data1")

    # Check that TTL is set
    ttl = _remaining_ttl(data)
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
    ttl = _remaining_ttl(data)
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
    ttl = _remaining_ttl(data)
    assert ttl > 2 and ttl <= 5


def test_no_ttl_on_permanent_model():
    """Test that _ttl=None doesn't set expiration even with Meta.ttl."""
    data = CachedData(key="test5", value="data5")
    data._ttl = None  # Explicitly set to None to disable TTL
    data.save()

    # Check that no TTL is set
    ttl = _remaining_ttl(data)
    assert ttl == -1


def test_invalid_meta_ttl_raises():
    """Test that invalid Meta.ttl raises ModelException."""
    from src.popoto.exceptions import ModelException

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
    ttl = _remaining_ttl(data)
    assert ttl > 1 and ttl <= 2


def test_an_expired_record_is_gone_from_every_query():
    """#759 M5: past its TTL a record is missing from get, filter, count and
    exists alike, while a permanent one is untouched."""
    CachedData.create(key="short", value="x")
    PermanentData.create(key="long", value="x")
    time.sleep(2.5)
    assert CachedData.query.get(key="short") is None
    assert CachedData.query.filter(value="x") == []
    assert CachedData.exists(key="short") is False
    assert [p.key for p in PermanentData.query.filter(value="x")] == ["long"]


def test_a_save_after_expiry_creates_the_record_again():
    """#759 M5: saving under an expired key writes a fresh record with a
    fresh TTL (HSET on an expired key creates a new hash)."""
    data = CachedData.create(key="again", value="old")
    time.sleep(2.5)
    data.value = "new"
    data.save()
    reloaded = CachedData.query.get(key="again")
    assert reloaded is not None and reloaded.value == "new"
    assert 0 < _remaining_ttl(reloaded) <= 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
