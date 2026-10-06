"""``GeoField`` on Postgres without PostGIS (#759 plan §5 M5).

The two-leg behaviour (radius searches, distances, order, counts, the errors
the server refuses with) is pinned by the conformance files --
``test_geo_with_distances.py``, ``test_geofield.py``, ``test_delete_all.py``,
the geo tests of ``test_query_thread_safety.py`` and ``test_async.py`` -- and
by the seeded probe ``scripts/probe_geo_parity.py`` (a slice runs at the end of
this file). This file covers what has no Redis leg: the columns and the index,
the stored score and decoded position against values read off Redis,
member removal, atomic refusal, ``Meta.ttl``, ``popoto.batch()``, async, and
the scope a geo leaf can and cannot take.
"""

from __future__ import annotations

import asyncio
import importlib.util
import math
from pathlib import Path

import pytest
from redis.exceptions import ResponseError

import popoto
from popoto.backends import BackendCapabilityError, QueryCall
from popoto.backends.planning import plan_from_call
from popoto.backends.postgres import geo
from popoto.backends.postgres.plan import render_where
from popoto.backends.postgres.ttl import frozen_clock
from popoto.models.q import Q
from popoto.models.query import QueryException

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "scripts" / "probe_geo_parity.py"
T0 = 1_900_000_000.0

ROME = (41.902782, 12.496366)
VATICAN = (41.904755, 12.454628)


class GeoPlace(popoto.Model):
    name = popoto.KeyField()
    bucket = popoto.IndexedField(type=str, null=True)
    place = popoto.GeoField()


class GeoTtlPlace(popoto.Model):
    name = popoto.KeyField()
    place = popoto.GeoField()

    class Meta:
        ttl = 60


def _geo_row(admin, pg, key, table="geo_place"):
    return admin.execute(
        f'SELECT "place__geohash", "place__geolon", "place__geolat" '
        f'FROM "{pg.schema}"."{table}" WHERE "_pk" = %s',
        (key,),
    ).fetchone()


def _names(rows):
    return sorted(r.name for r in rows)


# -- the arithmetic, against values read off Redis ------------------------------


ROME_LAT_UNFUSED = 41.902782133789835
"""Rome's decoded latitude in plain IEEE arithmetic: what ``GEOPOS`` replies
on a Redis built without floating-point contraction (x86-64)."""
ROME_LAT_FUSED = 41.90278213378984
"""The same on Redis 8.10.2 built by clang on arm64, which fuses the decode's
``min + (i / 2**step) * scale`` into one rounding: the adjacent double."""


def test_scores_and_positions_match_redis():
    """``GEOADD`` scores and ``GEOPOS`` positions: Rome; a point on longitude
    180, whose 54-bit score the zset's double rounds to a neighbouring cell;
    one on the latitude limit (bit 52)."""
    rome = geo.score_of(12.496366, 41.902782)
    assert rome == 3480343273965391
    assert geo.decode_point(rome) == (12.496366202831268, ROME_LAT_UNFUSED)
    edge = geo.score_of(180.0, 32.7614995816)
    assert edge == 10221133194347524
    assert geo.decode_point(edge)[0] == 180.0
    assert geo.score_of(10.0, 85.05112878) == 6758286363893802
    assert geo.decode_point(6758286363893802)[1] == 85.05112878


def test_reply_distance_is_withdist_four_decimals():
    rome = geo.decode_point(geo.score_of(12.496366, 41.902782))
    meters = geo.distance(12.454628, 41.904755, *rome)
    assert geo.reply_distance(meters, 1000.0) == 3.4621  # GEORADIUS … WITHDIST
    assert geo.reply_distance(0.0, 1609.34) == 0.0
    assert geo.reply_distance(1234.56789, 1.0) == 1234.5679


def test_a_nan_distance_replies_as_x86_64_redis_does():
    """``asin`` of a rounding-inflated argument above 1 is ``NaN`` in C, and
    ``WITHDIST`` prints ``llrint(NaN * 10000)``. x86-64 (CI's
    ``redis:7-alpine``, the reference) returns ``LLONG_MIN`` there, printed
    ``-922337203685477.5808`` whatever the unit; arm64 returns 0 and replies
    ``0.0000`` (the probe's ``nan_distance_arm64``)."""
    assert geo.NAN_DISTANCE_REPLY == float("-922337203685477.5808")
    assert geo.NAN_DISTANCE_REPLY == -(2**63) / 10000
    for conversion in geo.UNIT_TO_METERS.values():
        assert geo.reply_distance(math.nan, conversion) == geo.NAN_DISTANCE_REPLY


