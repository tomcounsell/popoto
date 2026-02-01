# Plan: Comprehensive Stress Tests for Popoto

**Status**: PLANNED
**Created**: 2026-02-01
**Issue**: #63 follow-up — improve test coverage and confidence

## New file: `tests/test_stress.py`

A single test file using proper pytest structure with fixtures for cleanup. Each test creates, exercises, and verifies data at scale. Uses `pytest.mark.slow` so they can be skipped in quick CI runs.

### Test cases

1. **test_bulk_create_save_delete** — Create and save 1000 KeyValue models, query all back, verify count and data integrity, delete all, verify empty.

2. **test_bulk_sorted_field_range_queries** — Save 500 items with SortedField(type=int), then run range queries (`__gt`, `__lt`, `__gte`, `__lte`) and verify result counts match expected ranges.

3. **test_bulk_geo_radius_search** — Save 200 locations with GeoField spread across a geographic area, then run radius searches at various distances and verify results are within the expected radius.

4. **test_bulk_unique_key_operations** — Save 500 items with UniqueKeyField, verify uniqueness enforcement (duplicate raises exception), verify all are queryable by unique key.

5. **test_bulk_relationship_integrity** — Create 100 parent + 500 child models with Relationship fields, query children by parent, verify referential integrity after saves and deletes.

6. **test_bulk_filter_key_field** — Save 500 items with varied KeyField values, exercise `__startswith`, `__endswith`, `__in`, `__contains`, `__isnull` filters at scale, verify result correctness.

7. **test_bulk_mixed_field_types** — Save 200 models with int, float, decimal, string, bool, list, dict, date, datetime fields, load them all back, assert every field round-trips correctly.

8. **test_async_concurrent_operations** — Use `asyncio.gather` to concurrently create 200 items, then concurrently query them, then concurrently delete them. Verify data integrity at each step.

9. **test_sorted_field_ordering** — Save 500 items with SortedField, query with `order_by` (ascending and descending), verify ordering is correct across the full result set.

10. **test_rapid_update_cycles** — Save 100 items, then update each one 10 times in sequence (1000 total saves), verify final values are correct and no data corruption.

### Shared fixtures

- `flush_redis` autouse fixture calling `POPOTO_REDIS_DB.flushdb()` before each test
- Model classes defined at module level (outside test functions)
- `pytest.mark.slow` on all tests

### pyproject.toml update

Add pytest marker config:
```toml
[tool.pytest.ini_options]
markers = ["slow: stress and performance tests"]
```

## Files to modify
- **Create**: `tests/test_stress.py`
- **Edit**: `pyproject.toml` — add pytest markers config

## Verification
```bash
pytest tests/test_stress.py -v        # Run all stress tests
pytest -m "not slow"                  # Run fast tests only
pytest tests/test_stress.py -k bulk_create  # Run single stress test
```
