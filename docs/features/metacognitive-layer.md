# Metacognitive Memory Layer

Retrieval self-assessment for agent memory — surfaces mechanical quality signals so agents can decide whether to trust their context, retry with different cues, or caveat their answers.

## What it is

The metacognitive layer adds three opt-in capabilities on top of the existing `ContextAssembler` pipeline. Tier 1 introduces `RetrievalQuality`, a dataclass describing how trustworthy a retrieval is across four dimensions (avg confidence, score spread, feeling-of-knowing, and staleness). Tier 2 extends `ObservationProtocol` with a `"used"` outcome that records confirmed reads without strengthening memory signals, and adds `PredictionLedgerMixin.error_summary()` for systematic bias detection. Tier 3 wraps a `ContextAssembler` in an `AdaptiveAssembler` that adjusts `score_weights` online via a keep/revert loop. All signals are mechanical — no LLM self-reporting.

## When to use

- You want to know before (or after) a retrieval whether the context is trustworthy enough to act on.
- You want to skip an expensive full retrieval when the memory store has nothing relevant.
- You want to record that an agent consumed a memory (for access accounting) without overcounting it as an "acted" signal.
- You want to find systematic patterns in prediction errors (which times of day, which task types are consistently wrong).
- You want `score_weights` to adapt automatically as the retrieval environment shifts over long agent sessions.

---

## Tier 1: `RetrievalQuality` + `assess()` + `assess_quality=True`

### `RetrievalQuality` dataclass

```python
from popoto.recipes import RetrievalQuality
# or: from popoto import RetrievalQuality
```

| Field | Type | Meaning |
|-------|------|---------|
| `avg_confidence` | `float` | Mean `ConfidenceField.get_confidence()` across selected records. `1.0` when the model has no `ConfidenceField` — "no evidence against the retrieval." |
| `score_spread` | `float` | Coefficient of variation (`stddev / mean`) of per-record composite scores. High spread: one record dominates. Low spread: results are roughly equivalent. `0.0` when `abs(mean) < 1e-9`. |
| `fok_score` | `float` | Feeling-of-knowing, `0.0–1.0`. Formula: `0.4 * cue_familiarity + 0.4 * partial_retrieval_count + 0.2 * subthreshold_activation`, averaged across query cues. `0.0` when no cues were provided. |
| `staleness_ratio` | `float` | Fraction of selected records whose decayed `DecayingSortedField` relevance falls below the surfacing threshold. `0.0` when the model has no `DecayingSortedField`. |
| `score_distribution` | `list[float]` | Full list of per-record scores for histogram analysis. Empty when unavailable. |
| `per_cue_fok` | `dict` | Maps cue value → `{cue_familiarity, partial_retrieval_count, subthreshold_activation, component_score}` for debugging. |

> **Partition- and decay-aware scoring.** The per-record score signals (`score_spread`, `score_distribution`, `staleness_ratio`, and the FOK `subthreshold_activation` component) are read from the **partition-specific** sorted-set index, so they are correct for models whose sorted fields declare `partition_by` (the common agent-memory case). For `DecayingSortedField` / `CyclicDecayField`, whose index stores a last-updated timestamp rather than relevance, the signals use the field's **decayed** score (the same value `Query.top_by_decay()` surfaces), not the raw timestamp.

**FOK components:**

- `cue_familiarity` — `1.0` if `ExistenceFilter.might_exist()` says the cue is probably present; `0.0` if definitely absent; `0.5` neutral when no `ExistenceFilter` is configured.
- `partial_retrieval_count` — `min(len(pull_candidates), max_items) / max_items`. Did enough candidates surface to have something to pick from?
- `subthreshold_activation` — fraction of pull candidates with score strictly between 0 and `surfacing_threshold`. "Almost-remembered" content that didn't make the cut.

### `ContextAssembler.assess()` — pre-retrieval FOK probe

Call `assess()` before `assemble()` to check whether the full retrieval is worth the round-trip cost. It runs only an `ExistenceFilter` check and a low-limit `composite_score` probe — no `CoOccurrence` propagation, no push path, no post-effects.

```python
from popoto.recipes.context_assembler import ContextAssembler

assembler = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 0.6, "confidence": 0.3, "recency": 0.1},
    max_items=10,
    max_tokens=4000,
)

quality = assembler.assess({"topic": "kubernetes deployment"})

print(quality.fok_score)       # e.g. 0.72
print(quality.avg_confidence)  # e.g. 0.81

if quality.fok_score < 0.3:
    # Memory store doesn't know this domain; skip the expensive retrieval
    return "I don't have relevant context on that topic."

result = assembler.assemble({"topic": "kubernetes deployment"})
```

