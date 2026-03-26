---
status: Done
type: bug
appetite: Small
owner: Agent
created: 2026-03-26
tracking: https://github.com/tomcounsell/popoto/issues/283
last_comment_id:
---

# KeyField Index Stale Reads

## Problem

`Model.query.filter()` can miss records that were just created. A consumer calls `Model.create(status="pending", chat_id="123")` and immediately queries `Model.query.filter(chat_id="123", status="pending")` -- the query returns empty. After ~0.5-1s the same query succeeds.

**Current behavior:**
`create()` returns successfully but an immediate `filter()` on the same KeyField values returns an empty set. Workers relying on create-then-query patterns see intermittent "queue empty" false negatives.

**Desired outcome:**
After `Model.create()` returns, any subsequent `filter()` using the same field values must find the record. Index consistency must be guaranteed by the time `create()` / `save()` returns.

## Prior Art

- **PR #148**: "Make save() atomic via internal pipeline" -- Introduced internal pipelines so that the HSET + SADD + on_save calls are batched into a single `pipeline.execute()`. This fixed partial-write visibility (hash exists but index doesn't) for the no-external-pipeline path. However, the bug persists because `pre_save()` performs non-pipelined reads (unique constraint checks via `SCARD`, `SISMEMBER`, `HGET`) that create a window where the pipeline hasn't executed yet but pre_save has already returned.
- **Issue #151**: "Comprehensive edge case tests for field index operations" -- Added tests for index mutation edge cases but did not address timing/atomicity of index writes.

## Data Flow

1. **Entry point**: `Model.create(**kwargs)` or `instance.save()`
2. **pre_save()**: Validates fields, checks unique constraints. Performs **non-pipelined reads** (`HGET`, `SCARD`, `SISMEMBER`) directly against Redis. These reads are not part of the atomic pipeline.
3. **save() -- internal pipeline path** (no external pipeline provided):
   - Creates `internal_pipeline = POPOTO_REDIS_DB.pipeline()`
   - Queues: `HSET` (hash data), `SADD` (class set), field `on_save()` calls (KeyField SADD to index sets)
   - Calls `internal_pipeline.execute()` -- all commands sent as one batch
4. **Return**: `save()` returns the HSET result; `create()` returns the model instance
5. **Query**: `Model.query.filter(field=value)` calls `KeyFieldMixin.filter_query()` which reads `SMEMBERS` on the index set key `$KeyF:ModelName:field_name:value`

The pipeline in step 3 is already atomic (all commands execute in a single round-trip). The index SADD and hash HSET are in the same pipeline. So the question is: why would SMEMBERS not see the SADD result after `pipeline.execute()` returns?

**Root cause hypothesis**: The issue is NOT in the internal pipeline path (which is correctly atomic). The issue occurs when callers use an **external pipeline** -- passing `pipeline=pipe` to `save()` or `create()`. In that case, `save()` queues commands onto the external pipeline but does NOT call `execute()`. The caller is responsible for calling `pipe.execute()` later. If the caller queries between `save(pipeline=pipe)` and `pipe.execute()`, the index writes haven't been flushed yet.

**Secondary hypothesis**: Redis connection pooling or client-side buffering could cause `pipeline.execute()` to return before the server has fully committed the writes, but this is unlikely with standard redis-py synchronous pipelines (they wait for all responses).

**Tertiary hypothesis**: The `pre_save()` unique constraint checks (`SCARD`, `SISMEMBER`, `HGET`) are non-pipelined reads that happen before the pipeline executes. If another process is concurrently writing, these reads could see stale state. However, this affects write correctness, not read-after-write visibility.

## Architectural Impact

- **Interface changes**: None -- `save()` and `filter()` signatures remain identical
- **Coupling**: No new coupling introduced
- **New dependencies**: None
- **Data ownership**: No change
- **Reversibility**: Fully reversible -- changes are internal to save/query paths

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

No prerequisites -- this work has no external dependencies.

## Solution

### Key Elements

- **Atomic save verification**: Confirm that the internal pipeline path is correct and add a test proving create-then-filter works in a single thread
- **External pipeline documentation**: Document that callers using `save(pipeline=pipe)` must call `pipe.execute()` before querying
- **Regression test**: Add a test that creates a record and immediately queries for it via multiple KeyField intersection, asserting the record is found

