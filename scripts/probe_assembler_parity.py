#!/usr/bin/env python
"""Seeded two-leg probe: ``ContextAssembler`` on Redis vs Postgres (#759 M2c).

Each *shape* builds the same random memory corpus on a Redis-bound and a
Postgres-bound copy of one model -- agents (the decay field's partition), a
plain ``tier`` field, tags, contents drawn from a small vocabulary (shared
terms, repeated terms, stopwords, empty texts), base scores, save times spread
over weeks, confidence signals, staged reads, re-saves that change the text
(so Redis's running BM25 ``avgdl`` drifts as it does in production) and
deletes -- then runs the same ``assemble()`` calls on both legs and compares:

=================  ===========================================================
class              what is compared
=================  ===========================================================
``keys``           the ranked keys of ``result.records`` (and ``proactive``)
``tokens``         ``metadata["token_count"]`` and ``result.formatted``
``metadata``       ``pull_count``, ``push_count``, ``total_candidates``, the
                   confidence-gate dict, the reader-gate counts
``trace``          ``metadata["trace"]``: keys, ranks, sources; scores 1e-9
``quality``        ``RetrievalQuality``: every scalar and per-cue component
                   1e-9, the score distribution element-wise
``effects``        after the call (the post-effects): every record's
                   confidence state and live staged reads
``outcomes``       the same after ``ObservationProtocol.on_context_used``
``assess``         ``assess()``'s ``RetrievalQuality``
=================  ===========================================================

Each call draws its mode (``auto``/``hybrid``/``lexical``/``composite``, as
the model allows), cues, scope (none, the partition, the partition plus a
plain field, a plain field alone), tags, ``exclude_keys``, ``max_items``,
``max_tokens`` (none, or a budget with the default estimator or a
length-based counter), output format, gate, ``assess_quality`` and
``emit_trace``. ``time.time`` is frozen per call so both legs score at one
instant. Postgres's vector arm runs exact, or on HNSW (by pinning
``Defaults.PG_VECTOR_EXACT_MAX`` to 0) in some shapes.

A disagreement is classified; the report lists every class with examples,
and the run exits non-zero on any class not explained in
``EXPLAINED`` (each one is a documented divergence or a tie the two legs
break by an order neither defines).

Safety (CLAUDE.md, #577): Redis is bound from ``REDIS_URL`` before popoto is
imported, database 0 is refused, and only this probe's keys are deleted.
Postgres: the script creates its own ``popoto_test_<hex>`` schema in
``POPOTO_POSTGRES_URL`` (whose database must already have
``CREATE EXTENSION vector``) and drops only that.

Usage::

    REDIS_URL=redis://localhost:6379/13 \\
    POPOTO_POSTGRES_URL=postgresql://localhost:5432/popoto_m2c \\
        python scripts/probe_assembler_parity.py --seeds 1 2 3 --shapes 100
"""

from __future__ import annotations

import argparse
import math
import os
import random
import re
import sys
import tempfile
import time
import uuid
import zlib
from collections import Counter
from typing import Any, Callable
from unittest import mock

if __name__ == "__main__":
    _url = os.environ.get("REDIS_URL", "")
    if not _url or _url.rstrip("/").endswith("/0") or _url.count("/") < 3:
        sys.exit("set REDIS_URL=redis://localhost:6379/<n> (n != 0) before running")
    if not os.environ.get("POPOTO_POSTGRES_URL"):
        sys.exit("set POPOTO_POSTGRES_URL to a database with CREATE EXTENSION vector")
    os.environ.setdefault("POPOTO_EMBEDDING_INVALIDATION", "none")
    os.environ["POPOTO_CONTENT_PATH"] = tempfile.mkdtemp(prefix="popoto-asm-probe-")

import numpy as np  # noqa: E402

