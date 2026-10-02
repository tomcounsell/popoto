"""``fields/validity_field.py`` and ``fields/supersession.py`` reach storage
through ``get_backend()`` (#631 WS1e).

Two guards, in opposite directions, mirroring
``tests/test_models_base_routes_through_backend.py`` (WS1a):

* **Behavioural.** A recording backend that delegates every call to the real
  ``RedisBackend`` is installed with ``set_backend()``. Each validity entry
  point must show up as a call on the *backend* -- ``save`` as ``supersede``
  (mode ``open``), ``delete`` as ``drop_validity``, ``resolve_*_keys`` as
  ``interval_members``, ``is_valid_at`` / ``get_valid_from`` as
  ``interval_of``, chain reads as ``map_get`` -- so a site that quietly goes
  back to ``get_REDIS_DB()`` fails here even though Redis would still have
  done the right thing. Delegation keeps the exercised path real; the
  recorder only watches, and also keeps the keyword arguments so the WS0
  deviations (2: ``now`` travels; 3: ``drop_validity`` takes the member;
  10: ``model_prefix`` is ``$ValidityF:<Model>``) are asserted on the wire.

* **Shape.** The source itself: no ``get_REDIS_DB()`` call, no ``run_lua(``,
  exactly as many ``native()`` sites as the PR body ledgers (three, all on
  the transfer path), each with its naming comment, and no
  ``isinstance(pipeline, redis...)`` dispatch (architect decision 1).

Tests that read ``$ValidityF:*`` keys through the raw client are marked
``redis_only``. Nothing here is marked ``conformance``: the spy wraps
``RedisBackend`` by construction, and every validity method is still a
``PostgresBackend`` stub (WS3e), so a Postgres leg could only xfail
wholesale. The spy is installed per test and reset in ``finally``;
``set_backend(None)`` makes the next ``get_backend()`` re-run selection.
"""

from __future__ import annotations

import ast
import re
import time
from pathlib import Path

import pytest
import redis.exceptions

import popoto
from popoto import backends
from popoto.backends import redis as redis_backend_module
from popoto.backends.redis import RedisBackend, _validity_keys
from popoto.fields import supersession as supersession_module
from popoto.fields import validity_field as validity_module
from popoto.fields.supersession import SupersedeResult, SupersessionProtocol
from popoto.fields.validity_field import (
    ValidityCloseBeforeStartError,
    ValidityField,
    ValidityMemberAbsentError,
    ValidityValidFromConflictError,
)

FIELDS_DIR = Path(popoto.__file__).parent / "fields"
VALIDITY_PY = FIELDS_DIR / "validity_field.py"
SUPERSESSION_PY = FIELDS_DIR / "supersession.py"


class RecordingBackend:
    """Delegates to a real ``RedisBackend``; records names and keyword args."""

    def __init__(self) -> None:
        self._real = RedisBackend()
        self.calls: list[str] = []
        self.invocations: list[tuple[str, tuple, dict]] = []

    def clear(self) -> None:
        self.calls.clear()
        self.invocations.clear()

    def kwargs_of(self, name: str) -> dict:
        """The keyword arguments of the most recent call to ``name``."""
        for recorded, _args, kwargs in reversed(self.invocations):
            if recorded == name:
                return kwargs
        raise AssertionError(f"{name} was never called; calls={self.calls}")

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def _recorded(*args, **kwargs):
            self.calls.append(name)
            self.invocations.append((name, args, kwargs))
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


class Claim(popoto.Model):
    name = popoto.KeyField()
    validity = popoto.ValidityField()


def _key(name: str) -> str:
    return Claim(name=name).db_key.redis_key


def _save(name: str, **kwargs) -> Claim:
    obj = Claim(name=name, **kwargs)
    obj.save()
    return obj


def _raw_interval(obj: Claim) -> tuple:
    """``(valid_from, invalid_at)`` straight off the Redis client."""
    keys = ValidityField.get_all_keys(Claim, "validity")
    client = popoto.get_redis()
    member = obj.db_key.redis_key
    return (
        client.zscore(keys["valid_from"], member),
        client.zscore(keys["invalid_at"], member),
    )


