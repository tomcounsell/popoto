"""Index-family conformance: D side maps, E set indexes, F sorted indexes and
J's ``scan_index_members`` / ``drop_index`` (#631 WS3b).

Every test runs on every configured backend with the *same* assertions:
``RedisBackend`` is the oracle (its bodies are today's code, moved verbatim in
WS0), so whatever it returns is what ``PostgresBackend`` must return. Where an
expectation is computed rather than spelled out -- the glob battery, the
``ZRANGE`` index arithmetic, the property test -- it comes from a Python port
of the Redis rule (:func:`redis_glob_match` is ``stringmatchlen``;
:class:`ReferenceSortedSet` is a sorted set ordered by ``(score, member
bytes)``), and the Redis leg re-validates the port on every run while the
Postgres leg is held to it. The property test on the Postgres leg also
compares against the live ``RedisBackend`` directly.

The one documented difference is in :class:`TestScanDeviations`: Redis globs
match *bytes* while Postgres regexes match *characters*, so ``?`` and ``[...]``
disagree on a non-ASCII name. ``*`` -- the only glob popoto's callers use --
agrees everywhere.

Never touches database 0 or schema ``public``: the Redis leg goes through the
plugin-bound client and the Postgres leg through the harness's own schema.
"""

from __future__ import annotations

import math
import random
from typing import Any

import pytest

from popoto.backends.redis import RedisBackend

pytestmark = pytest.mark.conformance

PREFIX = "popoto_test:631:indexes"
SET_A = f"{PREFIX}:_tag:a"
SET_B = f"{PREFIX}:_tag:b"
SET_C = f"{PREFIX}:_tag:c"
ZSET = f"{PREFIX}:_score"
ZSET_2 = f"{PREFIX}:_score:acme"
MAP = f"{PREFIX}:_conf"
CLASS_SET = f"$Class:{PREFIX}:Record"
INF = float("inf")


def m(suffix: Any) -> str:
    return f"{PREFIX}:Record:{suffix}"


# -- Reference rules (ported from Redis) ----------------------------------------


def redis_glob_match(pattern: bytes, string: bytes) -> bool:
    """A direct port of Redis's ``stringmatchlen`` (``util.c``), byte for
    byte and without case folding, as ``SCAN MATCH`` / ``HSCAN MATCH`` use it.
    """
    p, s = 0, 0
    while p < len(pattern) and s < len(string):
        c = pattern[p : p + 1]
        if c == b"*":
            while p + 1 < len(pattern) and pattern[p + 1 : p + 2] == b"*":
                p += 1
            if p + 1 == len(pattern):
                return True
            for start in range(s, len(string) + 1):
                if redis_glob_match(pattern[p + 1 :], string[start:]):
                    return True
            return False
        if c == b"?":
            s += 1
        elif c == b"[":
            p += 1
            negate = pattern[p : p + 1] == b"^"
            if negate:
                p += 1
            match = False
            while True:
                if pattern[p : p + 1] == b"\\" and len(pattern) - p >= 2:
                    p += 1
                    if pattern[p] == string[s]:
                        match = True
                elif pattern[p : p + 1] == b"]":
                    break
                elif p >= len(pattern):
                    p -= 1
                    break
                elif len(pattern) - p >= 3 and pattern[p + 1 : p + 2] == b"-":
                    lo, hi = pattern[p], pattern[p + 2]
                    if lo > hi:
                        lo, hi = hi, lo
                    p += 2
                    if lo <= string[s] <= hi:
                        match = True
                elif pattern[p] == string[s]:
                    match = True
                p += 1
            if negate:
                match = not match
            if not match:
                return False
            s += 1
        else:
            if c == b"\\" and len(pattern) - p >= 2:
                p += 1
            if pattern[p] != string[s]:
                return False
            s += 1
        p += 1
        if s == len(string):
            while pattern[p : p + 1] == b"*":
                p += 1
            break
    return p == len(pattern) and s == len(string)


def redis_window(n: int, start: int, stop: int) -> range:
    """``ZRANGE``'s index arithmetic (``zrangeGenericCommand``)."""
    if start < 0:
        start = n + start
    if stop < 0:
        stop = n + stop
    if start < 0:
        start = 0
    if start > stop or start >= n:
        return range(0)
    if stop >= n:
        stop = n - 1
    return range(start, stop + 1)


