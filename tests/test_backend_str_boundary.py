"""The field and query layers hand the backend ``str``, never ``bytes``
(#631 WS1f).

The protocol types every ``key`` / ``member`` / ``idx`` / ``class_set`` /
``obsolete_key`` parameter ``str`` and decodes every set / sorted / scan
*return* to ``str`` (#732 deviation 11). The query layer deliberately kept its
internal shape as raw ``bytes`` -- ``Query.keys()``, ``filter_for_keys_set()``
and the ``_members_as_bytes`` re-encode at each field boundary (WS1b/WS1c), so
a filter composed across families still intersects -- which means a ``bytes``
key can reach a backend call from ``Query.all()`` / ``filter()`` hydration,
``get_many()``, the composite-index side map, or a caller-supplied
``redis_key``. Redis never noticed (a UTF-8 ``str`` and the same ``bytes`` are
one wire token); Postgres rejects it with ``operator does not exist: text =
bytea`` (#748 / #750 integration proofs: 88-112 failures across the slice).

This file is the guard. :class:`StrictStrBackend` wraps a real
``RedisBackend`` and, for every protocol method, checks each argument bound to
a ``str``-typed parameter (derived from the ``Backend`` protocol's own
annotations, so a new method is covered without editing this file) and raises
``TypeError`` on anything that is not ``str``. Every routed entry point is then
driven through it along the paths that are known to carry ``bytes``. A mutant
that drops one ``as_key_str`` / ``as_key_strs`` at a call boundary fails the
test named for that routed file; the Redis leg of the real suite would not
notice, which is why these tests exist.

Everything here is Redis-bound by construction (the strict backend delegates to
``RedisBackend``), so the file carries no ``conformance`` mark.
"""

from __future__ import annotations

import collections.abc
import inspect
import time
import types
import typing

import pytest

import popoto
from popoto import backends
from popoto.backends import Backend, as_key_str, as_key_strs
from popoto.backends import redis as redis_backend
from popoto.backends.redis import RedisBackend
from popoto.fields.confidence_field import ConfidenceField
from popoto.fields.supersession import SupersessionProtocol
from popoto.fields.validity_field import ValidityField
from popoto.models.q import Q

# -- The guard -----------------------------------------------------------------


def _str_params() -> dict[str, dict[str, str]]:
    """``{method: {param: "scalar" | "seq"}}`` for every ``str``-typed
    parameter of the ``Backend`` protocol, read from its annotations."""
    out: dict[str, dict[str, str]] = {}
    for name, member in vars(Backend).items():
        if name.startswith("_") or not callable(member):
            continue
        hints = typing.get_type_hints(member)
        spec: dict[str, str] = {}
        for pname, hint in hints.items():
            if pname == "return":
                continue
            if hint is str:
                spec[pname] = "scalar"
                continue
            origin = typing.get_origin(hint)
            args = typing.get_args(hint)
            if origin in (typing.Union, types.UnionType) and set(args) == {
                str,
                type(None),
            }:
                spec[pname] = "scalar"
            elif origin in (collections.abc.Sequence, list, tuple) and args == (str,):
                spec[pname] = "seq"
        if spec:
            out[name] = spec
    return out


STR_PARAMS = _str_params()


class StrictStrBackend:
    """Delegates to ``RedisBackend``; refuses a non-``str`` where the protocol
    says ``str``. Records the method names so a test can also assert the
    entry point actually reached the seam."""

    def __init__(self) -> None:
        self._real = RedisBackend()
        self.calls: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr
        spec = STR_PARAMS.get(name, {})
        signature = inspect.signature(attr)

        def _checked(*args, **kwargs):
            self.calls.append(name)
            bound = signature.bind(*args, **kwargs)
            for pname, kind in spec.items():
                if pname not in bound.arguments:
                    continue
                value = bound.arguments[pname]
                if kind == "scalar":
                    if value is not None and not isinstance(value, str):
                        raise TypeError(
                            f"{name}({pname}=) received {type(value).__name__} "
                            f"{value!r}; the protocol types it str (WS1f)"
                        )
                else:
                    for item in value:
                        if not isinstance(item, str):
                            raise TypeError(
                                f"{name}({pname}=) contains "
                                f"{type(item).__name__} {item!r}; the protocol "
                                "types it Sequence[str] (WS1f)"
                            )
            return attr(*args, **kwargs)

        return _checked


