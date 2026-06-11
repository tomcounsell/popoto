---
status: Planning
type: bug
appetite: Medium
owner: valorengels
created: 2026-06-11
tracking: https://github.com/tomcounsell/popoto/issues/407
last_comment_id: 4677562230
---

# ConfidenceField: Capped-Evidence Bayesian Update ("Forgetful Bayesian")

## Problem

ConfidenceField's update rule is documented as Bayesian ("precision grows with sqrt(n)", "early evidence dominates, established beliefs resist change", "concurrent crystallizations converge") but the shipped Lua rule

```lua
local new_confidence = confidence + (signal - confidence) / math.sqrt(evidence_count + 1)
```

is a Robbins–Monro-style decaying-step EMA. The June 2026 adversarial audit (issue #407) empirically falsified every advertised property: order-dependent (8 signals in 70 orderings span 0.205–0.795), zero prior weight (first observation fully overwrites `initial_confidence`), non-convergent (1/√n step violates the Robbins–Monro sum-of-squares condition), and recency-dominated unlearning ~10× faster than Bayesian (50-confirmation belief crosses 0.5 after 5 contradictions). A related float-boundary defect: a first `contradicted` outcome on a fresh belief stores `0.09999999999999998`, strictly below `AUTO_DISCHARGE_CONFIDENCE_THRESHOLD = 0.1`, so homeostatic pressure auto-discharges on the *first* contradiction while the caller-visible value reads exactly `0.1`.

**Current behavior:** The math and the docs disagree; the order-dependence silently breaks the idempotence/convergence story TrajectoryMemory's crystallization is documented to rely on; the discharge threshold fires on an accidental float artifact.

**Desired outcome:** The maintainer decision recorded at [issue #407 comment 4677562230](https://github.com/tomcounsell/popoto/issues/407#issuecomment-4677562230) is implemented exactly:

1. **Capped-evidence Bayesian update** — `gain = 1/(min(evidence_count, cap) + 1)` with a per-field evidence cap (default **20**) and genuine prior pseudo-count weight for `initial_confidence`. Order-invariant and convergent within the evidence window; deliberate, bounded exponential forgetting beyond it (a maxed-out 0.9 belief crosses 0.5 after ~15 contradictions, not 5).
2. **Epsilon discharge boundary** — values within float epsilon of the threshold are *not* below it.
3. **TrajectoryMemory idempotence via observed-at watermark** — crystallize idempotence rests on timestamp filtering, not on a (false) commutativity claim.
4. **Docs honesty pass** — every claim matches the capped rule exactly: commutative/order-invariant *up to the cap*; recency-weighted with bounded forgetting beyond it; no blanket "concurrent crystallizations converge" claim; no `gain` mode switch.

## Freshness Check

**Baseline commit:** `1bffe67` (current main HEAD)
**Issue filed at:** 2026-06-11T02:49:00Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/fields/confidence_field.py:75` — sqrt-gain Lua line — **still holds, exact**
- `src/popoto/fields/confidence_field.py:1-5,46,74` — "Bayesian"/"resist change" docstring + Lua comments — **still hold**
- `src/popoto/fields/observation.py:372` — `if conf < AUTO_DISCHARGE_CONFIDENCE_THRESHOLD:` — **still holds, exact**
- `src/popoto/fields/constants.py:69-71` — `AUTO_DISCHARGE_CONFIDENCE_THRESHOLD = 0.1` — **still holds**
- `src/popoto/recipes/trajectory_memory.py:600-609` — `_observe_episodes` per-episode `update_confidence` loop — **still holds** (lines 600-609 exact)
- `src/popoto/recipes/trajectory_memory.py:449-457` — partial timestamp idempotence filter (`> last_reinforced`, strict) — **still holds**
- `docs/guides/trajectory-memory-recipe.md:100` — "Bayesian confidence updates are commutative — concurrent crystallizations converge" — **still holds** (note: path is `docs/guides/`, not `docs/recipes/`)

**Cited sibling issues/PRs re-checked:**
- #209 (Add ConfidenceField) — closed; introduced the "Bayesian" framing being corrected here
- #352 (Metacognitive layer) — closed; decision comment confirms it treats confidence as an averaged noisy signal, tolerant of either rule
- #289 / #281 — closed; round-trip/None mechanics, orthogonal to update semantics

**Commits on main since issue was filed (touching referenced files):** none — only `1bffe67` (CI workflow chore) landed since filing; it touches no cited file.

**Active plans in `docs/plans/` overlapping this area:** `experimental_tuning_magic_numbers.md` (shipped — established the "tuning constants are not user config" stance that the decision comment explicitly carves an exception from for `evidence_cap`); `subconscious_memory_constant_tuning.md` (shipped). No in-flight plan touches ConfidenceField semantics.

**Notes:** The decision comment's downstream-consumer analysis was independently re-verified: `memory_lifecycle.py:115-128` and `query.py` composite-score materialization (`query.py:1240+`) read confidence as a point value — formula-agnostic. Only TrajectoryMemory leans on commutativity.

## Prior Art

- **#209**: Add ConfidenceField — introduced the primitive and the "Bayesian certainty tracking" framing whose claims this plan makes true (within the window) or retracts (beyond it).
- **#281 / #289**: ConfidenceField round-trip/None fixes — mechanics only; established the pattern of syncing the instance attribute after `update_confidence` (preserved unchanged).
- **#352**: Metacognitive layer — consumes confidence as a noisy averaged signal; decision comment confirms no convergence assumption.
- **#404 / PR dab99dd**: EmbeddingField cross-process cache invalidation — recent precedent for changing agent-memory substrate behavior under the beta posture (breaking OK, no migration ceremony).

## Research

Skipped — purely internal change: a Lua arithmetic rule, a float comparison, and recipe/docs text. No external libraries, APIs, or ecosystem patterns are involved; Beta-Bernoulli posterior math and Robbins–Monro conditions are settled theory already laid out in the issue and decision comment.

## Spike Results

All assumptions were resolved by direct code-read during planning (no parallel spikes dispatched):

### spike-1: Lua script can receive cap and prior weight without API change
- **Assumption**: "The Lua EVAL call site can pass extra ARGV without changing the public `update_confidence` signature"
- **Method**: code-read
- **Finding**: `update_confidence` already passes `ARGV[1..3]` (member, signal, initial_confidence) at `confidence_field.py:410-417`. Appending `ARGV[4]` (evidence_cap) is a self-contained change.
- **Confidence**: high
- **Impact on plan**: No public API signature change needed; `evidence_cap` is a field constructor kwarg only. The prior pseudo-count is NOT passed as ARGV — it is inlined in the Lua script as a local constant (critique: Simplifier — passing a fixed internal constant through the protocol invites exactly the configurability the decision closed off).

### spike-2: Watermark save is implementable without new field plumbing
- **Assumption**: "crystallize can set `last_reinforced` to the max observed episode timestamp instead of wall-clock now"
- **Method**: code-read
- **Finding**: `Model.save(skip_auto_now=True)` exists (`models/base.py:863,997,2294`) and is already used in production code (`models/migrations.py:168`). `DecayingSortedField` forces `auto_now=True` (`decaying_sorted_field.py:135`) but `format_value_pre_save(..., skip_auto_now=True)` suppresses it (`sorted_field_mixin.py:299-316`).
- **Confidence**: high
- **Impact on plan**: Task 3 sets the watermark explicitly and saves with `skip_auto_now=True`; no field-system changes.

### spike-3: Capped formula reproduces the maintainer's stated trajectory
- **Assumption**: "gain = 1/(min(n,20)+1) gives ~14–15 contradictions to cross 0.5 from a maxed-out 0.9 belief"
- **Method**: code-read (closed-form check)
- **Finding**: At the cap, each contradiction (signal 0.1) multiplies `(conf − 0.1)` by 20/21. Crossing 0.5 from 0.9 requires k > ln(0.8/0.4)/ln(21/20) ≈ 14.2 → **15 contradictions**. Matches the decision comment's "~14–15".
- **Confidence**: high
- **Impact on plan**: The Success Criteria pin this trajectory as a regression test.

### spike-4: Prior pseudo-count of 1 yields exact order-invariance within the window
- **Assumption**: "Folding the prior in as `n_eff = min(evidence_count + prior_weight, cap)` makes the update an exact running mean over {prior, s1, …, sn}"
- **Method**: code-read (algebraic check)
- **Finding**: With prior weight 1: update 1 gives `(c0 + s1)/2`; update 2 gives `(c0 + s1 + s2)/3` — symmetric in s1, s2. By induction the value after n ≤ cap updates is `(c0 + Σsi)/(n+1)`, order-invariant, and `initial_confidence` carries exactly one observation of weight. Gain floor beyond cap is `1/(cap+1)` as decided.
- **Confidence**: high
- **Impact on plan**: Formula fixed as `n_eff = math.min(evidence_count + prior_weight, cap)`, `gain = 1/(n_eff + 1)`, with `prior_weight = 1` as an internal constant (not user config — only the cap is exposed, per Q2 of the decision).

## Data Flow

1. **Entry point**: `ConfidenceField.update_confidence(instance, field_name, signal)` — called directly, by `ObservationProtocol` (`observation.py` acted=0.9 / contradicted=0.1), or by `TrajectoryMemory._observe_episodes` (per-episode outcome signal).
2. **Lua EVAL** (`BAYESIAN_UPDATE_LUA`): atomically HGET → unpack msgpack `{confidence, evidence_count, corroborations, contradictions}` → apply gain → clamp [0,1] → increment counters → HSET. **Change lands here:** gain becomes `1/(min(evidence_count + prior_weight, cap) + 1)` with cap and prior weight passed as new ARGV.
3. **Return path**: Lua returns `tostring(new_confidence)` (`%.14g` text) → Python `float()` → instance attribute synced. Unchanged.
4. **Auto-discharge consumer** (`observation.py:367-380`): `_apply_contradicted` reads back confidence and compares to `AUTO_DISCHARGE_CONFIDENCE_THRESHOLD`. **Change lands here:** comparison gains an epsilon guard so the float artifact at exactly-0.1 no longer fires discharge.
5. **TrajectoryMemory consumer** (`trajectory_memory.py:430-472`): crystallize groups episodes → filters by `episode.recorded_at > pattern.last_reinforced` → `_observe_episodes` → `pattern.save()`. **Change lands here:** `last_reinforced` is advanced to the max *observed episode* timestamp (watermark) via `save(skip_auto_now=True)` instead of wall-clock save time, closing the query-to-save evidence-loss window.
6. **Formula-agnostic consumers** (verified, no changes): `PolicyCache` (Wilson CI on raw counts), `PredictionLedger` (fixed nudge past error threshold), `ContextAssembler`/`AdaptiveAssembler` (averaged signal), `memory_lifecycle` (point-value threshold), `composite_score` (point-value materialization).

## Architectural Impact

- **New dependencies**: none. Pure Lua arithmetic + Python float comparison — Redis/Valkey parity preserved (no modules, per `feedback_valkey_compatibility`).
- **Interface changes**: one new optional constructor kwarg `ConfidenceField(evidence_cap=...)`, default `Defaults.CONFIDENCE_EVIDENCE_CAP = 20`. No method-signature changes; ORM field API stays stable as required.
- **Coupling**: decreases — TrajectoryMemory's idempotence no longer depends on a property of ConfidenceField's update rule.
- **Data ownership**: unchanged; companion-hash schema `{confidence, evidence_count, corroborations, contradictions}` is byte-compatible (same keys, same msgpack shape). Existing stored data continues to load; only future update trajectories change. Beta posture: no migration ceremony.
- **Reversibility**: trivially reversible (revert the Lua constant change); stored data needs no rollback.

## Appetite

**Size:** Medium

**Team:** Solo dev, PM (critique + review via SDLC pipeline)

**Interactions:**
- PM check-ins: 0 — the maintainer decision comment already answered all open questions (Q1–Q4)
- Review rounds: 2 (plan critique, PR review)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli ping` | Test suite + Lua EVAL behavior |
| Editable install current | `python -c "import popoto; print(popoto.__version__)"` | Avoid false `test_version` failures (see CLAUDE.md ci-local notes) |

## Solution

### Key Elements

- **Capped Bayesian Lua rule**: replaces the sqrt-gain line; order-invariant running mean over `{prior, signals…}` while effective evidence ≤ cap, fixed-gain exponential forgetting (window cap+1) beyond it.
- **`evidence_cap` field config**: per-field epistemics knob (memory-window length), default 20, validated `int >= 1`. The *only* new exposed surface (decision Q2: no `gain` mode switch).
- **Prior pseudo-count**: internal constant, weight 1 — `initial_confidence` behaves as one real observation. Not user config (consistent with `feedback_magic_numbers`; the decision exposes only the cap).
- **Epsilon discharge guard**: `conf < THRESHOLD − epsilon` so the float artifact `0.0999…8` (== 0.1 at `%.14g` display precision) does not discharge; a genuine drop below does (decision Q3).
- **Crystallize watermark**: `last_reinforced` advances to the max observed episode timestamp, making sequential re-runs exactly idempotent and removing the dependence on commutativity.
- **Honest docs**: every Bayesian/commutativity/convergence claim re-scoped to "up to the cap"; recency-weighted bounded forgetting documented beyond it.

### Technical Approach

**1. Lua + field config (`src/popoto/fields/confidence_field.py`)**

```lua
-- ARGV[4] = evidence_cap (the only new ARGV)
local prior_weight = 1  -- internal constant; not user config (issue #407 decision Q2)
local cap = tonumber(ARGV[4])
local n_eff = math.min(evidence_count + prior_weight, cap)
local new_confidence = confidence + (signal - confidence) / (n_eff + 1)
```

- `evidence_count` semantics unchanged: counts *real* observations only (reporting via `get_confidence_data` is not inflated by the pseudo-count).
- Constructor: `evidence_cap = kwargs.pop("evidence_cap", None)` → default `Defaults.CONFIDENCE_EVIDENCE_CAP` (20); raise `ModelException` unless `int` and `>= 1`.
- New `Defaults` constants in `fields/constants.py`: `CONFIDENCE_EVIDENCE_CAP = 20` (deliberate user-facing config exception, per decision), `CONFIDENCE_EPSILON = 1e-9` (internal). The prior pseudo-count (1) lives only inside the Lua script — no `Defaults.CONFIDENCE_PRIOR_WEIGHT`, no Python-side knowledge of it (critique: Simplifier).
- EVAL call site appends `str(field.evidence_cap)` as ARGV[4] — nothing else.
- `update_confidence` docstring gains a **Warning** paragraph: all processes updating the same companion hash entry must configure identical `evidence_cap` values — the cap is passed per-call and not stored in the hash, so divergent caps across processes produce silently inconsistent gain schedules with no runtime detection (critique: Operator). Mirrored in the feature doc's `evidence_cap` parameter row.
- Rename the script constant to `CAPPED_BAYESIAN_UPDATE_LUA` and rewrite docstring/comments to the new semantics (module docstring lines 1–5, Lua header comment, `update_confidence` docstring).
- Clamp and `signal >= 0.5` corroboration/contradiction counting unchanged.

**2. Epsilon boundary (`src/popoto/fields/observation.py:372`)**

```python
if conf < AUTO_DISCHARGE_CONFIDENCE_THRESHOLD - CONFIDENCE_EPSILON:
```

Semantics per decision Q3: values within epsilon of the threshold are *not* below it. With the new rule a first contradiction on a fresh default belief lands at 0.3 anyway (prior now has weight), but the epsilon guard is the durable fix for any trajectory that lands on the representational boundary.

**3. TrajectoryMemory watermark (`src/popoto/recipes/trajectory_memory.py`)**

In `crystallize` (both new-pattern and existing-pattern paths):
- Compute `watermark = max(self._episode_score(e, self.episode_recency_field) for e in observed_episodes)`.
- After `_observe_episodes`, set `setattr(pattern, self.recency_field, watermark)` and call `pattern.save(skip_auto_now=True)` — **this applies to BOTH branches**. To avoid the new-pattern double-save (where `_create_pattern`'s internal `pattern.save()` at `trajectory_memory.py:597` writes an auto_now wall-clock score that the watermark save immediately overwrites), refactor `_create_pattern` to return the *unsaved* pattern; `crystallize`'s post-`_observe_episodes` `save(skip_auto_now=True)` becomes the single authoritative save for new and existing patterns alike (critique: Skeptic — an ambiguous branch split here would let the new-pattern path silently retain wall-clock semantics).
- Keep the strict `>` filter. Re-running crystallize after the fix observes exactly the episodes recorded after the last *observed* evidence, regardless of how much wall-clock time passed between query and save — closing the permanent-skip window where an episode recorded between query and save (score < save-time) was filtered out forever.
- Update the `crystallize` docstring (line ~400) and recipe doc to state the honest guarantee: **sequential re-run idempotence via watermark filtering; concurrent crystallization of the same partition is not coordinated** (recipe-level read-modify-write can double-observe) — run one crystallizer per partition. Within the evidence window the capped rule makes interleaved confidence updates order-invariant, which bounds (but does not eliminate) concurrent-crystallize drift; the docs must not claim convergence.
- Document two further watermark semantics explicitly (critique: Operator, User, Adversary):
  - `last_reinforced`'s stored score now equals the max *observed episode* `recorded_at`, which may predate the wall-clock crystallize call — an operator running `ZSCORE` reads "newest episode processed", not "when crystallize last ran", and recency-weighted recall (`DEFAULT_SCORE_WEIGHTS` puts 0.4 on `last_reinforced`) ranks patterns by episode time, so batched/nightly crystallization makes patterns appear correspondingly older than under the previous behavior.
  - The watermark guarantee is only as strong as cross-writer clock synchronization: a writer whose clock lags the max observed episode timestamp can record an episode *below* the watermark, which the strict `>` filter then skips forever. State the assumption (episode writers and crystallizer within ordinary NTP sync) in the recipe guide's idempotence section. A subtract-a-margin mitigation is deliberately **rejected**: episodes inside the margin would sit below an unmoving watermark and be re-observed on every run, breaking the zero-drift idempotence guarantee unless per-episode dedup is added — disproportionate for beta (see Race 4).

**4. Docs honesty pass** (grep-driven over `Bayesian|commutative|converge|sqrt|resist change`):
- `docs/features/confidence-field.md` — formula section, convergence table (rebuild for capped gain), header claim, auto-discharge section (epsilon + new first-contradiction trajectory), document `evidence_cap` with the window-length epistemics framing and worked forgetting example (0.9 → 0.5 in ~15 contradictions at cap 20).
- `docs/guides/trajectory-memory-recipe.md:100` — replace the commutativity sentence with the watermark idempotence + single-crystallizer-per-partition guidance; re-scope line 129/202 "Bayesian" labels to "capped Bayesian".
- Source docstrings/comments listed in item 1.
- Sweep remaining hits (`observation-protocol.md`, `agent-memory.md`, `agent-memory-quickstart.md`, `features/README.md`, `kitchen-edge-case-demo.md`, `policy-cache-recipe.md`, `prediction-ledger.md`, essay guides) — update only sentences describing the ConfidenceField update rule's properties; leave unrelated "Bayesian" mentions (e.g., PredictionLedger's own math) alone.

## Failure Path Test Strategy

### Exception Handling Coverage
- `observation.py:349-354,379-380` — existing `except (TypeError, ValueError[, AttributeError]): pass` graceful-degradation blocks are in scope of the touched lines but their behavior is unchanged; existing tests cover the unsaved-instance degradation path. No new swallow blocks are introduced.
- The Lua script's `pcall(cmsgpack.unpack, raw)` fallback (corrupt data → re-init from defaults) is unchanged; existing corrupt-data test coverage applies. Add one test asserting corrupt companion-hash data still recovers under the new ARGV count.

### Empty/Invalid Input Handling
- `evidence_cap` validation: `None` → default 20; non-int (str, float, bool), `0`, negative → `ModelException` at model-class definition time. Tests for each.
- `signal` validation unchanged (None → TypeError, out-of-range → ValueError) — existing tests retained.

### Error State Rendering
- Not user-visible UI; error propagation is exception-based and covered above.

## Test Impact

14 files call `update_confidence` (critique structural check; the 14th is `tests/benchmarks/scenarios/family_factory.py`, AUDIT-only); most use relative/ordering assertions and survive unchanged. Files with exact-value assertions tied to the old formula:

- [ ] `tests/test_confidence_field.py` (41 tests) — UPDATE: formula tests at lines ~175–200 ("first update = signal", "sqrt(2) denominator") and exact-value assertions at lines ~250–300 must be recomputed for the capped rule (first update from 0.5 with signal 0.9 → **0.7**, not 0.9). Auto-discharge test at line 402 manually seeds a sub-0.1 value — still valid; UPDATE to assert the epsilon boundary explicitly (exactly-0.1-representable value does NOT discharge; threshold − 2e-9 does).
- [ ] `tests/test_observation_protocol.py` (58 tests) — UPDATE: any assertion pinning post-acted/post-contradicted confidence values to the old trajectory; the "used does not update confidence" test (line 960) is value-free and survives.
- [ ] `tests/test_agent_memory_e2e.py::test_contradicted_with_low_confidence_auto_discharges_pressure` (line 663) — UPDATE with a stated closed-form oracle (critique: User): from `initial=0.5` with `signal=0.05`, value after n ≤ cap updates is `(0.5 + 0.05n)/(n+1)`; `< 0.1` requires `n > 8`, so **9 updates minimum**. Assert `final_conf < Defaults.AUTO_DISCHARGE_CONFIDENCE_THRESHOLD - Defaults.CONFIDENCE_EPSILON` (not a loose `< 0.15`), with the derivation as the oracle comment.
- [ ] `tests/test_trajectory_memory.py` (27 tests) — ADD watermark assertions (post-crystallize `last_reinforced == max observed episode recorded_at`), including a dedicated **new-pattern** test (first crystallize of a brand-new pattern sets the watermark, not wall-clock — critique: Archaeologist). NOTE: `test_recency_advances_on_reinforcement` (lines 225-245) passes unchanged under watermark semantics (second-batch episodes are recorded after a sleep, so the watermark naturally advances) — treat as a passive regression guard, not an update target (critique: Archaeologist).
- [ ] `tests/test_partitioned_confidence.py` (31 tests) — AUDIT: partition mechanics are formula-agnostic; recompute any exact-value assertions found.
- [ ] `tests/test_adaptive_assembler.py`, `test_adoption_ladder.py`, `test_composite_score_query.py`, `test_guide_examples.py`, `test_integration_v144.py`, `test_co_occurrence_field.py`, `test_context_assembler.py`, `test_event_stream_mixin.py`, `test_subconscious_memory_integration.py` — AUDIT: expected to pass (relative assertions); recompute any exact-value failures surfaced by the full run. `test_guide_examples.py` may pin doc-example outputs — UPDATE alongside the docs pass.

New tests (in `tests/test_confidence_field.py` unless noted):

**Float-tolerance convention for all order-invariance assertions** (critique: Skeptic, Adversary, Consistency — BLOCKER resolution): the incremental Lua form `c ← c + (s − c)/(n_eff + 1)` is *algebraically* order-invariant but NOT IEEE-754 bit-identical across orderings (intermediate roundings differ; error ≲ 1e-14 for cap ≤ 20). Every such assertion compares against the closed-form oracle `(c0 + Σsi)/(n+1)` with `abs(actual − oracle) < 1e-12` — a formula-derived bound, never `==`.

- Order-invariance property: a fixed evidence multiset (≤ cap signals) applied in several permutations lands within 1e-12 of the closed-form oracle (and hence of each other).
- Prior weight: one update with signal 0.9 from `initial_confidence=0.5` → 0.7; from 0.01 → 0.455; prior is not erased.
- Cap forgetting: belief **seeded directly** in the companion hash as `confidence=0.9, evidence_count=20` (driving 20 update-path corroborations from 0.5 at signal 0.9 reaches ≈0.8810, *not* 0.9 — the oracle comment must match the seeded state; critique: Adversary) crosses 0.5 on the **15th** consecutive 0.1-contradiction (spike-3 closed form `0.1 + 0.8·(20/21)^k`), not the 5th.
- Running-mean equivalence: n ≤ cap updates equal `(c0 + Σ signals)/(n+1)` within 1e-12.
- `evidence_cap` config: default 20; custom value honored (different forgetting rate); validation errors.
- Concurrency regression: re-run the existing atomicity pattern under the new script — concurrent updates within the window are order-invariant, so the final value is asserted against the closed-form oracle within 1e-12 (NOT exact equality, per the tolerance convention above).
- Epsilon boundary (in `test_confidence_field.py` + `test_observation_protocol.py`): seeded value at the float predecessor of 0.1 does not discharge; value clearly below threshold−epsilon does.
- Watermark idempotence (in `test_trajectory_memory.py`): double-crystallize produces zero confidence drift; an episode backdated between the watermark and wall-clock save time is still picked up by the next run (the exact scenario the old code lost forever).

No xfail/xpass tests relate to this bug (grep over `pytest.mark.xfail|pytest.xfail(` found no confidence/observation/trajectory hits).

## Rabbit Holes

- **Configurable prior pseudo-count** — the decision exposes only the cap. Weight 1 is an internal constant; do not add `prior_weight` kwargs.
- **`gain="recency"|"bayesian"` mode switch** — explicitly rejected (decision Q2). One honest behavior.
- **Migrating stored confidence values** — beta posture: trajectories change going forward, stored data is schema-compatible. No backfill, no version flags in the companion hash.
- **Coordinating concurrent crystallizers** — distributed locking per partition is a separate project. Document single-crystallizer-per-partition guidance; do not build locks.
- **Partitioned-confidence migration races** — audit finding CONC-6, explicitly dropped from #407 as a different mechanism; do not touch `migrate_to_partitioned`.
- **Renaming `BAYESIAN_UPDATE_LUA` everywhere it's mentioned in historical plan docs** — `docs/plans/` archives are historical records; only living docs (features/guides/source) get the honesty pass.
- **Tuning the cap empirically** — 20 is the maintainer's chosen default; no sweep in this plan (no-self-benchmarks stance).

## Risks

### Risk 1: Broad exact-value test fallout
**Impact:** Many tests across 14 files pin trajectories of the old formula; a sloppy update pass could "fix" tests to whatever the new code emits, masking an implementation bug.
**Mitigation:** Every recomputed expectation in tests must be derived from the closed form `(c0 + Σsi)/(n+1)` (window) or the `20/21` geometric decay (at cap) — stated in a comment next to the assertion — never copied from observed output. The spike-3/spike-4 closed forms are the oracle.

### Risk 2: `save(skip_auto_now=True)` suppresses auto_now for ALL fields on the pattern model
**Impact:** A consumer pattern model with additional auto_now fields (e.g., an `updated_at` DatetimeField) would silently stop updating those fields on crystallize saves.
**Mitigation:** Set the watermark value explicitly on the recency field before the save (so the suppression only "freezes" other auto-fields for crystallize-triggered saves); document this behavior in the recipe doc's pattern-model requirements; add a test with an extra auto_now field asserting the documented behavior.

### Risk 3: Float-equality watermark filtering misses same-timestamp episodes
**Impact:** Two episodes sharing an identical `time.time()` score where only one is observed → the other is excluded forever by the strict `>` filter.
**Mitigation:** Accept and document — scores are float seconds (microsecond resolution); collision requires same-partition, same-fingerprint episodes recorded in the same microsecond by the same process. Mention in the recipe doc's idempotence section. (Tracking observed episode IDs is the rabbit-hole alternative — disproportionate for beta.)

### Risk 4: Epsilon guard changes discharge behavior for existing near-threshold data
**Impact:** Stored confidences in `[0.1 − 1e-9, 0.1)` that *would* have discharged now don't.
**Mitigation:** That band is exactly the accidental float artifact the decision says must not discharge; behavior change is the point. Beta posture covers it; docs note the boundary semantics.

## Race Conditions

### Race 1: Crystallize query-to-save evidence loss (the bug being fixed)
**Location:** `src/popoto/recipes/trajectory_memory.py:449-468`
**Trigger:** Episode recorded by another writer after crystallize's episode query but before `pattern.save()`; its `recorded_at` < auto_now save time.
**Data prerequisite:** Episode must be visible to the *next* crystallize run.
**State prerequisite:** `last_reinforced` must not advance past unobserved evidence.
**Mitigation:** Watermark = max *observed* episode score; the late episode's score exceeds the watermark, so the strict `>` filter picks it up next run.

### Race 2: Concurrent crystallize of the same partition
**Location:** `trajectory_memory.py:430-472`
**Trigger:** Two processes crystallize the same partition simultaneously; both read the same episode group and both call `_observe_episodes` (recipe-level read-modify-write, not covered by Lua atomicity).
**Data prerequisite:** n/a — duplication, not loss.
**State prerequisite:** Single crystallizer per partition.
**Mitigation:** Documented operational constraint (recipe doc); within the evidence window the capped rule's order-invariance bounds the drift of interleaved single updates, but double-observation still double-counts — the docs say "one crystallizer per partition", and no convergence claim is made. Lock-based coordination is an explicit No-Go.

### Race 3: Concurrent `update_confidence` on one member
**Location:** `confidence_field.py` Lua EVAL
**Trigger:** Many writers updating the same member.
**Data prerequisite:** none.
**State prerequisite:** none.
**Mitigation:** Already atomic (audit: 2000/2000 bit-identical). The capped rule upgrades the guarantee within the window from "serialized" to "order-invariant" (within float tolerance, see Test Impact convention); regression-tested.

### Race 4: Cross-writer clock skew vs the watermark (critique: Adversary)
**Location:** `trajectory_memory.py` crystallize filter (`> last_reinforced`)
**Trigger:** Writer A's wall clock lags the crystallizer's view: B crystallizes observing episodes up to score 99 (watermark=99); A then records an episode stamped 95 by its lagging clock. The next run filters `> 99` and skips A's episode **forever** — qualitatively different from Race 3's same-microsecond collision; seconds of skew are common without tight NTP.
**Data prerequisite:** Multi-process episode writers with unsynchronized clocks.
**State prerequisite:** Watermark already advanced past the lagging writer's stamps.
**Mitigation:** Documented operational assumption (episode writers + crystallizer within ordinary NTP sync), stated in the recipe guide's idempotence section alongside single-crystallizer-per-partition. The subtract-a-margin alternative (`watermark − CLOCK_SKEW_MARGIN`) is **rejected**: episodes inside the margin sit below an unmoving watermark and would be re-observed on *every* subsequent run, breaking the zero-drift double-crystallize guarantee unless per-episode dedup is added — the rabbit-hole tradeoff documented in Technical Approach item 3. This is the same clock-trust class as audit finding CONC-4 (client wall clocks throughout decay math), which remains tracked separately.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #407] Partitioned-confidence migration races (audit finding CONC-6) — explicitly dropped from issue #407 itself as a different mechanism pending its own issue; this plan does not touch `migrate_to_partitioned`.
- Nothing else deferred — the cap config, epsilon fix, watermark idempotence, and docs pass are all in scope.

## Update System

No update system changes required — popoto is a library; the change ships as a normal version bump via the existing release flow. No new dependencies or config files.

## Agent Integration

No agent integration required — this is a library-internal change to popoto; no MCP server or bridge surface is involved.

## Documentation

### Feature Documentation
- [ ] Rewrite `docs/features/confidence-field.md` update-formula and convergence sections for the capped rule; document `evidence_cap` (epistemics knob: memory-window length) **including the same-cap-across-processes warning**, prior weight behavior, the ~15-contradiction forgetting example, and epsilon discharge semantics
- [ ] Update `docs/guides/trajectory-memory-recipe.md` line 100 idempotence paragraph (watermark semantics, single-crystallizer-per-partition, no commutativity claim, **cross-writer clock-sync assumption**) and the line 129/202 "Bayesian" labels
- [ ] State the `last_reinforced` semantic change in both the `crystallize` docstring and the recipe guide (critique: Operator, User): the stored score equals the max observed episode `recorded_at` — possibly earlier than the crystallize call — so `ZSCORE` reads "newest episode processed" not "when crystallize ran", and recency-weighted recall ranks batched-crystallized patterns by episode time
- [ ] Sweep `docs/features/observation-protocol.md`, `docs/features/agent-memory.md`, `docs/guides/agent-memory-quickstart.md`, `docs/features/README.md`, `docs/features/kitchen-edge-case-demo.md`, `docs/guides/policy-cache-recipe.md`, `docs/features/prediction-ledger.md` and essay guides for update-rule property claims (grep `Bayesian|commutative|converge|sqrt|resist change`); correct only sentences about ConfidenceField's update semantics

### External Documentation Site
- [ ] `mkdocs build --strict` passes (`scripts/ci-local.sh docs`)

### Inline Documentation
- [ ] `confidence_field.py` module docstring (lines 1–5), Lua header comment, `update_confidence` docstring rewritten for capped semantics
- [ ] `observation.py` auto-discharge comment (line 367) updated for epsilon semantics
- [ ] `trajectory_memory.py` `crystallize` docstring updated for watermark semantics

## Success Criteria

- [ ] Lua gain is `1/(min(evidence_count + 1, 20) + 1)`-style capped rule (prior weight 1 inlined in Lua, per-field cap default 20 as the only new ARGV); first update from default 0.5 with signal 0.9 yields **0.7**
- [ ] n ≤ cap updates match the running mean `(c0 + Σsi)/(n+1)` within 1e-12; permutation test shows order-invariance within 1e-12 (float rounding only — bit-identity is NOT claimed; critique BLOCKER resolution)
- [ ] Belief seeded at confidence=0.9 / evidence_count=20 crosses 0.5 on the 15th consecutive 0.1-contradiction (closed-form oracle)
- [ ] `ConfidenceField(evidence_cap=N)` honored and validated; companion-hash schema unchanged; ORM field API otherwise unchanged; docstring + feature doc carry the same-cap-across-processes warning
- [ ] Float predecessor of 0.1 no longer triggers auto-discharge; genuine sub-threshold drop still does
- [ ] Crystallize re-run produces zero confidence drift; a backdated late-arriving episode is observed on the next run; first crystallize of a brand-new pattern sets `last_reinforced` to the max observed episode timestamp (single authoritative save, no `_create_pattern` double-save)
- [ ] Docs explicitly state the `last_reinforced` watermark semantics (episode time, not crystallize time; recall-ranking impact under batched crystallization) and the cross-writer clock-sync assumption
- [ ] No doc, docstring, or comment claims unqualified Bayesian/commutative/convergent behavior; all claims scoped to the evidence window
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (confidence-core)**
  - Name: confidence-core-builder
  - Role: Lua rule, field config, constants, epsilon fix, and their unit tests
  - Agent Type: builder
  - Resume: true

- **Builder (trajectory-watermark)**
  - Name: trajectory-watermark-builder
  - Role: TrajectoryMemory watermark idempotence + tests
  - Agent Type: builder
  - Resume: true

- **Builder (test-sweep)**
  - Name: test-sweep-builder
  - Role: Audit/recompute exact-value assertions across the 13 affected test files using the closed-form oracle
  - Agent Type: test-engineer
  - Resume: true

- **Documentarian**
  - Name: confidence-docs
  - Role: Docs honesty pass across features/guides/source docstrings
  - Agent Type: documentarian
  - Resume: true

- **Validator (final)**
  - Name: confidence-validator
  - Role: Verify success criteria, run full suite + mkdocs strict build
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Capped Bayesian Lua rule + field config + epsilon fix
- **Task ID**: build-confidence-core
- **Depends On**: none
- **Validates**: tests/test_confidence_field.py, tests/test_observation_protocol.py
- **Informed By**: spike-1 (ARGV extension is self-contained), spike-3/spike-4 (closed-form oracles)
- **Assigned To**: confidence-core-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `Defaults.CONFIDENCE_EVIDENCE_CAP = 20` and `Defaults.CONFIDENCE_EPSILON = 1e-9` to `fields/constants.py` with comments distinguishing the cap (deliberate user config, decision #407) from the internal epsilon; the prior pseudo-count is inlined in the Lua script only — do NOT add a Python constant or ARGV for it
- Rewrite the Lua gain per Technical Approach item 1; rename script to `CAPPED_BAYESIAN_UPDATE_LUA`; pass `evidence_cap` as ARGV[4] (the only new ARGV)
- Add `evidence_cap` constructor kwarg with validation (`int >= 1`, bool rejected)
- Add the same-cap-across-processes Warning to the `update_confidence` docstring
- Apply the epsilon comparison at `observation.py:372` and update the adjacent comment
- Rewrite `confidence_field.py` docstrings/comments for capped semantics
- Add new unit tests listed in Test Impact (order-invariance, prior weight, cap forgetting from a directly-seeded 0.9/20 state, running-mean equivalence, cap config/validation, epsilon boundary, concurrency order-invariance, corrupt-data recovery under new ARGV) — all order-invariance assertions use the 1e-12 closed-form-oracle convention, never `==`

### 2. TrajectoryMemory watermark idempotence
- **Task ID**: build-trajectory-watermark
- **Depends On**: none
- **Validates**: tests/test_trajectory_memory.py
- **Informed By**: spike-2 (skip_auto_now exists and is production-proven)
- **Assigned To**: trajectory-watermark-builder
- **Agent Type**: builder
- **Parallel**: true
- In `crystallize`, compute the observed-episode watermark and persist it to the recency field via `save(skip_auto_now=True)` — the single authoritative save for BOTH branches: refactor `_create_pattern` to return the unsaved pattern (remove its internal `save()` at line ~597) so the new-pattern path cannot silently retain auto_now wall-clock semantics
- Update `crystallize` docstring (watermark guarantee, `last_reinforced` = max observed episode time not crystallize time, single-crystallizer-per-partition constraint, clock-sync assumption)
- Add tests: double-crystallize zero drift; backdated late episode observed on next run; extra-auto_now-field behavior (Risk 2); watermark equals max observed `recorded_at`; dedicated new-pattern test asserting first crystallize sets the watermark (not wall-clock)
- Do NOT modify `test_recency_advances_on_reinforcement` — it passes unchanged and serves as a passive regression guard

### 3. Exact-value test sweep
- **Task ID**: build-test-sweep
- **Depends On**: build-confidence-core, build-trajectory-watermark
- **Validates**: full suite (`pytest -x -q`)
- **Informed By**: spike-4 (closed-form oracle for all recomputed expectations)
- **Assigned To**: test-sweep-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Run the full suite; for every failure, recompute the expected value from the closed form (comment the derivation next to the assertion) — never from observed output
- Update `test_agent_memory_e2e.py` discharge test per the stated oracle (9+ updates at signal 0.05; assert `< threshold − epsilon`)
- Audit `test_partitioned_confidence.py`, `tests/benchmarks/scenarios/family_factory.py`, and the remaining caller files per Test Impact

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: build-test-sweep
- **Assigned To**: confidence-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Execute the Documentation section checklist (feature doc rewrite incl. cap warning, recipe doc incl. watermark/clock-sync/ranking notes, grep-driven sweep)
- `mkdocs build --strict` must pass

### 5. Final validation (single pass — critique: Simplifier merged the former tasks 4 and 6)
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: confidence-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify each Success Criterion against the diff and test output; confirm no `gain` mode switch, no `prior_weight` config, and no ARGV[5] leaked into the public API or protocol
- `scripts/ci-local.sh` (tests + stress + docs gates)
- Grep proof: no unqualified `commutative|concurrent crystallizations converge|resist change|sqrt(n)` claims remain in living docs/source
- Generate final report (pass/fail per criterion)

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Full suite | `pytest tests/ -x -q` | exit code 0 |
| Capped rule landed | `! grep -q "math.sqrt(evidence_count" src/popoto/fields/confidence_field.py` | exit code 0 (no matches; `grep -c` exits 1 on zero matches — critique: Consistency) |
| Cap constant | `grep -n "CONFIDENCE_EVIDENCE_CAP" src/popoto/fields/constants.py` | exit code 0 |
| Epsilon guard | `grep -n "CONFIDENCE_EPSILON" src/popoto/fields/observation.py` | exit code 0 |
| No stale commutativity claim | `grep -rn "concurrent crystallizations converge" docs/features docs/guides src/` | exit code 1 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |

## Critique Results

War room run 2026-06-11 (7 critics: Skeptic, Operator, Archaeologist, Adversary, Simplifier, User, Consistency + structural checks). All findings addressed in this revision; no finding challenged the core math direction or the watermark concept.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Consistency (+Skeptic, Adversary) | "Bit-identical across orderings" success criterion contradicts the ~1e-12 test spec; the incremental Lua form is algebraically order-invariant but not IEEE-754 bit-identical | Test Impact tolerance convention + Success Criteria rewrite | All order-invariance assertions: `abs(actual − closed_form_oracle) < 1e-12`, formula-derived bound, never `==` |
| CONCERN | Skeptic | Watermark save ambiguous between new-/existing-pattern branches; `_create_pattern`'s internal save makes a silent wall-clock retention bug likely | Technical Approach item 3, Task 2 | `_create_pattern` returns the unsaved pattern; `crystallize` performs the single authoritative `save(skip_auto_now=True)` for both branches |
| CONCERN | Archaeologist | New-pattern watermark behavior (auto_now overwritten by older episode timestamp) correct-by-design but untested | Task 2, Success Criteria | Dedicated new-pattern test: first crystallize sets `last_reinforced == max(episode.recorded_at)` |
| CONCERN | Adversary | Clock skew: a lagging writer's episodes can land below the watermark and be skipped forever | Race 4, Technical Approach item 3, Docs checklist | Documented NTP-sync assumption; margin mitigation explicitly rejected (would re-observe margin episodes every run, breaking zero-drift idempotence) |
| CONCERN | Operator | `last_reinforced` ZSCORE now reads episode time, not crystallize time — operators will misread it | Docs checklist, Technical Approach item 3 | Documentary notes in `crystallize` docstring + recipe guide + feature doc |
| CONCERN | User | The same semantic change shifts recency-weighted recall ranking under batched crystallization; not in docs checklist | Docs checklist + new Success Criterion | Ranking-impact sentence added to recipe guide and docstring requirements |
| CONCERN | Operator | Divergent `evidence_cap` across processes is undetectable (cap not stored in hash) | Technical Approach item 1, Task 1, Success Criteria | Warning paragraph in `update_confidence` docstring + feature doc parameter row |
| CONCERN | User | E2E discharge test could pass by coincidence without a stated oracle | Test Impact (e2e row), Task 3 | Closed form: `(0.5 + 0.05n)/(n+1) < 0.1 ⇒ n > 8`; assert `< threshold − epsilon` |
| CONCERN | Simplifier | ARGV[5]/`Defaults.CONFIDENCE_PRIOR_WEIGHT` smuggles a fixed constant through the protocol, inviting the config creep the decision closed off | Technical Approach item 1, spike-1, Task 1 | `local prior_weight = 1` inlined in Lua; ARGV[4] (cap) is the only new ARGV; no Python constant |
| CONCERN | Consistency | Verification table's `grep -c` check exits 1 on zero matches (reads as failure when correct) | Verification table | Replaced with `! grep -q …` (exit 0 iff no matches) |
| NIT | Adversary (+structural) | "Maxed-out 0.9" oracle premise unreachable via the update path (20 corroborations from 0.5 reach ≈0.8810) | Test Impact (cap-forgetting row) | Test seeds `confidence=0.9, evidence_count=20` directly in the companion hash; oracle comment matches the seeded state |
| NIT | Simplifier | Two serialized validator passes for four code changes | Tasks renumbered | Former tasks 4 and 6 merged into one post-docs `validate-all` |
| NIT | Archaeologist | `test_recency_advances_on_reinforcement` flagged for update but passes unchanged | Test Impact, Task 2 | Marked passive regression guard — do not modify |
| NIT | Structural | 14 (not 13) files call `update_confidence` | Test Impact | `tests/benchmarks/scenarios/family_factory.py` added to the AUDIT list |

---

## Open Questions

None — the maintainer decision comment (issue #407, comment 4677562230) resolved all open questions (Q1–Q4). Plan-level details it delegated ("exact pseudo-count is a plan detail", "name TBD by plan") are decided here: prior pseudo-count **1** (internal constant), kwarg name **`evidence_cap`**, epsilon **1e-9** (internal constant). If any of these readings misinterpret the decision, flag during critique.
