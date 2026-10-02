"""Decay ranking and confidence reach storage through ``get_backend()``
(#631 WS1d: ``fields/decaying_sorted_field.py``, ``fields/confidence_field.py``).

Two guards, in opposite directions, mirroring WS1a's
``tests/test_models_base_routes_through_backend.py``:

* **Behavioural.** A recording backend that delegates every call to the real
  ``RedisBackend`` is installed with ``set_backend()``. Each entry point in
  the slice must show up as a call on the *backend* -- ``rank_decayed`` as
  ``decayed_rank``, ``update_confidence`` as ``confidence_update``, the
  companion-hash reads and writes as ``map_get`` / ``map_set`` /
  ``map_delete`` / ``map_scan`` -- so a site that quietly goes back to
  ``get_REDIS_DB()`` fails here even though Redis would still have done the
  right thing. The recorder also keeps the keyword arguments, because two of
  the protocol deviations WS0 handed to WS1d (#732, items 7 and 8) are about
  *what* crosses the seam, not only whether it is crossed: the confidence
  triple must arrive as the literal strings the field layer carries (so the
  EVAL argv stays byte-identical), and an absent record must come back as
  ``None`` from the backend and leave as ``TypeError`` from the field.

* **Shape.** The source itself: no ``get_REDIS_DB()`` call, no ``run_lua(``,
  no ``POPOTO_REDIS_DB`` snapshot, no ``isinstance(pipeline, redis...)``,
  no Redis-command-shaped attribute call anywhere, and **zero** ``native()``
  sites -- this slice needs no escape hatch, so the ledger is pinned at
  nothing and a new hatch is a deliberate, ledgered addition.

The spy is installed per test and reset in ``finally``; ``set_backend(None)``
makes the next ``get_backend()`` re-run selection, so nothing leaks into the
rest of the session. Everything here is Redis-bound by construction (the
recorder wraps ``RedisBackend`` and the fixtures plant ZSET scores with the
raw client), which is why the file carries no ``conformance`` mark.
"""

from __future__ import annotations

import ast
import time
import uuid
from pathlib import Path

import msgpack
import pytest

import popoto
from popoto import backends
from popoto.backends import redis as redis_backend
from popoto.backends.redis import RedisBackend
from popoto.fields import confidence_field, decaying_sorted_field
from popoto.fields.confidence_field import ConfidenceField
from popoto.fields.decaying_sorted_field import (
    MODULATION_DISABLED,
    DecayingSortedField,
    validity_gate_args,
)
from popoto.fields.validity_field import ValidityField

FIELDS_DIR = Path(popoto.__file__).parent / "fields"
OWNED = {
    "decaying_sorted_field.py": FIELDS_DIR / "decaying_sorted_field.py",
    "confidence_field.py": FIELDS_DIR / "confidence_field.py",
}
DAY = 86400.0


class RecordingBackend:
    """Delegates to a real ``RedisBackend``; records names and keyword args."""

    def __init__(self) -> None:
        self._real = RedisBackend()
        self.calls: list[str] = []
        self.kwargs: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def _recorded(*args, **kwargs):
            self.calls.append(name)
            self.kwargs.append((name, args, kwargs))
            return attr(*args, **kwargs)

        return _recorded

    def last(self, name: str) -> tuple[tuple, dict]:
        for called, args, kwargs in reversed(self.kwargs):
            if called == name:
                return args, kwargs
        raise AssertionError(f"{name} was never called; calls={self.calls}")

    def clear(self) -> None:
        self.calls.clear()
        self.kwargs.clear()


@pytest.fixture
def spy():
    rec = RecordingBackend()
    backends.set_backend(rec)  # type: ignore[arg-type]
    try:
        assert backends.get_backend() is rec
        yield rec
    finally:
        backends.set_backend(None)


