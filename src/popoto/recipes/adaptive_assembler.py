"""AdaptiveAssembler — autoresearch-style keep/revert loop over score_weights.

Wraps a ``ContextAssembler`` and adjusts its ``score_weights`` over time via
a rolling-window quality comparison. Inspired by karpathy/autoresearch:
each iteration proposes a small weight perturbation, measures quality over
the next ``window_size`` calls, and *keeps* the change if quality improved
or *reverts* if it didn't. No ML training, no Redis state — pure in-memory
bookkeeping per process.

Design properties:

* **Optional and opt-in.** The baseline ``ContextAssembler`` is the
  recommended default; ``AdaptiveAssembler`` is a layer for agents that
  want online adaptation.
* **Single-threaded by design.** The rolling-window bookkeeping
  (``_current_window`` / ``_candidate_window`` / ``_baseline_quality``)
  is NOT atomic across concurrent calls and this class deliberately does
  not add locks. Multi-threaded agents must hold their own
  ``AdaptiveAssembler`` per thread.
* **Per-process only.** Adaptation does not survive process restarts.
  Matches the autoresearch pattern where each session's learnings are
  reflected in its final ``score_weights``.
* **Mechanical, not model-driven.** The quality metric is a pure function
  over ``RetrievalQuality``; no LLM self-reporting.

Example:
    from popoto.recipes.adaptive_assembler import AdaptiveAssembler
    from popoto.recipes.context_assembler import ContextAssembler

    inner = ContextAssembler(
        model_class=Memory,
        score_weights={"relevance": 0.6, "confidence": 0.3, "recency": 0.1},
        max_items=10,
    )
    adaptive = AdaptiveAssembler(inner, window_size=20)
    for cues in stream_of_queries:
        result = adaptive.assemble(cues)  # delegates + records quality
        ...  # use result.records
    # adaptive.current_weights now reflects any kept improvements
"""

from __future__ import annotations

import copy
import logging
import random
import statistics
from collections.abc import Callable

from ..fields.constants import Defaults
from .context_assembler import AssemblyResult, ContextAssembler, RetrievalQuality

logger = logging.getLogger(__name__)


# Hard floor/ceiling for individual weight values. Per plan Risk 2 mitigation:
# prevents the keep/revert loop from converging on degenerate all-zero or
# all-one weights that would optimize the quality proxy at the cost of
# downstream utility (Goodhart's Law).
_WEIGHT_FLOOR = 0.05
_WEIGHT_CEILING = 0.9


def _default_quality_metric(q: RetrievalQuality) -> float:
    """Default scalarization: fok_score * avg_confidence.

    Product punishes imbalance multiplicatively — you need *both* high
    FOK and high confidence for the signal to be strong.
    """
    return q.fok_score * q.avg_confidence


