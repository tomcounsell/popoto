# Agent Memory Quickstart

Add programmable memory to your AI agent. Level 0 is the whole loop in six
lines. Levels 1 through 6 build the same thing up one field at a time, so you
understand what each piece buys before you keep or drop it.

> **Prerequisites:** `pip install popoto` and Redis running on `localhost:6379`.
>
> **Full reference:** [Agent Memory](../features/agent-memory.md) maps all 15
> primitives and the layers composed on them.

## Level 0: Import the defaults

Skip the schema. `DefaultMemory` ships the benchmarked configuration (decay,
confidence, keyword search, and an association graph) and `SubconsciousMemory`
wraps it into a per-turn loop:

```python
from popoto.recipes import SubconsciousMemory

sm = SubconsciousMemory(agent_id="agent-1")

messages, assembly = sm.inject_context(messages)   # pre-turn: retrieve + inject
answer = call_your_llm(messages)                   # your LLM call
sm.extract_memories(answer, importance=0.6)        # post-turn: save what was learned
sm.report_outcomes(assembly, outcome="acted")      # feedback: reinforce what was used
```

That is a working memory loop with query-sensitive retrieval on by default.
`agent_id` is the only required argument; it partitions every index, and an
explicit `.filter(agent_id=...)` query (Levels 1+ below) always honors that
partition. `inject_context`'s default retrieval path (lexical/BM25) does not
yet filter by it ([#576](https://github.com/tomcounsell/popoto/issues/576)), so at Level 0 two agents sharing one Redis can
retrieve each other's memories — a distinct Redis database per agent is the
actual isolation boundary until that lands.

This is also the configuration the published benchmarks run. If you take
nothing else from this page, take this.

`DefaultMemory` also carries `NeverRecordMixin`, so credential- and
secret-shaped content is blocked before it reaches Redis with no extra setup
— see [NeverRecordFirewall](../features/never-record-firewall.md). If you
hand-build the schema at Levels 1-4 below, that protection is not there for
free; add `NeverRecordMixin` to your own model class if you want it.

The levels below exist for when you want to shape the schema yourself. See the
[SubconsciousMemory recipe](subconscious-memory-recipe.md) for the default
model's exact fields and every knob on the loop.

## Level 1: Recall, time-weighted and query-sensitive

The smallest useful memory. Records decay over time so recent, important ones
surface first, and a `BM25Field` makes retrieval respond to the query text
rather than only to the clock.

```python
from popoto import Model, AutoKeyField, KeyField, StringField, FloatField
from popoto import DecayingSortedField, BM25Field

class Memory(Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    content_bm25 = BM25Field(source="content")  # keyword search index

# Save
Memory(agent_id="agent-1", content="Deploy uses blue-green strategy", importance=2.0).save()
Memory(agent_id="agent-1", content="Lunch order: salad", importance=0.5).save()

# Retrieve top memories ranked by recency * importance
results = Memory.query.filter(agent_id="agent-1").top_by_decay(n=5)
for m in results:
    print(m.content)
```

**What you get:** records that matter surface first, old low-importance ones
fade, and a later `ContextAssembler` over this model reads the query text.

**Why `BM25Field` is here at Level 1.** Without it, `ContextAssembler`'s
`retrieval_mode="auto"` resolves to the query-blind `composite` path: query
cues are accepted and then ignored. That is a legitimate mode for some
workloads and the wrong default for most. See
[Query-Blind Retrieval](query-blind-retrieval.md) for which side you are on.

**Adding `BM25Field` to an existing model:** it indexes via the `on_save()`
hook, so new records are indexed automatically and existing ones need one
re-save:

```python
for memory in Memory.query.filter():
    memory.save()
```

This is idempotent, so it is safe to run more than once.

## Level 2: Attention, filtering noise and tracking reads

Add `WriteFilterMixin` to discard low-value records before they hit Redis, and
`AccessTrackerMixin` to know which memories the agent actually uses.

```python
from popoto import WriteFilterMixin, AccessTrackerMixin

class Memory(WriteFilterMixin, AccessTrackerMixin, Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    content_bm25 = BM25Field(source="content")

    _wf_min_threshold = 0.1       # below this: silently discarded
    _wf_priority_threshold = 0.7  # above this: tagged as priority

    def compute_filter_score(self):
        return self.importance or 0.0

# Low-value record is silently dropped (save returns False)
result = Memory(agent_id="agent-1", content="noise", importance=0.05).save()
assert result is False

# High-value record persists normally
Memory(agent_id="agent-1", content="critical finding", importance=0.9).save()

# After retrieving, confirm the agent used it
results = Memory.query.filter(agent_id="agent-1").top_by_decay(n=5)
results[0].confirm_access()  # marks as actually used
```

**What you get:** cleaner memory, since noise never persists. Read tracking
shows which memories drive agent behavior.

## Level 3: Learning, where outcomes strengthen or weaken beliefs

Add `ConfidenceField` for certainty tracking. Use `ObservationProtocol` to
report how the agent used each memory: acted on, dismissed, or contradicted.

```python
from popoto import ConfidenceField, ObservationProtocol

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
    content_bm25 = BM25Field(source="content")

    _wf_min_threshold = 0.1
    _wf_priority_threshold = 0.7

    def compute_filter_score(self):
        return self.importance or 0.0

m = Memory(agent_id="agent-1", content="API key rotates monthly", importance=0.8)
m.save()

# Corroborate: evidence confirms the belief
ConfidenceField.update_confidence(m, "confidence", signal=0.9)

# Or contradict: evidence weakens the belief
ConfidenceField.update_confidence(m, "confidence", signal=0.1)

# Report agent outcomes in bulk
outcome_map = {m.db_key.redis_key: "acted"}  # or "dismissed", "contradicted", "deferred", "used"
ObservationProtocol.on_context_used([m], outcome_map)
```

**What you get:** memories the agent acts on grow stronger. Contradicted
memories fade. The system learns from outcomes.

## Level 4: Association and multi-factor ranking

Add `CoOccurrenceField` for weighted associations between memories. Use
`composite_score()` to rank by several factors at once.

```python
from popoto import CoOccurrenceField

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
    content_bm25 = BM25Field(source="content")
    associations = CoOccurrenceField(symmetric=True, max_edges=50)

    _wf_min_threshold = 0.1
    _wf_priority_threshold = 0.7

    def compute_filter_score(self):
        return self.importance or 0.0

m1 = Memory(agent_id="agent-1", content="deploy process", importance=0.8)
m1.save()
m2 = Memory(agent_id="agent-1", content="rollback steps", importance=0.8)
m2.save()

# Link related memories
assoc_field = Memory._meta.fields["associations"]
assoc_field.link(Memory, m1.db_key.redis_key, m2.db_key.redis_key, initial_weight=0.5)

# Multi-factor retrieval: combine relevance with other indexes
results = Memory.query.filter(agent_id="agent-1").composite_score(
    indexes={"relevance": 1.0},
    limit=10,
)
```

**What you get:** memories form a graph. Retrieving one can surface related
ones, and multiple ranking factors combine into a single query.

This model now carries the same fields as the shipped `DefaultMemory` from
Level 0.

## Level 5: Cognition, assembling LLM-ready context

Use `ContextAssembler` to orchestrate the primitives into a single `assemble()`
call returning formatted, token-budgeted context.

```python
from popoto import ContextAssembler

# Use any Memory model from Levels 1-4
assembler = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 1.0},  # benchmarked default; see the note below
    max_items=10,
    max_tokens=4000,
)

result = assembler.assemble(
    query_cues={"topic": "deployment"},
    agent_id="agent-1",
)

# result.records   — selected model instances
# result.formatted : LLM-ready string
# result.metadata  — scores, timing, token counts

# Inject into your LLM prompt
system_prompt = f"You are a helpful assistant.\n\nRelevant context:\n{result.formatted}"
```

### Complete LLM integration example

Wire assembled context into an OpenAI SDK v1+ call and report outcomes:

```python
from openai import OpenAI
from popoto import ContextAssembler, ObservationProtocol

client = OpenAI()  # uses OPENAI_API_KEY env var

# Assemble memory context
result = assembler.assemble(query_cues={"topic": "deployment"}, agent_id="agent-1")

# Build messages with injected memory
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

**What you get:** one call assembles the right memories, respects token
budgets, and formats output for your LLM. Query-driven and proactive retrieval
in a single pipeline.

**On `score_weights={"relevance": 1.0}`:** this is the `best_value` from the
Tier 4 sweep (`tests/benchmarks/results/sweep_20260326_125145.json`, coding
assistant / research agent / support agent scenarios, 18/18 points OK). How
strong that evidence is: all six swept weight vectors tied at nDCG@5 = 1.0 on
those scenarios, so this is the selected best value and the simplest
single-index vector, not a configuration measured to beat the others. In
lexical and hybrid modes the pull path ignores `score_weights` entirely.

Wrapping this in a per-turn loop is what
[`SubconsciousMemory`](subconscious-memory-recipe.md) does, which brings you
back to Level 0.

## Level 6: Semantic search, finding memories by meaning

Add `ContentField` and `EmbeddingField` to store large content on the
filesystem and search it by semantic similarity. Redis stays lean; content and
vectors live on disk.

Keep `BM25Field` when you add `EmbeddingField`. The two together resolve to
**hybrid** retrieval, BM25 and vector fused by weighted Reciprocal Rank
Fusion. `EmbeddingField` alone, without `BM25Field`, resolves to the
query-blind composite path and is a regression, not an upgrade.

```python
import popoto
from popoto import (
    Model, AutoKeyField, KeyField, FloatField,
    ContentField, EmbeddingField, DecayingSortedField, ConfidenceField, BM25Field,
)
from popoto.embeddings.voyage import VoyageProvider

# Configure once at startup — sets the default embedding provider
# and content storage path for all fields
popoto.configure(
    embedding_provider=VoyageProvider(api_key="your-key"),
    content_path="/data/agent-memory",
)

# ----
# Prefer no API keys? Run Ollama locally and swap providers.
# Prerequisite: `ollama pull nomic-embed-text` and `ollama serve`.
#
# from popoto.embeddings.ollama import OllamaProvider
# popoto.configure(
#     embedding_provider=OllamaProvider(model="nomic-embed-text"),
#     content_path="/data/agent-memory",
# )
# ----

class SemanticMemory(Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = ContentField()                      # large text stored on filesystem
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    confidence = ConfidenceField(initial_confidence=0.5)
    content_bm25 = BM25Field(source="content")    # keep this: hybrid needs both arms
    embedding = EmbeddingField(source="content")  # auto-generates vector on save

# Save memories — embeddings are generated automatically
SemanticMemory.create(
    agent_id="agent-1",
    content="Q4 revenue exceeded projections by 12%, driven by enterprise deals.",
    importance=0.9,
)
SemanticMemory.create(
    agent_id="agent-1",
    content="Engineering headcount target is 50 by end of year.",
    importance=0.7,
)

# Similarity-only search — ranked by cosine similarity to the query
results = SemanticMemory.query.semantic_search("revenue performance", limit=5)
for m in results:
    print(m.content[:80])

# Combined search — blends similarity with decay and confidence signals
results = SemanticMemory.query.semantic_search(
    "revenue performance",
    indexes={"relevance": 0.4, "confidence": 0.3},
    limit=5,
)
```

**What you get:** memories searchable by meaning through `semantic_search()`,
and hybrid retrieval through `ContextAssembler`. On LongMemEval-S, hybrid beats
the lexical baseline on every metric; on LoCoMo's name-anchored questions the
fusion weighting routes the vector arm out of the way so hybrid does not fall
below lexical. Both runs are in [Benchmarks](../benchmarks.md).

> **Install extras:** `pip install popoto[voyage]` for Voyage AI embeddings, or
> `pip install popoto[openai]` for OpenAI. For a no-API-key setup, run
> [Ollama](https://ollama.com) locally and use `OllamaProvider` (stdlib only,
> no extras needed). See
> [Content and Embedding Fields](../features/content-and-embedding-fields.md)
> for all provider options.

## Writing memories: store the turn, do not rewrite it

`extract_memories()` decides what a turn leaves behind. The measured-best write
path is the default: save the raw turn.

LLM fact extraction was measured against raw turn ingestion on the judged-answer
harness across three models plus a heuristic sentence splitter, and every
extraction arm lost. The heuristic splitter scores 0.21 against raw ingestion's
0.36 on the same 77 scored items; the Claude arms cost more, in proportion to
how many turns they discard. Both failure modes are documented in
[LLM Memory Extraction](../features/llm-memory-extraction.md#evaluation-extraction-lost-to-raw-ingestion).

Treat extraction as an opt-in to test against your own corpus, not a default to
turn on:

```python
from popoto.extraction.claude import ClaudeExtractionProvider

sm = SubconsciousMemory(
    agent_id="agent-1",
    extraction_provider=ClaudeExtractionProvider(),  # requires popoto[anthropic]
)
```

## Import cheat sheet

All imports come from the top-level `popoto` package:

```python
# Models and fields
from popoto import Model, AutoKeyField, KeyField, StringField, FloatField
from popoto import DecayingSortedField, ConfidenceField, CoOccurrenceField
from popoto import BM25Field
from popoto import ContentField, EmbeddingField

# Mixins (listed before Model in class definition)
from popoto import WriteFilterMixin, AccessTrackerMixin

# Observation and context
from popoto import ObservationProtocol, ContextAssembler

# Constants for tuning
from popoto import InteractionWeight, TemporalPeriod, Defaults
```

Recipes — including the batteries-included model — live one level down:

```python
from popoto.recipes import DefaultMemory, SubconsciousMemory
```

**Never** use `from popoto.fields import ...`. The `popoto.fields` subpackage
does not re-export field types. Always import from `popoto` directly.

## What's next

- **[SubconsciousMemory Recipe](subconscious-memory-recipe.md):** the per-turn loop from Level 0, with every knob
- **[Query-Blind Retrieval](query-blind-retrieval.md):** when composite ranking is right, and when it costs you the answer
- **[Agent Memory](../features/agent-memory.md):** all 17 primitives and the layers composed on them
- **[Benchmarks](../benchmarks.md):** the numbers behind the defaults, including the runs that came out badly
- **[Tuning Magic Numbers](tuning-magic-numbers.md):** decay rates, confidence signals, and thresholds
- **[PolicyCache Recipe](policy-cache-recipe.md):** RL-style learned action selection on these primitives
- **[Trajectory Memory Recipe](trajectory-memory-recipe.md):** fingerprint-keyed procedural patterns
- **[RAG Chatbot Recipe](rag-chatbot-recipe.md):** retrieval-augmented chatbot with Popoto
