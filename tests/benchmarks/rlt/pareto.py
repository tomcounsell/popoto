"""Recall-vs-p99 Pareto frontier — the joint honest artifact (#460).

"At equal p99, who recalls more; at equal recall, who's faster." A point is
on the frontier if no other point dominates it on **both** axes
simultaneously (lower p99 AND higher-or-equal recall, with at least one axis
strictly better).

The recall axis is the **mean of ``recall_at_k()`` over the full query set**
per config (a single continuous value per point), not a per-query binary
value — ``recall_at_k()`` in ``tests/benchmarks/metrics/retrieval.py`` is
any-hit binary (0.0/1.0) *per query*; RLT's Pareto point aggregates that over
however many queries the corpus/query-set defines (clarified per
plan-critique finding).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ParetoPoint:
    """One (config, p99 latency, mean recall) point."""

    config_label: str
    p99_ms: float
    recall: float

    def to_dict(self) -> dict:
        return {
            "config_label": self.config_label,
            "p99_ms": self.p99_ms,
            "recall": self.recall,
        }


def _dominates(a: ParetoPoint, b: ParetoPoint) -> bool:
    """True if ``a`` dominates ``b`` (a is faster-or-equal AND recalls-more-or-equal,
    with at least one strictly better)."""
    faster_or_equal = a.p99_ms <= b.p99_ms
    recalls_more_or_equal = a.recall >= b.recall
    strictly_better = a.p99_ms < b.p99_ms or a.recall > b.recall
    return faster_or_equal and recalls_more_or_equal and strictly_better


def pareto_frontier(points: List[ParetoPoint]) -> List[ParetoPoint]:
    """Return the Pareto-optimal subset of ``points`` (lower p99, higher recall).

    Args:
        points: Candidate points, one per config/comparator run.

    Returns:
        The non-dominated points, in the same relative order as the input.
        ``[]`` if ``points`` is empty (a well-defined empty frontier, not an
        error).
    """
    frontier = []
    for candidate in points:
        if any(
            _dominates(other, candidate) for other in points if other is not candidate
        ):
            continue
        frontier.append(candidate)
    return frontier
