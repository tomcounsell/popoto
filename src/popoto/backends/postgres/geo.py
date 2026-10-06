"""``GeoField`` on Postgres without PostGIS (#759 M5).

Redis keeps a ``GeoField`` in a sorted set whose scores are 52-bit geohashes
(``GEOADD``), and answers a radius search (``GEORADIUS`` /
``GEORADIUSBYMEMBER``) by scanning up to nine geohash boxes around the center
and keeping the members whose haversine distance is within the radius. This
module reproduces that arithmetic operation for operation, from Redis's own C
(``src/geohash.c``, ``src/geohash_helper.c``, ``src/geo.c``; read at tag
8.10.2), so that the same points, searched the same way, give the same keys
and the same distances to the bit as a Redis built without floating-point
contraction (see "Arithmetic" below).

**Storage.** Beside the field's own ``jsonb`` value (the coordinates exactly
as given, which is what a read returns on both backends), a ``GeoField``
``f`` gets three auxiliary columns:

* ``f__geohash bigint`` -- the member's score, ``geohashEncodeWGS84`` at step
  26 and ``geohashAlign52Bits``: the very integer Redis stores as the zset
  score. ``NULL`` means "not in the geo index", which is where Redis's
  ``on_save`` ``ZREM``s the member (no value, or a zero/``None`` latitude or
  longitude) and where a deleted record leaves it. A B-tree on it serves the
  box scan: each geohash box is one ``[min, max)`` score range, exactly
  ``ZRANGEBYSCORE``'s, so the prefilter is Redis's own and needs no GiST.
* ``f__geolon`` / ``f__geolat double precision`` -- the member's position
  *decoded from the score* (``geohashDecodeToLongLatWGS84``): the cell's
  center, i.e. what ``GEOPOS`` returns. Redis measures distance from this
  quantised position, never from the coordinates as given, so the columns
  hold it, computed once at save.

**Search.** The geohash boxes (``geohashCalculateAreasByShapeWGS84``, with
its step estimate, its "decrease the step" check and its pruning of useless
neighbours) are computed here; one statement fetches the rows in those score
ranges (live rows only, on a ``Meta.ttl`` model); each candidate's distance
is ``geohashGetDistance`` and the radius test is ``geohashGetDistanceIfInRadius``
(``distance > radius * conversion`` excludes). The matched keys then scope
the query's own statement as ``_pk = ANY(…)``, and the distances order it.

**Arithmetic: plain IEEE doubles, never fused.** Every ``a*b + c`` here is
two roundings, as CPython evaluates it. That is deliberate: a Postgres
deployment has no Redis to mirror, so the port must not depend on how a
compiler built Redis, and the only portable reading of its C is the one with
no contraction. Encode and decode use no libm, so scores and decoded
positions are bit-identical, on every platform, to a Redis built without
contraction (the x86-64 builds, e.g. ``redis:7-alpine``, which CI runs). A
Redis built with clang on arm64 (e.g. Homebrew 8.10.2) contracts two
expressions into fused multiply-adds -- the decode's
``min + (i / 2**step) * scale`` and the haversine's
``u*u + cos(lat1)*cos(lat2)*v*v`` -- so its decoded positions and distances
are one fused rounding away from this in places (for a near-antipodal
distance, where ``asin``'s slope amplifies it, up to about 0.19 m), and a
member that close to the radius can be in on one and out on the other.

``sin``/``cos``/``asin`` are the host Python's libm, as Redis uses its own.
Libms agree to within an ulp but not on the last bit (CI's musl-linked Redis
against its glibc Python shows it), so a distance can differ by that much,
propagated through the formula, from a Redis on another libm.
``sqrt`` is IEEE, correctly rounded everywhere. ``scripts/probe_geo_parity.py``
classifies both kinds of difference. The distance stays in Python rather
than SQL so that the save's decode and the search's test share one
implementation; nothing in it needs Python beyond that.

**Distances** are reported as Redis replies them: ``WITHDIST`` divides the
meters by the unit's conversion factor and prints four decimals
(``fixedpoint_d2string``: ``llrint(d * 10000)``, ties to even), which redis-py
parses back with ``float``. :func:`reply_distance` is that value.

**What is not reproduced.** ``COUNT``/``ANY``/``DESC``/``WITHCOORD``/
``WITHHASH`` are ``GEOSEARCH`` options the public ``GeoField`` API does not
expose (it sends ``WITHDIST`` and ``ASC`` with ``with_distances``, nothing
otherwise), so there is nothing to reproduce. PostGIS (a ``geography``
column and a GiST index) could replace the box scan later as an
optimisation; it would not change the arithmetic above, and it is not a
dependency.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional, Sequence

from ..types import And, Cond, ModelSpec, Not, Op, Or, Predicate
from .schema import Column, TableSpec, quote_ident

__all__ = [
    "GEO_KIND",
    "GeoResolved",
    "NAN_DISTANCE_REPLY",
    "decode_point",
    "distance",
    "as_score",
    "encode",
    "score_of",
    "geo_columns",
    "geo_indexes",
    "geo_save_values",
    "reply_distance",
    "resolve_geo",
    "search_ranges",
]

GEO_KIND = "GeoField"
HASH_SUFFIX = "__geohash"
LON_SUFFIX = "__geolon"
LAT_SUFFIX = "__geolat"

# -- Redis's constants (geohash.h, geohash_helper.c, geo.c) ---------------------

GEO_STEP_MAX = 26
GEO_LAT_MIN = -85.05112878
GEO_LAT_MAX = 85.05112878
GEO_LONG_MIN = -180.0
GEO_LONG_MAX = 180.0
D_R = math.pi / 180.0
"""``#define D_R (M_PI / 180.0)``: folded to the same double."""
EARTH_RADIUS_IN_METERS = 6372797.560856
MERCATOR_MAX = 20037726.37
UNIT_TO_METERS: dict[str, float] = {"m": 1.0, "km": 1000.0, "ft": 0.3048, "mi": 1609.34}
"""``extractUnitOrReply``."""

