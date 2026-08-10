# Benchmarking

Popoto ships with three benchmark harnesses:

1. **Internal parametric sweep** (`tests/benchmarks/run_sweeps.py`) — tunes
   behavioral constants against synthetic scenarios. Covered in `tests/benchmarks/README.md`.

2. **External benchmark harness** (`tests/benchmarks/run_external.py`) — evaluates
   memory retrieval quality against published, named datasets. Covered on this page.

3. **Deterministic CSR harness** (`tests/benchmarks/csr/`) — a per-PR CI gate
   asserting named retrieval properties with deterministic scoring. Covered on
   this page (see [Deterministic CSR Harness](#deterministic-csr-harness)).

## How to check any number on this page

Every figure published here comes from a JSON artifact committed in this
repository, and the practices below exist so you can audit one rather than
trust it.

- **The artifact is the source.** Result pages under
  [Benchmarks results](benchmarks/results/index.md) are generated at build time
  from `tests/benchmarks/results/`. There are no hand-typed result tables to
  drift from the run that produced them.
- **The judge is pinned and hashed.** Judged runs record the judge model, the
  generator model, the temperature, and the SHA-256 of both the judge and
  generation prompts. A silent prompt or model swap is a test failure, not a
  quiet accuracy shift.
- **The environment is captured.** Every report carries Python version, OS,
  CPU count, dataset variant, sample mode, seed, and limit, so a report is
  reproducible from itself.
- **Negative results are published.** The measurements that went against the
  design ship on this page beside the ones that went for it: LLM extraction
  losing to raw ingestion, graph traversal buying one question at Recall@10 for
  4.2× the latency once the scoring defect was corrected, and that scoring
  defect itself, which inflated every LoCoMo number until it was found and
  fixed.
- **Metric families stay apart.** Retrieval recall and judged answer accuracy
  are never tabulated together or combined into a ranking.

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
| `hybrid` | `BM25Field` + `EmbeddingField` | BM25 + vector via **weighted, query-adaptive RRF (k=60)** | one-time ~90 MB `all-MiniLM-L6-v2` download; slower (CPU embedding) |
| `vector` | `EmbeddingField` only | pure cosine (no BM25, no RRF) | same ~90 MB `all-MiniLM-L6-v2` download as hybrid; slower (CPU embedding) |

Hybrid is **Valkey-safe**: vector similarity is computed in-process with numpy
cosine over `EmbeddingField` `.npy` files — no RediSearch, no vector-search modules,
no `FT.*` / `BF.*` commands. The vector signal uses the local
[`SentenceTransformersProvider`](fields.md#embeddingfield) (no API key).

!!! info "Hybrid fusion is weighted and query-adaptive"
    Unweighted RRF (k=60) gave the dense (vector) arm equal say with BM25 on
    every query. That helped on paraphrastic recall (LongMemEval-S) but hurt
    on coreference-heavy, multi-session dialogue (LoCoMo), where the dense arm
    — whose standalone recall there is near-zero (vector-only Recall@1 ~0.05)
    — still cast full rank-votes and displaced correct BM25 hits, so hybrid
    measurably **underperformed** `lexical` on LoCoMo (Recall@1 0.167 vs
    0.299; see the pre-fix numbers below). `ContextAssembler`'s hybrid pull
    path now classifies each query by shape and picks a weight regime
    (`_fusion_weights()` in `recipes/context_assembler.py`) before calling
    `Query.fuse(weights=...)`:

    - **Name/date/token-specific and not first-person** (LoCoMo's shape —
      third-person, name-anchored questions like "When did Caroline …";
      99.6% of LoCoMo carries a proper-noun name, 0% is first-person) →
      `FUSION_REGIME_KEYWORD_LEAN`, which sets the **vector weight to 0**.
      With the dense arm contributing zero to every document's RRF score, the
      fused ranking converges to the lexical (BM25 + graph) result, so hybrid
      cannot fall below lexical on this query shape.
    - **First-person / paraphrastic** (LongMemEval-S's shape — self-recall
      like "What degree did I …"; 95.8% first-person) →
      `FUSION_REGIME_NEUTRAL`, which is plain **unweighted RRF** — the exact
      blend that produced the LongMemEval-S hybrid win, preserved verbatim.

    `Query.fuse()` / `QueryBuilder.fuse()` gained an additive
    `weights: dict[str, float] | None` kwarg; `weights=None` (the default for
    any caller outside `ContextAssembler`) is byte-for-byte identical to the
    original unweighted RRF. The weights are experimental in-code tuning
    constants (not user config), fully in-process and Valkey-safe. Post-fix
    confirmation numbers are in the tables below: full-500 on LongMemEval-S,
    and a labelled 250-question stratified sample on LoCoMo, where a full hybrid
    pass costs ~5.2 hours of CPU embedding.

`vector` is a **harness-local diagnostic**: it bypasses
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

**LongMemEval-S, the full 500 questions (any-hit Recall), no sampling:**

| Mode | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|------|--:|---------:|---------:|----------:|----:|
| `lexical` (BM25) | 500 | 0.856 | 0.952 | 0.978 | 0.899 |
| `hybrid` (BM25+vector, weighted RRF) | 500 | **0.892** | **0.986** | **0.992** | **0.931** |
| agentmemory reference (BM25+vector) | 500 | — | 0.952 | 0.986 | 0.882 |

Hybrid outperforms the lexical baseline on every metric and exceeds the
agentmemory reference at Recall@5 (0.986 vs 0.952), Recall@10 (0.992 vs 0.986),
and MRR (0.931 vs 0.882). The vector signal helps most where keyword overlap is
weakest: `single-session-preference` Recall@1 rises from 0.40 (lexical) to 0.70,
and every question category reaches Recall@5 ≥ 0.967. Full per-category detail
is in the committed artifact
(`tests/benchmarks/results/external/longmemeval_s_latest_hybrid.json` —
500/500 questions, zero errors, every item resolved to the true hybrid path).

!!! info "Re-confirmed at full scale on 2026-08-07 after the #457 fusion change"
    The previously published hybrid row (0.894 / 0.986 / 0.992 / 0.932,
    `longmemeval_s_20260703_hybrid.json`) predated the
    [#457](https://github.com/tomcounsell/popoto/issues/457) weighted fusion
    change, and the only post-#457 evidence was a 100-question sample. The full
    500 were re-run under the current fusion
    ([#530](https://github.com/tomcounsell/popoto/issues/530)): **Recall@1 moves
    0.894 → 0.892 (one question of 500), Recall@5 and Recall@10 are unchanged,
    MRR moves 0.9317 → 0.9307.** First-person queries route to the neutral
    (unweighted-RRF) regime, so the weighted path is expected to be a near
    no-op here, and it is. Latency rose (p50 41.5 → 57.0 ms) because this run
    shared a machine with a concurrent benchmark; read the recall, not the
    milliseconds, from this particular run.

!!! info "Read the retrieval granularity before comparing these to anything"
    Popoto indexes **one record per conversation turn**, and a retrieved turn
    counts as a hit for its parent session. So these are session-level recall
    figures produced by turn-level ranking: 20 retrieved turns can cover far
    fewer than 20 distinct sessions, which makes the top-K window effectively
    wider in session terms than a system that ranks whole sessions directly.
    The agentmemory reference row is the closest available like-for-like
    comparison (same dataset, same any-hit recall metric family), and it still
    differs on this axis. Systems that rank whole sessions are answering a
    differently shaped question; the numbers are not interchangeable.

    The same source reports MemPalace at **Recall@5 96.6%** on LongMemEval-S,
    which is the number to beat at that rank, not the agentmemory row.

!!! info "Scoring correction — LoCoMo numbers were re-measured on 2026-08-07"
    Every LoCoMo retrieval number published before 2026-08-07 was produced by a
    harness that consulted the answer key when collapsing retrieved turns to
    result IDs ([#514](https://github.com/tomcounsell/popoto/issues/514)). Gold
    turns emitted their unique `turn_id` and held their own rank slot while
    non-gold turns collapsed into one shared `session_id` slot, so 20 retrieved
    turns became **13.2 rank slots on average** and gold was systematically
    lifted. Scoring now ranks one unit per dataset for every record alike —
    turn IDs for LoCoMo, session IDs for LongMemEval-S — decided from dataset
    metadata before retrieval runs.

    **LongMemEval-S is unaffected:** its ground truth is session IDs, which the
    old rule emitted on both branches, so its published numbers stand. A full
    500-question lexical re-run under the corrected harness
    (`longmemeval_s_20260807.json`) reproduces Recall@1 0.8560 / Recall@5
    0.9520 / Recall@10 0.9780 / MRR 0.8987 — identical on every question
    individually, with only latency differing.

    The corrected LoCoMo lexical run is `locomo_20260807.json`; the superseded
    run stays committed as `locomo_20260708.json`. As of the
    [#530](https://github.com/tomcounsell/popoto/issues/530) refresh the
    **hybrid**, **graph**, and **judged** arms have also been re-measured under
    gold-blind scoring. Their coverage is not uniform and every table below
    states it: hybrid is a **250-question stratified sample**, graph is the
    **full 282-question multi-hop slice**, judged is **100 questions from a
    2-dialogue subset**, and only lexical is the full 1986.

!!! warning "The corrected hybrid run is a 250-question sample, not a full run"
    A full LoCoMo hybrid pass re-embeds ~1.19M records and measured **~5.2
    hours** on the reference machine (10-core Apple silicon, all-MiniLM-L6-v2 on
    CPU). Rather than leave the superseded full run standing as the published
    hybrid number, #530 re-ran it as a **250-question stratified sample, seed
    0** (every category represented) under gold-blind scoring. That number
    carries sampling error a full run does not. A full-1986 hybrid refresh
    remains outstanding.

#### Coverage of the 2026-08-07 runs

Coverage is deliberately uneven, because a full hybrid pass costs hours of CPU
embedding. Read this table before reading any two rows of this page against each
other.

| Arm | Coverage | Full or sampled |
|---|---|---|
| LongMemEval-S `hybrid` | 500 of 500 questions | **Full** |
| LongMemEval-S `lexical` | 500 of 500 questions | **Full** |
| LoCoMo `lexical` | 1986 of 1986 questions | **Full** |
| LoCoMo `hybrid` | 250 of 1986, stratified, seed 0 | **Sampled** |
| LoCoMo `graph` (cat-1 slice) | 282 of 282 cat-1 questions | **Full slice** |
| LoCoMo `lexical`/`hybrid` (cat-1 slice) | 282 of 282 cat-1 questions | **Full slice** |
| LoCoMo judged | 100 of 304 QA from a 2-dialogue subset | **Sampled** |
| LoCoMo `--extraction` arms | not re-run | **Pre-#514 retrieval block** |

**Environment for every 2026-08-07 run on this page.** Python 3.12.13,
macOS-26.5.2-arm64 (10-core Apple silicon), Redis 8.6.2 on localhost, redis-py
8.1.0, sentence-transformers 5.7.0 with all-MiniLM-L6-v2 on CPU, numpy 2.5.1.
Datasets are the cached HuggingFace releases (`locomo10.json`,
`longmemeval_s_cleaned.json`). Each artifact restates its own Python, platform,
sample mode, seed, and limit, so any single report is reproducible from itself.

**LoCoMo lexical, full 1986 questions (any-hit Recall):**

| Mode | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|------|--:|---------:|---------:|----------:|----:|
| `lexical` (BM25), corrected scoring | 1986 | 0.2981 | 0.5302 | 0.6017 | 0.4005 |
| `lexical` (BM25), pre-#514 scoring (superseded) | 1986 | 0.2986 | 0.5534 | 0.6400 | 0.4124 |

**LoCoMo hybrid vs lexical, corrected scoring, on the identical 250-question
stratified sample (seed 0). SAMPLE, not a full run:**

| Mode | n | Recall@1 | Recall@5 | Recall@10 | MRR | p50 (ms) |
|------|--:|---------:|---------:|----------:|----:|---------:|
| `lexical` (BM25) | 250 | 0.3400 | 0.5120 | 0.5840 | 0.4178 | 6.92 |
| `hybrid` (weighted RRF) | 250 | 0.3400 | 0.5120 | 0.5880 | 0.4172 | 61.78 |

The hybrid row is the committed
`tests/benchmarks/results/external/locomo_latest_hybrid.json` (250/250
questions, zero errors). The lexical row is the same 250 item IDs re-aggregated
out of the committed full-1986 `locomo_latest.json`, so the two rows are exactly
the same questions scored exactly the same way: a like-for-like pair with no
sampling difference between them.

The two are indistinguishable except for one question at rank 10 and a rounding
difference in MRR. The query-shape discriminator routes LoCoMo's
name/date-anchored queries to the keyword-lean regime (vector weight 0), so the
fused ranking converges on the lexical ranking; with the dense arm zeroed, the
fused order *is* the lexical order, and the near-exact match is the expected
signature of that mechanism rather than a coincidence.

That resolves the earlier finding that hybrid **underperformed** lexical on
LoCoMo. Under unweighted RRF and pre-#514 scoring, hybrid scored Recall@1 0.1667
/ Recall@5 0.4235 / Recall@10 0.5403 / MRR 0.2835 across the full 1986
(`locomo_20260708_hybrid.json`, still committed): a weak vector arm was given
equal say on every query. Two things changed between that artifact and the table
above (gold-blind scoring **and** the [#457](https://github.com/tomcounsell/popoto/issues/457)
weighted/query-adaptive fusion), so the movement cannot be attributed to either
one alone, and the two runs also differ in coverage (1986 vs 250). What the
250-question pair does establish, because both of its rows are the same
questions under the same scoring, is that hybrid no longer costs anything
relative to lexical on this dataset.

The paraphrastic side is preserved: LongMemEval-S `hybrid` is unaffected by the
#514 correction (its ground truth is session IDs) and its full-500 confirmation
is in the table at the top of this section.

#### Category 5 ("adversarial") — evidence audit and leaderboard-parity slice

LoCoMo's category 5 is historically the hardest category industry-wide (the
original paper reports humans ≈89 F1 vs LLMs ≈2 F1), and most public
leaderboards exclude it. Popoto scores it *comparably* to the other categories
(n=446, Recall@1=0.3341, Recall@5=0.5897, Recall@10=0.6502, MRR=0.4453), which
warranted an audit of whether that number means anything.

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
| `lexical`, full 5-category (corrected) | 1986 | 0.2981 | 0.5302 | 0.6017 | 0.4005 |
| `lexical`, 4-category parity (corrected) | 1540 | 0.2877 | 0.5130 | 0.5877 | 0.3875 |
| `hybrid`, full 5-category (corrected, **250-question SAMPLE**) | 250 | 0.3400 | 0.5120 | 0.5880 | 0.4172 |
| `hybrid`, 4-category parity (corrected, **SAMPLE**) | 194 | 0.3041 | 0.4846 | 0.5619 | 0.3836 |
| `lexical`, full 5-category (pre-#514, superseded) | 1986 | 0.2986 | 0.5534 | 0.6400 | 0.4124 |
| `lexical`, 4-category parity (pre-#514, superseded) | 1540 | 0.2883 | 0.5390 | 0.6260 | 0.3991 |
| `hybrid`, full 5-category (pre-#514, superseded) | 1986 | 0.1667 | 0.4235 | 0.5403 | 0.2835 |
| `hybrid`, 4-category parity (pre-#514, superseded) | 1540 | 0.1552 | 0.4065 | 0.5181 | 0.2686 |

Only the `lexical` corrected rows are the exact 1540-question leaderboard
variant. The corrected `hybrid` rows are a 250-question stratified sample, whose
parity slice is 194 questions, not 1540. They are shown here for the
category-5 comparison, **not** as a leaderboard-parity claim.

Category 5 sits near the mean, so excluding it barely moves the numbers —
further evidence it is neither anomalously easy nor a scoring artifact. (The
slice re-aggregates 4-decimal per-category means, so it can differ from a raw
re-aggregation in the 4th place; reproducibility from the committed artifact is
the priority.)

**How this page reports category 5.** It stays in the full 5-category
aggregate, because the evidence audit shows its spans are meaningful retrieval
targets. The 4-category parity slice is published alongside it for leaderboard
comparability, and the caveat above travels with both. A refusal metric is not
applicable to this dataset; refusal capability is measured separately.

#### Confidence-gated retrieval: refusal precision

`ContextAssembler` carries an opt-in confidence gate
(`confidence_gate_threshold` / `confidence_gate_mode`; see
[ContextAssembler](features/context-assembler.md#confidence-gate) for the API).
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
front.

#### Popoto's judged accuracy: 0.36

**LoCoMo, lexical retrieval, judged answer accuracy: 0.3636.** Scored items
n=77 (100 questions sampled, 23 adversarial items excluded because the Mem0/GAM
prompt is a factual-match judge and cannot score a refusal), 28 correct, zero
judge errors. 95% confidence interval **≈ 0.25–0.47**. At n=77 a single flipped
item moves the point estimate by 1.3 points, so read the interval, not the third
decimal. Artifact: `tests/benchmarks/results/external/locomo_latest_judged.json`.

**Scope of the run.** The 100 questions are a stratified sample (seed 0) from a
derived two-dialogue LoCoMo subset (conversations 26 and 30, 788 turns, 304 QA
pairs), chosen so the five extraction arms below could be run against an
identical corpus at a bounded API cost. This is not the full 1986-pair LoCoMo,
and the interval above covers only sampling error within this subset, not
dialogue-selection variance across the other eight conversations.

Retrieval on that same run finds the right evidence far more often than the
generator answers from it. That gap is the honest headline of this number: the
retrieval layer is the part that measures well, and closing the generation gap
is open work rather than a solved problem.

**Chronology.** [Epic #456](https://github.com/tomcounsell/popoto/issues/456)
set this project's benchmark doctrine (native benchmarks, retrieval parity,
never cross-compare recall with judged accuracy) **before** this judged number
existed. The doctrine was not written to accommodate the result.

**Scoring provenance: re-run, and the prediction held.** The
[#514](https://github.com/tomcounsell/popoto/issues/514) gold-blind scoring
correction changed how retrieved turns collapse to *result IDs for recall
scoring*. The judged stage consumes retrieved memory **text** in rank order, so
judged accuracy should have been untouched by that defect. The whole judged run
was repeated on 2026-08-07 under gold-blind scoring
([#530](https://github.com/tomcounsell/popoto/issues/530)) against the identical
corpus, sample, judge, and generator, and it lands on **exactly 0.3636 again:
the same 28 correct out of the same 77 scored, zero judge errors.** The
co-reported retrieval block did move, as expected: Recall@10 0.5900 → 0.5600 and
MRR 0.3851 → 0.3784, with Recall@1 (0.2900) and Recall@5 (0.4700) unchanged.
The refreshed artifact is `locomo_20260807_judged.json`; the superseded one
stays committed as `locomo_20260806_judged.json`.

The five extraction-arm artifacts below (`locomo_latest_ext-*_judged.json`)
were **not** re-run and still carry pre-correction scoring in their retrieval
blocks. Their judged-accuracy column, which is the finding, is the quantity the
re-run above just demonstrated is invariant to the correction, and every arm ran
on the identical corpus and sample, so the ordering between them is unaffected.

!!! warning "This is not tabulated against vendor leaderboard accuracies"
    Public LoCoMo judged-accuracy claims are not comparable to this number, and
    the reasons are specific rather than defensive:

    - **The judge model is usually unnamed.** Judged accuracy drifts several
      points across judge models. Popoto pins `gpt-4o-mini`, temperature 0, and
      records the prompt SHA-256 in every artifact.
    - **Judge prompts vary in generosity** by roughly ten points on the same
      answers. Popoto reproduces the published Mem0/GAM `ACCURACY_PROMPT`
      verbatim.
    - **N is usually unstated**, so nobody can size the interval.
    - **The adversarial category is usually excluded silently.** Popoto excludes
      it too, and says so, with the count.
    - **At least one widely-republished figure does not reproduce.** Mem0's
      66.88% carries an open non-reproduction issue reporting roughly 0.20 from
      the official script, and several other published figures trace back to one
      citation chain rather than independent runs.

    A generator-model difference is **not** among the reasons. Mem0's paper used
    `gpt-4o-mini` as generator, the same model this harness uses, so that
    particular objection would be wrong.

#### Extraction, measured

`--extraction` runs the same judged harness with a fact-extraction step in
front of ingestion instead of storing turns as they arrive. Every extraction arm
lost to raw ingestion on the same 77 scored items:

| Ingestion path | Judged accuracy | Correct / 77 | Turns dropped | Records per turn |
|---|---:|---:|---:|---:|
| Raw turn ingestion (default) | **0.3636** | 28 | 0% | 1.00 |
| Heuristic sentence splitter | 0.2078 | 16 | 0.3% | 3.00 |
| `--extraction claude`, Sonnet | 0.1948 | 15 | 27.3% | 2.32 |
| `--extraction claude`, Opus | 0.1429 | 11 | 36.9% | 1.71 |
| `--extraction claude`, Haiku | 0.0519 | 4 | 63.4% | 0.98 |

Two distinct mechanisms, both measured rather than guessed. Across the three
Claude arms, accuracy falls monotonically with the **turn drop rate**: the
prompt's instruction to skip filler discards the turns holding the ground-truth
evidence, so the answer is gone before retrieval runs. The heuristic arm drops
almost nothing yet still loses 16 points, by the opposite failure: it shatters
each turn into roughly three sentence fragments, and a fragment retrieved
without its surrounding turn is not enough for the generator to answer from.

Rewriting a turn before storing it costs more than it saves on this dataset, in
both directions. Extraction therefore ships as a documented opt-in that stays
off by default. Artifacts: `locomo_latest_ext-*_judged.json`. Full write-up:
[LLM Memory Extraction](features/llm-memory-extraction.md).

Every arm ran on the identical corpus, sample, judge, and generator, so the
ordering between them is the finding and it does not depend on the scoring
correction described above.

### Graph Traversal

`graph_traversal_relationship_fields` extends the co-occurrence graph arm to
walk named `Relationship` edges 1–2 hops. Two measurements exist, and they say
different things.

**Capability, on authored fixtures** (`graph_eval_484/association_recall_*.json`,
100 trials each): the graph arm retrieves a target reachable only through a
`Relationship` edge at 1 hop and at 2 hops, where lexical retrieval and the
co-occurrence arm alone retrieve it in zero trials. Mean target rank 2.0 at one
hop, 3.0 at two. This measures that the traversal works, on a corpus authored
so that nothing else could work. It is a mechanism proof, not a quality score.

**Cost, on real data** (`graph_eval_484/locomo_latest*.json`, the **full**
282-question multi-hop LoCoMo slice, category 1, not sampled), re-measured
2026-08-07 under gold-blind scoring:

| Mode | n | Recall@1 | Recall@5 | Recall@10 | MRR | p50 (ms) |
|------|--:|---------:|---------:|----------:|----:|---------:|
| `lexical` | 282 | 0.1312 | 0.2979 | 0.4220 | 0.2177 | 8.62 |
| `hybrid` | 282 | 0.1312 | 0.2979 | 0.4220 | 0.2182 | 60.55 |
| graph traversal | 282 | 0.0745 | 0.2766 | 0.4326 | 0.1747 | 36.02 |

All three rows are the full 282-question category-1 slice, not a sample. The
`hybrid` row lands on the lexical row to four decimals on every recall metric,
which is the same query-shape convergence described under
[Retrieval modes](#retrieval-modes) showing up again on a different slice.

**The correction changed this conclusion.** Under the pre-#514 scoring these
same two arms read `lexical` 0.1312 / 0.3440 / 0.4965 / 0.2286 and graph 0.0816
/ 0.4858 / 0.5957 / 0.2236, which looked like a clear coverage win for graph at
ranks 5 and 10. Gold-blind scoring removes that. Graph now costs Recall@1
(0.1312 → 0.0745), costs Recall@5 (0.2979 → 0.2766), costs MRR (0.2177 →
0.1747), and buys **one extra question at Recall@10** (0.4220 → 0.4326, a
0.0106 difference, 3 of 282 items) for **4.2× the latency**. On this slice the
trade is not worth taking, and the earlier "better coverage at rank 5–10"
framing was an artifact of the defective scoring rather than a measured
property of the traversal.

The mechanism proof on authored fixtures above still stands: traversal reaches
targets nothing else reaches. What the real-data slice says is that
conversational-adjacency edges are not the graph that pays for it. Leave the
arm off unless you have real association edges, not adjacency ones.

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

The CSR (Constraint Satisfaction Rate) harness is the standing,
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

SIQ is Popoto's **native** benchmark. It measures the
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

!!! note "No Popoto SIQ score is published"
    The harness runs against four committed fixtures and both adapters, and the
    scores are asserted in `test_siq.py` as a validity check on the harness
    itself. They are not published as a result, because the only comparator
    that currently exists is `QueryOnlyStubAdapter`, a stand-in that scores
    near zero *by construction*, since the cue-blindness lint guarantees the
    recall turn shares no indexed token with the target. Reporting a Popoto
    number beside it would be reporting the lint, not a capability. A published
    SIQ number waits on a real competitor adapter
    ([#486](https://github.com/tomcounsell/popoto/issues/486),
    [#487](https://github.com/tomcounsell/popoto/issues/487)). Run it yourself
    with the commands below if you want the current values.

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

RLT is the axis no other harness on this page measures: **speed
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
(a p50 retrieval curve that grows gently with corpus size, 3.0 ms at 1,000
records to 6.0 ms at 20,000, and the read-degradation-under-write-load ratio),
not the exact millisecond.

The comparable published anchor is MEMTIER (arXiv:2605.03675), which reports
hybrid-RRF retrieval at **96.7 ms/query** on comparable hardware. Scope the
Popoto figures before reading them against it: the curve above is the
**in-process lexical** path. Adding arms costs time. LongMemEval-S hybrid
(BM25 + CPU embedding + RRF) runs p50 41.5 ms over 500 questions on an
otherwise idle machine (57.0 ms on the 2026-08-07 re-run, which shared the
machine with a concurrent benchmark), and graph traversal runs p50 36.0 ms on
its 282-question LoCoMo slice. None of these are
compared against hosted-service latencies, which bundle a network round trip
and a different substrate.

All figures below are the exact values from the committed artifact
(`rlt_20260721_redis.json`); they vary run-to-run (warmup, OS scheduling), so
treat them as one representative snapshot, not a pinned regression target.

**Scaling curve** — latency vs. corpus size (200 samples/point):

| corpus size | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|
| 1,000 | 3.02 | 4.01 | 5.79 |
| 5,000 | 3.30 | 7.05 | 9.58 |
| 10,000 | 5.90 | 7.94 | 9.56 |
| 20,000 | 6.02 | 9.27 | 15.26 |

**Throughput** (corpus 20,000): 149.3 queries/sec single-threaded, 293.9
queries/sec at concurrency 4.

**Live mixed workload** (corpus 5,000, 4 threads): read p99 goes 6.55 → 21.98 ms
(3.35× degradation) under concurrent write load; write p99 goes 0.78 → 11.72 ms
(14.93× degradation) under concurrent read load. The *degradation ratio* (not the
absolute latency) is the live-agent-relevant number, and note the caveat above
about thread-scheduling overhead being included — in this run writes (a very
light baseline) were hit far harder in relative terms than reads.

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
