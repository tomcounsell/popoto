# Benchmarking

Popoto ships with two benchmark harnesses:

1. **Internal parametric sweep** (`tests/benchmarks/run_sweeps.py`) — tunes
   behavioral constants against synthetic scenarios. Covered in `tests/benchmarks/README.md`.

2. **External benchmark harness** (`tests/benchmarks/run_external.py`) — evaluates
   memory retrieval quality against published, named datasets. Covered on this page.

---

## External Benchmark Harness

### Overview

The external harness measures how well Popoto's `ContextAssembler` retrieves
the relevant memory given a natural-language query. It supports two datasets:

| Dataset | Questions | Sessions | Notes |
|---------|-----------|----------|-------|
| **LongMemEval-S** | 500 | ~48 per question | Single ground-truth session per question |
| **LoCoMo** | ~350 QA pairs | 50 dialogues | Text-only; image turns skipped |

### Metrics

| Metric | Definition |
|--------|-----------|
| **Recall@K** | Fraction of relevant items found in top-K retrieved results. For single-relevant-item queries, this is a hit-rate: 1 if the correct session/turn appears in top-K, else 0. |
| **MRR** | Mean Reciprocal Rank — reciprocal of the rank of the first relevant result, averaged over all questions. |
| **p50 latency** | Median wall-clock time for one `assemble()` call (ms). |
| **p95 latency** | 95th-percentile latency (ms). |

Latency measurements cover the `ContextAssembler.assemble()` call only
(not dataset ingestion). Machine metadata (CPU, OS, Python version) is
included in the JSON report for reproducibility.

### Prerequisites

```bash
# Redis or Valkey running on localhost:6379
redis-cli ping   # should return PONG

# Install benchmark optional dependencies
pip install -e ".[benchmark]"

# Verify
pip show huggingface_hub sentence-transformers
```

Disk space: ~300 MB for dataset cache (`~/.cache/popoto_benchmarks/`).

### Running the Benchmark

```bash
# Full LongMemEval-S run (downloads dataset on first run, ~264 MB):
python -m tests.benchmarks.run_external --dataset longmemeval-s

# Full LoCoMo run:
python -m tests.benchmarks.run_external --dataset locomo

# Limit to N questions (faster, good for CI smoke tests):
python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 20

# Dry-run (no report saved):
python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 5 --dry-run

# Offline testing using fixture files (no download required):
python -m tests.benchmarks.run_external \
    --dataset longmemeval-s \
    --fixture tests/benchmarks/datasets/fixtures/longmemeval_s_sample.json

python -m tests.benchmarks.run_external \
    --dataset locomo \
    --fixture tests/benchmarks/datasets/fixtures/locomo_sample.json
```

### CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--dataset` | (required) | `longmemeval-s` or `locomo` |
| `--limit N` | all | Evaluate at most N questions |
| `--dry-run` | off | Print results without saving report files |
| `--fixture PATH` | download | Load dataset from a local JSON file |
| `--output DIR` | `results/external/` | Override output directory |
| `--error-threshold FLOAT` | 0.10 | Exit 1 if error rate exceeds this fraction |

### Report Artifacts

Reports are saved to `tests/benchmarks/results/external/`:

```
tests/benchmarks/results/external/
    longmemeval_s_20260522.json   # per-question detail
    longmemeval_s_20260522.md     # human-readable summary table
    longmemeval_s_latest.json     # symlink to most recent JSON
    longmemeval_s_latest.md       # symlink to most recent Markdown
    locomo_20260522.json
    locomo_20260522.md
    locomo_latest.json
    locomo_latest.md
```

Each JSON report includes:
- `summary` — aggregate Recall@1/5/10, MRR, p50/p95 latency
- `machine` — Python version, OS, CPU count
- `notes` — retrieval mode description
- `questions` — per-question detail (item_id, recall scores, status, errors)

### Baseline Numbers (v1.6.3)

**LongMemEval-S (fixture sample, 3 questions):**

| Metric | Score |
|--------|-------|
| Recall@1 | 0.0000 |
| Recall@5 | 0.0000 |
| Recall@10 | 0.0000 |
| MRR | 0.0000 |

**LoCoMo (fixture sample, 6 QA pairs):**

| Metric | Score |
|--------|-------|
| Recall@1 | 0.0000 |
| Recall@5 | 0.0000 |
| Recall@10 | 0.0000 |
| MRR | 0.0000 |

**Note:** The v1.6.3 baseline uses score-only retrieval (DecayingSortedField).
`ContextAssembler.assemble()` ranks candidates by composite score, not by
query-text similarity. Scores for freshly-ingested items with equal importance
are indistinguishable — the baseline is intentionally a floor.

**Reference:** agentmemory BM25+Vector (all-MiniLM-L6-v2) achieves
Recall@5 = 95.2%, Recall@10 = 98.6%, MRR = 88.2% on LongMemEval-S.
Issue #395 will add hybrid retrieval to close this gap.

### Architecture

```
CLI --dataset longmemeval-s
  → DatasetAdapter (download/cache from HuggingFace)
  → iterate BenchmarkItems (question, history, relevant_ids)
  → for each item:
      ExternalScenario.setup()
        — ingest each turn as a memory record via Popoto Model.save()
        — track session_id → Redis key mapping
      ExternalScenario.run()
        — call ContextAssembler.assemble(query_cues={"topic": query})
        — reverse-map Redis keys → session_ids
        — return ScenarioResult(retrieved_ids, relevant_ids)
      ExternalScenario.teardown()
        — scan and delete all Redis keys for this item
  → compute Recall@1/5/10, MRR, p50/p95 latency
  → write results/external/{dataset}_{YYYYMMDD}.{json,md}
```

**Model class:** `ExternalBenchmarkMemory` — a fresh Popoto Model per
benchmark item with:
- `agent_id`: KeyField (partitions per item)
- `content`: StringField (turn text)
- `importance`: FloatField (fixed at 0.5 for baseline)
- `relevance`: DecayingSortedField (decay_rate=0.5)
- `certainty`: ConfidenceField (initial 0.5)

**Score weights:** `{"relevance": 1.0}` — single-field baseline.

### Redis Compatibility

No Redis modules are used. All operations use standard Redis commands
compatible with both Redis and Valkey:
- `ZADD`, `ZREVRANGEBYSCORE` (sorted sets for indexed fields)
- `SET`, `GET` (model instance storage via msgpack)
- `SCAN`, `DEL` (cleanup)

```bash
# Verify no module commands used:
grep -rn "BF\.\|CMS\.\|TOPK\.\|FT\." tests/benchmarks/  # should return nothing
```

### Extending the Harness

To add a new dataset:
1. Create `tests/benchmarks/datasets/{name}.py` with an `iter_items()` generator
   yielding `BenchmarkItem` namedtuples (same shape as existing adapters).
2. Add a fixture file in `tests/benchmarks/datasets/fixtures/{name}_sample.json`.
3. Add tests to `tests/benchmarks/test_external.py`.
4. Register the dataset slug in `run_external.py`'s `DATASET_CHOICES`.

The `ExternalScenario` base class handles ingestion and teardown automatically.
