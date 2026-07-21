"""Refusal-metric benchmark for confidence-gated retrieval (issue #463).

Measures how well ``ContextAssembler``'s confidence gate
(``confidence_gate_threshold`` / ``confidence_gate_mode``) identifies
genuinely-unanswerable questions on the LoCoMo category-5 ("adversarial")
slice.

WHY THIS IS SEEDED, NOT ORGANIC (cold-start degeneracy)
---------------------------------------------------------------------------
LoCoMo's benchmark harness performs single-shot retrieval: history is
ingested once, one query is run, and there is no correction loop (no
``update_confidence`` calls from a real interaction/outcome cycle). Every
``ConfidenceField`` therefore starts, and stays, at the same
``initial_confidence`` (0.5) for every candidate, because
``ConfidenceField.get_confidence()`` returns ``initial_confidence`` until
some observation touches the companion hash. Without intervention the gate's
``gate_score`` would be a *constant* 0.5 across all 446 cat-5 items, so at any
fixed threshold the gate would either refuse *everything* or *nothing* — a
degenerate, uninformative result that says nothing about the gate mechanism.

To make the gate exercisable at all, this module manually seeds a spread of
``ConfidenceField`` values (deterministic, content-hash-derived, spanning
roughly [0.05, 0.95]) onto the ingested records before querying, simulating
"a realistic post-interaction confidence spread" a live agent might
accumulate over many corrections. This is a **simulation of a spread**, not
a measurement of one — see ``docs/benchmarks.md`` for the full caveat this
module's report reproduces.

WHY THE HEADLINE NUMBER IS STATISTICALLY THIN
---------------------------------------------------------------------------
Per the #454 / PR #471 cat-5 scoring audit, only **2 of 446** cat-5 items are
genuinely unanswerable ("Not mentioned"-style ground truth); the other 444
are answerable/evidence-grounded despite carrying the ``adversarial`` flag.
Refusal precision = TP / (TP + FP) where TP = correctly-refused-unanswerable
and FP = refused-but-answerable therefore has **at most 2 true positives** —
a single false positive swings the reported precision by tens of points.
This number is NOT leaderboard-comparable and must never be cross-compared
against recall/judged-accuracy metrics from other benchmark families (see
the repo's metric-family doctrine in docs/benchmarks.md).

Two test paths (mirrors tests/benchmarks/test_external.py's fixture-vs-
download split):

1. ``TestRefusalGateMechanics`` — deterministic, seeded, synthetic fixture.
   No network/cache dependency; runs unconditionally in CI. Asserts gate
   *mechanics* work: a low-confidence unanswerable-style candidate is
   refused, a high-confidence answerable-style candidate is not.
2. ``TestRefusalPrecisionLoCoMoCat5`` — the actual 446-item cat-5 refusal-
   precision number-producer. Skipped unless
   ``~/.cache/popoto_benchmarks/locomo.json`` is present (network/cache
   gated, mirroring the hybrid/vector MiniLM-cache skip pattern in
   test_external.py).
"""

import hashlib
import logging
import uuid
from pathlib import Path
from typing import Dict, List

import pytest

from src import popoto
from src.popoto.fields.bm25_field import BM25Field
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.recipes.context_assembler import (
    EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD,
    ContextAssembler,
)
from src.popoto.redis_db import POPOTO_REDIS_DB
from tests.benchmarks.datasets.locomo import CACHED_FILE, iter_items

logger = logging.getLogger("POPOTO.Benchmark.ConfidenceGateRefusal")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _seed_confidence(item, field_name: str, confidence: float) -> None:
    """Write a ConfidenceField companion-hash value directly.

    Bypasses ``update_confidence()``'s capped-Bayesian update rule (which
    only asymptotically approaches a target after many calls) so the
    seeded corpus has an exact, deterministic confidence spread. Mirrors
    ``tests/test_confidence_field.py::_seed_confidence_data``.
    """
    import msgpack

    field = item._meta.fields[field_name]
    data_hash_key = field.get_data_hash_key(item, field_name)
    member_key = item.db_key.redis_key
    POPOTO_REDIS_DB.hset(
        data_hash_key,
        member_key,
        msgpack.packb(
            {
                "confidence": confidence,
                "evidence_count": 1,
                "corroborations": 1 if confidence >= 0.5 else 0,
                "contradictions": 0 if confidence >= 0.5 else 1,
            }
        ),
    )


