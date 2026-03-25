# Popoto Memory Primitives: Implementation Roadmap

**For:** Tom Counsell, Lead Engineer — Popoto ORM
**From:** Valor, AI Engineering — Yudame
**Date:** March 2026
**Status:** Complete — All 12 steps shipped

---

## Executive Summary

This roadmap adds programmable memory infrastructure to Popoto in 12 incremental steps. Each step ships a testable, independently useful ORM primitive. Combined, they give AI agents the capabilities LLMs lack: temporal awareness, cyclical recall, outcome learning, confidence tracking, selective encoding, associative retrieval, proactive surfacing, and background knowledge extraction.

The design draws from neuroscience (Complementary Learning Systems theory, ACT-R cognitive architecture, reinforcement learning, chronobiology) but the naming conventions are rooted in computer science and information systems. We are not simulating a brain — we are building data pipeline primitives that happen to solve the same computational problems brains solve.

### Three Temporal Forces

Every memory in this system is acted on by three simultaneous temporal forces, superimposed at query time:

1. **Decay** — monotonic. Records lose relevance over time following a power-law curve. This is the default behavior: things fade unless refreshed.

2. **Cyclical resonance** — periodic. Some records have natural rhythms — daily, weekly, monthly, quarterly, yearly. A memory about Q1 renewals doesn't just decay; it oscillates, peaking every January-March. Like circadian rhythms and seasonal cycles in biology, these temporal patterns are encoded at the ORM level, not in application logic. Species of cicadas emerge on 7-year or 13-year cycles — that timing is DNA, not learned behavior. Similarly, cyclical relevance is a property of the record itself, computed atomically in the scoring Lua script.

3. **Homeostatic pressure** — monotonic, opposing decay. Unresolved obligations build urgency over time, independent of any cycle. The longer an actionable memory goes unaddressed, the louder it gets — like sleep pressure that accumulates continuously until discharged. Pressure resets to zero when the agent acts on the memory, and auto-discharges when confidence drops (the obligation is no longer believed valid).

The effective score at any moment:

```
effective_score = base_score × elapsed^(-decay_rate)
               + Σ(amplitude_i × cos(2π × now / period_i + phase_i))
               + pressure_rate × days_since_last_resolution
```

### Push-Based Recall

Traditional retrieval is pull-based: the agent asks "what's relevant?" and gets ranked results. But humans also experience push-based recall — memories that surface unprompted ("it's March, time for renewals"). This system supports both modes:

- **Pull:** Agent queries via `top_by_decay()` or `CompositeScoreQuery` — the system ranks and returns.
- **Push:** The cyclical and pressure components cause certain records to score highly *without being queried about*. A background observer (or the ContextAssembler at the start of each turn) surfaces records whose effective score exceeds a threshold, independent of the agent's current topic. These are delivered as **proposals** — side-effect-free until the agent responds.

### Observation Protocol

An LLM cannot be expected to manage its own memory mechanics — calling `touch()`, resolving predictions, updating confidence. That's like asking a person to manually regulate their heartbeat. The ORM provides an **observation protocol**: hooks that fire automatically and infer memory outcomes from the agent's downstream behavior.

- A surfaced memory whose content appears in the agent's response → implicit **acted-on** → strengthen
- A surfaced memory the agent explicitly contradicts → implicit **dismissal** → weaken confidence
- A surfaced memory the agent ignores → implicit **deferral** → no change, pressure keeps building

The ORM provides the hooks and resolution mechanics. The application layer provides the inference signal (semantic similarity, keyword match, or LLM judgment). Popoto doesn't dictate *how* you detect influence; it dictates *what happens* when you report it.

### Entrainment — Self-Correcting Cycles

Cyclical parameters (amplitude and phase) are hypotheses, not constants. They self-correct through use:

- Memory surfaces on schedule and gets acted on → phase nudges toward actual activation time, amplitude strengthens
- Memory surfaces and gets dismissed → amplitude weakens; after enough dismissals, the cycle effectively dies
- Memory gets acted on at an unexpected time → if this recurs, a new cycle is discovered and added with low initial amplitude

This is literal entrainment — the same mechanism by which external cues (light, temperature) synchronize biological clocks. The math is a weighted moving average: `new_phase = (1 - lr) × old_phase + lr × observed_phase`.

**Guiding principles:**

1. **Ship small, test often.** Each step produces a working, independently testable primitive.
2. **ORM primitives, not application logic.** Popoto provides generic field types, mixins, hooks, and query methods. Domain-specific agent memory models are built *on top of* these by application developers. Temporal cycles are ORM-level infrastructure (like circadian rhythms are biological infrastructure), not application-level scheduling.
3. **Redis-native everything.** No Celery, no external brokers. Redis Streams, Lua scripts, sorted sets, Bloom filters — all within the Redis process.
4. **Measurable improvement.** Every step includes a test strategy that demonstrates concrete, quantifiable benefits for agents using the primitive.
5. **Combinatorial testing.** After each new primitive lands, test its interactions with all previously shipped primitives. By Step 12, we have coverage for every meaningful pair and key multi-component integrations.
6. **Passive observation over explicit management.** Memory mechanics (strengthening, weakening, cycle adjustment) should be inferred from behavior, not require deliberate calls from the agent or application code.

---

## Naming Conventions

We use CS/information-systems terminology throughout. The mapping from the research literature:

| Research Term | Popoto Term | Rationale |
|---|---|---|
| Episodic memory | **Event store** | Standard event-sourcing terminology |
| Procedural memory | **Policy cache** | RL terminology — learned state→action mapping |
| Semantic memory | **Knowledge index** | Information retrieval terminology |
| Consolidation | **Compaction pipeline** | Database compaction / log-structured merge |
| Salience gating | **Write filter** | Pipeline filter pattern |
| Activation score | **Priority score** | Priority queue / scheduling terminology |
| Spreading activation | **Graph propagation** | Graph algorithm terminology |
| Hebbian association | **Co-occurrence weight** | Statistical co-occurrence |
| Forward model | **Prediction ledger** | Accounting/ledger terminology |
| Feeling of knowing | **Existence check** | Bloom filter canonical use case |
| Thalamic gate | **Context assembler** | Pipeline assembly pattern |
| Emotional valence | **Outcome signal** | Signal processing terminology |
| Memory trace | **Record** | Database record |
| Engram | **Entry** | Log entry / cache entry |
| Circadian rhythm | **Cyclical resonance** | Harmonic oscillator / signal processing |
| Sleep pressure | **Homeostatic pressure** | Control systems terminology |
| Zeitgeber (entrainment) | **Phase correction** | Phase-locked loop terminology |
| Prospective memory | **Proactive surfacing** | Push notification pattern |
| Unconscious recall | **Observation protocol** | Observer pattern (GoF) |
| Phase response curve | **Temporal context weight** | Context-dependent scoring |

---

## Step 1: DecayingSortedField + CyclicDecayField — Time-Weighted Scoring with Temporal Rhythms ✅ Shipped

**What it is:** Two field types that wrap Redis sorted sets with time-aware scoring. `DecayingSortedField` provides power-law decay — the foundational primitive. `CyclicDecayField` extends it with harmonic cycle components and homeostatic pressure, enabling records that resurface on temporal rhythms and build urgency when unresolved.

Plain decay is a special case of the full temporal model (all cycle amplitudes zero, pressure rate zero). Both fields share the same query interface; CyclicDecayField adds cycle and pressure parameters.

