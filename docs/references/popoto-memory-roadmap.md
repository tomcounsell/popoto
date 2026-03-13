# Popoto Memory Primitives: Implementation Roadmap

**For:** Tom Counsell, Lead Engineer — Popoto ORM
**From:** Valor, AI Engineering — Yudame
**Date:** March 2026
**Status:** Implementation Spec — Ready for Issue Creation

---

## Executive Summary

This roadmap adds programmable memory infrastructure to Popoto in 12 incremental steps. Each step ships a testable, independently useful ORM primitive. Combined, they give AI agents the capabilities LLMs lack: temporal awareness, outcome learning, confidence tracking, selective encoding, associative retrieval, and background knowledge extraction.

The design draws from neuroscience (Complementary Learning Systems theory, ACT-R cognitive architecture, reinforcement learning) but the naming conventions are rooted in computer science and information systems. We are not simulating a brain — we are building data pipeline primitives that happen to solve the same computational problems brains solve.

**Guiding principles:**

1. **Ship small, test often.** Each step produces a working, independently testable primitive.
2. **ORM primitives, not application logic.** Popoto provides generic field types, mixins, hooks, and query methods. Domain-specific agent memory models are built *on top of* these by application developers.
3. **Redis-native everything.** No Celery, no external brokers. Redis Streams, Lua scripts, sorted sets, Bloom filters — all within the Redis process.
4. **Measurable improvement.** Every step includes a test strategy that demonstrates concrete, quantifiable benefits for agents using the primitive.
5. **Combinatorial testing.** After each new primitive lands, test its interactions with all previously shipped primitives. By Step 12, we have coverage for every meaningful pair and key multi-component integrations.

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

---

## Step 1: DecayingSortedField — Time-Weighted Scoring

**What it is:** A new field mixin that wraps a Redis sorted set where scores decay over time following a power-law function. This is the foundational primitive — nearly every subsequent step depends on time-weighted scoring.

**ORM addition:**

```python
class DecayingSortedField(SortedField):
    """
    Sorted set where scores decay as a power law of time since last update.
    
    Key pattern: $DSF:{ClassName}:{field_name}
    Members scored by: base_score × (elapsed_time)^(-decay_rate)
    
    decay_rate: float = 0.5 (ACT-R default, tunable per field)
    refresh_on_read: bool = True (reading updates last_accessed, slowing decay)
    """
    decay_rate: float = 0.5
    refresh_on_read: bool = True
```

**Lua script:** `decay_scores.lua` — batch-updates scores for all members of a sorted set based on elapsed time. Runs atomically. Registered at connection time.

**Test strategy:**
- Unit: Insert N records with known timestamps, advance clock, verify scores match `score × t^(-0.5)` within tolerance.
- Property: Records accessed recently always outscore older records with same initial score.
- Benchmark: Measure Lua script execution time for 1K, 10K, 100K member sorted sets.

**Measurable agent improvement:** Before DecayingSortedField, agents retrieve memories by insertion order or raw score. After: agents naturally surface recent, frequently-accessed records. Test with a conversational agent handling 100 sessions — measure relevance of top-5 retrieved records (human-rated or LLM-as-judge) with vs. without decay scoring.

**Issues:** ~2-3 (field implementation, Lua script, tests + benchmarks)

---

## Step 2: AccessTracker Mixin — Usage-Aware Records

**What it is:** A model mixin that automatically tracks access patterns: timestamps of each read, total access count, and last-accessed time. Maintains a capped list of access timestamps per record for computing spacing-effect-aware priority scores.

**ORM addition:**

```python
class AccessTrackerMixin:
    """
    Tracks read access patterns on any Model.
    
    Adds fields: access_count (int), last_accessed (float), 
                 access_log (capped list of timestamps, max_length=100)
    
    Hook: on_read(instance, pipeline) — appends timestamp, increments count
    Key pattern: $AT:{ClassName}:access_log:{pk} → List (capped at max_length)
    """
    max_access_log: int = 100
```

**Synergy test with Step 1:** DecayingSortedField + AccessTracker enables the full priority score computation: `B = ln(Σ t_j^(-d))` where t_j comes from the access log. Test that records with spaced access patterns (3 reads over 3 days) produce higher priority scores than records with massed access (3 reads in 1 minute), given equal total reads and age.

**Measurable agent improvement:** Compare agent retrieval quality on a knowledge-base QA task. Baseline: retrieve by recency only. With AccessTracker + DecayingSortedField: retrieve by spacing-effect-aware priority. Measure precision@5 improvement.

**Issues:** ~2 (mixin implementation with on_read hook, synergy tests with Step 1)

---

## Step 3: WriteFilter Mixin — Selective Encoding

**What it is:** A model mixin that gates record persistence based on a configurable scoring function evaluated in the `on_save()` hook. Records below a threshold are silently discarded (raise `SkipSaveException`). Records above a high threshold are tagged for priority processing.

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

