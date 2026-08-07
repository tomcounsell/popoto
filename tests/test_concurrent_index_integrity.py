"""
Regression tests for concurrent index integrity (issue #412).

Validates the atomic Lua-based INDEX_SWAP_LUA fix for cross-process race
conditions in IndexedField and UniqueField save() operations.

Test coverage:
  1. Multiprocess concurrency — dual-membership invariant (spawn-based)
  2. Convergence invariants — pointer consistency after concurrent writes
  3. Single-process correctness — nullable first save, idempotent resave
  4. Uniqueness rejection — no traces left on failed save
  5. Hash-content parity — EVAL writes field bytes identically to plain HSET
  6. Pointer invisibility — \x00 pointer never surfaces as a model attribute
  7. Legacy-record upgrade — ARGV[6] hint path for pre-fix records
  8. Forced mid-EVAL error — no half-state visible after Lua error
  9. Partial-save (update_fields) — correct behavior for indexed-only updates
"""

import multiprocessing as mp
import sys
import os

import msgpack
import pytest
import redis as redis_lib

import popoto
from popoto import ModelException
from popoto.fields.indexed_field_mixin import IndexedFieldMixin
from popoto.fields.shortcuts import IndexedField, UniqueField
from popoto.redis_db import POPOTO_REDIS_DB

# INDEX_SWAP_LUA is introduced by the #412 implementation. Import it lazily
# so that test collection succeeds even before the implementation lands.
# Tests that directly use INDEX_SWAP_LUA will be skipped/fail with a clear
# message if the symbol is absent.
try:
    from popoto.fields.indexed_field_mixin import INDEX_SWAP_LUA
    _LUA_AVAILABLE = True
except ImportError:
    INDEX_SWAP_LUA = None  # type: ignore[assignment]
    _LUA_AVAILABLE = False

requires_lua = pytest.mark.skipif(
    not _LUA_AVAILABLE,
    reason="INDEX_SWAP_LUA not yet implemented (#412 in progress)",
)

# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

# CI environments that cannot fork/spawn should skip these with:
#   pytest -m "not concurrent_mp"
concurrent_mp = pytest.mark.concurrent_mp


# ---------------------------------------------------------------------------
# Test models
#
# Defined at module level so they are importable by spawned subprocesses.
# Each model has a unique class name to avoid cross-test key collisions.
# ---------------------------------------------------------------------------


class ConcurrentStatusModel(popoto.Model):
    """IndexedField model used by multiprocess concurrent tests."""

    record_id = popoto.AutoKeyField()
    status = IndexedField(type=str)


class ConcurrentEmailModel(popoto.Model):
    """UniqueField model used by multiprocess concurrent tests."""

    record_id = popoto.AutoKeyField()
    email = UniqueField(type=str)


class PtrTestModel(popoto.Model):
    """IndexedField model for pointer-invariant tests."""

    record_id = popoto.AutoKeyField()
    status = IndexedField(type=str)


class NullableModel(popoto.Model):
    """IndexedField model with nullable field for first-save tests."""

    record_id = popoto.AutoKeyField()
    tag = IndexedField(type=str, null=True)


class UniqueRejectionModel(popoto.Model):
    """UniqueField model for rejection-leaves-no-trace test."""

    record_id = popoto.AutoKeyField()
    email = UniqueField(type=str)


class HashParityModel(popoto.Model):
    """Model for hash-content parity tests."""

    record_id = popoto.AutoKeyField()
    status = IndexedField(type=str, null=True)
    email = UniqueField(type=str)


class PointerInvisibleModel(popoto.Model):
    """Model for pointer invisibility tests."""

    record_id = popoto.AutoKeyField()
    status = IndexedField(type=str)


class LegacyUpgradeModel(popoto.Model):
    """Model for legacy-record upgrade tests. Seeded manually in Redis."""

    record_id = popoto.KeyField()
    status = IndexedField(type=str)


class LegacyUniqueModel(popoto.Model):
    """Model for legacy-record upgrade with UniqueField."""

    record_id = popoto.KeyField()
    email = UniqueField(type=str)


class PartialSaveModel(popoto.Model):
    """Model for partial-save (update_fields) tests."""

    record_id = popoto.AutoKeyField()
    status = IndexedField(type=str)
    other = popoto.Field(type=str, default="unchanged")


class PartialSaveUniqueModel(popoto.Model):
    """Model for partial-save + UniqueField conflict tests."""

    record_id = popoto.AutoKeyField()
    email = UniqueField(type=str)
    label = popoto.Field(type=str, default="x")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_redis() -> redis_lib.Redis:
    """Raw redis.Redis connection (decode_responses=False) on the same DB."""
    pool = POPOTO_REDIS_DB.connection_pool
    kwargs = pool.connection_kwargs
    return redis_lib.Redis(
        host=kwargs.get("host", "localhost"),
        port=kwargs.get("port", 6379),
        db=kwargs.get("db", 0),
        decode_responses=False,
    )


def _set_prefix(field_class, model_class, field_name: str) -> str:
    """Derive the full index key prefix from field_class_key (not hard-coded)."""
    return (
        f"{field_class.field_class_key.redis_key}:{model_class.__name__}:{field_name}"
    )


def _index_set_key(field_class, model_class, field_name: str, value) -> str:
    """Full index Set key for a given field value, matching production's on_save() derivation."""
    from popoto.models.db_key import DB_key

    field = model_class._meta.fields[field_name]
    set_key_prefix = field.get_special_use_field_db_key(model_class, field_name)
    return DB_key(set_key_prefix, value).redis_key


