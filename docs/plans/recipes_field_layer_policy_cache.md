---
status: Planning
type: chore
appetite: Small
created: 2026-09-06
tracking: https://github.com/tomcounsell/popoto/issues/647
---

# Recipes through the field layer, PR 3: `policy_cache`

## Problem

`src/popoto/recipes/policy_cache.py` is the sharpest instance of the #630
bypass: it does not merely call the shared client, it **owns a Lua script**.
At `bb8ff588` the sites are:

| # | Line | What | What it is asking |
|---|---|---|---|
| 1 | 72 | `from ..redis_db import POPOTO_REDIS_DB, run_lua` | direct handle on the global client and the raw eval helper |
| 2 | 132-172 | the `TD_UPDATE_LUA` script constant | a recipe-owned Lua script that `HGET`s and `HSET`s the `q_value` field of the model hash and hand-rolls Popoto's `__Decimal__` cmsgpack wire format (lines 154, 169-170) |
| 3 | 377-386 | `run_lua(POPOTO_REDIS_DB, TD_UPDATE_LUA, 1, redis_key, ...)` in `update_q_value()` | "atomically apply a TD(0) update to this instance's Q-value" |
| 4 | 416-424 | `_get_redis_key(instance)` reading `instance._redis_key` / `instance.db_key.redis_key` | extract the raw hash key string to hand to `KEYS[1]` |

Two distinct defects, not one:

- **Encoding duplication.** Line 169 reproduces Popoto's internal Decimal
  encoding (`{['__Decimal__']=true, ['as_encodable']=tostring(new_q)}`) inside
  a recipe. That format is owned by the serialization layer. If it ever
  changes, a recipe silently writes values that `DecayingSortedField`'s
  `base_score_field` reader (`decaying_sorted_field.py:149-160`) decodes as its
  `1.0` fallback — with no error raised, because the guard there is
  `type(decoded) == 'number'` or a table carrying `as_encodable`, not a
  field-aware decoder.
- **Layer inversion.** Every other Lua script in the repo is owned by a field
  or a mixin and invoked through a method on that owner:
  `ConfidenceField.update_confidence`, `ValidityField.execute_supersede`,
  `IndexedFieldMixin`, `CoOccurrenceField`, `ExistenceFilter`,
  `PredictionLedgerMixin`, and `Model` itself. `TD_UPDATE_LUA` is the only Lua
  script in the codebase owned by a bare module-level function in `recipes/`.
  Recipes are meant to be *compositions of* primitives; this one is a
  primitive wearing a recipe's clothes.

Consequence: the recipe is a direct consumer of the Redis client object — the
coupling that blocks the storage-backend seam #630 is a prerequisite for — and
it is why `update_q_value` alone among these operations has no pipeline
support and no typed-error remap.

## Freshness Check

**Disposition: Unchanged.** Baseline `bb8ff588` (branch point from
`origin/main`, 2026-09-06).

- Issue #647 was filed 2026-09-06T11:15Z, minutes before this plan.
  `git log --since` on `src/popoto/recipes/policy_cache.py` returns nothing —
  the file has not moved since the issue was written. Its last substantive
  change is `16aa702e` (PR #594).
- Parent #630's inventory (taken at `85c9faa`, 2026-09-04) names one site in
  this recipe. Re-verified by hand at HEAD: one `run_lua` call, one owned
  script, one client import, one raw-key helper. Still exactly that.
- The two sibling PRs that set the pattern are merged and readable on main:
  **PR #634** (`default_memory`) and **PR #644** (`provenance_journal`, merged
  2026-09-06T11:14Z).
- Sibling in flight: **#646** (`graph_traversal`) has a plan committed on main
  at this same baseline (`docs/plans/recipes_field_layer_graph_traversal.md`).
  Different recipe file, and it adds nothing under `src/popoto/fields/`. No
  overlap; coordination signal only.
- No other active plan in `docs/plans/` touches `policy_cache.py`, `q_value`,
  or `DecayingSortedField.base_score_field`.

## Prior Art

- **PR #634 / `docs/plans/recipes_field_layer_default_memory.md`** — #630 PR 1.
  Established the series' rules: new methods resolve the client at call time so
  test spies keep intercepting; parity argued from the *command sequence*, not
  from a green suite alone.
