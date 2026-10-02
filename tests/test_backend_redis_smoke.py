"""Conformance-style smoke test for ``RedisBackend`` (#631 WS0).

Calls a handful of backend methods and checks each against the direct client
path -- the same commands issued through ``get_REDIS_DB()`` -- on the pytest
plugin's isolated database. These are the first tests to exercise the moved
bodies; the ``conformance`` marker that parameterises them over installed
backends is WS2's, so nothing here is marked yet.

Never touches database 0: the autouse flush and every key below go through
the plugin-bound client.
"""

import msgpack
import pytest

from popoto import backends, redis_db
from popoto.backends.redis import RedisBackend
from popoto.exceptions import ModelException

PREFIX = "popoto_test:631"


@pytest.fixture
def backend():
    return RedisBackend()


@pytest.fixture
def client():
    return redis_db.get_REDIS_DB()


def test_the_default_selection_is_the_backend_under_test():
    assert isinstance(backends.get_backend(), RedisBackend)


class TestRecords:
    def test_save_load_exists_delete_roundtrip(self, backend, client):
        key = f"{PREFIX}:Record:1"
        class_set = f"$Class:{PREFIX}:Record"
        fields = {b"name": msgpack.packb("alice"), b"age": msgpack.packb(30)}

        reply = backend.save_record(key, fields, class_set=class_set)
        assert reply == 2  # the HSET count, as Model.save reports it
        assert client.hgetall(key) == fields
        assert client.smembers(class_set) == {key.encode()}

        assert backend.load_record(key) == client.hgetall(key)
        assert backend.load_record(f"{PREFIX}:Record:missing") is None
        assert backend.record_exists(key) is bool(client.exists(key)) is True
        assert backend.list_keys(class_set) == {key}
        assert backend.count_records(class_set) == client.scard(class_set) == 1

        assert backend.load_fields(key, ["name"]) == [client.hget(key, "name")]
        assert backend.load_fields(key, ["name", "age", "nope"]) == client.hmget(
            key, ["name", "age", "nope"]
        )
        with pytest.raises(ValueError):
            backend.load_fields(key, [])

        assert backend.delete_record(key, class_set=class_set) is True
        assert not client.exists(key)
        assert client.scard(class_set) == 0
        assert backend.delete_record(key, class_set=class_set) is False

    def test_save_record_migrates_an_obsolete_key(self, backend, client):
        class_set = f"$Class:{PREFIX}:Record"
        old, new = f"{PREFIX}:Record:old", f"{PREFIX}:Record:new"
        backend.save_record(old, {b"f": b"\x01"}, class_set=class_set)
        backend.save_record(
            new, {b"f": b"\x01"}, class_set=class_set, obsolete_key=old, ttl=60
        )
        assert not client.exists(old)
        assert client.smembers(class_set) == {new.encode()}
        assert 0 < client.ttl(new) <= 60

    def test_load_records_matches_a_pipelined_hgetall(self, backend, client):
        keys = [f"{PREFIX}:Record:{i}" for i in range(3)]
        client.hset(keys[0], mapping={b"a": b"1"})
        client.hset(keys[2], mapping={b"b": b"2"})
        pipe = client.pipeline()
        for key in keys:
            pipe.hgetall(key)
        direct = [h or None for h in pipe.execute()]
        assert backend.load_records(keys) == direct
        assert backend.load_records([]) == []

    def test_records_exist_matches_a_pipelined_exists(self, backend, client):
        # protocol-1: the batched form of record_exists, one round trip.
        keys = [f"{PREFIX}:Record:{i}" for i in range(3)]
        client.hset(keys[0], mapping={b"a": b"1"})
        client.hset(keys[2], mapping={b"b": b"2"})
        pipe = client.pipeline()
        for key in keys:
            pipe.exists(key)
        direct = [bool(r) for r in pipe.execute()]
        assert backend.records_exist(keys) == direct == [True, False, True]
        assert backend.records_exist([]) == []

    def test_save_and_delete_queue_on_a_unit_of_work(self, backend, client):
        key = f"{PREFIX}:Record:uow"
        class_set = f"$Class:{PREFIX}:Record"
        uow = backend.begin()
        assert (
            backend.save_record(key, {b"f": b"v"}, class_set=class_set, uow=uow) is None
        )
        assert not client.exists(key), "nothing may be written before commit()"
        results = uow.commit()
        assert results[0] == 1
        assert client.hgetall(key) == {b"f": b"v"}

    def test_save_record_without_a_class_set_leaves_the_set_alone(
        self, backend, client
    ):
        # protocol-2 (#735 review B1): ``class_set=None`` means no SADD of the
        # key and no SREM of the obsolete key; the obsolete hash is still DEL'd.
        old, new = f"{PREFIX}:Record:p-old", f"{PREFIX}:Record:p-new"
        class_set = f"$Class:{PREFIX}:Record"
        client.hset(old, mapping={b"f": b"\x01"})
        client.sadd(class_set, old)
        assert backend.save_record(new, {b"f": b"\x02"}, obsolete_key=old) == 1
        assert client.hgetall(new) == {b"f": b"\x02"}
        assert not client.exists(old)
        assert client.smembers(class_set) == {old.encode()}, "set untouched"
        uow = backend.begin()
        assert backend.save_record(new, {b"g": b"\x03"}, uow=uow) is None
        assert uow.commit() == [1]
        assert client.smembers(class_set) == {old.encode()}

    def test_set_expiry_matches_expire_and_expireat(self, backend, client):
        # protocol-2 (#735 review B2): the partial save's trailing EXPIRE.
        import time

        key = f"{PREFIX}:Record:ttl"
        client.hset(key, mapping={b"f": b"v"})
        assert backend.set_expiry(key, ttl=600) is True
        assert client.ttl(key) == 600
        assert backend.set_expiry(key, expire_at=time.time() + 1200) is True
        assert 1190 <= client.ttl(key) <= 1200
        assert backend.set_expiry(key) is None, "nothing to issue"
        assert backend.set_expiry(f"{PREFIX}:Record:missing", ttl=5) is False
        uow = backend.begin()
        assert backend.set_expiry(key, ttl=30, uow=uow) is None
        assert 1190 <= client.ttl(key) <= 1200, "nothing applied before commit()"
        assert uow.commit() == [True]
        assert client.ttl(key) == 30


