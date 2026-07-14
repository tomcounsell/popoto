# Memory Telemetry

`popoto.recipes.memory_telemetry` turns every live
[`ContextAssembler.assemble()`](context-assembler.md) call into a durable,
TTL-bounded, Valkey-safe event record — the injected memory set (ids, ranks,
scores), retrieval mode, budget consumed, and latency — joinable to the later
outcome reported through the [ObservationProtocol](observation-protocol.md)
(`acted` / `used` / `dismissed` / `deferred` / `contradicted`). An offline
analyzer reads those records into injection-precision, confidence-calibration,
and decay-regret reports.

Live agents give what offline benchmarks cannot: the real query/turn
distribution and real outcome labels. The outcome signal already exists — it is
used to nudge `ConfidenceField` / `CyclicDecayField` and then evaporates. This
recipe records it so every live agent becomes a continuous, real-workload
benchmark.

!!! warning "Privacy default is ids-only"
    Content capture is **opt-in**. The default `capture="ids"` records memory
    ids, ranks, scores, and metadata only — never memory content or query text.
    Enabling `capture="content"` is a per-store decision made explicitly in
    code (never a global env var or runtime toggle) for fully-open deployments
    whose operator asserts the store is publishable. Even in all-open
    deployments, humans talking to an agent can volunteer personal details;
    enabling `content` means the operator takes responsibility for that store
    being public-safe.

## Components

| Component | Role |
|-----------|------|
| `AssemblyEvent` | Popoto `Model` (msgpack, `Meta.ttl`) — one record per `assemble()` call |
| `TelemetryRecorder` | Wraps a `ContextAssembler`; writes one event per call; fail-open, sampled |
| `report_outcomes()` | Joins later outcomes onto the matching event record |
| `TelemetryAnalyzer` | Offline, read-only report generator |

## Quick start

```python
from popoto.recipes.context_assembler import ContextAssembler
from popoto.recipes.memory_telemetry import (
    TelemetryRecorder, TelemetryAnalyzer, report_outcomes,
)

assembler = ContextAssembler(Memory, score_weights={"relevance": 1.0})
recorder = TelemetryRecorder(assembler)            # capture="ids" (private)

result = recorder.assemble({"topic": "deploy"}, agent_id="agent-1")
event_id = result.metadata["telemetry_event_id"]

# ... later, when the agent's response is observed ...
report_outcomes(event_id, {mem.db_key.redis_key: "acted"})

# ... offline ...
print(TelemetryAnalyzer(agent_id="agent-1").report())
```

`TelemetryRecorder.assemble()` delegates to the wrapped assembler unchanged and
returns the real `AssemblyResult`, adding `metadata["telemetry_event_id"]` when
an event was written. It forces the assembler's off-by-default
[`emit_trace`](context-assembler.md#telemetry-hook-emit_trace) hook on so the
injection trace is available.

## `AssemblyEvent`

One record per call. Queryable by `agent_id` (partition), TTL-bounded so
telemetry can never grow a store past the scale posture.

| Field | Notes |
|-------|-------|
| `event_id` | Auto key |
| `agent_id` | Partition — matches `assemble(agent_id=...)` |
| `model_name` | Class the assembler queried |
| `ts` | Unix timestamp (float) |
| `retrieval_mode` | `"hybrid"` \| `"lexical"` \| `"composite"` |
| `capture` | `"ids"` (default) \| `"content"` (opt-in) |
| `injected` | `[{key, rank, score, source[, content]}]`, rank order |
| `budget_tokens` / `budget_items` | Budget consumed |
| `latency_ms` | `assemble()` wall time |
| `overhead_ms` | Telemetry preparation cost |
| `query_text` | Populated **only** under `capture="content"` |
| `outcomes` | Joined by `report_outcomes()`: `[{key, outcome, at}]` |

## `TelemetryRecorder`

```python
TelemetryRecorder(
    inner,                 # a ContextAssembler
    *,
    capture="ids",         # "ids" (private) | "content" (opt-in). Invalid -> ValueError
    sample_rate=1.0,       # fraction of calls recorded, [0.0, 1.0]
    ttl=DEFAULT_EVENT_TTL,  # per-instance TTL (seconds)
    rng=None,              # random.Random for deterministic sampling in tests
)
```

- **Fail-open.** Any error in the telemetry path is caught and logged — it
  never propagates into `assemble()` and never blocks a live agent. (Popoto has
  no Sentry dependency; a deployment routes these warnings to its own reporter.)
- **Sampling.** Lower `sample_rate` on hot paths where write volume would
  distort latency benchmarks.
- **Overhead reported.** `recorder.overhead_stats` returns
  `{count, mean_ms, p95_ms, max_ms}` over the measured per-call telemetry cost
  — so telemetry can be shown *not* to distort latency numbers.

## `report_outcomes()`

```python
report_outcomes(event_id, outcome_map, *, apply_effects=False, instances=None)
```

For each `memory_key -> outcome` in `outcome_map` whose key was injected in this
event, appends `{key, outcome, at}` to the event's `outcomes`. Keys are the same
redis keys stored in `injected` (i.e. `instance.db_key.redis_key`); outcomes are
validated against `ObservationProtocol.VALID_OUTCOMES`.

Record-only by default — telemetry is pure observation and does not double-apply
confidence/decay effects. Pass `apply_effects=True` (with `instances=`) to also
forward to `ObservationProtocol.on_context_used()` so one call owns both sides.
A missing event (e.g. TTL-expired) is a logged no-op returning `None`, not an
error. Re-saving refreshes the event's TTL to the model default.

## `TelemetryAnalyzer`

Offline, read-only. All metrics are computed over injected memories that carry a
joined outcome; injections without one are counted as `pending`.

| Method | Signal |
|--------|--------|
| `injection_precision()` | Fraction of injected-and-labeled memories acted on, overall and by rank |
| `confidence_calibration()` | Acted-rate bucketed by injection-time score |
| `decay_regret()` | Injected-then-`dismissed`/`contradicted` vs injected-then-`acted`, by score bucket |
| `report()` | Markdown summary of all three |

```python
an = TelemetryAnalyzer(agent_id="agent-1")
an.injection_precision()      # {'injected', 'labeled', 'pending', 'acted_rate', 'by_rank', ...}
an.confidence_calibration()   # {'buckets': [{'label', 'labeled', 'acted', 'acted_rate'}, ...]}
an.decay_regret()             # {'regret', 'acted', 'regret_rate', 'by_bucket': [...]}
print(an.report())            # markdown
```

Fusion-disagreement and refusal-threshold analyses are fast-follows (the trace
already stores the fused score, so they need no schema change).

## Scope and constraints

- **Valkey-safe:** only core commands. The injection trace uses read-only
  pipelined `ZSCORE`; the event write is an ordinary model `save()`. No modules,
  no Lua.
- **TTL mandatory:** `AssemblyEvent.Meta.ttl` bounds the store; the recorder can
  shorten but the record always expires.
- **Not self-benchmarking:** no before/after gates and no automated tuning. This
  produces *evidence* for the maintainer-reserved policy-default conversations
  (refusal threshold, decay rates, outcome semantics) — it never changes a
  default on its own.
- **Namespace:** popoto owns model-keyed, queryable, TTL'd records
  (`AssemblyEvent:*`); a deployment's own counter namespace (e.g. `analytics:*`)
  is separate by design.

## See Also

- [ContextAssembler](context-assembler.md) — the retrieval-to-injection bridge and the `emit_trace` hook
- [ObservationProtocol](observation-protocol.md) — the outcome vocabulary the join records
- [Agent Memory](agent-memory.md) — the primitives overview
