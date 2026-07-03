"""CI gate for the deterministic CSR harness (issue #418).

Gates on determinism, suite health, report shape, the exact-comparison
discriminative check (the #409 query-blindness detector fires on the
known-blind composite control and stays quiet on the lexical path), and the
empty-fallback contract. RSR/Gap magnitudes are reported, never gated
(no calibrated baseline yet — see the plan's No-Gos).

Requires Redis (DB 15 via the popoto pytest plugin). Lexical/composite only:
no model download, no LLM, no Redis-module command.
"""

import pytest

from src.popoto.recipes.context_assembler import ContextAssembler
from tests.benchmarks.csr import (
    ADVERSARIAL_GAP_ALERT,
    ASSEMBLER_MAX_ITEMS,
    QUERY_BLIND_CSR_ALERT,
)
from tests.benchmarks.csr.corpus import (
    CsrTestCase,
    PlantedMemory,
    cleanup,
    plant,
)
from tests.benchmarks.csr.run_csr import (
    build_markdown_report,
    compute_aggregate,
    run_case,
    run_suite,
)
from tests.benchmarks.csr.satisfaction import (
    CoversTopic,
    Excludes,
    InTopK,
    NoneOlderThan,
    evaluate_all,
)
from tests.benchmarks.csr.suites.default import SUITE


@pytest.fixture(scope="module")
def suite_aggregate():
    """One full-suite run shared by the shape/discriminative tests."""
    return run_suite(SUITE)


def _case_result(aggregate, case_id):
    return next(c for c in aggregate["cases"] if c["case_id"] == case_id)


class TestSuiteHealth:
    def test_every_case_executes_without_error(self, suite_aggregate):
        assert suite_aggregate["summary"]["n_errors"] == 0
        for c in suite_aggregate["cases"]:
            assert c["status"] == "ok", f"{c['case_id']}: {c.get('error')}"

    def test_aggregate_shape(self, suite_aggregate):
        s = suite_aggregate["summary"]
        assert 0.0 <= s["rsr_std"] <= 1.0
        assert 0.0 <= s["rsr_adv"] <= 1.0
        assert s["adversarial_gap"] == s["rsr_std"] - s["rsr_adv"]
        assert s["n_cases"] == len(SUITE)
        # every primitive present in the per-primitive table
        assert set(suite_aggregate["per_primitive"]) == {
            case.primitive for case in SUITE
        }
        # every scored run records its executed path and query
        for c in suite_aggregate["cases"]:
            assert len(c["runs"]) == 2
            for run in c["runs"]:
                assert run["executed_path"] in (
                    "lexical",
                    "composite",
                    "composite-fallback",
                )
                assert run["total"] == c["n_assertions"]
            assert "rankings_identical" in c
            assert "query_blind_flag" in c

    def test_no_scored_lexical_run_used_the_composite_fallback(self, suite_aggregate):
        """The post-plant guard + authored fixtures keep every lexical case
        on the lexical path — a fallback may never masquerade as lexical."""
        for c in suite_aggregate["cases"]:
            if c["retrieval_mode"] == "auto":
                for run in c["runs"]:
                    assert run["executed_path"] == "lexical", (
                        f"{c['case_id']}/{run['run']} executed "
                        f"{run['executed_path']}"
                    )


class TestDeterminism:
    def test_double_run_identical(self):
        """Same suite twice -> bit-identical RSR(std), RSR(adv), Gap.

        This is the standing tripwire for BM25 score ties (Lua table.sort is
        unstable): a tie-sensitive corpus makes the two runs diverge here.
        """
        first = run_suite(SUITE)
        second = run_suite(SUITE)
        assert first["summary"]["rsr_std"] == second["summary"]["rsr_std"]
        assert first["summary"]["rsr_adv"] == second["summary"]["rsr_adv"]
        assert (
            first["summary"]["adversarial_gap"] == second["summary"]["adversarial_gap"]
        )
        # Per-case CSRs and rankings identical too, not just the means.
        for c1, c2 in zip(first["cases"], second["cases"]):
            assert c1["case_id"] == c2["case_id"]
            assert c1["csr_std"] == c2["csr_std"]
            assert c1["csr_adv"] == c2["csr_adv"]
            assert [r["ranked_ids"] for r in c1["runs"]] == [
                r["ranked_ids"] for r in c2["runs"]
            ]


