# Model Meta Class Implementation Plan

## Issue Reference
GitHub Issue #27: Model class Meta

## Current Status: 3 of 3 Features Complete

### ✅ Completed Features

#### 1. Meta.order_by (PR #50)
**Status:** Fully implemented, tested, awaiting merge

**What it does:**
- Defines default ordering for all query results
- Supports ascending (`"price"`) and descending (`"-price"`)
- Works with `all()`, `filter()`, `limit`
- Can be overridden per-query with explicit `order_by` parameter

**Implementation:**
- `ModelOptions.order_by` attribute stores the field name
- Parsed in `ModelBase.__new__` with validation
- Applied in `Query.all()`, `Query.filter()`, and `Query.prepare_results()`
- 7 comprehensive tests pass

**Files modified:**
- `src/popoto/models/base.py` - ModelOptions and metaclass
- `src/popoto/models/query.py` - Query methods
- `tests/test_meta_order_by.py` - Test suite

---

#### 2. Meta.ttl (Already in main branch)
**Status:** Fully implemented, tested, in production

**What it does:**
- Sets Time-To-Live (TTL) for Redis keys in seconds
- Models automatically expire after specified duration
- Instance-level override with `_ttl` attribute
- Alternative `_expire_at` for absolute timestamp expiration

**Implementation:**
- `ModelOptions.ttl` attribute stores default TTL
- Parsed in `ModelBase.__new__` with validation (must be positive integer)
- Applied in `Model.__init__` as default `_ttl` value
- Redis `EXPIRE`/`EXPIREAT` called in `Model.save()`
- TTL refreshed on every save, not just create
- 7 comprehensive tests pass

**Files:**
- `src/popoto/models/base.py` - Already has full implementation
- `tests/test_meta_ttl.py` - Comprehensive test suite

**Popoto Innovation:**
This is unique to Popoto - Peewee (SQL-based) doesn't have TTL since SQL databases don't natively support key expiration. Showcases Redis-native advantages.

---

### ✅ Completed: Meta.indexes (Peewee-style pattern)

**Target API:**
```python
class Transaction(Model):
    from_acct = KeyField()
    to_acct = Field()
    amount = Field()

    class Meta:
        indexes = (
            # (field_names_tuple, is_unique_boolean)
            (('from_acct', 'to_acct'), True),   # Unique composite
            (('to_acct',), False),               # Non-unique single
        )
```

**Why indexes instead of unique_together:**
- More flexible: handles both unique AND non-unique indexes
- Follows Peewee's proven pattern
- Single consistent API instead of multiple options

#### What's Already Done (60% complete)

**✅ Infrastructure:**
1. `ModelOptions.indexes` attribute added
2. Metaclass parsing in `ModelBase.__new__`
3. Comprehensive validation:
   - Must be tuple/list
   - Each index must be 2-tuple `(field_names, is_unique)`
   - Field names must be tuple/list
   - `is_unique` must be boolean
   - All field names must exist
4. Helper methods added to ModelOptions:
   - `get_index_key(field_names)` - Generate Redis key for index
   - `compute_index_hash(model_instance, field_names)` - Hash field values

**✅ Test Suite:**
- 10 comprehensive tests in `tests/test_meta_indexes.py`
- All tests pass

**✅ Implementation Complete:**
- Storage: Redis HASH at `$Index:ClassName:field1:field2`
- Entries: `{sha256_hash_of_values: instance_db_key}`
- NULL handling: Multiple NULLs allowed (SQL standard)
- Update handling: Old hash removed, new hash added
- Delete handling: Hash entry removed from index

---

## Resolved Design Decisions

### Problem 1: Index Storage Strategy ✅ RESOLVED

**Chosen Solution: Option B - Redis HASH**

```
$Index:Transaction:from_account:to_account
HASH: {hash1: "Transaction:tx1", hash2: "Transaction:tx2", ...}
```

**Why this approach:**
- Direct hash→key lookup with `HEXISTS`/`HGET` - O(1)
- Easy cleanup with `HDEL`
- Simple to check if value exists AND verify it's not our own key
- No iteration needed

**Implementation:**
- `ModelOptions.get_index_key(field_names)` - generates Redis key
- `ModelOptions.compute_index_hash(instance, field_names)` - SHA256 of values (16 chars)
- `ModelOptions.compute_index_hash_from_values(field_names, values_dict)` - for cleanup of old values

---

### Problem 2: Update Semantics ✅ RESOLVED

**Chosen Solution: Use existing `_saved_field_values`**

The `_saved_field_values` dict was already added for relationship field cleanup. Reused it for index cleanup.

