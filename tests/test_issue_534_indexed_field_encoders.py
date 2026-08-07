"""Regression tests for #534.

``IndexedFieldMixin.on_save()`` packed the raw field value with a bare
``msgpack.packb()`` instead of routing it through ``TYPE_ENCODER_DECODERS``
first. Every type in that registry (``datetime``, ``date``, ``time``,
``Decimal``) therefore raised ``TypeError`` at save time, and even for types
msgpack could pack natively the index-path bytes were not guaranteed to match
the bytes ``encode_popoto_model_obj()`` writes.

These tests cover both halves: the save must succeed and round-trip, and the
hash bytes written by the Lua index path must be byte-for-byte identical to
the canonical encoder's output.
"""

import datetime
from decimal import Decimal

import msgpack
import pytest

from popoto import AutoKeyField, IndexedField, KeyField, Model, UniqueField
from popoto.models.encoding import encode_popoto_model_obj
from popoto.redis_db import POPOTO_REDIS_DB


class EncoderTypeIndexedModel(Model):
    """Indexed fields for every type in TYPE_ENCODER_DECODERS."""

    key = AutoKeyField()
    at = IndexedField(type=datetime.datetime, null=True)
    on = IndexedField(type=datetime.date, null=True)
    clock = IndexedField(type=datetime.time, null=True)
    amount = IndexedField(type=Decimal, null=True)


class EncoderTypeUniqueModel(Model):
    """UniqueField shares the same on_save() path."""

    key = AutoKeyField()
    at = UniqueField(type=datetime.datetime)


DT = datetime.datetime(2026, 1, 1, 12, 0)
DATE = datetime.date(2026, 1, 1)
TIME = datetime.time(12, 0)
AMOUNT = Decimal("12.34")


@pytest.fixture(autouse=True)
def _clean():
    EncoderTypeIndexedModel.delete_all()
    EncoderTypeUniqueModel.delete_all()
    yield
    EncoderTypeIndexedModel.delete_all()
    EncoderTypeUniqueModel.delete_all()


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("at", DT),
        ("on", DATE),
        ("clock", TIME),
        ("amount", AMOUNT),
    ],
)
def test_indexed_encoder_type_saves_and_round_trips(field_name, value):
    """Saving an IndexedField of an encoder-registry type must not raise."""
    instance = EncoderTypeIndexedModel.create(**{field_name: value})

    reloaded = EncoderTypeIndexedModel.query.get(key=instance.key)
    assert getattr(reloaded, field_name) == value


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("at", DT),
        ("on", DATE),
        ("clock", TIME),
        ("amount", AMOUNT),
    ],
)
def test_indexed_encoder_type_hash_bytes_match_canonical_encoder(field_name, value):
    """Index-path hash bytes must equal encode_popoto_model_obj() output."""
    instance = EncoderTypeIndexedModel.create(**{field_name: value})

    stored = POPOTO_REDIS_DB.hget(instance.db_key.redis_key, field_name)
    expected = encode_popoto_model_obj(instance)[field_name.encode()]
    assert stored == expected
    assert msgpack.unpackb(stored) == msgpack.unpackb(expected)


def test_indexed_datetime_filter_finds_record():
    """The value Set built on save must be reachable by an exact filter."""
    instance = EncoderTypeIndexedModel.create(at=DT)

    results = EncoderTypeIndexedModel.query.filter(at=DT)
    assert [r.key for r in results] == [instance.key]


def test_unique_field_encoder_type_saves():
    """UniqueField(type=datetime) uses the same packing path."""
    instance = EncoderTypeUniqueModel.create(at=DT)

    reloaded = EncoderTypeUniqueModel.query.get(key=instance.key)
    assert reloaded.at == DT


def test_indexed_encoder_type_value_change_moves_index():
    """Re-saving with a new value must SREM the old Set and SADD the new one."""
    instance = EncoderTypeIndexedModel.create(at=DT)
    later = DT + datetime.timedelta(days=1)
    instance.at = later
    instance.save()

    assert [r.key for r in EncoderTypeIndexedModel.query.filter(at=later)] == [
        instance.key
    ]
    assert list(EncoderTypeIndexedModel.query.filter(at=DT)) == []


def test_indexed_none_value_still_saves():
    """None must keep taking the plain-packb branch."""
    instance = EncoderTypeIndexedModel.create(at=None)

    reloaded = EncoderTypeIndexedModel.query.get(key=instance.key)
    assert reloaded.at is None


def test_indexed_str_value_unaffected():
    """Types outside the registry keep their existing bytes."""

    class PlainIndexedModel(Model):
        key = KeyField()
        status = IndexedField(type=str)

    PlainIndexedModel.delete_all()
    try:
        instance = PlainIndexedModel.create(key="a", status="active")
        stored = POPOTO_REDIS_DB.hget(instance.db_key.redis_key, "status")
        assert msgpack.unpackb(stored) == "active"
    finally:
        PlainIndexedModel.delete_all()
