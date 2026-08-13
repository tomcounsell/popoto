"""ExistenceFilter and FrequencySketch — probabilistic data structures as Popoto fields.

ExistenceFilter wraps a Bloom filter implemented with Redis strings (SETBIT/GETBIT)
and Lua scripts. Answers "have I ever stored a record matching this fingerprint?"
in O(1). No Redis modules required — works on both Redis and Valkey.

FrequencySketch wraps a Count-Min Sketch implemented with Redis hashes (HINCRBY/HGET)
and Lua scripts. Provides approximate frequency counting for fingerprints.
No Redis modules required — works on both Redis and Valkey.

Design:
    Both fields are "side-effect fields" — they do not store a value on the model
    instance. They only maintain probabilistic indexes via on_save() hooks. This
    follows the same pattern as SortedFieldMixin maintaining a sorted set index.

    Hash functions:
    ExistenceFilter uses the Kirsch–Mitzenmacher double hashing optimization:
    h_i(x) = (h1(x) + i * h2(x)) mod m, where h1 is DJB2 and h2 is FNV-1.
    Two hash functions simulate k independent Bloom-row hashes.
    FrequencySketch uses independent per-row polynomial hashes: each of the
    depth rows has its own prime multiplier and modulus, forming a practically
    pairwise-independent family that restores the standard CMS error bound.

    Tokenization:
    On save, fingerprint strings are automatically tokenized into individual words.
    Each token is added to the bloom filter / count-min sketch separately. This
    enables word-level queries: saving "kubernetes deployment guide" allows
    might_exist("kubernetes") to return True. Tokenization lowercases, splits on
    non-word characters, filters tokens shorter than 3 characters, and removes
    common English stop words. If tokenization produces zero tokens, the raw
    fingerprint is used as a fallback.

Redis Key Patterns:
    - Bloom filter: $EF:{ClassName}:{field_name} — single Redis string (bit array)
    - Count-Min Sketch: $FS:{ClassName}:{field_name} — single Redis hash

Valkey Compatibility:
    Uses only core Redis commands: SETBIT, GETBIT, HINCRBY, HGET, EVAL.
    No BF.*, CMS.*, or other module commands. Works identically on Redis and Valkey.

Example:
    from popoto import Model, KeyField, Field
    from popoto.fields.existence_filter import ExistenceFilter, FrequencySketch

    class Memory(Model):
        agent_id = KeyField()
        topic = Field(type=str)
        bloom = ExistenceFilter(
            error_rate=0.01,
            capacity=100_000,
            fingerprint_fn=lambda inst: inst.topic,
        )
        freq = FrequencySketch(
            fingerprint_fn=lambda inst: inst.topic,
        )

    # After saving some memories...
    if Memory.bloom.definitely_missing("kubernetes"):
        print("No memories about kubernetes")
    else:
        results = Memory.query.filter(agent_id="agent-1")

    count = Memory.freq.get_frequency("kubernetes")
"""

import logging
import math

import redis

from ..redis_db import POPOTO_REDIS_DB
from .field import Field

logger = logging.getLogger("POPOTO.ExistenceFilter")

# ---------------------------------------------------------------------------
# Tokenization — delegates to shared module
# ---------------------------------------------------------------------------

from ._tokenizer import tokenize  # noqa: F401, E402

# ---------------------------------------------------------------------------
# Lua Scripts — Bloom Filter
# ---------------------------------------------------------------------------

BLOOM_ADD_LUA = """
local key = KEYS[1]
local item = ARGV[1]
local m = tonumber(ARGV[2])
local k = tonumber(ARGV[3])
local LARGE_MOD = 4503599627370496  -- 2^52, safe for Lua doubles

-- Double hashing: h1 (DJB2) and h2 (FNV-1 variant)
local h1 = 5381
local h2 = 16777619
for i = 1, #item do
    local c = string.byte(item, i)
    h1 = ((h1 * 33) + c) % LARGE_MOD
    h2 = ((h2 * 16777619) + c) % LARGE_MOD
end
h1 = h1 % m
h2 = h2 % m

for i = 0, k - 1 do
    local pos = (h1 + i * h2) % m
    redis.call('SETBIT', key, pos, 1)
end
return 1
"""

