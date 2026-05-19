---
status: Planning
type: feature
appetite: Medium
owner: valorengels
created: 2026-05-19
tracking: https://github.com/tomcounsell/popoto/issues/394
last_comment_id:
---

# External Benchmark Harness: LongMemEval-S + LoCoMo Adapters

## Problem

**Current behavior:** Popoto Agent Memory has an internal parametric sweep harness (`tests/benchmarks/`) that tunes ~25 behavioral constants against synthetic scenarios. All metrics are relative to synthetic ground truth; there are no published numbers on any external, named benchmark dataset.

**Desired outcome:** A reproducible benchmark harness that:
1. Loads **LongMemEval-S** (500 Qs, ~48 sessions per Q) and **LoCoMo** (50 multi-session dialogues) datasets on demand.
2. Ingests conversation histories into Popoto's memory primitives (`ContextAssembler`, `SubconsciousMemory`).
3. Reports **Recall@1/5/10**, **MRR**, and **latency (p50/p95)** against external ground truth.
4. Commits a baseline report artifact so future PRs can show measurable delta.

This is the **first issue in a 3-issue sequence**: benchmark harness → default hybrid retrieval → consolidation/decay lifecycle. Each subsequent issue needs the baseline this establishes.

## Freshness Check

**Baseline commit:** `10cf580e35102d19f568c72708930b96345cd10e`
**Issue filed at:** 2026-05-17T23:09:48Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/recipes/context_assembler.py` — still present; file structure as described in issue
- `tests/benchmarks/` — still present with all submodules (metrics/, scenarios/, run_sweeps.py, sweep.py)
- `tests/benchmarks/metrics/retrieval.py` — `mean_reciprocal_rank` and `precision_at_k` functions still present; `recall_at_k` not yet present (this plan adds it)

**Cited sibling issues/PRs re-checked:**
- #304 — BM25Field and RRF fusion — **merged** (2026-03-29); `BM25Field` and `CompositeScoreQuery.fuse()` confirmed in `src/popoto/fields/bm25_field.py`

**Commits on main since issue was filed (touching referenced files):**
- None — repo has no commits since 2026-05-17T23:09:48Z.

**Active plans in `docs/plans/` overlapping this area:** None — no existing plan targets external benchmark integration.

**Notes:** `precision_at_k` covers the "hits in top-k" metric but is named after precision, not recall. Recall@K for these benchmarks is `min(1, hits_in_top_k / |relevant|)` since each query typically has exactly one correct memory item — making Recall@K equivalent to whether the item appears in top-K. The plan adds an explicit `recall_at_k` function to the metrics module for clarity.

## Prior Art

- **Issue #304 / PR merged 2026-03-29**: "Hybrid retrieval: BM25 + RRF fusion for multi-signal ranked search" — shipped `BM25Field`, `CompositeScoreQuery.fuse()`. Directly relevant: these are the retrieval primitives the benchmark will exercise.
- No prior issues found targeting LongMemEval-S or LoCoMo dataset integration.

## Research

**Queries used:**
- "LongMemEval-S benchmark dataset download 2026 memory retrieval evaluation"
- "LoCoMo long conversation memory benchmark dataset download format 2025"
- "agentmemory rohitg00 LongMemEval benchmark R@5 95.2% retrieval approach 2025"

**Key findings:**

- **LongMemEval-S dataset** is 264 MB, 500 questions, ~48 sessions/Q, ~115K tokens. Available from HuggingFace: `xiaowu0162/longmemeval-cleaned`, file `longmemeval_s_cleaned.json`, download via `hf_hub_download`. License: public research use. ([source](https://github.com/xiaowu0162/longmemeval))

- **LoCoMo dataset** is 50 multi-session dialogues (~600 turns, ~16K tokens each) with grounded QA. Official site: [snap-research.github.io/locomo](https://snap-research.github.io/locomo/). Download via HuggingFace or from the repo. ([source](https://arxiv.org/abs/2402.17753))

- **agentmemory reference results**: BM25 + Vector hybrid = 95.2% R@5, 98.6% R@10, 88.2% MRR on LongMemEval-S using `all-MiniLM-L6-v2` embeddings. Pure BM25-alone = 86.2%. These are the numbers to compare against. ([source](https://github.com/rohitg00/agentmemory/blob/main/benchmark/LONGMEMEVAL.md))

- **Embedding model choice**: `all-MiniLM-L6-v2` (sentence-transformers) is the de facto standard for these benchmarks. It runs locally, no API needed, and is Valkey-compat (pure Python scoring, no Redis modules required).

- **Valkey compatibility constraint**: All retrieval must use pure Redis commands (no BM25.*, HNSW modules). Popoto's `BM25Field` (Lua-script based) and `ContentField`/cosine-similarity via sorted sets satisfy this requirement.

## Spike Results

### spike-1: Does LongMemEval-S have a single correct memory item per question, or multiple?
- **Assumption**: "Each query has one ground-truth memory item, making Recall@K = binary (found/not-found in top-K)"
- **Method**: code-read of agentmemory benchmark docs and LongMemEval paper
- **Finding**: LongMemEval questions have one primary evidence session/turn. Recall@K = 1 if the relevant session appears in top-K retrieved results, else 0. Averaged over all 500 questions.
- **Confidence**: high
- **Impact on plan**: `recall_at_k` simplifies to hit-rate at K; no multi-label averaging needed.

### spike-2: Does `huggingface_hub` exist as a dependency or must it be added?
- **Assumption**: "huggingface_hub is not in the current dep set and must be added as an optional dep"
- **Method**: code-read of `pyproject.toml`
- **Finding**: Confirmed below — `huggingface_hub` is absent from deps. It needs to be an optional `[benchmark]` extra to avoid bloating the base install.
- **Confidence**: high
- **Impact on plan**: Add `huggingface_hub` and `sentence-transformers` to `[project.optional-dependencies]` `benchmark` group.

## Data Flow

1. **Entry point**: CLI invocation `python -m tests.benchmarks.run_external --dataset longmemeval-s`
2. **Dataset adapter** (`tests/benchmarks/datasets/longmemeval_s.py`): Downloads/caches `longmemeval_s_cleaned.json` via `hf_hub_download` to `~/.cache/popoto_benchmarks/`. Yields `(conversation_history: list[dict], query: str, evidence_session_id: str)` tuples.
3. **ExternalScenario wrapper** (`tests/benchmarks/scenarios/external_base.py`): For each conversation history, calls `SubconsciousMemory.extract_and_store(turn)` (or equivalent) to ingest all turns into Redis. Then calls `ContextAssembler.assemble(query_cues={"text": query})` to retrieve.
4. **Metrics computation**: Returns `ScenarioResult` with `retrieved_ids` (ordered Redis keys), `relevant_ids` = {evidence_session_id}. `recall_at_k`, `MRR`, and latency computed by extended `retrieval.py`.
5. **Report generation**: `ResultsAggregator` (from `sweep.py`) collects per-question results. Writes `tests/benchmarks/results/external/{dataset}_{date}.json` and `{dataset}_{date}.md`.
6. **Output**: Committed markdown + JSON report as the baseline artifact.

## Architectural Impact

- **New optional dependencies**: `huggingface_hub`, `sentence-transformers` — added under `[project.optional-dependencies].benchmark`. Not in base install.
- **New modules** (all additive, no existing interfaces changed):
  - `tests/benchmarks/datasets/longmemeval_s.py` — dataset adapter
  - `tests/benchmarks/datasets/locomo.py` — dataset adapter
  - `tests/benchmarks/datasets/__init__.py`
  - `tests/benchmarks/scenarios/external_base.py` — base class for external dataset scenarios
  - `tests/benchmarks/run_external.py` — CLI entry point
- **Metrics extension**: `recall_at_k` added to `tests/benchmarks/metrics/retrieval.py` (additive, existing functions unchanged).
- **Results directory**: `tests/benchmarks/results/external/` — new subdirectory for committed report artifacts.
- **Coupling**: New code depends on existing `ContextAssembler` and `SubconsciousMemory` APIs. No new coupling introduced in the other direction.
- **Reversibility**: Fully additive — removing it means deleting the new files and the optional dep. No existing behavior changed.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1-2 (scope alignment on CI integration question)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey running on localhost:6379 | `redis-cli ping` | Benchmark ingests into Redis |
| `huggingface_hub` (optional dep) | `pip show huggingface_hub` | Dataset download |
| `sentence-transformers` (optional dep) | `pip show sentence-transformers` | Embedding generation |
| ~500 MB disk for dataset cache | `df -h ~/.cache` | LongMemEval-S is 264 MB |

Run all checks: `python scripts/check_prerequisites.py docs/plans/external_benchmark_harness.md`

## Solution

### Key Elements

- **Dataset adapters**: Two thin modules in `tests/benchmarks/datasets/` that download/cache external datasets and yield a standard `BenchmarkItem` namedtuple: `(history, query, relevant_ids)`.
- **ExternalScenario base class**: Reuses the existing `Scenario` ABC pattern. Ingests a conversation history into Popoto memory primitives, runs a retrieval query, returns a `ScenarioResult`.
- **recall_at_k metric**: Pure function added to `retrieval.py` alongside existing `precision_at_k`, `ndcg_at_k`, `mean_reciprocal_rank`.
- **run_external.py CLI**: Mirrors `run_sweeps.py` structure; supports `--dataset longmemeval-s|locomo`, `--limit N` (for quick smoke runs), and writes report artifacts.
- **Report artifacts**: Committed markdown summary + JSON detail in `tests/benchmarks/results/external/`.

### Flow

```
CLI --dataset longmemeval-s
  → DatasetAdapter (download/cache JSON from HuggingFace)
  → iterate BenchmarkItems (500 questions)
  → for each item:
      ExternalScenario.setup() — ingest conversation history → SubconsciousMemory / ContextAssembler
      ExternalScenario.run() — assemble(query) → retrieved_ids
      ExternalScenario.teardown() — flushdb prefix
  → compute recall@1/5/10, MRR, p50/p95 latency
  → write results/external/longmemeval_s_YYYYMMDD.{json,md}
