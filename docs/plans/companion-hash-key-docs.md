---
status: Ready
type: chore
appetite: Small
owner: Valor
created: 2026-04-03
tracking: https://github.com/tomcounsell/popoto/issues/338
last_comment_id:
---

# Companion Hash Key Public API Documentation

## Problem

PR #336 exposed nine companion hash key methods as public API across ConfidenceField, CyclicDecayField, and CoOccurrenceField. These methods let users build Redis keys for companion hashes (data hashes, cycle hashes, pressure hashes, edge sorted sets) without reverse-engineering suffix conventions from source code.

**Current behavior:**

The only documentation is in `docs/plans/companion-hash-key-api.md` (the implementation plan). No user-facing docs mention `get_data_hash_key()`, `get_cycles_hash_key()`, `get_pressure_hash_key()`, `get_edge_key()`, or their `_from_values`/`_from_parts` variants. Users who need direct Redis access to companion hashes have no guidance.

**Desired outcome:**

- Both instance-based and class-based companion key methods documented in `docs/api-reference.md`
- A practical debugging/inspection example in `docs/features/confidence-field.md`
- A key inspection example in `docs/multi-tenancy.md` showing how to verify tenant isolation at the Redis level

## Prior Art

- **PR #336**: Expose public API for companion hash key methods -- shipped the code, updated docstrings, but did not update user-facing docs
- **PR #323 / Plan**: companion-hash-key-api.md -- implementation plan that defined the method signatures and patterns

No prior attempts to document these methods in user-facing docs.

## Architectural Impact

No architectural impact. This is purely a documentation change -- no code, no tests, no behavior changes.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

This is a docs-only task: add sections to three existing markdown files.

## Prerequisites

No prerequisites -- this work has no external dependencies.

## Solution

### Key Elements

- **api-reference.md updates**: Add method signatures, parameters, return values, and brief descriptions for all nine public companion key methods, organized under their respective field class sections
- **confidence-field.md example**: Add a practical example showing how to use `get_data_hash_key()` for debugging or direct Redis inspection
- **multi-tenancy.md example**: Add a section showing how to use companion key methods to verify tenant isolation of companion hashes

### Technical Approach

#### 1. api-reference.md -- Add companion key methods to each field section

**ConfidenceField section** (after `migrate_to_partitioned`, before "ObservationProtocol entrainment"):

Add three new method subsections:

- `ConfidenceField.get_data_hash_key(instance, field_name)` -- builds Redis key for the companion hash from a model instance. Documents the key pattern (`$ConfidencF:{Model}:{field}:data[:{partition}]`).
- `ConfidenceField.get_data_hash_key_from_values(model_class, field_name, **partition_values)` -- builds companion hash key from explicit partition values (no instance needed). Documents the `QueryException` raised when partition values are missing.
- `ConfidenceField.get_old_data_hash_key(instance, field_name)` -- builds key using saved (pre-mutation) partition values. Useful during custom migrations.

**CyclicDecayField section** (after the parameter table, before AccessTrackerMixin):

Add four new method subsections:

- `CyclicDecayField.get_cycles_hash_key(instance, field_name)` -- builds Redis key for cycles companion hash. Pattern: `$CyclicDecayF:{Model}:{field}:{partitions}:cycles`.
- `CyclicDecayField.get_pressure_hash_key(instance, field_name)` -- builds Redis key for pressure companion hash. Pattern: `$CyclicDecayF:{Model}:{field}:{partitions}:pressure`.
- `CyclicDecayField.get_cycles_hash_key_from_parts(model_class, field_name, *partition_values)` -- class method, builds cycles key from explicit partition values.
- `CyclicDecayField.get_pressure_hash_key_from_parts(model_class, field_name, *partition_values)` -- class method, builds pressure key from explicit partition values.

**CoOccurrenceField section** (needs to be located or added; currently CoOccurrenceField is only referenced in Defaults table):

Add two method subsections in a new CoOccurrenceField heading if one does not exist, or append to the existing section:

- `CoOccurrenceField.get_edge_key(model_class, pk)` -- builds Redis key for a PK's edge sorted set. Pattern: `$CoOcF:{ClassName}:{field_name}:{pk}`.
- `CoOccurrenceField.get_edge_key_prefix(model_class)` -- builds key prefix for scanning all edge sets.

#### 2. confidence-field.md -- Add debugging example

Insert a new subsection under "## API Reference" titled "### Inspecting Companion Hash Keys". Content:

```python
# Get the Redis key for direct inspection
memory = Memory.query.get(key="fact1")
field = Memory._options.fields["certainty"]
hash_key = field.get_data_hash_key(memory, "certainty")
print(hash_key)
# => "$ConfidencF:Memory:certainty:data"  (or with partition suffix)

# Use with redis-cli for debugging
import redis
r = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"))
raw_data = r.hgetall(hash_key)
```

Also show the `get_data_hash_key_from_values` variant for query-path usage without loading an instance.

#### 3. multi-tenancy.md -- Add key inspection example

Add a subsection under "## Hash-based field partitioning (ConfidenceField)" titled "### Verifying tenant isolation". Content shows using `get_data_hash_key_from_values()` to confirm that companion hashes are separate per tenant:

