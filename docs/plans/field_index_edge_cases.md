---
status: Ready
type: chore
appetite: Medium
owner: valorengels
created: 2026-03-06
tracking: https://github.com/tomcounsell/popoto/issues/151
---

# Comprehensive Edge Case Tests for Field Index Operations

## Problem

Issue #149 exposed a silent data corruption bug: `KeyField.on_save()` wasn't cleaning up old index entries when a field value changed via `save()`. The fix in PR #150 was straightforward, but it raises an important question: **what other index maintenance edge cases exist across all field types?**

Popoto has 5 field types with `on_save`/`on_delete` hooks that maintain Redis indexes:
- `KeyField` - Set index (`$KeyF:...`)
- `SortedField` - Sorted Set index (`$SortedF:...`)
- `GeoField` - Geo Set index (`$GeoF:...`)
- `Relationship` - Relationship Set index (`$RelationshipF:...`)
- `UniqueKeyField` - KeyField Set + uniqueness constraint

Each has index cleanup responsibilities. The #149 bug pattern (on_save adds to new index, never removes from old) could exist in any of them.

**Current behavior:** No systematic test coverage for index consistency across field value mutations, partial saves, null transitions, and composite key changes.

**Desired outcome:** A comprehensive test suite that verifies correct index behavior for all field types across all mutation scenarios, validating both query-level behavior (`.filter()`) AND raw Redis state (`SMEMBERS`, `ZRANGEBYSCORE`, etc.).

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

This is primarily a test-writing effort. The test cases are well-defined in the issue. Implementation is a matter of translating the hypothesized scenarios into executable tests.

## Prerequisites

No prerequisites -- this work has no external dependencies beyond a running Redis server (already required for all Popoto tests).

## Solution

### Key Elements

- **Test file**: A single new test file `tests/test_field_index_edge_cases.py` containing all edge case tests organized by field type
- **Raw Redis assertions**: Tests verify both ORM-level query results and raw Redis state (SMEMBERS, ZRANGEBYSCORE, GEORADIUS) to catch phantom index entries invisible to the query layer
- **Bug documentation**: Any discovered bugs are filed as separate issues with reproduction steps

### Flow

**Define test models** -> **Test each field type's mutation scenarios** -> **Verify index state at Redis level** -> **File bugs for failures** -> **Report results**

### Technical Approach

- Define minimal test models with the specific field combinations needed for each scenario
- Use `POPOTO_REDIS_DB` directly to inspect raw Redis state after each operation
- Use pytest fixtures (`setup_method`/`teardown_method`) for clean Redis state between tests
- Organize tests into classes by field type for readability
- Each test follows the pattern: create -> mutate -> save -> assert query correctness -> assert Redis index correctness

## Test Cases

### 1. SortedField -- Partition key change leaves orphan in old sorted set (**Fixed in #154**)

When `partition_by` is set, changing the partition key field should remove the entry from the old partition's sorted set and add it to the new one. ZADD is idempotent within the same sorted set but cannot clean up a different sorted set. Fixed by enhancing `on_save()` and `on_delete()` to use `_saved_field_values` for computing the old partition's sorted set key.

### 2. Relationship -- Changing the related object leaves orphan in old relationship set

Changing `player.team` from team_a to team_b should remove the player from team_a's relationship index set and add to team_b's.

### 3. GeoField -- Coordinates update in place

GEOADD is idempotent (member-keyed), so updating coordinates should work correctly. Verify with GEORADIUS queries at old and new coordinates.

### 4. KeyField -- Mutation from non-null to null

Changing a nullable KeyField from a real value to None should clean up the old value's index set.

### 5. KeyField -- Mutation from null to non-null

Creating with null and then setting a value should correctly add to the new index without leaving ghost entries.

### 6. UniqueKeyField -- Mutation cleans up old unique index

Changing a unique field's value should release the old value for reuse by other instances.

### 7. save(update_fields=["status"]) -- Partial save with KeyField

The partial save path only calls `on_save()` for listed fields. Verify index correctness when a KeyField is in `update_fields`.

### 8. save(update_fields=["status"]) -- Partial save index cleanup

Verify the partial save path correctly handles index cleanup via `_saved_field_values`.

### 9. Composite KeyField -- Changing one of two KeyFields

Changing one KeyField in a composite key changes the Redis key itself, triggering the `obsolete_redis_key` path. Verify no double-add or missed cleanup.

### 10. delete_all() -- Index cleanup at scale

After `delete_all()`, verify all index sets are empty and no orphaned Redis keys remain.

### 11. Rapid create-mutate-delete cycle

