"""``SortedFieldMixin.score``: read one member's score from a sorted index.

Companion to ``count()``/``members()`` (see ``test_sorted_field_reads.py``):
issues a single ``ZSCORE`` against the resolved sorted-set key with the
instance's redis key as the member (#649, part of the #630 series).

Covered here:

- a member present in the index returns its score
- a member absent from the index returns ``None``
- ``partitioned=True`` (the default) resolves the partition-specific key,
  matching ``count()``/``members()``
- ``partitioned=False`` reads the bare unpartitioned key; for a field WITH
  ``partition_by`` set this is a DIFFERENT key that cannot contain the
  member, so it returns ``None`` where ``partitioned=True`` returns the
  real score (issue #658)
- for a field WITHOUT ``partition_by``, both flags resolve to the same key
  and return the same score
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.decaying_sorted_field import DecayingSortedField


class ScorePartitionedDecay(popoto.Model):
    name = popoto.UniqueKeyField()
    category = popoto.KeyField(null=False)
    relevance = DecayingSortedField(partition_by="category")


class ScorePlainSorted(popoto.Model):
    name = popoto.UniqueKeyField()
    score = popoto.SortedField(type=float, default=0.0)


MODELS = [ScorePartitionedDecay, ScorePlainSorted]


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


class TestScore:
    def test_returns_score_for_present_member(self):
        record = ScorePlainSorted(name="p", score=3.5)
        record.save()
        field = ScorePlainSorted._meta.fields["score"]
        assert field.score(record, "score") == 3.5

    def test_returns_none_for_absent_member(self):
        ScorePlainSorted(name="p", score=3.5).save()
        absent = ScorePlainSorted(name="ghost", score=0.0)
        field = ScorePlainSorted._meta.fields["score"]
        assert field.score(absent, "score") is None

    def test_partitioned_default_resolves_partition_key(self):
        record = ScorePartitionedDecay(name="a1", category="alpha")
        record.save()
        field = ScorePartitionedDecay._meta.fields["relevance"]
        expected = field.get_partitioned_sortedset_db_key(record, "relevance").redis_key
        base = field.get_special_use_field_db_key(
            ScorePartitionedDecay, "relevance"
        ).redis_key
        assert expected != base
        assert field.score(record, "relevance") is not None

    def test_partitioned_false_reads_different_key_and_returns_none(self):
        record = ScorePartitionedDecay(name="a1", category="alpha")
        record.save()
        field = ScorePartitionedDecay._meta.fields["relevance"]

        partitioned_key = field.get_partitioned_sortedset_db_key(
            record, "relevance"
        ).redis_key
        bare_key = field.get_special_use_field_db_key(
            ScorePartitionedDecay, "relevance"
        ).redis_key
        assert partitioned_key != bare_key

        assert field.score(record, "relevance", partitioned=True) is not None
        assert field.score(record, "relevance", partitioned=False) is None

    def test_unpartitioned_field_both_flags_agree(self):
        record = ScorePlainSorted(name="p", score=7.0)
        record.save()
        field = ScorePlainSorted._meta.fields["score"]
        partitioned_key = field.get_partitioned_sortedset_db_key(
            record, "score"
        ).redis_key
        bare_key = field.get_special_use_field_db_key(
            ScorePlainSorted, "score"
        ).redis_key
        assert partitioned_key == bare_key
        assert (
            field.score(record, "score", partitioned=True)
            == field.score(record, "score", partitioned=False)
            == 7.0
        )
