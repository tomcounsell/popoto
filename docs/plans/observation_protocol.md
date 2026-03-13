---
status: Ready
type: feature
appetite: Medium
owner: Valor
created: 2026-03-13
tracking: https://github.com/tomcounsell/popoto/issues/198
last_comment_id: IC_kwDOExCOnM7xpo_j
---

# ObservationProtocol + RecallProposal — Outcome-Driven Memory Effects

## Problem

After DecayingSortedField (#193), CyclicDecayField (#196), and AccessTrackerMixin (#197), the agent memory system can track decay, temporal rhythms, and read patterns. But there is no mechanism for the application to report *how* the agent used retrieved memories.

**Current behavior:**
An LLM retrieves memories via `top_by_decay()`. AccessTrackerMixin stages the reads. But the staging never resolves — there's no feedback loop telling the ORM whether the agent acted on, dismissed, or ignored each memory. `touch()`, `resolve_pressure()`, and cycle strengthening/weakening must be called manually — but an LLM cannot manage its own memory mechanics.

**Desired outcome:**
An `ObservationProtocol` that defines three lifecycle hooks and a `RecallProposal` for tracking proactively surfaced memories. The application layer reports behavioral signals (acted, dismissed, deferred, contradicted); the ORM applies effects atomically.

## Prior Art

- **PR #201**: Add CyclicDecayField — ships cycles/pressure companion hashes, Lua scoring, `resolve_pressure()`. This issue builds the observation layer on top.
- **PR #203**: Add AccessTrackerMixin — ships `on_read()`, `confirm_access()`, `discard_staged_access()`, `no_track()`. These are the staging primitives that this protocol resolves.
- **PR #199**: Add DecayingSortedField — ships `touch()`, `top_by_decay()`. The `acted` outcome calls `touch()`.
- **PR #190**: `atomic_increment()` — established Lua scripting pattern for atomic Redis operations.

No prior attempts at observation/feedback mechanisms exist.

## Data Flow

1. **Entry point**: Agent queries memories via `Model.query.filter(...).top_by_decay("field", n=10)`. Query hooks fire `on_read()` → timestamps staged in `$AT:{Class}:staged:{key}`.
2. **Proactive surfacing** (optional): Application calls `ObservationProtocol.on_surfaced(instances, reason)` for memories pushed into context by cyclical/pressure scoring. Creates `RecallProposal` entries in `$RP:{Class}:pending:{partition}` ZSET.
3. **Agent processes**: LLM generates a response using (or ignoring) the surfaced memories. This happens outside Popoto.
4. **Outcome reporting**: Application calls `ObservationProtocol.on_context_used(instances, outcome_map)` with a dict mapping instance PKs to outcomes (`acted`/`dismissed`/`deferred`/`contradicted`).
5. **Effect application**: For each instance, the protocol applies outcome-specific effects atomically:
   - `acted` → `touch()`, `confirm_access()`, strengthen cycles, `resolve_pressure()`
   - `dismissed` → `discard_staged_access()`, weaken cycles
   - `deferred` → `discard_staged_access()`, no other effects (pressure keeps building)
   - `contradicted` → `discard_staged_access()`, weaken cycles aggressively
6. **Proposal cleanup**: Resolved proposals are removed from the pending ZSET. Expired proposals (past TTL) are treated as `deferred` on next cleanup pass.

## Architectural Impact

- **New files**: `src/popoto/fields/observation.py` (ObservationProtocol + RecallProposal)
- **Modified files**: `src/popoto/fields/cyclic_decay_field.py` (add `strengthen_cycle` / `weaken_cycle` class methods), `src/popoto/models/base.py` (add `strengthen_cycle` / `weaken_cycle` instance methods), `src/popoto/__init__.py` (exports), `src/popoto/fields/__init__.py` (if needed)
- **New dependencies**: None (uses existing msgpack, redis-py)
- **Interface changes**: Adds new public methods on Model (`strengthen_cycle`, `weaken_cycle`). Adds `ObservationProtocol` class with static methods. Adds `RecallProposal` internal class. No changes to existing signatures.
- **Coupling**: ObservationProtocol depends on AccessTrackerMixin, DecayingSortedField, and CyclicDecayField but gracefully degrades when a model doesn't use all of them (e.g., a model with only DecayingSortedField still gets touch() on acted).
- **Reversibility**: High — entirely additive. Removing the module leaves existing functionality untouched.

## Appetite

**Size:** Medium

**Team:** Solo dev, PM

**Interactions:**
- PM check-ins: 0 (scope is well-defined by issue #198)
- Review rounds: 1 (code review)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| DecayingSortedField available | `python -c "from popoto import DecayingSortedField"` | touch() dependency |
| CyclicDecayField available | `python -c "from popoto import CyclicDecayField"` | cycle/pressure dependency |
| AccessTrackerMixin available | `python -c "from popoto import AccessTrackerMixin"` | staging dependency |
| Redis running | `python -c "from popoto import get_redis; get_redis().ping()"` | Storage backend |

## Solution

### Key Elements

- **ObservationProtocol**: Static class with three hooks — `on_read()`, `on_surfaced()`, `on_context_used()`. Stateless coordinator that dispatches effects based on outcome type.
- **RecallProposal**: Internal Redis-backed tracking for proactively surfaced memories. ZSET keyed by model class and partition, scored by surfaced_at timestamp. TTL-based expiration.
- **Model.strengthen_cycle(field_name, factor)**: Multiplies cycle amplitude by factor (>1.0 strengthens). Reads/writes companion cycles hash atomically.
- **Model.weaken_cycle(field_name, factor)**: Multiplies cycle amplitude by factor (<1.0 weakens). Same mechanism as strengthen but with factor < 1.0. Single method could handle both, but separate names improve readability at call sites.
- **Graceful degradation**: Each effect checks whether the model supports it (has the right mixin/field type) before applying. A model with DecayingSortedField but no CyclicDecayField still gets `touch()` on acted — just no cycle/pressure effects.

### Flow

**Query retrieval** → `on_read()` stages timestamps → **Agent processes** → Application infers outcomes → `on_context_used(instances, outcome_map)` → **Effects applied atomically per instance** → Proposals resolved

**Proactive path** → `on_surfaced(instances, reason)` → RecallProposal created → **Agent processes** → `on_context_used()` → Effects applied → Proposal marked resolved/expired

### Technical Approach

#### ObservationProtocol (new file: `src/popoto/fields/observation.py`)

```python
class ObservationProtocol:
    """Lifecycle hooks for passive behavioral inference on memory models."""

    @staticmethod
    def on_read(instance, pipeline=None):
        """Fire when query hydrates an instance. Delegates to AccessTrackerMixin staging."""
        if hasattr(instance, 'on_read'):
            instance.on_read(pipeline=pipeline)

    @staticmethod
    def on_surfaced(instances, reason="proactive", partition=None, pipeline=None):
        """Fire when proactive system pushes memories into agent context.
        Creates RecallProposal entries. Side-effect-free on the memories."""
        RecallProposal.create_batch(instances, reason=reason, partition=partition, pipeline=pipeline)

    @staticmethod
    def on_context_used(instances, outcome_map, pipeline=None):
        """Fire when application reports how agent responded to surfaced memories.
        outcome_map: {instance_pk: "acted"|"dismissed"|"deferred"|"contradicted"}
        Applies effects based on outcome."""
        # For each instance, look up its outcome and apply effects
        for instance in instances:
            pk = instance._redis_key or instance.db_key.redis_key
            outcome = outcome_map.get(pk, "deferred")
            _apply_outcome(instance, outcome, pipeline=pipeline)
```

#### Outcome Effects (in `observation.py`)

```python
def _apply_outcome(instance, outcome, pipeline=None):
    """Apply effects for a single outcome on a single instance."""
    if outcome == "acted":
        _apply_acted(instance, pipeline)
    elif outcome == "dismissed":
        _apply_dismissed(instance, pipeline)
    elif outcome == "deferred":
        _apply_deferred(instance, pipeline)
    elif outcome == "contradicted":
        _apply_contradicted(instance, pipeline)

def _apply_acted(instance, pipeline=None):
    """Acted: touch decay clock, confirm reads, strengthen cycles, discharge pressure."""
    # Find DecayingSortedField(s) on the model
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, DecayingSortedField):
            instance.touch(field_name, pipeline=pipeline)
    # Confirm staged reads
    if hasattr(instance, 'confirm_access'):
        instance.confirm_access(pipeline=pipeline)
    # Strengthen cycles (if CyclicDecayField)
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, CyclicDecayField):
            instance.strengthen_cycle(field_name, factor=1.2, pipeline=pipeline)
            if field.pressure_rate > 0:
                instance.resolve_pressure(field_name, pipeline=pipeline)

def _apply_dismissed(instance, pipeline=None):
    """Dismissed: discard staged reads, weaken cycles."""
    if hasattr(instance, 'discard_staged_access'):
        instance.discard_staged_access(pipeline=pipeline)
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, CyclicDecayField):
            instance.weaken_cycle(field_name, factor=0.8, pipeline=pipeline)

def _apply_deferred(instance, pipeline=None):
    """Deferred: discard staged reads, no other effects. Pressure keeps building."""
    if hasattr(instance, 'discard_staged_access'):
        instance.discard_staged_access(pipeline=pipeline)

def _apply_contradicted(instance, pipeline=None):
    """Contradicted: discard staged reads, aggressively weaken cycles."""
    if hasattr(instance, 'discard_staged_access'):
        instance.discard_staged_access(pipeline=pipeline)
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, CyclicDecayField):
            instance.weaken_cycle(field_name, factor=0.5, pipeline=pipeline)
```

#### RecallProposal (in `observation.py`)

```python
class RecallProposal:
    """Internal tracking for proactively surfaced memories.

    Key pattern: $RP:{ClassName}:pending:{partition} → ZSET scored by surfaced_at
    Statuses: pending → acted | dismissed | deferred | contradicted | expired
    TTL: default 3600s (1 hour). Unresolved proposals treated as deferred.
    """
    DEFAULT_TTL = 3600

    @classmethod
    def create_batch(cls, instances, reason="proactive", partition=None, pipeline=None):
        """Create pending proposals for a batch of instances."""
        # ZADD to $RP:{ClassName}:pending:{partition} with score=now

    @classmethod
    def resolve(cls, instance, outcome, partition=None, pipeline=None):
        """Remove a resolved proposal from the pending set."""
        # ZREM from pending set

    @classmethod
    def expire_stale(cls, model_class, partition=None, ttl=None, pipeline=None):
        """Remove proposals older than TTL. Returns expired member keys."""
        # ZRANGEBYSCORE where score < now - ttl, then ZREM

    @classmethod
    def get_pending(cls, model_class, partition=None):
        """Return all pending proposals as (member_key, surfaced_at) pairs."""
        # ZRANGE with WITHSCORES
```

#### New Model Methods: `strengthen_cycle` / `weaken_cycle`

Added to `Model` in `base.py`, following the pattern of `touch()` and `resolve_pressure()`:

```python
def strengthen_cycle(self, field_name, factor=1.2, pipeline=None):
    """Multiply all cycle amplitudes by factor (>1.0 strengthens).
    Reads current cycles from companion hash, multiplies each amplitude,
    writes back. Atomic via single HGET + HSET."""

def weaken_cycle(self, field_name, factor=0.8, pipeline=None):
    """Multiply all cycle amplitudes by factor (<1.0 weakens).
    Same mechanics as strengthen_cycle but with factor < 1.0."""
```

Both methods:
1. Validate field is CyclicDecayField
2. Validate model is saved
3. Read msgpack cycles from companion hash
4. Multiply each amplitude by factor
5. Write back to companion hash
6. Accept optional pipeline for batching

#### Integration with `no_track()` and `top_by_decay()`

- `no_track()` already suppresses `on_read()` in query hooks (PR #203). ObservationProtocol's internal operations (e.g., re-querying during outcome application) should use `no_track()` to avoid re-staging reads.
- `top_by_decay()` returns model instances with `on_read()` already fired (if `_track_reads=True`). The application passes these same instances to `on_context_used()` — the protocol resolves their staged reads based on outcome.
- Pressure tracking is orthogonal to access tracking: `resolve_pressure()` is called by the `acted` outcome handler, not by `top_by_decay()`. Querying does not auto-resolve pressure.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `on_context_used()` with invalid outcome string → raises ValueError
- [ ] `on_context_used()` with instance PK not in outcome_map → defaults to "deferred"
- [ ] `strengthen_cycle()` / `weaken_cycle()` on non-CyclicDecayField → raises TypeError
- [ ] `strengthen_cycle()` / `weaken_cycle()` on unsaved instance → raises TypeError
- [ ] `on_surfaced()` with empty instances list → no-op, no error

### Empty/Invalid Input Handling
- [ ] `on_context_used()` with empty instances list → no-op
- [ ] `on_context_used()` with empty outcome_map → all instances get "deferred"
- [ ] `RecallProposal.expire_stale()` with no pending proposals → returns empty list
- [ ] `strengthen_cycle()` with factor=1.0 → no-op (amplitudes unchanged)

### Error State Rendering
- [ ] Not applicable — this is ORM infrastructure, no user-visible rendering

## Rabbit Holes

- **Confidence field integration**: The roadmap mentions "corroborate confidence" and "contradict confidence" as outcome effects. ConfidenceField doesn't exist yet (Step 4). The plan should NOT try to build confidence effects now — leave those as no-ops with TODO comments that activate when ConfidenceField ships.
- **Semantic similarity inference**: Popoto provides the hooks; the application provides the inference signal. Do NOT build any NLP/embedding-based acted-vs-dismissed detection into Popoto itself.
- **Automatic proposal creation from top_by_decay()**: It's tempting to auto-create proposals whenever `top_by_decay()` runs. Don't — the application decides what constitutes "surfacing" vs. internal queries. Keep `on_surfaced()` as an explicit call.
- **Co-occurrence field integration**: The roadmap mentions strengthening co-occurrence links on `acted`. CoOccurrenceField doesn't exist yet (Step 5). Skip.
- **Entrainment / phase correction**: The roadmap describes self-correcting cycle phases. That's Step 4 territory. Strengthen/weaken amplitude only for now.

## Risks

### Risk 1: Pipeline atomicity across mixed operations
**Impact:** If `on_context_used()` applies effects across touch(), confirm_access(), strengthen_cycle(), and resolve_pressure() without a pipeline, partial failures leave inconsistent state.
**Mitigation:** When no pipeline is provided, create an internal pipeline and execute it. All effects for a single instance are batched into one `pipeline.execute()` call.

### Risk 2: Factor accumulation on cycle amplitudes
**Impact:** Repeated strengthening/weakening could drive amplitudes to infinity or zero.
**Mitigation:** Clamp amplitudes to `[0.0, max_amplitude]` range. Default `max_amplitude=100.0`. Amplitudes below a threshold (e.g., 0.01) are treated as zero (cycle effectively dead).

## Race Conditions

### Race 1: Concurrent on_context_used() calls for same instance
**Location:** `observation.py` — `_apply_acted()` / `_apply_dismissed()`
**Trigger:** Two agent turns process the same memory simultaneously.
**Data prerequisite:** Instance must exist in Redis with valid sorted set entry.
**State prerequisite:** Staged reads exist in the staging list.
**Mitigation:** Each effect is atomic at the Redis level (ZADD, HSET, EVAL). `confirm_access()` uses a Lua script that atomically moves staged reads. Worst case: double-touch (harmless — just updates timestamp twice) or double-confirm (Lua script handles empty staging list gracefully, returns 0).

### Race 2: Proposal expiration during outcome resolution
**Location:** `RecallProposal.expire_stale()` vs `RecallProposal.resolve()`
**Trigger:** Expiration cleanup runs while application is resolving the same proposal.
**Mitigation:** `resolve()` uses ZREM which is idempotent. If expiration removes the proposal first, resolve() returns 0 (already removed). Effects are applied regardless of proposal state — the proposal is just tracking, not gating.

## No-Gos (Out of Scope)

- **ConfidenceField effects** — deferred to Step 4 when ConfidenceField ships
- **CoOccurrenceField effects** — deferred to Step 5
- **Entrainment / phase correction** — deferred to Step 4
- **Automatic inference of outcomes** — application responsibility, not ORM
- **Automatic proposal creation from queries** — application decides what constitutes surfacing
- **Background expiration thread/task** — application calls `expire_stale()` when appropriate
- **Async API** — Popoto stays sync per DX best practices doc

## Update System

No update system changes required — this is a library feature in the Popoto package. Users upgrade via `pip install --upgrade popoto`.

## Agent Integration

No agent integration required — Popoto is a standalone ORM library. Agent frameworks (PydanticAI, Claude Agent SDK) call these methods from their tool functions, as documented in `docs/plans/dx-best-practices.md`.

## Documentation

- [ ] Update `docs/agent-memory.md` with ObservationProtocol usage examples
- [ ] Update `docs/api-reference.md` with new methods (strengthen_cycle, weaken_cycle, ObservationProtocol, RecallProposal)
- [ ] Add inline docstrings following existing patterns (see `access_tracker.py`, `cyclic_decay_field.py`)
- [ ] Update `docs/references/popoto-memory-roadmap.md` Step 2 status to "Shipped"

## Success Criteria

- [ ] `ObservationProtocol` defines `on_read()`, `on_surfaced()`, `on_context_used()` hooks
- [ ] `on_read()` delegates to AccessTrackerMixin staging
- [ ] `on_surfaced()` creates RecallProposal entries in Redis ZSET
- [ ] `on_context_used()` applies correct effects for each of 4 outcome types
- [ ] `acted` → touch(), confirm_access(), strengthen_cycle(), resolve_pressure()
- [ ] `dismissed` → discard_staged_access(), weaken_cycle(factor=0.8)
- [ ] `deferred` → discard_staged_access() only
- [ ] `contradicted` → discard_staged_access(), weaken_cycle(factor=0.5)
- [ ] `Model.strengthen_cycle(field_name, factor)` multiplies cycle amplitudes
- [ ] `Model.weaken_cycle(field_name, factor)` multiplies cycle amplitudes
- [ ] Amplitude clamping prevents infinity/zero drift
- [ ] RecallProposal ZSET with TTL-based expiration
- [ ] Graceful degradation: DecayingSortedField-only models get touch() on acted, skip cycle/pressure effects
- [ ] `no_track()` suppresses staging in internal operations
- [ ] All effects batched via pipeline for atomicity
- [ ] Tests pass (`pytest tests/test_observation_protocol.py -x -q`)
- [ ] Documentation updated

## Team Orchestration

### Team Members

- **Builder (observation)**
  - Name: observation-builder
  - Role: Implement ObservationProtocol, RecallProposal, strengthen/weaken cycle methods
  - Agent Type: builder
  - Resume: true

- **Validator (observation)**
  - Name: observation-validator
  - Role: Verify implementation against success criteria and test coverage
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: observation-docs
  - Role: Update agent-memory docs, API reference, roadmap status
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Add strengthen_cycle / weaken_cycle to Model
- **Task ID**: build-cycle-methods
- **Depends On**: none
- **Assigned To**: observation-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `strengthen_cycle(field_name, factor, pipeline)` to `Model` in `base.py`
- Add `weaken_cycle(field_name, factor, pipeline)` to `Model` in `base.py`
- Both: validate CyclicDecayField, validate saved, read msgpack, multiply amplitudes, write back
- Clamp amplitudes to [0.0, 100.0] range, treat < 0.01 as zero
- Follow exact pattern of `resolve_pressure()` for validation and Redis access

### 2. Implement ObservationProtocol + RecallProposal
- **Task ID**: build-observation
- **Depends On**: build-cycle-methods
- **Assigned To**: observation-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `src/popoto/fields/observation.py`
- Implement `ObservationProtocol` with three static methods
- Implement `RecallProposal` with ZSET-backed storage
- Implement `_apply_outcome()` dispatcher and four outcome handlers
- Use internal pipelines for atomicity when no pipeline provided
- Add exports to `src/popoto/__init__.py`

### 3. Write tests
- **Task ID**: build-tests
- **Depends On**: build-observation
- **Assigned To**: observation-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_observation_protocol.py`
- Test each outcome type independently (acted, dismissed, deferred, contradicted)
- Test graceful degradation (DecayingSortedField-only model)
- Test RecallProposal create/resolve/expire lifecycle
- Test synergy with AccessTrackerMixin (confirm vs discard staged reads)
- Test synergy with CyclicDecayField (amplitude changes)
- Test pipeline atomicity
- Test edge cases (empty inputs, invalid outcomes, unsaved instances)

### 4. Validate implementation
- **Task ID**: validate-observation
- **Depends On**: build-tests
- **Assigned To**: observation-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify all success criteria met
- Verify lint/format: `python -m ruff check . && python -m ruff format --check .`
- Verify exports: `python -c "from popoto import ObservationProtocol, RecallProposal"`

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-observation
- **Assigned To**: observation-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/agent-memory.md` with usage examples
- Update `docs/api-reference.md` with new methods
- Update roadmap Step 2 status

### 6. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: observation-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all validation commands
- Verify all success criteria met including documentation
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check .` | exit code 0 |
| Format clean | `python -m ruff format --check .` | exit code 0 |
| Observation tests | `pytest tests/test_observation_protocol.py -v` | exit code 0 |
| Exports work | `python -c "from popoto import ObservationProtocol, RecallProposal"` | exit code 0 |
| Cycle methods exist | `python -c "from popoto import Model; assert hasattr(Model, 'strengthen_cycle')"` | exit code 0 |
