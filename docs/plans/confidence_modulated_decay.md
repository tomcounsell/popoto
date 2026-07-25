---
status: Planning
type: feature
appetite: Medium
owner: valorengels
created: 2026-07-25
tracking: https://github.com/tomcounsell/popoto/issues/491
last_comment_id:
---

# Confidence-Modulated Decay — outcome evidence changes how fast a memory is forgotten

## Problem

Popoto's agent-memory substrate already **learns** from use: `ObservationProtocol.on_context_used`
maps injection outcomes (`acted` / `dismissed` / `contradicted`) into a capped-evidence Bayesian
`ConfidenceField`. But nothing consumes that learning to change **how fast a memory is forgotten**.
A memory dismissed ten times decays at exactly the same rate as one acted on ten times.

This is not hypothetical. The reference deployment (Valor, `tomcounsell/ai`) now reports honest
production telemetry: **82.1% of all memory injections are dismissed** (`/memories/metrics.json`,
2026-07-24, 390 records, aggregate act rate 17.9%). The system is drowning in memories it has
already learned are useless, and its forgetting mechanism cannot hear that signal.

**Current behavior:**

- `DecayingSortedField.decay_rate` is a **field-level constant** shared by every record
  (`src/popoto/fields/decaying_sorted_field.py:143-145`).
- The only per-instance modulation is `base_score_field` (`:146`), which scales the curve's
  **magnitude** — `decayed = base_score * elapsed_days^(-decay_rate)` (Lua `:90`). A low-confidence
  memory can start lower, but it is never forgotten *sooner*: magnitude scaling preserves relative
  order for all time, so a demoted record never crosses a pruning threshold any faster.
- `Query.top_by_decay(decay_rate=...)` permits a **per-query** override
  (`src/popoto/models/query.py:353-359`) — global to that call, not per record.
- `_apply_acted` refreshes the decay clock via `Model.touch()` (`observation.py:242-244`,
  `base.py:1975-2031`), so *use* already slows effective decay by resetting elapsed time. There is
  **no symmetric mechanism** by which dismissal accelerates it.
- `MemoryLifecycle` (`src/popoto/recipes/memory_lifecycle.py`) already performs auto-forget via
  `tick()`, and already reads confidence for **promotion**
  (`LIFECYCLE_PROMOTION_CONFIDENCE_THRESHOLD`, `:229`) — but its forget criteria are
  **confidence-blind**: importance floor AND idle time only (`_default_should_forget`, `:236-250`).
  Confidence is applied asymmetrically: it can promote a memory to permanence, but never hasten its
  removal.

**Desired outcome:**

Per-record effective decay responds to accumulated outcome evidence — corroborated memories persist
longer, dismissed/contradicted memories are forgotten faster — with **no new API calls for
adopters** beyond the outcome reporting they already do, and **byte-identical behavior** for records
with no confidence evidence.

## Freshness Check

**Baseline commit:** `c7bd62c01e83f826ee00a463a82ab825d449aa77`
**Issue filed at:** 2026-07-22T04:32:48Z
**Disposition:** **Overlap** (see below) — core premise verified intact; scope widened by one
discovery.

**File:line references re-verified:**

- `src/popoto/fields/decaying_sorted_field.py:143-145` — decay_rate is a ctor constant — **still holds**.
- `src/popoto/fields/decaying_sorted_field.py:146` — `base_score_field` scales magnitude — **still holds**
  (Lua math confirmed at `:86-90`).
- `src/popoto/fields/observation.py:262-269` — `_apply_acted` corroborates ConfidenceField — **still holds**.
- `src/popoto/query.py:353` — per-query decay_rate override — **drifted**: the real path is
  `src/popoto/models/query.py:353-359`. The issue cited a non-existent path. Corrected throughout
  this plan.
- `src/popoto/fields/confidence_field.py` — `update_confidence` API — **still holds**.

**Cited sibling issues/PRs re-checked:**

- #417 (ConfidenceField Bayesian fork) — merged 2026-06-11, still the update primitive.
- #462 / PR #483 (confidence-modulated hop admission) — merged 2026-07-21; precedent confirmed:
  `_avg_confidence` gates graph traversal (`context_assembler.py:630`, `:1904-1930`).
- #476 (1.8.0 `\x00idxset` forward-incompat) — **still open**; relevant only if we write to the
  model hash (Option B). The chosen approach (Option A) writes nothing, sidestepping it entirely.
- tomcounsell/ai#2203 (outcome-loop hardening) — **closed 2026-07-23**, shipped via ai PR #2301.
  This is what makes the confidence signal trustworthy enough to act on; before it, an optimistic
  fallback judge stamped keyword overlap as "acted".

**Commits on main since issue was filed (touching referenced files):** none. `git log --since`
returns empty; the repo is unchanged at `c7bd62c`.

**Active plans in `docs/plans/` overlapping this area:**

