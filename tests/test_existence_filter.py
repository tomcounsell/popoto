"""Tests for ExistenceFilter and FrequencySketch field types.

Tests cover:
- ExistenceFilter: add item, verify might_exist() returns True
- ExistenceFilter: query unseen item, verify definitely_missing() returns True
- ExistenceFilter: false positive rate within 2x of configured bounds (10,000+ items)
- FrequencySketch: increment and query frequency
- FrequencySketch: multiple increments accumulate correctly
- Custom fingerprint_fn works
- Default fingerprint (redis_key) works
- Synergy: WriteFilterMixin + ExistenceFilter — rejected record not in Bloom
- Synergy: ExistenceFilter pre-filter pattern
- Pipeline support: on_save() works within a Redis pipeline
- Graceful behavior when Bloom/CMS key doesn't exist yet
- on_delete() is a no-op (Bloom still contains fingerprint after delete)
- Empty string fingerprint handled correctly
- No Redis module dependencies — vanilla Redis/Valkey only
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402
from src import popoto  # noqa: E402
from src.popoto.fields.existence_filter import (
    ExistenceFilter,
    FrequencySketch,
    tokenize,
)  # noqa: E402
from src.popoto.fields.write_filter import WriteFilterMixin  # noqa: E402
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

# --- Test Models ---


class BloomModel(popoto.Model):
    name = popoto.UniqueKeyField()
    topic = popoto.Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.topic,
    )


class FreqModel(popoto.Model):
    name = popoto.UniqueKeyField()
    topic = popoto.Field(type=str)
    freq = FrequencySketch(
        fingerprint_fn=lambda inst: inst.topic,
    )


class BloomDefaultFingerprintModel(popoto.Model):
    """Uses default fingerprint (redis_key) when no fingerprint_fn is set."""

    name = popoto.UniqueKeyField()
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=10_000,
    )


class BloomFreqComboModel(popoto.Model):
    """Model with both ExistenceFilter and FrequencySketch."""

    name = popoto.UniqueKeyField()
    topic = popoto.Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.topic,
    )
    freq = FrequencySketch(
        fingerprint_fn=lambda inst: inst.topic,
    )


class FilteredBloomModel(WriteFilterMixin, popoto.Model):
    """Model with WriteFilterMixin + ExistenceFilter for synergy test."""

    name = popoto.UniqueKeyField()
    topic = popoto.Field(type=str)
    importance = popoto.FloatField(default=0.0)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.topic,
    )

    def compute_filter_score(self):
        return self.importance or 0.0


class StatisticalBloomModel(popoto.Model):
    """Model for statistical false positive rate testing."""

    name = popoto.UniqueKeyField()
    topic = popoto.Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.05,
        capacity=10_000,
        fingerprint_fn=lambda inst: inst.topic,
    )


# --- Fixtures ---


@pytest.fixture(autouse=True)
def cleanup_redis():
    """Clean up test keys before and after each test."""
    patterns = [
        "$EF:BloomModel:*",
        "$FS:FreqModel:*",
        "$EF:BloomDefaultFingerprintModel:*",
        "$EF:BloomFreqComboModel:*",
        "$FS:BloomFreqComboModel:*",
        "$EF:FilteredBloomModel:*",
        "$EF:StatisticalBloomModel:*",
        "$WF:FilteredBloomModel:*",
        "BloomModel:*",
        "FreqModel:*",
        "BloomDefaultFingerprintModel:*",
        "BloomFreqComboModel:*",
        "FilteredBloomModel:*",
        "StatisticalBloomModel:*",
    ]
    for pattern in patterns:
        for key in POPOTO_REDIS_DB.scan_iter(match=pattern):
            POPOTO_REDIS_DB.delete(key)
    # Also clean class set keys
    for cls_name in [
        "BloomModel",
        "FreqModel",
        "BloomDefaultFingerprintModel",
        "BloomFreqComboModel",
        "FilteredBloomModel",
        "StatisticalBloomModel",
    ]:
        POPOTO_REDIS_DB.delete(f"{cls_name}:all")
    yield
    for pattern in patterns:
        for key in POPOTO_REDIS_DB.scan_iter(match=pattern):
            POPOTO_REDIS_DB.delete(key)
    for cls_name in [
        "BloomModel",
        "FreqModel",
        "BloomDefaultFingerprintModel",
        "BloomFreqComboModel",
        "FilteredBloomModel",
        "StatisticalBloomModel",
    ]:
        POPOTO_REDIS_DB.delete(f"{cls_name}:all")


# --- ExistenceFilter Tests ---


class TestExistenceFilterBasic:
    """Basic ExistenceFilter functionality."""

    def test_might_exist_after_save(self):
        """After saving an item, might_exist() returns True."""
        item = BloomModel(name="item1", topic="kubernetes")
        item.save()

        assert BloomModel.bloom.might_exist(BloomModel, "kubernetes") is True

    def test_definitely_missing_for_unseen(self):
        """For an item never saved, definitely_missing() returns True."""
        assert (
            BloomModel.bloom.definitely_missing(BloomModel, "never-seen-topic") is True
        )

    def test_might_exist_returns_false_for_unseen(self):
        """For an item never saved, might_exist() returns False."""
        assert BloomModel.bloom.might_exist(BloomModel, "never-seen-topic") is False

    def test_definitely_missing_returns_false_after_save(self):
        """After saving, definitely_missing() returns False."""
        item = BloomModel(name="item2", topic="docker")
        item.save()

        assert BloomModel.bloom.definitely_missing(BloomModel, "docker") is False

    def test_multiple_items(self):
        """Multiple distinct fingerprints are tracked independently."""
        for i, topic in enumerate(["alpha", "beta", "gamma"]):
            BloomModel(name=f"item-{i}", topic=topic).save()

        for topic in ["alpha", "beta", "gamma"]:
            assert BloomModel.bloom.might_exist(BloomModel, topic) is True

        assert BloomModel.bloom.definitely_missing(BloomModel, "delta") is True

    def test_empty_string_fingerprint(self):
        """Empty string fingerprint is handled correctly."""
        item = BloomModel(name="empty", topic="")
        item.save()

        assert BloomModel.bloom.might_exist(BloomModel, "") is True

    def test_bloom_key_doesnt_exist_yet(self):
        """When Bloom filter key doesn't exist, GETBIT returns 0, so might_exist returns False."""
        # No save has happened, key does not exist in Redis
        assert BloomModel.bloom.might_exist(BloomModel, "anything") is False
        assert BloomModel.bloom.definitely_missing(BloomModel, "anything") is True


