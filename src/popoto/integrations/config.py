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
``POPOTO_MEMORY_TURN_KEYED``            ``1``                          ``0`` restores the pre-#574 session FIFO handoff
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

ALLOW_DB0_ENV = "POPOTO_MEMORY_ALLOW_DB0"
"""Deploy-level opt-in for writing agent memory to Redis database 0. An
environment variable rather than a constructor argument on purpose: a PyPI
adopter running the hook cannot edit model code, and a hook is a bare
command string with no place to pass Python arguments. Accepts the same
truthy set as ``POPOTO_MEMORY_ENABLED`` (``1``/``true``/``yes``/``on``)."""

HOOK_SOCKET_TIMEOUT_SECONDS = 1.0
"""Socket connect and read timeout applied when the integration binds its
own connection (``POPOTO_MEMORY_URL`` or ``REDIS_URL``). The read hook sits
on the user's prompt path, so a hung server must cost about a second, not
the library default of five per attempt. Retries are disabled for the same
reason: the harness will run the hook again next turn. Lives here rather
than in ``popoto.fields.constants.Defaults`` because it is integration
transport config, not a retrieval tuning constant; see that docstring for
the convention."""

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
        turn_keyed: When ``True`` (the default) the read-to-write handoff is
            keyed on the harness's per-turn identifier, so an outcome report
            resolves the turn that actually staged it or resolves nothing.
            When ``False`` the handoff is the pre-#574 session-wide FIFO, in
            both its behavior and its on-disk encoding. Set from
            ``POPOTO_MEMORY_TURN_KEYED``; this is the deploy-level escape
            hatch for an operator running Popoto from PyPI who cannot edit
            code.
    """

    url: str = DEFAULT_URL
    agent_id: str = "default"
    max_items: int = DEFAULT_MAX_ITEMS
    max_tokens: int = DEFAULT_MAX_TOKENS
    ingest: str = DEFAULT_INGEST
    enabled: bool = True
    log_path: Path = Path(DEFAULT_LOG_PATH).expanduser()
    url_is_explicit: bool = False
    url_source: str = "default"
    allow_db0: bool = False
    turn_keyed: bool = True

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
        inherited_url = env.get("REDIS_URL", "").strip()
        if explicit_url:
            url, url_source = explicit_url, "POPOTO_MEMORY_URL"
        elif inherited_url:
            url, url_source = inherited_url, "REDIS_URL"
        else:
            url, url_source = DEFAULT_URL, "default"

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
            url_source=url_source,
            allow_db0=_as_bool(env.get(ALLOW_DB0_ENV), False),
            turn_keyed=_as_bool(env.get("POPOTO_MEMORY_TURN_KEYED"), True),
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


class Db0RefusedError(ValueError):
    """Raised when agent memory would be written to Redis database 0.

    Subclasses ``ValueError`` so the existing handlers keep working: the
    hook's blanket catch, the MCP dispatcher's, and ``doctor``'s explicit
    ``except ValueError`` all predate this error and all do the right thing
    with it.
    """


def redact_url(url: str) -> str:
    """Return ``url`` with any password replaced by ``***``.

    ``status()`` feeds the MCP ``memory_status`` tool and ``doctor``, both
    of which land in transcripts, so the connection URL must never carry
    the credential through.
    """
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.password:
        return url
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    userinfo = f"{parts.username or ''}:***@"
    return urlunsplit(parts._replace(netloc=userinfo + host))


def effective_db(config: MemoryConfig) -> int:
    """The database number this service will actually write to.

    Two cases, and conflating them is the trap:

    * ``url_is_explicit`` -- the caller named a URL, so its ``db`` is the
      answer. A URL with no ``db`` at all raises the "no database number"
      ``ValueError`` here.
    * otherwise -- Popoto's live connection is the answer, not
      ``config.url``. With no ``POPOTO_MEMORY_URL``, ``config.url`` is
      ``DEFAULT_URL`` (database 0) even when the process is on database
      15: the pytest plugin swaps ``POPOTO_REDIS_DB``'s pool in place and
      never touches ``MemoryConfig``. Reading ``config.url`` here would
      refuse on every test in the suite and on every host application
      that configured its own connection.
    """
    from redis.connection import parse_url

    from ..redis_db import POPOTO_REDIS_DB

    if config.url_is_explicit:
        wanted = parse_url(config.url)
        if "db" not in wanted:
            raise ValueError(_no_db_message(config.url))
        return int(wanted["db"])
    return int(POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db", 0) or 0)


def suggest_free_db() -> Optional[int]:
    """Lowest database in 1..15 that currently holds no keys, or ``None``.

    Best effort and advisory only. Reads ``INFO keyspace`` (a core command
    on both Redis and Valkey), which reports only non-empty databases, so
    anything in 1..15 absent from that report is empty. Any exception
    yields ``None``; a diagnostic must never be the thing that fails. This
    function never rebinds anything.
    """
    try:
        from ..redis_db import POPOTO_REDIS_DB

        info = POPOTO_REDIS_DB.info("keyspace")
        used = set()
        for name in info:
            if isinstance(name, bytes):
                name = name.decode()
            if str(name).startswith("db"):
                used.add(int(str(name)[2:]))
        for candidate in range(1, 16):
            if candidate not in used:
                return candidate
    except Exception:
        return None
    return None


def _no_db_message(url: str) -> str:
    return (
        f"POPOTO_MEMORY_URL={url!r} has no database number. "
        "Write it in full, for example redis://localhost:6379/0 -- a "
        "trailing slash with nothing after it is not database 0, it is "
        "no database at all, and honouring it would silently leave "
        "every write on whatever database Popoto is already using."
    )


def _db0_refusal_message(config: MemoryConfig) -> str:
    free = suggest_free_db()
    example_db = free if free is not None else 1
    hint = f" (database {free} is empty on this server)" if free is not None else ""
    example = f"redis://localhost:6379/{example_db}"
    if config.url_source == "POPOTO_MEMORY_URL":
        first = (
            f"POPOTO_MEMORY_URL={config.url} targets Redis database 0, and "
            "Popoto refuses to write agent memory there."
        )
        fix = f"Give the memory corpus a database of its own{hint}:"
    elif config.url_source == "REDIS_URL":
        first = (
            f"REDIS_URL={config.url} puts Popoto on Redis database 0, and "
            "Popoto refuses to write agent memory there."
        )
        fix = (
            "Point the memory corpus at a database of its own without moving "
            f"the rest of your application{hint}:"
        )
    else:
        first = (
            "Popoto's default connection is Redis database 0, and Popoto "
            "refuses to write agent memory there."
        )
        fix = f"Give the memory corpus a database of its own{hint}:"
    return (
        f"{first} DB 0 is the default database of every stock Redis/Valkey "
        "install, so it is the one most likely to already hold another "
        "application's data, and a single FLUSHDB destroys both. The pytest "
        "plugin refuses popoto_test_db=0 for the same reason; this is the "
        "same rule on the write path.\n"
        f"{fix}\n"
        f"    export POPOTO_MEMORY_URL={example}\n"
        "Or, if database 0 really is where this corpus belongs, opt in at "
        "deploy time:\n"
        f"    export {ALLOW_DB0_ENV}=1"
    )


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

    Raises:
        ValueError: If ``config.url`` carries no parseable database number.
            Refusing is the point: ``redis://localhost:6379/`` parses to a
            dict with no ``db`` key, so silently keeping the current
            connection would leave writes on database 0 while the caller
            believes it redirected them.
        Db0RefusedError: If the effective database is 0 and neither
            ``POPOTO_MEMORY_ALLOW_DB0`` nor ``config.allow_db0`` opts in.
    """
    # A disabled memory layer writes to no database, so there is nothing to
    # refuse. This has to come first: MemoryService.__init__ binds
    # unconditionally while config.enabled is consulted inside the
    # operation methods, so guarding without this check turns the
    # documented kill switch into a crash for the operator most likely to
    # reach for it -- someone on a DB 0 machine turning memory off. It also
    # skips the INFO keyspace probe and the POPOTO_REDIS_DB import on the
    # disabled path.
    if not config.enabled:
        return False

    if not config.url_is_explicit and config.url_source != "REDIS_URL":
        # Neither variable set: an in-process caller (a test under the
        # pytest plugin, a host application) chose this connection. Leave
        # it, timeouts included. ``url_is_explicit`` rather than
        # ``url_source`` because in-process callers build MemoryConfig
        # directly and set only the former. No rebind happens, so the DB 0
        # guard judges the live connection -- the database that will
        # actually be written to. The zero-configuration path is exactly
        # the one #584 is about.
        if effective_db(config) == 0 and not config.allow_db0:
            raise Db0RefusedError(_db0_refusal_message(config))
        return False

    import redis
    from redis.backoff import NoBackoff
    from redis.connection import parse_url
    from redis.retry import Retry

    from ..redis_db import POPOTO_REDIS_DB

    wanted = parse_url(config.url)
    if "db" not in wanted:
        raise ValueError(_no_db_message(config.url))
    target_db = int(wanted["db"])

    if not config.url_is_explicit:
        # REDIS_URL path. Popoto's own import-time resolution read the same
        # variable, so the live connection diverging from REDIS_URL means an
        # in-process caller deliberately swapped the pool afterwards -- the
        # pytest plugin moving the suite to an isolated database, or a host
        # application binding its own connection. Rebinding here would move
        # the shared pool behind that caller's back (and, when REDIS_URL
        # names database 0, would do so on the exact path #584 refuses), so
        # keep the swapped connection and judge the refusal against it.
        live_db = int(
            POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db", 0) or 0
        )
        if live_db != target_db:
            if live_db == 0 and not config.allow_db0:
                from dataclasses import replace

                # The live connection, not REDIS_URL, is what would be
                # written to -- word the refusal accordingly.
                raise Db0RefusedError(
                    _db0_refusal_message(replace(config, url_source="default"))
                )
            return False

    # This call rebinds the pool to ``config.url``, so the refusal must
    # judge that URL -- "this call will rebind" and "this URL's database"
    # are one decision. Judging the live connection here let a swapped-in
    # connection on another database mask a REDIS_URL naming database 0,
    # and the rebind below then silently moved the pool there.
    if target_db == 0 and not config.allow_db0:
        raise Db0RefusedError(_db0_refusal_message(config))

    client = POPOTO_REDIS_DB
    current = dict(client.connection_pool.connection_kwargs)
    target_matches = all(current.get(key) == value for key, value in wanted.items())
    if target_matches and current.get("socket_timeout") == HOOK_SOCKET_TIMEOUT_SECONDS:
        return False

    current.update(wanted)
    # The hook sits on the user's prompt path. One short attempt, no
    # retries: a hung server costs about a second, and the harness runs
    # the hook again next turn. redis-py's default is 5 s per attempt with
    # up to 10 retries, which turned one hung server into a 25 s stall.
    current["socket_timeout"] = HOOK_SOCKET_TIMEOUT_SECONDS
    current["socket_connect_timeout"] = HOOK_SOCKET_TIMEOUT_SECONDS
    current["retry"] = Retry(NoBackoff(), 0)
    old_pool = client.connection_pool
    client.connection_pool = redis.ConnectionPool(
        connection_class=old_pool.connection_class, **current
    )
    try:
        old_pool.disconnect()
    except Exception:
        pass
    return True
