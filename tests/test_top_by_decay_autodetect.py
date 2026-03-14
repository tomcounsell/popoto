"""Tests for top_by_decay() field_name auto-detection.

Tests cover:
- Auto-detect single DecayingSortedField (field_name omitted)
- Raise QueryException when model has zero DecayingSortedFields
- Raise QueryException when model has multiple DecayingSortedFields
- Explicit field_name parameter still works as before
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.models.query import QueryException


# --- Test Models ---


class SingleDecayModel(popoto.Model):
    """Model with exactly one DecayingSortedField."""
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField()


class NoDecayModel(popoto.Model):
    """Model with no DecayingSortedField."""
    name = popoto.UniqueKeyField()
    score = popoto.SortedField(type=float, default=0.0)


class MultiDecayModel(popoto.Model):
    """Model with two DecayingSortedFields."""
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField()
    freshness = DecayingSortedField()


# --- Setup / Teardown ---


def setup_module():
    for model in [SingleDecayModel, NoDecayModel, MultiDecayModel]:
        model.delete_all()


def teardown_module():
    for model in [SingleDecayModel, NoDecayModel, MultiDecayModel]:
        model.delete_all()


# --- Auto-detection tests ---


class TestAutoDetectSingleField:
    """When model has exactly one DecayingSortedField, field_name is optional."""

    def setup_method(self):
        SingleDecayModel.delete_all()

    def teardown_method(self):
        SingleDecayModel.delete_all()

    def test_auto_detect_returns_results(self):
        """top_by_decay() without field_name works on single-DSF model."""
        SingleDecayModel.create(name="item1")
        SingleDecayModel.create(name="item2")

        results = SingleDecayModel.query.top_by_decay(n=10)
        assert len(results) == 2

    def test_auto_detect_matches_explicit(self):
        """Auto-detected result equals explicit field_name='relevance'."""
        SingleDecayModel.create(name="a")
        SingleDecayModel.create(name="b")

        auto_results = SingleDecayModel.query.top_by_decay(n=10)
        explicit_results = SingleDecayModel.query.top_by_decay("relevance", n=10)

        auto_names = [r.name for r in auto_results]
        explicit_names = [r.name for r in explicit_results]
        assert auto_names == explicit_names

    def test_auto_detect_empty_set(self):
        """Auto-detect works on empty model (returns [])."""
        results = SingleDecayModel.query.top_by_decay(n=10)
        assert results == []


class TestNoDecayFieldRaises:
    """Model with zero DecayingSortedFields raises QueryException."""

    def test_raises_on_no_decay_field(self):
        with pytest.raises(QueryException, match="has no DecayingSortedField"):
            NoDecayModel.query.top_by_decay(n=10)


class TestMultipleDecayFieldsRaises:
    """Model with multiple DecayingSortedFields raises QueryException."""

    def test_raises_on_multiple_decay_fields(self):
        with pytest.raises(QueryException, match="Multiple DecayingSortedFields"):
            MultiDecayModel.query.top_by_decay(n=10)


class TestExplicitFieldNameStillWorks:
    """Explicit field_name bypasses auto-detection."""

    def setup_method(self):
        MultiDecayModel.delete_all()

    def teardown_method(self):
        MultiDecayModel.delete_all()

    def test_explicit_on_multi_decay_model(self):
        """Specifying field_name on multi-DSF model works."""
        MultiDecayModel.create(name="x")

        results = MultiDecayModel.query.top_by_decay("relevance", n=10)
        assert len(results) == 1
        assert results[0].name == "x"

    def test_explicit_on_single_decay_model(self):
        """Specifying field_name on single-DSF model still works."""
        SingleDecayModel.create(name="y")

        results = SingleDecayModel.query.top_by_decay("relevance", n=10)
        assert len(results) == 1
        assert results[0].name == "y"
        SingleDecayModel.delete_all()