- `memory_lifecycle.md` (status: Ready; issue #396 closed 2026-05-22) — **the overlap**. The
  recon behind issue #491 examined `fields/` but not `recipes/`, and missed that `MemoryLifecycle`
  already ships auto-forget. This does not invalidate the issue (its claims about
  `DecayingSortedField` all hold) but it reveals a **second surface** where confidence should
  modulate forgetting, and it is the surface that actually deletes records. Scope widened to cover
  both; see Solution.
- `decay_fields_stable_tie_break.md` (#448, shipped) — defines the determinism contract this plan
  must not break.
- `lifecycle_tick_bypass_access_tracking.md` (#413, shipped) — lifecycle internals; no conflict.

**Notes:** One stale docstring found: `decaying_sorted_field.py:136` says `decay_rate` "Default 0.5"
while `Defaults.DECAY_RATE = 0.1` (`constants.py:52`). Drive-by fix included.

## Prior Art

- **PR #417** — *capped-evidence Bayesian ConfidenceField* (merged 2026-06-11). Produces the
  per-record confidence this plan consumes. Its `CAPPED_BAYESIAN_UPDATE_LUA` already demonstrates
  reading + `cmsgpack.unpack`ing the confidence payload inside Lua — the exact block we reuse.
- **PR #483 / #462** — *confidence/decay-modulated hop admission* (merged 2026-07-21). Direct
  precedent that confidence may gate retrieval behavior; establishes the house pattern of a
  confidence-derived modulation constant. Applied to traversal; this plan applies the same idea to
  lifecycle.
- **#396 / `memory_lifecycle.md`** — *memory lifecycle: consolidation + decay + auto-forget*
  (closed 2026-05-22, shipped). Delivered `MemoryLifecycle.tick()` with promote + auto-forget.
  Confidence gates promotion but **not** forgetting — the asymmetry this plan closes.
- **#448 / `decay_fields_stable_tie_break.md`** — *unstable Lua `table.sort` tie-ordering* (shipped).
  Established the bit-exactness + key-ascending tie-break contract that constrains any change to the
  decay scripts.
- **#410** — *PolicyCache Q-values and DecayingSortedField timestamps alias the same ZSET score slot*.
  Cautionary prior art: the ZSET score slot is the decay clock and is not free real estate.
  Reinforces choosing a read-time approach over storing new per-record state.
- **PolicyCache `TD_UPDATE_LUA`** (`recipes/policy_cache.py:132-172`) — exact prior art for the
  *rejected* Option B (HGET → compute → HSET a scalar on the model hash without `save()`).

## Research

**Queries used:**

- memory decay / forgetting curve models incorporating strength or evidence (ACT-R, Ebbinghaus)
- half-life regression / spaced repetition decay parameterization
- agent-memory systems with confidence- or access-modulated decay (2026 literature)

**Key findings:**

- **Pavlik & Anderson (2005)** — the closest precedent: they relax ACT-R's constant decay `d` and
  make it a function of memory strength, `d_i = c·e^(m_i) + a`.
  [PDF](http://act-r.psy.cmu.edu/wordpress/wp-content/uploads/2012/12/409s15516709cog0000_14.pdf).
  Note their coupling runs the *opposite* sign (high strength ⇒ faster decay, which generates the
  spacing effect); we inherit the **functional form**, not the sign.
- **Duolingo Half-Life Regression** (Settles & Meeder, ACL 2016) — `h = 2^(θ·x)`: half-life is
  **log-linear in evidence features**, which is what makes it unconditionally positive without
  clamps. [PDF](https://research.duolingo.com/papers/settles.acl16.pdf).
- **Generative Agents** (Park et al., UIST 2023) — importance is an *additive* term over a fixed
  exponential recency decay; importance never modulates the rate.
  [Paper](https://dl.acm.org/doi/fullHtml/10.1145/3586183.3606763). Confirms that rate modulation is
  genuinely under-occupied territory.
- **FadeMem (2026)** — adaptive decay modulated by relevance/access frequency
  ([arXiv](https://arxiv.org/html/2601.18642)); **Oblivion (2026)** — decay-driven accessibility
  rather than deletion ([arXiv](https://arxiv.org/html/2604.00131)). Both validate the direction;
  neither uses a Bayesian outcome-evidence signal, which is our differentiator.

**How this informs the plan:** both principled precedents put the strength signal inside an
**exponential**, guaranteeing a positive rate by construction with no clamping. We adopt that family
(see spike-4), which is why the recommended formula is `r · 2^(s·(1−2c))` rather than an affine or
odds-ratio form.

## Spike Results

### spike-1: Can the decay Lua read a per-member ConfidenceField value?
- **Assumption**: "The read-time Lua can fetch each member's confidence with one extra HGET."
- **Method**: code-read
- **Finding**: **Yes, with one precondition.** The ZSET member string *is* the model instance's full
  `redis_key` (`sorted_field_mixin.py:524-533`), and ConfidenceField's `:data` hash is keyed by that
  same `redis_key` (`confidence_field.py:308`, `:513`) — a direct join, no translation. The payload
  is a msgpack dict `{confidence, evidence_count, corroborations, contradictions}` (`:352-358`), and
  `CAPPED_BAYESIAN_UPDATE_LUA:69-83` already contains a working Lua unpack block to copy verbatim.
  **Direct precedent exists**: `CyclicDecayField` already passes companion hashes as `KEYS[2]/KEYS[3]`
  and does `HGET <companion> member` per loop iteration (`cyclic_decay_field.py:49-51, 95, 113`).
  **Precondition/blocker**: the `:data` key embeds `partition_by` values (`confidence_field.py:225-229`),
  and Lua cannot derive them from the member string — the key must be computed Python-side and passed
  as `KEYS[2]`. If `ConfidenceField.partition_by` is **not** a subset of the query's filters, one decay
  ZSET can span multiple `:data` hashes and no single `KEYS[2]` covers them.
- **Confidence**: high
- **Impact on plan**: Selects Option A as feasible; adds the partition-subset precondition as an
  explicit guard (raise `QueryException`, mirroring `query.py:1276-1284`) and as a Risk.

### spike-2: What is the backward-compatibility and test surface?
- **Assumption**: "Changing the decay computation is contained and testable."
- **Method**: code-read
- **Finding**: The surface is **wide and heavily locked-down**. Four independent consumers evaluate
  the decay scripts: `query.py:410` (`top_by_decay`), `query.py:1216` (`_materialize_decay_scores`
  for composite score), `context_assembler.py:527` (metacognitive score proxy — with an explicit
  no-drift contract at `:412-414`), and `CyclicDecayField`, which **carries a forked copy of the
  decay math** (`cyclic_decay_field.py:56-149`) that must be edited in lockstep or
  `TestEquivalenceWithDecaySortedField::test_same_ranking` (`test_cyclic_decay_field.py:546`) fails.
  Hard constraints found: tied-score **bit-exactness** on Lua's `tostring()` output
  (`test_decaying_sorted_field.py:547`, `test_cyclic_decay_field.py:893`); **strict linearity in
  base_score at rel_tol=1e-6** (`test_context_assembler.py:1223-1226`); the `0.01`-day elapsed floor
  hard-coded in two tests; latency budgets 1K < 1.0s / 10K < 5.0s
  (`test_decaying_sorted_field.py:459,:497`). A new constant requires **four coordinated edits**
  (`constants.py`, `tests/benchmarks/overrides.py`, `tests/benchmarks/test_defaults_sync.py`,
  `docs/guides/tuning-magic-numbers.md`). **Correction to the issue**: RLT does **not** exercise
  decay — its corpus has no `DecayingSortedField` (`tests/benchmarks/rlt/corpus.py:96-100`), so the
  issue's "RLT micro-benchmark" acceptance criterion was wrong; the real latency guards are the
  in-test benchmarks. Two gotchas: `tests/test_lua_decay_scoring.py:29-67` holds a **stale private
  copy** of the Lua that will not detect production changes; `test_context_assembler.py:860` has a
  stale comment.
- **Confidence**: high
- **Impact on plan**: Drives the Test Impact section, the "must not break" verification rows, the
  dual-script (plain + cyclic) task split, and replaces the issue's bogus RLT criterion with the real
  in-test latency budgets.

### spike-3: Is Option B (persisted per-record decay multiplier) better?
- **Assumption**: "Writing a multiplier on outcome events may be simpler than read-time modulation."
- **Method**: code-read
- **Finding**: **Feasible but strictly worse.** Exact prior art exists (`TD_UPDATE_LUA`,
  `policy_cache.py:132-172`: HGET → compute → HSET, no `save()`), and it would need **zero** ranking-Lua
  changes if it reused `base_score_field`. But it carries four costs Option A does not: (1) **not
  pipeline-atomic** — a read-modify-write EVAL must run immediately, adding a second round trip to
  every `acted`/`dismissed`/`contradicted`; (2) **unbounded drift** — repeated multiplicative updates
  compound toward 0 or ∞ without clamps; (3) **`save()` clobber** — `save()` rewrites the whole
  encoded object (`base.py:1312`), so a stale in-memory instance silently reverts the multiplier;
  (4) **duplicated state** — the multiplier is derivable from confidence, so it is a second source of
  truth that can desync. Also notable: `ObservationProtocol` has **zero** prior art of writing to the
  model's own hash; every effect today lands in a companion structure.
- **Confidence**: high
- **Impact on plan**: **Option A selected.** Option B recorded as a rejected alternative with
  reasons, so it is not re-litigated at build time.

### spike-4: What is the right modulation formula?
- **Assumption**: "A simple multiplier on decay_rate will work."
- **Method**: web-research
- **Finding**: Recommended `effective_rate = decay_rate · 2^(s · (1 − 2·confidence))` — the
  Pavlik–Anderson / HLR exponential family. Neutral is **bit-exact** (`c=0.5` ⇒ exponent `0` ⇒
  `math.pow(2,0)` is exactly `1.0`); the multiplier is bounded in `[2^-s, 2^s]` with **no clamping
  required for correctness**; the base is the literal `2`, so no negative-base NaN risk in Lua 5.1;
  and `s` has a plain-language meaning (*doublings of decay rate at zero confidence*). Affine and
  odds-ratio alternatives were rejected: affine needs a floor guard or it inverts the curve
  (rate ≤ 0 ⇒ scores that *grow* with age); odds-ratio is unbounded at both ends and its behavior is
  dominated by an arbitrary clamp that becomes a hidden second knob.
  **Critical trap found — the sub-one-day sign flip.** `elapsed_days` is floored at **0.01**
  (`decaying_sorted_field.py:83`), and for `t < 1` the term `t^(-rate)` is a multiplier **> 1** that
  a *larger* rate amplifies *more*. At `t = 0.01`, rate 0.66 gives ×21.9 while rate 0.35 gives ×5.0.
  So for the first 24 hours the modulation would run **backwards**, boosting exactly the
  low-confidence junk it was meant to bury — and since agent memory is touched constantly, most of
  the working set lives in that region. This is not a corner case; it would have silently inverted
  the feature. Fix that preserves bit-exact neutrality:
  `decayed = base · t^(−r) · max(t, 1.0)^(−(eff − r))` — the first factor is today's formula
  unchanged, the second is exactly `1.0` both when `c = 0.5` and when the record is fresher than a
  day.
  Also: rate modulation causes **at most one rank inversion per pair** (log-log lines cross exactly
  once), which is well-behaved and semantically desirable ("evidence wins long-run, salience wins
  short-run") — but it means a record's *rank* can improve over time even as its score falls, so
  cached top-N snapshots drift in a way magnitude weighting never produces. Worth documenting.
- **Confidence**: high
- **Impact on plan**: Fixes the formula, the neutrality guarantee, the `max(t,1.0)` guard (without
  which the feature is worse than useless), and adds a docs note on rank inversion.

## Data Flow

1. **Entry point — outcome reporting.** Agent session ends; deployment calls
   `ObservationProtocol.on_context_used(instances, {redis_key: "acted"|"dismissed"|...})`.
2. **Evidence accumulation.** `_apply_acted` / `_apply_contradicted` call
   `ConfidenceField.update_confidence(...)`, which runs `CAPPED_BAYESIAN_UPDATE_LUA` and writes the
   updated `{confidence, evidence_count, ...}` payload into the `:data` companion hash keyed by the
   record's `redis_key`. *(unchanged by this plan)*
3. **Retrieval — key resolution (new).** `QueryBuilder.top_by_decay` /
   `_materialize_decay_scores` / `ContextAssembler._decayed_partition_scores` resolve the
   ConfidenceField's `:data` hash key Python-side via `get_data_hash_key_from_values(...)`, applying
   the partition-subset guard, and pass it as `KEYS[2]` plus the strength constant in `ARGV`.
4. **Scoring — read-time modulation (new).** Inside the decay Lua loop, per member:
   `HGET KEYS[2] member` → `cmsgpack.unpack` → `confidence` (default `INITIAL_CONFIDENCE` when
   absent/undecodable) → `eff = r · 2^(s·(1−2c))` → `decayed = base · t^(−r) · max(t,1.0)^(−(eff−r))`.
5. **Ranking.** Existing two-level comparator (score desc, then member key ascending) and top-N
   truncation run unchanged.
6. **Output A — injection.** `ContextAssembler.assemble()` surfaces fewer low-confidence memories,
   directly attacking the 82% dismissal rate.
7. **Output B — deletion (new).** `MemoryLifecycle.tick()` → `_default_should_forget` additionally
   tests `confidence < LIFECYCLE_FORGET_CONFIDENCE_CEILING`, so repeatedly-dismissed records become
   forget-eligible without waiting for importance to bottom out.

## Architectural Impact

- **New dependencies**: none. No new imports, services, or libraries. Pure Redis/Valkey Lua —
  no Redis modules (Valkey parity is absolute).
- **Interface changes**: additive only. One new opt-in field kwarg
  (`confidence_modulation_field=None`), one new `Defaults` constant, one new optional lifecycle
  constant. All existing signatures unchanged; `None` sentinel preserves today's behavior byte-for-byte.
- **Coupling**: increases coupling between `DecayingSortedField` and `ConfidenceField` — previously
  independent primitives. Mitigated by making it opt-in and by defaulting to neutral when confidence
  is absent or undecodable.
- **Data ownership**: unchanged. ConfidenceField remains the single source of truth for evidence;
  the decay path is a pure reader. **This is the main reason Option A beat Option B** — no duplicated
  derived state.
- **Reversibility**: high. Setting `confidence_modulation_field=None` (the default) fully disables it;
  no data migration, nothing written, nothing to roll back.

## Appetite

**Size:** Medium

**Team:** Solo dev, PM (maintainer decisions), code reviewer

**Interactions:**
- PM check-ins: 1-2 (opt-in-vs-auto-detect decision; strength constant default)
- Review rounds: 1-2 (Lua changes touch a heavily-locked-down determinism contract)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli ping` | Test suite requires a live server (DB 15 isolation) |
| Editable install current | `python -c "import popoto; print(popoto.__version__)"` | Avoids false `test_version` failures per CLAUDE.md |

## Solution

### Key Elements

- **Read-time confidence modulation (Option A)**: the decay scripts read each member's confidence
  from the existing ConfidenceField `:data` hash and adjust that member's effective decay rate.
  Nothing new is stored.
- **Bit-exact neutrality**: absent, undecodable, or exactly-neutral (`0.5`) confidence produces
  byte-identical scores to today. This is a hard requirement, not an aspiration — existing tests
  assert tied-score bit-exactness and base-score linearity at `rel_tol=1e-6`.
- **Sub-one-day guard**: the correction term applies only to `t ≥ 1 day`, preventing the sign flip
  that would otherwise boost fresh low-confidence junk.
- **Dual-script parity**: the same change lands in both `DECAY_SCORE_LUA` and the forked
  `CYCLIC_DECAY_LUA`, preserving the cyclic≡plain equivalence contract.
- **Confidence-aware forgetting**: `MemoryLifecycle._default_should_forget` gains a confidence
  ceiling, closing the promote/forget asymmetry so dismissed memories actually leave the corpus.
- **Opt-in kwarg**: `DecayingSortedField(confidence_modulation_field="confidence")`. Default `None`
  = today's behavior exactly.

### Flow

**Agent dismisses an injected memory** → `on_context_used(..., "dismissed")` → ConfidenceField
evidence drops → *(next retrieval)* → decay Lua reads lowered confidence → effective decay rate rises
→ **memory ranks lower and is not injected** → *(next lifecycle tick)* → confidence below ceiling +
idle → **memory is forgotten**.

### Technical Approach

- **Formula** (spike-4): `eff = decay_rate · 2^(s · (1 − 2·c))`, then
  `decayed = base · t^(−r) · max(t, 1.0)^(−(eff − r))`. Defensive clamp `c = max(0, min(1, c))` before
  the pow, since the value is read from a hash that could hold anything.
- **Lua wiring** (spike-1): add `KEYS[2]` = confidence `:data` hash key, `ARGV[5]` = strength `s`
  (or `""`/`0` to disable), `ARGV[6]` = default confidence. Copy the proven unpack block from
  `CAPPED_BAYESIAN_UPDATE_LUA:69-83`. Follow the `CyclicDecayField` KEYS-passing precedent.
- **Three call sites** must thread the new KEYS/ARGV: `query.py:410`, `query.py:1216`,
  `context_assembler.py:527`. They are contractually required to agree
  (`test_context_assembler.py:1236` asserts proxy == `top_by_decay`).
- **Partition guard** (spike-1): resolve the `:data` key via
  `ConfidenceField.get_data_hash_key_from_values(...)`. If `ConfidenceField.partition_by` is not
  satisfiable from the query's filters, raise `QueryException` with an actionable message, mirroring
  `query.py:1276-1284`. Do **not** silently disable modulation — silent degradation is how a
  benchmark lies.
- **Constant**: `Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH` under the
  `# -- DecayingSortedField` block, with the house sweep-evidence comment. Recommended initial value
  `0.5` (spike-4 advises 0.3–0.7 pending real-data validation), plus the four coordinated
  registrations (`constants.py`, `overrides.py`, `test_defaults_sync.py`, `tuning-magic-numbers.md`).
- **Lifecycle**: add `LIFECYCLE_FORGET_CONFIDENCE_CEILING` and extend `_default_should_forget` to
  `(importance < floor OR confidence < ceiling) AND idle > idle_seconds`. Semantic memories stay
  protected. Note `_get_importance_score` reads the **raw ZSET timestamp** as an acknowledged proxy
  (`memory_lifecycle.py:163-170`) — confidence is the sharper signal precisely because it is exact.
- **Drive-by fixes**: correct the stale `decay_rate` docstring (`decaying_sorted_field.py:136`);
  make `tests/test_lua_decay_scoring.py` import the production script instead of its stale private
  copy, or delete the duplicate.

**Rejected alternative — Option B (persisted per-record multiplier)**: feasible with exact prior art
(`TD_UPDATE_LUA`), but adds a write round trip per outcome, needs drift clamps, is silently reverted
by `save()`, and duplicates state derivable from confidence. Recorded here so it is not re-litigated.

## Failure Path Test Strategy

### Exception Handling Coverage
- `context_assembler.py:535` wraps the decay EVAL in a broad `except Exception` — assert observable
  behavior (log/fallback), not silent zeros, when `KEYS[2]` is missing or the payload is corrupt.
- Lua-side `pcall(cmsgpack.unpack, ...)` failure must fall back to the neutral default, and a test
  must plant a deliberately corrupt confidence payload to prove it.
- `ConfidenceField.update_confidence` failures in `observation.py:268-269` are already swallowed for
  unsaved instances — unchanged by this plan; no new swallows introduced.

### Empty/Invalid Input Handling
- Member absent from the `:data` hash (`HGET` → nil) ⇒ neutral default ⇒ byte-identical score. Test.
- Confidence out of range (negative, > 1, NaN-ish, string) ⇒ clamped to `[0,1]`. Test.
- Empty ZSET, `n=0`, and missing `:data` hash entirely ⇒ existing behavior preserved. Test.
- `s = 0` ⇒ multiplier exactly `1.0` ⇒ byte-identical. Test (this is the kill switch).

### Error State Rendering
- Partition-mismatch must raise `QueryException` with a message naming the missing filter(s) — test
  the message, not just the type. This is a library, so "user-visible output" is the exception.

## Test Impact

- [ ] `tests/test_decaying_sorted_field.py::TestDecayTieOrdering` (all 4 tests) — UPDATE: add a
  variant where tied members carry *different* confidence, asserting they correctly **stop** tying,
  while the no-confidence case remains bit-exactly tied.
- [ ] `tests/test_decaying_sorted_field.py::TestDecayFormula` — UPDATE: add modulated known-value
  cases alongside the existing unmodulated ones (which must still pass unchanged).
- [ ] `tests/test_decaying_sorted_field.py::TestDecayBenchmarks` — UPDATE: keep 1K < 1.0s / 10K < 5.0s
  budgets with modulation **enabled** (the extra HGET per member is the risk).
- [ ] `tests/test_cyclic_decay_field.py::TestEquivalenceWithDecaySortedField::test_same_ranking` —
  UPDATE: must still pass with modulation on both scripts; add a modulated equivalence case.
- [ ] `tests/test_cyclic_decay_field.py::TestCyclicTieOrdering` — UPDATE: mirror the plain-field
  tie changes.
- [ ] `tests/test_context_assembler.py:1202-1235` (`test_distinct_base_scores_give_nonzero_spread`) —
  UPDATE: the `rel_tol=1e-6` linearity assertion must be re-verified under a neutral-confidence corpus
  (it should pass untouched; if it does not, neutrality is broken).
- [ ] `tests/test_context_assembler.py::test_proxy_matches_top_by_decay` — UPDATE: extend to a corpus
  *with* confidence data, proving all call sites agree after the change.
- [ ] `tests/test_lua_decay_scoring.py:29-67` — REPLACE: import the production `DECAY_SCORE_LUA`
  instead of the stale private copy (it silently fails to detect production changes today).
- [ ] `tests/test_memory_lifecycle.py` — UPDATE: add forget-eligibility cases driven by low
  confidence; assert semantic-tier protection still holds.
- [ ] `tests/benchmarks/test_defaults_sync.py:52-88` — UPDATE: register the new constant(s) or the
  sync test fails.

## Rabbit Holes

- **Rewriting `CyclicDecayField` to share the parent's Lua.** The fork is real and already divergent
  (the parent has sign-preserving decay, the cyclic copy does not). Deduplicating is a worthy but
  separate refactor; doing it here couples this feature to a risky cleanup under a determinism
  contract. Edit both copies, note the divergence, move on.
- **Tuning `s` against synthetic benchmarks.** The sweep corpus is not the production distribution;
  Valor's real dismissal data (and the SIQ corpus from #493) is the honest tuning ground. Ship a
  conservative default and tune later with real data.
- **Making confidence feed `base_score_field` as well.** Tempting and easy, but spike-4 warns the two
  channels compound multiplicatively and `s` stops being the only knob. Pick one channel: the rate.
- **Generalizing to "any field can modulate any other field."** A configurable modulation framework
  is a much larger design. This plan wires one specific, well-motivated coupling.
- **Fixing `_get_importance_score`'s raw-timestamp proxy** in MemoryLifecycle. Known imprecision,
  acknowledged in its own docstring, orthogonal to this change.

## Risks

### Risk 1: Partition mismatch between ConfidenceField and DecayingSortedField
**Impact:** A single decay ZSET spans multiple confidence `:data` hashes; no single `KEYS[2]` covers
all members, so some members silently read neutral confidence — modulation appears to work but is
partially inert.
**Mitigation:** Explicit guard — resolve the key Python-side and raise `QueryException` naming the
missing filters when `ConfidenceField.partition_by` is not satisfiable from the query filters. Fail
loudly; never silently degrade. Test the raise path directly.

### Risk 2: Breaking the determinism / bit-exactness contract
**Impact:** #448's tie-break guarantees regress; `len(set(scores)) == 1` assertions fail; cached and
fresh queries disagree; the metacognitive proxy drifts from `top_by_decay`.
**Mitigation:** Neutrality is bit-exact by construction (`math.pow(2, 0)` is exactly `1.0`, and the
`max(t,1.0)` term is exactly `1.0` at neutral). Keep the existing unmodulated tests unchanged as the
regression oracle; add modulated cases alongside rather than editing the originals.

### Risk 3: Latency regression from one extra HGET per member
**Impact:** The decay loop is O(N) over the whole ZSET; an extra HGET per member could threaten the
1K < 1.0s / 10K < 5.0s budgets and the injection path's latency.
**Mitigation:** The existing loop already does one HGET per member for `base_score_field`, so this is
a constant-factor change, not an order change. Benchmarks run with modulation **enabled**; if the 10K
budget is threatened, fall back to skipping the HGET entirely when `s = 0` or the modulation field is
unset (which is also the default path, so unmodulated users pay nothing).

### Risk 4: The sub-one-day sign flip (would invert the feature)
**Impact:** Without the `max(t, 1.0)` guard, low-confidence records that were just touched get
*boosted* above high-confidence ones — the opposite of the goal — for the majority of the working set.
**Mitigation:** The guard is in the formula from the start (spike-4). Add an explicit regression test:
a freshly-touched low-confidence record must **not** outrank a freshly-touched high-confidence one.

### Risk 5: Behavior change for existing adopters with confidence data
**Impact:** Anyone already using both fields would see ranking shift on upgrade.
**Mitigation:** Opt-in kwarg, default `None`. No existing model changes behavior without an explicit
edit. (See Open Questions — auto-detect was considered and deliberately not chosen.)

## Race Conditions

### Race 1: Confidence updated concurrently with a decay read
**Location:** `confidence_field.py:465-475` (EVAL write) vs. the decay Lua's `HGET` (read).
**Trigger:** An outcome is reported for record X while another process ranks a set containing X.
**Data prerequisite:** None — the `:data` hash entry may or may not exist.
**State prerequisite:** None.
**Mitigation:** Benign by construction. Each Lua script is atomic in Redis, so the read sees either
the pre- or post-update payload — never a torn value. Ranking is advisory and recomputed per query;
a one-query-stale confidence has no correctness consequence. No locking needed.

### Race 2: `save()` clobbering modulation state
**Location:** N/A.
**Trigger:** N/A.
**Mitigation:** **Structurally impossible under Option A** — nothing is persisted for modulation.
This race is a property of the rejected Option B and is a primary reason it was rejected.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #493] Tuning the strength constant `s` against real-world data — requires the mined
  SIQ fixture corpus from #493; this plan ships a conservative literature-grounded default.
- [SEPARATE-SLUG #492] Any change to scoping/partitioning semantics. This plan *guards* the partition
  mismatch; it does not redesign partitioning.
- [SEPARATE-SLUG #476] Model-hash / index-pointer concerns. Option A writes nothing to the model hash,
  so #476 is untouched — it remains a prerequisite for central hosting, not for this work.
- [SEPARATE-SLUG #487] Competitor/comparator benchmark runs of the modulated decay path.
- Deduplicating the forked `CYCLIC_DECAY_LUA` — see Rabbit Holes; both copies are edited, the refactor
  is not attempted. *(No separate issue yet; if the build surfaces concrete pain, file one then.)*

## Update System

No update-system changes required — this is a library-internal change with no deployment,
propagation, or migration steps. Adopters opt in by adding one field kwarg.

## Agent Integration

No agent integration required — Popoto is a library, not an agent surface. The reference deployment
(Valor) adopts it by setting `confidence_modulation_field` on its `Memory.relevance` field; that
wiring is deployment-side work tracked separately, not part of this plan.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/decaying-sorted-field.md` — new kwarg, the formula, the neutrality
  guarantee, the `max(t,1.0)` guard rationale, and the rank-inversion note (a record's rank can
  improve over time even as its score falls — cached top-N snapshots drift).
- [ ] Update `docs/features/cyclic-decay-field.md` — same modulation applies.
- [ ] Update `docs/features/confidence-field.md` and `docs/features/observation-protocol.md` —
  document that confidence now feeds forgetting, closing the loop.
- [ ] Update `docs/features/agent-memory.md` and `docs/guides/subconscious-memory-recipe.md` — the
  "validates and prunes during regular use" story is now literally true.

### External Documentation Site
- [ ] Update `docs/fields.md:668-731` (DecayingSortedField) and `:732-766` (CyclicDecayField).
- [ ] Add the new constant row to `docs/guides/tuning-magic-numbers.md:51-55`.
- [ ] Update `CHANGELOG.md`.
- [ ] Verify `mkdocs build --strict` passes (`scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Lua comments explaining the `max(t,1.0)` guard — non-obvious and load-bearing; without the
  rationale a future reader will "simplify" it away and silently invert the feature.
- [ ] Docstring for the new kwarg; fix the stale `decay_rate` "Default 0.5" docstring.

## Success Criteria

- [ ] A record with low confidence (repeated `dismissed`/`contradicted`) has a measurably faster
  effective decay than an identical record with neutral confidence; high confidence is measurably
  slower.
- [ ] A **freshly-touched** low-confidence record does not outrank a freshly-touched high-confidence
  record (the sub-one-day sign-flip regression test).
- [ ] Records with no confidence data, and any model without the opt-in kwarg, produce
  **byte-identical** decayed scores to `c7bd62c` (existing tests pass unchanged).
- [ ] `s = 0` and `confidence_modulation_field=None` are exact no-ops.
- [ ] Both `DECAY_SCORE_LUA` and `CYCLIC_DECAY_LUA` are modulated; cyclic≡plain equivalence holds.
- [ ] All three EVAL call sites agree (`test_proxy_matches_top_by_decay` passes on a
  confidence-bearing corpus).
- [ ] Partition mismatch raises `QueryException` naming the missing filter(s).
- [ ] `MemoryLifecycle` forgets a low-confidence idle record that today's importance+idle criteria
  would retain; semantic tier remains protected.
- [ ] Latency budgets hold with modulation enabled (1K < 1.0s, 10K < 5.0s).
- [ ] No Redis modules used; runs identically on Redis and Valkey.
- [ ] Tests pass (`/do-test`); docs updated (`/do-docs`).

## Team Orchestration

### Team Members

- **Builder (lua-core)** — Name: `lua-builder` · Role: both decay Lua scripts + formula + guards ·
  Agent Type: builder · Domain: Redis/Popoto data · Resume: true
- **Builder (call-sites)** — Name: `wiring-builder` · Role: thread KEYS/ARGV through the three EVAL
  sites + partition guard · Agent Type: builder · Resume: true
- **Builder (lifecycle)** — Name: `lifecycle-builder` · Role: confidence-aware forget + constants
  registration · Agent Type: builder · Resume: true
- **Test engineer** — Name: `decay-tester` · Role: neutrality/bit-exactness, sign-flip regression,
  corrupt-payload, partition-raise, latency · Agent Type: test-engineer · Resume: true
- **Validator** — Name: `decay-validator` · Role: verify all success criteria, especially
  byte-identical neutrality · Agent Type: validator · Resume: true
- **Documentarian** — Name: `decay-docs` · Role: docs cascade · Agent Type: documentarian · Resume: true

## Step by Step Tasks

### 1. Modulated decay math in both Lua scripts
- **Task ID**: build-lua
- **Depends On**: none
- **Validates**: tests/test_decaying_sorted_field.py, tests/test_cyclic_decay_field.py
- **Informed By**: spike-1 (member key == redis_key; copy unpack block from
  `CAPPED_BAYESIAN_UPDATE_LUA:69-83`), spike-4 (formula + `max(t,1.0)` guard), spike-2 (bit-exactness
  contract; cyclic script is a fork that must be edited in lockstep)
- **Assigned To**: lua-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `KEYS[2]` (confidence `:data` hash), `ARGV[5]` (strength `s`), `ARGV[6]` (default confidence) to
  `DECAY_SCORE_LUA`; skip the HGET entirely when modulation is disabled.
- Implement `c = max(0, min(1, c))`; `eff = r * math.pow(2, s*(1-2c))`;
  `decayed = base * math.pow(t,-r) * math.pow(math.max(t,1.0), -(eff-r))`.
- Mirror the identical change into `CYCLIC_DECAY_LUA`, preserving its existing structure.
- Comment the `max(t,1.0)` guard with the sign-flip rationale.

### 2. Thread confidence key through all three EVAL call sites
- **Task ID**: build-wiring
- **Depends On**: build-lua
- **Validates**: tests/test_context_assembler.py, tests/test_composite_score_query.py
- **Informed By**: spike-1 (partition-subset precondition; `get_data_hash_key_from_values`), spike-2
  (three sites must agree; `test_proxy_matches_top_by_decay` is the contract)
- **Assigned To**: wiring-builder
- **Agent Type**: builder
- **Parallel**: false
- Add the `confidence_modulation_field=None` kwarg to `DecayingSortedField.__init__`.
- Resolve the `:data` key and pass KEYS/ARGV at `query.py:410`, `query.py:1216`,
  `context_assembler.py:527`.
- Implement the partition-subset guard raising `QueryException` (mirror `query.py:1276-1284`).

### 3. Constants registration (four coordinated edits)
- **Task ID**: build-constants
- **Depends On**: none
- **Validates**: tests/benchmarks/test_defaults_sync.py
- **Informed By**: spike-2 (constants.py → overrides.py → test_defaults_sync.py →
  tuning-magic-numbers.md, or the sync test fails)
- **Assigned To**: lifecycle-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `DECAY_CONFIDENCE_MODULATION_STRENGTH` (initial `0.5`) and
  `LIFECYCLE_FORGET_CONFIDENCE_CEILING` with house-format provenance comments.
- Register in `overrides.py` (`VALID_RANGES` included) and exempt/list in `test_defaults_sync.py`.

### 4. Confidence-aware forgetting in MemoryLifecycle
- **Task ID**: build-lifecycle
- **Depends On**: build-constants
- **Validates**: tests/test_memory_lifecycle.py
- **Informed By**: freshness check (forget is confidence-blind at `memory_lifecycle.py:236-250`
  while promotion uses confidence at `:229`)
- **Assigned To**: lifecycle-builder
- **Agent Type**: builder
- **Parallel**: false
- Extend `_default_should_forget`: `(importance < floor OR confidence < ceiling) AND idle > idle_seconds`.
- Preserve semantic-tier protection and the custom-`should_forget` override path.

### 5. Test suite
- **Task ID**: build-tests
- **Depends On**: build-lua, build-wiring, build-lifecycle
- **Validates**: full suite
- **Informed By**: spike-2 (must-not-break list; stale private Lua copy in test_lua_decay_scoring.py)
- **Assigned To**: decay-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Neutrality: byte-identical scores with no confidence / `s=0` / kwarg unset.
- Sign-flip regression: fresh low-confidence must not outrank fresh high-confidence.
- Differing-confidence members correctly stop tying; no-confidence members remain bit-exactly tied.
- Corrupt/out-of-range confidence payload falls back to neutral.
- Partition mismatch raises with an actionable message.
- Latency budgets with modulation enabled; fix `test_lua_decay_scoring.py` to import production Lua.

### 6. Documentation
- **Task ID**: document-feature
- **Depends On**: build-tests
- **Assigned To**: decay-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Cascade per the Documentation section; include the rank-inversion caveat and the stale-docstring fix.

### 7. Final validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: decay-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify every Success Criterion, with special attention to byte-identical neutrality vs `c7bd62c`.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Decay suites pass | `pytest tests/test_decaying_sorted_field.py tests/test_cyclic_decay_field.py -q` | exit code 0 |
| Assembler agreement | `pytest tests/test_context_assembler.py -q -k "proxy or decay"` | exit code 0 |
| Lifecycle forget | `pytest tests/test_memory_lifecycle.py -q` | exit code 0 |
| Defaults sync | `pytest tests/benchmarks/test_defaults_sync.py -q` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Types clean | `mypy src/` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| No Redis modules (Valkey parity) | `grep -rnE '\b(BF\.|CMS\.|TOPK\.|TDIGEST\.|FT\.|JSON\.)' src/popoto/ \| wc -l` | match count == 0 |
| Cyclic Lua also modulated (anti-criterion: no half-edit) | `grep -c "math.max(elapsed_days, 1.0)" src/popoto/fields/cyclic_decay_field.py` | output > 0 |
| Stale private Lua copy removed | `grep -c "^DECAY_SCORE_LUA = " tests/test_lua_decay_scoring.py` | match count == 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

1. **Opt-in kwarg vs auto-detect.** This plan proposes an explicit
   `confidence_modulation_field=None` kwarg (safest; zero surprise on upgrade). The alternative is
   auto-detecting a ConfidenceField the way `ContextAssembler` auto-detects field capabilities
   (`context_assembler.py:1080`) — more "subconscious by default," but it silently changes ranking
   for every existing adopter that has both fields. Given the substrate is in beta and breaking
   changes were accepted (2026-06-11 decision), auto-detect is defensible. **Recommendation: ship
   opt-in now, revisit auto-detect after Valor validates the effect on real data.**
2. **Initial strength constant.** Spike-4 recommends `s ∈ [0.3, 0.7]` pending real-data validation;
   this plan proposes `0.5` (⇒ ×1.41 faster at `c=0`, ×0.71 slower at `c=1`). Accept, or start more
   conservative at `0.3`?
3. **Lifecycle forget semantics.** Proposed `(importance < floor OR confidence < ceiling) AND idle >
   idle_seconds` — the `OR` means low confidence alone (with idleness) is sufficient to forget. The
   stricter `AND` would require both low importance *and* low confidence. Given 82% production
   dismissal, `OR` is the aggressive-but-correct reading; confirm that is the intent.
