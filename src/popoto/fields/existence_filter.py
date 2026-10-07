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
    h_i(x) = (h1(x) + i * h2(x)) mod m. Two hash functions simulate k
    independent Bloom-row hashes. The pair is versioned per filter (#775):
    v2 (every filter created since) uses 32-bit FNV-1a over the bytes forward
    and reversed, with all arithmetic exact in Lua doubles; v1 (DJB2 plus an
    FNV-1-shaped multiply) lost low-order bits past 2^53 and is kept only so
    filters built with it keep answering for the tokens they hold, until
    ``rebuild_indexes()`` rebuilds them as v2. See ``BLOOM_V2_HEADER``.
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
    - Bloom filter: $EF:{ClassName}:{field_name} — single Redis string (bit array,
      followed by a 4-byte version marker on v2 filters)
    - Bloom rebuild staging: $EF:{ClassName}:{field_name}:rebuild — exists only
      while rebuild_indexes() converts a v1 filter
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
from typing import Any

import redis

from ..redis_db import get_REDIS_DB, run_lua
from .field import Field

logger = logging.getLogger("POPOTO.ExistenceFilter")

# ---------------------------------------------------------------------------
# Tokenization — delegates to shared module
# ---------------------------------------------------------------------------

from ._tokenizer import tokenize  # noqa: F401, E402

# ---------------------------------------------------------------------------
# Lua Scripts — Bloom Filter
# ---------------------------------------------------------------------------

#: Hash version a filter is written with (#775). A filter is a plain Redis
#: string; version 2 marks itself with these four bytes placed immediately
#: after its bit array, at byte offset ``ceil(m / 8)``. Version 1 (every
#: filter written before #775) has no marker and needs none: its hash only
#: ever produces positions ``< m``, so its string never reaches that offset,
#: and a missing marker can only mean v1. The marker lives *inside* the
#: filter's own key, so it travels with the bits through ``DUMP``/``RESTORE``,
#: ``RENAME``, replication and RDB/AOF -- nothing can separate the version
#: from the bits it describes, which is what makes a lost marker (and so a
#: v2 filter read with the v1 hash, i.e. false negatives) impossible.
BLOOM_HASH_VERSION = 2
BLOOM_V2_HEADER = b"\x89EF\x02"

#: Suffix of the staging key ``rebuild_indexes()`` builds a v2 filter in
#: before swapping it over a v1 one. It sits under the same ``$EF:{Class}:``
#: prefix, so tooling that classifies keys by family (the #756 migration's
#: inventory) sees it as part of the existence filter.
BLOOM_REBUILD_SUFFIX = ":rebuild"


def bloom_header_offset(m: int) -> int:
    """Byte offset of the v2 version marker: the first byte past ``m`` bits."""
    return (m + 7) // 8


# Shared Lua prelude for the four bloom scripts. Lua numbers are IEEE
# doubles, exact only below 2^53, and Redis/Valkey script Lua is 5.1 with no
# guaranteed integer type -- so the v2 hash below never forms a value at or
# above 2^53, and needs no ``bit`` library.
_BLOOM_LUA_LIB = r"""
local EF_V2_HEADER = '\137EF\2'

-- v1 (legacy): DJB2 + an FNV-1-shaped multiply, reduced mod 2^52. Both
-- products pass 2^53 (h1 * 33 after a few bytes, h2 * 16777619 from the
-- first), so low-order bits are rounded away and similar tokens collapse
-- onto a handful of positions (#775: 400 similar tokens set 4 of 66 bits).
-- It is kept operation for operation: every filter written before #775
-- holds exactly these positions, and changing any step would make tokens
-- already added test absent.
local function ef_positions_v1(item, m, k)
    local LARGE_MOD = 4503599627370496  -- 2^52
    local h1 = 5381
    local h2 = 16777619
    for i = 1, #item do
        local c = string.byte(item, i)
        h1 = ((h1 * 33) + c) % LARGE_MOD
        h2 = ((h2 * 16777619) + c) % LARGE_MOD
    end
    h1 = h1 % m
    h2 = h2 % m
    local pos = {}
    for i = 0, k - 1 do
        pos[i + 1] = (h1 + i * h2) % m
    end
    return pos
end

-- v2: two 32-bit FNV-1a hashes, over the bytes forward (h1) and reversed
-- (h2), combined by Kirsch-Mitzenmacher double hashing. Every value stays
-- below 2^42, so the arithmetic is exact.
local function ef_xor8(a, b)
    local r, p = 0, 1
    for _ = 1, 8 do
        local x, y = a % 2, b % 2
        if x ~= y then r = r + p end
        a = (a - x) / 2
        b = (b - y) / 2
        p = p * 2
    end
    return r
end

local function ef_fnv1a_step(h, c)
    -- h ^= c touches only the low byte; then h * 16777619 mod 2^32, split as
    -- h * 403 + h * 2^24 (mod 2^32 the second term is (h mod 2^8) * 2^24).
    local lo = h % 256
    h = h - lo + ef_xor8(lo, c)
    return (h * 403 + (h % 256) * 16777216) % 4294967296
end

local function ef_positions_v2(item, m, k)
    local n = #item
    local h1, h2 = 2166136261, 2166136261
    for i = 1, n do
        h1 = ef_fnv1a_step(h1, string.byte(item, i))
        h2 = ef_fnv1a_step(h2, string.byte(item, n + 1 - i))
    end
    local a = h1 % m
    -- A step of 0 would put all k probes on one bit; keep it in [1, m - 1].
    local b = 0
    if m > 1 then b = h2 % (m - 1) + 1 end
    local pos = {}
    for i = 0, k - 1 do
        pos[i + 1] = (a + (i * b) % m) % m
    end
    return pos
end

local function ef_header_offset(m)
    return math.floor((m + 7) / 8)
end

-- The filter's version from its own bytes: the v2 marker, else v1. An
-- absent key reads as v1 here, which is harmless for a read (every bit is
-- 0); the add scripts check EXISTS and create a missing key as v2.
local function ef_is_v2(key, m)
    local hb = ef_header_offset(m)
    return redis.call('GETRANGE', key, hb, hb + 3) == EF_V2_HEADER
end

local function ef_positions(v2, item, m, k)
    if v2 then
        return ef_positions_v2(item, m, k)
    end
    return ef_positions_v1(item, m, k)
end
"""

# Shared by the two add scripts: KEYS[1] is the filter, KEYS[2] its rebuild
# staging key. A missing filter is created as v2 (marker first, so it is
# never observable without one); an existing filter keeps the hash it was
# built with. While a rebuild has a staging key open, every add also writes
# the token's v2 bits there, so a save racing the rebuild is not lost when
# the staging key replaces the filter.
_BLOOM_ADD_PRELUDE = """
local key = KEYS[1]
local staging = KEYS[2]
local v2 = ef_is_v2(key, m)
if not v2 and redis.call('EXISTS', key) == 0 then
    redis.call('SETRANGE', key, ef_header_offset(m), EF_V2_HEADER)
    v2 = true
end
local stage = redis.call('EXISTS', staging) == 1

local function ef_add(item)
    for _, p in ipairs(ef_positions(v2, item, m, k)) do
        redis.call('SETBIT', key, p, 1)
    end
    if stage then
        for _, p in ipairs(ef_positions_v2(item, m, k)) do
            redis.call('SETBIT', staging, p, 1)
        end
    end
end
"""

BLOOM_ADD_LUA = (
    _BLOOM_LUA_LIB
    + """
local item = ARGV[1]
local m = tonumber(ARGV[2])
local k = tonumber(ARGV[3])
"""
    + _BLOOM_ADD_PRELUDE
    + """
ef_add(item)
return 1
"""
)

BLOOM_ADD_MULTI_LUA = (
    _BLOOM_LUA_LIB
    + """
local m = tonumber(ARGV[1])
local k = tonumber(ARGV[2])
"""
    + _BLOOM_ADD_PRELUDE
    + """
-- Loop over all tokens passed as ARGV[3..N]
for t = 3, #ARGV do
    ef_add(ARGV[t])
end
return 1
"""
)

BLOOM_EXISTS_LUA = _BLOOM_LUA_LIB + """
local key = KEYS[1]
local item = ARGV[1]
local m = tonumber(ARGV[2])
local k = tonumber(ARGV[3])

for _, p in ipairs(ef_positions(ef_is_v2(key, m), item, m, k)) do
    if redis.call('GETBIT', key, p) == 0 then
        return 0
    end
end
return 1
"""

BLOOM_EXISTS_BATCH_LUA = _BLOOM_LUA_LIB + """
local key = KEYS[1]
local m = tonumber(ARGV[1])
local k = tonumber(ARGV[2])
local v2 = ef_is_v2(key, m)

local results = {}
for t = 3, #ARGV do
    local found = 1
    for _, p in ipairs(ef_positions(v2, ARGV[t], m, k)) do
        if redis.call('GETBIT', key, p) == 0 then
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


def _search_backend(model_class: Any) -> Any:
    """The model's backend when it is not Redis (#759 M2b), else ``None``.

    On Postgres both fields are exact tables (plan §1.1): ``might_exist`` has
    no false positives and ``get_frequency`` no over-count, where the bloom
    and count-min sketch below allow both. The Redis paths never consult it.
    """
    from ..backends import get_backend

    backend = get_backend(model_class)
    return None if backend.name == "redis" else backend


def _query_tokens(fingerprint: Any) -> list[str]:
    """The tokens a query checks: ``tokenize``, else the raw lowercased
    string -- what ``on_save`` stores as its fallback."""
    query_str = str(fingerprint)
    return tokenize(query_str) or [query_str.lower()]


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

    Hash version (#775):
        A filter created now is v2 and carries a version marker after its
        bit array. A filter built before #775 is v1 and keeps answering with
        the v1 hash, so no token it holds tests absent; new saves into it
        also stay v1. ``hash_version()`` says which one a filter is,
        ``check_indexes()`` lists v1 filters under ``legacy_hash``, and
        ``rebuild_indexes()`` rebuilds a v1 filter from the records as v2,
        swapping it in atomically.

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

    # Export/import: the bit array is deliberately NOT carried, and #556
    # settled that as a permanent contract rather than pending work. A bit is
    # not decomposable to a record: hash collisions mean several records set
    # the same bit, which is exactly why on_delete() is a no-op here. But
    # unlike the mutation stream, the destination does not need the source's
    # array -- importing a record calls on_save(), which sets that record's
    # own bits. A filter rebuilt over the imported set is *more* accurate
    # than a carried one, because it carries no bits for records that were
    # never imported. Rebuild is the better answer, not the fallback.
    roundtrip_policy: str = "partial"
    roundtrip_note: str = (
        "Bloom filter bits are not carried; the destination's filter is "
        "rebuilt by the imported records' own saves, which is more accurate "
        "than carrying bits set by records outside the export. Permanent "
        "contract, not pending work."
    )

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

    def _class_bloom_key(self, model_class) -> str:
        """``$EF:{ClassName}:{field_name}`` from the model class."""
        return f"$EF:{model_class.__name__}:{self.name}"  # type: ignore[attr-defined]

    def hash_version(self, model_class) -> "int | None":
        """The hash version this field's Redis filter is built with (#775).

        ``2`` for a filter created since #775, ``1`` for a legacy filter
        (still answered with the hash it was built with, never with v2's),
        ``None`` when the filter does not exist yet -- the next save creates
        it as v2. Always ``None`` off Redis: Postgres keeps an exact token
        table, not a bit array, so it has no hash to version.
        """
        if _search_backend(model_class) is not None:
            return None
        key = self._class_bloom_key(model_class)
        m, _k = self._compute_params()
        client = get_REDIS_DB()
        hb = bloom_header_offset(m)
        if client.getrange(key, hb, hb + len(BLOOM_V2_HEADER) - 1) == BLOOM_V2_HEADER:
            return 2
        return 1 if client.exists(key) else None

    def _begin_v2_rebuild(self, model_class) -> "str | None":
        """Open a v2 staging key when this field's filter is legacy v1.

        Returns the staging key (the caller re-saves every record, which
        dual-writes v2 bits into it, then calls :meth:`_finish_v2_rebuild`),
        or ``None`` when there is nothing to convert. A staging key left by
        an interrupted rebuild is discarded and started over.
        """
        if self.hash_version(model_class) != 1:
            return None
        staging = self._class_bloom_key(model_class) + BLOOM_REBUILD_SUFFIX
        m, _k = self._compute_params()
        client = get_REDIS_DB()
        pipe = client.pipeline(transaction=True)
        pipe.delete(staging)
        pipe.setrange(staging, bloom_header_offset(m), BLOOM_V2_HEADER)
        pipe.execute()
        return staging

    def _finish_v2_rebuild(self, model_class, staging: str) -> None:
        """Swap the finished v2 staging key over the v1 filter in one RENAME.

        Readers see the complete v1 filter up to this command and the
        complete v2 filter from it on -- never a partly built one, so a
        rebuild opens no false-negative window.
        """
        get_REDIS_DB().rename(staging, self._class_bloom_key(model_class))

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
        staging = key + BLOOM_REBUILD_SUFFIX
        m, k = field._compute_params()
        client = (
            pipeline if isinstance(pipeline, redis.client.Pipeline) else get_REDIS_DB()
        )
        tokens = tokenize(fingerprint)
        if not tokens:
            # Fallback: add the raw fingerprint lowercased (handles empty strings,
            # short tokens, redis keys, etc.)
            run_lua(client, BLOOM_ADD_LUA, 2, key, staging, fingerprint.lower(), m, k)
        else:
            run_lua(client, BLOOM_ADD_MULTI_LUA, 2, key, staging, m, k, *tokens)
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
        backend = _search_backend(model_class)
        if backend is not None:
            return backend.membership_query(
                model_class._meta.spec,
                self.name,  # type: ignore[attr-defined]
                _query_tokens(fingerprint),
                mode="any",
            )
        key = f"$EF:{model_class.__name__}:{self.name}"
        m, k = self._compute_params()
        query_str = str(fingerprint)
        tokens = tokenize(query_str)
        if not tokens:
            # Fallback: check the raw lowercased query (matches on_save fallback)
            result = run_lua(
                get_REDIS_DB(), BLOOM_EXISTS_LUA, 1, key, query_str.lower(), m, k
            )
            return bool(result)
        # Check if ANY token is in the bloom filter
        for token in tokens:
            result = run_lua(get_REDIS_DB(), BLOOM_EXISTS_LUA, 1, key, token, m, k)
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
        m, k = self._compute_params()
        backend = _search_backend(model_class)
        if backend is not None:
            # Postgres has no bit array. Report the fill a bloom of these
            # parameters would have after the distinct tokens actually stored,
            # 1 - e^(-k n / m): the same capacity signal, 0.0 when empty.
            n = backend.membership_size(model_class._meta.spec, self.name)  # type: ignore[attr-defined]
            return 1.0 - math.exp(-k * n / m) if m > 0 else 0.0
        if m <= 0:
            return 0.0
        key = f"$EF:{model_class.__name__}:{self.name}"
        # Count only the bit array, bytes [0, ceil(m/8)): a v2 filter's
        # version marker follows it and is not filter state. A v1 filter's
        # string never reaches that offset, so the range is all of it.
        set_bits = get_REDIS_DB().bitcount(key, 0, bloom_header_offset(m) - 1)
        return set_bits / m

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

        backend = _search_backend(model_class)
        if backend is not None:
            # Postgres: one exact lookup for every token.
            raw_results = backend.membership_query(
                model_class._meta.spec, self.name, all_tokens, mode="each"  # type: ignore[attr-defined]
            )
        else:
            # Single Lua EVAL for all tokens
            raw_results = run_lua(
                get_REDIS_DB(), BLOOM_EXISTS_BATCH_LUA, 1, key, m, k, *all_tokens
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

    # Export/import: the sketch counters are deliberately NOT carried, and
    # #556 settled that as a permanent contract rather than pending work.
    # A counter is shared by every value that hashes to it, so no record's
    # contribution can be isolated -- the same reason on_delete() is a no-op.
    # What the destination gets instead is a sketch rebuilt from the imported
    # saves: frequencies are lower than the source's, because the source
    # counted saves the destination never received. That is the honest
    # reading -- "how often has this been seen *here*" -- and it is why the
    # counters are not carried rather than merely not yet carried.
    roundtrip_policy: str = "partial"
    roundtrip_note: str = (
        "Count-Min Sketch counters are not carried; the destination's sketch "
        "counts only the saves it observes, so frequencies restart from the "
        "import rather than continuing the source's totals. Permanent "
        "contract, not pending work."
    )

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
            pipeline if isinstance(pipeline, redis.client.Pipeline) else get_REDIS_DB()
        )
        tokens = tokenize(fingerprint)
        if not tokens:
            # Fallback: increment the raw fingerprint lowercased
            run_lua(
                client,
                CMS_INCR_LUA,
                1,
                key,
                fingerprint.lower(),
                field.width,
                field.depth,
            )
        else:
            run_lua(
                client, CMS_INCR_MULTI_LUA, 1, key, field.width, field.depth, *tokens
            )
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
        backend = _search_backend(model_class)
        if backend is not None:
            counts = backend.membership_query(
                model_class._meta.spec,
                self.name,  # type: ignore[attr-defined]
                _query_tokens(fingerprint),
                mode="count",
            )
            return min(counts) if counts else 0
        key = f"$FS:{model_class.__name__}:{self.name}"
        query_str = str(fingerprint)
        tokens = tokenize(query_str)
        if not tokens:
            # Fallback: query the raw lowercased fingerprint
            result = run_lua(
                get_REDIS_DB(),
                CMS_QUERY_LUA,
                1,
                key,
                query_str.lower(),
                self.width,
                self.depth,
            )
            return int(result) if result else 0
        # For multi-token queries, return min frequency (conservative)
        min_freq = None
        for token in tokens:
            result = run_lua(
                get_REDIS_DB(), CMS_QUERY_LUA, 1, key, token, self.width, self.depth
            )
            freq = int(result) if result else 0
            if min_freq is None or freq < min_freq:
                min_freq = freq
        return min_freq if min_freq is not None else 0
