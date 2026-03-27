# Popoto pytest plugin (popoto.pytest_plugin) provides autouse fixtures:
#
#   _popoto_test_db    (session-scoped) - switches to test DB (default: 15)
#   _popoto_flush_db   (function-scoped) - flushes DB before each test
#   _popoto_reset_async (function-scoped) - resets async Redis connection
#
# These are registered via the pytest11 entry point in pyproject.toml
# and activate automatically when popoto is installed.
#
# To disable: pytest -p no:popoto
# To override DB: set POPOTO_TEST_DB env var or popoto_test_db ini option
