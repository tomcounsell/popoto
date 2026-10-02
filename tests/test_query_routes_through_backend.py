"""The sorted, key and query slice reaches storage through ``get_backend()``
(#631 WS1b): ``fields/sorted_field_mixin.py``, ``fields/key_field_mixin.py``,
``fields/unique_field_mixin.py`` and ``models/query.py``.

Two guards, in opposite directions, mirroring WS1a's
``test_models_base_routes_through_backend.py``:

* **Behavioural.** A recording backend that delegates every call to the real
  ``RedisBackend`` is installed with ``set_backend()``. Each routed entry
  point must show up as a call on the *backend* -- ``SortedField.on_save`` as
  ``sorted_add``, ``filter(score__gt=...)`` as ``sorted_range``,
  ``KeyField.filter_query`` as ``index_members`` / ``index_union`` /
  ``scan_record_keys``, ``Query.get`` as ``load_record``, the ``values=``
  projection as ``load_fields_many`` -- so a site that quietly goes back to
  ``get_REDIS_DB()`` fails here even though Redis would still have done the
  right thing. Delegation keeps the exercised path real; the recorder only
  watches, and it also records arguments, which is how deviation 12 (bounds
  travel as numbers plus inclusivity flags, never Redis wire strings) is
  pinned at the protocol boundary rather than inferred from a wire trace.

* **Shape.** The source itself: no ``get_REDIS_DB()`` call, no ``run_lua(``,
  no ``isinstance(pipeline, redis...)``, and exactly as many ``native()``
  sites as the PR body ledgers, each with its naming comment. The count is
  pinned so a new escape-hatch site is a deliberate, ledgered addition.

Deviation 11 (the backend decodes members to ``str``) is handled the way
WS1c handles it: the *field boundary* re-encodes to ``bytes``, so every
field's ``filter_query`` result, ``Query.filter_for_keys_set`` and
``Query.keys`` keep the pre-seam ``bytes`` shape and a filter composed across
families intersects correctly whichever family merged first. The tests below
assert the field results byte-for-byte against the raw Redis replies, and
``test_a_str_producer_at_a_field_boundary_would_match_nothing`` records why
the convention is load-bearing until one later PR flips the whole layer.

The spy is installed per test and the previous binding restored in
``finally``, so nothing leaks into the rest of the session.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import popoto
from popoto import backends
from popoto.backends.redis import RedisBackend
from popoto.fields.key_field_mixin import KeyFieldMixin

PKG = Path(popoto.__file__).parent
OWNED = {
    "sorted_field_mixin": PKG / "fields" / "sorted_field_mixin.py",
    "key_field_mixin": PKG / "fields" / "key_field_mixin.py",
    "unique_field_mixin": PKG / "fields" / "unique_field_mixin.py",
    "query": PKG / "models" / "query.py",
}
# The PR body's native() ledger, per file.
NATIVE_LEDGER = {
    "sorted_field_mixin": 0,
    "key_field_mixin": 0,
    "unique_field_mixin": 0,
    "query": 9,
}


class RecordingBackend:
    """Delegates to a real ``RedisBackend``; records names and arguments."""

    def __init__(self) -> None:
        self._real = RedisBackend()
        self.calls: list[str] = []
        self.detail: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def _recorded(*args, **kwargs):
            self.calls.append(name)
            self.detail.append((name, args, kwargs))
            return attr(*args, **kwargs)

        return _recorded

    def args_of(self, name: str) -> list[tuple[tuple, dict]]:
        return [(a, kw) for n, a, kw in self.detail if n == name]

    def clear(self) -> None:
        self.calls.clear()
        self.detail.clear()


@pytest.fixture
def spy():
    previous = backends._BACKEND
    rec = RecordingBackend()
    backends.set_backend(rec)  # type: ignore[arg-type]
    try:
        assert backends.get_backend() is rec
        yield rec
    finally:
        backends.set_backend(previous)


class Probe(popoto.Model):
    name = popoto.KeyField()
    room = popoto.KeyField()
    score = popoto.SortedField(type=float, partition_by="room")
    rank = popoto.SortedField(type=int)
    note = popoto.Field(type=str, default="")


class NullProbe(popoto.Model):
    name = popoto.KeyField()
    kind = popoto.KeyField(null=True)


class AutoProbe(popoto.Model):
    id = popoto.AutoKeyField()
    label = popoto.KeyField()


class TrackedProbe(popoto.AccessTrackerMixin, popoto.Model):
    name = popoto.KeyField()
    rank = popoto.SortedField(type=int)


def seed(n: int = 5) -> list[Probe]:
    return [
        Probe.create(
            name=f"p{i}",
            room="a" if i % 2 else "b",
            score=float(i),
            rank=i,
            note="hit" if i < 3 else "miss",
        )
        for i in range(n)
    ]


def b(*keys: str) -> set[bytes]:
    return {k.encode() for k in keys}


# -- Sorted field --------------------------------------------------------------


class TestSortedFieldReachesTheBackend:
    def test_on_save_goes_through_sorted_add(self, spy):
        Probe.create(name="s", room="a", score=1.0, rank=1)
        assert spy.calls.count("sorted_add") == 2, spy.calls  # score and rank
        assert "index_add" in spy.calls  # the key fields, same save

    def test_on_save_on_a_caller_pipeline_queues_and_returns_it(self, spy):
        obj = Probe.create(name="q", room="a", score=1.0, rank=1)
        field = Probe._meta.fields["rank"]
        pipe = spy.begin()
        spy.clear()
        returned = field.__class__.on_save(obj, "rank", 7, pipeline=pipe)
        assert returned is pipe, "the caller's unit of work comes back"
        assert spy.calls == ["sorted_add"], spy.calls
        (args, kwargs), *_ = spy.args_of("sorted_add")
        assert kwargs["uow"] is pipe
        assert field.score(obj, "rank") == 1.0, "nothing lands before commit()"
        pipe.commit()
        assert field.score(obj, "rank") == 7.0

    def test_partition_change_goes_through_sorted_remove_on_the_old_index(self, spy):
        obj = Probe.create(name="m", room="a", score=2.0, rank=2)
        old_idx = Probe._meta.fields["score"].get_partitioned_sortedset_db_key(
            obj, "score"
        )
        spy.clear()
        obj.room = "b"
        obj.save(migrate_key=True)
        removed = [a[0] for a, _ in spy.args_of("sorted_remove")]
        assert old_idx.redis_key in removed, (removed, spy.calls)
        assert Probe.query.filter(score__gte=0, room="a") == []
        assert [o.name for o in Probe.query.filter(score__gte=0, room="b")] == ["m"]

    def test_on_delete_goes_through_sorted_remove(self, spy):
        obj = Probe.create(name="d", room="a", score=1.0, rank=1)
        spy.clear()
        obj.delete()
        assert spy.calls.count("sorted_remove") == 2, spy.calls
        assert "index_remove" in spy.calls

    def test_count_members_and_score_go_through_the_backend(self, spy):
        rows = seed(4)
        field = Probe._meta.fields["score"]
        inst = Probe(name="x", room="a")
        spy.clear()
        assert field.count(inst, "score") == 2
        assert spy.calls == ["sorted_count"], spy.calls
        spy.clear()
        got = field.members(inst, "score")
        assert got == [rows[1].pk, rows[3].pk]  # #634: str, by contract
        assert all(isinstance(k, str) for k in got)
        assert spy.calls == ["sorted_members"], spy.calls
        spy.clear()
        assert field.members(inst, "score", 0, 0, reverse=True) == [rows[3].pk]
        assert spy.args_of("sorted_members")[0][1] == {"reverse": True}
        spy.clear()
        assert field.score(rows[3], "score") == 3.0
        assert spy.calls == ["sorted_score"], spy.calls

    def test_filter_query_hands_the_backend_numbers_and_flags(self, spy):
        # Deviation 12: no "(5.0" / "-inf" strings cross the seam; the backend
        # renders them. The int stays an int so the rendered bound is the
        # pre-seam "2", not "2.0".
        seed(6)
        field = Probe._meta.fields["rank"]
        cases = {
            ("rank__gt", 2): (2, float("inf"), False, True),
            ("rank__gte", 2): (2, float("inf"), True, True),
            ("rank__lt", 2): (float("-inf"), 2, True, False),
            ("rank__lte", 2): (float("-inf"), 2, True, True),
            ("rank", 2): (2, 2, True, True),
            ("rank__between", (1, 3)): (1, 3, True, True),
        }
        for (param, value), (lo, hi, lo_inc, hi_inc) in cases.items():
            spy.clear()
            got = field.__class__.filter_query(Probe, "rank", **{param: value})
            assert spy.calls == ["sorted_range"], (param, spy.calls)
            (args, kwargs), *_ = spy.args_of("sorted_range")
            assert args[1:] == (lo, hi), (param, args)
            assert type(args[1]) is type(lo) and type(args[2]) is type(hi), param
            assert (kwargs["lo_inclusive"], kwargs["hi_inclusive"]) == (
                lo_inc,
                hi_inc,
            ), param
            assert kwargs["reverse"] is False and kwargs["limit"] is None
            assert all(isinstance(k, bytes) for k in got), param

    def test_pushdown_passes_limit_and_reverse_through_sorted_range(self, spy):
        seed(6)
        spy.clear()
        rows = Probe.query.filter(rank__gte=1, limit=2, order_by="-rank")
        assert [o.rank for o in rows] == [5, 4]
        (args, kwargs), *_ = spy.args_of("sorted_range")
        assert kwargs["reverse"] is True
        margin = popoto.fields.constants.Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN
        assert kwargs["limit"] == 2 + margin
        assert "load_records" in spy.calls, spy.calls

    def test_filter_query_result_is_the_raw_reply_byte_for_byte(self, spy):
        # Deviation 11, sorted side: the backend decodes, the field boundary
        # re-encodes, and what the Query layer sees is exactly the pre-seam
        # ZRANGEBYSCORE reply -- same members, same order, same type.
        seed(6)
        field = Probe._meta.fields["rank"]
        idx = field.get_sortedset_db_key(Probe, "rank").redis_key
        raw = popoto.get_redis().zrangebyscore(idx, "(1", "4")
        got = field.__class__.filter_query(Probe, "rank", rank__gt=1, rank__lte=4)
        assert got == raw
        assert len(got) == 3 and all(isinstance(k, bytes) for k in got)


# -- Key field -----------------------------------------------------------------


class TestKeyFieldReachesTheBackend:
    def test_on_save_goes_through_index_add(self, spy):
        obj = Probe.create(name="k", room="a", score=1.0, rank=1)
        added = [a[0] for a, _ in spy.args_of("index_add")]
        assert "$KeyF:Probe:name:k" in added and "$KeyF:Probe:room:a" in added
        assert all(a[1] == obj.pk for a, _ in spy.args_of("index_add"))

    def test_key_mutation_goes_through_index_remove_then_index_add(self, spy):
        obj = NullProbe.create(name="n", kind="x")
        spy.clear()
        obj.kind = "y"
        obj.save(migrate_key=True)
        assert "index_remove" in spy.calls and "index_add" in spy.calls
        assert spy.calls.index("index_remove") < spy.calls.index("index_add")
        assert [o.name for o in NullProbe.query.filter(kind="y")] == ["n"]
        assert NullProbe.query.filter(kind="x") == []

    def test_on_delete_goes_through_index_remove(self, spy):
        obj = NullProbe.create(name="n", kind="x")
        spy.clear()
        obj.delete()
        assert "index_remove" in spy.calls, spy.calls
        assert NullProbe.query.filter(kind="x") == []

    def test_on_save_on_a_caller_pipeline_queues_and_returns_it(self, spy):
        obj = NullProbe.create(name="n", kind="x")
        field = NullProbe._meta.fields["kind"]
        pipe = spy.begin()
        spy.clear()
        assert field.__class__.on_save(obj, "kind", "x", pipeline=pipe) is pipe
        assert spy.calls == ["index_add"]
        assert spy.args_of("index_add")[0][1]["uow"] is pipe
        pipe.commit()

    def test_exact_and_in_go_through_index_members_and_index_union(self, spy):
        seed(4)
        spy.clear()
        got = KeyFieldMixin.filter_query(Probe, "room", room="a")
        assert spy.calls == ["index_members"], spy.calls
        assert got == b("Probe:p1:a", "Probe:p3:a")
        spy.clear()
        got = KeyFieldMixin.filter_query(Probe, "name", name__in=["p0", "p3", "zz"])
        assert spy.calls == ["index_union"], spy.calls
        assert got == b("Probe:p0:b", "Probe:p3:a")
        spy.clear()
        assert KeyFieldMixin.filter_query(Probe, "name", name__in=[]) == set()
        assert spy.calls == ["index_union"], spy.calls

    def test_isnull_true_is_index_members_and_false_is_scan_record_keys(self, spy):
        NullProbe.create(name="n1", kind="x")
        NullProbe.create(name="n2", kind=None)
        spy.clear()
        got = KeyFieldMixin.filter_query(NullProbe, "kind", kind__isnull=True)
        assert spy.calls == ["index_members"], spy.calls
        assert got == b("NullProbe:None:n2")
        spy.clear()
        got = KeyFieldMixin.filter_query(NullProbe, "kind", kind__isnull=False)
        assert spy.calls == ["scan_record_keys"], spy.calls
        assert got == b("NullProbe:x:n1")

    def test_pattern_and_auto_key_lookups_go_through_scan_record_keys(self, spy):
        seed(3)
        auto = AutoProbe.create(label="first")
        spy.clear()
        assert KeyFieldMixin.filter_query(Probe, "name", name__startswith="p") == b(
            "Probe:p0:b", "Probe:p1:a", "Probe:p2:b"
        )
        assert spy.calls == ["scan_record_keys"], spy.calls
        spy.clear()
        assert KeyFieldMixin.filter_query(Probe, "name", name__endswith="2") == b(
            "Probe:p2:b"
        )
        assert spy.calls == ["scan_record_keys"], spy.calls
        spy.clear()
        assert AutoProbe.query.get(id=auto.id).label == "first"
        assert spy.calls[0] == "scan_record_keys", spy.calls

    def test_filter_query_result_is_the_raw_reply_byte_for_byte(self, spy):
        # Deviation 11, set side.
        seed(4)
        raw = popoto.get_redis().smembers("$KeyF:Probe:room:b")
        got = KeyFieldMixin.filter_query(Probe, "room", room="b")
        assert got == raw == b("Probe:p0:b", "Probe:p2:b")


# -- Query ---------------------------------------------------------------------


class TestQueryReachesTheBackend:
    def test_get_goes_through_load_record(self, spy):
        obj = Probe.create(name="g", room="a", score=1.0, rank=1)
        spy.clear()
        assert Probe.query.get(name="g", room="a").note == ""
        assert spy.calls == ["load_record"], spy.calls
        spy.clear()
        assert Probe.query.get(redis_key="Probe:absent:a") is None
        assert spy.calls == ["load_record"], spy.calls
        assert Probe.query.get(name="g", room="a").pk == obj.pk

    def test_get_many_goes_through_load_records(self, spy):
        rows = seed(2)
        spy.clear()
        got = Probe.query.get_many([rows[1].pk, "Probe:absent:a", rows[0].pk])
        assert spy.calls == ["load_records"], spy.calls
        assert [None if o is None else o.name for o in got] == ["p1", None, "p0"]

    def test_keys_all_and_count_go_through_list_keys_and_count_records(self, spy):
        rows = seed(3)
        spy.clear()
        keys = Probe.query.keys()
        assert spy.calls == ["list_keys"], spy.calls
        assert sorted(keys) == sorted(o.pk.encode() for o in rows)
        assert all(isinstance(k, bytes) for k in keys), "pre-seam shape kept"
        assert set(keys) == popoto.get_redis().smembers("$Class:Probe")
        spy.clear()
        assert len(Probe.query.all()) == 3
        assert spy.calls == ["list_keys", "load_records"], spy.calls
        spy.clear()
        assert Probe.query.count() == 3
        assert spy.calls == ["count_records"], spy.calls
        spy.clear()
        assert Probe.query.count(rank__gte=1) == 2
        assert spy.calls == ["sorted_range"], spy.calls

    def test_filter_hydrates_through_load_records(self, spy):
        seed(4)
        spy.clear()
        rows = Probe.query.filter(rank__gte=1, room="a")
        assert [o.name for o in rows] == ["p1", "p3"]
        assert spy.calls == ["sorted_range", "index_members", "load_records"], spy.calls

    def test_values_projection_goes_through_load_fields_many(self, spy):
        seed(4)
        spy.clear()
        rows = list(Probe.query.filter(rank__gte=2, values=("note", "name")))
        assert spy.calls == ["sorted_range", "load_fields_many"], spy.calls
        (args, _), *_ = spy.args_of("load_fields_many")
        assert args[1] == ["note", "name"]
        assert sorted(r["name"] for r in rows) == ["p2", "p3"]
        assert {r["note"] for r in rows} == {"hit", "miss"}
        spy.clear()
        # Key-only projection is parsed off the keys: no record read at all.
        rows = list(Probe.query.filter(rank__gte=2, values=("name",)))
        assert spy.calls == ["sorted_range"], spy.calls
        assert sorted(r["name"] for r in rows) == ["p2", "p3"]

    def test_filter_for_keys_set_is_the_raw_intersection_byte_for_byte(self, spy):
        seed(6)
        client = popoto.get_redis()
        raw_sorted = client.zrangebyscore("$SortF:Probe:rank", "1", "+inf")
        raw_set = client.smembers("$KeyF:Probe:room:a")
        got = Probe.query.filter_for_keys_set(rank__gte=1, room="a")
        assert (
            got
            == set(raw_sorted) & raw_set
            == b("Probe:p1:a", "Probe:p3:a", "Probe:p5:a")
        )

    def test_a_str_producer_at_a_field_boundary_would_match_nothing(
        self, spy, monkeypatch
    ):
        # Why the re-encode is load-bearing (WS1c's mutant, reproduced): the
        # Query layer intersects the fields' raw results as they are, so a
        # family that handed the backend's str through would make every
        # filter composed with another family match nothing. Each family
        # therefore re-encodes at its own boundary until one PR flips the
        # whole layer; this test is the record of that contract and goes
        # with that PR.
        seed(6)
        real = KeyFieldMixin.filter_query.__func__

        def as_str(cls, model, field_name, **params):
            return {k.decode() for k in real(cls, model, field_name, **params)}

        assert [o.name for o in Probe.query.filter(rank__gte=2, room="a")] == [
            "p3",
            "p5",
        ]
        monkeypatch.setattr(KeyFieldMixin, "filter_query", classmethod(as_str))
        assert Probe.query.filter(rank__gte=2, room="a") == []

    def test_fire_on_read_opens_the_unit_of_work_on_the_backend(self, spy):
        TrackedProbe.create(name="t", rank=1)
        spy.clear()
        assert len(TrackedProbe.query.filter(rank__gte=1)) == 1
        assert spy.calls[-1] == "begin", spy.calls

    def test_ranking_path_is_native_but_hydrates_through_load_records(self, spy):
        seed(4)
        spy.clear()
        rows = Probe.query.composite_score({"rank": 1.0}, limit=2)
        assert [o.name for o in rows] == ["p3", "p2"]
        assert "native" in spy.calls, spy.calls
        assert "load_records" in spy.calls, spy.calls

    def test_keys_debug_paths_use_native(self, spy):
        seed(2)
        spy.clear()
        assert len(Probe.query.keys(catchall=True)) >= 2
        assert spy.calls == ["native"], spy.calls
        spy.clear()
        Probe.query.keys(clean=True)
        assert spy.calls[0] == "native", spy.calls


# -- Source shape --------------------------------------------------------------


class TestSourceShape:
    """Pin the grep criterion and the ``native()`` ledger on the source.

    Source checks, not seam checks: a site that swaps ``sorted_add(..., uow=p)``
    for ``p.zadd(...)`` still has no ``get_REDIS_DB()`` in it. The recording
    tests above are the real guard;
    ``test_no_command_is_issued_outside_a_native_site`` narrows the gap for
    the command names the models do not share.
    """

    REDIS_COMMANDS = frozenset(
        "hset hget hgetall hdel hmget hincrby hincrbyfloat sadd srem smembers "
        "scard sismember sunion sinter zadd zrem zincrby zscore zcard zrange "
        "zrevrange zrangebyscore zrevrangebyscore zunionstore zdiffstore "
        "zrangestore expire expireat eval evalsha hscan sscan zscan scan_iter "
        "pipeline".split()
    )

    SOURCES = {name: path.read_text() for name, path in OWNED.items()}
    TREES = {name: ast.parse(src) for name, src in SOURCES.items()}

    @classmethod
    def _calls_named(cls, tree: ast.AST, name: str) -> list[int]:
        return [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == name)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
            )
        ]

    @pytest.mark.parametrize("name", sorted(OWNED))
    def test_no_direct_accessor_or_lua_call_remains(self, name):
        tree = self.TREES[name]
        assert self._calls_named(tree, "get_REDIS_DB") == [], name
        assert self._calls_named(tree, "run_lua") == [], name
        assert self._calls_named(tree, "scan_keys") == [], name
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "POPOTO_REDIS_DB", (name, node.lineno)
            if isinstance(node, ast.Attribute):
                assert node.attr != "POPOTO_REDIS_DB", (name, node.lineno)
            if isinstance(node, ast.ImportFrom):
                imported = {a.name for a in node.names}
                assert "POPOTO_REDIS_DB" not in imported, name
                assert "get_REDIS_DB" not in imported, name
                assert "run_lua" not in imported, name

    @pytest.mark.parametrize("name", sorted(OWNED))
    def test_native_sites_match_the_ledger_and_are_commented(self, name):
        sites = self._calls_named(self.TREES[name], "native")
        assert len(sites) == NATIVE_LEDGER[name], (name, sites)
        lines = self.SOURCES[name].splitlines()
        for lineno in sites:
            window = "\n".join(lines[max(0, lineno - 6) : lineno])
            assert re.search(r"#\s*native\(\):", window), (
                f"native() at {name}:{lineno} has no '# native(): <feature>' "
                "comment within the five lines above it"
            )

    @pytest.mark.parametrize("name", sorted(OWNED))
    def test_no_command_is_issued_outside_a_native_site(self, name):
        # A Redis-command-shaped attribute call may only live in a (sync)
        # function that holds a ledgered native() site. ``async def`` bodies
        # are exempt: the async twins talk to the async client, which the
        # plan lists as a non-goal of the seam, and they hold no
        # ``get_REDIS_DB()`` either (the test above). ``delete``/``exists``/
        # ``keys``/``get``/``type`` are left out of the set because the models
        # and dicts have methods of those names.
        tree = self.TREES[name]
        natives = self._calls_named(tree, "native")
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        async_spans = [
            (n.lineno, n.end_lineno)
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        ]
        offenders = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ):
                continue
            if node.func.attr not in self.REDIS_COMMANDS:
                continue
            if any(lo <= node.lineno <= hi for lo, hi in async_spans):
                continue
            holders = [f for f in functions if f.lineno <= node.lineno <= f.end_lineno]
            ledgered = any(
                f.lineno <= ln <= f.end_lineno for f in holders for ln in natives
            )
            if not ledgered:
                offenders.append((node.lineno, node.func.attr))
        assert offenders == [], (name, offenders)

    @pytest.mark.parametrize("name", ["sorted_field_mixin", "key_field_mixin"])
    def test_mixins_take_the_unit_of_work_by_duck_type(self, name):
        # Architect decision 1: ``pipeline is not None``, never an isinstance
        # against redis-py, and the mixin no longer imports redis-py at all
        # (popoto's own ``..redis_db`` module, for ``ENCODING``, is not redis-py).
        src = self.SOURCES[name]
        assert "isinstance(pipeline, redis" not in src
        for node in ast.walk(self.TREES[name]):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names]
                module = getattr(node, "module", None) or ""
                is_redis_py = module == "redis" or module.startswith("redis.")
                assert "redis" not in names and not is_redis_py, (name, node.lineno)

    def test_unique_field_mixin_has_no_storage_sites(self):
        # The plan: "UniqueFieldMixin performs no storage operation at all";
        # it is owned here so that stays true in both directions.
        assert self._calls_named(self.TREES["unique_field_mixin"], "get_backend") == []
