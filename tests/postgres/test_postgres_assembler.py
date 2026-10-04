"""``[PG-only]`` and two-server checks for ``ContextAssembler`` on Postgres
(#759 M2c, plan §5 M2 exit criteria, #758 D8).

The behaviour each leg shares is the conformance suite
(``test_context_assembler*.py``, ``test_retrieval_quality_regression.py``,
``test_backend_parity_assembler.py``). This file holds what needs both
servers in one test, or Postgres alone:

* the exit criterion: for the retrieval-quality fixtures,
  ``ContextAssembler(retrieval_mode="auto")`` returns the same ranked keys,
  with the same token accounting, on a Redis-bound and a Postgres-bound
  model, because the hybrid path ranks BM25 with corpus-wide statistics;
* with the Redis client patched to record (and refuse) every command, a
  Postgres-bound ``assemble()`` plus ``on_context_used()`` -- every option
  the assembler has, ``assess()`` too -- issues none and completes;
* a first save on a fresh database creates the schema with no CLI step;
* a sleeping embedding provider cannot push ``save()`` past the backfill
  budget on the assembler's model, and ``assemble()`` right after it is not
  held up;
* the post-effects are one transaction of bulk statements, and the bulk
  suppression is bit-identical to ``update_confidence``;
* an outage propagates instead of reading as "no memories";
* a CI-sized slice of ``scripts/probe_assembler_parity.py``.
"""

import importlib.util
import json
import random
import time
import uuid
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

np = pytest.importorskip("numpy")

