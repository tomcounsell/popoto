"""``SortedFieldMixin.count`` / ``members``: read one partition of a sorted index.

The two classmethods are the field-layer answer to "how many members does
this index hold?" and "which members, in score order?". They own the key
building and the bytes-to-str decoding that ``recipes/default_memory.py``
used to do by hand (#630).

Covered here:

- cardinality on a partitioned ``DecayingSortedField`` and an unpartitioned
  ``SortedField``
- ascending order, ``reverse=True`` order, and a ``start``/``stop`` window
- empty index -> ``0`` / ``[]``; ``stop < start`` -> ``[]``
- members are ``str`` (the record's redis key), never bytes
- partitions are isolated from each other
- a ``QueryException`` from the partition key builder propagates untouched
"""

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.sorted_field_mixin import SortedFieldMixin
from src.popoto.models.query import QueryException


class ReadsPartitionedDecay(popoto.Model):
    name = popoto.UniqueKeyField()
    category = popoto.KeyField(null=False)
    relevance = DecayingSortedField(partition_by="category")


class ReadsPlainSorted(popoto.Model):
    name = popoto.UniqueKeyField()
    score = popoto.SortedField(type=float, default=0.0)


MODELS = [ReadsPartitionedDecay, ReadsPlainSorted]


def setup_module():
    for model in MODELS:
        model.delete_all()


def teardown_module():
    for model in MODELS:
        model.delete_all()


@pytest.fixture(autouse=True)
def clean_models():
    for model in MODELS:
        model.delete_all()
    yield
    for model in MODELS:
        model.delete_all()


def _seed_partition(category, names):
    """Save records in ``names`` order with distinct decay timestamps."""
    records = []
    for name in names:
        record = ReadsPartitionedDecay(name=name, category=category)
        record.save()
        records.append(record)
        time.sleep(0.002)
    return records


def _seed_scores(scores):
    """Save one plain record per ``(name, score)`` pair."""
    return [ReadsPlainSorted(name=n, score=s).save() or n for n, s in scores]


def _plain_key(name):
    return ReadsPlainSorted(name=name).db_key.redis_key


class TestCount:
    def test_partitioned_cardinality_per_partition(self):
        first = _seed_partition("alpha", ["a1", "a2", "a3"])
        other = _seed_partition("beta", ["b1"])
        field = ReadsPartitionedDecay._meta.fields["relevance"]
        assert field.count(first[0], "relevance") == 3
        assert field.count(other[0], "relevance") == 1

    def test_unpartitioned_cardinality(self):
        _seed_scores([("p", 1.0), ("q", 2.0)])
        field = ReadsPlainSorted._meta.fields["score"]
        assert field.count(ReadsPlainSorted(name="p"), "score") == 2

    def test_empty_index_is_zero(self):
        field = ReadsPartitionedDecay._meta.fields["relevance"]
        probe = ReadsPartitionedDecay(name="none", category="empty")
        assert field.count(probe, "relevance") == 0

    def test_returns_int(self):
        _seed_scores([("p", 1.0)])
        field = ReadsPlainSorted._meta.fields["score"]
        assert type(field.count(ReadsPlainSorted(name="p"), "score")) is int


