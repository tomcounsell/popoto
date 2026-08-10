"""Per-field export/import round-trip fidelity (issue #554, AC #2).

The headline is a single fixture model that stacks every hard field type at
once -- implicit ``AutoKeyField``, ``DecayingSortedField``, ``BM25Field``,
``EmbeddingField``, ``ConfidenceField``, ``ExistenceFilter``, and
``WriteFilterMixin`` -- and proves the whole stack survives a
export -> wipe -> import cycle, with every field either restoring its state
or being reported with its ``roundtrip_policy`` in ``ImportReport.fidelity``.

On top of that, each of the four failure classes named in #554 gets its own
adversarial test:

1. Identity -- the imported record keeps the source ``_auto_key`` / redis_key
   instead of minting a fresh UUID.
2. Learned state -- all four ``ConfidenceField`` companion-hash values survive,
   not just ``confidence``, despite ``on_save`` reseeding the hash with
   ``initial_confidence`` via HSETNX.
3. Time-derived state -- a two-year-old ``DecayingSortedField`` timestamp is
   still two years old after import, not restamped to ``now`` by ``auto_now``.
4. Learned amplitudes -- ``CyclicDecayField`` cycles and pressure age survive,
   which works only because ``import_state`` runs *after* ``save()``.

Plus the three protocol-level guarantees: the embedding vector carries
byte-for-byte and a wrong-vector-space import is a loud error; the model-level
mixin carriers reached by the driver's MRO walk (``AccessTrackerMixin``,
``EventStreamMixin``) participate; and the derived fields that emit nothing
(``BM25Field``, ``ExistenceFilter``, the ``WriteFilterMixin`` priority ZSET)
are genuinely rebuilt by ``on_save``.

Everything runs against live Redis. The only stub is the deterministic
embedding provider the existing embedding tests already use -- Redis itself is
never mocked.

Known pre-existing bug (deliberately NOT asserted here, deferred to #556):
``CyclicDecayField.on_save`` unconditionally overwrites learned per-member
cycle amplitudes with the class-level defaults, so any ordinary save discards
what ``strengthen_cycle`` / ``weaken_cycle`` accumulated. The import path only
survives it because carried state is restored after the save.
"""

import io
import json
import os
import shutil
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import msgpack  # noqa: E402
import pytest  # noqa: E402

np = pytest.importorskip("numpy")

from src import popoto  # noqa: E402
from src.popoto.embeddings import AbstractEmbeddingProvider  # noqa: E402
from src.popoto.exceptions import ModelException  # noqa: E402
from src.popoto.fields.access_tracker import AccessTrackerMixin  # noqa: E402
from src.popoto.fields.bm25_field import BM25Field  # noqa: E402
from src.popoto.fields.confidence_field import ConfidenceField  # noqa: E402
from src.popoto.fields.constants import TemporalPeriod  # noqa: E402
from src.popoto.fields.cyclic_decay_field import CyclicDecayField  # noqa: E402
from src.popoto.fields.decaying_sorted_field import (  # noqa: E402
    DecayingSortedField,
)
from src.popoto.fields.embedding_field import (  # noqa: E402
    EmbeddingField,
    _get_embeddings_dir,
    invalidate_cache,
    set_default_provider,
)
from src.popoto.fields.event_stream import EventStreamMixin  # noqa: E402
from src.popoto.fields.existence_filter import ExistenceFilter  # noqa: E402
from src.popoto.fields.write_filter import WriteFilterMixin  # noqa: E402
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402
from src.popoto.transfer import export_records, import_records  # noqa: E402

EMBEDDING_DIM = 4
STREAM_NAME = "test_transfer_fidelity_mutations"


# --- Deterministic embedding provider (no Redis is mocked) ---------------


