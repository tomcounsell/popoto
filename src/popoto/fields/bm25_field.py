"""BM25Field: Ranked keyword search using BM25 scoring in Redis.

Maintains term frequency / document frequency statistics in Redis sorted sets
and computes BM25(k1=1.2, b=0.75) scores at query time via Lua scripts.
No Redis modules required -- works on both Redis and Valkey.

Design:
    BM25Field is a "side-effect field" like ExistenceFilter -- it does not store
    a value on the model instance. It maintains an inverted index and corpus
    statistics via on_save()/on_delete() hooks. At query time, a Lua script
    computes BM25 scores server-side and returns ranked results.

    Tokenization reuses the shared tokenizer from ``fields/_tokenizer.py``
    (same as ExistenceFilter): lowercase, split on non-word chars, filter
    short tokens, remove stop words.

Redis Key Patterns:
    - ``$BM25:{Class}:{field}:inv:{term}`` -- ZSET {doc_key: tf} (inverted index)
    - ``$BM25:{Class}:{field}:tf:{doc_key}`` -- ZSET {term: tf} (forward index)
    - ``$BM25:{Class}:{field}:df`` -- ZSET {term: df} (document frequency)
    - ``$BM25:{Class}:{field}:dl`` -- ZSET {doc_key: doc_length}
    - ``$BM25:{Class}:{field}:n`` -- STRING doc_count
    - ``$BM25:{Class}:{field}:avgdl`` -- STRING avg_doc_length

Example:
    class Memory(popoto.Model):
        key = popoto.AutoKeyField()
        raw_content = ContentField()
        content = BM25Field(source="raw_content")

    # After saving documents...
    results = BM25Field.search(Memory, "content", "redis deployment", limit=10)
    # Returns [(redis_key, bm25_score), ...]
"""

import logging
import math

from ._tokenizer import tokenize
from .field import Field
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger("POPOTO.BM25Field")

# ---------------------------------------------------------------------------
# Lua Scripts
# ---------------------------------------------------------------------------

