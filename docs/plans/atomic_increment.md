---
status: Planning
type: feature
appetite: Small
owner: Solo dev
created: 2026-03-12
tracking: https://github.com/tomcounsell/popoto/issues/179
last_comment_id:
---

# Atomic Increment for Numeric Fields

## Problem

Incrementing a numeric field on a Popoto model currently requires a read-modify-write cycle: fetch the model, change the value in Python, save back. This is not atomic -- concurrent increments can lose updates.

**Current behavior:**
```python
episode = Episode.query.get(id="abc")
episode.deviation_count += 1
episode.save()  # Another process may have incremented between our read and write
```

**Desired outcome:**
```python
new_val = episode.atomic_increment('deviation_count', 1)
# Atomically increments in Redis, returns new value, updates in-memory instance
```

## Prior Art

No prior issues or PRs found related to atomic increment functionality.

## Data Flow

1. **Entry point**: User calls `model_instance.atomic_increment(field_name, delta)` on a saved model instance
2. **Validation**: Method checks that the field exists, is a numeric type (int, float, Decimal), and the model has been saved (has a redis_key)
3. **Redis Lua script**: A Lua script executes atomically in Redis:
   - Reads the msgpack-encoded field value from the model's hash
   - Decodes the msgpack bytes to a number
   - Adds the delta
   - Re-encodes the result as msgpack bytes
   - Writes the new value back to the hash field
   - Returns the new numeric value
4. **In-memory update**: The method updates the in-memory attribute on the model instance and `_saved_field_values`
5. **Sorted index update**: If the field is a SortedField, the sorted set score is updated via ZADD
6. **Output**: Returns the new numeric value to the caller

## Architectural Impact

- **New dependencies**: None -- uses existing Redis Lua script support via redis-py
- **Interface changes**: Adds one new public method `atomic_increment()` to the Model class
- **Coupling**: No increase -- method is self-contained on the Model class
- **Data ownership**: No change -- the model still owns its data in Redis
- **Reversibility**: Easy to remove -- single method addition with no side effects on existing code

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

- **`Model.atomic_increment()`**: Instance method that atomically increments a numeric field value in Redis
- **Lua script**: Ensures atomicity since msgpack encoding prevents direct use of HINCRBY/HINCRBYFLOAT
- **Sorted index sync**: Updates the SortedField index score if the field is sorted

### Flow

**Model instance** -> `atomic_increment('field', delta)` -> **validate field type + saved state** -> **execute Lua script in Redis** -> **update in-memory value + sorted index** -> **return new value**

### Technical Approach

- **Lua script for atomicity**: Popoto stores field values as msgpack-encoded bytes in Redis hashes. Redis HINCRBY/HINCRBYFLOAT cannot operate on msgpack-encoded values directly. A Lua script will read, decode, increment, re-encode, and write back atomically within a single Redis operation.
- **msgpack in Lua**: For integers, msgpack uses simple encodings (single byte for small ints, 1-9 bytes for larger). The Lua script will use `cmsgpack.unpack()` and `cmsgpack.pack()` which are available in Redis's built-in Lua environment.
- **Type-aware delta handling**: IntField uses integer arithmetic; FloatField/DecimalField use float arithmetic. The delta type should match the field type.
- **SortedField integration**: If the incremented field is also a SortedField, update the sorted set score with ZINCRBY after the Lua script executes.
- **Pipeline support**: Accept an optional `pipeline` argument for batched operations, consistent with existing save/delete API patterns.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `atomic_increment` on an unsaved model (no redis_key) raises TypeError
- [ ] `atomic_increment` on a non-numeric field raises TypeError
- [ ] `atomic_increment` on a nonexistent field name raises AttributeError or similar

### Empty/Invalid Input Handling
- [ ] Delta of 0 is valid and returns current value
- [ ] Negative delta works correctly (decrement)
- [ ] None delta raises TypeError

### Error State Rendering
- No user-visible output -- API returns numeric values or raises exceptions

## Rabbit Holes

- **Supporting non-numeric types**: Don't try to make increment work for strings, lists, etc. Only int, float, Decimal.
- **Batch increment across multiple fields**: Keep the API to one field per call. Multi-field atomic increment is a separate concern.
- **Custom msgpack type handling in Lua**: Decimal is stored as a tagged dict `{"__Decimal__": True, "as_encodable": "1.5"}`. The Lua script should handle the standard int/float msgpack encoding, but for Decimal, the approach should decode and re-encode the tagged dict format. This adds complexity -- consider whether Decimal support is worth including in v1 or should be deferred.

## Risks

