---
status: In Progress
type: feature
appetite: Medium
owner: Valor
created: 2026-03-23
tracking: https://github.com/tomcounsell/popoto/issues/264
---

# Update Guide Docs, Add Example Tests, Design Subconscious Memory Recipe

## Problem

Three gaps exist in our published documentation and recipes:

1. **Incomplete LLM integration examples**: The quickstart guide (Level 5) shows ContextAssembler output injected into a system prompt string but never shows an actual LLM call. Developers must guess how to wire Popoto memory into a real `client.chat.completions.create()` call. The `context-assembler.md` feature doc has the same gap.

2. **Guide code not tested verbatim**: `tests/test_adoption_ladder.py` and `tests/test_policy_cache.py` validate the same concepts as the guides but do not mirror the published code blocks exactly. If a guide example drifts from the real API, nothing catches it until a developer copy-pastes and gets an error.

3. **No subconscious memory recipe**: The epistemic flow doc describes "silent context injection" and "ambient monitoring" as the north star architecture, but there is no runnable implementation. Developers who want automatic memory injection/extraction around LLM turns must build the orchestration layer from scratch.

**Desired outcome:**
- Every code block in the quickstart and recipe guides is proven correct by CI
- Level 5 shows a complete save-assemble-call-LLM loop using OpenAI SDK v1+
- A new subconscious memory recipe demonstrates automatic memory read/write around each LLM turn, using PydanticAI for the agent framework and Popoto for the memory layer

## Prior Art

- `docs/guides/agent-memory-quickstart.md` -- 5-level adoption ladder, Level 5 stops at string formatting
- `docs/guides/policy-cache-recipe.md` -- recipe guide, no LLM call examples
- `docs/features/context-assembler.md` -- feature doc, ends at `assemble()` result
- `docs/guides/epistemic-flow-cognitive-agent-architectures.md` -- architectural vision doc describing silent injection, Thalamic Gate, ambient monitoring
- `tests/test_adoption_ladder.py` -- concept-level tests for Levels 1-5
- `tests/test_policy_cache.py` -- concept-level tests for PolicyCache
- `src/popoto/recipes/context_assembler.py` -- ContextAssembler implementation
- `src/popoto/recipes/policy_cache.py` -- PolicyCache implementation

## Architectural Impact

- **New dependencies**: `openai` and `pydantic-ai` as optional dependencies (used in examples/recipes only, not in core Popoto)
- **Interface changes**: None to core ORM. New recipe file in `src/popoto/recipes/`
- **Coupling**: Zero coupling to core ORM -- subconscious memory recipe is pure composition of existing primitives
- **Data ownership**: Uses the same Memory model patterns from the quickstart guide
- **Reversibility**: Trivial -- recipe, guide, and test files are additive

## Appetite

**Size:** Medium

Three distinct workstreams (guide updates, verbatim tests, new recipe) but each is well-scoped. The subconscious memory recipe is the largest piece but follows established patterns from PolicyCache.

## Prerequisites

| Requirement | Purpose |
|-------------|---------|
| All 14 memory primitives shipped | Recipe composes existing primitives |
| ContextAssembler shipped | Core of the injection pipeline |
| Redis running on localhost:6379 | Tests require Redis |

## Solution

### Part 1: Update Guide Code Examples

**Files to modify:**

- `docs/guides/agent-memory-quickstart.md` -- Add complete LLM call example at Level 5
- `docs/features/context-assembler.md` -- Add end-to-end LLM integration snippet

**What changes:**

Level 5 of the quickstart currently ends at:
```python
system_prompt = f"You are a helpful assistant.\n\nRelevant context:\n{result.formatted}"
```

