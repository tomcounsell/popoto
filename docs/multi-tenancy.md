# Multi-Tenancy and Namespace Isolation

Popoto models often need to isolate data by tenant, project, or environment. For
example, a memory system where project A's episodes live separately from project B's,
using the same model class.

Redis has no built-in schema or table concept — isolation comes from key structure.
Popoto's `KeyField` already provides this naturally.

## The Pattern: Use a KeyField

Add a `KeyField` for your namespace, tenant, or project identifier. All Redis keys
and indexes automatically partition by KeyField values, giving you complete isolation
with zero extra machinery.

That isolation holds under concurrency: querying two tenants from two threads at the
same time returns each thread only its own tenant's rows. Popoto 1.9.0 and earlier had
a bug here — the per-query bookkeeping on the shared `Model.query` object was not
per-thread, so concurrent queries on one model class could return rows from another
tenant's partition ([#600](https://github.com/tomcounsell/popoto/issues/600)). See
[Thread Safety](configuration.md#thread-safety).

```python
from popoto import Model, KeyField, AutoKeyField, Field, SortedField

class Episode(Model):
    project_id = KeyField(type=str)
    episode_id = AutoKeyField()
    title = Field(type=str)
    score = SortedField(type=float)
```

Instances are stored at keys like `Episode:project-a:abc123`, and sorted indexes
partition by `project_id` automatically:

```python
# Project A's episodes
Episode.create(project_id="project-a", title="First meeting", score=0.8)
Episode.create(project_id="project-a", title="Follow-up", score=0.6)

# Project B's episodes (completely isolated)
Episode.create(project_id="project-b", title="Kickoff", score=0.9)

# Query within a single project — only returns project A's episodes
results = Episode.query.filter(project_id="project-a", score__gte=0.5)
print(len(results))
# => 2
```

### What gets isolated

Because `project_id` is a KeyField, these Redis structures are all scoped per project:

- **Instance keys**: `Episode:project-a:abc123`
- **Sorted indexes**: `$SortedF:Episode:score:project-a` (via `partition_by`, see below)
- **KeyField index sets**: `$KeyF:Episode:project_id:project-a`

The `$Class:Episode` set contains all instances across projects, but queries on
`project_id` use the KeyField index for O(1) lookups rather than scanning.

### Combine with partition_by for sorted queries

If you always query sorted fields within a single project, use `partition_by` to
scope the sorted index:

```python
class Episode(Model):
    project_id = KeyField(type=str)
    episode_id = AutoKeyField()
    title = Field(type=str)
    score = SortedField(type=float, partition_by=('project_id',))
```

Now `score` range queries are fast and isolated per project:

```python
# Uses the partition-scoped sorted set — no cross-project data
top_episodes = Episode.query.filter(
    project_id="project-a",
    score__gte=0.7,
)
```

!!! tip
    Without `partition_by`, sorted field range queries work across all projects.
    With `partition_by=('project_id',)`, each project gets its own sorted set,
    making range queries faster and fully isolated.

!!! note "Datetime partition values are canonicalized to UTC"
    If the field you partition on is a `datetime` rather than a string tenant id, the
    partition segment is canonicalized to UTC, so aware, offset and naive
    representations of one instant share a single partition instead of splitting into
    several. Non-datetime partition values, including the string ids used throughout
    this page, are unchanged.

## Passing the namespace through your application

The KeyField pattern is explicit — you pass `project_id` to every `create()`,
`filter()`, and `load()` call. In web applications, you can reduce boilerplate
with a helper that reads the current context:

```python
from contextvars import ContextVar

# Set once per request (e.g., in middleware)
_current_project = ContextVar("current_project")

def current_project_id() -> str:
    return _current_project.get()

def set_current_project(project_id: str):
    _current_project.set(project_id)
```

Then use it in your application code:

```python
# In middleware or request setup
set_current_project("project-a")

# In your business logic
episodes = Episode.query.filter(
    project_id=current_project_id(),
    score__gte=0.5,
)

new_episode = Episode.create(
    project_id=current_project_id(),
    title="New episode",
    score=0.75,
)
```

This keeps the namespace explicit at the model level while avoiding repetitive
string passing in application code.

## Hash-based field partitioning (ConfidenceField)

SortedField partitions sorted set indexes. ConfidenceField partitions its companion
Redis hash, which stores per-member Bayesian metadata (`{confidence, evidence_count,
corroborations, contradictions}`).

Without partitioning, reads require `HGETALL` on a single hash containing every
member across all tenants. With `partition_by`, each tenant gets its own hash,
making reads O(partition_size) instead of O(total_members).

```python
from popoto import Model, KeyField, UniqueKeyField, StringField
from popoto.fields.confidence_field import ConfidenceField

class Memory(Model):
    project = KeyField(type=str)
    key = UniqueKeyField()
    content = StringField()
    certainty = ConfidenceField(initial_confidence=0.5, partition_by='project')
```

This creates per-project companion hashes:

| Without partition_by | With partition_by='project' |
|----------------------|----------------------------|
| `$ConfidencF:Memory:certainty:data` (all members) | `$ConfidencF:Memory:certainty:data:atlas` (project atlas only) |
| | `$ConfidencF:Memory:certainty:data:hermes` (project hermes only) |

!!! note "Datetime partition values are canonicalized to UTC"
    When the partition field holds a `datetime`, the segment appended to the companion
    hash name is canonicalized to UTC, so aware, offset and naive representations of
    one instant share a single hash. String project ids like `atlas` above render as
    `str(value)` and are unchanged.

Usage is identical to unpartitioned ConfidenceField -- the partition is resolved
automatically from the model instance:

```python
memory = Memory.create(project="atlas", key="fact1", content="The sky is blue")

# Writes to the 'atlas' partition hash automatically
ConfidenceField.update_confidence(memory, "certainty", signal=0.9)

# Reads from the 'atlas' partition hash automatically
confidence = ConfidenceField.get_confidence(memory, "certainty")
```

### Decay queries against a partitioned ConfidenceField

If the same model also declares a `DecayingSortedField` (or `CyclicDecayField`),
[confidence-modulated decay](features/decaying-sorted-field.md#confidence-modulated-decay)
joins the decay index to the confidence `:data` hash. A partitioned `ConfidenceField`
spreads that hash across tenants, so a single decay query cannot cover the result set
unless it names the partition. `top_by_decay()` and `composite_score()` raise
`QueryException` listing the filters they need:

```python
Memory.query.filter(project="atlas").top_by_decay(10)   # OK

Memory.query.filter(key="fact1").top_by_decay(10)
# QueryException: ... ConfidenceField 'certainty' ... is partitioned by project.
# Query must include filter(s) for: project.
```

Add the partition filter, or set `confidence_modulation_field=False` on the decay
field to opt that field out of modulation.

### Partition key changes

If a model instance changes its partition key value (e.g., moves from project A to
project B), ConfidenceField automatically detects the change during `on_save()` and
moves the entry from the old partition hash to the new one. No manual intervention
is needed.

### Migrating existing data

If you add `partition_by` to an existing ConfidenceField that already has data in a
single unpartitioned hash, use the migration helper:

```python
# Preview what would happen
report = ConfidenceField.migrate_to_partitioned(Memory, "certainty", dry_run=True)
print(report)
# {'total': 1200, 'migrated': 0, 'errors': [], 'partitions': {'atlas': 800, 'hermes': 400}}

# Run the actual migration
report = ConfidenceField.migrate_to_partitioned(Memory, "certainty")
```

The migration reads each entry from the unpartitioned hash, loads the corresponding
model instance to determine its partition field values, and writes to the correct
partitioned hash. The old unpartitioned hash is deleted after a successful migration.

### Verifying tenant isolation

Use the companion key methods to confirm that each tenant's companion hash is stored
under a separate Redis key. This is useful for auditing, monitoring dashboards, or
integration tests that validate isolation.

```python
field = Memory._options.fields["certainty"]

# Build keys for each tenant without loading instances
atlas_key = field.get_data_hash_key_from_values(Memory, "certainty", project="atlas")
hermes_key = field.get_data_hash_key_from_values(Memory, "certainty", project="hermes")

print(atlas_key)   # => "$ConfidencF:Memory:certainty:data:atlas"
print(hermes_key)  # => "$ConfidencF:Memory:certainty:data:hermes"
assert atlas_key != hermes_key  # Companion hashes are fully isolated
```

You can also verify isolation for instance-based keys:

```python
memory_a = Memory.query.get(project="atlas", key="fact1")
memory_b = Memory.query.get(project="hermes", key="fact2")

key_a = field.get_data_hash_key(memory_a, "certainty")
key_b = field.get_data_hash_key(memory_b, "certainty")
assert key_a != key_b  # Each tenant's data lives in a separate hash
```

### Filtered reads without partitioning

If partitioning is not appropriate for your use case, `get_confidence_filtered()`
provides an HSCAN-based alternative that avoids loading all entries into memory:

```python
# Match members by Redis key pattern
results = ConfidenceField.get_confidence_filtered(Memory, "certainty", pattern="Memory:atlas:*")
# Returns: {member_key: {confidence, evidence_count, ...}} for matching entries
```

## When to use separate Redis databases instead

For stronger isolation (e.g., compliance requirements, independent TTL policies,
or different Redis instances per tenant), use separate Redis connections rather
than key prefixing:

```python
from popoto.redis_db import set_REDIS_DB_settings

# Switch the entire connection for a tenant
set_REDIS_DB_settings(redis_url="redis://tenant-a-host:6379/0")
```

This is heavier but provides complete data separation at the connection level.

## Summary

| Approach | Isolation level | Complexity | Best for |
|----------|----------------|------------|----------|
| KeyField (recommended) | Key prefix + index partitioning | None — uses existing fields | Most multi-tenant apps |
| SortedField partition_by | Per-tenant sorted set indexes | One parameter | Sorted range queries within a tenant |
| ConfidenceField partition_by | Per-tenant companion hashes | One parameter | Large confidence hashes with tenant-scoped reads |
| ContextVar helper | Same as KeyField, less boilerplate | Minimal application code | Web apps with per-request tenancy |
| Separate Redis databases | Full connection isolation | Configuration management | Compliance, independent scaling |