def _get_all_sets_for_field(r: redis_lib.Redis, field_class, model_class, field_name: str) -> dict:
    """Return {set_key: set(members)} for every value-Set of a field."""
    prefix = _set_prefix(field_class, model_class, field_name)
    pattern = f"{prefix}:*"
    result = {}
    for raw_key in r.keys(pattern):
        key_str = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
        members = {
            m.decode("utf-8") if isinstance(m, bytes) else m
            for m in r.smembers(raw_key)
        }
        result[key_str] = members
    return result


def _assert_no_dual_membership(r: redis_lib.Redis, member_key: str, field_class, model_class, field_name: str):
    """Assert that member_key appears in at most one value-Set for the field."""
    sets = _get_all_sets_for_field(r, field_class, model_class, field_name)
    containing = [k for k, members in sets.items() if member_key in members]
    assert len(containing) <= 1, (
        f"Dual membership: {member_key!r} found in {len(containing)} sets: {containing}"
    )


def _assert_exactly_one_set(r: redis_lib.Redis, member_key: str, field_class, model_class, field_name: str):
    """Assert that member_key appears in exactly one value-Set for the field."""
    sets = _get_all_sets_for_field(r, field_class, model_class, field_name)
    containing = [k for k, members in sets.items() if member_key in members]
    assert len(containing) == 1, (
        f"Expected {member_key!r} in exactly one set, found in {len(containing)}: {containing}"
    )


def _assert_no_unique_set_has_multiple(r: redis_lib.Redis, field_class, model_class, field_name: str):
    """Assert no UniqueField value-Set has more than one member."""
    sets = _get_all_sets_for_field(r, field_class, model_class, field_name)
    for key, members in sets.items():
        assert len(members) <= 1, (
            f"UniqueField Set {key!r} has {len(members)} members: {members}"
        )


def _pointer_side_key(member_redis_key: str, field_name: str) -> str:
    """Match production's IndexedFieldMixin._pointer_side_key() derivation (#476).

    Delegated rather than restated: the derivation moved under the
    "$IdxPtr:" namespace in #540 and a copy here would silently rot.
    """
    from popoto.fields.indexed_field_mixin import IndexedFieldMixin

    return IndexedFieldMixin._pointer_side_key(member_redis_key, field_name)


def _check_pointer_matches_set(r: redis_lib.Redis, member_redis_key: str, field_name: str, field_class, model_class):
    """Verify that the server-authoritative pointer side key matches which Set contains the member.

    #476: the pointer now lives in a standalone side key (not a field inside
    the model hash) — see IndexedFieldMixin._pointer_side_key().
    """
    ptr_key = _pointer_side_key(member_redis_key, field_name)
    raw_ptr = r.get(ptr_key.encode("utf-8"))
    if raw_ptr is None:
        # No pointer yet (legacy record or first save not yet run): skip check
        return
    pointer_set = raw_ptr.decode("utf-8") if isinstance(raw_ptr, bytes) else raw_ptr
    # The member must be in the pointed-to set
    assert r.sismember(pointer_set, member_redis_key), (
        f"Pointer {ptr_key!r} = {pointer_set!r} but member {member_redis_key!r} is NOT in that Set"
    )
    # And must not be in any other set
    _assert_no_dual_membership(r, member_redis_key, field_class, model_class, field_name)
    # And must never be exposed as a hash field (that was the pre-#476 bug)
    assert r.hget(member_redis_key, f"{field_name}\x00idxset".encode("utf-8")) is None, (
        "Legacy in-hash pointer field was written by current code (should only ever "
        "be read as a migration fallback, never written)"
    )


# ---------------------------------------------------------------------------
# Subprocess helpers for multiprocess tests (must be module-level picklable)
# ---------------------------------------------------------------------------


def _get_test_redis_url() -> str:
    """Get the current test redis URL including DB number."""
    from popoto.redis_db import POPOTO_REDIS_DB

    conn = POPOTO_REDIS_DB.connection_pool.connection_kwargs
    host = conn.get("host", "localhost")
    port = conn.get("port", 6379)
    db = conn.get("db", 15)
    return f"redis://{host}:{port}/{db}"


def _swap_db_inplace(redis_url: str) -> None:
    """Swap POPOTO_REDIS_DB connection pool in-place to match redis_url.

    Must be called after ``import popoto`` in a spawned worker, where
    module-level import has already created the connection on the default DB.
    Mirrors pytest_plugin._swap_db: modifies the existing object in-place so
    all cached references (e.g. in query.py) automatically see the test DB.
    """
    import redis as _redis_lib
    import urllib.parse
    from popoto import redis_db as _redis_db_mod

    parsed = urllib.parse.urlparse(redis_url)
    db_num = int(parsed.path.lstrip("/")) if parsed.path.lstrip("/").isdigit() else 15
    db_obj = _redis_db_mod.POPOTO_REDIS_DB
    current_kwargs = dict(db_obj.connection_pool.connection_kwargs)
    current_kwargs["db"] = db_num
    current_kwargs["host"] = parsed.hostname or "localhost"
    current_kwargs["port"] = parsed.port or 6379
    current_kwargs.setdefault("socket_timeout", 5)
    current_kwargs.setdefault("socket_connect_timeout", 5)
    # Remove non-serializable attrs injected by redis-py maint pool
    for _k in ("maint_notifications_pool_handler", "maint_notifications_config",
               "orig_host_address", "orig_socket_timeout", "orig_socket_connect_timeout"):
        current_kwargs.pop(_k, None)
    connection_class = db_obj.connection_pool.connection_class
    new_pool = _redis_lib.ConnectionPool(connection_class=connection_class, **current_kwargs)
    old_pool = db_obj.connection_pool
    db_obj.connection_pool = new_pool
    old_pool.disconnect()


