"""Tests for the popoto pytest plugin.

These tests verify that the auto-registering pytest plugin correctly:
- Switches to a dedicated test database
- Flushes the database before each test
- Resets the async Redis connection
- Preserves authentication credentials
- Supports env var and ini option overrides
"""

import asyncio
import importlib
import os
import subprocess
import sys
import textwrap
import types

import pytest

import popoto
from popoto import redis_db
from popoto.pytest_plugin import _resolve_test_db
from popoto.redis_db import get_async_redis_db


def _get_db():
    """Access the current POPOTO_REDIS_DB via module attribute to avoid stale refs."""
    return redis_db.POPOTO_REDIS_DB


# Captured at IMPORT time, i.e. during collection — before any fixture runs.
# The plugin must already be on the test DB at this point, otherwise test
# modules that touch models at module level write to DB 0 (#522).
_DB_AT_IMPORT_TIME = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get(
    "db", 0
)

# Mirrors the DB that TestIsolationWarning._inert_env pins children to via
# REDIS_URL (#595, a No-Go to edit), and the DB the inert-plumbing test seeds its
# marker key on. If that helper's DB ever changes, this must change with it --
# the probe sources below interpolate this constant so a mismatch fails loudly
# rather than silently asserting against the wrong database.
_INERT_PROBE_DB = 14

# The DB the env-beats-ini child is pinned to. Deliberately a literal, not a value
# resolved through _resolve_test_db: this test exists to pin the resolution chain
# independently, so deriving the expectation from the resolver would make it a
# tautology. Must be a DB no parent suite run uses -- an opted-in child flushes its
# DB before each of its own tests, so a collision would wipe the parent's live DB
# mid-test.
_ENV_OVERRIDE_CHILD_DB = 12


@pytest.fixture(scope="session")
def expected_test_db(pytestconfig):
    """The DB this session is supposed to be isolated on, resolved the way the
    plugin resolves it.

    Skips if the plugin is inert (no ``popoto_test_db`` opt-in). Only use this
    fixture in tests that do NOT also carry a ``!= 0`` DB-0 guard: a skip raised
    here happens during fixture setup and aborts the whole test body, which would
    make those guards conditional (#577). Such tests resolve inline instead, after
    the guard has already run.
    """
    db = _resolve_test_db(pytestconfig)
    if db is None:
        pytest.skip("plugin is inert (no popoto_test_db opt-in) — nothing to assert")
    return db


class TestPluginModule:
    """Verify the plugin module structure and hooks."""

    def test_plugin_importable(self):
        """The plugin module can be imported."""
        from popoto import pytest_plugin

        assert pytest_plugin is not None

    def test_pytest_addoption_exists(self):
        """The plugin exposes the pytest_addoption hook."""
        from popoto import pytest_plugin

        assert hasattr(pytest_plugin, "pytest_addoption")
        assert callable(pytest_plugin.pytest_addoption)

    def test_fixtures_exist(self):
        """The plugin defines the expected fixtures."""
        from popoto import pytest_plugin

        assert hasattr(pytest_plugin, "_popoto_test_db")
        assert hasattr(pytest_plugin, "_popoto_flush_db")
        assert hasattr(pytest_plugin, "_popoto_reset_async")


class TestDatabaseIsolation:
    """Verify the plugin switches to and isolates the test database."""

    def test_not_on_db_zero(self):
        """The plugin should have switched away from DB 0."""
        pool_kwargs = _get_db().connection_pool.connection_kwargs
        current_db = pool_kwargs.get("db", 0)
        assert current_db != 0, "Tests should not run on DB 0"

    def test_on_test_db(self, expected_test_db):
        """The connection should be on the configured test DB (default 15)."""
        pool_kwargs = _get_db().connection_pool.connection_kwargs
        current_db = pool_kwargs.get("db", 0)
        assert (
            current_db == expected_test_db
        ), f"Expected DB {expected_test_db}, got DB {current_db}"

    def test_swap_happens_before_test_modules_are_imported(self, pytestconfig):
        """#522: the DB swap must precede collection, not the first test.

        A session-scoped autouse fixture first runs when the first test
        executes — after pytest has imported every test module. Any module
        that runs model code at import time (``Model.create(...)`` at module
        level) would therefore write to DB 0, the developer's real database.
        The plugin does the swap in ``pytest_configure`` instead.
        """
        # Guard first, unconditionally: this assertion must never be skipped.
        assert _DB_AT_IMPORT_TIME != 0, (
            "Connection was still on DB 0 while test modules were being "
            "imported; module-level model code would write to the real database"
        )
        expected = _resolve_test_db(pytestconfig)
        if expected is None:
            pytest.skip("plugin is inert (no popoto_test_db opt-in)")
        assert _DB_AT_IMPORT_TIME == expected

    def test_db_is_empty_at_start(self):
        """Each test should start with an empty database (flushed by fixture)."""
        assert _get_db().dbsize() == 0

    def test_data_does_not_leak_step1(self):
        """Write data that should not appear in the next test."""
        _get_db().set("leak_test_key", "should_not_leak")
        assert _get_db().get("leak_test_key") == b"should_not_leak"

    def test_data_does_not_leak_step2(self):
        """Verify data from the previous test was flushed."""
        assert _get_db().get("leak_test_key") is None
        assert _get_db().dbsize() == 0


