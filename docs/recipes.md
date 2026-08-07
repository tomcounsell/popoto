# Recipes

Patterns and walkthroughs for common Popoto operations. Symbol-level reference
documentation lives under [the API Reference](reference/SUMMARY.md) and is
auto-generated from docstrings — this page captures the prose and worked
examples that don't naturally live next to a single symbol.

## Version Introspection

`popoto.__version__` resolves to the installed distribution's version string
via `importlib.metadata` (PEP 566). `pyproject.toml` is the single source of
truth — the package exposes whatever is set in `[project].version`. When
importing from an uninstalled source tree, `__version__` falls back to the
PEP 440-compliant sentinel `"0.0.0+unknown"`.

```python
import popoto
print(popoto.__version__)  # e.g. "1.6.0"
```

No separate `VERSION` file, no static string in `__init__.py` — so there is no
risk of version skew between the code on disk and the version reported at
runtime.

## Bulk Operations

Popoto provides bulk operation methods for efficient batch processing using
Redis pipelines. These methods significantly reduce network round-trips
compared to individual operations, making them ideal for importing data,
batch updates, and cleanup tasks.

### Choosing a Batch Size

All bulk methods accept a `batch_size` parameter (default 1000) that controls
memory usage and pipeline size. When processing more instances than
`batch_size`, operations are automatically split into multiple pipeline
executions.

```python
# Process 10,000 instances in batches of 500
Restaurant.bulk_create(large_list, batch_size=500)
```

**When to adjust batch size:**

- **Increase** for faster throughput when memory is not a concern.
- **Decrease** when instances are large or memory is constrained.
- **Default (1000)** works well for most use cases.

### Async Bulk Methods

All bulk operations have async counterparts that run in a thread pool to
avoid blocking the event loop. See [Async Operations](async.md) for details.

| Sync | Async |
|------|-------|
| `Model.bulk_create(instances)` | `await Model.async_bulk_create(instances)` |
| `Model.bulk_update(queryset, **updates)` | `await Model.async_bulk_update(queryset, **updates)` |
| `Model.bulk_delete(queryset)` | `await Model.async_bulk_delete(queryset)` |
| `Model.delete_all()` | `await Model.async_delete_all()` |

```python
# Async bulk create
restaurants = await Restaurant.async_bulk_create([
    Restaurant(name="Async Eats", cuisine="Fusion", rating=4.5),
    Restaurant(name="Pipeline Pizzeria", cuisine="Italian", rating=4.3),
])

# Async bulk update
count = await Restaurant.async_bulk_update(
    Restaurant.query.filter(rating__gte=4.0),
    is_featured=True
)

# Async bulk delete
count = await Restaurant.async_bulk_delete(
    Restaurant.query.filter(status="closed")
)
```

### Why `delete_all()` instead of `DEL`/`FLUSHDB`?

**Never delete Popoto data directly with Redis commands like `DEL`,
`FLUSHDB`, or `KEYS ... | xargs redis-cli DEL`.** Popoto maintains secondary
indexes for fast queries:

- **SortedField** → Redis sorted sets for range queries
- **GeoField** → Redis geo sets for location queries
- **UniqueKeyField** → Redis keys for uniqueness constraints
- **Class sets** → Track all instances of each model

If you delete instance keys directly, these indexes become orphaned:

- Range queries return stale results
- Geo queries find deleted locations
- Unique constraints block valid values
- `count()` returns wrong numbers

`delete_all()` properly invokes each instance's `delete()` method, which
triggers all field `on_delete` hooks to clean up indexes. This is the **only
safe way** to bulk-delete Popoto data.

```python
# CORRECT - cleans up all indexes
Restaurant.delete_all()

# WRONG - leaves orphaned indexes
redis_client.delete(*redis_client.keys("Restaurant:*"))
```

### Bulk Operations: Worked Examples

**Data Import**

```python
# Import restaurants from CSV
import csv

with open("restaurants.csv") as f:
    reader = csv.DictReader(f)
    instances = [
        Restaurant(
            name=row["name"],
            cuisine=row["cuisine"],
            rating=float(row["rating"]),
        )
        for row in reader
    ]

created = Restaurant.bulk_create(instances)
print(f"Imported {len(created)} restaurants")
```

**Batch Status Update**

