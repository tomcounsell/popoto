"""Tests for the tombstone negative prior (#494).

A tombstone (#491) is durable evidence that a kind of memory was learned to be
worthless. These tests cover turning that evidence into a write-time drawdown:

- End-to-end: bury content, then write it again -> score is drawn down
- Escalation: 1/2/3 burials compound, floored at TOMBSTONE_PRIOR_FLOOR
- False-positive safety: a dissimilar record is NOT penalized at all
- Normalization: case/whitespace differences still match; empty does not
- Bounded storage: writing past TOMBSTONE_PRIOR_LIMIT evicts the OLDEST
- Telemetry: penalized writes are counted, not silent
- Kill switch: POPOTO_TOMBSTONE_PRIOR_DISABLE, read after import
- Byte-identical for non-adopters: a model with no ExistenceFilter issues
  ZERO extra Redis commands on save()
- Failure paths: unreachable Redis / corrupt count degrade to "no penalty"
- Burial recording skips models with no content fingerprint

**Non-vacuity.** Nothing here spies on ``popoto.redis_db.POPOTO_REDIS_DB``:
field modules bind their client through ``get_REDIS_DB()``, so a spy on the
module attribute would watch an object production never calls and capture
nothing, and empty-vs-empty comparisons would pass vacuously. Assertions read
real Redis state through ``popoto.get_redis()`` or spy on
``TombstonePriorStore`` methods directly. Every drawdown test asserts a
*specific* expected value rather than "no exception raised", and every test
whose behavior depends on a burial existing asserts the burial count FIRST, so
a precondition that never took effect fails at that line instead of letting an
unreachable guard pass silently.
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import logging  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from src import popoto  # noqa: E402
from src.popoto.fields.constants import Defaults  # noqa: E402
from src.popoto.fields.existence_filter import ExistenceFilter  # noqa: E402
from src.popoto.fields.tombstone_prior import (  # noqa: E402
    STAT_DRAWDOWN_TOTAL,
    STAT_PENALIZED,
    TombstonePriorStore,
    digest_fingerprint,
    penalty_for,
)
from src.popoto.fields.decaying_sorted_field import (  # noqa: E402
    DecayingSortedField,
)
from src.popoto.fields.write_filter import WriteFilterMixin  # noqa: E402
from src.popoto.recipes.memory_lifecycle import MemoryLifecycle  # noqa: E402

# --- Test Models ---


class PriorMemory(WriteFilterMixin, popoto.Model):
    """Carries a content fingerprint, so the negative prior applies."""

    name = popoto.UniqueKeyField()
    content = popoto.Field(type=str)
    importance = popoto.FloatField(default=0.0)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.content,
    )

    def compute_filter_score(self):
        return self.importance or 0.0


class PlainMemory(WriteFilterMixin, popoto.Model):
    """No ExistenceFilter at all — must see byte-identical write behavior."""

    name = popoto.UniqueKeyField()
    content = popoto.Field(type=str)
    importance = popoto.FloatField(default=0.0)

    def compute_filter_score(self):
        return self.importance or 0.0


class NoFingerprintFnMemory(WriteFilterMixin, popoto.Model):
    """Has an ExistenceFilter but no fingerprint_fn — no content identity."""

    name = popoto.UniqueKeyField()
    content = popoto.Field(type=str)
    importance = popoto.FloatField(default=0.0)
    bloom = ExistenceFilter(error_rate=0.01, capacity=1000)

    def compute_filter_score(self):
        return self.importance or 0.0


class LifecyclePriorMemory(popoto.Model):
    """Forgettable by MemoryLifecycle AND carrying a content fingerprint."""

    name = popoto.UniqueKeyField()
    tier = popoto.KeyField(type=str, default="episodic")
    content = popoto.Field(type=str)
    relevance = DecayingSortedField(decay_rate=0.5)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.content,
    )


class LifecyclePlainMemory(popoto.Model):
    """Forgettable, but with no content fingerprint to record."""

    name = popoto.UniqueKeyField()
    tier = popoto.KeyField(type=str, default="episodic")
    content = popoto.Field(type=str)
    relevance = DecayingSortedField(decay_rate=0.5)


class TwoFilterMemory(WriteFilterMixin, popoto.Model):
    """Two ExistenceFilters, and the *unfingerprinted one is declared first*.

    Field order decides nothing about which filter carries content identity,
    and nothing documents or enforces an order — so a scan that stopped at the
    first ExistenceFilter it saw would resolve this model to "no identity" and
    silently disable the negative prior for it. That is a no-error, no-warning
    failure, which is why the ordering is pinned by a model rather than left to
    a reviewer to notice.
    """

    name = popoto.UniqueKeyField()
    content = popoto.Field(type=str)
    importance = popoto.FloatField(default=0.0)
    seen = ExistenceFilter(error_rate=0.01, capacity=1000)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=1000,
        fingerprint_fn=lambda inst: inst.content,
    )

    def compute_filter_score(self):
        return self.importance or 0.0


class LifecycleTwoFilterMemory(popoto.Model):
    """Same declaration order, on the burial side of the same contract."""

    name = popoto.UniqueKeyField()
    tier = popoto.KeyField(type=str, default="episodic")
    content = popoto.Field(type=str)
    relevance = DecayingSortedField(decay_rate=0.5)
    seen = ExistenceFilter(error_rate=0.01, capacity=1000)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.content,
    )


ALL_MODELS = (
    PriorMemory,
    PlainMemory,
    NoFingerprintFnMemory,
    LifecyclePriorMemory,
    LifecyclePlainMemory,
    TwoFilterMemory,
    LifecycleTwoFilterMemory,
)


@pytest.fixture(autouse=True)
def clean_prior_keyspace():
    """Drop every model's prior keyspace and priority set around each test.

    Model-scoped deletes rather than a flush: DB 0 is a live store on developer
    machines and this suite must never be the thing that reaches for FLUSHDB.
    """

    def _clear():
        client = popoto.get_redis()
        for model in ALL_MODELS:
            TombstonePriorStore(model).purge_all()
            client.delete(f"$WF:{model.__name__}:priority")
            # Drop the per-class auto-detect cache so a test that monkeypatches
            # a model's fields cannot leak a stale answer into the next test.
            if "_wf_fingerprint_field_cache" in model.__dict__:
                delattr(model, "_wf_fingerprint_field_cache")

    _clear()
    yield
    _clear()


def _bury(model, fingerprint, times=1, ts=None):
    """Record ``times`` burials of ``fingerprint`` and assert they landed.

    Asserting the count here is what keeps every downstream drawdown assertion
    non-vacuous: if the precondition silently failed to write, the test fails
    on this line rather than passing because the guard never ran.
    """
    store = TombstonePriorStore(model)
    for i in range(times):
        assert store.record_burial(fingerprint, ts if ts is not None else time.time())
    assert store.burial_count(fingerprint) == times
    return store


# --- digest / penalty units ------------------------------------------------


def test_digest_normalizes_case_and_whitespace():
    assert digest_fingerprint("  Hello World  ") == digest_fingerprint("hello world")


def test_digest_distinguishes_different_content():
    assert digest_fingerprint("alpha") != digest_fingerprint("beta")


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_digest_rejects_empty_fingerprints(value):
    """An empty fingerprint is not an identity — hashing it would give every
    content-less record the same digest and let them accumulate burials
    against each other."""
    assert digest_fingerprint(value) is None


def test_penalty_escalates_and_floors():
    assert penalty_for(0) == 1.0
    assert penalty_for(1) == pytest.approx(0.5)
    assert penalty_for(2) == pytest.approx(0.25)
    assert penalty_for(3) == pytest.approx(0.125)
    # 0.5 ** 10 == 0.0009765625, well under the 0.05 floor.
    assert penalty_for(10) == pytest.approx(Defaults.TOMBSTONE_PRIOR_FLOOR)


# --- end-to-end drawdown ---------------------------------------------------


def test_matching_write_is_drawn_down_after_one_burial():
    _bury(PriorMemory, "duplicate content")

    item = PriorMemory(name="e2e-1", content="duplicate content", importance=0.8)
    item.save()

    # 0.8 * 0.5 == 0.4, and the record still persists because 0.4 clears the
    # 0.1 min threshold. The drawdown is a weight, not a new rejection path.
    assert item._write_filter_score == pytest.approx(0.4)
    assert PriorMemory.query.get(name="e2e-1") is not None


def test_repeat_burials_escalate_suppression():
    expected = {1: 0.4, 2: 0.2, 3: 0.1}
    for burials, want in expected.items():
        TombstonePriorStore(PriorMemory).purge_all()
        _bury(PriorMemory, "escalating content", times=burials)

        item = PriorMemory(
            name=f"esc-{burials}", content="escalating content", importance=0.8
        )
        item.save()
        assert item._write_filter_score == pytest.approx(want), burials


def test_enough_burials_push_the_write_under_the_existing_gate():
    """The drawdown introduces no new rejection path — it feeds the existing
    WF_MIN_THRESHOLD comparison, which is what drops the write."""
    _bury(PriorMemory, "worthless chatter", times=6)

    item = PriorMemory(name="gated", content="worthless chatter", importance=0.5)
    item.save()

    # penalty floors at 0.05 -> 0.5 * 0.05 == 0.025 < 0.1 min threshold.
    assert item._write_filter_score == pytest.approx(0.025)
    assert PriorMemory.query.get(name="gated") is None


def test_drawdown_matches_across_case_and_whitespace_differences():
    _bury(PriorMemory, "Same Thing Said Again")

    item = PriorMemory(
        name="normalized", content="  same thing said again  ", importance=0.8
    )
    item.save()
    assert item._write_filter_score == pytest.approx(0.4)


# --- false-positive safety -------------------------------------------------


def test_dissimilar_record_is_not_penalized_at_all():
    """The characterization test for false-positive behavior. Matching is exact
    on a normalized fingerprint, so a record that merely shares words with a
    buried one is untouched — score unchanged AND no telemetry written."""
    store = _bury(PriorMemory, "the quick brown fox jumps")

    item = PriorMemory(
        name="dissimilar",
        content="the quick brown dog sleeps",
        importance=0.8,
    )
    item.save()

    assert item._write_filter_score == pytest.approx(0.8)
    assert store.stats()[STAT_PENALIZED] == 0
    assert store.stats()[STAT_DRAWDOWN_TOTAL] == pytest.approx(0.0)


def test_unburied_content_is_not_penalized():
    _bury(PriorMemory, "something else entirely")

    item = PriorMemory(name="fresh", content="brand new insight", importance=0.8)
    item.save()
    assert item._write_filter_score == pytest.approx(0.8)


# --- bounded storage -------------------------------------------------------


def test_prior_storage_is_bounded_and_evicts_the_oldest(monkeypatch):
    monkeypatch.setattr(Defaults, "TOMBSTONE_PRIOR_LIMIT", 10)
    store = TombstonePriorStore(PriorMemory)

    # Ascending timestamps so "oldest" is unambiguous.
    for i in range(25):
        assert store.record_burial(f"content-{i}", 1000.0 + i)

    assert store.count() == 10
    # The 15 oldest are gone; the 10 newest survive with their counts intact.
    for i in range(15):
        assert store.burial_count(f"content-{i}") == 0, i
    for i in range(15, 25):
        assert store.burial_count(f"content-{i}") == 1, i


def test_reburying_refreshes_recency_so_it_survives_the_sweep(monkeypatch):
    monkeypatch.setattr(Defaults, "TOMBSTONE_PRIOR_LIMIT", 5)
    store = TombstonePriorStore(PriorMemory)

    for i in range(5):
        assert store.record_burial(f"item-{i}", 1000.0 + i)
    # item-0 is the oldest; rebury it at the newest timestamp.
    assert store.record_burial("item-0", 2000.0)
    assert store.burial_count("item-0") == 2

    for i in range(5, 9):
        assert store.record_burial(f"item-{i}", 3000.0 + i)

    assert store.count() == 5
    assert store.burial_count("item-0") == 2
    assert store.burial_count("item-1") == 0


# --- telemetry -------------------------------------------------------------


def test_penalized_writes_are_counted_and_the_drawdown_is_summed():
    store = _bury(PriorMemory, "counted content")

    PriorMemory(name="t1", content="counted content", importance=0.8).save()
    PriorMemory(name="t2", content="counted content", importance=0.6).save()

    stats = store.stats()
    assert stats[STAT_PENALIZED] == 2
    # (0.8 - 0.4) + (0.6 - 0.3) == 0.7
    assert stats[STAT_DRAWDOWN_TOTAL] == pytest.approx(0.7)


def test_stats_reads_zeros_when_nothing_has_been_penalized():
    stats = TombstonePriorStore(PriorMemory).stats()
    assert stats == {STAT_PENALIZED: 0, STAT_DRAWDOWN_TOTAL: 0.0}


def test_drawdown_is_logged_at_debug(caplog):
    _bury(PriorMemory, "logged content")
    with caplog.at_level(logging.DEBUG, logger="POPOTO.WriteFilter"):
        PriorMemory(name="logged", content="logged content", importance=0.8).save()
    assert any("tombstone prior" in r.message for r in caplog.records)


# --- kill switch -----------------------------------------------------------


def test_kill_switch_disables_the_prior_after_import(monkeypatch):
    """The switch is read at call time, not bound at import, so a deploy-time
    flip is not a no-op. Set AFTER the module is imported and after a burial
    exists — the burial proves the drawdown would otherwise have fired."""
    store = _bury(PriorMemory, "switched content")

    monkeypatch.setenv("POPOTO_TOMBSTONE_PRIOR_DISABLE", "1")
    item = PriorMemory(name="switched", content="switched content", importance=0.8)
    item.save()

    assert item._write_filter_score == pytest.approx(0.8)
    assert store.stats()[STAT_PENALIZED] == 0


def test_prior_is_on_by_default_when_the_switch_is_unset(monkeypatch):
    monkeypatch.delenv("POPOTO_TOMBSTONE_PRIOR_DISABLE", raising=False)
    _bury(PriorMemory, "default on content")
    item = PriorMemory(name="default-on", content="default on content", importance=0.8)
    item.save()
    assert item._write_filter_score == pytest.approx(0.4)


# --- byte-identical behavior for non-adopters ------------------------------


def test_model_without_existence_filter_issues_no_extra_redis_commands(
    monkeypatch, caplog
):
    """The auto-detect short-circuits before any client call, which is what
    makes 'deployments not using tombstones see byte-identical write behavior'
    structurally true rather than flag-dependent.

    The score assertion alone would be VACUOUS: ``_apply_tombstone_prior``'s
    outer handler also returns the score unchanged, so a broken auto-detect
    that raised on every save would pass a score-only test while issuing work
    and losing the guarantee. (Proven: mutation M8.) Three assertions close
    that hole — the auto-detect must resolve to None, the store must never be
    touched, and *no warning may be logged*, which is the signal that
    distinguishes the intended short-circuit from the exception path.
    """
    calls = []
    monkeypatch.setattr(
        TombstonePriorStore,
        "burial_count",
        lambda self, fp: calls.append(fp) or 0,
    )
    monkeypatch.setattr(
        TombstonePriorStore,
        "note_penalty",
        lambda self, before, after: calls.append("penalty"),
    )

    assert PlainMemory._wf_fingerprint_field() is None

    with caplog.at_level(logging.WARNING, logger="POPOTO.WriteFilter"):
        item = PlainMemory(name="plain", content="anything", importance=0.8)
        item.save()

    assert calls == []
    assert [r.message for r in caplog.records] == []
    assert item._write_filter_score == pytest.approx(0.8)
    assert PlainMemory.query.get(name="plain") is not None


def test_existence_filter_without_fingerprint_fn_is_treated_as_no_identity(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        TombstonePriorStore,
        "burial_count",
        lambda self, fp: calls.append(fp) or 0,
    )
    item = NoFingerprintFnMemory(name="nofn", content="anything", importance=0.8)
    item.save()
    assert calls == []
    assert item._write_filter_score == pytest.approx(0.8)


def test_non_numeric_score_is_normalized_before_the_multiplier(monkeypatch):
    """Order is load-bearing: normalization runs first, so a non-numeric score
    is already 0.0 when the multiplier hits it and behavior is unchanged."""
    _bury(PriorMemory, "bad score content")
    monkeypatch.setattr(
        PriorMemory, "compute_filter_score", lambda self: "not a number"
    )
    item = PriorMemory(name="badscore", content="bad score content", importance=0.8)
    item.save()
    assert item._write_filter_score == 0.0
    assert PriorMemory.query.get(name="badscore") is None


# --- failure paths ---------------------------------------------------------


def test_consult_failure_admits_the_write_unchanged_and_warns(monkeypatch, caplog):
    def boom(self, fingerprint):
        raise RuntimeError("redis is on fire")

    monkeypatch.setattr(TombstonePriorStore, "burial_count", boom)

    with caplog.at_level(logging.WARNING, logger="POPOTO.WriteFilter"):
        item = PriorMemory(name="boom", content="anything", importance=0.8)
        item.save()

    assert item._write_filter_score == pytest.approx(0.8)
    assert PriorMemory.query.get(name="boom") is not None
    assert any("tombstone prior" in r.message for r in caplog.records)


def test_corrupt_burial_count_is_coerced_to_zero_with_a_warning(caplog):
    store = TombstonePriorStore(PriorMemory)
    burials_key, _, _ = store.keys()
    key = digest_fingerprint("corrupt content")
    popoto.get_redis().hset(burials_key, key, b"not-an-int")

    with caplog.at_level(logging.WARNING, logger="POPOTO.TombstonePrior"):
        assert store.burial_count("corrupt content") == 0

    assert any("not an integer" in r.message for r in caplog.records)


def test_burial_write_failure_is_reported_not_raised(monkeypatch, caplog):
    import src.popoto.fields.tombstone_prior as tp

    def boom(*args, **kwargs):
        raise RuntimeError("pipeline down")

    monkeypatch.setattr(tp, "_batch", boom)
    with caplog.at_level(logging.WARNING, logger="POPOTO.TombstonePrior"):
        assert TombstonePriorStore(PriorMemory).record_burial("x", 1.0) is False
    assert any("burial write failed" in r.message for r in caplog.records)


def test_record_burial_skips_an_empty_fingerprint():
    store = TombstonePriorStore(PriorMemory)
    assert store.record_burial("   ", 1.0) is False
    assert store.count() == 0


# --- burial recording through MemoryLifecycle ------------------------------


def test_tombstoning_records_a_burial_for_a_fingerprinted_model():
    """The end-to-end link between #491's forgetting and #494's prior."""
    record = LifecyclePriorMemory(name="doomed", content="doomed content")
    record.save()

    lifecycle = MemoryLifecycle(LifecyclePriorMemory, importance_field="relevance")
    assert lifecycle.tombstone(record) is not None

    store = TombstonePriorStore(LifecyclePriorMemory)
    assert store.burial_count("doomed content") == 1


