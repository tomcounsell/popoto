"""``fields/indexed_field_mixin.py`` and ``fields/tag_field.py`` reach storage
through ``get_backend()`` (#631 WS1c).

Mirrors ``tests/test_models_base_routes_through_backend.py`` (WS1a). Two
guards, in opposite directions:

* **Behavioural.** A recording backend that delegates every call to the real
  ``RedisBackend`` is installed with ``set_backend()``. Each entry point in
  the slice must show up as a call on the *backend* -- ``on_save`` as
  ``swap_index`` / ``swap_tags``, ``on_delete`` as ``drop_index_entry`` /
  ``drop_tag_entries``, every ``filter_query`` lookup as the set-index method
  it maps to -- so a site that quietly goes back to ``get_REDIS_DB()`` or
  ``run_lua(...)`` fails here even though Redis would still have done the
  right thing. Each assertion also checks the *result* (the record is found,
  the conflict is raised with the pre-seam text, the index is gone), so the
  recorder never passes on routing alone. Delegation keeps the exercised path
  real; the recorder only watches.

* **Shape.** The source itself: no ``get_REDIS_DB()`` call, no ``run_lua(``,
  no ``POPOTO_REDIS_DB`` name, no ``isinstance(pipeline, redis...)``, no
  Redis-command-shaped attribute call, and exactly as many ``native()`` sites
  as the PR body ledgers -- zero for this slice. The Lua constants stay
  importable from their old modules because other tests and
  ``tests/test_transfer_roundtrip.py``'s source scan look for them there.

The spy is installed per test and reset in ``finally``; ``set_backend(None)``
makes the next ``get_backend()`` re-run selection, so nothing leaks into the
rest of the session.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import popoto
from popoto import ModelException, backends
from popoto.backends import redis as redis_backend
from popoto.backends.redis import RedisBackend
from popoto.fields import indexed_field_mixin, tag_field
from popoto.fields.indexed_field_mixin import IndexedFieldMixin
from popoto.fields.tag_field import TagFieldMixin

INDEXED_PY = Path(popoto.__file__).parent / "fields" / "indexed_field_mixin.py"
TAG_PY = Path(popoto.__file__).parent / "fields" / "tag_field.py"


class RecordingBackend:
    """Delegates to a real ``RedisBackend`` and records the method names."""

    def __init__(self) -> None:
        self._real = RedisBackend()
        self.calls: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def _recorded(*args, **kwargs):
            self.calls.append(name)
            return attr(*args, **kwargs)

        return _recorded


@pytest.fixture
def spy():
    rec = RecordingBackend()
    backends.set_backend(rec)  # type: ignore[arg-type]
    try:
        assert backends.get_backend() is rec
        yield rec
    finally:
        backends.set_backend(None)


class Probe(popoto.Model):
    name = popoto.KeyField()
    status = popoto.IndexedField(type=str, null=True)
    tags = popoto.TagField()


class UniqueProbe(popoto.Model):
    name = popoto.KeyField()
    email = popoto.UniqueField(type=str)


def _key(name: str) -> str:
    return Probe(name=name).db_key.redis_key


class TestOnSaveReachesTheBackend:
    def test_save_runs_the_index_and_tag_swaps_eagerly(self, spy):
        Probe(name="a", status="live", tags=["t1", "t2"]).save()
        # One IndexedField hook and one TagField hook, each executed now (no
        # uow): Model.save runs them before its own pipeline.
        assert spy.calls.count("swap_index") == 1, spy.calls
        assert spy.calls.count("swap_tags") == 1, spy.calls
        assert "native" not in spy.calls
        spy.calls.clear()
        UniqueProbe(name="u", email="a@x").save()
        assert spy.calls.count("swap_index") == 1, spy.calls
        # And the swaps did their job: every lookup finds the record.
        assert Probe.query.filter(status="live")[0].name == "a"
        assert Probe.query.filter(tags__all=["t1", "t2"])[0].name == "a"
        assert UniqueProbe.query.filter(email="a@x")[0].name == "u"

    def test_save_on_a_caller_pipeline_queues_the_swaps_on_it(self, spy):
        pipe = spy.begin()
        spy.calls.clear()
        returned = Probe(name="q", status="s", tags=["t"]).save(pipeline=pipe)
        assert returned is pipe
        assert UniqueProbe(name="q", email="q@x").save(pipeline=pipe) is pipe
        # External path: the unique pre-check reads the index first, then
        # the swaps are queued; nothing is visible until commit().
        assert "index_members" in spy.calls, spy.calls
        assert spy.calls.count("swap_index") == 2
        assert spy.calls.count("swap_tags") == 1
        assert Probe.query.filter(status="s") == []
        assert Probe.query.filter(tags__contains="t") == []
        assert UniqueProbe.query.filter(email="q@x") == []
        pipe.commit()
        assert Probe.query.filter(status="s")[0].name == "q"
        assert Probe.query.filter(tags__contains="t")[0].name == "q"
        assert UniqueProbe.query.filter(email="q@x")[0].name == "q"

    def test_unique_conflict_on_the_internal_path_keeps_the_pre_seam_message(self, spy):
        UniqueProbe.create(name="first", email="dup@x")
        spy.calls.clear()
        # Model.pre_save's own index_members check raises first on the public
        # path ("Unique constraint violated: ..."), so the hook is driven
        # directly here, exactly as Model.save drives it after pre_save.
        # Deviation 6: the backend's ModelException names record_key/field/
        # new_idx; the user sees Model.field and the raw value, byte for byte
        # as before the seam.
        second = UniqueProbe(name="second", email="dup@x")
        email_field = type(UniqueProbe._meta.fields["email"])
        with pytest.raises(ModelException) as excinfo:
            email_field.on_save(second, "email", "dup@x")
        assert str(excinfo.value) == (
            "Uniqueness violation on UniqueProbe.email: value 'dup@x' is "
            "already taken by another instance"
        )
        assert spy.calls == ["swap_index"], "the conflict was not the backend's"
        # The swap is all-or-nothing: no hash field, no pointer, no membership.
        assert not UniqueProbe.exists(name="second")
        assert [o.name for o in UniqueProbe.query.filter(email="dup@x")] == ["first"]

    def test_unique_conflict_on_a_caller_pipeline_raises_from_the_pre_check(self, spy):
        UniqueProbe.create(name="first", email="dup@x")
        pipe = spy.begin()
        spy.calls.clear()
        second = UniqueProbe(name="second", email="dup@x")
        email_field = type(UniqueProbe._meta.fields["email"])
        with pytest.raises(ModelException) as excinfo:
            email_field.on_save(second, "email", "dup@x", pipeline=pipe)
        assert str(excinfo.value) == (
            "Uniqueness violation on UniqueProbe.email: value 'dup@x' is "
            "already taken by another instance"
        )
        # The pre-check is index_members; the swap is never queued.
        assert spy.calls == ["index_members"], spy.calls
        assert len(pipe) == 0, "nothing was queued on the caller's pipeline"

    def test_value_change_moves_membership_through_swap_index(self, spy):
        obj = Probe.create(name="m", status="old")
        spy.calls.clear()
        obj.status = "new"
        obj.save()
        assert "swap_index" in spy.calls
        assert Probe.query.filter(status="old") == []
        assert Probe.query.filter(status="new")[0].name == "m"


class TestOnDeleteReachesTheBackend:
    def test_delete_drops_the_index_and_tag_entries(self, spy):
        obj = Probe.create(name="d", status="s", tags=["x", "y"])
        spy.calls.clear()
        assert obj.delete() is True
        # Model.delete hands the hooks its own pipeline, so both land on a uow.
        assert spy.calls.count("drop_index_entry") == 1, spy.calls
        assert spy.calls.count("drop_tag_entries") == 1, spy.calls
        assert Probe.query.filter(status="s") == []
        assert Probe.query.filter(tags__any=["x", "y"]) == []
        unique = UniqueProbe.create(name="d", email="d@x")
        spy.calls.clear()
        assert unique.delete() is True
        assert spy.calls.count("drop_index_entry") == 1, spy.calls
        assert UniqueProbe.query.filter(email="d@x") == []

    def test_delete_without_a_pointer_falls_back_to_the_value_index(self, spy):
        # The pointer side keys are Redis layout the backend owns; remove
        # them so drop_* must use the fallback indexes the mixins compute.
        obj = Probe.create(name="f", status="s", tags=["x"])
        client = popoto.get_redis()
        client.delete(IndexedFieldMixin._pointer_side_key(_key("f"), "status"))
        client.delete(TagFieldMixin._tag_pointer_side_key(_key("f"), "tags"))
        spy.calls.clear()
        assert obj.delete() is True
        assert "drop_index_entry" in spy.calls
        assert "drop_tag_entries" in spy.calls
        # Assert on the raw key sets, not on filter(): filter() hydrates and
        # silently drops an orphaned index member whose hash is gone, which
        # would make a dropped fallback invisible here.
        assert Probe.query.filter_for_keys_set(status="s") == set()
        assert Probe.query.filter_for_keys_set(tags__contains="x") == set()

    def test_direct_hook_call_without_a_pipeline_executes_now(self, spy):
        obj = Probe.create(name="h", status="s", tags=["x"])
        spy.calls.clear()
        status_field = type(Probe._meta.fields["status"])
        removed = status_field.on_delete(obj, "status", "s")
        assert spy.calls == ["drop_index_entry"]
        assert removed == 1  # the SREM reply, as before the seam
        assert Probe.query.filter(status="s") == []
        spy.calls.clear()
        type(Probe._meta.fields["tags"]).on_delete(obj, "tags", ["x"])
        assert spy.calls == ["drop_tag_entries"]
        assert Probe.query.filter(tags__contains="x") == []


class TestFilterQueryReachesTheBackend:
    @pytest.fixture(autouse=True)
    def _records(self):
        Probe.create(name="n1", status="active", tags=["a", "b"])
        Probe.create(name="n2", status="archived", tags=["b"])
        Probe.create(name="n3", status=None, tags=[])

    def test_exact_match_uses_index_members(self, spy):
        found = Probe.query.filter(status="active")
        assert [o.name for o in found] == ["n1"]
        assert "index_members" in spy.calls, spy.calls

    def test_in_uses_index_union(self, spy):
        found = Probe.query.filter(status__in=["active", "archived"])
        assert {o.name for o in found} == {"n1", "n2"}
        assert "index_union" in spy.calls, spy.calls
        spy.calls.clear()
        assert Probe.query.filter(status__in=[]) == []
        assert "index_union" not in spy.calls  # empty list never hits storage

    def test_isnull_true_uses_index_members(self, spy):
        found = Probe.query.filter(status__isnull=True)
        assert [o.name for o in found] == ["n3"]
        assert "index_members" in spy.calls

    def test_isnull_false_scans_index_names_then_reads_each(self, spy):
        found = Probe.query.filter(status__isnull=False)
        assert {o.name for o in found} == {"n1", "n2"}
        assert "scan_index_names" in spy.calls, spy.calls
        assert "index_members" in spy.calls

    def test_startswith_and_endswith_scan_index_names(self, spy):
        found = Probe.query.filter(status__startswith="arch")
        assert [o.name for o in found] == ["n2"]
        assert "scan_index_names" in spy.calls
        spy.calls.clear()
        found = Probe.query.filter(status__endswith="ive")
        assert [o.name for o in found] == ["n1"]
        assert "scan_index_names" in spy.calls

    def test_tag_contains_any_all_use_the_set_index_family(self, spy):
        assert [o.name for o in Probe.query.filter(tags__contains="a")] == ["n1"]
        assert "index_members" in spy.calls
        spy.calls.clear()
        assert {o.name for o in Probe.query.filter(tags__any=["a", "b"])} == {
            "n1",
            "n2",
        }
        assert "index_union" in spy.calls
        spy.calls.clear()
        assert [o.name for o in Probe.query.filter(tags__all=["a", "b"])] == ["n1"]
        assert "index_intersection" in spy.calls
        spy.calls.clear()
        assert Probe.query.filter(tags__any=[]) == []
        assert Probe.query.filter(tags__all=[]) == []
        assert spy.calls == [], "empty lists never hit storage"

    def test_results_keep_the_bytes_shape_the_other_mixins_intersect(self, spy):
        # Deviation 11: the backend decodes members to str, but
        # Query.filter_for_keys_set intersects this field's set with the raw
        # bytes KeyFieldMixin still returns. A str set here would make a
        # composed filter match nothing.
        assert Probe.query.filter_for_keys_set(status="active") == {_key("n1").encode()}
        assert Probe.query.filter_for_keys_set(tags__contains="b") == {
            _key("n1").encode(),
            _key("n2").encode(),
        }
        composed = Probe.query.filter(name="n1", status="active", tags__contains="b")
        assert [o.name for o in composed] == ["n1"]


class TestLuaConstantsStayImportable:
    def test_the_old_modules_still_export_the_scripts(self):
        assert indexed_field_mixin.INDEX_SWAP_LUA is redis_backend.INDEX_SWAP_LUA
        assert tag_field.TAG_SWAP_LUA is redis_backend.TAG_SWAP_LUA

    def test_the_mixin_key_builders_match_the_backend(self):
        # The mixins keep their key builders for tests and diagnostics; they
        # must stay byte-equal to what the backend derives.
        assert IndexedFieldMixin._pointer_side_key("M:1", "f") == (
            redis_backend._idx_ptr_key("M:1", "f")
        )
        assert IndexedFieldMixin._pre_540_pointer_side_key("M:1", "f") == (
            redis_backend._idx_pre_540_ptr_key("M:1", "f")
        )
        assert IndexedFieldMixin._legacy_pointer_field("f") == (
            redis_backend._idx_legacy_ptr_field("f")
        )
        assert TagFieldMixin._tag_pointer_side_key("M:1", "f") == (
            redis_backend._tag_ptr_key("M:1", "f")
        )
        assert TagFieldMixin._pre_540_tag_pointer_side_key("M:1", "f") == (
            redis_backend._tag_pre_540_ptr_key("M:1", "f")
        )


class TestSourceShape:
    """Pin the grep criterion and the (empty) ``native()`` ledger on the source.

    These are source checks, not seam checks: a site that swaps
    ``swap_index(...)`` for ``pipe.evalsha(...)`` still has no
    ``get_REDIS_DB()`` or ``run_lua(`` in it. The recording tests above are
    the real guard; ``test_no_command_is_issued_on_a_unit_of_work`` narrows
    the gap for the command names the mixins could reach for.
    """

    REDIS_COMMANDS = frozenset(
        "hset hget hgetall hdel hmget sadd srem smembers sunion sinter scard "
        "sismember zadd zrem zrange eval evalsha register_script scan_keys "
        "scan_iter hscan sscan zscan pipeline".split()
    )

    SOURCES = {INDEXED_PY: INDEXED_PY.read_text(), TAG_PY: TAG_PY.read_text()}
    TREES = {path: ast.parse(src) for path, src in SOURCES.items()}

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

    @pytest.mark.parametrize("path", [INDEXED_PY, TAG_PY], ids=["indexed", "tag"])
    def test_no_accessor_or_lua_call_remains(self, path):
        tree = self.TREES[path]
        assert self._calls_named(tree, "get_REDIS_DB") == []
        assert self._calls_named(tree, "run_lua") == []
        assert self._calls_named(tree, "get_redis") == []
        # Nor the stale snapshot shape (#655): no name, attribute or import
        # of POPOTO_REDIS_DB in code. Comments may still mention it in prose.
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "POPOTO_REDIS_DB", node.lineno
            if isinstance(node, ast.Attribute):
                assert node.attr != "POPOTO_REDIS_DB", node.lineno
            if isinstance(node, ast.ImportFrom):
                names = {a.name for a in node.names}
                assert "POPOTO_REDIS_DB" not in names
                assert "get_REDIS_DB" not in names
                assert "run_lua" not in names

    @pytest.mark.parametrize("path", [INDEXED_PY, TAG_PY], ids=["indexed", "tag"])
    def test_native_ledger_is_empty(self, path):
        # The PR body's ledger for WS1c has no entries: everything in these
        # two modules is in the #631 slice. A new site is a deliberate,
        # ledgered addition that bumps this number and names its feature.
        sites = self._calls_named(self.TREES[path], "native")
        assert sites == [], sites
        lines = self.SOURCES[path].splitlines()
        for lineno in sites:  # pragma: no cover - empty ledger
            window = "\n".join(lines[max(0, lineno - 6) : lineno])
            assert re.search(r"#\s*native\(\):", window)

    @pytest.mark.parametrize("path", [INDEXED_PY, TAG_PY], ids=["indexed", "tag"])
    def test_no_command_is_issued_on_a_unit_of_work(self, path):
        # A Redis-command-shaped attribute call (``pipeline.srem``,
        # ``client.smembers``) may only live in a function holding a ledgered
        # ``native()`` site -- and this slice has none, so: nowhere.
        offenders = [
            (node.lineno, node.func.attr)
            for node in ast.walk(self.TREES[path])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in self.REDIS_COMMANDS
        ]
        assert offenders == [], offenders

    @pytest.mark.parametrize("path", [INDEXED_PY, TAG_PY], ids=["indexed", "tag"])
    def test_no_isinstance_pipeline_check_and_no_redis_import(self, path):
        # Architect decision 1: the field layer checks ``pipeline is not None``.
        source = self.SOURCES[path]
        assert "isinstance(pipeline, redis" not in source
        for node in ast.walk(self.TREES[path]):
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("redis") for a in node.names)

    def test_the_lua_scripts_are_re_exported_not_redefined(self):
        for path, tree in self.TREES.items():
            assigned = {
                t.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                for t in node.targets
                if isinstance(t, ast.Name)
            }
            assert "INDEX_SWAP_LUA" not in assigned, path
            assert "TAG_SWAP_LUA" not in assigned, path
        assert "from ..backends.redis import INDEX_SWAP_LUA" in self.SOURCES[INDEXED_PY]
        assert "from ..backends.redis import TAG_SWAP_LUA" in self.SOURCES[TAG_PY]
