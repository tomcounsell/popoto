---
status: Ready
type: feature
appetite: Small
owner: valorengels
created: 2026-02-11
tracking: https://github.com/tomcounsell/popoto/issues/127
---

# to_dict() Method for Cleaner Data Access

## Problem

Popoto model instances carry internal state (`_redis_key`, `obsolete_redis_key`, `_db_content`, `_saved_field_values`) that makes them awkward to use as data objects. Users end up writing boilerplate wrapper classes just to access field values cleanly:

```python
class Job:
    def __init__(self, redis_job: RedisJob):
        self._rj = redis_job

    @property
    def job_id(self) -> str:
        return self._rj.job_id
    # ... 15 more identical properties
```

**Current behavior:** No built-in way to extract field values as a plain dict. Users must manually read each field or write wrapper classes.

**Desired outcome:** `instance.to_dict()` returns a plain dict of field values, ready for JSON serialization, API responses, template rendering, or passing to functions that expect plain data.

## Appetite

**Size:** Small

**Team:** Solo dev, no review needed.

**Interactions:**
- PM check-ins: 0
- Review rounds: 0

This is a focused addition of one method to the Model base class plus tests.

## Prerequisites

No prerequisites -- standard dev environment with Redis running.

## Solution

### Key Elements

- **`to_dict()` instance method**: Returns `{field_name: value}` for all explicit (public) fields on the model
- **Relationship handling (Peewee-inspired)**: By default, relationship fields serialize as the related instance's `redis_key` string. With `relationships=True`, recursively calls `to_dict()` on related objects. A `seen` set tracks visited instances to safely break circular references (falling back to redis_key strings for already-seen objects).
- **`include`/`exclude` params**: Optional field name filtering for projecting subsets of fields
- **`max_depth` param**: Controls recursion depth when `relationships=True` (default: no limit, protected by `seen` set)

### Flow

**User calls `.to_dict()`** -> iterate `_meta.explicit_fields` -> collect `getattr(self, field_name)` for each -> for Relationship fields: return redis_key string (default) or recurse with circular ref protection (`relationships=True`) -> apply include/exclude -> return plain dict

### Technical Approach

1. Add `to_dict()` method to `Model` in `src/popoto/models/base.py`
2. By default, return all explicit (public) fields -- skip hidden fields (underscore-prefixed) since they're internal
3. For Relationship fields (default behavior): extract the `redis_key` string from the related Model instance via `db_key.redis_key`, or use the string directly if lazy-loaded, or `None` if unset
4. For Relationship fields (`relationships=True`): recursively call `.to_dict()` on the related instance, passing along a `_seen` set containing the current instance's redis_key to detect circular references. If a related instance is already in `_seen`, fall back to redis_key string.
5. `max_depth` decrements on each recursion level. When it reaches 0, relationship fields fall back to redis_key strings regardless of `relationships` flag.
6. Accept optional `include` (whitelist) or `exclude` (blacklist) keyword arguments as sets/lists of field names

Method signature:
```python
def to_dict(self, include=None, exclude=None, relationships=False, max_depth=None, _seen=None) -> dict:
```

Example usage:
```python
class User(Model):
    email = KeyField()
    name = Field(type=str)
    score = SortedField()

user = User.create(email="alice@example.com", name="Alice", score=42.0)

user.to_dict()
# {'email': 'alice@example.com', 'name': 'Alice', 'score': 42.0}

user.to_dict(include=['email', 'name'])
# {'email': 'alice@example.com', 'name': 'Alice'}

user.to_dict(exclude=['score'])
# {'email': 'alice@example.com', 'name': 'Alice'}
```

Relationship examples:
```python
class Author(Model):
    name = KeyField()

class Book(Model):
    title = KeyField()
    author = Relationship(model=Author)

book = Book.create(title="The Hobbit", author=tolkien)

# Default: redis_key strings (safe, zero-cost)
book.to_dict()
# {'title': 'The Hobbit', 'author': 'Author:Tolkien'}

# Opt-in recursion
book.to_dict(relationships=True)
# {'title': 'The Hobbit', 'author': {'name': 'Tolkien'}}

# Circular reference safety (Person has friend -> Person)
alice.to_dict(relationships=True)
# {'name': 'Alice', 'friend': {'name': 'Bob', 'friend': 'Person:Alice'}}
#                                              ^ falls back to key string (already seen)
```