```

### Technical Approach

- **Dataset caching**: Use `hf_hub_download` with `local_dir=Path.home() / ".cache/popoto_benchmarks"`. Check for cached file on import; skip download if present.
- **Ingestion strategy**: For each conversation turn in `history`, create a `SubconsciousMemory`-compatible model instance with the turn's content + timestamp. The adapter converts the dataset's session IDs to Redis keys tracked for cleanup.
- **Retrieval**: Call `ContextAssembler.assemble(query_cues={"text": query})` with default settings (no tuning overrides). This exercises the live default retrieval pipeline.
- **Ground truth mapping**: LongMemEval-S evidence is identified by session/turn ID. The adapter stores this as the Redis key suffix so `ScenarioResult.relevant_ids` maps correctly.
- **Embedding**: Use `sentence-transformers` `all-MiniLM-L6-v2` locally — no external API, no Redis modules, Valkey-compatible.
- **Latency**: Measure only the `assemble()` call, not ingestion. Record wall-clock time per query; compute p50/p95 over the dataset.
- **Report format**: Markdown table showing per-dataset aggregate (R@1, R@5, R@10, MRR, p50, p95) + JSON with per-question detail. File named `{dataset}_{YYYYMMDD}.md/json`. Latest run also symlinked as `{dataset}_latest.md/json`.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `hf_hub_download` network failure → adapter should raise `RuntimeError("Dataset unavailable: ...")` with a clear message pointing to manual download instructions. Not silently caught.
- [ ] Redis connection failure during ingestion → propagate as `ConnectionError`; do not continue with partial data.
- [ ] Individual question ingestion failure → log warning with question ID, continue to next question, include error count in final report.

### Empty/Invalid Input Handling
- [ ] Empty `history` list → `ExternalScenario.setup()` should skip ingestion and return `ScenarioResult` with `status="skipped-empty"`.
- [ ] `query` is empty string → `ContextAssembler.assemble()` returns empty; `recall_at_k` = 0 for all K.
- [ ] Dataset JSON missing expected fields → adapter raises `ValueError` with field name and record index.

### Error State Rendering
- [ ] Final report must include `errors: N` count even on partial success.
- [ ] CLI exit code 1 if >10% of questions errored (configurable threshold).

## Test Impact

No existing tests are affected — all new code is additive. The benchmark CLI (`run_external.py`) is not covered by existing `tests/benchmarks/test_harness.py` or `test_sweep.py`.

New tests to add:
- [ ] `tests/benchmarks/test_external.py::test_recall_at_k` — unit test for the new metric function
- [ ] `tests/benchmarks/test_external.py::test_longmemeval_adapter_schema` — validates adapter yields correct `BenchmarkItem` shape from a small fixture file (no download required)
- [ ] `tests/benchmarks/test_external.py::test_locomo_adapter_schema` — same for LoCoMo

## Rabbit Holes

- **Beating agentmemory's 95.2%**: This issue establishes measurement only. Do not tune retrieval parameters here.
- **Full 500-question CI run**: Running all 500 LongMemEval-S questions in CI will be slow (~minutes) and require network/disk. CI integration is out of scope for this issue.
- **Multimodal LoCoMo**: LoCoMo includes images. Vision support is not a Popoto feature. Use text-only turns; document the limitation in the report.
- **Custom embedding model**: `all-MiniLM-L6-v2` is the reference choice. Don't swap it in this issue.
- **Dataset version pinning**: Don't build a full dataset version management system. A single cached file + SHA check is sufficient.

## Risks

### Risk 1: HuggingFace dataset API changes
**Impact:** `hf_hub_download` raises unexpected error; dataset unavailable.
**Mitigation:** Include fallback instructions in the adapter README: "If download fails, manually place `longmemeval_s_cleaned.json` in `~/.cache/popoto_benchmarks/`." The adapter checks for the cached file first.

### Risk 2: ContextAssembler API not compatible with dataset's conversation format
**Impact:** Ingestion schema mismatch; zero recall on all questions.
**Mitigation:** Write the `ExternalScenario` adapter with explicit field mapping. Include a `--dry-run --limit 5` mode that prints ingested records before running retrieval, making schema mismatches visible immediately.

### Risk 3: Latency numbers not reproducible across machines
**Impact:** p50/p95 numbers vary based on hardware; baselines mislead.
**Mitigation:** Report includes machine metadata (CPU, OS, Python version, Redis version) in the JSON. Latency numbers are informational, not pass/fail gates.

## Race Conditions

No race conditions identified — the benchmark runner is single-threaded and synchronous. Each question is ingested, queried, and torn down sequentially. `flushdb` is scoped to the benchmark key prefix, not the entire DB.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #395] Changing the default retrieval mode in `ContextAssembler` — tracked in issue #395.
- [SEPARATE-SLUG #396] Memory lifecycle / consolidation / auto-forget — tracked in issue #396.
- [EXTERNAL] Running the benchmark in CI on every PR — requires CI infrastructure decisions (disk/network budget) that need human sign-off.
- [EXTERNAL] Publishing Popoto's benchmark results to a public leaderboard — requires human review of numbers before publication.

## Update System

No update system changes required — this feature is purely internal to the benchmark harness and `tests/` directory.

## Agent Integration

No agent integration required — this is a standalone benchmark CLI tool. No MCP server changes, no bridge changes.

## Documentation

- [ ] Create `docs/benchmarks.md` describing the external benchmark harness, how to run it, and what the baseline numbers mean.
- [ ] Add link from `docs/recipes.md` to `docs/benchmarks.md` under a "Benchmarking" section.
- [ ] Update `tests/benchmarks/README.md` with the new `datasets/` submodule and `run_external.py` usage.

## Success Criteria

- [ ] `python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 20` completes without error and prints R@K + MRR + latency.
- [ ] `python -m tests.benchmarks.run_external --dataset longmemeval-s` produces a committed report in `tests/benchmarks/results/external/`.
- [ ] Same for `--dataset locomo`.
- [ ] Initial baseline numbers committed to the repo (R@1, R@5, R@10, MRR on both datasets).
- [ ] No Redis-module dependencies (Valkey-compat confirmed: `grep -r "BF\.\|CMS\.\|TOPK\." tests/benchmarks/` returns nothing).
- [ ] `tests/benchmarks/test_external.py` passes without network access (fixture-based tests only).
- [ ] `docs/benchmarks.md` created.
- [ ] Tests pass (`pytest tests/benchmarks/ -x -q`).

## Team Orchestration

### Team Members

- **Builder (datasets)**
  - Name: dataset-builder
  - Role: Implement dataset adapters for LongMemEval-S and LoCoMo; add `recall_at_k` to retrieval metrics
  - Agent Type: builder
  - Resume: true

- **Builder (runner)**
  - Name: runner-builder
  - Role: Implement `ExternalScenario` base class, `run_external.py` CLI, report artifact generation
  - Agent Type: builder
  - Resume: true

- **Validator (benchmark)**
  - Name: benchmark-validator
  - Role: Verify adapters yield correct schema, CLI runs end-to-end with `--limit 5`, report artifact is well-formed
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: benchmark-documentarian
  - Role: Create `docs/benchmarks.md`, update README
  - Agent Type: documentarian
  - Resume: true

### Available Agent Types
(standard Tier 1 set)

## Step by Step Tasks

### 1. Add optional benchmark dependencies to pyproject.toml
- **Task ID**: build-deps
- **Depends On**: none
- **Validates**: `pip show huggingface_hub sentence-transformers` after `pip install -e ".[benchmark]"`
- **Assigned To**: dataset-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `[project.optional-dependencies]` `benchmark` group with `huggingface_hub>=0.23`, `sentence-transformers>=2.7`
- Verify existing `[dev]` extras still install cleanly

### 2. Implement LongMemEval-S dataset adapter
- **Task ID**: build-longmemeval-adapter
- **Depends On**: build-deps
- **Validates**: `tests/benchmarks/test_external.py::test_longmemeval_adapter_schema`
- **Assigned To**: dataset-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `tests/benchmarks/datasets/__init__.py`
- Create `tests/benchmarks/datasets/longmemeval_s.py` with `BenchmarkItem = namedtuple(...)` and `iter_items()` generator
- Create fixture file `tests/benchmarks/datasets/fixtures/longmemeval_s_sample.json` (3-5 questions, no real data)
- Write `tests/benchmarks/test_external.py::test_longmemeval_adapter_schema` using fixture

### 3. Implement LoCoMo dataset adapter
- **Task ID**: build-locomo-adapter
- **Depends On**: build-deps
- **Validates**: `tests/benchmarks/test_external.py::test_locomo_adapter_schema`
- **Assigned To**: dataset-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `tests/benchmarks/datasets/locomo.py` with same `BenchmarkItem` shape
- Create fixture file `tests/benchmarks/datasets/fixtures/locomo_sample.json`
- Write `tests/benchmarks/test_external.py::test_locomo_adapter_schema`

### 4. Add recall_at_k to retrieval metrics
- **Task ID**: build-recall-metric
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_external.py::test_recall_at_k`
- **Assigned To**: runner-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `recall_at_k(retrieved, relevant, k)` to `tests/benchmarks/metrics/retrieval.py`
- Write `tests/benchmarks/test_external.py::test_recall_at_k` with edge cases (empty retrieved, k=0, relevant not in retrieved)

