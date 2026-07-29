"""Direct tests of the production decay Lua script (``DECAY_SCORE_LUA``).

This file used to carry a *private copy* of the Lua script, which meant it
happily passed while production drifted underneath it (spike-2, issue #491).
It now imports the real script from
``src.popoto.fields.decaying_sorted_field``, so any change to production math
is exercised here.

Because it drives the script directly (rather than through a Model), it is the
cheapest place to pin down the raw contract:

- Lua execution basics (eval, math.pow, table.sort)
- Power-law decay: ``base_score * elapsed_days^(-decay_rate)``
- base_score read from the member's own model hash via ``cmsgpack``
- Confidence modulation (#491): neutrality, direction, the ``max(t, 1.0)``
  sign-flip guard, clamping, and corrupt-payload fallback

EVAL shapes used below:

    pre-#491 (oracle):  eval(LUA, 1, zset, now, rate, n, base_field)
    modulated:          eval(LUA, 2, zset, conf_hash,
                             now, rate, n, base_field, s, c0)

The 1-KEY form is the byte-exact regression oracle: ``ARGV[5]`` is nil there,
so ``s`` is 0 and the modulation branch never runs.
"""

import sys
import os
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import msgpack

from src.popoto.fields.decaying_sorted_field import DECAY_SCORE_LUA
from src.popoto.redis_db import POPOTO_REDIS_DB

ZSET_KEY = "_test:decay:timestamps"
CONF_KEY = "_test:decay:confidence"


def cleanup(*extra_keys):
    keys = [ZSET_KEY, CONF_KEY, *extra_keys]
    keys += [k for k in POPOTO_REDIS_DB.scan_iter(match="item:*")]
    keys += [k for k in POPOTO_REDIS_DB.scan_iter(match="_test:decay*")]
    if keys:
        POPOTO_REDIS_DB.delete(*keys)


def set_base_score(member, value, field="base"):
    """Production Lua reads the base score off the member's OWN hash."""
    POPOTO_REDIS_DB.hset(member, field, msgpack.packb(value))


def set_confidence(member, confidence, hash_key=CONF_KEY):
    POPOTO_REDIS_DB.hset(
        hash_key,
        member,
        msgpack.packb(
            {
                "confidence": confidence,
                "evidence_count": 10,
                "corroborations": 0,
                "contradictions": 10,
            },
            use_bin_type=True,
        ),
    )


def run_plain(now, rate="0.5", n="10", base_field=""):
    """Pre-#491 call shape — the byte-exact regression oracle."""
    return POPOTO_REDIS_DB.eval(
        DECAY_SCORE_LUA, 1, ZSET_KEY, str(now), str(rate), str(n), base_field
    )


def run_modulated(now, s, c0="0.5", rate="0.5", n="10", base_field="", conf=CONF_KEY):
    return POPOTO_REDIS_DB.eval(
        DECAY_SCORE_LUA,
        2,
        ZSET_KEY,
        conf,
        str(now),
        str(rate),
        str(n),
        base_field,
        str(s),
        str(c0),
    )


def as_scores(result):
    """[member, score, ...] -> {member: raw_score_string}."""
    decoded = [x.decode() if isinstance(x, bytes) else x for x in (result or [])]
    return {decoded[i]: decoded[i + 1] for i in range(0, len(decoded), 2)}


class TestLuaBasic:
    """Verify Lua execution works at all."""

    def test_lua_echo(self):
        """Simplest possible Lua script."""
        result = POPOTO_REDIS_DB.eval("return 'hello'", 0)
        assert result == b"hello"

    def test_lua_arithmetic(self):
        """Lua can do math and return results."""
        result = POPOTO_REDIS_DB.eval("return tostring(2.5 * 3.0)", 0)
        assert float(result) == 7.5

    def test_lua_power_function(self):
        """Lua math.pow works (needed for decay formula)."""
        result = POPOTO_REDIS_DB.eval("return tostring(math.pow(4.0, -0.5))", 0)
        assert abs(float(result) - 0.5) < 0.001

    def test_lua_pow_two_to_zero_is_exactly_one(self):
        """The neutrality guarantee rests on math.pow(2, 0) == 1.0 exactly."""
        result = POPOTO_REDIS_DB.eval(
            "return tostring(math.pow(2, 0.5 * 2 * (0.5 - 0.5)) == 1.0)", 0
        )
        assert result == b"true"

    def test_lua_table_sort(self):
        """Lua table.sort works (needed for ranking)."""
        script = """
        local t = {3, 1, 2}
        table.sort(t)
        return t
        """
        result = POPOTO_REDIS_DB.eval(script, 0)
        assert result == [1, 2, 3]


