---
status: Ready
type: feature
appetite: Large
owner: Valor
created: 2026-03-30
tracking: https://github.com/tomcounsell/popoto/issues/307
last_comment_id:
---

# Immutable Key Fields + Indexed Non-Key Fields

## Problem

KeyField conflates two distinct concerns: **identity** (forming the Redis storage key) and **indexing** (enabling queries). Developers who want a queryable field (e.g., `email`, `status`, `category`) are forced to make it a KeyField, even when it should not be part of the object's identity.

**Current behavior:**
- `User.query.filter(email="alice@example.com")` requires `email = KeyField()` or `email = UniqueKeyField()`
- This makes `email` part of the Redis key: `User:alice@example.com`
- Changing `email` silently deletes the old Redis key and creates a new one (key migration via #298), which can orphan references
- There is no runtime protection against accidental KeyField mutation
- `SortedField` provides non-key indexing, but only for range queries -- not exact-match lookups

**Desired outcome:**
- Developers can get exact-match query support on any field without making it a KeyField
- KeyFields are immutable after first save, preventing accidental identity changes
- Intentional key migration remains possible via explicit opt-in (`save(migrate_key=True)`)

## Prior Art

- **Issue #298 / PR #300**: KeyField migration for filter()-loaded instances -- added `_saved_field_values` tracking and obsolete key cleanup in `save()`. This is the foundation for Part 2's immutability detection.
- **PR #150**: Fix KeyField index corruption on value mutation -- fixed Set index cleanup when key values change. Demonstrates the index maintenance pattern this plan extends.
- **PR #148**: Make save() atomic via internal pipeline -- all field `on_save()` hooks run in a single pipeline. Guarantees zero extra round-trips for new indexed field maintenance.
- **Issue #308**: Duplicate of #307 (closed) -- noted edge case that `_saved_field_values` may not be populated for instances not loaded via `query.get()`.
- **Issue #147**: save() without pipeline is non-atomic -- fixed by #148. Relevant because our new index maintenance relies on the pipeline guarantee.

## Data Flow

### Part 1: Indexed Field -- Save Path

1. **Entry point**: `model_instance.save()` called
2. **pre_save()**: Validates all fields including indexed fields (type, null, uniqueness check for `unique=True`)
3. **on_save() hook**: `IndexedFieldMixin.on_save()` fires for each indexed field:
   - `INDEX_SWAP_LUA` reads the server-authoritative `{field}\x00idxset` pointer, SREMs the old Set, SADDs the new Set, and advances the pointer — all atomically
   - If `unique=True`, the script checks occupancy before any write and raises `POPOTO_UNIQUE_CONFLICT` → `ModelException` on conflict
   - Adds `model.db_key.redis_key` to Set at `$IndexF:Model:field_name:value` (or `$UniquF:` for UniqueField)
   - All operations pipelined inside the save MULTI/EXEC — zero extra round-trips
4. **Output**: Instance persisted, index Sets updated atomically

### Part 1: Indexed Field -- Query Path

1. **Entry point**: `Model.query.filter(status="active")`
2. **Query.filter_for_keys_set()**: Routes `status` param to `IndexedFieldMixin.filter_query()` via `filter_query_params_by_field`
3. **filter_query()**: Executes `SMEMBERS $IndexF:Model:status:active` -- returns set of redis_keys
4. **Output**: Set of matching redis_keys passed to `get_many_objects()` for instance hydration

### Part 2: Immutable KeyField -- Save Path

1. **Entry point**: `model_instance.save()` called
2. **Immutability check** (new, early in save): Compares current KeyField values against `_saved_field_values`
   - If `_saved_field_values` is empty/missing: skip check (fresh instance, not loaded from DB)
   - If any KeyField value differs and `migrate_key=True` not passed: raise `KeyMutationError`
   - If `migrate_key=True`: proceed with existing key migration logic
3. **Output**: Either raises error or proceeds with normal save

## Architectural Impact

- **New mixin**: `IndexedFieldMixin` in `src/popoto/fields/indexed_field_mixin.py` -- follows established pattern of KeyFieldMixin and SortedFieldMixin
- **New exception**: `KeyMutationError` in `src/popoto/exceptions.py`
- **Interface changes**: `save()` gains `migrate_key=False` parameter; `Field.__init__` gains `indexed=False` parameter
- **Coupling**: Low -- indexed field mixin is self-contained; immutability check is a guard clause in save()
- **Data ownership**: No change -- indexed fields maintain their own Redis Sets, same as KeyFieldMixin
- **Reversibility**: High -- removing `indexed=True` from a field definition stops maintaining the index; old index Sets become orphaned but harmless. Removing immutability check is a one-line deletion.

## Appetite

**Size:** Large

**Team:** Solo dev, PM

**Interactions:**
- PM check-ins: 1-2 (scope alignment on uniqueness enforcement strategy)
- Review rounds: 1 (code review)

## Prerequisites

No prerequisites -- this work has no external dependencies. Requires only Redis/Valkey (already present in the test environment).

## Solution

### Key Elements

- **IndexedFieldMixin**: New mixin providing Set-based secondary indexes for non-key fields. Implements `on_save()`, `on_delete()`, `get_filter_query_params()`, and `filter_query()` following the exact pattern of KeyFieldMixin.
- **Uniqueness enforcement**: When `indexed=True, unique=True`, `on_save()` checks the target index Set before adding. Raises `ModelException` if another instance already occupies that value.
- **Shortcut classes**: `IndexedField` and `UniqueField` in `shortcuts.py` for ergonomic declarations.
- **Immutability guard**: Early check in `save()` comparing KeyField values against `_saved_field_values`. Raises `KeyMutationError` if changed without `migrate_key=True`.
- **Field attribute**: `indexed=False` added to base `Field.__init__` defaults.

### Flow

**Define model** -> Declare `Field(indexed=True)` -> **save()** -> IndexedFieldMixin.on_save() maintains Set index -> **filter()** -> IndexedFieldMixin.filter_query() queries Set -> **Results**

**Mutate KeyField** -> **save()** -> Immutability check fires -> `KeyMutationError` raised -> **save(migrate_key=True)** -> Existing migration logic proceeds

### Technical Approach

- Index key pattern: `$IndexF:Model:field_name:value` -> Set of redis_keys (mirrors `$KeyF` pattern; `UniqueField` uses `$UniquF:`)
- The prefix is auto-generated by `FieldBase` metaclass via `field_class_key` (e.g. `IndexedField` → `$IndexF`, `UniqueField` → `$UniquF`)
- Reuse `DB_key` for all key construction
- `get_filter_query_params()` returns same lookup set as KeyFieldMixin: exact, `__in`, `__isnull`, `__startswith`, `__endswith`
- `filter_query()` follows KeyFieldMixin's implementation pattern
- Uniqueness enforcement uses `INDEX_SWAP_LUA` — atomic server-side occupancy check + SADD; raises `ModelException` on conflict (PR #412, merged)
- Immutability check runs before `pre_save()` in `save()` -- earliest possible interception point
- `_saved_field_values` empty dict (fresh instance) or missing attribute: skip immutability check to prevent false positives

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `KeyMutationError` raised on KeyField mutation -- test asserts exact exception type and message
- [ ] `ModelException` raised on uniqueness violation for `Field(indexed=True, unique=True)` -- test asserts message includes field name and duplicate value
- [ ] `save(migrate_key=True)` does NOT raise -- test confirms successful key migration

### Empty/Invalid Input Handling
- [ ] `Field(indexed=True)` with `None` value: index Set key uses `None` string (same as KeyFieldMixin pattern)
- [ ] `Field(indexed=True)` with empty string: index Set key uses empty string segment
- [ ] Fresh instance (no `_saved_field_values`) does not false-positive on immutability check

### Error State Rendering
- [ ] `KeyMutationError` message includes field name and old/new values for debuggability
- [ ] Uniqueness violation message includes model name, field name, and conflicting value

## Test Impact

- [ ] `tests/test_key_fields.py` -- UPDATE: Add assertions that KeyField mutation raises `KeyMutationError` (currently KeyField mutation silently triggers key migration)
- [ ] `tests/test_migrations.py` -- UPDATE: Verify `save(migrate_key=True)` works as the new escape hatch for intentional migrations
- [ ] `tests/test_field_index_edge_cases.py` -- UPDATE: May need new cases for indexed non-key fields interacting with existing edge cases

No other existing tests should break because:
- `indexed` defaults to `False`, so all existing Field declarations are unaffected
- The immutability check only fires when `_saved_field_values` is populated AND a KeyField value has changed, which existing tests that mutate KeyFields will now need `migrate_key=True` for

## Rabbit Holes

- **Hash-based index for unique fields**: Using a single Redis Hash per field instead of Set-per-value. Dropped because Set-per-value is simpler, already proven, and memory overhead is negligible.
- **`model.migrate_key()` dedicated method**: Nice-to-have but `save(migrate_key=True)` is sufficient. Can be added later as sugar.
- **`KeyField(mutable=True)` per-field opt-out**: With indexed non-key fields available, there is no legitimate use case for mutable key fields. All key fields are immutable.
- **RediSearch/Valkey-Search modules**: Out of scope -- must use native commands only.
- **Composite indexed fields**: Indexing on combinations of non-key fields (e.g., `indexed_together`). Separate feature if ever needed.

## Risks

### Risk 1: Breaking existing code that mutates KeyFields
**Impact:** Any code that changes a KeyField value and calls `save()` will now raise `KeyMutationError` instead of silently migrating.
**Mitigation:** This is intentional -- silent key migration is the bug this feature fixes. All such call sites must add `migrate_key=True`. Clear error message guides the fix. The CHANGELOG must document this breaking change prominently.

### Risk 2: Uniqueness check race condition
**Impact:** Two concurrent saves with the same unique indexed value could both pass the uniqueness check before either writes the Set.
**Mitigation:** **Resolved by PR #412 (merged).** `IndexedFieldMixin.on_save()` now uses `INDEX_SWAP_LUA` — an atomic Lua script that performs the uniqueness check and SADD as a single server-side operation inside the save MULTI/EXEC. The second concurrent writer gets a `POPOTO_UNIQUE_CONFLICT` error reply, mapped to `ModelException`. No "best-effort" caveat needed for the internal pipeline path. (The external-pipeline path — `save(pipeline=...)` — still has a best-effort pre-check; the authoritative enforcement is at `execute()` time.)

## Race Conditions

### Race 1: Concurrent uniqueness check for indexed unique fields
**Location:** `IndexedFieldMixin.on_save()` — previously a SCARD/SMEMBERS check followed by SADD
**Trigger:** Two processes save instances with the same unique indexed value simultaneously
**Data prerequisite:** Index Set must be empty (or contain only self) at check time
**State prerequisite:** Both checks execute before either write
**Mitigation:** **Resolved by PR #412 (merged).** The check-and-SADD are now a single atomic `INDEX_SWAP_LUA` EVAL. See `docs/indexed_fields.md` for the Concurrency Guarantee section.

## No-Gos (Out of Scope)

- `model.migrate_key()` dedicated method -- `save(migrate_key=True)` is sufficient for now
- `KeyField(mutable=True)` per-field mutability control -- all key fields are immutable
- Composite indexed fields (`indexed_together`) -- separate feature
- Redis module-based indexing (RediSearch, etc.)
- Automatic index rebuild/migration tooling -- can use existing `reindex()` pattern
- `__contains` lookup for indexed fields -- KeyFieldMixin has it declared but not implemented; same for indexed fields

## Update System

No update system changes required -- this is a library feature in the Popoto package. Users get it via `pip install --upgrade popoto`.

## Agent Integration

No agent integration required -- this is a library-level ORM feature. No MCP servers, bridge changes, or tool wrapping needed.

## Documentation

### Feature Documentation
- [ ] Create `docs/indexed_fields.md` describing `Field(indexed=True)`, `Field(indexed=True, unique=True)`, `IndexedField`, and `UniqueField` with examples
- [ ] Update `docs/fields.md` (or equivalent field reference) to include indexed field attribute
- [ ] Update `docs/queries.md` (or equivalent query reference) to show filtering on indexed non-key fields

### External Documentation Site
- [ ] Update MkDocs pages for field types and query filtering
- [ ] Add indexed fields to the field type comparison table
- [ ] Verify `mkdocs build` passes

### Inline Documentation
- [ ] Docstrings on `IndexedFieldMixin` class and all public methods
- [ ] Docstring on `KeyMutationError` with example
- [ ] Update `save()` docstring to document `migrate_key` parameter
- [ ] Code comments on `INDEX_SWAP_LUA` explaining the server-authoritative pointer and KEYS/ARGV contract (already done in PR #412)

## Success Criteria

- [ ] `Field(indexed=True)` maintains a Set-based index and supports `filter()` queries (exact match, `__in`, `__isnull`)
- [ ] `Field(indexed=True, unique=True)` enforces uniqueness on save
- [ ] `IndexedField` and `UniqueField` shortcut classes exist in `shortcuts.py`
- [ ] Modifying a KeyField value and calling `save()` raises `KeyMutationError`
- [ ] `save(migrate_key=True)` bypasses the immutability check and performs key migration
- [ ] Instances not loaded via `query.get()` (no `_saved_field_values`) do not false-positive on immutability check
- [ ] Index maintenance adds zero extra Redis round-trips (pipelined with existing save)
- [ ] All indexed field operations work on both Redis and Valkey (no modules required)
- [ ] Tests cover: indexed field CRUD, uniqueness enforcement, KeyField immutability, migrate_key escape hatch, partial saves with indexed fields
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (indexed-field-mixin)**
  - Name: indexed-mixin-builder
  - Role: Implement IndexedFieldMixin, shortcuts, and Field attribute
  - Agent Type: builder
  - Resume: true

- **Builder (immutability-guard)**
  - Name: immutability-builder
  - Role: Implement KeyMutationError and save() immutability check
  - Agent Type: builder
  - Resume: true

- **Test Engineer (full-suite)**
  - Name: test-engineer
  - Role: Write tests for both features, update existing tests
  - Agent Type: test-engineer
  - Resume: true

- **Validator (integration)**
  - Name: integration-validator
  - Role: Verify end-to-end behavior, index consistency, and backward compatibility
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Create indexed fields documentation, update field/query references
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Add `indexed` attribute to base Field and create IndexedFieldMixin
- **Task ID**: build-indexed-mixin
- **Depends On**: none
- **Validates**: tests/test_indexed_fields.py (create)
- **Assigned To**: indexed-mixin-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `indexed: bool = False` to `Field.__init__` defaults in `field.py`
- Create `src/popoto/fields/indexed_field_mixin.py` with `IndexedFieldMixin` class
- Implement `on_save()`: maintain Set at `$IndexF:Model:field_name:value`, using the `INDEX_SWAP_LUA` atomic script (already implemented by PR #412) for old-value cleanup and uniqueness enforcement
- Implement `on_delete()`: remove from Set
- Implement `get_filter_query_params()`: return exact, `__in`, `__isnull`, `__startswith`, `__endswith`
- Implement `filter_query()`: SMEMBERS for exact, SUNION for `__in`, scan for pattern lookups
- Uniqueness enforcement is already provided by `INDEX_SWAP_LUA` (PR #412): the script raises `POPOTO_UNIQUE_CONFLICT` → `ModelException` when occupied by another instance
- Register indexed fields in `ModelOptions.add_field()` -- add `indexed_field_names` set, track fields with `indexed=True`

### 2. Add shortcut classes
- **Task ID**: build-shortcuts
- **Depends On**: build-indexed-mixin
- **Validates**: tests/test_indexed_fields.py
- **Assigned To**: indexed-mixin-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `IndexedField(IndexedFieldMixin, Field)` to `shortcuts.py` -- sets `indexed=True`
- Add `UniqueField(IndexedFieldMixin, Field)` to `shortcuts.py` -- sets `indexed=True, unique=True, null=False`
- Export from `src/popoto/__init__.py`

### 3. Implement KeyField immutability
- **Task ID**: build-immutability
- **Depends On**: none
- **Validates**: tests/test_immutable_keys.py (create)
- **Assigned To**: immutability-builder
- **Agent Type**: builder
- **Parallel**: true (parallel with task 1)
- Add `KeyMutationError(ModelException)` to `src/popoto/exceptions.py`
- Add `migrate_key=False` parameter to `save()` in `base.py`
- Add immutability guard early in `save()`: iterate `_meta.key_field_names`, compare `getattr(self, name)` to `_saved_field_values.get(name)`, raise `KeyMutationError` if any differ and `migrate_key` is not True
- Guard must skip check when `_saved_field_values` is empty (fresh instance not loaded from DB)
- Guard must skip auto key fields (they are set once and never change)

### 4. Write comprehensive tests
- **Task ID**: build-tests
- **Depends On**: build-indexed-mixin, build-shortcuts, build-immutability
- **Validates**: pytest tests/test_indexed_fields.py tests/test_immutable_keys.py -v
- **Assigned To**: test-engineer
- **Agent Type**: test-engineer
- **Parallel**: false
- Create `tests/test_indexed_fields.py`:
  - Test indexed field CRUD: save with indexed field, verify Set exists, delete and verify Set cleaned up
  - Test filter by indexed field: exact match, `__in`, `__isnull`
  - Test `__startswith` and `__endswith` on indexed fields
  - Test uniqueness enforcement: save two instances with same unique indexed value, assert raises
  - Test uniqueness allows update of own value (re-save same instance)
  - Test indexed field value change: old Set entry removed, new Set entry added
  - Test partial save (`update_fields`) with indexed fields
  - Test indexed field with `null=True` and None value
  - Test multiple indexed fields on same model
- Create `tests/test_immutable_keys.py`:
  - Test KeyField mutation raises `KeyMutationError`
  - Test `save(migrate_key=True)` succeeds
  - Test fresh instance (no prior save) does not raise
  - Test instance constructed without `query.get()` does not false-positive
  - Test AutoKeyField is exempt from immutability check
  - Test multiple KeyFields: changing any one raises
  - Test error message includes field name and values
- Update `tests/test_key_fields.py` and `tests/test_migrations.py` as needed for `migrate_key=True`

### 5. Integration validation
- **Task ID**: validate-integration
- **Depends On**: build-tests
- **Assigned To**: integration-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify backward compatibility: existing models without `indexed=True` behave identically
- Verify index key patterns in Redis: `redis-cli KEYS '$IndexF:*'` (IndexedField) and `redis-cli KEYS '$UniquF:*'` (UniqueField)
- Verify no extra Redis round-trips: check pipeline command count before/after indexed field addition

### 6. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-integration
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/indexed_fields.md` with usage examples and API reference
- Update field reference docs
- Update query filtering docs
- Verify `mkdocs build` passes

### 7. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: integration-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all verification checks from table below
- Verify all success criteria met
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check src/ tests/` | exit code 0 |
| Format clean | `python -m ruff format --check src/ tests/` | exit code 0 |
| Indexed field test | `pytest tests/test_indexed_fields.py -v` | exit code 0 |
| Immutable key test | `pytest tests/test_immutable_keys.py -v` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| IndexedField importable | `python -c "from popoto import IndexedField, UniqueField"` | exit code 0 |
| KeyMutationError importable | `python -c "from popoto.exceptions import KeyMutationError"` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

No open questions -- the issue spec is detailed and the codebase recon confirms all assumptions. The solution follows established patterns (KeyFieldMixin, SortedFieldMixin) with no novel architectural decisions.