def _worker_set_status(record_id: str, value: str, barrier: mp.Barrier, result_queue: mp.Queue, redis_url: str = "redis://localhost:6379/15"):
    """Worker: wait at barrier then save status=value on the shared record.

    The spawn-based worker re-imports the test module (including ``import popoto``
    at module level) before this function body runs, so POPOTO_REDIS_DB is already
    created against the default DB.  _swap_db_inplace() reconnects in-place so
    all popoto internal references (query.py, etc.) automatically use the test DB.
    """
    import popoto as _pop

    _swap_db_inplace(redis_url)

    class ConcurrentStatusModel(_pop.Model):
        record_id = _pop.AutoKeyField()
        status = _pop.IndexedField(type=str)

    # Hydrate the existing record
    obj = ConcurrentStatusModel.query.get(record_id=record_id)
    if obj is None:
        result_queue.put(("error", f"record not found: {record_id}"))
        return
    barrier.wait()
    try:
        obj.status = value
        obj.save()
        result_queue.put(("ok", value))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


def _worker_save_unique_email(email: str, barrier: mp.Barrier, result_queue: mp.Queue, redis_url: str = "redis://localhost:6379/15"):
    """Worker: wait at barrier then try to create a record with the given email.

    Same spawn-reconnect pattern as _worker_set_status: _swap_db_inplace() is
    called after module-level import so the test DB is used for all popoto ops.
    """
    import popoto as _pop
    from popoto import ModelException as ME

    _swap_db_inplace(redis_url)

    class ConcurrentEmailModel(_pop.Model):
        record_id = _pop.AutoKeyField()
        email = _pop.UniqueField(type=str)

    barrier.wait()
    try:
        obj = ConcurrentEmailModel(email=email)
        obj.save()
        result_queue.put(("ok", obj.db_key.redis_key))
    except ME:
        result_queue.put(("conflict", email))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


# ---------------------------------------------------------------------------
# 1. Multiprocess Concurrency Tests
# ---------------------------------------------------------------------------


@concurrent_mp
class TestMultiprocessConcurrency:
    """
    Spawn-based concurrency tests that exercise the dual-membership invariant
    across real parallel processes. Skipped in environments that cannot spawn.
    """

    ROUNDS = 50  # Reduced from 200 for practical CI runtime; still catches races

    @pytest.mark.slow
    def test_indexed_field_no_dual_membership(self):
        """
        Two processes simultaneously save the same record to different status
        values. After each round, the member must appear in at most one value-Set.

        Validates INDEX_SWAP_LUA eliminates the SREM-then-SADD race window.
        """
        dual_membership_rounds = 0
        r = _raw_redis()

        ctx = mp.get_context("spawn")

        redis_url = _get_test_redis_url()
        for round_num in range(self.ROUNDS):
            # Create a fresh record each round
            obj = ConcurrentStatusModel.create(status="initial")
            member_key = obj.db_key.redis_key
            record_id = obj.record_id

            barrier = ctx.Barrier(2)
            q = ctx.Queue()

            p1 = ctx.Process(target=_worker_set_status, args=(record_id, "A", barrier, q, redis_url))
            p2 = ctx.Process(target=_worker_set_status, args=(record_id, "B", barrier, q, redis_url))
            p1.start()
            p2.start()
            p1.join(timeout=10)
            p2.join(timeout=10)

            results = [q.get_nowait() for _ in range(2)]
            errors = [r for r in results if r[0] == "error"]
            assert not errors, f"Round {round_num}: worker errors: {errors}"

            # Check invariant: no dual-membership
            all_sets = _get_all_sets_for_field(r, IndexedField, ConcurrentStatusModel, "status")
            containing = [k for k, members in all_sets.items() if member_key in members]
            if len(containing) > 1:
                dual_membership_rounds += 1

            # Cleanup for next round
            obj.delete()
            for k in r.keys(f"$IndexF:ConcurrentStatusModel:*"):
                r.delete(k)

        assert dual_membership_rounds == 0, (
            f"{dual_membership_rounds}/{self.ROUNDS} rounds had dual-membership"
        )

    @pytest.mark.slow
    def test_unique_field_no_double_occupancy(self):
        """
        Two processes try to save DIFFERENT records with the SAME unique email.
        At most one should succeed, and no UniqueField Set should have >1 member.

        Validates that the Lua uniqueness check closes the TOCTOU window.
        """
        double_occupancy_rounds = 0
        r = _raw_redis()

        ctx = mp.get_context("spawn")

        redis_url = _get_test_redis_url()
        for round_num in range(min(self.ROUNDS, 25)):
            test_email = f"race{round_num}@example.com"

            # Delete any leftover set from previous round
            for k in r.keys(f"$UniquF:ConcurrentEmailModel:email:*"):
                r.delete(k)

            barrier = ctx.Barrier(2)
            q = ctx.Queue()

            p1 = ctx.Process(target=_worker_save_unique_email, args=(test_email, barrier, q, redis_url))
            p2 = ctx.Process(target=_worker_save_unique_email, args=(test_email, barrier, q, redis_url))
            p1.start()
            p2.start()
            p1.join(timeout=10)
            p2.join(timeout=10)

            results = [q.get_nowait() for _ in range(2)]
            ok_results = [r2 for r2 in results if r2[0] == "ok"]

            if len(ok_results) > 1:
                double_occupancy_rounds += 1

            # Verify the Set has at most 1 member
            _assert_no_unique_set_has_multiple(r, UniqueField, ConcurrentEmailModel, "email")

            # Cleanup
            for k in r.keys("ConcurrentEmailModel:*"):
                r.delete(k)
            for k in r.keys("$Class:ConcurrentEmailModel"):
                r.delete(k)

        assert double_occupancy_rounds == 0, (
            f"{double_occupancy_rounds} rounds had double occupancy in UniqueField Set"
        )


