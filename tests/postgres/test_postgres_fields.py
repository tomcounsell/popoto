"""``[PG-only]`` behaviour of the M1.1 field breadth (#759 plan §5 M1.1).

The model-level parity is the conformance suite (gate (b)) and
``tests/test_backend_parity_fields.py``; this file covers what has no Redis
counterpart: the column types and indexes the compiler emits for the new
fields, the ``jsonb`` encoding of the collections, the UNIQUE indexes as the
backstop behind ``pre_save``'s read, ``sample_related_keys`` as ``ORDER BY
random()``, and the documented divergences (an aware ``time``, ``push()`` on
a record that does not exist).
"""

import datetime
import math
from decimal import Decimal

import pytest

import popoto
from popoto.backends import BackendCapabilityError, get_backend
from popoto.backends.postgres.codec import decode_json, encode_json
from popoto.exceptions import ModelException


class PgOwner(popoto.Model):
    handle = popoto.KeyField()


class PgItem(popoto.Model):
    sku = popoto.KeyField()
    status = popoto.IndexedField(type=str, null=True)
    email = popoto.UniqueField(type=str)
    tags = popoto.TagField()
    owner = popoto.Relationship(model=PgOwner, null=True)
    blob = popoto.BytesField(null=True)
    on = popoto.DateField(null=True)
    at = popoto.TimeField(null=True)
    things = popoto.ListField(null=True)
    meta = popoto.DictField(null=True)
    bag = popoto.SetField(null=True)
    pair = popoto.TupleField(null=True)
    recent = popoto.ListField(max_length=2)
    a = popoto.Field(type=str, null=True)
    b = popoto.Field(type=str, null=True)

    class Meta:
        indexes = ((("a", "b"), True), (("status", "a"), False))


def _columns(admin, schema, table):
    rows = admin.execute(
        "SELECT column_name, data_type, udt_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    ).fetchall()
    return {name: (data_type, udt) for name, data_type, udt in rows}


def _indexes(admin, schema, table):
    rows = admin.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = %s AND tablename = %s",
        (schema, table),
    ).fetchall()
    return dict(rows)


def test_column_types_for_the_m1_1_fields(pg, pg_schema, admin):
    PgItem.create(sku="s", email="e", recent=[])
    cols = _columns(admin, pg_schema.name, "pg_item")
    assert cols["status"] == ("text", "text")
    assert cols["email"] == ("text", "text")
    assert cols["tags"] == ("ARRAY", "_text")
    assert cols["owner"] == ("text", "text")
    assert cols["blob"] == ("bytea", "bytea")
    assert cols["on"] == ("date", "date")
    assert cols["at"] == ("time without time zone", "time")
    for name in ("things", "meta", "bag", "pair", "recent"):
        assert cols[name] == ("jsonb", "jsonb"), name


def test_indexes_for_the_m1_1_fields(pg, pg_schema, admin):
    PgItem.create(sku="s", email="e", recent=[])
    idx = _indexes(admin, pg_schema.name, "pg_item")
    assert "USING btree (status)" in idx["pg_item__status__idx"]
    assert idx["pg_item__uniq__email__idx"].startswith("CREATE UNIQUE INDEX")
    assert "USING gin (tags)" in idx["pg_item__tags__idx"]
    assert "USING btree (owner)" in idx["pg_item__owner__idx"]
    assert idx["pg_item__meta__a__b__idx"].startswith("CREATE UNIQUE INDEX")
    assert "(a, b)" in idx["pg_item__meta__a__b__idx"]
    assert not idx["pg_item__meta__status__a__idx"].startswith("CREATE UNIQUE")
    # The scalar fields with no index option get none.
    assert not any(
        name.startswith(("pg_item__blob", "pg_item__things")) for name in idx
    )


def test_relationship_column_holds_the_target_pk(pg, pg_schema, admin):
    owner = PgOwner.create(handle="h")
    PgItem.create(sku="s", email="e", owner=owner, recent=[])
    (stored,) = admin.execute(
        f'SELECT owner FROM "{pg_schema.name}".pg_item'
    ).fetchone()
    assert stored == owner.pk == "PgOwner:h"


def test_tags_are_a_normalised_text_array(pg, pg_schema, admin):
    PgItem.create(sku="s", email="e", tags={"b", "a", 2}, recent=[])
    PgItem.create(sku="t", email="f", recent=[])
    rows = dict(
        admin.execute(f'SELECT sku, tags FROM "{pg_schema.name}".pg_item').fetchall()
    )
    assert rows == {"s": ["2", "a", "b"], "t": []}