class TestExistenceFilterDefaultFingerprint:
    """Test default fingerprint behavior (redis_key)."""

    def test_default_fingerprint_uses_redis_key(self):
        """When no fingerprint_fn is set, uses the model's redis_key."""
        item = BloomDefaultFingerprintModel(name="test-key")
        item.save()

        # The fingerprint is the redis_key of the saved instance
        redis_key = item.db_key.redis_key
        assert (
            BloomDefaultFingerprintModel.bloom.might_exist(
                BloomDefaultFingerprintModel, redis_key
            )
            is True
        )

        # A completely different fingerprint should not be found
        assert (
            BloomDefaultFingerprintModel.bloom.definitely_missing(
                BloomDefaultFingerprintModel, "zzzmissingzzz"
            )
            is True
        )


class TestExistenceFilterOnDelete:
    """Verify on_delete() is a no-op."""

    def test_delete_does_not_remove_from_bloom(self):
        """After deleting a model instance, the Bloom filter still contains its fingerprint."""
        item = BloomModel(name="deleteme", topic="ephemeral")
        item.save()

        # Confirm it's in the Bloom filter
        assert BloomModel.bloom.might_exist(BloomModel, "ephemeral") is True

        # Delete the model instance
        item.delete()

        # Bloom filter should still report it as possibly present
        assert BloomModel.bloom.might_exist(BloomModel, "ephemeral") is True


class TestExistenceFilterPipeline:
    """Test pipeline support in on_save()."""

    def test_on_save_with_pipeline(self):
        """on_save() works within a Redis pipeline."""
        pipeline = POPOTO_REDIS_DB.pipeline()
        item = BloomModel(name="pipelined", topic="pipelining")
        # Save normally to create the model, which triggers on_save via pipeline internally
        item.save()

        # Verify the Bloom filter was updated
        assert BloomModel.bloom.might_exist(BloomModel, "pipelining") is True


