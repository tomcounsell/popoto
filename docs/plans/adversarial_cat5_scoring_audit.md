---
status: Planning
type: investigation
appetite: Small
owner: valorengels
created: 2026-07-14
tracking: https://github.com/tomcounsell/popoto/issues/454
last_comment_id: 4934211140
---

# Adversarial (cat-5) Scoring Audit — Evidence Matching, Refusal-Metric Recommendation, Leaderboard-Parity Slice

## Problem

In the full LoCoMo lexical run (#447, PR #452), category-5 ("adversarial")
questions scored **comparably to other categories**: n=446, Recall@1=0.3341,
Recall@5=0.6031, Recall@10=0.6883, MRR=0.4581. Adversarial is historically the
*hardest* LoCoMo category industry-wide (paper: humans ≈89 F1 vs LLMs ≈2 F1),
and most leaderboards exclude it entirely. The suspicion (§1.3 of
`benchmarking_strategy_2026-07.md`): the category tests *refusal of
unanswerable queries*, so scoring it as any-hit retrieval means "the harness is
matching evidence spans instead of testing refusal — measuring the wrong
thing, not a strength."

The investigation must (per the 2026-07-10 addendum) end in a recommendation
among three options, **presented for maintainer sign-off** (scoring-semantics is
policy-level per the standing maintainer-decisions memo, not shippable
unilaterally):

1. Score cat-5 with a **refusal metric** (precision of "no-answer" decisions).
2. **Exclude** cat-5 from the retrieval table and report it separately — and in
   either case publish a 4-category **leaderboard-parity slice**.
3. **Keep as-is with a caveat** — only if the evidence audit shows the spans
   are genuinely meaningful for this category.

Reshaped under epic #456 (Track A): the recommendation is the scoring
foundation for #463 (confidence-gated retrieval), and the 4-category parity
slice becomes a standing published artifact.

## Freshness Check

**Baseline commit:** 0b4c629 (origin/main HEAD, release v1.8.0 merge) at plan time.
**Issue filed:** 2026-07-10; last comment 2026-07-10T10:03:52Z.
**Disposition:** Unchanged — but the audit **refutes the issue's central
suspicion** for this dataset snapshot (see Research).

**File:line references re-verified:**
- `tests/benchmarks/results/external/locomo_latest.json` (→ `locomo_20260708.json`) —
  present; `by_question_type` carries per-category n + R@1/5/10 + MRR. Confirmed.
- `tests/benchmarks/results/external/locomo_latest_hybrid.json` — present, same shape.
- `tests/benchmarks/metrics/retrieval.py` — `recall_at_k` (any-hit),
  `mean_reciprocal_rank`, pure functions, no Redis deps. The natural home for a
  pure `leaderboard_parity_slice` helper.
- `tests/benchmarks/run_external.py:306` `_by_question_type`, `:331`
  `compute_aggregate`, `:422` emits `by_question_type` into the report — the
  integration point to emit a forward-looking `leaderboard_parity` block.
- `tests/benchmarks/datasets/locomo.py` — `is_adversarial = "evidence" not in qa`;
  cat-5 items carry `adversarial_answer` and (in this snapshot) `evidence`.
- `docs/benchmarks.md:214-224` — existing factual cat-5 paragraph to be
  expanded with the audit finding + parity slice + sign-off recommendation.
- `~/.cache/popoto_benchmarks/locomo.json` — the committed dataset snapshot
  (10 dialogues, 1986 QA), audited directly.

**Commits on main since issue filed touching these files:** #468 (harness DB-0
isolation) and #466/#467 (docs publishing, vector baseline) — none change the
committed LoCoMo artifacts or the retrieval metrics. No drift.

## Prior Art

- **#453 (PR #466)**: Published benchmark results on the docs site; established
  the metric-family doctrine (never cross-compare recall vs judge-accuracy) and
  the current caveated cat-5 paragraph in `docs/benchmarks.md`. This plan
  extends that page.
- **#455 (PR #467)**: Vector-only diagnostic baseline — sibling §1.4 item.
- **benchmarking_strategy_2026-07.md §1.3 / §3.6**: source of the three options
  and the #463 confidence-gated-retrieval tie-in.
- No prior cat-5 evidence audit exists — this is the first.

## Research (evidence audit — the load-bearing finding)

Audit of all 446 cat-5 items in the committed snapshot
(`~/.cache/popoto_benchmarks/locomo.json`):

- **446/446 carry a populated `evidence` field** (1 dia_id: 432; 2 dia_ids: 14).
  The adapter's empty-relevant-set special case never fires — confirmed.
- **444/446 have an answerable `adversarial_answer` that the evidence span
  directly supports.** Examples: *"What did Caroline realize after her charity
  race?"* → `adversarial_answer="self-care is important"`, evidence D2:3 =
  *"I'm starting to realize that self-care is really important."* The span is
  the literal grounding for the answer.
- **Only 2/446** have a refusal-style answer (`"Not mentioned"`, both pointing
  at D18:2) — a negligible n.

**Conclusion:** In this snapshot, cat-5 is **not** a refusal category. It is a
set of grounded, answerable retrieval questions whose evidence spans are
genuinely meaningful. Scoring them as any-hit retrieval is measuring retrieval —
the same thing every other category measures — **not** "the wrong thing." The
issue's suspicion is **not confirmed for this data**; the evidence audit
supports **Option 3** (keep in the aggregate).

