---
status: Planning
type: feature
appetite: Medium
owner: Valor
created: 2026-03-17
tracking: https://github.com/tomcounsell/popoto/issues/213
last_comment_id:
---

# ExistenceFilter — Fast Pre-Retrieval Check via Redis Bloom Filter

## Problem

When an agent retrieves memories, it often queries for topics that have no stored records at all. Each miss still pays the full cost of a composite query (ZUNIONSTORE, ZREVRANGE, hydration). For agents that scan across many topics, 30-60% of queries may target empty topic spaces.

**Current behavior:**
Every retrieval query runs the full pipeline regardless of whether any records exist for the given fingerprint. There is no fast-path "definitely nothing here" check.

**Desired outcome:**
An `ExistenceFilter` field type backed by a Redis Bloom filter that answers "have I ever stored a record matching this fingerprint?" in O(1). When `definitely_missing()` returns True, the caller can skip expensive retrieval entirely. A companion `FrequencySketch` field wraps Count-Min Sketch for approximate frequency counting.

## Prior Art

- **Issue #212 / PR #221-222**: CompositeScoreQuery — the primary retrieval method that ExistenceFilter would pre-filter. Shipped.
- **Issue #208**: WriteFilterMixin — gates save() based on scoring. ExistenceFilter must respect this: records rejected by WriteFilter must NOT be added to the Bloom filter.
- **PR #194**: Agent Memory docs and roadmap — defines ExistenceFilter as Step 8.

No prior issues or PRs found related to Bloom filters or Count-Min Sketch in this repository. This is greenfield work.

## Data Flow

1. **On save**: Model.save() executes normally. After persistence, the ExistenceFilter field's `on_save()` hook computes a fingerprint via the configured `fingerprint_fn` and calls `BF.ADD` on the Bloom filter key.
2. **WriteFilter integration**: If the model uses WriteFilterMixin and the save is rejected (SkipSaveException), `on_save()` hooks are never called, so the Bloom filter is never updated. No special code needed — the existing save flow handles this.
3. **Pre-retrieval check**: Before running CompositeScoreQuery, caller invokes `ExistenceFilter.might_exist(fingerprint)` which calls `BF.EXISTS`. If False, the caller skips the full query.
4. **FrequencySketch on save**: Similar to ExistenceFilter — `on_save()` calls `CMS.INCRBY` with the fingerprint. On query, `FrequencySketch.query(fingerprint)` calls `CMS.QUERY` to get the approximate count.
5. **On delete**: `on_delete()` is a no-op for both field types. Bloom filters do not support removal (by design — false negatives are impossible). CMS does not support decrement.

## Architectural Impact

- **New dependencies**: None new. `redis-py` already exposes `BF.*` and `CMS.*` commands via `redis.commands.bf.BFBloom` and `redis.commands.bf.CMSBloom`. Requires Redis Stack (RedisBloom module) on the server.
- **Interface changes**: Two new field types (`ExistenceFilter`, `FrequencySketch`). No changes to existing field types or the query system.
- **Coupling**: Low. ExistenceFilter is a standalone field type. Integration with CompositeScoreQuery is at the application level (caller decides to check before querying), not inside the query system.
- **Data ownership**: Each field owns its own Redis key (`$EF:{Class}:{field}` for Bloom, `$FS:{Class}:{field}` for CMS).
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
| Redis Stack with RedisBloom | `python -c "from popoto.redis_db import POPOTO_REDIS_DB; POPOTO_REDIS_DB.bf().reserve('_popoto_test_bf', 0.01, 100); POPOTO_REDIS_DB.delete('_popoto_test_bf')"` | BF.* commands available |
| Redis Stack with CMS | `python -c "from popoto.redis_db import POPOTO_REDIS_DB; POPOTO_REDIS_DB.cms().initbyprob('_popoto_test_cms', 0.001, 0.01); POPOTO_REDIS_DB.delete('_popoto_test_cms')"` | CMS.* commands available |

## Solution

### Key Elements

