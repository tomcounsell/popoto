#!/usr/bin/env python
"""Seeded two-leg probe: Redis (``GEOADD``/``GEORADIUS``, the oracle) vs
Postgres (``popoto.backends.postgres.geo``), ``GeoField`` (#759 M5).

Each *shape* saves a few records of one model with a ``GeoField`` -- points
drawn uniformly, clustered, on and just past the latitude limits
(±85.05112878), on and next to the antimeridian, duplicated, at a zero
latitude or longitude (which the index leaves out), ``None``, as strings and
as ints -- then moves, clears and deletes some, and runs radius searches
around a stored point, near one, around a member, at random, and at the
extremes, with radii drawn log-uniformly over ten decades, ``0``, ``inf``,
*exactly* the distance to a member and one ulp either side of it, and the
argument errors the server refuses; in all four units, with and without
distances, ordered or not, limited or not.

=================  ==========================================================
class              what is compared
=================  ==========================================================
``save``           each save's outcome (value, or exception text)
``score``          each indexed record's stored score: ``ZSCORE`` vs
                   ``<f>__geohash``
``position``       its decoded position: ``GEOPOS`` vs ``<f>__geolon`` /
                   ``<f>__geolat``, to the bit
``distance_bits``  the exact distance each leg measures, read off its own
                   radius test: a member is inside at radius ``d`` and
                   outside at the double just below ``d`` iff that leg's
                   distance is ``d`` (``unit=m``, so ``radius * conversion =
                   d``). For Redis, ``d`` is the port's haversine from
                   Redis's own ``GEOPOS``; for Postgres, from its stored
                   position.
``filter``         ``filter(shape=…, <geo params>)``: which records, their
                   ``_geo_distance`` / ``_geo_distance_unit``, and the order
                   where the order is defined (by ``order_by``; by distance
                   with ``with_distances``, ties compared as a set)
``count``          ``query.count`` with the same parameters
``error``          a search both legs refuse: the same message
=================  ==========================================================

Documented mismatch classes are counted apart (``documented``) and listed in
``docs/features/postgres-backend.md``; any other mismatch is printed with its
seed, shape and inputs, and the run exits non-zero:

* ``error_class`` -- a refused search raises Redis's ``ResponseError`` on
  Redis and ``QueryException`` with the same text on Postgres.
* ``save_invalid_pair`` -- a point ``GEOADD`` refuses: Redis has written the
  hash before the ``GEOADD`` fails (the record exists, unindexed); Postgres
  refuses before writing (``ValueError``, same text). The probe deletes the
  half-written Redis record so later reads compare like with like.
* ``fma_position_ulp`` -- a decoded position (``GEOPOS``), or the distance
  Redis's radius test measures, that differs from the port's by one fused
  rounding. The port is plain IEEE arithmetic, which is what a Redis built
  without floating-point contraction computes (x86-64, CI's
  ``redis:7-alpine``): there this class is 0. A clang/arm64 build fuses the
  decode's ``min + t * scale`` and the haversine's ``u*u + c`` into one
  rounding each.
* ``fma_boundary`` -- a radius search or count that differs only by members
  that the contraction alone moves across the radius. Also 0 against a build
  without contraction.
* ``libm_ulp`` -- a distance Redis's radius test measures that differs from
  the port's though the inputs are identical, and lies within the envelope
  the same formula gives when each of its five libm results (``sin``,
  ``cos``, ``asin``) is off by at most one ulp. The running Redis's libm is
  not this process's: CI's ``redis:7-alpine`` links musl, CI's Python glibc,
  and they disagree in the last bit now and then. 0 where both share a libm
  (a local Redis and Python on one macOS).
* ``libm_boundary`` -- a search or count that differs only by members whose
  libm envelope straddles the radius.

The ``fma_*`` classes are not a tolerance. A mismatch joins one only when a
model of the same C *with* the two contractions (``_fused_decode`` /
``_fused_distance``, below; the probe's, not the backend's) reproduces the
running Redis to the bit. The ``libm_*`` classes are a bound, not a model --
nothing portable reproduces another libm's last bit -- but a tight one:
one ulp per libm call, propagated through the formula, never a free ulp
count on the result. Positions call no libm and are never in them. Anything
else is undocumented. "One ulp" is the
rounding, not always the gap: one rounding of the decode's product (the
magnitude of the range, up to 180) is several ulps of a latitude near zero,
and the report prints the widest gap seen.

Safety: Redis is bound from ``REDIS_URL`` *before* importing popoto and
database 0 is refused (CLAUDE.md, #577); on Postgres the script creates its
own ``popoto_test_<uuid4().hex>`` schema and drops only that.

Usage::

    REDIS_URL=redis://localhost:6379/10 \\
    POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \\
        python scripts/probe_geo_parity.py --seeds 1 2 3 --shapes 500
"""

