---
status: Planning
type: feature
appetite: Large
owner: Solo dev
created: 2026-03-15
tracking: https://github.com/tomcounsell/popoto/issues/210
last_comment_id:
---

# CoOccurrenceField — Weighted Association Edges with Graph Propagation

## Problem

Popoto's memory primitives (Steps 1–4) give agents temporal awareness, cyclical recall, confidence tracking, and selective encoding. But there is no mechanism for **associative retrieval** — knowing that retrieving record A should boost the relevance of related records B and C.

**Current behavior:**
Application developers must manually track relationships between records using external data structures or custom Redis operations. There is no ORM-level primitive for weighted, decaying edges between model instances, and no way to do multi-hop graph propagation at query time.

**Desired outcome:**
A `CoOccurrenceField` that maintains weighted bidirectional (or unidirectional) edges between model instances using Redis sorted sets. Weights strengthen via co-retrieval and decay when not reinforced. BFS graph propagation with exponential weight decay per hop enables multi-hop associative retrieval entirely within Redis.

## Prior Art

- **Issue #193 / PR #199**: DecayingSortedField — Established the pattern for Lua-based server-side scoring on sorted sets. CoOccurrenceField will follow the same Lua script pattern for `propagate()`.
- **Issue #154 / PR #159**: SortedField ghost entry fix on partition key change — Relevant pattern for cleanup when edges are pruned.
- **PR #201**: CyclicDecayField — Demonstrates complex Lua script pattern with multiple parameters. CoOccurrenceField's propagate BFS will be similarly complex.
- **Issue #208 / PR #214**: WriteFilterMixin — Shows how to add a mixin that gates persistence behavior. CoOccurrenceField's `symmetric` mode is a similar behavioral modifier.

No prior attempts at co-occurrence or association fields exist in the repository.

## Data Flow

1. **Entry point**: Application calls `CoOccurrenceField.link(source_pk, target_pk)` or `strengthen(source_pk, target_pk)`
2. **Key resolution**: Field generates sorted set key `$CoOcF:{ClassName}:{field_name}:{pk}` using existing `get_special_use_field_db_key()` pattern
3. **Redis ZADD/ZINCRBY**: Score represents edge weight. If `symmetric=True`, a mirror operation runs on the target's sorted set
4. **Pruning**: If member count exceeds `max_edges`, ZREMRANGEBYRANK removes lowest-weight edges
5. **Query — get_linked()**: ZRANGEBYSCORE with min_weight filter returns associated PKs with weights
6. **Query — propagate()**: Lua BFS script traverses sorted sets across multiple hops, applying exponential decay per hop, returns aggregated weights
7. **Maintenance — weaken_all()**: Multiplies all scores for a PK's sorted set by `decay_factor` (< 1.0), prunes edges below threshold

## Architectural Impact

- **New dependencies**: None — uses only Redis sorted set commands already used by SortedFieldMixin
- **Interface changes**: New field class with its own API (`link`, `strengthen`, `weaken_all`, `get_linked`, `propagate`). Does not modify existing Field/SortedFieldMixin interfaces.
- **Coupling**: Low. CoOccurrenceField is self-contained. Synergy tests with Steps 1–4 are opt-in integration tests, not coupling.
- **Data ownership**: Each CoOccurrenceField instance owns its sorted sets. Keys are namespaced per field per model, no conflicts.
- **Reversibility**: Fully reversible — remove the field definition, and the sorted set keys become orphaned (cleanable via SCAN pattern match).

## Appetite

**Size:** Large

**Team:** Solo dev, PM

**Interactions:**
- PM check-ins: 1-2 (scope alignment on propagate() complexity, synergy test depth)
- Review rounds: 1-2 (code review for Lua script correctness, edge pruning logic)