def test_collections_are_json_documents(pg, pg_schema, admin):
    PgItem.create(
        sku="s",
        email="e",
        things=[b"\x00\xff", math.inf, (1, 2)],
        meta={1: "int key", "k": None},
        bag={3},
        pair=(1, "x"),
        recent=[Decimal("2.5")],
    )
    row = admin.execute(
        f'SELECT things, meta, bag, pair, recent FROM "{pg_schema.name}".pg_item'
    ).fetchone()
    things, meta, bag, pair, recent = row
    assert things[2] == [1, 2] and things[0]["__pg_bytes__"] is True
    assert meta["__pg_map__"] is True
    assert bag == [3] and pair == [1, "x"]
    assert recent == [{"__Decimal__": True, "as_encodable": "2.5"}]
    loaded = PgItem.query.get(sku="s")
    assert loaded.things == [b"\x00\xff", math.inf, [1, 2]]
    assert loaded.meta == {1: "int key", "k": None}
    assert loaded.bag == {3} and loaded.pair == (1, "x")
    assert loaded.recent == [Decimal("2.5")]


def test_codec_refuses_what_msgpack_refuses():
    with pytest.raises(TypeError):
        encode_json(list, [Decimal("1")])
    with pytest.raises(TypeError):
        encode_json(dict, {"s": {1, 2}})
    assert decode_json(tuple, encode_json(tuple, (1, (2, 3)))) == (1, [2, 3])


def test_unique_index_is_the_backstop_behind_the_pre_save_read(pg):
    """Inside one transaction pre_save's read (autocommit, outside it) cannot
    see the first row; the UNIQUE index catches the second, with the same
    text, and the whole unit rolls back."""
    with pytest.raises(
        ModelException,
        match="^Unique constraint violated: email=e already exists on another instance$",
    ):
        with pg.transaction() as uow:
            PgItem(sku="s", email="e", recent=[]).save(pipeline=uow)
            PgItem(sku="t", email="e", recent=[]).save(pipeline=uow)
    assert PgItem.query.count() == 0


def test_meta_unique_index_is_the_backstop_too(pg):
    with pytest.raises(
        ModelException,
        match=r"^Unique index violation on \('a', 'b'\): \(x, y\) already exists$",
    ):
        with pg.transaction() as uow:
            PgItem(sku="s", email="e", a="x", b="y", recent=[]).save(pipeline=uow)
            PgItem(sku="t", email="f", a="x", b="y", recent=[]).save(pipeline=uow)


def test_sample_related_keys_orders_by_random(pg):
    owner = PgOwner.create(handle="h")
    for sku in ("p1", "p2", "p3"):
        PgItem.create(sku=sku, email=sku, owner=owner, recent=[])
    seen = {
        popoto.Relationship.sample_related_keys(PgItem, "owner", owner.pk, 1)[0]
        for _ in range(40)
    }
    # 40 single draws from three members: all three appear unless the order
    # is not random (P(miss one) < 3 * (2/3)**40, about 3e-7).
    assert seen == {"PgItem:p1", "PgItem:p2", "PgItem:p3"}


def test_an_aware_time_is_refused(pg):
    tz = datetime.timezone(datetime.timedelta(hours=7))
    with pytest.raises(ValueError, match="wall-clock time only"):
        PgItem.create(sku="s", email="e", at=datetime.time(9, tzinfo=tz), recent=[])
    item = PgItem.create(sku="s", email="e", at=datetime.time(9, 30), recent=[])
    assert PgItem.query.get(sku="s").at == datetime.time(9, 30)
    assert item.at == datetime.time(9, 30)


def test_push_on_a_record_that_no_longer_exists_raises(pg):
    item = PgItem.create(sku="s", email="e", recent=[])
    PgItem.query.get(sku="s").delete()
    with pytest.raises(ModelException, match="does not exist"):
        item.recent.push(1)


def test_push_is_one_atomic_update(pg):
    item = PgItem.create(sku="s", email="e", recent=[1])
    item.recent.push(2)
    item.recent.push(3)
    assert item.recent == [3, 2]
    assert PgItem.query.get(sku="s").recent == [3, 2]


def test_unsupported_index_types_are_refused_at_declaration():
    with pytest.raises(BackendCapabilityError, match="indexed field needs a scalar"):

        class PgBadIndex(popoto.Model):
            key = popoto.KeyField()
            things = popoto.IndexedField(type=list)

            class Meta:
                backend = "postgres"


def test_unregistered_field_calls_are_refused(pg):
    """Every field kind's adapters exist since #759 M5, so an unregistered
    ``(field, op)`` is refused by name rather than by milestone."""
    PgItem.create(sku="s", email="e", recent=[])
    spec = PgItem._meta.spec
    with pytest.raises(BackendCapabilityError, match="has no adapter"):
        get_backend(PgItem).field_call(spec, "things", "push")
