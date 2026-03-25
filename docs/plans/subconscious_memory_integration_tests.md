---
status: Ready
type: chore
appetite: Medium
owner: Valor
created: 2026-03-25
tracking: https://github.com/tomcounsell/popoto/issues/267
last_comment_id:
---

# SubconsciousMemory: Integration Tests for Behavioral Gaps

## Problem

The existing SubconsciousMemory test suite (`tests/test_subconscious_memory.py`) verifies plumbing -- methods run without raising, records get saved. It does not verify that the system *behaves correctly* under realistic conditions. There are 8 specific behavioral gaps identified in issue #267 where the code could be silently broken and tests would still pass.

**Current behavior:**
Tests seed 1 memory and check it appears (tautology), use mocked LLM calls, never test agent isolation, and never verify token budgets or observation feedback effects.

**Desired outcome:**
Every behavioral claim made in the SubconsciousMemory and ContextAssembler documentation is backed by at least one integration test hitting real Redis.

## Prior Art

- **PR #265**: "Add SubconsciousMemory recipe, update guide LLM examples, add verbatim tests" -- shipped the recipe and the current test file. Tests were intentionally basic to ship the recipe quickly. This plan fills the gaps left behind.

No prior issues found related to SubconsciousMemory integration testing.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

Pure test work with no production code changes. The complexity is in understanding the field interactions (DecayingSortedField decay, ConfidenceField signals, WriteFilter thresholds) and writing tests that verify them end-to-end through the SubconsciousMemory API.

## Prerequisites

No prerequisites -- this work only requires Redis (already required by all Popoto tests).

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis running | `python -c "from popoto.redis_db import POPOTO_REDIS_DB; POPOTO_REDIS_DB.ping()"` | Redis connection |

## Solution

### Key Elements

- **Test file**: New `tests/test_subconscious_memory_integration.py` to keep integration tests separate from existing unit-level tests
- **Shared fixtures**: SCMemory model with WriteFilterMixin + AccessTrackerMixin + DecayingSortedField + ConfidenceField (same as existing tests, extended as needed)
- **8 test classes**: One per behavioral gap from the issue

### Technical Approach

All tests use real Redis, no mocking of Popoto internals. Tests clean up after themselves using the existing `_clean_keys` pattern.

#### Gap 1: Retrieval Relevance
- Seed 20+ memories across diverse topics (deployment, databases, auth, frontend, etc.)
- Query with a specific topic cue
- Assert that top-ranked results are topically relevant and irrelevant memories are absent or ranked low
- Uses `inject_context` which delegates to `ContextAssembler.assemble()` with `CompositeScoreQuery`

#### Gap 2: Agent Isolation
- Create memories for agent-1 and agent-2
- Verify `inject_context` for agent-1 only surfaces agent-1 memories
- Verify agent-2 memories are invisible (not in assembly records)
- The partition is enforced by `KeyField` on `agent_id` and the `filter(**filters)` in `_pull_path`

#### Gap 3: Multi-turn Accumulation
- Run 5+ inject-extract-inject cycles
- Track memory count growth after each cycle
- Verify that DecayingSortedField decay causes older memories to rank lower over time (use `touch` timing)
- Verify no echo-chamber: re-extracted content from injected context does not dominate rankings

#### Gap 4: Observation Feedback Loop
- Save a memory, inject it, then report "acted" outcome
- Read back ConfidenceField value -- it should increase (ACTED_CONFIDENCE_SIGNAL > 0.5)
- Save another memory, inject, report "contradicted"
- Read back ConfidenceField -- it should decrease (CONTRADICTED_CONFIDENCE_SIGNAL < 0.5)
- Run `inject_context` again and verify ranking order changed accordingly

#### Gap 5: Token Budget Enforcement
- Seed many memories with long content (each ~500 chars)
- Set `max_tokens=4000` on SubconsciousMemory
- Call `inject_context` and measure the total token count in `assembly.metadata["token_count"]`
- Assert it stays within budget

#### Gap 6: Extraction Heuristic Edge Cases
- Test `_split_sentences` directly with:
  - Markdown bullet lists (`"- item one\n- item two"`)
  - Code blocks with periods (`"Call foo.bar(). Then check."`)
  - URLs (`"Visit https://example.com. Next sentence."`)
  - Decimal numbers (`"Version 3.14 was released. It includes fixes."`)
- These test the regex `(?<=[.!?])\s+` behavior on real-world LLM output patterns

#### Gap 7: Configurable Field Names
- Define an alternative model with `text` instead of `content`, `score` instead of `importance`, `owner` instead of `agent_id`
- Create SubconsciousMemory with `content_field="text"`, `importance_field="score"`, `agent_id_field="owner"`
- Verify extract_memories saves to the correct fields and inject_context retrieves properly

#### Gap 8: WriteFilter Boundary Behavior
- Test exact-at-threshold: importance == `_wf_min_threshold` (0.2) should save
- Test just-below: importance = 0.19 should be filtered
- Test priority threshold: importance >= `_wf_priority_threshold` (0.7) should be tagged as priority
- Test just-below priority: importance = 0.69 should save but not be priority

## Failure Path Test Strategy

### Exception Handling Coverage
- SubconsciousMemory wraps all operations in try/except (lines 170-172, 232-233, 262-265 in subconscious_memory.py). The integration tests verify observable outcomes (memory saved or not, ranking changed or not) rather than testing exception handlers directly.
- No new exception handlers are being added.

### Empty/Invalid Input Handling
- [ ] Gap 6 tests cover edge case inputs to `_split_sentences`
- [ ] Existing tests already cover empty/None inputs to `extract_memories` and `inject_context`

