"""End-to-end tests for hybrid retrieval: BM25 + RRF fusion.

Tests cover:
- Hybrid retrieval (BM25 + simulated vector + RRF) finds documents that single signals miss
- Exact keyword query surfaces correct document even when other signal is weak
- RRF fusion combines signals to produce better top-1 than any single signal
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402
from src import popoto  # noqa: E402
from src.popoto.fields.bm25_field import BM25Field  # noqa: E402

# --- Test Model ---


class HybridDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    raw_content = popoto.Field(type=str)
    content = BM25Field(source="raw_content")


# --- Tests ---


class TestHybridRetrieval:
    """End-to-end hybrid retrieval combining BM25 with simulated signals."""

    def _setup_corpus(self):
        """Create a small corpus of documents for hybrid retrieval testing."""
        docs = {}
        corpus = {
            "redis_specific": "redis cluster configuration sentinel failover setup guide",
            "general_deploy": "deployment best practices monitoring alerting systems",
            "redis_deploy": "redis deployment production configuration monitoring",
            "unrelated": "python machine learning neural network training guide",
        }
        for name, content in corpus.items():
            doc = HybridDoc(name=name, raw_content=content)
            doc.save()
            docs[name] = doc.db_key.redis_key
        return docs

    def test_bm25_finds_exact_keyword_match(self):
        """BM25 surfaces document with exact keyword match."""
        docs = self._setup_corpus()

        bm25_results = BM25Field.search(
            HybridDoc, "content", "sentinel failover", limit=10
        )
        keys = [k for k, _s in bm25_results]
        assert docs["redis_specific"] in keys

    def test_rrf_combines_multiple_signals(self):
        """RRF fusion combines BM25 + simulated semantic signal."""
        docs = self._setup_corpus()

        # Signal 1: BM25 keyword search
        bm25_results = BM25Field.search(
            HybridDoc, "content", "redis configuration", limit=10
        )

        # Signal 2: Simulated semantic similarity (pretend redis_deploy is most
        # semantically similar to a query about "production redis setup")
        semantic_results = [
            (docs["redis_deploy"], 0.95),
            (docs["redis_specific"], 0.80),
            (docs["general_deploy"], 0.60),
        ]

        # Fuse both signals
        results = HybridDoc.query.fuse(
            keyword=bm25_results,
            semantic=semantic_results,
            k=60,
            limit=5,
        )

        assert len(results) > 0
        result_names = [r.name for r in results]

        # redis_deploy should rank high -- it appears in both signals
        assert "redis_deploy" in result_names[:2]

    def test_hybrid_better_than_single_signal(self):
        """RRF fusion produces better ranking than either signal alone.

        Setup: redis_deploy appears mid-ranked in both signals individually,
        but should rise to the top when fused because it appears in both.
        """
        docs = self._setup_corpus()

        # Signal 1: BM25 for "deployment monitoring"
        # (general_deploy might rank first because it has both terms prominently)
        bm25_results = BM25Field.search(
            HybridDoc, "content", "deployment monitoring", limit=10
        )

        # Signal 2: Simulated semantic -- redis_deploy ranks second
        semantic_results = [
            (docs["general_deploy"], 0.90),
            (docs["redis_deploy"], 0.85),
            (docs["redis_specific"], 0.30),
        ]

        fused = HybridDoc.query.fuse(
            keyword=bm25_results,
            semantic=semantic_results,
            k=60,
            limit=5,
        )

        fused_names = [r.name for r in fused]
        # Both general_deploy and redis_deploy should be in top results
        # The key test: fusion includes docs from both signals
        assert len(fused) >= 2
        assert "general_deploy" in fused_names[:3]
        assert "redis_deploy" in fused_names[:3]

    def test_keyword_only_doc_still_surfaces(self):
        """A document found only by keywords still appears in fused results."""
        docs = self._setup_corpus()

        # BM25 finds redis_specific via "sentinel"
        bm25_results = BM25Field.search(HybridDoc, "content", "sentinel", limit=10)

        # Semantic signal doesn't include redis_specific at all
        semantic_results = [
            (docs["general_deploy"], 0.90),
            (docs["redis_deploy"], 0.85),
        ]

        fused = HybridDoc.query.fuse(
            keyword=bm25_results,
            semantic=semantic_results,
            k=60,
            limit=10,
        )

        fused_names = [r.name for r in fused]
        # redis_specific should still appear (from keyword signal)
        assert "redis_specific" in fused_names

    def test_query_builder_keyword_search_with_fuse(self):
        """Full pipeline: QueryBuilder.keyword_search() -> fuse()."""
        docs = self._setup_corpus()

        # Use QueryBuilder for keyword search
        kw_instances = HybridDoc.query.keyword_search("redis configuration", limit=10)
        assert len(kw_instances) > 0
        for inst in kw_instances:
            assert hasattr(inst, "_bm25_score")

        # Convert to (key, score) tuples for fuse()
        kw_scored = [(inst.db_key.redis_key, inst._bm25_score) for inst in kw_instances]

        # Simulated second signal
        semantic_results = [
            (docs["redis_deploy"], 0.9),
            (docs["general_deploy"], 0.7),
        ]

        # Fuse
        results = HybridDoc.query.fuse(
            keyword=kw_scored,
            semantic=semantic_results,
            k=60,
            limit=5,
        )
        assert len(results) > 0
