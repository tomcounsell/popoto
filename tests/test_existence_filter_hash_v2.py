"""ExistenceFilter hash v2 and the legacy v1 filters it must keep serving (#775).

The pre-#775 bloom hash did its arithmetic in Lua doubles past 2^53, so
similar tokens collapsed onto a handful of positions (400 of them set 4 of
66 bits). v2 keeps every intermediate exact. Changing the hash in place would
make every token already added to a live filter test absent, so the version
is stored per filter, in the filter's own bytes, and a v1 filter keeps its
hash until ``rebuild_indexes(bloom_hash_version=2)`` converts it.

v2 is **opt-in** (PR #801 review): pre-#775 code reads every filter with the
v1 hash, so by default a new filter is still v1 and ``rebuild_indexes()``
keeps each filter's version. A conversion holds a per-filter lock (renewed
by a background thread for as long as it is held), fills a fixed-name
staging key the lock's token owns, and swaps it in with a compare-and-rename.

These tests are Redis-path tests (the bit array has no Postgres counterpart;
Postgres stores an exact token table). The cross-backend contract -- no false
negatives, bounded false positives -- is pinned in the conformance module
``test_existence_filter.py``.

The v1 filters here are built -- and read, for the mixed-version tests -- by
the **pre-#775 scripts themselves**, copied verbatim below from
``src/popoto/fields/existence_filter.py`` as of ``origin/main`` before #775
(``c0fbf79e``). Do not edit them: they are the old code, frozen,
and every compatibility assertion is only as good as their fidelity.
"""

import io
import math
import os
import random
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from src import popoto
from src.popoto.fields import existence_filter as ef
from src.popoto.fields.constants import Defaults
from src.popoto.fields.existence_filter import (
    BLOOM_REBUILD_STAGING_SUFFIX,
    BLOOM_REBUILD_SUFFIX,
    BLOOM_V2_HEADER,
    BloomLockRenewer,
    BloomRebuildInProgressError,
    BloomRebuildLostLockError,
    ExistenceFilter,
    bloom_header_offset,
)
from src.popoto.redis_db import get_REDIS_DB, run_lua

# ---------------------------------------------------------------------------
# The pre-#775 scripts, verbatim (the old code)
# ---------------------------------------------------------------------------

LEGACY_BLOOM_ADD_LUA = """
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

LEGACY_BLOOM_ADD_MULTI_LUA = """
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

LEGACY_BLOOM_EXISTS_LUA = """
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

LEGACY_BLOOM_EXISTS_BATCH_LUA = """
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
# Python reference for v2: 32-bit FNV-1a forward and over the reversed bytes
# ---------------------------------------------------------------------------


def fnv1a32(data: bytes) -> int:
    h = 2166136261
    for c in data:
        h ^= c
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def v2_positions(token: str, m: int, k: int) -> list:
    data = token.encode("utf-8")
    a = fnv1a32(data) % m
    b = fnv1a32(data[::-1]) % (m - 1) + 1 if m > 1 else 0
    return [(a + i * b) % m for i in range(k)]


def set_positions(raw: bytes, m: int) -> set:
    """Set bit positions < m of a Redis bitmap (bit 0 is the MSB of byte 0)."""
    out = set()
    for byte_index, byte in enumerate(raw[: bloom_header_offset(m)]):
        for bit in range(8):
            if byte & (0x80 >> bit):
                pos = byte_index * 8 + bit
                if pos < m:
                    out.add(pos)
    return out


def expected_set_bits(m: int, k: int, n: int) -> "tuple[float, float]":
    """Mean and standard deviation of set bits after n distinct inserts."""
    p = 1 - math.exp(-k * n / m)
    return m * p, math.sqrt(m * p * (1 - p))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class HashV2Doc(popoto.Model):
    """capacity 1,000 at 5%: m = 6,235 bits, k = 4."""

    name = popoto.UniqueKeyField()
    topic = popoto.Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.05,
        capacity=1000,
        fingerprint_fn=lambda inst: inst.topic,
        hash_version=2,
    )


class HashV1Doc(popoto.Model):
    """HashV2Doc's parameters with the default hash version (v1)."""

    name = popoto.UniqueKeyField()
    topic = popoto.Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.05, capacity=1000, fingerprint_fn=lambda inst: inst.topic
    )


class HashRace(popoto.Model):
    """capacity 100,000 at 1%: m = 958,505 bits, k = 6. Sparse for the few
    thousand tokens the race tests add, so a wiped filter shows up as
    (nearly) every token missing rather than being masked by saturation."""

    name = popoto.UniqueKeyField()
    topic = popoto.Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.01, capacity=100_000, fingerprint_fn=lambda inst: inst.topic
    )


class HashV2Big(popoto.Model):
    """capacity 10,000 at 5%: m = 62,352 bits, k = 4."""

    name = popoto.UniqueKeyField()
    topic = popoto.Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.05,
        capacity=10_000,
        fingerprint_fn=lambda inst: inst.topic,
        hash_version=2,
    )


class HashV2Tiny(popoto.Model):
    """The #775 report's filter: capacity 20 at 20% -> m = 66 bits, k = 2."""

    name = popoto.UniqueKeyField()
    topic = popoto.Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.2,
        capacity=20,
        fingerprint_fn=lambda inst: inst.topic,
        hash_version=2,
    )


MODELS = (HashV2Doc, HashV1Doc, HashRace, HashV2Big, HashV2Tiny)


def _wipe():
    client = get_REDIS_DB()
    for model in MODELS:
        for pattern in (f"$EF:{model.__name__}:*", f"{model.__name__}:*"):
            for key in client.scan_iter(match=pattern, count=1000):
                client.delete(key)


@pytest.fixture(autouse=True)
def clean():
    _wipe()
    yield
    _wipe()


def bloom_key(model) -> str:
    return f"$EF:{model.__name__}:bloom"


def similar_tokens(n: int, start: int = 0) -> list:
    return [f"ytopic{i:04d}" for i in range(start, start + n)]


@pytest.fixture
def legacy_writes(monkeypatch):
    """Make ``on_save`` run the pre-#775 add scripts: the old code's writes.

    The legacy scripts read only ``KEYS[1]``, so the rebuild lock and staging
    key the current ``on_save`` passes as ``KEYS[2..3]`` are ignored, exactly
    as the old
    ``numkeys=1`` call did -- in particular they never dual-write a rebuild's
    staging key. Every bit they set is the old code's. Patched for both
    create versions: old code had no notion of one.
    """
    for name in ("BLOOM_ADD_LUA", "BLOOM_ADD_V2_LUA"):
        monkeypatch.setattr(ef, name, LEGACY_BLOOM_ADD_LUA)
    for name in ("BLOOM_ADD_MULTI_LUA", "BLOOM_ADD_MULTI_V2_LUA"):
        monkeypatch.setattr(ef, name, LEGACY_BLOOM_ADD_MULTI_LUA)
    return monkeypatch


