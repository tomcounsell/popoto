"""Unit tests for the CSR assertion engine (pure functions, no Redis).

Pins the pass/fail/missing-id/empty-list/boundary-k semantics of every
assertion type, per the plan's Failure Path Test Strategy.
"""

import pytest

from tests.benchmarks.csr.satisfaction import (
    CoversTopic,
    Excludes,
    InTopK,
    NoneOlderThan,
    RanksAbove,
    evaluate_all,
)

RANKED = ["a", "b", "c", "d"]
META = {
    "a": {"timestamp": 400.0, "topic": "alpha"},
    "b": {"timestamp": 300.0, "topic": "alpha"},
    "c": {"timestamp": 200.0, "topic": "beta"},
    "d": {"timestamp": 100.0, "topic": "beta"},
    "e": {"timestamp": 50.0, "topic": "gamma"},  # planted but never ranked
}


@pytest.mark.parametrize(
    "assertion,ranked,expected",
    [
        # --- InTopK: pass / fail-by-rank / missing id / empty list ---
        (InTopK("a", k=2), RANKED, True),
        (InTopK("c", k=2), RANKED, False),
        (InTopK("zz", k=4), RANKED, False),
        (InTopK("a", k=4), [], False),
        # boundary k: k <= 0 and k > len clamp to the whole list
        (InTopK("d", k=0), RANKED, True),
        (InTopK("d", k=-3), RANKED, True),
        (InTopK("d", k=99), RANKED, True),
        # --- RanksAbove: pass / fail / missing either id / empty list ---
        (RanksAbove("a", "c"), RANKED, True),
        (RanksAbove("c", "a"), RANKED, False),
        (RanksAbove("zz", "a"), RANKED, False),
        (RanksAbove("a", "zz"), RANKED, False),
        (RanksAbove("a", "b"), [], False),
        # --- NoneOlderThan: pass / fail / empty list vacuous / boundary k ---
        (NoneOlderThan(50.0, k=4), RANKED, True),
        (NoneOlderThan(250.0, k=4), RANKED, False),
        (NoneOlderThan(250.0, k=2), RANKED, True),  # c,d outside top-2
        (NoneOlderThan(999.0, k=4), [], True),
        (NoneOlderThan(150.0, k=0), RANKED, False),  # k=0 clamps to whole list
        # --- CoversTopic: pass / fail / vacuous topic / empty list ---
        (CoversTopic("alpha", k=2), RANKED, True),
        (CoversTopic("beta", k=2), RANKED, False),
        (CoversTopic("beta", k=4), RANKED, True),
        (CoversTopic("gamma", k=4), RANKED, False),  # e planted, never ranked
        (CoversTopic("nosuch", k=4), RANKED, True),  # vacuous: no planted ids
        (CoversTopic("alpha", k=4), [], False),
        (CoversTopic("nosuch", k=4), [], True),
        # --- Excludes: pass / fail / empty list vacuous / boundary k ---
        (Excludes("zz", k=4), RANKED, True),
        (Excludes("a", k=4), RANKED, False),
        (Excludes("d", k=2), RANKED, True),  # d outside top-2
        (Excludes("a", k=4), [], True),
        (Excludes("d", k=0), RANKED, False),  # k=0 clamps to whole list
    ],
)
def test_assertion_semantics(assertion, ranked, expected):
    assert assertion.evaluate(ranked, META) is expected


def test_missing_id_never_raises():
    """Ids absent from corpus/meta fail deterministically, never KeyError."""
    for assertion in (
        InTopK("ghost"),
        RanksAbove("ghost", "a"),
        RanksAbove("a", "ghost"),
        Excludes("ghost"),
    ):
        # Must not raise; Excludes("ghost") is True, the rest False.
        assertion.evaluate(RANKED, META)


def test_none_older_than_skips_unplanted_ranked_id():
    """A ranked id with no planted_meta entry cannot prove age -> skipped."""
    assert NoneOlderThan(500.0, k=2).evaluate(["ghost", "a"], META) is False
    assert NoneOlderThan(500.0, k=1).evaluate(["ghost", "a"], META) is True


def test_evaluate_all_counts_and_detail():
    assertions = [InTopK("a", k=2), InTopK("c", k=2), Excludes("zz")]
    passed, total, per_assertion = evaluate_all(assertions, RANKED, META)
    assert (passed, total) == (2, 3)
    assert [p["passed"] for p in per_assertion] == [True, False, True]
    assert per_assertion[0]["type"] == "InTopK"
    assert "InTopK" in per_assertion[0]["assertion"]


def test_evaluate_all_empty_assertions():
    passed, total, per_assertion = evaluate_all([], RANKED, META)
    assert (passed, total, per_assertion) == (0, 0, [])
