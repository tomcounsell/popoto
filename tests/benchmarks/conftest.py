"""Benchmark test configuration.

Provides fixtures for Redis isolation and constant override injection.
All benchmarks use Redis DB 15 or a unique key prefix to prevent
interference with concurrent tests or development data.
"""

import os
import time
import uuid

import pytest
import redis

from src.popoto.redis_db import POPOTO_REDIS_DB

BENCHMARK_DB = 15
BENCHMARK_PREFIX = f"bench:{uuid.uuid4().hex[:8]}:"


@pytest.fixture(autouse=True)
def benchmark_redis_cleanup():
    """Clean up benchmark keys after each test."""
    yield
    # Clean up any keys with our benchmark prefix
    try:
        cursor = 0
        while True:
            cursor, keys = POPOTO_REDIS_DB.scan(
                cursor, match=f"{BENCHMARK_PREFIX}*", count=100
            )
            if keys:
                POPOTO_REDIS_DB.delete(*keys)
            if cursor == 0:
                break
    except Exception:
        pass


@pytest.fixture
def run_id():
    """Unique identifier for a benchmark run."""
    return f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
