# Subconscious Memory Recipe

> **New to Agent Memory?** Start with the [Quickstart Guide](agent-memory-quickstart.md) for a progressive adoption path.

Automatic memory injection and extraction around every LLM turn. The agent's memory works silently -- assembling context before each call and saving new observations after each response -- without the application needing to manage memory explicitly.

!!! note "Retrieval mode determines query-sensitivity"
    Whether retrieved memories are selected by query text depends on which fields are on your model. With `BM25Field` only, retrieval is **query-sensitive** (lexical mode). With both `BM25Field` and `EmbeddingField`, retrieval is **query-sensitive** (hybrid mode). With neither field, retrieval is **query-blind** (composite mode) — memories are ranked by importance/confidence scores, not by relevance to the user's query. See [ContextAssembler retrieval modes](../features/context-assembler.md#pull-path-modes-retrieval_mode) for details.

    `SubconsciousMemory` uses `retrieval_mode='auto'` — the effective mode is determined by which fields are on the model at init time. Adding or removing `BM25Field`/`EmbeddingField` changes the effective mode without any change at the `SubconsciousMemory` call site. Adding `EmbeddingField` to a BM25-only model silently flips `lexical → hybrid`.

    The default model declares a `BM25Field`, so the zero-argument path below is query-sensitive. Bring a model without one and `ContextAssembler` logs a `WARNING` naming the missing field.

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
from popoto.recipes import SubconsciousMemory

sm = SubconsciousMemory(agent_id="agent-1")

messages, assembly = sm.inject_context(messages)   # pre-turn
answer = call_your_llm(messages)                   # your LLM call
sm.extract_memories(answer, importance=0.6)        # post-turn
sm.report_outcomes(assembly, outcome="acted")      # feedback
```

That is the whole loop. `agent_id` is the only required argument — it partitions every index, so two agents sharing one Redis never see each other's memories.

### What the defaults give you

Leaving `model_class` unset selects `popoto.recipes.DefaultMemory`, the shipped model:

```python
from popoto.recipes import DefaultMemory

