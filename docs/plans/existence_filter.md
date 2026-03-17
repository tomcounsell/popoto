---
status: Planning
type: feature
appetite: Medium
owner: Valor
created: 2026-03-17
tracking: https://github.com/tomcounsell/popoto/issues/213
last_comment_id:
---

# ExistenceFilter — Fast Pre-Retrieval Check via Lua-Based Bloom Filter

## Problem

When an agent retrieves memories, it often queries for topics that have no stored records at all. Each miss still pays the full cost of a composite query (ZUNIONSTORE, ZREVRANGE, hydration). For agents that scan across many topics, 30-60% of queries may target empty topic spaces.

**Current behavior:**
Every retrieval query runs the full pipeline regardless of whether any records exist for the given fingerprint. There is no fast-path "definitely nothing here" check.

**Desired outcome:**
An `ExistenceFilter` field type backed by a Bloom filter implemented with Redis strings (`SETBIT`/`GETBIT`) and Lua scripts. Answers "have I ever stored a record matching this fingerprint?" in O(1). When `definitely_missing()` returns True, the caller can skip expensive retrieval entirely. A companion `FrequencySketch` field uses Lua + Redis strings for approximate frequency counting via Count-Min Sketch.

**Valkey compatibility constraint:**
Popoto supports both Redis and Valkey. The RedisBloom module (`BF.*`, `CMS.*` commands) is not available on Valkey. Therefore, both ExistenceFilter and FrequencySketch must use only core Redis data structures (strings with bitwise ops, hashes) and Lua scripts — no module dependencies.

## Prior Art

- **Issue #212 / PR #221-222**: CompositeScoreQuery — the primary retrieval method that ExistenceFilter would pre-filter. Shipped.
- **Issue #208**: WriteFilterMixin — gates save() based on scoring. ExistenceFilter must respect this: records rejected by WriteFilter must NOT be added to the Bloom filter.
- **PR #194**: Agent Memory docs and roadmap — defines ExistenceFilter as Step 8.

No prior issues or PRs found related to Bloom filters or Count-Min Sketch in this repository. This is greenfield work.

## Data Flow

1. **On save**: Model.save() executes normally. After persistence, the ExistenceFilter field's `on_save()` hook computes a fingerprint via the configured `fingerprint_fn` and runs a Lua script that hashes the fingerprint with `k` hash functions and sets the corresponding bits in a Redis string via `SETBIT`.
2. **WriteFilter integration**: If the model uses WriteFilterMixin and the save is rejected (SkipSaveException), `on_save()` hooks are never called, so the Bloom filter is never updated. No special code needed — the existing save flow handles this.
3. **Pre-retrieval check**: Before running CompositeScoreQuery, caller invokes `ExistenceFilter.might_exist(fingerprint)` which runs a Lua script checking all `k` bit positions via `GETBIT`. If any bit is 0, returns False (definitely missing).
4. **FrequencySketch on save**: Similar to ExistenceFilter — `on_save()` runs a Lua script that hashes the fingerprint with `d` hash functions and increments counters in `d` Redis hash fields. On query, `FrequencySketch.get_frequency(fingerprint)` returns the minimum counter value across all hash functions.
5. **On delete**: `on_delete()` is a no-op for both field types. Bloom filters do not support removal (by design — false negatives are impossible). CMS does not support decrement.

## Architectural Impact

- **New dependencies**: None. Uses only core Redis commands (`SETBIT`, `GETBIT`, `HGET`, `HINCRBY`) and Lua scripts via `EVAL`. Works on both Redis and Valkey without any modules.
- **Interface changes**: Two new field types (`ExistenceFilter`, `FrequencySketch`). No changes to existing field types or the query system.
- **Coupling**: Low. ExistenceFilter is a standalone field type. Integration with CompositeScoreQuery is at the application level (caller decides to check before querying), not inside the query system.
- **Data ownership**: Each field owns its own Redis key (`$EF:{Class}:{field}` for Bloom filter bit string, `$FS:{Class}:{field}:{row}` for CMS counter hashes).
- **Reversibility**: Easy — purely additive. Removing the field type removes the Bloom/CMS keys but has no effect on model data.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0 (well-defined by the roadmap issue)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis or Valkey server | `python -c "from popoto.redis_db import POPOTO_REDIS_DB; POPOTO_REDIS_DB.ping()"` | Core Redis commands + Lua scripting available |

