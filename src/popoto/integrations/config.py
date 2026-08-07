"""Environment-driven configuration for the harness integration.

Everything the hook process and the MCP server need is resolved from the
environment plus the current working directory, because neither has a
config file of its own: a hook is a bare command string in the harness's
settings, and an MCP server is a command plus an ``env`` map. There is no
place to pass Python arguments, so the environment is the whole interface.

Every variable is optional. The zero-configuration path is "local Redis or
Valkey on the default port, memories scoped to this project".

======================================  =============================  ====
Variable                                Default                        Note
======================================  =============================  ====
``POPOTO_MEMORY_URL``                   ``REDIS_URL`` or
                                        ``redis://localhost:6379/0``   Valkey URLs are identical
``POPOTO_MEMORY_AGENT_ID``              basename of the cwd            Per-project scoping
``POPOTO_MEMORY_MAX_ITEMS``             ``5``                          Diverges from the benchmark; see below
``POPOTO_MEMORY_MAX_TOKENS``            ``800``                        Under Codex's 2500-token cap
``POPOTO_MEMORY_INGEST``                ``raw``                        ``raw`` | ``heuristic``
``POPOTO_MEMORY_ENABLED``               ``1``                          Kill switch, no config edit needed
``POPOTO_MEMORY_LOG``                   ``~/.popoto/memory.log``       Where swallowed errors land
======================================  =============================  ====

**The ``max_items`` / ``max_tokens`` divergence is deliberate and is the
only place this package departs from the benchmarked configuration.** The
retrieval benchmark ran at ``max_items=20``, which is a reasonable budget
for a question-answering evaluation and the wrong budget for a coding
harness, where a turn fires every few seconds and context is contested by
file contents, tool output, and the system prompt. Codex additionally caps
injected context at 2500 tokens (``additionalContextLimit``). Scoring is
*not* changed: ``score_weights`` stays at the benchmarked
``{"relevance": 1.0}`` and retrieval stays on the lexical/BM25 path that
:class:`popoto.recipes.DefaultMemory` selects.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

DEFAULT_URL = "redis://localhost:6379/0"
"""Connection URL used when neither ``POPOTO_MEMORY_URL`` nor ``REDIS_URL``
is set. Valkey uses the same scheme, so this default covers both."""

DEFAULT_MAX_ITEMS = 5
"""Records injected per turn. See the module docstring for why this is not
the benchmark's 20."""

DEFAULT_MAX_TOKENS = 800
"""Soft token budget for the injected block, chosen to sit well under
Codex's 2500-token ``additionalContextLimit``."""

DEFAULT_INGEST = "raw"
"""Write path. ``raw`` is :class:`~popoto.extraction.RawTurnExtractionProvider`,
the arm issue #489 measured ahead of the heuristic (0.3636 vs 0.2078 judged
accuracy)."""

VALID_INGEST_MODES = ("raw", "heuristic")
"""Accepted ``POPOTO_MEMORY_INGEST`` values. ``llm`` is deliberately absent:
it requires an API key, and the zero-key promise is the point of this
integration. Use the library path with ``ClaudeExtractionProvider`` for
that."""

DEFAULT_LOG_PATH = "~/.popoto/memory.log"
"""Where the hook writes the errors it swallows. A hook has no console, so
a file is the only observable surface besides ``popoto-memory doctor``."""

