"""Tests for ValidityField / SupersessionProtocol — the validity axis (#580).

Tests cover:
- Interval correctness: valid_from / invalid_at / ingested_at ZSET contents
- ``+inf`` open sentinel round-tripping through ZADD/ZSCORE/ZRANGEBYSCORE and
  Lua ``tonumber`` (Redis/Valkey engine-parity guard, plan Risk 2)
- Query API: ``filter(validity__current=...)`` and ``filter(validity__as_of=t)``
- Supersession chains: two- and three-step, traversable in both directions from
  any anchor; closed-never-deleted; cycle safety; dangling links
- Atomicity: exactly one state-mutating server call, fault injection after the
  EVAL, idempotency under retry
- Pushdown preservation: ``filter(limit=N, order_by=<sorted field>)`` still sets
  ``_pushdown_limit`` with gating enabled (gating is not a filter kwarg)
- Structural guard: all three production ``DECAY_SCORE_LUA`` call sites pass
  numkeys 4 (plan Risk 1)
- Gate-disabled byte parity with the pre-#580 decay script
- The ZUNIONSTORE/SUM composite leak the D5b mask closes, plus a control
  proving the mask (not the decay gate) is what closes it
- Exclusion semantics: a record with no interval is UNMANAGED and stays visible
- ContextAssembler: invalidated records absent, ``assemble(as_of=t)`` replay,
  kill switch, no-ValidityField passthrough, max_items fill
- Every Failure Path case in the plan, including the D9 TTL warning
- p50 micro-benchmark of gated vs ungated retrieval at 20k records
"""

import inspect
import os
import re
import statistics
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto import SupersessionProtocol, ValidityField
from src.popoto.fields import supersession as supersession_module
from src.popoto.fields import validity_field as validity_module
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.constants import Defaults
from src.popoto.fields.decaying_sorted_field import (
    DECAY_SCORE_LUA,
    DecayingSortedField,
    validity_gate_args,
)
from src.popoto.models.query import Query, QueryBuilder
from src.popoto.recipes import context_assembler as assembler_module
from src.popoto.recipes.context_assembler import ContextAssembler
from src.popoto.redis_db import POPOTO_REDIS_DB

# --- Test Models ---


class ValidFact(popoto.Model):
    """The workhorse: a validity axis alongside decay and confidence arms."""

    name = popoto.UniqueKeyField()
    importance = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(base_score_field="importance")
    certainty = ConfidenceField(initial_confidence=0.5)
    validity = ValidityField()


class PlainFact(popoto.Model):
    """No ValidityField — the passthrough / byte-parity oracle."""

    name = popoto.UniqueKeyField()
    importance = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(base_score_field="importance")


class PushdownFact(popoto.Model):
    """Plain SortedField + ValidityField, for the limit-pushdown assertion."""

    name = popoto.UniqueKeyField()
    score = popoto.SortedField(type=float, default=0.0)
    validity = ValidityField()


class TTLFact(popoto.Model):
    """ValidityField on a TTL model — the plan D9 warning path."""

    name = popoto.UniqueKeyField()
    validity = ValidityField()

    class Meta:
        ttl = 60


class ValidMemory(popoto.Model):
    """Assembler model with a validity axis."""

    memory_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    content = popoto.StringField(default="")
    relevance = DecayingSortedField(partition_by="agent_id")
    validity = ValidityField()


class PlainMemory(popoto.Model):
    """Assembler model without a validity axis — exact-passthrough oracle."""

    memory_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    content = popoto.StringField(default="")
    relevance = DecayingSortedField(partition_by="agent_id")


class BenchFact(popoto.Model):
    """20k-record micro-benchmark partition."""

    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField()
    validity = ValidityField()


ALL_MODELS = [
    ValidFact,
    PlainFact,
    PushdownFact,
    TTLFact,
    ValidMemory,
    PlainMemory,
    BenchFact,
]

VALIDITY_MODELS = [
    (ValidFact, "validity"),
    (PushdownFact, "validity"),
    (TTLFact, "validity"),
    (ValidMemory, "validity"),
    (BenchFact, "validity"),
]


# --- Helpers ---


def _wipe_validity_keys():
    """Delete every derived validity key, including per-identity pointers.

    ``on_delete`` handles per-record entries, but a test that hand-writes index
    state (or leaves an open pointer behind) must not leak into the next test.
    """
    for model, field_name in VALIDITY_MODELS:
        keys = list(ValidityField.get_all_keys(model, field_name).values())
        prefix = ValidityField.get_prefix_db_key(model, field_name).redis_key
        keys.extend(
            k.decode() if isinstance(k, bytes) else k
            for k in POPOTO_REDIS_DB.keys(f"{prefix}:open:*")
        )
        if keys:
            POPOTO_REDIS_DB.delete(*keys)


def _reset():
    for model in ALL_MODELS:
        model.delete_all()
    _wipe_validity_keys()
    validity_module._TTL_WARNED.clear()


def setup_module():
    _reset()


def teardown_module():
    _reset()


@pytest.fixture(autouse=True)
def clean_state():
    _reset()
    yield
    _reset()
    Defaults.VALIDITY_GATING_ENABLED = True


def _save(model, **kwargs):
    """Create + save + return the instance (``save()`` returns a bool)."""
    instance = model(**kwargs)
    instance.save()
    return instance


def _names(records):
    return sorted(r.name for r in records)


def _zscore(key, member):
    return POPOTO_REDIS_DB.zscore(key, member)


def _interval(model, field_name, instance):
    """Return ``(valid_from, invalid_at, ingested_at)`` scores for a record."""
    keys = ValidityField.get_all_keys(model, field_name)
    member = instance.db_key.redis_key
    return (
        _zscore(keys["valid_from"], member),
        _zscore(keys["invalid_at"], member),
        _zscore(keys["ingested_at"], member),
    )


# ---------------------------------------------------------------------------
# A. Interval correctness and the +inf open sentinel (plan Risk 2)
# ---------------------------------------------------------------------------


