---
status: Planning
type: bug
appetite: Medium
owner: Valor
created: 2026-06-22
tracking: https://github.com/tomcounsell/popoto/issues/410
last_comment_id:
---

# PolicyCache Q-value / Decay-Timestamp Storage-Slot Separation

## Problem

`PolicyCache` is Popoto's reference recipe for reinforcement-learning-style learned
action selection. Its model `PolicyEntry` stores `state → action → expected_value`
triples, where `expected_value` is a **Q-value** updated by temporal-difference (TD)
learning. The Q-value is declared as a `DecayingSortedField`, whose Redis sorted-set
(ZSET) score is — by design — a wall-clock *last-updated timestamp* used for power-law
recency decay.

The Q-value and the timestamp are written to the **same ZSET score slot**, so each
write path destroys the other. TD learning through PolicyCache is structurally unusable.

**Current behavior:**

1. **Every `save()`/`touch()` destroys the learned Q-value.** `DecayingSortedField`
   forces `auto_now=True` (`decaying_sorted_field.py:133-137`), so
   `SortedFieldMixin.format_value_pre_save` replaces the field value with `time.time()`
   on every save, and `Model.touch()` ZADDs `time.time()` into the same sorted set
   (`base.py:1908-1931`). A learned Q (≈[0,1]) is silently overwritten with ~1.75e9.
   The ObservationProtocol `"acted"` handler touches **every** DecayingSortedField on the
   instance (`observation.py:231-244`), so merely recording that a policy was acted upon
   erases what was learned.
2. **Every decay query misreads a surviving Q as a 1970-epoch timestamp.** The decay Lua
   computes `elapsed_days = (now − score) / 86400` (`decaying_sorted_field.py:83`). A Q of
   0.5 reads as ~20,600 days elapsed, so all learned policies decay-rank as ~equally
   ancient while one freshly-touched (Q-destroyed) entry dominates the ranking.
3. **The next TD update after a clobber is nonsensical.** `TD_UPDATE_LUA` ZSCOREs the
   current score and computes `td_error = reward + γ·maxQ′ − 1.75e9`
   (`policy_cache.py:132-157`).

The code concedes the collision: `initialize_q_value`'s docstring says "After save(),
DecayingSortedField stores the current timestamp as score. This function overrides that"
(`policy_cache.py:362-377`) — but nothing prevents re-clobbering on the next save/touch.
The TD(0) math is correct in isolation; the storage design makes it unusable.

**Desired outcome:**
Learned Q-values and decay timestamps live in **separate storage** so neither write path
can corrupt the other. Q-values survive `save()`, `touch()`, and ObservationProtocol
outcomes; decay queries only ever see timestamps; TD updates only ever see Q-values; and
`initialize_q_value`'s "override the timestamp" workaround disappears rather than being
patched around.

## Freshness Check

**Baseline commit:** `31ce5b486893da9b1cddf3fd5249de7fa983cbf6`
**Issue filed at:** 2026-06-11T05:20:28Z
**Disposition:** Minor drift

