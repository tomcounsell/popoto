---
status: Ready
type: bugfix
appetite: Small
owner: Valor
created: 2026-03-31
tracking: https://github.com/tomcounsell/popoto/issues/317
last_comment_id:
---

# Query.get() should accept a plain redis_key string positionally

## Problem

`Model.query.get(redis_key_string)` raises `AttributeError: 'str' object has no attribute 'redis_key'` because the positional argument binds to the `db_key` parameter (a `DB_key` type), not `redis_key`.

The `get()` signature is:

```python
def get(self, db_key: DB_key = None, redis_key: str = None, **kwargs)
```

When a caller passes a plain string like `"Memory:abc123"` positionally, it lands in `db_key`. Line 1528 then calls `db_key.redis_key`, which fails because `str` has no `.redis_key` attribute. If the caller wraps the call in `try/except`, the error is silently swallowed and `get()` appears to return `None` for every key -- even when the objects exist in Redis.

This is a natural usage pattern: users who have a redis_key string from a previous query or external source will intuitively pass it as the first argument.

## Prior Art

- **Issue #317**: Reports the bug with test cases showing the expected behavior.
- No prior PRs addressing this.

## Solution

### Key Elements

1. **Type-check `db_key` at the top of `get()`**: Before any logic runs, detect when `db_key` is a plain string (not a `DB_key` instance) and reinterpret it as `redis_key`:

```python
# In get(), before existing logic (around line 1520):
if isinstance(db_key, str) and not redis_key:
    redis_key = db_key
    db_key = None
```

This is a minimal, safe change:
- It only activates when `db_key` is a plain `str` and `redis_key` was not explicitly provided.
- `DB_key` extends `list`, so `isinstance(db_key, str)` is `False` for actual `DB_key` instances -- no false positives.
- If someone passes both a positional string and `redis_key=`, the explicit keyword wins (the positional string is discarded). This matches Python convention where explicit keyword arguments take precedence.

2. **No signature change**: The type hints and parameter names stay the same. This is purely a runtime accommodation of a common misuse pattern.

## Architectural Impact

- **Files changed**: `src/popoto/models/query.py` (3 lines added)
- **New dependencies**: None
- **Interface changes**: None. The method signature is unchanged. This makes an existing signature more forgiving.
- **Reversibility**: Trivial. Remove the 3-line guard clause.

## Appetite

**Size:** Tiny (3-line fix + tests)

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis running | `redis-cli ping` | Tests require Redis |

## Scope

### In Scope

- Add type-check guard in `Query.get()` to accept positional string as `redis_key`
- Add test coverage for positional string, `DB_key`, and keyword `redis_key` usage

### Out of Scope

- Changing the method signature or parameter names
- Supporting other positional argument types (e.g., `int`, `bytes`)
- Deprecation warnings (this is a bugfix, not a migration)

## No-Gos

- Do not rename or reorder parameters (would break existing callers using keyword arguments)
- Do not add deprecation warnings for the positional pattern (it should just work)
- Do not change the behavior when `db_key` is an actual `DB_key` instance

## Update System

No update system changes required. This is an internal library bugfix. Users install popoto via pip/uv; the fix ships with the next release.

## Agent Integration

No agent integration required. This is a bugfix internal to the `popoto` library's `Query.get()` method. No MCP servers, bridge changes, or tool wrappers are involved.

## Tasks

- [ ] Add type-check guard at top of `Query.get()` in `src/popoto/models/query.py` (~line 1520)
- [ ] Add test file `tests/test_query_get_positional.py` with test cases
- [ ] Verify all existing tests still pass

## Failure Path Test Strategy

| Failure | Expected behavior | Test |
|---------|-------------------|------|
| Plain redis_key string passed positionally | Treated as `redis_key`, object returned | `test_get_positional_redis_key_string` |
| `DB_key` instance passed positionally | Works as before, `.redis_key` called | `test_get_positional_db_key` |
| Both positional string and `redis_key=` kwarg | Kwarg wins, positional discarded | `test_get_positional_string_with_kwarg_redis_key` |
| Nonexistent redis_key passed positionally | Returns `None` | `test_get_positional_nonexistent_key` |
| kwargs lookup (no positional) | Works as before | `test_get_kwargs_unchanged` |

## Test Impact

No existing tests affected -- all existing callers of `Query.get()` use keyword arguments (`name=`, `redis_key=`, etc.) or pass `DB_key` instances. The guard clause only activates for plain `str` in the `db_key` position, which no existing test exercises.

## Documentation

- [ ] Update docstring on `Query.get()` to document that a plain redis_key string can be passed as the first positional argument
- [ ] Add a positional-string example to the docstring's Examples section
- [ ] No external documentation changes needed -- this is a minor DX improvement with no public API shape change

## Rabbit Holes

- **Overloaded dispatch**: It might be tempting to use `@overload` or `singledispatch` for cleaner type handling. Overkill for a 3-line guard. Keep it simple.
- **Checking for colon in string**: Do not try to validate whether the string "looks like" a redis key (e.g., contains `:`). Any string in the `db_key` position that is not a `DB_key` instance should be treated as a redis_key. The Redis lookup itself handles validation -- if the key does not exist, `hgetall` returns an empty dict and `get()` returns `None`.
- **Edge case: empty string**: An empty string `""` passed positionally will be treated as `redis_key=""`, which will hit `hgetall("")` and return `None`. This is fine -- same behavior as `get(redis_key="")`.