import popoto  # noqa: E402
from popoto import (  # noqa: E402
    AccessTrackerMixin,
    ConfidenceField,
    DecayingSortedField,
    ObservationProtocol,
)
from popoto.backends import set_backend  # noqa: E402
from popoto.embeddings import AbstractEmbeddingProvider  # noqa: E402
from popoto.fields.bm25_field import BM25Field  # noqa: E402
from popoto.fields.constants import Defaults  # noqa: E402
from popoto.fields.embedding_field import EmbeddingField, invalidate_cache  # noqa: E402
from popoto.fields.existence_filter import ExistenceFilter  # noqa: E402
from popoto.recipes.context_assembler import ContextAssembler  # noqa: E402

DAY = 86400.0
TOL = 1e-9
STOP = ["the", "and", "for", "with"]
AGENTS = ["a1", "a2", "a3"]
TIERS = ["hot", "cold"]
TAGS = ["t1", "t2", "t3"]
OUTCOMES = ["acted", "used", "dismissed", "deferred", "contradicted"]
RELEVANCE_VALUE = re.compile(r"(relevance\W+)[-+0-9.e]+")

#: Classes that are not regressions, each a row of
#: docs/features/postgres-backend.md. A shape whose state one of them changed
#: ends there (a later call would only compare the consequence).
EXPLAINED: dict[str, str] = {
    "tokens:reload-after-touch": (
        "M2a divergence: after an `acted` outcome touched a record, a reload "
        "returns the save-time decay value on Redis (touch moves only the "
        "sorted-set score) and the touched clock on Postgres, so the "
        "serialized `relevance` differs; ranking does not"
    ),
    "keys:budget-after-touch": (
        "the same divergence reaching a token budget: the serialized "
        "`relevance` differs by a digit, the estimate by one token, and the "
        "greedy packing admits a different record; without the budget the "
        "keys agree (checked by re-running the call unbudgeted)"
    ),
    "keys:bm25-near-tie": (
        "M2b class: Redis's running avgdl drifts after re-saves and deletes "
        "(Postgres counts it live), so two BM25 scores within 1e-6 can order "
        "the other way in the arm, and RRF inherits the order"
    ),
    "keys:vector-exact-tie-order": (
        "M2b divergence: two records with the same text have the same "
        "vector, so the same similarity; Redis returns them in "
        "directory-listing order of their .npy files, Postgres by key, "
        "bytewise, and RRF inherits the order"
    ),
    "keys:vector-float32-near-tie": (
        "M2b divergence: numpy and pgvector accumulate float32 dot products "
        "in a different order, so similarities within 1e-6 can order the "
        "other way in the vector arm"
    ),
}


def _seed(shape_seed: int, text: str) -> int:
    """A process-independent seed (``hash()`` of a str is salted per run)."""
    return zlib.crc32(f"{shape_seed}:{text}".encode()) & 0x7FFFFFFF


class SeededProvider(AbstractEmbeddingProvider):
    """A vector per distinct text from a per-shape seed; texts sharing a
    first word share a direction, so near neighbours exist."""

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim
        self.seed = 0

    def embed(self, texts, input_type=None):
        out = []
        for text in texts:
            words = text.split() or [""]
            base = np.random.RandomState(_seed(self.seed, words[0])).randn(self._dim)
            noise = np.random.RandomState(_seed(self.seed, text)).randn(self._dim)
            out.append((base + 0.6 * noise).tolist())
        return out

    @property
    def dimensions(self):
        return self._dim

    @property
    def max_batch_size(self):
        return 64


PROVIDER = SeededProvider()


def _fingerprint(inst: Any) -> Any:
    return inst.content


class AsmHybrid(AccessTrackerMixin, popoto.Model):
    """Valor's memory shape: BM25 + embedding, so ``auto`` is hybrid."""

    name = popoto.UniqueKeyField()
    agent = popoto.KeyField()
    tier = popoto.Field(type=str, default="hot")
    tags = popoto.TagField()
    content = popoto.StringField(default="")
    importance = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(
        partition_by="agent", base_score_field="importance", decay_rate=0.5
    )
    certainty = ConfidenceField()
    lexical = BM25Field(source="content")
    embedding = EmbeddingField(source="content", provider=PROVIDER)
    bloom = ExistenceFilter(error_rate=0.01, capacity=5000, fingerprint_fn=_fingerprint)


