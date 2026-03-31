---
status: Ready
type: feature
appetite: Small
owner: Valor
created: 2026-03-31
tracking: https://github.com/tomcounsell/popoto/issues/322
last_comment_id:
---

# Model.check_indexes() Read-Only Health Check

## Problem

There is no way to inspect index health without modifying data. `rebuild_indexes()` destructively deletes and reconstructs all indexes. `Query.keys(clean=True)` removes orphaned entries immediately with no dry-run option. Operators must choose between "fix everything now" or "know nothing" -- there is no inspection step.

**Current behavior:**
```python
# Option A: destructive fix (no preview)
User.rebuild_indexes()

# Option B: also destructive (removes orphans on the spot)
User.query.keys(clean=True)
```

**Desired outcome:**
```python
result = User.check_indexes()
# {'class_set': 3, 'sorted_fields': {'score': 12}, 'key_fields': {'status': 2},
#  'geo_fields': {}, 'composite_indexes': {}, 'total': 17}

if result['total'] > 100:
    alert("Too many orphans, run User.rebuild_indexes()")
```

## Prior Art

- #146: Added `rebuild_indexes()` and `raw_update()` -- established the index iteration pattern
- #151: Comprehensive edge case tests for field index operations
- `rebuild_indexes()` at `base.py:2707` iterates all five index types (class set, sorted fields, key fields, geo fields, composite indexes) -- the same iteration pattern is reused here but read-only

## Data Flow

1. **Entry point**: `Model.check_indexes(batch_size=1000)` classmethod
2. **Class set scan**: SSCAN the `$Class:ModelName` set, pipeline EXISTS on each member key
3. **Key field scan**: For each non-auto key field, SCAN for `$KeyF:ModelName:field_name:*` patterns, SSCAN each set, pipeline EXISTS on members
4. **Sorted field scan**: For each sorted field, SCAN for sorted set keys (handling partitioned fields), ZSCAN each sorted set, pipeline EXISTS on members
5. **Geo field scan**: For each geo field, ZSCAN the geo sorted set, pipeline EXISTS on members
6. **Composite index scan**: For each composite index, HSCAN the hash, pipeline EXISTS on values (which are instance keys)
7. **Return**: Structured dict with per-type orphan counts

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

No prerequisites -- uses only existing Redis connection, `_meta` field metadata, and SCAN/pipeline infrastructure already in the codebase.

## Solution

### Key Elements

- **`Model.check_indexes()`**: New classmethod on the base Model class, placed after `rebuild_indexes()` (~line 2844)
- **`Model.async_check_indexes()`**: Async counterpart using `asyncio.to_thread()`
- **Batched pipeline EXISTS**: For each index type, collect member keys via SCAN variants, then batch EXISTS checks in pipelines of `batch_size`
- **Structured return dict**: `{'class_set': int, 'key_fields': {name: int}, 'sorted_fields': {name: int}, 'geo_fields': {name: int}, 'composite_indexes': {key: int}, 'total': int}`

### Flow

**User code** --> `Model.check_indexes()` --> **SSCAN/ZSCAN/HSCAN per index** --> **Pipelined EXISTS** --> **Count non-existent** --> **Return structured dict**

### Technical Approach

**Class set** (`$Class:ModelName` -- a Redis Set):
- SSCAN to iterate all members
- Each member is an instance redis key (e.g., `User:alice@example.com`)
- Pipeline EXISTS on each key; count those returning 0

**Key field sets** (`$KeyF:ModelName:field_name:value` -- Redis Sets):
- SCAN for keys matching `$KeyF:ModelName:field_name:*` pattern (same as `rebuild_indexes()` line 2764)
- Skip auto fields (they do not maintain index sets)
- For each matching set key, SSCAN to get members (instance redis keys)
- Pipeline EXISTS; count non-existent

**Sorted fields** (Redis Sorted Sets):
- For each sorted field, get the base key via `field.get_special_use_field_db_key()`
- SCAN for keys matching `base_key*` pattern (handles partitioned fields, same as line 2753)
- ZSCAN each sorted set; members are instance redis keys
- Pipeline EXISTS; count non-existent

