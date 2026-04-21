---
status: docs_complete
type: feature
appetite: Large
owner: valorengels
created: 2026-04-20
tracking: https://github.com/tomcounsell/popoto/issues/352
last_comment_id:
---

# Metacognitive Memory Layer — Retrieval Self-Assessment, FOK, and Adaptive Strategy

## Problem

Popoto's 14 agent-memory primitives are mechanically sophisticated signal processors. They track, rank, and retrieve memories, but they cannot reason about the quality of their own decisions. An agent using `ContextAssembler.assemble()` today gets a ranked list with no indication of whether the retrieval is trustworthy, no feedback loop to improve future retrievals, and no way to express "my context feels shaky."

**Current behavior:**
- `ConfidenceField.get_confidence()` returns a per-memory Bayesian score, but `ContextAssembler` never surfaces the aggregate — the caller cannot ask "what's the average confidence of the context I just got?"
- `ContextAssembler.assemble()` uses fixed `score_weights` (e.g., `{"relevance": 0.6, "confidence": 0.3}`) passed at construction; there is no mechanism for weights to adapt based on observed retrieval quality history.
- `PredictionLedgerMixin` exposes `get_highest_errors(model_class, partition, limit)` (per-instance top-K errors) but no aggregation across instances grouped by arbitrary dimensions (task type, source, time bucket).
- `ExistenceFilter.might_exist()` is wired into `ContextAssembler._pull_path` as a pre-check to short-circuit misses, but it is not exposed as a feeling-of-knowing signal the *caller* can consult before asking to retrieve.
- `ObservationProtocol.VALID_OUTCOMES` is `{"acted", "dismissed", "deferred", "contradicted"}`. There is no outcome for "I consumed this memory (read it, reasoned over it) but didn't act yet" — the "used" signal design-spec'd in the issue is absent.
- Observation signals (`ACTED_CONFIDENCE_SIGNAL`, `CONTRADICTED_CONFIDENCE_SIGNAL`, etc.) are applied uniformly regardless of the credibility of the source that generated them.

**Desired outcome:**
- `ContextAssembler.assess()` (new method, optional) returns a `RetrievalQuality` dataclass that the agent can inspect to decide how much to trust its context.
- `ContextAssembler.assemble()` optionally attaches a `RetrievalQuality` to `AssemblyResult.metadata["quality"]` when `assess_quality=True` is passed — default off.
- `PredictionLedgerMixin.error_summary(group_by=...)` aggregates prediction errors by caller-specified dimensions and returns summary statistics plus outlier groups.
- `ObservationProtocol` gains a `"used"` outcome distinct from `"acted"`: tracks that the agent consumed the memory, but not whether the memory was incorporated into the response.
- An `AdaptiveAssembler` class (wraps `ContextAssembler`) adjusts `score_weights` over time via an autoresearch-style keep/revert loop — keep changes that improve a measurable quality metric, revert those that don't.
- All metacognitive features are optional and purely additive: existing `ContextAssembler` API is unchanged, existing tests pass without modification.

## Freshness Check

**Baseline commit:** `debb1e4` (2026-04-20)
**Issue filed at:** 2026-04-05T07:06:50Z (15 days before plan time)
**Disposition:** Minor drift

