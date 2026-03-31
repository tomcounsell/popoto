---
status: Ready
type: feature
appetite: Small
owner: Valor
created: 2026-03-31
tracking: https://github.com/tomcounsell/popoto/issues/318
last_comment_id:
---

# Query.get_many() for Bulk Key Hydration

## Problem

After any set-based query (sorted set range, key intersection, RRF fusion), users end up with N redis keys that need hydrating into model instances. The only public option today is a loop of `Model.query.get(redis_key=key)` calls, which issues N sequential HGETALL round-trips.

**Current behavior:**
```python
results = []
for key in ranked_keys:
    obj = Model.query.get(redis_key=key)
    if obj:
        results.append(obj)
# 20-50 sequential Redis round-trips for a typical retrieval pipeline
```

**Desired outcome:**
```python
instances = Model.query.get_many(redis_keys=["Key:a", "Key:b", "Key:c"])
# Single pipelined round-trip, order preserved, None for missing keys
```

## Prior Art

No prior issues or PRs found related to a public `get_many` API.

Internally, `Query.get_many_objects()` (line 2399) is a static method that serves as the core bulk retrieval engine for `filter()`, `all()`, etc. It takes `db_keys` as a **set of bytes**, supports ordering/limit/projection, and silently drops missing keys. The proposed `get_many` is a different interface: a public instance method taking a **list of string** redis keys, preserving input order, and returning `None` placeholders for missing keys. The internal method is not suitable as a public API because (a) it takes bytes not strings, (b) it takes a set so order is lost, (c) it silently drops missing keys rather than preserving positional correspondence.

## Data Flow

1. **Entry point**: User calls `Model.query.get_many(redis_keys=["Key:a", "Key:b"])`
2. **Pipeline construction**: Create a Redis pipeline, issue `HGETALL` for each key
3. **Pipeline execution**: Single round-trip to Redis, returns list of hashmaps in order
4. **Deserialization**: Each non-empty hashmap is decoded via `decode_popoto_model_hashmap()`; empty hashmaps become `None`
5. **on_read hook**: `_fire_on_read()` is called with the non-None instances (for AccessTrackerMixin support)
6. **Output**: Ordered list of instances/None returned to caller

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

No prerequisites -- this work has no external dependencies. Uses only existing Redis connection and decoding infrastructure.

## Solution

### Key Elements

- **`Query.get_many()`**: New public instance method on the Query class accepting a list of string redis keys
- **`AsyncQuery.async_get_many()`**: Async counterpart using native async Redis pipeline

### Flow

**User code** --> `Model.query.get_many(redis_keys=[...])` --> **Pipeline HGETALL** --> **decode each hashmap** --> **fire on_read** --> **return ordered list**

### Technical Approach

- Add `get_many()` as an instance method on `Query` (near `get()` at ~line 1548), not a static/class method
- Accept `redis_keys: list[str]` as a keyword argument (matching `get(redis_key=str)` naming convention)
- Use `POPOTO_REDIS_DB.pipeline()` to batch all HGETALL calls (pattern already used 6+ times in query.py)
- Preserve input order: zip results with input keys, return `None` for empty hashmaps
- Call `_fire_on_read()` with non-None instances for AccessTrackerMixin compatibility
- Add `async_get_many()` on `AsyncQuery` using `get_async_redis_db()` and async pipeline
- Accept optional `skip_none: bool = False` parameter -- when True, filter out None entries (convenience for callers who do not need positional correspondence)

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] No exception handlers in the new method -- errors propagate naturally (bad keys, connection errors)

### Empty/Invalid Input Handling
- [ ] Empty list input returns empty list immediately (no pipeline created)
- [ ] Keys that do not exist in Redis produce `None` in the output list
- [ ] Non-string keys raise TypeError (enforced by Redis client)

### Error State Rendering
- Not applicable -- this is a data access method with no user-visible rendering

## Test Impact

No existing tests affected -- this is a greenfield feature adding a new public method with no modifications to existing methods or behavior.

## Rabbit Holes

- **Refactoring existing pipeline+HGETALL patterns to use get_many internally**: The internal patterns in `filter()`, `composite_score()`, etc. work with bytes keys and have specialized logic (scoring, ordering). Unifying them with `get_many` would be a large refactor with no user benefit. Leave them as-is.
- **Adding db_key support**: The `get()` method accepts both `db_key` and `redis_key`. For `get_many`, only `redis_keys` (strings) is needed. Users with DB_key objects can trivially do `[k.redis_key for k in db_keys]`. Adding a parallel `db_keys` parameter adds complexity for minimal value.
- **Chunking large key lists**: Redis pipelines handle thousands of commands efficiently. Adding chunking logic adds complexity without demonstrated need. Can be added later if profiling shows a need.
- **Adding ordering/limit/projection parameters**: The internal `get_many_objects` has these. The public `get_many` should stay simple -- users who need ordering can sort the input list or use `filter()`.