**Geo fields** (Redis Geo Sorted Sets):
- For each geo field, get the geo key via `GeoField.get_geo_db_key()`
- ZSCAN the geo sorted set; members are instance redis keys
- Pipeline EXISTS; count non-existent

**Composite indexes** (`$Index:ModelName:field1:field2` -- Redis Hashes):
- For each index in `_meta.indexes`, get the index key via `_meta.get_index_key()`
- HSCAN the hash; values are instance redis keys
- Pipeline EXISTS; count non-existent

### Implementation Detail: Batched EXISTS

```python
def _count_orphans(keys_to_check: list, batch_size: int) -> int:
    """Pipeline EXISTS in batches, return count of non-existent keys."""
    orphan_count = 0
    for i in range(0, len(keys_to_check), batch_size):
        batch = keys_to_check[i:i + batch_size]
        pipe = POPOTO_REDIS_DB.pipeline()
        for key in batch:
            pipe.exists(key)
        results = pipe.execute()
        orphan_count += sum(1 for exists in results if not exists)
    return orphan_count
```

This uses EXISTS (O(1) per key) rather than HGETALL (O(N) per hash), keeping the check lightweight.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] No custom exception handling -- Redis connection errors propagate naturally
- [ ] If a model has no instances and no indexes, returns all-zero dict (not an error)

### Empty/Invalid Input Handling
- [ ] Model with no sorted/key/geo fields returns zero for those categories
- [ ] Model with no instances returns zero orphans across all index types
- [ ] Orphaned class set entries (key in set but hash deleted) are correctly counted

### Error State Rendering
- Not applicable -- returns a dict, no user-visible rendering

## Test Impact

No existing tests affected -- this is a greenfield feature adding new classmethods with no modifications to existing methods or behavior. The `test_field_index_edge_cases.py` and `test_migrations.py` files are unrelated to the new check functionality.

## Rabbit Holes

- **Returning the actual orphan keys**: Returning keys instead of counts would be useful for debugging but changes the API surface and memory profile. Start with counts only; a future enhancement can add a `verbose=True` option.
- **Integrating with rebuild_indexes()**: Tempting to make rebuild call check first, but they serve different purposes. Keep them independent.
- **Checking for reverse orphans** (instances in Redis but missing from indexes): This is the inverse problem and would require scanning all instance keys and verifying index membership. Much more expensive. Out of scope -- `rebuild_indexes()` already handles this case.
- **Adding a fix parameter**: `check_indexes(fix=True)` is basically `clean_indexes()`. Keep check read-only; clean is a separate future method.

## Risks

### Risk 1: Large index scan performance
**Impact:** For models with millions of index entries, full scan could take significant time
**Mitigation:** batch_size parameter controls pipeline batch size. SCAN-based iteration is already production-safe in the codebase (used by rebuild_indexes). Document that this is an administrative operation, not meant for hot paths.

### Risk 2: Key format assumptions
**Impact:** If index member format varies between index types, EXISTS checks could give wrong results
**Mitigation:** Follow exact same key extraction patterns as rebuild_indexes(). Sorted set and geo set members are instance redis keys. Key field set members are instance redis keys. Composite index hash values are instance redis keys. All checked with EXISTS.

## Race Conditions

Minimal race condition risk. A concurrent delete could cause a key to disappear between SCAN and EXISTS, producing a false orphan count. This is acceptable for a diagnostic tool -- the count is a point-in-time snapshot, not a guarantee. Document this in the docstring.

## No-Gos (Out of Scope)

- No writes to Redis (strictly read-only)
- No returning of orphan key lists (counts only)
- No reverse orphan detection (instances missing from indexes)
- No automatic fix/clean behavior
- No Redis module dependencies (must work on both Redis and Valkey)

## Update System

No update system changes required -- this is an upstream library feature in popoto.

## Agent Integration

No agent integration required -- this is a library method in the popoto package.

## Documentation

### Inline Documentation
- [ ] Comprehensive docstring on `check_indexes()` with Args, Returns, Example sections (matching `rebuild_indexes()` docstring style at line 2707)
- [ ] Comprehensive docstring on `async_check_indexes()` matching async conventions in the file

### External Documentation Site
- [ ] No external docs site changes needed -- popoto does not currently have a docs site

## Success Criteria

