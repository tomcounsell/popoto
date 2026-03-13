"""Test that Lua scripting works for decay-based scoring in Redis.

Validates:
- Basic Lua script execution via redis-py eval()
- Power-law decay formula: base_score * elapsed^(-decay_rate)
- Batch score computation across sorted set members
- Score ordering correctness (recent > old, high base > low base)
"""

import sys
import os
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB


# Lua script: compute decayed scores for all members of a sorted set.
# The sorted set stores members with their last_updated timestamp as score.
# Base scores are stored in a separate hash.
# Returns top-N members ranked by decayed score.
#
# KEYS[1] = sorted set key (member -> last_updated_timestamp)
# KEYS[2] = hash key (member -> base_score)
# ARGV[1] = current timestamp (seconds)
# ARGV[2] = decay rate (e.g. 0.5)
# ARGV[3] = max results to return
DECAY_SCORE_LUA = """
local zset_key = KEYS[1]
local hash_key = KEYS[2]
local now = tonumber(ARGV[1])
local decay_rate = tonumber(ARGV[2])
local max_results = tonumber(ARGV[3])

-- Get all members with their last_updated timestamps
local members = redis.call('ZRANGE', zset_key, 0, -1, 'WITHSCORES')

local scored = {}
for i = 1, #members, 2 do
    local member = members[i]
    local last_updated = tonumber(members[i + 1])

    -- Get base score from hash
    local base_str = redis.call('HGET', hash_key, member)
    local base_score = tonumber(base_str) or 1.0

    -- Compute elapsed time in days (minimum 0.01 to avoid division by zero)
    local elapsed_days = math.max((now - last_updated) / 86400, 0.01)

    -- Power-law decay: base_score * elapsed^(-decay_rate)
    local decayed = base_score * math.pow(elapsed_days, -decay_rate)

    table.insert(scored, {member, decayed})
end

-- Sort by decayed score descending
table.sort(scored, function(a, b) return a[2] > b[2] end)

-- Return top-N as flat array: [member1, score1, member2, score2, ...]
local result = {}
for i = 1, math.min(max_results, #scored) do
    table.insert(result, scored[i][1])
    table.insert(result, tostring(scored[i][2]))
end
return result
"""


def cleanup(zset_key, hash_key):
    POPOTO_REDIS_DB.delete(zset_key, hash_key)


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
        result = POPOTO_REDIS_DB.eval(
            "return tostring(math.pow(4.0, -0.5))", 0
        )
        assert abs(float(result) - 0.5) < 0.001

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

    ZSET_KEY = "_test:decay:timestamps"
    HASH_KEY = "_test:decay:base_scores"

    def setup_method(self):
        cleanup(self.ZSET_KEY, self.HASH_KEY)

    def teardown_method(self):
        cleanup(self.ZSET_KEY, self.HASH_KEY)

    def test_single_member(self):
        """One member, verify decay formula output."""
        now = time.time()
        one_day_ago = now - 86400

        POPOTO_REDIS_DB.zadd(self.ZSET_KEY, {"item:1": one_day_ago})
        POPOTO_REDIS_DB.hset(self.HASH_KEY, "item:1", "1.0")

        result = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA, 2,
            self.ZSET_KEY, self.HASH_KEY,
            str(now), "0.5", "10"
        )

        assert len(result) == 2
        member = result[0].decode() if isinstance(result[0], bytes) else result[0]
        score = float(result[1])

        assert member == "item:1"
        # 1.0 * 1.0^(-0.5) = 1.0
        assert abs(score - 1.0) < 0.05

    def test_recent_beats_old(self):
        """Recently updated member should outscore older one with same base."""
        now = time.time()

        POPOTO_REDIS_DB.zadd(self.ZSET_KEY, {
            "recent": now - 3600,       # 1 hour ago
            "old": now - 86400 * 30,    # 30 days ago
        })
        POPOTO_REDIS_DB.hset(self.HASH_KEY, mapping={
            "recent": "1.0",
            "old": "1.0",
        })

        result = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA, 2,
            self.ZSET_KEY, self.HASH_KEY,
            str(now), "0.5", "10"
        )

        # First result should be "recent"
        first = result[0].decode() if isinstance(result[0], bytes) else result[0]
        first_score = float(result[1])
        second = result[2].decode() if isinstance(result[2], bytes) else result[2]
        second_score = float(result[3])

        assert first == "recent"
        assert second == "old"
        assert first_score > second_score

    def test_high_base_beats_low_base(self):
        """Higher base score should win when timestamps are equal."""
        now = time.time()
        same_time = now - 86400  # both 1 day ago

        POPOTO_REDIS_DB.zadd(self.ZSET_KEY, {
            "high": same_time,
            "low": same_time,
        })
        POPOTO_REDIS_DB.hset(self.HASH_KEY, mapping={
            "high": "10.0",
            "low": "1.0",
        })

        result = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA, 2,
            self.ZSET_KEY, self.HASH_KEY,
            str(now), "0.5", "10"
        )

        first = result[0].decode() if isinstance(result[0], bytes) else result[0]
        assert first == "high"
        assert float(result[1]) > float(result[3])

    def test_decay_rate_affects_ranking(self):
        """Higher decay rate penalizes old items more."""
        now = time.time()

        POPOTO_REDIS_DB.zadd(self.ZSET_KEY, {
            "recent": now - 3600,        # 1 hour ago
            "old": now - 86400 * 10,     # 10 days ago
        })
        POPOTO_REDIS_DB.hset(self.HASH_KEY, mapping={
            "recent": "1.0",
            "old": "5.0",  # old has 5x base score
        })

        # With low decay (0.1), old item's high base score can win
        result_low = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA, 2,
            self.ZSET_KEY, self.HASH_KEY,
            str(now), "0.1", "10"
        )

        # With high decay (1.0), recency dominates
        result_high = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA, 2,
            self.ZSET_KEY, self.HASH_KEY,
            str(now), "1.0", "10"
        )

        first_low = result_low[0].decode() if isinstance(result_low[0], bytes) else result_low[0]
        first_high = result_high[0].decode() if isinstance(result_high[0], bytes) else result_high[0]

        # Low decay: base score matters more -> "old" (5.0 base) could win
        # High decay: recency matters more -> "recent" wins
        assert first_high == "recent"

    def test_limit_respected(self):
        """Only top-N results returned."""
        now = time.time()

        members = {}
        base_scores = {}
        for i in range(20):
            key = f"item:{i}"
            members[key] = now - (i * 3600)  # each 1 hour older
            base_scores[key] = "1.0"

        POPOTO_REDIS_DB.zadd(self.ZSET_KEY, members)
        POPOTO_REDIS_DB.hset(self.HASH_KEY, mapping=base_scores)

        result = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA, 2,
            self.ZSET_KEY, self.HASH_KEY,
            str(now), "0.5", "5"
        )

        # 5 results * 2 (member + score) = 10 elements
        assert len(result) == 10

    def test_empty_sorted_set(self):
        """No members returns empty result."""
        result = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA, 2,
            self.ZSET_KEY, self.HASH_KEY,
            str(time.time()), "0.5", "10"
        )
        assert result is None or result == []

    def test_missing_base_score_defaults_to_one(self):
        """Members without a base score in the hash default to 1.0."""
        now = time.time()

        POPOTO_REDIS_DB.zadd(self.ZSET_KEY, {"no_base": now - 86400})
        # Don't set any base score in hash

        result = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA, 2,
            self.ZSET_KEY, self.HASH_KEY,
            str(now), "0.5", "10"
        )

        assert len(result) == 2
        score = float(result[1])
        # 1.0 * 1.0^(-0.5) = 1.0 (1 day elapsed, decay_rate 0.5)
        assert abs(score - 1.0) < 0.05


