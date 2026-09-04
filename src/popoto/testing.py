"""Testing utilities for Popoto.

Provides helpers for test isolation when running tests against Redis.

Automatic Plugin (Recommended):

    If you install popoto and use pytest, the ``popoto.pytest_plugin`` is
    registered automatically via entry points. It switches to a dedicated
    test database (default: DB 15), flushes before each test, and resets
    the async connection. No configuration needed -- just run ``pytest``.

    To override the test DB number::

        # Environment variable
        POPOTO_TEST_DB=14 pytest

        # Or in pyproject.toml
        [tool.pytest.ini_options]
        popoto_test_db = 14

    To disable the plugin::

        pytest -p no:popoto

Manual Helpers (for non-pytest or custom setups):

    The functions below are available for test runners other than pytest
    or for cases where you need manual control over DB switching::

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

from .redis_db import set_REDIS_DB_settings, get_REDIS_DB


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
    # Resolve the client at call time: ``set_REDIS_DB_settings`` rebinds the
    # module global, so an import-time snapshot would flush the database that
    # was bound when this module was first imported, not the one in use now.
    get_REDIS_DB().flushdb()
