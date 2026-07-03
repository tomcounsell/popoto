"""Tests for CSR corpus planting (requires Redis; DB 15 via pytest plugin)."""

import pytest

from src.popoto.fields.bm25_field import BM25Field
from src.popoto.redis_db import POPOTO_REDIS_DB
from tests.benchmarks.csr.corpus import (
    BM25_FIELD_NAME,
    CsrAuthoringError,
    CsrTestCase,
    PlantedMemory,
    cleanup,
    lint_case,
    lint_suite,
    plant,
)
from tests.benchmarks.csr.satisfaction import InTopK


def _memory(mid, content, topic="alpha", timestamp=100.0, importance=0.5):
    return PlantedMemory(
        id=mid,
        content=content,
        timestamp=timestamp,
        topic=topic,
        importance=importance,
    )


def _case(**overrides):
    base = dict(
        case_id="unit_case",
        planted_corpus=(
            _memory("rel-1", "the falcon telescope observes distant nebula fields"),
            _memory("rel-2", "telescope calibration relies on the falcon guidance"),
            _memory("dis-1", "lunch orders arrive from the noodle shop downtown"),
            _memory("dis-2", "the janitor waters ferns beside the stairwell"),
        ),
        standard_query="falcon telescope nebula",
        adversarial_query="stargazing rig pointed at faraway noodle clouds",
        primitive="unit",
        assertions=(InTopK("rel-1", 5),),
        relevant_ids=frozenset({"rel-1", "rel-2"}),
    )
    base.update(overrides)
    return CsrTestCase(**base)


def _scan_all(pattern):
    found = []
    cursor = 0
    while True:
        cursor, keys = POPOTO_REDIS_DB.scan(cursor, match=pattern, count=200)
        found.extend(keys)
        if cursor == 0:
            break
    return found


class TestLint:
    def test_valid_case_passes(self):
        lint_case(_case())

    def test_empty_assertions_rejected(self):
        with pytest.raises(CsrAuthoringError, match="empty assertion list"):
            lint_case(_case(assertions=()))

    def test_adversarial_token_shared_with_relevant_rejected(self):
        with pytest.raises(CsrAuthoringError, match="shares indexed token"):
            lint_case(_case(adversarial_query="telescope pointed at clouds"))

    def test_adversarial_disjoint_from_whole_corpus_rejected(self):
        with pytest.raises(CsrAuthoringError, match="zero hits|no indexed token"):
            lint_case(_case(adversarial_query="xylophone zephyr quixotic"))

    def test_relevant_id_not_in_corpus_rejected(self):
        with pytest.raises(CsrAuthoringError, match="not in corpus"):
            lint_case(_case(relevant_ids=frozenset({"ghost"})))

    def test_empty_relevant_ids_rejected(self):
        with pytest.raises(CsrAuthoringError, match="empty relevant_ids"):
            lint_case(_case(relevant_ids=frozenset()))

    def test_duplicate_case_ids_rejected(self):
        with pytest.raises(CsrAuthoringError, match="duplicate case_id"):
            lint_suite([_case(), _case()])


class TestPlant:
    def test_reverse_map_covers_every_planted_memory(self):
        case = _case()
        model_class, agent_id, id_to_record, reverse_map = plant(case)
        try:
            assert set(id_to_record) == {m.id for m in case.planted_corpus}
            for mid, record in id_to_record.items():
                assert reverse_map[record.db_key.redis_key] == mid
        finally:
            cleanup(model_class, agent_id)

    def test_key_isolation_between_cases(self):
        """Two plants of the same case get distinct model classes and keys."""
        mc1, ag1, _, rmap1 = plant(_case())
        mc2, ag2, _, rmap2 = plant(_case())
        try:
            assert mc1.__name__ != mc2.__name__
            assert set(rmap1).isdisjoint(set(rmap2))
            # BM25 search on one class never returns the other's keys
            hits = BM25Field.search(mc1, BM25_FIELD_NAME, "falcon telescope", limit=10)
            assert hits and all(mc1.__name__ in key for key, _ in hits)
        finally:
            cleanup(mc1, ag1)
            cleanup(mc2, ag2)

    def test_duplicate_planted_id_raises(self):
        corpus = (
            _memory("dup", "the falcon telescope observes nebula fields"),
            _memory("dup", "another noodle memory entirely"),
        )
        with pytest.raises(CsrAuthoringError, match="duplicate planted ids"):
            plant(_case(planted_corpus=corpus))

    def test_zero_bm25_hit_adversarial_raises_and_cleans_up(self):
        case = _case(adversarial_query="xylophone zephyr quixotic")
        with pytest.raises(CsrAuthoringError, match="zero BM25 hits"):
            plant(case)
        assert _scan_all("*CsrMem*") == []

    def test_cleanup_leaves_db_clean(self):
        model_class, agent_id, _, _ = plant(_case())
        assert _scan_all(f"*{model_class.__name__}*")  # keys exist
        cleanup(model_class, agent_id)
        assert _scan_all(f"*{model_class.__name__}*") == []
        assert _scan_all(f"*{agent_id}*") == []
