# TTL (Time-to-Live)

Popoto supports automatic data expiration through Redis TTL. You can set
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

When you save an instance, Popoto calls Redis `EXPIRE` on the key with the
configured TTL. After that many seconds, Redis removes the key automatically:

```python
session = AgentSession.create(agent_name="assistant", context="planning task")
# Redis will delete this session after 90 days
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

### What Happens When a Key Expires

When Redis removes an expired key, subsequent `load()` or `query.get()` calls
return `None`. Popoto handles orphaned secondary index entries gracefully
during queries. For proactive cleanup of orphaned index entries left behind by
expired keys, use [`Model.clean_indexes()`](recipes.md#index-maintenance).

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
`_expire_at`. This accepts a `datetime` object and calls Redis `EXPIREAT`:

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

## See Also

- [Model Meta Options](meta.md) -- full reference for `Meta.ttl`, `order_by`, and `indexes`
- [Recipes](recipes.md#instance-ttl-attributes) -- `_ttl` and `_expire_at` instance attributes
