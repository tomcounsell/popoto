#!/usr/bin/env python
"""Seeded two-leg probe: M2b search on Redis vs Postgres (#759 M2b).

Each *shape* is one random scenario, drawn from a seeded RNG: a corpus (size,
vocabulary, repeated terms, empty documents, scopes), an embedding
dimension (<= 64), a vector regime (exact, or HNSW by pinning
``Defaults.PG_VECTOR_EXACT_MAX`` to 0), a few updates and deletes (so Redis's
running ``avgdl`` drifts the way it does in production), and a batch of
queries. The same operations run against a Redis-bound and a Postgres-bound
copy of the model, and every answer is compared:

* ``BM25Field.search`` -- same keys in the same order, scores within 1e-9
  relative (with and without ``allowed_keys``, random ``limit``);
* ``BM25Field.get_idf`` -- equal;
* ``Query.keyword_search`` -- the same instances and ``_bm25_score``;
* the vector arm (``QueryBuilder._get_vector_scores``) -- the same keys,
  similarities within 1e-6 absolute;
* ``ExistenceFilter.might_exist`` / ``FrequencySketch.get_frequency`` --
  Postgres exactly the truth; Redis never a false negative / never under;
* ``Query.fuse`` over the BM25 list and a random list, with and without a
  builder filter -- the same instances and ``_rrf_score``.

A disagreement is classified and counted; the report lists each class with
an example. Exit status 0 means no *unexplained* class.

Safety (CLAUDE.md, #577): Redis is bound from ``REDIS_URL`` before popoto is
imported, database 0 is refused, and only that database's probe keys are
touched (``FLUSHDB`` on the named, non-zero database). Postgres: the script
creates its own ``popoto_test_<hex>`` schema in ``POPOTO_POSTGRES_URL`` (whose
database must already have ``CREATE EXTENSION vector``), and drops only that.

Usage::

    REDIS_URL=redis://localhost:6379/13 \\
    POPOTO_POSTGRES_URL=postgresql://localhost:5432/popoto_m2b \\
    python scripts/probe_search_parity.py --seeds 1 2 3 --shapes 170
"""

from __future__ import annotations

import argparse
import collections
import os
import random
import sys
import tempfile
import time
import uuid
import zlib
from urllib.parse import parse_qs, urlsplit

REDIS_URL = os.environ.get("REDIS_URL", "")


def _redis_db_number(url: str) -> int:
    parts = urlsplit(url)
    path = parts.path.strip("/")
    if path:
        return int(path)
    qs = parse_qs(parts.query).get("db")
    return int(qs[0]) if qs else 0


if not REDIS_URL or _redis_db_number(REDIS_URL) == 0:
    sys.exit("set REDIS_URL=redis://localhost:6379/<n>, n != 0, before running")
PG_URL = os.environ.get("POPOTO_POSTGRES_URL", "")
if not PG_URL:
    sys.exit("set POPOTO_POSTGRES_URL to a database that has CREATE EXTENSION vector")
os.environ.setdefault("POPOTO_EMBEDDING_INVALIDATION", "none")
_CONTENT = tempfile.mkdtemp(prefix="popoto-probe-")
os.environ["POPOTO_CONTENT_PATH"] = _CONTENT

import numpy as np  # noqa: E402

import popoto  # noqa: E402
from popoto.backends import _swap_instance  # noqa: E402
from popoto.backends.postgres import PostgresBackend  # noqa: E402
from popoto.embeddings import AbstractEmbeddingProvider  # noqa: E402
from popoto.fields.bm25_field import BM25Field  # noqa: E402
from popoto.fields.constants import Defaults  # noqa: E402
from popoto.fields.embedding_field import EmbeddingField, invalidate_cache  # noqa: E402
from popoto.fields.existence_filter import (
    ExistenceFilter,
    FrequencySketch,
)  # noqa: E402
from popoto.models.query import QueryBuilder  # noqa: E402
from popoto.redis_db import get_REDIS_DB  # noqa: E402

STOP = ["the", "and", "for", "with", "from", "that"]


def _seed(shape_seed: int, text: str) -> int:
    """A process-independent seed (``hash()`` of a str is salted per run)."""
    return zlib.crc32(f"{shape_seed}:{text}".encode()) & 0x7FFFFFFF


