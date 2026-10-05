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