def test_tombstoning_a_model_without_a_content_fingerprint_records_nothing():
    """A key-derived fingerprint could never match a future write, so recording
    it would only consume the retention bound."""
    record = LifecyclePlainMemory(name="plain-doomed", content="whatever")
    record.save()

    lifecycle = MemoryLifecycle(LifecyclePlainMemory, importance_field="relevance")
    assert lifecycle.tombstone(record) is not None

    assert TombstonePriorStore(LifecyclePlainMemory).count() == 0


def test_auto_detect_skips_past_an_unfingerprinted_filter_to_a_later_one():
    """Declaration order must not decide whether the capability engages.

    ``TwoFilterMemory`` declares an ExistenceFilter with no ``fingerprint_fn``
    *before* the one that has it. A scan that stopped at the first
    ExistenceFilter would resolve to None here and disable the negative prior
    for the whole model, with no error and no warning to say so. Asserting the
    resolved field is the fingerprinted one — not merely that the result is
    non-None — is what makes this fail on the stop-at-first shape rather than
    on any object being returned.
    """
    field = TwoFilterMemory._wf_fingerprint_field()

    assert field is not None
    assert field is TwoFilterMemory._meta.fields["bloom"]
    assert field.fingerprint_fn is not None

    _bury(TwoFilterMemory, "buried twice-filtered content")

    item = TwoFilterMemory(
        name="second-filter", content="buried twice-filtered content", importance=0.8
    )
    item.save()

    assert item._write_filter_score == pytest.approx(0.4)


