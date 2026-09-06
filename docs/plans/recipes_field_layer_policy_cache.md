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

## Data Flow

## Appetite

## Prerequisites

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
