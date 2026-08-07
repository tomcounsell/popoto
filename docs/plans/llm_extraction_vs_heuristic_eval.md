# LLM extraction vs heuristic — measured (#489)

Follow-up to #461 / PR #481, which shipped the `popoto.extraction` path
(`ClaudeExtractionProvider`, pinned `claude-opus-4-8`, opt-in) but deferred
the "evaluated, not vibes" measurement. This is that measurement.

**Bottom line: write-time extraction as currently shipped makes retrieval and
answer quality worse, not better, on LoCoMo. Judged accuracy drops 46-86%
relative to raw-turn ingestion at every tier and every price point.** The
parked "which model tier?" question is not the binding question — the
extraction *strategy* is. Recommendation in [Recommendation](#recommendation).

## What was run

| | |
|---|---|
| Dataset | LoCoMo, 2-dialogue bounded sample (`conv-26`, `conv-30`) — 788 unique turns, 304 QA pairs |
| Items scored | 100, `--sample stratified --seed 0` |
| Retrieval mode | `lexical` (BM25) |
| Judge | `gpt-4o-mini`, pinned Mem0/GAM protocol (#458); 77 scored, 23 adversarial excluded |
| Bench DB | `POPOTO_BENCH_DB=8` |
| Environment | redis-py 7.1.1, anthropic 0.116.0, Python 3.13, macOS |
| API spend | **$8.22** total (788 turns × 3 tiers, one-time cache warm) + ~$0.21 judging |

Extraction is one API call per turn, and LoCoMo's 1986 QA items share only 10
dialogues — every item re-ingests its dialogue's full history. Naively that is
~1.19M calls (~$5.6k at the Opus tier) over ground containing only 5,882
unique turns. Extraction is a pure function of (model, prompt, text), so
`tests/benchmarks/extraction_axis.py` caches on disk keyed by a hash of
exactly those three. The benchmark runs below made **zero** API calls
(40,350 cache hits each) and every tier saw byte-identical inputs.

## Results

Retrieval recall and judged accuracy are **separate metric families** and are
never cross-compared (#453).

| Ingest arm | facts/turn | records | turns→0 facts | R@1 | R@5 | R@10 | MRR | **judged acc** | correct/n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **raw** (baseline) | 1.00 | 40,350 | 0.0% | **0.290** | 0.470 | 0.590 | **0.385** | **0.3636** | 28/77 |
| heuristic | 3.00 | 120,876 | 0.3% | 0.240 | 0.440 | 0.540 | 0.331 | 0.2078 | 16/77 |
| claude-opus-4-8 | 1.71 | 69,163 | 36.9% | 0.260 | 0.460 | 0.560 | 0.350 | 0.1429 | 11/77 |
| claude-sonnet-5 | 2.32 | 93,492 | 27.3% | 0.260 | **0.530** | **0.600** | 0.371 | 0.1948 | 15/77 |
| claude-haiku-4-5 | 0.98 | 39,639 | 63.3% | 0.230 | 0.430 | 0.550 | 0.327 | 0.0519 | 4/77 |

`raw` is the arm every previously committed `results/external/*` artifact was
produced under: one record per turn, content verbatim.

### Retrieval recall barely moves; judged accuracy collapses

Recall@1 moves within a 0.23-0.29 band while judged accuracy falls from 0.364
to as low as 0.052. That divergence is itself the finding, and it is a
measurement artifact worth naming: **recall is scored on turn-id attribution,
so it overstates the extraction arms.** A record derived from the right turn
counts as a hit even when its rewritten text no longer contains the answer.
Judged accuracy has no such escape hatch. Reading recall alone would have made
extraction look roughly neutral.

### Two compounding failure mechanisms

Of the questions the baseline answered correctly but an extraction arm got
wrong, 79-97% produced an explicit "I do not have that information":

| Arm | raw-correct → wrong | "no info" answers | of which recall@10 = 0 |
|---|---:|---:|---:|
| heuristic | 19 | 15 (78.9%) | 7 (36.8%) |
| claude-opus-4-8 | 30 | 28 (93.3%) | 12 (40.0%) |
| claude-haiku-4-5 | 34 | 33 (97.1%) | 12 (35.3%) |

**1. Evidence destruction at write time.** Measured directly against the 97
distinct ground-truth evidence turns the 100 sampled questions depend on —
the share that extract to *zero facts* and are therefore never written at all:

| Arm | evidence turns yielding zero facts |
|---|---|
| heuristic | 0 / 97 (0.0%) |
| claude-sonnet-5 | 19 / 97 (19.6%) |
| claude-opus-4-8 | 25 / 97 (25.8%) |
| claude-haiku-4-5 | **51 / 97 (52.6%)** |

The shipped `EXTRACTION_PROMPT` instructs the model to "skip greetings,
filler, and purely conversational scaffolding". On LoCoMo's short chat turns
(mean 124 characters) that instruction discards a quarter to a half of the
turns the benchmark's own ground truth marks as evidence. The failure is
silent — `extract()` fails open by contract, so a dropped turn is
indistinguishable from a turn with nothing to say.

**2. Content lossiness.** The other ~60% of regressions had recall@10 > 0 —
the evidence *was* retrieved — and the generator still could not answer.
Rewriting a turn into a terse third-person fact ("The user went to the pier
with Melanie") preserves the gist and drops the specifics that graded QA
depends on. The heuristic arm shows the same effect by a different route:
it destroys no evidence (0/97) but fragments each turn into 3.0 sentence
records, so a fixed top-10 budget covers proportionally fewer source turns.

### Tier comparison

| Tier | $/1M in-out | cost, 788 turns | facts/turn | evidence destroyed | judged acc |
|---|---|---:|---:|---:|---:|
| claude-opus-4-8 | $5 / $25 | $4.73 | 1.65 | 25.8% | 0.1429 |
| claude-sonnet-5 | $2 / $10 (intro) | $2.85 | 2.24 | 19.6% | 0.1948 |
| claude-haiku-4-5 | $1 / $5 | $0.64 | 0.94 | 52.6% | 0.0519 |

Cheaper is not uniformly worse — **Sonnet 5 beat Opus 4.8 on every axis while
costing 40% less** (judged 0.195 vs 0.143, R@5 0.530 vs 0.460, 19.6% vs 25.8%
evidence destroyed). The ordering is not a capability ladder; it tracks how
*aggressively* each model applies the prompt's "skip filler" instruction.
Haiku is the most aggressive and is disqualified by a 52.6% evidence-loss
rate, not by fact quality — the facts it does write are fine (see the spot
check in the run log; all three tiers extracted the same three facts from an
identical probe utterance).

Full-corpus extrapolation (5,882 unique LoCoMo turns, cache-deduped):
Opus $35, Sonnet $21, Haiku $5. Without the content-hash cache the same
coverage costs ~$5,573 / ~$2,229 / ~$1,115.

### SIQ (subconscious injection quality) — underpowered, directional only

The committed SIQ corpus is **4 traces with 5 recall targets total**. Every
number below is directional; none should be quoted as a result.

| Arm | precision@budget | recall@budget | budget efficiency | anticipation misses | records written |
|---|---:|---:|---:|---:|---:|
| native (fixture-authored) | 0.667 | 1.00 | 0.706 | 0 | 8 |
| heuristic | 0.250 | 1.00 | 0.321 | 0 | 21 |
| claude-opus-4-8 | 0.363 | 1.00 | 0.404 | 0 | 16 |
| claude-sonnet-5 | 0.333 | 1.00 | 0.398 | 0 | 15 |
| claude-haiku-4-5 | 0.500 | 1.00 | 0.572 | 0 | 10 |

`recall@budget = 1.00` and **zero anticipation misses for every arm**: the
corpus has no discriminating power on the axis that matters, so it cannot
tell us whether extraction preserves need-to-know recall. The precision and
efficiency spread is explained entirely by how many records each arm writes —
the `native` arm writes only the 8 hand-authored memories and no noise, so it
is an unfair upper bound rather than a comparable system.

`tests/benchmarks/siq/extraction_adapter.py` extracts from the raw utterance
rather than planting the fixture's authored memory, which is the question SIQ
should be asking of extraction: **does model-assigned importance reproduce the
hand-tuned importance that query-blind injection ranks on?** With 5 targets it
cannot answer. Widening the SIQ corpus is tracked separately (#493).

## Recommendation

**1. Do not enable extraction by default, and add a warning to the docs.**
No arm, at any price, beat raw-turn ingestion on judged accuracy. The shipped
path is opt-in, so no default changes — but the docs should state that
write-time extraction currently trades answer accuracy for index compression,
with a measured 46-86% relative judged-accuracy loss on LoCoMo.

**2. If the pinned tier stays as-is, switch `EXTRACTION_MODEL` to
`claude-sonnet-5`.** It dominates the pinned `claude-opus-4-8` on every
measured axis at 40% of the cost. This is the direct answer to the question
parked in #461. It is a one-line change in
`src/popoto/extraction/claude.py` — but see (3) before making it, because
picking a tier ratifies a strategy that does not currently pay for itself.

**3. The higher-leverage fix is the prompt and the write policy, not the
model.** Two changes are indicated by the mechanisms above and should be
measured before any tier is locked in:

- **Never drop a turn.** Make extraction *additive*: write the verbatim turn
  and any extracted facts alongside it. That caps the downside at index size
  and makes the 20-53% evidence-destruction failure structurally impossible.
- **Soften "skip greetings, filler, and purely conversational scaffolding"**,
  which is what discards evidence-bearing short turns. Retain the source text
  on the record so retrieval can fall back to it.

**4. Do not generalize this to agent memory from live traffic.** LoCoMo is
graded on verbatim recall of dialogue specifics, which is close to the
worst case for a lossy summarizer. Extraction may still pay off for
long-horizon agent memory, where compression and entity linking matter more
than verbatim fidelity — but that is an untested hypothesis, and this study
does not support it.

## Confidence

Moderate on direction, low on magnitude. 77 judged items on 2 of 10 LoCoMo
dialogues, single seed, lexical retrieval only. The headline ordering (every
extraction arm below baseline on judged accuracy) is large — 0.364 → 0.052 at
the extreme — and has a directly measured mechanism (evidence destruction,
counted against ground-truth evidence turns, not inferred), so it is unlikely
to be sampling noise. The *gaps between tiers* are 4-15 items wide and should
not be treated as separated; the Sonnet-over-Opus result in particular rests
on a 4-item judged margin and a 6-point evidence-destruction gap. Not
replicated on LongMemEval-S, hybrid retrieval, or a second seed.

## Reproducing

```bash
# 1. Build the 2-dialogue bounded fixture from the cached LoCoMo dataset
python tests/benchmarks/make_locomo_subset.py conv-26,conv-30 /tmp/locomo_2dlg.json

# 2. Warm the extraction cache (this is the only step that costs money: ~$8.22)
python tests/benchmarks/warm_extraction_cache.py /tmp/locomo_2dlg.json \
    claude-opus-4-8,claude-sonnet-5,claude-haiku-4-5 12

# 3. Run each ingest arm (free — served entirely from the cache)
COMMON="--dataset locomo --fixture /tmp/locomo_2dlg.json --limit 100 \
        --sample stratified --seed 0 --retrieval-mode lexical --judged"
POPOTO_BENCH_DB=8 python -m tests.benchmarks.run_external $COMMON --extraction raw
POPOTO_BENCH_DB=8 python -m tests.benchmarks.run_external $COMMON --extraction heuristic
for m in claude-opus-4-8 claude-sonnet-5 claude-haiku-4-5; do
  POPOTO_BENCH_DB=8 python -m tests.benchmarks.run_external $COMMON \
      --extraction claude --extraction-model $m
done

# 4. SIQ axis (directional only, n=4 traces)
POPOTO_BENCH_DB=13 python -m tests.benchmarks.siq.run_siq_extraction \
    heuristic claude-opus-4-8 claude-sonnet-5 claude-haiku-4-5
```

Artifacts: `tests/benchmarks/results/external/locomo_20260806*_judged.{json,md}`
(`raw` keeps the unsuffixed baseline name; extraction arms carry `_ext-*`) and
`tests/benchmarks/results/siq/siq_extraction_axis_489.json`. Each artifact
embeds an `extraction` block with the arm, model, prompt SHA-256, token
counts, and estimated cost, so no number can be read without knowing which
ingest path produced it.