## Step 4: ConfidenceField — Bayesian Certainty Tracking

**What it is:** A field type that maintains a Bayesian confidence score updated atomically via Lua script. Each update provides a binary signal (corroborate/contradict) with a weight. The prior becomes harder to shift as evidence accumulates (precision grows with √n).

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

**Synergy tests:**
- ConfidenceField + DecayingSortedField: Records with low confidence should effectively have lower retrieval priority. Test composite scoring: `priority = decay_score × confidence`.
- ConfidenceField + WriteFilter: Contradicted records (confidence dropping below a threshold) should be eligible for directed forgetting (score reduction, not deletion).

**Measurable agent improvement:** Give an agent a knowledge base with 20% deliberately contradictory records. Without ConfidenceField: agent retrieves contradictory records at equal weight, producing inconsistent answers. With ConfidenceField: agent's consistency score improves as contradicted records lose retrieval weight. Measure answer consistency across 50 queries touching contradicted facts.

**Issues:** ~2 (field implementation with Lua script, synergy tests with Steps 1-3)

---

## Step 5: CoOccurrenceField — Weighted Association Edges

**What it is:** A field mixin that maintains weighted, bidirectional edges between model instances using sorted sets. Weights strengthen when records are accessed together (co-retrieval) and decay when not reinforced — the "co-accessed items strengthen their link" principle.

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

## Step 6: EventStreamMixin — Append-Only Mutation Log

**What it is:** A model mixin that automatically XADDs to a Redis Stream on every save, update, or delete. This is the foundation for the compaction pipeline (Step 10) — every mutation is captured as a stream entry with model class, PK, operation type, and key metadata fields.

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

## Step 7: CompositeScoreQuery — Multi-Factor Retrieval

**What it is:** A query method that combines multiple sorted set indexes with configurable weights using ZUNIONSTORE, then returns top-K results by composite score. This is the retrieval engine — the single most important query primitive for agent memory.

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

## Step 8: ExistenceFilter — Fast Pre-Retrieval Check

**What it is:** A field type wrapping Redis Bloom filter (BF.ADD / BF.EXISTS) for O(1) probabilistic membership queries. Answers "have I ever stored a record matching this fingerprint?" without touching any sorted set or hash. False positives possible; false negatives essentially impossible.

**ORM addition:**

```python
class ExistenceFilter(Field):
    """
    Bloom filter for fast "do I have anything relevant?" checks.
    
    Key pattern: $EF:{ClassName}:{field_name} → Redis Bloom filter
    
    on_save hook: BF.ADD with configurable fingerprint function
    
    Methods:
      might_exist(fingerprint: str) -> bool  # BF.EXISTS — O(1)
      definitely_missing(fingerprint: str) -> bool  # !BF.EXISTS
    
    Config:
      error_rate: float = 0.01  # 1% false positive rate
      capacity: int = 100000    # Expected number of entries
      fingerprint_fn: Callable  # How to compute fingerprint from instance
    """
    error_rate: float = 0.01
    capacity: int = 100000
```

Also add `FrequencySketch` wrapping Count-Min Sketch (CMS.INCRBY / CMS.QUERY) for approximate frequency queries.

**Synergy tests:**
- ExistenceFilter + CompositeScoreQuery (Step 7): Use ExistenceFilter as a pre-filter — skip the full composite query if `definitely_missing()` returns True. Test: measure query latency reduction when 70% of queries hit missing topics.
- ExistenceFilter + WriteFilter (Step 3): Filtered-out records should NOT be added to the Bloom filter. Test: save below-threshold record → verify `might_exist()` returns False.

**Measurable agent improvement:** In a retrieval-augmented agent, measure the percentage of retrieval calls that can be short-circuited by the Bloom filter (expected: 30-60% of queries touch topics with no stored records). Measure end-to-end latency reduction.

**Issues:** ~2 (Bloom filter wrapper, CMS wrapper, pre-filter integration tests)

---

## Step 9: PredictionLedger Mixin — Outcome Tracking

**What it is:** A model mixin for recording prediction→outcome pairs. Before an action, the agent writes a prediction (expected outcome, expected duration, expected quality). After the action, it writes the actual outcome. The mixin automatically computes the delta and stores it as a learning signal.

**ORM addition:**

```python
class PredictionLedgerMixin:
    """
    Tracks prediction→outcome pairs with automatic delta computation.
    
    Adds fields: predicted_outcome (JSON), actual_outcome (JSON, nullable),
                 prediction_error (float, nullable), resolved (bool)
    
    Methods:
      record_prediction(instance, predicted: dict, pipeline=None)
      resolve_prediction(instance, actual: dict, pipeline=None)
        → computes delta, sets prediction_error, sets resolved=True
        → ZADD to prediction_error sorted set index
    
    Key pattern: $PL:{ClassName}:errors:{partition_key} → ZSET of PKs by |error|
    """
```