class DecayProbe(popoto.Model):
    name = popoto.KeyField()
    strength = popoto.FloatField(default=1.0)
    last_accessed = DecayingSortedField(decay_rate=0.5, base_score_field="strength")


class GatedProbe(popoto.Model):
    name = popoto.KeyField()
    last_accessed = DecayingSortedField(decay_rate=0.5)
    validity = ValidityField()


class ConfProbe(popoto.Model):
    name = popoto.KeyField()
    certainty = ConfidenceField(initial_confidence=0.5)


class PartProbe(popoto.Model):
    name = popoto.KeyField()
    project = popoto.KeyField()
    certainty = ConfidenceField(initial_confidence=0.5, partition_by="project")


def _zkey(model_class, field_name="last_accessed"):
    field = model_class._meta.fields[field_name]
    return field.get_sortedset_db_key(model_class, field_name).redis_key


def _aged(model_class, name, *, days, now, **kwargs):
    record = model_class(name=name, **kwargs)
    record.save()
    popoto.get_redis().zadd(
        _zkey(model_class), {record.db_key.redis_key: now - days * DAY}
    )
    return record


def _decoded(reply):
    out = {}
    for i in range(0, len(reply), 2):
        member = reply[i]
        if isinstance(member, bytes):
            member = member.decode()
        out[member] = float(reply[i + 1])
    return out


# ---------------------------------------------------------------------------
# Decay ranking
# ---------------------------------------------------------------------------


