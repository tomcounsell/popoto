---
status: Planning
type: chore
appetite: Small
owner: agent
created: 2026-04-03
tracking: https://github.com/tomcounsell/popoto/issues/339
last_comment_id:
---

# Update Kitchen Sink Example App with v1.4.4 Features

## Problem

The kitchen sink example app at `examples/popoto_kitchen/` was built before the 8 PRs merged on 2026-03-31 and does not demonstrate any of the new v1.4.4 features. Developers looking at the example app to learn Popoto miss coverage of ConfidenceField with partition_by, get_many(), check_indexes()/clean_indexes(), and the companion hash key public API.

**Current behavior:**
The example app demonstrates: KeyField, AutoKeyField, UniqueKeyField, SortedField (with partition_by), GeoField, Relationship, Model Meta options, and partial save with update_fields. It has no usage of ConfidenceField, get_many(), check_indexes(), clean_indexes(), or companion hash key inspection methods.

**Desired outcome:**
The example app demonstrates all four new features with runnable code that works against a local Redis/Valkey instance. New features fit naturally into the existing food delivery domain.

## Prior Art

- **PR #112**: Add Popoto Kitchen TUI example application -- Created the original TUI app with models, seed data, and Textual screens.
- **PR #168**: Demo: add edge case mutation flows to Popoto Kitchen (PRs #159-#163) -- Updated the kitchen app to exercise SortedField partition_by fix and partial save. Established the pattern for how to add feature demos to the existing app.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

No prerequisites -- this work has no external dependencies. Requires only a local Redis/Valkey instance (same as the existing example app).

## Solution

### Key Elements

- **ReviewScore model**: A new model using ConfidenceField with `partition_by="restaurant"` to track review confidence per restaurant. This fits the food delivery domain naturally -- confidence in a review score increases with more evidence.
- **Operations script**: A new `operations.py` module with functions demonstrating `check_indexes()`, `clean_indexes()`, and `get_many()` as operational workflows.
- **Companion hash key inspection**: Included in operations.py to show how `get_data_hash_key()` reveals the underlying Redis key structure.
- **Seed data updates**: Seed the new ReviewScore model with sample confidence data.

### Flow

**Seed data** -> ReviewScore instances created with ConfidenceField -> `update_confidence()` called with signals -> **Operations script** -> `check_indexes()` reports health -> `clean_indexes()` removes orphans -> `get_many()` bulk-loads orders -> companion hash key inspection shows Redis internals

### Technical Approach

- Add `ReviewScore` model to `models.py` with `ConfidenceField(partition_by="restaurant")`
- Add `operations.py` with standalone functions that can be called from `__main__.py --ops`
- Extend `seed.py` to create ReviewScore instances and apply confidence signals
- Keep TUI changes minimal -- this is primarily about the model and script layer, not new screens

## Failure Path Test Strategy

### Exception Handling Coverage
- No exception handlers in scope -- this is example code, not library code. Example scripts will print errors to stdout rather than catching silently.

### Empty/Invalid Input Handling
- The operations script will handle the case where no models exist (empty database) by printing a message and returning early.

### Error State Rendering
- Not applicable -- no new TUI screens. Operations output goes to stdout.

## Test Impact

No existing tests affected -- this is purely additive example code. The example app has no test suite of its own; correctness is verified by running the app against Redis.

## Rabbit Holes

- Adding a new TUI screen/tab for operations. The operations are best demonstrated as CLI scripts, not interactive widgets. A TUI screen would require significant Textual code for marginal demo value.
- Making ConfidenceField queryable/sortable in the TUI. The demo should show the API, not build a full analytics dashboard.
- Adding tests for the example app itself. The example app is documentation, not production code.

## Risks

### Risk 1: ConfidenceField requires cmsgpack in Redis Lua
**Impact:** The Bayesian update Lua script uses `cmsgpack.unpack`/`cmsgpack.pack`, which are built into Redis but may not be in all Valkey builds.
**Mitigation:** This is an existing constraint of ConfidenceField, not new to this work. The example documents this requirement.

## Race Conditions

No race conditions identified -- example scripts run sequentially in a single process.

