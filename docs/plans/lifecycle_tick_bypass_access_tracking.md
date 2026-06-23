---
status: Ready
type: bug
appetite: Medium
owner: valorengels
created: 2026-06-23
tracking: https://github.com/tomcounsell/popoto/issues/413
last_comment_id:
revision_applied: true
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

**Why single-pass hydration stays in scope for THIS bug (not scope-creep).** Single-pass is
not a speculative performance optimization — it is the *mechanism* that satisfies the issue's
own acceptance criterion "each record hydrated at most once per tick." The double hydration is
the literal cause of the double tracking: today `_iter_tier` and `_iter_non_semantic` each call
`.all()`, and each tracked `.all()` fires a staged read per hydrated record, so the corpus is
both hydrated twice *and* staged twice per tick. Collapsing to one pass is therefore the minimal
change that removes the duplicate hydration the issue explicitly calls out; suppressing tracking
(`.no_track()`) on a *still-doubled* hydration would leave the "hydrated at most once" criterion
unmet. The two changes are coupled by the bug, not bundled for convenience. (It *could* in
principle be split into a separate slug, but doing so would leave this issue's acceptance
criterion only half-met, so it is kept here.)

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
  - `AccessTrackerMixin.on_read()` gains a TTL refresh (`EXPIRE`) — additive, no signature
    change. New class constant `_staged_ttl_seconds`.
  - `Query.get()` / `Query.get_many()` — **no change** (Decision 2; lifecycle never calls
    them).
  - `MemoryLifecycle`'s internal iterators switch to `.no_track()` reads — internal only.
- **Coupling**: slightly *decreases* — lifecycle no longer implicitly writes into the
  AccessTracker index as a side effect of maintenance.
- **Data ownership**: unchanged. AccessTracker still owns staged/confirmed/meta keys;
  the change is that maintenance reads no longer write them.
- **Reversibility**: high. Each change is a small, independently revertible edit; no data
  migration. The new `_staged_ttl_seconds` constant can be tuned or removed.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0 (all three open questions resolved in this revision)
- Review rounds: 1

