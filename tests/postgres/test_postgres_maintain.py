"""``[PG-only]`` index maintenance on Postgres (#759 M5, plan §2 ``maintain``).

The conformance half is ``test_check_indexes.py`` / ``test_clean_indexes.py``
/ ``test_migrations.py`` on both legs. Their drift-injecting tests delete
Redis keys through the raw client and are ``redis_only``; this file injects
the Postgres shapes of drift by SQL and checks that ``check_indexes`` finds
each one, that ``clean_indexes`` / ``rebuild_indexes`` repair it, and that
the repair is visible through the public read paths -- so none of these
checks can pass on a table that never drifted.

Drift kinds (``popoto.backends.postgres.maintain``): **orphans** (a side row
naming no record -- only a load with triggers disabled can leave one, so the
tests disable them the same way), **missing** and **stale** (a live record's
derived side rows absent or not what its values derive), and
**partial_writes** (a ``NULL`` auto-key column).
"""

import asyncio
import threading

import pytest

np = pytest.importorskip("numpy")

import popoto  # noqa: E402
from popoto.backends import BackendCapabilityError  # noqa: E402
from popoto.backends.postgres.ttl import frozen_clock  # noqa: E402
from popoto.embeddings import AbstractEmbeddingProvider  # noqa: E402
from popoto.fields.bm25_field import BM25Field  # noqa: E402
from popoto.fields.co_occurrence_field import CoOccurrenceField  # noqa: E402
from popoto.fields.embedding_field import EmbeddingField  # noqa: E402
from popoto.fields.existence_filter import ExistenceFilter  # noqa: E402
from popoto.fields.supersession import SupersessionProtocol  # noqa: E402
from popoto.fields.validity_field import ValidityField  # noqa: E402


class _Provider(AbstractEmbeddingProvider):
    """Deterministic 4-d vectors from the text."""

    def embed(self, texts, input_type=None):
        return [
            np.random.RandomState(sum(map(ord, t)) % (2**31)).randn(4).tolist()
            for t in texts
        ]

    @property
    def dimensions(self):
        return 4

    @property
    def max_batch_size(self):
        return 32


class MaintDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    project = popoto.Field(type=str, default="p1")
    stamp = popoto.SortedField(type=float, default=0.0, partition_by="project")
    text = popoto.StringField(default="")
    content = BM25Field(source="text")
    embedding = EmbeddingField(source="text", provider=_Provider())
    bloom = ExistenceFilter(fingerprint_fn=lambda inst: inst.text)


class MaintAuto(popoto.Model):
    uuid = popoto.AutoKeyField()
    data = popoto.Field(type=str, null=True)


class MaintFact(popoto.Model):
    name = popoto.Field(type=str)
    validity = ValidityField()


class MaintKeyed(popoto.Model):
    email = popoto.KeyField()
    score = popoto.SortedField(type=float, default=0.0)


class MaintTtl(popoto.Model):
    name = popoto.UniqueKeyField()
    text = popoto.StringField(default="")
    content = BM25Field(source="text")

    class Meta:
        ttl = 60


def _q(pg_schema, table):
    return f'"{pg_schema.name}"."{table}"'


def _count(admin, sql, params=()):
    return admin.execute(sql, params).fetchone()[0]


def _without_triggers(admin, sql, params=()):
    """Run ``sql`` the way a bulk load or ``pg_restore --disable-triggers``
    does: with foreign keys unenforced, so a cascade does not fire."""
    with admin.transaction():
        admin.execute("SET LOCAL session_replication_role = replica")
        admin.execute(sql, params)


def _side(check, field):
    return check["side_tables"][field]


def _docs():
    MaintDoc(name="a", text="postgres maintenance guide", project="p1").save()
    MaintDoc(name="b", text="redis caching strategies", project="p2").save()
    MaintDoc(name="c", text="", project="p1").save()


def _hits(query):
    return [k for k, _s in BM25Field.search(MaintDoc, "content", query)]


