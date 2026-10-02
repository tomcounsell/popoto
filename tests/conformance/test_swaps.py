"""Swap-family conformance: G's ``swap_index`` / ``drop_index_entry`` /
``swap_tags`` / ``drop_tag_entries`` (#631 WS3c).

Every test runs on every configured backend with the *same* assertions:
``RedisBackend`` is the oracle (its bodies run ``INDEX_SWAP_LUA`` and
``TAG_SWAP_LUA`` verbatim, moved in WS0), so whatever it returns, raises or
leaves behind is what ``PostgresBackend`` must return, raise and leave behind.
Observation goes through the protocol only -- ``index_members`` for the index
side, ``load_record`` for the hash side -- so the pointer, which is a side key
on Redis and a table on Postgres, is observed through its *effect* (which
index the next swap or drop leaves) rather than its shape.

Three things are leg-aware and pinned rather than hidden:

* the pre-#540 pointer side key is a Redis key-layout migration artefact with
  no Postgres shape, so its adoption is asserted on the Redis leg only;
* a queued swap that conflicts surfaces at ``commit()`` as the server's raw
  ``ResponseError`` on Redis (the pipeline relays the Lua's error reply) and
  as the protocol's ``ModelException`` on Postgres;
* a failing operation in a unit of work rolls back the whole queue on
  Postgres and commits the rest on Redis (documented in
  ``docs/features/postgres-backend.md``).

Never touches database 0 or schema ``public``: the Redis leg goes through the
plugin-bound client and the Postgres leg through the harness's own schema.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterator, Sequence

import msgpack
import pytest

from popoto.backends import postgres as postgres_module
from popoto.backends.postgres import PostgresBackend
from popoto.backends.redis import RedisBackend
from popoto.exceptions import ModelException

pytestmark = pytest.mark.conformance

PREFIX = "popoto_test:631:swaps"
CLASS_SET = f"$Class:{PREFIX}:Record"
MESSAGE = "Uniqueness violation on {key}.{field}: the value indexed at {idx!r} is already taken by another instance"


def key(suffix: Any) -> str:
    return f"{PREFIX}:Record:{suffix}"


def idx(value: Any, field: str = "f") -> str:
    """The set index for ``field == value`` (``DB_key(...).redis_key`` shape)."""
    return f"{PREFIX}:_{field}:{value}"


def packed(value: Any) -> bytes:
    return msgpack.packb(value)


def swap(backend: Any, k: str, value: Any, *, unique: bool = True, **kw: Any) -> Any:
    """``swap_index`` of field ``f`` to ``value`` with the packed bytes the
    field layer would send."""
    return backend.swap_index(k, "f", idx(value), packed(value), unique=unique, **kw)


def members(backend: Any, *values: Any, field: str = "f") -> list[set[str]]:
    return [backend.index_members(idx(v, field)) for v in values]


# -- swap_index -----------------------------------------------------------------


class TestSwapIndex:
    def test_new_value_joins_the_index_and_writes_the_field(self, backend):
        assert swap(backend, key(1), "a") == 1
        assert backend.index_members(idx("a")) == {key(1)}
        # The field bytes land in the record, byte for byte, and nothing else
        # does: the pointer is never a field of the record (#476).
        assert backend.load_record(key(1)) == {b"f": packed("a")}

    def test_changed_value_moves_the_member(self, backend):
        swap(backend, key(1), "a")
        assert swap(backend, key(1), "b") == 1
        assert members(backend, "a", "b") == [set(), {key(1)}]
        assert backend.load_record(key(1)) == {b"f": packed("b")}

    def test_unchanged_value_is_an_idempotent_rewrite(self, backend):
        swap(backend, key(1), "a")
        assert swap(backend, key(1), "a") == 1
        assert swap(backend, key(1), "a") == 1
        assert backend.index_members(idx("a")) == {key(1)}
        assert backend.load_record(key(1)) == {b"f": packed("a")}

    def test_rewrites_the_bytes_even_when_the_index_is_unchanged(self, backend):
        # Same index, different bytes (the field layer's encoding changed,
        # or a non-indexed sibling field rode along): the hash write is
        # unconditional on the idempotent path.
        swap(backend, key(1), "a")
        assert backend.swap_index(key(1), "f", idx("a"), b"\x01\x02", unique=True) == 1
        assert backend.load_record(key(1)) == {b"f": b"\x01\x02"}
        assert backend.index_members(idx("a")) == {key(1)}

    def test_the_swap_creates_the_record_when_absent(self, backend):
        # HSET on a missing hash creates it; the class set is save_record's
        # business, not the swap's.
        assert backend.record_exists(key(1)) is False
        swap(backend, key(1), "a")
        assert backend.record_exists(key(1)) is True
        assert backend.count_records(CLASS_SET) == 0

    def test_other_fields_of_the_record_survive(self, backend):
        backend.save_record(key(1), {b"name": packed("n")}, class_set=CLASS_SET)
        swap(backend, key(1), "a")
        swap(backend, key(1), "b")
        assert backend.load_record(key(1)) == {b"name": packed("n"), b"f": packed("b")}

    def test_fields_have_independent_pointers(self, backend):
        backend.swap_index(key(1), "f", idx("a"), packed("a"), unique=False)
        backend.swap_index(key(1), "g", idx("x", "g"), packed("x"), unique=False)
        backend.swap_index(key(1), "f", idx("b"), packed("b"), unique=False)
        assert members(backend, "a", "b") == [set(), {key(1)}]
        assert backend.index_members(idx("x", "g")) == {key(1)}
        assert backend.load_record(key(1)) == {b"f": packed("b"), b"g": packed("x")}

    def test_non_unique_index_is_shared(self, backend):
        assert swap(backend, key(1), "a", unique=False) == 1
        assert swap(backend, key(2), "a", unique=False) == 1
        assert backend.index_members(idx("a")) == {key(1), key(2)}

    def test_two_records_moving_in_opposite_directions(self, backend):
        swap(backend, key(1), "a", unique=False)
        swap(backend, key(2), "b", unique=False)
        swap(backend, key(1), "b", unique=False)
        swap(backend, key(2), "a", unique=False)
        assert members(backend, "a", "b") == [{key(2)}, {key(1)}]


class TestUniqueConflict:
    def test_conflict_raises_the_oracle_message_and_writes_nothing(self, backend):
        swap(backend, key(1), "a")
        with pytest.raises(ModelException) as info:
            swap(backend, key(2), "a")
        assert str(info.value) == MESSAGE.format(key=key(2), field="f", idx=idx("a"))
        # Validation before mutation: the loser left no trace anywhere.
        assert backend.index_members(idx("a")) == {key(1)}
        assert backend.load_record(key(2)) is None
        assert backend.record_exists(key(2)) is False

    def test_conflict_on_a_move_leaves_the_loser_where_it_was(self, backend):
        swap(backend, key(1), "a")
        swap(backend, key(2), "b")
        with pytest.raises(ModelException):
            swap(backend, key(2), "a")
        assert members(backend, "a", "b") == [{key(1)}, {key(2)}]
        assert backend.load_record(key(2)) == {b"f": packed("b")}
        # The pointer still names b: a later move leaves b, not a.
        swap(backend, key(2), "c")
        assert members(backend, "a", "b", "c") == [{key(1)}, set(), {key(2)}]

    def test_self_is_never_a_conflict(self, backend):
        # A member planted without a pointer (an index rebuilt by hand, or a
        # record that predates pointers) is still "self", not "another".
        backend.index_add(idx("a"), key(1))
        assert swap(backend, key(1), "a") == 1
        assert backend.index_members(idx("a")) == {key(1)}

    def test_a_record_with_a_stale_pointer_cannot_claim_a_taken_value(self, backend):
        swap(backend, key(1), "a")
        swap(backend, key(2), "b")
        # Take ``a`` away from 1 by hand: 1's pointer is now stale.
        backend.index_remove(idx("a"), key(1))
        swap(backend, key(2), "a")  # free now
        with pytest.raises(ModelException):
            swap(backend, key(1), "a")
        assert members(backend, "a", "b") == [{key(2)}, set()]

    def test_non_unique_never_conflicts(self, backend):
        swap(backend, key(1), "a")
        assert swap(backend, key(2), "a", unique=False) == 1
        assert backend.index_members(idx("a")) == {key(1), key(2)}

    def test_unique_sees_a_crowded_index_as_a_conflict(self, backend):
        swap(backend, key(1), "a", unique=False)
        swap(backend, key(2), "a", unique=False)
        with pytest.raises(ModelException):
            swap(backend, key(3), "a")
        # An occupant re-saving the same value takes the idempotent path,
        # which the Lua checks *before* uniqueness: no raise, no change.
        assert swap(backend, key(1), "a") == 1
        assert backend.index_members(idx("a")) == {key(1), key(2)}
        # Moving an occupant to another taken value does conflict.
        swap(backend, key(3), "b")
        with pytest.raises(ModelException):
            swap(backend, key(1), "b")
        assert members(backend, "a", "b") == [{key(1), key(2)}, {key(3)}]

    def test_the_value_is_free_again_after_a_move(self, backend):
        swap(backend, key(1), "a")
        swap(backend, key(1), "b")
        assert swap(backend, key(2), "a") == 1
        assert members(backend, "a", "b") == [{key(2)}, {key(1)}]

    def test_the_value_is_free_again_after_a_drop(self, backend):
        swap(backend, key(1), "a")
        backend.drop_index_entry(key(1), "f", fallback_idx=idx("a"))
        assert swap(backend, key(2), "a") == 1


# -- Legacy records ----------------------------------------------------------------


class TestLegacyRecords:
    """Records that predate the server-authoritative pointer (#476 / #540)."""

    def test_legacy_old_idx_hint_leaves_the_old_index_when_no_pointer(self, backend):
        # The field layer's hint: the previously saved value's index, for a
        # record that was never swapped by pointer-aware code.
        backend.index_add(idx("old"), key(1))
        backend.save_record(key(1), {b"f": packed("old")}, class_set=CLASS_SET)
        assert (
            backend.swap_index(
                key(1),
                "f",
                idx("new"),
                packed("new"),
                unique=True,
                legacy_old_idx=idx("old"),
            )
            == 1
        )
        assert members(backend, "old", "new") == [set(), {key(1)}]
        assert backend.load_record(key(1)) == {b"f": packed("new")}

    def test_the_hint_is_ignored_once_a_pointer_exists(self, backend):
        swap(backend, key(1), "a")
        backend.index_add(idx("z"), key(1))  # unrelated membership
        backend.swap_index(
            key(1), "f", idx("b"), packed("b"), unique=True, legacy_old_idx=idx("z")
        )
        # The pointer (a) decided what to leave; the hint (z) did not.
        assert members(backend, "a", "b", "z") == [set(), {key(1)}, {key(1)}]

    def test_the_hint_naming_the_new_index_is_not_applied(self, backend):
        backend.index_add(idx("a"), key(1))
        backend.swap_index(
            key(1), "f", idx("a"), packed("a"), unique=True, legacy_old_idx=idx("a")
        )
        assert backend.index_members(idx("a")) == {key(1)}

    def test_pre_476_in_hash_pointer_is_adopted_and_scrubbed(self, backend):
        # A record imported with the pre-#476 ``{field}\x00idxset`` field:
        # the swap reads it as the pointer, leaves that index, and HDELs it.
        backend.index_add(idx("old"), key(1))
        backend.save_record(
            key(1),
            {b"f": packed("old"), b"f\x00idxset": idx("old").encode()},
            class_set=CLASS_SET,
        )
        assert swap(backend, key(1), "new") == 1
        assert members(backend, "old", "new") == [set(), {key(1)}]
        assert backend.load_record(key(1)) == {b"f": packed("new")}

    def test_pre_476_in_hash_pointer_is_scrubbed_on_an_idempotent_resave(self, backend):
        backend.index_add(idx("a"), key(1))
        backend.save_record(
            key(1),
            {b"f": packed("a"), b"f\x00idxset": idx("a").encode()},
            class_set=CLASS_SET,
        )
        assert swap(backend, key(1), "a") == 1
        assert backend.load_record(key(1)) == {b"f": packed("a")}
        assert backend.index_members(idx("a")) == {key(1)}

    def test_pre_476_in_hash_pointer_beats_the_hint(self, backend):
        backend.index_add(idx("ptr"), key(1))
        backend.index_add(idx("hint"), key(1))
        backend.save_record(
            key(1), {b"f\x00idxset": idx("ptr").encode()}, class_set=CLASS_SET
        )
        backend.swap_index(
            key(1),
            "f",
            idx("new"),
            packed("new"),
            unique=False,
            legacy_old_idx=idx("hint"),
        )
        assert members(backend, "ptr", "hint", "new") == [set(), {key(1)}, {key(1)}]

    def test_drop_honours_the_pre_476_in_hash_pointer(self, backend):
        backend.index_add(idx("ptr"), key(1))
        backend.index_add(idx("fallback"), key(1))
        backend.save_record(
            key(1), {b"f\x00idxset": idx("ptr").encode()}, class_set=CLASS_SET
        )
        assert backend.drop_index_entry(key(1), "f", fallback_idx=idx("fallback")) == 1
        assert members(backend, "ptr", "fallback") == [set(), {key(1)}]

    def test_pre_540_side_key_is_adopted_and_reclaimed(self, backend, backend_is_redis):
        """The 1.8.1/1.8.2 pointer side key (``{key}\\x00idxptr\\x00{field}``)
        collides with the model key glob (#540); the Lua adopts its value
        and DELs it. Redis key layout only: no Postgres record can carry
        one, so the Postgres leg asserts nothing here."""
        if not backend_is_redis:
            pytest.skip("Redis key-layout migration artefact; no Postgres shape")
        client = backend.client
        backend.index_add(idx("old"), key(1))
        client.set(f"{key(1)}\x00idxptr\x00f", idx("old"))
        assert swap(backend, key(1), "new") == 1
        assert members(backend, "old", "new") == [set(), {key(1)}]
        assert client.exists(f"{key(1)}\x00idxptr\x00f") == 0
        assert client.get(f"$IdxPtr:{key(1)}:f") == idx("new").encode()


# -- drop_index_entry -------------------------------------------------------------


class TestDropIndexEntry:
    def test_pointer_present_drops_from_the_pointed_index(self, backend):
        swap(backend, key(1), "a")
        # The fallback names a different index; the pointer wins.
        assert backend.drop_index_entry(key(1), "f", fallback_idx=idx("stale")) == 1
        assert backend.index_members(idx("a")) == set()

    def test_pointer_absent_falls_back_to_the_value_index(self, backend):
        backend.index_add(idx("a"), key(1))
        assert backend.drop_index_entry(key(1), "f", fallback_idx=idx("a")) == 1
        assert backend.index_members(idx("a")) == set()

    def test_nothing_to_drop_replies_zero(self, backend):
        assert backend.drop_index_entry(key(1), "f", fallback_idx=idx("a")) == 0
        swap(backend, key(1), "a")
        assert backend.drop_index_entry(key(1), "f", fallback_idx=idx("a")) == 1
        # Second drop: pointer gone, fallback index empty.
        assert backend.drop_index_entry(key(1), "f", fallback_idx=idx("a")) == 0

    def test_drop_clears_the_pointer(self, backend):
        swap(backend, key(1), "a")
        backend.drop_index_entry(key(1), "f", fallback_idx=idx("a"))
        # Re-plant the membership by hand: with the pointer gone, the next
        # drop can only find it through the fallback.
        backend.index_add(idx("a"), key(1))
        assert backend.drop_index_entry(key(1), "f", fallback_idx=idx("other")) == 0
        assert backend.index_members(idx("a")) == {key(1)}

    def test_drop_leaves_the_record_and_other_fields_alone(self, backend):
        swap(backend, key(1), "a")
        backend.swap_index(key(1), "g", idx("x", "g"), packed("x"), unique=False)
        backend.drop_index_entry(key(1), "f", fallback_idx=idx("a"))
        assert backend.load_record(key(1)) == {b"f": packed("a"), b"g": packed("x")}
        assert backend.index_members(idx("x", "g")) == {key(1)}

    def test_drop_of_a_shared_index_removes_only_this_member(self, backend):
        swap(backend, key(1), "a", unique=False)
        swap(backend, key(2), "a", unique=False)
        backend.drop_index_entry(key(1), "f", fallback_idx=idx("a"))
        assert backend.index_members(idx("a")) == {key(2)}


# -- swap_tags ----------------------------------------------------------------------


def tag_idxs(*tags: str) -> list[str]:
    return [idx(t, "t") for t in tags]


def swap_tags(backend: Any, k: str, *tags: str, **kw: Any) -> Any:
    return backend.swap_tags(k, "t", tag_idxs(*tags), packed(list(tags)), **kw)


def tag_members(backend: Any, *tags: str) -> list[set[str]]:
    return members(backend, *tags, field="t")


class TestSwapTags:
    def test_new_tags_join_every_index_and_write_the_field(self, backend):
        assert swap_tags(backend, key(1), "x", "y") == 1
        assert tag_members(backend, "x", "y", "z") == [{key(1)}, {key(1)}, set()]
        assert backend.load_record(key(1)) == {b"t": packed(["x", "y"])}

    def test_changed_tags_are_diffed(self, backend):
        swap_tags(backend, key(1), "x", "y")
        assert swap_tags(backend, key(1), "y", "z") == 1
        assert tag_members(backend, "x", "y", "z") == [set(), {key(1)}, {key(1)}]
        assert backend.load_record(key(1)) == {b"t": packed(["y", "z"])}

    def test_unchanged_tags_are_a_no_op_rewrite(self, backend):
        swap_tags(backend, key(1), "x", "y")
        assert swap_tags(backend, key(1), "x", "y") == 1
        assert tag_members(backend, "x", "y") == [{key(1)}, {key(1)}]

    def test_untagged_save_leaves_every_index(self, backend):
        swap_tags(backend, key(1), "x", "y")
        assert swap_tags(backend, key(1)) == 1
        assert tag_members(backend, "x", "y") == [set(), set()]
        assert backend.load_record(key(1)) == {b"t": packed([])}
        # The pointer is empty too: a later drop finds nothing by pointer.
        backend.index_add(idx("x", "t"), key(1))
        assert backend.drop_tag_entries(key(1), "t", fallback_idxs=[]) == 0
        assert backend.index_members(idx("x", "t")) == {key(1)}

    def test_duplicate_indexes_in_one_call_are_one_membership(self, backend):
        assert (
            backend.swap_tags(key(1), "t", tag_idxs("x", "x"), packed(["x", "x"])) == 1
        )
        assert backend.index_members(idx("x", "t")) == {key(1)}
        swap_tags(backend, key(1), "y")
        assert tag_members(backend, "x", "y") == [set(), {key(1)}]

    def test_tags_are_shared_between_records(self, backend):
        swap_tags(backend, key(1), "x", "y")
        swap_tags(backend, key(2), "y", "z")
        assert tag_members(backend, "x", "y", "z") == [
            {key(1)},
            {key(1), key(2)},
            {key(2)},
        ]
        swap_tags(backend, key(1), "z")
        assert tag_members(backend, "x", "y", "z") == [
            set(),
            {key(2)},
            {key(1), key(2)},
        ]

    def test_tag_and_index_pointers_on_one_record_are_independent(self, backend):
        swap(backend, key(1), "a")
        swap_tags(backend, key(1), "x")
        backend.drop_index_entry(key(1), "f", fallback_idx=idx("a"))
        assert backend.index_members(idx("x", "t")) == {key(1)}
        swap_tags(backend, key(1), "y")
        assert tag_members(backend, "x", "y") == [set(), {key(1)}]
        assert backend.load_record(key(1)) == {b"f": packed("a"), b"t": packed(["y"])}

    def test_a_membership_planted_outside_the_pointer_is_not_diffed_away(self, backend):
        # Only what the pointer names is left; a by-hand membership stays.
        swap_tags(backend, key(1), "x")
        backend.index_add(idx("hand", "t"), key(1))
        swap_tags(backend, key(1), "y")
        assert tag_members(backend, "x", "y", "hand") == [set(), {key(1)}, {key(1)}]

    def test_pre_540_tag_side_key_is_adopted_and_reclaimed(
        self, backend, backend_is_redis
    ):
        if not backend_is_redis:
            pytest.skip("Redis key-layout migration artefact; no Postgres shape")
        client = backend.client
        backend.index_add(idx("old", "t"), key(1))
        client.sadd(f"{key(1)}\x00tagptr\x00t", idx("old", "t"))
        assert swap_tags(backend, key(1), "new") == 1
        assert tag_members(backend, "old", "new") == [set(), {key(1)}]
        assert client.exists(f"{key(1)}\x00tagptr\x00t") == 0
        assert client.smembers(f"$TagPtr:{key(1)}:t") == {idx("new", "t").encode()}


class _RaisingFallback(Sequence[str]):
    """A ``fallback_idxs`` that raises on first use, standing in for the
    field layer's lazy sequence whose first ``bool()``/``len()``/iteration
    normalises the in-memory value and may raise ``ModelException`` (#744
    review, B1). Counts every touch."""

    def __init__(self, items: list[str] | None = None) -> None:
        self.items = items
        self.touches = 0

    def _touch(self) -> list[str]:
        self.touches += 1
        if self.items is None:
            raise ModelException("fallback materialised: TagField value must be a list")
        return self.items

    def __len__(self) -> int:
        return len(self._touch())

    def __iter__(self) -> Iterator[str]:
        return iter(self._touch())

    def __getitem__(self, i: Any) -> Any:
        return self._touch()[i]

    def __bool__(self) -> bool:
        return bool(self._touch())


class TestDropTagEntries:
    def test_pointer_present_drops_every_pointed_index(self, backend):
        swap_tags(backend, key(1), "x", "y")
        assert (
            backend.drop_tag_entries(key(1), "t", fallback_idxs=tag_idxs("stale")) == 1
        )
        assert tag_members(backend, "x", "y") == [set(), set()]

    def test_the_fallback_is_never_touched_while_a_pointer_exists(self, backend):
        # Base's order: the pointer read decides first; the fallback -- lazy
        # in the field layer, raising here -- is only consulted when it is
        # empty. Executed now and queued alike.
        swap_tags(backend, key(1), "x", "y")
        raising = _RaisingFallback()
        assert backend.drop_tag_entries(key(1), "t", fallback_idxs=raising) == 1
        assert raising.touches == 0
        assert tag_members(backend, "x", "y") == [set(), set()]
        swap_tags(backend, key(1), "z")
        raising = _RaisingFallback()
        uow = backend.begin()
        assert (
            backend.drop_tag_entries(key(1), "t", fallback_idxs=raising, uow=uow)
            is None
        )
        assert raising.touches == 0
        results = uow.commit()
        assert isinstance(results, list) and results  # per-command on Redis
        assert raising.touches == 0
        assert backend.index_members(idx("z", "t")) == set()

    def test_the_fallback_is_consulted_once_only_when_the_pointer_is_empty(
        self, backend
    ):
        backend.index_add(idx("x", "t"), key(1))
        counting = _RaisingFallback(tag_idxs("x"))
        assert backend.drop_tag_entries(key(1), "t", fallback_idxs=counting) == 0
        assert counting.touches >= 1
        assert backend.index_members(idx("x", "t")) == set()
        # And a raising fallback with no pointer propagates, writing nothing.
        backend.index_add(idx("x", "t"), key(1))
        with pytest.raises(ModelException, match="fallback materialised"):
            backend.drop_tag_entries(key(1), "t", fallback_idxs=_RaisingFallback())
        assert backend.index_members(idx("x", "t")) == {key(1)}

    def test_pointer_absent_falls_back_to_the_value_indexes(self, backend):
        backend.index_add(idx("x", "t"), key(1))
        backend.index_add(idx("y", "t"), key(1))
        # The DEL reply counts the pointer: absent, so 0, even though two
        # memberships went.
        assert (
            backend.drop_tag_entries(key(1), "t", fallback_idxs=tag_idxs("x", "y")) == 0
        )
        assert tag_members(backend, "x", "y") == [set(), set()]

    def test_nothing_to_drop_replies_zero(self, backend):
        assert backend.drop_tag_entries(key(1), "t", fallback_idxs=[]) == 0
        assert backend.drop_tag_entries(key(1), "t", fallback_idxs=tag_idxs("x")) == 0

    def test_drop_clears_the_pointer(self, backend):
        swap_tags(backend, key(1), "x")
        assert backend.drop_tag_entries(key(1), "t", fallback_idxs=[]) == 1
        assert backend.drop_tag_entries(key(1), "t", fallback_idxs=[]) == 0
        backend.index_add(idx("x", "t"), key(1))
        assert backend.drop_tag_entries(key(1), "t", fallback_idxs=[]) == 0
        assert backend.index_members(idx("x", "t")) == {key(1)}

    def test_drop_of_shared_tags_removes_only_this_member(self, backend):
        swap_tags(backend, key(1), "x", "y")
        swap_tags(backend, key(2), "x")
        backend.drop_tag_entries(key(1), "t", fallback_idxs=[])
        assert tag_members(backend, "x", "y") == [{key(2)}, set()]
        assert backend.load_record(key(1)) == {b"t": packed(["x", "y"])}


# -- Unit of work --------------------------------------------------------------


class TestUnitOfWork:
    def test_queued_swaps_write_nothing_before_commit(self, backend):
        swap(backend, key(1), "a")
        swap_tags(backend, key(1), "x")
        uow = backend.begin()
        assert swap(backend, key(1), "b", uow=uow) is None
        assert swap_tags(backend, key(1), "y", uow=uow) is None
        assert swap(backend, key(2), "c", uow=uow) is None
        assert members(backend, "a", "b", "c") == [{key(1)}, set(), set()]
        assert tag_members(backend, "x", "y") == [{key(1)}, set()]
        assert backend.load_record(key(1)) == {b"f": packed("a"), b"t": packed(["x"])}
        results = uow.commit()
        assert isinstance(results, list) and len(results) == 3
        assert all(r == 1 for r in results)
        assert members(backend, "a", "b", "c") == [set(), {key(1)}, {key(2)}]
        assert tag_members(backend, "x", "y") == [set(), {key(1)}]
        assert backend.load_record(key(1)) == {b"f": packed("b"), b"t": packed(["y"])}
        assert uow.commit() == []

    def test_queued_drops_write_nothing_before_commit(self, backend):
        swap(backend, key(1), "a")
        swap_tags(backend, key(1), "x", "y")
        uow = backend.begin()
        assert (
            backend.drop_index_entry(key(1), "f", fallback_idx=idx("a"), uow=uow)
            is None
        )
        assert backend.drop_tag_entries(key(1), "t", fallback_idxs=[], uow=uow) is None
        assert backend.index_members(idx("a")) == {key(1)}
        assert tag_members(backend, "x", "y") == [{key(1)}, {key(1)}]
        results = uow.commit()
        assert isinstance(results, list) and results
        assert backend.index_members(idx("a")) == set()
        assert tag_members(backend, "x", "y") == [set(), set()]
        # Both pointers are gone: a re-planted membership is only reachable
        # through the fallbacks now.
        backend.index_add(idx("a"), key(1))
        backend.index_add(idx("x", "t"), key(1))
        assert backend.drop_index_entry(key(1), "f", fallback_idx=idx("other")) == 0
        assert backend.drop_tag_entries(key(1), "t", fallback_idxs=[]) == 0

    def test_leaving_the_block_without_commit_discards_the_queue(self, backend):
        with backend.begin() as uow:
            swap(backend, key(1), "a", uow=uow)
            swap_tags(backend, key(1), "x", uow=uow)
        assert backend.index_members(idx("a")) == set()
        assert backend.index_members(idx("x", "t")) == set()
        assert backend.load_record(key(1)) is None

    def test_a_queued_conflict_raises_at_commit_and_writes_nothing_of_its_own(
        self, backend, backend_is_redis
    ):
        """The authoritative check runs when the queue executes, and the
        conflicting swap itself leaves no trace on either backend. *How* it
        surfaces differs and is pinned: a Redis pipeline relays the Lua's
        raw error reply (``ResponseError: POPOTO_UNIQUE_CONFLICT``), while
        Postgres raises the protocol's ``ModelException`` at ``commit()``."""
        swap(backend, key(1), "a")
        uow = backend.begin()
        assert swap(backend, key(2), "a", uow=uow) is None
        with pytest.raises(Exception) as info:
            uow.commit()
        if backend_is_redis:
            from redis.exceptions import ResponseError

            assert isinstance(info.value, ResponseError)
            assert "POPOTO_UNIQUE_CONFLICT" in str(info.value)
        else:
            assert isinstance(info.value, ModelException)
            assert str(info.value) == MESSAGE.format(
                key=key(2), field="f", idx=idx("a")
            )
        assert backend.index_members(idx("a")) == {key(1)}
        assert backend.load_record(key(2)) is None
        assert uow.commit() == []

    def test_a_queued_conflict_and_the_rest_of_the_queue(
        self, backend, backend_is_redis
    ):
        """Documented deviation: Postgres rolls back the whole queue, a Redis
        pipeline commits the commands around the failed one."""
        swap(backend, key(1), "a")
        uow = backend.begin()
        swap_tags(backend, key(3), "x", uow=uow)  # before the conflict
        swap(backend, key(2), "a", uow=uow)  # conflicts
        swap(backend, key(3), "b", uow=uow)  # after the conflict
        with pytest.raises(Exception):
            uow.commit()
        assert backend.index_members(idx("a")) == {key(1)}
        assert backend.load_record(key(2)) is None
        if backend_is_redis:
            assert backend.index_members(idx("x", "t")) == {key(3)}
            assert backend.index_members(idx("b")) == {key(3)}
        else:
            assert backend.index_members(idx("x", "t")) == set()
            assert backend.index_members(idx("b")) == set()
            assert backend.load_record(key(3)) is None


# -- Concurrency ---------------------------------------------------------------


class TestConcurrentUniqueClaim:
    def test_two_instances_claiming_one_value_get_one_success_and_one_conflict(
        self, backend, backend_is_redis
    ):
        """Two backend instances (two connections on Postgres), two records,
        one unique value, released together through a barrier: exactly one
        swap succeeds, the other raises the oracle's conflict, and the index
        holds exactly the winner. Redis's single thread guarantees this; the
        Postgres backend's advisory lock on the target index reproduces it.
        """
        if backend_is_redis:
            instances: list[Any] = [RedisBackend(), RedisBackend()]
        else:
            instances = [PostgresBackend(backend.url), PostgresBackend(backend.url)]
            for inst in instances:
                inst._connection()  # bootstrap outside the timed section
        barrier = threading.Barrier(2)
        outcomes: dict[str, Any] = {}

        def claim(inst: Any, k: str) -> None:
            try:
                barrier.wait(timeout=10.0)
                outcomes[k] = swap(inst, k, "u")
            except BaseException as exc:  # asserted below
                outcomes[k] = exc

        threads = [
            threading.Thread(target=claim, args=(inst, key(n)), name=f"claim-{n}")
            for n, inst in enumerate(instances, start=1)
        ]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20.0)
            assert not any(t.is_alive() for t in threads), "a claim never finished"
        finally:
            if not backend_is_redis:
                for inst in instances:
                    inst.close()
        winners = [k for k, out in outcomes.items() if out == 1]
        losers = [k for k, out in outcomes.items() if isinstance(out, ModelException)]
        assert len(winners) == 1 and len(losers) == 1, outcomes
        assert str(outcomes[losers[0]]) == MESSAGE.format(
            key=losers[0], field="f", idx=idx("u")
        )
        assert backend.index_members(idx("u")) == {winners[0]}
        assert backend.load_record(winners[0]) == {b"f": packed("u")}
        assert backend.load_record(losers[0]) is None

    def test_the_second_claimant_waits_for_the_first_and_then_sees_its_row(
        self, backend, backend_is_redis, monkeypatch
    ):
        """The deterministic shape of the race above, Postgres leg only.

        Instance A (the fixture backend) has passed its uniqueness check for
        ``u`` and is about to join the index; ``_add_member`` is hooked to
        start instance B's claim of ``u`` for another record on a thread and
        to return only once B is either done (the double-claim shape: B read
        an empty index too) or blocked on A's lock in ``pg_stat_activity``
        (the correct shape). A then commits, B wakes, re-reads under the lock,
        finds A's row and raises. Without the index lock both claims commit
        and the unique index holds two members. Redis has no hook point and
        needs none.
        """
        if backend_is_redis:
            pytest.skip("Postgres-leg assertion: Redis serialises these natively")
        import psycopg

        other = PostgresBackend(backend.url)
        probe = psycopg.connect(backend.url, autocommit=True)
        other_pid = other._connection().info.backend_pid
        outcome: list[Any] = []

        def claim_from_b() -> None:
            try:
                outcome.append(swap(other, key(2), "u"))
            except BaseException as exc:  # asserted below
                outcome.append(exc)

        claimer = threading.Thread(target=claim_from_b, name="instance-B-claim")
        real_add_member = postgres_module._add_member

        def hooked_add_member(cur, index, member):
            # Fire once: A is past validation, inside its transaction.
            monkeypatch.setattr(postgres_module, "_add_member", real_add_member)
            claimer.start()
            deadline = time.monotonic() + 10.0
            while claimer.is_alive() and time.monotonic() < deadline:
                row = probe.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                    (other_pid,),
                ).fetchone()
                if row is not None and row[0] == "Lock":
                    break  # B is queued behind A's advisory lock
                time.sleep(0.01)
            else:
                assert not claimer.is_alive(), "B neither finished nor blocked"
            return real_add_member(cur, index, member)

        monkeypatch.setattr(postgres_module, "_add_member", hooked_add_member)
        try:
            got = swap(backend, key(1), "u")
            claimer.join(timeout=10.0)
            assert not claimer.is_alive(), "B's claim never completed after A committed"
        finally:
            probe.close()
            other.close()
        assert got == 1
        assert len(outcome) == 1 and isinstance(outcome[0], ModelException), outcome
        assert str(outcome[0]) == MESSAGE.format(key=key(2), field="f", idx=idx("u"))
        assert backend.index_members(idx("u")) == {key(1)}, "double claim"
        assert backend.load_record(key(2)) is None

    def test_two_instances_swapping_one_record_serialise(
        self, backend, backend_is_redis
    ):
        """Two instances moving the *same* record to different values at once:
        the record ends in exactly one index -- the last writer's -- never in
        both (the record-key lock orders the two read-modify-writes)."""
        if backend_is_redis:
            instances: list[Any] = [RedisBackend(), RedisBackend()]
        else:
            instances = [PostgresBackend(backend.url), PostgresBackend(backend.url)]
            for inst in instances:
                inst._connection()
        swap(backend, key(1), "start", unique=False)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def move(inst: Any, value: str) -> None:
            try:
                barrier.wait(timeout=10.0)
                for _ in range(20):
                    swap(inst, key(1), value, unique=False)
            except BaseException as exc:  # asserted below
                errors.append(exc)

        threads = [
            threading.Thread(target=move, args=(inst, v), name=f"move-{v}")
            for inst, v in zip(instances, ("left", "right"))
        ]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30.0)
            assert not any(t.is_alive() for t in threads), "a move never finished"
        finally:
            if not backend_is_redis:
                for inst in instances:
                    inst.close()
        assert not errors, errors
        found = members(backend, "start", "left", "right")
        assert found[0] == set()
        assert sorted(len(s) for s in found[1:]) == [0, 1], found
        final = msgpack.unpackb(backend.load_record(key(1))[b"f"])
        assert found[1 if final == "left" else 2] == {key(1)}

    def test_the_second_mover_waits_for_the_first_on_the_record_lock(
        self, backend, backend_is_redis, monkeypatch
    ):
        """The deterministic shape of the record race above, Postgres leg only.

        Instance A is moving record 1 from ``start`` to ``left`` and has read
        its pointer; ``_remove_member`` is hooked to start instance B's move
        of the same record to ``right`` on a thread and to return only once B
        is either done (both read ``start`` as the old index: a dual
        membership) or blocked on A's record lock. With the lock B waits,
        then reads ``left`` as the old index and leaves it: the record ends in
        ``right`` only, as Redis's single thread would leave it.
        """
        if backend_is_redis:
            pytest.skip("Postgres-leg assertion: Redis serialises these natively")
        import psycopg

        swap(backend, key(1), "start", unique=False)
        other = PostgresBackend(backend.url)
        probe = psycopg.connect(backend.url, autocommit=True)
        other_pid = other._connection().info.backend_pid
        outcome: list[Any] = []

        def move_from_b() -> None:
            try:
                outcome.append(swap(other, key(1), "right", unique=False))
            except BaseException as exc:  # asserted below
                outcome.append(exc)

        mover = threading.Thread(target=move_from_b, name="instance-B-move")
        real_remove_member = postgres_module._remove_member

        def hooked_remove_member(cur, index, member):
            # Fire once: A has read the pointer (``start``) and is about to
            # leave it, inside its transaction.
            monkeypatch.setattr(postgres_module, "_remove_member", real_remove_member)
            mover.start()
            deadline = time.monotonic() + 10.0
            while mover.is_alive() and time.monotonic() < deadline:
                row = probe.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                    (other_pid,),
                ).fetchone()
                if row is not None and row[0] == "Lock":
                    break  # B is queued behind A's record lock
                time.sleep(0.01)
            else:
                assert not mover.is_alive(), "B neither finished nor blocked"
            return real_remove_member(cur, index, member)

        monkeypatch.setattr(postgres_module, "_remove_member", hooked_remove_member)
        try:
            got = swap(backend, key(1), "left", unique=False)
            mover.join(timeout=10.0)
            assert not mover.is_alive(), "B's move never completed after A committed"
        finally:
            probe.close()
            other.close()
        assert got == 1
        assert outcome == [1], outcome
        assert members(backend, "start", "left", "right") == [
            set(),
            set(),
            {key(1)},
        ], "dual membership: B did not see A's move"
        assert backend.load_record(key(1)) == {b"f": packed("right")}


# -- Nothing still stubbed -------------------------------------------------------


def test_no_family_still_raises_on_postgres(backend, backend_is_redis):
    # Family I is WS3e's (see ``test_validity.py``) and H is WS3d's (see
    # ``test_decay.py``); only ``native()`` refuses, by design.
    if backend_is_redis:
        pytest.skip("Postgres-leg assertion")
    assert (
        backend.decayed_rank(
            idx("z"), now=0.0, decay_rate=0.1, limit=None, pretrim_max_ratio=1.0
        )
        == []
    )
    with pytest.raises(NotImplementedError, match="Redis-only"):
        backend.native()
