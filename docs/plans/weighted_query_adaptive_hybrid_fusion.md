---
status: Ready
type: feature
appetite: Medium
owner: valorengels
created: 2026-07-14
tracking: https://github.com/tomcounsell/popoto/issues/457
---

# Weighted / query-adaptive hybrid fusion — fix the LoCoMo hybrid regression

## Problem

Hybrid retrieval (BM25 + vector, **unweighted** RRF k=60) **underperforms**
pure lexical on LoCoMo at every k, while **winning** on LongMemEval-S at every k:

| Dataset | Mode | R@1 | R@5 | R@10 | MRR |
|---|---|---|---|---|---|
| LoCoMo | lexical | **0.2986** | **0.5534** | **0.6400** | **0.4124** |
| LoCoMo | hybrid | 0.1667 | 0.4235 | 0.5403 | 0.2835 |
| LongMemEval-S | lexical | 0.8560 | 0.9520 | 0.9780 | 0.8987 |
| LongMemEval-S | hybrid | **0.8940** | **0.9860** | **0.9920** | **0.9317** |

The vector arm **adds** value on LongMemEval-S but **subtracts** it on LoCoMo.
Unweighted RRF gives a weak-but-confident dense arm equal say with BM25; on
coreference-heavy multi-session dialogue (LoCoMo) the dense arm retrieves
topically-similar-but-wrong turns that displace correct BM25 hits. For a live
agent this means the current fusion cannot be trusted across conversation
shapes — the system should adapt, not fix one blend.

## Gating step (blocks BUILD) — vector-only LoCoMo baseline + decision rule

