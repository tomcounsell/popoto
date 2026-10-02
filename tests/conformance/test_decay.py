"""Decay-and-confidence conformance: group H -- ``decayed_rank`` and
``confidence_update`` (#631 WS3d).

Every test runs on every configured backend with the *same* assertions:
``RedisBackend`` is the oracle (its two bodies are ``DECAY_SCORE_LUA`` and
``CAPPED_BAYESIAN_UPDATE_LUA``, moved verbatim in WS0), so whatever it
returns and stores -- the flat ``[member, score, ...]`` reply down to the
``%.14g`` rendering of each score, and the msgpack payload the confidence
update writes down to the byte -- is what ``PostgresBackend`` must return and
store. Two references hold the Redis leg itself to account: :func:`lua_score`
and :func:`lua_update` are the two scripts' arithmetic in Python, same
operation order, same libm ``pow``, and every score is checked against them
within :data:`REL_TOL`; the packed payload is checked against the Postgres
backend's own packer, so the Redis leg proves the packer reproduces cmsgpack
and the Postgres leg proves the backend uses it.

Floating point: the Lua computes in doubles and Postgres in ``double
precision``, and both call the platform's ``pow``. On one machine the two
agree bit for bit (``TestRandomParity`` counts the rendered scores that
differ and prints the count); across libms a last-ulp difference is possible,
so the *asserted* contract is identical order and scores within ``1e-9``
relative. Where no ``pow`` is involved (an elapsed time of exactly one day,
whose power is exactly 1) the reply bytes are asserted equal.

Data goes in through the protocol only: records through ``save_record``, the
decay index and the validity intervals through ``sorted_add``, confidence
payloads through ``map_set``. Never touches database 0 or schema ``public``.
"""

from __future__ import annotations

import math
import random
import threading
from typing import Any

import msgpack
import pytest

from popoto.backends.postgres import PostgresBackend, _pack_confidence
from popoto.backends.redis import RedisBackend, _validity_keys

pytestmark = pytest.mark.conformance

PREFIX = "popoto_test:631:decay"
CLASS_SET = f"$Class:{PREFIX}:Memory"
#: The decay field's sorted index (member -> last-seen epoch).
ZIDX = f"{PREFIX}:Memory:_last_seen"
#: The confidence field's ``:data`` companion map.
CONF = f"$ConfidencF:{PREFIX}:Memory:certainty:data"
MODEL = f"$ValidityF:{PREFIX}:Memory"
KEYS = _validity_keys(MODEL, "validity")
IA, VF = KEYS["invalid_at"], KEYS["valid_from"]

NOW = 1_700_000_000.0
DAY = 86400.0
INF = float("inf")
RATE = 0.5
C0 = 0.5
STRENGTH = 0.5
PRETRIM = 4.0
#: The stated float tolerance (relative), see the module docstring.
REL_TOL = 1e-9

INITIAL = 0.5
CAP = 20


# -- helpers -------------------------------------------------------------------


def member(suffix: Any) -> str:
    return f"{PREFIX}:Memory:{suffix}"


def save(backend: Any, suffix: Any, strength: Any = None) -> str:
    """A record; ``strength`` is the base-score field, packed the way the
    field layer packs it unless given as raw ``bytes``."""
    fields: dict[bytes, bytes] = {b"name": msgpack.packb(str(suffix))}
    if strength is not None:
        fields[b"strength"] = (
            strength if isinstance(strength, bytes) else msgpack.packb(strength)
        )
    backend.save_record(member(suffix), fields, class_set=CLASS_SET)
    return member(suffix)


def ts(age_days: float) -> float:
    """The last-seen timestamp of a member last touched ``age_days`` ago."""
    return NOW - age_days * DAY


def seed(backend: Any, ages: dict[Any, float], idx: str = ZIDX) -> None:
    for suffix, age in ages.items():
        backend.sorted_add(idx, member(suffix), ts(age))


def set_confidence(backend: Any, suffix: Any, payload: Any) -> None:
    raw = payload if isinstance(payload, bytes) else msgpack.packb(payload)
    backend.map_set(CONF, member(suffix), raw)


def rank(backend: Any, idx: str = ZIDX, **overrides: Any) -> list[Any]:
    kwargs: dict[str, Any] = dict(
        now=NOW,
        decay_rate=RATE,
        limit=None,
        base_score_field="",
        confidence=None,
        validity=None,
        pretrim_max_ratio=PRETRIM,
    )
    kwargs.update(overrides)
    return backend.decayed_rank(idx, **kwargs)


def decoded(reply: list[Any]) -> list[tuple[str, float]]:
    """The flat reply as the three callers decode it."""
    assert len(reply) % 2 == 0, reply
    assert all(isinstance(item, bytes) for item in reply), reply
    return [(reply[i].decode(), float(reply[i + 1])) for i in range(0, len(reply), 2)]


def members(reply: list[Any]) -> list[str]:
    return [m for m, _ in decoded(reply)]


def lua_tostring(value: float) -> str:
    """Lua 5.1's ``tostring`` on a number: ``%.14g``."""
    return "%.14g" % value


def lua_score(
    timestamp: float,
    *,
    base: float = 1.0,
    rate: float = RATE,
    now: float = NOW,
    c: float | None = None,
    s: float = 0.0,
    c0: float = C0,
) -> float:
    """``DECAY_SCORE_LUA``'s arithmetic, operation for operation. ``c`` is the
    member's confidence when modulation is on (``None`` = modulation off)."""
    elapsed = max((now - timestamp) / 86400, 0.01)
    sign = -1 if base < 0 else 1
    decayed = sign * abs(base) * math.pow(elapsed, -rate)
    if c is not None:
        cc = max(0, min(1, c))
        eff = rate * math.pow(2, s * 2 * (c0 - cc))
        decayed = decayed * math.pow(max(elapsed, 1.0), -(eff - rate))
    return decayed