_U64 = (1 << 64) - 1


# -- geohash.c ----------------------------------------------------------------

_B = (
    0x5555555555555555,
    0x3333333333333333,
    0x0F0F0F0F0F0F0F0F,
    0x00FF00FF00FF00FF,
    0x0000FFFF0000FFFF,
    0x00000000FFFFFFFF,
)


def _interleave64(xlo: int, ylo: int) -> int:
    """``interleave64``: ``xlo`` (latitude) in the even bits, ``ylo``
    (longitude) in the odd bits."""
    x, y = xlo & 0xFFFFFFFF, ylo & 0xFFFFFFFF
    for shift, mask in ((16, _B[4]), (8, _B[3]), (4, _B[2]), (2, _B[1]), (1, _B[0])):
        x = (x | (x << shift)) & mask
        y = (y | (y << shift)) & mask
    return x | (y << 1)


def _deinterleave64(interleaved: int) -> int:
    """``deinterleave64``: latitude in the low 32 bits, longitude above."""
    x = interleaved & _U64
    y = (interleaved >> 1) & _U64
    for shift, mask in ((0, _B[0]), (1, _B[1]), (2, _B[2]), (4, _B[3]), (8, _B[4])):
        x = (x | (x >> shift)) & mask
        y = (y | (y >> shift)) & mask
    x = (x | (x >> 16)) & _B[5]
    y = (y | (y >> 16)) & _B[5]
    return x | (y << 32)


def encode(longitude: float, latitude: float, step: int = GEO_STEP_MAX) -> int:
    """``geohashEncode`` over the WGS84 ranges. The caller has range-checked
    the point (``extractLongLatOrReply``)."""
    lat_offset = (latitude - GEO_LAT_MIN) / (GEO_LAT_MAX - GEO_LAT_MIN)
    long_offset = (longitude - GEO_LONG_MIN) / (GEO_LONG_MAX - GEO_LONG_MIN)
    lat_offset *= float(1 << step)
    long_offset *= float(1 << step)
    # The doubles convert to uint32_t: truncation toward zero.
    return _interleave64(int(lat_offset), int(long_offset))


