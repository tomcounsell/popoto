# Benchmarking

Popoto ships with three benchmark harnesses:

1. **Internal parametric sweep** (`tests/benchmarks/run_sweeps.py`) — tunes
   behavioral constants against synthetic scenarios. Covered in `tests/benchmarks/README.md`.

2. **External benchmark harness** (`tests/benchmarks/run_external.py`) — evaluates
   memory retrieval quality against published, named datasets. Covered on this page.

3. **Deterministic CSR harness** (`tests/benchmarks/csr/`) — a per-PR CI gate
   asserting named retrieval properties with deterministic scoring. Covered on
   this page (see [Deterministic CSR Harness](#deterministic-csr-harness)).

---

## External Benchmark Harness

### Overview

The external harness measures how well Popoto's `ContextAssembler` retrieves
the relevant memory given a natural-language query, and can optionally run an
end-to-end judged-answer stage (retrieve → generate → LLM-judge) for
leaderboard-comparable accuracy numbers (see
[Judged-Answer Accuracy](#judged-answer-accuracy-tier-5)). It supports two datasets:

| Dataset | Questions | Sessions | Notes |
|---------|-----------|----------|-------|
| **LongMemEval-S** | 500 | ~48 per question | Single ground-truth session per question |
| **LoCoMo** | 1986 QA pairs | 10 dialogues | Multi-session dict schema; image turns ingested via BLIP caption so image-evidence stays retrievable |

### Metrics

| Metric | Definition |
|--------|-----------|
| **Recall@K** | Any-hit hit-rate: 1.0 if **any** relevant session/turn appears in the top-K retrieved results, else 0.0. This is the definition used by the published reference numbers and preserves the invariant MRR ≤ Recall@K. (A fractional variant — proportion of multi-evidence items found — is available as `fractional_recall_at_k` for per-evidence coverage analysis, but is not the headline metric.) |
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

# Limit to N questions (faster, good for CI smoke tests).
# By default --limit takes a *representative* sample across the whole
# dataset (--sample stride), so small runs reflect every question_type
# rather than the first N records (which are all one easy category):
python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 20

# Strongest small-run guarantee — every category proportionally represented:
python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 12 --sample stratified --seed 0

# Reproduce a legacy contiguous-prefix run (warns):
python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 20 --sample head

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
| `--limit N` | all | Evaluate at most N questions (sampled per `--sample`) |
| `--sample MODE` | `stride` | How `--limit` selects: `stride` (even spread, representative), `stratified` (proportional per `question_type`), `shuffle` (seeded random), `head` (legacy contiguous prefix). Ignored without `--limit`. |
| `--seed N` | 0 | Seed for `shuffle`/`stratified` sampling |
| `--retrieval-mode MODE` | `lexical` | `lexical` (BM25 only), `hybrid` (BM25 + vector via RRF), or `vector` (`EmbeddingField` only, pure cosine — diagnostic). See [Retrieval Modes](#retrieval-modes). |
| `--dry-run` | off | Print results without saving report files |
| `--fixture PATH` | download | Load dataset from a local JSON file |
| `--output DIR` | `results/external/` | Override output directory |
| `--error-threshold FLOAT` | 0.10 | Exit 1 if retrieval error rate exceeds this fraction |
| `--judged` | off | Run the end-to-end judged-answer stage (retrieve → generate → LLM-judge). Requires `OPENAI_API_KEY`; skips gracefully without one. `lexical`/`hybrid` only. See [Judged-Answer Accuracy](#judged-answer-accuracy-tier-5). |
| `--judge-error-threshold FLOAT` | 0.25 | With `--judged`, exit 1 if the judge-error (API failure) rate exceeds this fraction. Independent of `--error-threshold`. |

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

Non-lexical runs suffix the retrieval mode into both the dated and `_latest`
filenames (e.g. `longmemeval_s_20260703_hybrid.json`,
`longmemeval_s_latest_hybrid.json`; `vector` uses the `_vector` suffix), so a
hybrid or vector run never overwrites the lexical baseline artifacts or their
symlinks. `--judged` runs add an independent `_judged` suffix that composes with
the mode suffix (e.g. `locomo_20260714_judged.*` for lexical+judged,
`locomo_20260714_hybrid_judged.*` for hybrid+judged), so a judged run never
clobbers a retrieval-only artifact.

Committing a `_latest` artifact (any mode) auto-publishes it to the
[Benchmarks results pages](benchmarks/results/index.md) on the docs site on the
next deploy — the `lexical`, `hybrid`, and `vector` pages per dataset are generated
from these artifacts by `docs/scripts/gen_benchmark_pages.py`, with no
hand-edited prose tables. An artifact for a mode nobody wired a page for is
reported as a loud build-time warning rather than silently dropped.

Each JSON report includes:
- `summary` — aggregate Recall@1/5/10, MRR, p50/p95 latency
- `sampling` — `sample_mode` / `seed` / `limit` used for the run (so a report is reproducible from itself)
- `by_question_type` — per-category Recall@1/5/10 + MRR + count breakdown
- `machine` — Python version, OS, CPU count
- `notes` — retrieval mode description
- `questions` — per-question detail (item_id, recall scores, status, errors, and `metadata.question_type`)
- `judge` / `judged` — present only on `--judged` runs: the pinned judge identity (model, prompt SHA-256, protocol, temperature) and the judged-accuracy aggregate (see [Judged-Answer Accuracy](#judged-answer-accuracy-tier-5))

### Retrieval Modes

`--retrieval-mode` selects how the harness retrieves candidates. `lexical` and
`hybrid` drive `ContextAssembler.assemble()`; field presence on the per-item model
determines the effective mode (`retrieval_mode="auto"`). `vector` is a harness-local
diagnostic that **bypasses** the assembler and ranks by pure cosine directly:

| Mode | Fields | Method | Cost |
|------|--------|--------|------|
| `lexical` (default) | `BM25Field` | none (BM25 ranking) | no model download; fast |
| `hybrid` | `BM25Field` + `EmbeddingField` | BM25 + vector via **RRF (k=60)** | one-time ~90 MB `all-MiniLM-L6-v2` download; slower (CPU embedding) |
| `vector` | `EmbeddingField` only | pure cosine (no BM25, no RRF) | same ~90 MB `all-MiniLM-L6-v2` download as hybrid; slower (CPU embedding) |

Hybrid is **Valkey-safe**: vector similarity is computed in-process with numpy
cosine over `EmbeddingField` `.npy` files — no RediSearch, no vector-search modules,
no `FT.*` / `BF.*` commands. The vector signal uses the local
[`SentenceTransformersProvider`](fields.md#sentencetransformersprovider) (no API key).

!!! warning "Hybrid is not universally better than lexical"
    Hybrid retrieval is **not** a strict upgrade over `lexical`. Its unweighted
    RRF (k=60) gives the dense arm equal say, which helps on paraphrastic queries
    (hybrid wins on LongMemEval-S at every _k_) but **hurts on coreference-heavy,
    multi-session dialogue**, where topically-similar-but-wrong turns displace
    correct BM25 hits — hybrid **underperforms** `lexical` on LoCoMo at every _k_
    (Recall@1 0.167 vs 0.299; see the LoCoMo results below). If your
    workload resembles long, multi-session conversation, benchmark both modes on
    your own data before defaulting to hybrid; `lexical` remains the safe default.
    Weighted / query-adaptive fusion to make hybrid trustworthy across
    conversation shapes is tracked in
    [issue #457](https://github.com/tomcounsell/popoto/issues/457).

`vector` is a **harness-local diagnostic** (issue #455): it bypasses
`ContextAssembler` entirely — auto-mode would resolve an embedding-only model to the
query-blind `composite` path, so the harness ranks by raw cosine over the
`EmbeddingField` directly. It isolates **only** the dense arm; it does **not**
exercise the graph / co-occurrence arm that also lives inside `hybrid`. Use it to
diagnose the vector signal's standalone strength, not as a production retrieval path.

```bash
# Lexical (default) — BM25 only, no model download:
python -m tests.benchmarks.run_external --dataset longmemeval-s

# Hybrid — BM25 + vector (downloads MiniLM on first run):
pip install -e ".[benchmark]"
python -m tests.benchmarks.run_external --dataset longmemeval-s --retrieval-mode hybrid

# Vector — pure cosine over EmbeddingField only (diagnostic; downloads MiniLM):
python -m tests.benchmarks.run_external --dataset longmemeval-s --retrieval-mode vector
```

**LongMemEval-S, 500 questions (any-hit Recall):**

| Mode | Recall@1 | Recall@5 | Recall@10 | MRR |
|------|---------:|---------:|----------:|----:|
| `lexical` (BM25) | 0.856 | 0.952 | 0.978 | 0.899 |
| `hybrid` (BM25+vector, RRF) | **0.894** | **0.986** | **0.992** | **0.932** |
| agentmemory reference (BM25+vector) | — | 0.952 | 0.986 | 0.882 |

Hybrid outperforms the lexical baseline on every metric and exceeds the
agentmemory reference at Recall@5 (0.986 vs 0.952), Recall@10 (0.992 vs 0.986),
and MRR (0.932 vs 0.882). The vector signal helps most where keyword overlap is
weakest: `single-session-preference` Recall@1 rises from 0.40 (lexical) to 0.70,
and every question category reaches Recall@5 ≥ 0.967. Full per-category detail
is in the committed artifact
(`tests/benchmarks/results/external/longmemeval_s_latest_hybrid.json` —
500/500 questions, zero errors, every item resolved to the true hybrid path).

**LoCoMo, 1986 questions (any-hit Recall):**

| Mode | Recall@1 | Recall@5 | Recall@10 | MRR |
|------|---------:|---------:|----------:|----:|
| `lexical` (BM25) | 0.2986 | 0.5534 | 0.6400 | 0.4124 |
| `hybrid` (BM25+vector, RRF) | 0.1667 | 0.4235 | 0.5403 | 0.2835 |

Unlike LongMemEval-S, hybrid **underperforms** lexical on LoCoMo across every
metric — a real, measured result (no retrieval tuning was performed; this
harness run is measurement-only per issue #447). Full per-category detail is
in the committed artifact
(`tests/benchmarks/results/external/locomo_latest_hybrid.json` — 1986/1986
questions, zero errors).

#### Category 5 ("adversarial") — evidence audit and leaderboard-parity slice

LoCoMo's category 5 is historically the hardest category industry-wide (the
original paper reports humans ≈89 F1 vs LLMs ≈2 F1), and most public
leaderboards exclude it. Popoto scores it *comparably* to the other categories
(n=446, Recall@1=0.3341, Recall@5=0.6031, Recall@10=0.6883, MRR=0.4581), which
warranted an audit (issue #454) of whether that number means anything.

**Audit finding — the cat-5 spans are genuinely meaningful for retrieval in
this snapshot.** Direct inspection of all 446 category-5 items in the cached
LoCoMo dataset (the `snap-research/locomo` HuggingFace release, file
`locomo10.json`, cached locally at `~/.cache/popoto_benchmarks/locomo.json` —
downloaded on first run, not checked into the repo) shows:

- **446/446 carry a populated `evidence` field** (so the harness's empty-
  relevant-set special case never fires — category 5 is scored as ordinary
  retrieval, like every other category).
- **444/446 have an answerable `adversarial_answer` whose evidence span
  directly supports it.** For example, *"What did Caroline realize after her
  charity race?"* → `adversarial_answer="self-care is important"`, evidence turn
  D2:3 = *"I'm starting to realize that self-care is really important."* Only
  **2/446** are refusal-style ("Not mentioned").

So in **this** snapshot, category 5 is **not** a refusal category; it is a set
of grounded, answerable retrieval questions. Scoring it as any-hit retrieval is
measuring retrieval — the same thing measured for every category — not
"measuring the wrong thing."

**Why the apparent paradox (paper's ≈2 F1 vs our 0.33 Recall@1) dissolves:** it
is a **metric-family difference**, not a scoring artifact. The paper's ≈2 F1 is
*end-to-end judged-answer* accuracy (retrieve → generate → judge); Popoto's
0.3341 is *retrieval any-hit recall* (did the evidence turn get retrieved).
These families are not convertible (see the metric-family note above).
Adversarial is hard to *answer/judge*, not hard to *retrieve* — so a normal
retrieval-recall number on it is expected and honest.

!!! warning "cat-5 is not a refusal-capability signal"
    This snapshot's category 5 is answerable and evidence-grounded. Its recall
    number is **not** evidence that Popoto refuses unanswerable queries, and it
    is **not** comparable to systems that report a refusal-adversarial metric or
    that drop the category. Refusal capability (confidence-gated retrieval) is
    tracked separately in
    [issue #463](https://github.com/tomcounsell/popoto/issues/463) and must be
    evaluated on a dataset that actually contains unanswerable questions — a
    "precision of no-answer decisions" metric has no signal on a snapshot where
    only 2/446 items are unanswerable.

**Leaderboard-parity slice (4-category, n=1540).** For apples-to-apples
comparison with the common no-adversarial boards (1,540-QA, 4-category), the
harness reports a parity slice that re-aggregates the run with category 5
excluded — `1986 − 446 = 1540`, exactly the leaderboard variant count. The
slice is computed from the committed `by_question_type` breakdown (no re-run
needed) by `leaderboard_parity_slice()` and emitted into each LoCoMo report as
a `leaderboard_parity` block:

| Slice | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|------|--:|---------:|---------:|----------:|----:|
| `lexical`, full 5-category | 1986 | 0.2986 | 0.5534 | 0.6400 | 0.4124 |
| `lexical`, 4-category parity | 1540 | 0.2883 | 0.5390 | 0.6260 | 0.3991 |
| `hybrid`, full 5-category | 1986 | 0.1667 | 0.4235 | 0.5403 | 0.2835 |
| `hybrid`, 4-category parity | 1540 | 0.1552 | 0.4065 | 0.5181 | 0.2686 |

Category 5 sits near the mean, so excluding it barely moves the numbers —
further evidence it is neither anomalously easy nor a scoring artifact. (The
slice re-aggregates 4-decimal per-category means, so it can differ from a raw
re-aggregation in the 4th place; reproducibility from the committed artifact is
the priority.)

**Recommendation (pending maintainer sign-off — scoring semantics is
policy-level).** Keep category 5 in the full 5-category aggregate (the evidence
audit shows its spans are meaningful), publish the 4-category parity slice
alongside it for leaderboard comparability, and always carry the caveat above.
A refusal metric is not applicable to this dataset and is deferred to #463.

#### Confidence-gated retrieval — refusal precision (issue #463)

Issue #463 shipped an opt-in confidence gate on `ContextAssembler`
(`confidence_gate_threshold` / `confidence_gate_mode`; see
[ContextAssembler](features/agent-memory.md#contextassembler) for the API).
`tests/benchmarks/test_confidence_gate_refusal.py` measures how well the gate
identifies genuinely-unanswerable questions on the same category-5 slice
audited above.

**CAVEAT — this is a seeded simulation, not an organic measurement.** Read the
caveat before the number:

- As established in the cat-5 audit directly above, **only 2 of the 446
  category-5 items are genuinely unanswerable**. Refusal precision =
  TP/(TP+FP) therefore has **at most 2 true positives** — a single false
  positive swings the reported precision by tens of points. This is not a
  statistically meaningful sample size.
- `ConfidenceField` gating is **cold-start-degenerate** on LoCoMo: the harness
  performs single-shot retrieval with no correction loop, so every candidate's
  confidence sits at the same `initial_confidence` with no observation history
  to diverge it. Left unseeded, `gate_score` would be constant across all 446
  items and the gate would refuse either everything or nothing. To make the
  gate exercisable at all, the benchmark manually seeds a deterministic,
  content-hash-derived confidence spread (roughly `[0.05, 0.95]`) onto every
  ingested record before querying, simulating "a realistic post-interaction
  confidence spread" — this is a **simulation of a spread**, not a measurement
  of one.

**Result (lexical retrieval, `EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD = 0.5`,
`confidence_gate_mode="refuse"`, full 446-item cat-5 slice):**

| Items refused | True positives (TP) | False positives (FP) | Refusal precision TP/(TP+FP) |
|---:|---:|---:|---:|
| 221 | 2 | 219 | 0.009 (≈0.9%) |

!!! warning "Not leaderboard-comparable, not a real-world refusal-accuracy claim"
    This number demonstrates that the gate mechanism works end-to-end — it
    faithfully refuses when the (seeded) rank-0 confidence is low — not that
    Popoto's confidence gate achieves ~1% precision in production. With only 2
    true positives available on this dataset, and a confidence spread that was
    manually seeded rather than organically observed, the figure carries no
    statistical weight and must **never** be cross-compared against any
    recall or judged-accuracy number from the sections above (metric-family
    doctrine) or against another system's refusal-capability claim. See the
    [category 5 audit](#category-5-adversarial-evidence-audit-and-leaderboard-parity-slice)
    above for why LoCoMo cat-5 is a poor substrate for a refusal metric in the
    first place — the same limitation applies here.

Reproduce: `pytest tests/benchmarks/test_confidence_gate_refusal.py -v` (the
deterministic `TestRefusalGateMechanics` class runs unconditionally in CI; the
446-item `TestRefusalPrecisionLoCoMoCat5` class is cache-gated on
`~/.cache/popoto_benchmarks/locomo.json` and prints the same caveat inline
with the report).

### Judged-Answer Accuracy (Tier 5)

The metrics above are **retrieval-level** — did the right memory get retrieved.
Every published vendor leaderboard (Hindsight, Mem0, Zep, Memori, Backboard)
instead reports **end-to-end judged accuracy**: retrieve → generate an answer →
have an LLM grade that answer against the gold answer. The `--judged` flag adds
that stage so Popoto is comparable to those boards.

> **Two different metric families.** Retrieval recall and judged accuracy are
> reported side by side but **must never be cross-compared or combined into a
> single ranking** (per the #453 framing requirements). A judged run's artifact
> keeps them in separate blocks (`summary` vs `judged`).

**Pinned judge (reproducibility).** The judge is the **Mem0 / GAM evaluation
protocol** (`ACCURACY_PROMPT`, arXiv:2504.19413) reproduced verbatim, with
`gpt-4o-mini` as both judge and answer-generator, at `temperature=0`. Judged
accuracy drifts several points across judge models, so the judge identity —
model id, prompt SHA-256, protocol reference, temperature — is recorded in the
`judge` block of every judged artifact. Both the model and the prompt are pinned
in `tests/benchmarks/judge.py` and guarded by tests, so a silent swap is a test
failure.

**API key + cost.** Generation and judging call the OpenAI API, so `--judged`
needs `OPENAI_API_KEY` and the `[openai]` extra (`pip install -e ".[openai]"`).
Without them the stage **skips gracefully** (prints a cost estimate and exits 0,
the same posture as hybrid's model download). Each item costs ~2 `gpt-4o-mini`
calls (~$0.0004/item under the harness's documented token assumptions); a full
LoCoMo judged run (1986 QA) is ~$0.8, a `--limit 200` slice ~$0.08. The harness
prints this estimate (sized by `--limit`, or the dataset's full size when
unlimited) before running.

```bash
# Judged run over a representative 200-question LoCoMo slice (lexical retrieval):
export OPENAI_API_KEY=sk-...
python -m tests.benchmarks.run_external --dataset locomo --judged --limit 200

# Hybrid retrieval + judged accuracy (writes locomo_<date>_hybrid_judged.*):
python -m tests.benchmarks.run_external --dataset locomo --judged \
    --retrieval-mode hybrid --limit 200
```

The `judged` block reports `judged_accuracy` (fraction CORRECT over scored
items), a per-`question_type` breakdown, and separate counts for judge errors
and skipped items. **LoCoMo adversarial (category-5) items are excluded from the
headline `judged_accuracy`** and reported separately under `adversarial`: the
Mem0/GAM prompt is a factual-match judge, not a refusal judge, so it cannot score
refusal answers meaningfully (a dedicated refusal metric is tracked in #463; the
cat-5 scoring audit that recommended it was #454).
Per-item fault isolation means a transient API error records a `judge_error`
status and the run continues rather than aborting a paid run.

Vector mode (`--retrieval-mode vector`) is **not** supported with `--judged` (the
diagnostic vector path returns no memory text to answer from) and is rejected up
front. No self-benchmarked judged numbers are committed — the harness ships the
capability; live runs are operator-run with a key.

### Baseline Numbers (v1.6.3)

**LongMemEval-S (fixture sample, 3 questions):**

| Metric | Score |
|--------|-------|
| Recall@1 | 0.0000 |
| Recall@5 | 0.0000 |
| Recall@10 | 0.0000 |
| MRR | 0.0000 |

**LoCoMo, full dataset (1986 QA pairs, lexical):**

| Metric | Score |
|--------|-------|
| Recall@1 | 0.2986 |
| Recall@5 | 0.5534 |
| Recall@10 | 0.6400 |
| MRR | 0.4124 |

See `tests/benchmarks/results/external/locomo_latest.json` for the full
per-question and per-category report.

**Note:** The v1.6.3 baseline (LongMemEval-S row above) uses score-only
retrieval (DecayingSortedField). `ContextAssembler.assemble()` ranks
candidates by composite score, not by query-text similarity. Scores for
freshly-ingested items with equal importance are indistinguishable — that
baseline is intentionally a floor. The LoCoMo numbers above, by contrast, are
a real BM25-lexical run over the full dataset (see
[Retrieval Modes](#retrieval-modes)), not a score-only floor measurement.

**Reference:** agentmemory BM25+Vector (all-MiniLM-L6-v2) achieves
Recall@5 = 95.2%, Recall@10 = 98.6%, MRR = 88.2% on LongMemEval-S.
Popoto's hybrid mode (`--retrieval-mode hybrid`, see
[Retrieval Modes](#retrieval-modes)) exceeds all three reference numbers
(0.986 / 0.992 / 0.932); the lexical BM25 baseline alone already matches
the reference Recall@5 (0.952).

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

---

## Deterministic CSR Harness

### Overview

The CSR (Constraint Satisfaction Rate) harness (issue #418) is the standing,
deterministic regression gate the external harness cannot be: the same corpus
and query produce a **bit-identical score every run** — no LLM judge, no
embedding model, no Redis-module command, identical on Redis and Valkey. It
adapts CogBench's *scoring methodology* (not its task suite): each test case
is `(planted corpus, standard query, adversarial query, assertions)`, scored
by deterministic assertions over the ranked list that
`ContextAssembler.assemble()` returns.

It exists to catch #409-class regressions — "retrieval is query-independent;
gibberish and a real query return the bit-identical result set" — which
shipped to users and was caught only by a hand-rolled adversarial audit.

### Metrics and What They Mean

| Metric | Meaning |
|--------|---------|
| **CSR** (per case) | Passed assertions / total assertions (fractional) |
| **RSR (standard)** | Mean case CSR for the standard queries |
| **RSR (adversarial)** | Mean case CSR for the authored adversarial paraphrases |
| **Adversarial Gap** | `RSR(standard) − RSR(adversarial)` |

Two signals, with **opposite** signatures:

- A **large Adversarial Gap** on the lexical path means **keyword
  dependence** — retrieval only works when the query shares surface tokens
  with the stored memory. Expected of pure BM25 and flagged in the report
  (`ADVERSARIAL_GAP_ALERT`), it is **not** the #409 signature.
- **Query-blindness** (the #409 signature) is the opposite: a query-blind
  path produces the **identical ranked list** for the standard and
  adversarial query (Gap = 0 exactly) *and* a **low standard CSR** (the
  ranking ignores the query, so it cannot satisfy query-relevance
  assertions). The detector is `rankings_identical AND csr_std <
  QUERY_BLIND_CSR_ALERT`, never the size of the gap.

The seed suite carries a case pair proving the detector discriminates:
`query_blind_409` (lexical — different rankings, high standard CSR) and
`composite_control_409` (query-blind by construction — identical rankings,
low standard CSR, flag fires).

### Why Lexical-Only

Retrieval runs through `BM25Field` (pure Lua over core Redis commands) so
`retrieval_mode="auto"` resolves to `"lexical"` — genuinely query-sensitive
*and* deterministic. Hybrid/embedding CSR is out of scope by design: float
embeddings and model versioning break "same input → identical score". The
adversarial queries are **authored fixture data** (committed paraphrases, no
runtime rewrite step, no synonym RNG), so the adversarial input is
byte-identical every run.

### Running It

```bash
# CI gate (pytest, DB-15 isolation, gates on determinism + suite health +
# the discriminative check — never on RSR/Gap magnitudes):
pytest tests/benchmarks/test_csr.py -q

# Manual run — writes tests/benchmarks/results/csr/csr_{date}.{json,md}
# and csr_latest.{json,md}:
python -m tests.benchmarks.csr.run_csr

# Summary only, no report written:
python -m tests.benchmarks.csr.run_csr --dry-run
```

The JSON report records `executed_path` per run (a zero-BM25-hit query
silently falls back to the query-blind composite path inside the assembler;
the harness pre-flights every query so a fallback can never masquerade as a
lexical number) and `rankings_identical` per case.

To add a test case, see "Adding a CsrTestCase" in `tests/benchmarks/README.md`.

## Subconscious Injection Quality (SIQ) Harness

### Overview

SIQ (issue #459) is Popoto's flagship **native** benchmark — it measures the
one thing composite (query-blind) retrieval does that no public benchmark
scores: *without an explicit query cueing it, did the right memory get injected
into context at the right turn?* Every other harness on this page (the external
LongMemEval-S/LoCoMo runs, the Tier-5 judged-answer harness, even CSR) is
**query-driven** — an explicit question or `query_cues` dict is supplied and the
harness scores whether the right evidence came back. SIQ is the complement.

Each case is a multi-turn agent **trace**: a later turn needs a memory that was
established several turns earlier, but the turn's own message never lexically
restates it (coreference, implication, or need-to-know). This is exactly the
regime where query-blind importance/decay ranking should beat query-driven
retrieval — and where a pure retriever scores ~0 by construction. The traces are
**committed deterministic fixtures** (CSR discipline: no runtime generation RNG,
no wall-clock in ranking, bit-identical scores every run), and retrieval runs
through `ContextAssembler` in `composite` mode (a plain `importance` SortedField,
no BM25/embedding field on the model, so the pull path is genuinely query-blind).

An authoring **cue-blindness lint** makes the ground truth un-gameable: using the
*real* BM25 tokenizer (`src.popoto.fields._tokenizer.tokenize` — the same
preprocessing BM25 indexes with), `lint_trace` rejects any trace whose
`should_recall` turn shares even one indexed token with the target memory. The
constraint only ever gets *harder* to satisfy, never easier.

### Metrics and What They Mean

| Metric | Meaning |
|--------|---------|
| **Injection Precision @ budget** | Of the memories injected under the `max_items`/`max_tokens` budget at an evaluation turn, the fraction that are ground-truth useful |
| **Injection Recall @ budget** | Of that turn's `should_recall` targets, the fraction actually injected |
| **Anticipation lead time** | Per memory: the number of consecutive turns immediately before it becomes explicitly relevant during which it was already injected (rewards proactive recall; a query retriever scores 0 here by construction) |
| **Budget efficiency** | `useful_tokens / injected_tokens` — quality tied to the token budget the recipe already enforces |

Precision, recall, and budget efficiency are evaluated **only at turns that
carry a `should_recall` annotation** — the moments of defined information need.
Narrative turns contribute nothing to them; proactive injection at those turns
is credited by *anticipation lead time* instead. Edge cases resolve to
`None` (undefined), never a `ZeroDivisionError`: precision is `None` when
nothing was injected, recall is `None` at a non-evaluation turn, and a memory
never injected when it mattered is a **miss** (excluded from the mean, counted
separately) rather than a bogus lead value.

### Why It's Competitor-Fair

`SiqAdapter` (a `Protocol`, mirroring the Tier-5 `JudgeProtocol`) is the
extension point: any memory system replayed turn-by-turn can be scored on
identical footing. Two adapters ship:

- **`NativeAdapter`** — Popoto's query-blind composite mode (the system under
  test); the composite-mode invariant is asserted before scoring.
- **`QueryOnlyStubAdapter`** — a dependency-free, deterministic *query-driven*
  baseline (the Mem0/Zep/Hindsight stand-in). It injects only memories whose
  content lexically overlaps the current message, so by the cue-blindness lint
  it injects **nothing** at the recall turn — scoring ~0 recall and all-miss
  anticipation *by construction*. That near-zero baseline is the harness's own
  validity proof (asserted in `test_siq.py`), not a rigged result.

Real Mem0/Zep/Hindsight adapters (heavyweight optional deps + live API keys) are
a tracked follow-up; no competitor numbers are fabricated here. An optional
LLM-judged "usefulness" cross-check reuses the Tier-5 pinned judge
(`gpt-4o-mini`, temperature 0) and is never in the default/CI path.

Baseline over the four committed fixtures (`coreference_relocation`,
`implication_deadline`, `need_to_know_allergy`, `multi_recall_preferences`):

| adapter | recall @ budget | anticipation misses | mean lead |
|---|---|---|---|
| `native` (query-blind) | **1.000** | 0 / 5 targets | 4.0 turns |
| `query_stub` (query-driven) | 0.000 | 5 / 5 targets | n/a |

Everything is Valkey-safe — core Redis commands only, no modules — and the
whole default suite needs no network, no model download, and no API key.

### DB Hygiene

The CI-facing surface is `tests/benchmarks/test_siq.py`, which runs under the
pytest **db15** isolation plugin like every other `test_*.py` — it plants
per-trace, uniquely-prefixed model classes and tears them down, never touching
db0. The optional `run_siq.py` CLI is a manual (non-pytest) report generator, so
it reuses `run_external`'s bench-DB machinery: a dedicated bench DB (default
**14**, `POPOTO_BENCH_DB` override, **db0 rejected**), pointed at only at
`main()` time. Point the CLI away from any in-flight external run (which also
uses db14), e.g. `POPOTO_BENCH_DB=13`.

### Running It

```bash
# CI gate (pytest, DB-15 isolation, deterministic — no network, no API key):
pytest tests/benchmarks/test_siq.py -q

# Manual run — writes tests/benchmarks/results/siq/siq_{date}_{adapter}.{json,md}
# and siq_latest_{adapter}.{json,md}. Use a free bench DB (db0 rejected):
POPOTO_BENCH_DB=13 python -m tests.benchmarks.siq.run_siq --adapter native
POPOTO_BENCH_DB=13 python -m tests.benchmarks.siq.run_siq --adapter query_stub

# Summary only, no report written:
POPOTO_BENCH_DB=13 python -m tests.benchmarks.siq.run_siq --dry-run
```

To add a trace, drop a committed JSON fixture in `tests/benchmarks/siq/fixtures/`
(schema in `tests/benchmarks/siq/corpus.py`); `lint_trace` enforces the
cue-blindness and anticipation-window authoring rules at load time.

## RLT (Retrieval Latency & Throughput) Harness

### Overview

RLT (issue #460) is the axis no other harness on this page measures: **speed
under load**, not retrieval/injection quality. Popoto's substrate premise is
RAM-speed Redis/Valkey memory with no separate vector-service round-trip — RLT
is the harness that puts a number on that claim, jointly with recall so the
result can't be read as "fast but wrong" or "accurate but slow" in isolation.

Five metrics, one submodule each under `tests/benchmarks/rlt/`:

1. **Latency** — p50/p95/p99 per retrieval + end-to-end assemble latency
   (`latency.py`). `ContextAssembler.assemble()` already *is* the full
   retrieve→rank→inject pipeline, so "per retrieval" and "end-to-end assemble"
   are the same measured call in this harness — both labels refer to timing
   `assemble()` itself; there is no separate lower-level retrieval-only API
   surface to isolate without changing `src/popoto/`.
2. **Throughput** — queries/sec at a fixed corpus size, single-threaded or
   under a bounded `ThreadPoolExecutor` (`throughput.py`).
3. **Scaling curve** — latency vs. corpus size, 10³ → 2×10⁴ (`scaling.py`, the
   maintainer's scale target; larger points are informational-only and outside
   this harness's default/CI path — not over-engineered past 20k).
4. **Live mixed workload** — concurrent turn-ingest writes + assembly reads,
   measuring read-latency degradation under write load and vice versa
   (`mixed_workload.py`). Caveat: a thread-based load generator measures
   client-side thread-scheduling overhead alongside genuine server-side
   latency; a multi-process load generator would be needed to fully isolate
   server-only behavior, which is out of scope for this harness.
5. **Recall-vs-p99 Pareto frontier** — jointly with the retrieval harness:
   "at equal p99, who recalls more; at equal recall, who's faster"
   (`pareto.py`). The recall axis is the mean of `recall_at_k()`
   (`tests/benchmarks/metrics/retrieval.py`) over the full query set per
   config, not a per-query binary value.

The corpus is a synthetic, deterministic, lexical-only (BM25) surface
(`corpus.py`) — a fixed topic vocabulary with authored (not derived) ground
truth per query, following the CSR/SIQ discipline. No EmbeddingField, so no
model download is needed to run the fast unit-test corpora.

### Percentile Convention

`rlt/latency.py`'s `percentile()` deliberately reuses the **same nearest-rank
formula** the external harness's `compute_aggregate()` already uses for its
own p50/p95 (`sorted(values)[int(len(values) * p/100)]`) rather than
introducing a second, numerically different percentile definition into this
document — RLT's and the external harness's latency numbers are directly
comparable, not apples-to-oranges.

### Headline Numbers (native Popoto, Redis)

First real run of the harness, captured on an isolated DB once the machine was
quiet (full artifact: `tests/benchmarks/results/rlt/rlt_latest_redis.json`).
Backend **redis 8.x**, Python 3.12, Apple-silicon (10 cores). These are native
Popoto (`ContextAssembler`) numbers only — competitor comparators are still to
come (see Follow-Up). Absolute latency is machine-dependent; read the *shape*
(sub-6-ms p50 retrieval that grows gently with corpus size, and the
read-degradation-under-write-load ratio), not the exact millisecond.

All figures below are the exact values from the committed artifact
(`rlt_20260721_redis.json`); they vary run-to-run (warmup, OS scheduling), so
treat them as one representative snapshot, not a pinned regression target.

**Scaling curve** — latency vs. corpus size (200 samples/point):

| corpus size | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|
| 1,000 | 3.09 | 3.89 | 5.63 |
| 5,000 | 4.91 | 9.87 | 12.16 |
| 10,000 | 8.13 | 10.72 | 12.16 |
| 20,000 | 8.69 | 10.39 | 10.81 |

**Throughput** (corpus 20,000): 115.5 queries/sec single-threaded, 288.1
queries/sec at concurrency 4.

**Live mixed workload** (corpus 5,000, 4 threads): read p99 goes 10.79 → 27.30 ms
(2.53× degradation) under concurrent write load; write p99 goes 0.92 → 9.41 ms
(10.19× degradation) under concurrent read load. The *degradation ratio* (not the
absolute latency) is the live-agent-relevant number, and note the caveat above
about thread-scheduling overhead being included — in this run writes (a light
baseline) were hit harder in relative terms than reads.

> **Reproduce:** `python -m tests.benchmarks.rlt.run_rlt --db <isolated> --backend redis --mixed-workload`.

### Still Deferred

- **Valkey run.** The issue calls for results on **both** Redis and Valkey (the
  first benchmark where the two could measurably differ). No Valkey server was
  available in the session that produced the Redis numbers above, so the Valkey
  run is deferred — the harness is backend-agnostic (`--backend valkey` against
  a Valkey-pointed `REDIS_URL`), so it is purely an ops step, not a code change.
- **Real Mem0/Zep/vector-DB comparators.** The `RltAdapter` Protocol
  (`comparators.py`) ships with a `NativeAdapter` (Popoto) and a dependency-free
  `NullAdapter` proving the contract. **No real Mem0/Zep/vector-DB numbers are
  fabricated here** — real adapters (heavyweight optional deps + live services)
  and the recall-vs-p99 competitor Pareto are a tracked follow-up.

### Comparator Adapters

`RltAdapter` (a `Protocol`, mirroring the Tier-5 `JudgeProtocol` and SIQ's
`SiqAdapter`) is the extension point: `ingest(content) -> None` and
`query(text) -> (result_ids, latency_ms)`. Two adapters ship:

- **`NativeAdapter`** — wraps `ContextAssembler` (the system under test).
- **`NullAdapter`** — a dependency-free stub returning empty results at a
  fixed synthetic latency, proving the Protocol contract end to end without
  any real competitor.

Real Mem0/Zep/vector-DB (pgvector/Qdrant) adapters are heavyweight optional
dependencies plus live external services — implementing and running them is
explicitly a follow-up, not part of this PR.

### DB Hygiene

The CI-facing surface is `tests/benchmarks/test_rlt.py`, which runs under the
pytest **db15** isolation plugin like every other `test_*.py` in this suite —
tiny synthetic corpora (tens of records), never the real 10³–2×10⁴ scaling
range, never db0 or db14.

The manual `run_rlt.py` CLI (for the real, deferred measurement runs) requires
`--db` **explicitly** — unlike `run_external.py`'s `POPOTO_BENCH_DB` default-14
pattern, this harness has no default. `--db 0`, `--db 14`, and `--db 15` are
all rejected: 0 is production-shaped, 14 is reserved for the concurrently
running external-benchmark chain, and 15 is the project's pytest-isolation DB
(flushed by every test session — a manual run there would race with or
pollute tests).

### Running It

```bash
# CI gate (pytest, DB-15 isolation, tiny synthetic corpora):
pytest tests/benchmarks/test_rlt.py -q

# Manual real-corpus run (deferred — do NOT run against db0, db14, or db15;
# do NOT run concurrently with any other heavy benchmark chain sharing this
# machine, since RLT measures wall-clock latency):
python -m tests.benchmarks.rlt.run_rlt --db 13 --backend redis
python -m tests.benchmarks.rlt.run_rlt --db 13 --backend redis --dry-run
```

### Follow-Up

Tracked in [issue #487](https://github.com/tomcounsell/popoto/issues/487):
(1) run the real headline latency/throughput/scaling/mixed-workload
measurements on both Redis and Valkey once the machine is confirmed quiet,
against an explicitly isolated DB; (2) implement real Mem0/Zep/vector-DB
`RltAdapter`s and run a real competitor comparison producing the
recall-vs-p99 Pareto frontier this harness computes the machinery for.