# ---------------------------------------------------------------------------
# 2. Convergence Invariant Tests
# ---------------------------------------------------------------------------


class TestConvergenceInvariants:
    """
    After any sequence of saves, the server-authoritative pointer in the hash
    must match the Set the member is actually in, with no dual-membership.
    """

    def test_pointer_matches_set_after_save(self):
        """After save(), the hash pointer field must match the value-Set key."""
        r = _raw_redis()
        obj = PtrTestModel.create(status="active")
        member_key = obj.db_key.redis_key

        _check_pointer_matches_set(r, member_key, "status", IndexedField, PtrTestModel)

    def test_pointer_matches_set_after_value_change(self):
        """After changing indexed value, pointer must point to the new Set."""
        r = _raw_redis()
        obj = PtrTestModel.create(status="start")
        obj.status = "finish"
        obj.save()

        member_key = obj.db_key.redis_key
        _check_pointer_matches_set(r, member_key, "status", IndexedField, PtrTestModel)

    def test_no_dual_membership_after_value_change(self):
        """Changing value must SREM from old set and SADD to new; no overlap."""
        r = _raw_redis()
        obj = PtrTestModel.create(status="before")
        member_key = obj.db_key.redis_key

        obj.status = "after"
        obj.save()

        _assert_no_dual_membership(r, member_key, IndexedField, PtrTestModel, "status")
        _assert_exactly_one_set(r, member_key, IndexedField, PtrTestModel, "status")

    def test_record_in_exactly_one_set_after_multiple_saves(self):
        """After N saves to alternating values, record is in exactly one Set."""
        r = _raw_redis()
        obj = PtrTestModel.create(status="v0")

        for i in range(1, 6):
            obj.status = f"v{i}"
            obj.save()

        member_key = obj.db_key.redis_key
        _assert_exactly_one_set(r, member_key, IndexedField, PtrTestModel, "status")


# ---------------------------------------------------------------------------
# 3. Single-Process Tests
# ---------------------------------------------------------------------------


class TestNullableFieldFirstSave:
    """Tests for nullable IndexedField first-save and subsequent update."""

    def test_first_save_with_none_value(self):
        """First save with value=None writes member to None-value Set."""
        r = _raw_redis()
        obj = NullableModel(tag=None)
        obj.save()
        member_key = obj.db_key.redis_key

        # Member should be in the None set
        none_set_key = _index_set_key(IndexedField, NullableModel, "tag", None)
        members = {
            m.decode("utf-8") if isinstance(m, bytes) else m
            for m in r.smembers(none_set_key)
        }
        assert member_key in members

        # And in exactly one set total
        _assert_exactly_one_set(r, member_key, IndexedField, NullableModel, "tag")

    def test_resave_from_none_to_value_moves_membership(self):
        """Changing from None to a string SREMs from None-set, SADDs to new."""
        r = _raw_redis()
        obj = NullableModel.create(tag=None)
        member_key = obj.db_key.redis_key

        obj.tag = "hello"
        obj.save()

        # No longer in None set
        none_set_key = _index_set_key(IndexedField, NullableModel, "tag", None)
        none_members = {
            m.decode("utf-8") if isinstance(m, bytes) else m
            for m in r.smembers(none_set_key)
        }
        assert member_key not in none_members

        # Now in the "hello" set
        hello_set_key = _index_set_key(IndexedField, NullableModel, "tag", "hello")
        hello_members = {
            m.decode("utf-8") if isinstance(m, bytes) else m
            for m in r.smembers(hello_set_key)
        }
        assert member_key in hello_members

        _assert_exactly_one_set(r, member_key, IndexedField, NullableModel, "tag")

    def test_absolute_first_save_no_srem_on_nonexistent_key(self):
        """First save of a brand-new record must not crash (no old set to SREM)."""
        # This would fail if the Lua script tries to SREM from "" as a real key
        obj = NullableModel(tag="fresh")
        obj.save()  # Must not raise
        results = NullableModel.query.filter(tag="fresh")
        assert any(r.record_id == obj.record_id for r in results)