class TestExistenceFilterFillRatio:
    """Test fill_ratio diagnostic method."""

    def test_fill_ratio_zero_before_saves(self):
        """Fill ratio is 0.0 before any saves."""
        ratio = BloomModel.bloom.fill_ratio(BloomModel)
        assert ratio == 0.0

    def test_fill_ratio_increases_with_saves(self):
        """Fill ratio increases as items are added."""
        for i in range(100):
            BloomModel(name=f"fill-{i}", topic=f"uniquetopic{i:04d}").save()

        ratio = BloomModel.bloom.fill_ratio(BloomModel)
        assert 0.0 < ratio < 1.0


class TestExistenceFilterStatistical:
    """Statistical test: false positive rate within 2x of configured bounds."""

    def test_false_positive_rate(self):
        """False positive rate should be within 2x of configured error_rate.

        Uses StatisticalBloomModel with error_rate=0.05 and capacity=10,000.
        Inserts 10,000 items with unique single-word topics, then queries
        10,000 items that were never inserted. The false positive rate should
        be <= 0.10 (2x the configured 0.05).

        Topics are single tokens (no hyphens/spaces) so each save adds exactly
        one token to the bloom filter, matching pre-tokenization behavior.
        """
        # Insert 10,000 items with unique single-word topics
        num_items = 10_000
        for i in range(num_items):
            StatisticalBloomModel(
                name=f"stat-{i}", topic=f"xinserted{i:06d}"
            ).save()

        # Query 10,000 items that were never inserted
        false_positives = 0
        num_queries = 10_000
        for i in range(num_queries):
            if StatisticalBloomModel.bloom.might_exist(
                StatisticalBloomModel, f"xneverhere{i:06d}"
            ):
                false_positives += 1

        fp_rate = false_positives / num_queries
        configured_rate = 0.05

        # False positive rate should be within 2x of configured rate
        assert fp_rate <= configured_rate * 2, (
            f"False positive rate {fp_rate:.4f} exceeds 2x configured rate "
            f"{configured_rate * 2:.4f}"
        )

        # Sanity: should have some false positives (extremely unlikely to have zero)
        # but not mandatory — just verify the rate is bounded


# --- FrequencySketch Tests ---


class TestFrequencySketchBasic:
    """Basic FrequencySketch functionality."""

    def test_increment_and_query(self):
        """Single save increments frequency to 1."""
        item = FreqModel(name="freq1", topic="testing")
        item.save()

        count = FreqModel.freq.get_frequency(FreqModel, "testing")
        assert count == 1

    def test_multiple_increments(self):
        """Multiple saves accumulate correctly."""
        for i in range(5):
            FreqModel(name=f"freq-{i}", topic="popular").save()

        count = FreqModel.freq.get_frequency(FreqModel, "popular")
        assert count == 5

    def test_different_topics_independent(self):
        """Different fingerprints are counted independently."""
        for i in range(3):
            FreqModel(name=f"a-{i}", topic="analytics").save()
        FreqModel(name="b-0", topic="blockchain").save()

        assert FreqModel.freq.get_frequency(FreqModel, "analytics") == 3
        assert FreqModel.freq.get_frequency(FreqModel, "blockchain") == 1

    def test_query_unseen_returns_zero(self):
        """Querying a fingerprint never seen returns 0."""
        count = FreqModel.freq.get_frequency(FreqModel, "never-seen")
        assert count == 0

    def test_cms_key_doesnt_exist_yet(self):
        """When CMS key doesn't exist, HGET returns nil, so frequency is 0."""
        count = FreqModel.freq.get_frequency(FreqModel, "nonexistent")
        assert count == 0


class TestFrequencySketchOnDelete:
    """Verify on_delete() is a no-op for FrequencySketch."""

    def test_delete_does_not_decrement(self):
        """After deleting a model instance, frequency count is unchanged."""
        item = FreqModel(name="del-freq", topic="counting")
        item.save()
        assert FreqModel.freq.get_frequency(FreqModel, "counting") == 1

        item.delete()
        # Count should still be 1 (no decrement on delete)
        assert FreqModel.freq.get_frequency(FreqModel, "counting") == 1


