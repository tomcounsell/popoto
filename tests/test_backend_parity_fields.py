"""Field-breadth edges where Postgres columns and Redis indexes could disagree
(#759 M1.1).

Runs on both conformance legs. Each case is one the Postgres compiler or
renderer handles on purpose: an indexed lookup matching by the value's key
string, negation over ``NULL`` on an indexed field, tag lookups given
non-string tags, lazy versus eager ``Relationship`` reads, chained
relationship lookups, nested collection round trips, uniqueness messages,
``sample_related_keys``' count contract, and deleting a record whose
in-memory tag value is malformed (#744 B1).
"""

import datetime
from decimal import Decimal

import pytest

import popoto
from popoto import Q
from popoto.backends.types import BackendCapabilityError
from popoto.exceptions import ModelException
from popoto.models.query import QueryException

pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]


class ParityAuthor(popoto.Model):
    name = popoto.KeyField()
    country = popoto.IndexedField(type=str, null=True)


class ParityBook(popoto.Model):
    title = popoto.KeyField()
    status = popoto.IndexedField(type=str, null=True)
    isbn = popoto.UniqueField(type=str)
    tags = popoto.TagField()
    author = popoto.Relationship(model=ParityAuthor, null=True)


class ParityLedger(popoto.Model):
    entry = popoto.KeyField()
    debit = popoto.Field(type=str, null=True)
    credit = popoto.Field(type=str, null=True)
    payload = popoto.DictField(null=True)
    items = popoto.ListField(null=True)
    recent = popoto.ListField(max_length=3)

    class Meta:
        indexes = ((("debit", "credit"), True),)


def keys(results):
    return sorted(r.title for r in results)


@pytest.fixture
def books():
    tolkien = ParityAuthor.create(name="tolkien", country="uk")
    le_guin = ParityAuthor.create(name="leguin", country="us")
    ParityBook.create(
        title="hobbit", status="5", isbn="1", tags=["a", "1"], author=tolkien
    )
    ParityBook.create(title="lotr", status=None, isbn="2", tags=["a"], author=tolkien)
    ParityBook.create(title="earthsea", status="x", isbn="3", author=le_guin)
    ParityBook.create(title="orphan", status="x", isbn="4", tags=["b"])
    return tolkien, le_guin


def test_indexed_lookup_matches_by_key_string(books):
    assert keys(ParityBook.query.filter(status=5)) == ["hobbit"]
    assert keys(ParityBook.query.filter(status__in=[5, "x"])) == [
        "earthsea",
        "hobbit",
        "orphan",
    ]


def test_indexed_negation_keeps_null_rows(books):
    assert keys(ParityBook.query.filter(~Q(status="x"))) == ["hobbit", "lotr"]
    assert keys(ParityBook.query.filter(status__isnull=True)) == ["lotr"]


def test_tag_lookups_render_non_string_tags_as_strings(books):
    assert keys(ParityBook.query.filter(tags__contains=1)) == ["hobbit"]
    assert keys(ParityBook.query.filter(tags__any=[1, "b"])) == ["hobbit", "orphan"]
    assert keys(ParityBook.query.filter(tags__all=["a", 1])) == ["hobbit"]


def test_tag_negation_and_or_keep_untagged_rows(books):
    assert keys(ParityBook.query.filter(~Q(tags__contains="a"))) == [
        "earthsea",
        "orphan",
    ]
    either = ParityBook.query.filter(Q(tags__contains="b") | Q(status="5"))
    assert keys(either) == ["hobbit", "orphan"]


def test_relationship_filter_and_chained_lookup(books):
    tolkien, le_guin = books
    assert keys(ParityBook.query.filter(author=tolkien)) == ["hobbit", "lotr"]
    assert keys(ParityBook.query.filter(author=le_guin, status="x")) == ["earthsea"]
    with pytest.raises(QueryException, match="expects model instance"):
        list(ParityBook.query.filter(author="ParityAuthor:tolkien"))


