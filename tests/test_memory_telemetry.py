"""Tests for the live-agent memory telemetry recipe (issue #464).

Covers the event-record write, the opt-in content-capture flag, the
off-by-default ``emit_trace`` hook on ``ContextAssembler.assemble()``, the
fail-open guarantee, sampling, the ``report_outcomes()`` join, the offline
analyzer, and overhead reporting.

Uses the pytest plugin's DB-15 isolation (each test gets a clean DB); no manual
flush fixture is needed.
"""

import random

import pytest

from popoto import KeyField, Model, SortedField, StringField
from popoto.recipes.context_assembler import ContextAssembler
from popoto.recipes.memory_telemetry import (
    DEFAULT_EVENT_TTL,
    AssemblyEvent,
    TelemetryAnalyzer,
    TelemetryRecorder,
    report_outcomes,
)
from popoto.redis_db import POPOTO_REDIS_DB


class TeleMem(Model):
    mem_id = KeyField()
    agent_id = KeyField(null=True)
    content = StringField(null=True)
    # Plain SortedField with scores in [0, 1] so calibration buckets are
    # deterministic; partition_by exercises the partition-aware score capture.
    relevance = SortedField(type=float, partition_by="agent_id")


# Descending relevance so top-k selection is deterministic.
_SCORES = (0.9, 0.7, 0.5, 0.3, 0.1)


def _seed(agent="a1", scores=_SCORES):
    mems = []
    for i, s in enumerate(scores):
        m = TeleMem(mem_id=f"m{i}", agent_id=agent, content=f"note {i}", relevance=s)
        m.save()
        mems.append(m)
    return mems


def _assembler(max_items=3):
    return ContextAssembler(
        TeleMem, score_weights={"relevance": 1.0}, max_items=max_items
    )


# ---------------------------------------------------------------------------
# 1. Event write, ids-only
# ---------------------------------------------------------------------------


def test_event_write_ids_only():
    _seed()
    rec = TelemetryRecorder(_assembler(), capture="ids")
    result = rec.assemble({"topic": "note"}, agent_id="a1")

    assert len(result.records) == 3
    event_id = result.metadata["telemetry_event_id"]

    events = list(AssemblyEvent.query.all())
    assert len(events) == 1
    ev = events[0]

    assert ev.event_id == event_id
    assert ev.agent_id == "a1"
    assert ev.model_name == "TeleMem"
    assert ev.retrieval_mode == "composite"
    assert ev.capture == "ids"
    assert ev.budget_items == 3
    assert ev.budget_tokens >= 0
    assert ev.latency_ms is not None and ev.latency_ms >= 0.0

    # Injected set: ids/ranks/scores/source, in descending-score rank order.
    assert [i["rank"] for i in ev.injected] == [0, 1, 2]
    assert [i["source"] for i in ev.injected] == ["pull", "pull", "pull"]
    scores = [i["score"] for i in ev.injected]
    # Partition-aware capture returns the real (non-zero) sorted-field scores.
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > 0.0
    # ids-only: no content, no query_text.
    assert all("content" not in i for i in ev.injected)
    assert ev.query_text is None

    # TTL is set (mandatory bound).
    ttl = POPOTO_REDIS_DB.ttl(ev.db_key.redis_key)
    assert 0 < ttl <= DEFAULT_EVENT_TTL


# ---------------------------------------------------------------------------
# 2. Content capture opt-in
# ---------------------------------------------------------------------------


def test_content_capture_opt_in():
    _seed()
    rec = TelemetryRecorder(_assembler(), capture="content")
    result = rec.assemble({"topic": "note"}, agent_id="a1")
    ev = AssemblyEvent.query.get(event_id=result.metadata["telemetry_event_id"])

    assert ev.capture == "content"
    assert ev.query_text == "note"
    assert all("content" in i for i in ev.injected)
    assert ev.injected[0]["content"]["content"].startswith("note")


def test_ids_capture_never_leaks_content():
    _seed()
    rec = TelemetryRecorder(_assembler(), capture="ids")  # default is also ids
    result = rec.assemble({"topic": "note"}, agent_id="a1")
    ev = AssemblyEvent.query.get(event_id=result.metadata["telemetry_event_id"])
    assert ev.query_text is None
    assert all("content" not in i for i in ev.injected)


def test_unknown_capture_raises():
    with pytest.raises(ValueError):
        TelemetryRecorder(_assembler(), capture="everything")


def test_default_capture_is_ids():
    rec = TelemetryRecorder(_assembler())
    assert rec.capture == "ids"


# ---------------------------------------------------------------------------
# 3. emit_trace parity
# ---------------------------------------------------------------------------


