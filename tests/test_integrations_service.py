"""MemoryService against real Redis, on the pytest plugin's isolated database.

No mocking of Popoto internals: the point of this layer is that hooks and
MCP tools reach the same keyspace through the same model, and only a real
round trip proves it.

The service is constructed with an explicit ``MemoryConfig`` whose
``url_is_explicit`` is ``False``, so :func:`bind_connection` is never invoked
and the suite stays on the database the pytest plugin selected.
"""

import json
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from popoto.integrations.config import MemoryConfig, derive_agent_id  # noqa: E402
from popoto.integrations.service import (  # noqa: E402
    COUNTER_KEY_PREFIX,
    MemoryService,
    _decode_pending_entry,
)
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


def key_of(record):
    return record.db_key.redis_key


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


def test_feedback_default_outcome_is_used_not_acted(tmp_path, monkeypatch):
    """A caller that omits ``outcome`` cannot have observed the memory
    influencing the response, so the default must not strengthen
    ConfidenceField or refresh any decay clock (see fields/observation.py).
    Pin the outcome service.feedback applies by default so an omitted
    argument can't silently regress to "acted".
    """
    from popoto.fields import observation as observation_module

    calls = []
    original = observation_module.ObservationProtocol.on_context_used

    def recording_on_context_used(instances, outcome_map, **kwargs):
        calls.append(dict(outcome_map))
        return original(instances, outcome_map, **kwargs)

    monkeypatch.setattr(
        observation_module.ObservationProtocol,
        "on_context_used",
        staticmethod(recording_on_context_used),
    )

    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green with automatic rollback")
    service.assemble("how do deploys roll back?", session_id="s1")
    assert service.feedback("s1") == 1
    assert calls
    assert set(calls[0].values()) == {"used"}


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

    ``turn_id=None`` throughout: this is the positional fallback a harness
    that sends no turn identifier still relies on. The resolved keys are
    asserted exactly, because ``first != second`` alone passes under the very
    off-by-one misattribution #574 removes.
    """
    service = make_service(tmp_path)
    deploys = seed(service, "Deploys are blue-green with automatic rollback")[0]
    bundler = seed(service, "Frontend bundles are built with esbuild not webpack")[0]

    service.assemble("how do deploys roll back?", session_id="s1", turn_id=None)
    service.assemble("what bundler do we use?", session_id="s1", turn_id=None)

    first = service._pop_pending("s1", turn_id=None)
    second = service._pop_pending("s1", turn_id=None)
    assert first == [key_of(deploys)]
    assert second == [key_of(bundler)]
    assert service._pop_pending("s1", turn_id=None) == []


def test_pending_list_is_capped(tmp_path):
    from popoto.integrations.service import MAX_PENDING_TURNS

    service = make_service(tmp_path)
    # One turn pushes one pending entry, and only turns that actually inject
    # push at all. Per-session suppression means a record is injected once, so
    # driving N turns needs N * max_items distinct records -- seeding a single
    # memory would inject on turn 1 and go quiet, leaving a list of length 1.
    turns = MAX_PENDING_TURNS + 5
    seed(
        service,
        *[f"Deploys roll back automatically, note {i}" for i in range(turns * 5)],
    )
    for i in range(turns):
        service.assemble("how do deploys roll back?", session_id="s1", turn_id=f"t{i}")
    length = POPOTO_REDIS_DB.llen(service._pending_key("s1"))
    assert length == MAX_PENDING_TURNS
    # The cap trims the oldest tagged entries, not the newest: the turns that
    # survive must be the most recent ones, still claimable by name.
    staged = [
        _decode_pending_entry(raw)[1]
        for raw in POPOTO_REDIS_DB.lrange(service._pending_key("s1"), 0, -1)
    ]
    assert staged == [f"t{i}" for i in range(turns - MAX_PENDING_TURNS, turns)]


def test_pending_key_has_a_ttl(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green with automatic rollback")
    service.assemble("how do deploys roll back?", session_id="s1", turn_id="t1")
    assert POPOTO_REDIS_DB.ttl(service._pending_key("s1")) > 0
    # The TTL is set by the same pipeline that writes the tagged payload, so
    # assert the entry really took the turn-keyed shape and not the legacy one.
    raw = POPOTO_REDIS_DB.lrange(service._pending_key("s1"), 0, -1)[0]
    assert _decode_pending_entry(raw)[:2] == (True, "t1")


# --- turn-keyed handoff (#574) -------------------------------------------------


def test_feedback_resolves_the_turn_that_staged_it(tmp_path):
    """Two turns staged in order, reported out of order, each claims its own.

    The pairing is by name, so the report order is free. Under the positional
    handoff the later report would have taken the head of the queue and
    applied its outcome to the other turn's records.
    """
    service = make_service(tmp_path)
    first, second = seed(service, "Deploys are blue-green", "Bundles use esbuild")

    service._push_pending("s1", [first], turn_id="t1")
    service._push_pending("s1", [second], turn_id="t2")

    assert service._pop_pending("s1", turn_id="t2") == [key_of(second)]
    assert service._pop_pending("s1", turn_id="t1") == [key_of(first)]
    assert POPOTO_REDIS_DB.llen(service._pending_key("s1")) == 0


def test_interleaved_turns_from_two_sessions_do_not_cross(tmp_path):
    """Two concurrent sessions, four turns, reported in a scrambled order.

    Sessions already have their own pending list, so this pins the property
    that turn keying does not weaken it: no report ever resolves a key that
    belongs to the other session, whatever order the write hooks land in.
    """
    service = make_service(tmp_path)
    a1, a2, b1, b2 = seed(
        service,
        "Session A turn one: deploys are blue-green",
        "Session A turn two: bundles use esbuild",
        "Session B turn one: rate limits live in the gateway",
        "Session B turn two: staging resets nightly",
    )

    service._push_pending("sA", [a1], turn_id="t1")
    service._push_pending("sB", [b1], turn_id="t1")
    service._push_pending("sA", [a2], turn_id="t2")
    service._push_pending("sB", [b2], turn_id="t2")

    resolved = [
        ("sB", service._pop_pending("sB", turn_id="t2")),
        ("sA", service._pop_pending("sA", turn_id="t1")),
        ("sB", service._pop_pending("sB", turn_id="t1")),
        ("sA", service._pop_pending("sA", turn_id="t2")),
    ]

    assert [keys for _session, keys in resolved] == [
        [key_of(b2)],
        [key_of(a1)],
        [key_of(b1)],
        [key_of(a2)],
    ]
    a_keys = {key_of(a1), key_of(a2)}
    b_keys = {key_of(b1), key_of(b2)}
    for session, keys in resolved:
        foreign = b_keys if session == "sA" else a_keys
        assert not foreign.intersection(keys)
    assert POPOTO_REDIS_DB.llen(service._pending_key("sA")) == 0
    assert POPOTO_REDIS_DB.llen(service._pending_key("sB")) == 0


def test_aborted_turn_does_not_shift_later_pairings(tmp_path):
    """A turn whose write hook never fires must not shift the next one.

    This is the #574 failure in its simplest form: with positional pairing,
    turn 2's report pops turn 1's abandoned entry and every later pairing is
    off by one for the life of the session.
    """
    service = make_service(tmp_path)
    aborted, reported = seed(service, "Deploys are blue-green", "Bundles use esbuild")

    service._push_pending("s1", [aborted], turn_id="t1")
    service._push_pending("s1", [reported], turn_id="t2")

    assert service._pop_pending("s1", turn_id="t2") == [key_of(reported)]

    remaining = POPOTO_REDIS_DB.lrange(service._pending_key("s1"), 0, -1)
    assert len(remaining) == 1
    assert _decode_pending_entry(remaining[0]) == (True, "t1", [key_of(aborted)])


def test_subagent_stop_resolves_the_parent_turn_once(tmp_path):
    """A session configured to fire its write event twice reports once.

    ``SubagentStop`` plus ``Stop`` pops more than the read path pushed. The
    second report for the same turn finds nothing left tagged with it and
    resolves nothing, instead of consuming the next turn's entry.
    """
    service = make_service(tmp_path)
    parent, later = seed(service, "Deploys are blue-green", "Bundles use esbuild")

    service._push_pending("s1", [parent], turn_id="t1")
    service._push_pending("s1", [later], turn_id="t2")

    assert service.feedback("s1", outcome="acted", turn_id="t1") == 1
    # The duplicate must resolve nothing even with t2 already queued behind
    # it -- under positional pairing this second report would have consumed
    # t2's entry and returned 1.
    assert service.feedback("s1", outcome="acted", turn_id="t1") == 0
    assert service.feedback("s1", outcome="acted", turn_id="t2") == 1


def test_untagged_harness_keeps_fifo_order(tmp_path):
    """Hermes and OpenClaw send no turn id and keep the positional pairing.

    The returned keys are asserted, not the count: the decode step must reach
    into the payload, and a fallback that parsed a tagged entry as a bare
    array would hand back ``["t", "k"]`` -- two strings, in order, resolving
    nothing -- which any count-only or ordering-only assertion would pass.
    """
    service = make_service(tmp_path)
    first, second = seed(service, "Deploys are blue-green", "Bundles use esbuild")

    service._push_pending("s1", [first], turn_id=None)
    service._push_pending("s1", [second], turn_id=None)

    assert service._pop_pending("s1", turn_id=None) == [key_of(first)]
    assert service._pop_pending("s1", turn_id=None) == [key_of(second)]
    assert service._pop_pending("s1", turn_id=None) == []


def test_legacy_entries_are_claimed_after_an_upgrade(tmp_path):
    """An entry staged before the upgrade is still claimable afterwards.

    A session live across the version boundary has bare arrays on its list
    and a turn id arriving on the write hook. Positional pairing is the only
    pairing those entries ever had, so a list with no tags at all falls back
    to it rather than stranding the outcome.
    """
    service = make_service(tmp_path)
    record = seed(service, "Deploys are blue-green")[0]
    POPOTO_REDIS_DB.rpush(service._pending_key("s1"), json.dumps([key_of(record)]))

    assert service._pop_pending("s1", turn_id="t1") == [key_of(record)]
    assert POPOTO_REDIS_DB.llen(service._pending_key("s1")) == 0


def test_upgrade_fallback_stops_once_a_tagged_entry_exists(tmp_path):
    """The upgrade fallback is bounded by the absence of any tag.

    As soon as one tagged entry is on the list the session is post-upgrade,
    and an unrecognized turn id is a miss rather than a licence to pop the
    head -- reporting against whatever sits there is the misattribution this
    change removes. Both elements stay put.
    """
    service = make_service(tmp_path)
    legacy, tagged = seed(service, "Deploys are blue-green", "Bundles use esbuild")
    redis_key = service._pending_key("s1")
    POPOTO_REDIS_DB.rpush(redis_key, json.dumps([key_of(legacy)]))
    service._push_pending("s1", [tagged], turn_id="t1")

    assert service._pop_pending("s1", turn_id="unknown-turn") == []

    remaining = POPOTO_REDIS_DB.lrange(redis_key, 0, -1)
    assert [_decode_pending_entry(raw) for raw in remaining] == [
        (False, None, [key_of(legacy)]),
        (True, "t1", [key_of(tagged)]),
    ]
    assert service.status()["counters"].get("pending_miss", 0) == 1


def test_turn_keyed_kill_switch_restores_fifo(tmp_path):
    """``POPOTO_MEMORY_TURN_KEYED=0`` restores the pre-#574 handoff whole.

    Not just the claim half: entries go back to the bare-array shape, so a
    deployment that flips the switch mid-session leaves nothing tagged behind
    for a later reader to misread.
    """
    assert MemoryConfig.from_env({"POPOTO_MEMORY_TURN_KEYED": "0"}).turn_keyed is False

    service = make_service(tmp_path, turn_keyed=False)
    first, second = seed(service, "Deploys are blue-green", "Bundles use esbuild")

    service._push_pending("s1", [first], turn_id="t1")
    service._push_pending("s1", [second], turn_id="t2")

    staged = POPOTO_REDIS_DB.lrange(service._pending_key("s1"), 0, -1)
    assert [_decode_pending_entry(raw) for raw in staged] == [
        (False, None, [key_of(first)]),
        (False, None, [key_of(second)]),
    ]
    # Positional even though a turn id is supplied, and t2's report takes the
    # head of the queue: exactly the behavior the switch exists to restore.
    assert service._pop_pending("s1", turn_id="t2") == [key_of(first)]
    assert service._pop_pending("s1", turn_id="t1") == [key_of(second)]


def test_corrupt_pending_entry_is_logged_and_skipped(tmp_path):
    """A poisoned element costs one turn's outcome report, not the turn.

    Nothing raises out of either claim path, and the garbage does not block
    the tagged entry sitting behind it.
    """
    service = make_service(tmp_path)
    record = seed(service, "Deploys are blue-green")[0]
    redis_key = service._pending_key("s1")
    POPOTO_REDIS_DB.rpush(redis_key, "{not json at all")
    service._push_pending("s1", [record], turn_id="t1")

    assert service._pop_pending("s1", turn_id="t1") == [key_of(record)]

    # And the same garbage on the positional path is a quiet no-op.
    assert service.feedback("s1", outcome="acted", turn_id=None) == 0
    assert POPOTO_REDIS_DB.llen(redis_key) == 0


def test_duplicate_push_for_same_turn_stages_one_claimable_entry(tmp_path):
    """A redelivered read hook must not stage a second, unclaimable entry.

    The dedupe check is advisory rather than atomic, but it covers the case
    it exists for: one turn, staged twice, leaves exactly one entry, and only
    the first report resolves anything.
    """
    service = make_service(tmp_path)
    record = seed(service, "Deploys are blue-green")[0]

    service._push_pending("s1", [record], turn_id="t1")
    service._push_pending("s1", [record], turn_id="t1")

    assert POPOTO_REDIS_DB.llen(service._pending_key("s1")) == 1
    assert service.feedback("s1", outcome="acted", turn_id="t1") == 1
    assert service.feedback("s1", outcome="acted", turn_id="t1") == 0


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
    # NOTE: this URL is decorative, not the unreachability mechanism -- unlike
    # tests/test_integrations_hooks.py (which relies on a genuinely closed
    # port), _UnreachableService overrides `.redis` with a `_DeadRedis` stub
    # above, so no connection to this address is ever attempted.
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


@pytest.mark.parametrize("turn_id", [None, "t1"])
def test_feedback_degrades_quietly_when_redis_is_down(tmp_path, turn_id):
    """A dead server must not raise on the outcome path.

    Both claim paths are exercised: the positional ``LPOP`` fallback and the
    turn-keyed ``LRANGE``/``LREM`` claim, which touches Redis one command
    earlier and would otherwise be an uncovered way to raise into a user turn.

    Only the pending-handoff half of the service is exercised here, because
    the model's own client is the process-wide one and cannot be swapped per
    instance. The genuine server-down case is covered end to end, in a
    subprocess against a closed port, by
    ``tests/test_integrations_hooks.py::test_read_hook_with_redis_down``.
    """
    # NOTE: decorative URL, same as above -- _UnreachableService's `.redis`
    # override (a `_DeadRedis` stub) is what makes every call fail here, not
    # this port being closed.
    service = _UnreachableService(
        MemoryConfig(
            agent_id=AGENT,
            log_path=tmp_path / "memory.log",
            url="redis://127.0.0.1:6399/0",
        )
    )
    assert service.feedback("s1", turn_id=turn_id) == 0


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


# --- never-record firewall: a privacy drop is not an outage (#561) ----------


def test_privacy_drop_is_not_recorded_as_a_capture_failure(tmp_path):
    """A deliberate drop must stay out of the failure counter and the log.

    ``capture()`` treats an empty result from non-empty text as evidence the
    write path has silently stopped working. The never-record firewall makes
    empty results legitimate, so without a guard every credential paste and
    every off-the-record turn would look like an outage — noise proportional
    to how well the firewall works.
    """
    service = make_service(tmp_path)
    counter_key = f"{COUNTER_KEY_PREFIX}:{AGENT}:capture"
    POPOTO_REDIS_DB.delete(counter_key)

    keys = service.capture("Off the record: the prod password rotates on Fridays.")

    assert keys == []
    assert POPOTO_REDIS_DB.get(counter_key) is None
    assert not (tmp_path / "memory.log").exists()
    assert DefaultMemory.never_record_counts().get("off_the_record") == 1


def test_credential_paste_through_capture_is_dropped_silently(tmp_path):
    service = make_service(tmp_path)
    counter_key = f"{COUNTER_KEY_PREFIX}:{AGENT}:capture"
    POPOTO_REDIS_DB.delete(counter_key)

    secret = "sk-ant-api03-ZZQQWWEERRTTYYUUIIOOPPAASSDDFFGG"
    assert service.capture(f"my api key is {secret}") == []
    assert POPOTO_REDIS_DB.get(counter_key) is None
    assert not (tmp_path / "memory.log").exists()


def test_genuine_empty_result_still_records_a_failure(tmp_path):
    """The outage alarm must survive: only privacy drops are exempted."""

    class EmptyProvider:
        def extract(self, text):
            return []

    service = make_service(tmp_path)
    service.memory._extractor = EmptyProvider()
    counter_key = f"{COUNTER_KEY_PREFIX}:{AGENT}:capture"
    POPOTO_REDIS_DB.delete(counter_key)

    assert service.capture("an ordinary turn with no secrets in it") == []
    assert POPOTO_REDIS_DB.get(counter_key) is not None
    assert (tmp_path / "memory.log").exists()