class StubProvider(AbstractEmbeddingProvider):
    """Deterministic provider: the same text always yields the same vector."""

    _model_name = "stub-fidelity-v1"

    def __init__(self, dim=EMBEDDING_DIM):
        self._dim = dim
        self.call_count = 0

    def embed(self, texts, input_type=None):
        self.call_count += 1
        vectors = []
        for text in texts:
            seed = sum(ord(c) for c in text) % 1000
            rng = np.random.RandomState(seed)
            vectors.append(rng.randn(self._dim).tolist())
        return vectors

    @property
    def dimensions(self):
        return self._dim

    @property
    def max_batch_size(self):
        return 32


# --- Test models ---------------------------------------------------------


class StackedDoc(WriteFilterMixin, popoto.Model):
    """AC #2: every hard field type stacked on one model.

    No KeyField is declared, so the metaclass generates an implicit
    ``_auto_key`` (``AutoKeyField``) -- the harder identity path, because
    ``to_dict()`` iterates ``explicit_fields`` and omits it entirely.
    """

    title = popoto.StringField(default="")
    importance = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField()
    certainty = ConfidenceField(initial_confidence=0.5)
    keywords = BM25Field(source="title")
    vector = EmbeddingField(source="title")
    seen = ExistenceFilter(
        error_rate=0.01,
        capacity=1000,
        fingerprint_fn=lambda instance: instance.title or "",
    )

    def compute_filter_score(self):
        return self.importance or 0.0


class CyclicDoc(popoto.Model):
    """CyclicDecayField carrier: learned amplitudes plus pressure age."""

    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.WEEKLY, 2.0, 0.0)],
        pressure_rate=0.1,
    )


class TrackedDoc(AccessTrackerMixin, EventStreamMixin, popoto.Model):
    """Model-level mixin carriers, reached only by the driver's MRO walk."""

    _stream_name = STREAM_NAME

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")


ALL_MODELS = (StackedDoc, CyclicDoc, TrackedDoc)


# --- Helpers -------------------------------------------------------------


def _wipe_model(model_class):
    """Delete every trace of a model: records, indexes, and companion keys.

    ``delete_all()`` handles records and indexes, but several auxiliary
    structures are deliberately never cleaned by ``on_delete`` (the
    ``ExistenceFilter`` bit array, the ``WriteFilterMixin`` priority ZSET, the
    access-tracker meta hash). They are removed here so that anything present
    after an import is proof of restore-or-rebuild rather than a leftover.
    """
    model_class.delete_all()
    for key in POPOTO_REDIS_DB.scan_iter(match=f"*{model_class.__name__}*"):
        POPOTO_REDIS_DB.delete(key)
    shutil.rmtree(
        os.path.join(_get_embeddings_dir(), model_class.__name__),
        ignore_errors=True,
    )
    invalidate_cache(model_class.__name__)


def _wipe_all():
    for model_class in ALL_MODELS:
        _wipe_model(model_class)
    POPOTO_REDIS_DB.delete(f"stream:{STREAM_NAME}")


@pytest.fixture(autouse=True)
def transfer_env(tmp_path):
    """Live Redis, a temp embedding directory, and a deterministic provider."""
    provider = StubProvider()
    set_default_provider(provider)
    os.environ["POPOTO_CONTENT_PATH"] = str(tmp_path / "content")

    _wipe_all()
    yield provider
    _wipe_all()

    set_default_provider(None)
    invalidate_cache()
    os.environ.pop("POPOTO_CONTENT_PATH", None)


def _export_text(model_class, **filters):
    result = export_records(model_class, **filters)
    assert result.data is not None
    return result


def _import_text(model_class, text, **kwargs):
    return import_records(model_class, io.StringIO(text), **kwargs)


def _parse(text):
    """Split JSONL into (manifest, [records])."""
    lines = [json.loads(line) for line in text.strip().split("\n")]
    return lines[0], lines[1:]


def _render(manifest, records):
    return "\n".join(json.dumps(obj) for obj in [manifest] + records) + "\n"


def _npy_bytes(vector):
    buffer = io.BytesIO()
    np.save(buffer, np.asarray(vector, dtype=np.float32))
    return buffer.getvalue()


