"""Tests for RRF (Reciprocal Rank Fusion) via QueryBuilder.fuse().

Tests cover:
- Two lists with overlapping documents, RRF produces correct merged ranking
- Document in all lists outranks document in one list
- Single list degenerates to that list's ranking
- Empty lists return empty results
- k parameter affects score distribution
- post_filter callback works
- No ranked lists raises QueryException
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402
from src import popoto  # noqa: E402
from src.popoto.models.query import QueryException  # noqa: E402

# --- Test Model ---


class RRFDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.Field(type=str)


# --- Helper ---


def _make_docs(names):
    """Create and save documents, return dict of name -> redis_key."""
    docs = {}
    for name in names:
        doc = RRFDoc(name=name, content=f"content for {name}")
        doc.save()
        docs[name] = doc.db_key.redis_key
    return docs


# --- Tests ---


class TestRRFFusionBasics:
    """Basic RRF fusion behavior."""

    def test_two_lists_overlapping(self):
        """RRF with two overlapping lists produces merged ranking."""
        docs = _make_docs(["rrf_a", "rrf_b", "rrf_c"])

        list1 = [
            (docs["rrf_a"], 10.0),
            (docs["rrf_b"], 5.0),
        ]
        list2 = [
            (docs["rrf_b"], 8.0),
            (docs["rrf_c"], 3.0),
        ]

        results = RRFDoc.query.fuse(
            keyword=list1,
            semantic=list2,
            k=60,
            limit=10,
        )

        assert len(results) > 0
        # rrf_b appears in both lists, should rank highest
        assert results[0].name == "rrf_b"

    def test_doc_in_all_lists_outranks_single(self):
        """Document appearing in all lists outranks one in a single list."""
        docs = _make_docs(["multi_all", "multi_one"])

        list1 = [(docs["multi_all"], 5.0), (docs["multi_one"], 3.0)]
        list2 = [(docs["multi_all"], 4.0)]
        list3 = [(docs["multi_all"], 3.0)]

        results = RRFDoc.query.fuse(
            signal1=list1,
            signal2=list2,
            signal3=list3,
            k=60,
            limit=10,
        )

        assert len(results) >= 1
        assert results[0].name == "multi_all"

    def test_single_list_preserves_ranking(self):
        """Single list degenerates to that list's ranking order."""
        docs = _make_docs(["single_a", "single_b", "single_c"])

        single_list = [
            (docs["single_a"], 10.0),
            (docs["single_b"], 5.0),
            (docs["single_c"], 1.0),
        ]

        results = RRFDoc.query.fuse(
            only_list=single_list,
            k=60,
            limit=10,
        )

        names = [r.name for r in results]
        assert names == ["single_a", "single_b", "single_c"]

    def test_rrf_score_attached_to_instances(self):
        """Fused results have _rrf_score attribute."""
        docs = _make_docs(["score_a"])

        results = RRFDoc.query.fuse(
            list1=[(docs["score_a"], 5.0)],
            k=60,
            limit=10,
        )

        assert len(results) == 1
        assert hasattr(results[0], "_rrf_score")
        assert results[0]._rrf_score > 0


class TestRRFFusionEdgeCases:
    """Edge cases and error conditions."""

    def test_empty_lists_return_empty(self):
        """All empty ranked lists return empty results."""
        results = RRFDoc.query.fuse(
            list1=[],
            list2=[],
            k=60,
            limit=10,
        )
        assert results == []

    def test_no_ranked_lists_raises(self):
        """fuse() with no ranked lists raises QueryException."""
        with pytest.raises(QueryException):
            RRFDoc.query.fuse(k=60, limit=10)

    def test_limit_respected(self):
        """fuse() respects the limit parameter."""
        docs = _make_docs(["lim_a", "lim_b", "lim_c", "lim_d", "lim_e"])

        ranked = [
            (docs["lim_a"], 10.0),
            (docs["lim_b"], 8.0),
            (docs["lim_c"], 6.0),
            (docs["lim_d"], 4.0),
            (docs["lim_e"], 2.0),
        ]

        results = RRFDoc.query.fuse(
            signal=ranked,
            k=60,
            limit=3,
        )

        assert len(results) <= 3


class TestRRFParameters:
    """Test RRF parameter effects."""

    def test_k_parameter_affects_scores(self):
        """Different k values produce different absolute scores."""
        docs = _make_docs(["k_a"])

        ranked = [(docs["k_a"], 10.0)]

        results_low_k = RRFDoc.query.fuse(
            signal=ranked,
            k=1,
            limit=10,
        )
        results_high_k = RRFDoc.query.fuse(
            signal=ranked,
            k=100,
            limit=10,
        )

        # Lower k = higher score (1/(1+1) = 0.5 vs 1/(100+1) ~ 0.0099)
        assert results_low_k[0]._rrf_score > results_high_k[0]._rrf_score

    def test_post_filter_removes_results(self):
        """post_filter callback removes results that don't pass."""
        docs = _make_docs(["filt_keep", "filt_drop"])

        ranked = [
            (docs["filt_keep"], 10.0),
            (docs["filt_drop"], 5.0),
        ]

        # Only keep results with keys containing "filt_keep"
        results = RRFDoc.query.fuse(
            signal=ranked,
            k=60,
            limit=10,
            post_filter=lambda key, score: "filt_keep" in key,
        )

        names = [r.name for r in results]
        assert "filt_keep" in names
        assert "filt_drop" not in names