`assess()` signature:

```python
ContextAssembler.assess(
    query_cues: dict | None = None,
    partition_filters: dict | None = None,
    probe_limit: int | None = None,  # defaults to max_items
) -> RetrievalQuality
```

Returns `RetrievalQuality` with all metrics set to `0.0` and a logged warning when `query_cues` is empty or `None`.

### `assemble(assess_quality=True)` — post-retrieval quality

To get quality scores on the actual retrieved records rather than a pre-retrieval probe, pass `assess_quality=True` to `assemble()`. The quality object is attached to `AssemblyResult.metadata["quality"]`. Default is `False`; the result shape is bit-for-bit identical to the pre-metacognitive behavior when the parameter is omitted.

```python
result = assembler.assemble(
    query_cues={"topic": "kubernetes deployment"},
    agent_id="agent-1",
    assess_quality=True,
)

quality = result.metadata["quality"]  # RetrievalQuality instance

# Use signals to decide how much to trust the context
if quality.staleness_ratio > 0.5:
    system_note = "Note: some retrieved memories may be outdated."
else:
    system_note = ""

if quality.avg_confidence < 0.4:
    caveat = "\n\nNote: confidence in these memories is low."
else:
    caveat = ""

messages = [
    {
        "role": "system",
        "content": f"Relevant context:{system_note}\n{result.formatted}{caveat}",
    },
    {"role": "user", "content": "What's our Kubernetes strategy?"},
]
```

**Performance note:** `assess_quality=True` adds bounded overhead — one `might_exist` call per query cue plus one `get_confidence` read per selected record (up to `max_items` reads). On a warm cache with 10 items, expect roughly 5% additional latency on `assemble()`. Confidence reads are pipelined in a single Redis round-trip batch.

### Building `RetrievalQuality` from a custom pipeline

If you run your own retrieval (BM25, RRF, hybrid, vector recall) and want the metacognitive signal without adopting `ContextAssembler`, call the `RetrievalQuality.from_records()` classmethod on the already-retrieved list:

```python
from popoto import RetrievalQuality

records = my_bm25_pipeline(query)  # any list of Popoto Model instances
quality = RetrievalQuality.from_records(
    records,
    query_cues={"topic": query},
    score_weights={"relevance": 1.0},
)
```

The factory introspects `records[0]._meta.fields` once to locate `ConfidenceField`, `ExistenceFilter`, and `DecayingSortedField` capabilities, then delegates to the same pure helpers `ContextAssembler.assess()` uses. Heterogeneous record lists (two or more concrete model classes) raise `TypeError` — score weights are per-model-class and would silently produce incorrect metrics otherwise. An empty list returns a zero-valued `RetrievalQuality` without raising.

---

## Tier 2: `error_summary(group_by=...)` + `"used"` outcome

### `PredictionLedgerMixin.error_summary()`

Aggregates prediction errors from the error sorted set, optionally grouped by time dimension or arbitrary callable.

```python
from popoto.fields.prediction_ledger import PredictionLedgerMixin

# Overall stats across all recorded predictions
summary = PredictionLedgerMixin.error_summary(Memory, partition="default")
# Returns: {"__all__": {"count": 842, "mean": 0.31, "stddev": 0.18,
#                        "p50": 0.28, "p90": 0.61, "p99": 0.88, "max": 0.97}}

# Group by hour of day to find time-of-day bias
by_hour = PredictionLedgerMixin.error_summary(
    Memory, partition="default", group_by="hour"
)
# Returns: {0: {"count": 32, "mean": 0.29, ...},
#           14: {"count": 118, "mean": 0.51, ...}, ...}
# Hour 14 (2 PM) shows elevated mean error — investigate why

# Group by day of week
by_weekday = PredictionLedgerMixin.error_summary(
    Memory, partition="default", group_by="weekday"
)
# Returns: {0: {...}, 1: {...}, ..., 6: {...}}
# Monday=0, Sunday=6

# Group by calendar date
by_day = PredictionLedgerMixin.error_summary(
    Memory, partition="default", group_by="day"
)
# Returns: {"2026-04-15": {...}, "2026-04-16": {...}, ...}

# Group by custom bucketer — callable(member_key, error) -> label
def error_band(member_key, error):
    if error < 0.3:
        return "low"
    elif error < 0.7:
        return "medium"
    return "high"

by_band = PredictionLedgerMixin.error_summary(
    Memory, partition="default", group_by=error_band
)
# Returns: {"low": {...}, "medium": {...}, "high": {...}}
```

