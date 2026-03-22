---
status: Shipped
type: feature
appetite: Medium
owner: Valor
created: 2026-03-14
tracking: https://github.com/tomcounsell/popoto/issues/208
last_comment_id:
---

# WriteFilterMixin — Selective Encoding Gate for Persistence

## Problem

When AI agents persist every observation, the Redis index fills with noise — low-value records that degrade retrieval precision and waste storage.

**Current behavior:**
Every `save()` call persists the record unconditionally. Application developers have no ORM-level mechanism to gate persistence based on record quality or importance.

**Desired outcome:**
A mixin that evaluates a scoring function in the `save()` path, silently discarding low-value records and tagging high-value records in a priority sorted set for preferential retrieval.

## Prior Art

No prior issues or PRs found related to write filtering or save gating in this repository. This is greenfield work defined as Step 3 of the [Agent Memory Roadmap](https://github.com/tomcounsell/popoto/blob/main/docs/references/popoto-memory-roadmap.md).

Related shipped work (Steps 1-2):
- **DecayingSortedField** (`src/popoto/fields/decaying_sorted_field.py`) — time-decay scoring on sorted sets
- **CyclicDecayField** (`src/popoto/fields/cyclic_decay_field.py`) — periodic resonance scoring
- **AccessTrackerMixin** (`src/popoto/fields/access_tracker.py`) — read pattern tracking with staged/confirmed pattern
- **ObservationProtocol** (`src/popoto/fields/observation.py`) — outcome-driven memory effects

## Data Flow

1. **Entry point**: Application calls `instance.save()` on a model using `WriteFilterMixin`
2. **Score computation**: `save()` calls `self.compute_filter_score()` (abstract, implemented by subclass)
3. **Gate decision**: Score compared against `min_threshold` (0.2) and `priority_threshold` (0.7)
   - Score < min_threshold → `SkipSaveException` raised internally → `save()` catches it and returns without persisting
   - Score >= min_threshold and < priority_threshold → normal save proceeds
   - Score >= priority_threshold → normal save proceeds AND `ZADD` to `$WF:{ClassName}:priority` sorted set
4. **Output**: Record either persisted (with optional priority tagging) or silently discarded

## Architectural Impact

- **New dependencies**: None — pure Redis operations (ZADD, ZREM)
- **Interface changes**: New `SkipSaveException` in `exceptions.py`; new abstract method pattern `compute_filter_score()` on the mixin
- **Coupling**: Low — the mixin intercepts `save()` via a hook method, does not modify `Model.save()` itself. Uses the same mixin pattern as `AccessTrackerMixin`.
- **Data ownership**: Adds a new key namespace `$WF:{ClassName}:priority` owned by the mixin
- **Reversibility**: Fully reversible — removing the mixin from a model class restores default save behavior. Priority sorted set keys can be cleaned up with a single `DEL` pattern.

## Appetite

**Size:** Medium

**Team:** Solo dev, PM

**Interactions:**
- PM check-ins: 1 (scope alignment on the hook mechanism)
- Review rounds: 1 (code review)

## Prerequisites

No prerequisites — this work has no external dependencies. Steps 1-2 (DecayingSortedField, AccessTrackerMixin) are already shipped.

## Solution

### Key Elements

- **SkipSaveException**: New exception in `exceptions.py` that `save()` catches to silently abort persistence
- **WriteFilterMixin**: Mixin class with configurable thresholds, abstract `compute_filter_score()`, and an `on_save` hook
- **Priority sorted set**: `$WF:{ClassName}:priority` stores PKs of high-value records scored by their filter score

### Flow

**Application save** → `compute_filter_score()` → **Gate decision** → [below threshold: discard] | [above threshold: persist] → [above priority threshold: ZADD to priority set]

### Technical Approach

- **Hook placement**: The mixin overrides a method that runs early in the `save()` path. The cleanest approach is to override `pre_save()` — if the score is below threshold, raise `SkipSaveException`. The `save()` method catches this exception and returns a sentinel value (e.g., `False` or `0`).
- **Exception-based gating**: `SkipSaveException` is the cleanest pattern because `pre_save()` already returns early on validation failures. The exception cleanly short-circuits the entire save pipeline including field hooks.
- **Priority tagging in on_save hook**: After a successful save, the mixin checks if the score exceeds `priority_threshold` and issues `ZADD`. This happens after persistence, not before, so the record is guaranteed to exist.
- **Pipeline support**: Both the gate check and priority ZADD accept an optional `pipeline` parameter, consistent with all other Popoto patterns.
- **Score caching**: The score computed during `pre_save` is cached on `self._write_filter_score` so the post-save priority check doesn't recompute.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `SkipSaveException` must be caught by `save()` and NOT propagate to the caller
- [ ] Verify that when `compute_filter_score()` raises an unexpected exception (not SkipSaveException), it propagates normally — don't swallow real errors

### Empty/Invalid Input Handling
- [ ] `compute_filter_score()` returning `None` → treat as below threshold (discard)
- [ ] `compute_filter_score()` returning negative value → treat as below threshold
- [ ] `compute_filter_score()` returning value > 1.0 → treat as above priority threshold
- [ ] Model with no fields set → scoring function handles gracefully

### Error State Rendering
- [ ] Not applicable — no user-visible output. All behavior is observable via Redis key existence.

## Rabbit Holes

- **Scoring function complexity**: The scoring function is application-layer. Popoto provides the gate, not the intelligence. Don't build any default scoring logic.
- **Retroactive filtering**: Don't build tooling to retroactively filter already-persisted records. That's a separate compaction concern (Step 10 in roadmap).
- **Priority queue consumption**: Don't build consumers for the priority sorted set. Downstream tools (ContextAssembler, Step 9) will read it.
- **Async scoring**: Don't add async support for `compute_filter_score()`. Keep it synchronous like all other Popoto hooks.

## Risks

### Risk 1: Save method complexity
**Impact:** The `save()` method in `base.py` is already complex (~350 lines with two paths: pipeline and non-pipeline). Adding exception handling increases maintenance burden.
**Mitigation:** Keep the mixin's interception point in `pre_save()` only. The `save()` method just needs a try/except around the `pre_save()` call, which is a minimal change.

### Risk 2: Pipeline mode incompatibility
**Impact:** In pipeline mode, `save()` queues commands but doesn't execute. A SkipSaveException mid-pipeline could leave the pipeline in an inconsistent state.
**Mitigation:** The gate check happens BEFORE any pipeline commands are queued (in `pre_save`). If skipped, no commands are added to the pipeline. Return the pipeline unchanged.

## Race Conditions

No race conditions identified. The write filter is evaluated synchronously within the save call. The priority ZADD is atomic. No cross-process state is shared — each save evaluates independently.

## No-Gos (Out of Scope)

- Retroactive filtering of existing records
- Default scoring implementations (application-layer concern)
- Priority set consumers or query methods (future ContextAssembler work)
- Async support for the scoring function
- Configurable key namespace prefix (use the `$WF:` convention)

## Update System

No update system changes required — this is a library feature in the popoto package. Updates propagate via package version bumps.

## Agent Integration

No agent integration required — this is a pure ORM primitive in the popoto library. No MCP servers, bridge changes, or tool wrappers needed.

## Documentation

### Inline Documentation
- [ ] Comprehensive docstrings on `WriteFilterMixin`, `SkipSaveException`, and `compute_filter_score()`
- [ ] Code comments explaining the gate mechanism in `save()`

### External Documentation
- [ ] Update roadmap doc to mark Step 3 as shipped
- [ ] Add usage example in the mixin's module docstring (matching AccessTrackerMixin pattern)

## Success Criteria

- [ ] `WriteFilterMixin` with configurable `min_threshold` and `priority_threshold`
- [ ] `SkipSaveException` cleanly aborts save without error
- [ ] Priority sorted set maintained via post-save hook
- [ ] `compute_filter_score()` abstract method pattern (raises NotImplementedError)
- [ ] Pipeline support for both gate and priority tagging
- [ ] Tests: record with score < 0.2 is not persisted
- [ ] Tests: record with score >= 0.2 and < 0.7 is persisted normally
- [ ] Tests: record with score >= 0.7 is persisted AND added to priority sorted set
- [ ] Tests: boundary conditions (exactly 0.2, exactly 0.7)
- [ ] Tests: synergy with DecayingSortedField (filtered-out records never in sorted set)
- [ ] Tests: synergy with AccessTrackerMixin (priority-tagged records track access identically)
- [ ] Tests: pipeline mode works correctly
- [ ] Tests pass (`/do-test`)
- [ ] Lint and format clean

## Team Orchestration

### Team Members

- **Builder (write-filter)**
  - Name: write-filter-builder
  - Role: Implement WriteFilterMixin, SkipSaveException, and save() integration
  - Agent Type: builder
  - Resume: true

- **Builder (tests)**
  - Name: test-builder
  - Role: Implement comprehensive test suite
  - Agent Type: test-writer
  - Resume: true

- **Validator (all)**
  - Name: write-filter-validator
  - Role: Verify implementation meets all success criteria
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Update docstrings and roadmap
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Add SkipSaveException
- **Task ID**: build-exception
- **Depends On**: none
- **Assigned To**: write-filter-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `SkipSaveException` to `src/popoto/exceptions.py`
- Export it from `src/popoto/__init__.py`

### 2. Implement WriteFilterMixin
- **Task ID**: build-mixin
- **Depends On**: build-exception
- **Assigned To**: write-filter-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `src/popoto/fields/write_filter.py` with `WriteFilterMixin`
- Implement: `_wf_min_threshold`, `_wf_priority_threshold` (underscore-prefixed per metaclass convention)
- Implement: `compute_filter_score()` raising `NotImplementedError`
- Implement: `_wf_key(kind)` for Redis key building (`$WF:{ClassName}:priority`)
- Implement: `_check_write_filter()` that computes score and raises `SkipSaveException` if below threshold
- Implement: `_tag_priority(pipeline=None)` that does ZADD if score >= priority threshold
- Cache score on `self._write_filter_score` during check

### 3. Integrate with Model.save()
- **Task ID**: build-save-integration
- **Depends On**: build-mixin
- **Assigned To**: write-filter-builder
- **Agent Type**: builder
- **Parallel**: false
- In `Model.save()`, wrap `pre_save()` call in try/except for `SkipSaveException`
- On catch: return `pipeline` (if pipeline mode) or `False` (if non-pipeline mode)
- After successful save (before return), check if instance has `_tag_priority` and call it
- Export `WriteFilterMixin` from `src/popoto/__init__.py` and add to `__all__`

### 4. Write tests
- **Task ID**: build-tests
- **Depends On**: build-save-integration
- **Assigned To**: test-builder
- **Agent Type**: test-writer
- **Parallel**: false
- Create `tests/test_write_filter.py`
- Test: score below min_threshold → key doesn't exist in Redis
- Test: score between thresholds → key exists, not in priority set
- Test: score above priority_threshold → key exists AND in priority set
- Test: boundary values (exactly 0.2, exactly 0.7)
- Test: custom thresholds work
- Test: pipeline mode (both skip and persist paths)
- Test: SkipSaveException doesn't propagate to caller
- Test: compute_filter_score() not implemented → NotImplementedError
- Test: synergy with DecayingSortedField
- Test: synergy with AccessTrackerMixin
- Test: cleanup on delete removes priority set entry

### 5. Validate
- **Task ID**: validate-all
- **Depends On**: build-tests
- **Assigned To**: write-filter-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify lint and format clean
- Verify all success criteria met
- Check that existing tests still pass (no regressions)

### 6. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-all
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Verify module docstring with usage example
- Update roadmap to mark Step 3 shipped

### 7. Final Validation
- **Task ID**: validate-final
- **Depends On**: document-feature
- **Assigned To**: write-filter-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all verification checks
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_write_filter.py -x -q` | exit code 0 |
| All tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check src/popoto/fields/write_filter.py` | exit code 0 |
| Format clean | `python -m ruff format --check src/popoto/fields/write_filter.py` | exit code 0 |
| Exception exported | `python -c "from popoto import SkipSaveException"` | exit code 0 |
| Mixin exported | `python -c "from popoto import WriteFilterMixin"` | exit code 0 |

---

## Open Questions

No open questions — the issue spec is detailed and the implementation approach follows established mixin patterns (AccessTrackerMixin). Ready for review.
