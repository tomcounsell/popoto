"""Gold-blind ID selection in the external benchmark harness (issue #514).

The defect these tests pin: when collapsing retrieved turns to result IDs,
``ExternalScenario.run()`` consulted the answer key —

    matching = [cid for cid in candidate_ids if cid in relevant_ids]
    chosen_ids = matching if matching else candidate_ids[:1]

``candidate_ids`` was ``[session_id, turn_id]``. For LongMemEval-S (session-ID
ground truth) both branches emit element 0, the session ID, so the rule was
benign and its published numbers stand. For LoCoMo (turn-ID ground truth) it
was asymmetric: a gold turn emitted its unique turn ID and held its own rank
slot, while every non-gold turn collapsed into one shared session slot
(measured compression over the full 1986-question corpus: 20 retrieved turns
became 13.2 rank slots on average, against 19.9 after the fix). Gold was
systematically lifted, inflating Recall@K and MRR.

The fix ranks one consistent unit per dataset — turn IDs for LoCoMo, session
IDs for LongMemEval-S — chosen from dataset metadata before retrieval runs,
with gold-blind first-occurrence dedup. These tests prove ID selection is
answer-key-independent, that LongMemEval-S output is unchanged, and that the
unit granularity reaches the report header.

Most tests here are pure and need no Redis; the four end-to-end scenario tests
run a real ingest + retrieve cycle on the test DB like the rest of the suite.
"""

import inspect

import pytest

from tests.benchmarks.datasets import BenchmarkItem, ground_truth_unit
from tests.benchmarks.datasets.locomo import iter_items as iter_locomo
from tests.benchmarks.datasets.longmemeval_s import iter_items as iter_longmemeval
from tests.benchmarks.metrics.retrieval import mean_reciprocal_rank, recall_at_k
from tests.benchmarks.run_external import build_markdown_report, compute_aggregate
from tests.benchmarks.scenarios.external_base import (
    ExternalScenario,
    _resolve_ranking_unit,
    collapse_to_ranking_unit,
)
from tests.benchmarks.test_external import LME_FIXTURE, LOCOMO_FIXTURE


def _legacy_collapse(retrieved_keys, redis_key_to_ids, relevant_ids):
    """The pre-#514 gold-aware ID selection, reproduced as a reference.

    Kept verbatim so the tests below can state exactly what changed and assert
    the LongMemEval-S equivalence directly against the old behaviour rather
    than against a description of it.

    Args:
        retrieved_keys: Redis keys in rank order.
        redis_key_to_ids: ``redis_key -> [session_id, turn_id]`` (the merged
            candidate list the old implementation built).
        relevant_ids: The answer key.

    Returns:
        Ranked IDs under the old rule.
    """
    out = []
    seen = set()
    for rk in retrieved_keys:
        candidate_ids = redis_key_to_ids.get(rk, [rk])
        matching = [cid for cid in candidate_ids if cid in relevant_ids]
        chosen_ids = matching if matching else candidate_ids[:1]
        for chosen_id in chosen_ids:
            if chosen_id not in seen:
                seen.add(chosen_id)
                out.append(chosen_id)
    return out


def _synthetic_maps(n_turns=20, session_id="s1"):
    """Build the maps a scenario holds for ``n_turns`` turns of one session.

    Returns:
        ``(redis_keys, session_map, turn_map, merged_map)`` — the ranked keys,
        the two single-granularity maps the fix uses, and the pre-#514 merged
        ``redis_key -> [session_id, turn_id]`` shape.
    """
    redis_keys = [f"ExtMem:{i}" for i in range(n_turns)]
    turn_ids = [f"{session_id}::{i}" for i in range(n_turns)]
    session_map = {session_id: list(redis_keys)}
    turn_map = {tid: [rk] for tid, rk in zip(turn_ids, redis_keys)}
    merged_map = {rk: [session_id, tid] for rk, tid in zip(redis_keys, turn_ids)}
    return redis_keys, session_map, turn_map, merged_map


# Query terms with strictly increasing rarity across the history below, so
# BM25 produces a strict total order (no ties) and the end-to-end tests can
# assert an exact ranking rather than a set. Turn i carries terms[:i+1], so
# turn 5 is the strongest match and turn 0 the weakest.
_TERMS = ["trail", "summit", "ridge", "canyon", "meadow", "lakeside"]

_EXPECTED_TURN_RANKING = [f"D1:{i}" for i in reversed(range(len(_TERMS)))]


