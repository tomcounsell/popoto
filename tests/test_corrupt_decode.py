"""Tests for corruption-tolerant decode (#573).

A non-key model-hash field whose stored bytes cannot be decoded is
QUARANTINED: raw bytes stay in Redis untouched, the attribute reads as the
field's declared default, a WARNING is logged on ``POPOTO.encoding``, and the
raw bytes land in ``instance._corrupt_fields``. A corrupt KeyField instead
raises ``CorruptFieldError``. ``save()`` refuses (raises ``CorruptFieldError``)
when it would overwrite a still-quarantined field it is actually writing.

Every row here is built with a normal ``save()`` first, and corruption is
planted afterward with ``POPOTO_REDIS_DB.hset(...)`` directly on the hash --
never a hand-built hash from scratch -- so the surrounding row is genuine.

Runs on ``POPOTO_TEST_DB=14``. The pytest plugin flushes that DB before every
test, so no manual cleanup fixture is needed here.
"""

import asyncio
import os
from decimal import Decimal

import msgpack
import pytest

import popoto
from popoto.exceptions import CorruptFieldError
from popoto.models.db_key import DB_key
from popoto.models.encoding import TYPE_ENCODER_DECODERS, decode_lazy_field
from popoto.models.query import Query
from popoto.redis_db import POPOTO_REDIS_DB

# ---------------------------------------------------------------------------
# Test model
# ---------------------------------------------------------------------------


class Widget(popoto.Model):
    """One KeyField (raises on corruption) plus several plain fields
    (quarantined on corruption) covering a spread of encoded types."""

    slug = popoto.KeyField()
    bio = popoto.Field(type=str, null=True, default=None)
    status = popoto.Field(type=str, null=True, default="unknown")
    count = popoto.Field(type=int, null=True, default=0)
    amount = popoto.Field(type=Decimal, null=True, default=None)
    tags = popoto.Field(type=set, null=True, default=None)


def _make(slug="w1", bio="hello", status="active", count=3, amount=None, tags=None):
    w = Widget(slug=slug, bio=bio, status=status, count=count, amount=amount, tags=tags)
    w.save()
    return w


class IndexedWidget(popoto.Model):
    """KeyField plus an IndexedField and a UniqueField, used to pin that a
    corrupt indexed/unique (non-key) field blocks save() before
    IndexedFieldMixin's eager index-swap EVAL runs -- confirmed correct by
    manual review but previously untested (#573 Tech Debt)."""

    slug = popoto.KeyField()
    email = popoto.UniqueField(type=str)
    status = popoto.IndexedField(type=str, null=True, default="unknown")


def _plant(redis_key, field_name, raw_bytes):
    """Overwrite one hash field with malformed raw bytes, in place."""
    POPOTO_REDIS_DB.hset(redis_key, field_name, raw_bytes)


CORRUPT_PAYLOADS = [
    ("index_pointer", b"$IndexF:M:status:active"),
    ("format_error", b"\xc1"),
    ("truncated_map", b"\x82\xa1a"),
    (
        "bad_datetime_tag",
        msgpack.packb({"as_encodable": True, "__datetime__": "not-a-date"}),
    ),
]
CORRUPT_IDS = [c[0] for c in CORRUPT_PAYLOADS]


# ---------------------------------------------------------------------------
# Non-key field quarantine: five payload shapes x entry points
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload_id,raw_bytes", CORRUPT_PAYLOADS, ids=CORRUPT_IDS)
def test_get_quarantines_corrupt_field(payload_id, raw_bytes, caplog):
    w = _make()
    _plant(w.db_key.redis_key, "bio", raw_bytes)

    with caplog.at_level("WARNING", logger="POPOTO.encoding"):
        loaded = Widget.query.get(slug="w1")

    assert loaded is not None
    assert loaded.bio is None  # declared default
    assert loaded.status == "active"  # healthy sibling still decodes
    assert loaded._corrupt_fields.get("bio") == raw_bytes
    assert any("quarantined field 'bio'" in r.message for r in caplog.records)

    # preserved-bytes criterion
    assert POPOTO_REDIS_DB.hget(w.db_key.redis_key, "bio") == raw_bytes


