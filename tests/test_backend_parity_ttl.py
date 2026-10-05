"""Record expiry and ``popoto.batch()``: the documented divergences, pinned on
both legs (#759 M5).

Where Redis and Postgres answer differently on purpose, each test asserts
*both* answers from the same code, branching on the leg -- so a change on
either side fails here rather than drifting silently. The table these pin is
"Records and other behaviour" in ``docs/features/postgres-backend.md``. What
the two legs agree on is ``test_meta_ttl.py``, ``test_batch.py`` and the
seeded probe, ``scripts/probe_ttl_parity.py``.
"""

import time

import pytest
import redis

import popoto
from popoto import DecayingSortedField
from popoto.backends import BackendError
from popoto.exceptions import ModelException
from popoto.fields.existence_filter import ExistenceFilter

pytestmark = [pytest.mark.conformance]

#: Seconds a short-lived record lives, and how long a test waits past it.
SHORT = 1
PAST = 1.6


class ParityTtl(popoto.Model):
    group = popoto.KeyField()
    name = popoto.KeyField()
    hits = popoto.IntField(default=0)

    class Meta:
        ttl = 3600


class ParityTtlRanked(popoto.Model):
    name = popoto.KeyField()
    text = popoto.StringField(default="")
    relevance = DecayingSortedField()
    bloom = ExistenceFilter(fingerprint_fn=lambda m: m.text)

    class Meta:
        ttl = 3600


class ParityUnique(popoto.Model):
    name = popoto.KeyField()
    code = popoto.UniqueField(type=str)


def _short(rec):
    rec._ttl = SHORT
    return rec


def _forever(rec):
    rec._ttl = None
    return rec


def test_a_fractional_ttl_is_a_documented_divergence(backend):
    rec = ParityTtl(group="g", name="f")
    rec._ttl = 1.5
    if backend.is_redis:
        # MULTI/EXEC wrote the hash before EXPIRE refused the value.
        with pytest.raises(redis.exceptions.ResponseError, match="not an integer"):
            rec.save()
        assert ParityTtl.query.get(group="g", name="f") is not None
    else:
        with pytest.raises(ModelException, match="not an integer"):
            rec.save()
        assert ParityTtl.query.get(group="g", name="f") is None


def test_count_after_expiry_is_a_documented_divergence(backend):
    _short(ParityTtl(group="g", name="short")).save()
    _forever(ParityTtl(group="g", name="long")).save()
    time.sleep(PAST)
    # Redis counts the class set, which keeps the expired member until a
    # hydrating read purges it; Postgres counts live rows.
    assert ParityTtl.query.count() == (2 if backend.is_redis else 1)
    assert [r.name for r in ParityTtl.query.filter(group="g")] == ["long"]
    assert ParityTtl.query.count() == 1  # purged by the read on Redis


def test_ranking_after_expiry_is_a_documented_divergence(backend):
    _forever(ParityTtlRanked(name="older", text="kept")).save()
    time.sleep(0.05)
    # Saved last, so its decay clock is the newest: it ranks first.
    _short(ParityTtlRanked(name="newer", text="gone")).save()
    time.sleep(PAST)
    top = [r.name for r in ParityTtlRanked.query.top_by_decay(n=1)]
    seen = ParityTtlRanked.bloom.might_exist(ParityTtlRanked, "gone")
    if backend.is_redis:
        # The expired member keeps its slot in the sorted set; hydration then
        # drops it, so n=1 returns nothing. The bloom never forgets.
        assert top == []
        assert seen is True
    else:
        assert top == ["older"]
        assert seen is False


def test_increment_after_expiry_is_a_documented_divergence(backend):
    rec = _short(ParityTtl(group="g", name="i", hits=5))
    rec.save()
    time.sleep(PAST)
    if backend.is_redis:
        # The script HSETs the field onto a fresh key: hits = 0 + 1.
        assert rec.atomic_increment("hits", 1) == 1
    else:
        with pytest.raises(ModelException, match="no longer exists"):
            rec.atomic_increment("hits", 1)


def test_a_failed_batch_is_a_documented_divergence(backend):
    pipe = popoto.batch()
    ParityUnique(name="a", code="x").save(pipeline=pipe)
    if backend.is_redis:
        # Both pass pre_save (nothing is committed yet); EXEC applies "a" and
        # reports b's unique conflict without rolling "a" back.
        ParityUnique(name="b", code="x").save(pipeline=pipe)
        with pytest.raises(Exception):
            pipe.execute()
        assert ParityUnique.query.get(name="a") is not None
    else:
        # The UNIQUE index refuses "b" inside the transaction; execute() then
        # rolls the whole batch back.
        with pytest.raises(ModelException):
            ParityUnique(name="b", code="x").save(pipeline=pipe)
        with pytest.raises(BackendError, match="rolled back"):
            pipe.execute()
        assert ParityUnique.query.get(name="a") is None
