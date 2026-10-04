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


def test_indexed_pattern_lookup_on_a_non_text_column_is_a_documented_divergence(
    backend_is_redis,
):
    """Redis matches ``__startswith`` against the canonical key rendering
    (``…T12:00:00.000000Z``); Postgres against the column's text cast
    (``… 12:00:00+00``). Pinned so neither changes silently."""
    ParityStamp.create(code="s", at=datetime.datetime(2026, 1, 1, 12, 0))
    found = [s.code for s in ParityStamp.query.filter(at__startswith="2026-01-01T")]
    assert found == (["s"] if backend_is_redis else [])
    found = [s.code for s in ParityStamp.query.filter(at__startswith="2026-01-01 ")]
    assert found == ([] if backend_is_redis else ["s"])


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
