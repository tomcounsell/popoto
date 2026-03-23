# RAG Chatbot Recipe

Build a retrieval-augmented generation (RAG) chatbot that stores conversation
history and knowledge in Popoto, retrieves relevant context via semantic search,
and feeds it to an LLM for grounded responses.

> **Prerequisites:**
>
> - `pip install popoto[voyage]` (or `popoto[openai]` for OpenAI embeddings)
> - Redis running on `localhost:6379`
> - An API key for your embedding provider
> - An API key for your LLM (this recipe uses OpenAI, but any LLM works)

## Architecture

```
User question
    |
    v
[Embed query] --> [Semantic search Popoto] --> [Top-K knowledge chunks]
    |                                                |
    v                                                v
[Build prompt with retrieved context] --> [LLM generates answer]
    |
    v
[Store conversation turn in Popoto]
```

Popoto handles the storage and retrieval layer. The LLM call is your choice --
OpenAI, Anthropic, a local model, or any API-compatible provider.

## Step 1: Configure Popoto

Call `popoto.configure()` once at application startup. This sets the default
embedding provider for all `EmbeddingField` instances and `semantic_search()` calls.

```python
import popoto
from popoto.embeddings.voyage import VoyageProvider

popoto.configure(
    embedding_provider=VoyageProvider(api_key="your-voyage-key"),
    content_path="/data/chatbot-memory",
)
```

See [Configuration](../configuration.md#content-and-embedding-configuration) for
all options including OpenAI provider setup.

## Step 2: Define Knowledge and Conversation Models

```python
from popoto import Model, AutoKeyField, KeyField, FloatField
from popoto import ContentField, EmbeddingField, DecayingSortedField

class Knowledge(Model):
    """Long-lived knowledge base entries. Ingest documents, FAQs, or any
    reference material the chatbot should be able to cite."""
    chunk_id = AutoKeyField()
    source = KeyField()              # e.g. "faq", "docs", "manual"
    content = ContentField()         # full text stored on filesystem
    embedding = EmbeddingField(source="content")  # auto-generated on save

class ConversationTurn(Model):
    """Stores each user/assistant exchange. Decaying relevance ensures
    recent conversation context surfaces first."""
    turn_id = AutoKeyField()
    session_id = KeyField()          # groups turns by conversation
    role = KeyField()                # "user" or "assistant"
    content = ContentField()
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="session_id",
    )
    embedding = EmbeddingField(source="content")
```

## Step 3: Ingest Knowledge

Load your knowledge base into Popoto. Each chunk gets an embedding automatically
on save.

```python
def ingest_document(source: str, chunks: list[str]):
    """Ingest a list of text chunks from a document source."""
    for chunk_text in chunks:
        Knowledge.create(source=source, content=chunk_text)

# Example: ingest FAQ entries
faqs = [
    "Our return policy allows returns within 30 days of purchase with receipt.",
    "Shipping is free on orders over $50. Standard shipping takes 3-5 business days.",
    "Premium members get 20% off all orders and priority customer support.",
]
ingest_document(source="faq", chunks=faqs)
```

## Step 4: Retrieve Context and Generate Responses

```python
import openai

client = openai.OpenAI(api_key="your-openai-key")

def ask(session_id: str, question: str) -> str:
    """Answer a user question using RAG retrieval from Popoto."""

    # 1. Search knowledge base by meaning
    knowledge_results = Knowledge.query.semantic_search(
        question, limit=3,
    )

    # 2. Search recent conversation for continuity
    conversation_results = ConversationTurn.query.filter(
        session_id=session_id,
    ).semantic_search(
        question,
        indexes={"relevance": 0.5},
        limit=5,
    )

    # 3. Build context block
    context_parts = []
    if knowledge_results:
        context_parts.append("## Relevant Knowledge")
        for k in knowledge_results:
            context_parts.append(f"- {k.content}")
    if conversation_results:
        context_parts.append("\n## Recent Conversation")
        for turn in conversation_results:
            context_parts.append(f"[{turn.role}]: {turn.content}")

    context_block = "\n".join(context_parts)

    # 4. Call the LLM
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a helpful customer support assistant. "
                    "Answer based on the provided context. "
                    "If the context does not contain the answer, say so.\n\n"
                    f"{context_block}"
                ),
            },
            {"role": "user", "content": question},
        ],
    )
    answer = response.choices[0].message.content

    # 5. Store both turns for future retrieval
    ConversationTurn.create(
        session_id=session_id, role="user", content=question, importance=1.0,
    )
    ConversationTurn.create(
        session_id=session_id, role="assistant", content=answer, importance=0.8,
    )

    return answer
```

## Step 5: Use It

```python
session = "session-abc123"

print(ask(session, "What is your return policy?"))
# => "You can return items within 30 days of purchase with a receipt..."

print(ask(session, "Do I get free shipping?"))
# => "Yes, shipping is free on orders over $50..."

# Follow-up that uses conversation context
print(ask(session, "What about for premium members?"))
# => "Premium members get 20% off all orders and priority support..."
```

## Variations

### Use OpenAI Embeddings Instead of Voyage

```python
from popoto.embeddings.openai import OpenAIProvider

popoto.configure(
    embedding_provider=OpenAIProvider(api_key="your-openai-key"),
)
```

Install: `pip install popoto[openai]`

### Add Confidence Tracking

Track which knowledge entries the chatbot actually uses and strengthen them
over time using `ConfidenceField`:

```python
from popoto import ConfidenceField

class Knowledge(Model):
    chunk_id = AutoKeyField()
    source = KeyField()
    content = ContentField()
    embedding = EmbeddingField(source="content")
    confidence = ConfidenceField(initial_confidence=0.5)

# After the LLM uses a knowledge entry successfully:
ConfidenceField.update_confidence(entry, "confidence", signal=0.9)

# Blend confidence into retrieval ranking:
results = Knowledge.query.semantic_search(
    question,
    indexes={"confidence": 0.3},
    limit=5,
)
```

### Async Version

Popoto model operations have async counterparts for creation and retrieval:

```python
async def async_ask(session_id: str, question: str) -> str:
    # Semantic search is synchronous — run it in a thread if needed
    knowledge_results = Knowledge.query.semantic_search(
        question, limit=3,
    )
    # ... rest of the pipeline
    await ConversationTurn.async_create(
        session_id=session_id, role="user", content=question,
    )
```

## Key Points

- **ContentField** stores large text on the filesystem, keeping Redis lean
- **EmbeddingField** auto-generates vectors on save -- no manual embedding calls
- **semantic_search()** handles embedding the query, computing similarity, and hydrating results
- Combined with `indexes`, semantic search blends meaning-based retrieval with decay, confidence, and other memory signals
- Conversation history with `DecayingSortedField` ensures recent context surfaces first

## See Also

- [Agent Memory Quickstart](agent-memory-quickstart.md) -- progressive levels from basic recall to semantic search
- [Content and Embedding Fields](../features/content-and-embedding-fields.md) -- deep dive into storage, providers, and caching
- [Configuration](../configuration.md#content-and-embedding-configuration) -- global setup for embeddings and content storage
- [PolicyCache Recipe](policy-cache-recipe.md) -- RL-style action selection built on memory primitives
