"""Tests for ContextAssembler hybrid retrieval mode.

Covers:
- retrieval_mode="auto" capability detection (select hybrid vs composite)
- retrieval_mode="hybrid" raises QueryException when fields absent
- retrieval_mode="composite" always uses composite path regardless of fields
- _pull_path_hybrid() integration: BM25 + vector + graph signals fused via RRF
- Fallback to composite when both BM25 and vector signals are empty
- Fallback to composite when BM25 raises, vector is the only signal
- score_weights still work when effective mode is composite
- QueryBuilder._get_vector_scores() returns (redis_key, float) tuples
- Backwards compatibility: score_weights-only callers unchanged on models without BM25/Embedding
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.exceptions import QueryException  # noqa: E402
from src.popoto.fields.bm25_field import BM25Field  # noqa: E402
from src.popoto.fields.co_occurrence_field import CoOccurrenceField  # noqa: E402
from src.popoto.fields.confidence_field import ConfidenceField  # noqa: E402
from src.popoto.fields.cyclic_decay_field import CyclicDecayField  # noqa: E402
from src.popoto.fields.decaying_sorted_field import DecayingSortedField  # noqa: E402
from src.popoto.fields.embedding_field import EmbeddingField  # noqa: E402
from src.popoto.fields.existence_filter import ExistenceFilter  # noqa: E402
from src.popoto.fields.field import Field  # noqa: E402
from src.popoto.fields.shortcuts import AutoKeyField, KeyField  # noqa: E402
from src.popoto.models.base import Model  # noqa: E402
from src.popoto.recipes.context_assembler import (  # noqa: E402
    ContextAssembler,
    HYBRID_CANDIDATE_MULTIPLIER,
    RRF_K,
)
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

# Skip the entire module when numpy is not installed (e.g. CI jobs that install
# only the base package without the [embeddings] extra).  EmbeddingField raises
# ImportError at class-definition time when numpy is absent, which would cause
# collection to fail rather than skip.
numpy = pytest.importorskip("numpy")

# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class CompositeOnlyMemory(Model):
    """Model with sorted field only — no BM25, no EmbeddingField."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")


class BM25OnlyMemory(Model):
    """Model with BM25Field but no EmbeddingField."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")
    content_index = BM25Field(source="content")


class EmbeddingOnlyMemory(Model):
    """Model with EmbeddingField but no BM25Field."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")
    embedding = EmbeddingField(source="content")


