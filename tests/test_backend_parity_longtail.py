"""The long-tail fields on both conformance legs (#759 M5).

The field test files plant much of their state through the raw Redis client;
``test_cyclic_decay_field.py`` now does it through leg-neutral helpers. This
file states the behaviours the M5 port had to get exactly right, once, with
the same helpers, so every assertion runs on Redis and on Postgres from the
same code:

* ``CYCLIC_DECAY_LUA`` against a Python transcription of the script, and its
  missing validity gate (the backend twin of the ``rank_decayed(zset_key)``
  tests, which are refused off Redis);
* the ``CYCLES_MERGE_LUA`` reset line, word for word;
* ``TD_UPDATE_LUA``'s ``tostring`` -- the reply and the stored value;
* ``RESOLVE_PREDICTION_LUA``'s ``cmsgpack`` re-pack of the entry (an
  integral number comes back an ``int``, an empty map a list, a ``nil``
  field dropped), the ``bin`` payload that refuses the resolution, and
  ``ZREVRANGE``'s rank arithmetic in ``get_highest_errors``;
* ``export_state`` / ``import_state`` for the cycles and the ledger.

Where the legs legitimately differ, the test pins both behaviours
(``backend_is_redis``); each such row is in docs/features/postgres-backend.md.
"""

import logging
import math
import time
from decimal import Decimal

import msgpack
import pytest
import redis

import popoto
from popoto import ConfidenceField, CyclicDecayField, ObservationProtocol
from popoto.backends import RecordId
from popoto.backends.routing import non_redis_backend
from popoto.fields.prediction_ledger import PredictionLedgerMixin
from popoto.fields.td_value_field import TDValueField

pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]

DAY = 86400.0


class LtRhythm(popoto.Model):
    name = popoto.UniqueKeyField()
    weight = popoto.FloatField(default=1.0)
    relevance = CyclicDecayField(
        decay_rate=0.5,
        base_score_field="weight",
        cycles=[(86400, 2.0, 0)],
        pressure_rate=0.1,
    )


class LtValue(popoto.Model):
    name = popoto.KeyField()
    q_value = popoto.TDValueField(default=Decimal("0"))