def as_score(bits: int) -> int:
    """A 52-bit hash as the zset holds it: a ``double``. A point on the
    latitude limit sets bit 52 and one on longitude 180 bit 53, and past
    ``2**53`` the conversion rounds (to even) -- so ``GEOADD`` of such a
    point stores, and ``GEOPOS`` decodes, a neighbouring cell's score. Range
    bounds go through the same conversion (``geoGetPointsInRange`` takes
    ``double``s), so integer comparisons of rounded values are the zset's
    ``double`` comparisons."""
    return int(float(bits))


def score_of(longitude: float, latitude: float) -> int:
    """The score ``GEOADD`` stores for a point (``geohashEncodeWGS84`` at step
    26, ``geohashAlign52Bits``, then a ``double``)."""
    return as_score(encode(longitude, latitude, GEO_STEP_MAX))


@dataclass(frozen=True)
class _Area:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float


def _decode(bits: int, step: int) -> _Area:
    """``geohashDecode``: ``min + (i / 2**step) * scale`` per edge."""
    sep = _deinterleave64(bits)
    ilato = sep & 0xFFFFFFFF
    ilono = (sep >> 32) & 0xFFFFFFFF
    lat_scale = GEO_LAT_MAX - GEO_LAT_MIN
    long_scale = GEO_LONG_MAX - GEO_LONG_MIN
    div = float(1 << step)
    return _Area(
        lat_min=GEO_LAT_MIN + (ilato * 1.0 / div) * lat_scale,
        lat_max=GEO_LAT_MIN + ((ilato + 1) * 1.0 / div) * lat_scale,
        lon_min=GEO_LONG_MIN + (ilono * 1.0 / div) * long_scale,
        lon_max=GEO_LONG_MIN + ((ilono + 1) * 1.0 / div) * long_scale,
    )


def decode_point(score: int) -> tuple[float, float]:
    """``(longitude, latitude)`` of a stored score: the cell's center,
    clamped to the WGS84 ranges (``geohashDecodeAreaToLongLat``) -- what
    ``GEOPOS`` returns."""
    area = _decode(score, GEO_STEP_MAX)
    lon = (area.lon_min + area.lon_max) / 2
    lon = min(max(lon, GEO_LONG_MIN), GEO_LONG_MAX)
    lat = (area.lat_min + area.lat_max) / 2
    lat = min(max(lat, GEO_LAT_MIN), GEO_LAT_MAX)
    return lon, lat


def _move_x(bits: int, step: int, d: int) -> int:
    x = bits & 0xAAAAAAAAAAAAAAAA
    y = bits & 0x5555555555555555
    zz = 0x5555555555555555 >> (64 - step * 2)
    if d > 0:
        x = (x + (zz + 1)) & _U64
    else:
        x = ((x | zz) - (zz + 1)) & _U64
    x &= 0xAAAAAAAAAAAAAAAA >> (64 - step * 2)
    return x | y


def _move_y(bits: int, step: int, d: int) -> int:
    x = bits & 0xAAAAAAAAAAAAAAAA
    y = bits & 0x5555555555555555
    zz = 0xAAAAAAAAAAAAAAAA >> (64 - step * 2)
    if d > 0:
        y = (y + (zz + 1)) & _U64
    else:
        y = ((y | zz) - (zz + 1)) & _U64
    y &= 0x5555555555555555 >> (64 - step * 2)
    return x | y


