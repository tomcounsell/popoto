---
status: Planning
type: bug
appetite: Medium
owner: valorengels
created: 2026-06-23
tracking: https://github.com/tomcounsell/popoto/issues/413
last_comment_id:
---

# MemoryLifecycle.tick() — bypass AccessTracker, single-pass hydration, bound staged lists

## Problem

`MemoryLifecycle.tick()` is the agent-memory maintenance sweep: it promotes eligible
episodic records to the semantic tier and forgets stale low-importance records. To do
this it *reads* the corpus — but it reads through the normal tracked query path, so the
maintenance job pollutes the very access signal it is meant to curate, and it does so
inefficiently.

**Current behavior** (measured on a 50,000-record corpus; PoC in issue #413):

1. **`tick()` stages O(N) AccessTracker reads per tick.** `_iter_tier` and
   `_iter_non_semantic` load records via `Model.query.filter(...).all()`; the
   `QueryBuilder.all()` path fires `_fire_on_read()` (`query.py:2426`,
   `:2429`), which `RPUSH`es one timestamp per hydrated record. `memory_lifecycle.py`
   never opts out (zero uses of `no_track`). First tick over 50k: **+40,000 staged keys
   (+2.6 MB)**; every steady-state tick appends **47,500** more staged timestamps that
   nothing ever confirms or cleans. A pure maintenance read thus inflates "access
   activity" pending confirmation and grows storage on every run.

2. **Staged lists grow unbounded** (PERF-6). `on_read()` is a bare `RPUSH` with no
   cap and no TTL (`access_tracker.py:89-103`). The `_max_access_log = 100` cap applies
   only to the *confirmed* log inside `confirm_access()`'s Lua (`access_tracker.py:41-52`).
   Any record read (by queries *or* by lifecycle ticks) but never explicitly confirmed
   accumulates ~18 bytes per read forever; a long-neglected backlog makes its eventual
   confirmation a blocking O(backlog) Lua call.

3. **The corpus is hydrated (at least) twice per tick; "batching" is cosmetic.**
   `_iter_tier` and `_iter_non_semantic` each call `.all()` — full hydration of every
   matching record into Python — *then* slice the already-loaded list by
   `TICK_BATCH_SIZE` (`memory_lifecycle.py:548-579`). Promote hydrates the episodic tier;
   forget re-hydrates all non-semantic records. Per tick at 50k: ~10 s wall, ~454,000
   Redis commands (~9 per record), even on a no-op tick.

**Desired outcome:** a `tick()` produces **zero** new staged AccessTracker entries;
each record is hydrated at most once per pass; staged lists are bounded so read-but-
never-confirmed records cannot grow without limit. The access-frequency signal then
reflects only genuine application reads.

**Explicit scope boundary (from the issue):** the full O(N) → server-side Lua top-K
rework of composite scoring and tier scans is **deferred** — the maintainer set a ~20k-
memory scale target (2026-06-11) at which O(N) ticks are acceptable. This issue is about
`tick()`'s self-pollution and gratuitous double work, not a general performance rewrite.

## Freshness Check

**Baseline commit:** `a46006607606ca8b828c18c924410d620bee187e`
**Issue filed at:** 2026-06-11T05:20:35Z
**Disposition:** Unchanged

**File:line references re-verified against current main:**
- `memory_lifecycle.py:461` — `tick()` definition — still holds (line 461).
- `memory_lifecycle.py:548-560` — `_iter_tier` calls `.filter(...).all()` then slices — still holds.
- `memory_lifecycle.py:562-579` — `_iter_non_semantic` calls `.filter().all()` OR `Query.all()`, then slices — still holds.
- `memory_lifecycle.py:662-665` — `_get_all_records` calls `Query.all()` — still holds.
- `memory_lifecycle.py:153` / `:180` — `OBJECT IDLETIME` fallback / `ZSCORE` per-record — still holds (lines 153, 180).
- `access_tracker.py:89-103` — `on_read()` is uncapped/un-TTL'd `RPUSH` — still holds.
- `access_tracker.py:41-52` — `CONFIRM_ACCESS_LUA` caps only the confirmed log via `LTRIM` — still holds.
- `query.py:277-289` — `QueryBuilder.no_track()` exists — still holds.
- `query.py:2320-2431` — `_execute_filter(_no_track=...)`; suppresses `_fire_on_read` at `:2426` — still holds.
- `query.py:1640` / `:1709` — `Query.get()` / `get_many()` fire `_fire_on_read` unconditionally — still holds.
- `query.py:1795-1835` — `Query.all()` (the bare Query, not QueryBuilder) routes through
  `get_many_objects` and does **NOT** call `_fire_on_read` at all — **corrects** the issue's
  framing: the `Query.all()` branch of `_iter_non_semantic` and `_get_all_records` are
  *already* tracking-free. The tracked path is the `QueryBuilder` (`.filter(...).all()`) path.

**Cited sibling issues/PRs re-checked:**
- #396 — introduced MemoryLifecycle; still the basis for the recipe under change.
- #407 — CLOSED (ConfidenceField EMA-vs-Bayesian); sibling audit finding, no overlap.

**Commits on main since issue filed (touching referenced files):** none
(`git log --since=2026-06-11T05:20:35Z` over the three files returns nothing).

**Active plans in `docs/plans/` overlapping this area:** none. `memory_lifecycle.md`
(May 2026) is the original #396 plan, not this bug. No plan touches `access_tracker.py`.

**Notes:** The only correction to the issue's framing is benign: the unfiltered
`Query.all()` paths do not stage reads today; the staging comes exclusively from the
`QueryBuilder` (`.filter(...).all()`) paths used in `_iter_tier` and the *filtered*
branch of `_iter_non_semantic` (active whenever `partition_filters` is set). The fix
must still cover those, and must remain correct if any future read path is added — see
Technical Approach for the belt-and-suspenders class-level switch.

## Prior Art

No prior issues or merged PRs found related to this work (`gh issue list --state closed`
and `gh pr list --state merged` for "no_track / access tracker / lifecycle / staged"
returned empty). The original AccessTracker design
(`docs/plans/access_tracker_mixin.md`) explicitly anticipated this need: *"Opt-out
mechanism: `no_track()` … Needed for internal operations (reindex, migration) that
shouldn't count as reads."* The opt-out shipped; the lifecycle recipe never adopted it.
This plan closes that gap.

## Research

No relevant external findings — this is a purely internal change to popoto's own query
and recipe layers (no external libraries, APIs, or ecosystem patterns involved).
Proceeding with codebase context.

## Data Flow

Tracing one `tick()` and where read-staging is fired:

1. **Entry point**: caller invokes `lifecycle.tick()` (`memory_lifecycle.py:461`).
2. **Promote pass** → `_promote_pass()` → `_iter_tier("episodic")` →
   `self.model_class.query.filter(tier="episodic", **partition_filters).all()`.
   `QueryBuilder.all()` (`query.py:1379`) → `_execute_filter(_no_track=False, ...)`
   (`query.py:1423`) → at `:2426` calls `_fire_on_read()` → `on_read()` `RPUSH` per record.
   **This is staging-source #1.**
3. **Forget pass** → `_forget_pass()` → `_iter_non_semantic()`:
   - If `partition_filters` set → `query.filter(**partition_filters).all()` →
     `QueryBuilder.all()` → fires `_fire_on_read`. **Staging-source #2.**
   - Else → `query.all()` (bare `Query.all()`, `query.py:1795`) → `get_many_objects`,
     **no** `_fire_on_read`. (Already tracking-free, but full second hydration.)
4. **Per-record predicate reads** (no staging, but extra round trips): `_get_tier`
   (attr), `_get_importance_score` → `ZSCORE` (`:180`), `_get_age_seconds` →
   `OBJECT IDLETIME` fallback (`:153`), `access_count`/`last_accessed` → `HGET` on the
   meta hash. These hit Redis per record but do **not** call `on_read`.
5. **Output**: promote re-saves with `migrate_key=True`; forget `delete()`s; summary dict.

The fix must (a) suppress staging at steps 2 and 3, and (b) collapse steps 2 and 3 into
a single hydration pass so the corpus is loaded once.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**:
  - `AccessTrackerMixin.on_read()` gains a bound (cap and/or TTL) — additive, no
    signature change.
  - `Query.get()` / `Query.get_many()` gain an opt-out parameter (`_no_track` / `no_track`)
    — additive with a default that preserves today's tracking behavior.
  - `MemoryLifecycle`'s internal iterators switch to non-tracking reads — internal only.
- **Coupling**: slightly *decreases* — lifecycle no longer implicitly writes into the
  AccessTracker index as a side effect of maintenance.
- **Data ownership**: unchanged. AccessTracker still owns staged/confirmed/meta keys;
  the change is that maintenance reads no longer write them.
- **Reversibility**: high. Each change is a small, independently revertible edit; no data
  migration. A new `_max_staged` / staged-TTL constant can be tuned or removed.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1-2 (confirm the staged-bound policy: cap vs TTL vs both)
- Review rounds: 1

The coding is small and localized (two files, plus tests). The communication cost is in
the one genuine policy decision — how to bound staged lists without breaking
`confirm_access()` semantics — which is the Open Question below.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli ping` | Test suite needs a live server (tests use DB 15) |

Run all checks: `python scripts/check_prerequisites.py docs/plans/lifecycle_tick_bypass_access_tracking.md`

## Solution

### Key Elements

- **Non-tracking internal reads in MemoryLifecycle**: every read `tick()` performs goes
  through a read path that does not stage AccessTracker timestamps.
- **Opt-out coverage on the direct read paths**: extend `no_track` to `Query.get()` and
  `Query.get_many()` so any internal lookup (now or future) can suppress staging — closing
  the gap the issue identifies. (`QueryBuilder.no_track()` already exists.)
- **Single-pass hydration in `tick()`**: load the corpus once and evaluate both promote
  and forget predicates over that pass, instead of two independent `.all()` hydrations.
- **Bounded staged lists**: `on_read()` enforces a documented cap and/or TTL so a record
  read-but-never-confirmed cannot grow without limit (fixes PERF-6 generally).

### Flow

`tick()` called → single non-tracking hydration of the corpus →
for each record evaluate promote-eligibility (episodic only) and forget-eligibility →
apply promotions (save+migrate_key) and forgets (delete) →
return summary. AccessTracker staged keys: **unchanged before vs after**.

### Technical Approach

**1. Make lifecycle reads non-tracking (defense in depth — two layers).**

- *Primary, explicit:* change `_iter_tier` and `_iter_non_semantic`'s `QueryBuilder`
  paths to chain `.no_track()`:
  `self.model_class.query.filter(**filters).no_track().all()`. This already threads
  `_no_track=True` into `_execute_filter` and suppresses `_fire_on_read` at `query.py:2426`.
- *Belt-and-suspenders, future-proof:* MemoryLifecycle reads are *all* internal, so the
  cleanest guarantee is to disable tracking for the duration of the read sweep. Prefer a
  small context-manager/guard that flips the model class's `_track_reads` to `False`
  around the hydration calls (restored in `finally`), OR rely solely on `.no_track()` if
  the team prefers per-query explicitness. **Decision deferred to Open Question #2.**
  The acceptance test (0 staged delta) must pass regardless of which layer is chosen.

**2. Extend opt-out to the direct-read paths.** Add a `no_track: bool = False` (or
`_no_track`) parameter to `Query.get()` (`query.py:1640`) and `Query.get_many()`
(`query.py:1709`) so the `_fire_on_read` calls there become conditional. `Query.all()`
(`query.py:1795`) already does not fire tracking — leave it, but note it in docstrings so
the asymmetry is intentional and documented. This is required by the issue's solution
sketch item 1 even though lifecycle's current hot path does not call `get()/get_many()` —
it removes the latent footgun and gives future internal callers a uniform opt-out.

**3. Single-pass hydration.** Replace the two independent `.all()` hydrations with one.
The promote pass needs episodic records; the forget pass needs all non-semantic records.
Episodic ⊆ non-semantic, so a single non-tracking hydration of all non-semantic records
covers both: promote-evaluate the episodic subset, forget-evaluate the whole set. Restructure
`tick()` (or a new private `_iter_corpus()`) to hydrate once and pass the in-memory list to
both predicate loops, eliminating the second `HGETALL` sweep. The cosmetic `TICK_BATCH_SIZE`
slicing over an already-fully-loaded list should be dropped (it provides no streaming benefit
today); keep `TICK_BATCH_SIZE` as a constant only if a future true-streaming path is intended,
otherwise remove it and update the docstring. **Note:** promotion mutates a record's key
(`migrate_key=True`), and a promoted (now semantic) record must NOT then be forget-evaluated —
preserve the existing semantic-exemption by checking tier *after* any promotion, or by
evaluating forget only on records that were not promoted this pass.

**4. Bound staged lists in `on_read()`.** In `on_read()` (`access_tracker.py:89-103`),
after the `RPUSH`, enforce a bound. Options (Open Question #1):
- **Cap**: `LTRIM staged_key -_max_staged -1` to keep only the most recent N staged
  timestamps. `confirm_access()` already `HINCRBY`s by `#staged` and trims the *confirmed*
  log — capping staged means a never-confirmed record's staged list is bounded, and a
  confirmed record's `access_count` counts only the capped staged window (acceptable; the
  confirmed-log cap of 100 already bounds visibility). Must be a single pipelined/
  atomic-enough op (RPUSH+LTRIM in the same pipeline the hook already uses).