def test_burial_side_also_skips_past_an_unfingerprinted_filter():
    """The write side and the burial side must agree on which filter carries
    identity, or a record gets penalized on a fingerprint it was never buried
    under — or, as here, never buried at all."""
    record = LifecycleTwoFilterMemory(name="doomed-2f", content="two-filter content")
    record.save()

    lifecycle = MemoryLifecycle(LifecycleTwoFilterMemory, importance_field="relevance")
    assert lifecycle.tombstone(record) is not None

    store = TombstonePriorStore(LifecycleTwoFilterMemory)
    assert store.burial_count("two-filter content") == 1


def test_a_failing_burial_does_not_roll_back_or_break_the_tombstone(
    monkeypatch, caplog
):
    record = LifecyclePriorMemory(name="survives", content="survives content")
    record.save()
    live_key = record.db_key.redis_key

    lifecycle = MemoryLifecycle(LifecyclePriorMemory, importance_field="relevance")
    monkeypatch.setattr(
        TombstonePriorStore,
        "record_burial",
        lambda self, fp, ts: (_ for _ in ()).throw(RuntimeError("nope")),
    )

    with caplog.at_level(logging.WARNING, logger="POPOTO.MemoryLifecycle"):
        tomb = lifecycle.tombstone(record)

    # The archive+removal completed before the burial was attempted, so the
    # tombstone is durable and returned regardless of what the prior did.
    assert tomb is not None
    assert lifecycle.get_tombstone(live_key) is not None
    assert any("negative-prior burial failed" in r.message for r in caplog.records)
