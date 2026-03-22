---
status: Archived
type: feature
appetite: Medium
owner: Valor
created: 2026-03-17
tracking: https://github.com/tomcounsell/popoto/issues/228
last_comment_id:
---

> **Archived** -- This plan has shipped. See [features/prediction-ledger.md](../features/prediction-ledger.md) for the current documentation.

# PredictionLedger — Outcome Tracking with Auto-Resolution

## Problem

AI agents lack the ability to learn from their own predictions. An agent predicts "this task will take 30 minutes" and it actually takes 90, but there's no mechanism to record that delta and feed it back into the memory system. The agent makes the same miscalibrated predictions forever.

**Current behavior:**
Agents can store memories, track confidence, and observe outcomes via ObservationProtocol, but there's no structured way to record prediction-outcome pairs, compute prediction error, or use that error signal to adjust confidence in the knowledge that informed the prediction.

**Desired outcome:**
A `PredictionLedgerMixin` that lets agents record predictions before acting, record actual outcomes after, and atomically computes the delta. High prediction errors feed back into `ConfidenceField` to reduce trust in bad knowledge. Auto-resolution via `ObservationProtocol` handles the common case where outcomes are inferred from behavior rather than explicitly reported.

## Prior Art

- **PR #206** (ObservationProtocol): Shipped the outcome inference hooks (`acted`/`dismissed`/`deferred`/`contradicted`). PredictionLedger extends this by treating every surfacing as an implicit prediction.
- **PR #215** (ConfidenceField): Shipped Bayesian confidence tracking. PredictionLedger is the primary producer of signals that drive confidence updates.
- **Issue #228**: The tracking issue for this work. Contains the API sketch and acceptance criteria.

No prior attempts at PredictionLedger exist — this is greenfield.

## Data Flow

1. **Record prediction**: Agent calls `record_prediction(instance, predicted={...})` before acting. Stores prediction metadata in Redis hash `$PL:{ClassName}:meta:{pk}` with `resolved=false`.
2. **Resolve prediction (explicit)**: Agent calls `resolve_prediction(instance, actual={...})` after acting. Lua script atomically reads prediction, computes error delta, sets `resolved=true`, `resolution_mode="explicit"`, and ZADDs error to `$PL:{ClassName}:errors:{partition}`.
3. **Resolve prediction (auto)**: When `ObservationProtocol.on_context_used()` fires with an `acted`/`dismissed`/`contradicted` outcome, it calls `auto_resolve(instance, outcome)` which maps the outcome to a prediction error and resolves the same way, with `resolution_mode="observed"`.
4. **Confidence feedback**: If the model has a `ConfidenceField` and prediction error exceeds `_pl_confidence_error_threshold` (default 0.7), calls `update_confidence(signal=_pl_confidence_low_signal)` (default 0.2) to reduce trust. Below the threshold, no confidence change.
5. **EventStreamMixin**: Resolution events are logged via `_xadd_event(op="prediction_resolved", ...)` for downstream processing.

## Architectural Impact

- **New file**: `src/popoto/fields/prediction_ledger.py` — self-contained mixin, follows the same pattern as `AccessTrackerMixin` and `WriteFilterMixin`
- **Modified file**: `src/popoto/fields/observation.py` — `_apply_acted()`, `_apply_dismissed()`, `_apply_contradicted()` gain calls to `auto_resolve()` when the instance uses `PredictionLedgerMixin`
- **New dependency**: None — uses existing Redis commands (HSET, HGET, ZADD) + Lua scripts
- **Interface changes**: New mixin class, new methods on instances. No existing interfaces change.
- **Coupling**: Loose — PredictionLedger checks for ConfidenceField/EventStreamMixin via `isinstance()` (graceful degradation pattern established by ObservationProtocol)
- **Reversibility**: Fully reversible — removing the mixin from a model class leaves all other functionality intact

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (scope alignment on auto-resolution mapping)
- Review rounds: 1 (code review)

## Prerequisites

No prerequisites — uses only core Redis commands and existing Popoto infrastructure.

## Solution

### Key Elements