class LtLedger(PredictionLedgerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    certainty = ConfidenceField()


class LtValid(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(cycles=[(86400, 1.0, 0)])
    validity = popoto.ValidityField()


class LtTtl(PredictionLedgerMixin, popoto.Model):
    """Every long-tail field on a ``Meta.ttl`` model (#783's read filter)."""

    name = popoto.KeyField()
    relevance = CyclicDecayField(cycles=[(86400, 2.0, 0)], pressure_rate=0.1)
    q_value = popoto.TDValueField(null=True)
    certainty = ConfidenceField()

    class Meta:
        ttl = 3600


#: A short-lived record's TTL, and how long a test waits past it.
SHORT = 1
PAST = 1.6


@pytest.fixture(autouse=True)
def _clean():
    def wipe():
        for model in (LtRhythm, LtValue, LtLedger, LtValid, LtTtl):
            model.delete_all()
        client = popoto.get_redis()
        for pattern in ("$PL:LtLedger*", "$PL:LtTtl*", "*LtTtl*"):
            for key in client.scan_iter(match=pattern):
                client.delete(key)

    wipe()
    yield
    wipe()


# -- leg-neutral helpers ---------------------------------------------------------


def _rid(record):
    return RecordId.from_key(record._meta.model_name, record.db_key.redis_key)


def _backdate(record, ts):
    backend = non_redis_backend(record)
    if backend is None:
        field = record._meta.fields["relevance"]
        zkey = field.get_partitioned_sortedset_db_key(record, "relevance").redis_key
        popoto.get_redis().zadd(zkey, {record.db_key.redis_key: ts})
        return
    table = backend._table(record._meta.spec).qualified
    backend._run(
        f'UPDATE {table} SET "relevance" = %s WHERE "_pk" = %s',
        [ts, record.db_key.redis_key],
        write=True,
    )


def _plant(record, cycles, pressure):
    """Raw cycles and pressure entries (``None`` leaves that half)."""
    backend = non_redis_backend(record)
    if backend is None:
        field = record._meta.fields["relevance"]
        client = popoto.get_redis()
        if cycles is not None:
            client.hset(
                field.get_cycles_hash_key(record, "relevance"),
                record.db_key.redis_key,
                msgpack.packb(cycles),
            )
        if pressure is not None:
            client.hset(
                field.get_pressure_hash_key(record, "relevance"),
                record.db_key.redis_key,
                msgpack.packb(pressure),
            )
        return
    backend.field_call(
        record._meta.spec, "relevance", "import", _rid(record), cycles, pressure
    )


def _ranked(model, now, n=10):
    """``[(key, score)]`` of the cyclic ranking at ``now``: the field's script
    on Redis, the backend's SQL on Postgres (both ``%.14g``)."""
    field = model._meta.fields["relevance"]
    backend = non_redis_backend(model)
    if backend is None:
        zkey = field.get_sortedset_db_key(model, "relevance").redis_key
        raw = field.rank_decayed(zkey, now=now, n=n)
        return [(raw[i].decode(), float(raw[i + 1])) for i in range(0, len(raw), 2)]
    return [
        (rid.canonical, score)
        for rid, score in backend.rank_decayed(
            model._meta.spec,
            "relevance",
            now=now,
            n=n,
            base_score_field=field.base_score_field,
        )
    ]


def _lua_score(now, ts, base, rate, cycles, pressure):
    """``CYCLIC_DECAY_LUA``, transcribed (no modulation), through ``%.14g``."""
    elapsed = max((now - ts) / DAY, 0.01)
    decayed = base * math.pow(elapsed, -rate)
    cyclic = 0
    for period, amp, *rest in cycles or []:
        phase = rest[0] if rest else 0
        if period > 0:
            cyclic = cyclic + amp * math.cos(2 * math.pi * (now - phase) / period)
    pressure_term = 0
    if pressure and pressure["rate"] > 0:
        pressure_term = pressure["rate"] * max(
            (now - pressure["last_resolved"]) / DAY, 0
        )
    return float("%.14g" % (decayed + cyclic + pressure_term))


# -- CYCLIC_DECAY_LUA ----------------------------------------------------------------


@pytest.mark.parametrize(
    "age_days, weight, cycles, pressure",
    [
        (1.0, 1.0, [[86400, 2.0, 0]], None),
        (10.0, 3.5, [[604800, 5.0, 12345.0], [86400, 1.25, 3.0]], None),
        (0.001, 1.0, [], {"rate": 0.1, "last_resolved": -30 * DAY}),
        (400.0, -2.0, [[31536000, 7.5, 1e9]], {"rate": 0.2, "last_resolved": 5 * DAY}),
        (3.0, 0.0, [[0, 9.0, 0], [-5, 9.0, 0], [86400, 0.0, 0]], None),
        (
            2.0,
            1.0,
            [[1e-3, 1.0, 0.0], [1e12, 2.0, -1e12]],
            {"rate": 0.0, "last_resolved": 0},
        ),
    ],
)
def test_the_score_matches_the_script_transcription(age_days, weight, cycles, pressure):
    now = 1_790_000_000.0
    record = LtRhythm.create(name="r", weight=weight)
    _backdate(record, now - age_days * DAY)
    stored_pressure = None
    if pressure is not None:
        stored_pressure = {
            "rate": pressure["rate"],
            "last_resolved": now + pressure["last_resolved"],
        }
    _plant(record, cycles, stored_pressure)
    expected = _lua_score(
        now, now - age_days * DAY, weight, 0.5, cycles, stored_pressure
    )
    assert _ranked(LtRhythm, now) == [(record.db_key.redis_key, expected)]


def test_overflowing_terms_saturate_on_both_legs():
    """An amplitude of 1.7e308 on two cycles overflows the sum to ``inf``:
    C saturates; Postgres would raise "value out of range" without the
    clamped fold."""
    now = 1_790_000_000.0
    record = LtRhythm.create(name="big")
    _backdate(record, now - DAY)
    _plant(record, [[86400, 1.7e308, now], [86400, 1.7e308, now]], None)
    assert _ranked(LtRhythm, now) == [(record.db_key.redis_key, math.inf)]


def test_the_cyclic_ranking_has_no_validity_gate():
    """``CYCLIC_DECAY_LUA`` carries no gate (``TestCyclicDecayGatingGap``): a
    closed record still ranks, on both legs, through top_by_decay."""
    from popoto.fields.supersession import SupersessionProtocol

    old = LtValid.create(name="old")
    new = LtValid.create(name="new")
    SupersessionProtocol.invalidate(old, superseded_by=new)
    ranked = LtValid.query.top_by_decay("relevance", n=10)
    assert sorted(r.name for r in ranked) == ["new", "old"]


# -- CYCLES_MERGE_LUA ------------------------------------------------------------------


def test_the_reset_line_is_the_same_on_both_legs(caplog):
    record = LtRhythm.create(name="reset")
    record.strengthen_cycle("relevance", factor=4.0)
    field = LtRhythm._meta.fields["relevance"]
    original = field.cycles
    try:
        field.cycles = [(86400, 3.0, 0)]
        with caplog.at_level("INFO", logger="POPOTO.CyclicDecayField"):
            record.save()
    finally:
        field.cycles = original
    lines = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert lines == [
        "CyclicDecayField declared amplitude changed for LtRhythm.relevance "
        "member=LtRhythm:reset period=86400: declared baseline 2.0 -> 3.0; "
        "discarded learned amplitude 8.0"
    ]


def test_cycles_and_pressure_round_trip_through_export_and_import():
    source = LtRhythm.create(name="src")
    source.strengthen_cycle("relevance", factor=1.5)
    state = CyclicDecayField.export_state(source, "relevance", None)
    assert state["cycles"] == [[86400, 3.0, 0.0, 2]]
    assert state["pressure"]["rate"] == 0.1

    target = LtRhythm.create(name="dst")
    CyclicDecayField.import_state(target, "relevance", state)
    imported = CyclicDecayField.export_state(target, "relevance", None)
    # import_state drops the deployment-local baseline (#698)
    assert [c[:3] for c in imported["cycles"]] == [[86400, 3.0, 0.0]]
    assert len(imported["cycles"][0]) == 3
    assert imported["pressure"] == state["pressure"]
    target.save()  # the next save acquires a baseline, keeping the amplitude
    assert CyclicDecayField.export_state(target, "relevance", None)["cycles"] == [
        [86400, 3.0, 0.0, 2]
    ]


def test_adjusting_an_empty_cycles_entry_keeps_it_empty():
    """``CYCLES_ADJUST_LUA`` over an empty entry re-packs an empty array; the
    SQL's ``array_agg`` over no rows is NULL, which left Postgres a row with
    periods and no amplitudes, and the next save's reset report raised
    ``TypeError`` (the long-tail probe, seed 2 shape 398)."""
    record = LtRhythm.create(name="empty")
    _plant(record, [], None)
    assert record.weaken_cycle("relevance", factor=1e-9) == []
    assert CyclicDecayField.export_state(record, "relevance", None).get("cycles") == []
    field = LtRhythm._meta.fields["relevance"]
    original = field.cycles
    field.cycles = []
    try:
        record.save()
    finally:
        field.cycles = original
    assert (
        CyclicDecayField.export_state(record, "relevance", None).get("cycles") is None
    )


# -- TD_UPDATE_LUA ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start, reward, mfq, alpha, gamma",
    [
        (Decimal("0"), 1.0, 0.0, 0.1, 0.95),
        (Decimal("0.123456789012345678"), 0.3, 2.0, 0.5, 0.9),
        (Decimal("-7.5"), -1.0, 0.25, 1.0, 0.0),
        (Decimal("1E+2"), 0.0, 0.0, 0.3333333333333333, 0.95),
    ],
)
def test_td_update_replies_and_stores_the_scripts_tostring(
    start, reward, mfq, alpha, gamma
):
    record = LtValue.create(name="v", q_value=start)
    q = float(start)
    td = reward + gamma * mfq - q
    new_q = q + alpha * td
    reply = TDValueField.td_update(
        record, "q_value", reward=reward, max_future_q=mfq, alpha=alpha, gamma=gamma
    )
    assert reply == float("%.14g" % td)
    assert LtValue.query.get(name="v").q_value == Decimal("%.14g" % new_q)


def test_td_update_inside_a_unit_of_work(backend_is_redis):
    """Redis queues the script on a pipeline and replies ``None``; a Postgres
    unit of work runs the update inside its transaction, so the TD error is
    known and returned (documented, as ``update_confidence`` does)."""
    record = LtValue.create(name="u")
    with popoto.backends.get_backend(LtValue).transaction() as uow:
        reply = TDValueField.td_update(
            record,
            "q_value",
            reward=1.0,
            pipeline=uow if not backend_is_redis else uow.pipeline,
        )
    if backend_is_redis:
        assert reply is None
    else:
        assert reply == 1.0
    assert LtValue.query.get(name="u").q_value == Decimal("0.1")


# -- RESOLVE_PREDICTION_LUA ------------------------------------------------------------------


def test_the_resolved_entry_is_the_scripts_cmsgpack_repack():
    record = LtLedger.create(name="pack")
    PredictionLedgerMixin.record_prediction(
        record,
        predicted={
            "whole": 2.0,
            "half": 0.5,
            "empty": {},
            "gone": None,
            "list": [1.0, 2],
        },
    )
    before = PredictionLedgerMixin.get_prediction_data(record)
    assert before["predicted"]["whole"] == 2.0 and isinstance(
        before["predicted"]["whole"], float
    )
    assert before["predicted"]["empty"] == {} and before["predicted"]["gone"] is None

    assert PredictionLedgerMixin.resolve_prediction(record, actual={"whole": 2.0}) > 0
    after = PredictionLedgerMixin.get_prediction_data(record)
    predicted = after["predicted"]
    assert predicted["whole"] == 2 and type(predicted["whole"]) is int
    assert predicted["half"] == 0.5
    assert predicted["empty"] == []  # an empty Lua table packs as an array
    assert "gone" not in predicted  # t[k] = nil stores nothing
    assert predicted["list"] == [1, 2]
    assert after["resolved"] is True and after["resolution_mode"] == "explicit"
    assert isinstance(after["resolved_at"], str)
    assert "recorded_at" in after


def test_a_bytes_prediction_cannot_be_resolved():
    """Redis's cmsgpack reads no msgpack ``bin``: the script's ``pcall``
    fails and the resolution is refused (``None``), on both legs."""
    record = LtLedger.create(name="bin")
    PredictionLedgerMixin.record_prediction(record, predicted={"raw": b"\x00\x01"})
    assert PredictionLedgerMixin.resolve_prediction(record, actual={"raw": 1}) is None
    assert PredictionLedgerMixin.auto_resolve(record, "acted") is None
    assert PredictionLedgerMixin.get_prediction_data(record)["resolved"] is False
    assert PredictionLedgerMixin.get_highest_errors(LtLedger) == []


def test_a_nan_prediction_error_is_a_documented_divergence(backend_is_redis):
    """``inf`` against a number is a NaN error. The script's ``ZADD`` refuses
    it after its ``HSET``, which Redis does not roll back, so the entry reads
    resolved with no error-set member; Postgres refuses it before anything is
    written. The same text on both legs, a different class (a row of the
    divergence table; the probe's ``ledger_nan_error`` class)."""
    import redis

    record = LtLedger.create(name="nan")
    PredictionLedgerMixin.record_prediction(record, predicted={"rel": math.inf})
    expected = redis.exceptions.ResponseError if backend_is_redis else ValueError
    with pytest.raises(expected, match="value is not a valid float"):
        PredictionLedgerMixin.resolve_prediction(record, actual={"rel": 1.0})
    data = PredictionLedgerMixin.get_prediction_data(record)
    if backend_is_redis:
        assert data["resolved"] is True
        assert math.isnan(data["prediction_error"])
    else:
        assert data["resolved"] is False
        assert data["prediction_error"] is None
    assert PredictionLedgerMixin.get_highest_errors(LtLedger) == []


def test_a_non_string_key_is_refused_on_read():
    record = LtLedger.create(name="keys")
    PredictionLedgerMixin.record_prediction(record, predicted={1: "a"})
    with pytest.raises(ValueError, match="not allowed for map key"):
        PredictionLedgerMixin.get_prediction_data(record)


@pytest.mark.parametrize(
    "limit, expected",
    [(10, ["c", "b", "a"]), (2, ["c", "b"]), (0, ["c", "b", "a"]), (-1, ["c", "b"])],
)
def test_highest_errors_follow_zrevrange_rank_arithmetic(limit, expected):
    for name, outcome in (("a", "acted"), ("b", "used"), ("c", "contradicted")):
        record = LtLedger.create(name=name)
        PredictionLedgerMixin.record_prediction(record, predicted={"x": 1.0})
        PredictionLedgerMixin.auto_resolve(record, outcome)
    got = PredictionLedgerMixin.get_highest_errors(LtLedger, limit=limit)
    assert [k.split(":")[-1] for k, _ in got] == expected


def test_ties_order_by_member_descending():
    for name in ("m1", "m3", "m2"):
        record = LtLedger.create(name=name)
        PredictionLedgerMixin.record_prediction(record, predicted={"x": 1.0})
        PredictionLedgerMixin.auto_resolve(record, "dismissed")
    got = PredictionLedgerMixin.get_highest_errors(LtLedger)
    assert [k for k, _ in got] == ["LtLedger:m3", "LtLedger:m2", "LtLedger:m1"]


def test_the_ledger_round_trips_through_export_and_import():
    source = LtLedger.create(name="s")
    PredictionLedgerMixin.record_prediction(source, predicted={"x": 1.0})
    PredictionLedgerMixin.auto_resolve(source, "contradicted")
    state = PredictionLedgerMixin.export_state(source)
    assert state["entry"]["resolved"] is True

    target = LtLedger.create(name="t")
    PredictionLedgerMixin.import_state(target, state)
    assert PredictionLedgerMixin.get_prediction_data(target) == state["entry"]
    got = dict(PredictionLedgerMixin.get_highest_errors(LtLedger))
    assert got[target.db_key.redis_key] == pytest.approx(0.9)


def test_observation_auto_resolve_and_feedback():
    """``contradicted``: the confidence signal, the auto-resolve (error 0.9)
    and its feedback signal (0.2) -- two confidence updates, in that order."""
    record = LtLedger.create(name="obs")
    PredictionLedgerMixin.record_prediction(record, predicted={"x": 1.0})
    ObservationProtocol.on_context_used(
        [record], {record.db_key.redis_key: "contradicted"}
    )
    data = PredictionLedgerMixin.get_prediction_data(record)
    assert data["resolution_mode"] == "observed"
    assert data["prediction_error"] == pytest.approx(0.9)
    conf = ConfidenceField.get_confidence_data(record, "certainty")
    assert conf["evidence_count"] == 2 and conf["contradictions"] == 2


def test_an_unedited_declaration_logs_nothing(caplog):
    """The shared reset-log helper fires only for a reset, on both legs."""
    record = LtRhythm.create(name="quiet")
    with caplog.at_level(logging.INFO, logger="POPOTO.CyclicDecayField"):
        record.save()
    assert not [r for r in caplog.records if r.name == "POPOTO.CyclicDecayField"]


# -- an expired Meta.ttl record (#783's live-row filter) ----------------------------


def _expiring(name, **values):
    record = LtTtl(name=name, **values)
    record._ttl = SHORT
    record.save()
    return record


def test_the_ledger_treats_an_expired_record_as_absent():
    """Redis's ``EXISTS`` guard misses an expired key: ``record`` and
    ``resolve`` raise ``TypeError``, ``auto_resolve`` returns ``None`` and
    nothing is resolved. The entry recorded while the record lived stays
    (the ``$PL:`` keys carry no TTL; the ledger table is not the row)."""
    record = _expiring("ledger")
    PredictionLedgerMixin.record_prediction(record, predicted={"x": 1.0})
    time.sleep(PAST)
    with pytest.raises(TypeError, match="saved model instance"):
        PredictionLedgerMixin.record_prediction(record, predicted={"x": 2.0})
    with pytest.raises(TypeError, match="saved model instance"):
        PredictionLedgerMixin.resolve_prediction(record, actual={"x": 1.0})
    assert PredictionLedgerMixin.auto_resolve(record, "acted") is None
    assert PredictionLedgerMixin.get_highest_errors(LtTtl) == []
    entry = PredictionLedgerMixin.get_prediction_data(record)
    assert entry["predicted"] == {"x": 1.0} and entry["resolved"] is False


def test_td_update_on_an_expired_record_reads_q_as_zero():
    """The script's ``HGET`` of an expired key is ``nil``, so ``Q = 0``: the
    reply ignores the value stored while the record lived. (Redis then
    ``HSET``s a hash holding only the value; Postgres writes nothing --
    the documented "record that no longer exists" row.)"""
    record = _expiring("td", q_value=Decimal("0.5"))
    time.sleep(PAST)
    reply = TDValueField.td_update(record, "q_value", reward=1.0, alpha=0.5)
    assert reply == 1.0  # 1.0 - 0, not 1.0 - 0.5


def test_cycle_state_of_an_expired_record_is_a_documented_divergence(backend):
    """Redis keeps the cycles and pressure companion entries after the
    record key expires, so an adjustment, an export and the merge of a later
    save all see them; on Postgres they went with the row, as #783's
    confidence state did: the adjustment finds no entry, the export no
    record, and a save over the expired key starts from the declaration."""
    record = _expiring("cyc")
    assert record.strengthen_cycle("relevance", factor=1.5) == [[86400, 3.0, 0.0]]
    time.sleep(PAST)
    adjusted = record.strengthen_cycle("relevance", factor=2.0)
    exported = CyclicDecayField.export_state(record, "relevance", None)
    again = LtTtl(name="cyc")
    again._ttl = None
    again.save()
    merged = CyclicDecayField.export_state(again, "relevance", None)["cycles"]
    if backend.is_redis:
        assert adjusted == [[86400, 6.0, 0.0]]
        assert exported["cycles"] == [[86400, 6.0, 0.0, 2]]
        assert merged == [[86400, 6.0, 0.0, 2]]
    else:
        assert adjusted == []
        assert exported is None
        assert merged == [[86400, 2.0, 0.0, 2]]


# -- CYCLES_ADJUST_LUA's tonumber(factor) ------------------------------------------


@pytest.mark.parametrize("factor", [None, "abc", True, "", "1_0"])
def test_a_non_numeric_factor_is_refused_before_writing(backend, factor):
    """``tonumber`` of the factor is ``nil`` and the script's first
    multiplication fails: nothing is written. Same message on both legs;
    Redis raises ``ResponseError``, Postgres ``ValueError`` (documented)."""
    record = LtRhythm.create(name="nil")
    expected = redis.exceptions.ResponseError if backend.is_redis else ValueError
    with pytest.raises(expected) as caught:
        record.weaken_cycle("relevance", factor=factor)
    assert str(caught.value).startswith(
        "user_script:15: attempt to perform arithmetic on local 'factor' "
        "(a nil value) script: "
    )
    if not backend.is_redis:
        from popoto.backends.postgres.longtail import _adjust_nil_factor_error

        assert str(caught.value) == _adjust_nil_factor_error()
    cycles = CyclicDecayField.export_state(record, "relevance", None)["cycles"]
    assert cycles == [[86400, 2.0, 0.0, 2]]  # never NaN


def test_a_nil_factor_reaches_no_arithmetic_without_a_cycle():
    """No entry is the script's ``nil`` (``[]``) and an empty entry loops
    over nothing: neither raises, on either leg."""
    record = LtRhythm.create(name="empty")
    _plant(record, [], None)
    assert record.strengthen_cycle("relevance", factor=None) == []
    other = LtRhythm.create(name="none")
    backend = non_redis_backend(other)
    if backend is None:
        field = other._meta.fields["relevance"]
        popoto.get_redis().hdel(
            field.get_cycles_hash_key(other, "relevance"), other.db_key.redis_key
        )
    else:
        table = backend._table(other._meta.spec).qualified
        backend._run(
            f'UPDATE {table} SET "relevance__cycle_period" = NULL ' 'WHERE "_pk" = %s',
            [other.db_key.redis_key],
            write=True,
        )
    assert other.strengthen_cycle("relevance", factor="abc") == []


@pytest.mark.parametrize(
    "factor, amplitude", [("0x10", 32.0), (" 0x1p-1 ", 1.0), ("+0X2", 4.0)]
)
def test_a_hex_factor_reads_as_lua_tonumber(factor, amplitude):
    record = LtRhythm.create(name="hex")
    assert record.strengthen_cycle("relevance", factor=factor) == [
        [86400, amplitude, 0.0]
    ]


_TONUMBER_CASES = [
    "0x10", "-0x10", " 0x10 ", "0X1F", "0x", "0x1p3", "0x1.8", "0x.8", "0x1.",
    "0x10p", "0x10 x", "0x1p99999", "-0x1p99999", "0x1p-99999", "1e5", " 5 ",
    "5\n", "\t5", "\v1", "1_0", "+5", ".5", "5.", ".", "", " ", "1e", "1e+",
    "1.5x", "nan", "NaN", "-nan", "nan(1)", "inf", "-inf", "Infinity",
    "infinit", "inf ", "True", "None", "abc", "٣", "1e400", "-1e400",
    "1e-400", "-1e-400", "-0", "4.9e-324", "1.7976931348623159e308", "1e5\x00x",
]  # fmt: skip


def test_lua_tonumber_matches_the_servers():
    """The Postgres leg's ``tonumber`` against Redis's own, case by case
    (the value and the sign of a zero)."""
    pytest.importorskip("psycopg")
    from popoto.backends.postgres.longtail import lua_tonumber

    script = (
        "local n = tonumber(ARGV[1]) if n == nil then return 'nil' end "
        "local z = '+' if n == 0 and 1 / n < 0 then z = '-' end "
        "return z .. string.format('%.17g', n)"
    )
    client = popoto.get_redis()
    for text in _TONUMBER_CASES:
        server = client.eval(script, 0, text).decode()
        ours = lua_tonumber(text)
        if server == "nil":
            assert ours is None, text
            continue
        assert ours is not None, text
        value = float(server[1:].replace("-nan", "nan"))
        if math.isnan(value):
            assert math.isnan(ours), text
            continue
        assert ours == value, text
        if value == 0:
            assert (math.copysign(1, ours) < 0) == (server[0] == "-"), text


# -- TD_UPDATE_LUA's tonumber of the stored value ----------------------------------


@pytest.mark.parametrize(
    "start",
    [
        Decimal("1e400"),
        Decimal("-1e400"),
        Decimal("1e-400"),
        Decimal("-1e-330"),
        Decimal("1.7976931348623159e308"),  # past the midpoint: inf
        Decimal("1.7976931348623158e308"),  # below it: DBL_MAX
        Decimal("2.4703282292062327e-324"),  # below 2**-1075: 0
        Decimal("2.5e-324"),  # above it: the smallest subnormal
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_td_update_reads_an_out_of_range_value_as_tonumber_does(start):
    """Lua's ``tonumber`` of the stored text overflows to ``±inf`` and
    underflows to ``±0``; Postgres's ``numeric::float8`` would raise "out of
    range", so the read is clamped at ``strtod``'s rounding boundaries."""
    record = LtValue.create(name="range", q_value=start)
    reply = TDValueField.td_update(
        record, "q_value", reward=1.0, max_future_q=0.0, alpha=0.5, gamma=0.0
    )
    q = float(str(start))
    td = 1.0 - q
    new_q = q + 0.5 * td
    stored = LtValue.query.get(name="range").q_value
    if math.isnan(td):
        assert math.isnan(reply)
    else:
        assert reply == float("%.14g" % td)
        assert math.copysign(1, reply) == math.copysign(1, td)
    if math.isnan(new_q):
        assert stored.is_nan()
    else:
        assert stored == Decimal("%.14g" % new_q)


def test_td_update_from_a_negative_zero_is_a_documented_divergence(backend):
    """A ``numeric`` has no ``-0``: ``Decimal('-0')`` is stored as ``0`` on
    Postgres, where Redis's ``tonumber("-0")`` is ``-0.0``. With a target of
    ``-0.0`` the script's ``-0.0 - -0.0`` is ``0.0``; Postgres's ``-0.0 -
    0.0`` is ``-0.0``. The stored values compare equal."""
    record = LtValue.create(name="negzero", q_value=Decimal("-0"))
    reply = TDValueField.td_update(
        record, "q_value", reward=-0.0, max_future_q=-0.0, alpha=1.0, gamma=0.0
    )
    assert reply == 0.0
    assert math.copysign(1, reply) == (1.0 if backend.is_redis else -1.0)
    assert LtValue.query.get(name="negzero").q_value == 0


# -- popoto.batch() --------------------------------------------------------------------


def test_long_tail_writes_join_a_batch():
    """A batched adjustment, pressure resolution, TD update and ledger write
    land on ``execute()`` and not before: Redis queues them; on Postgres each
    joins the batch's transaction (#783) behind the record lock it holds."""
    rhythm = LtRhythm.create(name="batch")
    value = LtValue.create(name="batch", q_value=Decimal("0"))
    ledger = LtLedger.create(name="batch")
    pipe = popoto.batch()
    try:
        assert rhythm.strengthen_cycle("relevance", factor=1.5, pipeline=pipe) is pipe
        assert rhythm.resolve_pressure("relevance", pipeline=pipe) is pipe
        reply = TDValueField.td_update(value, "q_value", reward=1.0, pipeline=pipe)
        assert reply is None
        PredictionLedgerMixin.record_prediction(
            ledger, predicted={"x": 1.0}, pipeline=pipe
        )
        state = CyclicDecayField.export_state(rhythm, "relevance", None)
        assert state["cycles"] == [[86400, 2.0, 0.0, 2]]
        assert LtValue.query.get(name="batch").q_value == Decimal("0")
        assert PredictionLedgerMixin.get_prediction_data(ledger) is None
        pipe.execute()
    finally:
        pipe.reset()
    state = CyclicDecayField.export_state(rhythm, "relevance", None)
    assert state["cycles"] == [[86400, 3.0, 0.0, 2]]
    assert state["pressure"]["rate"] == 0.1
    assert LtValue.query.get(name="batch").q_value == Decimal("0.1")
    entry = PredictionLedgerMixin.get_prediction_data(ledger)
    assert entry["predicted"] == {"x": 1.0}


def test_a_reset_batch_writes_no_long_tail_state():
    rhythm = LtRhythm.create(name="reset")
    pipe = popoto.batch()
    rhythm.strengthen_cycle("relevance", factor=1.5, pipeline=pipe)
    pipe.reset()
    state = CyclicDecayField.export_state(rhythm, "relevance", None)
    assert state["cycles"] == [[86400, 2.0, 0.0, 2]]