@pytest.fixture
def strict():
    previous = backends._BACKEND
    guard = StrictStrBackend()
    backends.set_backend(guard)  # type: ignore[arg-type]
    try:
        assert backends.get_backend() is guard
        yield guard
    finally:
        backends.set_backend(previous)


# -- Models ----------------------------------------------------------------------


class BoundaryProbe(popoto.Model):
    name = popoto.KeyField()
    room = popoto.KeyField()
    rank = popoto.SortedField(type=int)
    status = popoto.IndexedField(type=str, null=True)
    email = popoto.UniqueField(type=str)
    tags = popoto.TagField()
    note = popoto.Field(type=str, default="")


class BoundaryComposite(popoto.Model):
    a = popoto.KeyField()
    b = popoto.Field(type=str, default="")
    c = popoto.Field(type=str, default="")

    class Meta:
        indexes = [(("b", "c"), True)]


class BoundaryCounted(popoto.Model):
    key = popoto.KeyField()
    n = popoto.SortedField(type=int, default=0)
    seen = popoto.DecayingSortedField(type=float, default=0.0)


class BoundaryValidFact(popoto.Model):
    subject = popoto.KeyField()
    text = popoto.Field(type=str, default="")
    validity = ValidityField(identity_fields=("subject",))


class BoundaryConfident(popoto.Model):
    key = popoto.KeyField()
    score = popoto.DecayingSortedField(type=float, default=1.0)
    confidence = ConfidenceField()


def seed(n: int = 6) -> list:
    return [
        BoundaryProbe.create(
            name=f"p{i}",
            room="a" if i % 2 else "b",
            rank=i,
            status="active" if i % 3 else "idle",
            email=f"e{i}@x",
            tags=["t0", f"t{i % 2}"],
            note=f"n{i}",
        )
        for i in range(n)
    ]


# -- The helper itself ------------------------------------------------------------


class TestHelper:
    def test_bytes_decode_str_passes_other_renders(self):
        assert as_key_str(b"M:1") == "M:1"
        assert as_key_str("M:1") == "M:1"
        assert as_key_str(7) == "7"
        assert as_key_strs([b"a", "b"]) == ["a", "b"]
        assert as_key_strs(k for k in (b"x",)) == ["x"]

    def test_redis_backend_reply_decoding_is_the_same_function(self):
        # One definition for both directions of the seam.
        assert redis_backend._as_str is as_key_str

    def test_the_guard_covers_the_key_parameters(self):
        # The guard derives its table from the protocol; pin that it saw
        # the parameters this file is about, so a protocol annotation drift
        # cannot silently blind it.
        assert STR_PARAMS["load_records"] == {"keys": "seq"}
        assert STR_PARAMS["load_fields_many"]["keys"] == "seq"
        assert STR_PARAMS["records_exist"] == {"keys": "seq"}
        assert STR_PARAMS["load_record"] == {"key": "scalar"}
        assert STR_PARAMS["record_exists"] == {"key": "scalar"}
        assert STR_PARAMS["save_record"]["key"] == "scalar"
        assert STR_PARAMS["save_record"]["class_set"] == "scalar"
        assert STR_PARAMS["save_record"]["obsolete_key"] == "scalar"
        assert STR_PARAMS["index_add"] == {"idx": "scalar", "member": "scalar"}
        assert STR_PARAMS["sorted_add"] == {"idx": "scalar", "member": "scalar"}
        assert STR_PARAMS["interval_of"]["member"] == "scalar"
        assert STR_PARAMS["swap_index"]["record_key"] == "scalar"
        assert STR_PARAMS["swap_tags"]["new_idxs"] == "seq"

    def test_the_guard_is_live(self, strict):
        # Non-vacuity: the guard rejects bytes on its own, so a green test
        # below means the field layer sent str, not that nothing checked.
        with pytest.raises(TypeError, match="load_records.*bytes"):
            strict.load_records([b"BoundaryProbe:x:y"])
        with pytest.raises(TypeError, match="load_record.*bytes"):
            strict.load_record(b"BoundaryProbe:x:y")
        with pytest.raises(TypeError, match="record_exists.*bytes"):
            strict.record_exists(b"BoundaryProbe:x:y")
        with pytest.raises(TypeError, match="records_exist.*bytes"):
            strict.records_exist(["ok", b"BoundaryProbe:x:y"])
        with pytest.raises(TypeError, match="interval_of.*bytes"):
            strict.interval_of("a", "b", b"m")
        # and lets str through to the real backend
        assert strict.load_records(["BoundaryProbe:x:y"]) == [None]
        assert strict.record_exists("BoundaryProbe:x:y") is False


