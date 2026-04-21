# ObservationProtocol

Outcome-driven memory effects — the application layer reports how the agent used retrieved memories, and the ORM applies effects atomically.

## Overview

`ObservationProtocol` provides three lifecycle hooks for passive behavioral inference on memory models:

- **`on_read(instance)`** — fired when a query hydrates an instance. Delegates to `AccessTrackerMixin`.
- **`on_surfaced(instances, reason)`** — fired when a proactive system pushes memories into agent context. Creates `RecallProposal` entries.
- **`on_context_used(instances, outcome_map)`** — fired when the application reports how the agent responded. Applies effects based on outcome.

## Outcomes

Five outcomes drive different effects:

| Outcome | Confidence | Cycles | Pressure | Access | Predictions |
|---------|-----------|--------|----------|--------|-------------|
| `acted` | Corroborate (signal=0.9) | Strengthen (factor=1.2) | Resolve | Confirm | Auto-resolve (error=0.1) |
| `dismissed` | — | Weaken (factor=0.8) | — | Discard | Auto-resolve (error=0.5) |
| `deferred` | — | — | Keeps building | Discard | — |
| `contradicted` | Contradict (signal=0.1) | Aggressively weaken (factor=0.5) | Auto-discharge if confidence < 0.1 | Discard | Auto-resolve (error=0.9) |
| `used` | — | — | — | **Confirm** | Auto-resolve (error=0.3) |

### `"used"` vs `"deferred"`

`"used"` and `"deferred"` are the two outcomes that do not emit a strength signal, but they are observably different:

- **`"deferred"`** — agent set the memory aside without reading it. Staged reads are discarded (no confirmed-read trace). Pending predictions are left unresolved.
- **`"used"`** — agent read and reasoned over the memory but did not cite it in the response. Staged reads are **confirmed** via `AccessTrackerMixin.confirm_access()`. Predictions are auto-resolved with a moderate error (0.3). No confidence, cycle, or decay signal is emitted.

Use `"used"` when the memory informed the agent's reasoning without appearing directly in the output — a common case that `"acted"` overcounts and `"deferred"` undercounts.

```python
outcome_map = {
    memory1.db_key.redis_key: "acted",    # appeared in response
    memory2.db_key.redis_key: "used",     # informed reasoning, not cited
    memory3.db_key.redis_key: "dismissed",
    # memory4 not in map → defaults to "deferred"
}
ObservationProtocol.on_context_used(memories, outcome_map)
```

See [Metacognitive Layer](metacognitive-layer.md) for the full effects comparison table.

## Usage

```python
from popoto.fields.observation import ObservationProtocol

# After agent processes memories:
outcome_map = {
    memory1.db_key.redis_key: "acted",
    memory2.db_key.redis_key: "dismissed",
    memory3.db_key.redis_key: "contradicted",
}
ObservationProtocol.on_context_used(memories, outcome_map)
```

Instances not in the outcome_map default to `"deferred"`.

### Proactive Surfacing

When a proactive system pushes memories into agent context:

```python
ObservationProtocol.on_surfaced(memories, reason="proactive")
```

This creates `RecallProposal` entries in a Redis sorted set for tracking. Proposals expire after 1 hour (configurable via `RecallProposal.DEFAULT_TTL`).

## Tuning Constants

All constants are configurable via `Defaults`:

```python
from popoto.fields.constants import Defaults

Defaults.ACTED_CONFIDENCE_SIGNAL = 0.9
Defaults.CONTRADICTED_CONFIDENCE_SIGNAL = 0.1
Defaults.ACTED_CYCLE_STRENGTHEN_FACTOR = 1.2
Defaults.DISMISSED_CYCLE_WEAKEN_FACTOR = 0.8
Defaults.CONTRADICTED_CYCLE_WEAKEN_FACTOR = 0.5
Defaults.AUTO_DISCHARGE_CONFIDENCE_THRESHOLD = 0.1
```

| Constant | Default | Optimal Range | Notes |
|----------|---------|---------------|-------|
| `ACTED_CONFIDENCE_SIGNAL` | 0.9 | [0.5, 1.0] | Insensitive within range |
| `CONTRADICTED_CONFIDENCE_SIGNAL` | 0.1 | [0.05, 0.3] | Insensitive within range |
| `ACTED_CYCLE_STRENGTHEN_FACTOR` | 1.2 | [1.0, 2.0] | CLIFF EFFECT below 1.0 |
| `DISMISSED_CYCLE_WEAKEN_FACTOR` | 0.8 | [0.3, 1.0] | Insensitive within range |
| `CONTRADICTED_CYCLE_WEAKEN_FACTOR` | 0.5 | [0.3, 0.8] | Insensitive within range |
| `AUTO_DISCHARGE_CONFIDENCE_THRESHOLD` | 0.1 | [0.05, 0.3] | Insensitive within range |

## Effects Matrix

When `on_context_used()` fires, effects are dispatched per-primitive:

- **DecayingSortedField**: `acted` calls `touch()` to refresh the decay clock.
- **AccessTrackerMixin**: `acted` calls `confirm_access()`; all others call `discard_staged_access()`.
- **CyclicDecayField**: `acted` strengthens cycles and resolves pressure; `dismissed` and `contradicted` weaken cycles.
- **ConfidenceField**: `acted` corroborates; `contradicted` contradicts.
- **PredictionLedgerMixin**: `acted`, `dismissed`, and `contradicted` auto-resolve pending predictions with appropriate error values.

## RecallProposal

Internal ORM infrastructure for tracking proactively surfaced memories.

- **Key pattern**: `$RP:{ClassName}:pending:{partition}` (sorted set scored by surfaced_at)
- **Lifecycle**: pending -> acted | dismissed | deferred | contradicted | expired
- **TTL**: 3600s (1 hour). Unresolved proposals are treated as deferred.

```python
from popoto.fields.observation import RecallProposal

# Get pending proposals
pending = RecallProposal.get_pending(Memory, partition="default")

# Expire stale proposals
expired = RecallProposal.expire_stale(Memory, ttl=3600)
```

## See Also

- [Metacognitive Layer](metacognitive-layer.md) — full `"used"` outcome documentation, `error_summary`, and `AdaptiveAssembler`
- [ConfidenceField](confidence-field.md) — Bayesian certainty tracking
- [CyclicDecayField](cyclic-decay-field.md) — cyclical resonance and pressure
- [PredictionLedger](prediction-ledger.md) — outcome tracking
- [Agent Memory overview](agent-memory.md) — full primitives reference
