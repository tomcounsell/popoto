# PolicyCache

A capstone recipe composing all shipped Popoto memory primitives into an RL-style action selection cache. Agents accumulate state-action-outcome events; a StreamConsumer crystallization handler detects repeated successful patterns and creates PolicyEntry records.

## Overview

PolicyCache implements learned action selection:

1. **Events arrive** via EventStreamMixin — state-action-outcome triples.
2. **Crystallization** detects repeated successful patterns (via StreamConsumer handler).
3. **PolicyEntry** records are created composing DecayingSortedField, ConfidenceField, CoOccurrenceField, ExistenceFilter, and PredictionLedgerMixin.
4. **Retrieval** via CompositeScoreQuery ranks policies by multi-factor scores.
5. **Q-value updates** via temporal difference learning refine policy values over time.

## Components

### PolicyEntry Model

Composes all 12 shipped primitives:

```python
from popoto.recipes.policy_cache import PolicyEntry, compute_fingerprint

policy = PolicyEntry(
    agent_id="agent-1",
    state_fingerprint=compute_fingerprint({"task": "deploy", "env": "staging"}),
    state_features={"task": "deploy", "env": "staging"},
    action_type="run_playbook",
    action_spec={"playbook": "deploy.yml"},
    q_value=Decimal("0.5"),  # optional: seed initial Q-value at construction
)
policy.save()
```

#### Q-value and recency clock: two separate storage slots

`PolicyEntry` separates learned value from access recency:

| Field | Storage | Role |
|-------|---------|------|
| `q_value` | Model hash (`DecimalField`) | Learned Q-value; updated by TD(0) |
| `expected_value` | Sorted set (score) | Pure recency clock; decays with time |

`expected_value` is a `DecayingSortedField(partition_by="agent_id", base_score_field="q_value")`. The sorted-set score is the decay-weighted access timestamp; `q_value` supplies the base magnitude. Because the two slots are independent, a `save()`, `touch()`, or `"acted"` outcome that refreshes the decay clock does not overwrite the Q-value, and a TD update to `q_value` does not alter the recency clock.

### Crystallization Handler

Automatic pattern detection via StreamConsumer:

```python
from popoto.recipes.policy_cache import crystallization_handler
from popoto.streams.consumer import StreamConsumer

consumer = StreamConsumer(
    stream_key="stream:policy_mutations",
    group_name="crystallizer",
    consumer_name="worker-1",
    handler=crystallization_handler,
)
```

The handler counts events with the same `(state_fingerprint, action_type)`. When the count exceeds `MIN_EVENTS_FOR_CRYSTALLIZATION` and the Wilson CI lower bound exceeds `WILSON_CI_THRESHOLD`, a `PolicyEntry` is crystallized. The Wilson CI lower bound is passed as `q_value` at construction — a single `save()` persists both the model fields and the initial Q-value. No separate `initialize_q_value` call is needed.

### Q-Value Updates

Temporal difference learning for policy refinement:

```python
from popoto.recipes.policy_cache import update_q_value

# After observing reward from taking an action
update_q_value(policy, reward=0.8, next_max_q=0.6)
```

The update uses: `Q_new = Q_old + alpha * (reward + gamma * next_max_q - Q_old)`

### Temporal Discovery

Detects cyclical patterns in event timing:

```python
from popoto.recipes.policy_cache import temporal_discovery_handler
```

Uses chi-squared tests to detect non-uniform temporal distributions, then creates CyclicDecayField cycles for discovered patterns.

## Tuning Constants

All constants configurable via `Defaults`:

```python
from popoto.fields.constants import Defaults
```

| Constant | Default | Optimal Range | Description |
|----------|---------|---------------|-------------|
| `MIN_EVENTS_FOR_CRYSTALLIZATION` | 3 | [1, 10] | Minimum events before crystallization |
| `WILSON_CI_THRESHOLD` | 0.6 | [0.3, 0.8] | Wilson CI lower bound for crystallization |
| `TD_ALPHA` | 0.1 | [0.01, 0.5] | Q-value learning rate |
| `TD_GAMMA` | 0.95 | [0.8, 0.99) | Q-value discount factor |
| `CHI_SQUARED_P_THRESHOLD` | 0.05 | — | p-value threshold for temporal discovery |
| `INITIAL_CYCLE_AMPLITUDE` | 0.5 | — | Initial amplitude for discovered cycles |

## Utility Functions

- `compute_fingerprint(features)` — stable hash from feature dicts (sorted JSON + SHA-256)
- `wilson_ci_lower(successes, total)` — Wilson score confidence interval lower bound
- `chi_squared_uniform(observed)` — chi-squared test against uniform distribution

## See Also

- [ContextAssembler](context-assembler.md) — retrieval-to-injection bridge
- [Policy Cache Recipe Guide](../guides/policy-cache-recipe.md) — detailed architecture guide
- [Agent Memory overview](agent-memory.md) — full primitives reference
