---
status: Planning
type: feature
appetite: Medium
owner: Valor
created: 2026-03-14
tracking: https://github.com/tomcounsell/popoto/issues/209
last_comment_id:
---

# ConfidenceField — Bayesian Certainty Tracking with Entrainment

## Problem

All records in a Popoto-backed memory system currently have equal factual weight. When an agent retrieves contradictory information, both sides appear equally valid. There is no mechanism to track how confident the system should be in a given record, nor to let that confidence evolve based on evidence.

**Current behavior:**
Records returned by `top_by_decay()` are ranked by temporal relevance (decay + cycles + pressure) but carry no signal about factual reliability. A record that has been contradicted 10 times ranks the same as one corroborated 100 times, given equal temporal scores.

**Desired outcome:**
A `ConfidenceField` that maintains a Bayesian confidence score updated atomically via Lua script. Precision grows with √n so early evidence has outsized effect, while established beliefs resist change. The field also serves as the entrainment mechanism for CyclicDecayField — cycle parameters self-correct through observation outcomes.

## Prior Art

No prior issues or PRs found related to confidence tracking or Bayesian fields. This is greenfield work building on Steps 1-3 of the memory roadmap.

## Data Flow

1. **Entry point**: Application calls `instance.confidence.update(signal=0.9)` (corroborate) or `instance.confidence.update(signal=0.1)` (contradict)
2. **Python API**: `ConfidenceField.update()` packages signal and current evidence count, calls Lua script
3. **Lua script** (`bayesian_update.lua`): Atomically reads current `{confidence, evidence_count, corroborations, contradictions}` from a companion hash, applies precision-weighted Bayesian update, writes back
4. **Entrainment path**: `ObservationProtocol.on_context_used()` calls `_apply_acted/dismissed/contradicted` which updates ConfidenceField AND adjusts CyclicDecayField cycle parameters (phase, amplitude)
5. **Query-time integration**: `priority = decay_score × confidence` composite scoring in Lua or application layer
6. **Auto-discharge**: When confidence drops below 0.1, homeostatic pressure auto-discharges

## Architectural Impact

- **New dependencies**: None — uses existing Redis, msgpack, Lua infrastructure
- **Interface changes**: New `update()` method on ConfidenceField instances; new `confidence` companion hash key pattern; ObservationProtocol outcome handlers gain confidence update calls
- **Coupling**: Tightens coupling between ObservationProtocol and ConfidenceField (by design — entrainment IS the coupling mechanism). CyclicDecayField gains optional entrainment awareness but remains independent without ConfidenceField
- **Data ownership**: Confidence data owned by ConfidenceField companion hash, separate from CyclicDecayField cycle data
- **Reversibility**: Fully reversible — remove field type and companion hashes. No migration needed for existing data

## Appetite

**Size:** Medium

**Team:** Solo dev, PM

**Interactions:**
- PM check-ins: 1 (scope alignment on entrainment depth)
- Review rounds: 1 (code review)

## Prerequisites

No prerequisites — this work has no external dependencies. Requires Steps 1-3 (DecayingSortedField, CyclicDecayField, AccessTracker, ObservationProtocol, WriteFilter) which are already merged.

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Steps 1-3 merged | `python -c "from src.popoto.fields.cyclic_decay_field import CyclicDecayField; from src.popoto.fields.access_tracker import AccessTrackerMixin; from src.popoto.fields.observation import ObservationProtocol; from src.popoto.fields.write_filter import WriteFilterMixin; print('OK')"` | Dependencies exist |

## Solution

### Key Elements

- **ConfidenceField**: A Field subclass storing Bayesian confidence metadata in a companion Redis hash. Not a SortedField — it's a data field that tracks `{confidence, evidence_count, corroborations, contradictions}`
- **Lua script (`bayesian_update.lua`)**: Atomic read-modify-write for confidence updates with precision-weighted formula
- **Entrainment hooks**: Extensions to ObservationProtocol outcome handlers that update both content confidence and cycle parameters
- **Separate tracking**: Content confidence vs. cycle confidence are distinct — a memory can have high content confidence but low cycle confidence

### Flow

**Update signal** → `instance.confidence.update(signal=0.9)` → **Lua script** (atomic read-modify-write) → **Updated companion hash** → confidence available at query time

**Entrainment**: Agent retrieves memory → **ObservationProtocol.on_context_used()** → outcome determines effects → **ConfidenceField.update()** + **CyclicDecayField cycle adjustment** → both confidence and cycles evolve

### Technical Approach

