---
status: Complete
type: chore
appetite: Small
owner: Valor
created: 2026-03-31
tracking: https://github.com/tomcounsell/popoto/issues/323
last_comment_id:
---

# Companion Hash Key Public API

## Problem

Popoto's specialized fields (ConfidenceField, CyclicDecayField, CoOccurrenceField) maintain companion Redis hashes alongside the main model hash. The keys for these companion hashes are constructed by string-concatenating hardcoded suffixes (`:data`, `:cycles`, `:pressure`, `:<pk>`) onto a base key, but the methods that build these keys are private (`_get_data_hash_key`, `_get_cycles_hash_key`, etc.).

**Current behavior:**

The query system (`query.py:1213`) duplicates the `:data` suffix outside the owning field class, breaking encapsulation:

```python
# query.py — _materialize_confidence_field()
base_key = field.get_special_use_field_db_key(model_class, field_name)
data_hash_key = base_key.redis_key + ":data"   # hardcoded suffix
```

External callers who need companion hash keys must reverse-engineer the suffix convention from source code.

**Desired outcome:**

Companion hash key methods are public (no underscore prefix), documented, and used consistently. The suffix convention is defined in exactly one place per field type. `query.py` uses the field's own method instead of duplicating the suffix.

## Prior Art

- **PR #328**: Add partition_by support to ConfidenceField -- introduced `_get_data_hash_key_from_values()` for query-path access, but kept all methods private
- **PR #201**: Add CyclicDecayField -- introduced `:cycles` and `:pressure` suffix convention with private methods
- **PR #284**: Fix ConfidenceField attribute access (#281) -- fixed instance attribute sync but did not address key API

No prior attempt to expose a public companion key API.

## Architectural Impact

- **Interface changes**: Private methods become public (drop underscore prefix). This is additive -- existing private names can remain as aliases during transition or be removed since this is internal API.
- **Coupling**: Decreases coupling. `query.py` currently reaches into field internals; after this change it calls the field's own public method.
- **Reversibility**: Trivially reversible -- just rename methods back.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

This is a pure refactoring: rename methods, fix one caller in `query.py`, update tests to use new names. No behavioral changes.

## Prerequisites

No prerequisites -- this work has no external dependencies.

## Solution

### Key Elements

- **Public companion key methods**: Drop the underscore prefix from all companion hash key methods across ConfidenceField, CyclicDecayField, and CoOccurrenceField
- **query.py encapsulation fix**: Replace the hardcoded `":data"` suffix in `_materialize_confidence_field()` with a call to `field.get_data_hash_key_from_values()`
- **Test updates**: Mechanically update all test references from private to public method names

### Technical Approach

1. Rename methods (drop underscore prefix):
   - ConfidenceField: `_get_data_hash_key` -> `get_data_hash_key`, `_get_data_hash_key_from_values` -> `get_data_hash_key_from_values`, `_get_old_data_hash_key` -> `get_old_data_hash_key`
   - CyclicDecayField: `_get_cycles_hash_key` -> `get_cycles_hash_key`, `_get_pressure_hash_key` -> `get_pressure_hash_key`, `_get_cycles_hash_key_from_parts` -> `get_cycles_hash_key_from_parts`, `_get_pressure_hash_key_from_parts` -> `get_pressure_hash_key_from_parts`
   - CoOccurrenceField: `_get_edge_key` -> `get_edge_key`, `_get_edge_key_prefix` -> `get_edge_key_prefix`

2. Fix `query.py:1210-1213`: Replace the unpartitioned branch with a call to `field.get_data_hash_key_from_values(model_class, field_name)` (no partition values needed for unpartitioned fields -- the method already handles this).

3. Update all internal callers within each field class (on_save, on_delete, update_confidence, get_confidence, etc.) to use the new public names.

4. Update all test files that reference the private method names.

### Flow

**Field class defines suffix** -> Public `get_*_hash_key()` method builds full Redis key -> All callers (field internals, query.py, tests) use public method -> Suffix defined in exactly one place

## Failure Path Test Strategy

### Exception Handling Coverage
- No exception handlers in scope -- this is a pure rename refactoring

### Empty/Invalid Input Handling
- No new functions or modified signatures -- existing validation unchanged

### Error State Rendering
- No user-visible output -- internal API only

## Test Impact

All affected tests call private methods directly for Redis key construction. Each reference changes from `_get_*` to `get_*` (mechanical rename). No assertion logic changes.

