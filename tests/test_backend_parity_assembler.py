"""``ContextAssembler`` on both conformance legs (#759 M2c).

The assembler's own test files assert part of their behaviour through models
Postgres refuses until a later milestone (a ``CyclicDecayField``, a
``CoOccurrenceField``, a ``ValidityField``) or by planting Redis structures
through the raw client (a backdated ``ZADD``, a confidence ``HSET``, a deleted
``$BM25:`` key), so those tests carry ``redis_only``. This file re-states the
same behaviours with models both backends store and with **leg-neutral
helpers** -- on Redis they do exactly what those tests do, on Postgres the
equivalent column or table write -- so every assertion here runs on both
legs from the same code:

* the ExistenceFilter short-circuit and the feeling-of-knowing components;
* the metacognitive score proxy (score spread, staleness, sub-threshold
  activation) against ``top_by_decay``, with and without confidence
  modulation;
* the confidence gate on the pull path, in both modes;
* hybrid dispatch, the vector arm and the cold-index fallback;
* the post-effects: one staged read per selected pull record and one
  competitive-suppression signal per unselected candidate.

The same-keys-on-both-legs exit criterion and the zero-Redis-commands proof
need both servers in one test, so they live in
``tests/postgres/test_postgres_assembler.py``.
"""

import logging
import math
import time
from unittest.mock import patch

import msgpack
import pytest

np = pytest.importorskip("numpy")

import popoto  # noqa: E402
from popoto import AccessTrackerMixin, ConfidenceField  # noqa: E402
from popoto.backends import get_backend  # noqa: E402
from popoto.embeddings import AbstractEmbeddingProvider  # noqa: E402
from popoto.fields.bm25_field import BM25Field  # noqa: E402
from popoto.fields.decaying_sorted_field import DecayingSortedField  # noqa: E402
from popoto.fields.embedding_field import EmbeddingField  # noqa: E402
from popoto.fields.existence_filter import ExistenceFilter  # noqa: E402
from popoto.models.query import QueryBuilder  # noqa: E402
from popoto.recipes.context_assembler import (  # noqa: E402
    COMPETITIVE_SUPPRESSION_SIGNAL,
    ContextAssembler,
    RetrievalQuality,
    _compute_score_spread,
    _score_proxy_for_records,
    _staleness_ratio,
)

pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]

DAY = 86400.0


class HashProvider(AbstractEmbeddingProvider):
    """Deterministic 8-d vectors from the text."""

    def embed(self, texts, input_type=None):
        return [
            np.random.RandomState(sum(map(ord, t)) % (2**31)).randn(8).tolist()
            for t in texts
        ]

    @property
    def dimensions(self):
        return 8

    @property
    def max_batch_size(self):
        return 32


PROVIDER = HashProvider()


# -- models (test_context_assembler.py's, minus the fields Postgres refuses) ---


class PAFixture(popoto.Model):
    """``FixtureMemory``: decay, confidence and a topic ExistenceFilter --
    ``FullMemory`` without its CyclicDecayField and CoOccurrenceField."""

    memory_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    topic = popoto.Field(type=str)
    content = popoto.Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")
    confidence = ConfidenceField(initial_confidence=0.5)
    bloom = ExistenceFilter(
        error_rate=0.01, capacity=10_000, fingerprint_fn=lambda inst: inst.topic
    )


class PASimple(popoto.Model):
    memory_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    content = popoto.Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")


class PADecay(popoto.Model):
    """``DecayPartitionedMemory``."""

    memory_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    content = popoto.Field(type=str)
    strength = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(
        partition_by="agent_id", base_score_field="strength", decay_rate=0.5
    )
    bloom = ExistenceFilter(
        error_rate=0.01, capacity=10_000, fingerprint_fn=lambda inst: inst.content
    )


class PADecayConf(popoto.Model):
    """``DecayConfPartitionedMemory``."""

    memory_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    content = popoto.Field(type=str)
    strength = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(
        partition_by="agent_id", base_score_field="strength", decay_rate=0.5
    )
    certainty = ConfidenceField()


