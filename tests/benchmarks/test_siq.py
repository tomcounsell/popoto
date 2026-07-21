"""CI-facing tests for the SIQ benchmark (issue #459).

Fixture-based and deterministic. Runs under the standard pytest db15 isolation
plugin (``conftest.py``) — the harness plants per-trace, uniquely-prefixed model
classes into db15 and tears them down, never touching db0 or the db14 external
bench. No network, no API key, no RNG: every metric value below is asserted as
an exact expected constant.

Coverage:

- **Lint** — every committed fixture passes ``lint_trace``; malformed inline
  fixtures raise ``SiqAuthoringError`` (cue-blindness, anticipation window,
  established-earlier rules).
- **Native replay** — the query-blind composite adapter achieves full recall
  with multi-turn anticipation lead on every fixture; exact per-trace values.
- **Discriminant** — the ``QueryOnlyStubAdapter`` (query-driven baseline) scores
  zero recall and all-miss anticipation on the same fixtures *by construction*.
  This is the harness's own validity proof.
- **Metric edge cases** — empty ``should_recall`` and never-injected memories
  resolve to ``None``/miss, never a ``ZeroDivisionError`` or a bogus value.
- **Mode invariant** — the native adapter runs composite (query-blind) mode.
- **Determinism** — replaying the same trace twice yields identical scores.
- **Judge pin** — the reused Tier-5 judge identity is still pinned (SIQ's
  optional usefulness cross-check inherits the same pinned model + prompt).
"""

import pytest

from tests.benchmarks.siq.adapters import (
    NATIVE_SCORE_WEIGHTS,
    NativeAdapter,
    QueryOnlyStubAdapter,
)
from tests.benchmarks.siq.corpus import (
    FIXTURES_DIR,
    SiqAuthoringError,
    SiqMemory,
    SiqTrace,
    SiqTurn,
    lint_trace,
    load_all_traces,
)
from tests.benchmarks.siq.metrics import (
    TurnResult,
    anticipation_lead_times,
    score_trace,
    siq_token_count,
    turn_budget_efficiency,
    turn_precision,
    turn_recall,
)
from tests.benchmarks.siq.runner import replay, run_trace

FIXTURE_IDS = [
    "coreference_relocation",
    "implication_deadline",
    "multi_recall_preferences",
    "need_to_know_allergy",
]


def _traces_by_id():
    return {t.trace_id: t for t in load_all_traces(FIXTURES_DIR)}


# ---------------------------------------------------------------------------
# Fixture inventory + lint
# ---------------------------------------------------------------------------


def test_all_fixtures_present_and_lint_clean():
    traces = load_all_traces(FIXTURES_DIR)
    assert sorted(t.trace_id for t in traces) == sorted(FIXTURE_IDS)
    # load_all_traces lints each and enforces unique trace_id; reaching here is
    # the pass. Re-lint explicitly for clarity.
    for t in traces:
        lint_trace(t)


def _mem(mid, content, imp=0.5):
    return SiqMemory(id=mid, content=content, importance=imp)


def test_lint_rejects_cue_that_lexically_matches_target():
    """Cue-blindness: a should_recall message sharing a token with the memory."""
    trace = SiqTrace(
        trace_id="bad_cue",
        description="",
        turns=(
            SiqTurn(0, "user", "planted content", _mem("m", "peanut allergy severe")),
            SiqTurn(1, "user", "filler line here"),
            SiqTurn(
                2,
                "user",
                "tell me about the peanut dish",
                should_recall=frozenset({"m"}),
            ),
        ),
    )
    with pytest.raises(SiqAuthoringError, match="lexically cues"):
        lint_trace(trace)


def test_lint_rejects_recall_before_establish():
    trace = SiqTrace(
        trace_id="bad_order",
        description="",
        turns=(
            SiqTurn(0, "user", "nothing yet", should_recall=frozenset({"m"})),
            SiqTurn(1, "user", "planted", _mem("m", "zzz qqq")),
        ),
    )
    with pytest.raises(SiqAuthoringError, match="strictly earlier"):
        lint_trace(trace)