def _neighbors(bits: int, step: int) -> dict[str, int]:
    """``geohashNeighbors``."""

    def move(dx: int, dy: int) -> int:
        out = bits
        if dx:
            out = _move_x(out, step, dx)
        if dy:
            out = _move_y(out, step, dy)
        return out

    return {
        "north": move(0, 1),
        "south": move(0, -1),
        "east": move(1, 0),
        "west": move(-1, 0),
        "north_east": move(1, 1),
        "north_west": move(-1, 1),
        "south_east": move(1, -1),
        "south_west": move(-1, -1),
    }


# -- geohash_helper.c ---------------------------------------------------------


def _estimate_steps_by_radius(range_meters: float, lat: float) -> int:
    """``geohashEstimateStepsByRadius``."""
    if range_meters == 0:
        return 26
    step = 1
    while range_meters < MERCATOR_MAX:
        range_meters *= 2
        step += 1
    step -= 2
    if lat > 66 or lat < -66:
        step -= 1
        if lat > 80 or lat < -80:
            step -= 1
    return min(max(step, 1), 26)


def _cos(x: float) -> float:
    """C's ``cos``: ``NaN`` for an infinite argument (an ``inf`` radius),
    where Python's raises."""
    return math.cos(x) if math.isfinite(x) else math.nan


def _bounding_box(
    longitude: float, latitude: float, radius: float, conversion: float
) -> tuple[float, float, float, float]:
    """``geohashBoundingBox`` for a circle: ``(min_lon, min_lat, max_lon,
    max_lat)``. An infinite radius gives ``NaN`` edges, which every later
    comparison treats as false, as in C."""
    height = conversion * radius
    width = conversion * radius
    lat_delta = (height / EARTH_RADIUS_IN_METERS) / D_R
    long_delta_top = (
        width / EARTH_RADIUS_IN_METERS / _cos((latitude + lat_delta) * D_R)
    ) / D_R
    long_delta_bottom = (
        width / EARTH_RADIUS_IN_METERS / _cos((latitude - lat_delta) * D_R)
    ) / D_R
    southern = latitude < 0
    min_lon = longitude - (long_delta_bottom if southern else long_delta_top)
    max_lon = longitude + (long_delta_bottom if southern else long_delta_top)
    return min_lon, latitude - lat_delta, max_lon, latitude + lat_delta


def search_ranges(
    longitude: float, latitude: float, radius: float, conversion: float
) -> list[tuple[int, int]]:
    """The score ranges ``[min, max)`` Redis scans for a radius search
    (``geohashCalculateAreasByShapeWGS84`` then ``membersOfAllNeighbors`` /
    ``scoresOfGeoHashBox``): the center box and the neighbours it keeps."""
    min_lon, min_lat, max_lon, max_lat = _bounding_box(
        longitude, latitude, radius, conversion
    )
    radius_meters = radius * conversion
    steps = _estimate_steps_by_radius(radius_meters, latitude)
    bits = encode(longitude, latitude, steps)
    neighbors = _neighbors(bits, steps)
    area = _decode(bits, steps)
    decrease_step = (
        _decode(neighbors["north"], steps).lat_max < max_lat
        or _decode(neighbors["south"], steps).lat_min > min_lat
        or _decode(neighbors["east"], steps).lon_max < max_lon
        or _decode(neighbors["west"], steps).lon_min > min_lon
    )
    if steps > 1 and decrease_step:
        steps -= 1
        bits = encode(longitude, latitude, steps)
        neighbors = _neighbors(bits, steps)
        area = _decode(bits, steps)
    kept: dict[str, Optional[int]] = dict(neighbors)
    if steps >= 2:
        if area.lat_min < min_lat:
            kept["south"] = kept["south_west"] = kept["south_east"] = None
        if area.lat_max > max_lat:
            kept["north"] = kept["north_east"] = kept["north_west"] = None
        if area.lon_min < min_lon:
            kept["west"] = kept["south_west"] = kept["north_west"] = None
        if area.lon_max > max_lon:
            kept["east"] = kept["south_east"] = kept["north_east"] = None
    order = (
        "north",
        "south",
        "east",
        "west",
        "north_east",
        "north_west",
        "south_east",
        "south_west",
    )
    shift = 52 - steps * 2
    ranges: list[tuple[int, int]] = []
    for box in [bits] + [kept[name] for name in order]:
        if box is None:
            continue
        span = (as_score(box << shift), as_score((box + 1) << shift))
        if span not in ranges:
            ranges.append(span)
    return ranges