def _load_vector(model_class, redis_key):
    path = EmbeddingField._embedding_path(model_class.__name__, redis_key)
    return np.load(path)


def _make_stacked(title="kubernetes deployment guide", importance=0.9, age_days=0):
    """Create and save one StackedDoc, optionally with an aged decay clock."""
    doc = StackedDoc(title=title, importance=importance)
    if age_days:
        doc.relevance = time.time() - age_days * 86400
        doc.save(skip_auto_now=True)
    else:
        doc.save()
    return doc


# --- AC #2: the whole stack ---------------------------------------------


class TestStackedModelRoundTrip:
    """The seven-field stacked fixture model round-trips as a whole."""

    def test_full_stack_round_trips(self):
        first = _make_stacked(title="kubernetes deployment guide", importance=0.9)
        second = _make_stacked(title="redis caching strategies", importance=0.6)
        ConfidenceField.update_confidence(first, "certainty", 0.9)
        source_keys = {first.db_key.redis_key, second.db_key.redis_key}

        exported = _export_text(StackedDoc)
        assert exported.record_count == 2

        _wipe_model(StackedDoc)
        assert StackedDoc.query.count() == 0

        report = _import_text(StackedDoc, exported.data)
        assert report.total == 2
        assert report.count("landed") == 2, report.summary()
        assert report.count("partial") == 0
        assert report.count("rejected") == 0
        assert report.count("errored") == 0

        restored = list(StackedDoc.query.all())
        assert {doc.db_key.redis_key for doc in restored} == source_keys
        by_title = {doc.title: doc for doc in restored}
        assert set(by_title) == {
            "kubernetes deployment guide",
            "redis caching strategies",
        }
        assert by_title["kubernetes deployment guide"].importance == pytest.approx(0.9)
        assert by_title["redis caching strategies"].importance == pytest.approx(0.6)

    def test_every_field_is_reported_with_a_policy(self):
        _make_stacked()
        exported = _export_text(StackedDoc)
        _wipe_model(StackedDoc)
        report = _import_text(StackedDoc, exported.data)

        missing = set(StackedDoc._meta.fields) - set(report.fidelity)
        assert not missing, f"fields absent from the fidelity roll-up: {missing}"

        for name, info in report.fidelity.items():
            policy = info.get("policy")
            assert policy in ("rebuild", "carry", "partial"), (name, info)
            if policy == "partial":
                assert info.get("note"), f"{name} is 'partial' with no roundtrip_note"

    def test_carrier_fields_actually_emit_state(self):
        """A field declaring 'carry' must put something on the record line."""
        doc = _make_stacked()
        ConfidenceField.update_confidence(doc, "certainty", 0.9)

        exported = _export_text(StackedDoc)
        manifest, records = _parse(exported.data)
        assert len(records) == 1

        carriers = {
            name
            for name, info in manifest["fields"].items()
            if info.get("policy") == "carry"
        }
        assert carriers, "the stacked model declares no 'carry' fields"
        assert carriers <= set(records[0]["state"]), (
            f"carry fields with no exported state: "
            f"{carriers - set(records[0]['state'])}"
        )

    def test_write_filter_mixin_is_reported_as_rebuild(self):
        _make_stacked()
        exported = _export_text(StackedDoc)
        _wipe_model(StackedDoc)
        report = _import_text(StackedDoc, exported.data)

        assert "WriteFilterMixin" in report.fidelity
        assert report.fidelity["WriteFilterMixin"]["policy"] == "rebuild"


# --- Failure class 1: identity ------------------------------------------