from __future__ import annotations

import argparse
import itertools
import math
import os
import random
import struct
import sys
import time
import uuid
from collections import Counter
from typing import Any, Callable

if __name__ == "__main__":
    _url = os.environ.get("REDIS_URL", "")
    if not _url or _url.rstrip("/").endswith("/0") or _url.count("/") < 3:
        sys.exit("set REDIS_URL=redis://localhost:6379/<n> (n != 0) before running")
    if not os.environ.get("POPOTO_POSTGRES_URL"):
        sys.exit("set POPOTO_POSTGRES_URL=postgresql://host:port/db")

import popoto  # noqa: E402
from popoto.backends import set_backend  # noqa: E402
from popoto.backends.postgres import geo  # noqa: E402
from popoto.fields.geo_field import GeoField  # noqa: E402
from popoto.models.q import Q  # noqa: E402


class ProbeGeo(popoto.Model):
    shape = popoto.KeyField()
    name = popoto.KeyField()
    place = popoto.GeoField()


LAT = geo.GEO_LAT_MAX
UNITS = ("m", "km", "ft", "mi")


def _outcome(fn: Callable[[], Any]) -> Any:
    try:
        return ("ok", fn())
    except Exception as exc:  # noqa: BLE001 - compared across legs
        return ("!!", type(exc).__name__, _message(exc))


def _message(exc: BaseException) -> str:
    text = str(exc)
    # A pipelined GEOADD's error names the command; the server's text follows.
    marker = "of pipeline caused error: "
    return text.split(marker, 1)[1] if marker in text else text


class Shape:
    def __init__(self, seed: int, index: int, rng: random.Random) -> None:
        self.seed = seed
        self.index = index
        self.key = f"g{seed}x{index}"
        self.rng = rng
        self.points: dict[str, Any] = {}  # name -> value as last saved (ok)
        self.history: list[str] = []