def distance(lon1d: float, lat1d: float, lon2d: float, lat2d: float) -> float:
    """``geohashGetDistance``: haversine on Redis's earth radius, with its
    equal-longitude shortcut. ``asin`` of a rounding-inflated argument above
    1 is ``NaN`` in C (Python raises), and ``NaN > radius`` is false there, so
    such a point is kept -- reproduced."""
    lon1r = lon1d * D_R
    lon2r = lon2d * D_R
    v = math.sin((lon2r - lon1r) / 2)
    if v == 0.0:
        return EARTH_RADIUS_IN_METERS * abs(lat2d * D_R - lat1d * D_R)
    lat1r = lat1d * D_R
    lat2r = lat2d * D_R
    u = math.sin((lat2r - lat1r) / 2)
    a = u * u + math.cos(lat1r) * math.cos(lat2r) * v * v
    root = math.sqrt(a)
    arc = math.asin(root) if root <= 1.0 else math.nan
    return 2.0 * EARTH_RADIUS_IN_METERS * arc


NAN_DISTANCE_REPLY = float("-922337203685477.5808")
"""What ``WITHDIST`` replies for a distance that computes to ``NaN``, as an
x86-64 Redis replies it (CI's ``redis:7-alpine``, the reference build):
``fixedpoint_d2string`` prints ``llrint(NaN * 10000)``, and x86-64's
``llrint`` (``cvtsd2si``) returns ``LLONG_MIN`` for ``NaN``, which prints as
``-922337203685477.5808``. An arm64 Redis's ``llrint(NaN)`` is 0 and it
replies ``0.0000`` instead: a documented divergence."""


def reply_distance(meters: float, conversion: float) -> float:
    """The distance as ``WITHDIST`` replies it and redis-py reads it:
    ``meters / conversion`` printed with four decimals
    (``fixedpoint_d2string``: ``llrint(d * 10000)``, ties to even), parsed
    back with ``float``. A ``NaN`` distance (``asin`` of a rounding-inflated
    argument above 1) replies :data:`NAN_DISTANCE_REPLY`, as on x86-64."""
    value = meters / conversion
    if value != value:
        return NAN_DISTANCE_REPLY
    if value == 0:
        return 0.0
    return round(value * 10000.0) / 10000


# -- argument parsing as the server does it -----------------------------------

_STRTOD = re.compile(
    r"[+-]?(?:(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|inf(?:inity)?"
    r"|0x(?:[0-9a-f]+\.?[0-9a-f]*|\.[0-9a-f]+)(?:p[+-]?\d+)?)",
    re.IGNORECASE | re.ASCII,
)


