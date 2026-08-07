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
pool = redis.BlockingConnectionPool(host="127.0.0.1", port=6379, db=0)
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

The replacement connection keeps the same pooling policy as the one built at import: a
`BlockingConnectionPool` capped at `POPOTO_SYNC_MAX_CONNECTIONS` (default 128). Excess
callers wait for a free connection instead of opening an unbounded number of sockets.
`set_async_redis_db_settings()` behaves the same way, using `POPOTO_ASYNC_MAX_CONNECTIONS`.

Two cases opt out of the managed pool and use whatever you supply:

```python
# Your own pool, used as-is
set_REDIS_DB_settings(connection_pool=my_pool)

# Positional arguments — redis.Redis() and BlockingConnectionPool()
# do not share a positional signature, so they cannot be forwarded to a pool
set_REDIS_DB_settings("", "localhost", 6379)
```

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

## Content and Embedding Configuration

Use `popoto.configure()` to set global defaults for `ContentField` and `EmbeddingField`.
Call this once at application startup, before creating or querying any models that use
these field types.

```python
import popoto
from popoto.embeddings.voyage import VoyageProvider

popoto.configure(
    embedding_provider=VoyageProvider(api_key="your-key"),
    content_path="/data/popoto-content",
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `embedding_provider` | `AbstractEmbeddingProvider` | `None` | Default embedding provider for all EmbeddingFields and `semantic_search()`. |
| `content_store` | `AbstractContentStore` | `None` | Default content store for all ContentFields. Defaults to `FilesystemStore`. |
| `content_path` | `str` | `None` | Base directory for filesystem content storage. Overrides `POPOTO_CONTENT_PATH`. Only applies with the default `FilesystemStore`. |

You can also set per-field overrides by passing `store=` to `ContentField` or
`provider=` to `EmbeddingField`. Per-field settings take precedence over the
global configuration.

See [ContentField](fields.md#contentfield) and [EmbeddingField](fields.md#embeddingfield)
for field-level configuration.

### Multi-Worker Cache Invalidation

`EmbeddingField` keeps a per-process embedding matrix cache. In multi-worker
deployments (gunicorn, multiple containers/pods), the `POPOTO_EMBEDDING_INVALIDATION`
environment variable controls how peer processes are notified when a write
invalidates that cache:

| Value | Behavior |
|-------|----------|
| `pubsub` *(default)* | Valkey pub/sub notifies all workers within ~100 ms; falls back to an on-disk version check if the subscriber can't start. |
| `mtime` | No pub/sub; each worker reloads on the next `semantic_search()` after a peer write, using an on-disk `_version` counter. For batch/offline deployments without a live Valkey connection. |
| `none` | Pre-fix single-process behavior — zero overhead, no cross-process invalidation. |

See [EmbeddingField → Multi-Worker Deployments](fields.md#embeddingfield) for the
full staleness-window table and the cost of the default mode.

### Embedding Providers

Popoto ships with three built-in embedding providers. All implement the
`AbstractEmbeddingProvider` interface from `popoto.embeddings`.

#### Voyage AI

Install the optional dependency and configure:

```bash
pip install popoto[voyage]
```

```python
from popoto.embeddings.voyage import VoyageProvider

provider = VoyageProvider(
    api_key="your-voyage-key",  # or set VOYAGE_API_KEY env var
    model="voyage-3",           # default model, 1024 dimensions
)
popoto.configure(embedding_provider=provider)
```

Voyage AI supports `input_type` hints (`"document"` for indexing, `"query"` for
search) to optimize embeddings for retrieval. Popoto passes these automatically
when saving vs searching. Batch size limit is 128 texts per API call.

#### OpenAI

Install the optional dependency and configure:

```bash
pip install popoto[openai]
```

```python
from popoto.embeddings.openai import OpenAIProvider

provider = OpenAIProvider(
    api_key="your-openai-key",       # or set OPENAI_API_KEY env var
    model="text-embedding-3-small",  # default model
    dim=1536,                        # default dimensions
)
popoto.configure(embedding_provider=provider)
```

OpenAI embeddings ignore the `input_type` parameter. Batch size limit is 2048
texts per API call.

#### Ollama (local)

Local embeddings via a running Ollama server. No API key, no network
round-trip, no per-token cost. Uses stdlib only (no extras to install).

Prerequisites: install Ollama from <https://ollama.com>, pull an
embedding model, and start the server:

```bash
ollama pull nomic-embed-text
ollama serve
```

```python
import popoto
from popoto.embeddings.ollama import OllamaProvider

