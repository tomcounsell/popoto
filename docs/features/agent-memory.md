# Agent Memory

> **Getting Started?** See the [Agent Memory Quickstart](../guides/agent-memory-quickstart.md) for a progressive 5-level adoption guide.

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
| [ObservationProtocol](#observationprotocol) | Outcome-driven memory effects — acted/dismissed/deferred/contradicted/used | Shipped ([PR #206](https://github.com/tomcounsell/popoto/pull/206)) |
| [WriteFilter](#writefilter) | Gates persistence — low-value records silently discarded at write time | Shipped ([PR #214](https://github.com/tomcounsell/popoto/pull/214)) |
| [ConfidenceField](#confidencefield) | Bayesian certainty — corroboration strengthens, contradiction weakens | Shipped ([PR #215](https://github.com/tomcounsell/popoto/pull/215)) |
| [CoOccurrenceField](#cooccurrencefield) | Weighted associations — co-accessed records strengthen their link | Shipped ([PR #218](https://github.com/tomcounsell/popoto/pull/218)) |
| [EventStreamMixin](#eventstreammixin) | Append-only mutation log via Redis Streams | Shipped |
| [CompositeScoreQuery](#compositescorequery) | Multi-factor retrieval — combine N sorted indexes with weights | Shipped |
| [ExistenceFilter](#existencefilter) | Bloom filter for O(1) "do I know anything about X?" checks | Shipped |
| [BM25Field](hybrid-retrieval.md#bm25field) | Ranked keyword search with BM25 scoring in Redis sorted sets | Shipped ([PR #306](https://github.com/tomcounsell/popoto/pull/306)) |
| [Hybrid Retrieval (RRF)](hybrid-retrieval.md) | Multi-signal fusion — combine keyword, semantic, and graph signals via Reciprocal Rank Fusion | Shipped ([PR #306](https://github.com/tomcounsell/popoto/pull/306)) |
| [FrequencySketch](#frequencysketch) | Count-Min Sketch for approximate frequency counting | Shipped |
| [PredictionLedger](#predictionledger) | Outcome tracking — record predictions, observe results, compute error | Shipped |
| [StreamConsumer](#streamconsumer) | Background processing framework for Redis Streams | Shipped ([PR #238](https://github.com/tomcounsell/popoto/pull/238)) |
| [PolicyCache](#policycache) | Learned action selection — crystallized state-action-outcome patterns | Shipped ([PR #239](https://github.com/tomcounsell/popoto/pull/239)) |
| [ContextAssembler](#contextassembler) | Retrieval-to-injection bridge — assemble LLM-ready context within token budgets | Shipped |
| [MemoryLifecycle](#memorylifecycle) | Policy layer orchestrating episodic→semantic promotion, confidence-aware forgetting, and tombstones | Shipped ([PR #396](https://github.com/tomcounsell/popoto/pull/396), [#491](https://github.com/tomcounsell/popoto/issues/491)) |
| [TagField](#tagfield) | Optional multi-value scoping — agent/project/arbitrary tags for a central shared Redis | Shipped ([#492](https://github.com/tomcounsell/popoto/issues/492)) |

## DecayingSortedField

A `SortedField` subclass where records lose retrieval weight over time following a power-law decay curve. This is the foundational primitive — most subsequent primitives depend on time-weighted scoring.

The sorted set score is always a timestamp. A Lua script computes decay-ranked results at query time:

```text
decayed_score = base_score × elapsed_days ^ (-decay_rate)
```

With the default `decay_rate=0.1` (empirically tuned in sweep 2026-04-17; prior default was `0.5`), a record scores 1.0 after 1 day, 0.87 after 4 days, and 0.63 after 100 days.

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
| `decay_rate` | `float` | `0.1` | Controls how fast scores drop. Higher = faster decay. (Empirically tuned in sweep 2026-04-17; prior default was `0.5`.) |
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

A record with `importance=5.0` stays relevant longer than one with `importance=1.0` — the multiplier is `score^(1/decay_rate)`. At the default `decay_rate=0.1` importance strongly dominates recency; at `decay_rate=0.5` the multiplier is 25×.

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

With `decay_rate=0.5`, lifetime ≈ score² days (at the current default `decay_rate=0.1`, lifetime grows much more slowly with time — see [Tuning Magic Numbers](../guides/tuning-magic-numbers.md) for the empirical rationale):

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
        decay_rate=0.5,  # override default (0.1) for faster forgetting
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
| `decay_rate` | `float` | `0.1` | Power-law decay exponent (inherited). Empirically tuned in sweep 2026-04-17; prior default was `0.5`. |
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
| `_staged_ttl_seconds` | `int` | `86400` | TTL applied to the staged list on every `on_read()` call (24h default). Magic-number tuning knob — increase if your stage→confirm cadence exceeds 24h. |

### Staged-read TTL contract

Staged reads self-expire after `_staged_ttl_seconds` (default 24h). This bounds
Redis memory for records that are read but **never confirmed** without truncating
the count for records that confirm within the window.

**Contract:** Call `confirm_access()` within `_staged_ttl_seconds` of staging reads
via `on_read()`. Reads left unconfirmed past the TTL window are **permanently dropped**
from `access_count` / `last_accessed` by design — the `CONFIRM_ACCESS_LUA` script
finds an empty staged key and returns 0. Set `_staged_ttl_seconds` higher if your
agent's stage→confirm cadence exceeds 24h.

### Suppressing tracking for bulk operations

Use `no_track()` on the query builder to prevent `on_read()` from firing during
internal operations like reindexing, migration, or lifecycle ticks:

```python
# These reads won't be tracked
Memory.query.filter(agent_id="agent-1").no_track().all()
```

`MemoryLifecycle.tick()` uses `.no_track()` automatically — a periodic tick
produces **zero** new staged entries and does not inflate access-frequency signals.

Note: `Model.query.all()` (without `.filter()`) is non-tracking by design and
does not call `on_read()`. The tracking path is the `QueryBuilder`
(`.filter().all()`); chain `.no_track()` there for any internal sweep that
should not count as a user-originated read.

### Delete cleanup

When a tracked model instance is deleted, all three AccessTracker Redis keys (staged, access_log, meta) are automatically removed.

### Redis key patterns

| Key | Type | Purpose |
|-----|------|---------|
| `$AT:{ClassName}:staged:{pk}` | List | Pending read timestamps |
| `$AT:{ClassName}:access_log:{pk}` | List | Confirmed access timestamps (capped) |
| `$AT:{ClassName}:meta:{pk}` | Hash | `access_count` (int) and `last_accessed` (float) |

## ObservationProtocol

Provides lifecycle hooks for outcome-driven memory effects. The application reports how the agent used retrieved memories (acted, dismissed, deferred, contradicted, used); the ORM applies effects atomically.

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
        decay_rate=0.5,  # override default (0.1) for faster forgetting
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

### Five outcomes

| Outcome | Meaning | Effects |
|---------|---------|---------|
| `acted` | Agent used the memory | `touch()`, `confirm_access()`, `strengthen_cycle(1.2)`, `resolve_pressure()`, corroborate confidence (signal=0.9) |
| `dismissed` | Agent explicitly rejected | `discard_staged_access()`, `weaken_cycle(0.8)` |
| `deferred` | Agent didn't address it | `discard_staged_access()` only — pressure keeps building |
| `contradicted` | Agent explicitly contradicted | `discard_staged_access()`, `weaken_cycle(0.5)`, contradict confidence (signal=0.1), auto-discharge pressure if confidence < 0.1 |
| `used` | Agent reasoned over it without citing | `confirm_access()` only — no strength signal emitted |

See [Metacognitive Layer](metacognitive-layer.md) for when to use `"used"` vs `"acted"` and how `AdaptiveAssembler` tracks outcome quality over time.

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

# Score 0.05 < min_threshold (0.1) — silently discarded
Memory(agent_id="a1", content="noise", importance=0.05).save()

# Score 0.5 — persisted normally
Memory(agent_id="a1", content="useful", importance=0.5).save()

# Score 0.9 >= priority_threshold (0.7) — persisted AND added to priority set
Memory(agent_id="a1", content="critical", importance=0.9).save()
```

You provide the scoring logic; Popoto provides the gate. This keeps low-value observations out of the index, reducing noise in retrieval.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `_wf_min_threshold` | `0.1` | Below this score, `save()` silently discards via `SkipSaveException`. (Empirically tuned in sweep 2026-04-17; prior default was `0.2`.) |
| `_wf_priority_threshold` | `0.7` | At or above this score, record is added to `$WF:{ClassName}:priority` sorted set |

Priority-tagged records are stored in a Redis sorted set keyed `$WF:{ClassName}:priority`, scored by the filter score. On `delete()`, cleanup removes the record from the priority set automatically.

See [fields.md](../fields.md#writefiltermixin) for the full field reference.

## ConfidenceField

A `Field` subclass that tracks confidence metadata per member, updated atomically via Lua script using a capped-evidence Bayesian rule. Within the evidence window (default cap: 20) updates form an exact running mean — order-invariant and prior-weighted. Beyond the cap, a fixed gain produces bounded exponential forgetting.

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

### Capped-evidence update formula

```
n_eff = min(evidence_count + 1, cap)   # cap default: 20
new_confidence = confidence + (signal - confidence) / (n_eff + 1)
```

Within the evidence window (`evidence_count < cap`), this is an exact running mean over `{initial_confidence, s1, …, sn}` — order-invariant to ~1e-12. Beyond the cap, the gain is fixed at `1/(cap+1)`, producing bounded exponential forgetting (~15 consecutive contradictions to cross 0.5 from 0.9 at cap 20). Results are clamped to `[0, 1]`.

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

See [ConfidenceField feature docs](confidence-field.md) for the full reference including convergence behavior and Redis key patterns. See [API Reference: ConfidenceField](../reference/popoto/fields/confidence_field.md) for the complete method signatures.

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

See [CoOccurrenceField field docs](co-occurrence-field.md) for the full reference including methods, Redis key patterns, and synergy with other memory fields.

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
| `temperature` | `float` | `1.0` | Scales composite scores by dividing each by this value. Low values (0.02-0.1) sharpen discrimination; high values (2.0+) flatten scores. Must be > 0. `min_score` filters on raw scores before temperature scaling; `post_filter` receives scaled scores. |

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

A Bloom filter for O(1) probabilistic membership checks. Answers "have I ever stored anything about X?" without touching any sorted set or hash. False positives are possible; false negatives are impossible.

Shipped in [PR #225](https://github.com/tomcounsell/popoto/pull/225).

Implemented entirely with Redis strings (`SETBIT`/`GETBIT`) and Lua scripts. **No Redis modules required** -- works on both Redis and Valkey. The original roadmap specified RedisBloom module commands (`BF.ADD`/`BF.EXISTS`), but the implementation uses pure Lua with the Kirschner-Mitzenmacher double hashing optimization to maintain Valkey compatibility.

### Basic usage

```python
from popoto import Model, KeyField, Field
from popoto.fields.existence_filter import ExistenceFilter

class Memory(Model):
    agent_id = KeyField()
    topic = Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.topic,
    )
```

The `bloom` field automatically adds fingerprints to the Bloom filter on save. Check membership before running expensive queries:

```python
# Fast pre-check before expensive retrieval
if not Memory.bloom.definitely_missing(Memory, "kubernetes deployments"):
    results = Memory.query.filter(agent_id="agent-1").top_by_decay(5)
else:
    results = []  # skip retrieval entirely
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `error_rate` | `float` | `0.01` | Target false positive rate. Lower = more bits required. |
| `capacity` | `int` | `100_000` | Expected number of distinct items. Exceeding this degrades the error rate. |
| `fingerprint_fn` | `Callable` | `None` | Takes a model instance, returns a string fingerprint. Falls back to `redis_key` if not set. |

### Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `might_exist(model_class, fingerprint)` | `bool` | True if fingerprint is possibly present (may be false positive). |
| `definitely_missing(model_class, fingerprint)` | `bool` | True if fingerprint is guaranteed absent. Use this for pre-filtering. |
| `fill_ratio(model_class)` | `float` | Proportion of set bits (0.0-1.0). Monitor for capacity warnings. |

### How it works

A Bloom filter is a bit array of size `m` with `k` hash functions. Parameters are derived automatically from `error_rate` and `capacity`:

- `m = -capacity * ln(error_rate) / (ln(2)^2)` -- total bits
- `k = (m / capacity) * ln(2)` -- optimal number of hash functions

Hash functions use the Kirschner-Mitzenmacher double hashing optimization (DJB2 + FNV-1), computed in Lua for atomic operations.

### Design decisions

- **on_delete() is a no-op**: Bloom filters do not support removal. Stale positives are harmless for a pre-filter use case.
- **WriteFilter integration**: Records rejected by `WriteFilterMixin` are never added to the Bloom filter (the existing save flow raises `SkipSaveException` before `on_save()` hooks run).
- **Pipeline support**: `on_save()` accepts an optional Redis pipeline for atomic batch saves.

## FrequencySketch

A Count-Min Sketch for approximate frequency counting. Tracks how many times a fingerprint has been saved, with possible overcounting but never undercounting.

Shipped in [PR #225](https://github.com/tomcounsell/popoto/pull/225).

Implemented entirely with Redis hashes (`HINCRBY`/`HGET`) and Lua scripts. **No Redis modules required** -- works on both Redis and Valkey. Like ExistenceFilter, the original roadmap specified RedisBloom module commands (`CMS.INCRBY`/`CMS.QUERY`), but the implementation uses pure Lua for Valkey compatibility.

### Basic usage

```python
from popoto import Model, KeyField, Field
from popoto.fields.existence_filter import FrequencySketch

class Memory(Model):
    agent_id = KeyField()
    topic = Field(type=str)
    freq = FrequencySketch(
        fingerprint_fn=lambda inst: inst.topic,
    )
```

Query approximate frequency:

```python
count = Memory.freq.get_frequency(Memory, "kubernetes")
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `width` | `int` | `2003` | Number of counters per row (prime recommended). Higher = less overcounting. |
| `depth` | `int` | `7` | Number of independent hash functions / rows (1–7). Higher = more accurate. |
| `fingerprint_fn` | `Callable` | `None` | Takes a model instance, returns a string fingerprint. Falls back to `redis_key` if not set. |

### Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `get_frequency(model_class, fingerprint)` | `int` | Approximate count. May overcount, never undercounts. Returns 0 for unseen fingerprints. |

### Design decisions

- **on_delete() is a no-op**: CMS counters are monotonically increasing. Decrementing would violate the "never undercount" guarantee.
- **Pipeline support**: `on_save()` accepts an optional Redis pipeline for atomic batch saves.

### Combining ExistenceFilter and FrequencySketch

Both fields can be used together on the same model for fast existence checks and frequency tracking:

```python
class Memory(Model):
    agent_id = KeyField()
    topic = Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.topic,
    )
    freq = FrequencySketch(
        fingerprint_fn=lambda inst: inst.topic,
    )

# Pre-filter + frequency-weighted retrieval
if not Memory.bloom.definitely_missing(Memory, "kubernetes"):
    frequency = Memory.freq.get_frequency(Memory, "kubernetes")
    results = Memory.query.filter(agent_id="agent-1").top_by_decay(10)
```

## PredictionLedger

Records prediction-outcome pairs. Before an action, the agent writes what it expects. After, it writes what happened. The mixin computes the delta and stores it as a learning signal. High prediction errors feed back into `ConfidenceField` to reduce trust in the knowledge that informed the bad prediction. Auto-resolution via `ObservationProtocol` handles the common case where outcomes are inferred from behavior rather than explicitly reported.

### Setup

Add `PredictionLedgerMixin` as a base class alongside `Model`:

```python
from popoto import Model, UniqueKeyField, StringField
from popoto.fields.prediction_ledger import PredictionLedgerMixin
from popoto.fields.confidence_field import ConfidenceField

class Memory(PredictionLedgerMixin, Model):
    key = UniqueKeyField()
    content = StringField()
    certainty = ConfidenceField()

    _pl_partition = "default"
```

### Recording and resolving predictions

```python
# Agent records what it expects before acting
memory = Memory.create(key="fact1", content="sky is blue")
PredictionLedgerMixin.record_prediction(memory, predicted={"relevance": 0.9})

# After acting, resolve with actual outcome
error = PredictionLedgerMixin.resolve_prediction(memory, actual={"relevance": 0.3})
# error ≈ 0.6  (normalized absolute error)
```

### Auto-resolution via ObservationProtocol

When `ObservationProtocol.on_context_used()` fires with an `acted`, `dismissed`, `contradicted`, or `used` outcome, it automatically calls `auto_resolve()` on instances that use `PredictionLedgerMixin`:

| Outcome | Default Error | Interpretation |
|---------|--------------|----------------|
| `acted` | 0.1 | Prediction was roughly correct |
| `dismissed` | 0.5 | Wrong timing or relevance |
| `contradicted` | 0.9 | Factually wrong |
| `used` | 0.3 | Prediction approximately correct — memory informed reasoning |

These values are configurable via the `_pl_auto_resolve_errors` class attribute.

### ConfidenceField feedback

When prediction error exceeds `_pl_confidence_error_threshold` (default 0.7), `PredictionLedgerMixin` automatically calls `ConfidenceField.update_confidence(signal=0.2)` to reduce trust in the knowledge that informed the prediction.

### Querying prediction errors

```python
# Find instances with highest prediction errors
worst = PredictionLedgerMixin.get_highest_errors(Memory, partition="default", limit=10)
# Returns: [(redis_key, error_score), ...]

# Aggregate errors grouped by time or error band
summary = PredictionLedgerMixin.error_summary(Memory, partition="default")
# Returns: {"mean": 0.42, "p90": 0.81, ...} — see Metacognitive Layer for group_by options

# Read prediction data for a specific instance
data = PredictionLedgerMixin.get_prediction_data(memory)
# Returns: {"predicted": {...}, "resolved": True, "prediction_error": 0.6, ...}
```

For temporal and band-grouped aggregations via `error_summary(group_by=...)`, see [Metacognitive Layer](metacognitive-layer.md#predictionledgermixin-errorsummary).

### Class attributes

| Attribute | Default | Description |
|-----------|---------|-------------|
| `_pl_partition` | `"default"` | Partition key for the error sorted set |
| `_pl_confidence_error_threshold` | `0.7` | Error above which confidence is reduced |
| `_pl_confidence_low_signal` | `0.2` | Signal value sent to ConfidenceField |
| `_pl_auto_resolve_errors` | `{"acted": 0.1, "dismissed": 0.5, "contradicted": 0.9, "used": 0.3}` | Outcome-to-error mapping |

### Redis key patterns

- `$PL:{ClassName}:meta:{pk}` -- hash storing prediction metadata (msgpack-encoded)
- `$PL:{ClassName}:errors:{partition}` -- sorted set of instance keys scored by |error|

### Key properties

- **Idempotent resolution**: Resolving an already-resolved prediction is a no-op (returns `None`)
- **Atomic**: Resolution uses a Lua script for atomic read-compute-write-ZADD
- **Graceful degradation**: Works without ConfidenceField or EventStreamMixin (features are additive)
- **Valkey compatible**: Uses only HSET, HGET, ZADD, EVAL -- no Redis modules
- **Pipeline support**: All operations accept an optional `pipeline` parameter

## StreamConsumer

A Redis Streams consumer group framework for background processing. Manages consumer group creation, batch reading, acknowledgment, dead-letter handling, and pending entry recovery via XAUTOCLAIM.

Shipped in [PR #238](https://github.com/tomcounsell/popoto/pull/238).

### Basic usage

```python
from popoto.streams import StreamConsumer

async def my_handler(entries):
    """Process a batch of stream entries."""
    for entry_id, fields in entries:
        print(f"Processing {entry_id}: {fields}")

consumer = StreamConsumer(
    stream_key="stream:memory_mutations:agent_1",
    group_name="compaction",
    consumer_name="worker-1",
    handler=my_handler,
)

# Blocking loop — runs until stopped
await consumer.run()

# Or process one batch (useful for testing)
count = await consumer.process_batch()
```

Sync wrappers are available for scripts without an event loop:

```python
consumer.run_sync()              # blocking loop
count = consumer.process_batch_sync()  # single batch
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `stream_key` | `str` | *required* | Redis stream key (e.g., `"stream:memory_mutations"`). |
| `group_name` | `str` | *required* | Consumer group name for XREADGROUP. |
| `consumer_name` | `str` | *required* | This consumer's name within the group. |
| `handler` | `Callable` | *required* | Async function receiving `list[(entry_id, fields_dict)]`. All values decoded to str. |
| `batch_size` | `int` | `50` | XREADGROUP COUNT — entries per batch. |
| `block_ms` | `int` | `5000` | XREADGROUP BLOCK timeout in milliseconds. |
| `max_retries` | `int` | `3` | Handler invocation threshold before dead-lettering. |
| `claim_timeout_ms` | `int` | `180_000` | XAUTOCLAIM idle timeout (3 minutes). Entries idle longer are reclaimed. |
| `dead_letter_max_length` | `int` | `None` | Optional MAXLEN for the dead-letter stream. |

### Dead-letter handling

Entries that fail processing more than `max_retries` times (tracked via handler invocation counter) are moved to `dead:{stream_key}` with metadata:

| Field | Description |
|-------|-------------|
| `original_stream` | Source stream key |
| `original_id` | Original entry ID |
| `failure_count` | Number of delivery attempts |
| `last_error` | Error description |
| `dead_letter_ts` | Timestamp when dead-lettered |

### XAUTOCLAIM recovery

On each `process_batch()` call, the consumer checks for pending entries from crashed consumers. Entries idle longer than `claim_timeout_ms` are reclaimed via `XAUTOCLAIM` and re-delivered through the full decode → handler → XACK pipeline, restoring at-least-once delivery semantics. Handlers must be idempotent because a reclaimed entry may have been partially processed by the crashed consumer. Entries exceeding `max_retries` handler invocations are dead-lettered instead.

`process_batch()` returns the total number of entries handled in the cycle — both new entries and reclaimed entries are counted (new + reclaimed).

The `failure_count` field in dead-letter entries records the server-side delivery count (XPENDING `times_delivered`) at the time the entry was dead-lettered. This value may exceed the number of handler invocations because XAUTOCLAIM claim cycles increment `times_delivered` independently of handler execution.

### Graceful shutdown

```python
consumer.stop()  # exits run() after current batch completes
```

### EventStreamMixin synergy

StreamConsumer reads the streams that `EventStreamMixin` writes. No import dependency — it operates on raw stream keys:

```python
from popoto import Model, EventStreamMixin, UniqueKeyField, StringField
from popoto.streams import StreamConsumer

class Memory(EventStreamMixin, Model):
    _stream_name = "memory_mutations"
    key = UniqueKeyField()
    content = StringField()

# Producer: saves write to stream:memory_mutations
Memory(key="fact1", content="hello").save()

# Consumer: reads from the same stream
consumer = StreamConsumer(
    stream_key="stream:memory_mutations",
    group_name="compaction",
    consumer_name="worker-1",
    handler=my_handler,
)
count = consumer.process_batch_sync()
```

This is generic infrastructure — the processing logic is application code. One use case: pattern crystallization from raw events into `PolicyCache` entries.

## PolicyCache

A reference recipe (not core ORM) composing all shipped primitives into a reinforcement-learning-style action selection cache. When a StreamConsumer detects repeated successful patterns, it crystallizes them into reusable `PolicyEntry` records. Agents query policies by state fingerprint and select actions based on Q-value, confidence, and co-occurrence.

For the full guide with architecture diagrams, tuning advice, and design rationale, see [PolicyCache Recipe](../guides/policy-cache-recipe.md).

Shipped in [PR #239](https://github.com/tomcounsell/popoto/pull/239).

### Basic usage

```python
from decimal import Decimal

from popoto.recipes.policy_cache import (
    PolicyEntry, compute_fingerprint, update_q_value,
    crystallization_handler,
)

# Create a policy directly — seed q_value at construction
fp = compute_fingerprint({"task": "deploy", "env": "staging"})
policy = PolicyEntry(
    agent_id="agent-1",
    state_fingerprint=fp,
    state_features={"task": "deploy", "env": "staging"},
    action_type="run_playbook",
    action_spec={"playbook": "deploy.yml"},
    q_value=Decimal("0.0"),  # q_value lives in the model hash, not the ZSET score
)
policy.save()  # persists both model fields and q_value in one round-trip

# Update Q-value via temporal difference learning
td_error = update_q_value(policy, reward=1.0)

# Or let crystallization detect patterns automatically
from popoto.streams import StreamConsumer

consumer = StreamConsumer(
    stream_key="stream:policy_mutations",
    group_name="crystallizer",
    consumer_name="worker-1",
    handler=crystallization_handler,
)
```

### PolicyEntry model

```python
class PolicyEntry(EventStreamMixin, AccessTrackerMixin, PredictionLedgerMixin, Model):
    entry_id = AutoKeyField()
    agent_id = KeyField()
    state_fingerprint = KeyField()
    state_features = Field()                      # JSON dict
    action_type = KeyField()
    action_spec = Field()                         # JSON dict
    q_value = DecimalField(default=Decimal("0"))
    expected_value = DecayingSortedField(
        partition_by="agent_id",
        base_score_field="q_value",
    )
    confidence = ConfidenceField(initial_confidence=0.5)
    related_policies = CoOccurrenceField(symmetric=True, max_edges=100)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.state_fingerprint,
    )
```

WriteFilterMixin is intentionally excluded — the crystallization handler IS the write gate (Wilson CI > 0.6 threshold).

### Key functions

| Function | Description |
|----------|-------------|
| `compute_fingerprint(features, include_fields=None, include_timestamp=False)` | SHA-256 hash (16 hex chars) of state features |
| `update_q_value(instance, reward, max_future_q=0.0, alpha=0.1, gamma=0.95)` | Atomic TD(0) Q-value update via Lua script |
| `initialize_q_value(instance, initial_q=0.0)` | Set initial `q_value` in the model hash and save (no timestamp override) |
| `wilson_ci_lower(successes, total, z=1.96)` | Wilson score confidence interval lower bound |
| `chi_squared_uniform(observed, expected_per_bucket)` | Chi-squared test against uniform distribution |
| `crystallization_handler(entries)` | StreamConsumer handler for pattern detection |
| `temporal_discovery_handler(entries)` | StreamConsumer handler for cycle discovery |

### Tuning constants

| Constant | Default | Description |
|----------|---------|-------------|
| `MIN_EVENTS_FOR_CRYSTALLIZATION` | `3` | Min events before crystallization |
| `WILSON_CI_THRESHOLD` | `0.6` | Wilson CI lower bound threshold |
| `TD_ALPHA` | `0.1` | Q-value learning rate |
| `TD_GAMMA` | `0.95` | Q-value discount factor |
| `CHI_SQUARED_P_THRESHOLD` | `0.05` | p-value threshold for temporal discovery |
| `INITIAL_CYCLE_AMPLITUDE` | `0.5` | Initial amplitude for discovered cycles |

## ContextAssembler

A capstone recipe that orchestrates all shipped Popoto memory primitives into a single `assemble()` call. It combines query-driven retrieval (pull path) with proactive surfacing (push path), applies token budgets, and formats output for LLM context injection.

This is Step 12 of the [Popoto Memory Roadmap](../guides/popoto-memory-roadmap.md).

### Pipeline

```
Application
    │
    ▼
assemble(query_cues, agent_id)
    │
    ├─── Pull Path (query-driven) ──────────────────────────┐
    │    1. ExistenceFilter pre-check                       │
    │       └─ Skip retrieval if all cues definitely_missing│
    │    2. CompositeScoreQuery                             │
    │       └─ Multi-factor ranked retrieval via ZUNIONSTORE│
    │    3. CoOccurrence propagation                        │
    │       └─ BFS expansion to discover associated records │
    │                                                       │
    ├─── Push Path (proactive surfacing) ───────────────────┤
    │    1. CyclicDecayField scan via top_by_decay()        │
    │    2. Filter by surfacing_threshold                   │
    │                                                       │
    ├─── Merge + Re-rank ──────────────────────────────────-┤
    │    1. Deduplicate by redis_key                        │
    │    2. Apply max_items cap                             │
    │    3. Apply max_tokens budget (if set)                │
    │                                                       │
    ├─── Post-retrieval Effects ────────────────────────────┤
    │    1. on_read() for pull-path records                 │
    │    2. on_surfaced() for push-path (creates Recalls)   │
    │    3. Competitive suppression of non-selected pulls   │
    │                                                       │
    └─── Format ────────────────────────────────────────────┘
         structured (JSON) | xml | natural
         │
         ▼
    AssemblyResult
```

### Synergy Table

| Primitive | Role in ContextAssembler |
|-----------|--------------------------|
| DecayingSortedField | Score index for CompositeScoreQuery |
| CyclicDecayField | Push-path proactive surfacing |
| ConfidenceField | Score index + competitive suppression |
| CoOccurrenceField | Pull-path candidate expansion via BFS |
| ExistenceFilter | Pull-path pre-check (skip if absent) |
| AccessTrackerMixin | on_read post-effect tracking |
| ObservationProtocol | on_read / on_surfaced dispatch |
| RecallProposal | Created for push-path records |
| WriteFilterMixin | Priority score in composite |
| EventStreamMixin | Mutation logging (via model save) |
| PredictionLedgerMixin | Outcome tracking (via model save) |
| CompositeScoreQuery | Multi-factor ranked retrieval |

### API Reference

#### `ContextAssembler.__init__()`

```python
from popoto.recipes.context_assembler import ContextAssembler

assembler = ContextAssembler(
    model_class,              # Popoto Model class to query
    score_weights,            # Dict mapping field names to weights for CompositeScoreQuery
    max_items=10,             # Maximum records to return
    max_tokens=None,          # Optional token budget (enforced; first record always admitted)
    surfacing_threshold=0.5,  # Minimum score for push-path records
    propagation_depth=2,      # BFS depth for CoOccurrence propagation
    output_format="structured",  # "structured" (JSON), "xml", or "natural"
    token_counter=None,       # Optional callable(serialized_text: str) -> int; default: stdlib heuristic
    # Keyword-only:
    retrieval_mode="auto",    # "auto" (default), "hybrid", or "lexical"
    confidence_gate_threshold=None,  # float | None (default). See "Confidence Gate" below.
    confidence_gate_mode="refuse",   # "refuse" (default) or "flag"
    graph_traversal_relationship_fields=None,  # list[str] | None (default). See "Graph Traversal" below.
)
```

The assembler auto-detects which fields are present on `model_class` and adapts the pipeline accordingly. Models without `ExistenceFilter` skip the pre-check; models without `CoOccurrenceField` skip propagation; models without `CyclicDecayField` skip the push path entirely.

**Raises** `QueryException` at construction if `retrieval_mode="hybrid"` is
requested but the model lacks `BM25Field`/`EmbeddingField`; if
`confidence_gate_threshold` is set and `confidence_gate_mode` is not one of
`{"refuse", "flag"}`; or if `confidence_gate_threshold` is set but the model
lacks a `ConfidenceField`.

#### `ContextAssembler.assemble()`

```python
result = assembler.assemble(
    query_cues=None,          # Dict of query cues (e.g., {"topic": "deploy"})
    agent_id=None,            # Agent ID for partition filtering
    partition_filters=None,   # Dict of partition key-value pairs
)
```

Returns an `AssemblyResult`. If `query_cues` is `None`, the pull path is skipped and only push-path (proactive) results are returned.

Pass `assess_quality=True` to also run a pre-retrieval quality probe; the resulting `RetrievalQuality` dataclass is available in `result.metadata["quality"]`. For self-calibrating assemblies that fall back gracefully when quality is low, see `AdaptiveAssembler` in the [Metacognitive Layer](metacognitive-layer.md).

#### `AssemblyResult`

```python
@dataclass
class AssemblyResult:
    records: list       # All selected instances (pull + push, deduplicated)
    proactive: list     # Push-path subset of records
    formatted: str      # LLM-ready formatted string
    metadata: dict      # Scores, token counts, timing info
```

The `metadata` dict contains:

| Key | Description |
|-----|-------------|
| `pull_count` | Number of pull-path records in final selection |
| `push_count` | Number of push-path records in final selection |
| `token_count` | Estimated total tokens across selected records |
| `timing_ms` | Wall-clock time for the full pipeline |
| `total_candidates` | Total candidates before budget selection |
| `gate` | Confidence-gate decision dict. Present **only** when `confidence_gate_threshold` was set at construction — see [Confidence Gate](#confidence-gate) below. |

### Usage Examples

#### Pull-only assembly (query-driven retrieval)

```python
from popoto.recipes.context_assembler import ContextAssembler

assembler = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 0.6, "confidence": 0.3},
    max_items=10,
    max_tokens=4000,
)

result = assembler.assemble(
    query_cues={"topic": "deployment"},
    agent_id="agent-1",
)
print(result.formatted)       # JSON array of records
print(result.metadata)        # {"pull_count": 5, "push_count": 0, ...}
```

#### Push-only assembly (proactive surfacing)

```python
assembler = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 1.0},
    surfacing_threshold=0.3,
)

# No query_cues: only push-path runs
result = assembler.assemble(agent_id="agent-1")
for record in result.proactive:
    print(f"Proactively surfaced: {record.content}")
```

#### Combined pull + push assembly

```python
assembler = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 0.5, "urgency": 0.3, "confidence": 0.2},
    max_items=15,
    max_tokens=8000,
    surfacing_threshold=0.4,
    output_format="xml",
)

result = assembler.assemble(
    query_cues={"topic": "incident response"},
    agent_id="agent-1",
)
# result.records contains both pull and push results, deduplicated
# result.proactive is the push-path subset
# result.formatted is XML-formatted for LLM injection
```

#### Custom token counter

`token_counter` receives the serialized per-record string — the exact text slice
the formatter emits for each record (a JSON object indented inside the array for
`output_format="structured"`, a `<record>...</record>` block for `"xml"`, or a
`key: value` line for `"natural"`). It must return a non-negative `int`.

```python
import tiktoken

enc = tiktoken.encoding_for_model("gpt-4")

assembler = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 1.0},
    max_tokens=4000,
    token_counter=lambda text: len(enc.encode(text)),
)
```

### Confidence Gate

An opt-in gate (issue #463) that lets `assemble()` decline to inject a
pull-path answer when it isn't confident enough. It is off by default and has
**no shipped default threshold** — a caller must explicitly pass
`confidence_gate_threshold` to enable it.

```python
assembler = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 0.6, "confidence": 0.3},
    confidence_gate_threshold=0.5,
    confidence_gate_mode="refuse",  # or "flag"
)

result = assembler.assemble(query_cues={"topic": "deployment"}, agent_id="agent-1")
print(result.metadata["gate"])
# {"applied": True, "gate_score": 0.42, "threshold": 0.5, "mode": "refuse", "gated": True}
```

**Mechanism.** After the pull path runs, the gate reads the `ConfidenceField`
value of the rank-0 pull-path candidate — the single best-matching record,
which is the one the assembler would otherwise inject — via
`ConfidenceField.get_confidence()`, and compares it to
`confidence_gate_threshold`. This makes the gate **mode-agnostic**:
`get_confidence()` always returns a value in `[0, 1]` regardless of whether
the pull path is running composite (weighted-sum) or hybrid/RRF-fused
retrieval, so the same threshold means the same thing under either ranking
mode. Aggregating across multiple candidates (mean/max/quorum) is out of
scope — the rank-0 record is the deliberate, fixed design choice.

If reading `get_confidence()` raises (e.g. a candidate is missing its
confidence companion hash), the gate degrades gracefully: it logs
`logger.warning` and treats the gate as **not applied**
(`gate_score=None`, `applied=False`, `gated=False`), returning records
unchanged rather than crashing `assemble()` — matching the fault-tolerant
style already used by `emit_trace`/`_compute_quality` elsewhere in the same
method.

**`confidence_gate_mode`:**

| Mode | Behavior when `gate_score < threshold` |
|------|------------------------------------------|
| `"refuse"` (default) | Drops **all** pull-path records before injection. The push path is never touched. Competitive suppression is also skipped for the refused candidates (a refusal isn't punished as a "loss"), but the untouched candidate set still flows into `_compute_quality`, so a feeling-of-knowing (FoK) score computed via `assess_quality=True` correctly reflects that candidates *were found* even though the assembler chose not to inject them. |
| `"flag"` | Never drops records. The only effect is the `metadata["gate"]` annotation — `assess_quality`, suppression, and `result.formatted` are otherwise byte-for-byte identical to leaving the gate off. A caller who wants to act on a flagged low-confidence answer (e.g. prepend a disclaimer) must inspect `metadata["gate"]` itself; ContextAssembler ships no automated consumer of the flag. |

**`metadata["gate"]` shape** — attached to `AssemblyResult.metadata` only
when `confidence_gate_threshold` is not `None` (callers who never pass a
threshold get metadata that is bit-for-bit identical to pre-gate behavior —
no `"gate"` key at all):

| Key | Type | Description |
|-----|------|--------------|
| `applied` | `bool` | Whether the gate actually evaluated a confidence score (`False` if the pull path returned no records, or if `get_confidence()` raised). |
| `gate_score` | `float \| None` | The rank-0 candidate's confidence, or `None` when `applied` is `False`. |
| `threshold` | `float` | The configured `confidence_gate_threshold`, echoed back for convenience. |
| `mode` | `str` | The configured `confidence_gate_mode` (`"refuse"` or `"flag"`). |
| `gated` | `bool` | Whether `gate_score < threshold` (i.e. whether the gate decision fired). Always `False` when `applied` is `False`. |

**Construction-time validation.** Enabling the gate raises `QueryException`
if `confidence_gate_mode` is not one of `{"refuse", "flag"}`, or if the model
does not declare a `ConfidenceField` — mirroring the validation `retrieval_mode="hybrid"`
already performs for `BM25Field`/`EmbeddingField`.

**No-default policy.** `confidence_gate_threshold` ships with **no default** —
it must be explicitly opted into by the caller. The module also defines:

```python
EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD = 0.5  # NOT a shipped default
```

This constant exists **only** for the refusal-metric benchmark and report
scripts (see [Confidence-gated retrieval — refusal precision](../benchmarks.md#confidence-gated-retrieval-refusal-precision-issue-463)).
It is explicitly *not* `Defaults.CONFIDENCE_GATE_THRESHOLD` — no such
`Defaults` entry exists — and it is not used as the ctor default.  Promoting
it to a real default is a policy-level decision requiring maintainer
sign-off (tracked in [issue #463](https://github.com/tomcounsell/popoto/issues/463)).

### Graph Traversal

An opt-in extension (issue #462) of the existing CoOccurrence graph arm
(the "CoOccurrence propagation" step in the [Pipeline](#pipeline) diagram
above). It is off by default — pass `graph_traversal_relationship_fields`
to enable it; omitting it leaves `assemble()` byte-for-byte identical to
before this feature.

```python
assembler = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 0.6, "confidence": 0.3},
    graph_traversal_relationship_fields=["related_memory"],
)

result = assembler.assemble(query_cues={"topic": "deployment"}, agent_id="agent-1")
```

**Mechanism.** Where the pipeline previously called
`CoOccurrenceField.propagate()` directly, it now routes through
[`popoto.recipes.graph_traversal.traverse()`](https://github.com/tomcounsell/popoto/blob/main/src/popoto/recipes/graph_traversal.py),
which:

1. Runs the existing `CoOccurrenceField.propagate()` BFS (unchanged).
2. Additionally walks each named `Relationship` field 1-2 hops, in both
   directions (a node's own relationship value, and other nodes pointing at
   it via the field's `$RelationshipF:...` index Set), with fan-out bounded
   per hop via `SRANDMEMBER`.
3. Merges both signals (max-weight-wins per PK) and caps the result to a
   fixed `max_candidates` *before* any instance is loaded, so traversal
   cost stays bounded independent of graph fan-out.
4. If the model declares a `ConfidenceField` and/or `DecayingSortedField`,
   multiplies each surviving candidate's weight by its own confidence/decay
   state, so a low-confidence or heavily decayed node is less likely to
   survive into the merged candidate set than a fresh, corroborated one.

**`graph_traversal_relationship_fields`** must name **self-referential**
`Relationship` field(s) — a field on `model_class` whose `model` is
`model_class` itself (e.g. a `Memory.related_memory = Relationship(model=Memory)`
edge). A relationship pointing at a different model class is ignored with a
logged warning at construction time (traversal degrades to the
CoOccurrence-only signal, not an error), since `ContextAssembler` treats its
candidate set as homogeneous.

**Evaluation status.** This slice implements and unit-tests the traversal
mechanism (`tests/test_graph_traversal.py`). The LoCoMo multi-hop slice and
association-recall benchmark scenarios called for in issue #462 are tracked
as [issue #484](https://github.com/tomcounsell/popoto/issues/484) rather
than run here — see the note in
[the benchmarking strategy doc](../plans/benchmarking_strategy_2026-07.md#32-structuredgraph-memory-where-zep-hindsight-byterover-lead).

### Tuning Constants

| Constant | Default | Description |
|----------|---------|-------------|
| `COMPETITIVE_SUPPRESSION_SIGNAL` | `0.3` | Signal strength for competitive suppression of non-selected pull candidates. Values < 0.5 act as contradiction signals via `ConfidenceField.update_confidence()`. |
| `DEFAULT_SURFACING_THRESHOLD` | `0.5` | Minimum score for push-path records to be surfaced. |
| `DEFAULT_MAX_ITEMS` | `10` | Default maximum number of records returned. |
| `DEFAULT_PROPAGATION_DEPTH` | `2` | Default BFS depth for CoOccurrence propagation. |
| `EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD` | `0.5` | **Not** a shipped default — used only by the confidence-gate refusal benchmark/report script. Requires maintainer sign-off before it could become a real `Defaults` entry or ctor default (issue #463). |

`popoto/recipes/graph_traversal.py` defines its own experimental constants
(`RELATIONSHIP_HOP_DECAY`, `RELATIONSHIP_HOP_FANOUT_LIMIT`,
`GRAPH_TRAVERSAL_MAX_CANDIDATES`, `ADMISSION_THRESHOLD`) — in-code magic
numbers, not user-facing config, matching the convention above.

### Graceful Degradation

ContextAssembler adapts to whatever fields are present on the model class:

| Missing Field | Behavior |
|---------------|----------|
| ExistenceFilter | Pre-check skipped; proceeds directly to CompositeScoreQuery |
| CoOccurrenceField | Propagation skipped; uses CompositeScoreQuery results directly |
| CyclicDecayField | Push path skipped entirely; returns pull-path results only |
| ConfidenceField | Competitive suppression skipped for non-selected candidates |

This means `ContextAssembler` works with minimal models (just a `DecayingSortedField`) all the way up to full-featured models with all 12 primitives.

## Design principles

These primitives follow Popoto's existing patterns:

1. **ORM primitives, not application logic.** Popoto provides fields, mixins, hooks, and query methods. Domain-specific agent memory models are built on top by application developers.

2. **Redis-native everything.** No external brokers, job queues, or Redis modules. Lua scripts, sorted sets, streams, Bloom filters (via SETBIT/GETBIT) — all within the Redis process. Works identically on Redis and Valkey.

3. **Composable.** Each primitive is independently useful. Use `DecayingSortedField` alone for time-weighted ranking, add `CyclicDecayField` for temporal rhythms and urgency, or combine all twelve for a full cognitive memory system.

4. **Pipeline-safe.** Every operation accepts an optional `pipeline` parameter for atomic execution, consistent with all Popoto field hooks.

5. **Centralized tuning.** All ~24 behavioral constants are consolidated in `Defaults`, importable from the package root. Override globally or per-field — explicit kwargs always win.

```python
from popoto import Defaults

# Global override — all DecayingSortedFields default to 0.7
Defaults.DECAY_RATE = 0.7

# Per-field override still wins
relevance = DecayingSortedField(decay_rate=0.3)  # uses 0.3, not 0.7
```

See [Defaults API reference](../reference/popoto/fields/constants.md) and [Tuning Magic Numbers](../guides/tuning-magic-numbers.md) for the full constant table and benchmark-validated guidance.

## MemoryLifecycle

`MemoryLifecycle` is the policy layer that ties all the primitives together into
a coherent memory lifecycle: episodic → semantic consolidation, continuous decay
(via existing primitives), and auto-forget for low-value idle records.

It composes on top of `DecayingSortedField`, `ConfidenceField`, and
`AccessTrackerMixin` — it does not replace any of them.

### Two tiers

- **episodic** — default for new memories; specific events; subject to promotion and forget
- **semantic** — consolidated facts; decontextualized; protected from auto-forget

### Non-tracking reads

`tick()` reads the corpus through a **non-tracking** query path: every internal
read uses `.no_track()` so that a tick produces **zero** new `AccessTracker`
staged entries. The access-frequency signal therefore reflects only genuine
application reads, not maintenance sweeps.

A single pass hydrates the corpus once; promote-eligibility and forget-eligibility
are evaluated over that same in-memory snapshot. Before any record is forgotten the
record's authoritative tier is re-read from Redis — if the tier is now `"semantic"`
(promoted by a concurrent tick) or the key no longer exists, the forget is skipped.

### Confidence-aware forgetting

Confidence used to gate promotion only, so it could grant a memory permanence but
never hasten its removal. The default forget rule now reads outcome evidence too:

```text
(importance < FORGET_IMPORTANCE_FLOOR
 OR (confidence < FORGET_CONFIDENCE_CEILING AND evidence_count >= FORGET_MIN_EVIDENCE))
AND idle > FORGET_IDLE_SECONDS
```

The `evidence_count` conjunct is a safety floor, not a tuning knob: confidence moves
on every reported outcome, so without a minimum track record one unlucky dismissal
could bury a memory. Models with no `ConfidenceField` take the importance-only path,
which is identical to the previous behavior. The confidence field is resolved through
the same helper the decay path uses, so ranking and forgetting always listen to the
same signal — including the `DECAY_CONFIDENCE_MODULATION_ENABLED` kill switch and the
"two or more `ConfidenceField`s means off" ambiguity rule.

The kill switch is re-read on every call here too, not captured when the
`MemoryLifecycle` is constructed. Setting
`Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED = False` therefore suppresses
confidence-driven forgetting immediately, on lifecycle instances that already exist —
which matters more on this path than on the ranking path, because this is the half
that mutates data.

### Tombstones, not deletes

Forgetting archives the record and removes it from the live corpus rather than
deleting it. Exclusion from every retrieval mode is therefore **structural** — the
record is gone from the model keyspace, so no read path has to remember to filter it
— while its fingerprint, archived payload, and death metadata survive under
`$TOMB:{Model}:*`.

```python
summary = lifecycle.tick()
# {"promoted": 2, "forgotten": 5, "tombstoned": 5, "duration_ms": 8.3}

lifecycle.tombstone_count()          # 5
for tomb in lifecycle.list_tombstones(limit=10):
    print(tomb.redis_key, tomb.confidence_at_death, tomb.dismissal_count)

lifecycle.restore(tomb.redis_key)    # back in the live corpus, indexes rebuilt
```

| Method | Purpose |
|--------|---------|
| `tombstone(record, reason="policy")` | Forget one record; returns a `Tombstone` |
| `restore(redis_key)` | Bring a tombstoned record back and rebuild its indexes |
| `list_tombstones(limit=None)` | Retained tombstones, newest death first |
| `get_tombstone(redis_key)` | One `Tombstone`, or `None` |
| `tombstone_count()` | Number retained |
| `purge_tombstone(redis_key)` / `purge_all_tombstones()` | Drop tombstones permanently |
| `forget_hard(record)` | Irreversible delete, no tombstone |
| `confidence_forget_eligible(record)` | Whether evidence alone justifies forgetting |

A stored tombstone entry that is undecodable, is not a mapping, or is missing any
required field is skipped with a logged warning rather than returned as a partly-empty
`Tombstone`. So `list_tombstones()` and `get_tombstone()` can return fewer entries than
`tombstone_count()` reports if the archive has been corrupted or written to by something
other than `tombstone()` — every `Tombstone` you receive is complete.

Each `Tombstone` carries `redis_key`, `fingerprint`, `tier`, `importance_at_death`,
`confidence_at_death`, `evidence_count`, `dismissal_count`, `tombstoned_at`, and
`reason`. Alongside that death metadata, the stored tombstone entry also archives
the record's **full payload** — that archive is what makes `restore()` possible.
Because a tombstone is therefore about as large as the record it replaced,
retention is bounded by `LIFECYCLE_TOMBSTONE_RETENTION_LIMIT` (1000) with the
oldest aging out, so the tombstone corpus cannot outgrow the live corpus.
`tick()` reports `tombstoned` alongside `forgotten` so a runaway forget policy is
visible in telemetry immediately.

### Usage

```python
from popoto.recipes import MemoryLifecycle

lifecycle = MemoryLifecycle(
    model_class=Memory,
    importance_field="relevance",   # name of a DecayingSortedField
)

# Tag new memories
lifecycle.tag_new(record)  # assigns tier = "episodic"

# Periodic consolidation pass (non-tracking — zero staged AccessTracker entries)
summary = lifecycle.tick()
# {"promoted": 2, "forgotten": 5, "tombstoned": 5, "duration_ms": 8.3}

# Inspect lifecycle state
state = lifecycle.assess(record)
print(state.tier, state.promotion_eligible, state.forget_eligible)
```

See [`docs/recipes.md#memorylifecycle`](../recipes.md#memorylifecycle) for the
full API reference, custom policy examples, multi-agent partitioning, and
benchmark tuning instructions.

## TagField

`TagField` adds **optional, indexed, multi-value scoping** to a model. It is the
scoping primitive for a **centrally hosted Redis/Valkey serving many agents**: a
memory can be scoped by the agent it belongs to, a relevant project, or arbitrary
tags — and **every scoping dimension is optional**. A record with zero tags
transparently lives in the shared pool and is returned by unscoped queries.

Unlike KeyField partitioning (where the partition value becomes part of the
record's Redis key and is *required* at query time), tags are **metadata, not
identity**: a record can carry several tags at once, or none, and scoping is
opt-in per query.

### Basic usage

```python
from popoto import Model, AutoKeyField, Field, TagField

class Memory(Model):
    key = AutoKeyField()
    content = Field(type=str)
    tags = TagField()          # optional; zero tags == shared pool

Memory.create(content="deploy runbook", tags=["agent:valor", "project:popoto"])
Memory.create(content="general note")          # untagged — shared pool

# Membership / any-of (OR) / all-of (AND)
Memory.query.filter(tags__contains="agent:valor")
Memory.query.filter(tags__any=["agent:a", "agent:b"])       # SUNION
Memory.query.filter(tags__all=["agent:valor", "project:popoto"])  # SINTER

# Tag filters compose with any other filter through the same pipeline.
Memory.query.filter(project="popoto", tags__contains="agent:valor")
```

Accepts a `list`, `set`, or `tuple` of scalar tags; values are normalized to a
sorted, de-duplicated list. A bare exact-match `filter(tags=[...])` raises a
`QueryException` (use `__contains`/`__any`/`__all`) — a multi-value field has no
meaningful exact-match semantics.

### Convention over schema

popoto stays agnostic about which scoping dimensions exist. Agents cooperate on
prefixes — `agent:`, `project:` — or use bare tags; nothing is declared in the
schema. Colons in tag values are escaped in the index key, so `agent:valor` never
collides with popoto's key structure.

### Not a security boundary

Tags are a **cooperative** scoping mechanism between trusted agents, **not** access
control. A query with the right tag filter can read any tagged record; there is no
enforcement or isolation. Do not use tags to gate access to sensitive data.

### How it is indexed (Valkey-safe)

Per tag value, a plain Redis Set of instance keys:

```
$TagF:ModelName:field_name:tag_value -> Set of redis_keys
```

Index maintenance rides a single atomic Lua script (`TAG_SWAP_LUA`) that **diffs**
the record's previous tag membership against the new one and issues only the
necessary `SREM`/`SADD` calls — so re-saving with changed tags never leaves
orphaned Set members. The previous membership is read from a server-authoritative
pointer side key (a standalone Redis Set), never a client snapshot. All commands
are core Redis Set operations (`SADD`/`SREM`/`SMEMBERS`/`SUNION`/`SINTER`/`DEL`) —
**no Redis modules**, so it runs identically on Redis and Valkey.

### ContextAssembler integration

`ContextAssembler.assemble()` auto-detects a `TagField` on the model and accepts
optional tag constraints applied across **all** retrieval modes (composite,
hybrid, lexical, push):

```python
assembler = ContextAssembler(Memory, score_weights={"relevance": 1.0})

# Scope retrieval to one agent's memories (AND semantics by default)
assembler.assemble(query_cues={"topic": "deploy"}, tags=["agent:valor"])

# any-of semantics
assembler.assemble(
    query_cues={"topic": "deploy"},
    tags=["agent:valor", "agent:peer"],
    tag_match="any",
)

# Omitting tags is byte-identical to a model with no TagField.
assembler.assemble(query_cues={"topic": "deploy"})
```

`tag_match` defaults to `"all"` (AND / `SINTER`); pass `"any"` for OR (`SUNION`).

### Deploy-level kill switch

Tag scoping in the assembler is default-on via auto-detection. Because a PyPI
adopter cannot always edit model definitions, `Defaults.TAG_SCOPING_ENABLED`
(default `True`) is a deploy-level escape hatch: set it `False` to make the
assembler ignore tag constraints entirely (retrieval becomes identical to a model
without a TagField). Index maintenance and explicit
`Model.query.filter(tags__all=...)` queries are unaffected — the switch governs
only the subconscious assembler path.

## Further reading

- [Metacognitive Layer](metacognitive-layer.md) — quality assessment (`RetrievalQuality`, `assess()`), grouped error analysis (`error_summary`), and `AdaptiveAssembler`
- [Popoto Memory Roadmap](../guides/popoto-memory-roadmap.md) — full implementation spec with test strategies and benchmarks
- [Epistemic Flow in Cognitive Agent Architectures](../guides/epistemic-flow-cognitive-agent-architectures.md) — research background
- [Programmable Memory Systems — Neuroscience Design Spec](../guides/programmable-memory-systems-neuroscience-design-spec.md) — neuroscience foundations
- [Subconscious Memory Recipe](../guides/subconscious-memory-recipe.md) — automatic memory injection and extraction around LLM turns
- [Trajectory Memory Recipe](../guides/trajectory-memory-recipe.md) — fingerprint-keyed procedural memory: cluster completed task trajectories and recall "what worked last time"
- [MemoryLifecycle Recipe](../recipes.md#memorylifecycle) — episodic→semantic consolidation, auto-forget, and tier-scoped queries
- [Behavioral Episode Memory System](https://github.com/tomcounsell/ai/issues/376) — downstream consumer in the AI project