class AsmLexical(AccessTrackerMixin, popoto.Model):
    """BM25 only, so ``auto`` is lexical (the retrieval-quality fixture's)."""

    name = popoto.UniqueKeyField()
    agent = popoto.KeyField()
    tier = popoto.Field(type=str, default="hot")
    tags = popoto.TagField()
    content = popoto.StringField(default="")
    importance = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(
        partition_by="agent", base_score_field="importance", decay_rate=0.5
    )
    certainty = ConfidenceField()
    lexical = BM25Field(source="content")


class AsmComposite(popoto.Model):
    """No BM25: ``auto`` is the query-blind composite path."""

    name = popoto.UniqueKeyField()
    agent = popoto.KeyField()
    tier = popoto.Field(type=str, default="hot")
    tags = popoto.TagField()
    content = popoto.StringField(default="")
    importance = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(
        partition_by="agent", base_score_field="importance", decay_rate=0.5
    )
    certainty = ConfidenceField()
    bloom = ExistenceFilter(error_rate=0.01, capacity=5000, fingerprint_fn=_fingerprint)


MODELS = [AsmHybrid, AsmLexical, AsmComposite]
MODES = {
    AsmHybrid: ["auto", "hybrid", "lexical", "composite"],
    AsmLexical: ["auto", "lexical", "composite"],
    AsmComposite: ["auto", "composite"],
}