class DefaultMemory(AccessTrackerMixin, Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(base_score_field="importance", partition_by="agent_id")
    confidence = ConfidenceField()
    content_bm25 = BM25Field(source="content")
    associations = CoOccurrenceField()
```

The `BM25Field` is the load-bearing piece: it makes `retrieval_mode='auto'` resolve to the query-sensitive `lexical` mode. A model without one resolves to `composite`, which ignores the query text entirely (and now logs a warning saying so).

`DefaultMemory` deliberately omits `WriteFilterMixin` — it discards records below a score threshold and `save()` returns `False`, which is the wrong surprise for a first run. Add it once you want that behavior; the [quickstart](agent-memory-quickstart.md#level-2-attention-filter-noise-track-reads) covers it at Level 2. `EmbeddingField` is omitted too, since it needs an embedding provider; adding one to a subclass flips retrieval from `lexical` to `hybrid` with no change at this call site.

Two more defaults follow from the default model: `score_weights` becomes `{"relevance": 1.0}` (the benchmarked vector), and `confidence_field` / `co_occurrence_field` are wired to the model's `confidence` and `associations` fields.

### Bringing your own model

Every argument is still there. Passing `model_class` explicitly keeps the pre-existing defaults for `confidence_field` and `co_occurrence_field` (both `None`), so upgrading changes nothing for existing code:

```python
sm = SubconsciousMemory(
    model_class=Memory,          # any level from the quickstart guide
    agent_id="agent-1",
    score_weights={"relevance": 0.6, "confidence": 0.3},
    max_items=10,
    max_tokens=4000,
)
```

Applications that want their own Redis keyspace can subclass instead of authoring a schema:

```python
class ProjectMemory(DefaultMemory):
    pass  # keys become ProjectMemory:* instead of DefaultMemory:*
```

### Injected context format

The injected block carries the memory text and nothing else:

```
Relevant context:
- The deploy pipeline uses a blue-green strategy with automatic rollback.
- Q4 revenue exceeded projections by 12%, driven by enterprise deals.
```

The content-only shape is what makes the token budget go to memory rather than to bookkeeping. Measured over `DefaultMemory` with a 71-character memory, the content format runs 73 characters (1.03x the content, ~16 estimated tokens); the full JSON record — `memory_id` UUIDs, the `agent_id` the caller just supplied, `relevance` as a raw epoch float — runs 262 characters (3.69x, ~104 tokens) for the same one memory.

Pass `output_format="structured"` to restore the JSON payload verbatim, or `"xml"` / `"natural"` for the other [ContextAssembler formats](../features/context-assembler.md#output-formats-output_format).

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

By default (no `extraction_provider` passed to the constructor):

1. Splits the LLM response into sentences
2. Filters out sentences shorter than `extraction_min_length` (default 10 chars)
3. Saves each sentence as a new Memory record with the specified importance

!!! warning "The measured-best write path is the raw turn"
    Every rewrite-before-store path that has been measured lost to storing the
    turn as it arrived. On the judged-answer harness, raw turn ingestion scores
    **0.3636** over 77 items; the `HeuristicExtractionProvider` default scores
    **0.2078**, and the Claude extraction arms score lower still, in proportion
    to how many turns they discard. Full table, both failure mechanisms, and the
    scope of the measurement:
    [LLM Memory Extraction](../features/llm-memory-extraction.md#evaluation-extraction-lost-to-raw-ingestion).

    Passing a longer `response_text` straight through — one record per turn,
    no splitting — is the configuration those benchmarks ran. Reach for an
    extraction provider when your own corpus gives you a reason to, and measure
    it there.

Extraction is pluggable via a dedicated provider interface -- see the [LLM Memory Extraction](../features/llm-memory-extraction.md) feature doc for the full picture. In short:

- **`extraction_provider`** (default `None` -> `HeuristicExtractionProvider`): pass an `AbstractExtractionProvider` instance (e.g. `ClaudeExtractionProvider` from `popoto.extraction.claude`, requires `pip install popoto[anthropic]`) for LLM-based extraction that also returns entities, an importance opinion, and a confidence opinion per fact.
- **`confidence_field`** (default `None`, no-op unless set): name of a `ConfidenceField` on `model_class`. When set and a fact carries a confidence opinion, `extract_memories()` seeds that field via `ConfidenceField.update_confidence()`. Because `ConfidenceField` has no per-instance "set initial value" API, this is a *blend* with the field's `initial_confidence` prior, not a hard override -- see [the confidence blend nuance](../features/llm-memory-extraction.md#the-confidence-blend-nuance) before assuming the stored value equals the extracted confidence.
- **`co_occurrence_field`** (default `None`, no-op unless set): name of a `CoOccurrenceField` on `model_class`. When set and a fact names two or more distinct entities, every unordered entity pair is linked in that field's association graph.

Both `confidence_field` and `co_occurrence_field` are inert with the default `HeuristicExtractionProvider`, since it never populates `entities` or `confidence` on the facts it emits -- they only do work once an entity/confidence-emitting provider (like `ClaudeExtractionProvider`) is configured.

For a fully custom extraction source (not implementing the provider interface), you can still subclass `SubconsciousMemory` and override `extract_memories()` directly -- see [Extensibility](#extensibility) below.

### Outcome: `report_outcomes(assembly_result, outcome)`

Reports how the agent used the injected memories via `ObservationProtocol.on_context_used()`. Outcomes strengthen or weaken memories for future retrieval:

- `"acted"` -- the agent used this memory (strengthens confidence)
- `"dismissed"` -- the agent ignored this memory (mild weakening)
- `"contradicted"` -- the agent found this memory incorrect (strong weakening)
- `"deferred"` -- the agent noted but deferred action (neutral)
- `"used"` -- the memory informed reasoning without appearing in the response (confirms access, no strength signal)

### Outcomes prune the corpus

Reported outcomes are not only a ranking nudge -- they set how fast a memory leaves the corpus, so the layer stores, retrieves, validates, *and* prunes during regular use with no extra call site:

1. `report_outcomes(..., "dismissed")` lowers the record's `ConfidenceField` value.
2. The next retrieval reads that confidence inside the decay Lua and raises the record's effective decay rate (`eff = decay_rate * 2 ^ (s * 2 * (c0 - c))`), so it ranks lower -- see [Confidence-Modulated Decay](../features/decaying-sorted-field.md#confidence-modulated-decay).
3. The next [`MemoryLifecycle.tick()`](../recipes.md#memorylifecycle) tombstones it once it is idle, its confidence is below `FORGET_CONFIDENCE_CEILING`, and it has at least `FORGET_MIN_EVIDENCE` observations behind it.

Modulation is on by default whenever the model carries exactly one `ConfidenceField`, and forgetting tombstones rather than deletes, so a memory pruned by an unlucky run of dismissals can be brought back with `lifecycle.restore(redis_key)`. Set `Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED = False` to take confidence out of the ranking entirely, without touching model code.

`SubconsciousMemory` itself does not run lifecycle ticks -- compose it with a `MemoryLifecycle` instance as shown in [Composing with SubconsciousMemory](../recipes.md#composing-with-subconsciousmemory).

## Tuning

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_items` | 10 | Maximum memories injected per turn |
| `max_tokens` | 4000 | Token budget for injected context (enforced; see [Token Budget Semantics](../features/context-assembler.md#token-budget-semantics)) |
| `extraction_min_length` | 10 | Minimum chars for a sentence to become a memory |
| `model_class` | `None` (-> `DefaultMemory`) | Memory model. Leave unset for the batteries-included model |
| `score_weights` | `{"relevance": 1.0}` | Weight dict for composite scoring. The benchmarked vector; ignored by the pull path in lexical/hybrid modes |
| `output_format` | `"content"` | Injected payload shape. `"structured"` injects the full JSON record instead |
| `system_preamble` | "You are a helpful assistant." | Prefix for auto-created system messages |
| `content_field` | "content" | Name of the text content field on your model |
| `importance_field` | "importance" | Name of the importance score field |
| `extraction_provider` | `None` (-> `HeuristicExtractionProvider`) | `AbstractExtractionProvider` used by `extract_memories()`. See [LLM Memory Extraction](../features/llm-memory-extraction.md) |
| `confidence_field` | `None`, or `"confidence"` with the default model | Name of a `ConfidenceField` to seed from extracted facts' confidence opinions; no-op unless set |
| `co_occurrence_field` | `None`, or `"associations"` with the default model | Name of a `CoOccurrenceField` to link co-mentioned entities in; no-op unless set |

These constants can be tuned experimentally using the Tier 4 benchmark harness. See the [Tuning Magic Numbers](tuning-magic-numbers.md) guide for the full constant catalog, optimal ranges, and how to run parameter sweeps.

## Extensibility

### Custom Fact Extraction

The preferred way to customize extraction is to pass an `extraction_provider` -- either the built-in `ClaudeExtractionProvider` or your own `AbstractExtractionProvider` implementation -- rather than subclassing. See [LLM Memory Extraction](../features/llm-memory-extraction.md) for the full interface and a "writing a custom provider" example.

```python
from popoto.extraction import AbstractExtractionProvider, ExtractedFact

class MyProvider(AbstractExtractionProvider):
    def extract(self, text: str) -> list[ExtractedFact]:
        facts = my_extraction_function(text)
        return [
            ExtractedFact(text=f["text"], importance=f.get("importance"))
            for f in facts
        ]

sm = SubconsciousMemory(
    agent_id="agent-1",
    extraction_provider=MyProvider(),
)
```

If you need to change more than extraction itself (e.g. custom save logic, side effects beyond seeding confidence/co-occurrence), subclassing and overriding `extract_memories()` directly is still supported:

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
- [Query-Blind Retrieval](query-blind-retrieval.md) -- when composite ranking is right, and when it costs you the answer
- [LLM Memory Extraction](../features/llm-memory-extraction.md) -- pluggable extraction providers, entities, importance/confidence opinions
- [ContextAssembler](../features/context-assembler.md) -- retrieval-to-injection bridge
- [PolicyCache Recipe](policy-cache-recipe.md) -- RL-style learned action selection
- [Trajectory Memory Recipe](trajectory-memory-recipe.md) -- fingerprint-keyed procedural memory: cluster completed task trajectories and recall "what worked last time"
