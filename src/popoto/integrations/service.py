"""MemoryService -- the harness-agnostic core shared by hooks and MCP tools.

One object with four operations: :meth:`~MemoryService.assemble`,
:meth:`~MemoryService.capture`, :meth:`~MemoryService.feedback`, and
:meth:`~MemoryService.status`. Every harness adapter, and every MCP tool,
goes through it, so there is exactly one code path to Redis and exactly one
schema (:class:`popoto.recipes.DefaultMemory`).

Two contracts this module keeps deliberately:

**It returns a context string; it never touches a message array.** On the
hook path the harness places the returned string in the *user* turn (Claude
Code and Codex ``additionalContext``, Hermes ``context``, OpenClaw
``appendContext``), which appends after all sealed history and so preserves
the cached prefix. Message placement is the harness's decision, not this
module's. The library recipe's ``inject_context()`` appends at the tail for
the same reason; it can still be asked for the old ``position="system"``
behavior, which invalidates a cached system prefix on every turn.

**It suppresses what it already injected.** Selected keys go into a
per-session set (:data:`INJECTED_KEY_PREFIX`) and come back as
``assemble(exclude_keys=...)``. Injected blocks stay resident for the life of
a session, so re-retrieving the same top-k every turn -- which topically
similar consecutive prompts produce -- makes cumulative cache-read grow with
the square of turn count. Declining to re-add is append-only and therefore
free; pruning an already-sent block would cost every token behind it.

**It writes through** :class:`~popoto.extraction.RawTurnExtractionProvider`
**by default.** See :attr:`MemoryService.extractor` and issue #489.

Failures are swallowed -- a memory error must never break a user's turn --
but never silently. Each swallowed exception appends a line to the
configured log file and increments a counter that ``popoto-memory doctor``
reads back.

**It runs on whichever backend** :class:`~popoto.recipes.DefaultMemory`
**is bound to** (#814). On Redis the session state -- the pending-turn list,
the injected-key set, the counters and the last-success timestamps -- is the
raw keys named by the ``*_KEY_PREFIX`` constants below, exactly as before. On
Postgres (``POPOTO_BACKEND=postgres``) the same state is typed engine tables
reached through the backend's ``_harness`` and ``_counter`` adapters
(``popoto/backends/postgres/recipes.py``), and the process sends Redis no
command at all: no connection is bound, the database-0 guard (a Redis rule)
does not apply, and ``status()`` probes the Postgres server instead.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..backends.types import OUTAGE_ERRORS
from .config import redact_url, PENDING_TTL_SECONDS, MemoryConfig, bind_connection

logger = logging.getLogger("POPOTO.integrations")

COUNTER_KEY_PREFIX = "$popoto_memory:counter"
"""Redis key prefix for failure and activity counters. ``INCR`` is atomic,
so ``doctor`` can read these while a hook writes them."""

NON_FAILURE_COUNTERS = frozenset({"evicted", "heuristic_notice"})
"""Counter names that are *reports*, not integration errors.