- **PR #644 / `docs/plans/recipes_field_layer_provenance_journal.md`** — #630
  PR 2, merged today. Source of the base-vs-branch command-capture technique
  this plan reuses, and of the "re-read every comment adjacent to a swapped
  line" rule.
- **PR #427 (#410) `4fa3e097`** — the reason `q_value` is a separate storage
  slot at all. It split the Q-value out of the `DecayingSortedField` sorted-set
  score after the two writes were found to clobber each other. The regression
  tests it added (`TestQValueStorageSlotSeparation`,
  `tests/test_policy_cache.py:926`) are the primary oracle for this PR: they
  assert the Q survives `save()`, `touch()`, and an "acted" outcome.
- **PR #601 (#588) / `ValidityField.execute_supersede`**
  (`validity_field.py:897-1022`) — the canonical shape for a field classmethod
  wrapping a Lua script: field resolves the keys, pipeline branch queues and
  returns, non-pipeline branch remaps `ResponseError` through `map_lua_error`,
  return value decoded by the field.
- **`ConfidenceField.update_confidence`** (`confidence_field.py:495-587`) — the
  closest structural twin to what this PR builds: a classmethod that reads
  tuning constants off the field instance, runs one script, decodes a float,
  and syncs the new value back onto the Python instance.

### Why previous fixes did not cover this

PR #427 fixed *which slot* the Q-value lives in. It never questioned *who owns
the writer*, so the Lua stayed in the recipe. #630 is the first issue to name
ownership as the defect.

## Research

No external research. Purely internal refactor: no new dependencies, no
external API, no ecosystem pattern involved.

Valkey compatibility is already settled in-repo and this PR must not disturb
it: `grep -rnE '"\s*(BF|CMS|TOPK|TS|JSON)\.' src/popoto` returns nothing, and
`validity_field.py:30-33` / `existence_filter.py:33-35` carry the "core
commands only" declarations. `TD_UPDATE_LUA` uses `HGET`/`HSET` plus
`cmsgpack` only; moving the script text verbatim keeps that property by
construction.

## Spike Results

### spike-1: Is there any existing field/model helper that reads or writes a single field on the model hash?

- **Assumption**: `Model` or `DecimalField` already offers a single-field
  read/write that the recipe could have called instead of Lua.
- **Method**: code-read.
- **Result**: **False — none exists.** `DecimalField`
  (`fields/shortcuts.py:115-144`) is a bare `Field` subclass that only sets
  `type=Decimal`; it defines no accessors and relies entirely on the bulk
  `HSET`/`HGETALL` save/load path in `models/base.py`. The only single-field,
  non-full-save accessor on `Model` is `touch()` (`models/base.py:2389-2419`),
  which type-checks for `DecayingSortedField` and operates on the ZSET score,
  explicitly *not* the model hash. So the refactor cannot be a swap onto an
  existing method — new API is required, exactly as #647 anticipates.
- **Confidence**: high.
- **Impact if false**: the plan would collapse to a two-line call-site swap.

### spike-2: Where does the repo put a Lua script that a field owns?

- **Assumption**: field-owned scripts are class attributes.
- **Method**: code-read across `src/popoto/fields/` and `src/popoto/models/`.
- **Result**: **Refined.** Every one of the ~18 Lua constants in the repo is a
  **module-level** `NAME_LUA = ...` string in the owning field's module, with a
  KEYS/ARGV contract comment block directly above it, invoked through a method
  on the owning class via `run_lua`. Not one is a class attribute. Examples:
  `SUPERSEDE_LUA` (`validity_field.py:301`),
  `CAPPED_BAYESIAN_UPDATE_LUA` (`confidence_field.py:63`),
  `INDEX_SWAP_LUA` (`indexed_field_mixin.py:128`),
  `DECAY_SCORE_LUA` (`decaying_sorted_field.py:75`),
  `PURGE_ORPHAN_LUA` (`models/base.py:75`).
- **Confidence**: high.
- **Impact if false**: cosmetic only — placement would change, not ownership.

### spike-3: Would changing `q_value`'s field class break `DecayingSortedField(base_score_field="q_value")`?

- **Assumption**: `base_score_field` is resolved by field type and would need
  updating.
