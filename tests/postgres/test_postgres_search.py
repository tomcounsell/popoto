"""``[PG-only]`` behaviour of M2b search (#759 plan §5 M2, §1.1).

The model-level parity is the conformance suite (gate (b): ``test_bm25_field``,
``test_embedding_field``, ``test_existence_filter``, ``test_rrf_fusion``…) and
``tests/test_backend_parity_search.py``'s seeded two-leg probe. This file
covers what has no Redis counterpart: the columns and companion tables the
compiler emits, the postings and document-length rows, a scope change moving
postings atomically, the vector arm's exact/HNSW paths and recall guard, the
pgvector extension check, ``recall()`` and its scoping, the embedding
backfill and its budget, the ``_migrated_from``/``_estimated_fields`` engine
columns, and ``garbage_collect`` being a no-op.
"""

import math
import threading
import time

import pytest

np = pytest.importorskip("numpy")

import popoto  # noqa: E402
from popoto.backends import BackendCapabilityError, get_backend  # noqa: E402
from popoto.backends.postgres import search as search_mod  # noqa: E402
from popoto.embeddings import AbstractEmbeddingProvider  # noqa: E402
from popoto.fields._tokenizer import tokenize  # noqa: E402
from popoto.fields.bm25_field import BM25Field  # noqa: E402
from popoto.fields.constants import Defaults  # noqa: E402
from popoto.fields.content_field import ContentField  # noqa: E402
from popoto.fields.embedding_field import EmbeddingField  # noqa: E402
from popoto.fields.existence_filter import (  # noqa: E402
    ExistenceFilter,
    FrequencySketch,
)


class HashProvider(AbstractEmbeddingProvider):
    """Deterministic vectors from the text; counts its calls."""

    def __init__(self, dim=8, sleep_after=None, sleep=0.0):
        self._dim = dim
        self.calls = 0
        self.sleep_after = sleep_after
        self.sleep = sleep

    def embed(self, texts, input_type=None):
        self.calls += 1
        if self.sleep_after is not None and self.calls > self.sleep_after:
            time.sleep(self.sleep)
        out = []
        for text in texts:
            rng = np.random.RandomState(sum(map(ord, text)) % (2**31))
            out.append(rng.randn(self._dim).tolist())
        return out

    @property
    def dimensions(self):
        return self._dim

    @property
    def max_batch_size(self):
        return 32


PROVIDER = HashProvider(dim=8)

#: Starve the HNSW path so it comes back short: one candidate, no iterative
#: scan, and every plan but the HNSW index scan priced out -- the shape of
#: spike-4's 0.0-recall query, made deterministic.
STARVED_HNSW = (
    "SET LOCAL hnsw.ef_search = 1; SET LOCAL hnsw.iterative_scan = off; "
    "SET LOCAL enable_seqscan = off; SET LOCAL enable_bitmapscan = off; "
    "SET LOCAL enable_sort = off; "
)


class SearchDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    project = popoto.Field(type=str, null=True)
    owner = popoto.IndexedField(type=str, null=True)
    tags = popoto.TagField()
    stamp = popoto.SortedField(type=float, default=0.0, partition_by="project")
    text = popoto.StringField(default="")
    content = BM25Field(source="text")
    embedding = EmbeddingField(source="text", provider=PROVIDER)
    bloom = ExistenceFilter(fingerprint_fn=lambda inst: inst.text)
    freq = FrequencySketch(fingerprint_fn=lambda inst: inst.text)


class ContentDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    body = ContentField()
    content = BM25Field(source="body")


def _columns(admin, schema, table):
    rows = admin.execute(
        "SELECT column_name, data_type, udt_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    ).fetchall()
    return {name: (data_type, udt) for name, data_type, udt in rows}


def _tables(admin, schema):
    rows = admin.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,)
    ).fetchall()
    return {r[0] for r in rows}


def _indexes(admin, schema, table):
    rows = admin.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = %s AND tablename = %s",
        (schema, table),
    ).fetchall()
    return dict(rows)


@pytest.fixture(autouse=True)
def _provider():
    PROVIDER.calls = 0
    PROVIDER.sleep_after = None
    yield PROVIDER


def _doc(name, text, project="p1", **kw):
    doc = SearchDoc(name=name, text=text, project=project, **kw)
    doc.save()
    return doc


# -- the schema ----------------------------------------------------------------


class _Dim1536(AbstractEmbeddingProvider):
    def embed(self, texts, input_type=None):  # pragma: no cover - never called
        return [[0.0] * 1536 for _ in texts]

    @property
    def dimensions(self):
        return 1536

    @property
    def max_batch_size(self):
        return 1