def test_chained_relationship_lookup_is_a_documented_divergence(
    books, backend_is_redis
):
    """``author__country="uk"``: Redis's ``Relationship.filter_query`` asks the
    related field for its matching keys, gets ``bytes`` back, and fails on
    ``.db_key`` -- a pre-existing Redis bug left as it is (plan Direction 2).
    Postgres resolves the related query's keys and matches them
    (docs/features/postgres-backend.md, "Documented divergences"). Both are
    pinned so neither changes silently."""
    if backend_is_redis:
        with pytest.raises(AttributeError, match="db_key"):
            list(ParityBook.query.filter(author__country="uk"))
    else:
        assert keys(ParityBook.query.filter(author__country="uk")) == [
            "hobbit",
            "lotr",
        ]


class ParityStamp(popoto.Model):
    code = popoto.KeyField()
    at = popoto.IndexedField(type=datetime.datetime, null=True)
    pair = popoto.TupleField(null=True)


def test_indexed_pattern_lookup_on_a_datetime_matches_the_key_string():
    """Redis matches ``__startswith`` against the canonical key rendering
    (``…T12:00:00.000000Z``); Postgres renders the column the same way
    (``to_char`` in UTC), so the text cast's space form matches on neither."""
    ParityStamp.create(code="s", at=datetime.datetime(2026, 1, 1, 12, 0))
    found = [s.code for s in ParityStamp.query.filter(at__startswith="2026-01-01T")]
    assert found == ["s"]
    found = [s.code for s in ParityStamp.query.filter(at__endswith=":00.000000Z")]
    assert found == ["s"]
    found = [s.code for s in ParityStamp.query.filter(at__startswith="2026-01-01 ")]
    assert found == []


def test_collection_equality_is_a_documented_divergence(backend_is_redis):
    """Redis compares a plain collection field by Python equality after
    hydration, where a tuple never equals a list; Postgres compares the
    stored JSON documents. Equal-typed values match on both."""
    ParityStamp.create(code="s", pair=(1, "x"))
    assert [s.code for s in ParityStamp.query.filter(pair=(1, "x"))] == ["s"]
    by_list = [s.code for s in ParityStamp.query.filter(pair=[1, "x"])]
    assert by_list == ([] if backend_is_redis else ["s"])


def test_relationship_is_lazy_on_filter_and_eager_on_get(books):
    (lazy,) = ParityBook.query.filter(title="hobbit")
    assert lazy.author == "ParityAuthor:tolkien"
    eager = ParityBook.query.get(title="hobbit")
    assert isinstance(eager.author, ParityAuthor) and eager.author.country == "uk"
    assert ParityBook.query.get(title="orphan").author is None


def test_saving_a_relationship_to_the_wrong_model_raises(books):
    book = ParityBook(title="bad", isbn="9", author=ParityLedger(entry="e"))
    with pytest.raises(ModelException):
        book.save()


def test_unique_field_conflict_message_and_ignore_errors(books):
    with pytest.raises(
        ModelException,
        match=r"^Unique constraint violated: isbn=1 already exists on another instance$",
    ):
        ParityBook.create(title="copy", isbn="1")
    assert ParityBook(title="copy", isbn="1").save(ignore_errors=True) is False
    hobbit = ParityBook.query.get(title="hobbit")
    hobbit.status = "6"
    hobbit.save()  # its own value is not a conflict
    assert keys(ParityBook.query.filter(isbn="1")) == ["hobbit"]


def test_meta_unique_index_conflict_message():
    ParityLedger.create(entry="e1", debit="A", credit="B", recent=[])
    with pytest.raises(
        ModelException,
        match=r"^Unique index violation on \('debit', 'credit'\): \(A, B\) already exists$",
    ):
        ParityLedger.create(entry="e2", debit="A", credit="B", recent=[])
    ParityLedger.create(entry="e3", debit=None, credit="B", recent=[])
    ParityLedger.create(entry="e4", debit=None, credit="B", recent=[])