- **Method**: code-read.
- **Result**: **False, with a sharp caveat.** `base_score_field` is stored as a
  bare **string name** (`decaying_sorted_field.py:271`), threaded through
  `query.py:549-552` as `ARGV[4]`, and consumed in Lua by a raw
  `redis.call('HGET', member, base_score_field)` (`decaying_sorted_field.py:147`).
  It never looks at the Python field class. The caveat is the decoder: only a
  plain msgpack number, or a table carrying `as_encodable`, is accepted; **any
  other encoding falls through to the `1.0` default silently**
  (`decaying_sorted_field.py:149-160`). So the new field type is free to be any
  class it likes *provided its on-disk encoding stays the `__Decimal__` tagged
  dict*.
- **Confidence**: high.
- **Impact if false**: would force `base_score_field` plumbing changes and blow
  the Small appetite.

### spike-4: Do any tests pin the wire format or the command sequence?

- **Assumption**: a test asserts the raw cmsgpack bytes, constraining the
  refactor.
- **Method**: code-read of `tests/test_policy_cache.py` (1328 lines) and
  `tests/test_guide_examples.py`.
- **Result**: **No.** Despite its name,
  `test_q_value_encoding_round_trip` (`tests/test_policy_cache.py:1057`) goes
  through the Python API and asserts only `isinstance(reloaded.q_value,
  Decimal)` plus numeric closeness. No `Mock`, no `assert_called`, no literal
  `__Decimal__` byte assertion anywhere. The suite is a behavioral oracle, not
  an encoding oracle — which is precisely why this plan requires an explicit
  base-vs-branch command capture rather than trusting a green run.
- **Confidence**: high.
- **Impact if false**: a pinned test would become the parity proof and the
  capture step could be dropped.

## Data Flow

Today, one call:

```
caller: update_q_value(policy, reward=1.0, max_future_q=0.4)
  -> policy_cache._get_redis_key(instance)      # raw key string off db_key
  -> redis_db.run_lua(POPOTO_REDIS_DB, TD_UPDATE_LUA, 1, key, r, a, g, mfq)
       -> lua_script(text)  # process-wide cache, registered against POPOTO_REDIS_DB
       -> Script(keys=[key], args=[...], client=POPOTO_REDIS_DB)
            -> EVALSHA on the server:
                 HGET <key> q_value
                 (decode cmsgpack; __Decimal__ tagged dict or bare number)
                 compute td_error, new_q
                 HSET <key> q_value <cmsgpack __Decimal__ tagged dict>
  <- td_error (string) -> float()
```

After this PR, the same wire sequence, reached one layer lower:

```
caller: update_q_value(policy, reward=1.0, max_future_q=0.4)   # unchanged shim
  -> TDValueField.td_update(instance, "q_value", reward=..., max_future_q=...,
                            alpha=..., gamma=..., pipeline=None)
       -> field resolves the member key from instance.db_key (field layer owns
          key construction, not the recipe)
       -> run_lua(<client resolved at call time>, TD_UPDATE_LUA, 1, key, ...)
            -> identical EVALSHA / HGET / HSET sequence
       -> decode float, sync instance.q_value in memory
  <- td_error
```

The independent second reader is untouched and must stay untouched:

```
PolicyEntry.query.filter(...)  ->  DECAY_SCORE_LUA
                               ->  HGET <member> q_value   # base_score_field
```

## Appetite

**Small.** One recipe, one call site, one new field class that is a thin
subclass of an existing one, plus a verbatim script move. The whole diff should
read as relocation, not redesign. If the work starts requiring changes to
`base_score_field` plumbing, to the serialization layer, or to any test
expectation, the appetite is blown and the plan is wrong — stop and re-plan.

## Prerequisites

- PR #634 and PR #644 merged (both are, on `origin/main` at `bb8ff588`) — they
  define the series conventions this PR follows.
- No dependency on #646, #648, or #649. This PR touches
  `src/popoto/recipes/policy_cache.py` and adds one new module under
  `src/popoto/fields/`; the other lanes in the series touch neither.

## Solution

## Test Impact

## Rabbit Holes

## Risks

## Race Conditions

## No-Gos (Out of Scope)

## Documentation

## Success Criteria

## Step by Step Tasks

## Open Questions
