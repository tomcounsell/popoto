"""``[PG-only]`` transfer on Postgres (#759 M5, plan §2: ``transfer/`` is
composed of ``load``/``save`` plus ``field_call``).

The conformance half is the ``test_transfer_*.py`` files on both legs. Their
assertions that read a Redis structure through the raw client (a companion
hash, an ``.npy`` file, an edge sorted set) are ``redis_only``; this file
checks the same carried state on Postgres, through its columns and tables.

Three properties, per field family:

* **Same-backend round trip** -- export, a *fresh* schema (every table
  dropped, so nothing survives but the file), import, export again: the two
  exports' records are equal, and the reads agree.
* **Key regeneration** (``preserve_keys=False``) -- new keys, references
  remapped, carried state on the new key.
* **Cross-backend** -- an export taken from a Redis-bound model imports into
  the same model bound to Postgres and reads identically (a step toward
  #756's one-off copy, which needs exactly this file format to be
  backend-neutral). What does not cross is listed in ``CROSS_BACKEND_DROPS``.
"""

import io
import json

import pytest

np = pytest.importorskip("numpy")

import popoto  # noqa: E402
from popoto.backends import get_backend, set_backend  # noqa: E402
from popoto.embeddings import AbstractEmbeddingProvider  # noqa: E402
from popoto.fields.access_tracker import AccessTrackerMixin  # noqa: E402
from popoto.fields.bm25_field import BM25Field  # noqa: E402
from popoto.fields.co_occurrence_field import CoOccurrenceField  # noqa: E402
from popoto.fields.confidence_field import ConfidenceField  # noqa: E402
from popoto.fields.constants import TemporalPeriod  # noqa: E402
from popoto.fields.cyclic_decay_field import CyclicDecayField  # noqa: E402
from popoto.fields.decaying_sorted_field import DecayingSortedField  # noqa: E402
from popoto.fields.embedding_field import EmbeddingField  # noqa: E402
from popoto.fields.existence_filter import ExistenceFilter  # noqa: E402
from popoto.fields.prediction_ledger import PredictionLedgerMixin  # noqa: E402
from popoto.fields.supersession import SupersessionProtocol  # noqa: E402
from popoto.fields.validity_field import ValidityField  # noqa: E402
from popoto.transfer import export_records, import_records  # noqa: E402


class _Provider(AbstractEmbeddingProvider):
    _model_name = "tx-stub-v1"

    def embed(self, texts, input_type=None):
        return [
            np.random.RandomState(sum(map(ord, t)) % (2**31)).randn(4).tolist()
            for t in texts
        ]

    @property
    def dimensions(self):
        return 4

    @property
    def max_batch_size(self):
        return 32


PROVIDER = _Provider()


class TxAuthor(popoto.Model):
    name = popoto.UniqueKeyField()
    bio = popoto.StringField(default="")


class TxPlain(popoto.Model):
    """KeyField / Indexed / Tag / Relationship / the collection fields."""

    key = popoto.KeyField()
    owner = popoto.IndexedField(type=str, null=True)
    tags = popoto.TagField()
    author = popoto.Relationship(model=TxAuthor, null=True)
    items = popoto.ListField(null=True)
    attrs = popoto.DictField(null=True)
    labels = popoto.SetField(null=True)
    amount = popoto.DecimalField(null=True)
    when = popoto.DatetimeField(null=True)
    rank = popoto.SortedField(type=float, default=0.0)


class TxMemory(AccessTrackerMixin, popoto.Model):
    """Valor's slice: decay, confidence, BM25, embedding, existence, access."""

    memory_id = popoto.AutoKeyField()
    project = popoto.Field(type=str, default="ai")
    content = popoto.StringField(default="")
    relevance = DecayingSortedField(partition_by="project")
    certainty = ConfidenceField(initial_confidence=0.5)
    lexical = BM25Field(source="content")
    vector = EmbeddingField(source="content", provider=PROVIDER)
    seen = ExistenceFilter(fingerprint_fn=lambda m: m.content)


class TxHistory(PredictionLedgerMixin, popoto.Model):
    """The carried long tail: validity + supersession, graph, cycles, ledger."""

    name = popoto.UniqueKeyField()
    validity = ValidityField()
    edges = CoOccurrenceField(symmetric=False, max_edges=50)
    rhythm = CyclicDecayField(
        decay_rate=0.5, cycles=[(TemporalPeriod.WEEKLY, 2.0, 0.0)], pressure_rate=0.1
    )


def _records(text):
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    return sorted(lines[1:], key=lambda r: r["key"])


def _fresh(pg, pg_schema):
    """A fresh destination schema: every table dropped, nothing memoised."""
    pg_schema.drop_tables()
    pg.forget_tables()


