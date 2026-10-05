"""``Meta.ttl`` and ``popoto.batch()`` on Postgres (#759 plan §5 M5).

The conformance half -- the same ``test_meta_ttl.py``, ``test_batch.py`` and
``test_atomic_save.py`` code on both legs -- is gate (b); the seeded two-leg
probe is ``scripts/probe_ttl_parity.py`` (a slice runs at the end of this
file). This file covers what has no Redis counterpart, or whose Postgres
mechanism is the point:

* the ``_expires_at`` column, its partial index, and a TTL-free model's
  table and plans staying TTL-free;
* the M5 exit criterion: an expired row is invisible to every read before
  the reaper has run, and gone (side rows too) after a later write;
* read-path coverage: one expired record against every public read API;
* the reaper: bounded, backlog-aware, never waiting on a caller's locks,
  after a ``transaction()`` commits and not after a rollback;
* ``popoto.batch()`` on a Postgres-bound model: one transaction, atomic,
  and the one-backend-per-batch refusal.

*Now* is frozen with :func:`popoto.backends.postgres.ttl.frozen_clock`, the
only controllable clock (the server's ``statement_timestamp()`` otherwise).
"""

import importlib.util
import threading
import time
import zlib
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

import popoto  # noqa: E402
from popoto import (  # noqa: E402
    AccessTrackerMixin,
    ConfidenceField,
    DecayingSortedField,
    SupersessionProtocol,
    ValidityField,
)
from popoto.backends import (  # noqa: E402
    BackendCapabilityError,
    BackendError,
    RecordId,
    get_backend,
)
from popoto.backends.postgres import ttl as ttl_mod  # noqa: E402
from popoto.backends.postgres.ttl import frozen_clock  # noqa: E402
from popoto.embeddings import AbstractEmbeddingProvider  # noqa: E402
from popoto.exceptions import ModelException  # noqa: E402
from popoto.fields.bm25_field import BM25Field  # noqa: E402
from popoto.fields.constants import Defaults  # noqa: E402
from popoto.fields.embedding_field import EmbeddingField  # noqa: E402
from popoto.fields.existence_filter import ExistenceFilter  # noqa: E402
from popoto.recipes.context_assembler import ContextAssembler  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "scripts" / "probe_ttl_parity.py"

T0 = 1_900_000_000.0


class HashProvider(AbstractEmbeddingProvider):
    def __init__(self, dim=8):
        self._dim = dim

    def embed(self, texts, input_type=None):
        return [
            np.random.RandomState(zlib.crc32(t.encode())).randn(self._dim).tolist()
            for t in texts
        ]

    @property
    def dimensions(self):
        return self._dim

    @property
    def max_batch_size(self):
        return 32


PROVIDER = HashProvider()


class TtlNote(popoto.Model):
    name = popoto.KeyField()
    value = popoto.Field(type=str, null=True)
    rank = popoto.SortedField(type=float, default=0.0)
    hits = popoto.IntField(default=0)

    class Meta:
        ttl = 60


class TtlCoded(popoto.Model):
    name = popoto.KeyField()
    code = popoto.UniqueField(type=str)

    class Meta:
        ttl = 60


class PlainNote(popoto.Model):
    name = popoto.KeyField()
    value = popoto.Field(type=str, null=True)


class TtlMemory(AccessTrackerMixin, popoto.Model):
    """Every read surface M2-M3 built, on one Meta.ttl model."""

    project = popoto.KeyField()
    name = popoto.KeyField()
    owner = popoto.IndexedField(type=str, null=True)
    tags = popoto.TagField()
    text = popoto.StringField(default="")
    importance = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(
        partition_by="project", base_score_field="importance"
    )
    confidence = ConfidenceField()
    lexical = BM25Field(source="text")
    embedding = EmbeddingField(source="text", provider=PROVIDER)
    bloom = ExistenceFilter(fingerprint_fn=lambda m: m.text)

    class Meta:
        ttl = 3600