class DdlMemory(popoto.Model):
    memory_id = popoto.AutoKeyField()
    project_key = popoto.Field(type=str)
    relevance = popoto.SortedField(type=float, partition_by="project_key")
    content = popoto.StringField(default="")
    lexical = BM25Field(source="content")
    embedding = EmbeddingField(source="content", provider=_Dim1536())
    bloom = ExistenceFilter(fingerprint_fn=lambda m: m.content)
    seen = FrequencySketch(fingerprint_fn=lambda m: m.content)


def test_compiled_search_ddl_is_pinned():
    """The DDL #756's copy targets for the search fields (pure: no server).
    A change here is a schema change; it needs a migration story."""
    from popoto.backends.postgres.schema import compile_table

    DdlMemory()  # the AutoKeyField joins the spec at first instantiation
    ddl = compile_table(DdlMemory._meta.spec, "popoto").create_sql()
    t = '"popoto"."ddl_memory"'
    fk = f'REFERENCES {t} ("_pk") ON DELETE CASCADE'
    assert ddl == [
        f'CREATE TABLE {t} ("_pk" text PRIMARY KEY, "content" text, '
        '"embedding" bigint, "memory_id" text, "project_key" text, '
        '"relevance" double precision, "embedding__vec" vector(1536), '
        '"embedding__model" text, "embedding__hash" text, '
        '"_created_at" timestamptz NOT NULL DEFAULT now(), '
        '"_updated_at" timestamptz NOT NULL DEFAULT now(), '
        '"_migrated_from" jsonb, "_estimated_fields" text[])',
        f'CREATE UNIQUE INDEX IF NOT EXISTS "ddl_memory__keys__idx" ON {t} '
        '("memory_id")',
        f'CREATE INDEX IF NOT EXISTS "ddl_memory__sort__relevance__idx" ON {t} '
        '("project_key", "relevance", "_pk" COLLATE "C")',
        f'CREATE INDEX IF NOT EXISTS "ddl_memory__hnsw__embedding__idx" ON {t} '
        'USING hnsw ("embedding__vec" vector_cosine_ops)',
        f'CREATE INDEX IF NOT EXISTS "ddl_memory__unembedded__embedding__idx" ON '
        f'{t} ("_pk") WHERE "embedding__vec" IS NULL',
        f'CREATE INDEX IF NOT EXISTS "ddl_memory__model__embedding__idx" ON {t} '
        '("embedding__model")',
        'CREATE TABLE IF NOT EXISTS "popoto"."ddl_memory__bloom__tok" ('
        f'"token" text NOT NULL, "_pk" text NOT NULL {fk}, '
        'PRIMARY KEY ("token", "_pk"))',
        'CREATE INDEX IF NOT EXISTS "ddl_memory__bloom__tok__pk__idx" ON '
        '"popoto"."ddl_memory__bloom__tok" ("_pk")',
        'CREATE TABLE IF NOT EXISTS "popoto"."ddl_memory__lexical__post" ('
        f'"scope" text NOT NULL, "term" text NOT NULL, "_pk" text NOT NULL {fk}, '
        '"tf" integer NOT NULL, PRIMARY KEY ("scope", "term", "_pk"))',
        'CREATE INDEX IF NOT EXISTS "ddl_memory__lexical__post__pk__idx" ON '
        '"popoto"."ddl_memory__lexical__post" ("_pk")',
        'CREATE TABLE IF NOT EXISTS "popoto"."ddl_memory__lexical__dl" ('
        f'"_pk" text PRIMARY KEY {fk}, "scope" text NOT NULL, '
        '"len" integer NOT NULL)',
        'CREATE INDEX IF NOT EXISTS "ddl_memory__lexical__dl__scope__idx" ON '
        '"popoto"."ddl_memory__lexical__dl" ("scope") INCLUDE ("len")',
        'CREATE TABLE IF NOT EXISTS "popoto"."ddl_memory__seen__cnt" ('
        '"token" text PRIMARY KEY, "count" bigint NOT NULL)',
    ]


def test_search_columns_and_companion_tables(pg, pg_schema, admin):
    _doc("a", "kubernetes deployment guide")
    cols = _columns(admin, pg_schema.name, "search_doc")
    # Side-effect fields have no column; the embedding keeps its dimension
    # count (the value Redis stores) plus the vector and its provenance.
    for name in ("content", "bloom", "freq"):
        assert name not in cols
    assert cols["embedding"] == ("bigint", "int8")
    assert cols["embedding__vec"] == ("USER-DEFINED", "vector")
    assert cols["embedding__model"] == ("text", "text")
    assert cols["embedding__hash"] == ("text", "text")
    assert {
        "search_doc__content__post",
        "search_doc__content__dl",
        "search_doc__bloom__tok",
        "search_doc__freq__cnt",
    } <= _tables(admin, pg_schema.name)
    post = _columns(admin, pg_schema.name, "search_doc__content__post")
    assert post == {
        "scope": ("text", "text"),
        "term": ("text", "text"),
        "_pk": ("text", "text"),
        "tf": ("integer", "int4"),
    }
    idx = _indexes(admin, pg_schema.name, "search_doc")
    assert "USING hnsw (embedding__vec vector_cosine_ops)" in "".join(idx.values())
    (typmod,) = admin.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE "
        "attrelid = %s::regclass AND attname = 'embedding__vec'",
        (f'"{pg_schema.name}".search_doc',),
    ).fetchone()
    assert typmod == "vector(8)"