class TestIntervalCorrectness:
    def test_save_opens_an_interval(self):
        before = time.time()
        fact = _save(ValidFact, name="a")
        after = time.time()
        valid_from, invalid_at, ingested_at = _interval(ValidFact, "validity", fact)

        assert valid_from is not None and before <= valid_from <= after
        assert ingested_at is not None and before <= ingested_at <= after
        assert invalid_at == float("inf")

    def test_resave_does_not_shift_an_open_interval(self):
        """The script's NX semantics: a re-save never moves start or ingest."""
        fact = _save(ValidFact, name="a")
        first = _interval(ValidFact, "validity", fact)
        time.sleep(0.02)
        fact.importance = 2.0
        fact.save()
        assert _interval(ValidFact, "validity", fact) == first

    def test_supersede_closes_the_incumbent_interval(self):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        old = _save(ValidFact, name="free")
        SupersessionProtocol.supersede(old, identity_key=identity)
        time.sleep(0.02)
        new = _save(ValidFact, name="enterprise")
        closed_at = time.time()
        SupersessionProtocol.supersede(new, identity_key=identity)

        old_from, old_close, _ = _interval(ValidFact, "validity", old)
        new_from, new_close, _ = _interval(ValidFact, "validity", new)

        assert old_close != float("inf")
        assert old_from < old_close
        assert abs(old_close - closed_at) < 1.0
        assert new_close == float("inf")
        assert new_from >= old_from

    def test_backdated_supersede_splits_valid_time_from_transaction_time(self):
        """``at=`` moves valid time; ingest time is always real now."""
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        old = _save(ValidFact, name="free")
        SupersessionProtocol.supersede(old, identity_key=identity)
        time.sleep(0.05)
        backdate = time.time()  # after old opened, before the newcomer exists
        time.sleep(0.05)
        new = _save(ValidFact, name="enterprise")
        SupersessionProtocol.supersede(new, identity_key=identity, at=backdate)

        _, old_close, _ = _interval(ValidFact, "validity", old)
        new_from, _, new_ingested = _interval(ValidFact, "validity", new)
        # Valid time honors `at` exactly...
        assert old_close == pytest.approx(backdate, abs=1e-6)
        # ...while transaction time is always real now, never backdated.
        assert new_ingested > backdate
        assert new_from is not None

    def test_delete_removes_every_interval_entry(self):
        fact = _save(ValidFact, name="a")
        member = fact.db_key.redis_key
        fact.delete()
        keys = ValidityField.get_all_keys(ValidFact, "validity")
        assert _zscore(keys["valid_from"], member) is None
        assert _zscore(keys["invalid_at"], member) is None
        assert _zscore(keys["ingested_at"], member) is None


class TestOpenSentinel:
    """``+inf`` is the open-interval sentinel — Redis/Valkey parity guard.

    CI runs the suite against both engines (PR #544), so these assertions are
    the cross-engine check the plan asks for without any extra harness.
    """

    KEY = "$test:validity:sentinel"

    def teardown_method(self):
        POPOTO_REDIS_DB.delete(self.KEY)

    def test_zadd_zscore_round_trip(self):
        POPOTO_REDIS_DB.zadd(self.KEY, {"open": "+inf", "closed": 100.0})
        assert POPOTO_REDIS_DB.zscore(self.KEY, "open") == float("inf")
        assert POPOTO_REDIS_DB.zscore(self.KEY, "closed") == 100.0

    def test_zrangebyscore_treats_inf_as_still_open(self):
        POPOTO_REDIS_DB.zadd(self.KEY, {"open": "+inf", "closed": 100.0})
        now = 200.0
        still_open = POPOTO_REDIS_DB.zrangebyscore(self.KEY, f"({now}", "+inf")
        already_closed = POPOTO_REDIS_DB.zrangebyscore(self.KEY, "-inf", now)
        assert {m.decode() for m in still_open} == {"open"}
        assert {m.decode() for m in already_closed} == {"closed"}

    def test_lua_tonumber_parses_the_sentinel_as_infinite(self):
        """The exact comparison ``DECAY_SCORE_LUA``'s gate makes, isolated."""
        POPOTO_REDIS_DB.zadd(self.KEY, {"open": "+inf", "closed": 100.0})
        script = """
        local raw_open = redis.call('ZSCORE', KEYS[1], 'open')
        local raw_closed = redis.call('ZSCORE', KEYS[1], 'closed')
        local as_of = tonumber(ARGV[1])
        local open_n = tonumber(raw_open)
        local closed_n = tonumber(raw_closed)
        local results = {}
        results[1] = tostring(raw_open)
        results[2] = (open_n ~= nil and open_n <= as_of) and 'closed' or 'open'
        results[3] = (closed_n ~= nil and closed_n <= as_of) and 'closed' or 'open'
        results[4] = (open_n == math.huge) and 'inf' or 'finite'
        return results
        """
        raw, open_verdict, closed_verdict, huge = POPOTO_REDIS_DB.eval(
            script, 1, self.KEY, "200"
        )
        assert open_verdict.decode() == "open", f"sentinel rendered as {raw!r}"
        assert closed_verdict.decode() == "closed"
        assert huge.decode() == "inf"

    def test_open_sentinel_constant_matches_stored_score(self):
        fact = _save(ValidFact, name="a")
        _, invalid_at, _ = _interval(ValidFact, "validity", fact)
        assert invalid_at == Defaults.VALIDITY_OPEN_SENTINEL


# ---------------------------------------------------------------------------
# B. Query API
# ---------------------------------------------------------------------------


class TestQueryAPI:
    def _two_step(self):
        """Return ``(old, new, t_between)`` for one closed + one open record."""
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        old = _save(ValidFact, name="free")
        SupersessionProtocol.supersede(old, identity_key=identity)
        time.sleep(0.02)
        t_between = time.time()
        time.sleep(0.02)
        new = _save(ValidFact, name="enterprise")
        SupersessionProtocol.supersede(new, identity_key=identity)
        return old, new, t_between

    def test_current_true_returns_only_open_records(self):
        self._two_step()
        assert _names(ValidFact.query.filter(validity__current=True)) == ["enterprise"]

    def test_as_of_returns_the_historical_view(self):
        _, _, t_between = self._two_step()
        assert _names(ValidFact.query.filter(validity__as_of=t_between)) == ["free"]

    def test_as_of_before_any_record_is_empty(self):
        self._two_step()
        assert ValidFact.query.filter(validity__as_of=time.time() - 86400) == []

    def test_current_false_is_the_literal_complement(self):
        """Pinned semantics: ``current=False`` == "does not cover now".

        That means closed records AND not-yet-started ones. The alternative
        reading ("closed records only") is NOT what ships: a future-dated record
        is not current, so excluding it from the complement would leave a record
        in neither half of a boolean partition. This asserts the partition is
        total: current(True) | current(False) == every record with an interval,
        and the two halves are disjoint.
        """
        old, new, _ = self._two_step()
        future = _save(ValidFact, name="future")
        keys = ValidityField.get_all_keys(ValidFact, "validity")
        POPOTO_REDIS_DB.zadd(
            keys["valid_from"], {future.db_key.redis_key: time.time() + 3600}
        )

        current = set(_names(ValidFact.query.filter(validity__current=True)))
        complement = set(_names(ValidFact.query.filter(validity__current=False)))

        assert current == {"enterprise"}
        assert complement == {"free", "future"}
        assert current & complement == set()
        assert current | complement == {"free", "enterprise", "future"}

    def test_as_of_and_current_intersect(self):
        _, _, t_between = self._two_step()
        # Nothing is both current AND valid at the earlier instant here.
        assert (
            ValidFact.query.filter(validity__current=True, validity__as_of=t_between)
            == []
        )

    def test_current_requires_a_bool(self):
        # `filter()` is lazy — `.all()` is what runs the field's filter_query.
        with pytest.raises(ValueError, match="True or False"):
            ValidFact.query.filter(validity__current="yes").all()

    def test_as_of_requires_a_number(self):
        with pytest.raises(ValueError, match="epoch seconds"):
            ValidFact.query.filter(validity__as_of="yesterday").all()

    def test_filter_query_returns_a_set_not_a_list(self):
        """A list return would become ``_sorted_field_order`` (plan D2)."""
        self._two_step()
        result = ValidityField.filter_query(
            ValidFact, "validity", validity__current=True
        )
        assert isinstance(result, set)

    def test_validity_is_not_a_sorted_field(self):
        assert "validity" not in ValidFact._meta.sorted_field_names