class TtlFact(popoto.Model):
    name = popoto.KeyField()
    validity = ValidityField()

    class Meta:
        ttl = 3600


def _table_rows(admin, pg, model):
    from popoto.backends.postgres.schema import table_name_for

    return {
        row[0]
        for row in admin.execute(
            f'SELECT "_pk" FROM "{pg.schema}"."{table_name_for(model.__name__)}"'
        ).fetchall()
    }


def _count(admin, pg, table, where="TRUE", params=()):
    return admin.execute(
        f'SELECT count(*) FROM "{pg.schema}"."{table}" WHERE {where}', params
    ).fetchone()[0]


@pytest.fixture(autouse=True)
def _reaper_defaults(monkeypatch):
    monkeypatch.setattr(Defaults, "PG_REAPER_BATCH", 100)
    monkeypatch.setattr(Defaults, "PG_REAPER_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr(Defaults, "PG_REAPER_LOCK_TIMEOUT_MS", 50)


# -- schema -------------------------------------------------------------------


def test_a_ttl_model_has_the_expiry_column_and_a_partial_index(pg, admin):
    with frozen_clock(T0):
        TtlNote.create(name="a")
        PlainNote.create(name="a")
    cols = {
        r[0]: r[1]
        for r in admin.execute(
            "SELECT table_name, string_agg(column_name, ',') FROM "
            "information_schema.columns WHERE table_schema = %s AND "
            "column_name = '_expires_at' GROUP BY table_name",
            (pg.schema,),
        ).fetchall()
    }
    assert set(cols) == {"ttl_note"}, "only the Meta.ttl model gets the column"
    (indexdef,) = admin.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND "
        "indexname = 'ttl_note__expires__idx'",
        (pg.schema,),
    ).fetchone()
    assert "WHERE (_expires_at IS NOT NULL)" in indexdef


def test_a_ttl_free_model_keeps_ttl_free_sql(pg, monkeypatch):
    """No `_expires_at` anywhere in what a model without Meta.ttl sends."""
    sent = []
    backend = get_backend(PlainNote)
    original = backend._run

    def spy(sql, *a, **kw):
        sent.append(sql)
        return original(sql, *a, **kw)

    monkeypatch.setattr(backend, "_run", spy)
    note = PlainNote.create(name="a", value="x")
    PlainNote.query.get(name="a")
    PlainNote.query.filter(value="x")
    PlainNote.query.count()
    PlainNote.exists(name="a")
    note.delete()
    assert sent and not [s for s in sent if "_expires_at" in s]


# -- writes -------------------------------------------------------------------


def _expires(admin, pg, key):
    return admin.execute(
        f'SELECT "_expires_at" FROM "{pg.schema}"."ttl_note" WHERE "_pk" = %s',
        (key,),
    ).fetchone()[0]


def test_save_writes_now_plus_ttl_and_a_resave_refreshes_it(pg, admin):
    with frozen_clock(T0):
        note = TtlNote.create(name="a")
    assert _expires(admin, pg, note.db_key.redis_key) == T0 + 60
    with frozen_clock(T0 + 30):
        note.value = "b"
        note.save()
    assert _expires(admin, pg, note.db_key.redis_key) == T0 + 90
    with frozen_clock(T0 + 31):
        note._ttl = 5
        note.save(update_fields=["value"])  # a partial save refreshes it too
    assert _expires(admin, pg, note.db_key.redis_key) == T0 + 36


