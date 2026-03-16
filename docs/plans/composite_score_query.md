---
status: Planning
type: feature
appetite: Medium
owner: Valor
created: 2026-03-16
tracking: https://github.com/tomcounsell/popoto/issues/212
last_comment_id:
---

# CompositeScoreQuery — Multi-Factor Retrieval

## Problem

Each agent memory primitive produces its own scoring signal — decay-weighted timestamps, Bayesian confidence, access frequency, write filter priority, co-occurrence weights. But retrieval is single-factor: `top_by_decay()` ranks by time-decay alone. An agent needs to retrieve by a weighted composite of all signals simultaneously.

**Current behavior:**
Retrieving by multiple factors requires application-level code: fetch by decay, fetch confidence data, fetch access counts, compute composite in Python, re-rank. This is slow (multiple round trips), error-prone, and not composable via the query API.

**Desired outcome:**
A single query method that combines N sorted set indexes with configurable weights and returns top-K results ranked by composite score, with model hydration.

```python
results = Memory.query.filter(agent_id="agent-1").composite_score(
    indexes={"relevance": 0.4, "confidence": 0.3, "access_score": 0.2, "priority": 0.1},
    limit=10,
)
```

## Prior Art

- **PR #189**: `computed_sort()` — QueryBuilder extension pattern. Adds Python-side sorting via a caller-provided key function. CompositeScoreQuery follows the same QueryBuilder extension pattern but operates server-side in Redis for performance.
- **PR #199**: `DecayingSortedField` + `top_by_decay()` — Lua-based server-side scoring pattern. CompositeScoreQuery extends this to multiple indexes.
- **Issue #182**: `computed_sort()` planning — established the precedent for adding new query methods to QueryBuilder.

No prior attempts at composite/multi-index queries found.

## Data Flow

1. **Entry point**: Application calls `Model.query.filter(...).composite_score(indexes={...}, limit=10)`
2. **Index resolution**: For each named index, resolve the Redis sorted set key. Some indexes are native sorted sets (DecayingSortedField, WriteFilter priority). Others (ConfidenceField, AccessTracker) require materialization into temporary sorted sets.
3. **Materialization** (conditional): For non-ZSET indexes, a Lua script materializes hash data into a temporary sorted set keyed by the model PKs that exist in the filter set.
4. **ZUNIONSTORE**: Redis combines all resolved sorted set keys into a single temporary sorted set with configurable weights and aggregate mode (SUM/MIN/MAX).
5. **ZREVRANGE**: Top-K members extracted from the composite sorted set.
6. **Cleanup**: Temporary keys deleted (EXPIRE 5s for safety).
7. **Hydration**: PKs passed to existing Query infrastructure for model instance loading.
8. **Post-filter** (optional): Application callback filters hydrated instances.
9. **Output**: List of model instances ranked by composite score.

## Architectural Impact

- **New dependencies**: None — uses existing Redis commands (ZUNIONSTORE, ZREVRANGE, EXPIRE)
- **Interface changes**: New `composite_score()` method on `QueryBuilder` and `Query` classes. Follows the same pattern as `top_by_decay()` and `computed_sort()`.
- **Coupling**: Low. The method resolves index keys generically — it doesn't import or depend on specific field types. Index resolution uses a registry pattern so new field types can register their sorted set key patterns.
- **Data ownership**: No change. Each field continues to own its own index. CompositeScoreQuery reads but never writes to field indexes.
- **Reversibility**: Easy — it's an additive method with no schema changes.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (scope alignment on index resolution strategy)
- Review rounds: 1

## Prerequisites

No prerequisites — this work builds on existing shipped primitives (Steps 1-6).

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis available | `python -c "from src.popoto.redis_db import POPOTO_REDIS_DB; POPOTO_REDIS_DB.ping()"` | Redis connection |
| DecayingSortedField exists | `python -c "from popoto.fields import DecayingSortedField"` | Step 1 shipped |

## Solution

### Key Elements

