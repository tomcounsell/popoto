# ConfidenceField

A `Field` subclass that tracks confidence metadata per member, updated atomically via Lua script using a capped-evidence Bayesian rule.

## Overview

`ConfidenceField` maintains a confidence score for each record, allowing the system to track how certain it should be about a given piece of information. The update rule is a **capped-evidence Bayesian** (also called "forgetful Bayesian"): within an evidence window it is an exact running mean — order-invariant and prior-weighted; beyond the cap it is bounded exponential forgetting at a constant rate.

The field stores its metadata in a companion Redis hash:

- `{confidence, evidence_count, corroborations, contradictions}`

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `initial_confidence` | float | 0.5 | Starting confidence for new members (0-1). Acts as one prior pseudo-observation in the running mean. |
| `evidence_cap` | int | 20 | Maximum effective evidence count. Within this window updates are a running mean; beyond it updates apply a fixed gain of `1/(cap+1)`, producing bounded exponential forgetting. Must be an integer ≥ 1. |
| `partition_by` | str or tuple | `()` | Field name(s) to partition the companion hash by. Splits the single Redis hash into per-partition hashes for efficient reads. |

> **Warning — same cap across processes**: The `evidence_cap` is passed per-call and is NOT stored in the companion hash. All processes updating the same companion hash entry must be configured with identical `evidence_cap` values. Divergent caps produce silently inconsistent gain schedules with no runtime detection.

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

## Update Formula

The update rule uses a capped effective-evidence count:

```
n_eff = min(evidence_count + 1, cap)
new_confidence = confidence + (signal - confidence) / (n_eff + 1)
```

The `+1` in `evidence_count + 1` is an inlined prior pseudo-count — `initial_confidence` behaves as one real observation. Results are clamped to `[0, 1]`.

### Running-Mean Regime (evidence_count < cap)

While the real evidence count is below the cap, the formula is an exact running mean over `{prior, s1, s2, …, sn}`:

```
confidence after n updates = (initial_confidence + s1 + s2 + … + sn) / (n + 1)
```

This is order-invariant up to ~1e-12 (IEEE-754 intermediate rounding; the closed form is the oracle, not bit-identical output). The first update from `initial_confidence=0.5` with `signal=0.9` yields **0.7**, not 0.9 — the prior is never erased.

### Forgetting Regime (evidence_count >= cap)

Once the evidence count reaches the cap, the effective gain is fixed at `1/(cap+1)`. With `cap=20` (default), each update multiplies `(confidence - signal)` by `20/21 ≈ 0.952`.

**Worked example**: a belief with `confidence=0.9` and `evidence_count=20` (at the cap) subjected to 15 consecutive contradictions at `signal=0.1`:

```
After k contradictions: confidence = 0.1 + 0.8 × (20/21)^k
Crossing 0.5 requires:  0.8 × (20/21)^k < 0.4
                        k > ln(0.4/0.8) / ln(20/21) ≈ 14.2 → 15 contradictions
```

This is bounded exponential forgetting: well-established beliefs take roughly 15 systematic contradictions to cross the midpoint at the default cap.

### Update Behavior Summary

| Regime | Condition | Behavior |
|--------|-----------|----------|
| Running mean | evidence_count < cap (20) | Exact mean of {prior, all signals}; order-invariant to ~1e-12 |
| Fixed forgetting | evidence_count >= cap | Constant gain `1/(cap+1)`; ~15 contradictions cross 0.5 from 0.9 |

## Entrainment with ObservationProtocol

When used with `ObservationProtocol`, confidence is automatically updated based on how the agent uses retrieved memories:

| Outcome | Effect on Confidence |
|---------|---------------------|
| `acted` | Corroborate (signal=0.9) |
| `dismissed` | No change |
| `deferred` | No change |
| `contradicted` | Contradict (signal=0.1) |

### Auto-discharge

When confidence drops strictly below `AUTO_DISCHARGE_CONFIDENCE_THRESHOLD` (0.1), homeostatic pressure on any `CyclicDecayField` is automatically resolved (discharged). This prevents low-confidence memories from building urgency.

The comparison uses an epsilon guard: discharge fires only when `conf < 0.1 - 1e-9`. A confidence value that rounds to exactly 0.1 in display but is stored as `0.09999999999999998` (a common IEEE-754 artifact on the first contradiction from a default prior) does **not** trigger discharge. A genuine drop — one clearly below the threshold by more than epsilon — still does.

