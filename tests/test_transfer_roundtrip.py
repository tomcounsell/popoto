"""Round-trip fidelity tests for ``popoto.transfer``.

Covers the plan's AC #1 (plain-field byte-identical round trip), AC #3 (a
third-party ``Field`` subclass with no round-trip support), AC #5 (no
arguments needed), AC #6 (filtering via the existing query API), AC #7 (a
zero-match filter is distinguishable from an empty model), the format /
manifest contract, the ``roundtrip_policy`` declaration guard (plan Risk 3),
and the generic-driver constraint.

Everything here runs against live Redis (DB 15, isolated by the popoto pytest
plugin). No mocks.
"""

from __future__ import annotations

import ast
import datetime
import decimal
import importlib
import inspect
import io
import json
import pkgutil
from pathlib import Path

import pytest

import popoto
import popoto.fields
from popoto import Field, IndexedField, KeyField, Model, SortedField
from popoto.exceptions import ModelException
from popoto.fields.field import Field as BaseField

# popoto defines two distinct QueryException classes -- popoto.QueryException
# (from popoto.exceptions) is NOT the one the query layer raises. AC #6 is
# about the exception the query layer actually raises, so import it from
# there.
from popoto.models.query import QueryException
from popoto.transfer import export_records, import_records

# ---------------------------------------------------------------------------
# Fixture models
# ---------------------------------------------------------------------------


