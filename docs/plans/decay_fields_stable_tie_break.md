---
status: Planning
type: bug
appetite: Small
owner: Solo dev
created: 2026-07-07
tracking: https://github.com/tomcounsell/popoto/issues/448
last_comment_id: 4875414759
---

# DecayingSortedField / CyclicDecayField: deterministic Lua tie-break

## Problem

`DecayingSortedField` and `CyclicDecayField` rank members server-side in a Lua
script and return the top-N to callers of `query.top_by_decay()`. Both scripts
sort with a score-only comparator:

```lua
table.sort(scored, function(a, b) return a[2] > b[2] end)
```

Lua 5.1's `table.sort` is **not stable**, so when two or more members have
exactly equal effective scores the relative order of the tied run is undefined —
it can differ between two identical calls, and (worse) it can shuffle *which*
members survive the top-N truncation at the `max_results` boundary.

This is the identical defect fixed for `BM25Field` in #446 (PR #450, merged
`893c1e0`). It was deliberately scoped out of that PR and filed as this issue.

**Current behavior:**
Two members that share the same base score and the same stored timestamp (e.g.
batch-inserted memories saved in the same instant) produce equal decayed scores.
`top_by_decay()` may return them in different orders across repeated identical
calls, and truncation at `n` may include different members run-to-run.

**Desired outcome:**
Equal-scored members return in a deterministic order — member key (redis_key)
ascending, byte-wise — identical across repeated calls, with the tie-break
applied **inside each Lua script before top-N truncation** so the `max_results`
boundary is deterministic. Same guarantee on both Redis and Valkey.

## Freshness Check

**Baseline commit:** `893c1e0957ce34bf7b138d2132b338ab68f97e5d`
**Issue filed at:** 2026-07-03T10:26:40Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/fields/decaying_sorted_field.py:96` — claimed `table.sort(scored, function(a, b) return a[2] > b[2] end)` with no tie-break — still holds, exact line 96.
- `src/popoto/fields/cyclic_decay_field.py:132` — same score-only comparator — still holds, exact line 132.

**Cited sibling issues/PRs re-checked:**
- #446 / PR #450 — closed/merged 2026-07-03T11:22:15Z as commit `893c1e0`. Establishes the exact comparator shape and regression-test template this issue calls for (per the issue's upstream-change comment). Landscape confirmed, not shifted.

**Commits on main since issue was filed (touching referenced files):**
- None. `git log --since=<createdAt>` over both field files returns empty — neither file has changed since the issue was filed.

**Active plans in `docs/plans/` overlapping this area:** None. `decaying_sorted_field.md` and `cyclic_decay_field.md` exist but are both `status: Archived` original feature plans (#193, #196), not active work. `bm25_stable_tie_break.md` is the shipped sibling (#446) used here only as template.

**Notes:** No drift. Line numbers exact. The BM25 fix provides a verified, merged reference to copy from.

## Prior Art

- **PR #450 (#446)**: "deterministic tie-break for equal-scored BM25 search results" — merged 2026-07-03. Added a two-level total-order comparator (score descending, then member key `a[1]` ascending byte-wise) to `BM25_SEARCH_LUA` in `src/popoto/fields/bm25_field.py`, applied before top-K truncation. Added `TestBM25TieOrdering` (four regressions) to `tests/test_bm25_field.py`. **This is the direct template** — the comparator and test structure port almost verbatim to the two decay fields.
- **PR #444 (#418)**: CSR eval harness — unrelated to the fix, but its "no BM25 score ties" corpus rule was relaxed by #446 once ties became deterministic. No action needed here; decay fields are not part of the CSR corpus authoring rules.

No prior *failed* attempts on the decay fields — this is the first fix. `## Why Previous Fixes Failed` omitted.

## Research

No relevant external findings — proceeding with codebase context and the merged
#446 reference. The fix is purely internal Lua/Python with no external
libraries, APIs, or ecosystem patterns. (Redis-modules constraint already known:
the tie-break must live in the Lua script, no `BF.*`/`CMS.*`, identical on Redis
and Valkey.)

## Data Flow

Single-component, server-side. Both scripts follow the same shape:

1. **Entry point**: `query.top_by_decay(field, n=N)` (`src/popoto/models/query.py:292`/`2163`) → builds the sorted-set key and EVALs the field's Lua script with `max_results = N`.
2. **Lua — collect**: `ZRANGE zset_key 0 -1 WITHSCORES` returns members (unique — sorted-set members) with their stored timestamps; per member compute `decayed` (DecayingSortedField) or `decayed + cyclic + pressure` (CyclicDecayField); `table.insert(scored, {member, score})`.
3. **Lua — sort (defect)**: `table.sort` by score descending only → tied runs undefined.
4. **Lua — truncate**: loop `1 .. min(max_results, #scored)`, emit `[member1, score1, ...]`.
5. **Output**: query layer maps returned redis_keys back to model instances.

