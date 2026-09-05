"""``popoto.counters``: the durable-counter primitive recipes use (#630).

``increment`` is ``INCRBY`` returning the running total; ``read`` is ``GET``
returning ``0`` for an absent key. The key string is the caller's contract.
"""

import os
import sys
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src.popoto import counters
from src.popoto.redis_db import POPOTO_REDIS_DB


@pytest.fixture
def key():
    name = f"$test:counters:{uuid.uuid4().hex[:12]}"
    yield name
    POPOTO_REDIS_DB.delete(name)


def test_increment_returns_running_total(key):
    assert counters.increment(key) == 1
    assert counters.increment(key) == 2
    assert counters.increment(key, 5) == 7


def test_read_absent_key_is_zero(key):
    assert counters.read(key) == 0


def test_values_round_trip_as_int(key):
    counters.increment(key, 3)
    got = counters.read(key)
    assert got == 3
    assert type(got) is int
    assert type(counters.increment(key, 0)) is int


def test_increment_is_the_same_key_the_client_sees(key):
    """No prefixing or key rewriting happens inside the helper."""
    counters.increment(key, 4)
    assert int(POPOTO_REDIS_DB.get(key)) == 4
