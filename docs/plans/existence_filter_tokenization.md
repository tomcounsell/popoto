---
status: In Progress
type: bugfix
appetite: Small
owner: Valor Engels
created: 2026-03-26
tracking: https://github.com/tomcounsell/popoto/issues/280
---

# ExistenceFilter: Tokenize fingerprints on write for word-level bloom queries

## Problem

`ExistenceFilter.on_save()` adds the entire fingerprint string as a single bloom entry. But callers — including `ContextAssembler._pull_path()` at line 378 — query `might_exist()` with individual words/tokens from `query_cues`. The hashes never match because "kubernetes" and "Valor prefers kubernetes deployments" produce completely different bit patterns.

This means `might_exist()` always returns `False` for word-level queries, making the bloom filter useless as a pre-check. The ExistenceFilter's core guarantee (zero false negatives) is violated in practice because the write granularity (whole string) does not match the read granularity (individual tokens).

**Reproduction:**

```python
m = Memory(content="Valor prefers the full SDLC workflow", importance=6.0)
m.save()

Memory.bloom.might_exist(Memory, "SDLC")      # False — should be True
Memory.bloom.might_exist(Memory, "workflow")   # False — should be True
```

**Current behavior:** `on_save()` hashes `"Valor prefers the full SDLC workflow"` as one entry. `might_exist("SDLC")` hashes `"SDLC"` as a different entry. No overlap.

**Desired outcome:** `on_save()` tokenizes the fingerprint string and adds each meaningful token individually. `might_exist("SDLC")` then finds the bits set by the `"sdlc"` token.

## Prior Art

- **Issue #213 / PR #226**: Original ExistenceFilter implementation. Tests all use exact-match queries (save "kubernetes", query "kubernetes"), so the bug is invisible.
- **ContextAssembler** (`src/popoto/recipes/context_assembler.py` line 377-379): Iterates `query_cues.values()` and calls `definitely_missing()` with each cue value individually. This is the primary caller that exposes the mismatch.
- **FrequencySketch**: Same module, same pattern — `on_save()` stores the whole fingerprint as one entry. Same bug applies.

## Data Flow

### Current (broken)

```
Save: fingerprint_fn(instance) -> "kubernetes deployment guide"
      -> BLOOM_ADD_LUA("kubernetes deployment guide")  [1 entry]

Query: might_exist("kubernetes")
      -> BLOOM_EXISTS_LUA("kubernetes")  [different hash, no match]
      -> returns False  ← WRONG
```

### Proposed (fixed)

```
Save: fingerprint_fn(instance) -> "kubernetes deployment guide"
      -> tokenize -> ["kubernetes", "deployment", "guide"]
      -> BLOOM_ADD_LUA("kubernetes")   [entry 1]
      -> BLOOM_ADD_LUA("deployment")   [entry 2]
      -> BLOOM_ADD_LUA("guide")        [entry 3]

Query: might_exist("kubernetes")
      -> BLOOM_EXISTS_LUA("kubernetes")  [matches entry 1]
      -> returns True  ← CORRECT
```

## Solution

### 1. Add a `tokenize()` helper function

Add a module-level `tokenize(text)` function in `existence_filter.py`:

- Lowercase the input
- Split on whitespace and punctuation (regex: `\W+`)
- Strip remaining non-alphanumeric characters
- Filter out tokens shorter than 3 characters (avoids noise like "a", "is", "to")
- Filter out a small set of common English stop words (the, and, for, with, etc.)
- Return a list of unique tokens (deduplicated, order does not matter)

The min-length threshold of 3 and the stop word list are implementation constants, not user-configurable parameters. They can be adjusted later if needed.

**Why not configurable?** The tokenizer is internal plumbing. Making it configurable adds API surface without clear benefit. Users who need custom tokenization can override `fingerprint_fn` to return pre-tokenized content (one word per save, or a delimiter-separated format).

### 2. Modify `ExistenceFilter.on_save()` to tokenize

Current `on_save()` calls `BLOOM_ADD_LUA` once with the full fingerprint. Change it to:

1. Compute fingerprint via `_compute_fingerprint()`
2. Call `tokenize(fingerprint)` to get token list
3. If token list is empty (fingerprint was all stop words or too short), fall back to adding the raw fingerprint as-is (preserves backward compat for short/opaque fingerprints like redis keys)
4. Call `BLOOM_ADD_LUA` for each token (or batch via a multi-add Lua script)

**Pipeline support:** When a pipeline is provided, all `BLOOM_ADD_LUA` calls go through the pipeline. No special handling needed.

**Performance consideration:** A typical fingerprint produces 3-15 tokens. Each `BLOOM_ADD_LUA` is a single `EVAL` call. With pipelining (which `Model.save()` already uses), this is a single round-trip. Without pipelining, it is N round-trips — acceptable for the expected token count.

**Alternative: single Lua script for all tokens.** Instead of N separate `EVAL` calls, pass all tokens as ARGV and loop inside the Lua script. This is a strict improvement (1 EVAL instead of N) and should be the implementation approach. The Lua script becomes:

```lua
-- BLOOM_ADD_MULTI_LUA
-- KEYS[1] = bloom key
-- ARGV[1] = m (bits)
-- ARGV[2] = k (hash functions)
-- ARGV[3..N] = tokens to add
for t = 3, #ARGV do
    local item = ARGV[t]
    -- hash and SETBIT (same as current BLOOM_ADD_LUA)
end
```

### 3. Modify `FrequencySketch.on_save()` to tokenize

Apply the same tokenization to `FrequencySketch.on_save()`. Each token gets its own counter increment. `get_frequency("kubernetes")` then returns the correct count.

Same batched Lua script approach: pass all tokens as ARGV, loop inside Lua.

### 4. Normalize query input in `might_exist()` and `get_frequency()`

Add normalization to the query side to match the write-side tokenization:

- Lowercase the query string
- If the query is a single token (no whitespace), use it directly
- If the query contains multiple words, tokenize and check each — return True if ANY token matches (for `might_exist`), or return the min frequency across tokens (for `get_frequency`)

This ensures that `might_exist("Kubernetes")` matches `"kubernetes"` stored during save.

### 5. Update tests

See Test Impact section for specifics.

## No-Gos

- **Custom tokenizer parameter on the field.** Adds API complexity without clear use cases. Users who need custom tokenization should use `fingerprint_fn` to control input.
- **N-gram indexing.** Bigrams and trigrams would improve partial-match recall but dramatically increase the number of bloom entries per save, accelerating fill rate and degrading false positive rates. Out of scope.
- **Stemming or lemmatization.** Would require NLP dependencies (nltk, spacy). Too heavy for a Redis field type. Users can apply stemming in their `fingerprint_fn` if needed.
- **Backward compatibility migration.** Existing bloom filters with whole-string entries become stale after this change. This is acceptable: bloom filters are append-only probabilistic caches. The old entries waste some bits but do not cause false negatives. A `reset()` method could be added later to rebuild from scratch.

## Update System

No update system changes required — this is a library-internal bugfix in popoto. No deployment scripts, config files, or migration steps are affected.

## Agent Integration

