"""``Model.idle_seconds``/``load_fields``/``load_raw_hash`` (#649, #630 series).

These sit next to ``Model.exists`` as narrow, undecorated primitives the
recipe layer calls instead of talking to the Redis client directly. The
byte-identical-wire-behavior contract for #649 means the command chosen for
``load_fields`` (HGET for one field, HMGET for more) is load-bearing, and the
three-way outcome contract (absent -> omitted, undecodable -> None, Redis
error -> propagate) is invisible to any wire-diff oracle -- these tests are
what protects it.
"""

import os
import sys
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.models.encoding import decode_popoto_model_hashmap


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


def test_load_fields_single_name_issues_hget(widget, monkeypatch):
    calls = []
    original = popoto.POPOTO_REDIS_DB.execute_command

    def spy(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(popoto.POPOTO_REDIS_DB, "execute_command", spy)
    Widget.load_fields(widget.db_key.redis_key, "size")
    assert "HGET" in calls


def test_load_fields_multi_name_issues_hmget(widget, monkeypatch):
    calls = []
    original = popoto.POPOTO_REDIS_DB.execute_command

    def spy(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(popoto.POPOTO_REDIS_DB, "execute_command", spy)
    Widget.load_fields(widget.db_key.redis_key, "size", "name")
    assert "HMGET" in calls
    assert "HGET" not in calls


def test_load_fields_missing_field_is_omitted(widget):
    result = Widget.load_fields(widget.db_key.redis_key, "nonexistent_field")
    assert "nonexistent_field" not in result


def test_load_fields_happy_path_decodes_values(widget):
    result = Widget.load_fields(widget.db_key.redis_key, "size", "name")
    assert result == {"size": 3, "name": "bolt"}


def test_load_fields_undecodable_value_maps_to_none(widget):
    # Write raw non-msgpack bytes directly into the hash field -- this is
    # the case a wire diff cannot see: present-but-corrupt must map to
    # None, not be omitted or raise.
    popoto.POPOTO_REDIS_DB.hset(
        widget.db_key.redis_key, "size", b"\xff\xff\xff not msgpack"
    )
    result = Widget.load_fields(widget.db_key.redis_key, "size")
    assert "size" in result
    assert result["size"] is None


def test_load_fields_zero_names_raises_value_error(widget):
    with pytest.raises(ValueError):
        Widget.load_fields(widget.db_key.redis_key)


def test_load_raw_hash_returns_undecoded_bytes(widget):
    raw = Widget.load_raw_hash(widget.db_key.redis_key)
    assert raw
    for key, value in raw.items():
        assert isinstance(key, bytes)
        assert isinstance(value, bytes)


def test_load_raw_hash_round_trips_through_decode_popoto_model_hashmap(widget):
    raw = Widget.load_raw_hash(widget.db_key.redis_key)
    instance = decode_popoto_model_hashmap(
        Widget, raw, source_redis_key=widget.db_key.redis_key
    )
    assert instance.owner == widget.owner
    assert instance.name == widget.name
    assert instance.size == widget.size


def test_load_raw_hash_missing_key_returns_empty_dict():
    raw = Widget.load_raw_hash("Widget:nobody:nothing")
    assert raw == {}


def test_idle_seconds_live_key_returns_float(widget):
    result = Widget.idle_seconds(widget.db_key.redis_key)
    assert isinstance(result, float)


def test_idle_seconds_missing_key_returns_none():
    result = Widget.idle_seconds("Widget:nobody:nothing")
    assert result is None


def test_idle_seconds_sends_the_lowercase_idletime_subcommand(
    widget, monkeypatch, assert_captured
):
    """``OBJECT idletime`` -- lowercase, exactly as the recipe sent it.

    The subcommand string goes on the wire verbatim, so its case is a real
    byte difference, not cosmetic. Upper-casing it is the natural thing to
    write (Redis docs render subcommands in caps) and would break #649's
    byte-identical-command-sequence contract while every behavioral test
    still passed, because the server accepts either spelling.
    """
    seen = []
    original = popoto.POPOTO_REDIS_DB.object

    def spy(subcommand, *args, **kwargs):
        seen.append(subcommand)
        return original(subcommand, *args, **kwargs)

    monkeypatch.setattr(popoto.POPOTO_REDIS_DB, "object", spy)
    Widget.idle_seconds(redis_key=widget.db_key.redis_key)

    assert_captured(seen, ["idletime"])


def test_partial_load_apis_follow_a_rebound_global_client(
    monkeypatch, widget, assert_captured
):
    """``idle_seconds``/``load_fields``/``load_raw_hash`` read the client per call.

    Same contract as ``test_store_follows_a_rebound_global_client`` in
    ``tests/test_tombstone_store.py``: a name imported from ``redis_db`` is a
    snapshot that ``set_REDIS_DB_settings()`` never updates (#655), so these
    new classmethods resolve the client through the accessor instead.
    """
    seen = []
    real = popoto.get_redis()

    class RecordingClient:
        def __getattr__(self, name):
            attr = getattr(real, name)
            if not callable(attr):
                return attr

            def wrapper(*args, **kwargs):
                seen.append(name.upper())
                return attr(*args, **kwargs)

            return wrapper

    monkeypatch.setattr(
        popoto.redis_db, "POPOTO_REDIS_DB", RecordingClient(), raising=True
    )

    redis_key = widget.db_key.redis_key
    Widget.idle_seconds(redis_key=redis_key)
    Widget.load_fields(redis_key, "name")
    Widget.load_raw_hash(redis_key)

    assert_captured(seen, ["OBJECT", "HGET", "HGETALL"])
