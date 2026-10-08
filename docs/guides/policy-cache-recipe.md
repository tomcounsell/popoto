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
| `TDValueField` (`q_value`) | Learned Q-value stored with the record (a hash field on Redis, a column on Postgres); owns the atomic TD(0) update |
| `DecayingSortedField` (`expected_value`) | Pure recency clock; uses `q_value` as base magnitude via `base_score_field` |
| `ConfidenceField` | Capped-evidence confidence from outcome history |
| `CoOccurrenceField` | Weighted graph between related policies |
| `ExistenceFilter` | Fast state lookup (a Bloom filter on Redis, an exact token table on Postgres) |
| `EventStreamMixin` | Mutation log (Redis Streams on Redis, the events tables on Postgres) |
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
5. Uses ExistenceFilter (a Bloom filter on Redis) to skip likely-duplicate entries

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

- **Day of week** (7 equal-width buckets) — weekly patterns

Only weekly discovery remains. The previous `week_of_month` (4 buckets) and `month_of_year` (12 buckets) configs were removed because calendar periods (month, year) have variable-length buckets — a fixed seconds-period constant (the `TemporalPeriod.MONTHLY`/`YEARLY` constants) cannot represent them, and the equal-width uniform chi-squared null was biased, fabricating cycles from noise (97-98% false-positive rate at n=400). `day_of_week` has 7 equal-width buckets so the uniform expectation `E_i = n/7` holds.

Uses the chi-squared test against the uniform distribution. Significant weekly clusters (p < 0.05) are returned as `(period, amplitude, phase)` tuples suitable for `CyclicDecayField`. The `phase` is emitted in **seconds** — a midpoint offset of the peak weekday from the Thursday weekly epoch anchor (1970-01-01 00:00 UTC, which was a Thursday) — matching the `CyclicDecayField` cycle-tuple contract that documents `phase` as a time offset in seconds.

Calendar-accurate monthly or yearly discovery (e.g. via variable-length bucketing or a per-bucket-vector chi-squared statistic) is a possible future feature; no such helper ships today.

## Q-Value Updates

`TDValueField` owns the TD(0) update. It is a `DecimalField` subclass that
stores the learned value exactly as a plain `DecimalField` does (in the model
hash on Redis), and adds one operation the ordinary save path cannot express —
an atomic read-modify-write of that single field:

```
Q(s,a) <- Q(s,a) + alpha * [reward + gamma * max_Q(s',a') - Q(s,a)]
```

- **alpha** (learning rate): How much new information overrides old (default: 0.1)
- **gamma** (discount factor): Importance of future rewards (default: 0.95)
- Returns TD error (positive = better than expected)

On Redis the read and the write happen inside one Lua script (on Postgres, one
`UPDATE`), so concurrent updates to
the same entry serialize at the server instead of racing through a client-side
`HGET` / compute / `HSET` round trip.

`update_q_value()` is a convenience wrapper over
`TDValueField.td_update(entry, "q_value", ...)` — it keeps the recipe's
signature and defaults, and the field owns the script. Use the recipe function
when you are working with `PolicyEntry`; reach for the field directly when you
want TD updates on a model of your own. See
[TDValueField](../features/td-value-field.md).

### Storage architecture

Q-values live in two separate slots that never overwrite each other (described here as they are on Redis; on Postgres both are columns of the record's row):

- **`q_value` (model hash)** — the learned value. Updated by `update_q_value()` through the TD script on the model hash key. A `save()` or `touch()` on the instance does not reset it.
- **`expected_value` (sorted set)** — a pure recency/decay clock. Its score is the decay-weighted access timestamp multiplied by the `q_value` magnitude. Writing a new TD estimate via `update_q_value()` does not disturb this clock.

This separation means `Q(s,a)` survives every access pattern — `save()`, `touch()`, and `"acted"` outcome resolution — intact.

### Negative Q-values

`DecayingSortedField` includes a sign-preserving guard in its decay Lua script (and the Postgres `rank_decayed` statement keeps the same sign rule): negative Q-values retain their sign through the decay calculation. Policies that have been penalized remain penalized after aging.

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
- **Bloom filter false positives** (Redis only): ~1% of legitimate crystallizations may be skipped due to ExistenceFilter's error rate; on Postgres the filter is exact. Acceptable for reference use; production systems needing zero misses should add a secondary check.

## On Postgres

The recipe runs unchanged on a Postgres-bound `PolicyEntry`: `td_update` is
one `UPDATE` behind the record's key lock, the clock is the `expected_value`
column, and the mutation log is appended to the backend's events tables in
the save's own transaction, where `StreamConsumer` reads it. The existence
pre-check is exact there, so the Bloom false-positive skip above does not
happen, and a reload after `touch()` sees the touched clock (both rows of
[Documented divergences](../features/postgres-backend.md#records-and-other-behaviour)).
See [PolicyCache](../features/policy-cache.md#on-postgres),
[Long-tail fields](../features/postgres-backend.md#long-tail-fields-m5) and
[Event streams and pub/sub](../features/postgres-backend.md#event-streams-and-pubsub-m5).