Signature:

```python
PredictionLedgerMixin.error_summary(
    model_class,
    partition: str = "default",
    group_by: str | callable | None = None,
    limit: int = 100,
) -> dict
```

Built-in `group_by` string values: `"hour"` (0–23), `"weekday"` (0–6, Monday=0), `"day"` (ISO date string). Unknown strings raise `ValueError` listing the known options.

Each `stats_dict` value has keys: `count`, `mean`, `stddev`, `p50`, `p90`, `p99`, `max`.

**Implementation note:** `error_summary` reads the error sorted set via `ZREVRANGE` then fetches per-instance metadata via a pipelined batch of `HGET` calls — one call per member. This is one network round-trip regardless of `limit`. Corrupt msgpack entries are logged at warning level and skipped. The function is an eventually-consistent sampling tool, not a transactional snapshot; a resolution landing mid-batch may appear in some rows and not others.

Empty error set returns `{"__all__": {"count": 0, "mean": 0.0, ...}}` rather than raising.

### `"used"` outcome in `ObservationProtocol`

The `"used"` outcome records that the agent read and reasoned over a memory but did not act on it in the response. It is strictly distinct from `"deferred"`:

| Outcome | Staged read | Predictions | Confidence | Cycles | Decay touch |
|---------|-------------|-------------|------------|--------|-------------|
| `acted` | Confirm | Auto-resolve (error=0.1) | Corroborate (signal=0.9) | Strengthen (×1.2) | Yes |
| `dismissed` | Discard | Auto-resolve (error=0.5) | — | Weaken (×0.8) | — |
| `deferred` | Discard | — | — | — | — |
| `contradicted` | Discard | Auto-resolve (error=0.9) | Contradict (signal=0.1) | Aggressively weaken (×0.5) | — |
| **`used`** | **Confirm** | **Auto-resolve (error=0.3)** | **—** | **—** | **—** |

`"used"` confirms the staged read (via `AccessTrackerMixin.confirm_access()`) and auto-resolves pending predictions with a moderate error value (`PL_AUTO_RESOLVE_USED = 0.3`). It does not touch `ConfidenceField`, `CyclicDecayField`, or `DecayingSortedField`. The result: the read is recorded as confirmed (so access patterns are accurate), but no signal strength or confidence change is emitted.

**Use `"used"` when** the agent consulted the memory while composing an answer but the memory did not directly appear in the response — a common case that `"acted"` overcounts and `"deferred"` undercounts.

```python
from popoto.fields.observation import ObservationProtocol

outcome_map = {
    memory1.db_key.redis_key: "acted",       # memory appeared in response
    memory2.db_key.redis_key: "used",        # memory informed reasoning, not cited
    memory3.db_key.redis_key: "dismissed",   # agent explicitly set aside
    # memory4 not in map → defaults to "deferred"
}
ObservationProtocol.on_context_used(memories, outcome_map)
```

The constant for the auto-resolve error is configurable:

```python
from popoto.fields.constants import Defaults
Defaults.PL_AUTO_RESOLVE_USED = 0.2  # lower error if "used" should be treated more like "acted"
```

---

## Tier 3: `AdaptiveAssembler`

`AdaptiveAssembler` wraps a `ContextAssembler` and adjusts its `score_weights` online via an autoresearch-style keep/revert loop. Every `window_size` calls it proposes a small weight perturbation, measures quality over the next window, and either keeps the change (if average quality improved or stayed flat) or reverts to the previous baseline.

**Single-threaded by design.** The rolling-window bookkeeping is not atomic across concurrent calls and deliberately has no locks. Multi-threaded agents must hold one `AdaptiveAssembler` per thread. Adaptation does not survive process restarts — each session starts from the `score_weights` passed to the inner `ContextAssembler`.

### Basic setup

```python
from popoto.recipes.context_assembler import ContextAssembler
from popoto.recipes.adaptive_assembler import AdaptiveAssembler

inner = ContextAssembler(
    model_class=Memory,
    score_weights={"relevance": 0.6, "confidence": 0.3, "recency": 0.1},
    max_items=10,
    max_tokens=4000,
)

adaptive = AdaptiveAssembler(
    inner,
    window_size=20,           # calls per rolling window; default from Defaults.ADAPTIVE_QUALITY_WINDOW_SIZE
    weight_perturbation=0.05, # how much to shift per proposal
)
```

