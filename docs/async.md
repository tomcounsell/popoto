# Async Operations

Popoto provides async-native methods for all Model and Query operations, enabling seamless integration with asyncio-based applications like async web frameworks, job queues, and bot frameworks.

## Overview

All synchronous operations have async counterparts that run in a thread pool to avoid blocking the event loop. This means you can use Popoto in async applications without manually wrapping every call with `asyncio.to_thread()`.

## Async Model Methods

### async_create()

Create and save a new model instance asynchronously.

```python
import asyncio
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()
    favorite_color = Field()

async def main():
    lisa = await Person.async_create(
        name="Lalisa Manobal",
        favorite_color="yellow"
    )
    print(f"{lisa.name} likes {lisa.favorite_color}.")

asyncio.run(main())
```

### async_save()

Save changes to an existing model instance asynchronously.

```python
async def update_person():
    lisa = Person(name="Lalisa Manobal", favorite_color="yellow")
    await lisa.async_save()

    # Update a field
    lisa.favorite_color = "pink"
    await lisa.async_save()
```

### async_delete()

Delete a model instance asynchronously.

```python
async def remove_person():
    lisa = await Person.query.async_get(name="Lalisa Manobal")
    result = await lisa.async_delete()
    print(f"Deleted: {result}")  # => True if deleted
```

### async_load()

Load a model instance by its key fields asynchronously.

```python
async def load_person():
    lisa = await Person.async_load(name="Lalisa Manobal")
    if lisa:
        print(f"Found: {lisa.name}")
```

## Async Query Methods

### async_get()

Retrieve a single instance asynchronously.

```python
async def get_person():
    lisa = await Person.query.async_get(name="Lalisa Manobal")
    if lisa:
        print(f"{lisa.name} likes {lisa.favorite_color}.")
```

### async_filter()

Filter instances based on field values asynchronously.

```python
from datetime import datetime, timedelta
from popoto import Model, KeyField, SortedField, GeoField

class Person(Model):
    name = KeyField()
    level = SortedField(type=int)
    last_active = SortedField(type=datetime)
    location = GeoField()

async def find_active_people():
    yesterday = datetime.now() - timedelta(days=1)

    active_people = await Person.query.async_filter(
        level__gte=50,
        last_active__gt=yesterday
    )

    print(f"Found {len(active_people)} active people")
```

### async_all()

Retrieve all instances asynchronously.

```python
async def get_all_people():
    all_people = await Person.query.async_all()
    print(f"Total people: {len(all_people)}")
```

### async_count()

Count instances matching filter criteria asynchronously.

```python
async def count_people():
    total = await Person.query.async_count()
    high_level = await Person.query.async_count(level__gte=50)

    print(f"Total: {total}, High level: {high_level}")
```

### async_keys()

Retrieve Redis keys for model instances asynchronously.

```python
async def list_keys():
    keys = await Person.query.async_keys()
    print(f"Redis keys: {keys}")
```

## Concurrent Operations

You can use `asyncio.gather()` to run multiple operations concurrently for improved performance.

```python
async def concurrent_example():
    # Create multiple instances concurrently
    people = await asyncio.gather(
        Person.async_create(name="Lisa", favorite_color="yellow"),
        Person.async_create(name="Jennie", favorite_color="black"),
        Person.async_create(name="Rosé", favorite_color="pink"),
        Person.async_create(name="Jisoo", favorite_color="purple"),
    )

    print(f"Created {len(people)} people")

    # Query concurrently
    results = await asyncio.gather(
        Person.query.async_count(),
        Person.query.async_all(),
        Person.query.async_filter(favorite_color="pink"),
    )

    count, all_people, pink_lovers = results
    print(f"Count: {count}, Total: {len(all_people)}, Pink: {len(pink_lovers)}")
```

## Integration with Async Frameworks

### FastAPI Example

