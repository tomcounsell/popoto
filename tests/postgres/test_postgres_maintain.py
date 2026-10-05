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