def test_expire_at_is_whole_seconds_and_no_ttl_keeps_the_stored_expiry(pg, admin):
    import datetime

    at = datetime.datetime.fromtimestamp(T0 + 100.75, tz=datetime.timezone.utc)
    with frozen_clock(T0):
        note = TtlNote(name="a")
        note._ttl = None
        note._expire_at = at
        note.save()
        # EXPIREAT gets int(timestamp()): the fraction is dropped, as on Redis.
        assert _expires(admin, pg, note.db_key.redis_key) == T0 + 100
        note._expire_at = None
        note.value = "changed"
        note.save()  # neither _ttl nor _expire_at: HSET leaves a TTL alone
        assert _expires(admin, pg, note.db_key.redis_key) == T0 + 100
        fresh = TtlNote(name="never")
        fresh._ttl = None
        fresh.save()
        assert _expires(admin, pg, fresh.db_key.redis_key) is None


def test_an_instance_ttl_needs_meta_ttl(pg, admin):
    note = PlainNote(name="a")
    note._ttl = 10
    with pytest.raises(BackendCapabilityError, match="needs Meta.ttl"):
        note.save()
    note._ttl = None
    note._expire_at = __import__("datetime").datetime(2030, 1, 1)
    with pytest.raises(BackendCapabilityError, match="needs Meta.ttl"):
        note.save()
    assert PlainNote.query.count() == 0


def test_a_ttl_expire_refuses_is_refused_before_writing(pg):
    note = TtlNote(name="a")
    note._ttl = 1.5
    with pytest.raises(ModelException, match="value is not an integer"):
        note.save()
    assert TtlNote.query.count() == 0


def test_a_zero_or_negative_ttl_expires_at_once(pg):
    """EXPIRE 0 / a negative TTL deletes the key on Redis."""
    with frozen_clock(T0):
        for name, ttl in (("zero", 0), ("neg", -5)):
            note = TtlNote(name=name)
            note._ttl = ttl
            note.save()
        assert TtlNote.query.get(name="zero") is None
        assert TtlNote.query.get(name="neg") is None
        assert TtlNote.query.count() == 0


# -- the M5 exit criterion ------------------------------------------------------


def test_an_expired_row_is_invisible_before_the_reaper_and_gone_after_a_write(
    pg, admin
):
    """Plan §5 M5 exit criterion. The row is still in the table (no reaper
    has run) while every read already misses it; the next write's reaper
    deletes it, side rows included."""
    with frozen_clock(T0):
        doomed = TtlMemory(project="p", name="doomed", text="alpha beta gamma")
        doomed._ttl = 10
        doomed.save()
        TtlMemory(project="p", name="keeper", text="alpha delta").save()
    key = doomed.db_key.redis_key
    with frozen_clock(T0 + 11):
        assert TtlMemory.query.get(project="p", name="doomed") is None
        assert [m.name for m in TtlMemory.query.filter(project="p")] == ["keeper"]
        assert key in _table_rows(admin, pg, TtlMemory), (
            "the reaper has not run, so the row must still be stored: the read "
            "filter alone hid it"
        )
        assert _count(admin, pg, "ttl_memory__lexical__post", '"_pk" = %s', (key,))
        TtlMemory(project="q", name="other", text="unrelated").save()  # a write
        assert key not in _table_rows(admin, pg, TtlMemory)
        for side in ("ttl_memory__lexical__post", "ttl_memory__lexical__dl"):
            assert _count(admin, pg, side, '"_pk" = %s', (key,)) == 0
        assert (
            _count(admin, pg, "ttl_memory__embedding__vec", '"_pk" = %s', (key,)) == 0
        )
        assert _count(admin, pg, "ttl_memory__bloom__tok", '"_pk" = %s', (key,)) == 0


# -- read-path coverage ----------------------------------------------------------


def _names(rows):
    return {getattr(r, "name", None) or r for r in rows}