@pytest.fixture
def legacy_reads(monkeypatch):
    """Make ``might_exist``/``might_exist_batch`` run the pre-#775 scripts.

    Both are called with ``numkeys=1`` and the same arguments the old code
    passed, so a read through them *is* the old code's read."""
    monkeypatch.setattr(ef, "BLOOM_EXISTS_LUA", LEGACY_BLOOM_EXISTS_LUA)
    monkeypatch.setattr(ef, "BLOOM_EXISTS_BATCH_LUA", LEGACY_BLOOM_EXISTS_BATCH_LUA)
    return monkeypatch


def missing(model, tokens) -> int:
    """How many of ``tokens`` the filter calls definitely absent -- false
    negatives, when every token was added. Checked both ways a caller reads."""
    batch = model.bloom.might_exist_batch(model, tokens)
    by_batch = sum(1 for v in batch.values() if not v)
    by_one = sum(1 for t in tokens[::50] if not model.bloom.might_exist(model, t))
    assert by_one <= by_batch
    return by_batch


def rebuild_keys(model) -> list:
    """The lock and staging key of the model's filter -- and anything else
    under the lock's name, so a stray key would show up too."""
    lock = bloom_key(model) + BLOOM_REBUILD_SUFFIX
    keys = [k.decode() for k in get_REDIS_DB().scan_iter(match=lock + "*")]
    return sorted(keys)


def save_all(model, tokens, prefix="r"):
    for i, token in enumerate(tokens):
        model(name=f"{prefix}-{i}", topic=token).save()


# ---------------------------------------------------------------------------
# The v2 hash itself
# ---------------------------------------------------------------------------


class TestV2Hash:
    def _lua_positions(self, token, m, k):
        script = (
            ef._BLOOM_LUA_LIB
            + "\nreturn ef_positions_v2(ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3]))"
        )
        return run_lua(get_REDIS_DB(), script, 0, token, m, k)

    def test_fnv1a_known_vectors(self):
        # With m = 2^32 and k = 1 the first position is h1 itself.
        for text, want in (("a", 0xE40C292C), ("foobar", 0xBF9CF968)):
            assert self._lua_positions(text, 2**32, 1) == [want]
            assert fnv1a32(text.encode()) == want

    def test_lua_matches_python_reference(self):
        rnd = random.Random(775)
        for _ in range(300):
            token = "".join(
                chr(rnd.randrange(33, 0x2FFF)) for _ in range(rnd.randrange(1, 48))
            )
            m = rnd.choice([2, 66, 6235, 958_505, 2**31 - 1, 2**32, 10**12])
            k = rnd.randrange(1, 15)
            assert self._lua_positions(token, m, k) == v2_positions(token, m, k)

    def test_pure_lua_no_bit_library(self):
        # Redis and Valkey both ship LuaBitOp today, but nothing requires it
        # of a server: the v2 hash is plain arithmetic.
        assert "bit." not in ef._BLOOM_LUA_LIB

    def test_new_filter_is_v2_with_header_after_bit_array(self):
        HashV2Doc(name="a", topic="kubernetes").save()
        m, k = HashV2Doc.bloom._compute_params()
        raw = get_REDIS_DB().get(bloom_key(HashV2Doc))
        assert raw[bloom_header_offset(m) :] == BLOOM_V2_HEADER
        assert set_positions(raw, m) == set(v2_positions("kubernetes", m, k))
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 2

    def test_read_does_not_create_filter(self):
        assert HashV2Doc.bloom.might_exist(HashV2Doc, "anything") is False
        assert HashV2Doc.bloom.might_exist_batch(HashV2Doc, ["x", "y"]) == {
            "x": False,
            "y": False,
        }
        assert not get_REDIS_DB().exists(bloom_key(HashV2Doc))
        assert HashV2Doc.bloom.hash_version(HashV2Doc) is None

    def test_fill_ratio_excludes_header(self):
        save_all(HashV2Doc, similar_tokens(50))
        m, k = HashV2Doc.bloom._compute_params()
        raw = get_REDIS_DB().get(bloom_key(HashV2Doc))
        bits = set_positions(raw, m)
        want = set().union(*(v2_positions(t, m, k) for t in similar_tokens(50)))
        assert bits == want
        assert HashV2Doc.bloom.fill_ratio(HashV2Doc) == len(want) / m


# ---------------------------------------------------------------------------
# The #775 symptom, before and after
# ---------------------------------------------------------------------------


class TestSimilarTokens:
    def test_400_similar_tokens_set_expected_bit_count(self):
        save_all(HashV2Doc, similar_tokens(400))
        m, k = HashV2Doc.bloom._compute_params()
        mean, sd = expected_set_bits(m, k, 400)
        bits = round(HashV2Doc.bloom.fill_ratio(HashV2Doc) * m)
        assert abs(bits - mean) <= 5 * sd, (bits, mean, sd)

    def test_v1_collapse_reproduced_by_legacy_code(self, legacy_writes):
        """The bug, pinned as the old code's behavior: the same 400 tokens
        set a small fraction of the bits they should (measured: 64 of the
        ~1,411 expected)."""
        save_all(HashV2Doc, similar_tokens(400))
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 1
        m, k = HashV2Doc.bloom._compute_params()
        mean, _sd = expected_set_bits(m, k, 400)
        bits = HashV2Doc.bloom.fill_ratio(HashV2Doc) * m
        assert bits < mean / 10

    def test_issue_report_tiny_filter(self):
        """The issue's numbers: m = 66, k = 2, 400 similar tokens. v2
        saturates the filter, as 400 items in 66 bits must."""
        save_all(HashV2Tiny, similar_tokens(400))
        m, k = HashV2Tiny.bloom._compute_params()
        assert (m, k) == (66, 2)
        mean, _sd = expected_set_bits(m, k, 400)
        assert round(HashV2Tiny.bloom.fill_ratio(HashV2Tiny) * m) == round(mean) == 66

    def test_false_positive_rate_near_target_10k_probes(self):
        """capacity 10,000 at 5%, filled to capacity, probed 10,000 times with
        never-added tokens of the same shape and of another shape."""
        m, k = HashV2Big.bloom._compute_params()
        key = bloom_key(HashV2Big)
        tokens = [f"xinserted{i:06d}" for i in range(10_000)]
        client = get_REDIS_DB()
        for start in range(0, len(tokens), 500):
            run_lua(
                client,
                ef.BLOOM_ADD_MULTI_V2_LUA,
                3,
                key,
                key + BLOOM_REBUILD_SUFFIX,
                key + BLOOM_REBUILD_STAGING_SUFFIX,
                m,
                k,
                *tokens[start : start + 500],
            )
        assert HashV2Big.bloom.hash_version(HashV2Big) == 2
        theory = (1 - math.exp(-k * 10_000 / m)) ** k  # 0.0503
        for probes in (
            [f"xinserted{i:06d}" for i in range(10_000, 20_000)],
            [f"qneverseen{i:05d}" for i in range(10_000)],
        ):
            hits = HashV2Big.bloom.might_exist_batch(HashV2Big, probes)
            fpr = sum(hits.values()) / len(probes)
            # Binomial sd at 10k probes is ~0.0022; allow ~6 sd.
            assert abs(fpr - theory) <= 0.013, (fpr, theory)
        # No false negatives.
        hits = HashV2Big.bloom.might_exist_batch(HashV2Big, tokens[::97])
        assert all(hits.values())


