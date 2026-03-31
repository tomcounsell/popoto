---
status: Ready
type: feature
appetite: Small
owner: Valor
created: 2026-03-31
tracking: https://github.com/tomcounsell/popoto/issues/320
last_comment_id:
---

# Model.clean_indexes() Production-Safe Orphan Cleanup

## Problem

Popoto accumulates orphaned index entries when instances are deleted inconsistently (crashes, manual `DEL` commands, partial pipeline failures). The only cleanup mechanism is `Query.keys(clean=True)`, which has critical limitations that prevent production use.

**Current behavior:**
```python
# Uses Redis KEYS command (blocks server on large datasets)
# Only cleans class set and KeyField/Relationship indexes
# Does NOT clean SortedField, GeoField, or composite indexes
# Warning discourages production use
User.query.keys(clean=True)

# Alternative: destructive full rebuild (no surgical cleanup)
User.rebuild_indexes()
```

**Desired outcome:**
```python
# Production-safe orphan removal using SCAN
removed = User.clean_indexes()
print(f"Removed {removed} orphaned index entries")

# Async counterpart for event loop contexts
removed = await User.async_clean_indexes()

# Typical workflow: check first, then clean
result = User.check_indexes()
if result['total'] > 0:
    removed = User.clean_indexes()
```

## Prior Art

- **Issue #322 / PR #332**: Added `Model.check_indexes()` read-only health check (merged 2026-03-31). This is the READ counterpart; `clean_indexes()` is the WRITE counterpart that removes what `check_indexes()` detects.
- **Issue #146 / PR #158**: Added `rebuild_indexes()` and comprehensive edge case tests for field index operations. Established the batched-pipeline pattern and index iteration patterns reused here.
- **Issue #114**: Added `Model.delete_all()` classmethod for bulk operations. Established the pattern for bulk classmethods on Model.

## Data Flow

1. **Entry point**: `Model.clean_indexes(batch_size=1000)` classmethod call
2. **Class set scan**: SSCAN `$Class:ModelName` members, pipeline EXISTS to find orphans, pipeline SREM to remove them
3. **Key field scan**: For each non-auto key field, SCAN for `$KeyF:ModelName:field_name:*` sets, SSCAN each set, pipeline EXISTS + SREM for orphans
4. **Sorted field scan**: For each sorted field, SCAN for sorted set keys (handles partitioned fields), ZSCAN each, pipeline EXISTS + ZREM for orphans
5. **Geo field scan**: For each geo field, ZSCAN the geo sorted set, pipeline EXISTS + ZREM for orphans
6. **Composite index scan**: For each composite index, HSCAN the hash, pipeline EXISTS + HDEL for orphans
7. **Return**: Integer count of total orphaned entries removed

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

- **`Model.clean_indexes(batch_size=1000)`**: New classmethod that iterates all 5 index types, identifies orphaned entries via pipelined EXISTS, and removes them via pipelined SREM/ZREM/HDEL
- **`Model.async_clean_indexes(batch_size=1000)`**: Async counterpart using `asyncio.to_thread()`, matching `async_rebuild_indexes` pattern
- **Deprecation of `Query.keys(clean=True)`**: Update warning message to point users to `clean_indexes()`; keep the method functional for backward compatibility
- **Leverage `check_indexes()` helper patterns**: Reuse the same SSCAN/ZSCAN/HSCAN iteration patterns from `check_indexes()` but add write operations (SREM, ZREM, HDEL) for detected orphans

### Flow

**User code** --> `Model.clean_indexes()` --> **SSCAN/ZSCAN/HSCAN per index type** --> **Pipelined EXISTS** --> **Identify orphans** --> **Pipelined SREM/ZREM/HDEL** --> **Return removal count**

### Technical Approach

**Placement**: After `check_indexes()` in `base.py`, before `async_check_indexes()`. The async methods (`async_clean_indexes`, `async_check_indexes`, `async_rebuild_indexes`) are grouped together after the sync methods.