class ThirdPartyField(Field):
    """A field type Popoto does not ship, defined outside the library.

    AC #3: it declares **no** ``roundtrip_policy``, **no** ``export_state``
    and **no** ``import_state``. It must round-trip correctly purely on the
    base-class defaults. Do not add the protocol members here -- their absence
    is the whole point of this class.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("type", str)
        super().__init__(**kwargs)


class RoundTripPlain(Model):
    """Ordinary fields, including every type that goes through the encoders."""

    key = KeyField(type=str)
    title = Field(type=str, null=True)
    ratio = Field(type=float, null=True)
    flag = Field(type=bool, null=True)
    amount = Field(type=decimal.Decimal, null=True)
    when = Field(type=datetime.datetime, null=True)
    tags = Field(type=set, null=True)
    coords = Field(type=tuple, null=True)
    rank = SortedField(type=float, default=0.0)


class RoundTripAutoKey(Model):
    """No KeyField at all -- relies on the generated ``_auto_key``.

    Also carries the out-of-tree field type and an indexed field, so this one
    model exercises AC #3, AC #5, AC #6 and the identity-preservation case.
    """

    label = Field(type=str, null=True)
    custom = ThirdPartyField(null=True)
    status = IndexedField(type=str, null=True)


class RoundTripDestination(Model):
    """A second model, used only as a wrong-manifest import target."""

    key = KeyField(type=str)
    label = Field(type=str, null=True)


ALL_MODELS = (RoundTripPlain, RoundTripAutoKey, RoundTripDestination)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def clear_all():
    for model_class in ALL_MODELS:
        model_class.delete_all()


def split_export(data: str):
    """Return ``(manifest, [record, ...])`` parsed from JSONL export text."""
    lines = [line for line in data.splitlines() if line.strip()]
    manifest = json.loads(lines[0])
    records = [json.loads(line) for line in lines[1:]]
    return manifest, records


def records_by_key(data: str) -> dict:
    _manifest, records = split_export(data)
    return {record["key"]: record for record in records}


def make_plain_records():
    """Create two records covering populated and all-null field values."""
    populated = RoundTripPlain.create(
        key="populated",
        title="hello world",
        ratio=1.5,
        flag=True,
        amount=decimal.Decimal("12.34"),
        when=datetime.datetime(2020, 1, 2, 3, 4, 5),
        tags={"alpha", "beta"},
        coords=(1, 2, 3),
        rank=3.5,
    )
    nulls = RoundTripPlain.create(
        key="nulls",
        title=None,
        ratio=None,
        flag=False,
        amount=None,
        when=None,
        tags=None,
        coords=None,
        rank=0.0,
    )
    return populated, nulls


PLAIN_VALUE_FIELDS = (
    "key",
    "title",
    "ratio",
    "flag",
    "amount",
    "when",
    "tags",
    "coords",
    "rank",
)


def snapshot(instance, field_names=PLAIN_VALUE_FIELDS) -> dict:
    return {name: getattr(instance, name) for name in field_names}


# ---------------------------------------------------------------------------
# AC #1 -- plain-field round trip
# ---------------------------------------------------------------------------


class TestPlainFieldRoundTrip:
    """AC #1: a model of ordinary fields round-trips with identical values."""

    def setup_method(self):
        clear_all()

    def teardown_method(self):
        clear_all()

    def test_values_survive_delete_and_reimport(self):
        populated, nulls = make_plain_records()
        before = {
            populated.db_key.redis_key: snapshot(populated),
            nulls.db_key.redis_key: snapshot(nulls),
        }

        result = export_records(RoundTripPlain)
        assert result.record_count == 2
        assert result.errors == []

        RoundTripPlain.delete_all()
        assert RoundTripPlain.query.count() == 0

        report = import_records(RoundTripPlain, io.StringIO(result.data))
        assert report.total == 2
        assert report.count("landed") == 2, report.summary()

        after = {
            instance.db_key.redis_key: snapshot(instance)
            for instance in RoundTripPlain.query.all()
        }
        assert after == before

    def test_encoded_types_are_exact_not_stringified(self):
        """Decimal / datetime / set / tuple survive with their real types."""
        make_plain_records()
        result = export_records(RoundTripPlain)
        RoundTripPlain.delete_all()
        import_records(RoundTripPlain, io.StringIO(result.data))

        restored = RoundTripPlain.query.get(key="populated")
        assert restored.amount == decimal.Decimal("12.34")
        assert isinstance(restored.amount, decimal.Decimal)
        assert restored.when == datetime.datetime(2020, 1, 2, 3, 4, 5)
        assert isinstance(restored.when, datetime.datetime)
        assert restored.tags == {"alpha", "beta"}
        assert isinstance(restored.tags, set)
        assert restored.coords == (1, 2, 3)
        assert isinstance(restored.coords, tuple)
        assert restored.flag is True
        assert restored.ratio == 1.5

    def test_null_and_false_values_are_preserved_distinctly(self):
        make_plain_records()
        result = export_records(RoundTripPlain)
        RoundTripPlain.delete_all()
        import_records(RoundTripPlain, io.StringIO(result.data))

        restored = RoundTripPlain.query.get(key="nulls")
        assert restored.title is None
        assert restored.amount is None
        assert restored.when is None
        assert restored.tags is None
        assert restored.coords is None
        # False must not degrade into None.
        assert restored.flag is False

    def test_reexport_is_byte_identical(self):
        """The record lines of a re-export match the original, byte for byte.

        The manifest line is excluded: it carries ``exported_at``, which is
        deliberately a wall-clock stamp.
        """
        make_plain_records()
        original = export_records(RoundTripPlain).data

        RoundTripPlain.delete_all()
        import_records(RoundTripPlain, io.StringIO(original))
        second = export_records(RoundTripPlain).data

        original_lines = original.splitlines()[1:]
        second_lines = second.splitlines()[1:]
        assert sorted(second_lines) == sorted(original_lines)

    def test_overwrite_conflict_mode_round_trips_in_place(self):
        """A re-import over mutated records restores the exported values."""
        populated, _nulls = make_plain_records()
        before = snapshot(populated)
        result = export_records(RoundTripPlain)

        populated.title = "mutated"
        populated.ratio = 99.0
        populated.save()
        assert RoundTripPlain.query.get(key="populated").title == "mutated"

        report = import_records(
            RoundTripPlain, io.StringIO(result.data), on_conflict="overwrite"
        )
        assert report.count("landed") == 2, report.summary()
        assert snapshot(RoundTripPlain.query.get(key="populated")) == before


# ---------------------------------------------------------------------------
# Identity preservation -- the issue's failure class #1
# ---------------------------------------------------------------------------