class TestDiscriminativeCheck:
    """The #409 detector fires on the known-blind path, stays quiet on lexical.

    Exact comparisons only (ranking equality, control-vs-lexical CSR
    ordering) — never a calibrated numeric threshold.
    """

    def test_composite_control_is_query_blind(self, suite_aggregate):
        control = _case_result(suite_aggregate, "composite_control_409")
        # Identical rankings for standard and adversarial (Gap = 0 exactly):
        # _pull_path_composite never reads query text for ranking.
        assert control["rankings_identical"] is True
        assert control["csr_std"] == control["csr_adv"]
        # The detector (identical rankings AND low standard CSR) fires.
        assert control["csr_std"] < QUERY_BLIND_CSR_ALERT
        assert control["query_blind_flag"] is True

    def test_lexical_409_case_is_query_sensitive(self, suite_aggregate):
        lexical = _case_result(suite_aggregate, "query_blind_409")
        assert lexical["rankings_identical"] is False
        assert lexical["query_blind_flag"] is False
        for run in lexical["runs"]:
            assert run["executed_path"] == "lexical"

    def test_control_csr_below_lexical_csr_on_same_corpus(self, suite_aggregate):
        control = _case_result(suite_aggregate, "composite_control_409")
        lexical = _case_result(suite_aggregate, "query_blind_409")
        assert control["csr_std"] < lexical["csr_std"]

    def test_lexical_gap_is_reported_as_keyword_dependence(self, suite_aggregate):
        """A LARGE lexical gap is the healthy signature — reported, not the
        #409 signal. The seed suite's authored paraphrases produce one."""
        s = suite_aggregate["summary"]
        assert s["adversarial_gap"] >= ADVERSARIAL_GAP_ALERT
        assert s["gap_alert"] is True
        assert s["query_blind_cases"] == ["composite_control_409"]


class TestEmptyFallbackContract:
    """score_weights={} + zero-BM25-hit query -> records == [].

    Pins the chain: zero hits -> _pull_path_hybrid falls back to
    _pull_path_composite -> composite_score(indexes={}) raises QueryException
    -> swallowed -> result.records == []. InTopK/CoversTopic fail;
    Excludes/NoneOlderThan vacuously pass. A future popoto change to any
    link trips this test instead of silently shifting behavior.
    """

    def test_empty_fallback_contract(self):
        case = CsrTestCase(
            case_id="fallback_contract",
            primitive="contract",
            planted_corpus=(
                PlantedMemory("m-1", "falcon telescope nebula", 100.0, "alpha"),
                PlantedMemory("m-2", "noodle lunch downtown", 100.0, "beta"),
            ),
            standard_query="falcon telescope",
            adversarial_query="noodle clouds",
            assertions=(InTopK("m-1", 5),),
            relevant_ids=frozenset({"m-1"}),
        )
        model_class, agent_id, _, _ = plant(case)
        try:
            assembler = ContextAssembler(
                model_class,
                score_weights={},
                max_items=ASSEMBLER_MAX_ITEMS,
                retrieval_mode="auto",
            )
            assert assembler._effective_mode == "lexical"
            # Zero-hit query: no corpus token overlap at all.
            result = assembler.assemble(
                query_cues={"topic": "xylophone zephyr quixotic"},
                agent_id=agent_id,
            )
            assert result.records == []

            planted_meta = {
                m.id: {"timestamp": m.timestamp, "topic": m.topic}
                for m in case.planted_corpus
            }
            assertions = [
                InTopK("m-1", 5),
                CoversTopic("alpha", 5),
                Excludes("m-2", 5),
                NoneOlderThan(50.0, 5),
            ]
            passed, total, detail = evaluate_all(assertions, [], planted_meta)
            outcomes = {d["type"]: d["passed"] for d in detail}
            assert outcomes == {
                "InTopK": False,
                "CoversTopic": False,
                "Excludes": True,
                "NoneOlderThan": True,
            }
            assert (passed, total) == (2, 4)
        finally:
            cleanup(model_class, agent_id)