Large because: Lua BFS script is non-trivial, symmetric mode doubles write complexity, and the synergy test matrix with Steps 1–4 is wide.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis running | `python -c "from popoto.redis_db import POPOTO_REDIS_DB; POPOTO_REDIS_DB.ping()"` | Redis connection |
| Steps 1-4 present | `python -c "from popoto.fields.access_tracker import AccessTrackerMixin; from popoto.fields.confidence_field import ConfidenceField; from popoto.fields.cyclic_decay_field import CyclicDecayField; from popoto.fields.observation import ObservationProtocol; from popoto.fields.write_filter import WriteFilterMixin"` | Dependency fields exist |

## Solution

### Key Elements

- **`CoOccurrenceField` class**: A new field type backed by per-PK Redis sorted sets storing weighted edges to other PKs
- **Symmetric mode**: When `symmetric=True`, every write operation is mirrored on both source and target sorted sets
- **Edge pruning**: Automatic removal of lowest-weight edges when `max_edges` is exceeded
- **Lua BFS propagation**: Server-side graph traversal script that walks edges across hops with exponential decay
- **Maintenance decay**: Multiplicative weakening of all edges for a given PK

### Flow

**Application** → `link(A, B)` → **ZADD** to `$CoOcF:Model:field:A` → (if symmetric) **ZADD** to `$CoOcF:Model:field:B` → **ZCARD** check → (if > max_edges) **ZREMRANGEBYRANK** prune

**Application** → `propagate([seed_pk], depth=2)` → **Lua script** reads seed's sorted set → BFS queue → depth 1 neighbors with `weight × decay` → depth 2 neighbors with `weight × decay²` → return aggregated weights dict

### Technical Approach

- **File**: `src/popoto/fields/co_occurrence_field.py` — new file, following `decaying_sorted_field.py` pattern
- **Key pattern**: `$CoOcF:{ClassName}:{field_name}:{pk}` — one sorted set per instance PK
  - Uses `get_special_use_field_db_key(model, field_name)` then appends `:{pk}` at runtime
- **Does NOT inherit SortedFieldMixin**: Unlike DecayingSortedField, CoOccurrenceField doesn't store per-instance scores in a global sorted set. It maintains per-PK edge sets. It inherits from `Field` directly and implements its own Redis operations.
- **Lua script for propagate()**: Single EVALSHA call. Takes seed PKs, max depth, decay_per_hop, threshold. Returns flat array of `[pk, weight, pk, weight, ...]`.
- **Pipeline support**: All methods accept optional `pipeline` parameter for atomic batching
- **on_delete hook**: When a model instance is deleted, remove its sorted set AND remove it from all other instances' sorted sets (scan pattern `$CoOcF:{ClassName}:{field_name}:*` and ZREM)

### Key Methods

```python
class CoOccurrenceField(Field):
    symmetric: bool = True
    max_edges: int = 500
    decay_factor: float = 0.95

    def link(self, model_class, source_pk, target_pk, initial_weight=0.1, pipeline=None)
    def strengthen(self, model_class, source_pk, target_pk, delta=0.05, pipeline=None)
    def weaken_all(self, model_class, pk, factor=None, pipeline=None)
    def get_linked(self, model_class, pk, min_weight=0.01, limit=20) -> list[tuple[str, float]]
    def propagate(self, model_class, seed_pks, depth=2, decay_per_hop=0.5, threshold=0.01) -> dict[str, float]
    def unlink(self, model_class, source_pk, target_pk, pipeline=None)

    # Hook: cleanup on model delete
    @classmethod
    def on_delete(cls, model_instance, field_name, field_value, pipeline=None, **kwargs)
```

### Redis Key Layout

```
$CoOcF:Memory:associations:pk_abc123  → ZSET { pk_def456: 0.3, pk_ghi789: 0.15, ... }
$CoOcF:Memory:associations:pk_def456  → ZSET { pk_abc123: 0.3, ... }  # symmetric mirror
```

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `link()` with non-existent PK: should succeed (edges can exist before instances, or survive instance deletion temporarily)
- [ ] `propagate()` with empty seed list: should return empty dict
- [ ] `propagate()` with seed PK that has no edges: should return empty dict
- [ ] `weaken_all()` on PK with no edges: should be a no-op
- [ ] `get_linked()` with min_weight > all edge weights: should return empty list

