---
status: Planning
type: bug
appetite: Small
owner: Claude Code
created: 2026-06-26
tracking: https://github.com/tomcounsell/popoto/issues/416
last_comment_id:
---

# CoOccurrenceField: clamp edge weights to guarantee per-hop contraction

## Problem

`CoOccurrenceField.propagate()` implements spreading activation via a server-side Lua BFS that multiplies activation by `decay_per_hop * edge_weight` at each hop. Edge weights are unbounded — `strengthen()` is a raw `ZINCRBY` with no upper clamp — so heavily-used edges cross the amplification threshold (`edge_weight > 1 / decay_per_hop = 2.0` at defaults). Past that point each hop *amplifies* activation instead of decaying it: activation grows with distance, exceeds the seed's own weight (1.0), longer paths outscore shorter ones, and the threshold cutoff that bounds BFS work becomes structurally unreachable. On a 10k-node / 40k-edge graph with strengthened edges, depth-6 propagation visits 9,999 of 10,000 nodes with 20,142 re-expansions inside a single blocking Lua `EVAL` (148 ms), versus the healthy baseline that plateaus at ~1,096 nodes / ~1,112 expansions / 7 ms.

**Current behavior:** `strengthen()` at `co_occurrence_field.py:373` does `db.zincrby(source_key, delta, target_pk)` with no upper bound. From the default `initial_weight=0.1`, 48 co-retrievals at `delta=0.05` produce weight 2.5 — past the 2.0 amplification threshold. `propagate()` then returns `{b: 1.95, c: 2.44, d: 1.95, e: 2.44}` on a 5-node chain, when the documented behavior is monotonically decreasing activation. `weaken_all()` exists as a counter-force but has zero callers outside tests.

**Desired outcome:** Per-hop transfer (`decay_per_hop * effective_edge_weight`) is always <= 1 for any reachable stored edge weight. Activation is monotonically non-increasing in hop count, the threshold cutoff reliably terminates the BFS, and propagation cost stays bounded regardless of how heavily the graph has been strengthened.

## Freshness Check

**Baseline commit:** `c5cb2b11ff6386a379adebd7646e6fc3df75a20a`
**Issue filed at:** 2026-06-11T05:20:42Z
**Disposition:** Minor drift

**File:line references re-verified:**
- `src/popoto/fields/co_occurrence_field.py:169` — `local propagated_weight = weight * decay_per_hop * edge_weight` — still holds (exact match)
- `src/popoto/fields/co_occurrence_field.py:373` — `new_weight = db.zincrby(source_key, delta, target_pk)` unclamped — still holds (exact match)
- `src/popoto/fields/co_occurrence_field.py:375-377` — symmetric `zincrby` mirror — still holds (exact match)
- `src/popoto/fields/co_occurrence_field.py:440` — `weaken_all()` defined, zero callers outside tests — still holds (exact match)
- `src/popoto/fields/co_occurrence_field.py:56-63` — `LINK_WITH_PRUNE_LUA` NX preserves existing weight — still holds (exact match)
- `src/popoto/fields/co_occurrence_field.py:156-159` — visited/re-expansion logic — still holds (exact match)
- `src/popoto/fields/co_occurrence_field.py:177` — re-queue on higher weight — still holds (exact match)
- `src/popoto/fields/constants.py:84` — `CO_OCCURRENCE_DECAY_PER_HOP = 0.5` — still holds (exact match)
- `src/popoto/recipes/context_assembler.py` — issue cited `:1007` and `:1095` for propagate call sites; **drifted** to `:1227` and `:1332` due to PR #426 (BM25 first-class) and PR #419 (token budgets). Call signatures unchanged (`depth=self.propagation_depth, decay_per_hop=0.5, threshold=0.01`). No semantic impact on this issue.

**Cited sibling issues/PRs re-checked:** Issue body references the June 2026 audit (MATH-6 / PERF-8) and CONC-7. No sibling issue/PR numbers cited. CONC-7 is explicitly dropped from scope.

