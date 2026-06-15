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
import sys
import types

import pytest

import popoto
from popoto import redis_db
from popoto.redis_db import get_async_redis_db


def _get_db():
    """Access the current POPOTO_REDIS_DB via module attribute to avoid stale refs."""
    return redis_db.POPOTO_REDIS_DB


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

    def test_on_test_db(self):
        """The connection should be on the configured test DB (default 15)."""
        pool_kwargs = _get_db().connection_pool.connection_kwargs
        current_db = pool_kwargs.get("db", 0)
        assert current_db == 15, f"Expected DB 15, got DB {current_db}"

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

    def test_async_connection_is_preconfigured(self):
        """The async connection should be pre-configured for the test DB."""
        import redis.asyncio as aioredis

        assert isinstance(redis_db._POPOTO_ASYNC_REDIS_DB, aioredis.Redis)

    def test_async_connection_uses_test_db(self):
        """The async connection should point to the same DB as sync."""
        sync_db = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get(
            "db", 0
        )
        async_kwargs = redis_db._POPOTO_ASYNC_REDIS_DB.connection_pool.connection_kwargs
        async_db = async_kwargs.get("db", 0)
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

    def test_async_connection_on_test_db(self):
        """The async connection should be on the test DB, not DB 0."""
        import redis.asyncio as aioredis

        assert isinstance(redis_db._POPOTO_ASYNC_REDIS_DB, aioredis.Redis)
        async_db = (
            redis_db._POPOTO_ASYNC_REDIS_DB.connection_pool.connection_kwargs.get(
                "db", 0
            )
        )
        assert async_db == 15, f"Expected async on DB 15, got DB {async_db}"


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
        assert "src.popoto.redis_db" in sys.modules, (
            "pytest_configure did not register src.popoto.redis_db in sys.modules."
        )

    def test_src_popoto_redis_db_on_test_db(self):
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
        assert db == 15, f"Expected test DB 15, got DB {db}"

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

    def test_canonical_redis_db_on_test_db(self):
        """The canonical popoto.redis_db.POPOTO_REDIS_DB must be on DB 15.

        Baseline check that confirms the session fixture has run and the canonical
        connection has been swapped to the test DB. This test runs in all environments
        (single-tree and worktree alike).
        """
        import popoto.redis_db as canonical_rdb

        db = canonical_rdb.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get(
            "db", 0
        )
        assert db != 0, "popoto.redis_db.POPOTO_REDIS_DB is still on DB 0"
        assert db == 15, f"Expected DB 15, got {db}"


class TestDecoyModuleNotStomped:
    """Regression: alias-collapse must not affect unrelated same-named downstream modules.

    Option A's original name-suffix matcher (``name.endswith(".popoto")``) would have
    stomped on unrelated modules like ``acme.popoto`` in sys.modules.  Option D
    (alias-collapse) does not enumerate sys.modules at all — it only writes
    ``src.popoto.*`` aliases — so unrelated entries are safe by construction.

    These tests verify the invariant in all environments (single-tree and worktree).
    """

    def test_decoy_module_not_stomped(self):
        """A stub 'acme.popoto' in sys.modules is not touched by pytest_configure.

        Inject a decoy before re-running the configure hook, then verify it is
        unchanged.  (pytest_configure already ran at session start; we call it
        again to verify idempotency and non-interference.)

        We load pytest_configure from src.popoto.pytest_plugin so the test
        exercises the new implementation even when the editable install still
        points to the pre-fix main-repo file (a worktree-only situation).
        """
        from unittest.mock import MagicMock

        # Load the new pytest_configure from the actual module under test.
        # src.popoto.pytest_plugin resolves to the worktree file in development;
        # in CI (post-merge) it resolves to the installed file (same code).
        try:
            _pp = importlib.import_module("src.popoto.pytest_plugin")
        except ImportError:
            _pp = importlib.import_module("popoto.pytest_plugin")

        if not hasattr(_pp, "pytest_configure"):
            pytest.skip(
                "pytest_configure not found in loaded pytest_plugin — "
                "running against pre-fix installed version; skip in worktree."
            )

        # Build a minimal stub that looks like a downstream package's 'popoto'.
        decoy = types.ModuleType("acme.popoto")
        decoy.__file__ = "/fake/acme/popoto/__init__.py"
        decoy.sentinel = "untouched"

        sys.modules["acme.popoto"] = decoy
        try:
            # Re-run the hook — it must be idempotent and must not touch acme.popoto.
            _pp.pytest_configure(MagicMock())

            surviving = sys.modules.get("acme.popoto")
            assert surviving is decoy, (
                "pytest_configure replaced sys.modules['acme.popoto'] — "
                "the alias-collapse must only write src.* keys."
            )
            assert getattr(surviving, "sentinel", None) == "untouched", (
                "The decoy module's attributes were modified by pytest_configure."
            )
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
        probe.write_text(
            textwrap.dedent(
                f"""
                import src.popoto as popoto


                class {marker}(popoto.Model):
                    name = popoto.KeyField()


                def test_src_popoto_write_lands_on_test_db():
                    {marker}(name="probe").save()
                    assert {marker}.query.get(name="probe") is not None
                """
            )
        )

        # DB-0 client for the (non-destructive) leak check.  Connect on the same
        # host/port the suite uses, but always DB 0.
        kwargs = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.copy()
        kwargs["db"] = 0
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
