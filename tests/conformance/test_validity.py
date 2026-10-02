"""Validity-family conformance: group I -- ``supersede``, ``interval_of``,
``interval_members``, ``drop_validity``, ``open_pointer`` (#631 WS3e).

Every test runs on every configured backend with the *same* assertions:
``RedisBackend`` is the oracle (its ``supersede`` is ``SUPERSEDE_LUA``, moved
verbatim in WS0), so whatever it returns, stores or raises -- down to the text
of each typed exception -- is what ``PostgresBackend`` must return, store or
raise. State is read back only through the protocol: ``interval_of`` and
``sorted_score`` for the three interval indexes, ``map_get`` for the chain
links, ``open_pointer`` for the identity pointers and ``sorted_members`` for
the member listing ``filter(validity__current=False)`` relies on. The one
place a test reaches past the validity group is ``sorted_add``, to seed a
member into *one* interval index (an ``import_state`` shape) for the
exclusion-rule table.

Two protocol-visible differences are pinned rather than hidden: a unit of
work's ``commit()`` raises the raw ``ResponseError`` on Redis and the typed
``ValidityError`` on Postgres (:func:`typed` maps both to the same exception;
the Redis text additionally carries redis-py's ``Command # N (...) of
pipeline caused error:`` prefix), and ``commit()``'s per-operation entry for a supersede is the
raw script reply on Redis and the decoded member on Postgres (only its
truthiness is asserted, which is all ``ProvenanceJournal._write`` reads).

Never touches database 0 or schema ``public``: the Redis leg goes through the
plugin-bound client and the Postgres leg through the harness's own schema.
"""

from __future__ import annotations

import math
import threading
from typing import Any

import pytest

from popoto.backends.postgres import PostgresBackend
from popoto.backends.redis import RedisBackend, _validity_keys
from popoto.fields.validity_field import (
    CLOSE_BEFORE_START_ERROR,
    MEMBER_ABSENT_ERROR,
    VALID_FROM_CONFLICT_ERROR,
    ValidityCloseBeforeStartError,
    ValidityError,
    ValidityMemberAbsentError,
    ValidityValidFromConflictError,
    map_lua_error,
)

pytestmark = pytest.mark.conformance

PREFIX = "popoto_test:631:validity"
#: Deviation 10: the field layer hands the backend the opaque
#: ``$ValidityF:<Model>`` namespace and the field name separately.
MODEL = f"$ValidityF:{PREFIX}:Claim"
OTHER_MODEL = f"$ValidityF:{PREFIX}:Other"
FIELD = "validity"
CLASS_SET = f"$Class:{PREFIX}:Claim"
KEYS = _validity_keys(MODEL, FIELD)
VF, IA, IG = KEYS["valid_from"], KEYS["invalid_at"], KEYS["ingested_at"]
FWD, REV = KEYS["chain_fwd"], KEYS["chain_rev"]
INF = float("inf")
NOW = 1_700_000_000.0

MEMBER_ABSENT_TEXT = (
    "ValidityField: a member named by this call does not exist at write "
    "time, so no interval, chain link, or pointer was written ({detail})"
)
CLOSE_BEFORE_START_TEXT = (
    "ValidityField: close-at precedes the record's own valid_from ({detail})"
)
VALID_FROM_CONFLICT_TEXT = (
    "ValidityField: the asserted valid_from disagrees with the start "
    "already stored for this record; valid-time has one writer, the field "
    "value at construction ({detail})"
)


def member(suffix: Any) -> str:
    return f"{PREFIX}:Claim:{suffix}"


def save(backend: Any, *suffixes: Any) -> list[str]:
    """Create the records the membership guards look for; returns their keys."""
    keys = []
    for suffix in suffixes:
        key = member(suffix)
        backend.save_record(key, {b"name": str(suffix).encode()}, class_set=CLASS_SET)
        keys.append(key)
    return keys


def supersede(backend: Any, *, model: str = MODEL, **overrides: Any) -> Any:
    """``backend.supersede`` with every keyword defaulted the way
    ``ValidityField.execute_supersede`` defaults them."""
    kwargs: dict[str, Any] = dict(
        mode="open",
        new_member="",
        old_member="",
        now=NOW,
        valid_from=None,
        ingested_at=None,
        close_at=None,
        assert_valid_from=False,
        pointer_digest=None,
    )
    kwargs.update(overrides)
    return backend.supersede(model, FIELD, **kwargs)


def interval(backend: Any, key: str) -> tuple:
    return backend.interval_of(VF, IA, key)


def ingested(backend: Any, key: str) -> float | None:
    return backend.sorted_score(IG, key)


def links(backend: Any, key: str) -> tuple:
    """``(superseded_by, supersedes)`` as the chain walk reads them."""
    return (backend.map_get(FWD, key), backend.map_get(REV, key))


def pointer(backend: Any, digest: str, *, model: str = MODEL) -> str | None:
    return backend.open_pointer(model, FIELD, digest)


def typed(exc: BaseException) -> BaseException:
    """The typed exception for whatever a backend raised: already typed on the
    direct path of both backends and on Postgres's ``commit()``; the raw
    ``ResponseError`` on a Redis ``commit()``, which the field layer remaps
    through ``map_lua_error`` exactly like this."""
    return exc if isinstance(exc, ValidityError) else map_lua_error(exc)


def second_instance(backend: Any) -> Any:
    """A second backend instance on its own connection (Postgres) or client
    (Redis), for the two-connection races."""
    if isinstance(backend, RedisBackend):
        return RedisBackend()
    assert isinstance(backend, PostgresBackend)
    return PostgresBackend(backend.url)


