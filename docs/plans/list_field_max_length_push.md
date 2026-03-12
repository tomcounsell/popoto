---
status: Implemented
type: feature
appetite: Medium
owner: Solo dev
created: 2026-03-12
tracking: https://github.com/tomcounsell/popoto/issues/180
last_comment_id:
---

# ListField(max_length=N) with push() method

## Problem

ListField currently stores its entire value atomically in the model's Redis hash via msgpack serialization. To append a single item, you must read the full list, append in Python, and write the entire list back. This is inefficient for append-heavy patterns like event logs, tool call sequences, and capped accumulators.

**Current behavior:**
```python
session = AgentSession.query.get(id=session_id)
full_list = session.tool_sequence  # deserialize entire list
full_list.append(("test", "bash", 3))
session.tool_sequence = full_list
session.save()  # re-serialize entire list
```

**Desired outcome:**
```python
session.tool_sequence.push(("test", "bash", 3))  # single LPUSH + LTRIM, no full read/write
```

## Prior Art

No prior issues found related to this work. This is greenfield development on the existing ListField.

## Data Flow

1. **Entry point**: User calls `instance.field_name.push(value)` on a ListField with `max_length` set
2. **ListField.push()**: Serializes the value with msgpack, computes the Redis list key (`{model_db_key}::field_name`), executes `LPUSH` + `LTRIM 0 max_length-1` atomically via pipeline
3. **Read path**: When the model is loaded from Redis, ListField with `max_length` reads from the separate Redis list key via `LRANGE 0 -1` instead of the hash field, deserializes each element
4. **Save path**: When `model.save()` is called and the field has `max_length`, the `on_save()` hook writes the full list to the Redis list key (replacing any existing list) and skips storing in the hash
5. **Delete path**: `on_delete()` hook deletes the Redis list key `{model_db_key}::field_name`

## Architectural Impact

- **New dependencies**: None. Uses existing redis-py LPUSH, LTRIM, LRANGE, DEL commands.
- **Interface changes**: ListField gains `max_length` parameter and `push()` method. These are additive; no existing signatures change.
- **Coupling**: ListField with `max_length` couples the field to the model instance's `db_key` (needs the key to compute the Redis list key). This is the same pattern used by SortedFieldMixin and GeoField for their secondary data structures.
- **Data ownership**: The list data moves from being embedded in the model's hash to a separate Redis list key. This is a storage-level change invisible to the user API.
- **Reversibility**: Fully reversible. Removing `max_length` returns to current behavior. Data migration would require reading the Redis list and re-saving to the hash.

## Appetite

**Size:** Medium

**Team:** Solo dev + PM

**Interactions:**
- PM check-ins: 1 (scope alignment on edge cases)
- Review rounds: 1 (code review)

## Prerequisites

No prerequisites -- this work has no external dependencies.

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis running | `redis-cli ping` | Test execution |

## Solution

### Key Elements

- **CappedListProxy**: A list-like wrapper class returned by ListField when `max_length` is set. Holds a reference to the model instance, field name, and max_length. Provides `push()` method for direct Redis LPUSH + LTRIM.
- **ListField `on_save()` override**: When `max_length` is set, writes the list value to a separate Redis list key using `DEL` + `RPUSH *values` instead of storing in the hash.
- **ListField `on_delete()` override**: When `max_length` is set, deletes the Redis list key.
- **ListField read integration**: When a model is loaded and the field has `max_length`, reconstruct the list from `LRANGE` on the separate Redis list key.

### Flow

**Define model** with `ListField(max_length=N)` --> **Save instance** (on_save writes to Redis list key) --> **Push items** via `instance.field.push(value)` (LPUSH + LTRIM) --> **Read instance** (LRANGE reconstructs list) --> **Delete instance** (on_delete removes Redis list key)

### Technical Approach