class TestAutoKeyPreservation:
    """``to_dict()`` omits implicit key fields; export must add them back."""

    def setup_method(self):
        clear_all()

    def teardown_method(self):
        clear_all()

    def test_to_dict_alone_would_lose_the_auto_key(self):
        """Documents *why* the export has to add the implicit key back."""
        instance = RoundTripAutoKey.create(label="one")
        auto_key_name = RoundTripAutoKey._get_auto_key_field_name()
        assert auto_key_name is not None
        assert auto_key_name not in instance.to_dict()

    def test_auto_key_survives_the_round_trip(self):
        instance = RoundTripAutoKey.create(label="one", custom="cv", status="live")
        original_redis_key = instance.db_key.redis_key
        auto_key_name = RoundTripAutoKey._get_auto_key_field_name()
        original_auto_key = getattr(instance, auto_key_name)

        result = export_records(RoundTripAutoKey)
        exported = records_by_key(result.data)[original_redis_key]
        assert exported["values"][auto_key_name] == original_auto_key

        RoundTripAutoKey.delete_all()
        report = import_records(RoundTripAutoKey, io.StringIO(result.data))
        assert report.count("landed") == 1, report.summary()

        restored = RoundTripAutoKey.query.all()
        assert len(restored) == 1
        assert restored[0].db_key.redis_key == original_redis_key
        assert getattr(restored[0], auto_key_name) == original_auto_key


# ---------------------------------------------------------------------------
# AC #3 -- third-party field with no round-trip support
# ---------------------------------------------------------------------------


class TestThirdPartyFieldSubclass:
    """AC #3, the headline extensibility guarantee.

    ``ThirdPartyField`` adds no ``export_state``, no ``import_state`` and no
    ``roundtrip_policy``. It must round-trip correctly with zero driver
    changes and zero field changes, purely on ``Field``'s base defaults. If
    this test ever needs the field to gain a protocol member to pass, the
    guarantee has been broken.
    """

    def setup_method(self):
        clear_all()

    def teardown_method(self):
        clear_all()

    def test_field_declares_none_of_the_protocol_members(self):
        own = ThirdPartyField.__dict__
        assert "export_state" not in own
        assert "import_state" not in own
        assert "roundtrip_policy" not in own
        assert "roundtrip_note" not in own

    def test_value_round_trips_on_base_class_defaults(self):
        RoundTripAutoKey.create(label="one", custom="third-party value")
        RoundTripAutoKey.create(label="two", custom=None)

        result = export_records(RoundTripAutoKey)
        assert result.errors == []
        # Base export_state returns None, so nothing lands in `state`.
        _manifest, records = split_export(result.data)
        assert all(record["state"] == {} for record in records)

        RoundTripAutoKey.delete_all()
        report = import_records(RoundTripAutoKey, io.StringIO(result.data))
        assert report.count("landed") == 2, report.summary()

        restored = {i.label: i.custom for i in RoundTripAutoKey.query.all()}
        assert restored == {"one": "third-party value", "two": None}

    def test_manifest_reports_the_inherited_default_policy(self):
        RoundTripAutoKey.create(label="one", custom="x")
        manifest, _records = split_export(export_records(RoundTripAutoKey).data)
        entry = manifest["fields"]["custom"]
        assert entry["class"] == "ThirdPartyField"
        assert entry["policy"] == "rebuild"
        assert entry["note"] is None


# ---------------------------------------------------------------------------
# AC #5 -- no arguments needed
# ---------------------------------------------------------------------------


class TestNoArgumentExport:
    """AC #5: a model with no grouping/partition field exports with no args."""

    def setup_method(self):
        clear_all()

    def teardown_method(self):
        clear_all()

    def test_classmethod_with_no_arguments_exports_everything(self):
        for index in range(5):
            RoundTripAutoKey.create(label=f"label-{index}")

        result = RoundTripAutoKey.export_records()

        assert result.model == "RoundTripAutoKey"
        assert result.filter is None
        assert result.matched_count == 5
        assert result.record_count == 5
        _manifest, records = split_export(result.data)
        assert len(records) == 5

    def test_module_level_function_matches_the_classmethod(self):
        RoundTripAutoKey.create(label="only")
        via_function = export_records(RoundTripAutoKey)
        via_classmethod = RoundTripAutoKey.export_records()
        assert records_by_key(via_function.data) == records_by_key(via_classmethod.data)