def test_collections_round_trip_as_msgpack_would():
    ParityLedger.create(
        entry="e1",
        payload={"pair": (1, 2), "when": {"n": [1.5, None, True]}},
        items=[(1, 2), "x", 3],
        recent=[],
    )
    loaded = ParityLedger.query.get(entry="e1")
    assert loaded.payload == {"pair": [1, 2], "when": {"n": [1.5, None, True]}}
    assert loaded.items == [[1, 2], "x", 3]


def test_capped_push_keeps_element_types_and_the_cap():
    ledger = ParityLedger.create(entry="e1", recent=[])
    for value in (Decimal("1.5"), datetime.date(2026, 1, 2), (1, 2), "s"):
        ledger.recent.push(value)
    loaded = ParityLedger.query.get(entry="e1")
    assert loaded.recent == ["s", (1, 2), datetime.date(2026, 1, 2)]
    assert isinstance(loaded.recent[1], tuple)


def test_sample_related_keys_count_contract(books):
    tolkien, _ = books
    sample = popoto.Relationship.sample_related_keys(
        ParityBook, "author", tolkien.db_key.redis_key, 5
    )
    assert sorted(sample) == ["ParityBook:hobbit", "ParityBook:lotr"]
    repeated = popoto.Relationship.sample_related_keys(
        ParityBook, "author", tolkien.db_key.redis_key, -5
    )
    assert len(repeated) == 5
    assert set(repeated) <= {"ParityBook:hobbit", "ParityBook:lotr"}


def test_delete_with_a_malformed_in_memory_tag_value(books):
    """#744 B1: a stored record whose in-memory tags were overwritten with a
    malformed value still deletes. Redis reads the pointer side key and never
    normalises the value; Postgres deletes the row by key."""
    book = ParityBook(title="hobbit", isbn="1")
    book.tags = "oops"
    assert book.delete() is True
    assert ParityBook.query.get(title="hobbit") is None
    assert keys(ParityBook.query.filter(tags__contains="1")) == []


# -- value forms, patterns, ordering and capped lists (#770 review) ----------


class ParityOwner(popoto.Model):
    name = popoto.KeyField()


class ParityTyped(popoto.Model):
    name = popoto.KeyField()
    s = popoto.IndexedField(type=str, null=True)
    i = popoto.IndexedField(type=int, null=True)
    f = popoto.IndexedField(type=float, null=True)
    b = popoto.IndexedField(type=bool, null=True)
    dec = popoto.IndexedField(type=Decimal, null=True)
    dtm = popoto.IndexedField(type=datetime.datetime, null=True)
    dte = popoto.IndexedField(type=datetime.date, null=True)
    tim = popoto.TimeField(null=True)
    day = popoto.DateField(null=True)
    lst = popoto.ListField(null=True)
    owner = popoto.Relationship(model=ParityOwner, null=True)
    cap = popoto.ListField(max_length=3)


UTC = datetime.timezone.utc


@pytest.fixture
def typed():
    owner = ParityOwner.create(name="o1")
    ParityTyped.create(
        name="r1",
        s="Nope",
        i=1,
        f=1.0,
        b=True,
        dec=Decimal("1.50"),
        dtm=datetime.datetime(2026, 1, 1, 12, tzinfo=UTC),
        dte=datetime.date(2026, 1, 1),
        tim=datetime.time(1, 2, 3),
        day=datetime.date(2026, 1, 1),
        lst=[1, 2],
        owner=owner,
        cap=[1, "a"],
    )
    ParityTyped.create(
        name="r2", s=None, i=2, f=2.5, b=False, dec=Decimal("2"), lst=[2], cap=[]
    )
    ParityTyped.create(
        name="r3",
        s="x",
        dec=Decimal("1.5"),
        dte=datetime.date(2025, 6, 30),
        day=datetime.date(2025, 6, 30),
        lst=[],
        cap=[3],
    )