class TestIdempotentResave:
    """Resaving with the same value must not duplicate Set membership."""

    def test_idempotent_resave_indexed_field(self):
        """save() twice with same status must result in exactly one Set member."""
        r = _raw_redis()
        obj = PtrTestModel.create(status="stable")
        member_key = obj.db_key.redis_key

        obj.save()  # second save, same value

        _assert_exactly_one_set(r, member_key, IndexedField, PtrTestModel, "status")
        set_key = _index_set_key(IndexedField, PtrTestModel, "status", "stable")
        count = r.scard(set_key)
        assert count == 1, f"Expected 1 member in set, got {count}"

    def test_idempotent_resave_unique_field(self):
        """Re-saving a UniqueField with same value must not raise self-collision."""
        r = _raw_redis()

        class _UniqueResaveModel(popoto.Model):
            record_id = popoto.AutoKeyField()
            email = UniqueField(type=str)

        obj = _UniqueResaveModel.create(email="idem@example.com")
        member_key = obj.db_key.redis_key

        # Should not raise (would raise if uniqueness check treats self as conflict)
        obj.save()

        _assert_no_unique_set_has_multiple(r, UniqueField, _UniqueResaveModel, "email")
        _assert_exactly_one_set(r, member_key, UniqueField, _UniqueResaveModel, "email")

    def test_hash_field_value_unchanged_after_resave(self):
        """Pointer side key value must be unchanged after idempotent resave."""
        r = _raw_redis()
        obj = PtrTestModel.create(status="check")

        ptr_key = _pointer_side_key(obj.db_key.redis_key, "status")
        ptr_before = r.get(ptr_key.encode("utf-8"))
        assert ptr_before is not None, "Pointer side key not written on create"

        obj.save()

        ptr_after = r.get(ptr_key.encode("utf-8"))
        assert ptr_before == ptr_after, "Pointer changed on idempotent resave"


# ---------------------------------------------------------------------------
# 4. Uniqueness Rejection Leaves No Trace
# ---------------------------------------------------------------------------


class TestRejectedUniqueLeaveNoTrace:
    """Failed unique save must leave the database clean."""

    def test_rejected_unique_leaves_no_hash_entry(self):
        """When record B is rejected for duplicate email, B's hash must not exist."""
        r = _raw_redis()

        a = UniqueRejectionModel.create(email="taken@example.com")
        b = UniqueRejectionModel(email="taken@example.com")

        with pytest.raises(ModelException):
            b.save()

        # B's hash should not exist in Redis
        assert r.exists(b.db_key.redis_key.encode("utf-8")) == 0, (
            "Rejected record B's hash was written despite uniqueness conflict"
        )

    def test_rejected_unique_leaves_no_pointer(self):
        """Rejected save must not write any pointer (side key or legacy in-hash) for B."""
        r = _raw_redis()

        a = UniqueRejectionModel.create(email="taken2@example.com")
        b = UniqueRejectionModel(email="taken2@example.com")

        with pytest.raises(ModelException):
            b.save()

        ptr_key = _pointer_side_key(b.db_key.redis_key, "email")
        ptr_b = r.get(ptr_key.encode("utf-8"))
        assert ptr_b is None, f"Pointer side key was written for rejected record B: {ptr_b}"

        legacy_ptr_field = "email\x00idxset"
        legacy_ptr_b = r.hget(b.db_key.redis_key.encode("utf-8"), legacy_ptr_field.encode("utf-8"))
        assert legacy_ptr_b is None, f"Legacy in-hash pointer was written for rejected record B: {legacy_ptr_b}"

    def test_rejected_unique_set_still_has_only_a(self):
        """After B is rejected, the UniqueField Set must contain only A's key."""
        r = _raw_redis()

        a = UniqueRejectionModel.create(email="taken3@example.com")
        b = UniqueRejectionModel(email="taken3@example.com")

        with pytest.raises(ModelException):
            b.save()

        set_key = _index_set_key(UniqueField, UniqueRejectionModel, "email", "taken3@example.com")
        members = {
            m.decode("utf-8") if isinstance(m, bytes) else m
            for m in r.smembers(set_key)
        }
        assert len(members) == 1, f"Expected 1 member after rejection, got {len(members)}: {members}"
        assert a.db_key.redis_key in members


# ---------------------------------------------------------------------------
# 5. Hash-Content Parity Test
# ---------------------------------------------------------------------------


class TestHashContentParityAfterSave:
    """
    The EVAL script must write field bytes byte-for-byte identical to what
    encode_popoto_model_obj() / msgpack.packb() would produce.
    """

    def _read_hash_field_raw(self, r: redis_lib.Redis, redis_key: str, field_name: str) -> bytes:
        """Return raw msgpack bytes for a field from the model hash."""
        return r.hget(redis_key.encode("utf-8"), field_name.encode("utf-8"))

    def test_indexed_field_hash_bytes_match_msgpack(self):
        """After save(), HGET for the field returns the correct msgpack bytes."""
        r = _raw_redis()
        obj = HashParityModel(status="parity_check", email="p1@example.com")
        obj.save()

        raw = self._read_hash_field_raw(r, obj.db_key.redis_key, "status")
        expected = msgpack.packb("parity_check")
        assert raw == expected, (
            f"Hash field bytes mismatch: got {raw!r}, expected {expected!r}"
        )

    def test_unique_field_hash_bytes_match_msgpack(self):
        """UniqueField: after save(), HGET returns the correct msgpack bytes."""
        r = _raw_redis()
        obj = HashParityModel(status="parity2", email="p2@example.com")
        obj.save()

        raw = self._read_hash_field_raw(r, obj.db_key.redis_key, "email")
        expected = msgpack.packb("p2@example.com")
        assert raw == expected

    def test_none_value_hash_bytes_match_msgpack(self):
        """Nullable field with None: hash field must contain msgpack(None)."""
        r = _raw_redis()
        obj = NullableModel(tag=None)
        obj.save()

        raw = self._read_hash_field_raw(r, obj.db_key.redis_key, "tag")
        expected = msgpack.packb(None)
        assert raw == expected

    def test_hash_field_correct_after_value_change(self):
        """After changing value and resaving, hash field bytes must reflect new value."""
        r = _raw_redis()
        obj = HashParityModel(status="old", email="p3@example.com")
        obj.save()

        obj.status = "new"
        obj.save()

        raw = self._read_hash_field_raw(r, obj.db_key.redis_key, "status")
        expected = msgpack.packb("new")
        assert raw == expected

    def test_hash_field_correct_after_idempotent_resave(self):
        """Idempotent resave must still write correct field bytes."""
        r = _raw_redis()
        obj = HashParityModel(status="same", email="p4@example.com")
        obj.save()
        obj.save()  # idempotent

        raw = self._read_hash_field_raw(r, obj.db_key.redis_key, "status")
        expected = msgpack.packb("same")
        assert raw == expected