class ReferenceSortedSet:
    """A sorted set with Redis's order: by score, ties by member bytes."""

    def __init__(self) -> None:
        self.scores: dict[str, float] = {}

    def add(self, member: str, score: float) -> int:
        if math.isnan(score):
            raise ValueError("value is not a valid float")
        new = member not in self.scores
        self.scores[member] = float(score)
        return int(new)

    def remove(self, member: str) -> int:
        return int(self.scores.pop(member, None) is not None)

    def incr(self, member: str, delta: float) -> float:
        score = self.scores.get(member, 0.0) + delta
        if math.isnan(score):
            raise ValueError("resulting score is not a number (NaN)")
        self.scores[member] = score
        return score

    def ordered(self, *, reverse: bool = False) -> list[str]:
        items = sorted(self.scores.items(), key=lambda kv: (kv[1], kv[0].encode()))
        members = [member for member, _ in items]
        return members[::-1] if reverse else members

    def members(self, start: int, stop: int, *, reverse: bool = False) -> list[str]:
        ordered = self.ordered(reverse=reverse)
        return [ordered[i] for i in redis_window(len(ordered), start, stop)]

    def range(
        self,
        lo: float,
        hi: float,
        *,
        lo_inclusive: bool = True,
        hi_inclusive: bool = True,
        reverse: bool = False,
        limit: int | None = None,
    ) -> list[str]:
        def inside(score: float) -> bool:
            above = score >= lo if lo_inclusive else score > lo
            below = score <= hi if hi_inclusive else score < hi
            return above and below

        picked = [
            member
            for member in self.ordered(reverse=reverse)
            if inside(self.scores[member])
        ]
        if isinstance(limit, int) and limit > 0:
            picked = picked[:limit]
        return picked


# -- E. Set indexes ---------------------------------------------------------------


class TestSetIndexes:
    def test_add_and_remove_reply_counts(self, backend):
        assert backend.index_add(SET_A, m(1)) == 1
        assert backend.index_add(SET_A, m(1)) == 0
        assert backend.index_add(SET_A, m(2)) == 1
        assert backend.index_members(SET_A) == {m(1), m(2)}
        assert backend.index_remove(SET_A, m(1)) == 1
        assert backend.index_remove(SET_A, m(1)) == 0
        assert backend.index_remove(SET_A, m("never")) == 0
        assert backend.index_members(SET_A) == {m(2)}

    def test_empty_index_reads_are_empty_sets_not_none(self, backend):
        assert backend.index_members(SET_A) == set()
        assert backend.index_union([SET_A, SET_B]) == set()
        assert backend.index_intersection([SET_A, SET_B]) == set()
        assert backend.index_union([]) == set()
        assert backend.index_intersection([]) == set()
        backend.index_add(SET_A, m(1))
        backend.index_remove(SET_A, m(1))
        assert backend.index_members(SET_A) == set()

    def test_union_and_intersection(self, backend):
        for member in (1, 2, 3):
            backend.index_add(SET_A, m(member))
        for member in (2, 3, 4):
            backend.index_add(SET_B, m(member))
        backend.index_add(SET_C, m(3))
        assert backend.index_union([SET_A, SET_B]) == {m(1), m(2), m(3), m(4)}
        assert backend.index_union([SET_A, f"{PREFIX}:_tag:none"]) == {
            m(1),
            m(2),
            m(3),
        }
        assert backend.index_intersection([SET_A, SET_B]) == {m(2), m(3)}
        assert backend.index_intersection([SET_A, SET_B, SET_C]) == {m(3)}
        assert backend.index_intersection([SET_A, SET_A]) == {m(1), m(2), m(3)}
        assert backend.index_intersection([SET_A, f"{PREFIX}:_tag:none"]) == set()
        assert all(isinstance(x, str) for x in backend.index_union([SET_A, SET_B]))

    def test_queued_on_a_unit_of_work(self, backend):
        backend.index_add(SET_A, m("old"))
        uow = backend.begin()
        assert backend.index_add(SET_A, m(1), uow=uow) is None
        assert backend.index_remove(SET_A, m("old"), uow=uow) is None
        assert backend.index_members(SET_A) == {m("old")}, "nothing before commit()"
        results = uow.commit()
        assert isinstance(results, list) and len(results) == 2
        assert backend.index_members(SET_A) == {m(1)}

    def test_abandoned_unit_of_work_writes_nothing(self, backend):
        with backend.begin() as uow:
            backend.index_add(SET_A, m(1), uow=uow)
        assert backend.index_members(SET_A) == set()