- [ ] `tests/test_confidence_field.py` (2 calls) -- UPDATE: rename `_get_data_hash_key` to `get_data_hash_key`
- [ ] `tests/test_partitioned_confidence.py` (12 calls) -- UPDATE: rename `_get_data_hash_key`, `_get_data_hash_key_from_values` to public versions
- [ ] `tests/test_cyclic_decay_field.py` (18 calls) -- UPDATE: rename `_get_cycles_hash_key`, `_get_pressure_hash_key`, `_get_cycles_hash_key_from_parts`, `_get_pressure_hash_key_from_parts` to public versions
- [ ] `tests/test_observation_protocol.py` (3 calls) -- UPDATE: rename `_get_cycles_hash_key`, `_get_pressure_hash_key` to public versions
- [ ] `tests/test_agent_memory_e2e.py` (5 calls) -- UPDATE: rename `_get_pressure_hash_key`, `_get_cycles_hash_key` to public versions

## Rabbit Holes

- Adding a generic `get_companion_keys()` method to the base `Field` class -- over-abstraction for three field types with different suffix patterns
- Introducing a `suffix` parameter to `get_special_use_field_db_key()` -- adds complexity for minimal benefit; each field type already knows its own suffixes
- Renaming the Redis keys themselves -- explicitly out of scope, no data migration needed

## Risks

### Risk 1: Downstream consumers using private method names
**Impact:** Code outside this repo calling `_get_data_hash_key()` directly would break.
**Mitigation:** These are internal ORM methods. The private prefix already signals "no stability guarantee." The rename is the fix.

## Race Conditions

No race conditions identified -- this is a pure rename refactoring with no changes to concurrency, data flow, or timing.

## No-Gos (Out of Scope)

- Renaming Redis keys or changing key patterns (backward compatibility requirement)
- Adding a base class abstraction for companion key generation
- Migration tooling for existing data
- Adding `suffix` parameter to `get_special_use_field_db_key()`

## Update System

No update system changes required -- popoto is a library dependency, not a deployed service. Consumers pick up changes via version bump.

## Agent Integration

No agent integration required -- this is a library-internal refactoring in the popoto ORM. No MCP servers, bridge changes, or tool wrappers needed.

## Documentation

### Inline Documentation
- [ ] Update docstrings on all renamed methods to reflect public API status
- [ ] Add brief docstring note on each public method explaining when external callers would use it

### External Documentation Site
- [ ] Update `docs/plans/confidence_field.md` if it references private method names
- [ ] Update `docs/plans/cyclic_decay_field.md` if it references private method names
- [ ] Update `docs/plans/co_occurrence_field.md` if it references private method names
- [ ] Verify docs build passes (`mkdocs build`)

## Success Criteria

- [ ] `query.py` calls the field's own companion-key method instead of hardcoding `":data"`
- [ ] Each companion hash key method is public (no underscore prefix) and documented
- [ ] No location outside a field class hardcodes a companion hash suffix
- [ ] Existing tests continue to pass (updated to use public method names)
- [ ] Tests pass (`/do-test`)

## Team Orchestration

### Team Members

- **Builder (refactor)**
  - Name: refactor-builder
  - Role: Rename methods, fix query.py, update tests
  - Agent Type: builder
  - Resume: true

- **Validator (verify)**
  - Name: refactor-validator
  - Role: Verify no private method references remain, tests pass
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Rename companion key methods to public API
- **Task ID**: build-rename
- **Depends On**: none
- **Validates**: tests/test_confidence_field.py, tests/test_cyclic_decay_field.py, tests/test_partitioned_confidence.py
- **Assigned To**: refactor-builder
- **Agent Type**: builder
- **Parallel**: true
- Rename all `_get_*_hash_key*` methods to drop the underscore prefix in confidence_field.py, cyclic_decay_field.py, and co_occurrence_field.py
- Update all internal callers within each field class (on_save, on_delete, update_confidence, get_confidence, get_confidence_data, get_confidence_filtered, migrate_to_partitioned)
- Fix query.py:1210-1213 to call `field.get_data_hash_key_from_values(model_class, field_name)` instead of hardcoding `:data`
- Update all test files to use the new public method names

### 2. Validate refactoring
- **Task ID**: validate-rename
- **Depends On**: build-rename
- **Assigned To**: refactor-validator
- **Agent Type**: validator
- **Parallel**: false
- Grep for any remaining `_get_data_hash_key`, `_get_cycles_hash_key`, `_get_pressure_hash_key`, `_get_edge_key` references
- Grep for any hardcoded `":data"`, `":cycles"`, `":pressure"` suffix concatenation outside field classes
- Run full test suite
- Verify no behavioral changes

### 3. Final Validation
- **Task ID**: validate-all
- **Depends On**: validate-rename
- **Assigned To**: refactor-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all validation commands
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| No private key methods in field classes | `grep -rn '_get_data_hash_key\|_get_cycles_hash_key\|_get_pressure_hash_key\|_get_edge_key' src/popoto/fields/ src/popoto/models/query.py` | exit code 1 |
| No hardcoded :data suffix outside fields | `grep -rn '+ ":data"' src/popoto/models/` | exit code 1 |
| Lint clean | `python -m ruff check .` | exit code 0 |
| Format clean | `python -m ruff format --check .` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

No open questions -- this is a straightforward mechanical refactoring with clear scope.
