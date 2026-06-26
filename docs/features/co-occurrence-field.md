# CoOccurrenceField

A `Field` subclass that maintains weighted association edges between model instances using Redis sorted sets, with BFS graph propagation for multi-hop associative retrieval.

## Overview

`CoOccurrenceField` provides an ORM-level primitive for weighted, decaying edges between model instances. Each instance gets its own Redis sorted set storing edges to other instances with weights. Weights strengthen via `strengthen()` and decay via `weaken_all()`.

The field supports:
- **Symmetric mode** (default): edges are bidirectional
- **Asymmetric mode**: edges are unidirectional
- **Edge pruning**: automatic removal of lowest-weight edges when `max_edges` is exceeded
- **BFS propagation**: server-side Lua script traverses edges across hops with exponential decay

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `symmetric` | bool | `True` | If True, edges are bidirectional |
| `max_edges` | int | `500` | Maximum edges per PK; lowest-weight edges pruned when exceeded |
| `decay_factor` | float | `0.95` | Default multiplicative decay factor for `weaken_all()` |
| `CO_OCCURRENCE_WEIGHT_CAP` | float | `1.0` | Upper bound on stored edge weights (experimental tuning constant in `Defaults`, not user config). Clamped on every `strengthen()` write and applied as a read-time `min(edge_weight, cap)` in the propagation BFS. Chosen with headroom below `1 / decay_per_hop = 2.0` so that `cap * decay_per_hop < 1` guarantees per-hop contraction during propagation. |

## Redis Key Pattern

Each PK gets its own sorted set:

```
$CoOcF:{ClassName}:{field_name}:{pk}  ->  ZSET { target_pk: weight, ... }
```

Example:
```
$CoOcF:Memory:associations:Memory:pk_abc  ->  ZSET { Memory:pk_def: 0.3, Memory:pk_ghi: 0.15 }
$CoOcF:Memory:associations:Memory:pk_def  ->  ZSET { Memory:pk_abc: 0.3 }  # symmetric mirror
```

## Usage

```python
from popoto import Model, UniqueKeyField, StringField
from popoto.fields.co_occurrence_field import CoOccurrenceField

class Memory(Model):
    key = UniqueKeyField()
    content = StringField()
    associations = CoOccurrenceField(symmetric=True, max_edges=100)

# Create instances
mem_a = Memory.create(key="concept_a", content="Machine learning")
mem_b = Memory.create(key="concept_b", content="Neural networks")
mem_c = Memory.create(key="concept_c", content="Deep learning")

pk_a = mem_a.db_key.redis_key
pk_b = mem_b.db_key.redis_key
pk_c = mem_c.db_key.redis_key

# Access the field instance
field = Memory._meta.fields["associations"]
```

## Methods

### `link(model_class, source_pk, target_pk, initial_weight=0.1, pipeline=None)`

Create a weighted edge between two PKs. If symmetric, creates edges in both directions.

```python
field.link(Memory, pk_a, pk_b, initial_weight=0.2)
```

- Raises `ValueError` if `source_pk == target_pk` (no self-loops)
- Idempotent: linking an already-linked pair keeps the original weight

### `strengthen(model_class, source_pk, target_pk, delta=0.05, pipeline=None)`

Increase the weight of an existing edge via atomic Lua script.

Weights are clamped at `CO_OCCURRENCE_WEIGHT_CAP` (default 1.0) to guarantee per-hop contraction during propagation. The clamp is applied atomically via a Lua script (`STRENGTHEN_CLAMP_LUA`) that reads the current weight, adds delta, and writes back `min(old + delta, cap)` in a single `EVAL` — preventing concurrent `strengthen()` races on the same edge.

```python
new_weight = field.strengthen(Memory, pk_a, pk_b, delta=0.1)
# Returns the new weight after increment and clamp
```

- Raises `ValueError` if `delta <= 0`
- Returned weight is the clamped value, never exceeding `CO_OCCURRENCE_WEIGHT_CAP`

### `unlink(model_class, source_pk, target_pk, pipeline=None)`

Remove an edge. If symmetric, removes both directions.

```python
field.unlink(Memory, pk_a, pk_b)
```

### `weaken_all(model_class, pk, factor=None, pipeline=None)`

Multiplicatively decay all edge weights for a PK. Edges below threshold (0.001) are pruned.

```python
removed_count = field.weaken_all(Memory, pk_a, factor=0.9)
```

- `factor=0` removes all edges
- Raises `ValueError` if `factor > 1` or `factor < 0`

### `get_linked(model_class, pk, min_weight=0.01, limit=20)`

Get linked PKs sorted by weight descending.

```python
linked = field.get_linked(Memory, pk_a, min_weight=0.05, limit=10)
# Returns: [("target_pk_1", 0.8), ("target_pk_2", 0.3), ...]
```

### `propagate(model_class, seed_pks, depth=2, decay_per_hop=0.5, threshold=0.01)`

BFS graph propagation with exponential weight decay per hop. Uses a server-side Lua script for efficiency.

