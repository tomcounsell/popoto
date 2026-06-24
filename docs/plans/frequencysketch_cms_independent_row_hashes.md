---
status: Planning
type: bug
appetite: Small
owner: valor
created: 2026-06-24
tracking: https://github.com/tomcounsell/popoto/issues/414
last_comment_id:
---

# FrequencySketch: independent per-row hashes (restore the depth-7 CMS error bound)

## Problem

`FrequencySketch` is the Count-Min Sketch (CMS) field in Popoto's agent-memory layer.
It answers "roughly how many times have we seen this token?" in fixed memory. The
documented guarantee is the standard CMS bound: `estimate ≤ true + (e/width)·N` with
probability `≥ 1 − e^(−depth)`. At the defaults (`width=2000, depth=7`) that promises
overcount above `eN/w = 136` for at most `e^(−7) ≈ 0.091%` of items.

**Current behavior:** All 7 row hashes are the *same* DJB2-style roll, differing only
in their additive starting seed (`local h = 5381 + row * 16777619`, then a shared
`h = (h*33 + byte) % 2^52` loop, in all three Lua scripts: `CMS_INCR_LUA`,
`CMS_INCR_MULTI_LUA`, `CMS_QUERY_LUA`). Because the per-character loop is affine in the
seed, for any two **same-length** tokens `x, y` the difference `h_row(x) − h_row(y)` is a
constant *independent of row*. So same-length tokens that collide in row 0 collide in
**all 7 rows** — the min-over-rows recovers nothing and the sketch behaves like depth 1
at 7× the memory cost. The June 2026 audit (finding MATH-1) measured **6.67% of items
exceeding the `eN/w` bound vs the 0.091% theory promises — a ~73× violation** for
same-length tokens (13× for variable-length); never-seen tokens returned phantom counts
56.4% of the time. Any logic using `get_frequency()` to gate writes or rank salience
misreads rare or nonexistent topics as moderately frequent.

A second, independent defect surfaced during spike validation (see Spike Results): the
modulus `% 2^52` itself **overflows Lua's IEEE-double exact-integer ceiling (2^53) on
100% of inputs**. `h_prev * 33` with `h_prev < 2^52` reaches ≈2^57, so the current code
is already silently losing low-order precision in real Redis/Valkey Lua. The affine-seed
collapse is the headline bug; the unsafe modulus is a latent precision bug in the same
arithmetic that the fix must also correct.

**Desired outcome:** The `depth` row hashes are (practically) pairwise independent so
same-length colliding tokens no longer collide across all rows, and empirical error on
the audit workload falls within the standard `(e/width, 1 − e^(−depth))` bound. The
implementation stays pure Python + Lua and runs identically on Redis and Valkey (no
`CMS.*`/`BF.*` modules). All arithmetic stays within the Lua-double-safe `< 2^53`
ceiling. The substrate layer is beta, so **breaking the serialized sketch format is
acceptable** — old sketches are simply rebuilt; no migration shim.

## Freshness Check

