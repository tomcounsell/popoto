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

### Exception Handling Coverage

There is one broad handler in scope: the `except Exception` at
`graph_traversal.py:214-221` wrapping the reverse lookup. It is not silent — it
logs `logger.warning("graph_traversal: reverse lookup failed for %s.%s: %s")`
and sets `members = []` — but **no existing test exercises it**. The closest,
`test_exception_in_relationship_expansion_degrades_gracefully` (line 233),
monkeypatches `graph_traversal.expand_relationships` wholesale, so it never
reaches this handler.

This is a real coverage gap today, and the seam is what makes it cheaply
closable: once the body of the `try` is a single named call,
`Relationship.sample_related_keys` becomes a patch point. Add
`test_reverse_lookup_failure_warns_and_continues`: monkeypatch
`sample_related_keys` to raise, then assert (a) `caplog` contains the
`reverse lookup failed` warning, and (b) the forward-direction neighbors are
still returned — degradation, not collapse.

The new field method itself contains **no exception handler by design** (see
Technical Approach); there is nothing there to test for swallowing.

### Empty/Invalid Input Handling

`sample_related_keys` receives inputs from an internal loop, but its parameters
are reachable from public API (`fanout_limit` is a public kwarg of
`expand_relationships`). Behavior to pin with tests:

- **Empty/missing reverse index** — `SRANDMEMBER` on a nonexistent key returns
  `[]`; the method returns `[]`, not `None`. Test directly.
- **`count=0`** — Redis returns `[]`. Passed through unchanged; test that no
  exception is raised and the walk yields no reverse neighbors.
- **`related_key` as `str` vs `DB_key`** — both accepted; a `str` containing a
  colon (`"User:alice"`) must round-trip through `from_redis_key` and hit the
  same key the `DB_key` form produces. This is the colon trap, tested directly.
- **Empty-string `related_key`** — must not silently read a truncated key;
  assert the key built is the namespace joined with an empty segment (whatever
  `DB_key` already does), i.e. that the method adds no new special-casing.

Not agent-output processing; no empty-output loop risk.

### Error State Rendering

No user-visible rendering surface — the observable failure behavior is the
`logger.warning` plus the degraded (non-empty, forward-only) result, both
asserted in the test above.

## Test Impact

No existing test is expected to change. Justification, verified against
`0e3146af`:

- `tests/test_graph_traversal.py` (20 tests) asserts only on the `{pk: weight}`
  return values of `traverse` / `expand_relationships`, never on Redis calls.
- No test monkeypatches `srandmember`, the reverse-path client, or
  `POPOTO_REDIS_DB` for this recipe. Line 31's `POPOTO_REDIS_DB` import is used
  only by `test_decay_modulation_lowers_stale_weight` (line 293, a `zadd` for
  an unrelated decay fixture), so the swap breaks no patch point.
- No `xfail`/`skip` markers relate to this behavior.

New tests only:

- [ ] `tests/test_graph_traversal.py::test_reverse_lookup_failure_warns_and_continues` — ADD (closes the handler gap above)
- [ ] `tests/test_relationship_sample.py` — ADD: unit coverage for
      `sample_related_keys` (populated index, empty index, `count=0`,
      `str` vs `DB_key` related_key, colon-containing key, decode returns `str`)

## Rabbit Holes

- **Generalizing to "sample any field index."** A generic
  `Field.sample_index_members` across `SortedFieldMixin`, `KeyFieldMixin`, and
  `Relationship` is tempting and wrong here: those indexes are Sorted Sets and
  Sets with different member semantics, and there is one caller. Add it if and
  when #648/#649 produce a second and third.
- **Fixing `filter_query`.** It has real problems (unconditional pipeline,
  `str.strip` used as a prefix-strip at lines 501 and 510, which is a latent
  character-class bug). None of them are this PR. Do not touch it.
- **Changing the sampling semantics.** `SRANDMEMBER` with a positive count is
  distinct-member sampling with no ordering guarantee. Do not "improve" it to
  `SSCAN` with a cursor, or to a deterministic sample for testability — that
  changes command sequence and observable behavior, both of which this PR
  forbids.
- **Making the recipe's `try/except` narrower.** Tightening it to
  `RedisError` is a behavior change (a `TypeError` from a bad `fanout_limit`
  would newly propagate). It may well be right; it is #646's neighbor, not
  #646.