def _wire_text(value: Any) -> Any:
    """The argument as redis-py's encoder sends it: ``repr`` of an
    ``int``/``float``, a string as is; anything else (a ``bool``, a
    ``Decimal``) is refused before it is sent, with redis-py's text."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, bytes)):
        from ...models.query import QueryException

        raise QueryException(
            f"Invalid input of type: '{type(value).__name__}'. Convert to a "
            "bytes, string, int or float first."
        )
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, bytes):
        return value.decode("latin-1")
    return value


def _string2d(text: str) -> Optional[float]:
    """``string2d``: the whole string must be one ``strtod`` number, with no
    leading space, not ``nan``, and not out of range (``ERANGE`` with an
    infinite or zero result). ``None`` when it refuses."""
    if not _STRTOD.fullmatch(text):
        return None
    lowered = text.lower().lstrip("+-")
    value = float.fromhex(text) if lowered.startswith("0x") else float(text)
    if value != value:
        return None  # pragma: no cover - the pattern admits no nan
    if math.isinf(value) and not lowered.startswith("inf"):
        return None
    if lowered.startswith("0x"):
        nonzero = re.search(r"[1-9a-f]", lowered[2:].split("p")[0])
    else:
        nonzero = re.search(r"[1-9]", lowered.split("e")[0])
    if value == 0.0 and nonzero:
        return None
    return value


def parse_double(value: Any, message: str) -> float:
    """``getDoubleFromObjectOrReply``: the argument as redis-py sends it,
    parsed as the server parses it; refused with ``message``."""
    parsed = _string2d(_wire_text(value))
    if parsed is None:
        from ...models.query import QueryException

        raise QueryException(message)
    return parsed


def _pair_error(longitude: float, latitude: float) -> str:
    return f"invalid longitude,latitude pair {longitude:f},{latitude:f}"


def parse_point(longitude: Any, latitude: Any) -> tuple[float, float]:
    """``extractLongLatOrReply``: both parsed, then range-checked."""
    lon = parse_double(longitude, "value is not a valid float")
    lat = parse_double(latitude, "value is not a valid float")
    if (
        lon < GEO_LONG_MIN
        or lon > GEO_LONG_MAX
        or lat < GEO_LAT_MIN
        or lat > GEO_LAT_MAX
    ):
        from ...models.query import QueryException

        raise QueryException(_pair_error(lon, lat))
    return lon, lat


def parse_radius(radius: Any) -> float:
    """``extractDistanceOrReply``'s radius (the unit is checked earlier, by
    ``GeoField.parse_query``)."""
    value = parse_double(radius, "need numeric radius")
    if value < 0:
        from ...models.query import QueryException

        raise QueryException("radius cannot be negative")
    return value


# -- schema -------------------------------------------------------------------


def _geo_fields(spec: ModelSpec) -> list[str]:
    return sorted(n for n, fs in spec.fields.items() if fs.kind == GEO_KIND)


def geo_columns(spec: ModelSpec) -> list[Column]:
    """``<f>__geohash bigint`` and the decoded ``<f>__geolon`` /
    ``<f>__geolat`` per ``GeoField`` (auxiliary: never hydrated)."""
    out: list[Column] = []
    for name in _geo_fields(spec):
        out.append(Column(name + HASH_SUFFIX, "bigint", name, role="aux"))
        out.append(Column(name + LON_SUFFIX, "double precision", name, role="aux"))
        out.append(Column(name + LAT_SUFFIX, "double precision", name, role="aux"))
    return out


def geo_indexes(
    spec: ModelSpec, index_name: Callable[..., str]
) -> list[tuple[str, str, bool]]:
    """A partial B-tree on the score: each search box is one range of it."""
    out = []
    for name in _geo_fields(spec):
        col = quote_ident(name + HASH_SUFFIX)
        out.append((index_name("geo", name), f"({col}) WHERE {col} IS NOT NULL", False))
    return out


# -- writes -------------------------------------------------------------------


def geo_save_values(
    ts: TableSpec, spec: ModelSpec, obj: Any, names: Sequence[str]
) -> dict[str, Any]:
    """The geo columns a save writes, for each ``GeoField`` in ``names``.

    ``GeoField.on_save``'s rule: a value with a falsy latitude or longitude
    (``None``, ``0``) leaves the member out of the index (``ZREM``), else
    ``GEOADD`` stores the step-26 score. A point ``GEOADD`` refuses raises
    ``ValueError`` with Redis's text *before* anything is written; on Redis
    the hash has already been written by then (a documented divergence: the
    Postgres save is atomic)."""
    out: dict[str, Any] = {}
    for name in names:
        fs = spec.fields.get(name)
        if fs is None or fs.kind != GEO_KIND:
            continue
        value = getattr(obj, name, None)
        if isinstance(value, tuple) and len(value) == 2:
            latitude, longitude = value[0], value[1]
        else:
            latitude = longitude = None
        if not value or not (longitude and latitude):
            out.update(
                {
                    name + HASH_SUFFIX: None,
                    name + LON_SUFFIX: None,
                    name + LAT_SUFFIX: None,
                }
            )
            continue
        try:
            lon, lat = parse_point(longitude, latitude)
        except Exception as exc:
            raise ValueError(str(exc)) from None
        score = score_of(lon, lat)
        qlon, qlat = decode_point(score)
        out.update(
            {
                name + HASH_SUFFIX: score,
                name + LON_SUFFIX: qlon,
                name + LAT_SUFFIX: qlat,
            }
        )
    return out


# -- reads ----------------------------------------------------------------------


@dataclass(frozen=True)
class GeoResolved:
    """A geo leaf after its search ran: the keys it matched (a
    ``Cond(field, Op.WITHIN, GeoResolved)`` renders ``_pk = ANY(…)``) and,
    with ``with_distances``, each key's distance as Redis replies it."""

    keys: tuple[str, ...]
    distances: Optional[dict[str, float]] = None
    unit: Optional[str] = None


