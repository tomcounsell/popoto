"""Retrieval quality metrics for benchmark evaluation.

Provides precision@k, nDCG, and calibration error computations.
All functions are pure — no Redis or model dependencies.
"""

import math
from typing import Dict, List


def precision_at_k(retrieved: List[str], relevant: set, k: int) -> float:
    """Compute precision@k — fraction of top-k results that are relevant.

    Args:
        retrieved: Ordered list of retrieved item IDs.
        relevant: Set of relevant item IDs (ground truth).
        k: Number of top results to evaluate.

    Returns:
        Float in [0, 1]. Returns 0.0 if k <= 0 or retrieved is empty.
    """
    if k <= 0 or not retrieved:
        return 0.0
    top_k = retrieved[:k]
    hits = sum(1 for item in top_k if item in relevant)
    return hits / k


def ndcg_at_k(
    retrieved: List[str],
    relevance_scores: Dict[str, float],
    k: int,
) -> float:
    """Compute normalized discounted cumulative gain at k.

    Args:
        retrieved: Ordered list of retrieved item IDs.
        relevance_scores: Mapping of item IDs to relevance scores (higher = better).
        k: Number of top results to evaluate.

    Returns:
        Float in [0, 1]. Returns 0.0 if k <= 0 or no relevant items exist.
    """
    if k <= 0 or not retrieved or not relevance_scores:
        return 0.0

    # DCG of the retrieved ranking
    dcg = 0.0
    for i, item in enumerate(retrieved[:k]):
        rel = relevance_scores.get(item, 0.0)
        dcg += rel / math.log2(i + 2)  # i+2 because log2(1) = 0

    # Ideal DCG: sort by true relevance
    ideal_scores = sorted(relevance_scores.values(), reverse=True)[:k]
    idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(ideal_scores))

    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def calibration_error(
    predictions: List[float],
    outcomes: List[bool],
    n_bins: int = 10,
) -> float:
    """Compute expected calibration error (ECE).

    Groups predictions into bins and measures the average gap between
    predicted probability and observed frequency.

    Args:
        predictions: List of predicted probabilities in [0, 1].
        outcomes: List of boolean outcomes (True = positive).
        n_bins: Number of calibration bins. Default 10.

    Returns:
        Float in [0, 1]. Lower is better. Returns 0.0 if no predictions.
    """
    if not predictions or not outcomes:
        return 0.0
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"predictions and outcomes must have same length "
            f"({len(predictions)} vs {len(outcomes)})"
        )

    bin_edges = [i / n_bins for i in range(n_bins + 1)]
    total = len(predictions)
    ece = 0.0

    for b in range(n_bins):
        low, high = bin_edges[b], bin_edges[b + 1]
        # Include right edge for last bin
        indices = [
            i
            for i, p in enumerate(predictions)
            if (low <= p < high) or (b == n_bins - 1 and p == high)
        ]
        if not indices:
            continue
        bin_size = len(indices)
        avg_pred = sum(predictions[i] for i in indices) / bin_size
        avg_outcome = sum(1 for i in indices if outcomes[i]) / bin_size
        ece += (bin_size / total) * abs(avg_pred - avg_outcome)

    return ece


def mean_reciprocal_rank(retrieved: List[str], relevant: set) -> float:
    """Compute mean reciprocal rank (MRR).

    Args:
        retrieved: Ordered list of retrieved item IDs.
        relevant: Set of relevant item IDs.

    Returns:
        Float in [0, 1]. Returns 0.0 if no relevant item found.
    """
    for i, item in enumerate(retrieved):
        if item in relevant:
            return 1.0 / (i + 1)
    return 0.0