BM25_SAVE_LUA = """
-- KEYS[1] = tf:{doc_key}   (forward index for this doc)
-- KEYS[2] = df              (corpus document frequency)
-- KEYS[3] = dl              (document lengths)
-- KEYS[4] = n               (total doc count)
-- KEYS[5] = avgdl           (average doc length)
-- ARGV[1] = doc_key
-- ARGV[2] = inv_prefix      (prefix for inverted index keys: $BM25:Class:field:inv:)
-- ARGV[3..N] = tokens

local tf_key = KEYS[1]
local df_key = KEYS[2]
local dl_key = KEYS[3]
local n_key  = KEYS[4]
local avgdl_key = KEYS[5]
local doc_key = ARGV[1]
local inv_prefix = ARGV[2]

-- Step 1: Remove old terms for this document (if updating)
local old_terms = redis.call('ZRANGEBYSCORE', tf_key, '-inf', '+inf')
local old_dl = tonumber(redis.call('ZSCORE', dl_key, doc_key)) or 0
local is_new = (old_dl == 0 and #old_terms == 0)

for i = 1, #old_terms do
    local term = old_terms[i]
    local old_tf = tonumber(redis.call('ZSCORE', tf_key, term)) or 0
    -- Remove from inverted index
    redis.call('ZREM', inv_prefix .. term, doc_key)
    -- Decrement df if no other docs have this term via inv index
    local remaining = redis.call('ZCARD', inv_prefix .. term)
    if remaining == 0 then
        redis.call('ZREM', df_key, term)
    else
        -- Recalculate df as count of docs with this term
        redis.call('ZADD', df_key, remaining, term)
    end
end
-- Clear old forward index
redis.call('DEL', tf_key)

-- Step 2: Compute new term frequencies
local tokens = {}
for i = 3, #ARGV do
    tokens[#tokens + 1] = ARGV[i]
end

if #tokens == 0 then
    -- Empty content: remove doc from dl, potentially decrement n
    redis.call('ZREM', dl_key, doc_key)
    if not is_new then
        local current_n = tonumber(redis.call('GET', n_key)) or 0
        if current_n > 0 then
            redis.call('SET', n_key, current_n - 1)
        end
        -- Recompute avgdl
        local new_n = tonumber(redis.call('GET', n_key)) or 0
        if new_n > 0 then
            local total_dl = 0
            local all_dl = redis.call('ZRANGEBYSCORE', dl_key, '-inf', '+inf', 'WITHSCORES')
            for j = 2, #all_dl, 2 do
                total_dl = total_dl + tonumber(all_dl[j])
            end
            redis.call('SET', avgdl_key, tostring(total_dl / new_n))
        else
            redis.call('SET', avgdl_key, '0')
        end
    end
    return 0
end

local tf = {}
for i = 1, #tokens do
    local t = tokens[i]
    tf[t] = (tf[t] or 0) + 1
end

-- Step 3: Write new forward index and inverted index
local new_dl = #tokens
for term, count in pairs(tf) do
    redis.call('ZADD', tf_key, count, term)
    redis.call('ZADD', inv_prefix .. term, count, doc_key)
    -- Update df: count of docs with this term
    local inv_card = redis.call('ZCARD', inv_prefix .. term)
    redis.call('ZADD', df_key, inv_card, term)
end

-- Step 4: Update dl
redis.call('ZADD', dl_key, new_dl, doc_key)

-- Step 5: Update n
local current_n = tonumber(redis.call('GET', n_key)) or 0
if is_new then
    current_n = current_n + 1
    redis.call('SET', n_key, current_n)
end

-- Step 6: Recompute avgdl incrementally
if current_n > 0 then
    local old_avgdl = tonumber(redis.call('GET', avgdl_key)) or 0
    local new_avgdl
    if is_new then
        new_avgdl = ((old_avgdl * (current_n - 1)) + new_dl) / current_n
    else
        -- Update: replace old_dl with new_dl in the average
        new_avgdl = ((old_avgdl * current_n) - old_dl + new_dl) / current_n
    end
    redis.call('SET', avgdl_key, tostring(new_avgdl))
end

return 1
"""

BM25_DELETE_LUA = """
-- KEYS[1] = tf:{doc_key}   (forward index for this doc)
-- KEYS[2] = df              (corpus document frequency)
-- KEYS[3] = dl              (document lengths)
-- KEYS[4] = n               (total doc count)
-- KEYS[5] = avgdl           (average doc length)
-- ARGV[1] = doc_key
-- ARGV[2] = inv_prefix      (prefix for inverted index keys)

local tf_key = KEYS[1]
local df_key = KEYS[2]
local dl_key = KEYS[3]
local n_key  = KEYS[4]
local avgdl_key = KEYS[5]
local doc_key = ARGV[1]
local inv_prefix = ARGV[2]

-- Check if doc exists in the index
local doc_dl = tonumber(redis.call('ZSCORE', dl_key, doc_key))
if not doc_dl then
    return 0  -- doc not in index, nothing to do
end

-- Step 1: Remove terms from inverted index and update df
local old_terms = redis.call('ZRANGEBYSCORE', tf_key, '-inf', '+inf')
for i = 1, #old_terms do
    local term = old_terms[i]
    redis.call('ZREM', inv_prefix .. term, doc_key)
    local remaining = redis.call('ZCARD', inv_prefix .. term)
    if remaining == 0 then
        redis.call('ZREM', df_key, term)
    else
        redis.call('ZADD', df_key, remaining, term)
    end
end

-- Step 2: Remove forward index
redis.call('DEL', tf_key)

-- Step 3: Remove from dl
redis.call('ZREM', dl_key, doc_key)

-- Step 4: Decrement n
local current_n = tonumber(redis.call('GET', n_key)) or 0
if current_n > 0 then
    current_n = current_n - 1
    redis.call('SET', n_key, current_n)
end

-- Step 5: Recompute avgdl
if current_n > 0 then
    local old_avgdl = tonumber(redis.call('GET', avgdl_key)) or 0
    local new_avgdl = ((old_avgdl * (current_n + 1)) - doc_dl) / current_n
    redis.call('SET', avgdl_key, tostring(new_avgdl))
else
    redis.call('SET', avgdl_key, '0')
end

return 1
"""