def release(instance: Any) -> None:
    if isinstance(instance, PostgresBackend):
        instance.close()


# -- mode 'open' -----------------------------------------------------------------


class TestOpen:
    def test_open_writes_the_three_scores_and_returns_none(self, backend):
        (a,) = save(backend, "a")
        assert (
            supersede(backend, new_member=a, valid_from=10.0, ingested_at=20.0) is None
        )
        assert interval(backend, a) == (10.0, INF)
        assert ingested(backend, a) == 20.0
        assert links(backend, a) == (None, None)

    def test_open_defaults_every_instant_to_the_callers_clock(self, backend):
        # Deviation 2: ``now`` is the one clock; nothing reads the server's.
        (a,) = save(backend, "a")
        supersede(backend, new_member=a, now=12345.5)
        assert interval(backend, a) == (12345.5, INF)
        assert ingested(backend, a) == 12345.5

    def test_open_is_nx_so_a_resave_never_shifts_the_interval(self, backend):
        (a,) = save(backend, "a")
        supersede(backend, new_member=a, valid_from=10.0, ingested_at=20.0)
        supersede(backend, new_member=a, valid_from=99.0, ingested_at=98.0)
        assert interval(backend, a) == (10.0, INF)
        assert ingested(backend, a) == 20.0

    def test_open_does_not_require_the_record_to_exist(self, backend):
        # Plan Risk 1: mode 'open' is co-transactional with the record's own
        # write, so the membership guards never run for it.
        ghost = member("never-saved")
        assert supersede(backend, new_member=ghost, valid_from=1.0) is None
        assert interval(backend, ghost) == (1.0, INF)

    def test_open_sets_the_pointer_only_when_a_digest_is_given(self, backend):
        a, b = save(backend, "a", "b")
        supersede(backend, new_member=a)
        supersede(backend, new_member=b, pointer_digest="d")
        assert pointer(backend, "d") == b
        assert pointer(backend, "missing") is None

    def test_open_never_resurrects_a_closed_record(self, backend):
        # Plan Race 2: the reason on_save goes through the script and not a
        # bare ZADD. The closed record keeps its close and gets no pointer.
        (a,) = save(backend, "a")
        supersede(backend, new_member=a, valid_from=10.0)
        assert supersede(backend, mode="invalidate", old_member=a, close_at=50.0) == a
        supersede(backend, new_member=a, valid_from=10.0, pointer_digest="d", now=60.0)
        assert interval(backend, a) == (10.0, 50.0)
        assert pointer(backend, "d") is None

    def test_an_unasserted_disagreeing_valid_from_is_silently_kept_out(self, backend):
        (a,) = save(backend, "a")
        supersede(backend, new_member=a, valid_from=10.0)
        assert supersede(backend, new_member=a, valid_from=11.0) is None
        assert interval(backend, a) == (10.0, INF)

    def test_an_asserted_disagreeing_valid_from_is_refused_with_lua_numbers(
        self, backend
    ):
        # The reply renders both numbers with Lua's tostring (%.14g):
        # 1759500000.123456 is "1759500000.1235", 100.5 is "100.5".
        (a,) = save(backend, "a")
        supersede(backend, new_member=a, valid_from=1759500000.123456)
        with pytest.raises(ValidityValidFromConflictError) as excinfo:
            supersede(backend, new_member=a, valid_from=100.5, assert_valid_from=True)
        assert str(excinfo.value) == VALID_FROM_CONFLICT_TEXT.format(
            detail=f"{VALID_FROM_CONFLICT_ERROR} 1759500000.1235 100.5"
        )
        assert interval(backend, a) == (1759500000.123456, INF)
        # Integral and sub-unit values render without a fraction / with the
        # shortest %.14g form: "100" and "0.3".
        (b,) = save(backend, "b")
        supersede(backend, new_member=b, valid_from=100.0)
        with pytest.raises(ValidityValidFromConflictError) as excinfo:
            supersede(
                backend, new_member=b, valid_from=0.1 + 0.2, assert_valid_from=True
            )
        assert str(excinfo.value).endswith(f"({VALID_FROM_CONFLICT_ERROR} 100 0.3)")

    def test_an_asserted_agreeing_valid_from_passes(self, backend):
        (a,) = save(backend, "a")
        supersede(backend, new_member=a, valid_from=10.0)
        assert (
            supersede(backend, new_member=a, valid_from=10.0, assert_valid_from=True)
            is None
        )
        assert interval(backend, a) == (10.0, INF)


# -- mode 'supersede' ------------------------------------------------------------