- **TTL**: `EXPIRE staged_key <ttl>` so abandoned staged lists self-clean. Refreshed on
  every read.
- Both are plain Redis commands (Valkey-safe; no modules). A new module-level constant
  (e.g. `_max_staged = 1000` and/or `_staged_ttl_seconds`) is a magic-number tuning knob
  per project convention.
  **Correctness guard:** whatever bound is chosen, `confirm_access()`'s `access_count` and
  `last_accessed` must remain correct *for confirmed reads* — `last_accessed` reads
  `staged[#staged]` (the newest), which an `LTRIM ... -1` preserves; cap only drops the
  oldest staged entries. A regression test must assert confirm semantics under the cap.

## Failure Path Test Strategy

### Exception Handling Coverage
- `tick()`'s promote/forget loops already wrap `_should_promote` / `_should_forget` /
  `save` / `delete` in `except Exception` with `logger.warning` (`memory_lifecycle.py:592`,
  `:614`, `:633`, `:649`). These are observable (warning logged, record skipped) — keep
  them; the single-pass refactor must preserve the per-record try/except so one bad record
  does not abort the sweep. Add/keep a test asserting a record whose predicate raises is
  skipped (warning emitted) and the tick still completes.
- `on_read()` is fire-and-forget; the new bound must not introduce an uncaught path. If a
  pipeline is used, the bound op rides the same pipeline — no new exception surface. No
  `except Exception: pass` introduced.