# -- E. Scans -----------------------------------------------------------------------

#: Index names seeded for the glob battery (all ASCII, see TestScanDeviations).
SCAN_SET_NAMES = [
    f"{PREFIX}:_tag:a",
    f"{PREFIX}:_tag:b",
    f"{PREFIX}:_tag:c",
    f"{PREFIX}:_tag:d",
    f"{PREFIX}:_tag:ab",
    f"{PREFIX}:_tag:*",
    f"{PREFIX}:_tag:a.b",
    f"{PREFIX}:_tag:a:b",
    f"{PREFIX}:_tag:-",
    f"{PREFIX}:_tag:",
]
SCAN_SORTED_NAMES = [f"{PREFIX}:_score", f"{PREFIX}:_score:acme", f"{PREFIX}:_scores"]
SCAN_MAP_NAMES = [f"{PREFIX}:_conf", f"{PREFIX}:_conf:acme"]
SCAN_PATTERNS = [
    f"{PREFIX}:*",
    f"{PREFIX}:_tag:*",
    f"{PREFIX}:_tag:?",
    f"{PREFIX}:_tag:??",
    f"{PREFIX}:_tag:[ab]",
    f"{PREFIX}:_tag:[^ab]",
    f"{PREFIX}:_tag:[^ab]*",
    f"{PREFIX}:_tag:[a-c]",
    f"{PREFIX}:_tag:[c-a]",
    f"{PREFIX}:_tag:[\\*]",
    f"{PREFIX}:_tag:\\*",
    f"{PREFIX}:_tag:a*",
    f"{PREFIX}:_tag:a?b",
    f"{PREFIX}:_tag:a.b",
    f"{PREFIX}:_tag:a[.:]b",
    f"{PREFIX}:_tag:[-]",
    f"{PREFIX}:_tag:[ab",
    f"{PREFIX}:_tag:[]",
    f"{PREFIX}:_tag:**",
    f"{PREFIX}:_score*",
    f"{PREFIX}:_score:*",
    f"{PREFIX}:_*:acme",
    f"{PREFIX}:_conf",
    f"{PREFIX}:_tag:zzz*",
    f"{PREFIX}:_tag:a",
    "*",
]


class TestScans:
    @staticmethod
    def seed_names(backend) -> list[str]:
        for name in SCAN_SET_NAMES:
            backend.index_add(name, m(1))
        for name in SCAN_SORTED_NAMES:
            backend.sorted_add(name, m(1), 1.0)
        for name in SCAN_MAP_NAMES:
            backend.map_set(name, m(1), b"\x01")
        return SCAN_SET_NAMES + SCAN_SORTED_NAMES + SCAN_MAP_NAMES

    @pytest.mark.parametrize("pattern", SCAN_PATTERNS)
    def test_scan_index_names_matches_the_redis_glob(self, backend, pattern):
        names = self.seed_names(backend)
        expected = {
            name for name in names if redis_glob_match(pattern.encode(), name.encode())
        }
        got = backend.scan_index_names(pattern)
        # Only this test's names are compared: on Redis the scan sees every
        # key in the database, and "*" would also list what the plugin keeps.
        assert {name for name in got if name.startswith(PREFIX)} == expected
        assert len(got) == len(set(got)), "no duplicates"
        assert all(isinstance(name, str) for name in got)

    def test_scan_index_names_on_an_empty_store_is_empty(self, backend):
        assert backend.scan_index_names(f"{PREFIX}:*") == []

    def test_scan_record_keys_sees_only_records(self, backend):
        backend.save_record(m(1), {b"a": b"1"}, class_set=CLASS_SET)
        backend.save_record(m(2), {b"a": b"1"}, class_set=CLASS_SET)
        backend.save_record(m("x:y"), {b"a": b"1"}, class_set=CLASS_SET)
        # Non-record keys sharing the glob: a set and a sorted set named like
        # a record (a legacy pointer side key has this shape on Redis). A side
        # *map* is left out on purpose: on Redis it is a hash, which is what
        # the TYPE filter keeps, so the legs would legitimately differ.
        backend.index_add(m("$ptr"), m(1))
        backend.sorted_add(m("$zptr"), m(1), 1.0)
        got = backend.scan_record_keys(f"{PREFIX}:Record:*")
        assert sorted(got) == sorted([m(1), m(2), m("x:y")])
        assert sorted(backend.scan_record_keys(f"{PREFIX}:Record:?")) == [m(1), m(2)]
        assert backend.scan_record_keys(f"{PREFIX}:nothing:*") == []
        assert all(isinstance(key, str) for key in got)


