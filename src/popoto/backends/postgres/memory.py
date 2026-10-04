"""Ranking and memory state on Postgres (#759 M2a; plan §2 groups D and E).

This module is the Postgres half of Valor's ranking slice: the decay clock
and ``touch``, ``rank_decayed`` with base score and confidence modulation,
``ConfidenceField``'s capped-evidence update, ``AccessTrackerMixin``'s staged
and confirmed reads, ``rank_composite`` for the decay / confidence / access /
sorted arms, and the storage ``ObservationProtocol`` and ``RecallProposal``
need. :class:`PostgresMemoryOps` is mixed into
:class:`~popoto.backends.postgres.PostgresBackend`.

Storage (plan §3, "Field → column type mapping")
------------------------------------------------
* ``DecayingSortedField``: the field's own ``double precision`` column is the
  decay clock -- the epoch seconds Redis keeps as the member's sorted-set
  score. ``touch`` updates it (on Redis the hash keeps the save-time value
  while the sorted set moves; here a reload sees the touched time, a
  documented divergence). B-tree ``(partition cols…, f, _pk COLLATE "C")``.
* ``ConfidenceField``: the field's column keeps the model attribute, exactly
  as the Redis hash does, and four state columns replace the companion hash
  entry: ``<f>__conf``, ``<f>__n``, ``<f>__corr``, ``<f>__contra``. ``NULL``
  state is the seed ``on_save`` writes with ``HSETNX`` on Redis
  (``initial_confidence`` and three zeros), so a save never has to write it
  and a re-save can never reset it.
* ``AccessTrackerMixin``: ``_access_count``, ``_last_accessed``,
  ``_staged_reads`` and ``_staged_at``. Staged reads expire by comparison --
  they count only while ``_staged_at`` is within ``_staged_ttl_seconds``,
  which is what the Redis list's refreshed ``EXPIRE`` means. The confirmed
  access *log* (a capped Redis list) is not kept; count and last read are.
* ``RecallProposal``: one engine table per schema,
  ``popoto_recall_proposal (model, part, member, surfaced_at)``, created on
  first use like ``popoto_schema``.

No state column is indexed: each index would turn every
``update_confidence`` / ``confirm_access`` into a non-HOT update, and no query
filters or orders by one alone (the composite arms scan the model's rows).

The decay expression (the ``DECAY_SCORE_LUA`` oracle)
-----------------------------------------------------
Operation for operation, in ``double precision``, so the platform ``pow``
behind ``power()`` gives the Lua's bits::

    e  = greatest((now - f) / 86400, 0.01)
    s  = sign(b) * |b| * e ^ -rate
    s *= greatest(e, 1) ^ -(rate * 2 ^ (s2 * (c0 - c)) - rate)   -- modulation

where ``b`` is the base-score column (``1.0`` when unset, missing, ``NULL``
or not numeric, as the script's ``HGET`` + ``cmsgpack`` rule gives) and
``c`` the clamped stored confidence (``c0`` when there is none).

Postgres raises ``value out of range`` where C's ``pow`` and ``*`` overflow to
``inf`` or underflow to ``0`` (``power(0.01, -155)``, ``1e-300 * 1e-300``).
Every row whose inputs sit in a box where no operation can leave the
``double`` range is scored by the plain expression above. Any other row --
and every row when the call's constants are outside the box -- takes a
correlated scalar subquery that computes the same steps with each ``power``,
``*`` and ``-`` clamped: the exponent ``|y · ln x|`` is checked against the
double range first, so the result is ``inf`` / ``0`` (with its sign) where the
Lua's is, instead of an error. Inside a band of 0.003 in log space at each
end of the double range (results within 0.25% of ``DBL_MAX``, or between
``2.47e-324`` and ``2.48e-324``) the clamp saturates where the exact value
would still be finite; that is the only place the two legs can differ.

Scores come back rounded through ``%.14g`` -- what the Lua's ``tostring``
replies with -- so ``rank_decayed`` is score-identical to the Redis field
method. Ties break on ``_pk COLLATE "C"`` ascending, the script's comparator.
"""

from __future__ import annotations

import logging
import math
import random
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from ...fields.constants import Defaults
from ..types import (
    And,
    BackendCapabilityError,
    BackendRetryableError,
    Cond,
    ModelSpec,
    Op,
    Predicate,
    RankTerm,
    RecordId,
    Scored,
    UnitOfWork,
)
from .plan import render_where, to_column_value
from .schema import Column, TableSpec, quote_ident
from .validity import NOT_HANDLED as _VALIDITY_NOT_HANDLED
from .validity import (
    VALIDITY_KIND,
    PostgresValidityOps,
    included_sql,
    range_bound,
    validity_columns,
    validity_field_names,
    validity_indexes,
)

__all__ = [
    "ACCESS_FIELD",
    "CONF_SUFFIXES",
    "OBSERVE_FIELD",
    "RECALL_FIELD",
    "PostgresMemoryOps",
    "decay_score_sql",
    "lua_tostring",
    "memory_columns",
    "memory_indexes",
]

logger = logging.getLogger("POPOTO.postgres")

DECAY_KIND = "DecayingSortedField"
CONFIDENCE_KIND = "ConfidenceField"
SORTED_ARM_KINDS = frozenset({"SortedField", "SortedKeyField"})

#: State columns beside a ``ConfidenceField``'s own column.
CONF_SUFFIXES = ("__conf", "__n", "__corr", "__contra")

ACCESS_MIXIN = "AccessTrackerMixin"
ACCESS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("_access_count", "bigint"),
    ("_last_accessed", "double precision"),
    ("_staged_reads", "bigint"),
    ("_staged_at", "double precision"),
)

#: ``field_call`` pseudo-fields for model-level state (no Field instance).
ACCESS_FIELD = "_access"
RECALL_FIELD = "_recall"
OBSERVE_FIELD = "_observe"

RECALL_TABLE = "popoto_recall_proposal"

_NOT_HANDLED = object()

# -- float helpers ------------------------------------------------------------