# -- models/query.py -----------------------------------------------------------------


class TestQueryPy:
    """Every hydration path: the keys are the raw ``bytes`` the query layer
    intersects, decoded at ``load_records`` / ``load_fields_many``."""

    def test_all_and_delete_all_hydrate_with_str_keys(self, strict):
        seed(4)
        keys = BoundaryProbe.query.keys()
        assert keys and all(isinstance(k, bytes) for k in keys), "shape kept"
        assert len(BoundaryProbe.query.all()) == 4
        assert "load_records" in strict.calls
        assert len(BoundaryProbe.query.all(order_by="-rank", limit=2)) == 2
        assert BoundaryProbe.delete_all() == 4
        assert BoundaryProbe.query.count() == 0

    def test_values_projection_hands_str_keys_to_load_fields_many(self, strict):
        seed(3)
        rows = BoundaryProbe.query.all(values=("note", "name"))
        assert sorted(r["note"] for r in rows) == ["n0", "n1", "n2"]
        assert "load_fields_many" in strict.calls
        rows = list(BoundaryProbe.query.filter(rank__gte=1, values=("note",)))
        assert sorted(r["note"] for r in rows) == ["n1", "n2"]
        # key-only projection reads nothing
        rows = BoundaryProbe.query.all(values=("name", "room"))
        assert len(rows) == 3

    @pytest.mark.parametrize(
        "lookup",
        [
            dict(rank__gte=1),
            dict(rank__gt=1, rank__lte=4),
            dict(room="a"),
            dict(room__in=["a", "b"]),
            dict(room__startswith="a"),
            dict(room__isnull=False),
            dict(status="active"),
            dict(status__in=["active", "idle"]),
            dict(status__isnull=False),
            dict(status__startswith="act"),
            dict(email="e1@x"),
            dict(tags__contains="t1"),
            dict(tags__any=["t1", "t0"]),
            dict(tags__all=["t0", "t1"]),
            dict(rank__gte=1, room="a"),
            dict(rank__gte=0, room="b", status="active"),
            dict(room="a", tags__contains="t1"),
            dict(room="a", note="n1"),
            dict(rank__gte=1, limit=2, order_by="-rank"),
        ],
        ids=lambda d: "+".join(d),
    )
    def test_every_filter_shape_hydrates_with_str_keys(self, strict, lookup):
        seed(6)
        strict.calls.clear()
        rows = list(BoundaryProbe.query.filter(**lookup))
        assert rows, lookup
        assert "load_records" in strict.calls, (lookup, strict.calls)
        raw = BoundaryProbe.query.filter_for_keys_set(
            **{k: v for k, v in lookup.items() if k not in ("limit", "order_by")}
        )
        assert all(isinstance(k, bytes) for k in raw), "query layer shape kept"

    def test_q_objects_and_negation_hydrate_with_str_keys(self, strict):
        seed(6)
        assert len(BoundaryProbe.query.filter(Q(room="a") | Q(rank__gte=4))) == 4
        assert len(BoundaryProbe.query.filter(~Q(room="a"))) == 3
        assert len(BoundaryProbe.query.filter(Q(room="a") & ~Q(status="idle"))) == 2

    def test_count_with_a_client_side_filter_hydrates_with_str_keys(self, strict):
        seed(6)
        assert BoundaryProbe.query.count(rank__gte=1, note="n1") == 1
        assert BoundaryProbe.query.count(rank__gte=1) == 5

    def test_get_and_get_many_accept_the_bytes_keys_keys_hands_out(self, strict):
        rows = seed(3)
        keys = sorted(BoundaryProbe.query.keys())
        assert isinstance(keys[0], bytes)
        got = BoundaryProbe.query.get(redis_key=keys[0])
        assert got is not None and got.pk == keys[0].decode()
        assert BoundaryProbe.query.get(redis_key=b"BoundaryProbe:zz:a") is None
        many = BoundaryProbe.query.get_many(redis_keys=keys)
        assert sorted(o.pk for o in many) == sorted(o.pk for o in rows)
        many = BoundaryProbe.query.get_many(
            redis_keys=[keys[0], b"BoundaryProbe:zz:a"], skip_none=True
        )
        assert [o.pk for o in many] == [keys[0].decode()]
        # str keys keep working too
        assert BoundaryProbe.query.get(redis_key=rows[0].pk).pk == rows[0].pk

    def test_an_orphaned_member_is_purged_through_str_keys(self, strict):
        seed(3)
        popoto.get_redis().delete("BoundaryProbe:p1:a")
        assert len(BoundaryProbe.query.all()) == 2
        assert "purge_orphan" in strict.calls
        assert len(BoundaryProbe.query.filter(rank__gte=0)) == 2