def test_a_nan_distance_keeps_the_member_and_sorts_it_first(pg, monkeypatch):
    """``NaN > radius`` is false, so the member is in (``count`` too), and its
    reply sorts it ahead of every real distance, as the Redis path's sort of
    hydrated rows does with x86-64's reply."""
    GeoPlace.create(name="rome", place=ROME)
    GeoPlace.create(name="vatican", place=VATICAN)
    vatican = geo.decode_point(geo.score_of(VATICAN[1], VATICAN[0]))
    original = geo.distance

    def nan_at_vatican(lon1, lat1, lon2, lat2):
        if (lon2, lat2) == vatican:
            return math.nan
        return original(lon1, lat1, lon2, lat2)

    monkeypatch.setattr(geo, "distance", nan_at_vatican)
    near = dict(place=ROME, place_radius=5, place_radius_unit="km")
    rows = GeoPlace.query.filter(place_with_distances=True, **near)
    assert [(r.name, r._geo_distance) for r in rows] == [
        ("vatican", geo.NAN_DISTANCE_REPLY),
        ("rome", 0.0),
    ]
    assert GeoPlace.query.count(**near) == 2


def test_the_arithmetic_is_plain_ieee_never_fused():
    """The port is deterministic across platforms: each ``a*b + c`` rounds
    twice, as a Redis built without contraction computes it. A fusing build
    (clang, arm64) is one ulp away in places -- here, Rome's latitude."""
    assert math.nextafter(ROME_LAT_UNFUSED, 90.0) == ROME_LAT_FUSED
    assert not hasattr(geo, "FUSED_MULTIPLY_ADD")
    lat_min = geo._decode(3480343273965391, geo.GEO_STEP_MAX).lat_min
    sep = geo._deinterleave64(3480343273965391)
    plain = geo.GEO_LAT_MIN + ((sep & 0xFFFFFFFF) / float(1 << 26)) * (
        geo.GEO_LAT_MAX - geo.GEO_LAT_MIN
    )
    assert lat_min == plain


def test_a_score_past_2_52_is_in_no_search_box():
    """Redis's boxes never reach a score of 2**52 or more, so a point on the
    latitude limit or on longitude 180 is stored but no radius search finds
    it -- reproduced, not repaired."""
    ranges = geo.search_ranges(10.0, 85.0, 50.0, 1000.0)
    assert all(hi <= 2**52 for _lo, hi in ranges)
    assert not any(lo <= 6758286363893802 < hi for lo, hi in ranges)


def test_server_argument_parsing():
    assert geo.parse_radius("5e2") == 500.0
    assert geo.parse_radius("0x10") == 16.0
    assert geo.parse_radius(math.inf) == math.inf
    for bad, message in (
        (" 1", "need numeric radius"),
        ("1 ", "need numeric radius"),
        ("1_0", "need numeric radius"),
        (math.nan, "need numeric radius"),
        ("1e999", "need numeric radius"),
        (-1, "radius cannot be negative"),
    ):
        with pytest.raises(QueryException, match=message):
            geo.parse_radius(bad)
    with pytest.raises(QueryException, match="Invalid input of type: 'bool'"):
        geo.parse_radius(True)
    with pytest.raises(
        QueryException, match="invalid longitude,latitude pair 0.000000,86.000000"
    ):
        geo.parse_point(0.0, 86.0)


# -- schema and writes ----------------------------------------------------------


def test_columns_and_a_partial_btree_on_the_score(pg, admin):
    GeoPlace.create(name="rome", place=ROME)
    cols = dict(
        admin.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'geo_place'",
            (pg.schema,),
        ).fetchall()
    )
    assert cols["place"] == "jsonb"
    assert cols["place__geohash"] == "bigint"
    assert cols["place__geolon"] == cols["place__geolat"] == "double precision"
    (indexdef,) = admin.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND "
        "indexname = 'geo_place__geo__place__idx'",
        (pg.schema,),
    ).fetchone()
    assert "place__geohash" in indexdef and "IS NOT NULL" in indexdef