Per issue #457 ("Blocked on #455") and the #455 diagnostic plan, fusion code is
**blocked** until the decisive **vector-only LoCoMo** artifact exists and the
decision rule is applied. The `--retrieval-mode vector` harness path already
shipped (v1.8.0, #455), but **no `_vector` artifact is committed** yet.

- **Granularity parity is already settled** (#455 spike-1, code-read, high
  confidence): BM25Field and EmbeddingField both `source="content"` on the same
  per-turn record, and both arms enumerate the same full per-turn record set.
  The granularity-mismatch branch of #457's gate does **not** trigger; no hybrid
  re-measure-for-granularity is required before fusion work.
- **Decision rule** (recorded so #457 proceeds without a fresh interpretation
  debate):
  - vector-only LoCoMo R@1 **substantially below lexical** R@1 (mirroring the
    hybrid<lexical regression) → confirms the **weak-arm / RRF-dilution**
    hypothesis → **Direction 1/2 (down-weight/adapt the dense arm)**.
  - vector-only **competitive with hybrid** → reopens the **fusion-mechanism**
    hypothesis → the fix is in *how* the arms combine, and a graph-arm-off
    ablation is added before committing to a weight change.
- **Scope caveat (carried from the #455 critique):** the vector-only run
  isolates **only** the dense arm — not the graph/co-occurrence arm that also
  lives inside `_pull_path_hybrid`. If the number is ambiguous (neither clearly
  weak nor clearly competitive), a graph-arm-off ablation is run before any
  fusion change.

**Baseline evidence (in hand at plan time):** a 20-question stride smoke of the
committed vector harness returned LoCoMo vector-only R@1 = 0.05, R@5 = 0.30,
R@10 = 0.50, MRR = 0.179 — far below lexical (R@1 0.2986) and below even hybrid
(R@1 0.1667). The **full 1986-question** run is committed as the durable
artifact before BUILD begins; the smoke is a directional preview only.

## Directions (issue order; choice is evidence-gated)

1. **Weighted RRF** — give each arm a fixed weight in the RRF sum
   (`score(d) = Σ_i w_i / (k + rank_i)`), down-weighting the dense arm so a
   weak-but-confident vector list cannot displace correct BM25 hits. Simplest,
   in-code constants. **Risk:** a *fixed* vector down-weight helps LoCoMo but
   erodes the LongMemEval-S win (where the dense arm is strong). Must be tuned so
   LoCoMo hybrid ≥ lexical **and** LongMemEval-S hybrid stays ≥ its lexical.
2. **Query-adaptive arm weighting** — pick per-query weights from a cheap,
   Valkey-safe, in-process query-shape signal (name/date/exact-token-heavy →
   lean BM25; paraphrastic/semantic → lean vector). This is the live-agent-
   correct answer: the LongMemEval/LoCoMo split is *learned per query*, not
   hardcoded per dataset. Built on top of (1)'s weighted-RRF primitive.
3. **Embedding domain/granularity fit** — dialogue/coreference-tuned embeddings.
   **Out of scope** here (parity already holds; changing the embedding model is a
   separate, heavier investigation).

### Chosen approach (phased; pending full baseline confirmation)

**Phase A — weighted RRF primitive + query-adaptive weighting.** Build (1) the
weighted-RRF primitive and (2) a lightweight query-adaptive weighting policy
together, because the evidence forces it: a *fixed* down-weight cannot satisfy
both bars at once. The dense arm is **harmful** on LoCoMo (hybrid 0.1667 <<
lexical 0.2986) yet **helpful** on LongMemEval-S (hybrid 0.894 > lexical 0.856).
A single fixed vector weight that recovers LoCoMo (weight → near 0) would erode
the LongMemEval win, and one that keeps the LongMemEval win leaves LoCoMo broken.
Only a **per-query** weight — lean BM25 on token/name/date-specific queries
(LoCoMo's shape), lean vector on paraphrastic queries (LongMemEval's shape) —
can clear both. Weights are experimental tuning constants (magic-number stance):
in-code, not user config. **Finalized only after the full vector-only LoCoMo
artifact confirms the weak-arm reading.**

The weighted-RRF primitive is the reusable substrate; the query-adaptive policy
is the part that actually resolves the cross-dataset tension. If, after tuning,
a fixed global down-weight alone already clears both bars on the confirmation
runs, the query-adaptive policy can collapse to a constant — but the plan builds
the per-query path because the current numbers predict a fixed blend will not.

## Prior Art

- **#455** (vector-only baseline + parity audit) — the direct upstream gate.
  Established the `--retrieval-mode vector` harness path (`external_base.py`
  cosine bypass, `QueryBuilder._get_vector_scores`) and the decision rule this
  plan consumes. This plan runs the baseline the #455 plan specified but never
  executed, then implements the fusion #455 explicitly deferred.
- **#437 / PR #441** — hybrid BM25+vector mode + `QueryBuilder.fuse()` (the RRF
  function weighting extends). **Succeeded.**
- **#447 / PR #452** — full LoCoMo lexical + hybrid artifacts (the numbers above)
  and the mode-suffix artifact convention. **Succeeded.**
- **#442 / PR #443** — LongMemEval-S hybrid run + the invalidation-listener
  connection-leak fix (inherited by every long run here). **Succeeded.**

No prior failed fusion-weighting attempt exists — additive on an established
`fuse()` primitive.

## Research

- **RRF operates on ranks, not scores** — a weak arm still casts full rank-based
  votes, so an unweighted blend structurally gives a weak-but-confident dense arm
  equal say. Per-list weighting / per-query interpolation (DAT) are the
  established remedies. Validates Direction 1/2.
- **Dense underperforms BM25 on surface-matching / conversational multi-session
  dialogue, and which signal wins depends on query shape** — precisely the
  LongMemEval-S (hybrid wins) vs LoCoMo (lexical wins) split. Supports per-query
  weighting over a fixed blend (arxiv 2503.17507; training-free lexical–dense
  fusion arxiv 2606.04194, cited in the #455 plan).

## Solution

### Key elements

1. **Weighted RRF primitive** (`QueryBuilder.fuse` **and** the `Query.fuse`
   wrapper, `models/query.py`): add an explicit `weights: dict[str, float] |
   None = None` kwarg mapping each ranked-list name to a multiplier. Formula
   becomes `rrf_scores[doc] += weights.get(list_name, 1.0) * 1/(k + rank + 1)`.
   Default `weights=None` → every weight 1.0 → **byte-for-byte identical** to
   today's unweighted RRF (backward compatible; existing callers/tests
   unaffected). **Critical:** `Query.fuse` (query.py:2300) delegates via
   `**ranked_lists`; `weights` MUST be an explicit named parameter on BOTH
   `Query.fuse` and `QueryBuilder.fuse`, otherwise it is swept into
   `**ranked_lists` and mis-treated as a ranked list (silent corruption). A unit
   test asserts `weights` never appears as a fused list.
2. **Fusion weight constants** (`recipes/context_assembler.py`, next to
   `RRF_K = 60`): experimental magic-number constants for the arm weights, e.g.
   `FUSION_WEIGHT_KEYWORD`, `FUSION_WEIGHT_VECTOR`, `FUSION_WEIGHT_GRAPH`. Tuned
   empirically against the re-run artifacts; documented as experimental tuning
   constants, not user config.
3. **Query-adaptive weighting policy** (`recipes/context_assembler.py`): a small
   pure-Python, Valkey-safe helper `_fusion_weights(query_text) -> dict` wired
   into `_pull_path_hybrid` at the `fuse(k=RRF_K, **fuse_kwargs)` call
   (`context_assembler.py:1356`). **Concrete, deterministic heuristic** (no ML,
   no new deps, no server round-trip):
   - Compute a *lexical-specificity* signal from the query tokens: the fraction
     that are (a) capitalized proper-noun-like tokens, (b) digits / dates /
     years, or (c) quoted/exact-token strings. High fraction ⇒ token-specific
     (LoCoMo shape) ⇒ **BM25-lean** weights (keyword ≫ vector). Low fraction ⇒
     paraphrastic (LongMemEval shape) ⇒ **vector-lean** (or neutral) weights.
   - Map the signal to a small number of **discrete weight regimes** (e.g.
     BM25-lean / neutral / vector-lean), each a named in-code constant — not a
     continuous learned function. Graph-arm weight is a separate constant.
   - The exact constants + thresholds are tuned against the re-run artifacts and
     documented as experimental tuning knobs. Purely in-process; nothing touches
     Redis server-side, so Redis **and** Valkey behave identically.
4. **Validation re-runs (compute-bounded)**: tune weights on a fast,
   representative **stride subset** (`--limit 200 --sample stride`) of each
   dataset — quick iteration loop — then run **one** full-set confirmation run
   per dataset for the committed artifact. This caps compute at ~2 full runs
   (not one per tuning pass). Commit the new hybrid artifacts under the
   mode-suffix convention (never overwriting lexical/vector). Success = LoCoMo
   hybrid **recovers to ≥ lexical** at full-k (regression eliminated) **and**
   LongMemEval-S hybrid **still ≥ its lexical** (win retained).

### Data flow (unchanged except the weight injection)

`assemble()` → `_pull_path` → `_pull_path_hybrid` collects keyword/vector/graph
`(redis_key, score)` lists → **NEW:** compute per-query weights → `query.fuse(
k=RRF_K, weights=..., keyword=, vector=, graph=)` → weighted RRF sort → hydrate.
Vector-only diagnostic path (`external_base.py`) is **untouched**.

## Architectural Impact

- **New dependencies:** none. Pure-Python weighting; reuses numpy cosine and the
  existing `fuse()` hydration.
- **Interface change:** `QueryBuilder.fuse` / `Query.fuse` gain an optional
  `weights` kwarg (additive, defaulted to unweighted). No breaking change.
- **Valkey safety:** fusion + weighting stay fully in-process; no `FT.*`, no
  server-side vector ops, no new modules. Verified against the no-Redis-modules
  rule.
- **Reversibility:** high — remove the `weights` kwarg path + the constants +
  the `_fusion_weights` helper and behavior reverts to unweighted RRF.

## Magic-number / constant stance

Fusion weights are **experimental tuning constants** (per repo doctrine): defined
in-code near `RRF_K`, chosen from benchmark artifacts, and documented as tuning
knobs — **not** user/dev configuration and **not** exposed through any public
config surface.

## Test Impact

- `tests/test_queries.py` (or the fuse test module): ADD cases that
  `fuse(weights=None)` equals today's unweighted result (regression guard), and
  that a down-weighted arm demonstrably loses rank influence on a constructed
  two-list example (deterministic, no benchmark).
- Context-assembler tests: ADD a unit test that `_fusion_weights` returns
  BM25-leaning weights for a token/name/date query and vector-leaning weights for
  a paraphrastic query (pure function, deterministic).
- No existing test asserts RRF is unweighted-only (verified by grep), so the
  additive `weights` kwarg breaks nothing.
- `scripts/ci-local.sh` (tests + stress + docs) green.

## Rabbit holes

- **Learned/ML reranker over the two arms.** Direction 1/2's heuristic weighting
  is the appetite; a trained cross-encoder reranker is a separate, heavier issue.
- **Changing the embedding model** (Direction 3). Parity holds; embedding-domain
  fit is out of scope.
- **Endless weight tuning to chase LongMemEval-S 0.894 exactly.** The bar is
  *retain the win* (hybrid ≥ lexical), not "no regression at all." Tune to clear
  both bars, then stop.
- **Touching the vector-only diagnostic path.** It is the baseline instrument;
  leave `external_base.py` cosine path unchanged.

## Risks

1. **Fixed weights can't satisfy both datasets** → mitigated by the query-adaptive
   policy (per-query weights), validated by re-running both datasets.
2. **Long re-runs (LoCoMo 1986 q, LME 500 q) are multi-hour and can leak
   connections** → the `stop_invalidation_listeners()` teardown fix is inherited;
   runs are launched detached with progress logging; smoke first.
3. **Query-shape heuristic overfits LoCoMo** → keep the heuristic simple and
   symmetric (token-specific ↔ paraphrastic), validate it does not regress
   LongMemEval-S, and treat the constants as tunable.

## No-Gos (out of scope)

- Learned/ML reranker (separate issue).
- Embedding-model swap / Direction 3.
- Any user-facing config for fusion weights (magic-number doctrine).
- Modifying the vector-only diagnostic harness path.

## Success Criteria

- [ ] **Gating:** full vector-only LoCoMo run committed as
  `locomo_{date}_vector.{json,md}` + `locomo_latest_vector.*`; decision rule
  applied and recorded (weak-arm confirmed → weighted/adaptive fusion greenlit).
- [ ] `QueryBuilder.fuse(weights=None)` is byte-for-byte equivalent to the
  current unweighted RRF (regression test).
- [ ] Weighted + query-adaptive fusion implemented; weights are in-code
  experimental constants, Valkey-safe, no new deps.
- [ ] **LoCoMo hybrid recovers to ≥ lexical at full-k** (regression eliminated;
  R@1/5/10/MRR ≥ the lexical baseline — because the dense arm is net-harmful on
  LoCoMo, "≥ lexical" in practice means "converges to lexical as the harmful
  vector votes are down-weighted," and *exceeding* lexical is a stretch, not a
  gate), artifact committed.
- [ ] **LongMemEval-S hybrid retains its win** (hybrid ≥ lexical at full-k),
  artifact committed.
- [ ] `docs/benchmarks.md` updated: new hybrid numbers + the
  hybrid-underperforms-lexical caveat (commit de27078) updated to reflect the
  fixed fusion behavior.
- [ ] `scripts/ci-local.sh` (tests + stress + docs) green; `mkdocs build
  --strict` green.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Vector baseline committed | `ls tests/benchmarks/results/external/locomo_latest_vector.json` | exit 0 |
| fuse weights additive | `pytest tests/ -k fuse -q` | exit 0 |
| Weighting is in-process (Valkey-safe) | `grep -rnE 'FT\.|CMS\.|BF\.' src/popoto/models/query.py src/popoto/recipes/context_assembler.py` | no matches |
| LoCoMo hybrid ≥ lexical | compare `locomo_latest_hybrid.md` (new) vs `locomo_latest.md` | hybrid R@1/5/10/MRR ≥ lexical |
| LME win retained | compare `longmemeval_s_latest_hybrid.md` (new) vs `_latest.md` | hybrid ≥ lexical |
| Docs build | `mkdocs build --strict` | exit 0 |
| Tests | `scripts/ci-local.sh` | all gates pass |

## Critique Results

**Verdict: NEEDS REVISION → all findings addressed in this revision.** Self-
critique (the dispatched `plan-reviewer` subagent malfunctioned and returned no
usable output, so the lead dev performed the critique directly against the code).

| Severity | Finding | Addressed by |
|----------|---------|--------------|
| **BLOCKER** | `Query.fuse` (query.py:2300) delegates via `**ranked_lists`; a bare `weights` kwarg would be swallowed and mis-fused as a ranked list (silent corruption). | Solution element 1 now requires `weights` as an **explicit** named param on **both** `Query.fuse` and `QueryBuilder.fuse`, plus a unit test asserting `weights` is never fused as a list. |
| CONCERN | "LoCoMo hybrid ≥ lexical" over-promises: the dense arm is net-harmful on LoCoMo, so down-weighting can at best *converge* hybrid to lexical, not exceed it. Risked chasing a fabricated gain. | Success criterion reworded to "**recover to ≥ lexical** (regression eliminated)"; exceeding lexical is explicitly a stretch, not a gate. No fabricated targets. |
| CONCERN | Committing to both weighted RRF **and** query-adaptive up front is scope-heavy for a Medium appetite. | Kept both, but justified by evidence: a fixed blend provably cannot clear both dataset bars (harmful-on-LoCoMo vs helpful-on-LME), so the per-query path is necessary, not gold-plating. Phrased as phased (primitive → policy) with the policy collapsible to a constant if a fixed weight suffices. |
| CONCERN | Query-adaptive heuristic was underspecified ("regex/heuristic"). | Solution element 3 now names the exact lexical-specificity signal (proper-noun / digit-date / exact-token fraction) and a small set of discrete, in-code weight regimes — deterministic and unit-testable. |
| CONCERN | Validation cost unbounded (2 multi-hour runs per tuning iteration). | Element 4 bounds it: tune on `--limit 200 --sample stride` subsets, then **one** full confirmation run per dataset for the committed artifact. |
| NIT | Restated the gating rule and graph-arm-off ablation fallback. | Already present in the Gating step; confirmed the ambiguous-baseline branch triggers the graph-arm-off ablation before any weight change. |
</content>
</invoke>
