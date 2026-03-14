# Agent Memory DX: Best Practices Mapping

**Goal:** Define what ideal developer experience looks like when using Popoto Agent Memory primitives from AI agent code — specifically PydanticAI and Claude Agent SDK.

**Principle:** Popoto is the storage layer. It should feel like using any ORM from within agent tools — no special adapters, no framework coupling. If the DX is right, a developer defines models, calls `.save()` and `.query` in their tool functions, and everything just works.

---

## 1. Model Definition (Framework-Agnostic)

This is pure Popoto — identical regardless of which agent framework wraps it.

```python
# models/memory.py
from popoto import Model, KeyField, Field
from popoto.fields import AutoKeyField, DecayingSortedField
from popoto.fields.constants import InteractionWeight

class AgentMemory(Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = Field(type=str)
    source = Field(type=str, default="agent")        # "human" | "agent" | "system"
    importance = Field(type=float, default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
```

**DX goal:** This looks like any Django/Popoto model. No agent-specific imports. A developer who has never built an agent can read this and understand it.

---

## 2. PydanticAI Integration

### 2a. Deps carry agent identity, not memory instances

```python
from dataclasses import dataclass

@dataclass
class AgentDeps:
    agent_id: str
    user_role: str = "peer"        # for InteractionWeight
    source: str = "agent"          # "human" | "agent" | "system"
```

**Why:** Popoto models are global (like Django models). No need to pass a "memory store" instance — just pass the agent_id so tools know whose memories to read/write.

### 2b. Memory as tools — store and recall

```python
from pydantic_ai import Agent, RunContext
from models.memory import AgentMemory
from popoto.fields.constants import InteractionWeight

agent = Agent(
    "anthropic:claude-sonnet-4-20250514",
    deps_type=AgentDeps,
    system_prompt="You are a helpful assistant with persistent memory.",
)

@agent.tool
async def remember(ctx: RunContext[AgentDeps], content: str, importance: float = 1.0) -> str:
    """Store a memory for future recall."""
    AgentMemory(
        agent_id=ctx.deps.agent_id,
        content=content,
        source=ctx.deps.source,
        importance=importance,
    ).save()
    return f"Remembered."

@agent.tool
async def recall(ctx: RunContext[AgentDeps], limit: int = 5) -> str:
    """Retrieve the most relevant memories, ranked by recency and importance."""
    memories = (
        AgentMemory.query
        .filter(agent_id=ctx.deps.agent_id)
        .top_by_decay(limit)
    )
    if not memories:
        return "No memories found."
    return "\n".join(f"- {m.content}" for m in memories)
```

### 2c. Memory in dynamic instructions (context injection)

```python
@agent.instructions
async def inject_memory_context(ctx: RunContext[AgentDeps]) -> str:
    """Inject top memories into every conversation turn."""
    memories = (
        AgentMemory.query
        .filter(agent_id=ctx.deps.agent_id)
        .top_by_decay(5)
    )
    if not memories:
        return ""
    lines = "\n".join(f"- {m.content} (importance: {m.importance})" for m in memories)
    return f"Your most relevant memories:\n{lines}"
```

### 2d. Running the agent

```python
async def main():
    deps = AgentDeps(agent_id="support-bot-1", source="human", user_role="peer")
    result = await agent.run("What do you remember about deployment procedures?", deps=deps)
    print(result.output)
```

**DX takeaway:** Memory is just another data source accessed through tools and instructions. No special memory middleware. No framework adapter. Just Popoto queries inside PydanticAI tool functions.

---

## 3. Claude Agent SDK Integration

### 3a. Memory in system prompt construction

```python
from models.memory import AgentMemory

class MemoryAwareAgent(ValorAgent):
    def __init__(self, agent_id: str, **kwargs):
        self.agent_id = agent_id
        super().__init__(**kwargs)

    def _build_memory_context(self) -> str:
        """Fetch top memories and format for system prompt."""
        memories = (
            AgentMemory.query
            .filter(agent_id=self.agent_id)
            .top_by_decay(10)
        )
        if not memories:
            return ""
        lines = "\n".join(f"- {m.content}" for m in memories)
        return f"\n---\nRELEVANT MEMORIES:\n{lines}\n---"

    def _create_options(self, session_id=None):
        options = super()._create_options(session_id)
        # Append memory context to system prompt
        options.system_prompt += self._build_memory_context()
        return options
```

### 3b. Memory in hooks (post-tool persistence)

```python
from claude_agent_sdk import HookContext, PostToolUseHookInput

async def memory_hook(
    input_data: PostToolUseHookInput,
    tool_use_id: str | None,
    context: HookContext,
) -> dict:
    """After certain tool calls, persist observations as memories."""
    tool_name = input_data.get("tool_name", "")
    tool_output = input_data.get("tool_output", "")

    # Example: remember search results
    if tool_name == "WebSearch" and tool_output:
        agent_id = context.env.get("AGENT_ID", "default")
        AgentMemory(
            agent_id=agent_id,
            content=f"Search result: {tool_output[:500]}",
            source="system",
            importance=InteractionWeight.SYSTEM,
        ).save()

    return {}  # Don't modify tool output
```