## Risks

### Risk 1: Naming collision with internal `get_many_objects`
**Impact:** Developer confusion about which method to use
**Mitigation:** Clear docstring on `get_many()` explaining it is the public API for bulk key lookup, while `get_many_objects()` is an internal method used by `filter()`/`all()`. Different parameter signatures make them unambiguous.

## Race Conditions

No race conditions identified -- `get_many` is a read-only operation using a single atomic pipeline execution. No shared mutable state is involved.

## No-Gos (Out of Scope)

- No modification of existing internal methods (`get_many_objects`, `_async_get_many_objects`)
- No chunking/batching of large key lists
- No `db_keys` parameter (only `redis_keys` strings)
- No ordering, limit, or projection parameters
- No caching layer

## Update System

No update system changes required -- this is an upstream library feature in popoto.

## Agent Integration

No agent integration required -- this is a library method in the popoto package.

## Documentation

### Inline Documentation
- [ ] Comprehensive docstring on `get_many()` with Args, Returns, Example sections (matching `get()` docstring style)
- [ ] Comprehensive docstring on `async_get_many()` matching async conventions in the file

### External Documentation Site
- [ ] No external docs site changes needed -- popoto does not currently have a docs site

## Success Criteria

- [ ] `Model.query.get_many(redis_keys=[...])` returns ordered list with None for missing keys
- [ ] `Model.query.async_get_many(redis_keys=[...])` async equivalent works
- [ ] Empty input returns empty list without hitting Redis
- [ ] `_fire_on_read` is called for AccessTrackerMixin compatibility
- [ ] `skip_none=True` filters out None entries
- [ ] Tests pass (`/do-test`)

## Team Orchestration

### Team Members

- **Builder (get-many)**
  - Name: query-builder
  - Role: Implement get_many and async_get_many methods
  - Agent Type: builder
  - Resume: true

- **Validator (get-many)**
  - Name: query-validator
  - Role: Verify implementation and run tests
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Implement get_many() on Query class
- **Task ID**: build-get-many
- **Depends On**: none
- **Validates**: tests/test_get_many.py (create)
- **Assigned To**: query-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `get_many(self, redis_keys: list, skip_none: bool = False) -> list` method to `Query` class after the existing `get()` method (~line 1548)
- Use `POPOTO_REDIS_DB.pipeline()` + `hgetall` pattern
- Call `decode_popoto_model_hashmap()` for non-empty hashmaps, `None` otherwise
- Call `_fire_on_read()` with non-None instances
- Return empty list immediately for empty input
- When `skip_none=True`, filter None from result before returning

### 2. Implement async_get_many() on AsyncQuery class
- **Task ID**: build-async-get-many
- **Depends On**: none
- **Validates**: tests/test_get_many.py (create)
- **Assigned To**: query-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `async_get_many(self, redis_keys: list, skip_none: bool = False) -> list` method to `AsyncQuery` after `async_get()`
- Use `get_async_redis_db()` + async pipeline pattern
- Mirror sync implementation logic

### 3. Write tests
- **Task ID**: build-tests
- **Depends On**: build-get-many, build-async-get-many
- **Validates**: tests/test_get_many.py
- **Assigned To**: query-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_get_many.py` with:
  - Test basic bulk retrieval (save 3 objects, get_many with their keys, verify all returned)
  - Test order preservation (keys in specific order, verify output matches)
  - Test missing keys return None at correct positions
  - Test empty input returns empty list
  - Test skip_none=True filters None entries
  - Test mixed existing/missing keys
  - Test async_get_many mirrors sync behavior
  - Test single key (degenerate case)

### 4. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-tests
- **Assigned To**: query-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify lint and format pass
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_get_many.py -x -q` | exit code 0 |
| Full suite | `pytest tests/ -x -q --timeout=60` | exit code 0 |
| Lint clean | `python -m ruff check src/popoto/models/query.py` | exit code 0 |
| Format clean | `python -m ruff format --check src/popoto/models/query.py` | exit code 0 |
| Method exists | `python -c "from popoto.models.query import Query; assert hasattr(Query, 'get_many')"` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

No open questions -- the implementation is straightforward, following well-established patterns already present in query.py.