def test_postings_hold_raw_term_counts(pg, pg_schema, admin):
    doc = _doc("tf", "redis redis redis deployment")
    rows = admin.execute(
        f'SELECT scope, term, tf FROM "{pg_schema.name}".search_doc__content__post '
        "WHERE _pk = %s ORDER BY term",
        (doc.pk,),
    ).fetchall()
    assert rows == [("p1", "deployment", 1), ("p1", "redis", 3)]
    (length,) = admin.execute(
        f'SELECT len FROM "{pg_schema.name}".search_doc__content__dl WHERE _pk = %s',
        (doc.pk,),
    ).fetchone()
    assert length == 4  # total tokens, not unique ones (Redis's dl)


def test_delete_decrements_n_and_cascades(pg, pg_schema, admin):
    backend = get_backend(SearchDoc)
    spec = SearchDoc._meta.spec
    one = _doc("one", "document one content here")
    _doc("two", "document two content here")
    _doc("empty", "")  # no tokens: not in N, as on Redis
    # "one" is a stop word: lengths 3 ("document content here") and 4.
    assert backend.bm25_corpus(spec, "content") == (2, 3.5)
    one.delete()
    assert backend.bm25_corpus(spec, "content") == (1, 4.0)
    for table in ("search_doc__content__post", "search_doc__bloom__tok"):
        (n,) = admin.execute(
            f'SELECT count(*) FROM "{pg_schema.name}".{table} WHERE _pk = %s',
            (one.pk,),
        ).fetchone()
        assert n == 0, table


def test_scope_change_moves_postings_in_the_same_statement(pg, pg_schema, admin):
    doc = _doc("mover", "alpha beta gamma", project="p1")
    backend = get_backend(SearchDoc)
    spec = SearchDoc._meta.spec

    def in_scope(scope):
        return backend.keyword_search(
            spec, "content", ["alpha"], limit=5, scope=scope, stats="scope"
        )

    assert [r.canonical for r, _s in in_scope("p1")] == [doc.pk]
    # A partial save naming only the scope column moves the rows...
    doc.project = "p2"
    doc.save(update_fields=["project"])
    assert in_scope("p1") == []
    assert [r.canonical for r, _s in in_scope("p2")] == [doc.pk]
    scopes = admin.execute(
        f'SELECT DISTINCT scope FROM "{pg_schema.name}".search_doc__content__post'
    ).fetchall()
    assert scopes == [("p2",)]
    # ...and so does a full save. A NULL scope column is scope ''.
    doc.project = None
    doc.save()
    assert in_scope("p2") == []
    assert [r.canonical for r, _s in in_scope("")] == [doc.pk]
    assert [r.canonical for r, _s in in_scope(None)] == [doc.pk]  # unscoped


def test_update_fields_naming_the_source_reindexes(pg):
    doc = _doc("src", "original databases")
    doc.text = "networking now"
    doc.save(update_fields=["text"])
    # Redis runs only the listed fields' hooks, so its index would keep
    # "databases"; Postgres re-indexes when the source is listed.
    assert BM25Field.search(SearchDoc, "content", "databases") == []
    assert [k for k, _s in BM25Field.search(SearchDoc, "content", "networking")] == [
        doc.pk
    ]


def test_bm25_scores_match_the_lua_formula(pg):
    texts = {
        "d1": "redis cluster redis sentinel failover",
        "d2": "redis deployment production",
        "d3": "python machine learning guide guide guide",
        "d4": "cluster deployment monitoring alerting systems cluster",
    }
    for name, text in texts.items():
        _doc(name, text)
    query = "redis cluster guide"
    terms = tokenize(query)
    docs = {n: tokenize(t, unique=False) for n, t in texts.items()}
    n = len(docs)
    avgdl = sum(len(t) for t in docs.values()) / n
    k1, b = BM25Field.BM25_K1, BM25Field.BM25_B
    expected = {}
    for name, toks in docs.items():
        score = 0.0
        hit = False
        for term in terms:
            df = sum(1 for t in docs.values() if term in t)
            tf = toks.count(term)
            if not tf:
                continue
            hit = True
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1)
            score = score + idf * (
                (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * len(toks) / avgdl))
            )
        if hit:
            expected[f"SearchDoc:{name}"] = score
    got = BM25Field.search(SearchDoc, "content", query, limit=10)
    want = sorted(expected.items(), key=lambda kv: (-kv[1], kv[0].encode()))
    assert [k for k, _s in got] == [k for k, _s in want]
    for (_k, s), (_k2, e) in zip(got, want):
        assert s == float("%.14g" % e)


