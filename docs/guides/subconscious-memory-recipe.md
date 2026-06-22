# Subconscious Memory Recipe

> **New to Agent Memory?** Start with the [Quickstart Guide](agent-memory-quickstart.md) for a progressive adoption path.

Automatic memory injection and extraction around every LLM turn. The agent's memory works silently -- assembling context before each call and saving new observations after each response -- without the application needing to manage memory explicitly.

!!! note "Retrieval mode determines query-sensitivity"
    Whether retrieved memories are selected by query text depends on which fields are on your model. With `BM25Field` only, retrieval is **query-sensitive** (lexical mode). With both `BM25Field` and `EmbeddingField`, retrieval is **query-sensitive** (hybrid mode). With neither field, retrieval is **query-blind** (composite mode) — memories are ranked by importance/confidence scores, not by relevance to the user's query. See [ContextAssembler retrieval modes](../features/context-assembler.md#pull-path-modes-retrieval_mode) for details.

    `SubconsciousMemory` uses `retrieval_mode='auto'` — the effective mode is determined by which fields are on the model at init time. Adding or removing `BM25Field`/`EmbeddingField` changes the effective mode without any change at the `SubconsciousMemory` call site. Adding `EmbeddingField` to a BM25-only model silently flips `lexical → hybrid`.

## Architecture

```
User message
    |
    v
[Pre-turn: ContextAssembler.assemble() -> inject into system message]
    |
    v
[LLM inference]
    |
    v
[Post-turn: extract facts from response -> save as Memory records]
    |
    v
[Outcome: report acted/dismissed/contradicted via ObservationProtocol]
    |
    v
Agent response
```

## Quick Start

```python
from popoto import (
    Model, AutoKeyField, KeyField, StringField, FloatField,
    DecayingSortedField, ConfidenceField, BM25Field,
    WriteFilterMixin, AccessTrackerMixin,
)
from popoto.recipes.subconscious_memory import SubconsciousMemory

# Define your Memory model (any level from the quickstart guide)
class Memory(WriteFilterMixin, AccessTrackerMixin, Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    confidence = ConfidenceField(initial_confidence=0.5)
    content_bm25 = BM25Field(source="content")  # keyword search index

    _wf_min_threshold = 0.1  # default after sweep 2026-04-17 (was 0.2)
    _wf_priority_threshold = 0.7

    def compute_filter_score(self):
        return self.importance or 0.0

# Create the subconscious memory layer
sm = SubconsciousMemory(
    model_class=Memory,
    agent_id="agent-1",
    score_weights={"relevance": 0.6, "confidence": 0.3},
    max_items=10,
    max_tokens=4000,
)
```

### Reindexing Existing Records

`BM25Field` populates its keyword index via the `on_save()` hook. **New records are indexed automatically.** Existing records saved before `content_bm25` was added are not in the index and will not appear in BM25-driven retrieval.

To backfill, re-save every record once:

```python
for memory in Memory.query.filter():
    memory.save()
```

Run this once after adding `BM25Field` to an existing model. The operation is idempotent -- re-running it is safe.

## OpenAI SDK Integration

Wire subconscious memory into a standard OpenAI chat completion call:

```python
from openai import OpenAI

client = OpenAI()  # uses OPENAI_API_KEY env var

messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What's our deployment strategy?"},
]

# Pre-turn: inject memories into messages
# (query-sensitive if BM25Field/EmbeddingField on model; query-blind composite otherwise)
messages, assembly_result = sm.inject_context(messages)

# Call the LLM (messages now include memory context in the system message)
response = client.chat.completions.create(
    model="gpt-4.1-nano",
    messages=messages,
)
answer = response.choices[0].message.content

# Post-turn: extract facts from the response and save as new memories
new_memories = sm.extract_memories(answer, importance=0.6)

# Report outcomes: memories were used successfully
sm.report_outcomes(assembly_result, outcome="acted")
```

## How It Works

### Pre-turn: `inject_context(messages)`

1. Extracts the last user message as a query cue
2. Calls `ContextAssembler.assemble()` with the agent's memory model
3. Appends the formatted context to the system message (creates one if absent)
4. Returns the modified messages and an `AssemblyResult` for later outcome reporting

If no memories are found (or all are filtered), messages are returned unchanged.

The query cue is only meaningful in **query-sensitive** modes (lexical or hybrid, requiring `BM25Field`). In composite mode (no `BM25Field`, no `EmbeddingField`), the query cue is ignored and ranking is driven purely by importance/confidence scores.

### Post-turn: `extract_memories(response_text, importance)`

1. Splits the LLM response into sentences
2. Filters out sentences shorter than `extraction_min_length` (default 10 chars)
3. Saves each sentence as a new Memory record with the specified importance

The built-in extraction uses a simple sentence-splitting heuristic. For more accurate extraction, override this method or extract facts using a secondary LLM call and save them directly via your model class.

### Outcome: `report_outcomes(assembly_result, outcome)`

Reports how the agent used the injected memories via `ObservationProtocol.on_context_used()`. Outcomes strengthen or weaken memories for future retrieval:

- `"acted"` -- the agent used this memory (strengthens confidence)
- `"dismissed"` -- the agent ignored this memory (mild weakening)
- `"contradicted"` -- the agent found this memory incorrect (strong weakening)
- `"deferred"` -- the agent noted but deferred action (neutral)
- `"used"` -- the memory informed reasoning without appearing in the response (confirms access, no strength signal)

## Tuning

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_items` | 10 | Maximum memories injected per turn |
| `max_tokens` | 4000 | Token budget for injected context (enforced; see [Token Budget Semantics](../features/context-assembler.md#token-budget-semantics)) |
| `extraction_min_length` | 10 | Minimum chars for a sentence to become a memory |
| `score_weights` | (required) | Weight dict for composite scoring (e.g. `{"relevance": 0.6, "confidence": 0.3}`) |
| `system_preamble` | "You are a helpful assistant." | Prefix for auto-created system messages |
| `content_field` | "content" | Name of the text content field on your model |
| `importance_field` | "importance" | Name of the importance score field |

These constants can be tuned experimentally using the Tier 4 benchmark harness. See the [Tuning Magic Numbers](tuning-magic-numbers.md) guide for the full constant catalog, optimal ranges, and how to run parameter sweeps.

## Extensibility

### Custom Fact Extraction

Subclass `SubconsciousMemory` and override `extract_memories()` for LLM-based extraction:

```python
class SmartSubconsciousMemory(SubconsciousMemory):
    def extract_memories(self, response_text, importance=0.5):
        # Use a secondary LLM call to extract structured facts
        facts = my_extraction_function(response_text)
        saved = []
        for fact in facts:
            m = self.model_class(
                agent_id=self.agent_id,
                content=fact["text"],
                importance=fact.get("importance", importance),
            )
            m.save()
            saved.append(m)
        return saved
```

### Custom Query Cues

The default implementation uses the last user message as the query cue. For more sophisticated cue extraction, subclass and override the relevant portion of `inject_context()`.

## See Also

- [Agent Memory Quickstart](agent-memory-quickstart.md) -- progressive adoption guide
- [ContextAssembler](../features/context-assembler.md) -- retrieval-to-injection bridge
- [PolicyCache Recipe](policy-cache-recipe.md) -- RL-style learned action selection
- [Trajectory Memory Recipe](trajectory-memory-recipe.md) -- fingerprint-keyed procedural memory: cluster completed task trajectories and recall "what worked last time"