class Probe:
    def __init__(self, pg: Any, *, verbose: bool = False) -> None:
        self.pg = pg
        self.verbose = verbose
        self.checks: Counter = Counter()
        self.mismatches: Counter = Counter()
        self.documented: Counter = Counter()
        self.examples: dict[str, list[str]] = {}
        self.undocumented: list[str] = []
        self.shapes = 0
        self.edge_queries = 0
        self.q_queries = 0
        # distance_bits checks whose member no scanned box holds (a score
        # past 2**52): Redis's own behaviour, reproduced, not a divergence.
        self.uncovered = 0
        # The widest fma_position_ulp gap seen, in ulps of the value.
        self.spread: Counter = Counter()

    # -- plumbing -------------------------------------------------------------

    def leg(self, name: str) -> None:
        set_backend("redis" if name == "redis" else self.pg)

    def wipe(self) -> None:
        self.leg("redis")
        client = popoto.get_redis()
        for key in client.scan_iter(match="*ProbeGeo*", count=1000):
            client.delete(key)
        self.leg("postgres")
        table = self.pg._table(ProbeGeo._meta.spec)
        self.pg._run(f"TRUNCATE {table.qualified}")

    def miss(self, cls: str, detail: str) -> None:
        self.mismatches[cls] += 1
        self.undocumented.append(f"{cls}: {detail}")
        bucket = self.examples.setdefault(cls, [])
        if len(bucket) < 5:
            bucket.append(detail)
        if self.verbose:
            print(f"MISMATCH {cls}: {detail}", file=sys.stderr)

    def check(self, cls: str, ok: bool, detail: Callable[[], str]) -> None:
        self.checks[cls] += 1
        if not ok:
            self.miss(cls, detail())

    def both(self, fn: Callable[[str], Any]) -> dict[str, Any]:
        out = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            out[leg] = _outcome(lambda: fn(leg))
        return out

    def where(self, shape: Shape) -> str:
        return f"seed={shape.seed} shape={shape.index} [{'; '.join(shape.history)}]"

    # -- points -----------------------------------------------------------------

    def point(self, shape: Shape) -> Any:
        rng = shape.rng
        roll = rng.random()
        if roll < 0.25:
            return (rng.uniform(-LAT, LAT), rng.uniform(-180, 180))
        if roll < 0.45 and shape.points:
            base = self._some_point(shape)
            if base is not None:
                lat, lon = base
                jitter = 10 ** rng.uniform(-7, 0)
                return (
                    max(-LAT, min(LAT, lat + rng.uniform(-jitter, jitter))),
                    max(-180.0, min(180.0, lon + rng.uniform(-jitter, jitter))),
                )
        if roll < 0.55:
            lat = rng.choice(
                [LAT, -LAT, math.nextafter(LAT, 0), math.nextafter(-LAT, 0), 85.0]
            )
            return (lat * rng.choice([1, 1, -1]), rng.uniform(-180, 180))
        if roll < 0.65:
            lon = rng.choice(
                [180.0, -180.0, math.nextafter(180.0, 0), math.nextafter(-180.0, 0)]
            )
            return (rng.uniform(-80, 80), lon)
        if roll < 0.72 and shape.points:
            base = self._some_point(shape)
            if base is not None:
                return base  # a duplicate
        if roll < 0.76:
            # (0, 0) saves unindexed; one zero coordinate fails validation.
            return rng.choice(
                [(0.0, 0.0), (0.0, rng.uniform(-9, 9)), (rng.uniform(-9, 9), 0.0)]
            )
        if roll < 0.79:
            return (None, None)
        if roll < 0.84:
            return (repr(rng.uniform(-80, 80)), repr(rng.uniform(-179, 179)))
        if roll < 0.88:
            return (rng.randrange(-85, 86) or 1, rng.randrange(-180, 181) or 1)
        if roll < 0.92:
            # Past what GEOADD accepts.
            return rng.choice(
                [
                    (math.nextafter(LAT, 90), 10.0),
                    (-86.0, 10.0),
                    (10.0, math.nextafter(180.0, 200)),
                    (89.9, -181.0),
                ]
            )
        return (rng.uniform(-60, 60), rng.uniform(-60, 60))

    def _some_point(self, shape: Shape) -> Any:
        values = [v for v in shape.points.values() if v and v[0] and v[1]]
        if not values:
            return None
        value = shape.rng.choice(values)
        return (float(value[0]), float(value[1]))

    # -- writes -----------------------------------------------------------------

    def save(self, shape: Shape, name: str, value: Any) -> None:
        shape.history.append(f"save {name}={value!r}")

        def run(leg: str) -> Any:
            obj = ProbeGeo(shape=shape.key, name=name)
            obj.place = value
            obj.save()
            return "saved"

        got = self.both(run)
        r, p = got["redis"], got["postgres"]
        if r[0] == "!!" and p[0] == "!!" and r[2] == p[2] and "invalid" in r[2]:
            # Redis wrote the hash, then GEOADD refused the point.
            self.documented["save_invalid_pair"] += 1
            self.leg("redis")
            stale = ProbeGeo.query.get(shape=shape.key, name=name)
            if stale is not None:
                stale.delete()
            shape.points.pop(name, None)
            self.leg("postgres")
            survivor = ProbeGeo.query.get(shape=shape.key, name=name)
            if survivor is not None:  # pragma: no cover - a prior value
                survivor.delete()
            return
        self.check("save", r == p, lambda: f"{self.where(shape)}: {got}")
        if r[0] == "ok":
            shape.points[name] = value

    def delete(self, shape: Shape, name: str) -> None:
        shape.history.append(f"delete {name}")

        def run(leg: str) -> Any:
            obj = ProbeGeo.query.get(shape=shape.key, name=name)
            return obj.delete() if obj is not None else None

        got = self.both(run)
        self.check("save", got["redis"] == got["postgres"], lambda: f"{got}")
        shape.points.pop(name, None)

    # -- stored state -------------------------------------------------------------

    def flips(self, shape: Shape, params: dict[str, Any]) -> dict[str, set[str]]:
        """The members an arithmetic difference alone can move across the
        radius (positions from the Postgres leg's stored scores): ``fma``,
        where the port's radius test and the fused model's disagree; ``libm``,
        where the libm envelope of the port's distance straddles the radius.
        Both empty for a search either leg refuses."""
        self.leg("postgres")
        ts = self.pg._table(ProbeGeo._meta.spec)
        rows, _ = self.pg._run(
            f'SELECT "name", "_pk", "place__geohash", "place__geolon", '
            f'"place__geolat" FROM {ts.qualified} WHERE "shape" = %s '
            f'AND "place__geohash" IS NOT NULL',
            [shape.key],
        )
        try:
            radius = geo.parse_radius(params["place_radius"])
            conversion = geo.UNIT_TO_METERS[params["place_radius_unit"]]
            if "place_member" in params:
                pk = ProbeGeo(
                    shape=shape.key, name=params["place_member"].name
                ).db_key.redis_key
                found = [r for r in rows if r[1] == pk]
                if not found:
                    return {"fma": set(), "libm": set()}
                center = (found[0][3], found[0][4])
                fused_center = _fused_decode(found[0][2])
            else:
                lat, lon = params.get("place") or (
                    params["place_latitude"],
                    params["place_longitude"],
                )
                center = fused_center = geo.parse_point(lon, lat)
        except Exception:  # noqa: BLE001 - a refused search has no boundary
            return {"fma": set(), "libm": set()}
        limit = radius * conversion
        out: dict[str, set[str]] = {"fma": set(), "libm": set()}
        for name, _pk, score, mlon, mlat in rows:
            plain = not geo.distance(*center, mlon, mlat) > limit
            fused = not _fused_distance(*fused_center, *_fused_decode(score)) > limit
            if plain != fused:
                out["fma"].add(name)
            lo, hi = _libm_envelope(*center, mlon, mlat)
            if lo <= limit < hi:
                out["libm"].add(name)
        return out

    def compare_stored(self, shape: Shape) -> None:
        self.leg("redis")
        client = popoto.get_redis()
        geo_key = GeoField.get_geo_db_key(ProbeGeo, "place").redis_key
        names = sorted(shape.points)
        keys = [ProbeGeo(shape=shape.key, name=n).db_key.redis_key for n in names]
        if not keys:
            return
        scores = [client.zscore(geo_key, k) for k in keys]
        positions = client.execute_command("GEOPOS", geo_key, *keys)
        self.leg("postgres")
        ts = self.pg._table(ProbeGeo._meta.spec)
        rows, _ = self.pg._run(
            f'SELECT "_pk", "place__geohash", "place__geolon", "place__geolat" '
            f'FROM {ts.qualified} WHERE "_pk" = ANY(%s::text[])',
            [keys],
        )
        by_pk = {row[0]: row[1:] for row in rows}
        for key, score, pos in zip(keys, scores, positions):
            pg = by_pk.get(key, (None, None, None))
            want = None if score is None else int(score)
            self.check(
                "score",
                want == pg[0],
                lambda: f"{self.where(shape)}: {key} redis={score} pg={pg[0]}",
            )
            if pos is None:
                self.check("position", pg[1] is None, lambda: f"{key} pg={pg}")
                continue
            rpos = (float(pos[0]), float(pos[1]))
            exact = rpos == (pg[1], pg[2])
            if not exact and pg[0] is not None and rpos == _fused_decode(pg[0]):
                self.checks["position"] += 1
                self.documented["fma_position_ulp"] += 1
                self.spread["position"] = max(
                    self.spread["position"],
                    _ulps(rpos[0], pg[1]),
                    _ulps(rpos[1], pg[2]),
                )
                continue
            self.check(
                "position",
                exact,
                lambda: f"{self.where(shape)}: {key} redis={rpos!r} pg={pg[1:]!r}",
            )

    def distance_bits(self, shape: Shape, center: tuple[float, float]) -> None:
        """Read each leg's exact distance for one member off its radius test."""
        self.leg("redis")
        client = popoto.get_redis()
        geo_key = GeoField.get_geo_db_key(ProbeGeo, "place").redis_key
        names = [n for n, v in shape.points.items() if v and v[0] and v[1]]
        if not names:
            return
        name = shape.rng.choice(names)
        key = ProbeGeo(shape=shape.key, name=name).db_key.redis_key
        score = client.zscore(geo_key, key)
        if score is None:
            return
        (rpos,) = client.execute_command("GEOPOS", geo_key, key)
        lat, lon = center
        # Redis measures from its own decoded position: the port's haversine
        # from there is Redis's distance, up to the contraction.
        d = geo.distance(lon, lat, float(rpos[0]), float(rpos[1]))
        self.leg("postgres")
        ts = self.pg._table(ProbeGeo._meta.spec)
        rows, _ = self.pg._run(
            f'SELECT "place__geolon", "place__geolat" FROM {ts.qualified} '
            f'WHERE "_pk" = %s',
            [key],
        )
        pd = geo.distance(lon, lat, rows[0][0], rows[0][1])
        if not (d > 0 and math.isfinite(d) and pd > 0 and math.isfinite(pd)):
            return
        self.leg("redis")

        def inside(radius: float) -> bool:
            out = client.execute_command(
                "GEORADIUS", geo_key, repr(lon), repr(lat), repr(radius), "m"
            )
            return key.encode() in out

        def covered(radius: float) -> bool:
            # Whether a box Redis scans at this radius holds the member: a
            # score past 2**52 (the latitude limit, longitude 180) lies past
            # every box and is never found.
            ranges = geo.search_ranges(lon, lat, radius, 1.0)
            return any(lo <= int(score) < hi for lo, hi in ranges)

        below_d = math.nextafter(d, 0)
        at, below = inside(d), inside(below_d)
        want_at = covered(d)
        if not want_at:
            self.uncovered += 1
        redis_exact = (at, below) == (want_at, False)
        fd = _fused_distance(lon, lat, float(rpos[0]), float(rpos[1]))
        if not redis_exact and want_at and fd != d and math.isfinite(fd):
            # Redis's haversine contracts u*u + c: its distance is the fused
            # model's to the bit, or this is undocumented.
            if inside(fd) and not inside(math.nextafter(fd, 0)):
                self.documented["fma_position_ulp"] += 1
                self.spread["distance"] = max(self.spread["distance"], _ulps(d, fd))
                redis_exact = True
        if not redis_exact and want_at:
            # The running Redis's libm is not this process's (CI's
            # redis:7-alpine links musl; CI's Python, glibc). Its distance is
            # in the envelope one-ulp libm results give, or undocumented.
            lo, hi = _libm_envelope(lon, lat, float(rpos[0]), float(rpos[1]))
            if inside(hi) and not inside(math.nextafter(lo, 0)):
                self.documented["libm_ulp"] += 1
                redis_exact = True
        # The Postgres leg's own search, at its own distance: always exact.
        self.leg("postgres")

        def pg_inside(radius: float) -> bool:
            rows = ProbeGeo.query.filter(
                shape=shape.key, place=center, place_radius=radius
            )
            return name in {row.name for row in rows}

        pg_got = (pg_inside(pd), pg_inside(math.nextafter(pd, 0)))
        pg_want = (covered(pd), False)
        self.check(
            "distance_bits",
            redis_exact and pg_got == pg_want,
            lambda: (
                f"{self.where(shape)}: center={center!r} member={key} "
                f"redis_d={d!r} pg_d={pd!r} inside(d)={at} "
                f"inside(d-ulp)={below} covered={want_at} "
                f"pg={pg_got} want_pg={pg_want}"
            ),
        )

    # -- searches -----------------------------------------------------------------

    def search_args(self, shape: Shape) -> tuple[dict[str, Any], str]:
        rng = shape.rng
        params: dict[str, Any] = {}
        unit = rng.choice(UNITS)
        conversion = geo.UNIT_TO_METERS[unit]
        indexed = [n for n, v in shape.points.items() if v and v[0] and v[1]]
        roll = rng.random()
        center: Any = None
        label = ""
        if roll < 0.2 and shape.points:
            name = rng.choice(sorted(shape.points))
            self.leg("redis")
            params["place_member"] = ProbeGeo(shape=shape.key, name=name)
            label = f"member {name}"
        else:
            if roll < 0.45 and indexed:
                center = self._some_point(shape)
                label = "at a point"
            elif roll < 0.65 and indexed:
                lat, lon = self._some_point(shape)
                jitter = 10 ** rng.uniform(-6, 1)
                center = (
                    max(-LAT, min(LAT, lat + rng.uniform(-jitter, jitter))),
                    max(-180.0, min(180.0, lon + rng.uniform(-jitter, jitter))),
                )
                label = "near a point"
            elif roll < 0.75:
                center = (
                    rng.choice([LAT, -LAT, 84.9, -84.9]),
                    rng.choice([180.0, -180.0, 179.9999, -179.9999, 0.5]),
                )
                label = "extreme"
            elif roll < 0.78:
                center = rng.choice([(86.0, 0.0), (10.0, 180.5), ("abc", 1.0)])
                label = "invalid center"
            else:
                center = (rng.uniform(-LAT, LAT), rng.uniform(-180, 180))
                label = "random"
            if rng.random() < 0.5:
                params["place"] = center
            else:
                params["place_latitude"] = center[0]
                params["place_longitude"] = center[1]
        roll = rng.random()
        if roll < 0.25 and indexed and center is not None and label != "invalid center":
            # Exactly at a member's distance, or one ulp either side.
            name = rng.choice(indexed)
            value = shape.points[name]
            score = geo.encode(float(value[1]), float(value[0]))
            mlon, mlat = geo.decode_point(score)
            try:
                lon, lat = geo.parse_point(center[1], center[0])
            except Exception:  # noqa: BLE001
                lon, lat = 0.0, 0.0
            radius = geo.distance(lon, lat, mlon, mlat) / conversion
            radius = rng.choice(
                [radius, math.nextafter(radius, 0), math.nextafter(radius, math.inf)]
            )
            label += f", edge of {name}"
            self.edge_queries += 1
        elif roll < 0.3:
            radius = rng.choice([0, 0.0, math.inf, "5e2", "0x10", 1])
        elif roll < 0.34:
            radius = rng.choice([-1, "abc", math.nan, " 1", "1 ", True])
        else:
            radius = 10 ** rng.uniform(-3, 7.3) / conversion
        params["place_radius"] = radius
        params["place_radius_unit"] = unit
        if rng.random() < 0.6:
            params["place_with_distances"] = True
        return params, label

    def search(self, shape: Shape) -> None:
        params, label = self.search_args(shape)
        rng = shape.rng
        order = rng.choice([None, None, "name", "-name"])
        extra: dict[str, Any] = {}
        if order:
            extra["order_by"] = order
            if rng.random() < 0.4:
                extra["limit"] = rng.randrange(1, 5)
        use_q = None
        if rng.random() < 0.2:
            use_q = (rng.choice(["or", "not"]), f"n{rng.randrange(0, 4)}")
            self.q_queries += 1
        shown = {k: (v.name if k == "place_member" else v) for k, v in params.items()}
        where = f"{self.where(shape)} search({label}) {shown} {extra} q={use_q}"
        with_distances = bool(params.get("place_with_distances"))

        def run(leg: str) -> Any:
            if "place_member" in params:
                params["place_member"] = ProbeGeo(
                    shape=shape.key, name=params["place_member"].name
                )
            if use_q is not None:
                # A geo leaf inside OR / NOT: a record the leaf did not match
                # carries no distance and sorts last.
                other = Q(name=use_q[1])
                geo_q = Q(**params)
                combined = (geo_q | other) if use_q[0] == "or" else (~geo_q)
                rows = ProbeGeo.query.filter(Q(shape=shape.key) & combined, **extra)
            else:
                rows = ProbeGeo.query.filter(shape=shape.key, **params, **extra)
            out = []
            for row in rows:
                out.append(
                    (
                        row.name,
                        getattr(row, "_geo_distance", None),
                        getattr(row, "_geo_distance_unit", None),
                    )
                )
            return out

        got = self.both(run)
        r, p = got["redis"], got["postgres"]
        edge: dict[str, set[str]] | None = None
        if r[0] == "!!" or p[0] == "!!":
            self.compare_errors(r, p, where)
        else:
            rv, pv = r[1], p[1]
            limited = "limit" in extra
            if _same(rv, pv, order, with_distances, limited):
                self.checks["filter"] += 1
            else:
                edge = self.flips(shape, params)
                fma, libm = edge["fma"], edge["fma"] | edge["libm"]
                if fma and _same(rv, pv, order, with_distances, limited, fma):
                    self.checks["filter"] += 1
                    self.documented["fma_boundary"] += 1
                elif libm and _same(rv, pv, order, with_distances, limited, libm):
                    self.checks["filter"] += 1
                    self.documented["libm_boundary"] += 1
                else:
                    self.check(
                        "filter",
                        False,
                        lambda: f"{where}: redis={rv} pg={pv} boundary={edge}",
                    )

        def count(leg: str) -> Any:
            if "place_member" in params:
                params["place_member"] = ProbeGeo(
                    shape=shape.key, name=params["place_member"].name
                )
            return ProbeGeo.query.count(shape=shape.key, **params)

        got = self.both(count)
        r, p = got["redis"], got["postgres"]
        if r[0] == "!!" or p[0] == "!!":
            self.compare_errors(r, p, where + " count")
        elif r == p:
            self.checks["count"] += 1
        else:
            if edge is None:
                edge = self.flips(shape, params)
            gap = abs(r[1] - p[1])
            if edge["fma"] and gap <= len(edge["fma"]):
                self.checks["count"] += 1
                self.documented["fma_boundary"] += 1
            elif gap <= len(edge["fma"] | edge["libm"]):
                self.checks["count"] += 1
                self.documented["libm_boundary"] += 1
            else:
                self.check(
                    "count", False, lambda: f"{where} count: {got} boundary={edge}"
                )

    def compare_errors(self, r: Any, p: Any, where: str) -> None:
        if r[0] == "!!" and p[0] == "!!" and r[2] == p[2]:
            self.checks["error"] += 1
            if r[1] != p[1]:
                self.documented["error_class"] += 1
            return
        self.miss("error", f"{where}: redis={r} pg={p}")

    # -- driving --------------------------------------------------------------

    def run_shape(self, shape: Shape) -> None:
        rng = shape.rng
        for i in range(rng.randrange(1, 9)):
            self.save(shape, f"n{i}", self.point(shape))
        for _ in range(rng.randrange(0, 3)):
            if not shape.points:
                break
            name = rng.choice(sorted(shape.points))
            roll = rng.random()
            if roll < 0.5:
                self.save(shape, name, self.point(shape))
            elif roll < 0.7:
                self.save(shape, name, (None, None))
            else:
                self.delete(shape, name)
        self.compare_stored(shape)
        for _ in range(rng.randrange(3, 8)):
            self.search(shape)
        for _ in range(3):
            center = self._some_point(shape) or (rng.uniform(-80, 80), 0.5)
            roll = rng.random()
            if roll < 0.4:
                center = (rng.uniform(-LAT, LAT), rng.uniform(-180, 180))
            elif roll < 0.7:
                lat, lon = center
                jitter = 10 ** rng.uniform(-6, 0)
                center = (
                    max(-LAT, min(LAT, lat + rng.uniform(-jitter, jitter))),
                    max(-180.0, min(180.0, lon + rng.uniform(-jitter, jitter))),
                )
            elif roll < 0.8:
                # Antipodal-ish: the haversine's a near 1.
                lat, lon = center
                center = (-lat, lon - 180.0 if lon > 0 else lon + 180.0)
            self.distance_bits(shape, center)

    def run_seed(self, seed: int, shapes: int) -> None:
        rng = random.Random(seed)
        for i in range(shapes):
            self.run_shape(Shape(seed, i, random.Random(rng.random())))
            self.shapes += 1

    def report(self) -> str:
        lines = [
            f"shapes: {self.shapes} (radius-edge searches: {self.edge_queries}; "
            f"Q-object searches: {self.q_queries}; distance_bits members no "
            f"box covers, as on Redis: {self.uncovered})",
            "| class | checks | mismatches |",
            "|---|---:|---:|",
        ]
        for cls in sorted(self.checks):
            lines.append(f"| {cls} | {self.checks[cls]} | {self.mismatches[cls]} |")
        lines.append("\ndocumented (counted apart, not failures):")
        for cls in sorted(self.documented):
            lines.append(f"  {cls}: {self.documented[cls]}")
        for what in sorted(self.spread):
            lines.append(
                f"  widest fma_position_ulp gap ({what}): "
                f"{self.spread[what]} ulp of the value"
            )
        for cls, examples in sorted(self.examples.items()):
            lines.append(f"\n{cls} examples:")
            lines.extend(f"  - {e}" for e in examples)
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "shapes": self.shapes,
            "checks": dict(self.checks),
            "documented": dict(self.documented),
            "spread": dict(self.spread),
            "undocumented": list(self.undocumented),
        }