class AdaptiveAssembler:
    """Wraps a ``ContextAssembler`` with a keep/revert quality loop.

    Every ``window_size`` calls under the current baseline weights, the
    assembler proposes a symmetric perturbation (shift
    ``weight_perturbation`` from one weight key to another). It then
    gathers another ``window_size`` samples under the candidate, compares
    the rolling means, and either keeps the candidate as the new baseline
    or reverts to the original.

    Args:
        inner: An existing ``ContextAssembler`` to wrap. The
            ``AdaptiveAssembler`` may swap this out with a re-constructed
            instance when changing weights; callers should access weights
            via ``self.current_weights`` rather than capturing a direct
            reference to ``inner``.
        window_size: Number of calls per rolling window. Smaller windows
            adapt faster but noisier; larger windows converge more slowly
            but more reliably. Default
            ``Defaults.ADAPTIVE_QUALITY_WINDOW_SIZE`` (20).
        quality_metric: Callable that scalarizes a ``RetrievalQuality``
            into a single float. Default ``fok_score * avg_confidence``.
            If the metric raises on a given quality, the sample is logged
            and skipped rather than crashing the loop.
        weight_perturbation: How much weight to shift per proposal.
            Default 0.05.
        rng: Optional ``random.Random`` instance for deterministic tests.
            Defaults to a fresh ``random.Random()`` (non-seeded).
    """

    def __init__(
        self,
        inner: ContextAssembler,
        window_size: int = Defaults.ADAPTIVE_QUALITY_WINDOW_SIZE,
        quality_metric: Callable[[RetrievalQuality], float] | None = None,
        weight_perturbation: float = 0.05,
        rng: random.Random | None = None,
    ):
        self.inner = inner
        self._window_size = int(window_size)
        self._quality_metric = quality_metric or _default_quality_metric
        self._weight_perturbation = float(weight_perturbation)
        self._rng = rng if rng is not None else random.Random()

        # Snapshot the starting weights as the "original" to revert to.
        self._original_weights: dict[str, float] = dict(inner.score_weights)
        self._current_window: list[float] = []
        self._candidate_weights: dict[str, float] | None = None
        self._candidate_window: list[float] = []
        self._baseline_quality: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def assemble(self, query_cues=None, **kwargs) -> AssemblyResult:
        """Delegate to the wrapped assembler, recording quality per call.

        Forces ``assess_quality=True`` (overriding any caller kwarg) so
        quality is always computed. Appends the scalarized metric to
        whichever rolling window is active (baseline or candidate), then
        checks whether a state transition is due.

        Returns the inner ``AssemblyResult`` unchanged.
        """
        kwargs["assess_quality"] = True
        result = self.inner.assemble(query_cues=query_cues, **kwargs)

        quality = result.metadata.get("quality")
        if quality is None:
            # Defensive: inner didn't produce a RetrievalQuality (e.g.,
            # _compute_quality swallowed an exception and attached a bare
            # one, or assess_quality was somehow dropped). Skip sample.
            return result

        try:
            score = float(self._quality_metric(quality))
        except Exception as e:
            logger.warning(
                "quality_metric raised on %r — skipping sample: %s", quality, e
            )
            return result

        if self._candidate_weights is None:
            self._current_window.append(score)
        else:
            self._candidate_window.append(score)

        self._maybe_advance()
        return result

    @property
    def current_weights(self) -> dict[str, float]:
        """Return a copy of the currently-active score_weights."""
        return dict(self.inner.score_weights)

    @property
    def baseline_quality(self) -> float | None:
        """Rolling-window mean quality under baseline weights, or None."""
        return self._baseline_quality

    @property
    def is_testing_candidate(self) -> bool:
        """True when the assembler is currently gathering a candidate window."""
        return self._candidate_weights is not None

    # ------------------------------------------------------------------
    # Internal state machine
    # ------------------------------------------------------------------

    def _maybe_advance(self) -> None:
        """Advance the keep/revert state machine if a window just filled."""
        if self._candidate_weights is None:
            if len(self._current_window) >= self._window_size:
                self._baseline_quality = statistics.fmean(self._current_window)
                self._current_window.clear()
                self._start_candidate()
            return

        # candidate is under test
        if len(self._candidate_window) >= self._window_size:
            candidate_mean = statistics.fmean(self._candidate_window)
            baseline = self._baseline_quality
            if baseline is not None and candidate_mean >= baseline:
                self._keep_candidate(candidate_mean)
            else:
                self._revert_candidate()

    def _start_candidate(self) -> None:
        """Propose a candidate weight perturbation and swap inner."""
        candidate = self._propose_candidate()
        if candidate is None:
            # No valid perturbation found this round; retry next window.
            return
        self._candidate_weights = candidate
        self.inner = self._construct_inner(candidate)

    def _keep_candidate(self, candidate_mean: float) -> None:
        """Accept the candidate as the new baseline."""
        assert self._candidate_weights is not None
        logger.debug(
            "AdaptiveAssembler: keep candidate (%.4f >= %.4f baseline)",
            candidate_mean,
            self._baseline_quality if self._baseline_quality is not None else 0.0,
        )
        self._original_weights = dict(self._candidate_weights)
        self._baseline_quality = candidate_mean
        self._candidate_weights = None
        self._candidate_window.clear()
        self._current_window.clear()
        # inner already has the candidate weights — no swap needed.

    def _revert_candidate(self) -> None:
        """Reject the candidate; restore inner to the baseline weights."""
        logger.debug(
            "AdaptiveAssembler: revert candidate (baseline=%.4f)",
            self._baseline_quality if self._baseline_quality is not None else 0.0,
        )
        self.inner = self._construct_inner(self._original_weights)
        self._candidate_weights = None
        self._candidate_window.clear()
        self._current_window.clear()

    # ------------------------------------------------------------------
    # Proposal + construction helpers
    # ------------------------------------------------------------------

    def _propose_candidate(self) -> dict[str, float] | None:
        """Symmetric perturbation: shift weight_perturbation between two keys.

        Returns a new weights dict or None if clamping makes every
        proposal a no-op after several retries.
        """
        keys = list(self._original_weights.keys())
        if len(keys) < 2:
            return None

        for _ in range(3):
            key_from, key_to = self._rng.sample(keys, 2)
            candidate = dict(self._original_weights)
            raw_from = candidate[key_from] - self._weight_perturbation
            raw_to = candidate[key_to] + self._weight_perturbation
            new_from = max(_WEIGHT_FLOOR, min(_WEIGHT_CEILING, raw_from))
            new_to = max(_WEIGHT_FLOOR, min(_WEIGHT_CEILING, raw_to))
            # If clamping made the proposal a no-op (e.g., key_from was
            # already at the floor), retry with another pair.
            if new_from == candidate[key_from] and new_to == candidate[key_to]:
                continue
            candidate[key_from] = new_from
            candidate[key_to] = new_to
            return candidate
        return None

    def _construct_inner(self, score_weights: dict[str, float]) -> ContextAssembler:
        """Build a new ContextAssembler with the given score_weights.

        We shallow-copy ``self.inner`` and overwrite ``score_weights`` on
        the copy. This is simpler and more robust than snapshotting every
        constructor arg (the assembler has ~8 knobs and caches detected
        field capabilities on the instance). The copy preserves the
        detected-field cache — detected capabilities are a function of
        ``model_class``, not weights, so they are safe to share.
        """
        new_inner = copy.copy(self.inner)
        new_inner.score_weights = dict(score_weights)
        return new_inner
