# TDValueField

A `DecimalField` subclass that adds one operation the ordinary save path cannot express: an **atomic temporal-difference update** of a single hash field, performed server-side in one step (a Lua script on Redis, one `UPDATE` on Postgres).

## Overview

`TDValueField` stores a learned value — a Q-value, in reinforcement-learning terms — in the model hash exactly as a plain `DecimalField` does. Nothing lives outside the hash. What it adds is `td_update()`, which applies the TD(0) rule

```
Q(s,a) <- Q(s,a) + alpha * [reward + gamma * max Q(s',a') - Q(s,a)]
```

as a single atomic read-modify-write. The read and the write happen inside the same script, so two processes updating the same instance serialize at the server instead of racing through a client-side `HGET` / compute / `HSET` round trip, where the later writer silently discards the earlier one's update.

The field was extracted from `popoto.recipes.policy_cache`, which owned the script directly (issue #647, part of the #630 series). A recipe composes primitives; it should not own one. `update_q_value()` remains as a convenience wrapper with its original signature — see [PolicyCache](policy-cache.md).

## Parameters

None. `TDValueField` takes no constructor arguments beyond what `DecimalField` accepts, and declares no storage of its own.

The learning rate and discount factor are **call-time arguments** of `td_update()`, not field-level configuration. That is deliberate: they are experimental tuning constants (see `popoto.fields.constants.Defaults`), and a field-declared default would let two processes disagree about the gain schedule with no runtime detection — the same hazard `ConfidenceField`'s `evidence_cap` carries.

!!! warning "The field must be named `q_value`"
    The hash field name is hard-coded inside the Lua script. Parameterizing it would change the script text, hence its SHA, hence the on-wire `EVALSHA` payload. `td_update()` therefore refuses any other field name with a `ValueError` rather than silently updating the wrong column.

## Usage

```python
from decimal import Decimal
from popoto import Model, KeyField, TDValueField

class Bandit(Model):
    agent_id = KeyField()
    q_value = TDValueField(default=Decimal("0"))

arm = Bandit(agent_id="agent-1")
arm.save()

# One TD(0) step. Returns the TD error.
td_error = TDValueField.td_update(arm, "q_value", reward=1.0, max_future_q=0.6)
# => positive when the outcome beat the current estimate

# The in-memory attribute is NOT refreshed — reload to observe the new value.
arm = Bandit.query.get(agent_id="agent-1")
```

### Arguments

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `model_instance` | Model | — | A **saved** instance carrying a `TDValueField`. |
| `field_name` | str | — | Must be `"q_value"`. |
| `reward` | float | — | Observed reward signal. |
| `max_future_q` | float | `0.0` | Best value available in the next state. `0.0` means terminal — no future state. |
| `alpha` | float | `Defaults.TD_ALPHA` (0.1) | Learning rate: how much new information overrides old. |
| `gamma` | float | `Defaults.TD_GAMMA` (0.95) | Discount factor: importance of future rewards. |
| `pipeline` | Pipeline | `None` | Queue the update on a pipeline. Returns `None`; the TD error appears in that pipeline's results after `execute()`. |

Raises `ValueError` if the instance is unsaved, or if `field_name` is not a `TDValueField` named `q_value`. The guard issues **no** Redis command before raising.

## The in-memory attribute is not refreshed

`td_update()` returns the TD error, not the new value, and does not write back to the Python object. This is intentional: recomputing the new value client-side would duplicate the arithmetic and the encoding this field exists to own, and re-reading it would add a command to a wire sequence that is otherwise exactly one script call. **Reload the instance to observe the new value.**

## Storage and encoding

On Redis, the value is written as the `__Decimal__` tagged dict that every `DecimalField` uses, encoded with `cmsgpack` inside the script so it is byte-interchangeable with what Python's msgpack encoder produces.

This matters beyond tidiness. `DecayingSortedField` reads a `base_score_field` straight out of the member's hash in its own Lua and falls back to `1.0` for any encoding it does not recognize. A `PolicyEntry` declares `expected_value = DecayingSortedField(base_score_field="q_value")`, so if `TDValueField` wrote any other encoding, the decay clock would silently fall back to a magnitude of 1.0 rather than error. Keeping `TDValueField` a `DecimalField` subclass — `type is Decimal` — is what keeps that contract.

## On Postgres

On a Postgres-bound model (#759 M5) the field is a `numeric` column and
`td_update` is one `UPDATE` with the script's arithmetic in the same order,
storing the script's `tostring` of the new value and replying its `tostring`
of the TD error. A `DecayingSortedField` that names the field as its
`base_score_field` reads the column. See
[Long-tail fields](postgres-backend.md#long-tail-fields-m5).

Differences, from
[Documented divergences](postgres-backend.md#records-and-other-behaviour):

- `td_update(…, pipeline=uow)` with a Postgres `transaction()` or
  `popoto.batch()` runs inside it and returns the TD error; a Redis pipeline
  queues the script and returns `None`.
- On a record that no longer exists (or has expired) nothing is written, and
  the reply is computed from `Q = 0`; Redis writes an orphan hash entry.
- A NaN value is stored as `numeric` `NaN`, and a stored `Decimal('-0')`
  reads as `0` (the stored values compare equal).

## Valkey compatibility

Core commands only — `HGET`, `HSET`, and `cmsgpack` inside the script. No Redis-module commands (`BF.`, `CMS.`, `TOPK.`, `TS.`, `JSON.`), so this field behaves identically on Redis and Valkey.

## See also

- [PolicyCache](policy-cache.md) — the recipe that composes this field, and the `update_q_value()` wrapper.
- [DecayingSortedField](decaying-sorted-field.md) — reads `q_value` as a `base_score_field`.
- [ConfidenceField](confidence-field.md) — the sibling primitive whose per-call tuning argument follows the same rule.