_OVERFLOW_LN = "709.78"
"""Just under ``ln(DBL_MAX)`` = 709.7827: past it a result is ``inf``."""
_UNDERFLOW_LN = "745.13"
"""Just under ``-ln(2**-1075)`` = 745.1332: past it a result rounds to 0."""
_HALF_DBL_LIMIT = 2.0**1023


def lua_tostring(value: float) -> float:
    """``value`` as Lua 5.1's ``tostring`` renders it (``%.14g``), read back:
    what every number in a Redis Lua reply has been through."""
    return float("%.14g" % value)


def _lit(value: float) -> str:
    """A ``double precision`` literal, exact for every float (``repr`` round
    trips; the numeric literal converts with ``strtod``)."""
    value = float(value)
    if math.isnan(value):
        return "'NaN'::float8"
    if math.isinf(value):
        return "'Infinity'::float8" if value > 0 else "'-Infinity'::float8"
    return f"({value!r}::float8)"


_INF = "'Infinity'::float8"
_ZERO = "0::float8"
_ONE = "1::float8"


def _nonfinite(x: str) -> str:
    # In Postgres NaN sorts above every number, so this is "inf, -inf or NaN".
    return f"abs({x}) >= {_INF}"


def safe_pow(x: str, y: str) -> str:
    """``power(x, y)`` for ``x > 0`` that returns C's ``inf`` / ``0`` where
    Postgres would raise (``x`` and ``y`` are cheap SQL terms: column
    references or literals, each referenced several times)."""
    m = f"(least(greatest(abs({y}), 1e-300::float8), 1e300::float8)" f" * abs(ln({x})))"
    return (
        f"(CASE WHEN {x} = 'NaN'::float8 OR {y} = 'NaN'::float8"
        f" OR {x} = {_ONE} OR {y} = {_ZERO}"
        f" OR {_nonfinite(x)} OR {_nonfinite(y)} THEN power({x}, {y})"
        f" WHEN {m} > {_OVERFLOW_LN} AND ({y} > 0) = ({x} > 1) THEN {_INF}"
        f" WHEN {m} > {_UNDERFLOW_LN} AND ({y} > 0) <> ({x} > 1) THEN {_ZERO}"
        f" ELSE power({x}, {y}) END)"
    )


def safe_mul(a: str, b: str) -> str:
    """``a * b`` that saturates to a signed ``inf`` / ``0`` where Postgres
    would raise on overflow or underflow."""
    neg = f"(({a} < 0) <> ({b} < 0))"
    logsum = f"(ln(abs({a})) + ln(abs({b})))"
    return (
        f"(CASE WHEN {a} = {_ZERO} OR {b} = {_ZERO}"
        f" OR {_nonfinite(a)} OR {_nonfinite(b)} THEN {a} * {b}"
        f" WHEN {logsum} > {_OVERFLOW_LN}"
        f" THEN (CASE WHEN {neg} THEN '-Infinity'::float8 ELSE {_INF} END)"
        f" WHEN {logsum} < -{_UNDERFLOW_LN}"
        f" THEN (CASE WHEN {neg} THEN (-0.0)::float8 ELSE {_ZERO} END)"
        f" ELSE {a} * {b} END)"
    )


def safe_sub(a: str, b: str) -> str:
    """``a - b`` that saturates to ``±inf`` where Postgres would raise.
    Halving is exact for operands of magnitude >= 1e290, and their halved
    difference cannot overflow, so the test is exact."""
    half = _lit(_HALF_DBL_LIMIT)
    diff = f"({a} / 2::float8 - {b} / 2::float8)"
    return (
        f"(CASE WHEN {_nonfinite(a)} OR {_nonfinite(b)} THEN {a} - {b}"
        f" WHEN abs({a}) < 8e307::float8 AND abs({b}) < 8e307::float8"
        f" THEN {a} - {b}"
        f" WHEN least(abs({a}), abs({b})) < 1e290::float8 THEN {a} - {b}"
        f" WHEN {diff} >= {half} THEN {_INF}"
        f" WHEN {diff} <= -{half} THEN '-Infinity'::float8"
        f" ELSE {a} - {b} END)"
    )


# -- schema -------------------------------------------------------------------


def memory_columns(spec: ModelSpec) -> list[Column]:
    """State columns for the memory fields and mixins (role ``"state"``)."""
    out: list[Column] = []
    for name in sorted(spec.fields):
        if spec.fields[name].kind == CONFIDENCE_KIND:
            out.append(Column(name + "__conf", "double precision", name, "state"))
            out.extend(
                Column(name + suffix, "bigint", name, "state")
                for suffix in CONF_SUFFIXES[1:]
            )
    if ACCESS_MIXIN in spec.mixins:
        out.extend(Column(c, t, None, "state") for c, t in ACCESS_COLUMNS)
    # M3: a ValidityField's interval and chain-link columns (.validity).
    out.extend(validity_columns(spec))
    return out


def memory_indexes(
    spec: ModelSpec, index_name: Callable[..., str]
) -> list[tuple[str, str, bool]]:
    """A ``(partition cols…, f, _pk COLLATE "C")`` B-tree per decay clock,
    the shape a ``SortedField`` gets: it serves the partition filter of a
    ranking scan and a range query on the clock."""
    out = []
    for name, fs in sorted(spec.fields.items()):
        if fs.kind != DECAY_KIND:
            continue
        partition = tuple(fs.options.get("partition_by", ()) or ())
        cols = ", ".join(quote_ident(c) for c in partition + (name,))
        out.append((index_name("sort", name), f'({cols}, "_pk" COLLATE "C")', False))
    out.extend(validity_indexes(spec, index_name))
    return out


# -- the decay expression -----------------------------------------------------

_NUMERIC_BASE_TYPES = (int, float)


