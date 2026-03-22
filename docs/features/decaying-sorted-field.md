# DecayingSortedField

A `SortedField` subclass where records lose retrieval weight over time following a power-law decay curve. This is the foundational primitive for agent memory — most subsequent primitives depend on time-weighted scoring.

## Overview

The sorted set score is always a timestamp. A Lua script computes decay-ranked results at query time:

```text
decayed_score = base_score * elapsed_days ^ (-decay_rate)
```

With the default `decay_rate=0.5`, a record scores 1.0 after 1 day, 0.5 after 4 days, and 0.1 after 100 days. All computation happens server-side in Lua — no round trips for ranking.

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `decay_rate` | `float` | `0.5` | Controls how fast scores drop. Higher = faster decay. Configurable via `Defaults.DECAY_RATE`. |
| `base_score_field` | `str` | `None` | Name of a companion field whose value multiplies the decay curve. When `None`, base score is 1.0. |
| `partition_by` | `str` or `tuple` | `()` | Partition the sorted set by key field values, inherited from `SortedField`. |

## Usage

### Basic Model Definition

```python
from popoto import Model, KeyField, Field
from popoto.fields import DecayingSortedField

class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField()
```

The `relevance` field automatically timestamps records on save. Query for the most relevant recent records:

```python
memories = Memory.query.filter(agent_id="agent-1").top_by_decay(10)
```

### Base Score Weighting

Point `base_score_field` at another field to weight records differently:

```python
class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    importance = Field(type=float, default=1.0)
    relevance = DecayingSortedField(base_score_field="importance")
```

A record with `importance=5.0` stays relevant 25x longer than one with `importance=1.0` (at decay_rate=0.5).

### Source Weighting with InteractionWeight

Use `InteractionWeight` constants for multi-agent teams with human oversight:

```python
from popoto.fields.constants import InteractionWeight

class TeamMemory(Model):
    agent_id = KeyField()
    importance = Field(type=float, default=InteractionWeight.AGENT)
    content = Field(type=str)
    relevance = DecayingSortedField(base_score_field="importance")

# CEO directive — stays relevant for years
TeamMemory(
    agent_id="pm-1",
    importance=InteractionWeight.combine(InteractionWeight.HUMAN, InteractionWeight.EXECUTIVE),
    content="We're pivoting to enterprise",
).save()
```

### Refreshing Timestamps

Call `touch()` to reset the decay clock without a full save:

```python
memory.touch("relevance")
```

### Query-Time Overrides

Override `decay_rate` per query for different retrieval contexts:

```python
# Aggressive decay — only very recent records
hot = Memory.query.filter(agent_id="agent-1").top_by_decay(5, decay_rate=1.0)
```

## Tuning

The `decay_rate` default is configurable via `Defaults`:

```python
from popoto.fields.constants import Defaults

Defaults.DECAY_RATE = 0.3  # Slower decay globally
```

Explicit kwargs always override `Defaults`:

```python
# This field uses 0.7 regardless of Defaults.DECAY_RATE
fast_decay = DecayingSortedField(decay_rate=0.7)
```

## Architecture

- **Redis key pattern**: `ClassName:_field_name` (sorted set with timestamp scores)
- **Lua script**: Computes `base_score * elapsed_days^(-decay_rate)` server-side, reads base scores from model hash via cmsgpack
- **Inheritance**: Extends `SortedFieldMixin` + `Field`

## See Also

- [CyclicDecayField](cyclic-decay-field.md) — adds cyclical resonance and homeostatic pressure
- [Agent Memory overview](agent-memory.md) — full primitives reference