- Store confidence metadata in a companion Redis hash keyed `$ConfF:{Model}:{field}:data` with member keys mapping to msgpack `{confidence, evidence_count, corroborations, contradictions}`
- Bayesian update formula: `new_confidence = prior + (signal - prior) / sqrt(evidence_count + 1)` — precision grows with √n
- Lua script performs atomic HGET → unpack → compute → pack → HSET
- Phase correction: `new_phase = (1 - lr) × old_phase + lr × observed_phase` where `lr = 1 / sqrt(evidence_count + 1)`
- Amplitude adjustment on entrainment: strengthen factor for acted, weaken for dismissed/contradicted
- Amplitude threshold: when cycle amplitude drops below configurable threshold (default 0.1), cycle contribution returns 0
- Auto-discharge: when content confidence < 0.1, call `resolve_pressure()` on any CyclicDecayField

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] Lua script handles missing/corrupt companion hash data gracefully (defaults to initial_confidence=0.5)
- [ ] `update()` on unsaved model raises TypeError
- [ ] Invalid signal values (outside 0-1 range) raise ValueError

### Empty/Invalid Input Handling
- [ ] `update(signal=None)` raises TypeError
- [ ] Model with ConfidenceField but no data yet returns `initial_confidence` default
- [ ] Entrainment with model lacking CyclicDecayField is a no-op (graceful degradation)

### Error State Rendering
- [ ] Not applicable — no user-visible output (ORM-level feature)

## Rabbit Holes

- **Full Bayesian inference engine**: We're doing precision-weighted point estimates, not full posterior distributions. Don't implement Beta distributions or MCMC
- **Confidence-weighted query sorting in Lua**: The Lua scoring script could multiply decay × confidence, but that couples the scripts. Keep it as application-level composition (`priority = decay_score × confidence`) for v1
- **Cycle confidence as a separate ConfidenceField instance**: Track cycle confidence as metadata within the CyclicDecayField companion hash, not as a separate ConfidenceField. Avoid the complexity of cross-field references

## Risks

### Risk 1: Lua script complexity
**Impact:** Hard to debug atomic Lua updates with multiple companion hashes
**Mitigation:** Keep `bayesian_update.lua` simple — single hash read-modify-write. Entrainment cycle updates use existing CyclicDecayField methods, not inline Lua

### Risk 2: Evidence count overflow
**Impact:** After millions of updates, √n precision makes the field effectively immutable
**Mitigation:** Document this as intentional behavior (strong priors resist change). If needed later, add an optional decay on evidence_count

## Race Conditions

No race conditions identified — the Lua script provides atomic read-modify-write within a single Redis EVAL. Multiple concurrent updates are serialized by Redis's single-threaded execution model.

## No-Gos (Out of Scope)

