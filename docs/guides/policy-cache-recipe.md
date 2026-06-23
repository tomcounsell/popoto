# PolicyCache Recipe

> **New to Agent Memory?** Start with the [Quickstart Guide](agent-memory-quickstart.md) for a progressive adoption path.

A reference recipe composing all shipped Popoto memory primitives into an RL-style action selection cache. Agents accumulate state-action-outcome events; a crystallization handler detects repeated successful patterns and creates PolicyEntry records. Agents query policies for action selection, and outcomes update Q-values via temporal difference learning.

## Quick Start

```python
from decimal import Decimal

from popoto.recipes.policy_cache import (
    PolicyEntry,
    compute_fingerprint,
    update_q_value,
    crystallization_handler,
    temporal_discovery_handler,
)

# Create a policy entry with an initial Q-value
fp = compute_fingerprint({"task": "deploy", "env": "staging"})
policy = PolicyEntry(
    agent_id="agent-1",
    state_fingerprint=fp,
    state_features={"task": "deploy", "env": "staging"},
    action_type="run_playbook",
    action_spec={"playbook": "deploy.yml"},
    q_value=Decimal("0.5"),  # seed initial Q-value at construction
)
policy.save()  # persists both model fields and q_value in one round-trip

# Update Q-value after observing a reward
td_error = update_q_value(policy, reward=1.0)
```

## Architecture

PolicyEntry composes these primitives:

| Primitive | Role in PolicyCache |
|-----------|-------------------|
| `AutoKeyField` | Unique entry ID |
| `KeyField` | Agent partitioning, state fingerprinting, action type |
| `DecimalField` (`q_value`) | Learned Q-value stored in the model hash |
| `DecayingSortedField` (`expected_value`) | Pure recency clock; uses `q_value` as base magnitude via `base_score_field` |
| `ConfidenceField` | Capped-evidence confidence from outcome history |
| `CoOccurrenceField` | Weighted graph between related policies |
| `ExistenceFilter` | Bloom filter for fast state lookup |
| `EventStreamMixin` | Mutation log via Redis Streams |
| `AccessTrackerMixin` | Read pattern tracking |
| `PredictionLedgerMixin` | Outcome prediction and resolution |

## Crystallization

The `crystallization_handler` is an async function designed for use with `StreamConsumer`. It:

1. Groups incoming events by `(state_fingerprint, action_type)`
2. Counts successes and failures
3. Computes Wilson CI lower bound for conservative success rate estimation
4. Creates a PolicyEntry when evidence exceeds thresholds:
   - Minimum events: `MIN_EVENTS_FOR_CRYSTALLIZATION` (default: 3)
   - Wilson CI lower bound > `WILSON_CI_THRESHOLD` (default: 0.6)
5. Uses ExistenceFilter (Bloom filter) to skip likely-duplicate entries

```python
from popoto.streams import StreamConsumer

consumer = StreamConsumer(
    stream_key="stream:policy_mutations",
    group_name="crystallizer",
    consumer_name="worker-1",
    handler=crystallization_handler,
)
```

## Temporal Discovery

The `temporal_discovery_handler` identifies cyclical patterns in event timestamps:

- **Day of week** (7 buckets) — weekly patterns
- **Week of month** (4 buckets) — monthly patterns
- **Month of year** (12 buckets) — yearly patterns

Uses chi-squared test against uniform distribution. Significant clusters (p < 0.05) are returned as `(period, amplitude, phase)` tuples suitable for `CyclicDecayField`.

## Q-Value Updates

The `update_q_value` function performs atomic TD(0) updates via Lua script:

```
Q(s,a) <- Q(s,a) + alpha * [reward + gamma * max_Q(s',a') - Q(s,a)]
```

- **alpha** (learning rate): How much new information overrides old (default: 0.1)
- **gamma** (discount factor): Importance of future rewards (default: 0.95)
- Returns TD error (positive = better than expected)

### Storage architecture

Q-values live in two separate slots that never overwrite each other:

- **`q_value` (model hash)** — the learned value. Updated by `update_q_value()` via `HGET`/`HSET` on the model hash key. A `save()` or `touch()` on the instance does not reset it.
- **`expected_value` (sorted set)** — a pure recency/decay clock. Its score is the decay-weighted access timestamp multiplied by the `q_value` magnitude. Writing a new TD estimate via `update_q_value()` does not disturb this clock.

This separation means `Q(s,a)` survives every access pattern — `save()`, `touch()`, and `"acted"` outcome resolution — intact.

### Negative Q-values

`DecayingSortedField` includes a sign-preserving guard in its decay Lua script: negative Q-values retain their sign through the decay calculation. Policies that have been penalized remain penalized after aging.

## Tuning Constants

All numeric constants have been validated via parameter sweep ([tuning guide](tuning-magic-numbers.md)):

| Constant | Default | Purpose |
|----------|---------|---------|
| `MIN_EVENTS_FOR_CRYSTALLIZATION` | 3 | Minimum events before crystallization |
| `WILSON_CI_THRESHOLD` | 0.6 | Required Wilson CI lower bound |
| `TD_ALPHA` | 0.1 | Q-value learning rate |
| `TD_GAMMA` | 0.95 | Q-value discount factor |
| `CHI_SQUARED_P_THRESHOLD` | 0.05 | Temporal pattern significance |
| `INITIAL_CYCLE_AMPLITUDE` | 0.5 | Starting amplitude for discovered cycles |

## Design Decisions

- **WriteFilterMixin excluded**: The crystallization handler IS the write gate. Dual gating makes debugging harder.
- **Recipe, not core**: Lives in `popoto.recipes` to demonstrate composition without coupling to the ORM core.
- **Bloom filter false positives**: ~1% of legitimate crystallizations may be skipped due to ExistenceFilter's error rate. Acceptable for reference use; production systems needing zero misses should add a secondary check.
