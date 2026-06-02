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

import pytest


@pytest.fixture(autouse=True)
def _stop_embedding_invalidation_listeners():
    """Stop any EmbeddingField cross-process invalidation listener threads
    after each test.

    In the default ``pubsub`` mode, any test that reaches
    ``EmbeddingField.load_embeddings()`` (e.g. test_embedding_field.py,
    test_semantic_search.py, test_embedding_invalidation.py) spawns a daemon
    PubSubWorkerThread. Without this teardown those threads leak across the
    suite, holding Valkey connections against the flushed DB 15. This lives in
    conftest.py (test-only) rather than src/popoto/pytest_plugin.py (which ships
    inside the installed package and would affect every downstream consumer).
    """
    yield
    # Import lazily and via the same module path the embedding test suites use
    # (src.popoto.*) so we clear the same _listener_threads registry they
    # populate. Guarded: numpy-less environments never spawn listeners.
    try:
        from src.popoto.fields.embedding_field import stop_invalidation_listeners
    except Exception:
        return
    stop_invalidation_listeners()
