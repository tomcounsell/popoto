---
status: Completed
type: chore
appetite: Small
owner: valorengels
created: 2026-03-11
tracking: https://github.com/valorengels/popoto/issues/165
last_comment_id:
---

# Harden Edge Case Tests: Remove Dead xfail Guards in TC1, TC6, TC9

## Problem

Three test cases in `tests/test_field_index_edge_cases.py` contain conditional `pytest.xfail()` guards that were written when the underlying bugs were still open. Those bugs have since been fixed by PRs #159, #161, #162, and #163 — but the xfail branches were never removed.

**Current behavior:** Each guard is structured as `if len(old_members) > 0: pytest.xfail(...)`. Because the fixes are in place, `old_members` is always empty and the `xfail` branch is never reached. A future regression would repopulate `old_members`, hit the `xfail()` call, and the suite would still report green — hiding the regression entirely.

**Desired outcome:** Each `if len(old_members) > 0: pytest.xfail(...)` block is deleted and replaced with a direct `assert len(old_members) == 0`. A regression causes an immediate red test failure, not a silent xpass.

## Prior Art

- **PR #159**: Fix SortedField ghost entries on partition key change — fixed the bug that TC1's xfail guarded against.
- **PR #161**: Fix save() to remove obsolete key from class set — fixed the bugs that TC6 and TC9's xfail guards defended against.
- **PR #162**: Fix partial save (update_fields) obsolete_redis_key cleanup — companion to #161.
- **PR #163**: Related index cleanup work — companion to #161/#162.

## Data Flow

Not applicable — this is a test-only chore with no production code changes.

## Architectural Impact

- **New dependencies**: None
- **Interface changes**: None
- **Coupling**: None
- **Data ownership**: None
- **Reversibility**: Trivial — a one-line revert per test case

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

No prerequisites — this work has no external dependencies.

## Solution

### Key Elements

- **TC1 (~line 221)**: Delete the 4-line `if len(old_members) > 0: pytest.xfail(...)` block. Add `assert len(old_members) == 0, "Old partition sorted set should be empty after key change"` before the existing `assert len(new_members) == 1`.
- **TC6 (~line 442)**: Delete the 4-line `if len(old_members) > 0: pytest.xfail(...)` block. Add `assert len(old_members) == 0, "Old email index should be empty after value change"` before the existing assertion.
- **TC9 (~line 585)**: Delete the 4-line `if len(old_region_members) > 0: pytest.xfail(...)` block. Add `assert len(old_region_members) == 0, "Old region index should be empty after composite key change"` before the existing assertions.

### Flow

Read test file → locate three xfail blocks → delete each guard → insert hard assertion → run tests → all 12 pass

### Technical Approach

- Edit `tests/test_field_index_edge_cases.py` only — no production code touched
- Three surgical deletions + three assertion insertions
- No new test infrastructure needed

## Failure Path Test Strategy

### Exception Handling Coverage
- No exception handlers in scope — this is a test file edit only.

### Empty/Invalid Input Handling
- Not applicable — no new functions or inputs introduced.

### Error State Rendering
- Not applicable — no user-visible output.

## Rabbit Holes

- Do NOT refactor the test file beyond the three targeted xfail removals.
- Do NOT convert other xfail patterns in the suite unless they are explicitly covered by this issue.
- Do NOT investigate whether additional `old_members` assertions should be added elsewhere.

## Risks

### Risk 1: A regression is already present
**Impact:** After removing the xfail guards, one or more tests would fail immediately.
**Mitigation:** Run `pytest tests/test_field_index_edge_cases.py -v` before committing. If a test fails, the regression must be filed as a separate bug — the guard must still be removed and the hard assertion kept.

## Race Conditions

No race conditions identified — all test operations are synchronous, single-threaded, and use a local Redis instance with no shared state across test cases.

## No-Gos (Out of Scope)

- Converting any xfail markers in other test files
- Adding new test cases
- Touching production source files

## Update System

No update system changes required — this is a test-only chore.

## Agent Integration

No agent integration required — this is a test-only chore.

## Documentation

No documentation changes needed — no public API or behavior changes.

## Success Criteria

- [x] TC1 contains no `pytest.xfail()` call and asserts `len(old_members) == 0` directly
- [x] TC6 contains no `pytest.xfail()` call and asserts `len(old_members) == 0` directly
- [x] TC9 contains no `pytest.xfail()` call and asserts `len(old_region_members) == 0` directly
- [x] `pytest tests/test_field_index_edge_cases.py` reports 12 passed, 0 xfailed
- [x] `grep -c "pytest.xfail" tests/test_field_index_edge_cases.py` outputs `0`

## Team Orchestration

### Team Members

- **Builder (xfail-cleanup)**
  - Name: xfail-builder
  - Role: Remove the three dead xfail guards and insert hard assertions in `tests/test_field_index_edge_cases.py`
  - Agent Type: builder
  - Resume: true

- **Validator (xfail-cleanup)**
  - Name: xfail-validator
  - Role: Verify tests pass and no xfail calls remain
  - Agent Type: validator
  - Resume: true

### Step by Step Tasks

#### 1. Remove xfail guards and add hard assertions
- **Task ID**: build-xfail-cleanup
- **Depends On**: none
- **Assigned To**: xfail-builder
- **Agent Type**: builder
- **Parallel**: true
- Open `tests/test_field_index_edge_cases.py`
- **TC1 (~line 221):** Delete the block `if len(old_members) > 0:\n    pytest.xfail(...)` (lines 221–225). Insert `assert len(old_members) == 0, "Old partition sorted set should be empty after key change"` immediately before `assert len(new_members) == 1`.
- **TC6 (~line 442):** Delete the block `if len(old_members) > 0:\n    pytest.xfail(...)` (lines 442–446). Insert `assert len(old_members) == 0, "Old email index should be empty after value change"` immediately before the assertion that follows.
- **TC9 (~line 585):** Delete the block `if len(old_region_members) > 0:\n    pytest.xfail(...)` (lines 585–589). Insert `assert len(old_region_members) == 0, "Old region index should be empty after composite key change"` immediately before the assertion that follows.

#### 2. Validate
- **Task ID**: validate-xfail-cleanup
- **Depends On**: build-xfail-cleanup
- **Assigned To**: xfail-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_field_index_edge_cases.py -v` — must report 12 passed, 0 xfailed
- Run `grep -c "pytest.xfail" tests/test_field_index_edge_cases.py` — must output `0`
- Report pass/fail

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Edge case tests pass | `pytest tests/test_field_index_edge_cases.py -v` | exit code 0 |
| No xfail calls remain | `grep -c "pytest.xfail" tests/test_field_index_edge_cases.py` | output contains 0 |
| Full test suite passes | `pytest tests/ -x -q` | exit code 0 |