class PAGate(popoto.Model):
    """``GateMemory`` without the CyclicDecayField (the push path)."""

    memory_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    topic = popoto.Field(type=str)
    content = popoto.Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")
    confidence = ConfidenceField(initial_confidence=0.5)


class PAHybrid(popoto.Model):
    """``HybridMemory`` without the CoOccurrenceField."""

    memory_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    content = popoto.Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")
    content_index = BM25Field(source="content")
    embedding = EmbeddingField(source="content", provider=PROVIDER)


class PALexical(popoto.Model):
    """``RegressionMemory``: BM25 only, so ``auto`` resolves to lexical."""

    content = popoto.Field()
    content_bm25 = BM25Field(source="content")
    topic = popoto.Field()


class PATracked(AccessTrackerMixin, popoto.Model):
    """Read tracking plus a confidence, for the post-effects."""

    name = popoto.UniqueKeyField()
    agent_id = popoto.KeyField()
    content = popoto.Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")
    certainty = ConfidenceField()


MODELS = [
    PAFixture,
    PASimple,
    PADecay,
    PADecayConf,
    PAGate,
    PAHybrid,
    PALexical,
    PATracked,
]


@pytest.fixture(autouse=True)
def _clean(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("POPOTO_CONTENT_PATH", str(tmp_path / "content"))
    from popoto.fields.embedding_field import invalidate_cache

    def wipe():
        invalidate_cache()
        for model in MODELS:
            model.delete_all()
            for field in model._meta.fields.values():
                cache = getattr(field, "_confidence_modulation_cache", None)
                if cache is not None:
                    cache.clear()
        if backend.is_redis:
            client = popoto.get_redis()
            for pattern in ("*PA*", "$AT:PA*", "$RP:PA*"):
                for key in client.scan_iter(pattern):
                    client.delete(key)

    wipe()
    yield
    wipe()


# -- leg-neutral helpers (as tests/test_backend_parity_memory.py's) -----------


def _pg_run(backend, model, sql, params):
    pg = backend.instance
    table = pg._table(model._meta.spec)
    return pg._run(sql.format(table=table.qualified), params)


def backdate(backend, record, ts, field_name="relevance"):
    """Set the record's decay clock to ``ts`` (epoch seconds)."""
    if backend.is_redis:
        field = record._meta.fields[field_name]
        zkey = field.get_partitioned_sortedset_db_key(record, field_name).redis_key
        popoto.get_redis().zadd(zkey, {record.db_key.redis_key: ts})
    else:
        _pg_run(
            backend,
            type(record),
            f'UPDATE {{table}} SET "{field_name}" = %s WHERE "_pk" = %s',
            [ts, record.db_key.redis_key],
        )


def plant_confidence(backend, record, confidence, field_name="certainty"):
    """Write confidence state directly (ten corroborations), bypassing the
    update rule, as ``_set_confidence`` does on Redis."""
    if backend.is_redis:
        field = record._meta.fields[field_name]
        popoto.get_redis().hset(
            field.get_data_hash_key(record, field_name),
            record.db_key.redis_key,
            msgpack.packb(
                {
                    "confidence": confidence,
                    "evidence_count": 10,
                    "corroborations": 10,
                    "contradictions": 0,
                },
                use_bin_type=True,
            ),
        )
    else:
        _pg_run(
            backend,
            type(record),
            f'UPDATE {{table}} SET "{field_name}__conf" = %s, '
            f'"{field_name}__n" = 10, "{field_name}__corr" = 10, '
            f'"{field_name}__contra" = 0 WHERE "_pk" = %s',
            [confidence, record.db_key.redis_key],
        )


def clear_bm25_index(backend, model, field_name):
    """Empty a BM25Field's index: the ``$BM25:`` keys on Redis, the postings
    and document-length tables on Postgres."""
    if backend.is_redis:
        client = popoto.get_redis()
        keys = client.keys(f"$BM25:{model.__name__}:{field_name}:*")
        if keys:
            client.delete(*keys)
    else:
        pg = backend.instance
        _ts, bm = pg._bm25(model._meta.spec, field_name)
        pg._run(f"DELETE FROM {bm.post}", [], write=True)
        pg._run(f"DELETE FROM {bm.dl}", [], write=True)


def staged_reads(backend, record):
    if backend.is_redis:
        return popoto.get_redis().llen(record._at_key("staged"))
    return record._access_state(get_backend(type(record)))[2]


def _isclose(a, b):
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def _save(model, **kwargs):
    instance = model(**kwargs)
    instance.save()
    return instance


# -- ExistenceFilter and feeling-of-knowing ------------------------------------


def test_bloom_filter_skips_the_pull_when_every_cue_is_missing(backend):
    assembler = ContextAssembler(PAFixture, score_weights={"relevance": 1.0})
    result = assembler.assemble(
        query_cues={"topic": "nonexistent_xyz_123"},
        partition_filters={"agent_id": "a1"},
    )
    assert result.metadata["pull_count"] == 0


def test_a_populated_filter_does_not_short_circuit(backend):
    _save(PAFixture, agent_id="a1", topic="deploy", content="runbook")
    assembler = ContextAssembler(PAFixture, score_weights={"relevance": 1.0})
    result = assembler.assemble(
        query_cues={"topic": "deploy"}, partition_filters={"agent_id": "a1"}
    )
    assert result.metadata["pull_count"] == 1


def test_assess_standalone_returns_retrieval_quality(backend):
    _save(PAFixture, agent_id="agent-1", topic="deployment", content="notes")
    assembler = ContextAssembler(PAFixture, score_weights={"relevance": 1.0})
    quality = assembler.assess(query_cues={"topic": "deployment"})
    assert isinstance(quality, RetrievalQuality)
    assert "deployment" in quality.per_cue_fok
    assert quality.per_cue_fok["deployment"]["cue_familiarity"] in (0.0, 1.0)


def test_fok_cue_familiarity_with_existence_filter_present(backend):
    _save(PAFixture, agent_id="agent-1", topic="observable-topic", content="c")
    assembler = ContextAssembler(PAFixture, score_weights={"relevance": 1.0})
    quality = assembler.assess(query_cues={"topic": "observable-topic"})
    assert quality.per_cue_fok["observable-topic"]["cue_familiarity"] == 1.0


def test_avg_confidence_populated_with_confidence_field(backend):
    m1 = _save(PAFixture, agent_id="agent-1", topic="t1", content="c1")
    ConfidenceField.update_confidence(m1, "confidence", signal=0.9)
    assembler = ContextAssembler(
        PAFixture, score_weights={"relevance": 0.5, "confidence": 0.5}
    )
    result = assembler.assemble(
        query_cues={"topic": "t1"}, partition_filters={"agent_id": "agent-1"}
    )
    assert result.metadata["pull_count"] == 1
    quality = assembler.assemble(
        query_cues={"topic": "t1"},
        partition_filters={"agent_id": "agent-1"},
        assess_quality=True,
    ).metadata["quality"]
    # One 0.9 signal from 0.5, then (as the only candidate, it is selected)
    # no suppression: (0.5 + 0.9) / 2.
    assert _isclose(quality.avg_confidence, 0.7)


def test_score_spread_zero_on_empty_results(backend):
    assembler = ContextAssembler(PAFixture, score_weights={"relevance": 1.0})
    result = assembler.assemble(
        query_cues={"topic": "nothing-here"}, assess_quality=True
    )
    quality = result.metadata["quality"]
    assert quality.score_spread == 0.0
    assert quality.score_distribution == []


def test_assess_numeric_fixture(backend):
    """``TestRetrievalQualityAssembler.test_assess_numeric_fixture``: the
    same hardcoded scalars on both legs."""
    from popoto.fields.constants import Defaults

    for topic, signal in (("alpha", 0.9), ("beta", 0.5), ("gamma", 0.3)):
        m = _save(PAFixture, agent_id="a", topic=topic, content=topic)
        ConfidenceField.update_confidence(m, "confidence", signal=signal)
    assembler = ContextAssembler(
        PAFixture,
        score_weights={"relevance": 1.0},
        max_items=5,
        surfacing_threshold=0.5,
    )
    quality = assembler.assess(
        query_cues={"topic": "alpha", "other": "beta"},
        partition_filters={"agent_id": "a"},
    )
    assert _isclose(quality.avg_confidence, 1.6 / 3)
    assert _isclose(quality.score_spread, 0.0)
    assert _isclose(quality.fok_score, 0.64)
    assert _isclose(quality.staleness_ratio, 0.0)
    for cue in ("alpha", "beta"):
        comp = quality.per_cue_fok[cue]
        assert _isclose(comp["cue_familiarity"], 1.0)
        assert _isclose(comp["partial_retrieval_count"], 0.6)
        assert _isclose(comp["subthreshold_activation"], 0.0)
    assert len(quality.score_distribution) == 3
    for s in quality.score_distribution:
        assert _isclose(s, 0.01 ** (-Defaults.DECAY_RATE))


# -- the score proxy against top_by_decay --------------------------------------


def test_distinct_base_scores_give_nonzero_spread(backend):
    recs = [
        _save(PADecay, agent_id="a", content=f"c{i}", strength=w)
        for i, w in enumerate((1.0, 3.0, 9.0))
    ]
    proxy = _score_proxy_for_records(
        recs, model_class=PADecay, score_weights={"relevance": 1.0}
    )
    vals = sorted(proxy.values())
    factor = 0.01 ** (-0.5)
    for got, w in zip(vals, (1.0, 3.0, 9.0)):
        assert math.isclose(got, w * factor, rel_tol=1e-6)
    spread, dist = _compute_score_spread(
        recs, model_class=PADecay, score_weights={"relevance": 1.0}
    )
    assert spread > 0.0 and len(dist) == 3


def test_proxy_matches_top_by_decay(backend):
    recs = [
        _save(PADecay, agent_id="a", content=f"c{i}", strength=w)
        for i, w in enumerate((1.0, 4.0))
    ]
    now = time.time()
    backdate(backend, recs[0], now - 1 * DAY)
    backdate(backend, recs[1], now - 4 * DAY)
    ranked = PADecay.query.filter(agent_id="a").top_by_decay("relevance", n=10)
    proxy = _score_proxy_for_records(
        recs, model_class=PADecay, score_weights={"relevance": 1.0}
    )
    assert ranked[0].db_key.redis_key == max(proxy, key=proxy.get)
    # 4 * 4**-0.5 = 2 beats 1 * 1**-0.5 = 1, to the script's %.14g.
    assert math.isclose(proxy[recs[1].db_key.redis_key], 2.0, rel_tol=1e-6)


def test_a_record_outside_its_partition_scores_none(backend):
    """A record whose instance names another partition is not in that
    partition's sorted set on Redis, and scores ``None`` on both legs."""
    rec = _save(PADecay, agent_id="a", content="c", strength=2.0)
    weights = {"relevance": 1.0}
    proxy = _score_proxy_for_records([rec], model_class=PADecay, score_weights=weights)
    assert proxy[rec.db_key.redis_key] > 0
    # The same record, its instance moved to partition "b" without a save.
    rec.agent_id = "b"
    proxy = _score_proxy_for_records([rec], model_class=PADecay, score_weights=weights)
    assert list(proxy.values()) == [0.0]
    orphan = PADecay(agent_id="a", content="unsaved")
    proxy = _score_proxy_for_records(
        [orphan], model_class=PADecay, score_weights=weights
    )
    assert list(proxy.values()) == [0.0]


def test_aged_records_are_stale_fresh_are_not(backend):
    aged = _save(PADecay, agent_id="a", content="old", strength=1.0)
    fresh = _save(PADecay, agent_id="a", content="new", strength=1.0)
    backdate(backend, aged, time.time() - 40 * DAY)

    def ratio(records):
        return _staleness_ratio(
            records,
            model_class=PADecay,
            score_weights={"relevance": 1.0},
            surfacing_threshold=0.5,
            decaying_sorted_field_name="relevance",
        )

    assert ratio([fresh]) == 0.0
    assert ratio([aged, fresh]) == 0.5


def test_fok_subthreshold_activation_for_aged_records(backend):
    recs = [
        _save(PADecay, agent_id="a", content=f"deployment{i}", strength=1.0)
        for i in range(3)
    ]
    for r in recs:
        backdate(backend, r, time.time() - 40 * DAY)
    assembler = ContextAssembler(
        PADecay, score_weights={"relevance": 1.0}, surfacing_threshold=0.5
    )
    quality = assembler.assess(
        query_cues={"content": "deployment0"}, partition_filters={"agent_id": "a"}
    )
    subthreshold = [c["subthreshold_activation"] for c in quality.per_cue_fok.values()]
    assert subthreshold and all(s > 0.0 for s in subthreshold)


def test_proxy_matches_top_by_decay_with_confidence(backend):
    recs = []
    for i, confidence in enumerate((0.05, 0.95)):
        r = _save(PADecayConf, agent_id="a", content=f"c{i}", strength=1.0)
        plant_confidence(backend, r, confidence)
        recs.append(r)
    for r in recs:
        backdate(backend, r, time.time() - 10 * DAY)
    ranked = PADecayConf.query.filter(agent_id="a").top_by_decay("relevance", n=10)
    proxy = _score_proxy_for_records(
        recs, model_class=PADecayConf, score_weights={"relevance": 1.0}
    )
    assert len(set(proxy.values())) == 2, "proxy is not modulated by confidence"
    assert [r.db_key.redis_key for r in ranked] == sorted(
        proxy, key=proxy.get, reverse=True
    )
    assert ranked[0].db_key.redis_key == recs[1].db_key.redis_key


def test_linearity_in_base_score_under_a_neutral_confidence_corpus(backend):
    recs = []
    for i, w in enumerate((1.0, 3.0, 9.0)):
        r = _save(PADecayConf, agent_id="a", content=f"n{i}", strength=w)
        plant_confidence(backend, r, 0.5)
        recs.append(r)
    for r in recs:
        backdate(backend, r, time.time() - 10 * DAY)
    proxy = _score_proxy_for_records(
        recs, model_class=PADecayConf, score_weights={"relevance": 1.0}
    )
    vals = sorted(proxy.values())
    for got, w in zip(vals, (1.0, 3.0, 9.0)):
        assert math.isclose(got, w * 10 ** (-0.5), rel_tol=1e-6)


def test_proxy_is_neutral_when_no_evidence_has_been_recorded(backend):
    recs = [
        _save(PADecayConf, agent_id="a", content=f"z{i}", strength=w)
        for i, w in enumerate((1.0, 3.0))
    ]
    for r in recs:
        backdate(backend, r, time.time() - 10 * DAY)
    proxy = _score_proxy_for_records(
        recs, model_class=PADecayConf, score_weights={"relevance": 1.0}
    )
    for r, w in zip(recs, (1.0, 3.0)):
        assert math.isclose(proxy[r.db_key.redis_key], w * 10 ** (-0.5), rel_tol=1e-9)


def test_emit_trace_scores_are_the_decayed_proxy(backend):
    recs = [
        _save(PADecay, agent_id="a", content=f"c{i}", strength=w)
        for i, w in enumerate((1.0, 4.0))
    ]
    assembler = ContextAssembler(PADecay, score_weights={"relevance": 1.0})
    result = assembler.assemble(
        query_cues={"content": "c0"},
        partition_filters={"agent_id": "a"},
        emit_trace=True,
    )
    trace = result.metadata["trace"]
    assert [t["key"] for t in trace] == [
        recs[1].db_key.redis_key,
        recs[0].db_key.redis_key,
    ]
    factor = 0.01 ** (-0.5)
    assert math.isclose(trace[0]["score"], 4.0 * factor, rel_tol=1e-6)
    assert all(t["source"] == "pull" for t in trace)


# -- the confidence gate (pull path) -------------------------------------------

GATE_BASE_KEYS = {"applied", "gate_score", "threshold", "mode", "gated"}


def _gate(**kw):
    return ContextAssembler(PAGate, score_weights={"relevance": 1.0}, **kw)


def test_gate_with_no_pull_candidates(backend):
    result = _gate(confidence_gate_threshold=0.5).assemble(
        query_cues={"topic": "nonexistent_xyz"}, partition_filters={"agent_id": "a1"}
    )
    assert result.metadata["pull_count"] == 0
    assert result.metadata["gate"] == {
        "applied": False,
        "gate_score": None,
        "threshold": 0.5,
        "mode": "refuse",
        "gated": False,
    }


def test_gate_with_no_query_cues(backend):
    result = _gate(confidence_gate_threshold=0.5).assemble()
    assert set(result.metadata["gate"]) == GATE_BASE_KEYS
    assert result.metadata["gate"]["applied"] is False


def test_refuse_mode_gated_drops_the_pull_records_and_skips_suppression(backend):
    record = _save(PAGate, agent_id="a1", topic="pull-topic", content="c")
    assembler = _gate(confidence_gate_threshold=0.9, confidence_gate_mode="refuse")
    result = assembler.assemble(
        query_cues={"topic": "pull-topic"}, partition_filters={"agent_id": "a1"}
    )
    assert result.metadata["pull_count"] == 0
    assert result.metadata["gate"] == {
        "applied": True,
        "gate_score": 0.5,
        "threshold": 0.9,
        "mode": "refuse",
        "gated": True,
        "refused_keys": [record.db_key.redis_key],
    }
    # Refused, not suppressed: the candidate's confidence is untouched.
    assert ConfidenceField.get_confidence(record, "confidence") == 0.5


def test_refused_keys_come_from_all_pull_candidates_in_rank_order(backend):
    records = [
        _save(PAGate, agent_id="a1", topic=f"pull-topic-{i}", content="c")
        for i in range(3)
    ]
    assembler = _gate(confidence_gate_threshold=0.9)
    assembler._pull_path = lambda cues, filters: ([records[0]], list(records))
    result = assembler.assemble(
        query_cues={"topic": "pull-topic"}, partition_filters={"agent_id": "a1"}
    )
    assert result.metadata["pull_count"] == 0
    assert result.metadata["gate"]["refused_keys"] == [
        r.db_key.redis_key for r in records
    ]


def test_refuse_not_gated_retains_records(backend):
    _save(PAGate, agent_id="a1", topic="pull-topic", content="c")
    result = _gate(confidence_gate_threshold=0.1).assemble(
        query_cues={"topic": "pull-topic"}, partition_filters={"agent_id": "a1"}
    )
    assert result.metadata["pull_count"] == 1
    assert result.metadata["gate"]["gated"] is False
    assert result.metadata["gate"]["applied"] is True


def test_refuse_assess_quality_fok_not_corrupted(backend):
    _save(PAGate, agent_id="a1", topic="pull-topic", content="c")
    result = _gate(confidence_gate_threshold=0.9).assemble(
        query_cues={"topic": "pull-topic"},
        partition_filters={"agent_id": "a1"},
        assess_quality=True,
    )
    assert result.metadata["gate"]["gated"] is True
    assert result.metadata["quality"].fok_score > 0.0


@pytest.mark.parametrize("threshold,gated", [(0.9, True), (0.1, False)])
def test_flag_mode_retains_records(backend, threshold, gated):
    record = _save(PAGate, agent_id="a1", topic="pull-topic", content="c")
    result = _gate(
        confidence_gate_threshold=threshold, confidence_gate_mode="flag"
    ).assemble(query_cues={"topic": "pull-topic"}, partition_filters={"agent_id": "a1"})
    assert result.metadata["pull_count"] == 1
    assert result.metadata["gate"]["gated"] is gated
    if gated:
        assert result.metadata["gate"]["refused_keys"] == [record.db_key.redis_key]


def test_get_confidence_raises_gate_not_applied(backend, caplog):
    _save(PAGate, agent_id="a1", topic="pull-topic", content="c")
    assembler = _gate(confidence_gate_threshold=0.9)
    with patch.object(
        ConfidenceField, "get_confidence", side_effect=RuntimeError("boom")
    ):
        with caplog.at_level("WARNING", logger="POPOTO.ContextAssembler"):
            result = assembler.assemble(
                query_cues={"topic": "pull-topic"},
                partition_filters={"agent_id": "a1"},
            )
    assert "confidence gate get_confidence failed" in caplog.text
    assert result.metadata["gate"]["applied"] is False
    assert result.metadata["pull_count"] == 1


def test_a_model_without_a_validity_field_is_an_exact_passthrough(backend):
    for i in range(4):
        _save(PASimple, agent_id="a1", content=f"item-{i}")
    assembler = ContextAssembler(PASimple, score_weights={"relevance": 1.0})
    assert assembler._resolve_excluded_keys(None) is None
    baseline = assembler.assemble(
        query_cues={"content": "item-1"}, partition_filters={"agent_id": "a1"}
    )
    with_as_of = assembler.assemble(
        query_cues={"content": "item-1"},
        partition_filters={"agent_id": "a1"},
        as_of=time.time() - 3600,
    )
    assert [r.db_key.redis_key for r in baseline.records] == [
        r.db_key.redis_key for r in with_as_of.records
    ]
    assert len(baseline.records) == 4


# -- hybrid dispatch, the vector arm, the cold index ---------------------------


def test_forced_composite_with_hybrid_fields(backend):
    _save(PAHybrid, agent_id="a1", content="deploy runbook")
    assembler = ContextAssembler(
        PAHybrid, score_weights={"relevance": 1.0}, retrieval_mode="composite"
    )
    with patch.object(
        assembler, "_pull_path_composite", wraps=assembler._pull_path_composite
    ) as composite:
        result = assembler.assemble(
            query_cues={"topic": "test"}, partition_filters={"agent_id": "a1"}
        )
        composite.assert_called_once()
    assert result.metadata["pull_count"] == 1


def test_hybrid_continues_on_the_vector_arm_when_bm25_raises(backend):
    rec = _save(PAHybrid, agent_id="a1", content="deploy runbook")
    assembler = ContextAssembler(
        PAHybrid, score_weights={"relevance": 1.0}, retrieval_mode="hybrid"
    )
    with (
        patch(
            "popoto.fields.bm25_field.BM25Field.search",
            side_effect=Exception("BM25 failure"),
        ),
        patch.object(
            assembler, "_pull_path_composite", return_value=([], [])
        ) as composite,
    ):
        result = assembler.assemble(
            query_cues={"topic": "deploy runbook"},
            partition_filters={"agent_id": "a1"},
        )
        composite.assert_not_called()
    assert [r.db_key.redis_key for r in result.records] == [rec.db_key.redis_key]


def test_vector_scores_are_key_similarity_tuples(backend):
    rec = _save(PAHybrid, agent_id="a1", content="deploy runbook")
    result = QueryBuilder(PAHybrid.query)._get_vector_scores("deploy runbook", limit=10)
    assert [k for k, _s in result] == [rec.db_key.redis_key]
    assert isinstance(result[0][1], float)
    assert math.isclose(result[0][1], 1.0, abs_tol=1e-6)


def test_hybrid_scope_keeps_other_agents_out(backend):
    mine = _save(PAHybrid, agent_id="a1", content="deploy token alpha")
    for i in range(6):
        _save(PAHybrid, agent_id="other", content=f"deploy token alpha {i}")
    assembler = ContextAssembler(PAHybrid, score_weights={"relevance": 1.0})
    result = assembler.assemble(
        query_cues={"topic": "deploy token"}, partition_filters={"agent_id": "a1"}
    )
    assert [r.db_key.redis_key for r in result.records] == [mine.db_key.redis_key]


def test_cold_index_falls_back_to_composite(backend, caplog):
    for i in range(5):
        _save(PALexical, content=f"ci pipeline step {i}", topic="ci")
    clear_bm25_index(backend, PALexical, "content_bm25")
    assert BM25Field.search(PALexical, "content_bm25", "pipeline", limit=10) == []
    assembler = ContextAssembler(
        PALexical, score_weights={"relevance": 1.0}, max_items=10
    )
    with caplog.at_level(logging.WARNING, logger="POPOTO.ContextAssembler"):
        result = assembler.assemble(query_cues={"topic": "CI pipeline"})
    assert isinstance(result.records, list)
    assert any(
        "BM25 returned 0 hits" in r.getMessage()
        and "PALexical" in r.getMessage()
        and "re-save" in r.getMessage()
        for r in caplog.records
    )


def test_cold_index_recovery_after_resave(backend):
    saved = [
        _save(PALexical, content=f"database index {i}", topic="db") for i in range(3)
    ]
    _save(PALexical, content="unit pytest fixtures", topic="testing")
    clear_bm25_index(backend, PALexical, "content_bm25")
    assert BM25Field.search(PALexical, "content_bm25", "database index", limit=10) == []
    for rec in saved:
        rec.save()
    warm = BM25Field.search(PALexical, "content_bm25", "database index", limit=10)
    assert {k for k, _s in warm} == {r.db_key.redis_key for r in saved}


# -- post-effects --------------------------------------------------------------


def test_post_effects_stage_selected_reads_and_suppress_the_rest(backend):
    """The composite pull fetches ``max_items * 2`` candidates and stages one
    read on each (``_fire_on_read``); the post-effects then stage one more
    read on each selected record and send one suppression signal to each
    candidate that was not selected -- on Redis one queued script per
    candidate, on Postgres one bulk ``UPDATE`` in one transaction."""
    recs = [
        _save(PATracked, name=f"m{i}", agent_id="a1", content=f"c{i}") for i in range(4)
    ]
    for i, r in enumerate(recs):
        backdate(backend, r, time.time() - (i + 1) * DAY)
    assembler = ContextAssembler(
        PATracked, score_weights={"relevance": 1.0}, max_items=2
    )
    result = assembler.assemble(
        query_cues={"topic": "x"}, partition_filters={"agent_id": "a1"}
    )
    selected = [r.name for r in result.records]
    assert selected == ["m0", "m1"]
    suppressed = 0.5 + (COMPETITIVE_SUPPRESSION_SIGNAL - 0.5) / 2
    for r in recs:
        conf = ConfidenceField.get_confidence_data(r, "certainty")["confidence"]
        if r.name in selected:
            assert staged_reads(backend, r) == 2
            assert conf == 0.5
        else:
            assert staged_reads(backend, r) == 1
            assert _isclose(conf, suppressed)


def test_post_effects_then_on_context_used(backend):
    from popoto import ObservationProtocol

    recs = [
        _save(PATracked, name=f"m{i}", agent_id="a1", content=f"c{i}") for i in range(3)
    ]
    for i, r in enumerate(recs):
        backdate(backend, r, time.time() - (i + 1) * DAY)
    assembler = ContextAssembler(
        PATracked, score_weights={"relevance": 1.0}, max_items=2
    )
    result = assembler.assemble(
        query_cues={"topic": "x"}, partition_filters={"agent_id": "a1"}
    )
    ObservationProtocol.on_context_used(
        result.records,
        {
            result.records[0].db_key.redis_key: "acted",
            result.records[1].db_key.redis_key: "dismissed",
        },
    )
    acted, dismissed = result.records
    # Two staged reads (the composite fetch and the post-effects) confirmed.
    assert acted.access_count == 2
    assert staged_reads(backend, acted) == 0
    assert staged_reads(backend, dismissed) == 0
    assert dismissed.access_count == 0
    data = ConfidenceField.get_confidence_data(acted, "certainty")
    assert (data["corroborations"], data["evidence_count"]) == (1, 1)
    # The unselected third candidate was suppressed once, and nothing else.
    third = next(r for r in recs if r.name not in {acted.name, dismissed.name})
    data = ConfidenceField.get_confidence_data(third, "certainty")
    assert (data["contradictions"], data["evidence_count"]) == (1, 1)
