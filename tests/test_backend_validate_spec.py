"""``validate_spec`` -- the static declaration check -- and the pure pieces
of the protocol types (#759 M1a, plan §2 "Declaration versus bind").

``validate_spec`` must refuse what a backend cannot store, name the field and
the reason, and never touch a network: it runs at class creation. These tests
build specs from real models and from hand-made ``ModelSpec`` values.
"""

from __future__ import annotations

from datetime import datetime

import pytest

import popoto
from popoto.backends import (
    And,
    BackendCapabilityError,
    Cond,
    FieldSpec,
    ModelSpec,
    Not,
    Op,
    Or,
    QueryCall,
    RecordId,
    UnitOfWork,
    build_model_spec,
    compile_where,
    validate_spec,
)
from popoto.fields.field import Field


class VsPlain(popoto.Model):
    name = popoto.KeyField()
    rank = popoto.SortedField(type=int, default=0)
    score = popoto.FloatField(default=0.0)
    hits = popoto.IntField(default=0)
    label = popoto.StringField(null=True)
    when = popoto.DatetimeField(null=True)
    note = popoto.Field(type=str, null=True)


class VsWide(popoto.Model):
    """Fields Postgres still refuses after M1.1 (M2/M5 kinds, and an index on
    a collection)."""

    name = popoto.KeyField()
    place = popoto.GeoField()
    relevance = popoto.DecayingSortedField()
    listed = popoto.IndexedField(type=list, null=True)


class VsBreadth(popoto.Model):
    """The M1.1 plain-field breadth (#759 plan §5 M1.1)."""

    name = popoto.KeyField()
    tags = popoto.TagField()
    email = popoto.IndexedField(type=str, null=True)
    login = popoto.UniqueField(type=str)
    blob = popoto.Field(type=dict, null=True)
    raw = popoto.BytesField(null=True)
    things = popoto.ListField(null=True)
    recent = popoto.ListField(max_length=5)
    bag = popoto.SetField(null=True)
    pair = popoto.TupleField(null=True)
    meta = popoto.DictField(null=True)
    day = popoto.DateField(null=True)
    clock = popoto.TimeField(null=True)
    parent = popoto.Relationship(model=VsPlain, null=True)

    class Meta:
        indexes = ((("email", "day"), True),)


class HookingField(Field):
    def on_save(self, *args, **kwargs):  # pragma: no cover - never called
        return super().on_save(*args, **kwargs)


class QuietField(Field):
    """A subclass that overrides no hook: storable from its ``type=``."""


class VsCustom(popoto.Model):
    name = popoto.KeyField()
    hooked = HookingField(type=str, null=True)
    quiet = QuietField(type=int, null=True)


class VsTtl(popoto.Model):
    name = popoto.KeyField()

    class Meta:
        ttl = 60


def test_spec_shape():
    spec = VsPlain._meta.spec
    assert spec.name == "VsPlain"
    assert spec.key_fields == ("name",)
    assert spec.backend is None
    assert spec.fields["rank"].kind == "SortedField"
    assert spec.fields["rank"].py_type is int
    assert spec.fields["note"].kind == "Field"
    assert spec.fields["when"].kind == "DatetimeField"
    assert VsPlain._meta.spec is spec  # memoised


def test_spec_rebuilds_after_auto_key_is_added():
    class VsAuto(popoto.Model):
        label = popoto.Field(type=str, null=True)

    before = VsAuto._meta.spec
    assert "_auto_key" not in before.fields
    VsAuto(label="x")  # first instantiation adds the AutoKeyField
    after = VsAuto._meta.spec
    assert after.fields["_auto_key"].kind == "AutoKeyField"
    assert after.key_fields == ("_auto_key",)


def test_redis_accepts_everything():
    for model in (VsPlain, VsWide, VsBreadth, VsCustom, VsTtl):
        validate_spec(model._meta.spec, "redis")


def test_postgres_accepts_the_m1_slice():
    validate_spec(VsPlain._meta.spec, "postgres")


def test_postgres_accepts_the_m1_1_breadth():
    """#759 M1.1 unlocks indexed/unique/tag/relationship/collection/bytes/
    date/time fields and Meta.indexes."""
    spec = VsBreadth._meta.spec
    validate_spec(spec, "postgres")
    assert spec.indexes == (("email", "day"),)
    assert spec.unique_indexes == (("email", "day"),)
    assert spec.fields["recent"].options["capped"] is True
    assert "capped" not in spec.fields["things"].options


def test_postgres_refuses_fields_beyond_m1_1_naming_each():
    with pytest.raises(BackendCapabilityError) as info:
        validate_spec(VsWide._meta.spec, "postgres")
    message = str(info.value)
    assert "place (GeoField)" in message
    # DecayingSortedField is stored on Postgres from #759 M2a.
    assert "relevance" not in message
    assert "listed (IndexedField, type=list)" in message
    assert "indexed field needs a scalar column type" in message


def test_postgres_accepts_the_m2a_memory_fields_and_refuses_later_ones():
    """#759 M2a: the decay clock and ConfidenceField are stored on Postgres;
    a partitioned ConfidenceField (M3) and a CyclicDecayField (M5) are not."""

    class VsMemory(popoto.AccessTrackerMixin, popoto.Model):
        name = popoto.KeyField()
        relevance = popoto.DecayingSortedField(base_score_field="name")
        certainty = popoto.ConfidenceField()

    class VsMemoryLater(popoto.Model):
        name = popoto.KeyField()
        project = popoto.KeyField()
        certainty = popoto.ConfidenceField(partition_by="project")
        rhythm = popoto.CyclicDecayField()

    validate_spec(VsMemory._meta.spec, "postgres")
    assert VsMemory._meta.spec.mixins == frozenset({"AccessTrackerMixin"})
    with pytest.raises(BackendCapabilityError) as info:
        validate_spec(VsMemoryLater._meta.spec, "postgres")
    message = str(info.value)
    assert "certainty (ConfidenceField, partition_by=)" in message
    assert "partitioned confidence arrives in M3" in message
    assert "rhythm (CyclicDecayField)" in message


