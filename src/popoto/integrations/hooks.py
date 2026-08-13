"""Harness hook adapter: normalize a payload, call the service, emit a payload.

This is the subconscious half of the integration. A hook fires because the
harness reached a point in its own turn loop, not because the model decided
to call something, which is the only way recall and capture can happen on
*every* turn.

Claude Code and Codex have converged on the same hook contract: JSON on
stdin carrying ``hook_event_name``, ``UserPromptSubmit`` before the model
sees the prompt, ``Stop`` after the turn with the assistant's text in
``last_assistant_message``, and ``hookSpecificOutput.additionalContext`` as
the injection channel. One executable dispatching on ``hook_event_name``
therefore serves both harnesses with no per-harness code. Hermes and
OpenClaw send different field names for the same four facts, so they are
handled by normalizing their payloads into the same shape.

Output rules that are not negotiable:

* **Build the response as one string and write it once.** Codex treats
  stdout that starts with ``{`` but fails to parse as a hook failure, so a
  partial write is worse than no write.
* **Emit nothing when there is nothing to inject.** No empty
  ``additionalContext``, no empty header.
* **Always exit 0.** A memory failure must never fail a user's turn.
"""

import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("POPOTO.integrations.hooks")

READ_EVENTS = frozenset(
    {
        "UserPromptSubmit",
        "user_prompt_submit",
        "pre_llm_call",
        "before_prompt_build",
        "agent_turn_prepare",
    }
)
"""Events that inject context. Claude Code only allows injection from
``UserPromptSubmit``, ``UserPromptExpansion``, and ``SessionStart``; every
other event's stdout goes to the debug log. Codex matches. The remaining
names are the Hermes and OpenClaw equivalents."""

WRITE_EVENTS = frozenset(
    {
        "Stop",
        "SubagentStop",
        "stop",
        "post_llm_call",
        "llm_output",
        "agent_end",
    }
)
"""Events that capture the turn. All of them carry the assistant's final
text, so no transcript file is ever parsed and no JSONL schema change can
break capture."""

_QUERY_FIELDS = ("prompt", "user_message", "user_prompt", "message", "query")
_RESPONSE_FIELDS = (
    "last_assistant_message",
    "assistant_message",
    "response",
    "output",
    "text",
)


class NormalizedEvent:
    """A harness payload reduced to the four facts the service needs.

    Attributes:
        event: The raw ``hook_event_name`` as sent by the harness.
        kind: ``"read"``, ``"write"``, or ``"ignore"``.
        text: The prompt text on a read event, the assistant's final message
            on a write event.
        session_id: Harness session identifier, or ``None``.
        cwd: Working directory the harness reports, used to derive the
            default agent id. The harness's cwd is more accurate than the
            hook process's own, which some harnesses do not set.
    """

    __slots__ = ("event", "kind", "text", "session_id", "cwd")

    def __init__(
        self,
        event: str,
        kind: str,
        text: str,
        session_id: Optional[str],
        cwd: Optional[str],
    ):
        self.event = event
        self.kind = kind
        self.text = text
        self.session_id = session_id
        self.cwd = cwd


def _first_string(payload: Dict[str, Any], names: Tuple[str, ...]) -> str:
    """Return the first non-empty string among ``names``, searching one level
    into an ``extra``/``context``/``data`` sub-object (Hermes nests there)."""
    sources = [payload]
    for nested in ("extra", "context", "data", "input"):
        value = payload.get(nested)
        if isinstance(value, dict):
            sources.append(value)
    for source in sources:
        for name in names:
            value = source.get(name)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def normalize(payload: Dict[str, Any]) -> NormalizedEvent:
    """Reduce any supported harness payload to a :class:`NormalizedEvent`.

    This is the single point where a harness schema is read. A change to any
    harness's payload shape touches this function and nothing else.

    Args:
        payload: The decoded hook JSON.

    Returns:
        A :class:`NormalizedEvent`; ``kind`` is ``"ignore"`` for events this
        integration does not handle, which is most of them.
    """
    event = ""
    for name in ("hook_event_name", "hookEventName", "event", "event_type", "type"):
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            event = value.strip()
            break

    session_id = None
    for name in ("session_id", "sessionId", "conversation_id"):
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            session_id = value.strip()
            break

    cwd = None
    for name in ("cwd", "working_directory", "workspace"):
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            cwd = value.strip()
            break

    if event in READ_EVENTS:
        return NormalizedEvent(
            event, "read", _first_string(payload, _QUERY_FIELDS), session_id, cwd
        )
    if event in WRITE_EVENTS:
        return NormalizedEvent(
            event, "write", _first_string(payload, _RESPONSE_FIELDS), session_id, cwd
        )
    return NormalizedEvent(event, "ignore", "", session_id, cwd)