# ---------------------------------------------------------------------------
# Field hooks and classmethods
# ---------------------------------------------------------------------------


class TestFieldHooksReachTheBackend:
    def test_save_opens_through_supersede_in_mode_open(self, spy):
        _save("a")
        # ``begin`` is not asserted: ``Model.save`` opening its unit of work
        # on the backend is WS1a's observation (``models/base.py``), not this
        # file's; on this branch alone the hook is what WS1e owns.
        assert "supersede" in spy.calls, spy.calls
        kwargs = spy.kwargs_of("supersede")
        assert kwargs["mode"] == "open"
        assert kwargs["new_member"] == _key("a")
        assert kwargs["pointer_digest"] is None, "a plain save names no identity"
        assert kwargs["uow"] is not None, "save() queues onto its own unit of work"
        # Deviation 10: the opaque namespace and the field name, separately --
        # never the per-field ``$ValidityF:Claim:validity`` prefix.
        _name, args, _kwargs = spy.invocations[spy.calls.index("supersede")]
        assert args == ("$ValidityF:Claim", "validity"), args

    def test_declared_valid_from_pre_check_goes_through_interval_of(self, spy):
        t0 = time.time() - 3600.0
        _save("b", validity=t0)
        assert "interval_of" in spy.calls, "pre_save_validate bypassed interval_of"
        assert spy.calls.index("interval_of") < spy.calls.index("supersede")
        # And the conflict the pre-check exists for is still raised through it.
        again = Claim(name="b", validity=t0 - 5.0)
        spy.clear()
        with pytest.raises(ValidityValidFromConflictError):
            again.save()
        assert spy.calls == ["interval_of"], spy.calls

    def test_execute_supersede_direct_returns_the_closed_key(self, spy):
        old = _save("old")
        new = _save("new")
        spy.clear()
        closed = ValidityField.execute_supersede(
            Claim,
            "validity",
            new_member=new.db_key.redis_key,
            mode="supersede",
            old_member=old.db_key.redis_key,
        )
        assert closed == old.db_key.redis_key
        assert spy.calls == ["supersede"], spy.calls
        kwargs = spy.kwargs_of("supersede")
        assert kwargs["uow"] is None
        assert kwargs["mode"] == "supersede"
        assert kwargs["old_member"] == old.db_key.redis_key

    @pytest.mark.redis_only
    def test_execute_supersede_on_a_unit_of_work_queues_and_returns_it(self, spy):
        old = _save("old")
        pipe = spy.begin()
        spy.clear()
        returned = ValidityField.execute_supersede(
            Claim,
            "validity",
            mode="invalidate",
            old_member=old.db_key.redis_key,
            pipeline=pipe,
        )
        assert returned is pipe
        assert spy.calls == ["supersede"]
        assert spy.kwargs_of("supersede")["uow"] is pipe
        assert _raw_interval(old)[1] == float("inf"), "nothing applied before commit"
        pipe.commit()
        assert _raw_interval(old)[1] != float("inf")

    @pytest.mark.redis_only
    def test_deviation_2_the_clock_travels_to_the_script(self, spy):
        obj = _save("clock")
        popoto.get_redis().delete(
            *ValidityField.get_all_keys(Claim, "validity").values()
        )
        spy.clear()
        # ``now`` is ARGV[2]; with no explicit ``valid_from`` / ``ingested_at``
        # the script defaults both to it, so the stored scores prove the clock
        # the field layer passed is the one the backend forwarded.
        ValidityField.execute_supersede(
            Claim, "validity", new_member=obj.db_key.redis_key, mode="open", now=12345.5
        )
        kwargs = spy.kwargs_of("supersede")
        assert kwargs["now"] == 12345.5
        assert kwargs["valid_from"] is None and kwargs["ingested_at"] is None
        assert _raw_interval(obj) == (12345.5, float("inf"))

    @pytest.mark.redis_only
    def test_deviation_10_model_prefix_is_byte_equal_to_the_field_helpers(self):
        prefix = ValidityField._model_prefix(Claim)
        assert prefix == "$ValidityF:Claim"
        assert f"{prefix}:validity" == (
            ValidityField.get_prefix_db_key(Claim, "validity").redis_key
        )
        # All five keys the backend derives, plus the sixth (the pointer).
        assert _validity_keys(prefix, "validity") == ValidityField.get_all_keys(
            Claim, "validity"
        )
        digest = SupersessionProtocol.identity_key("user_42", "plan")
        assert f"{prefix}:validity:open:{digest}" == (
            ValidityField.get_open_pointer_key(Claim, "validity", digest)
        )

    @pytest.mark.redis_only
    def test_delete_goes_through_drop_validity_and_clears_every_key(self, spy):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        obj = _save("d")
        SupersessionProtocol.supersede(obj, identity_key=identity)
        pointer = ValidityField.get_open_pointer_key(Claim, "validity", identity)
        client = popoto.get_redis()
        assert client.exists(pointer)
        spy.clear()
        assert obj.delete() is True
        assert "drop_validity" in spy.calls, spy.calls
        kwargs = spy.kwargs_of("drop_validity")
        # Deviation 3: the backend takes the member and scans for pointers.
        assert "pointer_digest" not in kwargs
        assert spy.invocations[spy.calls.index("drop_validity")][1][2] == (
            obj.db_key.redis_key
        )
        assert _raw_interval(obj) == (None, None)
        assert not client.exists(pointer)

    @pytest.mark.redis_only
    def test_on_delete_on_a_unit_of_work_queues_and_returns_it(self, spy):
        obj = _save("q")
        pipe = spy.begin()
        spy.clear()
        returned = ValidityField.on_delete(obj, "validity", None, pipeline=pipe)
        assert returned is pipe
        assert spy.calls == ["drop_validity"]
        assert spy.kwargs_of("drop_validity")["uow"] is pipe
        assert _raw_interval(obj)[0] is not None, "nothing removed before commit"
        pipe.commit()
        assert _raw_interval(obj) == (None, None)

    def test_resolve_keys_go_through_interval_members(self, spy):
        obj = _save("r")
        spy.clear()
        assert ValidityField.resolve_valid_keys(Claim, "validity") == {
            obj.db_key.redis_key
        }
        assert spy.calls == ["interval_members"], spy.calls
        assert spy.kwargs_of("interval_members")["select"] == "valid"
        spy.clear()
        assert ValidityField.resolve_excluded_keys(Claim, "validity") == set()
        assert spy.calls == ["interval_members"], spy.calls
        assert spy.kwargs_of("interval_members")["select"] == "excluded"

    def test_exclusion_rule_is_exact_at_both_boundaries(self, spy):
        """``invalid_at <= as_of OR valid_from > as_of``; absence includes."""
        as_of = time.time() - 1000.0
        open_rec = _save("open", validity=as_of - 50.0)
        closed = _save("closed", validity=as_of - 50.0)
        SupersessionProtocol.invalidate(closed, at=as_of - 10.0)
        edge_close = _save("edge_close", validity=as_of - 50.0)
        SupersessionProtocol.invalidate(edge_close, at=as_of)  # == as_of: excluded
        edge_start = _save("edge_start", validity=as_of)  # == as_of: included
        future = _save("future", validity=as_of + 10.0)

        excluded = ValidityField.resolve_excluded_keys(Claim, "validity", as_of)
        assert excluded == {
            closed.db_key.redis_key,
            edge_close.db_key.redis_key,
            future.db_key.redis_key,
        }
        for included in (open_rec, edge_start):
            assert included.db_key.redis_key not in excluded
        # A key with no interval entry at all is simply not in the set.
        assert _key("never-indexed") not in excluded
        # The whitelist form is the exact mirror.
        assert ValidityField.resolve_valid_keys(Claim, "validity", as_of) == {
            open_rec.db_key.redis_key,
            edge_start.db_key.redis_key,
        }

    def test_is_valid_at_and_get_valid_from_go_through_interval_of(self, spy):
        t0 = time.time() - 100.0
        obj = _save("v", validity=t0)
        spy.clear()
        assert ValidityField.is_valid_at(Claim, "validity", obj.db_key.redis_key)
        assert spy.calls == ["interval_of"], spy.calls
        spy.clear()
        assert ValidityField.get_valid_from(obj, "validity") == t0
        assert spy.calls == ["interval_of"], spy.calls
        assert ValidityField.get_valid_from(Claim, "validity", _key("nobody")) is None

    def test_plus_inf_sentinel_passes_through_unchanged(self, spy):
        obj = _save("inf")
        start, close = RedisBackend().interval_of(
            *ValidityField.get_interval_keys(Claim, "validity"), obj.db_key.redis_key
        )
        assert close == float("inf")
        assert ValidityField.is_valid_at(
            Claim, "validity", obj.db_key.redis_key, as_of=start + 10**9
        )
        exported = ValidityField.export_state(obj, "validity", obj.validity)
        assert exported is not None
        assert exported["invalid_at"] == ValidityField.OPEN_SENTINEL_TOKEN

    def test_filter_query_goes_through_the_backend_and_keeps_bytes(self, spy):
        """Deviation 11 is WS1b's: ``filter_for_keys_set`` still intersects the
        raw ``bytes`` replies of the other index fields, so a ``str`` set here
        would make ``filter(validity__current=True, name=...)`` empty."""
        obj = _save("f")
        gone = _save("g")
        SupersessionProtocol.invalidate(gone)
        spy.clear()
        current = ValidityField.filter_query(Claim, "validity", validity__current=True)
        assert isinstance(current, set)
        assert current == {obj.db_key.redis_key.encode()}
        assert spy.calls == ["interval_members"], spy.calls

        spy.clear()
        complement = ValidityField.filter_query(
            Claim, "validity", validity__current=False
        )
        assert complement == {gone.db_key.redis_key.encode()}
        assert "sorted_members" in spy.calls and "interval_members" in spy.calls

        spy.clear()
        as_of = ValidityField.filter_query(
            Claim, "validity", validity__as_of=time.time()
        )
        assert as_of == {obj.db_key.redis_key.encode()}
        assert spy.calls == ["interval_members"], spy.calls

        # The public combination that the bytes contract protects.
        assert [
            c.name for c in Claim.query.filter(validity__current=True, name="f")
        ] == ["f"]
        assert Claim.query.filter(validity__current=True, name="g").all() == []

    def test_typed_errors_cross_the_seam(self, spy):
        with pytest.raises(ValidityMemberAbsentError):
            SupersessionProtocol.invalidate(Claim(name="ghost"))
        assert spy.calls == ["supersede"], spy.calls

        t0 = time.time() - 100.0
        obj = _save("c", validity=t0)
        spy.clear()
        with pytest.raises(ValidityCloseBeforeStartError):
            SupersessionProtocol.invalidate(obj, at=t0 - 1.0)
        assert spy.calls == ["supersede"], spy.calls

        spy.clear()
        with pytest.raises(ValidityValidFromConflictError):
            ValidityField.execute_supersede(
                Claim,
                "validity",
                new_member=obj.db_key.redis_key,
                mode="open",
                valid_from=t0 - 1.0,
                assert_valid_from=True,
            )
        assert spy.calls == ["supersede"], spy.calls

    def test_error_mapper_still_serves_every_token(self):
        """WS0 chose: the backend raises the typed exception itself, through
        this module's mapper. Every ``error_reply`` token maps, and the map is
        still importable from here (``recipes.provenance_journal`` and the
        Redis backend both do)."""
        expected = {
            validity_module.MEMBER_ABSENT_ERROR: ValidityMemberAbsentError,
            validity_module.CLOSE_BEFORE_START_ERROR: ValidityCloseBeforeStartError,
            validity_module.VALID_FROM_CONFLICT_ERROR: ValidityValidFromConflictError,
        }
        assert dict(validity_module._LUA_ERROR_MAP) == expected
        for token, exc_type in expected.items():
            mapped = validity_module.map_lua_error(
                redis.exceptions.ResponseError(f"{token} detail")
            )
            assert isinstance(mapped, exc_type)
        # The script text that produces those tokens is the backend's one copy.
        assert validity_module.SUPERSEDE_LUA is redis_backend_module.SUPERSEDE_LUA
        for token in expected:
            assert token in validity_module.SUPERSEDE_LUA


