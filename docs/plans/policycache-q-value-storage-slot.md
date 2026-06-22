---
status: Planning
type: bug
appetite: Medium
owner: Valor
created: 2026-06-22
tracking: https://github.com/tomcounsell/popoto/issues/410
last_comment_id:
revision_applied: true
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
`initialize_q_value`'s "override the timestamp" workaround is replaced by a plain hash
write (the public function is kept as a thin shim for API stability — it has live callers
in tests and docs — but its body no longer fights the decay clock).

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
  prior *structural* fix has been attempted; PR #239 shipped a first-write-only workaround
  (`initialize_q_value`), analyzed below in "Why Previous Fixes Failed." This is the first
  plan to remove the aliasing.

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
`DecayingSortedField` behavior). The Q-value lives in the model hash as a `DecimalField`
(`__Decimal__` tagged-dict encoding) and is read by the decay Lua as the `base_score_field`
it already supports (the decay Lua already decodes the Decimal tagged dict at
`decaying_sorted_field.py:75`) — so `decayed_rank = sign(Q) × |Q| × elapsed_days^(−decay_rate)`,
which is the intended "recent AND high-value" composite with sign preserved for the
negative Q-values TD legitimately produces (see Risk 4 / the DECAY_SCORE_LUA sign guard).
TD updates target the hash field via `cmsgpack.pack({__Decimal__=true, as_encodable=tostring(new_q)})`,
never the ZSET.

## Architectural Impact

- **New dependencies:** none. Uses the existing `base_score_field` mechanism in
  `DecayingSortedField` / `DECAY_SCORE_LUA` and a `DecimalField` from
  `fields/shortcuts.py`.
- **Q-value field type is `DecimalField`, not `FloatField`** (see Risk 1 / the encoding
  contract). `DecimalField` encodes via the `__Decimal__` tagged dict, which is the **only**
  representation that is byte-identical whether written by Python `msgpack.packb` or Lua
  `cmsgpack.pack` — making the TD-Lua write path and the Python load path truly
  interchangeable. A `FloatField` would NOT be interchangeable (cmsgpack compacts whole
  numbers to msgpack ints and fractions to float32, so a Lua-written Q loads back into
  Python as `int` or a precision-truncated `float`, corrupting the field).
- **Interface changes:** `PolicyEntry.expected_value` remains a `DecayingSortedField` but
  becomes a pure recency clock; a new `q_value = DecimalField(default=Decimal("0"))` holds
  the learned value and is wired as `expected_value`'s `base_score_field`. `update_q_value`,
  `initialize_q_value`, and `TD_UPDATE_LUA` retarget the hash field. **Both public function
  signatures are kept and unchanged** (`update_q_value(instance, reward, ...) -> float`,
  `initialize_q_value(instance, initial_q)`); `initialize_q_value` stays a thin public shim
  (it has live callers in `tests/test_guide_examples.py`,
  `tests/test_policy_cache.py`, `docs/guides/policy-cache-recipe.md`, and
  `docs/features/agent-memory.md` — removing it is a breaking API change with no benefit).
  Only the storage target of each function changes (ZSET score → model-hash `q_value`).
- **Coupling:** the cross-semantic *write* collision is eliminated. A new, weaker *read*
  dependency replaces it: the decay query now reads `q_value` from the companion hash field
  (with a silent 1.0 fallback if absent/mis-encoded — see "Residual Coupling" below). This
  is a strict improvement (the old failure clobbered learned data; the new failure degrades
  ranking to a sane default) but it is a real new failure surface, guarded by tests.
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

- **`DecimalField` Q-value on the model hash** — add `q_value = DecimalField(default=Decimal("0"))`
  to `PolicyEntry`. The model hash becomes the single source of truth for the learned value;
  `save()`/`touch()` never touch a learned value in a ZSET score slot again. `DecimalField`
  (not `FloatField`) is chosen because its `__Decimal__` tagged-dict encoding is the only
  representation byte-identical across the Python and Lua write paths (Risk 1).
