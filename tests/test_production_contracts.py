"""Production contracts for the agent-memory path.

Each test here states one behavior a production deployment depends on and
that ``docs/plans/agent_memory_production_audit.md`` found violated on
main. They are written to be red until the corresponding fix lands, so the
audit is a checklist rather than an opinion. Do not mark them xfail: a red
test is the point, and the file is the record of what is still open.

Grouping follows the audit's P0 list:

- isolation:      cross-agent scoping on the default retrieval path (#576)
- fail-fast:      Redis outage must surface, not masquerade as "no memories"
- bounded cost:   per-turn Redis round trips must not scale with fetch width
- safety:         no flushing strangers' databases, no DB 0, no credential
                  leakage, no PII to third parties
- consistency:    query state per call, TTL cannot orphan index members,
                  growth is bounded
- honesty:        error messages point at methods that exist, index
                  namespaces cannot collide

Environment notes: the black-hole server used by the hook-stall test accepts
TCP connections and never replies, so the measurement is the client's own
socket timeout times its number of attempts, independent of the host's
network policy.
"""

import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import textwrap
import threading
import time
import uuid

import pytest
import redis as redis_lib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

pytestmark = pytest.mark.contract

from popoto import (  # noqa: E402
    Field,
    KeyField,
    Model,
    SortedField,
)
from popoto.fields.field import Field as FieldBase  # noqa: E402
from popoto.recipes import DefaultMemory, SubconsciousMemory  # noqa: E402
from popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CommandLog:
    """Counts Redis round trips on the shared client.

    A single command is one round trip; a pipeline ``execute()`` is one round
    trip regardless of how many commands it carries. Only the shared client
    is instrumented, which is the one every field and recipe binds at import.
    """

    def __init__(self):
        self.commands = []
        self.pipeline_executes = 0

    @property
    def round_trips(self):
        return len(self.commands) + self.pipeline_executes

    def count(self, name):
        return sum(1 for c in self.commands if c == name)


@contextlib.contextmanager
def count_round_trips():
    log = CommandLog()
    client = POPOTO_REDIS_DB
    original_execute = client.execute_command
    original_pipeline = client.pipeline

    def execute_command(*args, **kwargs):
        if args:
            log.commands.append(str(args[0]).upper())
        return original_execute(*args, **kwargs)

    def pipeline(*args, **kwargs):
        pipe = original_pipeline(*args, **kwargs)
        pipe_execute = pipe.execute

        def execute(*a, **kw):
            log.pipeline_executes += 1
            return pipe_execute(*a, **kw)

        pipe.execute = execute
        return pipe

    client.execute_command = execute_command
    client.pipeline = pipeline
    try:
        yield log
    finally:
        client.execute_command = original_execute
        client.pipeline = original_pipeline