class SeededProvider(AbstractEmbeddingProvider):
    """A vector per distinct text, from a per-shape seed; texts that share
    a prefix word share a direction, so near neighbours exist."""

    def __init__(self, dim: int, seed: int) -> None:
        self._dim = dim
        self._seed = seed

    def embed(self, texts, input_type=None):
        out = []
        for text in texts:
            words = text.split() or [""]
            base = np.random.RandomState(_seed(self._seed, words[0])).randn(self._dim)
            noise = np.random.RandomState(_seed(self._seed, text)).randn(self._dim)
            out.append((base + 0.6 * noise).tolist())
        return out

    @property
    def dimensions(self):
        return self._dim

    @property
    def max_batch_size(self):
        return 64


def make_model(backend: str, provider: SeededProvider):
    attrs = {
        "__module__": __name__,
        "name": popoto.UniqueKeyField(),
        "owner": popoto.KeyField(null=True),
        "project": popoto.Field(type=str, null=True),
        "stamp": popoto.SortedField(type=float, default=0.0, partition_by="project"),
        "text": popoto.StringField(default=""),
        "content": BM25Field(source="text"),
        "embedding": EmbeddingField(source="text", provider=provider),
        "bloom": ExistenceFilter(
            error_rate=0.05, capacity=500, fingerprint_fn=lambda i: i.text
        ),
        "freq": FrequencySketch(width=97, depth=3, fingerprint_fn=lambda i: i.text),
        "Meta": type("Meta", (), {"backend": backend}),
    }
    return type("ProbeDoc", (popoto.Model,), attrs)


class Mismatch(Exception):
    pass


def rel_close(a: float, b: float, tol: float) -> bool:
    return a == b or abs(a - b) <= tol * max(abs(a), abs(b))