# ---------------------------------------------------------------------------
# SupersessionProtocol
# ---------------------------------------------------------------------------


class TestSupersessionProtocolReachesTheBackend:
    def test_supersede_and_invalidate_go_through_supersede(self, spy):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        old = _save("free")
        spy.clear()
        assert SupersessionProtocol.supersede(old, identity_key=identity) is None
        assert spy.calls == ["supersede"], spy.calls
        assert spy.kwargs_of("supersede")["pointer_digest"] == identity

        new = _save("enterprise")
        spy.clear()
        assert SupersessionProtocol.supersede(new, identity_key=identity) == (
            old.db_key.redis_key
        )
        assert spy.calls == ["supersede"], spy.calls

        newer = _save("newer")
        spy.clear()
        assert SupersessionProtocol.invalidate(new, superseded_by=newer) == (
            new.db_key.redis_key
        )
        assert spy.calls == ["supersede"], spy.calls
        kwargs = spy.kwargs_of("supersede")
        assert kwargs["mode"] == "invalidate"
        assert kwargs["pointer_digest"] is None

    def test_save_and_supersede_opens_the_unit_of_work_on_the_backend(self, spy):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        old = _save("free")
        SupersessionProtocol.supersede(old, identity_key=identity)
        spy.clear()
        result = SupersessionProtocol.save_and_supersede(
            Claim(name="enterprise"), identity_key=identity
        )
        assert isinstance(result, SupersedeResult)
        assert spy.calls[0] == "begin", spy.calls
        assert "supersede" in spy.calls
        assert result.closed_key == old.db_key.redis_key
        assert result.pipeline is None and result.close_index is None
        assert Claim.exists(name="enterprise")

    def test_save_and_supersede_on_a_caller_pipeline_does_not_commit(self, spy):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        old = _save("free")
        SupersessionProtocol.supersede(old, identity_key=identity)
        pipe = spy.begin()
        spy.clear()
        result = SupersessionProtocol.save_and_supersede(
            Claim(name="enterprise"), identity_key=identity, pipeline=pipe
        )
        assert "begin" not in spy.calls, "a caller pipeline is never replaced"
        assert spy.kwargs_of("supersede")["uow"] is pipe
        assert result.pipeline is pipe and result.closed_key is None
        assert not Claim.exists(name="enterprise")
        results = pipe.commit()
        closed = results[result.close_index]
        assert (closed.decode() if isinstance(closed, bytes) else closed) == (
            old.db_key.redis_key
        )

    def test_caller_pipeline_validation_is_duck_typed(self, spy):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        with pytest.raises(ValueError, match="must be a redis Pipeline") as ei:
            SupersessionProtocol.save_and_supersede(
                Claim(name="x"), identity_key=identity, pipeline=object()
            )
        assert str(ei.value) == (
            "SupersessionProtocol.save_and_supersede: pipeline must be a "
            "redis Pipeline, got object"
        )
        loose = popoto.get_redis().pipeline(transaction=False)
        with pytest.raises(ValueError, match="transaction=False"):
            SupersessionProtocol.save_and_supersede(
                Claim(name="x"), identity_key=identity, pipeline=loose
            )
        assert "supersede" not in spy.calls

    def test_chain_walks_through_map_get_and_interval_of(self, spy):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        records = []
        for name in ("v1", "v2", "v3"):
            rec = _save(name)
            SupersessionProtocol.supersede(rec, identity_key=identity)
            records.append(rec)
        v1, v2, v3 = records
        spy.clear()
        assert [c.name for c in SupersessionProtocol.chain(v2)] == ["v1", "v2", "v3"]
        assert "interval_of" in spy.calls and "map_get" in spy.calls, spy.calls
        assert "native" not in spy.calls
        spy.clear()
        assert SupersessionProtocol.superseded_by(v1).name == "v2"
        assert spy.calls[0] == "map_get", spy.calls
        spy.clear()
        assert SupersessionProtocol.supersedes(v3).name == "v2"
        assert spy.calls[0] == "map_get", spy.calls
        # Hydration is ``Query.get``'s business (WS1a routes it); nothing else
        # of this module's may reach storage on a one-hop walk.
        assert set(spy.calls) <= {"map_get", "load_record"}, spy.calls
        assert SupersessionProtocol.chain(Claim(name="unsaved")) == []

    @pytest.mark.redis_only
    def test_dangling_link_still_ends_the_chain(self, spy):
        v1 = _save("v1")
        popoto.get_redis().hset(
            ValidityField.get_chain_fwd_key(Claim, "validity"),
            v1.db_key.redis_key,
            "Claim:ghost",
        )
        assert [c.name for c in SupersessionProtocol.chain(v1)] == ["v1"]
        assert SupersessionProtocol.superseded_by(v1) is None


