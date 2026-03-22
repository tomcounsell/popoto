---
status: Ready
type: chore
appetite: Medium
owner: Tom
created: 2026-03-22
tracking: https://github.com/tomcounsell/popoto/issues/254
last_comment_id:
---

# Agent Memory DX: Validate, Redesign, and Ship a Quickstart Guide

## Problem

All 14 agent-memory primitives are shipped (v1.0.3). But the developer experience hasn't been validated end-to-end. The old DX plan (`docs/plans/dx-best-practices.md`) was written *before* the build — it contains broken imports, incorrect method signatures, and missing patterns that emerged during implementation.

**Current behavior:**
A developer wanting to adopt Popoto Agent Memory has no validated entry point. The feature doc (`docs/features/agent-memory.md`) is comprehensive but overwhelming — 1400+ lines covering all 14 primitives. The old DX plan has broken code. There's no progressive adoption path.

**Desired outcome:**
A quickstart guide at `docs/guides/agent-memory-quickstart.md` that a developer can follow from zero to working agent memory. Validated code snippets. Clear adoption ladder. Updated priming command reflecting the completed state.

## Prior Art

No prior quickstart or DX issues in this repo. The old DX plan (`docs/plans/dx-best-practices.md`) is the closest prior art — it's a pre-build design document that served as a guidepost but is now partially outdated.

- **PR #249**: Docs audit — fixed 2 incorrect import paths (shows import paths have been a pain point)
- **PR #253**: Consolidate constants, graduate docs — moved roadmap/research from `docs/references/` to `docs/guides/`, created `Defaults` class

## Spike Results

### spike-1: Validate old DX plan imports against shipped API
- **Assumption**: "Code snippets in dx-best-practices.md use correct import paths"
- **Method**: code-read
- **Finding**: `from popoto.fields import DecayingSortedField` FAILS — `popoto.fields.__init__.py` is empty. Correct path is `from popoto import DecayingSortedField`. All top-level imports work.
- **Confidence**: high
- **Impact on plan**: The quickstart must use `from popoto import ...` for everything. The old DX plan's import map (section 8) is wrong.

### spike-2: Validate resolve_pressure() usage
- **Assumption**: "resolve_pressure() returns a comparable value for filtering"
- **Method**: code-read
- **Finding**: `resolve_pressure(field_name, pipeline=None)` is a Model instance method that modifies state (resets pressure timestamp). It does NOT return a float. The old plan's `d.urgency.resolve_pressure() > 3.0` is completely wrong.
- **Confidence**: high
- **Impact on plan**: Quickstart must show correct usage pattern. Need to document how to *read* pressure vs. *resolve* it.

### spike-3: Check existing integration test coverage
- **Assumption**: "We need to write integration tests from scratch"
- **Method**: code-read
- **Finding**: `tests/test_agent_memory_e2e.py` already exists with multi-primitive composition tests. 16 individual primitive test files exist. `tests/test_dx_polish.py` also exists. Gap: no "adoption ladder" style tests that show progressive model enhancement.
- **Confidence**: high
- **Impact on plan**: Integration tests should be *additive* — adoption-ladder models, not duplicating existing e2e tests.

### spike-4: Check priming command staleness
- **Assumption**: "The priming command reflects current state"
- **Method**: code-read
- **Finding**: `.claude/commands/prime-agent-memory.md` says "12 of 14 primitives have shipped" and lists PolicyCache + ContextAssembler as "remaining work". Both shipped in PRs #239 and #233. References `docs/references/` paths that moved to `docs/guides/`. The priming command is stale.
- **Confidence**: high
- **Impact on plan**: Priming command update is added as a deliverable.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (adoption ladder review)
- Review rounds: 1

## Prerequisites

No prerequisites — this work has no external dependencies. Requires Redis on localhost:6379 for test validation.

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis running | `redis-cli ping` | Integration tests need Redis |

## Solution

### Key Elements

- **Validation report**: Run every snippet from the old DX plan against the shipped API, document what works/breaks/changed
- **Adoption ladder**: 5-level progressive adoption path with a model at each level, each independently runnable
- **Quickstart guide**: `docs/guides/agent-memory-quickstart.md` — the validated output of the above
- **Priming command update**: `.claude/commands/prime-agent-memory.md` — reflect all 14 shipped, correct file paths, remove "remaining work"
- **Adoption-ladder integration tests**: `tests/test_adoption_ladder.py` — one test per adoption level proving the DX works

### Flow