def _hiking_history():
    """A single-session LoCoMo-shaped history, one turn per rarity level."""
    return [
        {
            "role": "user",
            "content": "I hiked the " + " ".join(_TERMS[: i + 1]) + ".",
            "turn_id": f"D1:{i}",
            "session_id": "session_1",
        }
        for i in range(len(_TERMS))
    ]


def _locomo_item(relevant_ids=None):
    """A LoCoMo-shaped BenchmarkItem over :func:`_hiking_history`."""
    return BenchmarkItem(
        item_id="conv-1::qa0",
        history=_hiking_history(),
        query="Which " + " ".join(_TERMS) + " did I hike?",
        relevant_ids={"D1:3"} if relevant_ids is None else relevant_ids,
        metadata={
            "dataset": "locomo",
            "ground_truth_unit": "turn",
            "question_type": 4,
        },
    )


def _longmemeval_item():
    """A LongMemEval-S-shaped item whose query matches one session."""
    return BenchmarkItem(
        item_id="q1",
        history=[
            {
                "role": "user",
                "content": "I adopted a golden retriever named Max.",
                "turn_id": "s1::0",
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": "Max is a great name for a dog.",
                "turn_id": "s1::1",
                "session_id": "s1",
            },
        ],
        query="What is my dog Max?",
        relevant_ids={"s1"},
        metadata={
            "dataset": "longmemeval-s",
            "ground_truth_unit": "session",
            "question_type": "single-session-user",
        },
    )


class TestGoldBlindIdSelection:
    """ID selection is answer-key-independent (issue #514)."""

    def test_collapse_signature_cannot_see_the_answer_key(self):
        """The collapse helper takes no answer-key parameter at all.

        A structural guarantee: no future edit can reintroduce gold-awareness
        inside this function without changing its signature.
        """
        params = set(inspect.signature(collapse_to_ranking_unit).parameters)
        assert params == {"retrieved_keys", "unit_map"}

    def test_turn_ranking_is_one_slot_per_record(self):
        """Turn-unit ranking gives all 20 retrieved records their own slot."""
        redis_keys, _, turn_map, _ = _synthetic_maps()
        ranked = collapse_to_ranking_unit(redis_keys, turn_map)
        assert len(ranked) == len(redis_keys) == 20
        assert "s1" not in ranked

    @pytest.mark.parametrize(
        "answer_key",
        [
            set(),
            {"s1::0"},
            {"s1::19"},
            {"s1::3", "s1::7", "s1::11"},
            {"s1"},
            {"not-an-id"},
        ],
    )
    def test_legacy_rule_moved_with_the_answer_key(self, answer_key):
        """Baseline for the invariance claim: the old rule was gold-sensitive.

        Different answer keys produced different rankings from one identical
        retrieval — the property that made every LoCoMo number an artifact of
        its own ground truth.
        """
        redis_keys, _, _, merged_map = _synthetic_maps()
        legacy = _legacy_collapse(redis_keys, merged_map, answer_key)
        gold_turns = {tid for tid in answer_key if "::" in tid}
        # Every gold turn is promoted into its own slot; everything else in the
        # session shares a single slot, so the ranking length tracks the key.
        assert len(legacy) == len(gold_turns) + 1

    @pytest.mark.parametrize(
        "answer_key",
        [
            set(),
            {"s1::0"},
            {"s1::19"},
            {"s1::3", "s1::7", "s1::11"},
            {"s1"},
            {"not-an-id"},
        ],
    )
    def test_turn_ranking_identical_for_every_answer_key(self, answer_key):
        """Turn-unit ranking is identical whatever the answer key is.

        Includes the withheld case (``set()``): a ranking produced with no
        answer key at all equals the ranking produced with the real one.
        """
        redis_keys, _, turn_map, _ = _synthetic_maps()
        expected = [f"s1::{i}" for i in range(20)]
        # The answer key is not an input, so it cannot shift the result. Scored
        # afterwards, it changes the metric — and only the metric.
        assert collapse_to_ranking_unit(redis_keys, turn_map) == expected

    def test_shuffling_the_answer_key_cannot_change_the_ranking(self):
        """Repeated collapses over one retrieval yield one ranking."""
        redis_keys, _, turn_map, _ = _synthetic_maps()
        rankings = {
            tuple(collapse_to_ranking_unit(redis_keys, turn_map)) for _ in range(5)
        }
        assert len(rankings) == 1

    def test_legacy_rule_compressed_slots_and_lifted_gold(self):
        """Pins the measured defect and the inflation it produced.

        With one gold turn among 20 in a single session, the old rule emitted
        the gold turn ID plus one shared session slot — 2 rank slots for 20
        retrieved records, so a gold record could not rank worse than 2nd
        regardless of how the retriever actually ranked it.
        """
        redis_keys, _, turn_map, merged_map = _synthetic_maps()
        gold = {"s1::17"}

        legacy = _legacy_collapse(redis_keys, merged_map, gold)
        corrected = collapse_to_ranking_unit(redis_keys, turn_map)

        assert len(legacy) == 2
        assert legacy.index("s1::17") == 1
        assert len(corrected) == 20
        assert corrected.index("s1::17") == 17

        # The inflation, in the published metrics.
        assert recall_at_k(legacy, gold, 5) == 1.0
        assert recall_at_k(corrected, gold, 5) == 0.0
        assert mean_reciprocal_rank(legacy, gold) > mean_reciprocal_rank(
            corrected, gold
        )

    def test_unmapped_key_scores_as_an_honest_miss(self):
        """A key absent from the unit map emits itself, never a neighbour's ID."""
        ranked = collapse_to_ranking_unit(["ExtMem:missing"], {"s1::0": ["ExtMem:0"]})
        assert ranked == ["ExtMem:missing"]
        assert recall_at_k(ranked, {"s1::0"}, 1) == 0.0