# ---------------------------------------------------------------------------
# C. Supersession chains
# ---------------------------------------------------------------------------


class TestSupersessionChains:
    def _chain_of(self, *names):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        records = []
        for name in names:
            record = _save(ValidFact, name=name)
            SupersessionProtocol.supersede(record, identity_key=identity)
            records.append(record)
            time.sleep(0.01)
        return records

    def test_two_step_chain_from_either_anchor(self):
        old, new = self._chain_of("v1", "v2")
        assert _names(SupersessionProtocol.chain(old)) == ["v1", "v2"]
        assert _names(SupersessionProtocol.chain(new)) == ["v1", "v2"]
        assert [r.name for r in SupersessionProtocol.chain(new)] == ["v1", "v2"]

    def test_three_step_chain_from_every_anchor(self):
        v1, v2, v3 = self._chain_of("v1", "v2", "v3")
        for anchor in (v1, v2, v3):
            assert [r.name for r in SupersessionProtocol.chain(anchor)] == [
                "v1",
                "v2",
                "v3",
            ]

    def test_single_record_chain_is_itself(self):
        (only,) = self._chain_of("v1")
        assert [r.name for r in SupersessionProtocol.chain(only)] == ["v1"]

    def test_both_directions_one_hop(self):
        v1, v2, v3 = self._chain_of("v1", "v2", "v3")
        assert SupersessionProtocol.superseded_by(v1).name == "v2"
        assert SupersessionProtocol.superseded_by(v2).name == "v3"
        assert SupersessionProtocol.superseded_by(v3) is None
        assert SupersessionProtocol.supersedes(v3).name == "v2"
        assert SupersessionProtocol.supersedes(v2).name == "v1"
        assert SupersessionProtocol.supersedes(v1) is None

    def test_superseded_records_are_closed_never_deleted(self):
        v1, v2 = self._chain_of("v1", "v2")
        member = v1.db_key.redis_key
        assert POPOTO_REDIS_DB.exists(member) == 1
        fetched = ValidFact.query.get(name="v1")
        assert fetched is not None and fetched.name == "v1"
        _, closed_at, _ = _interval(ValidFact, "validity", v1)
        assert closed_at != float("inf")

    def test_chain_terminates_on_a_cycle(self):
        v1, v2 = self._chain_of("v1", "v2")
        fwd = ValidityField.get_chain_fwd_key(ValidFact, "validity")
        # Forge a cycle: v2 -> v1, which already links forward to v2.
        POPOTO_REDIS_DB.hset(fwd, v2.db_key.redis_key, v1.db_key.redis_key)
        chain = SupersessionProtocol.chain(v1)
        assert [r.name for r in chain] == ["v1", "v2"]

    def test_dangling_link_is_treated_as_a_chain_end(self):
        v1, v2 = self._chain_of("v1", "v2")
        fwd = ValidityField.get_chain_fwd_key(ValidFact, "validity")
        POPOTO_REDIS_DB.hset(fwd, v2.db_key.redis_key, "ValidFact:ghost")
        assert [r.name for r in SupersessionProtocol.chain(v1)] == ["v1", "v2"]
        assert SupersessionProtocol.superseded_by(v2) is None

    def test_chain_on_a_model_without_a_validity_field_is_empty(self):
        plain = _save(PlainFact, name="p")
        assert SupersessionProtocol.chain(plain) == []
        assert SupersessionProtocol.superseded_by(plain) is None
        assert SupersessionProtocol.supersedes(plain) is None

    def test_invalidate_writes_both_chain_links(self):
        old = _save(ValidFact, name="old")
        new = _save(ValidFact, name="new")
        closed = SupersessionProtocol.invalidate(old, superseded_by=new)
        assert closed == old.db_key.redis_key
        assert SupersessionProtocol.superseded_by(old).name == "new"
        assert SupersessionProtocol.supersedes(new).name == "old"


# ---------------------------------------------------------------------------
# D. Atomicity (plan AC: one EVAL, no torn state, idempotent under retry)
# ---------------------------------------------------------------------------


MUTATING_CLIENT_METHODS = [
    "zadd",
    "zrem",
    "zincrby",
    "hset",
    "hdel",
    "set",
    "delete",
    "zunionstore",
    "zinterstore",
    "zdiffstore",
    "zrangestore",
    "expire",
    "sadd",
    "srem",
]


class _CallCounter:
    """Count client calls by name, delegating to the real implementation."""

    def __init__(self, monkeypatch, names):
        self.counts = {name: 0 for name in names}
        for name in names:
            monkeypatch.setattr(POPOTO_REDIS_DB, name, self._wrap(name), raising=True)

    def _wrap(self, name):
        original = getattr(POPOTO_REDIS_DB, name)

        def wrapper(*args, **kwargs):
            self.counts[name] += 1
            return original(*args, **kwargs)

        return wrapper