def test_get_idf_and_corpus_stats(pg):
    _doc("i1", "kubernetes cluster setup")
    _doc("i2", "kubernetes monitoring")
    idf = BM25Field.get_idf(SearchDoc, "content", ["kubernetes", "setup", "absent"])
    assert idf["kubernetes"] == pytest.approx(math.log((2 - 2 + 0.5) / 2.5 + 1))
    assert idf["setup"] == pytest.approx(math.log((2 - 1 + 0.5) / 1.5 + 1))
    assert idf["absent"] == pytest.approx(math.log(2.5 / 0.5 + 1))
    BM25Field.recompute_stats(SearchDoc, "content")  # a no-op: no drift to fix


def test_scope_stats_differ_from_corpus_stats(pg):
    # "rare" is rare corpus-wide but common in p2, so per-scope IDF is lower.
    for i in range(6):
        _doc(f"c{i}", f"common words {i:03d}x", project="p1")
    _doc("r1", "rare token here", project="p2")
    _doc("r2", "rare again here", project="p2")
    backend = get_backend(SearchDoc)
    spec = SearchDoc._meta.spec
    corpus = backend.keyword_search(
        spec, "content", ["rare"], limit=5, scope="p2", stats="corpus"
    )
    scoped = backend.keyword_search(
        spec, "content", ["rare"], limit=5, scope="p2", stats="scope"
    )
    assert [r for r, _ in corpus] == [r for r, _ in scoped]
    assert corpus[0][1] > scoped[0][1]


def test_content_field_is_a_text_column(pg, pg_schema, admin):
    doc = ContentDoc(name="c", body="large body about kubernetes")
    doc.save()
    (body,) = admin.execute(
        f'SELECT body FROM "{pg_schema.name}".content_doc'
    ).fetchone()
    assert body == "large body about kubernetes"  # the text, not a $CF: ref
    assert ContentDoc.query.get(name="c").body == "large body about kubernetes"
    assert [k for k, _s in BM25Field.search(ContentDoc, "content", "kubernetes")] == [
        doc.pk
    ]


# -- the vector column --------------------------------------------------------


def test_vector_is_stored_as_float32_with_provenance(pg, pg_schema, admin):
    doc = _doc("v", "vector content")
    vec, model, digest, dims = admin.execute(
        f"SELECT embedding__vec::text, embedding__model, embedding__hash, embedding "
        f'FROM "{pg_schema.name}".search_doc WHERE _pk = %s',
        (doc.pk,),
    ).fetchone()
    stored = np.array([float(x) for x in vec.strip("[]").split(",")], np.float32)
    want = np.array(PROVIDER.embed(["vector content"])[0], dtype=np.float32)
    assert np.array_equal(stored, want)
    assert model == "HashProvider:unknown"
    assert digest == search_mod._md5("vector content")
    assert dims == 8 and doc.embedding == 8


def test_hydration_never_fetches_the_vector(pg):
    _doc("h", "hydrate me")
    loaded = SearchDoc.query.get(name="h")
    assert loaded.embedding == 8
    assert "embedding__vec" not in loaded.__dict__


def test_vector_search_paths_agree_and_rank_by_cosine(pg, monkeypatch):
    for i in range(30):
        _doc(f"vs{i:02d}", f"text number {i} about things")
    backend = get_backend(SearchDoc)
    spec = SearchDoc._meta.spec
    q = PROVIDER.embed(["text number 7 about things"])[0]
    exact, info = backend._vector_arm(spec, "embedding", q, limit=10)
    assert info["path"] == "exact" and not info["guard"]
    hnsw, info = backend._vector_arm(spec, "embedding", q, limit=10, force="hnsw")
    assert info["path"] == "hnsw"
    assert [r for r, _ in exact][0].canonical == "SearchDoc:vs07"
    # The numpy oracle Redis uses: float32, pre-normalized, dot product.
    matrix, keys = EmbeddingField.load_embeddings(SearchDoc)
    qv = np.array(q, dtype=np.float32)
    sims = matrix @ (qv / np.linalg.norm(qv))
    oracle = dict(zip(keys, sims.tolist()))
    for rid, score in exact:
        assert score == pytest.approx(oracle[rid.canonical], abs=1e-6)
    if not info["guard"]:
        assert {r.canonical for r, _ in hnsw} <= set(keys)
    # Above the threshold the HNSW path is chosen on its own.
    monkeypatch.setattr(Defaults, "PG_VECTOR_EXACT_MAX", 5)
    _scored, info = backend._vector_arm(spec, "embedding", q, limit=10)
    assert info["path"] == "hnsw"


