# DecayingSortedField

A `SortedField` subclass where records lose retrieval weight over time following a power-law decay curve. This is the foundational primitive for agent memory — most subsequent primitives depend on time-weighted scoring.

## Overview

The sorted set score is always a timestamp. A Lua script computes decay-ranked results at query time:

```text
decayed_score = base_score * elapsed_days ^ (-decay_rate)
```

With the default `decay_rate=0.1`, a record scores 1.0 after 1 day, 0.87 after 4 days, and 0.63 after 100 days. All computation happens server-side in Lua — no round trips for ranking.

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `decay_rate` | `float` | `0.1` | Controls how fast scores drop. Higher = faster decay. Configurable via `Defaults.DECAY_RATE`. Empirically tuned; see [Tuning Magic Numbers](../guides/tuning-magic-numbers.md). |
| `base_score_field` | `str` | `None` | Name of a companion field whose value multiplies the decay curve. When `None`, base score is 1.0. |
| `confidence_modulation_field` | `str`, `False`, or `None` | `None` | Which `ConfidenceField` modulates each record's effective decay rate. `None` auto-detects a single `ConfidenceField` on the model; a `str` names one explicitly; `False` disables modulation for this field. See [Confidence-Modulated Decay](#confidence-modulated-decay). |
| `partition_by` | `str` or `tuple` | `()` | Partition the sorted set by key field values, inherited from `SortedField`. |

## Usage

### Basic Model Definition

```python
from popoto import Model, KeyField, Field
from popoto.fields import DecayingSortedField

class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField()
```

The `relevance` field automatically timestamps records on save. Query for the most relevant recent records:

```python
memories = Memory.query.filter(agent_id="agent-1").top_by_decay(10)
```

### Base Score Weighting

Point `base_score_field` at another field to weight records differently:

```python
class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    importance = Field(type=float, default=1.0)
    relevance = DecayingSortedField(base_score_field="importance")
```

A record with `importance=5.0` stays relevant longer than one with `importance=1.0` — at a given threshold, lifetime scales as `score^(1/decay_rate)`. With the default `decay_rate=0.1` this ratio is very large (importance strongly dominates recency); with the prior `decay_rate=0.5` the ratio was `score²` (a more modest 25× for 5× importance). If you need faster forgetting, pass `decay_rate=0.5` or higher on the field constructor.

### Source Weighting with InteractionWeight

Use `InteractionWeight` constants for multi-agent teams with human oversight:

```python
from popoto.fields.constants import InteractionWeight

class TeamMemory(Model):
    agent_id = KeyField()
    importance = Field(type=float, default=InteractionWeight.AGENT)
    content = Field(type=str)
    relevance = DecayingSortedField(base_score_field="importance")

# CEO directive — stays relevant for years
TeamMemory(
    agent_id="pm-1",
    importance=InteractionWeight.combine(InteractionWeight.HUMAN, InteractionWeight.EXECUTIVE),
    content="We're pivoting to enterprise",
).save()
```

Human interactions are rare but high-signal; agent-to-agent interactions are
frequent but lower-signal. The constants split across two axes — **source**
(what kind of entity) and **role** (authority level) — combined by addition:

```python
class InteractionWeight:
    # Source axis
    HUMAN = 6.0
    AGENT = 1.0
    SYSTEM = 0.2

    # Role axis
    EXECUTIVE = 44.0
    MANAGER = 16.0
    PEER = 6.0
    SUBORDINATE = 1.0

    @staticmethod
    def combine(source, role):
        return source + role
```

At `decay_rate=0.5`, effective lifetime is roughly score² days (at the default
`decay_rate=0.1` lifetime grows much more slowly with score — see
[Tuning Magic Numbers](../guides/tuning-magic-numbers.md)):

| Combination | Score | Effective lifetime |
|-------------|-------|--------------------|
| Human executive | 50.0 | ~7 years |
| Human manager | 22.0 | ~1.3 years |
| Human peer | 12.0 | ~5 months |
| Agent executive | 45.0 | ~5.5 years |
| Agent manager | 17.0 | ~9 months |
| Agent peer | 7.0 | ~7 weeks |
| Agent subordinate | 2.0 | ~4 days |
| System | 0.2 | ~1 hour |

These are plain floats — override them freely for your domain. The values
encode two principles: human interactions are stickier than agent
interactions, and authority level determines how long directives persist.

### Refreshing Timestamps

Call `touch()` to reset the decay clock without a full save:

```python
memory.touch("relevance")
```

### Query-Time Overrides

Override `decay_rate` per query for different retrieval contexts:

```python
# Aggressive decay — only very recent records
hot = Memory.query.filter(agent_id="agent-1").top_by_decay(5, decay_rate=1.0)
```

On a model that also declares a [`ValidityField`](validity-and-supersession.md), a
keyword-only `as_of` reconstructs ranking at a past moment instead of now:

```python
past = Memory.query.filter(agent_id="agent-1").top_by_decay(5, as_of=two_weeks_ago)
```