class HybridMemory(Model):
    """Model with BM25Field + EmbeddingField + CoOccurrenceField — full hybrid."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")
    content_index = BM25Field(source="content")
    embedding = EmbeddingField(source="content")
    associations = CoOccurrenceField(symmetric=True, max_edges=50)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flush():
    POPOTO_REDIS_DB.flushdb()


# ---------------------------------------------------------------------------
# 1. auto mode — capability detection
# ---------------------------------------------------------------------------


class TestAutoModeCapabilityDetection:
    def test_auto_selects_hybrid_when_bm25_and_embedding_present(self):
        assembler = ContextAssembler(
            model_class=HybridMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="auto",
        )
        assert assembler._effective_mode == "hybrid"

    def test_auto_selects_composite_when_bm25_absent(self):
        assembler = ContextAssembler(
            model_class=EmbeddingOnlyMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="auto",
        )
        assert assembler._effective_mode == "composite"

    def test_auto_selects_lexical_when_embedding_absent(self):
        """BM25-only model (no EmbeddingField) should resolve to 'lexical' under auto."""
        assembler = ContextAssembler(
            model_class=BM25OnlyMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="auto",
        )
        assert assembler._effective_mode == "lexical"

    def test_auto_selects_composite_when_neither_field_present(self):
        assembler = ContextAssembler(
            model_class=CompositeOnlyMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="auto",
        )
        assert assembler._effective_mode == "composite"

    def test_auto_is_default(self):
        """retrieval_mode defaults to 'auto' — no explicit kwarg needed."""
        assembler = ContextAssembler(
            model_class=HybridMemory,
            score_weights={"relevance": 1.0},
        )
        # Model has both fields → should resolve to hybrid
        assert assembler._effective_mode == "hybrid"


# ---------------------------------------------------------------------------
# 2. hybrid mode — explicit raises when fields absent
# ---------------------------------------------------------------------------


class TestHybridModeRaises:
    def test_hybrid_raises_without_bm25(self):
        with pytest.raises(QueryException, match="BM25Field"):
            ContextAssembler(
                model_class=EmbeddingOnlyMemory,
                score_weights={"relevance": 1.0},
                retrieval_mode="hybrid",
            )

    def test_hybrid_raises_without_embedding(self):
        with pytest.raises(QueryException, match="EmbeddingField"):
            ContextAssembler(
                model_class=BM25OnlyMemory,
                score_weights={"relevance": 1.0},
                retrieval_mode="hybrid",
            )

    def test_hybrid_raises_without_either_field(self):
        with pytest.raises(QueryException):
            ContextAssembler(
                model_class=CompositeOnlyMemory,
                score_weights={"relevance": 1.0},
                retrieval_mode="hybrid",
            )

    def test_hybrid_does_not_raise_when_both_fields_present(self):
        """Sanity: explicit hybrid on fully-equipped model must not raise."""
        assembler = ContextAssembler(
            model_class=HybridMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="hybrid",
        )
        assert assembler._effective_mode == "hybrid"


# ---------------------------------------------------------------------------
# 3. composite mode — forced regardless of field presence
# ---------------------------------------------------------------------------


class TestCompositeModeForced:
    def test_composite_mode_forces_composite_even_with_hybrid_fields(self):
        assembler = ContextAssembler(
            model_class=HybridMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="composite",
        )
        assert assembler._effective_mode == "composite"

    def test_composite_mode_on_composite_only_model(self):
        assembler = ContextAssembler(
            model_class=CompositeOnlyMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="composite",
        )
        assert assembler._effective_mode == "composite"


# ---------------------------------------------------------------------------
# 4. field detection attributes
# ---------------------------------------------------------------------------


class TestFieldDetection:
    def test_bm25_field_detected(self):
        assembler = ContextAssembler(
            model_class=HybridMemory,
            score_weights={"relevance": 1.0},
        )
        assert assembler._bm25_field is not None
        assert assembler._bm25_field_name == "content_index"

    def test_embedding_field_detected(self):
        assembler = ContextAssembler(
            model_class=HybridMemory,
            score_weights={"relevance": 1.0},
        )
        assert assembler._embedding_field is not None
        assert assembler._embedding_field_name == "embedding"

    def test_bm25_not_detected_on_composite_model(self):
        assembler = ContextAssembler(
            model_class=CompositeOnlyMemory,
            score_weights={"relevance": 1.0},
        )
        assert assembler._bm25_field is None
        assert assembler._bm25_field_name is None

    def test_embedding_not_detected_on_composite_model(self):
        assembler = ContextAssembler(
            model_class=CompositeOnlyMemory,
            score_weights={"relevance": 1.0},
        )
        assert assembler._embedding_field is None
        assert assembler._embedding_field_name is None


# ---------------------------------------------------------------------------
# 5. backwards compatibility: score_weights callers unchanged
# ---------------------------------------------------------------------------


class TestBackwardsCompatibility:
    def test_composite_only_caller_unchanged(self):
        """Pre-existing callers that pass score_weights on models without
        BM25/EmbeddingField must work exactly as before — composite path,
        no error, assemble() returns empty result when DB is empty.
        """
        _flush()
        assembler = ContextAssembler(
            model_class=CompositeOnlyMemory,
            score_weights={"relevance": 1.0},
        )
        assert assembler._effective_mode == "composite"
        result = assembler.assemble(query_cues={"topic": "test"})
        assert result.records == []

    def test_forced_composite_with_hybrid_fields(self):
        """Callers who pass retrieval_mode='composite' must always use the
        composite path even if BM25 and EmbeddingField are present."""
        _flush()
        assembler = ContextAssembler(
            model_class=HybridMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="composite",
        )
        # Just verify it dispatches to _pull_path_composite without error
        with patch.object(
            assembler, "_pull_path_composite", wraps=assembler._pull_path_composite
        ) as mock_composite:
            assembler.assemble(query_cues={"topic": "test"})
            mock_composite.assert_called_once()


# ---------------------------------------------------------------------------
# 6. _pull_path dispatch
# ---------------------------------------------------------------------------


class TestPullPathDispatch:
    def test_hybrid_mode_calls_pull_path_hybrid(self):
        assembler = ContextAssembler(
            model_class=HybridMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="hybrid",
        )
        with patch.object(
            assembler, "_pull_path_hybrid", return_value=([], [])
        ) as mock_hybrid:
            assembler.assemble(query_cues={"topic": "test"})
            mock_hybrid.assert_called_once()

    def test_composite_mode_calls_pull_path_composite(self):
        assembler = ContextAssembler(
            model_class=CompositeOnlyMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="composite",
        )
        with patch.object(
            assembler, "_pull_path_composite", return_value=([], [])
        ) as mock_composite:
            assembler.assemble(query_cues={"topic": "test"})
            mock_composite.assert_called_once()

    def test_hybrid_fallback_when_both_signals_empty(self):
        """When BM25 + vector both return empty, hybrid falls back to
        composite path without crashing."""
        _flush()
        assembler = ContextAssembler(
            model_class=HybridMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="hybrid",
        )
        with (
            patch(
                "src.popoto.fields.bm25_field.BM25Field.search",
                return_value=[],
            ),
            patch.object(
                assembler,
                "_pull_path_composite",
                return_value=([], []),
            ) as mock_composite,
        ):
            # Vector returns empty because no embeddings stored
            result = assembler.assemble(query_cues={"topic": "test"})
            # Should fall back gracefully — either via composite or empty result
            assert isinstance(result.records, list)

    def test_hybrid_fallback_when_bm25_raises(self):
        """When BM25.search raises, hybrid continues with vector-only signal
        (or falls back to composite if vector is also empty)."""
        _flush()
        assembler = ContextAssembler(
            model_class=HybridMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="hybrid",
        )
        with patch(
            "src.popoto.fields.bm25_field.BM25Field.search",
            side_effect=Exception("BM25 failure"),
        ):
            # Should not raise — logs warning, continues
            result = assembler.assemble(query_cues={"topic": "test"})
            assert isinstance(result.records, list)

    def test_hybrid_fallback_on_fuse_exception(self):
        """When fuse() raises, hybrid falls back to composite path."""
        _flush()
        assembler = ContextAssembler(
            model_class=HybridMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="hybrid",
        )
        mock_results = [("key1", 1.0)]
        with (
            patch(
                "src.popoto.fields.bm25_field.BM25Field.search",
                return_value=mock_results,
            ),
            patch.object(
                assembler.model_class.query.__class__,
                "fuse",
                side_effect=Exception("fuse failure"),
                create=True,
            ),
            patch.object(
                assembler,
                "_pull_path_composite",
                return_value=([], []),
            ) as mock_composite,
        ):
            assembler.assemble(query_cues={"topic": "test"})
            # Fallback to composite must be called when fuse raises
            mock_composite.assert_called_once()


# ---------------------------------------------------------------------------
# 7. QueryBuilder._get_vector_scores
# ---------------------------------------------------------------------------


class TestGetVectorScores:
    def test_returns_list_on_model_without_embedding_field(self):
        """_get_vector_scores returns [] when the model has no EmbeddingField."""
        from src.popoto.models.query import QueryBuilder

        builder = QueryBuilder(CompositeOnlyMemory.query)
        result = builder._get_vector_scores("test query", limit=10)
        assert isinstance(result, list)
        assert result == []

    def test_returns_list_on_empty_query_text(self):
        """_get_vector_scores returns [] on empty query text."""
        from src.popoto.models.query import QueryBuilder

        builder = QueryBuilder(HybridMemory.query)
        result = builder._get_vector_scores("", limit=10)
        assert result == []

    def test_returns_list_on_whitespace_query(self):
        from src.popoto.models.query import QueryBuilder

        builder = QueryBuilder(HybridMemory.query)
        result = builder._get_vector_scores("   ", limit=10)
        assert result == []

    def test_returns_list_when_provider_is_none(self):
        """_get_vector_scores returns [] when EmbeddingField.provider is None."""
        from src.popoto.models.query import QueryBuilder

        builder = QueryBuilder(HybridMemory.query)
        # HybridMemory.embedding has no provider configured by default
        result = builder._get_vector_scores("test query", limit=10)
        assert isinstance(result, list)
        assert result == []

    def test_returns_tuples_when_provider_configured(self):
        """With a mock provider, _get_vector_scores returns (str, float) tuples."""
        import numpy as np
        from src.popoto.models.query import QueryBuilder

        # Mock provider
        mock_provider = MagicMock()
        mock_provider.embed.return_value = [[0.5, 0.5]]

        # Mock EmbeddingField.load_embeddings to return a fake matrix
        fake_matrix = np.array([[0.7071, 0.7071]], dtype=np.float32)
        fake_keys = ["HybridMemory:123"]

        # provider is a property — patch via the class descriptor, not the instance
        with (
            patch.object(
                HybridMemory._meta.fields["embedding"].__class__,
                "provider",
                new_callable=lambda: property(lambda self: mock_provider),
            ),
            patch(
                "src.popoto.fields.embedding_field.EmbeddingField.load_embeddings",
                return_value=(fake_matrix, fake_keys),
            ),
        ):
            builder = QueryBuilder(HybridMemory.query)
            result = builder._get_vector_scores("test query", limit=10)

        assert isinstance(result, list)
        assert len(result) > 0
        for item in result:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], str)
            assert isinstance(item[1], float)


# ---------------------------------------------------------------------------
# 8. constants
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 9. lexical mode — new first-class retrieval mode
# ---------------------------------------------------------------------------


class TestLexicalMode:
    """Tests for the new 'lexical' retrieval mode (BM25 + graph, no embeddings)."""

    def test_explicit_lexical_mode_on_bm25_model(self):
        """Explicit retrieval_mode='lexical' on a model with BM25Field must not raise."""
        assembler = ContextAssembler(
            model_class=BM25OnlyMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="lexical",
        )
        assert assembler._effective_mode == "lexical"

    def test_explicit_lexical_raises_without_bm25(self):
        """Explicit retrieval_mode='lexical' on a model without BM25Field must raise QueryException."""
        with pytest.raises(QueryException, match="BM25Field"):
            ContextAssembler(
                model_class=CompositeOnlyMemory,
                score_weights={"relevance": 1.0},
                retrieval_mode="lexical",
            )

    def test_explicit_lexical_raises_when_only_embedding_field(self):
        """Explicit 'lexical' on embedding-only model (no BM25Field) must raise."""
        with pytest.raises(QueryException, match="BM25Field"):
            ContextAssembler(
                model_class=EmbeddingOnlyMemory,
                score_weights={"relevance": 1.0},
                retrieval_mode="lexical",
            )

    def test_unknown_mode_raises_query_exception(self):
        """An unknown retrieval_mode string must raise QueryException (no silent fallback)."""
        with pytest.raises(QueryException, match="not a recognised mode"):
            ContextAssembler(
                model_class=CompositeOnlyMemory,
                score_weights={"relevance": 1.0},
                retrieval_mode="lexcal",  # typo
            )

    def test_lexical_mode_dispatches_to_pull_path_hybrid(self):
        """(BLOCKER 2) _pull_path must route 'lexical' mode to _pull_path_hybrid,
        NOT to _pull_path_composite."""
        assembler = ContextAssembler(
            model_class=BM25OnlyMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="lexical",
        )
        assert assembler._effective_mode == "lexical"
        with (
            patch.object(
                assembler, "_pull_path_hybrid", return_value=([], [])
            ) as mock_hybrid,
            patch.object(
                assembler, "_pull_path_composite", return_value=([], [])
            ) as mock_composite,
        ):
            assembler.assemble(query_cues={"topic": "test"})
            mock_hybrid.assert_called_once()
            mock_composite.assert_not_called()

    def test_vector_branch_never_called_in_lexical_mode(self):
        """(BLOCKER 1 / C2) In lexical mode, _get_vector_scores must NEVER be called.
        Uses a call-count sentinel — NOT raise AssertionError — because the warn-only
        except Exception in _pull_path_hybrid would swallow AssertionError and silently
        pass a regressed test."""
        assembler = ContextAssembler(
            model_class=BM25OnlyMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="lexical",
        )
        assert assembler._embedding_field is None, "lexical mode requires no EmbeddingField"

        call_count = {"n": 0}

        def counting_get_vector_scores(self_qb, *args, **kwargs):
            call_count["n"] += 1
            return []

        with (
            patch(
                "src.popoto.fields.bm25_field.BM25Field.search",
                return_value=[],
            ),
            patch(
                "src.popoto.models.query.QueryBuilder._get_vector_scores",
                counting_get_vector_scores,
            ),
        ):
            result = assembler.assemble(query_cues={"topic": "test"})
            assert isinstance(result.records, list)

        assert call_count["n"] == 0, (
            f"_get_vector_scores was called {call_count['n']} times in lexical mode; "
            "the embedding_field guard is broken"
        )

    def test_lexical_mode_does_not_call_composite_on_bm25_results(self):
        """(BLOCKER 2 dispatch test) When BM25 returns results in lexical mode,
        _pull_path_composite must NOT be entered."""
        _flush()
        assembler = ContextAssembler(
            model_class=BM25OnlyMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="lexical",
        )
        mock_bm25_results = [("BM25OnlyMemory:1", 0.9), ("BM25OnlyMemory:2", 0.8)]
        with (
            patch(
                "src.popoto.fields.bm25_field.BM25Field.search",
                return_value=mock_bm25_results,
            ),
            patch.object(
                assembler, "_pull_path_composite", return_value=([], [])
            ) as mock_composite,
            patch.object(assembler.model_class.query.__class__, "fuse", return_value=[], create=True),
        ):
            assembler.assemble(query_cues={"topic": "test"})
            mock_composite.assert_not_called()

    def test_lexical_zero_bm25_hits_falls_back_to_composite(self):
        """When BM25 returns zero hits in lexical mode, the path degrades to composite."""
        _flush()
        assembler = ContextAssembler(
            model_class=BM25OnlyMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="lexical",
        )
        with (
            patch(
                "src.popoto.fields.bm25_field.BM25Field.search",
                return_value=[],  # zero BM25 hits
            ),
            patch.object(
                assembler, "_pull_path_composite", return_value=([], [])
            ) as mock_composite,
        ):
            result = assembler.assemble(query_cues={"topic": "zzz flurble xyzzy gibberish"})
            # Zero BM25 hits → should fall back to composite (by design)
            mock_composite.assert_called_once()
            assert isinstance(result.records, list)

    def test_auto_lexical_empty_db_returns_list(self):
        """BM25-only model with 'auto' resolving to 'lexical' must return
        a list (empty) when DB is empty, not raise."""
        _flush()
        assembler = ContextAssembler(
            model_class=BM25OnlyMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="auto",
        )
        assert assembler._effective_mode == "lexical"
        result = assembler.assemble(query_cues={"topic": "deployment"})
        assert isinstance(result.records, list)


class TestConstants:
    def test_rrf_k_is_60(self):
        assert RRF_K == 60

    def test_hybrid_candidate_multiplier_is_positive(self):
        assert HYBRID_CANDIDATE_MULTIPLIER > 0