```python
# Mark all orders older than 30 days as archived
from datetime import datetime, timedelta

cutoff = datetime.now() - timedelta(days=30)
old_orders = Order.query.filter(created_at__lt=cutoff)
count = Order.bulk_update(old_orders, status="archived")
print(f"Archived {count} old orders")
```

**Cleanup Task**

```python
# Remove all soft-deleted records
deleted_count = Restaurant.bulk_delete(
    Restaurant.query.filter(is_deleted=True)
)
print(f"Permanently removed {deleted_count} restaurants")
```

## Index Maintenance

Popoto maintains secondary indexes (sorted sets, key field sets, geo indexes,
composite indexes, and the class set) alongside your model data. Over time,
indexes can accumulate orphaned entries — references to instance keys that no
longer exist in Redis. This typically happens after direct Redis deletions,
TTL expirations, or interrupted operations.

The recommended workflow is **diagnose → clean → verify**:

```python
# Step 1: Read-only health check (zero writes)
result = User.check_indexes()
print(f"Found {result['total']} orphaned index entries")

# Step 2: Production-safe surgical cleanup
if result['total'] > 0:
    removed = User.clean_indexes()
    print(f"Cleaned {removed} orphans")

# Step 3: Verify
after = User.check_indexes()
assert after['total'] == 0
```

`check_indexes()` returns a per-index-type breakdown:

```python
{
    'class_set': int,         # absent-hash orphans (EXISTS == 0)
    'partial_writes': int,    # hash exists but missing the AutoKeyField value
    'key_fields': {field_name: int, ...},
    'sorted_fields': {field_name: int, ...},
    'geo_fields': {field_name: int, ...},
    'composite_indexes': {index_key: int, ...},
    'total': int,             # sum of all the above
}
```

### Partial-Write Orphans

For models whose primary key is a single `AutoKeyField`, `check_indexes()`
also detects **partial-write orphans**: hashes that exist in Redis but are
missing the auto-key field value. These appear as ghost rows in
`query.all()` (with `id=None`, `_redis_key=None`) and `instance.delete()`
silently no-ops on them. Common causes are crashed saves and mid-pipeline
process exits.

When `clean_indexes()` encounters a partial-write orphan it removes the
class-set membership AND issues `DEL` on the corrupt hash — the hash is
unrecoverable and must not linger in Redis. Models with composite
`KeyField`s (no single `AutoKeyField`) skip this check; their behavior is
unchanged.

> **Operational guidance:** Do not run `clean_indexes()` during active
> migrations or HDEL-based field migrations. A brief HDEL window on the
> auto-key field can cause healthy hashes to be misclassified as
> partial-write orphans and deleted. Run during low-traffic periods.

### When to Use `rebuild_indexes()` vs `clean_indexes()`

`clean_indexes()` is the right choice for routine maintenance — it surgically
removes only the orphaned entries (SREM, ZREM, HDEL) and leaves valid index
data untouched, so concurrent queries continue to return correct results.

`rebuild_indexes()` deletes all secondary indexes and reconstructs them from
source hash data. Use it as a last resort: for repairing structurally
corrupted indexes, after bulk imports that bypassed normal `save()` hooks, or
when upgrading field types that change index structure. **During the rebuild
window, queries relying on those indexes may return incomplete results.**

### Async Index Maintenance

All three index maintenance methods have async counterparts that use
`asyncio.to_thread` under the hood, keeping the event loop free during
potentially long-running scans.

| Sync | Async |
|------|-------|
| `Model.check_indexes()` | `await Model.async_check_indexes()` |
| `Model.clean_indexes()` | `await Model.async_clean_indexes()` |
| `Model.rebuild_indexes()` | `await Model.async_rebuild_indexes()` |

```python
async def maintain_all_indexes():
    """Check and clean indexes for all models concurrently."""
    results = await asyncio.gather(
        User.async_check_indexes(),
        Restaurant.async_check_indexes(),
        Order.async_check_indexes(),
    )

    for model_name, result in zip(["User", "Restaurant", "Order"], results):
        if result['total'] > 0:
            print(f"{model_name}: {result['total']} orphans found, cleaning...")

    if results[0]['total'] > 0:
        await User.async_clean_indexes()
    if results[1]['total'] > 0:
        await Restaurant.async_clean_indexes()
    if results[2]['total'] > 0:
        await Order.async_clean_indexes()
```