Add a complete example showing the full loop:
```python
from openai import OpenAI

client = OpenAI()  # uses OPENAI_API_KEY env var

# Assemble memory context
result = assembler.assemble(query_cues={"topic": "deployment"}, agent_id="agent-1")

# Build messages with injected memory
messages = [
    {"role": "system", "content": f"You are a helpful assistant.\n\nRelevant context:\n{result.formatted}"},
    {"role": "user", "content": "What's our deployment strategy?"},
]

# Call the LLM
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=messages,
)

answer = response.choices[0].message.content

# Report outcomes -- which memories did the agent actually use?
outcome_map = {r.db_key.redis_key: "acted" for r in result.records}
ObservationProtocol.on_context_used(result.records, outcome_map)
```

The `context-assembler.md` feature doc gets a similar end-to-end snippet in a new "LLM Integration" section.

The `policy-cache-recipe.md` does not contain LLM call examples, so no changes needed there.

### Part 2: Verbatim Guide Example Tests

**Files to create:**

- `tests/test_guide_examples.py`

**Approach:**

Each test extracts the core logic from a guide code block and runs it. The test code should match the guide code as closely as possible -- same variable names, same imports, same API calls. Where cleanup or assertions are needed, they are added after the guide code, clearly separated.

**Test structure:**

```
class TestQuickstartLevel1:
    def test_save_and_retrieve(self):
        # --- Code from guide (verbatim) ---
        ...
        # --- Assertions ---
        ...

class TestQuickstartLevel2:
    def test_write_filter_discards_noise(self):
        ...
    def test_high_value_persists(self):
        ...

class TestQuickstartLevel3:
    def test_confidence_and_observation(self):
        ...

class TestQuickstartLevel4:
    def test_link_and_composite_score(self):
        ...

class TestQuickstartLevel5:
    def test_context_assembly(self):
        ...
    def test_llm_integration(self):
        # Tests the LLM call example (mocked -- no real API call in CI)
        ...

class TestPolicyCacheGuide:
    def test_quick_start_example(self):
        ...

class TestContextAssemblerGuide:
    def test_usage_example(self):
        ...
    def test_llm_integration(self):
        ...
```

**Key constraint:** The LLM call examples use `unittest.mock.patch` to mock the OpenAI client. The test proves the Popoto code is correct (assemble, inject, report outcomes) without requiring an API key.

### Part 3: Subconscious Memory Recipe

**Files to create:**

- `src/popoto/recipes/subconscious_memory.py` -- orchestration layer
- `docs/guides/subconscious-memory-recipe.md` -- recipe guide
- `tests/test_subconscious_memory.py` -- recipe tests

**Architecture:**

The subconscious memory recipe wraps a PydanticAI agent (or any chat-completions-compatible client) with automatic memory injection and extraction:

```
User message
    |
    v
[Pre-turn hook: ContextAssembler.assemble() -> inject into messages]
    |
    v
[LLM inference]
    |
    v
[Post-turn hook: extract observations from response -> save as Memory records]
    |
    v
[Outcome hook: report acted/dismissed/contradicted via ObservationProtocol]
    |
    v
Agent response
```

**Core class: `SubconsciousMemory`**

```python
class SubconsciousMemory:
    """Automatic memory injection and extraction around LLM turns.

    Wraps an existing chat flow with:
    - Pre-turn: assemble relevant memories, inject as system context
    - Post-turn: extract facts/observations from LLM response, save as Memory
    - Outcome: report how injected memories were used
    """

    def __init__(
        self,
        model_class,           # Popoto Model class (Level 1-5)
        agent_id: str,
        score_weights: dict,   # passed to ContextAssembler
        max_items: int = 10,
        max_tokens: int = 4000,
        extraction_prompt: str = None,  # system prompt for fact extraction
    ):
        ...

    def inject_context(self, messages: list[dict]) -> tuple[list[dict], AssemblyResult]:
        """Pre-turn: assemble memories and inject into messages array."""
        ...

    def extract_memories(self, response_text: str, importance: float = 0.5) -> list:
        """Post-turn: extract facts from LLM response, save as Memory records."""
        ...

    def report_outcomes(self, assembly_result, outcome: str = "acted"):
        """Outcome hook: report how injected memories were used."""
        ...
```

