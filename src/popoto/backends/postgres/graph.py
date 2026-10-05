"""Co-occurrence edges and graph expansion on Postgres (#759 M4; plan §2 group F).

``CoOccurrenceField`` keeps, on Redis, one sorted set per source key,
``$CoOcF:<Model>:<field>:<src>`` -> ``{dst: weight}``, written by
``LINK_WITH_PRUNE_LUA`` / ``STRENGTHEN_CLAMP_LUA`` / ``WEAKEN_ALL_LUA`` and
walked by ``PROPAGATE_BFS_LUA``. Here each field has one companion table::

    <table>__<field>__edge (src text, dst text, weight double precision,
                            PRIMARY KEY (src, dst))

with one row per directed edge -- the members of ``src``'s sorted set. A
symmetric field writes both directions, as the Lua writes both sets, so the
two weights of a pair can differ exactly as they can on Redis (a prune or a
``weaken_all`` touches one side only). There is no foreign key: Redis links
any two key strings, records or not. Every tie-break is ``COLLATE "C"``,
the sorted set's bytewise member order.

Writes (``graph_update``)
-------------------------
Each op is one statement, behind the record-key advisory locks of the edge
sets it writes (``src``, and ``dst`` when symmetric), sorted by ``_pk``: the
backend's one lock order (plan §6). The lock is what makes a link's
count-then-prune atomic, as the Lua is. A record delete on a model with a
symmetric field also writes its partners' sets (the reverse edges), so it
takes the partners' record-key locks too, in the same sorted sequence
(:func:`graph_delete_lock_sql`).

* ``link`` -- ``LINK_WITH_PRUNE_LUA`` per direction: an existing edge keeps
  its weight and nothing is pruned; a new one is added, and when the set
  then holds more than ``max_edges`` the lowest ``count - max_edges`` by
  ``(weight, dst)`` are removed -- the new edge among them when it ranks
  lowest (it is then never inserted). The reply is the Lua number reply
  Redis sends, an *integer*: the weight truncated toward zero.
* ``strengthen`` -- ``STRENGTHEN_CLAMP_LUA``: ``min(old + delta, cap)`` with
  a missing edge as ``0``, written without a prune (as on Redis); the reply
  is ``tostring(new)`` (Lua's ``%.14g``).
* ``unlink`` -- ``ZREM`` per direction.
* ``weaken`` -- ``WEAKEN_ALL_LUA``: every edge of ``src`` times ``factor``,
  an edge below ``0.001`` afterwards removed; the reply is how many were.
* ``replace`` -- ``import_state``: the edge set replaced wholesale. A NaN
  weight is refused before any write (on Redis the ``DELETE`` has already
  emptied the set when ``ZADD`` refuses it -- a documented divergence).

The arithmetic is the Lua's, in ``double precision``. Postgres raises where C
overflows or underflows, so the statements guard the one product that can
(``weight * factor`` underflowing, whose edge is removed either way), and an
input outside the box where no step can leave the double range (a
non-finite or ``> 1e300`` delta, a NaN weight or factor) takes
:meth:`GraphMixin._graph_exact`: the same steps in Python floats, which are
C doubles, inside one transaction.

Reads (``graph_expand``)
------------------------
* ``mode="bfs"`` -- ``PROPAGATE_BFS_LUA``. The Lua runs a FIFO queue with a
  visited map: an entry ``(pk, w, depth)`` expands, through the top
  ``max_edges`` neighbours of ``pk`` by ``(weight, dst)`` descending, unless
  an earlier entry for ``pk`` had a weight ``>= w``; a neighbour is reached
  with ``w * decay * min(edge, cap)`` and kept when that is ``>= threshold``;
  each result is the maximum weight it was reached with, seeds excluded.
  With ``threshold > 0`` every entry's weight is positive and the step is
  monotone in ``w``, so an entry the visited map skips is dominated by the
  earlier one (heavier, and no deeper, so with at least as many hops left):
  the result is the maximum over every walk of at most ``depth`` hops whose
  weights stay ``>= threshold``. Up to
  ``Defaults.PG_GRAPH_RECURSIVE_MAX_LAYERS`` (2) layers that is one
  ``WITH RECURSIVE`` statement, a layer per iteration, keeping each node's
  heaviest arrival per layer (:func:`bfs_sql`). The recursive statement sees
  only the previous layer, so it cannot apply the visited map and re-expands
  every reached node on every layer: past two layers its work grows with
  depth x fan-out where the Lua's stops (#781 review: 24.9 s against 0.10 s
  on a 400-node clique). Deeper calls run :meth:`GraphMixin._graph_bfs_layered`
  instead -- one statement per layer (:func:`bfs_layer_sql`) and the visited
  rule between layers, so they expand no more than the Lua does and stop
  after the last layer that improved a node, whatever ``depth`` is. A
  statement timeout is never what bounds a graph read. Outside that box
  (``threshold <= 0`` or below ``1e-290``, a negative or non-finite
  ``decay_per_hop``, a non-finite cap) the step is not monotone and the
  visited map's order matters, so :func:`simulate_bfs` replays the Lua's
  queue exactly in Python over the same top-``max_edges`` neighbour lists,
  fetched one layer per statement. Scores come back through ``%.14g``, as
  the Lua's ``tostring`` sends them.
* ``mode="linked"`` -- ``get_linked``: ``ZREVRANGEBYSCORE +inf min LIMIT 0 n``,
  one indexed read of ``src``'s rows (``graph_expand`` at depth 1 with the
  stored weights as scores, unclamped).
* ``mode="edges"`` -- ``export_state``'s ``ZRANGE 0 -1 WITHSCORES``.

Never imports ``redis``.
"""