BM25_SEARCH_LUA = """
-- KEYS[1] = df              (corpus document frequency)
-- KEYS[2] = dl              (document lengths)
-- KEYS[3] = n               (total doc count)
-- KEYS[4] = avgdl           (average doc length)
-- ARGV[1] = inv_prefix      (prefix for inverted index keys)
-- ARGV[2] = limit
-- ARGV[3] = k1
-- ARGV[4] = b
-- ARGV[5..N] = query terms

local df_key = KEYS[1]
local dl_key = KEYS[2]
local n_key  = KEYS[3]
local avgdl_key = KEYS[4]
local inv_prefix = ARGV[1]
local search_limit = tonumber(ARGV[2])
local k1 = tonumber(ARGV[3])
local b  = tonumber(ARGV[4])

-- Read corpus stats
local N = tonumber(redis.call('GET', n_key)) or 0
if N == 0 then
    return {}
end
local avgdl = tonumber(redis.call('GET', avgdl_key)) or 1

-- Collect query terms
local query_terms = {}
for i = 5, #ARGV do
    query_terms[#query_terms + 1] = ARGV[i]
end

if #query_terms == 0 then
    return {}
end

-- Score accumulator: doc_key -> score
local scores = {}
local doc_keys_set = {}

for _, term in ipairs(query_terms) do
    local df = tonumber(redis.call('ZSCORE', df_key, term)) or 0
    if df > 0 then
        -- IDF: log((N - df + 0.5) / (df + 0.5) + 1)
        local idf = math.log((N - df + 0.5) / (df + 0.5) + 1)

        -- Get all docs containing this term from the inverted index
        local inv_results = redis.call('ZRANGEBYSCORE', inv_prefix .. term, '-inf', '+inf', 'WITHSCORES')
        for j = 1, #inv_results, 2 do
            local dkey = inv_results[j]
            local tf = tonumber(inv_results[j + 1])
            local dl = tonumber(redis.call('ZSCORE', dl_key, dkey)) or 1

            -- BM25 term score: idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl/avgdl))
            local tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
            local term_score = idf * tf_norm

            scores[dkey] = (scores[dkey] or 0) + term_score
            doc_keys_set[dkey] = true
        end
    end
end

-- Collect and sort by score descending
local results = {}
for dkey, score in pairs(scores) do
    results[#results + 1] = {dkey, score}
end

table.sort(results, function(a, b) return a[2] > b[2] end)

-- Return top-K as flat array: [key1, score1, key2, score2, ...]
local output = {}
local count = math.min(search_limit, #results)
for i = 1, count do
    output[#output + 1] = results[i][1]
    output[#output + 1] = tostring(results[i][2])
end

return output
"""