**Pattern**: Each index type follows the same structure:
1. Collect member keys via SCAN variant (SSCAN for sets, ZSCAN for sorted sets, HSCAN for hashes)
2. Batch EXISTS checks in pipeline to find non-existent instance keys
3. Batch removal commands (SREM/ZREM/HDEL) in pipeline for orphaned entries
4. Accumulate count

**Class set** (`$Class:ModelName` -- Redis Set):
- SSCAN to iterate all members
- Pipeline EXISTS on each member key
- Pipeline SREM for members whose instance hash no longer exists

**Key field sets** (`$KeyF:ModelName:field_name:value` -- Redis Sets):
- SCAN for keys matching `$KeyF:ModelName:field_name:*` (same pattern as `rebuild_indexes()` line 2764)
- Skip auto fields (they do not maintain index sets)
- For each matching set key, SSCAN members, pipeline EXISTS + SREM

**Sorted fields** (Redis Sorted Sets):
- For each sorted field, get base key via `field.get_special_use_field_db_key()`
- SCAN for keys matching `base_key*` (handles partitioned fields, same as line 2753)
- ZSCAN each sorted set; pipeline EXISTS + ZREM

**Geo fields** (Redis Geo Sorted Sets):
- For each geo field, get geo key via `GeoField.get_geo_db_key()`
- ZSCAN the geo sorted set; pipeline EXISTS + ZREM

**Composite indexes** (`$Index:ModelName:field1:field2` -- Redis Hashes):
- For each index in `_meta.indexes`, get index key via `_meta.get_index_key()`
- HSCAN the hash; values are instance redis keys, keys are index hashes
- Pipeline EXISTS on values; pipeline HDEL for orphaned hash entries

**Helper functions** (inner functions, mirroring `check_indexes()` structure):
- `_collect_orphans_from_set(set_key)`: SSCAN + batched EXISTS, returns list of orphan keys
- `_collect_orphans_from_sorted_set(zset_key)`: ZSCAN + batched EXISTS, returns list of orphan keys
- `_collect_orphans_from_hash(hash_key)`: HSCAN + batched EXISTS, returns list of (hash_field, value) orphan pairs

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] No custom exception handling -- Redis connection errors propagate naturally (same as `check_indexes()` and `rebuild_indexes()`)
- [ ] If pipeline execution partially fails, Redis raises the error (no silent swallowing)

### Empty/Invalid Input Handling
- [ ] Model with no instances and no indexes: returns 0 (not an error)
- [ ] Model with no sorted/key/geo fields: skips those index types, returns 0 for them
- [ ] Model with healthy indexes (no orphans): returns 0 with no writes performed

### Error State Rendering
- Not applicable -- returns an integer count, no user-visible rendering

## Test Impact

- [ ] `tests/test_check_indexes.py` -- no changes needed. `clean_indexes()` is a new method and does not modify `check_indexes()` behavior. The existing test models (`CheckUser`, `CheckGeoPlace`, `CheckComposite`, `CheckMinimal`, `CheckPartitioned`) can be reused in the new test file.

No other existing tests affected -- this is an additive feature adding new classmethods with no modifications to existing methods. The `Query.keys(clean=True)` warning text change is backward-compatible (method still works, just emits a different warning string).

## Rabbit Holes

- **Returning a detailed breakdown instead of a count**: Tempting to return a dict like `check_indexes()`, but the issue specifies "returns integer count of orphaned entries removed." A detailed breakdown can be a future enhancement. Users who want the breakdown should call `check_indexes()` first.
- **Calling check_indexes() internally**: Could call `check_indexes()` to find orphans then remove them, but this would double the EXISTS calls. Better to combine detection and removal in a single pass for efficiency.
- **Cleaning empty index keys**: After removing all orphaned members from a key field set, the set itself might be empty. Tempting to delete the empty set key, but this is a separate concern and could race with concurrent writes. Out of scope.
- **Adding a dry_run parameter**: `clean_indexes(dry_run=True)` is functionally `check_indexes()`. Keep them as separate methods with clear semantics.

## Risks

