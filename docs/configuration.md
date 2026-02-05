# Configuration

Popoto connects to Redis or Valkey automatically when imported. By default it connects to `localhost:6379`, which works for local development. For production, set the `REDIS_URL` environment variable.

## Redis/Valkey Connection

Popoto works with both Redis and [Valkey](https://valkey.io) (the open-source Redis fork). The same configuration works for either - just point `REDIS_URL` at your server.

### Using REDIS_URL (Recommended)

Set `REDIS_URL` as an environment variable before starting your application.

```bash
export REDIS_URL="redis://localhost:6379/0"
```

The URL format follows the Redis URI scheme:

```
redis://[[username:]password@]host[:port][/database]
```

Common examples:

```bash
# Local development (default if REDIS_URL is not set)
REDIS_URL="redis://localhost:6379/0"

# Remote Redis with password
REDIS_URL="redis://:mypassword@redis.example.com:6379/0"

# Redis with username and password (Redis 6+)
REDIS_URL="redis://myuser:mypassword@redis.example.com:6379/0"

# Heroku Redis, Render, Railway, etc.
REDIS_URL="redis://default:abc123@some-host.cloud:6379"

# Valkey (same format works)
REDIS_URL="redis://localhost:6379/0"
```

When `REDIS_URL` is set, Popoto calls `redis.from_url()` to establish the connection. This works with both Redis and Valkey servers.

### Default Connection

If `REDIS_URL` is not set, Popoto connects to `127.0.0.1:6379` using a connection pool:

```python
# This is what Popoto does internally when REDIS_URL is not set
pool = redis.ConnectionPool(host="127.0.0.1", port=6379, db=0)
POPOTO_REDIS_DB = redis.Redis(connection_pool=pool)
```

No configuration is needed for local development with Redis running on the default port.

## Reconfiguring at Runtime

Use `set_REDIS_DB_settings()` to change the Redis connection after import. This is useful for testing or multi-environment setups.

```python
from popoto.redis_db import set_REDIS_DB_settings

# Connect to a different Redis instance
set_REDIS_DB_settings(host="redis.example.com", port=6380, db=1)

# Connect with password
set_REDIS_DB_settings(host="redis.example.com", port=6379, password="secret")
```

The function accepts the same keyword arguments as `redis.Redis()`.

!!! warning
    Calling `set_REDIS_DB_settings()` replaces the global connection. Any in-flight operations on the old connection may fail.

## Accessing the Connection Directly

If you need to run raw Redis commands, you can access the connection object:

```python
from popoto.redis_db import get_REDIS_DB

redis_db = get_REDIS_DB()

# Run raw Redis commands
redis_db.ping()
redis_db.info()
```

## Debugging

### Print Redis Info

Use `print_redis_info()` to log memory usage and server info:

```python
from popoto.redis_db import print_redis_info

print_redis_info()
# Logs memory usage percentage and server info to the POPOTO-REDIS_DB logger
```

### Redis/Valkey CLI

You can inspect Popoto's data directly using `redis-cli` (or `valkey-cli` for Valkey - commands are identical):

```bash
redis-cli

# List all keys for a model
KEYS Restaurant:*

# Inspect a specific instance (stored as a hash)
HGETALL Restaurant:Burger{&#58;}Palace

# Check sorted set indexes
ZRANGE "$SortedF:Restaurant:rating" 0 -1 WITHSCORES

# Check geo indexes
GEOPOS "$GeoF:Restaurant:location" "Restaurant:Burger Palace"

# Watch all Redis commands in real time
MONITOR
```

See the [CLAUDE.md](https://github.com/tomcounsell/popoto) debugging section for more Redis CLI patterns.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | *(empty)* | Redis connection URL. Falls back to localhost:6379. |
| `BEGINNING_OF_TIME` | `0` | Unix timestamp used as the minimum time boundary for time-based queries. |
| `POPOTO_LOG_LEVEL` | `WARNING` | Log level for POPOTO-REDIS_DB logger (DEBUG, INFO, WARNING, ERROR, CRITICAL) |

## Thread Safety

### What IS Thread-Safe

Redis connections in Popoto use a connection pool, which is thread-safe. Multiple
threads can safely execute Redis operations concurrently:

```python
from concurrent.futures import ThreadPoolExecutor
from popoto import Model, KeyField, Field

class Counter(Model):
    name = KeyField()
    value = Field(type=int, default=0)

def increment(name):
    counter = Counter.query.get(name=name)
    counter.value += 1
    counter.save()

# Safe: each thread gets its own connection from the pool
with ThreadPoolExecutor(max_workers=10) as pool:
    pool.map(increment, ["counter1"] * 10)
```

!!! warning
    The example above has a race condition in the read-modify-write pattern.
    While the Redis *connection* is thread-safe, the *logic* of reading a value,
    modifying it in Python, and writing it back is not atomic. Use Redis
    transactions or Lua scripts for atomic operations.

### What is NOT Thread-Safe

Model instances should not be shared across threads. Each thread should create
or load its own instances:

```python
# UNSAFE: sharing instance across threads
user = User.query.get(username="alice")
# Don't pass `user` to another thread

# SAFE: each thread loads its own instance
def process_user(username):
    user = User.query.get(username=username)
    # work with user
```

### Best Practices

1. **Create model instances per-thread** — don't share instances across threads
2. **Use atomic Redis operations** for concurrent updates to the same key
3. **Consider async** for I/O-bound workloads (see [Async Operations](async.md))
4. **Use pipelines** for batch operations within a single thread

!!! tip
    For high-concurrency scenarios, consider using Popoto's async API instead of
    threading. See [Async Operations](async.md) for details.

## Logging

Popoto uses Python's standard logging module. You can configure log levels
globally or per-logger.

### Environment Variable

Set `POPOTO_LOG_LEVEL` to control the default log level for Popoto's Redis
connection logger:

```bash
export POPOTO_LOG_LEVEL=DEBUG  # Show all connection details
export POPOTO_LOG_LEVEL=INFO   # Show connection events
export POPOTO_LOG_LEVEL=WARNING  # Default - only warnings and errors
export POPOTO_LOG_LEVEL=ERROR  # Only errors
```

### Programmatic Configuration

For finer control, configure individual loggers:

```python
import logging

# Set all Popoto loggers to DEBUG
for name in [
    "POPOTO-REDIS_DB",
    "POPOTO.model_base",
    "POPOTO.Query",
    "POPOTO.field",
    "POPOTO.KeyFieldMixin",
    "POPOTO.SortedFieldMixin",
    "POPOTO.GeoField",
    "POPOTO.Relationship",
    "POPOTO-publisher",
    "POPOTO-subscriber",
]:
    logging.getLogger(name).setLevel(logging.DEBUG)

# Or configure a specific logger
logging.getLogger("POPOTO.Query").setLevel(logging.DEBUG)
```

### Logger Reference

| Logger Name | Purpose |
|------------|---------|
| `POPOTO-REDIS_DB` | Connection events, errors, health checks |
| `POPOTO.model_base` | Model creation, metaclass operations |
| `POPOTO.Query` | Query execution, filtering, results |
| `POPOTO.field` | Field validation, type checking |
| `POPOTO.KeyFieldMixin` | Key field operations |
| `POPOTO.SortedFieldMixin` | Sorted set index operations |
| `POPOTO.GeoField` | Geographic queries and indexing |
| `POPOTO.Relationship` | Relationship loading and saving |
| `POPOTO-publisher` | PubSub publishing events |
| `POPOTO-subscriber` | PubSub subscription events |

### Integration with Frameworks

**Django:**
```python
# settings.py
LOGGING = {
    'version': 1,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
    },
    'loggers': {
        'POPOTO-REDIS_DB': {
            'handlers': ['console'],
            'level': 'INFO',
        },
    },
}
```

**Flask:**
```python
import logging
logging.getLogger("POPOTO-REDIS_DB").setLevel(logging.INFO)
app.logger.info("Popoto logging configured")
```

!!! tip
    During development, set `POPOTO_LOG_LEVEL=DEBUG` to see all Redis
    operations. In production, use `WARNING` or `ERROR` to reduce noise.
