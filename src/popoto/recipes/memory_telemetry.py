"""MemoryTelemetry — turn live ``assemble()`` calls into a real-workload benchmark.

Issue #464 (part of #456, Track A). Live agents give us the two things every
offline benchmark lacks: the real query/turn distribution and real outcome
labels. ``ContextAssembler.assemble()`` already picks a memory set on every
turn, and ``ObservationProtocol.on_context_used()`` already knows whether each
memory was acted / used / dismissed / deferred / contradicted — but that signal
is consumed to nudge confidence/decay and then evaporates. This recipe records
it durably (TTL-bounded, Valkey-safe) so every live agent becomes a continuous,
real-workload benchmark.

Three pieces:

* :class:`AssemblyEvent` — one msgpack-backed Popoto model instance per
  ``assemble()`` call, with a mandatory default TTL so telemetry can never grow
  a store past the 20k-scale posture. Dogfoods the same Redis/Valkey.
* :class:`TelemetryRecorder` — wraps a ``ContextAssembler`` (same pattern as
  ``AdaptiveAssembler``), delegates ``assemble()``, and writes one event per
  call. Fail-open: a telemetry error never breaks or measurably slows
  ``assemble()``. Overhead is measured and reported.
* :func:`report_outcomes` — joins the later outcome onto the matching event.
* :class:`TelemetryAnalyzer` — offline, read-only report generator producing
  injection-precision, confidence-calibration, and decay-regret reports.

Privacy: content capture is **opt-in**. The default ``capture="ids"`` records
memory ids, ranks, scores, and metadata only — never memory content or query
text. ``capture="content"`` (set explicitly in code, per store — never a global
env var or runtime toggle) additionally records content and query text, for
fully-open deployments whose operator asserts the store is publishable.

Namespace note: popoto owns *model-keyed, queryable, TTL'd* records
(``AssemblyEvent:*``); the agent stack owns its ``analytics:*`` counters. This
is a deliberate divergence, not a third convention.

Example:
    from popoto.recipes.context_assembler import ContextAssembler
    from popoto.recipes.memory_telemetry import (
        TelemetryRecorder, TelemetryAnalyzer, report_outcomes,
    )

    assembler = ContextAssembler(Memory, score_weights={"relevance": 1.0})
    recorder = TelemetryRecorder(assembler)          # capture="ids" (private)
    result = recorder.assemble({"topic": "deploy"}, agent_id="agent-1")
    event_id = result.metadata["telemetry_event_id"]
    # ... later, when the agent's response is observed ...
    report_outcomes(event_id, {mem.db_key.redis_key: "acted"})
    # ... offline ...
    print(TelemetryAnalyzer(agent_id="agent-1").report())
"""

from __future__ import annotations

import logging
import random
import time

from ..fields.observation import VALID_OUTCOMES
from ..fields.shortcuts import (
    AutoKeyField,
    FloatField,
    IntField,
    KeyField,
    ListField,
    StringField,
)
from ..models.base import Model
from .context_assembler import _get_key, _record_to_dict

logger = logging.getLogger("POPOTO.MemoryTelemetry")


# ---------------------------------------------------------------------------
# Tuning constants — experimental magic numbers (feedback_magic_numbers), NOT
# user config. Sourced docstrings per project convention.
# ---------------------------------------------------------------------------

DEFAULT_EVENT_TTL = 7 * 24 * 3600
"""Default lifetime (seconds) of an AssemblyEvent record: 7 days. Bounds the
telemetry store so it can never grow past the 20k-scale posture regardless of
call volume. Long enough to join same-week outcomes and run a weekly analyzer
pass; short enough that residue self-clears. Recorder may shorten per store."""

DEFAULT_SAMPLE_RATE = 1.0
"""Fraction of ``assemble()`` calls recorded. 1.0 = every call. Lower it on hot
paths where the write volume would distort #460's latency numbers; the recorder
reports measured overhead so the operator can pick a rate from evidence."""

CALIBRATION_BUCKET_EDGES = (0.2, 0.4, 0.6, 0.8)
"""Interior edges partitioning injection-time score into 5 calibration buckets
([-inf,0.2), [0.2,0.4), [0.4,0.6), [0.6,0.8), [0.8,inf)). Used only by the
offline analyzer to bin the confidence-calibration and decay-regret curves."""

