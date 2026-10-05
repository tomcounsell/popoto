#!/usr/bin/env python
"""Seeded two-leg probe: Redis streams (the oracle) vs the Postgres events
tables, for ``EventStreamMixin``, the stream commands and ``StreamConsumer``
(#759 M5).

Each *shape* replays one random session on both legs against one or two
stream keys, through ``popoto.streams.stream_client`` -- the Redis client on
one leg, the Postgres backend's stream store on the other -- plus the model
layer (an ``EventStreamMixin`` model's saves, partial saves, deletes and a
rolled-back unit of work) and ``StreamConsumer`` batches with a handler that
fails at random (reclaim, retry and dead-letter). A shape is either *auto*
(server-clock ids) or *explicit* (``XADD`` with chosen ids, so the ids
themselves are compared byte for byte).

Ids differ between legs in auto shapes (two servers' clocks), so each leg's
ids are renamed ``e1, e2, ...`` in creation order before comparing -- the
order and identity of every id is compared, not its clock value. Idle times
are not compared (two clocks); claims use a ``min_idle_time`` of 0 or of
10**9 ms, so which entries qualify is deterministic. Every reply is compared
after that renaming, and every error by its message text.

====================  =========================================================
class                 what is compared
====================  =========================================================
``xadd``              the id (renamed) or the error text
``xrange``            ``XRANGE``/``XREVRANGE`` with random bounds (``-``,
                      ``+``, ids, exclusive ``(`` ids, bare ``<ms>``) and counts
``xlen``              the length
``xdel`` ``xtrim``    the number removed (``XTRIM`` exact, by length or MINID)
``group``             ``XGROUP CREATE`` (``0``, ``$``, an id; ``MKSTREAM``;
                      ``BUSYGROUP``), ``DESTROY``, ``DELCONSUMER``
``xreadgroup``        ``>`` (with ``COUNT``, ``NOACK``) and history reads
``xack``              the number acknowledged
``xpending``          the summary and the extended form (consumer filter)
``xclaim``            ``XCLAIM`` and ``XAUTOCLAIM`` (cursor, ``JUSTID``,
                      deleted entries)
``xinfo``             ``XINFO GROUPS``: name, consumers, pending, last id,
                      ``entries-read`` and ``lag``
``delete``            ``DEL`` of a stream key
``model``             the mutation entries a model's writes append
``consumer``          a ``StreamConsumer`` batch: its count or error, what the
                      handler saw, and the dead-letter stream
``state``             at the end: every stream's entries, groups and pending
====================  =========================================================

Documented class (counted, not a failure; the shape ends there because the
legs' lengths differ from then on by construction):

* ``approx_trim`` -- ``MAXLEN ~ N`` (and ``XTRIM ~``) on a stream longer than
  ``N``: Redis trims whole radix-tree nodes and so keeps at least ``N``
  (here, every entry: a probe stream never fills a node); Postgres keeps
  exactly ``N``. The probe checks Postgres kept ``min(len, N)`` and Redis
  at least as many.

Safety: Redis is bound from ``REDIS_URL`` *before* importing popoto and
database 0 is refused (CLAUDE.md, #577); on Postgres the script creates its
own ``popoto_test_<uuid4().hex>`` schema and drops only that.

Usage::

    REDIS_URL=redis://localhost:6379/13 \\
    POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \\
        python scripts/probe_events_parity.py --seeds 1 2 3 --shapes 120
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
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
from popoto.fields.event_stream import EventStreamMixin  # noqa: E402
from popoto.streams import StreamConsumer, stream_client  # noqa: E402


class ProbeEv(EventStreamMixin, popoto.Model):
    _stream_name = "probe_ev"
    _stream_metadata_fields = ("tag",)

    name = popoto.UniqueKeyField()
    tag = popoto.StringField(default="")
    n = popoto.IntField(default=0)


MODEL_KEY = "stream:probe_ev"
KEYS = (MODEL_KEY, "stream:probe_ev:aux")
GROUPS = ("ga", "gb")
CONSUMERS = ("c1", "c2", "c3")
WORDS = ["alpha", "beta", "gamma", "delta", "0", "-1", "3.5", ""]
ID_RE = re.compile(rb"^\d+-\d+$")
NEVER_IDLE = 10**9
LEGS = ("redis", "postgres")


class Probe:
    def __init__(self, pg: Any, *, verbose: bool = False) -> None:
        self.pg = pg
        self.verbose = verbose
        self.checks: Counter = Counter()
        self.mismatches: Counter = Counter()
        self.examples: dict[str, list[str]] = {}
        self.documented: Counter = Counter()
        self.shapes = 0
        self.ended = 0
        self.ops = 0
        self.labels: dict[str, dict[bytes, str]] = {}
        self.ids: dict[str, dict[str, bytes]] = {}
        self.count = 0
        self.explicit_ms = 0
        self.trace: Any = None
        self.ctx: tuple = (MODEL_KEY,)

    # -- legs ---------------------------------------------------------------------

    def leg(self, name: str) -> Any:
        set_backend("redis" if name == "redis" else self.pg)
        return stream_client(ProbeEv)

    def wipe(self) -> None:
        client = self.leg("redis")
        for pattern in ("*probe_ev*", "*ProbeEv*"):
            for key in client.scan_iter(match=pattern, count=1000):
                client.delete(key)
        self.leg("postgres")
        t = self.pg._events_ready()
        self.pg._run(
            "TRUNCATE "
            + ", ".join(t[n] for n in ("popoto_stream", "popoto_stream_attempt"))
            + " CASCADE",
            write=True,
        )
        self.pg._run(f"TRUNCATE {self.pg._table(ProbeEv._meta.spec).qualified}")
        self.labels = {leg: {} for leg in LEGS}
        self.ids = {leg: {} for leg in LEGS}
        self.count = 0
        self.explicit_ms = 1000

    # -- id renaming ----------------------------------------------------------------

    def learn(self, leg: str, raw: bytes, label: str, stream: str = MODEL_KEY) -> None:
        # Per stream: two streams can mint the same raw id in one millisecond.
        self.labels[leg].setdefault(stream, {})[raw] = label
        self.ids[leg][label] = raw

    def knows(self, leg: str, raw: bytes, stream: str) -> bool:
        return raw in self.labels[leg].get(stream, {})

    def new_label(self) -> str:
        self.count += 1
        return f"e{self.count}"

    def norm(self, leg: str, value: Any, ctx: Any = None) -> Any:
        """``value`` with each raw id renamed, looking the id up in the
        ``ctx`` streams first (default: the stream the op addressed)."""
        ctx = self.ctx if ctx is None else ctx
        if isinstance(value, (bytes, str)):
            raw = value.encode() if isinstance(value, str) else value
            if ID_RE.match(raw):
                maps = self.labels[leg]
                for stream in list(ctx) + list(maps):
                    if raw in maps.get(stream, {}):
                        return maps[stream][raw]
                return raw
            return value
        if isinstance(value, dict):
            return {
                self.norm(leg, k, ctx): self.norm(leg, v, ctx)
                for k, v in value.items()
                if k not in ("time_since_delivered", "idle", "inactive")
                and k not in (b"ts", b"dead_letter_ts", "ts", "dead_letter_ts")
            }
        if isinstance(value, (list, tuple)):
            return [self.norm(leg, v, ctx) for v in value]
        return value

    def raw_id(self, leg: str, label: Any) -> Any:
        if isinstance(label, str) and label in self.ids[leg]:
            return self.ids[leg][label]
        return label

    # -- comparison -----------------------------------------------------------------

    def miss(self, cls: str, detail: str) -> None:
        self.mismatches[cls] += 1
        bucket = self.examples.setdefault(cls, [])
        if len(bucket) < 5:
            bucket.append(detail)
        if self.verbose:
            print(f"MISMATCH {cls}: {detail}", file=sys.stderr)

    def both(self, fn: Callable[[str, Any], Any]) -> dict[str, Any]:
        out = {}
        for leg in LEGS:
            client = self.leg(leg)
            try:
                out[leg] = ("ok", fn(leg, client))
            except Exception as exc:  # compared, never raised
                out[leg] = ("err", str(exc))
        return out

    def compare(self, cls: str, out: dict[str, Any], tag: str) -> dict[str, Any]:
        self.ops += 1
        self.checks[cls] += 1
        normed = {
            leg: (kind, self.norm(leg, value)) for leg, (kind, value) in out.items()
        }
        if normed["redis"] != normed["postgres"]:
            self.miss(
                cls, f"{tag}: redis={normed['redis']!r} pg={normed['postgres']!r}"
            )
        elif self.trace is not None and self.trace in tag:
            print(f"  {cls} {tag}: {normed['redis']!r}", file=sys.stderr)
            try:
                info = self.leg("redis").xinfo_stream(MODEL_KEY)
                meta = self.pg.streams()._stream_meta(None, MODEL_KEY)
                print(
                    f"    redis added={info['entries-added']} "
                    f"maxdel={self.norm('redis', info['max-deleted-entry-id'])} "
                    f"| pg added={meta and meta['added']} maxdel={meta and meta['max_del']}",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(f"    ({exc})", file=sys.stderr)
        return normed

    # -- one shape ------------------------------------------------------------------

    def shape(self, rng: random.Random, seed: int, index: int) -> None:
        self.shapes += 1
        tag = f"seed={seed} shape={index}"
        self.wipe()
        explicit = rng.random() < 0.3
        for step in range(rng.randint(15, 45)):
            stop = self.op(rng, explicit, f"{tag} step={step}")
            if stop:
                self.ended += 1
                return
        self.state(tag)

    def fields(self, rng: random.Random) -> dict:
        out: dict = {}
        for _ in range(rng.randint(1, 4)):
            key = rng.choice(["a", "b", "c", "op", "x y"])
            out[key] = rng.choice(
                [rng.choice(WORDS), rng.randint(-5, 50), rng.random() * 10, b"\x00\xff"]
            )
        return out

    def own(self) -> list:
        """The labels minted on the stream the op addresses. An id from
        another stream is only a number, and two streams can mint the same
        one in a millisecond on one leg and not on the other."""
        return list(self.labels["redis"].get(self.ctx[0], {}).values())

    def known(self, rng: random.Random) -> Any:
        own = self.own()
        if own and rng.random() < 0.85:
            return rng.choice(own)
        return rng.choice(["1-0", "0-0", "999999999999999-0", "abc"])

    def bound(self, rng: random.Random, start: bool) -> Any:
        roll = rng.random()
        if roll < 0.3:
            return "-" if start else "+"
        own = self.own()
        if roll < 0.75 and own:
            return ("(" if rng.random() < 0.3 else "") + rng.choice(own)
        return rng.choice(["0", "1000", "1001-2", "(0-0", "99999999999999"])

    def resolve(self, leg: str, value: Any) -> Any:
        """A label, possibly behind ``(``, to the leg's raw id."""
        if (
            isinstance(value, str)
            and value.startswith("(")
            and value[1:] in self.ids[leg]
        ):
            return b"(" + self.ids[leg][value[1:]]
        return self.raw_id(leg, value)

    def op(self, rng: random.Random, explicit: bool, tag: str) -> bool:
        roll = rng.random()
        key = KEYS[0] if rng.random() < 0.75 else KEYS[1]
        group = rng.choice(GROUPS)
        consumer = rng.choice(CONSUMERS)
        self.ctx = (key,)
        if roll < 0.22:
            return self.xadd(rng, key, explicit, tag)
        if roll < 0.30:
            return self.model_write(rng, tag)
        if roll < 0.38:
            lo, hi = self.bound(rng, True), self.bound(rng, False)
            count = rng.choice([None, 1, 2, 5])
            rev = rng.random() < 0.4

            def rng_fn(leg: str, c: Any) -> Any:
                if rev:
                    return c.xrevrange(
                        key,
                        max=self.resolve(leg, hi),
                        min=self.resolve(leg, lo),
                        count=count,
                    )
                return c.xrange(
                    key,
                    min=self.resolve(leg, lo),
                    max=self.resolve(leg, hi),
                    count=count,
                )

            self.compare(
                "xrange",
                self.both(rng_fn),
                f"{tag} {key} {lo}..{hi} n={count} rev={rev}",
            )
        elif roll < 0.41:
            self.compare("xlen", self.both(lambda leg, c: c.xlen(key)), tag)
        elif roll < 0.45:
            picks = [self.known(rng) for _ in range(rng.randint(1, 3))]
            self.compare(
                "xdel",
                self.both(
                    lambda leg, c: c.xdel(key, *[self.raw_id(leg, p) for p in picks])
                ),
                f"{tag} {picks}",
            )
        elif roll < 0.48:
            if rng.random() < 0.5:
                n = rng.randint(0, 6)
                approx = rng.random() < 0.3
                if approx:
                    return self.approx_trim(key, n, tag)
                self.compare(
                    "xtrim",
                    self.both(lambda leg, c: c.xtrim(key, maxlen=n, approximate=False)),
                    f"{tag} maxlen={n}",
                )
            else:
                pick = self.known(rng)
                self.compare(
                    "xtrim",
                    self.both(
                        lambda leg, c: c.xtrim(
                            key, minid=self.raw_id(leg, pick), approximate=False
                        )
                    ),
                    f"{tag} minid={pick}",
                )
        elif roll < 0.55:
            start = rng.choice(["0", "$", "known"])
            sid = self.known(rng) if start == "known" else start
            mk = rng.random() < 0.6
            self.compare(
                "group",
                self.both(
                    lambda leg, c: c.xgroup_create(
                        key, group, id=self.raw_id(leg, sid), mkstream=mk
                    )
                ),
                f"{tag} create {key} {group} {sid} mk={mk}",
            )
        elif roll < 0.57:
            if rng.random() < 0.5:
                self.compare(
                    "group",
                    self.both(lambda leg, c: c.xgroup_destroy(key, group)),
                    f"{tag} destroy",
                )
            else:
                self.compare(
                    "group",
                    self.both(
                        lambda leg, c: c.xgroup_delconsumer(key, group, consumer)
                    ),
                    f"{tag} delconsumer {consumer}",
                )
        elif roll < 0.68:
            history = rng.random() < 0.25
            sid = rng.choice(["0", "known"]) if history else ">"
            if sid == "known":
                sid = self.known(rng)
            count = rng.choice([None, 1, 2, 3])
            noack = rng.random() < 0.1
            self.compare(
                "xreadgroup",
                self.both(
                    lambda leg, c: c.xreadgroup(
                        group,
                        consumer,
                        {key: self.raw_id(leg, sid)},
                        count=count,
                        noack=noack,
                    )
                ),
                f"{tag} {group}/{consumer} {sid} n={count} noack={noack}",
            )
        elif roll < 0.73:
            picks = [self.known(rng) for _ in range(rng.randint(1, 3))]
            self.compare(
                "xack",
                self.both(
                    lambda leg, c: c.xack(
                        key, group, *[self.raw_id(leg, p) for p in picks]
                    )
                ),
                f"{tag} {picks}",
            )
        elif roll < 0.79:
            if rng.random() < 0.5:
                self.compare(
                    "xpending", self.both(lambda leg, c: c.xpending(key, group)), tag
                )
            else:
                who = rng.choice([None, consumer])
                n = rng.randint(1, 6)
                self.compare(
                    "xpending",
                    self.both(
                        lambda leg, c: c.xpending_range(
                            key, group, "-", "+", n, consumername=who
                        )
                    ),
                    f"{tag} range n={n} who={who}",
                )
        elif roll < 0.86:
            idle = rng.choice([0, 0, NEVER_IDLE])
            if rng.random() < 0.5:
                picks = [self.known(rng) for _ in range(rng.randint(1, 3))]
                justid = rng.random() < 0.3
                self.compare(
                    "xclaim",
                    self.both(
                        lambda leg, c: c.xclaim(
                            key,
                            group,
                            consumer,
                            idle,
                            [self.raw_id(leg, p) for p in picks],
                            justid=justid,
                        )
                    ),
                    f"{tag} xclaim {picks} idle={idle} justid={justid}",
                )
            else:
                start = rng.choice(["0-0", "known"])
                if start == "known":
                    start = self.known(rng)
                count = rng.choice([None, 1, 2, 3])
                justid = rng.random() < 0.3
                self.compare(
                    "xclaim",
                    self.both(
                        lambda leg, c: c.xautoclaim(
                            key,
                            group,
                            consumer,
                            idle,
                            start_id=self.raw_id(leg, start),
                            count=count,
                            justid=justid,
                        )
                    ),
                    f"{tag} xautoclaim start={start} n={count} idle={idle} justid={justid}",
                )
        elif roll < 0.91:
            self.compare("xinfo", self.both(lambda leg, c: c.xinfo_groups(key)), tag)
        elif roll < 0.93:
            self.compare("delete", self.both(lambda leg, c: c.delete(key)), tag)
        else:
            self.consume(rng, group, tag)
        return False

    def xadd(self, rng: random.Random, key: str, explicit: bool, tag: str) -> bool:
        flds = self.fields(rng)
        maxlen = None
        if rng.random() < 0.15:
            maxlen = rng.randint(0, 5)
            if rng.random() < 0.4:
                return self.approx_trim(key, maxlen, tag, fields=flds)
        nomk = rng.random() < 0.05
        sid: Any = "*"
        if explicit:
            choice = rng.random()
            if choice < 0.6:
                self.explicit_ms += rng.choice([0, 1, 1, 7])
                sid = f"{self.explicit_ms}-{rng.randint(0, 3)}"
            elif choice < 0.85:
                sid = f"{self.explicit_ms}-*"
            elif choice < 0.95:
                sid = str(self.explicit_ms + 1)
            else:
                sid = rng.choice(["0-0", "1-1", "garbage"])
        out = self.both(
            lambda leg, c: c.xadd(
                key, flds, id=sid, maxlen=maxlen, approximate=False, nomkstream=nomk
            )
        )
        label = None
        if out["redis"][0] == "ok" and out["postgres"][0] == "ok":
            r, p = out["redis"][1], out["postgres"][1]
            if r is not None and p is not None:
                label = self.new_label()
                self.learn("redis", r, label, key)
                self.learn("postgres", p, label, key)
        self.compare("xadd", out, f"{tag} {key} id={sid} maxlen={maxlen} nomk={nomk}")
        return False

    def approx_trim(self, key: str, maxlen: int, tag: str, fields: Any = None) -> bool:
        """``MAXLEN ~`` / ``XTRIM ~``: the documented class. Postgres must
        keep exactly ``min(n, maxlen)`` of the ``n`` entries; Redis anything
        from that up to ``n`` (whole radix nodes). Equal lengths carry on."""
        lens, replies, before = {}, {}, {}
        for leg in LEGS:
            c = self.leg(leg)
            before[leg] = c.xlen(key)
            try:
                if fields is not None:
                    replies[leg] = c.xadd(key, fields, maxlen=maxlen, approximate=True)
                else:
                    c.xtrim(key, maxlen=maxlen, approximate=True)
            except Exception as exc:
                replies[leg] = f"!{exc}"
            lens[leg] = c.xlen(key)
        n = before["redis"] + (1 if fields is not None else 0)
        self.checks["approx_trim"] += 1
        if (
            before["redis"] != before["postgres"]
            or lens["postgres"] != min(n, maxlen)
            or not lens["postgres"] <= lens["redis"] <= n
        ):
            self.miss("approx_trim", f"{tag} {key} maxlen~{maxlen} {before} -> {lens}")
            return True
        if lens["redis"] != lens["postgres"]:
            self.documented["approx_trim"] += 1
            return True
        if fields is not None and all(isinstance(replies.get(g), bytes) for g in LEGS):
            label = self.new_label()
            for leg in LEGS:
                self.learn(leg, replies[leg], label, key)
        return False

    def model_write(self, rng: random.Random, tag: str) -> bool:
        name = rng.choice(["m1", "m2", "m3"])
        kind = rng.choice(["save", "save", "partial", "delete", "rollback"])
        before = {leg: len(self.leg(leg).xrange(MODEL_KEY)) for leg in LEGS}

        def write(leg: str, c: Any) -> Any:
            if kind == "save":
                # The reply is M1's HSET-shaped count (new fields on Redis, a
                # fresh row on Postgres): compared as "saved", not as a count.
                return ProbeEv(name=name, tag=rng_tag, n=n).save() is not False
            if kind == "partial":
                # A partial save of a stored record (a fresh instance's
                # partial save is M1's territory, not the stream's).
                obj = ProbeEv.query.get(name=name)
                if obj is None:
                    return "absent"
                obj.n = n
                return bool(obj.save(update_fields=["n"]) is not False)
            if kind == "delete":
                return ProbeEv(name=name).delete()
            # A write that never commits: a Redis pipeline discarded unexecuted,
            # a Postgres transaction rolled back. Neither leaves an entry.
            if leg == "redis":
                pipe = popoto.get_redis().pipeline()
                ProbeEv(name=name, tag=rng_tag).save(pipeline=pipe)
                pipe.reset()
                return "rolled back"
            from popoto.backends import get_backend

            try:
                with get_backend(ProbeEv).transaction() as uow:
                    ProbeEv(name=name, tag=rng_tag).save(pipeline=uow)
                    raise RuntimeError("roll back")
            except RuntimeError:
                return "rolled back"

        rng_tag = rng.choice(WORDS)
        n = rng.randint(0, 9)
        out = self.both(write)
        self.compare("model", out, f"{tag} {kind} {name}")
        tails = {}
        self.ctx = (MODEL_KEY,)
        for leg in LEGS:
            entries = self.leg(leg).xrange(MODEL_KEY)
            fresh = entries[before[leg] :] if len(entries) >= before[leg] else entries
            tails[leg] = [{k: v for k, v in f.items() if k != b"ts"} for _, f in fresh]
            if leg == "redis":
                labels = []
                for raw, _ in fresh:
                    label = self.new_label()
                    labels.append(label)
                    self.learn(leg, raw, label)
            else:
                for raw, label in zip([r for r, _ in fresh], labels):
                    self.learn(leg, raw, label)
        self.checks["model"] += 1
        if tails["redis"] != tails["postgres"]:
            self.miss("model", f"{tag} {kind} {name}: entries {tails}")
        return False

    def consume(self, rng: random.Random, group: str, tag: str) -> None:
        fail = rng.random() < 0.4
        max_retries = rng.randint(1, 3)
        batch = rng.randint(1, 4)
        out = {}
        for leg in LEGS:
            self.leg(leg)
            seen: list = []

            async def handler(entries: Any, seen: list = seen) -> None:
                seen.extend(eid for eid, _ in entries)
                if fail:
                    raise RuntimeError("handler failed")

            consumer = StreamConsumer(
                MODEL_KEY,
                group,
                "worker",
                handler,
                batch_size=batch,
                block_ms=1,
                claim_timeout_ms=0,
                max_retries=max_retries,
                model=ProbeEv,
            )
            try:
                result: Any = ("ok", consumer.process_batch_sync())
            except Exception as exc:
                result = ("err", str(exc))
            consumer.close()
            dead_key = "dead:" + MODEL_KEY
            dead = stream_client(ProbeEv).xrange(dead_key)
            for raw, _ in dead:
                if not self.knows(leg, raw, dead_key):
                    if leg == "redis":
                        label = self.new_label()
                        self.dead_labels = getattr(self, "dead_labels", [])
                        self.dead_labels.append(label)
                    else:
                        label = self.dead_labels.pop(0)
                    self.learn(leg, raw, label, dead_key)
            # The dead letter's own id is the dead stream's; its
            # original_id names an entry of the source stream.
            dead = [
                (self.norm(leg, raw, (dead_key,)), self.norm(leg, f, (MODEL_KEY,)))
                for raw, f in dead
            ]
            seen_ids = [self.norm(leg, x.encode(), (MODEL_KEY,)) for x in seen]
            out[leg] = ("ok", (result, seen_ids, dead))
        self.dead_labels = []
        self.compare(
            "consumer", out, f"{tag} group={group} fail={fail} retries={max_retries}"
        )

    def state(self, tag: str) -> None:
        def snapshot(leg: str, c: Any) -> Any:
            out = {}
            for key in KEYS + ("dead:" + MODEL_KEY,):
                groups = []
                try:
                    for g in c.xinfo_groups(key):
                        name = g["name"]
                        groups.append((g, c.xpending_range(key, name, "-", "+", 1000)))
                except Exception as exc:
                    groups = [str(exc)]
                out[key] = self.norm(
                    leg, (c.xrange(key), c.xlen(key), groups), (key, MODEL_KEY)
                )
            return out

        self.compare("state", self.both(snapshot), f"{tag} end")

    def report(self) -> str:
        lines = [
            f"shapes {self.shapes} (ended early on a documented class: {self.ended}), "
            f"ops compared {self.ops}",
        ]
        for cls in sorted(self.checks):
            lines.append(
                f"  {cls:12s} checks {self.checks[cls]:6d}  mismatches "
                f"{self.mismatches.get(cls, 0)}"
            )
        for cls, n in sorted(self.documented.items()):
            lines.append(f"  documented {cls}: {n}")
        for cls, examples in self.examples.items():
            lines.append(f"  examples {cls}:")
            lines.extend(f"    {e}" for e in examples)
        return "\n".join(lines)


def run(
    pg: Any, seeds: list[int], shapes: int, *, verbose: bool = False, trace: Any = None
) -> Probe:
    probe = Probe(pg, verbose=verbose)
    probe.trace = trace
    for seed in seeds:
        rng = random.Random(seed)
        for index in range(shapes):
            probe.shape(rng, seed, index)
    set_backend(None)
    return probe


def main() -> None:
    import psycopg

    from popoto.backends.postgres import PostgresBackend

    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--shapes", type=int, default=120)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument(
        "--trace",
        default=None,
        help='print every op of tags containing this, e.g. "shape=26 "',
    )
    args = ap.parse_args()
    url = os.environ["POPOTO_POSTGRES_URL"]
    schema = f"popoto_test_{uuid.uuid4().hex}"
    admin = psycopg.connect(url, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    pg = PostgresBackend(dsn=url, schema=schema)
    started = time.perf_counter()
    try:
        probe = run(pg, args.seeds, args.shapes, verbose=args.verbose, trace=args.trace)
    finally:
        pg.streams().close()
        pg.close()
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
    print(f"seeds {args.seeds}, {time.perf_counter() - started:.1f}s")
    print(probe.report())
    sys.exit(1 if sum(probe.mismatches.values()) else 0)


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    main()