class Probe:
    def __init__(self, pg: PostgresBackend, admin) -> None:
        self.pg = pg
        self.admin = admin
        self.classes = collections.Counter()
        self.examples: dict[str, str] = {}
        self.comparisons = collections.Counter()

    def note(self, cls: str, example: str) -> None:
        self.classes[cls] += 1
        self.examples.setdefault(cls, example)

    def reset(self) -> None:
        get_REDIS_DB().flushdb()
        invalidate_cache()
        for root, dirs, files in os.walk(_CONTENT, topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
        schema = self.pg.schema
        self.admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        self.admin.execute(f'CREATE SCHEMA "{schema}"')
        self.pg.forget_tables()

    # -- one shape -------------------------------------------------------------

    def shape(self, rng: random.Random, shape_id: str) -> None:
        self.reset()
        dim = rng.choice([2, 3, 8, 16, 31, 64])
        provider = SeededProvider(dim, rng.randrange(1 << 30))
        regime = rng.choice(["exact", "hnsw"])
        Defaults.PG_VECTOR_EXACT_MAX = 0 if regime == "hnsw" else 5000
        R = make_model("redis", provider)
        P = make_model("postgres", provider)
        vocab = [f"w{rng.randrange(10**6):06d}x" for _ in range(rng.randint(3, 40))]
        projects = [None, "p1", "p2", "p3"][: rng.randint(1, 4)]
        owners = ["alice", "bob", "carol"]
        n_docs = rng.randint(1, 120)
        docs = {}

        def text() -> str:
            if rng.random() < 0.05:
                return ""
            words = [rng.choice(vocab) for _ in range(rng.randint(1, 12))]
            if rng.random() < 0.3:
                words += rng.sample(STOP, 2)
            if rng.random() < 0.2:
                words.insert(0, words[0])  # repeated term, tf > 1
            return " ".join(words)

        keys: dict[str, str] = {}

        def save(name: str, **values) -> None:
            for M in (R, P):
                obj = M(name=name, **values)
                obj.save()
                keys[name] = obj.pk

        for i in range(n_docs):
            values = {
                "owner": rng.choice(owners),
                "project": rng.choice(projects),
                "stamp": float(i),
                "text": text(),
            }
            docs[f"d{i:04d}"] = values
            save(f"d{i:04d}", **values)
        for _ in range(rng.randint(0, n_docs // 3 + 1)):
            name = rng.choice(list(docs))
            if rng.random() < 0.3:
                for M in (R, P):
                    M.query.get(name=name).delete()
                del docs[name]
                if not docs:
                    break
            else:
                docs[name]["text"] = text()
                docs[name]["project"] = rng.choice(projects)
                save(name, **docs[name])
        if not docs:
            return
        Defaults.PG_VECTOR_EXACT_MAX = 0 if regime == "hnsw" else 5000
        live = [keys[n] for n in docs]
        for q in range(rng.randint(3, 8)):
            self.queries(rng, R, P, live, vocab, f"{shape_id}/q{q}/{regime}/d{dim}")
        self.membership(rng, R, P, docs, vocab, shape_id)

    def queries(self, rng, R, P, live, vocab, where) -> None:
        terms = rng.sample(vocab, min(len(vocab), rng.randint(1, 4)))
        if rng.random() < 0.2:
            terms.append("absentterm")
        if rng.random() < 0.1:
            terms = rng.sample(STOP, 2)
        query = " ".join(terms)
        limit = rng.choice([1, 2, 3, 5, 10, 50])
        allowed = None
        if rng.random() < 0.4:
            allowed = set(rng.sample(live, rng.randint(0, len(live))))
        r = BM25Field.search(R, "content", query, limit=limit, allowed_keys=allowed)
        p = BM25Field.search(P, "content", query, limit=limit, allowed_keys=allowed)
        self.comparisons["bm25_search"] += 1
        self.compare_scored("bm25", r, p, 1e-9, f"{where} {query!r} limit={limit}")

        tokens = query.split()
        self.comparisons["get_idf"] += 1
        ri = BM25Field.get_idf(R, "content", tokens)
        pi = BM25Field.get_idf(P, "content", tokens)
        if ri != pi:
            self.note("idf", f"{where}: {ri} != {pi}")

        self.comparisons["keyword_search"] += 1
        rk = [
            (i.name, i._bm25_score) for i in R.query.keyword_search(query, limit=limit)
        ]
        pk = [
            (i.name, i._bm25_score) for i in P.query.keyword_search(query, limit=limit)
        ]
        self.compare_scored("keyword_search", rk, pk, 1e-9, where)

        self.comparisons["vector"] += 1
        vlimit = rng.choice([1, 5, 10, 200])
        rv = QueryBuilder(R.query)._get_vector_scores(query, limit=vlimit)
        pv = QueryBuilder(P.query)._get_vector_scores(query, limit=vlimit)
        self.compare_vectors(rv, pv, f"{where} {query!r} limit={vlimit}")

        self.comparisons["fuse"] += 1
        other = [(k, 1.0) for k in rng.sample(live, min(5, len(live)))]
        owner = rng.choice(["alice", "bob", None])
        rq = R.query.filter(owner=owner) if owner else R.query
        pq = P.query.filter(owner=owner) if owner else P.query
        rf = [
            (i.name, i._rrf_score) for i in rq.fuse(keyword=r, other=other, limit=limit)
        ]
        pf = [
            (i.name, i._rrf_score) for i in pq.fuse(keyword=r, other=other, limit=limit)
        ]
        if rf != pf:
            self.note("fuse", f"{where}: {rf[:4]} != {pf[:4]}")

    def compare_scored(self, label, r, p, tol, where) -> None:
        if [k for k, _ in r] != [k for k, _ in p]:
            rs, ps = dict(r), dict(p)
            if set(rs) == set(ps) and all(rel_close(rs[k], ps[k], tol) for k in rs):
                # Same members and scores, different order: a near-tie that
                # Redis's drifted avgdl and the exact one break differently.
                self.note(f"{label}:near-tie-order", f"{where}: {r[:3]} vs {p[:3]}")
            else:
                self.note(f"{label}:members", f"{where}: {r[:3]} vs {p[:3]}")
            return
        for (k, rs), (_k, ps) in zip(r, p):
            if not rel_close(rs, ps, tol):
                self.note(f"{label}:score", f"{where}: {k} {rs!r} vs {ps!r}")
                return

    def compare_vectors(self, r, p, where) -> None:
        rk = [k for k, _ in r]
        pk = [k for k, _ in p]
        rs, ps = dict(r), dict(p)
        for k in set(rk) & set(pk):
            if abs(rs[k] - ps[k]) > 1e-6:
                self.note("vector:score", f"{where}: {k} {rs[k]!r} vs {ps[k]!r}")
                return
        if rk == pk:
            return
        if set(rk) == set(pk):
            # Classify by the first disagreement: the two keys' Redis scores.
            i = next(j for j in range(len(rk)) if rk[j] != pk[j])
            gap = abs(rs[rk[i]] - rs[pk[i]])
            if gap == 0:
                cls = "vector:exact-tie-order"  # equal vectors: listdir vs _pk
            elif gap <= 1e-6:
                cls = "vector:float32-near-tie-order"
            else:
                cls = "vector:order"
            self.note(cls, f"{where}: {rk[i]} {rs[rk[i]]!r} / {pk[i]} {rs[pk[i]]!r}")
            return
        # Different members: at the limit boundary (equal or near-equal
        # scores straddling it), or a real miss.
        boundary = r and p and abs(r[-1][1] - p[-1][1]) <= 1e-6
        self.note(
            "vector:boundary-tie" if boundary else "vector:members",
            f"{where}: {r[-2:]} vs {p[-2:]}",
        )

    def membership(self, rng, R, P, docs, vocab, where) -> None:
        from popoto.fields._tokenizer import tokenize

        present = collections.Counter()
        # Postgres's token table holds the live records' tokens; its count
        # table counts every save, live or deleted (as the sketch does), so
        # its truth is read from Postgres's own exactness checks below.
        for values in docs.values():
            toks = tokenize(values["text"]) or [values["text"].lower()]
            present.update(set(toks))
        for token in rng.sample(vocab, min(len(vocab), 10)) + ["absentterm"]:
            self.comparisons["might_exist"] += 1
            truth = token in present
            pr = P.bloom.might_exist(P, token)
            rr = R.bloom.might_exist(R, token)
            if pr != truth:
                self.note("bloom:postgres-not-exact", f"{where}: {token} {pr}")
            if truth and not rr:
                self.note("bloom:redis-false-negative", f"{where}: {token}")
            if rr and not truth:
                self.note("bloom:redis-false-positive (expected)", f"{where}: {token}")
            self.comparisons["get_frequency"] += 1
            pf = P.freq.get_frequency(P, token)
            rf = R.freq.get_frequency(R, token)
            if rf < pf:
                self.note("cms:redis-undercount", f"{where}: {token} {rf} < {pf}")
            elif rf > pf:
                self.note("cms:redis-overcount (expected)", f"{where}: {token}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--shapes", type=int, default=170)
    args = ap.parse_args()

    import psycopg

    schema = f"popoto_test_{uuid.uuid4().hex}"
    admin = psycopg.connect(PG_URL, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    pg = PostgresBackend(dsn=PG_URL, schema=schema)
    previous = _swap_instance("postgres", pg)
    probe = Probe(pg, admin)
    started = time.time()
    shapes = 0
    try:
        for seed in args.seeds:
            rng = random.Random(seed)
            for i in range(args.shapes):
                probe.shape(rng, f"s{seed}/{i}")
                shapes += 1
    finally:
        _swap_instance("postgres", previous)
        pg.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        get_REDIS_DB().flushdb()

    print(f"shapes: {shapes} over seeds {args.seeds} in {time.time() - started:.0f}s")
    print(
        "comparisons:",
        dict(probe.comparisons),
        "total",
        sum(probe.comparisons.values()),
    )
    expected = {k for k in probe.classes if "(expected)" in k}
    explained = {
        "vector:exact-tie-order",
        "vector:float32-near-tie-order",
        "vector:boundary-tie",
        "bm25:near-tie-order",
        "keyword_search:near-tie-order",
    }
    print("mismatch classes:")
    for cls, n in sorted(probe.classes.items()):
        tag = (
            "expected"
            if cls in expected
            else "explained" if cls in explained else "UNEXPLAINED"
        )
        print(f"  {cls}: {n} [{tag}]  e.g. {probe.examples[cls]}")
    bad = set(probe.classes) - expected - explained
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