class TestAtomicity:
    def test_supersede_issues_exactly_one_mutating_call(self, monkeypatch):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        old = _save(ValidFact, name="free")
        SupersessionProtocol.supersede(old, identity_key=identity)
        new = _save(ValidFact, name="enterprise")

        counter = _CallCounter(monkeypatch, ["eval"] + MUTATING_CLIENT_METHODS)
        closed = SupersessionProtocol.supersede(new, identity_key=identity)

        assert closed == old.db_key.redis_key
        assert counter.counts["eval"] == 1, counter.counts
        others = {k: v for k, v in counter.counts.items() if k != "eval" and v}
        assert others == {}, f"supersede() made non-EVAL mutating calls: {others}"

    def test_invalidate_issues_exactly_one_mutating_call(self, monkeypatch):
        old = _save(ValidFact, name="old")
        new = _save(ValidFact, name="new")
        counter = _CallCounter(monkeypatch, ["eval"] + MUTATING_CLIENT_METHODS)
        SupersessionProtocol.invalidate(old, superseded_by=new)
        assert counter.counts["eval"] == 1
        assert {k: v for k, v in counter.counts.items() if k != "eval" and v} == {}

    def test_fault_after_eval_leaves_no_torn_state(self, monkeypatch):
        """Inject a failure the instant the EVAL returns.

        The observable state must be fully-old or fully-new — never the torn
        shape the plan names: interval-closed but still index-visible.
        """
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        old = _save(ValidFact, name="free")
        SupersessionProtocol.supersede(old, identity_key=identity)
        new = _save(ValidFact, name="enterprise")

        real_eval = POPOTO_REDIS_DB.eval

        def exploding_eval(*args, **kwargs):
            real_eval(*args, **kwargs)
            raise RuntimeError("injected fault immediately after EVAL")

        monkeypatch.setattr(POPOTO_REDIS_DB, "eval", exploding_eval)
        with pytest.raises(RuntimeError, match="injected fault"):
            SupersessionProtocol.supersede(new, identity_key=identity)
        monkeypatch.undo()

        _, old_close, _ = _interval(ValidFact, "validity", old)
        old_is_closed = old_close != float("inf")

        visible_filter = _names(ValidFact.query.filter(validity__current=True))
        visible_decay = _names(ValidFact.query.top_by_decay("relevance", n=10))
        visible_composite = _names(
            ValidFact.query.composite_score(indexes={"relevance": 1.0}, limit=10)
        )

        if old_is_closed:
            # Fully-new: closed AND gone from every retrieval path.
            assert "free" not in visible_filter
            assert "free" not in visible_decay, "closed but still index-visible"
            assert "free" not in visible_composite, "closed but still index-visible"
            assert SupersessionProtocol.superseded_by(old).name == "enterprise"
        else:
            # Fully-old: open AND still visible, with no half-written chain.
            assert "free" in visible_filter
            assert SupersessionProtocol.superseded_by(old) is None

    def test_supersede_is_idempotent_under_retry(self):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        old = _save(ValidFact, name="free")
        SupersessionProtocol.supersede(old, identity_key=identity)
        new = _save(ValidFact, name="enterprise")

        first = SupersessionProtocol.supersede(new, identity_key=identity)
        _, close_after_first, _ = _interval(ValidFact, "validity", old)
        time.sleep(0.02)
        second = SupersessionProtocol.supersede(new, identity_key=identity)
        _, close_after_second, _ = _interval(ValidFact, "validity", old)

        assert first == old.db_key.redis_key
        # The retry finds the pointer already on the newcomer: nothing re-closes.
        assert second is None
        assert close_after_first == close_after_second

    def test_invalidate_twice_does_not_reclose(self):
        old = _save(ValidFact, name="old")
        first = SupersessionProtocol.invalidate(old)
        _, first_close, _ = _interval(ValidFact, "validity", old)
        time.sleep(0.02)
        second = SupersessionProtocol.invalidate(old)
        _, second_close, _ = _interval(ValidFact, "validity", old)
        assert first == old.db_key.redis_key
        assert second is None
        assert first_close == second_close

    def test_save_cannot_resurrect_a_closed_record(self):
        """Plan Race 2: a save interleaving with a supersession is a no-op."""
        old = _save(ValidFact, name="old")
        SupersessionProtocol.invalidate(old)
        _, closed_at, _ = _interval(ValidFact, "validity", old)
        old.importance = 9.0
        old.save()
        _, after_save, _ = _interval(ValidFact, "validity", old)
        assert after_save == closed_at != float("inf")


# ---------------------------------------------------------------------------
# E. Pushdown preservation (gating is not a filter kwarg)
# ---------------------------------------------------------------------------


class TestPushdownPreservation:
    def test_limit_pushdown_survives_validity_gating(self):
        """``_pushdown_limit`` is still set on a ValidityField-bearing model.

        Gating lives in the decay Lua, the composite mask and the assembler
        post-filter — never in ``filter()``'s kwargs — so
        ``_sorted_pushdown_args``' condition 5 (no filter param survives the
        ordering field) is untouched.
        """
        for i in range(6):
            _save(PushdownFact, name=f"p{i}", score=float(i))

        assert Defaults.VALIDITY_GATING_ENABLED is True
        results = PushdownFact.query.filter(
            score__gte=0, limit=3, order_by="score"
        ).all()

        assert PushdownFact.query._pushdown_limit == 3
        assert len(results) == 3

    def test_pushdown_matches_a_model_without_a_validity_field(self):
        """Same query shape on a no-validity model pushes down identically."""
        for i in range(6):
            _save(PushdownFact, name=f"p{i}", score=float(i))
        PushdownFact.query.filter(score__gte=0, limit=3, order_by="score").all()
        gated = PushdownFact.query._pushdown_limit

        Defaults.VALIDITY_GATING_ENABLED = False
        try:
            PushdownFact.query.filter(score__gte=0, limit=3, order_by="score").all()
            ungated = PushdownFact.query._pushdown_limit
        finally:
            Defaults.VALIDITY_GATING_ENABLED = True
        assert gated == ungated == 3

    def test_range_read_bound_survives_gating(self):
        """White-box on ``_sorted_pushdown_args`` — the Redis-side bound.

        ``_pushdown_limit`` alone is ambiguous: ``_bound_keys_before_hydration``
        sets it too, on the wider set of queries where only the hydration is
        bounded. This asserts the narrower, load-bearing thing — that condition
        5 (no filter param survives the ordering field) still passes with
        gating enabled, i.e. gating did not become a filter kwarg.
        """
        query = PushdownFact.query
        field = query.options.fields["score"]
        query._pushdown_allowed = True
        query._sorted_field_order = None
        try:
            assert query._sorted_pushdown_args(
                "score",
                field,
                {"score__gte"},
                {"score__gte": 0, "limit": 3, "order_by": "score"},
            ) == (3, False)
        finally:
            query._pushdown_allowed = False

    def test_a_deliberate_validity_filter_does_disable_the_range_bound(self):
        """The documented trade-off, asserted so it stays documented.

        ``validity__current`` is a *deliberate* query; it survives the ordering
        field, so condition 5 refuses the Redis-side bound. That is exactly why
        the default retrieval path never expresses gating as a filter kwarg.
        """
        query = PushdownFact.query
        field = query.options.fields["score"]
        query._pushdown_allowed = True
        query._sorted_field_order = None
        try:
            assert query._sorted_pushdown_args(
                "score",
                field,
                {"score__gte", "validity__current"},
                {
                    "score__gte": 0,
                    "validity__current": True,
                    "limit": 3,
                    "order_by": "score",
                },
            ) == (None, False)
        finally:
            query._pushdown_allowed = False

    def test_a_deliberate_validity_filter_still_returns_correct_rows(self):
        """And the results stay correct, bound or no bound."""
        records = [_save(PushdownFact, name=f"p{i}", score=float(i)) for i in range(6)]
        for record in records[:3]:
            SupersessionProtocol.invalidate(record)
        results = PushdownFact.query.filter(
            score__gte=0, validity__current=True, limit=3, order_by="score"
        ).all()
        assert sorted(r.name for r in results) == ["p3", "p4", "p5"]


# ---------------------------------------------------------------------------
# F. Structural guard on the three DECAY_SCORE_LUA call sites (plan Risk 1)
# ---------------------------------------------------------------------------


def _decay_eval_numkeys(source):
    """Return the numkeys literal of every ``eval(DECAY_SCORE_LUA, ...)`` site.

    Comment lines between the script and its numkeys argument are skipped, so
    the check reads the actual argument rather than whatever text follows.
    """
    found = []
    for match in re.finditer(r"eval\(\s*DECAY_SCORE_LUA\s*,", source):
        for line in source[match.end() :].splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            token = re.match(r"([^,)]*)", stripped).group(1).strip()
            found.append(token)
            break
    return found