# -- healthy ---------------------------------------------------------------------


def test_a_healthy_model_reports_no_drift_and_the_redis_shape(pg):
    _docs()
    result = MaintDoc.check_indexes()
    assert result["total"] == 0
    assert result["class_set"] == 0
    assert result["partial_writes"] == 0
    assert result["key_fields"] == {"name": 0}
    assert set(result["sorted_fields"]) == {"stamp"}
    assert result["geo_fields"] == {} and result["composite_indexes"] == {}
    assert result["side_tables"] == {
        name: {"orphans": 0, "missing": 0, "stale": 0}
        for name in ("bloom", "content", "embedding")
    }
    assert MaintDoc.check_indexes(batch_size=1) == result


def test_check_is_read_only(pg, pg_schema, admin):
    _docs()
    tables = [
        "maint_doc",
        "maint_doc__content__post",
        "maint_doc__content__dl",
        "maint_doc__embedding__vec",
        "maint_doc__bloom__tok",
    ]
    admin.execute(f"DELETE FROM {_q(pg_schema, 'maint_doc__content__dl')}")

    def snapshot():
        return {
            t: admin.execute(f"SELECT * FROM {_q(pg_schema, t)} ORDER BY 1").fetchall()
            for t in tables
        }

    before = snapshot()
    assert MaintDoc.check_indexes()["total"] > 0
    assert snapshot() == before


# -- orphans ---------------------------------------------------------------------


def test_orphans_left_by_a_trigger_less_delete_are_found_and_cleaned(
    pg, pg_schema, admin
):
    _docs()
    victim = MaintDoc.query.get(name="a").db_key.redis_key
    _without_triggers(
        admin, f'DELETE FROM {_q(pg_schema, "maint_doc")} WHERE _pk = %s', (victim,)
    )
    check = MaintDoc.check_indexes()
    # "a" had 3 distinct terms (3 postings) + a length row.
    assert _side(check, "content")["orphans"] == 4
    assert _side(check, "embedding")["orphans"] == 1
    assert _side(check, "bloom")["orphans"] == 3  # its fingerprint tokens
    assert check["total"] == 8

    removed = MaintDoc.clean_indexes(batch_size=2)
    assert removed == 8
    after = MaintDoc.check_indexes()
    assert after["total"] == 0
    for table in ("post", "dl"):
        assert (
            _count(
                admin,
                f"SELECT count(*) FROM "
                f"{_q(pg_schema, 'maint_doc__content__' + table)} WHERE _pk = %s",
                (victim,),
            )
            == 0
        )
    # The survivors' rows are untouched and still found.
    assert _hits("redis") == [MaintDoc.query.get(name="b").db_key.redis_key]


def test_validity_pointer_orphans_are_found_and_cleaned(pg, pg_schema, admin):
    old = MaintFact.create(name="old")
    SupersessionProtocol.supersede(old, identity_key="subject")
    healthy = MaintFact.create(name="healthy")
    SupersessionProtocol.supersede(healthy, identity_key="other")
    pointer = _q(pg_schema, "maint_fact__validity__open")
    assert _count(admin, f"SELECT count(*) FROM {pointer}") == 2

    _without_triggers(
        admin,
        f'DELETE FROM {_q(pg_schema, "maint_fact")} WHERE _pk = %s',
        (old.db_key.redis_key,),
    )
    assert MaintFact.check_indexes()["side_tables"]["validity"]["orphans"] == 1
    assert MaintFact.clean_indexes() == 1
    rows = admin.execute(f"SELECT member FROM {pointer}").fetchall()
    assert rows == [(healthy.db_key.redis_key,)]


# -- missing and stale -----------------------------------------------------------


