# Plan: Query Performance Improvements

**Status**: COMPLETE
**Created**: 2026-02-03
**Completed**: 2026-02-03
**Issue**: #84 (tracking), #77, #78

## Overview

Address the three main performance bottlenecks identified in the query performance audit (PR #79):

1. **KEYS command blocks Redis** — Replace with SCAN
2. **Deserialization is 60% of bulk query time** — Optimize encoding
3. **`__in` scales linearly** — Batch optimization

## Phase 1: Replace KEYS with SCAN (#77)

**Priority**: Critical
**Risk**: KEYS blocks entire Redis server at scale

### Current State

`KeyFieldMixin.filter_query()` uses Redis `KEYS` command for pattern matching:

```python
# key_field_mixin.py lines 168, 185, 194, 198
POPOTO_REDIS_DB.keys(f"{Model._get_db_key()}*")
```

### Implementation

#### 1.1 Add SCAN utility function

Create `src/popoto/redis_db.py` helper:

```python
def scan_keys(pattern: str, count: int = 1000) -> list[bytes]:
    """Non-blocking replacement for KEYS using cursor-based SCAN."""
    results = []
    cursor = 0
    while True:
        cursor, keys = POPOTO_REDIS_DB.scan(cursor=cursor, match=pattern, count=count)
        results.extend(keys)
        if cursor == 0:
            break
    return results
```

#### 1.2 Replace KEYS calls in key_field_mixin.py

Update four locations:
- Line 168: Auto field exact match
- Line 185: `__isnull=False`
- Line 194: `__startswith`
- Line 198: `__endswith`

Replace:
```python
POPOTO_REDIS_DB.keys(pattern)
```

With:
```python
scan_keys(pattern)
```

#### 1.3 Leave debug KEYS calls in query.py

Lines 98 and 110 are debug-only (`keys(clean=True)` and `keys(catchall=True)`). These are acceptable for development use.

### Verification

```bash
pytest tests/test_queries.py -v -k "startswith or endswith or isnull"
pytest tests/profile_queries.py -v -s  # Compare timings
```

## Phase 2: Optimize Deserialization (#78)

**Priority**: High
**Impact**: 60% of bulk query time

### Current State

`decode_popoto_model_hashmap()` in `encoding.py` calls `msgpack.unpackb()` per field:

```python
for field_name, value_bytes in hashmap.items():
    value = msgpack.unpackb(value_bytes)  # Called N times per model
    setattr(instance, field_name, value)
```

### Implementation: Lazy Deserialization (Option B)

Don't decode fields until accessed. This avoids storage migration and provides immediate benefit for partial-field queries.

```python
class LazyModel:
    _raw_data: dict[str, bytes]
    _decoded: dict[str, Any]

    def __getattr__(self, name):
        if name not in self._decoded:
            self._decoded[name] = msgpack.unpackb(self._raw_data[name])
        return self._decoded[name]
```

Benefits:
- No change to storage format
- Big win when only accessing few fields
- Backward compatible with existing data

---

<details>
<summary>Other options considered</summary>

#### Option A: Single-blob encoding

Store entire model as one msgpack blob instead of per-field hashes.

Pros:
- One `unpackb()` call per model vs N calls
- Simpler Redis key structure

Cons:
- Can't use HMGET for partial field fetches
- Migration needed for existing data

#### Option B: Lazy deserialization — CHOSEN

See implementation above.

#### Option C: `__slots__` optimization

Add `__slots__` to Model class:

```python
class Model(metaclass=ModelBase):
    __slots__ = ('_field_values', '_key_values', ...)
```

Pros:
- Faster attribute access
- Lower memory per instance

Cons:
- Smallest impact of the three
- May conflict with dynamic field system

</details>

### Verification

```bash
pytest tests/profile_queries.py -v -s -k "all_query or deserialization"
```

## Phase 3: `__in` Query Optimization

**Priority**: Medium
**Current behavior**: O(N) pipeline of SMEMBERS

### Implementation

Batch `__in` queries using SUNION when querying against sorted set indexes:

```python
# Current: N separate SMEMBERS
for value in values:
    keys = POPOTO_REDIS_DB.smembers(f"{prefix}:{value}")
    results.extend(keys)

# Optimized: single SUNION
keys_to_union = [f"{prefix}:{v}" for v in values]
results = POPOTO_REDIS_DB.sunion(keys_to_union)
```

### Verification

```bash
pytest tests/profile_queries.py -v -s -k "in_query"
```

## Phase 4: Documentation

Update `docs/query.md` with performance best practices:

1. Use `count()` instead of `len(all())` — 186x faster
2. Use `values=('field1', 'field2')` for partial fetches — 2-4x faster
3. Prefer sorted field range queries over pattern filters
4. Avoid `__startswith`/`__endswith` on large datasets

## Files to Modify

### Phase 1
- **Edit**: `src/popoto/redis_db.py` — add `scan_keys()` utility
- **Edit**: `src/popoto/fields/key_field_mixin.py` — replace KEYS with scan_keys

### Phase 2
- **Edit**: `src/popoto/models/encoding.py` — lazy deserialization
- **Edit**: `src/popoto/models/base.py` — LazyModel support

### Phase 3
- **Edit**: `src/popoto/fields/key_field_mixin.py` — SUNION for `__in`

### Phase 4
- **Edit**: `docs/query.md` — performance best practices section

## Verification Plan

```bash
# Run full test suite
pytest

# Run profiling before/after each phase
pytest tests/profile_queries.py -v -s

# Verify no regressions
pytest tests/test_queries.py tests/test_stress.py -v
```

## Success Metrics

| Metric | Before | Target |
|--------|--------|--------|
| KEYS blocking | Yes | No (SCAN) |
| `all()` 1000 items | 10.2ms | <5ms |
| `filter(__startswith)` | 1.3ms blocking | 1.4ms non-blocking |
| `__in` 100 values | O(100) | O(1) SUNION |