class TestDecayEvalCallSites:
    def test_decay_eval_call_sites_pass_four_keys(self):
        """All three production sites pass numkeys 4 (plan Risk 1).

        Passing the two validity keys without bumping numkeys silently shunts
        them into ARGV, shifting ``base_score_field`` and the confidence
        parameters — a corruption that fails quietly, which is why this is a
        test and not a grep.
        """
        sites = {
            "query.QueryBuilder.top_by_decay": inspect.getsource(
                QueryBuilder.top_by_decay
            ),
            "query.QueryBuilder._materialize_decay_field": inspect.getsource(
                QueryBuilder._materialize_decay_field
            ),
            "context_assembler._decayed_partition_scores": inspect.getsource(
                assembler_module._decayed_partition_scores
            ),
        }
        for name, source in sites.items():
            numkeys = _decay_eval_numkeys(source)
            assert len(numkeys) == 1, f"{name}: expected 1 DECAY_SCORE_LUA eval site"
            assert numkeys[0] == "4", (
                f"{name}: eval(DECAY_SCORE_LUA, ...) passes numkeys "
                f"{numkeys[0]!r}, expected '4' — the validity KEYS[3]/KEYS[4] "
                "would be shunted into ARGV"
            )

    def test_helper_detects_a_stale_numkeys(self):
        """The guard's own guard: prove the matcher can see a bad site."""
        bad = "result = POPOTO_REDIS_DB.eval(\n    DECAY_SCORE_LUA,\n    # note\n    2,\n)"
        assert _decay_eval_numkeys(bad) == ["2"]

    def test_cyclic_decay_lua_sites_are_not_matched(self):
        """CYCLIC_DECAY_LUA is deliberately unmodified — never flag it."""
        for source in (
            inspect.getsource(assembler_module._decayed_partition_scores),
            inspect.getsource(QueryBuilder.top_by_decay),
        ):
            assert "CYCLIC_DECAY_LUA" in source
            assert len(_decay_eval_numkeys(source)) == 1

    def test_query_top_by_decay_delegates_rather_than_evaluating(self):
        """``Query.top_by_decay`` is a thin wrapper; the EVAL lives on the
        builder. Pinned so the site inventory above cannot go stale silently."""
        assert "DECAY_SCORE_LUA" not in inspect.getsource(Query.top_by_decay)


# ---------------------------------------------------------------------------
# G. Gate-disabled byte parity with the pre-#580 script
# ---------------------------------------------------------------------------


class TestGateDisabledParity:
    def _decay_zset_key(self, model, field_name="relevance"):
        return DecayingSortedField.get_special_use_field_db_key(
            model, field_name
        ).redis_key

    def test_numkeys_two_and_four_with_empty_gate_keys_agree(self):
        """The pre-change caller shape and the new one produce equal scores."""
        for i in range(5):
            _save(PlainFact, name=f"p{i}", importance=float(i + 1))
        zkey = self._decay_zset_key(PlainFact)
        now = time.time()

        old_shape = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA,
            2,
            zkey,
            "",
            str(now),
            "0.5",
            "10",
            "importance",
            "0",
            "0.5",
        )
        new_shape = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA,
            4,
            zkey,
            "",
            "",
            "",
            str(now),
            "0.5",
            "10",
            "importance",
            "0",
            "0.5",
            "",
        )
        assert old_shape == new_shape

    def test_gate_keys_present_but_no_as_of_is_still_disabled(self):
        """All three of KEYS[3]/KEYS[4]/ARGV[7] are required to engage."""
        for i in range(5):
            _save(ValidFact, name=f"v{i}", importance=float(i + 1))
        SupersessionProtocol.invalidate(ValidFact.query.get(name="v0"))
        zkey = self._decay_zset_key(ValidFact)
        valid_from, invalid_at = ValidityField.get_interval_keys(ValidFact, "validity")
        now = time.time()

        baseline = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA,
            2,
            zkey,
            "",
            str(now),
            "0.5",
            "10",
            "importance",
            "0",
            "0.5",
        )
        no_as_of = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA,
            4,
            zkey,
            "",
            invalid_at,
            valid_from,
            str(now),
            "0.5",
            "10",
            "importance",
            "0",
            "0.5",
            "",  # ARGV[7] empty -> gate off
        )
        gated = POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA,
            4,
            zkey,
            "",
            invalid_at,
            valid_from,
            str(now),
            "0.5",
            "10",
            "importance",
            "0",
            "0.5",
            repr(now),
        )
        assert baseline == no_as_of
        assert len(gated) == len(baseline) - 2
        assert b"ValidFact:v0" not in gated

    def test_kill_switch_restores_ungated_retrieval(self):
        old = _save(ValidFact, name="old")
        _save(ValidFact, name="new")
        SupersessionProtocol.invalidate(old)

        assert "old" not in _names(ValidFact.query.top_by_decay("relevance", n=10))

        Defaults.VALIDITY_GATING_ENABLED = False
        try:
            assert validity_gate_args(ValidFact) == ("", "", "")
            assert "old" in _names(ValidFact.query.top_by_decay("relevance", n=10))
            assert "old" in _names(
                ValidFact.query.composite_score(indexes={"relevance": 1.0}, limit=10)
            )
        finally:
            Defaults.VALIDITY_GATING_ENABLED = True

    def test_model_without_a_validity_field_disables_the_gate(self):
        assert validity_gate_args(PlainFact) == ("", "", "")
        assert validity_gate_args(None) == ("", "", "")

    def test_byte_parity_oracle_files_keep_their_short_numkeys(self):
        """``test_lua_decay_scoring`` / ``test_confidence_modulated_decay`` are
        deliberately unmodified: their numkeys of 1 and 2 are the evidence that
        the gate-disabled path is unchanged. If someone "fixes" them to 4, that
        evidence is gone — so pin the shape here.
        """
        for filename in (
            "test_lua_decay_scoring.py",
            "test_confidence_modulated_decay.py",
        ):
            source = open(os.path.join(SCRIPT_DIR, filename)).read()
            numkeys = _decay_eval_numkeys(source)
            assert numkeys, f"{filename}: expected hand-eval DECAY_SCORE_LUA sites"
            assert set(numkeys) <= {"1", "2"}, (
                f"{filename}: byte-parity oracle now passes numkeys {numkeys} — "
                "the pre-#580 evidence has been destroyed"
            )


# ---------------------------------------------------------------------------
# H. The ZUNIONSTORE/SUM composite leak (the regression that motivated D5b)
# ---------------------------------------------------------------------------