class TestIdentityPreserved:
    """A naive to_dict() -> save() mints a fresh UUID. Import must not."""

    def test_auto_key_and_redis_key_are_preserved(self):
        doc = _make_stacked()
        source_auto_key = doc._auto_key
        source_redis_key = doc.db_key.redis_key
        assert source_auto_key

        exported = _export_text(StackedDoc)
        _wipe_model(StackedDoc)
        report = _import_text(StackedDoc, exported.data)
        assert report.count("landed") == 1, report.summary()

        restored = list(StackedDoc.query.all())
        assert len(restored) == 1
        assert restored[0]._auto_key == source_auto_key
        assert restored[0].db_key.redis_key == source_redis_key
        assert POPOTO_REDIS_DB.exists(source_redis_key)

    def test_implicit_auto_key_is_in_the_exported_values(self):
        """to_dict() omits the implicit key; export must add it back."""
        doc = _make_stacked()
        assert "_auto_key" not in doc.to_dict()

        exported = _export_text(StackedDoc)
        _manifest, records = _parse(exported.data)
        assert records[0]["values"]["_auto_key"] == doc._auto_key


# --- Failure class 2: learned state -------------------------------------


class TestConfidenceStatePreserved:
    """ConfidenceField.on_save reseeds via HSETNX; all four values must live."""

    SIGNALS = (0.9, 0.95, 0.85, 0.2, 0.1)  # 3 corroborations, 2 contradictions

    def _drive_confidence(self, doc):
        for signal in self.SIGNALS:
            ConfidenceField.update_confidence(doc, "certainty", signal)
        return ConfidenceField.get_confidence_data(doc, "certainty")

    def test_all_four_confidence_values_survive(self):
        doc = _make_stacked()
        before = self._drive_confidence(doc)
        redis_key = doc.db_key.redis_key

        initial = StackedDoc._meta.fields["certainty"].initial_confidence
        assert before["evidence_count"] == len(self.SIGNALS)
        assert before["corroborations"] == 3
        assert before["contradictions"] == 2
        assert abs(before["confidence"] - initial) > 0.05, (
            "the fixture failed to drive confidence away from initial_confidence, "
            "so this test could not detect a reset-to-prior regression"
        )

        exported = _export_text(StackedDoc)
        _wipe_model(StackedDoc)
        report = _import_text(StackedDoc, exported.data)
        assert report.count("landed") == 1, report.summary()

        restored = list(StackedDoc.query.all())[0]
        assert restored.db_key.redis_key == redis_key
        after = ConfidenceField.get_confidence_data(restored, "certainty")

        assert after["confidence"] == pytest.approx(before["confidence"], abs=1e-9)
        assert after["evidence_count"] == before["evidence_count"]
        assert after["corroborations"] == before["corroborations"]
        assert after["contradictions"] == before["contradictions"]

    def test_restored_confidence_is_not_the_prior(self):
        """Guards the test above: a reset-to-prior regression must fail loudly."""
        doc = _make_stacked()
        self._drive_confidence(doc)

        exported = _export_text(StackedDoc)
        _wipe_model(StackedDoc)
        _import_text(StackedDoc, exported.data)

        restored = list(StackedDoc.query.all())[0]
        initial = StackedDoc._meta.fields["certainty"].initial_confidence
        after = ConfidenceField.get_confidence_data(restored, "certainty")
        assert abs(after["confidence"] - initial) > 0.05
        assert after["evidence_count"] != 0


# --- Failure class 3: time-derived state --------------------------------


class TestDecayTimestampPreserved:
    """auto_now would restamp the decay clock and silently reorder queries."""

    def test_two_year_old_timestamp_stays_old(self):
        doc = _make_stacked(age_days=730)
        source_ts = doc.relevance
        now = time.time()
        assert source_ts < now - 700 * 86400

        exported = _export_text(StackedDoc)
        _wipe_model(StackedDoc)
        report = _import_text(StackedDoc, exported.data)
        assert report.count("landed") == 1, report.summary()

        restored = list(StackedDoc.query.all())[0]
        assert restored.relevance == pytest.approx(source_ts, abs=5.0)
        assert restored.relevance < time.time() - 365 * 86400, (
            "the decay reference timestamp was restamped to ~now on import; "
            "every decay-ordered query would silently reorder"
        )

    def test_sorted_set_score_matches_the_old_timestamp(self):
        doc = _make_stacked(age_days=730)
        source_ts = doc.relevance

        exported = _export_text(StackedDoc)
        _wipe_model(StackedDoc)
        _import_text(StackedDoc, exported.data)

        restored = list(StackedDoc.query.all())[0]
        sortedset_key = DecayingSortedField.get_sortedset_db_key(
            StackedDoc, "relevance"
        ).redis_key
        score = POPOTO_REDIS_DB.zscore(sortedset_key, restored.db_key.redis_key)
        assert score is not None
        assert score == pytest.approx(source_ts, abs=5.0)