# ---------------------------------------------------------------------------
# The transfer path: out of the slice, through the escape hatch
# ---------------------------------------------------------------------------


class TestTransferPathUsesTheEscapeHatch:
    def test_export_import_and_pointer_scan_use_native(self, spy):
        identity = SupersessionProtocol.identity_key("user_42", "plan")
        obj = _save("e")
        SupersessionProtocol.supersede(obj, identity_key=identity)
        spy.clear()
        state = ValidityField.export_state(obj, "validity", obj.validity)
        assert state is not None and state["open_pointers"] == [identity]
        assert "native" in spy.calls, spy.calls
        assert "interval_of" not in spy.calls
        spy.clear()
        assert ValidityField.find_open_pointers_for_member(
            Claim, "validity", obj.db_key.redis_key
        ) == [ValidityField.get_open_pointer_key(Claim, "validity", identity)]
        assert spy.calls == ["native"], spy.calls
        spy.clear()
        ValidityField.import_state(obj, "validity", state)
        assert spy.calls == ["native"], spy.calls

    @pytest.mark.redis_only
    def test_export_import_round_trip_restores_a_closed_interval(self, spy):
        t0 = time.time() - 100.0
        obj = _save("rt", validity=t0)
        SupersessionProtocol.invalidate(obj, at=t0 + 10.0)
        state = ValidityField.export_state(obj, "validity", obj.validity)
        assert state == {
            "valid_from": t0,
            "invalid_at": t0 + 10.0,
            "ingested_at": state["ingested_at"],
            "chain_fwd": None,
            "chain_rev": None,
            "open_pointers": [],
        }
        obj.delete()
        assert _raw_interval(obj) == (None, None)
        fresh = _save("rt", validity=t0)  # on_save reopens the interval
        assert _raw_interval(fresh) == (t0, float("inf"))
        ValidityField.import_state(fresh, "validity", state)
        assert _raw_interval(fresh) == (t0, t0 + 10.0)


