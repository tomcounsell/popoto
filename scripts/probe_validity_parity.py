#!/usr/bin/env python
"""Seeded two-leg probe: Redis (``SUPERSEDE_LUA`` and the decay gate, the
oracle) vs Postgres, validity intervals and supersession (#759 M3).

Each *shape* runs the same random sequence of operations on both backends --
saves (some declaring a past or future ``valid_from``, some re-declaring a
different one), ``supersede`` over a small pool of identities with close
instants before, at and after the incumbent's start, ``invalidate`` with and
without a successor (including one never saved), direct
``execute_supersede`` calls in every mode with explicit incumbents and
asserted starts, ``save_and_supersede`` / ``save_and_invalidate``, and
deletes -- comparing every return value and every raised exception (type and
text) as it goes. Then it compares the state and the reads:

=====================  =======================================================
class                  what is compared
=====================  =======================================================
``op``                 each operation's return value, or its exception's type
                       and message
``interval``           every record's ``valid_from`` / ``invalid_at`` /
                       ``ingested_at``
``links``              every record's ``superseded_by`` / ``supersedes``
``pointer``            every identity's open-claim pointer
``chain``              ``chain`` / ``superseded_by`` / ``supersedes`` from
                       every record
``excluded``           ``resolve_excluded_keys`` at several instants
``valid``              ``resolve_valid_keys`` at the same instants
``filter``             ``filter(validity__as_of=…)`` and ``__current=True/False``
``top_by_decay``       gated ranking order at each instant
``composite``          the masked ``composite_score`` order at each instant
=====================  =======================================================

The instants include the interval ends themselves (``<=`` vs ``<``), ``±inf``,
``1e308`` and NaN. ``time.time`` is a deterministic clock that advances
between operations identically on both legs. A mismatch is printed with its
seed, shape and inputs; the run exits non-zero if there is any.

Safety: Redis is bound from ``REDIS_URL`` *before* importing popoto and
database 0 is refused (CLAUDE.md, #577); on Postgres the script creates its
own ``popoto_test_<uuid4().hex>`` schema and drops only that.

Usage::

    REDIS_URL=redis://localhost:6379/10 \\
    POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \\
        python scripts/probe_validity_parity.py --seeds 1 2 3 --shapes 200
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
from typing import Any, Callable
from unittest import mock

if __name__ == "__main__":
    _url = os.environ.get("REDIS_URL", "")
    if not _url or _url.rstrip("/").endswith("/0") or _url.count("/") < 3:
        sys.exit("set REDIS_URL=redis://localhost:6379/<n> (n != 0) before running")
    if not os.environ.get("POPOTO_POSTGRES_URL"):
        sys.exit("set POPOTO_POSTGRES_URL=postgresql://host:port/db")

import popoto  # noqa: E402
from popoto import (  # noqa: E402
    ConfidenceField,
    DecayingSortedField,
    SupersessionProtocol,
    ValidityField,
)
from popoto.backends import RecordId, set_backend  # noqa: E402

INF = float("inf")
FIELD = "validity"


class ProbeClaim(popoto.Model):
    name = popoto.KeyField()
    relevance = DecayingSortedField(decay_rate=0.5)
    certainty = ConfidenceField()
    validity = ValidityField()


class ProbeScoped(popoto.Model):
    agent = popoto.KeyField()
    name = popoto.KeyField()
    relevance = DecayingSortedField(decay_rate=0.3, partition_by="agent")
    validity = ValidityField()


MODELS = (ProbeClaim, ProbeScoped)
IDENTITIES = ("a" * 16, "b" * 16, "c" * 16)
NAMES = ("a", "ab", "b", "c", "d", "e", "f", "z")


def _same_float(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b
    a, b = float(a), float(b)
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return a == b


def _outcome(fn: Callable[[], Any]) -> Any:
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - compared across legs
        return ("!!", type(exc).__name__, str(exc))
    return ("ok", result)


class Probe:
    """Runs shapes against one Redis and one Postgres backend."""

    def __init__(self, pg: Any, *, verbose: bool = False) -> None:
        self.pg = pg
        self.verbose = verbose
        self.checks: Counter = Counter()
        self.mismatches: Counter = Counter()
        self.examples: dict[str, list[str]] = {}
        self.shapes = 0
        self.clock = 0.0

    # -- plumbing -------------------------------------------------------------

    def now(self) -> float:
        return self.clock

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
            self.pg._run(f"TRUNCATE {table.qualified} CASCADE")

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

    def both(self, fn: Callable[[str], Any]) -> dict[str, Any]:
        out = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            out[leg] = _outcome(lambda: fn(leg))
        return out

    # -- leg-aware state reads ------------------------------------------------

    def state(self, leg: str, model: Any, key: str) -> tuple:
        if leg == "redis":
            keys = ValidityField.get_all_keys(model, FIELD)
            client = popoto.get_redis()
            scores = tuple(
                client.zscore(keys[i], key)
                for i in ("valid_from", "invalid_at", "ingested_at")
            )
            links = tuple(
                (lambda raw: raw.decode() if isinstance(raw, bytes) else raw)(
                    client.hget(keys[i], key)
                )
                for i in ("chain_fwd", "chain_rev")
            )
            return scores + links
        state = self.pg.field_call(
            model._meta.spec,
            FIELD,
            "interval",
            RecordId.from_key(model._meta.model_name, key),
        )
        if state is None:
            return (None,) * 5
        return (
            state["valid_from"],
            state["invalid_at"],
            state["ingested_at"],
            state["superseded_by"],
            state["supersedes"],
        )

    def pointer(self, leg: str, model: Any, digest: str) -> Any:
        if leg == "redis":
            raw = popoto.get_redis().get(
                ValidityField.get_open_pointer_key(model, FIELD, digest)
            )
            return raw.decode() if isinstance(raw, bytes) else raw
        return self.pg.field_call(model._meta.spec, FIELD, "pointer", digest)

    # -- one shape ----------------------------------------------------------------

    def shape(self, rng: random.Random, seed: int, index: int) -> None:
        self.shapes += 1
        tag = f"seed={seed} shape={index}"
        model = rng.choice(MODELS)
        agent = rng.choice(["x", "y"])
        self.clock = 1_790_000_000.0 + rng.uniform(0, 1e6)
        with mock.patch("time.time", self.now):
            self.wipe()
            names = rng.sample(NAMES, rng.randint(3, len(NAMES)))
            records: dict[str, dict[str, Any]] = {"redis": {}, "postgres": {}}
            starts: list[float] = []

            def make(leg: str, name: str, **kw: Any) -> Any:
                extra = {"agent": agent} if model is ProbeScoped else {}
                record = model(name=name, **extra, **kw)
                records[leg][name] = record
                return record

            for name in names[: rng.randint(2, len(names))]:
                declared = None
                if rng.random() < 0.25:
                    declared = self.clock + rng.choice([-5000.0, -1.0, 0.0, 3.0, 9e3])
                    starts.append(declared)
                self.op(
                    tag,
                    f"save {name} validity={declared}",
                    lambda leg, name=name, declared=declared: (
                        make(leg, name, validity=declared).save() and None
                    ),
                )
                starts.append(self.clock)
                self.clock += rng.choice([0.0, 0.5, 7.0, 3600.0])
            for _ in range(rng.randint(3, 12)):
                self.random_op(rng, tag, model, names, records, make, starts)
                self.clock += rng.choice([0.0, 0.25, 1.0, 60.0, 86400.0])
            self.compare_state(tag, model, names, records)
            instants = sorted(set(starts))[:6] + [
                self.clock,
                self.clock + 1.0,
                INF,
                -INF,
                1e308,
                float("nan"),
            ]
            for state in self._states(model, names):
                for value in state[:2]:
                    if value is not None:
                        instants.append(value)
            for as_of in instants:
                self.compare_reads(tag, model, agent, names, as_of)
        set_backend(None)

    def _states(self, model: Any, names: list[str]) -> list[tuple]:
        out = []
        for name in names:
            key = self._key(model, name)
            out.append(self.state("redis", model, key))
        return out

    @staticmethod
    def _key(model: Any, name: str, agent: str = "x") -> str:
        if model is ProbeScoped:
            return f"ProbeScoped:{agent}:{name}"
        return f"ProbeClaim:{name}"

    def op(
        self,
        tag: str,
        label: str,
        fn: Callable[[str], Any],
        *,
        rolled_back: Any = None,
    ) -> None:
        out = self.both(fn)
        r, p = out["redis"], out["postgres"]
        if (
            rolled_back is not None
            and r[0] == p[0] == "!!"
            and r[1] == p[1]
            and _token(r[2]) == _token(p[2])
        ):
            # Documented divergence: save_and_* that fails in its close.
            # Redis's MULTI/EXEC applied the successor's save (no rollback)
            # and redis-py prefixes the reply with "Command # N (...) of
            # pipeline caused error:"; Postgres rolled the whole unit back.
            # Same type, same token. Re-sync by deleting the successor Redis
            # kept, so the rest of the shape compares like with like.
            self.checks["op_save_and_rollback (documented)"] += 1
            self.leg("redis")
            kept = rolled_back()
            if kept is not None:
                kept.delete()
            return
        self.check(
            "op",
            _results_equal(out["redis"], out["postgres"]),
            lambda: f"{tag} {label}: {out}",
        )

    def random_op(
        self,
        rng: random.Random,
        tag: str,
        model: Any,
        names: list[str],
        records: dict,
        make: Callable[..., Any],
        starts: list[float],
    ) -> None:
        kind = rng.choice(
            [
                "supersede",
                "supersede",
                "invalidate",
                "invalidate_ghost",
                "direct",
                "save_and_supersede",
                "save_and_invalidate",
                "resave",
                "resave_declared",
                "delete",
                "new",
            ]
        )
        name = rng.choice(names)
        other = rng.choice(names)
        identity = rng.choice(IDENTITIES)
        offset = rng.choice([None, -86400.0, -1.0, 0.0, 0.5, 30.0])
        at = None if offset is None else self.clock + offset
        if rng.random() < 0.2 and starts:
            at = rng.choice(starts)  # exactly at some start

        def rec(leg: str, n: str) -> Any:
            return records[leg].get(n) or make(leg, n)

        if kind == "supersede":
            self.op(
                tag,
                f"supersede {name} {identity} at={at}",
                lambda leg: SupersessionProtocol.supersede(
                    rec(leg, name), identity_key=identity, at=at
                ),
            )
        elif kind == "invalidate":
            succ = other if rng.random() < 0.6 else None
            self.op(
                tag,
                f"invalidate {name} by={succ} at={at}",
                lambda leg: SupersessionProtocol.invalidate(
                    rec(leg, name),
                    at=at,
                    superseded_by=rec(leg, succ) if succ else None,
                ),
            )
        elif kind == "invalidate_ghost":
            self.op(
                tag,
                f"invalidate {name} by=ghost",
                lambda leg: SupersessionProtocol.invalidate(
                    rec(leg, name), superseded_by=make(leg, "ghost")
                ),
            )
        elif kind == "direct":
            mode = rng.choice(["open", "supersede", "invalidate"])
            new_member = self._key(model, name) if rng.random() < 0.85 else ""
            old_member = self._key(model, other) if rng.random() < 0.4 else ""
            if rng.random() < 0.1:
                old_member = self._key(model, "gone")
            digest = identity if rng.random() < 0.6 else ""
            valid_from = rng.choice([None, self.clock - 10.0, self.clock])
            assert_vf = valid_from is not None and rng.random() < 0.5
            self.leg("redis")
            if (
                mode == "open"
                and new_member
                and not popoto.get_redis().exists(new_member)
            ):
                self.open_absent(tag, model, new_member, digest, valid_from, assert_vf)
                return
            self.op(
                tag,
                f"execute_supersede mode={mode} new={new_member} old={old_member} "
                f"digest={digest} vf={valid_from} assert={assert_vf} close={at}",
                lambda leg: ValidityField.execute_supersede(
                    model,
                    FIELD,
                    new_member=new_member,
                    mode=mode,
                    valid_from=valid_from,
                    close_at=at,
                    old_member=old_member,
                    identity_digest=digest,
                    assert_valid_from=assert_vf,
                ),
            )
        elif kind == "save_and_supersede":
            fresh = f"{name}{rng.randint(0, 9)}"
            if fresh not in names:
                names.append(fresh)
            self.op(
                tag,
                f"save_and_supersede {fresh} {identity} at={at}",
                lambda leg: SupersessionProtocol.save_and_supersede(
                    make(leg, fresh), identity_key=identity, at=at
                ).closed_key,
                rolled_back=lambda: records["redis"].get(fresh),
            )
        elif kind == "save_and_invalidate":
            fresh = f"{name}{rng.randint(0, 9)}"
            if fresh not in names:
                names.append(fresh)
            self.op(
                tag,
                f"save_and_invalidate {fresh} closes={other} at={at}",
                lambda leg: SupersessionProtocol.save_and_invalidate(
                    make(leg, fresh), closes=rec(leg, other), at=at
                ).closed_key,
                rolled_back=lambda: records["redis"].get(fresh),
            )
        elif kind == "resave":
            self.op(tag, f"resave {name}", lambda leg: rec(leg, name).save() and None)
        elif kind == "resave_declared":
            declared = self.clock + rng.choice([-100.0, 0.0])
            self.op(
                tag,
                f"resave {name} validity={declared}",
                lambda leg: _resave_declared(rec(leg, name), declared),
            )
        elif kind == "delete":
            self.op(tag, f"delete {name}", lambda leg: rec(leg, name).delete())
        else:
            fresh = f"{name}{rng.randint(0, 9)}"
            if fresh not in names:
                names.append(fresh)
            self.op(tag, f"save {fresh}", lambda leg: make(leg, fresh).save() and None)

    def open_absent(
        self,
        tag: str,
        model: Any,
        member: str,
        digest: str,
        valid_from: Any,
        assert_vf: bool,
    ) -> None:
        """Documented divergence: mode ``'open'`` naming a member with no
        record. The script's ``ZADD NX`` indexes it on Redis; on Postgres the
        interval is the record's row, so nothing is written. Both return
        ``None``; Redis is then re-synced (the member's index entries
        removed, the identity's pointer restored) so the shape continues
        like with like."""
        client = popoto.get_redis()
        pointer_key = (
            ValidityField.get_open_pointer_key(model, FIELD, digest) if digest else ""
        )
        previous = client.get(pointer_key) if pointer_key else None
        out = self.both(
            lambda leg: ValidityField.execute_supersede(
                model,
                FIELD,
                new_member=member,
                mode="open",
                valid_from=valid_from,
                assert_valid_from=assert_vf,
                identity_digest=digest,
            )
        )
        self.check(
            "op",
            _results_equal(out["redis"], out["postgres"]),
            lambda: f"{tag} open on absent {member}: {out}",
        )
        self.checks["open_absent_record (documented)"] += 1
        self.leg("redis")
        keys = ValidityField.get_all_keys(model, FIELD)
        for index in ("valid_from", "invalid_at", "ingested_at"):
            client.zrem(keys[index], member)
        if pointer_key:
            if previous is None:
                client.delete(pointer_key)
            else:
                client.set(pointer_key, previous)

    # -- comparisons ----------------------------------------------------------------

    def compare_state(self, tag: str, model: Any, names: list[str], records: dict):
        for name in names:
            key = self._key(model, name)
            redis_state = self.state("redis", model, key)
            pg_state = self.state("postgres", model, key)
            self.check(
                "interval",
                all(_same_float(a, b) for a, b in zip(redis_state[:3], pg_state[:3])),
                lambda: f"{tag} {key}: redis={redis_state} pg={pg_state}",
            )
            self.check(
                "links",
                redis_state[3:] == pg_state[3:],
                lambda: f"{tag} {key}: redis={redis_state} pg={pg_state}",
            )
            for which in ("chain", "superseded_by", "supersedes"):
                out = self.both(
                    lambda leg, which=which: _walk(records[leg].get(name), which)
                )
                self.check(
                    "chain",
                    out["redis"] == out["postgres"],
                    lambda: f"{tag} {which}({key}): {out}",
                )
        for digest in IDENTITIES:
            redis_ptr = self.pointer("redis", model, digest)
            pg_ptr = self.pointer("postgres", model, digest)
            self.check(
                "pointer",
                redis_ptr == pg_ptr,
                lambda: f"{tag} {digest}: redis={redis_ptr} pg={pg_ptr}",
            )

    def compare_reads(
        self, tag: str, model: Any, agent: str, names: list[str], as_of: float
    ):
        for cls, fn in (
            (
                "excluded",
                lambda leg: sorted(
                    ValidityField.resolve_excluded_keys(model, FIELD, as_of=as_of)
                ),
            ),
            (
                "valid",
                lambda leg: sorted(
                    ValidityField.resolve_valid_keys(model, FIELD, as_of=as_of)
                ),
            ),
            (
                "filter",
                lambda leg: sorted(
                    r.db_key.redis_key
                    for r in model.query.filter(validity__as_of=as_of)
                ),
            ),
            (
                "top_by_decay",
                lambda leg: [
                    r.db_key.redis_key
                    for r in _scoped(model, agent).top_by_decay(n=20, as_of=as_of)
                ],
            ),
            (
                "composite",
                lambda leg: [
                    r.db_key.redis_key
                    for r in _scoped(model, agent).composite_score(
                        _indexes(model), limit=20, as_of=as_of
                    )
                ],
            ),
        ):
            out = self.both(fn)
            if _nan_bound(out):
                # Documented divergence: a NaN bound is refused by both with
                # one text -- Redis's ResponseError, Postgres's
                # QueryException (M1's divergence (v)).
                self.checks["nan_bound (documented)"] += 1
                continue
            self.check(
                cls,
                _results_equal(out["redis"], out["postgres"]),
                lambda: f"{tag} {cls} as_of={as_of!r}: {out}",
            )
        for current in (True, False):
            out = self.both(
                lambda leg: sorted(
                    r.db_key.redis_key
                    for r in model.query.filter(validity__current=current)
                )
            )
            self.check(
                "filter",
                out["redis"] == out["postgres"],
                lambda: f"{tag} current={current}: {out}",
            )

    # -- report -------------------------------------------------------------------

    def report(self) -> str:
        lines = [
            f"shapes: {self.shapes}",
            "| class | checks | mismatches |",
            "|---|---:|---:|",
        ]
        for cls in sorted(self.checks):
            lines.append(f"| {cls} | {self.checks[cls]} | {self.mismatches[cls]} |")
        for cls, examples in sorted(self.examples.items()):
            lines.append(f"\n{cls} examples:")
            lines.extend(f"  - {e}" for e in examples)
        return "\n".join(lines)


def _nan_bound(out: dict) -> bool:
    r, p = out["redis"], out["postgres"]
    return (
        r[:2] == ("!!", "ResponseError")
        and p[:2] == ("!!", "QueryException")
        and r[2] == p[2] == "min or max is not a float"
    )


def _token(message: str) -> str:
    """The ``POPOTO_VALIDITY_*`` reply line inside an exception message,
    without redis-py's pipeline prefix."""
    start = message.find("POPOTO_VALIDITY_")
    return message[start:] if start >= 0 else message


def _resave_declared(record: Any, declared: float) -> None:
    record.validity = declared
    record.save()


def _walk(record: Any, which: str) -> Any:
    if record is None:
        return None
    if which == "chain":
        return [r.db_key.redis_key for r in SupersessionProtocol.chain(record)]
    found = getattr(SupersessionProtocol, which)(record)
    return None if found is None else found.db_key.redis_key


def _scoped(model: Any, agent: str) -> Any:
    if model is ProbeScoped:
        return model.query.filter(agent=agent)
    return model.query


def _indexes(model: Any) -> dict:
    if model is ProbeClaim:
        return {"relevance": 0.5, "certainty": 0.5}
    return {"relevance": 1.0}


def _results_equal(a: Any, b: Any) -> bool:
    """Outcomes equal: same exception type and text, or equal results (a
    float compared bit for bit, NaN equal to NaN)."""
    if a[0] != b[0]:
        return False
    if a[0] == "!!":
        return a[1:] == b[1:]
    x, y = a[1], b[1]
    if isinstance(x, float) or isinstance(y, float):
        return _same_float(x, y)
    return x == y


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