def _base_term(ts: TableSpec, spec: ModelSpec, base_field: Optional[str]) -> str:
    """The base score per row, as the Lua reads it: a number in the hash, else
    ``1.0`` (a missing field, ``None``, a string, a boolean...). A
    ``Decimal`` is the tagged envelope the script ``tonumber``s; ``numeric``
    converts to ``float8`` through the same ``strtod``."""
    import decimal

    if not base_field or base_field not in ts.field_types:
        return ""
    fs = spec.fields.get(base_field)
    if fs is None or fs.kind in ("TagField", "Relationship"):
        return ""
    py_type = ts.field_types[base_field]
    if py_type is bool or py_type not in _NUMERIC_BASE_TYPES + (decimal.Decimal,):
        return ""
    return f"coalesce(t.{quote_ident(base_field)}::float8, {_ONE})"


def partition_match_sql(
    ts: TableSpec, partition: Optional[Mapping[str, Any]], alias: str = "t"
) -> str:
    """``TRUE`` for a row in the partition a query names, as literals (the
    decay expression carries no parameters). A partitioned
    ``ConfidenceField`` keeps one companion hash per partition on Redis, and
    a query reads the one its filters name (#759 M3): a row outside it reads
    as having no confidence data. Empty when there is no partition."""
    if not partition:
        return ""
    from psycopg import sql

    terms = []
    for name, value in partition.items():
        column = f"{alias}.{quote_ident(name)}"
        stored = to_column_value(ts, name, value)
        if stored is None:
            terms.append(f"{column} IS NULL")
            continue
        sql_type = ts.column_map()[name]
        literal = sql.Literal(stored).as_string()
        terms.append(f"{column} = ({literal})::{sql_type}")
    return "(" + " AND ".join(terms) + ")"


def decay_score_sql(
    ts: TableSpec,
    spec: ModelSpec,
    field: str,
    *,
    now: float,
    rate: float,
    base_field: Optional[str],
    confidence_field: Optional[str],
    strength: float,
    clamped: bool = False,
    confidence_partition: Optional[Mapping[str, Any]] = None,
) -> str:
    """The ``DECAY_SCORE_LUA`` score of one row of ``ts`` (aliased ``t``), as
    one SQL expression with no parameters (every constant is an exact
    ``float8`` literal). ``clamped=True`` returns the clamped expression alone
    (the tests compare it with the plain one). ``confidence_partition`` is
    the partition a partitioned confidence field is read from: a row outside
    it modulates as if it had no confidence data (M3)."""
    f = f"t.{quote_ident(field)}"
    b = _base_term(ts, spec, base_field)
    modulate = bool(confidence_field) and strength != 0
    c0 = 0.5
    cc = ""
    if modulate:
        assert confidence_field is not None
        conf_spec = spec.fields[confidence_field]
        c0 = float(conf_spec.options.get("initial_confidence", 0.5))
        conf_col = f"t.{quote_ident(confidence_field + '__conf')}"
        match = partition_match_sql(ts, confidence_partition)
        if match:
            conf_col = f"(CASE WHEN {match} THEN {conf_col} END)"
        cc = f"greatest({_ZERO}, least({_ONE}, coalesce({conf_col}, {_lit(c0)})))"
    s2 = float(strength) * 2
    now_l, rate_l, neg_l = _lit(now), _lit(rate), _lit(-float(rate))
    c0_l, s2_l = _lit(c0), _lit(s2)

    # -- the plain expression ----------------------------------------------
    e = f"greatest(({now_l} - {f}) / 86400::float8, 0.01::float8)"
    plain = f"power({e}, {neg_l})"
    if b:
        sign = f"(CASE WHEN {b} < 0 THEN -1::float8 ELSE {_ONE} END)"
        plain = f"({sign} * abs({b}) * {plain})"
    if modulate:
        eff = f"({rate_l} * power(2::float8, {s2_l} * ({c0_l} - {cc})))"
        plain = f"({plain} * power(greatest({e}, {_ONE}), -({eff} - {rate_l})))"

    # -- the box inside which no step can leave the double range ------------
    finite = all(math.isfinite(v) for v in (now, rate, s2, c0))
    tame = (
        finite
        and 1.0 <= now <= 1e15
        and (rate == 0 or 1e-100 <= abs(rate) <= 10)
        and (not modulate or 1e-6 <= abs(s2) <= 2)
    )
    guards = [f"{f} BETWEEN {_lit(now - 86400.0 * 1e5)} AND 1e300::float8"]
    if b:
        guards.append(
            f"({b} = {_ZERO} OR abs({b}) BETWEEN 1e-40::float8 AND 1e40::float8)"
        )
    if modulate:
        guards.append(f"({cc} = {c0_l} OR abs({c0_l} - {cc}) >= 1e-300::float8)")

    # -- the clamped expression, one step per derived table -----------------
    levels: list[list[tuple[str, str]]] = []
    levels.append(
        [
            ("d", safe_sub(now_l, f)),
            (
                "sg",
                f"(CASE WHEN {b} < 0 THEN -1::float8 ELSE {_ONE} END)" if b else _ONE,
            ),
            ("mag", f"abs({b})" if b else _ONE),
        ]
        + ([("cc", cc)] if modulate else [])
    )
    keep = ["sg", "mag"] + (["cc"] if modulate else [])
    levels.append(
        [
            (
                "e",
                "greatest((CASE WHEN l0.d < 864::float8 THEN 0.01::float8"
                " ELSE l0.d / 86400::float8 END), 0.01::float8)",
            )
        ]
        + [(k, f"l0.{k}") for k in keep]
    )
    nxt = [("e", "l1.e"), ("sg", "l1.sg"), ("mag", "l1.mag")]
    nxt.append(("p1", safe_pow("l1.e", neg_l)))
    if modulate:
        nxt.append(("z", safe_mul(s2_l, f"({c0_l} - l1.cc)")))
    levels.append(nxt)
    nxt = [("e", "l2.e"), ("d0", f"(l2.sg * {safe_mul('l2.mag', 'l2.p1')})")]
    if modulate:
        nxt.append(("q", safe_pow("2::float8", "l2.z")))
    levels.append(nxt)
    if modulate:
        levels.append(
            [("e", "l3.e"), ("d0", "l3.d0"), ("eff", safe_mul(rate_l, "l3.q"))]
        )
        levels.append(
            [
                ("d0", "l4.d0"),
                ("g", f"greatest(l4.e, {_ONE})"),
                ("y2", f"(-{safe_sub('l4.eff', rate_l)})"),
            ]
        )
        levels.append([("d0", "l5.d0"), ("p2", safe_pow("l5.g", "l5.y2"))])
        levels.append([("s", safe_mul("l6.d0", "l6.p2"))])
    else:
        levels.append([("s", "l3.d0")])
    sql = ""
    for depth, cols in enumerate(levels):
        select = ", ".join(f"{expr} AS {name}" for name, expr in cols)
        if depth == 0:
            sql = f"SELECT {select} OFFSET 0"
        else:
            sql = f"SELECT {select} FROM ({sql}) AS l{depth - 1} OFFSET 0"
    last = len(levels) - 1
    clamped_sql = f"(SELECT l{last}.s FROM ({sql}) AS l{last})"
    if clamped or not tame:
        return clamped_sql
    return f"(CASE WHEN {' AND '.join(guards)} THEN {plain} ELSE {clamped_sql} END)"


