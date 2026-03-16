"""Tests for composite_score() — multi-factor retrieval via ZUNIONSTORE.

Tests cover:
- Two-index synergy: decay + confidence
- Three-index synergy: decay + confidence + access frequency
- Four-index synergy: + write filter priority
- CoOccurrence boost synergy test
- Single-index degenerate case
- Empty indexes dict -> QueryException
- Invalid field name -> QueryException
- Field without sorted set index -> QueryException
- Empty result set -> empty list
- Aggregate modes: SUM, MIN, MAX
- min_score filtering
- post_filter callback
- Partition filter integration
- limit=0 -> empty list
"""

import sys
import os
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.access_tracker import AccessTrackerMixin
from src.popoto.fields.write_filter import WriteFilterMixin
from src.popoto.fields.co_occurrence_field import CoOccurrenceField
from src.popoto.models.query import QueryException
from src.popoto.redis_db import POPOTO_REDIS_DB


# --- Test Models ---


class CompositeMemory(AccessTrackerMixin, WriteFilterMixin, popoto.Model):
    """Model with all composite-scorable field types."""

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    importance = popoto.FloatField(default=0.5)
    relevance = DecayingSortedField(base_score_field="importance")
    certainty = ConfidenceField(initial_confidence=0.5)
    associations = CoOccurrenceField(symmetric=True, max_edges=100)

    def compute_filter_score(self):
        return self.importance or 0.0


class SimpleDecayModel(popoto.Model):
    """Model with just a DecayingSortedField for simple tests."""

    name = popoto.UniqueKeyField()
    score = DecayingSortedField()


class PartitionedComposite(popoto.Model):
    """Model with partitioned DecayingSortedField."""

    name = popoto.UniqueKeyField()
    category = popoto.KeyField(null=False)
    relevance = DecayingSortedField(partition_by="category")


class PlainModel(popoto.Model):
    """Model with no sorted set fields."""

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")


class SortedOnlyModel(popoto.Model):
    """Model with a plain SortedField."""

    name = popoto.UniqueKeyField()
    score = popoto.SortedField(type=float, default=0.0)


ALL_MODELS = [
    CompositeMemory,
    SimpleDecayModel,
    PartitionedComposite,
    PlainModel,
    SortedOnlyModel,
]


