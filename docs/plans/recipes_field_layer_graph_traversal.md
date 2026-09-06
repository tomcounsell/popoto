---
status: Planning
type: chore
appetite: Small
created: 2026-09-06
tracking: https://github.com/tomcounsell/popoto/issues/646
---

# Recipes through the field layer, PR 3: `graph_traversal`

## Problem

`src/popoto/recipes/graph_traversal.py` reaches past the field layer exactly
once. At `0e3146af` the site is:

| # | Line | Call | What it is asking |
|---|---|---|---|
| 1 | 213 | `POPOTO_REDIS_DB.srandmember(reverse_key, fanout_limit)` | "give me up to N members of this Relationship reverse index" |

The call itself is one command, but it is not the whole bypass. To reach it the
recipe first rebuilds the reverse index key by hand (lines 207-212):

```python
reverse_key = DB_key(
    Relationship.get_special_use_field_db_key(model_class, field_name),
    DB_key.from_redis_key(pk),
).redis_key
```

That is three pieces of field-layer knowledge living in a recipe: the
`$RelationshipF:<Model>:<field>` namespace convention, the fact that the
related key is appended as a `DB_key` segment, and — the subtle one — that a
colon-joined redis_key string must be parsed with `DB_key.from_redis_key` and
not `DB_key(...)`, because the latter escapes the colon
(`DB_key("User:alice").redis_key == "User{&#58;}alice"`). The recipe also owns
the bytes→str decode of the returned members (lines 221-222), which is the
storage layer's encoding contract, not a traversal concern.

Nothing in the public `Relationship` API can express this read. The closest is
`Relationship.filter_query` (`src/popoto/fields/relationship.py:441`), and it
is unusable here for three independent reasons:

1. It issues `SMEMBERS` and returns the **entire** set — there is no bounded or
   sampled form. On a hub node this is precisely the unbounded fan-out the
   recipe's `fanout_limit` exists to prevent.
2. It requires a **`Model` instance** (`if not isinstance(query_value, Model):
   raise QueryException`, line 487). `expand_relationships` holds only a
   `pk` redis_key string; satisfying `filter_query` would mean a `load()` per
   node per hop — a round trip added to the hot loop.
3. It unconditionally opens a pipeline and calls `execute()` (lines 482, 521),
   so routing through it would emit `MULTI`/`EXEC` where the recipe emits one
   `SRANDMEMBER`, violating this PR's byte-identical-behavior contract.

So the bypass is not laziness on the recipe's part; the entry point it needs
does not exist. This plan adds it.

## Freshness Check

**Disposition: Unchanged.** Baseline `0e3146af` (origin/main head, 2026-09-06).

- Issue #646 was filed 2026-09-06T11:15:14Z, after the #630 split. Both
  file:line references in its body were re-read against `0e3146af` and both
  still hold exactly: `graph_traversal.py:49` is
  `from ..redis_db import POPOTO_REDIS_DB`, and `graph_traversal.py:213` is the
  `srandmember` call.
- `git log --since` on `src/popoto/recipes/graph_traversal.py`,
  `src/popoto/fields/relationship.py` and `tests/test_graph_traversal.py`
  shows no commits since the issue was filed.
- Sibling PR #644 (issue #630 PR 2) merged as `3a5d00ca`; main has since
  advanced to `0e3146af` with a plan commit for #645. Neither touches this
  recipe. #644's additions (`Model.exists`, `popoto.batch`) are the pattern
  this plan follows, not a dependency of it.
- `grep -rn 'srandmember' src/` returns exactly one hit — line 213. There is no
  second site to sweep.
- No active plan in `docs/plans/` covers `graph_traversal`. The three sibling
  plans in the #630 series (`..._provenance_journal.md`, and the not-yet-written
  `policy_cache` / `context_assembler` / `memory_lifecycle` plans) each scope a
  disjoint recipe file.

## Prior Art

| Reference | Relevance |
|---|---|
| #630 (umbrella, OPEN) | "Recipes bypass the field layer with direct Redis calls." Split 2026-09-06 into #646-#649, one per recipe, because the SDLC state machine has one plan slot per issue and the series has one plan doc per recipe. Closes when #649 lands. |
| PR #634 (#630 PR 1) | First recipe converted (`default_memory`). Established the series pattern: add the missing field/model entry point, swap the recipe, prove neutrality with the existing tests. |
| PR #644 (#630 PR 2, `3a5d00ca`) | `provenance_journal`. Added `Model.exists()` and `popoto.batch()`. Directly precedential here: `ValidityField.get_valid_from` was used as the argument that a **public classmethod on the field class** is the right home for a read the field layer already implicitly owns. |
| PR #500 (#490) | `redis_db.sibling_client_kwargs()`. Prior art for the general shape "recipe knows too much about the client; move the knowledge into the layer that owns it." |

No prior attempt to fix *this* site exists, so there is no
`## Why Previous Fixes Failed` section.

## Research

No external research performed — this is a purely internal refactor of one
call site onto an existing in-repo API surface, with no new libraries, no
ecosystem patterns, and no external documentation in play. Phase 0.7's skip
condition applies verbatim.

One in-repo constraint worth restating because it bounds the design: Popoto
must run unmodified on Valkey, so only core commands plus Lua are permitted.
`SRANDMEMBER` is a core Set command present in Valkey; the new entry point
introduces no command that is not already issued today.

## Data Flow

_TBD_

## Appetite

_TBD_

## Prerequisites

_TBD_

## Solution

_TBD_

## Failure Path Test Strategy

_TBD_

## Test Impact

_TBD_

## Rabbit Holes

_TBD_

## Risks

_TBD_

## Race Conditions

_TBD_

## No-Gos (Out of Scope)

_TBD_

## Documentation

_TBD_

## Success Criteria

_TBD_

## Step by Step Tasks

_TBD_

## Critique Results

_Pending `/do-plan-critique`._