# -- the fused model: Redis's C as a contracting compiler builds it -------------
#
# Classification only. The port (``geo``) is plain IEEE arithmetic; a Redis
# built by clang on arm64 contracts two ``a*b + c`` into one rounding. A
# mismatch is ``fma_*`` only when this model reproduces the running Redis to
# the bit, so nothing else can hide in the class.


def _fma(a: float, b: float, c: float) -> float:
    """``a*b + c`` rounded once (exact rationals; ``int / int`` rounds
    correctly)."""
    an, ad = a.as_integer_ratio()
    bn, bd = b.as_integer_ratio()
    cn, cd = c.as_integer_ratio()
    return (an * bn * cd + cn * ad * bd) / (ad * bd * cd)


def _fused_decode(score: int) -> tuple[float, float]:
    """``geo.decode_point`` with the decode's ``min + t * scale`` fused."""
    sep = geo._deinterleave64(score)
    ilat, ilon = sep & 0xFFFFFFFF, (sep >> 32) & 0xFFFFFFFF
    div = float(1 << geo.GEO_STEP_MAX)
    lat_scale = geo.GEO_LAT_MAX - geo.GEO_LAT_MIN
    lon_scale = geo.GEO_LONG_MAX - geo.GEO_LONG_MIN
    lat = (
        _fma(ilat * 1.0 / div, lat_scale, geo.GEO_LAT_MIN)
        + _fma((ilat + 1) * 1.0 / div, lat_scale, geo.GEO_LAT_MIN)
    ) / 2
    lon = (
        _fma(ilon * 1.0 / div, lon_scale, geo.GEO_LONG_MIN)
        + _fma((ilon + 1) * 1.0 / div, lon_scale, geo.GEO_LONG_MIN)
    ) / 2
    return (
        min(max(lon, geo.GEO_LONG_MIN), geo.GEO_LONG_MAX),
        min(max(lat, geo.GEO_LAT_MIN), geo.GEO_LAT_MAX),
    )