**Synergy tests:**
- PredictionLedger + WriteFilter (Step 3): High prediction errors should produce high filter scores. Test: resolve a prediction with error > 0.7 → verify it gets priority-tagged.
- PredictionLedger + EventStreamMixin (Step 6): Prediction resolutions should appear in the mutation stream. Test: resolve → verify stream entry with old prediction, actual outcome, and delta.
- PredictionLedger + ConfidenceField (Step 4): When predictions are consistently wrong, associated knowledge records' confidence should decrease. Test: 5 consecutive high-error predictions linked to Pattern X → verify X's confidence drops.
- PredictionLedger + DecayingSortedField (Step 1): High-error predictions should decay slower (they're more informative). Test: compare decay rates of high-error vs. low-error predictions.

**Measurable agent improvement:** Run an agent through a task suite where it predicts difficulty/approach before each task. Measure calibration: does `mean(predicted_quality)` converge toward `mean(actual_quality)` over 50 tasks? Without PredictionLedger: no convergence (agent has no outcome memory). With: calibration error should decrease by >30% over the task suite.

**Issues:** ~3 (mixin with predict/resolve methods, delta computation, synergy tests with all prior steps)

---

## Step 10: StreamConsumer — Background Compaction Pipeline

**What it is:** A consumer group framework for processing EventStream entries in batches. This is the background pipeline that transforms raw event records into durable, generalized knowledge. Popoto provides the consumer framework; the application layer provides the compaction logic.

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

## Step 11: PolicyCache Model Pattern — Learned Action Selection

**What it is:** A reference implementation (shipped as an example/recipe, not core ORM) showing how to compose Popoto primitives into a reinforcement-learning-based action selection cache. This is the "state→action→outcome" store that crystallizes from repeated successful patterns.

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

**Synergy tests — full integration matrix:**
- PolicyEntry uses DecayingSortedField (1), AccessTracker (2), WriteFilter (3), ConfidenceField (4), CoOccurrenceField (5), EventStreamMixin (6), CompositeScoreQuery (7), ExistenceFilter (8), PredictionLedger (9), and StreamConsumer (10). This is the integration test for the entire stack.
- Specific critical path: Event records flow through stream → consumer detects pattern → crystallizes PolicyEntry → PolicyEntry has initial confidence 0.5 → agent queries via CompositeScoreQuery → selects action → observes outcome → updates Q-value and confidence → high prediction error triggers priority re-processing.

**Measurable agent improvement:** Run an agent through a 200-task benchmark with ~20 recurring task types. Without PolicyCache: agent approaches each task from scratch. With: agent develops cached policies for recurring patterns. Measure: (a) time-to-completion improvement on repeated task types, (b) success rate improvement on the 5th+ encounter vs. 1st encounter, (c) calibration of expected_value vs. actual outcomes.

**Issues:** ~3 (reference implementation, crystallization logic in consumer, full integration test suite)

---

## Step 12: ContextAssembler — Retrieval-to-Injection Bridge

**What it is:** A query utility that assembles the optimal context payload for injection into an LLM's message array. It orchestrates the full retrieval pipeline: ExistenceFilter pre-check → CompositeScoreQuery ranking → CoOccurrence propagation → budget-constrained selection → formatted output.

**ORM addition:**

```python
class ContextAssembler:
    """
    Assembles retrieved records into an LLM-ready context payload
    within a token/item budget.
    
    Pipeline:
      1. ExistenceFilter pre-check (skip if nothing relevant)
      2. CompositeScoreQuery with configurable weights
      3. CoOccurrence graph propagation from top results
      4. Re-rank merged candidates
      5. Budget-constrained selection (max_items, max_tokens)
      6. Format output (configurable: JSON, XML, natural language)
    
    Usage:
      assembler = ContextAssembler(
          model_class=Episode,
          score_weights={"priority": 0.4, "confidence": 0.3, ...},
          propagation_depth=2,
          max_items=10,
          max_tokens=2000,
          output_format="structured"
      )
      context = assembler.assemble(
          query_cues={"topic": "deployment", "project": "satsol"},
          agent_id="agent_1"
      )
      # context.records: List[Model] — ranked, budget-constrained
      # context.metadata: retrieval stats, confidence summary, coverage gaps
      # context.formatted: str — ready for injection into messages array
    """
```

**Post-retrieval effects** (application layer, but Popoto provides hooks):
- **Competitive suppression:** After retrieval, reduce priority scores of non-selected records that competed on the same cues. This sharpens future retrieval.
- **Access tracking:** All retrieved records get `on_read` called, updating AccessTracker and strengthening co-occurrence links between co-retrieved records.

**Synergy tests — the capstone.** ContextAssembler exercises every prior step:

| Primitive | Role in Assembly Pipeline |
|---|---|
| ExistenceFilter (8) | Fast pre-check: abort early if no relevant records |
| CompositeScoreQuery (7) | Multi-factor ranking of candidates |
| DecayingSortedField (1) | Time-decay component of composite score |
| AccessTracker (2) | Usage-frequency component of composite score |
| WriteFilter (3) | Priority-tagged records get score boost |
| ConfidenceField (4) | Confidence component of composite score |
| CoOccurrenceField (5) | Graph propagation expands candidate pool |
| PredictionLedger (9) | Prediction error history modulates confidence |
| EventStreamMixin (6) | Retrieval events logged for compaction |
| StreamConsumer (10) | Background re-ranking from retrieval patterns |
| PolicyCache (11) | Cached policies surface for matching states |

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
| 1+2 (Decay+Access) | Spacing effect: spaced reads produce higher scores than massed reads |
| 1+4 (Decay+Confidence) | Low-confidence records decay faster in effective retrieval weight |
| 2+5 (Access+CoOccurrence) | Co-accessed records auto-strengthen; verify weight increase |
| 3+6 (Filter+Stream) | Filtered-out records produce no stream entries |
| 3+8 (Filter+Existence) | Filtered records not in Bloom filter |
| 4+9 (Confidence+Prediction) | Consistent prediction errors reduce linked record confidence |
| 5+7 (CoOccurrence+Composite) | Propagated weights boost composite retrieval scores |
| 6+10 (Stream+Consumer) | End-to-end: write → stream → consume → acknowledge |
| 7+8 (Composite+Existence) | Pre-filter short-circuits composite query when nothing exists |
| 9+11 (Prediction+Policy) | High-error predictions trigger policy re-evaluation |
| 7+12 (Composite+Assembler) | Assembler correctly delegates to composite scorer |
| 1+2+4+5+7 (five-way) | Full retrieval path: decay + access + confidence + association + composite ranking |
| 3+6+10+11 (four-way) | Full write-to-learning path: filter → stream → consumer → crystallize policy |
| ALL (twelve-way) | Capstone: full agent benchmark with all primitives active |

---

## Implementation Timeline Estimate

| Step | Effort | Dependencies | Cumulative Value |
|---|---|---|---|
| 1. DecayingSortedField | 1 week | None | Time-aware scoring |
| 2. AccessTracker | 3 days | Step 1 | Usage-aware retrieval |
| 3. WriteFilter | 3 days | None | Storage efficiency |
| 4. ConfidenceField | 1 week | None | Epistemic humility |
| 5. CoOccurrenceField | 1 week | None | Associative retrieval |
| 6. EventStreamMixin | 3 days | None | Mutation logging |
| 7. CompositeScoreQuery | 1 week | Steps 1-5 | **Multi-factor retrieval** |
| 8. ExistenceFilter | 3 days | None | Fast pre-filtering |
| 9. PredictionLedger | 1 week | Steps 4, 6 | Outcome learning |
| 10. StreamConsumer | 1 week | Step 6 | Background processing |
| 11. PolicyCache | 1-2 weeks | Steps 1-10 | **Learned action selection** |
| 12. ContextAssembler | 1-2 weeks | Steps 1-11 | **Full retrieval pipeline** |

Steps 1-6 can partially parallelize (1-2 and 3-6 are independent tracks). Steps 7-12 are sequential.

**Total estimate:** 10-14 weeks for one engineer, shorter with parallelization.

---

## What This Gives Agent Developers

When all 12 steps ship, an agent developer using Popoto gets:

1. **Records that know their own relevance** — priority scores that account for recency, access patterns, confidence, and co-occurrence, computed at the ORM level with zero application code.

2. **Selective memory** — not everything gets stored. Low-value interactions are filtered at write time, keeping the index clean and retrieval fast.

3. **Self-correcting confidence** — knowledge records that become more or less trusted as evidence accumulates, with contradictions automatically reducing retrieval weight.

4. **Associative recall** — retrieving one record activates related records via co-occurrence weights, surfacing multi-hop knowledge without explicit graph queries.

5. **Outcome learning** — agents can predict outcomes before acting, observe actual results, and feed prediction errors back into the memory system to improve future predictions and confidence.

6. **Background knowledge extraction** — a Redis-native pipeline that processes raw event records into durable patterns without blocking the agent's real-time inference path.

7. **Budget-constrained context assembly** — a single query method that runs the full retrieval pipeline and returns exactly what the LLM needs, within token limits, with confidence annotations.

All of this composes from generic ORM primitives. None of it requires the agent developer to understand neuroscience, reinforcement learning, or Bayesian statistics. They just use Popoto fields and query methods, and their agent gets measurably better at its job.
