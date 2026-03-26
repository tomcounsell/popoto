"""
Test round-trip persistence of static field defaults through Redis.

Verifies that after save() + query.get(), each field type's static default
is correctly reconstructed by decode_value(). Covers the gap identified in
GitHub issue #289 — static defaults were only tested in-memory, not after
a full save-load round-trip.
"""

import os
import sys
from decimal import Decimal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.redis_db import POPOTO_REDIS_DB


# --- Test Models (one per field type for isolation) ---


class IntDefaultModel(popoto.Model):
    name = popoto.UniqueKeyField()
    value = popoto.IntField(default=42)


class FloatDefaultModel(popoto.Model):
    name = popoto.UniqueKeyField()
    value = popoto.FloatField(default=3.14)


class DecimalDefaultModel(popoto.Model):
    name = popoto.UniqueKeyField()
    value = popoto.DecimalField(default=Decimal("1.5"))


class StringDefaultModel(popoto.Model):
    name = popoto.UniqueKeyField()
    value = popoto.StringField(default="hello")


class BoolDefaultModel(popoto.Model):
    name = popoto.UniqueKeyField()
    value = popoto.BooleanField(default=False)


class DictDefaultModel(popoto.Model):
    name = popoto.UniqueKeyField()
    value = popoto.DictField(default={"key": "val"})


class ListDefaultModel(popoto.Model):
    name = popoto.UniqueKeyField()
    value = popoto.ListField(default=[1, 2, 3])


class SetDefaultModel(popoto.Model):
    name = popoto.UniqueKeyField()
    value = popoto.SetField(default={"a", "b"})


ALL_MODELS = [
    IntDefaultModel,
    FloatDefaultModel,
    DecimalDefaultModel,
    StringDefaultModel,
    BoolDefaultModel,
    DictDefaultModel,
    ListDefaultModel,
    SetDefaultModel,
]


# --- Fixtures ---


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up Redis keys after each test."""
    yield
    for model_class in ALL_MODELS:
        for key in POPOTO_REDIS_DB.keys(f"{model_class.__name__}:*"):
            POPOTO_REDIS_DB.delete(key)
        # Clean up class set keys
        POPOTO_REDIS_DB.delete(model_class.__name__)


# --- Parametrized Round-Trip Test ---


@pytest.mark.parametrize(
    "model_class,expected_default",
    [
        (IntDefaultModel, 42),
        (FloatDefaultModel, 3.14),
        (DecimalDefaultModel, Decimal("1.5")),
        (StringDefaultModel, "hello"),
        (BoolDefaultModel, False),
        (DictDefaultModel, {"key": "val"}),
        (ListDefaultModel, [1, 2, 3]),
        (SetDefaultModel, {"a", "b"}),
    ],
    ids=[
        "IntField",
        "FloatField",
        "DecimalField",
        "StringField",
        "BooleanField",
        "DictField",
        "ListField",
        "SetField",
    ],
)
def test_static_default_roundtrip(model_class, expected_default):
    """Static default survives save-load round-trip through Redis (issue #289)."""
    instance = model_class.create(name="roundtrip1")

    # Verify in-memory default is correct before reload
    assert instance.value == expected_default

    # Reload from Redis
    loaded = model_class.query.get(name="roundtrip1")

    assert loaded.value is not None, (
        f"{model_class.__name__}.value should not be None after reload"
    )
    assert loaded.value == expected_default, (
        f"{model_class.__name__}.value should be {expected_default!r} after reload, "
        f"got {loaded.value!r}"
    )
