"""Regression tests for KeyField index stale reads (issue #283).

Verifies that Model.create() and save() produce immediately-queryable
index entries -- filter() must find records right after create/save
returns, with no timing gap.
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto


class Job(popoto.Model):
    chat_id = popoto.KeyField()
    status = popoto.KeyField()
    payload = popoto.Field(null=True)


# Cleanup before tests
for item in Job.query.all():
    item.delete()


def test_create_then_filter_single_keyfield():
    """filter() finds a record immediately after create() by one KeyField."""
    job = Job.create(chat_id="c1", status="pending", payload="data")
    try:
        results = Job.query.filter(status="pending").all()
        keys = [r.chat_id for r in results]
        assert "c1" in keys, f"Expected c1 in {keys}"
    finally:
        job.delete()


def test_create_then_filter_multi_keyfield_intersection():
    """filter() finds a record immediately after create() by multiple KeyFields."""
    job = Job.create(chat_id="c2", status="pending", payload="data")
    try:
        results = Job.query.filter(chat_id="c2", status="pending").all()
        assert len(results) == 1, f"Expected 1 result, got {len(results)}"
        assert results[0].payload == "data"
    finally:
        job.delete()


def test_create_multiple_then_filter():
    """filter() finds all records immediately after multiple create() calls."""
    jobs = []
    for i in range(5):
        jobs.append(Job.create(chat_id=f"batch_{i}", status="queued"))
    try:
        results = Job.query.filter(status="queued").all()
        found_ids = {r.chat_id for r in results}
        expected_ids = {f"batch_{i}" for i in range(5)}
        assert expected_ids <= found_ids, f"Missing: {expected_ids - found_ids}"
    finally:
        for j in jobs:
            j.delete()


def test_save_then_filter():
    """filter() finds a record immediately after save() (no pipeline)."""
    job = Job(chat_id="c3", status="active")
    job.save()
    try:
        results = Job.query.filter(chat_id="c3", status="active").all()
        assert len(results) == 1, f"Expected 1 result, got {len(results)}"
    finally:
        job.delete()


def test_external_pipeline_not_visible_until_execute():
    """Records saved via external pipeline are NOT visible before execute()."""
    pipe = POPOTO_REDIS_DB.pipeline()
    job = Job(chat_id="c4", status="deferred")
    job.save(pipeline=pipe)

    # Before execute: filter should NOT find it
    results = Job.query.filter(chat_id="c4", status="deferred").all()
    assert len(results) == 0, f"Expected 0 before execute, got {len(results)}"

    # After execute: filter should find it
    pipe.execute()
    results = Job.query.filter(chat_id="c4", status="deferred").all()
    assert len(results) == 1, f"Expected 1 after execute, got {len(results)}"

    # Cleanup
    for r in results:
        r.delete()


def test_is_persisted_flag_internal_pipeline():
    """_is_persisted is True after save() with internal pipeline."""
    job = Job(chat_id="c5", status="new")
    assert job._is_persisted is False
    job.save()
    assert job._is_persisted is True
    job.delete()


def test_is_persisted_flag_create():
    """_is_persisted is True after create()."""
    job = Job.create(chat_id="c6", status="new")
    assert job._is_persisted is True
    job.delete()


def test_is_persisted_flag_external_pipeline():
    """_is_persisted remains False when using external pipeline."""
    pipe = POPOTO_REDIS_DB.pipeline()
    job = Job(chat_id="c7", status="new")
    job.save(pipeline=pipe)
    assert job._is_persisted is False
    pipe.execute()
    # Even after external execute, _is_persisted stays False
    # (Popoto can't observe external pipeline execution)
    assert job._is_persisted is False

    # Cleanup
    loaded = Job.query.filter(chat_id="c7").all()
    for r in loaded:
        r.delete()


def test_is_persisted_flag_loaded_from_redis():
    """_is_persisted is True for instances loaded from Redis."""
    job = Job.create(chat_id="c8", status="loaded")
    loaded = Job.query.filter(chat_id="c8").all()
    assert len(loaded) == 1
    assert loaded[0]._is_persisted is True
    job.delete()


if __name__ == "__main__":
    test_create_then_filter_single_keyfield()
    test_create_then_filter_multi_keyfield_intersection()
    test_create_multiple_then_filter()
    test_save_then_filter()
    test_external_pipeline_not_visible_until_execute()
    test_is_persisted_flag_internal_pipeline()
    test_is_persisted_flag_create()
    test_is_persisted_flag_external_pipeline()
    test_is_persisted_flag_loaded_from_redis()
    print("All tests passed!")