class TestLongMemEvalUnaffected:
    """LongMemEval-S output is byte-identical to the pre-#514 rule.

    Its ground truth is session IDs, and the session ID was always candidate
    element 0, so the old rule's gold-matching branch and its
    ``candidate_ids[:1]`` fallback both emitted the same ID. The published
    LongMemEval-S numbers (R@1 0.894 / R@5 0.986 / R@10 0.992 / MRR 0.932)
    therefore stand.
    """

    @pytest.mark.parametrize("answer_key", [set(), {"s1"}, {"s2"}, {"s1", "s2"}])
    def test_single_session_matches_legacy(self, answer_key):
        """Session-unit collapse equals the legacy rule, for any answer key.

        The keys parametrized here are session IDs, which is the only shape
        LongMemEval-S ground truth takes — pinned by
        :meth:`test_turn_ids_can_never_collide_with_session_ids`.
        """
        redis_keys, session_map, _, merged_map = _synthetic_maps()
        corrected = collapse_to_ranking_unit(redis_keys, session_map)
        assert _legacy_collapse(redis_keys, merged_map, answer_key) == corrected
        assert corrected == ["s1"]

    @pytest.mark.parametrize(
        "answer_key", [set(), {"s0"}, {"s3"}, {"s1", "s2"}, {"s0", "s1", "s2", "s3"}]
    )
    def test_many_interleaved_sessions_match_legacy(self, answer_key):
        """Same equivalence with sessions interleaved in rank order.

        Interleaving matters: it is the case where dedup order could differ
        between the two rules if the fix had changed session-level behaviour.
        """
        keys = []
        session_map = {}
        merged_map = {}
        for s in range(4):
            for t in range(5):
                rk = f"ExtMem:{s}:{t}"
                keys.append(rk)
                session_map.setdefault(f"s{s}", []).append(rk)
                merged_map[rk] = [f"s{s}", f"s{s}::{t}"]
        ranked_keys = [keys[i] for i in (0, 5, 1, 10, 15, 6, 2, 11, 16, 7)]

        corrected = collapse_to_ranking_unit(ranked_keys, session_map)
        assert _legacy_collapse(ranked_keys, merged_map, answer_key) == corrected
        assert corrected == ["s0", "s1", "s2", "s3"]

    def test_turn_ids_can_never_collide_with_session_ids(self):
        """The adapter's ``turn_id`` is ``{session_id}::{idx}``, never a session ID.

        This is what made element 0 unambiguous for LongMemEval-S, and it is
        the precondition the equivalence above rests on.
        """
        items = iter_longmemeval(fixture_path=LME_FIXTURE)
        assert items
        for item in items:
            session_ids = {t["session_id"] for t in item.history}
            turn_ids = {t["turn_id"] for t in item.history}
            assert session_ids.isdisjoint(turn_ids)
            assert item.relevant_ids <= session_ids

    def test_scenario_ranks_sessions_end_to_end(self):
        """A LongMemEval-shaped run still reports session IDs."""
        result = ExternalScenario(item=_longmemeval_item()).execute()
        assert result.status == "ok"
        assert result.metadata["ranking_unit"] == "session"
        assert result.retrieved_ids == ["s1"]


