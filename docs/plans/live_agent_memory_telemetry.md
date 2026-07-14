---
status: planned
type: feature
appetite: Large
owner: valorengels
created: 2026-07-14
tracking: https://github.com/tomcounsell/popoto/issues/464
last_comment_id:
revision_applied: false
---

# Live-agent memory telemetry: instrument ContextAssembler + outcome loop

## Problem

Every offline benchmark the project runs (SIQ #459, RLT #460, hybrid sweeps) is a
*proxy* for the one thing they cannot synthesize: the real query/turn distribution a
live agent sees, and real outcome labels for the memories it injects. That signal
already exists in the running system — `ContextAssembler.assemble()` picks a set of
memories on every turn, and `ObservationProtocol.on_context_used()` already knows
whether each one was **acted / used / dismissed / deferred / contradicted**. But the
outcome signal is consumed to nudge `ConfidenceField` / `CyclicDecayField` and then
**evaporates**: no record of *which memories were injected, at what rank, with what
score, and what happened to them* survives the call.

The 2026-07-10 dogfood audit (issue #464 comment) confirmed the gap on this machine's
live db0: the memory system runs live (≈2k `Memory` records, ≈47k `ReflectionRun`),
but **zero outcome records exist** — the outcome loop is not wired end-to-end, so
telemetry ships blind until the join is captured. It also found an existing
`analytics:*` counter namespace in the *agent stack* (not popoto), and Sentry for
error reporting in the agent stack.

**Desired outcome.** A thin, opt-in telemetry layer in popoto that turns every live
`assemble()` call into a durable, TTL-bounded, Valkey-safe event record: the injected
set (ids, ranks, scores), retrieval mode, budget consumed, and latency — joinable to
the later outcome. Plus an offline analyzer that reads those records into
decay-regret and confidence-calibration reports. Privacy default: **ids only**; memory
content/query text captured **only** on explicit per-store opt-in.

## Scope boundary (what this issue is and is NOT)

- **IN (this repo, popoto):** the event-record model, the recorder that instruments
  `ContextAssembler`, the `report_outcomes()` join, the offline analyzer, the
  `telemetry_capture` opt-in flag, and docs.
- **OUT (separate repo, the AI agent stack):** the "agent-side wiring audit" —
  actually calling `report_outcomes()` / `on_context_used()` in the live deployments,
  routing telemetry-path errors to Sentry, and the `analytics:*` counter convention.
  popoto ships the *mechanism*; the deployment wires it. This plan produces a handoff
  note for that work, but writes no agent-stack code.
- **OUT (v1):** export/publication tooling for content-bearing traces (that becomes
  part of #459 fixture curation); fusion-disagreement and refusal-threshold analyses
  (fast-follows per the issue); per-signal (BM25 vs vector vs graph) score
  decomposition (the v1 trace captures the final fused/composite score per record).
- **Not self-benchmarking** in the 2026-06-11 sense: no before/after gates, no
  automated tuning. This produces *evidence* for the maintainer-reserved
  policy-default conversations (refusal threshold, decay rates, outcome semantics).

## Design

New recipe module: `src/popoto/recipes/memory_telemetry.py`. Recipes are the right
layer — this composes existing primitives (a `Model` with TTL, `ContextAssembler`,
`ObservationProtocol`) and is substrate/beta, so breaking changes here are acceptable
while the ORM core stays untouched. One small, additive, off-by-default hook is added
to `ContextAssembler.assemble()` (see §4).

### 1. Event-record model: `AssemblyEvent`

A concrete Popoto `Model` shipped by popoto (dogfooding — telemetry lands in the same
Redis/Valkey), with a mandatory default TTL so telemetry can never grow a store past
the 20k-scale posture.

```python
class AssemblyEvent(Model):
    event_id       = AutoKeyField()
    agent_id       = KeyField(null=True)     # partition; matches assemble(agent_id=)
    model_name     = KeyField(null=True)     # class the assembler queried
    ts             = SortedField(type=float) # unix ts; range queries for the analyzer
    retrieval_mode = KeyField(null=True)     # "hybrid"|"lexical"|"composite"
    capture        = KeyField(null=True)     # "ids" | "content"
    injected       = ListField(default=[])   # [{key, rank, score, source}]
    budget_tokens  = IntField(null=True)     # metadata["token_count"]
    budget_items   = IntField(null=True)     # len(records)
    corpus_size    = IntField(null=True)     # count of candidate store (best-effort)
    latency_ms     = FloatField(null=True)   # metadata["timing_ms"]
    overhead_ms    = FloatField(null=True)   # cost of the telemetry write itself
    query_text     = StringField(null=True)  # ONLY when capture == "content"
    outcomes       = ListField(default=[])   # joined later: [{key, outcome, at}]

    class Meta:
        ttl = DEFAULT_EVENT_TTL  # seconds; magic-number constant, not user config
```

- `injected` entries are `{"key", "rank", "score", "source"}` where `source` is
  `"pull"` or `"push"`. Under `capture="ids"` (default) there is **no content** —
  ids/ranks/scores/metadata only. Under `capture="content"`, entries additionally
  carry `"content"` (the record dict) and the event carries `query_text`.
- msgpack-serialized `ListField` holds the list-of-dicts natively (existing behavior).
- **Namespace decision (audit comment #2):** popoto owns *model-keyed, queryable,
  TTL'd records* (`AssemblyEvent:*`); the agent stack owns the `analytics:*` counters.
  Deliberate divergence, not a third convention — documented in the feature doc.

### 2. `TelemetryRecorder` — instrument `assemble()`

Wraps a `ContextAssembler` (same pattern as `AdaptiveAssembler`), delegates
`assemble()`, and writes one `AssemblyEvent` per call. **Fail-open**: any error in the
telemetry path is caught and logged (popoto has no Sentry dep; the agent stack routes
these to Sentry). Telemetry must never break or measurably slow `assemble()`.

```python
class TelemetryRecorder:
    def __init__(self, inner, *, event_model=AssemblyEvent, capture="ids",
                 sample_rate=1.0, ttl=DEFAULT_EVENT_TTL, rng=None): ...
    def assemble(self, query_cues=None, agent_id=None, **kwargs) -> AssemblyResult:
        kwargs["emit_trace"] = True                 # force the trace hook on
        result = self.inner.assemble(query_cues, agent_id=agent_id, **kwargs)
        if self._should_sample():
            self._record(result, query_cues, agent_id)  # try/except, timed
        return result
```

- `sample_rate` (0.0–1.0, deterministic via injected `rng`) caps volume on hot paths.
- `_record` reads `result.metadata["trace"]` (ids/rank/score/source), `token_count`,
  `timing_ms`, and `inner._effective_mode`; times its own write into `overhead_ms`;
  writes the event with `_ttl = self.ttl`; stashes `result.metadata["telemetry_event_id"]`
  so the caller can join outcomes later.
- Cumulative overhead is exposed via `recorder.overhead_stats` (count, mean, p95) so
  the cost is *reported*, satisfying the issue's "record the cost" constraint and
  protecting #460's latency numbers.
- `capture="content"` is set explicitly in code at construction — **never** a global
  env var or runtime toggle. Passing an unknown value raises at construction.

### 3. Outcome join: `report_outcomes()`

```python
def report_outcomes(event_id, outcome_map, *, event_model=AssemblyEvent,
                    apply_effects=False, instances=None): ...
```

Loads the `AssemblyEvent`, and for each injected key present in `outcome_map` appends
`{"key", "outcome", "at"}` to `event.outcomes`, then saves (TTL preserved). Validates
outcomes against `ObservationProtocol.VALID_OUTCOMES`. Record-only by default:
telemetry is pure observation and does not double-apply confidence/decay effects.
`apply_effects=True` (with `instances=`) forwards to
`ObservationProtocol.on_context_used()` for callers that want the recorder to own both
sides. Missing event (TTL-expired) is a logged no-op, not an error.

### 4. `ContextAssembler` instrumentation hook (additive, off-by-default)

Add `emit_trace: bool = False` to `assemble()`, mirroring the existing
`assess_quality` opt-in exactly. When `True`, attach:

```python
metadata["trace"] = [
    {"key": _get_key(r), "rank": i, "score": score, "source": "pull"|"push"}
    for i, r in enumerate(selected)
]
```

Scores come from the existing read-only, pipelined `_score_proxy_for_records(selected)`
(ZSCORE-only, Valkey-safe) — no new query machinery. `source` is derived from the
already-computed `pull_keys`/`push_keys`. When `emit_trace=False` (every existing
caller) the result is **bit-for-bit identical** to today. This is the only change to a
shipped file; it is additive and within the beta recipe layer.

### 5. Offline analyzer: `TelemetryAnalyzer`

Report generator in the style of the sweep reports (returns a dict + a markdown
string; no side effects, read-only over `AssemblyEvent`).

```python
class TelemetryAnalyzer:
    def __init__(self, event_model=AssemblyEvent, agent_id=None): ...
    def load_events(self, since=None, limit=None) -> list[AssemblyEvent]
    def injection_precision(self) -> dict     # injected → acted fraction, overall + @rank
    def confidence_calibration(self) -> dict  # score-at-injection buckets → acted rate
    def decay_regret(self) -> dict            # injected-then-dismissed vs -then-acted
    def report(self) -> str                   # markdown summary
```

- **injection_precision**: over events with joined outcomes, fraction of injected
  memories whose outcome is `acted` (and `acted|used`), sliced by rank — the live
  injection-precision-@-budget signal (SIQ #459 in production).
- **confidence_calibration**: bucket injected memories by their injection-time `score`,
  report acted-rate per bucket → the calibration curve that informs ConfidenceField
  cap/initial constants.
- **decay_regret**: counts of injected-then-`dismissed`/`contradicted` (surfaced but
  wrong) vs injected-then-`acted` per score bucket — first empirical feedback for the
  decay magic numbers.
- Fusion-disagreement and refusal-threshold analyses are explicitly deferred
  (fast-follow, per the issue) — the per-record trace already stores the fused score,
  so adding them later needs no schema change.

### 6. Exports

Add `TelemetryRecorder`, `TelemetryAnalyzer`, `AssemblyEvent`, `report_outcomes` to
`recipes/__init__.py` and the top-level `popoto/__init__.py` (`__all__`), matching how
`AdaptiveAssembler` / `ContextAssembler` are exported.

## Constraints (from the issue, enforced here)

- **Valkey-safe:** only core commands. The trace uses ZSCORE (read-only pipeline); the
  event write is an ordinary model `save()`. No modules, no Lua added.
- **Overhead measured and reported:** `overhead_ms` per event + `overhead_stats`
  aggregate on the recorder.
- **TTL mandatory:** `Meta.ttl = DEFAULT_EVENT_TTL`; recorder can shorten but the model
  always expires. Telemetry cannot grow a store unboundedly.
- **Privacy default off:** `capture="ids"`; `content` is per-store, code-set, explicit.
- **Magic numbers, not config:** `DEFAULT_EVENT_TTL`, calibration bucket edges, and the
  sample-rate default are experimental constants with sourced docstrings, not user
  knobs (feedback_magic_numbers).
- **ORM stays stable:** the only shipped-file change is the additive, off-by-default
  `emit_trace` kwarg on the (beta, recipe-layer) `ContextAssembler`.

## Testing

New `tests/test_memory_telemetry.py` (uses the pytest plugin's DB-15 isolation; no
manual flush needed):

1. **Event write, ids-only:** `assemble()` via recorder writes one `AssemblyEvent` with
   injected ids/ranks/scores, correct mode/budget/latency, TTL set, and **no**
   `query_text`/`content`.
2. **Content capture opt-in:** `capture="content"` includes `query_text` and per-record
   `content`; `capture="ids"` (default) never does. Unknown `capture` raises.
3. **emit_trace parity:** `assemble(emit_trace=False)` metadata is byte-identical to a
   run without the kwarg; `emit_trace=True` adds a well-formed `trace`.
4. **Fail-open:** a recorder whose event model save raises (monkeypatched) still returns
   the real `AssemblyResult` and logs; `assemble()` never propagates the error.
5. **Sampling:** `sample_rate=0.0` writes nothing; `1.0` writes every call; a seeded rng
   at `0.5` writes the expected subset.
6. **Outcome join:** `report_outcomes(event_id, {...})` appends outcomes to the matching
   event; invalid outcome raises; expired/missing event is a logged no-op.
7. **Analyzer:** seed a handful of events + outcomes, assert `injection_precision`,
   `confidence_calibration`, and `decay_regret` compute the expected numbers, and
   `report()` returns non-empty markdown.
8. **Overhead reported:** `overhead_stats` is populated after N calls.

Run narrow: `PYTHONPATH=src pytest tests/test_memory_telemetry.py` plus the existing
`tests/test_context_assembler*.py` to prove the `emit_trace` addition is inert.
Then `scripts/ci-local.sh` for the gates.

## Rabbit holes (out of scope — do not chase)

- **Per-signal score decomposition** (BM25 vs vector vs graph per record): v1 stores the
  fused/composite score only. The fuse path would need to thread per-arm scores through
  `QueryBuilder.fuse()` — a separate change.
- **Streaming/async event writes, batching queues, background flushers:** v1 writes
  synchronously inside the sampled call (fail-open, timed). If overhead proves material
  at scale, a batched writer is a follow-up, informed by the reported `overhead_stats`.
- **Tuning any constant from the collected data:** explicitly maintainer-reserved.
- **Export/publication of content traces:** #459 fixture curation owns it.

## Handoff (agent-stack, separate repo — not built here)

1. Call `report_outcomes()` (or `TelemetryRecorder` + `on_context_used`) where the live
   agents resolve memory outcomes — the audit found this is not wired today.
2. Decide `capture` per store: only stores the operator asserts are public-safe get
   `content`. Document that humans can volunteer personal details even to all-open
   agents — enabling `content` means the operator takes responsibility for that store
   being publishable.
3. Route telemetry-path warnings to Sentry (popoto only logs).
