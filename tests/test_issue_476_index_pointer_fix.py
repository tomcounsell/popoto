"""
Regression tests for issue #476.

#476 identified three problems in the 1.8.0 atomic secondary-index feature
(#412 / PR #424):

  1. Forward-incompatible hash schema: INDEX_SWAP_LUA wrote a raw
     ``{field}\\x00idxset`` pointer field directly INTO the model hash.
     Pre-1.8.0 decoders (which msgpack.unpackb() every hash field with no
     \\x00 skip) crash with msgpack.exceptions.ExtraData on any record with
     an indexed/unique field written by 1.8.0.
  2. delete() read the index pointer via HGET AFTER the model hash was
     already DELETEd (base.py), so the read always returned nil and fell
     back to a possibly-stale ``_saved_field_values`` snapshot, risking an
     orphaned index Set member pointing at a deleted hash.
  3. The internal (no-external-pipeline) save path queued the uniqueness
     EVAL into the *same* Redis MULTI/EXEC transaction as the base HSET
     and other bookkeeping. Redis does not roll back other queued commands
     when one command in a transaction errors, so a genuine uniqueness
     conflict could leave the base HSET committed while the EVAL (and thus
     the index write) failed — an orphaned, index-less "ghost" hash.

The fix moves the pointer to a standalone side key (never a hash field),
reorders Model.delete() to run field.on_delete() hooks before the hash is
physically removed, and runs indexed/unique field on_save() eagerly (its
own atomic EVAL) before the rest of a record's internal-pipeline writes are
even queued.
"""

import msgpack
import pytest
import redis as redis_lib

import popoto
from popoto import ModelException
from popoto.fields.indexed_field_mixin import IndexedFieldMixin
from popoto.fields.shortcuts import IndexedField, UniqueField
from popoto.redis_db import POPOTO_REDIS_DB


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


def _pointer_side_key(member_redis_key: str, field_name: str) -> str:
    return f"{member_redis_key}\x00idxptr\x00{field_name}"


def _pre_1_8_0_decode(raw_hash: dict) -> dict:
    """Reproduce the v1.7.1 decode_popoto_model_hashmap() unpack loop.

    Unconditionally msgpack.unpackb()'s every hash field with no \\x00-name
    skip — this is exactly what a reader running the pre-1.8.0 decoder does
    (see ``git show v1.7.1:src/popoto/models/encoding.py``).
    """
    return {
        key_b.decode("utf-8"): msgpack.unpackb(value_b, strict_map_key=False)
        for key_b, value_b in raw_hash.items()
    }


class ForwardCompatIndexedModel(popoto.Model):
    record_id = popoto.AutoKeyField()
    status = IndexedField(type=str)
    email = UniqueField(type=str)


class DeleteOrderModel(popoto.Model):
    record_id = popoto.AutoKeyField()
    status = IndexedField(type=str)


class UniqueConflictOrphanModel(popoto.Model):
    """Has a non-indexed field alongside the UniqueField — the scenario
    where a pre-#476 uniqueness conflict could leave an orphaned,
    partially-written hash (base HSET commits, EVAL for the unique field
    does not)."""

    record_id = popoto.AutoKeyField()
    label = popoto.Field(type=str, default="unset")
    email = UniqueField(type=str)


class TestForwardCompatNoPointerFieldInHash:
    """#476 problem 1: the pointer must never be a field inside the model hash."""

    def test_pre_1_8_0_decoder_does_not_crash_on_indexed_record(self):
        """A pre-1.8.0-style decode of a current-code-written record must not
        raise msgpack.exceptions.ExtraData (the actual reported crash mechanism)."""
        r = _raw_redis()
        obj = ForwardCompatIndexedModel.create(
            status="active", email="fwd-compat@example.com"
        )

        raw_hash = r.hgetall(obj.db_key.redis_key.encode("utf-8"))
        decoded = _pre_1_8_0_decode(raw_hash)

        assert decoded["status"] == "active"
        assert decoded["email"] == "fwd-compat@example.com"
        assert decoded["record_id"] == obj.record_id

    def test_no_x00_field_written_to_hash_for_indexed_or_unique(self):
        """Neither field's HSET write may ever introduce a \\x00-named hash field."""
        r = _raw_redis()
        obj = ForwardCompatIndexedModel.create(
            status="active2", email="fwd-compat2@example.com"
        )
        raw_hash = r.hgetall(obj.db_key.redis_key.encode("utf-8"))
        for key_b in raw_hash:
            assert (
                b"\x00" not in key_b
            ), f"Pointer field leaked into model hash: {key_b!r}"

    def test_pointer_lives_in_side_key_not_hash(self):
        r = _raw_redis()
        obj = ForwardCompatIndexedModel.create(
            status="active3", email="fwd-compat3@example.com"
        )
        ptr_key = _pointer_side_key(obj.db_key.redis_key, "status")
        assert (
            r.get(ptr_key.encode("utf-8")) is not None
        ), "Pointer side key was not written"
        assert r.hget(obj.db_key.redis_key.encode("utf-8"), b"status\x00idxset") is None