class TestAuthPreservation:
    """Verify connection auth kwargs are preserved when switching DBs."""

    def test_connection_kwargs_preserved(self):
        """Host and port should be preserved from the original connection."""
        pool_kwargs = _get_db().connection_pool.connection_kwargs
        # Should have standard connection params
        assert "host" in pool_kwargs
        assert "port" in pool_kwargs

    def test_host_is_valid(self):
        """The host should be a valid string (not None or empty)."""
        pool_kwargs = _get_db().connection_pool.connection_kwargs
        host = pool_kwargs.get("host", "")
        assert host, "Host should not be empty"

    def test_port_is_valid(self):
        """The port should be a positive integer."""
        pool_kwargs = _get_db().connection_pool.connection_kwargs
        port = pool_kwargs.get("port", 0)
        assert isinstance(port, int)
        assert port > 0


class TestAsyncReset:
    """Verify the async connection is reset before each test."""

    def test_async_connection_is_cleared_for_lazy_creation(self):
        """The async global is cleared (not pre-created) before each test.

        Pre-creating a client in the fixture's synchronous setup would bind it
        to the wrong event loop (pytest-asyncio makes a fresh loop per test),
        causing async ops to silently target the wrong loop. The fixture leaves
        the global ``None`` so ``get_async_redis_db()`` rebuilds it lazily
        inside the test's own running loop.
        """
        assert redis_db._POPOTO_ASYNC_REDIS_DB is None

    def test_async_connection_uses_test_db_when_built(self):
        """A lazily-built async client points to the same DB as sync."""
        sync_db = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get(
            "db", 0
        )

        loop = asyncio.new_event_loop()
        try:
            async_redis = loop.run_until_complete(get_async_redis_db())
        finally:
            loop.close()
        async_db = async_redis.connection_pool.connection_kwargs.get("db", 0)
        assert async_db == sync_db, f"Async DB {async_db} != sync DB {sync_db}"

    def test_async_lock_is_fresh(self):
        """The async lock should be a fresh asyncio.Lock."""
        assert isinstance(redis_db._async_redis_lock, asyncio.Lock)


class TestAsyncIntegration:
    """Verify async Redis works correctly with the plugin."""

    @staticmethod
    async def _get_and_set():
        """Helper to test async Redis operations."""
        async_redis = await get_async_redis_db()
        await async_redis.set("async_test_key", "async_value")
        result = await async_redis.get("async_test_key")
        return result

    def test_async_operations_work(self):
        """Async Redis operations should work after plugin reset."""
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(self._get_and_set())
            assert result == b"async_value"
        finally:
            loop.close()

    def test_async_connection_on_test_db(self, expected_test_db):
        """A lazily-built async connection is on the test DB, not DB 0."""
        import redis.asyncio as aioredis

        loop = asyncio.new_event_loop()
        try:
            async_redis = loop.run_until_complete(get_async_redis_db())
        finally:
            loop.close()
        assert isinstance(async_redis, aioredis.Redis)
        async_db = async_redis.connection_pool.connection_kwargs.get("db", 0)
        assert (
            async_db == expected_test_db
        ), f"Expected async on DB {expected_test_db}, got DB {async_db}"


class PluginTestModel(popoto.Model):
    """Model defined at module level to avoid metaclass re-registration issues."""

    name = popoto.KeyField()


class TestModelIntegration:
    """Verify Popoto models work correctly with the test DB."""

    def test_model_save_and_retrieve(self):
        """Models should save to and retrieve from the test DB."""
        obj = PluginTestModel(name="test_item")
        obj.save()

        retrieved = PluginTestModel.query.get(name="test_item")
        assert retrieved is not None
        assert retrieved.name == "test_item"

    def test_model_data_isolated(self):
        """Model data from previous test should not exist (flushed by fixture)."""
        result = PluginTestModel.query.get(name="test_item")
        assert result is None, "Data from previous test should have been flushed"

    def test_dbsize_zero_before_operations(self):
        """DB should be empty before any model operations."""
        assert _get_db().dbsize() == 0


def _is_single_tree_env():
    """Return True when src.popoto and popoto resolve to the same physical file.

    In a git worktree the worktree's own src/ directory is on sys.path, so
    ``import src.popoto`` loads a *different* file than the installed ``popoto``
    package.  In that scenario the alias-collapse tests are not meaningful
    (the conftest itself re-imports from the worktree path), so they skip.
    """
    import importlib.util as _ilu

    canonical_file = popoto.__file__

    # If src.popoto is already in sys.modules, compare its __file__.
    src_mod = sys.modules.get("src.popoto")
    if src_mod is not None:
        return getattr(src_mod, "__file__", None) == canonical_file

    # Otherwise find where src.popoto *would* load from.
    try:
        spec = _ilu.find_spec("src.popoto")
    except (ModuleNotFoundError, ValueError):
        # No src/ on path at all — the canonical install-only scenario.
        return True
    if spec is None:
        return True
    return spec.origin == canonical_file