def _haversine(
    lon1d: float,
    lat1d: float,
    lon2d: float,
    lat2d: float,
    *,
    fused: bool = False,
    nudge: tuple[int, ...] = (0, 0, 0, 0, 0),
) -> float:
    """``geo.distance``, optionally with ``u*u + c`` fused, and with each libm
    result (``sin`` v, ``sin`` u, the two ``cos``, ``asin``) moved by
    ``nudge[i]`` ulps. ``sqrt`` is IEEE, correctly rounded everywhere."""

    def off(x: float, n: int) -> float:
        for _ in range(abs(n)):
            x = math.nextafter(x, math.inf if n > 0 else -math.inf)
        return x

    lon1r, lon2r = lon1d * geo.D_R, lon2d * geo.D_R
    v = math.sin((lon2r - lon1r) / 2)
    if v == 0.0:
        return geo.EARTH_RADIUS_IN_METERS * abs(lat2d * geo.D_R - lat1d * geo.D_R)
    v = off(v, nudge[0])
    lat1r, lat2r = lat1d * geo.D_R, lat2d * geo.D_R
    u = off(math.sin((lat2r - lat1r) / 2), nudge[1])
    c = off(math.cos(lat1r), nudge[2]) * off(math.cos(lat2r), nudge[3]) * v * v
    a = _fma(u, u, c) if fused else u * u + c
    root = math.sqrt(a)
    if root > 1.0:
        return math.nan
    return 2.0 * geo.EARTH_RADIUS_IN_METERS * off(math.asin(root), nudge[4])