def test_emit_trace_off_by_default_is_inert():
    _seed()
    asm = _assembler()
    baseline = asm.assemble({"topic": "note"}, agent_id="a1")
    explicit_off = asm.assemble({"topic": "note"}, agent_id="a1", emit_trace=False)

    assert "trace" not in baseline.metadata
    assert "trace" not in explicit_off.metadata
    # Same metadata key set AND values with and without the (off) kwarg
    # (timing_ms is wall-clock and varies run-to-run, so exclude it).
    assert set(baseline.metadata) == set(explicit_off.metadata)
    volatile = {"timing_ms"}
    for k in set(baseline.metadata) - volatile:
        assert baseline.metadata[k] == explicit_off.metadata[k]


def test_emit_trace_adds_only_trace():
    _seed()
    asm = _assembler()
    off = asm.assemble({"topic": "note"}, agent_id="a1")
    on = asm.assemble({"topic": "note"}, agent_id="a1", emit_trace=True)

    assert set(on.metadata) - set(off.metadata) == {"trace"}
    trace = on.metadata["trace"]
    assert len(trace) == len(on.records)
    for rank, entry in enumerate(trace):
        assert set(entry) == {"key", "rank", "score", "source"}
        assert entry["rank"] == rank
        assert entry["source"] in ("pull", "push")


# ---------------------------------------------------------------------------
# 4. Fail-open
# ---------------------------------------------------------------------------


def test_recorder_is_fail_open(monkeypatch):
    _seed()

    def _boom(self, *a, **k):
        raise RuntimeError("redis exploded")

    monkeypatch.setattr(AssemblyEvent, "save", _boom, raising=True)
    rec = TelemetryRecorder(_assembler(), capture="ids")

    # Must not raise, must return the real assembly result.
    result = rec.assemble({"topic": "note"}, agent_id="a1")
    assert len(result.records) == 3
    # No event id was recorded because the write failed.
    assert "telemetry_event_id" not in result.metadata
    # Overhead was still measured (finally-block).
    assert rec.overhead_stats["count"] == 1


# ---------------------------------------------------------------------------
# 5. Sampling
# ---------------------------------------------------------------------------


def test_sample_rate_zero_records_nothing():
    _seed()
    rec = TelemetryRecorder(_assembler(), sample_rate=0.0)
    for _ in range(5):
        rec.assemble({"topic": "note"}, agent_id="a1")
    assert len(list(AssemblyEvent.query.all())) == 0


def test_sample_rate_one_records_every_call():
    _seed()
    rec = TelemetryRecorder(_assembler(), sample_rate=1.0)
    for _ in range(5):
        rec.assemble({"topic": "note"}, agent_id="a1")
    assert len(list(AssemblyEvent.query.all())) == 5


def test_sample_rate_fractional_is_deterministic_with_seeded_rng():
    _seed()
    rec = TelemetryRecorder(_assembler(), sample_rate=0.5, rng=random.Random(1234))
    for _ in range(20):
        rec.assemble({"topic": "note"}, agent_id="a1")
    n = len(list(AssemblyEvent.query.all()))
    # Some but not all calls sampled; the exact count is reproducible.
    assert 0 < n < 20


def test_sampled_out_calls_skip_trace_cost():
    # When a call is not sampled, emit_trace must not be forced on the inner
    # assembler, so no trace-proxy work is done (sample_rate protects latency).
    _seed()
    rec = TelemetryRecorder(_assembler(), sample_rate=0.0)
    result = rec.assemble({"topic": "note"}, agent_id="a1")
    assert "trace" not in result.metadata
    assert "telemetry_event_id" not in result.metadata


def test_invalid_sample_rate_raises():
    with pytest.raises(ValueError):
        TelemetryRecorder(_assembler(), sample_rate=1.5)


# ---------------------------------------------------------------------------
# 6. Outcome join
# ---------------------------------------------------------------------------


def test_report_outcomes_joins_injected_keys():
    _seed()
    rec = TelemetryRecorder(_assembler())
    result = rec.assemble({"topic": "note"}, agent_id="a1")
    event_id = result.metadata["telemetry_event_id"]
    injected_keys = [i["key"] for i in result.metadata["trace"]]

    updated = report_outcomes(
        event_id,
        {injected_keys[0]: "acted", injected_keys[1]: "dismissed"},
    )
    assert updated is not None
    outcomes = {o["key"]: o["outcome"] for o in updated.outcomes}
    assert outcomes == {
        injected_keys[0]: "acted",
        injected_keys[1]: "dismissed",
    }
    assert all("at" in o for o in updated.outcomes)


def test_report_outcomes_ignores_non_injected_keys():
    _seed()
    rec = TelemetryRecorder(_assembler())
    result = rec.assemble({"topic": "note"}, agent_id="a1")
    event_id = result.metadata["telemetry_event_id"]

    updated = report_outcomes(event_id, {"TeleMem:a1:not-injected": "acted"})
    assert updated.outcomes == []