def test_save_stores_the_score_and_the_decoded_position(pg, admin):
    rome = GeoPlace.create(name="rome", place=ROME)
    assert _geo_row(admin, pg, rome.db_key.redis_key) == (
        3480343273965391,
        12.496366202831268,
        ROME_LAT_UNFUSED,
    )
    # The value reads back as given, not quantised.
    assert GeoPlace.query.get(name="rome").place == ROME


def test_moving_clearing_and_deleting_a_point(pg, admin):
    place = GeoPlace.create(name="p", place=ROME)
    near = dict(place_radius=10, place_radius_unit="km")
    assert _names(GeoPlace.query.filter(place=ROME, **near)) == ["p"]

    place.place = (48.8566, 2.3522)  # Paris
    place.save()
    assert _names(GeoPlace.query.filter(place=ROME, **near)) == []
    assert _names(GeoPlace.query.filter(place=(48.8566, 2.3522), **near)) == ["p"]

    for cleared in ((None, None), (0.0, 0.0)):
        place.place = cleared
        place.save()
        assert _geo_row(admin, pg, place.db_key.redis_key) == (None, None, None)
        assert GeoPlace.query.get(name="p").place == cleared
        assert GeoPlace.query.count(place=(48.8566, 2.3522), **near) == 0

    place.place = ROME
    place.save()
    place.delete()
    assert GeoPlace.query.count(place=ROME, **near) == 0


def test_a_point_geoadd_refuses_is_refused_before_writing(pg):
    """Redis writes the hash and then fails the GEOADD, leaving an unindexed
    record; Postgres refuses first (same text, ValueError)."""
    with pytest.raises(
        ValueError, match="invalid longitude,latitude pair 10.000000,86.000000"
    ):
        GeoPlace.create(name="north", place=(86.0, 10.0))
    assert GeoPlace.query.get(name="north") is None


def test_a_partial_save_writes_the_geo_columns_only_with_the_field(pg, admin):
    place = GeoPlace.create(name="p", bucket="a", place=ROME)
    before = _geo_row(admin, pg, place.db_key.redis_key)
    place.bucket = "b"
    place.place = VATICAN
    place.save(update_fields=["bucket"])
    assert _geo_row(admin, pg, place.db_key.redis_key) == before
    place.save(update_fields=["place"])
    assert _geo_row(admin, pg, place.db_key.redis_key)[0] == geo.score_of(
        VATICAN[1], VATICAN[0]
    )


# -- searches -------------------------------------------------------------------


def test_by_member_searches_around_the_stored_position(pg):
    rome = GeoPlace.create(name="rome", place=ROME)
    GeoPlace.create(name="vatican", place=VATICAN)
    rows = GeoPlace.query.filter(
        place_member=rome,
        place_radius=5,
        place_radius_unit="km",
        place_with_distances=True,
    )
    # GEORADIUSBYMEMBER … WITHDIST on Redis 8.10.2: both ends quantised, so
    # 3.4623 km where the search from Vatican's coordinates as given says
    # 3.4621.
    assert [(r.name, r._geo_distance) for r in rows] == [
        ("rome", 0.0),
        ("vatican", 3.4623),
    ]


def test_by_member_of_an_unindexed_record_and_of_an_empty_index(pg):
    nowhere = GeoPlace.create(name="nowhere", place=(None, None))
    # Nothing is indexed under the field: GEORADIUSBYMEMBER on a missing key
    # replies empty without decoding the member.
    assert list(GeoPlace.query.filter(place_member=nowhere, place_radius=1)) == []
    GeoPlace.create(name="rome", place=ROME)
    with pytest.raises(QueryException, match="could not decode requested zset member"):
        list(GeoPlace.query.filter(place_member=nowhere, place_radius=1))


