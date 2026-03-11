# Plan: Fix class set cleanup for obsolete_redis_key in save()

**Issue**: #157
**Slug**: `obsolete-key-class-set`

## Problem

When a KeyField value changes during `save()`, the Redis key changes (e.g., `Job:init:uuid` -> `Job:running:uuid`). The save() method correctly:
1. Adds the new key to the class set via `pipeline.sadd()` (line 1140-1142 / 1206-1208)
2. Calls `field.on_delete()` for all fields with old values (line 1147-1159 / 1214-1226)
3. Deletes the old Redis hash key via `pipeline.delete()` (line 1160 / 1227)

But it does NOT remove the old key from the class set (`SREM`). This leaves orphaned entries that inflate `Model.query.count()` and survive even after `delete()` (which only removes the current key).

## Root Cause

Missing `pipeline.srem(self._meta.db_class_set_key.redis_key, self.obsolete_redis_key)` in both save() code paths.

Compare with `delete()` (line 1452-1454) which correctly does:
```python
pipeline = pipeline.srem(
    self._meta.db_class_set_key.redis_key, delete_redis_key
)  # 2
```

## Fix

### File: `src/popoto/models/base.py`

**Change 1 — External pipeline path (line ~1160)**

Current code:
```python
                pipeline.delete(self.obsolete_redis_key)  # 4
                self.obsolete_redis_key = None
```

Add `srem` before the `delete`:
```python
                pipeline = pipeline.srem(
                    self._meta.db_class_set_key.redis_key,
                    self.obsolete_redis_key,
                )  # 4a - remove old key from class set
                pipeline.delete(self.obsolete_redis_key)  # 4b
                self.obsolete_redis_key = None
```

**Change 2 — Internal pipeline path (line ~1227)**

Current code:
```python
                internal_pipeline.delete(self.obsolete_redis_key)  # 4
                self.obsolete_redis_key = None
```

Add `srem` before the `delete`:
```python
                internal_pipeline.srem(
                    self._meta.db_class_set_key.redis_key,
                    self.obsolete_redis_key,
                )  # 4a - remove old key from class set
                internal_pipeline.delete(self.obsolete_redis_key)  # 4b
                self.obsolete_redis_key = None
```

Note: The external pipeline path chains `pipeline = pipeline.srem(...)` (to preserve the pipeline return value pattern used throughout that branch). The internal pipeline path calls `internal_pipeline.srem(...)` without assignment (matching that branch's convention).

### File: `tests/test_field_index_edge_cases.py`

**Change 3 — Convert xfail to real assertion (line ~722-731)**

Replace the conditional `pytest.xfail(...)` block with a hard assertion:
```python
        # Class set should be clean after create-mutate-delete cycle
        count = EdgeLifecycle.query.count()
        assert count == 0, (
            f"Class set should be empty after delete, got count={count}"
        )
```

This test (Test Case 11 `test_rapid_lifecycle_leaves_no_orphans`) was written specifically to document this bug and will now serve as the regression test.

## No-Gos

- Do NOT change `delete()` — it already handles class set removal correctly
- Do NOT change field `on_delete()` / `on_save()` — the class set is a model-level concern, not a field concern
- Do NOT add new test files — the existing xfail test covers the exact scenario

## Verification

1. `test_rapid_lifecycle_leaves_no_orphans` passes without xfail
2. All existing tests still pass (especially `test_key_fields.py`, `test_field_index_edge_cases.py`)
3. Manual verification: create → mutate KeyField → save → check `SMEMBERS` on class set → only new key present
