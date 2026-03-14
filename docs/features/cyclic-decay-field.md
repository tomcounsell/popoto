# CyclicDecayField

A `DecayingSortedField` subclass that adds cyclical resonance and homeostatic pressure to time-weighted scoring.

## Overview

`CyclicDecayField` extends `DecayingSortedField` with two additional temporal forces computed atomically in a single Lua script:

1. **Cyclical resonance**: Periodic boosts following cosine curves. A record about Q1 renewals can resurface every January.
2. **Homeostatic pressure**: Urgency that builds linearly the longer an item goes unresolved. Discharged by calling `resolve_pressure()`.

The effective score is: `decay + cyclic_resonance + pressure`

When `cycles=[]` and `pressure_rate=0.0`, behavior is identical to `DecayingSortedField`.

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `decay_rate` | float | 0.5 | Power-law decay exponent (inherited) |
| `base_score_field` | str | None | Companion field whose value multiplies the decay curve (inherited) |
| `cycles` | list | `[]` | List of `(period, amplitude, phase)` tuples |
| `pressure_rate` | float | 0.0 | Rate of urgency buildup per unresolved day |
| `partition_by` | str/tuple | `()` | Partition sorted set by key fields (inherited) |

### Cycle Tuples

Each cycle is a `(period, amplitude, phase)` tuple:

- **period**: Duration in seconds. Use `TemporalPeriod` constants.
- **amplitude**: Peak boost value (non-negative).
- **phase**: Time offset in seconds (shifts the cosine curve).

The resonance formula: `amplitude * cos(2 * pi * (now - phase) / period)`

### TemporalPeriod Constants

Import from `popoto.fields.constants`:

| Constant | Value (seconds) |
|----------|----------------|
| `DAILY` | 86,400 |
| `WEEKLY` | 604,800 |
| `MONTHLY` | 2,592,000 |
| `QUARTERLY` | 7,776,000 |
| `YEARLY` | 31,536,000 |

## Usage

### Basic Model Definition

```python
from popoto import Model, KeyField, Field, CyclicDecayField
from popoto.fields.constants import TemporalPeriod

class Directive(Model):
    agent_id = KeyField()
    content = Field(type=str)
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.QUARTERLY, 5.0, 0)],
        pressure_rate=0.1,
    )
```

### Querying Top Results

```python
# Top 10 directives by combined decay + cyclic + pressure score
top = Directive.query.filter(agent_id="agent-1").top_by_decay("relevance", n=10)
```

### Resolving Pressure

```python
# Discharge accumulated urgency for a directive
directive.resolve_pressure("relevance")
```

### Adjusting Cycle Amplitudes

Use `strengthen_cycle()` and `weaken_cycle()` to dynamically adjust how strongly cycles influence a record's score. Both methods multiply all cycle amplitudes by a factor, with clamping to `[0.0, 100.0]`. Amplitudes below `0.01` snap to zero (effectively killing the cycle).

```python
# Strengthen: multiply all cycle amplitudes by 1.5x
directive.strengthen_cycle("relevance", factor=1.5)

# Weaken: multiply all cycle amplitudes by 0.6x
directive.weaken_cycle("relevance", factor=0.6)
```

These methods are used internally by [ObservationProtocol](agent-memory.md#observationprotocol) to adjust cycles based on agent behavior outcomes:

- **acted** outcome calls `strengthen_cycle(factor=1.2)` — reinforcing cycles that led to useful memories
- **dismissed** outcome calls `weaken_cycle(factor=0.8)` — dampening cycles for rejected memories
- **contradicted** outcome calls `weaken_cycle(factor=0.5)` — aggressively dampening contradicted memories

You can also call them directly for custom cycle management outside the ObservationProtocol.

### Refreshing the Decay Clock

```python
# Same as DecayingSortedField — updates the timestamp
directive.touch("relevance")
```

## Redis Data Model

CyclicDecayField stores data in three Redis structures:

1. **Sorted set** (inherited): `$CyclicDecayF:{Model}:{field}:{partitions}` — member timestamps
2. **Cycles hash**: `$CyclicDecayF:{Model}:{field}:{partitions}:cycles` — per-member cycle tuples (msgpack)
3. **Pressure hash**: `$CyclicDecayF:{Model}:{field}:{partitions}:pressure` — per-member `{rate, last_resolved}` (msgpack)

All three structures are maintained automatically by `on_save()` and `on_delete()`.

## Scoring Formula

The extended Lua script computes per member:

```
elapsed_days = max((now - last_updated) / 86400, 0.01)
decay = base_score * elapsed_days ^ (-decay_rate)
cyclic = sum(amplitude * cos(2 * pi * (now - phase) / period) for each cycle)
pressure = pressure_rate * max((now - last_resolved) / 86400, 0)
effective_score = decay + cyclic + pressure
```

When companion hashes return nil (no cycle/pressure data), the overhead is two nil HGET lookups per member.

## Error Handling

- `CyclicDecayField(cycles=[(0, 1.0, 0)])` raises `ModelException` (zero period)
- `CyclicDecayField(pressure_rate=-1)` raises `ModelException` (negative rate)
- `resolve_pressure()` on unsaved model raises `TypeError`
- `resolve_pressure()` on non-CyclicDecayField raises `TypeError`
- `resolve_pressure()` with `pressure_rate=0` raises `TypeError`
- `strengthen_cycle()` / `weaken_cycle()` on non-CyclicDecayField raises `TypeError`
- `strengthen_cycle()` / `weaken_cycle()` on unsaved model raises `TypeError`

## Integration with ObservationProtocol

When used with [ObservationProtocol](agent-memory.md#observationprotocol), cycle amplitudes are adjusted automatically based on how the agent responds to surfaced memories. See [Agent Memory — Four outcomes](agent-memory.md#four-outcomes) for the full effects table.
