---
status: Planning
type: feature
appetite: Medium
owner: tomcounsell
created: 2026-05-18
tracking: https://github.com/tomcounsell/popoto/issues/394
last_comment_id:
---

# External Benchmark Harness: LongMemEval-S + LoCoMo

## Problem

Popoto Agent Memory has 14 shipped primitives and a parametric tuning harness at `tests/benchmarks/` that sweeps ~25 behavioral constants against synthetic scenarios (factual recall, multi-step reasoning, temporal scheduling, recipe-layer support/coding/research agents). Those sweeps measure precision@k, nDCG@5, MRR, ECE on hand-crafted data — they tell us "did constant X move the dial against our own scenarios?" but not "how does Popoto Agent Memory stack up on externally-recognized benchmarks?"

**Current behavior:** No published Popoto number on any external memory benchmark. Internal sweeps live in `tests/benchmarks/results/sweep_*.json`. Comparable systems (e.g., `agentmemory`) report 95.2% R@5 on LongMemEval-S; we have no equivalent figure.

**Why this is painful now:** Two large changes are queued — issue #395 (default `ContextAssembler` to hybrid retrieval: BM25 + vector + graph + RRF) and issue #396 (working→episodic→semantic consolidation/decay lifecycle). Without an external baseline, "did this help?" is unanswerable on real data, and we can't publish credible comparisons.

**Desired outcome:** A reproducible benchmark runner that loads LongMemEval-S and LoCoMo, ingests each example's conversation history into the current Popoto memory stack, runs the query, and reports R@1/R@5/R@10, MRR, and latency p50/p95. Outputs a committed markdown + JSON artifact per dataset per run, so #395 and #396 can show measurable deltas against the numbers this plan establishes.

## Freshness Check

**Baseline commit:** `10cf580e35102d19f568c72708930b96345cd10e`
**Issue filed at:** 2026-05-17T23:09:48Z (less than 24h before plan time)
**Disposition:** Unchanged

**File:line references re-verified:**
- `tests/benchmarks/` — directory exists with the expected structure (scenarios/, metrics/, results/, sweep.py, run_sweeps.py, overrides.py, ratchet.py). Confirmed.
- `tests/benchmarks/metrics/retrieval.py` — has `precision_at_k`, `ndcg_at_k`, `mean_reciprocal_rank`, `calibration_error`. Does NOT yet have `recall_at_k` or latency percentile helpers. Confirmed gap.
- `tests/benchmarks/scenarios/base.py` — `Scenario` ABC with `setup/run/teardown` and `ScenarioResult` dataclass with `retrieved_ids/relevant_ids/relevance_scores/duration_ms`. Confirmed.
- `src/popoto/recipes/context_assembler.py` — present, will be the primary integration point.
- PR #304 (BM25Field + CompositeScoreQuery.fuse) — closed/merged; `BM25Field` and `CompositeScoreQuery` are available.

**Cited sibling issues/PRs re-checked:**
- #395 "Default ContextAssembler to hybrid retrieval" — open, will consume this plan's baseline.
- #396 "Memory lifecycle: working/episodic/semantic consolidation + decay + auto-forget" — open, will consume this plan's baseline.
- #304 "Hybrid retrieval: BM25 + RRF fusion" — closed/shipped.

**Commits on main since issue was filed:** None touching `tests/benchmarks/` since the issue was filed (issue created < 24h before plan time, baseline SHA unchanged from main tip).

**Active plans in `docs/plans/` overlapping this area:** None. Closest neighbors are `subconscious_memory_constant_tuning.md` and `scenario_code_path_coverage.md` — both shipped, both about internal parametric sweeps, not external datasets.

**Notes:** Premise holds verbatim. No drift.

## Prior Art

Searched closed issues and merged PRs for `benchmark`, `longmemeval`, `locomo`. The only directly related closed item is #304 (BM25 + RRF) which is the dependency this plan builds on, not a prior attempt at this work.

