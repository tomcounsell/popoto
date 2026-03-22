# PredictionLedgerMixin

Outcome tracking — record predictions before acting, resolve them against actual outcomes, and feed prediction errors back into ConfidenceField.

## Overview

`PredictionLedgerMixin` provides a mixin for recording predictions and resolving them. High prediction errors reduce trust in bad knowledge via ConfidenceField feedback. Auto-resolution via ObservationProtocol handles the common case where outcomes are inferred from behavior.

## Parameters (Class Attributes)

| Attribute | Default | Defaults Key | Description |
|-----------|---------|-------------|-------------|
| `_pl_partition` | `"default"` | — | Partition key for the error sorted set |
| `_pl_confidence_error_threshold` | `0.7` | `PL_CONFIDENCE_ERROR_THRESHOLD` | Error above which confidence is reduced |
| `_pl_confidence_low_signal` | `0.2` | `PL_CONFIDENCE_LOW_SIGNAL` | Signal sent to ConfidenceField when error exceeds threshold |
| `_pl_auto_resolve_errors` | `{"acted": 0.1, "dismissed": 0.5, "contradicted": 0.9}` | `PL_AUTO_RESOLVE_ACTED`, `PL_AUTO_RESOLVE_DISMISSED`, `PL_AUTO_RESOLVE_CONTRADICTED` | Prediction error values for auto-resolution |

All thresholds and error values are configurable via `Defaults`:

```python
from popoto.fields.constants import Defaults

Defaults.PL_CONFIDENCE_ERROR_THRESHOLD = 0.8
Defaults.PL_AUTO_RESOLVE_ACTED = 0.05
```

## Usage

### Model Definition

```python
from popoto import Model, UniqueKeyField, Field
from popoto.fields.prediction_ledger import PredictionLedgerMixin
from popoto.fields.confidence_field import ConfidenceField

class Memory(PredictionLedgerMixin, Model):
    key = UniqueKeyField()
    content = Field(type=str)
    certainty = ConfidenceField()

    _pl_partition = "default"
```

### Recording and Resolving Predictions

```python
memory = Memory.create(key="fact1", content="sky is blue")

# Record a prediction before acting
PredictionLedgerMixin.record_prediction(memory, predicted={"relevance": 0.9})

# Later, resolve against actual outcome
PredictionLedgerMixin.resolve_prediction(memory, actual={"relevance": 0.3})
# prediction_error = |0.9 - 0.3| / max(0.9, 0.3, 1) = 0.6
# Since 0.6 < 0.7 (threshold), confidence is NOT reduced
```

### Auto-Resolution via ObservationProtocol

When `ObservationProtocol.on_context_used()` fires with an outcome, predictions are auto-resolved:

- `"acted"` -> error = 0.1 (low error, prediction was roughly right)
- `"dismissed"` -> error = 0.5 (moderate error)
- `"contradicted"` -> error = 0.9 (high error, confidence reduced)

### Querying Prediction Errors

```python
# Get records with highest prediction errors
errors = PredictionLedgerMixin.get_top_errors(Memory, partition="default", n=10)
```

## Prediction Error Computation

For numeric values: `|predicted - actual| / max(|predicted|, |actual|, 1)`

For string values: `0.0` if equal, `1.0` if different.

Missing keys contribute `1.0` error per key.

## Architecture

- **Meta hash**: `$PL:{ClassName}:meta:{pk}` — msgpack prediction metadata per instance
- **Error sorted set**: `$PL:{ClassName}:errors:{partition}` — PKs scored by |error|
- **Lua script**: Atomic resolution — read, update, ZADD error in one operation
- **ConfidenceField feedback**: When error exceeds `_pl_confidence_error_threshold`, sends `_pl_confidence_low_signal` to reduce trust

## See Also

- [ConfidenceField](confidence-field.md) — Bayesian certainty tracking (receives error feedback)
- [ObservationProtocol](observation-protocol.md) — auto-resolution via outcome hooks
- [Agent Memory overview](agent-memory.md) — full primitives reference
