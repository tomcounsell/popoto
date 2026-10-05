#!/usr/bin/env python
"""Seeded two-leg probe: Redis (the Lua oracle) vs Postgres, the
co-occurrence graph (#759 M4).

Each *shape* picks a model (symmetric or not, ``max_edges`` from 1 to 500),
a pool of keys (plain strings, record keys, non-ASCII, prefixes of each
other) and replays one random sequence of ``CoOccurrenceField`` writes on
both legs -- ``link`` with weights from typical to ``-inf``, ties and the
cap, ``strengthen`` with deltas from ``1e-12`` to past the cap, ``unlink``,
``weaken_all`` with factors ``0``, tiny, typical and ``1``, record deletes
and ``import_state`` -- comparing every return value (or that both raise).
Then it asks both legs the same questions:

=====================  =======================================================
class                  what is compared
=====================  =======================================================
``link``               every ``link`` return (the Lua integer reply)
``strengthen``         every ``strengthen`` return (``%.14g``)
``weaken``             every ``weaken_all`` return (edges removed)
``write_error``        that a write raises on both legs or on neither
``edges``              each key's full edge set and weights, exact
``get_linked``         random ``min_weight`` (incl. exclusive) and ``limit``
``propagate``          random depth, decay, threshold: keys and ``%.14g``
                       weights, exact
``propagate_exact``    the same with the Postgres SQL path forced off (the
                       Python replay of the Lua queue)
``propagate_layered``  the same with every SQL-domain call forced onto the
                       visited-pruned statement per layer
``propagate_recursive``  the same forced onto the one ``WITH RECURSIVE``
                       statement (depth <= 8 only: it cannot prune)
``import_nan_state``   after an ``import_state`` both legs refuse for a NaN
                       weight: Redis's set is empty (its ``DELETE`` ran),
                       Postgres's is the set before the import
``export``             ``export_state`` for every key
``composite``          ``composite_score`` with ``co_occurrence_boost`` from
                       ``propagate``: order, exact
``traverse``           ``graph_traversal.traverse``: the same (key, weight)
                       multiset
=====================  =======================================================

Documented classes, counted but not failures (docs/features/postgres-backend.md):

* ``nan_error_type`` -- a NaN weight or result (``link``, ``strengthen``,
  ``weaken_all``, ``import_state``), or a NaN ``get_linked`` bound, raises
  ``ResponseError`` on Redis and ``ValueError`` on Postgres, same text.
* ``limit_none_error_type`` -- ``get_linked(limit=None)`` raises redis-py's
  ``DataError`` on Redis and ``ValueError`` on Postgres, same text.
* ``nan_import_keeps_set`` -- a refused NaN ``import_state`` has emptied the
  set on Redis and left it unchanged on Postgres (checked exactly as
  ``import_nan_state``); the probe then empties it on Postgres too, so the
  legs carry on from the same state.
* ``traverse_tie_order`` -- ``traverse()`` lists equal weights in the order
  ``propagate``'s dict holds them: Lua table order on Redis, score then key
  on Postgres.
* ``composite_orphan_slot`` -- a ``co_occurrence_boost`` key with no record
  takes a top-K slot on Redis (and is dropped at hydration); on Postgres only
  records rank. The probe also compares the boost restricted to record keys,
  exactly.

A mismatch is printed with its seed, shape and inputs; the run exits non-zero
if there is any undocumented one.

Safety: Redis is bound from ``REDIS_URL`` *before* importing popoto and
database 0 is refused (CLAUDE.md, #577); on Postgres the script creates its
own ``popoto_test_<uuid4().hex>`` schema and drops only that.

Usage::

    REDIS_URL=redis://localhost:6379/13 \\
    POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \\
        python scripts/probe_graph_parity.py --seeds 1 2 3 --shapes 200
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
import uuid
from collections import Counter
from typing import Any, Callable, Optional
from unittest import mock

if __name__ == "__main__":
    _url = os.environ.get("REDIS_URL", "")
    if not _url or _url.rstrip("/").endswith("/0") or _url.count("/") < 3:
        sys.exit("set REDIS_URL=redis://localhost:6379/<n> (n != 0) before running")
    if not os.environ.get("POPOTO_POSTGRES_URL"):
        sys.exit("set POPOTO_POSTGRES_URL=postgresql://host:port/db")

import popoto  # noqa: E402
from popoto import ConfidenceField  # noqa: E402
from popoto.backends import set_backend  # noqa: E402
from popoto.backends.postgres import graph as pg_graph  # noqa: E402
from popoto.fields.co_occurrence_field import CoOccurrenceField  # noqa: E402
from popoto.fields.constants import Defaults  # noqa: E402
from popoto.recipes import graph_traversal  # noqa: E402


class ProbeGraphSym(popoto.Model):
    name = popoto.KeyField()
    certainty = ConfidenceField()
    edges = CoOccurrenceField(symmetric=True, max_edges=4)


class ProbeGraphAsym(popoto.Model):
    name = popoto.KeyField()
    certainty = ConfidenceField(initial_confidence=0.3)
    edges = CoOccurrenceField(symmetric=False, max_edges=2)


class ProbeGraphOne(popoto.Model):
    name = popoto.KeyField()
    certainty = ConfidenceField()
    edges = CoOccurrenceField(symmetric=True, max_edges=1)


class ProbeGraphWide(popoto.Model):
    name = popoto.KeyField()
    certainty = ConfidenceField(initial_confidence=0.9)
    edges = CoOccurrenceField(symmetric=True)


MODELS = (ProbeGraphSym, ProbeGraphAsym, ProbeGraphOne, ProbeGraphWide)
NAME_CHARS = "aBz0_:-~é"


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return a == b and math.copysign(1, a) == math.copysign(1, b)
    return bool(a == b)


class Probe:
    """Runs shapes against one Redis and one Postgres backend."""

    def __init__(self, pg: Any, *, verbose: bool = False) -> None:
        self.pg = pg
        self.verbose = verbose
        self.checks: Counter = Counter()
        self.mismatches: Counter = Counter()
        self.documented: Counter = Counter()
        self.examples: dict[str, list[str]] = {}
        self.shapes = 0
        self.sql_paths = 0
        self.exact_paths = 0

    # -- plumbing -------------------------------------------------------------

    def leg(self, name: str) -> None:
        set_backend("redis" if name == "redis" else self.pg)

    def wipe(self) -> None:
        self.leg("redis")
        client = popoto.get_redis()
        for model in MODELS:
            for key in client.scan_iter(match=f"*{model.__name__}*", count=1000):
                client.delete(key)
        self.leg("postgres")
        for model in MODELS:
            ts = self.pg._table(model._meta.spec)
            self.pg._run(f"TRUNCATE {ts.qualified}, {pg_graph.edge_table(ts, 'edges')}")

    def miss(self, cls: str, detail: str) -> None:
        self.mismatches[cls] += 1
        bucket = self.examples.setdefault(cls, [])
        if len(bucket) < 5:
            bucket.append(detail)
        if self.verbose:
            print(f"MISMATCH {cls}: {detail}", file=sys.stderr)

    def check(self, cls: str, ok: bool, detail: Callable[[], str]) -> None:
        self.checks[cls] += 1
        if not ok:
            self.miss(cls, detail())

    def both(self, fn: Callable[[str], Any]) -> dict[str, tuple[str, Any]]:
        out = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            try:
                out[leg] = ("ok", fn(leg))
            except Exception as exc:  # compared, never raised
                out[leg] = ("err", exc)
        return out

    def compare_write(self, cls: str, got: dict, tag: str, what: str) -> None:
        r, p = got["redis"], got["postgres"]
        if r[0] == "err" or p[0] == "err":
            same = r[0] == p[0]
            self.check("write_error", same, lambda: f"{tag} {what}: {r!r} vs {p!r}")
            if same and type(r[1]).__name__ != type(p[1]).__name__:
                self.error_type(r[1], p[1], f"{tag} {what}")
            return
        self.check(
            cls, _same(r[1], p[1]), lambda: f"{tag} {what}: {r[1]!r} vs {p[1]!r}"
        )

    def error_type(self, r: BaseException, p: BaseException, what: str) -> None:
        """Both legs raised, with different classes: only the documented
        pairs, with the same text, are not a mismatch."""
        if "must both be specified" in str(r) and str(r) == str(p):
            self.documented["limit_none_error_type"] += 1
        elif ("valid float" in str(r) and "NaN" in str(p)) or (
            "not a float" in str(r) and str(r) == str(p)
        ):
            self.documented["nan_error_type"] += 1
        else:
            self.miss("write_error", f"{what}: {type(r)} vs {type(p)}")

    # -- inputs -----------------------------------------------------------------

    @staticmethod
    def weight(rng: random.Random) -> float:
        roll = rng.random()
        if roll < 0.55:
            return round(rng.uniform(0.0, 1.0), rng.randint(1, 8))
        if roll < 0.65:
            return rng.choice([0.1, 0.5, 1.0, 0.25])  # ties
        if roll < 0.75:
            return -round(rng.uniform(0, 3), 3)
        return rng.choice(
            [
                0.0,
                -0.0,
                1e-320,
                5e-324,
                1e-12,
                0.001,
                0.00099,
                -1e300,
                -math.inf,
                1.0,
                0.9999999999999999,
                float("nan"),
            ]
        )

    @staticmethod
    def delta(rng: random.Random) -> float:
        roll = rng.random()
        if roll < 0.7:
            return round(rng.uniform(0.001, 0.4), rng.randint(1, 9))
        return rng.choice(
            [1e-12, 5e-324, 0.05, 2.0, 1e305, math.inf, 1.0, 0.1234567890123456]
        )

    @staticmethod
    def factor(rng: random.Random) -> float:
        roll = rng.random()
        if roll < 0.6:
            return round(rng.uniform(0.05, 0.99), 4)
        return rng.choice([0, 1, 1e-300, 0.5, 0.001, 0.95])

    def keys(self, rng: random.Random, model: Any) -> tuple[dict[str, str], list[str]]:
        n = rng.randint(2, 12)
        names: list[str] = []
        while len(names) < n:
            name = "".join(rng.choice(NAME_CHARS) for _ in range(rng.randint(1, 3)))
            if name not in names:
                names.append(name)
        records = {model(name=nm).db_key.redis_key: nm for nm in names[: n // 2]}
        plain = names[n // 2 :]
        if rng.random() < 0.3 and plain:
            plain.append(plain[0] + "x")  # a prefix of another key
        return records, plain

    # -- one shape ----------------------------------------------------------------

    def shape(self, rng: random.Random, seed: int, index: int) -> None:
        self.shapes += 1
        tag = f"seed={seed} shape={index}"
        model = rng.choice(MODELS)
        field = model._meta.fields["edges"]
        self.wipe()
        names, plain = self.keys(rng, model)
        record_keys = list(names)
        pool = record_keys + plain
        for leg in ("redis", "postgres"):
            self.leg(leg)
            for key in record_keys:
                model.create(name=names[key])
        for step in range(rng.randint(5, 50)):
            self.write(rng, model, field, pool, record_keys, names, f"{tag} op={step}")
        self.state(model, field, pool, tag)
        for _ in range(rng.randint(1, 4)):
            self.linked(rng, model, field, pool, tag)
        for _ in range(rng.randint(2, 6)):
            self.propagate(rng, model, field, pool, tag)
        self.composite(rng, model, field, pool, record_keys, tag)

    def write(self, rng, model, field, pool, record_keys, names, tag) -> None:
        roll = rng.random()
        a, b = rng.sample(pool, 2) if len(pool) > 1 else (pool[0], pool[0] + "y")
        if roll < 0.45:
            w = self.weight(rng)
            got = self.both(lambda leg: field.link(model, a, b, initial_weight=w))
            self.compare_write("link", got, tag, f"link({a!r}, {b!r}, {w!r})")
        elif roll < 0.7:
            d = self.delta(rng)
            got = self.both(lambda leg: field.strengthen(model, a, b, delta=d))
            self.compare_write(
                "strengthen", got, tag, f"strengthen({a!r}, {b!r}, {d!r})"
            )
        elif roll < 0.8:
            got = self.both(lambda leg: field.unlink(model, a, b))
            self.compare_write("unlink", got, tag, f"unlink({a!r}, {b!r})")
        elif roll < 0.92:
            f = self.factor(rng)
            got = self.both(lambda leg: field.weaken_all(model, a, factor=f))
            self.compare_write("weaken", got, tag, f"weaken_all({a!r}, {f!r})")
        elif roll < 0.97 and record_keys:
            key = rng.choice(record_keys)

            def delete(leg: str) -> Any:
                inst = model.query.get(redis_key=key)
                return None if inst is None else inst.delete()

            got = self.both(delete)
            self.compare_write("delete", got, tag, f"delete({key!r})")
            if rng.random() < 0.5:
                for leg in ("redis", "postgres"):
                    self.leg(leg)
                    if model.query.get(redis_key=key) is None:
                        model.create(name=names[key])
        else:
            src = rng.choice(pool)
            edges = {
                rng.choice(pool): self.weight(rng) for _ in range(rng.randint(1, 6))
            }
            inst = model(name="probe-import")
            self.leg("postgres")
            before = self.edges_of(model, field, src)

            def imp(leg: str) -> Any:
                with mock.patch.object(type(inst), "db_key", mock.PropertyMock()) as dk:
                    dk.return_value.redis_key = src
                    return CoOccurrenceField.import_state(
                        inst, "edges", {"edges": edges, "max_edges": 9}
                    )

            got = self.both(imp)
            self.compare_write("import", got, tag, f"import({src!r}, {edges!r})")
            if got["redis"][0] == got["postgres"][0] == "err" and any(
                math.isnan(v) for v in edges.values()
            ):
                self.leg("redis")
                after_r = self.edges_of(model, field, src)
                self.leg("postgres")
                after_p = self.edges_of(model, field, src)
                self.check(
                    "import_nan_state",
                    after_r == [] and after_p == before,
                    lambda: f"{tag} import({src!r}, {edges!r}): {after_r} / "
                    f"{after_p} (before {before})",
                )
                self.documented["nan_import_keeps_set"] += 1
                field.weaken_all(model, src, factor=0)  # resync (Postgres leg)

    def edges_of(self, model, field, key) -> list:
        return field.get_linked(model, key, min_weight="-inf", limit=-1)

    def state(self, model, field, pool, tag) -> None:
        for key in pool:
            got = self.both(lambda leg: self.edges_of(model, field, key))
            self.check(
                "edges",
                got["redis"] == got["postgres"]
                and all(
                    _same(x[1], y[1])
                    for x, y in zip(got["redis"][1], got["postgres"][1])
                ),
                lambda: f"{tag} {key!r}: {got}",
            )

            def export(leg: str) -> Any:
                inst = model(name="probe-export")
                with mock.patch.object(type(inst), "db_key", mock.PropertyMock()) as dk:
                    dk.return_value.redis_key = key
                    return CoOccurrenceField.export_state(inst, "edges")

            got = self.both(export)
            self.check(
                "export", got["redis"] == got["postgres"], lambda: f"{tag} {got}"
            )

    def linked(self, rng, model, field, pool, tag) -> None:
        key = rng.choice(pool)
        roll = rng.random()
        if roll < 0.5:
            min_weight: Any = round(rng.uniform(-0.5, 1.0), 2)
        elif roll < 0.7:
            min_weight = f"({rng.choice([0.1, 0.5, 0.25, 0])}"
        else:
            min_weight = rng.choice(["-inf", 0, 0.01, "+inf", 1.0])
        if rng.random() < 0.1:
            min_weight = float("nan")
        limit = rng.choice(
            [1, 2, 3, 20, 0, -1, None] if rng.random() < 0.2 else [1, 2, 3, 20, 0, -1]
        )
        got = self.both(
            lambda leg: field.get_linked(model, key, min_weight=min_weight, limit=limit)
        )
        r, p = got["redis"], got["postgres"]
        if r[0] == "err" or p[0] == "err":
            same = r[0] == p[0]
            self.check(
                "get_linked",
                same,
                lambda: f"{tag} get_linked({key!r}, {min_weight!r}, {limit}): {got}",
            )
            if same:
                self.error_type(
                    r[1], p[1], f"{tag} get_linked({min_weight!r}, {limit})"
                )
            return
        self.check(
            "get_linked",
            got["redis"] == got["postgres"],
            lambda: f"{tag} get_linked({key!r}, {min_weight!r}, {limit}): {got}",
        )

    def propagate(self, rng, model, field, pool, tag) -> None:
        seeds = rng.sample(pool, rng.randint(1, min(3, len(pool))))
        if rng.random() < 0.15:
            seeds = seeds + seeds[:1]  # a repeated seed
        depth = rng.choice(
            [1, 2, 2, 3, 4, 6, 2.5, -1]
            + ([1e9, math.inf, 50] if rng.random() < 0.2 else [])
        )
        decay = rng.choice(
            [0.5, 0.5, 0.3, 0.9, round(rng.uniform(0.05, 0.99), 3)]
            + ([0.0, -0.5, -0.9, 1e-300] if rng.random() < 0.2 else [])
            + ([0.999999, 0.99] if rng.random() < 0.2 else [])
        )
        threshold = rng.choice(
            [0.01, 0.01, 0.001, 0.1, 1e-6]
            + ([0.0, -0.1, 1e-300, -math.inf] if rng.random() < 0.2 else [])
        )
        args = dict(depth=depth, decay_per_hop=decay, threshold=threshold)
        inside = pg_graph._bfs_in_sql_domain(threshold, decay, 1.0)
        self.sql_paths += inside
        self.exact_paths += not inside
        got = self.both(lambda leg: field.propagate(model, seeds, **args))
        self.check(
            "propagate",
            got["redis"] == got["postgres"],
            lambda: f"{tag} propagate({seeds!r}, {args}): {got}",
        )
        if inside:
            # The same question on the exact path (the Python replay of the
            # Lua queue), which the SQL domain would otherwise never reach.
            with mock.patch.object(pg_graph, "_bfs_in_sql_domain", lambda *a: False):
                self.leg("postgres")
                try:
                    exact: Any = ("ok", field.propagate(model, seeds, **args))
                except Exception as exc:
                    exact = ("err", exc)
            self.check(
                "propagate_exact",
                exact == got["redis"],
                lambda: f"{tag} propagate({seeds!r}, {args}) exact: {exact} vs {got}",
            )
            # And on each of the two SQL statements, whichever the depth
            # would pick: the visited-pruned layer statement, and the one
            # WITH RECURSIVE (only at small depth -- it cannot prune).
            forced = [("propagate_layered", 0)]
            if depth <= 8:
                forced.append(("propagate_recursive", 10**9))
            for cls, layers in forced:
                with mock.patch.object(
                    Defaults, "PG_GRAPH_RECURSIVE_MAX_LAYERS", layers
                ):
                    try:
                        other: Any = ("ok", field.propagate(model, seeds, **args))
                    except Exception as exc:
                        other = ("err", exc)
                self.check(
                    cls,
                    other == got["redis"],
                    lambda: f"{tag} propagate({seeds!r}, {args}) {cls}: "
                    f"{other} vs {got}",
                )
        if got["redis"][0] == "ok" and threshold > 0:
            trav = self.both(
                lambda leg: graph_traversal.traverse(
                    model,
                    seeds,
                    co_occurrence_field=field,
                    depth=min(int(depth), 6) if 0 < depth < math.inf else 1,
                    decay_per_hop=0.5,
                    threshold=threshold,
                )
            )
            r, p = trav["redis"], trav["postgres"]
            self.check(
                "traverse",
                r[0] == p[0] == "ok" and sorted(r[1]) == sorted(p[1]),
                lambda: f"{tag} traverse({seeds!r}): {trav}",
            )
            if r[0] == p[0] == "ok" and sorted(r[1]) == sorted(p[1]) and r[1] != p[1]:
                weights = [w for _, w in r[1]]
                if len(set(weights)) < len(weights):
                    self.documented["traverse_tie_order"] += 1
                else:
                    self.miss("traverse", f"{tag} order {trav}")

    def composite(self, rng, model, field, pool, record_keys, tag) -> None:
        if not record_keys:
            return
        seeds = rng.sample(pool, 1)
        extra = {k: round(rng.uniform(0, 1), 3) for k in rng.sample(record_keys, 1)}
        certainty = rng.choice([1.0, 0.2, 0.0])
        limit = rng.choice([1, 3, 10])

        def run(leg: str, records_only: bool) -> Any:
            boost = field.propagate(model, seeds, depth=2)
            boost.update(extra)
            if records_only:
                live = {k for k in boost if model.query.get(redis_key=k) is not None}
                boost = {k: v for k, v in boost.items() if k in live}
            ranked = model.query.composite_score(
                {"certainty": certainty},
                co_occurrence_boost=boost or None,
                limit=limit,
            )
            return [r.db_key.redis_key for r in ranked]

        got = self.both(lambda leg: run(leg, True))
        self.check(
            "composite",
            got["redis"] == got["postgres"],
            lambda: f"{tag} composite (record keys): {got}",
        )
        # With keys that have no record in the boost: Redis gives them top-K
        # slots and drops them at hydration, Postgres ranks records only (the
        # documented row) -- so Redis's list is Postgres's with entries
        # missing, never reordered.
        got = self.both(lambda leg: run(leg, False))
        r, p = got["redis"], got["postgres"]
        ok = r[0] == p[0] == "ok"
        if ok and r[1] != p[1]:
            it = iter(p[1])
            ok = all(k in it for k in r[1]) and len(p[1]) > len(r[1])
            if ok:
                self.documented["composite_orphan_slot"] += 1
        self.check("composite", ok, lambda: f"{tag} composite (all keys): {got}")

    def report(self) -> str:
        lines = [
            f"shapes: {self.shapes}; propagate calls on the SQL path "
            f"{self.sql_paths}, on the exact path {self.exact_paths}",
            "| class | checks | mismatches |",
            "|---|---:|---:|",
        ]
        for cls in sorted(set(self.checks) | set(self.mismatches)):
            lines.append(f"| {cls} | {self.checks[cls]} | {self.mismatches[cls]} |")
        if self.documented:
            lines.append(
                "documented (not failures): "
                + ", ".join(f"{k}={v}" for k, v in sorted(self.documented.items()))
            )
        for cls, examples in sorted(self.examples.items()):
            lines.append(f"\n{cls} examples:")
            lines.extend(f"  - {e}" for e in examples)
        return "\n".join(lines)


def run(pg: Any, seeds: list[int], shapes: int, *, verbose: bool = False) -> Probe:
    probe = Probe(pg, verbose=verbose)
    for seed in seeds:
        rng = random.Random(seed)
        for index in range(shapes):
            probe.shape(rng, seed, index)
    probe.wipe()
    set_backend(None)
    return probe


def main() -> None:
    import psycopg

    from popoto.backends.postgres import PostgresBackend

    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--shapes", type=int, default=200)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    url = os.environ["POPOTO_POSTGRES_URL"]
    schema = f"popoto_test_{uuid.uuid4().hex}"
    admin = psycopg.connect(url, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    pg = PostgresBackend(dsn=url, schema=schema)
    started = time.perf_counter()
    try:
        probe = run(pg, args.seeds, args.shapes, verbose=args.verbose)
    finally:
        pg.close()
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
    print(f"seeds {args.seeds}, {time.perf_counter() - started:.1f}s")
    print(probe.report())
    sys.exit(1 if sum(probe.mismatches.values()) else 0)


if __name__ == "__main__":
    main()
