# Agent Memory

Popoto Agent Memory is a set of ORM primitives that give AI agents programmable memory — records that decay over time, strengthen through use, track confidence, form associations, and surface the right context at the right moment.

These primitives are generic Redis-backed field types, mixins, and query methods. They don't encode any specific agent architecture. You compose them into memory models the same way you'd compose `KeyField`, `SortedField`, and `Relationship` into any Popoto model.

## Why agents need memory primitives

LLMs reason well over content you put in their context window. What they can't do on their own:

- **Prioritize by recency and frequency** — know which records are "hot" right now
- **Learn from outcomes** — track what worked and what didn't
- **Manage certainty** — downweight contradicted knowledge automatically
- **Retrieve associatively** — surface related records without explicit graph queries
- **Filter noise** — avoid storing low-value observations in the first place

Popoto Agent Memory adds these capabilities as composable ORM building blocks. Each one is independently useful; together they form a complete agent memory system.

## Primitives overview

The primitives ship incrementally. Each builds on the ones before it.

| Primitive | What it does | Status |
|-----------|-------------|--------|
| [DecayingSortedField](#decayingsortedfield) | Time-weighted scoring — records lose relevance over time unless refreshed | [#193](https://github.com/tomcounsell/popoto/issues/193) |
| [AccessTracker](#accesstracker) | Tracks read patterns — access count, timestamps, spacing effects | Planned |
| [WriteFilter](#writefilter) | Gates persistence — low-value records silently discarded at write time | Planned |
| [ConfidenceField](#confidencefield) | Bayesian certainty — corroboration strengthens, contradiction weakens | Planned |
| [CoOccurrenceField](#cooccurrencefield) | Weighted associations — co-accessed records strengthen their link | Planned |
| [EventStreamMixin](#eventstreammixin) | Append-only mutation log via Redis Streams | Planned |
| [CompositeScoreQuery](#compositescorequery) | Multi-factor retrieval — combine N sorted indexes with weights | Planned |
| [ExistenceFilter](#existencefilter) | Bloom filter for O(1) "do I know anything about X?" checks | Planned |
| [PredictionLedger](#predictionledger) | Outcome tracking — record predictions, observe results, compute error | Planned |
| [StreamConsumer](#streamconsumer) | Background processing framework for Redis Streams | Planned |
| [PolicyCache](#policycache) | Learned action selection — crystallized state-action-outcome patterns | Planned |
| [ContextAssembler](#contextassembler) | Retrieval-to-injection bridge — assemble LLM-ready context within token budgets | Planned |

## DecayingSortedField

A `SortedField` subclass where records lose retrieval weight over time following a power-law decay curve. This is the foundational primitive — most subsequent primitives depend on time-weighted scoring.

The sorted set score is always a timestamp. A Lua script computes decay-ranked results at query time:

```
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

## AccessTracker

Tracks read access patterns on any model: access count, last-accessed timestamp, and a capped list of access timestamps. Combined with `DecayingSortedField`, this enables spacing-effect-aware scoring — records accessed repeatedly over time rank higher than records accessed many times in a burst.

```python
class Memory(Model, AccessTrackerMixin):
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField()
```

!!! note
    AccessTracker introduces an `on_read()` hook to the field protocol. Details TBD during implementation.

## WriteFilter

Gates persistence based on a scoring function evaluated at write time. Records below a threshold are silently discarded. Records above a high threshold are tagged for priority processing.

```python
class Memory(Model, WriteFilterMixin):
    agent_id = KeyField()
    content = Field(type=str)

    @classmethod
    def compute_filter_score(cls, instance):
        # Application logic — Popoto provides the gating mechanism
        return score_between_0_and_1
```

You provide the scoring logic; Popoto provides the gate. This keeps low-value observations out of the index, reducing noise in retrieval.

## ConfidenceField

A field that maintains a Bayesian confidence score updated atomically via Lua script. Each update provides a signal (corroborate or contradict) with a weight. The score converges as evidence accumulates.

```python
class Knowledge(Model):
    topic = KeyField()
    claim = Field(type=str)
    confidence = ConfidenceField(default=0.5)

# Corroborate
knowledge.confidence.update(signal=0.9)

# Contradict
knowledge.confidence.update(signal=0.1)
```

When combined with `DecayingSortedField` or `CompositeScoreQuery`, confidence acts as a multiplier on retrieval weight — low-confidence records are naturally deprioritized.

## CoOccurrenceField

Maintains weighted, bidirectional edges between model instances using sorted sets. Weights strengthen when records are accessed together and decay when not reinforced.

```python
class Memory(Model):
    memory_id = AutoKeyField()
    content = Field(type=str)
    associations = CoOccurrenceField(symmetric=True, max_edges=500)

# Strengthen link between two memories
Memory.associations.strengthen(memory_a.pk, memory_b.pk, delta=0.05)

# Retrieve associated memories with graph propagation
related = Memory.associations.propagate(
    seed_pks=[memory_a.pk],
    depth=2,
    decay_per_hop=0.5
)
```

Graph propagation uses BFS with exponential weight decay per hop. Direct neighbors get full weight; two hops away get `weight × 0.25`.

## EventStreamMixin

Automatically appends to a Redis Stream on every save, update, or delete. This is infrastructure for background processing — the mixin writes events, your application consumes them.

```python
class Memory(Model, EventStreamMixin):
    class Meta:
        stream_name = "memory_mutations"
        max_stream_length = 10000
```

Every mutation produces a stream entry with model class, primary key, operation type, and metadata. Processing is handled by `StreamConsumer`.

## CompositeScoreQuery

Combines multiple sorted set indexes via `ZUNIONSTORE` with configurable weights, returning top-K results by composite score. This is where all the scoring primitives converge.

```python
results = Memory.query.composite_score(
    indexes={
        "relevance": 0.4,      # DecayingSortedField
        "confidence": 0.3,     # ConfidenceField
        "access_score": 0.2,   # AccessTracker
        "priority": 0.1,       # WriteFilter priority set
    },
    limit=10
)
```

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

3. **Composable.** Each primitive is independently useful. Use `DecayingSortedField` alone for time-weighted ranking, or combine all twelve for a full cognitive memory system.

4. **Pipeline-safe.** Every operation accepts an optional `pipeline` parameter for atomic execution, consistent with all Popoto field hooks.

## Further reading

- [Popoto Memory Roadmap](../references/popoto-memory-roadmap.md) — full implementation spec with test strategies and benchmarks
- [Epistemic Flow in Cognitive Agent Architectures](../references/epistemic-flow-cognitive-agent-architectures.md) — research background
- [Programmable Memory Systems — Neuroscience Design Spec](../references/programmable-memory-systems-neuroscience-design-spec.md) — neuroscience foundations
- [Behavioral Episode Memory System](https://github.com/tomcounsell/ai/issues/376) — downstream consumer in the AI project