@pytest.mark.parametrize("payload_id,raw_bytes", CORRUPT_PAYLOADS, ids=CORRUPT_IDS)
def test_filter_lazy_quarantines_on_first_access(payload_id, raw_bytes, caplog):
    w = _make()
    _plant(w.db_key.redis_key, "bio", raw_bytes)

    results = Widget.query.filter(slug="w1")
    loaded = list(results)[0]

    # Lazy: not yet decoded, so no quarantine and no warning until accessed.
    assert loaded._corrupt_fields == {}

    with caplog.at_level("WARNING", logger="POPOTO.encoding"):
        value = loaded.bio

    assert value is None
    assert loaded._corrupt_fields.get("bio") == raw_bytes
    assert loaded.status == "active"
    assert POPOTO_REDIS_DB.hget(w.db_key.redis_key, "bio") == raw_bytes
    assert any("quarantined field 'bio'" in r.message for r in caplog.records)


@pytest.mark.parametrize("payload_id,raw_bytes", CORRUPT_PAYLOADS, ids=CORRUPT_IDS)
def test_all_lazy_quarantines_on_first_access(payload_id, raw_bytes):
    w = _make()
    _plant(w.db_key.redis_key, "bio", raw_bytes)

    loaded = Widget.query.all()[0]
    assert loaded.bio is None
    assert loaded._corrupt_fields.get("bio") == raw_bytes
    assert loaded.status == "active"


@pytest.mark.parametrize("payload_id,raw_bytes", CORRUPT_PAYLOADS, ids=CORRUPT_IDS)
def test_values_omits_quarantined_field(payload_id, raw_bytes, caplog):
    w = _make()
    _plant(w.db_key.redis_key, "bio", raw_bytes)

    with caplog.at_level("WARNING", logger="POPOTO.encoding"):
        rows = Widget.query.filter(slug="w1").values("bio", "status").all()

    assert len(rows) == 1
    row = rows[0]
    # fields_only path: quarantined key is omitted entirely, never a wrong value.
    assert "bio" not in row
    assert row["status"] == "active"
    assert any("quarantined field 'bio'" in r.message for r in caplog.records)
    assert POPOTO_REDIS_DB.hget(w.db_key.redis_key, "bio") == raw_bytes


@pytest.mark.parametrize("payload_id,raw_bytes", CORRUPT_PAYLOADS, ids=CORRUPT_IDS)
def test_get_many_objects_eager_quarantines(payload_id, raw_bytes):
    w = _make()
    _plant(w.db_key.redis_key, "bio", raw_bytes)

    objects = Query.get_many_objects(Widget, {w.db_key.redis_key}, lazy=False)
    assert len(objects) == 1
    obj = objects[0]
    assert obj.bio is None
    assert obj._corrupt_fields.get("bio") == raw_bytes
    assert obj.status == "active"


@pytest.mark.parametrize("payload_id,raw_bytes", CORRUPT_PAYLOADS, ids=CORRUPT_IDS)
def test_get_many_objects_lazy_quarantines_on_access(payload_id, raw_bytes):
    w = _make()
    _plant(w.db_key.redis_key, "bio", raw_bytes)

    objects = Query.get_many_objects(Widget, {w.db_key.redis_key}, lazy=True)
    assert len(objects) == 1
    obj = objects[0]
    assert obj._corrupt_fields == {}  # not decoded yet
    assert obj.bio is None
    assert obj._corrupt_fields.get("bio") == raw_bytes


@pytest.mark.parametrize("payload_id,raw_bytes", CORRUPT_PAYLOADS, ids=CORRUPT_IDS)
def test_async_get_many_objects_quarantines(payload_id, raw_bytes):
    w = _make()
    _plant(w.db_key.redis_key, "bio", raw_bytes)

    async def _run():
        return await Query._async_get_many_objects(
            Widget, {w.db_key.redis_key}, lazy=False
        )

    objects = asyncio.run(_run())
    assert len(objects) == 1
    obj = objects[0]
    assert obj.bio is None
    assert obj._corrupt_fields.get("bio") == raw_bytes
    assert obj.status == "active"


# ---------------------------------------------------------------------------
# Once-per-decode warning (lazy cache)
# ---------------------------------------------------------------------------


def test_warning_fires_once_per_decode_not_per_access(caplog):
    w = _make()
    _plant(w.db_key.redis_key, "bio", b"\xc1")

    loaded = Widget.query.filter(slug="w1")[0]

    with caplog.at_level("WARNING", logger="POPOTO.encoding"):
        _ = loaded.bio
        _ = loaded.bio
        _ = loaded.bio

    warnings = [r for r in caplog.records if "quarantined field 'bio'" in r.message]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# Undecodable field NAME (skipped + warned, does not block save)