### Empty/Invalid Input Handling
- Empty corpus: `tick()` over zero records returns `{promoted:0, forgotten:0}` and stages
  nothing — add/keep a test.
- A record without AccessTrackerMixin: `_fire_on_read` already no-ops for non-mixin classes
  (`query.py:93`); `no_track` is harmless there.
- `on_read()` bound on a brand-new staged list (length 1): `LTRIM`/`EXPIRE` must be no-ops
  that don't error.

### Error State Rendering
- Not user-facing UI. The observable "error state" is the warning log on a skipped record;
  covered above.

## Test Impact

- `tests/test_access_tracker.py` — UPDATE: existing tests assert exact staged lengths
  (e.g. `len(staged) == 5` after 5 reads, line ~106). If a staged **cap** is chosen with
  `_max_staged` well above test sizes, these stay green; if the cap is set low, update the
  affected assertions. Add NEW tests: (a) staged list bounded after `> _max_staged` reads;
  (b) `confirm_access()` count/last_accessed correct under the cap; (c) `no_track` suppresses
  staging via `Query.get()` and `Query.get_many()` (new opt-out coverage).
- `tests/test_memory_lifecycle.py` — UPDATE/ADD: new test asserting a `tick()` over a
  seeded `AccessTrackerMixin` corpus produces 0 staged delta; new test asserting single-pass
  hydration (instrumented `HGETALL` count ≤ corpus size, via `INFO commandstats` or a query
  counter); behavior-parity test confirming promote/forget cohorts are unchanged.