def _every_read(lk, gk):
    """``{api: names it returned}`` for every public read of ``TtlMemory``,
    over a corpus of two records named ``live`` and ``gone``. Key-returning
    reads are mapped back to names; boolean reads report the names they
    found."""
    by_key = {lk: "live", gk: "gone"}

    def keys(ks):
        return {by_key[k.decode() if isinstance(k, bytes) else k] for k in ks}

    found = lambda pairs: {n for n, ok in pairs if ok}  # noqa: E731
    return {
        "get": found(
            (n, TtlMemory.query.get(project="p", name=n) is not None)
            for n in ("live", "gone")
        ),
        "get_many": _names(TtlMemory.query.get_many([lk, gk], skip_none=True)),
        "filter": _names(TtlMemory.query.filter(project="p")),
        "all": _names(TtlMemory.query.all()),
        "count": (
            {"live", "gone"}
            if TtlMemory.query.count(project="p") == 2
            else {"live"} if TtlMemory.query.count(project="p") == 1 else set()
        ),
        "keys": keys(TtlMemory.query.keys()),
        "exists": found((n, TtlMemory.exists(redis_key=k)) for k, n in by_key.items()),
        "indexed": _names(TtlMemory.query.filter(owner="o")),
        "tags": _names(TtlMemory.query.filter(tags__any=["t"])),
        "values": {
            d["name"] for d in TtlMemory.query.filter(project="p", values=("name",))
        },
        "load_fields": found(
            (n, bool(TtlMemory.load_fields(k, "text"))) for k, n in by_key.items()
        ),
        "top_by_decay": _names(TtlMemory.query.filter(project="p").top_by_decay(n=10)),
        "composite_score": _names(
            TtlMemory.query.filter(project="p").composite_score(
                {"relevance": 0.5, "confidence": 0.5}
            )
        ),
        "top_by_relevance": {
            m.name for m, _s in TtlMemory.query.top_by_relevance(scope="p")
        },
        "bm25_search": keys(
            k for k, _s in BM25Field.search(TtlMemory, "lexical", "kubernetes")
        ),
        "keyword_search": _names(TtlMemory.query.keyword_search("kubernetes")),
        "semantic_search": _names(
            TtlMemory.query.semantic_search("kubernetes runbook")
        ),
        "load_embeddings": keys(EmbeddingField.load_embeddings(TtlMemory)[1]),
        "recall": {m.name for m, _s in TtlMemory.query.recall("kubernetes")},
        "recall_scoped": {
            m.name for m, _s in TtlMemory.query.recall("kubernetes", scope="p")
        },
        "confidence_state": found(
            # A missing record reads as the seed (no evidence).
            (n, ConfidenceField.get_confidence_data(m, "confidence")["evidence_count"])
            for n, m in (
                ("live", TtlMemory(project="p", name="live")),
                ("gone", TtlMemory(project="p", name="gone")),
            )
        ),
        "assembler": _names(
            ContextAssembler(TtlMemory, score_weights={"relevance": 1.0}, max_items=10)
            .assemble({"topic": "kubernetes"}, partition_filters={"project": "p"})
            .records
        ),
    }


def test_every_public_read_misses_an_expired_record(pg, admin):
    """One record that never expires and one that does, with the same text,
    partition, owner and tags, so every read that finds one finds the other.
    Before the expiry each read returns both (the check is not vacuous);
    after it each returns only the live one -- while the expired row is
    still stored, so the read filter did it, not the reaper."""
    with frozen_clock(T0):
        for name, ttl in (("live", None), ("gone", 10)):
            mem = TtlMemory(
                project="p", name=name, owner="o", tags=["t"], text="kubernetes runbook"
            )
            mem._ttl = ttl
            mem.save()
            ConfidenceField.update_confidence(mem, "confidence", 0.9)
    lk = TtlMemory(project="p", name="live").db_key.redis_key
    gk = TtlMemory(project="p", name="gone").db_key.redis_key
    with frozen_clock(T0 + 5):
        before = _every_read(lk, gk)
    assert {api: seen for api, seen in before.items() if seen != {"live", "gone"}} == {}
    with frozen_clock(T0 + 11):
        after = _every_read(lk, gk)
        assert {api: seen for api, seen in after.items() if seen != {"live"}} == {}
        backend = get_backend(TtlMemory)
        # Corpus statistics leave the expired document out, and its
        # confidence cannot be updated: it is no record.
        assert backend.bm25_corpus(TtlMemory._meta.spec, "lexical")[0] == 1
        with pytest.raises(TypeError):
            ConfidenceField.update_confidence(
                TtlMemory(project="p", name="gone"), "confidence", 0.1
            )
        assert gk in _table_rows(admin, pg, TtlMemory)
        # Membership: the live record still holds the text.
        assert TtlMemory.bloom.might_exist(TtlMemory, "kubernetes runbook")
        live = TtlMemory.query.get(project="p", name="live")
        live._ttl = 1
        live.save()
    with frozen_clock(T0 + 13):
        # Both expired: the exact membership table forgets the text.
        assert not TtlMemory.bloom.might_exist(TtlMemory, "kubernetes runbook")
        assert TtlMemory.query.count() == 0
    assert len(before) >= 20, "the coverage list shrank"


