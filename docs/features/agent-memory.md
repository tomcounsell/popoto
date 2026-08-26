# Agent Memory

> **Getting started?** The [Agent Memory Quickstart](../guides/agent-memory-quickstart.md)
> is a progressive 6-level adoption guide. This page is the map: what each piece
> is for, how the pieces compose, and where the full reference for each one
> lives.

Popoto Agent Memory is a set of Redis-backed ORM primitives for programmable
memory. Records decay over time, strengthen through use, carry confidence that
moves with evidence, form associations, and get assembled into LLM context
within a token budget.

They are generic field types, mixins, and query methods. They encode no
particular agent architecture. You compose them into memory models the same way
you compose `KeyField`, `SortedField`, and `Relationship` into any Popoto model.

## What LLMs cannot do for themselves

A language model reasons well over whatever is in its context window. What it
cannot do unaided is decide what belongs there:

- **Prioritize by recency and importance.** Know which records are hot now.
- **Learn from outcomes.** Track what worked and what did not.
- **Manage certainty.** Downweight contradicted knowledge automatically.
- **Retrieve associatively.** Surface related records without an explicit graph query.
- **Filter noise.** Keep low-value observations out of storage entirely.

Each primitive below covers one of those. Each is independently useful.

## The 17 primitives

| Primitive | What it does | Full reference |
|-----------|--------------|----------------|
| DecayingSortedField | Time-weighted scoring: records lose relevance unless refreshed | [page](decaying-sorted-field.md) |
| CyclicDecayField | Temporal rhythms and homeostatic pressure on top of decay | [page](cyclic-decay-field.md) |
| ConfidenceField | Capped-evidence Bayesian certainty: corroboration strengthens, contradiction weakens | [page](confidence-field.md) |
| CoOccurrenceField | Weighted association edges, strengthened by co-access, traversed by BFS | [page](co-occurrence-field.md) |
| BM25Field | Ranked keyword search in Redis sorted sets, and what makes retrieval query-sensitive | [page](hybrid-retrieval.md#bm25field) |
| CompositeScoreQuery | Multi-factor retrieval: combine N sorted indexes with weights, server-side | [page](composite-score-query.md) |
| ExistenceFilter | Bloom filter for O(1) "do I know anything about X?" | [page](existence-filter.md) |
| FrequencySketch | Count-Min Sketch for approximate frequency counting | [reference](../fields.md#frequencysketch) |
| PredictionLedger | Record a prediction, observe the outcome, feed the error back into confidence | [page](prediction-ledger.md) |
| ObservationProtocol | Outcome-driven effects: acted, dismissed, deferred, contradicted, used | [page](observation-protocol.md) |
| AccessTrackerMixin | Two-stage read tracking: reads stage cheaply, then promote on confirmation | [reference](../fields.md#accesstrackermixin) |
| WriteFilterMixin | Gates persistence: low-value records are discarded before they reach Redis | [reference](../fields.md#writefiltermixin) |
| NeverRecordMixin | Deterministic privacy gate: credentials and secrets are blocked before they reach Redis | [page](never-record-firewall.md) |
| EventStreamMixin | Append-only mutation log via Redis Streams | [reference](../fields.md#eventstreammixin) |
| TagField | Optional multi-value scoping for a centrally hosted Redis serving many agents | [reference](../fields.md#tagfield) |
| ValidityField | Bitemporal validity intervals: a record is either a member of default retrieval or it isn't | [page](validity-and-supersession.md) |
| SupersessionProtocol | Write-side vocabulary for "this claim replaces what was previously believed" | [page](validity-and-supersession.md) |
| AppendOnlyMixin | Write-once records: no in-place mutation, no delete, corrections are new records | [page](provenance-journal.md) |

## The layers composed on top

These are recipes and policy layers built from the primitives above. They are
counted separately, never summed into the primitive count.

| Layer | What it does | Full reference |
|-------|--------------|----------------|
| ContextAssembler | The capstone: one `assemble()` call runs query-driven retrieval, proactive surfacing, budgeting, and formatting | [page](context-assembler.md) |
| Hybrid Retrieval | Fuses keyword, vector, and graph signals via weighted Reciprocal Rank Fusion | [page](hybrid-retrieval.md) |
| SubconsciousMemory | Wraps a chat loop: inject before the model call, extract after, report outcomes | [recipe](../guides/subconscious-memory-recipe.md) |
| ProvenanceJournal | Append-only attributed entries — speaker, turn, verbatim span — with confirm/supersede/retract annotations | [page](provenance-journal.md) |
| Auditable Extraction | Opt-in candidate/verdict/decision-log pipeline: every candidate gets a logged terminal verdict, precision/recall computable offline | [page](auditable-extraction.md) |
| MemoryLifecycle | Episodic-to-semantic promotion, confidence-aware forgetting, restorable tombstones | [recipe](../recipes.md#memorylifecycle) |
| PolicyCache | Reinforcement-learning-style action selection over crystallized state-action-outcome patterns | [page](policy-cache.md) |
| StreamConsumer | Consumer-group framework for background processing of the mutation stream | [recipe](../recipes.md#streamconsumer) |
| Metacognitive Layer | Retrieval-quality scoring, grouped error analysis, and a self-adjusting assembler | [page](metacognitive-layer.md) |

## How the pieces compose

```text
                      ┌──────────────────────────────┐
  your turn loop ───► │      SubconsciousMemory      │
                      └──────────────┬───────────────┘
                                     ▼
                      ┌──────────────────────────────┐
                      │       ContextAssembler       │
                      └──────────────┬───────────────┘
                   ┌─────────────────┼─────────────────┐
                   ▼                 ▼                 ▼
            pull (query)      push (proactive)    post-effects
       ExistenceFilter        CyclicDecayField    AccessTracker
       BM25Field / RRF        surfacing threshold ObservationProtocol
       CompositeScoreQuery                        competitive suppression
       CoOccurrence BFS
                   │                 │                 │
                   └─────────────────┼─────────────────┘
                                     ▼
                         merge, dedupe, token budget
                                     ▼
                        AssemblyResult → your prompt

  underneath, on every record:
    DecayingSortedField · ConfidenceField · WriteFilterMixin ·
    NeverRecordMixin · PredictionLedger · TagField · EventStreamMixin
  over time, across the corpus:
    MemoryLifecycle: promote, forget, tombstone, restore
```

The pull path answers "what matches this query". The push path answers "what
should this agent be thinking about anyway". `assemble()` runs both, merges
them, and enforces one budget across the result.

Which arms actually run is decided by which fields your model declares. A model
with no `CyclicDecayField` has no push path. A model with no `BM25Field`
resolves to [query-blind ranking](../guides/query-blind-retrieval.md), which is
the right choice for some workloads and quietly wrong for others.

## Design principles

1. **ORM primitives, not application logic.** Popoto ships fields, mixins,
   hooks, and query methods. Domain-specific memory models are yours to build
   on top.

2. **Redis-native everything.** No external brokers, job queues, or Redis
   modules. Lua scripts, sorted sets, streams, and Bloom filters over
   `SETBIT`/`GETBIT` all run inside the Redis process, so the same code runs
   identically on Redis and Valkey.

3. **Composable, degrading gracefully.** Each primitive is independently
   useful, and every layer adapts to whichever fields are present rather than
   requiring the full set.

4. **Pipeline-safe.** Every operation accepts an optional `pipeline` parameter
   for atomic execution, consistent with all Popoto field hooks.

5. **Centralized tuning.** Behavioral constants live in `Defaults`, importable
   from the package root. Override globally or per field. Explicit kwargs
   always win.

```python
from popoto import Defaults

# Global override: all DecayingSortedFields default to 0.7
Defaults.DECAY_RATE = 0.7

# Per-field override still wins
relevance = DecayingSortedField(decay_rate=0.3)  # uses 0.3, not 0.7
```

See [Defaults API reference](../reference/popoto/fields/constants.md) and
[Tuning Magic Numbers](../guides/tuning-magic-numbers.md) for the full constant
table and the empirical basis behind each value.

## Where to go next

- **Building something:** [Quickstart](../guides/agent-memory-quickstart.md),
  then the [SubconsciousMemory recipe](../guides/subconscious-memory-recipe.md)
- **Deciding whether retrieval will respond to your queries:**
  [Query-blind retrieval](../guides/query-blind-retrieval.md)
- **Checking the claims:** [Benchmarks](../benchmarks.md), including the
  measurements that came out badly
- **Tuning:** [Magic Numbers](../guides/tuning-magic-numbers.md) and
  [Parametric Sweep](parametric-sweep.md)
- **Field-level API:** [Models and Fields](../fields.md) and the
  [API Reference](../reference/index.md)