**Commits on main since issue was filed (touching referenced files):**
- `31ce5b4` feat(#409): make BM25 a first-class retrieval mode and recipe default (#426) — touched `context_assembler.py`, shifted propagate call site line numbers. Irrelevant to the root cause (edge weight math in `co_occurrence_field.py`).
- `11cf23c` fix(context-assembler): honest token budgets (#419) — touched `context_assembler.py`, shifted line numbers. Irrelevant to the root cause.
- No commits touched `co_occurrence_field.py` or `constants.py` since the issue was filed.

**Active plans in `docs/plans/` overlapping this area:** none. The recent plans (temporal_discovery, frequencysketch_cms, lifecycle_tick, stream_consumer, policycache, bm25) all target different subsystems.

**Notes:** The only drift is the `context_assembler.py` line numbers (1007/1095 -> 1227/1332), caused by two merged PRs that added code above the call sites. The propagate call signatures and parameters are unchanged. Corrected line numbers are used in this plan.

## Prior Art

No prior issues found related to this work. `gh issue list --state closed --search "co_occurrence weight propagate"` and `gh pr list --state merged --search "co_occurrence weight clamp"` both returned empty results. This is the first fix for the unbounded edge weight problem.

## Research

No relevant external findings — proceeding with codebase context and training data. This is a purely internal fix (Redis/Valkey ZSET + Lua, no external libraries or APIs).

## Data Flow

Trace of the co-occurrence propagation path that this fix touches:

1. **Entry point**: `ContextAssembler.assemble()` calls `self._co_occurrence_field.propagate(model_class, seed_pks, depth=2, decay_per_hop=0.5, threshold=0.01)` at `context_assembler.py:1227` and `:1332`.
2. **`propagate()`** (`co_occurrence_field.py:519`): loads `PROPAGATE_BFS_LUA` and calls `EVAL` with the key prefix and parameters. The Lua BFS walks edges, computing `propagated_weight = weight * decay_per_hop * edge_weight` at each hop.
3. **Edge weights**: stored as ZSET scores in `$CoOcF:{ClassName}:{field}:{pk}`. Written by `strengthen()` (unclamped `ZINCRBY`) and `link()` (NX `ZADD` at `initial_weight`).
4. **Output**: `{neighbor_pk: max_activation}` dict. `ContextAssembler` uses these scores to boost retrieval ranking.
5. **Write path (this fix)**: `strengthen()` will clamp stored weights at `CO_OCCURRENCE_WEIGHT_CAP` so `decay_per_hop * edge_weight <= decay_per_hop <= 1` always holds.
6. **Read path (this fix)**: `PROPAGATE_BFS_LUA` will apply `min(edge_weight, cap)` as defense-in-depth for pre-existing over-cap weights.

## Architectural Impact

- **New dependencies**: none. Uses existing Redis/Valkey ZSET + Lua primitives.
- **Interface changes**: `strengthen()` return value semantics change slightly — returned weight is now the clamped weight, not the raw `ZINCRBY` result. This is a narrowing of the existing contract (the return is still a float weight, just bounded).
- **Coupling**: no change. The fix is internal to `CoOccurrenceField` and `constants.py`.
- **Data ownership**: no change. Edge weights are still owned by `CoOccurrenceField`.
- **Reversibility**: high. Reverting the clamp (removing the cap constant and the `min()` calls) restores the old behavior. The substrate layer is beta — breaking stored-weight semantics is acceptable per 2026-06-11 maintainer decisions.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1 (code review via /do-pr-review)

Single-file fix plus one constant, one Lua script modification, tests, and docs. The solution space is already narrowed by the issue's Solution Sketch.

## Prerequisites

No prerequisites — this work has no external dependencies. Requires only a running Redis/Valkey instance for tests (already required by the test suite).

## Solution

### Key Elements

- **Weight cap constant**: `CO_OCCURRENCE_WEIGHT_CAP = 1.0` in `constants.py`, alongside the existing `CO_OCCURRENCE_*` swept constants. At the default `decay_per_hop = 0.5`, any `edge_weight <= 1.0` gives per-hop transfer `<= 0.5 < 1` — a strict contraction. The cap is an experimental-tuning value, not user-facing config.
- **Clamp at write (`strengthen()`)**: replace the raw `ZINCRBY` with a small Lua script (`STRENGTHEN_CLAMP_LUA`) that does `min(old + delta, cap)` atomically. The symmetric mirror gets the same treatment. This ensures all newly-written weights are <= cap.
- **Defense-in-depth at read (`PROPAGATE_BFS_LUA`)**: apply `min(edge_weight, cap)` when reading edge weights in the BFS. This handles pre-existing over-cap weights immediately, without requiring a one-time migration — the contraction guarantee holds for any reachable stored weight.
- **`link()` validation**: validate `initial_weight <= cap` in `link()` so new edges cannot start above the cap. The existing `LINK_WITH_PRUNE_LUA` already uses NX semantics, so this is a parameter check only.

### Flow

`strengthen()` call -> Lua `EVAL` (read current weight, add delta, clamp at cap, write back) -> clamped weight returned -> `propagate()` reads edge -> `min(edge_weight, cap)` in Lua BFS -> contraction guaranteed -> threshold pruning terminates BFS -> bounded result set

### Technical Approach

**Write-path clamp via `STRENGTHEN_CLAMP_LUA`:**

```lua
-- KEYS[1] = source ZSET key
-- ARGV[1] = target_pk
-- ARGV[2] = delta
-- ARGV[3] = cap
local existing = redis.call('ZSCORE', KEYS[1], ARGV[1])
local old = tonumber(existing) or 0
local delta = tonumber(ARGV[2])
local cap = tonumber(ARGV[3])
local new_weight = math.min(old + delta, cap)
redis.call('ZADD', KEYS[1], new_weight, ARGV[1])
return tostring(new_weight)
```

This replaces `db.zincrby(source_key, delta, target_pk)` in `strengthen()`. For the symmetric mirror, the same script runs against the target's ZSET. The script is atomic (single `EVAL`), works on both Redis and Valkey, and uses only `ZSCORE`/`ZADD` (no modules).

**Read-path defense in `PROPAGATE_BFS_LUA`:**

Change line 169 from:
```lua
local propagated_weight = weight * decay_per_hop * edge_weight
```
to:
```lua
local effective_weight = math.min(edge_weight, cap)
local propagated_weight = weight * decay_per_hop * effective_weight
```

where `cap` is passed as `ARGV[6]` (new parameter). This guarantees contraction even for pre-existing over-cap weights that haven't been re-strengthened yet.

**Pre-existing over-cap weights policy:** read-time `min()` in `PROPAGATE_BFS_LUA` handles them immediately — no one-time migration needed. The next `strengthen()` call on such an edge will clamp it at the cap (since `min(over_cap + delta, cap) = cap`). This is the "lazy clamp on next write + read-time min()" policy from the issue's Solution Sketch, chosen because it guarantees the invariant for all reachable weights without a migration step.

**`link()` validation:** add `if initial_weight > cap: raise ValueError(...)` in `link()`. The existing `LINK_WITH_PRUNE_LUA` is unchanged (it already uses NX, so it never overwrites an existing weight).

**Cap constant placement:** `CO_OCCURRENCE_WEIGHT_CAP = 1.0` in the `Defaults` class in `constants.py`, in the `CoOccurrenceField` section alongside `CO_OCCURRENCE_DECAY_FACTOR`, `CO_OCCURRENCE_INITIAL_WEIGHT`, and `CO_OCCURRENCE_DECAY_PER_HOP`. Comment notes the swept-tuning context and the contraction invariant: `cap <= 1 / decay_per_hop` ensures contraction.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] No `except Exception: pass` blocks in scope. The `propagate()` call in `context_assembler.py` is already wrapped in `try/except Exception` (lines 1226-1240, 1331-1345) which logs and continues — this is existing behavior, not new, and is appropriate for a non-critical retrieval boost.
- [ ] `strengthen()` raises `ValueError` for `delta <= 0` (existing) — verify it still does after the Lua change.
- [ ] `link()` raises `ValueError` for `initial_weight > cap` (new) — test this.

### Empty/Invalid Input Handling
- [ ] `strengthen()` on a nonexistent edge (no existing ZSET member) — Lua script handles `nil` from `ZSCORE` by defaulting to 0, then `min(0 + delta, cap) = delta` (assuming delta <= cap). Test this edge case.
- [ ] `propagate()` with over-cap pre-existing weights — `min()` clamps them. Test this.

### Error State Rendering
- [ ] No user-visible output in scope. The fix is internal to the field layer. Error states propagate via existing exception paths.

## Test Impact

- [ ] `tests/test_co_occurrence_field.py::TestStrengthen::test_strengthen_increments_weight` — UPDATE: still passes as-is (0.1 + 0.05 = 0.15 < cap=1.0), no change needed. Verify.
- [ ] `tests/test_co_occurrence_field.py::TestStrengthen::test_strengthen_symmetric` — UPDATE: still passes as-is (0.15 < cap). Verify.
- [ ] `tests/test_co_occurrence_field.py::TestPropagate::test_propagate_linear_chain` — UPDATE: uses `initial_weight=1.0` which equals cap. With read-time `min(1.0, 1.0) = 1.0`, results unchanged. Verify.
- [ ] `tests/test_co_occurrence_field.py::TestPropagate::test_propagate_star_graph` — UPDATE: uses weights 0.8/0.6/0.4, all < cap. Unchanged. Verify.
- [ ] `tests/test_co_occurrence_field.py::TestPropagate::test_propagate_multi_path_uses_max` — UPDATE: uses `initial_weight=1.0` and `0.5`. With `min(1.0, 1.0) = 1.0`, unchanged. Verify.
- [ ] `tests/test_co_occurrence_field.py::TestLink::test_link_idempotent` — UPDATE: uses `initial_weight=0.2` and `0.9`, both <= cap. Unchanged. Verify.

All existing tests use weights <= 1.0 (the cap), so no existing test assertions need to change. New tests are additive.

## Rabbit Holes

- **Asymptotic clamp variant** (`w += delta * (cap - w)`): the issue mentions this as an alternative that preserves ordering without exact saturation. It is tempting but adds complexity (harder to test exact values, subtler math) for marginal benefit at the current scale. The hard cap `min(old + delta, cap)` is simpler, predictable, and satisfies all acceptance criteria. Avoid the asymptotic variant for now.
- **One-time migration script for over-cap weights**: unnecessary. Read-time `min()` in `PROPAGATE_BFS_LUA` handles pre-existing over-cap weights immediately. A migration would add operational complexity for no correctness gain.
- **Fixing CONC-7 (symmetric link two-EVAL race)**: explicitly dropped by the issue. Same file, different defect. File separately if pursued.
- **Wiring up `weaken_all()` to a recipe**: the issue notes `weaken_all()` has zero callers, but fixing that is a separate concern (time-based forgetting policy). This plan only fixes the contraction invariant. Do not add callers to `weaken_all()`.
- **Sweeping the cap value**: the cap is `1.0` by reasoning (`cap <= 1 / decay_per_hop` ensures contraction at the default `0.5`). A parameter sweep is out of scope — the value is derived from the existing `decay_per_hop` constant, not independently tuned.

## Risks

### Risk 1: Existing stored weights above 1.0 in production
**Impact:** Pre-existing over-cap weights would violate the contraction invariant if the read-path `min()` were missing.
**Mitigation:** The read-path `min(edge_weight, cap)` in `PROPAGATE_BFS_LUA` guarantees the invariant for any stored weight, including pre-existing over-cap ones. No migration required. The substrate layer is beta, so over-cap weights in existing deployments are expected to be clamped lazily.

### Risk 2: `strengthen()` Lua script changes return value semantics
**Impact:** Callers expecting the raw `ZINCRBY` result (unbounded) would see a different value after clamping.
**Mitigation:** The return value is still a float weight — just bounded. The only caller in `src/` is `context_assembler.py` which does not inspect the return value of `strengthen()`. Tests verify the new clamped return value.

### Risk 3: Cap interacts with `weaken_all()` pruning threshold
**Impact:** `weaken_all()` prunes edges below `factor * 0.01`. With weights capped at 1.0, this threshold is unchanged. No interaction.
**Mitigation:** No action needed — `weaken_all()` is orthogonal (time-based decay, not co-retrieval strengthening). Verify with existing `weaken_all()` tests.

## Race Conditions

### Race 1: Concurrent `strengthen()` on the same edge
**Location:** `co_occurrence_field.py:340-377` (strengthen method)
**Trigger:** Two concurrent `strengthen()` calls on the same source-target edge.
**Data prerequisite:** Both read the same `ZSCORE` before either writes.
**State prerequisite:** Redis/Valkey single-threaded command execution.
**Mitigation:** The `STRENGTHEN_CLAMP_LUA` script runs as a single atomic `EVAL` — Redis/Valkey executes it as one indivisible command. The read-then-write inside the script is serialized by the server's single-threaded model. No race possible.

No other concurrency concerns — `propagate()` is read-only, `link()` already uses atomic NX Lua.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #416-CONC-7] CONC-7 (symmetric `link()` two-EVAL race) — same file, different invariant. The issue explicitly drops it: "file separately if pursued."
- [EXTERNAL] Sweeping `CO_OCCURRENCE_WEIGHT_CAP` across multiple values — the cap is derived from the existing `decay_per_hop` constant (`cap <= 1 / decay_per_hop`), not an independently tuned parameter. A sweep would require simultaneously retuning `decay_per_hop` and is out of scope for a bug fix.
- [EXTERNAL] Wiring `weaken_all()` into a recipe (time-based forgetting) — the issue notes zero callers but fixing that is a separate policy decision, not a contraction-invariant fix.

## Update System

No update system changes required — this is a purely internal library fix. The new constant is picked up automatically when the package is updated. No new dependencies, config files, or migration steps for existing installations.

## Agent Integration

No agent integration required — this is a substrate-layer (CoOccurrenceField) fix. The agent does not directly invoke `strengthen()` or `propagate()`; these are called internally by `ContextAssembler` which is already wired into the agent's context assembly path. No MCP server or bridge changes needed.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/co-occurrence-field.md` to document:
  - The weight cap (`CO_OCCURRENCE_WEIGHT_CAP = 1.0`) and the contraction guarantee
  - That `strengthen()` now clamps at the cap (behavioral change from unbounded `ZINCRBY`)
  - That `propagate()` applies `min(edge_weight, cap)` as defense-in-depth
  - The relationship between cap and `decay_per_hop` (cap <= 1 / decay_per_hop ensures contraction)
- [ ] Verify docs build passes (`mkdocs build --strict`)

### External Documentation Site
- [ ] The `docs/features/co-occurrence-field.md` page is served by the MkDocs site. Update is covered above.

### Inline Documentation
- [ ] Update `strengthen()` docstring to note the clamp behavior
- [ ] Update `propagate()` docstring to note the read-time `min()` defense
- [ ] Update `CoOccurrenceField` class docstring to mention the weight cap
- [ ] Comment on `STRENGTHEN_CLAMP_LUA` explaining the atomic clamp
- [ ] Comment on the `min()` line in `PROPAGATE_BFS_LUA` explaining the defense-in-depth

## Success Criteria

- [ ] For any sequence of `link()`/`strengthen()` calls, the effective per-hop transfer factor (`decay_per_hop * effective_edge_weight`) is <= 1 at the default `decay_per_hop = 0.5`; equivalently, on a chain graph built by maximally strengthening every edge, `propagate()` activation is monotonically non-increasing in hop count and never exceeds 1.0.
- [ ] The chain reproduction from the issue (5-node chain `a-b-c-d-e`, all edges at weight 2.5) returns `b >= c >= d >= e` after the fix (currently `b: 1.95, c: 2.44, d: 1.95, e: 2.44`).
- [ ] On the 10k-node / 40k-edge graph with all edges maximally strengthened, depth-6 single-seed `propagate()` plateaus like the healthy graph (expansion count stops growing once activation crosses the threshold), instead of visiting 9,999 nodes with 20,142 expansions.
- [ ] Semantics for pre-existing over-cap stored weights are decided (read-time `min()` in propagate + lazy clamp on next strengthen), implemented, and covered by a test.
- [ ] Regression test added exercising strengthen-past-the-cap + propagate; passes against both Redis and Valkey (no module commands introduced).
- [ ] `docs/features/co-occurrence-field.md` updated to state the weight bound and the contraction guarantee.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)
- [ ] No `except Exception: pass` blocks introduced in touched files

