# ContextAssembler

Retrieval-to-injection bridge — assembles LLM-ready context within token budgets by orchestrating pull-path (query-driven) and push-path (proactive surfacing) retrieval across all Popoto memory primitives.

## Overview

`ContextAssembler` provides a single `assemble()` call that:

1. **Pull path**: ExistenceFilter pre-check → retrieval → CoOccurrence propagation
2. **Push path**: CyclicDecayField temporal scan above surfacing threshold
3. **Merge**: Deduplicate, re-rank, budget-select, post-effects, format

### Pull Path Modes (`retrieval_mode`)

The pull path supports four modes controlled by the `retrieval_mode` constructor parameter:

| Mode | Behaviour | When to use |
|------|-----------|-------------|
| `"auto"` *(default)* | Detects fields on the model: both `BM25Field` + `EmbeddingField` → `"hybrid"`; `BM25Field` only → `"lexical"`; neither → `"composite"` | Most callers — no configuration needed |
| `"lexical"` | BM25 + graph propagation (no embeddings, no numpy required) fused via RRF (k=60) | Models with `BM25Field` but no `EmbeddingField`; zero-dep query-sensitive retrieval |
| `"hybrid"` | BM25 (lexical) + vector (semantic) + graph fused via RRF (k=60) | Models with both `BM25Field` and `EmbeddingField` configured |
| `"composite"` | Original `CompositeScoreQuery` weighted-sum path — **query-blind** (takes no query-text input; ranks by score indexes only) | Backwards-compatible override; when `score_weights` drive all ranking |

**Emergent mode under `"auto"`:** the effective retrieval mode is determined by which fields are declared on the model at init time. Declaring or removing `BM25Field`/`EmbeddingField` changes the effective mode without any change at the `ContextAssembler` call site. For example: adding `EmbeddingField` to a BM25-only model silently flips `lexical → hybrid`.

**Zero-hit fallback:** when BM25 returns zero matches (e.g. a query with no lexical overlap with the corpus), the lexical and hybrid paths degrade to `"composite"` ranking — query-blind, but no crash. Document this at your call site if queries are expected to have no keyword overlap.

`retrieval_mode="lexical"` raises `QueryException` at init if `BM25Field` is absent. `retrieval_mode="hybrid"` raises `QueryException` at init if `BM25Field` or `EmbeddingField` is absent. Any unrecognised mode string raises `QueryException` immediately.

```python
# Lexical mode — auto-detected when only BM25Field is on the model (no EmbeddingField)
assembler = ContextAssembler(
    model_class=Memory,  # has BM25Field, no EmbeddingField
    score_weights={"relevance": 0.6},  # ignored in lexical pull path
    max_items=10,
)
# retrieval_mode defaults to "auto"; resolves to "lexical" (BM25 + graph, no embeddings)

# Hybrid mode — auto-detected when BM25Field + EmbeddingField are on the model
assembler = ContextAssembler(
    model_class=Memory,  # has BM25Field AND EmbeddingField
    score_weights={"relevance": 0.6},  # ignored in hybrid pull path
    max_items=10,
)
# retrieval_mode defaults to "auto"; resolves to "hybrid"

# Force composite path (pre-v1.7 behaviour, query-blind)
assembler = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 0.6, "confidence": 0.3},
    retrieval_mode="composite",
)

# Force lexical explicitly (raises QueryException if BM25Field absent)
assembler = ContextAssembler(
    model_class=Memory,
    score_weights={},
    retrieval_mode="lexical",
)

# Force hybrid explicitly (raises QueryException if either field absent)
assembler = ContextAssembler(
    model_class=Memory,
    score_weights={},
    retrieval_mode="hybrid",
)
```

## Primitive Synergy

| Primitive | Role in ContextAssembler |
|-----------|--------------------------|
| DecayingSortedField | Score index for CompositeScoreQuery |
| CyclicDecayField | Push-path proactive surfacing |
| ConfidenceField | Score index + competitive suppression |
| CoOccurrenceField | Pull-path graph expansion (both paths) |
| ExistenceFilter | Pull-path pre-check (skip if absent) |
| BM25Field | Lexical and hybrid pull-paths: keyword-search signal for RRF (BM25 + graph, no embeddings) |
| EmbeddingField | Hybrid pull-path: vector signal for RRF |
| AccessTrackerMixin | on_read post-effect tracking |
| ObservationProtocol | on_read / on_surfaced dispatch |
| RecallProposal | Created for push-path records |
| WriteFilterMixin | Priority score in composite |
| EventStreamMixin | Mutation logging (via model save) |
| PredictionLedgerMixin | Outcome tracking (via model save) |
| CompositeScoreQuery | Multi-factor ranked retrieval (composite mode) |

