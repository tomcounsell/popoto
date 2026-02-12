---
status: Planning
type: feature
appetite: Large
owner: Tom
created: 2026-02-12
tracking: https://github.com/tomcounsell/popoto/issues/134
---

# Making Migrations Great

## Problem

When migrating existing Popoto model data (adding fields, renaming fields, rebuilding indexes), the load-modify-save pattern triggers side effects that corrupt data. The most acute issue: `auto_now` fields overwrite `updated_at` timestamps during schema migrations that aren't real user updates.

**Current behavior:**
```python
obj = Model.query.get(key="x")
obj.new_field = "value"
obj.save()  # auto_now fires, overwrites updated_at with migration time
```

Beyond `auto_now`, there's no clean way to:
- Update specific fields without re-saving the entire object
- Bulk-modify Redis hashes without instantiating models
- Rebuild secondary indexes (sorted sets, geo, compound) after schema changes
- Perform common migration operations without writing boilerplate Redis commands

**Desired outcome:**
A set of migration-friendly APIs that let developers transform data safely, following patterns proven by Django/SQLAlchemy/Alembic, adapted for Redis's schemaless nature.

## Use Cases for Model Changes

Before designing the solution, here's the full inventory of model changes a Popoto user may need to migrate. These are drawn from patterns in Django, Alembic, and ActiveRecord, filtered for what's relevant to a Redis ORM.

### Tier 1: Common (most projects hit these)

| # | Change | What Breaks | Current Workaround | Needed |
|---|--------|-------------|-------------------|--------|
| 1 | **Add a field** | Nothing (msgpack ignores missing keys) | None needed - field returns `None`/default | Backfill utility if default value needed on all existing records |
| 2 | **Add a field with backfill** | `auto_now` fires on save | Raw Redis `HSET` per key | `save(skip_auto_now=True)` or `save(update_fields=[...])` |
| 3 | **Remove a field** | Orphaned data in hashes, orphaned indexes | Manual cleanup | Field removal cleanup utility |
| 4 | **Rename a field** | Old field key in hash, old indexes | Load-delete-recreate | Hash field rename + index rebuild |
| 5 | **Add a SortedField index** | No sorted set exists for old data | Manual ZADD loop | `rebuild_indexes()` |
| 6 | **Change `partition_by`** | Old sorted sets keyed wrong | Delete old sets + rebuild | `rebuild_indexes()` |

### Tier 2: Structural (key pattern changes)

| # | Change | What Breaks | Current Workaround | Needed |
|---|--------|-------------|-------------------|--------|
| 7 | **Add a KeyField** | Redis key pattern changes entirely | `migrations.py` pipeline.rename pattern | Keep current pattern, improve docs |
| 8 | **Remove a KeyField** | Redis key pattern changes | Same as above | Same |
| 9 | **Rename model class** | All keys, sets, indexes use old class name | Full key migration | Model rename utility |
| 10 | **Change field type** (e.g., `Field` -> `SortedField`) | No index exists for existing data | Manual index build | `rebuild_indexes()` |

### Tier 3: Data transformations

| # | Change | What Breaks | Current Workaround | Needed |
|---|--------|-------------|-------------------|--------|
| 11 | **Transform field values** (e.g., normalize emails) | Stale data | Load-modify-save loop | Bulk update that skips hooks |
| 12 | **Split a field** (e.g., `name` -> `first_name` + `last_name`) | Old field remains | Custom script | `raw_update()` + cleanup |
| 13 | **Merge fields** | Old fields remain | Custom script | Same |
| 14 | **Change field encoding** | Existing data in old format | Re-encode all records | Bulk re-save |

### Tier 4: Index maintenance

| # | Change | What Breaks | Current Workaround | Needed |
|---|--------|-------------|-------------------|--------|
| 15 | **Rebuild all indexes** | Indexes drift from source data | No utility exists | `rebuild_indexes()` |
| 16 | **Add compound index** | No index for existing data | No utility exists | Index build from existing data |
| 17 | **Remove compound index** | Orphaned Redis keys | Manual DEL | Index cleanup utility |
| 18 | **Fix corrupted indexes** | Stale entries in sorted sets | `query.keys(clean=True)` partial fix | Full index rebuild |

