---
status: Planning
type: bug
appetite: Small
owner: Valor Engels
created: 2026-03-10
tracking: https://github.com/tomcounsell/popoto/issues/155
---

# Fix Relationship.on_save() Index Cleanup on Value Change

## Problem

When a `Relationship` field value changes (e.g., `player.team = team_b`), `on_save()` adds the instance to the new related object's index set but never removes it from the old one. This creates ghost entries in relationship index sets, causing stale query results.

**Current behavior:**
- `player.team = team_b; player.save()` adds player to team_b's index set
- Player remains in team_a's index set (ghost entry)
- `Player.query.filter(team=team_a)` incorrectly returns the player
- Setting a relationship to `None` also fails to clean up the old index set

**Desired outcome:**
- On relationship change, old index set entry is removed before new one is added
- `Player.query.filter(team=team_a)` returns empty after reassignment
- Setting relationship to `None` removes from the old related object's index set
- Edge case tests 2 and 12 in `tests/test_field_index_edge_cases.py` pass (currently `xfail`)

## Prior Art

- **PR #150**: "Fix KeyField index corruption on value mutation" -- Fixed the identical bug in `KeyFieldMixin.on_save()` by checking `_saved_field_values` for old values and calling `SREM` before `SADD`. Merged 2026-03-06. This is the exact pattern to replicate.
- **Issue #151 / PR #158**: "Comprehensive edge case tests for field index operations" -- Created tests that discovered this bug (test cases 2 and 12 marked as `xfail`). Merged 2026-03-07.
- **Issue #41 / PR #44**: "Relationship field: Edge case with lazy-loaded relationships during on_delete()" -- Fixed lazy-loaded string value handling in `on_delete()`. Related but different bug surface. Closed 2026-01-27.

## Data Flow

1. **Entry point**: User changes a Relationship field value (`player.team = team_b`) and calls `player.save()`
2. **Model.save()**: Iterates over all fields and calls `field.on_save()` for each (line 1162-1171 in `base.py`)
3. **Relationship.on_save()**: Currently resolves the new `field_value` to a `related_db_key`, constructs the index set key (`$RelationshipF:Player:team:Team:TeamB`), and calls `SADD` to add the player's db_key to that set
4. **Bug**: No `SREM` is called on the OLD index set (`$RelationshipF:Player:team:Team:TeamA`), leaving a ghost entry
5. **After save**: `_saved_field_values` is updated with current values (line 1189-1192 in `base.py`), so the window to detect the old value is only INSIDE `on_save()`

## Why Previous Fixes Failed

This section is not applicable -- no previous attempt to fix this specific bug exists. However, the identical bug WAS fixed for `KeyFieldMixin` in PR #150, and the fix pattern is proven. The `Relationship` field was simply overlooked during that fix.

## Architectural Impact

- **New dependencies**: None
- **Interface changes**: None -- `Relationship.on_save()` signature is unchanged
- **Coupling**: No change -- uses the same `_saved_field_values` mechanism already used by `KeyFieldMixin`
- **Data ownership**: No change
- **Reversibility**: Trivially reversible (revert a single method change)

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

The fix is a direct port of the proven PR #150 pattern from `KeyFieldMixin.on_save()` to `Relationship.on_save()`. The code change is ~15 lines. Existing `xfail` tests already validate the fix.

## Prerequisites

No prerequisites -- this work has no external dependencies.

## Solution

### Key Elements

- **Relationship.on_save() old-value cleanup**: Before adding to the new relationship index set, check `_saved_field_values` for the old relationship value. If it differs from the current value, remove the instance from the old index set.

### Flow

**player.team changes** -> `save()` -> `Relationship.on_save()` -> read old value from `_saved_field_values` -> `SREM` old index set -> `SADD` new index set -> `_saved_field_values` updated

### Technical Approach

- Read `_saved_field_values` for the Relationship field's previous value inside `on_save()`
- Resolve the old value to a `related_db_key` using the same type-handling logic already in `on_save()` (Model instance, str redis_key, or None)
- Construct the old relationship set key and call `SREM` to remove the instance
- Then proceed with the existing `SADD` logic for the new value
- Handle the None-to-value and value-to-None transitions correctly (old value `None` maps to `related_db_key = "None"`)

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] No new exception handlers are added in this change. The existing error handling for invalid redis_key strings and unexpected types is unchanged.

### Empty/Invalid Input Handling
- [ ] Verify behavior when `_saved_field_values` is empty (first save -- no old value to clean up)
- [ ] Verify behavior when old value is `None` and new value is a Model instance
- [ ] Verify behavior when old value is a Model instance and new value is `None`
- [ ] Verify behavior when old value is a lazy-loaded string (redis_key format)

### Error State Rendering
- [ ] Not applicable -- this is a backend data integrity fix with no user-visible rendering

## Rabbit Holes