# --- Failure class 4: CyclicDecayField ----------------------------------


class TestCyclicDecayStatePreserved:
    """Learned amplitudes and accumulated pressure age survive the trip.

    This only works because the driver calls ``import_state`` AFTER
    ``save()``: ``CyclicDecayField.on_save`` unconditionally rewrites the
    cycles hash with the class-level defaults. The ordering is what this test
    ultimately pins down.
    """

    PAST = 1_600_000_000.0  # a fixed, unambiguously old last_resolved

    def _cycles(self, doc):
        field = CyclicDoc._meta.fields["relevance"]
        raw = POPOTO_REDIS_DB.hget(
            field.get_cycles_hash_key(doc, "relevance"), doc.db_key.redis_key
        )
        return msgpack.unpackb(raw, raw=False) if raw else None

    def _pressure(self, doc):
        field = CyclicDoc._meta.fields["relevance"]
        raw = POPOTO_REDIS_DB.hget(
            field.get_pressure_hash_key(doc, "relevance"), doc.db_key.redis_key
        )
        return msgpack.unpackb(raw, raw=False) if raw else None

    def _seed_pressure(self, doc, last_resolved):
        field = CyclicDoc._meta.fields["relevance"]
        POPOTO_REDIS_DB.hset(
            field.get_pressure_hash_key(doc, "relevance"),
            doc.db_key.redis_key,
            msgpack.packb(
                {"rate": field.pressure_rate, "last_resolved": last_resolved}
            ),
        )

    def test_learned_amplitude_and_pressure_age_survive(self):
        doc = CyclicDoc(name="cyc1")
        doc.save()

        default_amplitude = CyclicDoc._meta.fields["relevance"].cycles[0][1]
        doc.strengthen_cycle("relevance", factor=1.5)
        doc.weaken_cycle("relevance", factor=1.25)
        self._seed_pressure(doc, self.PAST)

        learned = self._cycles(doc)
        assert learned is not None
        learned_amplitude = learned[0][1]
        assert abs(learned_amplitude - default_amplitude) > 1e-6, (
            "the fixture failed to move the amplitude off the class default, "
            "so a clobber regression would be undetectable"
        )

        exported = _export_text(CyclicDoc)
        _wipe_model(CyclicDoc)
        report = _import_text(CyclicDoc, exported.data)
        assert report.count("landed") == 1, report.summary()

        restored = CyclicDoc.query.get(name="cyc1")
        assert restored is not None

        after_cycles = self._cycles(restored)
        assert after_cycles is not None
        assert after_cycles[0][1] == pytest.approx(learned_amplitude)
        assert after_cycles[0][1] != pytest.approx(default_amplitude)

        after_pressure = self._pressure(restored)
        assert after_pressure is not None
        assert after_pressure["last_resolved"] == pytest.approx(self.PAST, abs=1e-6)
        assert after_pressure["last_resolved"] < time.time() - 86400

    def test_cyclic_field_declares_carry(self):
        doc = CyclicDoc(name="cyc2")
        doc.save()
        self._seed_pressure(doc, self.PAST)

        exported = _export_text(CyclicDoc)
        manifest, records = _parse(exported.data)
        assert manifest["fields"]["relevance"]["policy"] == "carry"
        assert "relevance" in records[0]["state"]


# --- EmbeddingField: vector carry + provenance fingerprint ---------------