class TestSupersede:
    def test_explicit_incumbent_is_closed_chained_and_returned(self, backend):
        old, new = save(backend, "old", "new")
        supersede(backend, new_member=old, valid_from=10.0)
        closed = supersede(
            backend,
            mode="supersede",
            new_member=new,
            old_member=old,
            valid_from=50.0,
            close_at=50.0,
            now=50.0,
        )
        assert closed == old
        assert isinstance(closed, str)
        assert interval(backend, old) == (10.0, 50.0)
        assert interval(backend, new) == (50.0, INF)
        assert ingested(backend, new) == 50.0
        assert links(backend, old) == (new.encode(), None)
        assert links(backend, new) == (None, old.encode())

    def test_pointer_resolves_the_incumbent_and_is_repointed(self, backend):
        old, new, newer = save(backend, "old", "new", "newer")
        supersede(backend, new_member=old, valid_from=10.0, pointer_digest="d")
        assert (
            supersede(
                backend, mode="supersede", new_member=new, now=50.0, pointer_digest="d"
            )
            == old
        )
        assert pointer(backend, "d") == new
        assert (
            supersede(
                backend,
                mode="supersede",
                new_member=newer,
                now=60.0,
                pointer_digest="d",
            )
            == new
        )
        assert pointer(backend, "d") == newer
        assert interval(backend, old) == (10.0, 50.0)
        assert interval(backend, new) == (50.0, 60.0)
        assert interval(backend, newer) == (60.0, INF)
        assert links(backend, old) == (new.encode(), None)
        assert links(backend, new) == (newer.encode(), old.encode())
        assert links(backend, newer) == (None, new.encode())

    def test_an_explicit_incumbent_beats_the_pointer(self, backend):
        a, b, c = save(backend, "a", "b", "c")
        supersede(backend, new_member=a, valid_from=10.0, pointer_digest="d")
        supersede(backend, new_member=b, valid_from=10.0)
        assert (
            supersede(
                backend,
                mode="supersede",
                new_member=c,
                old_member=b,
                now=50.0,
                pointer_digest="d",
            )
            == b
        )
        assert interval(backend, a) == (10.0, INF)
        assert interval(backend, b) == (10.0, 50.0)
        assert pointer(backend, "d") == c

    def test_closing_is_idempotent(self, backend):
        # Plan Race 1: under retry, or when two writers race one identity, the
        # second close is a no-op -- the chain is not forked and the close
        # instant does not move.
        old, new, other = save(backend, "old", "new", "other")
        supersede(backend, new_member=old, valid_from=10.0)
        supersede(backend, mode="supersede", new_member=new, old_member=old, now=50.0)
        assert (
            supersede(
                backend, mode="supersede", new_member=other, old_member=old, now=70.0
            )
            is None
        )
        assert interval(backend, old) == (10.0, 50.0)
        assert links(backend, old) == (new.encode(), None)
        assert links(backend, other) == (None, None)
        # The newcomer of the no-op close still opens.
        assert interval(backend, other) == (70.0, INF)

    def test_a_member_cannot_supersede_itself(self, backend):
        (a,) = save(backend, "a")
        supersede(backend, new_member=a, valid_from=10.0)
        assert (
            supersede(backend, mode="supersede", new_member=a, old_member=a, now=50.0)
            is None
        )
        assert interval(backend, a) == (10.0, INF)
        assert links(backend, a) == (None, None)

    def test_a_pointer_left_naming_a_deleted_record_means_no_incumbent(self, backend):
        # Plan Risk 3: the pointer is a hint, not an assertion.
        old, new = save(backend, "old", "new")
        supersede(backend, new_member=old, valid_from=10.0, pointer_digest="d")
        backend.delete_record(old, class_set=CLASS_SET)
        assert (
            supersede(
                backend, mode="supersede", new_member=new, now=50.0, pointer_digest="d"
            )
            is None
        )
        assert interval(backend, new) == (50.0, INF)
        assert pointer(backend, "d") == new
        assert links(backend, new) == (None, None)

    def test_an_incumbent_with_no_interval_is_not_closed(self, backend):
        old, new = save(backend, "old", "new")
        assert (
            supersede(
                backend, mode="supersede", new_member=new, old_member=old, now=50.0
            )
            is None
        )
        assert interval(backend, old) == (None, None)
        assert interval(backend, new) == (50.0, INF)
        assert links(backend, old) == (None, None)

    def test_close_before_start_is_refused_and_writes_nothing(self, backend):
        # All-or-nothing: the newcomer is not opened and nothing is linked.
        old, new = save(backend, "old", "new")
        supersede(backend, new_member=old, valid_from=100.0)
        with pytest.raises(ValidityCloseBeforeStartError) as excinfo:
            supersede(
                backend,
                mode="supersede",
                new_member=new,
                old_member=old,
                close_at=50.0,
                now=50.0,
            )
        assert str(excinfo.value) == CLOSE_BEFORE_START_TEXT.format(
            detail=CLOSE_BEFORE_START_ERROR
        )
        assert interval(backend, old) == (100.0, INF)
        assert interval(backend, new) == (None, None)
        assert ingested(backend, new) is None
        assert links(backend, old) == (None, None)
        assert links(backend, new) == (None, None)

    def test_closing_exactly_at_the_start_is_allowed(self, backend):
        old, new = save(backend, "old", "new")
        supersede(backend, new_member=old, valid_from=100.0)
        assert (
            supersede(
                backend,
                mode="supersede",
                new_member=new,
                old_member=old,
                close_at=100.0,
                now=100.0,
            )
            == old
        )
        assert interval(backend, old) == (100.0, 100.0)

    def test_an_absent_successor_is_refused_by_name(self, backend):
        (old,) = save(backend, "old")
        supersede(backend, new_member=old, valid_from=10.0, pointer_digest="d")
        ghost = member("ghost")
        with pytest.raises(ValidityMemberAbsentError) as excinfo:
            supersede(
                backend,
                mode="supersede",
                new_member=ghost,
                now=50.0,
                pointer_digest="d",
            )
        assert str(excinfo.value) == MEMBER_ABSENT_TEXT.format(
            detail=f"{MEMBER_ABSENT_ERROR} successor {ghost}"
        )
        assert interval(backend, old) == (10.0, INF)
        assert interval(backend, ghost) == (None, None)
        assert pointer(backend, "d") == old

    def test_an_absent_asserted_incumbent_is_refused_by_name(self, backend):
        (new,) = save(backend, "new")
        ghost = member("ghost")
        with pytest.raises(ValidityMemberAbsentError) as excinfo:
            supersede(
                backend, mode="supersede", new_member=new, old_member=ghost, now=50.0
            )
        assert str(excinfo.value) == MEMBER_ABSENT_TEXT.format(
            detail=f"{MEMBER_ABSENT_ERROR} incumbent {ghost}"
        )
        assert interval(backend, new) == (None, None)

    def test_every_token_maps_to_a_value_error_subclass(self, backend):
        # Plan D4: ObservationProtocol degrades on ``except ValueError``.
        (old,) = save(backend, "old")
        supersede(backend, new_member=old, valid_from=100.0)
        raised: list[BaseException] = []
        for kwargs in (
            dict(mode="supersede", new_member=member("ghost"), old_member=old),
            dict(mode="invalidate", old_member=member("ghost")),
            dict(mode="invalidate", old_member=old, close_at=50.0),
            dict(mode="open", new_member=old, valid_from=1.0, assert_valid_from=True),
        ):
            with pytest.raises(ValueError) as excinfo:
                supersede(backend, now=50.0, **kwargs)
            raised.append(excinfo.value)
        assert [type(e) for e in raised] == [
            ValidityMemberAbsentError,
            ValidityMemberAbsentError,
            ValidityCloseBeforeStartError,
            ValidityValidFromConflictError,
        ]
        assert all(isinstance(e, ValidityError) for e in raised)