def _import(model, text, **kwargs):
    report = import_records(model, io.StringIO(text), **kwargs)
    assert report.count("landed") == report.total, report.summary()
    return report


def _normalize(record):
    """Compare what each family promises to carry. A cycles entry's 4th
    slot (the declared baseline) is deployment-local and deliberately not
    imported (#698), so it is compared on its first three slots."""
    record = json.loads(json.dumps(record))
    rhythm = (record.get("state") or {}).get("rhythm")
    if rhythm and "cycles" in rhythm:
        rhythm["cycles"] = [c[:3] for c in rhythm["cycles"]]
    return record


@pytest.fixture(autouse=True)
def _content_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("POPOTO_CONTENT_PATH", str(tmp_path / "content"))


def _seed_plain():
    import datetime
    import decimal

    ada = TxAuthor.create(name="ada", bio="analyst")
    TxAuthor.create(name="bob")
    TxPlain(
        key="p1",
        owner="ops",
        tags=["x", "y"],
        author=ada,
        items=[1, "two", 3.5],
        attrs={"a": 1, "nested": {"b": [1, 2]}},
        labels={"m", "n"},
        amount=decimal.Decimal("12.34"),
        when=datetime.datetime(2024, 5, 6, 7, 8, 9, tzinfo=datetime.timezone.utc),
        rank=3.0,
    ).save()
    TxPlain(key="p2", rank=1.0).save()


def _seed_memory():
    import time

    a = TxMemory(project="ai", content="postgres index maintenance")
    a.relevance = time.time() - 400 * 86400
    a.save(skip_auto_now=True)
    ConfidenceField.update_confidence(a, "certainty", 0.9)
    ConfidenceField.update_confidence(a, "certainty", 0.2)
    for _ in range(3):
        a.on_read()
    a.confirm_access()
    TxMemory(project="web", content="redis caching layer").save()
    return a


def _seed_history():
    old = TxHistory.create(name="old")
    SupersessionProtocol.supersede(old, identity_key="topic")
    new = TxHistory.create(name="new")
    SupersessionProtocol.supersede(new, identity_key="topic")
    field = TxHistory._meta.fields["edges"]
    field.link(TxHistory, old.db_key.redis_key, new.db_key.redis_key, 0.4)
    field.link(TxHistory, old.db_key.redis_key, "TxHistory:never-exported", 0.7)
    new.strengthen_cycle("rhythm", factor=1.5)
    TxHistory.record_prediction(old, {"score": 0.25})
    TxHistory.resolve_prediction(old, {"score": 1.0})
    TxHistory.record_prediction(new, {"score": 0.5})


# -- same-backend round trip, per family --------------------------------------------


def test_plain_fields_round_trip_into_a_fresh_schema(pg, pg_schema):
    _seed_plain()
    authors, plain = export_records(TxAuthor).data, export_records(TxPlain).data
    before = {p.key: p.to_dict() for p in TxPlain.query.all()}
    _fresh(pg, pg_schema)
    _import(TxAuthor, authors)
    _import(TxPlain, plain)
    assert _records(export_records(TxPlain).data) == _records(plain)
    assert {p.key: p.to_dict() for p in TxPlain.query.all()} == before
    # The indexes the import rebuilt serve their queries.
    assert [p.key for p in TxPlain.query.filter(owner="ops")] == ["p1"]
    assert [p.key for p in TxPlain.query.filter(tags__contains="x")] == ["p1"]
    assert [p.key for p in TxPlain.query.filter(rank__gte=2.0)] == ["p1"]
    assert TxPlain.query.get(key="p1").author.name == "ada"


def test_memory_fields_round_trip_into_a_fresh_schema(pg, pg_schema, admin):
    a = _seed_memory()
    text = export_records(TxMemory).data
    records = _records(text)
    state = next(r for r in records if r["key"] == a.db_key.redis_key)
    assert set(state["state"]) == {"certainty", "vector"}
    assert set(state["model_state"]["AccessTrackerMixin"]) == {
        "access_count",
        "last_accessed",
    }
    vector_before = admin.execute(
        f'SELECT _pk, vector__vec::text FROM "{pg_schema.name}".tx_memory '
        "ORDER BY _pk"
    ).fetchall()

    _fresh(pg, pg_schema)
    _import(TxMemory, text)
    assert [_normalize(r) for r in _records(export_records(TxMemory).data)] == [
        _normalize(r) for r in records
    ]
    restored = TxMemory.query.get(memory_id=a.memory_id)
    assert restored.access_count == 3
    assert restored.relevance == pytest.approx(a.relevance)
    assert ConfidenceField.export_state(restored, "certainty", None) == (
        state["state"]["certainty"]
    )
    # Carried vectors land in the record row and the narrow table alike.
    assert (
        admin.execute(
            f'SELECT _pk, vector__vec::text FROM "{pg_schema.name}".tx_memory '
            "ORDER BY _pk"
        ).fetchall()
        == vector_before
    )
    assert TxMemory.check_indexes()["total"] == 0
    # Derived state was rebuilt by the save: BM25 and the existence tokens.
    hits = BM25Field.search(TxMemory, "lexical", "maintenance")
    assert [k for k, _ in hits] == [a.db_key.redis_key]
    seen = TxMemory._meta.fields["seen"]
    assert seen.might_exist(TxMemory, "postgres index maintenance")