# ---------------------------------------------------------------------------
# Compatibility: a v1 filter built by the old code
# ---------------------------------------------------------------------------


class TestLegacyFilterCompatibility:
    def _build_v1(self, legacy_writes, tokens, model=HashV2Doc):
        save_all(model, tokens)
        legacy_writes.undo()  # from here on, the current code
        assert model.bloom.hash_version(model) == 1
        assert not get_REDIS_DB().getrange(
            bloom_key(model),
            bloom_header_offset(model.bloom._compute_params()[0]),
            -1,
        )

    def test_every_added_token_still_present_after_upgrade(self, legacy_writes):
        tokens = similar_tokens(400)
        self._build_v1(legacy_writes, tokens)
        assert all(HashV2Doc.bloom.might_exist(HashV2Doc, t) for t in tokens[::7])
        assert all(HashV2Doc.bloom.might_exist_batch(HashV2Doc, tokens).values())

    def test_reads_agree_with_legacy_reads(self, legacy_writes):
        """On a v1 filter the current scripts answer exactly as the old ones
        did, probe for probe -- positives and negatives alike."""
        self._build_v1(legacy_writes, similar_tokens(300))
        m, k = HashV2Doc.bloom._compute_params()
        probes = similar_tokens(600) + [f"other{i}" for i in range(400)]
        old = run_lua(
            get_REDIS_DB(),
            LEGACY_BLOOM_EXISTS_BATCH_LUA,
            1,
            bloom_key(HashV2Doc),
            m,
            k,
            *probes,
        )
        new = run_lua(
            get_REDIS_DB(),
            ef.BLOOM_EXISTS_BATCH_LUA,
            1,
            bloom_key(HashV2Doc),
            m,
            k,
            *probes,
        )
        assert new == old

    def test_new_adds_to_v1_filter_stay_v1_and_byte_identical(self, legacy_writes):
        """Mixed: saves after the upgrade keep writing v1 positions until the
        filter is rebuilt, and the bytes are exactly what the old code would
        have written for the same tokens."""
        first, later = similar_tokens(200), similar_tokens(200, start=200)
        m, k = HashV2Doc.bloom._compute_params()
        client = get_REDIS_DB()
        reference = "$EF:HashV2Doc:reference"
        run_lua(client, LEGACY_BLOOM_ADD_MULTI_LUA, 1, reference, m, k, *first)
        run_lua(client, LEGACY_BLOOM_ADD_MULTI_LUA, 1, reference, m, k, *later)
        run_lua(client, LEGACY_BLOOM_ADD_LUA, 1, reference, "ab", m, k)

        self._build_v1(legacy_writes, first)
        save_all(HashV2Doc, later, prefix="later")
        HashV2Doc(name="short", topic="ab").save()  # the raw-fingerprint path
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 1
        assert client.get(bloom_key(HashV2Doc)) == client.get(reference)
        assert all(HashV2Doc.bloom.might_exist_batch(HashV2Doc, first + later).values())

    def test_check_indexes_reports_legacy_hash(self, legacy_writes):
        self._build_v1(legacy_writes, similar_tokens(20))
        report = HashV2Doc.check_indexes()
        assert report["legacy_hash"] == ["bloom"]
        assert report["total"] == 0  # informational, not an orphan

    def test_rebuild_converts_v1_to_v2(self, legacy_writes):
        tokens = similar_tokens(400)
        self._build_v1(legacy_writes, tokens)
        assert HashV2Doc.rebuild_indexes(bloom_hash_version=2) == 400
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 2
        assert rebuild_keys(HashV2Doc) == []  # lock released, staging renamed
        assert get_REDIS_DB().pttl(bloom_key(HashV2Doc)) == -1  # no TTL carried
        assert HashV2Doc.check_indexes()["legacy_hash"] == []
        assert all(HashV2Doc.bloom.might_exist_batch(HashV2Doc, tokens).values())
        m, k = HashV2Doc.bloom._compute_params()
        want = set().union(*(v2_positions(t, m, k) for t in tokens))
        raw = get_REDIS_DB().get(bloom_key(HashV2Doc))
        assert set_positions(raw, m) == want  # exactly the records' v2 bits
        # Later saves write v2 now.
        HashV2Doc(name="after", topic="postrebuild").save()
        assert HashV2Doc.bloom.might_exist(HashV2Doc, "postrebuild")

    @pytest.mark.parametrize("version", [None, 2])
    def test_rebuild_leaves_v2_filter_alone(self, version):
        save_all(HashV2Doc, similar_tokens(10))
        before = get_REDIS_DB().get(bloom_key(HashV2Doc))
        HashV2Doc.rebuild_indexes(bloom_hash_version=version)
        assert get_REDIS_DB().get(bloom_key(HashV2Doc)) == before
        assert rebuild_keys(HashV2Doc) == []

    def test_default_rebuild_keeps_v1_in_place(self, legacy_writes):
        """Without the opt-in a rebuild is main's: re-save into the live v1
        filter. No lock, no staging key, and the bytes are exactly what the
        old code's own rebuild would leave."""
        tokens = similar_tokens(200)
        self._build_v1(legacy_writes, tokens)
        before = get_REDIS_DB().get(bloom_key(HashV2Doc))
        assert HashV2Doc.rebuild_indexes() == 200
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 1
        assert get_REDIS_DB().get(bloom_key(HashV2Doc)) == before
        assert rebuild_keys(HashV2Doc) == []

    @pytest.mark.parametrize("bad", [0, 1, 3, "2"])
    def test_rebuild_rejects_other_versions(self, bad):
        with pytest.raises(ValueError, match="bloom_hash_version"):
            HashV2Doc.rebuild_indexes(bloom_hash_version=bad)

    def test_save_racing_rebuild_is_not_lost(self, legacy_writes):
        """A save that lands while the staging key is open dual-writes it --
        found through the lock's value, inside the save's own script -- so
        the swap does not drop the racing record's tokens."""
        self._build_v1(legacy_writes, similar_tokens(30))
        token = HashV2Doc.bloom._begin_v2_rebuild(HashV2Doc)
        lock = bloom_key(HashV2Doc) + BLOOM_REBUILD_SUFFIX
        staging = bloom_key(HashV2Doc) + BLOOM_REBUILD_STAGING_SUFFIX
        assert get_REDIS_DB().get(lock) == token.encode()
        assert rebuild_keys(HashV2Doc) == [lock, staging]
        HashV2Doc(name="racer", topic="racingtoken").save()
        m, k = HashV2Doc.bloom._compute_params()
        staged = set_positions(get_REDIS_DB().get(staging), m)
        assert set(v2_positions("racingtoken", m, k)) <= staged
        # Live filter is still v1 and already answers for it.
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 1
        assert HashV2Doc.bloom.might_exist(HashV2Doc, "racingtoken")
        HashV2Doc.bloom._finish_v2_rebuild(HashV2Doc, token)
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 2
        assert HashV2Doc.bloom.might_exist(HashV2Doc, "racingtoken")

    def test_interrupted_rebuild_keeps_v1_and_drops_staging(
        self, legacy_writes, monkeypatch
    ):
        tokens = similar_tokens(30)
        self._build_v1(legacy_writes, tokens)

        def boom(cls, batch_size):
            raise RuntimeError("interrupted")

        monkeypatch.setattr(HashV2Doc, "_rebuild_from_records", classmethod(boom))
        with pytest.raises(RuntimeError):
            HashV2Doc.rebuild_indexes(bloom_hash_version=2)
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 1
        assert rebuild_keys(HashV2Doc) == []
        assert all(HashV2Doc.bloom.might_exist_batch(HashV2Doc, tokens).values())


