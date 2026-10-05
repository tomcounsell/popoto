"""Fixture models and a realistic source store for the #756 migration tests.

``MigMemory`` mirrors the shape of Valor's ``Memory`` model (``models/memory.py``
in the ai repo, as surveyed by #757 and #758): three key fields, a decay clock
partitioned by ``project_key`` with ``importance`` as its base score, a
``ConfidenceField``, a ``BM25Field`` and an ``EmbeddingField`` over
``content``, an ``ExistenceFilter``, ``WriteFilterMixin`` and
``AccessTrackerMixin``, and the outcome telemetry kept inside ``metadata``.
It is a fixture, not Valor's code: this repo never imports or reads Valor's
tree.

``MigLongTail`` and ``MigTtl`` cover the rest of the gap list in
``docs/features/postgres-backend.md`` ("Cross-backend migration notes"):
``ContentField``, ``FrequencySketch``, ``GeoField``, ``CyclicDecayField``
(with a declared baseline), ``CoOccurrenceField`` (over its ``max_edges``),
``ValidityField`` with supersession, ``EventStreamMixin``,
``PredictionLedgerMixin``, and a per-record TTL.

``VALOR_MEMORY_MAPPING`` is the declarative mapping the runbook documents for
Valor's ``Memory``.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

import popoto
from popoto.embeddings import AbstractEmbeddingProvider
from popoto.fields.access_tracker import AccessTrackerMixin
from popoto.fields.bm25_field import BM25Field
from popoto.fields.co_occurrence_field import CoOccurrenceField
from popoto.fields.confidence_field import ConfidenceField
from popoto.fields.constants import TemporalPeriod
from popoto.fields.content_field import ContentField
from popoto.fields.cyclic_decay_field import CyclicDecayField
from popoto.fields.decaying_sorted_field import DecayingSortedField
from popoto.fields.embedding_field import EmbeddingField
from popoto.fields.event_stream import EventStreamMixin
from popoto.fields.existence_filter import ExistenceFilter, FrequencySketch
from popoto.fields.prediction_ledger import PredictionLedgerMixin
from popoto.fields.supersession import SupersessionProtocol
from popoto.fields.validity_field import ValidityField
from popoto.fields.write_filter import WriteFilterMixin
from popoto.transfer.migrate_redis_to_postgres import ModelMapping

DIMS = 4


class StubProvider(AbstractEmbeddingProvider):
    """A deterministic 4-d provider. ``calls`` proves the migration never
    embeds: every vector it loads is the carried one."""

    _model_name = "mig-stub-v1"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts, input_type=None):
        self.calls.append(list(texts))
        return [
            np.random.RandomState(sum(map(ord, t)) % (2**31)).randn(DIMS).tolist()
            for t in texts
        ]

    @property
    def dimensions(self):
        return DIMS

    @property
    def max_batch_size(self):
        return 32


PROVIDER = StubProvider()


class MigMemory(WriteFilterMixin, AccessTrackerMixin, popoto.Model):
    """Valor's ``Memory`` shape (#757 Source Inventory)."""

    agent_id = popoto.KeyField()
    memory_id = popoto.KeyField()
    project_key = popoto.KeyField()
    content = popoto.StringField(default="")
    title = popoto.StringField(default="")
    importance = popoto.FloatField(default=1.0)
    source = popoto.StringField(default="agent")
    reference = popoto.StringField(default="")
    metadata = popoto.DictField(default=dict)
    superseded_by = popoto.StringField(default="")
    superseded_by_rationale = popoto.StringField(default="")
    relevance = DecayingSortedField(
        partition_by="project_key", base_score_field="importance"
    )
    confidence = ConfidenceField(initial_confidence=0.5)
    bm25 = BM25Field(source="content")
    embedding = EmbeddingField(source="content", provider=PROVIDER)
    bloom = ExistenceFilter(fingerprint_fn=lambda m: m.content)

    def compute_filter_score(self):
        return self.importance


class MigLongTail(EventStreamMixin, PredictionLedgerMixin, popoto.Model):
    """The long tail of the gap list."""

    _stream_name = "mig_longtail_events"

    name = popoto.UniqueKeyField()
    body = ContentField()
    place = popoto.GeoField(null=True)
    freq = FrequencySketch(fingerprint_fn=lambda m: m.name)
    rhythm = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.WEEKLY, 2.0, 0.0)],
        pressure_rate=0.1,
    )
    links = CoOccurrenceField(symmetric=False, max_edges=50)
    validity = ValidityField()


class MigTtl(popoto.Model):
    """A ``Meta.ttl`` model; one record carries a longer per-record TTL."""

    name = popoto.UniqueKeyField()

    class Meta:
        ttl = 3600


MODELS = (MigMemory, MigLongTail, MigTtl)

RETIREMENT_SENTINELS = (
    "dismissal-prune",
    "decay-prune-tier2",
    "cleanup-junk-extraction",
)

VALOR_MEMORY_MAPPING = ModelMapping(
    model=MigMemory,
    # Outcome history entries are the earliest evidence of a memory's
    # existence besides its access log (#757 L14).
    created_at_paths=("metadata.outcome_history[].ts",),
    sentinel_values={"superseded_by": RETIREMENT_SENTINELS},
    id_patterns={"memory_id": r"^[0-9a-f]{32}$"},
)

MAPPINGS = (
    VALOR_MEMORY_MAPPING,
    ModelMapping(model=MigLongTail),
    ModelMapping(model=MigTtl),
)


