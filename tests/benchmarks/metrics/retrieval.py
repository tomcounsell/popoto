"""Retrieval quality metrics for benchmark evaluation.

Provides precision@k, nDCG, and calibration error computations.
All functions are pure — no Redis or model dependencies.
"""

import math
from typing import Dict, Iterable, List


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


def recall_at_k(retrieved: List[str], relevant: set, k: int) -> float:
    """Compute recall@k as an any-hit hit-rate.

    Returns 1.0 if *any* relevant item appears in the top-k results, else 0.0.
    This is the standard hit-rate definition used by published memory
    benchmarks (LongMemEval-S, LoCoMo) and by the external reference numbers
    this harness compares against.

    Any-hit recall — not fractional recall — is the correct headline metric
    here for two reasons: (1) it is directly comparable to the external
    reference hit-rates, and (2) it preserves the invariant
    ``MRR <= recall_at_k`` for any single result, since a first relevant item
    at rank r contributes 1/r <= 1 to MRR but a full 1.0 to an any-hit. The
    datasets carry *multi-evidence* ground truth (a question may have 2-6
    relevant sessions), so fractional recall would deflate a rank-1 hit to
    1/|relevant| and break that invariant. Use :func:`fractional_recall_at_k`
    when the proportion of relevant items found is genuinely wanted.

    Args:
        retrieved: Ordered list of retrieved item IDs.
        relevant: Set of relevant item IDs (ground truth).
        k: Number of top results to evaluate.

    Returns:
        1.0 if at least one relevant item is in the top-k, else 0.0. Returns
        0.0 if k <= 0, retrieved is empty, or relevant is empty.
    """
    if k <= 0 or not retrieved or not relevant:
        return 0.0
    hits = sum(1 for item in retrieved[:k] if item in relevant)
    return 1.0 if hits > 0 else 0.0


def fractional_recall_at_k(retrieved: List[str], relevant: set, k: int) -> float:
    """Compute fractional recall@k — proportion of relevant items in top-k.

    Returns ``(# relevant items found in top-k) / (total # relevant items)``,
    capped at 1.0. Unlike :func:`recall_at_k` (any-hit hit-rate), this rewards
    finding *more* of a multi-evidence ground-truth set: a rank-1 hit on a
    2-evidence question scores 0.5 here but 1.0 under :func:`recall_at_k`.

    This is NOT the headline benchmark metric — it is not comparable to the
    external any-hit reference numbers and does not preserve ``MRR <= recall``.
    It is retained for per-evidence coverage analysis.

    Args:
        retrieved: Ordered list of retrieved item IDs.
        relevant: Set of relevant item IDs (ground truth).
        k: Number of top results to evaluate.

    Returns:
        Float in [0, 1]. Returns 0.0 if k <= 0, retrieved is empty, or
        relevant is empty.
    """
    if k <= 0 or not retrieved or not relevant:
        return 0.0
    hits = sum(1 for item in retrieved[:k] if item in relevant)
    return min(1.0, hits / len(relevant))


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


def leaderboard_parity_slice(
    by_question_type: Dict,
    exclude_categories: Iterable = ("5",),
) -> Dict:
    """Re-aggregate a per-``question_type`` breakdown with categories excluded.

    Given the ``by_question_type`` block a benchmark run emits (each category
    mapping to ``n`` + ``recall_at_1/5/10`` + ``mrr``), recompute the aggregate
    over only the *retained* categories, weighting each category's mean by its
    ``n``. Every metric here is itself a mean over questions, so the n-weighted
    mean of the per-category means equals the overall metric on the retained
    subset — no per-question data needed, so the slice is reproducible from a
    committed artifact's ``by_question_type`` alone.

    The motivating use is LoCoMo's "leaderboard-parity" slice (issue #454):
    excluding category 5 ("adversarial", n=446) from the 1986-QA run yields the
    exact 1540-QA, 4-category configuration the common no-adversarial
    leaderboards report, so Popoto lines up against them without a re-run.

    Category keys are compared as strings, so this works whether the breakdown
    keys are ints (a live in-process report) or strings (a JSON round-trip).

    Args:
        by_question_type: Mapping of category key → {"n", "recall_at_1",
            "recall_at_5", "recall_at_10", "mrr"}.
        exclude_categories: Category keys to drop before re-aggregating.
            Compared by string value. Default drops LoCoMo's cat-5.

    Returns:
        Dict with ``excluded`` (sorted list of the excluded keys that were
        actually present), ``n`` (retained question count), and the
        re-aggregated ``recall_at_1/5/10`` + ``mrr`` (rounded to 4 places).
        Recalls are ``0.0`` when no questions are retained.
    """
    excluded_set = {str(c) for c in exclude_categories}
    excluded_present = sorted(
        str(cat) for cat in by_question_type if str(cat) in excluded_set
    )

    total_n = 0
    acc = {"recall_at_1": 0.0, "recall_at_5": 0.0, "recall_at_10": 0.0, "mrr": 0.0}
    for cat, stats in by_question_type.items():
        if str(cat) in excluded_set:
            continue
        n = stats["n"]
        total_n += n
        for metric in acc:
            acc[metric] += n * stats[metric]

    if total_n == 0:
        result = {m: 0.0 for m in acc}
    else:
        result = {m: round(acc[m] / total_n, 4) for m in acc}

    return {"excluded": excluded_present, "n": total_n, **result}
