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


def test_score_uses_pk_not_a_recomputed_key_for_a_mutated_instance():
    """The ZSCORE member is ``pk``, so an in-memory KeyField edit cannot lose it.

    ``pk`` is ``_redis_key or db_key.redis_key`` -- the expression the recipe
    call site this method replaced used. The two agree for any record that has
    not been mutated since it was loaded, so a wire capture cannot tell them
    apart; they diverge exactly here, for a record whose KeyField was changed
    in memory with no intervening save. ``pk`` still names the member that is
    actually in the Sorted Set; a recomputed ``db_key.redis_key`` names one
    that was never added, and would silently read ``None``.
    """
    record = ScorePlainSorted(name="mutated-in-memory", score=7.5)
    record.save()

    assert popoto.SortedField.score(record, "score") == 7.5

    # Mutate the KeyField in memory only -- no save(). db_key.redis_key now
    # recomputes to a key that was never written; pk still holds the saved one.
    record.name = "a-different-name"
    assert record.pk != record.db_key.redis_key

    assert popoto.SortedField.score(record, "score") == 7.5


def test_score_follows_a_rebound_global_client(monkeypatch):
    """``score()`` resolves the client per call, not from an import snapshot.

    Its sibling readers in the same mixin still hold the module-level name
    imported at load time, which ``set_REDIS_DB_settings()`` rebinds without
    updating (#655, which tracks the remaining call sites). This method is
    new, so it takes the accessor and this test pins that choice.
    """
    record = ScorePlainSorted(name="p", score=3.5)
    record.save()
    field = ScorePlainSorted._meta.fields["score"]

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

    assert field.score(record, "score") == 3.5
    assert seen == ["ZSCORE"]
