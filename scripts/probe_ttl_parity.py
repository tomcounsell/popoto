#!/usr/bin/env python
"""Seeded two-leg probe: Redis (``EXPIRE``/``EXPIREAT``, the oracle) vs
Postgres (``_expires_at``, the read filter and the reaper), record expiry
and ``popoto.batch()`` (#759 M5).

Each *shape* is a random sequence of operations on a few records of one
``Meta.ttl`` model, run on both backends: saves carrying no TTL, the model's
``Meta.ttl``, a long, a short (expires during the run), a zero or a negative
``_ttl``, or an ``_expire_at`` in the past, the near future or the far
future; partial (``update_fields``) saves; deletes; ``popoto.batch()``es of
saves and deletes that are executed or reset. Every operation's return value
or exception is compared, and after each one every read:

=====================  =======================================================
class                  what is compared
=====================  =======================================================
``op``                 each operation's outcome (value, or exception type and
                       text)
``get``                ``query.get`` of every record: present, and its values
``exists``             ``Model.exists`` of every record
``filter``             ``filter(shape=…)``: which records, in key order
``range``              ``filter(shape=…, rank__gte=0)``: which records
``ttl``                each record's remaining TTL, bucketed: ``-2`` (none),
                       ``-1`` (no expiry), ``short`` (expires this run),
                       ``long``
``after``              the same reads once the short expiries have passed --
                       Redis by its own clock, Postgres by the server's
``post``               writes to the expired records afterwards: a save that
                       recreates one, a delete, an increment
``virtual``            Postgres alone against an exact model of the
                       sequence, at instants a frozen clock picks: just
                       before, *at* and after each expiry, and far ahead
``count``              ``query.count(shape=…)``
=====================  =======================================================

Redis's clock cannot be frozen, so the two-leg comparison runs in real time:
the operations of every shape first (Postgres's clock frozen at the instant
each shape starts, Redis's running a few milliseconds behind it), then one
sleep past every short expiry, then the ``after`` and ``post`` reads. The
short expiries sit at least a second away from every read on both sides of
the sleep, so no read races an expiry. The ``virtual`` class is where the
controllable clock earns its keep: :func:`popoto.backends.postgres.ttl.
frozen_clock` reads the Postgres side at the expiry instants themselves.

Documented mismatch classes are counted apart (``documented``) and listed in
``docs/features/postgres-backend.md``; any other mismatch is printed with its
seed, shape and inputs, and the run exits non-zero.

Safety: Redis is bound from ``REDIS_URL`` *before* importing popoto and
database 0 is refused (CLAUDE.md, #577); on Postgres the script creates its
own ``popoto_test_<uuid4().hex>`` schema and drops only that.

Usage::

    REDIS_URL=redis://localhost:6379/10 \\
    POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \\
        python scripts/probe_ttl_parity.py --seeds 1 2 3 --shapes 100
"""

from __future__ import annotations

import argparse
import datetime
import os
import random
import sys
import time
import uuid
from collections import Counter
from typing import Any, Callable, Optional

if __name__ == "__main__":
    _url = os.environ.get("REDIS_URL", "")
    if not _url or _url.rstrip("/").endswith("/0") or _url.count("/") < 3:
        sys.exit("set REDIS_URL=redis://localhost:6379/<n> (n != 0) before running")
    if not os.environ.get("POPOTO_POSTGRES_URL"):
        sys.exit("set POPOTO_POSTGRES_URL=postgresql://host:port/db")

import popoto  # noqa: E402
from popoto.backends import RecordId, get_backend, set_backend  # noqa: E402
from popoto.backends.postgres.ttl import frozen_clock  # noqa: E402


class ProbeTtl(popoto.Model):
    shape = popoto.KeyField()
    name = popoto.KeyField()
    value = popoto.Field(type=str, null=True)
    hits = popoto.IntField(default=0)
    rank = popoto.SortedField(type=float, default=0.0, partition_by="shape")

    class Meta:
        ttl = 3600


