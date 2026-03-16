# Agent Memory

Popoto Agent Memory gives you ORM primitives for programmable memory — records that decay over time, strengthen through use, track confidence, form associations, and surface the right context at the right moment.

These are generic Redis-backed field types, mixins, and query methods. They don't encode any specific agent architecture. You compose them into memory models the same way you'd compose `KeyField`, `SortedField`, and `Relationship` into any Popoto model.

## Why your agent needs memory primitives

LLMs reason well over content you put in their context window. What they can't do on their own:

- **Prioritize by recency and frequency** — know which records are "hot" right now
- **Learn from outcomes** — track what worked and what didn't
- **Manage certainty** — downweight contradicted knowledge automatically
- **Retrieve associatively** — surface related records without explicit graph queries
- **Filter noise** — avoid storing low-value observations in the first place

Popoto Agent Memory gives you these capabilities as composable ORM building blocks. Each one is independently useful; together they form a complete agent memory system.

## Primitives overview

The primitives ship incrementally. Each builds on the ones before it.

| Primitive | What it does | Status |
|-----------|-------------|--------|
| [DecayingSortedField](#decayingsortedfield) | Time-weighted scoring — records lose relevance over time unless refreshed | Shipped ([PR #199](https://github.com/tomcounsell/popoto/pull/199)) |
| [CyclicDecayField](#cyclicdecayfield) | Temporal rhythms + homeostatic pressure on top of decay | Shipped ([PR #201](https://github.com/tomcounsell/popoto/pull/201)) |
| [AccessTracker](#accesstracker) | Tracks read patterns — access count, timestamps, spacing effects | Shipped ([PR #203](https://github.com/tomcounsell/popoto/pull/203)) |
| [ObservationProtocol](#observationprotocol) | Outcome-driven memory effects — acted/dismissed/deferred/contradicted | Shipped ([PR #206](https://github.com/tomcounsell/popoto/pull/206)) |
| [WriteFilter](#writefilter) | Gates persistence — low-value records silently discarded at write time | Shipped ([PR #214](https://github.com/tomcounsell/popoto/pull/214)) |
| [ConfidenceField](#confidencefield) | Bayesian certainty — corroboration strengthens, contradiction weakens | Shipped ([PR #215](https://github.com/tomcounsell/popoto/pull/215)) |
| [CoOccurrenceField](#cooccurrencefield) | Weighted associations — co-accessed records strengthen their link | Shipped ([PR #218](https://github.com/tomcounsell/popoto/pull/218)) |
| [EventStreamMixin](#eventstreammixin) | Append-only mutation log via Redis Streams | Shipped |
| [CompositeScoreQuery](#compositescorequery) | Multi-factor retrieval — combine N sorted indexes with weights | Shipped |
| [ExistenceFilter](#existencefilter) | Bloom filter for O(1) "do I know anything about X?" checks | Planned |
| [PredictionLedger](#predictionledger) | Outcome tracking — record predictions, observe results, compute error | Planned |
| [StreamConsumer](#streamconsumer) | Background processing framework for Redis Streams | Planned |
| [PolicyCache](#policycache) | Learned action selection — crystallized state-action-outcome patterns | Planned |
| [ContextAssembler](#contextassembler) | Retrieval-to-injection bridge — assemble LLM-ready context within token budgets | Planned |

## DecayingSortedField

A `SortedField` subclass where records lose retrieval weight over time following a power-law decay curve. This is the foundational primitive — most subsequent primitives depend on time-weighted scoring.

The sorted set score is always a timestamp. A Lua script computes decay-ranked results at query time:

```text
decayed_score = base_score × elapsed_days ^ (-decay_rate)
```

With the default `decay_rate=0.5`, a record scores 1.0 after 1 day, 0.5 after 4 days, and 0.1 after 100 days.

### Basic usage

```python
from popoto import Model, KeyField, Field
from popoto.fields import DecayingSortedField

class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField()
```

The `relevance` field automatically timestamps records on save. Query for the most relevant recent records with `top_by_decay()`:

```python
memories = (
    Memory.query.filter(agent_id="agent-1")
    .top_by_decay(10)
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `decay_rate` | `float` | `0.5` | Controls how fast scores drop. Higher = faster decay. |
| `base_score_field` | `str` | `None` | Name of a companion field whose value multiplies the decay curve. When `None`, base score is 1.0. |
| `partition_by` | `str` or `tuple` | `()` | Partition the sorted set by key field values, inherited from `SortedField`. |

### Base score weighting

By default, all records decay at the same rate — ranking is purely by recency. To weight records differently, point `base_score_field` at another field:

```python
class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    importance = Field(type=float, default=1.0)
    relevance = DecayingSortedField(base_score_field="importance")
```

A record with `importance=5.0` stays relevant 25x longer than one with `importance=1.0` (at decay_rate=0.5).

### Source weighting for teamwork

In multi-agent teams with human oversight, human interactions are rare but high-signal. Agent-to-agent interactions are frequent but lower-signal. Use `InteractionWeight` constants to ensure human directives don't get drowned out by agent chatter:

```python
from popoto.fields.constants import InteractionWeight

class TeamMemory(Model):
    agent_id = KeyField()
    source = Field(type=str)
    role = Field(type=str)
    importance = Field(type=float, default=InteractionWeight.AGENT)
    content = Field(type=str)
    relevance = DecayingSortedField(base_score_field="importance")

# CEO gives a directive — stays relevant for years
TeamMemory(agent_id="pm-1", source="human", role="executive",
           importance=InteractionWeight.combine(
               InteractionWeight.HUMAN, InteractionWeight.EXECUTIVE),
           content="We're pivoting to enterprise").save()

# Agent colleague logs a finding — moderate lifetime
TeamMemory(agent_id="pm-1", source="agent", role="peer",
           importance=InteractionWeight.combine(
               InteractionWeight.AGENT, InteractionWeight.PEER),
           content="Found 3 broken API contracts in staging").save()
```

Weights are split across two axes — **source** (human vs agent) and **role** (authority level) — combined by addition:

```python
class InteractionWeight:
    # Source axis — what kind of entity
    HUMAN = 6.0
    AGENT = 1.0
    SYSTEM = 0.2

    # Role axis — authority level
    EXECUTIVE = 44.0
    MANAGER = 16.0
    PEER = 6.0
    SUBORDINATE = 1.0

    @staticmethod
    def combine(source, role):
        return source + role
```

With `decay_rate=0.5`, lifetime ≈ score² days:

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

These are just floats — override them freely for your domain. The values encode two principles: **human interactions are stickier than agent interactions**, and **authority level determines how long directives persist**.

### Query-time overrides

Both `decay_rate` and `base_score_field` can be overridden per query for different retrieval contexts:

```python
# Default decay rate from field definition
recent = Memory.query.filter(agent_id="agent-1").top_by_decay(5)

# Aggressive decay — only very recent records
hot = Memory.query.filter(agent_id="agent-1").top_by_decay(5, decay_rate=1.0)

# Weight by a different field for this query
by_urgency = Memory.query.filter(agent_id="agent-1").top_by_decay(5, base_score_field="urgency")
```

### Refreshing timestamps

Call `touch()` to update a record's timestamp without a full save. This resets the decay clock:

```python
memory = Memory.query.get(agent_id="agent-1", content="deployment procedure")
memory.touch("relevance")
```

### Existing SortedField queries

All standard range queries work against the timestamp score:

```python
import time

one_week_ago = time.time() - 86400 * 7
recent = Memory.query.filter(agent_id="agent-1", relevance__gte=one_week_ago)
```

## CyclicDecayField

A `DecayingSortedField` subclass that adds **cyclical resonance** and **homeostatic pressure** to time-weighted scoring. When cycles and pressure are both zero, behavior is identical to `DecayingSortedField`.

The effective score at query time: `decay + cyclic_resonance + pressure`

- **Cyclical resonance** — periodic boosts following cosine curves. A record about Q1 renewals resurfaces every January.
- **Homeostatic pressure** — urgency that builds linearly while an item goes unresolved. Discharged by calling `resolve_pressure()`.

### Basic usage

```python
from popoto import Model, KeyField, Field, CyclicDecayField
from popoto.fields.constants import TemporalPeriod

class Directive(Model):
    agent_id = KeyField()
    content = Field(type=str)
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.QUARTERLY, 5.0, 0)],
        pressure_rate=0.1,
    )
```

Query with the same `top_by_decay()` interface:

```python
top = Directive.query.filter(agent_id="agent-1").top_by_decay(n=10)
```

Discharge accumulated urgency when the agent acts on a record:

```python
directive.resolve_pressure("relevance")
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `decay_rate` | `float` | `0.5` | Power-law decay exponent (inherited). |
| `base_score_field` | `str` | `None` | Companion field whose value multiplies the decay curve (inherited). |
| `cycles` | `list` | `[]` | List of `(period, amplitude, phase)` tuples. Use `TemporalPeriod` constants for period values. |
| `pressure_rate` | `float` | `0.0` | Rate of urgency buildup per unresolved day. |
| `partition_by` | `str`/`tuple` | `()` | Partition sorted set by key fields (inherited). |

### TemporalPeriod constants

Import from `popoto.fields.constants`:

| Constant | Value (seconds) | Usage |
|----------|----------------|-------|
| `TemporalPeriod.DAILY` | 86,400 | Daily check-ins |
| `TemporalPeriod.WEEKLY` | 604,800 | Weekly reviews |
| `TemporalPeriod.MONTHLY` | 2,592,000 | Monthly reports |
| `TemporalPeriod.QUARTERLY` | 7,776,000 | Quarterly planning |
| `TemporalPeriod.YEARLY` | 31,536,000 | Annual cycles |

See [CyclicDecayField feature docs](cyclic-decay-field.md) for the full reference including the scoring formula, Redis data model, and error handling.

## AccessTracker

Tracks read access patterns on any model using a two-stage pipeline: reads are first staged (cheap), then atomically promoted to a confirmed access log. This prevents naive "every read strengthens" behavior — only meaningful reads count.

Shipped in [PR #203](https://github.com/tomcounsell/popoto/pull/203).

### Basic usage

```python
from popoto import Model, KeyField, Field, AccessTrackerMixin, DecayingSortedField

class Memory(Model, AccessTrackerMixin):
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField()

# Reading triggers on_read() automatically via query hooks
memories = Memory.query.filter(agent_id="agent-1").top_by_decay(5)

# After the agent acts on a memory, confirm the read
memories[0].confirm_access()       # promotes staged → confirmed
memories[1].discard_staged_access() # clears staging without promoting

# Inspect access patterns
print(memories[0].access_count)    # 42
print(memories[0].last_accessed)   # 1741872000.0
```

### How staging works

`on_read()` fires automatically when instances are hydrated via `Query.get()`, `Query.filter()`, `top_by_decay()`, and their async variants. Each call appends a timestamp to a per-instance staging list (`RPUSH` — single Redis command, batched via pipeline).

Staged reads are not yet "real" — they represent candidate accesses. Your application decides which reads were meaningful:

- `confirm_access()` — atomically promotes all staged timestamps to the confirmed access log using a Lua script. Updates `access_count` and `last_accessed`.
- `discard_staged_access()` — clears staging without affecting confirmed data.

### Configuration

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `_max_access_log` | `int` | `100` | Maximum timestamps kept in the confirmed access log. Older entries trimmed on confirm. |
| `_track_reads` | `bool` | `True` | Set to `False` to disable automatic `on_read()` from queries. |

### Suppressing tracking for bulk operations

Use `no_track()` on the query builder to prevent `on_read()` from firing during internal operations like reindexing or migration:

```python
# These reads won't be tracked
Memory.query.filter(agent_id="agent-1").no_track().all()
```

### Delete cleanup

When a tracked model instance is deleted, all three AccessTracker Redis keys (staged, access_log, meta) are automatically removed.

### Redis key patterns

| Key | Type | Purpose |
|-----|------|---------|
| `$AT:{ClassName}:staged:{pk}` | List | Pending read timestamps |
| `$AT:{ClassName}:access_log:{pk}` | List | Confirmed access timestamps (capped) |
| `$AT:{ClassName}:meta:{pk}` | Hash | `access_count` (int) and `last_accessed` (float) |

## ObservationProtocol

Provides lifecycle hooks for outcome-driven memory effects. The application reports how the agent used retrieved memories (acted, dismissed, deferred, contradicted); the ORM applies effects atomically.

Shipped in [PR #206](https://github.com/tomcounsell/popoto/pull/206).

### Why observation matters

An LLM cannot manage its own memory mechanics. Calling `touch()`, resolving predictions, updating confidence is like asking a person to regulate their heartbeat. The ORM must provide hooks that fire automatically and infer memory outcomes from the agent's downstream behavior.

### Basic usage

```python
from popoto import (
    Model, KeyField, Field, AccessTrackerMixin,
    CyclicDecayField, ObservationProtocol, RecallProposal,
)
from popoto.fields.constants import TemporalPeriod

class Memory(AccessTrackerMixin, Model):
    agent_id = KeyField()
    content = Field(type=str)
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.QUARTERLY, 5.0, 0)],
        pressure_rate=0.1,
    )

# 1. Agent retrieves memories (on_read fires automatically via query hooks)
memories = Memory.query.filter(agent_id="agent-1").top_by_decay(n=10)

# 2. Optional: mark proactively surfaced memories
ObservationProtocol.on_surfaced(memories[:3], reason="pressure_threshold")

# 3. Agent processes memories and generates a response...

# 4. Application infers outcomes from agent behavior
outcome_map = {
    memories[0].db_key.redis_key: "acted",        # Agent used this
    memories[1].db_key.redis_key: "dismissed",     # Agent rejected this
    memories[2].db_key.redis_key: "contradicted",  # Agent contradicted this
    # memories[3:] not in map → default to "deferred"
}
ObservationProtocol.on_context_used(memories, outcome_map)
```

### Three hooks

| Hook | When it fires | Effect |
|------|--------------|--------|
| `on_read(instance)` | Query hydrates an instance | Delegates to AccessTrackerMixin staging |
| `on_surfaced(instances, reason)` | Proactive system pushes memories into context | Creates RecallProposal entries |
| `on_context_used(instances, outcome_map)` | Application reports how agent responded | Applies outcome-specific effects |

### Four outcomes

| Outcome | Meaning | Effects |
|---------|---------|---------|
| `acted` | Agent used the memory | `touch()`, `confirm_access()`, `strengthen_cycle(1.2)`, `resolve_pressure()`, corroborate confidence (signal=0.9) |
| `dismissed` | Agent explicitly rejected | `discard_staged_access()`, `weaken_cycle(0.8)` |
| `deferred` | Agent didn't address it | `discard_staged_access()` only — pressure keeps building |
| `contradicted` | Agent explicitly contradicted | `discard_staged_access()`, `weaken_cycle(0.5)`, contradict confidence (signal=0.1), auto-discharge pressure if confidence < 0.1 |

### Graceful degradation

Each effect checks whether the model supports it before applying. A model with `DecayingSortedField` but no `CyclicDecayField` still gets `touch()` on acted — just no cycle/pressure effects. A model without `AccessTrackerMixin` skips staging operations entirely. A model without `ConfidenceField` skips confidence updates.

### Cycle amplitude adjustment

Two new Model methods for direct cycle control:

```python
# Strengthen: multiply all cycle amplitudes by 1.5x
memory.strengthen_cycle("relevance", factor=1.5)

# Weaken: multiply all cycle amplitudes by 0.6x
memory.weaken_cycle("relevance", factor=0.6)
```

Amplitudes are clamped to `[0.0, 100.0]`. Values below `0.01` snap to zero (effectively dead cycle).

### RecallProposal

Internal ORM infrastructure for tracking proactively surfaced memories. Not a user-facing Model.

```python
# Create proposals when surfacing memories
RecallProposal.create_batch(instances, reason="proactive", partition="agent-1")

# Check pending proposals
pending = RecallProposal.get_pending(Memory, partition="agent-1")

# Expire stale proposals (older than TTL, default 1 hour)
expired = RecallProposal.expire_stale(Memory, partition="agent-1", ttl=3600)
```

Proposals are stored in Redis ZSETs keyed by `$RP:{ClassName}:pending:{partition}`, scored by surfaced_at timestamp. Resolved proposals are removed from the pending set. Expired proposals (past TTL) are treated as deferred.

### Redis key patterns

| Key | Type | Purpose |
|-----|------|---------|
| `$RP:{ClassName}:pending:{partition}` | ZSET | Pending recall proposals, scored by surfaced_at |

## WriteFilter

Gates persistence based on a scoring function evaluated at write time. Records below a threshold are silently discarded. Records above a high threshold are tagged for priority processing in a Redis sorted set.

```python
from popoto import Model, KeyField, Field
from popoto.fields.write_filter import WriteFilterMixin

class Memory(WriteFilterMixin, Model):
    agent_id = KeyField()
    content = Field(type=str)
    importance = Field(type=float, default=0.5)

    def compute_filter_score(self):
        return self.importance or 0.0

# Score 0.1 < min_threshold (0.2) — silently discarded
Memory(agent_id="a1", content="noise", importance=0.1).save()

# Score 0.5 — persisted normally
Memory(agent_id="a1", content="useful", importance=0.5).save()

# Score 0.9 >= priority_threshold (0.7) — persisted AND added to priority set
Memory(agent_id="a1", content="critical", importance=0.9).save()
```

You provide the scoring logic; Popoto provides the gate. This keeps low-value observations out of the index, reducing noise in retrieval.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `_wf_min_threshold` | `0.2` | Below this score, `save()` silently discards via `SkipSaveException` |
| `_wf_priority_threshold` | `0.7` | At or above this score, record is added to `$WF:{ClassName}:priority` sorted set |

Priority-tagged records are stored in a Redis sorted set keyed `$WF:{ClassName}:priority`, scored by the filter score. On `delete()`, cleanup removes the record from the priority set automatically.

See [fields.md](../fields.md#writefiltermixin) for the full field reference.

## ConfidenceField

A `Field` subclass that tracks Bayesian confidence metadata per member, updated atomically via Lua script. Precision grows with `sqrt(n)` — early evidence has outsized effect while established beliefs resist change.

Shipped in [PR #215](https://github.com/tomcounsell/popoto/pull/215).

### Basic usage

```python
from popoto import Model, UniqueKeyField, StringField, ConfidenceField

class Knowledge(Model):
    key = UniqueKeyField()
    claim = StringField()
    certainty = ConfidenceField(initial_confidence=0.5)

knowledge = Knowledge.create(key="fact1", claim="The sky is blue")

# Corroborate (signal >= 0.5 increases confidence)
ConfidenceField.update_confidence(knowledge, "certainty", signal=0.9)

# Contradict (signal < 0.5 decreases confidence)
ConfidenceField.update_confidence(knowledge, "certainty", signal=0.1)

# Read current confidence
confidence = ConfidenceField.get_confidence(knowledge, "certainty")

# Read all metadata
data = ConfidenceField.get_confidence_data(knowledge, "certainty")
# Returns: {confidence: 0.5, evidence_count: 2, corroborations: 1, contradictions: 1}
```

### Bayesian update formula

```
new_confidence = prior + (signal - prior) / sqrt(evidence_count + 1)
```

Early updates move confidence significantly; later updates have diminishing effect as evidence accumulates. Results are clamped to `[0, 1]`.

### Entrainment with ObservationProtocol

When used with `ObservationProtocol.on_context_used()`, confidence is automatically updated based on how the agent uses retrieved memories:

| Outcome | Effect on Confidence |
|---------|---------------------|
| `acted` | Corroborate (signal=0.9) |
| `dismissed` | No change |
| `deferred` | No change |
| `contradicted` | Contradict (signal=0.1); auto-discharge pressure if confidence drops below 0.1 |

When confidence drops below 0.1 due to a `contradicted` outcome, homeostatic pressure on any `CyclicDecayField` is automatically resolved (discharged). This prevents low-confidence memories from building urgency.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `initial_confidence` | `float` | `0.5` | Starting confidence for new members (0-1). |

See [ConfidenceField feature docs](confidence-field.md) for the full reference including convergence behavior and Redis key patterns. See [API Reference](../api-reference.md#confidencefield) for the complete method signatures.

When combined with `DecayingSortedField` or `CompositeScoreQuery`, confidence acts as a multiplier on retrieval weight — low-confidence records are naturally deprioritized.

## CoOccurrenceField

Maintains weighted association edges between model instances using per-PK Redis sorted sets. Weights strengthen via `link()` and `strengthen()`, and decay via `weaken_all()`. Supports symmetric (bidirectional) and asymmetric (unidirectional) modes.

Shipped in [PR #218](https://github.com/tomcounsell/popoto/pull/218).

```python
from popoto import Model, UniqueKeyField, StringField
from popoto.fields.co_occurrence_field import CoOccurrenceField

class Memory(Model):
    key = UniqueKeyField()
    content = StringField()
    associations = CoOccurrenceField(symmetric=True, max_edges=100)

# Create instances and access the field
mem_a = Memory.create(key="ml", content="Machine learning")
mem_b = Memory.create(key="nn", content="Neural networks")
field = Memory._meta.fields["associations"]

# Link and strengthen
field.link(Memory, mem_a.db_key.redis_key, mem_b.db_key.redis_key)
field.strengthen(Memory, mem_a.db_key.redis_key, mem_b.db_key.redis_key, delta=0.05)

# BFS graph propagation — multi-hop associative retrieval
scores = field.propagate(Memory, [mem_a.db_key.redis_key], depth=2, decay_per_hop=0.5)
```

Graph propagation uses a server-side Lua BFS script with exponential weight decay per hop. When the same PK is reached via multiple paths, `max(weight)` is used. Automatic edge pruning via `max_edges` prevents unbounded memory growth.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `symmetric` | `bool` | `True` | If True, edges are bidirectional |
| `max_edges` | `int` | `500` | Maximum edges per PK; lowest-weight pruned when exceeded |
| `decay_factor` | `float` | `0.95` | Default multiplicative decay for `weaken_all()` |

See [CoOccurrenceField field docs](../fields/co-occurrence-field.md) for the full reference including methods, Redis key patterns, and synergy with other memory fields.

## EventStreamMixin

Automatically appends to a Redis Stream on every `save()`, `update()`, or `delete()`. This is infrastructure for background processing — the mixin writes events, your application consumes them via Redis Streams' consumer group API.

### Quick Start

```python
from popoto import Model, EventStreamMixin, UniqueKeyField, StringField

class Memory(EventStreamMixin, Model):
    _stream_name = "memory_mutations"       # Stream key: stream:memory_mutations
    _stream_max_length = 10000              # Approximate MAXLEN trimming
    _stream_metadata_fields = ("source",)   # Extra fields in each entry

    key = UniqueKeyField()
    content = StringField()
    source = StringField(default="")

memory = Memory(key="fact1", content="hello", source="user")
memory.save()    # XADD with op="create"
memory.content = "updated"
memory.save()    # XADD with op="update"
memory.delete()  # XADD with op="delete"
```

### Configuration

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `_stream_name` | `str` | `"mutations"` | Name for the Redis Stream (key: `stream:{name}`) |
| `_stream_partition_field` | `str` | `None` | Field name to partition streams by (key becomes `stream:{name}:{value}`) |
| `_stream_max_length` | `int` | `10000` | Approximate max entries via `MAXLEN ~` trimming |
| `_stream_metadata_fields` | `tuple` | `()` | Field names whose values are included in stream entries |

### Stream Entry Fields

Every stream entry contains these string fields:

| Field | Description |
|-------|-------------|
| `model` | Model class name |
| `pk` | Redis key of the instance |
| `op` | Operation: `"create"`, `"update"`, `"delete"`, or custom |
| `ts` | Unix timestamp |
| `changed_fields` | Comma-separated list of updated fields (from `update_fields`) |

Plus any fields listed in `_stream_metadata_fields`.

### Partitioned Streams

Route events to different streams based on a field value:

```python
class TenantMemory(EventStreamMixin, Model):
    _stream_name = "mutations"
    _stream_partition_field = "tenant"

    key = UniqueKeyField()
    tenant = StringField()

# Writes to stream:mutations:acme
TenantMemory(key="x", tenant="acme").save()

# Writes to stream:mutations:beta
TenantMemory(key="y", tenant="beta").save()
```

### Custom Events (Non-Save Operations)

Operations that bypass `Model.save()` (like `ConfidenceField.update_confidence()` and `CoOccurrenceField.strengthen()`) can log events via the public `_xadd_event()` method:

```python
# Called automatically by ConfidenceField.update_confidence()
instance._xadd_event(
    op="confidence_update",
    extra_fields={"field": "trust", "signal": "0.8"},
)
```

### Error Handling

- **Non-pipeline mode**: XADD failures are caught and logged — `save()` always succeeds if the data write succeeded.
- **Pipeline mode**: XADD is queued atomically with the save. If the pipeline fails, both data and stream entry fail together.

### WriteFilter Interaction

When `WriteFilterMixin` discards a record (score below threshold), the `save()` returns before reaching the EventStreamMixin hook. No stream entry is produced for filtered records.

### Redis Key Patterns

- `stream:{stream_name}` — default stream key
- `stream:{stream_name}:{partition_value}` — partitioned stream key

## CompositeScoreQuery

Combines multiple sorted set indexes via `ZUNIONSTORE` with configurable weights, returning top-K results ranked by composite score. This is where all the scoring primitives converge into a single retrieval call.

Without `composite_score()`, retrieving by multiple factors requires application-level code: fetch by decay, fetch confidence data, fetch access counts, compute composite in Python, re-rank. This is slow (multiple round trips), error-prone, and not composable via the query API. `composite_score()` does it all server-side in a single call.

### Basic usage

```python
from popoto import Model, KeyField, Field
from popoto.fields import DecayingSortedField, ConfidenceField
from popoto.fields.access_tracker import AccessTrackerMixin
from popoto.fields.write_filter import WriteFilterMixin

class Memory(AccessTrackerMixin, WriteFilterMixin, Model):
    agent_id = KeyField()
    content = Field(type=str)
    importance = Field(type=float, default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    certainty = ConfidenceField(initial_confidence=0.5)

    def compute_filter_score(self):
        return self.importance or 0.0
```

Retrieve the top-10 memories ranked by a weighted composite of decay, confidence, access frequency, and write filter priority:

```python
results = Memory.query.filter(agent_id="agent-1").composite_score(
    indexes={
        "relevance": 0.4,      # DecayingSortedField (decay-computed scores)
        "certainty": 0.3,      # ConfidenceField (Bayesian confidence)
        "access_count": 0.2,   # AccessTracker (read frequency)
        "priority": 0.1,       # WriteFilter priority set
    },
    limit=10,
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `indexes` | `dict[str, float]` | *required* | Mapping of field names to weights. Weights are arbitrary positive floats; relative ratios matter, not absolute values. |
| `limit` | `int` | `10` | Maximum number of results to return. |
| `aggregate` | `str` | `"SUM"` | How ZUNIONSTORE combines scores: `"SUM"`, `"MIN"`, or `"MAX"`. |
| `min_score` | `float` | `None` | Optional minimum composite score. Results below this threshold are excluded. |
| `post_filter` | `Callable[[str, float], bool]` | `None` | Optional `(redis_key, score) -> bool` callback. Applied after scoring but before hydration. Return `True` to keep. |
| `co_occurrence_boost` | `dict` | `None` | Optional `{redis_key: weight}` dict from `CoOccurrenceField.propagate()`. Injected as an additional scoring signal. |

### Supported index types

| Index name | Field type | Resolution strategy |
|------------|-----------|-------------------|
| Any `DecayingSortedField` | `DecayingSortedField` / `CyclicDecayField` | Materializes decay-computed scores into a temp ZSET via the existing Lua decay script |
| Any `SortedField` | `SortedFieldMixin` | Uses the existing sorted set directly |
| Any `ConfidenceField` | `ConfidenceField` | Materializes confidence values from the companion hash into a temp ZSET |
| `"access_count"` or `"access_score"` | `AccessTrackerMixin` | Materializes `access_count` from meta hashes into a temp ZSET (uses `SMEMBERS` — see scaling note below) |
| `"priority"` | `WriteFilterMixin` | Uses the `$WF:{Class}:priority` sorted set directly |

> **Scaling note:** The `access_count`/`access_score` index uses `SMEMBERS` to discover all model instances before materializing scores. For models with 100K+ instances, this scan can be expensive. Use `post_filter` or partitioned queries to narrow the result set at that scale.

### CoOccurrence boost

Inject associative retrieval scores from `CoOccurrenceField.propagate()`:

```python
from popoto.fields.co_occurrence_field import CoOccurrenceField

# Get propagated association scores from a seed memory
assoc_field = Memory._meta.fields["associations"]
co_scores = assoc_field.propagate(Memory, seed_pks=["memory_key_1"], depth=2)

# Inject as a boost signal in composite scoring
results = Memory.query.filter(agent_id="agent-1").composite_score(
    indexes={"relevance": 0.3, "certainty": 0.3},
    co_occurrence_boost=co_scores,
    limit=10,
)
```

A record with mediocre decay and confidence scores but a strong co-occurrence association to the seed will surface higher in results.

### Post-filter callback

Use `post_filter` to exclude specific records after scoring but before model hydration:

```python
# Exclude already-used memories
used_keys = {"Memory:key1", "Memory:key2"}
results = Memory.query.filter(agent_id="agent-1").composite_score(
    indexes={"relevance": 0.5, "certainty": 0.5},
    post_filter=lambda key, score: key not in used_keys,
    limit=10,
)
```

### Temp key management

All temporary Redis keys use a `$CSQ:` prefix with a UUID suffix and a 5-second `EXPIRE` for cleanup safety. Keys are deleted immediately after the query completes. Even if the process crashes mid-query, keys auto-expire within 5 seconds.

### Error handling

- Empty `indexes` dict raises `QueryException`
- Invalid field name raises `QueryException` with list of valid fields
- Field without a sorted set index raises `QueryException`
- `"priority"` on a non-`WriteFilterMixin` model raises `QueryException`
- `"access_count"` on a non-`AccessTrackerMixin` model raises `QueryException`
- Missing partition filters for partitioned fields raises `QueryException`
- `limit=0` returns an empty list (no error)

## ExistenceFilter

Wraps a Redis Bloom filter for O(1) probabilistic membership checks. Answers "have I ever stored anything about X?" without touching any sorted set or hash. False positives possible; false negatives essentially impossible.

```python
class Memory(Model):
    topic_fingerprint = ExistenceFilter(error_rate=0.01, capacity=100000)

# Fast pre-check before expensive retrieval
if not Memory.topic_fingerprint.definitely_missing("kubernetes deployments"):
    results = Memory.query.filter(...).top_by_decay(5)
```

!!! note
    Requires the [RedisBloom](https://redis.io/docs/latest/develop/data-types/probabilistic/bloom-filter/) module.

## PredictionLedger

Records prediction-outcome pairs. Before an action, the agent writes what it expects. After, it writes what happened. The mixin computes the delta and stores it as a learning signal.

```python
class TaskAttempt(Model, PredictionLedgerMixin):
    task_id = AutoKeyField()
    agent_id = KeyField()
    predicted_outcome = Field(type=dict)
    actual_outcome = Field(type=dict, null=True)
```

High prediction errors feed back into `ConfidenceField` to reduce trust in the knowledge that informed the bad prediction.

## StreamConsumer

A Redis Streams consumer group framework for background processing. Manages consumer group creation, batch processing, acknowledgment, and dead-letter handling.

```python
consumer = StreamConsumer(
    stream_key="stream:memory_mutations:agent_1",
    group_name="compaction",
    batch_size=50,
    handler=my_handler
)
consumer.run()  # Blocking loop
```

This is generic infrastructure — the processing logic is application code. One use case: pattern crystallization from raw events into `PolicyCache` entries.

## PolicyCache

A reference pattern (not core ORM) showing how to compose all primitives into a reinforcement-learning-style action selection cache. When the compaction pipeline detects repeated successful patterns, it crystallizes them into reusable policies.

```python
class PolicyEntry(Model):
    entry_id = AutoKeyField()
    agent_id = KeyField()
    state_fingerprint = KeyField()
    action_spec = Field(type=dict)
    expected_value = DecayingSortedField()
    confidence = ConfidenceField()
    related_policies = CoOccurrenceField()
```

## ContextAssembler

A query utility that orchestrates the full retrieval pipeline and assembles budget-constrained context payloads for LLM injection.

```python
assembler = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 0.4, "confidence": 0.3, "access": 0.3},
    max_items=10,
    max_tokens=2000
)
context = assembler.assemble(query_cues={"topic": "deployment"}, agent_id="agent-1")
```

The pipeline: ExistenceFilter pre-check, CompositeScoreQuery ranking, CoOccurrence propagation, budget-constrained selection, formatted output.

## Design principles

These primitives follow Popoto's existing patterns:

1. **ORM primitives, not application logic.** Popoto provides fields, mixins, hooks, and query methods. Domain-specific agent memory models are built on top by application developers.

2. **Redis-native everything.** No external brokers or job queues. Lua scripts, sorted sets, streams, Bloom filters — all within the Redis process.

3. **Composable.** Each primitive is independently useful. Use `DecayingSortedField` alone for time-weighted ranking, add `CyclicDecayField` for temporal rhythms and urgency, or combine all twelve for a full cognitive memory system.

4. **Pipeline-safe.** Every operation accepts an optional `pipeline` parameter for atomic execution, consistent with all Popoto field hooks.

## Further reading

- [Popoto Memory Roadmap](../references/popoto-memory-roadmap.md) — full implementation spec with test strategies and benchmarks
- [Epistemic Flow in Cognitive Agent Architectures](../references/epistemic-flow-cognitive-agent-architectures.md) — research background
- [Programmable Memory Systems — Neuroscience Design Spec](../references/programmable-memory-systems-neuroscience-design-spec.md) — neuroscience foundations
- [Behavioral Episode Memory System](https://github.com/tomcounsell/ai/issues/376) — downstream consumer in the AI project
