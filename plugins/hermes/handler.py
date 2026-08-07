"""Hermes hook handler for Popoto subconscious memory.

Hermes is the only harness in scope whose hooks are Python, so this runs
in-process: no subprocess, no interpreter startup, just the Redis round trip.
The service is built once and reused for the life of the gateway.

Copy this file and ``HOOK.yaml`` into ``~/.hermes/hooks/popoto-memory/``.
"""

_SERVICE = None


def _service():
    """Build the memory service once and keep it.

    Deferred rather than done at import so a Redis that is not up yet at
    gateway start does not stop the hook from loading.
    """
    global _SERVICE
    if _SERVICE is None:
        from popoto.integrations import MemoryService

        _SERVICE = MemoryService()
    return _SERVICE


async def handle(event_type, context):
    """Dispatch one Hermes hook event.

    Args:
        event_type: ``"pre_llm_call"`` or ``"post_llm_call"``.
        context: The Hermes event mapping. ``extra.user_message`` carries the
            prompt on the read event; ``extra.response`` carries the model's
            text on the write event.

    Returns:
        ``{"context": "..."}`` on the read path when there is something to
        inject, otherwise ``None``. Hermes places the returned context in the
        user message rather than the system prompt, which is what keeps the
        cached prefix intact across turns.
    """
    from popoto.integrations import hooks

    payload = dict(context or {})
    payload.setdefault("event_type", event_type)

    try:
        output = hooks.handle_payload(payload, service=_service())
    except Exception:
        # A memory failure must never break the user's turn. The reason is
        # already in ~/.popoto/memory.log and in `popoto-memory doctor`.
        return None

    if not output:
        return None

    import json

    return json.loads(output)