def render_context(event: NormalizedEvent, context: str) -> Dict[str, Any]:
    """Wrap assembled context in the harness's own response shape.

    Claude Code and Codex read ``hookSpecificOutput.additionalContext``.
    Hermes reads ``context``. OpenClaw reads ``appendContext``. All three
    place the text in the *user* turn rather than the system prompt, which
    is what keeps the cached system prefix intact across turns.

    Args:
        event: The normalized event, whose raw ``event`` name selects the
            response shape.
        context: The assembled context block. Must be non-empty; callers
            emit nothing at all when it is empty.

    Returns:
        The response object to serialize.
    """
    if event.event in ("pre_llm_call",):
        return {"context": context}
    if event.event in ("before_prompt_build", "agent_turn_prepare"):
        return {"appendContext": context}
    return {
        "hookSpecificOutput": {
            "hookEventName": event.event,
            "additionalContext": context,
        }
    }


def handle_payload(payload: Dict[str, Any], service: Any = None) -> Optional[str]:
    """Run one hook event end to end.

    Args:
        payload: The decoded hook JSON.
        service: An optional :class:`~popoto.integrations.service.MemoryService`.
            Defaults to one built from the environment and the payload's
            ``cwd``. Passing one is how the in-process Hermes handler and
            the tests avoid rebuilding the service per event.

    Returns:
        The exact string to write to stdout, or ``None`` when the hook must
        stay silent. The caller writes it in a single ``write`` call.
    """
    event = normalize(payload)
    if event.kind == "ignore":
        return None

    if service is None:
        from .config import MemoryConfig
        from .service import MemoryService

        service = MemoryService(MemoryConfig.from_env(os.environ, cwd=event.cwd))

    if not service.config.enabled:
        return None

    if event.kind == "read":
        context = service.assemble(event.text, session_id=event.session_id)
        if not context.strip():
            return None
        return json.dumps(render_context(event, context))

    service.capture(event.text, session_id=event.session_id)
    if event.session_id:
        # A hook fires on every turn and cannot know whether a surfaced
        # memory actually influenced the response, so it must not claim
        # "acted" (which strengthens ConfidenceField/decay clocks on every
        # turn and defeats decay entirely). "used" only confirms the staged
        # read. See fields/observation.py for the outcome effects matrix.
        service.feedback(event.session_id, outcome="used")
    return None


def run(stdin_text: str, service: Any = None) -> Optional[str]:
    """Decode a raw stdin body and handle it.

    Args:
        stdin_text: The raw bytes the harness wrote to the hook's stdin,
            already decoded to ``str``.
        service: Optional service override, as for :func:`handle_payload`.

    Returns:
        The stdout string, or ``None``. Malformed JSON returns ``None``
        rather than raising: an unparseable payload is a harness problem,
        and crashing would surface it as a failed turn.
    """
    if not stdin_text or not stdin_text.strip():
        return None
    try:
        payload = json.loads(stdin_text)
    except (ValueError, TypeError):
        _log_hook_error(
            "hook_decode", ValueError(f"unparseable stdin ({len(stdin_text)} bytes)")
        )
        return None
    if not isinstance(payload, dict):
        _log_hook_error(
            "hook_decode", ValueError(f"stdin was {type(payload).__name__}, not object")
        )
        return None
    try:
        return handle_payload(payload, service=service)
    except Exception as exc:
        # Covers a misconfigured POPOTO_MEMORY_URL, which raises out of
        # MemoryService construction. Exiting 0 keeps the turn alive; the
        # log line is what stops it from being invisible.
        _log_hook_error("hook_run", exc)
        return None


def _log_hook_error(operation: str, exc: BaseException) -> None:
    """Append one failure line to the configured log file.

    Writes the file directly rather than going through
    ``MemoryService._record_failure``. Constructing a service is exactly
    what may have just failed, and it would also open a Redis connection
    only to record that Redis is unusable.
    """
    from datetime import datetime, timezone

    from .config import MemoryConfig

    logger.warning("popoto memory %s failed: %s", operation, exc)
    try:
        path = MemoryConfig.from_env(os.environ).log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {operation} {type(exc).__name__}: {exc}\n")
    except Exception:
        pass