# ---------------------------------------------------------------------------
# AC #6 -- filtering through the existing query API
# ---------------------------------------------------------------------------


class TestFilteringViaQueryApi:
    """AC #6: filters forward to ``Model.query.filter``, unknown ones raise."""

    def setup_method(self):
        clear_all()

    def teardown_method(self):
        clear_all()

    def test_valid_filter_narrows_the_export(self):
        RoundTripAutoKey.create(label="a", status="active")
        RoundTripAutoKey.create(label="b", status="active")
        RoundTripAutoKey.create(label="c", status="idle")

        result = export_records(RoundTripAutoKey, status="active")

        assert result.matched_count == 2
        assert result.record_count == 2
        assert result.filter == "Q(status='active')"
        assert result.filter_kwargs == {"status": "active"}
        _manifest, records = split_export(result.data)
        assert {record["values"]["label"] for record in records} == {"a", "b"}

    def test_unknown_filter_param_raises_rather_than_being_ignored(self):
        RoundTripAutoKey.create(label="a", status="active")

        with pytest.raises(QueryException) as excinfo:
            export_records(RoundTripAutoKey, no_such_field="whatever")

        assert "no_such_field" in str(excinfo.value)

    def test_filtered_export_imports_only_the_filtered_subset(self):
        RoundTripAutoKey.create(label="a", status="active")
        RoundTripAutoKey.create(label="b", status="idle")

        result = export_records(RoundTripAutoKey, status="active")
        RoundTripAutoKey.delete_all()
        report = import_records(RoundTripAutoKey, io.StringIO(result.data))

        assert report.total == 1
        assert report.count("landed") == 1, report.summary()
        assert [i.label for i in RoundTripAutoKey.query.all()] == ["a"]


# ---------------------------------------------------------------------------
# AC #7 -- zero-match filter vs. empty model
# ---------------------------------------------------------------------------


class TestZeroMatchIsDistinguishableFromEmptyModel:
    """AC #7, the subtle one.

    Both cases produce ``matched_count == 0``. They are told apart by
    ``filter``: a filter that matched nothing keeps its provenance string; an
    unfiltered export of an empty model has ``filter is None``.
    """

    def setup_method(self):
        clear_all()

    def teardown_method(self):
        clear_all()

    def test_zero_match_filter_keeps_its_filter_provenance(self):
        RoundTripAutoKey.create(label="a", status="active")

        result = export_records(RoundTripAutoKey, status="nobody-has-this")

        assert result.matched_count == 0
        assert result.record_count == 0
        assert result.filter is not None
        assert result.filter == "Q(status='nobody-has-this')"

        manifest, records = split_export(result.data)
        assert records == []
        assert manifest["matched_count"] == 0
        assert manifest["filter"] == "Q(status='nobody-has-this')"

    def test_empty_model_unfiltered_has_no_filter(self):
        assert RoundTripAutoKey.query.count() == 0

        result = export_records(RoundTripAutoKey)

        assert result.matched_count == 0
        assert result.record_count == 0
        assert result.filter is None

        manifest, records = split_export(result.data)
        assert records == []
        assert manifest["matched_count"] == 0
        assert manifest["filter"] is None

    def test_the_two_zero_count_cases_are_distinguishable(self):
        RoundTripAutoKey.create(label="a", status="active")
        zero_match = export_records(RoundTripAutoKey, status="nobody-has-this")
        RoundTripAutoKey.delete_all()
        empty_model = export_records(RoundTripAutoKey)

        assert zero_match.matched_count == empty_model.matched_count == 0
        # Identical counts, different provenance -- that is the whole point.
        assert zero_match.filter is not None
        assert empty_model.filter is None
        assert zero_match.filter != empty_model.filter

        zero_manifest, _ = split_export(zero_match.data)
        empty_manifest, _ = split_export(empty_model.data)
        assert zero_manifest["filter"] is not None
        assert empty_manifest["filter"] is None

    def test_empty_export_imports_as_a_zero_count_report_not_an_error(self):
        result = export_records(RoundTripAutoKey)
        report = import_records(RoundTripAutoKey, io.StringIO(result.data))
        assert report.total == 0
        assert report.count("landed") == 0
        assert report.source_matched_count == 0