def test_lint_rejects_recall_of_unknown_memory():
    trace = SiqTrace(
        trace_id="unknown_mem",
        description="",
        turns=(
            SiqTurn(0, "user", "planted", _mem("real", "zzz qqq")),
            SiqTurn(1, "user", "filler line here"),
            SiqTurn(
                2, "user", "unrelated words only", should_recall=frozenset({"ghost"})
            ),
        ),
    )
    with pytest.raises(SiqAuthoringError, match="never established"):
        lint_trace(trace)


def test_lint_rejects_no_anticipation_window():
    """A recall exactly one turn after establishment can't exercise lead time."""
    trace = SiqTrace(
        trace_id="no_window",
        description="",
        turns=(
            SiqTurn(0, "user", "planted", _mem("m", "zzz qqq")),
            SiqTurn(1, "user", "unrelated words only", should_recall=frozenset({"m"})),
        ),
    )
    with pytest.raises(SiqAuthoringError, match="anticipation"):
        lint_trace(trace)


# ---------------------------------------------------------------------------
# Native (query-blind composite) replay — exact deterministic values
# ---------------------------------------------------------------------------

# Expected per-trace native scores. Precision/recall are exact rationals;
# budget_efficiency is asserted via approx to a literal derived from the
# deterministic token counter over the committed fixture content.
NATIVE_EXPECTED = {
    "coreference_relocation": {
        "precision": 1.0,
        "recall": 1.0,
        "efficiency": 1.0,
        "leads": {"mem_relocation": 4},
        "misses": 0,
    },
    "implication_deadline": {
        "precision": 0.5,
        "recall": 1.0,
        "efficiency": pytest.approx(21 / 38),
        "leads": {"mem_locked_demo": 4},
        "misses": 0,
    },
    "multi_recall_preferences": {
        "precision": pytest.approx(2 / 3),
        "recall": 1.0,
        "efficiency": pytest.approx(0.7),
        "leads": {"mem_vegetarian": 5, "mem_budget": 3},
        "misses": 0,
    },
    "need_to_know_allergy": {
        "precision": 0.5,
        "recall": 1.0,
        "efficiency": pytest.approx(4 / 7),
        "leads": {"mem_allergy": 4},
        "misses": 0,
    },
}


@pytest.mark.parametrize("trace_id", FIXTURE_IDS)
def test_native_replay_scores(trace_id):
    trace = _traces_by_id()[trace_id]
    scored = run_trace(trace, NativeAdapter)
    exp = NATIVE_EXPECTED[trace_id]
    assert scored["precision_at_budget"] == exp["precision"]
    assert scored["recall_at_budget"] == exp["recall"]
    assert scored["budget_efficiency"] == exp["efficiency"]
    assert scored["anticipation_lead_times"] == exp["leads"]
    assert scored["anticipation_misses"] == exp["misses"]
    # Native anticipates every target: mean lead is strictly positive.
    assert scored["mean_anticipation_lead"] > 0


# ---------------------------------------------------------------------------
# Discriminant: the query-driven baseline scores ~0 by construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trace_id", FIXTURE_IDS)
def test_query_stub_is_blind_to_uncued_memories(trace_id):
    """The whole point: a query-driven retriever fails these traces.

    Because the cue-blindness lint guarantees the recall-turn message shares no
    token with the target memory, the stub never injects the target at the turn
    it matters — recall 0.0, every anticipation a miss.
    """
    trace = _traces_by_id()[trace_id]
    stub = run_trace(trace, QueryOnlyStubAdapter)
    native = run_trace(trace, NativeAdapter)

    assert stub["recall_at_budget"] == 0.0
    assert stub["anticipation_misses"] == stub["n_recall_targets"]
    assert stub["mean_anticipation_lead"] is None
    # The discriminating gap: native recalls, the query retriever does not.
    assert native["recall_at_budget"] == 1.0
    assert native["recall_at_budget"] > stub["recall_at_budget"]


# ---------------------------------------------------------------------------
# Metric-function unit tests (pure, no Redis)
# ---------------------------------------------------------------------------


def test_precision_recall_none_at_non_evaluation_turn():
    r = TurnResult(
        index=0,
        should_recall=frozenset(),
        injected_ids=("a", "b"),
        injected_tokens={"a": 3, "b": 3},
    )
    assert turn_precision(r) is None
    assert turn_recall(r) is None
    assert turn_budget_efficiency(r) is None