**Baseline commit:** `a79d1cf3745480e7d1ea8fbcbf45f30a654c120e`
**Issue filed at:** 2026-06-11T05:20:37Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/fields/existence_filter.py:196-212` — `CMS_INCR_LUA` with `local h = 5381 + row * 16777619` and shared `h*33` loop — **still holds, verbatim**.
- `src/popoto/fields/existence_filter.py:214-233` — `CMS_INCR_MULTI_LUA` repeats the construction — **still holds**.
- `src/popoto/fields/existence_filter.py:235-255` — `CMS_QUERY_LUA` repeats the construction — **still holds**.
- `src/popoto/fields/existence_filter.py:620-621` — defaults `width=2000, depth=7` — **still holds** (in `FrequencySketch.__init__`).
- `src/popoto/fields/existence_filter.py:672-679` — `FrequencySketch.on_save` invokes the Lua via `tokenize()` + EVAL — **still holds**.
- Bloom Lua (lines 91-96, 117-122, 141-146, 169-174) uses two structurally different base hashes (multipliers 33 and 16777619) — **confirmed**; this is the correct model the issue cites, and is out of scope.

**Cited sibling issues/PRs re-checked:**
- #415, #416 (sibling MATH/audit findings) — still OPEN, unrelated code paths (temporal / co-occurrence). No interaction.

**Commits on main since issue was filed (touching referenced files):**
- `git log --since=2026-06-11 -- src/popoto/fields/existence_filter.py docs/features/existence-filter.md` returns **nothing**. The file is untouched since the audited revision.

**Active plans in `docs/plans/` overlapping this area:** none. (`existence_filter.md`, `existence_filter_tokenization.md`, `batch-might-exist-selectivity.md` are all shipped/older and touch the Bloom side or tokenization, not the CMS row-hash family.)

**Notes:** No drift. Line numbers exact. Bug reproduced via spike against live Redis (independence metric and Zipf bound both reproduced the audit's failure on current code).

## Prior Art

- **PR #225** (merged 2026-03-17): "Add ExistenceFilter and FrequencySketch field types" — introduced the current CMS Lua with the affine-seed row construction. This is the PR that introduced the defect. No prior attempt has touched the CMS hash family.
- **PR #282** (merged 2026-03-26): "Tokenize fingerprints for word-level bloom queries" — added `tokenize()`, which `on_save`/`get_frequency` now use to split fingerprints into per-token increments. Relevant because the fix's three Lua scripts must keep agreeing across the multi-token path, but it did not touch the hash family.
- **Issue #213 / #341**: ExistenceFilter (Bloom) feature + batch selectivity — Bloom side only, **out of scope** (Bloom verified textbook-correct by the audit).

No prior attempt fixed the CMS row correlation, so there is no "Why Previous Fixes Failed" section — this is the first fix.

## Research

**Queries used:**
- "Count-Min Sketch pairwise independent hash family construction two base hashes h1 + i*h2"

**Key findings:**
- The `h_i(x) = (h1(x) + i·h2(x)) mod w` double-hashing combine (Kirsch–Mitzenmacher style, as the module's Bloom code uses) is the classic *Bloom-filter* trick. The CS literature on CMS specifies row hashes drawn from a *pairwise-independent family* (e.g. polynomial hashing `Σ aᵢ xⁱ mod p`), and is silent on whether the two-base-hash combine is adequate for CMS rows. Sources: [Cormode CMS encyclopedia entry](http://dimacs.rutgers.edu/~graham/pubs/papers/encalgs-cm.pdf), [Wikipedia: Count–min sketch](https://en.wikipedia.org/wiki/Count%E2%80%93min_sketch), [Wikipedia: Universal hashing](https://en.wikipedia.org/wiki/Universal_hashing).
- This ambiguity is exactly why the spike (below) empirically compared the double-hash combine against genuinely independent per-row polynomials. The double-hash combine retained a residual ~5.5e-4 all-7-row collision rate (its linear `r·h2` structure occasionally re-aligns all rows); the per-row independent polynomial hit gold-standard independence. **The literature pointed at the right family (independent polynomials), and the spike confirmed it beats the convenient double-hash shortcut for CMS rows.**

## Spike Results

Two spikes ran (prototype, throwaway scripts, no repo writes). The second corrected a
bignum-masking flaw in the first by enforcing the real Lua 2^53 ceiling.

### spike-1: Which hash-family design decorrelates the rows?
- **Assumption**: "Giving each row a genuinely independent hash (vs the affine seed) restores cross-row independence; some specific construction will satisfy the acceptance metrics."
- **Method**: prototype (Python models of candidate Lua hashes; 20k same-length tokens; row-0-collision → extra-row-match measurement; Zipf bound check)
- **Finding**: Candidates A/B (`h1 + r·h2 (+ r²)` double-hash) are **bit-for-bit identical pairwise** and only marginally better than the bug (the `r²` term cancels in a colliding pair's difference). **The dominant root cause is the composite `width=2000 = 2⁴·5³`**: shared small factors with row indices constrain `(r·Δh) mod 2000` to sub-lattices, inflating cross-row collisions. Switching to **prime `width=2003`** plus a per-row independent polynomial reached gold-standard independence.
- **Confidence**: high (matched a truly-random "gold" baseline)
- **Impact on plan**: Width must change `2000 → 2003` (prime) — this is the single biggest fix. Per-row hashes must be independent polynomials, not seed-only or double-hash variants.

### spike-2: Is the winning construction Lua-double-safe (strict 2^53 ceiling)?
- **Assumption**: "The constants from spike-1 are expressible in Redis Lua 5.1 without exceeding the 2^53 exact-integer ceiling before any modulo."
- **Method**: prototype with a strict guard flagging any intermediate `≥ 2^53` *before* the `%` (simulating real double arithmetic — spike-1 used Python bignums and masked this).
- **Finding**:
  1. **The current code overflows on 100% of inputs.** `h*33` with `h<2^52` reaches ≈2^56.5 (11.2× over 2^53). `M=2^52` is unusable for *any* polynomial roll — even `mult=2` overflows. This is a second, latent precision defect in the same arithmetic.
  2. **Lua-safe winner — per-row independent polynomial with prime moduli < 2^25:**
     - `WIDTH = 2003` (prime; was 2000)
     - 7 distinct prime moduli ≈2^24: `P = {16777259, 16777289, 16777291, 16777331, 16777333, 16777337, 16777381}`
     - 7 distinct prime multipliers ≈2^25: `M = {33554467, 33554473, 33554501, 33554503, 33554509, 33554519, 33554527}`
     - per row `r` (0-indexed 0..6): seed `h = r + 1`; for each byte `c`: `h = (h * M[r] + c) % P[r]`; then `col = h % WIDTH`.
     - **Max intermediate** = `(P[r]-1)*M[r] + 255 ≈ 2^49.0`, comfortably under 2^53. **No finalizer** (the prime modulus does the mixing; a big-multiplier finalizer would reintroduce overflow).
  3. **Validated numbers (strict 2^53 guard):**
     - Independence (20k length-9 tokens, width=2003): mean extra-row matches **0.00308** (theory `6/w = 0.00300`); **all-7-row fraction = 0** (theory ~1.5e-20; current buggy code ~1e-3).
     - Zipf (100k events / 10k vocab, Zipf-1.2): **undercounts = 0**; **bound violations = 0/6066 = 0.000%** (target < 0.091%); max overestimate 15; overflow events 0.
     - Double-hash family reconfirmed under the safe ceiling: mean 0.00332 but **all-7 still 5.5e-4** → rejected in favor of per-row poly.
- **Confidence**: high
- **Impact on plan**: Adopt the exact constants above. Drop `% 2^52` entirely (replace with per-row prime modulus). All three Lua scripts share the identical construction.

## Data Flow

1. **Entry point**: `Model.save()` → calls `FrequencySketch.on_save(...)` (`existence_filter.py:641-680`).
2. **Fingerprint + tokenize**: `_compute_fingerprint()` → `tokenize()` splits into word tokens.
3. **Increment (Lua)**: multi-token path → `CMS_INCR_MULTI_LUA`; empty-token fallback → `CMS_INCR_LUA`. For each token, for each row, compute `col` and `HINCRBY key "row:col" 1` on hash `$FS:{Class}:{field}`.
4. **Query**: `get_frequency()` (`existence_filter.py:701-733`) tokenizes the query and runs `CMS_QUERY_LUA` per token, returning the **min** counter across rows.
5. **Output**: integer estimate (≥ true count).

The hash family is the transformation at steps 3 and 4. **All three Lua scripts must use the identical family** or increment/query disagree (undercount). That is the single load-bearing invariant of the change.

## Architectural Impact

- **New dependencies**: none. Pure Lua + Python.
- **Interface changes**: none. `FrequencySketch(width=…, depth=…, fingerprint_fn=…)` signature unchanged; `get_frequency()` signature unchanged. Only the **default** `width` changes `2000 → 2003` and the internal hash arithmetic changes.
- **Coupling**: unchanged. The three Lua constants are defined once at module level.
- **Data ownership**: unchanged (`$FS:{Class}:{field}` Redis hash).
- **Reversibility**: trivial — revert the three Lua scripts + default. Beta substrate, breaking serialized format is acceptable; no migration. Existing sketches keyed under old hashes simply produce stale counts until rebuilt (acceptable per issue).

## Appetite

**Size:** Small

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0 (scope is fully specified by the issue + spikes)
- Review rounds: 1 (correctness-sensitive arithmetic; one review pass)

The hash design is already resolved by the spikes; the build is a focused edit to three
Lua scripts + one default + a regression test. The risk is in arithmetic correctness,
not scope.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli ping` | Tests EVAL the Lua against a live server (DB 15 isolated by the pytest plugin) |