class TestRRFWeightedFusion:
    """Tests for the weighted RRF primitive (issue #457)."""

    def test_weights_none_matches_unweighted(self):
        """weights=None is byte-for-byte equivalent to today's unweighted RRF."""
        docs = _make_docs(["wnone_a", "wnone_b", "wnone_c"])

        list1 = [(docs["wnone_a"], 10.0), (docs["wnone_b"], 5.0)]
        list2 = [(docs["wnone_b"], 8.0), (docs["wnone_c"], 3.0)]

        unweighted = RRFDoc.query.fuse(keyword=list1, semantic=list2, k=60, limit=10)
        explicit_none = RRFDoc.query.fuse(
            keyword=list1, semantic=list2, k=60, limit=10, weights=None
        )

        assert [r.name for r in unweighted] == [r.name for r in explicit_none]
        assert [r._rrf_score for r in unweighted] == [
            r._rrf_score for r in explicit_none
        ]

    def test_weights_all_one_matches_unweighted_scores(self):
        """An explicit all-1.0 weights dict reproduces unweighted RRF scores."""
        docs = _make_docs(["wone_a", "wone_b"])

        ranked = [(docs["wone_a"], 10.0), (docs["wone_b"], 5.0)]

        unweighted = RRFDoc.query.fuse(signal=ranked, k=60, limit=10)
        weighted = RRFDoc.query.fuse(
            signal=ranked, k=60, limit=10, weights={"signal": 1.0}
        )

        assert [r._rrf_score for r in unweighted] == [r._rrf_score for r in weighted]

    def test_downweighted_arm_loses_influence(self):
        """A down-weighted arm's votes count less, changing the merged order."""
        docs = _make_docs(["dw_lexical_top", "dw_vector_top"])

        # Without weighting, "dw_vector_top" (rank 1 in vector) ties/competes
        # with "dw_lexical_top" (rank 1 in keyword) plus a rank-2 keyword hit
        # for the same doc is absent, so with equal weight the two rank-1
        # entries score identically and the vector arm's pick could win ties.
        # Down-weighting vector must make the keyword arm's rank-1 pick win
        # unambiguously.
        keyword_list = [(docs["dw_lexical_top"], 10.0)]
        vector_list = [(docs["dw_vector_top"], 10.0)]

        unweighted = RRFDoc.query.fuse(
            keyword=keyword_list, vector=vector_list, k=60, limit=10
        )
        # Equal weight -> tie broken by insertion/dict order, not a strong
        # assertion target. Assert the down-weighted case explicitly instead.
        assert len(unweighted) == 2

        weighted = RRFDoc.query.fuse(
            keyword=keyword_list,
            vector=vector_list,
            k=60,
            limit=10,
            weights={"keyword": 1.0, "vector": 0.1},
        )

        assert weighted[0].name == "dw_lexical_top"
        keyword_score = next(
            r._rrf_score for r in weighted if r.name == "dw_lexical_top"
        )
        vector_score = next(r._rrf_score for r in weighted if r.name == "dw_vector_top")
        assert keyword_score > vector_score

    def test_weights_default_missing_list_to_one(self):
        """Lists not present in the weights dict default to weight 1.0."""
        docs = _make_docs(["wmiss_a", "wmiss_b"])

        list_a = [(docs["wmiss_a"], 10.0)]
        list_b = [(docs["wmiss_b"], 10.0)]

        # Only "a" is named in weights; "b" should still default to 1.0.
        results = RRFDoc.query.fuse(
            a=list_a, b=list_b, k=60, limit=10, weights={"a": 2.0}
        )

        score_a = next(r._rrf_score for r in results if r.name == "wmiss_a")
        score_b = next(r._rrf_score for r in results if r.name == "wmiss_b")
        assert score_a == pytest.approx(2.0 * score_b)

    def test_weights_never_treated_as_ranked_list(self):
        """weights is an explicit kwarg, never swept into **ranked_lists."""
        docs = _make_docs(["wsafe_a"])

        ranked = [(docs["wsafe_a"], 10.0)]

        # If `weights` were accidentally captured by **ranked_lists, this
        # call would either raise (dict isn't a list of (key, score) tuples)
        # or silently corrupt the fusion. It must succeed and only fuse
        # `signal`.
        results = RRFDoc.query.fuse(
            signal=ranked, k=60, limit=10, weights={"signal": 0.5}
        )

        assert len(results) == 1
        assert results[0].name == "wsafe_a"
