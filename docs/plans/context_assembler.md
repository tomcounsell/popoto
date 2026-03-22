---
status: Shipped
type: feature
appetite: Medium
owner: Valor
created: 2026-03-20
tracking: https://github.com/tomcounsell/popoto/issues/233
last_comment_id: 4097083382
---

# ContextAssembler — Retrieval-to-Injection Bridge with Token Budgets

## Problem

AI agents need to retrieve relevant memories at the start of each turn and inject them into the LLM context window, but there's no unified retrieval pipeline that combines all 12 shipped primitives into a single call. Developers must manually orchestrate ExistenceFilter checks, CompositeScoreQuery ranking, CoOccurrence propagation, CyclicDecayField scanning, and budget-constrained selection — a multi-step process that's easy to get wrong.

**Current behavior:**
All 12 memory primitives are shipped and work individually, but composing them requires ~50 lines of manual orchestration per retrieval call: checking Bloom filters, running composite queries, scanning for proactive surfacing, propagating associations, counting tokens, and formatting output. There's no reference implementation or reusable utility.

**Desired outcome:**
A `ContextAssembler` recipe that orchestrates the full retrieval pipeline in a single `assemble()` call:
1. **Pull path** (query-driven): ExistenceFilter pre-check → CompositeScoreQuery ranking → CoOccurrence propagation
2. **Push path** (proactive surfacing): CyclicDecayField temporal scan for records exceeding a surfacing threshold
3. **Merge + budget**: Combine candidates, apply token budget, format for LLM injection
4. **Post-retrieval effects**: Access tracking, RecallProposal creation, competitive suppression

This is Step 12 (capstone) of the [Popoto Memory Roadmap](../guides/popoto-memory-roadmap.md).

## Prior Art

No prior issues or PRs found related to ContextAssembler or retrieval pipeline orchestration. This is greenfield work.

Relevant shipped prerequisites (all 12 steps complete):
- **PR #239**: PolicyCache (Step 11) — reference recipe composing all primitives into RL-style action selection
- **PR #238**: StreamConsumer (Step 10) — background stream processing
- **PR #237**: PredictionLedger (Step 9) — outcome tracking
- **PR #206**: ObservationProtocol + RecallProposal (Step 8) — outcome-driven memory effects
- **Issue #212**: CompositeScoreQuery (Step 7) — multi-factor ranking via ZUNIONSTORE
- **PR #220**: EventStreamMixin (Step 6) — mutation logging
- **Issue #210**: CoOccurrenceField (Step 5) — weighted association graph with BFS propagation
- **Issue #209**: ConfidenceField (Step 4) — Bayesian certainty tracking
- **Issue #208**: WriteFilterMixin (Step 3) — selective encoding gate
- **Issue #193**: DecayingSortedField + CyclicDecayField (Steps 1-2) — temporal decay scoring

## Data Flow

1. **Entry point**: Application calls `assembler.assemble(query_cues={"topic": "deployment"}, agent_id="agent-1")`
2. **Pull path — ExistenceFilter pre-check**: If model has an ExistenceFilter field, check `might_exist()` against query cue fingerprint. If `definitely_missing()`, skip expensive retrieval for that cue.
3. **Pull path — CompositeScoreQuery**: Run `model_class.query.filter(partition).composite_score(indexes=score_weights, limit=max_items*2, co_occurrence_boost=propagated_scores)` to get ranked candidates with their composite scores.
4. **Pull path — CoOccurrence propagation**: From top-N pull results, propagate via `CoOccurrenceField.propagate()` BFS to discover associated records not in the initial result set. Add discovered records as additional candidates with attenuated scores.
5. **Push path — CyclicDecayField scan**: Run `model_class.query.filter(partition).top_by_decay(field_name, n=max_items)` on CyclicDecayField indexes. Filter to records whose score exceeds `surfacing_threshold`. These are candidates independent of the query.
6. **Merge + re-rank**: Combine pull and push candidates. Deduplicate by redis_key. Pull-path records keep their composite score; push-path records keep their cyclic+pressure score. Re-rank by final score.
7. **Budget selection**: Apply `max_items` cap. If `max_tokens` is set, estimate token count per record and truncate to fit budget.
8. **Post-retrieval effects**: Fire `ObservationProtocol.on_read()` for pull-path records. Fire `ObservationProtocol.on_surfaced()` for push-path records (creates RecallProposals). Apply competitive suppression to non-selected candidates.
9. **Format output**: Return `AssemblyResult` with `.records`, `.proactive`, `.formatted`, `.metadata`.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (scope alignment on token counting strategy)
- Review rounds: 1 (code review)

## Prerequisites

No prerequisites — all 12 memory primitives are shipped. Redis connection via `POPOTO_REDIS_DB` is available.