def _fused_distance(lon1d: float, lat1d: float, lon2d: float, lat2d: float) -> float:
    """``geo.distance`` with the haversine's ``u*u + c`` fused."""
    return _haversine(lon1d, lat1d, lon2d, lat2d, fused=True)


_NUDGES = list(itertools.product((-1, 0, 1), repeat=5))


def _libm_envelope(
    lon1d: float, lat1d: float, lon2d: float, lat2d: float
) -> tuple[float, float]:
    """The least and greatest distance the port's formula gives when each of
    its five libm results is off by at most one ulp -- the accuracy every
    mainstream libm (glibc, musl, Apple's) meets, without agreeing on the
    last bit. A Redis on another libm measures a distance in this range."""
    values = [
        x
        for n in _NUDGES
        if (x := _haversine(lon1d, lat1d, lon2d, lat2d, nudge=n)) == x
    ]
    if not values:
        return math.nan, math.nan
    return min(values), max(values)


def _ulps(a: float, b: float) -> int:
    """How many doubles apart ``a`` and ``b`` are (same sign assumed)."""
    if a == b:
        return 0
    ia = int.from_bytes(struct.pack("<d", abs(a)), "little")
    ib = int.from_bytes(struct.pack("<d", abs(b)), "little")
    return abs(ia - ib) if (a < 0) == (b < 0) else ia + ib