def test_validity_reads_miss_an_expired_record(pg):
    with frozen_clock(T0):
        old = TtlFact(name="old")
        old._ttl = 10
        old.save()
        new = TtlFact(name="new")
        new._ttl = None
        new.save()
        SupersessionProtocol.supersede(old, identity_key=("u", "plan"))
        SupersessionProtocol.supersede(new, identity_key=("u", "plan"))
        assert [f.name for f in SupersessionProtocol.chain(new)] == ["old", "new"]
    with frozen_clock(T0 + 11):
        assert [f.name for f in SupersessionProtocol.chain(new)] == ["new"]
        assert ValidityField.resolve_excluded_keys(TtlFact, "validity") == set()
        assert {f.name for f in TtlFact.query.filter(validity__as_of=T0 + 11)} == {
            "new"
        }


# -- a write to an expired record -------------------------------------------------


def test_a_save_over_an_expired_key_writes_a_fresh_record(pg, admin):
    with frozen_clock(T0):
        first = TtlMemory(project="p", name="x", text="first words")
        first._ttl = 10
        first.save()
        ConfidenceField.update_confidence(first, "confidence", 1.0)
    with frozen_clock(T0 + 11):
        again = TtlMemory(project="p", name="x", text="second")
        assert again.save() > 0, "HSET on an expired key creates the hash anew"
        key = again.db_key.redis_key
        # The expired record's postings went with it; its confidence state
        # starts at the seed again.
        terms = {
            r[0]
            for r in admin.execute(
                f'SELECT "term" FROM "{pg.schema}"."ttl_memory__lexical__post" '
                'WHERE "_pk" = %s',
                (key,),
            ).fetchall()
        }
        assert terms == {"second"}
        assert ConfidenceField.get_confidence(again, "confidence") == 0.5


def test_record_writes_to_an_expired_record_see_no_record(pg):
    with frozen_clock(T0):
        note = TtlNote(name="a", hits=1)
        note._ttl = 10
        note.save()
    with frozen_clock(T0 + 11):
        assert note.delete() is False, "DEL of an expired key reports 0"
    with frozen_clock(T0):
        note = TtlNote(name="b", hits=1)
        note._ttl = 10
        note.save()
    with frozen_clock(T0 + 11):
        with pytest.raises(ModelException, match="no longer exists"):
            note.atomic_increment("hits", 1)


def test_ttl_remaining_reads_like_redis_ttl(pg):
    with frozen_clock(T0):
        note = TtlNote.create(name="a")
        forever = TtlNote(name="b")
        forever._ttl = None
        forever.save()
    backend = get_backend(TtlNote)
    spec = TtlNote._meta.spec
    ids = [
        RecordId.from_key("TtlNote", k)
        for k in (note.db_key.redis_key, forever.db_key.redis_key, "TtlNote:zz")
    ]
    with frozen_clock(T0 + 10.4):
        assert backend.ttl_remaining(spec, ids) == [50, -1, -2]
    with frozen_clock(T0 + 10.6):
        assert backend.ttl_remaining(spec, ids)[0] == 49  # (ms + 500) // 1000
    with frozen_clock(T0 + 61):
        assert backend.ttl_remaining(spec, ids) == [-2, -1, -2]


