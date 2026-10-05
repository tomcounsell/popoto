"""The long-tail memory fields on Postgres (#759 M5; plan §2 H, §3).

Three Redis features that each owned a Lua script, stored natively:

=======================  =====================================================
feature                  Postgres
=======================  =====================================================
``CyclicDecayField``     the decay clock is the field's own ``double
                         precision`` column, as for ``DecayingSortedField``
                         (M2a); the learned cycles are four parallel
                         ``double precision[]`` columns, ``<f>__cycle_period``,
                         ``<f>__cycle_amp``, ``<f>__cycle_phase`` and
                         ``<f>__cycle_base`` (the #698 declared baseline,
                         ``NULL`` element = unknown), and the homeostatic
                         pressure two ``double precision`` columns,
                         ``<f>__pressure_rate`` and ``<f>__pressure_at``
                         (``last_resolved``). ``NULL`` = no companion-hash
                         entry. ``CYCLIC_DECAY_LUA`` is a SQL expression
                         (:func:`cyclic_score_sql`) that ``rank_decayed``,
                         ``composite_score``'s decay arm and the assembler's
                         score proxy rank with; ``CYCLES_MERGE_LUA`` is part of
                         the save's upsert (:func:`cyclic_save_parts`);
                         ``CYCLES_ADJUST_LUA`` is one ``UPDATE``
                         (``strengthen_cycle`` / ``weaken_cycle``).
``TDValueField``         a ``numeric`` column, as ``DecimalField``;
                         ``TD_UPDATE_LUA`` is one ``UPDATE`` that writes the
                         new value through the script's ``%.14g``.
``PredictionLedgerMixin``  two engine tables, ``popoto_prediction_ledger
                         (model, member, entry jsonb, lua_packed)`` -- the
                         ``$PL:{Class}:meta:{pk}`` hash entry -- and
                         ``popoto_prediction_error (model, part, member,
                         error)`` -- the ``$PL:{Class}:errors:{part}`` sorted
                         set. ``RESOLVE_PREDICTION_LUA`` is one statement.
=======================  =====================================================

Why arrays and not ``jsonb`` (plan §3 said ``jsonb``): the cycle amplitudes
are ``double``s the score multiplies, and ``jsonb`` numbers are ``numeric``,
which cannot hold ``NaN`` or ``±Infinity`` (an amplitude can be either: a
declared ``float("inf")`` passes the field's validation) and would add a text
round trip to every row a ranking scans. A ``float8[]`` keeps every bit.

Bit-exactness
-------------
Every number the scripts compute is computed here operation for operation in
``double precision``, in the script's order of evaluation: ``cos`` and
``power`` are the platform's ``libm`` on both servers, as for M2a's decay
expression. Where C saturates (``inf`` / ``0``) and Postgres would raise
(``value out of range``, ``input is out of range`` for ``cos(inf)``), the
clamped helpers of :mod:`.memory` take over; the plain expression is used
for every row whose inputs lie in a box where no step can leave the double
range. Numbers the scripts *return* went through Lua's ``tostring``
(``%.14g``), and numbers they *store* through ``cmsgpack``, which packs an
integral number as an integer: :func:`lua_num` and :func:`lua_repack` apply
the same rules on the way out.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from ..types import ModelSpec, RecordId, UnitOfWork
from .codec import _BYTES, _FLOAT, _MAP
from .memory import (
    _NOT_HANDLED,
    _ZERO,
    _lit,
    _nonfinite,
    decay_score_sql,
    lua_tostring,
    safe_mul,
    safe_sub,
)
from .recipes import ENGINE_TABLES
from .schema import Column, TableSpec, quote_ident

__all__ = [
    "CYCLE_SUFFIXES",
    "CYCLIC_KIND",
    "LEDGER_FIELD",
    "LEDGER_MIXIN",
    "LongtailOpsMixin",
    "PRESSURE_SUFFIXES",
    "TD_KIND",
    "cyclic_columns",
    "cyclic_save_parts",
    "cyclic_score_sql",
    "lua_num",
    "lua_repack",
    "lua_tonumber",
    "merge_cycles",
]

logger = logging.getLogger("POPOTO.postgres")

CYCLIC_KIND = "CyclicDecayField"
TD_KIND = "TDValueField"
LEDGER_MIXIN = "PredictionLedgerMixin"
#: ``field_call`` pseudo-field for the prediction ledger (no Field instance).
LEDGER_FIELD = "_ledger"

#: Columns beside a ``CyclicDecayField``'s clock: the cycles, element-wise.
CYCLE_SUFFIXES = ("__cycle_period", "__cycle_amp", "__cycle_phase", "__cycle_base")
#: ``{rate, last_resolved}``: ``NULL`` rate = no pressure entry.
PRESSURE_SUFFIXES = ("__pressure_rate", "__pressure_at")

LEDGER_TABLE = "popoto_prediction_ledger"
ERROR_TABLE = "popoto_prediction_error"
# The engine tables are created on first use by RecipeOpsMixin._engine, like
# the M4b ones; registered here so this module owns its DDL.
ENGINE_TABLES.setdefault(
    LEDGER_TABLE,
    "model text NOT NULL, member text NOT NULL, entry jsonb NOT NULL, "
    "lua_packed boolean NOT NULL, PRIMARY KEY (model, member)",
)
ENGINE_TABLES.setdefault(
    ERROR_TABLE,
    "model text NOT NULL, part text NOT NULL, member text NOT NULL, "
    "error double precision NOT NULL, PRIMARY KEY (model, part, member)",
)

_TWO_PI = 2 * math.pi
"""``2 * math.pi`` in the script: Lua's ``math.pi`` is C's ``M_PI``."""
_NAN = "'NaN'::float8"
_INF = "'Infinity'::float8"
_INT64 = 2.0**63


# -- Lua / cmsgpack number and table rules ------------------------------------


def lua_tonumber(value: Any) -> Optional[float]:
    """Lua 5.1 ``tonumber`` of an ``ARGV`` string (``str(value)``): ``strtod``
    with nothing trailing, else ``nil`` (``None``)."""
    text = str(value)
    if "_" in text:  # Python's float() accepts digit separators; strtod not
        return None
    try:
        return float(text)
    except ValueError:
        return None


def lua_num(value: float) -> Any:
    """A Lua number as ``cmsgpack`` packs it and msgpack reads it back: an
    integer when it is integral and fits ``int64`` (``-0.0`` -> ``0``), else
    the ``float`` (a ``float32`` encoding, when exact, decodes to the same
    double)."""
    if isinstance(value, bool) or not isinstance(value, float):
        return value
    if math.isfinite(value) and value == int(value) and -_INT64 <= value < _INT64:
        return int(value)
    return value


_MAX_NESTING = 16
"""``LUACMSGPACK_MAX_NESTING``: a table this deep packs as ``nil``."""


class _LuaUnpackError(ValueError):
    """The value would fail ``cmsgpack.unpack`` inside the script, so its
    ``pcall`` returns false: a msgpack ``bin`` (Python ``bytes``; Redis's
    cmsgpack reads no ``bin`` type -- "Bad data format in input"), or a
    ``nil`` or ``NaN`` table key."""


def _lua_value(value: Any) -> Any:
    """``value`` as ``cmsgpack.unpack`` turns it into a Lua value, written
    back in Python terms: numbers become doubles, a ``str`` a Lua string
    (bytes), ``None`` stays ``None`` (``nil``)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise _LuaUnpackError("Bad data format in input")
    if isinstance(value, str):
        return value.encode("utf-8", "surrogateescape")
    if isinstance(value, (list, tuple)):
        # lua_rawseti(t, j + 1, v): a nil element leaves a hole.
        return {
            float(i + 1): _lua_value(v) for i, v in enumerate(value) if v is not None
        }
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            key = _lua_value(k)
            if key is None or (isinstance(key, float) and math.isnan(key)):
                raise _LuaUnpackError("table index is nil or NaN")
            if isinstance(key, dict):
                key = id(key)  # a table key: identity, never an array index
            if v is None:
                continue  # t[k] = nil stores nothing
            out[key] = _lua_value(v)
        return out
    return value