### Risk 1: cmsgpack availability in Redis Lua
**Impact:** If Redis's built-in Lua doesn't include cmsgpack, the Lua script approach won't work.
**Mitigation:** cmsgpack is available in Redis since version 2.6. Verify with a simple test. Fallback: use WATCH/MULTI optimistic locking instead of Lua.

### Risk 2: Decimal field msgpack encoding complexity
**Impact:** Decimal values use tagged-dict encoding (`{"__Decimal__": True, "as_encodable": "1.5"}`), which is more complex to handle in Lua.
**Mitigation:** For v1, handle int and float natively in Lua. For Decimal, decode the tagged dict, perform float arithmetic, and re-encode. Accept minor precision differences since Redis scores already use float.

## Race Conditions

No race conditions -- the Lua script executes atomically in Redis. The sorted index update (ZINCRBY) is also atomic. The only non-atomic step is the in-memory Python attribute update, which is acceptable since Python's GIL prevents concurrent access within a single process, and cross-process atomicity is guaranteed by Redis.

## No-Gos (Out of Scope)

- Atomic increment on Relationship fields or non-numeric fields
- Atomic increment on multiple fields in a single call
- Class-level increment (incrementing all instances matching a query)
- Async version (`async_atomic_increment`) -- can be added later following the existing async pattern

## Update System

No update system changes required -- this is a library feature addition.

## Agent Integration

No agent integration required -- this is a core ORM feature.

## Documentation

### Feature Documentation
- [ ] Add docstring to `atomic_increment()` method with examples
- [ ] Update MkDocs API reference if auto-generated

### Inline Documentation
- [ ] Code comments on the Lua script explaining msgpack handling
- [ ] Docstring covering supported field types, return value, and error cases

## Success Criteria

- [ ] `model.atomic_increment('int_field', 1)` atomically increments and returns new value
- [ ] `model.atomic_increment('float_field', 0.5)` works for float fields
- [ ] `model.atomic_increment('decimal_field', 1)` works for Decimal fields
- [ ] In-memory instance attribute is updated after increment
- [ ] SortedField index is updated if field is sorted
- [ ] TypeError raised for non-numeric fields
- [ ] TypeError raised on unsaved models (no redis_key)
- [ ] Concurrent increments do not lose updates
- [ ] Pipeline support works correctly
- [ ] Tests pass (`pytest`)
- [ ] Linting clean (`ruff check`)

## Team Orchestration

### Team Members

- **Builder (atomic-increment)**
  - Name: increment-builder
  - Role: Implement atomic_increment method, Lua script, and tests
  - Agent Type: builder
  - Resume: true

- **Validator (atomic-increment)**
  - Name: increment-validator
  - Role: Verify implementation meets all success criteria
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Implement atomic_increment method and tests
- **Task ID**: build-atomic-increment
- **Depends On**: none
- **Assigned To**: increment-builder
- **Agent Type**: builder
- **Parallel**: true
- Write failing tests for atomic_increment (TDD red phase):
  - Test integer increment on IntField
  - Test float increment on FloatField
  - Test Decimal increment on DecimalField
  - Test TypeError on non-numeric field (StringField)
  - Test TypeError on unsaved model
  - Test negative delta (decrement)
  - Test zero delta
  - Test in-memory value update after increment
  - Test SortedField index update after increment
  - Test concurrent increments don't lose updates
  - Test pipeline support
- Implement `atomic_increment()` method on Model class in `src/popoto/models/base.py`
- Implement Lua script for atomic msgpack read-increment-write
- Add SortedField index sync via ZINCRBY
- Run tests to green

### 2. Validate implementation
- **Task ID**: validate-atomic-increment
- **Depends On**: build-atomic-increment
- **Assigned To**: increment-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify all success criteria are met
- Run full test suite
- Verify linting passes
- Check that the Lua script handles edge cases

### 3. Final Validation
- **Task ID**: validate-all
- **Depends On**: validate-atomic-increment
- **Assigned To**: increment-validator
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
| Increment test exists | `pytest tests/ -k "atomic_increment" -v` | exit code 0 |

---

## Open Questions

1. **Decimal precision**: The Lua script operates on floats internally. For DecimalField, should we accept float precision loss in the Lua script, or should we handle the tagged-dict encoding (`{"__Decimal__": True, "as_encodable": "1.5"}`) as string arithmetic in Lua? The simpler approach (float conversion) may be acceptable since Redis sorted set scores already use float.

2. **Return type for DecimalField**: Should `atomic_increment` on a DecimalField return a `Decimal` object (matching the field type) or a `float` (matching what Redis returns)? Recommendation: return `Decimal` for consistency with the field's type contract, converting the float result.

3. **Async version**: Should `async_atomic_increment` be included in this scope or deferred? The existing codebase has async equivalents for save/delete. Recommendation: defer to a follow-up since the issue doesn't mention async.