import popoto  # noqa: E402
from popoto import (  # noqa: E402
    AccessTrackerMixin,
    ConfidenceField,
    DecayingSortedField,
    ObservationProtocol,
    WriteFilterMixin,
)
from popoto.backends import (  # noqa: E402
    BackendUnavailableError,
    RecordId,
    _swap_instance,
    get_backend,
    set_backend,
)
from popoto.backends.postgres import search as search_mod  # noqa: E402
from popoto.embeddings import AbstractEmbeddingProvider  # noqa: E402
from popoto.fields.bm25_field import BM25Field  # noqa: E402
from popoto.fields.constants import Defaults  # noqa: E402
from popoto.fields.embedding_field import EmbeddingField  # noqa: E402
from popoto.fields.existence_filter import ExistenceFilter  # noqa: E402
from popoto.recipes.context_assembler import (  # noqa: E402
    COMPETITIVE_SUPPRESSION_SIGNAL,
    ContextAssembler,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "retr1_corpus.json"
PROBE = ROOT / "scripts" / "probe_assembler_parity.py"


class HashProvider(AbstractEmbeddingProvider):
    """Deterministic vectors from the text; optionally sleeps on a text."""

    def __init__(self, dim=8):
        self._dim = dim
        self.sleep_on = None
        self.sleep = 0.0
        self.calls = []

    def embed(self, texts, input_type=None):
        self.calls.append((list(texts), input_type))
        if self.sleep_on is not None and self.sleep_on in texts:
            time.sleep(self.sleep)
        return [
            np.random.RandomState(zlib.crc32(t.encode())).randn(self._dim).tolist()
            for t in texts
        ]

    @property
    def dimensions(self):
        return self._dim

    @property
    def max_batch_size(self):
        return 32


PROVIDER = HashProvider()


# -- models ---------------------------------------------------------------------


class RetrLexical(popoto.Model):
    """``test_retrieval_quality_regression.RegressionMemory``'s fields: BM25
    only, so ``auto`` resolves to lexical. ``record_id`` is the key here
    rather than an ``AutoKeyField`` beside it, so a record has the same key
    on both legs: equal BM25 scores are ordered by key, and so is the RRF
    sum's input, and random keys would order them differently per leg."""

    record_id = popoto.KeyField()
    content = popoto.Field()
    content_bm25 = BM25Field(source="content")
    topic = popoto.Field()


class RetrHybrid(popoto.Model):
    """The same corpus partitioned by ``agent_id``, as
    ``test_context_assembler_hybrid.py``'s models are, with an embedding, so
    ``auto`` resolves to hybrid."""

    agent_id = popoto.KeyField()
    record_id = popoto.KeyField()
    content = popoto.Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")
    content_index = BM25Field(source="content")
    embedding = EmbeddingField(source="content", provider=PROVIDER)


class ValorMemory(WriteFilterMixin, AccessTrackerMixin, popoto.Model):
    """Valor's memory shape (#758 spike-1): every field the assembler reads."""

    memory_id = popoto.AutoKeyField()
    project_key = popoto.KeyField()
    agent_id = popoto.KeyField()
    tier = popoto.Field(type=str, default="hot")
    tags = popoto.TagField()
    content = popoto.StringField(default="")
    importance = popoto.FloatField(default=1.0)
    metadata = popoto.DictField(default=dict)
    relevance = DecayingSortedField(
        partition_by="project_key", base_score_field="importance"
    )
    confidence = ConfidenceField()
    lexical = BM25Field(source="content")
    embedding = EmbeddingField(source="content", provider=PROVIDER)
    bloom = ExistenceFilter(fingerprint_fn=lambda m: m.content)

    def compute_filter_score(self):
        return self.importance


@pytest.fixture(autouse=True)
def _provider(tmp_path, monkeypatch):
    monkeypatch.setenv("POPOTO_CONTENT_PATH", str(tmp_path / "content"))
    PROVIDER.sleep_on = None
    PROVIDER.calls = []
    yield PROVIDER
    PROVIDER.sleep_on = None


def _leg(pg, name):
    set_backend("redis" if name == "redis" else pg)


def _wipe_redis(*models):
    client = popoto.get_redis()
    for model in models:
        for key in client.scan_iter(match=f"*{model.__name__}*", count=1000):
            client.delete(key)


def _corpus():
    return json.loads(FIXTURE.read_text())


# -- the M2 exit criterion: same ranked keys on both legs ----------------------


def test_the_retrieval_quality_fixture_ranks_identically_on_both_legs(pg):
    """Lexical (``auto`` on a BM25-only model): the 200-record, 20-query
    fixture, the same records in the same order and the same token count for
    every query, and the regression file's P@10 bar holds on Postgres."""
    from popoto.fields.embedding_field import invalidate_cache

    data = _corpus()
    ranked = {}
    try:
        for leg in ("redis", "postgres"):
            _leg(pg, leg)
            invalidate_cache()
            for rec in data["records"]:
                RetrLexical(
                    content=rec["content"], topic=rec["topic"], record_id=str(rec["id"])
                ).save()
            assembler = ContextAssembler(
                RetrLexical, score_weights={"relevance": 1.0}, max_items=10
            )
            assert assembler._effective_mode == "lexical"
            ranked[leg] = []
            for q in data["queries"]:
                result = assembler.assemble(query_cues={"topic": q["query_text"]})
                ranked[leg].append(
                    (
                        [r.db_key.redis_key for r in result.records],
                        result.metadata["token_count"],
                        result.formatted,
                    )
                )
    finally:
        _leg(pg, "postgres")
        _wipe_redis(RetrLexical)
    assert ranked["redis"] == ranked["postgres"]
    hits = [
        len({int(k.rsplit(":", 1)[1]) for k in keys} & set(q["relevant_ids"])) / 10
        for (keys, _t, _f), q in zip(ranked["postgres"], data["queries"])
    ]
    assert sum(len(keys) for keys, _t, _f in ranked["postgres"]) > 100
    assert sum(hits) / len(hits) >= 0.5


def test_the_fixture_ranks_identically_through_the_hybrid_path(pg):
    """Hybrid (``auto`` on BM25 + embedding), partitioned by ``agent_id``: the
    fixture split over two agents, every query scoped to each agent, the
    same keys in the same order on both legs."""
    from popoto.fields.embedding_field import invalidate_cache

    data = _corpus()
    ranked = {}
    t0 = 1_790_000_000.0
    try:
        for leg in ("redis", "postgres"):
            _leg(pg, leg)
            invalidate_cache()
            for rec in data["records"]:
                # The decay field is serialized into the formatted output:
                # both legs save each record at the same instant. The
                # fixture's texts repeat (templated per topic), and equal
                # texts are equal vectors, which the two vector arms order
                # differently by design (Redis: .npy listing order; Postgres:
                # key -- a documented M2b row): the record id makes each
                # text, so each vector, distinct, as real memories are.
                with patch("time.time", lambda: t0 + rec["id"]):
                    RetrHybrid(
                        agent_id=f"agent-{rec['id'] % 2}",
                        content=f"{rec['content']} (memory {rec['id']})",
                        record_id=str(rec["id"]),
                    ).save()
            assembler = ContextAssembler(
                RetrHybrid, score_weights={"relevance": 1.0}, max_items=10
            )
            assert assembler._effective_mode == "hybrid"
            ranked[leg] = []
            for q in data["queries"]:
                for agent in ("agent-0", "agent-1"):
                    result = assembler.assemble(
                        query_cues={"topic": q["query_text"]}, agent_id=agent
                    )
                    ranked[leg].append(
                        (
                            [r.db_key.redis_key for r in result.records],
                            result.metadata["token_count"],
                            result.formatted,
                        )
                    )
    finally:
        _leg(pg, "postgres")
        _wipe_redis(RetrHybrid)
    assert ranked["redis"] == ranked["postgres"]
    assert all(len(keys) == 10 for keys, _t, _f in ranked["postgres"])


def test_the_hybrid_path_ranks_bm25_with_corpus_statistics(pg, monkeypatch):
    """Decision 3: the assembler's BM25 arm uses corpus-wide statistics
    (``stats="corpus"``), never ``recall()``'s per-scope default -- and on a
    corpus whose scopes differ the two would score differently, so the
    choice is load-bearing for the parity above."""
    for i in range(6):
        RetrHybrid(
            agent_id="big", record_id=f"b{i}", content=f"kubernetes upgrade notes {i}"
        ).save()
    RetrHybrid(agent_id="small", record_id="s0", content="kubernetes rollout").save()
    RetrHybrid(agent_id="small", record_id="s1", content="unrelated text").save()
    backend = get_backend(RetrHybrid)
    seen = []
    real = backend.keyword_search

    def spy(*args, **kwargs):
        # No default: a caller that dropped the kwarg would record None
        # (#779 review note 5).
        seen.append(kwargs.get("stats"))
        return real(*args, **kwargs)

    monkeypatch.setattr(backend, "keyword_search", spy)
    result = ContextAssembler(RetrHybrid, score_weights={"relevance": 1.0}).assemble(
        query_cues={"topic": "kubernetes"}, agent_id="small"
    )
    assert [r.content for r in result.records][0] == "kubernetes rollout"
    assert seen == ["corpus"]
    spec = RetrHybrid._meta.spec
    corpus = real(spec, "content_index", ["kubernetes"], limit=5, scope="small")
    scoped = real(
        spec, "content_index", ["kubernetes"], limit=5, scope="small", stats="scope"
    )
    assert corpus and scoped
    assert corpus[0][1] != scoped[0][1]


# -- zero Redis commands ------------------------------------------------------


class _RedisRecorder:
    """Patches the Redis connection layer: every command (and every attempt
    to check out a connection) is recorded and refused."""

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


def test_a_postgres_bound_assembler_issues_no_redis_command(pg, monkeypatch):
    recorder = _RedisRecorder(monkeypatch)
    for i in range(24):
        ValorMemory(
            project_key="valor" if i % 3 else "other",
            agent_id=f"agent-{i % 2}",
            tier="hot" if i % 4 else "cold",
            tags=["ops"] if i % 2 else ["dev"],
            content=f"kubernetes deploy runbook step {i} alpha",
            importance=1.0 + i / 10,
            metadata={"i": i},
        ).save()
    assembler = ContextAssembler(
        ValorMemory,
        score_weights={"relevance": 0.6, "confidence": 0.3},
        max_items=4,
        max_tokens=400,
        confidence_gate_threshold=0.1,
        confidence_gate_mode="flag",
    )
    assert assembler._effective_mode == "hybrid"
    result = assembler.assemble(
        query_cues={"topic": "kubernetes deploy", "other": "runbook"},
        partition_filters={"project_key": "valor", "tier": "hot"},
        agent_id="agent-1",
        tags=["ops"],
        tag_match="any",
        assess_quality=True,
        emit_trace=True,
        exclude_keys={"ValorMemory:nope"},
        record_gate=lambda r: True,
    )
    assert result.records, "the assembler found nothing; the proof would be vacuous"
    assert result.metadata["quality"].fok_score > 0
    assert result.metadata["trace"][0]["score"] > 0
    keys = [r.db_key.redis_key for r in result.records]
    ObservationProtocol.on_context_used(
        result.records,
        dict(zip(keys, ["acted", "used", "dismissed", "contradicted"])),
    )
    quality = assembler.assess({"topic": "kubernetes"}, {"project_key": "valor"})
    assert quality.per_cue_fok["kubernetes"]["cue_familiarity"] == 1.0
    lexical = ContextAssembler(
        ValorMemory, score_weights={"relevance": 1.0}, retrieval_mode="lexical"
    ).assemble({"topic": "runbook"}, partition_filters={"project_key": "valor"})
    composite = ContextAssembler(
        ValorMemory, score_weights={"relevance": 1.0}, retrieval_mode="composite"
    ).assemble({"topic": "runbook"}, partition_filters={"project_key": "valor"})
    assert lexical.records and composite.records
    assert recorder.calls == []
    # The recorder is live: a Redis command here is caught (non-vacuity).
    with pytest.raises(Exception):
        popoto.get_redis().ping()
    assert len(recorder.calls) == 1
    # The effects landed: the acted record's reads were confirmed and its
    # confidence corroborated.
    acted = result.records[0]
    assert acted.access_count >= 1
    data = ConfidenceField.get_confidence_data(acted, "confidence")
    assert data["corroborations"] == 1


def test_the_post_effects_are_one_transaction_of_bulk_statements(pg, monkeypatch):
    for i in range(8):
        ValorMemory(
            project_key="valor", agent_id="a", content=f"deploy note {i}"
        ).save()
    backend = get_backend(ValorMemory)
    ops = []
    real = backend.field_call

    def spy(spec, field, op, *args, **kwargs):
        ops.append((field, op))
        return real(spec, field, op, *args, **kwargs)

    monkeypatch.setattr(backend, "field_call", spy)
    monkeypatch.setattr(
        ConfidenceField,
        "update_confidence",
        classmethod(lambda *a, **k: pytest.fail("per-candidate update_confidence")),
    )
    assembler = ContextAssembler(
        ValorMemory, score_weights={"relevance": 1.0}, max_items=2
    )
    candidates = list(ValorMemory.query.filter(project_key="valor"))
    assembler._pull_path = lambda cues, filters: (candidates[:2], candidates)
    ops.clear()
    assembler.assemble({"topic": "deploy"}, partition_filters={"project_key": "valor"})
    assert ops == [
        ("_observe", "atomically"),
        ("_observe", "lock"),
        ("_access", "stage"),
        ("confidence", "signal_many"),
    ]


def test_bulk_suppression_matches_update_confidence_bit_for_bit(pg):
    rng = random.Random(759)
    bulk, single = [], []
    for i in range(40):
        a = ValorMemory(project_key="p", agent_id="a", content=f"a{i}")
        a.save()
        b = ValorMemory(project_key="p", agent_id="b", content=f"b{i}")
        b.save()
        for _ in range(rng.randint(0, 30)):
            s = rng.choice([0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0, rng.random()])
            ConfidenceField.update_confidence(a, "confidence", signal=s)
            ConfidenceField.update_confidence(b, "confidence", signal=s)
        bulk.append(a)
        single.append(b)
    backend = get_backend(ValorMemory)
    spec = ValorMemory._meta.spec
    for signal in (COMPETITIVE_SUPPRESSION_SIGNAL, 0.9, 5e-324, 0.5):
        count = backend.field_call(
            spec,
            "confidence",
            "signal_many",
            [RecordId.from_key("ValorMemory", r.db_key.redis_key) for r in bulk]
            + [RecordId.from_key("ValorMemory", "ValorMemory:missing")],
            signal,
        )
        assert count == len(bulk)
        for r in single:
            ConfidenceField.update_confidence(r, "confidence", signal=signal)
        table = backend._table(spec)
        rows, _ = backend._run(
            f'SELECT "agent_id", "content", "confidence__conf", "confidence__n", '
            f'"confidence__corr", "confidence__contra" FROM {table.qualified}',
            [],
        )
        state = {(agent, content[1:]): tuple(rest) for agent, content, *rest in rows}
        for i in range(40):
            assert state[("a", str(i))] == state[("b", str(i))]


def test_an_outage_propagates_instead_of_reading_as_no_memories(pg, monkeypatch):
    ValorMemory(project_key="valor", agent_id="a", content="deploy note").save()
    backend = get_backend(ValorMemory)

    def down(*args, **kwargs):
        raise BackendUnavailableError("PoolTimeout: postgres is down")

    monkeypatch.setattr(backend, "keyword_search", down)
    assembler = ContextAssembler(ValorMemory, score_weights={"relevance": 1.0})
    with pytest.raises(BackendUnavailableError):
        assembler.assemble(
            {"topic": "deploy"}, partition_filters={"project_key": "valor"}
        )


# -- fully subconscious: DDL on first use, a bounded backfill -----------------


def test_a_first_save_on_a_fresh_database_creates_the_schema(pg_schema, admin):
    """No CLI step: a schema that does not exist yet, a first ``save``
    through the assembler's model, and ``assemble()`` finds it."""
    from popoto.backends.postgres import PostgresBackend

    schema = f"popoto_test_{uuid.uuid4().hex}"
    assert not admin.execute(
        "SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)
    ).fetchall()
    backend = PostgresBackend(dsn=pg_schema.url, schema=schema)
    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)
    try:
        memory = ValorMemory(project_key="valor", agent_id="a", content="first memory")
        memory.save()
        result = ContextAssembler(
            ValorMemory, score_weights={"relevance": 1.0}
        ).assemble(
            {"topic": "first memory"}, partition_filters={"project_key": "valor"}
        )
        assert [r.content for r in result.records] == ["first memory"]
        tables = {
            row[0]
            for row in admin.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,)
            ).fetchall()
        }
        assert {"valor_memory", "popoto_schema"} <= tables
        recorded = admin.execute(
            f'SELECT table_name FROM "{schema}".popoto_schema'
        ).fetchall()
        assert ("valor_memory",) in recorded
    finally:
        _swap_instance("postgres", previous_instance)
        set_backend(previous)
        backend.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_a_sleeping_provider_cannot_hold_save_or_assemble(pg, monkeypatch):
    """The backfill budget on the assembler's model: a row saved while the
    provider was missing, a provider that then sleeps on that row's text (the
    backfill's call), a save that returns within the budget, and an
    ``assemble()`` right after it that is not held up either -- the row
    without a vector is still found by its BM25 arm."""
    monkeypatch.setattr(Defaults, "PG_BACKFILL_BUDGET_SECONDS", 0.3)
    field = ValorMemory._meta.fields["embedding"]
    field._provider = None
    try:
        ValorMemory(
            project_key="valor", agent_id="a", content="waiting for a vector"
        ).save()
    finally:
        field._provider = PROVIDER
    PROVIDER.sleep_on = "waiting for a vector"
    PROVIDER.sleep = 3.0
    start = time.monotonic()
    ValorMemory(
        project_key="valor", agent_id="a", content="this save returns in budget"
    ).save()
    assert time.monotonic() - start < 0.3 + 1.0
    start = time.monotonic()
    result = ContextAssembler(ValorMemory, score_weights={"relevance": 1.0}).assemble(
        {"topic": "waiting vector"}, partition_filters={"project_key": "valor"}
    )
    assert time.monotonic() - start < 1.0
    assert "waiting for a vector" in [r.content for r in result.records]
    assert search_mod._backfill_busy.locked()
    PROVIDER.sleep_on = None
    assert search_mod._backfill_busy.acquire(timeout=10)
    search_mod._backfill_busy.release()


# -- the seeded probe, CI-sized ------------------------------------------------


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_assembler_parity", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_a_seeded_assembler_probe_finds_no_undocumented_mismatch(pg, tmp_path):
    module = _load_probe()
    try:
        probe = module.run(pg, seeds=[759], shapes=12)
    finally:
        set_backend(pg)
        _wipe_redis(*module.MODELS)
    assert probe.shapes == 12
    assert probe.calls >= 24
    assert sum(probe.nonempty.values()) >= 12
    assert not probe.unexplained(), probe.report()