class TestIncrement:
    def test_increment_matches_the_msgpack_envelope(self, backend, client):
        key = f"{PREFIX}:Counter:1"
        client.hset(key, "n", msgpack.packb(5))
        assert backend.increment_field(key, "n", 3, kind="int") == 8
        assert msgpack.unpackb(client.hget(key, "n")) == 8
        assert backend.increment_field(key, "ratio", 0.5, kind="float") == 0.5
        from decimal import Decimal

        got = backend.increment_field(key, "d", Decimal("1.25"), kind="decimal")
        assert got == Decimal("1.25")
        stored = msgpack.unpackb(client.hget(key, "d"))
        assert stored["__Decimal__"] is True and stored["as_encodable"] == "1.25"


class TestSortedIndexes:
    def test_range_members_count_and_score_agree_with_the_client(self, backend, client):
        idx = f"{PREFIX}:_score"
        for member, score in (("a", 1.0), ("b", 2.0), ("c", 3.0), ("d", 4.0)):
            assert backend.sorted_add(idx, member, score) == 1
        assert backend.sorted_count(idx) == client.zcard(idx) == 4
        assert backend.sorted_score(idx, "b") == client.zscore(idx, "b") == 2.0
        assert backend.sorted_score(idx, "zz") is None

        assert backend.sorted_members(idx) == [
            m.decode() for m in client.zrange(idx, 0, -1)
        ]
        assert backend.sorted_members(idx, 0, 1, reverse=True) == ["d", "c"]

        decode = lambda reply: [m.decode() for m in reply]  # noqa: E731
        assert backend.sorted_range(idx, 2.0, 3.0) == decode(
            client.zrangebyscore(idx, "2.0", "3.0")
        )
        assert backend.sorted_range(idx, 2.0, 3.0, lo_inclusive=False) == decode(
            client.zrangebyscore(idx, "(2.0", "3.0")
        )
        assert backend.sorted_range(
            idx, float("-inf"), float("inf"), reverse=True, limit=2
        ) == decode(client.zrevrangebyscore(idx, "+inf", "-inf", start=0, num=2))
        assert backend.sorted_range(idx, 2.0, float("inf"), hi_inclusive=False) == [
            "b",
            "c",
            "d",
        ]

        assert backend.sorted_increment(idx, "a", 10.0) == 11.0
        assert client.zscore(idx, "a") == 11.0
        assert backend.sorted_remove(idx, "a") == 1
        assert backend.sorted_count(idx) == 3


class TestSetIndexes:
    def test_members_union_intersection(self, backend, client):
        red, blue = f"{PREFIX}:_tag:red", f"{PREFIX}:_tag:blue"
        backend.index_add(red, "k1")
        backend.index_add(red, "k2")
        backend.index_add(blue, "k2")
        assert backend.index_members(red) == {m.decode() for m in client.smembers(red)}
        assert backend.index_union([red, blue]) == {"k1", "k2"}
        assert backend.index_intersection([red, blue]) == {"k2"}
        assert backend.index_union([]) == set()
        assert backend.index_remove(red, "k1") == 1
        assert backend.index_members(red) == {"k2"}

    def test_scan_record_keys_drops_non_hash_keys(self, backend, client):
        client.hset(f"{PREFIX}:Model:1", "f", "v")
        client.set(f"{PREFIX}:Model:1\x00idxptr\x00f", "ptr")
        client.rpush(f"{PREFIX}:Model:1::items", "x")
        assert backend.scan_record_keys(f"{PREFIX}:Model:*") == [f"{PREFIX}:Model:1"]
        assert sorted(backend.scan_index_names(f"{PREFIX}:Model:*")) == sorted(
            k.decode() for k in redis_db.scan_keys(f"{PREFIX}:Model:*")
        )


