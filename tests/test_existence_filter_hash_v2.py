"""ExistenceFilter hash v2 and the legacy v1 filters it must keep serving (#775).

The pre-#775 bloom hash did its arithmetic in Lua doubles past 2^53, so
similar tokens collapsed onto a handful of positions (400 of them set 4 of
66 bits). v2 keeps every intermediate exact. Changing the hash in place would
make every token already added to a live filter test absent, so the version
is stored per filter, in the filter's own bytes, and a v1 filter keeps its
hash until ``rebuild_indexes()`` rebuilds it.

These tests are Redis-path tests (the bit array has no Postgres counterpart;
Postgres stores an exact token table). The cross-backend contract -- no false
negatives, bounded false positives -- is pinned in the conformance module
``test_existence_filter.py``.

The v1 filters here are built by the **pre-#775 scripts themselves**, copied
verbatim below from ``src/popoto/fields/existence_filter.py`` as of
``origin/main`` before #775. Do not edit them: they are the old code, frozen,
and every compatibility assertion is only as good as their fidelity.
"""

import io
import math
import random

import pytest

from src import popoto
from src.popoto.fields import existence_filter as ef
from src.popoto.fields.existence_filter import (
    BLOOM_REBUILD_SUFFIX,
    BLOOM_V2_HEADER,
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
        error_rate=0.05, capacity=1000, fingerprint_fn=lambda inst: inst.topic
    )


class HashV2Big(popoto.Model):
    """capacity 10,000 at 5%: m = 62,352 bits, k = 4."""

    name = popoto.UniqueKeyField()
    topic = popoto.Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.05, capacity=10_000, fingerprint_fn=lambda inst: inst.topic
    )


class HashV2Tiny(popoto.Model):
    """The #775 report's filter: capacity 20 at 20% -> m = 66 bits, k = 2."""

    name = popoto.UniqueKeyField()
    topic = popoto.Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.2, capacity=20, fingerprint_fn=lambda inst: inst.topic
    )


MODELS = (HashV2Doc, HashV2Big, HashV2Tiny)


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

    The legacy scripts read only ``KEYS[1]``, so the staging key the current
    ``on_save`` passes as ``KEYS[2]`` is ignored, exactly as the old
    ``numkeys=1`` call did. Every bit they set is the old code's.
    """
    monkeypatch.setattr(ef, "BLOOM_ADD_LUA", LEGACY_BLOOM_ADD_LUA)
    monkeypatch.setattr(ef, "BLOOM_ADD_MULTI_LUA", LEGACY_BLOOM_ADD_MULTI_LUA)
    return monkeypatch


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
                ef.BLOOM_ADD_MULTI_LUA,
                2,
                key,
                key + BLOOM_REBUILD_SUFFIX,
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
        assert HashV2Doc.rebuild_indexes() == 400
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 2
        assert not get_REDIS_DB().exists(bloom_key(HashV2Doc) + BLOOM_REBUILD_SUFFIX)
        assert HashV2Doc.check_indexes()["legacy_hash"] == []
        assert all(HashV2Doc.bloom.might_exist_batch(HashV2Doc, tokens).values())
        m, k = HashV2Doc.bloom._compute_params()
        want = set().union(*(v2_positions(t, m, k) for t in tokens))
        raw = get_REDIS_DB().get(bloom_key(HashV2Doc))
        assert set_positions(raw, m) == want  # exactly the records' v2 bits
        # Later saves write v2 now.
        HashV2Doc(name="after", topic="postrebuild").save()
        assert HashV2Doc.bloom.might_exist(HashV2Doc, "postrebuild")

    def test_rebuild_leaves_v2_filter_alone(self):
        save_all(HashV2Doc, similar_tokens(10))
        before = get_REDIS_DB().get(bloom_key(HashV2Doc))
        HashV2Doc.rebuild_indexes()
        assert get_REDIS_DB().get(bloom_key(HashV2Doc)) == before

    def test_save_racing_rebuild_is_not_lost(self, legacy_writes):
        """A save that lands while the staging key is open dual-writes it, so
        the swap does not drop the racing record's tokens."""
        self._build_v1(legacy_writes, similar_tokens(30))
        staging = HashV2Doc.bloom._begin_v2_rebuild(HashV2Doc)
        assert staging == bloom_key(HashV2Doc) + BLOOM_REBUILD_SUFFIX
        HashV2Doc(name="racer", topic="racingtoken").save()
        # Live filter is still v1 and already answers for it.
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 1
        assert HashV2Doc.bloom.might_exist(HashV2Doc, "racingtoken")
        HashV2Doc.bloom._finish_v2_rebuild(HashV2Doc, staging)
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
            HashV2Doc.rebuild_indexes()
        assert HashV2Doc.bloom.hash_version(HashV2Doc) == 1
        assert not get_REDIS_DB().exists(bloom_key(HashV2Doc) + BLOOM_REBUILD_SUFFIX)
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
                patch.setattr(ef, "BLOOM_ADD_MULTI_LUA", LEGACY_BLOOM_ADD_MULTI_LUA)
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
        ):
            assert _family_of(key, names, {}) == ("HashV2Doc", "$EF")
        assert "$EF" in FAMILY_DISPOSITIONS