**Design decisions:**

- The recipe does NOT depend on PydanticAI at the core level. `SubconsciousMemory` works with plain `list[dict]` messages. The guide shows how to wire it into PydanticAI via tool hooks, but the recipe itself is framework-agnostic.
- Memory extraction from LLM responses can use a simple heuristic (sentence splitting + importance scoring) or delegate to a secondary LLM call. The recipe ships with the heuristic approach and documents how to plug in LLM-based extraction.
- The recipe is a composition layer -- it uses ContextAssembler internally and creates Memory records using the standard Popoto save API.

**Test approach for subconscious memory:**

- Test inject_context: verify memories are assembled and injected into the messages array
- Test extract_memories: verify facts are extracted from response text and saved as Memory records
- Test report_outcomes: verify ObservationProtocol is called correctly
- Test full round-trip: inject -> mock LLM call -> extract -> report
- All tests use mocked LLM calls (no real API keys)

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] inject_context with no existing memories returns original messages unchanged
- [ ] extract_memories with empty response text is a no-op
- [ ] report_outcomes with empty assembly result is a no-op
- [ ] Mocked OpenAI client failures do not crash the memory layer

### Empty/Invalid Input Handling
- [ ] Empty messages list passed to inject_context
- [ ] Response text with no extractable facts
- [ ] Assembly result with zero records

## Test Impact

No existing tests are modified. Three new test scenarios:

1. `tests/test_guide_examples.py` -- mirrors guide code blocks verbatim
2. `tests/test_subconscious_memory.py` -- subconscious memory recipe tests
3. Existing `tests/test_adoption_ladder.py` and `tests/test_policy_cache.py` are left unchanged

## Rabbit Holes

- **Real LLM calls in tests**: All LLM interactions are mocked. Do not add OpenAI API key requirements to CI.
- **PydanticAI deep integration**: The recipe shows how to wire into PydanticAI but does not become a PydanticAI plugin. Keep the interface at the `list[dict]` message level.
- **Sophisticated NLP extraction**: The fact extraction heuristic is intentionally simple. LLM-based extraction is documented as an option but not the default.
- **Ambient token monitoring**: The epistemic flow doc describes real-time token stream monitoring. This is a future enhancement -- the recipe operates at the turn level (pre-turn/post-turn hooks), not mid-stream.

## No-Gos (Out of Scope)

- No changes to core Popoto ORM fields or models
- No changes to ContextAssembler or PolicyCache
- No real-time token stream monitoring (turn-level hooks only)
- No PydanticAI dependency in core Popoto (optional, for guide examples only)
- No OpenAI API key required for any test

## Documentation

### Guide Updates
- [ ] `docs/guides/agent-memory-quickstart.md` -- Level 5 LLM call example
- [ ] `docs/features/context-assembler.md` -- LLM integration section

### New Guide
- [ ] `docs/guides/subconscious-memory-recipe.md` -- complete recipe walkthrough

### Inline Documentation
- [ ] Comprehensive docstrings on SubconsciousMemory class and methods
- [ ] Comments explaining the injection/extraction pipeline

## Success Criteria

- [ ] Level 5 of quickstart guide includes a complete OpenAI SDK v1+ LLM call example
- [ ] `docs/features/context-assembler.md` includes an end-to-end LLM integration snippet
- [ ] `tests/test_guide_examples.py` mirrors every code snippet from quickstart and recipe guides verbatim
- [ ] All guide example tests pass in CI
- [ ] New `docs/guides/subconscious-memory-recipe.md` -- complete subconscious memory recipe
- [ ] New `src/popoto/recipes/subconscious_memory.py` -- orchestration layer implementation
- [ ] New `tests/test_subconscious_memory.py` -- recipe tests
- [ ] Recipe demonstrates: auto-inject context before LLM turn, auto-extract memories after LLM turn, outcome reporting