# ---------------------------------------------------------------------------
# The version travels with the filter
# ---------------------------------------------------------------------------


class TestVersionPortability:
    @pytest.mark.parametrize("legacy", [False, True])
    def test_dump_restore_keeps_version(self, legacy, monkeypatch):
        tokens = similar_tokens(100)
        with monkeypatch.context() as patch:
            if legacy:
                _patch_old_writes(patch)
            save_all(HashV2Doc, tokens)
        version = HashV2Doc.bloom.hash_version(HashV2Doc)
        assert version == (1 if legacy else 2)
        client = get_REDIS_DB()
        key = bloom_key(HashV2Doc)
        dumped = client.dump(key)
        client.delete(key)
        client.restore(key, 0, dumped)
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == version
        assert all(HashV2Doc.bloom.might_exist_batch(HashV2Doc, tokens).values())

    def test_export_import_into_fresh_destination_builds_v2(self, legacy_writes):
        """Bits are never carried (#556); the import's saves rebuild the
        filter. From a v1 source into an empty destination that is a v2
        filter holding every imported token."""
        tokens = similar_tokens(60)
        save_all(HashV2Doc, tokens)
        legacy_writes.undo()
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 1
        data = HashV2Doc.export_records().data
        _wipe()
        HashV2Doc.import_records(io.StringIO(data))
        assert HashV2Doc.query.count() == 60
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 2
        assert all(HashV2Doc.bloom.might_exist_batch(HashV2Doc, tokens).values())

    def test_export_import_default_destination_builds_v1(self):
        tokens = similar_tokens(30)
        save_all(HashV1Doc, tokens)
        data = HashV1Doc.export_records().data
        _wipe()
        HashV1Doc.import_records(io.StringIO(data))
        assert HashV1Doc.bloom.hash_version(HashV1Doc) == 1
        assert missing(HashV1Doc, tokens) == 0

    def test_import_into_existing_v1_filter_keeps_v1(self, legacy_writes):
        tokens = similar_tokens(40)
        save_all(HashV2Doc, tokens)
        legacy_writes.undo()
        data = HashV2Doc.export_records().data
        for key in get_REDIS_DB().scan_iter(match="HashV2Doc:*"):
            get_REDIS_DB().delete(key)  # records gone, the v1 filter stays
        HashV2Doc.import_records(io.StringIO(data))
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 1
        assert all(HashV2Doc.bloom.might_exist_batch(HashV2Doc, tokens).values())

    def test_migration_inventory_classifies_staging_key_as_ef(self):
        """The #756 inventory stops on an unexpected key family; the staging
        key must fall in ``$EF``."""
        from src.popoto.migrate_redis_to_postgres import (
            FAMILY_DISPOSITIONS,
            _family_of,
        )

        names = {"HashV2Doc": "HashV2Doc"}
        for key in (
            "$EF:HashV2Doc:bloom",
            "$EF:HashV2Doc:bloom" + BLOOM_REBUILD_SUFFIX,
            "$EF:HashV2Doc:bloom" + BLOOM_REBUILD_STAGING_SUFFIX,
        ):
            assert _family_of(key, names, {}) == ("HashV2Doc", "$EF")
        assert "$EF" in FAMILY_DISPOSITIONS


# ---------------------------------------------------------------------------
# Default mode: v2 is opt-in (PR #801 review, B2)
# ---------------------------------------------------------------------------


def _patch_old_writes(mp):
    for name in ("BLOOM_ADD_LUA", "BLOOM_ADD_V2_LUA"):
        mp.setattr(ef, name, LEGACY_BLOOM_ADD_LUA)
    for name in ("BLOOM_ADD_MULTI_LUA", "BLOOM_ADD_MULTI_V2_LUA"):
        mp.setattr(ef, name, LEGACY_BLOOM_ADD_MULTI_LUA)


class old_code:
    """Run the block as a pre-#775 process would: the old scripts, verbatim,
    for every bloom read and write."""

    def __enter__(self):
        self._mp = pytest.MonkeyPatch()
        _patch_old_writes(self._mp)
        self._mp.setattr(ef, "BLOOM_EXISTS_LUA", LEGACY_BLOOM_EXISTS_LUA)
        self._mp.setattr(ef, "BLOOM_EXISTS_BATCH_LUA", LEGACY_BLOOM_EXISTS_BATCH_LUA)
        return self

    def __exit__(self, *exc):
        self._mp.undo()
        return False


def toks(prefix: str, n: int) -> list:
    return [f"{prefix}{i:06d}" for i in range(n)]


def legacy_reference(tokens, model) -> bytes:
    """The bytes the old code alone would leave after adding ``tokens``."""
    m, k = model.bloom._compute_params()
    client = get_REDIS_DB()
    ref = f"$EF:{model.__name__}:reference"
    client.delete(ref)
    for t in tokens:
        run_lua(client, LEGACY_BLOOM_ADD_MULTI_LUA, 1, ref, m, k, t)
    raw = client.get(ref)
    client.delete(ref)
    return raw