No Redis modules required. The Bloom filter and Count-Min Sketch are implemented entirely with Lua scripts, `SETBIT`/`GETBIT` (for Bloom), and `HINCRBY`/`HGET` (for CMS).

## Solution

### Key Elements

- **`ExistenceFilter` field**: A Field subclass that maintains a Bloom filter (via Redis string + Lua) alongside the model. Provides `might_exist()` and `definitely_missing()` class-level methods.
- **`FrequencySketch` field**: A Field subclass that maintains a Count-Min Sketch (via Redis hashes + Lua). Provides `get_frequency()` class-level method.
- **Fingerprint function**: Configurable callable that derives a fingerprint string from a model instance. Defaults to using the model's redis_key.
- **Lua-based hashing**: Both data structures use Lua scripts for atomic multi-bit/multi-counter operations. Hash functions are computed in Lua using a seeded variant of DJB2/FNV to produce `k` independent bit positions (Bloom) or `d` independent counter slots (CMS).

### Flow

**Save path**: Model.save() → WriteFilter check (if applicable) → persist data → Field.on_save() → ExistenceFilter.on_save() computes fingerprint → Lua script sets `k` bits via `SETBIT`

**Query path**: Application calls `ExistenceFilter.might_exist(model_class, fingerprint)` → Lua script checks `k` bits via `GETBIT` → True/False

**Frequency path**: Application calls `FrequencySketch.get_frequency(model_class, fingerprint)` → Lua script reads `d` counters via `HGET` → returns minimum

### Technical Approach

#### Bloom Filter via Redis Strings + Lua

A Bloom filter is a bit array of size `m` with `k` hash functions. For a given `error_rate` and `capacity`:
- `m = -capacity * ln(error_rate) / (ln(2)^2)` — total bits
- `k = (m / capacity) * ln(2)` — optimal number of hash functions

Redis strings support `SETBIT`/`GETBIT` on arbitrary offsets up to 512MB (2^32 bits). A single Redis string key serves as the entire bit array.

Hash functions are computed in Lua using double hashing: `h_i(x) = (h1(x) + i * h2(x)) mod m`, where `h1` and `h2` are derived from a single hash of the fingerprint string. This is the standard Kirschner-Mitzenmacher optimization — two hash functions simulate `k` independent ones.

#### ExistenceFilter Field Implementation

ExistenceFilter is NOT a standard value field — it does not store a value on the model instance. It is a "side-effect field" that only maintains a Bloom filter index via `on_save()`. Similar pattern to how SortedFieldMixin maintains a sorted set index.

```python
class ExistenceFilter(Field):
    """Bloom filter for O(1) probabilistic membership checks.

    Implemented with Redis SETBIT/GETBIT and Lua scripts.
    No Redis modules required — works on both Redis and Valkey.
    """

    # Config
    error_rate: float = 0.01
    capacity: int = 100_000
    fingerprint_fn: Callable = None  # defaults to model redis_key

    def on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs):
        fingerprint = cls._compute_fingerprint(model_instance)
        key = cls._bloom_key(model_instance)
        m, k = cls._compute_params()
        # Lua script atomically sets k bits
        POPOTO_REDIS_DB.eval(BLOOM_ADD_LUA, 1, key, fingerprint, m, k)

    @classmethod
    def might_exist(cls, model_class, fingerprint: str) -> bool:
        key = f"$EF:{model_class.__name__}:{cls.name}"
        m, k = cls._compute_params()
        return bool(POPOTO_REDIS_DB.eval(BLOOM_EXISTS_LUA, 1, key, fingerprint, m, k))

    @classmethod
    def definitely_missing(cls, model_class, fingerprint: str) -> bool:
        return not cls.might_exist(model_class, fingerprint)
```