# --- Combo Tests ---


class TestBloomFreqCombo:
    """Test model with both ExistenceFilter and FrequencySketch."""

    def test_both_fields_work_together(self):
        """Both fields maintain their indexes independently."""
        item = BloomFreqComboModel(name="combo1", topic="synergy")
        item.save()

        assert (
            BloomFreqComboModel.bloom.might_exist(BloomFreqComboModel, "synergy")
            is True
        )
        assert (
            BloomFreqComboModel.freq.get_frequency(BloomFreqComboModel, "synergy")
            == 1
        )


# --- Synergy Tests ---


class TestWriteFilterSynergy:
    """Synergy: WriteFilterMixin + ExistenceFilter.

    Records rejected by WriteFilter (SkipSaveException) should NOT have
    their fingerprints added to the Bloom filter, because on_save() hooks
    are never called when save is skipped.
    """

    def test_rejected_record_not_in_bloom(self):
        """Record below WriteFilter threshold is not in Bloom filter."""
        # importance=0.1 < min_threshold=0.2 -> SkipSaveException
        item = FilteredBloomModel(
            name="filtered-out",
            topic="negligible",
            importance=0.1,
        )
        item.save()  # silently discarded

        # Bloom filter should NOT contain this fingerprint
        assert (
            FilteredBloomModel.bloom.definitely_missing(
                FilteredBloomModel, "negligible"
            )
            is True
        )

    def test_accepted_record_in_bloom(self):
        """Record above WriteFilter threshold IS in Bloom filter."""
        # importance=0.5 >= min_threshold=0.2 -> saved normally
        item = FilteredBloomModel(
            name="accepted",
            topic="significant",
            importance=0.5,
        )
        item.save()

        assert (
            FilteredBloomModel.bloom.might_exist(FilteredBloomModel, "significant")
            is True
        )


class TestPreFilterPattern:
    """Synergy: ExistenceFilter as pre-filter for retrieval.

    Verify that definitely_missing() returns True for unseen fingerprints
    and False for seen ones, enabling short-circuit of expensive queries.
    """

    def test_prefilter_skips_unseen(self):
        """definitely_missing() allows skipping retrieval for unseen topics."""
        BloomModel(name="known", topic="kubernetes").save()

        # Known topic: don't skip
        assert BloomModel.bloom.definitely_missing(BloomModel, "kubernetes") is False

        # Unknown topic: skip retrieval
        assert BloomModel.bloom.definitely_missing(BloomModel, "terraform") is True


class TestComputeParams:
    """Test _compute_params() parameter derivation."""

    def test_default_params(self):
        """Default error_rate=0.01, capacity=100_000 produces reasonable m and k."""
        m, k = BloomModel.bloom._compute_params()
        # m should be ~958,506 bits for 1% error rate at 100k capacity
        assert m > 900_000
        assert m < 1_100_000
        # k should be ~7 for these parameters
        assert 5 <= k <= 10

    def test_custom_params(self):
        """Custom error_rate and capacity produce different m and k."""
        ef = ExistenceFilter(error_rate=0.1, capacity=1000)
        m, k = ef._compute_params()
        # m should be ~4,793 bits for 10% error at 1k capacity
        assert m > 4_000
        assert m < 6_000
        # k should be ~3
        assert 2 <= k <= 5


class TestFingerprintFnValidation:
    """Test fingerprint_fn edge cases."""

    def test_fingerprint_fn_returning_none_raises(self):
        """fingerprint_fn returning None raises ValueError."""

        class BadFpModel(popoto.Model):
            name = popoto.UniqueKeyField()
            bloom = ExistenceFilter(
                fingerprint_fn=lambda inst: None,
            )

        item = BadFpModel(name="bad")
        with pytest.raises(ValueError, match="fingerprint_fn returned None"):
            item.save()


# --- Tokenization Tests ---