**File:line references re-verified (all against baseline):**
- `recipes/policy_cache.py:203-205` — `expected_value = DecayingSortedField(partition_by="agent_id")` — **still holds.**
- `recipes/policy_cache.py:132-157` — `TD_UPDATE_LUA` ZSCOREs/ZADDs the field's ZSET — **still holds.**
- `recipes/policy_cache.py:362-377` — `initialize_q_value` raw-ZADDs over the timestamp, docstring admits collision — **still holds.**
- `recipes/policy_cache.py:391-402` — `_get_sortedset_key` resolves the partitioned ZSET key — **still holds.**
- `recipes/policy_cache.py:499-512` — crystallization handler orders `save()` then `initialize_q_value()` — **still holds.**
- `fields/decaying_sorted_field.py:133-137` — forces `auto_now=True` — **still holds.**
- `fields/decaying_sorted_field.py:83` — decay Lua `elapsed_days = (now − last_updated) / 86400` — **still holds.**
- `fields/decaying_sorted_field.py:67-79` — decay Lua reads `base_score_field` via `HGET member <field>` + `cmsgpack.unpack`, handling plain numbers and Decimal tagged dicts — **confirmed (key to the chosen solution).**
- `models/base.py:1908-1931` — `touch()` ZADDs `time.time()` into the field's ZSET — **drifted:** issue cited `1874-1931`; the `touch()` method now begins at line 1908 (the 1874-1907 range is the preceding `_incrby`-style helper). The ZADD/timestamp behavior is unchanged.
- `fields/observation.py:231-244` — `_apply_acted` touches every DecayingSortedField — **drifted:** issue cited `236-239`; the touch loop is now at `242-244`. Behavior unchanged (PR #417 changed only the confidence epsilon path in this file).

**Cited sibling issues/PRs re-checked:**
- Audit findings MATH-2 and POLICY-1 — internal audit labels, no separate GitHub issues; both describe this single defect.

**Commits on main since issue was filed (touching referenced files):**
- `17a8329` fix(#412): atomic secondary-index maintenance via Lua — touched `base.py` (filtered IndexedFieldMixin fields out of the plain HSET mapping; comment fixes). **Irrelevant** to the Q-value/timestamp aliasing; shifted some line numbers.
- `c1bd02f` feat(confidence #417): capped-evidence Bayesian update — touched `observation.py` (confidence epsilon path only). **Irrelevant** to `_apply_acted`'s DecayingSortedField touch loop; shifted some line numbers.

**Active plans in `docs/plans/` overlapping this area:** `policy_cache.md` exists but is **Archived** (the original recipe-shipping plan, issue #232 / PR #239). No active overlap.

**Notes:** Drift is line-number-only; every claim in the issue holds against current source. Corrected line numbers are reflected throughout this plan.

## Prior Art

- **Issue #232 / PR #239** (closed/merged): "Add PolicyCache — learned action selection
  from crystallized patterns" — the original recipe that shipped `PolicyEntry`,
  `DecayingSortedField` as `expected_value`, `TD_UPDATE_LUA`, `initialize_q_value`, and the
  crystallization handler. This is the code that introduced the storage-slot collision. No
  prior fix has been attempted; this is the first plan to address the aliasing.

## Why Previous Fixes Failed

The only "fix" present is `initialize_q_value`'s timestamp-override workaround
(`policy_cache.py:362-377`), introduced with the original recipe.

| Prior Fix | What It Did | Why It Failed / Was Incomplete |
|-----------|-------------|--------------------------------|
| PR #239 `initialize_q_value` | After `save()`, raw-ZADDs the initial Q over the timestamp the field just wrote | Only protects the **initial** write. The very next `save()`, `touch()`, or `"acted"` outcome re-clobbers Q with `time.time()`. It patches one symptom of the aliasing rather than removing the alias. |

**Root cause pattern:** A single ZSET score slot is forced to carry two semantically
incompatible quantities (a learned value and a wall-clock timestamp). Any fix that keeps
both in the same slot is doomed; the only durable fix is to give each its own storage.

## Data Flow

Q-value write/read paths today (all converging on one ZSET score slot):

1. **Crystallization** (`crystallization_handler`, `policy_cache.py:499-512`):
   `PolicyEntry(...).save()` → `SortedFieldMixin.format_value_pre_save` writes
   `time.time()` into ZSET score → `initialize_q_value(policy, ci_lower)` raw-ZADDs Q over
   it. Q now in the slot.
2. **TD update** (`update_q_value` → `TD_UPDATE_LUA`, `policy_cache.py:316-359`):
   `ZSCORE` reads current Q → computes new Q → `ZADD` writes new Q back. Q stays in slot.
3. **Any save** (e.g. mutating `action_spec` then `.save()`): `format_value_pre_save`
   overwrites the slot with `time.time()`. **Q destroyed.**
4. **Any touch** (`Model.touch("expected_value")`, or `"acted"` via `_apply_acted`):
   ZADDs `time.time()`. **Q destroyed.**
5. **Decay query** (`top_by_decay` / `composite_score`, `query.py:292,1180-1238` →
   `DECAY_SCORE_LUA`): reads each member's ZSET score as `last_updated`, computes
   `elapsed_days`, multiplies by `base_score` read from `HGET member <base_score_field>`.
   If a Q survived in the slot it is misread as a 1970 timestamp.

**Target data flow:** the ZSET score slot holds **only** the timestamp (untouched
`DecayingSortedField` behavior). The Q-value lives in the model hash as a plain numeric
field and is read by the decay Lua as the `base_score_field` it already supports — so
`decayed_rank = Q × elapsed_days^(−decay_rate)`, which is exactly the intended
"recent AND high-value" composite. TD updates target the hash field, never the ZSET.

## Architectural Impact

- **New dependencies:** none. Uses the existing `base_score_field` mechanism in
  `DecayingSortedField` / `DECAY_SCORE_LUA` and a plain `FloatField` from
  `fields/shortcuts.py`.
- **Interface changes:** `PolicyEntry.expected_value` remains a `DecayingSortedField` but
  becomes a pure recency clock; a new plain numeric field (e.g. `q_value`) holds the
  learned value and is wired as `expected_value`'s `base_score_field`. `update_q_value`,
  `initialize_q_value`, and `TD_UPDATE_LUA` retarget the hash field. Public function
  signatures (`update_q_value(instance, reward, ...) -> float`,
  `initialize_q_value(instance, initial_q)`) stay the same; only their storage target
  changes.
- **Coupling:** decreases — Q storage and decay storage are decoupled.
- **Data ownership:** the model hash now owns the Q-value (single source of truth); the
  ZSET owns only the decay timestamp.
- **Reversibility:** easy. Beta substrate layer; breaking storage-schema change is
  acceptable per the issue, no migration ceremony. Reverting is a field-definition + Lua
  change.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1-2 (confirm the chosen storage shape; see Open Questions)
- Review rounds: 1 (Lua correctness + Valkey compatibility)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli ping` | Test suite + Lua eval against a live server |

Run all checks: `python scripts/check_prerequisites.py docs/plans/policycache-q-value-storage-slot.md`

## Solution

### Key Elements

- **Plain Q-value field on the model hash** — add a `FloatField` (e.g. `q_value`) to
  `PolicyEntry`. The model hash becomes the single source of truth for the learned value;
  `save()`/`touch()` never touch a learned value in a ZSET score slot again.
- **`expected_value` reduced to a pure recency clock** — keep it a `DecayingSortedField`
  but set `base_score_field="q_value"`. Its ZSET score holds only the timestamp; the decay
  Lua already reads the base score from the model hash via `HGET` + `cmsgpack.unpack`.
- **Retargeted TD update** — `TD_UPDATE_LUA` reads/writes the Q-value from the model hash
  (`HGET`/`HSET` with cmsgpack encoding) instead of `ZSCORE`/`ZADD` on the ZSET. TD stays
  atomic (single Lua eval).
- **Removed workaround** — `initialize_q_value` becomes a plain initial write of `q_value`
  into the hash (or is folded into model construction), and the crystallization handler's
  "overrides timestamp" comment/ordering dependency disappears.

### Flow

Crystallization → `PolicyEntry(..., q_value=ci_lower).save()` → ZSET score = timestamp
(decay clock), hash `q_value` = ci_lower → agent acts → `"acted"` touches ZSET (clock
refreshes, **Q untouched**) → outcome observed → `update_q_value(reward)` HSETs new Q into
hash (**timestamp untouched**) → `top_by_decay("expected_value")` ranks by
`q_value × elapsed_days^(−decay_rate)`.

### Technical Approach

- **Encoding contract is load-bearing.** `DECAY_SCORE_LUA` reads the base score via
  `HGET member q_value` then `cmsgpack.unpack`, accepting a plain number or a Decimal
  tagged dict (`decaying_sorted_field.py:67-79`). The new `TD_UPDATE_LUA` MUST write
  `q_value` with `cmsgpack.pack(new_q)` so the decay path can read it back. The existing
  Popoto hash encoding for a `FloatField` is msgpack-per-field; confirm the exact bytes a
  Python-side `instance.q_value = x; instance.save()` produces and match it in Lua so both
  write paths are interchangeable. This is the single highest-risk detail — get it via a
  read of `models/encoding.py` + a round-trip assertion in the regression test.
- `expected_value`'s field declaration gains `base_score_field="q_value"`. No change to
  `DecayingSortedField` itself is required.
- `update_q_value`: change `TD_UPDATE_LUA` to `HGET`/`HSET` on the model hash key
  (`_get_redis_key(instance)`), reading current Q (default 0 if missing), computing the
  TD(0) update, writing it back. Drop `_get_sortedset_key` from this path. The TD formula
  (`Q ← Q + α(r + γ·maxQ′ − Q)`) is unchanged.
- `initialize_q_value`: replace the raw-ZADD with a plain hash write of `q_value`
  (cmsgpack-encoded HSET, or simply set `instance.q_value` and let `save()` persist it).
  Update the docstring to remove the timestamp-override language.
- `crystallization_handler`: pass `q_value=ci_lower` at construction (preferred) so the
  initial Q is persisted by the same `save()` that establishes the decay clock — removing
  the save-then-initialize ordering dependency entirely.
- Verify `composite_score` / `top_by_decay` over `expected_value` now produce sane
  rankings (no 1970-epoch artifacts).

## Failure Path Test Strategy

### Exception Handling Coverage
- `crystallization_handler` wraps `initialize_q_value` in `try/except` that logs a
  warning (`policy_cache.py:509-512`). After the refactor, construction-time `q_value`
  removes this fragile path; if any `try/except` remains around Q initialization, add a
  test asserting the warning fires on a forced failure (e.g. unsaved instance) rather than
  silently swallowing.
- No other `except Exception: pass` blocks are in scope.

### Empty/Invalid Input Handling
- `update_q_value` on an instance whose hash has no `q_value` yet: Lua `HGET` returns nil →
  treat as Q=0 (matches today's `ZSCORE` nil→0 behavior). Add a test for the
  "TD update before any initialize" case.
- Decay Lua already defaults `base_score` to 1.0 when the hash field is missing or
  non-numeric (`decaying_sorted_field.py:67-79`) — add a test that a `PolicyEntry` with no
  `q_value` still decay-ranks (base 1.0) rather than erroring.

### Error State Rendering
- No user-visible UI. The observable "error state" is a nonsensical TD error
  (~−1.7e9) or a 1970-epoch decay rank — the regression tests assert these never occur.

## Test Impact

- [ ] `tests/test_policy_cache.py::TestPolicyCache::test_q_value_update` (line 134) —
  UPDATE: still asserts the TD formula, but storage target moves to the hash; assert
  `q_value` (hash) holds the new Q and the ZSET score is unchanged/absent-of-Q.
- [ ] `tests/test_policy_cache.py::test_q_value_update_requires_save` (line 162) — UPDATE:
  same TD behavior, new storage target.
- [ ] `tests/test_policy_cache.py::test_composite_score_query` (line 277) — UPDATE: the
  query must now reflect `q_value × decay`; values change but ordering semantics hold.
- [ ] `tests/test_policy_cache.py::test_end_to_end` (line 517) — UPDATE: assert Q survives
  the full lifecycle.
- [ ] `tests/test_policy_cache.py::test_crystallization_then_query` (line 560) — UPDATE:
  crystallized entries rank by learned Q, not by 1970-epoch artifact.

The existing hand-computed TD values stay numerically valid (the formula is unchanged);
only the storage assertions change.

## Rabbit Holes

- **Don't modify `DecayingSortedField` or `DECAY_SCORE_LUA`.** The `base_score_field`
  mechanism already does exactly what's needed; changing the field class risks every other
  consumer (Memory, AccessTracker, etc.).
- **Don't introduce a second ZSET for Q-values.** It re-creates a parallel partitioned-key
  scheme and a second source of truth that `top_by_decay` can't read. The hash field is
  strictly simpler and is what the decay Lua already expects.
- **Don't build a migration path.** Beta substrate layer; the issue explicitly permits a
  breaking storage-schema change with no migration ceremony.
- **Don't reimplement the TD formula.** It is correct and test-verified; only its storage
  target changes.

## Risks

### Risk 1: Lua/Python encoding mismatch for `q_value`
**Impact:** If `TD_UPDATE_LUA` writes `q_value` in a format the decay Lua's
`cmsgpack.unpack` (or a Python-side read) can't decode, decay ranking silently falls back
to base 1.0 and the bug appears "half-fixed."
**Mitigation:** Read `models/encoding.py` to confirm the per-field msgpack format; write
the regression test to round-trip Q through BOTH paths — set via `update_q_value` (Lua
HSET), read via Python attribute load AND via `top_by_decay` (Lua HGET) — asserting the
same value.

### Risk 2: Valkey compatibility
**Impact:** A Redis-module command would break Valkey.
**Mitigation:** Only `HGET`/`HSET`/`EVAL`/`cmsgpack` are used (all already used by the
existing decay Lua). No `BF.*`/`CMS.*`/module commands introduced. Confirmed by the local
CI suite, which exercises the Lua paths against local Redis.

### Risk 3: `FloatField` interaction with `save()` HSET filtering
**Impact:** PR #424 changed `base.py` to filter `IndexedFieldMixin` fields out of the
plain HSET mapping. A plain `FloatField` is not an indexed field, so it should be HSET
normally — but this must be verified so `q_value` actually lands in the hash on `save()`.
**Mitigation:** Test that `PolicyEntry(q_value=0.6).save()` then a fresh load returns 0.6
from the hash.

## Race Conditions

### Race 1: Concurrent TD update vs. decay read
**Location:** `policy_cache.py` `update_q_value` (HSET) vs. `query.py` decay read (HGET).
**Trigger:** A TD update HSETs `q_value` while a `top_by_decay` Lua eval reads it.
**Data prerequisite:** `q_value` must exist in the hash before decay reads it; if absent,
the decay Lua defaults to base 1.0 (safe).
**State prerequisite:** none beyond a saved instance.
**Mitigation:** Each TD update is a single atomic Lua eval (HGET+compute+HSET in one
script), so a concurrent decay read sees either the old or the new Q, never a torn value.
This is strictly better than today's `ZSCORE`/`ZADD` (also atomic) and removes the
cross-semantic clobber entirely. No new lock needed.

### Race 2: `save()`/`touch()` vs. TD update
**Location:** `format_value_pre_save` / `touch()` (ZSET) vs. `update_q_value` (hash).
**Trigger:** Interleaved save/touch and TD update.
**Mitigation:** After this change they target **disjoint storage** (ZSET score vs. hash
field), so they cannot race on the same datum — this is the core fix.

## No-Gos (Out of Scope)

- **Storage migration for any pre-existing `PolicyEntry` records.** Tagged rationale:
  beta substrate layer, breaking schema change explicitly permitted by the issue
  ("no migration ceremony needed"). Not deferred to a follow-up — there is deliberately
  nothing to migrate.
- Nothing else deferred — every relevant item (field, Lua, both init paths, tests, docs)
  is in scope for this plan.

## Update System

No update system changes required — this is purely an internal library change to a recipe
module and is shipped via the normal package release.

## Agent Integration

No agent integration required — `PolicyCache` is a Popoto library recipe, not an MCP tool
or bridge component. No `.mcp.json` or bridge changes.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/policy-cache.md` (and `docs/recipes/` if present) to describe
  the Q-value-in-hash + decay-timestamp-in-ZSET split, and that `expected_value` is a
  recency clock whose `base_score_field` is `q_value`.
- [ ] Update any docstrings/examples that show `initialize_q_value` overriding a timestamp.

### External Documentation Site
- [ ] `mkdocs build --strict` passes (run via `scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Rewrite `initialize_q_value` and `update_q_value` docstrings to reflect hash storage.
- [ ] Update the `expected_value` field comment in `PolicyEntry` to note it is a pure decay
  clock with `base_score_field="q_value"`.

## Success Criteria

- [ ] A `PolicyEntry`'s Q-value survives `instance.save()`: write Q=0.6, mutate
  `action_spec`, `save()`, read back Q=0.6 from the hash.
- [ ] A `PolicyEntry`'s Q-value survives `instance.touch("expected_value")` and the
  ObservationProtocol `"acted"` path.
- [ ] `top_by_decay`/`composite_score` over `PolicyEntry` never read a Q as a timestamp;
  ranking reflects `q_value × recency` with no 1970-epoch artifacts.
- [ ] `update_q_value()` after a save/touch produces a TD error from the true prior Q
  (≈[0,1]), never ~−1.7e9; existing hand-computed TD tests still pass.
- [ ] `initialize_q_value`'s timestamp-override workaround is removed (reduced to a plain
  hash write or folded into construction).
- [ ] Regression test added covering save-after-learn AND touch-after-learn (the exact
  untested gap the audit identified).
- [ ] Round-trip test: Q set via `update_q_value` (Lua) reads identically via Python load
  AND via the decay Lua's base-score read.
- [ ] Full suite passes against local Redis (`scripts/ci-local.sh --fast`); no
  Redis-module commands introduced (Valkey compatibility).
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (policy-q-storage)**
  - Name: q-storage-builder
  - Role: Implement the field split, retarget `TD_UPDATE_LUA`/`initialize_q_value` to the
    model hash, wire `base_score_field`, update crystallization handler.
  - Agent Type: builder
  - Resume: true

- **Builder (tests)**
  - Name: q-storage-test-builder
  - Role: Add save-after-learn / touch-after-learn / acted-outcome regression tests and the
    Lua↔Python encoding round-trip test; update affected existing tests.
  - Agent Type: test-engineer
  - Resume: true

- **Validator (policy-q-storage)**
  - Name: q-storage-validator
  - Role: Verify all success criteria, run `scripts/ci-local.sh --fast`, grep for any
    Redis-module commands.
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: q-storage-doc
  - Role: Update feature docs and docstrings.
  - Agent Type: documentarian
  - Resume: true

### Available Agent Types

(See standard roster.)

## Step by Step Tasks

### 1. Confirm field encoding contract
- **Task ID**: build-encoding-recon
- **Depends On**: none
- **Validates**: n/a (recon; output feeds build-core)
- **Assigned To**: q-storage-builder
- **Agent Type**: builder
- **Parallel**: false
- Read `src/popoto/models/encoding.py` to determine the exact per-field msgpack bytes a
  `FloatField` produces in the model hash.
- Confirm the decay Lua's `HGET`+`cmsgpack.unpack` (decaying_sorted_field.py:67-79) decodes
  those bytes; record the exact `cmsgpack.pack` form the TD Lua must emit.

### 2. Implement Q-value storage split
- **Task ID**: build-core
- **Depends On**: build-encoding-recon
- **Validates**: tests/test_policy_cache.py
- **Informed By**: build-encoding-recon (encoding form for TD Lua HSET)
- **Assigned To**: q-storage-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `q_value = FloatField(default=0.0)` (or equivalent plain numeric field) to
  `PolicyEntry`.
- Set `expected_value = DecayingSortedField(partition_by="agent_id", base_score_field="q_value")`.
- Rewrite `TD_UPDATE_LUA` to `HGET`/`HSET` `q_value` on the model hash key (cmsgpack), nil→0
  default; keep it a single atomic eval. Update `update_q_value` to pass the hash key.
- Rewrite `initialize_q_value` to a plain hash write (or fold into construction) and remove
  the timestamp-override docstring.
- Pass `q_value=ci_lower` at construction in `crystallization_handler`; remove the
  save-then-initialize ordering dependency and the stale comment.

### 3. Regression + encoding round-trip tests
- **Task ID**: build-tests
- **Depends On**: build-core
- **Validates**: tests/test_policy_cache.py
- **Assigned To**: q-storage-test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Add: save-after-learn, touch-after-learn, and `"acted"`-outcome-after-learn tests
  asserting Q survives.
- Add: Lua↔Python round-trip test (set via `update_q_value`, read via Python load and via
  `top_by_decay`).
- Add: TD-update-before-initialize (nil→0) and decay-rank-with-missing-`q_value`
  (base 1.0) edge cases.
- Update affected existing tests (see Test Impact).

### 4. Validation
- **Task ID**: validate-core
- **Depends On**: build-tests
- **Assigned To**: q-storage-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `scripts/ci-local.sh --fast`; verify all success criteria.
- `grep -rn 'BF\.\|CMS\.\|TOPK\.\|CF\.' src/popoto/recipes/policy_cache.py` → no module
  commands.

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-core
- **Assigned To**: q-storage-doc
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/policy-cache.md` and docstrings; ensure `mkdocs build --strict`
  passes.

### 6. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: q-storage-validator
- **Agent Type**: validator
- **Parallel**: false
- Re-run full fast CI; confirm all success criteria including docs.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Policy tests pass | `pytest tests/test_policy_cache.py -q` | exit code 0 |
| Full fast suite | `scripts/ci-local.sh --fast` | exit code 0 |
| No Redis-module commands | `grep -rEn 'BF\.|CMS\.|TOPK\.|CF\.' src/popoto/recipes/policy_cache.py` | exit code 1 |
| Workaround removed | `grep -n 'overrides that\|overrides the timestamp\|overrides timestamp' src/popoto/recipes/policy_cache.py` | exit code 1 |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. **Storage shape confirmation:** This plan chooses the hash-field-as-`base_score_field`
   approach (candidate 2 in the issue) over a second ZSET (candidate 1), because the decay
   Lua already reads a base score from the model hash and it gives a single source of truth.
   Any objection to making `expected_value` a pure decay clock with
   `base_score_field="q_value"`?
2. **Field name:** `q_value` is proposed for the new hash field. Acceptable, or prefer a
   name like `learned_value` / `q`?
3. **`initialize_q_value` fate:** Reduce it to a plain hash write (keep the public function
   for API stability) vs. remove it entirely and rely on `q_value=...` at construction.
   Preference?