### 3c. Memory in sub-agent definitions

```python
# The memory-aware sub-agent gets memory tools via its prompt
researcher = AgentDefinition(
    description="Research agent with persistent memory",
    prompt="""You have access to persistent memory.
    Use the remember() tool to store important findings.
    Use the recall() tool before starting new research to check what you already know.
    """,
    tools=None,  # Inherits all tools including memory tools
)
```

**DX takeaway:** In Agent SDK, memory integrates at two levels: (1) system prompt injection before each session, and (2) hooks that automatically persist observations. The agent doesn't need explicit memory tools — hooks handle persistence transparently.

---

## 4. Common Patterns (Both Frameworks)

### 4a. Source weighting for multi-agent teams

```python
# Human gives a directive — persists for months
AgentMemory(
    agent_id="pm-bot",
    content="Focus on enterprise features this quarter",
    source="human",
    importance=InteractionWeight.combine(InteractionWeight.HUMAN, InteractionWeight.EXECUTIVE),
).save()

# Agent observes something — moderate persistence
AgentMemory(
    agent_id="pm-bot",
    content="API latency increased 40% after last deploy",
    source="agent",
    importance=InteractionWeight.combine(InteractionWeight.AGENT, InteractionWeight.PEER),
).save()

# System logs something — short-lived
AgentMemory(
    agent_id="pm-bot",
    content="Deployed version 2.4.1",
    source="system",
    importance=InteractionWeight.SYSTEM,
).save()
```

### 4b. Refreshing memories on access (touch)

```python
@agent.tool
async def recall_and_refresh(ctx: RunContext[AgentDeps], query: str, limit: int = 5) -> str:
    """Recall memories and refresh their timestamps (they stay relevant longer)."""
    memories = (
        AgentMemory.query
        .filter(agent_id=ctx.deps.agent_id)
        .top_by_decay(limit)
    )
    for m in memories:
        m.touch("relevance")  # Reset decay clock
    return "\n".join(f"- {m.content}" for m in memories)
```

### 4c. Different decay rates for different retrieval contexts

```python
# "What happened recently?" — aggressive decay, only very fresh memories
hot = AgentMemory.query.filter(agent_id="bot-1").top_by_decay(5, decay_rate=1.0)

# "What do you know about X?" — gentle decay, long-term knowledge
deep = AgentMemory.query.filter(agent_id="bot-1").top_by_decay(10, decay_rate=0.2)

# "What's most important overall?" — default decay with importance weighting
balanced = AgentMemory.query.filter(agent_id="bot-1").top_by_decay(10)
```

### 4d. Temporal rhythms with CyclicDecayField

```python
from popoto import Model, KeyField, Field, CyclicDecayField
from popoto.fields.constants import TemporalPeriod

class RecurringDirective(Model):
    directive_id = KeyField()
    agent_id = KeyField()
    content = Field(type=str)
    urgency = CyclicDecayField(
        decay_rate=0.3,
        cycles=[
            (TemporalPeriod.QUARTERLY, 5.0, 0),  # boost every quarter
        ],
        partition_by="agent_id",
    )
```

```python
@agent.tool
async def check_directives(ctx: RunContext[AgentDeps]) -> str:
    """Check which directives need attention based on cyclic pressure."""
    directives = (
        RecurringDirective.query
        .filter(agent_id=ctx.deps.agent_id)
        .top_by_decay(5)
    )
    urgent = [d for d in directives if d.urgency.resolve_pressure() > 3.0]
    if not urgent:
        return "No urgent directives."
    return "\n".join(f"- {d.content}" for d in urgent)
```

### 4e. Read tracking with AccessTrackerMixin

```python
from popoto import Model, KeyField, Field, DecayingSortedField, AccessTrackerMixin

class TrackedMemory(AccessTrackerMixin, Model):
    memory_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField()
```

```python
# Bulk operations: suppress read tracking with no_track()
all_memories = TrackedMemory.query.filter().no_track().all()

# Staged/confirmed read pattern in observation context
memory = TrackedMemory.query.get(memory_id="abc123")  # stages access
memory.confirm_access()     # mark as actually used by agent
# or
memory.discard_staged_access()  # agent loaded but didn't use it
```

---

## 5. What Popoto Must Get Right (DX Requirements)

These are the non-negotiable DX properties that flow from the usage patterns above:

### 5a. Zero-config timestamps
`DecayingSortedField` must auto-set `time.time()` on save. The developer should never have to manually set a timestamp. This is forced internally via `auto_now=True`.

### 5b. `top_by_decay()` returns model instances
Not raw Redis keys, not scores. Full model instances, pre-loaded, ordered by decayed score. The developer chains `.top_by_decay(N)` like they chain `.limit(N)`.

### 5c. Pipeline-safe everything
Every operation (`save()`, `touch()`, `top_by_decay()`) must accept an optional `pipeline` parameter. Agent frameworks batch operations — breaking pipeline support breaks atomicity.

