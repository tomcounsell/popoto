"""Tests for the end-to-end judged-answer harness (Tier 5, issue #458).

Every test runs with **no ``openai`` and no API key**: the LLM client is a
:class:`FakeClient` injected via the :class:`JudgeProtocol` seam, and
``is_judge_available`` / ``build_openai_client`` are monkeypatched for the
``main()`` end-to-end cases. The default suite therefore never reaches out to an
external API.
"""

from pathlib import Path

import pytest

from tests.benchmarks import judge as judge_mod
from tests.benchmarks import run_external as R
from tests.benchmarks.datasets import BenchmarkItem

FIXTURE = Path(__file__).parent / "datasets" / "fixtures" / "locomo_sample.json"

# Committed pins — a change to either is a loud failure (that's the point).
EXPECTED_JUDGE_PROMPT_SHA256 = (
    "cb44a52ee9ba372f0ec81c3dd0a6d49190da808fef56760e3424592bd146340d"
)


class FakeClient:
    """A JudgeProtocol fake. Discriminates generation vs judge calls by the
    presence of a system message (only the judge call has one), and records every
    call for assertions."""

    def __init__(self, gen_answer="a shell necklace", judge_label="CORRECT"):
        self.gen_answer = gen_answer
        self.judge_label = judge_label
        self.calls = []

    def chat(self, model, messages, temperature):
        self.calls.append(
            {"model": model, "messages": messages, "temperature": temperature}
        )
        is_judge = any(m["role"] == "system" for m in messages)
        if is_judge:
            return f'Reasoning here. {{"label": "{self.judge_label}"}}'
        return self.gen_answer


# ---------------------------------------------------------------------------
# Pins (requirements 1 & 2)
# ---------------------------------------------------------------------------


def test_judge_model_is_pinned_gpt4o_mini():
    assert judge_mod.JUDGE_MODEL == "gpt-4o-mini"


def test_generation_model_is_pinned_gpt4o_mini():
    # Generator is also pinned so the documented cost figures stay valid.
    assert judge_mod.GENERATION_MODEL == "gpt-4o-mini"


def test_temperature_pinned_zero():
    assert judge_mod.JUDGE_TEMPERATURE == 0


def test_judge_prompt_hash_is_pinned():
    # Guards against silent prompt drift — judged accuracy is not comparable
    # across prompt variants.
    assert judge_mod.prompt_sha256(judge_mod.LLM_JUDGE_PROMPT) == (
        EXPECTED_JUDGE_PROMPT_SHA256
    )


def test_judge_prompt_is_the_mem0_gam_protocol():
    p = judge_mod.LLM_JUDGE_PROMPT
    # Signature phrasing from the verbatim Mem0/GAM ACCURACY_PROMPT.
    assert "label an answer to a question as 'CORRECT' or 'WRONG'" in p
    assert "Do you remember what I got the last time I went to Hawaii?" in p
    assert 'json format with the key as "label"' in p
    assert "{question}" in p and "{gold_answer}" in p and "{generated_answer}" in p


def test_identity_block_records_judge_provenance():
    ident = judge_mod.judge_identity()
    assert ident["judge_model"] == "gpt-4o-mini"
    assert ident["generation_model"] == "gpt-4o-mini"
    assert ident["temperature"] == 0
    assert ident["protocol"] == "mem0/gam"
    assert ident["protocol_ref"] == "arXiv:2504.19413"
    assert ident["judge_prompt_sha256"] == EXPECTED_JUDGE_PROMPT_SHA256


# ---------------------------------------------------------------------------
# Judge response parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"label": "CORRECT"}', "CORRECT"),
        ('{"label": "WRONG"}', "WRONG"),
        ("{'label': 'correct'}", "CORRECT"),  # single quotes, lowercase
        ('Because it matches. {"label": "CORRECT"}', "CORRECT"),
        ("The answer is CORRECT", "CORRECT"),  # bare token fallback
        ("Nope, WRONG.", "WRONG"),
        ("Includes both CORRECT and WRONG somehow", "WRONG"),  # ambiguous -> WRONG
        ("total gibberish", "WRONG"),  # unparseable -> WRONG
        ("", "WRONG"),  # empty -> WRONG
    ],
)
def test_parse_label(text, expected):
    assert judge_mod._parse_label(text) == expected


# ---------------------------------------------------------------------------
# Generation + judging with the fake
# ---------------------------------------------------------------------------


def test_generate_answer_uses_pinned_model_and_context():
    client = FakeClient(gen_answer="the answer")
    out = judge_mod.generate_answer(client, "What did I buy?", ["bought a necklace"])
    assert out == "the answer"
    call = client.calls[0]
    assert call["model"] == "gpt-4o-mini"
    assert call["temperature"] == 0
    assert "bought a necklace" in call["messages"][0]["content"]
    assert "What did I buy?" in call["messages"][0]["content"]


