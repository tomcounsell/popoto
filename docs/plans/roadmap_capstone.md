---
status: Ready
type: chore
appetite: Medium
owner: Solo dev
created: 2026-03-22
tracking: https://github.com/tomcounsell/popoto/issues/251
last_comment_id:
---

# Roadmap Capstone: Consolidate Constants, Graduate Docs, Prepare for Experiments

## Problem

All 14 agent-memory primitives are shipped and constants have been validated via parameter sweep (#234, PR #250). Three friction points remain before the repo is ready for bivariate experiments on constants:

**Current behavior:**
- ~19 Category 1 tuning constants are scattered across 7 files using 3 different override mechanisms (module-level constants, class attributes, field constructor kwargs). Running bivariate experiments requires importing from multiple modules and using different override patterns for each.
- 10 of 14 primitives lack standalone feature docs — they're only documented in the comprehensive `agent-memory.md` guide and plan docs in `docs/plans/`. Plan docs contain implementation details and resolved questions that aren't useful to library users.
- The roadmap (`docs/guides/popoto-memory-roadmap.md`) says "Complete" but individual steps may not all show shipped status consistently.
- The benchmark harness (`tests/benchmarks/overrides.py`) only handles module-level constants via `setattr`, missing field kwargs and class attributes.

**Desired outcome:**
- All tuning constants importable from `popoto.fields.constants.Defaults` with backward-compatible fallback
- Feature docs for 10 primitives in `docs/features/` (4 existing + 6 new for complex primitives)
- Roadmap clearly marked complete
- Benchmark harness updated for centralized `Defaults`

## Prior Art

- **PR #250**: Experimental tuning benchmark harness — shipped 2026-03-22. Established the `tests/benchmarks/overrides.py` pattern with `MODULE_CONSTANTS` registry and `apply_overrides()` context manager. Currently handles 14 module-level constants but not field kwargs or class attributes.
- **Issue #234**: Plan experimental tuning for agent-memory magic numbers — closed. Defined the constant categorization (Cat 1: behavioral tuning, Cat 2: structural capacity, Cat 3: edge pruning, Cat 4: domain).

## Spike Results

### spike-1: Current constants audit
- **Assumption**: "Issue #251 lists all Category 1 constants accurately"
- **Method**: code-read
- **Finding**: Issue is mostly accurate. Full inventory: 14 module-level constants (6 in `fields/observation.py`, 6 in `recipes/policy_cache.py`, 2 in `recipes/context_assembler.py`), 5 field kwargs (decay_rate, initial_confidence, decay_factor, initial_weight, decay_per_hop), 5 class attributes (_wf_min_threshold, _wf_priority_threshold, _pl_confidence_error_threshold, _pl_confidence_low_signal, _pl_auto_resolve_errors). Issue missed 2 `recipes/policy_cache.py` constants already in the harness (CHI_SQUARED_P_THRESHOLD, INITIAL_CYCLE_AMPLITUDE) and all method parameter defaults (initial_weight, decay_per_hop).
- **Confidence**: high
- **Impact on plan**: Defaults class needs ~24 constants, not ~19. Method param defaults (initial_weight, decay_per_hop) need a different injection pattern.

### spike-2: Feature doc coverage audit
- **Assumption**: "7 primitives need new feature docs"
- **Method**: code-read
- **Finding**: 4 primitives have standalone feature docs (CyclicDecayField, ConfidenceField, CoOccurrenceField, CompositeScoreQuery). The other 10 are covered in `agent-memory.md` with substantial detail but no standalone docs. The issue says 7 need docs but the actual count depends on whether `agent-memory.md` coverage is considered sufficient.
- **Confidence**: high
- **Impact on plan**: Creating 10 standalone feature docs is excessive — most primitives have thorough coverage in `agent-memory.md`. Focus on primitives complex enough to warrant their own page.

## Architectural Impact

- **New dependencies**: None
- **Interface changes**: `Defaults` class adds a new public API surface. All existing interfaces remain unchanged (backward compatible).
- **Coupling**: Slightly increases coupling — primitives gain a dependency on `constants.py`. But this is intentional centralization of scattered state.
- **Data ownership**: No change to data ownership.
- **Reversibility**: Fully reversible — `Defaults` is additive. Removing it just means reverting to hardcoded values.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (scope alignment on which primitives need standalone feature docs)
- Review rounds: 1

## Prerequisites

No prerequisites — this work has no external dependencies.

## Solution

### Key Elements

- **Defaults class**: Central registry in `src/popoto/fields/constants.py` alongside existing `InteractionWeight` and `TemporalPeriod`. Each constant is a class attribute with a docstring. Primitives read from `Defaults` at runtime, falling back to `Defaults.X` when no explicit kwarg is passed.
- **Feature docs**: Create standalone feature docs for primitives that warrant deeper reference beyond `agent-memory.md`.
- **Roadmap update**: Mark all steps shipped, add "Complete" section.
- **Harness update**: Replace per-module `setattr` patching with `Defaults` attribute patching.

### Technical Approach

**Defaults class pattern:**

```python
class Defaults:
    """Central registry of tunable behavioral constants.

    Override any constant before model definition or at runtime:
        from popoto.fields.constants import Defaults
        Defaults.DECAY_RATE = 0.3
    """

    # DecayingSortedField
    DECAY_RATE = 0.5

    # ConfidenceField
    INITIAL_CONFIDENCE = 0.5

    # ObservationProtocol
    ACTED_CONFIDENCE_SIGNAL = 0.9
    CONTRADICTED_CONFIDENCE_SIGNAL = 0.1
    ACTED_CYCLE_STRENGTHEN_FACTOR = 1.2
    DISMISSED_CYCLE_WEAKEN_FACTOR = 0.8
    CONTRADICTED_CYCLE_WEAKEN_FACTOR = 0.5
    AUTO_DISCHARGE_CONFIDENCE_THRESHOLD = 0.1

    # WriteFilterMixin
    WF_MIN_THRESHOLD = 0.2
    WF_PRIORITY_THRESHOLD = 0.7

    # CoOccurrenceField
    CO_OCCURRENCE_DECAY_FACTOR = 0.95
    CO_OCCURRENCE_INITIAL_WEIGHT = 0.1
    CO_OCCURRENCE_DECAY_PER_HOP = 0.5

    # PredictionLedgerMixin
    PL_CONFIDENCE_ERROR_THRESHOLD = 0.7
    PL_CONFIDENCE_LOW_SIGNAL = 0.2
    PL_AUTO_RESOLVE_ACTED = 0.1
    PL_AUTO_RESOLVE_DISMISSED = 0.5
    PL_AUTO_RESOLVE_CONTRADICTED = 0.9

    # PolicyCache (recipes/policy_cache.py)
    MIN_EVENTS_FOR_CRYSTALLIZATION = 3
    WILSON_CI_THRESHOLD = 0.6
    TD_ALPHA = 0.1
    TD_GAMMA = 0.95
    CHI_SQUARED_P_THRESHOLD = 0.05
    INITIAL_CYCLE_AMPLITUDE = 0.5

    # ContextAssembler (recipes/context_assembler.py)
    COMPETITIVE_SUPPRESSION_SIGNAL = 0.3
    DEFAULT_SURFACING_THRESHOLD = 0.5
```

**Injection pattern per category:**

| Category | Current | After |
|----------|---------|-------|
| Module-level constants (`fields/observation.py`, `recipes/policy_cache.py`, `recipes/context_assembler.py`) | `ACTED_CONFIDENCE_SIGNAL = 0.9` | `ACTED_CONFIDENCE_SIGNAL = Defaults.ACTED_CONFIDENCE_SIGNAL` (assigned at import time; functions continue to read bare module-level name) |
| Field kwargs (decay_rate, initial_confidence, etc.) | `def __init__(self, decay_rate=0.5)` | `def __init__(self, decay_rate=None)` then `self.decay_rate = decay_rate if decay_rate is not None else Defaults.DECAY_RATE` |
| Class attributes (_wf_min_threshold, etc.) | `_wf_min_threshold = 0.2` | `_wf_min_threshold = Defaults.WF_MIN_THRESHOLD` (assigned at import time) |
| Method params (initial_weight, decay_per_hop) | `def link(..., initial_weight=0.1)` | `def link(..., initial_weight=None)` then use `Defaults.CO_OCCURRENCE_INITIAL_WEIGHT` as fallback |

**Critical: module-level override semantics.** Module-level constants (`fields/observation.py`, `recipes/policy_cache.py`, `recipes/context_assembler.py`) are initialized from `Defaults` at import time, but functions continue to reference them by bare name (e.g., `signal=ACTED_CONFIDENCE_SIGNAL`). This means the benchmark harness must patch **both** `Defaults.X` and the module-level alias (`setattr(observation_mod, 'ACTED_CONFIDENCE_SIGNAL', value)`) to take effect at runtime. The harness `apply_overrides()` already patches module-level names; it must additionally patch `Defaults` so that any code constructing new fields mid-test picks up the override. This dual-patch is the simplest approach that preserves backward compatibility without changing function internals.

**`None` sentinel safety:** For field kwargs (`decay_rate`, `initial_confidence`), `None` is never a valid field value, so the `None` sentinel is safe. For method params where `None` could mean "no value" (e.g., `initial_weight` in `link()`), use a private sentinel: `_UNSET = object()` as the default, with `if initial_weight is _UNSET: initial_weight = Defaults.CO_OCCURRENCE_INITIAL_WEIGHT`.

**Backward compatibility:** Explicit kwargs still override `Defaults`. Setting `Defaults.DECAY_RATE = 0.3` only affects instances that don't pass an explicit `decay_rate=` kwarg. Existing `from popoto.fields.observation import ACTED_CONFIDENCE_SIGNAL` imports continue to work (module-level names remain).

**Feature doc strategy:** Per spike-2, creating 10 standalone docs is excessive — most primitives have thorough coverage in `agent-memory.md`. Create standalone feature docs only for the 6 primitives complex enough to warrant their own page. 4 already exist (CyclicDecayField, ConfidenceField, CoOccurrenceField, CompositeScoreQuery). Create 6 new ones:

1. DecayingSortedField (foundational primitive, complex Lua scoring)
2. ObservationProtocol (multi-outcome effects matrix, tight coupling with multiple primitives)
3. ExistenceFilter + FrequencySketch (probabilistic data structures, Lua-heavy)
4. PredictionLedgerMixin (prediction-outcome lifecycle, multi-state machine)
5. PolicyCache (capstone recipe composing all primitives, already has `guides/policy-cache-recipe.md`)
6. ContextAssembler (capstone recipe, pipeline architecture)

The remaining 4 simpler primitives (AccessTrackerMixin, WriteFilterMixin, EventStreamMixin, StreamConsumer) are adequately documented in `agent-memory.md` and don't warrant standalone pages.

**Filename convention:** Use kebab-case, matching existing feature docs (e.g., `co-occurrence-field.md`, `confidence-field.md`). New files: `decaying-sorted-field.md`, `observation-protocol.md`, `existence-filter.md`, `prediction-ledger.md`, `policy-cache.md`, `context-assembler.md`.

After feature docs are created, refactor any unique content from plan docs into feature docs. For plan docs whose primitive has no standalone feature doc, refactor unique content into `agent-memory.md` instead. Then archive all 13 shipped agent-memory plan docs (see task 4 for approach).

## Failure Path Test Strategy

### Exception Handling Coverage
- No exception handlers in scope — this is a refactoring of constant locations, not behavioral changes.

### Empty/Invalid Input Handling
- `Defaults` attributes are plain floats/ints with no input validation needed. The existing `VALID_RANGES` in `overrides.py` handles boundary checking for experiments.

### Error State Rendering
- No user-visible output. This is library internals.

## Test Impact

- `tests/benchmarks/overrides.py` — UPDATE: Replace `MODULE_CONSTANTS` registry with `Defaults`-based patching
- `tests/benchmarks/test_harness.py` — UPDATE: Adjust to test new `Defaults`-based override pattern
- `tests/benchmarks/test_sweep.py` — UPDATE: May need adjustments if override mechanism changes

No existing functional tests affected — constant values remain identical, only the source location changes. All field behavior tests continue to pass because `Defaults` values match current hardcoded values exactly.

- **NEW test**: `tests/benchmarks/test_defaults_sync.py` — Drift detection test that verifies every module-level constant alias matches its corresponding `Defaults` attribute. Iterates over `MODULE_CONSTANTS` registry and asserts `getattr(mod, attr) == getattr(Defaults, attr)` for each. This catches cases where a module constant is updated but `Defaults` is not (or vice versa).

## Rabbit Holes

- **Adding validation to Defaults class**: Tempting to add range validation, type checking, or property descriptors to `Defaults`. Not worth it — the benchmark harness already has `VALID_RANGES` and `is_degenerate()`. Keep `Defaults` as plain class attributes.
- **Creating a `Defaults.reset()` method**: The test harness already uses a context manager pattern. Don't add reset logic to the class itself.
- **Refactoring constant names**: Don't rename constants in primitive files — just have them read from `Defaults`. Renaming would break any external code referencing the module-level names.
- **Category 2/3/4 constants**: Structural capacity (max_edges, _max_access_log), edge pruning, and domain constants are explicitly out of scope per issue #251.

## Risks

### Risk 1: Breaking backward compatibility with explicit kwargs
**Impact:** Users passing `DecayingSortedField(decay_rate=0.5)` must continue to work identically.
**Mitigation:** `None` sentinel pattern — kwargs default to `None`, and `Defaults.X` is only used when kwarg is `None`. Existing code with explicit values is unaffected.

### Risk 2: Import cycle with constants.py
**Impact:** If primitives import from `constants.py` and `constants.py` somehow imports from primitives, circular import.
**Mitigation:** `constants.py` already exists with `InteractionWeight` and `TemporalPeriod` — no primitive imports from it. Adding `Defaults` to the same file follows the established pattern.

## Race Conditions

No race conditions identified — all operations are synchronous class attribute reads. `Defaults` is modified at import time or test setup, never concurrently.

## No-Gos (Out of Scope)

- Changing any constant values (that's the experiments phase)
- Category 2/3/4 constants (structural capacity, edge pruning, domain)
- New primitives or features
- The downstream Behavioral Episode Memory System (separate repo)
- MkDocs site redesign or restructuring beyond adding nav entries

## Update System

No update system changes required — this is a library-internal refactoring with no deployment or service implications.

## Agent Integration

No agent integration required — Popoto is a library consumed by other projects.

## Documentation

### Feature Documentation
- [ ] Create standalone feature docs for 6 complex primitives (DecayingSortedField, ObservationProtocol, ExistenceFilter+FrequencySketch, PredictionLedgerMixin, PolicyCache, ContextAssembler)
- [ ] Add new feature doc entries to `mkdocs.yml` nav
- [ ] Update `docs/features/agent-memory.md` status table to show all 14 as Shipped

### External Documentation Site
- [ ] Add new feature docs to mkdocs.yml nav under Features
- [ ] Verify `mkdocs serve` builds cleanly

### Inline Documentation
- [ ] Docstring on `Defaults` class explaining override pattern
- [ ] Docstring on each `Defaults` attribute grouping

## Success Criteria

- [ ] All Category 1 constants importable from `popoto.fields.constants.Defaults`
- [ ] Each primitive reads its defaults from `Defaults` (backward-compatible with explicit kwargs)
- [ ] Standalone feature docs exist for 10 primitives in `docs/features/` (4 existing + 6 new)
- [ ] All 13 shipped agent-memory plan docs archived (status: Archived, redirect to feature doc or `agent-memory.md`)
- [ ] New feature docs added to `mkdocs.yml` nav
- [ ] Roadmap doc marked complete with all steps showing shipped status
- [ ] Benchmark harness (`tests/benchmarks/overrides.py`) updated to use centralized `Defaults`
- [ ] All tests pass (`pytest`)
- [ ] `agent-memory.md` status table updated

## Team Orchestration

### Team Members

- **Builder (constants)**
  - Name: constants-builder
  - Role: Create Defaults class and wire all primitives to read from it
  - Agent Type: builder
  - Resume: true

- **Builder (docs)**
  - Name: docs-builder
  - Role: Create standalone feature docs and update roadmap/mkdocs
  - Agent Type: documentarian
  - Resume: true

- **Builder (harness)**
  - Name: harness-builder
  - Role: Update benchmark harness to use Defaults
  - Agent Type: builder
  - Resume: true

- **Validator (all)**
  - Name: capstone-validator
  - Role: Verify all success criteria met
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Create Defaults class in constants.py
- **Task ID**: build-defaults
- **Depends On**: none
- **Validates**: `pytest tests/ -x -q` (all existing tests still pass)
- **Assigned To**: constants-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `Defaults` class to `src/popoto/fields/constants.py` with all ~24 Category 1 constants
- Wire `fields/observation.py` module-level constants to read from `Defaults`
- Wire `recipes/policy_cache.py` module-level constants to read from `Defaults`
- Wire `recipes/context_assembler.py` module-level constants to read from `Defaults`
- Wire `decaying_sorted_field.py` field kwargs to fall back to `Defaults`
- Wire `confidence_field.py` field kwargs to fall back to `Defaults`
- Wire `co_occurrence_field.py` field kwargs and method params to fall back to `Defaults`
- Wire `write_filter.py` class attributes to read from `Defaults`
- Wire `prediction_ledger.py` class attributes to read from `Defaults`
- Run full test suite to confirm no regressions

### 2. Update benchmark harness
- **Task ID**: build-harness
- **Depends On**: build-defaults
- **Validates**: `pytest tests/benchmarks/ -x -q`
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: false
- Update `tests/benchmarks/overrides.py` to patch `Defaults` attributes instead of per-module `setattr`
- Simplify `MODULE_CONSTANTS` registry to use `Defaults` as single source
- Update `apply_overrides()` context manager to dual-patch both `Defaults` and module aliases
- Add drift detection test (`tests/benchmarks/test_defaults_sync.py`) — verifies every module-level alias matches its `Defaults` counterpart
- Update harness tests

### 3. Create standalone feature docs for complex primitives
- **Task ID**: build-docs
- **Depends On**: none
- **Validates**: `mkdocs build` (no errors)
- **Assigned To**: docs-builder
- **Agent Type**: documentarian
- **Parallel**: true (parallel with build-defaults)
- Create 6 standalone feature docs (kebab-case filenames): `decaying-sorted-field.md`, `observation-protocol.md`, `existence-filter.md`, `prediction-ledger.md`, `policy-cache.md`, `context-assembler.md`
- Extract user-facing content from plan docs and `agent-memory.md` into each standalone doc
- Add all new feature doc entries to `mkdocs.yml` nav under an "Agent Memory" subsection within Features
- Update `agent-memory.md` status table — all 14 primitives Shipped
- Update roadmap doc — ensure all steps marked Shipped, add completion note

### 4. Archive shipped plan docs
- **Task ID**: cleanup-plans
- **Depends On**: build-docs
- **Validates**: no dangling references; archived plans have redirect headers
- **Assigned To**: docs-builder
- **Agent Type**: documentarian
- **Parallel**: false
- Run `grep -rn 'plans/' docs/ --include='*.md' | grep -v 'docs/plans/'` to find all cross-references to plan docs from other docs
- Search GitHub issues/PRs for links to plan docs: `gh search issues "docs/plans/decaying_sorted_field" --repo tomcounsell/popoto` (repeat for each plan)
- Fix cross-references in docs to point to new feature docs instead
- Refactor any unique implementation context from plan docs into corresponding feature docs (or `agent-memory.md` for primitives without standalone docs)
- Add a redirect header to each of the 13 shipped agent-memory plan docs: update frontmatter to `status: Archived` and prepend a note pointing to the corresponding feature doc or `agent-memory.md` section. Do NOT delete — external GitHub issue/PR links must remain valid
- Exclude archived plans from `mkdocs.yml` nav (they remain in the repo but not in the docs site)

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-defaults, build-harness, build-docs, cleanup-plans
- **Assigned To**: capstone-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/ -x -q` — all tests pass
- Verify `from popoto.fields.constants import Defaults` works and has all constants
- Verify backward compatibility: `DecayingSortedField(decay_rate=0.3)` still overrides `Defaults`
- Verify `mkdocs build` succeeds
- Check all success criteria

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Defaults importable | `python -c "from popoto.fields.constants import Defaults; print(Defaults.DECAY_RATE)"` | output contains 0.5 |
| MkDocs builds | `mkdocs build --strict 2>&1; echo $?` | output contains 0 |
| Backward compat | `python -c "from popoto.fields import DecayingSortedField; f = DecayingSortedField(decay_rate=0.3); assert f.decay_rate == 0.3"` | exit code 0 |

## RFC Feedback

Three specialist critics reviewed the plan. A subsequent plan critique identified additional issues. BLOCKERs were addressed inline. Remaining CONCERNs:

| Severity | Critic | Feedback | Plan Response |
|----------|--------|----------|---------------|
| BLOCKER (resolved) | code-reviewer, data-architect | Module-level constant patching has a static-copy problem — if functions read `Defaults.X` at call time, old `setattr(module, name)` patching stops working | Resolved: functions continue reading bare module-level names; harness patches both `Defaults` and module aliases. Documented in "Critical: module-level override semantics" section. |
| BLOCKER (resolved) | code-reviewer | `None` sentinel for method params could conflict where `None` has meaning | Resolved: use `_UNSET = object()` sentinel for method params; `None` is safe for field kwargs. Documented in "`None` sentinel safety" section. |
| BLOCKER (resolved) | plan-critique | `policy_cache.py` and `context_assembler.py` referenced under `fields/` but live under `recipes/` | Resolved: all references now use explicit paths (`recipes/policy_cache.py`, `recipes/context_assembler.py`) in injection table, Defaults class comments, spike findings, and task 1 subtasks. |
| CONCERN | data-architect | Mixing structural constants (`KEY_SEPARATOR`) and behavioral constants (`DECAY_RATE`) in one file | Accepted: `Defaults` is a clearly named class, not a dump of all constants. Structural constants remain as bare module-level names in `constants.py`. The two concerns are visually and semantically separated. |
| CONCERN | code-reviewer | No production-time override mechanism beyond class attribute mutation | Accepted: `Defaults` is intended for test-time tuning and application startup configuration. Thread-safe runtime mutation is out of scope — the experiments phase will evaluate whether that's needed. |
| CONCERN (resolved) | plan-critique | Plan contradicts spike-2 finding by committing to 10 new feature docs after spike said "excessive" | Resolved: reduced to 6 standalone docs for complex primitives only. Simpler primitives (AccessTrackerMixin, WriteFilterMixin, EventStreamMixin, StreamConsumer) are adequately covered in `agent-memory.md`. |
| CONCERN (resolved) | plan-critique | Deleting 13 plan docs will break external links in GitHub issues/PRs | Resolved: plan docs are now archived (status: Archived + redirect header) instead of deleted. External links remain valid. |
| CONCERN (resolved) | plan-critique | Dual-patch pattern (Defaults + module alias) is fragile with no drift detection | Resolved: added drift detection test (`test_defaults_sync.py`) to task 2 that verifies module aliases match `Defaults` values. |
| CONCERN (resolved) | plan-critique | No filename convention specified for new feature docs | Resolved: kebab-case, matching existing docs (e.g., `decaying-sorted-field.md`). Documented in "Feature doc strategy" section. |
| CONCERN | docs-specialist | mkdocs.yml nav placement for new docs not specified | Resolved: added "Agent Memory" subsection under Features in task 3. |
| CONCERN | docs-specialist | Cross-references from other docs to plan files being deleted | Resolved: added grep step + GH search to task 4 before archival. |

---

## Resolved Questions

1. **Feature doc scope**: Create standalone feature docs for 6 complex primitives (DecayingSortedField, ObservationProtocol, ExistenceFilter+FrequencySketch, PredictionLedgerMixin, PolicyCache, ContextAssembler). Simpler primitives are covered in `agent-memory.md`.

2. **Defaults naming convention**: Drop leading underscore. Use `ALL_CAPS_SNAKE_CASE` for global-like constants (which all Category 1 constants are). Example: `WF_MIN_THRESHOLD` not `_wf_min_threshold`.

3. **Plan doc lifecycle**: Refactor unique content from plan docs into feature docs or `agent-memory.md`, then archive plan docs (status: Archived + redirect). Do NOT delete — external GitHub links must remain valid.

4. **Feature doc filenames**: Kebab-case, matching existing convention (e.g., `decaying-sorted-field.md`).