class TestDefaultIsV1:
    def test_new_filter_is_v1_and_byte_identical_to_old_code(self):
        tokens = similar_tokens(100)
        save_all(HashV1Doc, tokens)
        assert HashV1Doc.bloom.hash_version(HashV1Doc) == 1
        assert get_REDIS_DB().get(bloom_key(HashV1Doc)) == legacy_reference(
            tokens, HashV1Doc
        )
        assert HashV1Doc.check_indexes()["legacy_hash"] == ["bloom"]

    @pytest.mark.parametrize("bad", [0, 3, "2", None])
    def test_field_rejects_unknown_hash_version(self, bad):
        with pytest.raises(ValueError, match="hash_version"):
            ExistenceFilter(fingerprint_fn=lambda inst: "x", hash_version=bad)

    @pytest.mark.parametrize("value", [1, 2])
    def test_frequency_sketch_has_no_hash_version(self, value):
        """Field.__init__ ignores unknown keywords, so FrequencySketch must
        refuse this one itself rather than construct and ignore it."""
        from src.popoto.fields.existence_filter import FrequencySketch

        with pytest.raises(TypeError, match="hash_version"):
            FrequencySketch(fingerprint_fn=lambda inst: "x", hash_version=value)

    def test_existing_v2_filter_is_written_as_v2_by_a_default_field(self):
        """A filter some opted-in process (or a v2 rebuild) made v2 stays v2:
        a default-mode field reads and writes it with v2, never v1."""
        m, k = HashV1Doc.bloom._compute_params()
        key = bloom_key(HashV1Doc)
        lock = key + BLOOM_REBUILD_SUFFIX
        staging = key + BLOOM_REBUILD_STAGING_SUFFIX
        first = toks("pre", 50)
        run_lua(
            get_REDIS_DB(),
            ef.BLOOM_ADD_MULTI_V2_LUA,
            3,
            key,
            lock,
            staging,
            m,
            k,
            *first,
        )
        assert HashV1Doc.bloom.hash_version(HashV1Doc) == 2
        later = toks("post", 50)
        save_all(HashV1Doc, later)
        assert HashV1Doc.bloom.hash_version(HashV1Doc) == 2
        want = set().union(*(v2_positions(t, m, k) for t in first + later))
        assert set_positions(get_REDIS_DB().get(key), m) == want
        assert missing(HashV1Doc, first + later) == 0

    def test_save_never_creates_a_staging_key(self):
        """A lock whose staging key is gone (or never made) is not written
        through: an add creating a markerless staging key is what would let
        a swap install it as an empty "v1" filter."""
        lock = bloom_key(HashV1Doc) + BLOOM_REBUILD_SUFFIX
        get_REDIS_DB().set(lock, "deadbeef")
        save_all(HashV1Doc, toks("x", 5))
        assert not get_REDIS_DB().exists(bloom_key(HashV1Doc) + ":rebuild:staging")

    def test_save_never_writes_a_staging_key_without_a_lock(self):
        """A staging key a dead rebuild left (its lock lapsed) is frozen:
        saves write the staging key only while a lock exists."""
        staging = bloom_key(HashV1Doc) + BLOOM_REBUILD_STAGING_SUFFIX
        m, _k = HashV1Doc.bloom._compute_params()
        get_REDIS_DB().setrange(staging, bloom_header_offset(m), BLOOM_V2_HEADER)
        frozen = get_REDIS_DB().get(staging)
        save_all(HashV1Doc, toks("x", 5))
        assert get_REDIS_DB().get(staging) == frozen


