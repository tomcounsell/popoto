"""A CyclicDecayField subclass must read the companion hashes on_save wrote.

Regression test for the defect #662 fixed as a consequence of routing
``models/query.py`` through ``rank_decayed``.

``FieldBase`` auto-assigns a distinct ``field_class_key`` per field class
(``fields/field.py``, ``DB_key(f"${name.strip('Field')}F")``) and enforces
uniqueness, so every subclass of :class:`CyclicDecayField` gets its own key
prefix. ``on_save`` writes the cycles/pressure companion hashes through the
*instance*, i.e. under the subclass's prefix. The old query path derived them
through ``CyclicDecayField.get_cycles_hash_key_from_parts`` -- a classmethod
hard-bound to the base class -- while building the ZSET key from
``field.__class__``. So writes landed under ``$MySubclassF:`` and query reads
looked under ``$CyclicDecayF:``.

Nothing raised. The Lua script short-circuits on a nil ``HGET``, so a
subclassed CyclicDecayField silently degraded to plain exponential decay at
query time: no cycles, no pressure, no error. ``rank_decayed`` derives the
companions as a suffix of the ZSET key it was handed, which agrees with
``on_save``.
"""

import sys
import os
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import msgpack
import pytest

from src import popoto
from src.popoto.fields.cyclic_decay_field import CyclicDecayField
from src.popoto.fields.constants import TemporalPeriod
from src.popoto.redis_db import get_REDIS_DB

CYCLES = [(TemporalPeriod.YEARLY, 5.0, 0)]


class SubclassedCyclicField(CyclicDecayField):
    """A no-op subclass. The point is only that it is a distinct class."""


class SubclassCyclicDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = SubclassedCyclicField(decay_rate=0.5, cycles=CYCLES, pressure_rate=0.1)


class BaseCyclicDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(decay_rate=0.5, cycles=CYCLES, pressure_rate=0.1)


@pytest.fixture
def seeded():
    for model in (SubclassCyclicDoc, BaseCyclicDoc):
        for i in range(3):
            model.create(name=f"{model.__name__}-{i}")
    yield
    for model in (SubclassCyclicDoc, BaseCyclicDoc):
        for obj in model.query.all():
            obj.delete()


def _companion_keys(model, field_name):
    field = model._meta.fields[field_name]
    instance = model.query.all()[0]
    return (
        field.get_cycles_hash_key(instance, field_name),
        field.get_pressure_hash_key(instance, field_name),
    )


def test_subclass_companion_keys_carry_the_subclass_prefix(seeded):
    """The keys on_save writes follow the field's own class, not the base."""
    cycles_key, pressure_key = _companion_keys(SubclassCyclicDoc, "relevance")

    assert cycles_key.startswith("$SubclassedCyclicF:"), cycles_key
    assert pressure_key.startswith("$SubclassedCyclicF:"), pressure_key

    db = get_REDIS_DB()
    assert db.exists(cycles_key), (
        f"on_save wrote no cycles hash at {cycles_key!r} — the fixture is "
        "not exercising the companion-hash write path"
    )

    # The pre-#662 derivation, kept here as the negative control: it is what
    # the query path used to compute, and it names a key nothing ever wrote.
    stale = CyclicDecayField.get_cycles_hash_key_from_parts(
        SubclassCyclicDoc, "relevance"
    )
    assert stale != cycles_key
    assert stale.startswith("$CyclicDecayF:"), stale
    assert not db.exists(stale), (
        f"{stale!r} exists — the two derivations no longer diverge, so this "
        "test has stopped testing anything"
    )


def _rank_with_pressure(model):
    """Give one of two rows 30 days of unresolved pressure; return the ranking.

    ``pressure_rate=0.1`` over 30 days is a +3.0 boost, which dominates the
    sub-millisecond decay difference between two rows created back to back.
    Mirrors ``test_cyclic_decay_field.py::test_pressure_increases_over_time``,
    which is the base-class form of this same check.
    """
    quiet = model.create(name=f"{model.__name__}-quiet")
    urgent = model.create(name=f"{model.__name__}-urgent")

    field = model._meta.fields["relevance"]
    pressure_key = field.get_pressure_hash_key(urgent, "relevance")
    now = time.time()
    db = get_REDIS_DB()
    db.hset(
        pressure_key,
        urgent.db_key.redis_key,
        msgpack.packb({"rate": 0.1, "last_resolved": now - 86400 * 30}),
    )
    db.hset(
        pressure_key,
        quiet.db_key.redis_key,
        msgpack.packb({"rate": 0.1, "last_resolved": now}),
    )

    return [obj.name for obj in model.query.top_by_decay("relevance", n=2)]


def test_subclass_top_by_decay_reads_the_pressure_hash_on_save_wrote():
    """The behavioral half: the subclass must not degrade to plain decay.

    Before #662 the query path derived the pressure hash key through
    ``CyclicDecayField.get_pressure_hash_key_from_parts``, so for a subclass it
    read a key ``on_save`` never wrote. The Lua script short-circuits on the
    nil HGET, the +3.0 pressure boost silently vanished, and the ranking fell
    back to raw decay order.

    Asserted against the base class in the same test so a change that breaks
    pressure ranking *generally* is distinguishable from the subclass defect.
    """
    try:
        assert _rank_with_pressure(BaseCyclicDoc)[0] == "BaseCyclicDoc-urgent"
        assert _rank_with_pressure(SubclassCyclicDoc)[0] == (
            "SubclassCyclicDoc-urgent"
        ), (
            "the subclass ignored its pressure hash — query is reading "
            "companion keys under the base class's prefix (#662)"
        )
    finally:
        for model in (SubclassCyclicDoc, BaseCyclicDoc):
            for obj in model.query.all():
                obj.delete()