## Solution

### Key Elements

- **Single shared hash construction**: one canonical per-row polynomial family, used identically by `CMS_INCR_LUA`, `CMS_INCR_MULTI_LUA`, and `CMS_QUERY_LUA`. Define the constant arrays (`P`, `M`) inline in each script's Lua (Lua has no shared-include; keep the three copies byte-identical and assert agreement in a test).
- **Prime width default**: `FrequencySketch.__init__` default `width = 2003` (prime), replacing `2000`. `depth = 7` unchanged.
- **Lua-double-safe arithmetic**: per-row prime modulus `< 2^25`, per-row multiplier `< 2^25`; drop `% 2^52`. Every intermediate proven `< 2^49`.
- **Regression test**: encodes the row-correlation check (same-length row-0-colliding tokens must NOT collide in all rows) so the affine flaw can't silently return.

### Flow

`Model.save()` → `on_save` tokenizes → for each token, **each of 7 rows uses its own
(prime modulus, prime multiplier) polynomial** → `HINCRBY $FS:Class:field "row:col"` →
`get_frequency()` runs the **same** family per row → returns **min** across rows
(now a genuine 7-row minimum, not a depth-1 echo).

### Technical Approach

- Replace, in each of the three CMS Lua scripts, the block
  ```lua
  for row = 0, d - 1 do
      local h = 5381 + row * 16777619
      for i = 1, #item do
          h = ((h * 33) + string.byte(item, i)) % LARGE_MOD
      end
      h = h % w
      ...
  end
  ```
  with a per-row polynomial keyed by Lua tables `P` (moduli) and `M` (multipliers):
  ```lua
  local P = {16777259, 16777289, 16777291, 16777331, 16777333, 16777337, 16777381}
  local M = {33554467, 33554473, 33554501, 33554503, 33554509, 33554519, 33554527}
  for row = 0, d - 1 do
      local pr = P[row + 1]
      local mr = M[row + 1]
      local h = row + 1            -- small distinct seed
      for i = 1, #item do
          h = (h * mr + string.byte(item, i)) % pr
      end
      local col = h % w
      ...
  end
  ```
  Drop the `LARGE_MOD = 2^52` constant (no longer used). `w` (width) remains a runtime
  ARGV so user-supplied non-prime widths still work — but the **default** is now prime.