class TestSrcPopotoImportPaths:
    """Regression tests: src.popoto import paths must resolve to the same DB as popoto.

    These tests verify the fix for issue #420: before the fix, tests importing via
    src.popoto wrote to DB 0 instead of DB 15, causing pollution across test runs.

    The tests inspect sys.modules state set by the pytest_configure hook.  They
    automatically SKIP in git-worktree environments where the worktree's own
    src/ directory adds a second physical copy of src/popoto that shadows the
    installed canonical package — that is a worktree-specific artefact, not a
    bug in the fix.
    """

    def test_src_popoto_registered_in_sys_modules(self):
        """pytest_configure must register src.popoto in sys.modules.

        The alias-collapse hook's job is to populate sys.modules["src.popoto"]
        so that any subsequent ``import src.popoto`` statement gets the canonical
        object from the cache rather than loading a fresh module off disk.
        """
        if not _is_single_tree_env():
            pytest.skip(
                "Worktree env: two distinct src/popoto copies — alias-collapse tests skipped"
            )
        assert "src.popoto" in sys.modules, (
            "pytest_configure did not register src.popoto in sys.modules. "
            "The alias-collapse fix did not run."
        )

    def test_src_popoto_redis_db_registered_in_sys_modules(self):
        """pytest_configure must register src.popoto.redis_db in sys.modules."""
        if not _is_single_tree_env():
            pytest.skip(
                "Worktree env: two distinct src/popoto copies — alias-collapse tests skipped"
            )
        assert (
            "src.popoto.redis_db" in sys.modules
        ), "pytest_configure did not register src.popoto.redis_db in sys.modules."

    def test_src_popoto_redis_db_on_test_db(self, pytestconfig):
        """sys.modules['src.popoto.redis_db'].POPOTO_REDIS_DB must be on the test DB.

        This is the key correctness invariant for issue #420: before the fix,
        src.popoto.redis_db was a distinct object with POPOTO_REDIS_DB still on DB 0.
        The fix ensures sys.modules['src.popoto.redis_db'] is the canonical module
        whose POPOTO_REDIS_DB has already been swapped to DB 15 by the session fixture.
        """
        if not _is_single_tree_env():
            pytest.skip(
                "Worktree env: two distinct src/popoto copies — alias-collapse tests skipped"
            )

        src_redis_db = sys.modules.get("src.popoto.redis_db")
        if src_redis_db is None:
            pytest.skip(
                "src.popoto.redis_db not in sys.modules — alias-collapse did not run"
            )

        db = src_redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db", 0)
        assert db != 0, (
            "src.popoto.redis_db.POPOTO_REDIS_DB is on DB 0 — the isolation fix did not work. "
            "Model saves via src.popoto would pollute DB 0 instead of the test DB."
        )
        expected = _resolve_test_db(pytestconfig)
        if expected is None:
            pytest.skip("plugin is inert (no popoto_test_db opt-in)")
        assert db == expected, f"Expected test DB {expected}, got DB {db}"

    def test_src_popoto_redis_db_is_canonical_redis_db(self):
        """sys.modules['src.popoto.redis_db'] must be the same object as popoto.redis_db.

        If they are distinct objects, swapping the connection pool on the canonical
        module does not affect the src.popoto.redis_db path.
        """
        if not _is_single_tree_env():
            pytest.skip(
                "Worktree env: two distinct src/popoto copies — alias-collapse tests skipped"
            )

        import popoto.redis_db as canonical_redis_db

        src_redis_db = sys.modules.get("src.popoto.redis_db")
        if src_redis_db is None:
            pytest.skip(
                "src.popoto.redis_db not in sys.modules — alias-collapse did not run"
            )

        assert src_redis_db is canonical_redis_db, (
            "sys.modules['src.popoto.redis_db'] is a distinct object from popoto.redis_db. "
            "The DB-15 swap applied to popoto.redis_db does not cover the src.popoto path."
        )

    def test_src_popoto_popoto_redis_db_identity(self):
        """POPOTO_REDIS_DB via src.popoto.redis_db is the same object as via popoto.redis_db.

        When both module paths share the same module object, they share the same
        POPOTO_REDIS_DB client, so in-place connection pool swaps are automatically
        visible on both paths.
        """
        if not _is_single_tree_env():
            pytest.skip(
                "Worktree env: two distinct src/popoto copies — alias-collapse tests skipped"
            )

        import popoto.redis_db as canonical_rdb

        src_rdb = sys.modules.get("src.popoto.redis_db")
        if src_rdb is None:
            pytest.skip(
                "src.popoto.redis_db not in sys.modules — alias-collapse did not run"
            )

        assert src_rdb.POPOTO_REDIS_DB is canonical_rdb.POPOTO_REDIS_DB, (
            "POPOTO_REDIS_DB is not the same object via both import paths. "
            "Writes via src.popoto would target a different Redis client than the test DB client."
        )

    def test_canonical_redis_db_on_test_db(self, pytestconfig):
        """The canonical popoto.redis_db.POPOTO_REDIS_DB must be on the resolved test DB.

        Baseline check that confirms the session fixture has run and the canonical
        connection has been swapped to the test DB. This test runs in all environments
        (single-tree and worktree alike).
        """
        import popoto.redis_db as canonical_rdb

        db = canonical_rdb.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get(
            "db", 0
        )
        assert db != 0, "popoto.redis_db.POPOTO_REDIS_DB is still on DB 0"
        expected = _resolve_test_db(pytestconfig)
        if expected is None:
            pytest.skip("plugin is inert (no popoto_test_db opt-in)")
        assert db == expected, f"Expected DB {expected}, got {db}"


