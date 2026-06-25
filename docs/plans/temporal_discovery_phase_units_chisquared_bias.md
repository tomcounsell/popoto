---
status: Planning
type: bug
appetite: Medium
owner: valor
created: 2026-06-25
tracking: https://github.com/tomcounsell/popoto/issues/415
last_comment_id:
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

**Notes:** Both bugs reproduced fresh on 2026-06-11 (FP rates 0.21/0.39/0.74/0.97; Thursday phase=1.7952 rad). Line numbers updated above; otherwise premises unchanged. Bug still present on the baseline commit.

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
2. **Bucketing:** timestamps are collected (≥3 required) and binned by three configs:
   `day_of_week` (7 buckets, period WEEKLY), `week_of_month` (4 buckets, period MONTHLY),
   `month_of_year` (12 buckets, period YEARLY).
3. **Significance test:** for each config, `chi_squared_uniform(buckets, expected)` is
   compared against `CHI_SQUARED_CRITICAL_VALUES[df]`. **Bug 2 lives here** — `expected`
   is uniform but the buckets are unequal-width for week_of_month / month_of_year.
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
- PM check-ins: 1-2 (one decision needed: keep, fix, or drop week_of_month / month_of_year — see Open Questions)
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
- **Per-bucket expected counts for the χ² test.** Generalize `chi_squared_uniform` (or add a
  sibling) to accept a per-bucket expected vector `E_i = n·p_i` derived from the actual bucket
  widths, instead of a single uniform `expected_per_bucket`. This restores the nominal alpha.
- **Resolve week_of_month / month_of_year alignment.** week_of_month buckets are
  calendar-misaligned with the fixed 30-day `MONTHLY` constant and have no clean seconds-phase
  mapping; recommend dropping it. month_of_year's per-bucket-probability fix removes the χ²
  bias, but its phase mapping to a fixed 365-day `YEARLY` constant is approximate — decision in
  Open Questions.
- **End-to-end peak-timing test** crossing the producer→consumer boundary through real Lua.

### Flow

Clustered events (e.g. every Thursday) → `temporal_discovery_handler` → `(WEEKLY, 0.5, phase_seconds)`
→ install on `CyclicDecayField.cycles` → query-time Lua score → **peaks within Thursday of the
weekly period, demonstrably NOT at the epoch-aligned boundary**.

### Technical Approach

**Bug 1 — phase in seconds (day_of_week / weekly, the salvageable case):**

- The weekly period anchor: `now mod 604800 == 0` falls on **Thursday 1970-01-01 00:00 UTC**
  (epoch day 0 was a Thursday). `tm_wday` is Mon=0 … Sun=6, so Thursday is `tm_wday == 3`.
- Map the peak weekday to a seconds offset from the anchor, centered at the weekday's midpoint:
  `phase = (((peak_wday - 3) % 7) * 86400 + 43200) % 604800`
  where `43200 = 0.5 * 86400` centers the cosine peak at mid-day of the peak weekday rather than
  at its 00:00 boundary. (The `- 3` rebases Monday-origin `tm_wday` onto the Thursday anchor;
  `% 7` and `% 604800` keep it in range.)
- Generalize the same "bucket index → representative seconds offset within the period" idea for
  any kept config: `phase = ((bucket_anchor_offset + bucket_midpoint_within_period) % period)`.
  The plan must verify the anchor for each kept period against `time.gmtime(0)` rather than
  assuming it.

**Bug 2 — correct expected counts:**

- Compute each bucket's true probability under uniform-in-time as `p_i = width_i / total_width`,
  then `E_i = n * p_i`. For day_of_week all `p_i = 1/7` (already correct — equal-width). For
  week_of_month / month_of_year, `p_i` reflects the real day-spans.
- Add a `chi_squared` variant taking a per-bucket expected vector:
  `sum((o_i - E_i)^2 / E_i for i)` with `E_i > 0` guards. Keep `chi_squared_uniform` as a thin
  wrapper (`E_i = expected` for all i) so existing callers/tests are unaffected, OR retire it if
  week_of_month is dropped and day_of_week stays uniform (decide during build per Open Q1).