- Keep `depth` ≤ 7 covered by the 7-entry `P`/`M` tables. **Guard**: if a user passes
  `depth > 7`, the tables would index `nil`. Decide and implement one of: (a) cap/validate
  `depth ≤ 7` in `__init__` with a clear `ValueError`, or (b) generate `P`/`M` for arbitrary
  depth. Recommended: **(a)** — defaults are experimental-tuning constants, not user config;
  a validation error is honest and cheap. (See Open Question 1.)
- The three scripts must stay byte-identical in the hash block. A unit test EVALs all three
  on the same token and asserts the computed columns agree (single source of truth).
- The `tokenize()` lowercasing and the `CMS_INCR_LUA` empty-token fallback path are unchanged.

## Failure Path Test Strategy

### Exception Handling Coverage
- No `except Exception: pass` blocks in `on_save`/`get_frequency`/the Lua scripts. State: **No swallowing exception handlers in scope.** (Lua errors from EVAL propagate to Python as `redis.exceptions.ResponseError` — surfaced, not swallowed.)

### Empty/Invalid Input Handling
- Empty fingerprint → `tokenize()` returns `[]` → `CMS_INCR_LUA` fallback increments `fingerprint.lower()` (existing behavior; covered by `test_empty_string_fingerprint`-style cases — confirm still green after the change).
- `get_frequency` on never-seen token → must return a small integer ≥ 0 (existing `test_query_unseen_returns_zero`); the fix should *reduce* phantom counts but the test asserts exact 0 only on an empty sketch — keep that assertion.
- `depth > 7` (table over-index) → must raise a clear error, not a cryptic Lua `nil` arithmetic error. Add a test asserting the chosen guard behavior.

### Error State Rendering
- No user-visible UI. The "error state" here is the *statistical* error bound; it is tested by the Zipf bound regression rather than a rendering path.

## Test Impact

- [ ] `tests/test_existence_filter.py::TestFrequencySketchBasic::test_increment_and_query` — **UPDATE only if asserting on internal columns** (it asserts counts, so likely unaffected; confirm green).
- [ ] `tests/test_existence_filter.py::TestComputeParams::test_default_params` / `test_custom_params` — these are **Bloom** `_compute_params` tests, NOT CMS. Unaffected (Bloom out of scope). Confirm still green.
- [ ] Any test asserting `FrequencySketch().width == 2000` — **UPDATE to 2003**. (Grep for `width` / `2000` in the test file during build; the `## Parameters` table in docs also changes.)
- [ ] `tests/test_agent_memory_e2e.py`, `tests/test_confidence_field.py` — reference FrequencySketch; verify their assertions don't hard-code `width=2000` or exact phantom counts. Likely unaffected (they test counts, not internals).

