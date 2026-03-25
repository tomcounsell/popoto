---
status: In Progress
type: chore
appetite: Medium
owner: Valor Engels
created: 2026-03-25
tracking: https://github.com/tomcounsell/popoto/issues/268
---

# SubconsciousMemory: Experiment Design for Default Constant Tuning

## Problem

SubconsciousMemory ships with 8 numeric constants set by intuition. These constants control extraction quality, retrieval budgets, write filtering, confidence initialization, and scoring weights. Without structured experiments, there is no evidence the current defaults produce good behavior across realistic agent workloads.

The existing benchmark harness (shipped via PR #250 for issue #234) already covers lower-level field constants (`_wf_min_threshold`, `_wf_priority_threshold`, `initial_confidence`, decay rates, cycle factors, etc.) but does not evaluate how those constants interact at the SubconsciousMemory recipe layer, nor does it cover the recipe-specific constants (`extraction_min_length`, `max_items`, `max_tokens`, `default importance`, `score_weights`).

**This document is an experiment design proposal, not an implementation plan.** The deliverable is a structured methodology that a follow-up issue can execute.

## Constants Under Evaluation

### Group A: SubconsciousMemory-Layer Constants (New Coverage)

| # | Constant | Default | Location | Role |
|---|----------|---------|----------|------|
| 1 | `DEFAULT_EXTRACTION_MIN_LENGTH` | `10` | `subconscious_memory.py:69` | Minimum character length for a sentence to be saved as a memory |
| 2 | `max_items` | `10` | `SubconsciousMemory.__init__` | Maximum memory records injected per turn |
| 3 | `max_tokens` | `4000` | `SubconsciousMemory.__init__` | Soft token budget for injected context |
| 4 | `default importance` | `0.5` | `extract_memories()` arg | Importance score assigned to newly extracted memories |
| 5 | `score_weights` | none documented | User-provided to constructor | Weight dict for ContextAssembler composite scoring |

### Group B: Already-Benchmarked Constants (Interaction Effects at Recipe Layer)

| # | Constant | Default | Existing Coverage |
|---|----------|---------|-------------------|
| 6 | `_wf_min_threshold` | `0.2` | Tier 1 sweep in `tests/benchmarks/run_sweeps.py` |
| 7 | `_wf_priority_threshold` | `0.7` | Interaction pair with `_wf_min_threshold` |
| 8 | `initial_confidence` | `0.5` | Tier 1 sweep in `tests/benchmarks/run_sweeps.py` |

Group B constants have field-level sensitivity data. This plan focuses on measuring their **end-to-end** impact through the SubconsciousMemory recipe pipeline (inject_context -> LLM turn -> extract_memories -> report_outcomes).

## Metrics

### M1: Extraction Signal-to-Noise Ratio (for constants 1, 4)

**What it measures:** Of the sentences extracted by `extract_memories()`, what fraction are meaningful, self-contained facts versus noise (fragments, filler, repetition).

**How to measure:**
- Feed a known LLM response (fixed corpus) through `extract_memories()` with varying `extraction_min_length`.
- Ground truth: human-labeled set of "meaningful" vs "noise" sentences from the response.
- Metric: `precision = meaningful_extracted / total_extracted`, `recall = meaningful_extracted / total_meaningful`.

**Success criterion:** F1 score above 0.75 across all three scenarios. The optimal `extraction_min_length` maximizes F1 without dropping recall below 0.6.

### M2: Retrieval Precision at K (for constants 2, 3, 5, 6, 7, 8)

**What it measures:** When `inject_context()` retrieves memories, do the top-K memories actually relate to the current query?

**How to measure:**
- Pre-populate a memory store with a known corpus (tagged with relevance labels per query).
- Call `inject_context()` with test queries under varying `max_items`, `max_tokens`, and `score_weights`.
- Compare retrieved set against ground-truth relevant set.
- Metrics: precision@K, nDCG@K (reuse existing `tests/benchmarks/metrics/retrieval.py`).

**Success criterion:** nDCG@5 above 0.7 averaged across scenarios. Precision@K should not drop below 0.5 at any sweep point.

### M3: Token Budget Efficiency (for constants 2, 3)

**What it measures:** How much of the token budget is used by high-relevance memories versus wasted on low-relevance padding?

**How to measure:**
- With a fixed corpus and query set, measure `sum(relevance * tokens) / total_tokens` for the injected context.
- A perfectly efficient budget spends all tokens on the most relevant memories.
- Metric: weighted token utilization ratio.

**Success criterion:** Token utilization ratio above 0.6 (at least 60% of token budget spent on above-median relevance memories).

### M4: Importance Distribution Health (for constant 4)

**What it measures:** Does the default importance value cause all memories to cluster in a narrow band, defeating ranking?

**How to measure:**
- Run a multi-turn simulation (20+ turns) with fixed `importance=0.5` versus varied importance values.
- After N turns, measure the standard deviation of importance scores across all stored memories.
- Compare ranking differentiation: how many distinct rank positions exist in retrieval results?

**Success criterion:** Standard deviation of importance scores above 0.15 after 20 turns. Distinct rank positions should be at least 60% of stored memories.

### M5: Write Filter Pass Rate (for constants 6, 7)

**What it measures:** What percentage of extracted memories survive the write filter, and is the filtered set truly low-value?

**How to measure:**
- Extract memories from a test corpus, then check which would be blocked by `_wf_min_threshold`.
- Human-label the blocked set: what fraction were genuinely low-value?
- Metric: filter precision = `correctly_blocked / total_blocked`.

**Success criterion:** Filter precision above 0.8 (at least 80% of blocked memories are genuinely low-value). Pass rate between 40-80% (not too permissive, not too restrictive).

### M6: Score Weight Sensitivity (for constant 5)

**What it measures:** How sensitive is retrieval quality to the choice of score_weights?

**How to measure:**
- Sweep across weight configurations: pure relevance `{"relevance": 1.0}`, pure confidence `{"confidence": 1.0}`, balanced `{"relevance": 0.5, "confidence": 0.5}`, and several weighted blends.
- Measure nDCG@5 for each configuration.
- Identify whether there is a robust default or if weights are highly scenario-dependent.

**Success criterion:** Identify a default weight configuration that achieves nDCG@5 within 90% of the scenario-specific optimal across all test scenarios.

## Experiment Design

### Experiment 1: Extraction Min Length Sweep

**Constant:** `DEFAULT_EXTRACTION_MIN_LENGTH`
**Controlled setup:** 3 fixed LLM response texts (one per scenario), each ~500 words with human-labeled sentence boundaries and relevance tags.
**Sweep values:** [5, 8, 10, 15, 20, 30, 50, 80]
**Measure:** M1 (extraction signal-to-noise)
**Judge:** Automated against human-labeled ground truth. No LLM call needed.

### Experiment 2: Max Items Sweep

**Constant:** `max_items`
**Controlled setup:** Memory store with 50 pre-loaded memories per scenario. 10 test queries with known relevant sets.
**Sweep values:** [3, 5, 7, 10, 15, 20, 30]
**Measure:** M2 (retrieval precision@K), M3 (token budget efficiency)
**Judge:** Automated metrics from existing benchmark harness.

### Experiment 3: Max Tokens Sweep

**Constant:** `max_tokens`
**Controlled setup:** Same as Experiment 2. Vary token budget with max_items fixed at current default (10).
**Sweep values:** [500, 1000, 2000, 4000, 6000, 8000, 12000]
**Measure:** M2, M3
**Judge:** Automated. Also measure actual token counts of injected context to verify budget enforcement.

### Experiment 4: Default Importance Sweep

**Constant:** `default importance` (the `importance` arg to `extract_memories()`)
**Controlled setup:** 20-turn conversation simulation. Each turn extracts 2-5 memories. After all turns, query for "most important" memories.
**Sweep values:** [0.1, 0.3, 0.5, 0.7, 0.9] (fixed default) plus a "varied" condition where importance is sampled from [0.3, 0.5, 0.7] per turn.
**Measure:** M4 (importance distribution health), M2 (does importance differentiation improve retrieval?)
**Judge:** Automated distribution analysis.

### Experiment 5: Score Weights Grid

**Constant:** `score_weights`
**Controlled setup:** Memory store with 50 pre-loaded memories having varied relevance, confidence, and decay scores.
**Sweep configurations:**
- `{"relevance": 1.0}` (relevance only)
- `{"confidence": 1.0}` (confidence only)
- `{"relevance": 0.6, "confidence": 0.3}` (current suggested default)
- `{"relevance": 0.5, "confidence": 0.5}` (equal blend)
- `{"relevance": 0.7, "confidence": 0.2, "decay": 0.1}` (three-factor)
- `{"relevance": 0.4, "confidence": 0.3, "decay": 0.3}` (three-factor equal-ish)
**Measure:** M6 (score weight sensitivity), M2
**Judge:** Automated nDCG@5 comparison.

### Experiment 6: Write Filter Thresholds at Recipe Layer

**Constants:** `_wf_min_threshold`, `_wf_priority_threshold`
**Controlled setup:** Run the full SubconsciousMemory pipeline (extract -> save with write filter -> retrieve). 20-turn simulation.
**Sweep:** `_wf_min_threshold` over [0.05, 0.1, 0.2, 0.3, 0.5], `_wf_priority_threshold` over [0.5, 0.6, 0.7, 0.8, 0.9]
**Measure:** M5 (write filter pass rate), M2 (does tighter filtering improve retrieval quality downstream?)
**Judge:** Automated. Compare against field-level results from existing harness to check for emergent effects.

### Experiment 7: Initial Confidence at Recipe Layer

**Constant:** `initial_confidence`
**Controlled setup:** Same 20-turn simulation. Vary starting confidence for new memories.
**Sweep values:** [0.1, 0.3, 0.5, 0.7, 0.9]
**Measure:** M2, M4 (does initial_confidence affect ranking differentiation after observation feedback?)
**Judge:** Automated. Compare against field-level results from existing harness.

### Experiment 8: Interaction Effects

**Pairs to test:**
1. `max_items` x `max_tokens` -- do they conflict or complement?
2. `extraction_min_length` x `default importance` -- does short extraction + high importance create noise amplification?
3. `_wf_min_threshold` x `initial_confidence` -- does low confidence + high filter threshold starve the memory store?
4. `score_weights` x `max_items` -- does the optimal weight configuration change with more/fewer items?

**Setup:** Pairwise grid using 3 values per constant (low, default, high).
**Measure:** M2 (nDCG@5) for each combination.
**Judge:** Automated. Flag pairs where the interaction effect (measured as deviation from additive model) exceeds 10% of the metric range.

## Corpus and Scenarios

### Scenario 1: Support Agent Accumulating Customer Context

**Description:** A customer support agent handles a 25-turn conversation about a billing issue. Early turns establish account details, middle turns discuss the problem, late turns negotiate a resolution.

**Memory characteristics:**
- High redundancy (customer repeats themselves)
- Temporal importance gradient (early context is foundational, late context is actionable)
- Mix of factual (account number, plan type) and emotional (frustration level) content

**Ground truth:** 15 memories are "essential" (account details, problem statement, resolution steps). 10 are "nice to have" (emotional context, pleasantries). 25+ are noise (repetition, filler).

**Why this scenario matters:** Tests extraction noise filtering, importance ranking, and whether max_items/max_tokens capture the essential memories without waste.

### Scenario 2: Coding Assistant Tracking Project Decisions

**Description:** A development assistant participates in a 30-turn design discussion. Topics include architecture choices, rejected alternatives, dependency decisions, and performance constraints.

**Memory characteristics:**
- Low redundancy, high information density
- Contradictions over time (decision A is proposed, then rejected in favor of B)
- Cross-referencing (decision B depends on constraint C established earlier)

**Ground truth:** 20 memories are "essential" (final decisions, active constraints). 5 are "superseded" (rejected alternatives -- should have low confidence after contradiction). 15 are contextual.

**Why this scenario matters:** Tests whether the confidence/observation feedback loop correctly demotes superseded decisions and whether score_weights properly balance recency against established importance.

### Scenario 3: Research Agent Collecting Facts from Multiple Sources

**Description:** A research agent processes 5 distinct source documents (each ~200 words), extracting facts about a topic. Some facts appear in multiple sources (corroboration), some contradict across sources.

**Memory characteristics:**
- Multiple extraction rounds (one per source document)
- Corroborated facts should gain confidence; contradicted facts should lose it
- High extraction volume (50+ candidate sentences across all sources)

**Ground truth:** 12 facts are "corroborated" (appear in 2+ sources). 5 are "contradicted" (conflicting claims). 8 are "unique but valid." Rest are noise or partial.

**Why this scenario matters:** Stress-tests extraction_min_length on varied source quality, tests whether write filter thresholds correctly pass corroborated facts and block noise, and tests whether score_weights properly rank corroborated facts above unique ones.

## Recommended Tooling

### Tier 1: Pure Pytest Benchmarks (No LLM Required)

Experiments 2, 3, 4, 6, 7, and 8 can be implemented as pure pytest benchmarks extending the existing harness in `tests/benchmarks/`. They exercise SubconsciousMemory's `inject_context()` and `extract_memories()` methods with pre-built corpora and measure retrieval metrics.

**Extend the existing harness by:**
- Adding a `Tier4` sweep section in `run_sweeps.py` for SubconsciousMemory-layer constants
- Creating new scenario classes in `tests/benchmarks/scenarios/` that use `SubconsciousMemory` directly instead of lower-level primitives
- Reusing the existing `SweepRunner`, `ResultsAggregator`, and retrieval metrics
- Adding override support for `SubconsciousMemory.__init__` kwargs in `overrides.py`

### Tier 2: Human-Labeled Ground Truth (One-Time Setup)

Experiment 1 (extraction quality) and Experiment 5 (score weights) require human-labeled ground truth datasets. These should be:
- Committed as JSON fixtures in `tests/benchmarks/fixtures/`
- Format: `{"text": "...", "sentences": [{"text": "...", "label": "meaningful|noise", "relevance": 0.0-1.0}]}`
- Created once, reused across all sweep runs

### Tier 3: LLM-Based Extraction Quality (Optional Enhancement)

For higher-fidelity evaluation of extraction quality, an optional LLM judge can score extracted sentences on a 1-5 scale for "standalone informativeness." This is not required for the initial experiments but would improve M1 measurement for edge cases where the regex splitter produces ambiguous results.

## Implementation Notes

### Extending the Override System

The existing `overrides.py` handles field-level and module-level constants via `apply_overrides()`. SubconsciousMemory-layer constants are constructor arguments, not module-level constants. The override mechanism needs a small extension:

- Add a `CONSTRUCTOR_OVERRIDES` registry mapping constant names to their SubconsciousMemory `__init__` parameter names
- Scenarios that test SubconsciousMemory constants should instantiate the recipe inside the `apply_overrides()` context manager, reading override values from the registry

### Scenario Base Class Extension

The existing `Scenario` base class (`tests/benchmarks/scenarios/base.py`) works at the field/query level. SubconsciousMemory scenarios need a higher-level base that:
- Creates a SubconsciousMemory instance with override-aware constructor kwargs
- Provides helper methods for multi-turn simulation (inject -> extract -> report cycle)
- Defines ground truth at the sentence/memory level, not just the record-ID level

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] SubconsciousMemory extraction with empty response text must return empty list, not crash
- [ ] inject_context with zero stored memories must return unmodified messages
- [ ] score_weights with missing field names must raise a clear error during ContextAssembler construction