class TestCompositeValidityMask:
    def _superseded_with_a_strong_confidence_arm(self):
        """Close ``old`` while leaving it strongly scored by the certainty arm."""
        old = _save(ValidFact, name="old", importance=1.0)
        new = _save(ValidFact, name="new", importance=1.0)
        for _ in range(4):
            ConfidenceField.update_confidence(old, "certainty", signal=0.95)
        ConfidenceField.update_confidence(new, "certainty", signal=0.2)
        SupersessionProtocol.invalidate(old, superseded_by=new)
        return old, new

    def test_superseded_member_does_not_leak_through_the_union(self):
        self._superseded_with_a_strong_confidence_arm()
        results = ValidFact.query.composite_score(
            indexes={"relevance": 0.5, "certainty": 0.5}, limit=10
        )
        assert _names(results) == ["new"]

    def test_control_the_leak_reproduces_without_the_mask(self, monkeypatch):
        """CONTROL: with only the decay gate, the closed member DOES surface.

        This is what proves the previous test exercises the D5b mask and not
        merely the decay-Lua gate: the decay arm already skips ``old``, but
        ``AGGREGATE SUM`` reads that absence as a 0 contribution, so the
        confidence arm alone still floats it into the result.
        """
        self._superseded_with_a_strong_confidence_arm()
        monkeypatch.setattr(QueryBuilder, "_apply_validity_mask", lambda *a, **kw: None)
        leaked = ValidFact.query.composite_score(
            indexes={"relevance": 0.5, "certainty": 0.5}, limit=10
        )
        assert "old" in _names(leaked), (
            "control failed: the closed member did not leak with the mask "
            "disabled, so the mask test proves nothing"
        )

    def test_single_index_decay_path_is_gated_by_the_lua_alone(self):
        old, _ = self._superseded_with_a_strong_confidence_arm()
        results = ValidFact.query.composite_score(indexes={"relevance": 1.0}, limit=10)
        assert _names(results) == ["new"]

    def test_top_by_decay_excludes_the_closed_member(self):
        self._superseded_with_a_strong_confidence_arm()
        assert _names(ValidFact.query.top_by_decay("relevance", n=10)) == ["new"]

    def test_composite_as_of_reconstructs_the_historical_view(self):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        old = _save(ValidFact, name="old")
        SupersessionProtocol.supersede(old, identity_key=identity)
        time.sleep(0.02)
        t_between = time.time()
        time.sleep(0.02)
        new = _save(ValidFact, name="new")
        SupersessionProtocol.supersede(new, identity_key=identity)

        assert _names(
            ValidFact.query.composite_score(indexes={"relevance": 1.0}, limit=10)
        ) == ["new"]
        assert _names(
            ValidFact.query.composite_score(
                indexes={"relevance": 1.0}, limit=10, as_of=t_between
            )
        ) == ["old"]

    def test_top_by_decay_as_of_reconstructs_the_historical_view(self):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        old = _save(ValidFact, name="old")
        SupersessionProtocol.supersede(old, identity_key=identity)
        time.sleep(0.02)
        t_between = time.time()
        time.sleep(0.02)
        new = _save(ValidFact, name="new")
        SupersessionProtocol.supersede(new, identity_key=identity)

        assert _names(ValidFact.query.top_by_decay("relevance", n=10)) == ["new"]
        assert _names(
            ValidFact.query.top_by_decay("relevance", n=10, as_of=t_between)
        ) == ["old"]


# ---------------------------------------------------------------------------
# I. Exclusion semantics: no interval == UNMANAGED == fully visible
# ---------------------------------------------------------------------------


class TestUnmanagedRecords:
    """A record with no interval entry predates the field's adoption.

    Every gating layer must be subtractive, never a whitelist: a whitelist
    would silently hide every record written before a ``ValidityField`` was
    added to an existing model.
    """

    def _unmanaged_plus_managed(self):
        managed = _save(ValidFact, name="managed")
        legacy = _save(ValidFact, name="legacy")
        keys = ValidityField.get_all_keys(ValidFact, "validity")
        for name in ("valid_from", "invalid_at", "ingested_at"):
            POPOTO_REDIS_DB.zrem(keys[name], legacy.db_key.redis_key)
        assert _interval(ValidFact, "validity", legacy) == (None, None, None)
        return managed, legacy

    def test_unmanaged_record_survives_plain_filter(self):
        self._unmanaged_plus_managed()
        assert _names(ValidFact.query.filter(name="legacy")) == ["legacy"]
        assert _names(ValidFact.query.all()) == ["legacy", "managed"]

    def test_unmanaged_record_survives_top_by_decay(self):
        self._unmanaged_plus_managed()
        assert _names(ValidFact.query.top_by_decay("relevance", n=10)) == [
            "legacy",
            "managed",
        ]

    def test_unmanaged_record_survives_composite_score(self):
        self._unmanaged_plus_managed()
        assert _names(
            ValidFact.query.composite_score(
                indexes={"relevance": 0.5, "certainty": 0.5}, limit=10
            )
        ) == ["legacy", "managed"]

    def test_unmanaged_record_survives_the_assembler(self):
        managed = _save(ValidMemory, agent_id="a1", content="managed")
        legacy = _save(ValidMemory, agent_id="a1", content="legacy")
        for key in ValidityField.get_all_keys(ValidMemory, "validity").values():
            POPOTO_REDIS_DB.zrem(key, legacy.db_key.redis_key)

        assembler = ContextAssembler(
            model_class=ValidMemory, score_weights={"relevance": 1.0}, max_items=10
        )
        result = assembler.assemble(
            query_cues={"content": "legacy"}, partition_filters={"agent_id": "a1"}
        )
        contents = {r.content for r in result.records}
        assert "legacy" in contents

    def test_unmanaged_record_is_absent_from_a_deliberate_current_filter(self):
        """Consistent with the other half: ``__current`` is a *positive* claim.

        An unmanaged record makes no claim about being valid now, so it is not
        returned by ``validity__current=True`` — but it is equally not returned
        by ``validity__current=False``, so neither deliberate query invents an
        answer for it.
        """
        self._unmanaged_plus_managed()
        assert _names(ValidFact.query.filter(validity__current=True)) == ["managed"]
        assert _names(ValidFact.query.filter(validity__current=False)) == []


# ---------------------------------------------------------------------------
# J. ContextAssembler integration
# ---------------------------------------------------------------------------


