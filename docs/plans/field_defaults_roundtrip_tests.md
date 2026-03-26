---
status: Done
type: chore
appetite: Small
owner: Agent
created: 2026-03-26
tracking: https://github.com/tomcounsell/popoto/issues/289
last_comment_id:
---

# Field Defaults Round-Trip Tests

## Problem

Two categories of field default behavior lack regression tests after save-load round-trips through Redis.

**Current behavior:**
1. ConfidenceField -- PR #284 fixed a bug where `instance.confidence` returned `None` instead of `initial_confidence` after attribute access. The fix was verified on fresh instances, but no test reloads the instance from Redis and checks the value. If the fix regresses (e.g., a change to the deserialization path in `encoding.py`), the existing tests will not catch it.
2. General field defaults -- `test_callable_defaults.py` tests callable defaults (e.g., `uuid.uuid4`) through save-load, but static defaults on basic field types (`IntField(default=42)`, `FloatField(default=1.0)`, `BooleanField(default=False)`, etc.) are only tested in-memory. A serialization bug could silently turn defaults into `None` on reload.

**Desired outcome:**
After `Model.create()` or `save()`, reloading the instance from Redis via `query.get()` returns the configured default/initial value for every field -- verified by automated tests for each field type.

## Prior Art

- **Issue #281**: ConfidenceField returns None despite initial=0.5 configured -- reported the original bug
- **PR #284**: Fix ConfidenceField attribute access returning None -- fixed the bug but tests only verify fresh instances, not reloaded ones
- **Issue #288**: Test gaps: round-trip persistence, partition queries, and field defaults -- proposed 6 tests but 4 of 6 were already covered; closed in favor of this narrower issue (#289) with only the 2 actual gaps
- **PR #47**: Fix multiple issues including #25 (callable defaults) -- added `test_callable_defaults.py` but only tested callable defaults through save-load, not static defaults

## Data Flow

1. **Entry point**: `Model.create(**kwargs)` -- creates instance, assigns field defaults, calls `save()`
2. **save()**: Internal pipeline queues `HSET` (hash data with encoded field values), `SADD` (class set, index sets), field `on_save()` calls
3. **encoding.py**: `encode_value()` serializes each field value (int, float, str, bool, dict, list, set) into a Redis-storable format; `decode_value()` deserializes on load
4. **query.get()**: Reads the hash from Redis, calls `decode_value()` for each field, constructs a new model instance with decoded values
5. **Output**: The reloaded instance's field values should match the configured defaults

The gap is at step 4-5: no tests verify that `decode_value()` correctly reconstructs default values after a full round-trip.

## Architectural Impact

- **New dependencies**: None
- **Interface changes**: None -- purely additive tests
- **Coupling**: No new coupling
- **Data ownership**: No change
- **Reversibility**: Fully reversible -- delete the test functions

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

No prerequisites -- this work has no external dependencies. Requires a running Redis instance for integration tests.

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis running | `redis-cli ping` | Test persistence round-trips |

## Solution

### Key Elements

- **ConfidenceField reload test**: A single test in `test_confidence_field.py` that creates, saves, reloads via `query.get()`, and asserts the confidence value matches `initial_confidence`
- **Parametrized static defaults test**: A parametrized test covering 8+ field types with static defaults through save-load-assert round-trips

### Technical Approach

1. Add `test_confidence_reload_from_redis` to the `TestAttributeAccess` class in `test_confidence_field.py`:
   - Create a `ConfidenceItem` with a known name
   - Reload via `ConfidenceItem.query.get(name=...)`
   - Assert `loaded.certainty is not None`
   - Assert `loaded.certainty == 0.5` (the configured `initial_confidence`)

2. Add a new test file `tests/test_field_defaults_roundtrip.py` with a parametrized test:
   - Define a model class per field type (or one model with all field types)
   - Each case: `KeyField` + one typed field with a static default
   - Pattern: `create()` -> `query.get()` -> assert value equals default
   - Field types to cover: `IntField(default=42)`, `FloatField(default=3.14)`, `DecimalField(default=Decimal("1.5"))`, `StringField(default="hello")`, `BooleanField(default=False)`, `DictField(default={"key": "val"})`, `ListField(default=[1, 2, 3])`, `SetField(default={"a", "b"})`

3. Each test cleans up after itself by deleting created instances.

### Flow

**Test runner** -> create model with defaults -> save to Redis -> reload via query.get() -> assert defaults match -> cleanup

## Failure Path Test Strategy

### Exception Handling Coverage
- No exception handlers in scope -- these are purely additive assertion tests