def test_missing_postings_are_found_and_rebuilt(pg, pg_schema, admin):
    _docs()
    a = MaintDoc.query.get(name="a").db_key.redis_key
    for table in ("post", "dl"):
        admin.execute(
            f"DELETE FROM {_q(pg_schema, 'maint_doc__content__' + table)} "
            "WHERE _pk = %s",
            (a,),
        )
    assert _hits("maintenance") == []
    check = MaintDoc.check_indexes()
    assert _side(check, "content") == {"orphans": 0, "missing": 1, "stale": 0}

    result = MaintDoc.rebuild_indexes(batch_size=2)
    assert int(result) == 3 and result.diverged_keys == []
    assert MaintDoc.check_indexes()["total"] == 0
    assert _hits("maintenance") == [a]


def test_raw_update_of_a_source_leaves_postings_stale_until_rebuild(pg):
    _docs()
    a = MaintDoc.query.get(name="a").db_key.redis_key
    assert MaintDoc.raw_update([a], text="vacuum analyze") == 1
    # The row moved; its postings did not (raw_update runs no hooks).
    assert MaintDoc.query.get(name="a").text == "vacuum analyze"
    assert _hits("vacuum") == []
    assert _hits("maintenance") == [a]
    check = MaintDoc.check_indexes()
    assert _side(check, "content")["stale"] == 1
    # The narrow vector row still matches the record's (unchanged) vector,
    # and the fingerprint tokens of the new text are missing.
    assert _side(check, "embedding") == {"orphans": 0, "missing": 0, "stale": 0}
    assert _side(check, "bloom")["missing"] == 1

    MaintDoc.rebuild_indexes()
    assert MaintDoc.check_indexes()["total"] == 0
    assert _hits("vacuum") == [a]
    assert _hits("maintenance") == []
    bloom = MaintDoc._meta.fields["bloom"]
    assert bloom.might_exist(MaintDoc, "vacuum analyze")


def test_postings_under_the_wrong_scope_are_stale(pg, pg_schema, admin):
    _docs()
    admin.execute(
        f"UPDATE {_q(pg_schema, 'maint_doc__content__post')} SET scope = 'elsewhere'"
    )
    admin.execute(
        f"UPDATE {_q(pg_schema, 'maint_doc__embedding__vec')} SET scope = 'x' "
        "WHERE scope = 'p2'"
    )
    check = MaintDoc.check_indexes()
    assert _side(check, "content")["stale"] == 2  # "c" has no tokens
    assert _side(check, "embedding")["stale"] == 1
    MaintDoc.rebuild_indexes()
    assert MaintDoc.check_indexes()["total"] == 0
    scopes = admin.execute(
        f"SELECT DISTINCT scope FROM {_q(pg_schema, 'maint_doc__content__post')} "
        "ORDER BY 1"
    ).fetchall()
    assert scopes == [("p1",), ("p2",)]


def test_a_missing_or_altered_narrow_vector_is_rebuilt_from_the_record(
    pg, pg_schema, admin
):
    _docs()
    narrow = _q(pg_schema, "maint_doc__embedding__vec")
    before = dict(admin.execute(f"SELECT _pk, v::text FROM {narrow}").fetchall())
    a = MaintDoc.query.get(name="a").db_key.redis_key
    b = MaintDoc.query.get(name="b").db_key.redis_key
    admin.execute(f"DELETE FROM {narrow} WHERE _pk = %s", (a,))
    admin.execute(f"UPDATE {narrow} SET v = '[9,9,9,9]' WHERE _pk = %s", (b,))
    check = MaintDoc.check_indexes()
    assert _side(check, "embedding") == {"orphans": 0, "missing": 1, "stale": 1}
    MaintDoc.rebuild_indexes()
    assert MaintDoc.check_indexes()["total"] == 0
    after = dict(admin.execute(f"SELECT _pk, v::text FROM {narrow}").fetchall())
    assert after == before