**Lua script for BLOOM_ADD:**
```lua
local key = KEYS[1]
local item = ARGV[1]
local m = tonumber(ARGV[2])
local k = tonumber(ARGV[3])

-- Double hashing: h1 and h2 from DJB2 variants
local h1 = 5381
local h2 = 0x01000193  -- FNV offset basis
for i = 1, #item do
    local c = string.byte(item, i)
    h1 = ((h1 * 33) + c) % m
    h2 = ((h2 * 0x01000193) + c) % m  -- FNV-1 step
end

for i = 0, k - 1 do
    local pos = (h1 + i * h2) % m
    redis.call('SETBIT', key, pos, 1)
end
return 1
```

**Lua script for BLOOM_EXISTS:**
```lua
local key = KEYS[1]
local item = ARGV[1]
local m = tonumber(ARGV[2])
local k = tonumber(ARGV[3])

local h1 = 5381
local h2 = 0x01000193
for i = 1, #item do
    local c = string.byte(item, i)
    h1 = ((h1 * 33) + c) % m
    h2 = ((h2 * 0x01000193) + c) % m
end

for i = 0, k - 1 do
    local pos = (h1 + i * h2) % m
    if redis.call('GETBIT', key, pos) == 0 then
        return 0
    end
end
return 1
```

#### Redis Key Patterns

- Bloom filter: `$EF:{ClassName}:{field_name}` — single Redis string key per field per model class (bit array)
- Count-Min Sketch: `$FS:{ClassName}:{field_name}` — single Redis hash per field per model class (rows as hash fields)

These follow the existing Popoto convention: `$` prefix for internal keys, short mnemonic (`EF` = ExistenceFilter, `FS` = FrequencySketch), then class and field name.

#### FrequencySketch Field Implementation (Count-Min Sketch via Lua + Redis Hash)

A Count-Min Sketch uses a `d × w` matrix of counters. Each row uses a different hash function. To increment: hash with each of `d` functions, increment the corresponding counter in each row. To query: return the minimum counter value across all rows.

Implementation: a single Redis hash where field names encode `row:column` and values are integer counters. Lua scripts perform atomic multi-counter operations.

```python
class FrequencySketch(Field):
    """Count-Min Sketch for approximate frequency queries.

    Implemented with Redis hashes and Lua scripts.
    No Redis modules required — works on both Redis and Valkey.
    """

    width: int = 2000       # number of counters per row
    depth: int = 7          # number of hash functions (rows)
    fingerprint_fn: Callable = None

    def on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs):
        fingerprint = cls._compute_fingerprint(model_instance)
        key = cls._cms_key(model_instance)
        # Lua script atomically increments d counters
        POPOTO_REDIS_DB.eval(CMS_INCR_LUA, 1, key, fingerprint, cls.width, cls.depth)

    @classmethod
    def get_frequency(cls, model_class, fingerprint: str) -> int:
        key = f"$FS:{model_class.__name__}:{cls.name}"
        result = POPOTO_REDIS_DB.eval(
            CMS_QUERY_LUA, 1, key, fingerprint, cls.width, cls.depth
        )
        return int(result) if result else 0
```

**Lua script for CMS_INCR:**
```lua
local key = KEYS[1]
local item = ARGV[1]
local w = tonumber(ARGV[2])
local d = tonumber(ARGV[3])

for row = 0, d - 1 do
    -- Seeded hash per row
    local h = 5381 + row * 0x01000193
    for i = 1, #item do
        h = ((h * 33) + string.byte(item, i)) % w
    end
    redis.call('HINCRBY', key, row .. ':' .. h, 1)
end
return 1
```