## API Reference

### `ConfidenceField.update_confidence(instance, field_name, signal, pipeline=None)`

Atomically update confidence using the capped-evidence Bayesian formula.

- **signal**: Float 0-1. Values >= 0.5 corroborate, < 0.5 contradict.
- **pipeline**: Optional Redis pipeline to queue the update on.
- **Returns**: The new confidence value — or `None` when queued on a pipeline.
- **Raises**: `TypeError` if unsaved or wrong field type; `ValueError` if signal out of range.

**Pipeline mode (1.9.0)** changes three things, because the result does not
exist until `execute()`:

- the return value is `None` rather than the new confidence;
- the instance attribute is **not** synced;
- an unsaved instance is skipped server-side by the script's `EXISTS` guard
  instead of raising `TypeError` — the round trip that raised is what the
  batching removes.

Before 1.9.0 the `pipeline` argument was accepted and ignored: every update
ran immediately, one round trip each. Competitive suppression across N
records is now one `execute()` (`inject_context` went from 25 round trips per
turn to 8). If you passed `pipeline=` before and relied on the returned
float, read the value back with `get_confidence` after `execute()`.

> **Warning**: All processes calling `update_confidence` on the same companion hash entry must use identical `evidence_cap` values. The cap is not stored in Redis and divergent values produce silently inconsistent update trajectories.

### `ConfidenceField.get_confidence(instance, field_name)`

Read the current confidence value.

- **Returns**: Float confidence value, or `initial_confidence` if no data exists.

### `ConfidenceField.get_confidence_data(instance, field_name)`

Read all confidence metadata.

- **Returns**: Dict with keys `confidence`, `evidence_count`, `corroborations`, `contradictions`.

Note: `evidence_count` reflects real observations only — the prior pseudo-count is internal to the Lua update and does not inflate this counter.

### Inspecting Companion Hash Keys

Each `ConfidenceField` stores its confidence metadata in a companion Redis hash alongside
the main model hash. The public companion key methods let you build these Redis keys
for debugging, monitoring, or direct Redis inspection without reverse-engineering
suffix conventions.

```python
import popoto
from popoto import Model, UniqueKeyField, StringField
from popoto.fields.confidence_field import ConfidenceField

class Memory(Model):
    key = UniqueKeyField()
    content = StringField()
    certainty = ConfidenceField(initial_confidence=0.5)

# Create and update a memory
memory = Memory.create(key="fact1", content="The sky is blue")
ConfidenceField.update_confidence(memory, "certainty", signal=0.9)

# Get the companion hash key for direct Redis inspection
field = Memory._options.fields["certainty"]
hash_key = field.get_data_hash_key(memory, "certainty")
print(hash_key)
# => "$ConfidencF:Memory:certainty:data"

# Inspect the raw companion hash in Redis
r = popoto.get_redis()
raw_data = r.hgetall(hash_key)
print(raw_data)
# Shows all members and their msgpack-encoded confidence metadata
```

`popoto.get_redis()` returns the connection Popoto is actually bound to. Building a
client by hand — `redis.from_url(...)` — opens a *different* connection, and unless its
URL names the same database, `hgetall` reads a database the write never touched and
quietly returns `{}` with nothing to indicate why. A URL with no `/database` component
names database 0, which is rarely where your data is.

When you do not have an instance loaded, use `get_data_hash_key_from_values` to build
the key from explicit values:

```python
# Build the key without loading a model instance
key = field.get_data_hash_key_from_values(Memory, "certainty")
# => "$ConfidencF:Memory:certainty:data"

# For partitioned fields, pass the partition values as keyword arguments
# key = field.get_data_hash_key_from_values(Memory, "certainty", project="atlas")
```

## Partitioned Reads

When the companion hash grows large (thousands of members), reads become expensive because
`HGETALL` loads every entry. The `partition_by` parameter splits the hash by one or more
field values, so each read only touches the relevant partition.

```python
class Memory(Model):
    project = KeyField(type=str)
    key = UniqueKeyField()
    content = StringField()
    certainty = ConfidenceField(initial_confidence=0.5, partition_by='project')
```

All read and write operations automatically resolve the correct partition hash from the
model instance. Queries on partitioned ConfidenceFields must include the partition field
value(s), or a `QueryException` is raised.