# -- models/base.py ----------------------------------------------------------------


class TestModelsBasePy:
    def test_exists_load_raw_hash_and_load_fields_accept_bytes_keys(self, strict):
        rows = seed(2)
        key = sorted(BoundaryProbe.query.keys())[0]
        assert isinstance(key, bytes)
        assert BoundaryProbe.exists(redis_key=key) is True
        assert BoundaryProbe.exists(redis_key=b"BoundaryProbe:zz:a") is False
        assert BoundaryProbe.exists(key.decode()) is True
        raw = BoundaryProbe.load_raw_hash(key)
        assert raw and all(isinstance(k, bytes) for k in raw)
        assert BoundaryProbe.load_fields(key, "note")["note"] == "n0"
        assert set(BoundaryProbe.load_fields(key, "note", "rank")) == {"note", "rank"}
        assert rows[0].pk == key.decode()

    def test_save_update_partial_and_delete_send_str(self, strict):
        p = BoundaryProbe.create(
            name="x", room="r", rank=1, status="s", email="x@x", tags=["t"]
        )
        p.status = "s2"
        p.note = "n"
        p.save()
        p.note = "n2"
        p.save(update_fields=["note"])
        p.status = "s3"
        p.save(update_fields=["status"])
        pipe = popoto.get_redis().pipeline()
        p.note = "n3"
        p.save(pipeline=pipe)
        pipe.execute()
        assert BoundaryProbe.query.get(name="x", room="r").note == "n3"
        assert p.delete() is True
        assert BoundaryProbe.query.count() == 0
        for name in ("save_record", "set_expiry", "delete_record", "record_exists"):
            assert name in strict.calls, name

    def test_key_migration_sends_str_obsolete_key(self, strict):
        c = BoundaryCounted.create(key="k1", n=1)
        c.key = "k2"
        c.save(migrate_key=True)
        assert BoundaryCounted.query.get(key="k2") is not None
        assert BoundaryCounted.query.get(key="k1") is None
        assert BoundaryCounted.query.count() == 1

    def test_composite_index_maintenance_sends_str_keys(self, strict):
        BoundaryComposite.create(a="1", b="x", c="y")
        BoundaryComposite.create(a="2", b="x", c="z")
        with pytest.raises(popoto.ModelException):
            BoundaryComposite.create(a="3", b="x", c="y")
        assert BoundaryComposite.check_indexes()["total"] == 0
        assert BoundaryComposite.clean_indexes() == 0
        # Orphan a record behind the side map: its map value is the record
        # key as *bytes* (map_set stores the encoded key), and both
        # maintenance paths hand those values to records_exist.
        popoto.get_redis().delete("BoundaryComposite:1")
        strict.calls.clear()
        report = BoundaryComposite.check_indexes()
        assert sum(report["composite_indexes"].values()) == 1
        assert "records_exist" in strict.calls
        removed = BoundaryComposite.clean_indexes()
        assert removed >= 1
        assert BoundaryComposite.check_indexes()["total"] == 0
        assert len(BoundaryComposite.query.all()) == 1

    def test_rebuild_check_and_clean_indexes_send_str(self, strict):
        seed(3)
        popoto.get_redis().delete("BoundaryProbe:p2:b")
        assert BoundaryProbe.check_indexes()["total"] > 0
        assert BoundaryProbe.clean_indexes() > 0
        assert BoundaryProbe.check_indexes()["total"] == 0
        result = BoundaryProbe.rebuild_indexes()
        assert int(result) == 2 and not result.diverged_keys
        assert len(BoundaryProbe.query.filter(rank__gte=0)) == 2

    def test_atomic_increment_and_touch_send_str(self, strict):
        c = BoundaryCounted.create(key="k", n=1)
        assert c.atomic_increment("n", 2) == 3
        pipe = popoto.get_redis().pipeline()
        c.atomic_increment("n", 1, pipeline=pipe)
        pipe.execute()
        c.touch("seen")
        assert BoundaryCounted.query.get(key="k").n == 4
        assert {"increment_field", "sorted_increment", "sorted_add"} <= set(
            strict.calls
        )