- **`expected_value` reduced to a pure recency clock** — keep it a `DecayingSortedField`
  but set `base_score_field="q_value"`. Its ZSET score holds only the timestamp; the decay
  Lua already reads the base score from the model hash via `HGET` + `cmsgpack.unpack`,
  including the `__Decimal__` tagged-dict branch (`decaying_sorted_field.py:75`).
- **Retargeted TD update** — `TD_UPDATE_LUA` reads/writes the Q-value from the model hash
  (`HGET`/`HSET`) instead of `ZSCORE`/`ZADD` on the ZSET, writing the Q as a `__Decimal__`
  tagged dict. TD stays atomic (single Lua eval).
- **Sign-guarded decay** — add a minimal, additive sign-preserving guard to
  `DECAY_SCORE_LUA` so negative/zero Q-values (which TD produces) rank correctly instead of
  inverting (Risk 4). This is the one deliberate exception to the "don't touch
  DECAY_SCORE_LUA" rabbit hole; it is backward-compatible (positive bases are unchanged) and
  reconciled below.
- **Retargeted workaround** — `initialize_q_value` is kept as a thin public shim but its
  body changes from a raw timestamp-overriding `ZADD` to a plain `q_value` hash write
  (`__Decimal__`-encoded HSET on the model hash). The crystallization handler additionally
  passes `q_value=ci_lower` at construction so the initial Q is persisted by the same
  `save()` that establishes the decay clock — this is the canonical init path; the shim
  remains for direct callers and API stability.

### Flow

Crystallization → `PolicyEntry(..., q_value=ci_lower).save()` → ZSET score = timestamp
(decay clock), hash `q_value` = ci_lower → agent acts → `"acted"` touches ZSET (clock
refreshes, **Q untouched**) → outcome observed → `update_q_value(reward)` HSETs new Q into
hash (**timestamp untouched**) → `top_by_decay("expected_value")` ranks by
`q_value × elapsed_days^(−decay_rate)`.

### Technical Approach

