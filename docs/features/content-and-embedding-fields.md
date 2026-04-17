# Content and Embedding Fields

ContentField and EmbeddingField extend Popoto's storage model beyond Redis hashes,
routing large content and vector embeddings to the filesystem while keeping Redis
lean and fast.

## Overview

| Field | Stores in Redis | Stores on Filesystem | Purpose |
|-------|----------------|---------------------|---------|
| ContentField | `$CF:{hash}:{path}` reference | Raw content bytes | Large text, documents, binary data |
| EmbeddingField | Dimension count (integer) | `.npy` vector file | Vector embeddings for similarity search |

Both fields are transparent to the caller -- you read and write normal Python values,
and the storage indirection happens automatically on save/load.

## ContentField

### How It Works

1. **On save**: Content is written to the filesystem first (atomic temp-file + rename),
   then a compact reference string (`$CF:{sha256}:{path}`) is stored in Redis.
2. **On access**: The descriptor detects the `$CF:` prefix and lazily loads content
   from the filesystem, caching it on the instance.
3. **On delete**: No-op. Content files are append-only (content-addressable storage
   means identical content shares files). Use `garbage_collect()` to clean up.

### Storage Layout

```
~/.popoto/content/
  Document/
    <sha256_hash>       # Content file (raw bytes)
    <sha256_hash>.v1    # Previous version (auto-archived on overwrite)
```

The base directory defaults to `~/.popoto/content/` and can be changed via:
- `POPOTO_CONTENT_PATH` environment variable
- `popoto.configure(content_path="/data/content")`
- Per-field: `ContentField(store=FilesystemStore(base_path="/data/content"))`

### Custom Content Stores

Implement `AbstractContentStore` to use a different backend (S3, GCS, etc.):

```python
from popoto.stores import AbstractContentStore

class S3Store(AbstractContentStore):
    def save(self, content: bytes, key: str, model_class_name: str) -> str:
        # Upload to S3, return a $CF: reference string
        ...

    def load(self, reference: str) -> bytes:
        # Download from S3 using the reference
        ...

    def delete(self, reference: str) -> None:
        # Remove from S3
        ...
```

Register it globally or per-field:

```python
popoto.configure(content_store=S3Store(bucket="my-bucket"))
# or
body = ContentField(store=S3Store(bucket="my-bucket"))
```

## EmbeddingField

### How It Works

1. **On save**: Reads the source field value, calls the embedding provider to generate
   a vector, saves it as a `.npy` file, and stores the dimension count in Redis.
2. **On query**: `load_embeddings()` reads all `.npy` files for a model class into a
   pre-normalized numpy matrix. This matrix is cached in memory for fast cosine similarity.
3. **On delete**: The `.npy` file is removed and the cache is invalidated.

### Storage Layout

```
~/.popoto/content/.embeddings/
  Memory/
    <hex_encoded_redis_key>.npy    # numpy float32 vector
```

### Embedding Providers

Providers are pluggable. Configure a default via `popoto.configure()` or pass one
directly to `EmbeddingField(provider=...)`.

#### VoyageProvider

Voyage AI embeddings, optimized for retrieval tasks with asymmetric query/document
embedding.

```python
from popoto.embeddings.voyage import VoyageProvider

provider = VoyageProvider(
    api_key="your-key",           # or VOYAGE_API_KEY env var
    model="voyage-3-lite",        # default
    dimensions=512,               # default
    max_batch_size=128,           # default
)
```

Install: `pip install popoto[voyage]`

#### OpenAIProvider

OpenAI text embeddings.

```python
from popoto.embeddings.openai import OpenAIProvider

provider = OpenAIProvider(
    api_key="your-key",                  # or OPENAI_API_KEY env var
    model="text-embedding-3-small",      # default
    dimensions=1536,                      # default
    max_batch_size=2048,                  # default
)
```

Install: `pip install popoto[openai]`

#### OllamaProvider