```python
field = Memory._options.fields["certainty"]

# Build keys for each tenant without loading instances
atlas_key = field.get_data_hash_key_from_values(Memory, "certainty", project="atlas")
hermes_key = field.get_data_hash_key_from_values(Memory, "certainty", project="hermes")

print(atlas_key)   # => "$ConfidencF:Memory:certainty:data:atlas"
print(hermes_key)  # => "$ConfidencF:Memory:certainty:data:hermes"
assert atlas_key != hermes_key  # Companion hashes are fully isolated
```

## Failure Path Test Strategy

### Exception Handling Coverage
No exception handlers in scope -- this is a documentation-only change.

### Empty/Invalid Input Handling
Not applicable -- no code changes.

### Error State Rendering
Not applicable -- no code changes.

## Test Impact

No existing tests affected -- this is a documentation-only change with no code modifications.

## Rabbit Holes

- Adding a tutorial-style "working with companion hashes" standalone guide -- overkill for nine methods that are documented in context
- Documenting internal key construction details (how `get_special_use_field_db_key` works) -- implementation detail, not user-facing
- Adding CoOccurrenceField full API reference to api-reference.md beyond the two key methods -- separate issue, out of scope

## Risks

### Risk 1: Key pattern examples become stale
**Impact:** Documented patterns diverge from actual key format after future refactoring.
**Mitigation:** Examples use method calls to show the key, not hardcoded strings. The method output is shown as comments that are easy to verify.

## Race Conditions

No race conditions identified -- this is a documentation-only change.

## No-Gos (Out of Scope)

- Full CoOccurrenceField API reference (link/get_linked/propagate) -- separate documentation task
- CyclicDecayField full API reference -- only companion key methods are in scope
- Code changes of any kind
- New test files

## Update System

No update system changes required -- popoto is a library dependency, not a deployed service.

## Agent Integration

No agent integration required -- this is a documentation-only change.

## Documentation

This plan IS the documentation task. The deliverables are:

### External Documentation Site
- [ ] Update `docs/api-reference.md` with companion key method signatures for ConfidenceField (3 methods), CyclicDecayField (4 methods), CoOccurrenceField (2 methods)
- [ ] Update `docs/features/confidence-field.md` with practical debugging example
- [ ] Update `docs/multi-tenancy.md` with tenant isolation verification example
- [ ] Verify docs build passes (`mkdocs build`)

## Success Criteria

- [ ] All nine public companion key methods documented in `docs/api-reference.md` with signatures, parameters, return values
- [ ] At least one practical usage example in `docs/features/confidence-field.md`
- [ ] Tenant isolation example in `docs/multi-tenancy.md`
- [ ] `mkdocs build` passes without errors
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (docs)**
  - Name: docs-builder
  - Role: Write documentation sections for all three files
  - Agent Type: documentarian
  - Resume: true

- **Validator (docs)**
  - Name: docs-validator
  - Role: Verify docs build, check method signatures match source code
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Add companion key methods to api-reference.md
- **Task ID**: build-api-ref
- **Depends On**: none
- **Validates**: mkdocs build
- **Assigned To**: docs-builder
- **Agent Type**: documentarian
- **Parallel**: true
- Add `get_data_hash_key`, `get_data_hash_key_from_values`, `get_old_data_hash_key` under ConfidenceField section (after `migrate_to_partitioned`, before "ObservationProtocol entrainment")
- Add `get_cycles_hash_key`, `get_pressure_hash_key`, `get_cycles_hash_key_from_parts`, `get_pressure_hash_key_from_parts` under CyclicDecayField section (after parameter table, before AccessTrackerMixin)
- Add `get_edge_key`, `get_edge_key_prefix` in a CoOccurrenceField section

### 2. Add debugging example to confidence-field.md
- **Task ID**: build-confidence-example
- **Depends On**: none
- **Assigned To**: docs-builder
- **Agent Type**: documentarian
- **Parallel**: true
- Add "Inspecting Companion Hash Keys" subsection under "API Reference" showing `get_data_hash_key()` and `get_data_hash_key_from_values()` usage

### 3. Add tenant isolation example to multi-tenancy.md
- **Task ID**: build-multi-tenancy-example
- **Depends On**: none
- **Assigned To**: docs-builder
- **Agent Type**: documentarian
- **Parallel**: true
- Add "Verifying tenant isolation" subsection under "Hash-based field partitioning" showing `get_data_hash_key_from_values()` for per-tenant key inspection

### 4. Validate documentation
- **Task ID**: validate-docs
- **Depends On**: build-api-ref, build-confidence-example, build-multi-tenancy-example
- **Assigned To**: docs-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `mkdocs build` and verify no errors
- Cross-check method signatures in docs against source code
- Verify all nine methods are documented

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: validate-docs
- **Assigned To**: docs-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all verification commands
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Docs build | `mkdocs build -q` | exit code 0 |
| ConfidenceField methods in api-ref | `grep -c 'get_data_hash_key' docs/api-reference.md` | output > 0 |
| CyclicDecayField methods in api-ref | `grep -c 'get_cycles_hash_key' docs/api-reference.md` | output > 0 |
| CoOccurrenceField methods in api-ref | `grep -c 'get_edge_key' docs/api-reference.md` | output > 0 |
| Example in confidence-field.md | `grep -c 'get_data_hash_key' docs/features/confidence-field.md` | output > 0 |
| Example in multi-tenancy.md | `grep -c 'get_data_hash_key_from_values' docs/multi-tenancy.md` | output > 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

No open questions -- the scope is clear (document existing public methods) and all method signatures are already defined in source code.