class TestScanDeviations:
    def test_single_character_globs_match_a_byte_on_redis_and_a_character_on_postgres(
        self, backend, backend_is_redis
    ):
        """Documented in ``docs/features/postgres-backend.md``: ``é`` is two
        bytes to Redis and one character to Postgres. ``*`` agrees."""
        name = f"{PREFIX}:_tag:é"
        backend.index_add(name, m(1))
        assert backend.scan_index_names(f"{PREFIX}:_tag:*") == [name]
        single = backend.scan_index_names(f"{PREFIX}:_tag:?")
        double = backend.scan_index_names(f"{PREFIX}:_tag:??")
        if backend_is_redis:
            assert (single, double) == ([], [name])
        else:
            assert (single, double) == ([name], [])


# -- F. Sorted indexes --------------------------------------------------------------

#: Same score, so the order is decided by member bytes alone. Redis compares
#: with ``memcmp``; Postgres must say ``COLLATE "C"`` to agree (the default
#: collation puts ``B`` between ``a`` and ``b``).
TIED = ["b", "a", "B", "aa", "a:1", "a-1", "é", "Z", "_", "0", "~", "a b"]
TIED_IN_BYTE_ORDER = sorted(TIED, key=str.encode)


class TestSortedIndexes:
    def test_add_returns_one_for_new_and_zero_for_a_score_update(self, backend):
        assert backend.sorted_add(ZSET, m(1), 1.0) == 1
        assert backend.sorted_score(ZSET, m(1)) == 1.0
        assert backend.sorted_add(ZSET, m(1), 2.5) == 0
        assert backend.sorted_score(ZSET, m(1)) == 2.5
        assert backend.sorted_add(ZSET, m(1), 2.5) == 0
        assert backend.sorted_count(ZSET) == 1
        assert isinstance(backend.sorted_score(ZSET, m(1)), float)

    def test_remove_reply_counts(self, backend):
        backend.sorted_add(ZSET, m(1), 1.0)
        assert backend.sorted_remove(ZSET, m(1)) == 1
        assert backend.sorted_remove(ZSET, m(1)) == 0
        assert backend.sorted_remove(ZSET, m("never")) == 0
        assert backend.sorted_count(ZSET) == 0

    def test_empty_index_reads(self, backend):
        assert backend.sorted_score(ZSET, m(1)) is None
        assert backend.sorted_count(ZSET) == 0
        assert backend.sorted_members(ZSET) == []
        assert backend.sorted_members(ZSET, 0, -1, reverse=True) == []
        assert backend.sorted_range(ZSET, -INF, INF) == []
        backend.sorted_add(ZSET, m(1), 1.0)
        assert backend.sorted_score(ZSET, m("other")) is None

    def test_special_scores_round_trip(self, backend):
        for suffix, score in (
            ("inf", INF),
            ("ninf", -INF),
            ("neg0", -0.0),
            ("tiny", 5e-324),
        ):
            backend.sorted_add(ZSET, m(suffix), score)
            assert backend.sorted_score(ZSET, m(suffix)) == score
        assert backend.sorted_score(ZSET, m("tiny")) == 5e-324
        assert backend.sorted_members(ZSET) == [
            m("ninf"),
            m("neg0"),
            m("tiny"),
            m("inf"),
        ]

    def test_members_orders_ties_by_member_bytes(self, backend):
        for member in TIED:
            backend.sorted_add(ZSET, member, 1.0)
        backend.sorted_add(ZSET, "first", 0.5)
        backend.sorted_add(ZSET, "last", 3.0)
        assert backend.sorted_members(ZSET) == ["first"] + TIED_IN_BYTE_ORDER + ["last"]
        assert backend.sorted_members(ZSET, reverse=True) == (
            ["last"] + TIED_IN_BYTE_ORDER[::-1] + ["first"]
        )
        assert backend.sorted_range(ZSET, 1.0, 1.0) == TIED_IN_BYTE_ORDER
        assert backend.sorted_range(ZSET, 1.0, 1.0, reverse=True) == (
            TIED_IN_BYTE_ORDER[::-1]
        )

    @pytest.mark.parametrize(
        ("start", "stop"),
        [
            (0, -1),
            (0, 0),
            (0, 2),
            (1, 3),
            (-3, -1),
            (-1, -1),
            (-1, -3),
            (-100, 1),
            (2, 100),
            (5, 2),
            (5, 5),
            (6, 6),
            (-6, -6),
            (-7, -7),
            (-7, 0),
            (4, -1),
            (4, -2),
            (3, -3),
            (0, -6),
            (0, -7),
        ],
    )
    @pytest.mark.parametrize("reverse", [False, True])
    def test_members_window_arithmetic(self, backend, start, stop, reverse):
        members = {"e": 5.0, "a": 1.0, "c": 2.0, "b": 2.0, "f": 5.0, "d": 3.5}
        ref = ReferenceSortedSet()
        for member, score in members.items():
            backend.sorted_add(ZSET, member, score)
            ref.add(member, score)
        got = backend.sorted_members(ZSET, start, stop, reverse=reverse)
        assert got == ref.members(start, stop, reverse=reverse)

    def test_range_bounds_inclusive_exclusive_and_infinite(self, backend):
        scores = {"a": 1.0, "b": 2.0, "c": 2.0, "d": 3.0, "e": INF, "f": -INF}
        for member, score in scores.items():
            backend.sorted_add(ZSET, member, score)
        assert backend.sorted_range(ZSET, 2.0, 3.0) == ["b", "c", "d"]
        assert backend.sorted_range(ZSET, 2.0, 3.0, lo_inclusive=False) == ["d"]
        assert backend.sorted_range(ZSET, 2.0, 3.0, hi_inclusive=False) == ["b", "c"]
        assert (
            backend.sorted_range(ZSET, 2, 3, lo_inclusive=False, hi_inclusive=False)
            == []
        )
        assert backend.sorted_range(ZSET, 2.0, 2.0, lo_inclusive=False) == []
        assert backend.sorted_range(ZSET, -INF, INF) == ["f", "a", "b", "c", "d", "e"]
        assert backend.sorted_range(ZSET, -INF, INF, lo_inclusive=False) == [
            "a",
            "b",
            "c",
            "d",
            "e",
        ]
        assert backend.sorted_range(ZSET, -INF, INF, hi_inclusive=False) == [
            "f",
            "a",
            "b",
            "c",
            "d",
        ]
        assert backend.sorted_range(ZSET, 3.0, 1.0) == []
        assert backend.sorted_range(ZSET, 1.5, 1.9) == []
        assert backend.sorted_range(ZSET, INF, INF) == ["e"]
        assert backend.sorted_range(ZSET, -INF, -INF) == ["f"]

    def test_range_reverse_and_limit(self, backend):
        scores = {"a": 1.0, "b": 2.0, "c": 2.0, "d": 3.0}
        for member, score in scores.items():
            backend.sorted_add(ZSET, member, score)
        assert backend.sorted_range(ZSET, 1.0, 3.0, reverse=True) == [
            "d",
            "c",
            "b",
            "a",
        ]
        assert backend.sorted_range(ZSET, 1.0, 3.0, limit=2) == ["a", "b"]
        assert backend.sorted_range(ZSET, 1.0, 3.0, reverse=True, limit=2) == ["d", "c"]
        assert backend.sorted_range(ZSET, 1.0, 3.0, limit=100) == ["a", "b", "c", "d"]
        # Non-positive limits mean "unbounded" in the Redis backend.
        assert backend.sorted_range(ZSET, 1.0, 3.0, limit=0) == ["a", "b", "c", "d"]
        assert backend.sorted_range(ZSET, 1.0, 3.0, limit=-1) == ["a", "b", "c", "d"]
        assert backend.sorted_range(ZSET, 1.0, 3.0, limit=None) == ["a", "b", "c", "d"]
        assert backend.sorted_range(
            ZSET, 2.0, 3.0, lo_inclusive=False, reverse=True, limit=1
        ) == ["d"]

    def test_increment_creates_updates_and_returns_the_new_score(self, backend):
        got = backend.sorted_increment(ZSET, m(1), 2.5)
        assert got == 2.5 and isinstance(got, float)
        assert backend.sorted_increment(ZSET, m(1), -1) == 1.5
        assert backend.sorted_increment(ZSET, m(1), 0.1) == 1.5 + 0.1
        assert backend.sorted_score(ZSET, m(1)) == 1.5 + 0.1
        assert backend.sorted_increment(ZSET, m(1), INF) == INF
        assert backend.sorted_count(ZSET) == 1

    def test_nan_scores_are_refused_and_change_nothing(self, backend):
        backend.sorted_add(ZSET, m("inf"), INF)
        with pytest.raises(Exception):
            backend.sorted_add(ZSET, m("nan"), float("nan"))
        with pytest.raises(Exception):
            backend.sorted_increment(ZSET, m("inf"), -INF)
        with pytest.raises(Exception):
            backend.sorted_increment(ZSET, m("inf"), float("nan"))
        assert backend.sorted_members(ZSET) == [m("inf")]
        assert backend.sorted_score(ZSET, m("inf")) == INF

    def test_queued_on_a_unit_of_work(self, backend):
        backend.sorted_add(ZSET, m("old"), 1.0)
        uow = backend.begin()
        assert backend.sorted_add(ZSET, m(1), 1.0, uow=uow) is None
        assert backend.sorted_increment(ZSET, m(1), 2.0, uow=uow) is None
        assert backend.sorted_remove(ZSET, m("old"), uow=uow) is None
        assert backend.sorted_members(ZSET) == [m("old")], "nothing before commit()"
        results = uow.commit()
        assert isinstance(results, list) and len(results) == 3
        assert backend.sorted_members(ZSET) == [m(1)]
        assert backend.sorted_score(ZSET, m(1)) == 3.0

    def test_indexes_are_independent(self, backend):
        backend.sorted_add(ZSET, m(1), 1.0)
        backend.sorted_add(ZSET_2, m(1), 9.0)
        assert backend.sorted_score(ZSET, m(1)) == 1.0
        assert backend.sorted_score(ZSET_2, m(1)) == 9.0
        backend.sorted_remove(ZSET, m(1))
        assert backend.sorted_count(ZSET_2) == 1


