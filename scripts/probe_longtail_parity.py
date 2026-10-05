#!/usr/bin/env python
"""Seeded two-leg probe: Redis (the Lua oracle) vs Postgres, the long-tail
fields (#759 M5): ``CyclicDecayField``, ``TDValueField`` and
``PredictionLedgerMixin``.

Each *shape* builds the same random state on both backends, then asks both
legs the same questions and compares the answers. ``time.time`` is frozen
per shape so both legs stamp and score at one instant.

==================  ==========================================================
class               what is compared
==================  ==========================================================
``cyclic_rank``     ``rank_decayed`` member order and scores, bit for bit
                    (``CYCLIC_DECAY_LUA`` vs the SQL), over planted clocks,
                    cycles (periods, amplitudes and phases from ordinary to
                    ``0``/``NaN``/``inf``/``1e300``), pressure entries, base
                    scores and confidence; ``now`` and the rate vary per call
``cyclic_merge``    the stored cycles and pressure after each ``save()``
                    (``CYCLES_MERGE_LUA``) under random declarations, and the
                    #698 reset log lines
``cyclic_adjust``   ``strengthen_cycle`` / ``weaken_cycle`` returns and the
                    stored cycles (``CYCLES_ADJUST_LUA``), random factors
``cyclic_query``    ``top_by_decay`` order and ``composite_score`` over the
                    cyclic arm (order, scores to 1e-9: the Redis arm is
                    ``%.14g``-rounded before ``ZADD``)
``td``              every ``td_update`` reply and the reloaded value
                    (``TD_UPDATE_LUA``), bit for bit
``ledger``          ``record`` / ``resolve`` / ``auto_resolve`` returns and
                    exceptions, ``get_prediction_data`` (types included: the
                    script's ``cmsgpack`` re-pack), ``get_highest_errors``
                    under Redis's rank arithmetic, ``error_summary``
``observe``         ``on_context_used`` over a model with every long-tail
                    field: cycles, pressure, confidence and ledger state after
==================  ==========================================================

Documented classes (counted, not failures): a ``NaN`` ranking score (the
Lua comparator is inconsistent around it -- M2a's rule: every member's score
must agree and Postgres's order must be the sorted one), and a ``NaN``
``td_update`` value's sign (Lua prints ``-nan``/``nan`` by platform;
``numeric`` has one ``NaN``), and a ``NaN`` prediction error (Redis's
``ResponseError`` after the script's un-rolled-back ``HSET``, Postgres's
``ValueError`` before any write: the shape is counted from that step on).

Safety: Redis is bound from ``REDIS_URL`` *before* importing popoto and
database 0 is refused (CLAUDE.md, #577); on Postgres the script creates its
own ``popoto_test_<uuid4().hex>`` schema and drops only that.

Usage::

    REDIS_URL=redis://localhost:6379/7 \\
    POPOTO_POSTGRES_URL=postgresql://localhost:5432/<db> \\
        python scripts/probe_longtail_parity.py --seeds 1 2 3 --shapes 500
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import random
import struct
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

import msgpack  # noqa: E402

import popoto  # noqa: E402
from popoto import (  # noqa: E402
    AccessTrackerMixin,
    ConfidenceField,
    CyclicDecayField,
    ObservationProtocol,
)
from popoto.backends import RecordId, set_backend  # noqa: E402
from popoto.backends.postgres.memory import partition_where  # noqa: E402
from popoto.fields.constants import Defaults  # noqa: E402
from popoto.fields.decaying_sorted_field import (  # noqa: E402
    confidence_modulation_args,
    resolve_confidence_modulation_field,
)
from popoto.fields.prediction_ledger import PredictionLedgerMixin  # noqa: E402
from popoto.fields.td_value_field import TDValueField  # noqa: E402

DAY = 86400.0
REL_TOL = 1e-9
NAME_CHARS = "aBz0_:-~ é"


class LtCyc(popoto.Model):
    name = popoto.KeyField()
    agent = popoto.KeyField()
    w = popoto.FloatField(null=True)
    relevance = CyclicDecayField(
        decay_rate=0.5,
        partition_by="agent",
        base_score_field="w",
        cycles=[(31536000, 5.0, 0)],
        pressure_rate=0.1,
    )
    certainty = ConfidenceField()


class LtCycPlain(popoto.Model):
    name = popoto.KeyField()
    relevance = CyclicDecayField(
        decay_rate=0.3, cycles=[(86400, 2.0, 0), (604800, 3.0, 100)]
    )


class LtTd(popoto.Model):
    name = popoto.KeyField()
    q_value = TDValueField(null=True)


class LtLedger(PredictionLedgerMixin, popoto.Model):
    name = popoto.KeyField()
    certainty = ConfidenceField()


class LtFull(AccessTrackerMixin, PredictionLedgerMixin, popoto.Model):
    name = popoto.KeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5, cycles=[(604800, 4.0, 0)], pressure_rate=0.2
    )
    certainty = ConfidenceField()


MODELS = (LtCyc, LtCycPlain, LtTd, LtLedger, LtFull)
CYCLIC_MODELS = (LtCyc, LtCycPlain)


# -- comparison helpers ----------------------------------------------------------


def _bits(x: float) -> bytes:
    return struct.pack(">d", x)


def _same(a: float, b: float) -> bool:
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    if math.isinf(a) or math.isinf(b) or a == 0 or b == 0:
        return a == b
    return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=0.0)


def _identical(a: Any, b: Any) -> bool:
    """Equal values of equal types, ``NaN`` equal to ``NaN``, floats bit for
    bit except the sign of a zero... and of a ``NaN``."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        if math.isnan(a):
            return math.isnan(b)
        return _bits(a) == _bits(b) or (a == 0 and b == 0)
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_identical(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(_identical(x, y) for x, y in zip(a, b))
    return a == b


def _numeq(a: Any, b: Any) -> bool:
    """``_identical``, except an ``int`` equals the ``float`` of its value."""
    num = (int, float)
    if isinstance(a, num) and isinstance(b, num) and not isinstance(a, bool):
        if isinstance(a, float) and math.isnan(a):
            return isinstance(b, float) and math.isnan(b)
        return a == b and not isinstance(b, bool)
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_numeq(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(_numeq(x, y) for x, y in zip(a, b))
    return a == b


def _first_diff(r: Any, p: Any) -> str:
    """The first differing pair of two traces, for a readable mismatch."""
    if not isinstance(r, list) or not isinstance(p, list):
        return f"redis={r!r} pg={p!r}"
    for i, (a, b) in enumerate(zip(r, p)):
        if not _identical(a, b):
            return f"first diff at {i}: redis={a!r}\n   pg={b!r}"
    return f"lengths {len(r)} vs {len(p)}"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class Probe:
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
        self.wild = False

    # -- plumbing -----------------------------------------------------------------

    def leg(self, name: str) -> None:
        set_backend("redis" if name == "redis" else self.pg)

    def wipe(self) -> None:
        self.leg("redis")
        client = popoto.get_redis()
        for model in MODELS:
            for pattern in (f"*{model.__name__}*",):
                for key in client.scan_iter(match=pattern, count=1000):
                    client.delete(key)
        self.leg("postgres")
        for model in MODELS:
            table = self.pg._table(model._meta.spec)
            self.pg._run(f"TRUNCATE {table.qualified}")
        for engine in ("popoto_prediction_ledger", "popoto_prediction_error"):
            self.pg._run(f"TRUNCATE {self.pg._engine(engine)}")

    def miss(self, cls: str, detail: str) -> None:
        self.mismatches[cls] += 1
        bucket = self.examples.setdefault(cls, [])
        if len(bucket) < 5:
            bucket.append(detail[:1500])
        if self.verbose:
            print(f"MISMATCH {cls}: {detail[:3000]}", file=sys.stderr)

    def check(self, cls: str, ok: bool, detail: Callable[[], str]) -> None:
        self.checks[cls] += 1
        if not ok:
            self.miss(cls, detail())

    @staticmethod
    def attempt(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - compared across legs
            if str(exc).startswith("user_script:"):
                # A Lua error: Redis's ResponseError, Postgres's ValueError
                # with the same text (documented class difference).
                return ("!!", "LuaError", str(exc))
            return ("!!", type(exc).__name__)

    # -- raw state (both legs) -------------------------------------------------------

    def rid(self, record: Any) -> RecordId:
        return RecordId.from_key(record._meta.model_name, record.db_key.redis_key)

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

    def plant(self, leg: str, record: Any, cycles: Any, pressure: Any) -> None:
        """Write the cycles entry and the pressure entry raw (``None`` = no
        entry), bypassing the merge."""
        field = record._meta.fields["relevance"]
        key = record.db_key.redis_key
        if leg == "redis":
            client = popoto.get_redis()
            ck = field.get_cycles_hash_key(record, "relevance")
            pk = field.get_pressure_hash_key(record, "relevance")
            if cycles is None:
                client.hdel(ck, key)
            else:
                client.hset(ck, key, msgpack.packb(cycles))
            if pressure is None:
                client.hdel(pk, key)
            else:
                client.hset(pk, key, msgpack.packb(pressure))
            return
        table = self.pg._table(type(record)._meta.spec).qualified
        if cycles is None:
            cols: list[Any] = [None, None, None, None]
        else:
            cols = [
                [float(c[0]) for c in cycles],
                [float(c[1]) for c in cycles],
                [float(c[2]) for c in cycles],
                [float(c[3]) if len(c) > 3 else None for c in cycles],
            ]
        prs = (
            [None, None]
            if pressure is None
            else [
                float(pressure["rate"]),
                float(pressure["last_resolved"]),
            ]
        )
        self.pg._run(
            f'UPDATE {table} SET "relevance__cycle_period" = %s::float8[], '
            '"relevance__cycle_amp" = %s::float8[], '
            '"relevance__cycle_phase" = %s::float8[], '
            '"relevance__cycle_base" = %s::float8[], '
            '"relevance__pressure_rate" = %s, "relevance__pressure_at" = %s '
            'WHERE "_pk" = %s',
            cols + prs + [key],
        )

    def state(self, leg: str, record: Any) -> Any:
        field = record._meta.fields["relevance"]
        key = record.db_key.redis_key
        if leg == "redis":
            client = popoto.get_redis()
            raw_c = client.hget(field.get_cycles_hash_key(record, "relevance"), key)
            raw_p = client.hget(field.get_pressure_hash_key(record, "relevance"), key)
            p = None if raw_p is None else msgpack.unpackb(raw_p, raw=False)
            if p is not None:
                p = {"rate": p.get("rate"), "last_resolved": p.get("last_resolved")}
            return {
                "cycles": None if raw_c is None else msgpack.unpackb(raw_c, raw=False),
                "pressure": p,
            }
        st = self.pg.field_call(
            type(record)._meta.spec, "relevance", "state", self.rid(record)
        )
        if st is None:
            return {"cycles": None, "pressure": None}
        return st

    @staticmethod
    def norm_state(state: Any) -> Any:
        """Pressure numbers as floats on both legs (Python msgpack keeps the
        ``float`` the Python writers packed; cmsgpack never re-packs it)."""
        p = state["pressure"]
        if p is not None:
            p = {k: float(v) for k, v in p.items()}
        return {"cycles": state["cycles"], "pressure": p}

    # -- random values ---------------------------------------------------------------

    def period(self, rng: random.Random) -> float:
        roll = rng.random() if self.wild else rng.random() * 0.8
        if roll < 0.55:
            return float(rng.choice([86400, 604800, 2592000, 7776000, 31536000]))
        if roll < 0.8:
            return rng.uniform(1.0, 1e8)
        return rng.choice(
            [0.0, -5.0, math.nan, math.inf, 1e-300, 1e300, 1e-3, 0.5, 5e-324]
        )

    def amp(self, rng: random.Random) -> float:
        roll = rng.random() if self.wild else rng.random() * 0.7
        if roll < 0.7:
            return round(rng.uniform(0, 10), rng.randint(0, 6))
        return rng.choice(
            [0.0, 100.0, -3.0, 1e300, 1.7e308, math.inf, math.nan, 5e-324, 1e-305]
        )

    def phase(self, rng: random.Random, now: float) -> float:
        roll = rng.random() if self.wild else rng.random() * 0.9
        if roll < 0.4:
            return 0.0
        if roll < 0.6:
            return now - rng.uniform(0, 1e7)
        if roll < 0.9:
            return rng.uniform(-1e9, 3e9)
        return rng.choice([1e300, -1e300, math.inf, now])

    def cycles(self, rng: random.Random, now: float) -> Optional[list]:
        if rng.random() < 0.15:
            return None
        out = []
        for _ in range(rng.randint(0, 4)):
            c = [self.period(rng), self.amp(rng), self.phase(rng, now)]
            if rng.random() < 0.5:
                c.append(self.amp(rng))
            out.append(c)
        return out

    def pressure(self, rng: random.Random, now: float) -> Optional[dict]:
        if rng.random() < 0.25:
            return None
        rates = [0.1, 0.2, rng.uniform(0, 2), 0.0, -1.0]
        lasts = [
            now - rng.uniform(0, 60) * DAY,
            now - rng.uniform(0, 1) * DAY,
            now + DAY,
            now,
        ]
        if self.wild:
            rates += [math.nan, math.inf, 1e100, 1e-300]
            lasts += [0.0, -1e300, 1e300]
        rate = rng.choice(rates)
        last = rng.choice(lasts)
        return {"rate": rate, "last_resolved": last}

    def clock(self, rng: random.Random, now: float, last: Optional[float]) -> float:
        roll = rng.random() if self.wild else rng.random() * 0.9
        if last is not None and roll < 0.15:
            return last
        if roll < 0.3:
            return now
        if roll < 0.75:
            return now - rng.uniform(0, 60) * DAY
        if roll < 0.9:
            return now - rng.uniform(60, 5000) * DAY
        return rng.choice([now + 10 * DAY, now - 1e6 * DAY, -1e300])

    def now(self, rng: random.Random) -> float:
        if not self.wild or rng.random() < 0.7:
            return 1_790_000_000.0 + rng.uniform(0, 1e6)
        return rng.choice([0.0, 0.5, 1e16, 3600.0 * rng.randint(0, 168)])

    # -- one shape ---------------------------------------------------------------------

    def shape(self, rng: random.Random, seed: int, index: int) -> None:
        self.shapes += 1
        # A third of the shapes are "wild": NaN / inf / 1e300 / subnormal
        # inputs and pathological clocks, where the clamped SQL takes over.
        self.wild = rng.random() < 0.3
        tag = f"seed={seed} shape={index} wild={self.wild}"
        now = self.now(rng)
        with mock.patch("time.time", lambda: now):
            self.wipe()
            family = rng.choice(
                ["cyclic", "cyclic", "merge", "td", "ledger", "observe"]
            )
            getattr(self, f"shape_{family}")(rng, tag, now)
        set_backend(None)

    # cyclic ranking ---------------------------------------------------------------

    def shape_cyclic(self, rng: random.Random, tag: str, now: float) -> None:
        model = rng.choice(CYCLIC_MODELS)
        n = rng.randint(1, 16)
        agents = ["a", "b"][: rng.randint(1, 2)]
        specs = []
        names: set = set()
        last_ts = None
        for i in range(n):
            name = "".join(rng.choice(NAME_CHARS) for _ in range(rng.randint(1, 3)))
            name = f"{name}{i}" if name in names else name
            names.add(name)
            ts = self.clock(rng, now, last_ts)
            last_ts = ts
            spec = {
                "name": name,
                "ts": ts,
                "cycles": self.cycles(rng, now),
                "pressure": self.pressure(rng, now),
            }
            if model is LtCyc:
                spec["agent"] = rng.choice(agents)
                spec["w"] = rng.choice(
                    [None, 1.0, round(rng.uniform(0.01, 10), 3), -2.0, 0.0, 1e300]
                )
                spec["conf"] = (
                    None if rng.random() < 0.4 else rng.choice([0.05, 0.5, 0.95, 1.7])
                )
            specs.append(spec)
        records: dict[str, list] = {"redis": [], "postgres": []}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            for spec in specs:
                fields = {k: v for k, v in spec.items() if k in ("name", "agent", "w")}
                record = model(**fields)
                record.save()
                self.backdate(leg, record, spec["ts"])
                self.plant(leg, record, spec["cycles"], spec["pressure"])
                if spec.get("conf") is not None:
                    self.plant_confidence(leg, record, spec["conf"])
                records[leg].append(record)
        for _ in range(rng.randint(2, 4)):
            self.ranking(rng, model, specs, now, tag)
        self.top_and_composite(rng, model, specs, tag)

    def plant_confidence(self, leg: str, record: Any, confidence: float) -> None:
        if leg == "redis":
            field = record._meta.fields["certainty"]
            popoto.get_redis().hset(
                field.get_data_hash_key(record, "certainty"),
                record.db_key.redis_key,
                msgpack.packb(
                    {
                        "confidence": confidence,
                        "evidence_count": 10,
                        "corroborations": 10,
                        "contradictions": 0,
                    }
                ),
            )
            return
        table = self.pg._table(type(record)._meta.spec).qualified
        self.pg._run(
            f'UPDATE {table} SET "certainty__conf" = %s, "certainty__n" = 10, '
            '"certainty__corr" = 10, "certainty__contra" = 0 WHERE "_pk" = %s',
            [confidence, record.db_key.redis_key],
        )

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

    def ranking(self, rng, model, specs, now, tag) -> None:
        field = model._meta.fields["relevance"]
        rate = rng.choice(
            [None, None, 0.1, 0.5, 1.0, rng.uniform(0.01, 3)]
            + ([155.0, 1e-6, 0.0, -0.5] if rng.random() < 0.1 else [])
        )
        base = rng.choice([None, "w", ""]) if model is LtCyc else rng.choice([None, ""])
        modulate = model is LtCyc and rng.random() < 0.7
        strength = rng.choice([0.5, 0.5, 1.0, 0.0])
        n = rng.choice([None, 1, 3, len(specs), len(specs) + 2])
        partition = {}
        if field.partition_by:
            partition = {"agent": rng.choice([s["agent"] for s in specs])}
        out = {}
        with mock.patch.object(
            Defaults, "DECAY_CONFIDENCE_MODULATION_STRENGTH", strength
        ):
            for leg in ("redis", "postgres"):
                self.leg(leg)
                out[leg] = self.attempt(
                    lambda leg=leg: self._rank(
                        leg, model, now, rate, base, modulate, n, partition
                    )
                )

        def detail() -> str:
            return (
                f"{tag} {model.__name__} now={now!r} rate={rate} base={base!r} "
                f"mod={modulate} s={strength} n={n} part={partition} "
                f"redis={out['redis'] if isinstance(out['redis'], tuple) else out['redis'][:6]} "
                f"pg={out['postgres'] if isinstance(out['postgres'], tuple) else out['postgres'][:6]} "
                f"specs={specs}"
            )

        if isinstance(out["redis"], tuple) or isinstance(out["postgres"], tuple):
            if isinstance(out["redis"], tuple) and not isinstance(
                out["postgres"], tuple
            ):
                # The Lua's table.sort raises "invalid order function" when a
                # NaN score makes its comparator inconsistent (documented).
                if any(math.isnan(s) for _, s in out["postgres"]):
                    self.checks["cyclic_rank_nan_sort_error (documented)"] += 1
                    return
            self.check("cyclic_rank", out["redis"] == out["postgres"], detail)
            return
        with mock.patch.object(
            Defaults, "DECAY_CONFIDENCE_MODULATION_STRENGTH", strength
        ):
            self.leg("postgres")
            whole = self._rank(
                "postgres", model, now, rate, base, modulate, None, partition
            )
        if any(math.isnan(s) for leg in out.values() for _, s in leg) or any(
            math.isnan(sc) for _, sc in whole
        ):
            # A NaN anywhere in the partition, not only in the top n: the
            # Lua's sort can misorder real scores around it.
            with mock.patch.object(
                Defaults, "DECAY_CONFIDENCE_MODULATION_STRENGTH", strength
            ):
                full = {}
                for leg in ("redis", "postgres"):
                    self.leg(leg)
                    full[leg] = self.attempt(
                        lambda leg=leg: self._rank(
                            leg, model, now, rate, base, modulate, None, partition
                        )
                    )
            self.checks["cyclic_rank_nan (documented)"] += 1
            if isinstance(full["redis"], tuple):
                return
            rs, ps = dict(full["redis"]), dict(full["postgres"])
            self.check(
                "cyclic_rank",
                rs.keys() == ps.keys() and all(_identical(rs[k], ps[k]) for k in ps),
                lambda: f"{detail()} full {full}",
            )
            real = [s for _, s in full["postgres"] if not math.isnan(s)]
            self.check(
                "cyclic_rank",
                all(a >= b for a, b in zip(real, real[1:])),
                lambda: f"{detail()} full {full}",
            )
            return
        self.check(
            "cyclic_rank",
            [k for k, _ in out["redis"]] == [k for k, _ in out["postgres"]]
            and len(out["redis"]) == len(out["postgres"])
            and all(
                _identical(a, b)
                for (_, a), (_, b) in zip(out["redis"], out["postgres"])
            ),
            detail,
        )
        for (_, a), (_, b) in zip(out["redis"], out["postgres"]):
            self.scores += 1
            if _identical(a, b):
                self.bit_identical += 1
            elif math.isfinite(a) and math.isfinite(b) and a:
                self.max_rel_dev = max(self.max_rel_dev, abs(a - b) / abs(a))

    def top_and_composite(self, rng, model, specs, tag) -> None:
        field = model._meta.fields["relevance"]
        partition = (
            {"agent": rng.choice([s["agent"] for s in specs])}
            if field.partition_by
            else {}
        )
        n = rng.randint(1, len(specs) + 1)
        out: dict[str, Any] = {}
        comp: dict[str, Any] = {}
        kwargs = {
            "limit": rng.randint(1, len(specs) + 1),
            "temperature": rng.choice([1.0, 0.5]),
        }
        if rng.random() < 0.3:
            kwargs["min_score"] = rng.uniform(-1, 5)
        for leg in ("redis", "postgres"):
            self.leg(leg)
            q = model.query.filter(**partition).no_track()
            out[leg] = self.attempt(
                lambda q=q: [r.db_key.redis_key for r in q.top_by_decay(n=n)]
            )
            seen: list = []
            q = model.query.filter(**partition).no_track()
            comp[leg] = self.attempt(
                lambda q=q, seen=seen: (
                    q.composite_score(
                        {"relevance": 1.0},
                        post_filter=lambda k, s: seen.append((k, s)) or True,
                        **kwargs,
                    ),
                    seen,
                )[1]
            )
        self.leg("redis")
        scores = self.attempt(
            lambda: self._rank(
                "redis", model, time.time(), None, None, True, None, partition
            )
        )
        if isinstance(scores, tuple) or any(math.isnan(s) for _, s in scores):
            self.checks["cyclic_query_nan (documented)"] += 1
            return
        self.check(
            "cyclic_query",
            out["redis"] == out["postgres"],
            lambda: f"{tag} top_by_decay {out} specs={specs}",
        )
        r, p = comp["redis"], comp["postgres"]
        ok = (
            not isinstance(r, tuple)
            and not isinstance(p, tuple)
            and [k for k, _ in r] == [k for k, _ in p]
            and all(_same(a, b) for (_, a), (_, b) in zip(r, p))
        ) or (r == p)
        if not ok and not isinstance(r, tuple) and not isinstance(p, tuple):
            # The Redis arm is %.14g-rounded before ZADD and its min_score is
            # applied to the rounded values; a near-tie can order (or a
            # threshold admit) differently. Same members within 1e-9 counts.
            rd, pd = dict(r), dict(p)
            if (
                set(rd) == set(pd) and all(_same(rd[k], pd[k]) for k in rd)
            ) or self._rounding_only(r, p, kwargs):
                self.checks["cyclic_query_rounding (documented)"] += 1
                return
        self.check(
            "cyclic_query",
            ok,
            lambda: f"{tag} composite {kwargs} redis={r} pg={p} specs={specs}",
        )

    @staticmethod
    def _rounding_only(r, p, kwargs) -> bool:
        """The two lists differ only by members within 1e-9 of ``min_score``
        or of each other at the limit cut."""
        rd, pd = dict(r), dict(p)
        diff = set(rd) ^ set(pd)
        if not diff:
            return False
        ms = kwargs.get("min_score")
        for k in diff:
            score = rd.get(k, pd.get(k))
            if ms is not None and _same(score / kwargs["temperature"], ms):
                continue
            boundary = (r[-1][1] if r else None, p[-1][1] if p else None)
            if any(b is not None and _same(score, b) for b in boundary):
                continue
            return False
        return True

    # the merge and the adjustment -----------------------------------------------------

    def declaration(self, rng: random.Random) -> list:
        pool = [86400, 604800, 31536000, 2.5, 1e9]
        out = []
        for _ in range(rng.randint(0, 4)):
            period = rng.choice(pool)
            amp = rng.choice([0.0, 1.0, 2.0, 3.5, 5.0, rng.uniform(0, 10), 1e300])
            if rng.random() < 0.3:
                amp = int(amp) if amp < 1e18 else amp
            entry = (
                (period, amp)
                if rng.random() < 0.3
                else (
                    period,
                    amp,
                    rng.choice([0, 100, 777.5, -3]),
                )
            )
            out.append(entry)
        return out

    def shape_merge(self, rng: random.Random, tag: str, now: float) -> None:
        model = rng.choice(CYCLIC_MODELS)
        field = model._meta.fields["relevance"]
        original = (field.cycles, field.pressure_rate)
        names = [f"m{i}" for i in range(rng.randint(1, 3))]
        try:
            steps = []
            for _ in range(rng.randint(2, 7)):
                roll = rng.random()
                who = rng.choice(names)
                if roll < 0.45:
                    steps.append(
                        (
                            "save",
                            who,
                            self.declaration(rng),
                            rng.choice([0.0, 0.1, 0.5, -1.0, math.nan]),
                        )
                    )
                elif roll < 0.75:
                    factor = rng.choice(
                        [1.2, 0.8, 0.5, 2.0, rng.uniform(0, 3), 0.0, 1e-9, 1e300]
                        + ([math.inf, math.nan, -1.0] if rng.random() < 0.2 else [])
                        # tonumber() of the factor: nil (refused, nothing
                        # written) and a hex float (16), as the script reads.
                        + ([None, "abc", True, "0x10"] if rng.random() < 0.1 else [])
                    )
                    steps.append(("adjust", who, factor, rng.random() < 0.5))
                elif roll < 0.85:
                    steps.append(("resolve", who, None, None))
                elif roll < 0.95:
                    steps.append(("plant", who, self.cycles(rng, now), None))
                else:
                    steps.append(("export", who, None, None))
            results: dict[str, list] = {}
            for leg in ("redis", "postgres"):
                self.leg(leg)
                field.cycles, field.pressure_rate = original
                recs = {}
                for nm in names:
                    rec = model(name=nm, **({"agent": "a"} if model is LtCyc else {}))
                    rec.save()
                    recs[nm] = rec
                log = _Capture()
                lg = logging.getLogger("POPOTO.CyclicDecayField")
                lg.addHandler(log)
                prior = lg.level
                lg.setLevel(logging.INFO)
                trace = []
                planted: set = set()
                try:
                    for kind, who, a, b in steps:
                        rec = recs[who]
                        if kind == "plant":
                            planted.add(who)
                        elif kind == "save":
                            planted.discard(who)
                        if kind == "save":
                            field.cycles, field.pressure_rate = a, b
                            trace.append(("save", self.attempt(rec.save)))
                        elif kind == "adjust":
                            fn = rec.strengthen_cycle if b else rec.weaken_cycle
                            got = self.attempt(lambda fn=fn: fn("relevance", factor=a))
                            if not (isinstance(got, tuple) and got[:1] == ("!!",)):
                                # A refused adjustment (a nil factor) writes
                                # nothing, so a planted entry stays raw.
                                planted.discard(who)
                            trace.append(("adjust", got))
                        elif kind == "resolve":
                            trace.append(
                                (
                                    "resolve",
                                    self.attempt(
                                        lambda rec=rec: rec.resolve_pressure(
                                            "relevance"
                                        )
                                    ),
                                )
                            )
                        elif kind == "plant":
                            self.plant(leg, rec, a, self.state(leg, rec)["pressure"])
                        else:
                            trace.append(
                                (
                                    "export",
                                    CyclicDecayField.export_state(
                                        rec, "relevance", None
                                    ),
                                )
                            )
                        trace.append(
                            (
                                "state",
                                who,
                                self.norm_state(self.state(leg, rec)),
                                who in planted,
                            )
                        )
                finally:
                    lg.removeHandler(log)
                    lg.setLevel(prior)
                results[leg] = trace + [("log", log.lines)]
        finally:
            field.cycles, field.pressure_rate = original
        r, p = results["redis"], results["postgres"]
        ok = len(r) == len(p) and all(self._same_step(a, b) for a, b in zip(r, p))
        if not ok and len(r) == len(p):
            loose = all(
                self._same_step(a, b)
                or (a[0] == "state" and a[3] and _numeq(a, b))
                or (a[0] == "export" and _numeq(a, b))
                for a, b in zip(r, p)
            )
            if loose:
                # Documented: an entry written raw (import_state on Redis
                # packs Python msgpack) keeps the writer's float for an
                # integral number until the next save or adjustment
                # re-packs it through cmsgpack; Postgres reports cmsgpack's
                # int at once. Values identical.
                self.checks["cyclic_merge_raw_types (documented)"] += 1
                return
        self.check(
            "cyclic_merge",
            ok,
            lambda: f"{tag} {model.__name__} steps={steps}\n {_first_diff(r, p)}",
        )

    @staticmethod
    def _same_step(a: Any, b: Any) -> bool:
        if a[0] == "save":
            # save() returns the HSET-shaped reply; compare only success.
            return isinstance(a[1], tuple) == isinstance(b[1], tuple)
        if a[0] == "resolve":
            return isinstance(a[1], tuple) == isinstance(b[1], tuple)
        return _identical(a, b)

    # td -------------------------------------------------------------------------------

    def shape_td(self, rng: random.Random, tag: str, now: float) -> None:
        values = [
            None,
            Decimal("0"),
            Decimal("0.1"),
            Decimal(str(round(rng.uniform(-10, 10), rng.randint(0, 12)))),
            Decimal("1E+2"),
            Decimal("-0.30000000000000004"),
            Decimal("12345678901234567890.123456789"),
            Decimal("1e-320"),
        ]
        if self.wild:
            values += [Decimal("Infinity"), Decimal("NaN")]
            # Outside the double range: tonumber() gives ±inf / ±0.
            values += [Decimal("1e400"), Decimal("-1e-400"), Decimal("-1e400")]
        start = rng.choice(values)
        calls = []
        for _ in range(rng.randint(1, 6)):
            calls.append(
                {
                    "reward": rng.choice(
                        [1.0, 0.0, -1.0, rng.uniform(-5, 5), 1]
                        + ([1e308, -1e308, 5e-324] if self.wild else [])
                    ),
                    "max_future_q": rng.choice(
                        [0.0, rng.uniform(-5, 5), 2] + ([1e308] if self.wild else [])
                    ),
                    "alpha": rng.choice(
                        [0.1, 0.5, 1.0, rng.uniform(0, 1), 0]
                        + ([1e300] if self.wild else [])
                    ),
                    "gamma": rng.choice([0.95, 0.0, 1.0, rng.uniform(0, 1)]),
                }
            )
        out = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            rec = LtTd(name="q", q_value=start)
            rec.save()
            trace = []
            for kw in calls:
                trace.append(
                    self.attempt(
                        lambda kw=kw: TDValueField.td_update(rec, "q_value", **kw)
                    )
                )
                q = LtTd.query.get(name="q").q_value
                trace.append(q)
            out[leg] = trace
        r, p = out["redis"], out["postgres"]

        def same(a, b) -> bool:
            if isinstance(a, Decimal) and isinstance(b, Decimal):
                if a.is_nan() or b.is_nan():
                    if a.is_nan() and b.is_nan():
                        self.checks["td_nan_sign (documented)"] += 1
                        return True
                    return False
                return a == b
            if a is None or b is None:
                return a is b
            return _identical(a, b)

        self.check(
            "td",
            len(r) == len(p) and all(same(a, b) for a, b in zip(r, p)),
            lambda: f"{tag} start={start!r} calls={calls}\n redis={r}\n pg={p}",
        )

    # ledger ---------------------------------------------------------------------------

    def value(self, rng: random.Random, depth: int = 0) -> Any:
        roll = rng.random()
        if depth > 3 or roll < 0.55:
            return rng.choice(
                [
                    1.0,
                    0.5,
                    -0.0,
                    3,
                    -7,
                    2**53 + 1,
                    2**62 + 1,
                    "s",
                    "",
                    "é",
                    b"bytes",
                    True,
                    False,
                    None,
                    0.1,
                    1e300,
                    math.inf,
                    rng.uniform(-1, 1),
                ]
            )
        if roll < 0.75:
            return [self.value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
        return self.mapping(rng, depth + 1)

    def mapping(self, rng: random.Random, depth: int = 0) -> dict:
        out: dict = {}
        int_keys = rng.random() < 0.08
        for i in range(rng.randint(0, 3)):
            key: Any = i + 1 if int_keys else rng.choice(["x", "y", "z", "rel", "k"])
            out[key] = self.value(rng, depth)
        return out

    def shape_ledger(self, rng: random.Random, tag: str, now: float) -> None:
        names = [f"p{i}" for i in range(rng.randint(1, 4))]
        steps = []
        for _ in range(rng.randint(2, 9)):
            who = rng.choice(names)
            roll = rng.random()
            if roll < 0.35:
                steps.append(("record", who, self.mapping(rng)))
            elif roll < 0.55:
                steps.append(("resolve", who, self.mapping(rng)))
            elif roll < 0.75:
                steps.append(
                    (
                        "auto",
                        who,
                        rng.choice(["acted", "dismissed", "contradicted", "used"]),
                    )
                )
            elif roll < 0.85:
                steps.append(("partition", who, rng.choice(["default", "p2"])))
            else:
                steps.append(("delete", who, None))
        limits = [rng.choice([10, 1, 2, 0, -1, -2, 100])]
        # Drawn once, before the legs: a draw inside the leg loop gave the
        # two legs different limits (and shifted the stream between them).
        summary_limits = {part: rng.choice([100, 2]) for part in ("default", "p2")}
        out = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            recs = {nm: LtLedger(name=nm) for nm in names}
            for rec in recs.values():
                rec.save()
            trace = []
            for kind, who, arg in steps:
                rec = recs[who]
                if kind == "record":
                    trace.append(
                        self.attempt(
                            lambda rec=rec, arg=arg: PredictionLedgerMixin.record_prediction(
                                rec, predicted=arg
                            )
                        )
                    )
                elif kind == "resolve":
                    trace.append(
                        self.attempt(
                            lambda rec=rec, arg=arg: PredictionLedgerMixin.resolve_prediction(
                                rec, actual=arg
                            )
                        )
                    )
                elif kind == "auto":
                    trace.append(
                        self.attempt(
                            lambda rec=rec, arg=arg: PredictionLedgerMixin.auto_resolve(
                                rec, arg
                            )
                        )
                    )
                elif kind == "partition":
                    rec._pl_partition = arg
                else:
                    trace.append(self.attempt(rec.delete))
                    rec._pl_partition = rec.__dict__.get("_pl_partition", "default")
                trace.append(
                    (
                        "data",
                        self.attempt(
                            lambda rec=rec: PredictionLedgerMixin.get_prediction_data(
                                rec
                            )
                        ),
                        self.attempt(
                            lambda rec=rec: ConfidenceField.get_confidence_data(
                                rec, "certainty"
                            )
                        ),
                    )
                )
            for part in ("default", "p2"):
                for limit in limits:
                    trace.append(
                        (
                            "highest",
                            self.attempt(
                                lambda part=part, limit=limit: PredictionLedgerMixin.get_highest_errors(
                                    LtLedger, part, limit
                                )
                            ),
                        )
                    )
                trace.append(
                    (
                        "summary",
                        self.attempt(
                            lambda part=part: PredictionLedgerMixin.error_summary(
                                LtLedger, part, limit=summary_limits[part]
                            )
                        ),
                    )
                )
            out[leg] = trace
        r, p = out["redis"], out["postgres"]
        same = [len(r) == len(p) and _identical(a, b) for a, b in zip(r, p)]
        if len(r) == len(p) and not all(same):
            first = r[same.index(False)], p[same.index(False)]
            if first == (("!!", "ResponseError"), ("!!", "ValueError")):
                # Documented: a NaN prediction error. The script's ZADD
                # refuses it after its HSET, which Redis does not roll back;
                # Postgres refuses it before writing. The legs' ledgers part
                # from there, so the shape is counted, not compared further.
                self.checks["ledger_nan_error (documented)"] += 1
                return
        self.check(
            "ledger",
            len(r) == len(p) and all(_identical(a, b) for a, b in zip(r, p)),
            lambda: f"{tag} steps={steps}\n {_first_diff(r, p)}",
        )

    # observe --------------------------------------------------------------------------

    def shape_observe(self, rng: random.Random, tag: str, now: float) -> None:
        names = [f"o{i}" for i in range(rng.randint(1, 4))]
        plan = {
            nm: {
                "predict": rng.random() < 0.6,
                "conf": rng.choice([None, 0.05, 0.5, 0.95]),
                "reads": rng.randint(0, 2),
                "pressure": rng.random() < 0.5,
            }
            for nm in names
        }
        rounds = [
            {
                nm: rng.choice(
                    ["acted", "dismissed", "deferred", "contradicted", "used"]
                )
                for nm in names
                if rng.random() < 0.8
            }
            for _ in range(rng.randint(1, 3))
        ]
        out = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            recs = {}
            for nm in names:
                rec = LtFull(name=nm)
                rec.save()
                recs[nm] = rec
                if plan[nm]["conf"] is not None:
                    self.plant_confidence(leg, rec, plan[nm]["conf"])
                if plan[nm]["pressure"]:
                    self.plant(
                        leg,
                        rec,
                        self.state(leg, rec)["cycles"],
                        {"rate": 0.2, "last_resolved": now - 9 * DAY},
                    )
                if plan[nm]["predict"]:
                    PredictionLedgerMixin.record_prediction(rec, predicted={"x": 1.0})
                for _ in range(plan[nm]["reads"]):
                    rec.on_read()
            trace = []
            for outcomes in rounds:
                ObservationProtocol.on_context_used(
                    list(recs.values()),
                    {recs[nm].db_key.redis_key: o for nm, o in outcomes.items()},
                )
                for nm, rec in recs.items():
                    trace.append(
                        (
                            nm,
                            self.norm_state(self.state(leg, rec)),
                            ConfidenceField.get_confidence_data(rec, "certainty"),
                            PredictionLedgerMixin.get_prediction_data(rec),
                            rec.access_count,
                        )
                    )
            trace.append(PredictionLedgerMixin.get_highest_errors(LtFull))
            out[leg] = trace
        r, p = out["redis"], out["postgres"]
        self.check(
            "observe",
            len(r) == len(p) and all(_identical(a, b) for a, b in zip(r, p)),
            lambda: f"{tag} plan={plan} rounds={rounds}\n {_first_diff(r, p)}",
        )

    # -- report -------------------------------------------------------------------------

    def report(self) -> str:
        lines = [
            f"shapes: {self.shapes}",
            f"cyclic rank_decayed scores compared: {self.scores}, bit-identical "
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