def test_postgres_refuses_a_prediction_ledger_at_bind():
    """#773 review: the ledger lives in Redis structures until M5, so a
    Postgres model declaring it is refused statically instead of letting
    ``record_prediction`` / ``auto_resolve`` issue Redis commands for a
    record Redis does not hold."""
    from popoto.fields.prediction_ledger import PredictionLedgerMixin

    class VsLedger(PredictionLedgerMixin, popoto.AccessTrackerMixin, popoto.Model):
        name = popoto.KeyField()

    assert "PredictionLedgerMixin" in VsLedger._meta.spec.mixins
    validate_spec(VsLedger._meta.spec, "redis")
    with pytest.raises(BackendCapabilityError) as info:
        validate_spec(VsLedger._meta.spec, "postgres")
    message = str(info.value)
    assert "PredictionLedgerMixin is not supported yet" in message
    assert "M5" in message and "AccessTrackerMixin" not in message
    with pytest.raises(BackendCapabilityError, match="PredictionLedgerMixin"):

        class VsLedgerPg(PredictionLedgerMixin, popoto.Model):
            name = popoto.KeyField()

            class Meta:
                backend = "postgres"


def test_postgres_refuses_hook_overriding_custom_fields_only():
    with pytest.raises(BackendCapabilityError) as info:
        validate_spec(VsCustom._meta.spec, "postgres")
    message = str(info.value)
    assert "hooked" in message and "overrides on_save" in message
    assert "quiet" not in message
    assert VsCustom._meta.spec.fields["quiet"].kind == "Field"
    assert "custom_class" in VsCustom._meta.spec.fields["quiet"].options


def test_postgres_refuses_meta_ttl_and_accepts_indexes():
    with pytest.raises(BackendCapabilityError, match="Meta.ttl"):
        validate_spec(VsTtl._meta.spec, "postgres")
    spec = ModelSpec(
        name="X",
        key_fields=("k",),
        fields={"k": FieldSpec("k", "KeyField", str, False)},
        order_by=None,
        ttl=None,
        indexes=(("k",),),
    )
    validate_spec(spec, "postgres")  # Meta.indexes arrived in M1.1


def test_unknown_backend_name():
    with pytest.raises(BackendCapabilityError, match="unknown backend"):
        validate_spec(VsPlain._meta.spec, "mongo")


def test_explicit_meta_backend_runs_the_check_at_class_creation():
    with pytest.raises(BackendCapabilityError, match="GeoField"):

        class VsPgBad(popoto.Model):
            name = popoto.KeyField()
            place = popoto.GeoField()

            class Meta:
                backend = "postgres"

    class VsPgOk(popoto.Model):
        name = popoto.KeyField()
        when = popoto.SortedField(type=datetime, default=datetime(2026, 1, 1))

        class Meta:
            backend = "postgres"

    assert VsPgOk._meta.spec.backend == "postgres"


def test_abstract_models_are_not_checked():
    class VsAbstract(popoto.Model):
        tags = popoto.TagField()

        class Meta:
            abstract = True
            backend = "postgres"


def test_build_model_spec_is_pure():
    spec = build_model_spec(VsPlain._meta)
    assert spec == VsPlain._meta.spec


# -- compile_where ------------------------------------------------------------


def _call(*q_objects, **kwargs):
    return QueryCall(query=None, kind="filter", kwargs=kwargs, q_objects=q_objects)


def test_compile_where_kwargs():
    assert compile_where(_call()) is None
    assert compile_where(_call(name="a")) == Cond("name", Op.EXACT, "a")
    assert compile_where(_call(rank__gte=2, limit=3, order_by="rank")) == Cond(
        "rank", Op.GTE, 2
    )
    assert compile_where(_call(name__in=["a"], rank__between=(1, 2))) == And(
        (Cond("name", Op.IN, ["a"]), Cond("rank", Op.BETWEEN, (1, 2)))
    )


def test_compile_where_q_objects():
    Q = popoto.Q
    where = compile_where(_call(Q(name="a") | ~Q(rank__lt=3), org="x"))
    assert where == And(
        (
            Cond("org", Op.EXACT, "x"),
            Or((Cond("name", Op.EXACT, "a"), Not(Cond("rank", Op.LT, 3)))),
        )
    )


def test_compile_where_keeps_unknown_suffixes_as_field_names():
    # A double underscore that is not an operator is part of the name; the
    # backend refuses an unknown field, the compiler does not guess.
    assert compile_where(_call(geo__radius=5)) == Cond("geo__radius", Op.EXACT, 5)


# -- types ----------------------------------------------------------------------


def test_record_id_keeps_the_callers_key_object_out_of_identity():
    a = RecordId.from_key("M", b"M:1")
    b = RecordId.from_key("M", "M:1")
    assert a == b and hash(a) == hash(b)
    assert a.key == b"M:1" and b.key == "M:1"
    assert a.canonical == "M:1"


def test_unit_of_work_is_always_truthy():
    assert bool(UnitOfWork())
    assert bool(UnitOfWork([]))
    assert UnitOfWork(popoto.get_redis().pipeline()).is_redis_pipeline
    assert not UnitOfWork(object()).is_redis_pipeline
