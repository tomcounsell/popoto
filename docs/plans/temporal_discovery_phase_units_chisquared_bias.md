---
status: Ready
type: bug
appetite: Medium
owner: valor
created: 2026-06-25
tracking: https://github.com/tomcounsell/popoto/issues/415
last_comment_id:
revision_applied: true
---

# Temporal discovery: phase units (radians vs seconds) and biased chi-squared null

## Problem

`temporal_discovery_handler()` in the PolicyCache recipe is meant to discover *when*
events cluster in time and emit `(period, amplitude, phase)` cycle tuples that
`CyclicDecayField` consumes to add a cyclical resonance term to a decay score. It is
broken in two independent ways, both verified against `main`.

**Current behavior:**

1. **Phase emitted in radians, consumed as seconds.** The producer computes
   `phase = (peak_bucket / num_buckets) * 2 * pi` — a value in `[0, 2π]` radians
   (`policy_cache.py:622`). The consumer's Lua treats phase as a *seconds* offset:
   `cyclic + amplitude * cos(2π(now - phase)/period)` (`cyclic_decay_field.py:105`).
   On a weekly period (604,800 s) a phase of at most 6.28 s shifts the cosine by ~1e-5
   of a cycle. Thursday-clustered events are correctly detected as weekly, but emit
   `(604800, 0.5, 1.7952)` — a phase that is 2.97e-6 of a cycle as consumed. Every
   discovered cycle effectively peaks at the epoch-aligned period boundary regardless
   of which day actually peaked. The peak-timing information discovery exists to extract
   is destroyed. The CyclicDecayField docs already correctly specify phase as seconds
   ([docs/features/cyclic-decay-field.md:32](../features/cyclic-decay-field.md)); the
   producer violates the documented contract.

2. **week_of_month chi-squared test uses unequal-width buckets against a uniform null.**
   The bucket function `min(time.gmtime(ts).tm_mday // 7, 3)` (`policy_cache.py:593`)
   produces buckets spanning ~6, 7, 7, and 10–11 days (days 1–6, 7–13, 14–20, 21–31),
   yet the expected count is uniform `n / num_buckets` (`policy_cache.py:614`). Under
   uniform-in-time events bucket probabilities are ~(0.197, 0.230, 0.230, 0.343), so
   the χ² statistic grows linearly with n and crosses the df=3 critical value (7.815)
   around n ≈ 100. Monte Carlo against the actual handler measures spurious monthly-cycle
   detection at 21% (n=50), 39% (n=100), 74% (n=200), 97% (n=400) — versus the nominal
   5% the p=0.05 threshold promises. `month_of_year` has the same defect in milder form
   (months span 28–31 days against a uniform 12-bucket null).

**Desired outcome:** Discovered cycles carry correct peak timing through to
CyclicDecayField scoring (events clustered at a known recurring time produce a resonance
peak at that time, asserted end-to-end through the Lua), and the discovery tests hold
their nominal false-positive rate (≈ alpha) on uniform data at any n.

**Scope decision (settled in this revision — see Resolved Decisions):** Only the
equal-width `day_of_week` config is retained. Both `week_of_month` and `month_of_year`
are **dropped unconditionally** — their fixed-period (`MONTHLY`=30d / `YEARLY`=365d)
constants are irreparably misaligned with real calendar month/year lengths, so (a) there
is no clean bucket→seconds-phase mapping for them and (b) no static per-bucket expected
vector can match the calendar-dependent data-generating process (Feb=28/29, month mix
shifts by year). Dropping them removes both critique blockers at the root and means the
χ² fix reduces to keeping the existing equal-width `chi_squared_uniform` exactly as-is —
no new `chi_squared(observed, expected_vector)` helper is added (it would have no live
consumer). The only code change is the phase-units fix for `day_of_week`.

## Freshness Check

**Baseline commit:** `3c5035dfafb612faa119111eb854cd240fa83a5b`
**Issue filed at:** 2026-06-11T05:20:40Z
**Disposition:** Minor drift

**File:line references re-verified:**
- `policy_cache.py:606` (radians phase) — claim holds; drifted to **line 622** (`phase = (peak_bucket / num_buckets) * 2 * math.pi`).
- `policy_cache.py:576` (bucket lambda `min(tm_mday // 7, 3)`) — claim holds; drifted to **line 593**.
- `policy_cache.py:592` (uniform `expected`) — claim holds; drifted to **line 614** (`expected = len(timestamps) / num_buckets`).
- `cyclic_decay_field.py:105` (seconds consumption) — claim holds; **still at line 105** (`cyclic = cyclic + amplitude * math.cos(two_pi * (now - phase) / period)`).
- `docs/features/cyclic-decay-field.md:32` ("Time offset in seconds") — confirmed, still at line 32.