BLOOM_ADD_MULTI_LUA = """
local key = KEYS[1]
local m = tonumber(ARGV[1])
local k = tonumber(ARGV[2])
local LARGE_MOD = 4503599627370496  -- 2^52, safe for Lua doubles

-- Loop over all tokens passed as ARGV[3..N]
for t = 3, #ARGV do
    local item = ARGV[t]
    local h1 = 5381
    local h2 = 16777619
    for i = 1, #item do
        local c = string.byte(item, i)
        h1 = ((h1 * 33) + c) % LARGE_MOD
        h2 = ((h2 * 16777619) + c) % LARGE_MOD
    end
    h1 = h1 % m
    h2 = h2 % m
    for i = 0, k - 1 do
        local pos = (h1 + i * h2) % m
        redis.call('SETBIT', key, pos, 1)
    end
end
return 1
"""

BLOOM_EXISTS_LUA = """
local key = KEYS[1]
local item = ARGV[1]
local m = tonumber(ARGV[2])
local k = tonumber(ARGV[3])
local LARGE_MOD = 4503599627370496  -- 2^52, safe for Lua doubles

local h1 = 5381
local h2 = 16777619
for i = 1, #item do
    local c = string.byte(item, i)
    h1 = ((h1 * 33) + c) % LARGE_MOD
    h2 = ((h2 * 16777619) + c) % LARGE_MOD
end
h1 = h1 % m
h2 = h2 % m

for i = 0, k - 1 do
    local pos = (h1 + i * h2) % m
    if redis.call('GETBIT', key, pos) == 0 then
        return 0
    end
end
return 1
"""

BLOOM_EXISTS_BATCH_LUA = """
local key = KEYS[1]
local m = tonumber(ARGV[1])
local k = tonumber(ARGV[2])
local LARGE_MOD = 4503599627370496  -- 2^52, safe for Lua doubles

local results = {}
for t = 3, #ARGV do
    local item = ARGV[t]
    local h1 = 5381
    local h2 = 16777619
    for i = 1, #item do
        local c = string.byte(item, i)
        h1 = ((h1 * 33) + c) % LARGE_MOD
        h2 = ((h2 * 16777619) + c) % LARGE_MOD
    end
    h1 = h1 % m
    h2 = h2 % m

    local found = 1
    for i = 0, k - 1 do
        local pos = (h1 + i * h2) % m
        if redis.call('GETBIT', key, pos) == 0 then
            found = 0
            break
        end
    end
    results[#results + 1] = found
end
return results
"""

# ---------------------------------------------------------------------------
# Lua Scripts — Count-Min Sketch
# ---------------------------------------------------------------------------

CMS_INCR_LUA = """
local key = KEYS[1]
local item = ARGV[1]
local w = tonumber(ARGV[2])
local d = tonumber(ARGV[3])
-- Per-row independent polynomial hashes: max intermediate ≈ 2^49 (< 2^53), safe for Lua doubles
local P = {16777259, 16777289, 16777291, 16777331, 16777333, 16777337, 16777381}
local M = {33554467, 33554473, 33554501, 33554503, 33554509, 33554519, 33554527}

for row = 0, d - 1 do
    local pr = P[row + 1]
    local mr = M[row + 1]
    local h = row + 1            -- distinct seed per row
    for i = 1, #item do
        h = (h * mr + string.byte(item, i)) % pr
    end
    local col = h % w
    redis.call('HINCRBY', key, row .. ':' .. col, 1)
end
return 1
"""

CMS_INCR_MULTI_LUA = """
local key = KEYS[1]
local w = tonumber(ARGV[1])
local d = tonumber(ARGV[2])
-- Per-row independent polynomial hashes: max intermediate ≈ 2^49 (< 2^53), safe for Lua doubles
local P = {16777259, 16777289, 16777291, 16777331, 16777333, 16777337, 16777381}
local M = {33554467, 33554473, 33554501, 33554503, 33554509, 33554519, 33554527}

-- Loop over all tokens passed as ARGV[3..N]
for t = 3, #ARGV do
    local item = ARGV[t]
    for row = 0, d - 1 do
        local pr = P[row + 1]
        local mr = M[row + 1]
        local h = row + 1            -- distinct seed per row
        for i = 1, #item do
            h = (h * mr + string.byte(item, i)) % pr
        end
        local col = h % w
        redis.call('HINCRBY', key, row .. ':' .. col, 1)
    end
end
return 1
"""