## Solution

### Key Elements

- **`ContextAssembler` class**: Configurable orchestrator with `assemble()` method. Lives in `popoto/recipes/context_assembler.py`.
- **`AssemblyResult` dataclass**: Return type with `.records` (all selected), `.proactive` (push-path subset), `.formatted` (LLM-ready string), `.metadata` (scores, token counts, timing).
- **Pull pipeline**: ExistenceFilter → CompositeScoreQuery → CoOccurrence propagation. Reuses existing query infrastructure entirely.
- **Push pipeline**: `top_by_decay()` on CyclicDecayField with threshold filter. Independent of query cues.
- **Budget enforcer**: `max_items` hard cap + optional `max_tokens` soft cap with pluggable token estimator.
- **Output formatters**: Pluggable via `output_format` parameter. Ship three: `"structured"` (JSON), `"xml"` (XML tags), `"natural"` (natural language summary).
- **Competitive suppression**: Non-selected pull-path candidates get a small negative signal to prevent re-surfacing on identical cues.

### Flow

**Application** → `assembler.assemble(query_cues, agent_id)` → **ExistenceFilter pre-check** → **CompositeScoreQuery** → **CoOccurrence propagation** → **CyclicDecayField scan** → **Merge + re-rank** → **Budget selection** → **Post-retrieval effects** → **Format** → `AssemblyResult`

### Technical Approach

- **Reuse existing query infrastructure**: `composite_score()` already handles DecayingSortedField materialization, ConfidenceField extraction, AccessTracker frequency, WriteFilter priority, and CoOccurrence boost — all via ZUNIONSTORE. ContextAssembler orchestrates calls to this, not reimplements it.
- **Push path via `top_by_decay()`**: The existing `QueryBuilder.top_by_decay()` method already handles CyclicDecayField (with cyclic resonance + pressure) via Lua scripts. Filter results by `surfacing_threshold` in Python after retrieval.
- **CoOccurrence propagation**: Use `CoOccurrenceField.propagate(model_class, seed_pks, depth, decay_per_hop, threshold)` which runs BFS via Lua script. Feed propagated scores as `co_occurrence_boost` dict into `composite_score()`.
- **Token estimation**: Default heuristic is `len(str(record)) // 4` (chars/4 ≈ tokens). Accept optional `token_counter` callable for precise counting (e.g., tiktoken). No tiktoken dependency — users bring their own.
- **Pipeline optimization**: Use Redis pipeline for post-retrieval effects (on_read, on_surfaced, competitive suppression) to batch commands.
- **Competitive suppression**: For non-selected candidates from pull path, call `ConfidenceField.update_confidence(instance, field_name, signal=0.3)` — a mild negative signal that slightly reduces future ranking without aggressive weakening.
- **Recipe pattern**: Follow `policy_cache.py` pattern — module-level constants, utility functions, dataclass for results, main class. Importable from `popoto.recipes`.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `assemble()` with model class that has no scored fields: raises `QueryException` with descriptive message
- [ ] `assemble()` when Redis is unreachable: propagates `ConnectionError` (no silent swallowing)
- [ ] CoOccurrence propagation returning empty graph: gracefully returns pull-only results
- [ ] Token estimator raising exception: catches and falls back to default heuristic with warning log

### Empty/Invalid Input Handling
- [ ] `assemble()` with empty `query_cues`: returns push-path results only (no pull path)
- [ ] `assemble()` with `max_items=0`: returns empty `AssemblyResult`
- [ ] `assemble()` with `max_tokens=0`: returns empty `AssemblyResult`
- [ ] `assemble()` with model that has no CyclicDecayField: push path silently returns empty (no crash)
- [ ] `assemble()` with model that has no ExistenceFilter: skip pre-check, proceed to CompositeScoreQuery
- [ ] `assemble()` with model that has no CoOccurrenceField: skip propagation, use CompositeScoreQuery results directly

### Error State Rendering
- [ ] `AssemblyResult.metadata` includes timing info even on partial failures
- [ ] Warning logged when push path finds 0 records above surfacing threshold (not an error — normal)

## Test Impact

No existing tests affected — this is a greenfield recipe with no prior test coverage. All existing primitive tests remain unchanged since ContextAssembler only composes existing APIs without modifying them.

## Rabbit Holes