## Team Orchestration

### Team Members

- **Builder (co-occurrence-clamp)**
  - Name: clamp-builder
  - Role: Implement the weight cap constant, STRENGTHEN_CLAMP_LUA, propagate Lua min(), link() validation, and new tests
  - Agent Type: builder
  - Resume: true

- **Validator (co-occurrence-clamp)**
  - Name: clamp-validator
  - Role: Verify all acceptance criteria, run full test suite, confirm no Redis module commands, check docs
  - Agent Type: validator
  - Resume: true

- **Documentarian (co-occurrence-docs)**
  - Name: clamp-docs
  - Role: Update docs/features/co-occurrence-field.md and inline docstrings
  - Agent Type: documentarian
  - Resume: true

### Available Agent Types

**Tier 1 — Core (default choices):**
- `builder` - General implementation (default for most work)
- `validator` - Read-only verification (no Write/Edit tools)
- `code-reviewer` - Code review, security checks
- `test-engineer` - Test implementation and strategy
- `documentarian` - Documentation updates

## Step by Step Tasks

### 1. Add weight cap constant
- **Task ID**: build-constant
- **Depends On**: none
- **Validates**: `tests/test_co_occurrence_field.py` (existing tests still pass)
- **Assigned To**: clamp-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `CO_OCCURRENCE_WEIGHT_CAP = 1.0` to the `Defaults` class in `src/popoto/fields/constants.py`, in the CoOccurrenceField section (after line 86, alongside the other `CO_OCCURRENCE_*` constants)
- Add a comment noting the contraction invariant: `cap <= 1 / CO_OCCURRENCE_DECAY_PER_HOP` ensures per-hop transfer <= 1
- Import `CO_OCCURRENCE_WEIGHT_CAP` in `co_occurrence_field.py` (alongside the existing `Defaults` import)