## Lessons from Mature ORMs

### Django: Three Tiers of "Save"

Django provides three levels of hook bypass, and this graduated approach is the key insight:

| Level | Django | Hooks | Validation | auto_now |
|-------|--------|-------|------------|----------|
| Full | `instance.save()` | All | Yes | Yes |
| Partial | `instance.save(update_fields=[...])` | Listed only | Listed only | Only if listed |
| Raw | `QuerySet.update()` | None | None | None |

**Popoto should adopt this same three-tier pattern.**

### Django: `update_fields` Behavior

Django's `save(update_fields=["name"])`:
- Issues `UPDATE ... SET name=... WHERE id=...` (partial update)
- Only runs `pre_save`/`post_save` for listed fields
- `auto_now` fields are NOT auto-included - must be explicitly listed
- Empty list = no-op (no SQL issued)

This maps naturally to Redis: `HSET key field1 val1` updates individual hash fields without touching others.

### Alembic: Three-Phase Online Migration

For zero-downtime migrations of live systems:
1. Deploy code that writes to both old AND new structure
2. Background migration converts existing data
3. Deploy code that only uses new structure

This is especially natural for Redis since there are no schema locks.

### ActiveRecord: `update_column` / `update_columns`

Rails provides `update_column(:name, "value")` which:
- Writes directly to DB, skipping all callbacks and validations
- Equivalent to a raw `HSET` in Redis terms

### Key Takeaway

Every mature ORM provides an escape hatch from model hooks for migrations. The question isn't whether to provide one, but how many levels of control to offer.

## Appetite

**Size:** Large

**Team:** Solo dev + PM + reviewer

**Interactions:**
- PM check-ins: 2-3 (API design decisions, scope of index rebuild)
- Review rounds: 2+ (core save path changes need careful review)

This touches the core `save()` path and field hook system. Getting the API right matters more than shipping fast.

## Prerequisites

No prerequisites -- this work has no external dependencies.

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis running | `redis-cli ping` | Tests require Redis |
| Dev install | `python -c "import popoto"` | Package installed |

## Solution

### Key Elements

- **`save(update_fields=[...])`**: Partial save that only updates listed fields and only runs hooks for those fields. `auto_now` fields excluded unless explicitly listed.
- **`save(skip_auto_now=True)`**: Simple flag to suppress `auto_now` during migration saves. Simpler than `update_fields` for the common case.
- **`Model.rebuild_indexes(batch_size=1000)`**: Classmethod that deletes and reconstructs all secondary indexes (sorted sets, key field sets, geo indexes, compound indexes) from source hash data.
- **`Model.raw_update(redis_keys, **field_values)`**: Direct `HSET` on Redis hashes. No validation, no hooks, no index updates. Caller runs `rebuild_indexes()` after.

### Flow

**Migration with auto_now bypass:**
Developer identifies migration need -> Loads instances -> Modifies fields -> `save(skip_auto_now=True)` -> Timestamps preserved

**Migration with partial update:**
Developer identifies fields to change -> `save(update_fields=["new_field"])` -> Only `new_field` written and indexed -> Other fields untouched

**Index rebuild after schema change:**
Developer changes field type or partition_by -> Deploys new code -> Runs `Model.rebuild_indexes()` -> All indexes reconstructed from source data

**Bulk raw update:**
Developer needs fast data transformation -> `Model.raw_update(keys, field=value)` -> Direct HSET, no hooks -> `Model.rebuild_indexes()` to fix indexes

### Technical Approach

- `save(update_fields)`: In `pre_save()`, only run `format_value_pre_save()` for listed fields. In `save()`, use `HSET key field1 val1` for only those fields. Only call `on_save()` for listed fields.
- `save(skip_auto_now)`: Pass flag through to `format_value_pre_save()` on SortedFieldMixin. When True, skip the `auto_now` branch.
- `rebuild_indexes()`: Delete all known index keys (sorted sets, key field sets, geo, compound), then iterate all instances via `query.all()` calling `on_save()` for each field via pipeline.
- `raw_update()`: Accept list of redis_keys and field=value pairs. Issue `HSET` directly via pipeline. No model instantiation.