def test_report_outcomes_invalid_outcome_raises():
    _seed()
    rec = TelemetryRecorder(_assembler())
    result = rec.assemble({"topic": "note"}, agent_id="a1")
    event_id = result.metadata["telemetry_event_id"]
    with pytest.raises(ValueError):
        report_outcomes(event_id, {"TeleMem:a1:m0": "loved-it"})


def test_report_outcomes_missing_event_is_noop():
    # No event with this id exists (e.g., TTL expired).
    assert report_outcomes("does-not-exist", {"TeleMem:a1:m0": "acted"}) is None


# ---------------------------------------------------------------------------
# 7. Offline analyzer
# ---------------------------------------------------------------------------


def _record_with_outcomes(agent, outcomes_by_rank):
    """Assemble once and join a per-rank outcome map. Returns injected keys."""
    rec = TelemetryRecorder(_assembler(), capture="ids")
    result = rec.assemble({"topic": "note"}, agent_id=agent)
    injected = result.metadata["trace"]
    outcome_map = {
        injected[rank]["key"]: outcome
        for rank, outcome in outcomes_by_rank.items()
        if rank < len(injected)
    }
    report_outcomes(result.metadata["telemetry_event_id"], outcome_map)
    return injected


def test_analyzer_injection_precision():
    _seed("a1")
    # ranks: 0.9->acted, 0.7->acted, 0.5->dismissed
    _record_with_outcomes("a1", {0: "acted", 1: "acted", 2: "dismissed"})

    an = TelemetryAnalyzer(agent_id="a1")
    prec = an.injection_precision()
    assert prec["injected"] == 3
    assert prec["labeled"] == 3
    assert prec["pending"] == 0
    assert prec["acted"] == 2
    assert prec["acted_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert prec["by_rank"][0]["acted_rate"] == 1.0
    assert prec["by_rank"][2]["acted_rate"] == 0.0


def test_analyzer_confidence_calibration_buckets_by_score():
    _seed("a1")
    # scores 0.9 (bucket [0.8,inf)), 0.7 ([0.6,0.8)), 0.5 ([0.4,0.6))
    _record_with_outcomes("a1", {0: "acted", 1: "acted", 2: "dismissed"})

    an = TelemetryAnalyzer(agent_id="a1")
    buckets = {b["label"]: b for b in an.confidence_calibration()["buckets"]}
    assert buckets["[0.8, inf)"]["labeled"] == 1
    assert buckets["[0.8, inf)"]["acted_rate"] == 1.0
    assert buckets["[0.6, 0.8)"]["labeled"] == 1
    assert buckets["[0.6, 0.8)"]["acted_rate"] == 1.0
    assert buckets["[0.4, 0.6)"]["labeled"] == 1
    assert buckets["[0.4, 0.6)"]["acted_rate"] == 0.0


def test_analyzer_decay_regret():
    _seed("a1")
    _record_with_outcomes("a1", {0: "acted", 1: "contradicted", 2: "dismissed"})

    an = TelemetryAnalyzer(agent_id="a1")
    regret = an.decay_regret()
    assert regret["acted"] == 1
    assert regret["regret"] == 2  # contradicted + dismissed
    assert regret["regret_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_analyzer_report_is_markdown():
    _seed("a1")
    _record_with_outcomes("a1", {0: "acted", 1: "dismissed"})
    report = TelemetryAnalyzer(agent_id="a1").report()
    assert "# Live-agent memory telemetry report" in report
    assert "## Injection precision" in report
    assert "## Confidence calibration" in report
    assert "## Decay regret" in report


def test_analyzer_partition_scopes_events():
    _seed("a1")
    _seed("b2")
    _record_with_outcomes("a1", {0: "acted"})
    _record_with_outcomes("b2", {0: "dismissed"})

    a_events = TelemetryAnalyzer(agent_id="a1").load_events()
    assert len(a_events) == 1
    assert all(e.agent_id == "a1" for e in a_events)


# ---------------------------------------------------------------------------
# 8. Overhead reported
# ---------------------------------------------------------------------------


def test_overhead_stats_populated():
    _seed()
    rec = TelemetryRecorder(_assembler())
    for _ in range(4):
        rec.assemble({"topic": "note"}, agent_id="a1")
    stats = rec.overhead_stats
    assert stats["count"] == 4
    assert stats["mean_ms"] >= 0.0
    assert stats["p95_ms"] >= 0.0
    assert stats["max_ms"] >= stats["mean_ms"]


def test_overhead_stats_empty_when_no_samples():
    rec = TelemetryRecorder(_assembler(), sample_rate=0.0)
    assert rec.overhead_stats == {
        "count": 0,
        "mean_ms": 0.0,
        "p95_ms": 0.0,
        "max_ms": 0.0,
    }
