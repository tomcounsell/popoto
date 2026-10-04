"""``[PG-only]`` behaviour of the recipe-layer adapters (#759 M4, plan §2 H).

The model-level parity is the conformance suite (gate (b): the recipe test
files run on both legs). This file holds what has no Redis leg: the engine
tables the adapters write, ``idle_seconds``'s clock, a sorted field's
partition reads, the Postgres twins of the conformance tests that plant or
read raw Redis structures (each named in its twin's ``redis_only`` reason),
``NeverRecordMixin`` / ``AppendOnlyMixin`` on Postgres, and a run of the
recipes with the Redis connection layer refusing every command.
"""

import json
import logging
import time

import msgpack
import pytest

import popoto
from popoto import counters
from popoto.backends import BackendCapabilityError, get_backend
from popoto.exceptions import AppendOnlyViolation, NeverRecordException
from popoto.fields.access_tracker import AccessTrackerMixin
from popoto.fields.append_only import AppendOnlyMixin
from popoto.fields.confidence_field import ConfidenceField
from popoto.fields.decaying_sorted_field import DecayingSortedField
from popoto.fields.tombstone_prior import TombstonePriorStore, digest_fingerprint
from popoto.privacy.never_record import NeverRecordMixin
from popoto.recipes import default_memory as dm_module
from popoto.recipes.default_memory import EVICTION_COUNTER_PREFIX, DefaultMemory
from popoto.recipes.memory_lifecycle import MemoryLifecycle

ENV_VAR = "POPOTO_DEFAULT_MEMORY_MAX_RECORDS"


class RecIdle(popoto.Model):
    name = popoto.KeyField()
    note = popoto.StringField(null=True)


class RecTracked(AccessTrackerMixin, popoto.Model):
    name = popoto.KeyField()


class RecSorted(popoto.Model):
    name = popoto.KeyField()
    group = popoto.KeyField()
    rank = popoto.SortedField(type=float, partition_by="group")
    when = popoto.SortedField(type=int)


class RecTier(AccessTrackerMixin, popoto.Model):
    """A lifecycle model whose tier is not part of the key."""

    key = popoto.AutoKeyField()
    tier = popoto.IndexedField(type=str, default="episodic")
    relevance = DecayingSortedField(decay_rate=0.5)
    confidence = ConfidenceField(initial_confidence=0.5)


class RecKeyTier(AccessTrackerMixin, popoto.Model):
    key = popoto.AutoKeyField()
    tier = popoto.KeyField(type=str, default="episodic")
    relevance = DecayingSortedField(decay_rate=0.5)
    confidence = ConfidenceField(initial_confidence=0.5)


class RecPrivate(NeverRecordMixin, popoto.Model):
    name = popoto.KeyField()
    content = popoto.StringField(default="")


class RecLedger(AppendOnlyMixin, popoto.Model):
    entry = popoto.KeyField()
    amount = popoto.FloatField(default=0.0)


class _RedisRecorder:
    """Patches the Redis connection layer: every command (and every attempt to
    check out a connection) is recorded and refused."""

    def __init__(self, monkeypatch):
        import redis.connection

        self.calls = []

        def refuse(conn_self, *args, **kwargs):
            self.calls.append(args[:1])
            raise RuntimeError("a Redis command was issued")

        monkeypatch.setattr(redis.connection.Connection, "send_packed_command", refuse)
        monkeypatch.setattr(redis.connection.Connection, "send_command", refuse)
        monkeypatch.setattr(redis.connection.ConnectionPool, "get_connection", refuse)
        monkeypatch.setattr(
            redis.connection.BlockingConnectionPool, "get_connection", refuse
        )


def _set_confidence(record, confidence, evidence, field="confidence"):
    backend = get_backend(type(record))
    ts = backend._table(type(record)._meta.spec)
    backend._run(
        f'UPDATE {ts.qualified} SET "{field}__conf" = %s, "{field}__n" = %s, '
        f'"{field}__corr" = 0, "{field}__contra" = %s WHERE "_pk" = %s',
        [confidence, evidence, evidence, record.db_key.redis_key],
    )