class TestMixedVersionsDefaultMode:
    """Pre-#775 and current processes sharing one store, the current code in
    default mode: 0 tokens missing in every direction (the review's table,
    rows that were unsafe because new code created or rebuilt as v2)."""

    def test_old_creates_new_writes_both_read(self):
        with old_code():
            save_all(HashRace, toks("alpha", 300), prefix="a")
        save_all(HashRace, toks("beta", 300), prefix="b")
        assert HashRace.bloom.hash_version(HashRace) == 1
        assert missing(HashRace, toks("alpha", 300) + toks("beta", 300)) == 0
        with old_code():
            assert missing(HashRace, toks("alpha", 300) + toks("beta", 300)) == 0

    def test_new_creates_old_writes_both_read(self):
        save_all(HashRace, toks("gamma", 300), prefix="g")
        assert HashRace.bloom.hash_version(HashRace) == 1
        with old_code():
            assert missing(HashRace, toks("gamma", 300)) == 0
            save_all(HashRace, toks("delta", 300), prefix="d")
        assert missing(HashRace, toks("gamma", 300) + toks("delta", 300)) == 0
        assert get_REDIS_DB().get(bloom_key(HashRace)) == legacy_reference(
            toks("gamma", 300) + toks("delta", 300), HashRace
        )

    def test_new_default_rebuild_then_old_writes_both_read(self):
        with old_code():
            save_all(HashRace, toks("eps", 300), prefix="e")
        assert HashRace.rebuild_indexes() == 300
        assert HashRace.bloom.hash_version(HashRace) == 1
        with old_code():
            save_all(HashRace, toks("zeta", 300), prefix="z")
            assert missing(HashRace, toks("eps", 300) + toks("zeta", 300)) == 0
        assert missing(HashRace, toks("eps", 300) + toks("zeta", 300)) == 0

    def test_old_save_racing_new_default_rebuild(self):
        """The review's 4b lost 170/2000 at a v2 swap. A default rebuild has
        no swap: it re-saves into the live filter, so an old-code save in the
        middle of it lands where every reader looks."""
        with old_code():
            save_all(HashRace, toks("base", 400), prefix="s")
        calls = [0]
        on_save = ExistenceFilter.on_save.__func__

        def counting(cls, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 200:
                with old_code():
                    save_all(HashRace, toks("orace", 200), prefix="o")
            return on_save(cls, *args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ExistenceFilter, "on_save", classmethod(counting))
            HashRace._rebuild_indexes_redis(100)
        assert calls[0] >= 200
        assert HashRace.bloom.hash_version(HashRace) == 1
        assert missing(HashRace, toks("base", 400) + toks("orace", 200)) == 0
        with old_code():
            assert missing(HashRace, toks("base", 400) + toks("orace", 200)) == 0

    def test_why_v2_is_opt_in(self):
        """The hazard the rolling-upgrade rule exists for, pinned so the docs
        cannot drift from it: old code reading a v2 filter misses the v2
        tokens, and new code misses what old code writes into a v2 filter."""
        save_all(HashRace, toks("new", 300), prefix="n")
        assert HashRace.rebuild_indexes(bloom_hash_version=2) == 300
        with old_code():
            assert missing(HashRace, toks("new", 300)) > 250
            save_all(HashRace, toks("old", 300), prefix="o")
        assert missing(HashRace, toks("old", 300)) > 250


# ---------------------------------------------------------------------------
# One v2 rebuild at a time (PR #801 review, B1)
# ---------------------------------------------------------------------------

N_RACE = 1000


@pytest.fixture
def seeded():
    """A v1 filter of N_RACE records, built by the old code."""
    with old_code():
        save_all(HashRace, toks("base", N_RACE), prefix="s")
    assert HashRace.bloom.hash_version(HashRace) == 1
    return toks("base", N_RACE)


def class_set_size(model) -> int:
    return get_REDIS_DB().scard(model._meta.db_class_set_key.redis_key)


def lapse(key: str) -> None:
    """Let ``key`` expire, as a dead rebuild's lock does."""
    get_REDIS_DB().pexpire(key, 1)
    deadline = time.monotonic() + 2
    while get_REDIS_DB().exists(key) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not get_REDIS_DB().exists(key)


LOCK = bloom_key(HashRace) + BLOOM_REBUILD_SUFFIX
STAGING = bloom_key(HashRace) + BLOOM_REBUILD_STAGING_SUFFIX


def renewer_threads() -> list:
    """The live rebuild-lock renewer threads of this process."""
    return [
        t
        for t in threading.enumerate()
        if t.name.startswith("popoto-bloom-renew-") and t.is_alive()
    ]


class TestRebuildLock:
    def test_second_rebuild_refused_while_first_completes(self, seeded):
        """The review's race2: R2 starts while R1's staging key is full.
        R2 is refused before it deletes anything; 0 missing at every step."""
        f = HashRace.bloom
        t1 = f._begin_v2_rebuild(HashRace)
        HashRace._rebuild_from_records(1000)  # R1, step 2 done
        assert missing(HashRace, seeded) == 0
        before = class_set_size(HashRace)
        with pytest.raises(BloomRebuildInProgressError) as info:
            HashRace.rebuild_indexes(bloom_hash_version=2)
        assert info.value.holder == t1 and info.value.lock_key == LOCK
        assert 0 < info.value.expires_in_ms <= Defaults.BLOOM_REBUILD_LOCK_TTL_MS
        assert class_set_size(HashRace) == before == N_RACE  # nothing deleted
        assert missing(HashRace, seeded) == 0
        with pytest.raises(BloomRebuildInProgressError):
            f._begin_v2_rebuild(HashRace)
        assert missing(HashRace, seeded) == 0
        f._finish_v2_rebuild(HashRace, t1)  # R1's swap
        assert f.hash_version(HashRace) == 2
        assert missing(HashRace, seeded) == 0
        assert rebuild_keys(HashRace) == []
        # R2, retried, finds nothing to convert and re-saves in place.
        assert HashRace.rebuild_indexes(bloom_hash_version=2) == N_RACE
        assert missing(HashRace, seeded) == 0

    def test_default_rebuild_during_conversion_dual_writes(self, seeded):
        """A default rebuild is not refused (it never swaps); its re-saves
        land in the converter's staging key too."""
        f = HashRace.bloom
        t1 = f._begin_v2_rebuild(HashRace)
        save_all(HashRace, toks("mid", 100), prefix="m")
        assert HashRace.rebuild_indexes() == N_RACE + 100
        f._finish_v2_rebuild(HashRace, t1)
        assert missing(HashRace, seeded + toks("mid", 100)) == 0

    def test_dead_rebuild_loses_nothing_and_is_cleaned(self, seeded):
        """The review's race3: a rebuild dies halfway. The live filter was
        never touched; its lock lapses, check_indexes() reports its staging
        key, no save writes it, and the next rebuild deletes it."""
        f = HashRace.bloom
        t1 = f._begin_v2_rebuild(HashRace)
        save_all(HashRace, toks("base", N_RACE // 2), prefix="s")  # R1, half
        # R1 dies here: no finish, no abort.
        assert missing(HashRace, seeded) == 0
        assert f.hash_version(HashRace) == 1
        with pytest.raises(BloomRebuildInProgressError):
            HashRace.rebuild_indexes(bloom_hash_version=2)
        assert HashRace.check_indexes()["stale_bloom_staging"] == []  # still live
        lapse(LOCK)
        staging = STAGING
        report = HashRace.check_indexes()
        assert report["stale_bloom_staging"] == [staging]
        assert report["total"] == 0
        frozen = get_REDIS_DB().get(staging)
        save_all(HashRace, toks("after", 50), prefix="a")
        assert get_REDIS_DB().get(staging) == frozen  # nobody writes it
        assert missing(HashRace, seeded + toks("after", 50)) == 0
        assert HashRace.rebuild_indexes(bloom_hash_version=2) == N_RACE + 50
        assert f.hash_version(HashRace) == 2
        assert rebuild_keys(HashRace) == []
        assert HashRace.check_indexes()["stale_bloom_staging"] == []
        assert missing(HashRace, seeded + toks("after", 50)) == 0

    def test_lapsed_lock_refuses_the_swap(self, seeded):
        """R1 stalls past its lock; R2 takes over. R1's swap is refused (the
        compare-and-rename sees R2's token), the live filter is unchanged,
        R2's staging key is not disturbed -- not renewed, renamed or deleted
        by R1, though the name is the same -- and R2's swap completes."""
        f = HashRace.bloom
        t1 = f._begin_v2_rebuild(HashRace)
        HashRace._rebuild_from_records(1000)
        lapse(LOCK)
        t2 = f._begin_v2_rebuild(HashRace)
        assert t2 != t1
        assert rebuild_keys(HashRace) == [LOCK, STAGING]
        m, _k = f._compute_params()
        # R2's BEGIN emptied what R1 had staged.
        assert set_positions(get_REDIS_DB().get(STAGING), m) == set()
        save_all(HashRace, toks("r2", 20), prefix="q")  # R2-era bits
        r2_bytes = get_REDIS_DB().get(STAGING)
        get_REDIS_DB().pexpire(STAGING, 5000)
        get_REDIS_DB().pexpire(LOCK, 5000)
        assert f._renew_v2_rebuild(HashRace, t1) is False
        assert get_REDIS_DB().pttl(STAGING) <= 5000  # R1 did not extend it
        with pytest.raises(BloomRebuildLostLockError):
            f._finish_v2_rebuild(HashRace, t1)  # refused, then R1's abort
        f._abort_v2_rebuild(HashRace, t1)  # and once more, explicitly
        assert get_REDIS_DB().get(STAGING) == r2_bytes  # untouched by R1
        assert get_REDIS_DB().get(LOCK) == t2.encode()
        assert f.hash_version(HashRace) == 1
        assert missing(HashRace, seeded + toks("r2", 20)) == 0
        assert f._renew_v2_rebuild(HashRace, t2) is True
        HashRace._rebuild_from_records(1000)
        f._finish_v2_rebuild(HashRace, t2)
        assert f.hash_version(HashRace) == 2
        assert missing(HashRace, seeded) == 0
        assert rebuild_keys(HashRace) == []

    def test_swap_refuses_a_staging_key_without_marker(self, seeded):
        f = HashRace.bloom
        t1 = f._begin_v2_rebuild(HashRace)
        get_REDIS_DB().delete(STAGING)
        get_REDIS_DB().setbit(STAGING, 7, 1)
        with pytest.raises(BloomRebuildLostLockError):
            f._finish_v2_rebuild(HashRace, t1)
        assert f.hash_version(HashRace) == 1
        assert missing(HashRace, seeded) == 0
        assert rebuild_keys(HashRace) == []

    def test_step1_outlasting_the_ttl_still_converts(self, seeded, monkeypatch):
        """PR #801 review N1: nothing renewed the lock during step 1, so a
        conversion whose index deletion alone outlasted the TTL always lost
        it. The renewer thread runs from the moment the lock is taken."""
        monkeypatch.setattr(Defaults, "BLOOM_REBUILD_LOCK_TTL_MS", 300)
        original = HashRace._rebuild_from_records

        def after_slow_step1(cls, batch_size):
            time.sleep(1.0)  # > 3 TTLs between taking the lock and step 2
            assert get_REDIS_DB().exists(LOCK)
            return original(batch_size)

        monkeypatch.setattr(
            HashRace, "_rebuild_from_records", classmethod(after_slow_step1)
        )
        assert HashRace.rebuild_indexes(bloom_hash_version=2) == N_RACE
        assert HashRace.bloom.hash_version(HashRace) == 2
        assert get_REDIS_DB().pttl(bloom_key(HashRace)) == -1
        assert missing(HashRace, seeded) == 0
        assert rebuild_keys(HashRace) == []

    def test_real_step1_outlasting_the_ttl_still_converts(self, seeded, monkeypatch):
        """The reviewer's tickprobe, ported: a store whose step 1 really
        takes longer than the lock TTL (index sets to SCAN and DEL one by
        one, as on a large store), measured rather than assumed."""
        index = HashRace._meta.fields["name"].get_special_use_field_db_key(
            HashRace, "name"
        )
        junk = [f"{index.redis_key}:junk{i:06d}" for i in range(20_000)]
        client = get_REDIS_DB()
        try:
            for start in range(0, len(junk), 2000):
                pipe = client.pipeline(transaction=False)
                for key in junk[start : start + 2000]:
                    pipe.sadd(key, "x")
                pipe.execute()
            monkeypatch.setattr(Defaults, "BLOOM_REBUILD_LOCK_TTL_MS", 150)
            begin = HashRace._begin_bloom_rebuilds.__func__
            original = HashRace._rebuild_from_records
            marks = {}

            def timed_begin(cls):
                out = begin(cls)
                marks["lock"] = time.monotonic()
                return out

            def timed_step2(cls, batch_size):
                marks["step2"] = time.monotonic()
                return original(batch_size)

            monkeypatch.setattr(
                HashRace, "_begin_bloom_rebuilds", classmethod(timed_begin)
            )
            monkeypatch.setattr(
                HashRace, "_rebuild_from_records", classmethod(timed_step2)
            )
            assert HashRace.rebuild_indexes(bloom_hash_version=2) == N_RACE
            step1 = marks["step2"] - marks["lock"]
            # Not vacuous: step 1 alone outlasted the TTL several times over.
            assert step1 > 3 * 0.150, step1
            assert HashRace.bloom.hash_version(HashRace) == 2
            assert missing(HashRace, seeded) == 0
            assert rebuild_keys(HashRace) == []
        finally:
            for start in range(0, len(junk), 2000):
                client.delete(*junk[start : start + 2000])

    def test_lock_lost_mid_rebuild_refuses_the_swap(self, seeded, monkeypatch):
        """A renewal that finds the lock gone flags it; the swap is refused,
        every other index is rebuilt, and the live filter is unchanged."""
        monkeypatch.setattr(Defaults, "BLOOM_REBUILD_LOCK_TTL_MS", 300)
        original = HashRace._rebuild_from_records

        def lapsing(cls, batch_size):
            lapse(LOCK)
            time.sleep(0.3)  # the renewer runs and finds it gone
            return original(batch_size)

        monkeypatch.setattr(HashRace, "_rebuild_from_records", classmethod(lapsing))
        with pytest.raises(BloomRebuildLostLockError):
            HashRace.rebuild_indexes(bloom_hash_version=2)
        assert class_set_size(HashRace) == N_RACE  # the rebuild itself ran
        assert HashRace.bloom.hash_version(HashRace) == 1
        assert missing(HashRace, seeded) == 0
        assert rebuild_keys(HashRace) == []  # the ownerless staging key too

    def test_renewer_keeps_a_held_lock_alive(self, seeded, monkeypatch):
        monkeypatch.setattr(Defaults, "BLOOM_REBUILD_LOCK_TTL_MS", 300)
        f = HashRace.bloom
        t1 = f._begin_v2_rebuild(HashRace)
        with BloomLockRenewer(HashRace, [(f, t1)]) as renewer:
            time.sleep(0.8)  # well past the 0.3 s TTL, no record visited
        assert renewer.lost == []
        assert get_REDIS_DB().get(LOCK) == t1.encode()
        assert get_REDIS_DB().exists(STAGING)
        f._finish_v2_rebuild(HashRace, t1)
        assert get_REDIS_DB().pttl(bloom_key(HashRace)) == -1

    @pytest.mark.parametrize("exc", [RuntimeError, KeyboardInterrupt])
    def test_renewer_never_outlives_the_rebuild(self, seeded, monkeypatch, exc):
        monkeypatch.setattr(Defaults, "BLOOM_REBUILD_LOCK_TTL_MS", 300)
        seen = []

        def boom(cls, batch_size):
            seen.extend(renewer_threads())
            raise exc("interrupted")

        monkeypatch.setattr(HashRace, "_rebuild_from_records", classmethod(boom))
        with pytest.raises(exc):
            HashRace.rebuild_indexes(bloom_hash_version=2)
        assert len(seen) == 1 and seen[0].daemon  # it was running ...
        assert not seen[0].is_alive()  # ... and is stopped and joined
        assert renewer_threads() == []
        assert HashRace.bloom.hash_version(HashRace) == 1
        assert rebuild_keys(HashRace) == []
        assert missing(HashRace, seeded) == 0

    def test_renewer_is_stopped_after_a_conversion(self, seeded):
        assert HashRace.rebuild_indexes(bloom_hash_version=2) == N_RACE
        assert renewer_threads() == []
        assert HashRace.rebuild_indexes() == N_RACE  # no lock, no thread
        assert renewer_threads() == []

    def test_missing_filter_is_created_v2_by_conversion(self):
        save_all(HashRace, toks("fresh", 100), prefix="f")
        get_REDIS_DB().delete(bloom_key(HashRace))
        assert HashRace.bloom.hash_version(HashRace) is None
        assert HashRace.rebuild_indexes(bloom_hash_version=2) == 100
        assert HashRace.bloom.hash_version(HashRace) == 2
        assert missing(HashRace, toks("fresh", 100)) == 0

    def test_async_rebuild_converts(self, seeded):
        import asyncio

        assert asyncio.run(HashRace.async_rebuild_indexes()) == N_RACE
        assert HashRace.bloom.hash_version(HashRace) == 1
        got = asyncio.run(HashRace.async_rebuild_indexes(bloom_hash_version=2))
        assert got == N_RACE
        assert HashRace.bloom.hash_version(HashRace) == 2
        assert missing(HashRace, seeded) == 0


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_KILLED_REBUILD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {repo!r})
    from src import popoto
    from src.popoto.fields.constants import Defaults
    from src.popoto.fields.existence_filter import ExistenceFilter

    assert popoto.get_redis().connection_pool.connection_kwargs.get("db") == {db}

    class HashRace(popoto.Model):
        name = popoto.UniqueKeyField()
        topic = popoto.Field(type=str)
        bloom = ExistenceFilter(
            error_rate=0.01, capacity=100_000, fingerprint_fn=lambda i: i.topic
        )

    Defaults.BLOOM_REBUILD_LOCK_TTL_MS = 1500
    on_save = ExistenceFilter.on_save.__func__

    def slow(cls, *args, **kwargs):
        time.sleep(0.005)
        return on_save(cls, *args, **kwargs)

    ExistenceFilter.on_save = classmethod(slow)
    # Small batches, so the staging key fills while the process is alive.
    HashRace.rebuild_indexes(batch_size=50, bloom_hash_version=2)
    print("FINISHED")
    """)


class TestRebuildSigkill:
    def test_sigkill_mid_rebuild_loses_nothing(self, seeded):
        """A real process converting the filter is SIGKILLed mid-scan, with
        saves landing around it. 0 tokens missing before, during and after;
        its lock lapses and the next conversion completes."""
        db = get_REDIS_DB().connection_pool.connection_kwargs.get("db")
        assert db not in (None, 0)
        env = dict(os.environ, REDIS_URL=f"redis://localhost:6379/{db}")
        proc = subprocess.Popen(
            [sys.executable, "-c", _KILLED_REBUILD.format(repo=_REPO, db=db)],
            cwd=_REPO,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 30
            staged = 0
            while time.monotonic() < deadline and proc.poll() is None:
                if get_REDIS_DB().exists(LOCK):
                    staged = get_REDIS_DB().bitcount(STAGING)
                    if staged > 200:
                        break
                time.sleep(0.01)
            assert proc.poll() is None, proc.communicate()
            assert staged > 200
            save_all(HashRace, toks("race", 100), prefix="r")
            assert missing(HashRace, seeded + toks("race", 100)) == 0
            time.sleep(0.2)
        finally:
            proc.send_signal(signal.SIGKILL)
            out, err = proc.communicate()
        assert b"FINISHED" not in out, err
        assert HashRace.bloom.hash_version(HashRace) == 1
        assert missing(HashRace, seeded + toks("race", 100)) == 0
        assert get_REDIS_DB().exists(LOCK)  # held until it lapses
        with pytest.raises(BloomRebuildInProgressError):
            HashRace.rebuild_indexes(bloom_hash_version=2)
        deadline = time.monotonic() + 5
        while get_REDIS_DB().exists(LOCK) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not get_REDIS_DB().exists(LOCK)
        save_all(HashRace, toks("post", 50), prefix="p")
        assert HashRace.rebuild_indexes(bloom_hash_version=2) == N_RACE + 150
        assert HashRace.bloom.hash_version(HashRace) == 2
        assert rebuild_keys(HashRace) == []
        assert missing(HashRace, seeded + toks("race", 100) + toks("post", 50)) == 0


_STOPPED_REBUILD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {repo!r})
    from src import popoto
    from src.popoto.fields.constants import Defaults
    from src.popoto.fields.existence_filter import ExistenceFilter

    assert popoto.get_redis().connection_pool.connection_kwargs.get("db") == {db}

    class HashRace(popoto.Model):
        name = popoto.UniqueKeyField()
        topic = popoto.Field(type=str)
        bloom = ExistenceFilter(
            error_rate=0.01, capacity=100_000, fingerprint_fn=lambda i: i.topic
        )

    Defaults.BLOOM_REBUILD_LOCK_TTL_MS = 1000
    on_save = ExistenceFilter.on_save.__func__

    def slow(cls, *args, **kwargs):
        time.sleep(0.002)
        return on_save(cls, *args, **kwargs)

    ExistenceFilter.on_save = classmethod(slow)
    try:
        HashRace.rebuild_indexes(batch_size=50, bloom_hash_version=2)
        print("CONVERTED")
    except Exception as exc:
        print("RAISED", type(exc).__name__)
    """)


class TestRebuildSigstop:
    """A whole-process pause longer than the TTL: the renewer thread is
    paused with the rebuild, so the lock lapses and the swap is refused, in
    each order another conversion can interleave (the review's SIGSTOP
    rows). The paused rebuild never writes into, renews, renames or deletes
    the staging key of the conversion that took over."""

    @pytest.mark.parametrize("order", ["nobody", "r2_mid_flight", "r2_completed"])
    def test_paused_past_ttl_is_refused(self, seeded, order):
        db = get_REDIS_DB().connection_pool.connection_kwargs.get("db")
        assert db not in (None, 0)
        env = dict(os.environ, REDIS_URL=f"redis://localhost:6379/{db}")
        proc = subprocess.Popen(
            [sys.executable, "-c", _STOPPED_REBUILD.format(repo=_REPO, db=db)],
            cwd=_REPO,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        f = HashRace.bloom
        extra = toks("pause", 100)
        stopped = False
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and proc.poll() is None:
                if get_REDIS_DB().bitcount(STAGING) > 200:
                    break
                time.sleep(0.01)
            assert proc.poll() is None, proc.communicate()
            proc.send_signal(signal.SIGSTOP)
            stopped = True
            deadline = time.monotonic() + 5
            while get_REDIS_DB().exists(LOCK) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not get_REDIS_DB().exists(LOCK)  # lapsed: nobody renewed it
            save_all(HashRace, extra, prefix="p")
            assert missing(HashRace, seeded + extra) == 0
            t2 = None
            if order == "r2_completed":
                assert HashRace.rebuild_indexes(bloom_hash_version=2) == N_RACE + 100
                assert f.hash_version(HashRace) == 2
            elif order == "r2_mid_flight":
                t2 = f._begin_v2_rebuild(HashRace)
                HashRace._rebuild_from_records(1000)
                m, _k = f._compute_params()
                staged = set_positions(get_REDIS_DB().get(STAGING), m)
            proc.send_signal(signal.SIGCONT)
            stopped = False
            out, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                if stopped:
                    proc.send_signal(signal.SIGCONT)
                proc.kill()
                proc.communicate()
        assert b"RAISED BloomRebuildLostLockError" in out, (out, err)
        assert missing(HashRace, seeded + extra) == 0
        if order == "nobody":
            assert f.hash_version(HashRace) == 1
            assert rebuild_keys(HashRace) == []
        elif order == "r2_completed":
            assert f.hash_version(HashRace) == 2
            assert rebuild_keys(HashRace) == []
        else:
            # R1 resumed, re-saved, was refused and aborted -- and left R2's
            # lock and staging key in place, every R2 bit still set.
            assert f.hash_version(HashRace) == 1
            assert get_REDIS_DB().get(LOCK) == t2.encode()
            assert staged <= set_positions(get_REDIS_DB().get(STAGING), m)
            assert f._renew_v2_rebuild(HashRace, t2) is True
            f._finish_v2_rebuild(HashRace, t2)
            assert f.hash_version(HashRace) == 2
            assert rebuild_keys(HashRace) == []
        assert missing(HashRace, seeded + extra) == 0