class TestDecayRankingReachesTheBackend:
    def test_rank_decayed_is_one_decayed_rank_call(self, spy):
        tag = uuid.uuid4().hex[:8]
        now = time.time()
        for i in range(1, 4):
            _aged(DecayProbe, f"{tag}-{i}", days=i, now=now, strength=float(i))
        field = DecayProbe._meta.fields["last_accessed"]
        spy.clear()

        reply = field.rank_decayed(_zkey(DecayProbe), now=now)

        # n=None: the ZCARD now lives inside the backend, so the field issues
        # exactly one protocol call and nothing through native().
        assert spy.calls == ["decayed_rank"], spy.calls
        _, kwargs = spy.last("decayed_rank")
        assert kwargs["limit"] is None
        assert kwargs["decay_rate"] == 0.5
        assert kwargs["base_score_field"] == "strength"
        assert kwargs["now"] == now
        # Architect decision 4, kept: the raw flat reply, undecoded.
        assert isinstance(reply, list) and len(reply) == 6
        scored = _decoded(reply)
        assert set(scored) == {
            DecayProbe(name=f"{tag}-{i}").db_key.redis_key for i in range(1, 4)
        }

    def test_explicit_n_and_overrides_cross_the_seam(self, spy):
        tag = uuid.uuid4().hex[:8]
        now = time.time()
        _aged(DecayProbe, f"{tag}-1", days=1, now=now)
        field = DecayProbe._meta.fields["last_accessed"]
        spy.clear()

        reply = field.rank_decayed(
            _zkey(DecayProbe), now=now, n=5, decay_rate=0.9, base_score_field=""
        )

        assert spy.calls == ["decayed_rank"]
        _, kwargs = spy.last("decayed_rank")
        assert kwargs["limit"] == 5
        assert kwargs["decay_rate"] == 0.9
        assert kwargs["base_score_field"] == ""
        assert len(reply) == 2

    def test_empty_set_returns_empty_through_the_backend(self, spy):
        field = DecayProbe._meta.fields["last_accessed"]
        absent = f"$SortF:DecayProbe:last_accessed:{uuid.uuid4().hex}"
        spy.clear()
        assert field.rank_decayed(absent, now=time.time()) == []
        assert spy.calls == ["decayed_rank"]

    def test_confidence_triple_crosses_as_the_literal_strings(self, spy):
        # Deviation 8 (#732): the backend renders the triple with str(). The
        # field layer passes the strings it already holds, so the argv the
        # script sees is byte-identical to the pre-seam EVAL. A float here
        # would render "0.0" where today's wire carries "0".
        field = DecayProbe._meta.fields["last_accessed"]
        zkey = _zkey(DecayProbe)
        now = time.time()

        field.rank_decayed(zkey, now=now, n=1)
        _, kwargs = spy.last("decayed_rank")
        assert kwargs["confidence"] == MODULATION_DISABLED == ("", "0", "0.5")
        assert all(isinstance(part, str) for part in kwargs["confidence"])
        assert kwargs["validity"] is None

        explicit = ("$ConfidencF:DecayProbe:certainty:data", "0.3", "0.5")
        field.rank_decayed(zkey, now=now, n=1, confidence=explicit)
        _, kwargs = spy.last("decayed_rank")
        assert kwargs["confidence"] == explicit
        assert kwargs["confidence"][1] == "0.3" and kwargs["confidence"][2] == "0.5"

    def test_validity_triple_crosses_typed_and_bit_exact(self, spy):
        field = GatedProbe._meta.fields["last_accessed"]
        zkey = _zkey(GatedProbe)
        as_of = time.time()
        invalid_key, valid_key, as_of_repr = validity_gate_args(GatedProbe, as_of=as_of)
        assert as_of_repr == repr(as_of), "precondition: the gate is on"

        field.rank_decayed(
            zkey, now=as_of, n=1, validity=(invalid_key, valid_key, as_of_repr)
        )
        _, kwargs = spy.last("decayed_rank")
        assert kwargs["validity"] == (invalid_key, valid_key, as_of)
        assert isinstance(kwargs["validity"][2], float)
        # The backend renders repr(float(...)): the same bytes went out.
        assert repr(kwargs["validity"][2]) == as_of_repr

        # The disabled triple is the gate-off signal and crosses as None.
        field.rank_decayed(zkey, now=as_of, n=1, validity=("", "", ""))
        _, kwargs = spy.last("decayed_rank")
        assert kwargs["validity"] is None

    def test_pretrim_budget_is_read_at_call_time(self, spy, monkeypatch):
        from popoto.fields.constants import Defaults

        field = DecayProbe._meta.fields["last_accessed"]
        zkey = _zkey(DecayProbe)
        monkeypatch.setattr(Defaults, "VALIDITY_GATE_PRETRIM_MAX_RATIO", 0.25)
        field.rank_decayed(zkey, now=time.time(), n=1)
        _, kwargs = spy.last("decayed_rank")
        assert kwargs["pretrim_max_ratio"] == 0.25

    def test_top_by_decay_reaches_decayed_rank(self, spy):
        tag = uuid.uuid4().hex[:8]
        now = time.time()
        for i in range(1, 3):
            _aged(DecayProbe, f"{tag}-{i}", days=i, now=now)
        spy.clear()
        results = DecayProbe.query.top_by_decay("last_accessed", n=10)
        assert "decayed_rank" in spy.calls, spy.calls
        assert [r.name for r in results] == [f"{tag}-1", f"{tag}-2"]


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