- **Issue #304 / PR #304**: Hybrid retrieval (BM25 + RRF fusion) — merged. Provides `BM25Field` and `CompositeScoreQuery.fuse()` which downstream issue #395 will use. This plan does NOT depend on #395 landing — it measures the *current* stack as-is so #395 has a baseline to beat.
- **`tests/benchmarks/` harness** (multiple commits, 2026-03 through 2026-04): Established `Scenario`/`ScenarioResult`/`SweepRunner`/`ResultsAggregator` plumbing, retrieval metrics, parametric scenario factory, ratchet loop. This is the substrate. New work plugs into it; no greenfield harness.
- **No prior external-dataset benchmark attempt found.** This is the first published external number for Popoto.

## Research

**Queries used:**
- LongMemEval dataset schema and download
- LoCoMo dataset format and licensing
- Reciprocal Rank Fusion baseline expectations

**Key findings (from issue body + codebase, supplemented by training data — WebSearch tool not used because the relevant references are already cited in the issue):**
- LongMemEval (https://github.com/xiaowu0162/LongMemEval): "S" variant uses short context; dataset items pair a long multi-session history with a question whose answer is contained in some subset of historical turns. Released as JSON; load on demand from upstream, do not redistribute.
- LoCoMo (https://snap-research.github.io/locomo/): multi-session conversations with grounded QA; each example has a conversation transcript and a target answer tied to specific historical evidence spans.
- The `agentmemory` reference repo reports 95.2% R@5 on LongMemEval-S. Treat as a north-star, not a target — this issue establishes measurement, not optimization (per issue's explicit "out of scope").

**Implications for plan:**
- Both datasets are released as JSON-style payloads. Adapters can be simple iterators that yield `(conversation_history, query, expected_evidence_ids)` tuples.
- Datasets must not be committed (license + size). Cache locally under `tests/benchmarks/datasets/_cache/` (gitignored). CI must skip these benchmarks by default unless an opt-in env var is set.

## Spike Results

No spikes were required. All assumptions are codebase-verifiable from the freshness check above, and dataset adapter shape is constrained by the upstream JSON schema — not a verifiable assumption needing prototyping ahead of the plan. The build itself includes the dataset adapter as its first task; that task is the spike-equivalent and its output is committed code, not a throwaway prototype.

## Data Flow

1. **Entry point**: `python -m tests.benchmarks.run_external --dataset {longmemeval-s | locomo} [--limit N] [--profile {current | bm25-only | vector-only}]`
2. **Dataset adapter** (`tests/benchmarks/datasets/{longmemeval_s,locomo}.py`): Downloads dataset on first run into `tests/benchmarks/datasets/_cache/` (gitignored). Yields `BenchmarkExample(example_id, history, query, expected_evidence_ids, gold_answer)` instances.
3. **Scenario wrapper** (`tests/benchmarks/scenarios/external_dataset.py`): For each example, instantiates Popoto memory primitives under a unique Redis key prefix (reusing `Scenario` base teardown). Ingests `history` turns into memory (via `SubconsciousMemory.observe()` or direct `Memory.save()` — to be settled in task 2). Runs the query through `ContextAssembler.assemble(query=...)`. Returns `ScenarioResult` with `retrieved_ids=assembled-memory-ids`, `relevant_ids=expected_evidence_ids`, `duration_ms`.
4. **Per-example timing**: Capture `retrieval_duration_ms` separately from setup/ingest duration so latency p50/p95 reflects retrieval cost, not ingestion cost.
5. **Aggregation** (`tests/benchmarks/external_aggregator.py`): Streams `ScenarioResult`s into running R@K (1/5/10), MRR, latency percentiles. No need to hold all examples in memory.
6. **Output**:
   - JSON: `tests/benchmarks/results/external/{dataset}_{YYYYMMDD_HHMMSS}.json` — per-example results + summary stats + git SHA + dataset hash.
   - Markdown: `tests/benchmarks/results/external/{dataset}_{YYYYMMDD_HHMMSS}.md` — human-readable summary table.
   - Symlink: `tests/benchmarks/results/external/{dataset}_latest.json` updated to most recent run.

## Architectural Impact

- **New dependencies**: `datasets` (Hugging Face) OR plain `requests` for download — to be decided in task 1. Prefer `requests` + manual JSON parsing to avoid pulling in the full HF stack as a benchmark dep. No new runtime deps; this is a `tests/` dep.
- **Interface changes**: None to Popoto public API. Purely additive under `tests/benchmarks/`.
- **Coupling**: New scenario depends on `ContextAssembler` and `SubconsciousMemory` recipes. This is the right coupling — the benchmark is *of* the recipes.
- **Data ownership**: Datasets are external; cached locally; never committed. Result artifacts are committed (small JSON/MD).
- **Reversibility**: Fully reversible. Delete `tests/benchmarks/datasets/` + `tests/benchmarks/results/external/` to undo.
- **Valkey compat**: No Redis-module-only commands used. `BM25Field` (PR #304) is Valkey-compatible. `ContextAssembler` already runs on Valkey. Embedding-based vector ranking (if used) must use Popoto's existing vector path which is module-free. Confirmed against the `feedback_valkey_compatibility.md` memory.

## Appetite

**Size:** Medium

**Team:** Solo dev, PM (issue author for open-question resolution)

**Interactions:**
- PM check-ins: 1-2 (resolve open questions on embedding model + CI policy before build starts; review baseline numbers before committing)
- Review rounds: 1 (one PR review before merge)

Medium because it spans dataset download/caching, two adapters, scenario integration with two recipes, two new metric functions, a new runner CLI, result artifacts, and docs. Each piece is small but there are eight of them, and the first baseline run is a one-shot artifact commit that the next two issues anchor against — getting it right matters.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis or Valkey on `localhost:6379` | `redis-cli ping` returns `PONG` | Memory primitives need a backing store |
| `requests` available in dev env | `python -c "import requests"` exits 0 | Dataset download |
| Network access to dataset sources on first run | `curl -fsSL -o /dev/null https://github.com/xiaowu0162/LongMemEval` | LongMemEval download |
| Network access to LoCoMo | `curl -fsSL -o /dev/null https://snap-research.github.io/locomo/` | LoCoMo download |
| Embedding model resolution (see Open Question 3) | TBD after Q3 answered | Vector retrieval leg of `ContextAssembler` |

Run all checks: `python scripts/check_prerequisites.py docs/plans/external_benchmark_harness.md` (the repo's standard prerequisite runner if present; otherwise run the commands manually).

## Solution

### Key Elements

- **Dataset adapters** (`tests/benchmarks/datasets/longmemeval_s.py`, `tests/benchmarks/datasets/locomo.py`): Download-on-demand, cache locally, yield `BenchmarkExample` records. License-respecting: never commit raw data.
- **`BenchmarkExample` dataclass** (`tests/benchmarks/datasets/base.py`): `example_id: str`, `history: list[Turn]`, `query: str`, `expected_evidence_ids: set[str]`, `gold_answer: str | None`. Stable shape across both datasets.
- **`ExternalDatasetScenario`** (`tests/benchmarks/scenarios/external_dataset.py`): Subclass of `Scenario`. Ingests one `BenchmarkExample`'s history into Popoto memory under a unique key prefix, runs `ContextAssembler.assemble(query=example.query)`, returns `ScenarioResult` with retrieved IDs mapped back to evidence IDs.
- **Metric extensions** (`tests/benchmarks/metrics/retrieval.py`): Add `recall_at_k(retrieved, relevant, k)` and `latency_percentiles(durations_ms, ps=(50, 95))`. Pure functions, fully tested.
- **`ExternalAggregator`** (`tests/benchmarks/external_aggregator.py`): Streams per-example results into running R@1/5/10, MRR, latency percentiles. Emits JSON + markdown.
- **Runner CLI** (`tests/benchmarks/run_external.py`): `--dataset {longmemeval-s, locomo}`, `--limit N` (for fast iteration), `--profile {current, bm25-only, vector-only}` so #395 can compare retrieval modes without re-implementing the runner.
- **Baseline result artifacts**: First successful run on each dataset committed as the v1 baseline at `tests/benchmarks/results/external/{dataset}_baseline_v1.{json,md}`. Symlinks for `*_latest.{json,md}`.
- **Docs**: New page `docs/benchmarks.md` covering: how to run, what numbers mean, how to compare two runs, how to interpret latency-vs-recall tradeoff.

### Flow

External benchmark runner → loads dataset adapter → for each example: spins up Popoto memory under unique key prefix, ingests history, runs `ContextAssembler.assemble(query)`, captures retrieved IDs + retrieval latency, tears down prefix → streams into aggregator → writes JSON + markdown artifact → updates `_latest` symlink → prints summary table.

### Technical Approach

- **Build on `Scenario`/`ScenarioResult`** — no new harness. The existing `execute()` lifecycle (setup/run/teardown with timing + per-prefix cleanup) is exactly what's needed; one example = one scenario invocation.
- **Decouple retrieval latency from ingest latency** — the issue specifies "latency (p50/p95)". Reviewers will read this as *retrieval* latency. Time `ContextAssembler.assemble(...)` separately from history ingestion and store both on `ScenarioResult.metadata`; aggregator reports retrieval latency in the primary table and ingest latency in a secondary line.
- **Evidence ID mapping** — the trickiest piece. LongMemEval's expected-evidence and LoCoMo's grounding spans both reference *turns in the conversation*. We must map ingested memories back to their source turn so retrieved memory IDs can be compared against `expected_evidence_ids`. Strategy: when ingesting, the scenario records `memory_id -> source_turn_id` and the scenario's `run()` translates `retrieved_memory_ids → retrieved_evidence_ids` before returning. This translation happens *inside the scenario*, not the aggregator, so the aggregator stays dataset-agnostic.
- **Result determinism** — embedding-driven retrieval is not bit-deterministic, but R@K should be stable enough across runs that #395 can detect meaningful deltas. Commit the *first* clean run as `baseline_v1`. Subsequent CI runs (if enabled — Open Question 2) compare against `baseline_v1` with a tolerance band, not exact match.
- **No Redis modules** — Reconfirmed against the `feedback_valkey_compatibility.md` memory rule. `BM25Field`, `DecayingSortedField`, geo, sorted sets, and basic vector paths are all module-free.
- **Dataset cache layout**: `tests/benchmarks/datasets/_cache/{dataset_name}/{file}` with a `.gitignore` entry so the cache never enters git.
- **Failure mode for missing dataset**: If first-run download fails (network, upstream URL drift), the runner prints a clear remediation message pointing at the upstream URL and exits non-zero. No silent fallback to synthetic data.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] No new `except Exception: pass` blocks. The dataset adapter wraps `requests.get` failures with a clear remediation message and re-raises (or exits with a typed exit code).
- [ ] `ExternalDatasetScenario.run()` does NOT swallow ingestion or retrieval exceptions — they propagate to `Scenario.execute()` which already records them on `ScenarioResult.error_message` and continues to the next example. Verify by stubbing `ContextAssembler.assemble` to raise and confirming the example is reported as `status="error"` in the artifact.

### Empty/Invalid Input Handling
- [ ] Dataset adapter behavior on empty/malformed example: skipped with a logged warning, counted in the artifact's `skipped_examples` field, never silently dropped.
- [ ] `recall_at_k`/`latency_percentiles`: empty input returns `0.0` / `{}` rather than dividing by zero. Tested explicitly.
- [ ] Empty `expected_evidence_ids` (dataset row with no ground truth): example skipped with reason `"no-ground-truth"`, never counted in R@K denominators.

### Error State Rendering
- [ ] Markdown artifact has a clear "Errored examples" section if any example errored, with example_id + first 200 chars of error message. Tested by injecting one forced error.
- [ ] If 100% of examples errored, the runner exits non-zero — do not produce a green-looking artifact with R@5 = 0.

## Test Impact

No existing tests are affected — this is purely additive under `tests/benchmarks/datasets/`, `tests/benchmarks/scenarios/external_dataset.py`, `tests/benchmarks/metrics/retrieval.py` (additive functions only), and `tests/benchmarks/run_external.py`. Existing harness tests (`test_harness.py`, `test_sweep.py`, `test_factory.py`, etc.) cover the internal parametric path which is untouched by this work.

New tests to add:
- [ ] `tests/benchmarks/test_external_metrics.py` — unit tests for `recall_at_k`, `latency_percentiles`. Empty input, single item, k > len cases.
- [ ] `tests/benchmarks/test_dataset_adapters.py` — adapter contract tests against a fixture JSON file (committed, ~5 fake examples) so the adapter shape is tested without network.
- [ ] `tests/benchmarks/test_external_scenario.py` — `ExternalDatasetScenario` against the fixture: confirms evidence-id translation, latency capture, error propagation.
- [ ] `tests/benchmarks/test_run_external.py` — CLI smoke test (`--dataset fixture --limit 3`) producing a JSON artifact, asserting required fields are present.

## Rabbit Holes

- **Trying to beat agentmemory's 95.2% in this PR** — explicitly out of scope per the issue. This plan is measurement-only. Optimization belongs to #395 / #396.
- **Building a generic dataset-loader framework** — there are only two datasets in scope. Two concrete adapters with a shared dataclass beats an abstract loader registry. Refactor to a registry only if a third dataset shows up.
- **CI integration in this PR** — Open Question 2. Even if the answer is "yes, run on memory PRs", the CI plumbing is a separate cost (cache restore, time budget, baseline-vs-current comparison logic). Land the runner first; wire it to CI in a follow-up if requested.
- **Embedding model bake-off** — Open Question 3 picks one. Do not run a sweep across embedding models in this plan; that is an optimization activity, not a measurement activity.
- **Per-turn streaming ingest** — LongMemEval and LoCoMo histories can be long. The simplest correct ingestion is "ingest the whole history before querying". Resist the urge to model online/streaming ingest in this plan; if the lifecycle work in #396 changes ingest semantics, the runner can be re-run.

## Risks

### Risk 1: Evidence-ID mapping is wrong for one or both datasets
**Impact:** R@K numbers are silently incorrect; the baseline misleads #395 and #396. Worst case, "no improvement" looks like "improvement" because we're comparing wrong things.
**Mitigation:** For the first 10 examples of each dataset, manually hand-verify the `retrieved → evidence` mapping against the dataset's published gold spans before committing `baseline_v1`. Print top-3 retrieved IDs + gold IDs side-by-side in the markdown artifact's first run so a reviewer can sanity-check.

### Risk 2: Dataset upstream changes URL or schema
**Impact:** Adapter breaks; benchmark unreproducible.
**Mitigation:** Pin the dataset source revision (git SHA or release tag) in the adapter's module-level constant. Record the resolved hash of the downloaded file in the result artifact. If upstream drifts, the failing adapter is the first sign — better than silently scoring against a different dataset.

### Risk 3: Latency p50/p95 dominated by setup work obscures retrieval cost
**Impact:** "Latency" number is meaningless for comparison with #395 (which only changes retrieval, not ingestion).
**Mitigation:** Measure retrieval latency and ingestion latency separately. The primary headline is retrieval p50/p95. Ingestion latency is reported as a secondary line for completeness.

### Risk 4: Embedding model choice locks in a number that's not comparable to peers
**Impact:** Our 95.2%-equivalent number is incomparable to agentmemory because we used a different embedding model.
**Mitigation:** Document the embedding model + version in every result artifact's `config` block. Pick a widely-used default (see Open Question 3). Future runs that change the embedding model write to a new baseline file, not over the existing one.

## Race Conditions

No race conditions identified. The runner is single-process, single-threaded, and each example is fully serialized: setup → ingest → query → teardown → next example. Each example uses a unique Redis key prefix via the existing `Scenario._prefix = f"bench:{uuid.uuid4().hex[:8]}:"` pattern, eliminating cross-example interference. Redis operations within a single example are sequential. If a future parallel-runner variant is added, this section will need revisiting — but the v1 runner is serial by design (correctness over throughput for a baseline-establishing artifact).

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #395] Defaulting `ContextAssembler` to hybrid retrieval. This plan measures the *current* default — issue #395 will use the baseline this plan establishes to demonstrate delta.
- [SEPARATE-SLUG #396] Memory lifecycle (working/episodic/semantic) + consolidation + auto-forget. Same rationale — #396 ships with a measurable delta against this plan's baseline.
- [SEPARATE-SLUG #394] Beating agentmemory's 95.2% R@5 number. Explicitly out of scope per issue body. This issue establishes the measurement instrument; optimization is downstream work tracked under #395/#396 and any follow-ups they spawn.
- [EXTERNAL] Uploading the result artifacts to a public dashboard / leaderboard. If the team later wants a public scoreboard, that needs human decisions about hosting and presentation; not in this plan.

## Update System

No update system changes required — this feature is purely internal to `tests/benchmarks/` and developer-run. Nothing here ships to end users; nothing is propagated across machines.

## Agent Integration

No agent integration required — this is a tests/benchmarks harness invoked manually (and optionally from CI). It is not exposed via MCP and is not callable by the conversational agent. The runner is a developer/CI tool only.

## Documentation

### Feature Documentation
- [ ] Create `docs/benchmarks.md` covering: what the harness measures, how to run it, how to read the result artifacts, how to add a new dataset adapter, and a permalink note that PRs touching `ContextAssembler` or memory recipes should re-run the benchmark and link the result diff.
- [ ] Add a one-line entry in `docs/recipes.md` (or the docs index) pointing to `docs/benchmarks.md`.

### External Documentation Site
- [ ] MkDocs build passes (`mkdocs build --strict`). New page is listed in `mkdocs.yml` nav.

### Inline Documentation
- [ ] Each new module under `tests/benchmarks/datasets/` and `tests/benchmarks/scenarios/external_dataset.py` has a module-level docstring stating purpose, the dataset citation/URL, and the license posture (load-on-demand, no redistribution).
- [ ] `BenchmarkExample` dataclass has per-field docstring lines.
- [ ] `run_external.py --help` produces a clear usage block.

## Success Criteria

- [ ] `python -m tests.benchmarks.run_external --dataset longmemeval-s` produces R@1/R@5/R@10, MRR, retrieval latency p50/p95, ingest latency p50/p95, plus a JSON artifact and a markdown artifact under `tests/benchmarks/results/external/`.
- [ ] Same for `--dataset locomo`.
- [ ] `baseline_v1` artifacts for both datasets committed and referenced from `docs/benchmarks.md`.
- [ ] Datasets are downloaded on demand, cached under a gitignored path, and never committed.
- [ ] No Redis-module dependencies introduced. Runs on Valkey (verified by inspecting all new code paths for `BF.*`, `CMS.*`, `FT.*`, `JSON.*`, `TS.*`).
- [ ] Markdown artifact includes config block (git SHA, dataset hash, embedding model, Popoto version, redis/valkey version).
- [ ] New metric functions `recall_at_k` and `latency_percentiles` have unit tests covering empty, single-item, and `k > len` cases.
- [ ] Scenario evidence-ID mapping hand-verified for the first 10 examples per dataset before `baseline_v1` is committed.
- [ ] `docs/benchmarks.md` exists, is included in `mkdocs.yml`, and `mkdocs build --strict` passes.
- [ ] `pytest tests/benchmarks/ -x -q` passes (new tests included).
- [ ] Tests pass (`/do-test`).
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

### Team Members

- **Builder (dataset-adapters)**
  - Name: `dataset-builder`
  - Role: Build `BenchmarkExample` dataclass + LongMemEval-S and LoCoMo adapters with on-demand download/cache, plus the fixture dataset used for tests.
  - Agent Type: builder
  - Resume: true

- **Builder (scenario + metrics)**
  - Name: `scenario-builder`
  - Role: Build `ExternalDatasetScenario`, add `recall_at_k` + `latency_percentiles` to `metrics/retrieval.py`, build `ExternalAggregator`.
  - Agent Type: builder
  - Resume: true

- **Builder (runner + docs)**
  - Name: `runner-builder`
  - Role: Build `tests/benchmarks/run_external.py` CLI, run first baseline on each dataset, commit `baseline_v1` artifacts, write `docs/benchmarks.md` and update `mkdocs.yml`.
  - Agent Type: builder
  - Resume: true

- **Validator (correctness + Valkey)**
  - Name: `external-validator`
  - Role: Verify no Redis-module commands; verify evidence-ID mapping by hand-checking 10 examples per dataset; verify result artifacts contain required fields; verify `mkdocs build --strict` passes.
  - Agent Type: validator
  - Resume: true

### Available Agent Types

Standard Tier 1 set (`builder`, `validator`, `documentarian`). No specialists required.

## Step by Step Tasks

### 1. Build dataset adapters + fixture
- **Task ID**: build-adapters
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_dataset_adapters.py` (create); fixture JSON file under `tests/benchmarks/datasets/_fixtures/` exists.
- **Informed By**: Freshness Check (confirmed `tests/benchmarks/` shape), Research (dataset schemas)
- **Assigned To**: dataset-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `tests/benchmarks/datasets/__init__.py`.
- Create `tests/benchmarks/datasets/base.py` with `BenchmarkExample` dataclass: `example_id`, `history` (list of turn dicts), `query`, `expected_evidence_ids` (set), `gold_answer` (optional), `source_dataset` (str).
- Create `tests/benchmarks/datasets/longmemeval_s.py`: on-demand download to `tests/benchmarks/datasets/_cache/longmemeval_s/` (gitignored via a new `.gitignore` entry in that dir), pinned upstream revision constant, iterator that yields `BenchmarkExample`s.
- Create `tests/benchmarks/datasets/locomo.py`: same pattern for LoCoMo.
- Add `tests/benchmarks/datasets/_cache/` to the repo's gitignore.
- Create `tests/benchmarks/datasets/_fixtures/mini.json` with ~5 hand-built examples in the unified `BenchmarkExample`-compatible shape (committed; small, license-free).
- Write `tests/benchmarks/test_dataset_adapters.py` against the fixture: shape conformance, iterator stability, skip-on-empty-evidence behavior.

### 2. Build scenario + metric extensions + aggregator
- **Task ID**: build-scenario-metrics
- **Depends On**: build-adapters
- **Validates**: `tests/benchmarks/test_external_metrics.py` (create), `tests/benchmarks/test_external_scenario.py` (create).
- **Informed By**: Freshness Check (Scenario base + retrieval metrics confirmed), Data Flow section.
- **Assigned To**: scenario-builder
- **Agent Type**: builder
- **Parallel**: false
- Extend `tests/benchmarks/metrics/retrieval.py` with `recall_at_k(retrieved, relevant, k) -> float` and `latency_percentiles(durations_ms, ps=(50, 95)) -> dict[int, float]`. Pure functions, no Redis dependency.
- Create `tests/benchmarks/scenarios/external_dataset.py` defining `ExternalDatasetScenario(Scenario)` that takes a `BenchmarkExample` + a profile name. `setup()` ingests `history` into Popoto memory under `self._prefix`, recording `memory_id -> source_turn_id`. `run()` times `ContextAssembler.assemble(query=...)` independently from ingest, translates retrieved memory IDs back to evidence IDs, returns `ScenarioResult` with retrieval and ingest latency on `metadata`.
- Create `tests/benchmarks/external_aggregator.py`: streaming aggregator over `ScenarioResult`s producing R@1/5/10, MRR, retrieval p50/p95, ingest p50/p95, errored-example count, skipped-example count.
- Write metric tests covering empty inputs, single items, `k > len(retrieved)`.
- Write scenario tests against the fixture dataset, including a forced-error case verifying error propagation per Failure Path Test Strategy.

### 3. Build runner CLI + run baselines + commit artifacts
- **Task ID**: build-runner-baselines
- **Depends On**: build-scenario-metrics
- **Validates**: `tests/benchmarks/test_run_external.py` (create); committed artifacts `tests/benchmarks/results/external/longmemeval_s_baseline_v1.{json,md}` and `tests/benchmarks/results/external/locomo_baseline_v1.{json,md}`.
- **Informed By**: Solution / Technical Approach.
- **Assigned To**: runner-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/benchmarks/run_external.py`: `--dataset {longmemeval-s, locomo, fixture}`, `--limit N`, `--profile {current, bm25-only, vector-only}` (only `current` is required for v1; the other two are placeholders for #395). Writes JSON + markdown to `tests/benchmarks/results/external/{dataset}_{timestamp}.{json,md}` and updates `{dataset}_latest.{json,md}` symlinks. Includes config block (git SHA, dataset upstream revision, dataset content hash, embedding model name, Popoto version, Redis/Valkey server version).
- Write CLI smoke test using `--dataset fixture --limit 3` asserting the JSON artifact contains required fields.
- Hand-verify evidence-ID mapping on the first 10 examples per dataset (per Risk 1 mitigation) before running the full set.
- Run the full LongMemEval-S and LoCoMo benchmarks. Copy the result files to `*_baseline_v1.{json,md}`. Commit them.

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: build-runner-baselines
- **Assigned To**: runner-builder
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/benchmarks.md` covering: what the harness measures, how to run, how to read artifacts, latency-vs-recall caveats, how to add a new dataset adapter, the pattern for `*_baseline_v1` artifacts and how subsequent issues (#395, #396) should reference them.
- Add `docs/benchmarks.md` to `mkdocs.yml` nav. Confirm `mkdocs build --strict` passes.
- Add a one-line pointer in `docs/recipes.md` (or top-level docs index) to the new page.

### 5. Final validation
- **Task ID**: validate-all
- **Depends On**: build-adapters, build-scenario-metrics, build-runner-baselines, document-feature
- **Assigned To**: external-validator
- **Agent Type**: validator
- **Parallel**: false
- Confirm no Redis-module commands appear in new code (`grep -rE '(BF|CMS|FT|TS|JSON)\\.' tests/benchmarks/datasets tests/benchmarks/scenarios/external_dataset.py tests/benchmarks/external_aggregator.py tests/benchmarks/run_external.py` returns no matches).
- Run `pytest tests/benchmarks/ -x -q` — all pass.
- Spot-check 5 random examples from each baseline artifact: do retrieved IDs and gold IDs look sensible?
- Verify `mkdocs build --strict` passes.
- Verify baseline artifacts committed and referenced from `docs/benchmarks.md`.
- Verify `.gitignore` excludes `tests/benchmarks/datasets/_cache/`.
- Report pass/fail.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Benchmark tests pass | `pytest tests/benchmarks/ -x -q` | exit code 0 |
| Format clean | `python -m black --check src/ tests/` | exit code 0 |
| Type clean (new files only) | `mypy tests/benchmarks/datasets tests/benchmarks/scenarios/external_dataset.py tests/benchmarks/external_aggregator.py tests/benchmarks/run_external.py` | exit code 0 |
| Docs build clean | `mkdocs build --strict` | exit code 0 |
| No Redis modules | `grep -rE '(BF\|CMS\|FT\|TS\|JSON)\.' tests/benchmarks/datasets tests/benchmarks/scenarios/external_dataset.py tests/benchmarks/external_aggregator.py tests/benchmarks/run_external.py` | exit code 1 |
| Baseline artifacts committed | `test -f tests/benchmarks/results/external/longmemeval_s_baseline_v1.json && test -f tests/benchmarks/results/external/locomo_baseline_v1.json` | exit code 0 |
| Dataset cache gitignored | `git check-ignore tests/benchmarks/datasets/_cache/anything` | exit code 0 |
| Fixture-mode CLI smoke | `python -m tests.benchmarks.run_external --dataset fixture --limit 3` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique. Empty until run. -->

---

## Open Questions

1. **Dataset cache location & CI cache strategy.** Plan defaults to `tests/benchmarks/datasets/_cache/` gitignored. For CI: should we cache this directory across CI runs (via the CI provider's cache action) to avoid repeated downloads? If yes, what's the cache key strategy — pinned upstream revision? Confirm or override.
2. **CI invocation policy.** Should `python -m tests.benchmarks.run_external` run automatically on PRs that touch `src/popoto/recipes/context_assembler.py`, `src/popoto/recipes/subconscious_memory.py`, or any file under `tests/benchmarks/`? Or strictly manual + a comment-triggered runner? My recommendation: manual for v1 (this issue), wire to CI in a follow-up after #395 stabilizes the retrieval default — running the full external benchmark on every memory PR is expensive and the baseline only matters at #395/#396 milestones. Confirm.
3. **Embedding model choice for vector retrieval in the benchmark.** `ContextAssembler` uses Popoto's vector path which depends on whatever embedder is configured. For the published baseline, which embedding model+version should we lock in? Constraints: must work without Redis modules (Valkey-compat); should be reproducible (pinned version); should be a model peers also use so our numbers are comparable. Candidates from the Popoto codebase / common practice: `sentence-transformers/all-MiniLM-L6-v2`, `BAAI/bge-small-en-v1.5`, or `text-embedding-3-small` (OpenAI, requires API key). My recommendation: `BAAI/bge-small-en-v1.5` — strong public benchmarks, runs locally, free, deterministic. Confirm or pick an alternative.
