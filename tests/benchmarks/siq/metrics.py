"""SIQ scoring — pure, deterministic metric functions over replay results.

Every function here is a pure function of :class:`TurnResult` lists plus the
trace ground truth. No Redis, no RNG, no wall-clock — given the same
``TurnResult`` sequence the numbers are bit-for-bit reproducible, so
``test_siq.py`` asserts exact expected constants (no tolerance bands).

Metric definitions (locked in ``docs/plans/siq_benchmark.md`` "Critique
Response"):

Precision, recall, and budget efficiency are evaluated **only at turns that
carry a ``should_recall`` annotation** — the moments of defined information
need. Narrative turns (empty ``should_recall``) contribute nothing to these
three; scoring a proactive injection at every narrative turn as "imprecise"
would make the aggregate meaningless and punish the very anticipation the
benchmark exists to reward. Early injection at narrative turns is instead
credited by *anticipation lead time*, which is the metric designed for it.

- **Injection precision @ budget** (per evaluation turn): ``|injected ∩
  should_recall| / |injected|``. Undefined (``None``) at non-evaluation turns
  and when nothing was injected — never ``0/0``.
- **Injection recall @ budget** (per evaluation turn): ``|injected ∩
  should_recall| / |should_recall|``. Undefined (``None``) at non-evaluation
  turns.
- **Anticipation lead time** (per memory): given ``R`` = the first turn a memory
  is a ``should_recall`` target, and requiring it to be injected *at* ``R``, the
  count of consecutive turns immediately before ``R`` at which it was also
  injected. A memory not injected at ``R`` is a **miss** (excluded from the
  mean, counted separately) — never a negative or ``+inf`` value. This rewards
  *proactive, continuous* presence: a pure query-driven retriever, which only
  injects on a lexical match, is absent at ``R`` by the cue-blindness lint and
  therefore always misses.
- **Budget efficiency** (per evaluation turn): ``useful_tokens /
  injected_tokens`` where ``useful_tokens`` is the token mass of injected
  records whose id is in that turn's ``should_recall``. Undefined (``None``) at
  non-evaluation turns and when nothing was injected.

Token counting is a deterministic character-length heuristic (``len // 4``,
floor 1) — no model, no network — so budget numbers are reproducible from the
committed fixture text alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


def siq_token_count(text: str) -> int:
    """Deterministic token estimate: ``max(1, len(text) // 4)``.

    A whitespace/character heuristic with no model dependency, so the same
    fixture text always yields the same count. Floor of 1 so a non-empty
    record never counts as zero tokens.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class TurnResult:
    """The outcome of replaying one trace turn against one adapter.

    ``injected_ids`` is the rank-ordered list of memory ids the adapter placed
    in context for this turn (already budget-capped by the adapter).
    ``injected_tokens`` maps each injected id to its token count.
    """

    index: int
    should_recall: frozenset
    injected_ids: tuple
    injected_tokens: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-turn precision / recall
# ---------------------------------------------------------------------------


def turn_precision(result: TurnResult) -> Optional[float]:
    """Injection precision @ budget at an evaluation turn.

    ``None`` at non-evaluation turns (no ``should_recall``) and when nothing
    was injected.
    """
    if not result.should_recall:
        return None
    injected = set(result.injected_ids)
    if not injected:
        return None
    useful = injected & set(result.should_recall)
    return len(useful) / len(injected)


def turn_recall(result: TurnResult) -> Optional[float]:
    """Injection recall @ budget at an evaluation turn (``None`` otherwise)."""
    if not result.should_recall:
        return None
    injected = set(result.injected_ids)
    useful = injected & set(result.should_recall)
    return len(useful) / len(result.should_recall)


def turn_budget_efficiency(result: TurnResult) -> Optional[float]:
    """Useful-token / injected-token ratio at an evaluation turn.

    ``None`` at non-evaluation turns and when nothing was injected.
    """
    if not result.should_recall:
        return None
    injected = list(result.injected_ids)
    if not injected:
        return None
    injected_tokens = sum(result.injected_tokens.get(i, 0) for i in injected)
    if injected_tokens == 0:
        return None
    useful_ids = set(injected) & set(result.should_recall)
    useful_tokens = sum(result.injected_tokens.get(i, 0) for i in useful_ids)
    return useful_tokens / injected_tokens


# ---------------------------------------------------------------------------
# Anticipation lead time (per memory)
# ---------------------------------------------------------------------------


def anticipation_lead_times(
    results: List[TurnResult],
) -> Dict[str, Optional[int]]:
    """Per-memory anticipation lead time over a full trace replay.

    For each memory that is a ``should_recall`` target at some turn, let ``R``
    be its earliest such turn. If the memory is injected at ``R``, the lead time
    is the number of consecutive turns immediately before ``R`` at which it was
    also injected. If it is not injected at ``R`` the memory maps to ``None`` (a
    miss).

    Returns:
        dict ``mem_id -> lead (int) | None`` for every memory that is a
        ``should_recall`` target somewhere in the trace.
    """
    injected_at: Dict[int, set] = {r.index: set(r.injected_ids) for r in results}

    first_recall_turn: Dict[str, int] = {}
    for r in sorted(results, key=lambda x: x.index):
        for mem_id in r.should_recall:
            if mem_id not in first_recall_turn:
                first_recall_turn[mem_id] = r.index

    out: Dict[str, Optional[int]] = {}
    for mem_id, R in first_recall_turn.items():
        if mem_id not in injected_at.get(R, set()):
            out[mem_id] = None  # miss — not injected when it mattered
            continue
        lead = 0
        t = R - 1
        while t in injected_at and mem_id in injected_at[t]:
            lead += 1
            t -= 1
        out[mem_id] = lead
    return out


# ---------------------------------------------------------------------------
# Trace-level aggregation
# ---------------------------------------------------------------------------


def _mean(values: List[float]) -> Optional[float]:
    defined = [v for v in values if v is not None]
    if not defined:
        return None
    return sum(defined) / len(defined)


def score_trace(results: List[TurnResult]) -> dict:
    """Aggregate all SIQ metrics over one trace replay.

    Returns a JSON-serializable dict with mean precision/recall/efficiency over
    the turns where each is defined, the per-memory lead times, mean lead time
    over non-miss memories, and the miss count.
    """
    precisions = [turn_precision(r) for r in results]
    recalls = [turn_recall(r) for r in results]
    efficiencies = [turn_budget_efficiency(r) for r in results]
    leads = anticipation_lead_times(results)

    hit_leads = [v for v in leads.values() if v is not None]
    misses = sum(1 for v in leads.values() if v is None)

    return {
        "n_turns": len(results),
        "precision_at_budget": _mean(precisions),
        "recall_at_budget": _mean(recalls),
        "budget_efficiency": _mean(efficiencies),
        "anticipation_lead_times": {k: leads[k] for k in sorted(leads)},
        "mean_anticipation_lead": (_mean(hit_leads) if hit_leads else None),
        "anticipation_misses": misses,
        "n_recall_targets": len(leads),
    }