- **`composite_score()` on QueryBuilder**: New chainable method accepting index names, weights, limit, aggregate mode, and optional post-filter callback.
- **Index resolver**: Maps field names to their Redis sorted set keys. For native ZSET fields (DecayingSortedField, WriteFilter priority), resolves directly. For non-ZSET fields (ConfidenceField, AccessTracker), materializes temporary sorted sets via Lua.
- **Temp key management**: All temporary keys use a `$CSQ:` prefix with a UUID suffix and 5-second EXPIRE for cleanup safety.

### Flow

**Query call** → resolve partition filters → resolve index keys → materialize non-ZSET indexes → ZUNIONSTORE with weights → ZREVRANGE top-K → cleanup temps → hydrate models → optional post-filter → return results

### Technical Approach

#### Index Resolution Strategy

Each index name maps to a sorted set key via a resolution function. The resolver handles four cases:

1. **DecayingSortedField / CyclicDecayField**: Direct sorted set key `$SortedF:{Class}:{field}[:{partition}]`. But note: these store raw timestamps, not decay-computed scores. For composite scoring, we need the *decayed* scores. Two options:
   - **Option A (chosen)**: Materialize decay-computed scores into a temp ZSET via the existing Lua decay script, then feed that into ZUNIONSTORE.
   - Option B: Apply decay at ZUNIONSTORE time by treating timestamps as scores (loses the power-law math).

2. **WriteFilter priority**: Direct key `$WF:{Class}:priority`. Already a ZSET with filter scores.

3. **ConfidenceField**: Companion hash `$ConfidencF:{Class}:{field}:data`. Materialize: Lua reads all hash entries, unpacks msgpack confidence values, writes to temp ZSET.

4. **AccessTracker**: Meta hash `$AT:{Class}:meta:{pk}`. Materialize: Lua iterates PKs, reads `access_count` from each meta hash, writes to temp ZSET.

5. **CoOccurrence propagation**: Already returns a dict of `{pk: weight}` from `propagate()`. Application injects as a pre-computed temp ZSET.

#### Materialization via Lua

A single Lua script handles materialization for each non-ZSET index type:

```lua
-- KEYS[1] = source key (hash or pattern)
-- KEYS[2] = destination temp ZSET key
-- ARGV[1] = source type ("confidence_hash" | "access_meta")
-- ARGV[2...] = PKs to include (from filter set)
```

This keeps materialization server-side (no round trips per PK).

#### ZUNIONSTORE Execution

```python
# After all indexes are resolved to ZSET keys:
temp_composite_key = f"$CSQ:{model_name}:{uuid4().hex[:8]}"
POPOTO_REDIS_DB.zunionstore(
    temp_composite_key,
    keys={resolved_key: weight for resolved_key, weight in resolved_indexes},
    aggregate=aggregate_mode,  # SUM (default), MIN, MAX
)
POPOTO_REDIS_DB.expire(temp_composite_key, 5)
pks = POPOTO_REDIS_DB.zrevrange(temp_composite_key, 0, limit - 1, withscores=True)
```

#### Post-filter Callback

Optional callable receives `(redis_key, score)` tuples and returns `True` to keep. Applied after ZREVRANGE but before hydration. Useful for application-level filtering (e.g., exclude already-used memories).

#### API Design

```python
class QueryBuilder:
    def composite_score(
        self,
        indexes: dict[str, float],     # {field_name: weight}
        limit: int = 10,
        aggregate: str = "SUM",         # SUM | MIN | MAX
        min_score: float = None,        # optional floor
        post_filter: callable = None,   # optional (pk, score) -> bool
        co_occurrence_boost: dict = None,  # {pk: weight} from propagate()
    ) -> list:
        ...
```

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] Invalid field name in indexes dict → raise QueryException with clear message
- [ ] Field exists but has no sorted set index (e.g., plain Field) → raise QueryException
- [ ] Empty indexes dict → raise QueryException
- [ ] ZUNIONSTORE fails (temp key collision, Redis error) → cleanup temp keys, re-raise

### Empty/Invalid Input Handling
- [ ] Empty result set → return empty list (not None)
- [ ] All indexes empty (no members in any sorted set) → return empty list
- [ ] `limit=0` → return empty list
- [ ] Single index → still works (degenerate case, effectively a weighted sort)