# ---------------------------------------------------------------------------


def test_undecodable_field_name_skipped_and_warned(caplog):
    w = _make()
    stray_value = msgpack.packb("stray")
    POPOTO_REDIS_DB.hset(w.db_key.redis_key, b"st\xffatus", stray_value)

    with caplog.at_level("WARNING", logger="POPOTO.encoding"):
        loaded = Widget.query.get(slug="w1")

    assert loaded is not None
    assert loaded.bio == "hello"
    assert loaded.status == "active"
    assert any("skipped undecodable field name" in r.message for r in caplog.records)
    # A non-UTF-8 field name can never match a declared field, so it cannot
    # block a save that only writes declared fields.
    loaded.save()
    assert POPOTO_REDIS_DB.hget(w.db_key.redis_key, b"st\xffatus") == stray_value


def test_undecodable_field_name_recorded_on_lazy_instance():
    w = _make()
    stray_value = msgpack.packb("stray")
    POPOTO_REDIS_DB.hset(w.db_key.redis_key, b"st\xffatus", stray_value)

    loaded = Widget.query.filter(slug="w1")[0]
    assert repr(b"st\xffatus") in loaded._corrupt_fields
    assert loaded._corrupt_fields[repr(b"st\xffatus")] == stray_value


# ---------------------------------------------------------------------------
# save() write-back guard
# ---------------------------------------------------------------------------


def test_lazy_save_refuses_without_touching_poisoned_field():
    """Round-1 blocker regression: pins the post-encode guard placement."""
    w = _make()
    _plant(w.db_key.redis_key, "bio", b"\xc1")

    loaded = Widget.query.filter(slug="w1")[0]
    # Deliberately never touch loaded.bio before saving.
    with pytest.raises(CorruptFieldError, match="bio"):
        loaded.save()

    assert POPOTO_REDIS_DB.hget(w.db_key.redis_key, "bio") == b"\xc1"


def test_repair_on_lazy_instance_succeeds():
    """Round-1 blocker regression: pins the clear inside the lazy-cache branch."""
    w = _make()
    _plant(w.db_key.redis_key, "bio", b"\xc1")

    loaded = Widget.query.filter(slug="w1")[0]
    loaded.bio = "recovered"
    result = loaded.save()
    assert result is not False

    reloaded = Widget.query.get(slug="w1")
    assert reloaded.bio == "recovered"
    assert reloaded._corrupt_fields == {}


def test_partial_save_on_unrelated_field_succeeds():
    """Round-1 blocker regression: pins the _validate_names intersection."""
    w = _make()
    _plant(w.db_key.redis_key, "bio", b"\xc1")

    loaded = Widget.query.get(slug="w1")
    assert loaded.bio is None  # quarantined eagerly

    loaded.status = "archived"
    result = loaded.save(update_fields=["status"])
    assert result is not False

    assert POPOTO_REDIS_DB.hget(w.db_key.redis_key, "bio") == b"\xc1"
    reloaded_status = Widget.query.get(slug="w1")
    assert reloaded_status.status == "archived"
    assert reloaded_status._corrupt_fields.get("bio") == b"\xc1"


def test_full_save_still_refuses_after_partial_save_succeeded():
    """Round-1 blocker regression: pins that scoping did not weaken the guard."""
    w = _make()
    _plant(w.db_key.redis_key, "bio", b"\xc1")

    loaded = Widget.query.get(slug="w1")
    loaded.status = "archived"
    loaded.save(update_fields=["status"])  # succeeds, does not repair bio

    with pytest.raises(CorruptFieldError, match="bio"):
        loaded.save()

    assert POPOTO_REDIS_DB.hget(w.db_key.redis_key, "bio") == b"\xc1"


def test_save_refuses_row_unchanged_then_repair_then_save_works():
    w = _make()
    _plant(w.db_key.redis_key, "bio", b"\xc1")

    loaded = Widget.query.get(slug="w1")

    with pytest.raises(CorruptFieldError):
        loaded.save()

    # Row unchanged after the refusal.
    assert POPOTO_REDIS_DB.hget(w.db_key.redis_key, "bio") == b"\xc1"
    assert POPOTO_REDIS_DB.hget(w.db_key.redis_key, "status") is not None

    loaded.bio = "fixed"
    result = loaded.save()
    assert result is not False

    reloaded = Widget.query.get(slug="w1")
    assert reloaded.bio == "fixed"
    assert reloaded._corrupt_fields == {}


