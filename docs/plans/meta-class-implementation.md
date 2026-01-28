# Model Meta Class Implementation Plan

## Issue Reference
GitHub Issue #27: Model class Meta

## Current Status: 2 of 3 Features Complete

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

### 🔄 In Progress: Meta.indexes (Peewee-style pattern)

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

**✅ Test Suite Created:**
- 11 comprehensive tests in `tests/test_meta_indexes.py`
- Structure validation tests: ✅ PASS
- Unique constraint tests: ❌ FAIL (not implemented yet)
- Update/delete tests: Not yet run

**⚠️ Partial Implementation:**
- Started `pre_save()` unique checking logic
- Approach complexity identified (see problems below)

---

## Open Problems & Design Questions

### Problem 1: Index Storage Strategy

**Question:** How to efficiently store and check unique indexes in Redis?

**Current approach (incomplete):**
```python
# Store index membership in Redis SET
$Index:Transaction:from_acct:to_acct  # SET containing instance keys

# Store hash separately per instance
Transaction:tx1:hash:from_acct:to_acct  # STRING containing hash value
```

**Issues with current approach:**
1. **Two lookups required:** Check SET membership + get hash for each existing instance
2. **Race conditions:** Between check and save, another instance could be created
3. **Cleanup complexity:** Must track and delete hash keys on delete
4. **NULL handling unclear:** How to handle NULL values in composite indexes?

**Alternative approaches to explore:**

**Option A: Single Redis SET with hash as member**
```
$Index:Transaction:from_acct:to_acct
SET members: ["hash1:tx1", "hash2:tx2", ...]
```
- Pro: Single lookup with `SISMEMBER`
- Pro: Atomic check-and-add with Lua script
- Con: Harder to find instance by hash (need to iterate)

**Option B: Redis HASH mapping hash→instance_key**
```
$Index:Transaction:from_acct:to_acct
HASH: {hash1: "tx1", hash2: "tx2", ...}
```
- Pro: Direct hash→key lookup with `HEXISTS`
- Pro: Easy cleanup with `HDEL`
- Con: Not using Redis SET (inconsistent with other indexes)

**Option C: Sorted Set with hash as score**
```
$Index:Transaction:from_acct:to_acct
ZSET: {member: instance_key, score: numeric_hash}
```
- Pro: Efficient lookups
- Pro: Could enable range queries later
- Con: Hash must be numeric (collision risk with truncated SHA)

**Research needed:**
- How does Django handle composite unique constraints with caching?
- How do other Redis ORMs (ROM, redisco) handle unique indexes?
- What does Peewee do for index enforcement (SQL vs ORM layer)?

---

### Problem 2: Update Semantics

**Question:** When updating indexed fields, what's the cleanup sequence?

**Scenario:**
```python
tx = Transaction.get(id="tx1")  # from_acct="A", to_acct="B"
tx.to_acct = "C"  # Change indexed field
tx.save()  # What happens?
```

**Required operations:**
1. Check new combination (A, C) doesn't exist
2. Remove old hash from index (A, B)
3. Add new hash to index (A, C)
4. Atomic: No other instance can claim (A, C) between steps 1-3

**Current issue:**
- `pre_save()` only checks, doesn't know about old values
- `save()` doesn't know which fields changed
- No "before/after" tracking

**Potential solutions:**

**Option A: Track dirty fields**
```python
# In Model.__setattr__
if field_name in self._meta.fields:
    self._dirty_fields.add(field_name)
```
- Pro: Peewee uses this pattern (`only_save_dirty` option)
- Con: Adds complexity to field access

**Option B: Store original values from load**
```python
# In decode_popoto_model_hashmap()
instance._original_values = {field: value, ...}
```
- Pro: Simple comparison in pre_save()
- Con: Memory overhead for every instance
- Note: Similar to `_saved_field_values` added for relationship fix

**Option C: Read-before-write in pre_save()**
```python
# In pre_save(), reload current values from Redis
if instance.exists():
    current = Model.query.get(...)
    # Compare and compute diff
```
- Pro: No additional tracking needed
- Con: Extra Redis query on every save
- Con: Potential race condition

**Research needed:**
- How does Django's `Model.save(update_fields=...)` work?
- How does Peewee handle `only_save_dirty`?

---

### Problem 3: NULL Handling in Unique Indexes

**Question:** Should multiple NULL values be allowed in unique indexes?

**Standard SQL behavior:**
```sql
-- Two rows with (NULL, 'B') are allowed
INSERT INTO transactions (from_acct, to_acct) VALUES (NULL, 'B');
INSERT INTO transactions (from_acct, to_acct) VALUES (NULL, 'B');  -- OK
```

**But our hash approach:**
```python
hash("NULL:B") == hash("NULL:B")  # Would conflict!
```

**Options:**

**Option A: Follow SQL - Allow multiple NULLs**
- Don't add to index if any field is NULL
- Pro: Standard behavior
- Con: Doesn't enforce uniqueness on partial data

**Option B: Treat NULL as a value**
- Hash includes NULL as string "NULL"
- Pro: Simple implementation
- Con: Non-standard behavior

**Option C: Configurable per index**
```python
indexes = (
    (('field1', 'field2'), True, {'null_unique': False}),  # Multiple NULLs OK
)
```
- Pro: Maximum flexibility
- Con: Adds complexity

**Research needed:**
- How does Peewee handle NULL in composite indexes?
- What's Django's behavior?
- What do users expect from Redis ORM?

---

### Problem 4: Index Lifecycle Management

**Question:** When to add/remove from index sets?