### 5d. `touch()` is one line
`instance.touch("field_name")` — updates the sorted set timestamp without re-saving the full model. No extra queries, no load-then-save dance.

### 5e. Query-time parameter overrides
`decay_rate` and `base_score_field` set at field definition are defaults. Every query can override them: `.top_by_decay(10, decay_rate=1.0)`. Different retrieval contexts need different decay curves.

### 5f. `partition_by` just works
If `partition_by="agent_id"`, then `.filter(agent_id="X").top_by_decay(10)` queries only that agent's sorted set. No extra configuration. Inherited from `SortedFieldMixin`.

### 5g. InteractionWeight is importable and simple
`from popoto.fields.constants import InteractionWeight` — a plain class with float constants and a `combine()` staticmethod. No metaclass magic, no registration. Just floats.

### 5h. Sync API (no async required)
Popoto operations are sync (redis-py is sync). PydanticAI tools can be sync or async. Agent SDK hooks are async but can call sync code. Popoto should stay sync — the agent framework handles the async boundary.

### 5i. `no_track()` suppresses read tracking
`Model.query.filter(...).no_track().all()` suppresses `on_read()` for `AccessTrackerMixin` models. Essential for bulk operations, analytics queries, and internal tooling that shouldn't inflate access counts.

### 5j. `field_name` auto-detection in `top_by_decay()`
When a model has exactly one `DecayingSortedField` (or subclass like `CyclicDecayField`), `top_by_decay(n)` works without specifying the field name. When multiple exist, an explicit `field_name` is required — raises `QueryException` otherwise.

---

## 6. What Popoto Should NOT Do

### 6a. No framework adapters
No `PopotoMemoryProvider(framework="pydantic_ai")`. Popoto is an ORM. The agent framework calls it like any other database.

### 6b. No automatic context injection
Popoto doesn't know about LLM context windows. It returns model instances. The developer (or their agent framework) decides what goes into the prompt.

### 6c. No agent-specific field types
No `AgentMemoryField`. The primitives are generic: `DecayingSortedField`, `ConfidenceField`, `CoOccurrenceField`. They work for agent memory, recommendation engines, caching layers, or anything else that needs time-weighted scoring.

### 6d. No opinion on async
Popoto uses redis-py (sync). Don't add async variants. Agent frameworks handle the async boundary with `run_in_executor` or by allowing sync tools. Adding async would double the API surface for no benefit.

---

## 7. Testing DX

### 7a. Model tests are pure Popoto

```python
def test_memory_decay_ordering():
    """Recent memories rank higher than old ones."""
    old = AgentMemory(agent_id="test", content="old", importance=1.0)
    old.save()
    time.sleep(0.1)
    new = AgentMemory(agent_id="test", content="new", importance=1.0)
    new.save()

    results = AgentMemory.query.filter(agent_id="test").top_by_decay(2)
    assert results[0].content == "new"
    assert results[1].content == "old"
```

### 7b. Integration tests use real Redis (not mocks)

```python
def test_importance_weighting():
    """High-importance memories outlast low-importance ones."""
    AgentMemory(agent_id="test", content="important", importance=10.0).save()
    AgentMemory(agent_id="test", content="routine", importance=1.0).save()

    # Both just saved — but importance should still affect ranking
    results = AgentMemory.query.filter(agent_id="test").top_by_decay(2)
    assert results[0].content == "important"
```

### 7c. Agent integration tests are framework-level

```python
async def test_agent_remembers_and_recalls():
    """PydanticAI agent can store and retrieve memories."""
    deps = AgentDeps(agent_id="test-agent")

    # Store via tool
    await agent.run("Remember that the deploy key is in vault", deps=deps)

    # Recall via tool
    result = await agent.run("What do you know about deploy keys?", deps=deps)
    assert "vault" in result.output.lower()
```

---

## 8. Import Map

What the developer imports and from where:

```python
# Models (standard Popoto)
from popoto import Model, KeyField, Field
from popoto import DecayingSortedField, CyclicDecayField, AccessTrackerMixin

# Constants
from popoto import InteractionWeight, TemporalPeriod
# or directly: from popoto.fields.constants import InteractionWeight, TemporalPeriod

# Everything else is standard Python / framework code
from pydantic_ai import Agent, RunContext          # if using PydanticAI
from claude_agent_sdk import ClaudeAgentOptions    # if using Agent SDK
```

**No new top-level packages.** No `popoto.agent_memory`. No `popoto.integrations.pydantic_ai`. Just fields and constants in the existing namespace.

---

## Summary

The ideal DX is invisible infrastructure:

1. **Define models** with `DecayingSortedField` — looks like any Popoto model
2. **Query with `top_by_decay()`** — looks like any Popoto query
3. **Use from agent tools** — looks like any database call from a tool function
4. **Weight by source/role** — `InteractionWeight.combine()` returns a float
5. **Override at query time** — `decay_rate` and `base_score_field` per query

The developer's mental model: "Popoto gives me time-weighted sorted sets. I use them from my agent tools like I'd use any ORM."