# -- fields/* -------------------------------------------------------------------------


class TestKeyAndSortedFieldMixins:
    def test_key_and_sorted_hooks_and_lookups_send_str(self, strict):
        rows = seed(4)
        for lookup in (
            dict(room="a"),
            dict(room__in=["a"]),
            dict(room__in=[]),
            dict(room__startswith="a"),
            dict(room__endswith="b"),
            dict(room__isnull=False),
            dict(room__isnull=True),
            dict(name="p1"),
        ):
            list(BoundaryProbe.query.filter(**lookup))
        assert {"index_members", "index_union", "scan_record_keys"} <= set(strict.calls)
        p = rows[1]
        p.rank = 42
        p.save()
        assert [o.name for o in BoundaryProbe.query.filter(rank__gte=42)] == ["p1"]
        assert "sorted_range" in strict.calls
        field = BoundaryProbe._meta.fields["rank"]
        assert field.__class__.score(p, "rank") == 42
        assert field.__class__.count(BoundaryProbe, "rank") == 4
        assert len(field.__class__.members(BoundaryProbe, "rank")) == 4


class TestIndexedFieldMixinAndTagField:
    def test_swaps_drops_and_lookups_send_str(self, strict):
        p = BoundaryProbe.create(
            name="x", room="r", rank=1, status="s", email="x@x", tags=["a", "b"]
        )
        p.status = "s2"
        p.tags = ["b", "c"]
        p.save()
        p.status = None
        p.save()
        p.status = "s3"
        p.save(update_fields=["status"])
        pipe = popoto.get_redis().pipeline()
        p.tags = ["d"]
        p.save(pipeline=pipe)
        pipe.execute()
        assert [o.name for o in BoundaryProbe.query.filter(status="s3")] == ["x"]
        assert [o.name for o in BoundaryProbe.query.filter(tags__contains="d")] == ["x"]
        assert BoundaryProbe.query.filter(status__isnull=False).count() == 1
        assert [o.name for o in BoundaryProbe.query.filter(email="x@x")] == ["x"]
        assert [o.name for o in BoundaryProbe.query.filter(tags__all=["d"])] == ["x"]
        assert BoundaryProbe.query.filter(tags__any=["zz", "d"]).count() == 1
        q = BoundaryProbe(name="y", room="r", rank=2, status="s3", email="x@x")
        with pytest.raises(popoto.ModelException, match="(?i)unique"):
            q.save()
        assert p.delete() is True
        assert {
            "swap_index",
            "swap_tags",
            "drop_index_entry",
            "drop_tag_entries",
            "index_intersection",
        } <= set(strict.calls)