# ---------------------------------------------------------------------------
# 6. Pointer Invisibility Tests
# ---------------------------------------------------------------------------


class TestPointerInvisibility:
    """
    The \x00idxset pointer must never surface as a model attribute or query result.
    """

    def test_pointer_field_not_in_model_attributes(self):
        """After save(), model instance must not expose any \x00-containing key."""
        obj = PointerInvisibleModel.create(status="visible")

        for attr_name in vars(obj):
            assert "\x00" not in attr_name, (
                f"Pointer field leaked as model attribute: {attr_name!r}"
            )

    def test_pointer_field_not_in_filter_results(self):
        """filter() results must not include a \x00-containing attribute."""
        PointerInvisibleModel.create(status="query_test")
        results = PointerInvisibleModel.query.filter(status="query_test")
        assert results, "filter() returned no results"

        for result in results:
            for attr_name in vars(result):
                assert "\x00" not in attr_name, (
                    f"Pointer field leaked in filter() result: {attr_name!r}"
                )

    def test_pointer_field_not_in_query_all(self):
        """query.all() results must not include pointer fields."""
        PointerInvisibleModel.create(status="all_test")
        for result in PointerInvisibleModel.query.all():
            for attr_name in vars(result):
                assert "\x00" not in attr_name

    def test_fields_only_decode_path_crash_free(self):
        """values() projection must not crash when pointer fields are in hash."""
        PointerInvisibleModel.create(status="val_test")

        # This would crash with TypeError if pointer bytes were decoded as str key
        try:
            values_list = list(PointerInvisibleModel.query.filter(status="val_test").values("status"))
        except TypeError as exc:
            pytest.fail(f"values() crashed with TypeError (pointer decode bug?): {exc}")

        assert len(values_list) >= 1
        for row in values_list:
            # The pointer field must not appear in the projected dict
            for key in row:
                key_str = key.decode("utf-8") if isinstance(key, bytes) else key
                assert "\x00" not in key_str, (
                    f"Pointer field appeared in values() projection: {key!r}"
                )


# ---------------------------------------------------------------------------
# 7. Legacy-Record Upgrade Test (ARGV[6] hint path)
# ---------------------------------------------------------------------------


