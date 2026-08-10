"""MemoryService -- the harness-agnostic core shared by hooks and MCP tools.

One object with four operations: :meth:`~MemoryService.assemble`,
:meth:`~MemoryService.capture`, :meth:`~MemoryService.feedback`, and
:meth:`~MemoryService.status`. Every harness adapter, and every MCP tool,
goes through it, so there is exactly one code path to Redis and exactly one
schema (:class:`popoto.recipes.DefaultMemory`).

Two contracts this module keeps deliberately:

**It returns a context string; it never touches a message array.** The
library recipe's ``inject_context()`` appends to ``messages[0]``, the system
message, which invalidates a cached system prefix on every turn. On the hook
path the harness places the returned string in the *user* turn (Claude Code
and Codex ``additionalContext``, Hermes ``context``, OpenClaw
``appendContext``), which preserves prompt caching. Message placement is the
harness's decision, not this module's.

**It writes through** :class:`~popoto.extraction.RawTurnExtractionProvider`
**by default.** See :attr:`MemoryService.extractor` and issue #489.

Failures are swallowed -- a memory error must never break a user's turn --
but never silently. Each swallowed exception appends a line to the
configured log file and increments a Redis counter that
``popoto-memory doctor`` reads back.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import PENDING_TTL_SECONDS, MemoryConfig, bind_connection

logger = logging.getLogger("POPOTO.integrations")

COUNTER_KEY_PREFIX = "$popoto_memory:counter"
"""Redis key prefix for failure and activity counters. ``INCR`` is atomic,
so ``doctor`` can read these while a hook writes them."""

PENDING_KEY_PREFIX = "$popoto_memory:pending"
"""Redis key prefix for the read-hook-to-write-hook handoff."""

LAST_EVENT_KEY_PREFIX = "$popoto_memory:last"
"""Redis key prefix for last-success timestamps, so a silently broken
injection is visible in ``doctor`` without reading the log."""

MAX_PENDING_TURNS = 32
"""Cap on queued unresolved turns per session. A harness that reads without
ever firing its write event (or a crashed session) would otherwise grow the
handoff list without bound."""


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
        # The single place the connection is bound. Every entry point --
        # the hook, the MCP server, doctor, demo, the examples, the Hermes
        # handler -- reaches Redis through a MemoryService, so binding here
        # is what makes POPOTO_MEMORY_URL mean the same thing on all of
        # them. Binding in the CLI instead left demo, seed.py, verify.py and
        # the Hermes handler writing to database 0 while printing the URL
        # they were not using.
        #
        # Safe for in-process callers: bind_connection is a no-op unless
        # POPOTO_MEMORY_URL was set explicitly, so a test under the pytest
        # plugin, or a host application that configured its own connection,
        # keeps the one it chose.
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
        """Popoto's shared Redis or Valkey client."""
        from ..redis_db import POPOTO_REDIS_DB

        return POPOTO_REDIS_DB

    # -- public operations ----------------------------------------------

    def assemble(self, query: str, session_id: Optional[str] = None) -> str:
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

        Returns:
            The formatted context block, or ``""`` when memory is disabled,
            the query is empty, nothing matched, or retrieval failed. An
            empty string means the caller must emit no context key at all:
            a bare "Relevant context:" header with nothing under it is
            worse than silence.
        """
        if not self.config.enabled or not query or not query.strip():
            return ""

        try:
            result = self.memory._assembler.assemble(
                query_cues={"topic": query.strip()},
                agent_id=self.config.agent_id,
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
            self._push_pending(session_id, result.records)

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
        else:
            # Non-empty text always yields at least one fact, so reaching
            # here means the save was rejected or the server is gone. The
            # recipe swallows that internally; record it so `doctor` can
            # show a write path that has silently stopped working.
            self._record_failure(
                "capture", RuntimeError("no record written for a non-empty turn")
            )
        return keys

    def feedback(self, session_id: str, outcome: str = "acted") -> int:
        """Report how the memories injected for a turn were used.

        Pops the oldest unresolved turn for ``session_id`` and applies
        ``outcome`` to its records through
        :class:`~popoto.fields.observation.ObservationProtocol`, which is
        what drives the confidence and decay loop.

        A missing pending turn degrades to a no-op. It never reports
        against another turn's records: the handoff is a FIFO list and this
        method pops exactly one entry.

        Args:
            session_id: Harness session identifier.
            outcome: One of ``"acted"``, ``"used"``, ``"dismissed"``,
                ``"deferred"``, ``"contradicted"``. Default ``"acted"``.

        Returns:
            Number of records the outcome was applied to.
        """
        if not self.config.enabled or not session_id:
            return 0

        keys = self._pop_pending(session_id)
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
            count, counters, and last-success timestamps.
        """
        info: Dict[str, Any] = {
            "enabled": self.config.enabled,
            "redis_url": self.config.url,
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
        }

        try:
            t0 = time.perf_counter()
            server_info = self.redis.info("server")
            info["redis_reachable"] = True
            info["ping_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            name = "valkey" if server_info.get("valkey_version") else "redis"
            version = server_info.get("valkey_version") or server_info.get(
                "redis_version"
            )
            info["server"] = f"{name} {version}"
        except Exception as exc:
            info["errors"].append(f"redis unreachable: {exc}")
            return info

        try:
            mode = getattr(self.memory._assembler, "_effective_mode", None)
            info["retrieval_mode"] = mode
            info["query_blind"] = mode == "composite"
        except Exception as exc:
            info["errors"].append(f"retrieval mode unresolved: {exc}")

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

    def _push_pending(self, session_id: str, records: Any) -> None:
        """Queue this turn's injected record keys for later outcome reporting.

        A FIFO list, one entry per turn, rather than a single key per
        session. The write hook runs asynchronously on Claude Code, so a
        fast user can submit turn N+1 while turn N is still writing; a
        single slot would let turn N's outcome report land against turn
        N+1's records. ``RPUSH`` then ``LPOP`` pairs each write with the
        read that preceded it, and both commands are atomic, so no lock is
        needed. The list carries a TTL and a length cap so an abandoned
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
            redis_key = self._pending_key(session_id)
            pipe = self.redis.pipeline()
            pipe.rpush(redis_key, json.dumps(keys))
            pipe.ltrim(redis_key, -MAX_PENDING_TURNS, -1)
            pipe.expire(redis_key, PENDING_TTL_SECONDS)
            pipe.execute()
        except Exception as exc:
            self._record_failure("pending_push", exc)

    def _pop_pending(self, session_id: str) -> List[str]:
        """Pop and return the oldest unresolved turn's record keys."""
        try:
            raw = self.redis.lpop(self._pending_key(session_id))
            if not raw:
                return []
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            keys = json.loads(raw)
            return [k for k in keys if isinstance(k, str)]
        except Exception as exc:
            self._record_failure("pending_pop", exc)
            return []

    # -- observability ---------------------------------------------------

    def _record_failure(self, operation: str, exc: BaseException) -> None:
        """Log a swallowed exception and increment its counter.

        A hook has no console. Without both halves of this -- a durable log
        line and a counter ``doctor`` can read -- a user whose Redis moved
        would see memory quietly stop working with nothing to look at.
        """
        logger.warning("popoto memory %s failed: %s", operation, exc)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        line = f"{stamp} {operation} {type(exc).__name__}: {exc}\n"
        try:
            path = self.config.log_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            pass
        try:
            self.redis.incr(f"{COUNTER_KEY_PREFIX}:{self.config.agent_id}:{operation}")
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
            first = self.redis.setnx(marker, 1)
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