### Empty/Invalid Input Handling
- [ ] extraction_min_length=0 should extract all non-empty sentences (valid edge case, not an error)
- [ ] max_items=0 should inject no memories (valid but degenerate -- flag in results)
- [ ] max_tokens=0 should inject no memories (valid but degenerate -- flag in results)

### Error State Rendering
- [ ] If write filter blocks all extracted memories, report "0 saved, N filtered" not a misleading success

## Test Impact

No existing tests affected -- this is an experiment design document. The follow-up implementation will:
- Add new scenario classes in `tests/benchmarks/scenarios/` (no modifications to existing scenarios)
- Add a Tier 4 section to `tests/benchmarks/run_sweeps.py` (additive, no changes to Tiers 1-3)
- Extend `tests/benchmarks/overrides.py` with constructor override support (additive)

## Rabbit Holes

- **LLM-in-the-loop benchmarks**: Tempting to call a real LLM during benchmarks to test "realistic" extraction, but this makes benchmarks non-deterministic, slow, and expensive. Use fixed response corpora instead.
- **Optimizing score_weights with gradient descent**: The weight space is small enough (2-3 dimensions) that grid search suffices. Gradient methods add complexity without meaningful benefit.
- **Per-scenario optimal defaults**: If optimal constants differ wildly between scenarios, the right answer is "no universal default exists" -- document the sensitivity and let users tune. Do not overfit defaults to one scenario.