# ---------------------------------------------------------------------------
# delete() stays permitted on a poisoned row
# ---------------------------------------------------------------------------


def test_delete_succeeds_on_poisoned_row_no_orphaned_index_member():
    w = _make(slug="w-del", status="active")
    redis_key = w.db_key.redis_key
    _plant(redis_key, "bio", b"\xc1")

    loaded = Widget.query.get(slug="w-del")
    assert loaded is not None

    deleted = loaded.delete()
    assert deleted

    assert POPOTO_REDIS_DB.exists(redis_key) == 0
    # No orphaned unique-index member for the (indexed) KeyField.
    assert Widget.query.get(slug="w-del") is None
    remaining = Widget.query.filter(slug="w-del").all()
    assert remaining == []


# ---------------------------------------------------------------------------
# Legacy \x00 pointer skip is ordered BEFORE the guarded decode, in all
# three call sites (#573 Tech Debt: verified manually in review, untested)
# ---------------------------------------------------------------------------


def test_legacy_idxset_pointer_skipped_before_decode_in_all_three_paths(caplog):
    """A pre-#476 legacy pointer field name (``field\x00idxset``) must never
    reach the guarded decode seam: the ``\x00`` skip in ``encoding.py`` is
    ordered before ``_decode_field_value`` at all three call sites
    (``fields_only``, eager, and ``_create_lazy_model``) specifically so a
    legacy pointer is never misclassified as a corrupt declared field. A
    genuine corrupt field planted on the *same* row must still be quarantined
    normally -- the two mechanisms are independent.

    If the skip were ordered after the decode instead, ``status\x00idxset``
    would decode its field *name* successfully (``\x00`` is valid UTF-8), fail
    to find a matching KeyField, then fail to msgpack-decode its garbage
    value and get quarantined with a 'quarantined field' warning -- which is
    exactly what this test asserts does NOT happen.
    """
    w = _make()
    pointer_value = b"not-valid-msgpack\xc1"
    POPOTO_REDIS_DB.hset(w.db_key.redis_key, b"status\x00idxset", pointer_value)
    _plant(w.db_key.redis_key, "bio", b"\xc1")

    # -- eager (.get()) --
    with caplog.at_level("WARNING", logger="POPOTO.encoding"):
        loaded = Widget.query.get(slug="w1")
    assert loaded is not None
    assert loaded.bio is None  # genuine corruption still quarantined
    assert loaded._corrupt_fields.get("bio") == b"\xc1"
    assert "status\x00idxset" not in loaded._corrupt_fields
    assert not any("idxset" in r.message for r in caplog.records)

    # -- fields_only (.values()) --
    caplog.clear()
    with caplog.at_level("WARNING", logger="POPOTO.encoding"):
        rows = Widget.query.filter(slug="w1").values("bio", "status").all()
    row = rows[0]
    assert "bio" not in row
    assert row["status"] == "active"
    assert not any(b"idxset" in k for k in row if isinstance(k, bytes))
    assert not any("idxset" in r.message for r in caplog.records)

    # -- lazy (.filter() + first access) --
    caplog.clear()
    lazy_loaded = Widget.query.filter(slug="w1")[0]
    assert lazy_loaded._corrupt_fields == {}  # nothing decoded yet
    with caplog.at_level("WARNING", logger="POPOTO.encoding"):
        assert lazy_loaded.bio is None
    assert lazy_loaded._corrupt_fields.get("bio") == b"\xc1"
    assert "status\x00idxset" not in lazy_loaded._corrupt_fields
    assert not any("idxset" in r.message for r in caplog.records)

    # Raw bytes of the legacy pointer are preserved untouched, on all paths.
    assert (
        POPOTO_REDIS_DB.hget(w.db_key.redis_key, b"status\x00idxset")
        == pointer_value
    )


# ---------------------------------------------------------------------------
# Corrupt IndexedField / UniqueField: save() refuses before the eager
# index-swap EVAL (#573 Tech Debt: verified manually in review, untested)
# ---------------------------------------------------------------------------