- **Refactoring `on_save()` to share code with `KeyFieldMixin`**: Tempting to DRY up the old-value-cleanup pattern, but `Relationship` resolves values differently (Model instances vs. scalars) and the duplication is minimal. Not worth abstracting.
- **Fixing `on_delete()` to also check `_saved_field_values`**: The `on_delete()` method already receives the correct field value from the save path (line 1149 in `base.py`). Not needed here.
- **Adding cleanup for ALL historical ghost entries**: Existing ghost entries from before the fix are a data migration concern, not a code fix concern. Out of scope.

## Risks

### Risk 1: Old value is a Model instance that has been garbage collected
**Impact:** `_saved_field_values` stores the actual Python object. If the related model instance was reassigned and garbage-collected, accessing it could fail.
**Mitigation:** The `_saved_field_values` dict holds a reference, preventing GC. Also, we only need `db_key` from it, which is a simple property. This risk is theoretical -- the same pattern works in `KeyFieldMixin`.

### Risk 2: Lazy-loaded string values in _saved_field_values
**Impact:** If the relationship was loaded from Redis (not set by user code), the saved value may be a redis_key string rather than a Model instance.
**Mitigation:** The cleanup code must handle all three value types (Model, str, None) using the same type-dispatch already present in `on_save()`. The PR #44 fix already addressed this pattern for `on_delete()`.

## Race Conditions

No race conditions identified. All operations occur within a single Redis pipeline during `save()`, which executes atomically. The `_saved_field_values` read and the `SREM`/`SADD` calls are all within the same synchronous method call.

## No-Gos (Out of Scope)

- Cleaning up historical ghost entries from before this fix
- Refactoring `on_save()` into a shared base class method
- Adding `on_save()` cleanup for other field types (already done in PR #150 for KeyField, PR #158 tests confirm SortedField and GeoField are correct)
- Many-to-many relationship support (future feature, `many=True` is not implemented)

## Update System

No update system changes required -- this is a library bug fix internal to popoto. No deployment scripts, config files, or dependencies change.

## Agent Integration

No agent integration required -- this is a popoto library fix. No MCP servers, bridge changes, or tool wrapping needed.

## Documentation

### Inline Documentation
- [ ] Update `Relationship.on_save()` docstring to document the old-value cleanup behavior
- [ ] Add code comments explaining the `_saved_field_values` check (matching KeyFieldMixin style)

### External Documentation
- [ ] No external documentation changes needed -- this is an internal bug fix

## Success Criteria

- [ ] `Relationship.on_save()` removes instance from old index set when value changes
- [ ] Test case 2 (`test_changing_related_object_cleans_old_index`) passes without `xfail`
- [ ] Test case 12 (`test_clearing_relationship_removes_from_index`) passes without `xfail`
- [ ] All existing tests pass (no regressions)
- [ ] Tests pass (`/do-test`)
- [ ] Lint clean (`ruff check .`)

## Team Orchestration

### Team Members

- **Builder (relationship-fix)**
  - Name: relationship-fixer
  - Role: Implement the on_save() cleanup logic in Relationship field
  - Agent Type: builder
  - Resume: true

- **Validator (relationship-fix)**
  - Name: relationship-validator
  - Role: Verify fix passes edge case tests and no regressions
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Implement on_save() old-value cleanup
- **Task ID**: build-relationship-fix
- **Depends On**: none
- **Assigned To**: relationship-fixer
- **Agent Type**: builder
- **Parallel**: true
- Add `_saved_field_values` check at the top of `Relationship.on_save()` in `src/popoto/fields/relationship.py`
- Resolve old value to `related_db_key` using same type-dispatch (Model, str, None)
- Call `SREM` on old relationship set key when value has changed
- Update docstring to document the cleanup behavior
- Remove `xfail` markers from test cases 2 and 12 in `tests/test_field_index_edge_cases.py`

### 2. Validate fix
- **Task ID**: validate-relationship-fix
- **Depends On**: build-relationship-fix
- **Assigned To**: relationship-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_field_index_edge_cases.py -x -q` to verify edge case tests pass
- Run `pytest tests/ -x -q` to verify no regressions
- Run `ruff check .` and `ruff format --check .` for lint/format compliance

### 3. Final Validation
- **Task ID**: validate-all
- **Depends On**: validate-relationship-fix
- **Assigned To**: relationship-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify all success criteria met
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Edge case tests pass | `pytest tests/test_field_index_edge_cases.py -x -q` | exit code 0 |
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check .` | exit code 0 |
| Format clean | `python -m ruff format --check .` | exit code 0 |
| xfail removed test 2 | `grep -c "xfail" tests/test_field_index_edge_cases.py` | output contains 0 |

---

## Open Questions

None. The fix pattern is proven (PR #150), the test cases already exist (PR #158), and the scope is narrow. This plan is ready for implementation.
