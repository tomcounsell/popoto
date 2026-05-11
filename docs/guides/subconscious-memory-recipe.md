# Subconscious Memory Recipe

> **New to Agent Memory?** Start with the [Quickstart Guide](agent-memory-quickstart.md) for a progressive adoption path.

Automatic memory injection and extraction around every LLM turn. The agent's memory works silently -- assembling relevant context before each call and saving new observations after each response -- without the application needing to manage memory explicitly.

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
    DecayingSortedField, ConfidenceField,
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

## OpenAI SDK Integration

Wire subconscious memory into a standard OpenAI chat completion call:

```python
from openai import OpenAI

client = OpenAI()  # uses OPENAI_API_KEY env var

messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What's our deployment strategy?"},
]

# Pre-turn: inject relevant memories into messages
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

If no relevant memories are found, messages are returned unchanged.

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
| `max_tokens` | 4000 | Soft token budget for injected context |
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