# -- the backend half -----------------------------------------------------------


def _sort_key(key: str) -> bytes:
    """``COLLATE "C"`` order: the UTF-8 bytes."""
    return key.encode("utf-8", "surrogateescape")


class PostgresMemoryOps(PostgresValidityOps):
    """Protocol groups D/E (memory state, ranking) for
    :class:`~popoto.backends.postgres.PostgresBackend`. Relies on the
    backend's ``_table``, ``_run``, ``transaction`` and ``schema``.
    ``supersede``, ``chain`` and the validity adapters come from
    :class:`~.validity.PostgresValidityOps` (M3)."""

    # Provided by PostgresBackend.
    schema: str
    _table: Callable[..., TableSpec]
    _run: Callable[..., Any]
    transaction: Callable[..., Any]

    # -- D. memory state --------------------------------------------------------

    def touch(
        self,
        spec: ModelSpec,
        id: RecordId,
        field: str,
        *,
        at: float,
        uow: Optional[UnitOfWork] = None,
    ) -> float:
        """``UPDATE … SET f = $at``: the decay clock moves, as ``ZADD`` moves
        the member's score. A record that no longer exists is left alone
        (Redis would re-add an orphan member, which ranking then drops)."""
        ts = self._table(spec, write=True)
        col = quote_ident(field)
        self._run(
            f'UPDATE {ts.qualified} SET {col} = %s, "_updated_at" = now() '
            f'WHERE "_pk" = %s',
            [float(at), id.canonical],
            uow=uow,
            write=True,
        )
        return float(at)

    def update_confidence(
        self,
        spec: ModelSpec,
        id: RecordId,
        field: str,
        signal: float,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[dict[str, Any]]:
        """``CAPPED_BAYESIAN_UPDATE_LUA`` as one ``UPDATE … RETURNING``.

        ``n_eff = min(n + 1, cap)``; ``c' = clamp01(c + (signal - c) /
        (n_eff + 1))``; one more corroboration when ``signal >= 0.5``, else a
        contradiction. Returns the new state (``confidence`` rounded through
        ``%.14g``, as the script's reply is), or ``None`` when the record does
        not exist -- the script's ``EXISTS`` guard."""
        fs = spec.fields.get(field)
        if fs is None or fs.kind != CONFIDENCE_KIND:
            raise TypeError(f"{field} is not a ConfidenceField")
        ts = self._table(spec, write=True)
        ic = _lit(float(fs.options.get("initial_confidence", 0.5)))
        cap = int(fs.options.get("evidence_cap", Defaults.CONFIDENCE_EVIDENCE_CAP))
        sig = _lit(float(signal))
        conf, n, corr, contra = (quote_ident(field + s) for s in CONF_SUFFIXES)
        c = f"coalesce({conf}, {ic})"
        k = f"(least(coalesce({n}, 0) + 1, {cap}) + 1)"
        d = f"({sig} - {c})"
        # The quotient underflows to 0 in C, which Postgres refuses: below
        # half the least subnormal per unit of k, add a signed zero instead.
        step = (
            f"(CASE WHEN abs({d}) * 2::float8 <= {k} * {_lit(5e-324)}"
            f" THEN (CASE WHEN {d} < 0 THEN (-0.0)::float8 ELSE {_ZERO} END)"
            f" ELSE {d} / {k} END)"
        )
        corroborates = float(signal) >= 0.5
        sql = (
            f"UPDATE {ts.qualified} SET "
            f"{conf} = greatest({_ZERO}, least({_ONE}, {c} + {step})), "
            f"{n} = coalesce({n}, 0) + 1, "
            f"{corr} = coalesce({corr}, 0) + {1 if corroborates else 0}, "
            f"{contra} = coalesce({contra}, 0) + {0 if corroborates else 1}, "
            f'"_updated_at" = now() WHERE "_pk" = %s '
            f"RETURNING {conf}, {n}, {corr}, {contra}"
        )
        rows, _ = self._run(sql, [id.canonical], uow=uow, write=True)
        if not rows:
            return None
        value, evidence, corroborations, contradictions = rows[0]
        return {
            "confidence": lua_tostring(value),
            "evidence_count": int(evidence),
            "corroborations": int(corroborations),
            "contradictions": int(contradictions),
        }

    # -- E. ranking -------------------------------------------------------------

    def _decay_field(self, spec: ModelSpec, field: str) -> Any:
        fs = spec.fields.get(field)
        if fs is None or fs.kind != DECAY_KIND:
            raise BackendCapabilityError(
                f"{spec.name}.{field} is not a DecayingSortedField"
            )
        return fs

    def rank_decayed(
        self,
        spec: ModelSpec,
        field: str,
        *,
        now: float,
        n: Optional[int],
        where: Optional[Predicate] = None,
        as_of: Optional[float] = None,
        decay_rate: Optional[float] = None,
        base_score_field: Optional[str] = None,
        confidence_field: Optional[str] = None,
        validity_field: Optional[str] = None,
        confidence_partition: Optional[Mapping[str, Any]] = None,
    ) -> Scored:
        """``SELECT _pk, <decay> s … ORDER BY s DESC, _pk COLLATE "C" LIMIT n``.

        ``where`` scopes the scan (the partition ``top_by_decay`` resolves);
        ``n=None`` ranks every record; ``confidence_field`` turns modulation
        on with ``Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH`` read now,
        read from ``confidence_partition`` when that field is partitioned.
        ``validity_field`` gates the scan at ``as_of`` (``None`` = now) with
        the exclusion rule ``DECAY_SCORE_LUA`` applies (M3): a record closed
        at or before ``as_of``, or starting after it, is not ranked; a record
        with no interval is."""
        fs = self._decay_field(spec, field)
        if validity_field:
            self._validity_field(spec, validity_field)
        if n is not None and n <= 0:
            return []
        ts = self._table(spec)
        rate = float(
            decay_rate
            if decay_rate is not None
            else fs.options.get("decay_rate", Defaults.DECAY_RATE)
        )
        expr = decay_score_sql(
            ts,
            spec,
            field,
            now=float(now),
            rate=rate,
            base_field=base_score_field,
            confidence_field=confidence_field,
            strength=float(Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH),
            confidence_partition=confidence_partition,
        )
        kinds = {name: f.kind for name, f in spec.fields.items()}
        where_sql, params = render_where(ts, kinds, where)
        clause = f"t.{quote_ident(field)} IS NOT NULL"
        if where_sql:
            clause += " AND " + where_sql[len(" WHERE ") :]
        if validity_field:
            gate_at = time.time() if as_of is None else float(as_of)
            gate = included_sql(validity_field, gate_at)
            if gate != "TRUE":
                clause += " AND " + gate
        # ``OFFSET 0`` fences the subquery: without it the planner flattens
        # it and copies the score expression into both sort keys (the NaN
        # test and the ``DESC`` key), evaluating it twice per row (#773
        # review). The fence changes how often it runs, never its value.
        sql = (
            f'SELECT r."_pk", r."_score" FROM (SELECT t."_pk", {expr} AS "_score" '
            f"FROM {ts.qualified} AS t WHERE {clause} OFFSET 0) AS r "
            # A NaN score (0 * inf) has no place in the Lua's comparator --
            # `x > nan` is always false, so the script leaves it wherever its
            # sort happens to -- and Postgres would rank it first; it goes last.
            f"ORDER BY (r.\"_score\" = 'NaN'::float8), "
            f'r."_score" DESC, r."_pk" COLLATE "C"'
        )
        if n is not None:
            sql += f" LIMIT {int(n)}"
        rows, _ = self._run(sql, params)
        return [
            (RecordId(spec.name, (), pk, native=pk), lua_tostring(score))
            for pk, score in rows
        ]

    def _arm(
        self,
        ts: TableSpec,
        spec: ModelSpec,
        term: RankTerm,
        kinds: Mapping[str, str],
        now: float,
    ) -> tuple[str, str, list[Any]]:
        """``(domain, score, domain params)`` for one composite arm."""
        domain_sql, params = render_where(ts, dict(kinds), term.where)
        domain = domain_sql[len(" WHERE ") :] if domain_sql else "TRUE"
        if term.kind == "decay":
            assert term.field is not None
            fs = self._decay_field(spec, term.field)
            score = decay_score_sql(
                ts,
                spec,
                term.field,
                now=now,
                rate=float(fs.options.get("decay_rate", Defaults.DECAY_RATE)),
                base_field=fs.options.get("base_score_field"),
                confidence_field=term.options.get("confidence_field"),
                strength=float(Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH),
                confidence_partition=term.options.get("confidence_partition"),
            )
            col = f"t.{quote_ident(term.field)}"
            return f"({col} IS NOT NULL AND {domain})", score, params
        if term.kind == "confidence":
            assert term.field is not None
            fs = spec.fields[term.field]
            ic = _lit(float(fs.options.get("initial_confidence", 0.5)))
            conf = f"t.{quote_ident(term.field + '__conf')}"
            return f"({domain})", f"coalesce({conf}, {ic})", params
        if term.kind == "access":
            return (
                f'(coalesce(t."_access_count", 0) > 0 AND {domain})',
                't."_access_count"::float8',
                params,
            )
        if term.kind == "sorted":
            assert term.field is not None
            col = f"t.{quote_ident(term.field)}"
            return f"({col} IS NOT NULL AND {domain})", f"{col}::float8", params
        if term.kind == "similarity" and term.scores is not None:
            # A caller-supplied {key: score} arm (``semantic_search``'s
            # similarity_boost, #759 M3): the scores ride in a CTE that
            # rank_composite names after the arm, so the arm itself carries
            # no parameters. Keys with no record are not ranked (Redis ranks
            # them and then hydrates nothing for them).
            cte = term.options["cte"]
            return (
                f't."_pk" IN (SELECT k FROM {cte})',
                f'(SELECT v.s FROM {cte} AS v WHERE v.k = t."_pk")',
                [],
            )
        raise BackendCapabilityError(
            f"composite_score arm {term.kind!r} is not available on Postgres yet "
            "(similarity arrives with the M2 vector work, co_occurrence_boost in "
            "M4)"
        )

    def rank_composite(
        self,
        spec: ModelSpec,
        terms: Sequence[RankTerm],
        *,
        limit: int,
        aggregate: str,
        min_score: Optional[float],
        where: Optional[Predicate],
        as_of: Optional[float],
        temperature: float,
        validity_field: Optional[str] = None,
    ) -> Scored:
        """``composite_score`` as one ``SELECT``: each arm is a score over its
        own domain (the set its ``ZUNIONSTORE`` input would hold), a record is
        ranked when any arm holds it, and the aggregate runs over the arms
        that do -- ``SUM`` in the arms' order, ``MAX``/``MIN`` ignoring the
        rest. Ordered as ``ZREVRANGE`` orders: score, then ``_pk`` descending
        (``COLLATE "C"``). ``min_score`` is applied before ``temperature``
        divides the scores, as on Redis. ``validity_field`` is the validity
        mask (M3): a record the exclusion rule drops at ``as_of`` (``None`` =
        now) is not ranked, whichever arms hold it -- the ``ZDIFFSTORE`` the
        Redis path applies after the union."""
        if limit <= 0 or not terms:
            return []
        ts = self._table(spec)
        kinds = {name: f.kind for name, f in spec.fields.items()}
        now = time.time()
        ctes: list[str] = []
        cte_params: list[Any] = []
        arm_terms: list[RankTerm] = []
        for i, term in enumerate(terms):
            if term.kind == "similarity" and term.scores is not None:
                # Each caller-supplied arm is one CTE of (key, score), as the
                # Redis path ZADDs it into a temp sorted set.
                cte = f"arm{i}"
                ctes.append(
                    f"{cte}(k, s) AS (SELECT * FROM unnest(%s::text[], %s::float8[]))"
                )
                items = [(str(k), float(v)) for k, v in term.scores.items()]
                cte_params.append([k for k, _v in items])
                cte_params.append([v for _k, v in items])
                term = RankTerm(
                    term.kind,
                    term.weight,
                    term.field,
                    term.where,
                    term.scores,
                    dict(term.options, cte=cte),
                )
            arm_terms.append(term)
        terms = arm_terms
        arms = [self._arm(ts, spec, term, kinds, now) for term in terms]
        select = []
        params: list[Any] = []
        for i, (domain, score, dparams) in enumerate(arms):
            select.append(f"CASE WHEN {domain} THEN {score} END AS a{i}")
            params.extend(dparams)
        where_parts = []
        for domain, _score, dparams in arms:
            where_parts.append(domain)
            params.extend(dparams)
        inner_where = " OR ".join(where_parts)
        if validity_field:
            self._validity_field(spec, validity_field)
            gate = included_sql(
                validity_field, now if as_of is None else range_bound(as_of)
            )
            if gate != "TRUE":
                inner_where = f"({inner_where}) AND {gate}"
        if where is not None:
            gsql, gparams = render_where(ts, kinds, where)
            inner_where = f"({inner_where}) AND {gsql[len(' WHERE '):]}"
            params.extend(gparams)
        weighted = []
        for i, term in enumerate(terms):
            v = safe_mul(_lit(float(term.weight)), f"x.a{i}")
            # ZUNIONSTORE turns a NaN product (inf * 0) into 0.
            weighted.append(
                f"(CASE WHEN x.a{i} IS NULL THEN NULL ELSE "
                f"(CASE WHEN {v} = 'NaN'::float8 THEN {_ZERO} ELSE {v} END) END)"
            )
        agg = aggregate.upper()
        named = [f"w.w{i}" for i in range(len(weighted))]
        if agg == "SUM":
            # ZUNIONSTORE adds the arms one at a time and turns a NaN sum
            # (inf + -inf) into 0 at each step.
            total = f"coalesce({named[0]}, {_ZERO})"
            for w in named[1:]:
                step = f"({total} + coalesce({w}, {_ZERO}))"
                total = (
                    f"(CASE WHEN {step} = 'NaN'::float8 THEN {_ZERO} ELSE {step} END)"
                )
        elif agg == "MAX":
            total = f"greatest({', '.join(named)})"
        elif agg == "MIN":
            total = f"least({', '.join(named)})"
        else:
            raise ValueError(f"aggregate must be SUM, MIN or MAX (got {aggregate!r})")
        weights = ", ".join(f"{w} AS w{i}" for i, w in enumerate(weighted))
        sql = (
            f'SELECT y."_pk", y."_score" FROM (SELECT w."_pk", {total} AS "_score" '
            f'FROM (SELECT x."_pk", {weights} '
            f'FROM (SELECT t."_pk", {", ".join(select)} FROM {ts.qualified} AS t '
            f"WHERE {inner_where} OFFSET 0) AS x OFFSET 0) AS w) AS y"
        )
        if ctes:
            sql = f"WITH {', '.join(ctes)} {sql}"
            params = cte_params + params
        if min_score is not None:
            sql += f' WHERE y."_score" >= {_lit(float(min_score))}'
        sql += f' ORDER BY y."_score" DESC, y."_pk" COLLATE "C" DESC LIMIT {int(limit)}'
        rows, _ = self._run(sql, params)
        out: Scored = []
        for pk, score in rows:
            value = float(score)
            if temperature != 1.0:
                value = value / temperature
            out.append((RecordId(spec.name, (), pk, native=pk), value))
        return out

    # -- field_call adapters (plan §2 H, the field-adapter registry) -----------

    def _memory_field_call(
        self,
        spec: ModelSpec,
        field: str,
        op: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        uow: Optional[UnitOfWork],
    ) -> Any:
        """Adapters for the memory fields and mixins; ``_NOT_HANDLED`` for an
        op this module does not register."""
        handlers: dict[str, Callable[..., Any]] = {}
        if field == ACCESS_FIELD and ACCESS_MIXIN in spec.mixins:
            handlers = {
                "stage": self._access_stage,
                "confirm": self._access_confirm,
                "discard": self._access_discard,
                "state": self._access_state,
            }
        elif field == RECALL_FIELD:
            handlers = {
                "add": self._recall_add,
                "remove": self._recall_remove,
                "expire": self._recall_expire,
                "pending": self._recall_pending,
            }
        handler = handlers.get(op)
        if handler is not None:
            return handler(spec, *args, uow=uow, **kwargs)
        if field == OBSERVE_FIELD:
            if op == "lock":
                return self._lock_rows(spec, *args, uow=uow)
            if op == "atomically":
                return self._atomically(*args, uow=uow)
        fs = spec.fields.get(field)
        if fs is not None and fs.kind == CONFIDENCE_KIND and op == "state":
            return self._confidence_state(spec, field, *args)
        if fs is not None and fs.kind == VALIDITY_KIND:
            result = self._validity_field_call(spec, field, op, args, kwargs, uow)
            if result is not _VALIDITY_NOT_HANDLED:
                return result
        return _NOT_HANDLED

    def _confidence_state(
        self, spec: ModelSpec, field: str, id: RecordId
    ) -> Optional[dict[str, Any]]:
        """The stored state, or ``None`` when the record does not exist.
        ``NULL`` state is the seed: ``initial_confidence`` and three zeros."""
        ts = self._table(spec)
        fs = spec.fields[field]
        ic = float(fs.options.get("initial_confidence", 0.5))
        cols = ", ".join(quote_ident(field + s) for s in CONF_SUFFIXES)
        rows, _ = self._run(
            f'SELECT {cols} FROM {ts.qualified} WHERE "_pk" = %s', [id.canonical]
        )
        if not rows:
            return None
        conf, n, corr, contra = rows[0]
        return {
            "confidence": ic if conf is None else float(conf),
            "evidence_count": int(n or 0),
            "corroborations": int(corr or 0),
            "contradictions": int(contra or 0),
        }

    # AccessTrackerMixin ------------------------------------------------------

    def _access_stage(
        self,
        spec: ModelSpec,
        counts: Mapping[str, int],
        *,
        now: float,
        ttl: float,
        uow: Optional[UnitOfWork] = None,
    ) -> int:
        """Stage reads: ``counts`` maps a key to how many reads it gets (the
        ``RPUSH`` count). Staged reads still within ``ttl`` of the last one
        accumulate; older ones were dropped by the Redis key's ``EXPIRE`` and
        start over. Rows lock in ``_pk`` order."""
        if not counts:
            return 0
        ts = self._table(spec, write=True)
        keys = sorted(counts, key=_sort_key)
        cut = _lit(float(now) - float(ttl))
        sql = (
            f'WITH locked AS (SELECT "_pk" FROM {ts.qualified} '
            f'WHERE "_pk" = ANY(%s::text[]) ORDER BY "_pk" COLLATE "C" FOR UPDATE), '
            f"v AS (SELECT * FROM unnest(%s::text[], %s::bigint[]) AS v(pk, n)) "
            f"UPDATE {ts.qualified} AS t SET "
            f'"_staged_reads" = CASE WHEN t."_staged_at" > {cut} '
            f'THEN coalesce(t."_staged_reads", 0) + v.n ELSE v.n END, '
            f'"_staged_at" = {_lit(float(now))} '
            f'FROM locked JOIN v ON v.pk = locked."_pk" WHERE t."_pk" = locked."_pk"'
        )
        _, count = self._run(
            sql, [keys, keys, [int(counts[k]) for k in keys]], uow=uow, write=True
        )
        return int(count or 0)

    def _access_confirm(
        self,
        spec: ModelSpec,
        id: RecordId,
        *,
        now: float,
        ttl: float,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[int]:
        """``CONFIRM_ACCESS_LUA``: promote the live staged reads into the
        count, move ``last_accessed`` to the latest one, clear the stage.
        Returns how many were promoted, or ``None`` when the record does not
        exist."""
        ts = self._table(spec, write=True)
        cut = _lit(float(now) - float(ttl))
        live = f'(t."_staged_at" > {cut})'
        promoted = f'(CASE WHEN {live} THEN coalesce(t."_staged_reads", 0) ELSE 0 END)'
        sql = (
            f"UPDATE {ts.qualified} AS t SET "
            f'"_access_count" = coalesce(t."_access_count", 0) + {promoted}, '
            f'"_last_accessed" = CASE WHEN {promoted} > 0 THEN t."_staged_at" '
            f'ELSE t."_last_accessed" END, '
            f'"_staged_reads" = NULL, "_staged_at" = NULL '
            f'WHERE t."_pk" = %s RETURNING CASE WHEN old."_staged_at" > {cut} '
            f'THEN coalesce(old."_staged_reads", 0) ELSE 0 END'
        )
        rows, _ = self._run(sql, [id.canonical], uow=uow, write=True)
        if not rows:
            return None
        return int(rows[0][0])

    def _access_discard(
        self, spec: ModelSpec, id: RecordId, *, uow: Optional[UnitOfWork] = None
    ) -> None:
        ts = self._table(spec, write=True)
        self._run(
            f'UPDATE {ts.qualified} SET "_staged_reads" = NULL, "_staged_at" = NULL '
            f'WHERE "_pk" = %s AND "_staged_at" IS NOT NULL',
            [id.canonical],
            uow=uow,
            write=True,
        )

    def _access_state(
        self,
        spec: ModelSpec,
        id: RecordId,
        *,
        now: float,
        ttl: float,
        uow: Optional[UnitOfWork] = None,
    ) -> tuple[int, Optional[float], int]:
        """``(access_count, last_accessed, live staged reads)``; a missing
        record reads as never accessed, as a missing meta hash does."""
        ts = self._table(spec)
        cut = _lit(float(now) - float(ttl))
        rows, _ = self._run(
            f'SELECT coalesce("_access_count", 0), "_last_accessed", '
            f'CASE WHEN "_staged_at" > {cut} THEN coalesce("_staged_reads", 0) '
            f'ELSE 0 END FROM {ts.qualified} WHERE "_pk" = %s',
            [id.canonical],
            uow=uow,
        )
        if not rows:
            return 0, None, 0
        count, last, staged = rows[0]
        return int(count), (None if last is None else float(last)), int(staged)

    # RecallProposal ----------------------------------------------------------

    def _recall_table(self) -> str:
        """``<schema>.popoto_recall_proposal``, created on first use in this
        process (like ``popoto_schema``)."""
        qualified = f"{quote_ident(self.schema)}.{quote_ident(RECALL_TABLE)}"
        if getattr(self, "_recall_ready", False):
            return qualified
        from . import _schema_auto

        rows, _ = self._run(
            "SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename = %s",
            [self.schema, RECALL_TABLE],
        )
        if not rows:
            if not _schema_auto():
                from ..types import SchemaDriftError

                raise SchemaDriftError(
                    f"{self.schema}.{RECALL_TABLE} does not exist and "
                    "POPOTO_SCHEMA_AUTO=0"
                )
            self._run(
                "SELECT pg_advisory_xact_lock(hashtext(%s)); "
                f"CREATE SCHEMA IF NOT EXISTS {quote_ident(self.schema)}; "
                f"CREATE TABLE IF NOT EXISTS {qualified} (model text NOT NULL, "
                "part text NOT NULL, member text NOT NULL, "
                "surfaced_at double precision NOT NULL, "
                "PRIMARY KEY (model, part, member))",
                [f"popoto:ddl:{self.schema}.{RECALL_TABLE}"],
                write=True,
            )
        self._recall_ready = True
        return qualified

    def _recall_add(
        self,
        spec: ModelSpec,
        part: str,
        members: Sequence[str],
        now: float,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        if not members:
            return
        table = self._recall_table()
        unique = sorted(dict.fromkeys(members), key=_sort_key)
        self._run(
            f"INSERT INTO {table} (model, part, member, surfaced_at) "
            f"SELECT %s, %s, m, %s FROM unnest(%s::text[]) AS m "
            "ON CONFLICT (model, part, member) DO UPDATE "
            "SET surfaced_at = EXCLUDED.surfaced_at",
            [spec.name, part, float(now), unique],
            uow=uow,
            write=True,
        )

    def _recall_remove(
        self,
        spec: ModelSpec,
        part: str,
        member: str,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> int:
        table = self._recall_table()
        _, count = self._run(
            f"DELETE FROM {table} WHERE model = %s AND part = %s AND member = %s",
            [spec.name, part, member],
            uow=uow,
            write=True,
        )
        return int(count or 0)

    def _recall_expire(
        self,
        spec: ModelSpec,
        part: str,
        cutoff: float,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> list[str]:
        """Remove proposals surfaced at or before ``cutoff``; their members,
        in ``ZRANGEBYSCORE`` order (score, then member bytes)."""
        table = self._recall_table()
        rows, _ = self._run(
            f"DELETE FROM {table} WHERE model = %s AND part = %s "
            f"AND surfaced_at <= %s RETURNING member, surfaced_at",
            [spec.name, part, float(cutoff)],
            uow=uow,
            write=True,
        )
        ordered = sorted(rows, key=lambda r: (r[1], _sort_key(r[0])))
        return [member for member, _score in ordered]

    def _recall_pending(
        self, spec: ModelSpec, part: str, *, uow: Optional[UnitOfWork] = None
    ) -> list[tuple[str, float]]:
        table = self._recall_table()
        rows, _ = self._run(
            f"SELECT member, surfaced_at FROM {table} WHERE model = %s AND part = %s "
            'ORDER BY surfaced_at, member COLLATE "C"',
            [spec.name, part],
            uow=uow,
        )
        return [(member, float(score)) for member, score in rows]

    # ObservationProtocol -----------------------------------------------------

    def _lock_rows(
        self,
        spec: ModelSpec,
        ids: Sequence[RecordId],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> list[str]:
        """``SELECT … FOR UPDATE`` in ``_pk`` order: the row-lock half of the
        plan's §6 lock order, taken before any effect writes."""
        if not ids:
            return []
        ts = self._table(spec, write=True)
        # M3: a model with a ValidityField takes its (model, field) lock
        # first, the order every supersede takes them in, so a contradicted
        # outcome's supersession cannot deadlock against a concurrent one.
        self._validity_lock(ts, validity_field_names(spec), uow)
        keys = sorted({rid.canonical for rid in ids}, key=_sort_key)
        rows, _ = self._run(
            f'SELECT "_pk" FROM {ts.qualified} WHERE "_pk" = ANY(%s::text[]) '
            'ORDER BY "_pk" COLLATE "C" FOR UPDATE',
            [keys],
            uow=uow,
            write=True,
        )
        return [row[0] for row in rows]

    def _atomically(
        self, work: Callable[[UnitOfWork], Any], *, uow: Optional[UnitOfWork] = None
    ) -> Any:
        """Run ``work(uow)`` in one transaction. On a deadlock or
        serialization failure the transaction is retried up to
        ``Defaults.PG_TRANSACTION_RETRIES`` times with jitter, then
        :class:`BackendRetryableError` is raised. Inside a caller's own unit
        of work there is no retry: the failure is raised as
        :class:`BackendRetryableError` for the caller to retry."""
        from . import _import_psycopg, _pg_uow, _retryable, _rollback_errors

        psycopg = _import_psycopg()
        if _pg_uow(uow) is not None:
            try:
                return work(uow)  # type: ignore[arg-type]
            except _rollback_errors(psycopg) as exc:
                raise _retryable(exc) from exc
        retries = int(Defaults.PG_TRANSACTION_RETRIES)
        attempt = 0
        while True:
            try:
                with self.transaction() as tx:
                    return work(tx)
            except BackendRetryableError as exc:
                # ``transaction()`` turns a rollback anywhere in its block --
                # ``_run``, a raw one from ``work`` itself, or the COMMIT --
                # into this type, chained from the driver error.
                cause = exc.__cause__ or exc
                attempt += 1
                if attempt > retries:
                    raise _retryable(cause, attempt) from cause
                time.sleep(random.uniform(0.005, 0.05) * attempt)


def memory_field_call(
    backend: PostgresMemoryOps,
    spec: ModelSpec,
    field: str,
    op: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    uow: Optional[UnitOfWork],
) -> Any:
    return backend._memory_field_call(spec, field, op, args, kwargs, uow)


def partition_where(partition: Mapping[str, Any]) -> Optional[Predicate]:
    """``AND`` of ``field = value`` for a partition mapping (``None`` when
    empty)."""
    conds = [Cond(name, Op.EXACT, value) for name, value in partition.items()]
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else And(tuple(conds))


NOT_HANDLED = _NOT_HANDLED
