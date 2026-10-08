# ExistenceFilter and FrequencySketch

Probabilistic data structures for O(1) membership checks and approximate frequency counting — implemented as Lua-backed operations on Redis, and as exact token tables on Postgres (see [On Postgres](#on-postgres)).

## Overview

Two complementary primitives for fast pre-filtering:

- **ExistenceFilter** — Bloom filter for "do I know anything about X?" checks. False positives possible, false negatives impossible.
- **FrequencySketch** — Count-Min Sketch for approximate frequency counting. Overestimates possible, underestimates impossible.

On Redis both operate entirely server-side via Lua scripts, requiring no client-side state. The descriptions of false positives, overestimates, bit arrays and hash versions below are the Redis implementation.

## ExistenceFilter

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `capacity` | `int` | `100_000` | Expected number of unique items |
| `error_rate` | `float` | `0.01` | Target false positive rate |
| `fingerprint_fn` | `callable` | `None` | Takes a model instance, returns a fingerprint string |
| `hash_version` | `int` | `1` | Hash a *new* filter is created with: `1`, or `2` to opt in (see [Hash versions](#hash-versions) and the rolling-upgrade rule first) |

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

### Architecture (Redis)

- **Redis key pattern**: `$EF:{ClassName}:{field_name}` (string used as bit array, followed by a 4-byte version marker on v2 filters)
- **Lua script**: Computes k hash positions, sets/checks bits atomically
- **Hash**: Kirsch–Mitzenmacher double hashing over two 32-bit FNV-1a hashes (the bytes forward and reversed), all arithmetic exact in Lua doubles, plain Lua with no `bit` library
- **Size**: Automatically computed from `capacity` and `error_rate` using optimal Bloom filter formulas

### Hash versions

!!! warning "Rolling-upgrade rule: upgrade every process before opting in to v2"
    Code from before #775 reads and writes **every** filter with the v1 hash.
    Pointed at a v2 filter, it gives false negatives: its reads miss the
    tokens stored with v2, and the tokens it adds land on positions v2 reads
    never check. So v2 is **opt-in**, and nothing in this version creates a
    v2 filter unless asked to. Before passing `hash_version=2` or calling
    `rebuild_indexes(bloom_hash_version=2)`, upgrade **every** process that
    reads or writes the model -- workers, cron jobs, notebooks -- to a popoto
    with #775. Until then, leave the defaults alone: with them, old and new
    processes share v1 filters with no false negatives in either direction.

Filters built before #775 use a hash whose intermediate products pass 2^53,
where Lua's doubles drop low-order bits. Similar tokens (shared prefixes,
sequential ids) collapse onto a few positions: 400 such tokens set 64 of the
~1,411 bits they should in a 1,000-capacity filter, and 4 of 66 in the
issue's 20-capacity one. The filter still never gives a false negative, but
for tokens shaped like the ones it holds it answers "maybe" far more often
than `error_rate`.

Changing the hash in place would make every token already in a live filter
test absent, so the hash is versioned per filter:

- **v1** has no marker and needs none: its hash only produces positions below
  `m`, so its string never reaches the marker's offset. It is what every
  filter built before #775 is, and still what a new filter is by default. A
  v1 filter is read *and written* with the v1 hash, byte for byte as before.
- **v2** marks itself with four bytes, `\x89EF\x02`, stored right after its
  bit array at byte `ceil(m / 8)`. The marker is part of the filter's own
  string, so it moves with the bits through `DUMP`/`RESTORE`, `RENAME`,
  replication and RDB/AOF. A v2 filter is always read and written as v2,
  whatever the field's `hash_version` says.

`Model.check_indexes()` lists v1 filters under `legacy_hash` (informational;
not counted in `total`), and `field.hash_version(Model)` returns `2`, `1`, or
`None` for a filter that does not exist yet.

**Opting in**, once the rule above holds:

- `ExistenceFilter(..., hash_version=2)` makes a *missing* filter v2 when the
  first save creates it. It does not touch an existing filter.
- `Model.rebuild_indexes(bloom_hash_version=2)` (or
  `await Model.async_rebuild_indexes(bloom_hash_version=2)`) converts the
  model's v1 filters, and missing ones, to v2 from its records. Without the
  argument, `rebuild_indexes()` keeps each filter's version and re-saves
  into it in place, exactly as before #775. A v2 filter is never converted
  back.

**How a conversion stays free of false negatives:**

1. It takes the filter's lock, `$EF:{ClassName}:{field_name}:rebuild`, with
   `SET NX PX` and a random token, and in the same Lua step deletes whatever
   a dead conversion left in the staging key,
   `$EF:{ClassName}:{field_name}:rebuild:staging`, and opens it empty as v2.
   This happens before any index is deleted. The staging key's name is fixed;
   the token lives only in the lock's value, and every script that renews,
   swaps or drops the staging key first checks that the lock still holds its
   token, so a conversion that lost its lock can never extend, rename or
   delete the staging key of the one that took over.
2. It re-saves every record. Every save -- the rebuild's and any other
   process's -- checks the lock inside its own add script and, while one is
   held, also writes the staging key, so a save racing the rebuild is not
   lost. The live filter keeps answering in full the whole time.
3. It swaps the staging key over the live filter with one Lua
   compare-and-rename, which happens only if the lock still holds this
   rebuild's token and the staging key carries the v2 marker. It clears the
   expiry the rename would otherwise carry over, then releases the lock.
   Readers see the complete old filter until that step and the complete v2
   filter after it.

**One conversion of a filter runs at a time.** A second one raises
`BloomRebuildInProgressError` before it has changed anything, so no index is
deleted and no filter is touched; retry it once the first one finishes.
(Waiting was the alternative. A bounded wait short enough to be useful cannot
cover a full rebuild of a large store, and by the time the first rebuild
finishes, the conversion the second one asked for is already done.) A plain
`rebuild_indexes()` is never refused: it does no swap, and its re-saves also
land in the running conversion's staging key.

**Crashes.** The lock and staging key expire after
`Defaults.BLOOM_REBUILD_LOCK_TTL_MS` (60 s). A running conversion renews
both every third of that from a background thread, started when the lock
is taken and stopped just before the swap, so renewal covers step 1 (the
index deletion, which visits no record and can take seconds on a large
store) as well as the re-saves. If a conversion raises, it deletes its
staging key and releases the lock, and the thread is stopped and joined
first. If it dies (for example, SIGKILL), the live filter was never
touched, the lock lapses, no save writes the orphaned staging key, and
`check_indexes()` lists it under `stale_bloom_staging` until it expires or
the next conversion deletes it. That check is two `EXISTS` per bloom field,
not a keyspace scan. A conversion whose whole process stalls past its lock
(SIGSTOP, a VM pause: the renewer thread is paused too) has its swap refused
with `BloomRebuildLostLockError`. Every other index is still rebuilt, and
the filter is left as it was.

**What the opt-in does not cover:** a *pre-#775* process saving during a
conversion does not write the staging key. Its tokens are in the old filter
but not in the new one, so they are lost at the swap. This is one more
reason for the rolling-upgrade rule.

Tokens from deleted records drop out of a converted filter, as in any rebuild
from records. Export/import never carries the bits: importing into a
destination with no filter creates one in the field's `hash_version`.
Postgres keeps an exact token table and has no hash to version (see [On Postgres](#on-postgres)).

Every script declares the filter, the lock and the staging key in `KEYS`.

## FrequencySketch

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `width` | `int` | `2003` | Number of counters per hash function (prime recommended) |
| `depth` | `int` | `7` | Number of hash functions (must be 1–7) |
| `fingerprint_fn` | `callable` | `None` | Takes a model instance, returns a fingerprint string |

`FrequencySketch` has no `hash_version` argument: the hash versions apply to
`ExistenceFilter` only, and passing one raises `TypeError`.

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

### Architecture (Redis)

- **Redis key pattern**: `$FS:{ClassName}:{field_name}` (hash with counter rows)
- **Lua script**: Each of the 7 rows uses an independent per-row polynomial hash (distinct prime multiplier and modulus per row), restoring the standard CMS error bound: `estimate ≤ true + (e/width)·N` with probability `≥ 1 − e^(−depth)`.
- **Estimate**: Returns minimum across all rows (Count-Min property)

!!! note "Upgrade note (beta substrate — breaking change)"
    The `2000 → 2003` default width change and the switch to per-row independent polynomial hashes mean **existing `$FS:*` Redis/Valkey hashes are keyed under the old arithmetic and are NOT migrated**. There is no migration shim — the substrate layer is beta and breaking the serialized sketch format is acceptable. Any `FrequencySketch` persisted before this change will return stale counts until the underlying `$FS:{Class}:{field}` key is deleted (`DEL $FS:YourClass:field_name`) and the sketch is rebuilt from source events (by re-saving the relevant model instances).

## Batch Operations

When checking multiple keywords at once, use the batch methods to avoid per-keyword round-trips to the server.

### might_exist_batch()

Checks multiple fingerprints in one round-trip (a single Lua `EVAL` on Redis, one `SELECT` on Postgres). Returns a dict mapping each fingerprint to its boolean result.

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

Both batch methods execute a single `EVAL` command (on Postgres, a single `SELECT`) regardless of input size, so latency is constant (one round-trip) rather than linear in the number of keywords. This matters most for hook/subprocess callers where import time and connection setup dominate -- batching amortizes that cost across all keywords.

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
# One process spawn + one round-trip for all keywords
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

## On Postgres

On a Postgres-bound model neither primitive is probabilistic. An
`ExistenceFilter` is a token table, `<table>__<f>__tok (token, _pk)`, holding
each live record's tokens; a `FrequencySketch` is a count table,
`<table>__<f>__cnt (token, count)`. Saves keep both in step; a delete
cascades to the token rows, while the count table, like the sketch, is never
decremented. See
[Membership](postgres-backend.md#membership). The differences are rows in
[Records and other behaviour](postgres-backend.md#records-and-other-behaviour):

- `might_exist` (and the batch and count forms) is exact: no false
  positives, and a deleted record is forgotten. On a `Meta.ttl` model an
  expired record's tokens stop counting at once.
- `get_frequency` is the exact number of saves, never decremented, as the
  sketch never is.
- `fill_ratio` is an estimate, `1 - e^(-k·n/m)` for the `n` distinct tokens
  stored, the fill a bloom of the field's parameters would have.

The Redis-only parts do not apply: `capacity`, `error_rate` and the sketch's
width and depth only size the `fill_ratio` estimate, `hash_version` and
`rebuild_indexes(bloom_hash_version=2)` are accepted and ignored (there is no
bit array to version), `field.hash_version(Model)` returns `None`, and
`check_indexes()` reports no `legacy_hash` or `stale_bloom_staging` entries.

## See Also

- [API Reference: ExistenceFilter](../reference/popoto/fields/existence_filter.md) — method signatures and parameters
- [Agent Memory overview](agent-memory.md) — full primitives reference
- [ContextAssembler](context-assembler.md) — uses ExistenceFilter for pull-path pre-checks