class TestDecayScoring:
    """Test the full decay scoring Lua script."""

    def setup_method(self):
        cleanup()

    def teardown_method(self):
        cleanup()

    def test_single_member(self):
        """One member, verify decay formula output."""
        now = time.time()
        POPOTO_REDIS_DB.zadd(ZSET_KEY, {"item:1": now - 86400})
        set_base_score("item:1", 1.0)

        result = run_plain(now, base_field="base")

        assert len(result) == 2
        scores = as_scores(result)
        # 1.0 * 1.0^(-0.5) = 1.0
        assert abs(float(scores["item:1"]) - 1.0) < 0.05

    def test_recent_beats_old(self):
        """Recently updated member should outscore older one with same base."""
        now = time.time()
        POPOTO_REDIS_DB.zadd(
            ZSET_KEY, {"item:recent": now - 3600, "item:old": now - 86400 * 30}
        )
        set_base_score("item:recent", 1.0)
        set_base_score("item:old", 1.0)

        result = run_plain(now, base_field="base")
        decoded = [x.decode() for x in result]

        assert decoded[0] == "item:recent"
        assert decoded[2] == "item:old"
        assert float(decoded[1]) > float(decoded[3])

    def test_high_base_beats_low_base(self):
        """Higher base score should win when timestamps are equal."""
        now = time.time()
        same_time = now - 86400
        POPOTO_REDIS_DB.zadd(ZSET_KEY, {"item:high": same_time, "item:low": same_time})
        set_base_score("item:high", 10.0)
        set_base_score("item:low", 1.0)

        result = run_plain(now, base_field="base")
        decoded = [x.decode() for x in result]

        assert decoded[0] == "item:high"
        assert float(decoded[1]) > float(decoded[3])

    def test_decay_rate_affects_ranking(self):
        """Higher decay rate penalizes old items more."""
        now = time.time()
        POPOTO_REDIS_DB.zadd(
            ZSET_KEY, {"item:recent": now - 3600, "item:old": now - 86400 * 10}
        )
        set_base_score("item:recent", 1.0)
        set_base_score("item:old", 5.0)  # old has 5x base score

        low = [x.decode() for x in run_plain(now, rate="0.1", base_field="base")]
        high = [x.decode() for x in run_plain(now, rate="1.0", base_field="base")]

        # Low decay: base score matters more -> "old" (5.0 base) wins
        assert low[0] == "item:old"
        # High decay: recency matters more -> "recent" wins
        assert high[0] == "item:recent"

    def test_limit_respected(self):
        """Only top-N results returned."""
        now = time.time()
        members = {f"item:{i}": now - (i * 3600) for i in range(20)}
        POPOTO_REDIS_DB.zadd(ZSET_KEY, members)

        result = run_plain(now, n="5")
        # 5 results * 2 (member + score) = 10 elements
        assert len(result) == 10

    def test_empty_sorted_set(self):
        """No members returns empty result."""
        assert run_plain(time.time()) in (None, [])

    def test_empty_sorted_set_with_modulation_enabled(self):
        """n=0 members is unaffected by the modulation branch (#491)."""
        assert run_modulated(time.time(), s="0.5") in (None, [])

    def test_missing_base_score_defaults_to_one(self):
        """Members without a base score on their hash default to 1.0."""
        now = time.time()
        POPOTO_REDIS_DB.zadd(ZSET_KEY, {"item:no_base": now - 86400})

        result = run_plain(now, base_field="base")
        assert len(result) == 2
        # 1.0 * 1.0^(-0.5) = 1.0 (1 day elapsed, decay_rate 0.5)
        assert abs(float(result[1]) - 1.0) < 0.05


class TestDecayScoringFormula:
    """Verify the math matches expected ACT-R-style decay."""

    def setup_method(self):
        cleanup()

    def teardown_method(self):
        cleanup()

    def test_known_values(self):
        """Verify against hand-computed decay values."""
        now = time.time()
        cases = {"item:1day": 1, "item:4day": 4, "item:100day": 100}
        POPOTO_REDIS_DB.zadd(ZSET_KEY, {k: now - 86400 * d for k, d in cases.items()})
        for key in cases:
            set_base_score(key, 1.0)

        scores = as_scores(run_plain(now, base_field="base"))

        assert abs(float(scores["item:1day"]) - 1.0) < 0.05
        assert abs(float(scores["item:4day"]) - 0.5) < 0.05
        assert abs(float(scores["item:100day"]) - 0.1) < 0.05

    def test_base_score_scaling(self):
        """Base score multiplies the decay: score=5 at 4 days = 5 * 0.5 = 2.5."""
        now = time.time()
        POPOTO_REDIS_DB.zadd(ZSET_KEY, {"item:scaled": now - 86400 * 4})
        set_base_score("item:scaled", 5.0)

        scores = as_scores(run_plain(now, base_field="base"))
        # 5.0 * 4^(-0.5) = 5.0 * 0.5 = 2.5
        assert abs(float(scores["item:scaled"]) - 2.5) < 0.15