class TestConfidenceReachesTheBackend:
    def test_save_seeds_the_companion_hash_through_map_set(self, spy):
        ConfProbe.create(name="seed")
        sets = [kw for name, _, kw in spy.kwargs if name == "map_set"]
        assert sets, spy.calls
        assert sets[-1]["only_if_absent"] is True, "HSETNX seed must stay HSETNX"
        assert sets[-1]["uow"] is not None, "on_save writes on the save's unit of work"

    def test_update_confidence_direct_is_one_confidence_update(self, spy):
        obj = ConfProbe.create(name="up")
        spy.clear()
        new = ConfidenceField.update_confidence(obj, "certainty", signal=0.9)
        assert spy.calls == ["confidence_update"], spy.calls
        assert isinstance(new, float) and new > 0.5
        assert obj.certainty == new
        _, kwargs = spy.last("confidence_update")
        assert kwargs["require_record"] == obj.db_key.redis_key
        assert kwargs["initial"] == 0.5
        assert kwargs["cap"] == ConfProbe._meta.fields["certainty"].evidence_cap
        assert kwargs["uow"] is None if "uow" in kwargs else True

    def test_absent_record_is_none_from_the_backend_and_type_error_here(self, spy):
        # Deviation 7 (#732): the backend returns None for an absent
        # require_record; the field layer owns the TypeError and its wording.
        unsaved = ConfProbe(name="never-saved")
        spy.clear()
        with pytest.raises(TypeError, match="requires a saved model instance"):
            ConfidenceField.update_confidence(unsaved, "certainty", signal=0.9)
        # The call reached the backend (so the TypeError came from its None
        # reply, not from an earlier guard) and nothing else was issued.
        assert spy.calls == ["confidence_update"], spy.calls

    def test_update_confidence_on_a_pipeline_queues_on_it(self, spy):
        obj = ConfProbe.create(name="queued")
        gone = ConfProbe.create(name="gone")
        gone_key = gone.db_key.redis_key
        popoto.get_redis().delete(gone_key)  # record gone, hash entry remains
        pipe = spy.begin()
        spy.clear()

        assert ConfidenceField.update_confidence(obj, "certainty", 0.9, pipe) is None
        assert ConfidenceField.update_confidence(gone, "certainty", 0.9, pipe) is None
        assert spy.calls == ["confidence_update", "confidence_update"]
        for _, _, kwargs in spy.kwargs:
            assert kwargs["uow"] is pipe
            assert kwargs["require_record"] is not None
        assert ConfidenceField.get_confidence(obj, "certainty") == 0.5, "queued"
        pipe.commit()
        assert ConfidenceField.get_confidence(obj, "certainty") > 0.5
        # The script's KEYS[2] guard skipped the deleted record server-side.
        data = ConfidenceField.get_confidence_data(gone, "certainty")
        assert data["evidence_count"] == 0

    def test_reads_go_through_map_get(self, spy):
        obj = ConfProbe.create(name="read")
        spy.clear()
        assert ConfidenceField.get_confidence(obj, "certainty") == 0.5
        assert spy.calls == ["map_get"]
        spy.clear()
        assert (
            ConfidenceField.get_confidence_data(obj, "certainty")["corroborations"] == 0
        )
        assert spy.calls == ["map_get"]
        spy.clear()
        state = ConfidenceField.export_state(obj, "certainty", None)
        assert spy.calls == ["map_get"]
        assert state == {
            "confidence": 0.5,
            "evidence_count": 0,
            "corroborations": 0,
            "contradictions": 0,
        }

    def test_import_state_overwrites_through_map_set(self, spy):
        obj = ConfProbe.create(name="imp")
        spy.clear()
        ConfidenceField.import_state(
            obj,
            "certainty",
            {"confidence": 0.8, "evidence_count": 3, "corroborations": 2},
        )
        assert spy.calls == ["map_set"]
        _, kwargs = spy.last("map_set")
        assert not kwargs.get("only_if_absent"), "import must overwrite the seed"
        data = ConfidenceField.get_confidence_data(obj, "certainty")
        assert (data["confidence"], data["evidence_count"]) == (0.8, 3)

    def test_filtered_scan_goes_through_map_scan_at_count_100(self, spy):
        ConfProbe.create(name="scana")
        ConfProbe.create(name="scanb")
        spy.clear()
        found = ConfidenceField.get_confidence_filtered(
            ConfProbe, "certainty", pattern="ConfProbe:scan*"
        )
        assert spy.calls == ["map_scan"]
        args, kwargs = spy.last("map_scan")
        assert args[1] == "ConfProbe:scan*"
        assert kwargs["count"] == 100, "the HSCAN batch hint this loop always used"
        assert set(found) == {"ConfProbe:scana", "ConfProbe:scanb"}
        assert all(isinstance(k, str) for k in found)

    def test_delete_removes_the_entry_through_map_delete(self, spy):
        obj = ConfProbe.create(name="del")
        spy.clear()
        obj.delete()
        assert "map_delete" in spy.calls, spy.calls
        _, kwargs = spy.last("map_delete")
        assert (
            kwargs["uow"] is not None
        ), "on_delete writes on the delete's unit of work"
        assert ConfidenceField.get_confidence(obj, "certainty") == 0.5

    def test_partition_change_moves_the_entry_through_the_map_family(self, spy):
        obj = PartProbe.create(name="mv", project="alpha")
        ConfidenceField.update_confidence(obj, "certainty", 0.9)
        field = PartProbe._meta.fields["certainty"]
        old_hash = field.get_data_hash_key(obj, "certainty")
        reloaded = PartProbe.query.get(name="mv", project="alpha")
        spy.clear()

        reloaded.project = "beta"
        reloaded.save(migrate_key=True)

        # on_delete read the old entry (map_get), on_save restored it into the
        # new partition (map_set) and the old one was dropped (map_delete).
        assert spy.calls.index("map_get") < spy.calls.index("map_set")
        assert "map_delete" in spy.calls
        moved = PartProbe.query.get(name="mv", project="beta")
        data = ConfidenceField.get_confidence_data(moved, "certainty")
        assert data["evidence_count"] == 1, "learned state survived the move"
        assert popoto.get_redis().hget(old_hash, obj.db_key.redis_key) is None

    def test_migrate_to_partitioned_goes_through_scan_load_set_drop(self, spy):
        field = PartProbe._meta.fields["certainty"]
        base = field.get_special_use_field_db_key(PartProbe, "certainty")
        unpartitioned = base.redis_key + ":data"
        a = PartProbe.create(name="m-a", project="p1")
        b = PartProbe.create(name="m-b", project="p2")
        legacy = msgpack.packb(
            {
                "confidence": 0.7,
                "evidence_count": 2,
                "corroborations": 2,
                "contradictions": 0,
            }
        )
        client = popoto.get_redis()
        client.hset(unpartitioned, a.db_key.redis_key, legacy)
        client.hset(unpartitioned, b.db_key.redis_key, legacy)
        client.hset(unpartitioned, "PartProbe:orphan:p9", legacy)
        spy.clear()

        report = ConfidenceField.migrate_to_partitioned(PartProbe, "certainty")

        assert spy.calls[0] == "map_scan"
        _, kwargs = spy.last("map_scan")
        assert kwargs["count"] == 1000
        assert spy.calls.count("load_record") == 3
        assert spy.calls.count("map_set") == 2
        assert spy.calls[-1] == "drop_index"
        args, _ = spy.last("drop_index")
        assert args == (unpartitioned, "map")
        assert report["total"] == 3 and report["migrated"] == 2
        assert report["partitions"] == {"p1": 1, "p2": 1}
        assert [e["member_key"] for e in report["errors"]] == ["PartProbe:orphan:p9"]
        assert not client.exists(unpartitioned)
        assert ConfidenceField.get_confidence_data(a, "certainty")["confidence"] == 0.7

    def test_migrate_dry_run_writes_nothing(self, spy):
        field = PartProbe._meta.fields["certainty"]
        base = field.get_special_use_field_db_key(PartProbe, "certainty")
        unpartitioned = base.redis_key + ":data"
        a = PartProbe.create(name="d-a", project="p1")
        popoto.get_redis().hset(
            unpartitioned, a.db_key.redis_key, msgpack.packb({"confidence": 0.7})
        )
        spy.clear()
        report = ConfidenceField.migrate_to_partitioned(
            PartProbe, "certainty", dry_run=True
        )
        assert report["migrated"] == 0 and report["total"] == 1
        assert "map_set" not in spy.calls and "drop_index" not in spy.calls
        assert popoto.get_redis().exists(unpartitioned)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


