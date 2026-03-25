---
status: Approved
type: chore
appetite: Medium
owner: Valor Engels
created: 2026-03-25
tracking: https://github.com/tomcounsell/popoto/issues/273
---

# SubconsciousMemory: Implement Constant Tuning Experiments

## Problem

The experiment design doc (`docs/plans/subconscious_memory_constant_tuning.md`, issue #268) defines 8 experiments across 3 scenarios for tuning SubconsciousMemory's magic-number constants. The design is complete but no implementation exists. The existing benchmark harness (PR #250) covers field-level constants (Tiers 1-3) but cannot run recipe-layer experiments because:

1. The override system (`overrides.py`) only patches module-level constants and `Defaults` class attributes -- it cannot inject constructor arguments into `SubconsciousMemory` or `ContextAssembler`.
2. The `Scenario` base class operates at the field/query level (save records, run `composite_score`, compare retrieved IDs). SubconsciousMemory experiments need multi-turn simulation (inject_context -> LLM response -> extract_memories -> report_outcomes).
3. No ground-truth fixture data exists for extraction quality (M1) or token efficiency (M3) metrics.
4. The metrics module only has retrieval metrics (precision@K, nDCG@K, calibration error, MRR). New metrics are needed: extraction F1, token utilization ratio, importance distribution health.
5. The original 3 scenarios test single-partition, single-source-type workloads. Real deployments involve **dual-partition retrieval** (company-wide shared knowledge + client-scoped NDA-protected memories), **mixed source types** (human directives vs agent observations), **large document storage** (ContentField + EmbeddingField from PR #261), and multiple interacting dynamics (redundancy, contradictions, corroboration, temporal layers, multi-hop associations) all present simultaneously.

**Current behavior:** Running `python -m tests.benchmarks.run_sweeps` executes Tiers 1-3 for field-level constants only. SubconsciousMemory-layer constants have no experimental coverage.

**Desired outcome:** A Tier 4 sweep section in the harness that runs all 8 experiments across 4 realistic scenarios, each exercising dual-partition retrieval, mixed source weighting, large content + semantic search, and the full range of memory dynamics. Produces comparable output in `results/summary.json` and identifies optimal defaults with data.

## Prior Art

- **PR #250**: "Experimental tuning benchmark harness for agent-memory constants" -- shipped the SweepRunner/ResultsAggregator/Scenario framework. This is the foundation we extend.
- **PR #253**: "Consolidate constants, graduate docs, prepare for experiments" -- centralized constants into `fields/constants.py` with the `Defaults` class. Made override injection possible.
- **PR #272 / Issue #268**: The experiment design doc itself. Defines metrics M1-M6, experiments 1-8, and 3 scenarios. Marked complete as a design deliverable.
- **Issue #234**: Original issue for the field-level benchmark harness. Closed when PR #250 merged.
- **PR #261**: "ContentField and EmbeddingField: large content storage and semantic search" -- adds ContentField (filesystem-backed large content), EmbeddingField (vector embeddings), and `semantic_search()` with `similarity_boost` on composite scoring. Merged 2026-03-23. Enables scenarios with mixed short memories + full documents + semantic retrieval.

## Spike Results

### spike-1: Can SubconsciousMemory be instantiated without Redis for extraction tests?
- **Assumption**: "extract_memories needs a live Redis connection for every call"
- **Method**: code-read
- **Finding**: `extract_memories()` calls `instance.save()` which requires Redis. However, extraction quality (M1) can be measured by calling `_split_sentences()` directly (it is a static method) and comparing against ground truth without saving. For full pipeline tests, Redis is required.
- **Confidence**: high
- **Impact on plan**: M1 extraction experiments can be split into a fast path (static method, no Redis) and full path (with save). The fast path can run without Redis infrastructure.

### spike-2: Does the override system need architectural changes for constructor args?
- **Assumption**: "Constructor arguments require a fundamentally different override mechanism"
- **Method**: code-read
- **Finding**: The existing `apply_overrides()` context manager patches module-level constants. SubconsciousMemory constructor args (`max_items`, `max_tokens`, `extraction_min_length`) are passed at instantiation time, not read from module globals at runtime. The scenarios already receive `overrides` in their constructor and use them to build model classes (see `factual_recall.py:_build_model_class`). The same pattern works: recipe-layer scenarios read constructor args from `self.overrides` when instantiating `SubconsciousMemory`. No changes to `overrides.py` needed -- just scenario-level constructor arg forwarding.
- **Confidence**: high
- **Impact on plan**: Eliminates the "extend override system" task from the design doc. Scenarios handle it directly.

### spike-3: Can score_weights sweep be automated without score_weights having a module-level default?
- **Assumption**: "score_weights has no documented default, making sweeping difficult"
- **Method**: code-read
- **Finding**: `score_weights` is a required argument to `SubconsciousMemory.__init__`. There is no default. The sweep defines candidate configurations directly in the sweep definition (Experiment 5 in the design doc lists 6 configurations). The scenario passes each configuration as a constructor arg. This is straightforward.
- **Confidence**: high
- **Impact on plan**: score_weights sweep is a standard parametric sweep, just using dict values instead of floats.

## Data Flow

The experiment pipeline extends the existing sweep architecture:

1. **Entry point**: `run_sweeps.py --tier 4` triggers Tier 4 sweep definitions
2. **SweepRunner**: Iterates over constant values, instantiates recipe-layer scenarios with overrides
3. **Recipe-layer Scenario**: Creates a `SubconsciousMemory` instance with override-derived constructor args, runs multi-turn simulation using fixture data
4. **SubconsciousMemory pipeline**: `inject_context()` -> simulated LLM response (from fixture) -> `extract_memories()` -> `report_outcomes()`
5. **Metrics**: Scenario collects retrieval IDs, extraction counts, token usage, importance distributions and returns them in `ScenarioResult.metadata`
6. **ResultsAggregator**: Computes optimal values, sensitivity curves, cliff effects -- same as Tiers 1-3
7. **Output**: Results appended to `results/summary.json`

## Critical Review of the Design Doc

The design doc is well-structured but has three feasibility issues:

### Issue 1: M4 (Importance Distribution Health) measures caller behavior, not a tunable constant

The `default importance` argument (constant 4) is a fixed value passed to `extract_memories()`. All memories extracted in a single call get the same importance. The design doc proposes measuring std deviation of importance scores after 20 turns -- but this only varies if the *caller* varies the importance argument per turn. The constant itself (0.5) just sets the default when the caller does not specify.

**Resolution**: Reframe Experiment 4 to test two things: (a) the sensitivity of retrieval quality to the fixed default value (does 0.5 produce better nDCG than 0.3 or 0.7?), and (b) whether a "varied" importance strategy (different values per turn) produces better ranking than a fixed default. This keeps the experiment useful without pretending the constant controls distribution shape.

### Issue 2: M5 (Write Filter Pass Rate) requires labeled "genuinely low-value" judgments

The design doc proposes human-labeling blocked memories as "genuinely low-value" to compute filter precision. For an automated benchmark, we replace this with a proxy: memories extracted from designated "noise" sentences in the fixture (sentences pre-labeled as noise) that get blocked are true positives. Memories extracted from "meaningful" sentences that get blocked are false positives.

**Resolution**: Use fixture labels as proxy for human judgment. The fixture already needs meaningful/noise labels for M1, so this piggybacks on the same data.

### Issue 3: Experiment 8 interaction matrix is large but manageable

4 pairs x 9 combos x 4 scenarios = 144 sweep points. Each point runs a 20-turn simulation. At ~100ms per turn (Redis operations, no LLM), this is ~300 seconds total. Acceptable for a benchmark suite.

**Resolution**: No change needed. Include a `--skip-interactions` flag for fast runs.

### Issue 4: Original 3 scenarios are unrealistically simple

The design doc scenarios each test one concern in isolation (support = redundancy, coding = contradictions, research = corroboration). Real deployments always involve multiple dynamics simultaneously. Every scenario must weave in all of the following:

1. **Dual-partition retrieval** -- company-wide shared knowledge (engineering standards, company culture, reference docs) + client-scoped NDA-protected memories (project decisions, client data). The agent queries both partitions and merges results. Architecturally isolated via `project_key` + `partition_by`.
2. **Cross-partition conflict surfacing** -- company rules may conflict with client requirements (e.g. "all APIs use REST" vs client needs GraphQL). The memory system must surface both sides so the agent can flag the conflict, not silently follow one.
3. **Human vs agent source weighting** -- human directives (InteractionWeight.HUMAN=6.0) are rare but high-signal; agent observations (InteractionWeight.AGENT=1.0) are frequent but lower-signal. Both exist at company-wide and client-scoped levels.
4. **Large documents + semantic search** -- ContentField stores full documents from a shared knowledge base (e.g. `~/work-vault/`). EmbeddingField enables semantic retrieval. These compete for budget with short extracted conversation memories.
5. **Temporal layers** -- foundational old knowledge (company policies, architecture decisions) coexists with urgent recent context (today's bug, this sprint's deadline).
6. **Redundancy, contradictions, and corroboration** -- all three present in every scenario, not siloed.
7. **Cold start vs saturated** -- early turns have sparse memories; late turns have 50+ competing. Constants must work across both regimes.
8. **Mixed information density** -- some turns are info-dense, others are filler/pleasantries.
9. **Multi-hop associations** -- CoOccurrence links between memories; retrieving memory A should also surface related memory B.
10. **Proactive vs pull retrieval** -- ContextAssembler's push path (unsolicited surfacing) and pull path (query-driven) both tested.

**Resolution**: Redesign all scenarios to weave in all 10 dynamics. Add a 4th scenario (team coordination) focused on human/agent authority dynamics. Each scenario differs in emphasis and mix, not in which concerns are present.

### Issue 5: Observe-first philosophy — generous caps with 10x headroom, log everything

Multiple constants impose tight arbitrary limits that silently discard data. But three independent code reviews (None propagation analysis, Redis/Lua safety audit, design critique) converged on a key insight: **truly unlimited is unsafe at the Redis/Lua boundary and risks cascading failures before observation data is collected.** The right approach: **set caps at least 1 order of magnitude above expected natural values, add structured logging, and use real-world data to tighten or further loosen.**

The cost asymmetry favors generous-but-bounded: too generous = slightly suboptimal retrieval (easy to fix); truly unlimited = Redis OOM, Lua crashes, context window starvation (hard to recover from).

**Implementation detail:** All defaults are concrete integers (100, 40000, 1000, etc.) — no `None` or `float('inf')` as defaults. The `_to_redis_limit()` safety function exists as a defensive layer in case callers pass `None` or `float('inf')` at runtime, converting them to safe integers before they reach Lua/Redis. But the defaults themselves are always bounded integers.

**Constants to loosen (10x headroom over expected natural values):**

| Constant | Current | Expected p95 | New Default | Rationale |
|---|---|---|---|---|
| `max_items` | 10 | ~15 | **100** | Caller-bounded by memory count; 10x headroom. `_to_redis_limit()` guards runtime overrides. |
| `max_tokens` | 4000 | ~8000 | **40000** | Caller controls LLM context window; generous headroom |
| `DEFAULT_MAX_ITEMS` | 10 | ~15 | **100** | Same as max_items (deduplicate with SubconsciousMemory) |
| `_max_access_log` | 100 | ~50 | **1000** | Real Redis memory constraint: 1M records × 1000 entries × 8 bytes = 8GB. Hard integer cap, no infinity. |
| `DEFAULT_PROPAGATION_DEPTH` | 2 | 2-3 | **3, hard cap at 5** | Exponential fan-out: depth 3 = manageable, depth 4+ = Lua OOM risk. This is an architectural constraint, not a tunable knob. |
| Candidate pool multiplier | 2x | - | **3x** | Generous but bounded; prevents unbounded candidate retrieval |
| `RecallProposal DEFAULT_TTL` | 3600s | ~2h | **86400** (1 day) | Prevents unbounded ZSET growth; 1 day is 24x current with room to observe |
| `MIN_EVENTS_FOR_CRYSTALLIZATION` | 3 | 1-5 | **1** | No resource risk; allow early crystallization, log event count |

**Hardcoded values to fix (should read from Defaults):**

| Value | Location | Fix |
|---|---|---|
| `decay_per_hop=0.5` | context_assembler.py:418 | → read `Defaults.CO_OCCURRENCE_DECAY_PER_HOP` |
| `threshold=0.01` | context_assembler.py:419 | → add `Defaults.CO_OCCURRENCE_PROPAGATION_THRESHOLD` |
| `len(str(r)) // 4` | context_assembler.py:233 | → make token counter a pluggable callable |

**None/infinity safety at Redis/Lua boundary:**

`None` cannot be passed to Lua (`tonumber("None")` → `nil` → crash). `float('inf')` cannot be passed to Redis LIMIT clauses (expects int). All values must be validated before crossing the boundary:

```python
def _to_redis_limit(value: int | float | None, default: int = 999_999_999) -> int:
    """Convert Python unlimited representations to safe Redis integers."""
    if value is None or value == float('inf'):
        return default
    return int(min(value, default))
```

Apply this at every call site that passes limits to `POPOTO_REDIS_DB.eval()`, `zrevrangebyscore()`, or `LTRIM`.

**Logging requirements:**

Every loosened constant emits a structured log entry when a value crosses a "notable" threshold. These are not errors — they are data points for future tuning decisions. Thresholds are set at ~50% of the new cap to give early warning.

| What to log | Notable threshold | Cap | Log level |
|---|---|---|---|
| Items retrieved per query | > 50 | 100 | `INFO` |
| Tokens assembled per query | > 20000 | 40000 | `INFO` |
| Access log entries per record | > 500 | 1000 | `INFO` |
| CoOccurrence BFS depth reached | > 3 | 5 | `WARNING` |
| Candidate pool size before scoring | > 150 | 300 | `INFO` |
| RecallProposal age at resolution | > 12h | 24h | `INFO` |
| Events before first crystallization | > 20 | none | `DEBUG` |

Log format: `popoto.observe.<component>` logger, structured as:
```
popoto.observe.context_assembler: items_retrieved=73 query="sprint priorities" partition=client project_key=royop
popoto.observe.context_assembler: tokens_assembled=18432 query="sprint priorities" max_tokens=40000
popoto.observe.access_tracker: access_log_size=512 record_key="Memory:abc123"
popoto.observe.co_occurrence: bfs_depth_reached=4 seed_key="Memory:abc123" candidates_found=31
```

**Where logs go:**
- In production: standard Python logging via `logging.getLogger("popoto.observe")`. Users configure handlers as needed (file, stdout, observability platform).
- In benchmarks: captured in `ScenarioResult.metadata["observations"]` as a list of dicts for automated analysis. Each sweep point records all observations, enabling distribution analysis across constant values.
- In experiments: the `results/summary.json` output includes per-sweep-point observation summaries (p50/p95/max for each metric) so we can see how natural distributions shift with different constants.

**Follow-up issue (post battle-testing):**

After the experiments run and the Valor AI system has been in production for 2-4 weeks, file a follow-up issue:
> **Title:** "Review `popoto.observe` logs and tighten or loosen caps based on production data"
> **Scope:** Analyze `popoto.observe.*` logs from production and benchmark results. For each constant:
> 1. Chart the natural distribution (p50, p95, p99, max) across real workloads
> 2. Compare against current cap — is the cap ever hit? How close does p95 get?
> 3. Check whether hitting the cap correlated with degraded retrieval quality or missed conflict pairs
> 4. Decide: (a) cap is fine, (b) raise the cap (still hitting it), (c) lower the cap (never close to it, wasting resources), or (d) remove the cap entirely (data shows no risk)
> 5. For BFS depth specifically: measure actual graph density and fan-out to validate the hard cap of 5
>
> **Deliverable:** PR with data-backed cap adjustments and documentation of the reasoning.

**Resolution**: Set all arbitrary limits to at least 10x their expected natural values (generous-but-bounded). Add `_to_redis_limit()` safety conversion at the Lua/Redis boundary. Add structured observation logging at 50% of cap. Fix hardcoded values to read from Defaults. Experiments measure natural distributions with generous caps. Sweeps test tighter values to measure degradation curves.

### Issue 6: New metric needed -- cross-partition conflict surfacing rate

When memories from the company-wide partition contradict memories from the client-scoped partition, the retrieval system must surface both. This is not a ranking quality issue (M2) -- it is a correctness issue. If `max_items` or `score_weights` cause one side of a conflict to be dropped, the agent cannot detect the contradiction.

**Resolution**: Add metric M7 (Conflict Surfacing Rate): for each known cross-partition conflict pair in the fixture, measure whether both sides appear in the retrieval results. Success criterion: ≥90% of conflict pairs surfaced at any `max_items` ≥ 5.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (scope confirmation after fixture design)
- Review rounds: 1 (code review of new scenarios and metrics)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis running | `redis-cli ping` | All scenarios need Redis for model operations |
| Dev dependencies | `python -c "import pytest; import popoto"` | Test framework and library |

## Solution

### Key Elements

- **Observe-first loosening**: Remove all arbitrary hard limits from SubconsciousMemory, ContextAssembler, AccessTracker, and ObservationProtocol. Add structured `popoto.observe.*` logging at extreme thresholds. Fix hardcoded values in context_assembler.py to read from Defaults.
- **Fixture data**: JSON files in `tests/benchmarks/fixtures/` with pre-labeled conversation corpora for each scenario, each containing dual-partition memories (company-wide + client-scoped), mixed source types (human/agent), large document references, cross-partition conflict pairs, and CoOccurrence links
- **Recipe-layer scenario base class**: Extends `Scenario` with multi-turn simulation, dual-partition setup, and all 10 woven-in dynamics
- **New metrics**: Extraction F1, token utilization ratio, importance distribution std dev, conflict surfacing rate -- added to `tests/benchmarks/metrics/`
- **Tier 4 sweep definitions**: Added to `run_sweeps.py` covering all 8 experiments
- **4 recipe-layer scenarios**: Support agent, coding assistant, research agent, team coordination -- each exercising all dynamics with different emphasis

### Technical Approach

- Recipe-layer scenarios instantiate `SubconsciousMemory` with constructor args read from `self.overrides`, bypassing the need to modify `overrides.py`
- Each scenario creates two `project_key` partitions: one for company-wide shared knowledge, one for client-scoped memories. Retrieval queries both and merges results, matching the real Valor AI deployment pattern.
- Fixture data provides deterministic "LLM responses" for extraction, avoiding real LLM calls
- Each fixture includes sentence-level labels (`meaningful`/`noise`), relevance tags per query, source type (`human`/`agent`), partition assignment (`company`/`client`), conflict pair IDs, and CoOccurrence link definitions
- Some fixture entries represent large documents (ContentField) with associated embeddings (EmbeddingField), competing for retrieval budget alongside short extracted memories
- The `ScenarioResult.metadata` dict carries recipe-specific metrics (extraction counts, token usage, distribution stats, conflict surfacing rate) alongside standard retrieval metrics
- `score_weights` sweeps use dict-valued sweep points instead of float-valued ones; the `SweepRunner` already supports `Any` values

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `SubconsciousMemory.extract_memories()` with empty string -- must return empty list (existing behavior, add test)
- [ ] `SubconsciousMemory.inject_context()` with zero stored memories -- must return unmodified messages (existing behavior, add test)
- [ ] Scenario teardown must clean up all Redis keys even if the run step raises (base class handles this via try/finally)

### Empty/Invalid Input Handling
- [ ] Fixture files must be validated on load (schema check for required fields)
- [ ] Sweep with `max_items=0` should produce a `skipped-degenerate` result, not crash
- [ ] Sweep with `extraction_min_length=0` should run (valid edge case, extracts all sentences)

### Error State Rendering
- [ ] Sweep summary must report degenerate/error points distinctly from OK points (existing behavior in `ResultsAggregator`)

## Test Impact

No existing tests affected -- this is purely additive:
- New scenario classes in `tests/benchmarks/scenarios/` (no modifications to existing scenarios)
- New fixture files in `tests/benchmarks/fixtures/` (new directory)
- New metrics in `tests/benchmarks/metrics/` (additive, no changes to `retrieval.py`)
- New Tier 4 section in `run_sweeps.py` (additive, no changes to Tiers 1-3)
- New test file `tests/benchmarks/test_tier4.py` for validating new scenarios run correctly

## Rabbit Holes

- **Building a fancy reporting UI**: The existing JSON output and console summary are sufficient. Matplotlib charts or HTML reports add complexity without improving the tuning decisions. If needed later, it is a separate issue.
- **Using real LLM calls for extraction quality**: Deterministic fixture data is the right choice for reproducible benchmarks. Real LLM calls introduce non-determinism, latency, and cost.
- **Optimizing score_weights with scipy.optimize**: The weight space is 2-3 dimensions with 6 candidate configurations. Grid search is exhaustive at this scale. Optimization frameworks add a dependency for no benefit.
- **Per-scenario optimal defaults**: If constants differ wildly between scenarios, document the sensitivity. Do not overfit defaults to one scenario.

## Risks

### Risk 1: Fixture data quality determines experiment validity
**Impact:** If fixture conversations do not realistically represent the scenarios, the optimal constants will be tuned to artificial data. With all 10 dynamics woven into each fixture, the complexity of fixture authoring increases significantly.
**Mitigation:** Each fixture includes 20-30 turns of realistic dialogue with diverse sentence lengths, information density, dual-partition data, conflict pairs, and CoOccurrence links. Start with minimal synthetic data (5-10 turns) for harness validation, then expand. Review fixtures as part of PR review.

### Risk 2: Token counting heuristic varies with serialization
**Impact:** M3 (token budget efficiency) depends on `len(str(r)) // 4` which is a rough approximation. Results may not transfer to deployments with custom tokenizers.
**Mitigation:** Document the heuristic. Use consistent serialization across all experiments. Note that absolute token counts are approximate but relative comparisons between sweep points are valid.

### Risk 3: Redis state leakage between sweep points
**Impact:** If one sweep point's data leaks into the next, metrics are contaminated.
**Mitigation:** Each scenario uses a UUID-prefixed key namespace (existing pattern in `Scenario.__init__`) and `teardown()` cleans up via SCAN. Recipe-layer scenarios follow the same pattern.

## Race Conditions

No race conditions -- all experiments are sequential, single-process benchmarks using isolated Redis key prefixes.

## No-Gos (Out of Scope)

- **Tuning** constant values based on intuition -- that happens after experiments produce data, in a separate PR
- Loosening arbitrary limits IS in scope (Task 0) -- removing premature constraints is different from tuning
- LLM-based extraction quality scoring (Tier 3 in the design doc, marked as optional)
- Visualization or dashboard tooling for results
- Modifying existing Tier 1-3 sweep infrastructure or existing scenario classes
- Running the experiments as part of CI (benchmarks are manual dev-time tools)
- Re-imposing limits based on experiment data alone -- production observation logs must inform limit decisions (see Follow-Up Issue)

## Follow-Up Issue (Post Battle-Testing)

After experiments run and the Valor AI system has been in production for 2-4 weeks, file:

> **Title:** "Review `popoto.observe` logs and decide on limits for SubconsciousMemory constants"
>
> **Context:** Issue #273 loosened all arbitrary hard limits and added structured observation logging. The system has now been running without limits in production. This issue reviews the data.
>
> **Scope:**
> 1. Pull `popoto.observe.*` logs from production (Valor AI agent sessions across multiple client projects)
> 2. Pull observation summaries from benchmark `results/summary.json` (Tier 4 sweep data)
> 3. For each loosened constant, analyze:
>    - Natural distribution (p50, p95, p99, max) across real workloads
>    - Whether extreme values correlated with performance degradation (latency, memory usage)
>    - Whether unlimited defaults caused any issues in practice
>    - Whether cross-partition conflict surfacing was affected
> 4. Decide per-constant: (a) no limit needed, (b) add a generous limit with logging, or (c) add a strict limit
> 5. If limits are added, they must be justified by data, documented with the reasoning, and added to Defaults
>
> **Deliverable:** PR with data-backed limit decisions (or explicit "no limit needed" documentation)

## Update System

No update system changes required -- benchmark harness is development-only tooling.

## Agent Integration

No agent integration required -- benchmarks are development-only tools that do not need MCP exposure.

## Documentation

### Inline Documentation
- [ ] Docstrings on all new scenario classes and metric functions
- [ ] Comments explaining fixture data format and labeling conventions

### Feature Documentation
- [ ] After experiments run and defaults are adjusted (separate follow-up), update `docs/guides/subconscious-memory-tuning.md` with findings
- [ ] Add suggested default `score_weights` to `SubconsciousMemory` docstring (after experiments conclude)

## Success Criteria

- [ ] All arbitrary limits raised to 10x headroom (max_items=100, max_tokens=40000, _max_access_log=1000, propagation_depth=3 with hard cap 5, RecallProposal TTL=86400)
- [ ] `_to_redis_limit()` safety conversion applied at all Lua/Redis boundaries (no None/inf reaches Redis)
- [ ] Hardcoded values in context_assembler.py read from Defaults
- [ ] `popoto.observe.*` logging emits structured entries at notable thresholds (~50% of cap)
- [ ] `tests/benchmarks/fixtures/` contains 4 scenario fixture files (support_agent.json, coding_assistant.json, research_agent.json, team_coordination.json)
- [ ] Each fixture contains dual-partition data (company-wide + client-scoped), mixed sources (human/agent), large document entries, cross-partition conflict pairs, and CoOccurrence links
- [ ] `tests/benchmarks/scenarios/` contains a recipe-layer base class and 4 scenario implementations
- [ ] `tests/benchmarks/metrics/` contains extraction F1, token utilization, and conflict surfacing rate metrics
- [ ] `run_sweeps.py --tier 4` runs all experiments to completion
- [ ] `results/summary.json` includes Tier 4 results with optimal values per constant
- [ ] Interaction effect sweeps run via `--tier 4 --interactions`
- [ ] Tests pass (`pytest tests/benchmarks/test_tier4.py`)
- [ ] All sweep points produce `ok` or `skipped-degenerate` status (no `error` status)
- [ ] Conflict surfacing rate ≥90% for all scenarios at reasonable constant values

## Team Orchestration

### Team Members

- **Builder (fixtures)**
  - Name: fixture-builder
  - Role: Create ground-truth fixture data for 4 scenarios with all woven-in dynamics
  - Agent Type: builder
  - Resume: true

- **Builder (harness)**
  - Name: harness-builder
  - Role: Implement recipe-layer scenario base, 4 scenarios, new metrics, Tier 4 sweep defs
  - Agent Type: builder
  - Resume: true

- **Validator (sweep)**
  - Name: sweep-validator
  - Role: Run Tier 4 sweeps end-to-end and verify results
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 0. Loosen arbitrary limits, add safety layer, add observation logging
- **Task ID**: loosen-limits
- **Depends On**: none
- **Validates**: `pytest tests/ -x -q` (no regressions), `python -c "import logging; logging.getLogger('popoto.observe')"` (logger exists)
- **Informed By**: Issue 5 in Critical Review; None propagation analysis; Redis/Lua safety audit; design critique
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: true (can run alongside fixture creation)
- **Philosophy**: 10x headroom over expected natural values. Generous-but-bounded, not unlimited. Cost of too generous is low (easy to tighten); cost of unlimited is cascading failure (hard to recover).
- **Loosen defaults (10x headroom) — exact file locations:**
  - `max_items`: 10 → **100** at `src/popoto/recipes/subconscious_memory.py:113` and `src/popoto/recipes/context_assembler.py:105,219`
  - `max_tokens`: 4000 → **40000** at `src/popoto/recipes/subconscious_memory.py:114` and `src/popoto/recipes/context_assembler.py:220`
  - `_max_access_log`: 100 → **1000** at `src/popoto/fields/access_tracker.py:73`
  - `DEFAULT_PROPAGATION_DEPTH`: 2 → **3, hard cap at 5** at `src/popoto/recipes/context_assembler.py:108` (exponential fan-out — architectural constraint, not tunable)
  - Candidate pool: `self.max_items * 2` → `self.max_items * 3` at `src/popoto/recipes/context_assembler.py:400,428`
  - `RecallProposal DEFAULT_TTL`: 3600s → **86400** (1 day) at `src/popoto/fields/observation.py:370`
  - `MIN_EVENTS_FOR_CRYSTALLIZATION`: 3 → **1** at `src/popoto/fields/constants.py:69`
- **Add `_to_redis_limit()` safety conversion** — validates values at the Lua/Redis boundary. Converts `None` or `float('inf')` to safe integers. Prevents the 7 crash sites identified by the None propagation analysis. Apply at these specific call sites:
  - `context_assembler.py:311` — `merged[:self.max_items]` slice
  - `context_assembler.py:323` — `total_tokens + tokens > self.max_tokens` comparison
  - `context_assembler.py:400,428` — `self.max_items * 2` arithmetic (change to `self.max_items * 3`)
  - `access_tracker.py:145` — `str(self._max_access_log)` passed to Lua `tonumber()` via `CONFIRM_ACCESS_LUA`
  - `query.py:588` — `num=limit` in `zrevrangebyscore()` LIMIT clause
  - `query.py:593` — `limit - 1` in `zrevrange()` call
  - `co_occurrence_field.py:554` — `str(depth)` passed to Lua `tonumber()` via `PROPAGATE_BFS_LUA`
  - `context_assembler.py` push path — `limit=self.max_items` passed to `composite_score()`
- **Fix hardcoded values:**
  - `context_assembler.py` `decay_per_hop=0.5` → read `Defaults.CO_OCCURRENCE_DECAY_PER_HOP`
  - `context_assembler.py` `threshold=0.01` → add and read `Defaults.CO_OCCURRENCE_PROPAGATION_THRESHOLD`
  - Make token counter in `ContextAssembler` a pluggable callable (default: `len(str(r)) // 4`)
- **Add `popoto.observe` logger hierarchy** with structured logging at notable thresholds (~50% of cap). See Issue 5 table for thresholds and log levels.
- Add `Defaults.OBSERVE_*` threshold constants for each notable threshold so they are tunable
- Ensure all existing tests still pass with loosened defaults

### 1. Create fixture data for 4 scenarios
- **Task ID**: build-fixtures
- **Depends On**: none
- **Validates**: fixture files parse correctly, contain required fields
- **Informed By**: spike-1 (extract_memories uses _split_sentences statically)
- **Assigned To**: fixture-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `tests/benchmarks/fixtures/` directory
- **Every fixture** includes all of: dual-partition data (company-wide + client-scoped), mixed source types (human/agent with InteractionWeight values), large document entries (ContentField-style), cross-partition conflict pairs, CoOccurrence link definitions, temporal layers (old foundational + recent urgent), redundant/contradicted/corroborated sentences, and mixed information density. Scenarios differ in emphasis, not in which dynamics are present.
- Create `support_agent.json`: 25-turn support conversation. **Company pool**: tone guidelines, escalation policies, SLA rules. **Client pool**: this customer's account details, history, open tickets. **Conflicts**: company SLA says 24h response but client contract says 4h. **Human source**: manager overrides escalation path. **Documents**: full product FAQ (ContentField). **CoOccurrence**: account details linked to billing history.
- Create `coding_assistant.json`: 30-turn design discussion. **Company pool**: engineering standards, architecture patterns, shared Django template conventions. **Client pool**: this client's proprietary codebase decisions, performance constraints, tech stack. **Conflicts**: company standard says REST but client needs GraphQL; company says PostgreSQL but client uses DynamoDB. **Human source**: tech lead mandates a deadline constraint. **Documents**: architecture decision records (ContentField). **CoOccurrence**: dependency decisions linked to performance constraints.
- Create `research_agent.json`: 5 source documents with multi-source analysis. **Company pool**: research methodology standards, citation requirements, domain knowledge base. **Client pool**: NDA-protected source documents, client-specific findings, proprietary data. **Conflicts**: company methodology says "minimum 3 sources" but client has only 1 authoritative source. **Human source**: research director prioritizes certain findings. **Documents**: full source papers (ContentField). **CoOccurrence**: corroborated facts linked across sources.
- Create `team_coordination.json`: 20-turn multi-agent team session. **Company pool**: company values, process standards, hiring guidelines, Yudame brand voice. **Client pool**: project-specific sprint goals, client stakeholder preferences, NDA-scoped deliverables. **Conflicts**: company process says "2-week sprints" but client wants continuous delivery; company hiring bar vs client's urgency to ship. **Human source**: CEO directive (InteractionWeight.EXECUTIVE), PM instructions (InteractionWeight.MANAGER), vs frequent agent status updates (InteractionWeight.PEER). **Documents**: project charter, team handbook (ContentField). **CoOccurrence**: sprint goals linked to deliverable dependencies. **Emphasis**: tests whether InteractionWeight correctly prevents human directives from being drowned by agent chatter volume.
- Fixture turn schema: `{"turns": [{"role": "user|assistant", "content": "...", "source": "human|agent", "source_role": "executive|manager|peer|subordinate", "partition": "company|client"}], "documents": [{"id": "doc1", "content": "...", "partition": "...", "source": "...", "embedding": "<base64-encoded numpy array>"}], "ground_truth": {"sentences": [{"id": "s1", "text": "...", "label": "meaningful|noise", "partition": "company|client", "source": "human|agent", "importance": 6.0, "relevance_per_query": {"q1": 0.9}}], "conflict_pairs": [{"company_sentence_id": "s3", "client_sentence_id": "s7"}], "cooccurrence_links": [{"from": "s1", "to": "s2", "strength": 0.8}]}}`
- **Fixture validation**: Create `tests/benchmarks/fixtures/schema.py` with a `validate_fixture(data: dict)` function that checks: required keys present, valid enum values (`meaningful`/`noise`, `company`/`client`, `human`/`agent`, valid `source_role`), conflict pair IDs reference existing sentences, CoOccurrence link IDs reference existing sentences, and all documents have embeddings. Call on fixture load in the scenario base class.

### 2. Implement new metrics
- **Task ID**: build-metrics
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_tier4.py::test_extraction_f1`, `test_token_utilization`, `test_conflict_surfacing`
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `tests/benchmarks/metrics/extraction.py` with `extraction_f1(extracted, ground_truth_labels)` function
- Add `tests/benchmarks/metrics/token_efficiency.py` with `token_utilization_ratio(records, relevance_scores, token_counter)` function
- Add `importance_distribution_health(scores)` returning std dev and distinct rank count
- Add `tests/benchmarks/metrics/conflict_surfacing.py` with `conflict_surfacing_rate(retrieved_ids, conflict_pairs)` -- for each known cross-partition conflict pair, checks whether both sides appear in the retrieval results. Returns the fraction of conflict pairs fully surfaced.

### 3. Implement recipe-layer scenario base class
- **Task ID**: build-scenario-base
- **Depends On**: build-fixtures, build-metrics, **loosen-limits** (Task 0 — loosened defaults and `_to_redis_limit()` must be in place before scenarios instantiate SubconsciousMemory)
- **Validates**: `tests/benchmarks/test_tier4.py::test_recipe_scenario_lifecycle`
- **Informed By**: spike-2 (no override system changes needed)
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/benchmarks/scenarios/recipe_base.py` extending `Scenario`
- **Dual-partition setup**: base class creates two `project_key` partitions (company-wide + client-scoped), populates both from fixture data, and provides a merged query helper that retrieves from both partitions
- Add multi-turn simulation helper: loop over fixture turns, call `inject_context()` with merged dual-partition retrieval, use fixture "response" as simulated LLM output, call `extract_memories()` routing to correct partition, call `report_outcomes()`
- **InteractionWeight routing**: for each fixture turn, read `source` field (`"human"` or `"agent"`) and map to `InteractionWeight.HUMAN` (6.0) or `InteractionWeight.AGENT` (1.0). If fixture also specifies `role`, combine with `InteractionWeight.combine(source_weight, role_weight)`. Pass the resulting value as the `importance` argument to `extract_memories()` or as the `importance` field on the Memory instance.
- **Document ingestion**: base class loads ContentField entries from fixture `documents` array, generates mock embeddings for semantic search testing
- **CoOccurrence setup**: base class creates CoOccurrence links from fixture `cooccurrence_links` definitions
- SubconsciousMemory instantiation reads `extraction_min_length`, `max_items`, `max_tokens`, `score_weights` from `self.overrides` with sensible defaults (max_items=100, max_tokens=40000). Observation logging captures natural item/token counts for every sweep point.
- Build a Memory model class dynamically (same pattern as `factual_recall.py:_build_model_class`) with all required fields including `project_key`, `source`, `importance`, ContentField, EmbeddingField, and CoOccurrenceField

### 4. Implement 4 recipe-layer scenarios
- **Task ID**: build-scenarios
- **Depends On**: build-scenario-base
- **Validates**: `tests/benchmarks/test_tier4.py::test_support_agent_scenario`, `test_coding_assistant_scenario`, `test_research_agent_scenario`, `test_team_coordination_scenario`
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/benchmarks/scenarios/support_agent.py` loading `support_agent.json` fixture
- Create `tests/benchmarks/scenarios/coding_assistant.py` loading `coding_assistant.json` fixture
- Create `tests/benchmarks/scenarios/research_agent.py` loading `research_agent.json` fixture
- Create `tests/benchmarks/scenarios/team_coordination.py` loading `team_coordination.json` fixture
- Each scenario sets up two partitions (company-wide + client-scoped) using distinct `project_key` values, populates both with fixture data, and queries both partitions with merged results
- Each scenario defines ground truth (relevant IDs, relevance scores, conflict pairs) from fixture labels
- Each scenario returns `ScenarioResult` with standard retrieval metrics plus recipe-specific metrics in metadata (including conflict surfacing rate, source weight effectiveness, document vs short-memory budget split)

### 5. Add Tier 4 sweep definitions to run_sweeps.py
- **Task ID**: build-sweeps
- **Depends On**: build-scenarios
- **Validates**: `tests/benchmarks/test_tier4.py::test_tier4_sweep_definitions`
- **Informed By**: spike-3 (score_weights sweep uses dict values)
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `TIER4_SWEEPS` dict to `run_sweeps.py` with all single-constant sweeps from experiments 1-7
- Add `TIER4_INTERACTION_PAIRS` for experiment 8 pairwise sweeps
- Add `TIER4_SCENARIOS` list referencing the 4 new scenario classes
- Extend `main()` to accept `--tier 4` and `--tier all` to include Tier 4. **Specifically: add `"4"` to the argparse `choices` list at `run_sweeps.py:135`** (currently `["1", "2", "3", "all"]`).
- Handle dict-valued sweep points (score_weights) in the sweep runner -- if SweepRunner cannot handle dicts natively, add score_weights as a separate sweep method

### 6. Write test file for Tier 4
- **Task ID**: build-tests
- **Depends On**: build-sweeps
- **Validates**: `pytest tests/benchmarks/test_tier4.py -v`
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/benchmarks/test_tier4.py` with tests that each scenario runs without error
- Test that fixture files load and validate
- Test that new metrics produce correct values on known inputs
- Test that a small sweep (1 constant, 2 values, 1 scenario) completes with OK status

### 7. Validate end-to-end sweep
- **Task ID**: validate-sweep
- **Depends On**: build-tests
- **Assigned To**: sweep-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `python -m tests.benchmarks.run_sweeps --tier 4` and verify it completes
- Verify `results/summary.json` contains Tier 4 constant entries
- Verify no sweep points have `error` status
- Run `pytest tests/benchmarks/test_tier4.py -v` and verify all pass

### 8. Final Validation
- **Task ID**: validate-all
- **Depends On**: validate-sweep
- **Assigned To**: sweep-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify no regressions in existing Tier 1-3 tests
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Limits loosened | `python -c "from popoto.recipes.subconscious_memory import SubconsciousMemory; import inspect; sig=inspect.signature(SubconsciousMemory.__init__); print(sig.parameters['max_items'].default)"` | `100` |
| Observe logger exists | `python -c "import logging; logging.getLogger('popoto.observe.context_assembler')"` | exit code 0 |
| Tests pass | `pytest tests/benchmarks/test_tier4.py -v` | exit code 0 |
| Fixtures exist | `ls tests/benchmarks/fixtures/*.json \| wc -l` | output contains 4 |
| Tier 4 runs | `python -m tests.benchmarks.run_sweeps --tier 4 2>&1 \| tail -1` | output contains "Results saved" |
| No import errors | `python -c "from tests.benchmarks.scenarios.support_agent import SupportAgentScenario"` | exit code 0 |
| Full test suite | `pytest tests/ -x -q` | exit code 0 |

## Critique Results

<!-- Round 1 critique: 2026-03-25 -->
| Severity | Critics | Finding | Suggestion |
|----------|---------|---------|------------|
| BLOCKER | Skeptic, Adversary | Task 0 loosens `max_items` to `None` but `context_assembler.py:400,428` uses `self.max_items * 2` which raises `TypeError` when `max_items is None`. The plan mentions removing the `2x` cap but does not require it to happen atomically with the default change. | Make removing the `self.max_items * 2` references and changing the default a single atomic sub-task. Add a test that `ContextAssembler(max_items=None)` does not crash. Also handle `self.max_items` at line 311 (`merged[:None]` works but line 412 `candidates[:self.max_items]` should be verified). |
| BLOCKER | Operator, Skeptic | Task 5 says "Extend `main()` to accept `--tier 4`" but `run_sweeps.py:135` has `choices=["1", "2", "3", "all"]`. The `"4"` choice is not explicitly called out, so a builder could add the tier logic but forget to update argparse choices, causing `--tier 4` to fail. | Add explicit sub-bullet to Task 5: "Add `'4'` to argparse `choices` list at line 135." |
| CONCERN | Adversary, Skeptic | Task 0 loosens `_max_access_log` to `None`. The Lua script `CONFIRM_ACCESS_LUA` (access_tracker.py:47) calls `tonumber(ARGV[1])`. With `_max_access_log=None`, Python passes `str(None)` = `"None"`, and Lua `tonumber("None")` returns `nil`, causing `LTRIM` with nil argument to error. | Add sub-bullet to Task 0: skip LTRIM when `_max_access_log is None` (guard in Python before eval, or use sentinel like `2**31`). Add test for `confirm_access()` with unlimited log. |
| CONCERN | Skeptic, Simplifier | Task 1 fixture schema is complex (turns, documents, ground_truth with sentences/conflict_pairs/cooccurrence_links). Plan says "fixture files must be validated on load" but specifies no validation tooling or schema definition file. With 4 fixtures containing 20-30 turns each, manual validation is error-prone. | Add sub-task to Task 1: create a JSON Schema file or validation function for fixtures. Consider adding `jsonschema` as dev dependency. |
| CONCERN | Archaeologist | Plan references file paths imprecisely. `ObservationProtocol` and `RecallProposal` are in `src/popoto/fields/observation.py` (also re-exported in `recipes/`). `MIN_EVENTS_FOR_CRYSTALLIZATION` is in `src/popoto/fields/constants.py`. Builders unfamiliar with the codebase could waste time locating files. | Add explicit file paths for all constants in the Task 0 table (e.g., `RecallProposal.DEFAULT_TTL` in `src/popoto/fields/observation.py`). |
| CONCERN | Operator | Open Questions section has duplicate numbering: questions numbered 1, 2, 3, 4, 5, 3. The final question (Result storage for recipe-specific metrics) is misnumbered as "3" instead of "6". | Renumber the final question to 6. |
| NIT | Adversary | `_push_path` (context_assembler.py:454) passes `limit=self.max_items` to `composite_score()`. With `max_items=None`, this passes `limit=None`. Need to verify `composite_score()` handles `None` limit gracefully. | Verify `composite_score(limit=None)` works. If not, add to Task 0 fix list. |
| NIT | Operator | Verification table (line 484-485) uses `\|` escaped pipes in shell commands, which may cause copy-paste issues from markdown. | Use inline code blocks or note that pipes should not be escaped when running. |

<!-- Round 2 critique: 2026-03-25. Plan revised with bounded caps, _to_redis_limit(), Task 0, 4 scenarios. -->
| Severity | Critics | Finding | Suggestion |
|----------|---------|---------|------------|
| BLOCKER | Skeptic, Adversary | **Internal contradiction on float('inf') vs bounded integers.** Issue 5 line 114 says "Use `float('inf')` internally in Python for unlimited-capable parameters (`max_items`, `max_tokens`)" and the table says `max_items` is "converted to 999999999 at Redis LIMIT boundary." But Task 0 (line 385-387) says to set `max_items` to **100** and `max_tokens` to **40000** -- concrete bounded integers. The scenario base class (Task 3, line 443) also defaults to `max_items=100, max_tokens=40000`. A builder reading Issue 5 will implement `float('inf')` as the default; a builder reading Task 0 will implement `100`. These are fundamentally different behaviors. | Remove the `float('inf')` recommendation from Issue 5 entirely. The plan already chose bounded integers (100, 40000) everywhere that matters. The `_to_redis_limit()` safety layer should remain as defense-in-depth for callers who pass `None` or `inf`, but the library defaults must be concrete integers. Rewrite Issue 5 line 114 to say: "Use concrete bounded integers as defaults. The `_to_redis_limit()` function provides a safety net for edge-case caller inputs." |
| BLOCKER | Skeptic, Operator | **Round 1 BLOCKER (argparse --tier 4) NOT addressed.** The Round 1 critique identified that `run_sweeps.py:135` has `choices=["1", "2", "3", "all"]` and Task 5 does not explicitly mention adding `"4"` to the choices list. The plan was revised but this sub-bullet was never added to Task 5. The finding remains: a builder following Task 5 as written could add all the sweep logic but forget the argparse choices update, and `--tier 4` would fail with an argparse error. | Add explicit sub-bullet to Task 5: "Update argparse `choices` at `run_sweeps.py:135` from `['1', '2', '3', 'all']` to `['1', '2', '3', '4', 'all']`." |
| CONCERN | Adversary, Skeptic | **Round 1 CONCERN (Lua LTRIM crash) partially addressed but residual risk remains.** The plan added `_to_redis_limit()` (line 142-146) which handles `None` and `float('inf')`. However, Task 0 sets `_max_access_log` to **1000** (a bounded integer), so the Lua crash path (`tonumber("None")`) should never trigger with the new default. But the `_to_redis_limit()` call site is specified only generically ("Apply at every call site that passes limits to eval/zrevrangebyscore/LTRIM"). The Lua script `CONFIRM_ACCESS_LUA` takes `ARGV[1]` as a string passed via `str(self._max_access_log)` at `access_tracker.py:145`. If a caller subclasses and sets `_max_access_log = None`, `str(None)` = `"None"` reaches Lua. The `_to_redis_limit()` guard must be applied **in Python before the eval call**, not inside Lua. Task 0 does not list `access_tracker.py:145` as a specific call site. | Add `access_tracker.py:145` to Task 0's explicit list of `_to_redis_limit()` application sites. Change line 145 from `str(self._max_access_log)` to `str(_to_redis_limit(self._max_access_log, default=1000))`. |
| CONCERN | Adversary | **`composite_score()` limit parameter is typed `int` but plan may pass `float('inf')`.** `query.py:449` declares `limit: int = 10`. Line 593 computes `limit - 1` for `zrevrange`. If a scenario passes `float('inf')` as `max_items`, then `context_assembler.py:400` computes `float('inf') * 2 = float('inf')` and passes it to `composite_score(limit=float('inf'))`. Then `zrevrange(key, 0, float('inf') - 1)` passes `float('inf')` to Redis, which expects an integer. Even with `_to_redis_limit()` at the assembler level, there is no guard inside `composite_score()` itself. With the bounded-integer approach (max_items=100), this path is safe, but the `float('inf')` text in Issue 5 creates the risk. | This reinforces removing the `float('inf')` recommendation. Additionally, consider adding `_to_redis_limit()` inside `composite_score()` as belt-and-suspenders, or at minimum adding `assert isinstance(limit, int)` as a guard. |
| CONCERN | Skeptic | **Round 1 CONCERN (fixture validation) NOT addressed.** The plan still says "fixture files must be validated on load" (line 245) but no validation function, JSON Schema file, or `jsonschema` dev dependency is specified anywhere in the tasks. The fixture schema on line 416 is specified only as a single-line JSON example embedded in a bullet point -- not a formal schema. With the expanded scope (4 fixtures, dual-partition data, conflict pairs, CoOccurrence links, documents), schema drift between fixtures is likely. | Add a sub-task to Task 1: "Create `tests/benchmarks/fixtures/schema.py` with a `validate_fixture(data: dict)` function that checks required keys, valid enum values (meaningful/noise, company/client, human/agent), conflict pair reference integrity, and CoOccurrence link reference integrity. Call it on fixture load in the scenario base class." |
| CONCERN | Skeptic, Operator | **Task dependencies undercount parallelism opportunities.** Task 0 (loosen-limits) and Task 1 (build-fixtures) are both marked `Parallel: true` with no dependencies, which is correct. But Task 2 (build-metrics) is also marked `Parallel: true` with no dependencies, yet it shares no code with Tasks 0 or 1. All three could run simultaneously. However, Task 3 (build-scenario-base) depends on Tasks 1 and 2 but NOT Task 0. Since Task 0 changes library defaults and Task 3 instantiates `SubconsciousMemory` with those defaults, Task 3 should also depend on Task 0. Otherwise a builder could start Task 3 before the limits are loosened, and the scenario base class would use old defaults. | Add `loosen-limits` to Task 3's `Depends On` list: `build-fixtures, build-metrics, loosen-limits`. |
| CONCERN | Operator | **Round 1 CONCERN (imprecise file paths) NOT addressed.** Task 0 still does not list explicit source file paths for each constant. The task says "max_items: 10 -> 100 in SubconsciousMemory.__init__ and ContextAssembler" but does not give file paths. Verified locations: `SubconsciousMemory.__init__` is at `src/popoto/recipes/subconscious_memory.py:113`, `ContextAssembler` defaults at `src/popoto/recipes/context_assembler.py:105,219`, `_max_access_log` at `src/popoto/fields/access_tracker.py:73`, `DEFAULT_PROPAGATION_DEPTH` at `src/popoto/recipes/context_assembler.py:108`, `RecallProposal.DEFAULT_TTL` at `src/popoto/fields/observation.py:370`, `MIN_EVENTS_FOR_CRYSTALLIZATION` at `src/popoto/fields/constants.py:69` (re-imported at `src/popoto/recipes/policy_cache.py:80`). | Add these exact file paths to Task 0's bullet points so builders can locate each constant without searching. |
| CONCERN | Adversary | **Team coordination fixture (line 415) references `InteractionWeight.EXECUTIVE` and `InteractionWeight.MANAGER` but the scenario base class (Task 3) does not mention routing InteractionWeight values from fixtures into the memory importance field.** The fixture schema (line 416) has `"importance": 6.0` on ground_truth sentences but not on turns. The multi-turn simulation helper (Task 3, line 440) says "assign source type and InteractionWeight based on fixture labels" but the fixture turn schema only has `"source": "human|agent"`, not a role field. There is no mapping from `source` to `InteractionWeight` values. | Either (a) add a `"role"` field to the fixture turn schema (e.g., `"role": "executive|manager|peer"`) so importance can be computed via `InteractionWeight.combine(source, role)`, or (b) add a `"weight"` field directly to turns. Update Task 3 to describe how InteractionWeight is derived from fixture data. |
| NIT | Operator | **Round 1 NIT (escaped pipes in verification table) NOT addressed.** Lines 515-516 still use `\|` in shell commands within the verification table. | Replace `\|` with unescaped `|` or restructure the commands to avoid pipes. |
| NIT | Skeptic | **Candidate pool multiplier inconsistency.** The Issue 5 table (line 125) says candidate pool goes from `2x` to `3x`. Task 0 (line 390) says `self.max_items * 2` -> `self.max_items * 3`. But with `max_items` raised from 10 to 100, the candidate pool goes from 20 to 300 -- a 15x increase in candidates fetched per query, not the 1.5x suggested by "2x->3x". This compounding effect (new multiplier x new base) is not discussed. At 300 candidates per query, Redis ZUNIONSTORE and re-ranking costs may be notable. | Add a note acknowledging the compounding: with `max_items=100` and `3x` multiplier, candidate pool is 300. Confirm this is acceptable for Redis performance, or consider whether the multiplier should stay at 2x given the already-generous base. |
| NIT | Simplifier | **Open Questions 3-5 are design decisions that affect fixture schema and scenario implementation, but they are listed as "open" with no resolution.** If a builder starts Task 1 (fixtures) while these are unresolved, they cannot include `similarity_boost` in score_weights configs (Q3), cannot generate mock embeddings (Q4), and cannot implement dual-partition merging (Q5). These should be resolved before Tasks 1-3 begin. | Resolve Q3-Q5 in the plan text (even if tentatively) so builders have clear guidance. Recommended: Q3 = yes include similarity_boost, Q4 = synthetic embeddings, Q5 = concatenate and re-rank globally. |

---

## Open Questions

1. **Fixture data depth**: Should fixtures contain full realistic conversations (20-30 turns with natural language) or minimal synthetic data (short, artificial sentences designed to test edge cases)? Full realism is better for validity but takes longer to craft and review. Recommendation: start with minimal synthetic data (5-10 turns) for initial harness validation, then expand to full realism in a follow-up if the harness works.

2. **score_weights sweep format**: The design doc proposes 6 dict configurations for score_weights. The current `SweepRunner.run_single_sweep` expects a flat `{constant_name: value}` dict. Should we (a) add a special case in `SweepRunner` for dict-valued constants, or (b) create a dedicated `run_score_weights_sweep` method? Option (a) is simpler if the runner already handles `Any` types.

3. **score_weights should include similarity_boost**: With PR #261 merged, `semantic_search()` injects a `similarity_boost` signal into `composite_score()`. The score_weights sweep (Experiment 5) should include configurations that weight the similarity signal alongside relevance, confidence, and decay. **Resolved:** Yes, add 2-3 additional weight configurations that include `similarity` as a factor (e.g., `{"relevance": 0.4, "confidence": 0.2, "similarity": 0.4}`, `{"relevance": 0.3, "confidence": 0.2, "similarity": 0.3, "decay": 0.2}`). This brings the total score_weights configurations to ~8-9.

4. **Mock embeddings for benchmarks**: Semantic search requires embeddings. For deterministic benchmarks, should we (a) pre-compute real embeddings and store them in fixtures, or (b) use synthetic embeddings that encode known similarity relationships? **Resolved:** Option (b) — use synthetic embeddings. Create numpy arrays where known-related memories have high cosine similarity (>0.8) and unrelated memories have low similarity (<0.3). This is deterministic, controllable, and avoids embedding API dependency. Store as base64-encoded arrays in fixture JSON.

5. **Dual-partition query merging strategy**: When the scenario queries both company-wide and client-scoped partitions, how should results be merged? **Resolved:** Option (a) — concatenate and re-rank by composite score globally. This matches the natural ContextAssembler behavior (it scores all candidates uniformly). The `max_items` cap applies globally after merging, not per-partition. This means the experiments will reveal whether score_weights can naturally balance company-wide vs client-scoped relevance without explicit partition budgeting.

6. **Result storage for recipe-specific metrics**: Standard `SweepPoint` stores precision@K and nDCG@K. Recipe-layer experiments also produce extraction F1, token utilization, and importance distribution stats. Should these go in (a) additional fields on `SweepPoint`, or (b) the existing `metadata` pattern? Option (b) avoids changing the dataclass but makes aggregation harder.