# -- F. Property test ----------------------------------------------------------------

PROPERTY_MEMBERS = ["a", "A", "b", "B", "aa", "a:1", "a-1", "_", "0", "z", "é", "m:2"]
PROPERTY_SCORES = [-INF, -3.0, -0.5, 0.0, 1e-9, 0.5, 1.0, 1.5, 2.0, 2.0, 7.25, 1e6, INF]
PROPERTY_DELTAS = [-2.0, -0.5, 0.25, 1.0, 3.0]


class TestSortedProperty:
    def test_random_operation_sequences_match_the_reference_and_redis(
        self, backend, backend_is_redis
    ):
        """Seeded and deterministic: 80 random add / remove / increment
        operations over a member pool chosen for ties and byte-order
        traps, then 50 random bound queries (``sorted_range``), 50 random
        windows (``sorted_members``), every score and the count, all three
        compared against the reference. On the Postgres leg the same
        sequence is also run through the live ``RedisBackend`` and compared
        directly, so Redis is the oracle in both senses."""
        rng = random.Random(631)
        ref = ReferenceSortedSet()
        oracle = None if backend_is_redis else RedisBackend()
        oracle_idx = f"{ZSET}:oracle"

        def apply(op: str, member: str, value: float) -> None:
            if op == "add":
                got = backend.sorted_add(ZSET, member, value)
                expected = ref.add(member, value)
                if oracle is not None:
                    assert oracle.sorted_add(oracle_idx, member, value) == expected
            elif op == "remove":
                got = backend.sorted_remove(ZSET, member)
                expected = ref.remove(member)
                if oracle is not None:
                    assert oracle.sorted_remove(oracle_idx, member) == expected
            else:
                got = backend.sorted_increment(ZSET, member, value)
                expected = ref.incr(member, value)
                if oracle is not None:
                    assert (
                        oracle.sorted_increment(oracle_idx, member, value) == expected
                    )
            assert got == expected, (op, member, value)

        for _ in range(80):
            member = rng.choice(PROPERTY_MEMBERS)
            roll = rng.random()
            if roll < 0.55:
                apply("add", member, rng.choice(PROPERTY_SCORES))
            elif roll < 0.7:
                apply("remove", member, 0.0)
            elif not math.isinf(ref.scores.get(member, 0.0)):
                apply("incr", member, rng.choice(PROPERTY_DELTAS))

        def check(name: str, got: Any, expected: Any, from_oracle: Any) -> None:
            assert got == expected, name
            if oracle is not None:
                assert from_oracle == expected, f"{name} (reference vs Redis)"

        check(
            "count",
            backend.sorted_count(ZSET),
            len(ref.scores),
            oracle.sorted_count(oracle_idx) if oracle else None,
        )
        for member in PROPERTY_MEMBERS:
            check(
                f"score {member}",
                backend.sorted_score(ZSET, member),
                ref.scores.get(member),
                oracle.sorted_score(oracle_idx, member) if oracle else None,
            )
        check(
            "members",
            backend.sorted_members(ZSET),
            ref.ordered(),
            oracle.sorted_members(oracle_idx) if oracle else None,
        )

        bound_pool = PROPERTY_SCORES + [-1.0, 0.75, 1.25, 3.0, 4.0]
        for i in range(50):
            lo, hi = rng.choice(bound_pool), rng.choice(bound_pool)
            if rng.random() < 0.7 and lo > hi:
                lo, hi = hi, lo
            kwargs = dict(
                lo_inclusive=rng.random() < 0.6,
                hi_inclusive=rng.random() < 0.6,
                reverse=rng.random() < 0.4,
                limit=rng.choice([None, None, 0, 1, 2, 3, 5, 100]),
            )
            check(
                f"range #{i} {lo} {hi} {kwargs}",
                backend.sorted_range(ZSET, lo, hi, **kwargs),
                ref.range(lo, hi, **kwargs),
                oracle.sorted_range(oracle_idx, lo, hi, **kwargs) if oracle else None,
            )

        n = len(ref.scores)
        for i in range(50):
            start = rng.randint(-n - 3, n + 3)
            stop = rng.randint(-n - 3, n + 3)
            reverse = rng.random() < 0.5
            check(
                f"window #{i} {start} {stop} reverse={reverse}",
                backend.sorted_members(ZSET, start, stop, reverse=reverse),
                ref.members(start, stop, reverse=reverse),
                (
                    oracle.sorted_members(oracle_idx, start, stop, reverse=reverse)
                    if oracle
                    else None
                ),
            )