def _is_array(table: dict[Any, Any]) -> bool:
    """``table_is_an_array``: only positive integral number keys, and as many
    as the largest (so exactly ``1..n``; the empty table is an array)."""
    count = 0
    biggest = 0.0
    for key in table:
        if (
            isinstance(key, bool)
            or not isinstance(key, float)
            or key <= 0
            or key != int(key)
        ):
            return False
        biggest = max(biggest, key)
        count += 1
    return biggest == count


def _lua_pack(value: Any, level: int) -> Any:
    """``cmsgpack.pack`` of a Lua value, as msgpack (``raw=False``) reads it
    back."""
    if isinstance(value, dict):
        if level == _MAX_NESTING:
            return None
        if _is_array(value):
            return [
                _lua_pack(value[float(i)], level + 1) for i in range(1, len(value) + 1)
            ]
        return {
            _lua_pack(k, level + 1) if not isinstance(k, dict) else k: _lua_pack(
                v, level + 1
            )
            for k, v in value.items()
        }
    if isinstance(value, float):
        return lua_num(value)
    if isinstance(value, bytes):
        # cmsgpack packs every Lua string as a msgpack str.
        return value.decode("utf-8")
    return value


def lua_repack(value: Any) -> Any:
    """``value`` after ``cmsgpack.unpack`` then ``cmsgpack.pack`` -- what a
    script that read-modify-writes a msgpack entry stores. Raises
    ``ValueError`` where the unpack itself would fail."""
    return _lua_pack(_lua_value(value), 0)


# -- the cycle merge (CYCLES_MERGE_LUA), in Python ---------------------------


def _bits(x: float) -> bytes:
    """The match key: the double's bits (``'%.17g'`` keys tell ``-0.0``
    from ``0.0`` and keep equal values together, as bits do)."""
    return struct.pack(">d", x)


def declared_cycles(
    cycles: Sequence[Sequence[Any]],
) -> list[tuple[float, float, float]]:
    """A field's ``cycles`` as the script receives them: each slot
    ``str()``-ed into ``ARGV`` and ``tonumber``-ed back (phase ``or 0``)."""
    out = []
    for cycle in cycles:
        period = lua_tonumber(cycle[0])
        amp = lua_tonumber(cycle[1])
        phase = lua_tonumber(cycle[2] if len(cycle) > 2 else 0)
        if period is None or amp is None:
            raise ValueError(
                f"CyclicDecayField cycle {tuple(cycle)!r}: a non-numeric period or "
                "amplitude cannot be stored on Postgres"
            )
        out.append((period, amp, 0.0 if phase is None else phase))
    return out


def merge_cycles(
    stored: Optional[Sequence[tuple[float, float, Optional[float]]]],
    declared: Sequence[tuple[float, float, float]],
) -> tuple[
    list[tuple[float, float, float, float]], list[tuple[float, float, float, float]]
]:
    """``CYCLES_MERGE_LUA``'s cycles half: ``(output, resets)``.

    ``stored`` is ``[(period, amplitude, baseline)]`` (``None`` = no entry).
    Declared cycles are matched to stored ones by period, FIFO within a
    period; a picked entry keeps its learned amplitude unless its recorded
    baseline differs from the declared amplitude (the developer edited the
    declaration: a *reset*). Output entries are ``(period, amplitude, phase,
    declared amplitude)``."""
    buckets: dict[bytes, list[tuple[float, Optional[float]]]] = {}
    for period, amp, baseline in stored or ():
        buckets.setdefault(_bits(period), []).append((amp, baseline))
    output = []
    resets = []
    for period, declared_amp, phase in declared:
        amplitude = declared_amp
        bucket = buckets.get(_bits(period))
        if bucket:
            learned, baseline = bucket.pop(0)
            if baseline is None or (
                baseline == declared_amp and not math.isnan(baseline)
            ):
                amplitude = learned
            else:
                resets.append((period, baseline, declared_amp, learned))
        output.append((period, amplitude, phase, declared_amp))
    return output, resets