## Usage

```python
from popoto.recipes.context_assembler import ContextAssembler

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

# result.records — selected instances
# result.proactive — push-path subset
# result.formatted — LLM-ready string
# result.metadata — scores, timing, token counts
```

### AssemblyResult

The `assemble()` call returns an `AssemblyResult` dataclass:

| Field | Type | Description |
|-------|------|-------------|
| `records` | `list` | Selected model instances, ranked |
| `proactive` | `list` | Subset of records from push-path |
| `formatted` | `str` | LLM-ready formatted string |
| `metadata` | `dict` | Scores, timing, token counts |

## Tuning Constants

```python
from popoto.fields.constants import Defaults
```

| Constant | Default | Optimal Range | Description |
|----------|---------|---------------|-------------|
| `COMPETITIVE_SUPPRESSION_SIGNAL` | 0.3 | [0.1, 0.7] | Signal for suppressing non-selected pull-path candidates |
| `DEFAULT_SURFACING_THRESHOLD` | 0.5 | [0.1, 0.9] | Minimum score for push-path records |

Additional non-tunable defaults:

| Constant | Default | Description |
|----------|---------|-------------|
| `DEFAULT_MAX_ITEMS` | 10 | Maximum records returned |
| `DEFAULT_PROPAGATION_DEPTH` | 2 | BFS depth for CoOccurrence propagation |

## Pipeline Details

### Pull Path — Composite mode

1. **ExistenceFilter pre-check**: Skip query entirely if no matching topics exist (O(1)).
2. **CompositeScoreQuery**: Multi-factor ranked retrieval combining decay scores, confidence, and priority weights.
3. **CoOccurrence propagation**: BFS expansion from seed records to find associatively related memories.

### Pull Path — Hybrid mode (`"hybrid"` or auto-detected)

1. **ExistenceFilter pre-check**: Same short-circuit as composite path.
2. **BM25 lexical retrieval**: `BM25Field.search(query_text, limit=max_items×5)` — scored keyword matches.
3. **Vector retrieval**: `QueryBuilder._get_vector_scores(query_text, limit=max_items×5)` — cosine similarity via configured embedding provider.
4. **CoOccurrence graph expansion**: BFS from BM25 top-5 seeds (optional, requires `CoOccurrenceField`).
5. **RRF fusion**: `query.fuse(keyword=..., vector=..., graph=..., k=60, limit=max_items×2)` — rank-based fusion.

If both BM25 and vector signals return empty results, the path falls back to the composite path automatically.

### Push Path

1. **CyclicDecayField scan**: Find records whose cyclic + pressure score exceeds `DEFAULT_SURFACING_THRESHOLD`.
2. **RecallProposal creation**: Track surfaced records via `ObservationProtocol.on_surfaced()`.

### Merge and Budget