# ---------------------------------------------------------------------------
# Format / manifest contract
# ---------------------------------------------------------------------------


class TestFormatAndManifest:
    """Manifest is line 1; records follow; a wrong model refuses up front."""

    def setup_method(self):
        clear_all()

    def teardown_method(self):
        clear_all()

    def test_manifest_is_the_first_line_and_records_follow(self):
        make_plain_records()
        data = export_records(RoundTripPlain).data

        lines = [line for line in data.splitlines() if line.strip()]
        assert len(lines) == 3

        manifest = json.loads(lines[0])
        assert manifest["popoto_export"] == 1
        assert manifest["model"] == "RoundTripPlain"
        assert manifest["popoto_version"]
        assert manifest["exported_at"]
        assert manifest["consistent_snapshot"] is False

        for raw in lines[1:]:
            record = json.loads(raw)
            assert set(record) == {"key", "values", "state", "model_state"}
            assert record["key"].startswith("RoundTripPlain:")

    def test_manifest_record_count_matches_the_records_written(self):
        make_plain_records()
        result = export_records(RoundTripPlain)
        manifest, records = split_export(result.data)
        assert manifest["matched_count"] == len(records) == result.record_count == 2

    def test_manifest_carries_the_per_field_policy_rollup(self):
        make_plain_records()
        manifest, _records = split_export(export_records(RoundTripPlain).data)

        assert set(manifest["fields"]) == set(RoundTripPlain._meta.fields)
        for field_name, model_field in RoundTripPlain._meta.fields.items():
            entry = manifest["fields"][field_name]
            assert entry["class"] == type(model_field).__name__
            assert entry["policy"] == model_field.roundtrip_policy
            assert entry["note"] == model_field.roundtrip_note

    def test_import_report_surfaces_the_policy_rollup(self):
        make_plain_records()
        result = export_records(RoundTripPlain)
        RoundTripPlain.delete_all()
        report = import_records(RoundTripPlain, io.StringIO(result.data))

        assert set(report.fidelity) >= set(RoundTripPlain._meta.fields)
        rendered = report.summary()
        assert "round-trip fidelity" in rendered
        assert "amount" in rendered

    def test_wrong_model_manifest_raises_before_anything_is_written(self):
        RoundTripPlain.create(key="only", rank=1.0)
        data = export_records(RoundTripPlain).data
        assert RoundTripDestination.query.count() == 0

        with pytest.raises(ModelException) as excinfo:
            import_records(RoundTripDestination, io.StringIO(data))

        message = str(excinfo.value)
        assert "RoundTripPlain" in message
        assert "RoundTripDestination" in message
        # Nothing may have been written to the destination.
        assert RoundTripDestination.query.count() == 0
        assert len(RoundTripDestination.query.all()) == 0

    def test_missing_manifest_raises_before_anything_is_written(self):
        record_only = json.dumps({"key": "RoundTripDestination:x", "values": {}})
        with pytest.raises(ModelException):
            import_records(RoundTripDestination, io.StringIO(record_only + "\n"))
        assert RoundTripDestination.query.count() == 0


# ---------------------------------------------------------------------------
# Policy declaration enforcement (plan Risk 3) -- `pytest -k policy_declared`
# ---------------------------------------------------------------------------

FIELDS_PACKAGE_DIR = Path(popoto.fields.__file__).parent


def _iter_field_modules():
    """Import every module in ``src/popoto/fields/``.

    A module whose optional dependency is absent is skipped rather than
    failing the guard, so the check degrades gracefully in a lean env.
    """
    for module_info in pkgutil.iter_modules([str(FIELDS_PACKAGE_DIR)]):
        try:
            yield importlib.import_module(f"popoto.fields.{module_info.name}")
        except ImportError:  # pragma: no cover - optional dependency absent
            continue


def _classes_defined_in(module):
    for _name, obj in vars(module).items():
        if inspect.isclass(obj) and obj.__module__ == module.__name__:
            yield obj