### 2. Implement STRENGTHEN_CLAMP_LUA and modify strengthen()
- **Task ID**: build-strengthen-clamp
- **Depends On**: build-constant
- **Validates**: `tests/test_co_occurrence_field.py::TestStrengthen` (existing tests pass), new clamp test
- **Assigned To**: clamp-builder
- **Agent Type**: builder
- **Parallel**: true
- Define `STRENGTHEN_CLAMP_LUA` script string (read ZSCORE, add delta, clamp at cap via `math.min`, ZADD write-back, return new_weight)
- Register the script with `POPOTO_REDIS_DB.register_script()` (following the existing pattern used for `LINK_WITH_PRUNE_LUA` and `PROPAGATE_BFS_LUA`)
- Replace `db.zincrby(source_key, delta, target_pk)` in `strengthen()` with the Lua script call
- Replace the symmetric mirror `db.zincrby(target_key, delta, source_pk)` with the same Lua script call against the target key
- Handle the `pipeline` parameter: if a pipeline is provided, use `pipeline.eval(script, 1, key, ...)` instead of `db.eval(...)` (match the existing pattern in `link()`)
- Update `strengthen()` docstring to note the clamp behavior

### 3. Add read-time min() defense in PROPAGATE_BFS_LUA
- **Task ID**: build-propagate-defense
- **Depends On**: build-constant
- **Validates**: `tests/test_co_occurrence_field.py::TestPropagate` (existing tests pass), new contraction test
- **Assigned To**: clamp-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `cap` as `ARGV[6]` to `PROPAGATE_BFS_LUA` (new parameter)
- Change the weight computation: `local effective_weight = math.min(edge_weight, cap)` then `local propagated_weight = weight * decay_per_hop * effective_weight`
- Update the `propagate()` Python method to pass `Defaults.CO_OCCURRENCE_WEIGHT_CAP` as the 6th ARGV
- Update the Lua script comment block to document `ARGV[6] = weight cap`
- Update `propagate()` docstring to note the read-time clamp