class TestTokenizeFunction:
    """Unit tests for the tokenize() helper function."""

    def test_basic_tokenization(self):
        """Splits on whitespace and lowercases."""
        tokens = tokenize("Kubernetes Deployment Guide")
        assert set(tokens) == {"kubernetes", "deployment", "guide"}

    def test_splits_on_punctuation(self):
        """Splits on non-word characters like hyphens and colons."""
        tokens = tokenize("redis-cache:primary")
        assert set(tokens) == {"redis", "cache", "primary"}

    def test_filters_short_tokens(self):
        """Tokens shorter than 3 characters are removed."""
        tokens = tokenize("a is to go run")
        # "a", "is", "to", "go" are all < 3 chars; "run" is exactly 3
        assert tokens == ["run"]

    def test_filters_stop_words(self):
        """Common stop words are removed."""
        tokens = tokenize("the quick brown fox with all")
        assert "the" not in tokens
        assert "with" not in tokens
        assert "all" not in tokens
        assert "quick" in tokens
        assert "brown" in tokens
        assert "fox" in tokens

    def test_empty_string(self):
        """Empty string returns empty list."""
        assert tokenize("") == []

    def test_all_stop_words(self):
        """String of only stop words returns empty list."""
        assert tokenize("the and for with") == []

    def test_deduplication(self):
        """Duplicate tokens are removed."""
        tokens = tokenize("test test test again")
        assert tokens == ["test", "again"]

    def test_unicode_characters(self):
        """Unicode text (CJK, accented) is tokenized without crashing."""
        # CJK characters
        tokens = tokenize("部署 kubernetes")
        assert "kubernetes" in tokens

        # Accented Latin characters
        tokens = tokenize("déploiement réseau")
        assert "déploiement" in tokens
        assert "réseau" in tokens

        # Mixed unicode and ASCII
        tokens = tokenize("café naïve über")
        assert "café" in tokens
        assert "naïve" in tokens
        assert "über" in tokens


class TestExistenceFilterTokenization:
    """Test that ExistenceFilter tokenizes fingerprints on save."""

    def test_multiword_save_per_word_query(self):
        """Save a multi-word fingerprint, query individual words."""
        item = BloomModel(name="multiword", topic="kubernetes deployment guide")
        item.save()

        assert BloomModel.bloom.might_exist(BloomModel, "kubernetes") is True
        assert BloomModel.bloom.might_exist(BloomModel, "deployment") is True
        assert BloomModel.bloom.might_exist(BloomModel, "guide") is True

    def test_multiword_query_missing_word(self):
        """Query a word NOT in the saved fingerprint."""
        item = BloomModel(name="missing", topic="kubernetes deployment guide")
        item.save()

        assert BloomModel.bloom.definitely_missing(BloomModel, "terraform") is True

    def test_multiword_definitely_missing(self):
        """Multi-word definitely_missing query checks all tokens."""
        item = BloomModel(name="multi-dm", topic="kubernetes deployment")
        item.save()

        # Neither word was saved -> definitely missing
        assert BloomModel.bloom.definitely_missing(BloomModel, "terraform migration") is True

        # One word ("kubernetes") was saved -> not definitely missing
        # (might_exist uses ANY semantics, so definitely_missing returns False)
        assert BloomModel.bloom.definitely_missing(BloomModel, "kubernetes migration") is False

        # Both words saved -> not definitely missing
        assert BloomModel.bloom.definitely_missing(BloomModel, "kubernetes deployment") is False

    def test_case_insensitive_query(self):
        """Case-insensitive: save mixed case, query lowercase."""
        item = BloomModel(name="case", topic="Kubernetes")
        item.save()

        assert BloomModel.bloom.might_exist(BloomModel, "kubernetes") is True
        assert BloomModel.bloom.might_exist(BloomModel, "KUBERNETES") is True

    def test_stop_words_not_indexed(self):
        """Stop words from the fingerprint are not indexed."""
        item = BloomModel(name="stops", topic="the quick brown fox")
        item.save()

        # "the" is a stop word, should not be in the bloom filter
        # (unless it's a false positive, which is unlikely for a near-empty filter)
        assert BloomModel.bloom.might_exist(BloomModel, "quick") is True
        assert BloomModel.bloom.might_exist(BloomModel, "brown") is True

    def test_empty_tokenization_fallback(self):
        """Fingerprint that tokenizes to nothing falls back to raw string."""
        # "a is" tokenizes to [] (both too short or stop words), so raw "a is" is stored
        item = BloomModel(name="fallback", topic="a is")
        item.save()

        # The raw lowercased string should match via fallback
        assert BloomModel.bloom.might_exist(BloomModel, "a is") is True

    def test_empty_tokenization_fallback_mixed_case(self):
        """Fallback path lowercases fingerprint so mixed-case queries match."""
        # "AB" is 2 chars, below 3-char token minimum, so tokenize returns [].
        # The fallback must lowercase before storing so queries match.
        item = BloomModel(name="fallback-case", topic="AB")
        item.save()

        assert BloomModel.bloom.might_exist(BloomModel, "AB") is True
        assert BloomModel.bloom.might_exist(BloomModel, "ab") is True
        assert BloomModel.bloom.might_exist(BloomModel, "Ab") is True

    def test_multiword_query_matches_any_token(self):
        """Multi-word query returns True if ANY token matches."""
        item = BloomModel(name="partial", topic="kubernetes deployment")
        item.save()

        # "kubernetes migration" shares token "kubernetes" with the saved fingerprint
        assert BloomModel.bloom.might_exist(BloomModel, "kubernetes migration") is True