CMS_QUERY_LUA = """
local key = KEYS[1]
local item = ARGV[1]
local w = tonumber(ARGV[2])
local d = tonumber(ARGV[3])
-- Per-row independent polynomial hashes: max intermediate ≈ 2^49 (< 2^53), safe for Lua doubles
local P = {16777259, 16777289, 16777291, 16777331, 16777333, 16777337, 16777381}
local M = {33554467, 33554473, 33554501, 33554503, 33554509, 33554519, 33554527}

local min_count = nil
for row = 0, d - 1 do
    local pr = P[row + 1]
    local mr = M[row + 1]
    local h = row + 1            -- distinct seed per row
    for i = 1, #item do
        h = (h * mr + string.byte(item, i)) % pr
    end
    local col = h % w
    local val = tonumber(redis.call('HGET', key, row .. ':' .. col)) or 0
    if min_count == nil or val < min_count then
        min_count = val
    end
end
return min_count or 0
"""


def _compute_fingerprint_impl(field, model_instance):
    """Compute fingerprint string from a model instance.

    Shared implementation used by both ExistenceFilter and FrequencySketch.

    Uses the configured fingerprint_fn on the field. Falls back to the model's
    redis_key if no fingerprint_fn is set.

    Args:
        field: The field instance (ExistenceFilter or FrequencySketch).
        model_instance: The model instance to fingerprint.

    Returns:
        str: The fingerprint string.

    Raises:
        ValueError: If fingerprint_fn returns None.
    """
    if field.fingerprint_fn is not None:
        result = field.fingerprint_fn(model_instance)
    else:
        result = model_instance.db_key.redis_key
    if result is None:
        raise ValueError(
            f"fingerprint_fn returned None for {model_instance}. "
            f"fingerprint_fn must return a string."
        )
    return str(result)