def test_corrupt_indexed_and_unique_field_blocks_save_before_index_write():
    """save()'s quarantine guard must run before IndexedFieldMixin's eager
    on_save() EVAL, so a poisoned IndexedField/UniqueField's declared default
    can never land in the live secondary-index Set. Confirmed correct by
    manual review of save()'s call order; this pins it with an assertion on
    Set membership, not just on the raised exception.
    """
    w = IndexedWidget(slug="iw1", email="a@example.com", status="active")
    w.save()
    redis_key = w.db_key.redis_key

    status_field = IndexedWidget._meta.fields["status"]
    email_field = IndexedWidget._meta.fields["email"]
    active_set_key = DB_key(
        status_field.get_special_use_field_db_key(w, "status"), "active"
    ).redis_key
    unknown_set_key = DB_key(
        status_field.get_special_use_field_db_key(w, "status"), "unknown"
    ).redis_key
    email_set_key = DB_key(
        email_field.get_special_use_field_db_key(w, "email"), "a@example.com"
    ).redis_key

    # Sanity: on_save() populated both index Sets on the healthy save.
    assert POPOTO_REDIS_DB.sismember(active_set_key, redis_key)
    assert POPOTO_REDIS_DB.sismember(email_set_key, redis_key)

    _plant(redis_key, "status", b"\xc1")
    _plant(redis_key, "email", b"\xc1")

    loaded = IndexedWidget.query.get(slug="iw1")
    assert loaded.status == "unknown"  # declared default, quarantined
    assert loaded.email is None
    assert set(loaded._corrupt_fields) == {"status", "email"}

    # Scoped saves (rather than a full save()) isolate each field: a full
    # save() would raise ModelException from is_valid() on email's
    # null=False constraint before ever reaching the quarantine guard, which
    # would prove nothing about the guard itself. update_fields=[...] skips
    # is_valid() (see pre_save's partial-save branch) and reaches
    # _raise_if_quarantine_blocks directly, same as
    # test_partial_save_on_unrelated_field_succeeds above.

    # IndexedField: refuses with CorruptFieldError before on_save()'s eager
    # index-swap EVAL -- no member is added under the declared default.
    with pytest.raises(CorruptFieldError, match="status"):
        loaded.save(update_fields=["status"])
    assert not POPOTO_REDIS_DB.sismember(unknown_set_key, redis_key)
    assert POPOTO_REDIS_DB.sismember(active_set_key, redis_key)

    # UniqueField: same guarantee.
    with pytest.raises(CorruptFieldError, match="email"):
        loaded.save(update_fields=["email"])
    assert POPOTO_REDIS_DB.sismember(email_set_key, redis_key)

    # Raw bytes on the hash are untouched by either refused save.
    assert POPOTO_REDIS_DB.hget(redis_key, "status") == b"\xc1"
    assert POPOTO_REDIS_DB.hget(redis_key, "email") == b"\xc1"


# ---------------------------------------------------------------------------
# Corrupt KeyField: raises everywhere, never quarantined, no duplicate hash
# ---------------------------------------------------------------------------


def test_corrupt_keyfield_raises_on_every_entry_point():
    w = _make(slug="w-key")
    redis_key = w.db_key.redis_key
    _plant(redis_key, "slug", b"\xc1")

    before = sorted(POPOTO_REDIS_DB.keys("Widget:*"))

    with pytest.raises(CorruptFieldError, match="slug"):
        Widget.query.get(slug="w-key")

    with pytest.raises(CorruptFieldError, match="slug"):
        list(Widget.query.filter(slug="w-key"))

    with pytest.raises(CorruptFieldError, match="slug"):
        Widget.query.all()

    with pytest.raises(CorruptFieldError, match="slug"):
        Query.get_many_objects(Widget, {redis_key}, lazy=False)

    # lazy=True still eagerly decodes KeyFields (see _create_lazy_model), so
    # it raises immediately too, before any attribute is touched.
    with pytest.raises(CorruptFieldError, match="slug"):
        Query.get_many_objects(Widget, {redis_key}, lazy=True)

    with pytest.raises(CorruptFieldError, match="slug"):
        Widget.query.filter(slug="w-key").values("slug", "status").all()

    async def _run():
        return await Query._async_get_many_objects(Widget, {redis_key}, lazy=False)

    with pytest.raises(CorruptFieldError, match="slug"):
        asyncio.run(_run())

    after = sorted(POPOTO_REDIS_DB.keys("Widget:*"))
    assert before == after  # no second hash, no new index member