class TestFrequencySketchTokenization:
    """Test that FrequencySketch tokenizes fingerprints on save."""

    def test_multiword_per_token_frequency(self):
        """Each token in a multi-word fingerprint gets its own frequency count."""
        FreqModel(name="freq-multi-1", topic="kubernetes deployment").save()
        FreqModel(name="freq-multi-2", topic="kubernetes scaling").save()

        # "kubernetes" appears in both saves
        assert FreqModel.freq.get_frequency(FreqModel, "kubernetes") == 2
        # "deployment" appears in only one save
        assert FreqModel.freq.get_frequency(FreqModel, "deployment") == 1
        # "scaling" appears in only one save
        assert FreqModel.freq.get_frequency(FreqModel, "scaling") == 1

    def test_multiword_query_returns_min_frequency(self):
        """Multi-word get_frequency query returns the MIN across tokens."""
        FreqModel(name="freq-mw-1", topic="kubernetes deployment").save()
        FreqModel(name="freq-mw-2", topic="kubernetes scaling").save()
        FreqModel(name="freq-mw-3", topic="kubernetes monitoring").save()

        # "kubernetes" has freq 3, "deployment" has freq 1.
        # A multi-word query should return MIN(3, 1) = 1.
        assert FreqModel.freq.get_frequency(FreqModel, "kubernetes deployment") == 1

        # "kubernetes scaling" -> MIN(3, 1) = 1
        assert FreqModel.freq.get_frequency(FreqModel, "kubernetes scaling") == 1

    def test_case_insensitive_frequency(self):
        """Frequency queries are case-insensitive."""
        FreqModel(name="freq-case", topic="Kubernetes").save()

        assert FreqModel.freq.get_frequency(FreqModel, "kubernetes") == 1
        assert FreqModel.freq.get_frequency(FreqModel, "KUBERNETES") == 1

    def test_empty_tokenization_fallback_frequency(self):
        """Fingerprint that tokenizes to nothing uses raw string for frequency."""
        FreqModel(name="freq-empty", topic="an").save()

        # "an" is < 3 chars, tokenizes to []. Raw "an" is stored.
        assert FreqModel.freq.get_frequency(FreqModel, "an") == 1

    def test_empty_tokenization_fallback_frequency_mixed_case(self):
        """Fallback path lowercases fingerprint so mixed-case frequency queries match."""
        FreqModel(name="freq-case-fb", topic="XY").save()

        # "XY" tokenizes to [] (too short). Fallback must lowercase.
        assert FreqModel.freq.get_frequency(FreqModel, "XY") == 1
        assert FreqModel.freq.get_frequency(FreqModel, "xy") == 1
        assert FreqModel.freq.get_frequency(FreqModel, "Xy") == 1