def test_judge_answer_uses_pinned_model_and_temperature():
    client = FakeClient(judge_label="CORRECT")
    label, raw = judge_mod.judge_answer(client, "Q?", "gold", "generated")
    assert label == "CORRECT"
    assert "CORRECT" in raw
    call = client.calls[0]
    assert call["model"] == "gpt-4o-mini"
    assert call["temperature"] == 0
    # judge call carries a system message; gold+generated are interpolated.
    assert call["messages"][0]["role"] == "system"
    assert "gold" in call["messages"][1]["content"]
    assert "generated" in call["messages"][1]["content"]


# ---------------------------------------------------------------------------
# Availability gating
# ---------------------------------------------------------------------------


def test_is_judge_available_false_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert judge_mod.is_judge_available() is False


def test_is_judge_available_false_without_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "openai":
            raise ImportError("no openai")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert judge_mod.is_judge_available() is False


def test_build_openai_client_raises_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        judge_mod.build_openai_client()


# ---------------------------------------------------------------------------
# Retry / fault isolation (blocker B3)
# ---------------------------------------------------------------------------


def test_call_with_retry_recovers_after_transient_failures():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("429")
        return "ok"

    out = judge_mod.call_with_retry(flaky, attempts=3, sleep=lambda _s: None)
    assert out == "ok"
    assert attempts["n"] == 3


def test_call_with_retry_reraises_after_exhaustion():
    def always_fail():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        judge_mod.call_with_retry(always_fail, attempts=2, sleep=lambda _s: None)


# ---------------------------------------------------------------------------
# judge_one — per-item generate+judge
# ---------------------------------------------------------------------------


def _mk_qresult(retrieved_contents, question_type=1):
    return R.QuestionResult(
        item_id="i1",
        recall_at_1=1.0,
        recall_at_5=1.0,
        recall_at_10=1.0,
        mrr=1.0,
        retrieval_ms=1.0,
        status="ok",
        metadata={
            "retrieved_contents": retrieved_contents,
            "question_type": question_type,
        },
    )


def _mk_item(item_id="i1", answer="a shell necklace", adversarial=False, qt=1):
    md = {"answer": answer, "question_type": qt, "dataset": "locomo"}
    if adversarial:
        md["adversarial"] = True
    return BenchmarkItem(
        item_id=item_id,
        history=[],
        query="What did I buy in Hawaii?",
        relevant_ids={"x"},
        metadata=md,
    )


def test_judge_one_ok():
    client = FakeClient(gen_answer="a shell necklace", judge_label="CORRECT")
    out = R.judge_one(_mk_item(), _mk_qresult(["I bought a shell necklace"]), client)
    assert out["judge_status"] == "ok"
    assert out["judge_label"] == "CORRECT"
    assert out["generated_answer"] == "a shell necklace"
    assert out["adversarial"] is False


def test_judge_one_records_error_without_crashing():
    class BoomClient:
        def chat(self, model, messages, temperature):
            raise RuntimeError("api down")

    out = R.judge_one(
        _mk_item(),
        _mk_qresult(["ctx"]),
        BoomClient(),
        retry_sleep=lambda _s: None,
    )
    assert out["judge_status"] == "judge_error"
    assert out["judge_label"] == ""
    assert "api down" in out["judge_error"]


def test_judge_one_detects_adversarial():
    client = FakeClient()
    out = R.judge_one(
        _mk_item(adversarial=True, qt=5),
        _mk_qresult(["ctx"], question_type=5),
        client,
    )
    assert out["adversarial"] is True


# ---------------------------------------------------------------------------
# compute_judged_block — aggregation, adversarial exclusion (concern C4)
# ---------------------------------------------------------------------------


def _jr(item_id, label, status="ok", adversarial=False, qt=1):
    return {
        "item_id": item_id,
        "question_type": qt,
        "adversarial": adversarial,
        "judge_label": label,
        "judge_status": status,
        "generated_answer": "x",
    }


def test_compute_judged_block_excludes_adversarial_and_errors():
    judged = [
        _jr("a", "CORRECT"),
        _jr("b", "WRONG"),
        _jr("c", "CORRECT"),
        _jr("d", "CORRECT", adversarial=True, qt=5),  # excluded from headline
        _jr("e", "", status="judge_error"),  # excluded from headline
    ]
    block = R.compute_judged_block(judged, n_total=6)
    # scored = a,b,c -> 2 correct / 3 = 0.6667
    assert block["n_scored"] == 3
    assert block["n_correct"] == 2
    assert block["judged_accuracy"] == pytest.approx(0.6667, abs=1e-4)
    assert block["n_judge_errors"] == 1
    assert block["n_adversarial_excluded"] == 1
    # one item never judged (n_total=6, judged=5)
    assert block["n_judged_skipped"] == 1
    # adversarial reported separately, not folded into headline
    assert block["adversarial"]["n"] == 1
    assert block["adversarial"]["n_correct"] == 1
    assert "#454" in block["adversarial"]["note"]


