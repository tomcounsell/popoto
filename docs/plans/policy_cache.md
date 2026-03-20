---
status: Complete
type: feature
appetite: Medium
owner: Valor
created: 2026-03-20
tracking: https://github.com/tomcounsell/popoto/issues/232
last_comment_id: 4095890327
pr: https://github.com/tomcounsell/popoto/pull/239
---

# PolicyCache — Learned Action Selection from Crystallized Patterns

## Problem

AI agents repeatedly encounter similar situations but approach each from scratch — there's no mechanism to crystallize successful state→action→outcome patterns into reusable cached policies. An agent that has successfully handled "customer asks for refund" 10 times still treats the 11th encounter as novel.

**Current behavior:**
All 10 shipped memory primitives plus PredictionLedger and StreamConsumer exist, but there's no reference implementation showing how to compose them into a reinforcement-learning-style action selection cache. Developers wanting to build learned action selection must figure out the composition themselves.

**Desired outcome:**
A `PolicyEntry` reference model (shipped as example/recipe in `examples/`) and supporting logic that demonstrates:
1. State→action→expected_value triples built from Popoto primitives
2. Q-value temporal difference updates via Lua script
3. Crystallization trigger logic in a StreamConsumer handler (detects ≥3 repeated successes)
4. Temporal pattern discovery via timestamp clustering

This is Step 11 of the [Popoto Memory Roadmap](../guides/popoto-memory-roadmap.md).

## Prior Art

No prior issues or PRs found related to PolicyCache or action selection caching. This is greenfield work.

Relevant shipped prerequisites:
- **PR #238**: StreamConsumer (Step 10) — background processing framework. PolicyCache's crystallization handler runs inside a StreamConsumer.
- **PR #237**: PredictionLedger (Step 9) — outcome tracking. Prediction errors feed into policy confidence.
- **PR #220**: EventStreamMixin (Step 6) — mutation logging. Events flow to streams that the consumer reads.
- **PRs #206, #215, #216, etc.**: All 10 memory primitives are shipped and available.

## Data Flow

1. **Entry point**: Agent acts on a memory — `ObservationProtocol.on_context_used()` fires, `EventStreamMixin` writes a mutation entry to a Redis Stream
2. **StreamConsumer handler**: Reads batches of stream entries, groups by `(state_fingerprint, action_type)`, counts successes
3. **Crystallization check**: When same state+action has ≥3 successful outcomes and Wilson CI lower bound > 0.6, creates a `PolicyEntry`
4. **Temporal clustering**: Handler also clusters event timestamps using chi-squared test against uniform distribution (p < 0.05), discovers cyclical patterns
5. **Policy query**: Agent queries `PolicyEntry.query.filter(agent_id=..., state_fingerprint=...).top_by_decay()` to find cached policies for current state
6. **Q-value update**: After action outcome is known, Lua script atomically applies TD update: `Q += α(reward + γ·max_future_Q - Q)`
7. **Confidence feedback**: High prediction error from PredictionLedger weakens policy confidence via ConfidenceField

## Architectural Impact

- **New dependencies**: None — composes existing Popoto primitives only
- **Interface changes**: No changes to core ORM. New files in `examples/` and a recipe guide in `docs/`
- **Coupling**: Zero coupling to core ORM — PolicyEntry is a standard `popoto.Model` subclass using public APIs
- **Data ownership**: PolicyEntry owns its own Redis keys. StreamConsumer handler is application code
- **Reversibility**: Trivial — remove example files and guide. No core code changes to revert

## Appetite

**Size:** Medium

**Team:** Solo dev, PM

**Interactions:**
- PM check-ins: 1 (scope confirmation on crystallization thresholds)
- Review rounds: 1 (code review)

The Lua script for TD updates, crystallization logic, and temporal clustering add enough complexity for Medium, but scope is well-defined by the roadmap spec.

## Prerequisites

All 10 primitives + PredictionLedger + StreamConsumer must be shipped (verified: all closed).

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis 5.0+ | `python -c "from src.popoto.redis_db import POPOTO_REDIS_DB; info = POPOTO_REDIS_DB.info('server'); v = info['redis_version']; assert tuple(int(x) for x in v.split('.')[:2]) >= (5, 0)"` | Redis Streams support |
| Popoto primitives | `python -c "import popoto; assert all(hasattr(popoto, x) for x in ['DecayingSortedField', 'ConfidenceField', 'CoOccurrenceField', 'ExistenceFilter', 'StreamConsumer', 'PredictionLedgerMixin', 'ObservationProtocol'])"` | All dependencies available |

## Solution

### Key Elements

- **PolicyEntry model**: Reference model composing AutoKeyField, KeyField, Field, DecayingSortedField, ConfidenceField, CoOccurrenceField — demonstrates how all primitives work together
- **Q-value Lua script**: Atomic temporal difference update — `reward + gamma * max_future_q - current_q` applied to the DecayingSortedField score
- **Crystallization handler**: StreamConsumer handler function that detects repeated successful patterns and creates PolicyEntry records
- **Temporal pattern discovery**: Timestamp clustering using chi-squared test, adds discovered `(period, amplitude, phase)` cycles to CyclicDecayField