- **Calendar caveat:** even per-bucket-probability buckets are only approximately uniform because
  month lengths vary year to year and the `MONTHLY`/`YEARLY` period constants are fixed (30/365
  days). The FP-rate test (≤0.10 at n=400) is the acceptance gate that decides whether the
  approximation is good enough; if month_of_year still exceeds the bound after the E_i fix, it is
  dropped alongside week_of_month.

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
- [ ] If `chi_squared_uniform` is removed (week_of_month dropped, day_of_week uniform), audit
  for any other caller first (`grep chi_squared_uniform`) — currently only the handler uses it.

No other existing tests are affected — the change is confined to `temporal_discovery_handler`,
the χ² helper, and new tests. PolicyCache TD-learning tests do not touch this code path.

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
score across a sweep of `now` values over one period and checking the argmax falls within the
correct weekday window — and explicitly asserts the peak is NOT at the epoch boundary. Derive the
Thursday anchor from `time.gmtime(0)` in the test rather than hard-coding, so the assumption is
self-checking.

### Risk 2: Monte Carlo FP-rate test is flaky in CI
**Impact:** Intermittent failures erode trust and block merges.
**Mitigation:** Seed the RNG (`random.seed(...)`) for determinism; use a generous tolerance
(≤0.10 against nominal 0.05) and ≥100 trials so the seeded result is comfortably inside the bound.
The test is deterministic given the seed — record the observed rate in a comment.

### Risk 3: month_of_year still biased after the E_i fix
**Impact:** The per-bucket-probability correction may not fully restore alpha because real month
lengths vary across years against the fixed 365-day YEARLY period.
**Mitigation:** The FP-rate acceptance gate (≤0.10 at n=400) is applied to month_of_year too. If
it fails, drop month_of_year (same disposition as week_of_month). The plan does not commit to
keeping it sight-unseen.

## Race Conditions

No race conditions identified. `temporal_discovery_handler` is a pure function of its `entries`
argument — it reads no shared mutable state and performs no Redis writes. The end-to-end test
writes a CyclicDecayField then reads it back single-threaded. Concurrency is not in scope.

## No-Gos (Out of Scope)

- Nothing deferred — every relevant item is in scope for this plan. The week_of_month /
  month_of_year keep-or-drop decision (Open Q1) is resolved *within* this plan during build, not
  punted to a follow-up.

## Update System

No update system changes required — this is a purely internal library bug fix with no new
dependencies, config files, or migration steps.

## Agent Integration

No agent integration required — `temporal_discovery_handler` is a recipe-internal StreamConsumer
handler. It is not exposed via MCP and the bridge does not call it.

## Documentation

### Feature Documentation
- [ ] Update `docs/guides/policy-cache-recipe.md` temporal-discovery section to state phase is
  emitted in **seconds** (matching CyclicDecayField), and document which configs are kept
  (day_of_week, and month_of_year only if it passes the FP gate) and why week_of_month was
  removed/changed.
- [ ] Confirm `docs/features/cyclic-decay-field.md:32` ("Time offset in seconds") still accurately
  describes the contract — it does; verify no edit needed and note that the producer now honors it.