Create, immediately mutate, immediately delete. Verify all indexes are clean afterward.

### 12. Relationship -- Setting to None (clearing relationship)

Setting a relationship to None should remove the instance from the old related object's index set.

## Rabbit Holes

- **Fixing discovered bugs inline**: Tests should document bugs as separate issues. Do not fix bugs in this PR -- the scope is test coverage only.
- **Testing concurrent/async race conditions**: True race conditions require multi-threaded tests. Stick to single-threaded sequential edge cases.
- **Testing every permutation of field combinations**: Focus on the 12 specific scenarios from the issue rather than exhaustive combinatorial coverage.

## Risks

### Risk 1: Discovered bugs block test completion
**Impact:** Some tests may fail due to actual bugs in field index operations.
**Mitigation:** Mark failing tests with `pytest.mark.xfail(reason="Bug: ...")` and file separate issues. The test file should still be mergeable.

### Risk 2: Model class definitions conflict with existing test models
**Impact:** Redis key collisions between test files.
**Mitigation:** Use unique model class names (e.g., `EdgeMenuItem`, `EdgePlayer`) and thorough cleanup in `setup_method`/`teardown_method`.

## No-Gos (Out of Scope)

- Fixing any bugs discovered by these tests (file separate issues)
- Performance testing or benchmarking of index operations
- Testing pub/sub field behavior
- Testing DataFrameField index behavior
- Async-specific edge cases (covered separately if needed)

## Update System

No update system changes required -- this is purely a test addition.

## Agent Integration

No agent integration required -- this is a test-only change.

## Documentation

### Inline Documentation
- [ ] Clear docstrings on each test class explaining the edge case being tested
- [ ] Comments on non-obvious Redis assertions

No external documentation changes needed -- this is internal test coverage.

## Success Criteria

- [ ] All 12 test cases from issue #151 implemented as runnable tests
- [ ] Tests verify both query-level behavior (`.filter()`) AND raw Redis state
- [ ] Any bugs discovered are filed as separate issues
- [ ] Tests pass (or are marked xfail with bug references)
- [ ] Tests pass (`/do-test`)
- [ ] No regressions in existing test suite

## Team Members

- **Builder (test-writer)**
  - Name: edge-case-tester
  - Role: Implement all 12 edge case test scenarios
  - Agent Type: test-engineer
  - Resume: true

- **Validator (test-runner)**
  - Name: test-validator
  - Role: Run tests and verify results
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Create test file with model definitions and cleanup fixtures
- **Task ID**: build-test-scaffold
- **Depends On**: none
- **Assigned To**: edge-case-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Create `tests/test_field_index_edge_cases.py`
- Define all test model classes needed for edge case scenarios
- Implement `setup_method`/`teardown_method` for Redis cleanup

### 2. Implement SortedField and GeoField edge case tests
- **Task ID**: build-sorted-geo-tests
- **Depends On**: build-test-scaffold
- **Assigned To**: edge-case-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Test case 1: SortedField partition key change
- Test case 3: GeoField coordinate update

### 3. Implement Relationship edge case tests
- **Task ID**: build-relationship-tests
- **Depends On**: build-test-scaffold
- **Assigned To**: edge-case-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Test case 2: Changing related object
- Test case 12: Setting relationship to None

### 4. Implement KeyField and UniqueKeyField edge case tests
- **Task ID**: build-keyfield-tests
- **Depends On**: build-test-scaffold
- **Assigned To**: edge-case-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Test case 4: Non-null to null mutation
- Test case 5: Null to non-null mutation
- Test case 6: UniqueKeyField mutation cleanup
- Test case 9: Composite KeyField change

### 5. Implement partial save and lifecycle tests
- **Task ID**: build-lifecycle-tests
- **Depends On**: build-test-scaffold
- **Assigned To**: edge-case-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Test case 7: Partial save with KeyField in update_fields
- Test case 8: Partial save index cleanup
- Test case 10: delete_all() index cleanup
- Test case 11: Rapid create-mutate-delete

### 6. Run and validate all tests
- **Task ID**: validate-all
- **Depends On**: build-sorted-geo-tests, build-relationship-tests, build-keyfield-tests, build-lifecycle-tests
- **Assigned To**: test-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_field_index_edge_cases.py -v`
- Verify all tests pass or are appropriately marked xfail
- File separate issues for any discovered bugs

## Validation Commands

- `pytest tests/test_field_index_edge_cases.py -v` - Run all edge case tests
- `pytest tests/ -v` - Verify no regressions in existing tests
- `pytest tests/test_field_index_edge_cases.py -v --tb=long` - Detailed failure output for debugging