def lua_update(
    state: tuple[float, int, int, int], signal: float, *, cap: int = CAP
) -> tuple[float, int, int, int]:
    """``CAPPED_BAYESIAN_UPDATE_LUA``'s arithmetic: the state it *stores*
    (the confidence is the raw double; only the reply is rounded)."""
    confidence, evidence, corroborations, contradictions = state
    n_eff = min(evidence + 1, cap)
    new = confidence + (signal - confidence) / (n_eff + 1)
    new = max(0, min(1, new))
    evidence += 1
    if signal >= 0.5:
        corroborations += 1
    else:
        contradictions += 1
    return (new, evidence, corroborations, contradictions)


def reply(state: tuple[float, int, int, int]) -> tuple[float, int, int, int]:
    """The script's ``tostring`` reply for ``state`` as the Redis backend
    parses it: ``%.14g`` then ``float`` / ``int``."""
    confidence, evidence, corroborations, contradictions = state
    return (
        float(lua_tostring(confidence)),
        int(float(lua_tostring(evidence))),
        int(float(lua_tostring(corroborations))),
        int(float(lua_tostring(contradictions))),
    )


def packed(state: tuple[float, int, int, int]) -> bytes:
    """What ``cmsgpack.pack(updated)`` stores for ``state`` -- the Postgres
    backend's packer, which the Redis leg holds to the Lua's bytes."""
    confidence, evidence, corroborations, contradictions = state
    return _pack_confidence(
        {
            "confidence": confidence,
            "evidence_count": evidence,
            "corroborations": corroborations,
            "contradictions": contradictions,
        }
    )


def stored(backend: Any, suffix: Any) -> dict[str, Any] | None:
    raw = backend.map_get(CONF, member(suffix))
    return None if raw is None else msgpack.unpackb(raw)


def close(got: float, want: float) -> bool:
    if math.isinf(want) or math.isnan(want):
        return got == want or (math.isnan(got) and math.isnan(want))
    return math.isclose(got, want, rel_tol=REL_TOL, abs_tol=1e-300)


def assert_scores(reply: list[Any], expected: list[tuple[str, float]]) -> None:
    """Identical order; each score within the stated tolerance."""
    got = decoded(reply)
    assert [m for m, _ in got] == [m for m, _ in expected]
    for (_, g), (m, w) in zip(got, expected):
        assert close(g, w), (m, g, w)


def oracle(backend: Any) -> Any:
    """A second instance of the same backend on a second connection: Redis
    pools, so two ``RedisBackend`` objects are two users of one client;
    Postgres opens one connection per instance."""
    if isinstance(backend, RedisBackend):
        return RedisBackend()
    assert isinstance(backend, PostgresBackend)
    return PostgresBackend(backend.url)


# -- decayed_rank: reply shape ------------------------------------------------


class TestReplyShape:
    def test_empty_index_is_an_empty_list(self, backend):
        assert rank(backend) == []
        assert rank(backend, limit=5) == []
        assert rank(backend, limit=0) == []
        assert rank(backend, confidence=(CONF, "0.5", "0.5")) == []
        assert rank(backend, validity=(IA, VF, NOW)) == []

    def test_flat_reply_of_bytes_with_lua_rendered_scores(self, backend):
        # One day old: pow(1.0, -rate) == 1.0 exactly, so the score is the
        # base score and the rendered bytes are asserted, not approximated.
        save(backend, "a", 2.5)
        seed(backend, {"a": 1.0})
        assert rank(backend) == [member("a").encode(), b"1"]
        assert rank(backend, base_score_field="strength") == [
            member("a").encode(),
            b"2.5",
        ]

    def test_scores_render_like_lua_tostring(self, backend):
        # 4 days at rate 0.5: 4^-0.5 == 0.5 exactly; 100 days: 100^-0.5.
        seed(backend, {"a": 4.0, "b": 100.0})
        reply = rank(backend)
        assert reply[1] == b"0.5"
        assert reply[3] == lua_tostring(math.pow(100.0, -0.5)).encode()
        assert reply[3] == b"0.1"

    def test_a_member_without_a_record_scores_at_base_one(self, backend):
        seed(backend, {"ghost": 1.0})
        assert rank(backend, base_score_field="strength") == [
            member("ghost").encode(),
            b"1",
        ]


# -- decayed_rank: the power law ------------------------------------------------


