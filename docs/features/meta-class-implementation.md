# Meta Class Implementation - Completion Summary

**GitHub Issue:** #27
**Status:** ✅ Complete (3 of 3 features)
**Merged PRs:** #50, #51, #52

---

## Overview

Implemented Django/Peewee-style Meta class for model configuration. All three planned features are complete and merged to main.

---

## Completed Features

### 1. Meta.order_by (PR #50)

Default query result ordering.

**API:**
```python
class Product(Model):
    name = KeyField()
    price = SortedField()

    class Meta:
        order_by = "-price"  # Descending by default

# All queries ordered by price descending
products = Product.query.all()
```

**Implementation:**
- `ModelOptions.order_by` attribute
- Validates field exists at class definition time
- Applied in `Query.all()`, `Query.filter()`, `Query.prepare_results()`
- Can be overridden per-query with explicit `order_by` parameter

**Files:**
- `src/popoto/models/base.py`
- `src/popoto/models/query.py`
- `tests/test_meta_order_by.py` (7 tests)
- `docs/meta.md`

---

### 2. Meta.ttl (PR #51)

Time-To-Live (automatic expiration) for Redis keys.

**API:**
```python
class CachedData(Model):
    key = KeyField()
    value = Field()

    class Meta:
        ttl = 3600  # Expires after 1 hour

# Instance automatically expires
data = CachedData.create(key="session", value="data")
```

**Features:**
- Model-level default via `Meta.ttl`
- Instance-level override via `_ttl` attribute
- Absolute timestamp via `_expire_at` attribute
- Redis `EXPIRE`/`EXPIREAT` called on save

**Implementation:**
- `ModelOptions.ttl` attribute with validation (positive integer)
- `Model.__init__` sets default `_ttl` from Meta
- `Model.save()` applies TTL to Redis
- TTL refreshed on every save

**Files:**
- `src/popoto/models/base.py`
- `tests/test_meta_ttl.py` (7 tests)
- `docs/meta.md`

**Innovation:**
This is Popoto-specific. SQL-based ORMs like Peewee can't support this because SQL databases don't have native key expiration.

---

### 3. Meta.indexes (PR #52)

Composite unique indexes (Peewee pattern).

**API:**
```python
class Transaction(Model):
    transaction_id = KeyField()
    from_account = Field()
    to_account = Field()
    amount = Field()

    class Meta:
        indexes = (
            # (field_names_tuple, is_unique_boolean)
            (('from_account', 'to_account'), True),   # Unique composite
            (('to_account',), False),                  # Non-unique index
        )

# Duplicate detection on save
tx1 = Transaction.create(transaction_id="tx1", from_account="A", to_account="B", amount=100)
tx2 = Transaction.create(transaction_id="tx2", from_account="A", to_account="B", amount=200)
# ❌ ModelException: Unique index violation
```

**Features:**
- Enforces unique constraints on field combinations
- Supports both unique and non-unique indexes
- NULL handling: multiple NULLs allowed (SQL standard)
- Automatic index maintenance (create, update, delete)

**Implementation:**
- Redis HASH storage: `$Index:ClassName:field1:field2`
- SHA256 hash of field values as key
- `ModelOptions.compute_index_hash()` - generates hash (returns None for NULL)
- `Model.pre_save()` - validates unique constraints
- `Model.save()` - adds/updates index entries
- `Model.delete()` - removes index entries
- Uses `_saved_field_values` for proper cleanup on updates

**Files:**
- `src/popoto/models/base.py`
- `tests/test_meta_indexes.py` (10 tests)
- `docs/meta.md`

---

## Documentation

**`docs/meta.md`** - Complete reference documentation:
- All three Meta options documented with examples
- Validation rules and best practices
- Implementation details and Redis patterns

---

## Key Design Decisions

### 1. Index Storage: Redis HASH

Used Redis HASH instead of SET for O(1) lookups:
```
$Index:Transaction:from_account:to_account
HASH: {hash_of_values: instance_db_key}
```

### 2. Update Semantics: _saved_field_values

Reused existing `_saved_field_values` pattern for tracking old values:
- On save: remove old index entry, add new entry
- On delete: use saved values for cleanup

### 3. NULL Handling: SQL Standard

If any indexed field is NULL, skip index entry. Allows multiple NULLs in unique indexes.

### 4. Lifecycle: Direct in Model methods

Index management lives in `Model.pre_save()`, `Model.save()`, `Model.delete()` rather than field-based hooks.

---

## Testing

- **Meta.order_by:** 7 tests (all passing)
- **Meta.ttl:** 7 tests (all passing)
- **Meta.indexes:** 10 tests (all passing)
- **Total:** 24 tests for Meta features

---

## Future Considerations

1. **Meta inheritance:** Peewee supports inheriting Meta options from parent classes. Not implemented in Popoto.

2. **Custom ModelOptions:** Peewee allows custom `ModelOptions` subclasses via `model_options_base`. Could enable advanced Redis routing.

3. **Additional options:**
   - `abstract` - already exists
   - `database` - for multi-Redis routing
   - `table_name` equivalent - custom Redis key prefix

---

## Lessons from Peewee Research

1. **Indexes over unique_together:** Peewee's `indexes` pattern is more flexible than Django's `unique_together`
2. **Meta validation timing:** Both ORMs validate at class definition time (metaclass)
3. **TTL as innovation:** Popoto's TTL feature showcases Redis-native advantages over SQL ORMs

---

## Files Modified

**Source:**
- `src/popoto/models/base.py` - ModelOptions, metaclass, Model.save/delete/pre_save
- `src/popoto/models/query.py` - Query.all/filter with order_by

**Tests:**
- `tests/test_meta_order_by.py`
- `tests/test_meta_ttl.py`
- `tests/test_meta_indexes.py`

**Documentation:**
- `docs/meta.md` - Complete Meta reference

---

**Completed:** January 2026
**Implementation time:** ~3 days across 3 PRs