# -- mode 'invalidate' -----------------------------------------------------------


class TestInvalidate:
    def test_explicit_incumbent_is_closed_without_a_chain_link(self, backend):
        (old,) = save(backend, "old")
        supersede(backend, new_member=old, valid_from=10.0, pointer_digest="d")
        assert (
            supersede(backend, mode="invalidate", old_member=old, close_at=50.0) == old
        )
        assert interval(backend, old) == (10.0, 50.0)
        assert links(backend, old) == (None, None)
        # No newcomer, so the pointer is left where it was.
        assert pointer(backend, "d") == old

    def test_pointer_resolves_the_incumbent(self, backend):
        (old,) = save(backend, "old")
        supersede(backend, new_member=old, valid_from=10.0, pointer_digest="d")
        assert (
            supersede(backend, mode="invalidate", pointer_digest="d", now=50.0) == old
        )
        assert interval(backend, old) == (10.0, 50.0)

    def test_close_at_defaults_to_now(self, backend):
        (old,) = save(backend, "old")
        supersede(backend, new_member=old, valid_from=10.0)
        assert supersede(backend, mode="invalidate", old_member=old, now=77.0) == old
        assert interval(backend, old) == (10.0, 77.0)

    def test_an_already_closed_incumbent_returns_none(self, backend):
        (old,) = save(backend, "old")
        supersede(backend, new_member=old, valid_from=10.0)
        supersede(backend, mode="invalidate", old_member=old, close_at=50.0)
        assert (
            supersede(backend, mode="invalidate", old_member=old, close_at=60.0) is None
        )
        assert interval(backend, old) == (10.0, 50.0)

    def test_nothing_to_resolve_returns_none(self, backend):
        assert supersede(backend, mode="invalidate") is None
        assert supersede(backend, mode="invalidate", pointer_digest="unset") is None


# -- interval reads ---------------------------------------------------------------


class TestIntervalReads:
    def test_interval_of_absent_partial_and_open(self, backend):
        assert interval(backend, member("absent")) == (None, None)
        backend.sorted_add(VF, member("start-only"), 5.0)
        assert interval(backend, member("start-only")) == (5.0, None)
        backend.sorted_add(IA, member("close-only"), 9.0)
        assert interval(backend, member("close-only")) == (None, 9.0)
        (a,) = save(backend, "a")
        supersede(backend, new_member=a, valid_from=10.0)
        start, close = interval(backend, a)
        assert isinstance(start, float) and isinstance(close, float)
        assert start == 10.0
        assert math.isinf(close) and close > 0

    def test_exclusion_rule_over_every_index_shape(self, backend):
        """The rule every retrieval gate consumes through
        ``resolve_excluded_keys``: ``invalid_at <= as_of OR valid_from >
        as_of`` excludes; a member absent from one or both indexes is
        excluded only by the index it is in, and one in neither is never
        excluded. ``+inf`` never satisfies ``<= as_of`` for a finite
        ``as_of``. The valid set is the exact complement over managed
        members: ``valid_from <= as_of AND invalid_at > as_of``."""
        both = {
            "open": (10.0, INF),
            "closed": (10.0, 50.0),
            "future": (200.0, INF),
            "closed-at-as-of": (10.0, 100.0),
            "starts-at-as-of": (100.0, INF),
        }
        for suffix, (start, close) in both.items():
            backend.sorted_add(VF, member(suffix), start)
            backend.sorted_add(IA, member(suffix), close)
        backend.sorted_add(VF, member("vf-past-only"), 10.0)
        backend.sorted_add(VF, member("vf-future-only"), 200.0)
        backend.sorted_add(IA, member("ia-past-only"), 50.0)
        backend.sorted_add(IA, member("ia-open-only"), INF)
        save(backend, "unmanaged")

        excluded = backend.interval_members(VF, IA, 100.0, select="excluded")
        assert excluded == {
            member("closed"),
            member("future"),
            member("closed-at-as-of"),
            member("vf-future-only"),
            member("ia-past-only"),
        }
        valid = backend.interval_members(VF, IA, 100.0, select="valid")
        assert valid == {member("open"), member("starts-at-as-of")}
        assert all(isinstance(m, str) for m in excluded | valid)
        assert member("unmanaged") not in excluded | valid

    def test_infinite_as_of_bounds(self, backend):
        backend.sorted_add(VF, member("open"), 10.0)
        backend.sorted_add(IA, member("open"), INF)
        backend.sorted_add(VF, member("closed"), 10.0)
        backend.sorted_add(IA, member("closed"), 50.0)
        # inf <= inf: at the end of time every managed member is closed.
        assert backend.interval_members(VF, IA, INF, select="excluded") == {
            member("open"),
            member("closed"),
        }
        assert backend.interval_members(VF, IA, INF, select="valid") == set()
        # Nothing has started at -inf, and 1e308 is still finite.
        assert backend.interval_members(VF, IA, -INF, select="excluded") == {
            member("open"),
            member("closed"),
        }
        assert backend.interval_members(VF, IA, 1e308, select="excluded") == {
            member("closed")
        }
        assert backend.interval_members(VF, IA, 1e308, select="valid") == {
            member("open")
        }

    def test_empty_indexes_read_as_empty_sets(self, backend):
        assert backend.interval_members(VF, IA, NOW, select="valid") == set()
        assert backend.interval_members(VF, IA, NOW, select="excluded") == set()

    def test_the_interval_indexes_are_sorted_indexes(self, backend):
        # ``filter(validity__current=False)`` lists every managed member with
        # ``sorted_members`` on these two names; the open sentinel is the
        # highest score so an open member sorts last.
        a, b = save(backend, "a", "b")
        supersede(backend, new_member=a, valid_from=10.0)
        supersede(backend, new_member=b, valid_from=20.0)
        supersede(backend, mode="invalidate", old_member=a, close_at=50.0)
        assert backend.sorted_members(VF) == [a, b]
        assert backend.sorted_members(IA) == [a, b]
        assert backend.sorted_members(IA, reverse=True) == [b, a]
        assert backend.sorted_count(IG) == 2