### Error State Rendering
- [ ] Not applicable — this is an ORM query method, not user-facing UI

## Rabbit Holes

- **Real-time decay recomputation in ZUNIONSTORE**: Don't try to make ZUNIONSTORE itself apply decay math. Materialize decayed scores into a temp ZSET first, then combine. The two-step approach is clearer and proven by `top_by_decay()`.
- **Generic index auto-discovery**: Don't scan model fields to auto-detect all sorted sets. Require explicit index names in the `indexes` parameter. Auto-discovery adds complexity without clear value — the caller knows which signals matter for their retrieval context.
- **Persistent composite indexes**: Don't cache the ZUNIONSTORE result. Composite scores depend on real-time decay and changing weights. Always recompute.
- **Normalization across indexes**: Don't normalize scores to [0,1] before combining. Different indexes have different score distributions and that's fine — the weights handle relative importance. Normalization would add a full scan per index.

## Risks

### Risk 1: Materialization Latency for Large Sets
**Impact:** For models with 100K+ instances, materializing ConfidenceField or AccessTracker data into temp ZSETs could be slow (O(N) Lua script blocking Redis).
**Mitigation:** The Lua materialization script operates on the intersection of the filter set and the index, not all instances. Partition filters (via `filter()`) narrow this down. Document that composite_score should always be used with partition filters on large datasets.

### Risk 2: Temp Key Leaks
**Impact:** If the process crashes between ZUNIONSTORE and cleanup, orphaned temp keys accumulate.
**Mitigation:** All temp keys use EXPIRE 5s set immediately after creation. Even if cleanup fails, keys auto-expire. The `$CSQ:` prefix makes them identifiable for manual cleanup.

## Race Conditions

### Race 1: Concurrent composite_score queries
**Location:** Temp key creation and ZUNIONSTORE
**Trigger:** Two queries running simultaneously with the same model
**Data prerequisite:** None — each query creates its own UUID-suffixed temp key
**State prerequisite:** None
**Mitigation:** UUID suffix on temp keys ensures no collision. Each query's temp keys are fully independent.

### Race 2: Index mutation during query
**Location:** Between index resolution and ZUNIONSTORE
**Trigger:** Another process saves/deletes a model instance while the query is running
**Data prerequisite:** Source sorted sets must exist
**State prerequisite:** None
**Mitigation:** Acceptable — this is eventually consistent, same as any Redis query. The 5-second window is short enough that stale data is negligible.

## No-Gos (Out of Scope)

- Score normalization across indexes
- Automatic index discovery from model fields
- Persistent/cached composite indexes
- Async variant (defer to a later PR, following the pattern of other query methods)
- Integration with ExistenceFilter pre-checks (Step 8 concern)
- ContextAssembler token budgeting (Step 12 concern)

## Update System

No update system changes required — this is a library feature in the Popoto ORM package.

## Agent Integration