- The Redis list key pattern is `{model_db_key}::field_name` (double colon to distinguish from the model key delimiter which uses single colon)
- `push()` uses `LPUSH` to prepend (newest first) and `LTRIM 0 max_length-1` to cap the list
- `on_save()` with `max_length`: uses `DEL key` + `RPUSH key *values` in a pipeline to atomically replace the list. The field value is excluded from the hash serialization by returning `None` from `format_value_pre_save()` or by handling it in encoding
- `on_delete()` with `max_length`: issues `DEL` on the list key
- When loading a model, the field detects `max_length` and fetches from the Redis list key. This requires hooking into the decode path or using a post-load hook
- Each list element is individually msgpack-serialized so complex types (tuples, dicts) round-trip correctly
- `push()` requires the model instance to be saved first (must have a `_redis_key`); raises `ModelException` otherwise

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `push()` on an unsaved model instance raises `ModelException` with clear message
- [ ] `push()` when `max_length` is not set raises `ModelException`
- [ ] Redis connection failure during `push()` propagates the exception (no silent swallowing)

### Empty/Invalid Input Handling
- [ ] `push(None)` stores None as a valid list element (consistent with ListField null behavior)
- [ ] Empty list `[]` as initial value works correctly with `max_length`
- [ ] Model with `max_length` ListField and no data returns empty list, not None

### Error State Rendering
- [ ] Not applicable -- no user-visible rendering

## Rabbit Holes

- **Generic Redis list operations (pop, insert, index)**: Only `push()` is needed. Adding full list manipulation API is scope creep and should be a separate issue.
- **Async push() method**: The async model methods use `to_thread()` wrappers. Adding native async push is not needed in this iteration.
- **Automatic migration from hash-embedded to Redis list**: If a user adds `max_length` to an existing ListField with data, the old data in the hash is ignored. Do not build an automatic migration path.
- **Encoding optimization**: Storing each element individually vs. storing the whole list as one msgpack blob. Individual element serialization is cleaner for LPUSH but slightly more overhead. Do not optimize prematurely.

## Risks

### Risk 1: Read path integration complexity
**Impact:** Loading a model with `max_length` ListField requires fetching data from both the hash (regular fields) and a separate Redis list key. This adds a second Redis call during model load.
**Mitigation:** Use a pipeline to batch the HGETALL and LRANGE calls. Alternatively, lazy-load the list data on first access, similar to Relationship fields.

### Risk 2: Encoding consistency
**Impact:** List elements must be individually serialized/deserialized to support complex types (tuples, Decimals). Inconsistent encoding between `push()` and `save()` could cause data corruption.
**Mitigation:** Extract a shared encode/decode function for list elements. Test round-trip with all supported types.

## Race Conditions

### Race 1: Concurrent push() and save()
**Location:** ListField on_save and push methods
**Trigger:** One process calls `push()` while another calls `save()` on the same model instance with a full list replacement
**Data prerequisite:** Model must be saved (have a redis_key)
**State prerequisite:** Redis list key exists
**Mitigation:** `save()` uses `DEL + RPUSH` in a pipeline, which atomically replaces the list. A concurrent `push()` that executes between DEL and RPUSH would be lost. This is acceptable -- document that `save()` replaces the list entirely and concurrent `push()` may be lost. Users should choose one pattern per field: either `push()` or full `save()`.

## No-Gos (Out of Scope)

- Pop/dequeue operations (RPOP, LPOP)
- Async `push()` method
- Automatic data migration when adding `max_length` to existing field
- List indexing or slicing on the Redis side
- `max_length` enforcement on the non-capped (hash-embedded) list storage
- Query/filter support on list contents

## Update System

No update system changes required -- this is a library feature addition with no deployment or service implications.

## Agent Integration

No agent integration required -- this is a core ORM library feature.

## Documentation

### Feature Documentation
- [ ] Update `docs/fields.md` with ListField `max_length` and `push()` documentation
- [ ] Add usage examples showing both capped and uncapped ListField patterns

### External Documentation Site
- [ ] Update `docs/api-reference.md` with new ListField parameters and methods
- [ ] Verify docs build passes (`mkdocs serve`)