def _close(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            return a is b
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return a == b or math.isclose(a, b, rel_tol=TOL, abs_tol=TOL)
    return a == b


class Probe:
    def __init__(self, pg: Any, admin: Any = None, *, verbose: bool = False) -> None:
        self.pg = pg
        self.admin = admin
        self.verbose = verbose
        self.checks: Counter = Counter()
        self.mismatches: Counter = Counter()
        self.examples: dict[str, list[str]] = {}
        self.shapes = 0
        self.calls = 0
        #: Calls per effective mode, and how many returned records -- so a
        #: run that compared only empty results is visible in the report.
        self.modes: Counter = Counter()
        self.nonempty: Counter = Counter()
        #: Shapes ended early because a classified difference changed state.
        self.diverged_shapes = 0
        #: The evidence behind the last key-difference classification.
        self.why = ""
        #: Calls skipped because a cue's ExistenceFilter answer differs.
        self.skipped: Counter = Counter()

    # -- plumbing -------------------------------------------------------------

    def leg(self, name: str) -> None:
        set_backend("redis" if name == "redis" else self.pg)

    def wipe(self) -> None:
        invalidate_cache()
        self.leg("redis")
        client = popoto.get_redis()
        for model in MODELS:
            for key in client.scan_iter(match=f"*{model.__name__}*", count=1000):
                client.delete(key)
        base = os.environ.get("POPOTO_CONTENT_PATH")
        if base:
            for root, _dirs, files in os.walk(base, topdown=False):
                for f in files:
                    os.unlink(os.path.join(root, f))
        schema = self.pg.schema
        if self.admin is not None:
            self.admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            self.admin.execute(f'CREATE SCHEMA "{schema}"')
        else:
            for model in MODELS:
                try:
                    table = self.pg._table(model._meta.spec)
                except Exception:  # noqa: BLE001 - never bound yet
                    continue
                self.pg._run(f"TRUNCATE {table.qualified} CASCADE", write=True)
        self.pg.forget_tables()
        self.leg("postgres")

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

    def both(self, fn: Callable[[], Any]) -> dict[str, Any]:
        out = {}
        for leg in ("redis", "postgres"):
            self.leg(leg)
            try:
                out[leg] = fn()
            except Exception as exc:  # noqa: BLE001 - compared below
                out[leg] = f"!! {type(exc).__name__}: {exc}"
        return out

    # -- one shape --------------------------------------------------------------

    def shape(self, rng: random.Random, seed: int, index: int) -> None:
        self.shapes += 1
        tag = f"seed={seed} shape={index}"
        model = rng.choice(MODELS)
        PROVIDER.seed = rng.randrange(1 << 30)
        exact_max = Defaults.PG_VECTOR_EXACT_MAX
        Defaults.PG_VECTOR_EXACT_MAX = 0 if rng.random() < 0.25 else exact_max
        clock = [1_790_000_000.0 + rng.uniform(0, 1e6)]
        try:
            with mock.patch("time.time", lambda: clock[0]):
                self.wipe()
                live = self.corpus(rng, model, clock)
                if not live:
                    return
                for call in range(rng.randint(2, 5)):
                    # Under an hour per call: a staged read lives
                    # _staged_ttl_seconds (24 h) on both legs, but Redis
                    # expires it on the server's clock, which the frozen
                    # time.time does not move, so the probe stays inside it.
                    clock[0] += rng.uniform(1, 3600)
                    if not self.assemble(rng, model, live, f"{tag} call={call}"):
                        # The legs' state has diverged (a classified
                        # difference changed what was selected or stored):
                        # later calls would only compare that difference.
                        self.diverged_shapes += 1
                        return
                clock[0] += 60
                self.assess(rng, model, f"{tag} assess")
        finally:
            Defaults.PG_VECTOR_EXACT_MAX = exact_max
            set_backend(None)

    def corpus(self, rng: random.Random, model: Any, clock: list) -> list[str]:
        vocab = [f"w{rng.randrange(10**6):06d}x" for _ in range(rng.randint(3, 25))]
        self.vocab = vocab
        n = rng.randint(1, 60)
        docs: dict[str, dict] = {}

        def text() -> str:
            if rng.random() < 0.05:
                return ""
            words = [rng.choice(vocab) for _ in range(rng.randint(1, 10))]
            if rng.random() < 0.3:
                words += rng.sample(STOP, 2)
            if rng.random() < 0.2:
                words.insert(0, words[0])
            return " ".join(words)

        keys: dict[str, str] = {}

        def save(name: str, values: dict) -> None:
            for leg in ("redis", "postgres"):
                self.leg(leg)
                obj = model(name=name, **values)
                obj.save()
                keys[name] = obj.db_key.redis_key

        for i in range(n):
            clock[0] += rng.choice([1.0, 60.0, 3600.0, DAY, 7 * DAY])
            values = {
                "agent": rng.choice(AGENTS),
                "tier": rng.choice(TIERS),
                "tags": rng.sample(TAGS, rng.randint(0, 2)),
                "content": text(),
                "importance": round(rng.uniform(0.1, 5.0), 3),
            }
            docs[f"m{i:03d}"] = values
            save(f"m{i:03d}", values)
        for _ in range(rng.randint(0, n // 4 + 1)):
            name = rng.choice(sorted(docs))
            clock[0] += rng.uniform(1, DAY)
            if rng.random() < 0.3:
                for leg in ("redis", "postgres"):
                    self.leg(leg)
                    model.query.get(name=name).delete()
                del docs[name]
                if not docs:
                    return []
            else:
                docs[name]["content"] = text()
                docs[name]["tier"] = rng.choice(TIERS)
                save(name, docs[name])
        # Confidence evidence and staged reads, the same on both legs.
        for name in rng.sample(sorted(docs), min(len(docs), rng.randint(0, 8))):
            signals = [rng.choice([0.1, 0.3, 0.5, 0.7, 0.9]) for _ in range(3)]
            for leg in ("redis", "postgres"):
                self.leg(leg)
                inst = model.query.get(name=name)
                for s in signals:
                    ConfidenceField.update_confidence(inst, "certainty", signal=s)
        self.names = sorted(docs)
        self.docs = docs
        #: Records an ``acted`` outcome touched (see the reload-after-touch
        #: divergence in compare_tokens).
        self.touched: set[str] = set()
        return [keys[n] for n in self.names]

    # -- assemble ---------------------------------------------------------------

    def _assembler(self, model: Any, options: dict) -> ContextAssembler:
        return ContextAssembler(model, **options)

    def assemble(self, rng: random.Random, model: Any, live: list, tag: str) -> bool:
        self.calls += 1
        mode = rng.choice(MODES[model])
        weights = rng.choice(
            [
                {"relevance": 1.0},
                {"relevance": 0.6, "certainty": 0.3},
                {"certainty": 1.0},
            ]
        )
        options: dict[str, Any] = {
            "score_weights": weights,
            "max_items": rng.choice([1, 2, 3, 5, 8]),
            "output_format": rng.choice(["structured", "xml", "natural", "content"]),
            "retrieval_mode": mode,
        }
        budget = rng.random()
        if budget < 0.4:
            options["max_tokens"] = rng.choice([5, 20, 60, 150, 400])
            if rng.random() < 0.5:
                options["token_counter"] = lambda text: len(text) // 4
        if rng.random() < 0.3:
            options["confidence_gate_threshold"] = rng.choice([0.2, 0.5, 0.8])
            options["confidence_gate_mode"] = rng.choice(["refuse", "flag"])
        cue_words = rng.sample(self.vocab, min(len(self.vocab), rng.randint(1, 3)))
        if rng.random() < 0.15:
            cue_words.append("absentterm")
        if rng.random() < 0.1:
            cue_words = ["zzzabsent"]
        cues = {"topic": " ".join(cue_words)}
        if rng.random() < 0.3:
            cues["other"] = rng.choice(self.vocab)
        if rng.random() < 0.08:
            cues = None
        scope = rng.random()
        partition: dict[str, Any] = {}
        if scope < 0.55:
            partition = {"agent": rng.choice(AGENTS)}
        elif scope < 0.7:
            partition = {"agent": rng.choice(AGENTS), "tier": rng.choice(TIERS)}
        elif scope < 0.8:
            partition = {"tier": rng.choice(TIERS)}
        kwargs: dict[str, Any] = {
            "query_cues": cues,
            "partition_filters": partition,
            "assess_quality": rng.random() < 0.4,
            "emit_trace": rng.random() < 0.4,
        }
        if rng.random() < 0.25:
            kwargs["tags"] = rng.sample(TAGS, rng.randint(1, 2))
            kwargs["tag_match"] = rng.choice(["any", "all"])
        if rng.random() < 0.2:
            kwargs["exclude_keys"] = set(rng.sample(live, rng.randint(0, len(live))))
        detail_args = f"{tag} {options} {kwargs}"
        if cues and self.existence_differs(model, cues):
            # Documented (docs/features/postgres-backend.md, M2b): the
            # Postgres filter is exact and forgets a deleted or rewritten
            # record's tokens; Redis's bloom keeps them (and has false
            # positives). The legs then disagree on the short-circuit by
            # design, and the call's selection, and so its effects, differ.
            self.skipped["existence-filter-exact"] += 1
            return True

        results = self.both(lambda: self._assembler(model, options).assemble(**kwargs))
        r, p = results["redis"], results["postgres"]
        if isinstance(r, str) or isinstance(p, str):
            same = isinstance(r, str) and isinstance(p, str) and r == p
            self.check("keys", same, lambda: f"{detail_args}: {r} / {p}")
            return same
        effective = self._assembler(model, options)._effective_mode
        self.modes[effective] += 1
        if r.records:
            self.nonempty[effective] += 1
        rk = [x.db_key.redis_key for x in r.records]
        pk = [x.db_key.redis_key for x in p.records]
        if rk != pk:
            self.why = ""
            cls = self.classify_keys(model, options, kwargs, rk, pk)
            self.check(cls, False, lambda: f"{detail_args}: {rk} / {pk} {self.why}")
            return False
        self.checks["keys"] += 1
        self.compare_tokens(r, p, rk, detail_args)
        meta_keys = ("pull_count", "push_count", "total_candidates", "gate")
        rm = {k: r.metadata.get(k) for k in meta_keys}
        pm = {k: p.metadata.get(k) for k in meta_keys}
        self.check("metadata", rm == pm, lambda: f"{detail_args}: {rm} / {pm}")
        if "trace" in r.metadata or "trace" in p.metadata:
            rt, pt = r.metadata.get("trace"), p.metadata.get("trace")
            ok = (
                rt is not None
                and pt is not None
                and len(rt) == len(pt)
                and all(
                    a["key"] == b["key"]
                    and a["rank"] == b["rank"]
                    and a["source"] == b["source"]
                    and _close(float(a["score"]), float(b["score"]))
                    for a, b in zip(rt, pt)
                )
            )
            self.check("trace", ok, lambda: f"{detail_args}: {rt} / {pt}")
        if "quality" in r.metadata or "quality" in p.metadata:
            self.compare_quality(
                "quality",
                r.metadata.get("quality"),
                p.metadata.get("quality"),
                detail_args,
            )
        if not self.compare_state("effects", model, detail_args):
            return False
        if r.records and rng.random() < 0.6:
            outcome = {
                x.db_key.redis_key: rng.choice(OUTCOMES)
                for x in r.records
                if rng.random() < 0.8
            }

            for leg, res in (("redis", r), ("postgres", p)):
                self.leg(leg)
                ObservationProtocol.on_context_used(res.records, outcome)
            self.touched |= {k for k, v in outcome.items() if v == "acted"}
            return self.compare_state("outcomes", model, f"{detail_args} {outcome}")
        return True

    # -- classification ---------------------------------------------------------

    def existence_differs(self, model: Any, cues: dict) -> bool:
        field = model._meta.fields.get("bloom")
        if field is None:
            return False
        out = self.both(
            lambda: [bool(field.might_exist(model, str(v))) for v in cues.values()]
        )
        return out["redis"] != out["postgres"]

    def compare_tokens(self, r: Any, p: Any, keys: list, detail_args: str) -> None:
        same = (
            r.metadata["token_count"] == p.metadata["token_count"]
            and r.formatted == p.formatted
        )
        if same:
            self.checks["tokens"] += 1
            return

        def mask(text: str) -> str:
            return RELEVANCE_VALUE.sub(r"\1<t>", text)

        if mask(r.formatted) == mask(p.formatted) and self.touched & set(keys):
            # Documented (M2a): touch() moves only the sorted-set score on
            # Redis, so a reload keeps the save-time value of the decay
            # field; on Postgres the clock is the column, so a reload sees
            # the touched time. The serialized `relevance` differs, and with
            # it, by a digit, the estimated token count.
            self.check("tokens:reload-after-touch", False, lambda: detail_args)
            return
        self.check(
            "tokens",
            False,
            lambda: f"{detail_args}: {r.metadata['token_count']} / "
            f"{p.metadata['token_count']}\n{r.formatted[:300]}\n---\n"
            f"{p.formatted[:300]}",
        )

    def classify_keys(self, model, options, kwargs, rk, pk) -> str:
        """Name a ranked-key difference by re-asking the arms it is made of.
        The arms have no side effects; the unbudgeted re-run does, on both
        legs alike, and ends the shape anyway."""
        if options.get("max_tokens") is not None and self.touched:
            unbudgeted = {
                k: v
                for k, v in options.items()
                if k not in ("max_tokens", "token_counter")
            }
            again = self.both(
                lambda: [
                    x.db_key.redis_key
                    for x in self._assembler(model, unbudgeted)
                    .assemble(**kwargs)
                    .records
                ]
            )
            if again["redis"] == again["postgres"]:
                return "keys:budget-after-touch"
        cues = kwargs.get("query_cues") or {}
        text = " ".join(str(v) for v in cues.values())
        assembler = self._assembler(model, options)
        if assembler._effective_mode in ("hybrid", "lexical") and text:
            limit = assembler.max_items * 5 + len(kwargs.get("exclude_keys") or ())
            bm25 = self.both(
                lambda: BM25Field.search(model, "lexical", text, limit=limit)
            )
            if bm25["redis"] != bm25["postgres"]:
                rs, ps = dict(bm25["redis"]), dict(bm25["postgres"])
                if rs.keys() == ps.keys() and all(
                    math.isclose(rs[k], ps[k], rel_tol=1e-6) for k in rs
                ):
                    return "keys:bm25-near-tie"
                return "keys:bm25-arm"
            if assembler._effective_mode == "hybrid":
                from popoto.models.query import QueryBuilder

                vec = self.both(
                    lambda: QueryBuilder(model.query)._get_vector_scores(
                        text, limit=limit
                    )
                )
                rv, pv = vec["redis"], vec["postgres"]
                if [k for k, _ in rv] != [k for k, _ in pv]:
                    rs, ps = dict(rv), dict(pv)
                    common = set(rs) & set(ps)
                    rkeys = [k for k, _ in rv]
                    pkeys = [k for k, _ in pv]
                    first = next(
                        (i for i, (a, b) in enumerate(zip(rkeys, pkeys)) if a != b),
                        None,
                    )
                    exact_tie = False
                    if first is not None and rkeys[first] in ps:
                        a, b = rkeys[first], pkeys[first]
                        self.why = (
                            f"vector arm: {a} {rs.get(a)!r}/{ps.get(a)!r} vs "
                            f"{b} {rs.get(b)!r}/{ps.get(b)!r}"
                        )
                        exact_tie = b in rs and rs[a] == rs[b] and ps[a] == ps[b]
                    if all(abs(rs[k] - ps[k]) <= 1e-6 for k in common):
                        if exact_tie:
                            return "keys:vector-exact-tie-order"
                        return "keys:vector-float32-near-tie"
                    return "keys:vector-arm"
        return "keys"

    def compare_quality(self, cls: str, rq: Any, pq: Any, detail_args: str) -> None:
        def flat(q: Any) -> Any:
            if q is None:
                return None
            return {
                "avg_confidence": q.avg_confidence,
                "score_spread": q.score_spread,
                "fok_score": q.fok_score,
                "staleness_ratio": q.staleness_ratio,
                "dist": list(q.score_distribution),
                "per_cue": {
                    cue: dict(sorted(comp.items()))
                    for cue, comp in q.per_cue_fok.items()
                },
            }

        a, b = flat(rq), flat(pq)
        ok = a is not None and b is not None
        if ok:
            for key in (
                "avg_confidence",
                "score_spread",
                "fok_score",
                "staleness_ratio",
            ):
                ok = ok and _close(float(a[key]), float(b[key]))
            ok = ok and len(a["dist"]) == len(b["dist"])
            ok = ok and all(
                _close(float(x), float(y)) for x, y in zip(a["dist"], b["dist"])
            )
            ok = ok and a["per_cue"].keys() == b["per_cue"].keys()
            for cue in a["per_cue"] if ok else ():
                for k, v in a["per_cue"][cue].items():
                    ok = ok and _close(float(v), float(b["per_cue"][cue][k]))
        self.check(cls, ok, lambda: f"{detail_args}: {a} / {b}")

    def compare_state(self, cls: str, model: Any, detail_args: str) -> bool:
        def state() -> dict:
            out = {}
            for name in self.names:
                # An unsaved instance only to name the record: reading
                # through a query would stage a read and change the state.
                probe = model(name=name, agent=self.docs[name]["agent"])
                data = ConfidenceField.get_confidence_data(probe, "certainty")
                staged = None
                if issubclass(model, AccessTrackerMixin):
                    staged = self.staged(probe)
                out[name] = (
                    round(float(data["confidence"]), 12),
                    data["evidence_count"],
                    data["corroborations"],
                    data["contradictions"],
                    staged,
                )
            return out

        res = self.both(state)
        diff = {
            n: (res["redis"][n], res["postgres"][n])
            for n in self.names
            if isinstance(res["redis"], dict)
            and isinstance(res["postgres"], dict)
            and res["redis"][n] != res["postgres"][n]
        }
        ok = isinstance(res["redis"], dict) and isinstance(res["postgres"], dict)
        self.check(cls, ok and not diff, lambda: f"{detail_args}: {diff or res}")
        return ok and not diff

    def staged(self, probe: Any) -> int:
        from popoto.backends import get_backend

        backend = get_backend(type(probe))
        if backend.name == "redis":
            return int(popoto.get_redis().llen(probe._at_key("staged")))
        return int(probe._access_state(backend)[2])

    # -- assess -----------------------------------------------------------------

    def assess(self, rng: random.Random, model: Any, tag: str) -> None:
        cues = {"topic": " ".join(rng.sample(self.vocab, 1))}
        partition = {"agent": rng.choice(AGENTS)} if rng.random() < 0.7 else {}
        options = {
            "score_weights": rng.choice(
                [{"relevance": 1.0}, {"relevance": 0.6, "certainty": 0.3}]
            ),
            "max_items": rng.choice([2, 5]),
        }
        if self.existence_differs(model, cues):
            self.skipped["existence-filter-exact"] += 1
            return
        out = self.both(lambda: self._assembler(model, options).assess(cues, partition))
        r, p = out["redis"], out["postgres"]
        if isinstance(r, str) or isinstance(p, str):
            self.check("assess", r == p, lambda: f"{tag}: {r} / {p}")
            return
        self.compare_quality("assess", r, p, f"{tag} {options} {cues} {partition}")

    # -- report -------------------------------------------------------------------

    def report(self) -> str:
        lines = [
            f"shapes: {self.shapes}, assemble() calls: {self.calls}",
            "calls by effective mode (with records): "
            + ", ".join(
                f"{m} {n} ({self.nonempty[m]})" for m, n in sorted(self.modes.items())
            ),
            f"calls skipped (ExistenceFilter exact vs bloom, documented): "
            f"{self.skipped['existence-filter-exact']}",
            f"shapes ended at a classified divergence: {self.diverged_shapes}",
            "| class | checks | mismatches |",
            "|---|---:|---:|",
        ]
        for cls in sorted(self.checks):
            tag = " (explained)" if cls in EXPLAINED else ""
            lines.append(
                f"| {cls}{tag} | {self.checks[cls]} | {self.mismatches[cls]} |"
            )
        for cls, examples in sorted(self.examples.items()):
            lines.append(f"\n{cls} examples:")
            lines.extend(f"  - {e}" for e in examples)
        return "\n".join(lines)

    def unexplained(self) -> int:
        return sum(n for cls, n in self.mismatches.items() if cls not in EXPLAINED)


def run(
    pg: Any, seeds: list[int], shapes: int, *, admin: Any = None, verbose: bool = False
) -> Probe:
    probe = Probe(pg, admin, verbose=verbose)
    try:
        for seed in seeds:
            rng = random.Random(seed)
            for index in range(shapes):
                probe.shape(rng, seed, index)
    finally:
        set_backend(None)
    return probe


def main() -> None:
    import psycopg

    from popoto.backends import _swap_instance
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
    previous = _swap_instance("postgres", pg)
    started = time.perf_counter()
    try:
        probe = run(pg, args.seeds, args.shapes, admin=admin, verbose=args.verbose)
    finally:
        _swap_instance("postgres", previous)
        pg.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        client = popoto.get_redis()
        for model in MODELS:
            for key in client.scan_iter(match=f"*{model.__name__}*", count=1000):
                client.delete(key)
    print(f"seeds {args.seeds}, {time.perf_counter() - started:.1f}s")
    print(probe.report())
    sys.exit(1 if probe.unexplained() else 0)


if __name__ == "__main__":
    main()