## Rabbit Holes

- **Migration version tracking / migration files**: Django and Alembic track which migrations have run. This is overkill for Redis -- Popoto's schemaless nature means most changes don't need tracking. Don't build a migration runner.
- **Automatic migration detection**: Comparing "old schema" to "new schema" to auto-generate migrations. Redis has no schema to compare. Don't go here.
- **Reverse migrations**: Alembic-style `downgrade()`. The effort to maintain reverse migrations rarely pays off. Document forward-only patterns.
- **Online dual-write framework**: The three-phase Alembic pattern is useful knowledge but building framework support for it is over-engineering. Document the pattern, don't automate it.

## Risks

### Risk 1: `update_fields` changes the core save path
**Impact:** Regression in normal save behavior if conditional logic is wrong
**Mitigation:** Extensive test coverage. The `update_fields=None` (default) path must be identical to current behavior. Gate all new logic behind `if update_fields is not None`.

### Risk 2: `rebuild_indexes()` on large datasets
**Impact:** Memory pressure or long blocking operation
**Mitigation:** Batch processing with configurable `batch_size`. Use SCAN-based iteration, not `keys()`.

### Risk 3: `raw_update()` leaves indexes inconsistent
**Impact:** Queries return stale results after raw updates
**Mitigation:** Document clearly that `rebuild_indexes()` must follow `raw_update()`. Consider a `raw_update(..., rebuild=True)` convenience flag.

## No-Gos (Out of Scope)