class TestEmbeddingCarryAndProvenance:
    """Carrying blind is the corruption; the fingerprint is the guard."""

    def _doctored(self, exported_text, provider_name="OtherProvider", vector=None):
        """Rewrite the manifest provenance and the carried vector payload."""
        import base64

        manifest, records = _parse(exported_text)
        manifest["embedding_provenance"]["vector"]["provider"] = provider_name
        if vector is not None:
            encoded = base64.b64encode(_npy_bytes(vector)).decode("ascii")
            records[0]["state"]["vector"]["vector_npy_b64"] = encoded
        return _render(manifest, records)

    def test_vector_survives_byte_for_byte(self):
        doc = _make_stacked(title="vectors carry verbatim")
        redis_key = doc.db_key.redis_key
        before = _load_vector(StackedDoc, redis_key)
        assert before.shape == (EMBEDDING_DIM,)

        exported = _export_text(StackedDoc)
        _wipe_model(StackedDoc)
        assert not os.path.exists(
            EmbeddingField._embedding_path("StackedDoc", redis_key)
        )

        report = _import_text(StackedDoc, exported.data)
        assert report.count("landed") == 1, report.summary()

        after = _load_vector(StackedDoc, redis_key)
        assert np.array_equal(before, after)

    def test_manifest_carries_provider_provenance(self):
        _make_stacked()
        exported = _export_text(StackedDoc)
        manifest, _records = _parse(exported.data)

        provenance = manifest["embedding_provenance"]["vector"]
        assert set(provenance) == {"provider", "model", "dimensions"}
        assert provenance["provider"] == "StubProvider"
        assert provenance["dimensions"] == EMBEDDING_DIM

    def test_mismatched_provenance_raises_by_default(self):
        _make_stacked()
        exported = _export_text(StackedDoc)
        doctored = self._doctored(exported.data)
        _wipe_model(StackedDoc)

        with pytest.raises(ModelException) as excinfo:
            _import_text(StackedDoc, doctored)
        message = str(excinfo.value)
        assert "provenance" in message
        assert "vector" in message
        assert StackedDoc.query.count() == 0, "nothing may be written before the raise"

    def test_carry_imports_the_mismatched_vector_anyway(self):
        doc = _make_stacked()
        redis_key = doc.db_key.redis_key
        marker = [7.0, 7.0, 7.0, 7.0]
        doctored = self._doctored(_export_text(StackedDoc).data, vector=marker)
        _wipe_model(StackedDoc)

        report = _import_text(StackedDoc, doctored, on_embedding_mismatch="carry")
        assert report.count("landed") == 1, report.summary()
        assert any("provenance" in w for w in report.warnings)

        restored = _load_vector(StackedDoc, redis_key)
        assert np.allclose(restored, np.asarray(marker, dtype=np.float32))

    def test_regenerate_drops_the_carried_vector(self):
        doc = _make_stacked(title="regenerate me")
        redis_key = doc.db_key.redis_key
        original = _load_vector(StackedDoc, redis_key).copy()
        marker = [7.0, 7.0, 7.0, 7.0]
        doctored = self._doctored(_export_text(StackedDoc).data, vector=marker)
        _wipe_model(StackedDoc)

        report = _import_text(StackedDoc, doctored, on_embedding_mismatch="regenerate")
        assert report.count("landed") == 1, report.summary()
        assert any("re-embedding" in w for w in report.warnings)

        restored = _load_vector(StackedDoc, redis_key)
        assert not np.allclose(restored, np.asarray(marker, dtype=np.float32)), (
            "the carried vector was written despite on_embedding_mismatch="
            "'regenerate'"
        )
        assert np.allclose(restored, original)


# --- Model-level mixin carriers (critique BLOCKER 1) ---------------------