Local embeddings via a running [Ollama](https://ollama.com) server. No API
key, no per-token cost, no network dependency on a paid provider.

```python
from popoto.embeddings.ollama import OllamaProvider

provider = OllamaProvider(
    base_url="http://localhost:11434",   # default
    model="nomic-embed-text",            # default (768-dim)
    dim=None,                            # auto-detect from first response
)
```

**Setup:**

```bash
# Install Ollama from https://ollama.com
ollama pull nomic-embed-text    # or mxbai-embed-large (1024-dim), all-minilm (384-dim)
ollama serve                    # run the local server
```

**Behaviour:**

- Uses the batch-capable `/api/embed` endpoint (Ollama v0.2.0+).
- Vector dimensions are auto-detected from the first `embed()` response
  and cached. Pass `dim=<n>` to the constructor to declare them up front.
- `max_batch_size` defaults to 32 (conservative for local inference on
  modest hardware). Subclass to raise it.
- No external dependencies -- uses stdlib `urllib.request`.
- Error messages include actionable hints: connection refused points at
  `ollama serve`; missing models point at `ollama pull <model>`.

Install: `pip install popoto` (stdlib-only; no extras needed).

#### Custom Providers

Implement `AbstractEmbeddingProvider`:

```python
from popoto.embeddings import AbstractEmbeddingProvider

class MyProvider(AbstractEmbeddingProvider):
    def embed(self, texts, input_type=None):
        # Return list of float vectors, one per text
        ...

    @property
    def dimensions(self):
        return 768

    @property
    def max_batch_size(self):
        return 100
```

## semantic_search()

The query method `semantic_search()` ties ContentField and EmbeddingField together
into a retrieval pipeline.

### Similarity-Only Search

```python
results = Memory.query.semantic_search("revenue trends", limit=5)
```

Under the hood:
1. Query text is embedded via the provider (with `input_type="query"`)
2. Cosine similarity is computed against the cached embedding matrix
3. Top-K results are hydrated from Redis and returned

### Combined with Memory Signals

When `indexes` is provided, similarity scores are injected into `composite_score()`
as an additional weighted signal:

```python
results = Memory.query.semantic_search(
    "revenue trends",
    indexes={"relevance": 0.4, "confidence": 0.3},
    limit=10,
)
```

This produces a unified ranking that blends semantic relevance with recency (decay),
confidence, and other sorted field signals.

### Full Example

```python
import popoto
from popoto.fields.content_field import ContentField
from popoto.fields.embedding_field import EmbeddingField
from popoto.fields.decaying_sorted_field import DecayingSortedField
from popoto.fields.confidence_field import ConfidenceField
from popoto.embeddings.voyage import VoyageProvider

# Configure once at startup
popoto.configure(
    embedding_provider=VoyageProvider(api_key="your-key"),
    content_path="/data/agent-memory",
)

class Memory(popoto.Model):
    topic = popoto.KeyField()
    content = ContentField()
    relevance = DecayingSortedField()
    certainty = ConfidenceField()
    embedding = EmbeddingField(source="content")

# Create memories
Memory.create(topic="q4-revenue", content="Q4 revenue exceeded projections by 12%...")
Memory.create(topic="q3-revenue", content="Q3 revenue was flat compared to Q2...")
Memory.create(topic="hiring-plan", content="Engineering headcount target is 50 by EOY...")

# Semantic search with memory signals
results = Memory.query.semantic_search(
    "revenue performance",
    indexes={"relevance": 0.5, "certainty": 0.3},
    limit=5,
)

for memory in results:
    print(f"{memory.topic}: {memory.content[:80]}...")
```

## Cache Management

EmbeddingField maintains a process-level cache of pre-normalized numpy matrices.
The cache is automatically invalidated when embeddings are saved or deleted within
the same process.

For multi-process deployments, call `invalidate_cache()` to force a reload from disk:

```python
from popoto.fields.embedding_field import invalidate_cache

# Invalidate cache for a specific model
invalidate_cache("Memory")

# Invalidate all cached embeddings
invalidate_cache()
```

## Installation

| Extra | Command | Includes |
|-------|---------|----------|
| Base | `pip install popoto` | ContentField (no extra deps) |
| Embeddings | `pip install popoto[embeddings]` | numpy |
| Ollama | `pip install popoto[embeddings]` | numpy (no extra Python deps) |
| Voyage AI | `pip install popoto[voyage]` | numpy, voyageai |
| OpenAI | `pip install popoto[openai]` | numpy, openai |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `POPOTO_CONTENT_PATH` | `~/.popoto/content` | Base directory for content files and embeddings |
| `VOYAGE_API_KEY` | *(none)* | API key for VoyageProvider (alternative to passing `api_key=`) |
| `OPENAI_API_KEY` | *(none)* | API key for OpenAIProvider (alternative to passing `api_key=`) |
| *(none)* | — | OllamaProvider requires no API key |

## See Also

- [Fields > ContentField](../fields.md#contentfield) -- field reference
- [Fields > EmbeddingField](../fields.md#embeddingfield) -- field reference
- [Making Queries > Semantic Search](../query.md#semantic-search) -- query interface
- [Configuration](../configuration.md#content-and-embedding-configuration) -- global setup
- [API Reference](../api-reference.md#contentfield) -- method signatures
