---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/610
last_comment_id: none (issue has no comments)
revised: 2026-09-04
revision_applied: true
revision_applied_at: 2026-09-04T09:25:37Z
---

# `QueryBuilder.count()` must ignore `limit()` on the Q-object path

## Problem

A caller renders a page of results and a total: fetch five rows, then ask how
many rows exist so the UI can say "showing 5 of 60". On the plain-field path
that works. Add a `Q` object to the same query and the total silently becomes
the page size.

**Current behavior:**

`QueryBuilder.count()` short-circuits for `Q` objects to `len(self.all())`
(`src/popoto/models/query.py:1828-1829`), and `all()` forwards `_limit_value`
into the executed filter (`query.py:1811-1812`, and again in the
`computed_sort` branch at `query.py:1806-1807`). The tally therefore inherits
the row bound. The plain-field branch delegates to `Query.count(**self._filters)`
(`query.py:1830`), which never sees `limit` at all, so the two paths disagree
for an otherwise identical query.

All line numbers in this plan are cited against `b8e1dc4`, this branch's point,
re-swept during the 2026-09-04 critique round after the first draft's citations
were found 5-7 lines stale.

Reproduced for this plan against a 20-row partition on
`REDIS_URL=redis://localhost:6379/3` (Python 3.12.13, redis-py 7.1.1, repo root
venv, model with a `partition_by="room_id"` `SortedField`, keys deleted
afterwards, `dbsize` 0):

| Query form | Returned | Expected |
|---|---|---|
| `filter(Q(...)).limit(5).count()` | 5 | 20 |
| `filter(**same).limit(5).count()` (plain) | 20 | 20 |
| `filter(Q(...)).count()` (no limit) | 20 | 20 |
| `filter(Q(...)).order_by("-last_active_at").limit(5).count()` | 5 | 20 |
| `filter(Q(...)).computed_sort(fn).limit(5).count()` | 5 | 20 |
| `b = filter(Q(...))`; `b.first()`; `b.count()` | 1 | 20 |

The last row is the sharpest one and is new information not in the issue.
`first()` is implemented as `self.limit(1).all()` (`query.py:1832-1840`) and
`limit()` mutates the builder in place (`query.py:265`), so **calling `first()`
on a `Q` builder permanently pins any later `count()` on that builder to 1** —
no explicit `limit()` needed anywhere in user code. The `computed_sort` row is
also new: the issue asked for it to be confirmed or ruled out, and it is
confirmed.

**Desired outcome:**

`count()` reports the matching population regardless of any limit, on both
paths, for every branch of `all()`. A limit bounds the rows you get back; it
must not bound the tally.

## Freshness Check