- No tests deleted. No public behavior of `Query.all()` changes.

## Rabbit Holes

- **The O(N) → server-side Lua top-K rework.** Explicitly out of scope (20k target).
  Do NOT pipeline-optimize or rewrite composite scoring / tier scans beyond removing the
  duplicate hydration. Cheap pipelining of the per-record `ZSCORE`/`HGET`/`OBJECT IDLETIME`
  is *optional* and only if trivially low-effort; do not let it expand scope.
- **Reworking `confirm_access()` Lua.** The fix bounds the *staged* list; do not touch the
  confirmed-log cap logic or the Lua's count math beyond what a staged cap forces.
- **Streaming/SCAN-based iteration.** Tempting given "batching is cosmetic," but a true
  streaming iterator is a larger change than this bug needs at 20k scale. Single in-memory
  pass is sufficient; drop the fake batching rather than build real batching.
- **LIFECYCLE-1 semantics** (promotion is a relabel, importance is pure recency,
  `OBJECT IDLETIME` ≠ age). Related audit context, not this issue. Leave alone.

## Risks

### Risk 1: Staged cap/TTL silently changes confirm_access() semantics for confirmed reads
**Impact:** A record confirmed after heavy reading could under-count `access_count` or get
a wrong `last_accessed` if the bound drops the wrong entries.
**Mitigation:** Cap from the old end only (`LTRIM key -N -1`), preserving the newest entry
that `last_accessed` reads. Add a regression test asserting count/last_accessed correctness
under the cap. Choose `_max_staged` high enough that ordinary confirm cycles are unaffected.