**Lua script for CMS_QUERY:**
```lua
local key = KEYS[1]
local item = ARGV[1]
local w = tonumber(ARGV[2])
local d = tonumber(ARGV[3])

local min_count = math.huge
for row = 0, d - 1 do
    local h = 5381 + row * 0x01000193
    for i = 1, #item do
        h = ((h * 33) + string.byte(item, i)) % w
    end
    local val = tonumber(redis.call('HGET', key, row .. ':' .. h)) or 0
    if val < min_count then min_count = val end
end
return min_count
```

#### Pipeline Support

Lua scripts via `EVAL` support pipelining. The `on_save()` hook receives the pipeline parameter and should use it when available for atomic saves.

```python
def on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs):
    fingerprint = cls._compute_fingerprint(model_instance)
    key = cls._bloom_key(model_instance)
    m, k = cls._compute_params()
    client = pipeline if pipeline else POPOTO_REDIS_DB
    client.eval(BLOOM_ADD_LUA, 1, key, fingerprint, m, k)
```

#### WriteFilter Integration

No special code needed. The existing save flow in `Model.save()` (line 1048-1055 of `base.py`) raises `SkipSaveException` before any `on_save()` hooks are called. Therefore, records rejected by WriteFilter will never have their fingerprints added to the Bloom filter.

Synergy test should verify this: save a record below the WriteFilter threshold, confirm `might_exist()` returns False.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `EVAL` on a key with unexpected type (e.g., a hash where a string is expected) → Lua script error. Test that it propagates cleanly.
- [ ] Bloom filter bit array exceeds Redis string max size (512MB) → would require capacity > ~4 billion. Document this theoretical limit but don't guard against it (unrealistic for agent memory).

### Empty/Invalid Input Handling
- [ ] `might_exist()` with empty string fingerprint → should work (hash functions handle empty strings)
- [ ] `might_exist()` when Bloom filter key doesn't exist → `GETBIT` on non-existent key returns 0, so Lua correctly returns 0 (definitely missing). Verify this behavior.
- [ ] `get_frequency()` when CMS key doesn't exist → `HGET` on non-existent key returns nil, Lua defaults to 0. Verify.
- [ ] `fingerprint_fn` returns None → convert to string "None" or raise ValueError. Choose explicit error.

### Error State Rendering
- [ ] Not applicable — this is an ORM field type, not user-facing UI.

## Test Impact

No existing tests affected — this is a greenfield feature with no prior test coverage. All tests are new.

## Rabbit Holes

- **Bloom filter deletion/counting variants**: Don't use Cuckoo filters or counting Bloom filters to support deletion. Standard Bloom filters are simpler, more space-efficient, and deletion is not needed (false negatives are impossible, and stale positives are harmless for a pre-filter).
- **Auto-integration with CompositeScoreQuery**: Don't modify `composite_score()` to automatically check ExistenceFilter. The caller should explicitly call `definitely_missing()` before querying. Automatic integration adds coupling and removes caller control over the trade-off.
- **Cryptographic hash functions**: Don't use SHA/MD5 for hash functions. DJB2-based double hashing in Lua is fast and sufficient for Bloom filter quality. The theoretical analysis by Kirschner & Mitzenmacher (2006) proves double hashing provides the same guarantees as fully independent hash functions.
- **Multi-field fingerprints**: Don't build a framework for combining multiple fields into a fingerprint. The `fingerprint_fn` callable is sufficient — the caller can compose any fingerprint logic they need.
- **RedisBloom module support**: Don't add an optional path using `BF.*`/`CMS.*` commands when RedisBloom is available. The Lua implementation works everywhere and keeps the codebase simple. Performance difference is negligible for agent memory workloads.

## Risks

### Risk 1: Lua Hash Function Quality
**Impact:** Poor hash distribution could cause higher-than-expected false positive rates.
**Mitigation:** DJB2 + FNV double hashing is a well-studied approach. Include a statistical test that verifies the actual false positive rate is within 2x of the configured `error_rate` across 10,000+ items.