# -- drop_validity and the open pointers -----------------------------------------


class TestDropValidity:
    def test_drops_every_row_and_every_pointer_naming_the_member(self, backend):
        old, a = save(backend, "old", "a")
        supersede(backend, new_member=old, valid_from=10.0)
        supersede(backend, mode="supersede", new_member=a, old_member=old, now=50.0)
        # Two identities point at the same open record.
        supersede(backend, new_member=a, pointer_digest="d1")
        supersede(backend, new_member=a, pointer_digest="d2")
        assert pointer(backend, "d1") == pointer(backend, "d2") == a

        assert backend.drop_validity(MODEL, FIELD, a) == 1

        assert interval(backend, a) == (None, None)
        assert ingested(backend, a) is None
        assert links(backend, a) == (None, None)
        assert pointer(backend, "d1") is None
        assert pointer(backend, "d2") is None
        # Documented limitation: the neighbour's link still names the
        # deleted record as a *value*; the chain walk reads it as an end.
        assert links(backend, old) == (a.encode(), None)
        assert interval(backend, old) == (10.0, 50.0)

    def test_without_pointers_and_on_an_absent_member_returns_zero(self, backend):
        (a,) = save(backend, "a")
        supersede(backend, new_member=a, valid_from=10.0)
        assert backend.drop_validity(MODEL, FIELD, a) == 0
        assert interval(backend, a) == (None, None)
        assert backend.drop_validity(MODEL, FIELD, member("absent")) == 0

    def test_a_member_whose_name_is_a_prefix_of_anothers(self, backend):
        a, ab = save(backend, "a", "ab")
        supersede(backend, new_member=a, valid_from=10.0, pointer_digest="da")
        supersede(backend, new_member=ab, valid_from=20.0, pointer_digest="dab")
        assert backend.drop_validity(MODEL, FIELD, a) == 1
        assert interval(backend, a) == (None, None)
        assert pointer(backend, "da") is None
        assert interval(backend, ab) == (20.0, INF)
        assert ingested(backend, ab) == NOW
        assert pointer(backend, "dab") == ab

    def test_another_models_pointer_to_the_same_key_survives(self, backend):
        (a,) = save(backend, "a")
        supersede(backend, new_member=a, valid_from=10.0, pointer_digest="d")
        supersede(
            backend,
            model=OTHER_MODEL,
            new_member=a,
            valid_from=10.0,
            pointer_digest="d",
        )
        assert backend.drop_validity(MODEL, FIELD, a) == 1
        assert pointer(backend, "d") is None
        assert pointer(backend, "d", model=OTHER_MODEL) == a
        other = _validity_keys(OTHER_MODEL, FIELD)
        assert backend.interval_of(other["valid_from"], other["invalid_at"], a) == (
            10.0,
            INF,
        )

    def test_queued_on_a_unit_of_work(self, backend):
        (a,) = save(backend, "a")
        supersede(backend, new_member=a, valid_from=10.0, pointer_digest="d")
        uow = backend.begin()
        assert backend.drop_validity(MODEL, FIELD, a, uow=uow) is None
        assert interval(backend, a) == (10.0, INF)
        assert pointer(backend, "d") == a
        assert uow.commit()
        assert interval(backend, a) == (None, None)
        assert pointer(backend, "d") is None


