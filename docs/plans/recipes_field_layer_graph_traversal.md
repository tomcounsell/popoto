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

1. **Entry point**: `traverse(...)` (line 354) → `expand_relationships(model_class,
   seeds, relationship_field_names, hops, fanout_limit, ...)` (line 106).
2. **Per hop, per frontier node**: for each self-referential `Relationship`
   field name, the loop does a forward expansion (`load` the instance, resolve
   the field value to a pk) and a reverse expansion.
3. **Reverse expansion (the site this plan changes)**: `pk` (a redis_key string)
   + `field_name` are turned into the reverse index key, one `SRANDMEMBER key
   fanout_limit` is issued, and the returned members are decoded to `str`.
4. **Output**: decoded member pks are folded into `next_weights` at
   `hop_weight`, deduplicated against `visited`, and returned as the
   `{pk: weight}` result map.

After this change, step 3's key construction, command issue, and decode move
behind one `Relationship` classmethod call; steps 1, 2 and 4 are untouched.

## Appetite

**Size:** Small

**Team:** Solo dev, PM, code reviewer

**Interactions:**
- PM check-ins: 1 (the two design questions the PM asked to be argued, answered
  in Solution below before build starts)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|---|---|---|
| Redis/Valkey reachable on DB 9 | `redis-cli -n 9 PING` | Lane-isolated test database (never DB 0 — live agent store) |
| Worktree venv resolves to this checkout | `.venv/bin/python -c "import popoto, pathlib; print(pathlib.Path(popoto.__file__).resolve())"` | The wrong-package trap (CLAUDE.md, worktree gotcha 1) |
| Optional extras installed | `.venv/bin/python -c "import numpy, sentence_transformers, mcp"` | Otherwise ~95 tests silently deselect (worktree gotcha 2) |

## Solution

### The two design decisions the PM asked to be argued

**Decision 1 — a new PUBLIC classmethod on `Relationship`, not a private
helper.** Three arguments, in descending weight:

1. *No existing API can express the read.* Demonstrated in Problem above:
   `filter_query` is unbounded, instance-only, and pipeline-wrapping. A
   bounded sample of a reverse index is a genuinely new capability of the
   field, not a rephrasing of one it already has.
2. *A leading-underscore helper called from another module is the worst of both
   worlds.* It would carry every maintenance obligation of a public API (a
   second module depends on its signature and its return contract) while
   advertising that callers may ignore it. The private-helper option only wins
   when the caller and the helper live in the same module; here they do not.
3. *Precedent.* `ValidityField.get_valid_from` is exactly this shape — a public
   classmethod on a field class wrapping one read of that field's own index —
   and PR #644 used it for exactly this purpose. `Field` already exposes
   `get_special_use_field_db_key` publicly for the same reason.

The counter-argument ("one call site does not justify public API surface") is
real but weaker than it looks: the site count is one *today* because
`memory_lifecycle` (#649) and `context_assembler` (#648) have not been
converted yet, and reverse-index sampling is the natural primitive for any
recipe that walks edges. Even if it stayed at one caller forever, the
alternative is not "less API" — it is the same knowledge duplicated in a recipe
where no field-layer change can find it.

**Decision 2 — the signature is NOT `sample_reverse(pk, n)`.** The issue's
suggested shape cannot name the index it reads. Building the reverse key needs
three inputs, not one: the model class, the field name, and the related key. A
two-argument `sample_reverse(pk, n)` would have to be an *instance* method on a
bound field to recover the other two, and `Relationship`'s existing index
methods (`on_save`, `on_delete`, `filter_query`) are all classmethods taking
`(model, field_name, ...)`. Mirroring them:

```python
@classmethod
def sample_related_keys(
    cls,
    model: Type["Model"],
    field_name: str,
    related_key: Union[str, DB_key],
    count: int,
) -> list[str]:
```

- `model`, `field_name` — same leading pair as `filter_query`, so the family
  reads consistently.
- `related_key` — accepts either a `DB_key` (used as-is, like `filter_query`
  uses `query_value.db_key`) or a `str` redis_key, which is parsed with
  `DB_key.from_redis_key`. This is where the colon trap moves: the recipe stops
  needing to know that `DB_key(str)` escapes and `from_redis_key(str)`
  unescapes, because the field owns it.
- `count` — passed through to `SRANDMEMBER` unchanged, deliberately unvalidated
  (see Risks). `fanout_limit` is a public parameter of `expand_relationships`,
  so callers can already pass any int; adding validation here would change
  observable behavior and break the byte-identical contract.
- Returns `list[str]` — members decoded from bytes inside the method. Decoding
  is the storage layer's contract, and centralizing it means a future
  `decode_responses=True` client is a one-line change in one place.

**Decision 3 (unasked, but the PM invited a call) — do NOT pair #646 with
#647.** Three reasons: (a) #647's substance is a different argument entirely —
whether the recipe should keep owning `TD_UPDATE_LUA` or whether the Lua moves
into a field — and merging the two would bury it; (b) two closing keywords in
one PR re-creates in miniature the one-slot-many-PRs problem the #630 split
just fixed, since each sub-issue now has its own plan slot and its own
`session-ensure` run; (c) a thin PR is not a defect. #646's whole value is that
it is small enough to verify exhaustively.

### Key Elements

- **`Relationship.sample_related_keys`** (new, public): builds the reverse index
  key, issues one `SRANDMEMBER`, returns decoded `str` members.
- **`graph_traversal.expand_relationships`** (modified): calls it; drops the
  hand-built key, the direct client call, and the decode loop.
- **Module imports** (modified): `graph_traversal.py` drops both
  `from ..models.db_key import DB_key` and
  `from ..redis_db import POPOTO_REDIS_DB` — after the swap neither is used
  anywhere in the file, so the recipe stops importing the client at all.

### Flow

`traverse()` → `expand_relationships()` → **`Relationship.sample_related_keys(model_class, field_name, pk, fanout_limit)`** → one `SRANDMEMBER` → `list[str]` → weight accumulation → result map

### Technical Approach

- **What stays in the recipe:** the `try/except Exception` around the reverse
  lookup and its `logger.warning("graph_traversal: reverse lookup failed ...")`.
  Degradation policy belongs to the traversal — "a failed reverse hop yields no
  neighbors and the walk continues" is a traversal decision, and the log
  message names the recipe. The new field method must therefore **not** catch
  exceptions; it propagates, and the recipe keeps deciding what a failure means.
- **What moves into the field:** key construction, the `from_redis_key` parse,
  the `SRANDMEMBER` issue, and the bytes→str decode.
- **Byte-identical constraint:** exactly one `SRANDMEMBER key count` is emitted,
  with the same key bytes and the same count, in the same position in the
  sequence. No pipeline, no extra `EXISTS`, no validation round trip. This is
  verified empirically, not by inspection — see Success Criteria.
- **Non-goal:** this is not itself the storage-backend seam. Like
  `popoto.batch()` in #644, it is a prerequisite — it removes one more direct
  client consumer so that a future seam has fewer places to intercept.

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
