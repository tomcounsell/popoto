"""Tests for AdaptiveAssembler — keep/revert loop over score_weights.

Covers:
- Unit-level state machine: keep when candidate_mean >= baseline, revert otherwise.
- Edge cases: quality_metric exceptions are logged not raised; None query_cues
  does not crash; the weight floor/ceiling stops degenerate proposals.
- Integration: across a 200-call run on a seeded scenario, the adaptive
  loop reliably yields >= 5% improvement in mean quality (measured as
  last-50 vs first-50 calls).
"""

import os
import random
import statistics
import sys
import uuid
from unittest.mock import MagicMock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402

from src.popoto.fields.confidence_field import ConfidenceField  # noqa: E402
from src.popoto.fields.constants import Defaults  # noqa: E402
from src.popoto.fields.existence_filter import ExistenceFilter  # noqa: E402
from src.popoto.fields.field import Field  # noqa: E402
from src.popoto.fields.shortcuts import (  # noqa: E402
    AutoKeyField,
    KeyField,
    SortedField,
)
from src.popoto.models.base import Model  # noqa: E402
from src.popoto.recipes.adaptive_assembler import (  # noqa: E402
    AdaptiveAssembler,
    _default_quality_metric,
)
from src.popoto.recipes.context_assembler import (  # noqa: E402
    AssemblyResult,
    ContextAssembler,
    RetrievalQuality,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory_model():
    """Build a fresh Memory model class with a unique suffix to avoid registry
    collisions across test runs.

    The model has:
    - ``agent_id``: partition key
    - ``topic``: indexed by ExistenceFilter (drives FOK cue_familiarity)
    - ``content``: plain content field
    - ``relevance``: SortedField with manually-assigned scores so the
      scenario can produce a misleading "relevance" signal for the
      adaptive loop to learn past.
    - ``confidence``: ConfidenceField (sorted index by score)
    """
    suffix = uuid.uuid4().hex[:8]
    class_name = f"AdaptiveMemory_{suffix}"

    attrs = {
        "__module__": __name__,
        "__qualname__": class_name,
        "memory_id": AutoKeyField(),
        "agent_id": KeyField(),
        "topic": Field(type=str),
        "content": Field(type=str),
        "relevance": SortedField(type=float, partition_by="agent_id"),
        "confidence": ConfidenceField(initial_confidence=0.1),
        "bloom": ExistenceFilter(
            error_rate=0.01,
            capacity=10_000,
            fingerprint_fn=lambda inst: inst.topic,
        ),
    }
    return type(class_name, (Model,), attrs)


def _quality(fok, conf):
    """Build a minimal RetrievalQuality for mocked assembles."""
    return RetrievalQuality(
        avg_confidence=conf,
        score_spread=0.0,
        fok_score=fok,
        staleness_ratio=0.0,
    )


def _mock_inner(score_weights, quality: RetrievalQuality | None) -> ContextAssembler:
    """Fake a ContextAssembler that returns a fixed quality on assemble()."""
    mock = MagicMock(spec=ContextAssembler)
    mock.score_weights = dict(score_weights)
    md = {}
    if quality is not None:
        md["quality"] = quality

    def _assemble(query_cues=None, **kwargs):
        return AssemblyResult(records=[], proactive=[], formatted="", metadata=dict(md))

    mock.assemble.side_effect = _assemble
    return mock


# ---------------------------------------------------------------------------
# Unit-level tests
# ---------------------------------------------------------------------------


class TestDefaultQualityMetric:
    def test_product_of_fok_and_confidence(self):
        q = _quality(0.6, 0.5)
        assert _default_quality_metric(q) == pytest.approx(0.3)

    def test_zero_component_zeroes_metric(self):
        q = _quality(0.0, 0.9)
        assert _default_quality_metric(q) == 0.0


class TestProposalAndClamp:
    def test_propose_respects_floor_ceiling(self):
        # Original weights with one at the floor boundary.
        base = ContextAssembler.__new__(ContextAssembler)
        base.score_weights = {"a": 0.05, "b": 0.5, "c": 0.45}
        base.max_items = 10
        # Use our wrapper to access _propose_candidate behavior.
        adaptive = AdaptiveAssembler(
            inner=_mock_inner(base.score_weights, None),
            window_size=5,
            rng=random.Random(7),
            weight_perturbation=0.05,
        )
        # 50 proposals; each valid one must keep all weights in [0.05, 0.9].
        for _ in range(50):
            cand = adaptive._propose_candidate()
            if cand is None:
                continue
            for v in cand.values():
                assert 0.05 <= v <= 0.9

    def test_propose_returns_none_when_single_key(self):
        adaptive = AdaptiveAssembler(
            inner=_mock_inner({"only": 1.0}, None),
            window_size=3,
            rng=random.Random(0),
        )
        assert adaptive._propose_candidate() is None


class TestQualityMetricExceptionIsLoggedNotRaised:
    def test_metric_raising_does_not_crash_assemble(self, caplog):
        def bad_metric(q):
            raise RuntimeError("boom")

        inner = _mock_inner(
            {"relevance": 0.6, "confidence": 0.4},
            _quality(0.5, 0.5),
        )
        adaptive = AdaptiveAssembler(
            inner=inner,
            window_size=3,
            quality_metric=bad_metric,
            rng=random.Random(1),
        )
        import logging as _logging

        with caplog.at_level(_logging.WARNING):
            result = adaptive.assemble(query_cues={"topic": "x"})
        # Inner was called and returned; no exception propagated.
        assert isinstance(result, AssemblyResult)
        # Sample was skipped — no windows advanced.
        assert adaptive._current_window == []
        assert any("quality_metric" in rec.message for rec in caplog.records)


class TestEmptyQueryCuesSkipsSample:
    def test_quality_none_skips_sample(self):
        # Inner returns AssemblyResult with NO 'quality' metadata — the
        # AdaptiveAssembler should skip gracefully.
        inner = _mock_inner({"relevance": 0.5, "confidence": 0.5}, quality=None)
        adaptive = AdaptiveAssembler(
            inner=inner,
            window_size=3,
            rng=random.Random(0),
        )
        result = adaptive.assemble(query_cues=None)
        assert isinstance(result, AssemblyResult)
        assert adaptive._current_window == []


class TestKeepRevertStateMachine:
    """Unit tests for the keep/revert dispatch, using a mocked inner."""

    def test_keep_when_candidate_improves(self):
        # Scenario: baseline window fills with 0.2s, candidate window with 0.8s.
        # After candidate window, the state machine should keep the candidate.
        window = 3
        inner_baseline = _mock_inner(
            {"relevance": 0.6, "confidence": 0.4},
            _quality(0.5, 0.4),  # score = 0.20
        )
        adaptive = AdaptiveAssembler(
            inner=inner_baseline,
            window_size=window,
            rng=random.Random(0),
        )
        # Fill baseline window — triggers candidate proposal after window N.
        for _ in range(window):
            adaptive.assemble(query_cues={"x": 1})
        assert adaptive.is_testing_candidate
        # Now monkeypatch inner to report a higher quality for the candidate.
        adaptive.inner = _mock_inner(
            adaptive.current_weights,
            _quality(0.8, 0.8),  # score = 0.64
        )
        for _ in range(window):
            adaptive.assemble(query_cues={"x": 1})
        # After the second window, candidate_mean=0.64 >= baseline=0.20 -> keep.
        assert not adaptive.is_testing_candidate
        assert adaptive.baseline_quality is not None
        assert adaptive.baseline_quality == pytest.approx(0.64)

    def test_revert_when_candidate_worse(self):
        window = 3
        adaptive = AdaptiveAssembler(
            inner=_mock_inner(
                {"relevance": 0.6, "confidence": 0.4},
                _quality(0.8, 0.8),  # baseline score = 0.64
            ),
            window_size=window,
            rng=random.Random(0),
        )
        baseline_weights = dict(adaptive.current_weights)
        for _ in range(window):
            adaptive.assemble(query_cues={"x": 1})
        assert adaptive.is_testing_candidate
        # Swap inner to produce a weaker quality for candidate.
        adaptive.inner = _mock_inner(
            adaptive.current_weights,
            _quality(0.3, 0.3),  # score = 0.09
        )
        for _ in range(window):
            adaptive.assemble(query_cues={"x": 1})
        # Candidate_mean=0.09 < baseline=0.64 -> revert.
        assert not adaptive.is_testing_candidate
        # Weights reverted to the original baseline.
        assert adaptive.current_weights == baseline_weights
        # Baseline quality unchanged by the revert.
        assert adaptive.baseline_quality == pytest.approx(0.64)


class TestAdaptiveAssemblerProperties:
    def test_current_weights_returns_copy(self):
        inner = _mock_inner({"a": 0.5, "b": 0.5}, _quality(0.5, 0.5))
        adaptive = AdaptiveAssembler(inner=inner, window_size=3)
        w = adaptive.current_weights
        w["a"] = 999.0
        # Mutation of returned dict does not affect internal state.
        assert adaptive.current_weights["a"] != 999.0

    def test_default_window_size_uses_defaults(self):
        inner = _mock_inner({"a": 0.5, "b": 0.5}, _quality(0.5, 0.5))
        adaptive = AdaptiveAssembler(inner=inner)
        assert adaptive._window_size == Defaults.ADAPTIVE_QUALITY_WINDOW_SIZE


# ---------------------------------------------------------------------------
# Integration: AdaptiveAssembler beats fixed-weight baseline
# ---------------------------------------------------------------------------


class TestAdaptiveAssemblerIntegration:
    """End-to-end: adaptive loop improves a seeded scenario's quality signal.

    Scenario design: a ``Memory`` model with ConfidenceField + DecayingSortedField
    + ExistenceFilter. 40 topics are seeded. Half the memories have been
    positively reinforced (high confidence) and half have not. The ground-truth
    optimum weights skew toward confidence; the baseline weights skew toward
    relevance. The adaptive loop should find the confidence-heavy direction.

    Pass condition (per plan):
        mean improvement across 5 trials >= 5% AND stdev <= 2%,
        OR at least 4 of 5 trials individually show >= 5% improvement.
    """

    def _seed(self, MemoryCls, rng):
        """Save 40 memories with a misleading relevance signal.

        All records get a random ``relevance`` score. Only the first 15
        records receive positive confidence updates. The scenario design:
        ``relevance`` is a noisy/misleading signal, and ``confidence``
        reliably identifies the 15 "good" memories. An assembler that
        weights confidence higher produces better context.

        Why 15 good out of 40 (not 5 or 10): when there are exactly
        ``max_items`` good records, every retrieval surfaces all the
        good records regardless of weights and fixed == adaptive
        becomes identical. 15 good > 5 max_items creates enough
        differentiation for weight shifts to change selection.
        """
        for i in range(40):
            m = MemoryCls(
                agent_id="agent-1",
                topic=f"topic-{i}",
                content=f"content-{i}",
                relevance=rng.random(),
            )
            m.save()
            if i < 15:
                # "Good" records: repeated positive confidence signal.
                for _ in range(5):
                    ConfidenceField.update_confidence(m, "confidence", signal=0.98)
            # Remaining 25 stay at initial_confidence=0.1.

    def _run_trial(self, seed):
        """One trial: build an AdaptiveAssembler, run 500 calls, return
        ``(first_50_mean, last_50_mean, improvement)``.

        Comparing the first 50 to the last 50 calls of the same adaptive
        run measures whether the keep/revert loop has climbed to a better
        baseline after enough cycles. 500 calls at window=10 yields
        ~25 proposal/evaluate cycles — plenty of opportunity for the loop
        to converge on confidence-heavy weights.
        """
        MemoryCls = _make_memory_model()
        self._seed(MemoryCls, random.Random(seed))

        # Initial weights over-index on the misleading `relevance` signal.
        # Ground-truth optimum skews toward `confidence`.
        inner = ContextAssembler(
            model_class=MemoryCls,
            score_weights={"relevance": 0.9, "confidence": 0.1},
            max_items=5,
            surfacing_threshold=0.0,
        )
        adaptive = AdaptiveAssembler(
            inner=inner,
            window_size=10,
            weight_perturbation=0.1,
            rng=random.Random(seed + 1000),
        )

        query_rng = random.Random(seed)
        topics = [f"topic-{i}" for i in range(40)]
        scores = []
        for _ in range(500):
            topic = topics[query_rng.randrange(len(topics))]
            result = adaptive.assemble(
                query_cues={"topic": topic},
                partition_filters={"agent_id": "agent-1"},
            )
            q = result.metadata.get("quality")
            if q is not None:
                scores.append(_default_quality_metric(q))

        assert len(scores) >= 400, f"expected plenty of samples, got {len(scores)}"
        first_50 = statistics.fmean(scores[:50])
        last_50 = statistics.fmean(scores[-50:])
        improvement = (last_50 - first_50) / first_50 if first_50 > 1e-9 else 0.0
        return first_50, last_50, improvement

    def test_adaptive_beats_fixed_weights_reliable(self):
        improvements = []
        for trial in range(5):
            _first, _last, imp = self._run_trial(seed=42 + trial)
            improvements.append(imp)

        mean_imp = statistics.fmean(improvements)
        std_imp = statistics.pstdev(improvements) if len(improvements) > 1 else 0.0
        runs_above_5pct = sum(1 for x in improvements if x >= 0.05)

        reliable_mean = mean_imp >= 0.05 and std_imp <= 0.02
        reliable_majority = runs_above_5pct >= 4

        assert reliable_mean or reliable_majority, (
            f"improvements={improvements} mean={mean_imp:.4f} stdev={std_imp:.4f} "
            f"runs_above_5pct={runs_above_5pct}"
        )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestAdaptiveAssemblerEdgeCases:
    def test_inner_exception_does_not_corrupt_window_state(self):
        """If inner.assemble() raises, window state must be unchanged (no partial append)."""
        # Build a mock inner that raises RuntimeError on every call.
        from unittest.mock import MagicMock

        inner = MagicMock(spec=ContextAssembler)
        inner.score_weights = {"relevance": 0.6, "confidence": 0.4}
        inner.assemble.side_effect = RuntimeError("inner failure")

        adaptive = AdaptiveAssembler(
            inner=inner,
            window_size=3,
            rng=random.Random(0),
        )

        # The exception must propagate — inner exceptions are not quality_metric
        # exceptions and must NOT be swallowed.
        with pytest.raises(RuntimeError, match="inner failure"):
            adaptive.assemble(query_cues={"topic": "x"})

        # No partial append happened before the raise — window state is clean.
        assert len(adaptive._current_window) == 0