# -- schema ---------------------------------------------------------------------


def cyclic_columns(spec: ModelSpec) -> list[Column]:
    """State columns of every ``CyclicDecayField`` (role ``"state"``)."""
    out: list[Column] = []
    for name in sorted(spec.fields):
        if spec.fields[name].kind != CYCLIC_KIND:
            continue
        out.extend(
            Column(name + suffix, "double precision[]", name, "state")
            for suffix in CYCLE_SUFFIXES
        )
        out.extend(
            Column(name + suffix, "double precision", name, "state")
            for suffix in PRESSURE_SUFFIXES
        )
    return out


# -- clamped helpers beyond .memory's ------------------------------------------


def safe_add(a: str, b: str) -> str:
    """``a + b`` that saturates to ``±inf`` where Postgres would raise."""
    return safe_sub(a, f"(-{b})")


def safe_div(a: str, b: str) -> str:
    """``a / b`` for ``b > 0`` (or ``inf``) that gives C's ``inf`` / ``0``
    where Postgres would raise on overflow or underflow."""
    neg = f"(({a} < 0) <> ({b} < 0))"
    logq = f"(ln(abs({a})) - ln(abs({b})))"
    return (
        f"(CASE WHEN {a} = {_ZERO} OR {_nonfinite(a)} OR {_nonfinite(b)}"
        f" THEN {a} / {b}"
        f" WHEN {logq} > 709.78"
        f" THEN (CASE WHEN {neg} THEN '-Infinity'::float8 ELSE {_INF} END)"
        f" WHEN {logq} < -745.13"
        f" THEN (CASE WHEN {neg} THEN (-0.0)::float8 ELSE {_ZERO} END)"
        f" ELSE {a} / {b} END)"
    )


def _levels(first: str, steps: list[tuple[list[str], str]]) -> str:
    """Nest derived tables: ``first`` is the innermost ``SELECT`` (aliased
    ``l0``); each step is ``(columns, from-alias)`` selecting from the
    previous level. ``OFFSET 0`` keeps each one evaluated once per row."""
    sql = first
    for depth, (cols, _alias) in enumerate(steps):
        sql = f"SELECT {', '.join(cols)} FROM ({sql}) AS l{depth} OFFSET 0"
    return sql


# -- CYCLIC_DECAY_LUA ------------------------------------------------------------


def _now_is_tame(now: float) -> bool:
    return math.isfinite(now) and 1.0 <= now <= 1e15


def _cycles_sql(field: str, now: float, alias: str = "t") -> str:
    """The cyclic resonance of one row: ``0``, then ``+ amp * cos(2 * pi *
    (now - phase) / period)`` for each stored cycle in order whose period is
    ``> 0`` (``NaN`` is not)."""
    p, a, h = (f"{alias}.{quote_ident(field + s)}" for s in CYCLE_SUFFIXES[:3])
    now_l, two_pi = _lit(now), _lit(_TWO_PI)
    src = f"unnest({p}, {a}, {h}) WITH ORDINALITY AS u(p, a, h, o)"
    valid = "u.p > 0::float8 AND u.p <> 'NaN'::float8"
    hh = f"coalesce(u.h, {_ZERO})"

    # The clamped fold: each term with the clamped helpers (a derived table
    # per step), then summed left to right with a saturating add.
    l0 = (
        f"SELECT u.o, u.a, u.p, {safe_sub(now_l, hh)} AS d FROM {src} "
        f"WHERE {valid} OFFSET 0"
    )
    terms = _levels(
        l0,
        [
            (["l0.o", "l0.a", "l0.p", f"{safe_mul(two_pi, 'l0.d')} AS m"], "l0"),
            (["l1.o", "l1.a", f"{safe_div('l1.m', 'l1.p')} AS x"], "l1"),
            (
                [
                    "l2.o",
                    "l2.a",
                    f"(CASE WHEN abs(l2.x) = {_INF} THEN {_NAN} ELSE cos(l2.x) END)"
                    " AS c",
                ],
                "l2",
            ),
            (["l3.o", f"{safe_mul('l3.a', 'l3.c')} AS v"], "l3"),
        ],
    )
    fold = (
        f"(SELECT (WITH RECURSIVE f(i, s) AS (SELECT 0, {_ZERO} UNION ALL "
        f"SELECT f.i + 1, {safe_add('f.s', 'tt.v[f.i + 1]')} FROM f "
        f"WHERE f.i < coalesce(cardinality(tt.v), 0)) "
        f"SELECT f.s FROM f ORDER BY f.i DESC LIMIT 1) "
        f"FROM (SELECT array_agg(l4.v ORDER BY l4.o) AS v FROM ({terms}) AS l4) tt)"
    )
    if not _now_is_tame(now):
        return fold
    # The plain sum, for a row whose every cycle lies in the box where no step
    # can leave the double range: |now - phase| <= ~1e300, period in
    # [1e-6, 1e300], |amplitude| in {0} U [1e-290, 1e300] (|cos| >= ~1e-19 for
    # a double argument, so the product cannot underflow to 0, and the sum of
    # terms bounded by 1e300 cannot overflow).
    ok = (
        f"(u.p BETWEEN 1e-6::float8 AND 1e300::float8 "
        f"AND abs({hh}) <= 1e300::float8 "
        f"AND (u.a = {_ZERO} OR abs(u.a) BETWEEN 1e-290::float8 AND 1e300::float8))"
    )
    term = f"(u.a * cos({two_pi} * ({now_l} - {hh}) / u.p))"
    return (
        f"(SELECT CASE WHEN coalesce(bool_and({ok}), true) "
        f"THEN {_ZERO} + coalesce(sum(CASE WHEN {ok} THEN {term} END "
        f"ORDER BY u.o), {_ZERO}) ELSE {fold} END FROM {src} WHERE {valid})"
    )