Per-hop transfer is always <= `decay_per_hop` because edge weights are clamped. The BFS threshold reliably terminates the walk. A runtime guard raises `ValueError` if `cap * decay_per_hop >= 1.0`. A read-time `min(edge_weight, cap)` in the Lua BFS handles pre-existing over-cap weights as defense-in-depth.

```python
scores = field.propagate(Memory, [pk_a], depth=2, decay_per_hop=0.5)
# Returns: {"pk_b": 0.4, "pk_c": 0.08, ...}
```

- When same PK reached via multiple paths, uses `max(weight)`
- `depth=0` returns seeds only with weight 1.0
- Seeds are excluded from results (except depth=0)
- Raises `ValueError` if `CO_OCCURRENCE_WEIGHT_CAP * decay_per_hop >= 1.0` (contraction invariant violated; would amplify instead of decay)

## Edge Pruning

When `max_edges` is exceeded during `link()`, the lowest-weight edges are atomically pruned using a Lua script. This prevents unbounded memory growth.

## Contraction Guarantee

`propagate()` implements spreading activation: at each BFS hop, activation is multiplied by `decay_per_hop * effective_edge_weight`. For propagation to decay (rather than amplify) with hop count, the per-hop transfer factor must be strictly less than 1:

```
decay_per_hop * effective_edge_weight < 1
```

`CO_OCCURRENCE_WEIGHT_CAP` enforces this invariant by bounding `effective_edge_weight <= cap`. The cap is chosen with intentional headroom below the theoretical maximum `1 / decay_per_hop`:

- Default `decay_per_hop = 0.5` -> theoretical max cap = `1 / 0.5 = 2.0`
- Actual `CO_OCCURRENCE_WEIGHT_CAP = 1.0` (2x headroom)

At default config, `cap * decay_per_hop = 1.0 * 0.5 = 0.5 < 1`, so each hop contracts activation by at least half. The headroom means small upward adjustments to `decay_per_hop` do not silently violate the invariant.

A runtime guard in `propagate()` enforces `cap * decay_per_hop < 1` at call time and raises `ValueError` if the inequality fails. This guard fires on misconfiguration (e.g. a future constants sweep raises `decay_per_hop` above `1 / cap` without a matching cap adjustment), not under current defaults. It is the runtime backstop for the invariant the cap value is designed to satisfy.

The contraction invariant holds for any reachable stored weight, not just newly-written ones, because the Lua BFS applies `min(edge_weight, cap)` at read time as defense-in-depth.

## Known Limitation: Neighbor Selection Bias

The propagation BFS selects neighbors via `ZREVRANGE`, which ranks by the raw stored ZSET score. For pre-existing over-cap weights (edges written before the clamp was introduced, or edges above the cap from a previous lower cap value), the raw stored score may exceed `CO_OCCURRENCE_WEIGHT_CAP`.

This biases *which* neighbors are visited when `max_per_node` truncation fires: an over-cap edge with stored score 2.5 ranks above a clamped edge at 1.0, even though both have the same effective weight (1.0) after the read-time `min()`. The contraction invariant is not violated — the read-time clamp guarantees `effective_weight <= cap` regardless of the stored score — but the *selection* of which neighbors to expand is distorted by the un-clamped scores.

This limitation:

- **Does not violate the contraction invariant** — `min(edge_weight, cap)` is applied before computing propagated weight, so per-hop transfer stays bounded.
- **Self-corrects as over-cap edges are lazily clamped** on the next `strengthen()` call (since `min(over_cap + delta, cap) = cap`).
- **Only affects ranking when `max_per_node` truncation fires** — if all neighbors fit within the `max_per_node` budget, no truncation occurs and the bias is invisible.

The fix path, if the bias materializes as a ranking distortion in practice, is a one-time migration that clamps all existing ZSET scores in-place via a `ZADD XX` overwrite with `min(score, cap)`. This would normalize stored scores to their effective values and eliminate the ranking distortion. A follow-up issue should be filed if the bias is observed in production graphs.

## Cleanup on Delete

When a model instance is deleted, the `on_delete` hook:
1. Removes the instance's own edge sorted set
2. If symmetric, removes reverse edges from all connected PKs

## Synergy with Other Fields

### With DecayingSortedField
Propagated weights can identify related records for boosting retrieval scores:

```python
scores = field.propagate(Memory, [pk_a], depth=2)
# Use scores to boost DecayingSortedField retrieval ranking
```

### With AccessTrackerMixin
Co-accessed records can be linked to build association graphs:

```python
# After detecting co-access
field.link(Memory, pk_a, pk_b, initial_weight=0.1)
field.strengthen(Memory, pk_a, pk_b, delta=0.05)
```

### With ConfidenceField
Confidence values can modulate effective edge weights:

```python
confidence = ConfidenceField.get_confidence(instance, "certainty")
linked = field.get_linked(Memory, pk)
effective_weights = [(pk, w * confidence) for pk, w in linked]
```