def _geo_leaves(where: Optional[Predicate]) -> Iterator[Cond]:
    if where is None:
        return
    if isinstance(where, Cond):
        if where.op is Op.WITHIN and not isinstance(where.value, GeoResolved):
            yield where
        return
    if isinstance(where, Not):
        yield from _geo_leaves(where.item)
        return
    if isinstance(where, (And, Or)):
        for item in where.items:
            yield from _geo_leaves(item)


def _replace(where: Predicate, done: dict[int, Cond]) -> Predicate:
    if isinstance(where, Cond):
        return done.get(id(where), where)
    if isinstance(where, Not):
        return Not(_replace(where.item, done))
    if isinstance(where, And):
        return And(tuple(_replace(i, done) for i in where.items))
    if isinstance(where, Or):
        return Or(tuple(_replace(i, done) for i in where.items))
    return where  # pragma: no cover


def _search(
    backend: Any,
    ts: TableSpec,
    query: Any,
    scope: tuple[str, list[Any]] = ("", []),
) -> GeoResolved:
    """One ``GEORADIUS``/``GEORADIUSBYMEMBER``, in Redis's argument order:
    the center (or the member) is checked first, then the radius; a search
    by member of a field nothing is indexed under replies empty without
    looking at either, as a missing key does.

    ``scope`` is SQL the query ANDs beside this leaf anyway (its top-level
    siblings): it narrows the candidates whose distance is computed, never
    the member lookup, and cannot change which result rows the leaf
    admits."""
    from .ttl import live_sql

    name = query.field_name
    gh = quote_ident(name + HASH_SUFFIX)
    lon_col = quote_ident(name + LON_SUFFIX)
    lat_col = quote_ident(name + LAT_SUFFIX)
    live = live_sql(ts)
    live_and = f" AND {live}" if live else ""
    conversion = UNIT_TO_METERS[query.unit]
    member_key = query.member_key
    # redis-py encodes every argument before sending any: a bool or other
    # unsendable value is refused ahead of every server-side check.
    if member_key is None:
        _wire_text(query.longitude)
        _wire_text(query.latitude)
    _wire_text(query.radius)
    if member_key is not None:
        # The center and the "is anything indexed" check ignore expiry, as
        # the geo set does on Redis: an expired record's hash is gone there
        # but its member stays (no read purges it), so GEORADIUSBYMEMBER
        # still decodes it and searches around it. An expired row stays
        # here until the reaper deletes it; after that it is a missing
        # member, as on Redis after ``clean_indexes`` or a delete. The
        # candidates below are live rows only, which is what the Redis
        # path's hydration leaves of its reply.
        rows, _ = backend._run(
            f"SELECT (SELECT ARRAY[{lon_col}, {lat_col}] FROM {ts.qualified} "
            f'WHERE "_pk" = %s AND {gh} IS NOT NULL), '
            f"EXISTS (SELECT 1 FROM {ts.qualified} WHERE {gh} IS NOT NULL)",
            [member_key],
        )
        center, indexed = rows[0]
        if not indexed:
            return GeoResolved((), {} if query.with_distances else None, query.unit)
        if center is None:
            from ...models.query import QueryException

            raise QueryException("could not decode requested zset member")
        lon, lat = float(center[0]), float(center[1])
        radius = parse_radius(query.radius)
    else:
        lon, lat = parse_point(query.longitude, query.latitude)
        radius = parse_radius(query.radius)
    ranges = search_ranges(lon, lat, radius, conversion)
    clauses = " OR ".join(f"({gh} >= %s AND {gh} < %s)" for _ in ranges)
    params: list[Any] = [bound for span in ranges for bound in span]
    scope_sql, scope_params = scope
    rows, _ = backend._run(
        f'SELECT "_pk", {lon_col}, {lat_col} FROM {ts.qualified} '
        f"WHERE ({clauses}){live_and}" + (f" AND {scope_sql}" if scope_sql else ""),
        params + list(scope_params),
    )
    limit = radius * conversion
    keys: list[str] = []
    distances: dict[str, float] = {}
    for pk, mlon, mlat in rows:
        meters = distance(lon, lat, mlon, mlat)
        if meters > limit:
            continue
        keys.append(pk)
        if query.with_distances:
            distances[pk] = reply_distance(meters, conversion)
    return GeoResolved(
        tuple(keys), distances if query.with_distances else None, query.unit
    )