def test_recall_guard_reruns_exactly_when_hnsw_comes_back_short(pg, monkeypatch):
    for i in range(40):
        _doc(f"g{i:02d}", f"guard text {i}", owner="me" if i % 10 == 0 else "you")
    backend = get_backend(SearchDoc)
    spec = SearchDoc._meta.spec
    q = PROVIDER.embed(["guard text 3"])[0]
    monkeypatch.setattr(
        search_mod.SearchMixin, "_hnsw_prefix", staticmethod(lambda: STARVED_HNSW)
    )
    owner_me = popoto.backends.Cond("owner", popoto.backends.Op.EXACT, "me")
    exact, _ = backend._vector_arm(spec, "embedding", q, limit=10, where=owner_me)
    guarded, info = backend._vector_arm(
        spec, "embedding", q, limit=10, where=owner_me, force="hnsw"
    )
    assert info["guard"] is True
    assert guarded == exact
    assert len(exact) == len([r for r in exact if r[1] > 0])


def test_missing_vector_extension_is_a_clear_error_at_first_use(pg, monkeypatch):
    monkeypatch.setattr(
        search_mod, "VECTOR_EXTENSION_SQL", "SELECT NULL::text, false WHERE false"
    )
    pg.forget_tables()

    class NoVectorDoc(popoto.Model):
        name = popoto.UniqueKeyField()
        text = popoto.StringField(default="")
        embedding = EmbeddingField(source="text", provider=PROVIDER)

    with pytest.raises(BackendCapabilityError, match="CREATE EXTENSION vector"):
        NoVectorDoc(name="x", text="y").save()


def test_extension_off_the_search_path_is_a_clear_error(pg, monkeypatch):
    monkeypatch.setattr(
        search_mod, "VECTOR_EXTENSION_SQL", "SELECT 'elsewhere'::text, false"
    )
    pg.forget_tables()
    with pytest.raises(BackendCapabilityError, match="search_path"):
        _doc("x", "y")


def test_unknown_dimensions_compile_an_unconstrained_column_without_hnsw(
    pg, pg_schema, admin
):
    class NoProviderDoc(popoto.Model):
        name = popoto.UniqueKeyField()
        text = popoto.StringField(default="")
        embedding = EmbeddingField(source="text")

    NoProviderDoc(name="n", text="no provider").save()
    (typmod,) = admin.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE "
        "attrelid = %s::regclass AND attname = 'embedding__vec'",
        (f'"{pg_schema.name}".no_provider_doc',),
    ).fetchone()
    assert typmod == "vector"
    idx = _indexes(admin, pg_schema.name, "no_provider_doc")
    assert "hnsw" not in "".join(idx.values())


def test_gc_is_a_no_op_on_postgres(pg, tmp_path, monkeypatch):
    monkeypatch.setenv("POPOTO_CONTENT_PATH", str(tmp_path))
    SearchDoc.__embedding_garbage_collect__ = True
    try:
        emb_dir = tmp_path / ".embeddings" / "SearchDoc"
        emb_dir.mkdir(parents=True)
        stray = emb_dir / ("f" * 64 + ".npy")
        np.save(stray, np.zeros(8, dtype=np.float32))
        old = time.time() - 99999
        import os

        os.utime(stray, (old, old))
        tmp = emb_dir / "tmpOLD.npy"
        np.save(tmp, np.zeros(8, dtype=np.float32))
        os.utime(tmp, (old, old))
        _doc("gc", "anything")
        assert EmbeddingField.garbage_collect(SearchDoc) == 0
        assert EmbeddingField.sweep_stale_tempfiles(SearchDoc, 1) == 0
        assert stray.exists() and tmp.exists()
    finally:
        del SearchDoc.__embedding_garbage_collect__


# -- membership (protocol group G) --------------------------------------------


def test_membership_protocol_methods(pg):
    backend = get_backend(SearchDoc)
    spec = SearchDoc._meta.spec
    doc = _doc("m", "kubernetes cluster")
    assert backend.membership_query(spec, "bloom", ["kubernetes"], mode="any")
    assert backend.membership_query(
        spec, "bloom", ["cluster", "absent"], mode="each"
    ) == [True, False]
    backend.membership_add(spec, "freq", ["cluster", "extra"])
    assert backend.membership_query(
        spec, "freq", ["cluster", "extra", "absent"], mode="count"
    ) == [2, 1, 0]
    with pytest.raises(BackendCapabilityError, match="id="):
        backend.membership_add(spec, "bloom", ["new"])
    backend.membership_add(
        spec, "bloom", ["new"], id=popoto.backends.RecordId("SearchDoc", (), doc.pk)
    )
    assert SearchDoc.bloom.might_exist(SearchDoc, "new")
    assert 0.0 < SearchDoc.bloom.fill_ratio(SearchDoc) < 1.0


