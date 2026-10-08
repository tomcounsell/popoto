# TTL (Time-to-Live)

Popoto supports automatic data expiration. On Redis it uses the key's TTL; on
Postgres it uses an expiry column (see [On Postgres](#on-postgres)). You can set
expiration at the model level (all instances expire after a fixed duration),
override it per-instance, or set an absolute expiration timestamp.

TTL is ideal for temporary data like sessions, caches, tracking records,
or any data with a natural retention window.

## Model-Level TTL

Define a default TTL for all instances of a model using the `Meta` inner class:

```python
from popoto import Model, AutoKeyField, Field

class AgentSession(Model):
    session_id = AutoKeyField()
    agent_name = Field(type=str)
    context = Field(type=str, default="")
    token_count = Field(type=int, default=0)

    class Meta:
        ttl = 7776000  # 90 days (90 * 24 * 60 * 60)
```

When you save an instance on Redis, Popoto calls `EXPIRE` on the key with the
configured TTL. After that many seconds, Redis removes the key automatically:

```python
session = AgentSession.create(agent_name="assistant", context="planning task")
# The session expires after 90 days
```

### TTL Resets on Every Save

The TTL clock restarts each time you call `save()`. If you update a session
60 days in, the 90-day window starts over from that moment:

```python
session.token_count = 4500
session.save()
# TTL resets to 90 days from now
```

!!! warning
    Background processes that update records will extend the expiration window.
    If a periodic job calls `save()` on a session, that session may never expire.

### What Happens When a Key Expires (Redis)

When Redis removes an expired key, subsequent `load()` or `query.get()` calls
return `None`. Orphaned secondary index entries — an index still naming a key
whose hash is gone — are skipped during queries, and since 1.9.0 they are also
**purged on read** for every index derivable from the key alone: the class
set, non-auto `KeyField` sets, and sorted sets partitioned by key fields.
A query that walks past an orphan removes it, so a TTL'd model no longer
accumulates permanent index ghosts.

Indexes that cannot be derived from the key (a `SortedField` scored by a
non-key value, for example) still need the sweep: use
[`Model.clean_indexes()`](recipes.md#index-maintenance) for those, and for
cleanup you would rather not wait for a read to trigger.

## Per-Instance TTL Override

Override the model-level TTL for a specific instance by setting the `_ttl`
attribute before saving:

```python
# Short-lived session: 1 day instead of 90
ephemeral = AgentSession(agent_name="scratchpad")
ephemeral._ttl = 86400  # 1 day
ephemeral.save()
```

### Making an Instance Permanent

Set `_ttl` to `None` to disable expiration for a specific instance, even when
the model has a default TTL:

```python
important_session = AgentSession(agent_name="long-term-memory")
important_session._ttl = None  # Never expires
important_session.save()
```

!!! note
    The instance-level `_ttl` takes precedence over `Meta.ttl`. Setting it
    to `None` disables expiration for that instance only.

## Absolute Expiration

Instead of a relative duration, you can set an exact expiration time using
`_expire_at`. This accepts a `datetime` object (on Redis, Popoto calls `EXPIREAT`):

```python
from datetime import datetime, timedelta

session = AgentSession(agent_name="campaign-tracker")
session._expire_at = datetime(2026, 12, 31, 23, 59, 59)  # Expires end of year
session.save()
```

You can also compute the deadline dynamically:

```python
session._expire_at = datetime.now() + timedelta(hours=6)
session.save()
# Expires exactly 6 hours from now
```

## Mutual Exclusion: _ttl vs _expire_at

You cannot set both `_ttl` and `_expire_at` on the same instance. Popoto
raises a `ModelException` during validation if both are set:

```python
session = AgentSession(agent_name="conflict")
session._ttl = 3600
session._expire_at = datetime(2026, 6, 1)
session.save()
# => ModelException: Can set either ttl and expire_at. Not both.
```

Use one or the other. If the model has a `Meta.ttl` and you want to switch
to absolute expiration, clear the TTL first:

```python
session._ttl = None
session._expire_at = datetime(2026, 6, 1)
session.save()
```

## Complete Example: AgentSession with 90-Day TTL

```python
from datetime import datetime, timedelta
from popoto import Model, AutoKeyField, Field, DatetimeField

class AgentSession(Model):
    session_id = AutoKeyField()
    agent_name = Field(type=str)
    context = Field(type=str, default="")
    token_count = Field(type=int, default=0)
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)

    class Meta:
        ttl = 7776000  # 90 days

# Create a session -- expires in 90 days
session = AgentSession.create(agent_name="assistant", context="user onboarding")

# Update it -- TTL resets to 90 days from now
session.token_count = 2500
session.save()

# Override TTL for a temporary scratch session
scratch = AgentSession.create(agent_name="scratch")
scratch._ttl = 3600  # 1 hour
scratch.save()

# Make a session permanent
archival = AgentSession(agent_name="archival-memory")
archival._ttl = None
archival.save()

# Set absolute expiration
campaign = AgentSession(agent_name="q4-campaign")
campaign._ttl = None  # Clear model-level TTL
campaign._expire_at = datetime(2026, 12, 31, 23, 59, 59)
campaign.save()

# Check TTL at runtime
print(AgentSession._meta.ttl)
# => 7776000
```

## On Postgres

`Meta.ttl`, `_ttl` and `_expire_at` work on a Postgres model, with the same meaning:
`_ttl` overrides `Meta.ttl`, `_ttl = None` makes a record permanent, and every save
restarts the clock.

**How it maps.** A `Meta.ttl` model's table gets one more column, `_expires_at`
(`NULL` means the record never expires). A save sets it from `_ttl` (added to the
database server's clock) or `_expire_at`. Every read skips a row the instant it
expires, whether or not the row has been deleted yet, so `load()`, `get()`,
`filter()`, `count()` and `keys()` see live records only. There are no orphaned
index entries to clean up: the indexes belong to the row.

**Deletion.** Expired rows are deleted by a reaper that runs after writes to the
same table: up to 20 rows at a time, at most once a second per table and process.
Reads never delete. Correctness does not depend on it, because reads already skip
expired rows, but a table that is only read keeps its expired rows until the next
write. There is no cron job or CLI to run. Details are in
[Record expiry](features/postgres-backend.md#record-expiry-m5).

**Differences from Redis:**

- Setting `_ttl` or `_expire_at` on a model without `Meta.ttl` raises
  `BackendCapabilityError` before anything is written. On Redis it sets the key's
  TTL. Declare `Meta.ttl` on any model whose records may expire.
- A `_ttl` that is not a whole number (`1.5`) raises `ModelException`.
- Removing `Meta.ttl` later from a model whose table has the column raises
  `SchemaDriftError` on first use, until the column is dropped.
- To read a record's remaining TTL (here `AgentSession` with `backend = "postgres"`
  in its `Meta`), ask the backend. It answers as Redis's `TTL`
  command does (`-2` no live record, `-1` no expiry, else whole seconds):

    ```python
    from popoto.backends import get_backend, record_id

    backend = get_backend(AgentSession)
    backend.ttl_remaining(AgentSession._meta.spec, [record_id(session)])
    # => [7776000]
    ```

- Per-record TTLs are not carried by [Export & Import](guides/export-import.md) on
  either backend.

Each of these is a row in
[Records and other behaviour](features/postgres-backend.md#records-and-other-behaviour).

## See Also

- [Model Meta Options](meta.md) -- full reference for `Meta.ttl`, `order_by`, and `indexes`
- [Recipes](recipes.md#instance-ttl-attributes) -- `_ttl` and `_expire_at` instance attributes