class TestMembers:
    def test_ascending_is_score_order(self):
        _seed_scores([("mid", 5.0), ("low", 1.0), ("high", 9.0)])
        field = ReadsPlainSorted._meta.fields["score"]
        got = field.members(ReadsPlainSorted(name="low"), "score")
        assert got == [_plain_key("low"), _plain_key("mid"), _plain_key("high")]

    def test_reverse_is_descending(self):
        _seed_scores([("mid", 5.0), ("low", 1.0), ("high", 9.0)])
        field = ReadsPlainSorted._meta.fields["score"]
        got = field.members(ReadsPlainSorted(name="low"), "score", reverse=True)
        assert got == [_plain_key("high"), _plain_key("mid"), _plain_key("low")]

    def test_window_start_stop(self):
        _seed_scores([("a", 1.0), ("b", 2.0), ("c", 3.0), ("d", 4.0)])
        field = ReadsPlainSorted._meta.fields["score"]
        probe = ReadsPlainSorted(name="a")
        assert field.members(probe, "score", 0, 1) == [
            _plain_key("a"),
            _plain_key("b"),
        ]
        assert field.members(probe, "score", 1, 2) == [
            _plain_key("b"),
            _plain_key("c"),
        ]
        # The recipe's ``excess - 1`` arithmetic: a window of one.
        assert field.members(probe, "score", 0, 0) == [_plain_key("a")]

    def test_reverse_window(self):
        _seed_scores([("a", 1.0), ("b", 2.0), ("c", 3.0)])
        field = ReadsPlainSorted._meta.fields["score"]
        got = field.members(ReadsPlainSorted(name="a"), "score", 0, 1, reverse=True)
        assert got == [_plain_key("c"), _plain_key("b")]

    def test_partitioned_stalest_first(self):
        saved = _seed_partition("alpha", ["a1", "a2", "a3"])
        _seed_partition("beta", ["b1"])
        field = ReadsPartitionedDecay._meta.fields["relevance"]
        expected = [r.db_key.redis_key for r in saved]
        assert field.members(saved[0], "relevance") == expected
        assert field.members(saved[0], "relevance", reverse=True) == expected[::-1]

    def test_empty_index_is_empty_list(self):
        field = ReadsPartitionedDecay._meta.fields["relevance"]
        probe = ReadsPartitionedDecay(name="none", category="empty")
        assert field.members(probe, "relevance") == []

    def test_stop_before_start_is_empty_list(self):
        _seed_scores([("a", 1.0), ("b", 2.0)])
        field = ReadsPlainSorted._meta.fields["score"]
        assert field.members(ReadsPlainSorted(name="a"), "score", 1, 0) == []

    def test_members_are_str(self):
        _seed_scores([("a", 1.0)])
        field = ReadsPlainSorted._meta.fields["score"]
        got = field.members(ReadsPlainSorted(name="a"), "score")
        assert got and all(type(m) is str for m in got)


class TestPartitionResolution:
    """Both reads resolve the key through ``get_partitioned_sortedset_db_key``."""

    @pytest.mark.parametrize("method", ["count", "members"])
    def test_query_exception_from_key_builder_propagates(self, monkeypatch, method):
        def refuse(cls, model_instance, field_name):
            raise QueryException(f"{field_name} field is partitioned.")

        monkeypatch.setattr(
            SortedFieldMixin,
            "get_partitioned_sortedset_db_key",
            classmethod(refuse),
        )
        field = ReadsPartitionedDecay._meta.fields["relevance"]
        probe = ReadsPartitionedDecay(name="x", category="alpha")
        with pytest.raises(QueryException):
            getattr(field, method)(probe, "relevance")

    def test_reads_use_the_same_key_as_the_builder(self, monkeypatch):
        """The key handed to ZCARD is the builder's ``.redis_key`` string.

        The spy goes on the client object the mixin module holds, resolved
        through ``sorted_field_mixin.POPOTO_REDIS_DB`` at call time: that is
        the object ``count`` looks ``zcard`` up on, and it stays the same
        object even after a test elsewhere rebinds ``redis_db.POPOTO_REDIS_DB``.
        """
        from src.popoto.fields import sorted_field_mixin

        client = sorted_field_mixin.POPOTO_REDIS_DB
        saved = _seed_partition("alpha", ["a1"])
        field = ReadsPartitionedDecay._meta.fields["relevance"]
        expected = field.get_partitioned_sortedset_db_key(
            saved[0], "relevance"
        ).redis_key
        seen = []
        real = client.zcard

        def spy(key, *args, **kwargs):
            seen.append(key)
            return real(key, *args, **kwargs)

        monkeypatch.setattr(client, "zcard", spy)
        assert field.count(saved[0], "relevance") == 1
        assert seen == [expected]