# -- the reaper ---------------------------------------------------------------------


def _make_expired(n, *, at=T0, ttl=10, prefix="e"):
    with frozen_clock(at):
        for i in range(n):
            note = TtlNote(name=f"{prefix}{i:04d}")
            note._ttl = ttl
            note.save()


def test_the_reaper_is_bounded_and_drains_a_backlog_one_batch_per_write(
    pg, admin, monkeypatch
):
    monkeypatch.setattr(Defaults, "PG_REAPER_BATCH", 5)
    _make_expired(12)
    with frozen_clock(T0 + 20):
        assert _count(admin, pg, "ttl_note") == 12
        TtlNote.create(name="w1")  # due (interval passed): reaps 5
        assert _count(admin, pg, "ttl_note") == 13 - 5
        TtlNote.create(name="w2")  # a full batch was a backlog: due again
        assert _count(admin, pg, "ttl_note") == 14 - 10
        TtlNote.create(name="w3")  # 2 left, deleted; not a full batch
        assert _count(admin, pg, "ttl_note") == 3
        _make_expired(3, at=T0 + 20, ttl=0, prefix="z")  # expire at once
        # The last run was not a backlog, so the next one waits the interval.
        assert _count(admin, pg, "ttl_note") == 6
    with frozen_clock(T0 + 21.5):
        TtlNote.create(name="w4")
        assert _count(admin, pg, "ttl_note") == 4


def test_the_reaper_skips_a_record_a_writer_holds_and_never_waits(pg, admin):
    _make_expired(3)
    held = TtlNote(name="e0001").db_key.redis_key
    ts = get_backend(TtlNote)._table(TtlNote._meta.spec)
    other = pg_conn = popoto.backends.postgres._pool_for(get_backend(TtlNote).dsn)
    del other
    with pg_conn.connection() as conn:
        conn.autocommit = False
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"popoto:rec:{ts.qualified}:{held}"],
        )
        with frozen_clock(T0 + 20):
            started = time.perf_counter()
            deleted = ttl_mod.reap(get_backend(TtlNote), ts, force=True)
            elapsed = time.perf_counter() - started
        conn.rollback()
    assert sorted(deleted) == sorted(
        TtlNote(name=n).db_key.redis_key for n in ("e0000", "e0002")
    )
    assert elapsed < 0.5, f"the reaper waited {elapsed:.3f}s on a held record"
    with frozen_clock(T0 + 20):
        assert ttl_mod.reap(get_backend(TtlNote), ts, force=True) == [held]


def test_the_reaper_gives_up_rather_than_wait_on_a_row_lock(pg, monkeypatch):
    """A row locked by something that skipped the record-key lock (here, a
    raw FOR UPDATE) is passed over by SKIP LOCKED; the run returns at once."""
    _make_expired(2)
    backend = get_backend(TtlNote)
    ts = backend._table(TtlNote._meta.spec)
    pool = popoto.backends.postgres._pool_for(backend.dsn)
    with pool.connection() as conn:
        conn.autocommit = False
        conn.execute(
            f'SELECT 1 FROM {ts.qualified} WHERE "_pk" = %s FOR UPDATE',
            [TtlNote(name="e0000").db_key.redis_key],
        )
        with frozen_clock(T0 + 20):
            started = time.perf_counter()
            deleted = ttl_mod.reap(backend, ts, force=True)
            elapsed = time.perf_counter() - started
        conn.rollback()
    assert deleted == [TtlNote(name="e0001").db_key.redis_key]
    assert elapsed < 0.5