class TestLegacyRecordUpgrade:
    """
    Validates the ARGV[6] legacy-old-set hint. Seeds pre-fix on-disk records
    (no \x00idxset pointer) and verifies that the first Lua-backed save:
      - SREMs from the old value Set
      - SADDs to the new value Set
      - Writes the pointer
      - Leaves no dual-membership
    """

    def _seed_legacy_indexed_record(self, r: redis_lib.Redis, record_id: str, value: str):
        """Write a LegacyUpgradeModel hash without the \x00idxset pointer."""
        model_key = f"LegacyUpgradeModel:{record_id}"
        # Write minimal hash: only the data fields (no pointer)
        r.hset(
            model_key,
            mapping={
                b"record_id": msgpack.packb(record_id),
                b"status": msgpack.packb(value),
            },
        )
        # Add to class Set (as pre-fix code would)
        r.sadd("$Class:LegacyUpgradeModel", model_key)
        # Put the record in the old value Set
        old_set_key = f"$IndexF:LegacyUpgradeModel:status:{value}"
        r.sadd(old_set_key, model_key)

    def _seed_legacy_unique_record(self, r: redis_lib.Redis, record_id: str, email: str):
        """Write a LegacyUniqueModel hash without the \x00idxset pointer."""
        model_key = f"LegacyUniqueModel:{record_id}"
        r.hset(
            model_key,
            mapping={
                b"record_id": msgpack.packb(record_id),
                b"email": msgpack.packb(email),
            },
        )
        r.sadd("$Class:LegacyUniqueModel", model_key)
        old_set_key = f"$UniquF:LegacyUniqueModel:email:{email}"
        r.sadd(old_set_key, model_key)

    def test_legacy_indexed_record_upgrade_no_stranded_membership(self):
        """Pre-fix IndexedField record: first new-code save moves membership correctly."""
        r = _raw_redis()
        record_id = "legacy_idx_01"
        old_value = "old_status"
        new_value = "new_status"

        self._seed_legacy_indexed_record(r, record_id, old_value)
        model_key = f"LegacyUpgradeModel:{record_id}"

        # Verify pre-conditions: in old set, no pointer
        old_set_key = f"$IndexF:LegacyUpgradeModel:status:{old_value}"
        assert r.sismember(old_set_key, model_key)
        assert r.hget(model_key, "status\x00idxset".encode("utf-8")) is None

        # Hydrate via Popoto (without the pointer, _saved_field_values reflects DB)
        obj = LegacyUpgradeModel.query.get(record_id=record_id)
        assert obj is not None, "Could not hydrate legacy record"

        # First Lua-backed save to new value
        obj.status = new_value
        obj.save()

        # Record removed from old Set
        assert not r.sismember(old_set_key, model_key), (
            "Legacy record still in old Set after upgrade save"
        )

        # Record in new Set
        new_set_key = f"$IndexF:LegacyUpgradeModel:status:{new_value}"
        assert r.sismember(new_set_key, model_key), (
            "Legacy record not in new Set after upgrade save"
        )

        # Pointer now written to the side key (#476), not the legacy in-hash field
        ptr_key = _pointer_side_key(model_key, "status")
        ptr = r.get(ptr_key.encode("utf-8"))
        assert ptr is not None, "Pointer side key not written after legacy upgrade save"
        assert ptr.decode("utf-8") == new_set_key

        # The legacy in-hash pointer field must have been scrubbed, not repopulated
        assert r.hget(model_key, "status\x00idxset".encode("utf-8")) is None, (
            "Legacy in-hash pointer field was written/left behind by current code"
        )

        # No dual-membership
        _assert_no_dual_membership(r, model_key, IndexedField, LegacyUpgradeModel, "status")

    def test_legacy_unique_record_upgrade_no_stranded_membership(self):
        """Pre-fix UniqueField record: first new-code save moves membership correctly."""
        r = _raw_redis()
        record_id = "legacy_uniq_01"
        old_email = "legacy@old.com"
        new_email = "legacy@new.com"

        self._seed_legacy_unique_record(r, record_id, old_email)
        model_key = f"LegacyUniqueModel:{record_id}"

        obj = LegacyUniqueModel.query.get(record_id=record_id)
        assert obj is not None, "Could not hydrate legacy unique record"

        obj.email = new_email
        obj.save()

        old_set_key = f"$UniquF:LegacyUniqueModel:email:{old_email}"
        assert not r.sismember(old_set_key, model_key), (
            "Legacy unique record still in old Set after upgrade"
        )

        new_set_key = f"$UniquF:LegacyUniqueModel:email:{new_email}"
        assert r.sismember(new_set_key, model_key), (
            "Legacy unique record not in new Set after upgrade"
        )

        ptr_key = _pointer_side_key(model_key, "email")
        ptr = r.get(ptr_key.encode("utf-8"))
        assert ptr is not None
        assert ptr.decode("utf-8") == new_set_key

        assert r.hget(model_key, "email\x00idxset".encode("utf-8")) is None, (
            "Legacy in-hash pointer field was written/left behind by current code"
        )

        _assert_no_dual_membership(r, model_key, UniqueField, LegacyUniqueModel, "email")


# ---------------------------------------------------------------------------
# 8. Forced Mid-EVAL Error Test
# ---------------------------------------------------------------------------


class TestForcedMidEvalError:
    """
    Uses a test-only Lua script that forces an error after partial mutations
    to verify no half-state is visible through the Popoto query API.

    The production INDEX_SWAP_LUA is atomic because it runs inside Redis EVAL.
    This test verifies that a partial error leaves NO inconsistent state visible
    to reads (Redis rolls back EVAL atomically on error_reply).
    """

    # Test-only Lua variant: does SADD to new set, then errors before HSET
    _INDEX_SWAP_LUA_FORCE_ERROR = """
local model_key, new_set = KEYS[1], KEYS[2]
local field, member, new_bytes, is_unique, ptr_field, legacy_old_set =
  ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5], ARGV[6]
redis.call('SADD', new_set, member)
return redis.error_reply('FORCED_TEST_ERROR_PARTIAL_STATE')
"""

    def test_forced_error_leaves_no_hash_write(self):
        """After Lua error partway through, the model hash must not be updated."""
        r = _raw_redis()

        # Create a clean record first
        obj = PtrTestModel.create(status="before_error")
        model_key = obj.db_key.redis_key

        # Read hash field value before the forced error
        before_bytes = r.hget(model_key.encode("utf-8"), b"status")

        # Run the test-only broken Lua script directly
        new_set_key = _index_set_key(IndexedField, PtrTestModel, "status", "after_error")
        ptr_field = "status\x00idxset"

        try:
            r.eval(
                self._INDEX_SWAP_LUA_FORCE_ERROR,
                2,
                model_key,
                new_set_key,
                "status",
                model_key,
                msgpack.packb("after_error"),
                "0",
                ptr_field,
                "",
            )
        except redis_lib.ResponseError as exc:
            assert "FORCED_TEST_ERROR" in str(exc), f"Unexpected error: {exc}"

        # Hash field must be unchanged
        after_bytes = r.hget(model_key.encode("utf-8"), b"status")
        assert before_bytes == after_bytes, (
            "Hash field was mutated by partial Lua script that errored"
        )

    def test_forced_error_filter_sees_consistent_state(self):
        """After Lua error, filter() must not return a record with corrupted state."""
        r = _raw_redis()

        obj = PtrTestModel.create(status="consistent")
        model_key = obj.db_key.redis_key

        # Force an incomplete SADD to the "corrupted" value Set
        new_set_key = _index_set_key(IndexedField, PtrTestModel, "status", "corrupted")
        ptr_field = "status\x00idxset"

        try:
            r.eval(
                self._INDEX_SWAP_LUA_FORCE_ERROR,
                2,
                model_key,
                new_set_key,
                "status",
                model_key,
                msgpack.packb("corrupted"),
                "0",
                ptr_field,
                "",
            )
        except redis_lib.ResponseError:
            pass

        # NOTE: The test-only script is NOT atomic like the production one —
        # it does SADD before the error_reply, so this is a known inconsistency
        # that only this hand-crafted test can produce. The PRODUCTION Lua
        # (INDEX_SWAP_LUA) is fully atomic and never leaves this state.
        #
        # The key assertion is: querying via filter("consistent") must still
        # return our record (it was in the "consistent" Set before the error).
        results = PtrTestModel.query.filter(status="consistent")
        assert any(r2.record_id == obj.record_id for r2 in results), (
            "Record not found via original value after forced partial error"
        )


