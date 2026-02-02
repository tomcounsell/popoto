# Configuration

Popoto connects to Redis automatically when imported. By default it connects to `localhost:6379`, which works for local development. For production, set the `REDIS_URL` environment variable.

## Redis Connection

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
```

When `REDIS_URL` is set, Popoto calls `redis.from_url()` to establish the connection.

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

### Redis CLI

You can inspect Popoto's data directly using `redis-cli`:

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

## Logging

Popoto uses Python's `logging` module with these logger names:

| Logger | Purpose |
|--------|---------|
| `POPOTO-REDIS_DB` | Connection events and memory info |
| `POPOTO.model_base` | Model save/load/delete operations |
| `POPOTO.Query` | Query execution details |
| `POPOTO.field` | Field validation errors |
| `POPOTO.KeyFieldMixin` | Key field index operations |
| `POPOTO.SortedFieldMixin` | Sorted set operations |
| `POPOTO.GeoField` | Geo index operations |
| `POPOTO.Relationship` | Relationship field operations |
| `POPOTO-publisher` | Pub/sub publish events |
| `POPOTO-subscriber` | Pub/sub message handling |

Configure logging to see Popoto's debug output:

```python
import logging

logging.basicConfig(level=logging.DEBUG)
# Or target specific loggers
logging.getLogger("POPOTO.Query").setLevel(logging.DEBUG)
```