- Full posterior distribution tracking (Beta distributions, etc.)
- Confidence-weighted Lua scoring (keep as application-level composition)
- Confidence decay over time (evidence count doesn't expire)
- Confidence field as a SortedField (it's metadata, not a ranking dimension)
- UI or visualization of confidence scores

## Update System

No update system changes required — this is a library feature in the popoto package.

## Agent Integration

No agent integration required — this is a popoto ORM library feature. Users import and use it directly in their models.

## Documentation

### Feature Documentation
- [ ] Create `docs/fields/confidence-field.md` describing ConfidenceField usage
- [ ] Update field type index/README if one exists

### External Documentation Site
- [ ] Update MkDocs pages for the new field type
- [ ] Verify docs build passes with `mkdocs build`

### Inline Documentation
- [ ] Comprehensive docstrings on ConfidenceField class and all public methods
- [ ] Lua script documentation (comments explaining the Bayesian formula)
- [ ] Code comments on entrainment integration points in ObservationProtocol

## Success Criteria

- [ ] `ConfidenceField` class with `initial_confidence` parameter
- [ ] `update(signal)` method with Lua-backed atomic read-modify-write
- [ ] Precision-weighted Bayesian formula: `new = prior + (signal - prior) / sqrt(n + 1)`
- [ ] Companion hash stores `{confidence, evidence_count, corroborations, contradictions}`
- [ ] `confidence` property returns current confidence value (reads from companion hash)
- [ ] Entrainment: ObservationProtocol `acted` → corroborate content confidence + nudge cycle phase + strengthen amplitude
- [ ] Entrainment: ObservationProtocol `dismissed` → weaken cycle amplitude (content confidence unchanged)
- [ ] Entrainment: ObservationProtocol `contradicted` → contradict content confidence + aggressively weaken amplitude
- [ ] Separate content vs. cycle confidence tracking
- [ ] Auto-discharge pressure when confidence < 0.1
- [ ] Pipeline support in all Redis operations
- [ ] on_save/on_delete hooks for companion hash lifecycle
- [ ] Tests: Bayesian convergence (10th update moves less than 1st)
- [ ] Tests: entrainment loop (dismiss 3x → amplitude decreases)
- [ ] Tests: phase shift on acted at different time
- [ ] Tests: amplitude below threshold → cycle returns 0
- [ ] Tests: synergy with DecayingSortedField (`priority = decay_score × confidence`)
- [ ] Tests: synergy with WriteFilter (confidence < threshold → eligible for directed forgetting)
- [ ] Tests: auto-discharge pressure when confidence < 0.1
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (confidence-field)**
  - Name: confidence-builder
  - Role: Implement ConfidenceField class, Lua script, companion hash lifecycle, entrainment hooks
  - Agent Type: builder
  - Resume: true

- **Validator (confidence-field)**
  - Name: confidence-validator
  - Role: Verify Bayesian formula correctness, entrainment integration, pipeline support
  - Agent Type: validator
  - Resume: true

- **Builder (tests)**
  - Name: test-builder
  - Role: Implement comprehensive test suite for ConfidenceField
  - Agent Type: test-writer
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Create field documentation and update indexes
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Implement ConfidenceField core
- **Task ID**: build-confidence-field
- **Depends On**: none
- **Assigned To**: confidence-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/fields/confidence_field.py` with ConfidenceField class
- Implement companion hash key pattern `$ConfF:{Model}:{field}:data`
- Write `BAYESIAN_UPDATE_LUA` script for atomic read-modify-write
- Implement `update(signal)` method calling Lua script
- Implement `confidence` property for reading current value
- Implement `on_save()` to initialize companion hash with `initial_confidence`
- Implement `on_delete()` to clean up companion hash entries
- Add pipeline support to all Redis operations
- Export from `src/popoto/fields/__init__.py` and `src/popoto/__init__.py`

### 2. Implement entrainment integration
- **Task ID**: build-entrainment
- **Depends On**: build-confidence-field
- **Assigned To**: confidence-builder
- **Agent Type**: builder
- **Parallel**: false
- Extend `_apply_acted` in `observation.py`: corroborate content confidence, nudge cycle phase toward actual activation time, strengthen cycle amplitude
- Extend `_apply_dismissed` in `observation.py`: weaken cycle amplitude (leave content confidence unchanged)
- Extend `_apply_contradicted` in `observation.py`: contradict content confidence, aggressively weaken cycle amplitude
- Implement auto-discharge: when content confidence < 0.1, resolve pressure on CyclicDecayFields
- Add phase correction formula to CyclicDecayField or as utility in ConfidenceField

### 3. Validate core implementation
- **Task ID**: validate-core
- **Depends On**: build-entrainment
- **Assigned To**: confidence-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify ConfidenceField class structure and Lua script correctness
- Verify entrainment hooks in ObservationProtocol
- Check pipeline support throughout
- Run lint and format checks

### 4. Implement test suite
- **Task ID**: build-tests
- **Depends On**: validate-core
- **Assigned To**: test-builder
- **Agent Type**: test-writer
- **Parallel**: false
- Create `tests/test_confidence_field.py`
- Test: corroboration increases confidence, contradiction decreases
- Test: precision growth (10th update moves score less than 1st)
- Test: entrainment loop — dismiss 3x → amplitude decreases
- Test: phase shift on acted at different time
- Test: amplitude below threshold → cycle returns 0
- Test: synergy with DecayingSortedField (composite scoring)
- Test: synergy with WriteFilter (confidence < threshold)
- Test: auto-discharge pressure when confidence < 0.1
- Test: pipeline support
- Test: on_save/on_delete lifecycle
- Test: edge cases (unsaved model, invalid signal, missing data)

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: build-tests
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/fields/confidence-field.md`
- Update MkDocs config if needed
- Add comprehensive docstrings

### 6. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: confidence-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify all success criteria met
- Verify documentation builds
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_confidence_field.py -x -q` | exit code 0 |
| All tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check src/popoto/fields/confidence_field.py` | exit code 0 |
| Format clean | `python -m ruff format --check src/popoto/fields/confidence_field.py` | exit code 0 |
| Import works | `python -c "from src.popoto.fields.confidence_field import ConfidenceField; print('OK')"` | output contains OK |
| Docs build | `mkdocs build` | exit code 0 |

---

## Open Questions

1. **Composite scoring location**: The issue mentions `priority = decay_score × confidence` — should this be a utility function in popoto, or left entirely to the application layer? The plan currently scopes it as application-level composition.

2. **Cycle confidence storage**: The issue says "content vs cycle confidence tracked separately." The plan stores cycle confidence as metadata within the existing CyclicDecayField companion hash (alongside amplitude/phase). Is this the right location, or should cycle confidence be a separate companion hash?

3. **Amplitude threshold default**: The plan uses 0.1 as the default threshold below which cycle contribution returns 0. Is this the right default, or should it be configurable per-field?
