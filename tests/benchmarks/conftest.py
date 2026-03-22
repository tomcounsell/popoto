"""Benchmark test configuration.

Provides fixtures for Redis isolation and constant override injection.
All benchmarks use Redis DB 15 or a unique key prefix to prevent
interference with concurrent tests or development data.
"""

import time
import uuid

import pytest

BENCHMARK_DB = 15


@pytest.fixture
def run_id():
    """Unique identifier for a benchmark run."""
    return f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