class TestSourceShape:
    """Pin the grep criterion and the (empty) ``native()`` ledger on the source.

    Source checks are not seam checks: a site that swaps ``map_set(...,
    uow=p)`` for ``p.hset(...)`` has no ``get_REDIS_DB()`` in it. The
    recording tests above are the real guard; the command-name sweep below
    narrows the gap, and because this slice has no ``native()`` site at all
    it needs no "ledgered function" exemption -- *no* Redis-command-shaped
    attribute call may appear anywhere in either file.
    """

    REDIS_COMMANDS = frozenset(
        "hset hget hgetall hdel hmget hsetnx hscan hincrby hincrbyfloat hlen "
        "sadd srem smembers scard sismember zadd zrem zincrby zscore zrange "
        "zcard zrangebyscore expire expireat eval evalsha sscan zscan scan_iter "
        "exists delete pipeline execute_command script_load".split()
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
    def test_no_accessor_or_snapshot_remains(self, name):
        tree = self.TREES[name]
        assert self._calls_named(tree, "get_REDIS_DB") == [], name
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "POPOTO_REDIS_DB", (name, node.lineno)
            if isinstance(node, ast.Attribute):
                assert node.attr != "POPOTO_REDIS_DB", (name, node.lineno)
            if isinstance(node, ast.ImportFrom):
                imported = {a.name for a in node.names}
                assert "POPOTO_REDIS_DB" not in imported, (name, node.lineno)
                assert "get_REDIS_DB" not in imported, (name, node.lineno)
                assert "run_lua" not in imported, (name, node.lineno)
            if isinstance(node, ast.Import):
                assert all(a.name != "redis" for a in node.names), (
                    f"{name}:{node.lineno} imports redis; the pipeline check "
                    "is duck-typed (architect decision 1)"
                )

    @pytest.mark.parametrize("name", sorted(OWNED))
    def test_no_lua_is_evaluated_here(self, name):
        assert self._calls_named(self.TREES[name], "run_lua") == [], name

    @pytest.mark.parametrize("name", sorted(OWNED))
    def test_native_ledger_is_empty(self, name):
        # The PR body's ledger for WS1d: zero sites. A new one is a deliberate
        # addition that bumps this number, names its feature in a
        # ``# native(): <feature>`` comment and lands in the ledger.
        assert self._calls_named(self.TREES[name], "native") == [], name

    @pytest.mark.parametrize("name", sorted(OWNED))
    def test_no_command_is_issued_on_a_client_or_unit_of_work(self, name):
        offenders = [
            (node.lineno, node.func.attr)
            for node in ast.walk(self.TREES[name])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in self.REDIS_COMMANDS
        ]
        assert offenders == [], (name, offenders)

    @pytest.mark.parametrize("name", sorted(OWNED))
    def test_no_isinstance_pipeline_check_remains(self, name):
        assert "isinstance(pipeline, redis" not in self.SOURCES[name], name

    @pytest.mark.parametrize("name", sorted(OWNED))
    def test_get_backend_is_imported_from_the_backends_package(self, name):
        found = [
            node
            for node in ast.walk(self.TREES[name])
            if isinstance(node, ast.ImportFrom)
            and node.module == "backends"
            and "get_backend" in {a.name for a in node.names}
        ]
        assert found, f"{name} does not import get_backend from ..backends"

    def test_lua_constants_stay_re_exported(self):
        # tests/test_lua_decay_scoring.py and test_confidence_modulated_decay.py
        # import DECAY_SCORE_LUA from the field module; keep that door open.
        assert decaying_sorted_field.DECAY_SCORE_LUA is redis_backend.DECAY_SCORE_LUA
        assert (
            confidence_field.CAPPED_BAYESIAN_UPDATE_LUA
            is redis_backend.CAPPED_BAYESIAN_UPDATE_LUA
        )