1. **Deduplicate**: Records appearing in both paths are kept once.
2. **Re-rank**: Combined score from both paths.
3. **Budget-select**: Fit within `max_items` and `max_tokens` constraints. See [Token Budget Semantics](#token-budget-semantics) for the exact packing rules and counter contract.
4. **Post-effects**: Fire `ObservationProtocol.on_read()` for selected records.
5. **Competitive suppression**: Non-selected pull-path candidates receive a mild contradiction signal via ConfidenceField.

## Token Budget Semantics

`max_tokens` is enforced against the serialized text that is actually emitted to the LLM — not against a proxy like the Redis key or `str(record)`.

### Counter contract

`token_counter` receives one argument: the serialized per-record string for the active `output_format` (the exact slice the formatter emits — JSON object indented inside the array, `<record>...</record>` block, or `key: value` line). It must return a non-negative `int`.

```python
# Correct contract — text is the serialized record string
token_counter=lambda text: len(enc.encode(text))

# Old contract — do NOT use (tokenizes the Redis key, not the content)
# token_counter=lambda record: len(enc.encode(str(record)))  # broken
```

Supplying a callable that raises `TypeError` or `AttributeError` when called with a string (the signature of an old-contract `callable(record)` counter) triggers a `DeprecationWarning` at construction time and falls back to the stdlib heuristic on every call.

### Default heuristic (`_estimate_tokens`)

When no `token_counter` is supplied, `ContextAssembler` uses a zero-dependency escape-aware character-class heuristic (spike-1) that operates on the serialized string. It handles `json.dumps` `ensure_ascii=True` output (which converts all non-ASCII content to `\uXXXX` hex escapes) by counting escapes as whole units rather than individual characters.

Measured accuracy vs tiktoken cl100k_base over the `json.dumps`-formatted envelope:

| Content type | Error vs cl100k_base |
|---|---|
| English prose | +20.3% (overestimate) |
| Code | +20.6% (overestimate) |
| CJK | +4.5% (overestimate) |
| URLs / hashes | −15.0% (underestimate) |
| Emoji | −1.1% (underestimate, negligible) |

All errors are overestimates — the safe direction for budget enforcement (underestimates let more content through than intended) — except URL/hash-heavy content (−15.0%, the worst-case underestimate) and emoji (−1.1%, negligible).

For hard budget requirements or URL/hash-heavy memory stores, supply a real tokenizer via `token_counter` and/or set `max_tokens` with a safety margin (for example, 85% of your model's true context limit).

### Packing semantics: skip-not-break

Budget selection is greedy first-fit in rank order with **skip-not-break** behaviour: a record that does not fit within the remaining budget is skipped, and the loop continues to evaluate later (potentially smaller) records. Admitted records therefore need not form a strict rank-prefix of the candidate list.

**First-record guarantee**: The first record is always admitted regardless of its token count. This prevents `assemble()` from returning zero records when candidates exist. The tradeoff is that a single oversized record can overshoot the budget; the actual token count is always visible in `metadata["token_count"]`.

### Wrapper framing exclusion

Wrapper framing (JSON array brackets `[...]`, `<records>...</records>` envelope, enumeration prefixes in natural format) is excluded from per-record token counting. This residual is a fixed handful of tokens per assembly — less than 20 tokens per format, independent of record count or size — and is asserted by golden composition tests.

`metadata["token_count"]` reflects the serialized per-record content actually emitted. It does not include the wrapper framing residual.

### Hard-budget recommendations

- **Use a real tokenizer** for strict context-limit compliance: `token_counter=lambda text: len(enc.encode(text))` where `enc = tiktoken.encoding_for_model("gpt-4")`.
- **Apply a safety margin** when using the default heuristic, especially with URL/hash-heavy memories: set `max_tokens` to 85% of your model's true limit.
- **Check `metadata["token_count"]`** after assembly to confirm actual usage.

!!! warning "Upgrading from earlier versions"
    `max_tokens` is now enforced for real. If you set a `max_tokens` budget before this fix, you will receive **fewer records per assembly** than you did previously — the old counter was measuring the Redis key (typically 12–14 "tokens" per record regardless of content size), so any budget above `max_items × ~14` never engaged.

    **Action required:** audit your `max_tokens` values and raise them if needed. A budget of 4,000 previously admitted everything `max_items` allowed; to replicate that behaviour, either remove the budget or set it generously above your expected content size.

    Old-contract `callable(record)` counters trigger a `DeprecationWarning` at construction and fall back to the stdlib heuristic at call time. Update them to `callable(text: str) -> int`.

## LLM Integration

Wire assembled context into an LLM call using the OpenAI SDK v1+:

```python
from openai import OpenAI
from popoto import ContextAssembler, ObservationProtocol

client = OpenAI()  # uses OPENAI_API_KEY env var

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

# Build messages with injected memory context
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

## Retrieval Quality Scoring

To score the quality of a retrieval — avg confidence, feeling-of-knowing, score spread, staleness — pass `assess_quality=True` to `assemble()` or call the standalone `assess()` probe before retrieval:

```python
# Pre-retrieval probe (cheap — no propagation, no push path)
quality = assembler.assess({"topic": "deployment"})
if quality.fok_score < 0.3:
    return  # skip retrieval; memory store has nothing relevant

# Post-retrieval quality attached to metadata
result = assembler.assemble({"topic": "deployment"}, assess_quality=True)
quality = result.metadata["quality"]  # RetrievalQuality dataclass
print(quality.avg_confidence, quality.fok_score)
```

See [Metacognitive Layer](metacognitive-layer.md) for full documentation of `RetrievalQuality`, all four metrics, the `assess()` method, and the `AdaptiveAssembler` keep/revert loop.

## See Also

- [Metacognitive Layer](metacognitive-layer.md) — retrieval quality scoring, FOK, and adaptive weight tuning
- [PolicyCache](policy-cache.md) — learned action selection (uses ContextAssembler for retrieval)
- [Hybrid Retrieval](hybrid-retrieval.md) — BM25Field, EmbeddingField, and RRF fusion primitives
- [CompositeScoreQuery](composite-score-query.md) — multi-factor retrieval (composite mode)
- [CoOccurrenceField](co-occurrence-field.md) — associative expansion
- [Agent Memory overview](agent-memory.md) — full primitives reference
- [Subconscious Memory Recipe](../guides/subconscious-memory-recipe.md) — automatic memory injection and extraction around LLM turns
