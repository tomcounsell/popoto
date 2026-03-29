# Hybrid Retrieval: BM25 + RRF Fusion

Hybrid retrieval combines multiple search signals -- keyword relevance (BM25), semantic similarity (embeddings), and optionally graph associations (CoOccurrence) -- into a single ranked result set using Reciprocal Rank Fusion (RRF). This gives you the precision of exact keyword matching alongside the recall of semantic search.

## Why hybrid?

Each retrieval signal has blind spots:

| Signal | Good at | Bad at |
|--------|---------|--------|
| **BM25 keyword** | Exact terms, error codes, proper nouns, technical identifiers | Synonyms, paraphrases, conceptual similarity |
| **Semantic (embedding)** | Meaning, synonyms, conceptual overlap | Exact strings, rare terms, new jargon |
| **Graph (CoOccurrence)** | Associative leaps, records accessed together | Cold-start, isolated records |

RRF fusion merges these ranked lists without needing comparable score scales. It uses rank position, not raw scores, so BM25 scores and cosine similarities combine naturally.

## Components

### BM25Field

`BM25Field` maintains a full inverted index and corpus statistics in Redis sorted sets. It computes BM25 scores at query time via a server-side Lua script. No Redis modules required -- works on both Redis and Valkey.

```python
import popoto
from popoto.fields.bm25_field import BM25Field
from popoto.fields.content_field import ContentField

class Memory(popoto.Model):
    key = popoto.AutoKeyField()
    raw_content = ContentField()
    content_bm25 = BM25Field(source="raw_content")
```

BM25Field is a "side-effect field" -- it does not store a value on the model instance. When a model is saved, the `on_save()` hook reads text from the `source` field, tokenizes it, and atomically updates the inverted index via a Lua script.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source` | `str` | Yes | Name of the field to read content from for indexing. |

**Class Constants (override via subclass):**

| Constant | Default | Description |
|----------|---------|-------------|
| `BM25_K1` | `1.2` | Term frequency saturation. Higher values let repeated terms contribute more. |
| `BM25_B` | `0.75` | Document length normalization. 0 = no normalization, 1 = full normalization. |

**Redis Key Patterns:**

All BM25 data lives in native Redis structures (sorted sets and strings):

| Key | Type | Contents |
|-----|------|----------|
| `$BM25:{Class}:{field}:inv:{term}` | ZSET | Inverted index -- `{doc_key: tf}` per term |
| `$BM25:{Class}:{field}:tf:{doc_key}` | ZSET | Forward index -- `{term: tf}` per doc |
| `$BM25:{Class}:{field}:df` | ZSET | Document frequency -- `{term: df}` |
| `$BM25:{Class}:{field}:dl` | ZSET | Document lengths -- `{doc_key: token_count}` |
| `$BM25:{Class}:{field}:n` | STRING | Total document count |
| `$BM25:{Class}:{field}:avgdl` | STRING | Average document length |

### Tokenization

BM25Field shares its tokenizer with ExistenceFilter via `fields/_tokenizer.py`. The pipeline: lowercase, split on non-word characters, filter tokens shorter than 3 characters, remove common English stop words. BM25Field calls `tokenize(text, unique=False)` to preserve raw term counts for accurate term frequency; ExistenceFilter calls `tokenize(text, unique=True)` for deduplicated fingerprints.

### keyword_search()

The `keyword_search()` method on `QueryBuilder` provides the primary query interface for BM25. It tokenizes the query, executes the BM25 scoring Lua script, hydrates model instances, and attaches a `_bm25_score` attribute to each result.

```python
# Ranked keyword search
results = Memory.query.keyword_search("redis deployment timeout", limit=20)

for memory in results:
    print(f"{memory.key}: {memory._bm25_score:.3f}")
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query_text` | `str` | (required) | The search query string. |
| `field` | `str` | `None` | Name of the BM25Field to search. Auto-detected when the model has exactly one BM25Field. |
| `limit` | `int` | `10` | Maximum results to return. |

You can also call `BM25Field.search()` directly for raw `(redis_key, bm25_score)` tuples without hydration -- useful when feeding results into `fuse()`:

```python
from popoto.fields.bm25_field import BM25Field

scored = BM25Field.search(Memory, "content_bm25", "redis deployment", limit=50)
# Returns [(redis_key, bm25_score), ...]
```

### fuse() -- Reciprocal Rank Fusion

The `fuse()` method on `QueryBuilder` combines heterogeneous ranked lists using the RRF formula:

```
score(d) = sum(1 / (k + rank_i(d)))
```

Each ranked list is a sequence of `(redis_key, score)` tuples. The raw scores are used only for ordering within each list -- RRF uses rank positions, not score magnitudes, so different score scales combine naturally.

```python
from popoto.fields.bm25_field import BM25Field

# Get ranked lists from different signals
keyword_results = BM25Field.search(Memory, "content_bm25", "redis timeout", limit=50)
semantic_results = Memory.query.semantic_search("connection issues", limit=50)

