"""Hermes plugin for Popoto subconscious memory.

Install to ``~/.hermes/plugins/popoto-memory/`` (copy this file and
``plugin.yaml``), then run ``hermes plugins enable popoto-memory`` --
plugins are opt-in via ``plugins.enabled`` in ``~/.hermes/config.yaml``
(``hermes_cli/config.py:749-751``, read at ``plugins.py:256-270``). A
plugin on disk but absent from that list is recorded as ``enabled=False``
and never loaded.

Hermes is the only harness in scope whose hooks are Python, so this runs
in-process: no subprocess, no interpreter startup, just the Redis round
trip. The service is built once and reused for the life of the gateway.

Hermes's plugin manager calls a registered callback synchronously
(``ret = cb(**kwargs)``, ``hermes_cli/plugins.py:1911-1927``, never
awaited) -- an ``async def`` callback would return an un-awaited coroutine
that is silently dropped. Both callbacks here are therefore plain ``def``
and take ``**kwargs`` only, since the manager injects
``telemetry_schema_version`` unconditionally and any future kwarg would be
a ``TypeError`` on a positional signature.

The exact invoke-site kwargs (read from the installed hermes-agent==0.19.0
package):

    pre_llm_call  (agent/turn_context.py:692-703):
        session_id, task_id, turn_id, user_message, conversation_history,
        is_first_turn, model, platform, sender_id
    post_llm_call (agent/turn_finalizer.py:483-494):
        session_id, task_id, turn_id, user_message, assistant_response,
        conversation_history, model, platform

Plugin hooks carry no ``cwd`` at all -- only the separate *shell*-hook
subsystem serializes one. ``MemoryConfig.from_env()`` therefore falls back
to the Hermes process's own ``os.getcwd()``, which for a long-lived gateway
is wherever the operator started it: stable but arbitrary. Set
``POPOTO_MEMORY_AGENT_ID`` explicitly rather than relying on that fallback.
"""

import json
import os

_SERVICE = None


def _service():
    """Build the memory service once and keep it.

    Deferred rather than done at import so a Redis that is not up yet at
    gateway start does not stop the plugin from loading. Hermes's plugin
    callbacks are synchronous but a gateway serving multiple sessions may
    run turns on separate threads, so two first-ever calls could both see
    ``_SERVICE is None``. No lock is needed: the loser's ``MemoryService``
    is simply discarded, and construction is idempotent with respect to the
    Redis binding (it never rebinds an existing connection without an
    explicit URL).
    """
    global _SERVICE
    if _SERVICE is None:
        from popoto.integrations import MemoryService

        _SERVICE = MemoryService()
    return _SERVICE


def _envelope(hook_event_name, kwargs):
    """Build the JSON-able envelope ``hooks.handle_payload`` reads.

    Forwards every scalar kwarg verbatim and synthesizes ``hook_event_name``
    (``normalize()`` needs an event name and the invoke-site kwargs carry
    none). ``conversation_history`` is dropped: it is unbounded, unread by
    the adapter, and committing it into a fixture would turn a contract
    document into a transcript.
    """
    envelope = {"hook_event_name": hook_event_name}
    for key, value in kwargs.items():
        if key == "conversation_history":
            continue
        envelope[key] = value
    return envelope


def _tee(name, envelope):
    """Mirror the envelope to ``$POPOTO_HOOK_CAPTURE/<name>.json`` when set.

    This is how the committed fixtures under ``tests/fixtures/harness_payloads/``
    are produced: they are what this function wrote while driven through the
    real ``hermes_cli.plugins.invoke_hook`` dispatcher, not a transcription of
    vendor documentation.
    """
    directory = os.environ.get("POPOTO_HOOK_CAPTURE")
    if not directory:
        return
    try:
        with open(os.path.join(directory, f"{name}.json"), "w") as handle:
            json.dump(envelope, handle, indent=2, sort_keys=True, default=str)
    except Exception as exc:  # pragma: no cover - best-effort diagnostics
        from popoto.integrations.hooks import _log_hook_error

        _log_hook_error("hermes_tee", exc)


def _on_pre(**kwargs):
    """Registered for ``pre_llm_call``. Returns ``{"context": "..."}`` or ``None``.

    A memory failure must never break the user's turn -- Hermes swallows the
    exception a second time (per-callback in the manager, then again at the
    invoke site), so a silent popoto plugin would be indistinguishable from
    an absent one. The failure is therefore always logged before returning
    ``None``.
    """
    envelope = _envelope("pre_llm_call", kwargs)
    _tee("pre_llm_call", envelope)
    try:
        from popoto.integrations import hooks

        output = hooks.handle_payload(envelope, service=_service())
    except Exception as exc:
        from popoto.integrations.hooks import _log_hook_error

        _log_hook_error("hermes_pre_llm_call", exc)
        return None
    if not output:
        return None
    return json.loads(output)


def _on_post(**kwargs):
    """Registered for ``post_llm_call``. Return value is discarded by Hermes."""
    envelope = _envelope("post_llm_call", kwargs)
    _tee("post_llm_call", envelope)
    try:
        from popoto.integrations import hooks

        hooks.handle_payload(envelope, service=_service())
    except Exception as exc:
        from popoto.integrations.hooks import _log_hook_error

        _log_hook_error("hermes_post_llm_call", exc)
    return None


def register(ctx):
    """Hermes plugin entry point, called once at plugin load."""
    ctx.register_hook("pre_llm_call", _on_pre)
    ctx.register_hook("post_llm_call", _on_post)