- **PredictionLedgerMixin**: Model mixin that adds `record_prediction()`, `resolve_prediction()`, and `auto_resolve()` methods
- **Lua prediction resolution script**: Atomically reads prediction, computes error, marks resolved, ZADDs to error index
- **ObservationProtocol integration**: Auto-resolution hook wired into `_apply_acted()`, `_apply_dismissed()`, `_apply_contradicted()`
- **ConfidenceField feedback**: Prediction errors above threshold trigger confidence adjustment

### Flow

**Agent predicts** → `record_prediction(predicted={...})` → **Redis hash stores prediction**

**Agent acts** → `resolve_prediction(actual={...})` → **Lua computes delta** → **ZADD to error index** → **ConfidenceField.update_confidence()** → **EventStream logs resolution**

**OR (auto-resolution):**

**ObservationProtocol fires** → `auto_resolve(outcome="acted")` → **maps outcome to error** → **same resolution path**

### Technical Approach

- **Redis hash for prediction metadata**: `$PL:{ClassName}:meta:{pk}` stores `{predicted, resolved, resolution_mode, prediction_error, resolved_at}` as msgpack-encoded values. One hash per model class, keyed by instance PK.
- **Redis sorted set for error index**: `$PL:{ClassName}:errors:{partition}` stores PKs scored by `|prediction_error|`. Partition is the model class name by default (single partition); subclasses can override to partition by time window, category, or other domain-specific grouping. Enables querying "which predictions had the highest error?" for learning.
- **Lua script for atomic resolution**: Single EVAL that reads hash, computes error, updates hash, ZADDs to error set. Prevents race conditions between concurrent resolve calls.
- **Prediction error computation**: For dict predictions, use a simple key-by-key comparison. Numeric values use `|predicted - actual| / max(|predicted|, |actual|, 1)` (normalized absolute error). String values use exact match (0.0 or 1.0). Missing keys count as 1.0 error. Overall error is the mean across all keys.
- **Auto-resolution outcome mapping**: `acted` → error 0.1, `dismissed` → error 0.5, `contradicted` → error 0.9. Stored as class attributes (`_pl_error_acted`, `_pl_error_dismissed`, `_pl_error_contradicted`). These are magic numbers — best-guess defaults to be tuned via experiments, not intended for dev/user configuration.
- **Idempotent resolution**: Resolving an already-resolved prediction is a no-op (Lua script checks `resolved` flag first).

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `resolve_prediction()` on unsaved instance raises `TypeError` (consistent with ConfidenceField pattern)
- [ ] `resolve_prediction()` on already-resolved prediction is a no-op (returns None, no exception)
- [ ] `record_prediction()` with `None` predicted raises `ValueError`
- [ ] Lua script failure during resolution: non-pipeline mode logs warning and returns None (does not crash save)

### Empty/Invalid Input Handling
- [ ] `record_prediction(predicted={})` — empty dict is allowed (edge case: agent has no specific prediction)
- [ ] `resolve_prediction(actual=None)` — raises `ValueError`
- [ ] `auto_resolve()` with invalid outcome string — raises `ValueError`

### Error State Rendering
- Not applicable — no user-visible output. All errors propagate via exceptions or return values.

## Test Impact

No existing tests affected — this is a greenfield mixin. New test files will be created:
- `tests/test_prediction_ledger.py` — core mixin functionality
- Synergy tests added to existing test files or a dedicated synergy test file

## Rabbit Holes

- **Custom error metrics per field type**: Tempting to build sophisticated error computation (cosine similarity, edit distance, etc.) but the simple normalized-absolute-error approach covers 90% of use cases. Custom metrics can be added later via an overridable method.
- **Prediction history/versioning**: Storing multiple predictions per instance or tracking prediction evolution over time. Out of scope — the ledger tracks the most recent prediction-outcome pair per instance.
- **Automatic cycle amplitude adjustment from prediction error**: The roadmap mentions entrainment. This is already handled by ObservationProtocol's `strengthen_cycle`/`weaken_cycle` calls. PredictionLedger doesn't need to duplicate this.

## Risks

### Risk 1: Error computation for complex dict structures
**Impact:** Deeply nested dicts or mixed-type values could produce confusing error scores
**Mitigation:** Flatten to top-level keys only for v1. Document the limitation. Allow override via `compute_prediction_error(predicted, actual)` method on the mixin.