No CMS-related `xfail`/`pytest.xfail()` markers exist in the suite (grepped) — nothing to convert.

## Rabbit Holes

- **Don't touch the Bloom filter.** It uses two genuinely different base hashes and the audit verified it textbook-correct. The shared `16777619` multiplier appearing in both Bloom and the old CMS seed is coincidental, not a shared bug.
- **Don't add a migration shim / format-version byte.** Substrate is beta; breaking the serialized sketch is explicitly acceptable. Old `$FS:*` hashes are rebuilt on next save; do not write code to detect/upgrade them.
- **Don't generalize to arbitrary universal-hash parameterization** (random per-instance seeds, salt persisted to Redis, etc.). The constants are experimental-tuning values; a fixed, deterministic, well-chosen family is the goal. Random salting would *also* require persisting the salt and reopens the format/migration question.
- **Don't chase the `r²` / double-hash combine** — the spike proved it keeps a residual all-7 collision rate. Per-row independent polynomial only.
- **Don't try to keep `% 2^52`** "to minimize the diff." It overflows doubles on 100% of inputs; removing it is part of the fix.
- **Don't auto-pick the nearest prime for arbitrary user `width`.** Keep `width` an honest passthrough; only the default is prime. Validating/round-tripping user widths to primes is scope creep.

## Risks

### Risk 1: A user passes `depth > 7` and over-indexes the 7-entry P/M tables
**Impact:** Lua `nil` arithmetic error on save/query — a cryptic runtime failure.
**Mitigation:** Validate `depth ≤ 7` in `FrequencySketch.__init__` and raise a clear
`ValueError` (recommended), OR extend P/M. Add a test for the guard. (Open Question 1.)

### Risk 2: A user passes a composite `width` and re-hits sub-lattice correlation
**Impact:** Independence degrades for non-default composite widths (the `width=2000` failure mode).
**Mitigation:** The per-row *independent* polynomial (distinct moduli/multipliers) already
removes the affine-seed collapse regardless of width; prime width is an additional
improvement. Acceptable: the default is prime; document that prime widths are recommended.
Not a regression vs today (today every width is broken).

### Risk 3: The three Lua scripts drift out of sync during editing
**Impact:** Increment and query use different hashes → undercount (violates "never undercount").
**Mitigation:** A unit test EVALs all three scripts on the same token and asserts identical
columns. This is an acceptance criterion in the issue.

### Risk 4: Spike-1 used Python bignums and masked the 2^53 ceiling
**Impact:** A naive port of spike-1's billion-scale primes would overflow doubles in real Lua.
**Mitigation:** Already caught by spike-2; the adopted constants are all `< 2^25` with max
intermediate `< 2^49`. The build must use spike-2's constants, not spike-1's. A test should
assert no counter position exceeds `width` and that increment/query agree on a long token
(implicitly exercising the arithmetic on a real server).

## Race Conditions

