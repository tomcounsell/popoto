"""Token efficiency metrics for SubconsciousMemory benchmarks.

Measures how effectively token budgets are used -- whether high-relevance
memories consume most of the token budget versus low-relevance padding.
"""

import statistics
from typing import Callable, Dict, List, Optional

# In-repo test utility: importing the assembler's private helpers keeps this
# metric's default counter in lockstep with the production default — tokens
# are estimated over the serialized record content, not the Redis key.
from popoto.recipes.context_assembler import _estimate_tokens, _serialize_record


def _default_token_counter(record) -> int:
    """Estimate tokens over serialized record content (callable(record))."""
    try:
        return _estimate_tokens(_serialize_record(record, "structured"))
    except (AttributeError, TypeError):
        # Benchmark fixtures may pass plain strings instead of model
        # instances/dicts; estimate over their text directly.
        return _estimate_tokens(str(record))


def token_utilization_ratio(
    records: list,
    relevance_scores: Dict[str, float],
    token_counter: Optional[Callable] = None,
    key_fn: Optional[Callable] = None,
) -> Dict[str, float]:
    """Compute weighted token utilization ratio.

    Measures what fraction of the total token budget is consumed by
    above-median relevance records. A ratio of 1.0 means all tokens
    are spent on high-relevance content.

    Args:
        records: List of model instances or dicts.
        relevance_scores: Mapping of record key -> relevance score.
        token_counter: Optional callable(record) -> int token count.
            Default: ``_estimate_tokens(_serialize_record(r, "structured"))``
            — the assembler's stdlib heuristic over serialized content.
        key_fn: Optional callable(record) -> str key for lookup.
            Default: str(record).

    Returns:
        Dict with "utilization_ratio", "total_tokens",
        "high_relevance_tokens", "low_relevance_tokens".
    """
    if not records:
        return {
            "utilization_ratio": 0.0,
            "total_tokens": 0,
            "high_relevance_tokens": 0,
            "low_relevance_tokens": 0,
        }

    if token_counter is None:
        token_counter = _default_token_counter
    if key_fn is None:
        key_fn = str

    # Compute median relevance
    scores = list(relevance_scores.values())
    if not scores:
        return {
            "utilization_ratio": 0.0,
            "total_tokens": 0,
            "high_relevance_tokens": 0,
            "low_relevance_tokens": 0,
        }
    median_rel = statistics.median(scores) if scores else 0.5

    total_tokens = 0
    high_tokens = 0
    low_tokens = 0

    for record in records:
        key = key_fn(record)
        tokens = token_counter(record)
        total_tokens += tokens
        rel = relevance_scores.get(key, 0.0)
        if rel >= median_rel:
            high_tokens += tokens
        else:
            low_tokens += tokens

    ratio = high_tokens / max(total_tokens, 1)

    return {
        "utilization_ratio": ratio,
        "total_tokens": total_tokens,
        "high_relevance_tokens": high_tokens,
        "low_relevance_tokens": low_tokens,
    }


def importance_distribution_health(
    scores: List[float],
) -> Dict[str, float]:
    """Measure the health of an importance score distribution.

    A healthy distribution has enough spread (std dev) to differentiate
    records during ranking, and enough distinct rank positions for
    meaningful ordering.

    Args:
        scores: List of importance scores (float 0.0-1.0).

    Returns:
        Dict with "std_dev", "distinct_ranks", "rank_ratio",
        "n_scores", "mean".
    """
    if not scores:
        return {
            "std_dev": 0.0,
            "distinct_ranks": 0,
            "rank_ratio": 0.0,
            "n_scores": 0,
            "mean": 0.0,
        }

    n = len(scores)
    mean = sum(scores) / n
    std_dev = statistics.pstdev(scores) if n > 1 else 0.0

    # Count distinct rank positions (unique values rounded to 2 decimals)
    distinct = len(set(round(s, 2) for s in scores))
    rank_ratio = distinct / max(n, 1)

    return {
        "std_dev": std_dev,
        "distinct_ranks": distinct,
        "rank_ratio": rank_ratio,
        "n_scores": n,
        "mean": mean,
    }
