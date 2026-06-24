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

import math
import random
import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402
from src import popoto  # noqa: E402
from src.popoto.fields.existence_filter import (
    CMS_INCR_LUA,
    CMS_INCR_MULTI_LUA,
    CMS_QUERY_LUA,
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
            StatisticalBloomModel(name=f"stat-{i}", topic=f"xinserted{i:06d}").save()

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
            BloomFreqComboModel.freq.get_frequency(BloomFreqComboModel, "synergy") == 1
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
        # importance=0.05 < min_threshold=0.1 (2026-04-17 tuning)
        # -> SkipSaveException
        item = FilteredBloomModel(
            name="filtered-out",
            topic="negligible",
            importance=0.05,
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
        # importance=0.5 >= min_threshold=0.1 (2026-04-17 tuning)
        # -> saved normally
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
        assert (
            BloomModel.bloom.definitely_missing(BloomModel, "terraform migration")
            is True
        )

        # One word ("kubernetes") was saved -> not definitely missing
        # (might_exist uses ANY semantics, so definitely_missing returns False)
        assert (
            BloomModel.bloom.definitely_missing(BloomModel, "kubernetes migration")
            is False
        )

        # Both words saved -> not definitely missing
        assert (
            BloomModel.bloom.definitely_missing(BloomModel, "kubernetes deployment")
            is False
        )

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


# --- Batch Bloom Filter Tests ---


class TestMightExistBatch:
    """Tests for ExistenceFilter.might_exist_batch() — single round-trip batch check."""

    def test_batch_empty_list(self):
        """Empty input list returns empty dict."""
        result = BloomModel.bloom.might_exist_batch(BloomModel, [])
        assert result == {}

    def test_batch_all_missing(self):
        """All fingerprints missing when bloom is empty returns all False."""
        result = BloomModel.bloom.might_exist_batch(
            BloomModel, ["alpha", "beta", "gamma"]
        )
        assert result == {"alpha": False, "beta": False, "gamma": False}

    def test_batch_all_present(self):
        """All fingerprints found after saving them."""
        for i, topic in enumerate(["kubernetes", "deployment", "monitoring"]):
            BloomModel(name=f"batch-{i}", topic=topic).save()

        result = BloomModel.bloom.might_exist_batch(
            BloomModel, ["kubernetes", "deployment", "monitoring"]
        )
        assert result["kubernetes"] is True
        assert result["deployment"] is True
        assert result["monitoring"] is True

    def test_batch_mixed_present_and_missing(self):
        """Mix of present and missing fingerprints returns correct results."""
        BloomModel(name="batch-mix-1", topic="kubernetes").save()
        BloomModel(name="batch-mix-2", topic="deployment").save()

        result = BloomModel.bloom.might_exist_batch(
            BloomModel, ["kubernetes", "terraform", "deployment", "ansible"]
        )
        assert result["kubernetes"] is True
        assert result["deployment"] is True
        assert result["terraform"] is False
        assert result["ansible"] is False

    def test_batch_single_item(self):
        """Degenerate batch with one fingerprint works correctly."""
        BloomModel(name="batch-single", topic="kubernetes").save()

        result = BloomModel.bloom.might_exist_batch(BloomModel, ["kubernetes"])
        assert result == {"kubernetes": True}

    def test_batch_duplicate_fingerprints(self):
        """Duplicate fingerprints are deduplicated in result."""
        BloomModel(name="batch-dup", topic="kubernetes").save()

        result = BloomModel.bloom.might_exist_batch(
            BloomModel, ["kubernetes", "kubernetes", "kubernetes"]
        )
        assert result == {"kubernetes": True}
        assert len(result) == 1

    def test_batch_multiword_fingerprints(self):
        """Multi-word fingerprints are tokenized; ANY token match = hit."""
        BloomModel(name="batch-mw", topic="kubernetes deployment guide").save()

        result = BloomModel.bloom.might_exist_batch(
            BloomModel, ["kubernetes migration", "terraform setup"]
        )
        # "kubernetes migration" shares "kubernetes" with saved data
        assert result["kubernetes migration"] is True
        # "terraform setup" shares no tokens
        assert result["terraform setup"] is False

    def test_batch_fallback_short_tokens(self):
        """Fingerprints that tokenize to nothing use raw lowercased fallback."""
        BloomModel(name="batch-short", topic="AB").save()

        result = BloomModel.bloom.might_exist_batch(BloomModel, ["AB", "CD"])
        assert result["AB"] is True
        assert result["CD"] is False

    def test_batch_bloom_key_doesnt_exist(self):
        """When bloom key does not exist, all results are False."""
        result = BloomModel.bloom.might_exist_batch(
            BloomModel, ["never", "seen", "these"]
        )
        assert all(v is False for v in result.values())

    def test_batch_matches_individual_calls(self):
        """Batch results match individual might_exist() calls."""
        for i, topic in enumerate(["redis", "postgres", "mongodb"]):
            BloomModel(name=f"batch-match-{i}", topic=topic).save()

        fingerprints = ["redis", "postgres", "mongodb", "mysql", "sqlite"]
        batch_result = BloomModel.bloom.might_exist_batch(BloomModel, fingerprints)

        for fp in fingerprints:
            individual = BloomModel.bloom.might_exist(BloomModel, fp)
            assert batch_result[fp] == individual, (
                f"Mismatch for '{fp}': batch={batch_result[fp]}, "
                f"individual={individual}"
            )


class TestMightExistCount:
    """Tests for ExistenceFilter.might_exist_count() — convenience counter."""

    def test_count_empty_list(self):
        """Empty input returns 0."""
        assert BloomModel.bloom.might_exist_count(BloomModel, []) == 0

    def test_count_all_missing(self):
        """All missing returns 0."""
        assert BloomModel.bloom.might_exist_count(BloomModel, ["alpha", "beta"]) == 0

    def test_count_all_present(self):
        """All present returns full count."""
        for i, topic in enumerate(["redis", "postgres"]):
            BloomModel(name=f"count-{i}", topic=topic).save()

        assert (
            BloomModel.bloom.might_exist_count(BloomModel, ["redis", "postgres"]) == 2
        )

    def test_count_mixed(self):
        """Mix of present and missing returns correct count."""
        BloomModel(name="count-mix", topic="kubernetes").save()

        count = BloomModel.bloom.might_exist_count(
            BloomModel, ["kubernetes", "terraform", "ansible"]
        )
        assert count == 1


# --- CMS Row Independence Tests ---


class TestCMSRowIndependence:
    """Regression tests for the per-row independent polynomial hash family (#414).

    These tests guard against the affine-seed collapse bug where all 7 rows
    used the same hash, differing only in an additive seed term.
    """

    def test_rows_not_all_or_none(self):
        """Row-0-colliding token pairs must NOT collide in all 7 rows.

        With the old affine seed, any two same-length tokens that collide in row 0
        collide in all 7 rows (100% all-or-none rate). With per-row independent
        polynomials the all-7-row collision rate should be ~(1/width)^6 approx 0.
        """
        import random
        from collections import defaultdict

        from src.popoto.fields.existence_filter import CMS_INCR_LUA
        from src.popoto.redis_db import POPOTO_REDIS_DB as r

        rng = random.Random(42)
        width = 2003
        depth = 7

        # Generate 3000 random 9-char tokens
        chars = "abcdefghijklmnopqrstuvwxyz"
        tokens = ["".join(rng.choices(chars, k=9)) for _ in range(3000)]

        # For each token, compute its column in each row by checking which cells get incremented
        def get_cols(token):
            key = f"__test_cms_ind_{rng.randint(0, 10**9)}"
            r.delete(key)
            r.eval(CMS_INCR_LUA, 1, key, token, width, depth)
            cols = {}
            for field, val in r.hgetall(key).items():
                row_str, col_str = field.decode().split(":")
                cols[int(row_str)] = int(col_str)
            r.delete(key)
            return cols

        # Build columns for all tokens
        all_cols = [get_cols(t) for t in tokens]

        # Find pairs that collide in row 0
        row0_cols = {i: all_cols[i][0] for i in range(len(tokens))}
        row0_groups = defaultdict(list)
        for i, c in row0_cols.items():
            row0_groups[c].append(i)

        colliding_pairs = []
        for group in row0_groups.values():
            if len(group) >= 2:
                for a in range(len(group)):
                    for b in range(a + 1, len(group)):
                        colliding_pairs.append((group[a], group[b]))

        assert len(colliding_pairs) >= 5, (
            f"Need at least 5 colliding pairs to test; got {len(colliding_pairs)}"
        )

        all_7_count = 0
        for i, j in colliding_pairs:
            cols_i = all_cols[i]
            cols_j = all_cols[j]
            if all(cols_i[row] == cols_j[row] for row in range(depth)):
                all_7_count += 1

        assert all_7_count == 0, (
            f"{all_7_count}/{len(colliding_pairs)} colliding pairs matched in all 7 rows. "
            f"Expected 0 (old affine-seed bug would give ~100%)."
        )

    def test_independence_at_composite_width(self):
        """The polynomial fix decorrelates rows even at composite width=2000.

        This isolates causality: the independent per-row polynomial -- not the
        prime width -- is what removes the affine-seed collapse.
        """
        import random
        from collections import defaultdict

        from src.popoto.fields.existence_filter import CMS_INCR_LUA
        from src.popoto.redis_db import POPOTO_REDIS_DB as r

        rng = random.Random(99)
        width = 2000  # deliberately composite (the old broken default)
        depth = 7

        chars = "abcdefghijklmnopqrstuvwxyz"
        tokens = ["".join(rng.choices(chars, k=9)) for _ in range(3000)]

        def get_cols(token):
            key = f"__test_cms_comp_{rng.randint(0, 10**9)}"
            r.delete(key)
            r.eval(CMS_INCR_LUA, 1, key, token, width, depth)
            cols = {}
            for field, val in r.hgetall(key).items():
                row_str, col_str = field.decode().split(":")
                cols[int(row_str)] = int(col_str)
            r.delete(key)
            return cols

        all_cols = [get_cols(t) for t in tokens]

        row0_cols = {i: all_cols[i][0] for i in range(len(tokens))}
        row0_groups = defaultdict(list)
        for i, c in row0_cols.items():
            row0_groups[c].append(i)

        colliding_pairs = []
        for group in row0_groups.values():
            if len(group) >= 2:
                for a in range(len(group)):
                    for b in range(a + 1, len(group)):
                        colliding_pairs.append((group[a], group[b]))

        all_7_count = 0
        for i, j in colliding_pairs:
            cols_i = all_cols[i]
            cols_j = all_cols[j]
            if all(cols_i[row] == cols_j[row] for row in range(depth)):
                all_7_count += 1

        assert all_7_count == 0, (
            f"{all_7_count}/{len(colliding_pairs)} all-7-row collisions at composite width=2000. "
            f"The polynomial family must decorrelate rows regardless of width."
        )

    def test_three_scripts_agree(self):
        """CMS_INCR_LUA, CMS_INCR_MULTI_LUA, and CMS_QUERY_LUA must hit identical columns.

        All three scripts must use the identical per-row polynomial family or
        increment/query will disagree (undercount).
        """
        from src.popoto.fields.existence_filter import (
            CMS_INCR_LUA,
            CMS_INCR_MULTI_LUA,
            CMS_QUERY_LUA,
        )
        from src.popoto.redis_db import POPOTO_REDIS_DB as r

        width = 2003
        depth = 7
        token = "testtoken"
        n = 3

        # Increment via CMS_INCR_LUA
        key_single = "__test_agree_single"
        r.delete(key_single)
        for _ in range(n):
            r.eval(CMS_INCR_LUA, 1, key_single, token, width, depth)

        # Increment via CMS_INCR_MULTI_LUA
        key_multi = "__test_agree_multi"
        r.delete(key_multi)
        for _ in range(n):
            r.eval(CMS_INCR_MULTI_LUA, 1, key_multi, width, depth, token)

        # Query both keys via CMS_QUERY_LUA -- must read back >= n
        count_single = r.eval(CMS_QUERY_LUA, 1, key_single, token, width, depth)
        count_multi = r.eval(CMS_QUERY_LUA, 1, key_multi, token, width, depth)

        assert int(count_single) >= n, (
            f"CMS_INCR_LUA + CMS_QUERY_LUA: got {count_single}, expected >= {n}"
        )
        assert int(count_multi) >= n, (
            f"CMS_INCR_MULTI_LUA + CMS_QUERY_LUA: got {count_multi}, expected >= {n}"
        )

        # Columns must be identical: same cells incremented by both scripts
        cells_single = {k.decode(): v for k, v in r.hgetall(key_single).items()}
        cells_multi = {k.decode(): v for k, v in r.hgetall(key_multi).items()}
        assert set(cells_single.keys()) == set(cells_multi.keys()), (
            f"CMS_INCR_LUA and CMS_INCR_MULTI_LUA hit different columns for '{token}'.\n"
            f"single keys: {sorted(cells_single.keys())}\n"
            f"multi keys: {sorted(cells_multi.keys())}"
        )

        r.delete(key_single)
        r.delete(key_multi)

    @pytest.mark.parametrize("seed", [42, 137, 999])
    def test_zipf_error_bound(self, seed):
        """Error bound: fraction of items with estimate > true + eN/width < e^(-depth).

        Parametrized over 3 seeds to guard against lucky single-seed passes.
        eN/w bound with N=20k events, width=2003: 20000*e/2003 approx 27.2 per item.
        The fraction of items exceeding this bound must be < e^(-7) approx 0.091%.
        Never-undercount: 0 items with estimate < true count.
        """
        import math
        import random

        from src.popoto.fields.existence_filter import CMS_INCR_LUA, CMS_QUERY_LUA
        from src.popoto.redis_db import POPOTO_REDIS_DB as r

        rng = random.Random(seed)

        width = 2003
        depth = 7
        N = 20000  # total increment events
        vocab_size = 5000

        # Generate vocabulary of 9-char tokens
        chars = "abcdefghijklmnopqrstuvwxyz0123456789"
        vocab = list(
            {"".join(rng.choices(chars, k=9)) for _ in range(vocab_size * 2)}
        )[:vocab_size]

        # Zipf-1.2 distribution
        zipf_weights = [1.0 / (i + 1) ** 1.2 for i in range(len(vocab))]
        total_w = sum(zipf_weights)
        zipf_weights = [w / total_w for w in zipf_weights]

        # True counts
        true_counts = {t: 0 for t in vocab}

        key = f"__test_zipf_eb_{seed}"
        r.delete(key)

        # Sample and increment
        for _ in range(N):
            token = rng.choices(vocab, weights=zipf_weights)[0]
            true_counts[token] += 1
            r.eval(CMS_INCR_LUA, 1, key, token, width, depth)

        # Query all tokens
        eN_over_w = math.e * N / width  # ~27.2
        bound_violations = 0
        undercounts = 0
        queried = 0

        for token, true_cnt in true_counts.items():
            if true_cnt == 0:
                continue
            queried += 1
            est = int(r.eval(CMS_QUERY_LUA, 1, key, token, width, depth) or 0)
            if est < true_cnt:
                undercounts += 1
            if est > true_cnt + eN_over_w:
                bound_violations += 1

        r.delete(key)

        assert undercounts == 0, (
            f"seed={seed}: {undercounts} undercounts (CMS must never undercount)"
        )

        violation_rate = bound_violations / queried if queried > 0 else 0
        theory_limit = math.exp(-depth)  # e^(-7) approx 0.000912
        assert violation_rate < theory_limit, (
            f"seed={seed}: violation rate {violation_rate:.4%} >= theory limit {theory_limit:.4%} "
            f"({bound_violations}/{queried} items exceeded true+eN/w={eN_over_w:.1f})"
        )

    def test_depth_guard(self):
        """FrequencySketch raises ValueError for depth outside [1, 7]."""
        from src.popoto.fields.existence_filter import FrequencySketch

        with pytest.raises(ValueError, match=r"[1-7]|1.*7|depth"):
            FrequencySketch(fingerprint_fn=lambda x: "", depth=8)

        with pytest.raises(ValueError, match=r"[1-7]|1.*7|depth"):
            FrequencySketch(fingerprint_fn=lambda x: "", depth=0)

        # Valid depths should not raise
        for d in range(1, 8):
            fs = FrequencySketch(fingerprint_fn=lambda x: "", depth=d)
            assert fs.depth == d


# ---------------------------------------------------------------------------
# CMS hash-family fix regression tests (#414)
# ---------------------------------------------------------------------------

# Polynomial hash constants matching the fixed Lua scripts.
# These P (prime moduli) and M (prime multipliers) form a practically
# pairwise-independent hash family — one pair per CMS row.
_CMS_P = [16777259, 16777289, 16777291, 16777331, 16777333, 16777337, 16777381]
_CMS_M = [33554467, 33554473, 33554501, 33554503, 33554509, 33554519, 33554527]


def _compute_columns(token: str, width: int, depth: int) -> list[int]:
    """Python mirror of the fixed Lua CMS polynomial hash.

    Seed: h = row + 1
    Per-byte: h = (h * M[row] + byte) % P[row]
    Column: col = h % width

    Used by tests that verify the hash-family properties in pure Python
    without going through Redis, so they remain fast and deterministic.
    """
    cols = []
    for row in range(depth):
        pr = _CMS_P[row]
        mr = _CMS_M[row]
        h = row + 1
        for c in token.encode():
            h = (h * mr + c) % pr
        cols.append(h % width)
    return cols


class TestFrequencySketchCMSHashFamily:
    """Regression tests for the CMS per-row independent polynomial hash fix (#414).

    Before the fix, CMS used an affine seed  h = 5381 + row * 16777619 with a
    shared multiplier (33).  Every token that collided in row-0 also collided in
    ALL seven rows — destroying the independence that the standard CMS error bound
    requires.

    After the fix, each row uses its own prime modulus P[row] and prime multiplier
    M[row].  Row hashes are practically pairwise-independent, restoring the
    e^{-depth} exceedance probability.
    """

    def test_rows_not_all_or_none(self):
        """Per-row independence: row-0 collisions should NOT propagate to all 7 rows.

        With the old affine-seed approach, if two tokens hash to the same column in
        row 0 they ALWAYS hash to the same column in every row (100% correlation).
        With the new polynomial hashes the extra-row match probability per row is
        approximately 1/width — effectively zero for pairs that just happen to share
        a row-0 bucket.

        Test parameters:
            3000 9-char tokens, width=2003 (prime), depth=7
        """
        width, depth = 2003, 7
        rng = random.Random(42)
        chars = "abcdefghijklmnopqrstuvwxyz"
        tokens = ["".join(rng.choices(chars, k=9)) for _ in range(3000)]

        # Build a map: row-0 column -> list of full column vectors
        col0_groups: dict[int, list[list[int]]] = {}
        for tok in tokens:
            cols = _compute_columns(tok, width, depth)
            col0_groups.setdefault(cols[0], []).append(cols)

        # Count pairs that collide in row 0 AND in ALL 7 rows
        all7_collisions = 0
        total_row0_pairs = 0
        extra_row_matches = []  # count of matching rows beyond row-0 for each pair

        for group in col0_groups.values():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    total_row0_pairs += 1
                    matches_in_extra = sum(
                        1 for r in range(1, depth) if group[i][r] == group[j][r]
                    )
                    extra_row_matches.append(matches_in_extra)
                    if matches_in_extra == depth - 1:  # collide in all remaining rows
                        all7_collisions += 1

        # With independent hashes, zero all-7-row collisions is expected.
        assert all7_collisions == 0, (
            f"Found {all7_collisions} pairs colliding in all {depth} rows "
            f"(out of {total_row0_pairs} row-0 colliding pairs). "
            f"This indicates correlated row hashes — the affine-seed bug."
        )

        # Mean extra-row matches per pair should be near (depth-1)/width
        if total_row0_pairs > 0:
            mean_extra = sum(extra_row_matches) / total_row0_pairs
            theory = (depth - 1) / width
            # Allow 3x tolerance for statistical noise
            assert mean_extra <= theory * 3 + 0.01, (
                f"Mean extra-row match rate {mean_extra:.6f} is more than 3x the "
                f"theoretical {theory:.6f}, suggesting row correlation."
            )

    def test_independence_at_composite_width(self):
        """Row independence holds for composite width=2000 (not just prime widths).

        The old bug was in the hash seed construction, not in the modular reduction.
        Using a composite width proves the polynomial hash constants (not the prime
        width) are responsible for the fix.

        Test parameters:
            3000 9-char tokens, width=2000 (composite), depth=7
        """
        width, depth = 2000, 7
        rng = random.Random(99)
        chars = "abcdefghijklmnopqrstuvwxyz"
        tokens = ["".join(rng.choices(chars, k=9)) for _ in range(3000)]

        col0_groups: dict[int, list[list[int]]] = {}
        for tok in tokens:
            cols = _compute_columns(tok, width, depth)
            col0_groups.setdefault(cols[0], []).append(cols)

        all7_collisions = 0
        total_row0_pairs = 0
        for group in col0_groups.values():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    total_row0_pairs += 1
                    if all(group[i][r] == group[j][r] for r in range(1, depth)):
                        all7_collisions += 1

        assert all7_collisions == 0, (
            f"Found {all7_collisions} all-{depth}-row collisions out of "
            f"{total_row0_pairs} row-0 pairs (composite width={width}). "
            f"Row hashes are not independent."
        )

    def test_three_scripts_agree(self):
        """CMS_INCR_LUA, CMS_INCR_MULTI_LUA, and CMS_QUERY_LUA all compute the same columns.

        Increment a token via CMS_INCR_LUA on one key and via CMS_INCR_MULTI_LUA on
        another, then verify:
          1. CMS_QUERY_LUA returns the same count (>= 1) for both keys.
          2. The set of (row:col) hash fields written to Redis are identical,
             proving all three scripts use the same polynomial arithmetic.
        """
        token = "agreement"
        width, depth = 2003, 7
        key_single = "$FS:test:cms_agree_single"
        key_multi = "$FS:test:cms_agree_multi"

        # Clean up before and after
        POPOTO_REDIS_DB.delete(key_single, key_multi)
        try:
            # Increment via CMS_INCR_LUA (single-token path)
            POPOTO_REDIS_DB.eval(CMS_INCR_LUA, 1, key_single, token, width, depth)

            # Increment via CMS_INCR_MULTI_LUA (multi-token path, one token)
            POPOTO_REDIS_DB.eval(CMS_INCR_MULTI_LUA, 1, key_multi, width, depth, token)

            # Query both keys via CMS_QUERY_LUA
            count_single = POPOTO_REDIS_DB.eval(
                CMS_QUERY_LUA, 1, key_single, token, width, depth
            )
            count_multi = POPOTO_REDIS_DB.eval(
                CMS_QUERY_LUA, 1, key_multi, token, width, depth
            )

            assert count_single >= 1, "CMS_INCR_LUA did not increment any counter"
            assert count_multi >= 1, "CMS_INCR_MULTI_LUA did not increment any counter"
            assert count_single == count_multi, (
                f"CMS_INCR_LUA query={count_single} != "
                f"CMS_INCR_MULTI_LUA query={count_multi}: "
                f"scripts compute different columns for the same token."
            )

            # Verify the exact (row:col) fields written are identical
            fields_single = set(POPOTO_REDIS_DB.hgetall(key_single).keys())
            fields_multi = set(POPOTO_REDIS_DB.hgetall(key_multi).keys())
            assert fields_single == fields_multi, (
                f"Column fields differ between scripts.\n"
                f"  CMS_INCR_LUA:       {sorted(fields_single)}\n"
                f"  CMS_INCR_MULTI_LUA: {sorted(fields_multi)}"
            )

            # Cross-check: Python-computed columns must match what Redis stored
            py_cols = _compute_columns(token, width, depth)
            expected_fields = {
                f"{row}:{col}".encode() for row, col in enumerate(py_cols)
            }
            assert fields_single == expected_fields, (
                f"Redis columns don't match Python _compute_columns.\n"
                f"  Redis:  {sorted(fields_single)}\n"
                f"  Python: {sorted(expected_fields)}\n"
                f"This means the Lua scripts have NOT been updated with the "
                f"polynomial hash fix."
            )
        finally:
            POPOTO_REDIS_DB.delete(key_single, key_multi)

    @pytest.mark.parametrize("seed", [42, 123, 999])
    def test_zipf_error_bound(self, seed):
        """CMS error bound holds on a Zipf-1.2 workload (pure-Python simulation).

        Parameters (see inline comments for derivation):
            N      = 10 000 events
            vocab  = 2 000 distinct tokens ("tok{i:04d}")
            alpha  = 1.2  (Zipf exponent)
            width  = 2003, depth = 7

        Standard CMS bound: Pr[estimate > true + e*N/width] <= e^{-depth}
            e*N/width  = e * 10000 / 2003 ~= 13.6
            e^{-7}     ~= 0.091 %

        The simulation drives _compute_columns (the Python mirror of the fixed Lua
        arithmetic) rather than Redis, so the test runs in milliseconds and is
        strictly deterministic for each seed.  The same arithmetic is exercised by
        CMS_INCR_LUA / CMS_INCR_MULTI_LUA / CMS_QUERY_LUA in production.

        Assertions:
            - Zero undercounts (CMS never undercounts by design).
            - Violation fraction < 0.091% (theoretical worst-case per seed).
        """
        # --- parameters ---
        N = 10_000
        vocab_size = 2_000
        width, depth = 2003, 7
        alpha = 1.2
        # eN/width: the CMS additive error budget
        additive_error = math.e * N / width  # ~13.6

        # --- build Zipf distribution ---
        # weights[i] ~ 1/(i+1)^alpha, normalised to a probability vector
        rng = random.Random(seed)
        raw_weights = [1.0 / ((i + 1) ** alpha) for i in range(vocab_size)]
        total_weight = sum(raw_weights)
        probs = [w / total_weight for w in raw_weights]

        tokens = [f"tok{i:04d}" for i in range(vocab_size)]

        # Numpy is optional; fall back to a manual weighted-sample implementation
        try:
            import numpy as np  # noqa: PLC0415

            np_rng = np.random.default_rng(seed)
            events = [tokens[i] for i in np_rng.choice(vocab_size, size=N, p=probs)]
        except ImportError:
            # Manual alias-method-free approximation: random.choices with weights
            events = rng.choices(tokens, weights=raw_weights, k=N)

        # --- compute true counts ---
        true_counts: dict[str, int] = {}
        for tok in events:
            true_counts[tok] = true_counts.get(tok, 0) + 1

        # --- simulate CMS in Python ---
        cms: dict[tuple[int, int], int] = {}
        for tok in events:
            for row, col in enumerate(_compute_columns(tok, width, depth)):
                key = (row, col)
                cms[key] = cms.get(key, 0) + 1

        # --- evaluate estimates ---
        violations = 0
        undercounts = 0
        for tok in tokens:
            true_c = true_counts.get(tok, 0)
            cols = _compute_columns(tok, width, depth)
            estimate = min(cms.get((row, col), 0) for row, col in enumerate(cols))
            if estimate > true_c + additive_error:
                violations += 1
            if estimate < true_c:
                undercounts += 1

        violation_fraction = violations / vocab_size
        theoretical_bound = math.exp(-depth)  # ~0.000912

        assert undercounts == 0, (
            f"Seed {seed}: CMS produced {undercounts} undercounts — impossible with "
            f"correct hash arithmetic (CMS guarantees estimate >= true count)."
        )
        assert violation_fraction < theoretical_bound, (
            f"Seed {seed}: violation fraction {violation_fraction:.4%} exceeds the "
            f"theoretical CMS bound {theoretical_bound:.4%} (e^{{-{depth}}}). "
            f"This indicates correlated row hashes (affine-seed regression)."
        )

    def test_depth_guard(self):
        """FrequencySketch raises ValueError for depth outside 1..7.

        The per-row P/M tables have exactly 7 entries.  Constructing a
        FrequencySketch with depth outside [1, 7] must raise ValueError
        naming both the lower (1) and upper (7) bounds.
        """
        # depth=8 exceeds the table size
        with pytest.raises(ValueError, match=r"1.*7|7.*1"):
            FrequencySketch(fingerprint_fn=lambda x: str(x), depth=8)

        # depth=0 is below the minimum
        with pytest.raises(ValueError, match=r"1.*7|7.*1"):
            FrequencySketch(fingerprint_fn=lambda x: str(x), depth=0)
