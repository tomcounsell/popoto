---
status: Planning
type: feature
appetite: Medium
owner: Solo dev
created: 2026-03-13
tracking: https://github.com/tomcounsell/popoto/issues/197
last_comment_id:
---

# AccessTrackerMixin — Read Pattern Tracking with Staged vs Confirmed Reads

## Problem

**Current behavior:**
Popoto has no mechanism to track read access patterns. When an agent retrieves memories via `query.get()`, `query.filter()`, or `top_by_decay()`, there is no record of which records were accessed, how often, or how spread out the accesses were. The system cannot distinguish between frequently consulted records (high utility) and stored-but-never-read records (low utility).

**Desired outcome:**
An `AccessTrackerMixin` that tracks read patterns with a two-stage pipeline: reads are first logged to a staging area, then promoted to confirmed status via the ObservationProtocol (#198). This prevents naive "every read strengthens" behavior — only meaningful reads (those the agent actually used) count toward access patterns.

```python
class Memory(Model, AccessTrackerMixin):
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField()

# Reading triggers on_read automatically (stages timestamp)
memories = Memory.query.filter(agent_id="agent-1").top_by_decay("relevance", 5)

# After the agent acts on a memory, confirm the read
memories[0].confirm_access()       # promotes staged → confirmed
memories[1].discard_staged_access() # clears staging without promoting

# Inspect access patterns
print(memories[0].access_count)    # 42
print(memories[0].last_accessed)   # 1741872000.0
```

## Prior Art

- **PR #199 / Issue #193**: DecayingSortedField — the decay scoring that access patterns enhance. Established `top_by_decay()`, `touch()`, and the Lua scripting pattern.
- **PR #190**: `atomic_increment()` — Lua scripting pattern with `cmsgpack`, pipeline support.
- **PR #191**: `ListField(max_length=N)` — capped list pattern that `access_log` follows for storage.
- **PR #189**: `computed_sort()` — QueryBuilder extension pattern.
- **PR #200/201**: CyclicDecayField — companion hash pattern (`HGET`/`HSET` + msgpack) that AccessTracker's metadata hashes follow.

## Data Flow

### Hydration paths (where `on_read` must fire)

There are **5 entry points** where model instances are hydrated from Redis. All ultimately call `decode_popoto_model_hashmap()`:

1. **`Query.get()` → `POPOTO_REDIS_DB.hgetall()` → `decode_popoto_model_hashmap()`** — single-object direct lookup by key.

2. **`Query.get_many_objects()` → pipelined `hgetall()` → `decode_popoto_model_hashmap()`** — bulk loading from `filter()`, `all()`, and `_execute_filter()`. Returns list of instances.

3. **`QueryBuilder.top_by_decay()` → Lua script → pipelined `hgetall()` → `decode_popoto_model_hashmap()`** — decay-ranked retrieval. Hydrates instances from Lua result keys.

4. **`DB_key.get()` → `POPOTO_REDIS_DB.hgetall()` → `decode_popoto_model_hashmap()`** — low-level key-based retrieval.

5. **Async variants** (`async_get`, `async_filter`, `async_all`) — async counterparts of paths 1-2. Use `_async_get_many_objects()` which calls `decode_popoto_model_hashmap()`.

### The hook integration point

`decode_popoto_model_hashmap()` is the **single convergence point** for all hydration. But it is a **pure function** (takes bytes, returns instance) — it doesn't have access to pipeline context or model class metadata about whether AccessTracker is mixed in.

**Design decision:** `on_read()` does NOT fire inside `decode_popoto_model_hashmap()`. Instead, it fires **after hydration, at the query layer**, where we have:
- The model class (to check for AccessTrackerMixin)
- The hydrated instances (to get their redis_keys)
- Pipeline context (to batch RPUSH commands)

The hook fires in these specific locations:
- `Query.get()` — after `decode_popoto_model_hashmap()` returns the instance
- `Query.get_many_objects()` — after the pipeline hydration loop, before returning instances
- `QueryBuilder.top_by_decay()` — after pipeline hydration, before returning instances
- Async variants — same positions in the async paths

### Staging → Confirmation flow

```
query.get()/filter()/top_by_decay()
  → hydrate instances via decode_popoto_model_hashmap()
  → on_read() fires for each instance:
      RPUSH $AT:{ClassName}:staged:{pk} {timestamp}    # single Redis command
  → return instances to caller

[Later, after observation protocol determines the read was meaningful:]

instance.confirm_access()
  → Lua script atomically:
      1. LRANGE staged list (read all staged timestamps)
      2. RPUSH to confirmed access_log
      3. LTRIM confirmed access_log to max_length
      4. INCR access_count by len(staged)
      5. SET last_accessed to max(staged timestamps)
      6. DEL staged list

instance.discard_staged_access()
  → DEL staged list  (single Redis command)
```

## Architectural Impact

- **New dependencies**: None. Same Redis infrastructure.
- **Interface changes**: Adds `on_read()` hook concept to the field/mixin protocol. Adds `confirm_access()`, `discard_staged_access()`, `access_count`, `last_accessed` to models using the mixin. Does NOT modify `on_save()` or `on_delete()` signatures.
- **Coupling**: Medium. The hook integration touches `Query.get()`, `Query.get_many_objects()`, `QueryBuilder.top_by_decay()`, and their async counterparts. But the coupling is conditional — `on_read()` only fires when `issubclass(model_class, AccessTrackerMixin)`.
- **Data ownership**: AccessTrackerMixin owns 3 new Redis key patterns per instance: `$AT:{ClassName}:staged:{pk}`, `$AT:{ClassName}:access_log:{pk}`, `$AT:{ClassName}:meta:{pk}` (hash with access_count and last_accessed).
- **Reversibility**: Fully reversible. New mixin, no modifications to existing field or model behavior when mixin is absent.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (hook placement validation)
- Review rounds: 1 (code review for query.py changes)

The complexity is in the hook integration — touching 5+ query paths without breaking existing behavior. The mixin itself and the Lua script are straightforward (following established patterns from CyclicDecayField and ListField).

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis server running | `redis-cli ping` | Staging lists, access logs, Lua script |
| DecayingSortedField exists | `python -c "from popoto import DecayingSortedField; print('OK')"` | Synergy test dependency |
| Existing tests pass | `pytest tests/ -x -q` | No regressions baseline |

## Solution

### Key Elements

- **AccessTrackerMixin**: A model mixin (not a field) that adds read tracking behavior to any Model. Follows Python mixin pattern — `class Memory(Model, AccessTrackerMixin)`.
- **`on_read()` hook**: Fires at the query layer after hydration. Single `RPUSH` per instance — lightweight, no Lua. Controlled by `track_reads` class attribute (default `True`).
- **`confirm_access()` method**: Atomic Lua script that promotes staged timestamps to the confirmed access log.
- **`discard_staged_access()` method**: Clears staging list without promotion.
- **Metadata hash**: Per-instance hash storing `access_count` (int) and `last_accessed` (float). Updated atomically by the confirm Lua script.

### Flow

**Query retrieves instances** → `on_read()` stages timestamps in per-instance Redis lists

**Application observes agent used the memory** → `instance.confirm_access()` atomically promotes

**Application observes agent ignored the memory** → `instance.discard_staged_access()` clears staging

**Future query** → `access_count` and `last_accessed` available as properties on hydrated instances

### Technical Approach

1. **AccessTrackerMixin class** (`src/popoto/fields/access_tracker.py`):
   - Class-level config: `max_access_log = 100`, `track_reads = True`
   - `_get_staged_key(instance)` → `$AT:{ClassName}:staged:{redis_key}`
   - `_get_access_log_key(instance)` → `$AT:{ClassName}:access_log:{redis_key}`
   - `_get_meta_key(instance)` → `$AT:{ClassName}:meta:{redis_key}`
   - `on_read(instance, pipeline=None)` → `RPUSH` timestamp to staged list
   - `confirm_access(pipeline=None)` → Lua script: stage→confirm promotion
   - `discard_staged_access(pipeline=None)` → `DEL` staged list
   - Properties: `access_count`, `last_accessed` (read from meta hash, cached on instance)
   - `on_delete()` override → clean up all 3 Redis keys

2. **Query layer integration** (`src/popoto/models/query.py`):
   - Helper: `_fire_on_read(model_class, instances)` — checks `issubclass(model_class, AccessTrackerMixin)`, calls `on_read()` for each instance via a pipeline batch.
   - Insert call after hydration in: `Query.get()`, `Query.get_many_objects()`, `QueryBuilder.top_by_decay()`, and async variants.
   - The helper uses a pipeline to batch all RPUSH commands — one round trip regardless of instance count.

3. **Confirm Lua script** (embedded in `access_tracker.py`):
   ```
   KEYS[1] = staged list key
   KEYS[2] = access log key
   KEYS[3] = meta hash key
   ARGV[1] = max_access_log (cap)

   -- Read all staged timestamps
   local staged = redis.call('LRANGE', KEYS[1], 0, -1)
   if #staged == 0 then return 0 end

   -- Append to confirmed access log
   for _, ts in ipairs(staged) do
       redis.call('RPUSH', KEYS[2], ts)
   end

   -- Trim to cap
   redis.call('LTRIM', KEYS[2], -ARGV[1], -1)

   -- Update metadata
   redis.call('HINCRBY', KEYS[3], 'access_count', #staged)
   redis.call('HSET', KEYS[3], 'last_accessed', staged[#staged])

   -- Clear staging
   redis.call('DEL', KEYS[1])

   return #staged
   ```

4. **Opt-out mechanism**: `Model.query.filter(...).no_track()` returns a QueryBuilder that suppresses `on_read()`. Needed for internal operations (reindex, migration) that shouldn't count as reads. Implemented as a flag on QueryBuilder that `_fire_on_read()` checks.

5. **Delete cleanup**: Override model's `delete()` or hook into `on_delete()` to remove staged, access_log, and meta keys for the deleted instance.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `confirm_access()` on unsaved model raises `TypeError` (same pattern as `touch()`)
- [ ] `confirm_access()` when staging list is empty returns 0 (no-op, no error)
- [ ] `discard_staged_access()` when staging list is empty is a no-op
- [ ] `on_read()` silently skips instances without a redis_key (unsaved instances that somehow got into results)

### Empty/Invalid Input Handling
- [ ] `on_read()` with empty instance list is a no-op
- [ ] `access_count` on never-accessed instance returns 0
- [ ] `last_accessed` on never-accessed instance returns None
- [ ] `confirm_access()` with no staged reads returns 0

### Error State Rendering
- [ ] Not applicable — this is a data layer feature with no user-visible rendering

## Rabbit Holes

- **Spacing-effect scoring computation**: The synergy test mentions `B = ln(Σ t_j^(-d))` for priority scoring based on access log timestamps. This is a *query-time computation* that belongs in a future Lua script or `computed_sort()` function — NOT in AccessTrackerMixin itself. The mixin stores timestamps; scoring over them is a separate concern.
- **Automatic confirmation**: Tempting to auto-confirm reads after a timeout. But the whole point of staging is that confirmation is driven by the ObservationProtocol (#198). Don't add timer-based auto-confirmation.
- **on_read in decode_popoto_model_hashmap()**: Tempting to put the hook in the single convergence point for hydration. But `decode_popoto_model_hashmap()` is a pure deserialization function with no Redis connection context. The hook belongs at the query layer where we have pipeline access and model class metadata.
- **Per-field access tracking**: The issue defines access tracking at the model level (mixin), not the field level. Don't create an `AccessTrackerField` — it's a mixin on the model.
- **Async on_read**: The async query paths (`async_get`, `async_filter`) need `on_read` too, but `on_read` itself is a sync Redis `RPUSH`. Use `to_thread()` for the async paths, same pattern as `async_filter` uses for `filter_for_keys_set`.

## Risks

### Risk 1: Query layer modification scope
**Impact:** Touching 5+ methods in `query.py` creates regression risk for all query operations.
**Mitigation:** The hook is a single helper function `_fire_on_read()` called after hydration — it doesn't modify the hydration logic itself. Conditional on `issubclass(model_class, AccessTrackerMixin)`. Full regression test suite runs.

### Risk 2: Performance impact of on_read RPUSH per instance
**Impact:** Every query on AccessTracker-enabled models adds one `RPUSH` per instance returned.
**Mitigation:** Batched via pipeline — single round trip regardless of count. `RPUSH` is O(1). For 100 instances, that's 100 commands in one pipeline.execute() — ~1ms overhead. The `no_track()` opt-out is available for bulk operations.

### Risk 3: Orphaned staging lists
**Impact:** If staging timestamps are never confirmed or discarded, they accumulate in Redis.
**Mitigation:** Staging lists have no TTL by default (the ObservationProtocol determines timing). Document this as a responsibility of the application layer. Future work: optional TTL on staging lists via `staging_ttl` class attribute.

## Race Conditions

### Race 1: Concurrent on_read and confirm_access
**Location:** `AccessTrackerMixin.on_read()` and `confirm_access()`
**Trigger:** Thread A stages a read via `on_read()` while Thread B runs `confirm_access()`.
**Data prerequisite:** Staged list must exist.
**State prerequisite:** Both threads reference the same model instance's staging key.
**Mitigation:** `confirm_access()` is a Lua script (atomic). It reads and deletes the staging list in one operation. A concurrent `RPUSH` from `on_read()` either lands before the Lua script reads (and gets promoted) or after the DEL (and starts a new staging list). Both outcomes are correct — no data loss, no double-counting.

### Race 2: Concurrent confirm_access calls
**Location:** `confirm_access()` Lua script
**Trigger:** Two threads call `confirm_access()` on the same instance simultaneously.
**Data prerequisite:** Staged list with timestamps.
**State prerequisite:** N/A.
**Mitigation:** Lua scripts are atomic in Redis — only one executes at a time. The first promotes all staged reads. The second finds an empty staging list and returns 0. No double-counting.

## No-Gos (Out of Scope)

- **Spacing-effect scoring** — the mixin stores timestamps, not scores. Scoring is a query-time concern for CompositeScoreQuery or computed_sort.
- **ObservationProtocol integration** — AccessTracker provides `confirm_access()` and `discard_staged_access()` as the API surface. The ObservationProtocol (#198) calls these methods — that integration is in issue #198.
- **Automatic confirmation** — no timer-based auto-confirm. Confirmation is application-driven.
- **Per-field access tracking** — access tracking is model-level (mixin), not field-level.
- **Async `on_read` implementation** — use `to_thread()` for async paths, not a native async RPUSH.
- **Staging list TTL** — future work. Document as application responsibility for now.
- **Modifying `decode_popoto_model_hashmap()`** — the hook fires at the query layer, not in the deserialization function.

## Update System

No update system changes required — this is a library feature addition with no deployment or service concerns.

## Agent Integration

No agent integration required — this is a Popoto library primitive. Downstream consumers (like the Behavioral Episode Memory System) will use it, but that integration is in a different repository.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/agent-memory.md` AccessTracker section with concrete API examples
- [ ] Add cross-reference to CyclicDecayField docs for the staging → confirmation pattern

### External Documentation Site
- [ ] Add AccessTrackerMixin to MkDocs field reference
- [ ] Include usage examples with the two-stage read pattern

### Inline Documentation
- [ ] Docstrings on `AccessTrackerMixin`, `confirm_access()`, `discard_staged_access()`, `on_read()`
- [ ] Inline comments on the Lua confirm script
- [ ] Document the Redis key patterns

## Success Criteria

- [ ] `AccessTrackerMixin` is a model mixin that adds read tracking to any Model
- [ ] `on_read()` appends timestamp to staging list (single RPUSH per instance, batched via pipeline)
- [ ] `on_read()` fires automatically in `Query.get()`, `Query.get_many_objects()`, `QueryBuilder.top_by_decay()`, and async variants
- [ ] `confirm_access()` atomically: reads staged, appends to confirmed log, trims to max_access_log, increments access_count, updates last_accessed, deletes staged
- [ ] `discard_staged_access()` clears staging without affecting confirmed log
- [ ] Access log is capped at `max_access_log` (default 100)
- [ ] `access_count` and `last_accessed` are readable properties returning 0/None when never accessed
- [ ] `no_track()` QueryBuilder method suppresses `on_read()` for bulk operations
- [ ] Model deletion cleans up all 3 AccessTracker Redis keys
- [ ] Models WITHOUT AccessTrackerMixin are completely unaffected (zero overhead)
- [ ] Tests: on_read() stages timestamps without affecting confirmed log
- [ ] Tests: confirm_access() promotes staged reads, increments count, updates last_accessed
- [ ] Tests: discard_staged_access() clears staging without affecting confirmed log
- [ ] Tests: access_log capping works (stage 150 reads, confirm, verify 100 in log)
- [ ] Tests: concurrent on_read + confirm is safe (no data loss)
- [ ] Tests: spacing effect — spaced reads over 3 days produce different access log than massed reads
- [ ] Synergy test: confirmed access log enables priority score computation `B = ln(Σ t_j^(-d))`
- [ ] `partition_by` on DecayingSortedField interacts correctly with AccessTracker keys
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (access-tracker)**
  - Name: tracker-builder
  - Role: Implement AccessTrackerMixin, Lua confirm script, query layer hooks, no_track(), delete cleanup
  - Agent Type: builder
  - Resume: true

- **Test Engineer (access-tests)**
  - Name: tracker-tester
  - Role: Write comprehensive tests for staging, confirmation, discarding, capping, concurrency, synergy
  - Agent Type: test-engineer
  - Resume: true

- **Validator (integration)**
  - Name: integration-validator
  - Role: Verify all success criteria, run full test suite, check no regressions in query behavior
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Create AccessTrackerMixin class
- **Task ID**: build-mixin
- **Depends On**: none
- **Assigned To**: tracker-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/fields/access_tracker.py` with `AccessTrackerMixin`
- Implement `on_read(instance, pipeline=None)` — single RPUSH to staging list
- Implement `confirm_access(pipeline=None)` — atomic Lua script for stage→confirm promotion
- Implement `discard_staged_access(pipeline=None)` — DEL staging list
- Implement `access_count` and `last_accessed` properties (read from meta hash)
- Implement Redis key generation methods for staged, access_log, and meta keys
- Add export to `src/popoto/__init__.py` and `__all__`

### 2. Integrate on_read hook into Query layer
- **Task ID**: build-query-hooks
- **Depends On**: build-mixin
- **Assigned To**: tracker-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `_fire_on_read(model_class, instances)` helper to `query.py`
- Insert hook in `Query.get()` after `decode_popoto_model_hashmap()`
- Insert hook in `Query.get_many_objects()` after pipeline hydration loop
- Insert hook in `QueryBuilder.top_by_decay()` after pipeline hydration
- Insert hook in async variants (`async_get`, `_async_get_many_objects`)
- Implement `no_track()` on QueryBuilder that sets a flag suppressing `_fire_on_read`
- Ensure hook is conditional on `issubclass(model_class, AccessTrackerMixin)`

### 3. Add delete cleanup
- **Task ID**: build-cleanup
- **Depends On**: build-mixin
- **Assigned To**: tracker-builder
- **Agent Type**: builder
- **Parallel**: true
- Override or extend `delete()` behavior for AccessTrackerMixin models
- Clean up staged, access_log, and meta Redis keys on instance deletion
- Support pipeline parameter for atomic deletion

### 4. Write tests
- **Task ID**: build-tests
- **Depends On**: build-query-hooks, build-cleanup
- **Assigned To**: tracker-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Test on_read() stages timestamps (single and batch)
- Test confirm_access() promotes staged, increments count, updates last_accessed
- Test discard_staged_access() clears staging without affecting confirmed
- Test access_log capping (150 staged → confirm → 100 in log)
- Test access_count and last_accessed properties (zero-state and after confirm)
- Test no_track() suppresses on_read
- Test delete cleanup removes all 3 keys
- Test models without AccessTrackerMixin are unaffected
- Test concurrent on_read + confirm (threading-based)
- Test spacing effect (different timestamp distributions)
- Synergy test: priority score computation from confirmed access log
- Regression: existing query tests still pass

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-tests
- **Assigned To**: integration-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite (`pytest tests/ -x -q`)
- Verify all success criteria met
- Check no regressions in existing Query, DecayingSortedField, CyclicDecayField tests
- Verify exports in `__init__.py`

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| AccessTracker tests pass | `pytest tests/test_access_tracker.py -x -q` | exit code 0 |
| Decay tests still pass | `pytest tests/test_decaying_sorted_field.py -x -q` | exit code 0 |
| Query tests still pass | `pytest tests/test_queries.py -x -q` | exit code 0 |
| AccessTrackerMixin export | `python -c "from popoto import AccessTrackerMixin; print('OK')"` | output contains OK |
| Lint clean | `python -m ruff check src/popoto/fields/access_tracker.py` | exit code 0 |

---

## Resolved Design Decisions

1. **on_read fires at the query layer, not in decode_popoto_model_hashmap()** — `decode_popoto_model_hashmap()` is a pure deserialization function with no Redis connection context, pipeline access, or model class metadata. The query layer methods (`get()`, `get_many_objects()`, `top_by_decay()`) have all three. This mirrors how `on_save()` fires at the `Model.save()` level, not in the encoding function.

2. **on_read is a single RPUSH, not a Lua script** — per the issue spec. Staging must be as lightweight as possible because it fires on every query. A Lua script is overkill for a single append operation.

3. **Confirmation is atomic via Lua** — the promote-and-delete must be atomic to prevent double-counting in concurrent scenarios. Same Lua scripting pattern as `atomic_increment()` (PR #190).

4. **Mixin pattern, not field pattern** — access tracking applies to the model as a whole, not to a single field. A model either tracks reads or doesn't. This follows the composition pattern from the roadmap: `class Memory(Model, AccessTrackerMixin)`.

5. **Metadata stored in a dedicated hash, not on the model hash** — `access_count` and `last_accessed` are tracked in `$AT:{ClassName}:meta:{pk}`, not as fields on the model. This keeps the model hash clean and avoids schema changes for existing models adding the mixin.