# Fuse with RRF
results = Memory.query.fuse(
    keyword=keyword_results,
    semantic=[(m.db_key.redis_key, m._similarity_score) for m in semantic_results],
    k=60,
    limit=10,
)

for memory in results:
    print(f"{memory.key}: RRF={memory._rrf_score:.4f}")
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `k` | `int` | `60` | RRF smoothing constant. Higher values reduce the influence of top-ranked items. Standard value from Cormack et al. |
| `limit` | `int` | `10` | Maximum results to return. |
| `post_filter` | `Callable` | `None` | Optional `(redis_key, rrf_score) -> bool` callback. Return True to keep the result. |
| `**ranked_lists` | keyword args | (required) | Named ranked lists. Each value is a list of `(redis_key, score)` tuples sorted by score descending. |

### Maintenance

`BM25Field.recompute_stats()` recalculates `avgdl` and `n` from scratch to correct floating-point drift that may accumulate over many incremental updates:

```python
BM25Field.recompute_stats(Memory, "content_bm25")
```

## Hybrid Retrieval Recipe

Here is a complete example wiring BM25, semantic search, and CoOccurrence graph signals into a single fused retrieval call:

```python
import popoto
from popoto.fields.bm25_field import BM25Field
from popoto.fields.content_field import ContentField
from popoto.fields.embedding_field import EmbeddingField
from popoto.fields.co_occurrence_field import CoOccurrenceField
from popoto.fields.existence_filter import ExistenceFilter

class Memory(popoto.Model):
    key = popoto.AutoKeyField()
    raw_content = ContentField()

    # Search signals
    content_bm25 = BM25Field(source="raw_content")
    embedding = EmbeddingField(source="raw_content")
    bloom = ExistenceFilter(error_rate=0.01, capacity=100_000)
    associations = CoOccurrenceField()


def hybrid_search(query: str, limit: int = 10) -> list:
    """Multi-signal retrieval with RRF fusion."""

    # 1. Fast pre-check: skip if definitely no matches
    if Memory.bloom.definitely_missing(Memory, query):
        return []

    # 2. Gather ranked lists from each signal
    keyword_results = BM25Field.search(
        Memory, "content_bm25", query, limit=50
    )
    semantic_results = Memory.query.semantic_search(query, limit=50)

    # 3. Optional: graph propagation from top keyword hits
    graph_results = []
    if keyword_results:
        seed_key = keyword_results[0][0]
        propagated = CoOccurrenceField.propagate(
            Memory, "associations", seed_key, hops=2, limit=50
        )
        graph_results = list(propagated.items())

    # 4. Fuse with RRF
    results = Memory.query.fuse(
        keyword=keyword_results,
        semantic=[
            (m.db_key.redis_key, m._similarity_score)
            for m in semantic_results
        ],
        graph=graph_results,
        k=60,
        limit=limit,
    )

    return results
```

### How it works

1. **ExistenceFilter pre-check** -- O(1) Bloom filter test. If the query terms are definitely absent from the corpus, skip retrieval entirely.
2. **BM25 keyword search** -- Server-side Lua script computes BM25 scores across the inverted index. Returns exact-match precision for technical terms, error codes, and identifiers.
3. **Semantic search** -- Embedding similarity captures conceptual relevance that keyword matching misses.
4. **Graph propagation** -- CoOccurrence associations surface related records that share no lexical or semantic overlap with the query but were historically accessed together.
5. **RRF fusion** -- Reciprocal Rank Fusion merges all ranked lists using rank positions, not raw scores. Documents appearing in multiple lists get boosted. The `k=60` constant smooths rank influence so a #1 result in one list does not overwhelm a consistent #5 across three lists.

### Tuning

| Parameter | Effect | Guidance |
|-----------|--------|----------|
| `BM25_K1` | Term frequency saturation | Increase (1.5-2.0) for long documents where term repetition is meaningful. Decrease (0.5-1.0) for short texts. |
| `BM25_B` | Length normalization | 0.75 works well for mixed-length corpora. Set lower (0.3) if short documents should not be penalized. |
| `k` (RRF) | Rank smoothing | Default 60 is the standard. Lower values (10-20) amplify top-ranked results. Higher values (100+) flatten rank influence. |
| `limit` per signal | Recall pool size | Over-fetch per signal (50-100) then let RRF select the top-K. Larger pools improve fusion quality at the cost of more Redis operations. |

## See also

- [BM25Field in Models and Fields](../fields.md#bm25field) -- field configuration reference
- [ExistenceFilter](existence-filter.md) -- Bloom filter pre-check
- [Composite Score Query](composite-score-query.md) -- weighted ZUNIONSTORE fusion (complementary to RRF)
- [ContextAssembler](context-assembler.md) -- token-budgeted retrieval-to-injection pipeline
- [Agent Memory Quickstart](../guides/agent-memory-quickstart.md) -- progressive adoption guide
