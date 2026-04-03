---
status: Ready
type: chore
appetite: Small
owner: Valor
created: 2026-04-03
tracking: https://github.com/tomcounsell/popoto/issues/337
last_comment_id:
---

# Cross-Feature Integration Tests for v1.4.4 APIs

## Problem

The 8 PRs merged on 2026-03-31 (get_many, positional get, check_indexes, clean_indexes, partition_by, companion hash keys) each have thorough isolated tests (97 new tests total), but no tests exercise these features together.

**Current behavior:**
Each feature is tested in isolation. `test_get_many.py` tests bulk fetch, `test_check_indexes.py` tests orphan detection, `test_clean_indexes.py` tests orphan removal, `test_partitioned_confidence.py` tests partition_by. No test creates a realistic workflow that combines them.

**Desired outcome:**
A new `tests/test_integration_v144.py` file with three scenarios that exercise feature combinations reflecting real-world usage patterns, plus an async variant for at least one scenario.

## Prior Art

- **PR #332**: Add `Model.check_indexes()` -- established orphan detection across five index types
- **PR #334**: Add `Model.clean_indexes()` -- established orphan removal with check_indexes round-trip verification
- **PR #328**: Add partition_by to ConfidenceField -- introduced partitioned companion hash keys
- **PR #336**: Expose public API for companion hash key methods -- made hash key construction public
- **PR #330**: Add `get_many()` bulk retrieval -- established mget-based bulk fetch with order preservation

The isolated test files that already cover individual features:
- `tests/test_get_many.py` -- 11 tests for sync/async get_many
- `tests/test_check_indexes.py` -- 15 tests for orphan detection and read-only guarantee
- `tests/test_clean_indexes.py` -- 17 tests for orphan removal and round-trip verification
- `tests/test_partitioned_confidence.py` -- 22 tests for partition_by lifecycle
- `tests/test_meta_ttl.py` -- 7 tests for TTL expiry behavior

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

No prerequisites -- all features are already merged and their isolated tests pass. Requires Redis on localhost:6379 (standard test environment).

## Solution

### Key Elements

- **Scenario 1: get_many + check_indexes + clean_indexes round-trip** -- Tests the full diagnostic-and-repair workflow on bulk-fetched data
- **Scenario 2: partition_by + clean_indexes** -- Tests that index health tools work correctly with partitioned companion hashes
- **Scenario 3: TTL expiry + check_indexes** -- Tests that expired keys are correctly detected as orphans by the index health checker

### Technical Approach

All three scenarios follow a common pattern: create model instances via the ORM, introduce index corruption (by directly deleting Redis keys to bypass ORM cleanup), then verify the health-check and cleanup APIs detect and resolve the issue.

#### Scenario 1: get_many + check_indexes + clean_indexes

1. Create 5+ instances of a model with KeyField and SortedField
2. Bulk-fetch all with `get_many()` -- verify all returned correctly
3. Delete 2 instance hashes directly via `POPOTO_REDIS_DB.delete()` (bypassing ORM)
4. Call `get_many()` again -- verify deleted instances return as `None` at correct positions
5. Call `check_indexes()` -- verify it reports orphans (class_set >= 2, sorted_fields >= 2)
6. Call `clean_indexes()` -- verify it returns count > 0
7. Call `check_indexes()` again -- verify total == 0
8. Call `get_many()` with `skip_none=True` -- verify only surviving instances returned
9. Verify surviving instances have correct field values

Model definition:
```python
class IntegrationItem(popoto.Model):
    name = popoto.KeyField()
    score = popoto.SortedField(type=float)
    label = popoto.Field(type=str, null=True)
```

#### Scenario 2: partition_by + clean_indexes

1. Create instances of a ConfidenceField model with `partition_by="category"`
2. Update confidence values to ensure companion hash entries exist
3. Delete one instance hash directly via `POPOTO_REDIS_DB.delete()`
4. Call `check_indexes()` -- verify orphans detected
5. Call `clean_indexes()` -- verify orphans removed
6. Call `check_indexes()` -- verify total == 0
7. Verify surviving instances still have correct confidence data

Model definition:
```python
class IntegrationConfidence(popoto.Model):
    name = popoto.KeyField()
    category = popoto.KeyField()
    certainty = ConfidenceField(initial_confidence=0.5, partition_by="category")
```

Note: `check_indexes()` and `clean_indexes()` scan sorted fields and class sets. The ConfidenceField companion hashes use a separate key structure (`$Confidence:...`). The integration test should verify that after deleting an instance, the class set and any sorted/key field indexes are cleaned, and that the surviving instances' confidence data remains intact.

#### Scenario 3: TTL expiry + check_indexes

1. Define a model with `Meta.ttl = 2` (2-second expiry) and a SortedField
2. Create an instance
3. Verify `check_indexes()` reports 0 orphans immediately
4. Wait for TTL expiry (2.5 seconds)
5. Verify the instance key no longer exists (`query.get()` returns None)
6. Call `check_indexes()` -- verify it reports orphans (the sorted field and class set entries survive TTL because they are separate Redis keys)
7. Call `clean_indexes()` -- verify orphans removed
8. Call `check_indexes()` -- verify total == 0