_ACTED_OUTCOMES = frozenset({"acted"})
_POSITIVE_OUTCOMES = frozenset({"acted", "used"})
_REGRET_OUTCOMES = frozenset({"dismissed", "contradicted"})


# ---------------------------------------------------------------------------
# AssemblyEvent — the durable event record
# ---------------------------------------------------------------------------


class AssemblyEvent(Model):
    """One telemetry record per ``ContextAssembler.assemble()`` call.

    ids-only by default; content/query_text populated only under
    ``capture="content"``. TTL-bounded via ``Meta.ttl`` (overridable per
    instance by the recorder). Queryable by ``agent_id`` (partition KeyField).

    Fields:
        event_id: Auto key.
        agent_id: Partition — matches ``assemble(agent_id=...)``. May be None.
        model_name: Name of the model class the assembler queried.
        ts: Unix timestamp (float) of the call. Plain field (not a sorted
            index) to keep the hot-path write to a single key — the offline
            analyzer filters by time in Python over the TTL-bounded corpus.
        retrieval_mode: ``"hybrid"`` | ``"lexical"`` | ``"composite"``.
        capture: ``"ids"`` (default, private) | ``"content"`` (opt-in).
        injected: List of ``{key, rank, score, source[, content]}`` — one per
            injected memory, in final rank order. ``content`` present only
            under ``capture="content"``.
        budget_tokens / budget_items: Budget consumed by the injection.
        corpus_size: Best-effort candidate-store size (may be None).
        latency_ms: ``assemble()`` wall time.
        overhead_ms: Telemetry preparation cost (see TelemetryRecorder).
        query_text: The query cue text — populated only under
            ``capture="content"``.
        outcomes: Joined later by :func:`report_outcomes`; list of
            ``{key, outcome, at}``.
    """

    event_id = AutoKeyField()
    agent_id = KeyField(null=True)
    model_name = KeyField(null=True)
    ts = FloatField(null=True)
    retrieval_mode = KeyField(null=True)
    capture = KeyField(null=True)
    injected = ListField(default=[])
    budget_tokens = IntField(null=True)
    budget_items = IntField(null=True)
    corpus_size = IntField(null=True)
    latency_ms = FloatField(null=True)
    overhead_ms = FloatField(null=True)
    query_text = StringField(null=True)
    outcomes = ListField(default=[])

    class Meta:
        ttl = DEFAULT_EVENT_TTL


_VALID_CAPTURE = frozenset({"ids", "content"})


# ---------------------------------------------------------------------------
# TelemetryRecorder — instrument assemble()
# ---------------------------------------------------------------------------