### Technical Approach

1. **Write a reproducer test** that creates a model with 2+ KeyFields and immediately queries via `filter()` with both field values. If this passes reliably (which it should, given the internal pipeline is atomic), the bug is in the caller's external-pipeline usage, not in Popoto itself.

2. **Audit the `save(pipeline=external)` path** for the case where the caller passes an external pipeline. Confirm that the docstring clearly states the caller must execute the pipeline. Consider adding a warning or raising if a query is attempted on a model whose save hasn't been flushed.

3. **Add `_is_persisted` flag** to model instances: set to `True` only after `pipeline.execute()` completes (internal path) or after the model is loaded from Redis. This provides an observable signal for debugging stale-read issues. When `save()` uses an internal pipeline and `execute()` succeeds, set `_is_persisted = True`. When `save()` uses an external pipeline, leave `_is_persisted = False` until the caller executes.

4. **Verify `pipeline.execute()` is synchronous**: Add a code comment in `save()` confirming that redis-py's `Pipeline.execute()` blocks until all responses are received, guaranteeing write visibility after return.

### Flow

**Model.create()** -> **pre_save()** (validation) -> **save()** (internal pipeline: HSET + SADD + index SADD) -> **pipeline.execute()** (atomic flush) -> **_is_persisted = True** -> **return instance**

Then: **Model.query.filter()** -> **SMEMBERS** on index set -> finds the record

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] No new exception handlers introduced in this work

### Empty/Invalid Input Handling
- [ ] Test `filter()` with KeyField values that match no records returns empty set (existing behavior, verify not broken)
- [ ] Test `filter()` with None values on nullable KeyFields

### Error State Rendering
- [ ] Not applicable -- no user-visible output changes

## Test Impact

No existing tests affected -- this is purely additive (new regression test) plus internal flag addition. The existing test suite for `KeyFieldMixin.on_save()` and `filter_query()` remains valid.

## Rabbit Holes

- **WATCH/MULTI transactions**: Redis WATCH-based optimistic locking would add complexity for marginal benefit since the internal pipeline is already atomic. Not worth pursuing.
- **Lua scripting for atomic index updates**: Overkill for this bug. The pipeline approach is sufficient and avoids Redis module dependencies (Valkey compatibility concern).
- **Read-through fallback in filter()**: The issue suggests falling back to scanning when intersection is empty. This masks the root cause and adds latency. Avoid.
- **Fixing the external-pipeline timing window with auto-execute**: Changing `save(pipeline=pipe)` to auto-execute would break the batching contract. Not viable.

## Risks

### Risk 1: Bug is in the caller, not Popoto
**Impact:** If the stale-read is caused by external-pipeline usage patterns, the fix is documentation + `_is_persisted` flag rather than a code fix in save/query.
**Mitigation:** The reproducer test will confirm whether internal-pipeline creates are immediately queryable. If they are, document the correct usage pattern.

### Risk 2: Redis connection pool returning stale connections
**Impact:** Extremely unlikely with synchronous redis-py but would be hard to reproduce in tests.
**Mitigation:** The reproducer test runs in a single thread with a single connection, isolating this variable.

## Race Conditions

### Race 1: External pipeline save + immediate query
**Location:** `base.py` save() external pipeline path (line ~1233-1305)
**Trigger:** Caller calls `instance.save(pipeline=pipe)` then `Model.query.filter()` before `pipe.execute()`
**Data prerequisite:** Pipeline must be executed before index data is visible
**State prerequisite:** `pipe.execute()` must have returned
**Mitigation:** Document the contract. Add `_is_persisted` flag for debugging. Consider a `logger.warning` if a freshly-saved instance is queried before persistence is confirmed.

### Race 2: Concurrent create + filter across processes
**Location:** `key_field_mixin.py` on_save() and filter_query()
**Trigger:** Process A creates, Process B queries before pipeline.execute() in Process A completes
**Data prerequisite:** SADD must complete before SMEMBERS
**State prerequisite:** Pipeline execution must be complete
**Mitigation:** Already handled by internal pipeline atomicity -- `pipeline.execute()` is synchronous and blocks until all commands complete. Cross-process visibility is guaranteed by Redis's single-threaded command execution model.