class TestOpenPointer:
    def test_lifecycle(self, backend):
        old, new = save(backend, "old", "new")
        assert pointer(backend, "d") is None
        supersede(backend, new_member=old, valid_from=10.0, pointer_digest="d")
        assert pointer(backend, "d") == old
        assert isinstance(pointer(backend, "d"), str)
        supersede(
            backend, mode="supersede", new_member=new, now=50.0, pointer_digest="d"
        )
        assert pointer(backend, "d") == new
        # A pure invalidate leaves the pointer naming the closed record, which
        # the next supersede then reads as "already closed" (idempotent).
        supersede(backend, mode="invalidate", pointer_digest="d", now=60.0)
        assert pointer(backend, "d") == new
        assert interval(backend, new) == (50.0, 60.0)
        backend.drop_validity(MODEL, FIELD, new)
        assert pointer(backend, "d") is None

    def test_digests_are_namespaced_by_model_and_field(self, backend):
        (a,) = save(backend, "a")
        supersede(backend, new_member=a, pointer_digest="d")
        assert pointer(backend, "d", model=OTHER_MODEL) is None
        assert backend.open_pointer(MODEL, "other_field", "d") is None


# -- the unit of work --------------------------------------------------------------


class TestUnitOfWork:
    def test_queued_supersede_applies_at_commit(self, backend):
        old, new = save(backend, "old", "new")
        supersede(backend, new_member=old, valid_from=10.0, pointer_digest="d")
        uow = backend.begin()
        queued = supersede(
            backend,
            mode="supersede",
            new_member=new,
            now=50.0,
            pointer_digest="d",
            uow=uow,
        )
        assert queued is None
        assert interval(backend, old) == (10.0, INF)
        assert pointer(backend, "d") == old
        results = uow.commit()
        assert len(results) == 1
        # The supersede's entry is truthy exactly when something was closed
        # (what ProvenanceJournal._write reads from results[close_index]).
        assert results[0]
        assert interval(backend, old) == (10.0, 50.0)
        assert interval(backend, new) == (50.0, INF)
        assert pointer(backend, "d") == new
        assert uow.commit() == []

    def test_a_no_op_close_commits_falsy(self, backend):
        (a,) = save(backend, "a")
        uow = backend.begin()
        supersede(backend, new_member=a, valid_from=10.0, uow=uow)
        results = uow.commit()
        assert len(results) == 1
        assert not results[0]

    def test_leaving_the_block_without_commit_discards_the_queue(self, backend):
        old, new = save(backend, "old", "new")
        supersede(backend, new_member=old, valid_from=10.0)
        with backend.begin() as uow:
            supersede(
                backend,
                mode="supersede",
                new_member=new,
                old_member=old,
                now=50.0,
                uow=uow,
            )
        assert interval(backend, old) == (10.0, INF)
        assert interval(backend, new) == (None, None)
        assert uow.commit() == []

    def test_an_error_at_commit_is_the_typed_error_and_applies_nothing(self, backend):
        (old,) = save(backend, "old")
        supersede(backend, new_member=old, valid_from=10.0, pointer_digest="d")
        ghost = member("ghost")
        uow = backend.begin()
        supersede(
            backend,
            mode="supersede",
            new_member=ghost,
            now=50.0,
            pointer_digest="d",
            uow=uow,
        )
        with pytest.raises(Exception) as excinfo:
            uow.commit()
        error = typed(excinfo.value)
        assert isinstance(error, ValidityMemberAbsentError)
        # redis-py prefixes a pipeline reply with ``Command # N (...) of
        # pipeline caused error:``, so only the tail is byte-identical here.
        assert str(error).startswith("ValidityField: a member named by this call")
        assert str(error).endswith(f"{MEMBER_ABSENT_ERROR} successor {ghost})")
        assert interval(backend, old) == (10.0, INF)
        assert interval(backend, ghost) == (None, None)
        assert pointer(backend, "d") == old


class TestSameTransactionSuccessor:
    """#588: membership is decided at the instant of the write, so a successor
    saved in the same unit of work is visible to the guard."""

    def test_successor_saved_in_the_same_unit_of_work_closes_the_incumbent(
        self, backend
    ):
        (old,) = save(backend, "old")
        supersede(backend, new_member=old, valid_from=10.0, pointer_digest="d")
        new = member("new")
        uow = backend.begin()
        backend.save_record(new, {b"name": b"new"}, class_set=CLASS_SET, uow=uow)
        supersede(
            backend,
            mode="supersede",
            new_member=new,
            now=50.0,
            pointer_digest="d",
            uow=uow,
        )
        assert backend.record_exists(new) is False
        assert uow.commit()
        assert backend.record_exists(new) is True
        assert interval(backend, old) == (10.0, 50.0)
        assert interval(backend, new) == (50.0, INF)
        assert links(backend, old) == (new.encode(), None)
        assert pointer(backend, "d") == new

    def test_a_successor_not_saved_in_the_unit_of_work_is_refused(self, backend):
        (old,) = save(backend, "old")
        supersede(backend, new_member=old, valid_from=10.0)
        new = member("new")
        uow = backend.begin()
        supersede(
            backend, mode="supersede", new_member=new, old_member=old, now=50.0, uow=uow
        )
        with pytest.raises(Exception) as excinfo:
            uow.commit()
        assert isinstance(typed(excinfo.value), ValidityMemberAbsentError)
        assert interval(backend, old) == (10.0, INF)

    def test_invalidate_mode_with_a_same_unit_of_work_successor(self, backend):
        # The issue's literal shape: save(pipeline) then invalidate(...,
        # superseded_by=..., pipeline), one execute.
        (e1,) = save(backend, "e1")
        supersede(backend, new_member=e1, valid_from=10.0)
        e2 = member("e2")
        uow = backend.begin()
        backend.save_record(e2, {b"name": b"e2"}, class_set=CLASS_SET, uow=uow)
        supersede(backend, new_member=e2, valid_from=50.0, uow=uow)
        supersede(
            backend, mode="supersede", new_member=e2, old_member=e1, now=50.0, uow=uow
        )
        uow.commit()
        assert interval(backend, e1) == (10.0, 50.0)
        assert interval(backend, e2) == (50.0, INF)
        assert links(backend, e1) == (e2.encode(), None)
        assert links(backend, e2) == (None, e1.encode())


