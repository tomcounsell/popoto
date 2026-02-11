---
status: Ready
type: feature
appetite: Small
owner: valorengels
created: 2026-02-11
tracking: https://github.com/tomcounsell/popoto/issues/117
---

# Client-Side Filtering on Plain Fields

## Problem

`query.filter()` raises `QueryException` when passed a plain `Field` (not `SortedField`, `KeyField`, or `GeoField`):

```python
class Order(Model):
    order_id = AutoKeyField(strategy="uuid4")
    total = SortedField(type=float)
    status = Field(type=str, default="pending")

# Works - total is a SortedField (indexed)
Order.query.filter(total__gte=50)

# Crashes - status is a plain Field (unindexed)
Order.query.filter(status="delivered")
# => QueryException: Invalid filter parameters: status
```

**Current behavior:** Hard crash with `QueryException` on any plain Field filter parameter.

**Desired outcome:** Plain Field params are accepted and applied as client-side post-filters after indexed fields narrow the result set server-side.

## Appetite

**Size:** Small

**Team:** Solo dev, no review needed.

**Interactions:**
- PM check-ins: 0
- Review rounds: 0

The change is narrow and well-scoped: modify `filter_for_keys_set()` to separate unindexed field params from truly unknown params, then post-filter in `_execute_filter()`.

## Prerequisites

No prerequisites -- standard dev environment with Redis running.

## Solution

### Key Elements

- **Param classification**: Separate kwargs into indexed params (routed to Redis), plain field params (for post-filtering), and truly unknown params (raise QueryException)
- **Post-filter engine**: After loading objects from Redis, apply Python-side equality checks for plain field values
- **Debug logging**: Emit a `DEBUG` log when client-side filtering is used so developers can identify optimization opportunities

### Flow

**filter() called** -> classify params (indexed vs plain field vs unknown) -> query Redis with indexed params -> load objects -> post-filter on plain field values -> return results

### Technical Approach

1. In `filter_for_keys_set()`, instead of raising `QueryException` for all remaining kwargs, check if they match a known plain `Field` name on the model. Only raise for truly unknown params.
2. Return the plain field params alongside the key set so `_execute_filter()` can apply them.
3. In `_execute_filter()`, after loading objects, filter them by checking `getattr(obj, field_name) == value` for each plain field param.
4. Support exact match only for plain fields (no `__gt`, `__contains` lookups -- those require indexing).
5. Emit `logging.debug()` for each client-side filter applied.

## Rabbit Holes

- **Adding lookup expressions for plain fields** (`status__contains`, `status__in`, etc.) -- keep it to exact match only in this iteration. Lookup expressions can be added later.
- **Performance warnings at non-DEBUG levels** -- a DEBUG log is sufficient; don't add WARNING-level noise.
- **Changing Field.get_filter_query_params()** to return params -- this would break the architecture. Plain fields intentionally don't advertise filter params; the Query layer handles the fallback.

## Risks

### Risk 1: Silent performance degradation
**Impact:** Users filter on unindexed fields over large datasets without realizing it loads everything.
**Mitigation:** DEBUG log message tells developers which fields triggered client-side filtering. This matches the issue's proposal.

## No-Gos (Out of Scope)

- No lookup expressions for plain fields (no `__gt`, `__in`, `__contains`)
- No changes to `count()` -- count with plain field params would require loading all objects, which is misleading. Keep raising QueryException there.
- No changes to Field.get_filter_query_params() or filter_query() base implementations
- No new field types or mixins

## Documentation

### Inline Documentation
- Update docstrings on `filter_for_keys_set()` and `_execute_filter()` to document the client-side filtering behavior

## Success Criteria

- [ ] `Model.query.filter(plain_field=value)` returns correct results instead of raising QueryException
- [ ] `Model.query.filter(indexed_field=x, plain_field=y)` works as hybrid query
- [ ] Truly unknown params still raise QueryException
- [ ] DEBUG log emitted for each client-side filter applied
- [ ] `count()` with plain field params also works (via post-filtering)
- [ ] Existing tests pass unchanged
- [ ] New test file covers all scenarios

## Team Orchestration

Solo implementation -- no team orchestration needed for Small appetite.

## Step by Step Tasks

### 1. Modify filter_for_keys_set() to classify plain field params
- **Assigned To**: builder
- Separate remaining kwargs into plain field params vs truly unknown params
- Only raise QueryException for truly unknown params
- Return plain field params via a new mechanism (instance variable or return value)

### 2. Modify _execute_filter() to apply post-filtering
- **Depends On**: Task 1
- After loading objects, filter by plain field values
- Emit DEBUG log for each client-side filter
- Handle both full objects and values-mode dicts

### 3. Update count() to support plain field params
- **Depends On**: Task 1
- When plain field params present, fall back to loading + filtering + counting

### 4. Write tests
- **Depends On**: Tasks 1-3
- Test plain field filtering (exact match)
- Test hybrid filtering (indexed + plain)
- Test unknown params still raise QueryException
- Test QueryBuilder chaining with plain fields
- Test count() with plain field params

### 5. Final validation
- **Depends On**: Task 4
- Run full test suite: `pytest`
- Verify no regressions

## Validation Commands

- `pytest tests/test_client_side_filter.py -v` - new tests pass
- `pytest` - all existing tests pass
- `mypy src/` - type checking passes