def resolve_geo(
    backend: Any, ts: TableSpec, where: Optional[Predicate]
) -> tuple[Optional[Predicate], Optional[dict[str, float]], Optional[str]]:
    """Run every geo leaf of ``where`` and replace it with its matched keys.

    Returns the rewritten predicate, the distances of the leaves that asked
    for them (merged in predicate order: a later leaf's distance for a key
    wins, as a later ``state.geo_distances.update`` does on Redis), and the
    unit of the last such leaf; ``None`` for both when no leaf asked."""
    leaves = list(_geo_leaves(where))
    if not leaves or where is None:
        return where, None, None
    # A leaf that is a direct conjunct searches only the rows its geo-free
    # siblings admit (filter(bucket="x", place=…) computes no distance for
    # another bucket); a nested leaf searches every indexed row.
    scope: tuple[str, list[Any]] = ("", [])
    direct: set[int] = set()
    if isinstance(where, And):
        siblings = [i for i in where.items if not list(_geo_leaves(i))]
        direct = {id(i) for i in where.items if isinstance(i, Cond)}
        if siblings:
            from .plan import render_where

            sql, params = render_where(
                ts, ts.field_kinds, And(tuple(siblings)), live=False
            )
            scope = (sql[len(" WHERE ") :], params)
    done: dict[int, Cond] = {}
    distances: Optional[dict[str, float]] = None
    unit: Optional[str] = None
    for leaf in leaves:
        resolved = _search(
            backend, ts, leaf.value, scope if id(leaf) in direct else ("", [])
        )
        done[id(leaf)] = Cond(leaf.field, Op.WITHIN, resolved)
        if resolved.distances is not None:
            distances = {**(distances or {}), **resolved.distances}
            unit = resolved.unit
    return _replace(where, done), distances, unit


def within_sql(c: Cond, params: list[Any]) -> str:
    """A resolved geo leaf: the keys its search matched."""
    if not isinstance(c.value, GeoResolved):
        from ..types import BackendCapabilityError

        raise BackendCapabilityError(
            f"a GeoField radius filter on {c.field!r} scopes filter() and count() "
            "on Postgres; it cannot scope a ranking or search yet (#759 M5)"
        )
    if not c.value.keys:
        return "FALSE"
    params.append(list(c.value.keys))
    return '"_pk" = ANY(%s::text[])'