PENDING_TTL_SECONDS = 3600
"""Lifetime of a turn's pending-record handoff between the read hook and
the write hook. An hour is long enough for a slow turn and short enough
that an abandoned session does not leak keys."""

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _as_bool(raw: Optional[str], default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _as_int(raw: Optional[str], default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class MemoryConfig:
    """Resolved configuration for one :class:`~popoto.integrations.service.MemoryService`.

    Attributes:
        url: Redis or Valkey connection URL.
        agent_id: Partition key for every read and write. Defaults to the
            basename of the working directory, so two projects on one Redis
            do not read each other's memories.
        max_items: Maximum records injected per turn.
        max_tokens: Soft token budget for the injected block.
        ingest: ``"raw"`` or ``"heuristic"``.
        enabled: When ``False`` every operation is a no-op that still exits
            cleanly, so the kill switch never breaks a turn.
        log_path: File that swallowed exceptions are appended to.
        url_is_explicit: ``True`` when ``POPOTO_MEMORY_URL`` was set by the
            caller. :func:`bind_connection` refuses to rebind an already
            established Popoto connection unless this is ``True``, which is
            what keeps an in-process caller (a test, a Hermes handler) on
            the connection it already configured.
    """

    url: str = DEFAULT_URL
    agent_id: str = "default"
    max_items: int = DEFAULT_MAX_ITEMS
    max_tokens: int = DEFAULT_MAX_TOKENS
    ingest: str = DEFAULT_INGEST
    enabled: bool = True
    log_path: Path = Path(DEFAULT_LOG_PATH).expanduser()
    url_is_explicit: bool = False

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> "MemoryConfig":
        """Resolve configuration from environment variables and a directory.

        Unparseable numeric values fall back to their defaults rather than
        raising: a typo in a harness config must not break the user's turn.

        Args:
            env: Mapping to read variables from. Defaults to ``os.environ``.
            cwd: Directory whose basename becomes the default ``agent_id``.
                Defaults to the process working directory. Hook payloads
                carry the harness's ``cwd``, which is more accurate than the
                hook process's own.

        Returns:
            A frozen :class:`MemoryConfig`.
        """
        env = os.environ if env is None else env

        explicit_url = env.get("POPOTO_MEMORY_URL", "").strip()
        url = explicit_url or env.get("REDIS_URL", "").strip() or DEFAULT_URL

        agent_id = env.get("POPOTO_MEMORY_AGENT_ID", "").strip()
        if not agent_id:
            agent_id = derive_agent_id(cwd)

        ingest = env.get("POPOTO_MEMORY_INGEST", "").strip().lower()
        if ingest not in VALID_INGEST_MODES:
            ingest = DEFAULT_INGEST

        log_raw = env.get("POPOTO_MEMORY_LOG", "").strip() or DEFAULT_LOG_PATH

        return cls(
            url=url,
            agent_id=agent_id,
            max_items=_as_int(env.get("POPOTO_MEMORY_MAX_ITEMS"), DEFAULT_MAX_ITEMS),
            max_tokens=_as_int(env.get("POPOTO_MEMORY_MAX_TOKENS"), DEFAULT_MAX_TOKENS),
            ingest=ingest,
            enabled=_as_bool(env.get("POPOTO_MEMORY_ENABLED"), True),
            log_path=Path(log_raw).expanduser(),
            url_is_explicit=bool(explicit_url),
        )


def derive_agent_id(cwd: Optional[str] = None) -> str:
    """Derive the default ``agent_id`` from a directory path.

    The basename of the working directory. Coarse on purpose: it is
    understandable at a glance, stable across sessions in the same
    checkout, and overridable with ``POPOTO_MEMORY_AGENT_ID``. Two
    same-named directories in different parents do collide; a project
    identity scheme is a separate design problem.

    Args:
        cwd: Directory to derive from. Defaults to the process working
            directory. An unreadable working directory yields
            ``"default"`` rather than raising.

    Returns:
        A non-empty agent id string.
    """
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError:
            return "default"
    name = os.path.basename(os.path.normpath(str(cwd).strip() or "."))
    return name or "default"


def bind_connection(config: MemoryConfig) -> bool:
    """Point Popoto's shared connection at ``config.url``.

    Only acts when ``POPOTO_MEMORY_URL`` was set explicitly. Without it,
    Popoto's own import-time resolution (``REDIS_URL``, else
    ``localhost:6379/0``) already produced the same target, and leaving the
    live connection alone is the safe behavior for an in-process caller --
    a test running under the Popoto pytest plugin on an isolated database,
    or a Hermes ``handler.py`` inside a long-lived harness -- which has
    already chosen its connection. Rebinding those to a default would move
    writes to database 0 behind the caller's back.

    The rebind swaps the *pool* on the existing client object rather than
    replacing the client. Most of Popoto imports ``POPOTO_REDIS_DB`` at
    module load for speed, so those references are bound to one object for
    the life of the process; assigning a new client to the module global
    would leave every already-imported module writing to the old target.
    This is the same in-place technique ``popoto.pytest_plugin._swap_db``
    uses, and for the same reason.

    Args:
        config: The resolved configuration whose ``url`` to bind.

    Returns:
        ``True`` when the connection was rebound, ``False`` when it was
        already correct or no explicit URL was given.
    """
    if not config.url_is_explicit:
        return False

    import redis
    from redis.connection import parse_url

    from ..redis_db import POPOTO_REDIS_DB

    wanted = parse_url(config.url)
    client = POPOTO_REDIS_DB
    current = dict(client.connection_pool.connection_kwargs)
    if all(current.get(key) == value for key, value in wanted.items()):
        return False

    current.update(wanted)
    current.setdefault("socket_timeout", 5)
    current.setdefault("socket_connect_timeout", 5)
    old_pool = client.connection_pool
    client.connection_pool = redis.ConnectionPool(
        connection_class=old_pool.connection_class, **current
    )
    try:
        old_pool.disconnect()
    except Exception:
        pass
    return True
