# MemoryLifecycle importance score honours `partition_by`

- **Issue:** #658
- **Status:** Ready
- **Branch:** `session/sdlc-658`

## Problem

`_get_importance_score` (`src/popoto/recipes/memory_lifecycle.py:223-268`) reads the
importance field's sorted-set score with `partitioned=False`:

```python
raw_score = field.score(record, importance_field, partitioned=False)
```

`SortedFieldMixin.score(partitioned=False)` resolves the **base, unpartitioned** key
(`get_sortedset_db_key` → `get_special_use_field_db_key`). But `SortedFieldMixin.on_save`
(`sorted_field_mixin.py:668-671`) writes the member to the **partitioned** key
(`get_partitioned_sortedset_db_key`). For any importance field declared with
`partition_by`, the base key can never contain the member, so:

1. `ZSCORE` returns `None` for every record of that model;
2. the `if raw_score is not None` guard fails;
3. the function silently falls through to the direct-attribute read
   (`getattr(record, importance_field, None)`).

The sorted-set path is dead for partitioned models. That is not merely a reported
number: `_default_should_forget` gates on
`importance < lifecycle.FORGET_IMPORTANCE_FLOOR`, so it moves real retention
decisions.

This is the #474 defect class, already fixed once in `recipes/context_assembler.py`.
The `partitioned=False` flag was introduced by #649 deliberately, to keep that PR a
byte-identical no-op, with #658 named in both the call-site comment and the
`score()` docstring as the issue that migrates the caller.

## Solution

Drop the keyword. One line:

```python
raw_score = field.score(record, importance_field)
```

`partitioned=True` is `score()`'s default and resolves
`get_partitioned_sortedset_db_key(model_instance, field_name)` — the same key
`on_save`, `count()`, and `members()` use.

### Why this is a no-op for unpartitioned models

For a field with `partition_by == ()`:

- `partitioned=False` → `get_sortedset_db_key(type(instance), field_name)`
- `partitioned=True` → `get_partitioned_sortedset_db_key(instance, field_name)`
  → `get_sortedset_db_key(instance, field_name)` then a **zero-iteration** append loop.

Both bottom out in
`get_special_use_field_db_key`, whose entire body is
`DB_key(cls.field_class_key, model._meta.db_class_key, *field_names)`
(`fields/field.py:617`). `_meta` resolves identically on a class and on an instance,
so the two produce a byte-identical key. Existing behaviour for every unpartitioned
model — which is every model in `tests/test_memory_lifecycle.py` — is unchanged.

### Exception surface

`get_partitioned_sortedset_db_key` raises `QueryException` when a `partition_by`
field is missing from the instance. `_get_importance_score` already wraps the call
in `try: … except Exception: pass`, so a record lacking partition values degrades to
the attribute fallback rather than propagating — the same outcome it has today.
No new raise reaches a caller.

## Behaviour change and its disclosure

For a partitioned model, records that previously scored via the attribute read will
start scoring via the ZSET. That can flip `_default_should_forget` on an existing
deployment. This is the intended fix (the dead path is the defect), and it is not
guarded behind a flag: a deploy-level opt-out would preserve the bug as a supported
mode. It is disclosed as a `### Fixed` CHANGELOG entry calling out the retention
implication explicitly.

## Collateral edits

- `SortedFieldMixin.score` docstring (`sorted_field_mixin.py:569-584`) claims the
  `partitioned` flag "exists solely to preserve the pre-existing read in
  `recipes/memory_lifecycle.py`" and that "#658 tracks migrating that remaining
  caller off it". Both sentences go stale on merge. Rewrite to describe the flag as
  a general escape hatch for reading the base key, with no live caller.
- `_get_importance_score`'s long `partitioned=False` comment block
  (`memory_lifecycle.py:238-252`) is replaced by a short note recording that the key
  must match `on_save`'s.

## Tests

New file: `tests/test_memory_lifecycle_partitioned_importance.py`.

A partitioned model:

```python
class PartitionedMemory(popoto.Model):
    key = popoto.AutoKeyField()
    tier = KeyField(type=str, default="episodic")
    agent = KeyField(type=str, default="a1")
    relevance = DecayingSortedField(decay_rate=0.5, partition_by="agent")
```

1. **`test_partitioned_importance_reads_the_sorted_set`** — save a record, confirm
   `_get_importance_score` returns the normalized ZSET value, not the attribute
   value. Made discriminating by giving the attribute a value the ZSET path cannot
   produce, so a fallback read is detectable from the return value alone.
2. **`test_partitioned_importance_does_not_take_the_attribute_fallback`** — spy on
   `getattr`-fallback reachability by asserting the resolved key is the partitioned
   one and that `ZSCORE` against it returns non-`None`.
3. **`test_unpartitioned_importance_is_unchanged`** — control: the same assertions
   against an unpartitioned model, proving the byte-identity argument above.
4. **`test_forget_decision_moves_for_partitioned_model`** — the point of the issue:
   `_default_should_forget` returns a different answer before and after, driven by a
   real ZSET score rather than the attribute.

**Non-vacuity protocol (required).** For each test, delete or invert the exact thing
it names — revert `field.score(...)` to `partitioned=False`, or remove the
partition value — confirm the test FAILS, then restore. A mutation table goes in the
PR body. This is mandatory because the tests are structurally at risk of passing off
the fallback path while the ZSCORE path stays dead, which is precisely the defect.

Tests operate on `_get_importance_score` / `_default_should_forget` directly rather
than through `tick()`: `tick()` re-hydrates its corpus and filters, which has already
made sibling forget-guard tests structurally unreachable.

## Risks

| Risk | Mitigation |
| --- | --- |
| Unpartitioned models change behaviour | Byte-identity argument above, plus control test 3 |
| Tests pass off the fallback path | Mandatory mutation proof for every test |
| Silent retention change on deploy | CHANGELOG `### Fixed` entry naming the implication |
| `QueryException` escapes | Already caught by the existing `except Exception` |

## Out of scope

- The other 32 `POPOTO_REDIS_DB` module-global import sites (#655 owns them).
  `memory_lifecycle.py` has **zero** such references — #649 already routed this read
  through `SortedFieldMixin.score`, which itself uses `get_redis()`.