**Why the apparent paradox (paper's ≈2 F1 vs our 0.33 R@1) dissolves:** it is a
**metric-family difference**, not a scoring bug. The paper's ≈2 F1 is
*end-to-end judged-answer* accuracy (generate → judge); our 0.3341 is
*retrieval any-hit recall* (did the evidence turn get retrieved). Per the
MEMTIER anchor / #453 doctrine these families are non-convertible. Adversarial
is hard to *answer/judge*, not hard to *retrieve* — so a normal retrieval-recall
number on cat-5 is expected and honest, not a red flag.

**Parity slice (computed from committed `by_question_type`, no re-run):**
excluding cat-5 (n=446) from the 1986-QA run leaves **exactly n=1540** — the
common leaderboard variant count (1986 − 446 = 1540).

| Slice | n | R@1 | R@5 | R@10 | MRR |
|---|--:|--:|--:|--:|--:|
| lexical, full 5-cat | 1986 | 0.2986 | 0.5534 | 0.6400 | 0.4124 |
| lexical, 4-cat parity | 1540 | 0.2883 | 0.5390 | 0.6260 | 0.3991 |
| hybrid, full 5-cat | 1986 | 0.1667 | 0.4235 | 0.5403 | 0.2835 |
| hybrid, 4-cat parity | 1540 | 0.1552 | 0.4065 | 0.5181 | 0.2686 |

cat-5 sits near the mean, so excluding it barely moves the numbers — further
evidence it is neither anomalously easy nor a scoring artifact.

## Recommendation (for maintainer sign-off)

**Adopt Option 3 + the parity slice; do not build a refusal metric here.**

1. **Keep cat-5 in the full 5-category aggregate**, because the evidence audit
   shows its spans are genuinely meaningful for retrieval in this snapshot.
2. **Publish the 4-category (n=1540) leaderboard-parity slice** alongside the
   full aggregate, so readers can line Popoto up against the no-adversarial
   boards without a re-run.
3. **Mandatory caveat** (never a strength claim): this snapshot's cat-5 is
   *answerable and evidence-grounded*, not the paper's refusal-style
   adversarial; the number is **not** a refusal-capability signal and is **not**
   comparable to systems that report refusal-adversarial or that drop the
   category.
4. **Option 1 (refusal metric) does not apply to this dataset** — only 2/446
   items are genuinely unanswerable, so a "precision of no-answer decisions"
   metric has no signal here. Refusal capability (confidence-gated retrieval)
   belongs to **#463**, evaluated on a dataset that actually contains
   unanswerable questions.

## Scope / Implementation

**In scope (this PR):**
- `tests/benchmarks/metrics/retrieval.py`: add pure
  `leaderboard_parity_slice(by_question_type, exclude_categories)` →
  re-aggregate n-weighted R@1/5/10 + MRR over the retained categories. Single
  source of truth, computed from the per-category breakdown so any reader can
  reproduce it from the committed artifact.
- `tests/benchmarks/run_external.py`: emit a forward-looking
  `leaderboard_parity` block in the report **when the excluded category is
  present** (LoCoMo cat-5), leaving LongMemEval reports unchanged. Committed
  artifacts are **not** regenerated (would require a full multi-hour run); the
  published numbers are derived from the same pure function over the committed
  `by_question_type`.
- Tests: unit test for the pure function (weighting, missing-category,
  round-trip on full set) + a regression test asserting the slice against the
  committed `locomo_latest.json` (n=1540 and the four values above).
- `docs/benchmarks.md`: replace the current cat-5 paragraph with (a) the
  evidence-audit finding, (b) the parity-slice table, (c) the sign-off
  recommendation framing.
- Docs cascade (`/do-docs`).

**Explicitly out of scope (policy-level / other issues):**
- Changing the headline aggregate to exclude cat-5, or implementing a refusal
  metric in the harness — policy-level, requires maintainer sign-off; deferred
  to #463 for the capability.
- Re-running the benchmark / regenerating artifacts.
- Any tuning of retrieval.

## Risks

- **Rounding:** the slice is re-aggregated from 4-decimal per-category means, so
  it can differ from a raw re-aggregation in the 4th decimal. Acceptable and
  documented — reproducibility from the committed artifact is the priority, and
  the same pure function is the single source of truth.
- **Forward-only report block:** committed artifacts won't carry
  `leaderboard_parity` until the next full run. Mitigated: docs numbers are
  derived via the shared pure function and pinned by the regression test.

## Acceptance Criteria

- [ ] Evidence audit documented (444/446 answerable-grounded; 2 refusal; metric-family explanation).
- [ ] Recommendation (Option 3 + parity slice + caveat; Option 1 deferred to #463) stated for sign-off.
- [ ] `leaderboard_parity_slice` pure function + tests; regression test pins n=1540 and the four values.
- [ ] `run_external.py` emits `leaderboard_parity` for LoCoMo, LongMemEval unchanged.
- [ ] `docs/benchmarks.md` updated; `mkdocs build --strict` passes.
- [ ] `scripts/ci-local.sh` default gates pass.