class BM25Field(Field):
    """BM25 ranked keyword search field backed by Redis sorted sets.

    Maintains an inverted index and corpus statistics (tf, df, dl, n, avgdl)
    in Redis. Computes BM25 scores at query time via a Lua script.

    This is a "side-effect field" -- it does not store a value on the model
    instance. It reads content from a ``source`` field and maintains search
    indexes via on_save()/on_delete() hooks.

    Args:
        source: Name of the field to read content from for indexing.
            Required -- the source field should contain text content.
        **kwargs: Standard Field keyword arguments.

    Class Constants:
        BM25_K1: Term frequency saturation parameter. Default 1.2.
        BM25_B: Document length normalization parameter. Default 0.75.

    Redis Keys:
        - ``$BM25:{Class}:{field}:inv:{term}`` -- inverted index per term
        - ``$BM25:{Class}:{field}:tf:{doc_key}`` -- forward index per doc
        - ``$BM25:{Class}:{field}:df`` -- document frequency
        - ``$BM25:{Class}:{field}:dl`` -- document lengths
        - ``$BM25:{Class}:{field}:n`` -- total document count
        - ``$BM25:{Class}:{field}:avgdl`` -- average document length

    Example:
        class Memory(popoto.Model):
            key = popoto.AutoKeyField()
            raw_content = ContentField()
            content = BM25Field(source="raw_content")

        # Save some documents
        m = Memory(raw_content="kubernetes deployment guide")
        m.save()

        # Ranked keyword search
        results = BM25Field.search(Memory, "content", "kubernetes", limit=10)
        # Returns [(redis_key, bm25_score), ...]
    """

    # BM25 tuning parameters -- override via subclass for experimentation
    BM25_K1 = 1.2  # Term frequency saturation
    BM25_B = 0.75  # Document length normalization

    # Override Field defaults -- BM25Field does not store a value
    type: type = str
    null: bool = True
    default = None

    def __init__(self, source: str = None, **kwargs):
        if source is None:
            raise ValueError("BM25Field requires a 'source' parameter")
        self.source = source
        super().__init__(**kwargs)

    def _key_prefix(self, model_class):
        """Build the Redis key prefix for this BM25Field's data structures.

        Returns:
            str: Prefix like ``$BM25:{ClassName}:{field_name}``
        """
        return f"$BM25:{model_class.__name__}:{self.name}"

    @classmethod
    def on_save(
        cls,
        model_instance,
        field_name,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """Update BM25 index when a model instance is saved.

        Reads the source field value, tokenizes it, and atomically updates
        all BM25 data structures (tf, df, dl, n, avgdl, inverted index)
        via a single Lua script.

        Args:
            model_instance: The model instance being saved.
            field_name: Name of this field on the model.
            field_value: Current field value (unused -- BM25Field is side-effect only).
            pipeline: Optional Redis pipeline (not used for Lua eval).
            **kwargs: Additional context.

        Returns:
            The pipeline if provided, otherwise None.
        """
        field = model_instance._meta.fields[field_name]
        if not isinstance(field, BM25Field):
            return pipeline if pipeline else None

        model_class = type(model_instance)
        prefix = field._key_prefix(model_class)

        # Read source field content
        source_value = getattr(model_instance, field.source, None)
        if source_value is None:
            source_value = ""

        # Handle ContentField references
        if isinstance(source_value, str) and source_value.startswith("$CF:"):
            source_field = model_instance._meta.fields.get(field.source)
            if hasattr(source_field, "store"):
                try:
                    source_value = source_field.store.read(source_value)
                except Exception:
                    source_value = ""

        source_value = str(source_value)
        # Use unique=False to preserve raw term counts for accurate tf
        tokens = tokenize(source_value, unique=False)

        # Get the document's Redis key
        doc_key = model_instance.db_key.redis_key

        # Build Lua KEYS and ARGV
        tf_key = f"{prefix}:tf:{doc_key}"
        df_key = f"{prefix}:df"
        dl_key = f"{prefix}:dl"
        n_key = f"{prefix}:n"
        avgdl_key = f"{prefix}:avgdl"
        inv_prefix = f"{prefix}:inv:"

        keys = [tf_key, df_key, dl_key, n_key, avgdl_key]
        argv = [doc_key, inv_prefix] + tokens

        POPOTO_REDIS_DB.eval(BM25_SAVE_LUA, len(keys), *keys, *argv)

        return pipeline if pipeline else None

    @classmethod
    def on_delete(
        cls,
        model_instance,
        field_name,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """Remove a document from the BM25 index when deleted.

        Atomically reverses the save operation: removes terms from the
        inverted index, updates df, removes dl entry, decrements n,
        and recomputes avgdl.

        Args:
            model_instance: The model instance being deleted.
            field_name: Name of this field on the model.
            field_value: Current field value (unused).
            pipeline: Optional Redis pipeline (not used for Lua eval).
            **kwargs: Additional context.

        Returns:
            The pipeline if provided, otherwise None.
        """
        field = model_instance._meta.fields[field_name]
        if not isinstance(field, BM25Field):
            return pipeline if pipeline else None

        model_class = type(model_instance)
        prefix = field._key_prefix(model_class)
        doc_key = model_instance.db_key.redis_key

        tf_key = f"{prefix}:tf:{doc_key}"
        df_key = f"{prefix}:df"
        dl_key = f"{prefix}:dl"
        n_key = f"{prefix}:n"
        avgdl_key = f"{prefix}:avgdl"
        inv_prefix = f"{prefix}:inv:"

        keys = [tf_key, df_key, dl_key, n_key, avgdl_key]
        argv = [doc_key, inv_prefix]

        POPOTO_REDIS_DB.eval(BM25_DELETE_LUA, len(keys), *keys, *argv)

        return pipeline if pipeline else None

    @classmethod
    def search(cls, model_class, field_name, query_text, limit=10):
        """Search the BM25 index and return ranked results.

        Tokenizes the query, executes the BM25 scoring Lua script, and
        returns results sorted by BM25 score descending.

        Args:
            model_class: The Model class to search.
            field_name: Name of the BM25Field on the model.
            query_text: The search query string.
            limit: Maximum number of results to return. Default 10.

        Returns:
            list[tuple[str, float]]: List of (redis_key, bm25_score) tuples
                sorted by score descending. Returns empty list if query
                produces no tokens or corpus is empty.

        Raises:
            QueryException: If field_name does not refer to a BM25Field.
        """
        from ..models.query import QueryException

        field = model_class._meta.fields.get(field_name)
        if not isinstance(field, BM25Field):
            raise QueryException(
                f"keyword_search() requires a BM25Field. "
                f"'{field_name}' is {type(field).__name__ if field else 'not found'}"
            )

        query_tokens = tokenize(query_text or "")
        if not query_tokens:
            return []

        prefix = field._key_prefix(model_class)
        df_key = f"{prefix}:df"
        dl_key = f"{prefix}:dl"
        n_key = f"{prefix}:n"
        avgdl_key = f"{prefix}:avgdl"
        inv_prefix = f"{prefix}:inv:"

        keys = [df_key, dl_key, n_key, avgdl_key]
        argv = [inv_prefix, limit, field.BM25_K1, field.BM25_B] + query_tokens

        result = POPOTO_REDIS_DB.eval(BM25_SEARCH_LUA, len(keys), *keys, *argv)

        if not result:
            return []

        # Parse flat array: [key1, score1, key2, score2, ...]
        scored = []
        for i in range(0, len(result), 2):
            key = result[i]
            score = result[i + 1]
            if isinstance(key, bytes):
                key = key.decode()
            if isinstance(score, bytes):
                score = score.decode()
            scored.append((key, float(score)))

        return scored

    @classmethod
    def recompute_stats(cls, model_class, field_name):
        """Recompute avgdl from scratch to correct floating-point drift.

        Reads all document lengths from the dl sorted set and recomputes
        the average. Also verifies n matches the actual document count.

        Args:
            model_class: The Model class.
            field_name: Name of the BM25Field on the model.
        """
        field = model_class._meta.fields.get(field_name)
        if not isinstance(field, BM25Field):
            return

        prefix = field._key_prefix(model_class)
        dl_key = f"{prefix}:dl"
        n_key = f"{prefix}:n"
        avgdl_key = f"{prefix}:avgdl"

        # Get all document lengths
        all_dl = POPOTO_REDIS_DB.zrangebyscore(dl_key, "-inf", "+inf", withscores=True)

        actual_n = len(all_dl)
        total_dl = sum(score for _, score in all_dl)

        POPOTO_REDIS_DB.set(n_key, str(actual_n))
        if actual_n > 0:
            POPOTO_REDIS_DB.set(avgdl_key, str(total_dl / actual_n))
        else:
            POPOTO_REDIS_DB.set(avgdl_key, "0")

    @classmethod
    def get_idf(cls, model_class, field_name, tokens):
        """Get IDF scores for tokens without running a full search.

        Reads document frequency from the existing BM25 df sorted set
        and total doc count. Computes standard BM25 IDF:
            idf = log((N - df + 0.5) / (df + 0.5) + 1)

        Uses ZMSCORE (Redis >= 6.2, Valkey compatible) for batch df lookup.
        Falls back to individual ZSCORE calls if ZMSCORE is unavailable.

        Args:
            model_class: The Model class.
            field_name: Name of the BM25Field.
            tokens: Single token string or list of token strings.

        Returns:
            dict[str, float]: Mapping of token -> IDF score. Tokens not in
                the corpus get maximum IDF (log(N + 1) when df=0).
                Returns empty dict for empty token list.
                Returns {token: 0.0} for all tokens when corpus is empty (N=0).
        """
        field = model_class._meta.fields.get(field_name)
        if not isinstance(field, BM25Field):
            from ..models.query import QueryException

            raise QueryException(
                f"get_idf() requires a BM25Field. "
                f"'{field_name}' is "
                f"{type(field).__name__ if field else 'not found'}"
            )

        # Normalize single token to list
        if isinstance(tokens, str):
            tokens = [tokens]
        if not tokens:
            return {}

        prefix = field._key_prefix(model_class)
        n_key = f"{prefix}:n"
        df_key = f"{prefix}:df"

        # Read total doc count
        n_raw = POPOTO_REDIS_DB.get(n_key)
        N = int(n_raw) if n_raw else 0

        if N == 0:
            return {token: 0.0 for token in tokens}

        # Batch df lookup via ZMSCORE (Redis >= 6.2) with ZSCORE fallback
        df_values = cls._batch_zscore(df_key, tokens)

        # Compute IDF for each token
        result = {}
        for token, df_raw in zip(tokens, df_values):
            df = float(df_raw) if df_raw is not None else 0.0
            idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
            result[token] = idf

        return result

    @classmethod
    def _batch_zscore(cls, key, members):
        """Batch-read sorted set scores using ZMSCORE with ZSCORE fallback.

        ZMSCORE was added in Redis 6.2 and is supported by Valkey.
        Falls back to individual ZSCORE calls if ZMSCORE is unavailable.

        Args:
            key: Redis sorted set key.
            members: List of member strings to look up.

        Returns:
            list: Scores for each member (None if member not in set).
        """
        try:
            return POPOTO_REDIS_DB.zmscore(key, members)
        except (AttributeError, Exception):
            # ZMSCORE not available -- fall back to individual ZSCORE
            return [POPOTO_REDIS_DB.zscore(key, m) for m in members]

    @classmethod
    def filter_selective_tokens(
        cls, model_class, field_name, tokens, min_idf=1.0
    ):
        """Filter tokens to only those with IDF above a threshold.

        Useful for pre-filtering keywords before running search().
        Tokens not in the corpus are considered maximally selective
        and included.

        Args:
            model_class: The Model class.
            field_name: Name of the BM25Field.
            tokens: List of token strings.
            min_idf: Minimum IDF score to keep. Default 1.0.

        Returns:
            list[str]: Tokens with IDF >= min_idf, preserving original order.
        """
        if not tokens:
            return []

        idf_scores = cls.get_idf(model_class, field_name, tokens)
        return [t for t in tokens if idf_scores.get(t, 0.0) >= min_idf]