The coding is small and localized (two files, plus tests). The one genuine policy decision —
how to bound staged lists without breaking `confirm_access()` semantics — is resolved:
**TTL only, no cap** (Decision 1).

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli ping` | Test suite needs a live server (tests use DB 15) |

Run all checks: `python scripts/check_prerequisites.py docs/plans/lifecycle_tick_bypass_access_tracking.md`

## Solution

### Key Elements

- **Non-tracking internal reads in MemoryLifecycle**: every read `tick()` performs goes
  through a read path that does not stage AccessTracker timestamps.
- **Opt-out via the existing `QueryBuilder.no_track()`**: lifecycle's `.filter().all()`
  reads chain `.no_track()`; `Query.get()/get_many()` are not modified (Decision 2).
- **Single-pass hydration in `tick()`**: load the corpus once and evaluate both promote
  and forget predicates over that pass, instead of two independent `.all()` hydrations,
  with a re-check-tier-before-delete guard for the concurrent-tick window (Decision 3).
- **TTL-bounded staged lists**: `on_read()` refreshes an `EXPIRE` on the staged key so a
  record read-but-never-confirmed self-cleans; no cap, so `access_count` is unaffected
  (Decision 1, fixes PERF-6's actual unbounded case).

### Flow

`tick()` called → single non-tracking hydration of the corpus →
for each record evaluate promote-eligibility (episodic only) and forget-eligibility →
apply promotions (save+migrate_key) and forgets (delete) →
return summary. AccessTracker staged keys: **unchanged before vs after**.

### Technical Approach

**1. Make lifecycle reads non-tracking via `.no_track()` only (Decision 2).**
Change `_iter_tier` and `_iter_non_semantic`'s `QueryBuilder` paths to chain `.no_track()`:
`self.model_class.query.filter(**filters).no_track().all()`. This already threads
`_no_track=True` into `_execute_filter` and suppresses `_fire_on_read` at `query.py:2426`.
This is the **only** mechanism — no class-level `_track_reads=False` guard (rejected:
mutates process-wide class state, unsafe under concurrency). The unfiltered `Query.all()`
branch of `_iter_non_semantic` is already tracking-free (`query.py:1795-1835`), so after the
single-pass refactor folds both paths into one `.filter().no_track().all()` (or the
already-untracked bare `.all()`), every read `tick()` performs is non-staging. The
acceptance test asserts a 0 staged delta over a full tick.

**2. No changes to `Query.get()/get_many()` (Decision 2, narrowing Concern 3).**
The opt-out is scoped to the QueryBuilder `.filter().all()` path that lifecycle actually
uses. `Query.get()` (`query.py:1640`) and `Query.get_many()` (`query.py:1709`) are **left
untouched** — lifecycle never calls them, so adding `_no_track` parameters there is
over-scope. `Query.all()` (`query.py:1795`) already does not fire tracking; a one-line
docstring note records that this asymmetry is intentional (no signature change). A future
internal caller needing an opt-out on `get()/get_many()` adds it then, with its own test.

**3. Single-pass hydration with a re-check-tier-before-delete guard (Concern 2).**
Replace the two independent `.all()` hydrations with one. Promote needs episodic records;
forget needs all non-semantic records; episodic ⊆ non-semantic, so one non-tracking
hydration of all non-semantic records covers both: promote-evaluate the episodic subset,
forget-evaluate the rest. Restructure `tick()` (or a new private `_iter_corpus()`) to
hydrate once and pass the in-memory list to both predicate loops, eliminating the second
`HGETALL` sweep.

*Intra-tick correctness:* promotion mutates a record's key (`migrate_key=True`); a promoted
(now-semantic) record must NOT then be forget-evaluated. Track the set of records promoted
this pass and exclude them from forget-evaluation (or re-check tier on the in-memory object
after promotion). This preserves the existing semantic-exemption.

*Inter-tick race (Concern 2 — the single snapshot widens the window).* Today `_forget_pass`
re-hydrates fresh records, so a record promoted or deleted by a concurrent tick is re-read
and reflects current state before the delete decision. A single snapshot taken once at the
top makes the forget decision on a *stale* in-memory record — a record another tick already
promoted to semantic, or already deleted, could be deleted/double-deleted based on stale
tier. **Guard:** immediately before `record.delete()` in the forget branch, re-read the
record's authoritative tier from Redis (a single `HGET`/attr read on the live key, cheap and
already in the per-record budget) and skip the delete if the tier is now `semantic` or the
key no longer exists. This restores the freshness the per-pass re-hydration used to provide,
scoped to the one decision that is destructive. The existing per-record try/except still
catches a key deleted by a concurrent tick mid-delete (idempotent no-op). The plan no longer
claims "no new race" — it claims the guard closes the window the snapshot would otherwise
widen.

**4. Bound staged lists in `on_read()` via TTL only (Decision 1, resolving Concern 1).**
In `on_read()` (`access_tracker.py:89-103`), after the `RPUSH`, `EXPIRE` the staged key with
`_staged_ttl_seconds` (refreshed on every read). **No `LTRIM` cap** — a cap would feed a
truncated `#staged` into `confirm_access()`'s `HINCRBY access_count, #staged`
(`access_tracker.py:48`), permanently under-counting confirmed reads for any record read more
than the cap between confirmations (Concern 1). A TTL bounds storage for the only unbounded
case PERF-6 actually describes — records read but **never confirmed** — without touching the
count or `last_accessed` math. `EXPIRE` is a plain Redis command (Valkey-safe). When a
pipeline is passed, the `EXPIRE` rides the same pipeline as the `RPUSH`; otherwise it is a
second direct call (fire-and-forget, same as today's `RPUSH`).
Constant: `_staged_ttl_seconds = 86400` on `AccessTrackerMixin`, documented in the
`on_read()` docstring as a magic-number tuning knob.
**Correctness:** because no entries are dropped, `confirm_access()`'s `access_count` and
`last_accessed` (which reads `staged[#staged]`, the newest) are provably unchanged. A
regression test asserts confirm semantics are identical with and without the TTL.

## Failure Path Test Strategy

### Exception Handling Coverage
- `tick()`'s promote/forget loops already wrap `_should_promote` / `_should_forget` /
  `save` / `delete` in `except Exception` with `logger.warning` (`memory_lifecycle.py:592`,
  `:614`, `:633`, `:649`). These are observable (warning logged, record skipped) — keep
  them; the single-pass refactor must preserve the per-record try/except so one bad record
  does not abort the sweep. Add/keep a test asserting a record whose predicate raises is
  skipped (warning emitted) and the tick still completes.
- `on_read()` is fire-and-forget; the new `EXPIRE` must not introduce an uncaught path. If a
  pipeline is used, the `EXPIRE` rides the same pipeline — no new exception surface. No
  `except Exception: pass` introduced.
- The re-check-tier-before-delete guard (Technical Approach §3) is a read on the live key; if
  the key is already gone (concurrent delete) the read returns nothing and the guard skips
  the delete — no exception. Still wrapped by the existing per-record try/except.

### Empty/Invalid Input Handling
- Empty corpus: `tick()` over zero records returns `{promoted:0, forgotten:0}` and stages
  nothing — add/keep a test.
- A record without AccessTrackerMixin: `_fire_on_read` already no-ops for non-mixin classes
  (`query.py:93`); `no_track` is harmless there.
- `on_read()` `EXPIRE` on a brand-new staged list (length 1): setting a TTL on a key that
  exists is valid and idempotent; harmless.

### Error State Rendering
- Not user-facing UI. The observable "error state" is the warning log on a skipped record;
  covered above.

## Test Impact

- `tests/test_access_tracker.py` — existing tests assert exact staged lengths
  (e.g. `len(staged) == 5` after 5 reads, line ~106). Because the bound is **TTL only, no
  cap** (Decision 1), staged lengths are unchanged → these stay green. Add NEW tests:
  (a) staged key has a TTL set/refreshed after `on_read()` (`TTL key` in range, `> 0`,
  `≤ _staged_ttl_seconds`); (a2) staged key has a TTL after a **pipelined** `on_read()` (the
  hot path where a pipeline is passed in and executed) — asserts the EXPIRE rides the same
  pipeline as the RPUSH, so the pipelined branch cannot silently leave the staged list
  unbounded; (b) `confirm_access()` `access_count` and `last_accessed` are
  **identical** with and without the TTL across `> _max_access_log` reads (Concern 1
  regression — proves the bound does not corrupt the count).
- `tests/test_memory_lifecycle.py` — UPDATE/ADD: (a) `tick()` over a seeded
  `AccessTrackerMixin` corpus produces 0 staged delta (`$AT:*:staged:*` key count and total
  staged length identical before/after); (b) single-pass hydration (instrumented `HGETALL`
  count ≤ corpus size, via `INFO commandstats` or a query counter); (c) promote/forget
  parity over known cohorts; (d) **concurrent-tick guard** (Concern 2): seed a record,
  simulate a concurrent promotion-to-semantic between snapshot and delete (e.g. flip the
  tier in Redis directly, or monkeypatch the corpus to be stale), assert the forget pass
  re-reads tier and does NOT delete the now-semantic record.
- No new test on `Query.get()/get_many()` (Decision 2 — those paths are unchanged).
- `tests/test_memory_lifecycle.py::test_tick_batch_pagination` (`:353`) — **REWRITE, do not
  delete.** This test sets `lifecycle.TICK_BATCH_SIZE = 100` (`:359`) and is built around the
  batch-pagination semantics that the single-pass refactor eliminates (Decision 3 removes
  `TICK_BATCH_SIZE` entirely). The valuable assertion it carries — *a 200-record corpus is
  fully and correctly promoted in one `tick()`* — is preserved and now exercises **single-pass
  hydration** instead of batch pagination: drop the `lifecycle.TICK_BATCH_SIZE = 100` line,
  rename the test to `test_tick_single_pass_full_corpus` (or keep the name but update the
  docstring to "200 records promote correctly in a single hydration pass"), and keep the
  remaining body unchanged (seed 200 episodic records, `tick()`, assert `promoted == 200`,
  assert 0 remain episodic and 200 are semantic). Rewriting (not deleting) keeps full-corpus
  coverage while removing the dependency on the deleted constant. This is the only test that
  references `TICK_BATCH_SIZE`.
- No tests deleted. No public behavior of `Query.all()` / `Query.get()` / `Query.get_many()`
  changes.

## Rabbit Holes

- **The O(N) → server-side Lua top-K rework.** Explicitly out of scope (20k target).
  Do NOT pipeline-optimize or rewrite composite scoring / tier scans beyond removing the
  duplicate hydration. Cheap pipelining of the per-record `ZSCORE`/`HGET`/`OBJECT IDLETIME`
  is *optional* and only if trivially low-effort; do not let it expand scope.
- **Reworking `confirm_access()` Lua.** The fix bounds the *staged* list via TTL only; do
  NOT touch the confirmed-log cap logic or the Lua's count math at all — the TTL approach
  was chosen precisely so the Lua needs no change (Decision 1).
- **Streaming/SCAN-based iteration.** Tempting given "batching is cosmetic," but a true
  streaming iterator is a larger change than this bug needs at 20k scale. Single in-memory
  pass is sufficient; drop the fake batching rather than build real batching.
- **LIFECYCLE-1 semantics** (promotion is a relabel, importance is pure recency,
  `OBJECT IDLETIME` ≠ age). Related audit context, not this issue. Leave alone.

## Risks

### Risk 1: Staged TTL changes confirm_access() semantics for confirmed reads
**Impact:** If the bound dropped entries, a confirmed record could under-count
`access_count` (Concern 1) or get a wrong `last_accessed`.
**Mitigation:** The bound is **TTL only, no cap** (Decision 1) — `EXPIRE` drops *no*
entries while the list is live, so `confirm_access()`'s `HINCRBY access_count, #staged` and
`HSET last_accessed, staged[#staged]` are provably unchanged. The only effect is that an
abandoned (never-confirmed) staged list evaporates after `_staged_ttl_seconds`, which by
definition was never going to be confirmed. Regression test asserts count/last_accessed are
identical with and without the TTL.

### Risk 2: Single-pass refactor changes promote/forget decisions (intra-tick)
**Impact:** A promoted record gets forget-evaluated in the same tick, or a non-semantic
record is skipped.
**Mitigation:** Track records promoted this pass and exclude them from forget-evaluation
(or re-check tier on the in-memory object post-promotion). Behavior-parity test over known
promotable/forgettable cohorts (as in the issue PoC) must pass unchanged.

### Risk 3: Single snapshot makes stale forget decisions (inter-tick) — Concern 2
**Impact:** With a one-shot snapshot, a record another concurrent tick already promoted to
semantic or already deleted could be deleted/double-deleted from stale in-memory state —
the single pass *widens* the window the old per-pass re-hydration covered.
**Mitigation:** Re-check the record's authoritative tier (and existence) from Redis
immediately before `record.delete()`; skip if now-semantic or gone (Technical Approach §3).
Existing per-record try/except absorbs a mid-delete concurrent removal. Dedicated test
simulates the stale-snapshot case and asserts no erroneous delete.

### Risk 4: Existing test_access_tracker assertions on exact staged length break
**Impact:** Red suite.
**Mitigation:** With TTL-only (no cap), staged *lengths* are unchanged, so the existing
exact-length assertions stay green. No deliberate assertion edits needed; the new tests only
add TTL-presence and count-parity checks.

## Race Conditions

### Race 1: Concurrent ticks both hydrate and mutate the same record
**Location:** `memory_lifecycle.py:_promote_pass` / `_forget_pass` (post-refactor single pass).
**Trigger:** Two `tick()`s run concurrently over the same corpus.
**Data prerequisite:** Both read the same record before either mutates it.
**State prerequisite:** Promotion (`save(migrate_key=True)`) and forget (`delete()`) are
already idempotent at the record level per the existing `tick()` docstring (second write/
delete is a no-op).
**Mitigation (Concern 2 — revised; the plan no longer claims "no new race"):** The
single-pass snapshot *does* widen the window vs. today's per-pass re-hydration: the forget
decision is made on a record snapshotted once at the top, which a concurrent tick may have
since promoted to semantic or deleted. Mitigation is an explicit **re-check-tier-before-
delete guard** — re-read the record's authoritative tier and existence from Redis right
before `record.delete()`, and skip if it is now `semantic` or gone. This restores the
freshness the old re-hydration provided, scoped to the single destructive decision. The
non-tracking read additionally removes writes to the staged keys, *reducing* contention on
AccessTracker keys. The existing per-record try/except still absorbs a record deleted by a
concurrent tick between the guard read and the delete (idempotent no-op).

### Race 2: on_read() RPUSH+EXPIRE across concurrent readers
**Location:** `access_tracker.py:on_read`.
**Trigger:** Two readers stage the same record's staged list simultaneously.
**Data prerequisite:** Both push and both refresh the TTL.
**State prerequisite:** The staged key carries a TTL ≤ `_staged_ttl_seconds`.
**Mitigation:** With TTL-only (Decision 1), there is effectively no race — `EXPIRE` is
order-independent and idempotent; whichever reader runs last simply refreshes the same TTL,
and no entries are dropped, so concurrent readers cannot corrupt the count (unlike a cap,
which this design deliberately avoids). When a pipeline is present, RPUSH and EXPIRE ride it
together; otherwise both are direct calls, same as today's RPUSH.

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
- [ ] Update `docs/features/agent-memory.md` (the MemoryLifecycle section) to state that
      `tick()` reads are non-tracking and leave no AccessTracker trace. (Path per
      `mkdocs.yml:38`; a top-level `docs/agent-memory.md` would be an orphan page and fail
      `mkdocs build --strict`.)
- [ ] Update `docs/query.md` to note that `QueryBuilder.no_track()` (the `.filter().all()`
      path) suppresses read staging and that `Query.all()` is non-tracking by design. No
      `Query.get()/get_many()` opt-out is added (Decision 2).

### External Documentation Site
- [ ] `mkdocs build --strict` passes (docs gate in `scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Docstrings: `on_read()` documents the staged TTL and `_staged_ttl_seconds`;
      `tick()`/`_iter_*` docstrings updated to reflect single-pass + non-tracking + the
      re-check-tier-before-delete guard; `Query.all()` docstring notes it is non-tracking
      by design (no `Query.get()/get_many()` change).

## Success Criteria

- [ ] Over a seeded `AccessTrackerMixin` corpus, one `tick()` produces **0** new staged
      entries: `$AT:*:staged:*` key count and total staged list length identical before and
      after (regression test in `tests/test_memory_lifecycle.py`).
- [ ] Each record is hydrated at most once per `tick()` (instrumented query/`HGETALL` count
      ≤ corpus size in the test).
- [ ] Staged lists carry a refreshed TTL (`_staged_ttl_seconds`) on **both** the direct and
      the pipelined `on_read()` paths (the pipelined path is the hot path and must not leave the
      staged list unbounded); `confirm_access()` `access_count`/`last_accessed` are **identical**
      with and without the TTL (Concern 1 regression test).
- [ ] Promote/forget decisions unchanged on a fixture corpus with known cohorts (parity test).
- [ ] Concurrent-tick guard: a record promoted-to-semantic between snapshot and delete is
      NOT forgotten (Concern 2 test).
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
  - Role: TTL-bound staged lists in `on_read()` (no cap); docstring note on `Query.all()`
  - Agent Type: builder
  - Resume: true

- **Builder (lifecycle)**
  - Name: lifecycle-builder
  - Role: Non-tracking reads + single-pass hydration in `MemoryLifecycle.tick()`
  - Agent Type: builder
  - Resume: true

- **Test engineer**
  - Name: tick-tester
  - Role: 0-staged-delta, single-pass, parity, TTL-present, confirm-count-parity, concurrent-guard tests
  - Agent Type: test-engineer
  - Resume: true

- **Validator**
  - Name: tick-validator
  - Role: Verify all success criteria + full suite + Valkey-safety grep
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: tick-docs
  - Role: Update docs/features/agent-memory.md, docs/query.md, docstrings
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
- In `access_tracker.py:on_read()`, after RPUSH, `EXPIRE` the staged key with
  `_staged_ttl_seconds` (refreshed each read), in the same pipeline when one is passed.
  **No `LTRIM` cap** (Decision 1 — a cap corrupts `access_count`). Add the
  `_staged_ttl_seconds` class constant on `AccessTrackerMixin` with a docstring.
- Do **not** modify `Query.get()/get_many()` (Decision 2). Add a one-line docstring note on
  `Query.all()` recording that it is non-tracking by design.
- Update docstrings.

### 2. Non-tracking single-pass tick()
- **Task ID**: build-lifecycle
- **Depends On**: none
- **Validates**: tests/test_memory_lifecycle.py
- **Assigned To**: lifecycle-builder
- **Agent Type**: builder
- **Parallel**: true
- Chain `.no_track()` on the `QueryBuilder` reads in `_iter_tier` / `_iter_non_semantic`
  (Decision 2 — `.no_track()` only; no `_track_reads=False` class-level guard).
- Collapse the two `.all()` hydrations into a single non-tracking hydration of the corpus;
  evaluate promote (episodic subset) and forget (non-promoted non-semantic) over that one pass.
- Add the **re-check-tier-before-delete guard** (Concern 2): re-read tier/existence from
  Redis immediately before `record.delete()`, skip if now-semantic or gone.
- Remove `TICK_BATCH_SIZE` entirely (Decision 3) and its slicing/docstring references;
  preserve the semantic-exemption ordering and per-record try/except.

### 3. Tests
- **Task ID**: build-tests
- **Depends On**: build-access-tracker, build-lifecycle
- **Assigned To**: tick-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- 0-staged-delta tick test; single-pass hydration count test; promote/forget parity test;
  staged-TTL-present test (assert the staged key has a TTL after a plain `on_read()`); staged-
  TTL-present-on-pipelined-read test (assert the staged key has a TTL after an `on_read()` that
  rides a passed-in pipeline — the hot path — so the pipelined branch cannot silently stay
  unbounded, Concern from critique); confirm_access count/last_accessed parity with-vs-without
  TTL test (Concern 1); concurrent-tick re-check-before-delete guard test (Concern 2); empty-
  corpus and predicate-raises tests. (No `get()/get_many()` opt-out test — Decision 2.)
- **Rewrite** `test_tick_batch_pagination` → single-pass full-corpus test (drop the
  `TICK_BATCH_SIZE = 100` line; assert 200 records promote in one pass). See Test Impact.

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: build-access-tracker, build-lifecycle, build-tests
- **Assigned To**: tick-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/agent-memory.md`, `docs/query.md`; verify `mkdocs build --strict`.

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-tests, document-feature
- **Assigned To**: tick-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full suite; verify every success criterion; run the Valkey-safety grep from the
  Verification section (the `grep -E '\b(BF|CMS|...)\.[A-Z]'` form — NOT the `\|` form, which
  matches nothing under `-E` and passes vacuously); report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lifecycle tests pass | `pytest tests/test_memory_lifecycle.py tests/test_access_tracker.py -q` | exit code 0 |
| Lifecycle uses no_track | `grep -c "no_track" src/popoto/recipes/memory_lifecycle.py` | output > 0 |
| No class-level guard introduced | `grep -c "_track_reads *= *False" src/popoto/recipes/memory_lifecycle.py` | output == 0 |
| Staged list TTL-bounded in on_read | `grep -ci "expire" src/popoto/fields/access_tracker.py` | output > 0 |
| No LTRIM cap added to on_read | `grep -n "ltrim" src/popoto/fields/access_tracker.py` | only the pre-existing confirmed-log LTRIM (line ~47), none in on_read |
| TICK_BATCH_SIZE removed | `grep -c "TICK_BATCH_SIZE" src/popoto/recipes/memory_lifecycle.py` | output == 0 |
| No Redis-module commands added | see the Valkey-safety grep below the table (pipes can't render inside a markdown table cell) | exit 1 / 0 matches |
| Docs build | `mkdocs build --strict` | exit code 0 |

**Valkey-safety grep** (kept outside the table so the `|` alternation is literal and copy-pasteable
— under `grep -E` you write `a|b`, NOT `a\|b`; the earlier `\|` form never matched and passed
vacuously). The pattern anchors on a word boundary and an uppercase command suffix so it catches
real module commands (`BF.ADD`, `JSON.SET`, `FT.SEARCH`) without flagging prose like "lifecycle"
or "FT" inside identifiers:

```bash
grep -rnE '\b(BF|CMS|TOPK|CF|FT|TS|JSON)\.[A-Z]' \
  src/popoto/recipes/memory_lifecycle.py src/popoto/fields/access_tracker.py
# Expected: no output, exit 1 (zero matches). Verified clean against the current worktree.
```

## Critique Results

Critique returned **NEEDS REVISION** (1 blocker, 4 concerns, 3 open questions). This
revision resolves all of them. Summary of dispositions:

- **BLOCKER — wrong docs path** (`docs/agent-memory.md` → `docs/features/agent-memory.md`,
  3 places). Fixed in Documentation, Team Members, and Step 4; verified against
  `mkdocs.yml:38`.
- **CONCERN 1 — `access_count` under-count under a staged cap.** Resolved by **dropping the
  cap entirely** and bounding staged lists with a **TTL only** (Decision 1 below). A cap
  would feed a truncated `#staged` into `confirm_access()`'s `HINCRBY access_count, #staged`
  (`access_tracker.py:48`), permanently under-counting confirmed reads for any record read
  more than the cap between confirmations. TTL does not touch the count math.
- **CONCERN 2 — single-pass snapshot widens the concurrent promote/delete race.** Resolved
  by a **re-check-tier-before-delete guard** plus a saved-existence check (Decision-driven;
  see Technical Approach §3 and Race 1). The plan no longer claims "no new race"; it adds an
  explicit guard that closes the widened window.
- **CONCERN 3 — `no_track` on `get()/get_many()` is over-scope.** Resolved: opt-out is
  **narrowed to the QueryBuilder `.filter().all()` path** that lifecycle actually uses (which
  already supports `.no_track()`). `Query.get()/get_many()` are **not** modified (see
  Decision 2). Removes the over-scope and the latent API churn.
- **CONCERN 4 — `_track_reads=False` class-level guard mutates process-wide state.**
  Resolved: **rejected the class-level guard; `.no_track()`-only** (Decision 2). No shared
  mutable class state is touched.
- **OPEN QUESTIONS 1–3** — all three decided below.

### Resolved Decisions

**Decision 1 (was OQ1) — Staged-bound policy: TTL only, no cap.**
`on_read()` will `EXPIRE` the staged key with `_staged_ttl_seconds` (refreshed on every
read) and will **not** apply an `LTRIM` cap. Rationale: a cap silently corrupts
`access_count` (Concern 1) because `confirm_access()` sums `#staged` *after* the cap dropped
entries; a TTL bounds storage for read-but-never-confirmed records without altering any
count. A continuously-read-and-confirmed record is already bounded by the confirmed-log cap
(`_max_access_log = 100`) and by `confirm_access()` deleting the staged key (`DEL KEYS[1]`,
`access_tracker.py:50`). The only unbounded case the issue (PERF-6) actually describes is a
record read but **never confirmed** — exactly what a TTL self-cleans.
Constant: `_staged_ttl_seconds = 86400` (24h) — a magic-number tuning knob, class-level on
`AccessTrackerMixin`, documented in the `on_read()` docstring. 24h comfortably outlives any
realistic stage→confirm gap while ensuring abandoned lists evaporate within a day.
Valkey-safe (plain `EXPIRE`). `_max_staged` / `LTRIM` is **not** introduced.

**Decision 2 (was OQ2 + Concern 3 + Concern 4) — Non-tracking mechanism: `.no_track()` only,
scoped to the QueryBuilder path.**
Lifecycle reads suppress staging exclusively via `.no_track()` chained on the
`QueryBuilder` (`.filter(...).no_track().all()`), which already threads `_no_track=True`
into `_execute_filter` and skips `_fire_on_read` at `query.py:2426`. **No** class-level
`_track_reads=False` guard (rejected — mutating process-wide class state is unsafe under
concurrency and surprises other readers of the same class). **No** new opt-out on
`Query.get()/get_many()` (rejected — lifecycle never calls those paths; adding parameters
there is over-scope and unneeded API surface). The unfiltered `Query.all()` branch is
already tracking-free (Freshness Check, `query.py:1795-1835`). This covers every read path
`tick()` actually exercises; a future internal caller that needs an opt-out on
`get()/get_many()` can add it then, with its own test.

**Decision 3 (was OQ3) — Remove `TICK_BATCH_SIZE`.**
The constant only slices an already-fully-hydrated list and provides no streaming benefit.
The single-pass refactor removes the slicing; the constant is removed entirely (and any
docstring/reference to it). A true streaming iterator is an out-of-scope rabbit hole at the
20k target. If streaming is ever built, the constant returns with the iterator that uses it.