class TestValidityFieldAndSupersession:
    def test_validity_paths_send_str_members(self, strict):
        identity = SupersessionProtocol.identity_key("s", "text")
        a = BoundaryValidFact.create(subject="s", text="one")
        SupersessionProtocol.supersede(a, identity_key=identity)
        b = BoundaryValidFact(subject="s2", text="two")
        SupersessionProtocol.save_and_supersede(b, identity_key=identity)
        c = BoundaryValidFact(subject="s3", text="three")
        SupersessionProtocol.save_and_invalidate(c, closes=b)
        current = BoundaryValidFact.query.filter(validity__current=True)
        assert [o.subject for o in current] == ["s3"]
        closed = BoundaryValidFact.query.filter(validity__current=False)
        assert sorted(o.subject for o in closed) == ["s", "s2"]
        assert len(BoundaryValidFact.query.filter(validity__as_of=time.time())) == 1
        key = sorted(BoundaryValidFact.query.keys())[0]
        assert isinstance(key, bytes)
        assert ValidityField.is_valid_at(BoundaryValidFact, "validity", key) is False
        assert (
            ValidityField.get_valid_from(BoundaryValidFact, "validity", member_key=key)
            is not None
        )
        assert ValidityField.resolve_valid_keys(BoundaryValidFact, "validity") == {c.pk}
        assert ValidityField.resolve_excluded_keys(BoundaryValidFact, "validity") == {
            a.pk,
            b.pk,
        }
        assert [o.subject for o in SupersessionProtocol.chain(b)] == ["s", "s2", "s3"]
        assert SupersessionProtocol.superseded_by(a).subject == "s2"
        assert SupersessionProtocol.supersedes(b).subject == "s"
        SupersessionProtocol.invalidate(c)
        assert a.delete() is True
        assert BoundaryValidFact.delete_all() == 2
        assert {
            "supersede",
            "interval_members",
            "interval_of",
            "map_get",
            "drop_validity",
            "sorted_members",
        } <= set(strict.calls)


class TestDecayingSortedAndConfidenceFields:
    def test_decay_and_confidence_paths_send_str(self, strict):
        for i in range(3):
            BoundaryConfident.create(key=f"k{i}", score=float(i + 1))
        ranked = BoundaryConfident.query.top_by_decay("score", n=2)
        assert len(ranked) == 2 and {o.key for o in ranked} <= {"k0", "k1", "k2"}
        inst = BoundaryConfident.query.get(key="k0")
        ConfidenceField.update_confidence(inst, "confidence", 1.0)
        assert ConfidenceField.get_confidence(inst, "confidence") > 0
        pipe = popoto.get_redis().pipeline()
        ConfidenceField.update_confidence(inst, "confidence", 0.0, pipeline=pipe)
        pipe.execute()
        inst.touch("score")
        assert inst.delete() is True
        assert BoundaryConfident.delete_all() == 2
        assert {"decayed_rank", "confidence_update", "map_get", "map_delete"} <= set(
            strict.calls
        )


# -- The unit of work is tested for presence, never truthiness -------------------


class FalsyWhileEmpty:
    """A unit of work that is falsy until something is queued.

    ``PostgresUnitOfWork`` defines ``__len__``, so an empty one is falsy;
    redis-py's ``Pipeline.__bool__`` is unconditionally ``True``, which is
    why ``if pipeline:`` / ``pipeline if pipeline else None`` never showed on
    Redis. This proxy gives the real ``GuardedPipeline`` the Postgres
    truthiness so the Redis leg can prove architect decision 1
    (``uow is not None``) on its own.
    """

    def __init__(self) -> None:
        self._real = popoto.get_redis().pipeline()

    def __bool__(self) -> bool:
        return len(self._real) > 0

    def __len__(self) -> int:
        return len(self._real)

    def __getattr__(self, name):
        return getattr(self._real, name)


