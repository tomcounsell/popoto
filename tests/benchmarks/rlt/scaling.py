"""Scaling-curve runner — latency vs. corpus size, 10^3 -> 2x10^4 (#460).

Rebuilds (does not incrementally grow) the corpus fresh at each size — this
avoids decay-state drift between size points and keeps each point an
independent, reproducible measurement. Sizes beyond the maintainer's 20k
scale ceiling are supported (the function accepts any list of sizes) but are
never exercised by the default/CI path — informational only per the issue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from src.popoto.recipes.context_assembler import ContextAssembler

from .corpus import build_corpus, teardown_corpus
from .latency import LatencyReport, measure_latency

#: Default scaling-curve sizes spanning the maintainer's scale target
#: (10^3 -> 2x10^4). A benchmark tuning constant, not user config.
DEFAULT_CORPUS_SIZES = (1_000, 5_000, 10_000, 20_000)

#: Score weights for the composite fallback path (unused when BM25Field is
#: present, which it always is for RLT's corpus — kept for API parity with
#: ``ContextAssembler``'s constructor).
_SCORE_WEIGHTS = {"importance": 1.0}


@dataclass
class ScalingPoint:
    """One (corpus_size, latency) point on the scaling curve."""

    corpus_size: int
    latency: LatencyReport

    def to_dict(self) -> dict:
        return {"corpus_size": self.corpus_size, "latency": self.latency.to_dict()}


def run_scaling_curve(
    corpus_sizes: Sequence[int] = DEFAULT_CORPUS_SIZES,
    query_count: int = 10,
    seed: int = 0,
    max_items: int = 20,
    latency_repeats: int = 20,
) -> List[ScalingPoint]:
    """Build a fresh corpus at each size in ``corpus_sizes`` and measure latency.

    Args:
        corpus_sizes: Corpus sizes to measure, in order. Empty list returns
            ``[]`` with no corpus built — not an error.
        query_count: Queries per corpus (see :func:`~.corpus.build_corpus`).
        seed: Deterministic seed forwarded to :func:`~.corpus.build_corpus`.
        max_items: ``ContextAssembler`` injection budget.
        latency_repeats: How many times to replay the query set per size, so
            the percentile has enough samples to be meaningful (a bare
            10-query set collapses p95 and p99 onto the same nearest-rank
            index). The default (20) yields ``query_count * 20`` samples per
            point.

    Returns:
        One :class:`ScalingPoint` per size, in the same order as
        ``corpus_sizes``. Each point's corpus is torn down before the next
        one is built, so peak Redis footprint is one corpus at a time.
    """
    points: List[ScalingPoint] = []
    for size in corpus_sizes:
        corpus = build_corpus(size, seed=seed, query_count=query_count)
        try:
            assembler = ContextAssembler(
                model_class=corpus.model_class,
                score_weights=_SCORE_WEIGHTS,
                max_items=max_items,
                retrieval_mode="auto",
            )

            def _call(query, _assembler=assembler, _agent_id=corpus.agent_id):
                return _assembler.assemble(
                    query_cues={"topic": query.text}, agent_id=_agent_id
                )

            samples = list(corpus.queries) * max(1, latency_repeats)
            report = measure_latency(_call, samples)
            points.append(ScalingPoint(corpus_size=size, latency=report))
        finally:
            teardown_corpus(corpus)
    return points