```python
from fastapi import FastAPI
from popoto import Model, KeyField, Field

app = FastAPI()

class Person(Model):
    name = KeyField()
    favorite_color = Field()

@app.post("/people/")
async def create_person(name: str, favorite_color: str):
    person = await Person.async_create(
        name=name,
        favorite_color=favorite_color
    )
    return {"name": person.name, "favorite_color": person.favorite_color}

@app.get("/people/{name}")
async def get_person(name: str):
    person = await Person.query.async_get(name=name)
    if not person:
        return {"error": "Person not found"}
    return {"name": person.name, "favorite_color": person.favorite_color}

@app.get("/people/")
async def list_people():
    people = await Person.query.async_all()
    return [
        {"name": p.name, "favorite_color": p.favorite_color}
        for p in people
    ]
```

### Telethon Bot Example

```python
from telethon import TelegramClient, events
from popoto import Model, AutoKeyField, KeyField, Field

class Message(Model):
    message_id = AutoKeyField()
    chat_id = KeyField()
    text = Field()
    sender = Field()

client = TelegramClient('bot', api_id, api_hash)

@client.on(events.NewMessage)
async def handle_message(event):
    # Save message asynchronously
    await Message.async_create(
        chat_id=str(event.chat_id),
        text=event.text,
        sender=event.sender.username
    )

    # Query recent messages
    recent = await Message.query.async_filter(
        chat_id=str(event.chat_id),
        limit=10
    )

    await event.reply(f"Saved! Recent messages: {len(recent)}")

client.start()
client.run_until_disconnected()
```

### Job Queue Example

```python
from popoto import Model, AutoKeyField, KeyField, SortedField, Field

class Job(Model):
    job_id = AutoKeyField()
    project_key = KeyField()
    status = KeyField(default="pending")
    priority = SortedField(type=int, sort_by="project_key")
    created_at = SortedField(type=float, sort_by="project_key")
    message_text = Field()

async def enqueue_job(project: str, message: str, priority: int = 0):
    """Add a new job to the queue."""
    import time

    job = await Job.async_create(
        project_key=project,
        status="pending",
        priority=priority,
        created_at=time.time(),
        message_text=message
    )
    return job

async def dequeue_job(project: str):
    """Get the highest priority pending job."""
    jobs = await Job.query.async_filter(
        project_key=project,
        status="pending",
        order_by="-priority",  # Highest priority first
        limit=1
    )

    if not jobs:
        return None

    job = jobs[0]
    job.status = "running"
    await job.async_save()
    return job

async def complete_job(job):
    """Mark a job as complete and remove it."""
    await job.async_delete()

# Usage
async def worker():
    while True:
        job = await dequeue_job("myproject")
        if job:
            print(f"Processing: {job.message_text}")
            # Do work here
            await complete_job(job)
        else:
            await asyncio.sleep(1)
```

## Implementation Details

### Thread Pool Execution

All async methods use `asyncio.to_thread()` internally to run the synchronous Redis operations in a thread pool. This means:

- **No event loop blocking** - Redis calls don't block the async event loop
- **Automatic thread management** - Python's default executor handles threading
- **Same behavior as sync** - All validation, error handling, and Redis operations work identically

### Python Version Compatibility

Async methods work with Python 3.8+. For Python 3.8, a compatibility shim is used since `asyncio.to_thread()` was added in Python 3.9.

### Performance Considerations

- **Sub-millisecond operations**: Most Redis operations complete in under 1ms, making thread pool overhead negligible
- **Concurrent operations**: Use `asyncio.gather()` to run multiple independent operations in parallel
- **Batching with pipelines**: Pipeline support works the same way in async methods

### Future Optimization

The current implementation uses thread pools for simplicity and maintainability. A future optimization could use `redis.asyncio` for native async Redis operations, but benchmarking would be needed to justify the added complexity.

## Error Handling

Async methods raise the same exceptions as their synchronous counterparts:

```python
from popoto.models.base import ModelException
from popoto.models.query import QueryException

async def safe_create():
    try:
        person = await Person.async_create(
            name="",  # Empty name might violate constraints
            favorite_color="blue"
        )
    except ModelException as e:
        print(f"Model error: {e}")
    except QueryException as e:
        print(f"Query error: {e}")
```

## See Also

- [Models and Fields](fields.md) - Define your data models
- [Making Queries](query.md) - Query patterns and filters
- [Model Meta Options](meta.md) - Configure model behavior
