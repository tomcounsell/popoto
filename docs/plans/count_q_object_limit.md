---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/610
last_comment_id: none (issue has no comments)
---

# `QueryBuilder.count()` must ignore `limit()` on the Q-object path

## Problem

A caller renders a page of results and a total: fetch five rows, then ask how
many rows exist so the UI can say "showing 5 of 60". On the plain-field path
that works. Add a `Q` object to the same query and the total silently becomes
the page size.

**Current behavior:**

`QueryBuilder.count()` short-circuits for `Q` objects to `len(self.all())`
(`src/popoto/models/query.py:1821-1822`), and `all()` forwards `_limit_value`
into the executed filter (`query.py:1804-1805`, and again in the
`computed_sort` branch at `query.py:1799-1800`). The tally therefore inherits
the row bound. The plain-field branch delegates to `Query.count(**self._filters)`
(`query.py:1823`), which never sees `limit` at all, so the two paths disagree
for an otherwise identical query.

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
`first()` is implemented as `self.limit(1).all()` (`query.py:1826-1827`) and
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

**Baseline commit:** `0dbce75917ec7d4db79a5de6908d1f980b5ee9eb` (main, 2026-09-04 16:02 +0700)
**Issue filed at:** 2026-09-04T08:41:55Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/models/query.py:1814` — `def count(self) -> int:` — still holds
- `src/popoto/models/query.py:1821-1822` — `if self._q_objects: return len(self.all())` — still holds
- `src/popoto/models/query.py:1804-1805` — `kwargs["limit"] = self._limit_value` in the standard `all()` branch — still holds
- `src/popoto/models/query.py:1799-1800` — `results = results[: self._limit_value]` in the `computed_sort` branch — still holds
- `src/popoto/models/query.py:3238` — `Query.count(**kwargs)`, forwards to `filter_for_keys_set`, never applies `limit` — still holds
- `src/popoto/models/q.py:222` — `evaluate_q` calls `filter_for_keys_set(**q_obj.filters)` with no sibling kwargs — still holds

**Cited sibling issues/PRs re-checked:**
- #608 — merged (`6c39681`), tests-only, landed `test_count_is_not_truncated_by_a_present_limit` on the plain path
- #559 — closed by #608
- #602 / #571 — closed, async pushdown bound; no `count()` involvement
- #600 — **open**, will move `Query` pushdown bookkeeping off shared instance state on the sync path, in this same file

**Commits on main since the issue was filed (touching `src/popoto/models/query.py`):** none.

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

After the fix, step 3 skips both limit sites when the caller is `count()`, so
step 4 hydrates the full match set and step 5 returns its true length.

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
  `_execute(self, *, apply_limit: bool = True)`. Guard the `computed_sort`
  slice (`query.py:1799-1800`) and the `kwargs["limit"]` assignment
  (`query.py:1804-1805`) with `apply_limit`.
- `all()` becomes `return self._execute()`. Behavior identical, including the
  `computed_sort`/`order_by` precedence warning.
- `count()`'s `Q` branch becomes `return len(self._execute(apply_limit=False))`.
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

No agent integration required. `QueryBuilder.count()` is already reachable from
every consumer; the agent-memory retrieval paths that gate on a count before
widening a search (named in the issue's Impact) get the corrected value with no
wiring change.

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
- [ ] `mypy src/` error count is <= the recorded baseline (see Verification).
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
  version in the Build Record. Reference values from this plan's environment
  (repo root venv, Python 3.12.13, redis-py 7.1.1): **1178 errors in 71 files**
  total, **223** in `query.py`. CLAUDE.md warns the delta is redis-py-version
  dependent, so re-measure rather than trusting these.

### 2. Fix the count path
- **Task ID**: build-count
- **Depends On**: none
- **Validates**: tests/test_sorted_range_pushdown.py
- **Informed By**: the reproduction table in Problem (all six rows confirmed on DB 3)
- **Assigned To**: `count-builder`
- **Agent Type**: builder
- **Domain**: Redis/Popoto data
- **Parallel**: true
- Extract `QueryBuilder.all()`'s body into `_execute(self, *, apply_limit: bool = True)`.
- Gate `results[: self._limit_value]` and `kwargs["limit"] = self._limit_value` on `apply_limit`.
- `all()` returns `self._execute()`; `count()`'s `Q` branch returns `len(self._execute(apply_limit=False))`.
- Add the invariant paragraph to the `count()` docstring and the `apply_limit` note.
- Change nothing else in the file.

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
  `count()`.
- Name every new test `test_q_count_*` so the Verification grep can find them.
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
| New Q-count tests present | `grep -c "def test_q_count" tests/test_sorted_range_pushdown.py` | output > 3 |
| Plain-path #608 test still green | `POPOTO_TEST_DB=3 python -m pytest tests/test_sorted_range_pushdown.py -q -k "count_is_not_truncated_by_a_present_limit"` | exit code 0 |
| Reads still bounded | `POPOTO_TEST_DB=3 python -m pytest tests/test_sorted_range_pushdown.py -q -k "q_objects_disable_pushdown"` | exit code 0 |
| Full suite | `POPOTO_TEST_DB=3 python -m pytest -q` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| mypy no worse | `mypy src/ 2>&1 \| tail -1` | output contains `1178 errors` |
| CHANGELOG entry added | `git diff main -- CHANGELOG.md \| grep -c "^+.*count()"` | output > 0 |
| Docs state the invariant | `grep -ci "count() .*not.*bounded by\|never bounded by .*limit" docs/query.md` | output > 0 |
| Production diff is one file (anti-criterion) | `git diff --name-only main -- src/ \| grep -v "^src/popoto/models/query.py$"` | match count == 0 |
| No `_limit_value` mutation inside count (anti-criterion) | `git diff main -- src/popoto/models/query.py \| grep -c "^+.*self._limit_value = "` | match count == 0 |
| No new xfail/skip (anti-criterion) | `git diff main -- tests/ \| grep -c "^+.*\(xfail\|@pytest.mark.skip\)"` | match count == 0 |
| Async surface untouched (anti-criterion) | `git diff main -- src/ \| grep -c "async_count"` | match count == 0 |
| No Redis module commands (anti-criterion) | `git diff main -- src/ \| grep -cE "^\+.*(BF\.\|CMS\.\|JSON\.\|FT\.)"` | match count == 0 |

The mypy row's literal is this environment's baseline (Python 3.12.13,
redis-py 7.1.1). Re-measure at the branch point per task `baseline-mypy` and
update the row's literal before relying on it in another environment.

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. `first()` mutating the builder's `_limit_value` is the root of the sharpest
   failure shape here. This plan fixes the symptom for `count()` and leaves the
   mutation. Is a follow-up issue wanted for `limit()` returning a copy, or is
   the mutating-chain semantics deliberate and permanent?
2. Should the `evaluate_q` sibling-kwarg blindness (`q.py:222`, requiring a
   partition key to be repeated inside each `Q`) be filed as its own issue now,
   or does it stay a note on #610 until someone hits it in practice?
