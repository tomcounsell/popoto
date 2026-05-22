# ContextAssembler

Retrieval-to-injection bridge — assembles LLM-ready context within token budgets by orchestrating pull-path (query-driven) and push-path (proactive surfacing) retrieval across all Popoto memory primitives.

## Overview

`ContextAssembler` provides a single `assemble()` call that:

1. **Pull path**: ExistenceFilter pre-check → retrieval → CoOccurrence propagation
2. **Push path**: CyclicDecayField temporal scan above surfacing threshold
3. **Merge**: Deduplicate, re-rank, budget-select, post-effects, format

### Pull Path Modes (`retrieval_mode`)

The pull path supports three modes controlled by the `retrieval_mode` constructor parameter:

| Mode | Behaviour | When to use |
|------|-----------|-------------|
| `"auto"` *(default)* | Detects `BM25Field` + `EmbeddingField` on the model; uses `"hybrid"` if both present, `"composite"` otherwise | Most callers — no configuration needed |
| `"hybrid"` | BM25 (lexical) + vector (semantic) fused via RRF (k=60), optional CoOccurrence graph expansion | Models with both `BM25Field` and `EmbeddingField` configured |
| `"composite"` | Original `CompositeScoreQuery` weighted-sum path (pre-v1.7 behaviour) | Backwards-compatible override; when `score_weights` drive all ranking |

`retrieval_mode="hybrid"` raises `QueryException` at init if `BM25Field` or `EmbeddingField` is absent from the model.

```python
# Hybrid mode — auto-detected when BM25Field + EmbeddingField are on the model
assembler = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 0.6},  # ignored in hybrid pull path
    max_items=10,
)
# retrieval_mode defaults to "auto"; resolves to "hybrid" if both fields present

# Force composite path (pre-v1.7 behaviour)
assembler = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 0.6, "confidence": 0.3},
    retrieval_mode="composite",
)

# Force hybrid explicitly (raises QueryException if fields absent)
assembler = ContextAssembler(
    model_class=Memory,
    score_weights={},
    retrieval_mode="hybrid",
)
```

## Primitive Synergy

| Primitive | Role in ContextAssembler |
|-----------|--------------------------|
| DecayingSortedField | Score index for CompositeScoreQuery |
| CyclicDecayField | Push-path proactive surfacing |
| ConfidenceField | Score index + competitive suppression |
| CoOccurrenceField | Pull-path graph expansion (both paths) |
| ExistenceFilter | Pull-path pre-check (skip if absent) |
| BM25Field | Hybrid pull-path: lexical signal for RRF |
| EmbeddingField | Hybrid pull-path: vector signal for RRF |
| AccessTrackerMixin | on_read post-effect tracking |
| ObservationProtocol | on_read / on_surfaced dispatch |
| RecallProposal | Created for push-path records |
| WriteFilterMixin | Priority score in composite |
| EventStreamMixin | Mutation logging (via model save) |
| PredictionLedgerMixin | Outcome tracking (via model save) |
| CompositeScoreQuery | Multi-factor ranked retrieval (composite mode) |

## Usage

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

