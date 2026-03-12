---
status: Planning
type: feature
appetite: Medium
owner: tomcounsell
created: 2026-03-12
tracking: https://github.com/tomcounsell/popoto/issues/181
last_comment_id:
---

# Meta.namespace for Runtime Key Prefixing

## Problem

Multi-tenant applications and vault-scoped memory systems need to isolate data by prefixing all Redis keys for a model. For example, a memory system where project A's data lives under `mem:project-a:Episode:...` and project B's under `mem:project-b:Episode:...`, using the same model class definitions.

**Current behavior:**
There is an `env_partition_name` parameter in `set_REDIS_DB_settings()` (redis_db.py:133) with a TODO comment saying "use this to mark keys in redis db, so they can be separated and deleted" -- but it is completely non-functional. The parameter is accepted and read but never applied to any key generation. There is no way to prefix keys at the model level.

**Desired outcome:**
A `namespace` option on `Model.Meta` that prefixes ALL Redis keys for that model. The namespace must be overridable at runtime so the same model class can operate against different key spaces within a single process.

## Prior Art

No prior issues or PRs found related to namespace/prefix functionality. The only prior art is the unused `env_partition_name` parameter in `set_REDIS_DB_settings()`.

## Data Flow

The namespace prefix must be injected at every point where Redis keys are constructed. Here is the complete key generation flow:

1. **ModelOptions.__init__** (base.py:116-117): Creates `db_class_key = DB_key(model_name)` and `db_class_set_key = DB_key("$Class", db_class_key)`. These are the root keys from which all others derive.
2. **Model.db_key** property (base.py:645-651): Builds instance keys as `DB_key(self._meta.db_class_key, [key_field_values...])` producing `ClassName:val1:val2`.
3. **Field.get_special_use_field_db_key** (field.py:480): Builds field index keys as `DB_key(cls.field_class_key, model._meta.db_class_key, *field_names)` producing `$KeyF:ClassName:field_name`.
4. **KeyFieldMixin.on_save** (key_field_mixin.py:248): Creates unique set keys like `$KeyF:ClassName:field_name:value`.
5. **SortedFieldMixin.get_sortedset_db_key** (sorted_field_mixin.py:385): Creates sorted set keys like `$SortedF:ClassName:field_name:partition_values`.
6. **GeoField.get_geo_db_key** (geo_field.py:276): Creates geo index keys like `$GeoF:ClassName:field_name`.
7. **Query operations** (query.py): Uses `db_class_set_key` for SMEMBERS, `db_class_key` for SCAN patterns, and delegates to field mixins for index lookups.
8. **scan_keys** (redis_db.py:306): SCAN operations use patterns like `ClassName:*` which must also respect namespace.

The critical insight is that `db_class_key` is the root from which nearly all other keys are derived. If namespace is injected there, it propagates to instance keys, field index keys, and query operations automatically.

## Architectural Impact

- **New dependencies**: None -- uses only stdlib `contextvars` for thread-safe runtime overrides.
- **Interface changes**: New `Meta.namespace` attribute; new `Model.using_namespace()` context manager; new `Query.namespace()` chainable method. All additive -- no existing API changes.
- **Coupling**: Minimal increase. The namespace is stored on `ModelOptions` and read during key construction. A `contextvars.ContextVar` provides runtime override without coupling models to thread state.
- **Data ownership**: No change -- models still own their data; namespace is purely a key prefix concern.
- **Reversibility**: Fully reversible. Removing namespace support would only affect code that explicitly uses it. Default behavior (no namespace) is identical to current behavior.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1 (scope alignment on API surface)
- Review rounds: 1 (code review)

## Prerequisites

No prerequisites -- this work has no external dependencies.

## Solution

### Key Elements

- **ContextVar-based namespace resolution**: A `contextvars.ContextVar` holds the active namespace override. When no override is set, falls back to `Meta.namespace` (default empty string).
- **Namespace-aware db_class_key**: `ModelOptions.db_class_key` becomes a property that prepends the resolved namespace to the model name, producing keys like `ns:ClassName` instead of `ClassName`.
- **using_namespace() context manager**: Sets the ContextVar for a block of code, enabling runtime isolation.
- **Query.namespace() chain method**: Sets namespace on a per-query basis for one-off queries.

### Flow

**Default (no namespace)** -> Keys are `ClassName:val` as today -> No behavioral change

**Static namespace** -> `class Meta: namespace = "mem"` -> Keys become `mem:ClassName:val`

**Runtime override** -> `with Model.using_namespace("mem:project-a"):` -> Keys become `mem:project-a:ClassName:val` within the block

### Technical Approach