def test_the_reaper_runs_after_a_transaction_commits_and_not_after_a_rollback(
    pg, admin
):
    _make_expired(2)
    backend = get_backend(TtlNote)
    with frozen_clock(T0 + 20):
        with pytest.raises(RuntimeError):
            with backend.transaction() as uow:
                TtlNote(name="tx1").save(pipeline=uow)
                raise RuntimeError("roll it back")
        assert _count(admin, pg, "ttl_note") == 2, "a rollback reaps nothing"
        with backend.transaction() as uow:
            TtlNote(name="tx2").save(pipeline=uow)
            # Inside the transaction nothing has been reaped yet.
            assert _count(admin, pg, "ttl_note") == 2
        assert _count(admin, pg, "ttl_note") == 1


def test_concurrent_writers_and_reapers_never_deadlock(pg, monkeypatch):
    """Four threads saving, re-saving and deleting the same few TTL records
    -- half of them expiring at once -- with the reaper due on every write:
    no write fails (a deadlock would surface as BackendRetryableError once
    the retries were spent), and nothing expired survives a final reap."""
    monkeypatch.setattr(Defaults, "PG_REAPER_INTERVAL_SECONDS", 0.0)
    errors = []

    def worker(seed):
        rng = __import__("random").Random(seed)
        try:
            for i in range(60):
                name = f"k{rng.randrange(6)}"
                if rng.random() < 0.2:
                    TtlMemory(project="p", name=name).delete()
                    continue
                mem = TtlMemory(project="p", name=name, text=f"words {i} {seed}")
                mem._ttl = rng.choice([0, 0, 3600])
                mem.save()
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    backend = get_backend(TtlMemory)
    ts = backend._table(TtlMemory._meta.spec)
    ttl_mod.reap(backend, ts, force=True)
    rows, _ = backend._run(
        f'SELECT count(*) FROM {ts.qualified} WHERE "_expires_at" <= '
        "extract(epoch from statement_timestamp())"
    )
    assert rows[0][0] == 0


def test_the_reaper_never_fails_the_write(pg, monkeypatch, caplog):
    _make_expired(1)

    def boom(*a, **kw):
        raise RuntimeError("pool exploded")

    backend = get_backend(TtlNote)
    ts = backend._table(TtlNote._meta.spec)
    health = backend.health.as_dict()
    # reap() resolves the pool when it runs; break it for this one call.
    monkeypatch.setattr(popoto.backends.postgres, "_pool_for", boom)
    with frozen_clock(T0 + 20):
        with caplog.at_level("WARNING", logger="POPOTO.postgres"):
            assert ttl_mod.reap(backend, ts, force=True) == []
    assert "reaper skipped" in caplog.text
    assert backend.health.as_dict() == health, "a reaper run is not the caller's"


# -- popoto.batch() --------------------------------------------------------------


def test_a_batch_is_one_transaction_committed_by_execute(pg):
    pipe = popoto.batch()
    a = TtlNote(name="a").save(pipeline=pipe)
    assert a is pipe, "save hands the batch back, as a queued save does"
    TtlNote(name="b").save(pipeline=pipe)
    assert TtlNote.query.count() == 0, "not visible before execute()"
    assert pipe.execute() == []
    assert {n.name for n in TtlNote.query.all()} == {"a", "b"}


def test_a_failed_statement_rolls_the_whole_batch_back(pg):
    TtlCoded(name="taken", code="dup").save()
    pipe = popoto.batch()
    TtlCoded(name="a", code="a").save(pipeline=pipe)
    TtlNote(name="n").save(pipeline=pipe)
    with pytest.raises(ModelException):
        # The batch's rows are not committed, so pre_save's read cannot see
        # "a": the conflict is the UNIQUE index's, inside the transaction.
        TtlCoded(name="b", code="a").save(pipeline=pipe)
    with pytest.raises(BackendError, match="rolled back"):
        pipe.execute()
    assert {n.name for n in TtlCoded.query.all()} == {"taken"}
    assert TtlNote.query.count() == 0