- **Encoding contract is load-bearing — use the `__Decimal__` tagged dict, NOT a bare float.**
  `DECAY_SCORE_LUA` reads the base score via `HGET member q_value` then `cmsgpack.unpack`,
  accepting a plain number OR a Decimal tagged dict (`decaying_sorted_field.py:67-79`,
  tagged-dict branch at :75). Empirically verified against the project's live Redis build
  (Lua `cmsgpack`):
  - `cmsgpack.pack(0.0)` → `0x00` (msgpack **int** 0); `cmsgpack.pack(5.0)` → `0x05` (int);
    `cmsgpack.pack(0.25)` → `0xca…` (**float32**); `cmsgpack.pack(-1.5)` → `0xca…` (float32).
  - Python `msgpack.packb(5.0)` → `0xcb…` (**float64**) for all floats.
  - Therefore a `FloatField` is NOT interchangeable: a whole-number Q written by Lua loads
    into Python as `int` (type corruption of the field); a fractional Q loads as a
    float32-truncated value (precision loss / byte mismatch vs. Python's float64).
  - The `__Decimal__` tagged dict (`{"__Decimal__": True, "as_encodable": "<str>"}`) packs
    **byte-identically** from both sides (verified: both produce
    `82ab5f5f446563696d616c5f5f…`), and Python's decoder (`encoding.py:75`) reconstructs a
    `Decimal`. This is the chosen representation. The TD Lua MUST write
    `cmsgpack.pack({['__Decimal__']=true, ['as_encodable']=tostring(new_q)})`.
  - `q_value` is declared `DecimalField` so the Python write path (`save()`) produces the
    same tagged dict via `TYPE_ENCODER_DECODERS[Decimal]` (`encoding.py:259,72-76`).
- `expected_value`'s field declaration gains `base_score_field="q_value"`.
- **`DECAY_SCORE_LUA` sign guard (the one approved exception to the rabbit hole).** After
  decoding `base_score`, before `math.pow`, replace
  `local decayed = base_score * math.pow(elapsed_days, -decay_rate)` with:
  ```lua
  local sign = base_score < 0 and -1 or 1
  local mag = math.abs(base_score)
  local decayed = sign * mag * math.pow(elapsed_days, -decay_rate)
  ```
  This is additive and backward-compatible: for the existing positive-base consumers
  (Memory `strength`, AccessTracker, etc., all ≥ 0) `sign = 1` and `mag = base_score`, so
  the computed value is bitwise unchanged. It only affects the new signed `q_value` base.
  Zero base still yields 0 (rank floor), which is the correct "no learned value" behavior.
  `cyclic_decay_field.py` carries an independent copy of the decay Lua but is NOT used by
  PolicyEntry and is out of scope; it is left unchanged.
- `update_q_value`: change `TD_UPDATE_LUA` to `HGET`/`HSET` on the model hash key
  (`_get_redis_key(instance)`), reading current Q (decode the tagged dict / plain number,
  default 0 if missing), computing the TD(0) update, writing it back as the `__Decimal__`
  tagged dict. Drop `_get_sortedset_key` from this path. The TD formula
  (`Q ← Q + α(r + γ·maxQ′ − Q)`) is unchanged. Returns the same `td_error` string.
- `initialize_q_value`: keep the public signature; replace the raw-ZADD body with an
  `__Decimal__`-encoded HSET of `q_value` on the model hash (or set `instance.q_value` and
  `save()`). Rewrite the docstring to remove the timestamp-override language.
- `crystallization_handler`: pass `q_value=Decimal(str(ci_lower))` at construction (the
  canonical init path) so the initial Q is persisted by the same `save()` that establishes
  the decay clock — removing the save-then-initialize ordering dependency. The
  `initialize_q_value` call after `save()` is dropped here (the value is already set at
  construction); the shim remains available for direct callers.
- **Ordering contract (resolves the CONCERN about partial hashes):** the canonical path is
  construct-with-`q_value` → `save()` (single HSET writes the full hash including
  `q_value`). `update_q_value` is only ever called on a saved instance (it raises on
  unsaved, via `_get_redis_key`). A bare `HSET q_value` from the TD Lua against an
  already-saved instance updates a single field and never reconstructs/clears the rest of
  the hash, so it cannot leave an unreconstructable row. There is no path that TD-updates
  before `save()`. See Race Conditions for interleaving tests in both orders.
- Verify `composite_score` / `top_by_decay` over `expected_value` now produce sane
  rankings (no 1970-epoch artifacts; negative Q ranks below positive Q; the derived rank
  reflects the stored `q_value`, not the 1.0 fallback).

## Failure Path Test Strategy

### Exception Handling Coverage
- `crystallization_handler` currently wraps `initialize_q_value` in a `try/except` that logs
  a warning (`policy_cache.py:509-512`). The refactor sets `q_value` at construction and
  **removes that try/except and the post-save `initialize_q_value` call entirely** — the
  initial Q is now persisted by the same `save()`, so there is no separate fragile step to
  guard. The build must delete this block (not leave it as dead defensive code).
- No other `except Exception: pass` blocks are in scope.

### Empty/Invalid Input Handling
- `update_q_value` on an instance whose hash has no `q_value` yet: Lua `HGET` returns nil →
  treat as Q=0 (matches today's `ZSCORE` nil→0 behavior). Add a test for the
  "TD update before any initialize" case.
- Decay Lua defaults `base_score` to 1.0 when the hash field is missing or
  non-numeric (`decaying_sorted_field.py:67-79`) — add a test that a `PolicyEntry` with no
  `q_value` still decay-ranks (base 1.0) rather than erroring.
- **Negative/zero base.** Add a test that a `PolicyEntry` with a negative `q_value` (e.g.
  `Decimal("-0.4")`) ranks **below** one with a positive `q_value` of equal recency, and
  that a zero `q_value` ranks at the floor — proving the sign guard works and decay is not
  inverted.

### Residual Coupling / New Failure Mode (explicit)
The fix converts a loud cross-semantic *write* collision into a quieter *read* dependency:
`top_by_decay` now reads `q_value` from the companion hash field, falling back **silently**
to base 1.0 when `q_value` is absent or mis-encoded (`decaying_sorted_field.py:67-79`). A
mis-encoded Q would therefore make a policy rank as if it had base 1.0 rather than erroring.
This is mitigated by (a) the `DecimalField`/`__Decimal__` byte-identical encoding contract
(Risk 1) that prevents mis-encoding, and (b) an explicit success-criterion test asserting
the decayed rank **derives from the stored `q_value`, not the 1.0 fallback**, immediately
after save (e.g. a policy with `q_value=0.6` and one with `q_value=0.2` of equal recency
must rank in that order — impossible if both silently fell back to 1.0).

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

- **Don't modify the `DecayingSortedField` class or the `base_score_field` mechanism.** The
  mechanism already does exactly what's needed; changing the field class risks every other
  consumer (Memory, AccessTracker, etc.).
- **One narrow, approved exception: a sign guard in `DECAY_SCORE_LUA`.** The original rabbit
  hole said "don't touch `DECAY_SCORE_LUA`" on the assumption that base scores are always
  positive. That assumption no longer holds once a signed Q-value becomes the base — TD
  learning legitimately produces negative Q, and `base * elapsed^(−decay_rate)` with a
  negative base *inverts* decay (a negative score rises toward zero as it ages). We
  therefore **do** make a single, minimal, additive change (the `sign`/`mag` guard in
  Technical Approach / Risk 4) that is provably backward-compatible: for every existing
  positive-base consumer the computed value is unchanged. This is a deliberate, scoped
  reconciliation of the tension the critique flagged — not an open license to refactor the
  decay Lua. A negative-base ranking test guards it. (`cyclic_decay_field.py`'s separate
  decay-Lua copy is out of scope and untouched — PolicyEntry does not use it.)
- **Don't introduce a second ZSET for Q-values.** It re-creates a parallel partitioned-key
  scheme and a second source of truth that `top_by_decay` can't read. The hash field is
  strictly simpler and is what the decay Lua already expects.
- **Don't build a migration path.** Beta substrate layer; the issue explicitly permits a
  breaking storage-schema change with no migration ceremony.
- **Don't reimplement the TD formula.** It is correct and test-verified; only its storage
  target changes.

## Risks

### Risk 1: Lua/Python encoding mismatch for `q_value` (RESOLVED in design)
**Impact:** Lua `cmsgpack.pack` and Python `msgpack.packb` are NOT byte-interchangeable for
bare numbers (empirically verified against the project's live Redis): `cmsgpack.pack(5.0)`→
`0x05` (msgpack int), `cmsgpack.pack(0.25)`→`0xca…` (float32), `cmsgpack.pack(-1.5)`→float32;
Python writes float64 (`0xcb…`) for every float. A `FloatField` Q written by the TD Lua
would load into Python as `int` (whole numbers) or a float32-truncated value, corrupting the
field type and breaking the two-write-path interchange the fix depends on.
**Resolution (baked into the design, not just mitigated):** store `q_value` as a
`DecimalField`, and have the TD Lua write it as the `__Decimal__` tagged dict
(`{['__Decimal__']=true, ['as_encodable']=tostring(new_q)}`). This representation packs
byte-identically from both Lua `cmsgpack` and Python `msgpack` (verified:
`82ab5f5f446563696d616c5f5f…` from both), the decay Lua already decodes it
(`decaying_sorted_field.py:75`), and Python reconstructs a `Decimal` (`encoding.py:75`).
**Test:** round-trip Q through BOTH paths — set via `update_q_value` (Lua HSET), read via
Python attribute load AND via `top_by_decay` (Lua HGET) — asserting equal value AND that the
Python-loaded type is `Decimal` (not `int`/`float`). Exercise `0.0`, `5.0`, `-1.5`, `0.25`.
Run against the project's actual Redis/Valkey build before shipping.

### Risk 2: Valkey compatibility
**Impact:** A Redis-module command would break Valkey.
**Mitigation:** Only `HGET`/`HSET`/`EVAL`/`cmsgpack` are used (all already used by the
existing decay Lua). No `BF.*`/`CMS.*`/module commands introduced. Confirmed by the local
CI suite, which exercises the Lua paths against local Redis.

### Risk 3: `DecimalField` interaction with `save()` HSET filtering
**Impact:** PR #424 changed `base.py` to filter `IndexedFieldMixin` fields out of the
plain HSET mapping. A `DecimalField` is not an indexed field, so it should be HSET
normally — but this must be verified so `q_value` actually lands in the hash on `save()`.
**Mitigation:** Test that `PolicyEntry(q_value=Decimal("0.6")).save()` then a fresh load
returns `Decimal("0.6")` from the hash (the same value the decay Lua reads as base score).

### Risk 4: Negative/zero Q-values invert or freeze decay rank
**Impact:** TD learning produces negative Q (`new_q = current_q + alpha*td_error`, and
`td_error` can be negative). Feeding a negative base into `base * elapsed^(−decay_rate)`
makes it rise toward zero as it ages (inverting recency); a zero base freezes the rank at 0.
`q_value` was never a decay base before, so this is a genuinely new failure mode.
**Mitigation:** the sign-preserving guard in `DECAY_SCORE_LUA` (see Technical Approach /
Rabbit Holes): `decayed = sign(base) * |base| * elapsed^(−decay_rate)`. Backward-compatible
for all existing positive-base consumers (value bitwise unchanged). Guarded by a
negative-base ranking test (negative Q ranks below positive Q of equal recency; zero ranks
at floor).

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

### Race 2: `save()`/`touch()` vs. TD update on the ZSET
**Location:** `format_value_pre_save` / `touch()` (ZSET score) vs. `update_q_value` (hash
field).
**Trigger:** Interleaved save/touch and TD update.
**Mitigation:** `touch()` and the decay-clock write target the **ZSET score**, while TD
targets the **hash `q_value` field** — disjoint storage, so they cannot race on the same
datum. This is the core fix.

### Race 3: `save()` HSET vs. TD update on the same hash key
**Location:** `Model.save()` (full-hash HSET) vs. `update_q_value` (single-field HSET on
`q_value`).
**Trigger:** A `save()` that rewrites the whole model hash interleaves with a TD update.
**Concern (from critique):** TD and save now share the model hash, so the "fully disjoint
storage" claim is narrower than at first stated — a `save()` rewrites `q_value` to the
in-memory value, which could clobber a concurrent/recent TD update if the instance's
in-memory `q_value` is stale.
**Resolution — ordering contract:**
- The canonical lifecycle is `construct(q_value=…)` → `save()` (writes the full hash
  including the correct initial Q) → zero-or-more `update_q_value()` calls (single-field
  HSETs). After the initial save, the recipe never re-`save()`s a stale `q_value`: TD
  updates go straight to the hash field and are read back fresh.
- The crystallization handler no longer calls `initialize_q_value` after `save()` (Q is set
  at construction), so there is no save-then-overwrite step at all in the canonical path.
- A later full `save()` (e.g. mutating `action_spec`) on a freshly-loaded instance carries
  the persisted `q_value` (it is loaded from the hash), so re-saving does not reset it —
  unless the caller saved a long-held stale instance, which is a general Popoto
  last-writer-wins property, not specific to this change.
**Tests (both orders):**
- `td_update → save(unrelated field) → assert q_value survives` (reload reads the TD value,
  because the saved instance carried the loaded `q_value`).
- `save → td_update → save(unrelated field) → assert the second save does not reset
  q_value` (instance reloaded before the second save).
- `td_update before save` is asserted to be impossible (raises on unsaved instance).

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
- [ ] `initialize_q_value` is **kept as a public function with its existing signature**; its
  body is rewritten from the timestamp-overriding `ZADD` to a plain `__Decimal__`-encoded
  `q_value` hash write. (It is NOT removed — it has live callers in tests and docs.) The
  crystallization handler no longer calls it (Q set at construction).
- [ ] Negative/zero Q-values rank correctly: a negative `q_value` ranks below a positive one
  of equal recency; a zero `q_value` ranks at the floor — proving the `DECAY_SCORE_LUA` sign
  guard. Existing positive-base consumers' decay output is unchanged (regression-checked).
- [ ] The decayed rank derives from the stored `q_value`, not the silent 1.0 fallback:
  two equal-recency policies with `q_value` 0.6 vs 0.2 rank in that order immediately after
  save.
- [ ] Encoding round-trip test: Q set via `update_q_value` (Lua) reads **identically** via
  Python load AND via the decay Lua's base-score read, AND the Python-loaded type is
  `Decimal` (not `int`/`float`); covers `0.0`, `5.0`, `-1.5`, `0.25`.
- [ ] Regression test added covering save-after-learn AND touch-after-learn (the exact
  untested gap the audit identified), plus the two save/TD interleaving orders (Race 3).
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
- Confirm (already verified in this plan) that a `DecimalField` encodes via the `__Decimal__`
  tagged dict in `encoding.py` (lines 259, 72-76) and that Lua `cmsgpack.pack` of the same
  tagged dict is byte-identical. Re-run the byte check against the project's Redis/Valkey
  build if in doubt; bare-float encodings are NOT interchangeable (do not use `FloatField`).
- Record the exact `cmsgpack.pack({['__Decimal__']=true, ['as_encodable']=tostring(new_q)})`
  form the TD Lua must emit, matching the decay Lua's decode branch at
  `decaying_sorted_field.py:75`.

### 2. Implement Q-value storage split
- **Task ID**: build-core
- **Depends On**: build-encoding-recon
- **Validates**: tests/test_policy_cache.py
- **Informed By**: build-encoding-recon (encoding form for TD Lua HSET)
- **Assigned To**: q-storage-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `q_value = DecimalField(default=Decimal("0"))` to `PolicyEntry` (DecimalField, NOT
  FloatField — see encoding contract).
- Set `expected_value = DecayingSortedField(partition_by="agent_id", base_score_field="q_value")`.
- Add the sign-preserving guard to `DECAY_SCORE_LUA` (`sign`/`mag` before `math.pow`); verify
  positive-base output is unchanged. Leave `cyclic_decay_field.py` untouched (out of scope).
- Rewrite `TD_UPDATE_LUA` to `HGET`/`HSET` `q_value` on the model hash key, decoding the
  current Q (tagged dict or plain number, nil→0) and writing the new Q as the `__Decimal__`
  tagged dict; keep it a single atomic eval. Update `update_q_value` to pass the model-hash
  key and drop `_get_sortedset_key` from this path.
- Rewrite `initialize_q_value` body to a plain `__Decimal__`-encoded `q_value` hash write;
  keep the public signature; update the docstring to drop the timestamp-override language.
- Pass `q_value=Decimal(str(ci_lower))` at construction in `crystallization_handler`; delete
  the post-save `initialize_q_value` call, its surrounding `try/except`, and the stale
  "overrides timestamp" comment.

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
  `top_by_decay`) asserting equal value AND Python type `Decimal` for `0.0, 5.0, -1.5, 0.25`.
- Add: negative-base ranking test (negative Q < positive Q of equal recency; zero at floor)
  AND a positive-base regression assertion that existing decay output is unchanged.
- Add: rank-derives-from-stored-Q test (0.6 vs 0.2 of equal recency rank in order, proving
  no silent 1.0 fallback).
- Add: Race-3 interleaving tests in both orders (td→save→survives; save→td→save→no-reset).
- Add: TD-update-before-initialize (nil→0) and decay-rank-with-missing-`q_value`
  (base 1.0) edge cases.
- Update affected existing tests (see Test Impact); `initialize_q_value` callers keep
  working (signature unchanged), now asserting the hash holds the Q rather than the ZSET.

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
| Timestamp-override gone | `grep -n 'overrides that\|overrides the timestamp\|overrides timestamp' src/popoto/recipes/policy_cache.py` | exit code 1 |
| `initialize_q_value` kept (shim) | `grep -n 'def initialize_q_value' src/popoto/recipes/policy_cache.py` | exit code 0 |
| TD Lua writes Decimal tagged dict | `grep -n '__Decimal__' src/popoto/recipes/policy_cache.py` | exit code 0 |
| Decay Lua sign guard present | `grep -n 'math.abs' src/popoto/fields/decaying_sorted_field.py` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room) 2026-06-22. FULL depth (3 critics). -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Risk & Robustness | Lua `cmsgpack.pack` and Python `msgpack.packb` are NOT byte-interchangeable for `q_value`: empirically `cmsgpack.pack(0.0)`→`0x00` (msgpack int), `cmsgpack.pack(5.0)`→`0x05` (int), `cmsgpack.pack(-1.5)`→`0xca` (float32), while Python writes float64 (`0xcb`) for all. Whole-number Q-values written by TD Lua load back into Python as `int`, corrupting the FloatField type; the two write paths are not interchangeable as the plan assumes. | RESOLVED — Risk 1, Architectural Impact, Technical Approach, Decision 4 | q_value is now a `DecimalField`; TD Lua writes the `__Decimal__` tagged dict `{['__Decimal__']=true, ['as_encodable']=tostring(new_q)}`, which packs byte-identically from Lua and Python (verified live: `82ab5f5f446563696d616c5f5f…` from both) and decodes to `Decimal` on both sides. Round-trip test asserts value AND `Decimal` type for 0.0/5.0/-1.5/0.25. Re-verified empirically against the project's live Redis during this revision. |
| BLOCKER | Risk & Robustness | Negative/zero Q-values (TD learning produces them: `new_q = current_q + alpha*td_error`, td_error can be negative) feed the decay multiply `base_score * elapsed^(-decay_rate)` with no sign guard. A negative base rises toward zero as time passes (inverting decay); a zero base freezes rank. New failure mode — q_value was never the decay base before. | RESOLVED — Risk 4, Technical Approach, Rabbit Holes, Decision 5 | Added the sign-preserving guard `sign=base<0 and -1 or 1; mag=math.abs(base); decayed=sign*mag*math.pow(...)` to DECAY_SCORE_LUA. Rabbit Holes now explicitly reconciles the tension: this is the ONE approved, additive, backward-compatible exception (positive-base output bitwise unchanged; cyclic_decay_field.py untouched & out of scope). Negative-base ranking test + positive-base regression assertion added. |
| BLOCKER | History & Consistency | `initialize_q_value`'s fate is self-contradictory across four sections: Solution says "fold into construction" (removes the call), Architectural Impact says "public signature stays the same" (keeps it), Success Criteria straddles both, Open Question 3 leaves it unresolved. A builder cannot satisfy both. | RESOLVED — Desired Outcome, Architectural Impact, Solution, Success Criteria, Decision 3 | Single consistent stance across ALL sections: KEEP the public function and signature; rewrite its body to a plain `__Decimal__`-encoded hash write; the crystallization handler sets Q at construction and no longer calls it. Callers audited: live in test_guide_examples.py, test_policy_cache.py, docs/guides/policy-cache-recipe.md, docs/features/agent-memory.md — so removal is rejected. "Fold into construction" language removed from Solution. |
| CONCERN | Risk & Robustness | TD update before `save()` creates a partial model hash containing only `q_value`; a later `crystallization_handler.save()` (policy_cache.py:499-512) may clobber q_value back to ci_lower/default, or save() field-filtering may leave an unreconstructable hash. The "disjoint storage" claim no longer holds once TD and save share the model hash. | RESOLVED — Race 3, Technical Approach ordering contract | Defined the ordering contract: canonical path is construct(q_value)→save()→update_q_value (single-field HSET, never reconstructs the hash); update_q_value raises on unsaved so TD-before-save is impossible; crystallization no longer save-then-overwrites. Both-order interleaving tests added (td→save→survives; save→td→save→no-reset). "Disjoint storage" claim narrowed to ZSET-vs-hash and the shared-hash case addressed explicitly. |
| CONCERN | History & Consistency | Root cause is relocated, not eliminated: the write-collision becomes a read-dependency. Decay now reads base score from the q_value companion field with a silent 1.0 fallback when absent/mis-encoded (decaying_sorted_field.py:10-15). Old failure was loud (clobbered score); new failure is silent (decay reads default). Plan claims to remove coupling but doesn't call out or guard this new silent failure surface. | RESOLVED — "Residual Coupling / New Failure Mode" note, Architectural Impact coupling note, Success Criteria | Added an explicit Residual-Coupling section acknowledging the loud-write→silent-read tradeoff, mitigated by the byte-identical encoding contract and a success-criterion test asserting the rank derives from the stored q_value (0.6 vs 0.2 ordering) not the 1.0 fallback. DecimalField default is `Decimal("0")` (documented as the intended uninitialized base). |
| CONCERN | Scope & Value | All 3 Open Questions are framed as gating PM check-ins but none is a material human blocker. | RESOLVED — "Resolved Decisions" section | Open Questions converted to "Resolved Decisions" with rationale; none gates build. Field name & initialize fate folded into builder discretion with stated defaults. |
| NIT | Scope & Value | Failure Path Test Strategy hedges with "if any try/except remains around Q initialization, add a test" — but the chosen design (q_value at construction) deliberately removes that path. | RESOLVED — Failure Path Test Strategy | Made unconditional: the post-save initialize call and its try/except are deleted; the build must remove the block rather than guard it. |
| NIT | History & Consistency | Prior Art says "No prior fix attempted" but the later "Why Previous Fixes Failed" section analyzes PR #239's `initialize_q_value` timestamp-override as exactly such an attempt — self-contradictory wording. | RESOLVED — Prior Art | Reworded to "No prior structural fix attempted; PR #239 shipped a first-write-only workaround (initialize_q_value), analyzed below." |

---

## Resolved Decisions

These were Open Questions in the first draft. None is a material human blocker — each is
decided with rationale below, so build can start without a PM sign-off gate. Any can still
be overridden in review.

1. **Storage shape — DECIDED:** hash-field-as-`base_score_field` (candidate 2), not a second
   ZSET (candidate 1). The decay Lua already reads a base score from the model hash; this
   gives a single source of truth and avoids a parallel partitioned-key scheme `top_by_decay`
   can't read. `expected_value` becomes a pure decay clock with `base_score_field="q_value"`.
2. **Field name — DECIDED:** `q_value` (builder discretion to rename if a strong convention
   exists; default is `q_value`). Cosmetic.
3. **`initialize_q_value` fate — DECIDED:** keep the public function with its existing
   signature; rewrite the body to a plain `__Decimal__`-encoded `q_value` hash write. Do NOT
   remove it — it has live callers in `tests/test_guide_examples.py`,
   `tests/test_policy_cache.py`, `docs/guides/policy-cache-recipe.md`, and
   `docs/features/agent-memory.md`. The crystallization handler sets Q at construction and no
   longer calls it.
4. **Q-value field type — DECIDED:** `DecimalField` (not `FloatField`), because only the
   `__Decimal__` tagged-dict encoding is byte-interchangeable between the Lua and Python
   write paths (empirically verified). See Risk 1.
5. **DECAY_SCORE_LUA sign guard — DECIDED:** add the minimal additive sign guard despite the
   original rabbit hole, reconciled in Rabbit Holes / Risk 4. Backward-compatible for all
   positive-base consumers; required because Q can be negative.
