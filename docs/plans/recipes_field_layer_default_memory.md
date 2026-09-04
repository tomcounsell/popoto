---
status: Ready
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
- PM check-ins: 0 (the issue and this plan settle scope; the naming decision is recorded under Decisions)
- Review rounds: 1

Roughly four small additive methods, one module, one recipe rewrite of ~15 lines, and four new test files or test classes. The existing eviction suite is the oracle and does not change.


## Prerequisites
| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli -n 15 ping` | test suite (auto-isolated on DB 15 by the pytest plugin) |
| Build branch forks from origin/main | `git merge-base --is-ancestor 85c9faa8 HEAD` | the stale hotfix branch predates the eviction loop (see Freshness Check notes) |
| Full dev extras installed | `python -c "import numpy, mcp"` | a bare `.[dev]` venv deselects ~95 tests (CLAUDE.md worktree rule 2) |


## Solution
### Key Elements

- **Sorted-index reads on the field**: `SortedFieldMixin.count()` and `SortedFieldMixin.members()` answer "how many" and "which members, in score order" for one partition of a sorted field's index. They own the key building and the bytes-to-str decoding. Classmethods taking `(model_instance, field_name, ...)`, matching every existing hook on the mixin.
- **Untracked direct load on the query layer**: `Query.get(..., _no_track=True)` loads by key with exactly one `HGETALL` and no `on_read()` staging. Mirrored on the async `get` so the two paths stay in lockstep.
- **Durable counter helper**: `popoto.counters.increment(key, delta=1)` and `read(key)`. A semantic "bump this named counter" op with the same `INCRBY` under it, so the key layout that `MemoryService._read_counters()` scans is untouched.
- **Recipe rewrite**: the eviction block in `DefaultMemory.save()` calls those three and nothing else. Its imports of `POPOTO_REDIS_DB` and `decode_popoto_model_hashmap` go away.
- **Acceptance test**: a source-level test asserting `default_memory.py` contains no `POPOTO_REDIS_DB` or `run_lua` reference, written so PRs 2-4 add their file to a parametrized list.

### Flow

`DefaultMemory.save()` → over cap? (`field.count`) → warn once → `counters.increment` → `field.members` → per victim `query.get(_no_track=True)` → `.delete()` or `_purge_orphan_keys` → return save result

### Technical Approach

- **`SortedFieldMixin.count(cls, model_instance, field_name) -> int`**: `key = cls.get_partitioned_sortedset_db_key(model_instance, field_name).redis_key`; `return int(POPOTO_REDIS_DB.zcard(key))`. Call the client through the module global at call time; never capture the bound method at import (spike-1).
- **`SortedFieldMixin.members(cls, model_instance, field_name, start=0, stop=-1, reverse=False) -> list[str]`**: same key; `zrevrange` when `reverse` else `zrange`; decode each member with the same bytes-or-str rule the recipe uses today (`raw.decode() if isinstance(raw, bytes) else str(raw)`). `reverse=True` is there for `memory_lifecycle.list_tombstones` in PR 4 and costs one branch; it gets a test now so PR 4 does not add an untested path.
- **Partition key resolution** reuses `get_partitioned_sortedset_db_key`, which raises `QueryException` when a partition field is unset. Let that propagate; the recipe's enclosing `try` already turns it into the "eviction skipped" warning, which is today's behavior for the same condition.
- **`Query.get(self, db_key=None, redis_key=None, _no_track=False, **kwargs)`**: on the direct-key branch, skip `_fire_on_read` when `_no_track`. On the filter fallback branch, pass `_no_track` through to the `QueryBuilder` (`.no_track()` when set) so the flag means the same thing on both branches. Async `get` gets the identical keyword and skip. Document the keyword in both docstrings; it is public in the sense that recipes call it, underscore-prefixed by the `_execute_filter` precedent so it can never shadow a field.
- **`popoto/counters.py`**: two functions, module docstring explaining that this is the durable-counter primitive recipes use and that the key string is the caller's contract. Return `int(...)` of the client result. No key-prefix logic here.
- **Recipe**: replace the four sites as in Data Flow. Keep every log message, the `_EVICTION_WARNED` marker placement, the own-key skip, and the orphan-purge branch byte-for-byte. Remove the now-unused imports (`POPOTO_REDIS_DB`, `decode_popoto_model_hashmap`; `cast` if it becomes unused) because `ruff check src/` gates F401.
- **Exports**: `count`/`members` are methods, nothing to export. `counters` is a new top-level module; do not add it to `popoto/__init__.py` (recipes import it by path; keeping the public namespace unchanged is one of the issue's non-goals).
- **Docs**: mkdocstrings generates the API pages from docstrings (`docs/scripts/gen_api_pages.py`), so the new methods surface automatically. Add one short "Reading the index" paragraph to `docs/features/decaying-sorted-field.md` showing `count`/`members`, and a CHANGELOG `[Unreleased]` line.


## Failure Path Test Strategy
### Exception Handling Coverage
- [ ] `default_memory.py` has exactly one handler in scope: the `except Exception as exc:` around the whole eviction block that logs "DefaultMemory eviction skipped". It is already tested by `test_notice_survives_a_mid_loop_failure` (zrange raises) and `test_counter_still_incremented_when_the_loop_aborts` (hgetall raises). Both must keep passing unchanged, which proves the new field/query methods propagate the same exceptions from the same commands.
- [ ] The new methods add no handlers. `count`/`members`/`increment` let client errors propagate; `Query.get(_no_track=True)` returns `None` on a missing hash as it does today.

### Empty/Invalid Input Handling
- [ ] `members()` on an empty or missing index returns `[]` (test). `count()` on a missing index returns `0` (test).
- [ ] `members(start, stop)` with `stop < start` returns `[]` (Redis semantics; test pins it so the recipe's `excess - 1` arithmetic is safe at `excess == 0`, which the recipe already short-circuits before).
- [ ] `count()`/`members()` on a partitioned field with an unset partition value raises `QueryException` (test), matching `get_partitioned_sortedset_db_key` today.
- [ ] `counters.read()` on a missing key returns `0` (test).
- [ ] `Query.get(redis_key=..., _no_track=True)` on a missing key returns `None` and writes no staged-access key (test).

### Error State Rendering
- [ ] No user-visible rendering. The eviction warning text is unchanged and is asserted by the existing `caplog` tests.


## Test Impact
No existing tests are edited. `tests/test_default_memory_eviction.py` (15 test functions, several parametrized, across cap precedence, notice, and counter semantics) is the behavioral oracle and must pass byte-for-byte; spike-1 shows why its client-object monkeypatches keep intercepting the calls once they move into the field layer. `tests/test_production_contracts.py`, `tests/test_integrations_*.py`, and `tests/test_never_record_firewall.py` exercise `DefaultMemory.save()` and stay green.

New tests:
- [ ] `tests/test_sorted_field_reads.py` (create) — `count`/`members` on a partitioned `DecayingSortedField` and an unpartitioned `SortedField`: cardinality, ascending and `reverse=True` order, `start/stop` window, empty index, `stop < start`, unset partition raises, members are `str`.
- [ ] `tests/test_query_get_no_track.py` (create) — `Query.get(redis_key, _no_track=True)` on an `AccessTrackerMixin` model leaves no `$AT:{Class}:staged:{key}` list and no `access_log`; the default path still stages; missing key returns `None`; async mirror behaves the same.
- [ ] `tests/test_counters.py` (create) — `increment` returns the running total, `read` returns `0` when absent, values round-trip as `int`.
- [ ] `tests/test_recipes_field_layer.py` (create) — reads each recipe source file in a parametrized list (`default_memory.py` for now) and asserts neither `POPOTO_REDIS_DB` nor `run_lua` appears. This is the issue's acceptance grep as a test; PRs 2-4 append their file.


## Rabbit Holes
- **Building `score_of`, `window`, `Model.exists`, `load_fields`, or a `batch()` context manager now.** The issue lists them, but none is needed by `default_memory`. Each PR adds what it consumes; speculative surface would ship untested and could be shaped wrong for the recipe that actually needs it.
- **Migrating `integrations/service.py` counters to `popoto.counters`.** It writes through its own client instance (DB-0 isolation tests depend on that) and is not a recipe. Out of #630's scope.
- **Designing the `Backend` protocol.** #631 owns that. This PR's only obligation to it is semantic naming.
- **Making `no_track` a chainable builder on `Query`.** `Query` is not a builder; the filter path already has `QueryBuilder.no_track()`. One keyword on `get` is the smallest change that covers the direct-key path.
- **"Fixing" the recipe's `excess > 0` guard, warn-once set, or counter-before-delete ordering.** All deliberate (#596). Byte-identical is the contract.


## Risks
### Risk 1: A new method issues an extra Redis command and shifts the fault-injection counts
**Impact:** `test_counter_still_incremented_when_the_loop_aborts` arms a `hgetall` that explodes on its second call. If `Query.get(_no_track=True)` did any `hgetall` beyond the one the recipe does today (say, an `EXISTS` first, or a re-read), the failure would land on a different victim and the counter/deleted arithmetic would change.
**Mitigation:** `Query.get`'s direct-key branch is already a single `hgetall`; the `_no_track` change only removes the `_fire_on_read` call after it. `count`/`members` are one command each. The unchanged oracle suite catches any drift.

### Risk 2: Capturing the client at import time breaks the spies
**Impact:** the eviction tests would run the real commands and the spy/fault assertions would silently pass or fail for the wrong reason.
**Mitigation:** the plan states the rule (module global, attribute lookup at call time), and `test_disable_values_skip_eviction_and_the_zcard` plus the `_ZcardSpy.saw(zset_key)` assertions fail loudly if the spy stops seeing the partition key.

### Risk 3: `ruff` F401 on leftover imports
**Impact:** `lint.yml` fails the PR.
**Mitigation:** listed as an explicit build step and verified by `scripts/ci-local.sh lint`.

### Risk 4: Naming lands wrong for the later PRs
**Impact:** PR 3/4 need a different shape (for example `context_assembler` holds a zset key string, not always an instance) and rename, churning a public method.
**Mitigation:** all four sibling `zcard` sites were read during planning; three hold an instance and the fourth (`context_assembler:733`) derives its key from records it also holds. `count(model_instance, field_name)` serves all of them. Confirmed by the owner; see Decisions.


## Race Conditions
No new race conditions. Every operation here is synchronous on one client and the command sequence per save is unchanged (`ZCARD`, `INCRBY`, `ZRANGE`, then per-victim `HGETALL` + delete). The pre-existing window between `ZRANGE` and `HGETALL` (a victim deleted by another process in between) is already handled: `Query.get` returns `None` and the recipe routes the member to `_purge_orphan_keys`, which re-checks `EXISTS` inside Lua before touching indexes. Concurrent over-cap saves from two processes can both select overlapping victims; `delete()` is idempotent on a missing hash, and the counter contract (`counter >= deleted`) was written for exactly this.


## No-Gos (Out of Scope)
- [SEPARATE-SLUG #630] The remaining five recipes (`provenance_journal`, `graph_traversal`, `policy_cache`, `context_assembler`, `memory_lifecycle`) and the primitives only they need (`score_of`, `window`, `Model.exists`, `load_fields`, `batch()`, `Relationship.sample_reverse`, tombstone model, `last_access`). The parent issue sequences them as PRs 2-4; each adds what it consumes.
- [SEPARATE-SLUG #631] The `Backend` protocol and any Postgres implementation. This PR only keeps its new names semantic.
- Public API stays as it is: `POPOTO_REDIS_DB`, `get_redis()`, and `provenance_journal`'s `pipeline=` are untouched (issue non-goal, restated so the builder does not "tidy" them). `popoto/__init__.py` does not gain a `counters` export.
- `integrations/service.py` keeps writing its counters through its own client. It is not a recipe and #630 scopes recipes only; the shared contract is the key prefix, pinned by `test_counter_prefix_matches_the_service_constant`, which continues to pass.


## Update System
No update system changes required. Pure library change with no new dependency, config, or migration; the `/update` skill is unaffected.


## Agent Integration
No agent integration required. The MCP `memory_status` tool and `popoto-memory doctor` read the eviction counter by prefix scan and see the same key with the same value.


## Documentation
### Feature Documentation
- [ ] `docs/features/decaying-sorted-field.md` — add a short "Reading the index" subsection: `count(instance, "field")` and `members(instance, "field", start, stop, reverse=...)` with the partition rule. Use the `documentarian` agent type.
- [ ] `CHANGELOG.md` `[Unreleased]` — one line under Added for the three primitives and one under Changed noting `default_memory` now routes eviction through them (behavior unchanged).

### External Documentation Site
- [ ] API pages regenerate from docstrings via mkdocstrings; verify `mkdocs build` passes (`scripts/ci-local.sh docs`).
- [ ] `docs/recipes.md:542` and `docs/features/harness-integration.md:420` describe eviction as "`zcard - cap` records"; that stays accurate (same command underneath). Leave as is.

### Inline Documentation
- [ ] Docstrings on `count`, `members`, `Query.get(_no_track)`, `counters.increment`/`read`.
- [ ] A one-line comment at the recipe's `query.get(..., _no_track=True)` site preserving today's "would fire on_read and stage an access for a record about to be deleted" rationale.


## Success Criteria
- [ ] `grep -n "POPOTO_REDIS_DB\|run_lua" src/popoto/recipes/default_memory.py` returns nothing, and `tests/test_recipes_field_layer.py` asserts the same.
- [ ] `tests/test_default_memory_eviction.py` passes with zero edits (`git diff --stat main -- tests/test_default_memory_eviction.py` is empty).
- [ ] New tests pass: `tests/test_sorted_field_reads.py`, `tests/test_query_get_no_track.py`, `tests/test_counters.py`, `tests/test_recipes_field_layer.py`.
- [ ] Anti-criterion: `git diff main --stat -- src/popoto/integrations/ src/popoto/__init__.py` is empty (No-Gos: service counters untouched, no new public export).
- [ ] Anti-criterion: `grep -n "_zcard\s*=\|= POPOTO_REDIS_DB\.\(zcard\|zrange\|zrevrange\|incrby\|hgetall\)" src/popoto/fields/sorted_field_mixin.py src/popoto/counters.py src/popoto/models/query.py` returns nothing (no import-time capture of client methods).
- [ ] `scripts/ci-local.sh --fast` green: `ruff check src/`, `black --check src/ tests/`, tests on DB 15. State the redis-py version alongside any mypy count if one is reported.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)


## Team Orchestration
When this plan is executed, the lead agent orchestrates work using Task tools. The lead NEVER builds directly.

### Team Members

- **Builder (primitives)**
  - Name: primitives-builder
  - Role: `SortedFieldMixin.count`/`members`, `Query.get(_no_track)` sync + async, `popoto/counters.py`, and their unit tests
  - Agent Type: builder
  - Domain: Redis/Popoto data
  - Resume: true

- **Builder (recipe)**
  - Name: recipe-builder
  - Role: rewrite the eviction block in `default_memory.py`, remove dead imports, add `tests/test_recipes_field_layer.py`
  - Agent Type: builder
  - Domain: Redis/Popoto data
  - Resume: true

- **Validator (parity)**
  - Name: parity-validator
  - Role: run the unchanged oracle suite and every anti-criterion grep; confirm the environment (venv resolves to this checkout, extras installed, DB 15)
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: `decaying-sorted-field.md` subsection and CHANGELOG line
  - Agent Type: documentarian
  - Resume: true


## Step by Step Tasks
### 1. Sorted-index reads and counter helper
- **Task ID**: build-primitives
- **Depends On**: none
- **Validates**: tests/test_sorted_field_reads.py (create), tests/test_counters.py (create)
- **Informed By**: spike-1 (call the client via the module global at call time; pass `.redis_key` strings), spike-3 (counter is a bare key; keep the key string in the caller)
- **Assigned To**: primitives-builder
- **Agent Type**: builder
- **Parallel**: true
- Branch `chore/recipes-field-layer-default-memory` from `origin/main` (verify `git merge-base --is-ancestor 85c9faa8 HEAD`).
- Add `count` and `members` classmethods to `SortedFieldMixin` in `src/popoto/fields/sorted_field_mixin.py`, next to `get_partitioned_sortedset_db_key`. Decode members to `str`. `reverse=True` uses `zrevrange`.
- Create `src/popoto/counters.py` with `increment(key: str, delta: int = 1) -> int` and `read(key: str) -> int`.
- Write the two test files per Test Impact, on a throwaway partitioned model and an unpartitioned one.

### 2. Untracked direct load
- **Task ID**: build-no-track
- **Depends On**: none
- **Validates**: tests/test_query_get_no_track.py (create)
- **Informed By**: spike-2 (no existing flag on the direct-key path; one `hgetall`; async mirror has the same unconditional `_fire_on_read`)
- **Assigned To**: primitives-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `_no_track: bool = False` to `Query.get` (query.py:2017) and the async `get`. Skip `_fire_on_read` on the direct-key branch when set; forward as `.no_track()` on the filter-fallback branch.
- Update both docstrings. Add the test file per Test Impact, asserting on the `$AT:{Class}:staged:{key}` list.

### 3. Recipe rewrite
- **Task ID**: build-recipe
- **Depends On**: build-primitives, build-no-track
- **Validates**: tests/test_default_memory_eviction.py (unchanged), tests/test_recipes_field_layer.py (create), tests/test_production_contracts.py, tests/test_never_record_firewall.py
- **Informed By**: Data Flow (command sequence must stay `ZCARD`, `INCRBY`, `ZRANGE`, per-victim `HGETALL`)
- **Assigned To**: recipe-builder
- **Agent Type**: builder
- **Parallel**: false
- Replace the four sites in `DefaultMemory.save()` per Data Flow. Keep every log string, the `_EVICTION_WARNED` placement, the own-key skip, and the orphan-purge branch verbatim.
- Remove the `POPOTO_REDIS_DB` and `decode_popoto_model_hashmap` imports; drop `cast` if unused. `ruff check src/` must exit 0.
- Add `tests/test_recipes_field_layer.py` with a parametrized recipe list containing `default_memory.py`.
- Run `black src/ tests/`.

### 4. Parity validation
- **Task ID**: validate-parity
- **Depends On**: build-recipe
- **Assigned To**: parity-validator
- **Agent Type**: validator
- **Parallel**: false
- Confirm the environment first (CLAUDE.md worktree rules 1, 2, 4), then run `scripts/ci-local.sh --fast`.
- Run every Success Criteria grep and the `git diff --stat` anti-criteria; report each as pass/fail with the command output.
- Confirm `tests/test_default_memory_eviction.py` has no diff against main.

### 5. Documentation
- **Task ID**: docs
- **Depends On**: build-recipe
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: true
- `docs/features/decaying-sorted-field.md` "Reading the index" subsection; CHANGELOG `[Unreleased]` lines; `scripts/ci-local.sh docs` passes.

### 6. Pull request
- **Task ID**: pr
- **Depends On**: validate-parity, docs
- **Assigned To**: recipe-builder
- **Agent Type**: builder
- **Parallel**: false
- Open the PR against `main` referencing #630 without a closing keyword (PRs 2-4 remain). Title: `chore(#630): route default_memory eviction through field/model reads`. Body lists the three primitives and the byte-identical claim with the oracle suite name.


## Decisions (recorded 2026-09-04, not open)

1. **Method names: `count` / `members`.** Confirmed by the owner via `/ask-me`. The field object at the call site already says it is an index read; the issue's names stand for this PR, PRs 2-4, and the #631 protocol surface.
2. **Async parity for `_no_track`: mirror it.** Decided at plan time as low-stakes and reversible. The #571 fix landed for exactly this kind of sync/async drift, so the keyword goes on both `get` paths in the same PR.
