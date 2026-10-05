#!/usr/bin/env python
"""Seeded two-leg probe: Redis (the Lua oracle) vs Postgres, the question
queue (#759 M4).

Each *shape* replays one random session on both legs: one or two agents,
facts with a ``ConfidenceField``, and a sequence of turns (mostly
increasing, sometimes regressed or repeated) on which the host proposes
(three kinds, a small vocabulary so text and key-set dedup both fire, the
occasional invalid input), notes use, asks for the next question (with and
without cues), answers it (an option label, its 1-based index, a deflection,
garbage, or a reply to a question that was never delivered), and runs the
expiry and prune passes. Candidate ids are drawn from a per-leg counter, so
both legs name the same candidate the same way. Compared:

====================  ======================================================
class                 what is compared
====================  ======================================================
``propose``           the candidate id returned (or that both raise)
``next_question``     the delivered candidate id and its ask count
``record_answer``     ``(applied, reason, classification, option_index)``
``note_use``          the number of candidates touched
``expire_stale``      the number expired
``prune``             the number deleted
``state``             every candidate's stored status, ask count, delivered,
                      cooldown, answer, resolved, last-seen and expiry turns
``confidence``        every fact's confidence state after the answers
``bucket``            the agent's token-bucket turn
====================  ======================================================

Documented class (counted, not a failure; the shape ends there because the
legs' states differ from then on by construction):

* ``dedup_order`` -- a proposal that duplicates two or more open candidates
  folds into the first one ``QuestionCandidate.query.filter(agent_id=…)``
  returns: Redis set order on Redis, ``_pk`` order on Postgres (the M1 row
  "order of results with no ``order_by``").

There is no concurrency in a shape, so the queue's other documented
divergence (a candidate a concurrent writer holds is skipped on Postgres,
``FOR UPDATE SKIP LOCKED``) cannot occur;
``tests/postgres/test_postgres_question_queue.py`` pins it. A mismatch is printed with its seed, shape and op; the run exits
non-zero if there is any.

Safety: Redis is bound from ``REDIS_URL`` *before* importing popoto and
database 0 is refused (CLAUDE.md, #577); on Postgres the script creates its
own ``popoto_test_<uuid4().hex>`` schema and drops only that.

Usage::

    REDIS_URL=redis://localhost:6379/13 \\
    POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \\
        python scripts/probe_queue_parity.py --seeds 1 2 3 --shapes 200
"""

from __future__ import annotations

import argparse
import itertools
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
from popoto import ConfidenceField  # noqa: E402
from popoto.backends import set_backend  # noqa: E402
from popoto.recipes import question_queue as qq  # noqa: E402
from popoto.recipes.question_queue import QuestionCandidate  # noqa: E402


class ProbeQFact(popoto.Model):
    name = popoto.UniqueKeyField()
    certainty = ConfidenceField()


MODELS = (ProbeQFact, QuestionCandidate)
WORDS = ["deploy", "staging", "morning", "meeting", "dana", "budget", "rollout"]
STATE = (
    "status",
    "ask_count",
    "delivered_turn",
    "cooldown_until",
    "answer_option",
    "resolved_turn",
    "last_seen_turn",
    "expires_turn",
    "created_turn",
)