- **`ExistenceFilter` field**: A Field subclass that maintains a Redis Bloom filter alongside the model. Provides `might_exist()` and `definitely_missing()` class-level methods.
- **`FrequencySketch` field**: A Field subclass that maintains a Redis Count-Min Sketch. Provides `get_frequency()` class-level method.
- **Fingerprint function**: Configurable callable that derives a fingerprint string from a model instance. Defaults to using the model's redis_key.
- **Lazy initialization**: Bloom filter and CMS are created on first `BF.ADD`/`CMS.INCRBY` via `BF.RESERVE`/`CMS.INITBYPROB` if they don't exist, using redis-py's built-in error handling.

### Flow

**Save path**: Model.save() → WriteFilter check (if applicable) → persist data → Field.on_save() → ExistenceFilter.on_save() computes fingerprint → `BF.ADD` key fingerprint

**Query path**: Application calls `ExistenceFilter.might_exist(model_class, fingerprint)` → `BF.EXISTS` key fingerprint → True/False

**Frequency path**: Application calls `FrequencySketch.get_frequency(model_class, fingerprint)` → `CMS.QUERY` key fingerprint → approximate count

### Technical Approach

#### ExistenceFilter Field Implementation

ExistenceFilter is NOT a standard value field — it does not store a value on the model instance. It is a "side-effect field" that only maintains a Bloom filter index via `on_save()`. Similar pattern to how SortedFieldMixin maintains a sorted set index.

```python
class ExistenceFilter(Field):
    """Bloom filter for O(1) probabilistic membership checks."""

    # Config
    error_rate: float = 0.01
    capacity: int = 100_000
    fingerprint_fn: Callable = None  # defaults to model redis_key

    def on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs):
        fingerprint = cls._compute_fingerprint(model_instance)
        key = cls._bloom_key(model_instance)
        # BF.ADD auto-creates filter if it doesn't exist (with default params)
        # For explicit control, use BF.RESERVE first
        POPOTO_REDIS_DB.bf().add(key, fingerprint)

    @classmethod
    def might_exist(cls, model_class, fingerprint: str) -> bool:
        key = f"$EF:{model_class.__name__}:{cls.name}"
        return bool(POPOTO_REDIS_DB.bf().exists(key, fingerprint))

    @classmethod
    def definitely_missing(cls, model_class, fingerprint: str) -> bool:
        return not cls.might_exist(model_class, fingerprint)
```

#### Redis Key Patterns

- Bloom filter: `$EF:{ClassName}:{field_name}` — single key per field per model class
- Count-Min Sketch: `$FS:{ClassName}:{field_name}` — single key per field per model class

These follow the existing Popoto convention: `$` prefix for internal keys, short mnemonic (`EF` = ExistenceFilter, `FS` = FrequencySketch), then class and field name.

#### FrequencySketch Field Implementation

```python
class FrequencySketch(Field):
    """Count-Min Sketch for approximate frequency queries."""

    # CMS config
    width: int = 2000       # number of counters per hash
    depth: int = 7          # number of hash functions
    # Alternative: error_rate + probability for initbyprob
    error_rate: float = 0.001
    probability: float = 0.01
    fingerprint_fn: Callable = None

    def on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs):
        fingerprint = cls._compute_fingerprint(model_instance)
        key = cls._cms_key(model_instance)
        POPOTO_REDIS_DB.cms().incrby(key, [fingerprint], [1])

    @classmethod
    def get_frequency(cls, model_class, fingerprint: str) -> int:
        key = f"$FS:{model_class.__name__}:{cls.name}"
        result = POPOTO_REDIS_DB.cms().query(key, fingerprint)
        return result[0] if result else 0
```

#### Lazy Initialization via BF.RESERVE / CMS.INITBYPROB

On the first save, the Bloom filter or CMS may not exist. Two strategies:

1. **Option A (chosen)**: Use `BF.INSERT` with error/capacity params instead of `BF.ADD`. This auto-creates with specified params if the filter doesn't exist. For CMS, call `CMS.INITBYPROB` wrapped in try/except (ignore "already exists" error).

2. Option B: Check existence first with `EXISTS` key, then `RESERVE`/`INITBYPROB`. Costs an extra round-trip on every save.

#### Pipeline Support

