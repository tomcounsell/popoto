# Benchmark Harness for Agent-Memory Constants

Systematic parameter sweep framework for tuning Popoto's ~25 behavioral constants,
plus an external benchmark harness for evaluating retrieval quality against
published datasets (LongMemEval-S, LoCoMo).

## Structure

```
tests/benchmarks/
    conftest.py              # Redis fixtures and cleanup
    overrides.py             # Constant override injection context manager
    sweep.py                 # ParameterGrid, SweepRunner, ResultsAggregator
    run_sweeps.py            # CLI entry point for internal parameter sweeps
    run_external.py          # CLI entry point for external dataset benchmarks
    test_harness.py          # Tests for scenarios, metrics, overrides
    test_sweep.py            # Tests for sweep engine
    test_external.py         # Tests for external benchmark (fixture-based, no network)
    test_csr.py              # CI gate for the deterministic CSR harness
    csr/
        __init__.py          # CSR constants (DEFAULT_TOP_K, alert thresholds)
        satisfaction.py      # Assertion engine (InTopK, RanksAbove, ...)
        corpus.py            # PlantedMemory/CsrTestCase schema, plant(), lint
        run_csr.py           # CLI entry point + report writer
        suites/
            default.py       # Seed suite (~8 cases, #408-#416 enumeration)
    metrics/
        retrieval.py         # precision@k, recall@k, nDCG, calibration error, MRR
    scenarios/
        base.py              # Base Scenario class
        external_base.py     # ExternalScenario for dataset-driven benchmarks
        factual_recall.py    # Factual knowledge retrieval
        multi_step_reasoning.py  # Co-occurrence chain retrieval
        temporal_scheduling.py   # Cyclic decay task scheduling
    datasets/
        __init__.py          # BenchmarkItem namedtuple
        longmemeval_s.py     # LongMemEval-S adapter (500 questions)
        locomo.py            # LoCoMo adapter (~350 QA pairs)
        fixtures/
            longmemeval_s_sample.json  # 3-question fixture (offline testing)
            locomo_sample.json         # 2-dialogue fixture (offline testing)
    results/
        sweep_*.json         # Timestamped internal sweep results
        latest.json          # Symlink to most recent internal sweep
        external/
            longmemeval_s_*.{json,md}  # External benchmark reports
            locomo_*.{json,md}         # External benchmark reports
        csr/
            csr_*.{json,md}            # Deterministic CSR reports
```

## Quick Start

```bash
# Internal parameter sweeps (no network, ~6 seconds)
python -m tests.benchmarks.run_sweeps --tier all --interactions

# External benchmark (requires dataset download + Redis):
python -m tests.benchmarks.run_external --dataset longmemeval-s
python -m tests.benchmarks.run_external --dataset locomo

# Deterministic CSR harness (no network, no model download):
pytest tests/benchmarks/test_csr.py -q          # CI gate
python -m tests.benchmarks.csr.run_csr          # write report artifact

# External benchmark smoke test (fixture-based, no download):
python -m tests.benchmarks.run_external \
    --dataset longmemeval-s \
    --fixture tests/benchmarks/datasets/fixtures/longmemeval_s_sample.json \
    --limit 3 --dry-run

# Representative limited run: --limit selects a subset spanning the whole
# (category-grouped) dataset. Default --sample stride is deterministic;
# --sample stratified guarantees every question_type is represented;
# --sample shuffle --seed N is a seeded random sample; --sample head is the
# legacy contiguous prefix (benchmarks only the easiest category — opt-in).
# Reports record sample_mode/seed/limit and a per-question_type breakdown.
python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 12 --sample stratified --seed 0

# Run all tests
pytest tests/benchmarks/ -x -q
```

## Adding a New Constant

1. Add it to `VALID_RANGES` in `overrides.py`
2. If it's a module-level constant, add to `MODULE_CONSTANTS` in `overrides.py`
3. Add its sweep grid to the appropriate tier in `run_sweeps.py`
4. Run the sweep and check results

## Adding a New Scenario

1. Subclass `Scenario` in `scenarios/`
2. Implement `setup()`, `run()`, `teardown()`
3. Return `ScenarioResult` with retrieved_ids, relevant_ids, relevance_scores
4. Add to `ALL_SCENARIOS` in `run_sweeps.py`

## Adding a CsrTestCase

CSR cases live in `csr/suites/default.py` and are typed Python fixtures:

1. Author a small corpus (8–15 `PlantedMemory` items) with a handful of
   relevant memories on a topic and inert distractors.
2. Write the `standard_query` (shares vocabulary with the relevant memories)
   and the `adversarial_query` — a semantically equivalent, hand-authored
   paraphrase. Three authoring rules (the first two enforced at load by the
   lint, which uses the real BM25 tokenizer; the third by the double-run
   determinism test):
   - **No shared indexed tokens with any relevant memory** — a weak
     paraphrase behaves like the standard query.
   - **≥ 1 indexed token shared with a distractor memory** — BM25 must
     return hits, or the lexical path silently falls back to the
     query-blind composite path (`plant()` also enforces this post-plant
     with a real `BM25Field.search`).
   - **No BM25 score ties between assertion-referenced ids** — Lua's
     `table.sort` is unstable, so tied scores reorder run-to-run. Give the
     ids named in `RanksAbove`/`InTopK(k=1)` clearly distinct term overlap
     with the query.
3. Declare typed assertions (`InTopK`, `RanksAbove`, `NoneOlderThan`,
   `CoversTopic`, `Excludes`), set `relevant_ids` (the lint's ground truth),
   and append the case to `SUITE`. The module-load lint rejects rule
   violations and empty assertion lists.