### Empty/Invalid Input Handling
- [ ] `link()` with source_pk == target_pk: should raise ValueError (no self-loops)
- [ ] `strengthen()` with negative delta: should raise ValueError
- [ ] `propagate()` with depth=0: should return just seed PKs with weight 1.0
- [ ] `weaken_all()` with factor=0: should remove all edges (multiply by 0)
- [ ] `weaken_all()` with factor > 1: should raise ValueError (must be 0 < factor < 1)

### Error State Rendering
- [ ] Not applicable — this is a backend ORM field, no user-visible rendering

## Rabbit Holes

- **Full graph database features**: Don't implement Dijkstra, shortest path, connected components, or any graph algorithm beyond BFS propagation. This is an association primitive, not Neo4j.
- **Automatic co-retrieval detection**: The issue mentions "weights strengthen when records are accessed together." Don't build the detection mechanism — that's application-layer logic using AccessTracker. CoOccurrenceField provides `strengthen()`, the app calls it.
- **Weighted propagation merging strategies**: Keep it simple — when a PK is reached via multiple paths, use max weight (not sum). Sum can cause weight explosion in dense graphs.
- **Async Lua scripts**: Don't try to make propagate() async or non-blocking. Redis Lua is atomic and single-threaded — embrace it.
- **Edge metadata**: Don't store anything beyond weight. No edge labels, timestamps, or properties. That's a different data structure.

## Risks

### Risk 1: Lua BFS script complexity
**Impact:** Bugs in the Lua script are hard to debug — no step debugger, limited logging.
**Mitigation:** Write the Lua script incrementally with dedicated unit tests at each stage. Test with known graph topologies (linear chain, star, complete graph) where expected propagation weights can be computed by hand.

### Risk 2: on_delete cleanup performance
**Impact:** Deleting a highly-connected instance requires scanning all CoOccurrenceField sorted sets to remove references — O(N) where N is total instances.
**Mitigation:** For v1, accept the O(N) scan. Document that `max_edges=500` keeps the practical fan-out bounded. Add a TODO for v2 reverse-index optimization if profiling shows this is a bottleneck.

### Risk 3: Symmetric write consistency
**Impact:** If one ZADD succeeds but the mirror fails (crash, network issue), edges become asymmetric.
**Mitigation:** Use Redis pipeline for both writes. Pipeline execution is atomic within a single Redis instance. Document that Redis Cluster splits would need MULTI/EXEC per shard (out of scope for v1).

## Race Conditions

### Race 1: Concurrent strengthen + weaken_all
**Location:** `co_occurrence_field.py` — `strengthen()` and `weaken_all()` methods
**Trigger:** Thread A calls `strengthen(pk_a, pk_b, delta=0.05)` while Thread B calls `weaken_all(pk_a, factor=0.95)`
**Data prerequisite:** Edge between pk_a and pk_b must exist
**State prerequisite:** Both operations target the same sorted set key
**Mitigation:** Both operations use atomic Redis commands (ZINCRBY, Lua ZADD with multiplication). Individual commands are atomic in Redis. The final state will reflect both operations applied in some order — which is acceptable for approximate weights.

### Race 2: Concurrent link + prune on same PK
**Location:** `co_occurrence_field.py` — `link()` with max_edges enforcement
**Trigger:** Two concurrent `link()` calls both check ZCARD, both see count at max_edges-1, both add, resulting in max_edges+1
**Data prerequisite:** Sorted set near max_edges capacity
**State prerequisite:** Both threads targeting same PK's sorted set
**Mitigation:** Use a Lua script for link() that atomically does ZADD + ZCARD + conditional ZREMRANGEBYRANK. This ensures the prune check and the add are in the same atomic Lua execution.

## No-Gos (Out of Scope)

- **Automatic co-retrieval detection** — Application layer calls strengthen(), this field doesn't observe access patterns
- **Graph algorithms beyond BFS** — No shortest path, no connected components, no PageRank
- **Edge metadata/labels** — Edges carry only weight (float), no type or timestamp
- **Cross-model edges** — v1 supports edges between instances of the same model class only
- **Redis Cluster support** — All keys for a model's CoOccurrenceField must be on the same Redis instance
- **Reverse index for fast on_delete** — Accept O(N) scan for v1