### Risk 1: Concurrent writes during cleanup
**Impact:** A concurrent `save()` could add a new member to an index set between the EXISTS check and the SREM. If the instance key is created after EXISTS returns 0 but before SREM executes, we could remove a valid entry.
**Mitigation:** This is inherent to any non-transactional cleanup. The window is extremely small (microseconds between pipeline batches). Document that `clean_indexes()` should be run during low-traffic periods, similar to `rebuild_indexes()`. If a valid entry is accidentally removed, the next `save()` on that instance will re-add it to indexes.

### Risk 2: Large dataset memory usage
**Impact:** Models with millions of index entries could cause high memory usage during scan.
**Mitigation:** `batch_size` parameter controls pipeline batch size. SCAN-based iteration processes entries incrementally rather than loading all at once. Follow the same pattern that `rebuild_indexes()` uses successfully.

## Race Conditions

### Race 1: Concurrent instance creation during orphan removal
**Location:** `clean_indexes()` inner loop (EXISTS + SREM/ZREM/HDEL sequence)
**Trigger:** A new instance is saved between the EXISTS check (returns 0 because key does not exist yet) and the removal command
**Data prerequisite:** Instance hash must exist before its index entries are considered valid
**State prerequisite:** Index entries reference an existing instance hash
**Mitigation:** The time window between pipeline EXISTS and pipeline SREM is negligible (both are in the same pipeline batch). Even if an entry is incorrectly removed, the next `save()` call on that instance re-adds it to all indexes. Document this as a known limitation for concurrent workloads.

## No-Gos (Out of Scope)

- No Redis module dependencies (must work on both Redis and Valkey)
- No returning detailed breakdown (integer count only; use `check_indexes()` for breakdown)
- No cleaning of empty index keys after orphan removal
- No dry-run mode (that is what `check_indexes()` is for)
- No transactional guarantees (WATCH/MULTI) -- would severely impact performance
- No reverse orphan detection (instances missing from indexes -- that is what `rebuild_indexes()` handles)

## Update System

No update system changes required -- this is an upstream library feature in popoto.

## Agent Integration

No agent integration required -- this is a library method in the popoto package.

## Documentation

### External Documentation Site
- [ ] Add `clean_indexes()` and `async_clean_indexes()` to the Index Maintenance section in `docs/api-reference.md` (alongside existing `check_indexes()` and `rebuild_indexes()` entries)
- [ ] Update `docs/async.md` index maintenance row to include `async_clean_indexes`

### Inline Documentation
- [ ] Comprehensive docstring on `clean_indexes()` with Args, Returns, Example sections (matching `rebuild_indexes()` and `check_indexes()` docstring style)
- [ ] Comprehensive docstring on `async_clean_indexes()` matching async conventions in the file
- [ ] Update `Query.keys(clean=True)` warning to reference `clean_indexes()` as the preferred alternative

## Success Criteria

- [ ] `Model.clean_indexes(batch_size=1000)` classmethod exists and removes orphaned entries from all 5 index types
- [ ] Uses SCAN-based iteration (SSCAN, ZSCAN, HSCAN) -- not KEYS command
- [ ] Returns integer count of orphaned entries removed
- [ ] `Model.async_clean_indexes()` async counterpart exists
- [ ] `Query.keys(clean=True)` warning updated to reference `clean_indexes()`
- [ ] Tests verify orphan detection and removal for each index type
- [ ] Tests verify post-cleanup state is clean (re-running `check_indexes()` returns 0 orphans)
- [ ] All existing tests continue to pass
- [ ] Documentation updated in `docs/api-reference.md` and `docs/async.md`

## Team Orchestration

### Team Members

- **Builder (clean-indexes)**
  - Name: index-cleaner-builder
  - Role: Implement clean_indexes, async_clean_indexes, and deprecation warning update
  - Agent Type: builder
  - Resume: true