class TestDecoyModuleNotStomped:
    """Regression: alias-collapse must not affect unrelated same-named downstream modules.

    Option A's original name-suffix matcher (``name.endswith(".popoto")``) would have
    stomped on unrelated modules like ``acme.popoto`` in sys.modules.  Option D
    (alias-collapse) does not enumerate sys.modules at all — it only writes
    ``src.popoto.*`` aliases — so unrelated entries are safe by construction.

    These tests verify the invariant in all environments (single-tree and worktree).
    """

    def test_decoy_module_not_stomped(self):
        """A stub 'acme.popoto' in sys.modules is not touched by the alias-collapse.

        Inject a decoy before re-running the alias-collapse step, then verify
        it is unchanged.  (The collapse already ran at session start via
        ``pytest_configure``; we call it again to verify idempotency and
        non-interference.)

        Calls ``_collapse_src_popoto()`` directly rather than the full
        ``pytest_configure(config)`` hook: the latter also invokes
        ``_configure_test_db(config)`` (#595's isolation-warning tripwire),
        which needs a real pytest ``Config`` — a ``MagicMock()`` makes
        ``config.getini(...)`` return a ``MagicMock`` (not a string), which
        ``_resolve_test_db`` treats as "not opted in" and re-arms the
        tripwire on this suite's already-isolated (opted-in) DB-15 pool,
        producing a spurious warning later in the session. This test is about
        alias-collapse idempotency, not DB-config resolution, so it exercises
        only the function it means to test.

        We load the module from src.popoto.pytest_plugin so the test
        exercises the new implementation even when the editable install still
        points to the pre-fix main-repo file (a worktree-only situation).
        """
        # Load the module under test.
        # src.popoto.pytest_plugin resolves to the worktree file in development;
        # in CI (post-merge) it resolves to the installed file (same code).
        try:
            _pp = importlib.import_module("src.popoto.pytest_plugin")
        except ImportError:
            _pp = importlib.import_module("popoto.pytest_plugin")

        if not hasattr(_pp, "_collapse_src_popoto"):
            pytest.skip(
                "_collapse_src_popoto not found in loaded pytest_plugin — "
                "running against pre-fix installed version; skip in worktree."
            )

        # Build a minimal stub that looks like a downstream package's 'popoto'.
        decoy = types.ModuleType("acme.popoto")
        decoy.__file__ = "/fake/acme/popoto/__init__.py"
        decoy.sentinel = "untouched"

        sys.modules["acme.popoto"] = decoy
        try:
            # Re-run the collapse — it must be idempotent and not touch acme.popoto.
            _pp._collapse_src_popoto()

            surviving = sys.modules.get("acme.popoto")
            assert surviving is decoy, (
                "pytest_configure replaced sys.modules['acme.popoto'] — "
                "the alias-collapse must only write src.* keys."
            )
            assert (
                getattr(surviving, "sentinel", None) == "untouched"
            ), "The decoy module's attributes were modified by pytest_configure."
        finally:
            sys.modules.pop("acme.popoto", None)

    def test_src_popoto_keys_only_written(self):
        """pytest_configure writes only src.popoto.* keys, never other namespaces.

        After the plugin's pytest_configure runs, sys.modules must contain
        src.popoto (and src.popoto.* submodules) but must NOT contain any
        unexpected new entries under unrelated namespace prefixes.
        """
        # Collect all keys that start with "src.popoto" — the expected aliases.
        src_popoto_keys = {
            k for k in sys.modules if k == "src.popoto" or k.startswith("src.popoto.")
        }

        # Every aliased entry must be a non-None module object.
        for key in src_popoto_keys:
            mod = sys.modules[key]
            assert mod is not None, f"sys.modules[{key!r}] is None after alias-collapse"

        # There must be at least one aliased entry (the collapse ran).
        assert len(src_popoto_keys) >= 1, (
            "No src.popoto.* entries in sys.modules — pytest_configure alias-collapse "
            "did not run or registered nothing."
        )


class TestDB0Tripwire:
    """Tests for the Option C clean-DB-0-only tripwire behavior.

    The tripwire SKIPs (never fails) when DB 0 is non-idle, and fires only on a
    clean DB 0. These tests verify the SKIP-on-non-idle path works correctly.
    """

    def test_db0_is_non_idle_in_dev(self):
        """On a dev box, DB 0 typically has keys — tripwire should SKIP.

        This test documents the expected behavior: DB 0 is non-idle in normal dev
        environments, so no tripwire assertion should run. If DB 0 is clean here,
        the test still passes (it just means we're in a clean-slate environment).
        """
        import redis

        # Connect to DB 0 directly (not through the plugin-managed connection)
        db0_client = redis.Redis(host="localhost", port=6379, db=0)
        db0_size = db0_client.dbsize()
        db0_client.close()

        # The key invariant: regardless of DB 0 state, the test DB (DB 15) is isolated
        from popoto import redis_db as rdb

        test_db_num = rdb.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db", 0)
        assert test_db_num != 0, f"Tests must not run on DB 0. DB 0 size={db0_size}."