Both `BF.ADD` and `CMS.INCRBY` support pipelining via redis-py. The `on_save()` hook receives the pipeline parameter and should use it when available for atomic saves.

```python
def on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs):
    fingerprint = cls._compute_fingerprint(model_instance)
    key = cls._bloom_key(model_instance)
    client = pipeline if pipeline else POPOTO_REDIS_DB
    # Use execute_command for pipeline compatibility
    client.execute_command("BF.ADD", key, fingerprint)
```

#### WriteFilter Integration

No special code needed. The existing save flow in `Model.save()` (line 1048-1055 of `base.py`) raises `SkipSaveException` before any `on_save()` hooks are called. Therefore, records rejected by WriteFilter will never have their fingerprints added to the Bloom filter.

Synergy test should verify this: save a record below the WriteFilter threshold, confirm `might_exist()` returns False.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `BF.ADD` on a non-Bloom key type → should raise redis.ResponseError. Test that the error propagates (not swallowed).
- [ ] `CMS.INCRBY` on a non-CMS key type → same.
- [ ] RedisBloom module not loaded → `BF.ADD` raises "unknown command" error. Document this as a prerequisite. Test with a clear error message.

### Empty/Invalid Input Handling
- [ ] `might_exist()` with empty string fingerprint → should work (BF.EXISTS accepts any string)
- [ ] `might_exist()` when Bloom filter key doesn't exist → BF.EXISTS returns 0 (filter not found = definitely missing). Verify this behavior.
- [ ] `get_frequency()` when CMS key doesn't exist → CMS.QUERY returns error. Handle gracefully, return 0.
- [ ] `fingerprint_fn` returns None → convert to string "None" or raise ValueError. Choose explicit error.

### Error State Rendering
- [ ] Not applicable — this is an ORM field type, not user-facing UI.

## Test Impact

No existing tests affected — this is a greenfield feature with no prior test coverage. All tests are new.

## Rabbit Holes

- **Bloom filter deletion/counting variants**: Don't use Cuckoo filters or counting Bloom filters to support deletion. Standard Bloom filters are simpler, more space-efficient, and deletion is not needed (false negatives are impossible, and stale positives are harmless for a pre-filter).
- **Auto-integration with CompositeScoreQuery**: Don't modify `composite_score()` to automatically check ExistenceFilter. The caller should explicitly call `definitely_missing()` before querying. Automatic integration adds coupling and removes caller control over the trade-off.
- **Bloom filter persistence/backup**: Don't implement `BF.SCANDUMP`/`BF.LOADCHUNK` for Bloom filter migration. Redis persistence (RDB/AOF) handles this automatically.
- **Multi-field fingerprints**: Don't build a framework for combining multiple fields into a fingerprint. The `fingerprint_fn` callable is sufficient — the caller can compose any fingerprint logic they need.

## Risks

### Risk 1: RedisBloom Module Availability
**Impact:** BF.* and CMS.* commands fail if Redis server doesn't have RedisBloom loaded. This is a hard dependency on Redis Stack, not vanilla Redis.
**Mitigation:** Document the requirement clearly. Add a helper function to check module availability. Tests should skip gracefully if RedisBloom is not available (`pytest.importorskip` pattern or custom skip marker).

### Risk 2: Bloom Filter Capacity Overflow
**Impact:** When the number of items exceeds the configured capacity, the false positive rate degrades beyond the configured error_rate.
**Mitigation:** Use `BF.INFO` in a diagnostic method to check current fill ratio. Document the capacity parameter prominently. Default capacity of 100,000 is reasonable for most agent memory use cases.

## Race Conditions

No race conditions identified. BF.ADD and BF.EXISTS are atomic Redis commands. CMS.INCRBY and CMS.QUERY are atomic. Multiple concurrent saves adding to the same Bloom filter is safe — Bloom filter insertions are idempotent (adding the same item twice is a no-op). CMS.INCRBY is also safe under concurrency (each increment is atomic).

## No-Gos (Out of Scope)

