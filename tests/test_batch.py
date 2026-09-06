"""``popoto.batch()``: open a transaction without importing the client (#630).

The return type is load-bearing, not cosmetic: Popoto's field layer routes
writes into a caller's transaction with ``isinstance(pipeline,
redis.client.Pipeline)``, several sites of which fall back to the shared
client when the check fails. A batch object that were not a real pipeline
would execute immediately and silently, so the type assertions below are the
point of this file, not boilerplate.
"""

import os
import sys
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import redis
from src import popoto
from src.popoto.redis_db import POPOTO_REDIS_DB


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
        assert pipe.connection_pool is POPOTO_REDIS_DB.connection_pool
    finally:
        pipe.reset()


def test_commands_queue_and_apply_only_on_execute():
    key = f"$test:batch:{uuid.uuid4().hex[:12]}"
    pipe = popoto.batch()
    try:
        pipe.set(key, "1")
        assert POPOTO_REDIS_DB.get(key) is None  # nothing applied yet
        pipe.execute()
        assert POPOTO_REDIS_DB.get(key) == b"1"
    finally:
        POPOTO_REDIS_DB.delete(key)


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