### Error State Rendering
- No user-visible output -- these are programmatic API tests.

## Test Impact

No existing tests affected -- this is purely additive work creating a new test file. The existing `tests/test_subconscious_memory.py` remains unchanged.

## Rabbit Holes

- **LLM-based relevance scoring**: Do not attempt to use an LLM to judge retrieval quality. Use deterministic content patterns (keyword overlap) that make relevance testable without external APIs.
- **Time-based decay testing**: Do not try to manipulate real time or sleep() to test DecayingSortedField decay. Instead, use Redis ZADD to directly set scores and verify ranking behavior.
- **Exhaustive sentence splitting**: The `_split_sentences` regex is intentionally simple. Do not try to make it handle every edge case -- just document the known failure modes.

## Risks

### Risk 1: Redis state leakage between tests
**Impact:** Flaky tests if cleanup is incomplete
**Mitigation:** Use the existing `_clean_keys` pattern with `autouse=True` fixture. Each test class uses unique agent_id prefixes to avoid collisions.

### Risk 2: CompositeScoreQuery behavior may not match expected relevance
**Impact:** Retrieval relevance test could be brittle
**Mitigation:** Use content patterns with clear semantic separation (e.g., "database connection pooling" vs "CSS grid layout") so that even simple scoring separates them reliably.

## Race Conditions

No race conditions identified -- all tests are synchronous single-threaded operations against a local Redis instance.

## No-Gos (Out of Scope)

- No changes to production code (SubconsciousMemory, ContextAssembler, or any fields)
- No LLM API calls in tests -- all tests use deterministic data
- No performance benchmarks -- this is correctness testing only
- No testing of CyclicDecayField push-path (the SCMemory model does not include CyclicDecayField)

## Update System

No update system changes required -- this is a test-only change in the Popoto library.

## Agent Integration

No agent integration required -- this is a test-only change in the Popoto library (not the AI agent system).

## Documentation

- [ ] No new feature documentation needed -- this adds tests for an existing, already-documented feature
- [ ] Update `tests/test_subconscious_memory.py` module docstring to reference the new integration test file

## Success Criteria

- [ ] Each of the 8 gaps from issue #267 has at least one dedicated test
- [ ] All tests hit real Redis (no mocking of Popoto internals)
- [ ] All tests pass: `pytest tests/test_subconscious_memory_integration.py -v`
- [ ] Existing tests still pass: `pytest tests/test_subconscious_memory.py -v`
- [ ] Tests pass (`/do-test`)

## Team Orchestration

### Team Members

- **Builder (tests)**
  - Name: test-builder
  - Role: Write all 8 test classes in the new integration test file
  - Agent Type: test-engineer
  - Resume: true

- **Validator (tests)**
  - Name: test-validator
  - Role: Verify all tests pass and cover the specified gaps
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Create integration test file with shared fixtures
- **Task ID**: build-test-scaffolding
- **Depends On**: none
- **Validates**: `pytest tests/test_subconscious_memory_integration.py --collect-only` succeeds
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: true
- Create `tests/test_subconscious_memory_integration.py` with SCMemory model, clean_redis fixture, SubconsciousMemory fixture
- Create alternative model class for Gap 7 (configurable field names)

### 2. Implement Gap 1-4 tests (retrieval, isolation, accumulation, feedback)
- **Task ID**: build-tests-1-4
- **Depends On**: build-test-scaffolding
- **Validates**: `pytest tests/test_subconscious_memory_integration.py -k "Relevance or Isolation or Accumulation or Feedback" -v`
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- TestRetrievalRelevance: seed 20+ diverse memories, verify topical relevance of inject_context results
- TestAgentIsolation: two agents, verify cross-agent invisibility
- TestMultiTurnAccumulation: 5+ inject-extract cycles, verify growth and decay ranking
- TestObservationFeedback: acted boosts confidence, contradicted penalizes, verify ranking change

### 3. Implement Gap 5-8 tests (budget, extraction, fields, WriteFilter)
- **Task ID**: build-tests-5-8
- **Depends On**: build-test-scaffolding
- **Validates**: `pytest tests/test_subconscious_memory_integration.py -k "Budget or Extraction or Configurable or WriteFilter" -v`
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- TestTokenBudgetEnforcement: many long memories, verify token count respects max_tokens
- TestExtractionEdgeCases: markdown bullets, code blocks, URLs, decimal numbers
- TestConfigurableFieldNames: alternative model with non-default field names
- TestWriteFilterBoundary: exact threshold, below threshold, priority threshold, below priority

### 4. Validate all tests pass
- **Task ID**: validate-all
- **Depends On**: build-tests-1-4, build-tests-5-8
- **Assigned To**: test-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_subconscious_memory_integration.py -v` -- all pass
- Run `pytest tests/test_subconscious_memory.py -v` -- existing tests unaffected
- Run `python -m ruff check tests/test_subconscious_memory_integration.py` -- lint clean
- Verify each gap from issue #267 has at least one test

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Integration tests pass | `pytest tests/test_subconscious_memory_integration.py -v` | exit code 0 |
| Existing tests pass | `pytest tests/test_subconscious_memory.py -v` | exit code 0 |
| Lint clean | `python -m ruff check tests/test_subconscious_memory_integration.py` | exit code 0 |
| Format clean | `python -m ruff format --check tests/test_subconscious_memory_integration.py` | exit code 0 |
| All 8 gaps covered | `grep -c "class Test" tests/test_subconscious_memory_integration.py` | output > 7 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| CONCERN | [agent-type] | [The concern raised] | [How/whether it was addressed] |