### Risk 2: Auto-resolution produces false learning signals
**Impact:** An `acted` outcome doesn't always mean the prediction was correct — the agent may have acted despite the prediction being wrong
**Mitigation:** Use conservative error values (0.1 for acted, not 0.0). All magic numbers are class attributes, validated via parameter sweep (see [tuning guide](../../docs/guides/tuning-magic-numbers.md)).

## Race Conditions

### Race 1: Concurrent resolve calls on same instance
**Location:** `resolve_prediction()` Lua script
**Trigger:** Two processes both call `resolve_prediction()` on the same instance simultaneously
**Data prerequisite:** Prediction must be recorded (hash entry exists with `resolved=false`)
**State prerequisite:** Lua script reads `resolved` flag before writing
**Mitigation:** Lua scripts execute atomically in Redis — the first call resolves, the second sees `resolved=true` and is a no-op.

### Race 2: Record + resolve without save in between
**Location:** `record_prediction()` then immediate `resolve_prediction()`
**Trigger:** Agent records prediction but instance isn't saved yet
**Data prerequisite:** Instance must be saved (have a valid `db_key.redis_key`)
**State prerequisite:** Both methods check `db_key.redis_key` exists
**Mitigation:** Both methods raise `TypeError` if instance is unsaved (same pattern as `ConfidenceField.update_confidence()`).

## No-Gos (Out of Scope)

- Prediction history/log (multiple predictions per instance) — v2
- Custom error metric plugins beyond the overridable method
- Automatic cycle entrainment from prediction data (already handled by ObservationProtocol)
- Prediction aggregation/analytics queries (use the error sorted set directly)
- UI or dashboard for prediction calibration

## Update System

No update system changes required — this is a Popoto ORM library feature with no deployment infrastructure.

## Agent Integration

No agent integration required — this is a Popoto ORM primitive. Agent applications compose this mixin into their models.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/agent-memory.md` PredictionLedger section with full API reference
- [ ] Add `docs/plans/prediction_ledger.md` (this plan)

### External Documentation Site
- [ ] Update `docs/fields.md` with PredictionLedgerMixin reference
- [ ] Update `docs/api-reference.md` with method signatures
- [ ] Verify `mkdocs serve` builds cleanly

### Inline Documentation
- [ ] Module docstring in `prediction_ledger.py` with Redis key patterns and examples
- [ ] Method docstrings for all public methods

## Success Criteria

- [ ] `PredictionLedgerMixin` class with `record_prediction()`, `resolve_prediction()`, `auto_resolve()` methods
- [ ] Prediction error computed atomically via Lua script
- [ ] Auto-resolution from ObservationProtocol outcomes (`acted`, `dismissed`, `contradicted`)
- [ ] ConfidenceField integration — high error reduces confidence
- [ ] EventStreamMixin integration — prediction events logged via `_xadd_event()`
- [ ] Pipeline support (`pipeline=` parameter) for all operations
- [ ] Idempotent resolution (re-resolving is a no-op)
- [ ] Valkey compatible (no Redis modules — only HSET, HGET, ZADD, EVAL)
- [ ] Tests covering: basic predict/resolve, auto-resolve, idempotency, ConfidenceField synergy, EventStream synergy, ObservationProtocol synergy
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (prediction-ledger)**
  - Name: pl-builder
  - Role: Implement PredictionLedgerMixin, Lua script, and ObservationProtocol integration
  - Agent Type: builder
  - Resume: true

- **Validator (prediction-ledger)**
  - Name: pl-validator
  - Role: Verify mixin functionality, synergy tests, Redis key patterns
  - Agent Type: validator
  - Resume: true

- **Builder (tests)**
  - Name: test-builder
  - Role: Write comprehensive tests including synergy tests
  - Agent Type: test-writer
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Update agent-memory.md, fields.md, api-reference.md
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Implement PredictionLedgerMixin
- **Task ID**: build-prediction-ledger
- **Depends On**: none
- **Validates**: tests/test_prediction_ledger.py (create)
- **Assigned To**: pl-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/fields/prediction_ledger.py` with:
  - `PredictionLedgerMixin` class with underscore-prefixed class attributes
  - `record_prediction(instance, predicted, pipeline=None)` — stores prediction in Redis hash
  - `resolve_prediction(instance, actual, pipeline=None)` — Lua script computes error, marks resolved
  - `auto_resolve(instance, outcome, pipeline=None)` — maps outcome string to error, delegates to resolve logic
  - `get_prediction_data(instance)` — read current prediction metadata
  - `get_highest_errors(model_class, partition, limit)` — query error sorted set
  - `compute_prediction_error(predicted, actual)` — overridable error computation method
  - Lua script for atomic resolution (read hash → compute error → update hash → ZADD error)
  - ConfidenceField feedback: on resolution, if error > `_pl_confidence_error_threshold` (0.7), call `update_confidence(signal=_pl_confidence_low_signal)` (0.2) to reduce confidence. Below threshold, no change.
  - EventStreamMixin: on resolution, call `_xadd_event(op="prediction_resolved", ...)`
  - Redis keys: `$PL:{ClassName}:meta:{pk}` (hash), `$PL:{ClassName}:errors:{partition}` (sorted set)