NAMES = ("a", "b", "c")
#: (label, _ttl, _expire_at offset from the shape's instant). ``None`` in
#: both: the instance carries no TTL (the stored one is kept).
TTLS = (
    ("keep", None, None),
    ("meta", "meta", None),
    ("long", 3600, None),
    ("short", 1, None),
    ("zero", 0, None),
    ("negative", -3, None),
    ("at_past", None, -100.0),
    ("at_soon", None, 2.0),
    ("at_far", None, 7200.0),
)
SHORT = 3.5  # seconds past every short expiry before the "after" reads
FAR = 100.0  # a remaining TTL above this is "long"


def _outcome(fn: Callable[[], Any]) -> Any:
    try:
        return ("ok", fn())
    except Exception as exc:  # noqa: BLE001 - compared across legs
        return ("!!", type(exc).__name__, str(exc))


def _bucket(ttl: int) -> Any:
    if ttl in (-1, -2):
        return ttl
    return "long" if ttl > FAR else "short"


class Shape:
    """One shape's records, operations and the exact Postgres model."""

    def __init__(self, seed: int, index: int, rng: random.Random) -> None:
        self.seed = seed
        self.index = index
        self.key = f"s{seed}x{index}"
        self.rng = rng
        self.t0 = 0.0
        # name -> (present, expires_at or None) as Postgres sees it
        self.model: dict[str, tuple[bool, Optional[float]]] = {}
        self.history: list[str] = []