# -- idle_seconds ----------------------------------------------------------------


def test_idle_seconds_is_whole_seconds_since_the_last_write(pg):
    """``OBJECT IDLETIME`` replies whole seconds; Postgres counts from the
    row's last write (``_updated_at``), and ``None`` means no row."""
    r = RecIdle.create(name="a")
    assert RecIdle.idle_seconds(redis_key=r.db_key.redis_key) == 0.0
    assert RecIdle.idle_seconds(name="a") == 0.0
    assert RecIdle.idle_seconds("RecIdle:nobody") is None
    ts = pg._table(RecIdle._meta.spec)
    pg._run(
        f"UPDATE {ts.qualified} SET \"_updated_at\" = now() - interval '90.6 seconds'"
    )
    assert RecIdle.idle_seconds(name="a") == 90.0
    r.note = "touched"
    r.save()
    assert RecIdle.idle_seconds(name="a") == 0.0


def test_idle_seconds_counts_a_confirmed_read(pg):
    """With ``AccessTrackerMixin`` the later of the last write and the last
    confirmed read: a read the tracker confirmed resets the clock, as a read
    resets ``OBJECT IDLETIME`` (an unconfirmed read does not -- the
    documented divergence)."""
    r = RecTracked.create(name="t")
    ts = pg._table(RecTracked._meta.spec)
    pg._run(f"UPDATE {ts.qualified} SET \"_updated_at\" = now() - interval '1 hour'")
    assert RecTracked.idle_seconds(name="t") == 3600.0
    loaded = RecTracked.query.get(name="t")  # staged, not confirmed
    assert RecTracked.idle_seconds(name="t") == 3600.0
    loaded.confirm_access()
    assert RecTracked.idle_seconds(name="t") == 0.0


# -- a sorted field's partition reads ----------------------------------------------


def test_sorted_partition_reads_follow_zrange(pg):
    """``count`` / ``members`` / ``score``: the partition's rows, in
    ``(value, _pk)`` order, with ``ZRANGE``'s inclusive and negative ranks."""
    field = RecSorted._meta.fields["rank"]
    for name, group, rank in [
        ("b", "g", 2.0),
        ("a", "g", 2.0),
        ("c", "g", 1.0),
        ("e", "h", 0.5),
    ]:
        RecSorted.create(name=name, group=group, rank=rank, when=1)
    probe = RecSorted(name="x", group="g")
    assert field.count(probe, "rank") == 3
    assert field.members(probe, "rank") == [
        "RecSorted:g:c",
        "RecSorted:g:a",
        "RecSorted:g:b",
    ]
    assert field.members(probe, "rank", 0, 0) == ["RecSorted:g:c"]
    assert field.members(probe, "rank", -2, -1) == ["RecSorted:g:a", "RecSorted:g:b"]
    assert field.members(probe, "rank", 0, 1, reverse=True) == [
        "RecSorted:g:b",
        "RecSorted:g:a",
    ]
    assert field.members(probe, "rank", 2, 1) == []
    a = RecSorted.query.get(name="a", group="g")
    assert field.score(a, "rank") == 2.0
    assert field.score(a, "rank", partitioned=False) is None
    e = RecSorted.query.get(name="e", group="h")
    e.group = "g"  # the partition the instance now names holds no such member
    assert field.score(e, "rank") is None
    when = RecSorted._meta.fields["when"]
    assert when.count(probe, "when") == 4
    assert when.score(a, "when", partitioned=False) == 1.0


# -- counters and DefaultMemory's eviction ------------------------------------------


def test_counters_live_in_the_backend(pg, monkeypatch):
    recorder = _RedisRecorder(monkeypatch)
    assert counters.read("c:x", model=RecIdle) == 0
    assert counters.increment("c:x", 3, model=RecIdle) == 3
    assert counters.increment("c:x", model=RecIdle) == 4
    assert counters.read("c:x", model=RecIdle) == 4
    assert recorder.calls == []