## No-Gos (Out of Scope)

- Fixing the old-index-not-removed issue on KeyField value change (the write-side issue mentioned in the issue description -- already worked around by consumers)
- Adding SINTER server-side optimization (the TODO at line 1554 of query.py)
- Changing the external pipeline contract or auto-executing pipelines

## Update System

No update system changes required -- this is an internal library fix.

## Agent Integration

No agent integration required -- this is a core ORM bug fix.

## Documentation

### Inline Documentation
- [ ] Add docstring clarification to `save(pipeline=...)` about when index writes become visible
- [ ] Add code comment in internal pipeline path confirming synchronous execute semantics

### Feature Documentation
- [ ] Update `docs/features/` if a query consistency guide exists

## Success Criteria

- [ ] Reproducer test: `Model.create()` followed by immediate `filter()` with multi-KeyField intersection returns the created record (100% reliable, no flakiness)
- [ ] `_is_persisted` flag is set correctly for both internal and external pipeline paths
- [ ] Docstring on `save()` clearly documents external pipeline visibility semantics
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (save-query-fix)**
  - Name: save-query-builder
  - Role: Implement reproducer test, _is_persisted flag, docstring updates
  - Agent Type: builder
  - Resume: true

- **Validator (save-query-fix)**
  - Name: save-query-validator
  - Role: Verify test reliability, review pipeline atomicity claims
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Write reproducer test
- **Task ID**: build-reproducer-test
- **Depends On**: none
- **Validates**: tests/test_keyfield_stale_reads.py (create)
- **Assigned To**: save-query-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `tests/test_keyfield_stale_reads.py` with a model having 2+ KeyFields
- Test: create instance, immediately filter by both KeyField values, assert found
- Test: create instance, immediately filter by single KeyField value, assert found
- Test: create instance via external pipeline (without execute), filter, assert NOT found, then execute, filter, assert found

### 2. Add _is_persisted flag
- **Task ID**: build-persisted-flag
- **Depends On**: none
- **Assigned To**: save-query-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `_is_persisted = False` to Model.__init__
- Set `_is_persisted = True` after successful `internal_pipeline.execute()` in save()
- Leave `_is_persisted = False` in external pipeline path (caller responsibility)
- Set `_is_persisted = True` after model is loaded from Redis (in query hydration)

### 3. Update save() docstrings
- **Task ID**: build-docstrings
- **Depends On**: none
- **Assigned To**: save-query-builder
- **Agent Type**: builder
- **Parallel**: true
- Clarify in save() docstring that external pipeline callers must execute before querying
- Add inline comment confirming pipeline.execute() is synchronous in redis-py

### 4. Validate all changes
- **Task ID**: validate-all
- **Depends On**: build-reproducer-test, build-persisted-flag, build-docstrings
- **Assigned To**: save-query-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify _is_persisted flag behavior
- Review docstring accuracy

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-all
- **Assigned To**: save-query-builder
- **Agent Type**: documentarian
- **Parallel**: false
- Update inline documentation
- Add query consistency notes if applicable

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Reproducer test passes | `pytest tests/test_keyfield_stale_reads.py -v` | exit code 0 |
| No stale xfails | `grep -rn 'xfail' tests/ \| grep -v '# open bug'` | exit code 1 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| CONCERN | [agent-type] | [The concern raised] | [How/whether it was addressed] |

---

## Open Questions

1. **Can we reproduce in-process?** The issue reporter sees stale reads after ~0.5-1s delay. If the internal pipeline path is truly atomic (which code review suggests), the bug may be in the caller's pipeline usage pattern rather than in Popoto. The reproducer test will answer this definitively. If in-process create-then-filter always works, the fix becomes documentation + debugging tools rather than a code change to save/query.

2. **Should `_is_persisted` be a public API?** It could be useful for consumers to check whether an instance's data has been flushed to Redis, but it adds API surface. An alternative is making it private (`_is_persisted`) and only using it for internal assertions/logging.
