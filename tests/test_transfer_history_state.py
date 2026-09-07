"""Round-trip tests for history-shaped auxiliary state (#556).

#554 shipped the ``export_state`` / ``import_state`` protocol and left six
subsystems declaring ``roundtrip_policy = "partial"`` with a note pointing at
this issue. #556 decided each one on a single rule: carry a structure when the
exported bytes are a *fact about the record*; refuse to carry one that is a
property of the *source deployment's history*, which the destination never
lived through.

Three now carry (PredictionLedger entry, CoOccurrence edges, the confirmed
access log). Three are permanent limitations (the mutation stream, the Bloom
bit array, the Count-Min counters) -- and the tests for those assert the
*contract*, not an implementation, because a carry there would fabricate
history.

Everything runs against live Redis, isolated by the popoto pytest plugin.
No mocks.
"""

import io
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402

from src import popoto  # noqa: E402
from src.popoto.fields.access_tracker import AccessTrackerMixin  # noqa: E402
from src.popoto.fields.co_occurrence_field import CoOccurrenceField  # noqa: E402
from src.popoto.fields.constants import Defaults  # noqa: E402
from src.popoto.fields.event_stream import EventStreamMixin  # noqa: E402
from src.popoto.fields.existence_filter import (  # noqa: E402
    ExistenceFilter,
    FrequencySketch,
)
from src.popoto.fields.prediction_ledger import PredictionLedgerMixin  # noqa: E402
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402
from src.popoto.transfer import export_records, import_records  # noqa: E402

# --- Test models ---


