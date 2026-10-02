"""``models/base.py`` reaches storage through ``get_backend()`` (#631 WS1a).

Two guards, in opposite directions:

* **Behavioural.** A recording backend that delegates every call to the real
  ``RedisBackend`` is installed with ``set_backend()``. Each ``Model`` entry
  point in the slice must show up as a call on the *backend* -- ``save`` as
  ``save_record``, ``exists`` as ``record_exists``, ``delete`` as
  ``delete_record``, ``atomic_increment`` as ``increment_field`` -- so a site
  that quietly goes back to ``get_REDIS_DB()`` fails here even though Redis
  would still have done the right thing. Delegation keeps the exercised path
  real; the recorder only watches.

* **Shape.** The source itself: no ``get_REDIS_DB()`` call, ``run_lua(`` only
  where the ledger says, and exactly as many ``native()`` sites as the PR body
  ledgers, each with its naming comment. The ``native()`` count is pinned so a
  new escape-hatch site is a deliberate, ledgered addition and never an
  unnoticed one -- the plan's restated success criterion is "zero accessor /
  Lua sites outside ``backends/`` plus an enumerated ``native()`` ledger", and
  this file is what makes the second half checkable.

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
from popoto import backends
from popoto.backends.redis import RedisBackend

BASE_PY = Path(popoto.__file__).parent / "models" / "base.py"


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
    count = popoto.IntField(default=0)
    score = popoto.SortedField(type=float, default=0.0)
    label = popoto.Field(type=str, default="")

    class Meta:
        indexes = [(("name", "label"), True)]


class TestEntryPointsReachTheBackend:
    def test_save_goes_through_save_record_and_the_composite_map(self, spy):
        obj = Probe(name="a", label="x")
        obj.save()
        assert "begin" in spy.calls
        assert "save_record" in spy.calls, spy.calls
        assert "map_set" in spy.calls, "composite Meta.indexes write bypassed map_set"
        assert "map_get" in spy.calls, "pre_save unique-index check bypassed map_get"

    def test_partial_save_goes_through_save_record(self, spy):
        obj = Probe.create(name="p", label="x")
        spy.calls.clear()
        obj.label = "y"
        obj.save(update_fields=["label"])
        assert "save_record" in spy.calls, spy.calls
        assert "set_expiry" in spy.calls, "partial-save TTL bypassed set_expiry"

    def test_save_on_a_caller_pipeline_queues_on_it(self, spy):
        pipe = spy.begin()
        spy.calls.clear()
        returned = Probe(name="q", label="x").save(pipeline=pipe)
        assert returned is pipe
        assert "save_record" in spy.calls
        assert not popoto.get_redis().exists(Probe(name="q").db_key.redis_key)
        pipe.commit()
        assert Probe.exists(name="q")

    def test_exists_goes_through_record_exists(self, spy):
        Probe.exists(name="nobody")
        assert spy.calls == ["record_exists"], spy.calls

    def test_load_raw_hash_and_load_fields(self, spy):
        obj = Probe.create(name="r", label="x")
        key = obj.db_key.redis_key
        spy.calls.clear()
        assert Probe.load_raw_hash(key)
        assert spy.calls == ["load_record"]
        assert Probe.load_raw_hash("Probe:absent") == {}
        spy.calls.clear()
        assert Probe.load_fields(key, "label") == {"label": "x"}
        assert spy.calls == ["load_fields"]

    def test_delete_checks_existence_first_then_delete_record(self, spy):
        obj = Probe.create(name="d", label="x")
        spy.calls.clear()
        assert obj.delete() is True
        # Deviation 9: EXISTS before the hooks, never read off DEL's reply.
        assert spy.calls.index("record_exists") < spy.calls.index("delete_record")
        assert "map_delete" in spy.calls, "composite index cleanup bypassed map_delete"
        assert Probe(name="d").delete() is False

    def test_atomic_increment_goes_through_increment_and_sorted_increment(self, spy):
        obj = Probe.create(name="i", label="x")
        spy.calls.clear()
        assert obj.atomic_increment("count", 2) == 2
        assert spy.calls == ["increment_field"], spy.calls
        spy.calls.clear()
        assert obj.atomic_increment("score", 1.5) == 1.5
        assert spy.calls == ["increment_field", "sorted_increment"], spy.calls
        pipe = spy.begin()
        spy.calls.clear()
        assert obj.atomic_increment("score", 1.0, pipeline=pipe) is pipe
        assert spy.calls == ["increment_field", "sorted_increment"]
        pipe.commit()
        assert Probe.query.get(name="i").score == 2.5

    def test_purge_orphans_goes_through_purge_orphan(self, spy):
        obj = Probe.create(name="o", label="x")
        key = obj.db_key.redis_key
        popoto.get_redis().delete(key)  # orphan the index entries
        spy.calls.clear()
        assert Probe._purge_orphan_keys([key]) == 1
        assert spy.calls == ["begin", "purge_orphan"], spy.calls
        assert not popoto.get_redis().sismember("$Class:Probe", key)

    def test_maintenance_scans_go_through_the_backend(self, spy):
        Probe.create(name="m", label="x")
        spy.calls.clear()
        Probe.check_indexes()
        assert "scan_index_members" in spy.calls
        assert "records_exist" in spy.calls or "load_records" in spy.calls
        assert "map_scan" in spy.calls
        spy.calls.clear()
        Probe.clean_indexes()
        assert "scan_index_members" in spy.calls
        spy.calls.clear()
        assert Probe.rebuild_indexes() == 1
        assert "drop_index" in spy.calls
        assert "scan_record_keys" in spy.calls
        assert "load_record" in spy.calls
        assert "save_record" in spy.calls

    def test_bulk_operations_open_the_unit_of_work_on_the_backend(self, spy):
        Probe.bulk_create([Probe(name="b1", label="x"), Probe(name="b2", label="y")])
        assert spy.calls[0] == "begin"
        spy.calls.clear()
        assert Probe.delete_all() == 2
        assert "begin" in spy.calls and "delete_record" in spy.calls


class PartialProbe(popoto.Model):
    name = popoto.KeyField()
    label = popoto.Field(type=str, default="")


class PartialTtlProbe(popoto.Model):
    name = popoto.KeyField()
    email = popoto.IndexedField(type=str, default="")

    class Meta:
        ttl = 600


class TestPartialSaveKeepsThePreSeamWireOrder:
    """The #735 review's two blockers, as the public API sees them.

    Both are the partial-save path on an instance that was never fully saved.
    No ``src/`` recipe does this, so the suite could not see either; user
    code can. Each assertion is the pre-seam behaviour, measured on
    ``poc/backend-seam`` before WS1a.
    """

    def test_partial_save_of_a_fresh_instance_stays_out_of_the_class_set(self):
        # B1: the pre-seam partial path SADDed only on key migration. An
        # unconditional SADD surfaced a hash with no KeyField -- query.all()
        # returned a row with name=None -- and check_indexes counted it as
        # healthy membership.
        PartialProbe(name="fresh", label="q").save(update_fields=["label"])
        raw = popoto.get_redis().hgetall("PartialProbe:fresh")
        assert set(raw) == {b"label"}, "hash shape is the pre-seam one"
        assert PartialProbe.query.count() == 0
        assert PartialProbe.query.all() == []
        # Key migration still registers the new key and drops the old one.
        obj = PartialProbe.create(name="old", label="x")
        obj.name = "new"
        obj.save(update_fields=["name"], migrate_key=True)
        assert {o.name for o in PartialProbe.query.all()} == {"new"}

    def test_partial_save_on_a_caller_pipeline_keeps_meta_ttl(self):
        # B2: with an external pipeline, an EVAL-only partial save (every
        # listed field indexed) creates the hash inside the INDEX_SWAP EVAL
        # that the field hook queues. The EXPIRE must be queued after that
        # hook; queued before it, EXPIRE hits a missing key and the record
        # lives forever.
        with popoto.batch() as pipe:
            PartialTtlProbe(name="t", email="e@x").save(
                update_fields=["email"], pipeline=pipe
            )
            pipe.execute()
        client = popoto.get_redis()
        assert client.exists("PartialTtlProbe:t")
        assert client.ttl("PartialTtlProbe:t") == 600
        # The internal path and a non-indexed field on the external path
        # were never affected; pin them so a later reorder cannot trade one
        # for the other.
        PartialTtlProbe(name="u", email="f@x").save(update_fields=["email"])
        assert client.ttl("PartialTtlProbe:u") == 600


class TestSourceShape:
    """Pin the grep criterion and the ``native()`` ledger on the source.

    These are source checks, not seam checks: a site that swaps
    ``delete_record(..., uow=p)`` for ``p.delete(...); p.srem(...)`` still
    has no ``get_REDIS_DB()``, ``run_lua(`` or ``native()`` in it. The
    recording tests above are the real guard, and they cover only the entry
    points they enumerate; ``test_no_command_is_issued_on_a_unit_of_work``
    narrows the gap for the command names ``Model`` does not share.
    """

    REDIS_COMMANDS = frozenset(
        "hset hget hgetall hdel hmget hincrby hincrbyfloat sadd srem smembers "
        "scard sismember zadd zrem zincrby zscore zrange expire expireat eval "
        "evalsha hscan sscan zscan scan_iter".split()
    )

    SOURCE = BASE_PY.read_text()
    TREE = ast.parse(SOURCE)

    @staticmethod
    def _calls_named(name: str) -> list[int]:
        return [
            node.lineno
            for node in ast.walk(TestSourceShape.TREE)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == name)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
            )
        ]

    def test_no_get_redis_db_call_remains(self):
        assert self._calls_named("get_REDIS_DB") == []
        # Nor the stale snapshot shape (#655): no name, attribute or import of
        # POPOTO_REDIS_DB in code. Comments may still mention it in prose.
        for node in ast.walk(self.TREE):
            if isinstance(node, ast.Name):
                assert node.id != "POPOTO_REDIS_DB", node.lineno
            if isinstance(node, ast.Attribute):
                assert node.attr != "POPOTO_REDIS_DB", node.lineno
            if isinstance(node, ast.ImportFrom):
                assert "POPOTO_REDIS_DB" not in {a.name for a in node.names}

    def test_run_lua_is_only_the_cyclic_decay_site(self):
        sites = self._calls_named("run_lua")
        assert len(sites) == 1, sites
        enclosing = [
            node.name
            for node in ast.walk(self.TREE)
            if isinstance(node, ast.FunctionDef)
            and node.lineno <= sites[0] <= node.end_lineno
        ]
        assert enclosing == ["_adjust_cycle_amplitudes"], enclosing

    def test_native_sites_match_the_ledger_and_are_commented(self):
        # The PR body's ledger. A new site is a deliberate addition that
        # bumps this number and names its feature; never an incidental one.
        sites = self._calls_named("native")
        assert len(sites) == 5, sites
        lines = self.SOURCE.splitlines()
        for lineno in sites:
            window = "\n".join(lines[max(0, lineno - 6) : lineno])
            assert re.search(r"#\s*native\(\):", window), (
                f"native() at base.py:{lineno} has no '# native(): <feature>' "
                "comment within the five lines above it"
            )

    def test_no_command_is_issued_on_a_unit_of_work(self):
        # Review tech-debt 2: a Redis-command-shaped attribute call (``p.srem``,
        # ``pipe.hset``) may only live in a function that holds a ledgered
        # ``native()`` site. ``delete``/``exists``/``type`` are left out of the
        # set because ``Model`` has methods of those names, so a bypass that
        # uses only ``p.delete(...)`` is caught by the recording tests alone.
        natives = self._calls_named("native")
        functions = [n for n in ast.walk(self.TREE) if isinstance(n, ast.FunctionDef)]
        offenders = []
        for node in ast.walk(self.TREE):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ):
                continue
            if node.func.attr not in self.REDIS_COMMANDS:
                continue
            holders = [f for f in functions if f.lineno <= node.lineno <= f.end_lineno]
            ledgered = any(
                f.lineno <= ln <= f.end_lineno for f in holders for ln in natives
            )
            if not ledgered:
                offenders.append((node.lineno, node.func.attr))
        assert offenders == [], offenders

    def test_no_isinstance_pipeline_check_remains(self):
        # Architect decision 1: the field layer checks ``uow is not None``.
        assert "isinstance(pipeline, redis" not in self.SOURCE

    def test_the_inline_increment_script_is_gone(self):
        # ATOMIC_INCREMENT_LUA exists only in backends/redis.py (WS0 review
        # tech-debt item 3).
        assert "cmsgpack.unpack(current_packed)" not in self.SOURCE
        assert "lua_script" not in self.SOURCE