## No-Gos (Out of Scope)

- No new TUI screens or tabs
- No modifications to existing screens (dashboard, restaurants, menu, orders, drivers)
- No async operations or pub/sub demos
- No EmbeddingField or ContentField demos (require external providers)

## Update System

No update system changes required -- this is example code only.

## Agent Integration

No agent integration required -- this is an example application.

## Documentation

### Inline Documentation
- [ ] Docstrings on ReviewScore model explaining ConfidenceField usage
- [ ] Docstrings on operations.py functions explaining each feature demo
- [ ] Comments in seed.py explaining confidence signal patterns

## Success Criteria

- [ ] At least one model using ConfidenceField with partition_by
- [ ] Example script demonstrating get_many() usage
- [ ] Example of check_indexes() followed by clean_indexes() operational workflow
- [ ] Companion hash key inspection via get_data_hash_key() demonstrated
- [ ] All examples runnable against a local Redis/Valkey instance
- [ ] Existing app functionality unchanged
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (kitchen-sink)**
  - Name: kitchen-builder
  - Role: Add new model, operations script, and seed data
  - Agent Type: builder
  - Resume: true

- **Validator (kitchen-sink)**
  - Name: kitchen-validator
  - Role: Verify all examples run against Redis
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Add ReviewScore Model
- **Task ID**: build-model
- **Depends On**: none
- **Validates**: manual run of seed script
- **Assigned To**: kitchen-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `ReviewScore` model to `examples/popoto_kitchen/models.py` with:
  - `restaurant` as a `KeyField()` (partition key for the restaurant name)
  - `reviewer` as a `KeyField()` (the customer username)
  - `score` as a `ConfidenceField(initial_confidence=0.5, partition_by="restaurant")`
- Import `ConfidenceField` from `popoto`
- Add `ReviewScore` to imports in `seed.py` and `__init__.py` if needed

### 2. Add Operations Script
- **Task ID**: build-operations
- **Depends On**: none
- **Assigned To**: kitchen-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `examples/popoto_kitchen/operations.py` with functions:
  - `demo_get_many()` -- fetch multiple orders by key in one pipeline call
  - `demo_check_and_clean_indexes()` -- run check_indexes() on all models, then clean_indexes() if orphans found
  - `demo_companion_hash_keys()` -- show get_data_hash_key() on ReviewScore instances
  - `run_all()` -- run all demos with labeled output
- Each function prints its results to stdout with clear section headers

### 3. Update Seed Script
- **Task ID**: build-seed
- **Depends On**: build-model
- **Assigned To**: kitchen-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `seed_review_scores()` function to `seed.py`:
  - Create ReviewScore instances for a subset of customer/restaurant pairs
  - Call `ConfidenceField.update_confidence()` with varied signals (0.2-0.9) to build evidence history
  - Print count of review scores seeded
- Call `seed_review_scores()` from `seed_database()`
- Add `ReviewScore` to `clear_database()`

### 4. Update Entry Point
- **Task ID**: build-entrypoint
- **Depends On**: build-operations
- **Assigned To**: kitchen-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `--ops` flag to `__main__.py` argparse
- When `--ops` is passed, import and call `operations.run_all()` then exit
- Update module docstring with new usage example

### 5. Validate All Features
- **Task ID**: validate-all
- **Depends On**: build-seed, build-entrypoint
- **Assigned To**: kitchen-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `python -m popoto_kitchen --seed-only --clear` to seed fresh data
- Run `python -m popoto_kitchen --ops` to execute all operation demos
- Verify each demo produces output without errors
- Verify existing app still launches with `python -m popoto_kitchen`

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Models importable | `python -c "from examples.popoto_kitchen.models import ReviewScore"` | exit code 0 |
| Operations importable | `python -c "from examples.popoto_kitchen.operations import run_all"` | exit code 0 |
| Lint clean | `python -m ruff check examples/popoto_kitchen/` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

1. Should the operations script also demonstrate `get_many()` with `skip_none=True` to show both modes, or is a single call sufficient?
2. Is the ReviewScore model (keyed by restaurant + reviewer) the right domain fit, or would a different model better showcase partition_by?