## Step by Step Tasks

### Phase 1: Update Guide Code Examples

- **Task ID**: update-guides
- **Depends On**: none
- **Files Modified**:
  - `docs/guides/agent-memory-quickstart.md` (Level 5 section)
  - `docs/features/context-assembler.md` (new LLM Integration section)
- Add complete OpenAI SDK v1+ example at Level 5 showing: assemble -> build messages -> call LLM -> report outcomes
- Add LLM integration snippet to context-assembler.md
- Verify policy-cache-recipe.md needs no LLM call updates (confirmed: no LLM examples present)

### Phase 2: Create Verbatim Guide Example Tests

- **Task ID**: create-guide-tests
- **Depends On**: update-guides
- **Files Created**:
  - `tests/test_guide_examples.py`
- One test per guide code block, using exact imports and variable names from the guide
- LLM call examples use unittest.mock to mock the OpenAI client
- Redis cleanup fixtures matching the pattern from test_adoption_ladder.py
- Tests cover: quickstart Levels 1-5, policy-cache-recipe Quick Start, context-assembler Usage + LLM integration

### Phase 3: Implement Subconscious Memory Recipe

- **Task ID**: build-subconscious-recipe
- **Depends On**: update-guides (to ensure guide patterns are finalized)
- **Files Created**:
  - `src/popoto/recipes/subconscious_memory.py`
- Implement SubconsciousMemory class with inject_context, extract_memories, report_outcomes
- Use ContextAssembler internally for the injection pipeline
- Simple heuristic for fact extraction (sentence split + importance assignment)
- Document LLM-based extraction as an extensibility point

### Phase 4: Create Subconscious Memory Guide

- **Task ID**: write-subconscious-guide
- **Depends On**: build-subconscious-recipe
- **Files Created**:
  - `docs/guides/subconscious-memory-recipe.md`
- Architecture overview (pre-turn / post-turn / outcome hooks)
- Quick start example
- PydanticAI integration example
- Plain OpenAI SDK integration example
- Tuning and extensibility notes

### Phase 5: Create Subconscious Memory Tests

- **Task ID**: test-subconscious-recipe
- **Depends On**: build-subconscious-recipe
- **Files Created**:
  - `tests/test_subconscious_memory.py`
- Test inject_context with and without existing memories
- Test extract_memories from sample response text
- Test report_outcomes dispatching
- Test full round-trip with mocked LLM
- Redis cleanup fixtures

### Phase 6: Final Validation

- **Task ID**: validate-all
- **Depends On**: all previous phases
- Run full test suite: `pytest tests/ -x -q`
- Verify all guide code blocks are covered by test_guide_examples.py
- Verify mkdocs builds cleanly

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Guide tests pass | `pytest tests/test_guide_examples.py -x -q` | exit code 0 |
| Subconscious tests pass | `pytest tests/test_subconscious_memory.py -x -q` | exit code 0 |
| All tests pass | `pytest tests/ -x -q` | exit code 0 |
| Recipe importable | `python -c "from popoto.recipes.subconscious_memory import SubconsciousMemory; print('OK')"` | OK |
| Guide updated | `grep -q 'chat.completions.create' docs/guides/agent-memory-quickstart.md` | exit code 0 |
| New guide exists | `test -f docs/guides/subconscious-memory-recipe.md` | exit code 0 |

## Open Questions

1. Should `openai` be added to `pyproject.toml` as an optional dependency group (e.g., `[dev,llm]`), or should the guide examples simply document `pip install openai` as a prerequisite?

2. For the fact extraction heuristic in SubconsciousMemory, what granularity of extraction makes sense? One Memory record per sentence? Per paragraph? Per semantic unit? The plan defaults to per-sentence with configurable splitting.

3. Should SubconsciousMemory be exported from the top-level `popoto` package (like ContextAssembler is), or only available via `from popoto.recipes.subconscious_memory import SubconsciousMemory`?
