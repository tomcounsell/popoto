"""Tests for Query.get_many() and AsyncQuery.async_get_many() methods.

Tests cover:
- Basic bulk retrieval with order preservation
- Missing keys return None at correct positions
- Empty input returns empty list
- skip_none=True filters out None entries
- Mixed existing/missing keys
- Single key (degenerate case)
- Async parity with sync behavior
- Large batch retrieval
"""

import sys
import os
import asyncio

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.redis_db import POPOTO_REDIS_DB
import src.popoto.redis_db as redis_db_module


class Item(popoto.Model):
    name = popoto.KeyField(null=False)
    value = popoto.IntField(default=0)


def setup_module():
    """Clean up test data before running tests."""
    Item.delete_all()


def teardown_module():
    """Clean up test data after running tests."""
    Item.delete_all()


def _clean():
    Item.delete_all()


# --- Sync get_many tests ---


def test_get_many_basic_retrieval():
    """get_many returns all instances in correct order."""
    _clean()
    a = Item.create(name="alpha", value=1)
    b = Item.create(name="bravo", value=2)
    c = Item.create(name="charlie", value=3)

    keys = [a.db_key.redis_key, b.db_key.redis_key, c.db_key.redis_key]
    results = Item.query.get_many(redis_keys=keys)

    assert len(results) == 3
    assert results[0].name == "alpha"
    assert results[1].name == "bravo"
    assert results[2].name == "charlie"
    assert results[0].value == 1
    assert results[1].value == 2
    assert results[2].value == 3
    _clean()


def test_get_many_order_preservation():
    """get_many preserves input order even when reversed."""
    _clean()
    a = Item.create(name="first", value=10)
    b = Item.create(name="second", value=20)
    c = Item.create(name="third", value=30)

    # Request in reverse order
    keys = [c.db_key.redis_key, a.db_key.redis_key, b.db_key.redis_key]
    results = Item.query.get_many(redis_keys=keys)

    assert len(results) == 3
    assert results[0].name == "third"
    assert results[1].name == "first"
    assert results[2].name == "second"
    _clean()


def test_get_many_missing_keys():
    """get_many returns None for keys that do not exist in Redis."""
    _clean()
    a = Item.create(name="exists", value=42)

    keys = [a.db_key.redis_key, "Item:nonexistent", "Item:also_missing"]
    results = Item.query.get_many(redis_keys=keys)

    assert len(results) == 3
    assert results[0].name == "exists"
    assert results[1] is None
    assert results[2] is None
    _clean()


def test_get_many_empty_input():
    """get_many with empty list returns empty list without hitting Redis."""
    results = Item.query.get_many(redis_keys=[])
    assert results == []


def test_get_many_skip_none():
    """get_many with skip_none=True filters out None entries."""
    _clean()
    a = Item.create(name="real", value=99)

    keys = ["Item:fake1", a.db_key.redis_key, "Item:fake2"]
    results = Item.query.get_many(redis_keys=keys, skip_none=True)

    assert len(results) == 1
    assert results[0].name == "real"
    _clean()


def test_get_many_mixed_valid_missing():
    """get_many correctly interleaves valid instances and None."""
    _clean()
    a = Item.create(name="one", value=1)
    b = Item.create(name="two", value=2)

    keys = [
        a.db_key.redis_key,
        "Item:missing_a",
        b.db_key.redis_key,
        "Item:missing_b",
    ]
    results = Item.query.get_many(redis_keys=keys)

    assert len(results) == 4
    assert results[0].name == "one"
    assert results[1] is None
    assert results[2].name == "two"
    assert results[3] is None
    _clean()


def test_get_many_single_key():
    """get_many works with a single key (degenerate case)."""
    _clean()
    a = Item.create(name="solo", value=7)

    results = Item.query.get_many(redis_keys=[a.db_key.redis_key])

    assert len(results) == 1
    assert results[0].name == "solo"
    assert results[0].value == 7
    _clean()


def test_get_many_large_batch():
    """get_many handles a large batch of keys efficiently."""
    _clean()
    count = 50
    items = []
    for i in range(count):
        items.append(Item.create(name=f"batch_{i:03d}", value=i))

    keys = [item.db_key.redis_key for item in items]
    results = Item.query.get_many(redis_keys=keys)

    assert len(results) == count
    for i, result in enumerate(results):
        assert result is not None
        assert result.name == f"batch_{i:03d}"
        assert result.value == i
    _clean()


# --- Async get_many tests ---


@pytest.fixture(autouse=False)
def reset_async_redis():
    """Reset async Redis connection for async tests."""
    redis_db_module._POPOTO_ASYNC_REDIS_DB = None
    redis_db_module._async_redis_lock = asyncio.Lock()


@pytest.mark.asyncio
async def test_async_get_many_basic(reset_async_redis):
    """async_get_many returns all instances in correct order."""
    _clean()
    a = Item.create(name="async_a", value=10)
    b = Item.create(name="async_b", value=20)

    keys = [a.db_key.redis_key, b.db_key.redis_key]
    results = await Item.query.async_get_many(redis_keys=keys)

    assert len(results) == 2
    assert results[0].name == "async_a"
    assert results[1].name == "async_b"
    _clean()


@pytest.mark.asyncio
async def test_async_get_many_missing_keys(reset_async_redis):
    """async_get_many returns None for missing keys."""
    _clean()
    a = Item.create(name="async_exists", value=5)

    keys = [a.db_key.redis_key, "Item:async_missing"]
    results = await Item.query.async_get_many(redis_keys=keys)

    assert len(results) == 2
    assert results[0].name == "async_exists"
    assert results[1] is None
    _clean()


@pytest.mark.asyncio
async def test_async_get_many_empty(reset_async_redis):
    """async_get_many with empty list returns empty list."""
    results = await Item.query.async_get_many(redis_keys=[])
    assert results == []


@pytest.mark.asyncio
async def test_async_get_many_skip_none(reset_async_redis):
    """async_get_many with skip_none=True filters out None."""
    _clean()
    a = Item.create(name="async_real", value=42)

    keys = ["Item:async_fake", a.db_key.redis_key]
    results = await Item.query.async_get_many(redis_keys=keys, skip_none=True)

    assert len(results) == 1
    assert results[0].name == "async_real"
    _clean()
