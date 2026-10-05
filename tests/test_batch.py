"""``popoto.batch()``: open a transaction without importing the client (#630).

The return type is load-bearing, not cosmetic: Popoto's field layer routes
writes into a caller's transaction with ``isinstance(pipeline,
redis.client.Pipeline)``, several sites of which fall back to the shared
client when the check fails. A batch object that were not a real pipeline
would execute immediately and silently, so the type assertions below are the
point of this file, not boilerplate.

Backend conformance (#759 M5, plan §5 M5 gate (b)): every test runs once per
configured backend. The batch is the same object on both legs -- a Redis
pipeline that a Postgres-bound model's writes join as one transaction -- so
the type assertions hold on both. Tests that drive raw Redis commands on a
scratch key never reach a model's backend and run on the Redis leg only.
"""

import os
import sys
import uuid

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import redis
from src import popoto
from src.popoto.redis_db import get_REDIS_DB

pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]

RAW_REDIS = (
    "queues raw SET/GET on a scratch key with no model, so it never reaches a "
    "model's backend: a Postgres leg would rerun the same Redis commands"
)


class Gadget(popoto.Model):
    name = popoto.KeyField()
    size = popoto.IntField(null=True)


def test_batch_returns_a_real_redis_pipeline():
    pipe = popoto.batch()
    try:
        assert isinstance(pipe, redis.client.Pipeline)
    finally:
        pipe.reset()


def test_batch_is_transactional_by_default():
    pipe = popoto.batch()
    try:
        assert pipe.transaction is True
    finally:
        pipe.reset()


def test_batch_transaction_false_is_honored():
    pipe = popoto.batch(transaction=False)
    try:
        assert pipe.transaction is False
    finally:
        pipe.reset()


def test_batch_is_bound_to_the_shared_connection():
    pipe = popoto.batch()
    try:
        assert pipe.connection_pool is get_REDIS_DB().connection_pool
    finally:
        pipe.reset()


@pytest.mark.redis_only(reason=RAW_REDIS)
def test_commands_queue_and_apply_only_on_execute():
    key = f"$test:batch:{uuid.uuid4().hex[:12]}"
    pipe = popoto.batch()
    try:
        pipe.set(key, "1")
        assert get_REDIS_DB().get(key) is None  # nothing applied yet
        pipe.execute()
        assert get_REDIS_DB().get(key) == b"1"
    finally:
        get_REDIS_DB().delete(key)


def test_a_model_save_accepts_the_batch():
    """The isinstance gate in the field layer must recognise it."""
    name = f"gadget-{uuid.uuid4().hex[:8]}"
    pipe = popoto.batch()
    try:
        Gadget(name=name, size=7).save(pipeline=pipe)
        assert Gadget.exists(name=name) is False  # queued, not applied
        pipe.execute()
        assert Gadget.exists(name=name) is True
    finally:
        loaded = Gadget.query.get(name=name)
        if loaded is not None:
            loaded.delete()


def test_batch_is_exported_from_the_package():
    assert "batch" in popoto.__all__
    assert popoto.batch is not None


# -- #759 M5: the same batch code on both legs ---------------------------------


def _wipe():
    for gadget in Gadget.query.all():
        gadget.delete()


def test_a_batch_of_saves_lands_together_on_execute():
    _wipe()
    pipe = popoto.batch()
    for i in range(3):
        Gadget(name=f"b{i}", size=i).save(pipeline=pipe)
    assert Gadget.query.count() == 0
    pipe.execute()
    assert sorted(g.name for g in Gadget.query.all()) == ["b0", "b1", "b2"]
    _wipe()


def test_save_returns_the_batch_for_chaining():
    _wipe()
    pipe = popoto.batch()
    assert Gadget(name="c", size=1).save(pipeline=pipe) is pipe
    pipe.execute()
    assert Gadget.query.get(name="c").size == 1
    _wipe()


def test_a_batch_that_is_reset_writes_nothing():
    _wipe()
    pipe = popoto.batch()
    Gadget(name="r", size=1).save(pipeline=pipe)
    pipe.reset()
    assert pipe.execute() == []
    assert Gadget.query.get(name="r") is None


def test_leaving_a_with_block_without_execute_writes_nothing():
    _wipe()
    with popoto.batch() as pipe:
        Gadget(name="w", size=1).save(pipeline=pipe)
    assert Gadget.query.get(name="w") is None


def test_a_delete_in_a_batch_applies_on_execute():
    _wipe()
    Gadget.create(name="d", size=1)
    pipe = popoto.batch()
    Gadget.query.get(name="d").delete(pipeline=pipe)
    assert Gadget.query.get(name="d") is not None
    pipe.execute()
    assert Gadget.query.get(name="d") is None


# -- #759 M5 patch: post-save Redis effects follow the batch -------------------
#
# A save's EventStreamMixin XADD and WriteFilterMixin priority tag are Redis
# writes. On Redis they queue on the batch with the save. A Postgres save that
# joined the batch must send them only when the batch commits, and never after
# a rollback: a stream entry for a row that does not exist, or one a consumer
# reads before the row is visible, is the bug (#783 review, blocker 1).

STREAM = "stream:test_batch_effects"


class StreamGadget(popoto.EventStreamMixin, popoto.Model):
    name = popoto.KeyField()
    code = popoto.UniqueField(type=str)

    _stream_name = "test_batch_effects"


class TaggedGadget(popoto.WriteFilterMixin, popoto.Model):
    name = popoto.KeyField()

    def compute_filter_score(self):
        return 0.9  # above the priority threshold: every save is tagged