### Risk 2: Bloom Filter Capacity Overflow
**Impact:** When the number of items exceeds the configured capacity, the false positive rate degrades beyond the configured error_rate.
**Mitigation:** Provide a `fill_ratio()` diagnostic method that checks the proportion of set bits via Lua. Document the capacity parameter prominently. Default capacity of 100,000 is reasonable for most agent memory use cases.

## Race Conditions

No race conditions identified. Lua scripts execute atomically in Redis/Valkey. Multiple concurrent saves adding to the same Bloom filter is safe — Bloom filter insertions are idempotent (setting already-set bits is a no-op). CMS increments are also safe under concurrency (each Lua script execution is atomic).

## No-Gos (Out of Scope)

- Automatic CompositeScoreQuery integration (caller-side, not ORM-side)
- Bloom filter expiration/rotation (defer to a later PR)
- Async variants of might_exist/definitely_missing (follow pattern of other async additions)
- ContextAssembler token budgeting (Step 12 concern)
- Cuckoo filter support (unnecessary complexity)
- Bloom filter deletion support (contradicts the data structure's guarantees)
- Optional RedisBloom module path (unnecessary complexity for negligible perf gain)

## Update System

No update system changes required — this is a library feature in the Popoto ORM package.

## Agent Integration

No agent integration required — this is an ORM field type consumed by application code.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/agent-memory.md` ExistenceFilter section with shipped API
- [ ] Add ExistenceFilter and FrequencySketch to field type documentation

### External Documentation Site
- [ ] Update mkdocs pages if applicable
- [ ] Verify docs build passes

### Inline Documentation
- [ ] Docstrings on ExistenceFilter and FrequencySketch with usage examples
- [ ] Document that no Redis modules are needed (works on Redis and Valkey)
- [ ] Document fingerprint_fn contract

## Success Criteria

- [ ] `ExistenceFilter` field type with Lua-based Bloom filter (`SETBIT`/`GETBIT`) on save and query
- [ ] `might_exist(fingerprint)` and `definitely_missing(fingerprint)` class methods
- [ ] Configurable `error_rate`, `capacity`, and `fingerprint_fn`
- [ ] `FrequencySketch` field with Lua-based Count-Min Sketch (`HINCRBY`/`HGET`)
- [ ] No Redis module dependencies — works on both Redis and Valkey
- [ ] Records filtered by WriteFilterMixin are NOT added to the Bloom filter
- [ ] Pipeline support in `on_save()` for atomic batch saves
- [ ] `on_delete()` is a no-op (documented design decision)
- [ ] Graceful handling when Bloom/CMS key doesn't exist yet (GETBIT/HGET on missing keys return 0)
- [ ] Statistical test: false positive rate within 2x of configured `error_rate` across 10,000+ items
- [ ] Synergy test: ExistenceFilter + WriteFilter — filtered record not in Bloom
- [ ] Synergy test: ExistenceFilter as pre-filter — `definitely_missing()` returns True for unseen fingerprints, False for seen ones
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (existence-filter)**
  - Name: ef-builder
  - Role: Implement ExistenceFilter and FrequencySketch field types
  - Agent Type: builder
  - Resume: true

- **Builder (tests)**
  - Name: test-builder
  - Role: Implement tests for both field types and synergy tests
  - Agent Type: test-engineer
  - Resume: true

- **Validator (existence-filter)**
  - Name: ef-validator
  - Role: Verify implementation meets all success criteria
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Update agent-memory docs and field type documentation
  - Agent Type: documentarian
  - Resume: true

### Available Agent Types

**Tier 1 — Core (default choices):**
- `builder` - General implementation
- `validator` - Read-only verification
- `test-engineer` - Test implementation

## Step by Step Tasks

### 1. Implement ExistenceFilter and FrequencySketch fields
- **Task ID**: build-existence-filter
- **Depends On**: none
- **Assigned To**: ef-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/fields/existence_filter.py` with `ExistenceFilter` class
- Implement Bloom filter Lua scripts (BLOOM_ADD_LUA, BLOOM_EXISTS_LUA) using DJB2+FNV double hashing
- Implement `on_save()` hook: compute fingerprint, eval BLOOM_ADD_LUA (with pipeline support)
- Implement `on_delete()` as explicit no-op with docstring explaining why
- Implement `might_exist()` and `definitely_missing()` class methods via BLOOM_EXISTS_LUA
- Implement `_compute_params()` to derive `m` (bits) and `k` (hash functions) from `error_rate` and `capacity`
- Implement `_compute_fingerprint()` using configurable `fingerprint_fn` or model redis_key default
- Create `FrequencySketch` class in same file (or separate `frequency_sketch.py`)
- Implement CMS Lua scripts (CMS_INCR_LUA, CMS_QUERY_LUA) using seeded DJB2 hashing
- Implement `on_save()` hook: compute fingerprint, eval CMS_INCR_LUA
- Implement `get_frequency()` class method via CMS_QUERY_LUA
- Register both field types in `src/popoto/fields/__init__.py` if needed
- Ensure FieldBase metaclass assigns correct `field_class_key` (`$EF` and `$FS`)

### 2. Implement tests
- **Task ID**: build-tests
- **Depends On**: build-existence-filter
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Create `tests/test_existence_filter.py`
- Test ExistenceFilter: add item, verify `might_exist()` returns True
- Test ExistenceFilter: query unseen item, verify `definitely_missing()` returns True
- Test ExistenceFilter: false positive rate within 2x of configured bounds (statistical test with 10,000+ items)
- Test FrequencySketch: increment and query frequency
- Test FrequencySketch: multiple increments accumulate correctly
- Test custom `fingerprint_fn` works
- Test default fingerprint (redis_key) works
- Synergy: WriteFilterMixin + ExistenceFilter — rejected record not in Bloom
- Synergy: ExistenceFilter pre-filter pattern — verify short-circuit saves full query
- Test pipeline support: `on_save()` works within a Redis pipeline
- Test graceful behavior when Bloom/CMS key doesn't exist yet (first query before any save)
- Test `on_delete()` is a no-op (Bloom still contains the fingerprint after delete)
- No module dependencies — tests run on vanilla Redis and Valkey

### 3. Validate implementation
- **Task ID**: validate-existence-filter
- **Depends On**: build-tests
- **Assigned To**: ef-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify all success criteria met
- Check Redis key patterns match convention (`$EF:`, `$FS:`)
- Verify Lua scripts use no module commands (Redis + Valkey compatible)

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-existence-filter
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/agent-memory.md` ExistenceFilter section
- Add docstrings with examples
- Document Valkey compatibility (no modules required)

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: ef-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all tests
- Verify documentation builds
- Verify all success criteria met
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| ExistenceFilter tests pass | `pytest tests/test_existence_filter.py -v` | exit code 0 |
| Import works | `python -c "from popoto.fields.existence_filter import ExistenceFilter, FrequencySketch"` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |

---

## Open Questions

1. **Fingerprint default**: The roadmap specifies `fingerprint_fn: Callable` as a required config. The Bloom filter is most useful for topic-level pre-filtering, not per-instance checks. **Decision: require `fingerprint_fn`** — no default to `redis_key`. If the caller wants per-instance checks, they can pass `fingerprint_fn=lambda inst: inst.db_key.redis_key`, but the common case is topic strings.

2. **FrequencySketch config**: Since we're implementing CMS in Lua (not using `CMS.INITBYPROB`), expose `width` and `depth` directly. These map to the matrix dimensions the Lua script uses. **Decision: `width`/`depth` parameters** (default `width=2000, depth=7`), which gives roughly 0.1% error rate.

3. **Test environment**: No longer an issue. The Lua-based implementation uses only core Redis commands (`SETBIT`, `GETBIT`, `HINCRBY`, `HGET`, `EVAL`). **Decision: tests run on vanilla Redis and Valkey** — no skip markers needed.