def _same(
    rv: list,
    pv: list,
    order: Any,
    with_distances: bool,
    limited: bool,
    drop: Any = frozenset(),
) -> bool:
    """The two legs' rows agree, ignoring the names in ``drop`` (and, with a
    ``limit``, the rows a dropped name pushed past it)."""
    if drop:
        rv = [row for row in rv if row[0] not in drop]
        pv = [row for row in pv if row[0] not in drop]
        if limited:
            n = min(len(rv), len(pv))
            rv, pv = rv[:n], pv[:n]
    if order:
        return rv == pv
    if with_distances:
        same = sorted(rv, key=_by_distance) == sorted(pv, key=_by_distance)
        return same and _ascending(rv) and _ascending(pv)
    return sorted(rv) == sorted(pv)


def _by_distance(item: Any) -> Any:
    return (item[1] if item[1] is not None else math.inf, item[0])


def _ascending(rows: list) -> bool:
    values = [r[1] if r[1] is not None else math.inf for r in rows]
    return all(a <= b for a, b in zip(values, values[1:]))


def run(pg: Any, seeds: list[int], shapes: int, *, verbose: bool = False) -> dict:
    """Run ``shapes`` shapes per seed; returns :meth:`Probe.as_dict` plus the
    printable ``report``."""
    probe = Probe(pg, verbose=verbose)
    probe.wipe()
    try:
        for seed in seeds:
            probe.run_seed(seed, shapes)
    finally:
        probe.wipe()
        set_backend(None)
    out = probe.as_dict()
    out["report"] = probe.report()
    return out


def main() -> None:
    import psycopg

    from popoto.backends.postgres import PostgresBackend

    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--shapes", type=int, default=500)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    url = os.environ["POPOTO_POSTGRES_URL"]
    schema = f"popoto_test_{uuid.uuid4().hex}"
    admin = psycopg.connect(url, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    pg = PostgresBackend(dsn=url, schema=schema)
    started = time.perf_counter()
    try:
        result = run(pg, args.seeds, args.shapes, verbose=args.verbose)
    finally:
        pg.close()
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
    print(f"seeds {args.seeds}, {time.perf_counter() - started:.1f}s")
    print(result["report"])
    sys.exit(1 if result["undocumented"] else 0)


if __name__ == "__main__":
    main()