@contextlib.contextmanager
def black_hole_server():
    """A TCP listener that accepts connections and never answers."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    port = server.getsockname()[1]
    accepted = []
    stop = threading.Event()

    def accept_loop():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                accepted.append(conn)
            except socket.timeout:
                continue
            except OSError:
                break

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    try:
        yield port, accepted
    finally:
        stop.set()
        thread.join(timeout=1)
        for conn in accepted:
            with contextlib.suppress(OSError):
                conn.close()
        server.close()


def _seed(agent_id, n, prefix="Deploys use a blue-green cutover behind the LB"):
    for i in range(n):
        DefaultMemory(
            agent_id=agent_id,
            content=f"{prefix}, note {i}",
            importance=0.8,
        ).save()


def _test_db():
    return POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db")


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_default_retrieval_path_scopes_by_agent(self):
        """Agent B must never see agent A's memories on the shipped path.

        The lexical (BM25) branch is what ``DefaultMemory`` selects, and it
        is the branch the harness hook runs. Issue #576.
        """
        alpha = SubconsciousMemory(agent_id="contract-alpha")
        alpha.extract_memories(
            "The alpha deploy token is rotated weekly by the security team.",
            importance=0.9,
        )
        beta = SubconsciousMemory(agent_id="contract-beta")
        messages = [{"role": "user", "content": "How is the deploy token rotated?"}]
        out, result = beta.inject_context(messages)

        leaked = [r for r in result.records if r.agent_id != "contract-beta"]
        assert leaked == [], f"agent-beta retrieved {len(leaked)} of alpha's records"
        assert out == messages, "nothing should have been injected for agent-beta"


# ---------------------------------------------------------------------------
# Fail-fast on outage
# ---------------------------------------------------------------------------


class TestFailFast:
    def test_inject_context_surfaces_connection_errors(self):
        """A Redis outage must not look like "no relevant memories".

        ``SubconsciousMemory.inject_context`` catches every exception and
        returns the messages unchanged with an empty result. The caller then
        proceeds with a turn that silently lost its memory layer. Connection
        failures must propagate; only retrieval-quality failures may degrade.
        """
        sm = SubconsciousMemory(agent_id="contract-outage")

        def boom(*args, **kwargs):
            raise redis_lib.exceptions.ConnectionError("simulated outage")

        sm._assembler.assemble = boom
        with pytest.raises(redis_lib.exceptions.ConnectionError):
            sm.inject_context([{"role": "user", "content": "anything"}])

    def test_read_hook_is_bounded_when_redis_hangs(self):
        """One hook invocation must finish well under the harness timeout.

        Claude Code runs the read hook synchronously on ``UserPromptSubmit``.
        On main each of five independent connection attempts waits the full
        5 s socket timeout, so a hung server stalls the user's prompt for
        ~25 s. The contract is one attempt, and a total bound of 2 s.
        """
        budget_s = 2.0
        payload = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "contract-stall",
                "cwd": "/tmp/contract-stall",
                "prompt": "What is our deployment strategy?",
            }
        )
        script = textwrap.dedent("""
            import sys
            from popoto.integrations import hooks
            hooks.run(sys.stdin.read())
            """)
        with black_hole_server() as (port, accepted):
            env = dict(os.environ)
            env.pop("POPOTO_TEST_DB", None)
            env["POPOTO_MEMORY_URL"] = f"redis://127.0.0.1:{port}/15"
            env["POPOTO_MEMORY_AGENT_ID"] = "contract-stall"
            started = time.perf_counter()
            try:
                subprocess.run(
                    [sys.executable, "-c", script],
                    input=payload,
                    text=True,
                    env=env,
                    capture_output=True,
                    timeout=budget_s + 1.0,
                )
            except subprocess.TimeoutExpired:
                pytest.fail(
                    f"read hook still running after {budget_s + 1.0:.0f}s "
                    f"against a hung server ({len(accepted)} connection "
                    "attempts observed); the user's prompt is blocked"
                )
            elapsed = time.perf_counter() - started
        assert (
            elapsed < budget_s
        ), f"read hook took {elapsed:.1f}s against a hung server"
        assert len(accepted) <= 1, (
            f"hook opened {len(accepted)} connections to a hung server; "
            "after the first failure it must stop trying"
        )


# ---------------------------------------------------------------------------
# Bounded per-turn cost
# ---------------------------------------------------------------------------


class TestBoundedCost:
    def test_inject_context_round_trips_do_not_scale_with_candidates(self):
        """Retrieval cost must be a small constant, not O(fetched candidates).

        On main, competitive suppression issues EXISTS + EVAL sequentially
        for every fetched-but-not-selected candidate, so a wider fetch
        (``max_items * 5``) means proportionally more round trips before the
        LLM call. Budget: 8 round trips per turn.
        """
        agent = "contract-cost"
        _seed(agent, 60)
        sm = SubconsciousMemory(agent_id=agent)
        messages = [{"role": "user", "content": "blue-green cutover deploy note"}]

        sm.inject_context(messages)  # warm any script caches
        with count_round_trips() as log:
            _, result = sm.inject_context(messages)

        assert result.records, "seeded memories were not retrieved"
        assert log.round_trips <= 8, (
            f"{log.round_trips} round trips for one inject_context "
            f"({len(result.records)} records injected); "
            f"commands={sorted(set(log.commands))}"
        )

    def test_lua_scripts_are_registered_not_resent(self):
        """After the first call, ranking must use EVALSHA, not re-upload Lua.

        The decay scorer is ~7 KB and is sent with EVAL on every
        ``top_by_decay``; 38 call sites use raw ``eval`` and one uses
        ``register_script``.
        """
        agent = "contract-evalsha"
        _seed(agent, 5)
        query = DefaultMemory.query.filter(agent_id=agent)
        query.top_by_decay(n=3)  # first call may legitimately EVAL once
        with count_round_trips() as log:
            DefaultMemory.query.filter(agent_id=agent).top_by_decay(n=3)
        assert (
            log.count("EVAL") == 0
        ), f"top_by_decay re-sent {log.count('EVAL')} Lua script(s) on a warm call"

    def test_get_by_non_key_field_executes_once(self):
        """``query.get(field=...)`` must not run the same query three times."""

        class ContractGetModel(Model):
            key = KeyField()
            label = Field()

        ContractGetModel.create(key="a", label="one")
        ContractGetModel.create(key="b", label="two")
        ContractGetModel.query.get(label="one")  # warm
        with count_round_trips() as baseline:
            list(ContractGetModel.query.filter(label="one"))
        with count_round_trips() as log:
            ContractGetModel.query.get(label="one")
        assert log.round_trips <= baseline.round_trips, (
            f"get() used {log.round_trips} round trips against "
            f"{baseline.round_trips} for one filter(); it re-executes the query"
        )


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


class TestSafety:
    def test_pytest_plugin_does_not_flush_downstream_databases(self, tmp_path):
        """A project that merely depends on popoto must keep its DB 15.

        The ``pytest11`` entry point activates the plugin in every
        environment where popoto is installed, and an autouse fixture
        flushes DB 15 of ``REDIS_URL`` before each test. A downstream user
        with data on DB 15 loses it by running their own unrelated tests.
        """
        db = _test_db()
        sentinel = f"contract:downstream:{uuid.uuid4().hex}"
        kwargs = dict(POPOTO_REDIS_DB.connection_pool.connection_kwargs)
        probe = redis_lib.Redis(
            host=kwargs.get("host", "localhost"),
            port=kwargs.get("port", 6379),
            db=db,
        )
        probe.set(sentinel, "keep-me")
        (tmp_path / "test_unrelated.py").write_text(
            "def test_nothing():\n    assert 1 + 1 == 2\n"
        )
        env = dict(os.environ)
        env.pop("POPOTO_TEST_DB", None)
        env["REDIS_URL"] = (
            f"redis://{kwargs.get('host', 'localhost')}:{kwargs.get('port', 6379)}/{db}"
        )
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "."],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert probe.exists(sentinel), (
            f"downstream pytest run flushed DB {db}; the popoto plugin must be "
            "opt-in for projects that did not ask for it"
        )

    def test_integration_refuses_db0_unless_opted_in(self):
        """The harness must not write memory to the database everyone uses.

        Issue #584, as settled by the maintainer: ``DEFAULT_URL`` stays at
        DB 0 and the service refuses loudly rather than silently relocating
        an adopter's corpus. ``POPOTO_MEMORY_ALLOW_DB0=1`` is the deploy-level
        opt-in. The refusal runs before any rebinding, so this test never
        moves the live connection.
        """
        from popoto.integrations.config import MemoryConfig
        from popoto.integrations.service import MemoryService

        env = {"POPOTO_MEMORY_URL": "redis://localhost:6379/0"}
        config = MemoryConfig.from_env(env, cwd="/tmp/contract-db0")
        with pytest.raises(ValueError, match="refuses to write agent memory"):
            MemoryService(config)

        opted = MemoryConfig.from_env(
            {**env, "POPOTO_MEMORY_ALLOW_DB0": "1"}, cwd="/tmp/contract-db0"
        )
        assert opted.allow_db0 is True

    def test_status_redacts_credentials(self):
        """``status()`` feeds the MCP tool and transcripts; never echo secrets."""
        from popoto.integrations.config import MemoryConfig
        from popoto.integrations.service import MemoryService

        secret = "s3cr3t-" + uuid.uuid4().hex
        config = MemoryConfig(
            url=f"redis://:{secret}@memory.internal:6379/{_test_db()}",
            agent_id="contract-status",
            url_is_explicit=False,  # keep the live connection where it is
        )
        info = MemoryService(config).status()
        assert secret not in json.dumps(info, default=str)

    def test_error_reporting_never_sends_pii(self):
        """Opt-in Sentry reporting must not ship PII or raw exception text."""
        sentry_sdk = pytest.importorskip("sentry_sdk")
        from unittest import mock

        from popoto import _error_reporting

        captured = {}
        real_client = sentry_sdk.Client

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return real_client(*args, **kwargs)

        _error_reporting._client = None
        with mock.patch.object(sentry_sdk, "Client", side_effect=spy):
            _error_reporting.enable_error_reporting(dsn="https://key@sentry.test/1")
        assert captured.get("send_default_pii") is not True


# ---------------------------------------------------------------------------
# Consistency
# ---------------------------------------------------------------------------


class TestConsistency:
    def test_query_state_is_per_call_under_concurrency(self):
        """Two threads querying one model must not see each other's limits.

        ``Model.query`` is one object per class and ``filter()`` stores
        ordering, limit and pushdown state on it. This guard passes on main
        (6,000 interleaved queries across four threads produced no wrong
        shapes, because limit and order are re-applied from the call's own
        kwargs), so it is here to keep that true while the shared state is
        removed, not to demonstrate a defect.
        """

        class ContractRaceModel(Model):
            key = KeyField()
            score = SortedField(type=float)

        n = 30
        for i in range(n):
            ContractRaceModel.create(key=f"k{i}", score=float(i))

        errors = []
        barrier = threading.Barrier(2)
        iterations = 150

        def limited():
            barrier.wait()
            for _ in range(iterations):
                got = ContractRaceModel.query.filter(
                    score__gte=0.0, order_by="-score", limit=1
                )
                if len(got) != 1 or got[0].key != f"k{n - 1}":
                    errors.append(("limited", [g.key for g in got][:3]))
                    return

        def unlimited():
            barrier.wait()
            for _ in range(iterations):
                got = ContractRaceModel.query.filter(score__gte=0.0, order_by="score")
                if len(got) != n or got[0].key != "k0":
                    errors.append(("unlimited", len(got)))
                    return

        threads = [threading.Thread(target=limited), threading.Thread(target=unlimited)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert errors == [], f"interleaved queries returned wrong shapes: {errors[:3]}"

    def test_meta_ttl_expires_index_members_with_the_hash(self):
        """When a TTL row expires, its index entries must go with it.

        ``EXPIRE`` is applied to the hash only. Afterwards ``all()`` returns
        orphans and every query logs an error pointing at a repair method.
        """

        class ContractTTLModel(Model):
            key = KeyField()
            rank = SortedField(type=float)

            class Meta:
                ttl = 1

        ContractTTLModel.create(key="ephemeral", rank=1.0)
        time.sleep(1.6)
        assert ContractTTLModel.query.all() == []
        assert ContractTTLModel.query.filter(rank__gte=0.0) == []
        leftovers = [
            k
            for k in POPOTO_REDIS_DB.scan_iter(match="*ContractTTLModel*")
            if POPOTO_REDIS_DB.type(k) in (b"zset", b"set")
            and (
                POPOTO_REDIS_DB.zcard(k)
                if POPOTO_REDIS_DB.type(k) == b"zset"
                else POPOTO_REDIS_DB.scard(k)
            )
            > 0
        ]
        assert leftovers == [], f"index keys still hold expired members: {leftovers}"

    def test_default_memory_growth_is_bounded(self):
        """Memories per agent must be capped, by TTL or by count.

        Nothing on the default path evicts. One record per turn for the life
        of the install is not a memory system, it is a log. Contract: after
        writing 1,100 memories for one agent, either every hash carries a
        TTL or the partition holds at most 1,000.
        """
        cap = 1000
        agent = "contract-growth"
        _seed(agent, cap + 100, prefix="Fact")
        count = DefaultMemory.query.filter(agent_id=agent).count()
        if count > cap:
            sample = DefaultMemory.query.filter(agent_id=agent).all()[:5]
            ttls = [POPOTO_REDIS_DB.ttl(r.db_key.redis_key) for r in sample]
            assert all(t > 0 for t in ttls), (
                f"{count} memories for one agent, none expiring (ttl={ttls}); "
                "growth is unbounded"
            )


# ---------------------------------------------------------------------------
# Honesty
# ---------------------------------------------------------------------------


class TestHonesty:
    def test_methods_named_in_orphan_warnings_exist(self):
        """Every ``Model.<name>()`` an orphan-index message points at must exist."""
        import inspect

        from popoto.models import query as query_module

        source = inspect.getsource(query_module)
        referenced = set(re.findall(r"(?:Model|__name__\}|cls)\.([a-z_]+)\(\)", source))
        missing = sorted(
            name
            for name in referenced
            if name.endswith("_indexes") and not hasattr(Model, name)
        )
        assert missing == [], f"query.py messages point at undefined {missing}"

    def test_field_class_keys_are_unique(self):
        """Index namespaces derive from class names and must not collide.

        ``field_class_key`` is built with ``name.strip('Field')``, which
        strips a character set rather than a suffix (``FloatField`` maps to
        ``$oatF``). Any two field classes that fold to the same key share an
        index namespace on disk.
        """

        def subclasses(cls):
            for sub in cls.__subclasses__():
                yield sub
                yield from subclasses(sub)

        seen = {}
        collisions = []
        for cls in subclasses(FieldBase):
            key = str(getattr(cls, "field_class_key", ""))
            if not key:
                continue
            if key in seen and seen[key] is not cls:
                collisions.append((key, seen[key].__name__, cls.__name__))
            seen.setdefault(key, cls)
        assert collisions == [], collisions

        class ModelField(Field):
            pass

        class MoField(Field):
            pass

        assert str(ModelField.field_class_key) != str(MoField.field_class_key)