class HistPrediction(PredictionLedgerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")


class HistAssoc(popoto.Model):
    name = popoto.UniqueKeyField()
    edges = CoOccurrenceField(symmetric=False, max_edges=500)


class HistAssocSmall(popoto.Model):
    """Same shape, but a destination whose ``max_edges`` is deliberately tiny."""

    name = popoto.UniqueKeyField()
    edges = CoOccurrenceField(symmetric=False, max_edges=2)


class HistAccess(AccessTrackerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")


def _wipe(model_class):
    for instance in model_class.query.all():
        instance.delete()


def _roundtrip(model_class, destination=None, also_delete=()):
    """Export, wipe the source, then import -- returning the import report.

    ``also_delete`` names Redis keys that ``Model.delete()`` does *not* clean
    up. Without it a round-trip test in a single database is vacuous: the
    source's structure is still sitting there and the assertion passes whether
    or not anything was actually carried. ``PredictionLedgerMixin`` is the
    live case -- ``delete()`` leaves ``$PL:...:meta:{pk}`` and the record's
    member in the errors sorted set behind.
    """
    data = export_records(model_class).data
    _wipe(model_class)
    for key in also_delete:
        POPOTO_REDIS_DB.delete(key)
    return import_records(destination or model_class, io.StringIO(data))


# ---------------------------------------------------------------------------
# PredictionLedger: carried
# ---------------------------------------------------------------------------


class TestPredictionLedgerCarry:
    def setup_method(self):
        self._clear()

    def teardown_method(self):
        self._clear()

    @staticmethod
    def _clear():
        _wipe(HistPrediction)
        # delete() does NOT clean these -- see _roundtrip's docstring.
        for key in POPOTO_REDIS_DB.scan_iter(match="$PL:HistPrediction:*"):
            POPOTO_REDIS_DB.delete(key)

    @staticmethod
    def _leftovers(item):
        return [
            HistPrediction._meta_key(item),
            HistPrediction._error_key(HistPrediction, "default"),
        ]

    def test_unresolved_prediction_carries_with_its_original_recorded_at(self):
        item = HistPrediction.create(name="p1", content="x")
        HistPrediction.record_prediction(item, {"score": 0.4})
        before = HistPrediction.get_prediction_data(item)
        assert before is not None
        assert before["resolved"] is False

        _roundtrip(HistPrediction, also_delete=self._leftovers(item))

        restored = HistPrediction.query.get(name="p1")
        after = HistPrediction.get_prediction_data(restored)
        assert after is not None, "the ledger entry did not survive the round trip"
        assert after["predicted"] == before["predicted"]
        assert after["resolved"] is False
        # The point of a raw restore: the source's clock, not the import's.
        assert after["recorded_at"] == before["recorded_at"]

    def test_resolved_prediction_carries_error_and_timestamps_without_rerunning(self):
        item = HistPrediction.create(name="p2", content="x")
        HistPrediction.record_prediction(item, {"score": 0.0})
        HistPrediction.resolve_prediction(item, {"score": 1.0})
        before = HistPrediction.get_prediction_data(item)
        assert before["resolved"] is True
        assert before["prediction_error"] is not None

        _roundtrip(HistPrediction, also_delete=self._leftovers(item))

        restored = HistPrediction.query.get(name="p2")
        after = HistPrediction.get_prediction_data(restored)
        assert after is not None
        assert after["resolved"] is True
        # Re-resolution would stamp a fresh resolved_at and recompute the
        # error against the destination's data. Neither may happen.
        assert after["resolved_at"] == before["resolved_at"]
        assert after["prediction_error"] == before["prediction_error"]
        assert after["recorded_at"] == before["recorded_at"]

    def test_resolved_prediction_restores_its_error_sorted_set_member(self):
        item = HistPrediction.create(name="p3", content="x")
        HistPrediction.record_prediction(item, {"score": 0.0})
        HistPrediction.resolve_prediction(item, {"score": 1.0})
        member = item.db_key.redis_key
        error_key = HistPrediction._error_key(HistPrediction, "default")
        expected = POPOTO_REDIS_DB.zscore(error_key, member)
        assert expected is not None

        # also_delete removes the whole errors sorted set before the import,
        # so a score afterwards can only have come from import_state.
        _roundtrip(HistPrediction, also_delete=self._leftovers(item))

        # The score is a pure function of the carried entry (abs of the
        # carried prediction_error), so re-deriving it is derivation, not
        # fabrication.
        assert POPOTO_REDIS_DB.zscore(error_key, member) == pytest.approx(expected)

    def test_a_record_with_no_prediction_round_trips_without_inventing_one(self):
        item = HistPrediction.create(name="p4", content="x")
        assert HistPrediction.export_state(item) is None

        _roundtrip(HistPrediction, also_delete=self._leftovers(item))

        restored = HistPrediction.query.get(name="p4")
        assert HistPrediction.get_prediction_data(restored) is None


# ---------------------------------------------------------------------------
# CoOccurrence: carried
# ---------------------------------------------------------------------------


class TestCoOccurrenceCarry:
    def setup_method(self):
        _wipe(HistAssoc)
        _wipe(HistAssocSmall)

    def teardown_method(self):
        _wipe(HistAssoc)
        _wipe(HistAssocSmall)

    def _edges_of(self, model_class, instance):
        field = model_class._meta.fields["edges"]
        raw = POPOTO_REDIS_DB.zrange(
            field.get_edge_key(model_class, instance.db_key.redis_key),
            0,
            -1,
            withscores=True,
        )
        return {
            (m.decode("utf-8") if isinstance(m, bytes) else str(m)): float(s)
            for m, s in raw
        }

    def test_edge_weights_carry_verbatim(self):
        source = HistAssoc.create(name="a")
        field = HistAssoc._meta.fields["edges"]
        pk = source.db_key.redis_key
        field.link(HistAssoc, pk, "target-1", initial_weight=0.3)
        field.link(HistAssoc, pk, "target-2", initial_weight=0.7)
        before = self._edges_of(HistAssoc, source)
        assert before, "fixture produced no edges"

        _roundtrip(HistAssoc)

        restored = HistAssoc.query.get(name="a")
        after = self._edges_of(HistAssoc, restored)
        assert after == pytest.approx(before)

    def test_import_replaces_the_destination_edge_set_wholesale(self):
        source = HistAssoc.create(name="b")
        field = HistAssoc._meta.fields["edges"]
        pk = source.db_key.redis_key
        field.link(HistAssoc, pk, "carried", initial_weight=0.5)
        data = export_records(HistAssoc).data

        # Give the destination a *different* edge the export knows nothing
        # about, then import over it.
        _wipe(HistAssoc)
        dest = HistAssoc.create(name="b")
        field.link(HistAssoc, dest.db_key.redis_key, "pre-existing", 0.9)

        import_records(HistAssoc, io.StringIO(data), on_conflict="overwrite")

        after = self._edges_of(HistAssoc, HistAssoc.query.get(name="b"))
        assert "carried" in after
        assert "pre-existing" not in after, (
            "import must replace the edge set, not merge into it -- a merge "
            "would silently invent an association history"
        )

    def test_edges_are_truncated_to_the_destination_max_edges(self):
        source = HistAssoc.create(name="c")
        field = HistAssoc._meta.fields["edges"]
        pk = source.db_key.redis_key
        for i, weight in enumerate((0.1, 0.2, 0.5, 0.9)):
            field.link(HistAssoc, pk, f"t{i}", initial_weight=weight)

        state = HistAssoc._meta.fields["edges"].export_state(source, "edges", None)
        assert state is not None and len(state["edges"]) == 4

        dest = HistAssocSmall.create(name="c")
        HistAssocSmall._meta.fields["edges"].import_state(dest, "edges", state)

        after = self._edges_of(HistAssocSmall, dest)
        assert len(after) == 2, "must respect the destination's max_edges"
        # Truncation keeps the heaviest edges, matching the Lua prune.
        assert set(after) == {"t3", "t2"}

    def test_an_edge_to_a_record_outside_the_export_still_lands(self):
        """A dangling edge is the expected result of a filtered export.

        Each record carries its own edge set, so exporting one endpoint and
        not the other yields a one-directional edge pointing at a PK the
        destination does not hold. That must land as written and must not
        break the BFS -- ``propagate`` walks to a missing node and stops.
        """
        source = HistAssoc.create(name="e")
        field = HistAssoc._meta.fields["edges"]
        pk = source.db_key.redis_key
        field.link(HistAssoc, pk, "HistAssoc:never-exported", initial_weight=0.4)

        _roundtrip(HistAssoc)

        restored = HistAssoc.query.get(name="e")
        after = self._edges_of(HistAssoc, restored)
        assert "HistAssoc:never-exported" in after
        assert after["HistAssoc:never-exported"] == pytest.approx(0.4)

        scores = field.propagate(
            HistAssoc, [restored.db_key.redis_key], depth=2, decay_per_hop=0.5
        )
        assert "HistAssoc:never-exported" in scores

    def test_carried_weights_are_clamped_to_the_weight_cap(self):
        dest = HistAssoc.create(name="d")
        cap = Defaults.CO_OCCURRENCE_WEIGHT_CAP
        HistAssoc._meta.fields["edges"].import_state(
            dest, "edges", {"edges": {"hot": cap + 5.0}, "max_edges": 500}
        )
        after = self._edges_of(HistAssoc, dest)
        assert after["hot"] == pytest.approx(cap)


# ---------------------------------------------------------------------------
# AccessTracker: counters + confirmed log carried, staged reads dropped
# ---------------------------------------------------------------------------


class TestAccessTrackerCarry:
    def setup_method(self):
        _wipe(HistAccess)

    def teardown_method(self):
        _wipe(HistAccess)

    def _log_of(self, instance):
        return [
            float(ts)
            for ts in POPOTO_REDIS_DB.lrange(instance._at_key("access_log"), 0, -1)
        ]

    def test_confirmed_log_carries_alongside_the_counters(self):
        item = HistAccess.create(name="a1", content="x")
        for _ in range(3):
            item.on_read()
            time.sleep(0.001)
        assert item.confirm_access() == 3
        before_log = self._log_of(item)
        before_count = item.access_count
        assert len(before_log) == 3

        data = export_records(HistAccess).data
        log_key = item._at_key("access_log")
        _wipe(HistAccess)
        # Non-vacuity guard: delete() clears the tracker keys, so anything
        # observed after the import was genuinely restored from the payload.
        assert POPOTO_REDIS_DB.llen(log_key) == 0
        import_records(HistAccess, io.StringIO(data))

        restored = HistAccess.query.get(name="a1")
        assert restored.access_count == before_count
        assert self._log_of(restored) == pytest.approx(before_log)

    def test_staged_reads_are_not_carried(self):
        item = HistAccess.create(name="a2", content="x")
        item.on_read()  # staged, never confirmed
        assert POPOTO_REDIS_DB.llen(item._at_key("staged")) == 1

        # Asserted on the exported payload, not on the destination's staged
        # key: _track_reads is on, so any query the assertion itself runs
        # stages a fresh read and would mask the answer.
        state = HistAccess.export_state(item)
        assert state is None, (
            "an unconfirmed read must contribute nothing to the export -- "
            "dropping it is equivalent to discard_staged_access()"
        )

        _roundtrip(HistAccess)
        assert HistAccess.query.get(name="a2").access_count == 0

    def test_state_without_an_access_log_imports_cleanly(self):
        # The shape #554-era code exported: counters only, no access_log key.
        item = HistAccess.create(name="a3", content="x")
        HistAccess.import_state(item, {"access_count": 7, "last_accessed": 1234.5})
        assert item.access_count == 7
        assert self._log_of(item) == []

    def test_a_carried_log_is_trimmed_to_the_destination_cap(self):
        item = HistAccess.create(name="a4", content="x")
        cap = HistAccess._max_access_log
        timestamps = [float(i) for i in range(cap + 5)]
        HistAccess.import_state(item, {"access_log": timestamps})

        after = self._log_of(item)
        assert len(after) == cap
        # The trim keeps the most recent entries, matching CONFIRM_ACCESS_LUA.
        assert after == pytest.approx(timestamps[-cap:])


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


class TestExportWireShape:
    """The regression guard for the decode requirement.

    ``to_jsonable`` routes a dict with ``bytes`` keys through a tagged
    ``__dictpairs__`` / ``__bytes__`` encoding. That round-trips correctly, so a
    carrier that forgets to decode Redis's raw replies still passes every
    behavioral test above while silently emitting a different wire shape -- one
    no non-popoto reader can parse. Only an assertion on the JSONL itself
    catches it.
    """

    def setup_method(self):
        _wipe(HistAssoc)
        _wipe(HistAccess)

    def teardown_method(self):
        _wipe(HistAssoc)
        _wipe(HistAccess)

    def test_carried_structures_are_plain_json(self):
        assoc = HistAssoc.create(name="w1")
        field = HistAssoc._meta.fields["edges"]
        field.link(HistAssoc, assoc.db_key.redis_key, "t", initial_weight=0.25)

        access = HistAccess.create(name="w2", content="x")
        access.on_read()
        assert access.confirm_access() == 1

        def records_of(model_class):
            data = export_records(model_class).data
            assert "__dictpairs__" not in data, (
                f"{model_class.__name__}'s export carries a tagged bytes "
                f"encoding: a carrier handed raw Redis bytes to to_jsonable "
                f"instead of decoding them"
            )
            assert "__bytes__" not in data
            # The first line is the manifest; records carry a "key".
            found = [
                parsed
                for parsed in (json.loads(ln) for ln in data.splitlines() if ln.strip())
                if "key" in parsed
            ]
            assert found, f"{model_class.__name__} exported no record"
            return found

        edges = records_of(HistAssoc)[0]["state"]["edges"]["edges"]
        assert all(isinstance(k, str) for k in edges)
        assert all(isinstance(v, float) for v in edges.values())

        log = records_of(HistAccess)[0]["model_state"]["AccessTrackerMixin"][
            "access_log"
        ]
        assert all(isinstance(ts, float) for ts in log)


# ---------------------------------------------------------------------------
# The three permanent limitations
# ---------------------------------------------------------------------------


PERMANENT = [
    (EventStreamMixin, "mutation stream"),
    (ExistenceFilter, "Bloom bit array"),
    (FrequencySketch, "Count-Min counters"),
]


class TestPermanentLimitations:
    """These assert a *contract*, not an implementation.

    A carry here would fabricate history the destination never lived through,
    so the deliverable for these three is an honest, permanent note -- and a
    test that fails if someone later "finishes" what was already decided by
    quietly adding a carrier.
    """

    @pytest.mark.parametrize(
        "klass,label", PERMANENT, ids=lambda v: getattr(v, "__name__", v)
    )
    def test_declares_partial_with_a_note_stating_the_contract(self, klass, label):
        assert klass.__dict__["roundtrip_policy"] == "partial"
        note = klass.__dict__["roundtrip_note"]
        assert note, f"{klass.__name__} must explain what the destination gets"
        assert "permanent contract" in note.lower(), (
            f"{klass.__name__}'s note must say the limitation is permanent, "
            f"not leave a reader expecting future work"
        )

    @pytest.mark.parametrize(
        "klass,label", PERMANENT, ids=lambda v: getattr(v, "__name__", v)
    )
    def test_declares_no_carrier_of_its_own(self, klass, label):
        for member in ("export_state", "import_state"):
            assert member not in klass.__dict__, (
                f"{klass.__name__} declares {member}: #556 decided this "
                f"structure is not carried. Reopen that decision explicitly "
                f"rather than adding a carrier under a 'partial' policy."
            )

    @pytest.mark.parametrize(
        "klass,label", PERMANENT, ids=lambda v: getattr(v, "__name__", v)
    )
    def test_note_no_longer_dangles_a_tracking_issue(self, klass, label):
        note = klass.__dict__["roundtrip_note"]
        assert "#556" not in note, (
            "a settled contract must not cite an open-work issue number; "
            "the reasoning belongs in the source comment, not the user-facing "
            "note"
        )