def _pressure_sql(field: str, now: float, alias: str = "t") -> str:
    """``rate * max((now - last_resolved) / 86400, 0)`` when the row has a
    pressure entry with ``rate > 0``, else ``0``."""
    r, at = (f"{alias}.{quote_ident(field + s)}" for s in PRESSURE_SUFFIXES)
    now_l = _lit(now)
    last = f"coalesce({at}, {now_l})"
    live = f"coalesce({r} > {_ZERO} AND {r} <> {_NAN}, false)"
    clamped = (
        f"(SELECT {safe_mul('q.r', 'greatest(q.dd, 0::float8)')} FROM "
        f"(SELECT k.r, {safe_div('k.dl', '86400::float8')} AS dd FROM "
        f"(SELECT {r} AS r, {safe_sub(now_l, last)} AS dl OFFSET 0) AS k "
        f"OFFSET 0) AS q)"
    )
    if not _now_is_tame(now):
        return f"(CASE WHEN {live} THEN {clamped} ELSE {_ZERO} END)"
    plain = f"({r} * greatest(({now_l} - {last}) / 86400::float8, {_ZERO}))"
    tame = (
        f"({r} BETWEEN 1e-200::float8 AND 1e100::float8 "
        f"AND abs({last}) <= 1e15::float8)"
    )
    return (
        f"(CASE WHEN NOT {live} THEN {_ZERO} WHEN {tame} THEN {plain} "
        f"ELSE {clamped} END)"
    )


def cyclic_score_sql(
    ts: TableSpec,
    spec: ModelSpec,
    field: str,
    *,
    now: float,
    rate: float,
    base_field: Optional[str],
    confidence_field: Optional[str],
    strength: float,
    confidence_partition: Any = None,
) -> str:
    """The ``CYCLIC_DECAY_LUA`` score of one row of ``ts`` (aliased ``t``):
    ``decayed + cyclic + pressure``, evaluated in that order.

    ``decayed`` is :func:`~.memory.decay_score_sql` -- the script's decay
    and modulation steps are ``DECAY_SCORE_LUA``'s, and its unsplit ``base *
    pow`` equals that script's ``sign * |base| * pow`` once ``+ cyclic``
    (at least ``0``) has turned a ``-0`` into ``+0``. There is no validity
    gate: the script has none (``TestCyclicDecayGatingGap``)."""
    decayed = decay_score_sql(
        ts,
        spec,
        field,
        now=now,
        rate=rate,
        base_field=base_field,
        confidence_field=confidence_field,
        strength=strength,
        confidence_partition=confidence_partition,
    )
    cycles = _cycles_sql(field, now)
    pressure = _pressure_sql(field, now)
    return (
        f"(SELECT {safe_add('y.dc', 'y.p')} FROM (SELECT "
        f"{safe_add('z.d', 'z.c')} AS dc, z.p FROM (SELECT {decayed} AS d, "
        f"{cycles} AS c, {pressure} AS p OFFSET 0) AS z OFFSET 0) AS y)"
    )


# -- CYCLES_MERGE_LUA, as parts of the save upsert ------------------------------


@dataclass
class CyclicSave:
    """What one save does to its cyclic fields' state (the
    ``CYCLES_MERGE_LUA`` run ``on_save`` makes on Redis)."""

    cols: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, str] = field(default_factory=dict)
    returning: list[str] = field(default_factory=list)
    declared: dict[str, list[tuple[float, float, float]]] = field(default_factory=dict)

    def report(self, obj: Any, old: Sequence[Any]) -> None:
        """Emit ``on_save``'s #698 reset log line for each declared amplitude
        that won the merge, from the row's previous cycles (``RETURNING
        old.…``)."""
        from ...fields.cyclic_decay_field import log_declared_amplitude_reset

        member = obj.db_key.redis_key
        for i, (name, declared) in enumerate(self.declared.items()):
            periods, amps, bases = old[3 * i : 3 * i + 3]
            stored = None if periods is None else list(zip(periods, amps, bases))
            _out, resets = merge_cycles(stored, declared)
            for period, baseline, declared_amp, learned in resets:
                log_declared_amplitude_reset(
                    type(obj).__name__,
                    name,
                    member,
                    lua_num(period),
                    baseline,
                    declared_amp,
                    learned,
                )


def _array_lit(values: Sequence[float]) -> str:
    return "ARRAY[" + ", ".join(_lit(v) for v in values) + "]::float8[]"