### Risk 2: Single-pass refactor changes promote/forget decisions
**Impact:** A promoted record gets forget-evaluated in the same tick, or a non-semantic
record is skipped.
**Mitigation:** Preserve the semantic-exemption ordering: evaluate forget only on records
not promoted this pass (or re-check tier post-promotion). Behavior-parity test over known
promotable/forgettable cohorts (as in the issue PoC) must pass unchanged.

### Risk 3: Existing test_access_tracker assertions on exact staged length break
**Impact:** Red suite.
**Mitigation:** Set `_max_staged` above the sizes used in existing tests, or update those
assertions deliberately (listed in Test Impact). Reviewer confirms intent.

## Race Conditions

### Race 1: Concurrent ticks both hydrate and mutate the same record
**Location:** `memory_lifecycle.py:_promote_pass` / `_forget_pass` (post-refactor single pass).
**Trigger:** Two `tick()`s run concurrently over the same corpus.
**Data prerequisite:** Both read the same record before either mutates it.
**State prerequisite:** Promotion (`save(migrate_key=True)`) and forget (`delete()`) are
already idempotent at the record level per the existing `tick()` docstring (second write/
delete is a no-op).
**Mitigation:** No new race introduced — the single-pass change does not add shared mutable
state beyond the existing per-record idempotent ops. The non-tracking read removes writes to
the staged keys, *reducing* contention on AccessTracker keys. Keep the existing per-record
try/except so a record deleted by a concurrent tick mid-pass is skipped, not fatal.