- Store the default namespace in `ModelOptions` from `Meta.namespace` (read in `ModelBase.__new__`)
- Create a module-level `ContextVar("popoto_namespace", default=None)` in a new `src/popoto/context.py` (or in `redis_db.py`)
- Make `ModelOptions.db_class_key` a property that resolves: `ContextVar value || Meta.namespace || ""` and prepends it to the model name
- Cache the un-namespaced key as `_db_class_key_base` to avoid repeated DB_key construction
- `db_class_set_key` similarly becomes a property derived from `db_class_key`
- `using_namespace()` is a classmethod on Model returning a context manager that sets/resets the ContextVar
- The ContextVar approach is per-model-class. Each model class gets its own ContextVar so namespaces can differ per model. Alternatively, a single global ContextVar with a dict mapping model names to namespaces. The simpler approach: one ContextVar per model stored on ModelOptions.

**Key decision: per-model vs global ContextVar**

The issue says "the same model class needs to work against different namespaces in the same process." This implies a global ContextVar (one for all models) is sufficient -- when you enter a namespace context, ALL models use that namespace. This is simpler and matches the multi-tenant use case (tenant isolation applies to all models).

Use a single global `ContextVar` in `context.py`. The `using_namespace()` context manager on Model sets this global var. Individual models can still have different static `Meta.namespace` defaults, but the runtime override applies globally.

Resolution order for a given model:
1. Global ContextVar (runtime override) -- if set, used as-is
2. `Meta.namespace` on the model class -- static default
3. Empty string -- no prefix (backward compatible)

**Implementation detail for db_class_key as property:**

Currently `db_class_key` is set once in `ModelOptions.__init__` as `DB_key(self.model_name)`. Making it a property means every access constructs a new DB_key. To mitigate: cache the result keyed by the current namespace value. Since namespaces change rarely (only within context manager blocks), this cache will almost always hit.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] No `except Exception: pass` blocks in the touched files for namespace logic
- [ ] Test that invalid namespace values (None after explicit set, non-string) raise TypeError

