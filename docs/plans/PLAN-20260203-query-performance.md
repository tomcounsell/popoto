# Plan: Query Performance Improvements

**Status**: COMPLETE
**Created**: 2026-02-03
**Completed**: 2026-02-03
**Issue**: #84 (tracking), #77, #78

## Problem

Query performance audit (PR #79) identified three bottlenecks causing production-scale issues:

1. **KEYS command blocks Redis** — The `KEYS` command is O(N) and blocks the entire Redis server while scanning. At scale (>100k keys), this causes request timeouts and cascading failures.

2. **Deserialization is 60% of bulk query time** — Each model field is individually decoded via `msgpack.unpackb()`, creating N function calls per model. Bulk queries amplify this overhead.

3. **`__in` scales linearly** — Each value in an `__in` query generates a separate `SMEMBERS` call, making `filter(status__in=["a","b","c"])` O(N) instead of O(1).

## Appetite

**Medium (3-5 days)** | 1 developer

Phase 1 is critical and must ship first. Phases 2-3 are optimizations that can be deferred.

## Solution

### Phase 1: Replace KEYS with SCAN (Critical)

Add `scan_keys()` utility to `redis_db.py` using cursor-based iteration:

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

Replace KEYS calls in `key_field_mixin.py` (lines 168, 185, 194, 198).

### Phase 2: Lazy Deserialization (High)

Don't decode fields until accessed:

```python
class LazyModel:
    _raw_data: dict[str, bytes]
    _decoded: dict[str, Any]

    def __getattr__(self, name):
        if name not in self._decoded:
            self._decoded[name] = msgpack.unpackb(self._raw_data[name])
        return self._decoded[name]
```

### Phase 3: `__in` Query Optimization (Medium)

Batch `__in` queries using SUNION:

```python
# Current: N separate SMEMBERS
for value in values:
    keys = POPOTO_REDIS_DB.smembers(f"{prefix}:{value}")

# Optimized: single SUNION
keys_to_union = [f"{prefix}:{v}" for v in values]
results = POPOTO_REDIS_DB.sunion(keys_to_union)
```

## Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| SCAN returns duplicates during rehashing | Low | Dedupe results with set() |
| Lazy loading breaks existing code | Medium | Maintain full backward compat, decode on any attribute access |
| SUNION on large sets causes memory spike | Low | Only use for reasonable-sized `__in` lists (<1000 values) |

## Team Orchestration

### Team Members

- **Builder (scan)**
  - Name: scan-implementer
  - Role: Replace KEYS with SCAN across key_field_mixin
  - Agent Type: builder
  - Resume: true

- **Builder (lazy)**
  - Name: lazy-decoder
  - Role: Implement lazy deserialization in encoding.py
  - Agent Type: builder
  - Resume: true

- **Validator (perf)**
  - Name: perf-validator
  - Role: Run benchmarks to verify improvements
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Implement scan_keys utility
- **Task ID**: implement-scan
- **Depends On**: none
- **Assigned To**: scan-implementer
- **Agent Type**: builder
- **Parallel**: true
- Add `scan_keys()` function to `src/popoto/redis_db.py`
- Replace KEYS calls in `key_field_mixin.py` (4 locations)
- Run `pytest tests/test_queries.py -v -k "startswith or endswith or isnull"`

### 2. Add lazy deserialization
- **Task ID**: lazy-decode
- **Depends On**: none
- **Assigned To**: lazy-decoder
- **Agent Type**: builder
- **Parallel**: true
- Update `decode_popoto_model_hashmap()` in `encoding.py`
- Add lazy field wrapper in `base.py`
- Run `pytest tests/test_queries.py tests/test_stress.py`

### 3. Optimize __in queries
- **Task ID**: optimize-in
- **Depends On**: implement-scan
- **Assigned To**: scan-implementer
- **Agent Type**: builder
- **Parallel**: false
- Add SUNION optimization to `key_field_mixin.py`
- Run `pytest tests/test_queries.py -k "in"`

### 4. Validate performance improvements
- **Task ID**: validate-perf
- **Depends On**: implement-scan, lazy-decode, optimize-in
- **Assigned To**: perf-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/profile_queries.py -v -s`
- Compare before/after metrics
- Verify no test regressions

## Success Criteria

- [x] `scan_keys()` function added to redis_db.py
- [x] KEYS command replaced with SCAN in key_field_mixin.py
- [x] All existing query tests pass (139 passed)
- [x] `__startswith`/`__endswith` filters non-blocking
- [x] Lazy deserialization implemented (encoding.py, base.py)
- [x] `__in` queries use SUNION for O(1) performance

## Files Modified

- `src/popoto/redis_db.py` — add `scan_keys()` utility
- `src/popoto/fields/key_field_mixin.py` — replace KEYS, optimize `__in`
- `src/popoto/models/encoding.py` — lazy deserialization
- `src/popoto/models/base.py` — LazyModel support
- `docs/query.md` — performance best practices section
