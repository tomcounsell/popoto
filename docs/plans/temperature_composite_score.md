---
status: Planning
type: feature
appetite: Small
owner: Valor
created: 2026-03-20
tracking: https://github.com/tomcounsell/popoto/issues/236
last_comment_id:
---

# Temperature Parameter for composite_score()

## Problem

When retrieving agent memories via `composite_score()`, the raw weighted sum determines ranking. There is no way to control the *sharpness* of score discrimination -- whether the top result dominates or results are spread more evenly.

**Current behavior:**
`composite_score()` returns candidates ranked by raw ZUNIONSTORE output. The score distribution is fixed -- callers cannot tune whether retrieval is sharp (best match only) or exploratory (diverse spread).

**Desired outcome:**
A `temperature` parameter on `composite_score()` that scales scores post-ZUNIONSTORE. Low temperature (0.02-0.1) sharpens discrimination so the top result dominates. Default temperature (1.0) preserves current behavior. High temperature (2.0+) flattens scores toward uniform, enabling diversity.

```python
# Sharp retrieval -- top result dominates
results = Memory.query.composite_score(
    indexes={"relevance": 0.4, "certainty": 0.3},
    temperature=0.1,
    limit=5,
)

# Default -- unchanged behavior
results = Memory.query.composite_score(
    indexes={"relevance": 0.4, "certainty": 0.3},
    limit=5,
)

# Exploratory -- diverse spread
results = Memory.query.composite_score(
    indexes={"relevance": 0.4, "certainty": 0.3},
    temperature=3.0,
    limit=5,
)
```

## Prior Art

- **PR #222**: `CompositeScoreQuery` -- shipped the `composite_score()` method with ZUNIONSTORE, index resolution, and model hydration. This is the method being extended.
- **PR #223**: Docs for composite_score() -- added API reference and feature documentation.
- **Issue #234**: Magic number validation experiments -- temperature default (1.0) is a Category 1 magic number that should be validated.
- **Issue #233**: ContextAssembler -- planned feature that will eventually expose temperature as a retrieval mode knob. This plan adds the underlying parameter; ContextAssembler integration is out of scope.

No prior attempts at temperature scaling found.

## Data Flow

1. **Entry point**: Application calls `composite_score(..., temperature=0.1)`
2. **ZUNIONSTORE**: Raw composite scores computed as before (unchanged)
3. **ZREVRANGE**: Top-K members extracted with scores (unchanged)
4. **Temperature scaling** (NEW): Each score divided by temperature: `adjusted = score / temperature`
5. **Re-sort**: Results re-sorted by adjusted scores (order may change when aggregate=SUM with multiple indexes)
6. **Post-filter**: Applied to adjusted scores (unchanged flow)
7. **Hydration**: Model instances loaded (unchanged)

Note: Temperature scaling happens in Python after ZREVRANGE, not inside Redis. This keeps the Redis pipeline unchanged and avoids Lua complexity. The re-sort after scaling only matters when `limit` is smaller than the total result set and temperature changes relative ordering -- but since ZREVRANGE already fetches top-K by raw score, and division by a constant doesn't change ordering (it's monotonic), the ranking is preserved. The value of temperature is in the *score values* themselves, which downstream consumers (like a future ContextAssembler) can use for selection probability or thresholding.

## Architectural Impact

- **New dependencies**: None
- **Interface changes**: One new optional parameter `temperature: float = 1.0` on `composite_score()` (both QueryBuilder and Query classes)
- **Coupling**: None -- temperature is applied as a post-processing step on scores
- **Data ownership**: No change
- **Reversibility**: Trivial -- remove one parameter and one line of score division

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