# ---------------------------------------------------------------------------
# Source shape
# ---------------------------------------------------------------------------


class _Shape:
    """AST helpers over one source file."""

    #: Redis-command-shaped attribute names. ``get`` / ``set`` / ``delete`` /
    #: ``exists`` / ``keys`` are left out because dicts, kwargs and ``Model``
    #: have methods of those names; a bypass using only those is caught by the
    #: recording tests above.
    REDIS_COMMANDS = frozenset(
        "hset hget hgetall hdel hmget sadd srem smembers zadd zrem zscore "
        "zrange zrangebyscore zrevrangebyscore scan scan_iter eval evalsha "
        "pipeline execute".split()
    )

    def __init__(self, path: Path) -> None:
        self.path = path
        self.source = path.read_text()
        self.tree = ast.parse(self.source)

    def calls_named(self, name: str) -> list[int]:
        return [
            node.lineno
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == name)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
            )
        ]

    def functions(self) -> list[ast.FunctionDef]:
        return [n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)]


SHAPES = {path.name: _Shape(path) for path in (VALIDITY_PY, SUPERSESSION_PY)}
NATIVE_LEDGER = {"validity_field.py": 3, "supersession.py": 0}


@pytest.mark.parametrize("filename", sorted(SHAPES))
class TestSourceShape:
    """Pin the grep criterion and the ``native()`` ledger on both files.

    Source checks, not seam checks: the recording tests above are the real
    guard and these narrow the gap for command names they cannot see.
    """

    def test_no_accessor_or_script_call_remains(self, filename):
        shape = SHAPES[filename]
        assert shape.calls_named("get_REDIS_DB") == []
        assert shape.calls_named("run_lua") == []
        assert shape.calls_named("scan_keys") == []
        for node in ast.walk(shape.tree):
            if isinstance(node, ast.Name):
                assert node.id != "POPOTO_REDIS_DB", node.lineno
            if isinstance(node, ast.Attribute):
                assert node.attr != "POPOTO_REDIS_DB", node.lineno
            if isinstance(node, ast.ImportFrom):
                names = {a.name for a in node.names}
                assert "POPOTO_REDIS_DB" not in names
                assert "get_REDIS_DB" not in names
                assert "run_lua" not in names
                assert "scan_keys" not in names

    def test_native_sites_match_the_ledger_and_are_commented(self, filename):
        shape = SHAPES[filename]
        sites = shape.calls_named("native")
        assert len(sites) == NATIVE_LEDGER[filename], sites
        lines = shape.source.splitlines()
        for lineno in sites:
            window = "\n".join(lines[max(0, lineno - 6) : lineno])
            assert re.search(r"#\s*native\(\):", window), (
                f"native() at {filename}:{lineno} has no '# native(): <feature>' "
                "comment within the five lines above it"
            )

    def test_no_command_is_issued_outside_a_ledgered_function(self, filename):
        shape = SHAPES[filename]
        natives = shape.calls_named("native")
        functions = shape.functions()
        offenders = []
        for node in ast.walk(shape.tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ):
                continue
            if node.func.attr not in shape.REDIS_COMMANDS:
                continue
            holders = [f for f in functions if f.lineno <= node.lineno <= f.end_lineno]
            ledgered = any(
                f.lineno <= ln <= f.end_lineno for f in holders for ln in natives
            )
            if not ledgered:
                offenders.append((node.lineno, node.func.attr))
        assert offenders == [], offenders

    def test_no_isinstance_pipeline_dispatch_remains(self, filename):
        # Architect decision 1: the field layer checks ``uow is not None``.
        shape = SHAPES[filename]
        assert "isinstance(pipeline, redis" not in shape.source
        assert "import redis.client" not in shape.source


def test_supersede_lua_is_still_re_exported_for_its_readers():
    """``tests/test_validity_field.py`` reads ``validity_module.SUPERSEDE_LUA``
    and ``scripts/check_supersede_lua_phases.py`` reads the backend's text; the
    two must stay one object."""
    assert validity_module.SUPERSEDE_LUA is redis_backend_module.SUPERSEDE_LUA
    assert "-- MUTATION PHASE" in validity_module.SUPERSEDE_LUA


def test_neither_module_binds_the_accessor_names():
    """The stale-snapshot shape (#655) and the accessor itself are both absent
    from the module namespaces, not only from the call sites."""
    for module in (validity_module, supersession_module):
        assert not hasattr(module, "get_REDIS_DB"), module.__name__
        assert not hasattr(module, "POPOTO_REDIS_DB"), module.__name__
        assert not hasattr(module, "run_lua"), module.__name__
        assert not hasattr(module, "scan_keys"), module.__name__