### Flow

**Event occurs** → EventStreamMixin writes to stream → **StreamConsumer reads batch** → handler groups by state+action → **≥3 successes with Wilson CI > 0.6** → crystallize PolicyEntry → **Agent encounters similar state** → query PolicyEntry by state_fingerprint → **Select action** → execute → **Observe outcome** → TD update Q-value + update confidence

### Technical Approach

- PolicyEntry lives in `examples/policy_cache/` as a self-contained reference implementation
- Q-value update is a Lua script registered with `POPOTO_REDIS_DB.register_script()` for atomicity
- Crystallization runs as an `async def handler(entries)` passed to `StreamConsumer`
- Wilson confidence interval uses the formula: `(p + z²/2n - z√(p(1-p)/n + z²/4n²)) / (1 + z²/n)` where z=1.96 for 95% CI
- Temporal clustering: bucket timestamps into candidate periods, chi-squared test against uniform, p < 0.05 → add cycle
- Learning rate α=0.1 and discount factor γ=0.95 are magic numbers (documented, tunable via class attributes)
- Discovered cycles use same `(period, amplitude, phase)` format as programmed CyclicDecayField cycles

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] Crystallization handler catches exceptions per-batch — failed crystallizations don't crash the consumer loop
- [ ] Q-value Lua script handles missing keys gracefully (returns 0 for non-existent policies)
- [ ] Temporal clustering handles too few data points (< 3 timestamps) without error

### Empty/Invalid Input Handling
- [ ] Empty stream batch — handler returns immediately, no PolicyEntry created
- [ ] State fingerprint is None or empty — skip crystallization for that group
- [ ] Zero reward signal — Q-value update still runs (Q approaches 0 via TD)
- [ ] Single timestamp — temporal clustering skips (needs ≥3 for chi-squared)

### Error State Rendering
- [ ] No user-visible output — this is backend infrastructure. Errors logged via `logging.getLogger("POPOTO.PolicyCache")`

## Test Impact

No existing tests affected — this is a greenfield feature adding new example files. No existing models, fields, or query methods are modified.

The new test file `tests/test_policy_cache.py` will exercise all primitives in integration but does not modify any existing test.

## Rabbit Holes

- **Optimizing learning rate/discount factor**: α=0.1 and γ=0.95 are good-enough defaults. Hyperparameter tuning is a separate research project, not part of this implementation.
- **Multi-agent policy sharing**: Policies are per-agent via `agent_id` KeyField. Cross-agent policy transfer is Step 12+ territory.
- **Sophisticated state embedding**: State fingerprint is a simple hash. Neural state embeddings or similarity-based matching is out of scope.
- **Online A/B testing of policies**: Exploration vs exploitation strategies (epsilon-greedy, UCB) are application-layer decisions, not part of the reference implementation.

## Risks

### Risk 1: Crystallization threshold too aggressive or too conservative
**Impact:** Too low threshold creates noisy policies from coincidences; too high misses real patterns
**Mitigation:** Wilson CI lower bound > 0.6 with ≥3 observations is conservative. Threshold is a class attribute, easily tuned per deployment.

### Risk 2: Temporal clustering false positives
**Impact:** Discovers spurious cycles from random timestamp clusters
**Mitigation:** Chi-squared test at p < 0.05 is standard. Discovered cycles start with low amplitude and must be reinforced through entrainment to gain strength.

## Race Conditions

### Race 1: Concurrent crystallization of same state+action
**Location:** Crystallization handler
**Trigger:** Two consumer instances process overlapping batches containing the same state+action pattern
**Data prerequisite:** Stream entries with matching state_fingerprint + action_type
**State prerequisite:** No existing PolicyEntry for this state+action pair
**Mitigation:** Use `get_or_create()` for PolicyEntry — if already exists, update Q-value instead of creating duplicate. AutoKeyField composite key on (agent_id, state_fingerprint, action_type) ensures uniqueness.

## No-Gos (Out of Scope)

- No new ORM field types or mixins — this composes existing primitives only
- No changes to StreamConsumer, PredictionLedger, or any core module
- No exploration/exploitation strategy (epsilon-greedy, UCB, Thompson sampling)
- No cross-agent policy sharing or federation
- No neural/embedding-based state similarity
- No persistent storage of raw event windows (crystallize and discard)

## Update System

No update system changes required — this is a library feature (example/recipe) with no deployment infrastructure.

## Agent Integration

No agent integration required — Popoto is a library consumed by applications. There is no bridge, MCP server, or Telegram integration for this project.

## Documentation

### Feature Documentation
- [ ] Create `docs/plans/policy_cache.md` (this document)
- [ ] Create recipe guide at `docs/guides/policy-cache-recipe.md` covering usage, tuning, and composition patterns

### External Documentation Site
- [ ] Add PolicyCache recipe to `mkdocs.yml` navigation
- [ ] Verify `mkdocs serve` builds cleanly