The fix changes only step 3. Because members come from `ZRANGE` they are unique
strings, so `a[1] < b[1]` is a strict weak ordering (no equal keys). Truncation
(step 4) already runs after the sort, so applying the tie-break in the
comparator automatically makes the `max_results` boundary deterministic.

## Architectural Impact

- **New dependencies**: None.
- **Interface changes**: None. Return shape (`[member, score, ...]` flat array) and the `top_by_decay()` signature are unchanged. Only the ordering of previously-undefined tied runs becomes defined.
- **Coupling**: Unchanged. Self-contained Lua-string edits in two field modules.
- **Data ownership**: Unchanged.
- **Reversibility**: Trivial — revert two comparator edits.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0 (scope fully specified by issue + merged #446 template)
- Review rounds: 1 (standard PR review)

## Prerequisites

No prerequisites — this work has no external dependencies. Redis on
`localhost:6379` for the test suite (DB 15 auto-isolation via the popoto pytest
plugin), which is the standard dev setup.

## Solution

### Key Elements

- **`DECAY_SCORE_LUA` comparator** (`decaying_sorted_field.py`): replace the score-only comparator with a two-level total order — score descending, member key ascending byte-wise.
- **`CYCLIC_DECAY_LUA` comparator** (`cyclic_decay_field.py`): identical change against `effective_score`.
- **Regression tests** (one class per field): plant tied-score members and assert deterministic key-ascending order, repeatability across runs, and deterministic truncation at the `n` boundary.

### Flow

`query.top_by_decay(field, n)` → Lua computes scores → **two-level sort (score
desc, key asc)** → truncate to n → deterministic ordered member list.

### Technical Approach

Copy the merged #446 comparator shape into both scripts:

```lua
table.sort(scored, function(a, b)
    if a[2] ~= b[2] then
        return a[2] > b[2]
    end
    return a[1] < b[1]
end)
```

- `a[1]` is the member's full redis_key; members come from `ZRANGE` so they are unique within `scored` → strict weak ordering, no undefined comparator result.
- Applied before the existing truncation loop → deterministic top-N boundary.
- `a[1] < b[1]` is Lua's byte-wise string comparison → matches the "member-key-ascending" acceptance criterion and the BM25 guarantee wording.
- NaN scores are unreachable: `decayed` is a finite product (`math.pow(elapsed_days, -decay_rate)` with `elapsed_days >= 0.01`, finite base); `cyclic` is a finite cosine sum; `pressure` is a finite product. So `a[2] ~= b[2]` behaves as a normal total order.
- Update the `top_by_decay()` docstring(s) / field docstrings to state the deterministic tie order, mirroring how #446 documented `BM25Field.search()`.

**Test-planting mechanism (already established in `tests/test_decaying_sorted_field.py`):**
The existing suite plants exact timestamps by writing directly to the sorted set,
e.g. lines 336-342 `ZADD` two members with the *same* `old_time`. With a
base-score-free field (`DecayItem.relevance = DecayingSortedField()`, base 1.0
for all), identical timestamps → identical decayed scores → a genuine tie. The
regression tests reuse this pattern: `ZADD` N members with one shared timestamp,
call `top_by_decay`, and assert the returned redis_keys are key-ascending.
For `CyclicDecayField`, use a field with `cycles=[]` and `pressure_rate=0.0` so
`effective_score == decayed`, then plant identical timestamps the same way.

## Failure Path Test Strategy

### Exception Handling Coverage
No exception handlers in scope. The only `pcall`s in these scripts guard
`cmsgpack.unpack` of base-score / cycle / pressure data — untouched by this
change (the fix is confined to the `table.sort` comparator).

### Empty/Invalid Input Handling
- Empty sorted set: `#scored == 0`, the sort is a no-op, truncation loop returns `[]` — behavior unchanged by the comparator edit. Existing tests already cover the empty-result path.
- Single member: sort is trivially stable; tie-break never consulted. No new hazard.
- No agent-output processing in scope.

### Error State Rendering
No user-visible error rendering in scope — `top_by_decay` returns a list; the
change only reorders tied runs deterministically.

## Test Impact

No existing tests are modified or deleted — the change is purely additive and
makes a previously-undefined ordering defined. Existing decay/cyclic tests that
assert *distinct*-score ordering keep passing unchanged (they never hit tied
runs). New regressions are added:

- `tests/test_decaying_sorted_field.py` — ADD `TestDecayTieOrdering` (or module-level test group): tied-score planting, key-ascending assertion, repeat-run identity, deterministic truncation at `n`.
- `tests/test_cyclic_decay_field.py` — ADD the mirror class for `CyclicDecayField` with `cycles=[]`, `pressure_rate=0.0`.

Each new test must assert the tied scores are actually equal (proving the
tie-break path is exercised) and must fail against the pre-fix score-only
comparator.

## Rabbit Holes

- **Do not** try to make Lua `table.sort` "stable" generically or swap in a hand-rolled merge sort — the two-level total-order comparator is the whole fix.
- **Do not** touch the `cmsgpack` base-score / cycle / pressure decoding paths — out of scope and unrelated to ordering.
- **Do not** refactor the shared Lua between `DecayingSortedField` and `CyclicDecayField` into one template in this PR — they are independent strings today; unifying them is a separate chore. Copy the comparator into both.
- **Do not** change the returned score formatting (`tostring(score)`) or the flat-array return shape.

## Risks

### Risk 1: Tie-break relies on member-key uniqueness within `scored`
**Impact:** If two entries in `scored` could share `a[1]`, `a[1] < b[1]` would return false for both orderings and the comparator would not be a strict weak ordering (undefined `table.sort` behavior).
**Mitigation:** Members are produced by `ZRANGE ... WITHSCORES` over a sorted set, whose members are unique by definition. Each member is inserted once into `scored`. Uniqueness is structurally guaranteed. Documented inline in the comparator comment, matching the #446 rationale.

### Risk 2: Partitioned fields use a different sorted set per partition
**Impact:** A regression test that plants into the wrong (unpartitioned) key would not exercise the tie path.
**Mitigation:** Use non-partitioned test models (as `DecayItem` already is) for the tie regressions, or derive the key via `get_sortedset_db_key(...)` exactly as the existing tests at lines 183/220/336 do. Determinism is per-EVAL (single sorted set), so a non-partitioned model fully exercises the fix.

## Race Conditions

No race conditions identified. Each `top_by_decay` call EVALs a single Lua
script that runs atomically on the Redis/Valkey server (single-threaded command
execution); the sort and truncation happen within one script invocation over a
consistent snapshot of the sorted set. The fix adds no shared mutable state and
no cross-call ordering dependency.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #446] Unifying the duplicated decay Lua between `DecayingSortedField` and `CyclicDecayField` into a shared template — a separate refactor chore, not required for this determinism fix. (Tracked conceptually against the sibling tie-break work; this plan copies the comparator into both scripts independently.)

Otherwise: nothing deferred — both required call sites and both regression test
suites are in scope for this plan.

## Update System

No update system changes required — this is a purely internal library fix to two
Lua script strings; no deploy/propagation, new deps, or config.

## Agent Integration

No agent integration required — `top_by_decay` is an existing library query
method already reachable by callers; this change only makes its tied-run
ordering deterministic. No new tool/MCP surface.

## Documentation

### Feature Documentation
- [ ] Update `docs/fields.md` (DecayingSortedField / CyclicDecayField sections, if present) to state the deterministic tie order — score descending, redis_key ascending byte-wise, deterministic at the `n` cutoff — mirroring the BM25Field wording added by #446.
- [ ] Add a `Fixed` entry for #448 under `[Unreleased]` in `CHANGELOG.md`.

### External Documentation Site
- [ ] `mkdocs build --strict` passes (docs gate) if any `docs/` page is touched.

### Inline Documentation
- [ ] Inline comment on each new comparator explaining unstable-sort rationale + member-key-uniqueness guarantee (copy the #446 comment shape).
- [ ] Update `top_by_decay()` / field docstrings to note deterministic tie ordering.

## Success Criteria

- [ ] `DECAY_SCORE_LUA` comparator in `decaying_sorted_field.py` is a two-level total order (score desc, member key asc), applied before truncation.
- [ ] `CYCLIC_DECAY_LUA` comparator in `cyclic_decay_field.py` has the identical two-level total order against `effective_score`.
- [ ] New regression tests for both fields: tied-score members return key-ascending, identical across ≥10 repeated calls, and deterministic truncation at `n`. Each asserts tied scores are equal (tie path exercised) and each fails against the pre-fix comparator.
- [ ] Tie-break lives entirely inside each Lua script (no Redis modules; runs identically on Redis and Valkey).
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)
- [ ] No xfail/xpass to convert (search found none related to this bug).

## Team Orchestration

Single-component fix; one builder + one validator.

### Team Members

- **Builder (decay-tiebreak)**
  - Name: decay-tiebreak-builder
  - Role: Edit both Lua comparators and add regression tests for both fields.
  - Agent Type: builder
  - Resume: true

- **Validator (decay-tiebreak)**
  - Name: decay-tiebreak-validator
  - Role: Verify comparators are total-order + pre-truncation, tests fail pre-fix and pass post-fix, both Lua-internal.
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Fix both Lua comparators
- **Task ID**: build-comparators
- **Depends On**: none
- **Validates**: tests/test_decaying_sorted_field.py, tests/test_cyclic_decay_field.py
- **Informed By**: PR #450 diff (merged `893c1e0`) — comparator shape to copy verbatim
- **Assigned To**: decay-tiebreak-builder
- **Agent Type**: builder
- **Domain**: redis-lua
- **Parallel**: true
- In `src/popoto/fields/decaying_sorted_field.py`, replace the `table.sort(scored, function(a, b) return a[2] > b[2] end)` at line ~96 with the two-level comparator: `if a[2] ~= b[2] then return a[2] > b[2] end; return a[1] < b[1]`, plus an inline comment (unstable-sort rationale + member-key uniqueness).
- In `src/popoto/fields/cyclic_decay_field.py`, apply the identical change at line ~132 against `effective_score`.
- Update the field docstrings / `top_by_decay` docstring to state the deterministic tie order.

### 2. Add regression tests for both fields
- **Task ID**: build-tests
- **Depends On**: none
- **Validates**: tests/test_decaying_sorted_field.py, tests/test_cyclic_decay_field.py
- **Informed By**: `TestBM25TieOrdering` in tests/test_bm25_field.py (template); existing ZADD-same-timestamp planting at tests/test_decaying_sorted_field.py:336-342
- **Assigned To**: decay-tiebreak-builder
- **Agent Type**: builder
- **Domain**: redis-data
- **Parallel**: true
- Add `TestDecayTieOrdering` to `tests/test_decaying_sorted_field.py`: plant ≥5 members with one shared timestamp via direct `ZADD` (base score 1.0), assert `top_by_decay` returns redis_keys in ascending order regardless of insertion order; assert ≥10 repeated calls return identical lists; assert truncation at `n=3` returns the 3 lowest keys; assert tied scores are equal.
- Add the mirror class to `tests/test_cyclic_decay_field.py` using a `CyclicDecayField` with `cycles=[]` and `pressure_rate=0.0`.
- Confirm each new test FAILS against the pre-fix comparator (temporarily revert to verify red state), then passes with the fix.

### 3. Validate
- **Task ID**: validate-comparators
- **Depends On**: build-comparators, build-tests
- **Assigned To**: decay-tiebreak-validator
- **Agent Type**: validator
- **Parallel**: false
- Confirm both comparators are two-level total orders applied before truncation, and the tie-break is inside each Lua string (no Redis modules).
- Run `pytest tests/test_decaying_sorted_field.py tests/test_cyclic_decay_field.py -q` — all pass.
- Confirm the new tests fail against the pre-fix comparator (red-state proof captured).

### 4. Documentation
- **Task ID**: document-fix
- **Depends On**: validate-comparators
- **Assigned To**: decay-tiebreak-validator
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/fields.md` decay/cyclic sections with the deterministic tie-order guarantee.
- Add `Fixed` entry for #448 under `[Unreleased]` in `CHANGELOG.md`.
- Run `mkdocs build --strict` if docs touched.

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-fix
- **Assigned To**: decay-tiebreak-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full suite (`pytest -q`) and confirm no regressions.
- Verify all success criteria met including docs.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Decay/cyclic tests pass | `pytest tests/test_decaying_sorted_field.py tests/test_cyclic_decay_field.py -q` | exit code 0 |
| Full suite passes | `pytest -q` | exit code 0 |
| Decaying comparator has tie-break | `grep -c 'return a\[1\] < b\[1\]' src/popoto/fields/decaying_sorted_field.py` | output contains 1 |
| Cyclic comparator has tie-break | `grep -c 'return a\[1\] < b\[1\]' src/popoto/fields/cyclic_decay_field.py` | output contains 1 |
| No score-only comparator remains (decaying) | `grep -c 'function(a, b) return a\[2\] > b\[2\] end' src/popoto/fields/decaying_sorted_field.py` | match count == 0 |
| No score-only comparator remains (cyclic) | `grep -c 'function(a, b) return a\[2\] > b\[2\] end' src/popoto/fields/cyclic_decay_field.py` | match count == 0 |
| No Redis modules introduced | `grep -rEc 'BF\.\|CMS\.\|TOPK\.\|TDIGEST\.' src/popoto/fields/decaying_sorted_field.py src/popoto/fields/cyclic_decay_field.py` | match count == 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

None. Scope, fix shape, and test-planting mechanism are fully determined by the
issue's acceptance criteria and the merged #446 template. Proceeding to critique.