### Empty/Invalid Input Handling
- [ ] BooleanField with `default=False` is the key edge case: `False` is falsy and could be confused with `None` during deserialization. This is explicitly included in the parametrized test.
- [ ] Empty collection defaults (`default={}`, `default=[]`, `default=set()`) are covered if any field type uses them, but the primary goal is non-empty defaults

### Error State Rendering
- Not applicable -- no user-visible output

## Test Impact

No existing tests affected -- this is purely additive test coverage. No existing test behavior changes, no test files are modified (aside from appending a new test to `test_confidence_field.py`).

## Rabbit Holes

- Do not fix production code if a test reveals a bug -- file a separate issue instead
- Do not test callable defaults through round-trips (already covered in `test_callable_defaults.py::test_save_and_load_with_callable_default`)
- Do not test SortedField, DecayingSortedField, or ConfidenceField through the parametrized test -- these have specialized storage mechanisms and their own dedicated test files
- Do not add performance benchmarks or stress tests

## Risks

### Risk 1: A test reveals a live deserialization bug
**Impact:** A field type's default silently becomes `None` after reload
**Mitigation:** File a separate issue for the fix. The test itself is still valuable as a regression detector. Mark the failing assertion with `pytest.xfail` referencing the new issue until the fix lands.

## Race Conditions

No race conditions identified -- all tests are synchronous, single-threaded, and operate on isolated model instances with unique keys.

## No-Gos (Out of Scope)

- No production code changes
- No changes to encoding.py or field implementations
- No testing of callable defaults (already covered)
- No testing of specialized sorted/decay/confidence update mechanics (already covered in dedicated test files)

## Update System

No update system changes required -- this is a test-only change in an external library (popoto), not the ai system.

## Agent Integration

No agent integration required -- this is purely additive test coverage in the popoto library. No MCP servers, bridge changes, or tool exposure needed.

## Documentation

- [ ] No new feature documentation needed -- these are regression tests, not new features
- [ ] Add a brief comment block at the top of `test_field_defaults_roundtrip.py` explaining the purpose (round-trip coverage for static defaults, referencing issue #289)

## Success Criteria

- [ ] A test in `test_confidence_field.py` creates an instance, saves it, reloads via `query.get()`, and asserts `confidence is not None` and `confidence == initial_confidence`
- [ ] A parametrized test covers at least `IntField`, `FloatField`, `DecimalField`, `StringField`, `BooleanField`, `DictField`, `ListField`, and `SetField` with static defaults through save-load-assert round-trips
- [ ] All new tests pass: `pytest tests/test_confidence_field.py tests/test_field_defaults_roundtrip.py -v`
- [ ] No existing tests broken: `pytest tests/ -x -q`

## Team Orchestration

### Team Members

- **Builder (tests)**
  - Name: test-builder
  - Role: Implement both test files
  - Agent Type: test-engineer
  - Resume: true

- **Validator (tests)**
  - Name: test-validator
  - Role: Verify all tests pass and no regressions
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Add ConfidenceField reload test
- **Task ID**: build-confidence-reload
- **Depends On**: none
- **Validates**: `tests/test_confidence_field.py::test_confidence_reload_from_redis`
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: true
- Add `test_confidence_reload_from_redis` to `TestAttributeAccess` in `tests/test_confidence_field.py`
- Create instance with known name, reload via `query.get()`, assert `certainty is not None` and `certainty == 0.5`
- Clean up created instances

### 2. Add parametrized field defaults round-trip test
- **Task ID**: build-field-defaults
- **Depends On**: none
- **Validates**: `tests/test_field_defaults_roundtrip.py` (create)
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: true
- Create `tests/test_field_defaults_roundtrip.py`
- Define a parametrized test covering: IntField, FloatField, DecimalField, StringField, BooleanField, DictField, ListField, SetField
- Each case: define model, create instance, reload, assert default value matches
- Clean up after each test

### 3. Validate all tests pass
- **Task ID**: validate-all
- **Depends On**: build-confidence-reload, build-field-defaults
- **Assigned To**: test-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_confidence_field.py tests/test_field_defaults_roundtrip.py -v`
- Run `pytest tests/ -x -q` to confirm no regressions
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| New tests pass | `pytest tests/test_confidence_field.py tests/test_field_defaults_roundtrip.py -v` | exit code 0 |
| No regressions | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check tests/test_field_defaults_roundtrip.py` | exit code 0 |
| Format clean | `python -m ruff format --check tests/test_field_defaults_roundtrip.py` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---