class TestDeleteReadsIndexPointerBeforeHashRemoval:
    """#476 problem 2: delete() must read the index pointer while the model
    hash still exists, not after it has already been removed."""

    def test_delete_removes_correct_set_member_with_stale_in_memory_snapshot(self):
        """Simulates a stale ``_saved_field_values`` snapshot: object A is
        hydrated, then a DIFFERENT writer changes the record's indexed value
        (so the true DB/index state has moved on), then A.delete() is called
        using the now-stale handle. Correct behavior: the SET member removed
        must match the record's ACTUAL current index membership (read live
        from the pointer), not A's stale in-memory value.
        """
        r = _raw_redis()
        created = DeleteOrderModel.create(status="v1")
        member_key = created.db_key.redis_key

        # A separate hydration of the same record (simulates a second reader/writer)
        stale_handle = DeleteOrderModel.query.get(record_id=created.record_id)
        assert stale_handle._saved_field_values["status"] == "v1"

        # A different writer moves the record to "v2"
        writer_handle = DeleteOrderModel.query.get(record_id=created.record_id)
        writer_handle.status = "v2"
        writer_handle.save()

        v1_set_key = f"$IndexF:DeleteOrderModel:status:v1"
        v2_set_key = f"$IndexF:DeleteOrderModel:status:v2"
        assert not r.sismember(v1_set_key, member_key)
        assert r.sismember(v2_set_key, member_key)

        # Now delete using the STALE handle (its _saved_field_values still says "v1")
        result = stale_handle.delete()
        assert result is True

        # The record must be gone from the Set it was ACTUALLY in ("v2"),
        # not left as an orphan pointing at a deleted hash.
        assert not r.sismember(v2_set_key, member_key), (
            "Orphaned index member: stale delete() removed from the wrong "
            "(stale in-memory) Set instead of the record's true current Set"
        )
        # And must not have spuriously been removed from a set it was never
        # actually recorded against beyond what's expected.
        assert not r.sismember(v1_set_key, member_key)

    def test_delete_cleans_up_pointer_side_key(self):
        """The pointer side key itself must not be left behind after delete()."""
        r = _raw_redis()
        obj = DeleteOrderModel.create(status="cleanup_check")
        ptr_key = _pointer_side_key(obj.db_key.redis_key, "status")
        assert r.get(ptr_key.encode("utf-8")) is not None

        obj.delete()

        assert (
            r.get(ptr_key.encode("utf-8")) is None
        ), "Pointer side key was not cleaned up on delete()"


class TestUniqueConflictLeavesNoOrphanHash:
    """#476 problem 3: a uniqueness conflict on the internal (no external
    pipeline) save path must not leave any trace — not even a partially
    written hash with non-indexed fields set but no index entry."""

    def test_conflicting_create_leaves_absolutely_no_hash(self):
        a = UniqueConflictOrphanModel.create(label="A", email="orphan@example.com")
        b = UniqueConflictOrphanModel(label="B", email="orphan@example.com")

        with pytest.raises(ModelException):
            b.save()

        r = _raw_redis()
        # No hash at all -- not even the non-indexed 'label' field -- may exist.
        assert r.exists(b.db_key.redis_key.encode("utf-8")) == 0, (
            "Orphaned hash: non-indexed fields were committed despite the "
            "uniqueness conflict on the EVAL-owned field"
        )
        assert (
            r.sismember(
                "$Class:UniqueConflictOrphanModel", b.db_key.redis_key.encode("utf-8")
            )
            == 0
        ), "Rejected record's key leaked into the class set"

        # The unique index Set must contain only A.
        set_key = "$UniquF:UniqueConflictOrphanModel:email:orphan@example.com"
        members = {
            m.decode("utf-8") if isinstance(m, bytes) else m
            for m in r.smembers(set_key)
        }
        assert members == {a.db_key.redis_key}

    def test_conflicting_update_leaves_original_record_intact(self):
        a = UniqueConflictOrphanModel.create(label="A2", email="orphan2a@example.com")
        b = UniqueConflictOrphanModel.create(label="B2", email="orphan2b@example.com")

        b.label = "B2-attempted-rename"
        b.email = "orphan2a@example.com"  # already taken by a
        with pytest.raises(ModelException):
            b.save()

        r = _raw_redis()
        # b's hash must be unaffected: label must NOT have been updated to the
        # attempted (never-committed) value, since that would mean the base
        # HSET for non-indexed fields committed alongside a failed EVAL.
        label_raw = r.hget(b.db_key.redis_key.encode("utf-8"), b"label")
        assert msgpack.unpackb(label_raw) == "B2", (
            "Non-indexed field was updated despite the uniqueness conflict "
            "on the EVAL-owned field — partial-commit orphan state"
        )