No race conditions identified. Each `HINCRBY` is atomic; each EVAL is atomic server-side.
The change is to deterministic, side-effect-free hash arithmetic inside already-atomic Lua.
No shared mutable Python state, no cross-process coordination.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #415] Temporal phase-unit / chi-squared bias — separate audit finding, unrelated code path.
- [SEPARATE-SLUG #416] CoOccurrenceField unbounded edge weights — separate audit finding, unrelated code path.
- Bloom filter (`ExistenceFilter`) hashing — verified correct by the audit; not a bug. (Advisory; the regression test must NOT modify Bloom Lua — see Verification anti-criterion.)
- Sketch decay / deletion (`on_delete` stays a no-op), EVAL→EVALSHA caching, multi-token round-trip cost — all separately scoped by the audit; not this issue.
- Migration/upgrade of existing serialized sketches — substrate is beta; breaking the format is acceptable, no shim.

## Update System

No update-system changes required — this is a purely internal library change to one
module's Lua/Python; no new deps, config files, or cross-machine propagation.

## Agent Integration

No agent integration required — `FrequencySketch` is a Popoto ORM field consumed by library
users; there is no MCP server / bridge surface in this repo. (The "agent-memory" label
refers to Popoto's cognitive primitives, not a Telegram agent.)

## Documentation

### Feature Documentation
- [ ] Update `docs/features/existence-filter.md`: change the `width` default in the Parameters table from `2000` to `2003` (line ~88); optionally add a one-line note that row hashes are independent polynomials with a prime default width so the standard CMS error bound holds.
- [ ] No `docs/features/README.md` index change (entry already exists).

### External Documentation Site
- [ ] `mkdocs build --strict` must pass (run via `scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Update the `FrequencySketch` docstring (`existence_filter.py:577-612`): default `width` 2000→2003; one sentence that rows use independent per-row polynomial hashes (so the depth-7 bound holds).
- [ ] Add a Lua comment in each script naming the construction and the 2^53-safety invariant (max intermediate < 2^49).

## Success Criteria

- [ ] **Row independence:** For ≥3,000 random same-length tokens at `width=2003, depth=7`, row-0-colliding pairs show mean extra-row matches ≈ `6/width` and **zero** pairs collide in all 7 rows (current code: 100% all-or-none). Verified via a test EVALing the live Lua.
- [ ] **Error bound restored:** On a Zipf workload (≈100k events, ≈10k distinct same-length tokens), fraction of items with `estimate > true + eN/width` is below `e^(−depth) ≈ 0.091%` (currently 6.67%).
- [ ] **Never-undercount preserved:** 0 undercounts across all queried items on the workload.
- [ ] **Increment/query agreement:** a token incremented `n` times via both `CMS_INCR_LUA` and `CMS_INCR_MULTI_LUA` reads back ≥ `n` via `CMS_QUERY_LUA`; a unit test asserts all three scripts compute identical columns.
- [ ] **No 2^52 overflow:** the `LARGE_MOD = 2^52` modulus is removed from all three CMS scripts; arithmetic stays Lua-double-safe.
- [ ] **Regression test:** a suite test encodes the row-correlation check so the affine flaw can't silently return.
- [ ] **depth guard:** passing `depth > 7` raises a clear error (or is supported) — chosen behavior is tested.
- [ ] **No Redis modules:** implementation stays pure Lua + Python; full suite passes against Redis.
- [ ] Tests pass (`/do-test`).
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

The lead agent orchestrates; it does not build directly.

### Team Members

- **Builder (cms-hash)**
  - Name: `cms-hash-builder`
  - Role: Rewrite the three CMS Lua scripts to the per-row polynomial family, change the `width` default to 2003, add the depth guard, update docstrings.
  - Agent Type: builder
  - Resume: true

- **Test engineer (cms-tests)**
  - Name: `cms-test-writer`
  - Role: Write the row-correlation regression test, the three-script-agreement test, the Zipf bound + never-undercount test, and the depth-guard test.
  - Agent Type: test-engineer
  - Resume: true

- **Validator (cms)**
  - Name: `cms-validator`
  - Role: Verify all success criteria, run full suite, confirm no Bloom Lua changed, confirm `2^52` removed.
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: `cms-documentarian`
  - Role: Update `docs/features/existence-filter.md` and confirm `mkdocs build --strict`.
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Rewrite the CMS hash family
- **Task ID**: build-cms-hash
- **Depends On**: none
- **Validates**: tests/test_existence_filter.py
- **Informed By**: spike-2 (exact Lua-safe constants: P, M, width=2003, no finalizer, drop 2^52)
- **Assigned To**: cms-hash-builder
- **Agent Type**: builder
- **Parallel**: true
- In `src/popoto/fields/existence_filter.py`, replace the per-row hash block in `CMS_INCR_LUA`, `CMS_INCR_MULTI_LUA`, and `CMS_QUERY_LUA` with the per-row polynomial using `P = {16777259, 16777289, 16777291, 16777331, 16777333, 16777337, 16777381}` and `M = {33554467, 33554473, 33554501, 33554503, 33554509, 33554519, 33554527}`; seed `h = row + 1`; `h = (h * M[row+1] + byte) % P[row+1]`; `col = h % w`. Keep the three blocks byte-identical.
- Remove the `LARGE_MOD = 4503599627370496` line from all three CMS scripts (no longer referenced). Leave the Bloom scripts' `LARGE_MOD` untouched.
- Change `FrequencySketch.__init__` default `width` from `2000` to `2003`.
- Add a `depth` guard in `__init__` (recommended: `if self.depth > 7: raise ValueError(...)`) — implement per Open Question 1 resolution; default-safe behavior is to cap at 7.
- Update the `FrequencySketch` docstring (default width, independent-rows note) and add a Lua comment naming the family + the `< 2^49` safety invariant.

### 2. Write CMS correctness + regression tests
- **Task ID**: build-cms-tests
- **Depends On**: none
- **Validates**: tests/test_existence_filter.py (new cases)
- **Informed By**: spike-1, spike-2 (metrics + targets)
- **Assigned To**: cms-test-writer
- **Agent Type**: test-engineer
- **Parallel**: true
- Add `test_rows_not_all_or_none`: EVAL the live `CMS_INCR`/query hashing for ≥3,000 same-length tokens at width=2003, depth=7; assert zero pairs collide in all 7 rows and mean extra-row matches ≈ 6/width (loose tolerance).
- Add `test_three_scripts_agree`: increment a token via `CMS_INCR_LUA` and (separately, fresh key) via `CMS_INCR_MULTI_LUA`; `CMS_QUERY_LUA` reads back ≥ n in both; assert the columns the three scripts hit are identical for the same token.
- Add `test_zipf_error_bound`: build a Zipf workload (sized for test speed — e.g. ≥10k events; document the chosen N and the corresponding `eN/w` bound), assert bound-violation fraction below threshold and **zero undercounts**.
- Add `test_depth_guard`: assert chosen behavior for `depth > 7`.
- Update any test asserting `width == 2000` to `2003`.

### 3. Validate
- **Task ID**: validate-cms
- **Depends On**: build-cms-hash, build-cms-tests
- **Assigned To**: cms-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_existence_filter.py -q` and the full suite.
- Confirm `LARGE_MOD`/`2^52` removed from all three CMS scripts; confirm Bloom Lua unchanged (`git diff` touches only CMS blocks + default + docstring).
- Confirm all Success Criteria checkboxes.

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-cms
- **Assigned To**: cms-documentarian
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/existence-filter.md` Parameters table (`width` default 2003) and add the independent-rows note.
- Run `scripts/ci-local.sh docs` (mkdocs --strict).

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-cms-hash, build-cms-tests, validate-cms, document-feature
- **Assigned To**: cms-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full suite + docs gate; verify every Success Criterion; generate final report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_existence_filter.py -q` | exit code 0 |
| Full suite | `pytest -q` | exit code 0 |
| Width default updated | `python -c "from src.popoto.fields.existence_filter import FrequencySketch as F; print(F().width)"` | output contains 2003 |
| 2^52 modulus removed from CMS | `grep -n '4503599627370496' src/popoto/fields/existence_filter.py \| grep -i cms` | match count == 0 |
| Per-row prime moduli present | `grep -c '16777259' src/popoto/fields/existence_filter.py` | output > 0 |
| No Redis modules | `grep -rniE 'CMS\.|BF\.|CF\.|TOPK\.' src/popoto/fields/existence_filter.py` | match count == 0 |
| Bloom Lua untouched (anti-criterion) | `git diff main -- src/popoto/fields/existence_filter.py \| grep -E '^[-+]' \| grep -iE 'BLOOM_'` | match count == 0 |
| Format clean | `python -m black --check src/popoto/fields/existence_filter.py tests/test_existence_filter.py` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. **`depth > 7` handling.** The 7-entry `P`/`M` tables cap usable depth at 7 (the
   default). Preferred: validate `depth ≤ 7` in `__init__` and raise `ValueError`
   (defaults are experimental-tuning constants, not user config). Acceptable alternative:
   generate `P`/`M` for arbitrary depth from a deterministic prime sequence. **Default
   assumption if unanswered: cap/validate at 7 with a clear error.** Is that acceptable, or
   should arbitrary depth be supported?
2. **Zipf test size.** The acceptance criterion cites 100k events / 10k vocab. A full-size
   Zipf run may be slow in the unit suite. **Default assumption: scale down to the smallest
   N that still exercises the bound meaningfully (documented in the test), with the full-size
   run available as an opt-in/marked slow test.** Acceptable?
3. **Non-default composite widths.** We keep `width` an honest passthrough (only the default
   is prime). The independent per-row polynomial fixes the affine collapse at any width, but
   prime widths are statistically best. **Default assumption: document "prime width
   recommended" rather than silently snapping user widths to a prime.** Agree?