def _own_method_source(klass) -> str:
    """Concatenated source of every method the class defines itself."""
    chunks = []
    for name, value in klass.__dict__.items():
        if name.startswith("__"):
            continue
        function = getattr(value, "__func__", value)
        if not inspect.isfunction(function):
            continue
        try:
            chunks.append(inspect.getsource(function))
        except (OSError, TypeError):  # pragma: no cover - defensive
            continue
    return "\n".join(chunks)


def collect_field_subclasses():
    """Every ``Field`` subclass defined in ``src/popoto/fields/``."""
    found = {}
    for module in _iter_field_modules():
        for klass in _classes_defined_in(module):
            if issubclass(klass, BaseField):
                found[klass] = module.__name__
    return found


def collect_model_level_mixins():
    """Every stateful model-level mixin defined in ``src/popoto/fields/``.

    Per the plan: a class that is not a ``Field`` subclass but maintains Redis
    state of its own -- it defines ``on_save``, ``_check_write_filter``, a
    ``$``-prefixed key builder, or otherwise touches ``POPOTO_REDIS_DB``
    directly. (The literal ``on_save``/``$``-builder triple misses
    ``EventStreamMixin``, which writes streams through its own key helper, so
    the direct-Redis-access clause is included to catch it.)

    Two deliberate narrowings, both to keep the guard on classes the driver's
    MRO walk can actually reach:

    * The class must be named ``*Mixin``, i.e. built to be mixed into a Model.
      That excludes ``observation.py``'s all-classmethod coordinators
      (``ObservationProtocol``, ``RecallProposal``) and ``shortcuts.py``'s
      ``CappedListProxy`` -- Redis-touching helpers that never appear in any
      model's MRO. The cost is that a future stateful model-level class named
      something other than ``*Mixin`` escapes this guard;
      ``test_model_level_mixins_are_discovered`` pins the four the plan names.
    * A config-only mixin such as ``UniqueFieldMixin`` (it sets ``unique=True``
      and nothing else; the unique index is maintained by ``KeyFieldMixin``)
      has no state to declare a policy about and is not required to have one.
    """
    found = {}
    for module in _iter_field_modules():
        for klass in _classes_defined_in(module):
            if issubclass(klass, BaseField):
                continue
            if not klass.__name__.endswith("Mixin"):
                continue
            own = klass.__dict__
            source = _own_method_source(klass)
            is_stateful = (
                "on_save" in own
                or "_check_write_filter" in own
                or "$" in source
                or "POPOTO_REDIS_DB" in source
            )
            if not is_stateful:
                continue
            found[klass] = module.__name__
    return found


VALID_POLICIES = ("rebuild", "carry", "partial")


