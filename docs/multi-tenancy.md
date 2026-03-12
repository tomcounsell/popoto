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
| ContextVar helper | Same as KeyField, less boilerplate | Minimal application code | Web apps with per-request tenancy |
| Separate Redis databases | Full connection isolation | Configuration management | Compliance, independent scaling |
