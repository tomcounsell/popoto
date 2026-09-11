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
| `decay_rate` | float | 0.1 | Power-law decay exponent (inherited). |
| `base_score_field` | str | None | Companion field whose value multiplies the decay curve (inherited) |
| `confidence_modulation_field` | str / `False` / None | None | Which `ConfidenceField` modulates the per-record decay rate (inherited). See [Confidence-Modulated Decay](#confidence-modulated-decay). |
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
        decay_rate=0.5,  # override default (0.1) for faster forgetting
        cycles=[(TemporalPeriod.QUARTERLY, 5.0, 0)],
        pressure_rate=0.1,
    )
```

### Querying Top Results

```python
# Top 10 directives by combined decay + cyclic + pressure score
top = Directive.query.filter(agent_id="agent-1").top_by_decay(n=10)
```

!!! warning "`top_by_decay()` on a `CyclicDecayField` is not gated by `ValidityField`"
    `CYCLIC_DECAY_LUA` was deliberately left unmodified by the validity-gating work
    (issue #580), so a direct `top_by_decay()` call on a model with a `CyclicDecayField`
    can return a superseded record. This is a direct-caller gap only: `ContextAssembler`
    never calls `top_by_decay`, and its push path (`composite_score`) plus the assembler
    post-filter both gate cyclic results correctly. See
    [ValidityField and SupersessionProtocol](validity-and-supersession.md#known-limitations)
    for the full accounting.

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

These methods are used internally by [ObservationProtocol](observation-protocol.md) to adjust cycles based on agent behavior outcomes:

- **acted** outcome calls `strengthen_cycle(factor=1.2)` — reinforcing cycles that led to useful memories
- **dismissed** outcome calls `weaken_cycle(factor=0.8)` — dampening cycles for rejected memories
- **contradicted** outcome calls `weaken_cycle(factor=0.5)` — aggressively dampening contradicted memories

You can also call them directly for custom cycle management outside the ObservationProtocol.

#### Learned amplitudes persist across saves

Adjusted amplitudes are **per-record learned state** and survive ordinary saves. A cycle's `period` and `phase` stay declarative — they are refreshed from the model's `cycles` declaration on every save — but its `amplitude` is carried over from what `strengthen_cycle()` / `weaken_cycle()` accumulated:

```python
directive.strengthen_cycle("relevance", factor=1.5)
directive.title = "revised"
directive.save()          # amplitude stays at 1.5x, not reset to the default
```

Stored cycles are matched to declared ones **by period**, so reordering or editing your `cycles` declaration keeps each period's learned amplitude with that period. Adding a period to the declaration gives it the declared amplitude (nothing has been learned for it yet); removing one drops it — the declaration is authoritative about *which* cycles exist, and learning is authoritative about how strong each one is.

If you edit a declared amplitude for a period that has already learned a value, Popoto tries to tell "the developer changed the default" from "learning diverged" apart, by remembering the declared amplitude that was in effect the last time the record was saved (its *declared baseline*):

- **Baseline unchanged** (you haven't touched the `cycles` declaration for that period since the last save): the learned amplitude wins, same as before.
- **Baseline changed** (you edited the declared amplitude for that period): your new declared amplitude wins, and the previously learned amplitude for that period is discarded. This is a **destructive, non-recoverable reset** — there is no way to get the discarded learned amplitude back. Popoto logs one `INFO`-level line per reset naming the model, field, member key, period, old/new declared values, and the discarded learned amplitude, so this is auditable after the fact even though it can't be undone. This logging is deliberately unsampled and undeduplicated: editing a declared amplitude that every record has learned emits one INFO line **per reset per record**, so the log volume is proportional to how many records had learned that period — a one-shot burst bounded by the number of affected records, not an ongoing rate.
- **No baseline recorded** (a legacy record saved before this behavior existed, or a record whose cycles entry was written directly rather than through `on_save()`): treated the same as "unchanged" — the learned amplitude is preserved rather than reset, so upgrading to this version never silently discards existing learning.

!!! warning "Upgrading and editing in the same deploy swallows the edit once"

    A record written before this behavior existed has no recorded baseline. Its
    first save after upgrading adopts the *current* declared amplitude as that
    baseline rather than comparing against anything — so if you also edit
    `amplitude=` in that same deploy, the first save cannot tell the edit apart
    from "no baseline recorded" and preserves the learned value instead of
    resetting it. The edit appears to do nothing; a second save (with no further
    declaration change) is required before a reset is detected. This is
    accepted, not fixed — treating "no baseline" as "declaration changed" would
    instead reset *every* learned amplitude in the database on the first save
    after upgrade, which is strictly worse. Two remedies:

    - **Upgrade first, edit later**: let every record save at least once on the
      new version before editing the declared amplitude, so each one has a
      recorded baseline to compare against.
    - **Force the reset directly**: `hdel` the member's entry from the cycles
      companion hash and re-save, using the same procedure as the manual reset
      below — this discards the learned amplitude immediately regardless of
      baseline state.

    The same one-save swallow applies to a record restored via `import_state`
    (it normalizes to "baseline unknown," the same shape as a legacy record) and
    to a rolling deploy where old- and new-version processes save the same
    record with different in-process declarations.

If you want to intentionally discard learning for a period without changing its declared amplitude, use the zero-then-restore procedure below (delete the cycles hash entry) rather than round-tripping the declared value, since a no-op edit that doesn't change the declared value will not trigger a reset.

!!! warning "A cycle weakened to zero stays at zero"

    `weaken_cycle()` snaps amplitudes below `0.01` to `0.0`, and that zero is preserved like any other learned value — every future save keeps it. This is deliberate: a silenced cycle should stay silenced. There is no reset method; to restore the declared defaults for a record, delete its entry from the cycles companion hash, and the next `save()` will re-adopt them:

    ```python
    field = MyModel._meta.fields["relevance"]
    key = field.get_cycles_hash_key(record, "relevance")
    popoto.get_redis().hdel(key, record.db_key.redis_key)
    record.save()   # declared amplitudes restored
    ```

!!! note "The cycles/pressure merge is atomic, but a rolling deploy still has a window"

    `save()`'s read-decide-write against the cycles and pressure companion
    hashes, and `strengthen_cycle()`/`weaken_cycle()`'s read-clamp-write
    against the cycles hash, each run as one atomic server-side Lua script —
    two concurrent writers on the *same* process generation can no longer
    clobber each other's update, and there is no longer a pipeline-ordering
    hazard between `save()` and an amplitude adjustment sharing one pipeline
    (`save()`'s companion-hash write runs eagerly and immediately, regardless
    of any pipeline you pass it, precisely so its merge decision is available
    to log synchronously — only the parent timestamp update is queued onto
    your pipeline).

    That atomicity is a property of the *script*, not of the stored data, so
    it only protects writers that are actually running the script. During a
    rolling deploy, an old-version process (client-side `HGET`/`HSET`) and a
    new-version process (the Lua script) can still race each other on the
    same member — the lost-update window this feature closes is closed only
    once every writer has rolled forward. This is the same shape as the
    existing baseline-swallow caveat above, and the same remedy applies: it
    is a transient rollout window, not a persistent hazard, and resolves
    itself once the deploy completes.

!!! note "A whole-number learned amplitude may round-trip as `int`"

    Cycle amplitudes and declared baselines pass through the Lua scripts as
    msgpack values. Lua 5.1 has a single number type, so a whole-number
    amplitude (e.g. `10.0` after `strengthen_cycle(factor=5.0)` on a
    declared `2.0`) is indistinguishable from an integer inside the script
    and round-trips back to Python as `10`, not `10.0`. The *value* is
    unaffected — `10 == 10.0` and arithmetic on it behaves identically —
    only `isinstance(x, float)` checks can observe the difference. Popoto
    coerces amplitude back to `float` at the public boundaries that matter
    (the `strengthen_cycle()`/`weaken_cycle()` return value, the reset log
    line); a *period*, by contrast, is never coerced, since it may
    legitimately be a non-numeric `TemporalPeriod` string alias.

### Refreshing the Decay Clock

```python
# Same as DecayingSortedField — updates the timestamp
directive.touch("relevance")
```

## Redis Data Model

CyclicDecayField stores data in three Redis structures:

1. **Sorted set** (inherited): `$CyclicDecayF:{Model}:{field}:{partitions}` — member timestamps
2. **Cycles hash**: `$CyclicDecayF:{Model}:{field}:{partitions}:cycles` — per-member cycle tuples (msgpack). Each tuple is `[period, amplitude, phase]`, plus an optional 4th slot, `declared_baseline`, recording the declared amplitude as of the last save — used only to detect declaration edits (above) and invisible to the ranking Lua script, which reads no index beyond 2. `strengthen_cycle()` / `weaken_cycle()` still return and accept the 3-element `[period, amplitude, phase]` shape; the baseline slot is internal bookkeeping, not part of the public API. It's also deployment-local: [`export_state`/`import_state`](../fields.md) round-trip the learned amplitude but drop the baseline slot on import, so moving a record to a new deployment doesn't itself trigger a destructive reset on the first save there.
3. **Pressure hash**: `$CyclicDecayF:{Model}:{field}:{partitions}:pressure` — per-member `{rate, last_resolved}` (msgpack)

All three structures are maintained automatically by `on_save()` and `on_delete()`.

!!! note "The `$CyclicDecayF:` prefix follows the *field's own class*"
    `FieldBase` assigns each field class its own `field_class_key`, so a
    subclass of `CyclicDecayField` writes and reads under **its** prefix, not
    the base class's:

    ```python
    class UrgencyField(CyclicDecayField):
        pass
    # sorted set:   $UrgencyF:{Model}:{field}:{partitions}
    # cycles hash:  $UrgencyF:{Model}:{field}:{partitions}:cycles
    # pressure:     $UrgencyF:{Model}:{field}:{partitions}:pressure
    ```

    The companion hashes are always the sorted-set key plus a `:cycles` /
    `:pressure` suffix, which is why `rank_decayed()` derives them from the
    ZSET key it is handed rather than rebuilding them. The classmethod forms —
    `get_cycles_hash_key_from_parts()` / `get_pressure_hash_key_from_parts()` —
    resolve against **the class you call them on**, so call them on the field's
    own class (`type(field)` or `field.__class__`), never on `CyclicDecayField`
    when the field might be a subclass. Doing the latter is what made a
    subclassed field read hashes nothing had written; it degraded silently to
    plain decay rather than raising, and is fixed in
    [#662](https://github.com/tomcounsell/popoto/issues/662).

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

## Confidence-Modulated Decay

`CyclicDecayField` inherits confidence modulation from `DecayingSortedField` unchanged: the `decay`
term above is computed with a per-record effective rate derived from the record's `ConfidenceField`
value, while `cyclic` and `pressure` are untouched.

```text
eff   = decay_rate * 2 ^ (s * 2 * (c0 - c))
decay = base_score * t ^ (-decay_rate) * max(t, 1.0) ^ (-(eff - decay_rate))
```

Auto-detection, the `confidence_modulation_field` kwarg, the
`Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED` kill switch, bit-exact neutrality, the `max(t, 1.0)`
sign-flip guard, and the rank-inversion caveat all behave identically — see
[DecayingSortedField → Confidence-Modulated Decay](decaying-sorted-field.md#confidence-modulated-decay)
for the full explanation. Cyclic ≡ plain equivalence still holds: with `cycles=[]` and
`pressure_rate=0.0`, a modulated `CyclicDecayField` ranks identically to a modulated
`DecayingSortedField`.

### The confidence hash is `KEYS[4]` here

`CyclicDecayField` carries a fork of the decay Lua, and that fork already binds `KEYS[2]` = cycles
hash and `KEYS[3]` = pressure hash. The confidence `:data` hash is therefore appended at **`KEYS[4]`**
(numkeys 4 at both EVAL sites), not `KEYS[2]` as in `DECAY_SCORE_LUA`. The `ARGV` indices are the
same in both scripts (`ARGV[5]` = strength, `ARGV[6]` = `c0`); only the KEYS index differs.

Do not "unify" the two scripts on `KEYS[2]`: reusing it here would `cmsgpack.unpack` the cycles array
as a confidence dict, which corrupts scores silently instead of raising.

That rule is now enforced by the class boundary rather than by this comment.
`CyclicDecayField.rank_decayed()` overrides
[`DecayingSortedField.rank_decayed()`](decaying-sorted-field.md#ranking-a-partition-zset-directly)
and builds `[zset, zset + ":cycles", zset + ":pressure", confidence_hash]` itself,
so the base implementation's layout never reaches this fork's script and vice
versa. Callers pass the same arguments to either field and let method resolution
pick the layout.

The override accepts `validity=` and **ignores** it. `CYCLIC_DECAY_LUA` has no
validity gate — a deliberate omission documented under
[Known limitations](validity-and-supersession.md#known-limitations) — so a caller
that gates a mixed set of fields passes the gate args unconditionally instead of
branching on field type, and this field drops them.

### Redis structure

A fourth structure joins the three above when modulation is active — the `ConfidenceField` `:data`
hash, `$ConfidencF:{Model}:{field}:data[:{partition}]`. It is read-only from this script's
perspective; `ConfidenceField` remains its sole writer.

## Error Handling

- `CyclicDecayField(cycles=[(0, 1.0, 0)])` raises `ModelException` (zero period)
- `CyclicDecayField(pressure_rate=-1)` raises `ModelException` (negative rate)
- `resolve_pressure()` on unsaved model raises `TypeError`
- `resolve_pressure()` on non-CyclicDecayField raises `TypeError`
- `resolve_pressure()` with `pressure_rate=0` raises `TypeError`
- `strengthen_cycle()` / `weaken_cycle()` on non-CyclicDecayField raises `TypeError`
- `strengthen_cycle()` / `weaken_cycle()` on unsaved model raises `TypeError`

## Integration with ObservationProtocol

When used with [ObservationProtocol](observation-protocol.md), cycle amplitudes are adjusted automatically based on how the agent responds to surfaced memories. See [ObservationProtocol — Effects matrix](observation-protocol.md#effects-matrix) for the full effects table.