# --- Setup / Teardown ---


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up Redis keys after each test."""
    yield
    for model_class in ALL_MODELS:
        try:
            model_class.delete_all()
        except Exception:
            pass
    # Clean up any leftover temp keys
    for key in POPOTO_REDIS_DB.keys("$CSQ:*"):
        POPOTO_REDIS_DB.delete(key)


# --- Error case tests ---


class TestCompositeScoreErrors:
    """Test error handling for invalid inputs."""

    def test_empty_indexes_raises(self):
        """Empty indexes dict raises QueryException."""
        with pytest.raises(QueryException, match="non-empty indexes"):
            CompositeMemory.query.composite_score(indexes={})

    def test_invalid_field_name_raises(self):
        """Field name not on model raises QueryException."""
        with pytest.raises(QueryException, match="has no field"):
            CompositeMemory.query.composite_score(
                indexes={"nonexistent_field": 1.0}
            )

    def test_plain_field_raises(self):
        """Field without sorted set index raises QueryException."""
        with pytest.raises(QueryException, match="does not have a sorted set"):
            CompositeMemory.query.composite_score(
                indexes={"content": 1.0}
            )

    def test_priority_without_write_filter_raises(self):
        """'priority' on non-WriteFilterMixin model raises QueryException."""
        with pytest.raises(QueryException, match="WriteFilterMixin"):
            SimpleDecayModel.query.composite_score(
                indexes={"priority": 1.0}
            )

    def test_access_count_without_tracker_raises(self):
        """'access_count' on non-AccessTrackerMixin model raises QueryException."""
        with pytest.raises(QueryException, match="AccessTrackerMixin"):
            SimpleDecayModel.query.composite_score(
                indexes={"access_count": 1.0}
            )

    def test_invalid_aggregate_raises(self):
        """Invalid aggregate mode raises QueryException."""
        with pytest.raises(QueryException, match="aggregate must be"):
            CompositeMemory.query.composite_score(
                indexes={"relevance": 1.0},
                aggregate="INVALID",
            )

    def test_limit_zero_returns_empty(self):
        """limit=0 returns empty list."""
        result = CompositeMemory.query.composite_score(
            indexes={"relevance": 1.0}, limit=0
        )
        assert result == []


class TestCompositeScoreEmpty:
    """Test empty result handling."""

    def test_no_instances_returns_empty(self):
        """Query on model with no instances returns empty list."""
        result = CompositeMemory.query.composite_score(
            indexes={"relevance": 1.0}
        )
        assert result == []

    def test_empty_sorted_sets_returns_empty(self):
        """All indexes empty returns empty list."""
        result = SimpleDecayModel.query.composite_score(
            indexes={"score": 1.0}
        )
        assert result == []


# --- Single-index tests ---


class TestCompositeScoreSingleIndex:
    """Test degenerate case: single index works correctly."""

    def test_single_decay_index(self):
        """Single DecayingSortedField index returns ranked results."""
        a = SimpleDecayModel.create(name="old")
        time.sleep(0.05)
        b = SimpleDecayModel.create(name="new")

        results = SimpleDecayModel.query.composite_score(
            indexes={"score": 1.0}, limit=10
        )
        assert len(results) == 2
        # Both records should be present
        names = {r.name for r in results}
        assert names == {"old", "new"}

    def test_single_sorted_field(self):
        """Single plain SortedField index returns ranked results."""
        SortedOnlyModel.create(name="low", score=10.0)
        SortedOnlyModel.create(name="high", score=90.0)
        SortedOnlyModel.create(name="mid", score=50.0)

        results = SortedOnlyModel.query.composite_score(
            indexes={"score": 1.0}, limit=10
        )
        assert len(results) == 3
        assert results[0].name == "high"
        assert results[1].name == "mid"
        assert results[2].name == "low"


# --- Two-index synergy tests ---


class TestCompositeScoreTwoIndex:
    """Test decay + confidence synergy."""

    def test_high_confidence_recent_beats_low_confidence_old(self):
        """High-confidence recent record outranks low-confidence old record."""
        # Create "old" record
        old = CompositeMemory.create(
            name="old_fact", content="old", importance=0.8
        )
        time.sleep(0.05)
        # Create "new" record
        new = CompositeMemory.create(
            name="new_fact", content="new", importance=0.8
        )

        # Make old have low confidence, new have high confidence
        ConfidenceField.update_confidence(old, "certainty", signal=0.2)
        ConfidenceField.update_confidence(old, "certainty", signal=0.2)
        ConfidenceField.update_confidence(new, "certainty", signal=0.95)
        ConfidenceField.update_confidence(new, "certainty", signal=0.95)

        results = CompositeMemory.query.composite_score(
            indexes={"relevance": 0.5, "certainty": 0.5},
            limit=10,
        )
        assert len(results) >= 2
        # New + high confidence should beat old + low confidence
        assert results[0].name == "new_fact"


# --- Three-index synergy tests ---


class TestCompositeScoreThreeIndex:
    """Test decay + confidence + access frequency synergy."""

    def test_frequently_accessed_gets_boost(self):
        """Frequently accessed records get a boost from access_count."""
        # Create records
        rarely = CompositeMemory.create(
            name="rarely_used", content="r", importance=0.8
        )
        frequently = CompositeMemory.create(
            name="frequently_used", content="f", importance=0.8
        )

        # Set similar confidence
        ConfidenceField.update_confidence(rarely, "certainty", signal=0.7)
        ConfidenceField.update_confidence(frequently, "certainty", signal=0.7)

        # Give frequently_used many accesses
        for _ in range(10):
            frequently.on_read()
        frequently.confirm_access()

        results = CompositeMemory.query.composite_score(
            indexes={
                "relevance": 0.3,
                "certainty": 0.3,
                "access_count": 0.4,
            },
            limit=10,
        )
        assert len(results) >= 2
        # The frequently accessed one should rank higher
        result_names = [r.name for r in results]
        assert result_names.index("frequently_used") < result_names.index(
            "rarely_used"
        )


# --- Four-index synergy tests ---


class TestCompositeScoreFourIndex:
    """Test decay + confidence + access + write filter priority."""

    def test_priority_tagged_ranks_higher(self):
        """Priority-tagged records rank higher with priority weight."""
        # Create a high-priority record (importance=0.9 -> above priority threshold)
        high = CompositeMemory.create(
            name="high_priority", content="hp", importance=0.9
        )
        # Create a low-priority record (importance=0.5 -> normal save, no priority)
        low = CompositeMemory.create(
            name="low_priority", content="lp", importance=0.5
        )

        # Set similar confidence
        ConfidenceField.update_confidence(high, "certainty", signal=0.7)
        ConfidenceField.update_confidence(low, "certainty", signal=0.7)

        results = CompositeMemory.query.composite_score(
            indexes={
                "relevance": 0.25,
                "certainty": 0.25,
                "access_count": 0.1,
                "priority": 0.4,
            },
            limit=10,
        )
        # Both should be in results
        result_names = [r.name for r in results]
        assert "high_priority" in result_names
        # High priority should rank higher due to priority weight
        if "low_priority" in result_names:
            assert result_names.index("high_priority") < result_names.index(
                "low_priority"
            )


# --- CoOccurrence boost tests ---


class TestCompositeScoreCoOccurrence:
    """Test CoOccurrence boost injection."""

    def test_co_occurrence_boost_surfaces_mediocre_record(self):
        """Record with mediocre scores but strong association surfaces."""
        # Create records
        mediocre = CompositeMemory.create(
            name="mediocre", content="m", importance=0.5
        )
        strong = CompositeMemory.create(
            name="strong", content="s", importance=0.9
        )

        # Set similar recency (both just created)
        ConfidenceField.update_confidence(mediocre, "certainty", signal=0.5)
        ConfidenceField.update_confidence(strong, "certainty", signal=0.8)

        # Give mediocre a strong co-occurrence boost
        mediocre_key = mediocre.db_key.redis_key
        strong_key = strong.db_key.redis_key
        co_boost = {mediocre_key: 5.0, strong_key: 0.1}

        results = CompositeMemory.query.composite_score(
            indexes={"relevance": 0.2, "certainty": 0.2},
            co_occurrence_boost=co_boost,
            limit=10,
        )
        assert len(results) >= 2
        # Mediocre should now rank first due to strong co-occurrence boost
        assert results[0].name == "mediocre"


# --- Aggregate mode tests ---


class TestCompositeScoreAggregate:
    """Test SUM, MIN, MAX aggregate modes."""

    def test_sum_aggregate(self):
        """SUM (default) adds scores across indexes."""
        SortedOnlyModel.create(name="a", score=50.0)
        SortedOnlyModel.create(name="b", score=100.0)

        results = SortedOnlyModel.query.composite_score(
            indexes={"score": 1.0}, aggregate="SUM", limit=10
        )
        assert len(results) == 2
        assert results[0].name == "b"

    def test_min_aggregate(self):
        """MIN aggregate takes minimum score across indexes."""
        SortedOnlyModel.create(name="a", score=50.0)
        SortedOnlyModel.create(name="b", score=100.0)

        results = SortedOnlyModel.query.composite_score(
            indexes={"score": 1.0}, aggregate="MIN", limit=10
        )
        assert len(results) == 2
        assert results[0].name == "b"

    def test_max_aggregate(self):
        """MAX aggregate takes maximum score across indexes."""
        SortedOnlyModel.create(name="a", score=50.0)
        SortedOnlyModel.create(name="b", score=100.0)

        results = SortedOnlyModel.query.composite_score(
            indexes={"score": 1.0}, aggregate="MAX", limit=10
        )
        assert len(results) == 2
        assert results[0].name == "b"


# --- min_score tests ---


class TestCompositeScoreMinScore:
    """Test min_score filtering."""

    def test_min_score_filters_low_scores(self):
        """Records below min_score are excluded."""
        SortedOnlyModel.create(name="low", score=5.0)
        SortedOnlyModel.create(name="high", score=100.0)

        results = SortedOnlyModel.query.composite_score(
            indexes={"score": 1.0}, min_score=50.0, limit=10
        )
        assert len(results) == 1
        assert results[0].name == "high"

    def test_min_score_all_below_returns_empty(self):
        """All records below min_score returns empty list."""
        SortedOnlyModel.create(name="a", score=5.0)
        SortedOnlyModel.create(name="b", score=10.0)

        results = SortedOnlyModel.query.composite_score(
            indexes={"score": 1.0}, min_score=999.0, limit=10
        )
        assert results == []


# --- post_filter tests ---


class TestCompositeScorePostFilter:
    """Test post_filter callback."""

    def test_post_filter_excludes_records(self):
        """post_filter callback can exclude specific records."""
        SortedOnlyModel.create(name="keep", score=50.0)
        excluded = SortedOnlyModel.create(name="exclude", score=100.0)
        excluded_key = excluded.db_key.redis_key

        def filter_fn(redis_key, score):
            return redis_key != excluded_key

        results = SortedOnlyModel.query.composite_score(
            indexes={"score": 1.0},
            post_filter=filter_fn,
            limit=10,
        )
        assert len(results) == 1
        assert results[0].name == "keep"

    def test_post_filter_all_excluded_returns_empty(self):
        """Excluding all records returns empty list."""
        SortedOnlyModel.create(name="a", score=50.0)

        results = SortedOnlyModel.query.composite_score(
            indexes={"score": 1.0},
            post_filter=lambda k, s: False,
            limit=10,
        )
        assert results == []


# --- Partition filter tests ---


class TestCompositeScorePartitioned:
    """Test partition filter integration."""

    def test_partitioned_field_requires_partition_filter(self):
        """Partitioned field without partition filter raises QueryException."""
        PartitionedComposite.create(name="a", category="cat1")

        with pytest.raises(QueryException, match="partition filter"):
            PartitionedComposite.query.composite_score(
                indexes={"relevance": 1.0}, limit=10
            )

    def test_partitioned_field_with_filter(self):
        """Partitioned field with correct partition filter works."""
        PartitionedComposite.create(name="a", category="cat1")
        PartitionedComposite.create(name="b", category="cat1")
        PartitionedComposite.create(name="c", category="cat2")

        results = PartitionedComposite.query.filter(
            category="cat1"
        ).composite_score(
            indexes={"relevance": 1.0}, limit=10
        )
        assert len(results) == 2
        names = {r.name for r in results}
        assert names == {"a", "b"}


# --- Limit tests ---


class TestCompositeScoreLimit:
    """Test limit parameter."""

    def test_limit_constrains_results(self):
        """limit parameter constrains number of returned results."""
        for i in range(5):
            SortedOnlyModel.create(name=f"item_{i}", score=float(i * 10))

        results = SortedOnlyModel.query.composite_score(
            indexes={"score": 1.0}, limit=3
        )
        assert len(results) == 3

    def test_limit_larger_than_results(self):
        """limit larger than result set returns all results."""
        SortedOnlyModel.create(name="only_one", score=50.0)

        results = SortedOnlyModel.query.composite_score(
            indexes={"score": 1.0}, limit=100
        )
        assert len(results) == 1


# --- Temp key cleanup tests ---


class TestCompositeScoreTempKeys:
    """Test temp key cleanup."""

    def test_no_leaked_temp_keys(self):
        """No $CSQ: keys remain after query completes."""
        CompositeMemory.create(name="item", content="test", importance=0.8)
        ConfidenceField.update_confidence(
            CompositeMemory.query.get(name="item"), "certainty", signal=0.9
        )

        CompositeMemory.query.composite_score(
            indexes={"relevance": 0.5, "certainty": 0.5},
            limit=10,
        )

        # Check for any leftover $CSQ: keys
        csq_keys = POPOTO_REDIS_DB.keys("$CSQ:*")
        assert len(csq_keys) == 0, f"Leaked temp keys: {csq_keys}"

    def test_temp_keys_cleaned_on_empty_result(self):
        """Temp keys are cleaned even when result is empty."""
        SimpleDecayModel.query.composite_score(
            indexes={"score": 1.0}, limit=10
        )

        csq_keys = POPOTO_REDIS_DB.keys("$CSQ:*")
        assert len(csq_keys) == 0


# --- Convenience method on Query class tests ---


class TestQueryConvenienceMethod:
    """Test the convenience composite_score() method on Query."""

    def test_query_class_has_composite_score(self):
        """Query class exposes composite_score() convenience method."""
        SortedOnlyModel.create(name="item", score=50.0)

        # This uses Query.composite_score (not QueryBuilder.composite_score)
        results = SortedOnlyModel.query.composite_score(
            indexes={"score": 1.0}, limit=10
        )
        assert len(results) == 1
        assert results[0].name == "item"