**Implementation in `save()`:**
```python
# Manage indexes
for field_names, is_unique in self._meta.indexes:
    field_names_tuple = tuple(field_names)
    index_key = self._meta.get_index_key(field_names_tuple)
    # Remove old index entry if indexed fields changed
    if self._saved_field_values:
        old_hash = self._meta.compute_index_hash_from_values(
            field_names_tuple, self._saved_field_values
        )
        if old_hash:
            POPOTO_REDIS_DB.hdel(index_key, old_hash)
    # Add new index entry
    new_hash = self._meta.compute_index_hash(self, field_names_tuple)
    if new_hash:
        POPOTO_REDIS_DB.hset(index_key, new_hash, new_db_key.redis_key)
```

**Why this approach:**
- No additional infrastructure needed - reuses existing pattern
- Works correctly for both create (no old values) and update (has old values)
- Old hash is always removed before new hash is added

---

### Problem 3: NULL Handling in Unique Indexes ✅ RESOLVED

**Chosen Solution: Option A - Follow SQL Standard**

If any indexed field is NULL, don't add to the index. This allows multiple rows with NULL in the same indexed fields.

**Implementation in `compute_index_hash()`:**
```python
def compute_index_hash(self, model_instance, field_names: tuple) -> str:
    """Returns None if any field value is None (NULL handling)."""
    values = []
    for field_name in field_names:
        value = getattr(model_instance, field_name, None)
        if value is None:
            return None  # Don't index NULL values
        values.append(str(value))
    combined = ":".join(values)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]
```

**Why this approach:**
- Follows SQL standard behavior (most predictable for developers)
- Simple implementation (just return None for NULL values)
- In `pre_save()` and `save()`, we skip index operations when hash is None

---

### Problem 4: Index Lifecycle Management ✅ RESOLVED

**Chosen Solution: Option A - In Model methods directly**

Index management is added to:
1. `Model.pre_save()` - Check for unique violations
2. `Model.save()` - Add/update index entries (after field on_save calls)
3. `Model.delete()` - Remove index entries (after field on_delete calls)

**Why this approach:**
- Keeps all index logic together in the Model class
- No need for new pseudo-field abstraction
- Consistent with existing save/delete flow
- Works with both pipeline and non-pipeline modes

---

## Completed Implementation Summary

### What Was Implemented

1. **Redis HASH storage** for indexes at `$Index:ClassName:field1:field2`
2. **pre_save() uniqueness checking** - validates against existing entries
3. **save() index management** - removes old entries, adds new entries
4. **delete() index cleanup** - removes entries when instance deleted
5. **NULL handling** - multiple NULLs allowed (SQL standard behavior)
6. **Update handling** - uses `_saved_field_values` for old value cleanup

### Files Modified

- `src/popoto/models/base.py`:
  - `ModelOptions.compute_index_hash()` - returns None for NULL values
  - `ModelOptions.compute_index_hash_from_values()` - for cleanup of old values
  - `Model.pre_save()` - unique constraint checking
  - `Model.save()` - index entry add/update (both pipeline and non-pipeline)
  - `Model.delete()` - index entry cleanup

- `tests/test_meta_indexes.py`:
  - 10 comprehensive tests
  - All tests pass

- `docs/meta.md`:
  - Full documentation for indexes feature

---

## Documentation

**✅ `docs/meta.md`:**
- Complete documentation for `order_by`, `ttl`, and `indexes`
- Usage examples, validation rules, best practices
- All three Meta features fully documented

---

## Files Modified

**In branch `feature/meta-indexes`:**
- `docs/meta.md` - Complete Meta documentation (order_by, ttl, indexes)
- `src/popoto/models/base.py` - Full indexes implementation
- `tests/test_meta_indexes.py` - Full test suite (10 tests, all passing)

---

## Key Insights from Peewee Research

1. **Meta inheritance:** Peewee fully supports inheriting Meta options from parent classes. Popoto has TODO comment but not implemented.

2. **Model options extensibility:** Peewee allows custom `ModelOptions` subclasses via `model_options_base`. Could be useful for advanced Redis routing.

3. **Index pattern superiority:** Peewee doesn't use `unique_together` - the `indexes` pattern with uniqueness flag is more flexible and consistent.

4. **No TTL in Peewee:** Popoto's TTL feature is an innovation that Peewee can't replicate since SQL databases don't have native key expiration.

5. **Validation timing:** Both ORMs validate Meta options at class definition time (metaclass), not runtime.

---

## Success Criteria - All Met ✅

1. ✅ All 10 tests pass
2. ✅ Handles create, update, delete correctly
3. ✅ Proper error messages on violations
4. ✅ NULL values handled consistently (SQL standard)
5. ✅ Documentation updated with examples
6. ✅ Performance acceptable (O(1) Redis operations)

---

## Related Work

- **PR #50:** Meta.order_by (ready for merge)
- **PR #49:** Relationship field lazy-loading docs (ready for merge)
- **PR #48:** Relationship field improvements (merged)
- **PR #47:** Multiple bug fixes (merged)

**This branch:** Meta.indexes implementation complete, ready for review.