class TestDecayScoringFormula:
    """Verify the math matches expected ACT-R-style decay."""

    ZSET_KEY = "_test:decay_math:timestamps"
    HASH_KEY = "_test:decay_math:base_scores"

    def setup_method(self):
        cleanup(self.ZSET_KEY, self.HASH_KEY)

    def teardown_method(self):
        cleanup(self.ZSET_KEY, self.HASH_KEY)

    def test_known_values(self):
        """Verify against hand-computed decay values."""
        now = time.time()

        # Set up items at known elapsed times
        cases = {
            "1day": (now - 86400 * 1, 1.0),    # 1 day,  expect: 1.0 * 1^-0.5 = 1.0
            "4day": (now - 86400 * 4, 1.0),     # 4 days, expect: 1.0 * 4^-0.5 = 0.5
            "100day": (now - 86400 * 100, 1.0),  # 100 days, expect: 1.0 * 100^-0.5 = 0.1
        }

        members = {}
        base_scores = {}
        for key, (ts, base) in cases.items():
            members[key] = ts
            base_scores[key] = str(base)

        POPOTO_REDIS_DB.zadd(self.ZSET_KEY, members)
        POPOTO_REDIS_DB.hset(self.HASH_KEY, mapping=base_scores)

        result = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA, 2,
            self.ZSET_KEY, self.HASH_KEY,
            str(now), "0.5", "10"
        )

        # Parse results into dict
        scores = {}
        for i in range(0, len(result), 2):
            key = result[i].decode() if isinstance(result[i], bytes) else result[i]
            scores[key] = float(result[i + 1])

        assert abs(scores["1day"] - 1.0) < 0.05
        assert abs(scores["4day"] - 0.5) < 0.05
        assert abs(scores["100day"] - 0.1) < 0.05

    def test_base_score_scaling(self):
        """Base score multiplies the decay: score=5 at 4 days = 5 * 0.5 = 2.5."""
        now = time.time()

        POPOTO_REDIS_DB.zadd(self.ZSET_KEY, {"scaled": now - 86400 * 4})
        POPOTO_REDIS_DB.hset(self.HASH_KEY, "scaled", "5.0")

        result = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA, 2,
            self.ZSET_KEY, self.HASH_KEY,
            str(now), "0.5", "10"
        )

        score = float(result[1])
        # 5.0 * 4^(-0.5) = 5.0 * 0.5 = 2.5
        assert abs(score - 2.5) < 0.15