class Probe:
    """Runs shapes against one Redis and one Postgres backend."""

    def __init__(self, pg: Any, *, verbose: bool = False) -> None:
        self.pg = pg
        self.verbose = verbose
        self.checks: Counter = Counter()
        self.mismatches: Counter = Counter()
        self.examples: dict[str, list[str]] = {}
        self.shapes = 0
        self.deliveries = 0
        self.ended = 0
        self.documented: Counter = Counter()
        self.known: set = set()
        self.ids: dict[str, Any] = {}

    def leg(self, name: str) -> None:
        set_backend("redis" if name == "redis" else self.pg)

    def wipe(self) -> None:
        self.leg("redis")
        client = popoto.get_redis()
        for pattern in ("*ProbeQFact*", "*QuestionCandidate*", "$Question*"):
            for key in client.scan_iter(match=pattern, count=1000):
                client.delete(key)
        self.leg("postgres")
        for model in MODELS:
            self.pg._run(f"TRUNCATE {self.pg._table(model._meta.spec).qualified}")
        for table in ("popoto_question_bucket", "popoto_lease"):
            self.pg._run(f"TRUNCATE {self.pg._engine(table)}")
        self.ids = {
            "redis": itertools.count(1),
            "postgres": itertools.count(1),
        }

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
            counter = self.ids[leg]
            with mock.patch.object(
                qq.uuid,
                "uuid4",
                lambda c=counter: uuid.UUID(int=next(c)),
            ):
                try:
                    out[leg] = ("ok", fn(leg))
                except Exception as exc:  # compared, never raised
                    out[leg] = ("err", type(exc).__name__)
        return out

    # -- one shape --------------------------------------------------------------

    def shape(self, rng: random.Random, seed: int, index: int) -> None:
        self.shapes += 1
        tag = f"seed={seed} shape={index}"
        self.wipe()
        agents = ["a1", "a2"][: rng.randint(1, 2)]
        facts = [f"f{i}" for i in range(rng.randint(2, 6))]
        keys = [ProbeQFact(name=f).db_key.redis_key for f in facts]
        for leg in ("redis", "postgres"):
            self.leg(leg)
            for f in facts:
                ProbeQFact.create(name=f)
        delivered: dict[str, list[Any]] = {"redis": [], "postgres": []}
        self.known: set = set()
        turn = 0
        for step in range(rng.randint(10, 60)):
            roll = rng.random()
            if roll < 0.8:
                turn += rng.choice([0, 1, 1, 1, 2, 5, qq.QUESTION_BUDGET_TURNS])
            elif roll < 0.9:
                turn = max(0, turn - rng.randint(1, 4))
            agent = rng.choice(agents)
            stop = self.op(
                rng, agent, keys, turn, delivered, f"{tag} step={step} turn={turn}"
            )
            if stop:
                # A classified divergence: the legs' states differ from here
                # on by construction, so the shape ends (not a mismatch).
                self.ended += 1
                return
        self.state(keys, agents, tag)

    def op(self, rng, agent, keys, turn, delivered, tag) -> Any:
        roll = rng.random()
        if roll < 0.3:
            kind = rng.choice(["confirmation", "disjunction", "referent", "nope"])
            targets = rng.sample(keys, min(len(keys), rng.randint(1, 2)))
            words = rng.sample(WORDS, 3)
            text = rng.choice(
                [f"Is {words[0]} still {words[1]}?", f"Which {words[2]} did you mean?"]
            )
            labels = [t.split(":")[-1] for t in targets] + ["other"]
            options = [
                {
                    "label": label,
                    "acted": [t],
                    "contradicted": [u for u in targets if u != t],
                }
                for label, t in zip(labels, targets)
            ]
            if rng.random() < 0.05:
                options = [{"label": "", "acted": []}]
            signal = rng.choice(["disjunction", "gate_refusal", "evidence_gap"])

            def propose(leg: str) -> Any:
                c = qq.propose(
                    agent_id=agent,
                    question_text=text,
                    kind=kind,
                    source_module="probe",
                    target_keys=targets,
                    ambiguity_signal=signal,
                    turn=turn,
                    options=options,
                )
                return None if c is None else c.candidate_id

            got = self.both(propose)
            r, p = got["redis"], got["postgres"]
            if r != p and r[0] == p[0] == "ok" and {r[1], p[1]} <= self.known:
                # Two open candidates were both duplicates of this proposal,
                # and each leg returned (and touched) the first in its filter
                # order: Redis set order vs Postgres _pk order -- the M1 row
                # "order of results with no order_by", seen through dedup.
                self.documented["dedup_order"] += 1
                return True
            self.check("propose", r == p, lambda: f"{tag} {got}")
            if r[0] == "ok" and r[1] is not None:
                self.known.add(r[1])
        elif roll < 0.45:
            used = rng.sample(keys, rng.randint(1, len(keys)))
            got = self.both(lambda leg: qq.note_use(agent, used, turn))
            self.check(
                "note_use", got["redis"] == got["postgres"], lambda: f"{tag} {got}"
            )
        elif roll < 0.7:
            cues = rng.choice([None, " ".join(rng.sample(WORDS, 2)), "zzz"])

            def ask(leg: str) -> Any:
                q = qq.next_question(agent, turn=turn, query_cues=cues)
                if q is None:
                    return None
                delivered[leg].append(q)
                return (q.candidate_id, q.ask_count, q.delivered_turn)

            got = self.both(ask)
            if got["redis"] == ("ok", None) or got["redis"][0] == "err":
                pass
            else:
                self.deliveries += 1
            self.check(
                "next_question", got["redis"] == got["postgres"], lambda: f"{tag} {got}"
            )
        elif roll < 0.85:
            if not delivered["redis"] or len(delivered["redis"]) != len(
                delivered["postgres"]
            ):
                return
            i = rng.randrange(len(delivered["redis"]))
            options = delivered["redis"][i].options or []
            answer = rng.choice(
                [o.get("label", "") for o in options]
                + [str(rng.randint(1, max(1, len(options))))]
                + ["not sure", "skip", "banana", ""]
            )

            def answer_it(leg: str) -> Any:
                res = qq.record_answer(delivered[leg][i], answer, turn=turn)
                return (res.applied, res.reason, res.classification, res.option_index)

            got = self.both(answer_it)
            self.check(
                "record_answer", got["redis"] == got["postgres"], lambda: f"{tag} {got}"
            )
        elif roll < 0.95:
            got = self.both(lambda leg: qq.expire_stale(agent, turn))
            self.check(
                "expire_stale", got["redis"] == got["postgres"], lambda: f"{tag} {got}"
            )
        else:
            got = self.both(lambda leg: qq.prune(agent, turn))
            self.check("prune", got["redis"] == got["postgres"], lambda: f"{tag} {got}")

    def state(self, keys, agents, tag) -> None:
        def candidates(leg: str) -> Any:
            out = {}
            for agent in agents:
                for c in qq._candidates(agent):
                    out[c.candidate_id] = tuple(getattr(c, f) for f in STATE)
            return out

        got = self.both(candidates)
        self.check("state", got["redis"] == got["postgres"], lambda: f"{tag} {got}")

        def confidence(leg: str) -> Any:
            return [
                ConfidenceField.get_confidence_data(
                    ProbeQFact.query.get(redis_key=k), "certainty"
                )
                for k in keys
            ]

        got = self.both(confidence)
        self.check(
            "confidence", got["redis"] == got["postgres"], lambda: f"{tag} {got}"
        )

        def bucket(leg: str) -> Any:
            out = {}
            for agent in agents:
                if leg == "redis":
                    raw = popoto.get_redis().get(qq._bucket_key(agent))
                    out[agent] = None if raw is None else int(raw)
                else:
                    table = self.pg._engine("popoto_question_bucket")
                    rows, _ = self.pg._run(
                        f"SELECT last_turn FROM {table} WHERE agent = %s", [agent]
                    )
                    out[agent] = int(rows[0][0]) if rows else None
            return out

        got = self.both(bucket)
        self.check("bucket", got["redis"] == got["postgres"], lambda: f"{tag} {got}")

    def report(self) -> str:
        lines = [
            f"shapes: {self.shapes}; deliveries compared: {self.deliveries}; "
            f"shapes ended at a classified divergence: {self.ended}",
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