class TestIsolatedDbSubprocess:
    """End-to-end, environment-independent proof that DB 0 is never written.

    The in-process ``TestSrcPopotoImportPaths`` checks SKIP in a git worktree
    (two physical src/popoto copies make the in-process identity asserts
    meaningless).  This test does not: it spawns a *fresh* pytest process, so
    the plugin's ``pytest_configure`` alias-collapse runs cleanly with no prior
    ``src.popoto`` imports, and the proof holds in both single-tree and worktree
    environments.  This is the keeper fix-proof for issue #420, replacing the
    destructive ``redis-cli -n 0 flushdb`` verification the original plan
    proposed.
    """

    def test_isolated_db_subprocess(self, tmp_path):
        """Spawn pytest against a stand-in DB; assert DB 0 is never written.

        A subprocess runs a probe test that imports via ``src.popoto``, saves a
        uniquely-named model, and asserts it is retrievable on the plugin's test
        DB.  After it exits, this parent process scans DB 0 for the probe's
        unique key prefix and asserts none leaked there.

        Non-destructive: only *reads* DB 0, matching a marker that cannot collide
        with real application data.  DB 0 is never flushed.
        """
        import os
        import subprocess
        import sys
        import textwrap

        import redis as _redis

        # Repo root sits one level above this test file's tests/ directory and
        # holds the src/ tree.  Running ``python -m pytest`` from there puts the
        # repo root on sys.path, so ``import src.popoto`` resolves.
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not os.path.isdir(os.path.join(repo_root, "src", "popoto")):
            pytest.skip("no src/ layout next to the test suite — alias-collapse N/A")

        marker = "SubprocIsolationProbe420"
        stand_in_db = "14"  # distinct from this session's DB 15

        probe = tmp_path / "test_subproc_isolation_probe.py"
        probe.write_text(textwrap.dedent(f"""
                import src.popoto as popoto


                class {marker}(popoto.Model):
                    name = popoto.KeyField()


                def test_src_popoto_write_lands_on_test_db():
                    {marker}(name="probe").save()
                    assert {marker}.query.get(name="probe") is not None
                """))

        # DB-0 client for the (non-destructive) leak check.  Connect on the same
        # host/port the suite uses, but always DB 0.  Whitelist the params:
        # redis-py 8 injects pool-internal keys (himport_registry,
        # maint_notifications_*) into connection_kwargs that redis.Redis(**kw)
        # rejects with TypeError.
        kwargs = redis_db.sibling_client_kwargs(
            redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs, db=0
        )
        db0 = _redis.Redis(**kwargs)

        def _marker_keys():
            return list(db0.scan_iter(match=f"{marker}*"))

        # Clear any residue from a previous (possibly broken) run so the check
        # measures only this run.  Touches only the unique marker prefix, which
        # cannot collide with real application data.
        stale = _marker_keys()
        if stale:
            db0.delete(*stale)

        env = dict(os.environ)
        env["POPOTO_TEST_DB"] = stand_in_db
        # Pin the subprocess to *this* working tree, so it validates the code
        # under test rather than whatever ``popoto`` happens to be pip-installed
        # in the active venv.  Prepending repo_root/src makes ``import popoto``
        # (and the auto-loaded plugin) resolve here; repo_root makes
        # ``import src.popoto`` resolve to the same physical files.  In CI the
        # installed package already is this tree, so these entries are a no-op.
        src_dir = os.path.join(repo_root, "src")
        existing_pp = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(
            [repo_root, src_dir] + ([existing_pp] if existing_pp else [])
        )
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(probe), "-q"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            "subprocess pytest run failed — src.popoto write may not have been "
            f"isolated to the test DB:\nSTDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )

        # Non-destructive DB-0 leak check: the uniquely-named probe must never
        # appear on DB 0.
        try:
            leaked = _marker_keys()
            if leaked:
                db0.delete(*leaked)  # don't leave residue for the next run
        finally:
            db0.close()

        assert not leaked, (
            f"src.popoto write leaked to DB 0 — isolation bypassed. "
            f"Found {len(leaked)} key(s): {leaked[:10]}"
        )