### 5. Implement ExternalScenario base class and run_external.py
- **Task ID**: build-runner
- **Depends On**: build-longmemeval-adapter, build-locomo-adapter, build-recall-metric
- **Validates**: `python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 3 --dry-run`
- **Assigned To**: runner-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/benchmarks/scenarios/external_base.py` with `ExternalScenario(Scenario)` base class
- Implement LongMemEval-S and LoCoMo scenario subclasses
- Create `tests/benchmarks/run_external.py` CLI with `--dataset`, `--limit`, `--dry-run`, `--output` flags
- Implement report artifact generation (JSON + Markdown) in `tests/benchmarks/results/external/`

### 6. Run baseline and commit report artifacts
- **Task ID**: build-baseline
- **Depends On**: build-runner
- **Validates**: `tests/benchmarks/results/external/longmemeval_s_*.md` exists and contains R@5 number
- **Assigned To**: runner-builder
- **Agent Type**: builder
- **Parallel**: false
- Run `python -m tests.benchmarks.run_external --dataset longmemeval-s` (full 500-question run)
- Run `python -m tests.benchmarks.run_external --dataset locomo` (full 50-conversation run)
- Commit both report artifacts (JSON + MD) to `tests/benchmarks/results/external/`

### 7. Validate end-to-end
- **Task ID**: validate-benchmark
- **Depends On**: build-baseline
- **Assigned To**: benchmark-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify `tests/benchmarks/test_external.py` passes (`pytest tests/benchmarks/test_external.py -x -q`)
- Verify report artifacts exist and contain expected fields (R@1, R@5, R@10, MRR, p50_ms, p95_ms)
- Verify no Redis-module usage (`grep -r "BF\.\|CMS\.\|TOPK\." tests/benchmarks/` returns nothing)
- Verify `pip install -e ".[benchmark]"` installs `huggingface_hub` and `sentence-transformers`

### 8. Documentation
- **Task ID**: document-benchmarks
- **Depends On**: validate-benchmark
- **Assigned To**: benchmark-documentarian
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/benchmarks.md` — how to run, what each dataset tests, how to interpret R@K + MRR
- Add "Benchmarking" entry to `docs/recipes.md`
- Update `tests/benchmarks/README.md` with new submodules