def test_reset_or_leaving_a_with_block_rolls_back(pg):
    pipe = popoto.batch()
    TtlNote(name="a").save(pipeline=pipe)
    pipe.reset()
    with popoto.batch() as pipe2:
        TtlNote(name="b").save(pipeline=pipe2)
    assert TtlNote.query.count() == 0
    pipe.execute()  # nothing pending: a no-op
    assert TtlNote.query.count() == 0


def test_a_batch_refuses_to_mix_backends(pg):
    pipe = popoto.batch()
    pipe.set("$test:batch:mixed", "1")
    with pytest.raises(BackendCapabilityError, match="cannot mix"):
        TtlNote(name="a").save(pipeline=pipe)
    pipe.reset()
    pipe = popoto.batch()
    TtlNote(name="b").save(pipeline=pipe)
    with pytest.raises(BackendCapabilityError, match="cannot mix"):
        pipe.set("$test:batch:mixed", "1")
    pipe.execute()
    assert popoto.get_redis().get("$test:batch:mixed") is None
    assert [n.name for n in TtlNote.query.all()] == ["b"]


def test_a_redis_model_write_is_refused_by_a_postgres_batch(pg):
    class RedisSide(popoto.Model):
        name = popoto.KeyField()

        class Meta:
            backend = "redis"

    pipe = popoto.batch()
    TtlNote(name="a").save(pipeline=pipe)
    with pytest.raises(BackendCapabilityError, match="cannot mix"):
        RedisSide(name="r").save(pipeline=pipe)
    pipe.reset()
    assert not RedisSide.exists(name="r")


def test_delete_and_increment_join_the_batch(pg):
    note = TtlNote.create(name="a", hits=1)
    other = TtlNote.create(name="b")
    pipe = popoto.batch()
    assert note.atomic_increment("hits", 2, pipeline=pipe) == 3
    assert other.delete(pipeline=pipe) is pipe
    assert TtlNote.query.get(name="a").hits == 1
    assert TtlNote.query.get(name="b") is not None
    pipe.execute()
    assert TtlNote.query.get(name="a").hits == 3
    assert TtlNote.query.get(name="b") is None


def test_a_batch_reaps_after_it_commits(pg, admin):
    _make_expired(2)
    with frozen_clock(T0 + 20):
        pipe = popoto.batch(transaction=False)
        TtlNote(name="x").save(pipeline=pipe)
        assert _count(admin, pg, "ttl_note") == 2
        pipe.execute()
        assert _count(admin, pg, "ttl_note") == 1


def test_a_memory_write_joins_the_batch(pg):
    with frozen_clock(T0):
        mem = TtlMemory(project="p", name="m", text="x")
        mem.save()
    pipe = popoto.batch()
    assert ConfidenceField.update_confidence(mem, "confidence", 1.0, pipeline=pipe)
    pipe.reset()  # rolled back: the signal never landed
    assert ConfidenceField.get_confidence(mem, "confidence") == 0.5


def test_concurrent_batches_do_not_see_each_other(pg):
    seen = {}
    pipe = popoto.batch()
    TtlNote(name="mine").save(pipeline=pipe)

    def other():
        seen["count"] = TtlNote.query.count()

    worker = threading.Thread(target=other)
    worker.start()
    worker.join()
    pipe.execute()
    assert seen["count"] == 0 and TtlNote.query.count() == 1


# -- the seeded probe (slice) --------------------------------------------------------


def _probe():
    spec = importlib.util.spec_from_file_location("probe_ttl_parity", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not PROBE.exists(), reason="scripts/probe_ttl_parity.py")
def test_a_slice_of_the_seeded_ttl_probe_has_no_undocumented_mismatch(pg):
    probe = _probe()
    report = probe.run(pg, seeds=[7], shapes=40)
    assert report["shapes"] == 40
    assert report["undocumented"] == [], report["undocumented"][:5]