def test_fingerprint_change_replaces_the_records_tokens(pg):
    doc = _doc("fp", "alpha topic")
    doc.text = "beta topic"
    doc.save()
    assert not SearchDoc.bloom.might_exist(SearchDoc, "alpha")
    assert SearchDoc.bloom.might_exist(SearchDoc, "beta")
    # The count table counts saves, as the sketch does: "topic" twice.
    assert SearchDoc.freq.get_frequency(SearchDoc, "topic") == 2


# -- recall() [PG-only] --------------------------------------------------------


def _recall_corpus():
    _doc("r1", "redis cluster failover sentinel", project="p1", owner="alice")
    _doc("r2", "redis deployment production", project="p1", owner="bob")
    _doc("r3", "redis tuning memory", project="p2", owner="alice", tags=["ops"])
    _doc("r4", "python machine learning", project="p1", owner="alice", tags=["ml"])
    _doc("r5", "redis redis streams consumer", project="p2", owner="bob", tags=["ops"])


def test_recall_is_rrf_over_the_two_arms(pg):
    _recall_corpus()
    backend = get_backend(SearchDoc)
    spec = SearchDoc._meta.spec
    query = "redis cluster"
    # The oracle: each arm on its own, fused by RRF (k=60) in Python.
    keyword = backend.keyword_search(spec, "content", tokenize(query), limit=50)
    vector = backend.vector_search(
        spec, "embedding", PROVIDER.embed([query])[0], limit=50
    )
    fused = {}
    for ranked in (keyword, vector):
        for rank, (rid, _s) in enumerate(ranked, start=1):
            fused[rid.canonical] = fused.get(rid.canonical, 0.0) + 1.0 / (60 + rank)
    want = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0].encode()))[:3]
    results = SearchDoc.query.recall(query, limit=3)
    assert [i.pk for i, _s in results] == [k for k, _s in want]
    assert [s for _i, s in results] == pytest.approx([s for _k, s in want])
    assert all(isinstance(inst, SearchDoc) for inst, _s in results)
    assert results[0][0]._rrf_score == results[0][1]


def test_recall_scoping_composes(pg):
    _recall_corpus()
    in_p2 = {i.name for i, _ in SearchDoc.query.recall("redis", scope="p2")}
    assert in_p2 <= {"r3", "r5"} and "r3" in in_p2
    alice = SearchDoc.query.recall("redis", filters={"owner": "alice"})
    assert {i.owner for i, _ in alice} == {"alice"}
    ops = SearchDoc.query.recall("redis", tags=["ops"])
    assert {i.name for i, _ in ops} <= {"r3", "r5"}
    both = SearchDoc.query.recall(
        "redis", scope="p2", filters={"owner": ["bob"]}, tags=["ops"]
    )
    assert [i.name for i, _ in both] == ["r5"]
    nothing = SearchDoc.query.recall("redis", scope="p3")
    assert nothing == []


def test_recall_per_scope_stats_by_default_corpus_on_request(pg):
    _recall_corpus()
    backend = get_backend(SearchDoc)
    spec = SearchDoc._meta.spec
    # Only the BM25 arm, so the score is a function of its ranks alone; the
    # statistics show through keyword_search's own scores.
    scoped = SearchDoc.query.recall("redis", scope="p2", weights={"vector": 0})
    corpus = SearchDoc.query.recall(
        "redis", scope="p2", weights={"vector": 0}, bm25_stats="corpus"
    )
    assert [i.name for i, _ in scoped] == [i.name for i, _ in corpus]
    by_scope = backend.keyword_search(
        spec, "content", ["redis"], limit=5, scope="p2", stats="scope"
    )
    by_corpus = backend.keyword_search(
        spec, "content", ["redis"], limit=5, scope="p2", stats="corpus"
    )
    assert by_scope[0][1] != by_corpus[0][1]
    with pytest.raises(ValueError, match="bm25_stats"):
        SearchDoc.query.recall("redis", bm25_stats="global")


def test_recall_weights_drop_and_shift_arms(pg):
    _recall_corpus()
    lexical = SearchDoc.query.recall("redis cluster", weights={"vector": 0})
    assert lexical[0][1] == pytest.approx(1 / 61)
    vector_only = SearchDoc.query.recall("redis cluster", weights={"bm25": 0})
    assert vector_only and vector_only[0][1] == pytest.approx(1 / 61)
    assert SearchDoc.query.recall("redis", weights={"bm25": 0, "vector": 0}) == []