## Rabbit Holes

- **JSON serialization built-in** -- don't add `.to_json()` or handle datetime/Decimal formatting here. `to_dict()` returns Python types; users handle serialization.
- **`from_dict()` class method** -- a natural companion, but not needed. The existing `Model(**kwargs)` constructor already accepts field kwargs.
- **`__iter__` / `__dict__` overrides** -- changing dunder behavior would be surprising and could break internal mechanisms. Stick with an explicit method.
- **Lazy proxy objects in dict output** -- putting smart objects in the dict makes it non-plain. The whole point is escaping the ORM layer into plain data.

## Risks

### Risk 1: Lazy-loaded instances may not have all fields decoded
**Impact:** `to_dict()` on a lazy instance could trigger decoding of all fields, losing the performance benefit of lazy loading.
**Mitigation:** This is acceptable -- calling `to_dict()` inherently means "give me all values." The lazy decoding path in `__getattribute__` handles this transparently.

### Risk 2: Deep relationship chains with `relationships=True`
**Impact:** Could trigger many Redis lookups if relationship graph is deep.
**Mitigation:** The `seen` set prevents infinite loops. Users can use `max_depth` to limit recursion. Default (no `relationships` flag) has zero cost.

## No-Gos (Out of Scope)

- No `to_json()` method
- No `from_dict()` class method
- No changes to `__dict__`, `__iter__`, or other dunder methods
- No changes to how hidden fields work
- No dataclass decorator or integration
- No lazy proxy objects in dict output

## Documentation

### Inline Documentation
- Docstring on `to_dict()` with examples covering default, relationships, and circular ref behavior

### External Documentation Site
- [ ] Update docs if MkDocs site covers the Model API

## Success Criteria

- [ ] `model.to_dict()` returns dict of all explicit field names and values
- [ ] Hidden fields (underscore-prefixed) are excluded by default
- [ ] Relationship fields return redis_key strings by default
- [ ] `relationships=True` recursively serializes related objects as nested dicts
- [ ] Circular references fall back to redis_key strings via `seen` set tracking
- [ ] `max_depth` limits recursion depth
- [ ] `include` parameter limits output to specified fields
- [ ] `exclude` parameter omits specified fields
- [ ] Works correctly on lazy-loaded instances
- [ ] Works correctly on instances with auto-generated keys
- [ ] Existing tests pass unchanged
- [ ] New tests cover all scenarios

## Team Orchestration

Solo implementation -- no team orchestration needed for Small appetite.

## Step by Step Tasks

### 1. Add to_dict() method to Model class
- **Assigned To**: builder
- Add `to_dict(self, include=None, exclude=None, relationships=False, max_depth=None, _seen=None) -> dict` to `Model` in `base.py`
- Iterate `_meta.explicit_fields` and collect values via `getattr()`
- Handle Relationship fields:
  - Default: extract redis_key string from Model instance, pass through string value, or None
  - `relationships=True`: recursively call `to_dict()` on related instance with `_seen` set
  - If related instance's redis_key is in `_seen`, fall back to redis_key string
  - If `max_depth` reaches 0, fall back to redis_key string
- Apply include/exclude filtering

### 2. Write tests
- **Depends On**: Task 1
- Test basic to_dict() returns all explicit fields
- Test hidden fields are excluded
- Test with Relationship fields -- default redis_key string output
- Test with `relationships=True` -- nested dict output
- Test circular reference protection (seen set fallback)
- Test `max_depth` limiting
- Test include parameter
- Test exclude parameter
- Test with auto-generated key models
- Test with lazy-loaded instances
- Test that internal attrs (_redis_key, _db_content, etc.) are not in output

### 3. Final validation
- **Depends On**: Task 2
- Run full test suite: `pytest`
- Run type checking: `mypy src/`
- Verify no regressions

## Validation Commands

- `pytest tests/test_to_dict.py -v` - new tests pass
- `pytest` - all existing tests pass
- `mypy src/` - type checking passes
