#!/usr/bin/env python
"""``recall()`` latency on Postgres (#759 M2b exit criterion).

The plan's bar (§5 M2): ``recall`` on a 20k-row, 1536-d corpus is <= 15 ms
p95 at 5% scope and <= 60 ms p95 at 60% scope. The corpus follows #758
spike-4: five projects (60 / 20 / 10 / 5 / 5 %), documents of 40-80 tokens
from a 30k-term Zipf vocabulary, and clustered synthetic vectors. Records go
in through ``Model.save()`` -- postings, document length and vector written
by the engine's own statement -- then the table is ``ANALYZE``d.

Each measured call is the public ``Model.query.recall(q, scope=p,
limit=10)``: the query embedding (a synthetic provider, so the provider's
own latency is not in the number), the BM25 and vector arms, RRF fusion and
the full rows of the fused records (an index-only count, then one statement on the exact path; the HNSW arm is its own statement). The two arms are also
timed alone (``keyword_search`` with per-scope statistics, the vector arm)
to show where the time goes. ``--runs`` runs of ``--iters`` calls after a
warm-up; each run's p50 / p95 and the overall p95 are printed.

Safety: Postgres only (no Redis is touched). The script creates its own
``popoto_test_<hex>`` schema in ``POPOTO_POSTGRES_URL`` -- whose database must
have ``CREATE EXTENSION vector`` -- and drops only that, unless ``--keep``.

Usage::

    POPOTO_POSTGRES_URL=postgresql://localhost:5432/popoto_m2b \\
    python scripts/bench_recall.py [--n 20000] [--dim 1536] [--iters 200]
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
import time
import uuid

PG_URL = os.environ.get("POPOTO_POSTGRES_URL", "")
if not PG_URL:
    sys.exit("set POPOTO_POSTGRES_URL to a database that has CREATE EXTENSION vector")

import numpy as np  # noqa: E402

import popoto  # noqa: E402
from popoto.backends import _swap_instance  # noqa: E402
from popoto.backends.postgres import PostgresBackend  # noqa: E402
from popoto.embeddings import AbstractEmbeddingProvider  # noqa: E402
from popoto.fields._tokenizer import tokenize  # noqa: E402
from popoto.fields.bm25_field import BM25Field  # noqa: E402
from popoto.fields.constants import Defaults  # noqa: E402
from popoto.fields.embedding_field import EmbeddingField  # noqa: E402

PROJECTS = [("p60", 0.60), ("p20", 0.20), ("p10", 0.10), ("p5a", 0.05), ("p5b", 0.05)]


class ClusterProvider(AbstractEmbeddingProvider):
    """Synthetic clustered vectors: a document's first word picks its
    cluster, so queries have real neighbours. Fast, so it stays out of the
    measured latency."""

    def __init__(self, dim: int, clusters: int = 64, seed: int = 7) -> None:
        rng = np.random.default_rng(seed)
        self._dim = dim
        self.centers = rng.standard_normal((clusters, dim)).astype(np.float32)
        self._rng = np.random.default_rng(seed + 1)

    def embed(self, texts, input_type=None):
        out = []
        for text in texts:
            c = (sum(map(ord, text.split()[0])) if text else 0) % len(self.centers)
            v = self.centers[c] + 0.5 * self._rng.standard_normal(self._dim).astype(
                np.float32
            )
            out.append(v.tolist())
        return out

    @property
    def dimensions(self):
        return self._dim

    @property
    def max_batch_size(self):
        return 128


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--dim", type=int, default=1536)
    ap.add_argument("--iters", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument(
        "--exact-max",
        type=int,
        default=None,
        help="override Defaults.PG_VECTOR_EXACT_MAX for this run",
    )
    ap.add_argument(
        "--reuse",
        metavar="SCHEMA",
        help="time against a schema an earlier --keep run seeded (no seeding)",
    )
    args = ap.parse_args()
    if args.exact_max is not None:
        Defaults.PG_VECTOR_EXACT_MAX = args.exact_max

    import psycopg

    provider = ClusterProvider(args.dim)

    class BenchMemory(popoto.Model):
        name = popoto.UniqueKeyField()
        project = popoto.Field(type=str)
        stamp = popoto.SortedField(type=float, default=0.0, partition_by="project")
        content = popoto.StringField(default="")
        lexical = BM25Field(source="content")
        embedding = EmbeddingField(source="content", provider=provider)

        class Meta:
            backend = "postgres"

    schema = args.reuse or f"popoto_test_{uuid.uuid4().hex}"
    if not schema.startswith("popoto_test_"):
        sys.exit("--reuse takes a popoto_test_<hex> schema")
    admin = psycopg.connect(PG_URL, autocommit=True)
    if not args.reuse:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    backend = PostgresBackend(dsn=PG_URL, schema=schema)
    previous = _swap_instance("postgres", backend)
    rng = np.random.default_rng(11)
    vocab = [f"t{i:05d}" for i in range(30000)]
    zipf = 1.0 / np.arange(1, len(vocab) + 1) ** 1.07
    zipf /= zipf.sum()
    try:
        started = time.time()
        sizes = [int(args.n * share) for _p, share in PROJECTS]
        i = 0
        if args.reuse:
            sizes = [0] * len(PROJECTS)
            (i,) = admin.execute(
                f'SELECT count(*) FROM "{schema}".bench_memory'
            ).fetchone()
        for (project, _share), size in zip(PROJECTS, sizes):
            for _ in range(size):
                words = rng.choice(vocab, size=int(rng.integers(40, 81)), p=zipf)
                BenchMemory(
                    name=f"m{i:06d}",
                    project=project,
                    stamp=float(i),
                    content=" ".join(words),
                ).save()
                i += 1
                if i % 2000 == 0:
                    print(f"  seeded {i} in {time.time() - started:.0f}s", flush=True)
        seed_s = time.time() - started
        admin.execute(f'VACUUM ANALYZE "{schema}".bench_memory')
        for suffix in ("lexical__post", "lexical__dl", "embedding__vec"):
            admin.execute(f'VACUUM ANALYZE "{schema}".bench_memory__{suffix}')
        (postings,) = admin.execute(
            f'SELECT count(*) FROM "{schema}".bench_memory__lexical__post'
        ).fetchone()

        spec = BenchMemory._meta.spec
        qrng = np.random.default_rng(23)

        def query() -> str:
            return " ".join(qrng.choice(vocab[:5000], size=3, p=None))

        def timed(fn, n) -> list[float]:
            out = []
            for _ in range(n):
                q = query()
                t0 = time.perf_counter()
                fn(q)
                out.append((time.perf_counter() - t0) * 1000)
            return out

        def pct(xs, p) -> float:
            return float(np.percentile(xs, p))

        (server,) = admin.execute("SELECT version()").fetchone()
        (pgvector,) = admin.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        (buffers,) = admin.execute("SHOW shared_buffers").fetchone()
        print()
        print(
            f"environment: {platform.machine()} {platform.platform()}, Python "
            f"{platform.python_version()}, psycopg {psycopg.__version__}, "
            f"{server.split(',')[0]}, pgvector {pgvector}, shared_buffers {buffers}"
        )
        print(
            f"corpus: {i} rows, {args.dim}-d, {postings} postings, seeded through "
            f"save() in {seed_s:.0f}s; PG_VECTOR_EXACT_MAX="
            f"{Defaults.PG_VECTOR_EXACT_MAX}, PG_HNSW_EF_SEARCH="
            f"{Defaults.PG_HNSW_EF_SEARCH}"
        )
        results = {}
        for project, label, budget in (("p5a", "5%", 15.0), ("p60", "60%", 60.0)):
            recall = lambda q: BenchMemory.query.recall(
                q, scope=project, limit=10
            )  # noqa: E731
            bm25 = lambda q: backend.keyword_search(  # noqa: E731
                spec, "lexical", tokenize(q), limit=50, scope=project, stats="scope"
            )
            qv = provider.embed(["t00001 t00002"])[0]
            from popoto.backends.types import Cond, Op

            where = Cond("project", Op.EXACT, project)
            vec = lambda q: backend._vector_arm(  # noqa: E731
                spec, "embedding", qv, limit=50, where=where
            )
            timed(recall, args.warmup)
            runs, loads = [], []
            for _ in range(args.runs):
                runs.append(timed(recall, args.iters))
                loads.append(os.getloadavg()[0])
            every = [x for run in runs for x in run]
            arms = {
                "bm25 arm": timed(bm25, args.iters),
                "vector arm": timed(vec, args.iters),
            }
            _scored, info = vec("")
            BenchMemory.query.recall(query(), scope=project, limit=10)
            last = backend._last_recall
            print(
                f"\nrecall(scope={project!r}, limit=10) -- {label} scope "
                f"({info['embedded']} rows with a vector; vector path "
                f"{info['path']}, guard fired: {last.get('guard')})"
            )
            for k, run in enumerate(runs, 1):
                print(
                    f"  run {k}: p50 {pct(run, 50):6.2f} ms  p95 {pct(run, 95):6.2f} ms"
                    f"  (load average {loads[k - 1]:.2f})"
                )
            p95 = pct(every, 95)
            verdict = "PASS" if p95 <= budget else "FAIL"
            print(
                f"  all {len(every)}: p50 {pct(every, 50):.2f} ms, p95 {p95:.2f} ms "
                f"(exit <= {budget:.0f} ms p95: {verdict})"
            )
            for name, xs in arms.items():
                print(
                    f"  {name} alone: p50 {pct(xs, 50):.2f} ms  p95 {pct(xs, 95):.2f} ms"
                )
            results[label] = (p95, budget)
        return 0 if all(p <= b for p, b in results.values()) else 1
    finally:
        _swap_instance("postgres", previous)
        backend.close()
        if not (args.keep or args.reuse):
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        else:
            print(f"kept schema {schema}")
        admin.close()


if __name__ == "__main__":
    sys.exit(main())