def cyclic_save_parts(
    ts: TableSpec, spec: ModelSpec, obj: Any, names: Sequence[str]
) -> Optional[CyclicSave]:
    """``CYCLES_MERGE_LUA`` for each ``CyclicDecayField`` the save writes, as
    upsert parts: the new row's columns, and ``ON CONFLICT`` expressions that
    merge the declaration into the stored cycles -- period and phase from the
    declaration, amplitude learned unless the declaration was edited (#698),
    baseline = the declared amplitude -- and refresh the pressure rate,
    keeping ``last_resolved`` (``now`` for a new entry). No declared cycles
    clears the cycles; ``pressure_rate <= 0`` clears the pressure. ``None``
    when the save writes no cyclic field."""
    fields = [
        n for n in names if n in spec.fields and spec.fields[n].kind == CYCLIC_KIND
    ]
    if not fields:
        return None
    out = CyclicSave()
    now = time.time()
    table = quote_ident(ts.table)
    for name in fields:
        live = obj._meta.fields[name]
        declared = declared_cycles(live.cycles)
        out.declared[name] = declared
        cp, ca, ch, cb = (quote_ident(name + s) for s in CYCLE_SUFFIXES)
        pr, pa = (quote_ident(name + s) for s in PRESSURE_SUFFIXES)
        names_ = [name + s for s in CYCLE_SUFFIXES]
        if declared:
            dp = [d[0] for d in declared]
            da = [d[1] for d in declared]
            dh = [d[2] for d in declared]
            out.cols.update(dict(zip(names_, (dp, da, dh, list(da)))))
            merged = (
                "(SELECT array_agg(CASE WHEN s.o IS NULL THEN d.a "
                "WHEN s.b IS NULL OR (s.b = d.a AND s.b <> 'NaN'::float8) THEN s.a "
                "ELSE d.a END ORDER BY d.o) FROM (SELECT x.p, x.a, x.o, "
                "row_number() OVER (PARTITION BY float8send(x.p) ORDER BY x.o) AS r "
                f"FROM unnest({_array_lit(dp)}, {_array_lit(da)}) "
                "WITH ORDINALITY AS x(p, a, o)) AS d LEFT JOIN (SELECT y.a, y.b, "
                "y.o, float8send(y.p) AS k, row_number() OVER (PARTITION BY "
                f"float8send(y.p) ORDER BY y.o) AS r FROM unnest({table}.{cp}, "
                f"{table}.{ca}, {table}.{cb}) WITH ORDINALITY AS y(p, a, b, o)) AS s "
                "ON s.k = float8send(d.p) AND s.r = d.r)"
            )
            out.overrides[name + CYCLE_SUFFIXES[1]] = merged
        else:
            out.cols.update(dict.fromkeys(names_))
        rate = lua_tonumber(live.pressure_rate)
        if rate is not None and rate > 0:
            out.cols[name + PRESSURE_SUFFIXES[0]] = rate
            out.cols[name + PRESSURE_SUFFIXES[1]] = now
            out.overrides[name + PRESSURE_SUFFIXES[1]] = (
                f"CASE WHEN {table}.{pr} IS NOT NULL THEN {table}.{pa} "
                f"ELSE EXCLUDED.{pa} END"
            )
        else:
            out.cols[name + PRESSURE_SUFFIXES[0]] = None
            out.cols[name + PRESSURE_SUFFIXES[1]] = None
        out.returning.extend(f"old.{c}" for c in (cp, ca, cb))
    return out


# -- the field-adapter half ------------------------------------------------------


def _sort_key(key: str) -> bytes:
    return key.encode("utf-8", "surrogateescape")


def _rows_cycles(
    periods: Optional[Sequence[float]],
    amps: Optional[Sequence[float]],
    phases: Optional[Sequence[float]],
    bases: Optional[Sequence[Optional[float]]] = None,
) -> Optional[list[list[Any]]]:
    if periods is None:
        return None
    out = []
    for i, period in enumerate(periods):
        entry = [
            lua_num(float(period)),
            lua_num(float(amps[i])) if amps is not None else None,
            lua_num(float(phases[i])) if phases is not None else 0,
        ]
        if bases is not None and bases[i] is not None:
            entry.append(lua_num(float(bases[i])))
        out.append(entry)
    return out


# The ledger entry is jsonb with this module's codec tags (bytes, non-finite
# floats, non-str keys) -- never msgpack (plan §8) -- and none of popoto's
# type-registry tags: msgpack.unpackb in get_prediction_data has no object
# hook, so a nested {"__Decimal__": ...} dict stays a dict there too.


def _ledger_encode(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        text = repr(value)
        # jsonb keeps a number as numeric, whose text has no exponent and no
        # negative zero: 1e+300 would come back an int, -0.0 a 0.0. Those,
        # and what JSON cannot spell, are tagged so the float survives.
        if math.isfinite(value) and "e" not in text and text != "-0.0":
            return value
        return {_FLOAT: True, "as_encodable": text}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            _BYTES: True,
            "as_encodable": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, (list, tuple)):
        return [_ledger_encode(v) for v in value]
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            return {k: _ledger_encode(v) for k, v in value.items()}
        return {
            _MAP: True,
            "as_encodable": [
                [_ledger_encode(k), _ledger_encode(v)] for k, v in value.items()
            ],
        }
    raise TypeError(f"can not serialize {type(value).__name__!r} object")


def _hashable(key: Any) -> Any:
    return tuple(_hashable(k) for k in key) if isinstance(key, list) else key


def _ledger_decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_ledger_decode(v) for v in value]
    if isinstance(value, dict):
        if "as_encodable" in value:
            if value.get(_BYTES) is True:
                return base64.b64decode(value["as_encodable"])
            if value.get(_FLOAT) is True:
                return float(value["as_encodable"])
            if value.get(_MAP) is True:
                return {
                    _hashable(_ledger_decode(k)): _ledger_decode(v)
                    for k, v in value["as_encodable"]
                }
        return {k: _ledger_decode(v) for k, v in value.items()}
    return value


def _strict_keys(value: Any) -> None:
    """msgpack's ``strict_map_key``: ``unpackb`` refuses a map key that is
    not ``str`` / ``bytes`` (raised on Redis by ``get_prediction_data``)."""
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, (str, bytes)):
                raise ValueError(
                    f"{type(k).__name__} is not allowed for map key when "
                    "strict_map_key=True"
                )
            _strict_keys(v)
    elif isinstance(value, list):
        for v in value:
            _strict_keys(v)