class TestSiblingClientKwargs:
    """Regression tests for ``redis_db.sibling_client_kwargs`` (issue #490).

    redis-py 8 injects pool-internal bookkeeping keys (``himport_registry``,
    ``maint_notifications_config``, ``maint_notifications_pool_handler``,
    ``orig_*``) into a connection pool's ``connection_kwargs``. Splatting that
    dict wholesale into ``redis.Redis(**kwargs)`` — as the DB-0 tripwire and the
    subprocess-isolation test used to — raises ``TypeError: Redis.__init__() got
    an unexpected keyword argument 'himport_registry'`` on redis-py 8. The
    helper whitelists only the standard connection params, so it is robust on
    every redis-py version. These tests inject a synthetic pool-only key so they
    are meaningful even on redis-py 7 (where the real keys do not exist).
    """

    def test_strips_unknown_pool_only_keys(self):
        """Pool-internal keys are dropped; standard params survive."""
        source = {
            "host": "localhost",
            "port": 6379,
            "password": "secret",
            "socket_timeout": 5,
            # redis-py 8 pool-injected keys that Redis.__init__ rejects:
            "himport_registry": object(),
            "maint_notifications_pool_handler": object(),
            "orig_host_address": "10.0.0.1",
        }
        out = redis_db.sibling_client_kwargs(source)
        assert out["host"] == "localhost"
        assert out["port"] == 6379
        assert out["password"] == "secret"
        assert out["socket_timeout"] == 5
        assert "himport_registry" not in out
        assert "maint_notifications_pool_handler" not in out
        assert "orig_host_address" not in out

    def test_overrides_win(self):
        """Explicit overrides replace inherited values (e.g. db=0 probe)."""
        source = {"host": "localhost", "port": 6379, "db": 15}
        out = redis_db.sibling_client_kwargs(source, db=0)
        assert out["db"] == 0

    def test_drops_none_values(self):
        """``None`` params are dropped so they never mask real defaults."""
        source = {"host": "localhost", "port": 6379, "password": None}
        out = redis_db.sibling_client_kwargs(source)
        assert "password" not in out

    def test_result_builds_a_real_client_from_a_live_pool(self):
        """The whitelisted kwargs from the LIVE pool build a usable client.

        This is the end-to-end guard: take the running connection's actual
        ``connection_kwargs`` (which on redis-py 8 carry the poisonous
        pool-internal keys), whitelist them for DB 0, and confirm the resulting
        ``redis.Redis(**kwargs)`` constructs and pings without a TypeError.
        Non-destructive: only pings, never writes.
        """
        import redis as _redis

        live_kwargs = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs
        kwargs = redis_db.sibling_client_kwargs(live_kwargs, db=0)
        client = _redis.Redis(**kwargs)
        try:
            assert client.ping() is True
        finally:
            client.close()