def test_history_state_round_trips_into_a_fresh_schema(pg, pg_schema, admin):
    _seed_history()
    text = export_records(TxHistory).data
    records = _records(text)
    old = next(r for r in records if r["key"].endswith(":old"))
    assert {"validity", "edges", "rhythm"} <= set(old["state"])
    assert "PredictionLedgerMixin" in old["model_state"]

    _fresh(pg, pg_schema)
    _import(TxHistory, text)
    assert [_normalize(r) for r in _records(export_records(TxHistory).data)] == [
        _normalize(r) for r in records
    ]
    restored_old = TxHistory.query.get(name="old")
    restored_new = TxHistory.query.get(name="new")
    chain = SupersessionProtocol.chain(restored_new)
    assert [m.name for m in chain] == ["old", "new"]
    edges = dict(
        admin.execute(
            f'SELECT dst, weight FROM "{pg_schema.name}".tx_history__edges__edge '
            "WHERE src = %s",
            (restored_old.db_key.redis_key,),
        ).fetchall()
    )
    assert edges == {
        restored_new.db_key.redis_key: pytest.approx(0.4),
        "TxHistory:never-exported": pytest.approx(0.7),
    }
    assert TxHistory.get_prediction_data(restored_old)["resolved"] is True


def test_key_regeneration_remaps_references_and_moves_carried_state(pg, pg_schema):
    a = _seed_memory()
    text = export_records(TxMemory).data
    _fresh(pg, pg_schema)
    report = _import(TxMemory, text, preserve_keys=False)
    new_key = report.key_map[a.db_key.redis_key]
    assert new_key != a.db_key.redis_key
    restored = TxMemory.query.get(redis_key=new_key)
    assert restored.access_count == 3
    state = ConfidenceField.export_state(restored, "certainty", None)
    assert state["evidence_count"] == 2
    assert EmbeddingField.export_state(restored, "vector", None) is not None


# -- cross-backend: a Redis export into Postgres --------------------------------------

#: What a Redis export carries that Postgres does not keep (documented in
#: docs/features/postgres-backend.md, "Transfer"), per family.
CROSS_BACKEND_DROPS = {
    "AccessTrackerMixin.access_log": "Postgres keeps no confirmed access log (§1.1)",
}


def _on_redis(seed, models):
    """Seed and export on the Redis backend; return the exports and the
    public reads, then bind Postgres back."""
    previous = set_backend("redis")
    try:
        for model in models:
            assert get_backend(model).name == "redis"
            model.delete_all()
        seed()
        # Non-vacuity: the records really are Redis hashes.
        for model in models:
            keys = [str(o.db_key.redis_key) for o in model.query.all()]
            assert keys and all(popoto.get_redis().exists(k) for k in keys)
        exports = {m.__name__: export_records(m).data for m in models}
        reads = {
            m.__name__: {str(o.db_key.redis_key): o.to_dict() for o in m.query.all()}
            for m in models
        }
        return exports, reads
    finally:
        for model in models:
            model.delete_all()
        set_backend(previous)


def _drop_documented(record):
    record = _normalize(record)
    access = (record.get("model_state") or {}).get("AccessTrackerMixin")
    if access:
        access.pop("access_log", None)
    return record


@pytest.mark.parametrize(
    "seed,models",
    [
        (_seed_plain, (TxAuthor, TxPlain)),
        (_seed_memory, (TxMemory,)),
        (_seed_history, (TxHistory,)),
    ],
    ids=["plain", "memory", "history"],
)
def test_a_redis_export_imports_into_postgres_and_reads_identically(pg, seed, models):
    exports, reads = _on_redis(seed, models)
    for model in models:
        _import(model, exports[model.__name__])
    for model in models:
        name = model.__name__
        got = [_drop_documented(r) for r in _records(export_records(model).data)]
        want = [_drop_documented(r) for r in _records(exports[name])]
        assert got == want, name
        assert {
            str(o.db_key.redis_key): o.to_dict() for o in model.query.all()
        } == reads[name]
        assert model.check_indexes()["total"] == 0
    if TxMemory in models:
        # The carried counters crossed (the confirmed log did not: §1.1).
        assert sorted(o.access_count for o in TxMemory.query.all()) == [0, 3]
        hits = BM25Field.search(TxMemory, "lexical", "maintenance")
        assert len(hits) == 1