def test_compute_judged_block_by_question_type():
    judged = [
        _jr("a", "CORRECT", qt=1),
        _jr("b", "WRONG", qt=1),
        _jr("c", "CORRECT", qt=2),
    ]
    block = R.compute_judged_block(judged, n_total=3)
    bqt = block["by_question_type"]
    assert bqt["1"]["n"] == 2 and bqt["1"]["n_correct"] == 1
    assert bqt["2"]["judged_accuracy"] == 1.0


# ---------------------------------------------------------------------------
# Artifact suffix composition (blocker B1)
# ---------------------------------------------------------------------------


def _minimal_judged_aggregate():
    agg = R.compute_aggregate([], "locomo", retrieval_mode="lexical")
    agg["judge"] = judge_mod.judge_identity()
    agg["judged"] = R.compute_judged_block([_jr("a", "CORRECT")], n_total=1)
    return agg


def test_save_reports_lexical_judged_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RESULTS_DIR", tmp_path)
    jp, mp = R.save_reports(
        _minimal_judged_aggregate(), "locomo", retrieval_mode="lexical", judged=True
    )
    assert jp.name.endswith("_judged.json")
    assert "_hybrid" not in jp.name
    assert (tmp_path / "locomo_latest_judged.json").exists()


def test_save_reports_hybrid_judged_suffix_no_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RESULTS_DIR", tmp_path)
    agg = _minimal_judged_aggregate()
    agg["retrieval_mode"] = "hybrid"
    jp, mp = R.save_reports(agg, "locomo", retrieval_mode="hybrid", judged=True)
    # Composite suffix: must be _hybrid_judged, never plain _judged (which would
    # clobber the lexical-judged artifact) or plain _hybrid (retrieval artifact).
    assert jp.name.endswith("_hybrid_judged.json")
    assert (tmp_path / "locomo_latest_hybrid_judged.json").exists()
    # The lexical-judged and plain-hybrid names are untouched.
    assert not (tmp_path / "locomo_latest_judged.json").exists()


# ---------------------------------------------------------------------------
# Markdown report — two separate metric families (concern / #453)
# ---------------------------------------------------------------------------


def test_markdown_reports_families_separately():
    md = R.build_markdown_report(_minimal_judged_aggregate())
    assert "## Summary" in md  # retrieval recall table
    assert "## Judged Answer Accuracy" in md  # judged table
    assert "MUST NOT be cross-compared" in md
    assert "gpt-4o-mini" in md


# ---------------------------------------------------------------------------
# main() — CLI-level behaviors
# ---------------------------------------------------------------------------


def _run_main(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["run_external"] + argv)
    return R.main()


def test_main_rejects_vector_judged(monkeypatch):
    rc = _run_main(
        monkeypatch,
        ["--dataset", "locomo", "--retrieval-mode", "vector", "--judged"],
    )
    assert rc == 1


def test_main_judged_graceful_skip_without_key(monkeypatch, tmp_path):
    monkeypatch.setattr(judge_mod, "is_judge_available", lambda: False)
    rc = _run_main(
        monkeypatch,
        [
            "--dataset",
            "locomo",
            "--judged",
            "--fixture",
            str(FIXTURE),
            "--limit",
            "2",
            "--output",
            str(tmp_path),
        ],
    )
    assert rc == 0
    # No artifacts written on graceful skip.
    assert list(tmp_path.glob("*_judged.*")) == []


def test_main_judged_end_to_end_mocked(monkeypatch, tmp_path):
    monkeypatch.setattr(judge_mod, "is_judge_available", lambda: True)
    fake = FakeClient(gen_answer="a shell necklace", judge_label="CORRECT")
    monkeypatch.setattr(judge_mod, "build_openai_client", lambda: fake)
    rc = _run_main(
        monkeypatch,
        [
            "--dataset",
            "locomo",
            "--judged",
            "--fixture",
            str(FIXTURE),
            "--limit",
            "3",
            "--output",
            str(tmp_path),
        ],
    )
    assert rc == 0
    judged_jsons = list(tmp_path.glob("locomo_*_judged.json"))
    assert judged_jsons, "expected a _judged.json artifact"
    import json

    data = json.loads((tmp_path / "locomo_latest_judged.json").read_text())
    # Both metric families present, side by side, in separate blocks.
    assert "summary" in data  # retrieval recall
    assert "judged" in data  # judged accuracy
    assert data["judge"]["judge_model"] == "gpt-4o-mini"
    assert data["judge"]["judge_prompt_sha256"] == EXPECTED_JUDGE_PROMPT_SHA256
    # At least one item got judged, and generation actually ran through the fake.
    assert data["judged"]["n_judged"] >= 1
    assert fake.calls, "fake client should have been called"