class ExistenceFilter(Field):
    """Bloom filter for O(1) probabilistic membership checks.

    Implemented with Redis SETBIT/GETBIT and Lua scripts.
    No Redis modules required -- works on both Redis and Valkey.

    ExistenceFilter is a "side-effect field" that does not store a value on the
    model instance. It maintains a Bloom filter index via on_save() hooks. On
    query, might_exist() checks the filter; definitely_missing() is its inverse.

    False positives are possible (might_exist returns True for an item never
    added). False negatives are impossible (definitely_missing never returns
    True for an item that was added).

    Args:
        error_rate: Target false positive rate. Default 0.01 (1%).
        capacity: Expected number of distinct items. Default 100,000.
        fingerprint_fn: Callable that takes a model instance and returns a
            string fingerprint. This is required -- there is no default.

    Redis Key:
        ``$EF:{ClassName}:{field_name}`` -- single Redis string used as bit array.

    Example:
        class Memory(Model):
            topic = Field(type=str)
            bloom = ExistenceFilter(
                error_rate=0.01,
                capacity=100_000,
                fingerprint_fn=lambda inst: inst.topic,
            )

        memory = Memory(topic="kubernetes")
        memory.save()

        Memory.bloom.might_exist(Memory, "kubernetes")  # True
        Memory.bloom.definitely_missing(Memory, "new-topic")  # True
    """

    # Override Field defaults -- ExistenceFilter does not store a value
    type: type = str
    null: bool = True
    default = None

    def __init__(self, **kwargs):
        self.error_rate = kwargs.pop("error_rate", 0.01)
        self.capacity = kwargs.pop("capacity", 100_000)
        self.fingerprint_fn = kwargs.pop("fingerprint_fn", None)
        # Do not pass our custom kwargs to Field.__init__
        super().__init__(**kwargs)

    def _compute_params(self):
        """Derive Bloom filter parameters from error_rate and capacity.

        Returns:
            Tuple of (m, k) where m is total bits and k is number of hash functions.
        """
        # m = -capacity * ln(error_rate) / (ln(2)^2)
        m = int(-self.capacity * math.log(self.error_rate) / (math.log(2) ** 2))
        # k = (m / capacity) * ln(2)
        k = max(1, int((m / self.capacity) * math.log(2)))
        return m, k

    def _compute_fingerprint(self, model_instance):
        """Compute fingerprint string from a model instance.

        Delegates to the module-level _compute_fingerprint_impl helper.
        """
        return _compute_fingerprint_impl(self, model_instance)

    def _bloom_key(self, model_instance):
        """Build the Redis key for this Bloom filter.

        Returns:
            str: Key like ``$EF:{ClassName}:{field_name}``.
        """
        class_name = type(model_instance).__name__
        return f"$EF:{class_name}:{self.name}"

    @classmethod
    def on_save(
        cls,
        model_instance,
        field_name,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """Add the model instance's fingerprint tokens to the Bloom filter.

        Called automatically by Model.save() for each field. Computes the
        fingerprint, tokenizes it into individual words, and runs
        BLOOM_ADD_MULTI_LUA to set bits for each token. If tokenization
        produces no tokens, falls back to adding the raw fingerprint.

        Args:
            model_instance: The model instance being saved.
            field_name: Name of this field on the model.
            field_value: Current field value (unused -- ExistenceFilter is side-effect only).
            pipeline: Optional Redis pipeline for batch operations.
            **kwargs: Additional context.

        Returns:
            The pipeline if provided, otherwise the Lua script result.
        """
        field = model_instance._meta.fields[field_name]
        fingerprint = field._compute_fingerprint(model_instance)
        key = field._bloom_key(model_instance)
        m, k = field._compute_params()
        client = (
            pipeline if isinstance(pipeline, redis.client.Pipeline) else POPOTO_REDIS_DB
        )
        tokens = tokenize(fingerprint)
        if not tokens:
            # Fallback: add the raw fingerprint lowercased (handles empty strings,
            # short tokens, redis keys, etc.)
            client.eval(BLOOM_ADD_LUA, 1, key, fingerprint.lower(), m, k)
        else:
            client.eval(BLOOM_ADD_MULTI_LUA, 1, key, m, k, *tokens)
        return pipeline if pipeline else None

    @classmethod
    def on_delete(
        cls,
        model_instance,
        field_name,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """No-op. Bloom filters do not support removal by design.

        Bloom filters guarantee zero false negatives. Removing items would
        violate this guarantee because multiple items may share bit positions.
        Stale positives are harmless for a pre-filter use case.

        Returns:
            The pipeline if provided, otherwise None.
        """
        return pipeline if pipeline else None

    def might_exist(self, model_class, fingerprint):
        """Check if a fingerprint might exist in the Bloom filter.

        The query is tokenized using the same rules as on_save(). If the query
        produces tokens, returns True if ANY token is found in the filter.
        If tokenization produces no tokens, checks the raw lowercased query
        (matching the on_save fallback behavior).

        Returns True if the fingerprint is possibly in the set (may be a false
        positive). Returns False if the fingerprint is definitely not in the set
        (guaranteed correct).

        Args:
            model_class: The Model class to check against.
            fingerprint: The fingerprint string to look up.

        Returns:
            bool: True if possibly present, False if definitely absent.
        """
        key = f"$EF:{model_class.__name__}:{self.name}"
        m, k = self._compute_params()
        query_str = str(fingerprint)
        tokens = tokenize(query_str)
        if not tokens:
            # Fallback: check the raw lowercased query (matches on_save fallback)
            result = POPOTO_REDIS_DB.eval(
                BLOOM_EXISTS_LUA, 1, key, query_str.lower(), m, k
            )
            return bool(result)
        # Check if ANY token is in the bloom filter
        for token in tokens:
            result = POPOTO_REDIS_DB.eval(BLOOM_EXISTS_LUA, 1, key, token, m, k)
            if bool(result):
                return True
        return False

    def definitely_missing(self, model_class, fingerprint):
        """Check if a fingerprint is definitely not in the Bloom filter.

        Convenience inverse of might_exist(). When this returns True, the caller
        can skip expensive retrieval entirely.

        Args:
            model_class: The Model class to check against.
            fingerprint: The fingerprint string to look up.

        Returns:
            bool: True if definitely absent, False if possibly present.
        """
        return not self.might_exist(model_class, fingerprint)

    def fill_ratio(self, model_class):
        """Diagnostic: compute the proportion of set bits in the Bloom filter.

        Useful for monitoring capacity usage. When fill_ratio approaches 0.5,
        the false positive rate starts degrading beyond the configured error_rate.

        Args:
            model_class: The Model class whose Bloom filter to inspect.

        Returns:
            float: Ratio of set bits to total bits (0.0 to 1.0).
                Returns 0.0 if the Bloom filter key doesn't exist yet.
        """
        key = f"$EF:{model_class.__name__}:{self.name}"
        m, _ = self._compute_params()
        set_bits = POPOTO_REDIS_DB.bitcount(key)
        return set_bits / m if m > 0 else 0.0

    def might_exist_batch(self, model_class, fingerprints):
        """Check multiple fingerprints against the Bloom filter in one round-trip.

        Each fingerprint is tokenized using the same rules as might_exist().
        A fingerprint is considered a hit if ANY of its tokens is found in the
        filter. Uses a single Lua EVAL call for all tokens, then maps results
        back to the original fingerprints.

        Args:
            model_class: The Model class to check against.
            fingerprints: List of fingerprint strings to check.

        Returns:
            dict[str, bool]: Mapping of fingerprint -> might_exist result.
                Returns empty dict for empty input list.
        """
        if not fingerprints:
            return {}

        key = f"$EF:{model_class.__name__}:{self.name}"
        m, k = self._compute_params()

        # Tokenize each fingerprint and build a flat token list with a mapping
        # back to the original fingerprint.
        all_tokens = []
        # Maps: token index in all_tokens -> list of fingerprint strings
        token_to_fingerprints = []
        fingerprint_order = []
        seen_fingerprints = set()

        for fp in fingerprints:
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)
            fingerprint_order.append(fp)

            query_str = str(fp)
            tokens = tokenize(query_str)
            if not tokens:
                # Fallback: use raw lowercased query (matches might_exist behavior)
                tokens = [query_str.lower()]

            for token in tokens:
                all_tokens.append(token)
                token_to_fingerprints.append(fp)

        if not all_tokens:
            return {fp: False for fp in fingerprint_order}

        # Single Lua EVAL for all tokens
        raw_results = POPOTO_REDIS_DB.eval(
            BLOOM_EXISTS_BATCH_LUA, 1, key, m, k, *all_tokens
        )

        # Map token results back to fingerprints (ANY token hit = fingerprint hit)
        result = {fp: False for fp in fingerprint_order}
        for i, token_result in enumerate(raw_results):
            if bool(token_result):
                result[token_to_fingerprints[i]] = True

        return result

    def might_exist_count(self, model_class, fingerprints):
        """Count how many fingerprints might exist in a single round-trip.

        Convenience wrapper around might_exist_batch(). Returns the number
        of fingerprints for which might_exist would return True.

        Args:
            model_class: The Model class to check against.
            fingerprints: List of fingerprint strings to check.

        Returns:
            int: Number of fingerprints that might exist.
        """
        batch_result = self.might_exist_batch(model_class, fingerprints)
        return sum(1 for v in batch_result.values() if v)


class FrequencySketch(Field):
    """Count-Min Sketch for approximate frequency queries.

    Implemented with Redis hashes and Lua scripts.
    No Redis modules required -- works on both Redis and Valkey.

    FrequencySketch is a "side-effect field" that maintains a Count-Min Sketch
    alongside the model. Each save increments counters for the fingerprint.
    get_frequency() returns the approximate count.

    The Count-Min Sketch may overcount (never undercount). The error bound
    depends on width and depth parameters.

    Args:
        width: Number of counters per row. Default 2003 (prime, minimises hash collision).
        depth: Number of hash functions (rows). Default 7.
        fingerprint_fn: Callable that takes a model instance and returns a
            string fingerprint. This is required -- there is no default.

    Redis Key:
        ``$FS:{ClassName}:{field_name}`` -- single Redis hash with
        ``row:column`` field names and integer counter values.

    Example:
        class Memory(Model):
            topic = Field(type=str)
            freq = FrequencySketch(
                fingerprint_fn=lambda inst: inst.topic,
            )

        memory = Memory(topic="kubernetes")
        memory.save()
        memory.save()  # increment again

        Memory.freq.get_frequency(Memory, "kubernetes")  # ~2
    """

    # Override Field defaults -- FrequencySketch does not store a value
    MAX_DEPTH = 7
    type: type = str
    null: bool = True
    default = None

    def __init__(self, **kwargs):
        self.width = kwargs.pop("width", 2003)
        self.depth = kwargs.pop("depth", 7)
        self.fingerprint_fn = kwargs.pop("fingerprint_fn", None)
        super().__init__(**kwargs)
        if not (1 <= self.depth <= self.MAX_DEPTH):
            raise ValueError(
                f"FrequencySketch depth must be between 1 and {self.MAX_DEPTH}, got {self.depth}. "
                f"The P/M hash tables have {self.MAX_DEPTH} entries."
            )

    def _compute_fingerprint(self, model_instance):
        """Compute fingerprint string from a model instance.

        Delegates to the module-level _compute_fingerprint_impl helper.
        """
        return _compute_fingerprint_impl(self, model_instance)

    def _cms_key(self, model_instance):
        """Build the Redis key for this Count-Min Sketch.

        Returns:
            str: Key like ``$FS:{ClassName}:{field_name}``.
        """
        class_name = type(model_instance).__name__
        return f"$FS:{class_name}:{self.name}"

    @classmethod
    def on_save(
        cls,
        model_instance,
        field_name,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """Increment the Count-Min Sketch counters for this instance's fingerprint tokens.

        Called automatically by Model.save() for each field. Tokenizes the
        fingerprint and increments counters for each token individually.
        If tokenization produces no tokens, falls back to the raw fingerprint.

        Args:
            model_instance: The model instance being saved.
            field_name: Name of this field on the model.
            field_value: Current field value (unused).
            pipeline: Optional Redis pipeline for batch operations.
            **kwargs: Additional context.

        Returns:
            The pipeline if provided, otherwise the Lua script result.
        """
        field = model_instance._meta.fields[field_name]
        fingerprint = field._compute_fingerprint(model_instance)
        key = field._cms_key(model_instance)
        client = (
            pipeline if isinstance(pipeline, redis.client.Pipeline) else POPOTO_REDIS_DB
        )
        tokens = tokenize(fingerprint)
        if not tokens:
            # Fallback: increment the raw fingerprint lowercased
            client.eval(
                CMS_INCR_LUA, 1, key, fingerprint.lower(), field.width, field.depth
            )
        else:
            client.eval(CMS_INCR_MULTI_LUA, 1, key, field.width, field.depth, *tokens)
        return pipeline if pipeline else None

    @classmethod
    def on_delete(
        cls,
        model_instance,
        field_name,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """No-op. Count-Min Sketch does not support decrement.

        CMS counters are monotonically increasing. Decrementing would
        violate the "never undercount" guarantee.

        Returns:
            The pipeline if provided, otherwise None.
        """
        return pipeline if pipeline else None

    def get_frequency(self, model_class, fingerprint):
        """Query the approximate frequency of a fingerprint.

        The query is tokenized using the same rules as on_save(). If the query
        produces tokens, returns the minimum frequency across those tokens
        (conservative estimate). If tokenization produces no tokens, queries
        the raw lowercased fingerprint (matching the on_save fallback).

        Returns the Count-Min Sketch estimate. This value may overcount
        but never undercounts.

        Args:
            model_class: The Model class to query against.
            fingerprint: The fingerprint string to look up.

        Returns:
            int: Approximate frequency count. Returns 0 if the fingerprint
                has never been seen or the CMS key doesn't exist yet.
        """
        key = f"$FS:{model_class.__name__}:{self.name}"
        query_str = str(fingerprint)
        tokens = tokenize(query_str)
        if not tokens:
            # Fallback: query the raw lowercased fingerprint
            result = POPOTO_REDIS_DB.eval(
                CMS_QUERY_LUA, 1, key, query_str.lower(), self.width, self.depth
            )
            return int(result) if result else 0
        # For multi-token queries, return min frequency (conservative)
        min_freq = None
        for token in tokens:
            result = POPOTO_REDIS_DB.eval(
                CMS_QUERY_LUA, 1, key, token, self.width, self.depth
            )
            freq = int(result) if result else 0
            if min_freq is None or freq < min_freq:
                min_freq = freq
        return min_freq if min_freq is not None else 0