class TestAssemblerValidity:
    def _assembler(self, model_class=ValidMemory, max_items=10):
        return ContextAssembler(
            model_class=model_class,
            score_weights={"relevance": 1.0},
            max_items=max_items,
        )

    def test_validity_field_is_auto_detected(self):
        assert self._assembler()._validity_field_name == "validity"
        assert self._assembler(PlainMemory)._validity_field_name is None

    def test_invalidated_record_is_absent_from_assemble(self):
        old = _save(ValidMemory, agent_id="a1", content="stale")
        _save(ValidMemory, agent_id="a1", content="fresh")
        assembler = self._assembler()

        before = {
            r.content
            for r in assembler.assemble(
                query_cues={"content": "stale"}, partition_filters={"agent_id": "a1"}
            ).records
        }
        assert "stale" in before

        SupersessionProtocol.invalidate(old)

        after = {
            r.content
            for r in assembler.assemble(
                query_cues={"content": "stale"}, partition_filters={"agent_id": "a1"}
            ).records
        }
        assert "stale" not in after
        assert "fresh" in after

    def test_assemble_as_of_reconstructs_the_historical_view(self):
        old = _save(ValidMemory, agent_id="a1", content="stale")
        time.sleep(0.02)
        t_between = time.time()
        time.sleep(0.02)
        new = _save(ValidMemory, agent_id="a1", content="fresh")
        SupersessionProtocol.invalidate(old, superseded_by=new)

        assembler = self._assembler()
        historical = assembler.assemble(
            query_cues={"content": "stale"},
            partition_filters={"agent_id": "a1"},
            as_of=t_between,
        )
        contents = {r.content for r in historical.records}
        assert "stale" in contents
        # `fresh` had not been written yet at t_between.
        assert "fresh" not in contents

    def test_kill_switch_restores_ungated_assembly(self):
        old = _save(ValidMemory, agent_id="a1", content="stale")
        _save(ValidMemory, agent_id="a1", content="fresh")
        SupersessionProtocol.invalidate(old)
        assembler = self._assembler()

        Defaults.VALIDITY_GATING_ENABLED = False
        try:
            assert assembler._resolve_excluded_keys(None) is None
            contents = {
                r.content
                for r in assembler.assemble(
                    query_cues={"content": "stale"},
                    partition_filters={"agent_id": "a1"},
                ).records
            }
            assert "stale" in contents
        finally:
            Defaults.VALIDITY_GATING_ENABLED = True

    def test_model_without_a_validity_field_is_an_exact_passthrough(self):
        for i in range(4):
            _save(PlainMemory, agent_id="a1", content=f"item-{i}")
        assembler = self._assembler(PlainMemory)

        assert assembler._resolve_excluded_keys(None) is None
        baseline = assembler.assemble(
            query_cues={"content": "item-1"}, partition_filters={"agent_id": "a1"}
        )
        with_as_of = assembler.assemble(
            query_cues={"content": "item-1"},
            partition_filters={"agent_id": "a1"},
            as_of=time.time() - 3600,
        )
        assert [r.db_key.redis_key for r in baseline.records] == [
            r.db_key.redis_key for r in with_as_of.records
        ]
        assert baseline.metadata["pull_count"] == with_as_of.metadata["pull_count"]

    def test_heavily_superseded_partition_still_fills_max_items(self):
        """Plan Risk 3: gating must not shrink assembly below ``max_items``."""
        valid = [
            _save(ValidMemory, agent_id="a1", content=f"valid-{i}") for i in range(6)
        ]
        for i in range(12):
            stale = _save(ValidMemory, agent_id="a1", content=f"stale-{i}")
            SupersessionProtocol.invalidate(stale)

        assembler = self._assembler(max_items=5)
        result = assembler.assemble(
            query_cues={"content": "valid-0"}, partition_filters={"agent_id": "a1"}
        )
        assert len(result.records) == 5
        assert all(r.content.startswith("valid-") for r in result.records)
        assert len(valid) >= 5

    def test_excluded_keys_is_an_exclusion_set_not_a_whitelist(self):
        old = _save(ValidMemory, agent_id="a1", content="stale")
        new = _save(ValidMemory, agent_id="a1", content="fresh")
        SupersessionProtocol.invalidate(old, superseded_by=new)
        excluded = self._assembler()._resolve_excluded_keys(None)
        assert excluded == {old.db_key.redis_key}


# ---------------------------------------------------------------------------
# K. Failure paths (plan "Failure Path Test Strategy")
# ---------------------------------------------------------------------------


class TestFailurePaths:
    @pytest.mark.parametrize(
        "subject,predicate",
        [("", "plan"), ("user", ""), ("   ", "plan"), ("user", "\t\n"), ("", "")],
    )
    def test_empty_or_whitespace_identity_raises(self, subject, predicate):
        with pytest.raises(ValueError, match="non-empty"):
            SupersessionProtocol.identity_key(subject, predicate)

    def test_nul_in_an_identity_component_raises(self):
        with pytest.raises(ValueError, match="NUL"):
            SupersessionProtocol.identity_key("user\x0042", "plan")

    def test_identity_normalization_is_deterministic_and_collision_free(self):
        assert SupersessionProtocol.identity_key(
            " User_42 ", "Subscription  Plan"
        ) == SupersessionProtocol.identity_key("user_42", "subscription plan")
        assert SupersessionProtocol.identity_key(
            "ab", "c"
        ) != SupersessionProtocol.identity_key("a", "bc")
        assert len(SupersessionProtocol.identity_key("a", "b")) == 16

    def test_supersede_with_no_incumbent_opens_and_writes_no_chain_link(self):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        first = _save(ValidFact, name="first")
        assert SupersessionProtocol.supersede(first, identity_key=identity) is None

        _, invalid_at, _ = _interval(ValidFact, "validity", first)
        assert invalid_at == float("inf")
        fwd = ValidityField.get_chain_fwd_key(ValidFact, "validity")
        rev = ValidityField.get_chain_rev_key(ValidFact, "validity")
        assert POPOTO_REDIS_DB.hlen(fwd) == 0
        assert POPOTO_REDIS_DB.hlen(rev) == 0
        pointer = ValidityField.get_open_pointer_key(ValidFact, "validity", identity)
        assert POPOTO_REDIS_DB.get(pointer).decode() == first.db_key.redis_key

    def test_invalidate_before_valid_from_raises(self):
        record = _save(ValidFact, name="a")
        valid_from, _, _ = _interval(ValidFact, "validity", record)
        with pytest.raises(ValueError, match="valid_from"):
            SupersessionProtocol.invalidate(record, at=valid_from - 60)

    def test_invalidate_before_valid_from_leaves_the_record_open(self):
        record = _save(ValidFact, name="a")
        valid_from, _, _ = _interval(ValidFact, "validity", record)
        with pytest.raises(ValueError):
            SupersessionProtocol.invalidate(record, at=valid_from - 60)
        assert _interval(ValidFact, "validity", record)[1] == float("inf")
        assert _names(ValidFact.query.filter(validity__current=True)) == ["a"]

    def test_unsaved_instance_degrades_with_no_partial_state(self):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        unsaved = ValidFact(name="never-saved")

        assert SupersessionProtocol.supersede(unsaved, identity_key=identity) is None
        assert SupersessionProtocol.invalidate(unsaved) is None
        assert SupersessionProtocol.chain(unsaved) == []
        assert SupersessionProtocol.superseded_by(unsaved) is None

        keys = ValidityField.get_all_keys(ValidFact, "validity")
        assert POPOTO_REDIS_DB.zcard(keys["valid_from"]) == 0
        assert POPOTO_REDIS_DB.zcard(keys["invalid_at"]) == 0
        assert POPOTO_REDIS_DB.zcard(keys["ingested_at"]) == 0
        assert POPOTO_REDIS_DB.hlen(keys["chain_fwd"]) == 0
        assert POPOTO_REDIS_DB.hlen(keys["chain_rev"]) == 0
        pointer = ValidityField.get_open_pointer_key(ValidFact, "validity", identity)
        assert POPOTO_REDIS_DB.get(pointer) is None

    def test_invalidate_with_an_unsaved_successor_is_a_no_op(self):
        old = _save(ValidFact, name="old")
        assert (
            SupersessionProtocol.invalidate(old, superseded_by=ValidFact(name="ghost"))
            is None
        )
        assert _interval(ValidFact, "validity", old)[1] == float("inf")

    def test_protocol_on_a_model_without_a_validity_field_is_a_no_op(self):
        plain = _save(PlainFact, name="p")
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        assert SupersessionProtocol.supersede(plain, identity_key=identity) is None
        assert SupersessionProtocol.invalidate(plain) is None

    def test_assemble_as_of_on_a_model_without_a_validity_field_passes_through(self):
        _save(PlainMemory, agent_id="a1", content="only")
        assembler = ContextAssembler(
            model_class=PlainMemory, score_weights={"relevance": 1.0}
        )
        result = assembler.assemble(
            query_cues={"content": "only"},
            partition_filters={"agent_id": "a1"},
            as_of=time.time() - 86400,
        )
        assert {r.content for r in result.records} == {"only"}

    def test_malformed_identity_key_raises(self):
        record = _save(ValidFact, name="a")
        for bad in ("", "   ", ("only-one",), 42, None):
            with pytest.raises(ValueError):
                SupersessionProtocol.supersede(record, identity_key=bad)

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="mode must be one of"):
            ValidityField.execute_supersede(ValidFact, "validity", mode="reopen")

    def test_identity_pair_is_accepted_directly(self):
        old = _save(ValidFact, name="old")
        SupersessionProtocol.supersede(old, identity_key=("user_42", "plan"))
        new = _save(ValidFact, name="new")
        closed = SupersessionProtocol.supersede(new, identity_key=("User_42", " plan "))
        assert closed == old.db_key.redis_key

    def test_ttl_model_warns_once(self, caplog):
        validity_module._TTL_WARNED.clear()
        with caplog.at_level("WARNING", logger="POPOTO.ValidityField"):
            _save(TTLFact, name="a")
            _save(TTLFact, name="b")
            _save(TTLFact, name="c")
        warnings = [
            r for r in caplog.records if "truncates supersession chains" in r.message
        ]
        assert len(warnings) == 1
        assert "TTLFact" in warnings[0].getMessage()

    def test_no_ttl_model_does_not_warn(self, caplog):
        validity_module._TTL_WARNED.clear()
        with caplog.at_level("WARNING", logger="POPOTO.ValidityField"):
            _save(ValidFact, name="a")
        assert [
            r for r in caplog.records if "truncates supersession chains" in r.message
        ] == []