### 4. Add link() initial_weight validation
- **Task ID**: build-link-validation
- **Depends On**: build-constant
- **Validates**: `tests/test_co_occurrence_field.py::TestLink` (existing tests pass), new validation test
- **Assigned To**: clamp-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `if initial_weight > Defaults.CO_OCCURRENCE_WEIGHT_CAP: raise ValueError(...)` in `link()`
- Add a test that `link()` with `initial_weight > cap` raises `ValueError`

### 5. Write new tests
- **Task ID**: build-tests
- **Depends On**: build-strengthen-clamp, build-propagate-defense, build-link-validation
- **Validates**: new tests in `tests/test_co_occurrence_field.py`
- **Assigned To**: clamp-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- `test_strengthen_clamps_at_cap`: link at 0.1, strengthen repeatedly until cap, verify weight never exceeds cap and equals cap at saturation
- `test_strengthen_clamp_symmetric`: same for symmetric mode, verify both directions clamped
- `test_propagate_monotonic_with_strengthened_edges`: build a 5-node chain, strengthen all edges to cap, propagate depth=4, verify `b >= c >= d >= e` and all <= 1.0
- `test_propagate_threshold_terminates_strengthened_graph`: on a small strengthened graph (edges at cap), verify depth-6 propagate does not visit all nodes (threshold pruning fires)
- `test_propagate_read_time_min_for_overcap_weights`: manually `ZADD` a weight above cap, propagate, verify the effective weight is clamped (activation <= decay_per_hop * cap)
- `test_link_rejects_overcap_initial_weight`: `link()` with `initial_weight > cap` raises `ValueError`
- `test_strengthen_nonexistent_edge_clamps`: `strengthen()` on a nonexistent edge creates it at `min(delta, cap)`