def _effects_clean():
    for model in (StreamGadget, TaggedGadget):
        for rec in model.query.all():
            rec.delete()
    # The stream lives on the model's backend (Redis, or the Postgres events
    # tables since #759 M5): clear and read it there, never from Redis alone.
    StreamGadget.stream_client().delete(STREAM)
    get_REDIS_DB().delete(TaggedGadget(name="x")._wf_key("priority"))


def _stream_len():
    return StreamGadget.stream_len()


def _tags():
    return get_REDIS_DB().zcard(TaggedGadget(name="x")._wf_key("priority"))


@pytest.fixture
def effects():
    """Clean stream, tags and rows; and roll back any batch a failing
    assertion left open, so its row locks never outlive the test."""
    opened = []
    _effects_clean()

    def open_batch():
        opened.append(popoto.batch())
        return opened[-1]

    yield open_batch
    for pipe in opened:
        pipe.reset()
    _effects_clean()


def test_a_reset_batch_sends_no_stream_entry(effects):
    pipe = effects()
    StreamGadget(name="s1", code="s1").save(pipeline=pipe)
    assert _stream_len() == 0  # nothing before the commit
    pipe.reset()
    assert _stream_len() == 0
    assert StreamGadget.query.get(name="s1") is None


def test_leaving_a_with_block_sends_no_stream_entry(effects):
    with effects() as pipe:
        StreamGadget(name="s1", code="s1").save(pipeline=pipe)
    assert _stream_len() == 0
    assert StreamGadget.query.get(name="s1") is None


def test_an_executed_batch_sends_one_stream_entry_per_save(effects):
    pipe = effects()
    StreamGadget(name="s1", code="s1").save(pipeline=pipe)
    StreamGadget(name="s2", code="s2").save(pipeline=pipe)
    assert _stream_len() == 0
    pipe.execute()
    assert _stream_len() == 2
    ops = [e[1][b"op"] for e in StreamGadget.stream_range()]
    assert ops == [b"create", b"create"]


def test_a_failed_batch_sends_no_stream_entry_on_postgres(backend, effects):
    pipe = effects()
    StreamGadget(name="a", code="x").save(pipeline=pipe)
    if backend.is_redis:
        # Both saves queue (nothing is committed for pre_save to see); EXEC
        # applies "a" and its XADD and reports the conflict of "b" -- the
        # documented MULTI/EXEC divergence.
        StreamGadget(name="b", code="x").save(pipeline=pipe)
        with pytest.raises(Exception):
            pipe.execute()
        assert StreamGadget.query.get(name="a") is not None
    else:
        # The UNIQUE index refuses "b" inside the transaction, which aborts
        # it: execute() rolls the whole batch back, stream entries included.
        with pytest.raises(popoto.exceptions.ModelException):
            StreamGadget(name="b", code="x").save(pipeline=pipe)
        with pytest.raises(popoto.backends.BackendError):
            pipe.execute()
        assert StreamGadget.query.get(name="a") is None
        assert _stream_len() == 0


def test_a_caught_validation_error_lets_the_rest_of_the_batch_commit(effects):
    # A conflict with a *committed* row is refused before anything is sent,
    # on both legs: the batch stays healthy and execute() commits the rest,
    # stream entry included. Only a statement that fails inside the
    # Postgres transaction aborts the batch (the test above).
    StreamGadget(name="held", code="x").save()
    StreamGadget.stream_client().delete(STREAM)
    assert _stream_len() == 0
    pipe = effects()
    StreamGadget(name="a", code="a").save(pipeline=pipe)
    with pytest.raises(popoto.exceptions.ModelException, match="Unique"):
        StreamGadget(name="b", code="x").save(pipeline=pipe)
    pipe.execute()
    assert StreamGadget.query.get(name="a") is not None
    assert StreamGadget.query.get(name="b") is None
    assert _stream_len() == 1


def test_a_custom_event_joins_the_batch(effects):
    """#787 review blocker 3: ``_xadd_event(pipeline=batch)`` (the path
    ``update_confidence`` and the prediction ledger take) appends at
    ``execute()`` and not at all after ``reset()``, on both legs -- on
    Postgres by joining the batch's transaction, never escaping it."""
    StreamGadget(name="ev", code="ev").save()
    StreamGadget.stream_client().delete(STREAM)
    gadget = StreamGadget.query.get(name="ev")
    pipe = effects()
    gadget._xadd_event("custom", extra_fields={"k": "dropped"}, pipeline=pipe)
    assert _stream_len() == 0
    pipe.reset()
    assert _stream_len() == 0
    pipe = effects()
    gadget._xadd_event("custom", extra_fields={"k": "kept"}, pipeline=pipe)
    assert _stream_len() == 0
    pipe.execute()
    entries = StreamGadget.stream_range()
    assert [(e[1][b"op"], e[1][b"k"]) for e in entries] == [(b"custom", b"kept")]


def test_write_filter_tags_follow_the_batch(backend, effects):
    pipe = effects()
    TaggedGadget(name="t1").save(pipeline=pipe)
    assert _tags() == 0
    pipe.reset()
    assert _tags() == 0
    pipe = effects()
    TaggedGadget(name="t2").save(pipeline=pipe)
    assert _tags() == 0
    pipe.execute()
    # The priority tier is Redis-only (a no-op off Redis, plan §5 M2).
    assert _tags() == (1 if backend.is_redis else 0)