def test_missing_existence_tokens_are_rebuilt(pg, pg_schema, admin):
    _docs()
    bloom = MaintDoc._meta.fields["bloom"]
    admin.execute(f"DELETE FROM {_q(pg_schema, 'maint_doc__bloom__tok')}")
    assert not bloom.might_exist(MaintDoc, "redis caching strategies")
    # "c" has an empty text: its fingerprint token is "" and is missing too.
    assert _side(MaintDoc.check_indexes(), "bloom")["missing"] == 3
    MaintDoc.rebuild_indexes()
    assert bloom.might_exist(MaintDoc, "redis caching strategies")
    assert MaintDoc.check_indexes()["total"] == 0


def test_an_expired_record_is_neither_checked_nor_rebuilt(pg, pg_schema, admin):
    with frozen_clock(1_000_000.0):
        MaintTtl(name="live", text="alpha beta").save()
        gone = MaintTtl(name="gone", text="gamma delta")
        gone._ttl = 5
        gone.save()
    post = _q(pg_schema, "maint_ttl__content__post")
    admin.execute(f"DELETE FROM {post} WHERE _pk = %s", (gone.db_key.redis_key,))
    with frozen_clock(1_000_030.0):
        # The expired row's absent postings are not "missing"; its remaining
        # length row is the reaper's, not an orphan.
        assert MaintTtl.check_indexes()["total"] == 0
        assert int(MaintTtl.rebuild_indexes()) == 1
    assert (
        _count(
            admin,
            f"SELECT count(*) FROM {post} WHERE _pk = %s",
            (gone.db_key.redis_key,),
        )
        == 0
    )


# -- partial writes and divergence -----------------------------------------------


def test_a_null_auto_key_is_a_partial_write_and_clean_deletes_only_it(
    pg, pg_schema, admin
):
    keep = MaintAuto.create(data="keep")
    ghost = MaintAuto.create(data="ghost")
    table = _q(pg_schema, "maint_auto")
    admin.execute(
        f"UPDATE {table} SET uuid = NULL WHERE _pk = %s", (ghost.db_key.redis_key,)
    )
    check = MaintAuto.check_indexes()
    assert check["partial_writes"] == 1 and check["total"] == 1
    assert MaintAuto.clean_indexes() == 1
    assert [r[0] for r in admin.execute(f"SELECT _pk FROM {table}").fetchall()] == [
        keep.db_key.redis_key
    ]
    assert MaintAuto.check_indexes()["partial_writes"] == 0


def test_an_empty_auto_key_is_a_partial_write(pg, pg_schema, admin):
    ghost = MaintAuto.create(data="ghost")
    admin.execute(
        f"UPDATE {_q(pg_schema, 'maint_auto')} SET uuid = '' WHERE _pk = %s",
        (ghost.db_key.redis_key,),
    )
    assert MaintAuto.check_indexes()["partial_writes"] == 1


def test_rebuild_skips_and_reports_a_row_whose_key_column_diverged(
    pg, pg_schema, admin
):
    MaintKeyed.create(email="a@x", score=1.0)
    moved = MaintKeyed.create(email="b@x", score=2.0)
    admin.execute(
        f"UPDATE {_q(pg_schema, 'maint_keyed')} SET email = 'z@x' WHERE _pk = %s",
        (moved.db_key.redis_key,),
    )
    result = MaintKeyed.rebuild_indexes()
    assert int(result) == 1
    assert result.diverged_keys == [moved.db_key.redis_key]


def test_rebuild_analyzes_the_table_and_its_companions(pg, pg_schema, admin):
    _docs()
    MaintDoc.rebuild_indexes()
    rows = admin.execute(
        "SELECT relname, last_analyze IS NOT NULL OR last_autoanalyze IS NOT NULL "
        "FROM pg_stat_user_tables WHERE schemaname = %s AND relname LIKE 'maint_doc%%'",
        (pg_schema.name,),
    ).fetchall()
    analyzed = dict(rows)
    assert analyzed["maint_doc"] and analyzed["maint_doc__content__post"]


# -- raw_update ------------------------------------------------------------------