**Developer discovers Popoto** → Reads quickstart guide → **Recall** (DecayingSortedField) → time-weighted retrieval works → **Attention** (AccessTracker + WriteFilter) → noise filtered, reads tracked → **Learning** (Confidence + Observation) → outcomes strengthen/weaken beliefs → **Association** (CompositeScore + CoOccurrence) → multi-factor + graph retrieval → **Cognition** (ContextAssembler) → full LLM-ready context assembly

### Technical Approach

- Validate old DX plan snippets by reading code, not running them (faster, no test infrastructure)
- Build adoption ladder models in the test file first — if the tests are clean and readable, the guide writes itself
- Quickstart guide uses the same models from the tests (single source of truth)
- Priming command rewrite is mechanical — update counts, paths, remove stale sections
- No new API changes — this is pure documentation and validation

## Failure Path Test Strategy

### Exception Handling Coverage
- No exception handlers in scope — this is documentation and test work

### Empty/Invalid Input Handling
- [ ] Quickstart examples must handle empty query results gracefully (show `if not memories: return`)
- [ ] WriteFilter examples must demonstrate the silent discard behavior explicitly

### Error State Rendering
- Not applicable — no user-visible UI

## Test Impact

No existing tests affected — this is purely additive. New test file `tests/test_adoption_ladder.py` validates the quickstart guide's code examples.

## Rabbit Holes

- **Framework-specific integration examples (PydanticAI, Claude SDK)**: The old plan had these. Don't reproduce them in the quickstart — Popoto is framework-agnostic. Framework examples belong in a separate guide later, if ever.
- **Async API variants**: The issue explicitly lists this as a non-goal. Don't add async examples.
- **Rewriting the feature overview**: `docs/features/agent-memory.md` is comprehensive and correct. Don't touch it — the quickstart links to it for depth.
- **Fixing the old DX plan**: Delete `docs/plans/dx-best-practices.md` — it's outdated and misleading. The quickstart supersedes it.

## Risks

### Risk 1: Quickstart becomes another comprehensive reference
**Impact:** Developers bounce off a 1000-line "quickstart" the same way they bounce off the feature overview
**Mitigation:** Hard cap: quickstart guide must be under 300 lines. Each adoption level gets ~50 lines. Link to feature docs for depth.

### Risk 2: Adoption ladder levels feel arbitrary
**Impact:** Developers don't see why they'd adopt Level 2 before Level 3
**Mitigation:** Each level must have a clear "what you get" that the previous level can't do. Tests prove each level is independently useful.

## Race Conditions

No race conditions identified — all operations are synchronous, single-threaded documentation and test work.

## No-Gos (Out of Scope)

