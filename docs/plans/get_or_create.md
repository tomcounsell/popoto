---
status: In Progress
type: feature
appetite: Medium
owner: valorengels
created: 2026-02-11
tracking: https://github.com/tomcounsell/popoto/issues/125
---

# get_or_create / update_or_create Methods

## Problem

Users repeatedly implement the same boilerplate pattern for atomic-ish lookups with fallback creation.

**Current behavior:**

```python
# 8 lines of boilerplate every time
tracker = await AutoContinueTracker.async_get(session_id=session_id)
if tracker:
    tracker.count += 1
    await tracker.async_save()
else:
    tracker = await AutoContinueTracker.async_create(session_id=session_id, count=1)
```

**Desired outcome:**

```python
# Django-style convenience
tracker, created = await AutoContinueTracker.async_get_or_create(
    session_id=session_id,
    defaults={'count': 1}
)
if not created:
    tracker.count += 1
    await tracker.async_save()

# Or for the full pattern:
tracker, created = await AutoContinueTracker.async_update_or_create(
    session_id=session_id,
    defaults={'count': 1},
    update={'count': F('count') + 1}  # future enhancement
)
```

## Appetite

**Size:** Medium

**Team:** Solo dev + PM

**Interactions:**
- PM check-ins: 1 (scope alignment on atomicity guarantees)
- Review rounds: 1

The implementation is straightforward but has subtle atomicity considerations. Redis doesn't have native get-or-create, so we need to decide: simple check-then-create (accepts race conditions) vs. Lua script (true atomicity). For v1, simple approach with documented limitations is pragmatic.

## Prerequisites

No prerequisites — this work has no external dependencies.

## Solution

### Key Elements

- **`Model.get_or_create(**lookup, defaults={})`**: Returns `(instance, created)` tuple. Looks up by `lookup` kwargs, creates with `lookup + defaults` if not found.
- **`Model.update_or_create(**lookup, defaults={})`**: Returns `(instance, created)` tuple. If found, updates with `defaults` and saves. If not found, creates with `lookup + defaults`.
- **Async variants**: `async_get_or_create`, `async_update_or_create` using `to_thread()` wrapper pattern.

### Decisions (Approved)

1. **Retry once on unique constraint violation** — matches Django pattern
2. **Plain tuple return** — `(instance, created)` for simplicity
3. **Rely on existing field validation** — `query.get()` already validates

### Flow

**get_or_create:**
```
query.get(**lookup) → found? → return (instance, False)
                    → not found? → create(**lookup, **defaults) → return (instance, True)
                                 → unique violation? → retry get() → return (instance, False)
```

**update_or_create:**
```
query.get(**lookup) → found? → update fields from defaults → save() → return (instance, False)
                    → not found? → create(**lookup, **defaults) → return (instance, True)
```

### API Design

```python
@classmethod
def get_or_create(cls, defaults=None, **lookup) -> Tuple["Model", bool]:
    """
    Look up an object by lookup kwargs. If not found, create it with
    lookup + defaults.

    Args:
        defaults: Field values to use only when creating (not for lookup)
        **lookup: Field values to use for lookup AND creation

    Returns:
        (instance, created) tuple where created is True if object was created
    """

@classmethod
def update_or_create(cls, defaults=None, **lookup) -> Tuple["Model", bool]:
    """
    Look up an object by lookup kwargs. If found, update it with defaults
    and save. If not found, create with lookup + defaults.

    Args:
        defaults: Field values to update (if exists) or use for creation
        **lookup: Field values to use for lookup AND creation

    Returns:
        (instance, created) tuple where created is True if object was created
    """
```

## Rabbit Holes

- **Don't implement Lua-based atomic version in v1**
- **Don't add `create_defaults` parameter**
- **Don't add `F()` expressions for updates**
- **Don't add bulk variants**

## Risks

### Risk 1: Race condition between get and create
**Mitigation:** Catch unique constraint exception, retry get() once.

### Risk 2: update_or_create doesn't trigger all field hooks correctly
**Mitigation:** Use `setattr()` + `save()` which goes through the full save path.

## No-Gos (Out of Scope)

- Lua script atomic implementation (v2 consideration)
- `F()` expressions for update values
- `create_defaults` separate from `defaults`
- Bulk variants

## Success Criteria

- [ ] `Model.get_or_create(**lookup, defaults={})` returns `(instance, True)` when creating
- [ ] `Model.get_or_create(**lookup)` returns `(instance, False)` when object exists
- [ ] `Model.update_or_create(**lookup, defaults={})` updates existing and returns `(instance, False)`
- [ ] `Model.update_or_create(**lookup, defaults={})` creates new and returns `(instance, True)` when not found
- [ ] `async_get_or_create` and `async_update_or_create` work correctly
- [ ] Race condition retry: if unique constraint hit after get returned None, retry succeeds
- [ ] All field hooks (on_save for KeyField, SortedField, etc.) are triggered correctly
- [ ] All existing tests continue to pass
