---
status: Planning
type: feature
appetite: Medium
owner: Solo dev
created: 2026-03-13
tracking: https://github.com/tomcounsell/popoto/issues/193
last_comment_id: 4053387057
---

# DecayingSortedField — Time-Weighted Scoring via Lua

## Problem

Popoto's `SortedField` stores a static numeric score. To rank records by "relevance considering recency," developers must manually track timestamps, compute decay formulas in Python, and re-sort results client-side via `computed_sort()`. This is O(N) fetch + O(N log N) sort, and the decay logic leaks into every query call site.

**Current behavior:**
```python
import time, math

episodes = Episode.query.filter(agent_id="agent-1").all()
now = time.time()
ranked = sorted(
    episodes,
    key=lambda e: e.base_score * math.pow(max((now - e.last_accessed) / 86400, 0.01), -0.5),
    reverse=True
)[:10]
```

**Desired outcome:**
```python
class Episode(Model):
    agent_id = KeyField()
    relevance = DecayingSortedField(decay_rate=0.5)
    importance = FloatField(default=1.0)

# Server-side decay ranking — no Python math, no full-set fetch
top = Episode.query.filter(agent_id="agent-1").top_by_decay("relevance", n=10)

# Refresh timestamp without full save
episode.touch("relevance")
```

## Prior Art

- **PR #194**: Added Lua decay proof-of-concept tests (`tests/test_lua_decay_scoring.py`) — validates the decay formula and Lua execution pattern. DecayingSortedField builds directly on this.
- **PR #190**: Added `atomic_increment()` — established the pattern for Lua scripting in Popoto (uses `cmsgpack` + `POPOTO_REDIS_DB.eval()`). DecayingSortedField follows the same Lua script approach.
- **PR #189 / Issue #182**: Added `computed_sort()` to QueryBuilder — client-side sorting that `top_by_decay()` replaces for the decay use case. `computed_sort()` remains available for non-decay sorting.
- **PR #131 / Issue #124**: Range queries on SortedField (`__between`) — the `filter_query` pattern that DecayingSortedField inherits unchanged.
- **PR #138**: Renamed `sort_by` to `partition_by` — DecayingSortedField inherits this directly from `SortedFieldMixin`.

## Data Flow

1. **Model definition**: Developer declares `relevance = DecayingSortedField(decay_rate=0.5, base_score_field="importance")` on a Model class.
2. **Class creation (metaclass)**: `ModelBase` sees `DecayingSortedField` as a `SortedFieldMixin` subclass, registers `relevance` in `_meta.sorted_field_names`. The field's `field_class_key` is `$DecayingSortedF`.
3. **on_save()**: When a model instance is saved, `DecayingSortedField.on_save()` calls the parent `SortedFieldMixin.on_save()` which stores `current_timestamp` as the sorted set score (member = model redis_key, score = `time.time()`). The sorted set key follows the existing pattern: `$DecayingSortedF:{ModelName}:{field_name}:{partition_values}`.
4. **touch()**: `model.touch("relevance")` issues a single `ZADD` to update the timestamp in the sorted set without a full `model.save()`. Also updates `_saved_field_values` in-memory.
5. **top_by_decay(n)**: `QueryBuilder.top_by_decay("relevance", n=10)` executes a Lua script server-side:
   - Reads all members + timestamps from the sorted set via `ZRANGE ... WITHSCORES`
   - For each member, reads `base_score` from the model's hash (via `HGET` + msgpack decode of the `base_score_field` value) or defaults to 1.0
   - Computes `base_score * elapsed_days^(-decay_rate)` for each member
   - Sorts by decayed score descending, returns top-N member keys
   - QueryBuilder then fetches the full model instances for those keys via pipeline
6. **Output**: Returns a list of model instances ranked by decayed relevance.

## Architectural Impact

- **New dependencies**: None. Uses existing `redis-py` eval() and Lua (built into Redis).
- **Interface changes**: Adds `DecayingSortedField` class to `fields/shortcuts.py` (or new file), adds `top_by_decay()` method to `QueryBuilder`, adds `touch()` method to `Model`.
- **Coupling**: Low. `DecayingSortedField` subclasses `SortedFieldMixin` — no modifications to existing classes. `top_by_decay()` is a new method on `QueryBuilder`, not a modification of existing methods. `touch()` is a new method on `Model`.
- **Data ownership**: The sorted set score changes meaning from "field value" to "timestamp." This is internal to DecayingSortedField and does not affect existing SortedField behavior.
- **Reversibility**: Fully reversible — new subclass, new methods, no breaking changes.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (scope alignment on base_score handling)
- Review rounds: 1 (code review)