class TestPowerLaw:
    AGES = {"a": 0.5, "b": 1.0, "c": 2.0, "d": 7.0, "e": 30.0, "f": 365.0}

    def test_order_and_scores_follow_the_formula(self, backend):
        seed(backend, self.AGES)
        expected = sorted(
            ((member(k), lua_score(ts(age))) for k, age in self.AGES.items()),
            key=lambda pair: (-pair[1], pair[0]),
        )
        assert_scores(rank(backend), expected)
        assert members(rank(backend)) == [member(k) for k in "abcdef"]

    @pytest.mark.parametrize("rate", [0.1, 0.3, 1.0, 2.5])
    def test_decay_rate_is_the_calls(self, backend, rate):
        seed(backend, self.AGES)
        expected = sorted(
            (
                (member(k), lua_score(ts(age), rate=rate))
                for k, age in self.AGES.items()
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
        assert_scores(rank(backend, decay_rate=rate), expected)

    def test_now_is_the_calls(self, backend):
        seed(backend, self.AGES)
        later = NOW + 10 * DAY
        expected = sorted(
            (
                (member(k), lua_score(ts(age), now=later))
                for k, age in self.AGES.items()
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
        assert_scores(rank(backend, now=later), expected)

    def test_future_timestamps_floor_elapsed_at_a_hundredth_of_a_day(self, backend):
        # Both are "in the future": same floored elapsed, same score, so the
        # tie breaks on the member; and the score is the floor's.
        seed(backend, {"soon": -0.001, "later": -3000.0, "now": 0.0})
        reply = rank(backend)
        assert members(reply) == [member("later"), member("now"), member("soon")]
        floor = lua_tostring(math.pow(0.01, -RATE)).encode()
        assert reply[1] == reply[3] == reply[5] == floor

    def test_limit_truncates_after_sorting(self, backend):
        seed(backend, self.AGES)
        assert members(rank(backend, limit=2)) == [member("a"), member("b")]
        assert members(rank(backend, limit=1)) == [member("a")]
        assert members(rank(backend, limit=100)) == [member(k) for k in "abcdef"]
        assert rank(backend, limit=0) == []
        assert rank(backend, limit=-1) == []

    def test_ties_break_on_member_bytes_ascending(self, backend):
        names = ["B", "a", "aa", "a:1", "a-1", "_", "0", "~", "Z", "a a"]
        seed(backend, {name: 3.0 for name in names})
        expected = sorted(member(name).encode() for name in names)
        reply = rank(backend)
        assert reply[0::2] == expected
        assert len(set(reply[1::2])) == 1, "every score is the same"
        # Truncation respects the same order.
        assert rank(backend, limit=3)[0::2] == expected[:3]

    def test_a_second_index_is_not_scanned(self, backend):
        seed(backend, {"a": 1.0})
        seed(backend, {"b": 1.0}, idx=ZIDX + ":other")
        assert members(rank(backend)) == [member("a")]
        assert members(rank(backend, idx=ZIDX + ":other")) == [member("b")]


# -- decayed_rank: the base score ----------------------------------------------

DECIMAL = {"__Decimal__": True}
#: ``(stored strength payload, the base score the script reads from it)``.
#: Recorded by running the script's two rules on the Redis build under test
#: (``tests/conformance`` probe, 2026-10-03); the Redis leg re-validates every
#: row and the Postgres leg is held to it.
BASE_SCORE_CASES: list[tuple[str, Any, float]] = [
    ("float", 2.5, 2.5),
    ("float32-exact", 0.25, 0.25),
    ("tenth", 0.1, 0.1),
    ("int", 7, 7.0),
    ("negative-int", -7, -7.0),
    ("uint8", 200, 200.0),
    ("int8", -100, -100.0),
    ("uint16", 300, 300.0),
    ("int16", -300, -300.0),
    ("uint32", 70000, 70000.0),
    ("int32", -70000, -70000.0),
    ("int64", 2**40, float(2**40)),
    ("negative-int64", -(2**40), -float(2**40)),
    ("zero", 0, 0.0),
    ("negative-float", -2.5, -2.5),
    ("large", 1e300, 1e300),
    ("tiny", 5e-324, 5e-324),
    ("decimal-str", {**DECIMAL, "as_encodable": "2.75"}, 2.75),
    ("decimal-str-spaces", {**DECIMAL, "as_encodable": " 7.5 "}, 7.5),
    ("decimal-exp", {**DECIMAL, "as_encodable": "1e2"}, 100.0),
    ("decimal-number", {**DECIMAL, "as_encodable": 3}, 3.0),
    ("decimal-unparseable", {**DECIMAL, "as_encodable": "abc"}, 1.0),
    ("decimal-empty", {**DECIMAL, "as_encodable": ""}, 1.0),
    ("decimal-true", {**DECIMAL, "as_encodable": True}, 1.0),
    ("decimal-false", {**DECIMAL, "as_encodable": False}, 1.0),
    ("decimal-nil", {**DECIMAL, "as_encodable": None}, 1.0),
    ("string", "1.5", 1.0),
    ("bool", True, 1.0),
    ("nil", None, 1.0),
    ("array", [4.0], 1.0),
    ("map-without-tag", {"x": 1}, 1.0),
    (
        "nested-then-tag",
        {"z": [1, {"a": [None, True, "s"]}], "as_encodable": 4.5},
        4.5,
    ),
    ("empty-bytes", b"", 1.0),
    ("trailing-object", msgpack.packb(2.5) + msgpack.packb(7), 2.5),
    ("truncated", msgpack.packb(2.5)[:5], 1.0),
    ("garbage", b"\xc1garbage", 1.0),
    ("bin-is-bad-data", {"as_encodable": 4.5, "b": b"\x00"}, 1.0),
    ("ext-is-bad-data", b"\xd4\x01\x00", 1.0),
    ("uint64-as-signed", bytes.fromhex("cfffffffffffffffff"), -1.0),
    ("float32-tag", bytes.fromhex("ca3e800000"), 0.25),
    ("subnormal", bytes.fromhex("cb0000000000000001"), 5e-324),
    (
        "duplicate-key-last-wins",
        b"\x82"
        + msgpack.packb("as_encodable")
        + msgpack.packb("1")
        + msgpack.packb("as_encodable")
        + msgpack.packb("9"),
        9.0,
    ),
]


class TestBaseScore:
    @pytest.mark.parametrize(
        "payload,expected",
        [c[1:] for c in BASE_SCORE_CASES],
        ids=[c[0] for c in BASE_SCORE_CASES],
    )
    def test_the_scripts_two_rules(self, backend, payload, expected):
        # One day old, so the score *is* the base score, rendered exactly.
        save(backend, "m", payload)
        seed(backend, {"m": 1.0})
        reply = rank(backend, base_score_field="strength")
        assert reply == [member("m").encode(), lua_tostring(expected).encode()]

    def test_base_scales_the_curve_and_negative_sorts_last(self, backend):
        save(backend, "big", 3.0)
        save(backend, "small", 0.25)
        save(backend, "neg", -2.0)
        save(backend, "zero", 0)
        seed(backend, {"big": 30.0, "small": 2.0, "neg": 0.5, "zero": 1.0})
        expected = sorted(
            [
                (member("big"), lua_score(ts(30.0), base=3.0)),
                (member("small"), lua_score(ts(2.0), base=0.25)),
                (member("neg"), lua_score(ts(0.5), base=-2.0)),
                (member("zero"), lua_score(ts(1.0), base=0.0)),
            ],
            key=lambda pair: (-pair[1], pair[0]),
        )
        reply = rank(backend, base_score_field="strength")
        assert_scores(reply, expected)
        assert members(reply)[-1] == member("neg")
        assert reply[-1].startswith(b"-")

    def test_an_empty_field_name_ignores_the_record(self, backend):
        save(backend, "m", 2.5)
        seed(backend, {"m": 1.0})
        assert rank(backend, base_score_field="") == [member("m").encode(), b"1"]

    def test_a_field_the_record_lacks_is_base_one(self, backend):
        save(backend, "m", 2.5)
        seed(backend, {"m": 1.0})
        assert rank(backend, base_score_field="weight") == [member("m").encode(), b"1"]


# -- decayed_rank: confidence modulation ----------------------------------------


def lua_payload(state: tuple[float, int, int, int]) -> bytes:
    return packed(state)


#: ``(companion payload, the confidence the script reads, or None for c0)``.
CONFIDENCE_CASES: list[tuple[str, Any, float | None]] = [
    ("map", {"confidence": 0.7, "evidence_count": 1}, 0.7),
    ("lua-packed-map", lua_payload((0.3, 2, 1, 1)), 0.3),
    ("map-string-confidence", {"confidence": "0.7"}, None),
    ("map-false-confidence-then-index-1", {"confidence": False, 1: 0.2}, 0.2),
    ("array", [0.9, 2, 3, 4], 0.9),
    ("int-key-1", {1: 0.4}, 0.4),
    ("number", 0.3, None),
    ("empty-array", [], None),
    ("nested-first", [[1], 2], None),
    ("empty-bytes", b"", None),
    ("truncated", msgpack.packb({"confidence": 0.7})[:-2], None),
    ("garbage", b"\xc1garbage", None),
    ("bin-is-bad-data", {"confidence": 0.7, "b": b"\x00"}, None),
    ("above-one-clamps", {"confidence": 5.0}, 5.0),
    ("below-zero-clamps", {"confidence": -1.0}, -1.0),
    ("exactly-c0", {"confidence": C0}, C0),
    ("zero", {"confidence": 0}, 0.0),
    ("one", {"confidence": 1}, 1.0),
]


class TestConfidenceModulation:
    ON = (CONF, str(STRENGTH), str(C0))

    def test_neutral_confidence_is_bit_exact_with_modulation_off(self, backend):
        seed(backend, {"a": 10.0, "b": 0.5, "c": 400.0})
        set_confidence(backend, "a", {"confidence": C0})
        set_confidence(backend, "b", {"confidence": C0})
        # ``c`` has no payload: defaults to c0, equally neutral.
        assert rank(backend, confidence=self.ON) == rank(backend)

    def test_low_confidence_decays_faster_only_after_a_day(self, backend):
        seed(backend, {"low": 10.0, "high": 10.0, "fresh_low": 0.5, "fresh_high": 0.5})
        set_confidence(backend, "low", {"confidence": 0.1})
        set_confidence(backend, "high", {"confidence": 0.9})
        set_confidence(backend, "fresh_low", {"confidence": 0.1})
        set_confidence(backend, "fresh_high", {"confidence": 0.9})
        off = dict(decoded(rank(backend)))
        on = dict(decoded(rank(backend, confidence=self.ON)))
        assert on[member("low")] < off[member("low")] < on[member("high")]
        # The max(elapsed, 1.0) guard: no modulation inside the first day.
        assert on[member("fresh_low")] == off[member("fresh_low")]
        assert on[member("fresh_high")] == off[member("fresh_high")]
        assert members(rank(backend, confidence=self.ON))[:2] == [
            member("fresh_high"),
            member("fresh_low"),
        ]

    @pytest.mark.parametrize(
        "payload,c",
        [c[1:] for c in CONFIDENCE_CASES],
        ids=[c[0] for c in CONFIDENCE_CASES],
    )
    def test_the_payload_shapes_the_script_reads(self, backend, payload, c):
        seed(backend, {"m": 10.0})
        set_confidence(backend, "m", payload)
        want = lua_score(ts(10.0), c=C0 if c is None else c, s=STRENGTH)
        assert_scores(rank(backend, confidence=self.ON), [(member("m"), want)])

    @pytest.mark.parametrize(
        "triple",
        [
            ("", str(STRENGTH), str(C0)),  # no hash: off
            (CONF, "0", str(C0)),  # s == 0: off
            (CONF, "0.0", str(C0)),
            (CONF, "abc", str(C0)),  # tonumber(s) is nil -> 0: off
            (CONF, "", str(C0)),
        ],
        ids=["no-hash", "s-0", "s-0.0", "s-unparseable", "s-empty"],
    )
    def test_the_off_triples_are_byte_identical_to_off(self, backend, triple):
        seed(backend, {"a": 10.0, "b": 2.0})
        set_confidence(backend, "a", {"confidence": 0.1})
        assert rank(backend, confidence=triple) == rank(backend)

    def test_strength_and_c0_are_the_calls(self, backend):
        seed(backend, {"m": 10.0})
        set_confidence(backend, "m", {"confidence": 0.2})
        for s, c0 in [(1.0, 0.5), (0.5, 0.8), (2.0, 0.2), (0.25, 0.5)]:
            want = lua_score(ts(10.0), c=0.2, s=s, c0=c0)
            assert_scores(
                rank(backend, confidence=(CONF, str(s), str(c0))), [(member("m"), want)]
            )

    def test_an_unparseable_c0_defaults_to_a_half(self, backend):
        seed(backend, {"m": 10.0})
        set_confidence(backend, "m", {"confidence": 0.2})
        assert rank(backend, confidence=(CONF, "0.5", "abc")) == rank(
            backend, confidence=(CONF, "0.5", "0.5")
        )

    def test_modulation_composes_with_the_base_score(self, backend):
        save(backend, "m", 3.0)
        seed(backend, {"m": 10.0})
        set_confidence(backend, "m", {"confidence": 0.9})
        want = lua_score(ts(10.0), base=3.0, c=0.9, s=STRENGTH)
        assert_scores(
            rank(backend, base_score_field="strength", confidence=self.ON),
            [(member("m"), want)],
        )

    def test_the_float_triple_from_the_protocol_is_accepted(self, backend):
        # WS0 deviation 8: ``(str, float, float)`` renders through ``str()``.
        seed(backend, {"m": 10.0})
        set_confidence(backend, "m", {"confidence": 0.2})
        assert rank(backend, confidence=(CONF, STRENGTH, C0)) == rank(
            backend, confidence=self.ON
        )


# -- decayed_rank: the validity gate --------------------------------------------

#: ``(invalid_at, valid_from, as_of, included)`` -- the script's exclusion
#: rule: ``invalid_at <= as_of`` or ``valid_from > as_of`` excludes; absence
#: from an index includes.
GATE_CASES: list[tuple[str, float | None, float | None, float, bool]] = [
    ("unmanaged", None, None, NOW, True),
    ("closed-before", NOW - 1, None, NOW, False),
    ("closed-exactly-at", NOW, None, NOW, False),
    ("closes-after", NOW + 1, None, NOW, True),
    ("open-inf", INF, None, NOW, True),
    ("starts-after", None, NOW + 1, NOW, False),
    ("starts-exactly-at", None, NOW, NOW, True),
    ("started-before", None, NOW - 1, NOW, True),
    ("open-and-started", INF, NOW - 5, NOW, True),
    ("closed-and-started", NOW - 5, NOW - 10, NOW, False),
    ("open-but-future", INF, NOW + 5, NOW, False),
    ("malformed-both-clauses", NOW - 5, NOW + 5, NOW, False),
    ("as-of-inf-closes-open", INF, None, INF, False),
    ("as-of-inf-future-start", None, NOW, INF, True),
    ("as-of-minus-inf-start", None, NOW, -INF, False),
    ("as-of-minus-inf-close", NOW, None, -INF, True),
    ("as-of-1e308", NOW, None, 1e308, False),
]


class TestValidityGate:
    @pytest.mark.parametrize(
        "invalid_at,valid_from,as_of,included",
        [c[1:] for c in GATE_CASES],
        ids=[c[0] for c in GATE_CASES],
    )
    def test_the_exclusion_rule(self, backend, invalid_at, valid_from, as_of, included):
        seed(backend, {"g": 3.0, "control": 5.0})
        if invalid_at is not None:
            backend.sorted_add(IA, member("g"), invalid_at)
        if valid_from is not None:
            backend.sorted_add(VF, member("g"), valid_from)
        got = members(rank(backend, validity=(IA, VF, as_of)))
        assert member("control") in got, "an unmanaged member is never gated"
        assert (member("g") in got) is included

    @pytest.mark.parametrize(
        "triple",
        [None, ("", VF, NOW), (IA, "", NOW), ("", "", NOW)],
        ids=["none", "no-invalid-key", "no-valid-key", "no-keys"],
    )
    def test_the_gate_is_off_without_both_keys(self, backend, triple):
        seed(backend, {"closed": 3.0, "future": 2.0, "open": 1.0})
        backend.sorted_add(IA, member("closed"), NOW - 1)
        backend.sorted_add(VF, member("future"), NOW + 1)
        backend.sorted_add(IA, member("open"), INF)
        assert rank(backend, validity=triple) == rank(backend)
        assert len(members(rank(backend, validity=triple))) == 3

    def test_the_gate_composes_with_base_and_modulation_and_limit(self, backend):
        for name in ("closed", "a", "b", "c"):
            save(backend, name, 2.0)
            set_confidence(backend, name, {"confidence": 0.9})
        seed(backend, {"closed": 1.0, "a": 2.0, "b": 3.0, "c": 4.0})
        backend.sorted_add(IA, member("closed"), NOW - 1)
        reply = rank(
            backend,
            base_score_field="strength",
            confidence=(CONF, "0.5", "0.5"),
            validity=(IA, VF, NOW),
            limit=2,
        )
        assert members(reply) == [member("a"), member("b")]
        want = [
            (member("a"), lua_score(ts(2.0), base=2.0, c=0.9, s=0.5)),
            (member("b"), lua_score(ts(3.0), base=2.0, c=0.9, s=0.5)),
        ]
        assert_scores(reply, want)

    @pytest.mark.parametrize("ratio", [0.0, -1.0, 0.5, 4.0, 1e9])
    def test_the_pretrim_budget_never_changes_the_reply(self, backend, ratio):
        # ARGV[8] (#585) only picks the script's membership strategy.
        seed(backend, {"closed": 1.0, "future": 2.0, "open": 3.0, "free": 4.0})
        backend.sorted_add(IA, member("closed"), NOW - 1)
        backend.sorted_add(VF, member("future"), NOW + 1)
        backend.sorted_add(IA, member("open"), INF)
        backend.sorted_add(VF, member("open"), NOW - 1)
        gated = rank(backend, validity=(IA, VF, NOW), pretrim_max_ratio=ratio)
        assert gated == rank(backend, validity=(IA, VF, NOW))
        assert members(gated) == [member("open"), member("free")]

    def test_a_nan_as_of_excludes_nothing(self, backend):
        seed(backend, {"closed": 1.0, "future": 2.0})
        backend.sorted_add(IA, member("closed"), NOW - 1)
        backend.sorted_add(VF, member("future"), NOW + 1)
        assert rank(backend, validity=(IA, VF, float("nan"))) == rank(backend)

    def test_the_as_of_bound_is_bit_exact(self, backend):
        # A member closing one ulp after as_of is included; one ulp before
        # (or at) is excluded. Catches any rendering of the bound.
        as_of = 1_700_000_000.123456
        seed(backend, {"after": 1.0, "at": 1.0, "before": 1.0})
        backend.sorted_add(IA, member("after"), math.nextafter(as_of, INF))
        backend.sorted_add(IA, member("at"), as_of)
        backend.sorted_add(IA, member("before"), math.nextafter(as_of, -INF))
        assert members(rank(backend, validity=(IA, VF, as_of))) == [member("after")]


# -- decayed_rank: random parity against the Redis oracle ----------------------


def random_dataset(rng: random.Random, n: int) -> dict[str, dict[str, Any]]:
    """``n`` members with random age, base-score payload, confidence payload
    and validity interval, covering every shape the two scripts branch on."""
    data: dict[str, dict[str, Any]] = {}
    for i in range(n):
        name = f"m-{i:03d}"
        age = rng.uniform(-2.0, 400.0)
        kind = rng.random()
        if kind < 0.40:
            strength: Any = rng.uniform(-3.0, 5.0)
            base = float(strength)
        elif kind < 0.60:
            strength = rng.randint(-5, 50)
            base = float(strength)
        elif kind < 0.75:
            strength = {**DECIMAL, "as_encodable": "%.6f" % rng.uniform(0.0, 4.0)}
            base = float(strength["as_encodable"])
        elif kind < 0.85:
            strength = "no-record"
            base = 1.0
        elif kind < 0.95:
            strength = None  # record without the field
            base = 1.0
        else:
            strength = rng.choice(["junk", True, [1, 2], {"x": 1}])
            base = 1.0
        ckind = rng.random()
        if ckind < 0.50:
            c = rng.uniform(-0.2, 1.2)
            conf: Any = lua_payload((c, rng.randint(0, 30), 1, 1))
        elif ckind < 0.60:
            c = rng.uniform(0.0, 1.0)
            conf = [c, 1, 1, 0]
        elif ckind < 0.70:
            c = None
            conf = "missing"
        elif ckind < 0.80:
            c = rng.uniform(0.0, 1.0)
            conf = {1: c}
        elif ckind < 0.90:
            c = None
            conf = {"confidence": "0.7"}
        else:
            c = None
            conf = msgpack.packb({"confidence": 0.7})[:-3]
        vkind = rng.random()
        if vkind < 0.30:
            interval: tuple[float | None, float | None] = (
                NOW - rng.uniform(0.0, 10.0),
                NOW - rng.uniform(10.0, 20.0),
            )
        elif vkind < 0.40:
            interval = (NOW + rng.uniform(0.0, 10.0), NOW - rng.uniform(10.0, 20.0))
        elif vkind < 0.60:
            interval = (INF, NOW - rng.uniform(0.0, 20.0))
        elif vkind < 0.70:
            interval = (INF, NOW + rng.uniform(0.0, 20.0))
        else:
            interval = (None, None)
        data[name] = dict(
            age=age, strength=strength, base=base, conf=conf, c=c, interval=interval
        )
    return data


def load_dataset(backend: Any, data: dict[str, dict[str, Any]]) -> None:
    for name, row in data.items():
        if row["strength"] != "no-record":
            save(backend, name, row["strength"])
        backend.sorted_add(ZIDX, member(name), ts(row["age"]))
        if row["conf"] != "missing":
            set_confidence(backend, name, row["conf"])
        invalid_at, valid_from = row["interval"]
        if invalid_at is not None:
            backend.sorted_add(IA, member(name), invalid_at)
        if valid_from is not None:
            backend.sorted_add(VF, member(name), valid_from)


def reference_ranking(
    data: dict[str, dict[str, Any]],
    *,
    rate: float,
    modulate: tuple[float, float] | None,
    gate: bool,
    use_base: bool,
    limit: int | None,
) -> list[tuple[str, float]]:
    scored = []
    for name, row in data.items():
        invalid_at, valid_from = row["interval"]
        if gate and (
            (invalid_at is not None and invalid_at <= NOW)
            or (valid_from is not None and valid_from > NOW)
        ):
            continue
        base = row["base"] if use_base else 1.0
        if modulate is None:
            c = None
            s, c0 = 0.0, C0
        else:
            s, c0 = modulate
            c = c0 if row["c"] is None else row["c"]
        scored.append(
            (
                member(name),
                lua_score(ts(row["age"]), base=base, rate=rate, c=c, s=s, c0=c0),
            )
        )
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored if limit is None else scored[:limit]


SCENARIOS = [
    dict(rate=0.1, modulate=None, gate=False, use_base=False, limit=None),
    dict(rate=0.5, modulate=(0.5, 0.5), gate=True, use_base=True, limit=None),
    dict(rate=1.0, modulate=(1.0, 0.3), gate=True, use_base=True, limit=50),
    dict(rate=0.3, modulate=(0.25, 0.8), gate=False, use_base=True, limit=None),
    dict(rate=0.7, modulate=None, gate=True, use_base=False, limit=7),
]


class TestRandomParity:
    """~200 random members per scenario: identical order on every leg and
    scores within :data:`REL_TOL` of the Lua arithmetic; the Postgres leg is
    also replayed through the live ``RedisBackend`` and compared element for
    element (members exact, scores within tolerance), and the count of
    rendered scores that differ at all is printed for the record."""

    @pytest.mark.parametrize(
        "scenario", SCENARIOS, ids=[str(i) for i in range(len(SCENARIOS))]
    )
    def test_two_hundred_members(self, backend, backend_is_redis, scenario):
        rng = random.Random(631 + SCENARIOS.index(scenario))
        data = random_dataset(rng, 200)
        load_dataset(backend, data)
        kwargs: dict[str, Any] = dict(
            decay_rate=scenario["rate"], limit=scenario["limit"]
        )
        if scenario["use_base"]:
            kwargs["base_score_field"] = "strength"
        if scenario["modulate"] is not None:
            s, c0 = scenario["modulate"]
            kwargs["confidence"] = (CONF, str(s), str(c0))
        if scenario["gate"]:
            kwargs["validity"] = (IA, VF, NOW)
        reply = rank(backend, **kwargs)
        expected = reference_ranking(data, **scenario)
        assert len(expected) > 0
        assert_scores(reply, expected)
        max_rel = max(
            (
                abs(g - w) / abs(w)
                for (_, g), (_, w) in zip(decoded(reply), expected)
                if w
            ),
            default=0.0,
        )
        print(
            f"\nscenario {scenario}: {len(expected)} ranked, max rel deviation {max_rel:.3e}"
        )

        if backend_is_redis:
            return
        # The oracle proper: the same dataset through the live Lua.
        redis = RedisBackend()
        redis.client.delete(ZIDX, CONF, IA, VF)
        load_dataset(redis, data)
        try:
            oracle_reply = rank(redis, **kwargs)
        finally:
            redis.client.delete(ZIDX, CONF, IA, VF, *(member(n) for n in data))
        assert oracle_reply[0::2] == reply[0::2]
        differing = 0
        worst = 0.0
        for got, want in zip(decoded(reply), decoded(oracle_reply)):
            assert close(got[1], want[1]), (got, want)
            if want[1]:
                worst = max(worst, abs(got[1] - want[1]) / abs(want[1]))
        differing = sum(1 for a, b in zip(reply[1::2], oracle_reply[1::2]) if a != b)
        print(
            f"postgres vs redis: {len(reply) // 2} scores, {differing} rendered "
            f"differently, max rel deviation {worst:.3e}"
        )


# -- confidence_update ----------------------------------------------------------


class TestConfidenceUpdate:
    def update(self, backend: Any, suffix: Any, signal: float, **overrides: Any) -> Any:
        kwargs: dict[str, Any] = dict(
            initial=INITIAL, cap=CAP, require_record=member(suffix)
        )
        kwargs.update(overrides)
        return backend.confidence_update(CONF, member(suffix), signal, **kwargs)

    def test_a_sequence_matches_the_lua_in_reply_and_bytes(self, backend):
        save(backend, "m")
        state = (INITIAL, 0, 0, 0)
        for signal in [0.9, 0.1, 1.0, 0.0, 0.5, 0.75, 0.3333, 0.9]:
            state = lua_update(state, signal)
            result = self.update(backend, "m", signal)
            assert result == reply(state)
            assert isinstance(result[0], float)
            assert all(isinstance(n, int) for n in result[1:])
            assert backend.map_get(CONF, member("m")) == packed(state)

    def test_the_seed_the_field_layer_writes_is_read(self, backend):
        save(backend, "m")
        # on_save's HSETNX payload, in Python's key order.
        set_confidence(
            backend,
            "m",
            {
                "confidence": 0.3,
                "evidence_count": 0,
                "corroborations": 0,
                "contradictions": 0,
            },
        )
        assert self.update(backend, "m", 0.9) == reply(lua_update((0.3, 0, 0, 0), 0.9))
        assert stored(backend, "m") == {
            "corroborations": 1,
            "confidence": 0.3 + (0.9 - 0.3) / 2,
            "contradictions": 0,
            "evidence_count": 1,
        }

    def test_the_cap_freezes_the_gain(self, backend):
        save(backend, "m")
        state = (0.0, 0, 0, 0)
        results = []
        for _ in range(6):
            state = lua_update(state, 1.0, cap=2)
            result = self.update(backend, "m", 1.0, initial=0.0, cap=2)
            assert result == reply(state)
            results.append(result[0])
        # Below the cap: running mean (1/2 then 1/3 of the gap); at the cap
        # the gain is 1/(cap+1) = 1/3 of the gap for every later update.
        assert results[0] == 0.5
        gaps = [1.0] + [1.0 - r for r in results]
        for before, after in list(zip(gaps, gaps[1:]))[1:]:
            assert math.isclose(after / before, 2 / 3, rel_tol=1e-9)

    def test_an_absent_record_returns_none_and_writes_nothing(self, backend):
        assert self.update(backend, "never-saved", 0.9) is None
        assert backend.map_get(CONF, member("never-saved")) is None
        # A deleted record is absent too, and its payload is left as it was.
        save(backend, "gone")
        self.update(backend, "gone", 0.9)
        before = backend.map_get(CONF, member("gone"))
        backend.delete_record(member("gone"), class_set=CLASS_SET)
        assert self.update(backend, "gone", 0.9) is None
        assert backend.map_get(CONF, member("gone")) == before

    def test_without_the_record_guard_an_unsaved_member_is_updated(self, backend):
        assert self.update(backend, "never-saved", 0.9, require_record=None) == reply(
            lua_update((INITIAL, 0, 0, 0), 0.9)
        )
        assert stored(backend, "never-saved") == {
            "corroborations": 1,
            "confidence": INITIAL + (0.9 - INITIAL) / 2,
            "contradictions": 0,
            "evidence_count": 1,
        }

    @pytest.mark.parametrize("initial", [0.0, 0.3, 0.5, 1.0])
    def test_a_member_without_a_payload_starts_from_initial(self, backend, initial):
        save(backend, "m")
        assert self.update(backend, "m", 0.9, initial=initial) == reply(
            lua_update((initial, 0, 0, 0), 0.9)
        )

    @pytest.mark.parametrize(
        "payload",
        [
            b"\xc1garbage",
            b"",
            msgpack.packb(0.7),
            msgpack.packb("x"),
            msgpack.packb(None),
        ],
        ids=["garbage", "empty", "number", "string", "nil"],
    )
    def test_a_non_table_payload_falls_back_to_the_defaults(self, backend, payload):
        save(backend, "m")
        set_confidence(backend, "m", payload)
        assert self.update(backend, "m", 0.9) == reply(
            lua_update((INITIAL, 0, 0, 0), 0.9)
        )

    def test_an_array_payload_is_read_positionally(self, backend):
        save(backend, "m")
        set_confidence(backend, "m", [0.4, 3, 2, 1])
        assert self.update(backend, "m", 0.1) == reply(lua_update((0.4, 3, 2, 1), 0.1))

    def test_a_partial_map_defaults_the_missing_fields(self, backend):
        save(backend, "m")
        set_confidence(backend, "m", {"confidence": 0.8})
        assert self.update(backend, "m", 0.1) == reply(lua_update((0.8, 0, 0, 0), 0.1))
        set_confidence(backend, "m", {"evidence_count": 5, "contradictions": 2})
        assert self.update(backend, "m", 0.9) == reply(
            lua_update((INITIAL, 5, 0, 2), 0.9)
        )

    def test_a_numeric_string_is_coerced_like_lua_arithmetic(self, backend):
        save(backend, "m")
        set_confidence(backend, "m", {"confidence": "0.8", "evidence_count": "3"})
        assert self.update(backend, "m", 0.1) == reply(lua_update((0.8, 3, 0, 0), 0.1))

    def test_the_result_is_clamped_to_the_unit_interval(self, backend):
        save(backend, "m")
        set_confidence(backend, "m", {"confidence": 5.0})
        assert self.update(backend, "m", 1.0) == (1.0, 1, 1, 0)
        set_confidence(backend, "m", {"confidence": -2.0})
        assert self.update(backend, "m", 0.0) == (0.0, 1, 0, 1)
        assert stored(backend, "m")["confidence"] == 0

    def test_a_signal_of_one_half_corroborates(self, backend):
        save(backend, "m")
        assert self.update(backend, "m", 0.5) == (0.5, 1, 1, 0)
        assert self.update(backend, "m", 0.49999) == reply(
            lua_update((0.5, 1, 1, 0), 0.49999)
        )
        assert stored(backend, "m")["contradictions"] == 1

    def test_integral_results_pack_as_integers(self, backend):
        # cmsgpack packs an integral double as a msgpack integer: a
        # confidence that lands on exactly 1.0 or 0.0 is stored as 1 / 0.
        save(backend, "m")
        self.update(backend, "m", 1.0, initial=1.0)
        assert backend.map_get(CONF, member("m")) == packed((1.0, 1, 1, 0))
        assert msgpack.unpackb(backend.map_get(CONF, member("m")))["confidence"] == 1
        assert backend.map_get(CONF, member("m")).find(b"\xca") == -1

    def test_queued_on_a_unit_of_work(self, backend):
        save(backend, "m")
        self.update(backend, "m", 0.9)
        before = backend.map_get(CONF, member("m"))
        uow = backend.begin()
        assert self.update(backend, "m", 0.1, uow=uow) is None
        assert self.update(backend, "m", 0.1, uow=uow) is None
        assert backend.map_get(CONF, member("m")) == before, "nothing before commit"
        results = uow.commit()
        assert len(results) == 2 and all(results)
        state = lua_update(lua_update(lua_update((INITIAL, 0, 0, 0), 0.9), 0.1), 0.1)
        assert backend.map_get(CONF, member("m")) == packed(state)
        assert uow.commit() == []

    def test_a_unit_of_work_left_without_commit_applies_nothing(self, backend):
        save(backend, "m")
        self.update(backend, "m", 0.9)
        before = backend.map_get(CONF, member("m"))
        with backend.begin() as uow:
            self.update(backend, "m", 0.1, uow=uow)
        assert backend.map_get(CONF, member("m")) == before

    def test_an_absent_record_on_a_unit_of_work_is_a_none_entry(self, backend):
        save(backend, "m")
        uow = backend.begin()
        self.update(backend, "never-saved", 0.9, uow=uow)
        self.update(backend, "m", 0.9, uow=uow)
        results = uow.commit()
        assert results[0] is None
        assert results[1]
        assert backend.map_get(CONF, member("never-saved")) is None
        assert stored(backend, "m")["evidence_count"] == 1

    def test_two_connections_both_apply(self, backend):
        """Two instances (two connections on Postgres) updating one member
        concurrently: every update lands, and because the running mean is
        order-invariant below the cap the final state is the serial one."""
        save(backend, "m")
        other = oracle(backend)
        rounds = 6
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def worker(instance: Any, signal: float) -> None:
            try:
                barrier.wait(timeout=10)
                for _ in range(rounds):
                    self.update(instance, "m", signal)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(backend, 0.9)),
            threading.Thread(target=worker, args=(other, 0.1)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        if isinstance(other, PostgresBackend):
            other.close()
        assert not errors, errors
        final = stored(backend, "m")
        assert final is not None
        assert final["evidence_count"] == 2 * rounds
        assert final["corroborations"] == rounds
        assert final["contradictions"] == rounds
        # Exact running mean of {initial, signals...}: order-invariant.
        want = (INITIAL + rounds * 0.9 + rounds * 0.1) / (2 * rounds + 1)
        assert math.isclose(final["confidence"], want, rel_tol=1e-9)

    def test_identical_concurrent_signals_reach_the_serial_bytes(self, backend):
        save(backend, "m")
        other = oracle(backend)
        rounds = 5
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def worker(instance: Any) -> None:
            try:
                barrier.wait(timeout=10)
                for _ in range(rounds):
                    self.update(instance, "m", 0.9)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(b,)) for b in (backend, other)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        if isinstance(other, PostgresBackend):
            other.close()
        assert not errors, errors
        state = (INITIAL, 0, 0, 0)
        for _ in range(2 * rounds):
            state = lua_update(state, 0.9)
        assert backend.map_get(CONF, member("m")) == packed(state)


# -- the Postgres stubs are gone ------------------------------------------------


def test_no_group_h_method_still_raises_on_postgres(backend, backend_is_redis):
    if backend_is_redis:
        pytest.skip("Postgres-leg assertion")
    assert (
        backend.decayed_rank(
            ZIDX, now=NOW, decay_rate=RATE, limit=None, pretrim_max_ratio=PRETRIM
        )
        == []
    )
    assert backend.confidence_update(
        CONF, member("x"), 0.9, initial=INITIAL, cap=CAP
    ) == (
        0.7,
        1,
        1,
        0,
    )
    with pytest.raises(NotImplementedError, match="Redis-only"):
        backend.native()