- Register in `src/popoto/fields/__init__.py` and `src/popoto/__init__.py`

### 2. Wire ObservationProtocol integration
- **Task ID**: build-observation-integration
- **Depends On**: build-prediction-ledger
- **Validates**: tests/test_prediction_ledger.py
- **Assigned To**: pl-builder
- **Agent Type**: builder
- **Parallel**: false
- Modify `src/popoto/fields/observation.py`:
  - In `_apply_acted()`: check if instance uses `PredictionLedgerMixin`, if so call `auto_resolve(instance, "acted", pipeline)`
  - In `_apply_dismissed()`: same with `"dismissed"`
  - In `_apply_contradicted()`: same with `"contradicted"`
  - Import `PredictionLedgerMixin` locally to avoid circular imports (follow existing pattern)

### 3. Write tests
- **Task ID**: build-tests
- **Depends On**: build-observation-integration
- **Validates**: pytest tests/test_prediction_ledger.py -v
- **Assigned To**: test-builder
- **Agent Type**: test-writer
- **Parallel**: false
- Create `tests/test_prediction_ledger.py` with:
  - Basic predict/resolve cycle
  - Auto-resolve from each outcome type
  - Idempotent resolution (resolve twice, second is no-op)
  - Error computation for numeric, string, and mixed-type dicts
  - ConfidenceField synergy: high error reduces confidence
  - EventStreamMixin synergy: resolution produces stream entry
  - ObservationProtocol synergy: `on_context_used` triggers auto-resolve
  - Pipeline support: all operations work with pipeline parameter
  - Error cases: unsaved instance, None actual, already-resolved
  - `get_highest_errors` query

### 4. Validate implementation
- **Task ID**: validate-prediction-ledger
- **Depends On**: build-tests
- **Assigned To**: pl-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify all tests pass
- Verify Redis key patterns match documentation
- Verify Lua script handles edge cases (missing hash, concurrent calls)
- Verify graceful degradation (no ConfidenceField → skip feedback, no EventStream → skip logging)
- Verify no Redis module commands used (Valkey compatible)

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-prediction-ledger
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/agent-memory.md` PredictionLedger section with full API
- Update `docs/fields.md` with PredictionLedgerMixin reference
- Update `docs/api-reference.md` with method signatures
- Verify `mkdocs serve` builds

### 6. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: pl-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify all success criteria met
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_prediction_ledger.py -v` | exit code 0 |
| Full suite passes | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `black --check src/popoto/fields/prediction_ledger.py` | exit code 0 |
| No Redis modules | `grep -r 'BF\.\|CMS\.\|FT\.\|JSON\.' src/popoto/fields/prediction_ledger.py` | exit code 1 |
| Mixin importable | `python -c "from popoto.fields.prediction_ledger import PredictionLedgerMixin"` | exit code 0 |

## Resolved Questions

1. **Error computation granularity**: Top-level key comparison for v1. Nested dicts are out of scope.

2. **Auto-resolution error values**: Use defaults (`acted=0.1, dismissed=0.5, contradicted=0.9`) as class attributes. Validated via parameter sweep — all within safe operating ranges (see [tuning guide](../../docs/guides/tuning-magic-numbers.md)).

3. **Confidence feedback**: As implemented — threshold-based: if `prediction_error > _pl_confidence_error_threshold` (0.7), send `signal=_pl_confidence_low_signal` (0.2) to ConfidenceField. Below threshold, no change. Both values validated via parameter sweep.
