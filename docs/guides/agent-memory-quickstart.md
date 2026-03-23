# Agent Memory Quickstart

Add programmable memory to your AI agent in 5 progressive levels. Each level is independently useful — adopt only what you need.

> **Prerequisites:** `pip install popoto` and Redis running on `localhost:6379`.
>
> **Full reference:** [Agent Memory Feature Overview](../features/agent-memory.md) covers all 14 primitives in depth.

## Level 1: Recall — Time-Weighted Retrieval

The simplest useful memory. Records decay over time so recent, important memories surface first.

```python
from popoto import Model, AutoKeyField, KeyField, StringField, FloatField
from popoto import DecayingSortedField

class Memory(Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )

# Save
Memory(agent_id="agent-1", content="Deploy uses blue-green strategy", importance=2.0).save()
Memory(agent_id="agent-1", content="Lunch order: salad", importance=0.5).save()

# Retrieve top memories ranked by recency * importance
results = Memory.query.filter(agent_id="agent-1").top_by_decay(n=5)
for m in results:
    print(m.content)
```

**What you get:** Records that matter surface first. Old, low-importance records fade away naturally.

## Level 2: Attention — Filter Noise, Track Reads

Add `WriteFilterMixin` to discard low-value records before they hit Redis. Add `AccessTrackerMixin` to know which memories the agent actually uses.

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

    _wf_min_threshold = 0.2       # below this: silently discarded
    _wf_priority_threshold = 0.7  # above this: tagged as priority

    def compute_filter_score(self):
        return self.importance or 0.0

# Low-value record is silently dropped (save returns False)
result = Memory(agent_id="agent-1", content="noise", importance=0.1).save()
assert result is False

# High-value record persists normally
Memory(agent_id="agent-1", content="critical finding", importance=0.9).save()

# After retrieving, confirm the agent used it
results = Memory.query.filter(agent_id="agent-1").top_by_decay(n=5)
results[0].confirm_access()  # marks as actually used
```

**What you get:** Cleaner memory — noise never persists. Read tracking shows which memories drive agent behavior.

## Level 3: Learning — Outcomes Strengthen or Weaken Beliefs

Add `ConfidenceField` for Bayesian certainty tracking. Use `ObservationProtocol` to report how the agent used each memory — acted on, dismissed, or contradicted.

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

    _wf_min_threshold = 0.2
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
outcome_map = {m.db_key.redis_key: "acted"}  # or "dismissed", "contradicted", "deferred"
ObservationProtocol.on_context_used([m], outcome_map)
```

**What you get:** Memories that the agent acts on grow stronger. Contradicted memories fade. The system learns from outcomes.

## Level 4: Association — Multi-Factor Ranking

Add `CoOccurrenceField` for weighted associations between memories. Use `composite_score()` to rank by multiple factors at once.

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
    associations = CoOccurrenceField(symmetric=True, max_edges=50)

    _wf_min_threshold = 0.2
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

**What you get:** Memories form a graph. Retrieving one can surface related memories. Multiple ranking factors combine into a single query.

## Level 5: Cognition — LLM-Ready Context Assembly

Use `ContextAssembler` to orchestrate all primitives into a single `assemble()` call that returns formatted, token-budgeted context ready for your LLM prompt.

```python
from popoto import ContextAssembler

# Use any Memory model from Levels 1-4
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

# result.records   — selected model instances
# result.formatted — LLM-ready string (JSON by default)
# result.metadata  — scores, timing, token counts

# Inject into your LLM prompt
system_prompt = f"You are a helpful assistant.\n\nRelevant context:\n{result.formatted}"
```

**What you get:** One call assembles the right memories, respects token budgets, and formats output for your LLM. Pull-path (query-driven) and push-path (proactive surfacing) retrieval in a single pipeline.

## Level 6: Semantic Search — Find Memories by Meaning

Add `ContentField` and `EmbeddingField` to store large content on the filesystem and search it by semantic similarity. Redis stays lean (only references and dimension counts), while content and vectors live on disk.

```python
import popoto
from popoto import (
    Model, AutoKeyField, KeyField, FloatField,
    ContentField, EmbeddingField, DecayingSortedField, ConfidenceField,
)
from popoto.embeddings.voyage import VoyageProvider

# Configure once at startup — sets the default embedding provider
# and content storage path for all fields
popoto.configure(
    embedding_provider=VoyageProvider(api_key="your-key"),
    content_path="/data/agent-memory",
)

class Memory(Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = ContentField()                    # large text stored on filesystem
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    confidence = ConfidenceField(initial_confidence=0.5)
    embedding = EmbeddingField(source="content")  # auto-generates vector on save

# Save memories — embeddings are generated automatically
Memory.create(
    agent_id="agent-1",
    content="Q4 revenue exceeded projections by 12%, driven by enterprise deals.",
    importance=0.9,
)
Memory.create(
    agent_id="agent-1",
    content="Engineering headcount target is 50 by end of year.",
    importance=0.7,
)
Memory.create(
    agent_id="agent-1",
    content="The deploy pipeline uses blue-green strategy with automatic rollback.",
    importance=0.8,
)

# Similarity-only search — ranked by cosine similarity to the query
results = Memory.query.semantic_search("revenue performance", limit=5)
for m in results:
    print(m.content[:80])

# Combined search — blends similarity with decay and confidence signals
results = Memory.query.semantic_search(
    "revenue performance",
    indexes={"relevance": 0.4, "confidence": 0.3},
    limit=5,
)
```

**What you get:** Memories are searchable by meaning, not just keywords. Combined with decay and confidence, the most relevant, recent, and trusted memories surface first.

> **Install extras:** `pip install popoto[voyage]` for Voyage AI embeddings, or `pip install popoto[openai]` for OpenAI. See [Content and Embedding Fields](../features/content-and-embedding-fields.md) for all provider options.

## Import Cheat Sheet

All imports come from the top-level `popoto` package:

```python
# Models and fields
from popoto import Model, AutoKeyField, KeyField, StringField, FloatField
from popoto import DecayingSortedField, ConfidenceField, CoOccurrenceField
from popoto import ContentField, EmbeddingField

# Mixins (listed before Model in class definition)
from popoto import WriteFilterMixin, AccessTrackerMixin

# Observation and context
from popoto import ObservationProtocol, ContextAssembler

# Constants for tuning
from popoto import InteractionWeight, TemporalPeriod, Defaults
```

**Never** use `from popoto.fields import ...` — the `popoto.fields` subpackage does not re-export field types. Always import from `popoto` directly.

## What's Next

- **[Agent Memory Feature Overview](../features/agent-memory.md)** — all 14 primitives with full API documentation
- **[Content and Embedding Fields](../features/content-and-embedding-fields.md)** — deep dive into ContentField, EmbeddingField, and semantic_search
- **[RAG Chatbot Recipe](rag-chatbot-recipe.md)** — build a retrieval-augmented chatbot with Popoto
- **[Tuning Magic Numbers](tuning-magic-numbers.md)** — adjust decay rates, confidence signals, and thresholds
- **[PolicyCache Recipe](policy-cache-recipe.md)** — RL-style learned action selection built on these primitives
