---
status: Implemented
type: feature
appetite: Small
owner: Solo dev
created: 2026-03-12
tracking: https://github.com/tomcounsell/popoto/issues/182
last_comment_id:
---

# Add computed_sort() to QueryBuilder

## Problem

Users need to sort query results by computed/derived values that are not stored as indexed fields in Redis. For example, ranking memory episodes by an activation score that combines access count, age, and confidence -- values computed at query time.

**Current behavior:**
`Meta.order_by` and `QueryBuilder.order_by()` only support sorting by stored fields (via `SortedField` indexes or attribute-level Python sorting in `prepare_results`). There is no way to provide a custom sort function.

**Desired outcome:**
A `computed_sort(fn, reverse=False)` method on `QueryBuilder` that applies a caller-provided key function to sort results in Python after fetching, before applying `limit()`.

## Prior Art

- **Issue #136 / PR #139**: SortedField ordering -- added support for returning results in sorted order using Redis sorted sets. This is the server-side sorting foundation; `computed_sort` is the complementary client-side sorting feature.
- **PR #99**: Add chainable query builder methods -- introduced `QueryBuilder` with `filter()`, `order_by()`, `limit()`, `values()`, `first()`, `last()`. This is the exact class being extended.
- **Issue #75**: Performance audit of query and filter operations -- relevant because `computed_sort` is O(N) post-fetch; should document this.

## Data Flow

1. **Entry point**: User calls `.computed_sort(fn, reverse=True)` on a `QueryBuilder`
2. **QueryBuilder**: Stores `_computed_sort_fn` and `_computed_sort_reverse` on the builder instance
3. **QueryBuilder.all()**: Passes a new `computed_sort` parameter into `_execute_filter()` (or applies it after `_execute_filter()` returns, before limit)
4. **Post-fetch sort**: After `_execute_filter()` returns full results, `sorted(results, key=fn, reverse=reverse)` is applied
5. **Limit**: `limit` is applied after computed sort (slicing the sorted list)
6. **Output**: Sorted, limited list of model instances returned to caller

Key design decision: `computed_sort` should be applied **after** `_execute_filter()` returns but **before** the limit slice. This means the QueryBuilder's `all()` method needs to handle the sort-then-limit sequencing itself rather than delegating limit to `_execute_filter`.

## Architectural Impact

- **New dependencies**: None -- uses Python's built-in `sorted()`
- **Interface changes**: One new method on `QueryBuilder`; no changes to `Query` class internals
- **Coupling**: Minimal -- `computed_sort` is a pure post-processing step with no Redis interaction
- **Data ownership**: No change
- **Reversibility**: Trivially removable -- it is an additive method with no side effects

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1 (code review)

This is a well-scoped additive feature: one new method on `QueryBuilder`, a few lines of sort logic in `all()`, and tests.

## Prerequisites

No prerequisites -- this work has no external dependencies.

## Solution

### Key Elements

- **`computed_sort(fn, reverse=False)` method**: Stores the sort function and direction on the QueryBuilder instance
- **Modified `all()` execution**: When `_computed_sort_fn` is set, applies `sorted()` after fetch, then applies limit
- **Precedence rule**: `computed_sort` takes precedence over `order_by` when both are set (as specified in the issue)

### Flow

**User code** -> `.filter(...)` -> `.computed_sort(fn, reverse=True)` -> `.limit(5)` -> `.all()` -> **fetch all matches** -> **`sorted(results, key=fn, reverse=reverse)`** -> **`results[:limit]`** -> **return**

### Technical Approach

- Add `_computed_sort_fn` and `_computed_sort_reverse` attributes to `QueryBuilder.__init__`
- Add `computed_sort(fn, reverse=False)` method that sets these and returns `self`
- Modify `QueryBuilder.all()`: if `_computed_sort_fn` is set, remove `order_by` from kwargs (computed_sort takes precedence), call `_execute_filter` without limit, apply `sorted()`, then slice by limit
- Propagate `_computed_sort_fn` and `_computed_sort_reverse` in `QueryBuilder.filter()` (when creating new builders)
- The sort function receives a model instance as its argument (per issue spec)

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] Test that `computed_sort` with a function that raises an exception propagates the error cleanly (no silent swallowing)
- [ ] No exception handlers in scope of this change

### Empty/Invalid Input Handling
- [ ] Test `computed_sort` on empty result set returns `[]`
- [ ] Test `computed_sort` with `None` function raises `TypeError`

### Error State Rendering
- No user-visible output -- this is a library API

## Rabbit Holes

