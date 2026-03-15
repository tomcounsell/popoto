# ConfidenceField

A `Field` subclass that tracks Bayesian confidence metadata per member, updated atomically via Lua script.

## Overview

`ConfidenceField` maintains a confidence score for each record, allowing the system to track how certain it should be about a given piece of information. Precision grows with `sqrt(n)`, so early evidence has outsized effect while established beliefs resist change.

The field stores its metadata in a companion Redis hash:

- `{confidence, evidence_count, corroborations, contradictions}`

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `initial_confidence` | float | 0.5 | Starting confidence for new members (0-1) |

## Usage

```python
from popoto import Model, UniqueKeyField, StringField
from popoto.fields.confidence_field import ConfidenceField

class Memory(Model):
    key = UniqueKeyField()
    content = StringField()
    certainty = ConfidenceField(initial_confidence=0.5)

# Create a memory
memory = Memory.create(key="fact1", content="The sky is blue")

# Corroborate (signal >= 0.5 increases confidence)
ConfidenceField.update_confidence(memory, "certainty", signal=0.9)

# Contradict (signal < 0.5 decreases confidence)
ConfidenceField.update_confidence(memory, "certainty", signal=0.1)

# Read current confidence
confidence = ConfidenceField.get_confidence(memory, "certainty")

# Read all metadata
data = ConfidenceField.get_confidence_data(memory, "certainty")
# Returns: {confidence: 0.5, evidence_count: 2, corroborations: 1, contradictions: 1}
```

## Bayesian Update Formula

```
new_confidence = prior + (signal - prior) / sqrt(evidence_count + 1)
```

- **Early updates** have large effect (small evidence_count, small denominator)
- **Later updates** have diminishing effect (large evidence_count, large denominator)
- Results are clamped to `[0, 1]`

### Convergence Behavior

| Updates | Denominator | Movement per update |
|---------|-------------|---------------------|
| 1st | sqrt(1) = 1.0 | Full step |
| 4th | sqrt(4) = 2.0 | Half step |
| 9th | sqrt(9) = 3.0 | Third step |
| 100th | sqrt(100) = 10.0 | Tenth step |

## Entrainment with ObservationProtocol

When used with `ObservationProtocol`, confidence is automatically updated based on how the agent uses retrieved memories:

| Outcome | Effect on Confidence |
|---------|---------------------|
| `acted` | Corroborate (signal=0.9) |
| `dismissed` | No change |
| `deferred` | No change |
| `contradicted` | Contradict (signal=0.1) |

### Auto-discharge

When confidence drops below 0.1 due to a `contradicted` outcome, homeostatic pressure on any `CyclicDecayField` is automatically resolved (discharged). This prevents low-confidence memories from building urgency.

## API Reference

### `ConfidenceField.update_confidence(instance, field_name, signal)`

Atomically update confidence using the Bayesian formula.

- **signal**: Float 0-1. Values >= 0.5 corroborate, < 0.5 contradict.
- **Returns**: The new confidence value.
- **Raises**: `TypeError` if unsaved or wrong field type; `ValueError` if signal out of range.

### `ConfidenceField.get_confidence(instance, field_name)`

Read the current confidence value.

- **Returns**: Float confidence value, or `initial_confidence` if no data exists.

### `ConfidenceField.get_confidence_data(instance, field_name)`

Read all confidence metadata.

- **Returns**: Dict with keys `confidence`, `evidence_count`, `corroborations`, `contradictions`.

## Redis Key Patterns

| Key | Type | Description |
|-----|------|-------------|
| `$ConfidencF:{Model}:{field}:data` | HASH | Member -> msgpack confidence metadata |

## Companion Fields

`ConfidenceField` works alongside other memory system fields:

- **DecayingSortedField**: Composite scoring via `priority = decay_score * confidence`
- **CyclicDecayField**: Auto-discharge when confidence drops below threshold
- **WriteFilterMixin**: Use confidence in `compute_filter_score()` for directed forgetting
- **AccessTrackerMixin**: Read tracking independent of confidence