# -- two connections ---------------------------------------------------------------


class TestConcurrency:
    def _race(self, backend, calls):
        """Run ``calls`` (one per instance) from a barrier; returns the
        results in call order, re-raising any exception."""
        other = second_instance(backend)
        instances = [backend, other]
        barrier = threading.Barrier(len(calls))
        results: list[Any] = [None] * len(calls)
        errors: list[BaseException] = []

        def run(i, kwargs):
            try:
                barrier.wait(timeout=10)
                results[i] = supersede(instances[i], **kwargs)
            except BaseException as e:  # pragma: no cover - surfaced below
                errors.append(e)

        threads = [
            threading.Thread(target=run, args=(i, kwargs))
            for i, kwargs in enumerate(calls)
        ]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        finally:
            release(other)
        assert not errors, errors
        assert not any(t.is_alive() for t in threads)
        return results

    def test_two_connections_closing_one_incumbent_exactly_one_wins(self, backend):
        old, a, b = save(backend, "old", "a", "b")
        supersede(backend, new_member=old, valid_from=10.0)
        results = self._race(
            backend,
            [
                dict(mode="supersede", new_member=a, old_member=old, now=51.0),
                dict(mode="supersede", new_member=b, old_member=old, now=52.0),
            ],
        )
        assert sorted(results, key=str) == [None, old]
        winner, loser = (a, b) if results[0] == old else (b, a)
        close_at = 51.0 if winner == a else 52.0
        assert interval(backend, old) == (10.0, close_at)
        assert links(backend, old) == (winner.encode(), None)
        assert links(backend, winner) == (None, old.encode())
        assert links(backend, loser) == (None, None)
        assert interval(backend, a) == (51.0, INF)
        assert interval(backend, b) == (52.0, INF)

    def test_two_connections_on_one_identity_serialise_into_a_chain(self, backend):
        # Via the pointer there is no loser: the second writer reads the
        # first's newcomer as the incumbent, so the outcome is one of the two
        # serial orders and never a fork. One shared clock, as a batch has:
        # with distinct clocks the later-clocked writer running first would
        # make the other's close precede its start, a refusal on both legs.
        old, a, b = save(backend, "old", "a", "b")
        supersede(backend, new_member=old, valid_from=10.0, pointer_digest="d")
        results = self._race(
            backend,
            [
                dict(mode="supersede", new_member=a, now=51.0, pointer_digest="d"),
                dict(mode="supersede", new_member=b, now=51.0, pointer_digest="d"),
            ],
        )
        assert old in results
        first, second = (a, b) if results[0] == old else (b, a)
        assert results == ([old, first] if first == a else [first, old])
        assert interval(backend, old) == (10.0, 51.0)
        assert interval(backend, first) == (51.0, 51.0)
        assert interval(backend, second) == (51.0, INF)
        assert links(backend, old) == (first.encode(), None)
        assert links(backend, first) == (second.encode(), old.encode())
        assert links(backend, second) == (None, first.encode())
        assert pointer(backend, "d") == second

    # -- deterministic interleavings (Postgres leg) ----------------------------
    # A barrier race seldom lands two connections *inside* the function at
    # once, so it cannot see the locks. These hold the incumbent's
    # ``invalid_at`` row from a probe connection, start both writers, wait
    # until ``pg_stat_activity`` shows both blocked, release the row and
    # check the outcome is a serial one. Redis's single thread cannot
    # interleave at all, so the Redis leg skips (as #737's lost-update test
    # does) rather than pretending to assert anything.

    def _interleave(self, backend, calls):
        import psycopg

        (old,) = [member("old")]
        probe = psycopg.connect(backend.url)
        instances = [second_instance(backend) for _ in calls]
        results: list[Any] = [None] * len(calls)
        errors: list[BaseException] = []
        threads: list[threading.Thread] = []
        try:
            probe.execute(
                "SELECT score FROM popoto_sorted WHERE idx = %s AND member = %s "
                "FOR UPDATE",
                (IA, old),
            )

            def run(i: int, kwargs: dict) -> None:
                try:
                    results[i] = supersede(instances[i], **kwargs)
                except BaseException as e:  # pragma: no cover - surfaced below
                    errors.append(e)

            for i, kwargs in enumerate(calls):
                thread = threading.Thread(target=run, args=(i, kwargs))
                threads.append(thread)
                thread.start()
                self._wait_for_blocked(probe, i + 1)
            probe.rollback()
            for thread in threads:
                thread.join(timeout=30)
        finally:
            probe.close()
            for instance in instances:
                release(instance)
        assert not errors, errors
        assert not any(t.is_alive() for t in threads)
        return results

    @staticmethod
    def _wait_for_blocked(probe: Any, count: int) -> None:
        import time

        deadline = time.monotonic() + 10.0
        activity = (
            "SELECT pid, wait_event_type, wait_event, query FROM pg_stat_activity"
        )
        while time.monotonic() < deadline:
            # The view is snapshotted on first access within a transaction,
            # and the probe is inside one (holding the row), so each poll
            # must discard the previous snapshot or it never sees the writer.
            probe.execute("SELECT pg_stat_clear_snapshot()")
            rows = probe.execute(activity).fetchall()
            blocked = [
                row
                for row in rows
                if row[1] == "Lock" and "popoto_supersede" in (row[3] or "")
            ]
            if len(blocked) >= count:
                return
            time.sleep(0.02)
        raise AssertionError(
            f"{count} writer(s) never blocked on the held row; activity: {rows}"
        )

    def test_pointer_writers_blocked_on_the_incumbent_serialise_into_a_chain(
        self, backend, backend_is_redis
    ):
        if backend_is_redis:
            pytest.skip("Postgres-leg assertion: Redis cannot interleave a script")
        old, a, b = save(backend, "old", "a", "b")
        supersede(backend, new_member=old, valid_from=10.0, pointer_digest="d")
        results = self._interleave(
            backend,
            [
                dict(mode="supersede", new_member=a, now=51.0, pointer_digest="d"),
                dict(mode="supersede", new_member=b, now=51.0, pointer_digest="d"),
            ],
        )
        # The first writer in closes old; the second, released after it,
        # must read its newcomer as the incumbent -- not the stale pointer.
        assert results == [old, a]
        assert interval(backend, old) == (10.0, 51.0)
        assert interval(backend, a) == (51.0, 51.0)
        assert interval(backend, b) == (51.0, INF)
        assert links(backend, old) == (a.encode(), None)
        assert links(backend, a) == (b.encode(), old.encode())
        assert links(backend, b) == (None, a.encode())
        assert pointer(backend, "d") == b

    def test_a_pointer_writer_and_an_explicit_writer_blocked_on_one_incumbent(
        self, backend, backend_is_redis
    ):
        if backend_is_redis:
            pytest.skip("Postgres-leg assertion: Redis cannot interleave a script")
        old, a, b = save(backend, "old", "a", "b")
        supersede(backend, new_member=old, valid_from=10.0, pointer_digest="d")
        results = self._interleave(
            backend,
            [
                dict(mode="supersede", new_member=a, now=51.0, pointer_digest="d"),
                dict(mode="supersede", new_member=b, old_member=old, now=52.0),
            ],
        )
        # Exactly one close: the explicit writer, released second, re-reads
        # the row it waited on and finds the incumbent already closed.
        assert results == [old, None]
        assert interval(backend, old) == (10.0, 51.0)
        assert links(backend, old) == (a.encode(), None)
        assert links(backend, a) == (None, old.encode())
        assert links(backend, b) == (None, None)
        assert interval(backend, b) == (52.0, INF)
        assert pointer(backend, "d") == a

    # -- crossing chains (review B1 of #750) -----------------------------------
    # ``d1 -> X`` superseded by ``Y`` while ``d2 -> Y`` is superseded by ``X``:
    # two pointer-resolved incumbents, each the other's successor. Redis's
    # single thread completes both in some order; the first closes its
    # incumbent and repoints, the second finds its successor already closed
    # and closes its own incumbent without a repoint. Before the prefix lock
    # the two writers' advisory sets were disjoint, both entered the function,
    # and each NX insert on the *successor's* ``invalid_at`` row waited on the
    # other's uncommitted close: ``DeadlockDetected`` on one of them.

    def _crossing_outcome(self, backend, x, y, results):
        assert sorted(results, key=str) == sorted([x, y], key=str), results
        _, x_close = interval(backend, x)
        _, y_close = interval(backend, y)
        assert x_close == 51.0 and y_close == 51.0
        assert links(backend, x) == (y.encode(), y.encode())
        assert links(backend, y) == (x.encode(), x.encode())
        # Each writer returns its own incumbent whichever ran first, so the
        # order shows only in the pointers: the first writer repointed to its
        # successor, the second found its successor already closed and left
        # its pointer alone -- both name the first writer's successor.
        assert pointer(backend, "d1") == pointer(backend, "d2")
        assert pointer(backend, "d1") in (x, y)

    def _interleave_held(self, backend, held, calls):
        """Like :meth:`_interleave`, holding every ``invalid_at`` row in ``held``."""
        import psycopg

        probe = psycopg.connect(backend.url)
        instances = [second_instance(backend) for _ in calls]
        results: list[Any] = [None] * len(calls)
        errors: list[BaseException] = []
        threads: list[threading.Thread] = []
        try:
            for m in held:
                probe.execute(
                    "SELECT score FROM popoto_sorted WHERE idx = %s AND member = %s "
                    "FOR UPDATE",
                    (IA, m),
                )

            def run(i: int, kwargs: dict) -> None:
                try:
                    results[i] = supersede(instances[i], **kwargs)
                except BaseException as e:
                    errors.append(e)

            for i, kwargs in enumerate(calls):
                thread = threading.Thread(target=run, args=(i, kwargs))
                threads.append(thread)
                thread.start()
                self._wait_for_blocked(probe, i + 1)
            probe.rollback()
            for thread in threads:
                thread.join(timeout=30)
        finally:
            probe.close()
            for instance in instances:
                release(instance)
        assert not errors, errors
        assert not any(t.is_alive() for t in threads)
        return results

    def test_crossing_pointer_chains_both_complete_without_a_deadlock(
        self, backend, backend_is_redis
    ):
        for i in range(10):
            x, y = save(backend, f"x{i}", f"y{i}")
            supersede(backend, new_member=x, valid_from=10.0, pointer_digest="d1")
            supersede(backend, new_member=y, valid_from=10.0, pointer_digest="d2")
            calls = [
                dict(mode="supersede", new_member=y, now=51.0, pointer_digest="d1"),
                dict(mode="supersede", new_member=x, now=51.0, pointer_digest="d2"),
            ]
            if backend_is_redis:
                # No interleaving to force: the single thread is the oracle.
                results = self._race(backend, calls)
            else:
                results = self._interleave_held(backend, [x, y], calls)
            self._crossing_outcome(backend, x, y, results)