- [ ] `Model.check_indexes()` classmethod returns dict with orphan counts per index type
- [ ] Method makes zero writes to Redis (read-only)
- [ ] Uses SCAN-based iteration (SSCAN, ZSCAN, HSCAN) for member discovery
- [ ] Uses pipelined EXISTS for efficient batch existence checks
- [ ] `Model.async_check_indexes()` async counterpart exists and works
- [ ] Tests verify correct orphan detection across all five index types
- [ ] Tests verify no writes occur (read-only guarantee)
- [ ] All existing tests continue to pass

## Team Orchestration

### Team Members

- **Builder (check-indexes)**
  - Name: index-checker-builder
  - Role: Implement check_indexes and async_check_indexes methods
  - Agent Type: builder
  - Resume: true

- **Validator (check-indexes)**
  - Name: index-checker-validator
  - Role: Verify implementation and run tests
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Implement check_indexes() on Model class
- **Task ID**: build-check-indexes
- **Depends On**: none
- **Validates**: tests/test_check_indexes.py (create)
- **Assigned To**: index-checker-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `check_indexes(cls, batch_size: int = 1000) -> dict` classmethod to Model after `rebuild_indexes()` (~line 2844)
- Implement helper function `_count_orphans_in_set()` for SSCAN + pipelined EXISTS
- Implement helper function `_count_orphans_in_sorted_set()` for ZSCAN + pipelined EXISTS
- Implement helper function `_count_orphans_in_hash()` for HSCAN + pipelined EXISTS
- Check class set: SSCAN `$Class:ModelName`, EXISTS each member
- Check key field sets: SCAN for `$KeyF:ModelName:field_name:*`, SSCAN each, EXISTS members (skip auto fields)
- Check sorted field sets: SCAN for sorted set keys (handle partitioned), ZSCAN each, EXISTS members
- Check geo fields: ZSCAN geo sorted set, EXISTS members
- Check composite indexes: HSCAN hash, EXISTS values
- Return structured dict: `{'class_set': N, 'key_fields': {...}, 'sorted_fields': {...}, 'geo_fields': {...}, 'composite_indexes': {...}, 'total': N}`

### 2. Implement async_check_indexes() on Model class
- **Task ID**: build-async-check-indexes
- **Depends On**: build-check-indexes
- **Validates**: tests/test_check_indexes.py
- **Assigned To**: index-checker-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `async_check_indexes(cls, batch_size: int = 1000) -> dict` async classmethod after `check_indexes()`
- Use `asyncio.to_thread(cls.check_indexes, batch_size=batch_size)` pattern (matching `async_rebuild_indexes()` at line 2846)

### 3. Write tests
- **Task ID**: build-tests
- **Depends On**: build-async-check-indexes
- **Validates**: tests/test_check_indexes.py
- **Assigned To**: index-checker-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_check_indexes.py` with:
  - Test model with no orphans returns all-zero counts
  - Test class set orphan detection (delete instance hash, verify class_set count increments)
  - Test key field orphan detection (delete instance hash, verify key_fields count)
  - Test sorted field orphan detection (delete instance hash, verify sorted_fields count)
  - Test geo field orphan detection (delete instance hash, verify geo_fields count)
  - Test composite index orphan detection (delete instance hash, verify composite_indexes count)
  - Test read-only guarantee (snapshot Redis state before and after, verify no changes)
  - Test model with no indexes returns empty sub-dicts
  - Test async_check_indexes returns same result as sync version
  - Test total field is sum of all orphan counts

### 4. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-tests
- **Assigned To**: index-checker-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify lint and format pass
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_check_indexes.py -x -q` | exit code 0 |
| Full suite | `pytest tests/ -x -q --timeout=60` | exit code 0 |
| Lint clean | `python -m ruff check src/popoto/models/base.py` | exit code 0 |
| Format clean | `python -m ruff format --check src/popoto/models/base.py` | exit code 0 |
| Method exists | `python -c "from popoto import Model; assert hasattr(Model, 'check_indexes')"` | exit code 0 |
| Async exists | `python -c "from popoto import Model; assert hasattr(Model, 'async_check_indexes')"` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

No open questions -- the implementation follows well-established patterns from `rebuild_indexes()` and the index type structures are well-understood from the field mixin source code.