- **Async support for computed_sort on QueryBuilder**: The async methods (`async_filter`, `async_all`) live on `Query`, not `QueryBuilder`. Adding async `computed_sort` would require an async QueryBuilder, which is a separate project. Document as future work.
- **Optimizing computed_sort with partial fetch**: Tempting to try heap-based top-k selection instead of full sort. Not worth the complexity at small corpus sizes. Keep it simple with `sorted()`.
- **Caching computed sort results**: The sort function may be expensive, but caching adds complexity. Leave optimization to callers.

## Risks

### Risk 1: Performance with large result sets
**Impact:** `computed_sort` loads all matching records into memory before sorting. At 100K+ records this could be slow.
**Mitigation:** Document that this is O(N) post-fetch. The issue explicitly acknowledges this is acceptable for <10K records.

### Risk 2: Interaction with `values()` projection
**Impact:** When `values()` is used, results are dicts not model instances. The sort function would receive dicts instead of instances.
**Mitigation:** Document that `computed_sort` operates on whatever `all()` returns. If `values()` is used, the function receives dicts.

## Race Conditions

No race conditions identified -- `computed_sort` is a pure in-memory post-processing step with no shared state or concurrent access.

## No-Gos (Out of Scope)

- Async `computed_sort` on Query class (separate feature)
- Persistent computed sort indexes in Redis
- Automatic limit optimization (e.g., heap-based top-k)
- `computed_sort` on `values()` results with special dict handling

## Update System

No update system changes required -- this is a library feature addition with no deployment or migration impact.

## Agent Integration

No agent integration required -- this is a core ORM library feature.

## Documentation

### Feature Documentation
- [ ] Add `computed_sort()` to the Query API section in docs
- [ ] Add usage example to docstring

### External Documentation Site
- [ ] Update query docs page in MkDocs site

### Inline Documentation
- [ ] Docstring on `computed_sort()` method with usage example
- [ ] Code comment noting O(N) performance characteristic

## Success Criteria

- [ ] `QueryBuilder.computed_sort(fn, reverse=False)` method exists and is chainable
- [ ] Results are sorted by the provided function after fetch, before limit
- [ ] `computed_sort` takes precedence over `order_by` when both are set
- [ ] `computed_sort` composes with `filter()`, `limit()`, and `values()`
- [ ] Sort function receives model instances (or dicts when `values()` is used)
- [ ] Empty result sets return `[]` without error
- [ ] `_computed_sort_fn` and `_computed_sort_reverse` propagate through `filter()` chaining
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (computed-sort)**
  - Name: computed-sort-builder
  - Role: Implement `computed_sort()` method and modify `all()` execution
  - Agent Type: builder
  - Resume: true

- **Validator (computed-sort)**
  - Name: computed-sort-validator
  - Role: Verify implementation, run tests, check edge cases
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Implement computed_sort on QueryBuilder
- **Task ID**: build-computed-sort
- **Depends On**: none
- **Assigned To**: computed-sort-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `_computed_sort_fn` and `_computed_sort_reverse` to `QueryBuilder.__init__`
- Add `computed_sort(fn, reverse=False)` method returning `self`
- Propagate computed sort state in `QueryBuilder.filter()` when creating new builders
- Modify `QueryBuilder.all()` to apply computed sort after fetch, before limit
- Write tests: basic sort, reverse sort, with limit, with filter, empty results, chaining with filter, precedence over order_by, interaction with values()

### 2. Validate implementation
- **Task ID**: validate-computed-sort
- **Depends On**: build-computed-sort
- **Assigned To**: computed-sort-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify all success criteria met
- Run full test suite
- Verify docstrings and code comments

### 3. Documentation
- **Task ID**: document-computed-sort
- **Depends On**: validate-computed-sort
- **Assigned To**: computed-sort-builder
- **Agent Type**: documentarian
- **Parallel**: false
- Update query API docs with `computed_sort()` method
- Add usage example matching the issue's proposed API

### 4. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-computed-sort
- **Assigned To**: computed-sort-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all validation commands
- Verify all success criteria met
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check .` | exit code 0 |
| Format clean | `python -m ruff format --check .` | exit code 0 |
| computed_sort method exists | `python -c "from popoto.models.query import QueryBuilder; assert hasattr(QueryBuilder, 'computed_sort')"` | exit code 0 |

---

## Open Questions

1. **Should `computed_sort` work with `values()` projection?** The issue does not mention this case. The plan currently allows it (sort function receives dicts), but should we explicitly block it or document the behavior?

2. **Should `computed_sort` warn or raise when combined with `order_by`?** The issue says computed_sort "takes precedence" -- should it silently ignore `order_by`, or log a warning that `order_by` is being overridden?

3. **Should `computed_sort` be exposed in the public `__init__.py` exports?** Currently `QueryBuilder` is not directly imported by users -- they access it via `Model.query.filter()`. No export changes seem needed, but confirming.