from __future__ import annotations

import contextlib
import math
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence

from ...fields.constants import Defaults
from ..types import ModelSpec, RecordId, Scored, UnitOfWork
from .memory import _lit, lua_tostring, safe_mul
from .schema import TableSpec, _bounded, quote_ident

__all__ = [
    "EDGE_SUFFIX",
    "GRAPH_KIND",
    "GraphMixin",
    "bfs_layer_sql",
    "bfs_sql",
    "compile_graph",
    "edge_table",
    "graph_delete_lock_sql",
    "graph_delete_sql",
    "lua_integer_reply",
    "simulate_bfs",
]

GRAPH_KIND = "CoOccurrenceField"
EDGE_SUFFIX = "__edge"

#: ``WEAKEN_ALL_LUA``'s prune threshold (``CoOccurrenceField.weaken_all``).
WEAKEN_THRESHOLD = 0.001

#: Inputs at or under this magnitude cannot take a ``link``/``strengthen``
#: step outside the double range; anything larger takes the exact path.
_SAFE_MAGNITUDE = 1e300

#: The smallest threshold (and ``threshold * decay``) the ``WITH RECURSIVE``
#: path takes: below it a product could underflow to a value the threshold
#: still admits.
_MIN_SQL_THRESHOLD = 1e-290

_LLONG_MAX = 2**63 - 1
_LLONG_MIN = -(2**63)


def edge_table(ts: TableSpec, field: str) -> str:
    """The qualified edge table of ``field`` on ``ts``'s model."""
    name = _bounded(f"{ts.table}__{field}{EDGE_SUFFIX}")
    return f"{quote_ident(ts.schema)}.{quote_ident(name)}"


def graph_fields(spec: ModelSpec) -> list[str]:
    return sorted(n for n, fs in spec.fields.items() if fs.kind == GRAPH_KIND)


