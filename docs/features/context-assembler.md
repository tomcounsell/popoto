# ContextAssembler

Retrieval-to-injection bridge — assembles LLM-ready context within token budgets by orchestrating pull-path (query-driven) and push-path (proactive surfacing) retrieval across all Popoto memory primitives.

## Overview

`ContextAssembler` provides a single `assemble()` call that:

1. **Pull path**: ExistenceFilter pre-check -> CompositeScoreQuery -> CoOccurrence propagation
2. **Push path**: CyclicDecayField temporal scan above surfacing threshold
3. **Merge**: Deduplicate, re-rank, budget-select, post-effects, format

## Primitive Synergy

| Primitive | Role in ContextAssembler |
|-----------|--------------------------|
| DecayingSortedField | Score index for CompositeScoreQuery |
| CyclicDecayField | Push-path proactive surfacing |
| ConfidenceField | Score index + competitive suppression |
| CoOccurrenceField | Pull-path candidate expansion |
| ExistenceFilter | Pull-path pre-check (skip if absent) |
| AccessTrackerMixin | on_read post-effect tracking |
| ObservationProtocol | on_read / on_surfaced dispatch |
| RecallProposal | Created for push-path records |
| WriteFilterMixin | Priority score in composite |
| EventStreamMixin | Mutation logging (via model save) |
| PredictionLedgerMixin | Outcome tracking (via model save) |
| CompositeScoreQuery | Multi-factor ranked retrieval |

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

### Pull Path

1. **ExistenceFilter pre-check**: Skip query entirely if no matching topics exist (O(1)).
2. **CompositeScoreQuery**: Multi-factor ranked retrieval combining decay scores, confidence, and priority weights.
3. **CoOccurrence propagation**: BFS expansion from seed records to find associatively related memories.

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

## See Also

- [PolicyCache](policy-cache.md) — learned action selection (uses ContextAssembler for retrieval)
- [CompositeScoreQuery](composite-score-query.md) — multi-factor retrieval
- [CoOccurrenceField](co-occurrence-field.md) — associative expansion
- [Agent Memory overview](agent-memory.md) — full primitives reference
- [Subconscious Memory Recipe](../guides/subconscious-memory-recipe.md) — automatic memory injection and extraction around LLM turns