def test_raw_update_refuses_a_name_with_no_column(pg):
    doc = MaintDoc.create(name="r", text="x")
    with pytest.raises(BackendCapabilityError, match="content"):
        MaintDoc.raw_update([doc.db_key.redis_key], content="nope")
    with pytest.raises(BackendCapabilityError, match="nonexistent"):
        MaintDoc.raw_update([doc.db_key.redis_key], nonexistent=1)


def test_raw_update_never_creates_a_row(pg):
    assert MaintKeyed.raw_update(["MaintKeyed:nobody"], score=3.0) == 0
    assert MaintKeyed.query.count() == 0


# -- async and concurrency -------------------------------------------------------


def test_async_variants_match_the_sync_ones(pg, pg_schema, admin):
    _docs()
    admin.execute(f"DELETE FROM {_q(pg_schema, 'maint_doc__content__dl')}")
    sync = MaintDoc.check_indexes()
    assert asyncio.run(MaintDoc.async_check_indexes()) == sync
    assert asyncio.run(MaintDoc.async_rebuild_indexes()) == 3
    assert asyncio.run(MaintDoc.async_clean_indexes()) == 0
    assert MaintDoc.check_indexes()["total"] == 0


def test_rebuild_beside_concurrent_saves_ends_consistent(pg):
    """A rebuild page takes its records' key locks first, as every writer
    does, so saves racing it neither deadlock nor leave a page's stale read
    behind: the end state checks clean."""
    for i in range(40):
        MaintDoc(name=f"d{i}", text=f"word{i} common", project=f"p{i % 3}").save()
    errors = []

    def writer():
        try:
            for round_ in range(3):
                for i in range(0, 40, 3):
                    doc = MaintDoc.query.get(name=f"d{i}")
                    doc.text = f"changed{round_} common"
                    doc.save()
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    for _ in range(3):
        MaintDoc.rebuild_indexes(batch_size=7)
    thread.join()
    assert errors == []
    assert MaintDoc.check_indexes()["total"] == 0


# -- REINDEX failure modes (#788 review) ----------------------------------------


def _invalid_indexes(admin, pg_schema):
    """Every INVALID index on the test schema's tables (their TOAST tables
    included), by name."""
    return sorted(
        r[0]
        for r in admin.execute(
            "SELECT ic.relname FROM pg_index i "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "LEFT JOIN pg_class owner ON owner.reltoastrelid = t.oid "
            "JOIN pg_namespace n ON n.oid = coalesce(owner.relnamespace, "
            "t.relnamespace) WHERE NOT i.indisvalid AND n.nspname = %s",
            (pg_schema.name,),
        ).fetchall()
    )


def _blocker(pg_schema):
    """An idle-in-transaction session that wrote ``maint_doc``: what another
    agent's open transaction on the shared server looks like. ``REINDEX``
    and ``DROP INDEX CONCURRENTLY`` wait for it to end."""
    conn = pg_schema.connect()
    conn.execute("BEGIN")
    conn.execute(
        f"UPDATE {_q(pg_schema, 'maint_doc')} SET project = project WHERE _pk = %s",
        ("MaintDoc:b",),
    )
    return conn


@pytest.fixture
def short_maintain_timeouts(monkeypatch):
    from popoto.fields.constants import Defaults

    monkeypatch.setattr(Defaults, "PG_MAINTAIN_LOCK_TIMEOUT_MS", 300)
    monkeypatch.setattr(Defaults, "PG_MAINTAIN_CLEANUP_LOCK_TIMEOUT_MS", 100)


def _health(pg):
    h = pg.health
    return (h.ok, h.consecutive_failures, h.dropped_writes)


