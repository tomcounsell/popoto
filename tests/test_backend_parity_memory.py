"""Ranking and memory state on both conformance legs (#759 M2a).

The existing memory test files assert much of their behaviour by planting or
reading Redis structures through the raw client (a backdated ``ZADD``, a
confidence ``HSET``, ``LLEN`` of the staged-read list), so those tests carry
``redis_only``. This file re-states the same behaviours with **leg-neutral
helpers** -- on Redis they do exactly what those tests do, on Postgres the
equivalent column write or read -- so every assertion here runs on both legs
from the same code:

* the ``DECAY_SCORE_LUA`` formula, base scores, confidence modulation (its
  neutrality, its direction, the sub-one-day guard, the clamp) and the
  ``power()`` overflow/underflow boundary rows, against a Python transcription
  of the script and against each other;
* tie order (``_pk`` bytes ascending), truncation, partitions and ``touch``;
* the capped-evidence confidence update, bit for bit;
* staged and confirmed reads, including expiry by time;
* all five ``ObservationProtocol`` outcomes and ``RecallProposal``;
* ``composite_score`` over the decay, confidence, access and sorted arms.

Where the legs legitimately differ, the test pins both behaviours
(``backend_is_redis``); each such row is in docs/features/postgres-backend.md.
"""

import math
import time
from decimal import Decimal

import msgpack
import pytest

import popoto
from popoto import (
    AccessTrackerMixin,
    ConfidenceField,
    DecayingSortedField,
    ObservationProtocol,
    RecallProposal,
    WriteFilterMixin,
)
from popoto.backends import BackendCapabilityError, get_backend
from popoto.backends.postgres.memory import lua_tostring, partition_where
from popoto.fields.constants import Defaults
from popoto.fields.decaying_sorted_field import confidence_modulation_args
from popoto.models.query import QueryException

pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]

DAY = 86400.0


# -- models -------------------------------------------------------------------


class ParityDecay(popoto.Model):
    name = popoto.UniqueKeyField()
    weight = popoto.FloatField(default=1.0)
    count = popoto.IntField(default=1)
    amount = popoto.DecimalField(default=Decimal("1"))
    label = popoto.StringField(default="x")
    flag = popoto.BooleanField(default=True)
    relevance = DecayingSortedField(decay_rate=0.5, base_score_field="weight")