No agent integration required — this is a fix to the popoto library itself. The ExistenceFilter field is used by downstream consumers (like the ai repo's SubconsciousMemory), but no MCP server changes or bridge modifications are needed. The fix is transparent to callers.

## Failure Path Test Strategy

1. **Empty fingerprint after tokenization:** If `fingerprint_fn` returns a string that tokenizes to zero tokens (e.g., "a is the"), the fallback to raw fingerprint insertion is tested.
2. **Unicode and special characters:** Fingerprints with emoji, CJK characters, or mixed scripts should not crash the tokenizer. Tokens that survive the min-length filter are indexed normally.
3. **Very long fingerprints:** A fingerprint with hundreds of words should tokenize without issues. The bloom filter fill rate is tested via the statistical test.
4. **Pipeline failures:** If Redis is unavailable, the existing error propagation path handles it. No new failure modes introduced.

## Test Impact

- [ ] `tests/test_existence_filter.py::TestExistenceFilterBasic::test_might_exist_after_save` — UPDATE: still passes (single-word "kubernetes" tokenizes to ["kubernetes"])
- [ ] `tests/test_existence_filter.py::TestExistenceFilterBasic::test_multiple_items` — UPDATE: still passes (single-word topics)
- [ ] `tests/test_existence_filter.py::TestExistenceFilterBasic::test_empty_string_fingerprint` — UPDATE: empty string tokenizes to [], fallback to raw "" insertion. Verify might_exist("") still returns True.
- [ ] `tests/test_existence_filter.py::TestExistenceFilterStatistical::test_false_positive_rate` — UPDATE: statistical properties change because each save now adds multiple tokens (for multi-word fingerprints) or one token (for single-word). The test uses single-word fingerprints (`"inserted-{i}"`), so token count per save is 1. The min-length filter might strip the "inserted-" prefix items since they are single tokens with hyphens. Verify the test still exercises the statistical guarantee correctly; may need to use longer unique tokens.
- [ ] `tests/test_existence_filter.py::TestExistenceFilterDefaultFingerprint::test_default_fingerprint_uses_redis_key` — UPDATE: redis keys contain colons, which tokenize differently. The raw fallback (when tokenization produces no tokens or the fingerprint looks like a key) must be tested. May need to add the raw fingerprint alongside tokens.

## Rabbit Holes

- **Optimal stop word list.** Do not spend time curating a perfect stop word list. A minimal set of the 20-30 most common English words is sufficient. The bloom filter tolerates a few extra entries from non-stop words that happen to be short.
- **Benchmarking tokenization overhead.** Tokenization is string splitting and filtering — microseconds compared to the Redis round-trip. Do not benchmark this.
- **Backward compat with existing bloom data.** Old bloom entries are harmless false positives. Do not build a migration tool.

## Tasks

### Phase 1: Core Fix

- [ ] Add `tokenize(text: str) -> list[str]` function to `existence_filter.py`
  - Lowercase, split on `\W+`, filter tokens < 3 chars, filter stop words, deduplicate
  - Include a `_STOP_WORDS` frozenset constant
- [ ] Add `BLOOM_ADD_MULTI_LUA` Lua script that accepts multiple tokens as ARGV
- [ ] Modify `ExistenceFilter.on_save()` to tokenize fingerprint and use multi-add Lua
  - Fallback: if tokenization produces empty list, add raw fingerprint
- [ ] Add `CMS_INCR_MULTI_LUA` Lua script for FrequencySketch multi-token increment
- [ ] Modify `FrequencySketch.on_save()` to tokenize fingerprint and use multi-incr Lua
- [ ] Normalize query input in `might_exist()` — lowercase the query string
- [ ] Normalize query input in `get_frequency()` — lowercase the query string

### Phase 2: Tests

- [ ] Add test: save multi-word fingerprint, query individual words — all return `might_exist() == True`
- [ ] Add test: save multi-word fingerprint, query word NOT in fingerprint — returns `definitely_missing() == True`
- [ ] Add test: case-insensitive query (save "Kubernetes", query "kubernetes") returns True
- [ ] Add test: stop words are excluded (save "the quick brown fox", query "the" returns False or True depending on fallback — document expected behavior)
- [ ] Add test: empty tokenization fallback (fingerprint that tokenizes to nothing falls back to raw)
- [ ] Add test: FrequencySketch multi-word tokenization — frequency of individual words matches save count
- [ ] Update existing `test_false_positive_rate` if needed (verify single-word fingerprints still work)
- [ ] Verify all existing tests pass without modification (single-word fingerprints are trivially tokenized)

### Phase 3: Documentation

- [ ] Update `docs/features/existence_filter.md` to document tokenization behavior
- [ ] Add note about word-level granularity in the field's docstring
- [ ] Document the stop word list and min-token-length in code comments

## Documentation

- [ ] Update `docs/features/existence_filter.md` — add section on tokenization behavior, explain that fingerprints are automatically tokenized on write, describe the stop word filtering and min-length threshold
- [ ] Update docstrings in `src/popoto/fields/existence_filter.py` for `on_save()`, `might_exist()`, and the module docstring to reflect tokenization