Both renderers bucket every counter that does not end in ``_ok`` under
"failures"; these two are neither successes nor failures. ``evicted`` is the
``DefaultMemory`` data-loss report (#596) and ``heuristic_notice`` is the
one-time ingest-mode marker set by :meth:`MemoryService._warn_heuristic_cost`.
Subtract this set from any failure bucket rather than re-spelling the names.
"""

PENDING_KEY_PREFIX = "$popoto_memory:pending"
"""Redis key prefix for the read-hook-to-write-hook handoff."""

INJECTED_KEY_PREFIX = "$popoto_memory:injected"
"""Redis key prefix for the per-session set of already-injected record keys,
read back as ``assemble(exclude_keys=...)`` so a memory surfaced once is not
re-injected every turn. Separate from the pending FIFO, which ``feedback``
consumes one turn at a time; suppression needs the session-wide union."""

LAST_EVENT_KEY_PREFIX = "$popoto_memory:last"
"""Redis key prefix for last-success timestamps, so a silently broken
injection is visible in ``doctor`` without reading the log."""

HARNESS_FIELD = "_harness"
"""The Postgres backend's ``field_call`` adapter for the session state above
(``popoto.backends.postgres.recipes.HARNESS_FIELD``). Spelled here rather than
imported so the hook's import path never loads the Postgres backend package
on a Redis-bound process."""

COUNTER_FIELD = "_counter"
"""The backend counter adapter the Postgres leg keeps its counters in, under
the same key strings as the Redis counters, so the ``DefaultMemory``
eviction counter (#596) is read back with them on both backends."""

MAX_PENDING_TURNS = 32
"""Cap on queued unresolved turns per session. A harness that reads without
ever firing its write event (or a crashed session) would otherwise grow the
handoff list without bound."""


def _decode_pending_entry(
    raw: Any,
    on_corrupt: Optional[Callable[[BaseException], None]] = None,
) -> Tuple[bool, Optional[str], List[str]]:
    """Decode one pending-list element into ``(tagged, turn_id, keys)``.

    The single decode step both branches of
    :meth:`MemoryService._pop_pending` go through, so the ``LPOP`` fallback
    reads turn-tagged entries as happily as the claiming path reads them.
    Keeping two parsers here is the defect that would silently zero outcome
    reporting for harnesses that send no turn id: they take the fallback
    branch, and every entry they meet was written in the tagged shape.

    Three shapes are accepted. ``{"t": ..., "k": [...]}`` is the turn-keyed
    entry and decodes ``tagged=True``. A bare ``[...]`` array is a legacy or
    keying-disabled entry and decodes ``tagged=False`` -- ``tagged``
    distinguishes an entry with *no* ``t`` key from one whose ``t`` is
    merely null, which is what bounds the upgrade-in-flight fallback.
    Anything else is corrupt and decodes to no keys rather than raising: a
    poisoned entry must cost one turn's outcome report, not the turn. It is
    still reported -- ``on_corrupt`` receives the decode error so the caller
    can log and count it, because an entry silently decoding to nothing and
    an entry legitimately holding no keys are otherwise indistinguishable in
    the log, and only one of them is a bug.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        elem = json.loads(raw)
    except Exception as exc:
        if on_corrupt is not None:
            on_corrupt(exc)
        return False, None, []
    if isinstance(elem, dict):
        tagged = "t" in elem
        turn = elem.get("t")
        keys = elem.get("k")
    elif isinstance(elem, list):
        tagged, turn, keys = False, None, elem
    else:
        if on_corrupt is not None:
            on_corrupt(TypeError(f"pending entry is a {type(elem).__name__}"))
        return False, None, []
    if not isinstance(turn, str):
        turn = None
    if not isinstance(keys, list):
        return tagged, turn, []
    return tagged, turn, [k for k in keys if isinstance(k, str)]


class MemoryService:
    """Shared memory core over :class:`popoto.recipes.DefaultMemory`.

    Args:
        config: Resolved :class:`~popoto.integrations.config.MemoryConfig`.
            Defaults to :meth:`MemoryConfig.from_env`.

    Attributes:
        config: The configuration in force.

    Example::

        from popoto.integrations import MemoryService

        service = MemoryService()
        context = service.assemble("how do we deploy?", session_id="s1")
        service.capture("Deploys are blue-green with auto rollback", "s1")
    """

    def __init__(self, config: Optional[MemoryConfig] = None):
        self.config = config or MemoryConfig.from_env()
        self._memory: Any = None
        self._model: Any = None
        # Set by _record_failure on the first connection/timeout error.
        # Every later Redis-touching operation in this process is skipped:
        # a hook that already waited on a dead server once must not wait
        # four more times before the user's prompt goes through.
        self._redis_down: bool = False
        # The single place the connection is bound. Every entry point --
        # the hook, the MCP server, doctor, demo, the examples, the Hermes
        # plugin -- reaches Redis through a MemoryService, so binding here
        # is what makes POPOTO_MEMORY_URL mean the same thing on all of
        # them. Binding in the CLI instead left demo, seed.py, verify.py and
        # the Hermes plugin writing to database 0 while printing the URL
        # they were not using.
        #
        # Safe for in-process callers: bind_connection is a no-op unless
        # POPOTO_MEMORY_URL was set explicitly, so a test under the pytest
        # plugin, or a host application that configured its own connection,
        # keeps the one it chose.
        #
        # Redis only (#814). On another backend there is no Redis connection
        # to bind and no database 0 to refuse: binding would be the one Redis
        # call a Postgres-bound hook makes. A disabled service skips the
        # backend lookup too, so the kill switch works under any
        # misconfiguration of POPOTO_BACKEND.
        if not self.config.enabled or self.backend_name == "redis":
            bind_connection(self.config)

    # -- lazy wiring ----------------------------------------------------

    @property
    def model(self) -> Any:
        """The :class:`popoto.recipes.DefaultMemory` class.

        Imported on first access. This package defines no model of its own:
        a second memory schema would compete with the shipped default
        permanently, because it would be embedded in every installed
        harness config.
        """
        if self._model is None:
            from ..recipes import DefaultMemory

            self._model = DefaultMemory
        return self._model

    @property
    def extractor(self) -> Any:
        """The extraction provider selected by ``POPOTO_MEMORY_INGEST``.

        ``raw`` (the default) is
        :class:`~popoto.extraction.RawTurnExtractionProvider`: one verbatim
        record per turn. ``heuristic`` is the sentence-splitting provider,
        which issue #489 measured at 0.2078 judged accuracy against raw's
        0.3636 on the same slice. Selecting it logs that cost once.
        """
        from ..extraction import HeuristicExtractionProvider, RawTurnExtractionProvider

        if self.config.ingest == "heuristic":
            self._warn_heuristic_cost()
            return HeuristicExtractionProvider()
        return RawTurnExtractionProvider()

    @property
    def memory(self) -> Any:
        """The :class:`popoto.recipes.SubconsciousMemory` instance.

        Built lazily with this service's agent id, budgets, and extraction
        provider. The provider is always passed explicitly, so the recipe's
        heuristic default is never reached by accident.
        """
        if self._memory is None:
            from ..recipes import SubconsciousMemory

            self._memory = SubconsciousMemory(
                agent_id=self.config.agent_id,
                max_items=self.config.max_items,
                max_tokens=self.config.max_tokens,
                extraction_provider=self.extractor,
            )
        return self._memory

    @property
    def redis(self) -> Any:
        """Popoto's shared Redis or Valkey client.

        Used only when :attr:`backend_name` is ``"redis"``; nothing on the
        Postgres path reads it.
        """
        from ..redis_db import POPOTO_REDIS_DB

        return POPOTO_REDIS_DB

    @property
    def backend_name(self) -> str:
        """The name of the backend the memory model is bound to.

        ``DefaultMemory``'s ``Meta.backend`` when it names one, else the
        process default (:func:`popoto.backends.default_backend_name`:
        ``set_backend()``, else ``POPOTO_BACKEND``, else ``"redis"``). Resolved
        without constructing or connecting to the backend, so it answers even
        when the backend is unreachable or misconfigured; an unknown
        ``POPOTO_BACKEND`` raises :class:`ValueError`, which every entry point
        already reports as misconfiguration.
        """
        explicit = getattr(getattr(self.model, "_meta", None), "backend", None)
        if explicit:
            return str(explicit)
        from ..backends import default_backend_name

        return default_backend_name()

    def _backend_label(self) -> str:
        """:attr:`backend_name` for a message: never raises."""
        try:
            return self.backend_name
        except Exception:
            return os.environ.get("POPOTO_BACKEND", "").strip() or "unknown"

    def _store(self) -> Any:
        """The model's backend when it is not Redis, else ``None``.

        ``None`` selects the raw-key Redis code below, which is unchanged
        byte for byte; anything else is a backend whose ``field_call``
        adapters hold the session state. Resolving it issues no Redis command
        and dials nothing (``bind()`` only compiles the table spec); a
        Postgres backend selected without ``POPOTO_POSTGRES_URL`` raises
        :class:`~popoto.backends.BackendUnavailableError` here, which each
        caller records as that operation's failure.
        """
        from ..backends.routing import non_redis_backend

        return non_redis_backend(self.model)

    def _state(self, store: Any, field: str, op: str, *args: Any) -> Any:
        """One ``field_call`` on a non-Redis backend's state adapters."""
        return store.field_call(self.model._meta.spec, field, op, *args)

    def ping(self) -> None:
        """Round-trip to the bound backend; raises when it is unreachable.

        ``PING`` on Redis, :meth:`PostgresBackend.describe` on Postgres. Used
        by ``popoto-memory demo`` before it seeds anything.
        """
        store = self._store()
        if store is None:
            self.redis.ping()
            return
        describe = getattr(store, "describe", None)
        if describe is not None:
            describe()

    # -- public operations ----------------------------------------------

    def assemble(
        self,
        query: str,
        session_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> str:
        """Read path: retrieve memories relevant to ``query``.

        Assembles through :class:`~popoto.recipes.ContextAssembler` on the
        lexical/BM25 path, then records the selected record keys as a
        pending turn so a later :meth:`feedback` call can report outcomes
        against exactly those records.

        Args:
            query: The user's prompt text, used as the ``topic`` query cue.
                Empty or whitespace-only input skips retrieval entirely.
            session_id: Harness session identifier. Used only to scope the
                pending-turn handoff; ``None`` disables outcome reporting
                for this turn rather than raising.
            turn_id: Harness turn identifier, when the payload carried one.
                Tags the pending entry so :meth:`feedback` claims this
                turn's records by name instead of by queue position. A
                harness that sends none keeps the positional pairing.

        Returns:
            The formatted context block, or ``""`` when memory is disabled,
            the query is empty, nothing matched, or retrieval failed. An
            empty string means the caller must emit no context key at all:
            a bare "Relevant context:" header with nothing under it is
            worse than silence.
        """
        if not self.config.enabled or not query or not query.strip():
            return ""
        if self._redis_down:
            return ""

        exclude_keys = self._injected_keys(session_id)
        if self._redis_down:
            return ""
        try:
            result = self.memory.assembler.assemble(
                query_cues={"topic": query.strip()},
                agent_id=self.config.agent_id,
                exclude_keys=exclude_keys,
            )
        except Exception as exc:
            self._record_failure("assemble", exc)
            return ""

        # Unconditionally, including the empty-result case. The assembler
        # swallows its own connection errors and returns an empty result, so
        # "nothing retrieved" and "the server is gone" look identical from
        # here. This write is the probe that separates them: it costs one
        # pipelined round trip that the read path was going to make anyway,
        # and when it fails the reason lands in the log and the counters.
        self._touch("assemble")

        if not result.records or not result.formatted.strip():
            return ""

        if session_id:
            self._push_pending(session_id, result.records, turn_id=turn_id)
            self._mark_injected(session_id, result.records)

        return result.formatted

    def capture(
        self,
        text: str,
        session_id: Optional[str] = None,
        importance: float = 0.5,
    ) -> List[str]:
        """Write path: save an assistant turn as memory.

        Args:
            text: The assistant's final message for the turn. Empty or
                whitespace-only input writes nothing.
            session_id: Harness session identifier, recorded for symmetry
                with :meth:`assemble`; capture itself does not need it.
            importance: Base importance for the written record, feeding the
                decay score. Default ``0.5``.

        Returns:
            The Redis keys of the written records. One key per turn on the
            default ``raw`` ingest mode; empty on failure or when memory is
            disabled.
        """
        if not self.config.enabled or not text or not text.strip():
            return []

        try:
            saved = self.memory.extract_memories(text, importance=importance)
        except Exception as exc:
            self._record_failure("capture", exc)
            return []

        keys = []
        for instance in saved:
            try:
                keys.append(instance.db_key.redis_key)
            except Exception:
                continue

        if keys:
            self._touch("capture")
        elif getattr(self.memory, "last_extraction_privacy_dropped", False):
            # The never-record firewall dropped this turn on purpose (#561).
            # Nothing failed, so this must stay out of the failure counter and
            # out of the plaintext failure log -- otherwise every credential
            # paste and every off-the-record turn would look like the write
            # path had silently stopped working, and the noise would scale
            # with how well the firewall works. The drop is already counted in
            # `$NR:{ClassName}:counts`, which is its correct counter.
            pass
        else:
            # Non-empty text otherwise yields at least one fact, so reaching
            # here means the save was rejected or the server is gone. The
            # recipe swallows that internally; record it so `doctor` can
            # show a write path that has silently stopped working.
            self._record_failure(
                "capture", RuntimeError("no record written for a non-empty turn")
            )
        return keys

    def feedback(
        self,
        session_id: str,
        outcome: str = "used",
        turn_id: Optional[str] = None,
    ) -> int:
        """Report how the memories injected for a turn were used.

        Claims one unresolved turn for ``session_id`` and applies
        ``outcome`` to its records through
        :class:`~popoto.fields.observation.ObservationProtocol`, which is
        what drives the confidence and decay loop.

        Given ``turn_id``, claims the entry :meth:`assemble` staged for that
        same turn; otherwise pops the oldest. A missing pending turn
        degrades to a no-op, and a turn id that matches nothing reports
        against nothing rather than falling back to the head of the queue.
        Either way this method consumes exactly one entry.

        Args:
            session_id: Harness session identifier.
            turn_id: Harness turn identifier, when the payload carried
                one. Must be the same value the paired :meth:`assemble` call
                received; a value that matches no staged entry resolves
                nothing.
            outcome: One of ``"acted"``, ``"used"``, ``"dismissed"``,
                ``"deferred"``, ``"contradicted"``. Default ``"used"``: a
                caller that omits the outcome cannot have observed the
                memory influencing the response, so the safe default must
                not strengthen confidence or refresh decay clocks.

        Returns:
            Number of records the outcome was applied to.
        """
        if not self.config.enabled or not session_id:
            return 0

        keys = self._pop_pending(session_id, turn_id=turn_id)
        if not keys:
            return 0

        try:
            from ..fields.observation import ObservationProtocol

            records = self.model.query.get_many(keys, skip_none=True)
            records = [r for r in records if r is not None]
            if not records:
                return 0
            outcome_map = {r.db_key.redis_key: outcome for r in records}
            ObservationProtocol.on_context_used(records, outcome_map)
            return len(records)
        except Exception as exc:
            self._record_failure("feedback", exc)
            return 0

    def search(self, query: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Discretionary search, backing the ``memory_search`` MCP tool.

        Unlike :meth:`assemble` this returns structured records rather than
        an injection block, and it does not create a pending turn: an
        explicit search is not a subconscious injection and must not consume
        the outcome-reporting slot of one.

        Args:
            query: Search text.
            limit: Maximum records. Defaults to the configured
                ``max_items``.

        Returns:
            A list of ``{"key", "content", "importance"}`` dicts, most
            relevant first. Empty on failure.
        """
        if not self.config.enabled or not query or not query.strip():
            return []

        try:
            from ..recipes import ContextAssembler

            assembler = ContextAssembler(
                model_class=self.model,
                score_weights=dict(self.memory.score_weights),
                max_items=limit or self.config.max_items,
                max_tokens=self.config.max_tokens,
                output_format="content",
            )
            result = assembler.assemble(
                query_cues={"topic": query.strip()},
                agent_id=self.config.agent_id,
            )
        except Exception as exc:
            self._record_failure("search", exc)
            return []

        out = []
        for record in result.records:
            try:
                out.append(
                    {
                        "key": record.db_key.redis_key,
                        "content": getattr(record, "content", "") or "",
                        "importance": float(getattr(record, "importance", 0.0) or 0.0),
                    }
                )
            except Exception:
                continue
        return out

    def correct(self, key: str, outcome: str = "contradicted") -> bool:
        """Apply a corrective outcome to one record by Redis key.

        Backs the ``memory_feedback`` MCP tool, which is how a model marks a
        retrieved memory wrong without deleting it -- the confidence field
        then down-ranks it over time.

        Args:
            key: The record's Redis key, as returned by :meth:`search`.
            outcome: Outcome to apply. Default ``"contradicted"``.

        Returns:
            ``True`` when the record was found and updated.
        """
        if not self.config.enabled or not key:
            return False
        try:
            from ..fields.observation import ObservationProtocol

            records = self.model.query.get_many([key], skip_none=True)
            records = [r for r in records if r is not None]
            if not records:
                return False
            ObservationProtocol.on_context_used(records, {key: outcome})
            return True
        except Exception as exc:
            self._record_failure("correct", exc)
            return False

    def status(self) -> Dict[str, Any]:
        """Describe the live state of the integration.

        Everything ``popoto-memory doctor`` prints and the ``memory_status``
        MCP tool returns. Never raises: each probe degrades to an error
        string in its own field, because the whole point of this call is to
        run when something is broken.

        Returns:
            A dict with connection, configuration, retrieval mode, record
            count, counters, and last-success timestamps. ``backend`` names
            the backend and ``reachable`` says whether its server answered
            (and, on Postgres, meets the version floor). Both backends carry
            the same key set: on Redis ``redis_url`` and ``redis_reachable``
            are filled as they always have been and ``postgres`` is ``None``;
            on Postgres ``postgres`` is a sub-dict (DSN summary, schema,
            server version, pgvector, health) and ``redis_url`` /
            ``redis_reachable`` are ``None``, never absent.
        """
        try:
            backend_name = self.backend_name
        except Exception as exc:
            backend_name = self._backend_label()
            info: Dict[str, Any] = self._status_base(backend_name)
            info["errors"].append(f"backend unresolved: {exc}")
            return info

        info = self._status_base(backend_name)
        if backend_name == "redis":
            try:
                t0 = time.perf_counter()
                server_info = self.redis.info("server")
                info["redis_reachable"] = True
                info["reachable"] = True
                info["ping_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                name = "valkey" if server_info.get("valkey_version") else "redis"
                version = server_info.get("valkey_version") or server_info.get(
                    "redis_version"
                )
                info["server"] = f"{name} {version}"
            except Exception as exc:
                info["errors"].append(f"redis unreachable: {exc}")
                return info
        elif not self._status_postgres(info):
            return info

        try:
            mode = getattr(self.memory.assembler, "_effective_mode", None)
            info["retrieval_mode"] = mode
            info["query_blind"] = mode == "composite"
        except Exception as exc:
            info["errors"].append(f"retrieval mode unresolved: {exc}")

        if self.schema_pending(info):
            # Every read below would create the schema and its tables (a
            # Postgres table is made on first use), so a doctor pointed at the
            # wrong database would quietly create popoto's objects there and
            # then report them present. Nothing has been written, so nothing
            # is lost by not reading: the counts are zero.
            info["record_count"] = 0
            info["log_tail"] = self.log_tail()
            return info

        try:
            info["record_count"] = self.model.query.filter(
                agent_id=self.config.agent_id
            ).count()
        except Exception as exc:
            info["errors"].append(f"record count failed: {exc}")

        try:
            info["counters"] = self._read_counters()
        except Exception as exc:
            info["errors"].append(f"counters unreadable: {exc}")

        try:
            info["last_success"] = self._read_last_events()
        except Exception as exc:
            info["errors"].append(f"timestamps unreadable: {exc}")

        info["log_tail"] = self.log_tail()
        return info

    @staticmethod
    def schema_pending(info: Dict[str, Any]) -> bool:
        """Whether a :meth:`status` result describes a Postgres schema that
        does not exist yet. A caller that would read through the model (the
        doctor's latency probe) must skip the read then: on Postgres the
        first read creates the schema and its tables, and ``doctor`` creates
        nothing it was asked to check."""
        pg = info.get("postgres")
        return isinstance(pg, dict) and pg.get("schema_exists") is False

    def _status_base(self, backend_name: str) -> Dict[str, Any]:
        """The configuration half of :meth:`status`, before any probe.

        On Redis the keys come in the order they always have (``redis_url``
        second, ``redis_reachable`` before ``server``), with ``backend``,
        ``reachable`` and ``postgres`` (``None``) appended, so ``doctor
        --json`` on Redis only gains keys. Both shapes carry the same key set.
        """
        if backend_name == "redis":
            return {
                "enabled": self.config.enabled,
                "redis_url": redact_url(self.config.url),
                "url_source": self.config.url_source,
                "agent_id": self.config.agent_id,
                "max_items": self.config.max_items,
                "max_tokens": self.config.max_tokens,
                "ingest": self.config.ingest,
                "log_path": str(self.config.log_path),
                "model": "DefaultMemory",
                "redis_reachable": False,
                "server": None,
                "retrieval_mode": None,
                "query_blind": None,
                "record_count": None,
                "counters": {},
                "last_success": {},
                "errors": [],
                "backend": backend_name,
                "reachable": False,
                "postgres": None,
            }
        # config.url and its source describe a Redis connection. On another
        # backend the connection comes from that backend's own variable, so
        # reporting the Redis one would point an operator at a setting
        # nothing reads.
        #
        # ``redis_url`` / ``redis_reachable`` stay in the dict as ``None``
        # rather than being dropped: a consumer of ``doctor --json`` written
        # against the Redis shape indexes them unconditionally, and a
        # missing key is a KeyError where ``None`` reads as "not applicable".
        return {
            "enabled": self.config.enabled,
            "backend": backend_name,
            "redis_url": None,
            "url_source": (
                "POPOTO_POSTGRES_URL" if backend_name == "postgres" else "unknown"
            ),
            "agent_id": self.config.agent_id,
            "max_items": self.config.max_items,
            "max_tokens": self.config.max_tokens,
            "ingest": self.config.ingest,
            "log_path": str(self.config.log_path),
            "model": "DefaultMemory",
            "redis_reachable": None,
            "reachable": False,
            "server": None,
            "retrieval_mode": None,
            "query_blind": None,
            "record_count": None,
            "counters": {},
            "last_success": {},
            "errors": [],
        }

    def _status_postgres(self, info: Dict[str, Any]) -> bool:
        """Probe a non-Redis backend for :meth:`status`; ``True`` when it is
        usable. Fills ``info["postgres"]`` with what the backend reports:
        DSN summary (never the password), schema, server version and whether
        popoto supports it, pgvector, and the backend's health record."""
        pg: Dict[str, Any] = {
            "dsn": None,
            "schema": None,
            "schema_exists": None,
            "schema_tables": None,
            "server_version": None,
            "supported": None,
            "pgvector": None,
            "health": None,
        }
        info["postgres"] = pg
        try:
            store = self._store()
        except Exception as exc:
            info["errors"].append(f"{info['backend']} unavailable: {exc}")
            return False
        if store is None:  # pragma: no cover - backend_name said otherwise
            info["errors"].append("backend resolved to redis unexpectedly")
            return False
        pg["dsn"] = getattr(store, "dsn_summary", None)
        pg["schema"] = getattr(store, "schema", None)
        describe = getattr(store, "describe", None)
        try:
            if describe is None:
                raise RuntimeError(
                    f"the {info['backend']!r} backend reports no server facts"
                )
            t0 = time.perf_counter()
            facts = describe()
            info["ping_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        except Exception as exc:
            info["errors"].append(f"{info['backend']} unreachable: {exc}")
            pg["health"] = self._health_of(store)
            return False
        pg.update(
            {
                key: facts.get(key)
                for key in (
                    "schema",
                    "schema_exists",
                    "schema_tables",
                    "server_version",
                    "supported",
                    "pgvector",
                    "database",
                )
            }
        )
        pg["health"] = self._health_of(store)
        info["server"] = f"postgresql {facts.get('server_version')}"
        if not facts.get("supported"):
            info["errors"].append(
                f"postgres server {facts.get('server_version')} "
                f"({facts.get('server_encoding')}) is not supported: popoto "
                "needs PostgreSQL 18 or newer with server_encoding UTF8"
            )
            return False
        info["reachable"] = True
        return True

    @staticmethod
    def _health_of(store: Any) -> Optional[Dict[str, Any]]:
        health = getattr(store, "health", None)
        as_dict = getattr(health, "as_dict", None)
        return as_dict() if as_dict is not None else None

    def purge_integration_state(self) -> int:
        """Delete the integration's own state for this agent id.

        The pending handoffs, injected sets, counters and last-success
        stamps -- never memory records, which the caller deletes through the
        model. For a scratch agent id (``examples/harness_memory/verify.py``)
        that should leave nothing behind: on Redis the keys under the agent's
        prefixes, on Postgres the agent's rows in the harness tables and
        ``popoto_counter``, which would otherwise never expire. Returns how
        many keys or rows were removed.
        """
        agent = self.config.agent_id
        counter_prefix = f"{COUNTER_KEY_PREFIX}:{agent}:"
        store = self._store()
        if store is not None:
            return int(
                self._state(store, HARNESS_FIELD, "purge", agent, counter_prefix)
            )
        removed = 0
        for prefix in (
            PENDING_KEY_PREFIX,
            INJECTED_KEY_PREFIX,
            COUNTER_KEY_PREFIX,
            LAST_EVENT_KEY_PREFIX,
        ):
            for key in self.redis.scan_iter(match=f"{prefix}:{agent}:*", count=200):
                removed += int(self.redis.delete(key))
        return removed

    def log_tail(self, lines: int = 5) -> List[str]:
        """Return the last ``lines`` entries of the failure log.

        Args:
            lines: How many trailing lines to return. Default 5.

        Returns:
            The lines, oldest first. Empty when the log does not exist.
        """
        try:
            path = self.config.log_path
            if not path.exists():
                return []
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return [ln.rstrip("\n") for ln in handle.readlines()[-lines:]]
        except Exception:
            return []

    # -- pending-turn handoff -------------------------------------------

    def _pending_key(self, session_id: str) -> str:
        return f"{PENDING_KEY_PREFIX}:{self.config.agent_id}:{session_id}"

    def _report_corrupt_pending(self, exc: BaseException) -> None:
        """Log and count one undecodable pending entry.

        Filed under ``pending_pop`` rather than a name of its own: from
        ``doctor``'s side this is the same symptom as any other failed
        outcome report, and a counter nobody recognizes is worse than a
        familiar one.
        """
        self._record_failure("pending_pop", exc)

    def _has_pending_turn(self, redis_key: str, turn_id: str) -> bool:
        """Whether an entry for ``turn_id`` is already staged on the list."""
        for raw in self.redis.lrange(redis_key, 0, -1) or []:
            _tagged, turn, _keys = _decode_pending_entry(raw)
            if turn == turn_id:
                return True
        return False

    def _push_pending(
        self,
        session_id: str,
        records: Any,
        turn_id: Optional[str] = None,
    ) -> None:
        """Queue this turn's injected record keys for later outcome reporting.

        A list, one entry per turn, rather than a single key per session.
        The write hook runs asynchronously on Claude Code, so a fast user
        can submit turn N+1 while turn N is still writing; a single slot
        would let turn N's outcome report land against turn N+1's records.
        The list carries a TTL and a length cap so an abandoned session
        cannot leak.

        When the harness sends a turn identifier -- Claude Code's
        ``prompt_id``, Codex's ``turn_id`` -- the entry is tagged with it as
        ``{"t": turn_id, "k": keys}`` and :meth:`_pop_pending` claims that
        exact entry by value. Positional pairing alone is what let an
        aborted turn, a crashed session, or a ``SubagentStop``-configured
        session popping more than it pushed shift every later pairing by one
        and report an outcome against the wrong turn's records (#574).
        OpenClaw's plugin forwards ``ctx.runId`` as ``turn_id``, so it is
        keyed too. Hermes's plugin now forwards its own per-turn id the same
        way (``plugins/hermes/__init__.py``, #704) -- it mints one once per
        turn and passes the same value to both ``pre_llm_call`` and
        ``post_llm_call``. Only a harness that genuinely sends no turn id, or
        a session with ``POPOTO_MEMORY_TURN_KEYED=0``, keeps writing the bare
        key array and the positional pairing. The
        ``RPUSH``/``LTRIM``/``EXPIRE`` pipeline, the key name, the cap, and
        the TTL are unchanged either way.
        """
        try:
            keys = []
            for record in records:
                try:
                    keys.append(record.db_key.redis_key)
                except Exception:
                    continue
            if not keys:
                return
            store = self._store()
            if store is not None:
                # The same contract on a typed table: one entry per turn,
                # tagged when the harness sent a turn id, deduplicated on it,
                # capped and expiring with the session (the adapter's
                # docstring has the statement).
                self._state(
                    store,
                    HARNESS_FIELD,
                    "pending_push",
                    self.config.agent_id,
                    session_id,
                    keys,
                    turn_id if (self.config.turn_keyed and turn_id) else None,
                    MAX_PENDING_TURNS,
                    PENDING_TTL_SECONDS,
                )
                return
            redis_key = self._pending_key(session_id)
            if self.config.turn_keyed and turn_id:
                # Advisory, not atomic: two concurrent pushes for the same
                # turn can both read an absent entry and both write. It is
                # here so a redelivered hook does not stage a second
                # claimable entry that no pop will ever consume, not to
                # serialize writers -- a lock would cost every turn a round
                # trip to prevent a case that costs one stale list element.
                #
                # It keys on the turn id alone, so a second push for one turn
                # carrying *different* records drops those records from the
                # handoff: they are still injected and still suppressed, they
                # just get no outcome report. One turn resolves once, which is
                # the contract; a turn assembling twice is the anomaly.
                if self._has_pending_turn(redis_key, turn_id):
                    return
                payload = json.dumps(
                    {"t": turn_id, "k": keys},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            else:
                payload = json.dumps(keys)
            pipe = self.redis.pipeline()
            pipe.rpush(redis_key, payload)
            pipe.ltrim(redis_key, -MAX_PENDING_TURNS, -1)
            pipe.expire(redis_key, PENDING_TTL_SECONDS)
            pipe.execute()
        except Exception as exc:
            self._record_failure("pending_push", exc)

    # -- per-session injection suppression -------------------------------

    def _injected_key(self, session_id: str) -> str:
        return f"{INJECTED_KEY_PREFIX}:{self.config.agent_id}:{session_id}"

    def _injected_keys(self, session_id: Optional[str]) -> Optional[Set[str]]:
        """Return the record keys already injected in this session.

        Passed to ``assemble(exclude_keys=...)`` so a memory surfaced once is
        not re-injected every turn. Injected context is appended to the model's
        prompt and stays resident for the rest of the session, so re-adding the
        same top-k -- which topically similar consecutive prompts produce --
        makes cumulative cache-read grow with the square of turn count.
        Declining to re-add is append-only, so it costs nothing against the
        cache, unlike pruning.

        Returns ``None`` (not an empty set) when there is no session to scope
        by or the read fails, so ``assemble`` stays byte-identical to the
        unsuppressed path rather than silently gating on partial state.
        """
        if not session_id:
            return None
        try:
            store = self._store()
            if store is None:
                members = self.redis.smembers(self._injected_key(session_id))
            else:
                members = self._state(
                    store,
                    HARNESS_FIELD,
                    "injected_read",
                    self.config.agent_id,
                    session_id,
                )
        except Exception as exc:
            self._record_failure("injected_read", exc)
            return None
        if not members:
            return None
        return {
            m.decode("utf-8", errors="replace") if isinstance(m, bytes) else str(m)
            for m in members
        }

    def _mark_injected(self, session_id: str, records: Any) -> None:
        """Record this turn's keys so later turns suppress them.

        A SET, not the pending FIFO: the FIFO is consumed by ``feedback`` and
        must stay a per-turn queue, while suppression needs the accumulated
        union for the whole session. Carries the same TTL so an abandoned
        session cannot leak.
        """
        try:
            keys = []
            for record in records:
                try:
                    keys.append(record.db_key.redis_key)
                except Exception:
                    continue
            if not keys:
                return
            store = self._store()
            if store is not None:
                self._state(
                    store,
                    HARNESS_FIELD,
                    "injected_mark",
                    self.config.agent_id,
                    session_id,
                    keys,
                    PENDING_TTL_SECONDS,
                )
                return
            redis_key = self._injected_key(session_id)
            pipe = self.redis.pipeline()
            pipe.sadd(redis_key, *keys)
            pipe.expire(redis_key, PENDING_TTL_SECONDS)
            pipe.execute()
        except Exception as exc:
            self._record_failure("injected_mark", exc)

    def _pop_pending(
        self,
        session_id: str,
        turn_id: Optional[str] = None,
    ) -> List[str]:
        """Claim one unresolved turn's record keys and return them.

        With a turn id and turn keying enabled, claims the entry tagged with
        that id: ``LRANGE`` the list, find the first element whose ``t``
        matches, then ``LREM key 1 <that exact raw element>``. The keys are
        returned only when ``LREM`` reports a removal, so of two callers
        racing on one turn exactly one reports the outcome. ``LREM`` is
        given the raw element ``LRANGE`` returned rather than a
        re-serialization, so no difference in key order or separator
        spacing can make the claim silently miss.

        Falls back to the positional ``LPOP`` when there is no turn id, when
        turn keying is off, or when every staged entry is untagged -- a
        queue written entirely before this upgrade, where positional pairing
        is the only pairing those entries ever had. A turn id that matches
        nothing on a list that *does* carry tags is a miss, not a licence to
        pop positionally: reporting against whatever sits at the head is
        exactly the misattribution this change removes.
        """
        try:
            store = self._store()
            if store is not None:
                return self._pop_pending_store(store, session_id, turn_id)
        except Exception as exc:
            self._record_failure("pending_pop", exc)
            return []
        redis_key = self._pending_key(session_id)
        try:
            if turn_id and self.config.turn_keyed:
                saw_tagged = False
                for raw in self.redis.lrange(redis_key, 0, -1) or []:
                    tagged, turn, keys = _decode_pending_entry(
                        raw, on_corrupt=self._report_corrupt_pending
                    )
                    saw_tagged = saw_tagged or tagged
                    if turn is None or turn != turn_id:
                        continue
                    if not self.redis.lrem(redis_key, 1, raw):
                        return []
                    return keys
                if saw_tagged:
                    self._record_failure(
                        "pending_miss",
                        LookupError(f"no pending entry for turn {turn_id}"),
                    )
                    return []
            raw = self.redis.lpop(redis_key)
            if not raw:
                return []
            _tagged, _turn, keys = _decode_pending_entry(
                raw, on_corrupt=self._report_corrupt_pending
            )
            return keys
        except Exception as exc:
            self._record_failure("pending_pop", exc)
            return []

    def _pop_pending_store(
        self, store: Any, session_id: str, turn_id: Optional[str]
    ) -> List[str]:
        """:meth:`_pop_pending` on a non-Redis backend, with the same three
        outcomes: the turn's entry when one is staged (or nothing, when a
        concurrent caller claimed it first), a recorded ``pending_miss`` when
        the session holds tagged entries but none for this turn, and the
        positional pop of the oldest entry otherwise."""
        agent = self.config.agent_id
        if turn_id and self.config.turn_keyed:
            keys, hit, saw_tagged = self._state(
                store, HARNESS_FIELD, "pending_claim", agent, session_id, turn_id
            )
            if hit:
                return list(keys or [])
            if saw_tagged:
                self._record_failure(
                    "pending_miss",
                    LookupError(f"no pending entry for turn {turn_id}"),
                )
                return []
        popped = self._state(store, HARNESS_FIELD, "pending_pop", agent, session_id)
        return list(popped or [])

    # -- observability ---------------------------------------------------

    def _record_failure(self, operation: str, exc: BaseException) -> None:
        """Log a swallowed exception and try to increment its counter.

        A hook has no console, so the log line is the reliable channel --
        it always lands. The counter is best-effort against the same
        client that just failed: when Redis itself is unreachable, the
        counter write also fails silently, which is exactly the case a
        user whose Redis moved needs the log line for.

        The warning names the backend in use (#814), because a hook's stderr
        is often all an operator sees and "Error 61 connecting to ..." does
        not say which store was being dialled. A Postgres outage
        (:class:`~popoto.backends.BackendUnavailableError`) trips the same
        once-per-process short circuit a Redis connection error does.
        """
        backend = self._backend_label()
        logger.warning(
            "popoto memory %s failed (backend: %s): %s", operation, backend, exc
        )
        from ..backends.types import BackendUnavailableError

        if isinstance(exc, OUTAGE_ERRORS) or isinstance(exc, BackendUnavailableError):
            self._redis_down = True
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        detail = " ".join(str(exc).split())
        line = f"{stamp} {operation} {type(exc).__name__}: {detail}\n"
        try:
            path = self.config.log_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            pass
        if self._redis_down:
            return
        try:
            key = f"{COUNTER_KEY_PREFIX}:{self.config.agent_id}:{operation}"
            store = self._store()
            if store is None:
                self.redis.incr(key)
            else:
                self._state(store, COUNTER_FIELD, "increment", key, 1)
        except Exception:
            pass

    def _touch(self, operation: str) -> None:
        """Record a successful operation's timestamp and count.

        Doubles as the liveness probe for the read path: a failure here is
        reported rather than swallowed, because it is the only signal that
        distinguishes "no memories matched" from "the server is gone".
        """
        try:
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            store = self._store()
            if store is not None:
                agent = self.config.agent_id
                self._state(
                    store,
                    HARNESS_FIELD,
                    "touch",
                    agent,
                    operation,
                    stamp,
                    f"{COUNTER_KEY_PREFIX}:{agent}:{operation}_ok",
                )
                return
            pipe = self.redis.pipeline()
            pipe.set(
                f"{LAST_EVENT_KEY_PREFIX}:{self.config.agent_id}:{operation}", stamp
            )
            pipe.incr(f"{COUNTER_KEY_PREFIX}:{self.config.agent_id}:{operation}_ok")
            pipe.execute()
        except Exception as exc:
            self._record_failure(operation, exc)

    def _read_counters(self) -> Dict[str, int]:
        prefix = f"{COUNTER_KEY_PREFIX}:{self.config.agent_id}:"
        out: Dict[str, int] = {}
        store = self._store()
        if store is not None:
            counts = self._state(store, COUNTER_FIELD, "scan", prefix)
            return {name[len(prefix) :]: int(v) for name, v in counts.items()}
        for key in self.redis.scan_iter(match=f"{prefix}*", count=100):
            name = key.decode() if isinstance(key, bytes) else str(key)
            value = self.redis.get(name)
            try:
                out[name[len(prefix) :]] = int(value)
            except (TypeError, ValueError):
                continue
        return out

    def _read_last_events(self) -> Dict[str, str]:
        prefix = f"{LAST_EVENT_KEY_PREFIX}:{self.config.agent_id}:"
        out: Dict[str, str] = {}
        store = self._store()
        if store is not None:
            return dict(
                self._state(store, HARNESS_FIELD, "events", self.config.agent_id)
            )
        for key in self.redis.scan_iter(match=f"{prefix}*", count=100):
            name = key.decode() if isinstance(key, bytes) else str(key)
            value = self.redis.get(name)
            if value is None:
                continue
            out[name[len(prefix) :]] = (
                value.decode() if isinstance(value, bytes) else str(value)
            )
        return out

    def _warn_heuristic_cost(self) -> None:
        """Log the measured cost of leaving the default ingest mode, once."""
        marker = f"{COUNTER_KEY_PREFIX}:{self.config.agent_id}:heuristic_notice"
        try:
            store = self._store()
            if store is None:
                first = self.redis.setnx(marker, 1)
            else:
                first = self._state(store, COUNTER_FIELD, "set_if_absent", marker, 1)
        except Exception:
            first = True
        if first:
            logger.warning(
                "POPOTO_MEMORY_INGEST=heuristic selected. Issue #489 measured "
                "sentence-splitting extraction at 0.2078 judged accuracy "
                "against 0.3636 for raw turn ingestion on the same slice. "
                "Unset the variable to return to the measured-best write path."
            )


def resolve_log_path() -> str:
    """Return the effective log path without constructing a service.

    Returns:
        The absolute path swallowed errors are written to.
    """
    return str(MemoryConfig.from_env(os.environ).log_path)
