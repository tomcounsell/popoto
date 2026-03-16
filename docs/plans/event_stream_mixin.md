---
status: Ready
type: feature
appetite: Small
owner: Valor
created: 2026-03-16
tracking: https://github.com/tomcounsell/popoto/issues/211
pr: https://github.com/tomcounsell/popoto/pull/219
last_comment_id:
---

# EventStreamMixin — Append-Only Mutation Log via Redis Streams

## Problem

Popoto's agent memory primitives (Steps 1-7) handle real-time read/write scoring, but there's no durable, ordered record of mutations for background processing. Pattern detection, knowledge crystallization, and temporal clustering all require a mutation log. Without it, the background compaction pipeline (Step 10: StreamConsumer) has nothing to consume.

**Current behavior:**
Model saves and deletes modify Redis state silently. No audit trail, no way to replay changes, no hook for async processing.

**Desired outcome:**
Every save/delete on an opted-in model produces a Redis Stream entry capturing model class, PK, operation type, timestamp, and configurable metadata fields. The stream is bounded by approximate MAXLEN trimming and partitionable by a key field.

## Prior Art

No prior issues found related to EventStreamMixin or Redis Streams in this repository.

Established mixin patterns to follow:
- **WriteFilterMixin** (PR #214) — mixin that hooks into Model.save() via isinstance check in base.py, with cleanup in Model.delete(). EventStreamMixin follows the same integration pattern.
- **AccessTrackerMixin** (PR #203) — mixin with per-instance Redis data structures and delete-time cleanup.

## Data Flow

1. **Entry point**: Application calls `model.save()` or `model.delete()`
2. **WriteFilter gate**: If model uses WriteFilterMixin, SkipSaveException aborts before any persistence — EventStreamMixin must NOT fire
3. **Model.save() / Model.delete()**: Persists data to Redis hash, updates indexes
4. **EventStreamMixin hook**: After successful persistence, XADD to Redis Stream with entry data
5. **Redis Stream**: Entries accumulate with auto-generated IDs, bounded by MAXLEN ~
6. **Consumer (Step 10)**: Future StreamConsumer reads entries via XREADGROUP (out of scope)

Key insight: the XADD must happen **after** successful persistence (not before), and must **not** fire when WriteFilterMixin discards the record. This means integrating at the same point as `_tag_priority()` — after the save succeeds.

## Architectural Impact

- **New dependency**: None (Redis Streams are built into Redis 5.0+, which Popoto already requires)
- **Interface changes**: New mixin class. Model.save() and Model.delete() in base.py gain new isinstance checks (same pattern as WriteFilterMixin and AccessTrackerMixin)
- **Coupling**: Minimal — mixin is opt-in, stream writing is fire-and-forget within the save pipeline
- **Reversibility**: Easy — remove isinstance checks from base.py and delete the mixin file

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

No prerequisites — Redis Streams are available in Redis 5.0+ and Valkey, which Popoto already targets.

## Solution

### Key Elements

- **EventStreamMixin**: Model mixin class with configuration attributes and XADD logic
- **base.py integration**: isinstance checks after successful save/delete to trigger stream writes
- **Create vs Update detection**: Use `_db_content` — empty dict means create, non-empty means update
- **Public `_xadd_event()` method**: Exposed for direct-to-Redis operations (ConfidenceField.update_confidence, CoOccurrenceField.strengthen) that bypass Model.save()

### Technical Approach

**Mixin design:**

```python
class EventStreamMixin:
    _stream_name: str = "mutations"
    _stream_partition_field: str = None  # field name to partition by
    _stream_max_length: int = 10000
    _stream_metadata_fields: tuple = ()  # additional field names to include
```

All attributes use underscore prefix to avoid Popoto's ModelBase metaclass treating them as Fields (same pattern as WriteFilterMixin's `_wf_min_threshold`).

**Stream key pattern:**
- Without partition: `stream:{stream_name}`
- With partition: `stream:{stream_name}:{partition_value}`

**Stream entry fields (all strings per Redis Streams spec):**
- `model`: Model class name
- `pk`: Redis key of the instance
- `op`: One of `"create"`, `"update"`, `"delete"`
- `ts`: Unix timestamp string
- `changed_fields`: Comma-separated list (update_fields path only; empty string for full saves)
- Plus any fields named in `_stream_metadata_fields`

**Create vs Update detection:**
- `self._db_content` is `{}` (empty dict) on a fresh instance before first save
- After save, `_db_content` is populated with serialized data
- Check at the start of save (before persistence): empty `_db_content` → create, non-empty → update

**Integration points in base.py:**

1. After successful save (both full and partial paths, 4 locations matching WriteFilterMixin pattern):
   ```python
   from ..fields.event_stream import EventStreamMixin
   if isinstance(self, EventStreamMixin):
       self._xadd_mutation("create" or "update", pipeline=pipeline, update_fields=update_fields)
   ```

2. In delete() after field cleanup (same location as AccessTrackerMixin/WriteFilterMixin cleanup):
   ```python
   if isinstance(self, EventStreamMixin):
       self._xadd_mutation("delete", pipeline=pipeline)
   ```

**Pipeline handling:**
- When a pipeline is provided, XADD is queued onto it
- When no pipeline, execute XADD directly
- XADD uses `MAXLEN ~ {max_length}` (approximate trimming — Redis may keep slightly more than max)

**Public `_xadd_event()` for non-save operations:**

The roadmap explicitly requires ConfidenceField and CoOccurrenceField mutations to be loggable. These operations (e.g., `ConfidenceField.update_confidence()`, `CoOccurrenceField.strengthen()`) bypass Model.save() and write to Redis directly. The mixin exposes `_xadd_event(op, extra_fields, pipeline)` that these callers can invoke:

```python
# In ConfidenceField.update_confidence():
if isinstance(model_instance, EventStreamMixin):
    model_instance._xadd_event(
        op="confidence_update",
        extra_fields={"field": field_name, "old": str(old_conf), "new": str(new_conf)},
        pipeline=pipeline,
    )
```

This is a separate method from `_xadd_mutation()` (which is internal to save/delete). Both ultimately call XADD but `_xadd_event()` accepts arbitrary op strings and extra fields.

**WriteFilter interaction:**
- No special code needed. When WriteFilterMixin raises SkipSaveException, save() returns early before reaching the EventStreamMixin integration point. The mixin naturally won't fire.

### Files to Create/Modify

| File | Action |
|------|--------|
| `src/popoto/fields/event_stream.py` | Create — EventStreamMixin class |
| `src/popoto/models/base.py` | Modify — add isinstance checks in save() and delete() |
| `src/popoto/fields/confidence_field.py` | Modify — add `_xadd_event()` call in `update_confidence()` |
| `src/popoto/fields/co_occurrence_field.py` | Modify — add `_xadd_event()` call in `strengthen()` |
| `src/popoto/__init__.py` | Modify — export EventStreamMixin |
| `tests/test_event_stream_mixin.py` | Create — full test suite |
| `docs/features/agent-memory.md` | Modify — update EventStreamMixin status to Shipped |

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] XADD failure (e.g., Redis connection error during stream write) must not prevent save() from succeeding — catch and log, do not re-raise
- [ ] If `_stream_partition_field` references a non-existent field, raise ModelException at class definition time or first save

