"""Agent-harness integration: subconscious memory for Claude Code and friends.

**Hooks recall and capture; MCP tools search, save, and correct.** That
split is the whole design, and it is the thing not to "simplify" later.

Subconscious memory means the memory layer runs on every turn whether or
not the model asks for it. MCP tools cannot deliver that, because they are
agent-elected: the model calls a tool when it decides to, so an MCP-only
memory system is instructed memory wearing a subconscious label. Every
harness in scope exposes a deterministic per-turn hook surface instead --
Claude Code and Codex ``UserPromptSubmit``/``Stop``, Hermes
``pre_llm_call``/``post_llm_call``, OpenClaw ``before_prompt_build``/
``llm_output`` -- and that is where recall and capture live. The MCP server
covers the discretionary half and the clients that have no hook surface.

If a change here makes recall depend on the model calling ``memory_search``,
that change has broken the feature, not simplified it.

Layout:

:mod:`popoto.integrations.service`
    :class:`~popoto.integrations.service.MemoryService`, the shared core
    over :class:`popoto.recipes.DefaultMemory`. Returns a context *string*;
    it never mutates a message array or a system prompt.
:mod:`popoto.integrations.config`
    Environment and cwd resolution. Every setting is optional.
:mod:`popoto.integrations.hooks`
    Harness payload in, harness payload out.
:mod:`popoto.integrations.mcp_server`
    Stdio MCP server, four frozen tool names. Requires ``popoto[mcp]``.
:mod:`popoto.integrations.cli`
    The ``popoto-memory`` console entry point:
    ``hook`` | ``mcp`` | ``doctor`` | ``demo``.

Nothing heavy is imported at module scope. The read hook is synchronous and
on the critical path of every turn, so ``redis``, the memory model, and the
MCP SDK are all imported inside the function that first needs them.

Example::

    from popoto.integrations import MemoryService

    service = MemoryService()
    context = service.assemble("how do we deploy?", session_id="s1")
    service.capture("Deploys are blue-green with automatic rollback", "s1")
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .config import MemoryConfig
    from .service import MemoryService

__all__ = ["MemoryService", "MemoryConfig"]


def __getattr__(name: str) -> Any:
    """Resolve the two public names lazily.

    Keeps ``import popoto.integrations`` free of ``redis`` and the memory
    model, which is what makes the hook's cold start affordable.
    """
    if name == "MemoryService":
        from .service import MemoryService

        return MemoryService
    if name == "MemoryConfig":
        from .config import MemoryConfig

        return MemoryConfig
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