# result.records — selected instances
# result.proactive — push-path subset
# result.formatted — LLM-ready string
# result.metadata — scores, timing, token counts
```

### AssemblyResult

The `assemble()` call returns an `AssemblyResult` dataclass:

| Field | Type | Description |
|-------|------|-------------|
| `records` | `list` | Selected model instances, ranked |
| `proactive` | `list` | Subset of records from push-path |
| `formatted` | `str` | LLM-ready formatted string |
| `metadata` | `dict` | Scores, timing, token counts |

## Tuning Constants

```python
from popoto.fields.constants import Defaults
```

| Constant | Default | Optimal Range | Description |
|----------|---------|---------------|-------------|
| `COMPETITIVE_SUPPRESSION_SIGNAL` | 0.3 | [0.1, 0.7] | Signal for suppressing non-selected pull-path candidates |
| `DEFAULT_SURFACING_THRESHOLD` | 0.5 | [0.1, 0.9] | Minimum score for push-path records |

Additional non-tunable defaults:

| Constant | Default | Description |
|----------|---------|-------------|
| `DEFAULT_MAX_ITEMS` | 10 | Maximum records returned |
| `DEFAULT_PROPAGATION_DEPTH` | 2 | BFS depth for CoOccurrence propagation |

## Pipeline Details

### Pull Path — Composite mode

1. **ExistenceFilter pre-check**: Skip query entirely if no matching topics exist (O(1)).
2. **CompositeScoreQuery**: Multi-factor ranked retrieval combining decay scores, confidence, and priority weights.
3. **CoOccurrence propagation**: BFS expansion from seed records to find associatively related memories.

### Pull Path — Hybrid mode (`"hybrid"` or auto-detected)

1. **ExistenceFilter pre-check**: Same short-circuit as composite path.
2. **BM25 lexical retrieval**: `BM25Field.search(query_text, limit=max_items×5)` — scored keyword matches.
3. **Vector retrieval**: `QueryBuilder._get_vector_scores(query_text, limit=max_items×5)` — cosine similarity via configured embedding provider.
4. **CoOccurrence graph expansion**: BFS from BM25 top-5 seeds (optional, requires `CoOccurrenceField`).
5. **RRF fusion**: `query.fuse(keyword=..., vector=..., graph=..., k=60, limit=max_items×2)` — rank-based fusion.

If both BM25 and vector signals return empty results, the path falls back to the composite path automatically.

### Push Path

1. **CyclicDecayField scan**: Find records whose cyclic + pressure score exceeds `DEFAULT_SURFACING_THRESHOLD`.
2. **RecallProposal creation**: Track surfaced records via `ObservationProtocol.on_surfaced()`.

### Merge and Budget

1. **Deduplicate**: Records appearing in both paths are kept once.
2. **Re-rank**: Combined score from both paths.
3. **Budget-select**: Fit within `max_items` and `max_tokens` constraints.
4. **Post-effects**: Fire `ObservationProtocol.on_read()` for selected records.
5. **Competitive suppression**: Non-selected pull-path candidates receive a mild contradiction signal via ConfidenceField.

## LLM Integration

Wire assembled context into an LLM call using the OpenAI SDK v1+:

```python
from openai import OpenAI
from popoto import ContextAssembler, ObservationProtocol

client = OpenAI()  # uses OPENAI_API_KEY env var

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

# Build messages with injected memory context
messages = [
    {"role": "system", "content": f"You are a helpful assistant.\n\nRelevant context:\n{result.formatted}"},
    {"role": "user", "content": "What's our deployment strategy?"},
]

# Call the LLM
response = client.chat.completions.create(
    model="gpt-4.1-nano",
    messages=messages,
)

answer = response.choices[0].message.content

# Report outcomes — which memories did the agent actually use?
outcome_map = {r.db_key.redis_key: "acted" for r in result.records}
ObservationProtocol.on_context_used(result.records, outcome_map)
```

## Retrieval Quality Scoring

To score the quality of a retrieval — avg confidence, feeling-of-knowing, score spread, staleness — pass `assess_quality=True` to `assemble()` or call the standalone `assess()` probe before retrieval:

```python
# Pre-retrieval probe (cheap — no propagation, no push path)
quality = assembler.assess({"topic": "deployment"})
if quality.fok_score < 0.3:
    return  # skip retrieval; memory store has nothing relevant

# Post-retrieval quality attached to metadata
result = assembler.assemble({"topic": "deployment"}, assess_quality=True)
quality = result.metadata["quality"]  # RetrievalQuality dataclass
print(quality.avg_confidence, quality.fok_score)
```

See [Metacognitive Layer](metacognitive-layer.md) for full documentation of `RetrievalQuality`, all four metrics, the `assess()` method, and the `AdaptiveAssembler` keep/revert loop.

## See Also

- [Metacognitive Layer](metacognitive-layer.md) — retrieval quality scoring, FOK, and adaptive weight tuning
- [PolicyCache](policy-cache.md) — learned action selection (uses ContextAssembler for retrieval)
- [Hybrid Retrieval](hybrid-retrieval.md) — BM25Field, EmbeddingField, and RRF fusion primitives
- [CompositeScoreQuery](composite-score-query.md) — multi-factor retrieval (composite mode)
- [CoOccurrenceField](co-occurrence-field.md) — associative expansion
- [Agent Memory overview](agent-memory.md) — full primitives reference
- [Subconscious Memory Recipe](../guides/subconscious-memory-recipe.md) — automatic memory injection and extraction around LLM turns