def compile_graph(
    spec: ModelSpec, schema: str, table: str
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The edge companion table of each ``CoOccurrenceField``: ``(name, DDL)``
    entries for :attr:`TableSpec.companions`. Pure."""
    out = []
    for name in graph_fields(spec):
        bare = _bounded(f"{table}__{name}{EDGE_SUFFIX}")
        qualified = f"{quote_ident(schema)}.{quote_ident(bare)}"
        out.append(
            (
                bare,
                (
                    f"CREATE TABLE IF NOT EXISTS {qualified} ("
                    '"src" text NOT NULL, "dst" text NOT NULL, '
                    '"weight" double precision NOT NULL, '
                    'PRIMARY KEY ("src", "dst"))',
                ),
            )
        )
    return tuple(out)


def graph_delete_sql(ts: TableSpec, spec: ModelSpec) -> tuple[str, int]:
    """``WITH`` CTEs that run ``CoOccurrenceField.on_delete`` inside a record
    ``DELETE``: each deleted key's own edges go, and for a symmetric field
    the reverse edge in every set it linked to (the Redis hook removes the
    key from exactly those sets). Returns the SQL prefix (empty when the model
    has no graph field) and how many times the key array parameter appears in
    it, in order."""
    ctes: list[str] = []
    uses = 0
    for i, name in enumerate(graph_fields(spec)):
        table = edge_table(ts, name)
        ctes.append(
            f'"_g{i}" AS (DELETE FROM {table} WHERE "src" = ANY(%s::text[]) '
            'RETURNING "src", "dst")'
        )
        uses += 1
        if spec.fields[name].options.get("symmetric", True):
            # A reverse edge whose own source is being deleted goes with
            # that source's rows above; it is excluded here so no row is
            # deleted by two CTEs of one statement.
            ctes.append(
                f'"_g{i}r" AS (DELETE FROM {table} AS e USING "_g{i}" AS g '
                'WHERE e."src" = g."dst" AND e."dst" = g."src" '
                'AND NOT (e."src" = ANY(%s::text[])))'
            )
            uses += 1
    if not ctes:
        return "", 0
    return "WITH " + ", ".join(ctes) + " ", uses


def graph_delete_lock_sql(
    ts: TableSpec, spec: ModelSpec, keys: Sequence[str]
) -> tuple[str, list[Any]]:
    """The record-key locks a record ``DELETE`` takes when the model has a
    symmetric ``CoOccurrenceField``: the deleted keys *and* every partner
    whose edge set the reverse-edge CTE of :func:`graph_delete_sql` writes,
    in one ``_pk``-ordered sequence -- the backend's one lock order, so the
    delete never row-locks a partner's edges without that partner's key
    lock. Empty when there is no symmetric graph field (the deleted keys'
    own locks then cover every row it writes).

    The statement runs twice. The first takes the locks in order, with the
    partners its snapshot saw; once it returns, the deleted keys are held,
    and no new partner can appear (a ``link`` to a deleted key takes that
    key's lock). The second sees, in a fresh snapshot, a partner linked
    between the first's snapshot and its last lock -- normally none, and
    re-taking a held advisory lock is a no-op -- so every edge set the
    ``DELETE`` that follows writes is locked. Only such a racing partner is
    locked out of order."""
    symmetric = [
        n for n in graph_fields(spec) if spec.fields[n].options.get("symmetric", True)
    ]
    if not symmetric:
        return "", []
    partners = " ".join(
        f'UNION SELECT e."dst" FROM {edge_table(ts, n)} AS e '
        'WHERE e."src" = ANY(%s::text[])'
        for n in symmetric
    )
    one = (
        "SELECT count(pg_advisory_xact_lock(hashtextextended(%s || u.k, 0))) "
        "FROM unnest(ARRAY(SELECT a.k FROM (SELECT unnest(%s::text[]) AS k "
        f'{partners}) AS a ORDER BY a.k COLLATE "C")) WITH ORDINALITY AS u(k, i); '
    )
    params: list[Any] = [f"popoto:rec:{ts.qualified}:", list(keys)]
    params += [list(keys)] * len(symmetric)
    return one + one, params + params


def lua_integer_reply(value: float) -> float:
    """``float()`` of the integer Redis replies with for a Lua number: the
    script's ``return weight`` is converted with a C ``(long long)`` cast,
    truncating toward zero. Out of range the cast is the platform's; on
    the arm64 and x86-64 servers measured, ``-inf`` and anything below
    ``-2**63`` reply ``-2**63``. Above ``2**63`` (a weight over the cap,
    reachable only with a cap past ``2**63``) arm64 saturates to
    ``2**63 - 1`` and x86-64 replies ``-2**63``; this returns the arm64
    value."""
    if math.isnan(value):
        return 0.0
    if value >= 2.0**63:
        return float(_LLONG_MAX)
    if value <= -(2.0**63):
        return float(_LLONG_MIN)
    return float(math.trunc(value))


def _lua_min(x: float, cap: float) -> float:
    """Lua 5.1 ``math.min(x, cap)``: the first argument unless the second is
    smaller -- so a NaN ``x`` stays NaN."""
    return cap if cap < x else x


def _sort_key(key: str) -> bytes:
    return key.encode("utf-8", "surrogateescape")


def _not_a_float() -> ValueError:
    # ZADD's refusal of a NaN score, which Redis raises as a ResponseError
    # out of the script; the Postgres backend never imports redis.
    return ValueError("value is not a valid float (a NaN edge weight)")


def bfs_sql(table: str, *, fanout: int) -> str:
    """The ``WITH RECURSIVE`` statement for ``PROPAGATE_BFS_LUA`` in its
    monotone domain (module docstring). Parameters, in order: the seeds
    (``text[]``), the decay per hop, the cap, the depth and the threshold.
    One iteration is one BFS layer: each
    frontier node's top ``fanout`` edges by ``(weight, dst)`` descending, the
    Lua's product ``(w * decay) * min(edge, cap)`` -- the second ``*``
    saturating where Postgres would raise -- kept at ``>= threshold``, and
    each reached node's heaviest arrival in the layer carried on."""
    product = safe_mul('s."x"', 's."eff"')
    return (
        "WITH RECURSIVE "
        '"_seed" AS (SELECT DISTINCT u AS "pk" FROM unnest(%s::text[]) AS u), '
        '"_walk" ("pk", "w", "d") AS ('
        'SELECT "pk", 1::float8, 0 FROM "_seed" '
        "UNION ALL "
        'SELECT r."pk", r."w", r."d" FROM ('
        'SELECT h."pk", h."w", h."d", row_number() OVER '
        '(PARTITION BY h."pk" ORDER BY h."w" DESC) AS "rk" FROM ('
        f'SELECT s."dst" AS "pk", {product} AS "w", s."d" FROM ('
        'SELECT n."dst", (k."w" * %s::float8) AS "x", '
        'least(n."weight", %s::float8) AS "eff", k."d" + 1 AS "d" '
        'FROM "_walk" AS k CROSS JOIN LATERAL ('
        f'SELECT e."dst", e."weight" FROM {table} AS e WHERE e."src" = k."pk" '
        'ORDER BY e."weight" DESC, e."dst" COLLATE "C" DESC '
        f"LIMIT {int(fanout)}) AS n "
        'WHERE k."d" < %s::float8) AS s) AS h '
        'WHERE h."w" >= %s::float8 AND h."w" <> \'NaN\'::float8) AS r '
        'WHERE r."rk" = 1) '
        'SELECT "pk", max("w") AS "w" FROM "_walk" '
        'WHERE "d" >= 1 AND "pk" NOT IN (SELECT "pk" FROM "_seed") '
        'GROUP BY "pk" ORDER BY max("w") DESC, "pk" COLLATE "C"'
    )


def bfs_layer_sql(table: str, *, fanout: int) -> str:
    """One BFS layer of :func:`bfs_sql`, from a frontier passed in: each
    frontier node's top ``fanout`` edges by ``(weight, dst)`` descending, the
    same product kept at ``>= threshold``, and each reached node's heaviest
    arrival. Parameters, in order: the decay per hop, the cap, the frontier's
    keys (``text[]``) and weights (``float8[]``), the threshold. Weights
    cross the wire as ``float8`` text, which round-trips a double exactly."""
    product = safe_mul('s."x"', 's."eff"')
    return (
        'SELECT h."pk", max(h."w") AS "w" FROM ('
        f'SELECT s."dst" AS "pk", {product} AS "w" FROM ('
        'SELECT n."dst", (f."w" * %s::float8) AS "x", '
        'least(n."weight", %s::float8) AS "eff" '
        'FROM unnest(%s::text[], %s::float8[]) AS f("pk", "w") CROSS JOIN LATERAL ('
        f'SELECT e."dst", e."weight" FROM {table} AS e WHERE e."src" = f."pk" '
        'ORDER BY e."weight" DESC, e."dst" COLLATE "C" DESC '
        f"LIMIT {int(fanout)}) AS n) AS s) AS h "
        'WHERE h."w" >= %s::float8 AND h."w" <> \'NaN\'::float8 '
        'GROUP BY h."pk"'
    )


def simulate_bfs(
    neighbours: Callable[[Sequence[str]], Mapping[str, list[tuple[str, float]]]],
    seeds: Sequence[str],
    *,
    depth: float,
    decay: float,
    threshold: float,
    cap: float,
) -> dict[str, float]:
    """``PROPAGATE_BFS_LUA``, statement for statement, in Python floats (C
    doubles, as Lua's are). ``neighbours(nodes)`` returns each node's
    ``ZREVRANGE 0 max_edges-1 WITHSCORES``; it is called once per layer, for
    every distinct node of the layer (a superset of the ones that expand).
    Returns the raw weights, before ``tostring``."""
    queue: list[tuple[str, float, float]] = [(s, 1.0, 0) for s in seeds]
    results: dict[str, float] = {}
    visited: dict[str, float] = {}
    head = 0
    fetched: dict[str, list[tuple[str, float]]] = {}
    while head < len(queue):
        pk, weight, d = queue[head]
        if pk not in fetched and d < depth:
            # Fetch this entry's whole layer at once: FIFO order means every
            # entry of depth d is dequeued before any of depth d + 1.
            layer = [
                q[0]
                for q in queue[head:]
                if q[2] == d and q[0] not in fetched and q[2] < depth
            ]
            got = neighbours(list(dict.fromkeys(layer)))
            for node in layer:
                fetched[node] = list(got.get(node, ()))
        head += 1
        seen = visited.get(pk)
        if seen is not None and seen >= weight:
            continue
        visited[pk] = weight
        if not d < depth:
            continue
        for neighbour, edge in fetched.get(pk, ()):
            effective = _lua_min(edge, cap)
            propagated = weight * decay * effective
            if propagated >= threshold:
                best = results.get(neighbour)
                if best is None or propagated > best:
                    results[neighbour] = propagated
                queue.append((neighbour, propagated, d + 1))
    for seed in seeds:
        results.pop(seed, None)
    return results


def _bfs_in_sql_domain(threshold: float, decay: float, cap: float) -> bool:
    """Whether the ``WITH RECURSIVE`` statement gives the Lua's answer
    (module docstring): a positive, not vanishing threshold, a finite
    non-negative decay whose product with it cannot underflow, a finite
    positive cap."""
    if not (math.isfinite(threshold) and threshold >= _MIN_SQL_THRESHOLD):
        return False
    if not (math.isfinite(decay) and decay >= 0):
        return False
    if decay > 0 and threshold * decay < _MIN_SQL_THRESHOLD:
        return False
    return math.isfinite(cap) and cap > 0


def _recursive_fits(depth: float) -> bool:
    """Whether :func:`bfs_sql` may answer: it expands ``ceil(depth)`` layers
    and cannot prune across them, so only up to
    ``Defaults.PG_GRAPH_RECURSIVE_MAX_LAYERS`` (read per call)."""
    if not math.isfinite(depth):
        return False
    return math.ceil(depth) <= int(Defaults.PG_GRAPH_RECURSIVE_MAX_LAYERS)


class GraphMixin:
    """``graph_update`` / ``graph_expand`` for
    :class:`~popoto.backends.postgres.PostgresBackend`."""

    # Provided by PostgresBackend.
    _table: Callable[..., TableSpec]
    _run: Callable[..., tuple[list[tuple[Any, ...]], int]]
    _record_locked: Callable[..., tuple[str, list[Any]]]
    _connection: Callable[..., Any]
    transaction: Callable[..., Any]

    # -- writes ---------------------------------------------------------------

    def graph_update(
        self,
        spec: ModelSpec,
        field: str,
        op: str,
        src: RecordId,
        dst: Optional[RecordId],
        amount: Optional[float],
        *,
        uow: Optional[UnitOfWork] = None,
        cap: Optional[float] = None,
        edges: Optional[Mapping[str, float]] = None,
    ) -> Any:
        """One ``CoOccurrenceField`` write (module docstring). Returns what
        the field method returns on Redis: ``link`` the integer reply as a
        float, ``strengthen`` the ``%.14g`` new weight, ``weaken`` the number
        of edges removed, ``unlink`` / ``replace`` ``None``."""
        fs = spec.fields[field]
        symmetric = bool(fs.options.get("symmetric", True))
        max_edges = int(fs.options.get("max_edges", 500))
        ts = self._table(spec, write=True)
        table = edge_table(ts, field)
        s = src.canonical
        t = dst.canonical if dst is not None else None
        if op == "link":
            assert t is not None and amount is not None
            return self._graph_link(
                ts, table, s, t, float(amount), max_edges, symmetric, uow
            )
        if op == "strengthen":
            assert t is not None and amount is not None and cap is not None
            return self._graph_strengthen(
                ts, table, s, t, float(amount), float(cap), symmetric, uow
            )
        if op == "unlink":
            assert t is not None
            pairs = [(s, t)] + ([(t, s)] if symmetric else [])
            sql, params = self._record_locked(
                ts,
                [p[0] for p in pairs],
                f"DELETE FROM {table} AS e USING unnest(%s::text[], %s::text[]) "
                'AS p(s, d) WHERE e."src" = p.s AND e."dst" = p.d',
                [[p[0] for p in pairs], [p[1] for p in pairs]],
            )
            self._run(sql, params, uow=uow, write=True)
            return None
        if op == "weaken":
            assert amount is not None
            return self._graph_weaken(ts, table, s, float(amount), uow)
        if op == "replace":
            return self._graph_replace(ts, table, s, edges or {}, uow)
        raise ValueError(f"unknown graph_update op {op!r}")

    def _graph_link(
        self,
        ts: TableSpec,
        table: str,
        s: str,
        t: str,
        weight: float,
        max_edges: int,
        symmetric: bool,
        uow: Optional[UnitOfWork],
    ) -> float:
        if math.isnan(weight):
            return self._graph_exact(
                ts, table, "link", s, t, weight, None, max_edges, symmetric, uow
            )
        # A sorted set replies 0 for a score of -0 (and Lua then sees 0):
        # store the zero Redis gives back.
        weight += 0.0
        directions = [(s, t)] + ([(t, s)] if symmetric else [])
        ctes: list[str] = []
        params: list[Any] = []
        for i, (a, b) in enumerate(directions):
            # LINK_WITH_PRUNE_LUA for the set of `a`: the prune runs over
            # the set as it would be after the ZADD, so a new edge that
            # ranks among the lowest is simply never inserted.
            ctes += [
                f'"c{i}" AS (SELECT "dst", "weight" FROM {table} WHERE "src" = %s)',
                f'"h{i}" AS (SELECT "weight" FROM "c{i}" WHERE "dst" = %s)',
                f'"v{i}" AS (SELECT "dst", "weight" FROM "c{i}" UNION ALL '
                f'SELECT %s::text, %s::float8 WHERE NOT EXISTS (SELECT 1 FROM "h{i}"))',
                f'"r{i}" AS (SELECT "dst", row_number() OVER (ORDER BY "weight", '
                f'"dst" COLLATE "C") AS "rn", count(*) OVER () AS "n" FROM "v{i}")',
                f'"x{i}" AS (SELECT "dst" FROM "r{i}" WHERE "rn" <= "n" - {int(max_edges)} '
                f'AND NOT EXISTS (SELECT 1 FROM "h{i}"))',
                f'"i{i}" AS (INSERT INTO {table} ("src", "dst", "weight") '
                f'SELECT %s, %s, %s::float8 WHERE NOT EXISTS (SELECT 1 FROM "h{i}") '
                f'AND NOT EXISTS (SELECT 1 FROM "x{i}" WHERE "dst" = %s))',
                f'"p{i}" AS (DELETE FROM {table} WHERE "src" = %s '
                f'AND "dst" IN (SELECT "dst" FROM "x{i}"))',
            ]
            params += [a, b, b, weight, a, b, weight, b, a]
        sql = "WITH " + ", ".join(ctes) + ' SELECT (SELECT "weight" FROM "h0")'
        sql, params = self._record_locked(ts, [d[0] for d in directions], sql, params)
        rows, _ = self._run(sql, params, uow=uow, write=True)
        existing = rows[0][0] if rows else None
        return lua_integer_reply(weight if existing is None else float(existing))

    def _graph_strengthen(
        self,
        ts: TableSpec,
        table: str,
        s: str,
        t: str,
        delta: float,
        cap: float,
        symmetric: bool,
        uow: Optional[UnitOfWork],
    ) -> float:
        if not (
            math.isfinite(delta)
            and abs(delta) <= _SAFE_MAGNITUDE
            and math.isfinite(cap)
            and abs(cap) <= _SAFE_MAGNITUDE
        ):
            return self._graph_exact(
                ts, table, "strengthen", s, t, delta, cap, 0, symmetric, uow
            )
        directions = [(s, t)] + ([(t, s)] if symmetric else [])
        fresh = _lua_min(0.0 + delta, cap)
        c, dl = _lit(cap), _lit(delta)
        # min(old + delta, cap) as Lua's math.min orders it; an old weight
        # already over the cap (a raw write) clamps without the addition,
        # which could otherwise overflow.
        update = (
            f'CASE WHEN e."weight" > {c} THEN {c} '
            f'WHEN {c} < e."weight" + {dl} THEN {c} ELSE e."weight" + {dl} END'
        )
        ctes = []
        params: list[Any] = []
        for i, (a, b) in enumerate(directions):
            ctes.append(
                f'"u{i}" AS (INSERT INTO {table} AS e ("src", "dst", "weight") '
                f'VALUES (%s, %s, {_lit(fresh)}) ON CONFLICT ("src", "dst") '
                f'DO UPDATE SET "weight" = {update} RETURNING e."weight")'
            )
            params += [a, b]
        sql = "WITH " + ", ".join(ctes) + ' SELECT "weight" FROM "u0"'
        sql, params = self._record_locked(ts, [d[0] for d in directions], sql, params)
        rows, _ = self._run(sql, params, uow=uow, write=True)
        return lua_tostring(float(rows[0][0]))

    def _graph_weaken(
        self,
        ts: TableSpec,
        table: str,
        s: str,
        factor: float,
        uow: Optional[UnitOfWork],
    ) -> int:
        if factor == 0:
            # weaken_all(factor=0): ZCARD + DEL.
            sql, params = self._record_locked(
                ts, [s], f'DELETE FROM {table} WHERE "src" = %s', [s]
            )
            _, count = self._run(sql, params, uow=uow, write=True)
            return int(count or 0)
        if math.isnan(factor):
            return int(
                self._graph_exact(
                    ts, table, "weaken", s, None, factor, None, 0, False, uow
                )
            )
        f = _lit(factor)
        # weight * factor, except where Postgres would raise on an underflow
        # to 0: a product under e^-700 (~1e-304) is pruned either way.
        nw = (
            f'CASE WHEN e."weight" = 0 OR abs(e."weight") = \'Infinity\'::float8 '
            f'THEN e."weight" * {f} '
            f'WHEN ln(abs(e."weight")) + ln({f}) < -700 THEN 0::float8 '
            f'ELSE e."weight" * {f} END'
        )
        thr = _lit(WEAKEN_THRESHOLD)
        sql = (
            f'WITH "c" AS (SELECT e."dst", {nw} AS "nw" FROM {table} AS e '
            f'WHERE e."src" = %s), '
            f'"d" AS (DELETE FROM {table} AS e USING "c" WHERE e."src" = %s '
            f'AND e."dst" = "c"."dst" AND "c"."nw" < {thr} RETURNING 1), '
            f'"u" AS (UPDATE {table} AS e SET "weight" = "c"."nw" FROM "c" '
            f'WHERE e."src" = %s AND e."dst" = "c"."dst" AND NOT ("c"."nw" < {thr})) '
            'SELECT count(*) FROM "d"'
        )
        sql, params = self._record_locked(ts, [s], sql, [s, s, s])
        rows, _ = self._run(sql, params, uow=uow, write=True)
        return int(rows[0][0])

    def _graph_replace(
        self,
        ts: TableSpec,
        table: str,
        s: str,
        edges: Mapping[str, float],
        uow: Optional[UnitOfWork],
    ) -> None:
        dsts = list(edges)
        weights = [float(edges[d]) for d in dsts]
        if any(math.isnan(w) for w in weights):
            # import_state's ZADD refuses a NaN score -- on Redis after its
            # DELETE has already emptied the set. Refused here before any
            # write, so the set is left as it was and no NaN edge is stored
            # (Postgres would order it above every weight, and least(NaN,
            # cap) is the cap: the heaviest edge of the graph).
            raise _not_a_float()
        sql, params = self._record_locked(
            ts,
            [s],
            # Two statements in one message: an INSERT beside a DELETE CTE
            # would not see the deleted rows and conflict with them.
            f'DELETE FROM {table} WHERE "src" = %s; '
            f'INSERT INTO {table} ("src", "dst", "weight") '
            "SELECT %s, u.d, u.w FROM unnest(%s::text[], %s::float8[]) AS u(d, w)",
            # `+ 0.0`: a sorted set replies 0 for a -0 score.
            [s, s, dsts, [w + 0.0 for w in weights]],
        )
        self._run(sql, params, uow=uow, write=True)

    def _graph_exact(
        self,
        ts: TableSpec,
        table: str,
        op: str,
        s: str,
        t: Optional[str],
        amount: float,
        cap: Optional[float],
        max_edges: int,
        symmetric: bool,
        uow: Optional[UnitOfWork],
    ) -> Any:
        """The Lua scripts' steps in Python floats, one direction (one
        script) at a time, inside one transaction: for the inputs whose SQL
        arithmetic would raise or differ (NaN, non-finite or huge values).
        A step whose result is NaN raises ``ValueError`` where ``ZADD``
        refuses NaN, and the transaction rolls back: on Redis a symmetric
        write whose *second* script fails keeps the first script's write (a
        documented divergence, reachable only through a NaN result)."""
        directions = [(s, t)] + ([(t, s)] if symmetric and t is not None else [])

        def work(tx: UnitOfWork) -> Any:
            keys = [d[0] for d in directions]
            lock_sql, lock_params = self._record_locked(ts, keys, "SELECT 1", [])
            self._run(lock_sql, lock_params, uow=tx, write=True)
            reply: Any = None
            for i, (a, b) in enumerate(directions):
                rows, _ = self._run(
                    f'SELECT "dst", "weight" FROM {table} WHERE "src" = %s FOR UPDATE',
                    [a],
                    uow=tx,
                )
                current = {d: float(w) for d, w in rows}
                if op == "link":
                    assert b is not None
                    if b in current:
                        value: Any = lua_integer_reply(current[b])
                    else:
                        if math.isnan(amount):
                            raise _not_a_float()
                        current[b] = amount + 0.0
                        doomed = _prune(current, max_edges)
                        self._apply(table, a, current, doomed, tx, insert=b)
                        value = lua_integer_reply(amount)
                elif op == "strengthen":
                    assert b is not None and cap is not None
                    new = _lua_min(current.get(b, 0.0) + amount, cap) + 0.0
                    if math.isnan(new):
                        raise _not_a_float()
                    self._run(
                        f'INSERT INTO {table} ("src", "dst", "weight") '
                        'VALUES (%s, %s, %s) ON CONFLICT ("src", "dst") '
                        'DO UPDATE SET "weight" = EXCLUDED."weight"',
                        [a, b, new],
                        uow=tx,
                        write=True,
                    )
                    value = lua_tostring(new)
                else:  # weaken
                    removed = 0
                    for d in sorted(current, key=lambda k: (current[k], _sort_key(k))):
                        new = current[d] * amount
                        if new < WEAKEN_THRESHOLD:
                            self._run(
                                f'DELETE FROM {table} WHERE "src" = %s AND "dst" = %s',
                                [a, d],
                                uow=tx,
                                write=True,
                            )
                            removed += 1
                        elif math.isnan(new):
                            raise _not_a_float()
                        else:
                            self._run(
                                f'UPDATE {table} SET "weight" = %s '
                                'WHERE "src" = %s AND "dst" = %s',
                                [new, a, d],
                                uow=tx,
                                write=True,
                            )
                    value = removed
                if i == 0:
                    reply = value
            return reply

        from . import _pg_uow

        if _pg_uow(uow) is not None:
            return work(uow)  # type: ignore[arg-type]
        with self.transaction() as tx:
            return work(tx)

    def _apply(
        self,
        table: str,
        src: str,
        current: Mapping[str, float],
        doomed: Iterable[str],
        tx: UnitOfWork,
        *,
        insert: str,
    ) -> None:
        doomed = list(doomed)
        if insert not in doomed:
            self._run(
                f'INSERT INTO {table} ("src", "dst", "weight") VALUES (%s, %s, %s)',
                [src, insert, current[insert]],
                uow=tx,
                write=True,
            )
        rest = [d for d in doomed if d != insert]
        if rest:
            self._run(
                f'DELETE FROM {table} WHERE "src" = %s AND "dst" = ANY(%s::text[])',
                [src, rest],
                uow=tx,
                write=True,
            )

    # -- reads ----------------------------------------------------------------

    def graph_expand(
        self,
        spec: ModelSpec,
        field: str,
        seeds: Sequence[RecordId],
        *,
        depth: float,
        decay_per_hop: float,
        threshold: Any,
        fanout: Optional[int],
        cap: Optional[float] = None,
        mode: str = "bfs",
    ) -> Scored:
        """``mode="bfs"``: ``propagate`` (scores through ``%.14g``, seeds
        excluded); ``"linked"``: ``get_linked`` for the one seed, ``threshold``
        being its ``min_weight`` and ``fanout`` its ``limit``; ``"edges"``:
        every edge of the one seed, ascending (``export_state``). Module
        docstring for the semantics."""
        ts = self._table(spec)
        table = edge_table(ts, field)
        if mode == "linked":
            return self._graph_linked(
                spec, table, seeds[0].canonical, threshold, fanout
            )
        if mode == "edges":
            rows, _ = self._run(
                f'SELECT "dst", "weight" FROM {table} WHERE "src" = %s '
                'ORDER BY "weight", "dst" COLLATE "C"',
                [seeds[0].canonical],
            )
            return [(RecordId(spec.name, (), d, native=d), float(w)) for d, w in rows]
        if mode != "bfs":
            raise ValueError(f"unknown graph_expand mode {mode!r}")
        assert cap is not None
        names = [rid.canonical for rid in seeds]
        if not names or not depth > 0:
            return []
        if fanout is None:
            fanout = int(spec.fields[field].options.get("max_edges", 500))
        decay = float(decay_per_hop)
        thr = float(threshold)
        if _bfs_in_sql_domain(thr, decay, float(cap)):
            if _recursive_fits(float(depth)):
                rows, _ = self._run(
                    bfs_sql(table, fanout=int(fanout)),
                    [names, decay, float(cap), float(depth), thr],
                )
                return [
                    (RecordId(spec.name, (), pk, native=pk), lua_tostring(float(w)))
                    for pk, w in rows
                ]
            raw = self._graph_bfs_layered(
                table,
                names,
                depth=float(depth),
                decay=decay,
                threshold=thr,
                cap=float(cap),
                fanout=int(fanout),
            )
        else:
            raw = simulate_bfs(
                lambda nodes: self._graph_neighbours(table, nodes, int(fanout)),
                names,
                depth=float(depth),
                decay=decay,
                threshold=thr,
                cap=float(cap),
            )
        ordered = sorted(raw.items(), key=lambda kv: (-kv[1], _sort_key(kv[0])))
        return [
            (RecordId(spec.name, (), pk, native=pk), lua_tostring(w))
            for pk, w in ordered
        ]

    def _graph_bfs_layered(
        self,
        table: str,
        seeds: Sequence[str],
        *,
        depth: float,
        decay: float,
        threshold: float,
        cap: float,
        fanout: int,
    ) -> dict[str, float]:
        """``PROPAGATE_BFS_LUA`` in its monotone domain, one statement per
        layer (:func:`bfs_layer_sql`) with the Lua's visited rule between
        layers: a node goes on to the next layer only when it arrived
        strictly heavier than at any earlier layer. A pruned arrival is
        dominated by the earlier, no-deeper one (module docstring), so the
        answer is the recursive statement's; the work is not, because every
        expansion here is one the Lua's visited map also lets through.

        Terminates without the depth bound: the contraction guard keeps
        ``decay * cap < 1``, so a walk that repeats a node arrives lighter
        than its cycle-free part did, and every improving arrival is a simple
        path -- at most one layer per node reached. Returns the raw weights,
        before ``tostring``."""
        sql = bfs_layer_sql(table, fanout=fanout)
        best: dict[str, float] = {s: 1.0 for s in seeds}
        frontier = list(best.items())
        layer = 0
        with self._graph_read_session() as session:
            while frontier and layer < depth:
                rows, _ = self._run(
                    sql,
                    [decay, cap, [pk for pk, _ in frontier]]
                    + [[w for _, w in frontier], threshold],
                    uow=session,
                )
                frontier = []
                for pk, w in rows:
                    w = float(w)
                    seen = best.get(pk)
                    if seen is None or w > seen:
                        best[pk] = w
                        frontier.append((pk, w))
                layer += 1
        return {pk: w for pk, w in best.items() if pk not in seeds}

    @contextlib.contextmanager
    def _graph_read_session(self) -> Iterator[UnitOfWork]:
        """One pooled connection, in one read transaction, for the layer
        statements of a deep ``propagate``: each layer then costs one round
        trip rather than a pool checkout plus a ``SET LOCAL`` message. The
        statement timeout still applies to each statement, and a failure is
        classified as a read (never a dropped write)."""
        from . import PostgresUnitOfWork

        with self._connection(write=False) as conn:
            with conn.transaction():
                ms = int(Defaults.PG_STATEMENT_TIMEOUT_MS)
                if ms > 0:
                    conn.execute(f"SET LOCAL statement_timeout = {ms}")
                yield PostgresUnitOfWork(conn)

    def _graph_neighbours(
        self, table: str, nodes: Sequence[str], fanout: int
    ) -> dict[str, list[tuple[str, float]]]:
        """Each node's ``ZREVRANGE 0 fanout-1 WITHSCORES``, in one statement."""
        if not nodes or fanout <= 0:
            return {}
        rows, _ = self._run(
            'SELECT u.pk, n."dst", n."weight" FROM unnest(%s::text[]) AS u(pk) '
            "CROSS JOIN LATERAL ("
            f'SELECT e."dst", e."weight" FROM {table} AS e WHERE e."src" = u.pk '
            'ORDER BY e."weight" DESC, e."dst" COLLATE "C" DESC '
            f"LIMIT {int(fanout)}) AS n "
            'ORDER BY u.pk, n."weight" DESC, n."dst" COLLATE "C" DESC',
            [list(nodes)],
        )
        out: dict[str, list[tuple[str, float]]] = {}
        for pk, dst, weight in rows:
            out.setdefault(pk, []).append((dst, float(weight)))
        return out

    def _graph_linked(
        self,
        spec: ModelSpec,
        table: str,
        src: str,
        min_weight: Any,
        limit: Optional[int],
    ) -> Scored:
        """``ZREVRANGEBYSCORE key +inf <min> LIMIT 0 <limit> WITHSCORES``.

        The two refusals keep Redis's text and order -- redis-py's own
        ``DataError`` for ``limit=None`` is raised client-side before the
        server's ``ResponseError`` for a NaN bound -- as ``ValueError`` (the
        backend never imports redis; a documented divergence)."""
        if limit is None:
            raise ValueError("``start`` and ``num`` must both be specified")
        text = str(min_weight)
        exclusive = text.startswith("(")
        bound = float(text[1:] if exclusive else text)
        if math.isnan(bound):
            raise ValueError("min or max is not a float")
        n = int(limit)
        if n == 0:
            return []
        sql = (
            f'SELECT "dst", "weight" FROM {table} WHERE "src" = %s '
            f'AND "weight" {">" if exclusive else ">="} {_lit(bound)} '
            'ORDER BY "weight" DESC, "dst" COLLATE "C" DESC'
        )
        if n > 0:
            sql += f" LIMIT {n}"
        rows, _ = self._run(sql, [src])
        return [(RecordId(spec.name, (), d, native=d), float(w)) for d, w in rows]


def _prune(current: Mapping[str, float], max_edges: int) -> list[str]:
    """The members ``ZREMRANGEBYRANK 0 count-max_edges-1`` removes: the
    lowest by ``(score, member bytes)``."""
    count = len(current)
    if count <= max_edges:
        return []
    ranked = sorted(current, key=lambda k: (current[k], _sort_key(k)))
    return ranked[: count - max_edges]
