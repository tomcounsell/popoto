"""MemoryService against real Redis, on the pytest plugin's isolated database.

No mocking of Popoto internals: the point of this layer is that hooks and
MCP tools reach the same keyspace through the same model, and only a real
round trip proves it.

The service is constructed with an explicit ``MemoryConfig`` whose
``url_is_explicit`` is ``False``, so :func:`bind_connection` is never invoked
and the suite stays on the database the pytest plugin selected.
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from popoto.integrations.config import MemoryConfig, derive_agent_id  # noqa: E402
from popoto.integrations.service import MemoryService  # noqa: E402
from popoto.recipes import DefaultMemory  # noqa: E402
from popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

AGENT = "test-integrations-service"


def make_service(tmp_path, **overrides) -> MemoryService:
    defaults = dict(
        agent_id=AGENT,
        log_path=tmp_path / "memory.log",
        max_items=5,
        max_tokens=800,
    )
    defaults.update(overrides)
    return MemoryService(MemoryConfig(**defaults))


@pytest.fixture(autouse=True)
def clean_agent():
    _purge()
    yield
    _purge()


def _purge():
    for record in DefaultMemory.query.filter(agent_id=AGENT):
        try:
            record.delete()
        except Exception:
            pass
    for pattern in (
        f"$popoto_memory:pending:{AGENT}:*",
        f"$popoto_memory:counter:{AGENT}:*",
        f"$popoto_memory:last:{AGENT}:*",
    ):
        for key in POPOTO_REDIS_DB.scan_iter(match=pattern, count=200):
            POPOTO_REDIS_DB.delete(key)


def seed(service, *contents):
    saved = []
    for content in contents:
        record = service.model(agent_id=AGENT, content=content, importance=0.8)
        record.save()
        saved.append(record)
    return saved


# --- model and provider wiring ---------------------------------------------


def test_uses_the_shipped_default_model(tmp_path):
    assert make_service(tmp_path).model is DefaultMemory


def test_default_write_path_is_raw(tmp_path):
    from popoto.extraction import RawTurnExtractionProvider

    assert isinstance(make_service(tmp_path).extractor, RawTurnExtractionProvider)


def test_heuristic_is_opt_in_only(tmp_path):
    from popoto.extraction import HeuristicExtractionProvider

    service = make_service(tmp_path, ingest="heuristic")
    assert isinstance(service.extractor, HeuristicExtractionProvider)


def test_recipe_never_falls_back_to_its_own_default(tmp_path):
    """The provider is passed explicitly, so the recipe default is unreachable."""
    from popoto.extraction import RawTurnExtractionProvider

    service = make_service(tmp_path)
    assert isinstance(service.memory._extractor, RawTurnExtractionProvider)


def test_every_subconscious_memory_construction_passes_a_provider():
    """Risk 1, checked at the source rather than at runtime.

    ``SubconsciousMemory(...)`` without ``extraction_provider`` resolves to
    ``HeuristicExtractionProvider``, the arm issue #489 measured worst. A
    hook fires on every turn, so that default reaching the harness path
    would generate more memories through the weakest write path than every
    other Popoto usage combined. This walks the package's AST and fails if
    any construction omits the keyword.
    """
    import ast
    import pathlib

    package = pathlib.Path(__file__).resolve().parent.parent / (
        "src/popoto/integrations"
    )
    constructions = 0
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "SubconsciousMemory":
                continue
            constructions += 1
            keywords = {kw.arg for kw in node.keywords}
            assert "extraction_provider" in keywords, f"{path.name}:{node.lineno}"
    assert constructions == 1, f"expected one construction, found {constructions}"


def test_retrieval_is_query_sensitive(tmp_path):
    assert make_service(tmp_path).memory._assembler._effective_mode == "lexical"


def test_benchmarked_score_weights_are_unchanged(tmp_path):
    assert make_service(tmp_path).memory.score_weights == {"relevance": 1.0}


# --- assemble ---------------------------------------------------------------


def test_assemble_returns_a_string_not_a_message_array(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green with automatic rollback")
    context = service.assemble("how do deploys roll back?", session_id="s1")
    assert isinstance(context, str)
    assert "blue-green" in context


def test_assemble_selects_by_query(tmp_path):
    service = make_service(tmp_path)
    seed(
        service,
        "Deploys are blue-green with automatic rollback",
        "Frontend bundles are built with esbuild",
    )
    context = service.assemble("tell me about esbuild bundles", session_id=None)
    assert "esbuild" in context


def test_assemble_on_empty_query_is_silent(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green")
    assert service.assemble("", session_id="s1") == ""
    assert service.assemble("   \n ", session_id="s1") == ""


def test_assemble_with_no_matching_records_returns_empty_string(tmp_path):
    service = make_service(tmp_path)
    assert service.assemble("nothing has ever been stored", session_id="s1") == ""


def test_assemble_respects_max_items(tmp_path):
    service = make_service(tmp_path, max_items=2)
    seed(service, *[f"deploy note number {i}" for i in range(6)])
    context = service.assemble("deploy note", session_id=None)
    assert 0 < len(context.strip().splitlines()) <= 2


def test_disabled_service_assembles_nothing(tmp_path):
    service = make_service(tmp_path, enabled=False)
    seed(service, "Deploys are blue-green")
    assert service.assemble("deploys", session_id="s1") == ""


# --- capture -----------------------------------------------------------------


def test_capture_writes_one_record_per_turn(tmp_path):
    service = make_service(tmp_path)
    turn = "Deploys are blue-green. Rollback is automatic. The pipeline reports it."
    keys = service.capture(turn, session_id="s1")
    assert len(keys) == 1
    stored = DefaultMemory.query.filter(agent_id=AGENT)
    assert len(stored) == 1
    assert stored[0].content == turn


def test_capture_under_heuristic_splits(tmp_path):
    service = make_service(tmp_path, ingest="heuristic")
    turn = "Deploys are blue-green. Rollback is automatic. The pipeline reports it."
    assert len(service.capture(turn, session_id="s1")) == 3


def test_capture_of_empty_text_writes_nothing(tmp_path):
    service = make_service(tmp_path)
    assert service.capture("", session_id="s1") == []
    assert service.capture("   \n\t", session_id="s1") == []
    assert len(DefaultMemory.query.filter(agent_id=AGENT)) == 0


def test_disabled_service_captures_nothing(tmp_path):
    service = make_service(tmp_path, enabled=False)
    assert service.capture("something worth keeping", session_id="s1") == []


def test_captured_turn_is_retrievable_next_turn(tmp_path):
    service = make_service(tmp_path)
    service.capture("The staging database resets nightly at 02:00 UTC", session_id="s1")
    assert "staging" in service.assemble("when does staging reset?", session_id=None)


# --- pending handoff and feedback ---------------------------------------------


def test_feedback_reports_against_the_turn_that_was_injected(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green with automatic rollback")
    service.assemble("how do deploys roll back?", session_id="s1")
    assert service.feedback("s1", outcome="acted") == 1


def test_feedback_without_a_pending_turn_is_a_no_op(tmp_path):
    assert make_service(tmp_path).feedback("never-assembled") == 0


def test_feedback_without_a_session_id_is_a_no_op(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green")
    service.assemble("deploys", session_id=None)
    assert service.feedback("", outcome="acted") == 0


def test_interleaved_turns_do_not_cross_report(tmp_path):
    """read(N) read(N+1) write(N) write(N+1): each write pops its own turn.

    The write hook runs async on Claude Code, so turn N's outcome report can
    land after turn N+1 has already assembled. A single pending slot per
    session would make turn N report against turn N+1's records.
    """
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green with automatic rollback")
    seed(service, "Frontend bundles are built with esbuild not webpack")

    service.assemble("how do deploys roll back?", session_id="s1")
    service.assemble("what bundler do we use?", session_id="s1")

    first = service._pop_pending("s1")
    second = service._pop_pending("s1")
    assert first and second
    assert first != second
    assert service._pop_pending("s1") == []


def test_pending_list_is_capped(tmp_path):
    from popoto.integrations.service import MAX_PENDING_TURNS

    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green with automatic rollback")
    for _ in range(MAX_PENDING_TURNS + 5):
        service.assemble("how do deploys roll back?", session_id="s1")
    length = POPOTO_REDIS_DB.llen(service._pending_key("s1"))
    assert length == MAX_PENDING_TURNS


def test_pending_key_has_a_ttl(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green with automatic rollback")
    service.assemble("how do deploys roll back?", session_id="s1")
    assert POPOTO_REDIS_DB.ttl(service._pending_key("s1")) > 0


# --- search and correct (the MCP half) -----------------------------------------


def test_search_returns_structured_records(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Rate limits are enforced in the gateway")
    results = service.search("where are rate limits enforced?")
    assert results
    assert "gateway" in results[0]["content"]
    assert results[0]["key"].startswith("DefaultMemory:")


def test_search_does_not_consume_a_pending_slot(tmp_path):
    """An explicit search is not a subconscious injection."""
    service = make_service(tmp_path)
    seed(service, "Rate limits are enforced in the gateway")
    service.search("rate limits")
    assert service.feedback("any-session") == 0


def test_search_with_empty_query_returns_nothing(tmp_path):
    assert make_service(tmp_path).search("  ") == []


def test_correct_applies_to_one_record(tmp_path):
    service = make_service(tmp_path)
    record = seed(service, "Rate limits are enforced in the gateway")[0]
    assert service.correct(record.db_key.redis_key, outcome="contradicted") is True


def test_correct_on_a_missing_key_is_false(tmp_path):
    assert make_service(tmp_path).correct("DefaultMemory:nope:nope") is False


# --- observability --------------------------------------------------------------


def test_status_reports_a_healthy_setup(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green")
    info = service.status()
    assert info["redis_reachable"] is True
    assert info["model"] == "DefaultMemory"
    assert info["retrieval_mode"] == "lexical"
    assert info["query_blind"] is False
    assert info["record_count"] == 1
    assert info["ingest"] == "raw"


class _DeadRedis:
    """A client whose every command raises, standing in for a stopped server."""

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise ConnectionError("Connection refused")

        return _raise


class _UnreachableService(MemoryService):
    @property
    def redis(self):
        return _DeadRedis()


def test_status_survives_an_unreachable_server(tmp_path):
    service = _UnreachableService(
        MemoryConfig(
            agent_id=AGENT,
            log_path=tmp_path / "memory.log",
            url="redis://127.0.0.1:6399/0",
        )
    )
    info = service.status()
    assert info["redis_reachable"] is False
    assert any("unreachable" in err for err in info["errors"])


def test_feedback_degrades_quietly_when_redis_is_down(tmp_path):
    """A dead server must not raise on the outcome path.

    Only the pending-handoff half of the service is exercised here, because
    the model's own client is the process-wide one and cannot be swapped per
    instance. The genuine server-down case is covered end to end, in a
    subprocess against a closed port, by
    ``tests/test_integrations_hooks.py::test_read_hook_with_redis_down``.
    """
    service = _UnreachableService(
        MemoryConfig(
            agent_id=AGENT,
            log_path=tmp_path / "memory.log",
            url="redis://127.0.0.1:6399/0",
        )
    )
    assert service.feedback("s1") == 0


def test_failures_are_logged_and_counted(tmp_path):
    service = make_service(tmp_path)
    service._record_failure("assemble", RuntimeError("redis went away"))
    assert service.config.log_path.exists()
    assert "redis went away" in service.config.log_path.read_text()
    assert service.status()["counters"].get("assemble", 0) >= 1


def test_successes_are_timestamped(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green")
    service.assemble("deploys", session_id=None)
    assert "assemble" in service.status()["last_success"]


def test_log_tail_is_bounded(tmp_path):
    service = make_service(tmp_path)
    for i in range(12):
        service._record_failure("assemble", RuntimeError(f"failure {i}"))
    tail = service.log_tail(lines=5)
    assert len(tail) == 5
    assert "failure 11" in tail[-1]


def test_log_tail_on_a_missing_file_is_empty(tmp_path):
    assert make_service(tmp_path, log_path=tmp_path / "absent.log").log_tail() == []


# --- config resolution -------------------------------------------------------


def test_agent_id_defaults_to_the_directory_basename():
    assert derive_agent_id("/Users/dev/src/my-project") == "my-project"
    assert derive_agent_id("/Users/dev/src/my-project/") == "my-project"


def test_config_reads_every_variable():
    config = MemoryConfig.from_env(
        {
            "POPOTO_MEMORY_URL": "redis://elsewhere:6380/3",
            "POPOTO_MEMORY_AGENT_ID": "explicit",
            "POPOTO_MEMORY_MAX_ITEMS": "9",
            "POPOTO_MEMORY_MAX_TOKENS": "1234",
            "POPOTO_MEMORY_INGEST": "heuristic",
            "POPOTO_MEMORY_ENABLED": "0",
            "POPOTO_MEMORY_LOG": "/tmp/popoto-test.log",
        }
    )
    assert config.url == "redis://elsewhere:6380/3"
    assert config.url_is_explicit is True
    assert config.agent_id == "explicit"
    assert config.max_items == 9
    assert config.max_tokens == 1234
    assert config.ingest == "heuristic"
    assert config.enabled is False
    assert str(config.log_path) == "/tmp/popoto-test.log"


def test_config_defaults_are_the_documented_ones():
    config = MemoryConfig.from_env({}, cwd="/Users/dev/src/demo")
    assert config.url == "redis://localhost:6379/0"
    assert config.url_is_explicit is False
    assert config.agent_id == "demo"
    assert config.max_items == 5
    assert config.max_tokens == 800
    assert config.ingest == "raw"
    assert config.enabled is True


def test_config_falls_back_to_redis_url():
    config = MemoryConfig.from_env({"REDIS_URL": "redis://from-redis-url:6379/1"})
    assert config.url == "redis://from-redis-url:6379/1"
    assert config.url_is_explicit is False


def test_bad_values_fall_back_rather_than_raising():
    config = MemoryConfig.from_env(
        {
            "POPOTO_MEMORY_MAX_ITEMS": "not-a-number",
            "POPOTO_MEMORY_MAX_TOKENS": "-4",
            "POPOTO_MEMORY_INGEST": "telepathy",
        }
    )
    assert config.max_items == 5
    assert config.max_tokens == 800
    assert config.ingest == "raw"


def test_kill_switch_accepts_the_usual_spellings():
    for value in ("0", "false", "no", "off", "FALSE"):
        assert MemoryConfig.from_env({"POPOTO_MEMORY_ENABLED": value}).enabled is False
    for value in ("1", "true", "yes", "on", ""):
        assert MemoryConfig.from_env({"POPOTO_MEMORY_ENABLED": value}).enabled is True