def test_rebuild_inside_a_batch_is_refused_at_once(pg):
    """Reproduced in review: inside ``popoto.batch()`` the REINDEX waited on
    ``Lock/virtualxid`` -- the batch's own open transaction -- forever."""
    import time

    _docs()
    started = time.monotonic()
    with pytest.raises(BackendCapabilityError, match="rebuild_indexes"):
        with popoto.batch() as b:
            MaintDoc(name="z", text="inside a batch").save(pipeline=b)
            MaintDoc.rebuild_indexes()
    with pytest.raises(BackendCapabilityError, match="rebuild_indexes"):
        with pg.transaction() as uow:
            MaintDoc(name="y", text="inside a unit").save(pipeline=uow)
            MaintDoc.rebuild_indexes()
    assert time.monotonic() - started < 5
    assert MaintDoc.query.count() == 3  # both units rolled back
    assert MaintDoc.rebuild_indexes() == 3  # outside a unit it runs


def test_a_blocked_reindex_is_maintenance_not_an_outage(
    pg, pg_schema, admin, short_maintain_timeouts
):
    from popoto.backends import (
        BackendRetryableError,
        BackendUnavailableError,
        MaintenanceIncompleteError,
    )

    _docs()
    admin.execute(f"DELETE FROM {_q(pg_schema, 'maint_doc__content__dl')}")
    assert MaintDoc.check_indexes()["total"] == 2  # a and b lost their length
    before = _health(pg)
    blocker = _blocker(pg_schema)
    try:
        with pytest.raises(MaintenanceIncompleteError) as info:
            MaintDoc.rebuild_indexes()
        err = info.value
        assert isinstance(err, BackendRetryableError)
        assert not isinstance(err, BackendUnavailableError)
        assert "55P03" in str(err)  # lock_timeout, not a hang
        # What completed is reported, and it really committed.
        assert err.completed[:2] == ("side_rows", "orphans")
        assert err.failed_step.startswith("reindex ")
        assert err.indexed == 3
        assert _health(pg) == before  # not an outage, no dropped write
        check = MaintDoc.check_indexes()
        assert all(not any(c.values()) for c in check["side_tables"].values())
        assert _hits("redis") == ["MaintDoc:b"]
        # The failed CONCURRENTLY left INVALID transient indexes, and the
        # blocker keeps the immediate drop from removing them.
        left = _invalid_indexes(admin, pg_schema)
        assert left and all("_ccnew" in name for name in left)
        assert len(err.invalid_indexes) == len(left)
        assert check["invalid_indexes"] == len(left)
        assert check["total"] == len(left)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    # Once the session ends, the next rebuild drops them first and succeeds.
    assert MaintDoc.rebuild_indexes() == 3
    assert _invalid_indexes(admin, pg_schema) == []
    assert MaintDoc.check_indexes()["total"] == 0
    assert _health(pg) == before


def test_clean_drops_the_invalid_indexes_a_failed_rebuild_left(
    pg, pg_schema, admin, short_maintain_timeouts
):
    from popoto.backends import MaintenanceIncompleteError

    _docs()
    blocker = _blocker(pg_schema)
    try:
        with pytest.raises(MaintenanceIncompleteError):
            MaintDoc.rebuild_indexes()
        # Inside a unit of work clean leaves them (the drop would wait on
        # that unit); the orphan work still runs.
        with pg.transaction(), pg.second_connection_ok():  # outside it (#776)
            assert MaintDoc.clean_indexes() == 0
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    left = _invalid_indexes(admin, pg_schema)
    assert left
    assert MaintDoc.check_indexes()["invalid_indexes"] == len(left)
    assert MaintDoc.clean_indexes() == len(left)
    assert _invalid_indexes(admin, pg_schema) == []
    assert MaintDoc.check_indexes()["total"] == 0