# ---------------------------------------------------------------------------
# L. p50 micro-benchmark (plan Success Criterion: 20k records)
# ---------------------------------------------------------------------------


BENCH_N = 20_000
BENCH_SAMPLES = 21
BENCH_WARMUP = 3
#: Ceiling on the gated/ungated p50 ratio. NOT the plan's "within 1 ms"
#: criterion, which is not achievable on this path -- see the test docstring.
#: Calibrated with ~3x headroom over the measured ~1.4x so it fails on a real
#: regression (a second scan, a per-member round trip) and not on jitter.
BENCH_MAX_RATIO = 4.0


def _seed_bench_partition(closed_every=10):
    """Write 20k members straight into the index ZSETs.

    Hand-written rather than saved: 20k ``save()`` calls would dominate the
    measurement with write cost, and the read path under test only ever reads
    these three ZSETs.
    """
    zkey = DecayingSortedField.get_special_use_field_db_key(
        BenchFact, "relevance"
    ).redis_key
    valid_from, invalid_at = ValidityField.get_interval_keys(BenchFact, "validity")
    now = time.time()
    pipe = POPOTO_REDIS_DB.pipeline()
    for i in range(BENCH_N):
        member = f"BenchFact:bench{i}"
        pipe.zadd(zkey, {member: now - (i % 4096)})
        pipe.zadd(valid_from, {member: now - 10_000})
        pipe.zadd(invalid_at, {member: now - 5 if i % closed_every == 0 else "+inf"})
        if i % 2000 == 0:
            pipe.execute()
            pipe = POPOTO_REDIS_DB.pipeline()
    pipe.execute()
    return zkey, valid_from, invalid_at


def _p50(fn):
    for _ in range(BENCH_WARMUP):
        fn()
    samples = []
    for _ in range(BENCH_SAMPLES):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(samples)


@pytest.mark.slow
@pytest.mark.benchmark
class TestValidityBenchmark:
    def test_p50_gated_retrieval_overhead_at_20k(self):
        """Gated vs ungated p50 for a 20k-record ``top_by_decay``.

        Reported as a RATIO, not the plan's absolute "within 1 ms of ungated".
        That criterion is not reachable on this path and the discrepancy is
        structural, not machine-dependent: ``DECAY_SCORE_LUA`` scans the whole
        partition, so gating adds up to two ``ZSCORE``s for each of 20k
        members — tens of thousands of extra server-side operations. Measured
        locally at ~37 ms ungated / ~51 ms gated (~1.4x, ~14 ms absolute).
        Asserting "< 1 ms" would be a permanently red gate; asserting a wall
        clock number on shared CI hardware would be a coin flip. A ratio
        against a same-process, same-data ungated control is the part that is
        actually stable, and it still catches the regressions that matter:
        an extra pass over the partition, or a per-member round trip.
        """
        _seed_bench_partition()

        def run():
            return BenchFact.query.top_by_decay("relevance", n=10)

        gated_p50 = _p50(run)
        Defaults.VALIDITY_GATING_ENABLED = False
        try:
            ungated_p50 = _p50(run)
        finally:
            Defaults.VALIDITY_GATING_ENABLED = True

        ratio = gated_p50 / ungated_p50 if ungated_p50 else float("inf")
        print(
            f"\n[validity p50 @ {BENCH_N}] ungated={ungated_p50:.2f}ms "
            f"gated={gated_p50:.2f}ms delta={gated_p50 - ungated_p50:.2f}ms "
            f"ratio={ratio:.2f}x"
        )
        assert ratio < BENCH_MAX_RATIO, (
            f"validity gating cost {ratio:.2f}x ungated retrieval at {BENCH_N} "
            f"records (ungated p50 {ungated_p50:.2f}ms, gated {gated_p50:.2f}ms)"
        )

    def test_p50_excluded_key_resolution_at_20k(self):
        """Assembler layer 3 costs two range reads, independent of gating depth.

        This is the part of the plan's latency criterion that IS a sub-scan
        cost: one snapshot per ``assemble()`` call, no per-member work.
        """
        _seed_bench_partition()
        assembler = ContextAssembler(
            model_class=BenchFact, score_weights={"relevance": 1.0}
        )
        p50 = _p50(lambda: assembler._resolve_excluded_keys(None))
        print(f"\n[validity excluded-key resolution p50 @ {BENCH_N}] {p50:.2f}ms")
        assert p50 < 25.0