def typed_names(*args, **kwargs):
    return sorted(r.name for r in ParityTyped.query.filter(*args, **kwargs))


def typed_order(field):
    every = ["r1", "r2", "r3"]
    return [r.name for r in ParityTyped.query.filter(name__in=every, order_by=field)]


def test_in_with_mixed_numeric_types_binds_as_the_column_type(typed):
    """#770 review blocker 1: psycopg refuses a list of mixed types, so each
    ``__in`` element is cast to the column's type first (an integer column
    drops a non-integral number). These value forms agree on both legs."""
    assert typed_names(i__in=[1, Decimal("2")]) == ["r1", "r2"]
    assert typed_names(i__in=[1, 2.5]) == ["r1"]
    assert typed_names(f__in=[1.0, Decimal("2.5")]) == ["r1", "r2"]
    assert typed_names(dec__in=[Decimal("2"), 7.5]) == ["r2"]


def test_indexed_string_filter_matches_the_stored_key_string(typed):
    """A string that is a ``bool``/``date``/``datetime`` value's key string
    finds it on both legs (Redis: same set; Postgres: parsed only when it
    renders back byte for byte)."""
    assert typed_names(b="True") == ["r1"]
    assert typed_names(b="False") == ["r2"]
    assert typed_names(b="true") == []
    assert typed_names(b__in=["True", 0]) == ["r1"]
    assert typed_names(dte="2026-01-01") == ["r1"]
    assert typed_names(dte="2026-1-1") == []
    assert typed_names(dtm="2026-01-01T12:00:00.000000Z") == ["r1"]
    assert typed_names(dtm="2026-01-01 12:00:00+00:00") == []
    assert typed_names(i="1") == ["r1"]
    assert typed_names(f="1.0") == ["r1"]


def test_an_aware_time_never_equals_a_stored_time(typed):
    """Redis compares a ``TimeField`` by Python equality, where an aware
    ``time`` never equals a naive one; Postgres matches nothing for it
    rather than stripping the offset."""
    assert typed_names(tim=datetime.time(1, 2, 3)) == ["r1"]
    assert typed_names(tim=datetime.time(1, 2, 3, tzinfo=UTC)) == []


def test_numeric_indexed_equality_across_value_forms_is_a_documented_divergence(
    typed, backend_is_redis
):
    """Redis looks an ``IndexedField`` up by the filter value's key string, so
    ``i=1.0`` (``"1.0"``) misses a stored ``1`` (``"1"``) -- even though the
    save path coerces ``i=1.0`` to ``1``, so a record cannot be found by the
    value it was saved with. Postgres compares numbers, as Python equality
    does. A Redis query-layer bug, left as it is (plan gate (a))."""
    redis = backend_is_redis
    assert typed_names(i=1.0) == ([] if redis else ["r1"])
    assert typed_names(i=True) == ([] if redis else ["r1"])
    assert typed_names(f=1) == ([] if redis else ["r1"])
    assert typed_names(f="1") == ([] if redis else ["r1"])
    assert typed_names(dec=Decimal("1.5")) == (["r3"] if redis else ["r1", "r3"])
    assert typed_names(i__in=[1, 2.0]) == (["r1"] if redis else ["r1", "r2"])
    ParityTyped.create(name="r4", i=7.0, f=9, cap=[])
    assert typed_names(i=7.0) == ([] if redis else ["r4"])
    assert typed_names(f=9) == ([] if redis else ["r4"])


def test_indexed_pattern_lookup_renders_the_key_string(typed):
    """``__startswith``/``__endswith`` on a non-text indexed column match the
    key string Redis stores: ``1.0``, ``True``, ``2026-01-01``, ``1.50``."""
    assert typed_names(f__endswith="0") == ["r1"]
    assert typed_names(f__startswith="1.") == ["r1"]
    assert typed_names(b__startswith="T") == ["r1"]
    assert typed_names(b__startswith="t") == []
    assert typed_names(dte__startswith="2026-01") == ["r1"]
    assert typed_names(dec__endswith="50") == ["r1"]
    assert typed_names(i__startswith="2") == ["r2"]