### 6. Update documentation
- **Task ID**: document-feature
- **Depends On**: build-tests
- **Assigned To**: clamp-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/co-occurrence-field.md`:
  - Add `CO_OCCURRENCE_WEIGHT_CAP` to the Parameters section (note: experimental tuning constant, not user config)
  - Update `strengthen()` section to note the clamp: "Weights are clamped at `CO_OCCURRENCE_WEIGHT_CAP` (default 1.0) to guarantee per-hop contraction during propagation."
  - Update `propagate()` section to note the contraction guarantee: "Per-hop transfer is always <= `decay_per_hop` because edge weights are clamped. The BFS threshold reliably terminates the walk."
  - Add a "Contraction Guarantee" subsection explaining the invariant: `cap <= 1 / decay_per_hop` ensures `decay_per_hop * edge_weight <= 1`
- Update inline docstrings (strengthen, propagate, CoOccurrenceField class)

### 7. Final validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: clamp-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/test_co_occurrence_field.py -v` then `pytest tests/ -x -q`
- Verify no Redis module commands introduced: `grep -rn 'BF\.\|CMS\.\|TOPK\.\|CF\.' src/popoto/fields/co_occurrence_field.py` (should be empty)
- Verify docs build: `mkdocs build --strict`
- Verify all acceptance criteria from the issue are met
- Run the chain reproduction from the issue (5-node, all edges at 2.5) and confirm `b >= c >= d >= e`
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_co_occurrence_field.py -v` | exit code 0 |
| Full suite passes | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check src/popoto/fields/co_occurrence_field.py src/popoto/fields/constants.py` | exit code 0 |
| Format clean | `python -m ruff format --check src/popoto/fields/co_occurrence_field.py src/popoto/fields/constants.py` | exit code 0 |
| No Redis module commands | `grep -cE 'BF\.|CMS\.|TOPK\.|CF\.' src/popoto/fields/co_occurrence_field.py` | match count == 0 |
| Cap constant exists | `grep -c 'CO_OCCURRENCE_WEIGHT_CAP' src/popoto/fields/constants.py` | output > 0 |
| Strengthen uses Lua clamp | `grep -c 'STRENGTHEN_CLAMP_LUA' src/popoto/fields/co_occurrence_field.py` | output > 0 |
| Propagate has read-time min | `grep -c 'math.min(edge_weight' src/popoto/fields/co_occurrence_field.py` | output > 0 |
| Link validates initial_weight | `grep -c 'initial_weight.*CO_OCCURRENCE_WEIGHT_CAP' src/popoto/fields/co_occurrence_field.py` | output > 0 |
| Docs updated | `grep -c 'CO_OCCURRENCE_WEIGHT_CAP\|contraction\|clamp' docs/features/co-occurrence-field.md` | output > 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| No unclamped zincrby remains | `grep -c 'zincrby' src/popoto/fields/co_occurrence_field.py` | match count == 0 |

## Critique Results

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| | | | | |

---

## Open Questions

1. **Cap value choice**: The plan uses `CO_OCCURRENCE_WEIGHT_CAP = 1.0` because at the default `decay_per_hop = 0.5`, this gives per-hop transfer `<= 0.5` (a strong contraction). A lower cap (e.g. 0.5) would give an even stronger contraction but would saturate edges faster, reducing the dynamic range of `strengthen()`. Is 1.0 the right default, or should it be tighter?

2. **Asymptotic vs hard clamp**: The plan uses a hard clamp `min(old + delta, cap)` for simplicity and testability. The asymptotic variant `w += delta * (cap - w)` preserves ordering among saturated edges without exact saturation. Is the hard clamp acceptable, or is the asymptotic variant preferred for the agent-memory use case where distinguishing heavily-used edges matters?