---
status: Planning
type: chore
appetite: Small
owner: Tom Counsell
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/630
last_comment_id: none
---

# Recipes through the field layer, PR 1: `default_memory`

## Problem
`recipes/default_memory.py` is meant to be orchestration over the field
primitives it composes (a `DecayingSortedField` recency index, `AccessTrackerMixin`
read staging, the model's own persistence). Its over-cap eviction path instead
reaches past those primitives and drives the Redis client by hand at four sites:

| Line (at `85c9faa`) | Call | What the recipe is really asking |
|---|---|---|
| 197 | `POPOTO_REDIS_DB.zcard(zset_key)` | "how many members does the `relevance` partition index hold?" |
| 232 | `POPOTO_REDIS_DB.incrby(counter_key, excess)` | "bump the durable eviction counter" |
| 236 | `POPOTO_REDIS_DB.zrange(zset_key, 0, excess - 1)` | "give me the stalest N members of that index" |
| 240 | `POPOTO_REDIS_DB.hgetall(victim)` | "load this record for deletion without counting it as a read" |

To do that it rebuilds the sorted-set key through
`get_partitioned_sortedset_db_key(...).redis_key`, decodes raw bytes members
itself, and calls `decode_popoto_model_hashmap` directly to dodge the
`on_read()` hook that `Query.get()` fires unconditionally. Every one of those is
key-layout or hook-ordering knowledge that belongs to the field or the query
layer. When that layout changes, the recipe silently breaks.

This is also the first of the four PRs #630 sequences. It is the smallest
recipe by call count and the one whose tests already pin the exact Redis
command sequence (spies and fault injectors on `zcard`, `zrange`, `hgetall`),
so it is the right place to establish the pattern: add the read primitive the
recipe needs, swap the call site, prove byte-identical behavior with the
existing tests untouched.

**Current behavior:**
`default_memory.py` imports `POPOTO_REDIS_DB` and calls four client commands
directly. `grep -n "POPOTO_REDIS_DB\|run_lua" src/popoto/recipes/default_memory.py`
returns five lines (one import, four calls).

**Desired outcome:**
The same grep returns nothing. Eviction behavior, the eviction counter contract,
the log messages, and the exact number of `ZCARD`/`ZRANGE`/`HGETALL` round trips
are unchanged, proven by `tests/test_default_memory_eviction.py` passing with
zero edits. Three small additive primitives exist for the later PRs to reuse:
sorted-index reads on `SortedFieldMixin`, an untracked direct load on
`Query.get`, and a durable counter helper.


## Freshness Check
**Baseline commit:** `85c9faa8` (origin/main, the commit the issue's inventory names)
**Issue filed at:** 2026-09-04T10:28:00Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/recipes/default_memory.py` — issue claims 4 sites: `zcard`, `incrby`, `zrange`, `hgetall` — still holds, at lines 197, 232, 236, 240 (issue table gives no line numbers; recorded here for the builder).
- `src/popoto/fields/decaying_sorted_field.py` "do not unify KEYS[2]" comment — present at lines 68-80. Informs PR 3 (`context_assembler`), cited here only as context for why the read methods must own key building.
- `src/popoto/models/base.py` atomic-increment Lua (issue: "may already cover" the counter) — `Model.atomic_increment` at line 1984 increments a *model field* value inside the record hash. The eviction counter is a bare string key with no model behind it, so this does not cover it. See spike-3.

**Cited sibling issues/PRs re-checked:**
- #631 (storage backend POC) — opened 2026-09-04T10:30Z, two minutes after #630, still open. States explicitly that it does not depend on #630 and that "recipes stay Redis-bound until #630 lands". Its design constraint (the protocol speaks field semantics, never Redis structures) shapes the naming here: the new reads are `count`/`members` on an index, not `zcard`/`zrange` wrappers.
- #596 / PR #598, PR #603 — merged 2026-09-04. Wrote the current eviction loop, the counter contract, and the spy/fault-injection tests that are this plan's oracle. Closed.

**Commits on main since issue was filed (touching referenced files):** none. `git log --since=2026-09-04T10:28:00Z main` is empty; HEAD is the issue's own baseline.

**Active plans in `docs/plans/` overlapping this area:** none active. `default_memory_eviction_escape_hatch.md` (shipped as #598/#603) and `never_record_firewall.md` (shipped as #587) touch the same file but are complete; this plan builds on their result rather than alongside them.

**Notes:** the local checkout was on `fix/hotfixes-549-550-578-583`, 124 commits behind main, when planning started. On that branch `default_memory.py` predates the eviction loop and has zero direct calls, which is why a first grep disagreed with the issue. The build branch must fork from `origin/main`.


## Prior Art
- **#513 / PR #526**: DefaultMemory, query-blind warning, content-first injection -- created the recipe. No eviction yet, no direct client calls.
- **#594**: Agent memory production audit -- introduced the per-agent record cap and the first eviction loop, which is where `zcard`/`zrange`/`hgetall` entered the recipe.
- **#596 / PR #598 / PR #603**: deploy-level kill switch and loud first eviction -- added the env-var precedence, the `$popoto_memory:counter:{agent}:evicted` counter (the `incrby` site), the warn-once notice, and `tests/test_default_memory_eviction.py`. Those tests monkeypatch `POPOTO_REDIS_DB.zcard/zrange/hgetall` on the client instance to spy on and fault-inject the exact command sequence. They are the byte-identical oracle for this PR and must pass unedited.
- **#630**: the parent issue. Its "Sequencing" section names `default_memory` as a candidate first PR.
- **#631**: Postgres backend POC. Consumer of the seam this series prepares; constrains naming (semantic reads, not structure wrappers).
- **Existing primitives found in the tree**: `QueryBuilder.no_track()` (query.py:340) suppresses `on_read()` on filter queries but there is no equivalent on the direct-key `Query.get()` path; `_execute_filter(**kwargs)` already uses an underscore kwarg `_no_track`, the precedent this plan extends. `DB_key.exists()` exists on the key object. No field defines `count` or `members` today (grep of `src/popoto/fields/` is empty), so the new names do not shadow anything.

No previous attempt at this refactor exists, so the "Why Previous Fixes Failed" section is omitted.


## Research
No relevant external findings — this is an internal refactor over popoto's own field and query layers with no new library, API, or ecosystem pattern involved. Proceeding with codebase context.


## Spike Results
All three spikes were code-read spikes run inline during planning (Small appetite, cap of 2 dispatched agents; none were dispatched because every question was answerable from the tree in under five minutes).

### spike-1: Do the existing spy/fault tests survive routing through field methods?
- **Assumption**: "If `SortedFieldMixin.count()` calls `POPOTO_REDIS_DB.zcard(...)` through the module global, `tests/test_default_memory_eviction.py`'s monkeypatches still intercept it."
- **Method**: code-read
- **Finding**: Yes. `_ZcardSpy` and the `zrange`/`hgetall` fault injectors use `monkeypatch.setattr(POPOTO_REDIS_DB, "zcard", spy)`, patching the attribute on the shared client *object*. `sorted_field_mixin.py` already imports that same object (`from ..redis_db import POPOTO_REDIS_DB`, line 59) and the pytest plugin collapses `popoto` / `src.popoto` onto one client. Attribute lookup happens at call time, so a field method that writes `POPOTO_REDIS_DB.zcard(key)` is intercepted identically to the recipe's current call. The one way to break this is to capture a bound method at import time (`_zcard = POPOTO_REDIS_DB.zcard`); the plan forbids that.
- **Confidence**: high
- **Impact on plan**: existing tests stay as the oracle with zero edits. `count()` must pass the `.redis_key` string (not the `DB_key` object) because `_ZcardSpy.saw()` compares against the string the recipe passes today.

### spike-2: Is there already an untracked direct-key load?
- **Assumption**: "`Model.query.get(redis_key)` can skip `on_read()` through an existing flag."
- **Method**: code-read
- **Finding**: No. `Query.get()` (query.py:2017) does one `hgetall`, decodes, then calls `_fire_on_read` unconditionally. `no_track()` lives on `QueryBuilder`, which has no `get`. The recipe's hand-rolled `hgetall` + `decode_popoto_model_hashmap` exists precisely to avoid staging an access for a record about to be deleted (the staged key would be removed again by `_delete_access_tracker_keys`, so the cost is one wasted `RPUSH`+`EXPIRE` per victim, but "byte-identical" means no extra commands). The async mirror (`AsyncQuery.get`, query.py:~3500) has the same unconditional `_fire_on_read`.
- **Confidence**: high
- **Impact on plan**: add `_no_track: bool = False` to `Query.get()` and its async mirror. The underscore name follows the `_execute_filter(_no_track=...)` precedent and cannot collide with a public field name. `Query.get(redis_key=victim, _no_track=True)` then performs exactly one `hgetall`, which keeps the flaky-`hgetall` test's call count intact.

### spike-3: Where does the eviction counter increment belong?
- **Assumption**: "`Model.atomic_increment` covers the `incrby` site."
- **Method**: code-read
- **Finding**: It does not. `atomic_increment` runs a Lua read-decode-increment-encode on a *field inside a record hash*. The eviction counter is a bare Redis string at `$popoto_memory:counter:{agent_id}:evicted`, read by prefix scan in `MemoryService._read_counters()` (integrations/service.py:688) and surfaced by `status()`, the MCP `memory_status` tool, and `popoto-memory doctor`. Turning it into a model field would change the key and break that read contract, which `test_counter_prefix_matches_the_service_constant` pins. The counter needs a thin semantic primitive (`increment(key, delta) -> int`) that keeps the key exactly as it is. `integrations/service.py` writes its own counters through its own client (`self.redis.incr`, possibly bound to a different DB per the DB-0 isolation tests), so it is not migrated here.
- **Confidence**: high
- **Impact on plan**: new module `src/popoto/counters.py` with `increment()` and `read()`. The recipe keeps owning the key string (it is a recipe/integrations contract, not a field layout).


## Data Flow
Eviction is triggered from inside `DefaultMemory.save()` after the record's own
write completes. After this change the path is:

1. **Entry point**: `DefaultMemory(...).save()` with no caller pipeline and a truthy cap (class attr / env precedence unchanged).
2. **Index read**: `field.count(self, "relevance")` -> `SortedFieldMixin.count` builds the partition key with `get_partitioned_sortedset_db_key(self, "relevance")` and issues one `ZCARD`. Returns an `int`. `excess = count - cap`; under the cap the method returns here (one round trip per save, as today).
3. **Notice**: unchanged warn-once / debug logging, keyed on `(class name, agent_id)`.
4. **Counter**: `counters.increment(f"{EVICTION_COUNTER_PREFIX}:{agent_id}:evicted", excess)` -> one `INCRBY`. Still before the delete loop, still inside the enclosing `try`, so the "counter >= deleted" invariant holds.
5. **Victim selection**: `field.members(self, "relevance", 0, excess - 1)` -> one `ZRANGE`, members decoded to `str` inside the field method. The saving record's own key is skipped as today.
6. **Load for delete**: `type(self).query.get(redis_key=victim, _no_track=True)` -> one `HGETALL`, decode via `decode_popoto_model_hashmap`, **no** `_fire_on_read`. Returns `None` when the hash is gone.
7. **Delete or purge**: instance `.delete()` (cleans every index) or `self._purge_orphan_keys([victim])` for a member with no hash, exactly as today.
8. **Output**: `save()` returns the superclass result; any exception inside steps 2-7 is logged as "eviction skipped" and never fails the save.

Redis command sequence per over-cap save: `ZCARD`, `INCRBY`, `ZRANGE`, then per victim `HGETALL` + the delete pipeline. Identical to today.


## Architectural Impact
- **New dependencies**: none external. One new internal module (`popoto/counters.py`).
- **Interface changes**: all additive. `SortedFieldMixin.count(model_instance, field_name)` and `SortedFieldMixin.members(model_instance, field_name, start=0, stop=-1, reverse=False)` as classmethods (fields do not know their own name; every existing hook takes `(model, field_name)`, so these follow suit). `Query.get(..., _no_track=False)` and the async mirror gain one keyword. `popoto.counters.increment(key, delta=1) -> int` and `read(key) -> int`.
- **Coupling**: decreases. The recipe stops depending on `redis_db`, `models.encoding`, and the sorted-set key format. It depends on the field's public read API and the query layer instead.
- **Data ownership**: unchanged. The sorted index stays owned by the field; the counter key stays a recipe/integrations contract.
- **Reversibility**: trivial. The new methods are additive; reverting the recipe file restores the direct calls.
- **Downstream**: PRs 2-4 of #630 consume `count`/`members`/`_no_track` directly (`memory_lifecycle` has three `zcard` sites and `zrange`/`zrevrange`; `context_assembler` one `zcard`). #631's protocol later gets `count`/`members` as semantic index reads rather than `zcard`/`zrange`.


## Appetite
**Size:** Small

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0 (the issue and this plan settle scope; the one naming decision is recorded in Open Questions with a default)
- Review rounds: 1

Roughly four small additive methods, one module, one recipe rewrite of ~15 lines, and four new test files or test classes. The existing eviction suite is the oracle and does not change.


## Prerequisites
(placeholder)

## Solution
(placeholder)

## Failure Path Test Strategy
(placeholder)

## Test Impact
(placeholder)

## Rabbit Holes
(placeholder)

## Risks
(placeholder)

## Race Conditions
(placeholder)

## No-Gos (Out of Scope)
(placeholder)

## Update System
(placeholder)

## Agent Integration
(placeholder)

## Documentation
(placeholder)

## Success Criteria
(placeholder)

## Team Orchestration
(placeholder)

## Step by Step Tasks
(placeholder)

## Open Questions
(placeholder)