# -- D. Side maps ------------------------------------------------------------------


class TestMaps:
    def test_get_set_delete_replies_and_byte_round_trip(self, backend):
        payload = b"\x00\xff\xfe\x80msgpack"
        assert backend.map_get(MAP, m(1)) is None
        assert backend.map_set(MAP, m(1), payload) is True
        assert backend.map_get(MAP, m(1)) == payload
        assert isinstance(backend.map_get(MAP, m(1)), bytes)
        assert backend.map_set(MAP, m(1), b"v2") is False
        assert backend.map_get(MAP, m(1)) == b"v2"
        assert backend.map_delete(MAP, m(1)) == 1
        assert backend.map_delete(MAP, m(1)) == 0
        assert backend.map_get(MAP, m(1)) is None

    def test_only_if_absent_is_hsetnx(self, backend):
        assert backend.map_set(MAP, m(1), b"first", only_if_absent=True) is True
        assert backend.map_set(MAP, m(1), b"second", only_if_absent=True) is False
        assert backend.map_get(MAP, m(1)) == b"first"
        assert backend.map_set(MAP, m(1), b"third") is False
        assert backend.map_get(MAP, m(1)) == b"third"

    def test_scan_matches_the_glob_and_ignores_count(self, backend):
        entries = {m("a"): b"1", m("b"): b"2", m("ab"): b"3", "other": b"\x00"}
        for member, value in entries.items():
            backend.map_set(MAP, member, value)
        assert backend.map_scan(MAP) == entries
        assert backend.map_scan(MAP, "*") == entries
        assert backend.map_scan(MAP, count=1) == entries, "count is a batch hint"
        assert backend.map_scan(MAP, f"{PREFIX}:*", count=1) == {
            k: v for k, v in entries.items() if k != "other"
        }
        assert backend.map_scan(MAP, f"{PREFIX}:Record:?") == {
            m("a"): b"1",
            m("b"): b"2",
        }
        assert backend.map_scan(MAP, f"{PREFIX}:Record:a*") == {
            m("a"): b"1",
            m("ab"): b"3",
        }
        assert backend.map_scan(MAP, "nothing*") == {}
        assert backend.map_scan(f"{MAP}:missing") == {}
        got = backend.map_scan(MAP)
        assert all(isinstance(k, str) and isinstance(v, bytes) for k, v in got.items())

    def test_queued_on_a_unit_of_work(self, backend):
        backend.map_set(MAP, m("old"), b"o")
        uow = backend.begin()
        assert backend.map_set(MAP, m(1), b"v", uow=uow) is None
        assert backend.map_set(MAP, m(2), b"w", only_if_absent=True, uow=uow) is None
        assert backend.map_delete(MAP, m("old"), uow=uow) is None
        assert backend.map_scan(MAP) == {m("old"): b"o"}, "nothing before commit()"
        results = uow.commit()
        assert isinstance(results, list) and len(results) == 3
        assert backend.map_scan(MAP) == {m(1): b"v", m(2): b"w"}