class TestModelLevelMixinCarriers:
    """AccessTrackerMixin is not a Field; only the MRO walk reaches it."""

    def _make_tracked(self, name="tracked1", reads=3):
        doc = TrackedDoc(name=name, content="tracked content")
        doc.save()
        for _ in range(reads):
            doc.on_read()
        doc.confirm_access()
        return doc

    def test_access_counters_survive(self):
        doc = self._make_tracked(reads=3)
        before_count = doc.access_count
        before_last = doc.last_accessed
        assert before_count == 3
        assert before_last is not None

        exported = _export_text(TrackedDoc)
        _manifest, records = _parse(exported.data)
        assert records[0]["model_state"]["AccessTrackerMixin"]["access_count"] == 3

        _wipe_model(TrackedDoc)
        assert not POPOTO_REDIS_DB.exists(doc._at_key("meta"))

        report = _import_text(TrackedDoc, exported.data)
        assert report.count("landed") == 1, report.summary()

        restored = TrackedDoc.query.get(name="tracked1")
        assert restored.access_count == before_count
        assert restored.last_accessed == pytest.approx(before_last, abs=1e-6)

    def test_partial_model_mixin_appears_in_the_fidelity_report(self):
        self._make_tracked(name="tracked2")
        exported = _export_text(TrackedDoc)
        _wipe_model(TrackedDoc)
        report = _import_text(TrackedDoc, exported.data)

        assert "EventStreamMixin" in report.fidelity, (
            "a 'partial' model-level mixin must reach the report; this line was "
            "unreachable before the MRO walk existed"
        )
        entry = report.fidelity["EventStreamMixin"]
        assert entry["policy"] == "partial"
        assert entry["note"]
        assert "EventStreamMixin" in report.summary()
        assert entry["note"] in report.summary()

    def test_access_tracker_declares_partial_with_a_note(self):
        self._make_tracked(name="tracked3")
        exported = _export_text(TrackedDoc)
        _wipe_model(TrackedDoc)
        report = _import_text(TrackedDoc, exported.data)

        entry = report.fidelity["AccessTrackerMixin"]
        assert entry["policy"] == "partial"
        assert entry["note"]


# --- Derived fields rebuild for free ------------------------------------


class TestDerivedFieldsRebuildOnImport:
    """BM25Field / ExistenceFilter / priority ZSET emit nothing but must work."""

    def test_bm25_search_finds_the_imported_record(self):
        doc = _make_stacked(title="kubernetes deployment guide")
        redis_key = doc.db_key.redis_key

        exported = _export_text(StackedDoc)
        _wipe_model(StackedDoc)
        assert BM25Field.search(StackedDoc, "keywords", "kubernetes") == []

        report = _import_text(StackedDoc, exported.data)
        assert report.count("landed") == 1, report.summary()

        results = BM25Field.search(StackedDoc, "keywords", "kubernetes", limit=10)
        assert [key for key, _score in results] == [redis_key]
        assert all(score > 0 for _key, score in results)
        assert report.fidelity["keywords"]["policy"] == "rebuild"

    def test_existence_filter_reports_membership_after_import(self):
        _make_stacked(title="kubernetes deployment guide")

        exported = _export_text(StackedDoc)
        _wipe_model(StackedDoc)
        assert StackedDoc.seen.definitely_missing(StackedDoc, "kubernetes")

        report = _import_text(StackedDoc, exported.data)
        assert report.count("landed") == 1, report.summary()

        assert StackedDoc.seen.might_exist(StackedDoc, "kubernetes")
        assert StackedDoc.seen.definitely_missing(StackedDoc, "zzzabsenttoken")

    def test_write_filter_priority_zset_is_rebuilt(self):
        doc = _make_stacked(title="critical runbook", importance=0.95)
        redis_key = doc.db_key.redis_key
        priority_key = doc._wf_key("priority")
        assert POPOTO_REDIS_DB.zscore(priority_key, redis_key) is not None

        exported = _export_text(StackedDoc)
        _wipe_model(StackedDoc)
        assert POPOTO_REDIS_DB.zscore(priority_key, redis_key) is None

        report = _import_text(StackedDoc, exported.data)
        assert report.count("landed") == 1, report.summary()
        assert POPOTO_REDIS_DB.zscore(priority_key, redis_key) == pytest.approx(0.95)