### Inline Documentation
- [ ] Comprehensive docstrings on PolicyEntry model and all methods
- [ ] Inline comments on Lua script explaining TD update math
- [ ] Inline comments on chi-squared temporal clustering logic

## Success Criteria

- [ ] `PolicyEntry` reference model in `examples/policy_cache/models.py` with all primitive compositions
- [ ] Q-value TD update Lua script (atomic, Valkey compatible)
- [ ] Crystallization handler function for StreamConsumer
- [ ] Temporal pattern discovery (timestamp clustering + cycle creation)
- [ ] Full integration test in `tests/test_policy_cache.py` exercising all 11+ primitives
- [ ] Valkey compatible (no Redis modules — verified by running against standard Redis)
- [ ] Recipe guide in `docs/guides/policy-cache-recipe.md`
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (policy-cache)**
  - Name: policy-builder
  - Role: Implement PolicyEntry model, Lua script, crystallization handler, temporal clustering
  - Agent Type: builder
  - Resume: true

- **Builder (tests)**
  - Name: test-builder
  - Role: Write integration tests exercising all primitives through PolicyCache
  - Agent Type: test-engineer
  - Resume: true

- **Validator (policy-cache)**
  - Name: policy-validator
  - Role: Verify implementation correctness, Valkey compatibility, all primitives composed
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Create recipe guide and update mkdocs navigation
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Implement PolicyEntry Model and Lua Script
- **Task ID**: build-policy-model
- **Depends On**: none
- **Validates**: tests/test_policy_cache.py (create)
- **Assigned To**: policy-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `examples/policy_cache/__init__.py` and `examples/policy_cache/models.py`
- Define `PolicyEntry` model with: `entry_id` (AutoKeyField), `agent_id` (KeyField), `state_fingerprint` (KeyField), `state_features` (Field), `action_type` (KeyField), `action_spec` (Field), `expected_value` (DecayingSortedField), `confidence` (ConfidenceField), `related_policies` (CoOccurrenceField)
- Implement Q-value TD update Lua script: `KEYS[1]` = sorted set key, `ARGV[1]` = member, `ARGV[2]` = reward, `ARGV[3]` = max_future_q, `ARGV[4]` = learning_rate (0.1), `ARGV[5]` = discount_factor (0.95)
- Implement `update_q_value(policy, reward, max_future_q)` method
- Implement crystallization handler: `async def crystallize_handler(entries)` that groups by state+action, counts successes, checks Wilson CI, creates PolicyEntry via `get_or_create()`
- Implement temporal pattern discovery: `discover_temporal_patterns(timestamps, min_observations=3, p_threshold=0.05)` using chi-squared test
- Add `crystallize_handler` as a ready-to-use StreamConsumer handler

### 2. Write Integration Tests
- **Task ID**: build-tests
- **Depends On**: build-policy-model
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Create `tests/test_policy_cache.py`
- Test PolicyEntry CRUD (create, read, update, delete)
- Test Q-value TD update: verify score changes atomically
- Test crystallization: feed ≥3 matching events → verify PolicyEntry created
- Test crystallization threshold: feed 2 events → verify no PolicyEntry
- Test temporal clustering: feed clustered timestamps → verify cycle discovered
- Test full integration: event → stream → consumer → crystallize → query → TD update → confidence update
- Test concurrent crystallization: verify get_or_create prevents duplicates
- Test edge cases: empty batch, single timestamp, zero reward

### 3. Validate Implementation
- **Task ID**: validate-policy
- **Depends On**: build-tests
- **Assigned To**: policy-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify all primitives are composed (DecayingSortedField, ConfidenceField, CoOccurrenceField, KeyField, AutoKeyField, Field)
- Verify Lua script uses no Redis module commands
- Verify crystallization handler follows StreamConsumer handler protocol
- Verify tests pass
- Run `python -m ruff check .` and `python -m ruff format --check .`

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-policy
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/guides/policy-cache-recipe.md` with usage examples, tuning guide, and composition patterns
- Add entry to `mkdocs.yml` navigation under guides
- Verify `mkdocs serve` builds

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: policy-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify all success criteria met
- Verify documentation exists and builds
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_policy_cache.py -x -q` | exit code 0 |
| All tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check .` | exit code 0 |
| Format clean | `python -m ruff format --check .` | exit code 0 |
| Model exists | `python -c "from examples.policy_cache.models import PolicyEntry; print('OK')"` | output contains OK |
| Guide exists | `test -f docs/guides/policy-cache-recipe.md` | exit code 0 |

## Open Questions

1. Should PolicyEntry live in `examples/policy_cache/` (as the issue suggests — "application-layer pattern, shipped as example/recipe") or should it be promoted to `src/popoto/` as a first-class module? The roadmap says "reference implementation" which suggests examples.

2. The issue mentions `related_policies = CoOccurrenceField()` — should co-occurrence track which policies are frequently selected together (useful for multi-step plans), or is this optional decoration that can be deferred?

3. Should the temporal pattern discovery be a standalone utility function (reusable beyond PolicyCache) or tightly coupled to the crystallization handler?
