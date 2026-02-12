---
status: Planning
type: feature
appetite: Small
owner: Valor
created: 2026-02-12
tracking: https://github.com/tomcounsell/popoto/issues/135
---

# Rename sort_by to partition_by on SortedField

## Problem

`SortedField(sort_by=...)` is misleading. The parameter doesn't sort anything — it **partitions** the sorted set index by KeyField values, creating separate sorted sets per partition. The name `sort_by` suggests it controls sort order, which confuses users.

**Current behavior:**
```python
price = SortedField(type=float, sort_by="market")  # Creates separate sorted set per market
```
Users read `sort_by` and think it controls how results are ordered, not how the index is split.

**Desired outcome:**
```python
price = SortedField(type=float, partition_by="market")  # Clear: separate index per market
```
`partition_by` accurately describes the behavior. `sort_by` continues to work but emits a deprecation warning.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1 (PR review)

Straightforward rename with a deprecation shim. The code surface is well-contained.

## Prerequisites

No prerequisites — this work has no external dependencies.

## Solution

### Key Elements

- **`partition_by` parameter**: New canonical name accepted by `SortedFieldMixin.__init__`
- **`sort_by` deprecation shim**: `sort_by` still works but emits `DeprecationWarning`
- **Internal attribute rename**: The internal attribute becomes `self.partition_by` everywhere
- **Docs updated**: All references switch to `partition_by`

### Flow

User writes `partition_by="market"` → Field stores `self.partition_by = ("market",)` → Query reads `field.partition_by` to build sorted set keys → Same Redis behavior as before

User writes `sort_by="market"` → DeprecationWarning emitted → Maps to `self.partition_by` internally → Same behavior

### Technical Approach

- In `SortedFieldMixin.__init__`, accept both `partition_by` and `sort_by` kwargs
- If `sort_by` is provided (and `partition_by` is not), emit `DeprecationWarning` and copy to `partition_by`
- If both are provided, raise `ModelException` (ambiguous)
- Rename all internal references from `self.sort_by` to `self.partition_by` across:
  - `sorted_field_mixin.py` (main implementation)
  - `query.py` (reads `field.sort_by` for partition field names)
  - `shortcuts.py` (docstrings reference `sort_by`)
- Add a `sort_by` property on `SortedFieldMixin` that returns `self.partition_by` for backward compat (reading)
- Update all tests to use `partition_by`
- Update all docs to use `partition_by` with a note about the deprecation

## Rabbit Holes

- **Don't remove `sort_by` yet** — this is a deprecation cycle, not a removal. Removal happens in the next major version.
- **Don't rename Redis keys** — the Redis key structure (`$SortedF:Model:field:partition_value`) is unchanged. This is purely a Python API rename.
- **Don't change `sort_by` in query error messages to something new** — keep error messages accurate to what the user actually passed.

## Risks

### Risk 1: Breaking existing user code silently
**Impact:** Users who access `field.sort_by` directly would get an error
**Mitigation:** Add a `sort_by` property that proxies to `partition_by` and emits a deprecation warning

### Risk 2: Async module has parallel implementation
**Impact:** If the async module has separate `sort_by` handling, it could be missed
**Mitigation:** The async module uses the same `SortedFieldMixin` — grep confirmed `sort_by` in async tests is just model definitions, not separate implementation

## No-Gos (Out of Scope)

- Removing `sort_by` entirely (deferred to next major version)
- Renaming Redis key patterns
- Changing any runtime behavior beyond the deprecation warning

## Update System

No update system changes required — this is a library-only change.

## Agent Integration

No agent integration required — this is a library-only change.

## Documentation

### External Documentation Site
- [ ] Update `docs/fields.md` — rename `sort_by` section to `partition_by`, add deprecation note
- [ ] Update `docs/api-reference.md` — update parameter table and examples

### Inline Documentation
- [ ] Update docstrings in `sorted_field_mixin.py`
- [ ] Update docstrings in `shortcuts.py`

## Success Criteria

- [ ] `SortedField(partition_by="field")` works identically to old `sort_by`
- [ ] `SortedField(sort_by="field")` still works but emits `DeprecationWarning`
- [ ] Providing both `sort_by` and `partition_by` raises `ModelException`
- [ ] All internal code uses `partition_by` attribute name
- [ ] `field.sort_by` property returns `partition_by` value with deprecation warning
- [ ] All existing tests pass (updated to use `partition_by`)
- [ ] New test validates deprecation warning is emitted for `sort_by`
- [ ] Docs updated to use `partition_by`

## Team Orchestration

### Team Members

- **Builder (rename)**
  - Name: rename-builder
  - Role: Implement the rename across source, tests, and docs
  - Agent Type: builder
  - Resume: true

- **Validator (rename)**
  - Name: rename-validator
  - Role: Verify all references updated, tests pass, deprecation works
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Rename internal attribute and add deprecation shim
- **Task ID**: build-rename
- **Depends On**: none
- **Assigned To**: rename-builder
- **Agent Type**: builder
- **Parallel**: false
- In `sorted_field_mixin.py`:
  - Rename `self.sort_by` to `self.partition_by` throughout the file
  - Update `__init__` to accept `partition_by` kwarg, with `sort_by` as deprecated alias
  - Emit `DeprecationWarning` when `sort_by` is used: `"sort_by is deprecated, use partition_by instead"`
  - Raise `ModelException` if both `sort_by` and `partition_by` are provided
  - Add `sort_by` property that returns `self.partition_by` with deprecation warning
  - Update `field_defaults` dict to use `partition_by` key
  - Update all docstrings to reference `partition_by`
- In `query.py`:
  - Change all `field.sort_by` reads to `field.partition_by`
  - Update comments/docstrings
- In `shortcuts.py`:
  - Update docstrings referencing `sort_by` to `partition_by`

### 2. Update tests
- **Task ID**: build-tests
- **Depends On**: build-rename
- **Assigned To**: rename-builder
- **Agent Type**: builder
- **Parallel**: false
- Update all `sort_by=` in test model definitions to `partition_by=`
- Add test that `sort_by=` still works but emits `DeprecationWarning`
- Add test that providing both raises `ModelException`
- Run `pytest` to verify all tests pass

### 3. Update docs
- **Task ID**: build-docs
- **Depends On**: build-rename
- **Assigned To**: rename-builder
- **Agent Type**: builder
- **Parallel**: false
- Update `docs/fields.md`: rename section, update examples, add deprecation note
- Update `docs/api-reference.md`: update parameter table and examples

### 4. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-tests, build-docs
- **Assigned To**: rename-validator
- **Agent Type**: validator
- **Parallel**: false
- Grep for any remaining `sort_by` references that should be `partition_by`
- Verify deprecation warning works correctly
- Run full test suite
- Verify docs are consistent

## Validation Commands

- `pytest` — all tests pass
- `python -W error::DeprecationWarning -c "import popoto; popoto.SortedField(type=float, sort_by='x')"` — confirms deprecation warning is raised
- `grep -rn "sort_by" src/popoto/ --include="*.py"` — only the deprecation shim and property should remain
- `grep -rn "sort_by" tests/ --include="*.py"` — only deprecation-specific tests should remain