def test_recall_vector_arm_hnsw_path_and_guard(pg, monkeypatch):
    for i in range(25):
        _doc(f"h{i:02d}", f"hnsw text {i}", owner="me" if i % 5 == 0 else "you")
    backend = get_backend(SearchDoc)
    exact = SearchDoc.query.recall("hnsw text 5", filters={"owner": "me"})
    assert backend._last_recall["vector"] == "exact"
    assert backend._last_recall["guard"] is False
    assert backend._last_recall["embedded"] == 5
    monkeypatch.setattr(Defaults, "PG_VECTOR_EXACT_MAX", 0)
    monkeypatch.setattr(
        search_mod.SearchMixin, "_hnsw_prefix", staticmethod(lambda: STARVED_HNSW)
    )
    guarded = SearchDoc.query.recall("hnsw text 5", filters={"owner": "me"})
    assert backend._last_recall["vector"] == "hnsw"
    assert backend._last_recall["guard"] is True
    # The guard's exact re-run ranks exactly as the exact path did, so the
    # fused result is the same.
    assert [(i.name, s) for i, s in guarded] == [(i.name, s) for i, s in exact]


def test_hnsw_statement_uses_the_index_under_a_wide_filter(
    pg, pg_schema, admin, monkeypatch
):
    """The planner prices a filtered HNSW scan above reading the scope and
    sorting once the filter keeps most rows; _hnsw_prefix's settings leave
    the index as the only ordered path. EXPLAIN the very statement
    _vector_arm sends on the HNSW path."""
    for i in range(40):
        _doc(f"x{i:02d}", f"explain text {i}", owner="me" if i % 10 else "you")
    admin.execute(f'ANALYZE "{pg_schema.name}".search_doc')
    backend = get_backend(SearchDoc)
    spec = SearchDoc._meta.spec
    sent = []
    real_run = backend._run

    def spy(sql, params=(), **kw):
        sent.append((sql, list(params)))
        return real_run(sql, params, **kw)

    monkeypatch.setattr(backend, "_run", spy)
    owner_me = popoto.backends.Cond("owner", popoto.backends.Op.EXACT, "me")
    backend._vector_arm(
        spec,
        "embedding",
        PROVIDER.embed(["explain"])[0],
        limit=10,
        where=owner_me,
        force="hnsw",
    )
    sql, params = next((s, p) for s, p in sent if "SET LOCAL hnsw" in s)
    prefix, _, body = sql.partition("SELECT ")
    import psycopg

    cur = psycopg.ClientCursor(admin)
    cur.execute("BEGIN")
    try:
        cur.execute(prefix)
        cur.execute("EXPLAIN SELECT " + body, params)
        plan = "\n".join(r[0] for r in cur.fetchall())
    finally:
        cur.execute("ROLLBACK")
    assert "search_doc__hnsw__embedding__idx" in plan
    assert "Seq Scan" not in plan and "Bitmap" not in plan


def test_recall_is_postgres_only(pg):
    class RedisDoc(popoto.Model):
        name = popoto.UniqueKeyField()
        text = popoto.StringField(default="")
        content = BM25Field(source="text")

        class Meta:
            backend = "redis"

    with pytest.raises(BackendCapabilityError, match="Postgres-only"):
        RedisDoc.query.recall("anything")


def test_recall_validates_its_scoping_arguments(pg):
    with pytest.raises(ValueError, match="KeyField/IndexedField"):
        SearchDoc.query.recall("x", filters={"text": "y"})
    with pytest.raises(ValueError, match="scope"):
        SearchDoc.query.recall("x", scope={"owner": "y"})
    with pytest.raises(ValueError, match="tags_mode"):
        SearchDoc.query.recall("x", tags=["a"], tags_mode="most")
    with pytest.raises(ValueError, match="no scope"):
        ContentDoc.query.recall("x", scope="p1")


# -- the embedding backfill (#758 D7) --------------------------------------------


def test_backfill_embeds_rows_missing_a_vector(pg, pg_schema, admin):
    field = SearchDoc._meta.fields["embedding"]
    field._provider = None  # these saves get no vector
    try:
        for i in range(3):
            _doc(f"late{i}", f"late text {i}")
    finally:
        field._provider = PROVIDER
    (missing,) = admin.execute(
        f'SELECT count(*) FROM "{pg_schema.name}".search_doc '
        "WHERE embedding__vec IS NULL"
    ).fetchone()
    assert missing == 3
    _doc("trigger", "a save whose own embedding succeeds")
    (missing,) = admin.execute(
        f'SELECT count(*) FROM "{pg_schema.name}".search_doc '
        "WHERE embedding__vec IS NULL"
    ).fetchone()
    assert missing == 0
    assert SearchDoc.query.get(name="late1").embedding == 8