def test_precision_none_when_nothing_injected_but_recall_zero():
    r = TurnResult(
        index=0, should_recall=frozenset({"a"}), injected_ids=(), injected_tokens={}
    )
    assert turn_precision(r) is None  # nothing to judge precision on
    assert turn_recall(r) == 0.0  # failed to recall
    assert turn_budget_efficiency(r) is None


def test_partial_precision_and_efficiency():
    r = TurnResult(
        index=0,
        should_recall=frozenset({"a"}),
        injected_ids=("a", "b"),
        injected_tokens={"a": 4, "b": 12},
    )
    assert turn_precision(r) == 0.5
    assert turn_recall(r) == 1.0
    assert turn_budget_efficiency(r) == pytest.approx(4 / 16)


def test_anticipation_miss_when_never_injected_at_relevant_turn():
    results = [
        TurnResult(0, frozenset(), ("m",), {"m": 3}),
        TurnResult(1, frozenset(), (), {}),  # dropped before it mattered
        TurnResult(2, frozenset({"m"}), (), {}),  # relevant, not injected -> miss
    ]
    leads = anticipation_lead_times(results)
    assert leads == {"m": None}
    scored = score_trace(results)
    assert scored["anticipation_misses"] == 1
    assert scored["mean_anticipation_lead"] is None


def test_anticipation_counts_consecutive_lead_only():
    # Injected at turns 0,1 (gap at 2), then 3,4; relevant at 4 -> lead counts
    # back through 3 only (turn 2 breaks the run).
    results = [
        TurnResult(0, frozenset(), ("m",), {"m": 3}),
        TurnResult(1, frozenset(), ("m",), {"m": 3}),
        TurnResult(2, frozenset(), (), {}),
        TurnResult(3, frozenset(), ("m",), {"m": 3}),
        TurnResult(4, frozenset({"m"}), ("m",), {"m": 3}),
    ]
    assert anticipation_lead_times(results) == {"m": 1}


def test_token_counter_deterministic_floor():
    assert siq_token_count("") == 0
    assert siq_token_count("x") == 1  # floor of 1 for non-empty
    assert siq_token_count("a" * 40) == 10


# ---------------------------------------------------------------------------
# Mode invariant + determinism
# ---------------------------------------------------------------------------


def test_native_adapter_is_composite_mode():
    trace = _traces_by_id()["coreference_relocation"]
    adapter = NativeAdapter(trace)
    try:
        assert adapter._assembler._effective_mode == "composite"
        assert NATIVE_SCORE_WEIGHTS == {"importance": 1.0}
    finally:
        adapter.teardown()


def test_native_replay_is_deterministic():
    trace = _traces_by_id()["multi_recall_preferences"]
    first = run_trace(trace, NativeAdapter)
    second = run_trace(trace, NativeAdapter)
    # trace_id/adapter are identical; the metric block must match bit-for-bit.
    for key in (
        "precision_at_budget",
        "recall_at_budget",
        "budget_efficiency",
        "anticipation_lead_times",
        "mean_anticipation_lead",
        "anticipation_misses",
    ):
        assert first[key] == second[key]


def test_replay_lifecycle_plants_and_tears_down():
    """A full replay leaves no residue for its own model class."""
    from src.popoto.redis_db import POPOTO_REDIS_DB

    trace = _traces_by_id()["need_to_know_allergy"]
    adapter = NativeAdapter(trace)
    class_name = adapter._model_class.__name__

    def _scan_all():
        found, cursor = [], 0
        while True:
            cursor, k = POPOTO_REDIS_DB.scan(cursor, match=f"*{class_name}*", count=200)
            found.extend(k)
            if cursor == 0:
                break
        return found

    replay(trace, adapter)
    assert _scan_all(), "expected planted keys before teardown"
    adapter.teardown()
    assert _scan_all() == []


# ---------------------------------------------------------------------------
# Judge pin (SIQ's optional usefulness cross-check reuses the Tier-5 judge)
# ---------------------------------------------------------------------------


def test_optional_judge_identity_is_pinned():
    from tests.benchmarks import judge

    assert judge.JUDGE_MODEL == "gpt-4o-mini"
    assert judge.JUDGE_TEMPERATURE == 0
    # is_judge_available gates any live call so the default suite never needs a
    # key or the openai package.
    assert callable(judge.is_judge_available)