A live demo is available in the
[Popoto Kitchen example app](https://github.com/tomcounsell/popoto/tree/main/examples)
— run `python -m popoto_kitchen --ops` to see the
`check_indexes()` → `clean_indexes()` workflow across multiple models.

## Instance TTL Attributes

Every model instance exposes two attributes for controlling expiration. These
are set per-instance before calling `save()`. See [TTL](ttl.md) for full
documentation and examples.

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `_ttl` | `int` or `None` | Value of `Meta.ttl` | Time-to-live in seconds. Set to `None` to make the instance permanent. Takes precedence over `Meta.ttl`. |
| `_expire_at` | `datetime` or `None` | `None` | Absolute expiration timestamp. Calls Redis `EXPIREAT` on save. |

!!! warning
    Setting both `_ttl` and `_expire_at` on the same instance raises a
    `ModelException` during validation. Use one or the other.

```python
from datetime import datetime

# Override model TTL for one instance
order = Order(order_id="rush-123", total=49.99)
order._ttl = 604800  # 7 days instead of the default 30
order.save()

# Set absolute expiration
order._ttl = None
order._expire_at = datetime(2026, 12, 31, 23, 59, 59)
order.save()
```

## Exceptions: When Each Is Raised

These descriptions complement the auto-generated reference at
[`popoto.exceptions`](reference/popoto/exceptions.md).

- **`ModelException`** — raised when a model operation fails: validation
  errors, save failures, unique constraint violations, delete or load errors.
  Automatically reported when [error reporting](configuration.md#error-reporting-opt-in)
  is enabled.
- **`KeyMutationError`** (subclass of `ModelException`) — raised when a
  `KeyField` value is changed after initial save and `save()` is called
  without `migrate_key=True`. This prevents accidental identity changes that
  could orphan references. Override with `instance.save(migrate_key=True)`
  when you genuinely intend to migrate.
- **`QueryException`** — raised when a query is malformed or produces an
  unexpected result (e.g., invalid filter parameters, `get()` returning
  multiple results).
- **`PublisherException`** — raised when a publish operation fails (e.g.,
  missing channel name).
- **`SubscriberException`** — raised when a subscriber's message handler
  fails.
- **`PopotoException`** — base exception class for Popoto framework errors.
  Logs the error message on initialization.

```python
from popoto import KeyMutationError

instance = MyModel.query.get(name="old_name")
instance.name = "new_name"

try:
    instance.save()  # Raises KeyMutationError
except KeyMutationError:
    instance.save(migrate_key=True)  # Intentional migration succeeds
```

## Benchmarking

Popoto includes an external benchmark harness for evaluating memory retrieval
quality against published datasets. See **[docs/benchmarks.md](benchmarks.md)**
for full documentation.

Quick reference:

```bash
# Install benchmark dependencies
pip install -e ".[benchmark]"

# Run LongMemEval-S benchmark (downloads ~264 MB on first run)
python -m tests.benchmarks.run_external --dataset longmemeval-s

# Run LoCoMo benchmark
python -m tests.benchmarks.run_external --dataset locomo

# Quick smoke test (fixture-based, no download)
python -m tests.benchmarks.run_external \
    --dataset longmemeval-s \
    --fixture tests/benchmarks/datasets/fixtures/longmemeval_s_sample.json \
    --limit 3 --dry-run
```

Results are committed to `tests/benchmarks/results/external/` as Markdown and
JSON files, providing a baseline for future retrieval improvements.

## MemoryLifecycle

`MemoryLifecycle` is a policy layer that orchestrates memory tier transitions
and auto-forget. It composes existing Popoto primitives
(`DecayingSortedField`, `ConfidenceField`, `AccessTrackerMixin`) into a
working → episodic → semantic lifecycle — without replacing any of them.

### Two tiers

| Tier | Description |
|------|-------------|
| `"episodic"` | Default for new memories. Specific events with temporal context. Subject to promotion and auto-forget. |
| `"semantic"` | Consolidated facts. Decontextualized. Protected from auto-forget by default. |

### Quickstart

```python
import popoto
from popoto.fields.access_tracker import AccessTrackerMixin
from popoto.fields.shortcuts import KeyField
from popoto.fields.decaying_sorted_field import DecayingSortedField
from popoto.fields.confidence_field import ConfidenceField
from popoto.recipes import MemoryLifecycle

# 1. Define your model with a tier field and the primitives MemoryLifecycle reads
class Memory(AccessTrackerMixin, popoto.Model):
    key = popoto.AutoKeyField()
    tier = KeyField(type=str, default="episodic")   # KeyField = filter-queryable partition
    content = popoto.StringField(default="")
    relevance = DecayingSortedField(decay_rate=0.5)
    certainty = ConfidenceField(initial_confidence=0.5)

# 2. Instantiate once (usually at application start)
lifecycle = MemoryLifecycle(
    model_class=Memory,
    importance_field="relevance",   # name of a DecayingSortedField (required)
    tier_field="tier",              # default — name of the tier partition field
)

# 3. Tag new memories after saving them
record = Memory(content="Alice prefers dark mode")
record.save()
lifecycle.tag_new(record)           # sets tier = "episodic" and saves

# 4. Run a lifecycle pass periodically (e.g. after each conversation turn,
#    or on a background schedule)
summary = lifecycle.tick()
# {"promoted": 0, "forgotten": 0, "tombstoned": 0, "duration_ms": 1.4}

# 5. Inspect a record's lifecycle state
state = lifecycle.assess(record)
print(state.tier)               # "episodic"
print(state.access_count)       # 0 (no confirmed reads yet)
print(state.promotion_eligible) # False (below access threshold)
print(state.forget_eligible)    # False (not idle enough)
```

### Promotion criteria

A record is promoted from `"episodic"` to `"semantic"` when **all** of these
hold simultaneously:

| Criterion | Default |
|-----------|---------|
| `access_count >= PROMOTION_ACCESS_COUNT` | 3 |
| `confidence >= PROMOTION_CONFIDENCE_THRESHOLD` | 0.6 |
| `age_seconds >= PROMOTION_MIN_AGE_SECONDS` | 300 (5 min) |

Promotion is non-reversible in v1 (no demotion from semantic).

### Auto-forget criteria

A non-semantic record is forgotten when it is idle **and** either its importance has
bottomed out or its accumulated outcome evidence has turned decisively negative:

```text
(importance_score < FORGET_IMPORTANCE_FLOOR
 OR (confidence < FORGET_CONFIDENCE_CEILING AND evidence_count >= FORGET_MIN_EVIDENCE))
AND idle_seconds > FORGET_IDLE_SECONDS
```

| Criterion | Default |
|-----------|---------|
| `importance_score < FORGET_IMPORTANCE_FLOOR` | 0.1 |
| `confidence < FORGET_CONFIDENCE_CEILING` | 0.3 |
| `evidence_count >= FORGET_MIN_EVIDENCE` | 5 |
| `idle_seconds > FORGET_IDLE_SECONDS` | 86 400 (24 h) |

The confidence disjunct closes the promote/forget asymmetry — confidence already gated
promotion but was blind to forgetting. `FORGET_MIN_EVIDENCE` is a safety floor rather
than a tuning knob: confidence moves on every reported outcome, so without a minimum
track record one unlucky dismissal could bury a memory. Models with no
`ConfidenceField` take the importance-only path, unchanged from before.

Semantic records are **never** forgotten by the default policy.

### Tombstones

Forgetting archives the record and removes it from the live corpus rather than deleting
it, so exclusion from every retrieval mode is structural — no read path has to remember
to filter tombstones out. The fingerprint, archived payload, and death metadata are kept
under `$TOMB:{Model}:*`, and the decision is reversible.

```python
summary = lifecycle.tick()
# {"promoted": 0, "forgotten": 3, "tombstoned": 3, "duration_ms": 2.1}

lifecycle.tombstone_count()                 # 3
tombs = lifecycle.list_tombstones(limit=10) # newest death first
tombs[0].confidence_at_death                # e.g. 0.12
tombs[0].dismissal_count                    # e.g. 7

lifecycle.restore(tombs[0].redis_key)       # live again, all indexes rebuilt
```

| Method | Purpose |
|--------|---------|
| `tombstone(record, reason="policy")` | Forget one record explicitly; returns a `Tombstone` or `None` |
| `restore(redis_key)` | Re-save the archived record (re-runs every `on_save` hook, so sorted/unique/geo indexes are rebuilt) and drop its tombstone |
| `list_tombstones(limit=None)` | Retained `Tombstone`s, newest death first |
| `get_tombstone(redis_key)` | One `Tombstone`, or `None` if it aged out |
| `tombstone_count()` | Number currently retained |
| `purge_tombstone(redis_key)` | Drop one tombstone permanently |
| `purge_all_tombstones()` | Drop every tombstone for this model class |
| `forget_hard(record)` | Irreversible delete with no tombstone |
| `confidence_forget_eligible(record)` | Whether evidence alone justifies forgetting |

A `Tombstone` carries `redis_key`, `fingerprint` (the `ExistenceFilter` fingerprint when
the model has one, else the `redis_key`), `tier`, `importance_at_death`,
`confidence_at_death`, `evidence_count`, `dismissal_count`, `tombstoned_at`, and `reason`.

Retention is bounded by `TOMBSTONE_RETENTION_LIMIT` (1000); the oldest age out, so a
restore is only available while the tombstone is still retained. Archiving happens before
removal, so a crash between the two steps loses nothing.

### Custom policies

Override the default promotion or forget logic at construction time:

```python
def my_should_promote(record, lifecycle):
    """Promote immediately if content contains a confirmed fact."""
    if "confirmed:" in record.content:
        return "semantic"
    return None  # defer to normal criteria

def my_should_forget(record, lifecycle):
    """Never forget anything tagged 'keep'."""
    if getattr(record, "content", "").startswith("[keep]"):
        return False
    # Fall through to default behavior
    from popoto.recipes.memory_lifecycle import _default_should_forget
    return _default_should_forget(record, lifecycle)

lifecycle = MemoryLifecycle(
    model_class=Memory,
    importance_field="relevance",
    should_promote=my_should_promote,
    should_forget=my_should_forget,
)
```

### Composing with SubconsciousMemory

`MemoryLifecycle` is an independent policy layer — it composes *alongside*
`SubconsciousMemory`, not as a replacement:

```python
from popoto.recipes import DefaultMemory, MemoryLifecycle, SubconsciousMemory

Memory = DefaultMemory  # or your own model class

sm = SubconsciousMemory(model_class=Memory, agent_id="agent-1")
lifecycle = MemoryLifecycle(model_class=Memory, importance_field="relevance")

# Pre-turn: inject context from all tiers
messages, result = sm.inject_context(messages)

# ... LLM inference ...

# Post-turn: extract new memories into episodic tier
new_memories = sm.extract_memories(response_text)
for record in new_memories:
    lifecycle.tag_new(record)  # assigns tier = "episodic"

# Periodically: consolidate and prune
summary = lifecycle.tick()
```

### Partition filtering

In multi-agent deployments, scope each lifecycle instance to one agent:

```python
lifecycle = MemoryLifecycle(
    model_class=Memory,
    importance_field="relevance",
    partition_filters={"agent_id": "agent-1"},
)
lifecycle.tick()  # only touches agent-1's records
```

### Tuning the thresholds

The magic-number constants are class attributes:

```python
# Inspect defaults
print(MemoryLifecycle.PROMOTION_ACCESS_COUNT)          # 3
print(MemoryLifecycle.PROMOTION_CONFIDENCE_THRESHOLD)  # 0.6
print(MemoryLifecycle.PROMOTION_MIN_AGE_SECONDS)       # 300.0
print(MemoryLifecycle.FORGET_IMPORTANCE_FLOOR)         # 0.1
print(MemoryLifecycle.FORGET_IDLE_SECONDS)             # 86400.0
print(MemoryLifecycle.FORGET_CONFIDENCE_CEILING)       # 0.3
print(MemoryLifecycle.FORGET_MIN_EVIDENCE)             # 5
print(MemoryLifecycle.TOMBSTONE_RETENTION_LIMIT)       # 1000

# Override for a specific instance
lifecycle.PROMOTION_ACCESS_COUNT = 5
lifecycle.FORGET_IDLE_SECONDS = 43200.0  # 12 hours
```

Systematic tuning is done via the Tier 5 benchmark sweep:

```bash
python -m tests.benchmarks.run_sweeps --tier 5
```

See `docs/benchmarks/memory_lifecycle_baseline.md` for the sweep grid and
pre-lifecycle retrieval baselines.