## Risks

### Risk 1: The `str` overload of `related_key` re-introduces the colon trap at a new layer
**Impact:** If `sample_related_keys` parses a `str` with `DB_key(...)` instead
of `DB_key.from_redis_key(...)`, every colon-containing pk silently reads a
wrong (escaped) key and the reverse expansion returns nothing — a silent
recall regression with no error.
**Mitigation:** The dedicated colon test in `test_relationship_sample.py`
asserts the `str` and `DB_key` forms resolve to the same key, and the
command-sequence capture (Success Criteria) would show the changed key bytes.

### Risk 2: Behavior drift hidden by the tests' coarse granularity
**Impact:** The existing tests assert on final weights, so a subtly different
key or an extra command could pass all 20 and still change what a production
walk touches.
**Mitigation:** Do not rely on the suite alone. Capture the Redis command
sequence base-vs-branch for a fixed traversal and diff it, as PR #644 did. That
is the actual oracle for "byte-identical".

### Risk 3: Public API added for one caller
**Impact:** If #648/#649 turn out not to need reverse sampling, the repo carries
a one-caller public method forever.
**Mitigation:** Accepted, and argued in Solution Decision 1 — the alternative
is not less surface but duplicated field-layer knowledge in a recipe. The
method is six lines and fully tested; the carrying cost is near zero.

## Race Conditions

**No race conditions identified.** The change adds no new command, no new
ordering between commands, and no shared mutable state — it relocates the
construction of one key and the issue of one already-atomic `SRANDMEMBER`. The
pre-existing read-your-own-writes looseness of a BFS walk (another writer can
add an edge between hops) is unchanged by this PR, in either direction.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #647] `policy_cache` — 1 direct site, but the recipe owns
  `TD_UPDATE_LUA`; the design question is Lua ownership, unrelated to this one.
  Deliberately NOT paired into this lane run (Solution, Decision 3).