class Probe:
    """Runs shapes against one Redis and one Postgres backend."""

    def __init__(self, pg: Any, *, verbose: bool = False) -> None:
        self.pg = pg
        self.verbose = verbose
        self.checks: Counter = Counter()
        self.mismatches: Counter = Counter()
        self.documented: Counter = Counter()
        self.examples: dict[str, list[str]] = {}
        self.undocumented: list[str] = []
        self.shapes = 0

    # -- plumbing -------------------------------------------------------------

    def leg(self, name: str) -> None:
        set_backend("redis" if name == "redis" else self.pg)

    def wipe(self) -> None:
        self.leg("redis")
        client = popoto.get_redis()
        for key in client.scan_iter(match="*ProbeTtl*", count=1000):
            client.delete(key)
        self.leg("postgres")
        table = self.pg._table(ProbeTtl._meta.spec)
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

    def both(self, shape: Shape, fn: Callable[[str], Any]) -> dict[str, Any]:
        out = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            if leg == "postgres":
                with frozen_clock(shape.t0):
                    out[leg] = _outcome(lambda: fn(leg))
            else:
                out[leg] = _outcome(lambda: fn(leg))
        return out

    # -- operations -----------------------------------------------------------

    def _instance(self, shape: Shape, name: str, ttl: tuple) -> Any:
        label, ttl_value, at = ttl
        rec = ProbeTtl(shape=shape.key, name=name)
        rec.value = f"{label}-{shape.rng.randrange(1000)}"
        rec.rank = float(shape.rng.randrange(-2, 5))
        rec.hits = shape.rng.randrange(5)
        if ttl_value == "meta":
            pass  # __init__ already applied Meta.ttl
        else:
            rec._ttl = ttl_value
        if at is not None:
            rec._ttl = None
            rec._expire_at = datetime.datetime.fromtimestamp(
                shape.t0 + at, tz=datetime.timezone.utc
            )
        return rec

    def _model_save(self, shape: Shape, name: str, ttl: tuple) -> None:
        """Postgres's view of a save at ``shape.t0`` (the frozen clock)."""
        label, ttl_value, at = ttl
        present, expires = shape.model.get(name, (False, None))
        live = present and (expires is None or expires > shape.t0)
        if ttl_value == "meta":
            ttl_value = ProbeTtl._meta.ttl
        if ttl_value is not None:
            shape.model[name] = (True, shape.t0 + ttl_value)
        elif at is not None:
            shape.model[name] = (True, float(int(shape.t0 + at)))
        else:
            shape.model[name] = (True, expires if live else None)

    def op_save(self, shape: Shape, partial: bool) -> None:
        name = shape.rng.choice(NAMES)
        ttl = shape.rng.choice(TTLS)
        state = shape.rng.getstate()
        shape.history.append(f"save({name}, {ttl[0]}, partial={partial})")

        def run(leg: str) -> Any:
            shape.rng.setstate(state)
            rec = self._instance(shape, name, ttl)
            if partial:
                return rec.save(update_fields=["value", "hits"])
            return rec.save()

        present, expires = shape.model.get(name, (False, None))
        absent = not present or (expires is not None and expires <= shape.t0)
        out = self.both(shape, run)
        shape.rng.setstate(state)
        self._instance(shape, name, ttl)  # advance identically
        if partial and absent:
            # Documented since M1 ("save(update_fields=…) on a record that
            # does not exist yet"): Redis writes a partial hash that only the
            # key reaches (and, after an expiry, whatever index entries the
            # expired record left behind), Postgres inserts a row with the
            # unlisted columns NULL. An expired record is an absent one here,
            # so this is that row, not a TTL one. Counted, then both legs are
            # put back in step.
            self.checks["op"] += 1
            self.documented["partial_save_of_absent_record"] += 1
            for leg in ("redis", "postgres"):
                self.leg(leg)
                ProbeTtl(shape=shape.key, name=name).delete()
            shape.model[name] = (False, None)
            shape.history.append(f"(resync: delete {name})")
            return
        self.compare_op(shape, out)
        if out["postgres"][0] == "ok":
            self._model_save(shape, name, ttl)

    def op_delete(self, shape: Shape) -> None:
        name = shape.rng.choice(NAMES)
        shape.history.append(f"delete({name})")
        out = self.both(
            shape, lambda leg: ProbeTtl(shape=shape.key, name=name).delete()
        )
        self.compare_op(shape, out)
        shape.model[name] = (False, None)

    def op_batch(self, shape: Shape) -> None:
        names = shape.rng.sample(NAMES, 2)
        ttls = [shape.rng.choice(TTLS) for _ in names]
        doomed = shape.rng.choice(NAMES)
        delete = shape.rng.random() < 0.3 and doomed not in names
        commit = shape.rng.random() < 0.8
        state = shape.rng.getstate()
        shape.history.append(
            f"batch(save {list(zip(names, [t[0] for t in ttls]))}"
            f"{', delete ' + doomed if delete else ''}, "
            f"{'execute' if commit else 'reset'})"
        )

        def run(leg: str) -> Any:
            shape.rng.setstate(state)
            pipe = popoto.batch()
            for name, ttl in zip(names, ttls):
                self._instance(shape, name, ttl).save(pipeline=pipe)
            if delete:
                ProbeTtl(shape=shape.key, name=doomed).delete(pipeline=pipe)
            if commit:
                pipe.execute()
            else:
                pipe.reset()
            return commit

        out = self.both(shape, run)
        shape.rng.setstate(state)
        for name, ttl in zip(names, ttls):
            self._instance(shape, name, ttl)
        self.compare_op(shape, out)
        if commit and out["postgres"][0] == "ok":
            for name, ttl in zip(names, ttls):
                self._model_save(shape, name, ttl)
            if delete:
                shape.model[doomed] = (False, None)

    def compare_op(self, shape: Shape, out: dict) -> None:
        r, p = out["redis"], out["postgres"]
        # A save returns the HSET reply on Redis and the same count on
        # Postgres; a batch returns its flag; a delete its bool.
        self.check(
            "op",
            r == p,
            lambda: f"{self.where(shape)} redis={r} postgres={p}",
        )

    # -- reads ----------------------------------------------------------------

    def reads(self, shape: Shape, leg: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in NAMES:
            got = ProbeTtl.query.get(shape=shape.key, name=name)
            out[f"get:{name}"] = None if got is None else (got.value, got.hits)
            out[f"exists:{name}"] = ProbeTtl.exists(shape=shape.key, name=name)
            out[f"ttl:{name}"] = _bucket(self.remaining(shape, leg, name))
        out["filter"] = sorted(r.name for r in ProbeTtl.query.filter(shape=shape.key))
        out["range"] = sorted(
            r.name for r in ProbeTtl.query.filter(shape=shape.key, rank__gte=0)
        )
        return out

    def remaining(self, shape: Shape, leg: str, name: str) -> int:
        key = ProbeTtl(shape=shape.key, name=name).db_key.redis_key
        if leg == "redis":
            return int(popoto.get_redis().ttl(key))
        backend = get_backend(ProbeTtl)
        return backend.ttl_remaining(
            ProbeTtl._meta.spec, [RecordId.from_key("ProbeTtl", key)]
        )[0]

    def compare_reads(self, shape: Shape, phase: str) -> None:
        out = self.both(shape, lambda leg: self.reads(shape, leg))
        r, p = out["redis"], out["postgres"]
        if r[0] != "ok" or p[0] != "ok":
            self.check(
                phase, False, lambda: f"{self.where(shape)} redis={r} postgres={p}"
            )
            return
        for key in r[1]:
            cls = key.split(":")[0] if phase == "read" else phase
            self.check(
                cls,
                r[1][key] == p[1][key],
                lambda key=key: f"{self.where(shape)} {key}: redis={r[1][key]} "
                f"postgres={p[1][key]}",
            )

    def compare_count(self, shape: Shape, phase: str) -> None:
        """``count(shape=…)``. Redis counts the shape's index set, which
        keeps an expired member until a hydrating read purges it (or
        ``clean_indexes``); Postgres counts live rows. Documented when Redis
        is ahead by exactly its expired-but-unpurged members."""
        out = self.both(shape, lambda leg: ProbeTtl.query.count(shape=shape.key))
        r, p = out["redis"], out["postgres"]
        self.checks["count"] += 1
        if r == p:
            return
        live = len(
            self.both(shape, lambda leg: ProbeTtl.query.filter(shape=shape.key))[
                "postgres"
            ][1]
        )
        if r[0] == p[0] == "ok" and p[1] == live and r[1] > p[1]:
            self.documented["count_orphans"] += 1
            return
        self.miss("count", f"{self.where(shape)} {phase}: redis={r} postgres={p}")

    def virtual(self, shape: Shape) -> None:
        """Postgres alone, at instants the frozen clock picks, against the
        exact model: present iff saved, not deleted, and ``_expires_at`` is
        unset or later than the instant (strictly)."""
        self.leg("postgres")
        expiries = sorted(
            {e for present, e in shape.model.values() if present and e is not None}
        )
        instants = {shape.t0, shape.t0 + 0.25, shape.t0 + 4000.0}
        for e in expiries:
            instants.update({e - 0.001, e, e + 0.001})
        # Only from the shape's own instant on: a row that had expired by a
        # later write was reaped by it, so the past is not observable.
        for at in sorted(i for i in instants if i >= shape.t0):
            with frozen_clock(at):
                seen = {
                    name
                    for name in NAMES
                    if ProbeTtl.query.get(shape=shape.key, name=name) is not None
                }
                counted = ProbeTtl.query.count(shape=shape.key)
            want = {
                name
                for name, (present, e) in shape.model.items()
                if present and (e is None or e > at)
            }
            self.check(
                "virtual",
                seen == want and counted == len(want),
                lambda: f"{self.where(shape)} at t0{at - shape.t0:+.3f}: "
                f"postgres={sorted(seen)}/{counted} model={sorted(want)}",
            )

    # -- after the short expiries --------------------------------------------

    def post(self, shape: Shape) -> None:
        """Writes to records that may have expired: a delete (reports 0 for an
        expired key), a save (a fresh record), an increment (documented: Redis
        rebuilds a partial hash, Postgres raises)."""
        name = shape.rng.choice(NAMES)
        out = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            out[leg] = _outcome(lambda: ProbeTtl(shape=shape.key, name=name).delete())
        self.check(
            "post",
            out["redis"] == out["postgres"],
            lambda: f"{self.where(shape)} delete({name}) {out}",
        )
        name = shape.rng.choice(NAMES)
        ttl = shape.rng.choice([t for t in TTLS if t[0] in ("keep", "long", "meta")])
        state = shape.rng.getstate()
        for leg in ("redis", "postgres"):
            self.leg(leg)
            shape.rng.setstate(state)
            rec = self._instance(shape, name, ttl)
            out[leg] = _outcome(rec.save)
        self.check(
            "post",
            out["redis"] == out["postgres"],
            lambda: f"{self.where(shape)} resave({name}, {ttl[0]}) {out}",
        )
        # An increment of a record saved by this instance, after the record
        # may have expired under it.
        for leg in ("redis", "postgres"):
            self.leg(leg)
            got = ProbeTtl.query.get(shape=shape.key, name=name)
            out[leg] = None if got is None else (got.value, got.hits)
        self.check(
            "post",
            out["redis"] == out["postgres"],
            lambda: f"{self.where(shape)} reload({name}) {out}",
        )

    def increment_after_expiry(self, shape: Shape) -> None:
        """Documented divergence: an increment through an instance whose
        record expired (or was deleted) under it. Redis's script HSETs the
        field onto a fresh key -- a partial hash, outside the class set;
        Postgres raises ``ModelException``. Counted, then both legs are put
        back in step by deleting the record."""
        expired = [
            name
            for name, (present, e) in shape.model.items()
            if present and e is not None and e <= time.time() - 1
        ]
        if not expired:
            return
        name = expired[0]
        out = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            rec = ProbeTtl(shape=shape.key, name=name)
            rec._saved_field_values = {"name": name}  # "loaded" before expiry
            out[leg] = _outcome(lambda: rec.atomic_increment("hits", 1))
        r, p = out["redis"], out["postgres"]
        self.checks["post"] += 1
        if r == ("ok", 1) and p[:2] == ("!!", "ModelException"):
            self.documented["increment_after_expiry"] += 1
        else:
            self.miss("post", f"{self.where(shape)} increment({name}) {out}")
        for leg in ("redis", "postgres"):
            self.leg(leg)
            ProbeTtl(shape=shape.key, name=name).delete()

    # -- driving --------------------------------------------------------------

    def where(self, shape: Shape) -> str:
        return f"seed={shape.seed} shape={shape.index} [{'; '.join(shape.history)}]"

    def phase_a(self, shape: Shape) -> None:
        shape.t0 = time.time()
        for _ in range(shape.rng.randrange(2, 7)):
            roll = shape.rng.random()
            if roll < 0.55:
                self.op_save(shape, partial=shape.rng.random() < 0.2)
            elif roll < 0.75:
                self.op_delete(shape)
            else:
                self.op_batch(shape)
            self.compare_reads(shape, "read")
        self.compare_count(shape, "before")
        self.virtual(shape)

    def phase_c(self, shape: Shape) -> None:
        # count first: before a hydrating read has purged Redis's orphans.
        self.compare_count(shape, "after")
        self.compare_reads(shape, "after")
        self.compare_count(shape, "after-reads")
        self.increment_after_expiry(shape)
        self.post(shape)

    def run_seed(self, seed: int, shapes: int) -> None:
        rng = random.Random(seed)
        batch = [Shape(seed, i, random.Random(rng.random())) for i in range(shapes)]
        for shape in batch:
            self.phase_a(shape)
            self.shapes += 1
        # Every short expiry (ttl 1, or an _expire_at <= 2 s ahead) of the
        # last shape is SHORT - 2 s behind us after this.
        time.sleep(SHORT)
        for shape in batch:
            self.phase_c(shape)

    def report(self) -> str:
        lines = [
            f"shapes: {self.shapes}",
            "| class | checks | mismatches |",
            "|---|---:|---:|",
        ]
        for cls in sorted(self.checks):
            lines.append(f"| {cls} | {self.checks[cls]} | {self.mismatches[cls]} |")
        lines.append("\ndocumented (counted apart, not failures):")
        for cls in sorted(self.documented):
            lines.append(f"  {cls}: {self.documented[cls]}")
        for cls, examples in sorted(self.examples.items()):
            lines.append(f"\n{cls} examples:")
            lines.extend(f"  - {e}" for e in examples)
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "shapes": self.shapes,
            "checks": dict(self.checks),
            "documented": dict(self.documented),
            "undocumented": list(self.undocumented),
        }


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
    ap.add_argument("--shapes", type=int, default=100)
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