**Current hooks available:**
- `Model.pre_save()` - Validation before save
- `Model.save()` - Actual save to Redis
- `Model.delete()` - Deletion
- Field `on_save()` / `on_delete()` - Per-field hooks

**Where to put index management?**

**Option A: In Model.save() directly**
```python
def save(self):
    # ... existing save logic ...
    # Add to indexes
    for field_names, is_unique in self._meta.indexes:
        index_key = self._meta.get_index_key(field_names)
        hash_val = self._meta.compute_index_hash(self, field_names)
        POPOTO_REDIS_DB.sadd(index_key, f"{hash_val}:{self.db_key}")
```
- Pro: Centralized, easy to understand
- Con: Mixes concerns in already complex save()

**Option B: Create IndexField pseudo-field**
```python
class IndexManager:
    def on_save(self, model_instance, ...):
        # Manage all indexes
    def on_delete(self, model_instance, ...):
        # Clean up all indexes
```
- Pro: Consistent with how relationships work
- Pro: Keeps save() clean
- Con: Indexes aren't really fields

**Option C: Post-save hook**
```python
def save(self):
    # ... existing save logic ...
    self.post_save()

def post_save(self):
    # Handle indexes
    for field_names, is_unique in self._meta.indexes:
        # ...
```
- Pro: Clean separation
- Con: Another method to maintain

---

## Recommended Next Steps

### Phase 1: Research & Design (1-2 hours)

1. **Study other implementations:**
   - Django's `unique_together` enforcement
   - Peewee's index handling (SQL vs ORM layer)
   - Redis ORMs: ROM, redisco, walrus
   - Look for "composite key" or "compound index" patterns

2. **Design decisions needed:**
   - Choose Redis storage strategy (SET, HASH, or ZSET)
   - Decide on update semantics (track dirty vs read-before-write)
   - Define NULL handling behavior
   - Select implementation location (save() vs hooks)

3. **Create decision doc:**
   - Document chosen approach with rationale
   - Include Redis command examples
   - Show failure scenarios and how they're handled

### Phase 2: Core Implementation (3-4 hours)

1. **Implement chosen storage strategy**
2. **Complete pre_save() checking logic**
3. **Add index management to save()**
4. **Add index cleanup to delete()**
5. **Make all 11 tests pass**

### Phase 3: Edge Cases (1-2 hours)

1. **Handle NULL values correctly**
2. **Test update scenarios thoroughly**
3. **Add concurrency tests (simulate race conditions)**
4. **Document limitations**

### Phase 4: Polish (1 hour)

1. **Update docs/meta.md with indexes section**
2. **Add examples to documentation**
3. **Create PR**
4. **Update issue #27 as fully complete**

---

## Documentation Already Created

**✅ `docs/meta.md`:**
- Complete documentation for `order_by` and `ttl`
- Placeholder for `indexes` (marked as "Coming Soon")
- Usage examples, validation rules, best practices

**✅ Issue #27 Comment:**
- Progress update posted
- Shows 2/3 features complete
- Links to PRs and test files

**✅ Updated `docs/index.md`:**
- Added link to Meta options documentation

---

## Files Modified (Not Yet Committed)

**In branch `feature/meta-indexes`:**
- `docs/meta.md` - New comprehensive Meta documentation
- `docs/index.md` - Link to Meta docs
- `src/popoto/models/base.py` - Partial indexes implementation
- `tests/test_meta_indexes.py` - Full test suite (11 tests)

**Not committed:** Incomplete indexes implementation needs design decisions first.

---

## Key Insights from Peewee Research

1. **Meta inheritance:** Peewee fully supports inheriting Meta options from parent classes. Popoto has TODO comment but not implemented.

2. **Model options extensibility:** Peewee allows custom `ModelOptions` subclasses via `model_options_base`. Could be useful for advanced Redis routing.

3. **Index pattern superiority:** Peewee doesn't use `unique_together` - the `indexes` pattern with uniqueness flag is more flexible and consistent.

4. **No TTL in Peewee:** Popoto's TTL feature is an innovation that Peewee can't replicate since SQL databases don't have native key expiration.

5. **Validation timing:** Both ORMs validate Meta options at class definition time (metaclass), not runtime.

---

## Questions for Next Session

1. **Storage strategy:** Should we use Redis SET, HASH, or ZSET for index storage?
2. **Update tracking:** How to efficiently detect which indexed fields changed?
3. **NULL behavior:** Follow SQL standard or treat NULL as a value?
4. **Atomicity:** Do we need Lua scripts to prevent race conditions?
5. **Scope creep:** Should we also implement non-unique indexes, or only focus on unique constraints?

---

## Success Criteria

**When is indexes feature "done"?**

1. ✅ All 11 tests pass
2. ✅ Handles create, update, delete correctly
3. ✅ Proper error messages on violations
4. ✅ NULL values handled consistently
5. ✅ Documentation updated with examples
6. ✅ No race conditions in concurrent scenarios
7. ✅ Performance acceptable (minimal Redis calls)

---

## Estimated Remaining Effort

- **Research & Design:** 1-2 hours
- **Implementation:** 3-4 hours
- **Testing & Edge Cases:** 1-2 hours
- **Documentation & PR:** 1 hour

**Total:** 6-9 hours to complete indexes feature

---

## Related Work

- **PR #50:** Meta.order_by (ready for merge)
- **PR #49:** Relationship field lazy-loading docs (ready for merge)
- **PR #48:** Relationship field improvements (merged)
- **PR #47:** Multiple bug fixes (merged)

All Meta class work is unblocked - can proceed independently.
