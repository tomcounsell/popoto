# Plan: Make save() Atomic via Internal Pipeline

**Issue:** https://github.com/tomcounsell/popoto/issues/147
**Branch:** `session/atomic-save`

## Problem

`save()` without an explicit pipeline executes 5+ separate Redis commands sequentially. Between HSET (data write) and KeyField `on_save()` (index update), there's a window where the record exists but is invisible to `query.filter()`. This causes a race condition where `async_create()` followed by `async_filter()` misses newly created records.

## Solution

Follow the pattern already used by `delete()` (line 1442-1483): when no pipeline is provided, create an internal pipeline, queue all commands, and execute atomically.

## Changes

### File: `src/popoto/models/base.py`

#### 1. Full save path (lines 1193-1260)

Replace the `else` branch with internal pipeline usage:

```python
else:
    # Create internal pipeline for atomic execution
    internal_pipeline = POPOTO_REDIS_DB.pipeline()

    internal_pipeline.hset(new_db_key.redis_key, mapping=hset_mapping)  # 1

    if self._ttl is not None:
        internal_pipeline.expire(new_db_key.redis_key, self._ttl)  # 2
    elif self._expire_at is not None:
        internal_pipeline.expireat(
            new_db_key.redis_key, int(self._expire_at.timestamp())
        )  # 2

    internal_pipeline.sadd(
        self._meta.db_class_set_key.redis_key, new_db_key.redis_key
    )  # 3

    if (
        self.obsolete_redis_key
        and self.obsolete_redis_key != new_db_key.redis_key
    ):  # 4
        for field_name, field in self._meta.fields.items():
            field_value = self._saved_field_values.get(
                field_name, getattr(self, field_name)
            )
            field.on_delete(
                model_instance=self,
                field_name=field_name,
                field_value=field_value,
                pipeline=internal_pipeline,
                saved_redis_key=self.obsolete_redis_key,
                **kwargs,
            )
        internal_pipeline.delete(self.obsolete_redis_key)  # 4
        self.obsolete_redis_key = None

    for field_name, field in self._meta.fields.items():  # 5
        field.on_save(
            self,
            field_name=field_name,
            field_value=getattr(self, field_name),
            ignore_errors=ignore_errors,
            pipeline=internal_pipeline,
            **kwargs,
        )

    # Manage indexes  # 6
    for field_names, is_unique in self._meta.indexes:
        field_names_tuple = tuple(field_names)
        index_key = self._meta.get_index_key(field_names_tuple)
        if self._saved_field_values:
            old_hash = self._meta.compute_index_hash_from_values(
                field_names_tuple, self._saved_field_values
            )
            if old_hash:
                internal_pipeline.hdel(index_key, old_hash)
        new_hash = self._meta.compute_index_hash(self, field_names_tuple)
        if new_hash:
            internal_pipeline.hset(index_key, new_hash, new_db_key.redis_key)

    results = internal_pipeline.execute()
    db_response = results[0]  # HSET result (preserves backward compat)

    self._redis_key = new_db_key.redis_key  # 7
    self._saved_field_values = {  # 8
        field_name: getattr(self, field_name)
        for field_name in self._meta.fields.keys()
    }
    return db_response
```

#### 2. Partial save path (lines 1083-1109)

Same treatment for the `update_fields` non-pipeline branch:

```python
else:
    internal_pipeline = POPOTO_REDIS_DB.pipeline()
    internal_pipeline.hset(new_db_key.redis_key, mapping=hset_mapping)

    for field_name in update_fields:
        field = self._meta.fields[field_name]
        field.on_save(
            self,
            field_name=field_name,
            field_value=getattr(self, field_name),
            ignore_errors=ignore_errors,
            pipeline=internal_pipeline,
            **kwargs,
        )

    if self._ttl is not None:
        internal_pipeline.expire(new_db_key.redis_key, self._ttl)
    elif self._expire_at is not None:
        internal_pipeline.expireat(
            new_db_key.redis_key, int(self._expire_at.timestamp())
        )

    results = internal_pipeline.execute()
    db_response = results[0]

    self._redis_key = new_db_key.redis_key
    for field_name in update_fields:
        self._saved_field_values[field_name] = getattr(self, field_name)
    return db_response
```

### File: `tests/test_atomic_save.py`

New test file covering:

1. **test_save_atomic_all_or_nothing** — verify HSET + class set + KeyField index all exist after save (no partial state)
2. **test_save_filter_immediately_after_create** — create then filter by KeyField, record must be found
3. **test_async_create_then_async_filter** — async version of test 2
4. **test_save_return_value_backward_compat** — verify save() still returns int (HSET result)
5. **test_save_with_sorted_field_atomic** — SortedField index exists immediately after save
6. **test_partial_save_atomic** — update_fields path is also atomic
7. **test_save_with_pipeline_still_works** — explicit pipeline path unchanged
8. **test_key_migration_atomic** — changing a KeyField value cleans old + creates new atomically
9. **test_delete_remains_atomic** — delete() continues working correctly

## Scope

- Only modifying the two non-pipeline `else` branches in save's `_save_to_redis`
- No changes to the pipeline path (already correct)
- No changes to `delete()` (already uses internal pipeline)
- No field class changes needed (all `on_save`/`on_delete` hooks already accept pipeline param)

## Risks

- **Return value**: `pipeline.execute()` returns a list. We extract `results[0]` (HSET result) to preserve backward compat. All existing tests that check `save()` return value should still pass.
- **Error handling**: If any command in the pipeline fails, `pipeline.execute()` raises. Current behavior is that a failure at any step raises too, so semantics are preserved.
- **Performance**: Actually **improves** — fewer Redis round-trips (1 instead of 5+).

## Success Criteria

1. All 278 existing tests pass (zero regressions)
2. New atomicity tests pass
3. `save()` return value is still an int
4. `create()` + `query.filter()` finds the record immediately
5. `async_create()` + `async_filter()` finds the record immediately
6. Black and ruff pass