Three new components (field subclass, Lua script, QueryBuilder method) plus a Model method (`touch`), plus tests. The Lua script pattern is already proven by PR #190 and the POC tests in PR #194.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis server running | `redis-cli ping` | Lua script execution and sorted set operations |
| Existing Lua POC tests pass | `pytest tests/test_lua_decay_scoring.py -x -q` | Validates decay formula and Lua execution work |

## Solution

### Key Elements

- **DecayingSortedField**: A subclass of `SortedFieldMixin` + `Field` that stores timestamps as sorted set scores and provides `decay_rate` and `base_score_field` configuration.
- **Lua decay script**: Server-side script that computes `base_score * elapsed_days^(-decay_rate)` for all members in a sorted set and returns top-N ranked keys.
- **QueryBuilder.top_by_decay()**: New method on `QueryBuilder` that executes the Lua script and returns ranked model instances.
- **Model.touch()**: New method that updates the sorted set timestamp for a DecayingSortedField without a full save.

### Flow

**Developer defines model** → `DecayingSortedField` registers in `_meta.sorted_field_names`

**model.save()** → `on_save()` stores `time.time()` as sorted set score (inherits from `SortedFieldMixin`)

**model.touch("field")** → Single `ZADD` updates timestamp in sorted set

**query.top_by_decay("field", n)** → Lua script computes decay scores → returns top-N redis keys → pipeline fetch → model instances

### Technical Approach

1. **DecayingSortedField class** (`src/popoto/fields/decaying_sorted_field.py`):
   - Subclasses `SortedFieldMixin, Field` (same pattern as `SortedField`)
   - Constructor accepts `decay_rate` (float, default 0.5) and `base_score_field` (str or None, default None)
   - Forces `type=float` and `auto_now=True` behavior (always stores current timestamp as score)
   - `on_save()` delegates to `SortedFieldMixin.on_save()` with `time.time()` as the score
   - `format_value_pre_save()` always returns `time.time()` (the field value IS the timestamp)
   - All existing filter params (`__gt`, `__gte`, `__lt`, `__lte`, `__between`) work unchanged against the timestamp

2. **Lua script** (embedded as a constant string in the field module):
   - Based on the proven script from `tests/test_lua_decay_scoring.py`
   - KEYS[1] = sorted set key, ARGV[1] = now, ARGV[2] = decay_rate, ARGV[3] = max_results
   - For `base_score_field`: reads each member's model hash via `HGET` + `cmsgpack.unpack()` to extract the base score field value
   - If no `base_score_field`, all base scores default to 1.0
   - ARGV[4] = base_score_field name (or empty string for default=1.0)

3. **QueryBuilder.top_by_decay()** (in `src/popoto/models/query.py`):
   - `top_by_decay(field_name, n=10, decay_rate=None, base_score_field=None)` method on `QueryBuilder`
   - Validates `field_name` refers to a `DecayingSortedField`
   - Builds the sorted set key (respecting `partition_by` using filter params)
   - Calls `POPOTO_REDIS_DB.eval()` with the Lua script
   - Parses returned keys, fetches full instances via pipeline
   - Optional `decay_rate` and `base_score_field` overrides (defaults to field-level config)

4. **Model.touch()** (in `src/popoto/models/base.py`):
   - `touch(field_name)` method on `Model`
   - Validates field is a `DecayingSortedField`
   - Issues `ZADD` to update the sorted set score to `time.time()`
   - Updates `_saved_field_values` and in-memory attribute
   - Supports optional `pipeline` parameter for atomic batching

5. **Registration** (in `src/popoto/__init__.py` and `src/popoto/fields/shortcuts.py`):
   - Export `DecayingSortedField` from both modules
   - Add to `__all__`

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `touch()` on unsaved model instance raises `TypeError` (same pattern as `atomic_increment()`)
- [ ] `touch()` on non-DecayingSortedField raises `TypeError`
- [ ] `top_by_decay()` on non-DecayingSortedField raises `QueryException`
- [ ] `top_by_decay()` without required `partition_by` filter raises `QueryException`

### Empty/Invalid Input Handling
- [ ] `top_by_decay()` on empty sorted set returns empty list
- [ ] `top_by_decay(n=0)` returns empty list
- [ ] `touch()` with nonexistent field name raises `AttributeError`
- [ ] `DecayingSortedField(decay_rate=0)` raises `ModelException` (division by zero in formula)

