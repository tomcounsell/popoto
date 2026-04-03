---
status: Ready
type: chore
appetite: Small
owner: Valor
created: 2026-04-03
tracking: https://github.com/tomcounsell/popoto/issues/340
last_comment_id:
---

# Add Async Examples to Docs for get_many, check_indexes, clean_indexes

## Problem

The async variants `async_get_many()`, `async_check_indexes()`, and `async_clean_indexes()` are implemented and tested but the documentation pages only show synchronous examples or minimal one-liner references.

**Current behavior:**
- `docs/query.md` has a full "Get Multiple Objects by Key" section (lines 96-131) with sync examples but no async subsection
- `docs/api-reference.md` line 868 mentions `async_get_many` in a single sentence but provides no code example
- `docs/api-reference.md` Async Query Methods table (lines 1095-1103) omits `async_get_many`
- `docs/api-reference.md` lines 743-759 have a brief async index maintenance table and minimal snippet for `async_check_indexes` / `async_clean_indexes`, but no detailed standalone examples with context

**Desired outcome:**
- Each async method has a clear code example in the same doc page as its sync counterpart
- The Async Query Methods table includes `async_get_many`
- Developers can copy-paste working async examples from the docs

## Prior Art

No prior issues found related to async documentation gaps. The async methods were added across PRs #318 (get_many), #332 (check_indexes), and #334 (clean_indexes). Each PR added the implementation and tests but deferred documentation updates.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

No prerequisites -- this work has no external dependencies.

## Solution

### Key Elements

- **query.md async subsection**: Add an async code block after the sync `get_many()` section
- **api-reference.md async_get_many row**: Add `async_get_many` to the Async Query Methods table
- **api-reference.md index maintenance examples**: Expand the existing async index maintenance snippet with fuller examples matching the pattern used in `docs/async.md`

### Technical Approach

Follow the documentation pattern established in `docs/async.md`:
1. Use the food-delivery domain models (Restaurant, Customer, Driver, Order) already defined in async.md
2. Show `await` usage inside `async def` functions
3. Include practical context (when to use, what it returns)
4. Use `!!! tip` or `!!! note` admonitions for guidance

### Files to Modify

#### 1. `docs/query.md` -- Add async subsection after "Get Multiple Objects by Key"

After the existing sync examples (around line 131), add an async subsection:

```markdown
### Async Usage

The async counterpart uses a native async Redis pipeline for non-blocking bulk retrieval.

\```python
async def bulk_lookup():
    keys = ["Restaurant:Burger Palace", "Restaurant:Sushi Zen", "Restaurant:Gone Place"]
    restaurants = await Restaurant.query.async_get_many(redis_keys=keys)
    # => [<Restaurant>, <Restaurant>, None]

    # Drop missing entries
    restaurants = await Restaurant.query.async_get_many(redis_keys=keys, skip_none=True)
    # => [<Restaurant>, <Restaurant>]
\```

See [Async Operations](async.md#async_get_many) for more examples.
```

#### 2. `docs/api-reference.md` -- Add `async_get_many` to Async Query Methods table

Add a row to the table at line 1103:

```markdown
| `Model.query.get_many(...)` | `await Model.query.async_get_many(...)` |
```

#### 3. `docs/api-reference.md` -- Expand async index maintenance examples

Replace the minimal snippet at lines 751-759 with fuller examples that show each method individually with context, matching the style used in `docs/async.md`:

```markdown
### Async Index Maintenance

| Sync | Async |
|------|-------|
| `Model.check_indexes()` | `await Model.async_check_indexes()` |
| `Model.clean_indexes()` | `await Model.async_clean_indexes()` |
| `Model.rebuild_indexes()` | `await Model.async_rebuild_indexes()` |

\```python
async def health_check():
    """Non-blocking index health audit."""
    result = await User.async_check_indexes()
    print(f"Orphaned entries: {result['total']}")
    # => {'class_set': 0, 'key_fields': {}, 'sorted_fields': {}, ...}

async def scheduled_cleanup():
    """Production-safe orphan removal in an async worker."""
    result = await User.async_check_indexes()
    if result['total'] > 0:
        removed = await User.async_clean_indexes()
        print(f"Cleaned {removed} orphans")

        # Verify cleanup
        after = await User.async_check_indexes()
        assert after['total'] == 0
\```
```

