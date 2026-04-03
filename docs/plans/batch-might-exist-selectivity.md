---
status: In Progress
type: feature
appetite: Medium
owner: Valor
created: 2026-04-03
tracking: https://github.com/tomcounsell/popoto/issues/341
last_comment_id:
---

# ExistenceFilter: Batch might_exist() + Selectivity Signal

## Problem

When using Popoto's ExistenceFilter and BM25Field in a latency-sensitive hook (e.g., a PostToolUse subprocess that fires on every 3rd tool call), two patterns cause performance and relevance problems:

1. **N round-trips for N keywords.** `ExistenceFilter.might_exist()` checks one keyword per call. Checking 10 keywords = 10 `EVAL` calls. In the real integration, the hook extracts ~10 keywords per invocation and checks each against the bloom filter individually.

2. **No selectivity signal.** The bloom filter answers "does this word appear in any memory?" but not "how common is this word?" A keyword like `agent` that appears in 200/207 memories always hits the bloom filter but is useless for retrieval. A keyword like `watchdog` appearing in 3/207 memories is highly selective. Without this signal, the caller wastes time on BM25 retrieval queries dominated by generic keywords.

3. **Subprocess import cost.** In subprocess-per-call patterns (like Claude Code hooks), `from popoto import ...` + model registration costs ~150ms on every invocation. This is not a code change but a documentation/guidance gap.

**Desired outcome:** A single-PR enhancement that adds batch bloom checking and a selectivity signal, plus documentation for subprocess callers.

## Prior Art

- **Issue #213 / Plan: existence_filter.md** -- ExistenceFilter and FrequencySketch shipped. Bloom filter uses Lua scripts with SETBIT/GETBIT. CMS uses Lua scripts with HINCRBY/HGET. Both are module-free (Redis + Valkey compatible).
- **BM25Field** -- Already maintains document frequency (`df`) in a sorted set at `$BM25:{Class}:{field}:df` and total doc count (`n`) at `$BM25:{Class}:{field}:n`. IDF is computed inside the search Lua script but not exposed as a standalone query.
- **FrequencySketch** -- CMS-based approximate frequency already exists. Tracks per-token frequency incremented on every save. Could serve as the selectivity signal directly.

## Evaluation of Selectivity Signal Options

The issue proposes three approaches. Here is the analysis:

### Option A: Token Frequency Hash (new Redis hash alongside bloom)

- **Mechanism:** On save, maintain a Redis hash `$EF:{Class}:{field}:df` mapping each token to its document frequency count (HINCRBY +1 per unique doc containing the token).
- **Pros:** Exact document frequency. Simple to query (HMGET for batch). Directly computes selectivity as `df/N`.
- **Cons:** New data structure to maintain. Requires tracking which tokens a doc previously contributed (for update/overwrite correctness). Duplicates information already in BM25Field's `df` sorted set.
- **Verdict:** Viable but redundant if BM25Field is already on the model.

### Option B: FrequencySketch (existing CMS)

- **Mechanism:** FrequencySketch already exists and is already incremented on save. `get_frequency()` returns approximate count.
- **Pros:** Already shipped. No new data structures. Approximate counts are sufficient for "is this token too common?" decisions.
- **Cons:** CMS counts total occurrences (term frequency across all saves), not document frequency. A term appearing 50 times in one document looks the same as appearing once in 50 documents. This makes it a poor proxy for selectivity/IDF. Also requires a separate field declaration on the model.
- **Verdict:** Poor fit for selectivity. Good for "how often has this exact string been saved" but not "how many documents contain this term."

### Option C: BM25Field.get_idf() (expose existing data)

- **Mechanism:** BM25Field already stores `df` (document frequency per term) and `n` (total docs). A new `get_idf()` or `get_df()` class method reads these directly without running a full search.
- **Pros:** Zero new data structures. Data is already maintained and correct (handles updates, deletes). IDF formula is standard: `log((N - df + 0.5) / (df + 0.5) + 1)`. Can batch-query via ZMSCORE or a small Lua script.
- **Cons:** Only available when the model has a BM25Field. Couples the selectivity check to the search field.
- **Verdict:** Best option when BM25Field is present (which it is in the target use case). Most accurate, zero additional storage cost, already handles updates/deletes correctly.

### Recommendation

**Ship Option C (BM25Field.get_idf) as the primary selectivity signal.** It reuses existing, well-maintained data structures and provides exact IDF values. The caller's integration already uses both ExistenceFilter and BM25Field, so no additional model fields are needed.

**Do NOT add a new token frequency hash to ExistenceFilter.** It would duplicate BM25Field's df data and require complex update logic for correctness.