- **Validator (clean-indexes)**
  - Name: index-cleaner-validator
  - Role: Verify implementation and run tests
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Implement clean_indexes() on Model class
- **Task ID**: build-clean-indexes
- **Depends On**: none
- **Validates**: tests/test_clean_indexes.py (create)
- **Assigned To**: index-cleaner-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `clean_indexes(cls, batch_size: int = 1000) -> int` classmethod to Model after `check_indexes()` (after line 3001)
- Implement inner helper `_collect_orphans(keys_to_check)`: pipeline EXISTS in batches, return list of non-existent keys
- Implement inner helper `_scan_set_members(set_key)`: SSCAN all members of a Redis set
- Implement inner helper `_scan_sorted_set_members(zset_key)`: ZSCAN all members of a sorted set
- Implement inner helper `_scan_hash_entries(hash_key)`: HSCAN all key-value pairs of a hash
- Clean class set: SSCAN `$Class:ModelName`, find orphans, pipeline SREM
- Clean key field sets: SCAN for `$KeyF:ModelName:field_name:*`, SSCAN each, find orphans, pipeline SREM (skip auto fields)
- Clean sorted field sets: SCAN for sorted set keys (handle partitioned), ZSCAN each, find orphans, pipeline ZREM
- Clean geo fields: ZSCAN geo sorted set, find orphans, pipeline ZREM
- Clean composite indexes: HSCAN hash, find orphans by checking values (instance keys), pipeline HDEL using the hash field keys
- Return total count of removed entries

### 2. Implement async_clean_indexes() on Model class
- **Task ID**: build-async-clean-indexes
- **Depends On**: build-clean-indexes
- **Validates**: tests/test_clean_indexes.py
- **Assigned To**: index-cleaner-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `async_clean_indexes(cls, batch_size: int = 1000) -> int` async classmethod after `async_check_indexes()`
- Use `asyncio.to_thread(cls.clean_indexes, batch_size=batch_size)` pattern (matching `async_rebuild_indexes()`)

### 3. Update Query.keys(clean=True) deprecation warning
- **Task ID**: build-deprecation-warning
- **Depends On**: build-clean-indexes
- **Validates**: tests/test_clean_indexes.py
- **Assigned To**: index-cleaner-builder
- **Agent Type**: builder
- **Parallel**: true
- In `query.py` line 1677, update the warning message from "{clean} is for debugging purposes only. Not for use in production environment" to "Query.keys(clean=True) is deprecated. Use Model.clean_indexes() for production-safe orphan cleanup."
- Keep the method functional -- only change the warning text

### 4. Write tests
- **Task ID**: build-tests
- **Depends On**: build-clean-indexes, build-async-clean-indexes, build-deprecation-warning
- **Validates**: tests/test_clean_indexes.py
- **Assigned To**: index-cleaner-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_clean_indexes.py` with test models reusing patterns from `test_check_indexes.py`
- Test: no orphans returns 0, no writes performed
- Test: class set orphan removal (delete instance hash, run clean_indexes, verify SREM'd)
- Test: key field orphan removal
- Test: sorted field orphan removal
- Test: geo field orphan removal
- Test: composite index orphan removal
- Test: partitioned sorted field orphan removal
- Test: multiple orphans counted correctly
- Test: post-cleanup `check_indexes()` returns total=0
- Test: clean_indexes + check_indexes round-trip (create orphans, clean, verify clean)
- Test: async_clean_indexes returns same count as sync
- Test: batch_size parameter accepted and produces same results
- Test: deprecation warning text updated (capture warning from `Query.keys(clean=True)`)

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-tests
- **Assigned To**: index-cleaner-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify lint and format pass
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_clean_indexes.py -x -q` | exit code 0 |
| Full suite | `pytest tests/ -x -q --timeout=60` | exit code 0 |
| Lint clean | `python -m ruff check src/popoto/models/base.py src/popoto/models/query.py` | exit code 0 |
| Format clean | `python -m ruff format --check src/popoto/models/base.py src/popoto/models/query.py` | exit code 0 |
| Method exists | `python -c "from popoto import Model; assert hasattr(Model, 'clean_indexes')"` | exit code 0 |
| Async exists | `python -c "from popoto import Model; assert hasattr(Model, 'async_clean_indexes')"` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

No open questions -- the implementation follows well-established patterns from `check_indexes()` and `rebuild_indexes()`, the issue is well-specified with clear acceptance criteria, and all 5 index types are understood from the field mixin source code and the recently merged `check_indexes()` implementation.