def test_order_by_then_distance_then_limit(pg):
    for name, point in (("a", VATICAN), ("b", ROME), ("c", ROME)):
        GeoPlace.create(name=name, place=point)
    common = dict(
        place=ROME, place_radius=10, place_radius_unit="km", place_with_distances=True
    )
    rows = GeoPlace.query.filter(**common)
    assert [r.name for r in rows] == ["b", "c", "a"]  # by distance, then key
    rows = GeoPlace.query.filter(order_by="-name", limit=2, **common)
    assert [(r.name, r._geo_distance) for r in rows] == [("c", 0.0), ("b", 0.0)]
    # A projection carries no distance, as a values= dict never did.
    assert GeoPlace.query.filter(values=("name",), **common) == [
        {"name": "a"},
        {"name": "b"},
        {"name": "c"},
    ]


def test_a_geo_leaf_inside_or_and_not(pg):
    GeoPlace.create(name="rome", bucket="x", place=ROME)
    GeoPlace.create(name="paris", bucket="x", place=(48.8566, 2.3522))
    GeoPlace.create(name="far", bucket="y", place=(10.0, 10.0))
    geo_q = Q(
        place=ROME, place_radius=10, place_radius_unit="km", place_with_distances=True
    )
    rows = GeoPlace.query.filter(geo_q | Q(name="far"))
    assert [(r.name, getattr(r, "_geo_distance", None)) for r in rows] == [
        ("rome", 0.0),
        ("far", None),  # matched by the other branch: no distance, last
    ]
    assert _names(GeoPlace.query.filter(Q(bucket="x") & ~geo_q)) == ["paris"]


def test_sibling_filters_scope_the_candidates(pg, monkeypatch):
    for i in range(5):
        GeoPlace.create(name=f"n{i}", bucket="a" if i < 2 else "b", place=ROME)
    seen = []
    original = geo.distance

    def spy(*args):
        seen.append(args)
        return original(*args)

    monkeypatch.setattr(geo, "distance", spy)
    rows = GeoPlace.query.filter(bucket="a", place=ROME, place_radius=1)
    assert _names(rows) == ["n0", "n1"]
    assert len(seen) == 2, "only bucket a's rows are measured"


def test_a_geo_leaf_cannot_scope_a_ranking_yet(pg):
    plan = plan_from_call(
        QueryCall(query=GeoPlace.query, kind="filter", kwargs={"place": ROME})
    )
    ts = pg._table(GeoPlace._meta.spec)
    with pytest.raises(BackendCapabilityError, match="scopes filter\\(\\) and count"):
        render_where(ts, ts.field_kinds, plan.where)


def test_with_distances_is_a_computed_column_on_the_plan():
    plan = plan_from_call(
        QueryCall(
            query=GeoPlace.query,
            kind="filter",
            kwargs={"place": ROME, "place_with_distances": True},
        )
    )
    (col,) = plan.compute
    assert col.name == "_geo_distance" and col.expr.with_distances


# -- Meta.ttl, batch, async -----------------------------------------------------


def test_an_expired_record_is_out_of_every_search(pg):
    with frozen_clock(T0):
        rome = GeoTtlPlace.create(name="rome", place=ROME)
        GeoTtlPlace.create(name="vatican", place=VATICAN)
    near = dict(place=ROME, place_radius=10, place_radius_unit="km")
    with frozen_clock(T0 + 30):
        assert GeoTtlPlace.query.count(**near) == 2
    with frozen_clock(T0 + 61):
        assert GeoTtlPlace.query.count(**near) == 0
        assert list(GeoTtlPlace.query.filter(**near)) == []
        # Around the expired member itself: no live row is left to return.
        assert list(GeoTtlPlace.query.filter(place_member=rome, place_radius=1)) == []


EXPIRED_CENTER = dict(place_radius=10, place_radius_unit="km")


def _around(member):
    rows = GeoTtlPlace.query.filter(
        place_member=member, place_with_distances=True, **EXPIRED_CENTER
    )
    return [(r.name, r._geo_distance) for r in rows]