`as_of` is ignored on models without a `ValidityField`. See
[ValidityField and SupersessionProtocol](validity-and-supersession.md#point-in-time-reconstruction)
for how membership is evaluated at a past instant.

### Reading the Index

`count()` and `members()` read a sorted field's index directly, without going through a query. Recipes use them to answer "how many" and "which members" for one partition. Both are classmethods on the field object; the model instance supplies whichever partition values `partition_by` names, and the read resolves to exactly the key `save()` and `delete()` use for that instance, so an unset partition field reads the same `None` partition that `save()` wrote.

```python
field = Memory._meta.fields["relevance"]
field.count(memory, "relevance")                        # -> int, one ZCARD
field.members(memory, "relevance", 0, 9)                # -> 10 stalest keys, one ZRANGE
field.members(memory, "relevance", 0, 9, reverse=True)   # -> 10 freshest keys, one ZREVRANGE
```

`members()` returns Redis keys as `str`, ordered by score (`reverse=True` walks from the highest score down). `start`/`stop` follow `ZRANGE` bounds: inclusive, negative values count from the end, and `stop < start` returns `[]`.

### Ranking a partition ZSET directly

`rank_decayed()` runs the decay script against one already-resolved partition ZSET key and returns the flat `[member, score, member, score, ...]` reply. It is the seam a caller uses when it holds records rather than filter kwargs, and therefore cannot go through `Query.top_by_decay()`.

```python
field = Memory._meta.fields["relevance"]
zkey = field.get_sortedset_db_key(Memory, "relevance").redis_key
field.rank_decayed(zkey, now=time.time())                     # every member, one ZCARD + one EVAL
field.rank_decayed(zkey, now=time.time(), n=10)               # top 10, no ZCARD
field.rank_decayed(
    zkey,
    now=time.time(),
    confidence=(conf_data_key, strength, c0),                 # from confidence_modulation_args()
    validity=(invalid_at_key, valid_from_key, as_of),         # from validity_gate_args()
)
```

`n=None` (the default) asks for every member's score, which costs one `ZCARD` first; an empty ZSET returns `[]` without issuing the `EVAL` at all. Passing an explicit `n` skips the `ZCARD`. Omitting `confidence` or `validity` disables that join exactly as `MODULATION_DISABLED` / `VALIDITY_GATE_DISABLED` do — same scores, same script, same bytes on the wire.

!!! note "The **field** builds the `KEYS` array, deliberately"
    `rank_decayed` is polymorphic: `CyclicDecayField` overrides it with its own,
    mutually incompatible layout (confidence at `KEYS[4]`, not `KEYS[2]`). Each
    implementation lives in the class that owns its script, so neither body
    contains an index it must not use, and a caller holding both fields never
    has to know which is which — dispatch is ordinary method resolution. Callers
    that assemble the `KEYS` array themselves reintroduce exactly the coupling
    the "do not unify" comments in both scripts warn about.

## Confidence-Modulated Decay

`base_score_field` scales the curve's **magnitude**, which never changes relative order: a demoted
record starts lower but is never forgotten *sooner*. Confidence modulation changes the **rate**, so
accumulated outcome evidence actually alters how fast a record leaves the corpus. Corroborated
memories persist longer; repeatedly dismissed or contradicted ones fall away faster.

### The formula

Per record, the effective decay rate is derived from that record's
[`ConfidenceField`](confidence-field.md) value:

```text
eff   = decay_rate * 2 ^ (s * 2 * (c0 - c))
score = base_score * t ^ (-decay_rate) * max(t, 1.0) ^ (-(eff - decay_rate))
```

- `c` — the record's confidence, clamped to `[0, 1]`
- `c0` — the confidence field's own `initial_confidence` (**not** a hard-coded `0.5`)
- `s` — `Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH` (0.5), read as "doublings of the decay rate
  at zero confidence"
- `t` — `elapsed_days`, floored at `0.01`

The exponential form (Pavlik & Anderson 2005; Duolingo half-life regression) keeps the multiplier
bounded in `[2^-s, 2^s]` and positive by construction, with no clamping needed for correctness.

### Neutrality is bit-exact

When `c == c0` the exponent is exactly `0` and `math.pow(x, -0)` is exactly `1.0`, so the whole
correction term vanishes. Scores are **byte-identical** to the pre-modulation formula whenever:

- the record has no confidence evidence yet,
- the model carries no `ConfidenceField`,
- `confidence_modulation_field=False`,
- `Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH` is `0`, or
- `Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED` is `False`.

Centering on the field's own `c0` matters: `ConfidenceField.on_save` writes `initial_confidence` for
every saved record, so "no evidence" is nearly never literally absent data. A field configured with
`initial_confidence=0.3` would otherwise carry a permanent `2^(0.4s)` penalty on every zero-evidence
record.

Every disabled path also skips the per-member `HGET` entirely, so unmodulated deployments pay
nothing.

### Why `max(t, 1.0)` is load-bearing

The guard is not redundant tidying — removing it inverts the feature. `elapsed_days` is floored at
`0.01`, and for `t < 1` the term `t^(-rate)` is a multiplier **greater than 1** that a *larger* rate
amplifies *more*: at `t = 0.01`, rate 0.66 gives ×21.9 while rate 0.35 gives only ×5.0. Without the
clamp, modulation would run backwards for the first 24 hours and boost exactly the low-confidence
junk it exists to bury — and because agent memory is touched constantly, most of the working set
lives in that region. Clamping the correction's base to `>= 1.0` makes the term exactly `1.0` for
fresh records, so modulation only ever applies where a higher rate means a lower score.

### Caveat: rank inversion

Rate modulation makes two records' log-log score lines cross **exactly once**. That is semantically
what you want — salience wins short-run, evidence wins long-run — but it has a consequence magnitude
weighting never produces: a record's **rank can improve over time even as its score falls**. Cached
top-N snapshots therefore drift, and a record absent from yesterday's top 10 may appear in today's
without anything being written. Re-run `top_by_decay()` rather than caching its output if ordering
stability matters to your application.

### Usage

Auto-detection means the common case needs no configuration at all:

```python
from popoto import Model, KeyField, Field
from popoto.fields import DecayingSortedField
from popoto.fields.confidence_field import ConfidenceField

class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    certainty = ConfidenceField()          # exactly one -> modulation is ON
    relevance = DecayingSortedField()
```

Name the field explicitly when a model carries more than one `ConfidenceField`, or opt out:

```python
relevance = DecayingSortedField(confidence_modulation_field="certainty")
relevance = DecayingSortedField(confidence_modulation_field=False)   # opt out
```

Resolution order:

| Condition | Result |
|-----------|--------|
| `Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED = False` | Off (deploy-level kill switch) |
| `confidence_modulation_field=False` | Off |
| `confidence_modulation_field="name"` | That field; `ModelException` if missing or not a `ConfidenceField` |
| Exactly one `ConfidenceField` on the model | That field (auto-detected) |
| No `ConfidenceField` on the model | Off |
| Two or more, none named | Off, plus a `logger.warning` naming the candidates |

### Kill switch

Modulation is **default-on** via auto-detection, so an upgrade can change ranking without any model
change. The deploy-level kill switch turns modulation off without editing
model definitions:

```python
from popoto.fields.constants import Defaults

Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED = False
```

Set it at process start, before queries run. It is re-read on every call, so toggling it at runtime
takes effect immediately.

### Partitioned confidence fields

The Lua script joins the decay sorted set to the `ConfidenceField` `:data` hash on the member key.
When that field declares `partition_by`, one decay index can span several `:data` hashes and no
single hash covers the result set. Rather than silently modulating some members and not others, the
query raises `QueryException` naming the filters it needs:

```python
Memory.query.filter(agent_id="agent-1").top_by_decay(10)
# QueryException: ... ConfidenceField 'certainty' ... is partitioned by project.
# Query must include filter(s) for: project.
```

Add the missing filter, or set `confidence_modulation_field=False` on the decay field.

## Tuning

The `decay_rate` default is configurable via `Defaults`:

```python
from popoto.fields.constants import Defaults

Defaults.DECAY_RATE = 0.3  # Slower decay globally
```

Explicit kwargs always override `Defaults`:

```python
# This field uses 0.7 regardless of Defaults.DECAY_RATE
fast_decay = DecayingSortedField(decay_rate=0.7)
```

Modulation strength is tuned the same way:

```python
Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH = 0.3  # gentler evidence coupling
Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH = 0.0  # exact no-op
```

See [Tuning Magic Numbers](../guides/tuning-magic-numbers.md#decayingsortedfield-cyclicdecayfield)
for ranges and provenance.

## Architecture

- **Redis key pattern**: `ClassName:_field_name` (sorted set with timestamp scores)
- **Lua script**: Computes `base_score * elapsed_days^(-decay_rate)` server-side, reads base scores from model hash via cmsgpack
- **Confidence join**: the `ConfidenceField` `:data` hash is passed as `KEYS[2]`, with strength `s` as
  `ARGV[5]` and `c0` as `ARGV[6]`. The ZSET member string *is* the record's `redis_key`, and the
  `:data` hash is keyed by the same string, so the join needs no translation. One extra `HGET` per
  member, issued only when modulation is active. (`CyclicDecayField` forks this script and binds the
  confidence hash at `KEYS[4]` instead — see its docs.)
- **Inheritance**: Extends `SortedFieldMixin` + `Field`

## See Also

- [CyclicDecayField](cyclic-decay-field.md) — adds cyclical resonance and homeostatic pressure
- [ConfidenceField](confidence-field.md) — the evidence signal that modulates the rate
- [ValidityField and SupersessionProtocol](validity-and-supersession.md) — the orthogonal membership axis: validity decides whether a record is retrievable at all, decay decides its rank among what remains
- [Agent Memory overview](agent-memory.md) — full primitives reference