### 9. Final validation
- **Task ID**: validate-all
- **Depends On**: document-benchmarks
- **Assigned To**: benchmark-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/benchmarks/ -x -q`
- Verify all success criteria met
- Confirm baseline numbers are human-readable in committed markdown report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Benchmark tests pass | `pytest tests/benchmarks/test_external.py -x -q` | exit code 0 |
| Full test suite clean | `pytest tests/ -x -q` | exit code 0 |
| No Redis modules used | `grep -rn "BF\.\|CMS\.\|TOPK\." tests/benchmarks/` | exit code 1 (no matches) |
| Baseline report exists | `ls tests/benchmarks/results/external/longmemeval_s_*.md` | exit code 0 |
| LoCoMo report exists | `ls tests/benchmarks/results/external/locomo_*.md` | exit code 0 |
| Optional dep installable | `pip install -e ".[benchmark]" && pip show huggingface_hub` | exit code 0 |
| Docs build clean | `mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. **CI integration**: Should `run_external --limit 20` (smoke test) run in CI on PRs that touch `src/popoto/recipes/` or `src/popoto/fields/`? This requires disk (~300 MB cached) and ~60s runtime. Out of scope for this plan but needs a human decision before the next issue lands.

2. **Embedding model in ContextAssembler**: The benchmark will use `all-MiniLM-L6-v2` for vector retrieval. Does `ContextAssembler` currently support embedding-based retrieval, or does this benchmark only exercise BM25 + score-based retrieval? If vector retrieval isn't wired in yet, the baseline numbers will understate potential and should be labeled "BM25-only baseline" in the report.