def _ledger_jsonb(value: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(_ledger_encode(value), dumps=lambda v: json.dumps(v, allow_nan=False))


class LongtailOpsMixin:
    """The ``field_call`` adapters of this module, for
    :class:`~popoto.backends.postgres.PostgresBackend`."""

    schema: str
    _table: Callable[..., TableSpec]
    _run: Callable[..., tuple[list[tuple[Any, ...]], int]]
    _record_locked: Callable[..., tuple[str, list[Any]]]
    _engine: Callable[[str], str]

    def _longtail_field_call(
        self,
        spec: ModelSpec,
        field: str,
        op: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        uow: Optional[UnitOfWork],
    ) -> Any:
        handlers: dict[str, Callable[..., Any]] = {}
        if field == LEDGER_FIELD and LEDGER_MIXIN in spec.mixins:
            handlers = {
                "exists": self._ledger_exists,
                "record": self._ledger_record,
                "get": self._ledger_get,
                "resolve": self._ledger_resolve,
                "highest": self._ledger_highest,
                "rows": self._ledger_rows,
                "import": self._ledger_import,
            }
            handler = handlers.get(op)
            if handler is not None:
                return handler(spec, *args, uow=uow, **kwargs)
            return _NOT_HANDLED
        fs = spec.fields.get(field)
        if fs is None:
            return _NOT_HANDLED
        if fs.kind == CYCLIC_KIND:
            handlers = {
                "adjust": self._cycles_adjust,
                "resolve_pressure": self._cycles_resolve_pressure,
                "state": self._cycles_state,
                "import": self._cycles_import,
            }
        elif fs.kind == TD_KIND:
            handlers = {"td_update": self._td_update}
        handler = handlers.get(op)
        if handler is None:
            return _NOT_HANDLED
        return handler(spec, field, *args, uow=uow, **kwargs)

    # CyclicDecayField ----------------------------------------------------------

    def _cycles_adjust(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        factor: float,
        max_amplitude: float,
        min_threshold: float,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[list[list[Any]]]:
        """``CYCLES_ADJUST_LUA``: every amplitude ``* factor``, clamped to
        ``[0, max_amplitude]``, below ``min_threshold`` snapped to ``0`` (a
        ``NaN`` passes every comparison untouched, as in Lua). One
        ``UPDATE``; ``None`` when there is no stored cycles entry (the
        script's ``nil``)."""
        ts = self._table(spec, write=True)
        cp, ca, ch = (quote_ident(field + s) for s in CYCLE_SUFFIXES[:3])
        factor_n = lua_tonumber(factor)
        f_l = _lit(math.nan if factor_n is None else factor_n)
        mx, mn = _lit(float(max_amplitude)), _lit(float(min_threshold))
        x = safe_mul("u.a", f_l)
        clamp = (
            f"(SELECT array_agg((CASE WHEN w.y = {_NAN} THEN w.y "
            f"WHEN w.y < {mn} THEN {_ZERO} ELSE w.y END) ORDER BY w.o) FROM "
            f"(SELECT v.o, (CASE WHEN v.x = {_NAN} THEN v.x WHEN v.x < {_ZERO} "
            f"THEN {_ZERO} WHEN v.x > {mx} THEN {mx} ELSE v.x END) AS y FROM "
            f"(SELECT u.o, {x} AS x FROM unnest({ca}) WITH ORDINALITY AS u(a, o) "
            f"OFFSET 0) AS v) AS w)"
        )
        sql, params = self._record_locked(
            ts,
            [id.canonical],
            f"UPDATE {ts.qualified} SET {ca} = {clamp} "
            f'WHERE "_pk" = %s AND {cp} IS NOT NULL '
            f"RETURNING {cp}, {ca}, {ch}",
            [id.canonical],
        )
        rows, _ = self._run(sql, params, uow=uow, write=True)
        if not rows:
            return None
        return _rows_cycles(*rows[0])

    def _cycles_resolve_pressure(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        rate: float,
        at: float,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        """``resolve_pressure``'s blind ``HSET {rate, last_resolved: at}``."""
        ts = self._table(spec, write=True)
        pr, pa = (quote_ident(field + s) for s in PRESSURE_SUFFIXES)
        sql, params = self._record_locked(
            ts,
            [id.canonical],
            f"UPDATE {ts.qualified} SET {pr} = %s, {pa} = %s " f'WHERE "_pk" = %s',
            [float(rate), float(at), id.canonical],
        )
        self._run(sql, params, uow=uow, write=True)

    def _cycles_state(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[dict[str, Any]]:
        """The stored companion state, as the Redis hashes decode: ``cycles``
        (``[period, amplitude, phase(, baseline)]``, ``None`` = no entry)
        and ``pressure`` (``{"rate", "last_resolved"}`` or ``None``).
        ``None`` when the record does not exist."""
        ts = self._table(spec)
        cols = ", ".join(
            quote_ident(field + s) for s in CYCLE_SUFFIXES + PRESSURE_SUFFIXES
        )
        rows, _ = self._run(
            f'SELECT {cols} FROM {ts.qualified} WHERE "_pk" = %s',
            [id.canonical],
            uow=uow,
        )
        if not rows:
            return None
        periods, amps, phases, bases, rate, at = rows[0]
        return {
            "cycles": _rows_cycles(periods, amps, phases, bases),
            "pressure": (
                None
                if rate is None
                else {"rate": float(rate), "last_resolved": float(at)}
            ),
        }

    def _cycles_import(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        cycles: Optional[Sequence[Sequence[Any]]],
        pressure: Optional[dict[str, float]],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        """``import_state``'s raw writes: cycles as 3-element entries (no
        baseline, #698), pressure as given. ``None`` leaves that half
        untouched."""
        ts = self._table(spec, write=True)
        sets: list[str] = []
        params: list[Any] = []
        if cycles is not None:
            cp, ca, ch, cb = (quote_ident(field + s) for s in CYCLE_SUFFIXES)
            periods, amps, phases = [], [], []
            for cycle in cycles:
                values = [lua_tonumber(v) for v in cycle[:3]]
                if any(v is None for v in values):
                    raise ValueError(
                        f"cycle {tuple(cycle)!r}: Postgres stores numeric cycle "
                        "slots only"
                    )
                periods.append(values[0])
                amps.append(values[1])
                phases.append(values[2])
            sets.append(f"{cp} = %s, {ca} = %s, {ch} = %s, {cb} = %s")
            params += [periods, amps, phases, [None] * len(periods)]
        if pressure is not None:
            pr, pa = (quote_ident(field + s) for s in PRESSURE_SUFFIXES)
            sets.append(f"{pr} = %s, {pa} = %s")
            params += [float(pressure["rate"]), float(pressure["last_resolved"])]
        if not sets:
            return
        sql, all_params = self._record_locked(
            ts,
            [id.canonical],
            f"UPDATE {ts.qualified} SET {', '.join(sets)} WHERE \"_pk\" = %s",
            params + [id.canonical],
        )
        self._run(sql, all_params, uow=uow, write=True)

    # TDValueField --------------------------------------------------------------

    def _td_update(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        *,
        target: float,
        alpha: float,
        uow: Optional[UnitOfWork] = None,
    ) -> float:
        """``TD_UPDATE_LUA`` as one ``UPDATE``: ``td = target - q`` (``target``
        is the script's ``reward + gamma * max_future_q``, computed by the
        caller in the same order), ``q' = q + alpha * td``, stored as the
        ``numeric`` of ``tostring(q')`` -- Lua's ``%.14g`` -- with the
        trailing zeros of ``to_char``'s ``%.13e`` trimmed. Returns
        ``tostring(td)``. ``q`` is ``0`` when the column is ``NULL``. A
        record that does not exist is not written (Redis would leave a hash
        holding only the value) and the reply is the one ``q = 0`` gives."""
        ts = self._table(spec, write=True)
        col = quote_ident(field)
        # The constants enter as columns of a MATERIALIZED CTE, never as
        # literals in the arithmetic: the planner folds a constant
        # subexpression even inside a CASE arm that would not run, and the
        # clamped helpers' `x / 2` of a subnormal constant then raises
        # "underflow" at plan time.
        target_l = "c.tgt"
        nq = "e.nq"
        fmt = (
            f"(CASE WHEN {nq} = {_NAN} THEN 'NaN'::numeric "
            f"WHEN {nq} = {_INF} THEN 'Infinity'::numeric "
            f"WHEN {nq} = '-Infinity'::float8 THEN '-Infinity'::numeric "
            f"ELSE trim_scale(to_char({nq}, '9.9999999999999EEEE')::numeric) END)"
        )
        sql = (
            f'WITH c AS MATERIALIZED (SELECT t."_pk", '
            f"coalesce(t.{col}::float8, {_ZERO}) AS q, "
            f"{_lit(target)} AS tgt, {_lit(alpha)} AS alpha "
            f'FROM {ts.qualified} AS t WHERE t."_pk" = %s FOR UPDATE), '
            f'd AS (SELECT c."_pk", c.q, c.alpha, {safe_sub(target_l, "c.q")} AS td '
            f"FROM c), "
            f'm AS (SELECT d."_pk", d.q, d.td, {safe_mul("d.alpha", "d.td")} AS m '
            f"FROM d), "
            f'e AS (SELECT m."_pk", m.td, {safe_add("m.q", "m.m")} AS nq FROM m) '
            f"UPDATE {ts.qualified} AS t SET {col} = {fmt}, "
            f'"_updated_at" = now(), "_migrated_from" = NULL FROM e '
            f'WHERE t."_pk" = e."_pk" RETURNING e.td'
        )
        sql, params = self._record_locked(ts, [id.canonical], sql, [id.canonical])
        rows, _ = self._run(sql, params, uow=uow, write=True)
        if not rows:
            return lua_tostring(float(target) - 0.0)
        return lua_tostring(float(rows[0][0]))

    # PredictionLedgerMixin -----------------------------------------------------

    def _ledger_exists(
        self, spec: ModelSpec, id: RecordId, *, uow: Optional[UnitOfWork] = None
    ) -> bool:
        """``EXISTS <record key>``, read inside ``uow`` when given."""
        ts = self._table(spec)
        rows, _ = self._run(
            f'SELECT 1 FROM {ts.qualified} WHERE "_pk" = %s', [id.canonical], uow=uow
        )
        return bool(rows)

    def _ledger_record(
        self,
        spec: ModelSpec,
        id: RecordId,
        entry: dict[str, Any],
        *,
        uow: Optional[UnitOfWork] = None,
        lua_packed: bool = False,
    ) -> None:
        """``HSET $PL:{Class}:meta:{pk} {pk} <entry>``: the whole entry,
        replaced, behind the record's key lock (the ledger is that record's
        state)."""
        ts = self._table(spec, write=True)
        table = self._engine(LEDGER_TABLE)
        sql, params = self._record_locked(
            ts,
            [id.canonical],
            f"INSERT INTO {table} (model, member, entry, lua_packed) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (model, member) DO UPDATE "
            "SET entry = EXCLUDED.entry, lua_packed = EXCLUDED.lua_packed",
            [spec.name, id.canonical, _ledger_jsonb(entry), bool(lua_packed)],
        )
        self._run(sql, params, uow=uow, write=True)

    def _ledger_get(
        self,
        spec: ModelSpec,
        id: RecordId,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[dict[str, Any]]:
        """The entry as ``msgpack.unpackb(raw=False)`` returns it on Redis
        (``None`` when there is none): after a resolution, as the script's
        ``cmsgpack`` re-pack left it."""
        table = self._engine(LEDGER_TABLE)
        rows, _ = self._run(
            f"SELECT entry, lua_packed FROM {table} WHERE model = %s AND member = %s",
            [spec.name, id.canonical],
            uow=uow,
        )
        if not rows:
            return None
        return self._ledger_entry(rows[0][0], rows[0][1])

    @staticmethod
    def _ledger_entry(raw: Any, lua_packed: bool) -> Any:
        entry = _ledger_decode(raw)
        if lua_packed:
            entry = lua_repack(entry)
        _strict_keys(entry)
        return entry

    def _ledger_resolve(
        self,
        spec: ModelSpec,
        id: RecordId,
        *,
        part: str,
        error: float,
        mode: str,
        resolved_at: str,
        uow: Optional[UnitOfWork] = None,
    ) -> int:
        """``RESOLVE_PREDICTION_LUA`` as one statement: an entry not yet
        resolved -- Lua truthiness, so only an absent, ``nil`` or ``false``
        ``resolved`` -- is marked resolved with the error, mode and time,
        and the error sorted set gets ``|error|`` (``ZADD``). Returns ``1``,
        or ``0`` when there is no entry or it is resolved (or its
        ``cmsgpack.unpack`` would fail: a ``nil`` or ``NaN`` table key)."""
        if math.isnan(error):
            # The script's ZADD refuses a NaN score -- after its HSET, which
            # Redis does not roll back. Refused here before anything is
            # written (a documented divergence).
            raise ValueError("resulting score is not a number (NaN)")
        ts = self._table(spec, write=True)
        ledger = self._engine(LEDGER_TABLE)
        errors = self._engine(ERROR_TABLE)
        current = self._ledger_raw(spec, id, uow=uow)
        if current is None:
            return 0
        try:
            _lua_value(_ledger_decode(current[0]))
        except _LuaUnpackError:
            return 0
        lua_error = lua_num(float(error))
        patch = {
            "resolved": True,
            "prediction_error": lua_error,
            "resolution_mode": mode,
            "resolved_at": resolved_at,
        }
        sql = (
            f"WITH r AS (UPDATE {ledger} SET entry = entry || %s::jsonb, "
            "lua_packed = true WHERE model = %s AND member = %s "
            "AND jsonb_typeof(entry) = 'object' AND NOT entry ? %s "
            "AND (entry->'resolved' IS NULL "
            "OR entry->'resolved' IN ('null'::jsonb, 'false'::jsonb)) RETURNING 1) "
            f"INSERT INTO {errors} (model, part, member, error) "
            "SELECT %s, %s, %s, %s FROM r ON CONFLICT (model, part, member) "
            "DO UPDATE SET error = EXCLUDED.error RETURNING 1"
        )
        params = [
            _ledger_jsonb(patch),
            spec.name,
            id.canonical,
            _MAP,
            spec.name,
            str(part),
            id.canonical,
            abs(float(error)),
        ]
        sql, params = self._record_locked(ts, [id.canonical], sql, params)
        rows, _ = self._run(sql, params, uow=uow, write=True)
        return 1 if rows else 0

    def _ledger_raw(
        self, spec: ModelSpec, id: RecordId, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[tuple[Any, bool]]:
        table = self._engine(LEDGER_TABLE)
        rows, _ = self._run(
            f"SELECT entry, lua_packed FROM {table} WHERE model = %s AND member = %s",
            [spec.name, id.canonical],
            uow=uow,
        )
        return (rows[0][0], rows[0][1]) if rows else None

    def _ledger_highest(
        self,
        spec: ModelSpec,
        part: str,
        limit: int,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> list[tuple[str, float]]:
        """``ZREVRANGE errors 0 limit-1 WITHSCORES``: error descending, ties by
        member bytes descending, Redis's rank arithmetic (``limit <= 0``
        counts from the end)."""
        table = self._engine(ERROR_TABLE)
        sql = (
            f"SELECT member, error FROM {table} WHERE model = %s AND part = %s "
            'ORDER BY error DESC, member COLLATE "C" DESC'
        )
        params: list[Any] = [spec.name, str(part)]
        stop = int(limit) - 1
        if stop >= 0:
            sql += f" LIMIT {stop + 1}"
        rows, _ = self._run(sql, params, uow=uow)
        out = [(member, float(error)) for member, error in rows]
        if stop < 0:
            stop = len(out) + stop
            out = out[: stop + 1] if stop >= 0 else []
        return out

    def _ledger_rows(
        self,
        spec: ModelSpec,
        part: str,
        limit: int,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> list[tuple[str, float, Any]]:
        """``error_summary``'s reads: the top ``limit`` of the error set and
        each member's entry (``None`` when it has none; an entry that would
        not decode on Redis is skipped there, so it is reported as an
        exception object here for the caller to log)."""
        top = self._ledger_highest(spec, part, limit, uow=uow) if limit > 0 else []
        if not top:
            return []
        table = self._engine(LEDGER_TABLE)
        rows, _ = self._run(
            f"SELECT member, entry, lua_packed FROM {table} "
            "WHERE model = %s AND member = ANY(%s::text[])",
            [spec.name, [m for m, _ in top]],
            uow=uow,
        )
        entries = {member: (raw, packed) for member, raw, packed in rows}
        out: list[tuple[str, float, Any]] = []
        for member, score in top:
            found = entries.get(member)
            if found is None:
                out.append((member, score, None))
                continue
            try:
                out.append((member, score, self._ledger_entry(*found)))
            except Exception as exc:  # decode failure: reported, then skipped
                out.append((member, score, exc))
        return out

    def _ledger_import(
        self,
        spec: ModelSpec,
        id: RecordId,
        entry: dict[str, Any],
        part: Optional[str],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        """``import_state``: the entry written raw (Python msgpack on Redis,
        not the script's re-pack), and ``ZADD |prediction_error|`` when it is
        resolved."""
        self._ledger_record(spec, id, entry, uow=uow)
        error = entry.get("prediction_error")
        if entry.get("resolved") and error is not None:
            ts = self._table(spec, write=True)
            errors = self._engine(ERROR_TABLE)
            sql, params = self._record_locked(
                ts,
                [id.canonical],
                f"INSERT INTO {errors} (model, part, member, error) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (model, part, member) "
                "DO UPDATE SET error = EXCLUDED.error",
                [spec.name, str(part or "default"), id.canonical, abs(float(error))],
            )
            self._run(sql, params, uow=uow, write=True)


def longtail_field_call(
    backend: LongtailOpsMixin,
    spec: ModelSpec,
    field: str,
    op: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    uow: Optional[UnitOfWork],
) -> Any:
    return backend._longtail_field_call(spec, field, op, args, kwargs, uow)
