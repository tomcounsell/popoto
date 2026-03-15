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

Increase the weight of an existing edge via atomic ZINCRBY.

```python
new_weight = field.strengthen(Memory, pk_a, pk_b, delta=0.1)
# Returns the new weight after increment
```

- Raises `ValueError` if `delta <= 0`

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

```python
scores = field.propagate(Memory, [pk_a], depth=2, decay_per_hop=0.5)
# Returns: {"pk_b": 0.4, "pk_c": 0.08, ...}
```

- When same PK reached via multiple paths, uses `max(weight)`
- `depth=0` returns seeds only with weight 1.0
- Seeds are excluded from results (except depth=0)

## Edge Pruning

When `max_edges` is exceeded during `link()`, the lowest-weight edges are atomically pruned using a Lua script. This prevents unbounded memory growth.

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
