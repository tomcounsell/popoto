"""Testing utilities for Popoto.

Provides helpers for test isolation when running tests against Redis.

Example usage in pytest::

    # conftest.py
    import pytest
    from popoto.testing import use_test_db, flush_test_db

    @pytest.fixture(scope="session", autouse=True)
    def setup_test_db():
        use_test_db(15)
        yield
        flush_test_db()

    @pytest.fixture(autouse=True)
    def clean_db():
        yield
        flush_test_db()
"""
from .redis_db import set_REDIS_DB_settings, POPOTO_REDIS_DB


def use_test_db(db: int = 15):
    """Switch to a test database for isolated testing.

    Call this in your test setup to use a separate Redis database
    that won't interfere with development/production data.

    Args:
        db: Redis database number (0-15). Default is 15.

    Example:
        from popoto.testing import use_test_db

        def setup_module():
            use_test_db(15)
    """
    set_REDIS_DB_settings(db=db)


def flush_test_db():
    """Flush the current database.

    Use this in test teardown to clean up test data.
    WARNING: This deletes ALL keys in the current database.

    Example:
        from popoto.testing import flush_test_db

        def teardown_module():
            flush_test_db()
    """
    POPOTO_REDIS_DB.flushdb()