# ---------------------------------------------------------------------------
# msgpack-encoded None is not corruption
# ---------------------------------------------------------------------------


def test_msgpack_none_does_not_quarantine():
    w = _make(bio=None)
    assert POPOTO_REDIS_DB.hget(w.db_key.redis_key, "bio") == msgpack.packb(None)

    loaded = Widget.query.get(slug="w1")
    assert loaded.bio is None
    assert loaded._corrupt_fields == {}


# ---------------------------------------------------------------------------
# Kill switch: POPOTO_DECODE_QUARANTINE_DISABLE restores raising
# ---------------------------------------------------------------------------


def test_kill_switch_restores_raising_on_non_key_field(monkeypatch):
    w = _make()
    _plant(w.db_key.redis_key, "bio", b"\xc1")

    monkeypatch.setenv("POPOTO_DECODE_QUARANTINE_DISABLE", "1")

    with pytest.raises(Exception):
        Widget.query.get(slug="w1")


def test_kill_switch_off_by_default_quarantines():
    w = _make()
    _plant(w.db_key.redis_key, "bio", b"\xc1")
    assert os.environ.get("POPOTO_DECODE_QUARANTINE_DISABLE") is None

    loaded = Widget.query.get(slug="w1")
    assert loaded.bio is None
    assert loaded._corrupt_fields.get("bio") == b"\xc1"


# ---------------------------------------------------------------------------
# Ordinary construction still works (pins _corrupt_fields init ordering)
# ---------------------------------------------------------------------------


def test_ordinary_construction_still_works():
    w = Widget(slug="plain", bio="x", status="y", count=5)
    assert w._corrupt_fields == {}
    assert w.bio == "x"
    assert w.count == 5
    w.save()
    reloaded = Widget.query.get(slug="plain")
    assert reloaded.bio == "x"
    assert reloaded._corrupt_fields == {}


# ---------------------------------------------------------------------------
# decode_lazy_field (used unmodified by recipes/memory_lifecycle.py) still
# raises on corrupt bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload_id,raw_bytes", CORRUPT_PAYLOADS, ids=CORRUPT_IDS)
def test_decode_lazy_field_still_raises_unmodified(payload_id, raw_bytes):
    with pytest.raises(Exception):
        decode_lazy_field(raw_bytes)


def test_decode_lazy_field_healthy_value_unaffected():
    assert decode_lazy_field(msgpack.packb("ok")) == "ok"


# ---------------------------------------------------------------------------
# Healthy round trip over every TYPE_ENCODER_DECODERS type: no false positives
# ---------------------------------------------------------------------------


def test_healthy_round_trip_every_custom_type_leaves_no_corrupt_fields():
    import datetime as dt

    class Everything(popoto.Model):
        key = popoto.KeyField()
        dec = popoto.Field(type=Decimal, null=True, default=None)
        tup = popoto.Field(type=tuple, null=True, default=None)
        st = popoto.Field(type=set, null=True, default=None)
        dtm = popoto.Field(type=dt.datetime, null=True, default=None)
        dte = popoto.Field(type=dt.date, null=True, default=None)
        tme = popoto.Field(type=dt.time, null=True, default=None)

    assert set(TYPE_ENCODER_DECODERS.keys()) >= {
        Decimal,
        tuple,
        set,
        dt.datetime,
        dt.date,
        dt.time,
    }

    e = Everything(
        key="e1",
        dec=Decimal("3.14"),
        tup=(1, 2, 3),
        st={1, 2, 3},
        dtm=dt.datetime(2026, 9, 4, 12, 0, 0, tzinfo=dt.timezone.utc),
        dte=dt.date(2026, 9, 4),
        tme=dt.time(12, 30, 0),
    )
    e.save()

    loaded = Everything.query.get(key="e1")
    assert loaded._corrupt_fields == {}
    assert loaded.dec == Decimal("3.14")
    assert loaded.tup == (1, 2, 3)
    assert loaded.st == {1, 2, 3}
    assert loaded.dtm == dt.datetime(2026, 9, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
    assert loaded.dte == dt.date(2026, 9, 4)
    assert loaded.tme == dt.time(12, 30, 0)

    # lazy path too
    lazy_loaded = Everything.query.filter(key="e1")[0]
    for field_name in ("dec", "tup", "st", "dtm", "dte", "tme"):
        getattr(lazy_loaded, field_name)
    assert lazy_loaded._corrupt_fields == {}