**Baseline commit:** `b8e1dc469c7c2e75846017b436418d66cd98fcff` (this branch's point off `origin/main`)
**Issue filed at:** 2026-09-04T08:41:55Z
**Disposition:** Unchanged in behavior; **citations corrected**

**Correction (critique round, 2026-09-04).** The first draft recorded baseline
`0dbce75` and asserted "commits on main since the issue was filed (touching
`src/popoto/models/query.py`): none". **That was false.** `9c04b8c`
(#575/#570, "route partition_by renders through canonical_key_str") changed
`src/popoto/models/query.py` (+10/-3) after that baseline, which shifted every
citation below by 5-7 lines while the draft claimed each one "still holds". The
behavior under repair is unchanged — the shift is pure line drift, and the fix
targets code quoted by content, not by line — but the sweep is redone here
against `b8e1dc4` rather than a recorded baseline.

**File:line references re-verified against `b8e1dc4`:**
- `src/popoto/models/query.py:1821` — `def count(self) -> int:` — holds
- `src/popoto/models/query.py:1828-1829` — `if self._q_objects: return len(self.all())` — holds
- `src/popoto/models/query.py:1811-1812` — `kwargs["limit"] = self._limit_value` in the standard `all()` branch — holds
- `src/popoto/models/query.py:1806-1807` — `results = results[: self._limit_value]` in the `computed_sort` branch — holds
- `src/popoto/models/query.py:3245` — `Query.count(**kwargs)`, forwards to `filter_for_keys_set`, never applies `limit` — holds
- `src/popoto/models/query.py:3011` — `Query._execute_filter`, whose `_no_track` parameter is declared at `3014` and consumed at `3103`/`3158` — holds
- `src/popoto/models/q.py:222` — `evaluate_q` calls `filter_for_keys_set(**q_obj.filters)` with no sibling kwargs — holds
- `src/popoto/models/query.py:1858-1868` — `__len__` and `__bool__` also call `len(self.all())`. **Deliberately out of scope**: both are list-like operators over the rows the query returns, so a limit correctly bounds them. Only `count()` claims to report a population.

**Cited sibling issues/PRs re-checked:**
- #608 — merged (`6c39681`), tests-only, landed `test_count_is_not_truncated_by_a_present_limit` on the plain path
- #559 — closed by #608
- #602 / #571 — closed, async pushdown bound; no `count()` involvement
- #600 — **open**, will move `Query` pushdown bookkeeping off shared instance state on the sync path, in this same file

**Commits on main since the issue was filed (touching `src/popoto/models/query.py`):**
`9c04b8c` (#575/#570), merged 2026-09-04, +10/-3. It routes `partition_by`
renders through `canonical_key_str` and does not touch `all()`, `count()` or
`_execute_filter`. No interaction with this fix beyond the line drift noted
above.

**Active plans in `docs/plans/` overlapping this area:**
`sorted_pushdown_coverage_gaps.md` (#559, shipped) owns the plain-path
`count()` test this plan extends. No open plan edits `QueryBuilder.count()`.

**Notes:** Two behaviors beyond the issue's text were established during
reproduction and are folded into scope: the `computed_sort` branch truncates
too, and `first()` poisons a later `count()` on the same `Q` builder.

## Prior Art

- **PR #608 / issue #559**: added `test_count_is_not_truncated_by_a_present_limit`
  on the plain-field path and pinned the invariant in words. Its review found
  the same invariant violated on the sibling `Q` path, scoped the fix out as a
  production change, and filed #610. This plan is the follow-through.
- **PR #602 / issue #571**: applied the `SortedField` limit pushdown on the
  async path. Relevant only as evidence that the async surface is separate:
  it added no `QueryBuilder` async count.
- **PR #518**: the original conditional limit pushdown into `SortedField` range
  reads. Introduced the pushdown machinery, not the count divergence — the
  `Q` short-circuit at `count()` predates it.

No prior attempt to fix `QueryBuilder.count()` exists, so there is no
`## Why Previous Fixes Failed` section.

## Data Flow

1. **Entry point**: `Model.query.filter(Q(...)).limit(5).count()`.
   `filter()` stores the `Q` in `_q_objects` (`query.py:240`); `limit()` sets
   `_limit_value` on the builder in place.
2. **`QueryBuilder.count()`** (`query.py:1814`): `_q_objects` is non-empty, so
   it short-circuits to `len(self.all())` instead of `Query.count()`.
3. **`QueryBuilder.all()`** (`query.py:1766`): copies `_filters`, then either
   the `computed_sort` branch slices the sorted result to `_limit_value`
   (line 1800) or the standard branch sets `kwargs["limit"] = _limit_value`
   (line 1805).
4. **`Query._execute_filter`** (`query.py:3006`): with `q_objects` present it
   disables pushdown, resolves keys through `_evaluate_filter_args` →
   `evaluate_q`, then `get_many_objects(..., limit=kwargs["limit"])` hydrates
   at most `limit` objects.
5. **Output**: `len()` of that bounded list — the page size, returned as the
   population size.

6. **Read tracking**: `_execute_filter` ends by firing `_fire_on_read`
   (`query.py:3158`) unless `_no_track` is set, and `_fire_on_read`
   (`query.py:150-164`) issues a pipelined `RPUSH`+`EXPIRE` per hydrated
   instance for `AccessTrackerMixin` models. `all()` and `count()` both pass
   `_no_track=self._no_track`, which defaults to `False` (`query.py:204`).

After the fix, step 3 skips both limit sites when the caller is `count()`, so
step 4 hydrates the full match set and step 5 returns its true length.

**Step 6 is the reason `count()` must also suppress tracking** (critique
BLOCKER). Removing the bound without touching `_no_track` would turn a `Q` +
`limit` count from a 5-instance tracking write into a whole-population tracking
write — a read-shaped call issuing `O(population)` writes. `count()` therefore
passes `no_track=True` explicitly. This also corrects a smaller pre-existing
wrong: today a `Q` count already records a page-sized burst of phantom
"accesses" for rows the caller never received. A tally is not a read, so it
should record nothing. `all()` keeps `self._no_track` verbatim — changing it
would silently disable read tracking for every ORM read in the repo.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**: none public. `all()`, `count()`, `Query.count()` and
  `Query.async_count()` keep their signatures. One new private method on
  `QueryBuilder`.
- **Coupling**: unchanged. The change is contained to two adjacent methods.
- **Data ownership**: unchanged. No Redis key, index or stored format is touched.
- **Reversibility**: trivial — a single-file revert.
- **Cost**: a `Q` + `limit` count now hydrates the whole match set instead of a
  bounded page. That is a real cost increase, accepted deliberately: the
  bounded version returns a wrong answer. See Rabbit Holes for the keys-only
  optimization that is explicitly not attempted here.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -n 3 ping` | Test and repro backend |
| Test DB is not 0 | `python -c "import os; assert os.environ.get('POPOTO_TEST_DB','3') != '0'"` | DB 0 is the live agent store |
| Dev extras installed | `python -c "import numpy, sentence_transformers"` | Avoids ~95 silently deselected tests |

## Solution

### Key Elements

- **A limit-free execution seam on `QueryBuilder`**: one private method holding
  today's `all()` body, with both limit sites gated by a flag.
- **`all()` keeps its exact behavior**: it calls the seam with the limit applied.
- **`count()` calls the seam with the limit suppressed** on the `Q` branch, so
  both branches of `count()` now agree that a limit never bounds a tally.
- **A regression test group** beside the plain-path test that #608 landed,
  covering the `Q` form in every shape that reaches a limit site.

### Flow

`filter(Q(...))` → `.limit(5)` → **builder carries a live row bound** →
`.count()` → **executes without the bound** → full tally → `.all()` on the same
builder → **bound still live** → 5 rows.

### Technical Approach

- Extract the current body of `QueryBuilder.all()` into
  `_execute(self, *, apply_limit: bool = True, no_track: bool = False)`. Guard
  the `computed_sort` slice (`query.py:1806-1807`) and the `kwargs["limit"]`
  assignment (`query.py:1811-1812`) with `apply_limit`. Both
  `_execute_filter` call sites inside the seam pass `_no_track=no_track`
  instead of reading `self._no_track` directly.
- `all()` becomes `return self._execute(no_track=self._no_track)`. Behavior
  identical, including the `computed_sort`/`order_by` precedence warning and
  the existing tracking behavior. **`all()` must keep `self._no_track`** — a
  hardcoded value here would disable or force read tracking repo-wide.
- `count()`'s `Q` branch becomes
  `return len(self._execute(apply_limit=False, no_track=True))`.
- **`no_track=True` on the count path is load-bearing, not tidiness**
  (critique BLOCKER). `_execute_filter` fires `_fire_on_read` when `_no_track`
  is false (`query.py:3158`), and that helper pipelines an `RPUSH`+`EXPIRE` per
  instance (`query.py:150-164`). Suppressing the limit without suppressing
  tracking would make a `Q` + `limit` count issue a write per row of the whole
  population. Counting is not reading; it must record no accesses.
- **Single-source the invariant** in the cheap way available: `count()` gets one
  docstring paragraph stating that a limit bounds rows and never bounds a
  tally, naming both branches, so the plain path's silent compliance
  (`Query.count` simply never receiving `limit`) and the `Q` path's explicit
  suppression are documented as one rule at the one place both live. A shared
  runtime helper is not worth it for two call sites in the same method.
- Do **not** touch `_limit_value` on the builder. Clearing it inside `count()`
  would fix the tally by breaking the builder for every later call, and the
  #608 test deliberately asserts that the same builder still yields 5 rows.
- Keep the diff inside `QueryBuilder`. Issue #600 will rework `Query`'s
  pushdown bookkeeping in this same file; nothing here should collide with it.

### Async

No change. There is no async `count()` on `QueryBuilder` — the async surface is
`Query.async_count(**kwargs)` (`query.py:3685`), which passes kwargs to
`filter_for_keys_set` and never applies `limit`, and takes no `Q` objects. This
closes the issue's open question about async exposure: it is not exposed.

## Failure Path Test Strategy

### Exception Handling Coverage
No exception handlers in scope. `all()` and `count()` contain no `try`/`except`;
the only `try/finally` in the execution path (`query.py:3040-3043`) is untouched.

### Empty/Invalid Input Handling
- `_limit_value = None` (never set): both branches behave exactly as today.
- Non-positive and non-integer limits (`0`, `-1`, `None`, `True`) already have a
  parametrized guard in `test_non_positive_int_limit_does_not_bound`; the
  suppressed path cannot make them worse, because it removes the value entirely.
- Empty match set: `_execute_filter` returns `[]` before any limit site, so
  `count()` returns 0 on both branches.

### Error State Rendering
No user-visible rendering surface. The failure mode being fixed is a silent
wrong integer, and the tests assert the integer directly.

## Test Impact

- [ ] `tests/test_sorted_range_pushdown.py::test_count_is_not_truncated_by_a_present_limit` — UNCHANGED: it covers the plain path and must keep passing verbatim, as the guard that the fix did not disturb the compliant branch.
- [ ] `tests/test_sorted_range_pushdown.py::test_q_objects_disable_pushdown` — UNCHANGED: it asserts a `Q` + `limit` **read** returns 3 rows. The fix must not change it; if it does, the limit was wrongly suppressed on `all()`.
- [ ] `tests/test_chainable_queries.py`, `tests/test_expression_queries.py`, `tests/test_computed_sort.py` — UNCHANGED: grep confirms no test anywhere pairs a limit with a `count()` on a `Q`/Expression builder, so no test encodes the truncated behavior.

No existing test asserts the buggy value, so nothing needs UPDATE, DELETE or
REPLACE. The work is additive plus a two-method refactor with identical
observable behavior for `all()`.

## Rabbit Holes

- **Counting without hydration.** `_evaluate_filter_args` already returns a key
  set, so a `Q` count could in principle be `len(keys)` with no `HGETALL` at
  all. It cannot be that simple: client-side plain-field filters
  (`_pending_client_filters`) are applied only after hydration, so a keys-only
  tally would over-count exactly the queries #559's sibling work was about.
  Making that correct is a redesign of the count path, not this fix.
- **`evaluate_q` sibling-kwarg blindness** (`q.py:222`). Each `Q` is evaluated
  against `filter_for_keys_set` without the query's other kwargs, so a
  partitioned `SortedField` inside a `Q` requires its partition key repeated
  inside that same `Q` or `QueryException` fires from
  `sorted_field_mixin.py:760`. Real, listed on #610's Next Steps, and entirely
  separate from the tally. Touching it here would grow a three-line fix into a
  `Q` semantics change. File it as its own issue before working on it.
- **Reworking `limit()` to return a copy instead of mutating.** It would fix the
  `first()`-poisons-`count()` shape at the root, and it is a breaking change to
  chaining semantics across the whole ORM. Not in a Small appetite.
- **Touching `Query`'s pushdown bookkeeping.** #600 owns that file region.

## Risks

### Risk 1: The refactor silently changes `all()`
**Impact:** Every query path in the ORM runs through `all()`. A behavior drift
here is a repo-wide regression, not a local one.
**Mitigation:** The extraction is mechanical — the body moves verbatim and the
default `apply_limit=True` reproduces today's code exactly. The full suite is a
required gate, and `test_q_objects_disable_pushdown` plus the pushdown suite
assert the read is still bounded.

### Risk 2: A `Q` + `limit` count becomes expensive on a large match set
**Impact:** A query that hydrated 5 objects now hydrates the whole partition.
**Mitigation:** Accepted and documented in the CHANGELOG entry, because the
cheap answer was wrong. The cost matches what the plain path already pays for
an equivalent filtered count. The keys-only optimization is named as a rabbit
hole rather than attempted.

### Risk 3: Test-DB contention produces phantom failures
**Impact:** A concurrent worktree on the same Redis DB yields failures unrelated
to the diff, and CLAUDE.md records 73-158 phantom failures from exactly this.
**Mitigation:** Pin `POPOTO_TEST_DB=3` for this lane. Never DB 0 (live agent
store) and never DB 12 (`tests/test_pytest_plugin.py:52` flushes it mid-run).
State the DB alongside every count reported.

## Race Conditions

No race conditions identified. The change is synchronous, single-threaded,
touches no shared `Query` instance state, issues no new Redis command, and adds
no await point. The one concurrency hazard nearby — `Query` being a single
instance per model class, which #571 addressed for the async pushdown — is not
reached: `apply_limit` is a local argument, not instance state.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #600] Moving `Query` pushdown bookkeeping off shared instance
  state on the sync path. Same file, adjacent region, already filed and open.
  This plan's diff must stay inside `QueryBuilder.all()`/`count()` so the two
  do not collide.

Everything else surfaced during reproduction is either in scope (the
`computed_sort` branch and the `first()`-then-`count()` shape are both fixed and
tested here) or named in Rabbit Holes as work that needs its own issue before
anyone starts it.

## Update System

No update system changes required — this is an internal query-execution fix
with no new dependency, config file or migration step.

## Agent Integration

No wiring change is required — `QueryBuilder.count()` is already reachable from
every consumer, and the agent-memory retrieval paths that gate on a count before
widening a search (named in the issue's Impact) get the corrected value for free.

**Corrected during critique.** The first draft said those paths get the fix
"with no wiring change", full stop. That was incomplete in a way that mattered:
for a model mixing in `AccessTrackerMixin` — which is exactly the agent-memory
shape — the naive fix would also have changed what `count()` *writes*, turning
a tally into a population-scale burst of access records and corrupting the very
recency signal those paths rank on. The `no_track=True` suppression in
Technical Approach is what makes the "no wiring change" claim true. It is
pinned by its own test (task `build-tests`) on a tracker-mixing model, because
`PushdownDoc` does not mix the tracker in and no existing test would catch it.

## Documentation

### Feature Documentation
- [ ] No `docs/features/` page covers query counting; none is created for a
      three-line bug fix.

### External Documentation Site
- [ ] `docs/query.md` "Count Results" (line ~846): state that `count()` reports
      the full matching population and is **never bounded by `limit`**, on the
      chainable and kwargs forms alike, including queries carrying `Q` objects.
      Use that phrase verbatim so the Verification grep matches.
- [ ] `docs/query.md` chainable-API count example (line ~331): one sentence
      making the same point where the `QueryBuilder` form is introduced.
- [ ] `CHANGELOG.md` under `## [Unreleased]` → `### Fixed`: user-facing
      behavior change. Name the shapes that were wrong (`Q` + `limit`,
      `Q` + `computed_sort` + `limit`, and `first()` followed by `count()` on
      the same builder), state the corrected value, and disclose the cost
      change from Risk 2.
- [ ] `mkdocs build --strict` passes.

### Inline Documentation
- [ ] `QueryBuilder.count()` docstring carries the single-sourced invariant
      statement described in Technical Approach.
- [ ] `_execute`'s `apply_limit` argument is documented as "suppressed by
      `count()`, which must tally the population and not the page".
- [ ] Each new test's docstring names the guard it defends and what a dropped
      guard returns (the convention #608's review enforced in this module).

## Success Criteria

- [ ] `Model.query.filter(Q(...)).limit(n).count()` returns the full match count.
- [ ] The same builder still yields `n` rows afterwards, asserted in the same
      test, so the count assertion is not vacuous.
- [ ] Descending and ascending `order_by` forms both return the full count.
- [ ] The partitioned `SortedField` shape from the issue is the model under
      test (`PushdownDoc.last_active_at`, `partition_by="room_id"`).
- [ ] `Q` + `computed_sort` + `limit` returns the full count.
- [ ] `first()` followed by `count()` on the same `Q` builder returns the full
      count, not 1.
- [ ] An unlimited `Q` count is asserted as the control in the same test group.
- [ ] The plain-path test from #608 passes unchanged.
- [ ] `test_q_objects_disable_pushdown` passes unchanged — reads stay bounded.
- [ ] Full suite green on `POPOTO_TEST_DB=3`, with the environment stated.
- [ ] `ruff check src/` and `black --check src/ tests/` clean.
- [ ] A `Q` + `limit` `count()` records **no** access-tracking writes on an
      `AccessTrackerMixin` model, while `all()` on the same builder still does.
- [ ] `all()` still passes the builder's own `_no_track`, so read tracking is
      unchanged for every other ORM read.
- [ ] `mypy src/` error count is <= the baseline measured in this worktree
      (1126; see Verification).
- [ ] Documentation updated (`/do-docs`).
- [ ] The production diff touches `src/popoto/models/query.py` only.

## Team Orchestration

### Team Members

- **Builder (query-count)**
  - Name: `count-builder`
  - Role: The `_execute`/`count()` change plus the regression tests
  - Agent Type: builder
  - Resume: true

- **Validator (query-count)**
  - Name: `count-validator`
  - Role: Reproduce every number independently, including the mutation checks
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: `count-docs`
  - Role: `docs/query.md` and `CHANGELOG.md`
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Record the mypy baseline
- **Task ID**: baseline-mypy
- **Depends On**: none
- **Assigned To**: `count-builder`
- **Agent Type**: builder
- **Parallel**: true
- Run `mypy src/ 2>&1 | tail -1` on the branch point and record the total, the
  `src/popoto/models/query.py` subtotal, the redis-py version and the Python
  version in the Build Record. Reference values from the plan author's
  environment (repo root venv, Python 3.12.13, redis-py 7.1.1): **1178 errors in
  71 files** total, **223** in `query.py`. CLAUDE.md warns the delta is
  redis-py-version dependent, so re-measure rather than trusting these.

  **Measured in this lane's worktree at `b8e1dc4`** (`.worktrees/sdlc-610/.venv`,
  Python 3.13.2, redis-py 8.1.0): **1126 errors in 67 files** total, **222** in
  `src/popoto/models/query.py`. That is the baseline the Verification row
  compares against. The 1178 figure does not hold in this environment and must
  not be used as a gate here.

### 2. Fix the count path
- **Task ID**: build-count
- **Depends On**: none
- **Validates**: tests/test_sorted_range_pushdown.py
- **Informed By**: the reproduction table in Problem (all six rows confirmed on DB 3)
- **Assigned To**: `count-builder`
- **Agent Type**: builder
- **Domain**: Redis/Popoto data
- **Parallel**: true
- Extract `QueryBuilder.all()`'s body into `_execute(self, *, apply_limit: bool = True, no_track: bool = False)`.
- Gate `results[: self._limit_value]` and `kwargs["limit"] = self._limit_value` on `apply_limit`.
- Thread `no_track` into both `_execute_filter` calls inside the seam, replacing the direct `self._no_track` reads.
- `all()` returns `self._execute(no_track=self._no_track)` — keep `self._no_track` here, do not hardcode.
- `count()`'s `Q` branch returns `len(self._execute(apply_limit=False, no_track=True))`.
- Add the invariant paragraph to the `count()` docstring, and document `apply_limit` and `no_track` on `_execute`.
- Change nothing else in the file. Do not touch `__len__`/`__bool__` (`query.py:1858-1868`): those are list-like over returned rows and a limit correctly bounds them.

### 3. Regression tests
- **Task ID**: build-tests
- **Depends On**: build-count
- **Validates**: tests/test_sorted_range_pushdown.py
- **Assigned To**: `count-builder`
- **Agent Type**: test-engineer
- **Parallel**: false
- Add a test group directly after `test_count_is_not_truncated_by_a_present_limit`,
  reusing `PushdownDoc` and `_seed()` — the model already has the partitioned
  `SortedField` and the `*PushdownDoc*` flush glob already covers it, so no new
  fixture or model is needed.
- Cover, with the partition value inside the `Q` (required by
  `sorted_field_mixin.py:760`): the unlimited control; `limit(5)` descending;
  `limit(5)` ascending; the same builder draining to 5 rows after reporting the
  full count; `computed_sort` plus `limit(5)`; and `first()` followed by
  `count()`. That is six.
- **A seventh test pins the tracking-suppression blocker.** Define a small model
  that mixes in `AccessTrackerMixin` (`PushdownDoc` does not, so no existing or
  otherwise-planned test would catch a regression here), seed it, and assert
  that a `Q` + `limit` `.count()` leaves the staged-access key length
  **unchanged**, while an equivalent `.all()` still records accesses. Without
  this, the fix could silently convert every `Q` count into a population-scale
  write and the suite would stay green.
- Name every new test `test_q_count_*` so the Verification grep can find them,
  the tracking test included.
- Give each test a docstring naming the guard and what a dropped guard returns.

### 4. Mutation check
- **Task ID**: validate-mutation
- **Depends On**: build-tests
- **Assigned To**: `count-validator`
- **Agent Type**: validator
- **Parallel**: false
- Revert `apply_limit` to always-on (that is, restore `return len(self.all())`
  in the `Q` branch) and confirm the new tests fail. Restore the fix and confirm
  they pass. Report `git status src/` clean afterwards.
- Run a **second, independent mutation** for the tracking guard: keep
  `apply_limit=False` but change `count()`'s call to `no_track=False`, and
  confirm the tracking test fails while the six tally tests stay green.
- A test that survives this mutation is not a guard; either strengthen it or
  disclose it as a weak discriminator in its docstring, per the convention #608
  established in this module.

### 5. Documentation
- **Task ID**: document-count
- **Depends On**: build-tests
- **Assigned To**: `count-docs`
- **Agent Type**: documentarian
- **Parallel**: false
- Apply the `docs/query.md` and `CHANGELOG.md` edits listed in Documentation.

### 6. Final validation
- **Task ID**: validate-all
- **Depends On**: validate-mutation, document-count
- **Assigned To**: `count-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run every Verification row, state `POPOTO_TEST_DB`, the Python and redis-py
  versions beside each count, and confirm the editable install resolves to the
  checkout under test.

## Verification

Run with `POPOTO_TEST_DB=3` exported. Commands assume the repo root.

| Check | Command | Expected |
|-------|---------|----------|
| Target module passes | `POPOTO_TEST_DB=3 python -m pytest tests/test_sorted_range_pushdown.py -q` | exit code 0 |
| New Q-count tests present | `grep -c "def test_q_count" tests/test_sorted_range_pushdown.py` | output >= 7 |
| Plain-path #608 test still green | `POPOTO_TEST_DB=3 python -m pytest tests/test_sorted_range_pushdown.py -q -k "count_is_not_truncated_by_a_present_limit"` | exit code 0 |
| Reads still bounded | `POPOTO_TEST_DB=3 python -m pytest tests/test_sorted_range_pushdown.py -q -k "q_objects_disable_pushdown"` | exit code 0 |
| Full suite | `POPOTO_TEST_DB=3 python -m pytest -q` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| mypy no worse | `mypy src/ 2>&1 \| tail -1 \| grep -oE '[0-9]+ errors' \| grep -oE '[0-9]+'` | integer <= **1126** (baseline measured in this worktree: `.worktrees/sdlc-610/.venv`, Python 3.13.2, redis-py 8.1.0) |
| Tracking suppressed on the count path | `POPOTO_TEST_DB=3 python -m pytest tests/test_sorted_range_pushdown.py -q -k "q_count and track"` | exit code 0 |
| CHANGELOG entry added | `git diff main...HEAD -- CHANGELOG.md \| grep -c "^+.*count()"` | output > 0 |
| Docs state the invariant | `grep -ci "count() .*not.*bounded by\|never bounded by .*limit" docs/query.md` | output > 0 |
| Production diff is one file (anti-criterion) | `git diff --name-only main...HEAD -- src/ \| grep -v "^src/popoto/models/query.py$"` | match count == 0 |
| No `_limit_value` mutation inside count (anti-criterion) | `git diff main...HEAD -- src/popoto/models/query.py \| grep -c "^+.*self._limit_value = "` | match count == 0 |
| `all()` still passes the builder's own `_no_track` (anti-criterion) | `grep -c "self._execute(no_track=self._no_track)" src/popoto/models/query.py` | output == 1 |
| No new xfail/skip (anti-criterion) | `git diff main...HEAD -- tests/ \| grep -c "^+.*\(xfail\|@pytest.mark.skip\)"` | match count == 0 |
| Async surface untouched (anti-criterion) | `git diff main...HEAD -- src/ \| grep -c "async_count"` | match count == 0 |
| No Redis module commands (anti-criterion) | `git diff main...HEAD -- src/ \| grep -cE "^\+.*(BF\.\|CMS\.\|JSON\.\|FT\.)"` | match count == 0 |

**Every diff-based row uses the three-dot `main...HEAD` form deliberately**
(critique CONCERN). The two-dot form compares against whatever `main` points at
right now, and roughly ten SDLC lanes are merging into `main` concurrently; the
moment one of them lands a `src/` change, the two-dot form reports that lane's
files and the "production diff is one file" anti-criterion fails on work this
branch never touched. The three-dot form pins the comparison to the merge base.

The mypy row is a numeric `<=` comparison, not a string match, so a *better*
result passes instead of failing the gate — the first draft's `output contains
1178 errors` would have rejected an improvement, and contradicted the Success
Criterion that says "<= the recorded baseline". The 1126 baseline belongs to
this worktree's venv (Python 3.13.2, redis-py 8.1.0); per CLAUDE.md the delta is
redis-py-version dependent, so re-measure and restate the environment before
relying on this row anywhere else.

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Risk & Robustness | `count()`'s Q branch inherits `_no_track=self._no_track` (default `False`, `query.py:204`), so `_execute_filter` fires `_fire_on_read` (`query.py:3158`) over the *whole* population after the fix instead of the bounded page. `on_read` (`fields/access_tracker.py:158`) issues a real `RPUSH`+`EXPIRE` per instance through a pipeline (`query.py:161-163`), so a `Q` + `limit` count becomes a population-scale **write**. The plan's Agent Integration section claims agent-memory paths "get the corrected value with no wiring change" — that claim is false as written. | Pass `_no_track=True` unconditionally from `count()`; add a tracking-side-effect test | `count()`'s Q branch must be `len(self._execute(apply_limit=False, _no_track=True))`. `_execute` must thread its own `_no_track` argument to `_execute_filter` (`query.py:3014`, `3103`) rather than always reading `self._no_track`. **`all()`'s call must keep `_no_track=self._no_track`** — changing it would silently disable read-tracking for every ORM read. Add one test on an `AccessTrackerMixin` model asserting `.count()` leaves the staged-access key length unchanged; `PushdownDoc` does not mix the tracker in, so no existing or planned test catches this. |
| CONCERN | Scope & Value, History & Consistency (both) | Every `query.py` line citation is off by 5-7 lines, and the Freshness Check's claim "Commits on main since the issue was filed (touching `src/popoto/models/query.py`): none" is false — `9c04b8c` (#575/#570) changed that file (+10/-3) after the stated baseline `0dbce75`. Actual: `count()` 1821 (not 1814), Q short-circuit 1828-1829 (not 1821-1822), `computed_sort` slice 1806-1807 (not 1799-1800), `kwargs["limit"]` 1811-1812 (not 1804-1805), `Query.count` 3245 (not 3238), `_execute_filter` 3011 (not 3006). | Re-cite against `b8e1dc4`; correct the Freshness Check disposition | Low risk to the diff itself — Task 2 quotes the code text (`results[: self._limit_value]`, `kwargs["limit"] = self._limit_value`), so a builder matching on content lands correctly. The cost is reviewer trust: a Freshness Check that asserted "still holds" on six stale citations is not evidence for the plan's other unverified claims. Re-run the citation sweep against `git rev-parse HEAD`, not against the recorded baseline. |
| CONCERN | History & Consistency (reproduced by the aggregator) | The mypy Verification row expects the literal `1178 errors`, but the build environment produces **`Found 1126 errors in 67 files`** (worktree venv `.venv/bin/mypy`, Python 3.13.2, redis-py **8.1.0**). The plan's literal came from a different environment (repo root venv, Python 3.12.13, redis-py 7.1.1). As written the gate fails on a *better* result, and it contradicts the Success Criterion, which says "<= the recorded baseline". | Make the mypy row a numeric `<=` comparison against a baseline measured in this worktree | Replace the string match with a comparison, e.g. capture `mypy src/ 2>&1 \| tail -1 \| grep -oE '[0-9]+ errors'` and assert the integer is `<=` the value task `baseline-mypy` records **in this worktree's venv**. Per CLAUDE.md, the delta is redis-py-version dependent, so state `redis-py 8.1.0 / Python 3.13.2 / .worktrees/sdlc-610/.venv` beside whatever number is recorded. |
| CONCERN | Structural check | Four anti-criterion Verification rows diff against the moving ref `main`. `main` already resolves to `1f1e6ad`, two commits *ahead* of this branch's tip `b8e1dc4`. Both are docs-only today so the rows still pass, but ~10 concurrent SDLC lanes are merging into `main`; as soon as one lands a `src/` change, `git diff main -- src/` reports that lane's files and the "production diff is one file" anti-criterion fails on work this branch never touched. | Pin every anti-criterion diff to the merge base, not to `main` | Use the three-dot form throughout: `git diff main...HEAD -- src/` (equivalently `git diff $(git merge-base main HEAD) -- src/`). Applies to all four rows: "Production diff is one file", "No `_limit_value` mutation inside count", "No new xfail/skip", "Async surface untouched", plus the CHANGELOG row. |
| NIT | Scope & Value | Step 3 enumerates six `test_q_count_*` cases but the Verification row only requires `grep -c "def test_q_count" > 3` — half the specified coverage could be silently dropped and still pass the gate. | Tighten the threshold to `>= 6`. | n/a |

---

## Open Questions

Both questions are resolved in-plan so the build is not gated on a check-in.
Neither answer changes this plan's diff.

1. **`first()` mutating the builder's `_limit_value`.** *Resolved: leave the
   mutation, fix the symptom, do not file a follow-up yet.* Making `limit()`
   return a copy is a breaking change to chaining semantics across the whole
   ORM, it is already named in Rabbit Holes as outside a Small appetite, and
   #608 landed a test that deliberately asserts a builder keeps its bound after
   a `count()`. Changing the mutation now would contradict a just-merged
   guarantee. The `first()`-then-`count()` shape is fixed and tested here, which
   removes the sharp edge; the semantics question can be raised on its own
   merits later, with the ORM-wide blast radius as the subject rather than a
   count bug as the pretext.
2. **`evaluate_q` sibling-kwarg blindness (`q.py:222`).** *Resolved: it stays a
   note, no issue filed from this lane.* It is already recorded on #610's Next
   Steps and in Rabbit Holes with the reproduction attached, so the context is
   not lost. Filing it now would create a second open issue in the same file
   region that #600 is already reworking, competing for the same lines. Whoever
   picks up `Q` semantics should own it as one piece of work rather than
   inheriting a stub.
