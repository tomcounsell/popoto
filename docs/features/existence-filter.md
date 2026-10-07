# ExistenceFilter and FrequencySketch

Probabilistic data structures for O(1) membership checks and approximate frequency counting — implemented as Lua-backed Redis operations.

## Overview

Two complementary primitives for fast pre-filtering:

- **ExistenceFilter** — Bloom filter for "do I know anything about X?" checks. False positives possible, false negatives impossible.
- **FrequencySketch** — Count-Min Sketch for approximate frequency counting. Overestimates possible, underestimates impossible.

Both operate entirely in Redis via Lua scripts, requiring no client-side state.

## ExistenceFilter

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `capacity` | `int` | `100_000` | Expected number of unique items |
| `error_rate` | `float` | `0.01` | Target false positive rate |
| `fingerprint_fn` | `callable` | `None` | Takes a model instance, returns a fingerprint string |

### Usage

```python
from popoto import Model, KeyField, Field
from popoto.fields.existence_filter import ExistenceFilter

class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    bloom = ExistenceFilter(
        capacity=100_000,
        error_rate=0.01,
        fingerprint_fn=lambda inst: inst.content,
    )
```

Items are added automatically via `on_save()` -- no manual add step needed:

```python
memory = Memory(agent_id="agent-1", content="kubernetes deployment guide")
memory.save()  # Bloom filter updated automatically
```

Check membership before expensive queries:

```python
# O(1) check — avoids expensive query if topic is unknown
if Memory.bloom.might_exist(Memory, "deployment"):
    # Topic exists (or false positive) — proceed with full query
    results = Memory.query.filter(agent_id="agent-1").top_by_decay(10)
else:
    # Definitely not present — skip query entirely
    results = []
```

### Tokenization

Fingerprints are automatically tokenized on write. When `on_save()` runs, the fingerprint string is split into individual words so that word-level queries work correctly.

For example, saving a model with fingerprint `"kubernetes deployment guide"` adds three separate tokens to the bloom filter: `"kubernetes"`, `"deployment"`, and `"guide"`. A subsequent call to `might_exist("kubernetes")` returns `True`.

**Tokenization rules:**
- Input is lowercased
- Split on non-word characters (whitespace, hyphens, colons, etc.)
- Tokens shorter than 3 characters are filtered out
- Common English stop words are filtered out (the, and, for, with, etc.)
- Duplicate tokens are removed

**Fallback:** If tokenization produces zero tokens (e.g., the fingerprint is all stop words or very short), the raw fingerprint string is stored lowercased as a single entry.

**Query normalization:** Queries are tokenized using the same rules. For multi-token queries, `might_exist()` returns `True` if ANY token matches. For `get_frequency()`, the minimum frequency across tokens is returned.

### Architecture

- **Redis key pattern**: `$EF:{ClassName}:{field_name}` (string used as bit array, followed by a 4-byte version marker on v2 filters)
- **Lua script**: Computes k hash positions, sets/checks bits atomically
- **Hash**: Kirsch–Mitzenmacher double hashing over two 32-bit FNV-1a hashes (the bytes forward and reversed), all arithmetic exact in Lua doubles, plain Lua with no `bit` library
- **Size**: Automatically computed from `capacity` and `error_rate` using optimal Bloom filter formulas

### Hash versions

Filters built before #775 used a hash whose intermediate products passed
2^53, where Lua's doubles drop low-order bits. Similar tokens (shared
prefixes, sequential ids) collapsed onto a few positions: 400 such tokens set
64 of the ~1,411 bits they should in a 1,000-capacity filter, and 4 of 66 in
the issue's 20-capacity one. The filter still never gave a false negative, but
for tokens shaped like the ones it held it answered "maybe" far more often
than `error_rate`.

Changing the hash in place would have made every token already in a live
filter test absent, so the hash is versioned per filter:

- **v2** (every filter created since #775) marks itself with four bytes,
  `\x89EF\x02`, stored right after its bit array at byte `ceil(m / 8)`. The
  marker is part of the filter's own string, so it moves with the bits through
  `DUMP`/`RESTORE`, `RENAME`, replication and RDB/AOF.
- **v1** (a filter built before #775) has no marker and needs none: its hash
  only produces positions below `m`, so its string never reaches that offset.
  It keeps being read *and written* with the v1 hash, so every token it holds
  still tests present, and new saves into it stay v1 until it is rebuilt.

`Model.check_indexes()` lists v1 filters under `legacy_hash`
(informational; not counted in `total`), and `field.hash_version(Model)`
returns `2`, `1`, or `None` for a filter that does not exist yet.

`Model.rebuild_indexes()` rebuilds a v1 filter as v2 from the model's records.
It fills a staging key, `$EF:{ClassName}:{field_name}:rebuild`, which every
save also writes while it exists (so a save racing the rebuild is not lost),
then swaps it over the v1 filter with one `RENAME`. Readers see the complete v1
filter until that command and the complete v2 filter after it. If the rebuild
raises, the staging key is deleted and the v1 filter is untouched. Tokens from
deleted records drop out, as they would in any rebuild from records. A v2
filter is left in place.

Export/import never carries the bits, so importing into a destination with
no filter yet builds a v2 one. Postgres keeps an exact token table and has no
hash to version.

## FrequencySketch

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `width` | `int` | `2003` | Number of counters per hash function (prime recommended) |
| `depth` | `int` | `7` | Number of hash functions (must be 1–7) |
| `fingerprint_fn` | `callable` | `None` | Takes a model instance, returns a fingerprint string |

### Usage

```python
from popoto.fields.existence_filter import FrequencySketch

class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    freq = FrequencySketch(
        fingerprint_fn=lambda inst: inst.content,
    )
```

Frequency counters are incremented automatically via `on_save()`:

```python
memory = Memory(agent_id="agent-1", content="kubernetes deployment guide")
memory.save()  # Increments counters for each token
memory.save()  # Increments again

# Get approximate count (may overestimate, never underestimates)
count = Memory.freq.get_frequency(Memory, "kubernetes")  # ~2
```

### Architecture

- **Redis key pattern**: `$FS:{ClassName}:{field_name}` (hash with counter rows)
- **Lua script**: Each of the 7 rows uses an independent per-row polynomial hash (distinct prime multiplier and modulus per row), restoring the standard CMS error bound: `estimate ≤ true + (e/width)·N` with probability `≥ 1 − e^(−depth)`.
- **Estimate**: Returns minimum across all rows (Count-Min property)

!!! note "Upgrade note (beta substrate — breaking change)"
    The `2000 → 2003` default width change and the switch to per-row independent polynomial hashes mean **existing `$FS:*` Redis/Valkey hashes are keyed under the old arithmetic and are NOT migrated**. There is no migration shim — the substrate layer is beta and breaking the serialized sketch format is acceptable. Any `FrequencySketch` persisted before this change will return stale counts until the underlying `$FS:{Class}:{field}` key is deleted (`DEL $FS:YourClass:field_name`) and the sketch is rebuilt from source events (by re-saving the relevant model instances).

## Batch Operations

When checking multiple keywords at once, use the batch methods to avoid per-keyword round-trips to Redis.

### might_exist_batch()

Checks multiple fingerprints in a single Redis round-trip via a single Lua EVAL call. Returns a dict mapping each fingerprint to its boolean result.

```python
# Instead of N separate might_exist() calls:
results = Memory.bloom.might_exist_batch(Memory, ["kubernetes", "deployment", "postgres"])
# {"kubernetes": True, "deployment": True, "postgres": False}

# Filter to only keywords worth querying
candidates = [kw for kw, hit in results.items() if hit]
if candidates:
    results = Memory.query.filter(agent_id="agent-1").top_by_decay(10)
```

Duplicate fingerprints in the input list are deduplicated automatically. Each fingerprint is tokenized using the same rules as `might_exist()` -- a fingerprint is a hit if ANY of its tokens appears in the filter.

### might_exist_count()

When you only need to know how many keywords match (not which ones), `might_exist_count()` returns the count directly:

```python
hit_count = Memory.bloom.might_exist_count(Memory, ["kubernetes", "deployment", "postgres"])
if hit_count == 0:
    return []  # Nothing relevant -- skip the expensive query
```

### Performance

Both batch methods execute a single `EVAL` command regardless of input size, so latency is constant (one round-trip) rather than linear in the number of keywords. This matters most for hook/subprocess callers where import time and connection setup dominate -- batching amortizes that cost across all keywords.

### Integration Pattern: Subprocess Callers

For callers that invoke existence checks from a subprocess or hook (where Python import cost is significant), the batch API minimizes overhead:

```python
import subprocess, json

# Single subprocess call checks all keywords at once
keywords = ["kubernetes", "deployment", "redis", "postgres"]
result = subprocess.run(
    ["python", "-c", f"""
import json
from myapp.models import Memory
results = Memory.bloom.might_exist_batch(Memory, {json.dumps(keywords)})
print(json.dumps(results))
"""],
    capture_output=True, text=True
)
hits = json.loads(result.stdout)
# One process spawn + one Redis round-trip for all keywords
```

Compare this to calling `might_exist()` in a loop -- each call would either require its own subprocess (paying import cost each time) or a single subprocess with a loop (one import but N round-trips). The batch API gives you one import and one round-trip.

## When to Use Which

| Use Case | Primitive |
|----------|-----------|
| "Have I seen this topic before?" | ExistenceFilter |
| "How many times has this topic appeared?" | FrequencySketch |
| Pre-filter before expensive CompositeScoreQuery | ExistenceFilter |
| Check many keywords in one round-trip | `might_exist_batch()` |
| Count known keywords without details | `might_exist_count()` |
| Rank query terms by selectivity (IDF) | `BM25Field.get_idf()` |
| Frequency-based write filtering | FrequencySketch |

## See Also

- [API Reference: ExistenceFilter](../reference/popoto/fields/existence_filter.md) — method signatures and parameters
- [Agent Memory overview](agent-memory.md) — full primitives reference
- [ContextAssembler](context-assembler.md) — uses ExistenceFilter for pull-path pre-checks