### Using the adaptive assembler

```python
for query in incoming_queries:
    result = adaptive.assemble({"topic": query.topic}, agent_id=query.agent_id)
    # result is a standard AssemblyResult; use it exactly as you would from ContextAssembler
    inject_into_llm(result.formatted)

# Inspect the current weights after the session
print(adaptive.current_weights)
# e.g. {"relevance": 0.55, "confidence": 0.35, "recency": 0.1}
# — the loop shifted weight toward "confidence" because it improved quality
```

### Observing adaptation state

```python
print(adaptive.baseline_quality)    # float | None — rolling mean quality under current baseline
print(adaptive.is_testing_candidate) # True while gathering a candidate window
print(adaptive.current_weights)      # always the weights currently in use
```

### Weight evolution over a session

The loop adapts once every `2 * window_size` calls (one baseline window, one candidate window). With `window_size=20`, you get a weight proposal every 40 calls. Over a 200-call session:

- Calls 1–20: gather baseline window
- Calls 21–40: test candidate weights
- If candidate beats baseline: keep; new baseline = candidate mean
- If not: revert; try a different perturbation next round
- Continue until session ends

Individual weight values are clamped to `[0.05, 0.9]` to prevent degenerate all-zero or all-one configurations.

### Custom quality metric

The default scalarization is `fok_score * avg_confidence` — product punishes imbalance multiplicatively, requiring both high FOK and high confidence for a strong signal. Override via `quality_metric`:

```python
# Pessimistic: only as good as the worst dimension
adaptive = AdaptiveAssembler(
    inner,
    quality_metric=lambda q: min(q.fok_score, q.avg_confidence),
)

# Freshness-first: weight staleness more heavily
adaptive = AdaptiveAssembler(
    inner,
    quality_metric=lambda q: q.fok_score * q.avg_confidence * (1.0 - q.staleness_ratio),
)
```

If `quality_metric` raises on a given call (e.g., `None` multiplication), the exception is logged and the sample is skipped — the loop does not crash.

### Deterministic testing

Pass a seeded `random.Random` instance for reproducible tests:

```python
import random

rng = random.Random(42)
adaptive = AdaptiveAssembler(inner, rng=rng)
```

---

## Performance notes

- `assess_quality=True` on `assemble()` adds bounded overhead: one `might_exist` per query cue plus `get_confidence` reads pipelined in a single Redis round-trip for all selected records. Measured at ~5% additional latency on a 10-item selection with a warm cache.
- `error_summary()` reads the error sorted set with `ZREVRANGE` (one round-trip) then issues all per-instance `HGET` calls in a single pipelined batch regardless of `limit`. The total cost is two Redis round-trips.
- `AdaptiveAssembler` has no additional Redis overhead beyond the wrapped `ContextAssembler`. The rolling-window bookkeeping is pure in-memory Python.

---

## Limitations and v2 roadmap

- **No cross-restart persistence.** `AdaptiveAssembler`'s learned `score_weights` are per-process. If the agent restarts, adaptation starts over from the initial weights. Persisting learned weights to Redis is a v2 scope item.
- **Source credibility is bookkeeping-only in v1.** The plan design includes per-source credibility weighting of observation signals, but applying it requires a new per-source score index and significant changes to the `_apply_*` dispatch. v1 records the intent; the application is deferred to v2.
- **No statistical significance testing on keep/revert.** The loop uses mean comparison over a rolling window. A t-test or bootstrap CI before accepting a weight change would reduce noise but slow convergence. Marked as a v2 option.
- **`quality_metric` is a proxy, not a task metric.** `fok_score * avg_confidence` optimizes retrieval quality as measured by the memory layer, not downstream task performance. Monitor actual task outcomes alongside the adaptive loop. See plan Risk 2 for the Goodhart's Law caution.
- **`error_summary` is eventually consistent.** Not a transactional snapshot: a resolution landing between the `ZREVRANGE` and the pipelined `HGET` batch may appear in some rows and not others. Use as a sampling/debugging tool, not a real-time gauge.

---

## See also

- [ContextAssembler](context-assembler.md) — the underlying retrieval pipeline
- [PredictionLedger](prediction-ledger.md) — outcome tracking and `error_summary`
- [ObservationProtocol](observation-protocol.md) — full outcome effects matrix including `"used"`
- [Agent Memory overview](agent-memory.md) — full primitives reference
