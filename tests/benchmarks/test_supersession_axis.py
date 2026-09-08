"""Tests for the label-blind supersession producer (#692).

Fixture-based, no network access. These are unit tests of
``tests/benchmarks/supersession_axis.py`` in isolation — end-to-end tests that
exercise the producer through ``ExternalScenario`` live in
``test_external.py``.
"""

import inspect
import logging

import pytest

from src.popoto.fields.supersession import (
    SupersedeDeclinedError,
    SupersessionProtocol,
)
from src.popoto.fields.validity_field import (
    ValidityCloseBeforeStartError,
    ValidityMemberAbsentError,
)
from tests.benchmarks import supersession_axis as sa

# ---------------------------------------------------------------------------
# Label-blindness (AC1, Verification row 3)
# ---------------------------------------------------------------------------


class TestLabelBlindSignature:
    def test_identity_of_has_one_positional_param(self):
        """Structural gold-blindness assertion, mirroring #514's precedent."""
        sig = inspect.signature(sa.identity_of)
        params = list(sig.parameters.values())
        assert len(params) == 1
        assert params[0].kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )

    def test_identity_of_signature_excludes_label_params(self):
        sig = inspect.signature(sa.identity_of)
        assert "relevant_ids" not in sig.parameters
        assert "question_type" not in sig.parameters

    def test_route_write_signature_excludes_label_params(self):
        sig = inspect.signature(sa.route_write)
        assert "relevant_ids" not in sig.parameters
        assert "question_type" not in sig.parameters


# ---------------------------------------------------------------------------
# Identity rule table
# ---------------------------------------------------------------------------


class TestIdentityOf:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("I work at Acme Corp.", ("i", "work_at")),
            ("I live in Boston.", ("i", "live_in")),
            ("I now work at Acme.", None),  # "now" breaks the leading match
            ("i WORK at ACME", ("i", "work_at")),  # case-fold
            # "to" isn't a pinned preposition, so it's dropped: verb-only match.
            ("I moved to Denver last year.", ("i", "moved")),
            ("I have a dog.", ("i", "have")),
            ("I was a teacher.", ("i", "was")),
        ],
    )
    def test_positive_and_negative_cases(self, text, expected):
        assert sa.identity_of(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "The weather is nice today.",
            "You work at Acme Corp.",
            "",
            "   ",
        ],
    )
    def test_negative_cases_return_none(self, text):
        assert sa.identity_of(text) is None

    def test_none_on_empty_text(self):
        assert sa.identity_of("") is None
        assert sa.identity_of(None) is None

    def test_at_most_one_identity_per_unit(self):
        """Only the FIRST matching sentence is consulted."""
        text = "I work at Acme. I live in Boston."
        assert sa.identity_of(text) == ("i", "work_at")

    def test_non_matching_first_sentence_falls_through(self):
        text = "The weather is nice. I work at Acme."
        assert sa.identity_of(text) == ("i", "work_at")


# ---------------------------------------------------------------------------
# route_write
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, closed_key=None):
        self.closed_key = closed_key


class _FakeInstance:
    def __init__(self, save_return=True):
        self._save_return = save_return
        self.saved = False

    def save(self):
        self.saved = True
        return self._save_return


class TestRouteWritePlain:
    def test_plain_write_no_identity(self):
        stats = sa.SupersessionStats()
        instance = _FakeInstance()
        ok = sa.route_write(instance, identity=None, at=None, stats=stats)
        assert ok is True
        assert instance.saved is True
        assert stats.plain_writes == 1
        assert stats.identity_writes == 0


class TestRouteWriteFirstClaim:
    def test_first_claim_of_group_goes_through_protocol(self, monkeypatch):
        """Regression guard for the spike-2 finding: the FIRST claim of a
        group must also route through save_and_supersede, or the exclusion
        set stays empty (#692's central defect, one level down)."""
        calls = []

        def fake_save_and_supersede(instance, *, identity_key, at):
            calls.append((instance, identity_key, at))
            # First claim: nothing to close.
            return _FakeResult(closed_key=None)

        monkeypatch.setattr(
            SupersessionProtocol, "save_and_supersede", fake_save_and_supersede
        )
        stats = sa.SupersessionStats()
        instance = _FakeInstance()
        ok = sa.route_write(instance, identity=("i", "work_at"), at=1.0, stats=stats)
        assert ok is True
        assert len(calls) == 1
        assert stats.identity_writes == 1
        assert stats.supersessions == 0  # nothing closed on the first claim

    def test_second_claim_closes_first_and_exclusion_set_grows(self, monkeypatch):
        def fake_save_and_supersede(instance, *, identity_key, at):
            if fake_save_and_supersede.calls == 0:
                fake_save_and_supersede.calls += 1
                return _FakeResult(closed_key=None)
            fake_save_and_supersede.calls += 1
            return _FakeResult(closed_key="ExternalBenchmarkMemory:incumbent")

        fake_save_and_supersede.calls = 0
        monkeypatch.setattr(
            SupersessionProtocol, "save_and_supersede", fake_save_and_supersede
        )
        stats = sa.SupersessionStats()
        sa.route_write(_FakeInstance(), identity=("i", "work_at"), at=1.0, stats=stats)
        sa.route_write(_FakeInstance(), identity=("i", "work_at"), at=2.0, stats=stats)
        assert stats.identity_writes == 2
        assert stats.supersessions == 1


class TestRouteWriteFailures:
    @pytest.mark.parametrize(
        "exc_cls",
        [
            SupersedeDeclinedError,
            ValidityMemberAbsentError,
            ValidityCloseBeforeStartError,
        ],
    )
    def test_each_exception_type_counted_and_swallowed(
        self, monkeypatch, exc_cls, caplog
    ):
        def raising(instance, *, identity_key, at):
            raise exc_cls("boom")

        monkeypatch.setattr(SupersessionProtocol, "save_and_supersede", raising)
        stats = sa.SupersessionStats()
        with caplog.at_level(logging.WARNING, logger=sa.logger.name):
            ok = sa.route_write(
                _FakeInstance(), identity=("i", "work_at"), at=1.0, stats=stats
            )
        assert ok is False
        assert stats.failures == 1
        # Must not propagate -- caller sees a bool, not an exception.


# ---------------------------------------------------------------------------
# SupersessionStats
# ---------------------------------------------------------------------------


class TestSupersessionStats:
    def test_to_dict_reports_failures_even_when_zero(self):
        stats = sa.SupersessionStats()
        d = stats.to_dict()
        assert d["producer_failures"] == 0
        assert d["n_supersessions"] == 0