No prerequisites -- this work builds on the shipped `composite_score()` method (PR #222).

## Solution

### Key Elements

- **`temperature` parameter**: New optional float parameter on `composite_score()`, default 1.0
- **Score scaling**: After ZUNIONSTORE + ZREVRANGE, divide each score by temperature
- **Validation**: temperature must be > 0 (division by zero protection)

### Flow

**composite_score() call** → validate temperature > 0 → existing ZUNIONSTORE pipeline → ZREVRANGE with scores → divide each score by temperature → existing post-filter/hydration flow

### Technical Approach

The implementation is minimal -- a single validation check and a score transformation applied to the `raw_results` list after ZREVRANGE:

1. Add `temperature: float = 1.0` parameter to both `composite_score()` signatures
2. Validate `temperature > 0` at the top of the method (raise `QueryException` if not)
3. After ZREVRANGE returns `(member, score)` tuples, transform: `score = score / temperature`
4. Pass adjusted scores through existing post-filter and hydration

Since dividing all scores by a positive constant preserves ordering, this does not affect which results are returned or their rank. The adjusted scores are what downstream consumers see, enabling probability-based selection or adaptive thresholding.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `temperature=0` raises QueryException (division by zero)
- [ ] `temperature=-1` raises QueryException (negative temperature meaningless)
- [ ] No new exception handlers added; existing error paths unchanged

### Empty/Invalid Input Handling
- [ ] `temperature=1.0` (default) produces identical results to current behavior
- [ ] Very small temperature (0.001) does not cause overflow (scores are floats)
- [ ] Very large temperature (1000.0) does not lose precision below float epsilon

### Error State Rendering
- [ ] Not applicable -- ORM query method, not user-facing UI

## Test Impact

No existing tests affected -- the new parameter defaults to 1.0 which preserves current behavior. All existing tests pass without modification because `temperature=1.0` means `score / 1.0 = score` (identity operation).

## Rabbit Holes

- **Softmax normalization**: Don't implement softmax (exp(score/T) / sum). Simple division is sufficient for score scaling and avoids numerical stability issues. Softmax is a ContextAssembler concern if needed later.
- **Per-index temperature**: Don't add temperature to individual indexes. One global temperature on the composite score is the right granularity.
- **ContextAssembler integration**: Explicitly deferred to issue #233. This plan only adds the parameter to `composite_score()`.
- **Temperature in Lua/Redis**: Don't move temperature scaling into Redis. Python post-processing is simpler and doesn't affect performance (it's O(K) where K is the limit, typically 10-50).

## Risks

### Risk 1: Score semantics confusion
**Impact:** Users may expect temperature to work like LLM temperature (affecting sampling probability) rather than simple score scaling.
**Mitigation:** Clear docstring explaining that temperature scales scores linearly (`score / temperature`), not probabilistically. Reference CLaRa paper for context.

## Race Conditions

No race conditions identified -- temperature scaling is a stateless, in-process transformation applied to query results. No shared mutable state or async operations involved.

## No-Gos (Out of Scope)

- Softmax or probability-based normalization
- Per-index temperature control
- ContextAssembler integration (issue #233)
- Autoexperiment validation of optimal default (issue #234)
- Temperature presets / named modes (e.g., "sharp", "exploratory")

## Update System

No update system changes required -- this is a library feature in the Popoto ORM package.

## Agent Integration

No agent integration required -- this is an ORM query method consumed by application code. The ai repo's `ContextAssembler` (issue #233) will eventually use this parameter, but that integration is out of scope here.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/composite-score-query.md` to document the `temperature` parameter
- [ ] Update the API reference table in `docs/api-reference.md` to include `temperature`

### External Documentation Site
- [ ] Update mkdocs pages if the composite-score-query page is in mkdocs.yml

### Inline Documentation
- [ ] Docstring on `composite_score()` updated with `temperature` parameter docs and example

## Success Criteria

- [ ] `temperature` parameter on `composite_score()` (default 1.0 = current behavior)
- [ ] `temperature=0` and negative values raise QueryException
- [ ] Low temperature produces larger score spread (scores divided by small number)
- [ ] High temperature produces smaller score spread (scores divided by large number)
- [ ] Default temperature (1.0) produces identical results to current behavior
- [ ] Backward compatible -- no existing tests break
- [ ] Tests verifying score values change with temperature
- [ ] Documentation updated
- [ ] Tests pass (`/do-test`)

## Team Orchestration

### Team Members

- **Builder (temperature)**
  - Name: temperature-builder
  - Role: Add temperature parameter to composite_score(), validation, score scaling
  - Agent Type: builder
  - Resume: true

- **Validator (temperature)**
  - Name: temperature-validator
  - Role: Verify implementation, run tests, check backward compatibility
  - Agent Type: validator
  - Resume: true

### Available Agent Types

**Tier 1 — Core (default choices):**
- `builder` - General implementation
- `validator` - Read-only verification
- `test-engineer` - Test implementation

## Step by Step Tasks

### 1. Add temperature parameter and score scaling
- **Task ID**: build-temperature
- **Depends On**: none
- **Validates**: tests/test_composite_score_query.py
- **Assigned To**: temperature-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `temperature: float = 1.0` parameter to `QueryBuilder.composite_score()` in `src/popoto/models/query.py`
- Add `temperature: float = 1.0` parameter to `Query.composite_score()` convenience method
- Add validation: `if temperature <= 0: raise QueryException(...)`
- After ZREVRANGE, apply `score = score / temperature` to each result tuple
- Pass `temperature` through from Query to QueryBuilder
- Update docstrings with temperature parameter documentation and example

### 2. Add temperature tests
- **Task ID**: build-tests
- **Depends On**: build-temperature
- **Validates**: tests/test_composite_score_query.py
- **Assigned To**: temperature-builder
- **Agent Type**: builder
- **Parallel**: false
- Test: `temperature=1.0` produces identical results to no temperature argument
- Test: `temperature=0` raises QueryException
- Test: `temperature=-1` raises QueryException
- Test: `temperature=0.1` produces scores 10x larger than `temperature=1.0`
- Test: `temperature=10.0` produces scores 10x smaller than `temperature=1.0`
- Test: result ordering is preserved regardless of temperature value

### 3. Update documentation
- **Task ID**: document-feature
- **Depends On**: build-tests
- **Assigned To**: temperature-builder
- **Agent Type**: builder
- **Parallel**: false
- Update `docs/features/composite-score-query.md` parameter table and API reference
- Update `docs/api-reference.md` composite_score section if it exists

### 4. Final validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: temperature-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify all success criteria met
- Verify documentation is accurate

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_composite_score_query.py -v` | exit code 0 |
| All tests pass | `pytest tests/ -x -q` | exit code 0 |
| Import works | `python -c "from src.popoto.models.query import QueryBuilder; import inspect; sig = inspect.signature(QueryBuilder.composite_score); assert 'temperature' in sig.parameters"` | exit code 0 |
| Format clean | `python -m ruff format --check src/ tests/` | exit code 0 |
| Lint clean | `python -m ruff check src/ tests/` | exit code 0 |

---

## Open Questions

1. **Score scaling vs. ranking impact**: Since dividing all scores by a constant preserves ordering, temperature only affects the *values* of scores, not the ranking. This is useful for downstream probability-based selection (e.g., ContextAssembler sampling proportional to score). Should we also support a mode where temperature affects ranking (e.g., by adding noise scaled by temperature)? Leaning toward no -- pure score scaling is simpler, deterministic, and sufficient for the stated use case.

2. **withscores exposure**: Currently `composite_score()` returns model instances without exposing scores. For temperature to be useful to callers, they need to see the adjusted scores. Should this plan also add a `withscores=True` option that returns `(instance, score)` tuples? Or defer that to a separate issue? Leaning toward deferring -- the immediate value is for ContextAssembler (issue #233), which will have internal access to scores.
