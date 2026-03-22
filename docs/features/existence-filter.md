# ExistenceFilter and FrequencySketch

Probabilistic data structures for O(1) membership checks and approximate frequency counting — implemented as Lua-backed Redis operations.

## Overview

Two complementary primitives for fast pre-filtering:

- **ExistenceFilter** — Bloom filter for "do I know anything about X?" checks. False positives possible, false negatives impossible.
- **FrequencySketch** — Count-Min Sketch for approximate frequency counting. Overestimates possible, underestimates impossible.

Both operate entirely in Redis via Lua scripts, requiring no client-side state.

## ExistenceFilter

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `expected_items` | `int` | `10000` | Expected number of unique items |
| `false_positive_rate` | `float` | `0.01` | Target false positive rate |

### Usage

```python
from popoto import Model, KeyField, Field
from popoto.fields import ExistenceFilter

class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    topic_filter = ExistenceFilter(expected_items=10000, false_positive_rate=0.01)
```

Check membership before expensive queries:

```python
# O(1) check — avoids expensive query if topic is unknown
if ExistenceFilter.might_contain(Memory, "topic_filter", "deployment"):
    # Topic exists (or false positive) — proceed with full query
    results = Memory.query.filter(agent_id="agent-1").top_by_decay(10)
else:
    # Definitely not present — skip query entirely
    results = []
```

Add items to the filter:

```python
ExistenceFilter.add(Memory, "topic_filter", "deployment")
```

### Architecture

- **Redis key pattern**: `$EF:{ClassName}:{field_name}` (string used as bit array)
- **Lua script**: Computes k hash positions, sets/checks bits atomically
- **Size**: Automatically computed from `expected_items` and `false_positive_rate` using optimal Bloom filter formulas

## FrequencySketch

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `width` | `int` | `1024` | Number of counters per hash function |
| `depth` | `int` | `4` | Number of hash functions |

### Usage

```python
from popoto.fields.existence_filter import FrequencySketch

class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    access_frequency = FrequencySketch(width=1024, depth=4)
```

Count and query frequencies:

```python
# Increment count for an item
FrequencySketch.increment(Memory, "access_frequency", "deployment-topic")

# Get approximate count (may overestimate, never underestimates)
count = FrequencySketch.estimate(Memory, "access_frequency", "deployment-topic")
```

### Architecture

- **Redis key pattern**: `$FS:{ClassName}:{field_name}` (hash with counter rows)
- **Lua script**: Computes d hash positions across rows, increments/queries counters atomically
- **Estimate**: Returns minimum across all rows (Count-Min property)

## When to Use Which

| Use Case | Primitive |
|----------|-----------|
| "Have I seen this topic before?" | ExistenceFilter |
| "How many times has this topic appeared?" | FrequencySketch |
| Pre-filter before expensive CompositeScoreQuery | ExistenceFilter |
| Frequency-based write filtering | FrequencySketch |

## See Also

- [Agent Memory overview](agent-memory.md) — full primitives reference
- [ContextAssembler](context-assembler.md) — uses ExistenceFilter for pull-path pre-checks