No agent integration required — this is an ORM query method consumed by application code.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/agent-memory.md` CompositeScoreQuery section with shipped API
- [ ] Add entry to `docs/fields.md` or create `docs/features/composite-score-query.md`

### External Documentation Site
- [ ] Update mkdocs pages if applicable
- [ ] Verify docs build passes

### Inline Documentation
- [ ] Docstrings on `composite_score()` method with examples
- [ ] Document index resolution behavior for each field type

## Success Criteria

- [ ] `composite_score()` method on QueryBuilder and Query classes
- [ ] Configurable index weights and aggregate mode (SUM/MIN/MAX)
- [ ] Index resolution for: DecayingSortedField, WriteFilter priority, ConfidenceField, AccessTracker
- [ ] CoOccurrence boost injection via `co_occurrence_boost` parameter
- [ ] Temp key cleanup via EXPIRE
- [ ] Model hydration from composite results
- [ ] Optional `post_filter` callback
- [ ] Optional `min_score` floor
- [ ] Synergy test: decay + confidence → high-confidence recent record outranks low-confidence old record
- [ ] Synergy test: + access frequency → frequently accessed records get boost
- [ ] Synergy test: + write filter priority → priority-tagged records rank higher
- [ ] Synergy test: + co-occurrence boost → record with mediocre scores but strong association surfaces
- [ ] Single-index degenerate case works correctly
- [ ] Empty result handling (empty indexes, no matching members)
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (composite-score)**
  - Name: composite-builder
  - Role: Implement composite_score() method, index resolver, materialization Lua scripts
  - Agent Type: builder
  - Resume: true

- **Builder (tests)**
  - Name: test-builder
  - Role: Implement synergy tests and edge case tests
  - Agent Type: test-engineer
  - Resume: true

- **Validator (composite-score)**
  - Name: composite-validator
  - Role: Verify implementation meets all success criteria
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Update agent-memory.md and create feature docs
  - Agent Type: documentarian
  - Resume: true

### Available Agent Types

**Tier 1 — Core (default choices):**
- `builder` - General implementation
- `validator` - Read-only verification
- `test-engineer` - Test implementation

## Step by Step Tasks

### 1. Implement composite_score() method and index resolver
- **Task ID**: build-composite-score
- **Depends On**: none
- **Assigned To**: composite-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `composite_score()` method to `QueryBuilder` class in `src/popoto/models/query.py`
- Add convenience `composite_score()` to `Query` class
- Implement index resolver that maps field names to Redis ZSET keys
- Handle DecayingSortedField: materialize decay-computed scores via Lua into temp ZSET
- Handle WriteFilter priority: resolve `$WF:{Class}:priority` directly
- Handle ConfidenceField: Lua materialization from companion hash to temp ZSET
- Handle AccessTracker: Lua materialization from meta hashes to temp ZSET
- Handle CoOccurrence boost: accept pre-computed dict, write to temp ZSET
- Execute ZUNIONSTORE with weights and aggregate mode
- ZREVRANGE top-K with optional min_score
- EXPIRE all temp keys (5s)
- Hydrate models via existing query infrastructure
- Apply optional post_filter callback

### 2. Implement synergy and edge case tests
- **Task ID**: build-tests
- **Depends On**: build-composite-score
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Two-index synergy: decay + confidence
- Three-index synergy: decay + confidence + access frequency
- Four-index synergy: + write filter priority
- CoOccurrence boost synergy test
- Single-index degenerate case
- Empty indexes dict → QueryException
- Invalid field name → QueryException
- Empty result set → empty list
- Aggregate modes: SUM, MIN, MAX
- min_score filtering
- post_filter callback
- Partition filter integration

### 3. Validate implementation
- **Task ID**: validate-composite-score
- **Depends On**: build-tests
- **Assigned To**: composite-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify all success criteria met
- Check temp key cleanup (no leaked keys after queries)
- Review error messages for invalid inputs

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-composite-score
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/agent-memory.md` CompositeScoreQuery section
- Add docstrings with examples
- Update API reference if applicable

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: composite-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all tests
- Verify documentation builds
- Verify all success criteria met
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Composite score tests pass | `pytest tests/test_composite_score_query.py -v` | exit code 0 |
| Import works | `python -c "from popoto.models.query import QueryBuilder; assert hasattr(QueryBuilder, 'composite_score')"` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |

---

## Open Questions

1. **Decay materialization scope**: When materializing DecayingSortedField scores for ZUNIONSTORE, should the Lua script compute decay for ALL members in the sorted set, or only for PKs in the current filter set? Computing all is simpler but slower for large partitions. Computing only filtered PKs requires passing the filter set into Lua.

2. **AccessTracker score semantics**: Should the access_count (integer) or last_accessed (timestamp) be used as the sorted set score for AccessTracker materialization? `access_count` measures frequency, `last_accessed` measures recency. The roadmap mentions "access frequency" but both are useful. Consider supporting both via a parameter or defaulting to `access_count`.

3. **Weight validation**: Should we validate that weights sum to 1.0, or allow arbitrary positive floats? ZUNIONSTORE accepts arbitrary weights. Requiring sum-to-1.0 is more intuitive but unnecessarily restrictive. Leaning toward allowing arbitrary positive floats with documentation noting that relative ratios matter, not absolute values.