## Update System

No update system changes required — this is an ORM library (popoto), not the Valor AI deployment system. Consumers update via `pip install --upgrade popoto`.

## Agent Integration

No agent integration required — this is a standalone ORM field in the popoto library. The Valor AI agent system will consume it as a pip dependency when building memory models on top of popoto.

## Documentation

### Feature Documentation
- [ ] Create `docs/fields/co-occurrence-field.md` describing the field, key pattern, methods, and usage examples
- [ ] Add entry to project README under the Fields section
- [ ] Update `docs/references/popoto-memory-roadmap.md` to mark Step 5 as complete

### Inline Documentation
- [ ] Module docstring on `co_occurrence_field.py` following the established pattern (see `decaying_sorted_field.py`)
- [ ] Method docstrings on all public methods with Args, Returns, and Examples
- [ ] Lua script header comments explaining algorithm and KEYS/ARGV parameters

## Success Criteria

- [ ] `CoOccurrenceField` class exists in `src/popoto/fields/co_occurrence_field.py`
- [ ] `link()` creates weighted edges in Redis sorted sets
- [ ] `strengthen()` increments edge weight via ZINCRBY
- [ ] `symmetric=True` creates bidirectional edges (two sorted set entries)
- [ ] `symmetric=False` creates unidirectional edges
- [ ] `max_edges` cap works — lowest-weight edges pruned when exceeded
- [ ] `weaken_all()` multiplicatively decays all edges for a PK
- [ ] `get_linked()` returns `[(pk, weight), ...]` filtered by min_weight and limited
- [ ] `propagate()` performs BFS with exponential decay per hop via Lua script
- [ ] `unlink()` removes edges (both directions if symmetric)
- [ ] `on_delete()` cleans up edges when model instance is deleted
- [ ] Pipeline support on all write methods
- [ ] Self-loop prevention (source_pk == target_pk raises ValueError)
- [ ] Field registered in `src/popoto/fields/__init__.py` and importable from `popoto`
- [ ] Tests pass (`pytest tests/test_co_occurrence_field.py -x -q`)
- [ ] Lint clean (`python -m ruff check src/popoto/fields/co_occurrence_field.py`)
- [ ] Synergy tests with DecayingSortedField and AccessTracker

## Team Orchestration

### Team Members

- **Builder (co-occurrence-field)**
  - Name: field-builder
  - Role: Implement CoOccurrenceField class, Lua scripts, all methods
  - Agent Type: builder
  - Resume: true

- **Builder (tests)**
  - Name: test-builder
  - Role: Write comprehensive test suite including synergy tests
  - Agent Type: test-writer
  - Resume: true