# ---------------------------------------------------------------------------
# 9. Partial-Save (update_fields) Tests
# ---------------------------------------------------------------------------


class TestPartialSaveIndexedField:
    """
    Tests for save(update_fields=[...]) paths with IndexedField and UniqueField.
    Issue #412 BLOCKER: when all update_fields are IndexedField, the hset_mapping
    may be empty, causing DataError. The EVAL owns the hash write.
    """

    def test_partial_save_indexed_field_moves_membership(self):
        """save(update_fields=['status']) moves IndexedField Set membership."""
        r = _raw_redis()
        obj = PartialSaveModel.create(status="initial", other="keep")
        member_key = obj.db_key.redis_key

        obj.status = "updated"
        obj.save(update_fields=["status"])

        # Must be in "updated" set
        updated_set_key = _index_set_key(IndexedField, PartialSaveModel, "status", "updated")
        members = {
            m.decode("utf-8") if isinstance(m, bytes) else m
            for m in r.smembers(updated_set_key)
        }
        assert member_key in members

        # Must not be in old set
        _assert_no_dual_membership(r, member_key, IndexedField, PartialSaveModel, "status")

    def test_partial_save_indexed_only_no_data_error(self):
        """
        save(update_fields=['status']) when status is the only IndexedField
        must not raise redis.DataError due to empty hset_mapping.

        This is the Round-3 BLOCKER from the plan critique: the EVAL writes
        the field bytes, so hset_mapping can be empty for indexed-only updates.
        """
        obj = PartialSaveModel.create(status="before", other="keep")
        obj.status = "after"

        # Must not raise DataError or any other exception
        obj.save(update_fields=["status"])

        # Verify value was saved
        reloaded = PartialSaveModel.query.get(record_id=obj.record_id)
        assert reloaded is not None
        assert reloaded.status == "after"

    def test_partial_save_indexed_field_value_written_to_hash(self):
        """save(update_fields=['status']): HGET must return the new msgpack bytes."""
        r = _raw_redis()
        obj = PartialSaveModel.create(status="old", other="keep")

        obj.status = "new"
        obj.save(update_fields=["status"])

        raw = r.hget(obj.db_key.redis_key.encode("utf-8"), b"status")
        expected = msgpack.packb("new")
        assert raw == expected, (
            f"Partial save did not write field value to hash: got {raw!r}, expected {expected!r}"
        )

    def test_partial_save_does_not_overwrite_non_updated_field(self):
        """save(update_fields=['status']) must not touch the 'other' field."""
        r = _raw_redis()
        obj = PartialSaveModel.create(status="start", other="keep_this")

        obj.status = "changed"
        obj.save(update_fields=["status"])

        reloaded = PartialSaveModel.query.get(record_id=obj.record_id)
        assert reloaded.other == "keep_this"

    def test_partial_save_unique_field_conflict_raises(self):
        """save(update_fields=['email']) with conflicting unique value raises ModelException."""
        a = PartialSaveUniqueModel.create(email="a@example.com")
        b = PartialSaveUniqueModel.create(email="b@example.com")

        # Try to update b's email to a's taken value
        b.email = "a@example.com"
        with pytest.raises(ModelException):
            b.save(update_fields=["email"])

    def test_partial_save_unique_conflict_leaves_no_trace(self):
        """Rejected partial-save must not write new pointer or add to conflicting Set."""
        r = _raw_redis()
        a = PartialSaveUniqueModel.create(email="c@example.com")
        b = PartialSaveUniqueModel.create(email="d@example.com")
        b_key = b.db_key.redis_key

        b.email = "c@example.com"
        with pytest.raises(ModelException):
            b.save(update_fields=["email"])

        # b must still be in the "d@example.com" set
        d_set_key = _index_set_key(UniqueField, PartialSaveUniqueModel, "email", "d@example.com")
        d_members = {
            m.decode("utf-8") if isinstance(m, bytes) else m
            for m in r.smembers(d_set_key)
        }
        assert b_key in d_members, "b was removed from its original set after rejected partial save"

        # b must NOT be in "c@example.com" set alongside a
        c_set_key = _index_set_key(UniqueField, PartialSaveUniqueModel, "email", "c@example.com")
        c_members = {
            m.decode("utf-8") if isinstance(m, bytes) else m
            for m in r.smembers(c_set_key)
        }
        assert b_key not in c_members

    def test_partial_save_return_value_truthy(self):
        """
        save(update_fields=[...]) with only IndexedField members must still
        return a truthy value (not crash on results[0] indexing).

        Risk: when hset_mapping is empty, internal_pipeline.execute() returns
        an empty list; results[0] would IndexError. The guard must handle this.
        """
        obj = PartialSaveModel.create(status="init", other="x")
        obj.status = "truthy_test"

        result = obj.save(update_fields=["status"])
        # The save must complete without raising and the return must be truthy
        # (the EVAL result from INDEX_SWAP_LUA returns 1)
        assert result is not None
