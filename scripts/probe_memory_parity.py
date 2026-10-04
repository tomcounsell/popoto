#!/usr/bin/env python
"""Seeded two-leg probe: Redis (the Lua oracle) vs Postgres, ranking and
memory state (#759 M2a).

Each *shape* builds the same random corpus on both backends -- ages
(fresh, sub-day, days, years, the future, shared timestamps, and a few
pathological clocks), base scores of every type the decay script reads
(floats from ``1e-300`` to ``1e305`` and negative, ints past ``2**53``,
``Decimal``s, strings), confidence signal sequences through
``update_confidence``, partitions, staged and confirmed reads -- then asks
both legs the same questions and compares the answers:

==================  ==========================================================
class               what is compared
==================  ==========================================================
``confidence``      every ``update_confidence`` return, then the stored state
``rank_order``      ``rank_decayed`` member order (field method vs backend)
``rank_score``      ``rank_decayed`` scores, 1e-9 relative (``inf``/``0`` exact)
``top_by_decay``    the public call's hydrated order
``composite``       ``composite_score`` order and scores (1e-9 relative)
``access``          staged reads, ``access_count``, ``last_accessed``
==================  ==========================================================

Rate, base field, modulation strength (including ``0`` and ``2000``),
partition and limit vary per query; ``time.time`` is frozen per shape so both
legs score at one instant. A mismatch is printed with its seed, shape and
inputs; the run exits non-zero if there is any.

Safety: Redis is bound from ``REDIS_URL`` *before* importing popoto and
database 0 is refused (CLAUDE.md, #577); on Postgres the script creates its
own ``popoto_test_<uuid4().hex>`` schema and drops only that.

Usage::

    REDIS_URL=redis://localhost:6379/10 \\
    POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \\
        python scripts/probe_memory_parity.py --seeds 1 2 3 --shapes 200
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
from decimal import Decimal
from typing import Any, Callable, Optional
from unittest import mock

if __name__ == "__main__":
    _url = os.environ.get("REDIS_URL", "")
    if not _url or _url.rstrip("/").endswith("/0") or _url.count("/") < 3:
        sys.exit("set REDIS_URL=redis://localhost:6379/<n> (n != 0) before running")
    if not os.environ.get("POPOTO_POSTGRES_URL"):
        sys.exit("set POPOTO_POSTGRES_URL=postgresql://host:port/db")

import popoto  # noqa: E402
from popoto import (  # noqa: E402
    AccessTrackerMixin,
    ConfidenceField,
    DecayingSortedField,
)
from popoto.backends import set_backend  # noqa: E402
from popoto.backends.postgres.memory import partition_where  # noqa: E402
from popoto.fields.constants import Defaults  # noqa: E402
from popoto.fields.decaying_sorted_field import (  # noqa: E402
    confidence_modulation_args,
    resolve_confidence_modulation_field,
)

DAY = 86400.0
REL_TOL = 1e-9


class ProbeMem(AccessTrackerMixin, popoto.Model):
    name = popoto.KeyField()
    agent = popoto.KeyField()
    w = popoto.FloatField(null=True)
    k = popoto.IntField(null=True)
    d = popoto.DecimalField(null=True)
    s = popoto.StringField(null=True)
    relevance = DecayingSortedField(decay_rate=0.5, partition_by="agent")
    certainty = ConfidenceField()


class ProbeLow(AccessTrackerMixin, popoto.Model):
    name = popoto.KeyField()
    w = popoto.FloatField(null=True)
    relevance = DecayingSortedField(decay_rate=0.3, base_score_field="w")
    certainty = ConfidenceField(initial_confidence=0.3, evidence_cap=5)


class ProbeHigh(popoto.Model):
    name = popoto.KeyField()
    w = popoto.FloatField(null=True)
    relevance = DecayingSortedField(decay_rate=0.1)
    certainty = ConfidenceField(initial_confidence=0.9)


MODELS = (ProbeMem, ProbeLow, ProbeHigh)
NAME_CHARS = "aBz0_:-~ é"


def _same(a: float, b: float) -> bool:
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    if math.isinf(a) or math.isinf(b) or a == 0 or b == 0:
        return a == b
    return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=0.0)


class Probe:
    """Runs shapes against one Redis and one Postgres backend."""

    def __init__(self, pg: Any, *, verbose: bool = False) -> None:
        self.pg = pg
        self.verbose = verbose
        self.checks: Counter = Counter()
        self.mismatches: Counter = Counter()
        self.examples: dict[str, list[str]] = {}
        self.shapes = 0
        self.scores = 0
        self.bit_identical = 0
        self.max_rel_dev = 0.0

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
            table = self.pg._table(model._meta.spec)
            self.pg._run(f"TRUNCATE {table.qualified}")

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

    def backdate(self, leg: str, record: Any, ts: float) -> None:
        if leg == "redis":
            field = record._meta.fields["relevance"]
            zkey = field.get_partitioned_sortedset_db_key(record, "relevance")
            popoto.get_redis().zadd(zkey.redis_key, {record.db_key.redis_key: ts})
        else:
            table = self.pg._table(type(record)._meta.spec)
            self.pg._run(
                f'UPDATE {table.qualified} SET "relevance" = %s WHERE "_pk" = %s',
                [ts, record.db_key.redis_key],
            )

    def staged(self, leg: str, record: Any) -> int:
        if leg == "redis":
            return int(popoto.get_redis().llen(record._at_key("staged")))
        return int(record._access_state(self.pg)[2])

    # -- one shape --------------------------------------------------------------

    def shape(self, rng: random.Random, seed: int, index: int) -> None:
        self.shapes += 1
        tag = f"seed={seed} shape={index}"
        model = rng.choice(MODELS)
        now = 1_790_000_000.0 + rng.uniform(0, 1e6)
        with mock.patch("time.time", lambda: now):
            self.wipe()
            specs = self.corpus(rng, model, now)
            records: dict[str, dict[str, Any]] = {"redis": {}, "postgres": {}}
            for leg in ("redis", "postgres"):
                self.leg(leg)
                for spec in specs:
                    records[leg][spec["key"]] = self.build(leg, model, spec)
            self.signals(rng, model, specs, records, tag)
            self.reads(rng, model, specs, records, tag)
            for _ in range(rng.randint(2, 5)):
                self.ranking(rng, model, specs, now, tag)
            self.top_by_decay(rng, model, specs, now, tag)
            for _ in range(rng.randint(1, 3)):
                self.composite(rng, model, specs, now, tag)
        set_backend(None)

    def corpus(self, rng: random.Random, model: Any, now: float) -> list[dict]:
        n = rng.randint(1, 24)
        agents = ["a", "b", "c"][: rng.randint(1, 3)]
        names: set = set()
        out = []
        last_ts: Optional[float] = None
        for i in range(n):
            name = "".join(rng.choice(NAME_CHARS) for _ in range(rng.randint(1, 4)))
            name = f"{name}{i}" if name in names else name
            names.add(name)
            roll = rng.random()
            if last_ts is not None and roll < 0.15:
                ts = last_ts  # an exact tie
            elif roll < 0.25:
                ts = now  # fresh: the 0.01-day floor
            elif roll < 0.4:
                ts = now - rng.uniform(0, DAY)
            elif roll < 0.75:
                ts = now - rng.uniform(1, 60) * DAY
            elif roll < 0.88:
                ts = now - rng.uniform(60, 5000) * DAY
            elif roll < 0.94:
                ts = now + rng.uniform(0, 30) * DAY  # the future
            else:
                ts = rng.choice([now - 1e6 * DAY, -1e300, -math.inf, now - 1e9])
            last_ts = ts
            spec: dict[str, Any] = {"name": name, "ts": ts, "w": self.base(rng)}
            if model is ProbeMem:
                spec["agent"] = rng.choice(agents)
                spec["k"] = rng.choice([1, 7, -3, 0, 2**60 + 1, None])
                spec["d"] = rng.choice(
                    [
                        Decimal("1.5"),
                        Decimal("0.1"),
                        Decimal("-2"),
                        Decimal("1E+2"),
                        None,
                    ]
                )
                spec["s"] = rng.choice(["9.5", "x", None])
            out.append(spec)
        for spec in out:
            spec["key"] = model(
                **{k: v for k, v in spec.items() if k in ("name", "agent")}
            ).db_key.redis_key
        return out

    @staticmethod
    def base(rng: random.Random) -> Optional[float]:
        roll = rng.random()
        if roll < 0.6:
            return round(rng.uniform(0.01, 10), rng.randint(1, 6))
        if roll < 0.7:
            return None
        if roll < 0.8:
            return -rng.uniform(0.01, 5)
        return rng.choice([0.0, 1e-300, 1e300, 1e305, -1e305, 5e-324, 1e-30, 7e20])

    def build(self, leg: str, model: Any, spec: dict) -> Any:
        fields = {k: v for k, v in spec.items() if k in model._meta.fields}
        record = model(**fields)
        record.save()
        self.backdate(leg, record, spec["ts"])
        return record

    # -- comparisons -------------------------------------------------------------

    def signals(self, rng, model, specs, records, tag) -> None:
        special = [0.0, 1.0, 0.5, 0.49999, 0.9, 0.1]
        for spec in specs:
            if rng.random() < 0.4:
                continue
            seq = [
                rng.choice(special) if rng.random() < 0.3 else rng.random()
                for _ in range(rng.randint(1, 28))
            ]
            returned: dict[str, list[float]] = {}
            for leg in ("redis", "postgres"):
                self.leg(leg)
                record = records[leg][spec["key"]]
                returned[leg] = [
                    ConfidenceField.update_confidence(record, "certainty", s)
                    for s in seq
                ]
            self.check(
                "confidence",
                returned["redis"] == returned["postgres"],
                lambda: f"{tag} {spec['key']} returns {returned} for {seq}",
            )
            states = {}
            for leg in ("redis", "postgres"):
                self.leg(leg)
                states[leg] = ConfidenceField.get_confidence_data(
                    records[leg][spec["key"]], "certainty"
                )
            self.check(
                "confidence",
                states["redis"] == states["postgres"],
                lambda: f"{tag} {spec['key']} state {states}",
            )

    def reads(self, rng, model, specs, records, tag) -> None:
        if not issubclass(model, AccessTrackerMixin):
            return
        plan = {s["key"]: (rng.randint(0, 4), rng.random()) for s in specs}
        result = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            out = {}
            for key, (n, roll) in plan.items():
                record = records[leg][key]
                for _ in range(n):
                    record.on_read()
                staged = self.staged(leg, record)
                if roll < 0.6:
                    promoted = record.confirm_access()
                elif roll < 0.8:
                    record.discard_staged_access()
                    promoted = None
                else:
                    promoted = None
                out[key] = (
                    staged,
                    promoted,
                    record.access_count,
                    record.last_accessed,
                    self.staged(leg, record),
                )
            result[leg] = out
        self.check(
            "access",
            result["redis"] == result["postgres"],
            lambda: f"{tag} {result}",
        )

    def ranking(self, rng, model, specs, now, tag) -> None:
        field = model._meta.fields["relevance"]
        rate = rng.choice(
            [None, None, 0.1, 0.5, 1.0, 2.0, rng.uniform(0.01, 3)]
            + ([155.0, 200.0, 1e-6, 0.0, -0.5] if rng.random() < 0.15 else [])
        )
        base = rng.choice(
            [None, "w", "w", "k", "d", "s", "", "missing"]
            if model is ProbeMem
            else [None, "w", "", "missing"]
        )
        modulate = rng.random() < 0.7
        strength = rng.choice(
            [0.5, 0.5, 0.5, 1.0, 0.0] + ([2000.0] if rng.random() < 0.1 else [])
        )
        n = rng.choice([None, 1, 2, 5, len(specs), len(specs) + 3])
        partition = {}
        if field.partition_by:
            partition = {"agent": rng.choice([s["agent"] for s in specs])}
        out = {}
        with mock.patch.object(
            Defaults, "DECAY_CONFIDENCE_MODULATION_STRENGTH", strength
        ):
            for leg in ("redis", "postgres"):
                self.leg(leg)
                out[leg] = self._rank(
                    leg, model, now, rate, base, modulate, n, partition
                )

        def detail() -> str:
            return (
                f"{tag} {model.__name__} rate={rate} base={base!r} mod={modulate} "
                f"s={strength} n={n} part={partition} redis={out['redis'][:6]} "
                f"pg={out['postgres'][:6]}"
            )

        if any(math.isnan(s) for leg in out.values() for _, s in leg):
            # Documented divergence: a NaN score (0 * inf) makes the Lua's
            # comparator inconsistent (`x > nan` is always false), so its
            # table.sort may misplace the NaN *and* misorder real scores
            # around it; Postgres sorts the real scores and ranks NaN last.
            # Over the full ranking every member must still get the same
            # score on both legs, and Postgres's order must be the sorted one.
            with mock.patch.object(
                Defaults, "DECAY_CONFIDENCE_MODULATION_STRENGTH", strength
            ):
                full = {}
                for leg in ("redis", "postgres"):
                    self.leg(leg)
                    full[leg] = self._rank(
                        leg, model, now, rate, base, modulate, None, partition
                    )
            self.checks["rank_nan (documented)"] += 1
            redis_scores, pg_scores = dict(full["redis"]), dict(full["postgres"])
            self.check(
                "rank_score",
                redis_scores.keys() == pg_scores.keys()
                and all(_same(redis_scores[k], pg_scores[k]) for k in pg_scores),
                lambda: f"{detail()} full {full}",
            )
            # Sorted on the full double (the reply is %.14g-rounded, so equal
            # rounded scores need not tie): non-increasing, NaN last.
            real = [s for _, s in full["postgres"] if not math.isnan(s)]
            self.check(
                "rank_order",
                all(a >= b for a, b in zip(real, real[1:]))
                and all(math.isnan(s) for _, s in full["postgres"][len(real) :]),
                lambda: f"{detail()} full {full}",
            )
            return
        self.check(
            "rank_order",
            [k for k, _ in out["redis"]] == [k for k, _ in out["postgres"]],
            detail,
        )
        scores_ok = len(out["redis"]) == len(out["postgres"]) and all(
            _same(a, b) for (_, a), (_, b) in zip(out["redis"], out["postgres"])
        )
        self.check("rank_score", scores_ok, detail)
        for (_, a), (_, b) in zip(out["redis"], out["postgres"]):
            self.scores += 1
            if a == b:
                self.bit_identical += 1
            elif math.isfinite(a) and math.isfinite(b) and a:
                self.max_rel_dev = max(self.max_rel_dev, abs(a - b) / abs(a))

    def _rank(self, leg, model, now, rate, base, modulate, n, partition):
        field = model._meta.fields["relevance"]
        if leg == "redis":
            values = [str(partition[p]) for p in field.partition_by]
            zkey = field.get_sortedset_db_key(model, "relevance", *values).redis_key
            conf = (
                confidence_modulation_args(model, field, "relevance", filters=partition)
                if modulate
                else None
            )
            raw = field.rank_decayed(
                zkey,
                now=now,
                n=n,
                confidence=conf,
                decay_rate=rate,
                base_score_field=base,
            )
            return [(raw[i].decode(), float(raw[i + 1])) for i in range(0, len(raw), 2)]
        conf_name = None
        if modulate and Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH:
            conf_name, _ = resolve_confidence_modulation_field(
                model, field, "relevance"
            )
        chosen = field.base_score_field if base is None else base
        scored = self.pg.rank_decayed(
            model._meta.spec,
            "relevance",
            now=now,
            n=n,
            where=partition_where(partition),
            decay_rate=rate,
            base_score_field=chosen or None,
            confidence_field=conf_name,
        )
        return [(rid.canonical, score) for rid, score in scored]

    def top_by_decay(self, rng, model, specs, now, tag) -> None:
        field = model._meta.fields["relevance"]
        n = rng.randint(1, len(specs) + 2)
        out = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            query = model.query.filter().no_track()
            if field.partition_by:
                query = query.filter(agent=specs[0]["agent"])
            out[leg] = [r.db_key.redis_key for r in query.top_by_decay(n=n)]
        partition = {"agent": specs[0]["agent"]} if field.partition_by else {}
        self.leg("redis")
        scores = self._rank("redis", model, now, None, None, True, None, partition)
        if any(math.isnan(s) for _, s in scores):
            # Documented divergence (see ranking): NaN placement.
            self.checks["top_by_decay_nan (documented)"] += 1
            return
        self.check(
            "top_by_decay", out["redis"] == out["postgres"], lambda: f"{tag} {out}"
        )

    def composite(self, rng, model, specs, now, tag) -> None:
        arms = ["relevance", "certainty"]
        if issubclass(model, AccessTrackerMixin):
            arms.append(rng.choice(["access_count", "access_score"]))
        chosen = rng.sample(arms, rng.randint(1, len(arms)))
        indexes = {
            a: rng.choice([1.0, 0.4, 0.3, 2.5, rng.uniform(0.01, 3)]) for a in chosen
        }
        kwargs: dict[str, Any] = {
            "limit": rng.randint(1, len(specs) + 2),
            "aggregate": rng.choice(["SUM", "SUM", "MAX", "MIN"]),
            "temperature": rng.choice([1.0, 1.0, 0.1, 3.0]),
        }
        if rng.random() < 0.3:
            kwargs["min_score"] = rng.uniform(0, 2)
        out: dict[str, list] = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            seen: list = []
            query = model.query.filter().no_track()
            if model._meta.fields["relevance"].partition_by:
                query = query.filter(agent=specs[0]["agent"])
            try:
                query.composite_score(
                    indexes,
                    post_filter=lambda key, score, seen=seen: seen.append((key, score))
                    or True,
                    **kwargs,
                )
                out[leg] = seen
            except Exception as exc:  # noqa: BLE001 - classified below
                out[leg] = [f"!! {type(exc).__name__}"]
        detail = lambda: f"{tag} {indexes} {kwargs} {out}"  # noqa: E731
        if out["redis"] == ["!! ResponseError"] and "relevance" in indexes:
            # Documented divergence: a NaN decay score (0 * inf, e.g. a -inf
            # clock with above-prior confidence) makes Redis's ZADD of the
            # decay arm refuse the whole query; Postgres ranks it as 0, the
            # value ZUNIONSTORE gives a NaN product.
            self.leg("redis")
            partition = (
                {"agent": specs[0]["agent"]}
                if model._meta.fields["relevance"].partition_by
                else {}
            )
            arm = self._rank("redis", model, now, None, None, True, None, partition)
            if any(math.isnan(score) for _, score in arm):
                self.checks["composite_nan (documented)"] += 1
                return
        self.check(
            "composite",
            [k for k, *_ in out["redis"]] == [k for k, *_ in out["postgres"]]
            and all(
                isinstance(a, tuple) and _same(a[1], b[1])
                for a, b in zip(out["redis"], out["postgres"])
            ),
            detail,
        )

    # -- report -------------------------------------------------------------------

    def report(self) -> str:
        lines = [
            f"shapes: {self.shapes}",
            f"rank_decayed scores compared: {self.scores}, bit-identical "
            f"{self.bit_identical}, max relative deviation {self.max_rel_dev:.3g}",
            "| class | checks | mismatches |",
            "|---|---:|---:|",
        ]
        for cls in sorted(self.checks):
            lines.append(f"| {cls} | {self.checks[cls]} | {self.mismatches[cls]} |")
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