### Error State Rendering
- [ ] Not applicable — this is a data layer feature with no user-visible rendering

## Rabbit Holes

- **ZRANGEBYSCORE pre-filtering before Lua**: Tempting to add a time window pre-filter to reduce Lua iteration scope. This is a future optimization — for now, Lua iterates all members. The issue explicitly defers this to when a background worker exists (roadmap Steps 6/10).
- **Periodic score materialization**: Writing decayed scores back to a separate sorted set for O(log N) queries. This requires a background worker and is explicitly deferred.
- **CyclicDecayField**: The issue comment mentions harmonic cycle extensions. This is a separate subclass and a separate issue — do not scope-creep.
- **Async version of top_by_decay**: The async API can be added later following the existing async pattern. Do not implement in this PR.

## Risks

### Risk 1: Lua script performance on large sorted sets
**Impact:** `top_by_decay()` iterates all members of the sorted set (or partition). For 10K+ members, this may cause Redis latency spikes.
**Mitigation:** The issue explicitly accepts this tradeoff. Tests will benchmark 1K and 10K member sets to establish baselines. Future optimization via ZRANGEBYSCORE pre-filtering is deferred.

### Risk 2: cmsgpack availability in Redis Lua
**Impact:** The Lua script uses `cmsgpack.unpack()` to read base_score from model hashes. If Redis is compiled without cmsgpack, the script fails.
**Mitigation:** cmsgpack has been built into Redis since 2.6. The existing `atomic_increment()` (PR #190) already depends on cmsgpack, so this is proven infrastructure.

### Risk 3: Base score field may not exist on all model instances
**Impact:** If `base_score_field="importance"` but some instances don't have that field set, `HGET` returns nil.
**Mitigation:** The Lua script already handles this — `tonumber(base_str) or 1.0` defaults to 1.0 when the field is missing.

## Race Conditions

### Race 1: Concurrent touch() and save()
**Location:** `DecayingSortedField.on_save()` and `Model.touch()`
**Trigger:** Thread A calls `model.touch("relevance")` while Thread B calls `model.save()`. Both update the sorted set score.
**Data prerequisite:** Model must exist in Redis.
**State prerequisite:** Sorted set must contain the member.
**Mitigation:** Both operations use `ZADD` which is atomic in Redis. The last write wins, which is correct — both operations set the timestamp to `time.time()`, so the later one produces the more recent timestamp.

## No-Gos (Out of Scope)

- **CyclicDecayField** with harmonic cycle components — separate issue per the roadmap comment
- **ObservationProtocol** for passive behavioral inference — downstream consumer, not part of this primitive
- **Async `top_by_decay()`** — follow-up, not blocking
- **ZRANGEBYSCORE pre-filtering optimization** — explicitly deferred per issue
- **Score materialization via background worker** — explicitly deferred per issue
- **Modifying existing `SortedField` behavior** — this is a new subclass, not a modification

## Update System

No update system changes required — this is a library feature addition with no deployment or service concerns.

## Agent Integration

No agent integration required — this is a Popoto library primitive. Downstream consumers (like the Behavioral Episode Memory System in tomcounsell/ai#376) will use it, but that integration is in a different repository.

## Documentation

### Feature Documentation
- [ ] Add `docs/features/decaying-sorted-field.md` describing the field, its parameters, and usage examples
- [ ] Update `docs/features/README.md` index (if exists)

### External Documentation Site
- [ ] Add DecayingSortedField to the MkDocs site field reference
- [ ] Include usage examples with the agent memory use case

### Inline Documentation
- [ ] Docstrings on `DecayingSortedField`, `top_by_decay()`, and `touch()`
- [ ] Inline comments on the Lua script explaining the decay formula

## Success Criteria

- [ ] `DecayingSortedField` subclasses `SortedFieldMixin` and registers as a sorted field in `ModelOptions`
- [ ] `on_save()` stores current timestamp as the sorted set score
- [ ] `base_score_field` parameter reads base score from a companion field; defaults to 1.0 when unset
- [ ] `decay_rate` parameter defaults to 0.5 and is configurable per field
- [ ] `top_by_decay(n)` method on QueryBuilder executes Lua script and returns ranked model instances
- [ ] `top_by_decay()` accepts optional `decay_rate` and `base_score_field` overrides at query time
- [ ] `touch(field_name)` method on Model updates the sorted set timestamp without full save
- [ ] Existing `SortedField` filter params (`__gt`, `__gte`, `__lt`, `__lte`, `__between`) work unchanged on the timestamp score
- [ ] `partition_by` works identically to `SortedField`
- [ ] Tests verify decay formula correctness against hand-computed values
- [ ] Tests verify ranking: recent records outscore older records with equal base score
- [ ] Tests verify `base_score_field` affects ranking (higher base score wins at equal age)
- [ ] Lua script benchmarked for 1K and 10K member sorted sets
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (decaying-field)**
  - Name: field-builder
  - Role: Implement DecayingSortedField class, Lua script, QueryBuilder.top_by_decay(), and Model.touch()
  - Agent Type: builder
  - Resume: true

- **Test Engineer (decay-tests)**
  - Name: decay-tester
  - Role: Write comprehensive tests for decay scoring, edge cases, and benchmarks
  - Agent Type: test-engineer
  - Resume: true

- **Validator (integration)**
  - Name: integration-validator
  - Role: Verify all success criteria, run full test suite, check no regressions
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Create DecayingSortedField class
- **Task ID**: build-field
- **Depends On**: none
- **Assigned To**: field-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/fields/decaying_sorted_field.py` with `DecayingSortedField(SortedFieldMixin, Field)`
- Accept `decay_rate` (float, default 0.5) and `base_score_field` (str or None) parameters
- Override `format_value_pre_save()` to always return `time.time()`
- Override `on_save()` to store timestamp as sorted set score (delegate to parent)
- Embed the Lua decay script as a module-level constant
- Add `DecayingSortedField` export to `__init__.py` and `shortcuts.py`

### 2. Add top_by_decay() to QueryBuilder
- **Task ID**: build-query
- **Depends On**: build-field
- **Assigned To**: field-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `top_by_decay(field_name, n=10, decay_rate=None, base_score_field=None)` method to `QueryBuilder`
- Validate field is a DecayingSortedField
- Build sorted set key using partition_by from filter params
- Execute Lua script via `POPOTO_REDIS_DB.eval()`
- Parse returned keys and fetch model instances via pipeline
- Return list of model instances in decayed-score order

### 3. Add touch() to Model
- **Task ID**: build-touch
- **Depends On**: build-field
- **Assigned To**: field-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `touch(field_name, pipeline=None)` method to `Model`
- Validate field is a DecayingSortedField and model is saved
- Issue `ZADD` to update sorted set score to `time.time()`
- Update in-memory attribute and `_saved_field_values`

### 4. Write tests
- **Task ID**: build-tests
- **Depends On**: build-query, build-touch
- **Assigned To**: decay-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Test DecayingSortedField registration in ModelOptions
- Test on_save() stores timestamp
- Test touch() updates timestamp without full save
- Test top_by_decay() returns correctly ranked instances
- Test base_score_field affects ranking
- Test decay_rate parameter (low vs high decay)
- Test partition_by works with top_by_decay()
- Test existing filter params (__gt, __gte, __lt, __lte, __between) work on timestamp
- Test error cases: unsaved model, wrong field type, missing partition filter
- Test empty sorted set returns empty list
- Benchmark 1K and 10K member sets

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-tests
- **Assigned To**: integration-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite (`pytest tests/ -x -q`)
- Verify all success criteria met
- Check no regressions in existing SortedField tests
- Verify exports in `__init__.py`

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Decay tests pass | `pytest tests/test_decaying_sorted_field.py -x -q` | exit code 0 |
| Lua POC tests still pass | `pytest tests/test_lua_decay_scoring.py -x -q` | exit code 0 |
| Export exists | `python -c "from popoto import DecayingSortedField; print('OK')"` | output contains OK |
| Lint clean | `python -m ruff check src/popoto/fields/decaying_sorted_field.py` | exit code 0 |

---

## Open Questions

1. **Base score storage location**: The Lua script in the POC uses a separate hash key for base scores. In the full implementation, the base score lives in the model's own hash (e.g., `importance` field). The Lua script will need to `HGET` the model hash + `cmsgpack.unpack()` to extract the named field's value. This is the approach used by `atomic_increment()`. Is this the preferred approach, or should base scores be duplicated into a separate hash for simpler Lua access?

2. **field_class_key naming**: Should the sorted set key prefix be `$DecayingSortedF` (following the FieldBase metaclass pattern where the class name minus "Field" gets the prefix)? This means DecayingSortedField sorted sets will be separate from regular SortedField sorted sets, which is correct behavior but worth confirming.

3. **touch() scope**: Should `touch()` be restricted to DecayingSortedField only, or should it also work on any SortedField with `auto_now=True`? The issue specifies DecayingSortedField-only, but a broader `touch()` might be useful.
