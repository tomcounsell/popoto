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
from popoto import ConfidenceField, DecayingSortedField
from popoto.backends import BackendCapabilityError, BackendError, SchemaDriftError
from popoto.exceptions import ModelException
from popoto.fields.co_occurrence_field import CoOccurrenceField
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


class ParityTtlConfidence(popoto.Model):
    name = popoto.KeyField()
    confidence = ConfidenceField()

    class Meta:
        ttl = 3600


class ParityNoTtl(popoto.Model):
    name = popoto.KeyField()


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


def test_an_instance_ttl_without_meta_ttl_is_a_documented_divergence(backend):
    rec = ParityNoTtl(name="n")
    rec._ttl = 60
    if backend.is_redis:
        rec.save()
        assert 0 < popoto.get_redis().ttl(rec.db_key.redis_key) <= 60
    else:
        # Only a Meta.ttl model has _expires_at and the read filter.
        with pytest.raises(BackendCapabilityError, match="Meta.ttl"):
            rec.save()
        assert ParityNoTtl.query.get(name="n") is None


def test_state_keyed_by_an_expired_record_is_a_documented_divergence(backend):
    rec = _short(ParityTtlConfidence(name="c"))
    rec.save()
    assert ConfidenceField.update_confidence(rec, "confidence", 1.0) == 0.75
    time.sleep(PAST)
    # Both legs refuse a signal to a record that expired.
    with pytest.raises(TypeError, match="saved model instance"):
        ConfidenceField.update_confidence(rec, "confidence", 1.0)
    # Redis keeps the companion entry until something cleans it; on Postgres
    # it went with the row, so the read is the seed.
    held = ConfidenceField.get_confidence(rec, "confidence")
    assert held == (0.75 if backend.is_redis else 0.5)


def test_a_save_over_an_expired_key_is_a_documented_divergence(backend):
    rec = _short(ParityTtlConfidence(name="s"))
    rec.save()
    ConfidenceField.update_confidence(rec, "confidence", 1.0)
    time.sleep(PAST)
    again = _forever(ParityTtlConfidence(name="s"))
    again.save()
    # HSET makes a new hash that inherits the old companion entry; Postgres
    # deleted the expired row's state first, so the new record is fresh.
    held = ConfidenceField.get_confidence(again, "confidence")
    assert held == (0.75 if backend.is_redis else 0.5)


def test_a_batch_of_raw_commands_and_model_writes_is_a_documented_divergence(
    backend,
):
    key = "$test:parity:batch_mixed"
    pipe = popoto.batch()
    try:
        pipe.set(key, "1")
        if backend.is_redis:
            # One store: the raw command and the save share one MULTI/EXEC.
            ParityTtl(group="g", name="mixed").save(pipeline=pipe)
            pipe.execute()
            assert ParityTtl.query.get(group="g", name="mixed") is not None
            assert popoto.get_redis().get(key) == b"1"
        else:
            with pytest.raises(BackendCapabilityError, match="cannot mix"):
                ParityTtl(group="g", name="mixed").save(pipeline=pipe)
            assert ParityTtl.query.get(group="g", name="mixed") is None
    finally:
        pipe.reset()
        popoto.get_redis().delete(key)


def test_removing_meta_ttl_is_a_documented_divergence(backend):
    def declare(module, ttl):
        attrs = {"__module__": module, "name": popoto.KeyField()}
        if ttl:
            attrs["Meta"] = type("Meta", (), {"ttl": ttl})
        return type("ParityDroppedTtl", (popoto.Model,), attrs)

    first = declare("parity.first", 3600)
    first(name="d").save()
    second = declare("parity.second", None)
    rec = second(name="d")
    if backend.is_redis:
        # The next save simply stops issuing EXPIRE; HSET keeps the TTL.
        rec.save()
        assert popoto.get_redis().ttl(rec.db_key.redis_key) > 0
    else:
        # The table has an _expires_at column the model no longer declares.
        with pytest.raises(SchemaDriftError, match="_expires_at"):
            rec.save()
    for obj in first.query.all():
        obj.delete()


class ParityEdges(popoto.Model):
    name = popoto.UniqueKeyField()
    near = CoOccurrenceField()
    follows = CoOccurrenceField(symmetric=False)

    class Meta:
        ttl = 3600


def _edge_keys(field, key):
    return sorted(k for k, _w in field.get_linked(ParityEdges, key))


def _assert_edges_survive(want):
    """check/clean/rebuild keep every edge and report no drift."""
    assert ParityEdges.check_indexes()["total"] == 0
    assert ParityEdges.clean_indexes() == 0
    ParityEdges.rebuild_indexes()
    assert ParityEdges.check_indexes()["total"] == 0
    for field, key, keys in want:
        assert _edge_keys(field, key) == keys


def test_edges_to_never_saved_endpoints_survive_maintenance(backend):
    """Redis keeps an edge whose record does not exist and counts nothing;
    Postgres does the same (#788 review). Only Postgres also *reports* it,
    outside ``total``."""
    near = ParityEdges._meta.fields["near"]
    follows = ParityEdges._meta.fields["follows"]
    a = ParityEdges.create(name="a")
    ka = a.db_key.redis_key
    near.link(ParityEdges, ka, "ParityEdges:ghost", 0.5)
    follows.link(ParityEdges, "ParityEdges:ghost2", ka, 0.5)
    _assert_edges_survive(
        [
            (near, ka, ["ParityEdges:ghost"]),
            (follows, "ParityEdges:ghost2", [ka]),
        ]
    )
    if not backend.is_redis:
        dangling = ParityEdges.check_indexes()["side_tables"]["graph_edges"]
        assert dangling == {"dangling": 3}


def test_a_ttl_reaped_records_edges_survive_maintenance(backend):
    near = ParityEdges._meta.fields["near"]
    a = ParityEdges.create(name="a")
    b = _short(ParityEdges(name="b"))
    b.save()
    ka, kb = a.db_key.redis_key, b.db_key.redis_key
    near.link(ParityEdges, ka, kb, 0.5)
    time.sleep(PAST)
    if not backend.is_redis:
        from popoto.backends import get_backend
        from popoto.backends.postgres import ttl as ttl_mod

        pg = get_backend(ParityEdges)
        assert ttl_mod.reap(pg, pg._table(ParityEdges._meta.spec), force=True) == [kb]
    else:
        # Redis leaves the expired hash's own index entries behind (class
        # set, key field); that is not the edges' drift. Clear it first.
        ParityEdges.clean_indexes()
    _assert_edges_survive([(near, ka, [kb])])
