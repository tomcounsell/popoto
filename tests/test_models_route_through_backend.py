"""Every M1 public entry point reaches storage through the model's backend
(#759 M1a).

A recording backend -- a ``RedisBackend`` subclass that logs each protocol
call and then does the real work -- is bound as the process default, and each
entry point is driven once. The assertion is on the *recorded calls*, which
is what makes this test non-vacuous: an entry point that went back to calling
``get_REDIS_DB()`` directly still returns the right answer (Redis is real
here) but leaves no record, and fails. The PR records the scratch mutants that
proved it (e.g. ``Model.exists`` reverted to a bare ``EXISTS``).

``TestSourceShape`` is the cheap second guard -- the public bodies hold no
``get_REDIS_DB``/``run_lua`` -- and is not sufficient on its own: a body can
reach Redis through a helper. The recording tests are the real guard.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from decimal import Decimal

import pytest

import popoto
from popoto import Q
from popoto.backends import set_backend
from popoto.backends.redis import RedisBackend
from popoto.models.base import Model
from popoto.models.query import Query, QueryBuilder


class RecordingBackend(RedisBackend):
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _record(self, method, *args, **kwargs):
        self.calls.append(method)
        return getattr(super(), method)(*args, **kwargs)

    def bind(self, *a, **kw):
        return self._record("bind", *a, **kw)

    def save(self, *a, **kw):
        return self._record("save", *a, **kw)

    def load(self, *a, **kw):
        return self._record("load", *a, **kw)

    def delete(self, *a, **kw):
        return self._record("delete", *a, **kw)

    def exists(self, *a, **kw):
        return self._record("exists", *a, **kw)

    def increment(self, *a, **kw):
        return self._record("increment", *a, **kw)

    def select(self, *a, **kw):
        return self._record("select", *a, **kw)

    def count(self, *a, **kw):
        return self._record("count", *a, **kw)


class RouteUser(popoto.Model):
    name = popoto.KeyField()
    org = popoto.KeyField()
    rank = popoto.SortedField(type=int, default=0)
    note = popoto.Field(type=str, null=True)
    hits = popoto.IntField(default=0)
    amount = popoto.DecimalField(null=True)


@pytest.fixture
def rec():
    backend = RecordingBackend()
    previous = set_backend(backend)
    RouteUser(name="ann", org="acme", rank=1, note="a").save()
    RouteUser(name="bob", org="beta", rank=2).save()
    backend.calls.clear()
    yield backend
    set_backend(previous)


def _only(rec, *expected):
    calls = [c for c in rec.calls if c != "bind"]
    for method in expected:
        assert method in calls, f"{method} not recorded; got {calls}"
    return calls


# -- records ------------------------------------------------------------------


def test_save_full(rec):
    RouteUser(name="cat", org="acme", rank=3).save()
    assert _only(rec, "save") == ["save"]


def test_save_partial_and_pipelined(rec):
    u = RouteUser.query.get(name="ann", org="acme")
    rec.calls.clear()
    u.note = "b"
    u.save(update_fields=["note"])
    pipe = popoto.get_redis().pipeline()
    assert u.save(pipeline=pipe) is pipe
    pipe.execute()
    assert _only(rec, "save") == ["save", "save"]


def test_create_goes_through_save(rec):
    RouteUser.create(name="dan", org="acme", rank=4)
    _only(rec, "save")


def test_delete(rec):
    u = RouteUser.query.get(name="bob", org="beta")
    rec.calls.clear()
    assert u.delete() is True
    pipe = popoto.get_redis().pipeline()
    assert u.delete(pipeline=pipe) is pipe
    pipe.execute()
    assert _only(rec, "delete") == ["delete", "delete"]


def test_exists(rec):
    assert RouteUser.exists(name="ann", org="acme") is True
    assert RouteUser.exists("RouteUser:zzz:acme") is False
    assert _only(rec, "exists") == ["exists", "exists"]


def test_atomic_increment(rec):
    u = RouteUser.query.get(name="ann", org="acme")
    rec.calls.clear()
    assert u.atomic_increment("hits", 2) == 2
    assert u.atomic_increment("amount", Decimal("1.5")) == Decimal("1.5")
    pipe = popoto.get_redis().pipeline()
    assert u.atomic_increment("rank", 1, pipeline=pipe) is pipe
    pipe.execute()
    assert _only(rec, "increment") == ["increment"] * 3


def test_get_and_load(rec):
    assert RouteUser.query.get(name="ann", org="acme").note == "a"
    assert RouteUser.query.get("RouteUser:zzz:acme") is None
    assert RouteUser.load(name="ann", org="acme") is not None
    assert _only(rec, "load") == ["load"] * 3


def test_get_many(rec):
    out = RouteUser.query.get_many(["RouteUser:ann:acme", "RouteUser:zzz:acme"])
    assert [o.name if o else None for o in out] == ["ann", None]
    assert _only(rec, "load") == ["load"]


def test_get_many_objects_and_projection(rec):
    keys = RouteUser.query.keys()
    rec.calls.clear()
    assert len(Query.get_many_objects(RouteUser, set(keys))) == 2
    rows = Query.get_many_objects(RouteUser, set(keys), values=("note",))
    assert sorted(str(r["note"]) for r in rows) == ["None", "a"]
    assert _only(rec, "load") == ["load", "load"]


def test_load_fields(rec):
    assert RouteUser.load_fields("RouteUser:ann:acme", "note") == {"note": "a"}
    assert RouteUser.load_fields("RouteUser:ann:acme", "note", "rank") == {
        "note": "a",
        "rank": 1,
    }
    assert _only(rec, "load") == ["load", "load"]


# -- query --------------------------------------------------------------------


def test_filter(rec):
    assert [u.name for u in RouteUser.query.filter(org="acme")] == ["ann"]
    _only(rec, "select")


def test_filter_q_and_chain(rec):
    qb = RouteUser.query.filter(Q(org="acme") | Q(rank__gte=2))
    assert sorted(u.name for u in qb.order_by("rank").limit(5).all()) == [
        "ann",
        "bob",
    ]
    _only(rec, "select")


def test_values_projection(rec):
    assert RouteUser.query.filter(org="beta", values=("name",)).all() == [
        {"name": "bob"}
    ]
    _only(rec, "select")


def test_all_and_keys(rec):
    assert len(RouteUser.query.all()) == 2
    assert all(isinstance(k, bytes) for k in RouteUser.query.keys())
    calls = _only(rec, "select", "load")
    assert calls.count("select") == 2


def test_count(rec):
    assert RouteUser.query.count() == 2
    assert RouteUser.query.count(org="acme") == 1
    assert _only(rec, "count") == ["count", "count"]


def test_q_count_goes_through_select(rec):
    assert RouteUser.query.filter(Q(org="acme") | Q(org="beta")).count() == 2
    _only(rec, "select")


# -- source shape -------------------------------------------------------------

ROUTED = [
    (Model, "save"),
    (Model, "delete"),
    (Model, "exists"),
    (Model, "atomic_increment"),
    (Model, "load_fields"),
    (Model, "load"),
    (Query, "get"),
    (Query, "get_many"),
    (Query, "get_many_objects"),
    (Query, "_execute_filter"),
    (Query, "count"),
    (QueryBuilder, "_execute"),
    (QueryBuilder, "count"),
]


class TestSourceShape:
    @pytest.mark.parametrize(
        "owner,name", ROUTED, ids=lambda v: getattr(v, "__name__", v)
    )
    def test_routed_body_holds_no_redis_access(self, owner, name):
        fn = inspect.getattr_static(owner, name)
        fn = getattr(fn, "__func__", fn)
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        assert not names & {"get_REDIS_DB", "run_lua", "POPOTO_REDIS_DB"}