### Inline Documentation
- [ ] Docstrings on `push()`, updated `ListField.__init__`, `CappedListProxy`
- [ ] Code comments on the Redis list key pattern and encoding strategy

## Success Criteria

- [ ] `ListField(max_length=N)` stores data in a separate Redis list key
- [ ] `push(value)` appends to the list using LPUSH + LTRIM without reading the full list
- [ ] List is automatically capped at `max_length` items
- [ ] Reading a model with capped ListField returns the full list
- [ ] Deleting a model with capped ListField cleans up the Redis list key
- [ ] `ListField()` without `max_length` preserves existing behavior exactly
- [ ] Complex types (tuples, dicts, Decimals) round-trip through push/read correctly
- [ ] Tests pass (`pytest`)
- [ ] Linting passes (`ruff check .`)
- [ ] Documentation updated

## Team Orchestration

### Team Members

- **Builder (list-field)**
  - Name: list-field-builder
  - Role: Implement ListField max_length, push(), CappedListProxy, on_save/on_delete hooks
  - Agent Type: builder
  - Resume: true

- **Validator (list-field)**
  - Name: list-field-validator
  - Role: Verify all success criteria, test edge cases, validate existing behavior preserved
  - Agent Type: validator
  - Resume: true

- **Documentarian (list-field-docs)**
  - Name: list-field-docs
  - Role: Update fields.md, api-reference.md, inline docstrings
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Implement CappedListProxy and ListField max_length support
- **Task ID**: build-list-field
- **Depends On**: none
- **Assigned To**: list-field-builder
- **Agent Type**: builder
- **Parallel**: true
- Write failing tests for: ListField(max_length=N) creation, push() method, save/load round-trip, delete cleanup, existing ListField backward compatibility
- Implement CappedListProxy class with push() method
- Modify ListField to accept max_length parameter
- Add on_save() hook that writes to Redis list key when max_length is set
- Add on_delete() hook that deletes Redis list key when max_length is set
- Integrate read path to load from Redis list key for capped fields
- Ensure complex type encoding works for individual list elements
- Exclude capped list data from the hash serialization

### 2. Validate ListField implementation
- **Task ID**: validate-list-field
- **Depends On**: build-list-field
- **Assigned To**: list-field-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify all tests pass
- Verify existing ListField tests still pass (backward compatibility)
- Verify push() raises on unsaved model
- Verify Redis keys are cleaned up on delete
- Verify max_length cap is enforced after push
- Run linting and formatting checks

### 3. Documentation
- **Task ID**: document-list-field
- **Depends On**: validate-list-field
- **Assigned To**: list-field-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Update docs/fields.md with max_length and push() documentation
- Update docs/api-reference.md
- Add inline docstrings

### 4. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-list-field
- **Assigned To**: list-field-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify all success criteria met
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check .` | exit code 0 |
| Format clean | `python -m ruff format --check .` | exit code 0 |
| Docs build | `mkdocs build --strict 2>&1` | exit code 0 |
| ListField push test | `pytest tests/ -k "push" -v` | exit code 0 |
| Backward compat | `pytest tests/test_field_types.py -v` | exit code 0 |

---

## Open Questions

1. **Push ordering**: The issue says LPUSH (prepend). Should `push()` prepend (newest first) or append (oldest first)? LPUSH + LTRIM keeps newest items, which matches the "capped log" pattern. Confirm this is the desired behavior.

2. **Read path loading**: Loading capped list data requires an additional Redis call beyond the model's HGETALL. Two options:
   - **Eager**: Fetch list data during model load (extra LRANGE call, possibly pipelined). Simpler but adds latency for every model load.
   - **Lazy**: Fetch on first field access (like Relationship fields). More complex but no overhead if the field isn't accessed.
   Which approach is preferred?

3. **save() behavior with max_length**: When `model.save()` is called and the field has a Python list value, should it replace the entire Redis list? This means any items added via `push()` since the last load would be overwritten. Is this acceptable, or should `save()` be a no-op for capped list fields (only `push()` modifies them)?