@pytest.fixture
def evict_env(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    warned = set(dm_module._EVICTION_WARNED)
    dm_module._EVICTION_WARNED.clear()
    yield
    dm_module._EVICTION_WARNED.clear()
    dm_module._EVICTION_WARNED.update(warned)


def _seed(agent, n):
    out = []
    for i in range(n):
        m = DefaultMemory(agent_id=agent, content=f"memory {i}", importance=1.0)
        m.save()
        out.append(m)
    return out


def _evicted(agent):
    return counters.read(
        f"{EVICTION_COUNTER_PREFIX}:{agent}:evicted", model=DefaultMemory
    )


def _count(agent):
    return DefaultMemory.query.filter(agent_id=agent).count()


def test_the_eviction_counter_lives_in_the_backend(pg, evict_env, monkeypatch):
    """``test_default_memory_eviction.py``'s counter tests on Postgres: the
    count reaches the cap and the counter (a ``popoto_counter`` row) reports
    every record selected, with no Redis command."""
    recorder = _RedisRecorder(monkeypatch)
    monkeypatch.setenv(ENV_VAR, "5")
    _seed("ev1", 8)
    assert _count("ev1") == 5
    assert _evicted("ev1") == 3
    monkeypatch.setenv(ENV_VAR, "0")
    _seed("ev2", 6)
    monkeypatch.setenv(ENV_VAR, "2")
    DefaultMemory(agent_id="ev2", content="trigger", importance=1.0).save()
    assert _count("ev2") == 2
    assert _evicted("ev2") == 5
    assert recorder.calls == []


def test_a_disabled_cap_reads_no_partition_count(pg, evict_env, monkeypatch):
    """The disable path returns before the partition count (the Redis test's
    ``ZCARD`` spy, as a spy on the backend's ``count`` read)."""
    monkeypatch.setenv(ENV_VAR, "off")
    backend = get_backend(DefaultMemory)
    seen = []
    real = backend.field_call

    def spy(spec, field, op, *args, **kwargs):
        seen.append(op)
        return real(spec, field, op, *args, **kwargs)

    monkeypatch.setattr(backend, "field_call", spy)
    _seed("ev3", 3)
    assert "count" not in seen
    assert _count("ev3") == 3


def test_the_eviction_notice_survives_a_failing_members_read(
    pg, evict_env, monkeypatch, caplog
):
    monkeypatch.setenv(ENV_VAR, "0")
    _seed("ev4", 3)
    monkeypatch.setenv(ENV_VAR, "1")
    caplog.set_level(logging.WARNING, logger="POPOTO.DefaultMemory")
    backend = get_backend(DefaultMemory)
    real = backend.field_call

    def boom(spec, field, op, *args, **kwargs):
        if op == "members":
            raise RuntimeError("members exploded")
        return real(spec, field, op, *args, **kwargs)

    monkeypatch.setattr(backend, "field_call", boom)
    DefaultMemory(agent_id="ev4", content="last", importance=1.0).save()
    assert any("cap exceeded" in r.getMessage() for r in caplog.records)
    assert _evicted("ev4") == 3
    assert _count("ev4") == 4


def test_the_eviction_skips_the_saving_record(pg, evict_env, monkeypatch):
    """The own-key branch: the members read is rotated so the saving record
    heads the eviction window; it is skipped, so fewer are deleted than the
    counter reports."""
    monkeypatch.setenv(ENV_VAR, "0")
    _seed("ev5", 3)
    backend = get_backend(DefaultMemory)
    real = backend.field_call

    def rotated(spec, field, op, *args, **kwargs):
        if op == "members":
            partition, start, stop, reverse = args
            members = real(spec, field, op, partition, 0, -1, reverse)
            members = [members[-1]] + members[:-1]
            return members[start : stop + 1]
        return real(spec, field, op, *args, **kwargs)

    monkeypatch.setattr(backend, "field_call", rotated)
    monkeypatch.setenv(ENV_VAR, "1")
    DefaultMemory(agent_id="ev5", content="trigger", importance=1.0).save()
    assert _evicted("ev5") == 3
    assert 4 - _count("ev5") == 2


def test_the_eviction_counter_survives_an_aborted_loop(pg, evict_env, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "0")
    _seed("ev6", 4)
    monkeypatch.setenv(ENV_VAR, "1")
    backend = get_backend(DefaultMemory)
    real = backend.load
    state = {"calls": 0}

    def flaky(spec, ids, **kwargs):
        state["calls"] += 1
        if state["calls"] > 1:
            raise RuntimeError("load exploded mid-loop")
        return real(spec, ids, **kwargs)

    monkeypatch.setattr(backend, "load", flaky)
    DefaultMemory(agent_id="ev6", content="trigger", importance=1.0).save()
    monkeypatch.setattr(backend, "load", real)
    assert _evicted("ev6") == 4
    assert 5 - _count("ev6") < _evicted("ev6")


# -- MemoryLifecycle --------------------------------------------------------------------


def _ready(model, **kwargs):
    lifecycle = MemoryLifecycle(
        model_class=model, importance_field="relevance", **kwargs
    )
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0
    lifecycle.FORGET_IDLE_SECONDS = -1.0
    return lifecycle


def test_lifecycle_promotes_a_non_key_tier(pg):
    """The twin of the promotion tests: a tier that is not part of the key
    promotes on Postgres with a plain save (a ``KeyField`` tier would be a
    key migration, which Postgres refuses in v2)."""
    lifecycle = MemoryLifecycle(model_class=RecTier, importance_field="relevance")
    lifecycle.PROMOTION_ACCESS_COUNT = 1
    lifecycle.PROMOTION_CONFIDENCE_THRESHOLD = 0.0
    lifecycle.PROMOTION_MIN_AGE_SECONDS = 0.0
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0
    for _ in range(20):
        r = RecTier(tier="episodic")
        r.save()
        r.on_read()
        r.confirm_access()
    summary = lifecycle.tick()
    assert summary["promoted"] == 20
    assert RecTier.query.filter(tier="semantic").count() == 20
    assert lifecycle.tick()["promoted"] == 0
    fresh = RecTier(tier="episodic")
    fresh.save()
    lifecycle.tag_new(fresh, tier="semantic")
    assert RecTier.query.get(key=fresh.key).tier == "semantic"


def test_a_key_tier_promotion_is_refused_on_postgres(pg, caplog):
    """The documented divergence the redis_only promotion tests name:
    ``tick()`` logs the refused key migration and skips the record; a direct
    ``tag_new`` raises it."""
    lifecycle = MemoryLifecycle(model_class=RecKeyTier, importance_field="relevance")
    lifecycle.PROMOTION_ACCESS_COUNT = 0
    lifecycle.PROMOTION_CONFIDENCE_THRESHOLD = 0.0
    lifecycle.PROMOTION_MIN_AGE_SECONDS = 0.0
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0
    r = RecKeyTier(tier="episodic")
    r.save()
    caplog.set_level(logging.WARNING, logger="POPOTO.MemoryLifecycle")
    assert lifecycle.tick()["promoted"] == 0
    assert any("migrate_key" in rec.getMessage() for rec in caplog.records)
    with pytest.raises(BackendCapabilityError, match="migrate_key"):
        lifecycle.tag_new(r, tier="semantic")


def test_the_forget_guard_skips_a_vanished_row(pg):
    r = RecTier(tier="episodic")
    r.save()
    ts = pg._table(RecTier._meta.spec)

    def delete_then_forget(rec, lifecycle):
        pg._run(f'DELETE FROM {ts.qualified} WHERE "_pk" = %s', [rec._redis_key])
        return True

    lifecycle = MemoryLifecycle(
        model_class=RecTier,
        importance_field="relevance",
        should_forget=delete_then_forget,
    )
    assert lifecycle.tick()["forgotten"] == 0
    assert RecTier.query.get(key=r.key) is None


def test_tombstones_round_trip_on_postgres(pg, monkeypatch):
    """Forget, list, restore and retain, with no Redis command: the archive
    is ``popoto_tombstone``, the payload the record encoded as a Redis save
    would write it, so ``restore()`` decodes it unchanged."""
    recorder = _RedisRecorder(monkeypatch)
    lifecycle = _ready(RecTier)
    keep = RecTier(tier="episodic")
    keep.save()
    _set_confidence(keep, 0.95, 10)
    doomed = RecTier(tier="episodic")
    doomed.save()
    _set_confidence(doomed, 0.05, 10)
    summary = lifecycle.tick()
    assert summary["tombstoned"] == 1
    assert RecTier.query.get(key=doomed.key) is None
    (tomb,) = lifecycle.list_tombstones()
    assert tomb.redis_key == doomed._redis_key
    assert tomb.confidence_at_death == pytest.approx(0.05)
    assert tomb.evidence_count == 10
    restored = lifecycle.restore(tomb)
    assert restored.key == doomed.key and restored.tier == "episodic"
    assert RecTier.query.get(key=doomed.key) is not None
    assert lifecycle.tombstone_count() == 0
    assert recorder.calls == []


def test_tombstone_retention_is_bounded_on_postgres(pg):
    lifecycle = _ready(RecTier)
    lifecycle.TOMBSTONE_RETENTION_LIMIT = 2
    records = []
    for _ in range(4):
        r = RecTier(tier="episodic")
        r.save()
        _set_confidence(r, 0.05, 10)
        records.append(r)
        lifecycle.tombstone(r)
        time.sleep(0.002)
    assert lifecycle.tombstone_count() == 2
    kept = {t.redis_key for t in lifecycle.list_tombstones()}
    assert kept == {records[2]._redis_key, records[3]._redis_key}
    assert lifecycle.purge_all_tombstones() == 2


def test_a_partial_tombstone_entry_is_dropped(pg, caplog):
    lifecycle = _ready(RecTier)
    good = RecTier(tier="episodic")
    good.save()
    _set_confidence(good, 0.05, 10)
    lifecycle.tick()
    lifecycle._tombstones.archive(
        "RecTier:partial",
        msgpack.packb({"redis_key": "RecTier:partial", "reason": "policy"}),
        1.0,
    )
    assert lifecycle.tombstone_count() == 2
    with caplog.at_level(logging.WARNING, logger="POPOTO.MemoryLifecycle"):
        tombstones = lifecycle.list_tombstones()
    assert [t.redis_key for t in tombstones] == [good._redis_key]
    assert lifecycle.get_tombstone("RecTier:partial") is None


def test_the_negative_prior_lives_in_the_backend(pg, monkeypatch):
    recorder = _RedisRecorder(monkeypatch)
    store = TombstonePriorStore(RecTier)
    monkeypatch.setattr(
        "popoto.fields.tombstone_prior.Defaults.TOMBSTONE_PRIOR_LIMIT", 2
    )
    assert store.burial_count("Same Content") == 0
    assert store.record_burial("same content", 1.0)
    assert store.record_burial("  SAME content ", 2.0)
    assert store.burial_count("same content") == 2
    assert store.record_burial("other", 3.0)
    assert store.record_burial("third", 4.0)
    assert store.count() == 2  # the oldest digest aged out
    assert store.burial_count("same content") == 0
    store.note_penalty(1.0, 0.25)
    store.note_penalty(0.5, 0.25)
    assert store.stats() == {"penalized": 2, "drawdown_total": 1.0}
    assert store.purge_all() == 2
    assert store.stats() == {"penalized": 0, "drawdown_total": 0.0}
    assert digest_fingerprint("") is None
    assert recorder.calls == []


# -- NeverRecordMixin --------------------------------------------------------------------

SECRET = "my key is sk-ant-api03-" + "A" * 40


def test_the_firewall_refuses_before_any_write(pg, monkeypatch):
    """The privacy firewall on Postgres: the save is refused before the
    backend is asked to write anything, no row exists, the audit log is the
    backend's (content-free), and Redis is never touched."""
    recorder = _RedisRecorder(monkeypatch)
    backend = get_backend(RecPrivate)
    writes = []
    real = backend.save

    def spy(obj, **kwargs):
        writes.append(obj)
        return real(obj, **kwargs)

    monkeypatch.setattr(backend, "save", spy)
    refused = RecPrivate(name="a", content=SECRET)
    assert refused.save() is False
    assert refused._never_record_verdict is not None
    with pytest.raises(NeverRecordException):
        RecPrivate(name="b", content=SECRET)._check_never_record()
    assert writes == []
    assert RecPrivate.query.get(name="a") is None
    counts = RecPrivate.never_record_counts()
    assert sum(counts.values()) >= 1
    log = RecPrivate.never_record_log()
    assert log and all(set(e) == {"id", "reason", "detector", "at"} for e in log)
    assert "sk-ant" not in json.dumps(log) and "sk-ant" not in json.dumps(counts)
    RecPrivate(name="c", content="nothing secret here").save()
    assert len(writes) == 1
    assert recorder.calls == []


def test_the_firewall_log_is_capped(pg, monkeypatch):
    monkeypatch.setattr("popoto.privacy.never_record.Defaults.NR_TOMBSTONE_LOG_MAX", 3)
    for i in range(5):
        RecPrivate(name=f"n{i}", content=SECRET).save()
    log = RecPrivate.never_record_log()
    assert len(log) == 3
    assert len(RecPrivate.never_record_log(limit=2)) == 2
    assert sum(RecPrivate.never_record_counts().values()) == 5


# -- AppendOnlyMixin ------------------------------------------------------------------------


def test_append_only_on_postgres(pg):
    e = RecLedger(entry="e1", amount=1.0)
    e.save()
    e.amount = 2.0
    with pytest.raises(AppendOnlyViolation):
        e.save()
    with pytest.raises(AppendOnlyViolation):
        RecLedger(entry="e1", amount=3.0).save()
    with pytest.raises(AppendOnlyViolation):
        e.delete()
    with pytest.raises(AppendOnlyViolation):
        e.save(migrate_key=True)
    assert RecLedger.query.get(entry="e1").amount == 1.0
    assert RecLedger.hard_delete(e) is True
    assert RecLedger.query.get(entry="e1") is None


def test_append_only_sees_its_own_transaction(pg):
    """Redis's intra-pipeline shape (two saves of one key queued on one
    pipeline both pass the guard) is closed on Postgres: the guard reads
    inside the caller's transaction, so the second save is refused."""
    with pytest.raises(AppendOnlyViolation):
        with pg.transaction() as uow:
            RecLedger(entry="tx", amount=1.0).save(pipeline=uow)
            RecLedger(entry="tx", amount=2.0).save(pipeline=uow)
    assert RecLedger.query.get(entry="tx") is None


def test_unlisted_field_call_raises_capability_error(pg):
    with pytest.raises(BackendCapabilityError, match="no adapter"):
        pg.field_call(RecIdle._meta.spec, "note", "frobnicate")


def test_the_auditable_extraction_path_is_refused_on_postgres(pg):
    """The decision log is a Redis-only extraction surface (plan §1), so a
    Postgres-bound ``SubconsciousMemory`` refuses ``auditable_extraction``
    at construction rather than split its audit trail across two stores;
    the default path runs."""
    from popoto.extraction.decision_log import AuditableExtractionConfig
    from popoto.recipes.subconscious_memory import SubconsciousMemory

    with pytest.raises(BackendCapabilityError, match="decision log"):
        SubconsciousMemory(
            agent_id="audit",
            auditable_extraction=AuditableExtractionConfig(journal=object()),
        )
    memory = SubconsciousMemory(agent_id="plain")
    assert memory.decision_log is None
    saved = memory.extract_memories("Alice deployed the service on Tuesday.")
    assert saved and all(m.agent_id == "plain" for m in saved)