### Empty/Invalid Input Handling
- [ ] Empty string namespace = no prefix (backward compatible)
- [ ] Namespace with colons (e.g., "mem:project-a") works correctly -- colons in namespace are literal separators, not escaped
- [ ] Namespace with trailing colon is normalized (strip trailing colon since it's added as separator)

### Error State Rendering
- [ ] Not applicable -- this is a library feature with no user-visible rendering

## Rabbit Holes

- **Per-model ContextVars**: Allowing different runtime namespaces for different models simultaneously adds complexity without clear use cases. Use a single global ContextVar. If needed later, this can be extended.
- **Namespace migration tooling**: Migrating existing data from one namespace to another is a separate concern. Do not build migration utilities in this PR.
- **Async-specific namespace propagation**: `contextvars` works natively with `asyncio` -- ContextVars propagate to tasks automatically. Do not add special async handling.
- **Making env_partition_name functional**: The existing `env_partition_name` parameter is a different abstraction (connection-level). Resolve the TODO by documenting that `Meta.namespace` is the recommended approach, but do not wire `env_partition_name` into key generation.

## Risks

### Risk 1: Performance regression from property-based db_class_key
**Impact:** Every save/query/delete constructs a DB_key on access instead of reading a cached attribute.
**Mitigation:** Cache the constructed DB_key keyed by namespace value. ContextVar.get() is extremely fast (~50ns). Only reconstruct on namespace change.

### Risk 2: Namespace colons conflicting with key delimiter
**Impact:** A namespace like `mem:project-a` adds colons to the key, which could confuse `DB_key.from_redis_key()` parsing.
**Mitigation:** Namespace is prepended as a single opaque prefix followed by a colon separator. The `from_redis_key` method would need awareness that the first N segments may be namespace. Alternative: join namespace with a non-colon separator (but colons are natural for Redis). Best approach: store namespace segment count on ModelOptions so `from_redis_key` knows how many leading segments to skip.

## Race Conditions

No race conditions identified. `contextvars.ContextVar` is designed for concurrent use -- each async task or thread gets its own copy. The namespace resolution is read-only relative to the ContextVar (only `using_namespace()` writes to it, and it uses a token-based reset pattern).

## No-Gos (Out of Scope)

- Data migration between namespaces
- Namespace-aware FLUSHDB or bulk deletion across namespaces
- Connection-level partitioning (different Redis instances per namespace)
- Nested namespace composition (e.g., namespace="a" + using_namespace("b") = "a:b")
- Pub/sub namespace isolation (separate feature if needed)
- Making `env_partition_name` functional beyond documentation update

## Update System

No update system changes required -- this is a library feature change in the popoto package.

## Agent Integration

No agent integration required -- this is a core ORM library feature.

## Documentation

### Feature Documentation
- [ ] Update `docs/models.md` or equivalent with `Meta.namespace` documentation
- [ ] Add namespace usage examples to docs
- [ ] Add entry to changelog

### External Documentation Site
- [ ] Update MkDocs pages for Model Meta options
- [ ] Verify docs build passes

### Inline Documentation
- [ ] Docstrings on `using_namespace()`, `Meta.namespace`, namespace resolution logic
- [ ] Code comments explaining ContextVar caching strategy

## Success Criteria

- [ ] `Meta.namespace = "prefix"` causes all keys for that model to be prefixed with `prefix:`
- [ ] `Model.using_namespace("x")` context manager overrides namespace for all operations within the block
- [ ] `Query.namespace("x").filter(...)` works for one-off namespaced queries
- [ ] Default behavior (no namespace) produces identical keys to current behavior (backward compatible)
- [ ] All key types are prefixed: instance keys, $Class sets, $KeyF sets, $SortedF sorted sets, $GeoF geo indexes
- [ ] SCAN operations in `scan_keys` respect the active namespace
- [ ] Namespace works correctly with async operations (ContextVar propagation)
- [ ] The existing `env_partition_name` TODO is resolved with a docstring pointing to `Meta.namespace`
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (namespace-core)**
  - Name: namespace-builder
  - Role: Implement namespace resolution, ModelOptions changes, context manager, and query integration
  - Agent Type: builder
  - Resume: true

- **Builder (namespace-tests)**
  - Name: test-builder
  - Role: Write comprehensive tests for namespace feature
  - Agent Type: test-engineer
  - Resume: true

- **Validator (namespace-validation)**
  - Name: namespace-validator
  - Role: Verify all key types are correctly prefixed and backward compatibility is maintained
  - Agent Type: validator
  - Resume: true

- **Documentarian (namespace-docs)**
  - Name: docs-writer
  - Role: Update documentation for namespace feature
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Implement namespace context and ModelOptions changes
- **Task ID**: build-namespace-core
- **Depends On**: none
- **Assigned To**: namespace-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/context.py` with global `ContextVar("popoto_namespace", default=None)`
- Add `namespace` attribute to `ModelOptions.__init__` (default empty string)
- Read `Meta.namespace` in `ModelBase.__new__` and store on options
- Make `ModelOptions.db_class_key` a cached property that resolves namespace and prepends to model name
- Make `ModelOptions.db_class_set_key` derive from the namespaced `db_class_key`
- Add `using_namespace()` classmethod/context manager on `Model`
- Update `Model.db_key` property to use the namespace-aware `db_class_key`

### 2. Implement Query.namespace() chain method
- **Task ID**: build-query-namespace
- **Depends On**: build-namespace-core
- **Assigned To**: namespace-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `namespace()` method to `QueryBuilder` and `Query` classes
- Ensure namespace is applied when building query keys
- Ensure SCAN patterns in query use namespaced prefix

### 3. Update env_partition_name TODO
- **Task ID**: build-cleanup
- **Depends On**: build-namespace-core
- **Assigned To**: namespace-builder
- **Agent Type**: builder
- **Parallel**: true
- Update the TODO comment in `set_REDIS_DB_settings()` to document that `Meta.namespace` is the recommended approach
- Update docstrings referencing `env_partition_name`

### 4. Write tests for namespace feature
- **Task ID**: build-tests
- **Depends On**: build-namespace-core
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Test static `Meta.namespace` produces correct key prefixes for all key types
- Test `using_namespace()` context manager overrides and restores namespace
- Test `Query.namespace()` chain method
- Test backward compatibility (no namespace = current behavior)
- Test namespace with colons in value
- Test namespace with sorted fields, geo fields, key fields, relationships
- Test nested context managers
- Test async compatibility

### 5. Validate implementation
- **Task ID**: validate-namespace
- **Depends On**: build-tests, build-query-namespace, build-cleanup
- **Assigned To**: namespace-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify all success criteria are met
- Run full test suite
- Check that no existing tests are broken
- Verify key generation for each key type

### 6. Documentation
- **Task ID**: document-namespace
- **Depends On**: validate-namespace
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Update model documentation with Meta.namespace
- Add usage examples
- Update changelog

### 7. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-namespace
- **Assigned To**: namespace-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all validation commands
- Verify all success criteria met (including documentation)
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Namespace test exists | `pytest tests/ -k "namespace" -v` | exit code 0 |
| Backward compat | `pytest tests/ -x -q` | exit code 0 |

---

## Open Questions

1. **Namespace separator**: Should the namespace be joined to the model name with a colon (e.g., `mem:project-a:Episode:key`) or a different separator? Colons are natural for Redis but mean `DB_key.from_redis_key()` needs to know how many leading segments are namespace vs. model name. Alternative: use a different separator like `|` or store namespace length metadata.

2. **Global vs per-model runtime override**: The plan proposes a single global `ContextVar` so `using_namespace("x")` affects ALL models within the block. Should it be possible to override namespace for a single model class while leaving others at their default? (e.g., `Episode.using_namespace("x")` only affects Episode, not other models used inside the block.)

3. **Namespace in from_redis_key**: When deserializing keys from Redis (e.g., from SCAN results), how should the namespace prefix be stripped? Options: (a) Store namespace segment count on ModelOptions and strip during parsing. (b) Always strip everything before the known model class name. (c) Require the caller to pass the namespace context.

4. **Query.namespace() scope**: Should `Query.namespace()` set the namespace only for that specific query execution, or should it persist on the QueryBuilder for chained calls? The former is cleaner but the latter is more Django-like.