### External Documentation Site
- [ ] `mkdocs build --strict` passes (run via `scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Update `temporal_discovery_handler` docstring: phase units = seconds; describe the
  bucket→seconds-offset mapping and the per-bucket-probability χ² correction.
- [ ] Update the χ² helper docstring(s) for the new per-bucket-expected signature.

## Success Criteria

- [ ] **End-to-end peak-timing test:** events clustered every Thursday → handler emits a WEEKLY
  cycle which, installed on a `CyclicDecayField`, produces a Lua-evaluated score whose peak over
  one weekly period falls within Thursday and demonstrably NOT at the epoch-aligned period
  boundary.
- [ ] **Producer/consumer phase unit agreed and asserted across the boundary** (one test crossing
  producer→Lua, not two independent unit tests).
- [ ] **FP-rate test:** seeded Monte Carlo (≥100 trials), uniform-random timestamps at n=400 →
  monthly/relevant-config detection rate ≤ 0.10 (vs current 97–98%).
- [ ] Same FP bound holds at n=400 for day_of_week and (if kept) month_of_year.
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
  - Role: Fix phase units (seconds + epoch anchor) and the χ² expected-counts null in
    `temporal_discovery_handler`; resolve week_of_month/month_of_year per Open Q1.
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
- In `temporal_discovery_handler`, replace `phase = (peak_bucket / num_buckets) * 2 * math.pi`
  with a **seconds** offset anchored to the period epoch (for weekly: Thursday anchor,
  `phase = (((peak_wday - 3) % 7) * 86400 + 43200) % 604800`); generalize to a
  bucket→representative-seconds-offset mapping for each kept config. Derive/verify the period
  anchor from `time.gmtime(0)`, do not assume.
- Add a per-bucket-expected χ² computation (`E_i = n * p_i` from real bucket widths); keep
  `chi_squared_uniform` as a wrapper or retire it if no longer used (grep callers first).
- Resolve Open Q1: drop `week_of_month` (recommended — irreparable calendar/period misalignment),
  and keep `month_of_year` only if it passes the FP gate; otherwise drop it too.
- Update the handler + helper docstrings (units = seconds; per-bucket null).

### 2. Write tests (end-to-end phase + FP rate + regressions)
- **Task ID**: build-discovery-tests
- **Depends On**: build-discovery-fix
- **Validates**: tests/test_policy_cache.py
- **Assigned To**: discovery-test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- End-to-end peak-timing test: cluster events on Thursdays → emit cycle → install on a
  `CyclicDecayField` → evaluate the real Lua score across `now` over one weekly period → assert
  argmax within Thursday window AND not at the epoch boundary.
- Seeded Monte Carlo FP-rate tests (≥100 trials, `random.seed`) at n=400 for each kept config →
  detection rate ≤ 0.10. Include the n=50/100/200/400 progression as documentation in a comment.
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
- Valkey safety: `grep -rn -E 'BF\.|CMS\.|CF\.|TOPK\.' src/popoto/recipes/policy_cache.py` → none.
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
| No Redis-module commands | `grep -rn -E 'BF\.\|CMS\.\|CF\.\|TOPK\.' src/popoto/recipes/policy_cache.py` | exit code 1 |
| End-to-end phase test present | `grep -rn "CyclicDecayField" tests/test_policy_cache.py` | output contains CyclicDecayField |
| FP-rate test present | `grep -rni "false.positive\|fp_rate\|monte" tests/test_policy_cache.py` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. **Keep, fix, or drop week_of_month and month_of_year?** Recommendation: **drop week_of_month**
   (its 4 calendar "weeks" are irreparably misaligned with the fixed 30-day `MONTHLY` period — it
   has no clean seconds-phase mapping and even per-bucket-probability buckets only approximate
   uniformity), and **keep month_of_year only if it passes the FP-rate gate** (≤0.10 at n=400)
   after the `E_i = n·p_i` correction; drop it otherwise. day_of_week is fully salvageable (equal
   7-day buckets, clean weekly anchor) and is kept. Acceptable since substrate is beta. Confirm
   this disposition, or state a preference to retain all three.

2. **FP-rate tolerance and trial count for CI.** Plan proposes ≤0.10 (vs nominal 0.05) over ≥100
   seeded trials at n=400, to keep the test deterministic and non-flaky. Acceptable, or do you
   want a tighter bound / more trials (slower CI)?

3. **Phase centering convention** — center the cosine peak at the *midpoint* of the peak bucket
   (e.g. Thursday 12:00) via the `+43200` half-day offset, or at the bucket *start* (Thursday
   00:00)? Plan assumes midpoint as the most representative "when events cluster." Confirm.