def test_float_pattern_in_the_exponent_band_is_a_documented_divergence(
    backend_is_redis,
):
    """Python writes ``1e15`` as ``1000000000000000.0`` and switches to
    exponent form at ``1e16``; Postgres switches at ``1e15``."""
    ParityTyped.create(name="big", f=1e15, cap=[])
    found = typed_names(f__startswith="1000")
    assert found == (["big"] if backend_is_redis else [])


def test_pattern_lookup_matching_none_is_a_documented_divergence(
    typed, backend_is_redis
):
    """Redis globs the index keys, and a ``None`` value's set is named
    ``…:None``, so ``__startswith="No"`` matches a row holding ``None``. A
    Redis query-layer bug; Postgres's ``LIKE`` never matches ``NULL``."""
    redis = backend_is_redis
    assert typed_names(s__startswith="No") == (["r1", "r2"] if redis else ["r1"])
    assert typed_names(s__endswith="ne") == (["r2"] if redis else [])
    assert typed_names(s__startswith="") == (
        ["r1", "r2", "r3"] if redis else ["r1", "r3"]
    )
    assert typed_names(f__startswith="N") == (["r3"] if redis else [])


def test_order_by_a_collection_field_is_refused_on_postgres(typed, backend_is_redis):
    """Redis sorts hydrated lists as Python does (``[] < [1, 2] < [2]``);
    ``jsonb`` orders by length first. Rather than a silently different order,
    Postgres refuses."""
    if backend_is_redis:
        assert typed_order("lst") == ["r3", "r1", "r2"]
    else:
        with pytest.raises(BackendCapabilityError, match="collection field"):
            typed_order("lst")


@pytest.mark.parametrize("field", ["dte", "day"])
def test_order_by_a_date_holding_null_is_a_documented_divergence(
    typed, backend_is_redis, field
):
    """Redis sorts ``None`` as the type's zero, ``date()``, which raises.
    Postgres sorts ``NULL`` first. A Redis query-layer bug."""
    if backend_is_redis:
        with pytest.raises(TypeError, match="year"):
            typed_order(field)
    else:
        assert typed_order(field) == ["r2", "r3", "r1"]
        assert typed_order("-" + field) == ["r1", "r3", "r2"]


def test_order_by_a_relationship_is_a_documented_divergence(typed, backend_is_redis):
    """Redis sorts with the related model's class as the zero value and fails
    on ``_meta``. Postgres orders by the stored key string, ``NULL`` first.
    A Redis query-layer bug."""
    if backend_is_redis:
        with pytest.raises(AttributeError, match="_meta"):
            typed_order("owner")
    else:
        assert typed_order("owner") == ["r2", "r3", "r1"]


def test_capped_list_on_lazy_reads_is_a_documented_divergence(typed, backend_is_redis):
    """A capped ``ListField`` lives in its own Redis list key, which only the
    eager decode (``get``) loads: ``filter()``/``all()`` leave it ``None`` and
    ``values=`` drops it. Postgres stores it in the row, so every read returns
    it. A Redis query-layer bug; ``get`` agrees on both."""
    assert list(ParityTyped.query.get(name="r1").cap) == [1, "a"]
    lazy = {r.name: r.cap for r in ParityTyped.query.filter(name__in=["r1", "r3"])}
    every = {r.name: r.cap for r in ParityTyped.query.all()}
    (row,) = ParityTyped.query.filter(name="r1", values=("name", "cap"))
    if backend_is_redis:
        assert lazy == {"r1": None, "r3": None}
        assert set(every.values()) == {None}
        assert row == {"name": "r1"}
    else:
        assert {k: list(v) for k, v in lazy.items()} == {"r1": [1, "a"], "r3": [3]}
        assert list(every["r2"]) == []
        assert row["name"] == "r1" and list(row["cap"]) == [1, "a"]