def test_a_cancelled_reindex_is_maintenance_and_cleans_up_after_itself(
    pg, pg_schema, admin
):
    """A cancel with nothing else blocking: the immediate best-effort drop
    succeeds, so no INVALID index survives the failed call."""
    from popoto.backends import MaintenanceIncompleteError

    _docs()
    before = _health(pg)
    stop = threading.Event()

    def canceller():
        watcher = pg_schema.connect()
        try:
            while not stop.is_set():
                watcher.execute(
                    "SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                    "WHERE query LIKE 'REINDEX TABLE CONCURRENTLY%' "
                    "AND pid <> pg_backend_pid()"
                )
        finally:
            watcher.close()

    thread = threading.Thread(target=canceller)
    thread.start()
    try:
        with pytest.raises(MaintenanceIncompleteError) as info:
            for _ in range(50):  # until a cancel lands mid-REINDEX
                MaintDoc.rebuild_indexes()
    finally:
        stop.set()
        thread.join()
    assert "57014" in str(info.value)
    assert info.value.invalid_indexes == []
    assert _invalid_indexes(admin, pg_schema) == []
    assert _health(pg) == before


def test_async_maintenance_runs_on_the_async_backend(pg, pg_schema, admin):
    """``async_*_indexes`` go through ``_off_loop`` (#784's bridge): every
    statement -- the REINDEX on its dedicated connection included -- runs on
    the loop thread over ``AsyncConnection``s, not in a worker thread."""
    pytest.importorskip("greenlet")
    import psycopg

    from popoto.backends.postgres import aio

    _docs()
    admin.execute(f"DELETE FROM {_q(pg_schema, 'maint_doc__content__dl')}")
    main = threading.current_thread()
    seen = []
    real_connect = aio._Bridge.connect

    def connect(dsn, **kwargs):
        conn = real_connect(dsn, **kwargs)
        seen.append((threading.current_thread(), type(conn.async_connection)))
        return conn

    real_run = pg._run
    threads = set()

    def run(*args, **kwargs):
        threads.add(threading.current_thread())
        return real_run(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(aio._Bridge, "connect", staticmethod(connect))
        mp.setattr(pg, "_run", run)
        assert asyncio.run(MaintDoc.async_check_indexes())["total"] == 2
        assert asyncio.run(MaintDoc.async_rebuild_indexes()) == 3
        assert asyncio.run(MaintDoc.async_clean_indexes()) == 0
    assert threads == {main}
    assert seen and all(
        t is main and issubclass(cls, psycopg.AsyncConnection) for t, cls in seen
    )
    assert MaintDoc.check_indexes()["total"] == 0


def test_async_rebuild_inside_an_async_transaction_is_refused(pg):
    pytest.importorskip("greenlet")
    from popoto.backends.postgres.aio import get_async_backend

    _docs()

    async def main():
        async with get_async_backend(MaintDoc).transaction() as uow:
            await MaintDoc(name="z", text="in a unit").async_save(pipeline=uow)
            await MaintDoc.async_rebuild_indexes()

    with pytest.raises(BackendCapabilityError, match="rebuild_indexes"):
        asyncio.run(main())
    assert MaintDoc.query.count() == 3


def test_async_blocked_reindex_is_maintenance_not_an_outage(
    pg, pg_schema, admin, short_maintain_timeouts
):
    pytest.importorskip("greenlet")
    from popoto.backends import MaintenanceIncompleteError

    _docs()
    before = _health(pg)
    blocker = _blocker(pg_schema)
    try:
        with pytest.raises(MaintenanceIncompleteError):
            asyncio.run(MaintDoc.async_rebuild_indexes())
        assert _health(pg) == before
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert asyncio.run(MaintDoc.async_rebuild_indexes()) == 3
    assert _invalid_indexes(admin, pg_schema) == []


# -- co-occurrence edges ----------------------------------------------------------


class MaintGraph(popoto.Model):
    name = popoto.UniqueKeyField()
    near = CoOccurrenceField()  # symmetric
    follows = CoOccurrenceField(symmetric=False)


def _linked(field, key):
    return sorted(k for k, _w in field.get_linked(MaintGraph, key))


def test_dangling_edges_are_reported_apart_and_kept(pg, pg_schema, admin):
    """Redis keeps an edge whose record is gone and counts nothing, so
    check/clean/rebuild here keep it and ``total`` stays 0 (#788 review).
    ``check`` still reports it, as ``graph_edges.dangling``."""
    a, b, c, d = (MaintGraph.create(name=n) for n in "abcd")
    ka, kb, kc, kd = (o.db_key.redis_key for o in (a, b, c, d))
    near = MaintGraph._meta.fields["near"]
    follows = MaintGraph._meta.fields["follows"]
    near.link(MaintGraph, ka, kb, 0.5)  # a<->b
    near.link(MaintGraph, ka, "Elsewhere:x", 0.5)  # outside the key space
    follows.link(MaintGraph, kc, kd, 0.5)  # c->d
    follows.link(MaintGraph, kd, ka, 0.5)  # d->a
    check = MaintGraph.check_indexes()
    assert check["total"] == 0
    assert check["side_tables"] == {"graph_edges": {"dangling": 0}}
    for victim in (kb, kd):
        admin.execute(
            f"DELETE FROM {_q(pg_schema, 'maint_graph')} WHERE _pk = %s", (victim,)
        )
    before = (_linked(near, ka), _linked(follows, kc), _linked(follows, kd))
    check = MaintGraph.check_indexes()
    # near: both directions of a<->b. follows: d->a (its source is gone);
    # c->d is data (an asymmetric delete leaves edges *to* a record).
    assert check["side_tables"]["graph_edges"] == {"dangling": 3}
    assert check["total"] == 0
    assert MaintGraph.clean_indexes(batch_size=1) == 0
    MaintGraph.rebuild_indexes()
    assert MaintGraph.check_indexes() == check
    assert before == (_linked(near, ka), _linked(follows, kc), _linked(follows, kd))
    assert _linked(near, ka) == sorted([kb, "Elsewhere:x"])


def test_never_saved_endpoints_keep_their_edges(pg):
    a = MaintGraph.create(name="a")
    ka = a.db_key.redis_key
    near = MaintGraph._meta.fields["near"]
    follows = MaintGraph._meta.fields["follows"]
    near.link(MaintGraph, ka, "MaintGraph:ghost", 0.5)
    follows.link(MaintGraph, "MaintGraph:ghost2", ka, 0.5)
    check = MaintGraph.check_indexes()
    assert check["total"] == 0
    assert check["side_tables"]["graph_edges"]["dangling"] == 3
    assert MaintGraph.clean_indexes() == 0
    MaintGraph.rebuild_indexes()
    assert MaintGraph.check_indexes()["total"] == 0
    assert _linked(near, ka) == ["MaintGraph:ghost"]
    assert _linked(follows, "MaintGraph:ghost2") == [ka]


class MaintTtlGraph(popoto.Model):
    name = popoto.UniqueKeyField()
    near = CoOccurrenceField()

    class Meta:
        ttl = 60


def test_a_reaped_records_edges_stay_and_total_is_zero(pg):
    from popoto.backends import get_backend
    from popoto.backends.postgres import ttl as ttl_mod

    near = MaintTtlGraph._meta.fields["near"]
    with frozen_clock(1_000_000.0):
        a = MaintTtlGraph.create(name="a")
        b = MaintTtlGraph(name="b")
        b._ttl = 5
        b.save()
        ka, kb = a.db_key.redis_key, b.db_key.redis_key
        near.link(MaintTtlGraph, ka, kb, 0.5)
        assert MaintTtlGraph.check_indexes()["total"] == 0
    with frozen_clock(1_000_030.0):
        backend = get_backend(MaintTtlGraph)
        ts = backend._table(MaintTtlGraph._meta.spec)
        assert ttl_mod.reap(backend, ts, force=True) == [kb]
        check = MaintTtlGraph.check_indexes()
        assert check["total"] == 0
        assert check["side_tables"]["graph_edges"]["dangling"] == 2
        assert MaintTtlGraph.clean_indexes() == 0
        MaintTtlGraph.rebuild_indexes()
        assert MaintTtlGraph.check_indexes()["total"] == 0
        assert [k for k, _w in near.get_linked(MaintTtlGraph, ka)] == [kb]