class TelemetryRecorder:
    """Wrap a ``ContextAssembler`` and record one ``AssemblyEvent`` per call.

    Delegates ``assemble()`` unchanged and returns the real
    ``AssemblyResult`` (with ``metadata["telemetry_event_id"]`` added when an
    event was written). The telemetry path is **fail-open**: any error is
    caught and logged — it never propagates into ``assemble()``.

    Args:
        inner: A ``ContextAssembler`` (or any object with a compatible
            ``assemble(...)`` returning an ``AssemblyResult`` and exposing
            ``model_class`` / ``_effective_mode``).
        event_model: The event Model class. Default :class:`AssemblyEvent`.
        capture: ``"ids"`` (default, private — ids/ranks/scores/metadata only)
            or ``"content"`` (opt-in — additionally records memory content and
            query text). Set explicitly in code, per store. Any other value
            raises ``ValueError`` at construction.
        sample_rate: Fraction of calls to record in ``[0.0, 1.0]``. Default
            :data:`DEFAULT_SAMPLE_RATE` (1.0). ``0.0`` records nothing.
        ttl: Per-instance TTL (seconds) applied to each event. Default
            :data:`DEFAULT_EVENT_TTL`.
        rng: Optional ``random.Random`` for deterministic sampling in tests.
    """

    def __init__(
        self,
        inner,
        *,
        event_model=AssemblyEvent,
        capture: str = "ids",
        sample_rate: float = DEFAULT_SAMPLE_RATE,
        ttl: int = DEFAULT_EVENT_TTL,
        rng: random.Random | None = None,
    ):
        if capture not in _VALID_CAPTURE:
            raise ValueError(
                f"capture={capture!r} is not valid. Allowed: {sorted(_VALID_CAPTURE)}. "
                "'content' is opt-in and must be set explicitly per store."
            )
        if not 0.0 <= float(sample_rate) <= 1.0:
            raise ValueError(f"sample_rate must be in [0.0, 1.0], got {sample_rate!r}")
        self.inner = inner
        self.event_model = event_model
        self.capture = capture
        self.sample_rate = float(sample_rate)
        self.ttl = int(ttl)
        self._rng = rng if rng is not None else random.Random()
        self._overhead_samples: list[float] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def assemble(self, query_cues=None, agent_id=None, **kwargs):
        """Delegate to the wrapped assembler, recording one event per call.

        Forces ``emit_trace=True`` on the inner call (overriding any caller
        value) so the injection trace is available. Returns the inner
        ``AssemblyResult`` unchanged apart from an added
        ``metadata["telemetry_event_id"]`` when an event was written.
        """
        kwargs["emit_trace"] = True
        result = self.inner.assemble(query_cues=query_cues, agent_id=agent_id, **kwargs)
        if self._should_sample():
            self._record(result, query_cues, agent_id)
        return result

    @property
    def overhead_stats(self) -> dict:
        """Measured telemetry overhead across recorded calls.

        Returns a dict ``{count, mean_ms, p95_ms, max_ms}`` over the full
        per-call ``_record`` wall time (preparation + Redis write). Empty
        history yields zeros. This is the "record the cost" report the issue
        requires — so telemetry can be shown not to distort #460's latency.
        """
        samples = self._overhead_samples
        if not samples:
            return {"count": 0, "mean_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
        ordered = sorted(samples)
        n = len(ordered)
        # Nearest-rank p95 (no interpolation) — stable for small n.
        p95_idx = min(n - 1, max(0, int(round(0.95 * (n - 1)))))
        return {
            "count": n,
            "mean_ms": round(sum(ordered) / n, 4),
            "p95_ms": round(ordered[p95_idx], 4),
            "max_ms": round(ordered[-1], 4),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _should_sample(self) -> bool:
        if self.sample_rate >= 1.0:
            return True
        if self.sample_rate <= 0.0:
            return False
        return self._rng.random() < self.sample_rate

    @staticmethod
    def _query_text(query_cues) -> str | None:
        if not query_cues:
            return None
        return " ".join(str(v) for v in query_cues.values())

    def _record(self, result, query_cues, agent_id):
        """Write one AssemblyEvent. Fail-open: log and swallow any error."""
        wall_t0 = time.perf_counter()
        try:
            trace = result.metadata.get("trace") or []
            content_map = {}
            if self.capture == "content":
                for r in result.records:
                    try:
                        content_map[_get_key(r)] = _record_to_dict(r)
                    except Exception:  # pragma: no cover - defensive
                        pass

            injected = []
            for entry in trace:
                item = {
                    "key": entry["key"],
                    "rank": entry["rank"],
                    "score": entry["score"],
                    "source": entry["source"],
                }
                if self.capture == "content":
                    content = content_map.get(entry["key"])
                    if content is not None:
                        item["content"] = content
                injected.append(item)

            prep_ms = round((time.perf_counter() - wall_t0) * 1000.0, 4)

            model_name = getattr(
                getattr(self.inner, "model_class", None), "__name__", None
            )
            event = self.event_model(
                agent_id=agent_id,
                model_name=model_name,
                ts=time.time(),
                retrieval_mode=getattr(self.inner, "_effective_mode", None),
                capture=self.capture,
                injected=injected,
                budget_tokens=int(result.metadata.get("token_count", 0) or 0),
                budget_items=len(result.records),
                latency_ms=float(result.metadata.get("timing_ms", 0.0) or 0.0),
                overhead_ms=prep_ms,
                query_text=(
                    self._query_text(query_cues) if self.capture == "content" else None
                ),
            )
            event._ttl = self.ttl
            event.save()
            result.metadata["telemetry_event_id"] = event.event_id
        except Exception as e:  # fail-open
            logger.warning("telemetry record failed (fail-open): %s", e)
        finally:
            self._overhead_samples.append((time.perf_counter() - wall_t0) * 1000.0)


# ---------------------------------------------------------------------------
# Outcome join
# ---------------------------------------------------------------------------


def report_outcomes(
    event_id,
    outcome_map,
    *,
    event_model=AssemblyEvent,
    apply_effects: bool = False,
    instances=None,
):
    """Join later outcomes onto the matching AssemblyEvent.

    For each ``(memory_key -> outcome)`` in ``outcome_map`` whose key was
    injected in this event, appends ``{key, outcome, at}`` to the event's
    ``outcomes`` list and saves. Record-only by default: telemetry is pure
    observation and does not double-apply confidence/decay effects (the agent
    stack applies those via ``ObservationProtocol.on_context_used`` separately).

    Args:
        event_id: The ``event_id`` returned in
            ``result.metadata["telemetry_event_id"]``.
        outcome_map: Dict mapping memory redis keys (the same keys stored in
            ``injected``, i.e. ``instance.db_key.redis_key``) to outcome
            strings. Validated against ``ObservationProtocol.VALID_OUTCOMES``.
        event_model: The event Model class. Default :class:`AssemblyEvent`.
        apply_effects: When True (and ``instances`` provided), also forward to
            ``ObservationProtocol.on_context_used(instances, outcome_map)`` so
            one call owns both the telemetry join and the ORM effects. Default
            False.
        instances: Optional list of the memory instances, required only when
            ``apply_effects=True``.

    Returns:
        The updated ``AssemblyEvent``, or ``None`` if the event was not found
        (e.g. its TTL expired) — a missing event is a logged no-op, not an
        error.

    Raises:
        ValueError: If any outcome string is not a valid outcome.

    Note:
        Re-saving refreshes the event's TTL to the model default. A same-week
        outcome join therefore keeps the event alive another full TTL — fine,
        the event is still the freshest evidence for that injection.
    """
    for key, outcome in outcome_map.items():
        if outcome not in VALID_OUTCOMES:
            raise ValueError(
                f"Invalid outcome '{outcome}' for key '{key}'. "
                f"Valid outcomes: {sorted(VALID_OUTCOMES)}"
            )

    event = None
    try:
        event = event_model.query.get(event_id=event_id)
    except Exception as e:
        logger.warning("report_outcomes: lookup of event %s failed: %s", event_id, e)

    if event is None:
        logger.warning(
            "report_outcomes: event %s not found (expired?); no join written",
            event_id,
        )
        if apply_effects and instances:
            _apply_observation_effects(instances, outcome_map)
        return None

    now = time.time()
    injected_keys = {i.get("key") for i in (event.injected or [])}
    joined = list(event.outcomes or [])
    for key, outcome in outcome_map.items():
        if key in injected_keys:
            joined.append({"key": key, "outcome": outcome, "at": now})
    event.outcomes = joined
    event.save()

    if apply_effects and instances:
        _apply_observation_effects(instances, outcome_map)

    return event


def _apply_observation_effects(instances, outcome_map):
    from ..fields.observation import ObservationProtocol

    try:
        ObservationProtocol.on_context_used(instances, outcome_map)
    except Exception as e:  # fail-open: telemetry must not break the caller
        logger.warning("report_outcomes apply_effects failed (fail-open): %s", e)


# ---------------------------------------------------------------------------
# Offline analyzer
# ---------------------------------------------------------------------------


def _bucket_index(score: float) -> int:
    """Return the calibration bucket index for a score."""
    for i, edge in enumerate(CALIBRATION_BUCKET_EDGES):
        if score < edge:
            return i
    return len(CALIBRATION_BUCKET_EDGES)


def _bucket_label(i: int) -> str:
    edges = CALIBRATION_BUCKET_EDGES
    lo = "-inf" if i == 0 else f"{edges[i - 1]:.1f}"
    hi = "inf" if i == len(edges) else f"{edges[i]:.1f}"
    return f"[{lo}, {hi})"


class TelemetryAnalyzer:
    """Offline, read-only report generator over :class:`AssemblyEvent` records.

    Produces the v1 report family: injection precision (@budget and @rank),
    confidence calibration (acted-rate by injection-time score), and decay
    regret (injected-then-dismissed vs -then-acted by score bucket). All
    metrics are computed only over injected memories that carry a joined
    outcome; injections without an outcome are counted separately as
    ``pending``. Fusion-disagreement and refusal-threshold analyses are
    explicit fast-follows (the trace already stores the fused score, so they
    need no schema change).

    Args:
        event_model: The event Model class. Default :class:`AssemblyEvent`.
        agent_id: Optional partition to restrict analysis to one agent.
    """

    def __init__(self, event_model=AssemblyEvent, agent_id=None):
        self.event_model = event_model
        self.agent_id = agent_id

    def load_events(self, since=None, limit=None):
        """Load events, optionally filtered by ``agent_id`` and ``ts >= since``.

        Read-only. ``since`` is a unix timestamp (float); filtering is done in
        Python over the TTL-bounded corpus. ``limit`` caps the most-recent N by
        timestamp.
        """
        if self.agent_id is not None:
            events = list(self.event_model.query.filter(agent_id=self.agent_id))
        else:
            events = list(self.event_model.query.all())
        if since is not None:
            events = [e for e in events if (e.ts or 0.0) >= since]
        events.sort(key=lambda e: (e.ts or 0.0), reverse=True)
        if limit is not None:
            events = events[:limit]
        return events

    def _joined_injections(self, events):
        """Flatten events into per-injection rows joined with their outcome.

        Returns a list of dicts: ``{key, rank, score, source, outcome}`` where
        ``outcome`` is the last reported outcome for that key in that event, or
        ``None`` if none was joined.
        """
        rows = []
        for event in events:
            # key -> last outcome joined for this event
            outcome_by_key = {}
            for o in event.outcomes or []:
                outcome_by_key[o.get("key")] = o.get("outcome")
            for inj in event.injected or []:
                rows.append(
                    {
                        "key": inj.get("key"),
                        "rank": inj.get("rank"),
                        "score": float(inj.get("score", 0.0) or 0.0),
                        "source": inj.get("source"),
                        "outcome": outcome_by_key.get(inj.get("key")),
                    }
                )
        return rows

    def injection_precision(self, since=None, limit=None) -> dict:
        """Fraction of injected-and-labeled memories that were acted on.

        Returns ``{injected, labeled, pending, acted, acted_rate,
        acted_or_used_rate, by_rank}`` where ``by_rank`` maps rank -> that
        rank's acted_rate over labeled injections.
        """
        rows = self._joined_injections(self.load_events(since=since, limit=limit))
        labeled = [r for r in rows if r["outcome"] is not None]
        acted = sum(1 for r in labeled if r["outcome"] in _ACTED_OUTCOMES)
        positive = sum(1 for r in labeled if r["outcome"] in _POSITIVE_OUTCOMES)
        n = len(labeled)

        by_rank: dict = {}
        rank_groups: dict = {}
        for r in labeled:
            rank_groups.setdefault(r["rank"], []).append(r)
        for rank, group in sorted(
            rank_groups.items(), key=lambda kv: (kv[0] is None, kv[0])
        ):
            acted_g = sum(1 for r in group if r["outcome"] in _ACTED_OUTCOMES)
            by_rank[rank] = {
                "labeled": len(group),
                "acted_rate": round(acted_g / len(group), 4) if group else 0.0,
            }

        return {
            "injected": len(rows),
            "labeled": n,
            "pending": len(rows) - n,
            "acted": acted,
            "acted_rate": round(acted / n, 4) if n else 0.0,
            "acted_or_used_rate": round(positive / n, 4) if n else 0.0,
            "by_rank": by_rank,
        }

    def confidence_calibration(self, since=None, limit=None) -> dict:
        """Acted-rate bucketed by injection-time score.

        Returns ``{buckets: [{label, labeled, acted, acted_rate}, ...]}`` over
        the 5 :data:`CALIBRATION_BUCKET_EDGES` buckets. A well-calibrated
        scorer shows acted_rate rising monotonically across buckets.
        """
        rows = self._joined_injections(self.load_events(since=since, limit=limit))
        labeled = [r for r in rows if r["outcome"] is not None]
        n_buckets = len(CALIBRATION_BUCKET_EDGES) + 1
        buckets = [{"labeled": 0, "acted": 0} for _ in range(n_buckets)]
        for r in labeled:
            b = _bucket_index(r["score"])
            buckets[b]["labeled"] += 1
            if r["outcome"] in _ACTED_OUTCOMES:
                buckets[b]["acted"] += 1
        return {
            "buckets": [
                {
                    "label": _bucket_label(i),
                    "labeled": b["labeled"],
                    "acted": b["acted"],
                    "acted_rate": (
                        round(b["acted"] / b["labeled"], 4) if b["labeled"] else 0.0
                    ),
                }
                for i, b in enumerate(buckets)
            ]
        }

    def decay_regret(self, since=None, limit=None) -> dict:
        """Injected-then-regretted vs injected-then-acted, by score bucket.

        "Regret" = injected but the outcome was ``dismissed`` or
        ``contradicted`` (surfaced but wrong). Contrasted with ``acted``. This
        is the first empirical feedback signal for the decay magic numbers.

        Returns ``{regret, acted, regret_rate, by_bucket: [...]}``.
        """
        rows = self._joined_injections(self.load_events(since=since, limit=limit))
        labeled = [r for r in rows if r["outcome"] is not None]
        regret = sum(1 for r in labeled if r["outcome"] in _REGRET_OUTCOMES)
        acted = sum(1 for r in labeled if r["outcome"] in _ACTED_OUTCOMES)

        n_buckets = len(CALIBRATION_BUCKET_EDGES) + 1
        by_bucket = [{"regret": 0, "acted": 0} for _ in range(n_buckets)]
        for r in labeled:
            b = _bucket_index(r["score"])
            if r["outcome"] in _REGRET_OUTCOMES:
                by_bucket[b]["regret"] += 1
            elif r["outcome"] in _ACTED_OUTCOMES:
                by_bucket[b]["acted"] += 1

        denom = regret + acted
        return {
            "regret": regret,
            "acted": acted,
            "regret_rate": round(regret / denom, 4) if denom else 0.0,
            "by_bucket": [
                {
                    "label": _bucket_label(i),
                    "regret": b["regret"],
                    "acted": b["acted"],
                }
                for i, b in enumerate(by_bucket)
            ],
        }

    def report(self, since=None, limit=None) -> str:
        """Render the v1 telemetry report as markdown.

        Style mirrors the sweep reports: a headline table plus per-analysis
        breakdowns. Read-only; safe to run against a live store.
        """
        events = self.load_events(since=since, limit=limit)
        precision = self.injection_precision(since=since, limit=limit)
        calibration = self.confidence_calibration(since=since, limit=limit)
        regret = self.decay_regret(since=since, limit=limit)

        scope = (
            f"agent_id={self.agent_id!r}" if self.agent_id is not None else "all agents"
        )
        lines = [
            "# Live-agent memory telemetry report",
            "",
            f"Scope: {scope} · events: {len(events)} · "
            f"injected: {precision['injected']} · labeled: {precision['labeled']} "
            f"· pending: {precision['pending']}",
            "",
            "## Injection precision",
            "",
            f"- acted_rate: {precision['acted_rate']}",
            f"- acted_or_used_rate: {precision['acted_or_used_rate']}",
            "",
            "| rank | labeled | acted_rate |",
            "|---|---|---|",
        ]
        for rank, stats in precision["by_rank"].items():
            lines.append(f"| {rank} | {stats['labeled']} | {stats['acted_rate']} |")

        lines += [
            "",
            "## Confidence calibration (acted-rate by injection score)",
            "",
            "| score bucket | labeled | acted | acted_rate |",
            "|---|---|---|---|",
        ]
        for b in calibration["buckets"]:
            lines.append(
                f"| {b['label']} | {b['labeled']} | {b['acted']} | {b['acted_rate']} |"
            )

        lines += [
            "",
            "## Decay regret (injected-then-wrong vs injected-then-acted)",
            "",
            f"- regret_rate: {regret['regret_rate']} "
            f"(regret={regret['regret']}, acted={regret['acted']})",
            "",
            "| score bucket | regret | acted |",
            "|---|---|---|",
        ]
        for b in regret["by_bucket"]:
            lines.append(f"| {b['label']} | {b['regret']} | {b['acted']} |")

        lines += [
            "",
            "_Fusion-disagreement and refusal-threshold analyses are fast-follows "
            "(the trace already stores the fused score). Not self-benchmarking: this "
            "is evidence for maintainer-reserved policy-default decisions._",
        ]
        return "\n".join(lines)