def test_a_search_around_an_expired_member_on_redis():
    """The Redis leg of the next test, pinned. ``EXPIRE`` drops the hash; the
    geo-set member stays (no read purges a ``GeoField`` index), so
    ``GEORADIUSBYMEMBER`` decodes it and searches around its last position.
    ``filter()`` drops the expired rows at hydration; ``count()`` counts the
    reply, stale members included."""
    from popoto.backends import set_backend

    previous = set_backend("redis")
    client = popoto.get_redis()
    try:
        rome = GeoTtlPlace.create(name="rome", place=ROME)
        vatican = GeoTtlPlace.create(name="vatican", place=VATICAN)
        GeoTtlPlace.create(name="far", place=(10.0, 10.0))
        client.pexpireat(rome.db_key.redis_key, 1)  # expired
        assert _around(rome) == [("vatican", 3.4623)]
        assert GeoTtlPlace.query.count(place_member=rome, **EXPIRED_CENTER) == 2
        client.pexpireat(vatican.db_key.redis_key, 1)
        assert _around(rome) == []
        assert GeoTtlPlace.query.count(place_member=rome, **EXPIRED_CENTER) == 2
        # A member that was never saved: the key exists (stale members), the
        # member does not.
        with pytest.raises(
            ResponseError, match="^could not decode requested zset member$"
        ):
            _around(GeoTtlPlace(name="ghost"))
    finally:
        set_backend(previous)


def test_a_search_around_an_expired_member_runs_around_its_last_position(pg):
    """#791: as on Redis (above), until the reaper deletes the row. The
    center and the "is anything indexed" check read the row whether or not
    it has expired, the candidates are live rows only -- what Redis's reply
    is after ``filter()`` hydrates it. ``count()`` counts live rows (the
    documented ``Meta.ttl`` divergence), and a refusal is ``QueryException``
    with Redis's text. Once the reaper has deleted the row, the member is
    gone, as on Redis after ``clean_indexes`` or a delete."""
    from popoto.backends.postgres.ttl import reap

    with frozen_clock(T0):
        rome = GeoTtlPlace.create(name="rome", place=ROME)
    with frozen_clock(T0 + 30):
        vatican = GeoTtlPlace.create(name="vatican", place=VATICAN)
        GeoTtlPlace.create(name="far", place=(10.0, 10.0))
    with frozen_clock(T0 + 61):
        assert _around(rome) == [("vatican", 3.4623)]
        assert GeoTtlPlace.query.count(place_member=rome, **EXPIRED_CENTER) == 1
    with frozen_clock(T0 + 200):
        assert _around(rome) == []
        assert _around(vatican) == []
        assert GeoTtlPlace.query.count(place_member=rome, **EXPIRED_CENTER) == 0
        with pytest.raises(
            QueryException, match="^could not decode requested zset member$"
        ):
            _around(GeoTtlPlace(name="ghost"))
    with frozen_clock(T0 + 61):
        ts = pg._table(GeoTtlPlace._meta.spec)
        assert reap(pg, ts, force=True) == [rome.db_key.redis_key]
        with pytest.raises(
            QueryException, match="^could not decode requested zset member$"
        ):
            _around(rome)
        assert _around(vatican) == [("vatican", 0.0)]


def test_a_batch_commits_the_geo_columns_with_the_row(pg):
    pipe = popoto.batch()
    GeoPlace(name="rome", place=ROME).save(pipeline=pipe)
    assert GeoPlace.query.count(place=ROME, place_radius=1) == 0
    pipe.execute()
    assert GeoPlace.query.count(place=ROME, place_radius=1) == 1
    pipe = popoto.batch()
    GeoPlace(name="vatican", place=VATICAN).save(pipeline=pipe)
    pipe.reset()
    assert GeoPlace.query.count(place=VATICAN, place_radius=1) == 0


def test_async_save_and_search(pg):
    async def run():
        await GeoPlace(name="rome", place=ROME).async_save()
        await GeoPlace(name="vatican", place=VATICAN).async_save()
        rows = await GeoPlace.query.async_filter(
            place=VATICAN,
            place_radius=5,
            place_radius_unit="km",
            place_with_distances=True,
        )
        return [(r.name, r._geo_distance, r._geo_distance_unit) for r in rows]

    vatican, rome = asyncio.run(run())
    # From Vatican's coordinates as given to its own quantised cell: under a
    # metre. To Rome: GEORADIUS … WITHDIST's 3.4621 km.
    assert vatican[0] == "vatican" and vatican[1] < 0.001
    assert rome == ("rome", 3.4621, "km")


# -- the seeded probe (slice) ---------------------------------------------------


def _probe():
    spec = importlib.util.spec_from_file_location("probe_geo_parity", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not PROBE.exists(), reason="scripts/probe_geo_parity.py")