def test_backfill_is_bounded_by_its_batch_and_scope(pg, pg_schema, admin, monkeypatch):
    monkeypatch.setattr(Defaults, "PG_BACKFILL_BATCH", 2)
    field = SearchDoc._meta.fields["embedding"]
    field._provider = None
    try:
        for i in range(4):
            _doc(f"b{i}", f"batch text {i}", project="p1")
        _doc("other", "other scope", project="p9")
    finally:
        field._provider = PROVIDER
    _doc("t", "trigger text", project="p1")
    rows = dict(
        admin.execute(
            f'SELECT name, embedding__vec IS NOT NULL FROM "{pg_schema.name}".search_doc'
        ).fetchall()
    )
    assert sum(rows[f"b{i}"] for i in range(4)) == 2  # the batch bound
    assert rows["other"] is False  # another scope is left alone


def test_a_sleeping_provider_cannot_push_save_past_the_budget(pg, monkeypatch):
    monkeypatch.setattr(Defaults, "PG_BACKFILL_BUDGET_SECONDS", 0.3)
    field = SearchDoc._meta.fields["embedding"]
    field._provider = None
    try:
        _doc("slow-missing", "waiting for a vector")
    finally:
        field._provider = PROVIDER
    # The save's own embed is call 1 and fast; the backfill's (call 2) sleeps.
    PROVIDER.calls = 0
    PROVIDER.sleep_after = 1
    PROVIDER.sleep = 3.0
    start = time.monotonic()
    _doc("fast", "this save must return within the budget")
    elapsed = time.monotonic() - start
    assert elapsed < 0.3 + 1.0, f"save took {elapsed:.2f}s"
    assert SearchDoc.query.get(name="fast").embedding == 8
    # The abandoned call still holds the backfill slot: a save meanwhile
    # skips backfill rather than start a second thread behind it.
    assert search_mod._backfill_busy.locked()
    PROVIDER.sleep_after = None
    # Let the abandoned call finish before the next test needs the slot.
    assert search_mod._backfill_busy.acquire(timeout=10)
    search_mod._backfill_busy.release()


def test_backfill_never_overwrites_a_concurrent_edit(pg, pg_schema, admin):
    field = SearchDoc._meta.fields["embedding"]
    field._provider = None
    try:
        _doc("race", "old text")
    finally:
        field._provider = PROVIDER
    real_embed = PROVIDER.embed
    seen = []

    def edit_then_embed(texts, input_type=None):
        seen.append(texts)
        if texts == ["old text"]:
            admin.execute(
                f'UPDATE "{pg_schema.name}".search_doc SET text = %s WHERE name = %s',
                ("edited meanwhile", "race"),
            )
        return real_embed(texts, input_type)

    PROVIDER.embed = edit_then_embed
    try:
        _doc("trigger2", "triggering save")
    finally:
        del PROVIDER.embed
    (vec,) = admin.execute(
        f'SELECT embedding__vec FROM "{pg_schema.name}".search_doc WHERE name = %s',
        ("race",),
    ).fetchone()
    assert ["old text"] in seen  # the backfill did embed it...
    assert vec is None  # ...and the hash guard refused the stale vector


def test_backfill_errors_are_swallowed(pg):
    field = SearchDoc._meta.fields["embedding"]
    field._provider = None
    try:
        _doc("e1", "needs a vector")
    finally:
        field._provider = PROVIDER

    real_embed = PROVIDER.embed
    calls = []

    def fail_on_backfill(texts, input_type=None):
        calls.append(texts)
        if len(calls) > 1:
            raise RuntimeError("provider down")
        return real_embed(texts, input_type)

    PROVIDER.embed = fail_on_backfill
    try:
        _doc("e2", "save still succeeds")
    finally:
        del PROVIDER.embed
    assert len(calls) == 2
    assert SearchDoc.query.get(name="e2") is not None


# -- #756's engine columns -------------------------------------------------------


def test_native_writes_clear_migrated_from_and_keep_estimated_fields(
    pg, pg_schema, admin
):
    doc = _doc("mig", "imported text")
    admin.execute(
        f'UPDATE "{pg_schema.name}".search_doc SET _migrated_from = %s, '
        "_estimated_fields = %s WHERE _pk = %s",
        ('{"machine": "m1", "run_id": "r"}', ["stamp"], doc.pk),
    )
    doc.text = "natively edited"
    doc.save()
    migrated, estimated = admin.execute(
        f'SELECT _migrated_from, _estimated_fields FROM "{pg_schema.name}".search_doc '
        "WHERE _pk = %s",
        (doc.pk,),
    ).fetchone()
    assert migrated is None
    assert estimated == ["stamp"]