**Cited sibling issues/PRs re-checked:** None cited in the issue body beyond the audit (#408–#416 family).

**Commits on main since issue was filed (touching referenced files):**
- `4fa3e09` fix(#410): separate PolicyCache Q-value from DecayingSortedField timestamp slot — **irrelevant to the bugs but caused the ~16-line downward drift** in `policy_cache.py` line numbers (added the `q_value` construction/storage lines above the handler). The handler logic and chi-squared helper are byte-identical to what the audit described.
- `c1bd02f` feat(confidence): capped-evidence Bayesian update — irrelevant (ConfidenceField).

**Active plans in `docs/plans/` overlapping this area:** `cyclic_decay_field.md` is the original CyclicDecayField *design* plan (the consumer), not a fix for this bug. No overlap — this plan touches the producer (`policy_cache.py`) and adds an end-to-end test; it does not change the (verified-exact) consumer math.

**Notes:** Both bugs reproduced fresh on 2026-06-11 (FP rates 0.21/0.39/0.74/0.97; Thursday phase=1.7952 rad). Line numbers updated above; otherwise premises unchanged. Bug still present on the baseline commit. The cited line numbers (622/593/614) carry ~3 lines of residual drift versus the current file (~625/595/611) — every citation also quotes the exact code string, so the build greps by string and lands correctly regardless of the precise line.

## Prior Art

- **Issue #232** (closed 2026-03-20): *Add PolicyCache — learned action selection from crystallized patterns* — the original feature that introduced `temporal_discovery_handler`. This is the source of both defects; no prior fix was attempted.
- No merged PRs found addressing temporal discovery phase or the chi-squared null. This is the first fix attempt — the **Why Previous Fixes Failed** section is omitted (greenfield bug, no prior failed fixes).
- No `xfail` markers exist in `tests/test_policy_cache.py` related to this bug.

## Research

No relevant external findings — proceeding with codebase context and training data. The
work is purely internal: a unit conversion in a producer function and a correction to a
chi-squared expected-counts computation. The statistics (Pearson χ² with non-uniform
expected counts `E_i = n·p_i`) and the cosine-phase algebra are standard and fully
specified by the issue.

## Data Flow

1. **Entry point:** `StreamConsumer` delivers a batch of `(entry_id, fields_dict)` tuples
   to `temporal_discovery_handler(entries)`. Each `fields["ts"]` is a unix-timestamp string.
2. **Bucketing:** timestamps are collected (≥3 required) and binned by three configs *today*:
   `day_of_week` (7 buckets, period WEEKLY), `week_of_month` (4 buckets, period MONTHLY),
   `month_of_year` (12 buckets, period YEARLY). **This plan removes the latter two**, leaving only
   the equal-width `day_of_week`.
3. **Significance test:** for each config, `chi_squared_uniform(buckets, expected)` is
   compared against `CHI_SQUARED_CRITICAL_VALUES[df]`. **Bug 2 lives here** — `expected`
   is uniform but the buckets are unequal-width for week_of_month / month_of_year. The fix is to
   **delete those two configs**; `day_of_week`'s equal-width buckets make the uniform null correct,
   so `chi_squared_uniform` stays unchanged.
4. **Phase computation:** if significant, `peak_bucket = buckets.index(max(buckets))` and
   `phase = (peak_bucket / num_buckets) * 2π`. **Bug 1 lives here** — radians, not seconds.
5. **Emission:** `(period, INITIAL_CYCLE_AMPLITUDE, phase)` appended to `discovered_cycles`,
   returned to the caller (application code installs it on a `CyclicDecayField`).
6. **Consumption (out of scope to change, in scope to test):** `CyclicDecayField`'s
   `CYCLIC_DECAY_LUA` reads the cycle tuple from a Redis hash and computes
   `amplitude * cos(2π(now - phase)/period)` at query time. This is the boundary the
   end-to-end test must cross.

## Architectural Impact

- **New dependencies:** none. Uses stdlib `time`/`math` already imported in `policy_cache.py`.
- **Interface changes:** the *semantic* contract of the emitted `phase` value changes from
  radians to seconds. The tuple *shape* `(period, amplitude, phase)` is unchanged. Substrate
  layer is beta, so this is acceptable (and is a correctness fix, not a new API).
- **Coupling:** unchanged. Producer and consumer were already coupled by the tuple contract;
  this fix makes the producer *honor* the existing documented contract rather than introducing
  new coupling.
- **Data ownership:** unchanged.
- **Reversibility:** trivial — localized to one function plus tests; revert is a single-file diff.

## Appetite

**Size:** Medium

**Team:** Solo dev, plan critique, code reviewer

**Interactions:**
- PM check-ins: 1 (the keep/drop disposition is resolved in-plan; see Resolved Decisions)
- Review rounds: 1

The coding is small (one function + a stats helper signature). The weight is in (a) getting
the phase algebra right relative to the epoch anchor and (b) a seeded Monte Carlo FP-rate
test that must be deterministic in CI. Appetite reflects the alignment decision and the
care the statistical test demands, not LOC.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli ping` | CyclicDecayField end-to-end Lua test runs against a live server |

Run all checks: `python scripts/check_prerequisites.py docs/plans/temporal_discovery_phase_units_chisquared_bias.md`

## Solution

### Key Elements

- **Phase in seconds, anchored to the period epoch.** Replace the radians computation with a
  seconds offset such that `cos(2π(now - phase)/period)` peaks when the cluster recurs. The
  offset is measured from the period's epoch anchor (the instant where `now mod period == 0`).
- **Drop the two unequal-width configs (`week_of_month`, `month_of_year`) unconditionally.**
  Both bucket on calendar boundaries whose widths (6/7/7/10–11 days; 28–31 days) cannot be made to
  match either their fixed-seconds period constant (so no clean seconds-phase mapping exists) or a
  static per-bucket expected vector (so the χ² null cannot be repaired reproducibly — the true
  per-month probability is calendar- and leap-year-dependent). Removing them eliminates the bias at
  its source rather than papering over it with an approximation gated by a noisy FP test.
- **Keep `chi_squared_uniform` exactly as-is; add NO new χ² helper.** With only the equal-width
  `day_of_week` config surviving, the existing uniform test is correct for it (all `p_i = 1/7`).
  There is no unequal-width consumer, so a `chi_squared(observed, expected_vector)` helper would be
  dead code — it is therefore NOT added. `chi_squared_uniform` is imported and tested in
  `tests/test_policy_cache.py:34,506,510,515` and stays untouched.
- **End-to-end peak-timing test** crossing the producer→consumer boundary through real Lua.

### Flow

Clustered events (e.g. every Thursday) → `temporal_discovery_handler` → `(WEEKLY, 0.5, phase_seconds)`
→ install on `CyclicDecayField.cycles` → query-time Lua score → **peaks within Thursday of the
weekly period, demonstrably NOT at the epoch-aligned boundary**.

### Technical Approach

**Bug 1 — phase in seconds (day_of_week / weekly, the only retained config):**

- The weekly period anchor: `now mod 604800 == 0` falls on **Thursday 1970-01-01 00:00 UTC**
  (epoch day 0 was a Thursday). `tm_wday` is Mon=0 … Sun=6, so Thursday is `tm_wday == 3`.
- Map the peak weekday to a seconds offset from the anchor, centered at the weekday's midpoint.
  **This is the resolved convention (formerly Open Q3, now decided): center at the bucket
  midpoint**, the most representative "when events cluster":
  `phase = (((peak_bucket - 3) % 7) * 86400 + 43200) % 604800`
  **Use the existing code variable `peak_bucket` directly** — for day_of_week the bucket index *is*
  the weekday (`bucket_fn` returns `tm_wday`), so `peak_bucket == tm_wday` of the peak day. Do NOT
  introduce a separate `peak_wday` name; add a comment noting the identity. `43200 = 0.5 * 86400`
  centers the cosine peak at mid-day of the peak weekday rather than at its 00:00 boundary. (The
  `- 3` rebases Monday-origin `tm_wday` onto the Thursday anchor; `% 7` and `% 604800` keep it in
  range.) Worked examples (verified):
  Thu(bucket=3)→43200s (0.5d), Sun(bucket=6)→302400s (3.5d), Mon(bucket=0)→388800s (4.5d).
- Derive/verify the Thursday anchor by asserting `time.gmtime(0).tm_wday == 3` in code rather than
  silently hard-coding the constant `3`, so the assumption is self-checking. day_of_week is the only
  config left, so no generalization to other periods is needed — the bucket→seconds mapping is the
  single weekly formula above.

**Bug 2 — correct null (resolved by dropping the biased configs, not by a new helper):**

- The bias lived entirely in `week_of_month` and `month_of_year`: unequal-width calendar buckets
  tested against a uniform expected count. **Both are dropped unconditionally** (see Key Elements
  and Resolved Decisions), so the only surviving config, `day_of_week`, has genuinely equal-width
  7-day buckets for which the uniform null is correct (`E_i = n/7` for all i — exactly what
  `chi_squared_uniform` already computes).
- **No new `chi_squared(observed, expected_vector)` helper is added.** With no unequal-width config
  remaining there is no consumer for a per-bucket-vector test, so adding it would land dead code.
  This eliminates the circular build dependency and the "unused helper" risk entirely.
- **Keep `chi_squared_uniform` exactly as-is — do NOT retire or alter it.** It is imported and
  exercised by `tests/test_policy_cache.py` (import at line 34; called at lines 506, 510, 515 in
  `test_chi_squared_uniform_basic` / `test_chi_squared_zero_expected`). day_of_week continues to
  use it unchanged. The Valkey/grep gate is re-scoped to cover `tests/` as well as `src/` so any
  accidental removal of the symbol surfaces (see Verification table).
- **Why not repair the calendar configs:** the true per-month probability is calendar- and
  leap-year-dependent (Feb=28/29; the month mix shifts with the timestamp span), so no static
  `p_i` vector matches the data-generating process, and `MONTHLY`/`YEARLY` are fixed seconds
  constants with no clean calendar phase mapping. Repair would require either calendar-exact
  variable periods (a separate feature; Lua can't do calendar math) or a per-window-derived
  expected vector that is unverifiable without pinning the exact FP-test span. Dropping is the
  correct, reproducible fix; a follow-up issue can scope calendar-accurate monthly/yearly cycles
  if ever wanted.

**Constants:** `INITIAL_CYCLE_AMPLITUDE`, `CHI_SQUARED_P_THRESHOLD`, and the critical-value table
are experimental-tuning values (per project convention), not user config. No new config surface.

## Failure Path Test Strategy

### Exception Handling Coverage
- The handler has one `try/except (ValueError, TypeError)` around `float(ts_str)` that silently
  skips malformed timestamps. Add a test asserting a malformed `ts` is skipped and well-formed
  entries still produce the expected result (observable: correct cycle emitted, no crash). This
  is intended-skip behavior, not a swallowed error, but it must be covered.

### Empty/Invalid Input Handling
- `entries = []` → `[]` (existing path; keep a regression test).
- `< 3` timestamps → `[]` (existing `test_temporal_discovery_insufficient_data`, keep).
- All-malformed `ts` strings → `[]` (timestamps list ends up `< 3`). Add a test.
- `expected < 1` short-circuit (very few events per bucket) → config skipped without error.

### Error State Rendering
- No user-visible rendering. The "output" is the returned tuple list; tests assert its contents
  directly (including the FP-rate path where the correct output is an *empty* list).

## Test Impact

- [ ] `tests/test_policy_cache.py::test_temporal_discovery` — UPDATE: keep the weekly-detection
  assertion, but add a phase assertion is NOT done here (it's a unit-level emission test); the
  new end-to-end test owns phase. If week_of_month/month_of_year are dropped, this test is
  unaffected (it asserts WEEKLY).
- [ ] `tests/test_policy_cache.py::test_temporal_discovery_uniform` — UPDATE/KEEP: still must
  produce no weekly cycle on uniform day-of-week data. Strengthen by widening n if needed.
- [ ] `tests/test_policy_cache.py::test_temporal_discovery_insufficient_data` — KEEP unchanged.
- [ ] `tests/test_policy_cache.py::test_chi_squared_uniform_basic` and `::test_chi_squared_zero_expected`
  — KEEP unchanged. These import and call `chi_squared_uniform` (line 34 import; lines 506/510/515).
  **`chi_squared_uniform` MUST be preserved** — it is a tested public symbol, not handler-private.
  No new χ² helper is added (the unequal-width configs that would have needed it are dropped), so
  there are no new helper unit tests.

No other existing tests are affected — the change is confined to `temporal_discovery_handler`
(phase units + removal of two configs) and new tests. PolicyCache TD-learning tests do not touch
this code path.

## Rabbit Holes

- **Building a calendar-accurate variable-period cycle model.** CyclicDecayField periods are
  fixed seconds constants by design (Redis Lua can't do calendar math). Do NOT try to make
  monthly/yearly cycles track real calendar month lengths — either accept the fixed-period
  approximation (gated by the FP-rate test) or drop those configs. Calendar-exact cycles are a
  separate feature, not this bug fix.
- **Adding new period granularities (hourly, daily, quarterly).** The critical-value table hints
  at hours-of-day (df=23) and quarters (df=2), but adding new discovery configs is scope creep.
  Fix the three existing configs only.
- **Tuning INITIAL_CYCLE_AMPLITUDE / thresholds.** These are experimental constants; leave them.
  The fix is about *units and the null*, not amplitude tuning.
- **Reworking the consumer Lua.** The audit verified `CYCLIC_DECAY_LUA` exact to ≤1e-14. Do not
  touch it; only test against it.

## Risks

### Risk 1: Phase anchor computed wrong (off-by-N-days)
**Impact:** The cosine peaks on the wrong day — a subtle, plausible-looking error that unit tests
checking only "phase is in seconds range" would miss.
**Mitigation:** The end-to-end test asserts the *actual peak location* by evaluating the real Lua
score across a ≤3600s-resolution sweep of `now` values over one full weekly period and checking the
argmax falls within the correct weekday window — and explicitly asserts the peak is NOT at the epoch
boundary, via a score-delta margin (`peak - boundary >= 0.9`). **The swept member uses `base_score = 0`
and no pressure** so the score is pure resonance — otherwise the `now`-dependent decay term dominates
and breaks the assertion (see Success Criteria for the simulation). **The cluster is on Sunday, not
Thursday**, precisely because the epoch boundary is itself a Thursday (`time.gmtime(0).tm_wday == 3`):
a Sunday cluster's correct phase (302400s) is 3.5 days from the epoch boundary, so the off-by-anchor
and radians failures both move the argmax far enough to be caught. Derive the Thursday epoch anchor
from `time.gmtime(0)` in the test rather than hard-coding, so the assumption is self-checking.

### Risk 2: Monte Carlo FP-rate test is flaky in CI
**Impact:** Intermittent failures erode trust and block merges.
**Mitigation:** Use a dedicated seeded generator (`rng = random.Random(<fixed seed>)`) for
determinism and to avoid disturbing global RNG state; use a generous tolerance (≤0.10 against
nominal 0.05) and **≥500 trials** so the binomial sampling noise (at n=400, true rate ≈0.05 the
standard error over 500 trials is ≈0.0097) is small enough that the 0.05-vs-0.10 gate is not noise-
dominated — 100 trials (SE ≈0.022) was too coarse to separate ≈0.05 from the 0.10 ceiling reliably.
The test is deterministic given the seed — record the observed rate in a comment.

### Risk 3: dropping configs silently weakens discovery
**Impact:** Removing `week_of_month` and `month_of_year` means monthly/yearly clustering is no
longer auto-discovered.
**Mitigation:** Those configs never worked — they fabricated cycles from noise (97–98% FP at
n=400) and emitted radians phases that destroyed peak timing. Removing a detector that is wrong
~98% of the time is a net improvement, not a regression. The (verified-correct) `day_of_week`
weekly detection is retained. Calendar-accurate monthly/yearly discovery is captured as a possible
follow-up (No-Gos) rather than shipped broken. The FP-rate sentinel on day_of_week guards against
the refactor regressing the one config that does work.

## Race Conditions

No race conditions identified. `temporal_discovery_handler` is a pure function of its `entries`
argument — it reads no shared mutable state and performs no Redis writes. The end-to-end test
writes a CyclicDecayField then reads it back single-threaded. Concurrency is not in scope.

## No-Gos (Out of Scope)

- **Calendar-accurate monthly/yearly cycle discovery.** `week_of_month` and `month_of_year` are
  dropped (not repaired) because correct calendar-period detection needs variable periods and
  calendar-aware phase math that CyclicDecayField's fixed-seconds Lua model does not support. If
  monthly/yearly discovery is ever wanted, it is a separate feature with its own design — a
  follow-up issue, not this bug fix. (The keep-or-drop decision itself is resolved *within* this
  plan — see Resolved Decisions — only the future re-introduction is deferred.)

## Update System

No update system changes required — this is a purely internal library bug fix with no new
dependencies, config files, or migration steps.

## Agent Integration

No agent integration required — `temporal_discovery_handler` is a recipe-internal StreamConsumer
handler. It is not exposed via MCP and the bridge does not call it.

## Documentation

### Feature Documentation
- [ ] Update `docs/guides/policy-cache-recipe.md` temporal-discovery section to state phase is
  emitted in **seconds** (matching CyclicDecayField), and document that only `day_of_week` (weekly)
  discovery remains — both `week_of_month` and `month_of_year` were removed because their fixed
  seconds-period constants cannot represent variable calendar periods (and fabricated cycles from
  noise). Note calendar-accurate monthly/yearly discovery as a possible future feature.
- [ ] Confirm `docs/features/cyclic-decay-field.md:32` ("Time offset in seconds") still accurately
  describes the contract — it does; verify no edit needed and note that the producer now honors it.

### External Documentation Site
- [ ] `mkdocs build --strict` passes (run via `scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Update `temporal_discovery_handler` docstring: phase units = seconds; describe the
  day-of-week→seconds-offset mapping (Thursday epoch anchor, midpoint centering) and note that the
  calendar configs were removed (no χ² helper change).
- [ ] No χ² helper signature change — `chi_squared_uniform` is unchanged, so its docstring is
  untouched.

## Success Criteria

- [ ] **End-to-end peak-timing test (clustered on SUNDAY, not Thursday):** events clustered every
  **Sunday** → handler emits a WEEKLY cycle which, installed on a `CyclicDecayField`, produces a
  Lua-evaluated score whose peak over one weekly period falls within the Sunday window
  (correct phase ≈ 302400s, mid-Sunday) and demonstrably NOT at the epoch-aligned period boundary.
  **Sunday is chosen deliberately:** the epoch (now=0) is a Thursday (`time.gmtime(0).tm_wday == 3`,
  verified), so a Thursday cluster's correct phase (43200s) and the radians-bug phase (≈5.4s for the
  Sunday bucket, ~1.8s for Thursday) both fall inside/adjacent to the same epoch-boundary window —
  the bug would be undetectable. A Sunday cluster puts the correct peak 3.5 days (302400s) away from
  the epoch boundary while the radians value stays at ≈5.4s, giving an unambiguous, large separation.
- [ ] **Score-delta assertion at ≤3600s sweep resolution (isolate pure resonance):** sweep `now`
  across one full weekly period (604800s) in steps of **≤3600s** and assert (a) `argmax(score)` lands
  in the Sunday window, (b) the score at the correct-phase peak exceeds the score at the epoch
  boundary (`now ≡ 0`) by a concretely-defined margin, and (c) the argmax is NOT within the
  Thursday/epoch-boundary window.
  **CRITICAL — the decay term does NOT cancel across the sweep, so the test MUST zero it out.** The
  effective Lua score is `decayed + cyclic + pressure` where
  `decayed = base_score * max((now - last_updated)/86400, 0.01)^(-decay_rate)`. Because `now` varies
  across the sweep, `decayed` varies too (and *explodes* near `now = last_updated` where elapsed_days
  hits the 0.01 floor), so it does NOT cancel between the peak and boundary points. With the default
  `base_score = 1.0`, the boundary point near `now=0` wins on the decay spike (simulated:
  `peak - boundary = -8.47`, argmax at `now=0`) — the test would FAIL against a *correct* fix.
  **Fix: construct the swept member so `base_score = 0`** (set the companion `base_score_field` value
  to 0, and use no `pressure_rate`), making `decayed = 0` and `pressure = 0` so the score is **pure
  resonance** `amplitude * cos(2π(now - phase)/period)`.
  **The margin is then defined against the cosine model:** for a Sunday-peak cycle (`phase = 302400`,
  `period = 604800`, `amplitude = INITIAL_CYCLE_AMPLITUDE = 0.5`), the resonance at the true peak is
  `amplitude * cos(0) = +0.5` and at the epoch boundary `amplitude * cos(π) = -0.5`. Assert
  `peak_score - boundary_score >= 0.9` (analytic gap `amplitude*(1 - cos(π)) = 2*amplitude = 1.0`; 0.9
  leaves margin for the ≤3600s sweep granularity). **Verified by simulation:** with `base_score = 0`,
  peak=+0.5, boundary=-0.5, delta=1.0, argmax exactly at `now=302400` (inside the Sunday window). With
  the radians bug the emitted phase is ≈5.4s, making true-peak and boundary scores nearly identical
  (gap ≈ 0), so this assertion fails loudly on the bug and passes only on the fix.
- [ ] **Producer/consumer phase unit agreed and asserted across the boundary** (one test crossing
  producer→Lua, not two independent unit tests).
- [ ] **FP-rate test (day_of_week regression sentinel):** seeded Monte Carlo (≥500 trials,
  `random.Random(seed)`), uniform-random timestamps at n=400 → day_of_week (WEEKLY) detection rate
  ≤ 0.10. day_of_week is equal-width and already holds ≈alpha; this asserts the refactor did not
  regress it. (`week_of_month`/`month_of_year` are dropped, so there is no monthly/yearly FP gate to
  satisfy — the 97–98% spurious-monthly path is removed by deletion, the most direct possible fix.)
- [ ] **Dropped configs no longer emit:** assert `temporal_discovery_handler` never returns a
  `MONTHLY` or `YEARLY` cycle for any input (the configs are gone), and that no `week_of_month` /
  `month_of_year` references remain in `policy_cache.py`.
- [ ] **Regression:** the existing 20-Mondays-over-20-weeks case still detects WEEKLY; <3
  timestamps still returns `[]`; all-malformed `ts` returns `[]`; existing PolicyCache TD-learning
  tests unaffected.
- [ ] Full suite green against Redis; no Redis-module commands introduced (Valkey-compatible —
  grep for `BF.`/`CMS.`/`CF.` shows none).
- [ ] `docs/guides/policy-cache-recipe.md` and `docs/features/cyclic-decay-field.md` reflect the
  agreed phase semantics and config set.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

The lead agent orchestrates; it never builds directly.

### Team Members

- **Builder (discovery-fix)**
  - Name: discovery-builder
  - Role: Fix phase units (seconds + Thursday epoch anchor) in `temporal_discovery_handler` and
    remove the two biased configs (`week_of_month`, `month_of_year`) per Resolved Decisions #1.
    Keep `chi_squared_uniform` unchanged; add no new χ² helper.
  - Agent Type: builder
  - Resume: true

- **Builder (tests)**
  - Name: discovery-test-builder
  - Role: Write the end-to-end peak-timing test (through real Lua), the seeded FP-rate Monte
    Carlo tests, and the malformed-input/regression tests.
  - Agent Type: test-engineer
  - Resume: true

- **Validator (discovery)**
  - Name: discovery-validator
  - Role: Verify all success criteria; run full suite + Valkey-safety grep + FP-rate determinism.
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: discovery-doc
  - Role: Update recipe guide, cyclic-decay-field doc, docstrings; verify `mkdocs build --strict`.
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Fix phase units and chi-squared null
- **Task ID**: build-discovery-fix
- **Depends On**: none
- **Validates**: tests/test_policy_cache.py (existing temporal tests still pass)
- **Assigned To**: discovery-builder
- **Agent Type**: builder
- **Parallel**: false
- **Remove the two biased configs (no measurement needed — the drop is unconditional):** delete
  the `week_of_month` and `month_of_year` entries from `bucket_configs`, leaving only `day_of_week`.
  This is a flat decision (Resolved Decisions #1), not gated on any FP measurement, so there is no
  step-1↔step-2 ordering dependency.
- In `temporal_discovery_handler`, replace `phase = (peak_bucket / num_buckets) * 2 * math.pi`
  with a **seconds** offset anchored to the weekly epoch: Thursday anchor,
  `phase = (((peak_bucket - 3) % 7) * 86400 + 43200) % 604800`, midpoint-centered (see Technical
  Approach). Reuse the existing `peak_bucket` variable (it equals the peak `tm_wday` for day_of_week;
  add a comment). Assert `time.gmtime(0).tm_wday == 3` in code so the Thursday anchor is
  self-checking; do not hard-code the `3` without the assertion. day_of_week is the only config, so
  no multi-period generalization is required.
- **Keep `chi_squared_uniform` exactly as-is; add NO new χ² helper.** day_of_week is equal-width,
  so the existing uniform test is correct (`E_i = n/7`). A per-bucket-vector helper would have no
  consumer and is therefore NOT added (avoids dead code). `chi_squared_uniform` is imported and
  tested in `tests/test_policy_cache.py` (line 34 import; lines 506/510/515) — do not remove or
  alter it.
- Update the handler docstring (units = seconds; note that only day_of_week/weekly discovery
  remains and why the calendar configs were removed).

### 2. Write tests (end-to-end phase + FP rate + regressions)
- **Task ID**: build-discovery-tests
- **Depends On**: build-discovery-fix
- **Validates**: tests/test_policy_cache.py
- **Assigned To**: discovery-test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- End-to-end peak-timing test: cluster events on **Sundays** (NOT Thursdays — epoch boundary is a
  Thursday, which would mask the bug) → emit cycle → install on a `CyclicDecayField` → evaluate the
  real Lua score across `now` over one full weekly period at **≤3600s sweep resolution** → assert
  argmax within the Sunday window (correct phase ≈302400s), assert `peak_score - boundary_score >= 0.9`,
  and assert argmax is NOT in the Thursday/epoch-boundary window.
  **The swept member MUST have `base_score = 0` and no pressure** (set the `base_score_field` companion
  value to 0; leave `pressure_rate=0.0`) so the decay and pressure terms vanish and the score is pure
  resonance — otherwise the `now`-dependent decay term dominates and the assertion fails against a
  correct fix (simulated `peak - boundary = -8.47` with the default `base_score=1.0`). Derive the
  Thursday epoch anchor from `time.gmtime(0).tm_wday == 3` in-test (self-checking).
- Seeded Monte Carlo FP-rate test (**≥500 trials**, dedicated `random.Random(seed)` generator) at
  n=400 for **day_of_week** → detection rate ≤ 0.10. This is a **regression sentinel** (day_of_week
  is equal-width and already ≈alpha; assert the refactor did not regress it). There is no
  month_of_year/week_of_month FP gate — those configs are deleted in step 1, so assert instead that
  the handler never emits a MONTHLY or YEARLY cycle for any input. Include the n=50/100/200/400
  progression as documentation in a comment.
- Regression + failure-path tests: 20-Mondays weekly still detected; <3 → `[]`; all-malformed
  `ts` → `[]`; empty → `[]`.

### 3. Validate
- **Task ID**: validate-discovery
- **Depends On**: build-discovery-tests
- **Assigned To**: discovery-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_policy_cache.py -q` and the full suite.
- Confirm FP-rate test is deterministic (run twice, same result) and inside the bound.
- Valkey safety (src + tests): `grep -rnE 'BF\.|CMS\.|CF\.|TOPK\.' src/popoto/recipes/policy_cache.py tests/test_policy_cache.py` → none.
- Confirm `chi_squared_uniform` is still present and its two existing tests
  (`test_chi_squared_uniform_basic`, `test_chi_squared_zero_expected`) pass unchanged.
- Confirm `week_of_month` and `month_of_year` are removed from `policy_cache.py`, and that no new
  `chi_squared(` (vector) helper was added (no dead code).
- Confirm the e2e test clusters on Sunday (not Thursday) and sweeps at ≤3600s; confirm FP test uses
  `random.Random` with ≥500 trials.
- Report pass/fail against each Success Criterion.

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-discovery
- **Assigned To**: discovery-doc
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/guides/policy-cache-recipe.md` (phase = seconds; config set + rationale for
  removing/keeping week_of_month / month_of_year).
- Verify `docs/features/cyclic-decay-field.md` phase wording is consistent (likely no edit).
- `scripts/ci-local.sh docs` (`mkdocs build --strict`) passes.

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: discovery-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full suite + docs gate; verify every Success Criterion incl. documentation.
- Generate final report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Temporal tests pass | `pytest tests/test_policy_cache.py -q` | exit code 0 |
| Full suite passes | `pytest -q` | exit code 0 |
| Lint clean | `python -m ruff check src/popoto/recipes/policy_cache.py` | exit code 0 |
| No radians phase remains | `grep -n "2 \* math.pi\|2\*math.pi" src/popoto/recipes/policy_cache.py` | exit code 1 |
| `chi_squared_uniform` preserved | `grep -rn "chi_squared_uniform" src/popoto/recipes/policy_cache.py tests/test_policy_cache.py` | symbol present in BOTH files (def in src, import+calls in tests) |
| Biased configs removed | `grep -nE "week_of_month\|month_of_year" src/popoto/recipes/policy_cache.py` | exit code 1 (neither config remains) |
| No dead χ² vector helper | `grep -n "def chi_squared(" src/popoto/recipes/policy_cache.py` | exit code 1 (only `chi_squared_uniform` exists) |
| No orphaned MONTHLY/YEARLY consumers | `grep -rnE 'TemporalPeriod\.(MONTHLY|YEARLY)' src/` | exit code 1 after the two config lines are deleted (spot-checked pre-build: only those two lines reference them) |
| No Redis-module commands (src+tests) | `grep -rnE 'BF\.|CMS\.|CF\.|TOPK\.' src/popoto/recipes/policy_cache.py tests/test_policy_cache.py` | exit code 1 (correct ERE alternation — `\|` would be a literal pipe and vacuously pass) |
| End-to-end phase test present | `grep -rn "CyclicDecayField" tests/test_policy_cache.py` | output contains CyclicDecayField |
| FP-rate test present | `grep -rni "false.positive\|fp_rate\|monte" tests/test_policy_cache.py` | exit code 0 |
| FP-rate uses ≥500 trials | `grep -rn "random.Random\|500\|range(500" tests/test_policy_cache.py` | output shows seeded Random + ≥500 trials |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| Blocker | critique | FALSE PREMISE: plan said `chi_squared_uniform` is handler-only and could be retired, but it is imported and called in `tests/test_policy_cache.py:34,506,510,515`; retiring it breaks test collection. | Keep `chi_squared_uniform` intact. **[SUPERSEDED by 2nd round — the "add a NEW `chi_squared` helper" remedy was reversed: the second round drops the unequal-width configs entirely, so NO new helper is added. `chi_squared_uniform` is still preserved exactly as-is.]** Re-scoped grep/Valkey gate to include `tests/`. | Technical Approach (Bug 2), Key Elements, Test Impact, Verification table, build step 1. |
| Blocker | critique | E2E test as designed cannot detect the radians bug: epoch (now=0) is Thursday, so a Thursday cluster's correct phase (43200s) and the radians-bug phase both fall in the same epoch-boundary window. | Cluster on **Sunday** (correct phase 302400s, 3.5d from epoch boundary; radians value ≈5.4s). Assert argmax-in-Sunday-window + score-delta + NOT-at-epoch-boundary, sweeping `now` at ≤3600s resolution. | Success Criteria, Risk 1, build step 2. Verified `time.gmtime(0).tm_wday==3` and Sunday phase math. |
| Concern | critique | `E_i = n*p_i` machinery may have no consumer if day_of_week is the sole surviving config. | Sequence the keep/drop decision FIRST (build step 1); add `chi_squared` ONLY IF month_of_year survives, else day_of_week uses existing `chi_squared_uniform` — no dead code. | Key Elements (sequencing), build step 1. |
| Concern | critique | day_of_week FP-rate criterion contradicts the diagnosis (it's already ≈alpha). | Reframed as a **regression sentinel** (assert stays ≤0.10), not a fix target. | Success Criteria. |
| Concern | critique | ≥100 Monte Carlo trials too noisy for a 0.05-vs-0.10 gate. | Raised to **≥500 trials** with a dedicated seeded `random.Random(seed)`; documented SE math (≈0.0097 at 500 vs ≈0.022 at 100). | Risk 2, Success Criteria, build step 2, Verification. |
| Nit | critique | Phase formula both dictated (Technical Approach) and posed as Open Q3. | Resolved: midpoint centering decided in-plan; Open Questions removed. | Technical Approach (Bug 1), Open Questions removed. |

### Second critique round (after revision_applied)

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| Blocker | critique (R&R + S&V) | `month_of_year`, if kept, would still emit the radians phase bug — the seconds-phase mapping was only fully specified for the weekly case; the FP gate doesn't test phase correctness, so a config could pass the gate and ship a mis-timed peak. | **Drop `month_of_year` unconditionally** (alongside `week_of_month`). With no calendar config kept there is no radians-phase path to fix and no phase-timing gate to add. | Problem (Scope decision), Key Elements, Technical Approach (Bug 1/2), build step 1, Resolved Decisions #1. |
| Blocker | critique (R&R) | `month_of_year` `E_i = n·p_i` correction is non-reproducible: true per-month probability is calendar-/leap-dependent, and the plan never pinned the FP-test window, so the ≤0.10 gate is unverifiable as written. | **Drop `month_of_year` unconditionally.** No per-bucket-vector test, no FP window to pin. day_of_week is genuinely equal-width and uses the unchanged `chi_squared_uniform`. | Technical Approach (Bug 2 — "Why not repair"), Resolved Decisions #1, Risk 3. |
| Concern | critique (S&V + H&C) | Circular dependency: build step 1 needed a month_of_year FP measurement that step 2 produces, but step 2 depends on step 1. | Eliminated — the config drop is unconditional (no measurement feeds it), so step 1 has no dependency on step 2's FP result. | build step 1 (rewritten), step 2. |
| Concern | critique (H&C) | "kept iff FP gate / no dead code" invariant had no verification check. | Invariant removed (unconditional drop). Added Verification rows: configs removed (`grep week_of_month\|month_of_year` → exit 1) and no dead χ² vector helper (`grep "def chi_squared("` → exit 1). | Verification table, validate step 3. |
| Concern | critique (R&R) | e2e score-delta margin "2.0× the amplitude term separation" was undefined/ambiguous. | Defined concretely against the cosine model: true peak `amplitude*cos(0)=+0.5`, epoch boundary `amplitude*cos(π)=-0.5`, assert `peak - boundary >= 0.9` (analytic gap 1.0). | Success Criteria (Score-delta assertion). |
| Nit | critique (S&V) | Valkey grep regex `'BF\.\|CMS\.'` under `-E` matches a literal pipe → vacuously passes. | Fixed to unescaped ERE alternation `'BF\.|CMS\.|CF\.|TOPK\.'`. | Verification table, validate step 3. |
| Nit | critique (H&C) | Freshness Check line citations have ~3-line residual drift (622/593/614 vs 625/595/611). | Mitigated by exact code-string quotes; line numbers noted as approximate. Build greps by string, not line. | Freshness Check (note added). |

### Third critique round (after 2nd revision)

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| Blocker | critique (R&R, driver-verified) | The e2e score-delta assertion's claim that "the decay term cancels" is FALSE: `decayed = base_score * elapsed_days^(-decay_rate)` is evaluated at different `now`, so it does not cancel and explodes near `now=0`. Simulated `peak - boundary = -8.47` with default `base_score=1.0`; argmax lands at `now=0`, not Sunday — the test fails against a correct fix. | Build the swept member with **`base_score = 0`** (companion `base_score_field` value = 0, no pressure) so the score is pure resonance. Verified: peak=+0.5, boundary=-0.5, delta=1.0, argmax=302400 (Sunday window). | Success Criteria (Score-delta assertion), build step 2. |
| Concern | critique (all 3) | Phase formula used undefined `peak_wday`; the code variable is `peak_bucket` (identity holds only because day_of_week's bucket_fn returns `tm_wday`). | Use `peak_bucket` directly with a comment noting the identity; assert `time.gmtime(0).tm_wday == 3`. | Technical Approach (Bug 1), build step 1. |
| Concern | critique (S&V) | "No regression" for dropped MONTHLY/YEARLY configs not verified against downstream consumers. | Spot-checked `grep -rnE 'TemporalPeriod\.(MONTHLY|YEARLY)' src/` → only the two deleted config lines; added it as a Verification row. | Verification table. |
| Nit | critique (H&C) | Stale first-round Critique Results row still said "add a NEW chi_squared helper," reversed by round 2. | Annotated that row as SUPERSEDED. | Critique Results (first-round row). |

---

## Resolved Decisions

All prior Open Questions are resolved in this finalized plan:

1. **Config set:** **drop `week_of_month` AND `month_of_year` unconditionally.** Both bucket on
   calendar boundaries (6/7/7/10–11-day "weeks"; 28–31-day months) whose widths cannot be reconciled
   with their fixed-seconds period constants (`MONTHLY`=30d, `YEARLY`=365d) — there is no clean
   seconds-phase mapping for them, and no static per-bucket expected vector can match the
   calendar-/leap-dependent data-generating process (so the χ² null cannot be repaired reproducibly).
   **Keep only `day_of_week`** (equal 7-day buckets, clean Thursday-anchored weekly cycle, correct
   uniform null). The drop is flat — not gated on any FP measurement — which removes the build's
   step-1↔step-2 circular dependency and the dead-helper risk. Calendar-accurate monthly/yearly
   discovery, if ever wanted, is a separate future feature (see No-Gos).
2. **FP-rate sentinel:** for the surviving `day_of_week` config, tolerance ≤0.10 (vs nominal 0.05)
   over **≥500** seeded trials at n=400, using a dedicated `random.Random(seed)` for determinism
   without disturbing global RNG state. This is a regression sentinel (day_of_week already ≈alpha),
   not a keep/drop gate.
3. **Phase centering:** **bucket midpoint** (e.g. mid-Sunday via the `+43200` half-day offset) as the
   most representative "when events cluster."