### Empty/Invalid Input Handling
- [ ] Model with no key fields — PK defaults to auto key, stream entry uses that
- [ ] `_stream_metadata_fields` referencing a None-valued field — include as empty string, don't crash
- [ ] Empty `_stream_name` — raise ModelException

### Error State Rendering
- No user-visible output — this is internal ORM infrastructure

## Rabbit Holes

- **Consumer groups / XREADGROUP**: Out of scope — that's Step 10 (StreamConsumer). The mixin only writes.
- **Exactly-once delivery guarantees**: Redis Streams provide at-least-once via consumer groups. Don't try to add deduplication at the mixin level.
- **Lua scripting for XADD**: Unnecessary — XADD is already atomic. A simple redis-py call suffices.
- **Encoding metadata as msgpack**: Keep it simple — all stream entry values are strings. Consumers can parse as needed.
- **Async XADD**: Not needed. XADD is O(1) amortized and adds <0.5ms. Don't introduce asyncio complexity.

## Risks

### Risk 1: Stream write failure blocks save
**Impact:** If XADD raises an exception inside save(), the model might not persist even though the mutation log is non-critical.
**Mitigation:** Wrap XADD in try/except and log errors. The mutation log is best-effort — save() must always succeed if the data write succeeded. When pipeline is provided, the XADD is part of the atomic transaction; if the pipeline fails, both data and stream entry fail together (which is correct).

### Risk 2: Stream grows unbounded
**Impact:** Memory pressure on Redis if MAXLEN trimming doesn't engage.
**Mitigation:** Use `MAXLEN ~` (approximate) which is the standard Redis approach. Default 10000. Configurable per model.

## Race Conditions

No race conditions identified. XADD is atomic and append-only. Redis Streams handle concurrent writes natively — each XADD gets a unique auto-generated ID. Pipeline mode batches the XADD with the save, ensuring atomicity.

## No-Gos (Out of Scope)