- Migration file framework (no `makemigrations` / `migrate` commands)
- Migration version tracking in Redis
- Automatic schema diff detection
- Reverse / rollback migrations
- GUI or CLI migration runner
- Multi-database migration coordination
- `update_fields` on `create()` (doesn't make sense for new objects)

## Documentation

### Feature Documentation
- [ ] Create `docs/features/migrations.md` describing all migration APIs
- [ ] Update `docs/plans/migration_improvements.md` with final API

### Inline Documentation
- [ ] Docstrings on `save(update_fields, skip_auto_now)` parameters
- [ ] Docstrings on `rebuild_indexes()` and `raw_update()`
- [ ] Update `migrations.py` module docstring with new patterns

### External Documentation Site
- [ ] Update MkDocs migration guide with new APIs and examples

## Success Criteria

- [ ] `save(skip_auto_now=True)` suppresses `auto_now` timestamp updates
- [ ] `save(update_fields=["f1", "f2"])` only updates listed fields and their indexes
- [ ] `save(update_fields=[])` is a no-op (no Redis commands issued)
- [ ] `save()` with no arguments behaves identically to current behavior (no regression)
- [ ] `async_save()` passes through `update_fields` and `skip_auto_now`
- [ ] `Model.rebuild_indexes()` reconstructs sorted set, key field, geo, and compound indexes
- [ ] `Model.raw_update()` performs direct HSET without hooks
- [ ] All existing tests pass unchanged
- [ ] New tests cover each migration use case
- [ ] Documentation updated

## Team Orchestration

### Team Members

- **Builder (save-path)**
  - Name: save-builder
  - Role: Implement `update_fields` and `skip_auto_now` on save/pre_save/async_save
  - Agent Type: builder
  - Resume: true

- **Validator (save-path)**
  - Name: save-validator
  - Role: Verify save path changes don't regress, test all parameter combinations
  - Agent Type: validator
  - Resume: true

- **Builder (index-rebuild)**
  - Name: index-builder
  - Role: Implement `rebuild_indexes()` and `raw_update()` classmethods
  - Agent Type: builder
  - Resume: true

- **Validator (index-rebuild)**
  - Name: index-validator
  - Role: Verify index rebuild correctness across field types
  - Agent Type: validator
  - Resume: true

- **Builder (docs)**
  - Name: docs-builder
  - Role: Update migrations.py docstring, write migration guide
  - Agent Type: documentarian
  - Resume: true

- **Validator (final)**
  - Name: final-validator
  - Role: Run full test suite, verify all success criteria
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Implement `skip_auto_now` on save
- **Task ID**: build-skip-auto-now
- **Depends On**: none
- **Assigned To**: save-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `skip_auto_now: bool = False` parameter to `save()` and `pre_save()`
- Pass `skip_auto_now` through to `format_value_pre_save()` on SortedFieldMixin
- When `skip_auto_now=True`, skip the `if self.auto_now:` branch
- Pass through in `async_save()`
- Add tests: save with skip_auto_now preserves existing timestamp

### 2. Implement `update_fields` on save
- **Task ID**: build-update-fields
- **Depends On**: build-skip-auto-now
- **Assigned To**: save-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `update_fields: Optional[List[str]] = None` parameter to `save()` and `pre_save()`
- In `pre_save()`: only run `format_value_pre_save()` for listed fields (or all if None)
- In `save()`: when `update_fields` set, use `HSET key field1 val1 field2 val2` for only those fields
- In `save()`: only call `on_save()` for listed fields (or all if None)
- `update_fields=[]` should be a no-op
- `auto_now` fields NOT auto-included in `update_fields`
- Add tests for partial saves, no-op empty list, auto_now exclusion

### 3. Validate save path changes
- **Task ID**: validate-save
- **Depends On**: build-update-fields
- **Assigned To**: save-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full existing test suite -- zero regressions
- Verify `save()` with no new args is byte-identical in behavior
- Verify `update_fields` + `skip_auto_now` interaction
- Verify pipeline path and non-pipeline path both work

### 4. Implement `rebuild_indexes()`
- **Task ID**: build-rebuild-indexes
- **Depends On**: none
- **Assigned To**: index-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `rebuild_indexes(cls, batch_size=1000)` classmethod to Model
- Delete all existing secondary index keys (sorted sets, key field sets, geo, compound)
- Iterate all instances, call `on_save()` for each field via pipeline
- Batch pipeline.execute() per batch_size
- Add tests: create instances, delete indexes manually, rebuild, verify queries work

### 5. Implement `raw_update()`
- **Task ID**: build-raw-update
- **Depends On**: none
- **Assigned To**: index-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `raw_update(cls, redis_keys, **field_values)` classmethod
- Direct HSET via pipeline, no model instantiation
- No validation, no hooks, no index updates
- Add tests: raw_update modifies hash values, indexes are stale until rebuild

### 6. Validate index operations
- **Task ID**: validate-indexes
- **Depends On**: build-rebuild-indexes, build-raw-update
- **Assigned To**: index-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify rebuild_indexes works for sorted fields, key fields, geo fields, compound indexes
- Verify raw_update + rebuild_indexes round-trip
- Verify batch_size parameter works correctly

### 7. Update documentation
- **Task ID**: document-feature
- **Depends On**: validate-save, validate-indexes
- **Assigned To**: docs-builder
- **Agent Type**: documentarian
- **Parallel**: false
- Update `migrations.py` module docstring with new API patterns
- Add migration cookbook examples for each use case in the table above

### 8. Final validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: final-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest` -- all tests pass
- Run `mypy src/` -- no new type errors
- Verify all success criteria checked off

## Validation Commands

- `pytest tests/` - Full test suite passes
- `pytest tests/ -k "migration"` - Migration-specific tests pass
- `pytest tests/ -k "auto_now"` - Auto-now tests pass (no regression)
- `pytest tests/ -k "update_fields"` - New partial save tests pass
- `pytest tests/ -k "rebuild"` - Index rebuild tests pass
- `mypy src/` - Type checking passes

---

## Open Questions

1. **`update_fields` scope**: Should `update_fields` also skip validation for non-listed fields, or always validate the full model? Django validates the full model regardless. Skipping validation for non-listed fields would be faster but less safe.

2. **`raw_update` encoding**: Should `raw_update()` accept Python values and msgpack-encode them (matching Popoto's storage format), or accept raw bytes? Encoding them is safer and more consistent.

3. **Async `rebuild_indexes`**: Should we provide `async_rebuild_indexes()` from the start, or add it later? The sync version can be called from async via `to_thread()` like other async methods.

4. **Priority ordering**: Should we ship `skip_auto_now` first as a quick win (smaller PR), then `update_fields` + `rebuild_indexes` as a follow-up? Or all together?