- Automatic CompositeScoreQuery integration (caller-side, not ORM-side)
- Bloom filter expiration/rotation (defer to a later PR)
- Async variants of might_exist/definitely_missing (follow pattern of other async additions)
- ContextAssembler token budgeting (Step 12 concern)
- Cuckoo filter support (unnecessary complexity)
- Bloom filter deletion support (contradicts the data structure's guarantees)

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
- [ ] Document RedisBloom prerequisite in module docstring
- [ ] Document fingerprint_fn contract

## Success Criteria

- [ ] `ExistenceFilter` field type with `BF.ADD` on save and `BF.EXISTS` for queries
- [ ] `might_exist(fingerprint)` and `definitely_missing(fingerprint)` class methods
- [ ] Configurable `error_rate`, `capacity`, and `fingerprint_fn`
- [ ] `FrequencySketch` field wrapping Count-Min Sketch (`CMS.INCRBY` / `CMS.QUERY`)
- [ ] Records filtered by WriteFilterMixin are NOT added to the Bloom filter
- [ ] Pipeline support in `on_save()` for atomic batch saves
- [ ] `on_delete()` is a no-op (documented design decision)
- [ ] Graceful handling when Bloom/CMS key doesn't exist yet
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
- Implement `on_save()` hook: compute fingerprint, `BF.ADD` (with pipeline support)
- Implement `on_delete()` as explicit no-op with docstring explaining why
- Implement `might_exist()` and `definitely_missing()` class methods
- Implement lazy Bloom filter initialization via `BF.INSERT` with error/capacity params
- Implement `_compute_fingerprint()` using configurable `fingerprint_fn` or model redis_key default
- Create `FrequencySketch` class in same file (or separate `frequency_sketch.py`)
- Implement `on_save()` hook: compute fingerprint, `CMS.INCRBY`
- Implement `get_frequency()` class method
- Implement lazy CMS initialization via `CMS.INITBYPROB`
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
- Test ExistenceFilter: false positive rate within configured bounds (statistical test with 1000+ items)
- Test FrequencySketch: increment and query frequency
- Test FrequencySketch: multiple increments accumulate correctly
- Test custom `fingerprint_fn` works
- Test default fingerprint (redis_key) works
- Synergy: WriteFilterMixin + ExistenceFilter — rejected record not in Bloom
- Synergy: ExistenceFilter pre-filter pattern — verify short-circuit saves full query
- Test pipeline support: `on_save()` works within a Redis pipeline
- Test graceful behavior when Bloom/CMS key doesn't exist yet (first query before any save)
- Test `on_delete()` is a no-op (Bloom still contains the fingerprint after delete)
- Skip tests gracefully if RedisBloom module is not available

### 3. Validate implementation
- **Task ID**: validate-existence-filter
- **Depends On**: build-tests
- **Assigned To**: ef-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify all success criteria met
- Check Redis key patterns match convention (`$EF:`, `$FS:`)
- Review error handling for missing RedisBloom module

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-existence-filter
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/agent-memory.md` ExistenceFilter section
- Add docstrings with examples
- Document RedisBloom prerequisite

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

1. **Fingerprint default**: Should the default fingerprint be the model's `redis_key` (e.g., `ClassName:key_value`) or just the key field values? Using `redis_key` is simpler and unique per instance, but the issue description suggests a more abstract "topic fingerprint" (e.g., a topic string that multiple records might share). The Bloom filter is most useful when the fingerprint represents a *topic* or *category* rather than a unique instance key — otherwise you'd just check if the key exists in Redis. Recommendation: default to `redis_key` but document that `fingerprint_fn` should be overridden for topic-level pre-filtering.

2. **FrequencySketch initialization strategy**: Should FrequencySketch use `CMS.INITBYPROB` (error_rate + probability) or `CMS.INITBYDIM` (width + depth)? The `initbyprob` interface is more intuitive (specify desired accuracy), while `initbydim` gives direct control over memory. Leaning toward `initbyprob` as the default with `error_rate=0.001` and `probability=0.01`, matching the issue description's emphasis on configurability.

3. **Test environment**: Should tests be skipped with `pytest.mark.skipif` when RedisBloom is not available, or should CI require Redis Stack? This affects whether the test suite can run on vanilla Redis. Recommendation: skip gracefully with a clear message, and document Redis Stack as a requirement for full test coverage.