- **Validator (implementation)**
  - Name: field-validator
  - Role: Verify field implementation, Redis key patterns, edge cases
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Create field documentation, update roadmap
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Implement CoOccurrenceField core
- **Task ID**: build-field
- **Depends On**: none
- **Assigned To**: field-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/fields/co_occurrence_field.py`
- Implement `__init__` with `symmetric`, `max_edges`, `decay_factor` params
- Implement `_get_edge_key(model_class, pk)` → returns Redis key `$CoOcF:{ClassName}:{field_name}:{pk}`
- Implement `link()` with ZADD, symmetric mirror, and atomic Lua prune
- Implement `strengthen()` with ZINCRBY and symmetric mirror
- Implement `unlink()` with ZREM and symmetric mirror
- Implement `weaken_all()` with Lua script (iterate ZRANGEBYSCORE, multiply scores, remove below threshold)
- Implement `get_linked()` with ZREVRANGEBYSCORE + WITHSCORES
- Implement `on_delete()` hook — remove own sorted set + ZREM from connected PKs
- Register in `src/popoto/fields/__init__.py`
- Ensure importable from top-level `popoto` package

### 2. Implement Lua BFS propagation script
- **Task ID**: build-propagate
- **Depends On**: build-field
- **Assigned To**: field-builder
- **Agent Type**: builder
- **Parallel**: false
- Write `PROPAGATE_BFS_LUA` script following `DECAY_SCORE_LUA` pattern
- Script takes: KEYS (key pattern prefix), ARGV (seed PKs as JSON, max_depth, decay_per_hop, threshold, max_edges_per_node)
- BFS queue: start from seeds with weight 1.0, traverse neighbors, multiply weight by decay_per_hop per hop
- When same PK reached via multiple paths: use max(weight) not sum
- Return flat array `[pk1, weight1, pk2, weight2, ...]`
- Implement `propagate()` Python method that calls EVALSHA and parses results
- Handle edge case: depth=0 returns seeds only

### 3. Write unit tests
- **Task ID**: build-tests
- **Depends On**: build-field
- **Assigned To**: test-builder
- **Agent Type**: test-writer
- **Parallel**: true (can start once build-field is done, parallel with build-propagate)
- Create `tests/test_co_occurrence_field.py`
- Test: `link()` creates edge with correct weight
- Test: `link()` symmetric creates bidirectional edges
- Test: `link()` asymmetric creates unidirectional edge only
- Test: `strengthen()` increases weight (check ZSCORE)
- Test: `strengthen()` symmetric mode strengthens both directions
- Test: `unlink()` removes edge, symmetric removes both
- Test: `weaken_all()` multiplies all scores by factor
- Test: `weaken_all()` prunes edges below implied threshold
- Test: `max_edges` cap trims lowest-weight edges after exceeding limit
- Test: `get_linked()` returns sorted by weight descending
- Test: `get_linked()` with min_weight filters correctly
- Test: `get_linked()` with limit caps results
- Test: self-loop prevention (source_pk == target_pk → ValueError)
- Test: `on_delete()` cleans up edges in both directions
- Test: pipeline support — pass pipeline, verify commands batched
- Test: `propagate()` linear chain (A→B→C) with depth=2
- Test: `propagate()` star graph (A→B, A→C, A→D) with depth=1
- Test: `propagate()` multi-path (A→B→D, A→C→D) uses max weight
- Test: `propagate()` depth=0 returns seeds only
- Test: `propagate()` respects threshold cutoff

### 4. Write synergy tests
- **Task ID**: build-synergy-tests
- **Depends On**: build-tests, build-propagate
- **Assigned To**: test-builder
- **Agent Type**: test-writer
- **Parallel**: false
- Test: CoOccurrenceField + DecayingSortedField — propagated weights boost retrieval scores
- Test: CoOccurrenceField + AccessTracker — co-accessed records can be linked via strengthen()
- Test: CoOccurrenceField + ConfidenceField — confidence modulates effective edge weight

### 5. Validate implementation
- **Task ID**: validate-field
- **Depends On**: build-propagate, build-synergy-tests
- **Assigned To**: field-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/test_co_occurrence_field.py -v`
- Verify Redis key patterns match specification
- Verify Lua script handles empty inputs gracefully
- Verify symmetric mode consistency
- Run lint: `python -m ruff check src/popoto/fields/co_occurrence_field.py`

### 6. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-field
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/fields/co-occurrence-field.md`
- Update README with CoOccurrenceField entry
- Update roadmap to mark Step 5 as shipped

### 7. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: field-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify all success criteria met
- Verify docs exist and are accurate
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_co_occurrence_field.py -x -q` | exit code 0 |
| Full suite passes | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check src/popoto/fields/co_occurrence_field.py` | exit code 0 |
| Format clean | `python -m ruff format --check src/popoto/fields/co_occurrence_field.py` | exit code 0 |
| Import works | `python -c "from popoto.fields.co_occurrence_field import CoOccurrenceField"` | exit code 0 |
| Field registered | `python -c "from popoto import CoOccurrenceField"` | exit code 0 |

---

## Open Questions

None — the issue specification is comprehensive and all dependencies (Steps 1–4) are implemented. The key design decisions (max weight aggregation for multi-path propagation, per-PK sorted sets, Lua-based BFS) follow established patterns in the codebase.