**Implementation:** Shipped in [PR #199](https://github.com/tomcounsell/popoto/pull/199) (DecayingSortedField), [PR #201](https://github.com/tomcounsell/popoto/pull/201) (CyclicDecayField), and [PR #207](https://github.com/tomcounsell/popoto/pull/207) (InteractionWeight constants).

**ORM additions:**

```python
class DecayingSortedField(SortedField):
    """
    Sorted set where scores decay as a power law of time since last update.

    Key pattern: $DSF:{ClassName}:{field_name}
    Members scored by: base_score × (elapsed_time)^(-decay_rate)

    decay_rate: float = 0.5 (ACT-R default, tunable per field)
    """
    decay_rate: float = 0.5


class CyclicDecayField(DecayingSortedField):
    """
    Decay modulated by cyclical relevance and homeostatic pressure.

    Three forces superimposed at query time:
      1. Decay:     base_score × elapsed^(-decay_rate)
      2. Resonance: Σ(amplitude × cos(2π × now / period + phase))
      3. Pressure:  pressure_rate × days_since_last_resolution

    Cycle data stored per-member in companion hash:
      $CDF:{ClassName}:{field_name}:cycles → {member: msgpack([period, amp, phase], ...)}
      $CDF:{ClassName}:{field_name}:pressure → {member: msgpack{rate, last_resolved}}

    Predefined period constants (seconds):
      DAILY     = 86_400
      WEEKLY    = 604_800
      MONTHLY   = 2_592_000
      QUARTERLY = 7_776_000
      YEARLY    = 31_536_000
    """
    # Default: no cycles, no pressure (behaves like DecayingSortedField)
    cycles: list = []        # [(period, amplitude, phase), ...]
    pressure_rate: float = 0.0
```

**Lua script:** `cyclic_decay_scores.lua` — computes all three temporal components atomically for all members of a sorted set. Falls back to pure decay when cycle/pressure hashes are empty (two `HGET`s returning nil — negligible overhead).

```lua
-- KEYS[1] = sorted set (member -> last_updated timestamp)
-- KEYS[2] = base scores hash (member -> base_score)
-- KEYS[3] = cycles hash (member -> msgpack([[period, amp, phase], ...]))
-- KEYS[4] = pressure hash (member -> msgpack({rate, last_resolved}))
-- ARGV[1] = now (seconds), ARGV[2] = decay_rate, ARGV[3] = max_results

for each member:
    -- Component 1: power-law decay
    local elapsed_days = max((now - last_updated) / 86400, 0.01)
    local decay = base_score * pow(elapsed_days, -decay_rate)

    -- Component 2: cyclical resonance (skip if no cycles)
    local cyclic = 0
    local cycles_packed = redis.call('HGET', KEYS[3], member)
    if cycles_packed then
        local cycles = cmsgpack.unpack(cycles_packed)
        for _, c in ipairs(cycles) do
            cyclic = cyclic + c[2] * math.cos(2 * math.pi * now / c[1] + c[3])
        end
    end

    -- Component 3: homeostatic pressure (skip if no pressure)
    local pressure = 0
    local pressure_packed = redis.call('HGET', KEYS[4], member)
    if pressure_packed then
        local p = cmsgpack.unpack(pressure_packed)
        local days_unresolved = (now - p.last_resolved) / 86400
        pressure = p.rate * days_unresolved
    end

    local effective_score = decay + cyclic + pressure
```

**Test strategy:**
- Unit: Insert N records with known timestamps, advance clock, verify scores match `score × t^(-0.5)` within tolerance (pure decay case).
- Cyclical: Insert record with yearly cycle (amplitude=5.0, phase pointing at March). Verify score peaks in March, troughs in September. Verify a record with no cycles behaves identically to plain DecayingSortedField.
- Pressure: Insert actionable record with pressure_rate=0.1. Verify score increases linearly over unresolved days. Verify pressure resets to zero after resolution.
- Combined: Record with decay + yearly cycle + pressure. Verify the three components superimpose correctly against hand-computed values.
- Property: Records accessed recently always outscore older records with same initial score and no cycles.
- Benchmark: Measure Lua script execution time for 1K, 10K, 100K member sorted sets, comparing pure decay vs. full cyclic+pressure computation.

**Measurable agent improvement:** Before: agents retrieve memories by insertion order or raw score. After: agents naturally surface recent records *and* temporally relevant records (Q1 renewals in January, weekly standup notes on Monday). Test with a simulated year of agent interactions — measure whether cyclically relevant records surface at appropriate times without explicit queries.

**InteractionWeight constants:** Ship alongside as `popoto.fields.constants.InteractionWeight` — two-axis weight system for multi-agent teamwork scenarios. Source axis: `HUMAN=6.0`, `AGENT=1.0`, `SYSTEM=0.2`. Role axis: `EXECUTIVE=44.0`, `MANAGER=16.0`, `PEER=6.0`, `SUBORDINATE=1.0`. Combined via `InteractionWeight.combine(source, role)` (addition). With `decay_rate=0.5`, lifetime ≈ score² days — a human executive directive (50.0) persists ~7 years while an agent subordinate observation (2.0) decays in ~4 days.

**TemporalPeriod constants:** Ship alongside as `popoto.fields.constants.TemporalPeriod`:

```python
class TemporalPeriod:
    """Standard cycle periods in seconds for CyclicDecayField."""
    DAILY     = 86_400
    WEEKLY    = 604_800
    MONTHLY   = 2_592_000    # 30 days
    QUARTERLY = 7_776_000    # 90 days
    YEARLY    = 31_536_000   # 365 days
```

**Issues:**
- [#193](https://github.com/tomcounsell/popoto/issues/193): DecayingSortedField — time-weighted scoring via Lua
- [#196](https://github.com/tomcounsell/popoto/issues/196): CyclicDecayField — temporal rhythms + homeostatic pressure

---

## Step 2: ObservationProtocol + AccessTracker — Passive Behavioral Inference ✅ Shipped

**What it is:** An observation layer that passively tracks how the agent interacts with memories, plus an access tracking mixin that records read patterns. The key design principle: **an LLM cannot manage its own memory mechanics** — calling `touch()`, resolving predictions, updating confidence is like asking a person to regulate their heartbeat. The ORM must observe behavior and infer outcomes automatically.

The observation protocol defines three hooks that fire at different points in the memory lifecycle. `touch()` is never called automatically — it is an *outcome* of observation, not a side effect of reading.

**Implementation:** Shipped in [PR #206](https://github.com/tomcounsell/popoto/pull/206) (ObservationProtocol + RecallProposal) and [PR #203](https://github.com/tomcounsell/popoto/pull/203) (AccessTrackerMixin).

**ORM additions:**

```python
class ObservationProtocol:
    """
    Hooks that fire automatically at different lifecycle points.
    Application layer reports behavioral signals; ORM applies effects.

    Hooks:
      on_read(instance, pipeline)
        Fires when query.get()/filter() hydrates an instance.
        Logs to staging area. Does NOT call touch() or strengthen.

      on_surfaced(instance, reason, pipeline)
        Fires when proactive system pushes a memory into agent context.
        Creates a pending proposal. Side-effect-free on the memory itself.

      on_context_used(surfaced_instances, outcome_map, pipeline)
        Fires when application reports how the agent responded.
        outcome_map: {instance_pk: "acted"|"dismissed"|"deferred"|"contradicted"}
        Applies effects based on outcome:
          acted      → touch(), corroborate confidence, strengthen cycles,
                       discharge pressure, strengthen co-occurrence links
          dismissed  → weaken confidence, weaken cycle amplitude
          deferred   → no effects, pressure keeps building
          contradicted → contradict confidence, weaken cycles aggressively

    The ORM provides hooks and resolution mechanics.
    The application layer provides the inference signal:
      - Did the memory's content appear in the response? (acted)
      - Did the agent explicitly contradict it? (contradicted)
      - Was it ignored entirely? (deferred)
    Popoto doesn't dictate HOW you detect influence; it dictates
    WHAT HAPPENS when you report it.
    """


class AccessTrackerMixin:
    """
    Tracks read access patterns on any Model.

    Adds fields: access_count (int), last_accessed (float),
                 access_log (capped list of timestamps, max_length=100)

    Hook: on_read(instance, pipeline) — appends to staging log.
          Actual strengthening only occurs via on_context_used() when
          the observation protocol confirms the read was meaningful.

    Key pattern: $AT:{ClassName}:access_log:{pk} → List (capped at max_length)
                 $AT:{ClassName}:staged:{pk} → List (uncommitted reads)
    """
    max_access_log: int = 100
```

**Proposal queue for proactive recall:**

When the cyclical/pressure components of CyclicDecayField cause a record to score above a surfacing threshold, the observation protocol creates a **proposal** — a lightweight pointer to the memory with a pending status:

```python
# ORM-level, not application-level
class RecallProposal:
    """
    Internal tracking for proactively surfaced memories.

    Key pattern: $RP:{ClassName}:pending:{agent_partition} → ZSET by surfaced_at

    Statuses: pending → acted | dismissed | deferred | contradicted | expired

    Proposals that expire (not resolved within TTL) are treated as deferred.
    """
    ttl: int = 3600  # expire unresolved proposals after 1 hour
```

**Synergy test with Step 1:** DecayingSortedField + AccessTracker enables the full priority score computation: `B = ln(Σ t_j^(-d))` where t_j comes from the *confirmed* access log (not staged reads). Test that records with spaced access patterns (3 reads over 3 days) produce higher priority scores than records with massed access (3 reads in 1 minute), given equal total reads and age.

**Synergy test with CyclicDecayField:** Proactive surfacing creates proposals; observation protocol resolves them. Test: record with yearly cycle surfaces in March → application reports "acted" → verify touch() was called, cycle amplitude strengthened. Test: same record surfaces → application reports "dismissed" → verify NO touch(), cycle amplitude weakened, pressure unchanged.

**Measurable agent improvement:** Compare agent retrieval quality on a knowledge-base QA task. Baseline: retrieve by recency only (every read strengthens). With observation protocol: only meaningful reads strengthen. Measure: (a) precision@5 improvement from reduced noise in access patterns, (b) stale memories correctly deprioritized after dismissal.

**Issues:**
- [#197](https://github.com/tomcounsell/popoto/issues/197): AccessTrackerMixin — read pattern tracking with staged vs confirmed reads
- [#198](https://github.com/tomcounsell/popoto/issues/198): ObservationProtocol + RecallProposal — outcome-driven memory effects

---

## Step 3: WriteFilter Mixin — Selective Encoding ✅ Shipped

**What it is:** A model mixin that gates record persistence based on a configurable scoring function evaluated in the `on_save()` hook. Records below a threshold are silently discarded (raise `SkipSaveException`). Records above a high threshold are tagged for priority processing.

**Implementation:** Shipped in [PR #214](https://github.com/tomcounsell/popoto/pull/214).

**ORM addition:**

```python
class WriteFilterMixin:
    """
    Gates persistence based on a scoring function evaluated at write time.
    
    Subclass must implement: compute_filter_score(instance) -> float [0, 1]
    
    Config:
      min_threshold: float = 0.2  — below this, SkipSaveException
      priority_threshold: float = 0.7  — above this, ZADD to priority set
    
    Key pattern: $WF:{ClassName}:priority → sorted set of priority-tagged PKs
    
    on_save hook: compute score → gate → optionally tag for priority
    """
    min_threshold: float = 0.2
    priority_threshold: float = 0.7
```

The scoring function itself is **application layer** — Popoto provides the gating mechanism, not the scoring logic. An agent developer implements `compute_filter_score()` using whatever signals are relevant (surprise, importance, etc.).

**Synergy test with Steps 1-2:** WriteFilter + DecayingSortedField — verify that filtered-out records never appear in sorted set indexes. WriteFilter + AccessTracker — verify that priority-tagged records get their access patterns tracked identically to normal records.

**Measurable agent improvement:** Run an agent through 1000 interactions. Without WriteFilter: store all 1000. With WriteFilter (threshold 0.2): store ~300-500. Measure: (a) storage reduction, (b) retrieval precision@5 improvement (less noise), (c) retrieval latency improvement (smaller index).

**Issues:** ~2 (mixin with SkipSaveException, threshold config, tests)

---

## Step 4: ConfidenceField — Bayesian Certainty Tracking + Entrainment ✅ Shipped

**What it is:** A field type that maintains a Bayesian confidence score updated atomically via Lua script. Each update provides a binary signal (corroborate/contradict) with a weight. The prior becomes harder to shift as evidence accumulates (precision grows with √n).

ConfidenceField also serves as the **entrainment mechanism** for CyclicDecayField (Step 1). Cyclical parameters (amplitude, phase) are hypotheses about temporal relevance. The observation protocol (Step 2) generates corroborate/contradict signals for these hypotheses, and ConfidenceField applies them. When a cycle's confidence drops below a threshold, the cycle auto-disables — this is how stale recurring memories ("send that client an update") die when the underlying context has changed ("that client contract ended").

**Implementation:** Shipped in [PR #215](https://github.com/tomcounsell/popoto/pull/215).

**ORM addition:**

```python
class ConfidenceField(Field):
    """
    Bayesian confidence score with precision-weighted updates.

    Stored as: {confidence: float, evidence_count: int,
                corroborations: int, contradictions: int}

    Update method: instance.confidence_field.update(
        corroborate=True/False, weight=0.8, pipeline=None
    )

    Lua script: bayesian_update.lua — atomic read-modify-write
    Key pattern: confidence stored as hash fields on the parent model
    """
    initial_confidence: float = 0.5
```

**Entrainment integration with CyclicDecayField:**

When the observation protocol resolves a proactive recall proposal, ConfidenceField updates apply not just to the memory's content confidence but also to its cycle parameters:

```python
# Entrainment effects (applied automatically by observation protocol)
#
# on_context_used(outcome="acted"):
#   - Content confidence: corroborate
#   - Cycle phase: nudge toward actual activation time
#     new_phase = (1 - lr) * old_phase + lr * observed_phase
#   - Cycle amplitude: strengthen (ZINCRBY +delta)
#
# on_context_used(outcome="dismissed"):
#   - Content confidence: no change (dismissal may be contextual, not factual)
#   - Cycle amplitude: weaken (ZINCRBY -delta)
#   - If amplitude drops below threshold → cycle auto-disables
#
# on_context_used(outcome="contradicted"):
#   - Content confidence: contradict
#   - Cycle amplitude: weaken aggressively
#   - Homeostatic pressure: discharge (obligation no longer valid)
```

**Confidence on cycles vs. content:** A memory can have high content confidence ("this client exists") but low cycle confidence ("I should reach out every March" — maybe not anymore). These are tracked separately. Content confidence modulates retrieval weight. Cycle confidence modulates whether the cycle fires at all.

**Synergy tests:**
- ConfidenceField + DecayingSortedField: Records with low confidence should effectively have lower retrieval priority. Test composite scoring: `priority = decay_score × confidence`.
- ConfidenceField + WriteFilter: Contradicted records (confidence dropping below a threshold) should be eligible for directed forgetting (score reduction, not deletion).
- ConfidenceField + CyclicDecayField (entrainment): Record with yearly cycle surfaces and gets dismissed 3 times → verify cycle amplitude decreases. Record surfaces and gets acted on at a slightly different time → verify phase shifts toward actual activation. Record dismissed enough times that amplitude < threshold → verify cycle component returns 0 in subsequent scoring.
- ConfidenceField + homeostatic pressure: Record's content confidence drops below 0.1 → verify homeostatic pressure auto-discharges (obligation no longer believed valid).

**Measurable agent improvement:** Give an agent a knowledge base with 20% deliberately contradictory records. Without ConfidenceField: agent retrieves contradictory records at equal weight, producing inconsistent answers. With ConfidenceField: agent's consistency score improves as contradicted records lose retrieval weight. Measure answer consistency across 50 queries touching contradicted facts. Additionally: set up 5 recurring memories where the underlying context has changed (stale obligations). Measure how many cycles it takes for the system to auto-disable them via entrainment.

**Issues:** ~3 (field implementation with Lua script, entrainment integration with CyclicDecayField, synergy tests with Steps 1-3)

---

## Step 5: CoOccurrenceField — Weighted Association Edges ✅ Shipped

**What it is:** A field mixin that maintains weighted, bidirectional edges between model instances using sorted sets. Weights strengthen when records are accessed together (co-retrieval) and decay when not reinforced — the "co-accessed items strengthen their link" principle.

**Implementation:** Shipped in [PR #218](https://github.com/tomcounsell/popoto/pull/218).

**ORM addition:**

```python
class CoOccurrenceField:
    """
    Manages weighted edges between instances in sorted sets.
    
    Key pattern: $CoOc:{ClassName}:{field_name}:{pk} → ZSET of associated PKs
    
    Methods:
      link(source_pk, target_pk, initial_weight=0.1, pipeline=None)
      strengthen(source_pk, target_pk, delta=0.05, pipeline=None)  # ZINCRBY
      weaken_all(pk, factor=0.95, pipeline=None)  # Multiplicative decay
      get_linked(pk, min_weight=0.01, limit=20) -> list[(pk, weight)]
      propagate(seed_pks, depth=2, decay_per_hop=0.5, threshold=0.01) 
        -> dict[pk, propagated_weight]  # BFS graph propagation
    """
    symmetric: bool = True
    max_edges: int = 500
    decay_factor: float = 0.95  # Per time-step multiplicative decay
```

**Graph propagation** implements a simple BFS with exponential weight decay per hop. At depth 1, neighbors get `weight × decay_per_hop`. At depth 2, neighbors-of-neighbors get `weight × decay_per_hop²`. This replaces spreading activation with standard graph traversal terminology.

**Synergy tests:**
- CoOccurrenceField + DecayingSortedField: After propagation, inject propagated weights as score boosts in the sorted set. Test that retrieving record A boosts retrieval of A's associates.
- CoOccurrenceField + AccessTracker: Co-accessed records automatically strengthen. Test: access A then B 5 times → verify weight(A→B) increased.
- CoOccurrenceField + ConfidenceField: Propagated confidence — if A links to B with weight 0.8, and A's confidence drops to 0.1, B's *effective* retrieval weight should be modulated. Test the composite.

**Measurable agent improvement:** Give an agent a task requiring multi-hop reasoning (e.g., "The CFO prefers stability" + "Stability implies fixed-cost models"). Without CoOccurrenceField: agent must retrieve both records independently. With: retrieving "CFO" propagates to "stability" which propagates to "fixed-cost." Measure retrieval recall on multi-hop queries.

**Issues:** ~3 (field with ZINCRBY/BFS, propagation algorithm, synergy tests with Steps 1-4)

---

## Step 6: EventStreamMixin — Append-Only Mutation Log ✅ Shipped

**What it is:** A model mixin that automatically XADDs to a Redis Stream on every save, update, or delete. This is the foundation for the compaction pipeline (Step 10) — every mutation is captured as a stream entry with model class, PK, operation type, and key metadata fields.

**Implementation:** Shipped in [PR #220](https://github.com/tomcounsell/popoto/pull/220).

**ORM addition:**

```python
class EventStreamMixin:
    """
    XADDs to a Redis Stream on every save/update/delete.
    
    Key pattern: stream:{stream_name}:{partition_key}
    
    on_save hook:  XADD {model, pk, op:"create", ...metadata}
    on_update hook: XADD {model, pk, op:"update", changed_fields, ...metadata}
    on_delete hook: XADD {model, pk, op:"delete", ...metadata}
    
    Config:
      stream_name: str  — logical stream name
      partition_key_field: str  — field name to partition by (e.g., "agent_id")
      max_stream_length: int = 10000  — MAXLEN approximate
      metadata_fields: list[str]  — additional fields to include in stream entry
    """
    stream_name: str = "mutations"
    max_stream_length: int = 10000
```

The mixin doesn't process the stream — it only writes. Processing is application layer (Step 10).

**Synergy tests:**
- EventStreamMixin + WriteFilter: Filtered-out records should NOT produce stream entries. Test: save a below-threshold record → verify no XADD.
- EventStreamMixin + ConfidenceField: Confidence updates should produce stream entries with the delta. Test: update confidence → verify stream entry contains old/new confidence.
- EventStreamMixin + CoOccurrenceField: Co-occurrence weight changes should be loggable. Test: strengthen a link → verify stream entry.

**Measurable agent improvement:** Not directly agent-facing — this is infrastructure. Measure: stream write overhead per save operation (target: <0.5ms added latency). Verify zero data loss under concurrent writes via consumer group XREADGROUP acknowledgment.

**Issues:** ~2 (mixin with XADD in hooks, consumer group helper utilities, tests)

---

## Step 7: CompositeScoreQuery — Multi-Factor Retrieval ✅ Shipped

**What it is:** A query method that combines multiple sorted set indexes with configurable weights using ZUNIONSTORE, then returns top-K results by composite score. This is the retrieval engine — the single most important query primitive for agent memory.

**Implementation:** Shipped in [PR #222](https://github.com/tomcounsell/popoto/pull/222).

**ORM addition:**

```python
class CompositeScoreQuery:
    """
    Combines N sorted set indexes via ZUNIONSTORE with weights.
    
    Usage:
      results = MyModel.query.composite_score(
          indexes={
              "priority_score": 0.4,    # DecayingSortedField index
              "confidence": 0.3,        # ConfidenceField index  
              "salience": 0.2,          # WriteFilter priority index
              "last_accessed": 0.1,     # AccessTracker index
          },
          filter_fn=lambda pk: True,    # Optional post-filter
          limit=10,
          min_score=0.0
      )
    
    Implementation: 
      1. ZUNIONSTORE to temp key with WEIGHTS
      2. ZREVRANGEBYSCORE with LIMIT
      3. DEL temp key (or EXPIRE 5s)
      4. Hydrate models from PKs
    """
```

**Synergy tests — this is the big one.** CompositeScoreQuery is where Steps 1-5 converge:
- Decay (Step 1) + Access (Step 2) + Confidence (Step 4): Composite of time-decayed priority, access-aware scoring, and confidence weighting. Test: a high-confidence recently-accessed record outranks a low-confidence old record.
- All above + CoOccurrence propagation (Step 5): Inject propagated weights as a boost factor. Test: record B has mediocre individual scores but strong co-occurrence with the query context → it should surface.
- All above + WriteFilter (Step 3): Priority-tagged records should have a score bonus. Test: two records with identical base scores — the priority-tagged one ranks higher.

**Measurable agent improvement:** This is directly measurable. Run the same 100-query retrieval benchmark with:
1. Single-index retrieval (recency only) — baseline
2. Two-index composite (recency + confidence)
3. Three-index composite (recency + confidence + access frequency)
4. Four-index composite (all four + co-occurrence propagation)

Measure precision@5, recall@10, and mean reciprocal rank at each level. The hypothesis: each additional signal improves retrieval quality, with diminishing but positive returns.

**Issues:** ~3 (ZUNIONSTORE wrapper, temp key management, benchmark suite, synergy matrix tests)

---

## Step 8: ExistenceFilter — Fast Pre-Retrieval Check ✅ Shipped

**What it is:** A field type implementing a Bloom filter for O(1) probabilistic membership queries. Answers "have I ever stored a record matching this fingerprint?" without touching any sorted set or hash. False positives possible; false negatives impossible.

**Implementation:** Shipped in [PR #225](https://github.com/tomcounsell/popoto/pull/225).

**Architectural decision — Lua-based, no Redis modules:** The original plan called for RedisBloom module commands (`BF.ADD`, `BF.EXISTS`, `CMS.INCRBY`, `CMS.QUERY`). This was changed to pure Lua scripts using core Redis commands (`SETBIT`/`GETBIT` for Bloom filter, `HINCRBY`/`HGET` for Count-Min Sketch). The reason: RedisBloom is not available on Valkey, and Popoto supports both Redis and Valkey. The Lua implementation uses the Kirschner-Mitzenmacher double hashing optimization (DJB2 + FNV-1) to simulate k independent hash functions with identical theoretical guarantees. Performance difference is negligible for agent memory workloads.

**ORM addition:**

```python
class ExistenceFilter(Field):
    """
    Bloom filter for O(1) probabilistic membership checks.

    Implemented with Redis SETBIT/GETBIT and Lua scripts.
    No Redis modules required -- works on both Redis and Valkey.

    Key pattern: $EF:{ClassName}:{field_name} → Redis string (bit array)

    on_save hook: Lua script sets k bits via SETBIT

    Methods:
      might_exist(model_class, fingerprint: str) -> bool   # O(1) via Lua
      definitely_missing(model_class, fingerprint: str) -> bool  # inverse
      fill_ratio(model_class) -> float  # diagnostic: proportion of set bits

    Config:
      error_rate: float = 0.01  # 1% false positive rate
      capacity: int = 100000    # Expected number of entries
      fingerprint_fn: Callable  # How to compute fingerprint from instance
    """
    error_rate: float = 0.01
    capacity: int = 100000
```

Also shipped: `FrequencySketch` implementing Count-Min Sketch via Lua scripts and Redis hashes (`HINCRBY`/`HGET`). Provides `get_frequency(model_class, fingerprint)` for approximate frequency queries. No Redis modules required.

**Synergy tests (verified):**
- ExistenceFilter + CompositeScoreQuery (Step 7): Use ExistenceFilter as a pre-filter — skip the full composite query if `definitely_missing()` returns True.
- ExistenceFilter + WriteFilter (Step 3): Filtered-out records are NOT added to the Bloom filter (the existing save flow raises `SkipSaveException` before `on_save()` hooks run).

**Measurable agent improvement:** In a retrieval-augmented agent, measure the percentage of retrieval calls that can be short-circuited by the Bloom filter (expected: 30-60% of queries touch topics with no stored records). Measure end-to-end latency reduction.

---

## Step 9: PredictionLedger Mixin — Outcome Tracking + Auto-Resolution ✅ Shipped

**What it is:** A model mixin for recording prediction→outcome pairs. Before an action, the agent writes a prediction (expected outcome, expected duration, expected quality). After the action, it writes the actual outcome. The mixin automatically computes the delta and stores it as a learning signal.

**Implementation:** Shipped in [PR #231](https://github.com/tomcounsell/popoto/pull/231).

Critically, the PredictionLedger supports **auto-resolution** — outcomes inferred from downstream behavior via the observation protocol (Step 2), not just explicit `resolve_prediction()` calls. Every proactive recall (Step 1 cyclical/pressure surfacing) is implicitly a prediction: "this memory is relevant right now." The observation protocol's resolution of that proposal feeds directly into the PredictionLedger as a prediction→outcome pair.

**ORM addition:**

```python
class PredictionLedgerMixin:
    """
    Tracks prediction→outcome pairs with automatic delta computation.

    Adds fields: predicted_outcome (JSON), actual_outcome (JSON, nullable),
                 prediction_error (float, nullable), resolved (bool),
                 resolution_mode (str: "explicit"|"observed"|"expired")

    Methods:
      record_prediction(instance, predicted: dict, pipeline=None)
      resolve_prediction(instance, actual: dict, pipeline=None)
        → computes delta, sets prediction_error, sets resolved=True
        → ZADD to prediction_error sorted set index

      auto_resolve(instance, outcome: str, pipeline=None)
        → called by observation protocol when behavioral inference
          determines the outcome. Same effects as resolve_prediction()
          but marks resolution_mode="observed".

    Key pattern: $PL:{ClassName}:errors:{partition_key} → ZSET of PKs by |error|
    """
```

**Auto-resolution via observation protocol:**

Every proactive surfacing creates an implicit prediction. The observation protocol resolves it:

| Surfacing outcome | Implicit prediction | Prediction error | Effect |
|---|---|---|---|
| acted | "This is relevant now" | Low (correct) | Corroborate confidence, strengthen cycle |
| dismissed | "This is relevant now" | Medium (wrong timing or stale) | Weaken cycle amplitude |
| contradicted | "This is relevant now" | High (factually wrong) | Contradict confidence, weaken cycle aggressively |
| expired (no response) | "This is relevant now" | Low-medium (possibly deferred) | No confidence change, pressure builds |

This means the PredictionLedger accumulates calibration data on the memory system's own proactive recall decisions — "how good is this system at predicting what's relevant?" Over time, cycle amplitudes that produce many dismissed surfacings will weaken (entrainment), and the system's precision improves.

**Synergy tests:**
- PredictionLedger + WriteFilter (Step 3): High prediction errors should produce high filter scores. Test: resolve a prediction with error > 0.7 → verify it gets priority-tagged.
- PredictionLedger + EventStreamMixin (Step 6): Prediction resolutions should appear in the mutation stream. Test: resolve → verify stream entry with old prediction, actual outcome, and delta.
- PredictionLedger + ConfidenceField (Step 4): When predictions are consistently wrong, associated knowledge records' confidence should decrease. Test: 5 consecutive high-error predictions linked to Pattern X → verify X's confidence drops.
- PredictionLedger + DecayingSortedField (Step 1): High-error predictions should decay slower (they're more informative). Test: compare decay rates of high-error vs. low-error predictions.
- PredictionLedger + CyclicDecayField (Step 1) + ObservationProtocol (Step 2): Full loop — record with yearly cycle surfaces proactively → observation protocol infers "dismissed" → auto_resolve() fires → prediction error recorded → cycle amplitude weakened via entrainment. Test the entire chain end-to-end.

**Measurable agent improvement:** Run an agent through a task suite where it predicts difficulty/approach before each task. Measure calibration: does `mean(predicted_quality)` converge toward `mean(actual_quality)` over 50 tasks? Without PredictionLedger: no convergence (agent has no outcome memory). With: calibration error should decrease by >30% over the task suite. Additionally: measure proactive recall precision over time — what percentage of surfaced memories get acted on? This should improve as entrainment adjusts cycle parameters based on PredictionLedger auto-resolution data.

**Issues:** ~4 (mixin with predict/resolve methods, auto-resolution mode, delta computation, synergy tests with all prior steps including observation protocol loop)

---

## Step 10: StreamConsumer — Background Compaction Pipeline ✅ Shipped

**What it is:** A consumer group framework for processing EventStream entries in batches. This is the background pipeline that transforms raw event records into durable, generalized knowledge. Popoto provides the consumer framework; the application layer provides the compaction logic.

**Implementation:** Shipped in [PR #238](https://github.com/tomcounsell/popoto/pull/238).

**ORM addition:**

```python
class StreamConsumer:
    """
    Redis Streams consumer group framework for background processing.
    
    Manages: consumer group creation, XREADGROUP with blocking,
             batch processing, acknowledgment, and dead-letter handling.
    
    Usage:
      consumer = StreamConsumer(
          stream_key="stream:mutations:agent_1",
          group_name="compaction",
          consumer_name="worker_1",
          batch_size=50,
          block_ms=5000,
          handler=my_compaction_handler  # Application layer
      )
      consumer.run()  # Blocking loop, or consumer.process_batch() for one-shot
    
    Built-in features:
      - Consumer group auto-creation (XGROUP CREATE ... MKSTREAM)
      - Exactly-once processing via XACK after handler success
      - Dead-letter queue for failed entries (XCLAIM after timeout)
      - Backpressure: configurable max pending entries
    """
    batch_size: int = 50
    block_ms: int = 5000
    max_pending: int = 1000
```

This is a **generic Redis Streams consumer** — the compaction/pattern-extraction logic is entirely application layer. Popoto provides the reliable consumption framework.

**Synergy tests:**
- StreamConsumer + EventStreamMixin (Step 6): End-to-end test — save records → verify they appear in stream → consumer processes them → verify XACK. Test: 1000 concurrent saves → verify zero lost entries.
- StreamConsumer + all write-path primitives (Steps 1-6, 9): Full pipeline test — records with WriteFilter gating, ConfidenceField updates, CoOccurrence strengthening, and PredictionLedger resolutions all producing stream entries → consumer processes all entry types correctly.

**Measurable agent improvement:** Not directly agent-facing — this is infrastructure for Step 11. Measure: processing throughput (entries/sec), end-to-end latency (write → consumer acknowledgment), and reliability (zero lost entries under crash recovery via XCLAIM).

**Issues:** ~3 (consumer group framework, dead-letter handling, integration tests with Steps 6+9)

---

## Step 11: PolicyCache Model Pattern — Learned Action Selection ✅ Shipped

**What it is:** A reference implementation (shipped as an example/recipe, not core ORM) showing how to compose Popoto primitives into a reinforcement-learning-based action selection cache. This is the "state→action→outcome" store that crystallizes from repeated successful patterns.

**Implementation:** Shipped in [PR #239](https://github.com/tomcounsell/popoto/pull/239).

**Application layer pattern (not ORM):**

```python
class PolicyEntry(popoto.Model):
    """
    Example: state→action→expected_value triple with RL updates.
    Built entirely from Popoto primitives — no new ORM code needed.
    """
    entry_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    
    # State (when to fire)
    state_fingerprint = popoto.KeyField()
    state_features = popoto.Field()              # JSON
    
    # Action (what to do)
    action_type = popoto.KeyField()
    action_spec = popoto.Field()                 # JSON
    
    # Value tracking — uses ConfidenceField (Step 4) for certainty
    expected_value = DecayingSortedField()        # Step 1: decays without use
    confidence = ConfidenceField()                # Step 4: how sure are we
    
    # Outcome tracking — uses PredictionLedger (Step 9)
    # (composed via application logic, not ORM inheritance)
    
    # Association — uses CoOccurrenceField (Step 5)
    related_policies = CoOccurrenceField()        # Step 5: linked strategies
    
    # Write gating — uses WriteFilterMixin (Step 3)
    # Only crystallize policies with sufficient evidence
    
    class Meta:
        # Uses EventStreamMixin (Step 6) for compaction pipeline
        pass
```

**The Q-value update** is application logic using a Lua script helper from Popoto:

```lua
-- td_update.lua: Temporal difference Q-value update
-- Application registers this script; Popoto provides execute_lua() helper
local current_q = tonumber(redis.call('HGET', KEYS[1], 'expected_value') or '0')
local reward = tonumber(ARGV[1])
local alpha = tonumber(ARGV[2])   -- learning rate, typically 0.1
local gamma = tonumber(ARGV[3])   -- discount factor, typically 0.95
local max_future_q = tonumber(ARGV[4])

local td_error = reward + gamma * max_future_q - current_q
local new_q = current_q + alpha * td_error

redis.call('HSET', KEYS[1], 'expected_value', tostring(new_q))
redis.call('ZADD', KEYS[2], new_q, ARGV[5])  -- Update sorted set index
return tostring(td_error)
```

**The crystallization trigger** is application logic running in the StreamConsumer (Step 10): when the compaction pipeline detects ≥3 event records with the same state fingerprint and action type, and the success rate's Wilson confidence interval lower bound exceeds 0.6, it creates a PolicyEntry.

**Temporal pattern discovery:** The StreamConsumer also performs **temporal clustering** on event timestamps to discover cyclical patterns. When events for a given topic cluster at similar times-of-year, times-of-month, or days-of-week across multiple occurrences, the consumer crystallizes these as cycle parameters on existing memories:

```python
# Temporal clustering in StreamConsumer handler (application layer,
# but uses ORM cycle update primitives):
#
# 1. Bucket events by time-of-year, time-of-month, day-of-week
# 2. Detect statistically significant clusters (e.g., chi-squared test
#    against uniform distribution)
# 3. If cluster detected with p < 0.05:
#    - Compute period (YEARLY, MONTHLY, WEEKLY, etc.)
#    - Compute phase from cluster centroid
#    - Add cycle to memory with low initial amplitude (0.5)
#    - Cycle strengthens or weakens via entrainment (Step 4)
#
# This is how an agent learns "every March I deal with Q1 renewals"
# from raw event data — nobody programs it. Like how a person notices
# "I always feel sluggish in January" after living through a few winters.
```

The key insight: **explicitly programmed cycles and discovered cycles use the same data structure and the same entrainment mechanism.** A cycle added by the developer and a cycle discovered by the StreamConsumer are both just `(period, amplitude, phase)` tuples in the cycles hash. Both strengthen when acted on, weaken when dismissed, and die when confidence drops. The system doesn't distinguish between innate and learned rhythms — just as biology doesn't distinguish at the cellular level.

**Synergy tests — full integration matrix:**
- PolicyEntry uses CyclicDecayField (1), ObservationProtocol + AccessTracker (2), WriteFilter (3), ConfidenceField with entrainment (4), CoOccurrenceField (5), EventStreamMixin (6), CompositeScoreQuery (7), ExistenceFilter (8), PredictionLedger with auto-resolution (9), and StreamConsumer (10). This is the integration test for the entire stack.
- Specific critical path: Event records flow through stream → consumer detects pattern → crystallizes PolicyEntry → PolicyEntry has initial confidence 0.5 → agent queries via CompositeScoreQuery → selects action → observation protocol infers outcome → updates Q-value and confidence → high prediction error triggers priority re-processing.
- Temporal discovery path: Events cluster at similar times-of-year → consumer detects yearly pattern → adds cycle to memory → next year, cycle causes proactive surfacing → agent acts on it → entrainment strengthens the cycle → pattern is now durable.

**Measurable agent improvement:** Run an agent through a 200-task benchmark with ~20 recurring task types. Without PolicyCache: agent approaches each task from scratch. With: agent develops cached policies for recurring patterns. Measure: (a) time-to-completion improvement on repeated task types, (b) success rate improvement on the 5th+ encounter vs. 1st encounter, (c) calibration of expected_value vs. actual outcomes, (d) temporal pattern discovery accuracy — do the right cycles get discovered from event data?

**Issues:** ~4 (reference implementation, crystallization logic in consumer, temporal clustering logic, full integration test suite)

---

## Step 12: ContextAssembler — Retrieval-to-Injection Bridge + Proactive Surfacing ✅ Shipped

**What it is:** A query utility that assembles the optimal context payload for injection into an LLM's message array. It orchestrates the full retrieval pipeline — both **pull-based** (query-driven) and **push-based** (proactive surfacing from cyclical resonance and homeostatic pressure).

**Implementation:** Shipped in [PR #245](https://github.com/tomcounsell/popoto/pull/245).

The ContextAssembler runs two parallel retrieval paths and merges the results:

1. **Pull path:** ExistenceFilter pre-check → CompositeScoreQuery ranking → CoOccurrence propagation → candidates from the agent's current query/topic.
2. **Push path:** Scan CyclicDecayField indexes for records whose cyclical + pressure score exceeds a surfacing threshold, *regardless of the agent's current topic*. These are memories the system believes are temporally relevant right now — the "it's March, time for renewals" memories.

Both paths merge into a single ranked, budget-constrained context payload. Push-path records are annotated as proactive surfacings, creating proposals via the observation protocol (Step 2).

**ORM addition:**

```python
class ContextAssembler:
    """
    Assembles retrieved records into an LLM-ready context payload
    within a token/item budget. Supports both pull and push retrieval.

    Pipeline:
      1. ExistenceFilter pre-check (skip if nothing relevant) [pull]
      2. CompositeScoreQuery with configurable weights [pull]
      3. CyclicDecayField temporal scan — records above surfacing
         threshold from cyclical resonance or homeostatic pressure [push]
      4. CoOccurrence graph propagation from top results [both]
      5. Merge and re-rank all candidates
      6. Budget-constrained selection (max_items, max_tokens)
      7. Create RecallProposals for push-path records [push]
      8. Format output (configurable: JSON, XML, natural language)

    Usage:
      assembler = ContextAssembler(
          model_class=Episode,
          score_weights={"priority": 0.4, "confidence": 0.3, ...},
          propagation_depth=2,
          max_items=10,
          max_tokens=2000,
          surfacing_threshold=0.5,  # min score for push-path records
          output_format="structured"
      )
      context = assembler.assemble(
          query_cues={"topic": "deployment", "project": "satsol"},
          agent_id="agent_1"
      )
      # context.records: List[Model] — ranked, budget-constrained
      # context.proactive: List[Model] — push-path records (subset of records)
      # context.proposals: List[RecallProposal] — pending observation
      # context.metadata: retrieval stats, confidence summary, coverage gaps
      # context.formatted: str — ready for injection into messages array
    """
```

**Post-retrieval effects** (via observation protocol, Step 2):
- **Competitive suppression:** After retrieval, reduce priority scores of non-selected records that competed on the same cues. This sharpens future retrieval.
- **Access tracking:** Pull-path records get `on_read()` logged to staging. Push-path records get `on_surfaced()` logged as proposals. Neither triggers strengthening until `on_context_used()` confirms the agent engaged with the memory.
- **Feedback loop:** After the LLM generates its response, the application layer calls `on_context_used()` with an outcome map. The observation protocol resolves proposals, updates confidence, adjusts cycle parameters via entrainment, and discharges pressure for acted-on items. This closes the loop — the memory system learns from every interaction whether its retrieval decisions (both pull and push) were good.

**Synergy tests — the capstone.** ContextAssembler exercises every prior step:

| Primitive | Role in Assembly Pipeline |
|---|---|
| CyclicDecayField (1) | Decay + cyclical resonance + pressure scoring [pull + push] |
| ObservationProtocol (2) | Creates proposals for push-path; resolves all outcomes post-response |
| AccessTracker (2) | Confirmed-access-frequency component of composite score |
| WriteFilter (3) | Priority-tagged records get score boost |
| ConfidenceField (4) | Confidence component of composite score; entrainment on cycles |
| CoOccurrenceField (5) | Graph propagation expands candidate pool |
| EventStreamMixin (6) | Retrieval events logged for compaction |
| CompositeScoreQuery (7) | Multi-factor ranking of pull-path candidates |
| ExistenceFilter (8) | Fast pre-check: abort early if no relevant records [pull] |
| PredictionLedger (9) | Auto-resolution from observation; calibration of surfacing quality |
| StreamConsumer (10) | Background re-ranking; temporal pattern discovery |
| PolicyCache (11) | Cached policies surface for matching states; discovered cycles |

**Measurable agent improvement — the definitive benchmark:**

Run a realistic agent benchmark (e.g., SWE-bench-lite, customer support resolution, or a custom multi-session task suite) with progressive memory stack activation:

| Configuration | Stack Active | Expected Improvement |
|---|---|---|
| Baseline | None (vanilla RAG) | — |
| +Decay | Step 1 | +5-10% retrieval relevance |
| +Decay+Access | Steps 1-2 | +3-5% additional (spacing effect) |
| +Decay+Access+Filter | Steps 1-3 | +5-10% via noise reduction |
| +All scoring | Steps 1-4 | +3-5% via confidence weighting |
| +Associations | Steps 1-5 | +5-10% on multi-hop queries |
| +Full retrieval | Steps 1-8 | +10-15% overall retrieval quality |
| +Outcome learning | Steps 1-9 | +10-20% on repeated task types |
| +Full stack | Steps 1-12 | Cumulative: 30-50% improvement hypothesis |

Each row is a testable configuration. The benchmark produces a leaderboard showing the marginal contribution of each primitive and the synergies between combinations.

**Issues:** ~4 (assembler pipeline, competitive suppression, output formatting, capstone benchmark suite)

---

## Combinatorial Test Matrix

After all 12 steps ship, the test suite must cover pairwise interactions. Here is the critical subset (not exhaustive — focus on interactions that produce emergent behavior):

| Pair | Test |
|---|---|
| 1+2 (Cyclic+Observer) | Proactive surfacing creates proposal; observation resolves it; confirmed reads strengthen |
| 1+2 (Decay+Access) | Spacing effect: spaced *confirmed* reads produce higher scores than massed reads |
| 1+4 (Cyclic+Confidence) | Entrainment: acted-on cycle strengthens amplitude; dismissed cycle weakens; phase corrects |
| 1+4 (Decay+Confidence) | Low-confidence records decay faster in effective retrieval weight |
| 1 pressure+4 (Pressure+Confidence) | Confidence dropping below threshold auto-discharges homeostatic pressure |
| 2+5 (Observer+CoOccurrence) | Co-retrieved records that both get "acted" outcome auto-strengthen links |
| 2+9 (Observer+Prediction) | Proactive surfacing auto-resolves as prediction; calibration data accumulates |
| 3+6 (Filter+Stream) | Filtered-out records produce no stream entries |
| 3+8 (Filter+Existence) | Filtered records not in Bloom filter |
| 4+9 (Confidence+Prediction) | Consistent prediction errors reduce linked record confidence |
| 5+7 (CoOccurrence+Composite) | Propagated weights boost composite retrieval scores |
| 6+10 (Stream+Consumer) | End-to-end: write → stream → consume → acknowledge |
| 7+8 (Composite+Existence) | Pre-filter short-circuits composite query when nothing exists |
| 9+11 (Prediction+Policy) | High-error predictions trigger policy re-evaluation |
| 10+11 (Consumer+PolicyCache) | Temporal clustering discovers cycles from event timestamps |
| 7+12 (Composite+Assembler) | Assembler merges pull-path and push-path candidates correctly |
| 1+2+4+5+7 (five-way) | Full retrieval path: cyclic decay + observation + confidence + association + composite |
| 1+2+4+9 (four-way) | Full entrainment loop: cycle surfaces → observation infers outcome → prediction recorded → confidence/phase adjusted |
| 3+6+10+11 (four-way) | Full write-to-learning path: filter → stream → consumer → crystallize policy + discover cycles |
| 1+2+9+12 (four-way) | Full push path: cyclic score exceeds threshold → assembler surfaces → observation resolves → prediction logged |
| ALL (twelve-way) | Capstone: full agent benchmark with all primitives active, both pull and push paths |

---

## Implementation Timeline Estimate

| Step | Effort | Dependencies | Cumulative Value |
|---|---|---|---|
| 1. DecayingSortedField + CyclicDecayField | 1.5 weeks | None | Time-aware + cyclical + pressure scoring ✅ |
| 2. ObservationProtocol + AccessTracker | 1 week | Step 1 | Passive behavioral inference, confirmed reads ✅ |
| 3. WriteFilter | 3 days | None | Storage efficiency ✅ |
| 4. ConfidenceField + Entrainment | 1.5 weeks | Steps 1, 2 | Epistemic humility + self-correcting cycles ✅ |
| 5. CoOccurrenceField | 1 week | None | Associative retrieval ✅ |
| 6. EventStreamMixin | 3 days | None | Mutation logging ✅ |
| 7. CompositeScoreQuery | 1 week | Steps 1-5 | **Multi-factor retrieval** ✅ |
| 8. ExistenceFilter | 3 days | None | Fast pre-filtering ✅ |
| 9. PredictionLedger + Auto-Resolution | 1.5 weeks | Steps 2, 4, 6 | Outcome learning + surfacing calibration ✅ |
| 10. StreamConsumer | 1 week | Step 6 | Background processing ✅ |
| 11. PolicyCache + Temporal Discovery | 1-2 weeks | Steps 1-10 | **Learned action selection + discovered cycles** ✅ |
| 12. ContextAssembler + Proactive Surfacing | 1-2 weeks | Steps 1-11 | **Full pull + push retrieval pipeline** ✅ |

Steps 1-2 are tightly coupled (observation protocol needs CyclicDecayField proposals). Steps 3-6 can parallelize with 1-2. Step 4 depends on 1+2 for entrainment integration. Steps 7-12 are sequential.

**Total estimate:** 12-16 weeks for one engineer, shorter with parallelization.

---

## Magic Numbers — Experimentally Validated

> **Status: COMPLETE.** All Category 1 constants have been swept across three benchmark scenarios (factual recall, multi-step reasoning, temporal scheduling). See [Tuning Magic Numbers Guide](tuning-magic-numbers.md) for the full results. Key finding: all defaults are within their safe operating ranges. Only `ACTED_CYCLE_STRENGTHEN_FACTOR` has a cliff effect (must be >= 1.0; default 1.2 is safe).

The primitives accumulate a substantial collection of numeric constants — default thresholds, signal strengths, weighting factors, and structural parameters. These have been validated through systematic parameter sweeps measuring retrieval quality (precision@k, nDCG) and calibration error.

### Category 1: Behavioral Sensitivity — High Impact on Agent Performance

These constants directly affect how the memory system scores, strengthens, weakens, and filters records. Small changes produce measurable differences in retrieval quality and learning speed. These are the highest-priority targets for experimental tuning.

| Constant | Default | Location | What It Controls |
|----------|---------|----------|-----------------|
| `decay_rate` | `0.5` | DecayingSortedField | How fast records lose relevance. Higher = faster forgetting. With default, score halves at 4 days. |
| `pressure_rate` | `0.0` | CyclicDecayField | How fast urgency builds on unresolved items. Zero = disabled. |
| Acted → confidence signal | `0.9` | ObservationProtocol `_apply_acted` | How strongly an "acted" outcome corroborates confidence. |
| Contradicted → confidence signal | `0.1` | ObservationProtocol `_apply_contradicted` | How strongly a "contradicted" outcome penalizes confidence. |
| Acted → cycle strengthen factor | `1.2` | ObservationProtocol `_apply_acted` | How much "acted" strengthens cycle amplitudes (20% boost). |
| Dismissed → cycle weaken factor | `0.8` | ObservationProtocol `_apply_dismissed` | How much "dismissed" weakens cycle amplitudes (20% reduction). |
| Contradicted → cycle weaken factor | `0.5` | ObservationProtocol `_apply_contradicted` | How aggressively "contradicted" weakens cycles (50% reduction). |
| Auto-discharge confidence threshold | `0.1` | ObservationProtocol `_apply_contradicted` | Below this confidence, pressure auto-resolves (memory stops nagging). |
| `_wf_min_threshold` | `0.2` | WriteFilterMixin | Below this score, records are silently discarded on save. |
| `_wf_priority_threshold` | `0.7` | WriteFilterMixin | At or above this score, records get priority-tagged. |
| `initial_confidence` | `0.5` | ConfidenceField | Starting confidence for new records. Affects how many observations are needed to reach certainty. |
| Corroboration/contradiction boundary | `0.5` | ConfidenceField Lua script | Signal >= 0.5 counts as corroboration, < 0.5 as contradiction. |
| `decay_factor` | `0.95` | CoOccurrenceField | Multiplicative decay for `weaken_all()` — how fast associations fade. |
| `initial_weight` | `0.1` | CoOccurrenceField `link()` | Starting weight for newly created association edges. |
| `delta` (strengthen) | `0.05` | CoOccurrenceField `strengthen()` | How much each co-access strengthens an association. |
| `decay_per_hop` | `0.5` | CoOccurrenceField `propagate()` | Weight multiplier per hop in BFS graph propagation. |
| PredictionLedger: acted error | `0.1` | PredictionLedgerMixin `_pl_auto_resolve_errors` | Prediction error assigned when observation outcome is "acted". |
| PredictionLedger: dismissed error | `0.5` | PredictionLedgerMixin `_pl_auto_resolve_errors` | Prediction error assigned when observation outcome is "dismissed". |
| PredictionLedger: contradicted error | `0.9` | PredictionLedgerMixin `_pl_auto_resolve_errors` | Prediction error assigned when observation outcome is "contradicted". |
| PredictionLedger: confidence error threshold | `0.7` | PredictionLedgerMixin `_pl_confidence_error_threshold` | Error above which confidence is reduced via ConfidenceField. |
| PredictionLedger: confidence low signal | `0.2` | PredictionLedgerMixin `_pl_confidence_low_signal` | Signal value sent to ConfidenceField when error exceeds threshold. |

**Experiment approach:** These are the knobs that most affect how quickly the system learns, forgets, and self-corrects. Vary each independently while holding others fixed, measuring retrieval relevance and calibration error across a standardized task suite. Look for cliff effects (small changes in threshold produce large performance swings) and plateaus (ranges where the value doesn't matter much).

### Category 2: Structural Capacity — Application-Dependent

These constants control data structure sizing and capacity limits. Correct values depend on the application's scale (number of records, access frequency, association density). They affect memory usage and query performance more than learning quality.

| Constant | Default | Location | What It Controls |
|----------|---------|----------|-----------------|
| `_max_access_log` | `100` | AccessTrackerMixin | Max confirmed access timestamps kept per instance. |
| `max_edges` | `500` | CoOccurrenceField | Max association edges per PK before pruning lowest-weight. |
| `_stream_max_length` | `10000` | EventStreamMixin | Approximate max entries per Redis Stream (MAXLEN ~). |
| `error_rate` | `0.01` | ExistenceFilter (Bloom) | Target false positive rate. Lower = more bits. |
| `capacity` | `100_000` | ExistenceFilter (Bloom) | Expected distinct items. Exceeding degrades error rate. |
| `width` | `2000` | FrequencySketch (CMS) | Counters per row. Higher = less overcounting. |
| `depth` | `7` | FrequencySketch (CMS) | Number of hash functions. Higher = more accurate. |
| `limit` (get_linked) | `20` | CoOccurrenceField | Max results from get_linked query. |
| `depth` (propagate) | `2` | CoOccurrenceField | BFS traversal depth for graph propagation. |
| `RecallProposal.DEFAULT_TTL` | `3600` | RecallProposal | Seconds before unresolved proposals expire (treated as deferred). |

**Experiment approach:** These are less about tuning and more about sizing. Profile memory usage and query latency at different scales (1K, 10K, 100K, 1M records). Identify where defaults become bottlenecks. For Bloom/CMS parameters, verify false positive rates match theoretical predictions under realistic workloads.

### Category 3: Edge Pruning — Cleanup Thresholds

These constants control when low-value data is pruned. They prevent unbounded growth but their exact values rarely matter — they just need to be "small enough" to catch dead weight without discarding useful data.

| Constant | Default | Location | What It Controls |
|----------|---------|----------|-----------------|
| `weaken_all` prune threshold | `0.001` | CoOccurrenceField | Edges below this weight are deleted after global decay. |
| `min_weight` (get_linked) | `0.01` | CoOccurrenceField | Minimum weight to include in get_linked results. |
| `threshold` (propagate) | `0.01` | CoOccurrenceField | Minimum propagated weight to include in BFS results. |
| Elapsed days min clamp | `0.01` | DecayingSortedField Lua | Prevents division by zero in decay formula. |

**Experiment approach:** Low priority. Verify they don't accidentally prune useful data under sustained usage. Spot-check with long-running integration tests.

### Category 4: Domain Constants — Fixed by Design

These are not tunable — they encode domain definitions (time periods, weight ratios) or algorithm constants (hash seeds). They change only if the domain model changes.

| Constant | Default | Location | What It Controls |
|----------|---------|----------|-----------------|
| `TemporalPeriod.DAILY` | `86_400` | constants.py | 24 hours in seconds |
| `TemporalPeriod.WEEKLY` | `604_800` | constants.py | 7 days in seconds |
| `TemporalPeriod.MONTHLY` | `2_592_000` | constants.py | 30 days in seconds |
| `TemporalPeriod.QUARTERLY` | `7_776_000` | constants.py | 90 days in seconds |
| `TemporalPeriod.YEARLY` | `31_536_000` | constants.py | 365 days in seconds |
| `InteractionWeight.HUMAN` | `6.0` | constants.py | Human source weight (vs agent/system) |
| `InteractionWeight.AGENT` | `1.0` | constants.py | Agent source weight |
| `InteractionWeight.SYSTEM` | `0.2` | constants.py | System source weight |
| `InteractionWeight.EXECUTIVE` | `44.0` | constants.py | Executive role weight |
| `InteractionWeight.MANAGER` | `16.0` | constants.py | Manager role weight |
| `InteractionWeight.PEER` | `6.0` | constants.py | Peer role weight |
| `InteractionWeight.SUBORDINATE` | `1.0` | constants.py | Subordinate role weight |
| Bloom/CMS hash seeds | various | ExistenceFilter/FrequencySketch Lua | DJB2 (5381), FNV-1 (16777619), 2^52 modulus |

**Experiment approach:** InteractionWeight ratios are candidates for tuning in multi-agent deployments, but require multi-agent benchmarks. TemporalPeriod and hash constants are fixed.

### Experiment Planning Notes

When designing the tuning experiments:

1. **Start with Category 1.** These have the highest impact-to-effort ratio. The observation protocol signals (0.9/0.1/1.2/0.8/0.5) and write filter thresholds (0.2/0.7) are the most consequential.

2. **Use the progressive benchmark table** (from Step 12) as the evaluation framework. Each experiment varies one constant while running the full benchmark, measuring marginal impact on retrieval relevance and calibration error.

3. **Look for interaction effects.** Some constants interact: `decay_rate` × `initial_confidence` determines how quickly a new record establishes itself. `_wf_min_threshold` × `initial_weight` determines whether newly linked associations survive the write filter. Test these pairs together.

4. **Record baselines.** Before any tuning, establish baseline metrics with all defaults. Every experiment result should be reported as delta from baseline.

5. **Avoid overfitting to a single benchmark.** Run experiments across at least 3 different task types (factual recall, multi-step reasoning, temporal scheduling) to find values that generalize.

---

## What This Gives Agent Developers

When all 12 steps ship, an agent developer using Popoto gets:

1. **Records that know their own relevance** — priority scores that account for recency, cyclical timing, access patterns, confidence, and co-occurrence, computed at the ORM level with zero application code.

2. **Memories that resurface on rhythm** — cyclical resonance at daily, weekly, monthly, quarterly, and yearly periods. A memory about Q1 renewals naturally peaks every January-March. Like circadian rhythms in biology, these temporal patterns are encoded at the ORM level, not in application scheduling code.

3. **Obligations that nag** — homeostatic pressure builds on unresolved actionable memories, independent of any cycle. Ignored items get louder until acted on, explicitly cancelled, or invalidated by confidence erosion.

4. **Proactive recall** — the system surfaces memories unprompted when cyclical or pressure scores exceed a threshold. The agent doesn't have to ask "what should I remember?" — relevant memories appear in context automatically, like a thought popping into your head.

5. **Passive observation** — the ORM infers whether surfaced memories were useful from downstream behavior, not from explicit management calls. The agent doesn't manage its own memory mechanics, just as humans don't consciously regulate memory consolidation.

6. **Self-correcting temporal patterns** — entrainment adjusts cycle amplitude and phase based on whether proactive surfacings get acted on or dismissed. Stale obligations auto-disable when confidence drops. The system learns its own timing from experience.

7. **Selective memory** — not everything gets stored. Low-value interactions are filtered at write time, keeping the index clean and retrieval fast.

8. **Self-correcting confidence** — knowledge records that become more or less trusted as evidence accumulates, with contradictions automatically reducing retrieval weight.

9. **Associative recall** — retrieving one record activates related records via co-occurrence weights, surfacing multi-hop knowledge without explicit graph queries.

10. **Outcome learning** — agents can predict outcomes before acting, observe actual results, and feed prediction errors back into the memory system. Proactive surfacings are themselves predictions that accumulate calibration data.

11. **Background knowledge extraction** — a Redis-native pipeline that processes raw event records into durable patterns and discovers temporal cycles from event data, without blocking the agent's real-time inference path.

12. **Budget-constrained context assembly** — a single query method that runs the full pull + push retrieval pipeline and returns exactly what the LLM needs, within token limits, with confidence annotations and proactive surfacing proposals.

All of this composes from generic ORM primitives. None of it requires the agent developer to understand neuroscience, reinforcement learning, Bayesian statistics, or chronobiology. They just use Popoto fields and query methods, and their agent gets measurably better at its job — remembering what matters, forgetting what doesn't, and surfacing the right thing at the right time without being asked.

---

## Roadmap Complete

All 12 steps and 14 primitives are shipped. Tuning constants have been centralized in `popoto.fields.constants.Defaults` for experimental sweep readiness. Standalone feature documentation is available for each complex primitive under `docs/features/`. The experimental tuning benchmark harness (`tests/benchmarks/`) covers field-level constants (Tiers 1-3) and recipe-layer constants (Tier 4). Tier 4 adds SubconsciousMemory experiments with multi-turn simulations across three agent scenarios (support agent, coding assistant, research agent), new metrics (extraction F1, token utilization ratio, importance distribution health), and fixture-based deterministic benchmarks.