class TestErrorHandling:
    def test_assembler_error_marks_case_error_not_crash(self):
        """A case whose assembler construction raises surfaces as
        status="error" with zero passed assertions."""
        bad = CsrTestCase(
            case_id="bad_mode",
            primitive="contract",
            planted_corpus=(
                PlantedMemory("m-1", "falcon telescope nebula", 100.0, "alpha"),
                PlantedMemory("m-2", "noodle lunch downtown", 100.0, "beta"),
            ),
            standard_query="falcon telescope",
            adversarial_query="noodle clouds",
            assertions=(InTopK("m-1", 5),),
            relevant_ids=frozenset({"m-1"}),
            retrieval_mode="bogus-mode",
        )
        result = run_case(bad)
        assert result["status"] == "error"
        assert result["csr_std"] == 0.0
        assert result["csr_adv"] == 0.0
        assert "bogus-mode" in result["error"]

    def test_error_case_lowers_rsr_and_renders_in_markdown(self):
        error_case = {
            "case_id": "boom",
            "primitive": "p",
            "retrieval_mode": "auto",
            "n_memories": 1,
            "n_assertions": 1,
            "status": "error",
            "error": "RuntimeError: boom",
            "runs": [],
            "csr_std": 0.0,
            "csr_adv": 0.0,
            "rankings_identical": False,
            "query_blind_flag": False,
        }
        ok_case = {
            **error_case,
            "case_id": "fine",
            "status": "ok",
            "csr_std": 1.0,
            "csr_adv": 1.0,
            "runs": [
                {
                    "run": "standard",
                    "query": "q",
                    "executed_path": "lexical",
                    "effective_mode": "lexical",
                    "ranked_ids": ["a"],
                    "passed": 1,
                    "total": 1,
                    "csr": 1.0,
                    "assertions": [],
                },
                {
                    "run": "adversarial",
                    "query": "q2",
                    "executed_path": "lexical",
                    "effective_mode": "lexical",
                    "ranked_ids": ["a"],
                    "passed": 1,
                    "total": 1,
                    "csr": 1.0,
                    "assertions": [],
                },
            ],
        }
        aggregate = compute_aggregate([error_case, ok_case])
        assert aggregate["summary"]["n_errors"] == 1
        assert aggregate["summary"]["rsr_std"] == 0.5  # error counts as 0
        md = build_markdown_report(aggregate)
        # error case renders as a visible row, never a silent drop
        assert "ERROR: RuntimeError: boom" in md
        # gap = 0 here -> no gap-alert flag
        assert "Gap alert" not in md
        assert "Query-blindness flags" not in md

    def test_markdown_flag_rendering_branches(self):
        """Gap alert iff gap >= ADVERSARIAL_GAP_ALERT; query-blind flag iff
        rankings_identical AND csr_std < QUERY_BLIND_CSR_ALERT."""
        blind_case = {
            "case_id": "blind",
            "primitive": "p",
            "retrieval_mode": "composite",
            "n_memories": 1,
            "n_assertions": 1,
            "status": "ok",
            "runs": [],
            "csr_std": 1.0,
            "csr_adv": 1.0 - ADVERSARIAL_GAP_ALERT,
            "rankings_identical": True,
            "query_blind_flag": True,
        }
        aggregate = compute_aggregate([blind_case])
        assert aggregate["summary"]["gap_alert"] is True
        md = build_markdown_report(aggregate)
        assert "Gap alert" in md
        assert "Query-blindness flags" in md
        assert "`blind`" in md