- [SEPARATE-SLUG #648] `context_assembler` — 7 sites, two Lua scripts with
  recipe-owned KEYS layouts.
- [SEPARATE-SLUG #649] `memory_lifecycle` — 17 sites, hand-built tombstone
  store, Redis-only `OBJECT idletime`. Closes umbrella #630 when it lands.
- [SEPARATE-SLUG #630] The storage-backend seam itself. This PR removes one
  direct client consumer; it does not introduce a backend abstraction, and
  nothing in the diff should read like one.

## Documentation

`grep -rln 'graph_traversal\|expand_relationships'` across `docs/`, `README.md`
and `CHANGELOG.md` (excluding historical `docs/plans/`) returns exactly one
file: `docs/benchmarks.md:703`, which names the
`graph_traversal_relationship_fields` benchmark arm. That reference is about
what the benchmark measures, not how the recipe reads Redis, and this PR does
not change it.

Docs work in scope:

- `docs/relationship.md` — add a short section documenting
  `Relationship.sample_related_keys` as public API: what it reads (the reverse
  index Set), that `count` maps to `SRANDMEMBER`'s count (distinct members, no
  ordering guarantee), and that a `str` `related_key` is parsed as a redis_key.
- `CHANGELOG.md` `[Unreleased]` — one **Added** entry for the new classmethod,
  one **Changed** entry for the recipe no longer calling Redis directly,
  matching the phrasing PR #644 used.
- `mkdocs build --strict` must pass. Run it with
  `PYTHONPATH=<worktree>/src` — `docs/scripts/gen_api_pages.py` does an
  `importlib.import_module` importability check, and without the override it
  resolves against the primary checkout and silently omits new modules (the
  trap that cost a round on #644).

The full `/do-docs` cascade runs after build and before `verdict finalize`.

## Success Criteria

| # | Criterion | Check |
|---|---|---|
| 1 | The recipe issues no direct client call | `grep -n 'POPOTO_REDIS_DB' src/popoto/recipes/graph_traversal.py` returns nothing |
| 2 | The recipe no longer builds index keys | `grep -n 'DB_key\|get_special_use_field_db_key' src/popoto/recipes/graph_traversal.py` returns no code hits (docstring prose at lines 4/14/19-23 may remain) |
| 3 | Behavior is byte-identical | Redis command sequence captured for a fixed traversal on base and on branch is identical, command-for-command and key-for-key |
| 4 | Existing tests unchanged and green | `POPOTO_TEST_DB=9 .venv/bin/python -m pytest tests/test_graph_traversal.py` — 20 passed, and `git diff --stat` shows no deletions in that file |
| 5 | New coverage lands | `test_reverse_lookup_failure_warns_and_continues` plus `tests/test_relationship_sample.py` pass on DB 9 |
| 6 | Relationship suite unaffected | `POPOTO_TEST_DB=9 .venv/bin/python -m pytest tests/test_relationship*.py` green |
| 7 | Gates pass | `ruff check src/`, `black --check src/ tests/`, `scripts/mypy_ratchet.py` at or below baseline (state the redis-py version with the count) |
| 8 | Docs build | `PYTHONPATH=$PWD/src mkdocs build --strict` exits 0 |
| 9 | *(anti-criterion for the No-Gos)* No sibling recipe touched | `git diff --name-only main...HEAD` contains no `policy_cache.py`, `context_assembler.py`, or `memory_lifecycle.py` |
| 10 | *(anti-criterion)* No backend abstraction introduced | The diff adds no new module, protocol, or `Backend`/`Store` class — only one method on `Relationship` |

## Step by Step Tasks

1. **Add `Relationship.sample_related_keys`.**
   `src/popoto/fields/relationship.py`, placed next to `filter_query` so the
   index-reading methods stay together. Build the key exactly as the recipe
   does today (`get_special_use_field_db_key` joined with the related `DB_key`),
   parse a `str` `related_key` with `DB_key.from_redis_key`, issue one
   `SRANDMEMBER`, decode members to `str`, return `list[str]`. No try/except.
   *Validate:* `ruff check src/ && black --check src/`.

2. **Add `tests/test_relationship_sample.py`.** Cases from Failure Path Test
   Strategy: populated index, empty index, `count=0`, `str` vs `DB_key`
   `related_key`, colon-containing key, `str` return type.
   *Validate:* `POPOTO_TEST_DB=9 .venv/bin/python -m pytest tests/test_relationship_sample.py`.
   *Depends on:* 1.

3. **Capture the base command sequence.** On `main` in this worktree, run a
   fixed traversal under a command-recording client and save the sequence.
   Repro script must set `REDIS_URL=redis://localhost:6379/9` **before**
   `import popoto` (copy `scripts/scratch_repro.py`; never DB 0).
   *Validate:* a saved sequence file with a non-zero `SRANDMEMBER` count.

4. **Swap the recipe call site.** Replace lines 205-222 of
   `graph_traversal.py` with the `sample_related_keys` call inside the existing
   `try`, keeping the `except`/`logger.warning` verbatim and dropping the
   decode loop. Remove the now-unused `DB_key` and `POPOTO_REDIS_DB` imports.
   *Validate:* Success Criteria 1 and 2.
   *Depends on:* 1.

5. **Capture the branch command sequence and diff it against task 3.**
   *Validate:* empty diff (Success Criterion 3).
   *Depends on:* 3, 4.

6. **Add `test_reverse_lookup_failure_warns_and_continues`** to
   `tests/test_graph_traversal.py`.
   *Validate:* `POPOTO_TEST_DB=9 .venv/bin/python -m pytest tests/test_graph_traversal.py` — 21 passed.
   *Depends on:* 4.

7. **Run the narrow gate set.** `tests/test_graph_traversal.py`,
   `tests/test_relationship*.py`, `ruff`, `black --check`,
   `scripts/mypy_ratchet.py`. Narrow scope only — a full-suite run from this
   worktree collides with the other lanes on Redis state.
   *Validate:* all green; record the redis-py version alongside the mypy count.
   *Depends on:* 2, 5, 6.

8. **Docs.** `docs/relationship.md` section, `CHANGELOG.md` `[Unreleased]`
   entries, then `PYTHONPATH=$PWD/src mkdocs build --strict`.
   *Depends on:* 7.

9. **Open the PR with `Closes #646`** (this one does close its issue — unlike
   #644, which deliberately carried no closing keyword because #630 was an
   umbrella). Then: `/do-pr-review` → patch → `/do-docs` cascade → re-review the
   delta → `verdict finalize` **last** → merge gate.
   *Depends on:* 8.

## Critique Results

_Pending `/do-plan-critique`._