class TestSideMaps:
    def test_get_set_delete_scan(self, backend, client):
        idx = f"{PREFIX}:_confidence:data"
        assert backend.map_set(idx, "m1", b"\x01") is True
        assert backend.map_set(idx, "m1", b"\x02", only_if_absent=True) is False
        assert backend.map_get(idx, "m1") == client.hget(idx, "m1") == b"\x01"
        assert backend.map_get(idx, "nope") is None
        backend.map_set(idx, "other", b"\x03")
        assert backend.map_scan(idx) == {"m1": b"\x01", "other": b"\x03"}
        assert backend.map_scan(idx, "m*") == {"m1": b"\x01"}
        # protocol-2: ``count`` is the HSCAN batch hint, not a result limit.
        assert backend.map_scan(idx, count=1) == {"m1": b"\x01", "other": b"\x03"}
        assert backend.map_scan(idx, count=1000) == backend.map_scan(idx)
        assert backend.map_delete(idx, "m1") == 1
        assert client.hexists(idx, "m1") is False


class TestSwapsAndPurge:
    def test_swap_index_keeps_the_pointer_and_enforces_uniqueness(
        self, backend, client
    ):
        record = f"{PREFIX}:User:1"
        idx_a, idx_b = f"$KeyF:{PREFIX}:User:email:a", f"$KeyF:{PREFIX}:User:email:b"
        client.hset(record, mapping={b"email": msgpack.packb("a")})
        backend.swap_index(record, "email", idx_a, msgpack.packb("a"), unique=True)
        assert client.smembers(idx_a) == {record.encode()}
        assert client.get(f"$IdxPtr:{record}:email") == idx_a.encode()

        backend.swap_index(record, "email", idx_b, msgpack.packb("b"), unique=True)
        assert client.smembers(idx_a) == set()
        assert client.smembers(idx_b) == {record.encode()}

        other = f"{PREFIX}:User:2"
        client.hset(other, mapping={b"email": msgpack.packb("b")})
        with pytest.raises(ModelException):
            backend.swap_index(other, "email", idx_b, msgpack.packb("b"), unique=True)

        assert backend.drop_index_entry(record, "email", fallback_idx=idx_a) == 1
        assert client.smembers(idx_b) == set()
        assert client.get(f"$IdxPtr:{record}:email") is None

    def test_purge_orphan_only_when_the_record_is_gone(self, backend, client):
        record, zidx, sidx = f"{PREFIX}:M:1", f"{PREFIX}:_z", f"{PREFIX}:_s"
        client.zadd(zidx, {record: 1.0})
        client.sadd(sidx, record)
        client.hset(record, "f", "v")
        assert backend.purge_orphan(record, [(zidx, "sorted"), (sidx, "set")]) == 0
        client.delete(record)
        assert backend.purge_orphan(record, [(zidx, "sorted"), (sidx, "set")]) == 2
        assert client.zcard(zidx) == 0 and client.scard(sidx) == 0

    def test_scan_index_members(self, backend, client):
        zidx, sidx = f"{PREFIX}:_z", f"{PREFIX}:_s"
        client.zadd(zidx, {"a": 1, "b": 2})
        client.sadd(sidx, "x", "y")
        assert sorted(backend.scan_index_members(zidx, "sorted")) == ["a", "b"]
        assert sorted(backend.scan_index_members(sidx, "set")) == ["x", "y"]

    def test_drop_index_removes_the_whole_index(self, backend, client):
        # protocol-1: rebuild_indexes step 1, on every index kind.
        zidx, sidx, midx = f"{PREFIX}:_z", f"{PREFIX}:_s", f"{PREFIX}:_m"
        client.zadd(zidx, {"a": 1})
        client.sadd(sidx, "x")
        client.hset(midx, "h", "k")
        assert backend.drop_index(zidx, "sorted") == 1
        assert backend.drop_index(sidx, "set") == 1
        assert backend.drop_index(midx, "map") == 1
        assert not client.exists(zidx, sidx, midx)
        assert backend.drop_index(zidx, "sorted") == 0
        uow = backend.begin()
        client.sadd(sidx, "x")
        assert backend.drop_index(sidx, "set", uow=uow) is None
        assert client.exists(sidx), "nothing may be dropped before commit()"
        uow.commit()
        assert not client.exists(sidx)