- **Sophisticated token counting**: Tempting to add tiktoken or model-specific tokenizers as dependencies. Just ship a `len(str(x))//4` heuristic and accept a `token_counter` callable. Users bring their own tokenizer.
- **Async assembler**: The existing query infrastructure is synchronous. Don't add an async variant — it would require async versions of `composite_score()`, `top_by_decay()`, and all field operations. Out of scope.
- **Caching assembled results**: Tempting to cache `AssemblyResult` in Redis with TTL. The whole point is fresh retrieval — caching defeats the purpose of decay scoring.
- **Streaming/incremental assembly**: Don't stream records one-at-a-time. Batch retrieval via pipeline is the right pattern.
- **Custom re-ranking models**: The merge step uses simple score comparison. Don't add ML-based re-rankers — that's application-layer logic.
- **Competitive suppression tuning**: The suppression signal (0.3) is a magic number. Don't add configurable suppression strategies — ship the default, let users override via `post_filter`.

## Risks

### Risk 1: Pull+push path produces too many Redis round trips
**Impact:** Slow `assemble()` calls (>100ms) make it impractical for per-turn injection.
**Mitigation:** Pull path is 1-2 Redis calls (composite_score uses ZUNIONSTORE internally). Push path is 1 call (top_by_decay Lua script). Post-effects are pipelined. Total: ~4-6 Redis calls. Profile in integration test and document P95 latency.

### Risk 2: CoOccurrence propagation expands candidate pool explosively
**Impact:** BFS with high depth returns thousands of candidates, overwhelming budget selection.
**Mitigation:** `propagation_depth` defaults to 2 (not configurable beyond 3). `threshold` parameter prunes low-weight edges. `max_items*2` cap on propagated candidates prevents runaway expansion.

### Risk 3: Token estimation inaccuracy causes context window overflow
**Impact:** Agent sends context that exceeds LLM's window, causing truncation or errors.
**Mitigation:** Default heuristic overestimates slightly (chars/4). Users can provide precise `token_counter`. `max_tokens` is a soft cap — leave 10% headroom by default.

## Race Conditions

No race conditions identified — all operations are synchronous and single-threaded within a single `assemble()` call. Redis operations (ZUNIONSTORE, Lua scripts) are atomic. Post-retrieval effects use a single Redis pipeline.

## No-Gos (Out of Scope)

- Async `assemble()` variant
- tiktoken or model-specific tokenizer as a dependency
- Caching/memoizing assembled results
- Custom re-ranking models or ML-based scoring
- Streaming/incremental record delivery
- Configurable competitive suppression strategies
- Cross-model assembly (single model_class per assembler)
- Persistence of assembly history/audit log

## Update System

No update system changes required — this is a library feature in popoto with no deployment infrastructure.

## Agent Integration

No agent integration required — popoto is a library consumed by applications. No MCP servers, bridge changes, or tool wrapping needed.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/agent-memory.md` — fill in the ContextAssembler section (currently a placeholder)
- [ ] Add usage examples showing pull-only, push-only, and combined assembly

### External Documentation Site
- [ ] Update mkdocs.yml nav if new guide pages are added
- [ ] Verify docs build passes: `mkdocs build --strict`

### Inline Documentation
- [ ] Module docstring in `context_assembler.py` following `policy_cache.py` pattern (purpose, example, synergy table)
- [ ] Docstrings for `ContextAssembler.__init__()`, `assemble()`, and `AssemblyResult`
- [ ] Code comments on the merge+re-rank algorithm

## Success Criteria

- [ ] `ContextAssembler` class with `assemble()` method in `popoto/recipes/context_assembler.py`
- [ ] Pull path: ExistenceFilter pre-check → CompositeScoreQuery → CoOccurrence propagation
- [ ] Push path: CyclicDecayField scan above `surfacing_threshold`
- [ ] Budget-constrained selection (`max_items`, `max_tokens`)
- [ ] `AssemblyResult` with `.records`, `.proactive`, `.formatted`, `.metadata`
- [ ] RecallProposal creation for push-path records via `ObservationProtocol.on_surfaced()`
- [ ] Competitive suppression of non-selected pull-path candidates
- [ ] Three output formatters: structured (JSON), XML, natural language
- [ ] Pluggable `token_counter` with default heuristic
- [ ] Pipeline-batched post-retrieval effects
- [ ] Graceful degradation when optional fields (ExistenceFilter, CoOccurrenceField, CyclicDecayField) are absent
- [ ] Exported from `popoto.recipes` and `popoto.__init__`
- [ ] Valkey compatible (no Redis modules)
- [ ] Integration test exercising all 12 primitives end-to-end
- [ ] `docs/features/agent-memory.md` ContextAssembler section updated
- [ ] Tests pass: `pytest tests/test_context_assembler.py -x -q`

## Team Orchestration

### Team Members

- **Builder (assembler-core)**
  - Name: assembler-builder
  - Role: Implement ContextAssembler class, AssemblyResult, formatters, and budget logic
  - Agent Type: builder
  - Resume: true

- **Builder (assembler-tests)**
  - Name: test-builder
  - Role: Implement integration tests following test_policy_cache.py patterns
  - Agent Type: builder
  - Resume: true

- **Validator (all)**
  - Name: assembler-validator
  - Role: Verify all success criteria and run full test suite
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Implement ContextAssembler class and AssemblyResult
- **Task ID**: build-assembler
- **Depends On**: none
- **Validates**: tests/test_context_assembler.py (create)
- **Assigned To**: assembler-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/recipes/context_assembler.py` with:
  - `AssemblyResult` dataclass: `records`, `proactive`, `formatted`, `metadata` fields
  - `ContextAssembler.__init__(model_class, score_weights, max_items=10, max_tokens=None, surfacing_threshold=0.5, propagation_depth=2, output_format="structured", token_counter=None)`
  - `assemble(query_cues=None, agent_id=None, partition_filters=None)` method implementing the full pipeline
  - Pull path: detect ExistenceFilter on model → pre-check → `composite_score()` with `score_weights` → CoOccurrence `propagate()` if field exists
  - Push path: detect CyclicDecayField on model → `top_by_decay()` → filter by `surfacing_threshold`
  - Merge, deduplicate, budget-select, post-effects (pipeline-batched), format