### Race 2: on_read() RPUSH+LTRIM not atomic across concurrent readers
**Location:** `access_tracker.py:on_read`.
**Trigger:** Two readers stage the same record's staged list simultaneously.
**Data prerequisite:** Both push before either trims.
**State prerequisite:** Final staged length should be ≤ `_max_staged`.
**Mitigation:** Issue RPUSH and LTRIM in the same pipeline (the hook already builds a
pipeline in `_fire_on_read`). Interleaving may briefly exceed the cap between two readers'
ops, but each LTRIM re-bounds to N — eventual length is bounded. This is acceptable for a
cap whose purpose is preventing unbounded growth, not exact-length guarantees. TTL, if
chosen, is order-independent. Document this best-effort bound.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #413] The O(N) → server-side Lua top-K rework of composite scoring and
  tier scans is the deferred performance item; this plan only removes the duplicate
  hydration and the self-pollution. (Tracked by the scope boundary in #413 itself, which
  remains open until the implementation PR merges; the deferred rework is explicitly
  out of this plan per the maintainer's 20k-scale decision.)
- [SEPARATE-SLUG #413] LIFECYCLE-1 semantic concerns (promotion-as-relabel, recency-as-
  importance, IDLETIME-as-age) are related audit context, not this fix.

## Update System

No update-system changes required — this is a purely internal library change to popoto's
query and recipe modules; it ships as part of the normal package release.

## Agent Integration

No agent integration required — MemoryLifecycle and AccessTrackerMixin are library
primitives consumed in-process by popoto users; there is no MCP server or Telegram bridge
in this repo. (The `## Agent Integration` template section refers to a different repo's
architecture; popoto has no `.mcp.json` / bridge.)

## Documentation

### Feature Documentation
- [ ] Update `docs/agent-memory.md` (or the MemoryLifecycle section) to state that `tick()`
      reads are non-tracking and leave no AccessTracker trace.
- [ ] Update `docs/query.md` to document the new `no_track` opt-out on `Query.get()` /
      `Query.get_many()` and note that `Query.all()` is non-tracking by design.

### External Documentation Site
- [ ] `mkdocs build --strict` passes (docs gate in `scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Docstrings: `on_read()` documents the staged bound/TTL and the chosen constant;
      `tick()`/`_iter_*` docstrings updated to reflect single-pass + non-tracking;
      `Query.get()/get_many()` document the new opt-out.

## Success Criteria

- [ ] Over a seeded `AccessTrackerMixin` corpus, one `tick()` produces **0** new staged
      entries: `$AT:*:staged:*` key count and total staged list length identical before and
      after (regression test in `tests/test_memory_lifecycle.py`).
- [ ] Each record is hydrated at most once per `tick()` (instrumented query/`HGETALL` count
      ≤ corpus size in the test).
- [ ] Staged lists are bounded by a documented cap and/or TTL; `confirm_access()`
      `access_count`/`last_accessed` remain correct under the bound (test).
- [ ] Promote/forget decisions unchanged on a fixture corpus with known cohorts (parity test).
- [ ] `Query.get()` / `Query.get_many()` accept an opt-out that suppresses staging (test).
- [ ] No Redis-module commands introduced (plain commands + Lua only; Valkey-compatible).
- [ ] Steady-state no-op `tick()` issues measurably fewer Redis commands than baseline at the
      same corpus size (double hydration removed).
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

The lead agent orchestrates; it does not build directly.

### Team Members

- **Builder (access-tracker)**
  - Name: at-builder
  - Role: Bound staged lists in `on_read()`; add `no_track` opt-out to `Query.get()`/`get_many()`
  - Agent Type: builder
  - Resume: true

- **Builder (lifecycle)**
  - Name: lifecycle-builder
  - Role: Non-tracking reads + single-pass hydration in `MemoryLifecycle.tick()`
  - Agent Type: builder
  - Resume: true

- **Test engineer**
  - Name: tick-tester
  - Role: 0-staged-delta, single-pass, parity, bound-correctness, opt-out tests
  - Agent Type: test-engineer
  - Resume: true

- **Validator**
  - Name: tick-validator
  - Role: Verify all success criteria + full suite + Valkey-safety grep
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: tick-docs
  - Role: Update agent-memory.md, query.md, docstrings
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Bound staged lists + extend opt-out
- **Task ID**: build-access-tracker
- **Depends On**: none
- **Validates**: tests/test_access_tracker.py
- **Assigned To**: at-builder
- **Agent Type**: builder
- **Parallel**: true
- In `access_tracker.py:on_read()`, after RPUSH, enforce the staged bound (cap via
  `LTRIM key -_max_staged -1` and/or `EXPIRE`), in the same pipeline when one is passed.
  Add the `_max_staged` (and/or `_staged_ttl_seconds`) class/module constant with a docstring.
- In `query.py`, add an opt-out param to `Query.get()` (`:1640`) and `Query.get_many()`
  (`:1709`) making `_fire_on_read` conditional; default preserves current tracking behavior.
- Update docstrings.

### 2. Non-tracking single-pass tick()
- **Task ID**: build-lifecycle
- **Depends On**: none
- **Validates**: tests/test_memory_lifecycle.py
- **Assigned To**: lifecycle-builder
- **Agent Type**: builder
- **Parallel**: true
- Chain `.no_track()` on the `QueryBuilder` reads in `_iter_tier` / `_iter_non_semantic`
  (and/or guard `_track_reads=False` around the sweep — per Open Question #2).
- Collapse the two `.all()` hydrations into a single non-tracking hydration of the corpus;
  evaluate promote (episodic subset) and forget (non-promoted non-semantic) over that one pass.
- Drop the cosmetic `TICK_BATCH_SIZE` slicing (or keep the constant only if justified);
  preserve the semantic-exemption ordering and per-record try/except.

### 3. Tests
- **Task ID**: build-tests
- **Depends On**: build-access-tracker, build-lifecycle
- **Assigned To**: tick-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- 0-staged-delta tick test; single-pass hydration count test; promote/forget parity test;
  staged-bound + confirm-correctness test; `no_track` opt-out on `get()`/`get_many()` test;
  empty-corpus and predicate-raises tests.

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: build-access-tracker, build-lifecycle, build-tests
- **Assigned To**: tick-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/agent-memory.md`, `docs/query.md`; verify `mkdocs build --strict`.

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-tests, document-feature
- **Assigned To**: tick-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full suite; verify every success criterion; grep for Redis-module commands; report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lifecycle tests pass | `pytest tests/test_memory_lifecycle.py tests/test_access_tracker.py -q` | exit code 0 |
| Lifecycle uses no_track | `grep -c "no_track\|_track_reads" src/popoto/recipes/memory_lifecycle.py` | output > 0 |
| Staged list bounded in on_read | `grep -c "ltrim\|expire" src/popoto/fields/access_tracker.py` | output > 0 |
| No Redis-module commands added | `grep -rniE "BF\.\|CMS\.\|TOPK\.\|CF\.\|FT\.\|TS\.\|JSON\." src/popoto/recipes/memory_lifecycle.py src/popoto/fields/access_tracker.py` | match count == 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

1. **Staged-bound policy:** cap (`LTRIM`), TTL (`EXPIRE`), or both? A cap gives a hard
   length bound but changes `access_count` for never-confirmed records that exceed it; a TTL
   self-cleans abandoned lists but does not bound a continuously-read record. Recommendation:
   **both** — a generous `_max_staged` cap (e.g. 1000) plus a TTL — but confirm the policy
   and constant values (these are tuning magic numbers).
2. **Non-tracking mechanism in MemoryLifecycle:** explicit `.no_track()` on each query, or a
   `_track_reads=False` guard around the whole sweep (more robust to future read paths)? The
   plan recommends the guard as belt-and-suspenders plus `.no_track()` for clarity; confirm
   the team is happy carrying both, or pick one.
3. **`TICK_BATCH_SIZE`:** remove the now-useless constant entirely, or retain it for a future
   true-streaming iterator? Removing it is cleaner; retaining signals future intent.