def test_a_slice_of_the_seeded_geo_probe_has_no_undocumented_mismatch(pg):
    probe = _probe()
    report = probe.run(pg, seeds=[11], shapes=40)
    assert report["shapes"] == 40
    assert report["undocumented"] == [], report["undocumented"][:5]


def _another_libm(monkeypatch, probe):
    """Give the probe's reference model a libm that is off by one ulp on
    about one argument in sixteen: what a Redis on another libm looks like
    from here."""
    import struct
    import types

    def skewed(fn):
        def call(x):
            y = fn(x)
            bits = struct.unpack("<q", struct.pack("<d", x))[0]
            return math.nextafter(y, math.inf) if bits % 16 == 0 else y

        return call

    shim = types.SimpleNamespace(**{n: getattr(math, n) for n in dir(math)})
    for name in ("sin", "cos", "asin"):
        setattr(shim, name, skewed(getattr(math, name)))
    monkeypatch.setattr(probe, "math", shim)


@pytest.mark.skipif(not PROBE.exists(), reason="scripts/probe_geo_parity.py")
def test_the_probe_calibration_tells_another_libm_apart(monkeypatch):
    """#791: the ``libm_*`` classes are on only when the running Redis's
    distances need another libm. Against a model whose libm is skewed, the
    battery finds distances neither model reproduces."""
    probe = _probe()
    _another_libm(monkeypatch, probe)
    calibration = probe.Calibration(popoto.get_redis(), pairs=400)
    assert calibration.pairs > 300
    assert calibration.distances["other"] > 0, calibration.summary()
    assert calibration.libm_other
    assert popoto.get_redis().keys("ProbeGeoCal:*") == []


@pytest.mark.skipif(not PROBE.exists(), reason="scripts/probe_geo_parity.py")
def test_the_probe_classifies_each_differing_member():
    """#791: a search or count that differs is documented only when every
    member it differs by is a boundary case *and* the Postgres leg answers
    that member as the reference plain model does -- never because the gap
    is small."""
    probe = _probe().Probe(None, fuses=True, libm_other=True)
    edge = {
        "fma": {"a"},
        "libm": {"c"},
        "plain": {"a": True, "b": True, "c": False, "d": True},
    }
    assert probe.boundary({"b"}, {"a", "b"}, edge) == ("fma_boundary", {"a"})
    # Postgres leaves out a member the reference puts in: a port bug.
    assert probe.boundary({"a", "b"}, {"b"}, edge)[0] is None
    assert probe.boundary({"c"}, set(), edge) == ("libm_boundary", {"c"})
    assert probe.boundary({"c"}, {"a"}, edge)[0] == "libm_boundary"
    # A member outside every boundary set is never explained.
    assert probe.boundary({"d"}, set(), edge) == (None, set())
    # Under ~Q(...) the rows are the complement: present means outside.
    assert probe.boundary({"a"}, set(), edge, negated=True) == ("fma_boundary", {"a"})
    assert probe.boundary(set(), {"a"}, edge, negated=True)[0] is None


@pytest.mark.skipif(not PROBE.exists(), reason="scripts/probe_geo_parity.py")
@pytest.mark.parametrize("bug", ["radius", "boundary"])
def test_the_probe_flags_a_planted_bug_in_search_and_count(pg, monkeypatch, bug):
    """#791: the #789 review's planted bugs -- the earth radius wrong in its
    9th digit, ``>=`` for ``>`` at the radius -- reach the search and count
    stage as undocumented mismatches, not only the exact-distance check.
    They are planted through the port's ``distance``: scaled by the wrong
    radius, or one ulp long, which moves a member exactly at the radius out
    as ``>=`` does."""
    probe = _probe()
    original = geo.distance
    if bug == "radius":
        scale = 6372797.550856 / geo.EARTH_RADIUS_IN_METERS

        def planted(*args):
            return original(*args) * scale

    else:

        def planted(*args):
            return math.nextafter(original(*args), math.inf)

    monkeypatch.setattr(geo, "distance", planted)
    report = probe.run(pg, seeds=[11], shapes=40)
    stages = {line.split(":", 1)[0] for line in report["undocumented"]}
    assert {"filter", "count"} <= stages, stages