def _hex(i: int) -> str:
    return f"{i:032x}"


def seed_source(*, variant: str = "a", now: float | None = None) -> dict[str, Any]:
    """Write a realistic store through the bound Redis client.

    ``variant`` changes a few payloads so two "machines" can be merged: the
    shared ``memory_id`` 0x10 has equal payloads in both, 0x11 differs.
    Returns the facts the tests assert against."""
    now = time.time() if now is None else now
    day = 86400.0
    facts: dict[str, Any] = {"now": now}
    records = []
    specs = [
        # (id, project, content, importance, age_days, source)
        (1, "ai", "postgres index maintenance runbook", 6.0, 30, "human"),
        (2, "ai", "redis snapshot before cutover", 2.0, 5, "agent"),
        (3, "ai", "decay ranking uses importance", 3.0, 1, "agent"),
        (4, "web", "frontend caching layer notes", 1.0, 60, "system"),
        (5, "web", "postgres vector index hnsw", 4.0, 2, "knowledge"),
        (6, "ai", "dismissed junk extraction", 0.5, 90, "agent"),
        (7, "ai", "superseded fact about index", 1.5, 40, "agent"),
        (8, "ai", "missing embedding file record", 1.0, 3, "agent"),
        (9, "ai", "wrong dimension embedding record", 1.0, 4, "agent"),
    ]
    if variant == "b":
        specs = [(i + 100, *rest) for i, *rest in specs]
    specs.append((0x10, "ai", "shared memory synced to two machines", 2.5, 10, "human"))
    shared_body = "shared memory edited on machine " + variant
    specs.append((0x11, "ai", shared_body, 2.5, 8 if variant == "a" else 6, "agent"))

    for ident, project, content, importance, age, source in specs:
        memory = MigMemory(
            agent_id="valor",
            memory_id=_hex(ident),
            project_key=project,
            content=content,
            title=content[:16],
            importance=importance,
            source=source,
            reference='{"url": "https://example.test/%d"}' % ident,
            metadata={
                "tags": ["seed"],
                "category": "fixture",
                "dismissal_count": 0,
                "outcome_history": [
                    {"outcome": "acted", "ts": now - (age + 2) * day},
                ],
            },
        )
        memory.relevance = now - age * day
        memory.save(skip_auto_now=True)
        records.append(memory)
    by_id = {int(m.memory_id, 16): m for m in records}
    facts["by_id"] = {i: m.db_key.redis_key for i, m in by_id.items()}
    base = 100 if variant == "b" else 0

    # Confidence evidence, access history and staged reads.
    first = by_id[base + 1]
    ConfidenceField.update_confidence(first, "confidence", 0.9)
    ConfidenceField.update_confidence(first, "confidence", 0.8)
    ConfidenceField.update_confidence(by_id[base + 2], "confidence", 0.1)
    for _ in range(3):
        first.on_read()
    first.confirm_access()
    by_id[base + 3].on_read()  # a staged, unconfirmed read
    by_id[base + 3].on_read()

    # Retirement sentinel and a real supersession pointer.
    retired = by_id[base + 6]
    retired.superseded_by = "dismissal-prune"
    retired.save(skip_auto_now=True)
    old = by_id[base + 7]
    old.superseded_by = by_id[base + 1].memory_id
    old.superseded_by_rationale = "replaced by the runbook"
    old.save(skip_auto_now=True)

    if variant == "b":
        # Two records Postgres cannot take: a NUL byte, and an id that is
        # not Valor's 32-hex shape. Both are rejected and counted.
        for memory_id, content in (
            (_hex(112), "nul\x00byte in content"),
            ("not-a-hex-id", "an id outside the pattern"),
        ):
            bad = MigMemory(
                agent_id="valor",
                memory_id=memory_id,
                project_key="ai",
                content=content,
                metadata={},
            )
            bad.relevance = now - day
            bad.save(skip_auto_now=True)
            records.append(bad)
        facts["rejected_keys"] = sorted(r.db_key.redis_key for r in records[-2:])

    facts["memory_keys"] = sorted(m.db_key.redis_key for m in records)
    facts["missing_npy_key"] = by_id[base + 8].db_key.redis_key
    facts["wrong_dim_key"] = by_id[base + 9].db_key.redis_key
    facts["sentinel_key"] = retired.db_key.redis_key
    facts["staged_key"] = by_id[base + 3].db_key.redis_key
    facts["accessed_key"] = first.db_key.redis_key

    if variant == "a":
        _seed_longtail()
        record = MigTtl(name="long")
        record._ttl = 7200
        record.save()
        MigTtl.create(name="short")
    return facts


def _seed_longtail() -> None:
    a = MigLongTail.create(name="alpha", body="alpha body text", place=(41.9, 12.5))
    SupersessionProtocol.supersede(a, identity_key="topic")
    b = MigLongTail.create(name="beta", body="beta body text")
    SupersessionProtocol.supersede(b, identity_key="topic")
    field = MigLongTail._meta.fields["links"]
    field.link(MigLongTail, a.db_key.redis_key, b.db_key.redis_key, 0.4)
    field.link(MigLongTail, a.db_key.redis_key, "MigLongTail:never-saved", 0.7)
    b.strengthen_cycle("rhythm", factor=1.5)
    MigLongTail.record_prediction(a, {"score": 0.25})
    MigLongTail.resolve_prediction(a, {"score": 1.0})