**FrequencySketch remains available** for use cases that need raw occurrence counting, but it is not suitable as a selectivity/IDF signal.

## Data Flow

### Part 1: Batch might_exist()

```
Caller: bloom.might_exist_batch(Memory, ["agent", "session", "watchdog"])
  |
  v
Python: Build token list, call single Lua script
  |
  v
Lua: For each token, compute k hash positions, check all bits via GETBIT
  |
  v
Return: {"agent": True, "session": True, "watchdog": False}
```

One Redis round-trip instead of N.

### Part 2: BM25Field.get_idf()

```
Caller: BM25Field.get_idf(Memory, "content", ["agent", "session", "watchdog"])
  |
  v
Python: Read N from $BM25:Memory:content:n
         Read df for each term from $BM25:Memory:content:df via ZMSCORE
  |
  v
Compute: idf_i = log((N - df_i + 0.5) / (df_i + 0.5) + 1)
  |
  v
Return: {"agent": 0.02, "session": 0.15, "watchdog": 2.41}
```

Two Redis commands (GET + ZMSCORE) regardless of token count. Caller filters out low-IDF tokens before running BM25Field.search().

### Part 3: Recommended integration pattern (docs only)

No code changes. A documentation section showing:
- Minimal import pattern for subprocess callers
- Batch bloom + IDF filtering before search
- Example hook code

## Architectural Impact

- **New dependencies:** None.
- **Interface changes:** Two new public methods on existing classes. No changes to existing method signatures.
- **Coupling:** Low. `might_exist_batch()` is self-contained on ExistenceFilter. `get_idf()` reads existing BM25Field data structures.
- **Data ownership:** No new Redis keys. Both methods read from existing key patterns.
- **Reversibility:** Easy -- purely additive methods.

## Appetite

**Size:** Small-Medium (single PR)

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis or Valkey server | `python -c "from popoto.redis_db import POPOTO_REDIS_DB; POPOTO_REDIS_DB.ping()"` | Core Redis commands available |
| ExistenceFilter shipped | `python -c "from popoto import ExistenceFilter"` | Base class exists |
| BM25Field shipped | `python -c "from popoto import BM25Field"` | IDF data structures exist |

## Solution

### Part 1: ExistenceFilter.might_exist_batch()

Add a new Lua script `BLOOM_EXISTS_BATCH_LUA` that checks multiple tokens in a single EVAL call:

```lua
-- KEYS[1] = bloom filter key
-- ARGV[1] = m (total bits)
-- ARGV[2] = k (hash functions)
-- ARGV[3..N] = tokens to check

local key = KEYS[1]
local m = tonumber(ARGV[1])
local k = tonumber(ARGV[2])
local LARGE_MOD = 4503599627370496

local results = {}
for t = 3, #ARGV do
    local item = ARGV[t]
    local h1 = 5381
    local h2 = 16777619
    for i = 1, #item do
        local c = string.byte(item, i)
        h1 = ((h1 * 33) + c) % LARGE_MOD
        h2 = ((h2 * 16777619) + c) % LARGE_MOD
    end
    h1 = h1 % m
    h2 = h2 % m

    local found = 1
    for i = 0, k - 1 do
        local pos = (h1 + i * h2) % m
        if redis.call('GETBIT', key, pos) == 0 then
            found = 0
            break
        end
    end
    results[#results + 1] = found
end
return results
```

Python API:

```python
def might_exist_batch(self, model_class, fingerprints):
    """Check multiple fingerprints against the Bloom filter in one round-trip.

    Args:
        model_class: The Model class to check against.
        fingerprints: List of fingerprint strings to check.

    Returns:
        dict[str, bool]: Mapping of fingerprint -> might_exist result.
    """
    ...

def might_exist_count(self, model_class, fingerprints):
    """Count how many fingerprints might exist in a single round-trip.

    Convenience wrapper around might_exist_batch().

    Args:
        model_class: The Model class to check against.
        fingerprints: List of fingerprint strings to check.

    Returns:
        int: Number of fingerprints that might exist.
    """
    ...
```

Each fingerprint string is tokenized before checking (matching existing might_exist behavior). If a fingerprint produces multiple tokens, the batch script checks each token and considers the fingerprint a hit if ANY token is found (matching current behavior).

**Design decision:** The Lua script operates on individual tokens, not on fingerprints. The Python layer handles tokenization and maps fingerprints to tokens, then maps token results back to fingerprints. This keeps the Lua script simple and reuses the same hash functions as the single-token version.

### Part 2: BM25Field.get_idf()

New class method on BM25Field:

```python
@classmethod
def get_idf(cls, model_class, field_name, tokens, as_dict=True):
    """Get IDF scores for tokens without running a full search.

    Reads document frequency from the existing BM25 df sorted set
    and total doc count. Computes standard BM25 IDF:
        idf = log((N - df + 0.5) / (df + 0.5) + 1)

    Args:
        model_class: The Model class.
        field_name: Name of the BM25Field.
        tokens: Single token string or list of token strings.
        as_dict: If True, return dict mapping token -> IDF.
            If False, return list of (token, idf) tuples.

    Returns:
        dict[str, float] or list[tuple[str, float]]:
            IDF scores. Tokens not in the corpus get maximum IDF
            (log(N + 1) when df=0).
    """
```

Implementation uses two Redis commands:
1. `GET $BM25:{Class}:{field}:n` -- total doc count
2. `ZMSCORE $BM25:{Class}:{field}:df token1 token2 ...` -- batch df lookup

`ZMSCORE` was added in Redis 6.2 and is supported by Valkey. It returns scores for multiple members in a single call. For older Redis versions, fall back to individual `ZSCORE` calls.

Also add a convenience method:

```python
@classmethod
def filter_selective_tokens(cls, model_class, field_name, tokens, min_idf=1.0):
    """Filter tokens to only those with IDF above a threshold.

    Useful for pre-filtering keywords before running search().
    Tokens not in the corpus are considered maximally selective and included.

    Args:
        model_class: The Model class.
        field_name: Name of the BM25Field.
        tokens: List of token strings.
        min_idf: Minimum IDF score to keep. Default 1.0.

    Returns:
        list[str]: Tokens with IDF >= min_idf.
    """
```

### Part 3: Documentation for Subprocess Callers

Add a section to the agent memory documentation covering:

1. **Minimal import pattern:** Import only what you need (`from popoto.fields.existence_filter import ExistenceFilter` instead of `import popoto`).
2. **Batch bloom + IDF workflow:**
   ```python
   # 1. Batch bloom check (1 round-trip)
   hits = bloom.might_exist_batch(Memory, keywords)
   bloom_hits = [k for k, v in hits.items() if v]

   # 2. Filter by selectivity (1-2 round-trips)
   selective = BM25Field.filter_selective_tokens(
       Memory, "content", bloom_hits, min_idf=1.0
   )

   # 3. Search only if selective keywords remain
   if selective:
       results = BM25Field.search(Memory, "content", " ".join(selective))
   ```
3. **Subprocess best practices:** Keep model definitions minimal, consider a thin client script that only imports the fields needed for the check.

## Scope: Single PR vs Follow-Up

### In this PR (feasible, well-scoped):
- `ExistenceFilter.might_exist_batch()` and `might_exist_count()`
- `BM25Field.get_idf()` and `BM25Field.filter_selective_tokens()`
- Tests for all new methods
- Documentation section for subprocess callers

### Deferred to follow-up issues:
- **Import cost optimization** (lazy loading, deferred model registration) -- this is a larger refactor affecting the module system
- **EVALSHA caching** -- pre-register Lua scripts via SCRIPT LOAD for faster repeated calls. Minor optimization, not blocking.
- **Async variants** -- `async might_exist_batch()`, `async get_idf()`. Follow existing async patterns when added.
- **ExistenceFilter.might_exist() optimization** -- the current single-token method loops in Python calling EVAL per token. Could be refactored to use the batch Lua script internally. Minor refactor.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `might_exist_batch()` with empty list -- return empty dict
- [ ] `might_exist_batch()` when bloom key does not exist -- all False
- [ ] `get_idf()` when BM25 index is empty (n=0) -- return 0.0 for all tokens
- [ ] `get_idf()` with tokens not in corpus -- return maximum IDF
- [ ] `filter_selective_tokens()` with empty token list -- return empty list

### Edge Cases
- [ ] `might_exist_batch()` with a single fingerprint -- degenerate batch
- [ ] `might_exist_batch()` with duplicate fingerprints -- deduplicate in result
- [ ] `get_idf()` with a single token (string not list) -- handle gracefully
- [ ] `ZMSCORE` unavailable (Redis < 6.2) -- fall back to individual ZSCORE calls

### Integration
- [ ] Batch bloom + IDF filter + search end-to-end test
- [ ] Verify Lua scripts use only core commands (no module commands)

## Test Impact

No existing tests affected. All tests are new and additive:
- `tests/test_existence_filter.py` -- add batch method tests
- `tests/test_bm25_field.py` -- add get_idf and filter_selective_tokens tests

## Rabbit Holes

