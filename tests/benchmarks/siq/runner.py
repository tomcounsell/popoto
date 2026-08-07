"""SIQ replay driver — turn-by-turn ingest + inject + score (issue #459).

``replay(trace, adapter)`` walks a :class:`~tests.benchmarks.siq.corpus.SiqTrace`
in turn order. For each turn it first lets the adapter ingest the turn (writing
any established memory), then asks the adapter what it would inject *now*, and
records a :class:`~tests.benchmarks.siq.metrics.TurnResult`. Token accounting is
computed uniformly here (from the trace's own memory content) so every adapter
is scored identically regardless of how it stores memories.

Incremental planting is intrinsic to the replay: a memory only exists in the
adapter's store from the turn that establishes it onward, so the injected set
genuinely evolves turn to turn — which is what makes anticipation lead time
meaningful (a memory can be proactively injected across the turns between its
establishment and the turn it becomes relevant).
"""

from __future__ import annotations

from typing import List

from .corpus import SiqTrace
from .metrics import TurnResult, score_trace, siq_token_count


def replay(trace: SiqTrace, adapter) -> List[TurnResult]:
    """Replay ``trace`` against ``adapter``; return per-turn results.

    The adapter is responsible for its own setup (done in ``__init__``) and
    teardown (caller invokes ``adapter.teardown()``); ``replay`` does not own
    the adapter lifecycle so the same adapter policy can be inspected after the
    run if desired.
    """
    memories = trace.memories()
    # Precompute deterministic token counts once per memory.
    token_counts = {
        mem_id: siq_token_count(mem.content) for mem_id, mem in memories.items()
    }
    # An adapter that writes records the fixture didn't author (e.g. the #489
    # ExtractionAdapter, whose extracted facts have their own text) publishes
    # their token counts here. Without this the budget-efficiency denominator
    # would score those records as 0 tokens and silently overstate efficiency.
    extra = getattr(adapter, "extra_token_counts", None)

    results: List[TurnResult] = []
    for turn in trace.turns:
        adapter.ingest_turn(turn)
        injected = adapter.injected_for_turn(turn)
        results.append(
            TurnResult(
                index=turn.index,
                should_recall=turn.should_recall,
                injected_ids=tuple(injected),
                injected_tokens={
                    i: token_counts.get(
                        i, extra.get(i, 0) if extra is not None else 0
                    )
                    for i in injected
                },
            )
        )
    return results


def run_trace(trace: SiqTrace, adapter_factory) -> dict:
    """Build an adapter via ``adapter_factory(trace)``, replay, score, teardown.

    Returns the aggregated score dict (see :func:`metrics.score_trace`) plus the
    ``trace_id`` and ``adapter`` name. Teardown always runs, even on error.
    """
    adapter = adapter_factory(trace)
    try:
        results = replay(trace, adapter)
        scored = score_trace(results)
    finally:
        adapter.teardown()
    scored["trace_id"] = trace.trace_id
    scored["adapter"] = getattr(adapter, "name", adapter_factory.__name__)
    return scored