## Risks

### Risk 1: Extraction quality metrics are unreliable
**Impact:** M1 depends on human-labeled ground truth. If labels are noisy, the extraction_min_length recommendation will be unreliable.
**Mitigation:** Use 3 labelers, require 2/3 agreement. Start with obvious cases (very short fragments = noise, full sentences with facts = meaningful).

### Risk 2: Score weights are scenario-dependent
**Impact:** No single default weight configuration works across all agent types.
**Mitigation:** Document the sensitivity. Propose a "safe default" that is within 90% of optimal for all tested scenarios, even if not optimal for any single one.

### Risk 3: Token budget experiments are hardware-dependent
**Impact:** Token counting heuristic (`len(str(r)) // 4`) varies with record serialization format.
**Mitigation:** Use the same serialization format across all experiments. Document the heuristic and note that real deployments with custom token counters may see different results.

## Race Conditions

No race conditions -- all experiments are sequential, single-process benchmarks using isolated Redis key prefixes.

## No-Gos (Out of Scope)

- Running the experiments (this is the design doc only)
- Changing any defaults before experimental data supports it
- Testing LLM extraction quality (regex splitter only for now)
- Building a dashboard or visualization tool for results
- Modifying the existing Tier 1-3 sweep infrastructure

## Update System

No update system changes required -- this is an experiment design document with no code changes.

## Agent Integration

No agent integration required -- benchmarks are development-only tools that do not need MCP exposure.

## Documentation

- [ ] Create this plan document at `docs/plans/subconscious_memory_constant_tuning.md`
- [ ] When experiments are executed (follow-up issue), document findings in `docs/guides/subconscious-memory-tuning.md`
- [ ] Add suggested default `score_weights` to SubconsciousMemory docstring after experiments conclude

## Success Criteria

- [ ] Proposal covers all 8 constants listed in issue #268
- [ ] Each constant has a clear metric, experiment design, and success criterion
- [ ] At least 2 realistic scenarios defined (3 are defined above)
- [ ] References existing benchmark harness and shows how to extend it
- [ ] Proposal is committed in `docs/plans/`