provider = OllamaProvider(
    base_url="http://localhost:11434",  # default
    model="nomic-embed-text",           # default (768-dim)
    dim=None,                           # auto-detect on first embed()
)
popoto.configure(embedding_provider=provider)
```

Ollama ignores the `input_type` parameter. Default batch size limit is
32 texts per call (conservative for local inference; subclass to raise
it). If the server is unreachable, you will get a `RuntimeError` that
points at `ollama serve`; if the model is missing, the error points at
`ollama pull <model>`.

#### Custom Providers

Implement `AbstractEmbeddingProvider` to use any embedding service:

```python
from popoto.embeddings import AbstractEmbeddingProvider

class MyProvider(AbstractEmbeddingProvider):
    def embed(self, texts, input_type=None):
        # Call your embedding API here
        return [vector_for(t) for t in texts]

    @property
    def dimensions(self):
        return 768  # your vector size

    @property
    def max_batch_size(self):
        return 100
```

### Content Storage Path

ContentField stores large values on the filesystem instead of in Redis.
The storage location is resolved in this order:

1. `content_path` argument to `popoto.configure()`
2. `POPOTO_CONTENT_PATH` environment variable
3. Default: `~/.popoto/content`

```bash
# Set via environment variable
export POPOTO_CONTENT_PATH="/data/popoto-content"
```

```python
# Or set via configure()
popoto.configure(content_path="/data/popoto-content")
```

Files are organized by model class name under the base path, using
content-addressable storage (SHA-256) for versioning. You can also supply a
custom `AbstractContentStore` implementation (e.g., for S3 or GCS) via the
`content_store` parameter:

```python
from popoto.stores import AbstractContentStore

class S3Store(AbstractContentStore):
    def save(self, content, key, model_class_name):
        # upload to S3, return reference string
        ...
    def load(self, reference):
        # download from S3
        ...
    def delete(self, reference):
        ...
    def exists(self, reference):
        ...

popoto.configure(content_store=S3Store())
```

## Error Reporting (Opt-In)

Popoto includes optional, opt-in error reporting that sends library-specific
exceptions to the Popoto maintainers via [Sentry](https://sentry.io). This
helps the maintainers discover and fix bugs that users encounter in the wild.

**Error reporting is disabled by default.** Nothing is sent unless you
explicitly enable it.

### Installation

Install Popoto with the `monitoring` extra to include `sentry-sdk`:

```bash
pip install popoto[monitoring]
```

### Enabling

Call `enable_error_reporting()` once at application startup:

```python
import popoto

popoto.enable_error_reporting()
```

When enabled, Popoto-specific exceptions (such as `ModelException` and
`QueryException`) are automatically reported in the background. The reporter:

- Uses an **isolated Sentry client** that does not interfere with your
  application's own `sentry_sdk.init()` or global Sentry configuration
- Sends events **asynchronously** via a background thread -- no added latency
- **Silently degrades** if `sentry-sdk` is not installed, the network is
  unavailable, or any internal error occurs
- Never re-raises, never logs, never delays your application

### Custom DSN

To send reports to your own Sentry project instead of the Popoto maintainers,
set the `POPOTO_SENTRY_DSN` environment variable:

```bash
export POPOTO_SENTRY_DSN="https://your-key@your-org.ingest.sentry.io/your-project"
```

Or pass the DSN directly:

```python
popoto.enable_error_reporting(dsn="https://your-key@your-org.ingest.sentry.io/your-project")
```

### What Gets Sent

- Exception type, message, and traceback
- Popoto version and Python version
- No personally identifiable information beyond Sentry's default PII scrubbing

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | *(empty)* | Redis connection URL. Falls back to localhost:6379. |
| `BEGINNING_OF_TIME` | `0` | Unix timestamp used as the minimum time boundary for time-based queries. |
| `POPOTO_CONTENT_PATH` | `~/.popoto/content` | Base directory for ContentField filesystem storage and EmbeddingField `.npy` files. |
| `POPOTO_LOG_LEVEL` | `WARNING` | Log level for POPOTO-REDIS_DB logger (DEBUG, INFO, WARNING, ERROR, CRITICAL) |
| `POPOTO_SENTRY_DSN` | *(built-in)* | Override the Sentry DSN used by `enable_error_reporting()`. |
| `POPOTO_TEST_DB` | `15` | Redis DB number used by the pytest plugin for test isolation. Overrides the `popoto_test_db` ini option. DB 0 is rejected to prevent accidental production data loss. |
| `POPOTO_ASYNC_MAX_CONNECTIONS` | `128` | Maximum async Redis connection pool size (BlockingConnectionPool). |
| `POPOTO_SYNC_MAX_CONNECTIONS` | `128` | Maximum sync Redis connection pool size (BlockingConnectionPool). |

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
| `POPOTO.ContentField` | Content storage and lazy-loading operations |
| `POPOTO.EmbeddingField` | Embedding generation, caching, and storage |
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