**File:line references re-verified:**
- `src/popoto/fields/` (issue referenced as ContextAssembler's location) — **drifted**: `ContextAssembler` lives at `src/popoto/recipes/context_assembler.py` (509 lines); `PolicyCache` lives at `src/popoto/recipes/policy_cache.py`. `ConfidenceField`, `PredictionLedgerMixin`, `ExistenceFilter`, and `ObservationProtocol` do live under `src/popoto/fields/`. Claims hold — the metacognitive layer still needs to touch these files; the layer will be a new module under `src/popoto/recipes/`.
- `PredictionLedger` error aggregation — **confirmed absent**: `prediction_ledger.py:424 get_highest_errors()` returns `(member_key, error)` tuples per partition but has no `group_by` mechanism. Adding one is net-new.
- `ExistenceFilter.might_exist()` wiring to FOK — **confirmed absent**: `context_assembler.py:378` uses `definitely_missing()` as a pre-filter for the pull path; there is no FOK computation anywhere in code. A Grep for `FOK|feeling.of.knowing|RetrievalQuality` across `src/` returns zero hits.
- `ObservationProtocol` "used" outcome — **confirmed absent**: `observation.py:45 VALID_OUTCOMES = {"acted", "dismissed", "deferred", "contradicted"}`. "used" is not there.

**Cited sibling issues/PRs re-checked:**
- **#351** (prerequisite) — closed 2026-04-17T15:32:55Z via PR #361 (merged). Mechanical primitives are correctly tuned; the prerequisite is satisfied.
- **#296** (diagnosed flat sensitivity) — closed 2026-03-30. Feeds into #351's work, already absorbed.
- **#233** (ContextAssembler) — closed 2026-03-20. The recipe this plan extends.
- **#232** (PolicyCache) — closed 2026-03-20. Downstream consumer of RetrievalQuality.
- **#228** (PredictionLedger) — closed 2026-03-20. This plan extends it with `error_summary`.

**Commits on main since issue was filed (touching referenced files):**
- `c03e5ba` (2026-04-17) — "Fix override reach, decouple family ground truth, apply sweep results (#351) (#361)": landed the #351 dependency. Changes `fields/constants.py`, `benchmarks/`, but does not modify `context_assembler.py`, `prediction_ledger.py`, `observation.py`, or `existence_filter.py` in ways that affect the metacognitive layer's planned extensions.
- `5c9b5c1` (2026-04-19) — "Close 5-constant variance target via new family scenarios (#362) (#364)": benchmark scenarios only; no source changes relevant to metacognition.

**Active plans in `docs/plans/` overlapping this area:** none. The only plan that mentions `#352` is `apply_experiment_learnings.md:226`, which explicitly declares itself as this plan's prerequisite.

**Notes:** Minor drift — issue referenced `fields/` broadly; two components actually live under `recipes/`. This is a labeling issue, not a semantic one. The plan is written against the actual file layout.

## Prior Art

Search for closed issues on "metacognit" and "retrieval quality FOK" returned zero results. This is greenfield work. The closest prior art is the set of primitives this plan extends (#228, #232, #233), all of which closed successfully.

- **Issue/PR #351**: "Apply experiment learnings" — landed via PR #361. Correctly-tuned mechanical primitives are the foundation this plan builds on. Relevance: direct prerequisite.
- **Issue/PR #233**: "Add ContextAssembler — retrieval-to-injection bridge" — landed the capstone recipe. This plan adds an optional metacognitive companion, does not modify the assembly pipeline.
- **Issue/PR #228**: "Add PredictionLedger — outcome tracking with auto-resolution" — landed the per-instance error tracking. This plan adds an aggregation layer on top.
- **No prior attempts** to build a metacognitive layer exist. Greenfield.

## Research

**Queries used:**
- "metacognition feeling-of-knowing FOK retrieval confidence calibration LLM 2026"
- "karpathy autoresearch compounding baseline feedback loop pattern"
- "adaptive retrieval weights feedback outcome quality RAG 2026"

**Key findings:**

- **Anthropic's P(IK) — "probability I know"**. Recent LLM research introduces the `P(IK)` metric as a hallucination-reduction signal, operationally mapping to FOK from human metacognition. Source: [Frontiers in AI: Metacognition of ChatGPT in confidence judgements](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2026.1694192/full). This validates surfacing FOK as an explicit pre-retrieval signal — the design-spec'd formula in popoto matches current LLM research direction.

- **Metacognitive sensitivity vs. bias (Fleming & Lau framework)**. Metacognitive sensitivity is the correlation between accuracy and confidence; metacognitive efficiency is sensitivity normalized by task performance; metacognitive bias is the base-rate of confidence regardless of accuracy. For popoto's `RetrievalQuality`, this argues for reporting *both* average confidence (bias) and confidence variance (sensitivity proxy) — a single scalar is insufficient. Source: [Decoupling Metacognition from Cognition (AAAI 2025)](https://ojs.aaai.org/index.php/AAAI/article/view/34723).

- **Karpathy AutoResearch pattern — keep/revert loop**. Each iteration proposes a change; if a measurable score improves, commit as new baseline; if not, revert (e.g., git reset). ~12 experiments/hour overnight. Four runs × 50 experiments surfaced 20 real improvements on hand-tuned code. Source: [karpathy/autoresearch on GitHub](https://github.com/karpathy/autoresearch) and [DataCamp guide](https://www.datacamp.com/tutorial/guide-to-autoresearch). This directly informs Tier 3's `AdaptiveAssembler`: propose a weight delta, measure quality over a rolling window, keep if improved, revert if not. No ML training, just in-memory state and a quality metric.

- **PatchRAG / Feedback adaptation for RAG**. Recent work shows feedback can adjust retrieval behavior at inference time without retraining — the relevant metrics are *correction lag* (how fast feedback propagates) and *post-feedback performance* (reliability on semantically related queries after feedback). Source: [arxiv 2604.06647](https://arxiv.org/abs/2604.06647). This supports the "no ML training pipeline" constraint — the AdaptiveAssembler can work purely via rolling-window statistics and deterministic weight updates.

- **Dissociation between LLM performance and metacognitive behavior**. GPT-4's confidence shows a "shallow confidence-accuracy mapping" — confidence reflects output structure rather than internal uncertainty. Implication for popoto: don't let the LLM self-report trust; surface a *mechanical* retrieval quality signal the agent reads instead of introspecting. This is precisely what `RetrievalQuality` provides.

## Data Flow

End-to-end flow for a retrieval with metacognitive signals enabled:

1. **Agent calls `ContextAssembler.assemble(query_cues, assess_quality=True)`.**
2. **Pull path runs** (unchanged from current): ExistenceFilter pre-check → CompositeScoreQuery → CoOccurrence propagation. Candidates emerge with scores.
3. **Push path runs** (unchanged): CyclicDecayField scan above surfacing threshold. Proactive records emerge.
4. **Merge + dedup + budget-select** (unchanged): produces `selected` list.
5. **[NEW] Quality assessment runs** (only when `assess_quality=True`): compute `RetrievalQuality`:
   - `avg_confidence`: mean of `ConfidenceField.get_confidence()` across `selected`. Defaults to 1.0 if no ConfidenceField on model.
   - `score_spread`: coefficient of variation (stddev / mean) of composite scores attached during retrieval. Falls back to `0.0` when `abs(mean) < 1e-9` (empty scores or all-zero) — `stddev / mean` is undefined otherwise.
   - `fok_score`: for each cue value in `query_cues`, compute `0.4 * cue_familiarity + 0.4 * partial_retrieval_count + 0.2 * subthreshold_activation`, then average across cues. Components:
     - `cue_familiarity = 1.0 if (self._existence_filter and self._existence_filter.might_exist(self.model_class, str(cue_value))) else (0.5 if self._existence_filter is None else 0.0)`. Note the signature: `might_exist(model_class, fingerprint)` per `src/popoto/fields/existence_filter.py:429` — not `might_exist(cue)`. The `str(cue_value)` coercion matches the existing `_pull_path` pattern at `context_assembler.py:379` (`definitely_missing(self.model_class, str(cue_value))`).
     - `partial_retrieval_count = min(len(all_pull_candidates), max_items) / max_items`
     - `subthreshold_activation = count(candidates with score < surfacing_threshold but > 0) / max(len(all_pull_candidates), 1)`
   - `staleness_ratio`: fraction of `selected` with DecayingSortedField score below the model's configured decay threshold.
6. **[NEW] Quality attaches** to `AssemblyResult.metadata["quality"]`. Existing consumers that don't check `metadata["quality"]` are unaffected.
7. **Post-effects run** (unchanged): on_read, on_surfaced, competitive suppression.
8. **Output formatting** (unchanged): structured/xml/natural.
9. **Agent consumes `AssemblyResult`**. If quality signals are used, agent may decide to retry with different cues, widen scope, or caveat its downstream answer.
10. **[NEW] Later, agent reports outcome** via `ObservationProtocol.on_context_used(instances, outcome_map)` where outcome may now be `"used"` (agent consumed memory but didn't act on it — e.g., dismissed after reading). This drives the new feedback signal.
11. **[NEW] If `AdaptiveAssembler` wraps the assembler**, it records each `(query_cues, RetrievalQuality, downstream_outcome)` tuple in a rolling window. Periodically (every N calls), it proposes a `score_weights` adjustment, measures quality over the next M calls, and keeps the adjustment if avg quality improved, else reverts.

## Architectural Impact

- **New dependencies**: None beyond what popoto already uses. No new PyPI packages. No Redis modules.
- **Interface changes**:
  - `ContextAssembler.assemble()` gains an `assess_quality: bool = False` parameter (backward compatible). When True, populates `AssemblyResult.metadata["quality"]`. Rationale: per-call parameter matches the existing pattern for `agent_id` / `partition_filters` on `assemble()`; no constructor knob is added.
  - `ContextAssembler.assess(query_cues, ...) -> RetrievalQuality` — new standalone method for pre-retrieval FOK checks.
  - New dataclass `RetrievalQuality` in `src/popoto/recipes/context_assembler.py`.
  - `PredictionLedgerMixin.error_summary(model_class, partition=None, group_by=None) -> dict` — new classmethod.
  - `ObservationProtocol.VALID_OUTCOMES` gains `"used"`. Effect mapping for `"used"`: discards staged reads, does NOT weaken cycles, does NOT update confidence, auto-resolves predictions as `"used"` (new key in `_pl_auto_resolve_errors`). This is intentionally a weaker-than-`acted` signal.
  - New `AdaptiveAssembler` class in a new file `src/popoto/recipes/adaptive_assembler.py`. Wraps a `ContextAssembler`. Not a modification to `ContextAssembler` itself.
  - `Defaults` gains two new constants: `PL_AUTO_RESOLVE_USED` (prediction error for "used" outcome, default 0.3) and `ADAPTIVE_QUALITY_WINDOW_SIZE` (rolling window size, default 20).
- **Coupling**:
  - `RetrievalQuality` reads from existing fields (ConfidenceField, ExistenceFilter, DecayingSortedField) via their existing public APIs — no new coupling direction.
  - `AdaptiveAssembler` depends on `ContextAssembler` + `RetrievalQuality`. No reverse dependency.
  - `ObservationProtocol`'s new `"used"` outcome adds one branch to `_apply_outcome` dispatch.
- **Data ownership**: `AdaptiveAssembler` owns an in-memory rolling window. No Redis writes for the adaptation loop itself — it's pure in-process state. (This is deliberate: adaptation is per-process and stateless across restarts, matching the autoresearch pattern where each session's learnings are reflected in its final `score_weights`. Persisting cross-session adaptation is deferred to v2.)
- **Reversibility**: All new APIs are additive and off-by-default. Reverting is a matter of removing the new files and the single `"used"` branch in `observation.py`. Existing tests pass throughout.

## Appetite

**Size:** Large

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1-2 (scope alignment after Tier 1 lands; tradeoff call on AdaptiveAssembler's persistence before Tier 3)
- Review rounds: 2+ (Tier 1 + Tier 2 land as PR 1; Tier 3 lands as PR 2 or inline at reviewer's preference)

Three tiers, each independently shippable. Tier 1 delivers on 4 of 6 acceptance criteria (RetrievalQuality, FOK, error_summary, "used" outcome). Tier 2 delivers nothing new on its own — it's prep for Tier 3 (wiring `"used"` through the pipeline, extending PredictionLedger's auto-resolve map). Tier 3 delivers the remaining criterion (integration test for adaptive weight adjustment). The large appetite reflects depth across three primitives, not breadth across many files.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| #351 landed | `git log --oneline --all \| grep -c "#351"` (expect >= 1) | Metacognitive layer depends on correctly-tuned mechanical primitives |
| Redis/Valkey reachable | `redis-cli -u ${REDIS_URL:-redis://localhost:6379} ping` (expect "PONG") | Integration tests require a live Redis |
| `pytest` and dev deps | `.venv/bin/pytest --version` | Test harness |

Run all checks manually; no prerequisite automation required.

## Solution

### Key Elements

- **`RetrievalQuality` dataclass** (new, in `context_assembler.py`): 4 required fields (`avg_confidence`, `score_spread`, `fok_score`, `staleness_ratio`) plus optional `score_distribution` (full list of scores for histogram analysis) and `per_cue_fok` (dict mapping cue value → FOK component breakdown for debugging).
- **`ContextAssembler.assess(query_cues, partition_filters=None) -> RetrievalQuality`** (new method): computes quality *without* running the full assembly pipeline. Reads ExistenceFilter for cue_familiarity, runs a low-limit composite score to count candidates above/below threshold. Cheap — meant to be called before `assemble()` to decide whether retrieval is worth the round-trip.
- **`ContextAssembler.assemble(..., assess_quality=False)`**: per-call parameter on `assemble()` only (NOT on `__init__`). When True, computes `RetrievalQuality` on the actual `selected` list (not on a pre-retrieval probe) and attaches to `metadata["quality"]`. When False (default), existing behavior bit-for-bit. This matches the existing per-call parameter pattern for `agent_id` / `partition_filters`.
- **`PredictionLedgerMixin.error_summary(model_class, partition="default", group_by=None, limit=100) -> dict`**: aggregates errors from the error sorted set. When `group_by=None`, returns overall stats: `{count, mean, stddev, p50, p90, p99, max}`. When `group_by` is a callable `(member_key, error) -> group_label`, returns `{group_label: {...stats...}}`. When `group_by` is a string like `"weekday"` or `"hour"`, uses a built-in bucketing function over `resolved_at` timestamps read from the meta hash.
- **New `"used"` outcome in `ObservationProtocol`**: `VALID_OUTCOMES = {"acted", "dismissed", "deferred", "contradicted", "used"}`. Applied effects: discard staged reads, auto-resolve predictions with mapped error value, no confidence or cycle updates. Rationale: "used" means the agent consumed the memory but neither acted on it nor dismissed it — it was useful context without being directly actionable, a common case that `"acted"` overcounts and `"deferred"` undercounts.
- **`AdaptiveAssembler`** (new class in `src/popoto/recipes/adaptive_assembler.py`): wraps a `ContextAssembler` with an autoresearch keep/revert loop. Records `(RetrievalQuality, downstream_outcome_signal)` in a rolling window per call. Every `ADAPTIVE_QUALITY_WINDOW_SIZE` calls, proposes a symmetric weight perturbation (e.g., shift 0.05 from relevance to confidence), measures the next window's avg quality, and keeps or reverts.

### Flow

**Tier 1 — Read-only introspection:**

Agent code → `assembler.assemble(query_cues, assess_quality=True)` → populated `AssemblyResult.metadata["quality"]` → agent reads `quality.fok_score` and `quality.avg_confidence` → decides whether to trust the context or caveat the response

**Tier 2 — Outcome feedback extension:**

Agent reports outcome → `ObservationProtocol.on_context_used(records, {rk1: "used", rk2: "acted"})` → `_apply_used` discards staged access, auto-resolves prediction with `PL_AUTO_RESOLVE_USED` error value, does NOT touch confidence or cycle → existing effects for other outcomes unchanged → `PredictionLedgerMixin.error_summary(model_class, group_by="hour")` surfaces systematic bias

**Tier 3 — Adaptive strategy:**

Agent uses `AdaptiveAssembler(inner=ContextAssembler(...))` → each `adaptive.assemble(query_cues)` delegates to inner assembler and records `(quality, optional_outcome_callback)` → every 20 calls (configurable): adaptive proposes weight delta, measures quality window, keeps improvements, reverts regressions → over 100-call integration test, adaptive's avg quality beats fixed-weight baseline by a measurable margin (set target: >= 5% improvement in avg FOK * avg_confidence product)

### Technical Approach

**Tier 1: `RetrievalQuality` + `assess()` + `assemble(assess_quality=True)`**

- Add `RetrievalQuality` dataclass to `context_assembler.py`. Dataclass, not a Model — no Redis state. Carries the 4 required metrics + optional `score_distribution: list[float] = field(default_factory=list)` and `per_cue_fok: dict = field(default_factory=dict)`.
- Implement `ContextAssembler._compute_fok(query_cues, pull_candidates)` as a private helper. For each cue, derive the three components per the design-spec formula. Cue familiarity uses the verified ExistenceFilter API `might_exist(model_class, fingerprint)` (`src/popoto/fields/existence_filter.py:429`), coercing the cue value via `str(...)` to match the `_pull_path` pattern at `context_assembler.py:379`:
  ```
  familiar = 1.0 if (self._existence_filter and self._existence_filter.might_exist(self.model_class, str(cue_value))) else (0.5 if self._existence_filter is None else 0.0)
  ```
  When `ExistenceFilter` is not configured on the model, fall back to `cue_familiarity = 0.5` (neutral). When configured but `might_exist` returns False, `cue_familiarity = 0.0`. When no pull candidates surface, `partial_retrieval_count = 0` and `subthreshold_activation = 0`. Edge case: empty `query_cues` → `fok_score = 0.0` with a warning log.
- Implement `ContextAssembler._compute_quality(selected, all_pull_candidates, query_cues)` as a private helper invoked at the end of `assemble()` when `assess_quality=True`. Collects the 4 metrics. `score_spread` computation must guard against zero-mean: `score_spread = stddev / mean if abs(mean) >= 1e-9 else 0.0` (stddev/mean is undefined when mean is zero — empty scores or all-zero scores).
- Implement `ContextAssembler.assess(query_cues, partition_filters=None, probe_limit=None)`. Runs a low-cost probe: ExistenceFilter check + a composite_score call with `limit=probe_limit or max_items`. Does NOT run propagation, does NOT run push path, does NOT apply post-effects. Returns a `RetrievalQuality` built purely from the probe. Intended use: caller decides whether to run the full `assemble()` based on `fok_score`.
- Expose `RetrievalQuality` in `src/popoto/__init__.py` and the `recipes/__init__.py` so callers can type-hint against it.

**Tier 2: `PredictionLedgerMixin.error_summary` + `"used"` outcome**

- Add classmethod `PredictionLedgerMixin.error_summary(cls, model_class, partition="default", group_by=None, limit=100)`. Read the error sorted set (top `limit` by |error|) via `ZRANGE error_key 0 limit-1 WITHSCORES`. For each member, if `group_by` is callable, invoke it; if it's a string, bucket `resolved_at` read from each instance's per-instance meta hash via a **pipelined batch of per-instance `HGET` calls** (NOT a single `HMGET` — each PredictionLedger instance has its own meta hash keyed as `$PL:{ClassName}:meta:{pk}`). Invariant: `member_key == instance.db_key.redis_key == pk`, so `meta_key = f"$PL:{class_name}:meta:{member_key}"` and the read is `pipe.hget(meta_key, member_key)` for each member. After `pipe.execute()`, decode each msgpack blob, extract `resolved_at`, apply built-in bucketer. Compute stats: count, mean, stddev, percentiles via `statistics.quantiles` or a small helper. Return `dict` keyed by group (or `{"__all__": stats}` when no grouping). Bucketer contract (see built-in list below) must coerce `resolved_at` via `ts = float(data.get("resolved_at") or 0.0); if ts <= 0: continue` before any datetime conversion — `resolved_at` is stored as `str(time.time())` (see `prediction_ledger.py:300, :376`), not as a numeric type. Corrupt msgpack entries (unpack raises) must be logged at warning level and skipped, not propagated.
- Built-in bucketers for string `group_by` values. All built-in bucketers must perform the following coercion-and-guard on every row before bucketing (since `resolved_at` is a string per `prediction_ledger.py:300, :376`): `ts = float(data.get("resolved_at") or 0.0); if ts <= 0: continue`. Corrupt msgpack entries (unpack raises) must be logged at warning level and skipped, not allowed to crash the loop.
  - `"hour"`: bucket by `datetime.fromtimestamp(ts).hour` → 24 buckets
  - `"weekday"`: 7 buckets (`datetime.fromtimestamp(ts).weekday()`)
  - `"day"`: `YYYY-MM-DD` strings (`datetime.fromtimestamp(ts).date().isoformat()`)
  - Unknown string: `ValueError` with a list of known bucketers
- Add `"used"` to `VALID_OUTCOMES` in `observation.py`. Add `_apply_used(instance, pipeline)` function. The behavior is approximately `_apply_deferred + confirm_access + auto_resolve` — specifically:
  - **Confirm** the staged read via `instance.confirm_access(pipeline=pipeline)` when `hasattr(instance, "confirm_access") and callable(instance.confirm_access)` (matches `_apply_acted` at `src/popoto/fields/observation.py:219`). This commits the AccessTrackerMixin staged read as a confirmed read — the key behavioral distinction from `"deferred"`, which discards staged reads.
  - Auto-resolve predictions via `PredictionLedgerMixin.auto_resolve(instance, "used", pipeline=pipeline)`
  - Do NOT touch ConfidenceField, CyclicDecayField, or DecayingSortedField
  - Net effect: weaker than `"acted"` (no confidence corroboration, no cycle strengthening, no decay touch) but strictly stronger than `"deferred"` (which discards staged reads and does not auto-resolve). This gives an observable, testable distinction between the two outcomes.
- Extend `PredictionLedgerMixin._pl_auto_resolve_errors` to include `"used": Defaults.PL_AUTO_RESOLVE_USED`. Add `PL_AUTO_RESOLVE_USED = 0.3` to `Defaults` (moderate error — agent consumed but didn't act, so the prediction was neither confirmed nor contradicted).
- Add the `"used"` branch to `_apply_outcome` dispatch in `observation.py`.

**Tier 3: `AdaptiveAssembler` + integration test**

- New file `src/popoto/recipes/adaptive_assembler.py`. Class wraps a `ContextAssembler` instance. Owns:
  - `inner: ContextAssembler`
  - `window_size: int = Defaults.ADAPTIVE_QUALITY_WINDOW_SIZE` (default 20)
  - `quality_metric: callable = lambda q: q.fok_score * q.avg_confidence` (default; overridable)
  - `weight_perturbation: float = 0.05` (how much to shift per proposal)
  - `_current_window: list[float]` (rolling quality scores)
  - `_baseline_quality: float` (rolling-window mean of quality metric under current weights)
  - `_candidate_weights: dict | None` (currently-testing perturbation, or None)
  - `_candidate_window: list[float]` (quality under candidate weights)
  - `_original_weights: dict` (snapshot to revert to)
- `AdaptiveAssembler.assemble(query_cues, assess_quality=True)`: always computes quality; forwards to inner; appends quality metric to active window; after `window_size` samples, proposes or evaluates a candidate.
- Proposal strategy: symmetric random perturbation — pick two weight keys, shift `weight_perturbation` from one to the other. Clamp weights to [0, 1].
- Keep/revert logic: after `window_size` calls under candidate weights, compare `mean(_candidate_window)` vs `_baseline_quality`. If candidate improves by >= 0% (non-strict; noise-tolerant version documented in Rabbit Holes), keep: update `_original_weights` to candidate, clear windows, start a new proposal. Else revert: restore `_original_weights` on `inner`, clear windows, start a new proposal.
- Integration test: `tests/test_adaptive_assembler.py`. Builds a `Memory` model with `ConfidenceField`, `DecayingSortedField`, `ExistenceFilter`. Seeds 100 memories where "correct" retrievals (agent-intended) correlate with a weight distribution that differs from the initial. Runs 200 `assemble()` calls through `AdaptiveAssembler`, asserts final avg quality > initial avg quality. Deterministic: use `random.seed()` at test start, document the expected final weights within a tolerance band.

**Redis/Valkey compatibility:**
- `error_summary` uses only `ZRANGE`, `HMGET`. No modules.
- `AdaptiveAssembler` is pure Python — no Redis usage beyond what `inner` already does.
- FOK computation uses only existing `ExistenceFilter.might_exist()` (already Valkey-compat per #feedback memory).

## Failure Path Test Strategy

### Exception Handling Coverage

- [ ] `context_assembler.py` has existing `except Exception` blocks at lines 322 (token counter), 403 (CompositeScoreQuery), 431 (propagation), 459 (push path), 509 (post-effects pipeline). Each logs a warning. New code in `_compute_quality` and `_compute_fok` must follow the same pattern: catch exceptions per-record (e.g., `get_confidence` can raise on unsaved instances), log at warning level, contribute a sentinel value (e.g., `initial_confidence`) to the aggregate rather than aborting the whole metric.
- [ ] `prediction_ledger.error_summary` must handle empty error sets gracefully — return `{"count": 0}` instead of raising on `statistics.stdev([])`.
- [ ] `AdaptiveAssembler` must handle `quality_metric` exceptions — if `q.fok_score * q.avg_confidence` raises (e.g., None multiplication), log and skip that sample rather than crashing the loop.

### Empty/Invalid Input Handling

- [ ] `assess(query_cues={})` → return `RetrievalQuality(avg_confidence=0.0, score_spread=0.0, fok_score=0.0, staleness_ratio=0.0)` with a logged warning. Do not raise.
- [ ] `error_summary(group_by=None)` on a model with zero recorded predictions → return `{"__all__": {"count": 0, "mean": 0.0, ...}}`.
- [ ] `ObservationProtocol.on_context_used(instances=[], outcome_map={})` → already handled upstream (early return); add a test asserting no-op for `outcome_map={rk: "used"}` with empty instances.
- [ ] `AdaptiveAssembler.assemble()` with `query_cues=None` — forward to inner (which already handles this) and skip quality sample that round.

### Error State Rendering

- [ ] Not user-visible output. `RetrievalQuality` is a machine-readable dataclass; agents render it downstream. Assert dataclass `__repr__` produces a non-empty string for debug logs.

## Test Impact

- [ ] `tests/test_context_assembler.py` — UPDATE: add a new `class TestRetrievalQuality:` with ~8 tests covering `assess()`, `assemble(assess_quality=True)`, empty cues, missing ExistenceFilter fallback, FOK component breakdown. Existing tests pass unchanged (strict: `assemble()` with default args returns the same `AssemblyResult` modulo the new `metadata["quality"]` key which is absent by default).
- [ ] `tests/test_prediction_ledger.py` — UPDATE: add a new `class TestErrorSummary:` with ~6 tests: no-grouping, callable grouping, `"hour"` / `"weekday"` / `"day"` built-ins, empty error set, unknown built-in string raises ValueError.
- [ ] `tests/test_observation_protocol.py` — UPDATE: add `class TestUsedOutcome:` with ~4 tests: `"used"` in VALID_OUTCOMES, `on_context_used` with `"used"` → staged reads discarded, confidence unchanged, cycle unchanged, prediction auto-resolved with `PL_AUTO_RESOLVE_USED` error.
- [ ] **NEW** `tests/test_adaptive_assembler.py` — CREATE: unit tests for keep/revert logic (mocked inner assembler returning fixed quality values) + one integration test (full Redis, 100 memories, 200 calls, assert final > initial).

No existing test is deleted or modified in a breaking way. All existing tests remain valid.

## Rabbit Holes

- **Statistical significance testing on keep/revert decisions**. Tempting to add t-tests or bootstrap confidence intervals before accepting a weight change. **Skip in v1.** Autoresearch's lesson is that simple mean comparison over a rolling window converges quickly enough without formal significance; adding stats bogs down the loop and requires larger windows (slower adaptation). Document as a v2 option.
- **Per-agent quality metric customization**. Tempting to let each agent plug in its own quality scalarization (e.g., "I care about freshness, not confidence"). **Skip in v1.** Expose `quality_metric` as a constructor kwarg on `AdaptiveAssembler` only. Configuration-as-API can wait.
- **Persistent adaptation across process restarts**. Tempting to store `_original_weights` in Redis so that adaptation survives restarts. **Skip in v1.** Matches autoresearch's per-session model. Persistence introduces a second consistency problem (weight-vs-world) and demands a migration story. Flag as v2 in No-Gos.
- **FOK formula tuning (adjusting 0.4/0.4/0.2)**. Tempting to expose the weights as configurable. **Skip.** Design-spec formula is the canonical citation; diverging requires justification we don't have. Hard-code matching spec.
- **Bias detection as alerting**. Tempting to build thresholded alerts around `error_summary` ("if p99 error > 0.8 for a task type, emit an alert"). **Skip.** `error_summary` is a query primitive; alerting belongs in application code.
- **Source credibility weighting of observation signals**. Acceptance criteria list "per-source credibility" as a Tier 2 goal. After looking at the current code, credibility would require a new per-source score index and a rewrite of the `_apply_*` functions to consult it. The scope is comparable to this entire plan. **Move to Tier 2 scope-cut:** instead of per-source weighting, add a bookkeeping field (`source_credibility: float | None` on the outcome dict) that is *stored* for later use but not yet applied. This satisfies the spirit without the cost. Document as "source credibility bookkeeping only in v1; application deferred to v2."

## Risks

### Risk 1: `RetrievalQuality` computations add latency to `assemble()`

**Impact:** Users who turn on `assess_quality=True` might see 10-50ms slowdown per call because FOK probes hit Redis multiple times (one `might_exist` per cue, one confidence read per selected record).

**Mitigation:** Off by default. Document in the docstring that `assess_quality=True` adds bounded overhead (one `MIGHT_EXIST` per query cue + `get_confidence` per `selected` — max_items reads, so ~10 reads). Use existing pipeline pattern where possible: batch the confidence reads through `POPOTO_REDIS_DB.pipeline()`. Add a perf test that asserts `assemble(..., assess_quality=True)` completes within 100ms for a 10-item selection on a warm cache.

### Risk 2: `AdaptiveAssembler` converges on degenerate weights

**Impact:** In adversarial or non-stationary environments, the keep/revert loop could converge on weights that optimize the quality metric but hurt downstream task performance (Goodhart's Law).

**Mitigation:** (a) Ship the baseline (non-adaptive) `ContextAssembler` as the recommended default; `AdaptiveAssembler` is opt-in. (b) Cap weight perturbations so no weight can drop below 0.05 or rise above 0.9 without explicit override. (c) Integration test asserts improvement in multiple metrics simultaneously, not just the one `quality_metric` optimizes. (d) Document the rabbit-hole explicitly: "quality_metric is a proxy for downstream utility, not a substitute for it. Monitor actual task outcomes."

### Risk 3: `"used"` outcome semantic overlap with `"deferred"`

**Impact:** Agents might interchangeably use `"deferred"` and `"used"`, producing noisy signals.

**Mitigation:** The two outcomes are observably different in effect — not just in docstring:
- `"deferred"` — discards staged reads (no `confirm_access` call), does not auto-resolve predictions. Net: agent set memory aside without commitment; no trace recorded.
- `"used"` — **confirms** staged reads via `instance.confirm_access(pipeline=pipeline)` (matching `_apply_acted` at `src/popoto/fields/observation.py:219`) AND auto-resolves predictions with `PL_AUTO_RESOLVE_USED`. Net: agent committed to having consumed the memory; the read is recorded as confirmed, but no confidence/cycle/decay signal is emitted.

Precise docstring distinguishing them: `"deferred"` = agent ignored the memory (no commitment to having read it); `"used"` = agent read and reasoned over the memory but did not act on it in the response. The `TestUsedOutcome` class must assert that `"used"` produces a confirmed-read trace while `"deferred"` does not. Reinforce in `docs/features/observation-protocol.md`.

### Risk 4: `error_summary` with large error sets becomes slow

**Impact:** `ZRANGE error_key 0 limit-1 WITHSCORES` returns up to `limit` members; meta reads then require one `HGET` per instance (each PredictionLedger instance owns its own `$PL:{ClassName}:meta:{pk}` hash — there is no single hash to `HMGET` in one round-trip). For `limit=100` this is 100 pipelined `HGET` commands plus one `ZRANGE`, still one network round-trip but O(limit) Redis CPU.

**Mitigation:** Cap with a `limit` parameter (default 100, meaning "sample the top-N most-erroneous"). Pipeline the per-instance `HGET` batch to amortize round-trip cost. Document that this is a sampling function, not an exhaustive scan. For exhaustive scans, users should iterate over meta hashes themselves. Add a warning log if `ZCARD error_key > 10_000` and `limit=-1`.

## Race Conditions

### Race 1: `AdaptiveAssembler` weight swap mid-assemble

**Location:** `src/popoto/recipes/adaptive_assembler.py` — the moment the keep/revert decision fires and mutates `inner.score_weights`

**Trigger:** Two concurrent `adaptive.assemble(...)` calls: call A reads `inner.score_weights` at the top of `_pull_path`; before A completes, the adaptive loop (triggered by a prior call's bookkeeping) mutates `inner.score_weights`. Call A's quality sample is then attributed to the new weights incorrectly.

**Data prerequisite:** The `_current_window`/`_candidate_window` lists must be populated only by calls that observe the corresponding weights.

**State prerequisite:** Weight mutation must not interleave with an in-flight `_pull_path`.

**Mitigation:** `AdaptiveAssembler` is **single-threaded by design**. The inner-swap pattern (`self.inner = new_inner` in lieu of in-place mutation) only protects the `inner` reference itself; the `_current_window` / `_candidate_window` / `_baseline_quality` bookkeeping is NOT atomic across concurrent calls, and we explicitly do not add locks (that would expand scope beyond this plan). Document that `AdaptiveAssembler` is designed for single-thread-per-instance usage; multi-threaded agents must hold their own `AdaptiveAssembler` per thread. No multi-threaded test is included — attempting to assert thread-safety would require adding synchronization primitives that the class does not promise. If true multi-threaded use becomes a requirement, a follow-up plan can add locks or move bookkeeping to an atomic data structure.

### Race 2: `error_summary` reading per-instance meta hashes while `auto_resolve` updates them

**Location:** `prediction_ledger.py error_summary()` reading per-instance `$PL:{ClassName}:meta:{pk}` hashes via a pipelined batch of `HGET` calls while another process runs `RESOLVE_PREDICTION_LUA` on the same hashes.

**Trigger:** Concurrent resolution + summary queries.

**Data prerequisite:** Summary sees a consistent view of resolved predictions. Inconsistency cost: a single summary call may miss the most recent resolution, or observe resolution state for instance A while missing it for instance B (since each `HGET` is independent, the batch is NOT a single snapshot).

**Mitigation:** Each individual `HGET` is atomic and each entry is a single msgpack blob, also atomic — so no partial-write corruption is possible. However, unlike a single `HMGET`, a pipelined batch of per-instance `HGET`s is NOT a cross-key snapshot: a resolution landing between two pipelined `HGET`s may appear in one and not another. Summary may under-count or see mixed resolution-state across instances that land between the initial `ZRANGE` and the pipelined `HGET` batch, but it cannot corrupt data. Document that `error_summary` is eventually-consistent and not a cross-instance snapshot; not a real-time gauge. No code change needed.

## No-Gos (Out of Scope)

- **Persistent adaptation across process restarts.** `AdaptiveAssembler`'s learned weights are per-process. v2.
- **Source credibility application to confidence/cycle signals.** v1 only *records* per-source credibility if passed; it does not modify the `_apply_acted`/`_apply_contradicted` signal strengths based on credibility.
- **Adjusting FOK formula weights (0.4/0.4/0.2).** Locked to design spec.
- **Cross-agent quality aggregation / leaderboards.** Out of scope. RetrievalQuality is per-assemble-call.
- **LLM self-reported confidence.** Explicitly rejected per research findings (GPT-4 dissociation). Trust the mechanical signal.
- **Significance testing on keep/revert.** v1 uses mean comparison.
- **Explanation attachment (the "optional explanation" from the issue's Tier 3 sketch).** Deferred. `RetrievalQuality` is quantitative; a future `explain()` method can join human-readable reasons.
- **New primitives.** This is a layer on top of existing primitives. Zero new Fields, zero new Models.
- **Modifications to `Model` or `Field` base classes.** Explicit constraint from the issue. Enforce: the PR diff MUST NOT touch `src/popoto/models/base.py` or `src/popoto/fields/field.py`.

## Update System

No update system changes required — this is a pure library change, no deploy scripts or services.

## Agent Integration

No agent integration required — popoto is a library; consumers import it. There is no bridge or MCP server to wire.

## Documentation

### Feature Documentation

- [ ] Create `docs/features/metacognitive-layer.md` describing `RetrievalQuality`, `assess()`, `error_summary`, `"used"` outcome, and `AdaptiveAssembler`. Include worked examples.
- [ ] Update `docs/features/context-assembler.md` to cross-reference the new `assess_quality` parameter.
- [ ] Update `docs/features/prediction-ledger.md` to document `error_summary` and the new `"used"` outcome.
- [ ] Update `docs/features/observation-protocol.md` to document the `"used"` outcome and the distinction from `"deferred"`.
- [ ] Update feature documentation index (create `docs/features/README.md` if missing, or update equivalent nav-level documentation).

### External Documentation Site

- [ ] `mkdocs.yml` — add nav entry for the new metacognitive layer feature page.
- [ ] `docs/api-reference.md` — add references to new public classes/methods.
- [ ] Verify `mkdocs serve` renders the new page and cross-links without warnings.

### Inline Documentation

- [ ] Docstrings on every new public symbol (`RetrievalQuality`, `ContextAssembler.assess`, `AdaptiveAssembler`, `error_summary`, new `"used"` branches) with worked examples.
- [ ] Comments on non-obvious FOK component logic (why 0.5 neutral fallback, why multiply product for `quality_metric`).

## Success Criteria

- [ ] `ContextAssembler.assess(query_cues)` returns a `RetrievalQuality` with populated `avg_confidence`, `score_spread`, `fok_score`, `staleness_ratio`.
- [ ] `ContextAssembler.assemble(query_cues, assess_quality=True)` attaches the same `RetrievalQuality` to `AssemblyResult.metadata["quality"]`.
- [ ] `RetrievalQuality.fok_score` uses `ExistenceFilter.might_exist()` for the cue_familiarity component.
- [ ] `PredictionLedgerMixin.error_summary(model_class, group_by=None)` returns overall stats; `group_by=callable` returns per-group stats; `group_by="hour"|"weekday"|"day"` returns time-bucketed stats.
- [ ] `ObservationProtocol.on_context_used(records, {rk: "used"})` discards staged reads, auto-resolves predictions, does NOT touch confidence or cycle.
- [ ] Integration test `test_adaptive_assembler.py::test_adaptive_improves_over_baseline` demonstrates `AdaptiveAssembler` achieves >= 5% improvement in `fok_score * avg_confidence` over a fixed-weight baseline across a 200-call scenario.
- [ ] No breaking changes to existing `ContextAssembler` API — existing `tests/test_context_assembler.py` passes without modification.
- [ ] All existing tests pass (`pytest`).
- [ ] `src/popoto/models/base.py` and `src/popoto/fields/field.py` are unchanged in the diff (constraint enforcement).
- [ ] Documentation updated (`/do-docs`).
- [ ] `mypy src/` clean; `black src/ tests/` clean.

## Team Orchestration

### Team Members

- **Builder (tier-1-retrieval-quality)**
  - Name: `tier1-builder`
  - Role: Implements `RetrievalQuality`, `assess()`, `_compute_quality`, and the `assess_quality=True` branch in `assemble()`
  - Agent Type: builder
  - Resume: true

- **Validator (tier-1)**
  - Name: `tier1-validator`
  - Role: Verifies Tier 1 success criteria; runs `tests/test_context_assembler.py` before and after changes to confirm no regression; runs the new Tier 1 quality tests
  - Agent Type: validator
  - Resume: true

- **Builder (tier-2-feedback-loop)**
  - Name: `tier2-builder`
  - Role: Implements `PredictionLedgerMixin.error_summary`, the `"used"` outcome in `ObservationProtocol`, and the `PL_AUTO_RESOLVE_USED` default
  - Agent Type: builder
  - Resume: true

- **Validator (tier-2)**
  - Name: `tier2-validator`
  - Role: Verifies Tier 2 criteria; runs `tests/test_prediction_ledger.py` and `tests/test_observation_protocol.py` before/after
  - Agent Type: validator
  - Resume: true

- **Builder (tier-3-adaptive)**
  - Name: `tier3-builder`
  - Role: Implements `AdaptiveAssembler` and the integration test demonstrating adaptive improvement
  - Agent Type: builder
  - Resume: true

- **Validator (tier-3)**
  - Name: `tier3-validator`
  - Role: Runs the integration test 5 times (determinism check with the documented seed) to confirm "reliable" improvement — defined as >=5% improvement in at least 4 of 5 runs, OR mean improvement >=5% with stddev <=2%
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: `docs-writer`
  - Role: Creates `docs/features/metacognitive-layer.md` and updates cross-referenced feature docs + mkdocs nav
  - Agent Type: documentarian
  - Resume: true

- **Final Reviewer**
  - Name: `final-reviewer`
  - Role: Cross-tier validation: all success criteria, constraint enforcement (no modification to Model/Field base classes), Redis/Valkey compatibility (no modules used)
  - Agent Type: code-reviewer
  - Resume: true

## Step by Step Tasks

### 1. Tier 1 build: RetrievalQuality + assess() + assess_quality

- **Task ID**: `build-tier1`
- **Depends On**: none
- **Validates**: `tests/test_context_assembler.py` (existing tests pass unchanged) + `tests/test_context_assembler.py::TestRetrievalQuality` (new)
- **Informed By**: Freshness check findings (ContextAssembler lives in `recipes/`)
- **Assigned To**: `tier1-builder`
- **Agent Type**: builder
- **Parallel**: false
- Add `RetrievalQuality` dataclass to `src/popoto/recipes/context_assembler.py`
- Implement `ContextAssembler._compute_fok(query_cues, pull_candidates) -> float`
- Implement `ContextAssembler._compute_quality(selected, all_pull_candidates, query_cues) -> RetrievalQuality`
- Implement `ContextAssembler.assess(query_cues, partition_filters=None, probe_limit=None) -> RetrievalQuality`
- Add `assess_quality: bool = False` parameter to `ContextAssembler.assemble()` only (NOT to `__init__`); attach quality to `AssemblyResult.metadata["quality"]` when True. Matches the existing per-call parameter pattern (`agent_id`, `partition_filters`).
- Export `RetrievalQuality` from `src/popoto/recipes/__init__.py` and `src/popoto/__init__.py`
- Write new tests under `class TestRetrievalQuality` in `tests/test_context_assembler.py`

### 2. Tier 1 validate

- **Task ID**: `validate-tier1`
- **Depends On**: `build-tier1`
- **Assigned To**: `tier1-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_context_assembler.py -v` — assert all tests pass
- Run `pytest tests/test_context_assembler.py::TestRetrievalQuality -v` — assert all new tests pass
- Manually verify `AssemblyResult.metadata["quality"]` is absent when `assess_quality=False` (default) and present when True
- Confirm FOK formula components use the spec formula (0.4/0.4/0.2)

### 3. Tier 2 build: error_summary + "used" outcome

- **Task ID**: `build-tier2`
- **Depends On**: `validate-tier1`
- **Validates**: `tests/test_prediction_ledger.py`, `tests/test_observation_protocol.py` + new `TestErrorSummary`, `TestUsedOutcome`
- **Assigned To**: `tier2-builder`
- **Agent Type**: builder
- **Parallel**: false
- Add `PredictionLedgerMixin.error_summary(model_class, partition, group_by, limit)` classmethod
  - Read error set via `ZRANGE error_key 0 limit-1 WITHSCORES`
  - Fetch per-instance meta via a **pipelined batch of `HGET` calls** (one per member; each instance has its own `$PL:{ClassName}:meta:{pk}` hash — there is NO single hash with all members, so `HMGET` is wrong). Use `meta_key = f"$PL:{class_name}:meta:{member_key}"` and `pipe.hget(meta_key, member_key)`.
  - Decode msgpack; corrupt entries log-and-skip (do not crash)
  - Coerce `resolved_at` via `float(...)` with `<= 0` guard before bucketing
- Add built-in bucketers (`"hour"`, `"weekday"`, `"day"`) and `ValueError` for unknown strings
- Add `"used"` to `VALID_OUTCOMES` in `observation.py`
- Implement `_apply_used(instance, pipeline)` and wire into `_apply_outcome` dispatch
- Add `PL_AUTO_RESOLVE_USED = 0.3` to `src/popoto/fields/constants.py` `Defaults`
- Extend `_pl_auto_resolve_errors` property to include `"used"`
- Write `TestErrorSummary` and `TestUsedOutcome` test classes

### 4. Tier 2 validate

- **Task ID**: `validate-tier2`
- **Depends On**: `build-tier2`
- **Assigned To**: `tier2-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_prediction_ledger.py tests/test_observation_protocol.py -v` — assert all pass
- Verify `"used"` is in `VALID_OUTCOMES` and distinguishable from `"deferred"` in the effect profile
- Verify `error_summary` returns correct stats for all built-in bucketers

### 5. Tier 3 build: AdaptiveAssembler + integration test

- **Task ID**: `build-tier3`
- **Depends On**: `validate-tier2`
- **Validates**: new `tests/test_adaptive_assembler.py`
- **Assigned To**: `tier3-builder`
- **Agent Type**: builder
- **Parallel**: false
- Create `src/popoto/recipes/adaptive_assembler.py` with `AdaptiveAssembler` class
- Implement rolling-window bookkeeping, proposal strategy, keep/revert logic
- Add `ADAPTIVE_QUALITY_WINDOW_SIZE = 20` to `Defaults`
- Export `AdaptiveAssembler` from `src/popoto/recipes/__init__.py` and `src/popoto/__init__.py`
- Write unit tests with mocked inner assembler (fixed quality returns)
- Write integration test: 100 memories, 200 `adaptive.assemble()` calls, assert final quality >= 1.05 × initial quality
- Use `random.seed(42)` at test top; document the expected final `score_weights` tolerance band

### 6. Tier 3 validate

- **Task ID**: `validate-tier3`
- **Depends On**: `build-tier3`
- **Assigned To**: `tier3-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_adaptive_assembler.py -v` — assert all pass
- Run the integration test 5 times in a row — "reliable" means: asserts >=5% improvement in at least 4 of 5 runs, OR mean improvement >=5% with stddev <=2%. If flaky by that definition, request a loosening of the threshold or tightening of the seed and re-run.

### 7. Documentation

- **Task ID**: `document-feature`
- **Depends On**: `validate-tier3`
- **Assigned To**: `docs-writer`
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/features/metacognitive-layer.md` (new) — ~800 words, worked examples for each of the three tiers
- Update `docs/features/context-assembler.md` — document `assess_quality` parameter + cross-reference to metacognitive-layer.md
- Update `docs/features/prediction-ledger.md` — document `error_summary`
- Update `docs/features/observation-protocol.md` — document `"used"` outcome + distinction from `"deferred"`
- Update feature documentation index — add entry to `docs/features/README.md` if it exists, or update the equivalent nav-level index (create `docs/features/README.md` if neither exists)
- Update `mkdocs.yml` — nav entry
- Update `docs/api-reference.md` — new public symbols
- Run `mkdocs serve` locally to verify build

### 8. Final validation

- **Task ID**: `validate-all`
- **Depends On**: `document-feature`, `validate-tier1`, `validate-tier2`, `validate-tier3`
- **Assigned To**: `final-reviewer`
- **Agent Type**: code-reviewer
- **Parallel**: false
- Run full `pytest` — assert all tests pass
- Run `mypy src/` — assert clean
- Run `black --check src/ tests/` — assert clean
- `git diff main -- src/popoto/models/base.py src/popoto/fields/field.py` — assert empty
- Grep for `BF\.|CMS\.|TOPK\.` in new code — assert zero matches (Valkey compat)
- Verify all 6 issue acceptance criteria are satisfied with evidence (test file:test_name for each)
- Write final report summarizing what shipped

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Full tests pass | `pytest tests/ -x -q` | exit code 0 |
| Tier 1 tests pass | `pytest tests/test_context_assembler.py -v` | exit code 0 |
| Tier 2 tests pass | `pytest tests/test_prediction_ledger.py tests/test_observation_protocol.py -v` | exit code 0 |
| Tier 3 integration test passes | `pytest tests/test_adaptive_assembler.py -v` | exit code 0 |
| Type check clean | `mypy src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| No modifications to Model/Field base classes | `git diff main -- src/popoto/models/base.py src/popoto/fields/field.py \| wc -l` | output = 0 |
| No Redis modules used | `grep -rn 'BF\.\|CMS\.\|TOPK\.' src/popoto/recipes/adaptive_assembler.py src/popoto/recipes/context_assembler.py src/popoto/fields/prediction_ledger.py src/popoto/fields/observation.py` | exit code 1 (no matches) |
| RetrievalQuality exported | `python -c "from popoto.recipes import RetrievalQuality, AdaptiveAssembler"` | exit code 0 |
| "used" in VALID_OUTCOMES | `python -c "from popoto.fields.observation import VALID_OUTCOMES; assert 'used' in VALID_OUTCOMES"` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. **`AdaptiveAssembler` quality metric default.** The plan uses `lambda q: q.fok_score * q.avg_confidence` as the default scalarization. Is product the right aggregator, or should it be `min()` (pessimistic: only as good as your worst dimension) or a weighted sum? Product punishes imbalance multiplicatively, which seems correct for "I want high *both* FOK *and* avg_confidence," but the choice is worth a sanity check.

2. **Tier 3 improvement threshold: 5% or something else?** The integration test asserts a >=5% improvement in `fok_score * avg_confidence`. This is set arbitrarily. Is 5% ambitious enough to be meaningful, or so tight that noise will flake the test? Alternative: use a `t`-test with alpha=0.05 instead of a fixed threshold. Plan chose fixed threshold for simplicity; happy to revise.

3. **Should `"used"` auto-resolve predictions with error 0.3, or should we emit a warning and leave predictions unresolved?** Auto-resolving loses information (the prediction was neither confirmed nor contradicted; 0.3 is an opinionated guess). Leaving unresolved means predictions hang around until explicit resolution. **Resolution: auto-resolve with 0.3 by default** (matches the pattern of other outcomes), but document clearly that 0.3 is a placeholder and callers who care about precise prediction accounting should use explicit `resolve_prediction()` instead. The `"used"` outcome is observably distinct from `"deferred"` because `_apply_used` calls `instance.confirm_access(...)` (committing the staged read) while `_apply_deferred` discards staged reads — the distinction is behavioral, not just a numeric difference in auto-resolve error.

4. **Source credibility — bookkeeping only or skip entirely?** The issue lists it as Tier 2 scope. The plan scope-cuts it to "record in the outcome dict but don't apply". Is even bookkeeping worth the cognitive overhead if no one's going to wire it up in v1? Alternative: drop source credibility entirely from v1, defer the whole concept to v2. Consequence: one acceptance-criterion-adjacent goal from the issue is deferred explicitly (the issue calls out source credibility in the "Revised" recon bucket; a deferral is defensible).

5. **Does `AdaptiveAssembler.inner` swap (constructing a new `ContextAssembler`) break if the outer application holds a reference to the old inner?** Python's attribute lookup is atomic, but an outer caller that captured `adaptive.inner` into a local variable will keep using the old one. **Resolution: single-threaded by design.** `AdaptiveAssembler` is meant to be called directly (not "peek at inner"), and only from a single thread per instance. The inner-swap protects the `inner` attribute from a half-mutated state, but the rolling-window bookkeeping (`_current_window`, `_candidate_window`, `_baseline_quality`) is not atomic across concurrent calls and we deliberately do not add locks to keep scope tight. Multi-threaded agents must hold one `AdaptiveAssembler` per thread.