See [Multi-Tenancy: Hash-based field partitioning](../multi-tenancy.md#hash-based-field-partitioning-confidencefield)
for the full pattern including migration from unpartitioned data.

### HSCAN Filtered Reads

For unpartitioned hashes, `get_confidence_filtered()` uses HSCAN with MATCH to iterate
without loading all entries into memory:

```python
results = ConfidenceField.get_confidence_filtered(Memory, "certainty", pattern="Memory:atlas:*")
# Returns: {member_key: {confidence, evidence_count, ...}}
```

### Migration Helper

```python
# Dry run — see what would happen
report = ConfidenceField.migrate_to_partitioned(Memory, "certainty", dry_run=True)

# Execute migration
report = ConfidenceField.migrate_to_partitioned(Memory, "certainty")
```

## Confidence Feeds Forgetting

Confidence used to be a read-only signal for ranking and promotion. It now closes the loop: the
evidence a record accumulates changes how fast it is forgotten.

**Faster decay.** [`DecayingSortedField`](decaying-sorted-field.md#confidence-modulated-decay) and
[`CyclicDecayField`](cyclic-decay-field.md#confidence-modulated-decay) read each record's confidence
inside their ranking Lua and derive a per-record effective decay rate,
`eff = decay_rate * 2 ^ (s * 2 * (c0 - c))`. The zero point is this field's own
`initial_confidence`, so a record with no evidence is bit-exactly neutral for any configured `c0`.
When a model has exactly one `ConfidenceField`, this is on with no configuration.

**Eligibility for forgetting.** [`MemoryLifecycle`](../recipes.md#memorylifecycle) reads confidence
on both sides of the ledger: it promotes records to the protected semantic tier, and it also makes
weakly-evidenced records eligible for removal. Its forget rule:

```text
(importance < FORGET_IMPORTANCE_FLOOR
 OR (confidence < FORGET_CONFIDENCE_CEILING AND evidence_count >= FORGET_MIN_EVIDENCE))
AND idle > FORGET_IDLE_SECONDS
```

`evidence_count` is load-bearing. Confidence starts at `initial_confidence` and moves on *every*
signal, so without a minimum track record one unlucky dismissal could bury a memory. Records that
cross the rule are **tombstoned**, not deleted — reversible via `restore()`.

`ConfidenceField` remains the single source of truth for evidence. Nothing about modulation or
forgetting is persisted back onto the record: the decay path is a pure reader of this field's
`:data` hash, so there is no second copy of derived state to desync.

## Redis Key Patterns

| Key | Type | Description |
|-----|------|-------------|
| `$ConfidencF:{Model}:{field}:data` | HASH | Unpartitioned: all members' confidence metadata |
| `$ConfidencF:{Model}:{field}:data:{partition_value}` | HASH | Partitioned: members in one partition |

## Working Example: Popoto Kitchen

The [Popoto Kitchen example app](https://github.com/tomcounsell/popoto/tree/main/examples)
includes a `ReviewScore` model that demonstrates `ConfidenceField` with
`partition_by="restaurant"`. Run the operations demo to see capped-evidence updates,
companion hash key inspection, and partitioned confidence in action:

```bash
cd examples
uv run popoto-kitchen --seed-only --clear
uv run popoto-kitchen --ops
```

See [`examples/popoto_kitchen/operations.py`](https://github.com/tomcounsell/popoto/blob/main/examples/popoto_kitchen/operations.py)
for the full source.

## Companion Fields

`ConfidenceField` works alongside other memory system fields:

- **DecayingSortedField**: Composite scoring via `priority = decay_score * confidence`, and
  [confidence-modulated decay](decaying-sorted-field.md#confidence-modulated-decay) — confidence sets
  each record's effective decay *rate*, not just its magnitude
- **CyclicDecayField**: Auto-discharge when confidence drops below threshold; inherits confidence
  modulation (confidence hash bound at `KEYS[4]` in its forked Lua)
- **MemoryLifecycle**: Confidence gates promotion *and* forgetting; low-confidence idle records with
  enough evidence are tombstoned
- **WriteFilterMixin**: Use confidence in `compute_filter_score()` for directed forgetting
- **[ContextAssembler](context-assembler.md#confidence-gate)**: `get_confidence()` on the rank-0 pull-path candidate drives the opt-in confidence gate — `assemble()` can refuse or flag a low-confidence answer instead of injecting it
- **AccessTrackerMixin**: Read tracking independent of confidence