class TestRoundtripPolicyDeclared:
    """Guard against a field silently drifting out of true (plan Risk 3).

    Selected by ``pytest -k policy_declared``.
    """

    def test_field_subclasses_are_discovered(self):
        """Sanity check: the enumeration must not be vacuously empty."""
        subclasses = collect_field_subclasses()
        assert len(subclasses) > 20
        names = {klass.__name__ for klass in subclasses}
        assert {"KeyField", "SortedField", "ConfidenceField"} <= names

    def test_model_level_mixins_are_discovered(self):
        mixins = collect_model_level_mixins()
        names = {klass.__name__ for klass in mixins}
        assert {
            "AccessTrackerMixin",
            "EventStreamMixin",
            "PredictionLedgerMixin",
            "WriteFilterMixin",
        } <= names

    def test_every_field_has_a_policy_declared(self):
        """Each field declares a policy, or provably inherits a correct one.

        Inheriting ``"rebuild"`` from ``Field`` is allowed only for a class
        with no ``on_save`` of its own: the moment a field grows its own save
        side effects, it must say what happens to them on round trip.
        """
        offenders = []
        for klass, module_name in collect_field_subclasses().items():
            policy = getattr(klass, "roundtrip_policy", None)
            if policy not in VALID_POLICIES:
                offenders.append(
                    f"{module_name}.{klass.__name__}: "
                    f"roundtrip_policy={policy!r} is not one of {VALID_POLICIES}"
                )
                continue
            declares_own = "roundtrip_policy" in klass.__dict__
            has_own_on_save = "on_save" in klass.__dict__
            if has_own_on_save and not declares_own:
                offenders.append(
                    f"{module_name}.{klass.__name__}: defines its own on_save "
                    f"but declares no explicit roundtrip_policy"
                )
        assert not offenders, "\n".join(offenders)

    def test_every_model_level_mixin_has_a_policy_declared(self):
        offenders = []
        for klass, module_name in collect_model_level_mixins().items():
            if "roundtrip_policy" not in klass.__dict__:
                offenders.append(
                    f"{module_name}.{klass.__name__}: model-level mixin with "
                    f"independent state declares no roundtrip_policy"
                )
                continue
            policy = klass.__dict__["roundtrip_policy"]
            if policy not in VALID_POLICIES:
                offenders.append(
                    f"{module_name}.{klass.__name__}: "
                    f"roundtrip_policy={policy!r} is not one of {VALID_POLICIES}"
                )
        assert not offenders, "\n".join(offenders)

    def test_partial_policy_declared_requires_a_note(self):
        """``"partial"`` without a note is a report that explains nothing."""
        offenders = []
        candidates = {
            **collect_field_subclasses(),
            **collect_model_level_mixins(),
        }
        for klass, module_name in candidates.items():
            if klass.__dict__.get("roundtrip_policy") != "partial":
                continue
            note = klass.__dict__.get("roundtrip_note")
            if not isinstance(note, str) or not note.strip():
                offenders.append(
                    f"{module_name}.{klass.__name__}: declares policy "
                    f"'partial' but roundtrip_note is {note!r}"
                )
        assert not offenders, "\n".join(offenders)

    def test_carry_policy_declared_implements_both_protocol_halves(self):
        """A ``"carry"`` declaration is a promise to move real state."""
        offenders = []
        for klass, module_name in collect_field_subclasses().items():
            if klass.__dict__.get("roundtrip_policy") != "carry":
                continue
            for member in ("export_state", "import_state"):
                if member not in klass.__dict__:
                    offenders.append(
                        f"{module_name}.{klass.__name__}: declares policy "
                        f"'carry' but does not implement {member}"
                    )
        assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# Generic-driver guarantee
# ---------------------------------------------------------------------------

TRANSFER_PACKAGE_DIR = Path(popoto.transfer.__file__).parent


def _isinstance_type_names(tree: ast.AST):
    """Yield every type name referenced by an ``isinstance`` call."""

    def names_in(node):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr
        elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            for element in node.elts:
                yield from names_in(element)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        function_name = getattr(function, "id", getattr(function, "attr", None))
        if function_name not in ("isinstance", "issubclass"):
            continue
        if len(node.args) < 2:
            continue
        yield from names_in(node.args[1])


class TestGenericDriver:
    """The plan's hard constraint: the driver names no concrete field type.

    Adding a field type must require no change to ``src/popoto/transfer/``.
    The driver may (and does) use ``isinstance`` against builtins such as
    ``str`` and ``dict``; what it may never do is branch on a concrete Popoto
    ``Field`` subclass or model-level mixin.
    """

    def test_no_isinstance_check_against_a_concrete_field_or_mixin_type(self):
        forbidden = {
            klass.__name__
            for klass in (
                *collect_field_subclasses(),
                *collect_model_level_mixins(),
            )
        }
        offenders = []
        for path in sorted(TRANSFER_PACKAGE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for type_name in _isinstance_type_names(tree):
                if type_name in forbidden:
                    offenders.append(f"{path.name}: isinstance(..., {type_name})")
        assert not offenders, "\n".join(offenders)

    def test_driver_source_never_imports_a_concrete_field_type(self):
        forbidden = {
            klass.__name__
            for klass in (
                *collect_field_subclasses(),
                *collect_model_level_mixins(),
            )
        } - {"Field"}
        offenders = []
        for path in sorted(TRANSFER_PACKAGE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                for alias in node.names:
                    if alias.name.split(".")[-1] in forbidden:
                        offenders.append(f"{path.name}: imports {alias.name}")
        assert not offenders, "\n".join(offenders)