## Failure Path Test Strategy

### Exception Handling Coverage
- No exception handlers in scope -- this is purely a documentation change

### Empty/Invalid Input Handling
- Not applicable -- documentation only

### Error State Rendering
- Not applicable -- documentation only

## Test Impact

No existing tests affected -- this is a documentation-only change with no code modifications.

## Rabbit Holes

- Rewriting the entire async.md page for consistency -- out of scope, async.md already has good examples for these methods
- Adding async tabs/toggles with mkdocs-material content tabs -- would be nice but is a separate infrastructure decision
- Documenting async_rebuild_indexes in detail -- already covered adequately in the existing table

## Risks

### Risk 1: Stale examples
**Impact:** Code examples that don't match actual API behavior
**Mitigation:** Examples mirror the patterns already validated in `docs/async.md` and `tests/test_async.py`

## Race Conditions

No race conditions identified -- documentation-only change.

## No-Gos (Out of Scope)

- Refactoring existing async.md content
- Adding mkdocs content tabs for sync/async toggle
- Writing new tests
- Modifying any Python source code

## Update System

No update system changes required -- this is a documentation-only change.

## Agent Integration

No agent integration required -- documentation only.

## Documentation

### External Documentation Site
- [x] The changes ARE the documentation updates
- [ ] Verify docs build passes with `mkdocs serve`

## Success Criteria

- [ ] `docs/query.md` has async subsection with `async_get_many()` example under "Get Multiple Objects by Key"
- [ ] `docs/api-reference.md` Async Query Methods table includes `async_get_many` row
- [ ] `docs/api-reference.md` async index maintenance section has expanded examples for `async_check_indexes()` and `async_clean_indexes()`
- [ ] All examples follow the food-delivery domain pattern from `docs/async.md`
- [ ] `mkdocs serve` builds without errors
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (docs)**
  - Name: docs-builder
  - Role: Add async examples to query.md and api-reference.md
  - Agent Type: documentarian
  - Resume: true

- **Validator (docs)**
  - Name: docs-validator
  - Role: Verify examples match API and docs build
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Add async subsection to query.md
- **Task ID**: build-query-docs
- **Depends On**: none
- **Assigned To**: docs-builder
- **Agent Type**: documentarian
- **Parallel**: true
- Add async subsection after "Get Multiple Objects by Key" section (after line 131)
- Use Restaurant model examples consistent with existing sync examples on the page

### 2. Update api-reference.md async query table
- **Task ID**: build-api-ref-table
- **Depends On**: none
- **Assigned To**: docs-builder
- **Agent Type**: documentarian
- **Parallel**: true
- Add `async_get_many` row to the Async Query Methods table (after line 1103)

### 3. Expand api-reference.md async index maintenance examples
- **Task ID**: build-api-ref-indexes
- **Depends On**: none
- **Assigned To**: docs-builder
- **Agent Type**: documentarian
- **Parallel**: true
- Replace minimal snippet at lines 751-759 with fuller examples
- Show async_check_indexes and async_clean_indexes with practical context

### 4. Validate docs build
- **Task ID**: validate-docs
- **Depends On**: build-query-docs, build-api-ref-table, build-api-ref-indexes
- **Assigned To**: docs-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `mkdocs serve` and verify no build errors
- Verify all three async sections render correctly

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Docs build | `cd /Users/valorengels/src/popoto && mkdocs build --strict 2>&1` | exit code 0 |
| async_get_many in query.md | `grep -c 'async_get_many' docs/query.md` | output > 0 |
| async_get_many in api-reference table | `grep 'async_get_many' docs/api-reference.md` | exit code 0 |
| async_check_indexes example | `grep -c 'async_check_indexes' docs/api-reference.md` | output > 0 |
| async_clean_indexes example | `grep -c 'async_clean_indexes' docs/api-reference.md` | output > 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

None -- the scope is narrow and all patterns are already established in `docs/async.md`.