class TestRankingUnitResolution:
    """The ranking unit comes from the dataset, never from the answer key."""

    def test_locomo_items_declare_turn_unit(self):
        """LoCoMo adapter items carry ``ground_truth_unit == "turn"``."""
        items = iter_locomo(fixture_path=LOCOMO_FIXTURE)
        assert items
        for item in items:
            assert item.metadata["ground_truth_unit"] == "turn"

    def test_longmemeval_items_declare_session_unit(self):
        """LongMemEval-S adapter items carry ``ground_truth_unit == "session"``."""
        items = iter_longmemeval(fixture_path=LME_FIXTURE)
        assert items
        for item in items:
            assert item.metadata["ground_truth_unit"] == "session"

    def test_dataset_name_alone_resolves_the_unit(self):
        """A run without the explicit metadata key resolves by dataset name."""
        assert ground_truth_unit("locomo") == "turn"
        assert ground_truth_unit("longmemeval-s") == "session"
        assert ground_truth_unit("longmemeval_s") == "session"
        # Unknown datasets keep the historical session behaviour used by
        # hand-built fixtures elsewhere in the suite.
        assert ground_truth_unit("something-else") == "session"

    def test_unit_resolution_ignores_relevant_ids(self):
        """The unit is unchanged when the answer key is emptied or replaced."""
        item = _locomo_item()
        assert _resolve_ranking_unit(item) == "turn"
        assert _resolve_ranking_unit(item._replace(relevant_ids=set())) == "turn"
        assert _resolve_ranking_unit(item._replace(relevant_ids={"s1"})) == "turn"

    def test_locomo_scenario_ranks_turn_ids_end_to_end(self):
        """A LoCoMo-shaped run ranks turn IDs, one rank slot per record."""
        result = ExternalScenario(item=_locomo_item()).execute()
        assert result.status == "ok"
        assert result.metadata["ranking_unit"] == "turn"
        assert "session_1" not in result.retrieved_ids
        assert result.retrieved_ids == _EXPECTED_TURN_RANKING
        # Six retrieved records, six rank slots — no session-level collapse of
        # the non-gold records (the old rule produced two slots here).
        assert len(result.retrieved_ids) == len(_hiking_history())

    def test_locomo_scenario_ranking_survives_a_withheld_answer_key(self):
        """Same item, answer key withheld or decoyed: identical ranked IDs.

        The end-to-end proof that ``relevant_ids`` reaches only the metric.
        The history is built so BM25 ranks it strictly (no ties), which makes
        exact list equality a meaningful assertion here.
        """
        with_key = ExternalScenario(item=_locomo_item()).execute()
        withheld = ExternalScenario(item=_locomo_item(relevant_ids=set())).execute()
        decoyed = ExternalScenario(
            item=_locomo_item(relevant_ids={"D1:0", "session_1"})
        ).execute()

        assert with_key.status == withheld.status == decoyed.status == "ok"
        assert with_key.retrieved_ids == _EXPECTED_TURN_RANKING
        assert withheld.retrieved_ids == _EXPECTED_TURN_RANKING
        assert decoyed.retrieved_ids == _EXPECTED_TURN_RANKING


class TestRankingUnitInReport:
    """The report header documents the unit granularity (issue #514)."""

    def test_aggregate_records_the_ranking_unit(self):
        """``compute_aggregate`` stamps the dataset's unit into the artifact."""
        locomo = compute_aggregate([], dataset="locomo")
        assert locomo["ranking_unit"]["unit"] == "turn"
        assert "gold-blind" in locomo["ranking_unit"]["dedup"]
        assert any("Ranking unit: turn" in note for note in locomo["notes"])

        lme = compute_aggregate([], dataset="longmemeval-s")
        assert lme["ranking_unit"]["unit"] == "session"

    def test_markdown_header_states_the_ranking_unit(self):
        """The rendered report header carries a ``Ranking unit`` line."""
        md = build_markdown_report(compute_aggregate([], dataset="locomo"))
        assert "**Ranking unit:** turn (gold-blind, first occurrence wins)" in md

    def test_pre_514_artifacts_render_without_claiming_a_unit(self):
        """An artifact with no ``ranking_unit`` is labelled, not guessed."""
        aggregate = compute_aggregate([], dataset="locomo")
        aggregate.pop("ranking_unit")
        md = build_markdown_report(aggregate)
        assert "**Ranking unit:** unrecorded (pre-#514 artifact)" in md