- No new field types or API changes
- No framework adapters or middleware
- No async API additions
- No changes to `docs/features/agent-memory.md` (it's correct as-is)

## Update System

No update system changes required — this is documentation and tests only.

## Agent Integration

No agent integration required — this is an ORM library, not an agent system.

## Documentation

### Feature Documentation
- [ ] Create `docs/guides/agent-memory-quickstart.md` — the primary deliverable
- [ ] Link quickstart from `docs/features/agent-memory.md` (add "Getting Started" link at top)
- [ ] Link quickstart from repo README
- [ ] Cross-reference quickstart ↔ tuning guide, policy cache recipe, feature overview
- [ ] Delete outdated `docs/plans/dx-best-practices.md`

### Inline Documentation
- [ ] Update `.claude/commands/prime-agent-memory.md` to reflect all 14 shipped primitives

## Success Criteria

- [ ] Every code snippet in the quickstart guide runs without import errors against the shipped API
- [ ] Adoption ladder tests pass: `pytest tests/test_adoption_ladder.py -v`
- [ ] Quickstart guide is under 300 lines
- [ ] Each adoption level has a working model + query + assertion in the test file
- [ ] Priming command correctly states all 14 primitives shipped, references correct file paths
- [ ] No broken imports (`from popoto.fields import X` patterns eliminated)
- [ ] Tests pass (`/do-test`)

## Team Orchestration

### Team Members

- **Builder (quickstart)**
  - Name: dx-builder
  - Role: Validate old DX plan, write adoption ladder tests, write quickstart guide, update priming command
  - Agent Type: builder
  - Resume: true

- **Validator (quickstart)**
  - Name: dx-validator
  - Role: Run tests, verify import paths, check line count, validate guide accuracy
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Validate old DX plan against shipped API
- **Task ID**: build-validation
- **Depends On**: none
- **Assigned To**: dx-builder
- **Agent Type**: builder
- **Parallel**: true
- Read every code snippet in `docs/plans/dx-best-practices.md`
- Cross-reference each import, method call, and pattern against the shipped source
- Produce a validation summary (inline comments in the plan are sufficient — no separate report file needed)
- Key known issues: `from popoto.fields import` is broken, `resolve_pressure()` usage is wrong

### 2. Write adoption ladder integration tests
- **Task ID**: build-tests
- **Depends On**: build-validation
- **Validates**: tests/test_adoption_ladder.py
- **Assigned To**: dx-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_adoption_ladder.py` with 5 model definitions (one per adoption level)
- **Recall**: `DecayingSortedField` only — test `top_by_decay()` ordering
- **Attention**: + `AccessTrackerMixin` + `WriteFilterMixin` — test tracking, noise filtering
- **Learning**: + `ConfidenceField` + `ObservationProtocol` — test outcome effects
- **Association**: + `CompositeScoreQuery` + `CoOccurrenceField` — test multi-factor ranking
- **Cognition**: + `ContextAssembler` — test assembled output
- Each level's model must be backward-compatible (adding fields doesn't break existing queries)

### 3. Validate tests pass
- **Task ID**: validate-tests
- **Depends On**: build-tests
- **Assigned To**: dx-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_adoption_ladder.py -v`
- Verify all levels pass
- Verify imports use `from popoto import ...` (not `from popoto.fields import ...`)

### 4. Write quickstart guide
- **Task ID**: build-guide
- **Depends On**: validate-tests
- **Assigned To**: dx-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `docs/guides/agent-memory-quickstart.md`
- Structure: intro → adoption ladder (5 levels) → what's next links
- Code snippets pulled from the validated test models
- Under 300 lines total
- Link to `docs/features/agent-memory.md` for comprehensive reference
- Link to `docs/guides/tuning-magic-numbers.md` for parameter tuning

### 5. Update priming command
- **Task ID**: build-priming
- **Depends On**: none
- **Assigned To**: dx-builder
- **Agent Type**: builder
- **Parallel**: true (independent of guide work)
- Update `.claude/commands/prime-agent-memory.md`:
  - Change "12 of 14 primitives have shipped" → "All 14 primitives have shipped"
  - Add PolicyCache (`src/popoto/recipes/policy_cache.py`) and ContextAssembler (`src/popoto/recipes/context_assembler.py`) to shipped list
  - Remove "Remaining work (2 primitives)" section
  - Fix `docs/references/` paths → `docs/guides/` paths
  - Add quickstart guide to Step 1 or as new Step 0
  - Update "Downstream consumer" section if needed

### 6. Cross-reference and cleanup
- **Task ID**: build-links
- **Depends On**: build-guide
- **Assigned To**: dx-builder
- **Agent Type**: builder
- **Parallel**: false
- Add "Getting Started" link at top of `docs/features/agent-memory.md` pointing to quickstart
- Add quickstart link to repo README
- Cross-reference quickstart from `docs/guides/tuning-magic-numbers.md` and `docs/guides/policy-cache-recipe.md`
- Cross-reference from quickstart back to feature docs, tuning guide, and policy cache recipe
- Delete `docs/plans/dx-best-practices.md` (superseded, outdated, misleading)

### 7. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-guide, build-priming, build-links, validate-tests
- **Assigned To**: dx-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify quickstart guide is under 300 lines: `wc -l docs/guides/agent-memory-quickstart.md`
- Verify no `from popoto.fields import` in quickstart: `grep "from popoto.fields import" docs/guides/agent-memory-quickstart.md`
- Verify priming command has no "remaining work" language
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_adoption_ladder.py -v` | exit code 0 |
| All tests pass | `pytest tests/ -x -q` | exit code 0 |
| Guide under 300 lines | `wc -l < docs/guides/agent-memory-quickstart.md` | output < 300 |
| No broken imports in guide | `grep -c "from popoto.fields import" docs/guides/agent-memory-quickstart.md` | output contains 0 |
| Priming command updated | `grep -c "Remaining work" .claude/commands/prime-agent-memory.md` | output contains 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |

## Resolved Questions

1. **Adoption ladder naming**: Named levels loosely mapping to cognitive science — **Recall → Attention → Learning → Association → Cognition**
2. **Where to link the quickstart**: Yes — README, feature docs, and cross-references throughout all guides
3. **Old DX plan disposition**: Delete `docs/plans/dx-best-practices.md` — it's outdated and misleading