class BoundaryAuto(popoto.Model):
    # Declared, not implicit: an explicit AutoKeyField is in ``_meta.fields``,
    # so its hooks run through KeyFieldMixin's ``auto`` early-return -- the
    # branch that returned None on a falsy unit of work.
    # The plain Field is declared first so Model.save/delete reach the base
    # ``Field.on_save``/``on_delete`` while the unit of work is still empty.
    note = popoto.Field(type=str, default="")
    id = popoto.AutoKeyField()
    rank = popoto.SortedField(type=int, default=0)
    status = popoto.IndexedField(type=str, null=True)
    tags = popoto.TagField()


class TestUnitOfWorkIsTestedForPresence:
    def test_redis_pipeline_is_always_truthy_so_this_file_must_fake_it(self):
        assert bool(popoto.get_redis().pipeline()) is True
        assert bool(FalsyWhileEmpty()) is False

    def test_save_on_an_empty_unit_of_work_queues_every_hook(self, strict):
        uow = FalsyWhileEmpty()
        obj = BoundaryAuto(rank=1, status="s", tags=["t"], note="n")
        returned = obj.save(pipeline=uow)
        assert returned is uow, "save must hand the caller's unit of work back"
        assert len(uow) > 0, "nothing was queued: a hook ran outside the uow"
        # Nothing landed yet: the record is invisible until the caller commits.
        assert BoundaryAuto.query.count() == 0
        uow.execute()
        assert BoundaryAuto.query.count() == 1
        assert [o.note for o in BoundaryAuto.query.filter(status="s")] == ["n"]
        assert [o.note for o in BoundaryAuto.query.filter(tags__contains="t")] == ["n"]
        assert [o.note for o in BoundaryAuto.query.filter(rank__gte=1)] == ["n"]

    def test_partial_save_on_an_empty_unit_of_work_queues(self, strict):
        obj = BoundaryAuto.create(rank=1, status="s", note="n")
        uow = FalsyWhileEmpty()
        obj.note = "n2"
        assert obj.save(update_fields=["note"], pipeline=uow) is uow
        assert len(uow) > 0
        assert BoundaryAuto.query.get(redis_key=obj.pk).note == "n"
        uow.execute()
        assert BoundaryAuto.query.get(redis_key=obj.pk).note == "n2"
        assert obj.save(update_fields=[], pipeline=uow) is uow

    def test_delete_on_an_empty_unit_of_work_is_queued_not_executed(self, strict):
        obj = BoundaryAuto.create(rank=1, status="s", tags=["t"], note="n")
        uow = FalsyWhileEmpty()
        strict.calls.clear()
        returned = obj.delete(pipeline=uow)
        assert returned is uow, "delete must hand the caller's unit of work back"
        assert len(uow) > 0, "nothing was queued: delete ran in its own transaction"
        assert "record_exists" not in strict.calls, "that EXISTS is the no-uow path"
        assert BoundaryAuto.query.count() == 1, "deleted before the caller committed"
        uow.execute()
        assert BoundaryAuto.query.count() == 0
        assert BoundaryAuto.query.filter(status="s").count() == 0
        assert BoundaryAuto.query.filter(tags__contains="t").count() == 0

    def test_every_field_hook_returns_the_unit_of_work_it_was_given(self, strict):
        obj = BoundaryAuto.create(rank=1, status="s", tags=["t"], note="n")
        for field_name, field in obj._meta.fields.items():
            value = getattr(obj, field_name)
            # A fresh, still-empty (falsy) unit of work per hook: once one
            # hook has queued something the proxy is truthy and a
            # ``pipeline if pipeline else None`` mutant would pass it on.
            uow = FalsyWhileEmpty()
            assert (
                field.on_save(
                    obj, field_name=field_name, field_value=value, pipeline=uow
                )
                is uow
            ), f"{field_name}.on_save broke the hook chain"
            assert (
                field.on_delete(
                    obj, field_name=field_name, field_value=value, pipeline=uow
                )
                is uow
            ), f"{field_name}.on_delete broke the hook chain"