# -- J. Maintenance ----------------------------------------------------------------


class TestMaintenance:
    def test_drop_index_replies_like_del(self, backend):
        backend.index_add(SET_A, m(1))
        backend.index_add(SET_A, m(2))
        backend.sorted_add(ZSET, m(1), 1.0)
        backend.map_set(MAP, m(1), b"v")
        assert backend.drop_index(SET_A, "set") == 1
        assert backend.drop_index(SET_A, "set") == 0
        assert backend.index_members(SET_A) == set()
        assert backend.drop_index(ZSET, "sorted") == 1
        assert backend.drop_index(ZSET, "sorted") == 0
        assert backend.sorted_count(ZSET) == 0
        assert backend.drop_index(MAP, "map") == 1
        assert backend.drop_index(MAP, "map") == 0
        assert backend.map_scan(MAP) == {}
        assert backend.drop_index(f"{SET_A}:never", "set") == 0

    def test_drop_index_touches_only_the_named_index(self, backend):
        backend.index_add(SET_A, m(1))
        backend.index_add(SET_B, m(1))
        backend.sorted_add(ZSET, m(1), 1.0)
        backend.sorted_add(ZSET_2, m(1), 1.0)
        assert backend.drop_index(SET_A, "set") == 1
        assert backend.index_members(SET_B) == {m(1)}
        assert backend.drop_index(ZSET, "sorted") == 1
        assert backend.sorted_members(ZSET_2) == [m(1)]

    def test_drop_index_queued_on_a_unit_of_work(self, backend):
        backend.index_add(SET_A, m(1))
        uow = backend.begin()
        assert backend.drop_index(SET_A, "set", uow=uow) is None
        assert backend.index_members(SET_A) == {m(1)}, "nothing before commit()"
        assert len(uow.commit()) == 1
        assert backend.index_members(SET_A) == set()

    def test_scan_index_members_yields_every_member_once(self, backend):
        for member in (1, 2, 3):
            backend.index_add(SET_A, m(member))
            backend.sorted_add(ZSET, m(member), float(member))
        got_set = list(backend.scan_index_members(SET_A, "set"))
        got_sorted = list(backend.scan_index_members(ZSET, "sorted"))
        assert sorted(got_set) == [m(1), m(2), m(3)]
        assert sorted(got_sorted) == [m(1), m(2), m(3)]
        assert all(isinstance(member, str) for member in got_set + got_sorted)

    def test_scan_index_members_on_a_missing_index_yields_nothing(self, backend):
        scan = backend.scan_index_members(f"{SET_A}:missing", "set")
        assert iter(scan) is scan, "an iterator, as the SSCAN loop is"
        assert list(scan) == []
        assert list(backend.scan_index_members(f"{ZSET}:missing", "sorted")) == []

    def test_scan_index_members_is_lazy(self, backend):
        scan = backend.scan_index_members(SET_A, "set")
        backend.index_add(SET_A, m(1))
        assert list(scan) == [m(1)], "nothing ran before the first next()"
