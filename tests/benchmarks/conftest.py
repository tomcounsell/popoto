"""Benchmark test configuration.

Provides fixtures for Redis isolation and constant override injection.
All benchmarks use unique key prefixes to prevent interference with
concurrent tests or development data.
"""

import time
import uuid

import pytest


@pytest.fixture
def run_id():
    """Unique identifier for a benchmark run."""
    return f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