def _content_derived_confidence(content: str) -> float:
    """Deterministic pseudo-random confidence in [0.05, 0.95] from content.

    Used to simulate a non-degenerate post-interaction confidence spread
    across an ingested corpus (see module docstring — cold-start caveat).
    Deterministic (content-hash-derived) so the benchmark is reproducible
    run-to-run without a real interaction/outcome history.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    frac = int(digest[:8], 16) / 0xFFFFFFFF  # in [0, 1]
    return 0.05 + frac * 0.90  # in [0.05, 0.95]


def _is_cat5(item) -> bool:
    """True if ``item`` is a LoCoMo category-5 ("adversarial") question.

    Uses ``question_type`` (``qa["category"]``), NOT the loader's
    ``metadata["adversarial"]`` flag: that flag is defined as "evidence"
    absent from the QA pair, but per the #454 / PR #471 audit **446/446**
    cat-5 items in this dataset snapshot carry a populated ``evidence``
    field, so ``adversarial`` is (almost) always False here even for true
    cat-5 items. ``question_type`` is the reliable category signal — this
    mirrors ``tests/benchmarks/run_external.py::_is_adversarial``, which
    checks both signals for the same reason.
    """
    return item.metadata.get("question_type") in (5, "5")


def _is_genuinely_unanswerable(item) -> bool:
    """True if ``item`` (a LoCoMo BenchmarkItem) is genuinely unanswerable.

    NOT determined by the cat-5 / ``question_type==5`` membership alone —
    per the #454 / PR #471 audit, 444/446 cat-5 items carry an answerable
    ``adversarial_answer`` grounded in evidence. Only items whose ground-
    truth answer text is itself a refusal ("Not mentioned"-style) are
    genuinely unanswerable. This mirrors the identification method the
    #471 audit used (see docs/benchmarks.md's cat-5 audit section).
    """
    if not _is_cat5(item):
        return False
    answer = str(item.metadata.get("answer") or "")
    return "not mentioned" in answer.lower()


def _build_refusal_model(prefix: str):
    """Minimal lexical-mode Popoto model: BM25Field + ConfidenceField.

    Mirrors ``tests/benchmarks/scenarios/external_base.py``'s lexical
    ``ExternalBenchmarkMemory`` build, trimmed to only what the gate needs
    (no DecayingSortedField/push path — this benchmark only exercises the
    pull-path gate).
    """

    class RefusalMemory(popoto.Model):
        turn_id = popoto.AutoKeyField()
        agent_id = popoto.KeyField()
        content = popoto.StringField(default="")
        certainty = ConfidenceField(initial_confidence=0.5)
        content_index = BM25Field(source="content")

    RefusalMemory.__name__ = f"RefusalMem{prefix}"
    RefusalMemory.__qualname__ = RefusalMemory.__name__
    return RefusalMemory


def _teardown_model(model_class) -> None:
    """Scan-and-delete all Redis keys for a benchmark-local model class."""
    if model_class is None:
        return
    class_name = model_class.__name__
    try:
        cursor = 0
        while True:
            cursor, keys = POPOTO_REDIS_DB.scan(
                cursor, match=f"{class_name}:*", count=200
            )
            if keys:
                POPOTO_REDIS_DB.delete(*keys)
            if cursor == 0:
                break
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Path 1: deterministic seeded-fixture unit test (CI-safe, no network)
# ---------------------------------------------------------------------------


class TestRefusalGateMechanics:
    """Gate mechanics on a small, fully-controlled synthetic fixture.

    No LoCoMo download/cache dependency — confidence values are seeded
    directly, so gate behavior is deterministic and this class runs
    unconditionally in CI. This asserts the *mechanism* the full-corpus
    test below measures against real (seeded) LoCoMo data.
    """

    @pytest.fixture(autouse=True)
    def _model(self):
        prefix = uuid.uuid4().hex[:8]
        model_class = _build_refusal_model(prefix)
        yield model_class
        _teardown_model(model_class)

    def test_low_confidence_unanswerable_style_candidate_is_refused(self, _model):
        """A rank-0 candidate seeded well below threshold gets refused.

        Simulates a genuinely-unanswerable question: the best pull-path
        match is a poor match, so its (simulated post-interaction)
        confidence is low.
        """
        record = _model(
            agent_id="fixture-a",
            content="The weather was cloudy that afternoon.",
        )
        record.save()
        _seed_confidence(record, "certainty", 0.1)

        assembler = ContextAssembler(
            model_class=_model,
            score_weights={},
            confidence_gate_threshold=EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD,
            confidence_gate_mode="refuse",
        )
        assembler._pull_path = lambda cues, filters: ([record], [record])

        result = assembler.assemble(
            query_cues={"topic": "unrelated adversarial question"},
            partition_filters={"agent_id": "fixture-a"},
        )

        assert result.metadata["gate"]["gated"] is True
        assert result.metadata["gate"]["applied"] is True
        assert result.metadata["pull_count"] == 0

    def test_high_confidence_answerable_style_candidate_is_not_refused(self, _model):
        """A rank-0 candidate seeded well above threshold is retained.

        Simulates an answerable question: the assembler found a
        well-corroborated match and should inject it, not refuse it.
        """
        record = _model(
            agent_id="fixture-b",
            content="Jon took a temporary job as a delivery driver.",
        )
        record.save()
        _seed_confidence(record, "certainty", 0.9)

        assembler = ContextAssembler(
            model_class=_model,
            score_weights={},
            confidence_gate_threshold=EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD,
            confidence_gate_mode="refuse",
        )
        assembler._pull_path = lambda cues, filters: ([record], [record])

        result = assembler.assemble(
            query_cues={"topic": "what temporary job did Jon take"},
            partition_filters={"agent_id": "fixture-b"},
        )

        assert result.metadata["gate"]["gated"] is False
        assert result.metadata["gate"]["applied"] is True
        assert result.metadata["pull_count"] == 1

    def test_seeded_spread_precision_on_tiny_synthetic_corpus(self, _model):
        """End-to-end refusal-precision computation on a 4-item synthetic
        corpus (2 unanswerable / low-confidence, 2 answerable /
        high-confidence) — proves the precision formula (TP/(TP+FP)) used
        by the full-corpus test below is correct and yields 1.0 when the
        seeded spread perfectly separates unanswerable from answerable.
        """
        items = [
            # (content, is_unanswerable, seeded_confidence)
            ("Not mentioned in the conversation about Jon's job.", True, 0.05),
            ("Not mentioned in the conversation about Gina's job.", True, 0.08),
            ("Caroline realized self-care is important.", False, 0.85),
            ("The dog's name was Max.", False, 0.92),
        ]
        records = []
        truth = {}
        for content, is_unanswerable, confidence in items:
            record = _model(agent_id="fixture-c", content=content)
            record.save()
            _seed_confidence(record, "certainty", confidence)
            records.append(record)
            truth[record.db_key.redis_key] = is_unanswerable

        tp = 0
        fp = 0
        for record in records:
            assembler = ContextAssembler(
                model_class=_model,
                score_weights={},
                confidence_gate_threshold=EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD,
                confidence_gate_mode="refuse",
            )
            assembler._pull_path = lambda cues, filters, r=record: ([r], [r])
            result = assembler.assemble(
                query_cues={"topic": "probe"},
                partition_filters={"agent_id": "fixture-c"},
            )
            refused = result.metadata["gate"]["gated"]
            is_unanswerable = truth[record.db_key.redis_key]
            if refused and is_unanswerable:
                tp += 1
            elif refused and not is_unanswerable:
                fp += 1

        assert tp == 2
        assert fp == 0
        precision = tp / (tp + fp)
        assert precision == 1.0


# ---------------------------------------------------------------------------
# Path 2: full-corpus refusal-precision number-producer (cache-gated)
# ---------------------------------------------------------------------------


def _locomo_cache_present() -> bool:
    return Path(CACHED_FILE).exists()


@pytest.mark.skipif(
    not _locomo_cache_present(),
    reason=(
        "LoCoMo cache absent at ~/.cache/popoto_benchmarks/locomo.json — "
        "the full-corpus refusal-precision number-producer only runs when "
        "the dataset has already been downloaded/cached (network/cache-gated, "
        "mirrors test_external.py's MiniLM-cache skip pattern)."
    ),
)
class TestRefusalPrecisionLoCoMoCat5:
    """The actual 446-item cat-5 refusal-precision report.

    NOT a CI gate — this class only runs when the LoCoMo dataset is already
    cached locally. It ingests each of the (10) dialogues containing cat-5
    questions once, seeds a deterministic (content-hash-derived) confidence
    spread onto every ingested record — see the module docstring for why
    this seeding is necessary — then runs the confidence gate (at
    ``EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD``, lexical retrieval mode, mode
    "refuse") for every one of the 446 cat-5 questions and computes refusal
    precision = TP / (TP + FP) against the 2 genuinely-unanswerable ground
    truths identified by ``_is_genuinely_unanswerable``.

    The printed report leads with the cold-start-seeding and ≤2-true-
    positive statistical-thinness caveats — this number is illustrative of
    the gate *mechanism*, not a leaderboard-comparable refusal-accuracy
    claim (see docs/benchmarks.md).
    """

    def test_refusal_precision_on_full_cat5_slice(self, capsys):
        all_items = iter_items(fixture_path=None)
        cat5_items = [it for it in all_items if _is_cat5(it)]
        assert cat5_items, "expected LoCoMo cat-5 items in the cached dataset"

        unanswerable_ids = {
            it.item_id for it in cat5_items if _is_genuinely_unanswerable(it)
        }
        # Load-bearing premise from the #454 / PR #471 audit: exactly 2/446.
        # A drifted dataset snapshot would invalidate the caveat text below,
        # so fail loudly rather than silently reporting a stale number.
        assert len(unanswerable_ids) == 2, (
            f"expected exactly 2 genuinely-unanswerable cat-5 items per the "
            f"#454/#471 audit, found {len(unanswerable_ids)}: {unanswerable_ids}"
        )

        # Group items by sample_id so each dialogue's history is ingested
        # exactly once (cat-5 questions within a sample share history).
        items_by_sample: Dict[str, List] = {}
        for item in cat5_items:
            items_by_sample.setdefault(item.metadata["sample_id"], []).append(item)

        model_class = _build_refusal_model(uuid.uuid4().hex[:8])
        try:
            tp = 0
            fp = 0
            n_gated = 0
            for sample_id, items in items_by_sample.items():
                # Ingest this dialogue's shared history once.
                history = items[0].history
                agent_id = f"cat5:{sample_id}"
                records = []
                for turn in history:
                    content = turn.get("content", "")
                    if not content.strip():
                        continue
                    record = model_class(agent_id=agent_id, content=content.strip())
                    record.save()
                    records.append(record)
                    # Seed a deterministic, non-degenerate confidence spread
                    # (see module docstring — cold-start caveat).
                    _seed_confidence(
                        record,
                        "certainty",
                        _content_derived_confidence(content.strip()),
                    )

                if not records:
                    continue

                for item in items:
                    assembler = ContextAssembler(
                        model_class=model_class,
                        score_weights={},
                        max_items=5,
                        retrieval_mode="lexical",
                        confidence_gate_threshold=(
                            EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD
                        ),
                        confidence_gate_mode="refuse",
                    )
                    result = assembler.assemble(
                        query_cues={"topic": item.query},
                        partition_filters={"agent_id": agent_id},
                    )
                    gated = result.metadata["gate"]["gated"]
                    if gated:
                        n_gated += 1
                        if item.item_id in unanswerable_ids:
                            tp += 1
                        else:
                            fp += 1
        finally:
            _teardown_model(model_class)

        precision = tp / (tp + fp) if (tp + fp) > 0 else None

        report_lines = [
            "",
            "=" * 78,
            "Refusal precision (confidence gate) — ILLUSTRATIVE, NOT LEADERBOARD-GRADE",
            "=" * 78,
            "CAVEAT: this is a SEEDED SIMULATION, not an organic measurement.",
            "  - LoCoMo cat-5 has only 2/446 genuinely-unanswerable items (per the",
            "    #454 / PR #471 audit) -> refusal precision has <= 2 true positives;",
            "    a single false positive swings the number by tens of points.",
            "  - ConfidenceField gating is cold-start-degenerate on this single-shot",
            "    corpus (no correction loop -> constant initial_confidence for every",
            "    candidate), so the confidence spread used here was MANUALLY SEEDED",
            "    (deterministic, content-hash-derived) to make the gate exercisable.",
            "  - Do NOT cross-compare this number with any leaderboard recall/",
            "    judged-accuracy metric (metric-family doctrine).",
            "-" * 78,
            f"cat-5 items evaluated       : {len(cat5_items)}",
            f"genuinely unanswerable (TP-eligible): {len(unanswerable_ids)}",
            f"gate threshold               : {EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD}",
            "gate mode                    : refuse",
            f"items refused                : {n_gated}",
            f"true positives  (TP)         : {tp}",
            f"false positives (FP)         : {fp}",
            f"refusal precision TP/(TP+FP) : {precision}",
            "=" * 78,
            "",
        ]
        report = "\n".join(report_lines)
        print(report)

        # Sanity assertions — mechanism produced a well-formed result, not a
        # claim about real-world refusal accuracy.
        assert n_gated >= tp  # TP is a subset of everything refused
        if precision is not None:
            assert 0.0 <= precision <= 1.0

        captured = capsys.readouterr()
        assert "ILLUSTRATIVE, NOT LEADERBOARD-GRADE" in captured.out
        assert "<= 2 true positives" in captured.out
