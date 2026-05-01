"""
Test that lazy-loaded model instances return None / declared default for
fields absent from the loaded Redis hash, instead of leaking the class-level
Field descriptor object.

Regression coverage for: https://github.com/tomcounsell/popoto/issues/380

Background
----------
``Model.query.all()`` / ``Model.query.filter()`` use ``_create_lazy_model``
to skip ``__init__`` and defer msgpack deserialization. Before the fix in
PR #381 (this PR), fields absent from the Redis hash were never initialized,
so attribute access fell through to ``object.__getattribute__`` which
returned the class-level ``Field`` descriptor object — breaking any caller
that expected ``None`` / a primitive value.

These tests directly exercise ``_create_lazy_model`` with a hand-crafted
partial hash (no Redis I/O required), simulating a process that crashed
mid-write or a model that gained new fields after the row was last saved.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import msgpack

from src import popoto
from src.popoto.fields.field import Field
from src.popoto.models.encoding import _create_lazy_model


# --- Test models ---


class PartialHashModel(popoto.Model):
    """Covers a KeyField plus three field shapes for default resolution.

    - ``status``: no declared default → expect ``None``.
    - ``count``:  static default ``0`` → expect ``0``.
    - ``tags``:   callable default ``list`` → expect ``[]``.
    """

    project = popoto.KeyField()
    status = popoto.IndexedField()
    count = popoto.IntField(default=0)
    tags = popoto.ListField(default=list)


# --- Tests ---


def test_lazy_load_partial_hash():
    """Missing fields resolve to None / declared default, not a Field descriptor.

    Builds a partial hash containing only the KeyField, then loads it via
    ``_create_lazy_model`` and asserts every absent field returns its
    declared default (or ``None``) and is *not* an instance of ``Field``.
    """
    # Simulate a row where save() crashed after writing the KeyField but
    # before writing the rest of the hash.
    partial_hash = {
        b"project": msgpack.packb("crashed-mid-write"),
    }

    instance = _create_lazy_model(PartialHashModel, partial_hash)

    # KeyField present in hash → eagerly decoded, accessible as the value.
    assert instance.project == "crashed-mid-write"
    assert not isinstance(instance.project, Field)

    # status: no declared default → None.
    assert instance.status is None
    assert not isinstance(instance.status, Field), (
        f"status leaked Field descriptor: {type(instance.status)}"
    )

    # count: static default 0 → 0.
    assert instance.count == 0
    assert not isinstance(instance.count, Field)

    # tags: callable default list → [].
    assert instance.tags == []
    assert not isinstance(instance.tags, Field)


def test_lazy_load_full_hash_no_default_loop_clobber():
    """Defaults loop must not overwrite values present in the hash.

    Loads a fully-populated hash via ``_create_lazy_model`` and asserts
    every field round-trips correctly — guards against the new defaults
    loop accidentally clobbering decoded values.
    """
    full_hash = {
        b"project": msgpack.packb("fully-saved"),
        b"status": msgpack.packb("active"),
        b"count": msgpack.packb(42),
        b"tags": msgpack.packb(["alpha", "beta"]),
    }

    instance = _create_lazy_model(PartialHashModel, full_hash)

    # Every field accessible and correctly decoded; no descriptor leak.
    assert instance.project == "fully-saved"
    assert instance.status == "active"
    assert instance.count == 42
    assert instance.tags == ["alpha", "beta"]
    for value in (instance.project, instance.status, instance.count, instance.tags):
        assert not isinstance(value, Field)


def test_lazy_load_partial_hash_redis_key_unaffected():
    """The new defaults loop does not interfere with eager KeyField decoding.

    Specifically: ``_redis_key`` must still be populated when the KeyField
    is present in the partial hash (PR #300 behavior).
    """
    partial_hash = {
        b"project": msgpack.packb("with-redis-key"),
    }

    instance = _create_lazy_model(PartialHashModel, partial_hash)

    assert instance._redis_key is not None, (
        "_redis_key must be set when KeyField is present in the partial hash"
    )
    assert "project" in instance._saved_field_values
    assert instance._saved_field_values["project"] == "with-redis-key"
