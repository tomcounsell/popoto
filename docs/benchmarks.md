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