class TestIsolationWarning:
    """Tests for #595: warn once when the plugin is inert but popoto is used.

    All configure-time behavior under test (the one-shot tripwire armed in
    ``_configure_test_db`` and disarmed in ``pytest_unconfigure``) cannot be
    re-entered in-process — these are subprocess-based, like
    ``TestIsolatedDbSubprocess`` above.

    Every inert-path probe strips ``POPOTO_TEST_DB`` from the subprocess
    environment and points ``REDIS_URL`` at a non-zero DB (14, distinct from
    this session's 15). A developer with ``POPOTO_TEST_DB`` exported would
    otherwise make the plugin non-inert and the "no warning" probes would
    pass vacuously — the silent half is the dangerous one — so every inert
    probe also self-asserts its own inertness as its first statements.
    """

    @staticmethod
    def _repo_root():
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @classmethod
    def _base_env(cls, repo_root):
        """PYTHONPATH pinned to this worktree, like TestIsolatedDbSubprocess."""
        env = dict(os.environ)
        src_dir = os.path.join(repo_root, "src")
        existing_pp = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(
            [repo_root, src_dir] + ([existing_pp] if existing_pp else [])
        )
        return env

    @classmethod
    def _inert_env(cls, repo_root):
        """Env for a subprocess that must be genuinely inert (no opt-in)."""
        env = cls._base_env(repo_root)
        env.pop("POPOTO_TEST_DB", None)
        env["REDIS_URL"] = "redis://localhost:6379/14"
        return env

    @staticmethod
    def _run(argv, cwd, env):
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)

    def test_5a_inert_and_used_warns_once_naming_db(self, tmp_path):
        """Inert + popoto used → exactly one PopotoIsolationWarning, naming the DB."""
        repo_root = self._repo_root()
        probe = tmp_path / "test_5a_probe.py"
        probe.write_text(textwrap.dedent("""
                def test_probe(pytestconfig):
                    import os

                    assert pytestconfig.getini("popoto_test_db") == ""
                    assert "POPOTO_TEST_DB" not in os.environ

                    import popoto

                    class Probe5a(popoto.Model):
                        name = popoto.KeyField()

                    Probe5a(name="x").save()
                """))
        result = self._run(
            [sys.executable, "-m", "pytest", str(probe), "-q"],
            repo_root,
            self._inert_env(repo_root),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        out = result.stdout
        assert out.count("PopotoIsolationWarning") == 1, out
        assert "DB 14" in out, out
        assert "popoto_test_db" in out
        assert "POPOTO_TEST_DB" in out

    def test_5b_ini_opt_in_produces_no_warning(self, tmp_path):
        """Ini opt-in (-o popoto_test_db=15) → no warning; session is on DB 15.

        A tmp_path probe has no ini file of its own (pytest resolves
        ``inipath = None`` for it even with ``cwd=repo_root``, so the repo's
        own pyproject.toml never reaches the subprocess) — the opt-in must be
        driven from argv instead.
        """
        repo_root = self._repo_root()
        probe = tmp_path / "test_5b_probe.py"
        probe.write_text(textwrap.dedent("""
                def test_probe():
                    import popoto
                    from popoto import redis_db

                    class Probe5b(popoto.Model):
                        name = popoto.KeyField()

                    Probe5b(name="x").save()
                    db = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get(
                        "db"
                    )
                    assert db == 15, f"expected DB 15, got {db}"
                """))
        result = self._run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(probe),
                "-q",
                "-o",
                "popoto_test_db=15",
            ],
            repo_root,
            self._inert_env(repo_root),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PopotoIsolationWarning" not in result.stdout

    def test_5c_env_opt_in_produces_no_warning(self, tmp_path):
        """Env opt-in (POPOTO_TEST_DB=15) → no warning; session is on DB 15."""
        repo_root = self._repo_root()
        probe = tmp_path / "test_5c_probe.py"
        probe.write_text(textwrap.dedent("""
                def test_probe():
                    import popoto
                    from popoto import redis_db

                    class Probe5c(popoto.Model):
                        name = popoto.KeyField()

                    Probe5c(name="x").save()
                    db = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get(
                        "db"
                    )
                    assert db == 15, f"expected DB 15, got {db}"
                """))
        env = self._base_env(repo_root)
        env["POPOTO_TEST_DB"] = "15"
        result = self._run(
            [sys.executable, "-m", "pytest", str(probe), "-q"], repo_root, env
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PopotoIsolationWarning" not in result.stdout

    def test_5d_inert_and_unused_produces_no_warning(self, tmp_path):
        """Inert + popoto importable but unused (no Redis op) → no warning."""
        repo_root = self._repo_root()
        probe = tmp_path / "test_5d_probe.py"
        probe.write_text(textwrap.dedent("""
                def test_probe(pytestconfig):
                    import os

                    assert pytestconfig.getini("popoto_test_db") == ""
                    assert "POPOTO_TEST_DB" not in os.environ

                    import popoto

                    class Probe5d(popoto.Model):
                        name = popoto.KeyField()

                    assert Probe5d is not None
                """))
        result = self._run(
            [sys.executable, "-m", "pytest", str(probe), "-q"],
            repo_root,
            self._inert_env(repo_root),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PopotoIsolationWarning" not in result.stdout

    def test_5e_warning_error_filter_does_not_break_the_redis_op(self, tmp_path):
        """Under -W error::UserWarning the popoto op still succeeds.

        The wrapper's ``warnings.warn`` is exception-guarded and runs after
        an unguarded ``logger.warning`` mirror, so a downstream suite's
        ``filterwarnings = error`` cannot turn the advisory into a Redis
        failure. The mirror line is invisible on a passing run without live
        log flags — pytest's logging plugin only prints the captured-log
        section on failure — so this is run with ``-o log_cli=true
        --log-cli-level=WARNING`` and asserted against stdout+stderr rather
        than ``caplog`` (the trip happens in a different process).
        """
        repo_root = self._repo_root()
        probe = tmp_path / "test_5e_probe.py"
        probe.write_text(textwrap.dedent("""
                def test_probe(pytestconfig):
                    import os

                    assert pytestconfig.getini("popoto_test_db") == ""
                    assert "POPOTO_TEST_DB" not in os.environ

                    import popoto

                    class Probe5e(popoto.Model):
                        name = popoto.KeyField()

                    Probe5e(name="x").save()
                    assert Probe5e.query.get(name="x") is not None
                """))
        result = self._run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(probe),
                "-q",
                "-W",
                "error::UserWarning",
                "-o",
                "log_cli=true",
                "--log-cli-level=WARNING",
            ],
            repo_root,
            self._inert_env(repo_root),
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "popoto is writing to Redis DB" in combined, combined

    def test_5f_disarm_leaves_nothing_behind(self, tmp_path):
        """Inert + unused → after pytest_unconfigure, the pool carries no
        instance-level ``get_connection`` attribute (disarm left nothing
        behind). Checked via an ``atexit`` hook, which fires after
        ``pytest_unconfigure`` but before process exit.
        """
        repo_root = self._repo_root()
        probe = tmp_path / "test_5f_probe.py"
        probe.write_text(textwrap.dedent("""
                import atexit


                def _check_disarmed():
                    from popoto import redis_db

                    pool = redis_db.POPOTO_REDIS_DB.connection_pool
                    print(
                        "TRIPWIRE_DISARMED="
                        + str("get_connection" not in pool.__dict__)
                    )


                atexit.register(_check_disarmed)


                def test_probe(pytestconfig):
                    import os

                    assert pytestconfig.getini("popoto_test_db") == ""
                    assert "POPOTO_TEST_DB" not in os.environ

                    import popoto

                    class Probe5f(popoto.Model):
                        name = popoto.KeyField()

                    assert Probe5f is not None
                """))
        result = self._run(
            [sys.executable, "-m", "pytest", str(probe), "-q"],
            repo_root,
            self._inert_env(repo_root),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "TRIPWIRE_DISARMED=True" in result.stdout, result.stdout

    def test_5g_arming_is_non_fatal(self, tmp_path):
        """A conftest that rebinds POPOTO_REDIS_DB to something without
        ``.connection_pool`` must not abort the session — arming is wrapped
        in ``try/except Exception`` (round-3 blocker regression test).
        """
        repo_root = self._repo_root()
        (tmp_path / "conftest.py").write_text(textwrap.dedent("""
                from popoto import redis_db


                class _NoPoolClient:
                    @property
                    def connection_pool(self):
                        raise RuntimeError("no connection_pool here (5g probe)")


                def pytest_configure(config):
                    redis_db.POPOTO_REDIS_DB = _NoPoolClient()
                """))
        probe = tmp_path / "test_5g_probe.py"
        probe.write_text(textwrap.dedent("""
                def test_probe():
                    assert True
                """))
        result = self._run(
            [sys.executable, "-m", "pytest", str(probe), "-q"],
            repo_root,
            self._inert_env(repo_root),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout, result.stdout


class TestResolutionChain:
    """#549: pin the env>ini>None resolution chain and the inert plumbing contract.

    Reuses ``TestIsolationWarning``'s subprocess helpers (``_repo_root``,
    ``_base_env``, ``_inert_env``, ``_run``) by delegation rather than building a
    parallel harness. Deliberately NOT a subclass: inheriting would make pytest
    re-collect and re-run every ``TestIsolationWarning`` test under this class.

    Every child below pins ``REDIS_URL`` to an explicit non-zero DB
    before popoto is imported: CI exports a db-less ``REDIS_URL`` and a child
    that inherits it unpinned would land wherever that URL points (#603, #577).
    """

    def test_env_var_beats_ini_option(self, tmp_path, pytestconfig):
        """POPOTO_TEST_DB wins over the ini opt-in, with a literal expectation.

        The child gets ``-o popoto_test_db=15`` on argv *and*
        ``POPOTO_TEST_DB=12`` in its env; it must land on 12. The expectation is
        a hardcoded literal on purpose — resolving it through
        ``_resolve_test_db`` would move both sides of the assertion together and
        hide a resolver bug. This is the test that keeps the five
        resolution-based assertions above honest.
        """
        # An opted-in child flushes its own DB before each of its tests. If a
        # parent suite ever runs on the child's DB, that flush would wipe the
        # parent's live DB mid-test. Fail loudly instead of colliding silently.
        assert _resolve_test_db(pytestconfig) != _ENV_OVERRIDE_CHILD_DB, (
            f"Parent session is on DB {_ENV_OVERRIDE_CHILD_DB}, which this test's "
            "child process flushes. Pick a different _ENV_OVERRIDE_CHILD_DB."
        )

        repo_root = TestIsolationWarning._repo_root()
        probe = tmp_path / "test_env_beats_ini_probe.py"
        probe.write_text(textwrap.dedent("""
                def test_probe(pytestconfig):
                    # Prove the ini opt-in really reached this child: without
                    # this, the test would still pass on the env var alone and
                    # the "beats" half would be hollow.
                    assert pytestconfig.getini("popoto_test_db") == "15"

                    from popoto import redis_db

                    db = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get(
                        "db"
                    )
                    assert db == __CHILD_DB__, (
                        f"expected env override to win with DB __CHILD_DB__, got {db}"
                    )
            """).replace("__CHILD_DB__", str(_ENV_OVERRIDE_CHILD_DB)))
        env = TestIsolationWarning._base_env(repo_root)
        env["POPOTO_TEST_DB"] = str(_ENV_OVERRIDE_CHILD_DB)
        env["REDIS_URL"] = f"redis://localhost:6379/{_INERT_PROBE_DB}"
        result = TestIsolationWarning._run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(probe),
                "-q",
                "-o",
                "popoto_test_db=15",
            ],
            repo_root,
            env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout, result.stdout

    def test_inert_plugin_performs_no_swap_and_no_flush(self, tmp_path):
        """#549: an inert plugin must not swap the connection nor flush the DB.

        #595 covers the *warning* half of the inert path; this is the *plumbing*
        half. The child self-asserts its own inertness first, so a developer with
        ``POPOTO_TEST_DB`` exported cannot make this pass vacuously.

        The marker key co-resides on DB 14 with #595's ``test_5a``/``test_5d``
        probes. That is safe only because the inert path never flushes — today
        the inert branch of ``_configure_test_db`` arms a tripwire and returns,
        touching nothing. If that ever changes, this test and #595's fail
        together; this note is the explanation a future reader will need.
        """
        import uuid

        import redis

        repo_root = TestIsolationWarning._repo_root()
        marker = f"popoto_inert_probe:{uuid.uuid4().hex}"

        # Build the parent-side probe client with the repo's helper rather than
        # splatting a live pool's connection_kwargs, which redis-py 8 rejects
        # (himport_registry, maint_notifications_*, orig_*) -- the #490 crash.
        kwargs = redis_db.sibling_client_kwargs(
            redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs,
            db=_INERT_PROBE_DB,
        )
        probe_client = redis.Redis(**kwargs)

        probe = tmp_path / "test_inert_plumbing_probe.py"
        probe.write_text(textwrap.dedent("""
                def test_probe(pytestconfig):
                    import os

                    # Prove the plugin really is inert before asserting on it.
                    assert pytestconfig.getini("popoto_test_db") == ""
                    assert "POPOTO_TEST_DB" not in os.environ

                    from popoto import redis_db

                    db = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get(
                        "db"
                    )
                    assert db == __PROBE_DB__, (
                        f"inert plugin swapped the DB: got {db}"
                    )
            """).replace("__PROBE_DB__", str(_INERT_PROBE_DB)))

        try:
            probe_client.set(marker, "survives")
            result = TestIsolationWarning._run(
                [sys.executable, "-m", "pytest", str(probe), "-q"],
                repo_root,
                TestIsolationWarning._inert_env(repo_root),
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert probe_client.get(marker) == b"survives", (
                "Marker key vanished from DB "
                f"{_INERT_PROBE_DB}: the inert plugin flushed a database it "
                "was never opted in to touch."
            )
        finally:
            probe_client.delete(marker)
            probe_client.close()
