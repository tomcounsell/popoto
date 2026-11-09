"""The dated-memory adapter (tests/benchmarks/datasets/dated.py): a
question's history is its project's records from before the day it is
asked, undated records first. Fixture-based, no network, no Redis."""

from pathlib import Path

import pytest

from tests.benchmarks.datasets import ground_truth_unit
from tests.benchmarks.datasets.dated import iter_items

FIXTURE = Path(__file__).parent / "datasets" / "fixtures" / "dated_sample.json"


def test_history_holds_only_the_project_records_before_asked_on():
    items = {i.item_id: i for i in iter_items(fixture_path=FIXTURE)}
    assert [t["turn_id"] for t in items["q1"].history] == ["r3", "r1"]
    assert [t["turn_id"] for t in items["q2"].history] == ["r3", "r1", "r2"]
    assert [t["turn_id"] for t in items["q3"].history] == ["r4", "r5"]
    assert items["q2"].relevant_ids == {"r2"}
    assert items["q3"].metadata["question_type"] == "seasonal-calendar"
    assert items["q1"].history[0]["session_date"] is None


def test_questions_without_gold_are_skipped_and_ground_truth_is_per_record():
    assert "q4" not in {i.item_id for i in iter_items(fixture_path=FIXTURE)}
    assert ground_truth_unit("dated") == "turn"


def test_no_fixture_no_run():
    with pytest.raises(RuntimeError, match="no public copy"):
        iter_items()