- **Token frequency hash on ExistenceFilter.** Do not add a new Redis hash for document frequency tracking. BM25Field already maintains this data. Adding it to ExistenceFilter duplicates storage and requires complex update/delete logic that ExistenceFilter does not currently have.
- **Modifying FrequencySketch to track document frequency.** The CMS tracks occurrence counts, not document frequency. Changing its semantics would break existing users.
- **Auto-integration of selectivity filtering into BM25Field.search().** The caller should control which tokens to filter. Automatic filtering removes caller control over the relevance/recall trade-off.
- **Redis modules (BF.*, CMS.*, etc.).** Never use module commands. All features must work on both Redis and Valkey using core commands only.
- **Import-time optimization.** This is a larger refactor that should be a separate issue. The docs section provides practical guidance for subprocess callers without changing Popoto internals.

## Risks

### Risk 1: ZMSCORE Availability
**Impact:** ZMSCORE requires Redis >= 6.2 or compatible Valkey version.
**Mitigation:** Check for ZMSCORE support at runtime. Fall back to a pipeline of individual ZSCORE commands if unavailable. Both paths return identical results.

### Risk 2: Large Token Lists in Lua
**Impact:** Passing hundreds of tokens to a single EVAL call could block Redis briefly.
**Mitigation:** The use case involves ~10 keywords per call, well within safe limits. Document that batch sizes over 100 should be chunked by the caller.

## Race Conditions

No race conditions. Batch bloom check is read-only (GETBIT). IDF query is read-only (GET + ZMSCORE). Both can safely run concurrently with writes.

## No-Gos (Out of Scope)

- Import cost optimization / lazy loading (separate issue)
- EVALSHA script caching (minor optimization, separate PR)
- Async variants of new methods (follow existing async patterns)
- Automatic selectivity filtering inside BM25Field.search()
- New Redis data structures (reuse existing ones)
- Redis module commands (BF.*, CMS.*, etc.)

## Success Criteria

- [ ] `ExistenceFilter.might_exist_batch(model_class, fingerprints)` returns `dict[str, bool]` in a single Redis round-trip
- [ ] `ExistenceFilter.might_exist_count(model_class, fingerprints)` returns `int`
- [ ] `BM25Field.get_idf(model_class, field_name, tokens)` returns IDF scores using existing df/n data
- [ ] `BM25Field.filter_selective_tokens(model_class, field_name, tokens, min_idf)` filters by IDF threshold
- [ ] All new methods work on both Redis and Valkey (no module commands)
- [ ] Batch bloom check uses a single Lua EVAL (not N calls)
- [ ] IDF query uses at most 2 Redis commands (GET + ZMSCORE or pipeline of ZSCORE)
- [ ] Tests cover empty inputs, missing keys, single items, and integration flow
- [ ] Documentation section covers subprocess caller patterns
- [ ] Tests pass (`pytest tests/ -x -q`)

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Batch bloom tests | `pytest tests/test_existence_filter.py -v -k batch` | exit code 0 |
| IDF tests | `pytest tests/test_bm25_field.py -v -k idf` | exit code 0 |
| Import works | `python -c "from popoto import ExistenceFilter, BM25Field"` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |

## Step by Step Tasks

### 1. Add batch Lua script and might_exist_batch/might_exist_count to ExistenceFilter
- **Task ID**: build-batch-bloom
- **Depends On**: none
- Add `BLOOM_EXISTS_BATCH_LUA` Lua script to `existence_filter.py`
- Add `might_exist_batch()` method: tokenize each fingerprint, call batch Lua, map results back
- Add `might_exist_count()` convenience wrapper
- Handle edge cases: empty list, single item, duplicates

### 2. Add get_idf() and filter_selective_tokens() to BM25Field
- **Task ID**: build-idf-methods
- **Depends On**: none
- **Parallel**: true (with task 1)
- Add `get_idf()` class method: read N, batch-read df via ZMSCORE, compute IDF
- Add ZMSCORE fallback for older Redis versions
- Add `filter_selective_tokens()` convenience method
- Handle edge cases: empty corpus, missing tokens, single token input

### 3. Write tests
- **Task ID**: build-tests
- **Depends On**: build-batch-bloom, build-idf-methods
- Add batch bloom tests to `tests/test_existence_filter.py`
- Add IDF tests to `tests/test_bm25_field.py`
- Add integration test: batch bloom -> IDF filter -> search
- Cover edge cases from Failure Path section

### 4. Write subprocess caller documentation
- **Task ID**: write-docs
- **Depends On**: build-batch-bloom, build-idf-methods
- **Parallel**: true (with task 3)
- Add section to agent memory docs covering batch + IDF pattern
- Add subprocess best practices (minimal imports, thin client)
- Include complete example code

### 5. Validate
- **Task ID**: validate
- **Depends On**: build-tests, write-docs
- Run full test suite
- Verify all success criteria met
- Verify no module commands used (Redis + Valkey compatible)