# ---------------------------------------------------------------------------
# Confidence modulation at the raw-Lua level (#491)
#
# These drive the script directly, so they can plant payloads no Python API
# would ever write (corrupt bytes, out-of-range confidences) and can compare
# Lua's raw tostring() output byte-for-byte against the pre-#491 oracle.
# ---------------------------------------------------------------------------


class TestConfidenceModulationLua:
    AGED = 30  # days; well past the max(t, 1.0) guard

    def setup_method(self):
        cleanup()

    def teardown_method(self):
        cleanup()

    def _plant(self, now, days=AGED, members=("item:m",)):
        POPOTO_REDIS_DB.zadd(ZSET_KEY, {m: now - 86400 * days for m in members})

    def test_neutral_confidence_is_byte_identical(self):
        """c == c0 must reproduce the pre-#491 score string exactly."""
        now = time.time()
        self._plant(now)
        set_confidence("item:m", 0.5)

        assert run_modulated(now, s="0.5", c0="0.5") == run_plain(now)

    def test_neutral_at_non_default_c0_is_byte_identical(self):
        """The exponent centers on c0, not a hard-coded 0.5 (critique blocker)."""
        now = time.time()
        self._plant(now)
        set_confidence("item:m", 0.3)

        assert run_modulated(now, s="0.5", c0="0.3") == run_plain(now)
        # Sanity: the same payload against the default centering DOES move,
        # which is exactly the permanent penalty the c0 fix removes.
        assert run_modulated(now, s="0.5", c0="0.5") != run_plain(now)

    def test_zero_strength_is_byte_identical(self):
        """s = 0 is the arithmetic kill switch."""
        now = time.time()
        self._plant(now)
        set_confidence("item:m", 0.01)

        assert run_modulated(now, s="0") == run_plain(now)

    def test_empty_confidence_key_is_byte_identical(self):
        """An empty KEYS[2] disables the branch (and skips the HGET)."""
        now = time.time()
        self._plant(now)
        set_confidence("item:m", 0.01)

        assert run_modulated(now, s="0.5", conf="") == run_plain(now)

    def test_absent_member_is_byte_identical(self):
        """HGET -> nil falls back to c0 (neutral)."""
        now = time.time()
        self._plant(now)
        # Nothing written to CONF_KEY at all.
        assert run_modulated(now, s="0.5") == run_plain(now)

    def test_missing_confidence_hash_entirely_is_byte_identical(self):
        """The :data hash may not exist yet; that is neutral, not an error."""
        now = time.time()
        self._plant(now)
        POPOTO_REDIS_DB.delete(CONF_KEY)

        assert run_modulated(now, s="0.5", conf="_test:decay:absent") == run_plain(now)

    def test_low_confidence_decays_faster_high_decays_slower(self):
        """Directional effect at t > 1 day."""
        now = time.time()
        self._plant(now, members=("item:low", "item:mid", "item:high"))
        set_confidence("item:low", 0.05)
        set_confidence("item:mid", 0.5)
        set_confidence("item:high", 0.95)

        scores = as_scores(run_modulated(now, s="0.5"))
        neutral = as_scores(run_plain(now))

        assert float(scores["item:low"]) < float(scores["item:mid"])
        assert float(scores["item:mid"]) < float(scores["item:high"])
        # The neutral member is untouched, byte-for-byte.
        assert scores["item:mid"] == neutral["item:mid"]
        # ... and the others genuinely moved.
        assert float(scores["item:low"]) < float(neutral["item:low"])
        assert float(scores["item:high"]) > float(neutral["item:high"])

    def test_fresh_records_are_not_sign_flipped(self):
        """Risk 4: below one day the correction must be exactly 1.0.

        Without the ``math.max(elapsed_days, 1.0)`` guard, a larger effective
        rate AMPLIFIES the t^(-r) multiplier for t < 1, so low-confidence junk
        would outrank corroborated memories for the whole fresh working set.
        """
        now = time.time()
        # 0.5 days: inside the guard region, above the 0.01-day floor.
        POPOTO_REDIS_DB.zadd(
            ZSET_KEY, {"item:low": now - 43200, "item:high": now - 43200}
        )
        set_confidence("item:low", 0.01)
        set_confidence("item:high", 0.99)

        scores = as_scores(run_modulated(now, s="0.5"))
        neutral = as_scores(run_plain(now))

        # Fresh: both are exactly the unmodulated score. Not "close" — equal.
        assert scores["item:low"] == neutral["item:low"]
        assert scores["item:high"] == neutral["item:high"]
        assert scores["item:low"] == scores["item:high"]
        # Which means low confidence can never OUTRANK high confidence.
        assert float(scores["item:low"]) <= float(scores["item:high"])

    def test_exactly_one_day_is_the_hinge(self):
        """t == 1.0 day is the boundary: correction base is exactly 1.0."""
        now = time.time()
        POPOTO_REDIS_DB.zadd(ZSET_KEY, {"item:m": now - 86400})
        set_confidence("item:m", 0.01)

        assert run_modulated(now, s="0.5") == run_plain(now)

    def test_corrupt_payload_falls_back_to_neutral(self):
        """Undecodable bytes must not crash the script or zero the score.

        0xc1 is msgpack's "never used" byte, so ``cmsgpack.unpack`` raises and
        the ``pcall`` guard is what keeps the whole EVAL from failing. 0xff
        decodes to the scalar -1, exercising the ``type(data) == 'table'``
        guard instead. Both land on c0.
        """
        now = time.time()
        self._plant(now)
        for corrupt in (b"\xc1", b"", b"\xff not msgpack at all \x00"):
            POPOTO_REDIS_DB.hset(CONF_KEY, "item:m", corrupt)
            assert run_modulated(now, s="0.5") == run_plain(now), corrupt

    def test_non_table_payload_falls_back_to_neutral(self):
        """A validly-packed but wrongly-shaped payload is still neutral."""
        now = time.time()
        self._plant(now)
        POPOTO_REDIS_DB.hset(CONF_KEY, "item:m", msgpack.packb(42))

        assert run_modulated(now, s="0.5") == run_plain(now)

    def test_non_numeric_confidence_falls_back_to_neutral(self):
        """confidence='high' is not a number -> c0."""
        now = time.time()
        self._plant(now)
        POPOTO_REDIS_DB.hset(
            CONF_KEY, "item:m", msgpack.packb({"confidence": "high"}, use_bin_type=True)
        )

        assert run_modulated(now, s="0.5") == run_plain(now)

    def test_negative_confidence_clamps_to_zero(self):
        """c < 0 must behave exactly like c == 0, not blow up the exponent."""
        now = time.time()
        self._plant(now, members=("item:m",))
        set_confidence("item:m", -3.0)
        clamped = run_modulated(now, s="0.5")

        set_confidence("item:m", 0.0)
        assert clamped == run_modulated(now, s="0.5")

    def test_confidence_above_one_clamps_to_one(self):
        """c > 1 must behave exactly like c == 1."""
        now = time.time()
        self._plant(now, members=("item:m",))
        set_confidence("item:m", 7.5)
        clamped = run_modulated(now, s="0.5")

        set_confidence("item:m", 1.0)
        assert clamped == run_modulated(now, s="0.5")

    def test_modulated_score_matches_the_documented_formula(self):
        """decayed = base * t^-r * max(t,1)^-(eff - r), eff = r * 2^(s*2*(c0-c))."""
        now = time.time()
        days = 100
        POPOTO_REDIS_DB.zadd(ZSET_KEY, {"item:m": now - 86400 * days})
        set_confidence("item:m", 0.0)

        r, s, c0 = 0.5, 0.5, 0.5
        eff = r * 2 ** (s * 2 * (c0 - 0.0))
        expected = days ** (-r) * max(days, 1.0) ** (-(eff - r))

        got = float(as_scores(run_modulated(now, s=s, c0=c0, rate=r))["item:m"])
        assert abs(got - expected) < 1e-9

    def test_tied_members_with_different_confidence_stop_tying(self):
        """Modulation breaks a tie that neutrality preserves bit-exactly."""
        now = time.time()
        members = ("item:t_a", "item:t_b", "item:t_c")
        self._plant(now, members=members)

        # No confidence data: all three tie, bit-exactly (issue #448 contract).
        tied = as_scores(run_modulated(now, s="0.5"))
        assert len(set(tied.values())) == 1

        set_confidence("item:t_a", 0.05)
        set_confidence("item:t_b", 0.5)
        set_confidence("item:t_c", 0.95)
        untied = as_scores(run_modulated(now, s="0.5"))
        assert len(set(untied.values())) == 3