- Stream consumption / XREADGROUP (Step 10)
- Consumer group creation or management
- Stream entry deserialization utilities
- Backfilling existing records into the stream
- Per-field change tracking (diff of old vs new values) — `changed_fields` from `update_fields` is sufficient
- Backpressure or rate limiting on XADD

## Update System

No update system changes required — this is a library feature, not a deployed service.

## Agent Integration

No agent integration required — this is an ORM-level primitive.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/agent-memory.md` — change EventStreamMixin status to Shipped, add usage section
- [ ] Add entry to `docs/fields.md` if field reference page exists

### Inline Documentation
- [ ] Docstrings on EventStreamMixin class and all public methods
- [ ] Module-level docstring explaining Redis Streams key patterns

## Success Criteria

- [ ] `EventStreamMixin` class with `_stream_name`, `_stream_partition_field`, `_stream_max_length`, `_stream_metadata_fields`
- [ ] `save()` produces XADD with op="create" for new instances
- [ ] `save()` produces XADD with op="update" for existing instances
- [ ] `save(update_fields=["x"])` produces entry with changed_fields="x"
- [ ] `delete()` produces XADD with op="delete"
- [ ] MAXLEN ~ trimming keeps stream bounded
- [ ] Partitioned streams work (stream key includes partition field value)
- [ ] Metadata fields included in stream entries
- [ ] WriteFilter-discarded records produce NO stream entries
- [ ] Pipeline support (XADD queued on pipeline)
- [ ] XADD failure does not block save() (non-pipeline path)
- [ ] Synergy test: EventStreamMixin + ConfidenceField (update_confidence produces stream entry with old/new values)
- [ ] Synergy test: EventStreamMixin + CoOccurrenceField (strengthen produces stream entry with delta)
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (event-stream)**
  - Name: event-stream-builder
  - Role: Implement EventStreamMixin and integrate into base.py
  - Agent Type: builder
  - Resume: true

- **Validator (event-stream)**
  - Name: event-stream-validator
  - Role: Verify implementation, run tests, check all criteria
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-updater
  - Role: Update agent-memory.md and field docs
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Implement EventStreamMixin
- **Task ID**: build-event-stream
- **Depends On**: none
- **Assigned To**: event-stream-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/fields/event_stream.py` with EventStreamMixin class
- Implement `_get_stream_key()`, `_build_stream_entry()`, `_xadd_mutation()` methods
- Add isinstance checks in `src/popoto/models/base.py` save() (4 locations: full-save pipeline, full-save internal, partial-save pipeline, partial-save internal) and delete() (1 location)
- Export from `src/popoto/__init__.py`

### 2. Implement Tests
- **Task ID**: build-tests
- **Depends On**: build-event-stream
- **Assigned To**: event-stream-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_event_stream_mixin.py`
- Test create/update/delete operations produce correct stream entries
- Test MAXLEN trimming
- Test partition_field routing
- Test metadata_fields inclusion
- Test update_fields -> changed_fields mapping
- Test WriteFilter synergy (filtered records produce no entries)
- Test ConfidenceField synergy
- Test pipeline support
- Test error resilience (XADD failure doesn't block save)

### 3. Validate Implementation
- **Task ID**: validate-event-stream
- **Depends On**: build-tests
- **Assigned To**: event-stream-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify all success criteria met
- Check that existing tests still pass (no regression)

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-event-stream
- **Assigned To**: docs-updater
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/agent-memory.md` EventStreamMixin section
- Add usage examples

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: event-stream-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all tests
- Verify documentation accuracy
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_event_stream_mixin.py -x -q` | exit code 0 |
| Full suite passes | `pytest tests/ -x -q` | exit code 0 |
| Import works | `python -c "from popoto import EventStreamMixin"` | exit code 0 |
| Lint clean | `black --check src/popoto/fields/event_stream.py` | exit code 0 |

---

## Resolved Questions

1. **Non-pipeline XADD error handling**: Pipeline path: XADD is atomic with save (both succeed or fail). Non-pipeline path: try/except with logging (best-effort). This asymmetry is acceptable — pipeline mode implies the caller wants atomicity; non-pipeline mode is convenience.

2. **CoOccurrenceField / ConfidenceField stream entries**: The roadmap explicitly requires these. Resolved: expose `_xadd_event()` public method on the mixin. Add calls in `ConfidenceField.update_confidence()` and `CoOccurrenceField.strengthen()` — guarded by `isinstance(model_instance, EventStreamMixin)` checks.

3. **Stream key prefix**: The roadmap uses `stream:{stream_name}:{partition}` — deliberately NOT the `$` prefix convention. Streams are externally consumed (by Step 10 StreamConsumer), so human-readable keys are preferred. Every other primitive uses `$` but streams are the exception.