- Three output formatters: `_format_structured()` (JSON), `_format_xml()`, `_format_natural()`
- Competitive suppression via `ConfidenceField.update_confidence()` for non-selected pull candidates
- Default token estimator: `lambda record: len(str(record)) // 4`
- Follow `policy_cache.py` module structure: module docstring with synergy table, constants, utility functions, main class
- Update `popoto/recipes/__init__.py` to export `ContextAssembler` and `AssemblyResult`
- Update `popoto/__init__.py` to include `ContextAssembler` in public API

### 2. Implement integration tests
- **Task ID**: build-tests
- **Depends On**: build-assembler
- **Validates**: tests/test_context_assembler.py
- **Assigned To**: test-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_context_assembler.py` following `test_policy_cache.py` patterns:
  - Test model with all field types: DecayingSortedField/CyclicDecayField, ConfidenceField, CoOccurrenceField, ExistenceFilter, AccessTrackerMixin, WriteFilterMixin, EventStreamMixin, PredictionLedgerMixin
  - Helper functions `_clean_keys()` and `_clean_streams()` for test isolation
  - Test cases:
    - Pull-only assembly (no CyclicDecayField on model)
    - Push-only assembly (empty `query_cues`)
    - Combined pull+push assembly
    - Budget enforcement: `max_items` cap
    - Budget enforcement: `max_tokens` cap
    - ExistenceFilter pre-check skips retrieval when `definitely_missing()`
    - CoOccurrence propagation expands candidate pool
    - RecallProposal created for push-path records
    - Competitive suppression reduces confidence of non-selected candidates
    - Graceful degradation: model without ExistenceFilter
    - Graceful degradation: model without CoOccurrenceField
    - Graceful degradation: model without CyclicDecayField
    - Output format: structured (JSON parseable)
    - Output format: XML (valid tags)
    - Output format: natural (string)
    - Custom `token_counter` callable
    - Empty result set returns empty `AssemblyResult`
    - End-to-end: create records → assemble → verify effects → re-assemble → verify ranking changes
  - All tests use real Redis (no mocks), matching project test philosophy

### 3. Documentation
- **Task ID**: document-feature
- **Depends On**: build-tests
- **Assigned To**: assembler-validator
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/agent-memory.md` ContextAssembler section with:
  - Full API reference
  - Usage examples (pull-only, push-only, combined, custom token counter)
  - Pipeline steps diagram
  - Synergy table showing how each primitive participates

### 4. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: assembler-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all validation commands
- Verify all success criteria met
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_context_assembler.py -x -q` | exit code 0 |
| Full suite | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check src/popoto/recipes/context_assembler.py` | exit code 0 |
| Format clean | `python -m ruff format --check src/popoto/recipes/context_assembler.py` | exit code 0 |
| Class exists | `python -c "from popoto.recipes.context_assembler import ContextAssembler"` | exit code 0 |
| Public export | `python -c "from popoto import ContextAssembler"` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |

---

## Open Questions

1. **Token counting default**: The plan uses `len(str(record)) // 4` as the default heuristic. Is this acceptable, or should we require an explicit `token_counter` callable with no default?

2. **Competitive suppression signal strength**: Non-selected pull candidates get `signal=0.3` via `update_confidence()`. Is this the right level of suppression, or should it be configurable per-assembler?

3. **Push path partition**: The push path scans CyclicDecayField indexes. Should this always filter by `agent_id` partition (multi-agent isolation), or should it be opt-in via `partition_filters`?