Model definition:
```python
class IntegrationTTL(popoto.Model):
    name = popoto.KeyField()
    score = popoto.SortedField(type=float)

    class Meta:
        ttl = 2
```

#### Async variant

Scenario 1 repeated using `async_get_many`, `async_check_indexes`, and `async_clean_indexes`.

## Failure Path Test Strategy

### Exception Handling Coverage
- No exception handlers in scope -- these are integration tests exercising existing methods

### Empty/Invalid Input Handling
- Scenario 1 tests `get_many()` with keys pointing to deleted instances (returns None)
- Scenario 1 tests `get_many(skip_none=True)` filtering behavior after cleanup

### Error State Rendering
- Not applicable -- library methods return structured data, no user-visible rendering

## Test Impact

No existing tests affected -- this is a greenfield test file (`tests/test_integration_v144.py`) that does not modify any existing test files or production code.

## Rabbit Holes

- **Testing companion hash cleanup in clean_indexes**: The current `clean_indexes()` implementation cleans class sets, key field indexes, sorted field indexes, geo indexes, and composite indexes. It does NOT clean companion hash entries (ConfidenceField data hashes). Do not try to test companion hash cleanup -- that would be a new feature, not an integration test.
- **Testing all possible feature combinations**: There are many possible pairwise combinations of v1.4.4 features. Focus only on the three scenarios from the issue -- they cover the most realistic workflows.
- **Performance benchmarking**: Do not add timing assertions or performance tests. This is about correctness only.

## Risks

### Risk 1: TTL test timing sensitivity
**Impact:** Scenario 3 uses `time.sleep(2.5)` to wait for TTL expiry. On slow CI runners, this could be flaky.
**Mitigation:** Use a generous buffer (2.5s for a 2s TTL). If flaky, the TTL can be increased to 3s with a 4s wait.

### Risk 2: Test isolation between scenarios
**Impact:** Shared Redis DB 15 means scenarios could leak state.
**Mitigation:** Each test class uses unique model class names. The pytest plugin flushes DB between tests. Use `delete_all()` in setup/teardown as a belt-and-suspenders approach.

## Race Conditions

No race conditions identified -- all tests are synchronous (except the async variant which uses `asyncio.run()`) and single-threaded. The TTL scenario has inherent timing sensitivity but no concurrency.

## No-Gos (Out of Scope)

- No production code changes -- test-only issue
- No companion hash orphan cleanup (not supported by clean_indexes yet)
- No geo field or composite index integration scenarios (covered adequately in isolated tests)
- No performance or load testing

## Update System

No update system changes required -- this is a test-only change in the popoto library.

## Agent Integration

No agent integration required -- this is a library test file.

## Documentation

No documentation changes needed -- integration tests are self-documenting via their docstrings and do not require external documentation updates.

## Success Criteria

- [ ] New test file `tests/test_integration_v144.py` created
- [ ] Scenario 1 (get_many + check_indexes + clean_indexes) passes
- [ ] Scenario 2 (partition_by + clean_indexes) passes
- [ ] Scenario 3 (TTL expiry + check_indexes) passes
- [ ] At least one async variant passes
- [ ] All existing tests continue to pass
- [ ] Tests pass (`/do-test`)

## Team Orchestration

### Team Members

- **Builder (integration-tests)**
  - Name: integration-test-builder
  - Role: Implement all three test scenarios in a single test file
  - Agent Type: test-engineer
  - Resume: true

- **Validator (integration-tests)**
  - Name: integration-test-validator
  - Role: Run the test file and verify all scenarios pass
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Create integration test file
- **Task ID**: build-integration-tests
- **Depends On**: none
- **Validates**: tests/test_integration_v144.py (create)
- **Assigned To**: integration-test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Create `tests/test_integration_v144.py` with model definitions for all three scenarios
- Implement `TestGetManyCheckClean` class with Scenario 1 tests
- Implement `TestPartitionByCleanIndexes` class with Scenario 2 tests
- Implement `TestTTLExpiryCheckIndexes` class with Scenario 3 tests
- Implement `TestAsyncIntegration` class with async variant of Scenario 1
- Use `import popoto` (not `from src import popoto`) matching the style of `test_check_indexes.py`
- Use `POPOTO_REDIS_DB` for direct Redis manipulation (deleting instance hashes)
- Import `ConfidenceField` from `popoto.fields.confidence_field` for Scenario 2

### 2. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-integration-tests
- **Assigned To**: integration-test-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_integration_v144.py -x -v` and verify all tests pass
- Run `pytest tests/ -x -q --timeout=60` to verify no existing tests break
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Integration tests pass | `pytest tests/test_integration_v144.py -x -v` | exit code 0 |
| Full suite | `pytest tests/ -x -q --timeout=60` | exit code 0 |
| File exists | `test -f tests/test_integration_v144.py` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

No open questions -- the three scenarios are well-defined in the issue, the existing test files provide clear API usage patterns to follow, and this is a test-only change with no architectural decisions required.
