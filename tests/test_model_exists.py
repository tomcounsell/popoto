"""``Model.exists``: is this record stored, in one ``EXISTS`` (#630).

The model-layer answer to a question `recipes/provenance_journal.py` used to
ask the client directly. Three spellings -- KeyField kwargs, a ``DB_key``, and
a full Redis key string -- and one sharp edge the string spelling exists to
avoid: ``DB_key`` treats a string as ONE partial and escapes the ``:`` inside
it, so a multi-segment key handed to ``DB_key(...)`` names a key that never
exists. ``exists`` short-circuits a ``str`` to ``redis_key`` exactly as
``Query.get`` does, and the multi-segment cases below are what pin that.
"""

import os
import sys
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.models.db_key import DB_key


class Widget(popoto.Model):
    owner = popoto.KeyField()
    name = popoto.KeyField()
    size = popoto.IntField(null=True)


@pytest.fixture
def widget():
    w = Widget(owner=f"acme-{uuid.uuid4().hex[:8]}", name="bolt", size=3)
    w.save()
    yield w
    w.delete()


def test_exists_by_key_field_kwargs(widget):
    assert Widget.exists(owner=widget.owner, name="bolt") is True


def test_absent_by_key_field_kwargs(widget):
    assert Widget.exists(owner=widget.owner, name="nut") is False


def test_exists_by_multi_segment_redis_key_string(widget):
    """The shape `provenance_journal` passes: a full, colon-joined key."""
    key = widget.db_key.redis_key
    assert key.count(":") >= 2, f"expected a multi-segment key, got {key!r}"
    assert Widget.exists(key) is True
    assert Widget.exists(redis_key=key) is True


def test_absent_by_redis_key_string(widget):
    assert Widget.exists("Widget:nobody:nothing") is False


def test_exists_by_db_key_object(widget):
    assert Widget.exists(DB_key(widget.db_key)) is True


def test_string_is_not_escaped_into_a_key_that_never_exists(widget):
    """The blocker this signature was designed against.

    ``DB_key("Widget:acme:bolt").redis_key`` escapes both colons, so an
    implementation that fed the string to ``DB_key(...)`` would answer False
    for a record that is right there.
    """
    key = widget.db_key.redis_key
    assert DB_key(key).redis_key != key  # the trap is real
    assert Widget.exists(key) is True  # and this method does not fall in


def test_returns_a_bool_not_an_int(widget):
    assert type(Widget.exists(widget.db_key.redis_key)) is bool


def test_exists_after_delete():
    w = Widget(owner=f"acme-{uuid.uuid4().hex[:8]}", name="rivet")
    w.save()
    key = w.db_key.redis_key
    assert Widget.exists(key) is True
    w.delete()
    assert Widget.exists(key) is False