class ParityModulated(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(decay_rate=0.5)
    certainty = ConfidenceField()


class ParityLowPrior(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(decay_rate=0.5)
    certainty = ConfidenceField(initial_confidence=0.3)


class ParityPartitioned(popoto.Model):
    name = popoto.UniqueKeyField()
    agent = popoto.KeyField(null=False)
    note = popoto.StringField(default="")
    relevance = DecayingSortedField(decay_rate=0.5, partition_by="agent")


class ParityConfidence(popoto.Model):
    name = popoto.UniqueKeyField()
    certainty = ConfidenceField()


class ParityCapped(popoto.Model):
    name = popoto.UniqueKeyField()
    certainty = ConfidenceField(evidence_cap=5)


class ParityTracked(AccessTrackerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")


class ParityShortTTL(AccessTrackerMixin, popoto.Model):
    _staged_ttl_seconds = 1
    name = popoto.UniqueKeyField()


class ParityMemory(AccessTrackerMixin, popoto.Model):
    """The memory shape ObservationProtocol drives on both backends today:
    a decay clock, a confidence and read tracking (no cyclic field)."""

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    relevance = DecayingSortedField(decay_rate=0.5)
    certainty = ConfidenceField()


class ParityTrackedConfident(AccessTrackerMixin, popoto.Model):
    """Read tracking and a confidence but no decay clock: ``acted`` has
    nothing to touch, so only the access and confidence effects apply."""

    name = popoto.UniqueKeyField()
    certainty = ConfidenceField()


class ParityComposite(AccessTrackerMixin, WriteFilterMixin, popoto.Model):
    """``test_composite_score_query.CompositeMemory`` without the
    CoOccurrenceField (Postgres stores that from M4)."""

    name = popoto.UniqueKeyField()
    importance = popoto.FloatField(default=0.5)
    relevance = DecayingSortedField(base_score_field="importance")
    certainty = ConfidenceField(initial_confidence=0.5)

    def compute_filter_score(self):
        return self.importance


class ParityScored(popoto.Model):
    name = popoto.UniqueKeyField()
    score = popoto.SortedField(type=float, default=0.0)


class ParityPartComposite(popoto.Model):
    name = popoto.UniqueKeyField()
    category = popoto.KeyField(null=False)
    relevance = DecayingSortedField(partition_by="category")


MODELS = [
    ParityDecay,
    ParityModulated,
    ParityLowPrior,
    ParityPartitioned,
    ParityConfidence,
    ParityCapped,
    ParityTracked,
    ParityShortTTL,
    ParityMemory,
    ParityTrackedConfident,
    ParityComposite,
    ParityScored,
    ParityPartComposite,
]


@pytest.fixture(autouse=True)
def _clean(backend):
    def wipe():
        for model in MODELS:
            model.delete_all()
            for name, field in model._meta.fields.items():
                cache = getattr(field, "_confidence_modulation_cache", None)
                if cache is not None:
                    cache.clear()
        if backend.is_redis:
            client = popoto.get_redis()
            for pattern in ("$RP:Parity*", "$AT:Parity*", "$WF:Parity*"):
                for key in client.scan_iter(pattern):
                    client.delete(key)

    wipe()
    yield
    wipe()


# -- leg-neutral helpers ------------------------------------------------------


def _pg_run(backend, model, sql, params):
    pg = backend.instance
    table = pg._table(model._meta.spec)
    return pg._run(sql.format(table=table.qualified), params)


def backdate(backend, record, ts, field_name="relevance"):
    """Set the record's decay clock to ``ts`` (epoch seconds)."""
    if backend.is_redis:
        field = record._meta.fields[field_name]
        zkey = field.get_partitioned_sortedset_db_key(record, field_name).redis_key
        popoto.get_redis().zadd(zkey, {record.db_key.redis_key: ts})
    else:
        _pg_run(
            backend,
            type(record),
            f'UPDATE {{table}} SET "{field_name}" = %s WHERE "_pk" = %s',
            [ts, record.db_key.redis_key],
        )


def plant_confidence(
    backend,
    record,
    confidence,
    evidence_count=10,
    corroborations=10,
    contradictions=0,
    field_name="certainty",
):
    """Write confidence state directly, bypassing the update rule."""
    if backend.is_redis:
        field = record._meta.fields[field_name]
        popoto.get_redis().hset(
            field.get_data_hash_key(record, field_name),
            record.db_key.redis_key,
            msgpack.packb(
                {
                    "confidence": confidence,
                    "evidence_count": evidence_count,
                    "corroborations": corroborations,
                    "contradictions": contradictions,
                }
            ),
        )
    else:
        _pg_run(
            backend,
            type(record),
            f'UPDATE {{table}} SET "{field_name}__conf" = %s, '
            f'"{field_name}__n" = %s, "{field_name}__corr" = %s, '
            f'"{field_name}__contra" = %s WHERE "_pk" = %s',
            [
                confidence,
                evidence_count,
                corroborations,
                contradictions,
                record.db_key.redis_key,
            ],
        )


def staged_reads(backend, record):
    """Live staged (unconfirmed) reads for ``record``."""
    if backend.is_redis:
        return popoto.get_redis().llen(record._at_key("staged"))
    return record._access_state(get_backend(type(record)))[2]


def rank(
    backend,
    model,
    now,
    *,
    n=None,
    field_name="relevance",
    decay_rate=None,
    base_score_field=None,
    modulate=True,
    partition=None,
):
    """``[(key, score), …]`` from the ranking primitive of each leg:
    ``DecayingSortedField.rank_decayed`` (``DECAY_SCORE_LUA``) on Redis, the
    backend's ``rank_decayed`` (one ``SELECT``) on Postgres."""
    field = model._meta.fields[field_name]
    partition = partition or {}
    if backend.is_redis:
        values = [str(partition[pf]) for pf in field.partition_by]
        zkey = field.get_sortedset_db_key(model, field_name, *values).redis_key
        confidence = (
            confidence_modulation_args(model, field, field_name, filters=partition)
            if modulate
            else None
        )
        raw = field.rank_decayed(
            zkey,
            now=now,
            n=n,
            confidence=confidence,
            decay_rate=decay_rate,
            base_score_field=base_score_field,
        )
        out = []
        for i in range(0, len(raw), 2):
            key = raw[i].decode() if isinstance(raw[i], bytes) else raw[i]
            out.append((key, float(raw[i + 1])))
        return out
    from popoto.fields.decaying_sorted_field import (
        resolve_confidence_modulation_field,
    )

    conf_name = None
    if modulate and Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH:
        conf_name, _ = resolve_confidence_modulation_field(model, field, field_name)
    base = field.base_score_field if base_score_field is None else base_score_field
    scored = backend.instance.rank_decayed(
        model._meta.spec,
        field_name,
        now=now,
        n=n,
        where=partition_where(partition),
        decay_rate=decay_rate,
        base_score_field=base or None,
        confidence_field=conf_name,
    )
    return [(rid.canonical, score) for rid, score in scored]


def c_pow(x, y):
    """C's ``pow``: ``inf`` where Python raises ``OverflowError``."""
    try:
        return math.pow(x, y)
    except OverflowError:
        return math.inf


def lua_decay(now, ts, rate, base=1.0, confidence=None, s=0.5, c0=0.5):
    """``DECAY_SCORE_LUA``'s arithmetic, step for step (the oracle)."""
    elapsed = max((now - ts) / 86400, 0.01)
    sign = -1 if base < 0 else 1
    decayed = sign * abs(base) * c_pow(elapsed, -rate)
    if confidence is not None and s != 0:
        c = max(0, min(1, confidence))
        eff = rate * c_pow(2, s * 2 * (c0 - c))
        decayed = decayed * c_pow(max(elapsed, 1.0), -(eff - rate))
    return lua_tostring(decayed)


def lua_confidence(signals, initial=0.5, cap=20, seed=None):
    """``CAPPED_BAYESIAN_UPDATE_LUA``'s arithmetic over ``signals``."""
    conf, n, corr, contra = seed or (initial, 0, 0, 0)
    for signal in signals:
        n_eff = min(n + 1, cap)
        conf = max(0, min(1, conf + (signal - conf) / (n_eff + 1)))
        n += 1
        if signal >= 0.5:
            corr += 1
        else:
            contra += 1
    return conf, n, corr, contra


def keys(model, *names):
    return [model(name=n).db_key.redis_key for n in names]


# -- the decay formula --------------------------------------------------------


def test_known_decay_values_match_the_formula(backend):
    now = time.time()
    records = {}
    for label, days in [("1day", 1), ("4day", 4), ("100day", 100)]:
        records[label] = ParityDecay.create(name=label, weight=1.0)
        backdate(backend, records[label], now - DAY * days)
    ranked = rank(backend, ParityDecay, now)
    assert [k for k, _ in ranked] == keys(ParityDecay, "1day", "4day", "100day")
    assert [s for _, s in ranked] == [1.0, 0.5, 0.1]


@pytest.mark.parametrize(
    "field_name, value, expected_base",
    [
        ("weight", 2.5, 2.5),
        ("weight", -3.0, -3.0),
        ("weight", 0.0, 0.0),
        ("count", 7, 7.0),
        ("count", 2**60 + 1, float(2**60 + 1)),
        ("amount", Decimal("1.50"), 1.5),
        ("amount", Decimal("0.1"), 0.1),
        ("label", "9.5", 1.0),
        ("flag", True, 1.0),
        ("nonexistent", None, 1.0),
        ("", None, 1.0),
    ],
)
def test_base_score_is_read_as_the_script_reads_it(
    backend, field_name, value, expected_base
):
    """A number multiplies the curve; a string, a boolean or a missing field
    is 1.0, as the script's ``HGET`` + ``cmsgpack`` rule gives."""
    now = time.time()
    kwargs = {} if value is None else {field_name: value}
    record = ParityDecay.create(name="b", **kwargs)
    backdate(backend, record, now - 4 * DAY)
    ((key, score),) = rank(backend, ParityDecay, now, base_score_field=field_name)
    assert key == record.db_key.redis_key
    assert score == lua_decay(now, now - 4 * DAY, 0.5, base=expected_base)


def test_the_elapsed_floor_and_future_timestamps(backend):
    now = time.time()
    fresh = ParityDecay.create(name="fresh")
    future = ParityDecay.create(name="future")
    backdate(backend, future, now + 30 * DAY)
    ranked = dict(rank(backend, ParityDecay, now))
    floor = lua_decay(now, now, 0.5)
    assert floor == 10.0
    assert ranked[fresh.db_key.redis_key] == floor
    assert ranked[future.db_key.redis_key] == floor


@pytest.mark.parametrize("rate", [0.1, 0.5, 1.0, 2.0])
def test_decay_rate_override(backend, rate):
    now = time.time()
    record = ParityDecay.create(name="r")
    backdate(backend, record, now - 3 * DAY)
    ((_, score),) = rank(backend, ParityDecay, now, decay_rate=rate)
    assert score == lua_decay(now, now - 3 * DAY, rate)


@pytest.mark.parametrize(
    "days, rate, base, expected",
    [
        # POC (#752) boundary rows: Postgres power()/* raise where C saturates.
        (0.0, 155.0, 1.0, math.inf),  # 0.01-day floor ^ -155 overflows
        (0.0, 200.0, 1.0, math.inf),
        (100.0, 200.0, 1.0, 0.0),  # 100 days ^ -200 underflows
        (0.0, 0.5, 1e300, 1e301),  # base * 10, still finite
        (0.0, 2.0, 1e305, math.inf),  # base * 1e4 overflows the product
        (0.0, 0.5, -1e300, -1e301),
        (0.0, 2.0, -1e305, -math.inf),
        (100.0, 2.0, 1e-300, 1e-304),
        (1e6, 50.0, 1e-300, 0.0),  # product underflows
    ],
)
def test_power_overflow_and_underflow_saturate_as_the_lua_does(
    backend, days, rate, base, expected
):
    now = time.time()
    record = ParityDecay.create(name="edge", weight=base)
    backdate(backend, record, now - days * DAY)
    ((_, score),) = rank(backend, ParityDecay, now, decay_rate=rate)
    assert score == lua_decay(now, now - days * DAY, rate, base=base)
    if math.isinf(expected) or expected == 0.0:
        assert score == expected
    else:
        assert math.isclose(score, expected, rel_tol=1e-9)


def test_a_timestamp_far_in_the_past_scores_zero(backend):
    """A finite ``-1e300`` clock (and ``-inf``) underflows to 0 at rate 2."""
    now = time.time()
    far = ParityDecay.create(name="far")
    gone = ParityDecay.create(name="gone")
    backdate(backend, far, -1e300)
    backdate(backend, gone, -math.inf)
    ranked = dict(rank(backend, ParityDecay, now, decay_rate=2.0))
    assert ranked[far.db_key.redis_key] == 0.0
    assert ranked[gone.db_key.redis_key] == 0.0


# -- confidence modulation ----------------------------------------------------


def test_no_evidence_is_bit_exactly_neutral(backend):
    now = time.time()
    record = ParityModulated.create(name="a")
    backdate(backend, record, now - 10 * DAY)
    modulated = rank(backend, ParityModulated, now)
    plain = rank(backend, ParityModulated, now, modulate=False)
    assert modulated == plain


def test_non_default_initial_confidence_is_neutral_too(backend):
    now = time.time()
    record = ParityLowPrior.create(name="a")
    backdate(backend, record, now - 30 * DAY)
    assert rank(backend, ParityLowPrior, now) == rank(
        backend, ParityLowPrior, now, modulate=False
    )


def test_low_confidence_decays_faster_and_high_slower(backend):
    now = time.time()
    records = {}
    for name, confidence in (("low", 0.05), ("mid", 0.5), ("high", 0.95)):
        records[name] = ParityModulated.create(name=name)
        backdate(backend, records[name], now - 30 * DAY)
        plant_confidence(backend, records[name], confidence)
    modulated = dict(rank(backend, ParityModulated, now))
    neutral = dict(rank(backend, ParityModulated, now, modulate=False))
    low, mid, high = (records[n].db_key.redis_key for n in ("low", "mid", "high"))
    assert modulated[low] < modulated[mid] < modulated[high]
    assert modulated[mid] == neutral[mid]
    s = Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH
    for name, confidence in (("low", 0.05), ("mid", 0.5), ("high", 0.95)):
        key = records[name].db_key.redis_key
        assert modulated[key] == lua_decay(
            now, now - 30 * DAY, 0.5, confidence=confidence, s=s
        )
    ranked = ParityModulated.query.top_by_decay("relevance", n=10)
    assert [r.name for r in ranked] == ["high", "mid", "low"]


@pytest.mark.parametrize("days", [0.001, 0.25, 0.5, 0.999])
def test_inside_one_day_modulation_is_exactly_off(backend, days):
    """The ``max(elapsed, 1.0)`` guard: fresh records score as unmodulated,
    so low confidence never outranks high inside the first day."""
    now = time.time()
    for name, confidence in (("high_conf", 0.99), ("low_conf", 0.01)):
        record = ParityModulated.create(name=name)
        backdate(backend, record, now - days * DAY)
        plant_confidence(backend, record, confidence)
    assert rank(backend, ParityModulated, now) == rank(
        backend, ParityModulated, now, modulate=False
    )


def test_out_of_range_confidence_is_clamped(backend):
    now = time.time()
    under = ParityModulated.create(name="under")
    over = ParityModulated.create(name="over")
    for record in (under, over):
        backdate(backend, record, now - 30 * DAY)
    plant_confidence(backend, under, -4.0)
    plant_confidence(backend, over, 9.0)
    clamped = rank(backend, ParityModulated, now)
    plant_confidence(backend, under, 0.0)
    plant_confidence(backend, over, 1.0)
    assert clamped == rank(backend, ParityModulated, now)


def test_kill_switch_and_zero_strength_turn_modulation_off(backend, monkeypatch):
    now = time.time()
    record = ParityModulated.create(name="a")
    backdate(backend, record, now - 30 * DAY)
    plant_confidence(backend, record, 0.01)
    unmodulated = rank(backend, ParityModulated, now, modulate=False)
    assert rank(backend, ParityModulated, now) != unmodulated
    monkeypatch.setattr(Defaults, "DECAY_CONFIDENCE_MODULATION_STRENGTH", 0.0)
    assert rank(backend, ParityModulated, now) == unmodulated
    monkeypatch.setattr(Defaults, "DECAY_CONFIDENCE_MODULATION_STRENGTH", 0.5)
    monkeypatch.setattr(Defaults, "DECAY_CONFIDENCE_MODULATION_ENABLED", False)
    assert rank(backend, ParityModulated, now) == unmodulated


def test_an_extreme_strength_saturates_instead_of_raising(backend, monkeypatch):
    """``2 ^ (s * 2 * (c0 - c))`` overflows at strength 2000 (POC row)."""
    monkeypatch.setattr(Defaults, "DECAY_CONFIDENCE_MODULATION_STRENGTH", 2000.0)
    now = time.time()
    low = ParityModulated.create(name="low")
    high = ParityModulated.create(name="high")
    for record, confidence in ((low, 0.0), (high, 1.0)):
        backdate(backend, record, now - 30 * DAY)
        plant_confidence(backend, record, confidence)
    ranked = dict(rank(backend, ParityModulated, now))
    for record, confidence in ((low, 0.0), (high, 1.0)):
        assert ranked[record.db_key.redis_key] == lua_decay(
            now, now - 30 * DAY, 0.5, confidence=confidence, s=2000.0
        )
    assert ranked[low.db_key.redis_key] == 0.0


# -- ordering, partitions, touch ---------------------------------------------


TRAP_NAMES = ["B", "a", "aa", "a:1", "a-1", "_", "0", "~", "Z", "a a"]


def test_ties_break_on_key_bytes_ascending_and_truncate_deterministically(backend):
    now = time.time()
    for name in reversed(TRAP_NAMES):
        record = ParityDecay.create(name=name)
        backdate(backend, record, now - DAY)
    expected = sorted(keys(ParityDecay, *TRAP_NAMES), key=lambda k: k.encode())
    ranked = rank(backend, ParityDecay, now)
    assert [k for k, _ in ranked] == expected
    assert len({s for _, s in ranked}) == 1
    assert [k for k, _ in rank(backend, ParityDecay, now, n=3)] == expected[:3]
    via_query = ParityDecay.query.top_by_decay("relevance", n=4)
    assert [r.db_key.redis_key for r in via_query] == expected[:4]


def test_top_by_decay_scans_only_the_partition(backend):
    now = time.time()
    a_old = ParityPartitioned.create(name="a-old", agent="A", note="x")
    ParityPartitioned.create(name="a-new", agent="A", note="y")
    ParityPartitioned.create(name="b-new", agent="B", note="x")
    backdate(backend, a_old, now - 5 * DAY)
    results = ParityPartitioned.query.filter(agent="A").top_by_decay(n=10)
    assert [r.name for r in results] == ["a-new", "a-old"]
    # Only the partition filter scopes the scan, as on Redis, where the
    # partition picks the sorted set and every other filter is ignored.
    results = ParityPartitioned.query.filter(agent="A", note="x").top_by_decay(n=10)
    assert [r.name for r in results] == ["a-new", "a-old"]
    with pytest.raises(QueryException):
        ParityPartitioned.query.top_by_decay(n=10)


def test_touch_moves_the_clock_and_the_ranking(backend):
    now = time.time()
    a = ParityDecay.create(name="touch_a")
    b = ParityDecay.create(name="touch_b")
    for record in (a, b):
        backdate(backend, record, now - 5 * DAY)
    assert [r.name for r in ParityDecay.query.top_by_decay(n=10)] == [
        "touch_a",
        "touch_b",
    ]
    stamp = b.touch("relevance")
    assert b.relevance == stamp
    assert [r.name for r in ParityDecay.query.top_by_decay(n=10)] == [
        "touch_b",
        "touch_a",
    ]
    with pytest.raises(TypeError):
        ParityDecay(name="unsaved").touch("relevance")


def test_a_reload_after_touch_is_a_documented_divergence(backend_is_redis):
    """Redis moves only the sorted-set score, so the hash -- and a reload --
    keeps the save-time value; Postgres's clock *is* the field's column."""
    record = ParityDecay.create(name="t")
    saved = record.relevance
    time.sleep(0.01)
    stamp = record.touch("relevance")
    reloaded = ParityDecay.query.get(name="t")
    assert reloaded.relevance == (saved if backend_is_redis else stamp)


def test_field_rank_decayed_takes_a_redis_key_so_postgres_refuses_it(
    backend_is_redis,
):
    """Plan §1.1 TD-40: the field method ranks a sorted-set *key*; on Postgres
    it names ``top_by_decay`` instead of silently ranking an empty Redis key."""
    ParityDecay.create(name="x")
    field = ParityDecay._meta.fields["relevance"]
    zkey = field.get_sortedset_db_key(ParityDecay, "relevance").redis_key
    if backend_is_redis:
        assert len(field.rank_decayed(zkey, now=time.time())) == 2
    else:
        with pytest.raises(BackendCapabilityError, match="top_by_decay"):
            field.rank_decayed(zkey, now=time.time())


# -- confidence ---------------------------------------------------------------


SEQUENCE = [0.9, 0.1, 0.8, 0.2, 0.7, 0.6, 0.45, 0.3, 0.5, 0.49999, 1.0, 0.0]


def test_the_capped_update_matches_the_script_bit_for_bit(backend):
    record = ParityConfidence.create(name="seq")
    for i, signal in enumerate(SEQUENCE, start=1):
        returned = ConfidenceField.update_confidence(record, "certainty", signal)
        conf, n, corr, contra = lua_confidence(SEQUENCE[:i])
        assert returned == lua_tostring(conf)
        assert record.certainty == returned
    data = ConfidenceField.get_confidence_data(record, "certainty")
    assert data == {
        "confidence": conf,
        "evidence_count": n,
        "corroborations": corr,
        "contradictions": contra,
    }
    assert ConfidenceField.get_confidence(record, "certainty") == conf


def test_the_cap_freezes_the_gain(backend):
    default = ParityConfidence.create(name="d")
    capped = ParityCapped.create(name="c")
    for record in (default, capped):
        plant_confidence(backend, record, 0.9, evidence_count=20, corroborations=20)
    for k in range(1, 16):
        d = ConfidenceField.update_confidence(default, "certainty", 0.1)
        assert math.isclose(d, 0.1 + 0.8 * (20 / 21) ** k, rel_tol=0, abs_tol=1e-12)
    assert d < 0.5
    c = ConfidenceField.update_confidence(capped, "certainty", 0.1)
    assert abs(c - (0.9 - 0.8 / 6)) < 1e-12


def test_a_seed_and_a_resave_never_reset_the_state(backend):
    record = ParityConfidence.create(name="s")
    assert ConfidenceField.get_confidence_data(record, "certainty") == {
        "confidence": 0.5,
        "evidence_count": 0,
        "corroborations": 0,
        "contradictions": 0,
    }
    ConfidenceField.update_confidence(record, "certainty", 0.9)
    record.certainty = 0.123
    record.save()
    data = ConfidenceField.get_confidence_data(record, "certainty")
    assert data["evidence_count"] == 1
    assert data["confidence"] == 0.7
    # The attribute is the hash value Redis stores; the state is separate.
    assert ParityConfidence.query.get(name="s").certainty == 0.123


def test_update_confidence_refuses_an_unsaved_or_deleted_record(backend):
    with pytest.raises(TypeError, match="saved model instance"):
        ConfidenceField.update_confidence(
            ParityConfidence(name="never"), "certainty", 0.9
        )
    record = ParityConfidence.create(name="gone")
    record.delete()
    with pytest.raises(TypeError, match="saved model instance"):
        ConfidenceField.update_confidence(record, "certainty", 0.9)
    assert ConfidenceField.get_confidence_data(record, "certainty") == {
        "confidence": 0.5,
        "evidence_count": 0,
        "corroborations": 0,
        "contradictions": 0,
    }


def test_concurrent_updates_land_on_the_running_mean(backend):
    import threading

    record = ParityConfidence.create(name="threads")
    signals = [0.9, 0.1, 0.8, 0.2, 0.7, 0.3, 0.6, 0.4, 0.95, 0.05]
    threads = [
        threading.Thread(
            target=ConfidenceField.update_confidence, args=(record, "certainty", s)
        )
        for s in signals
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = ConfidenceField.get_confidence_data(record, "certainty")
    assert data["evidence_count"] == len(signals)
    assert abs(data["confidence"] - (0.5 + sum(signals)) / 11) < 1e-12


# -- access tracking ----------------------------------------------------------


def test_staged_reads_confirm_and_discard(backend):
    item = ParityTracked.create(name="t")
    before = time.time()
    item.on_read()
    item.on_read()
    after = time.time()
    assert staged_reads(backend, item) == 2
    assert item.confirm_access() == 2
    assert staged_reads(backend, item) == 0
    assert item.access_count == 2
    assert before <= item.last_accessed <= after
    item.on_read()
    item.on_read()
    item.on_read()
    item.discard_staged_access()
    assert staged_reads(backend, item) == 0
    assert item.confirm_access() == 0
    assert item.access_count == 2
    with pytest.raises(TypeError):
        ParityTracked(name="unsaved").confirm_access()


def test_queries_stage_reads_unless_untracked(backend):
    ParityTracked.create(name="q")
    ParityTracked.query.get(name="q")
    probe = ParityTracked(name="q")
    assert staged_reads(backend, probe) == 1
    ParityTracked.query.filter(name="q").all()
    assert staged_reads(backend, probe) == 2
    ParityTracked.query.filter(name="q").no_track().all()
    assert staged_reads(backend, probe) == 2
    ParityTracked.query.get_many([probe.db_key.redis_key] * 2)
    assert staged_reads(backend, probe) == 4


def test_staged_reads_expire_after_the_ttl(backend):
    item = ParityShortTTL.create(name="ttl")
    item.on_read()
    time.sleep(1.2)
    assert staged_reads(backend, item) == 0
    assert item.confirm_access() == 0
    assert item.access_count == 0
    assert item.last_accessed is None
    item.on_read()
    assert item.confirm_access() == 1
    assert item.access_count == 1
    # A read after the stage expired starts a new stage rather than adding to
    # the dead one (the Redis list was gone; RPUSH made a new one).
    item.on_read()
    time.sleep(1.2)
    item.on_read()
    assert staged_reads(backend, item) == 1
    assert item.confirm_access() == 1
    assert item.access_count == 2


def test_concurrent_reads_and_confirms_lose_nothing(backend):
    import threading

    item = ParityTracked.create(name="cc")

    def reads():
        for _ in range(20):
            item.on_read()

    def confirms():
        for _ in range(5):
            item.confirm_access()

    threads = [threading.Thread(target=reads), threading.Thread(target=confirms)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    item.confirm_access()
    assert item.access_count == 20


# -- observation --------------------------------------------------------------


def _memories(backend, *names, days=5):
    now = time.time()
    out = []
    for name in names:
        record = ParityMemory.create(name=name)
        backdate(backend, record, now - days * DAY)
        out.append(record)
    return out


def test_every_outcome_has_its_effects(backend):
    acted, used, dismissed, deferred, contradicted = _memories(
        backend, "acted", "used", "dismissed", "deferred", "contradicted"
    )
    for record in (acted, used, dismissed, deferred, contradicted):
        record.on_read()
    clock = acted.relevance
    ObservationProtocol.on_context_used(
        [acted, used, dismissed, deferred, contradicted],
        {
            acted.db_key.redis_key: "acted",
            used.db_key.redis_key: "used",
            dismissed.db_key.redis_key: "dismissed",
            contradicted.db_key.redis_key: "contradicted",
        },
    )
    assert acted.relevance > clock
    assert [r.name for r in ParityMemory.query.top_by_decay(n=1)] == ["acted"]
    assert (acted.access_count, used.access_count) == (1, 1)
    for record in (dismissed, deferred, contradicted):
        assert staged_reads(backend, record) == 0
        assert record.access_count == 0

    def conf(record):
        return ConfidenceField.get_confidence_data(record, "certainty")

    assert conf(acted)["corroborations"] == 1
    assert acted.certainty == lua_tostring(lua_confidence([0.9])[0])
    assert conf(contradicted)["contradictions"] == 1
    assert contradicted.certainty == lua_tostring(lua_confidence([0.1])[0])
    for record in (used, dismissed, deferred):
        assert conf(record)["evidence_count"] == 0


VERDICTS = ("acted", "used", "dismissed", "deferred", "contradicted")


@pytest.mark.parametrize("model", [ParityTracked, ParityTrackedConfident])
def test_outcomes_on_an_access_tracked_model_without_a_decay_field(backend, model):
    """#773 review gap: a model with ``AccessTrackerMixin`` and no decay
    field through ``on_context_used`` -- ``acted`` has no clock to touch, so
    each outcome reduces to its access (and, where declared, confidence)
    effect, and the proposals still resolve."""
    records = {v: model.create(name=v) for v in VERDICTS}
    for record in records.values():
        record.on_read()
        record.on_read()
    ObservationProtocol.on_surfaced(list(records.values()))
    ObservationProtocol.on_context_used(
        list(records.values()),
        {
            records[v].db_key.redis_key: v
            for v in ("acted", "used", "dismissed", "contradicted")
        },
    )
    for verdict, record in records.items():
        assert staged_reads(backend, record) == 0, verdict
        confirmed = 2 if verdict in ("acted", "used") else 0
        assert record.access_count == confirmed, verdict
        assert (record.last_accessed is not None) == bool(confirmed), verdict
        (fresh,) = model.query.filter(name=verdict).no_track().all()
        assert fresh.access_count == confirmed, verdict
    assert RecallProposal.get_pending(model) == []
    if model is ParityTrackedConfident:

        def conf(verdict):
            return ConfidenceField.get_confidence_data(records[verdict], "certainty")

        assert conf("acted")["corroborations"] == 1
        assert records["acted"].certainty == lua_tostring(lua_confidence([0.9])[0])
        assert conf("contradicted")["contradictions"] == 1
        assert records["contradicted"].certainty == lua_tostring(
            lua_confidence([0.1])[0]
        )
        for verdict in ("used", "dismissed", "deferred"):
            assert conf(verdict)["evidence_count"] == 0, verdict
    # a second round only confirms what was staged since
    records["acted"].on_read()
    ObservationProtocol.on_context_used(
        [records["acted"], records["used"]],
        {records["acted"].db_key.redis_key: "acted"},
    )
    assert records["acted"].access_count == 3
    assert records["used"].access_count == 2


def test_unsaved_instances_degrade_and_the_batch_still_lands(backend):
    (saved,) = _memories(backend, "saved")
    saved.on_read()
    unsaved = ParityMemory(name="unsaved")
    outcome = {saved.db_key.redis_key: "acted", unsaved.db_key.redis_key: "acted"}
    ObservationProtocol.on_context_used([unsaved, saved], outcome)
    assert saved.access_count == 1
    assert ConfidenceField.get_confidence_data(saved, "certainty")[
        "evidence_count"
    ] == (1)
    for verdict in ("acted", "used", "dismissed", "deferred", "contradicted"):
        ObservationProtocol.on_context_used(
            [ParityMemory(name=f"u-{verdict}")],
            {ParityMemory(name=f"u-{verdict}").db_key.redis_key: verdict},
        )
    assert ParityMemory.query.count() == 1
    with pytest.raises(ValueError, match="Invalid outcome"):
        ObservationProtocol.on_context_used([saved], {saved.db_key.redis_key: "x"})


def test_recall_proposals(backend):
    m1, m2 = _memories(backend, "p1", "p2")
    ObservationProtocol.on_surfaced([m1, m2], reason="proactive")
    ObservationProtocol.on_surfaced([m1], partition="agent-1")
    pending = RecallProposal.get_pending(ParityMemory)
    assert sorted(k for k, _ in pending) == keys(ParityMemory, "p1", "p2")
    assert [k for k, _ in RecallProposal.get_pending(ParityMemory, "agent-1")] == [
        m1.db_key.redis_key
    ]
    ObservationProtocol.on_context_used([m1], {m1.db_key.redis_key: "acted"})
    assert [k for k, _ in RecallProposal.get_pending(ParityMemory)] == [
        m2.db_key.redis_key
    ]
    assert RecallProposal.resolve(m2, "dismissed") == 1
    assert RecallProposal.resolve(m2, "dismissed") == 0
    assert RecallProposal.expire_stale(ParityMemory, ttl=3600) == []
    time.sleep(0.01)
    assert RecallProposal.expire_stale(ParityMemory, "agent-1", ttl=0) == [
        m1.db_key.redis_key
    ]
    assert RecallProposal.get_pending(ParityMemory, "agent-1") == []


# -- write filter -------------------------------------------------------------


def test_the_write_filter_gate_holds_on_both(backend):
    assert ParityComposite(name="low", importance=0.05).save() is False
    assert ParityComposite(name="mid", importance=0.5).save() is not False
    assert ParityComposite(name="high", importance=0.9).save() is not False
    assert sorted(r.name for r in ParityComposite.query.all()) == ["high", "mid"]


# -- composite ----------------------------------------------------------------


def _composite_corpus(backend):
    now = time.time()
    recent_high = ParityComposite.create(name="recent_high", importance=0.6)
    old_low = ParityComposite.create(name="old_low", importance=0.6)
    middling = ParityComposite.create(name="middling", importance=0.6)
    backdate(backend, old_low, now - 40 * DAY)
    backdate(backend, middling, now - 3 * DAY)
    for _ in range(4):
        ConfidenceField.update_confidence(recent_high, "certainty", 0.95)
        ConfidenceField.update_confidence(old_low, "certainty", 0.05)
    for _ in range(3):
        middling.on_read()
    middling.confirm_access()
    return recent_high, old_low, middling


def test_composite_ranks_by_the_weighted_arms(backend):
    _composite_corpus(backend)
    ranked = ParityComposite.query.composite_score(
        {"relevance": 0.4, "certainty": 0.3, "access_count": 0.3}, limit=10
    )
    assert [r.name for r in ranked] == ["middling", "recent_high", "old_low"]
    two = ParityComposite.query.composite_score(
        {"relevance": 0.5, "certainty": 0.5}, limit=10
    )
    assert [r.name for r in two] == ["recent_high", "middling", "old_low"]


@pytest.mark.parametrize(
    "aggregate, expected",
    [
        ("SUM", ["c", "b", "a"]),
        ("MAX", ["c", "b", "a"]),
        ("MIN", ["c", "b", "a"]),
    ],
)
def test_composite_aggregates_and_scores(backend, aggregate, expected):
    for name, score in (("a", 10.0), ("b", 50.0), ("c", 90.0)):
        ParityScored.create(name=name, score=score)
    scores = []
    ranked = ParityScored.query.composite_score(
        {"score": 2.0},
        limit=10,
        aggregate=aggregate,
        post_filter=lambda key, score: scores.append(score) or True,
    )
    assert [r.name for r in ranked] == expected
    assert scores == [180.0, 100.0, 20.0]


def test_composite_min_score_temperature_post_filter_and_ties(backend):
    for name, score in (("a", 10.0), ("b", 50.0), ("c", 50.0), ("d", 90.0)):
        ParityScored.create(name=name, score=score)
    seen = []
    ranked = ParityScored.query.composite_score(
        {"score": 1.0},
        limit=3,
        min_score=40,
        temperature=0.5,
        post_filter=lambda key, score: seen.append((key, score)) or key != "x",
    )
    # Equal scores come back in descending key order, as ZREVRANGE gives.
    assert [r.name for r in ranked] == ["d", "c", "b"]
    assert [s for _, s in seen] == [180.0, 100.0, 100.0]
    assert ParityScored.query.composite_score({"score": 1.0}, min_score=95) == []


def test_composite_partitioned_decay_arm(backend):
    ParityPartComposite.create(name="a1", category="A")
    ParityPartComposite.create(name="b1", category="B")
    with pytest.raises(QueryException, match="partition"):
        ParityPartComposite.query.composite_score({"relevance": 1.0})
    ranked = ParityPartComposite.query.filter(category="A").composite_score(
        {"relevance": 1.0}
    )
    assert [r.name for r in ranked] == ["a1"]


def test_composite_confidence_arm_spans_the_model(backend):
    """The confidence arm is every record with confidence state -- the whole
    companion hash on Redis -- so it is not narrowed by a decay partition."""
    ParityComposite.create(name="x", importance=0.6)
    ParityComposite.create(name="y", importance=0.6)
    ranked = ParityComposite.query.composite_score({"certainty": 1.0}, limit=10)
    assert [r.name for r in ranked] == ["y", "x"]


def test_composite_priority_arm_is_a_documented_divergence(backend_is_redis):
    """The WriteFilter priority tier is a no-op on Postgres (plan §5 M2), so
    ranking by it is refused there rather than silently empty."""
    ParityComposite.create(name="hi", importance=0.9)
    if backend_is_redis:
        ranked = ParityComposite.query.composite_score({"priority": 1.0})
        assert [r.name for r in ranked] == ["hi"]
    else:
        with pytest.raises(BackendCapabilityError, match="priority"):
            ParityComposite.query.composite_score({"priority": 1.0})


def test_composite_similarity_boost_waits_for_the_vector_arm(backend_is_redis):
    record = ParityComposite.create(name="s", importance=0.6)
    boost = {record.db_key.redis_key: 0.9}
    if backend_is_redis:
        ranked = ParityComposite.query.composite_score(
            {"certainty": 1.0}, similarity_boost=boost
        )
        assert [r.name for r in ranked] == ["s"]
    else:
        with pytest.raises(BackendCapabilityError, match="similarity"):
            ParityComposite.query.composite_score(
                {"certainty": 1.0}, similarity_boost=boost
            )


def test_composite_scores_agree_with_the_arms(backend):
    recent_high, old_low, middling = _composite_corpus(backend)
    seen = {}
    ParityComposite.query.composite_score(
        {"relevance": 0.4, "certainty": 0.3, "access_count": 0.3},
        limit=10,
        post_filter=lambda key, score: seen.__setitem__(key, score) or True,
    )
    now = time.time()
    decay = dict(rank(backend, ParityComposite, now))
    for record in (recent_high, old_low, middling):
        key = record.db_key.redis_key
        data = ConfidenceField.get_confidence_data(record, "certainty")
        expected = 0.4 * decay[key] + 0.3 * data["confidence"]
        if record.access_count:
            expected += 0.3 * record.access_count
        # The decay arm's clock reads time.time() itself: allow its drift.
        assert math.isclose(seen[key], expected, rel_tol=1e-6)


def test_a_nan_decay_score_in_composite_is_a_documented_divergence(
    backend, backend_is_redis
):
    """``0 * inf`` -- a ``-inf`` clock makes ``e ** -rate`` 0, and an
    above-prior confidence makes the correction ``inf ** y`` -- is NaN in both
    legs' ranking. Redis's composite then cannot ``ZADD`` it and the query
    raises; Postgres ranks the record with that arm at 0, the value
    ``ZUNIONSTORE`` gives a NaN product."""
    import redis

    record = ParityModulated.create(name="nan")
    backdate(backend, record, -math.inf)
    plant_confidence(backend, record, 1.0)
    ((_, score),) = rank(backend, ParityModulated, time.time())
    assert math.isnan(score)
    if backend_is_redis:
        with pytest.raises(redis.exceptions.ResponseError, match="not a valid float"):
            ParityModulated.query.composite_score({"relevance": 1.0})
    else:
        seen = []
        ranked = ParityModulated.query.composite_score(
            {"relevance": 1.0}, post_filter=lambda k, s: seen.append(s) or True
        )
        assert [r.name for r in ranked] == ["nan"] and seen == [0.0]


def test_where_a_nan_score_ranks_is_a_documented_divergence(backend, backend_is_redis):
    """A NaN score makes the Lua's comparator inconsistent (``x > nan`` is
    always false), so its ``table.sort`` may misplace the NaN member and even
    misorder real scores around it; Postgres sorts the real scores and ranks
    NaN last. Every member gets the same score on both legs."""
    now = time.time()
    nan_record = ParityModulated.create(name="m-nan")
    backdate(backend, nan_record, -math.inf)
    plant_confidence(backend, nan_record, 1.0)
    for name, days in (("a-fresh", 0.0), ("z-old", 9.0)):
        backdate(backend, ParityModulated.create(name=name), now - days * DAY)
    ranked = rank(backend, ParityModulated, now)
    scores = dict(ranked)
    fresh, old = keys(ParityModulated, "a-fresh", "z-old")
    assert scores[fresh] == lua_decay(now, now, 0.5)
    assert scores[old] == lua_decay(now, now - 9 * DAY, 0.5)
    assert math.isnan(scores[nan_record.db_key.redis_key])
    if not backend_is_redis:
        assert [k for k, _ in ranked] == [fresh, old, nan_record.db_key.redis_key]
