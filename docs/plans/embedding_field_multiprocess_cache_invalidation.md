---
status: Ready
type: bug
appetite: Medium
owner: valorengels
created: 2026-05-28
tracking: https://github.com/tomcounsell/popoto/issues/403
last_comment_id:
revision_applied: true
---

# EmbeddingField: Cross-Process Cache Invalidation

## Problem

`EmbeddingField` maintains a module-level dict `_embedding_cache` (keyed by model class name)
holding a pre-normalized numpy matrix and Redis key list per model class. When a record is
written or deleted, `invalidate_cache()` clears that dict — but only in the calling process.
Any other worker process holding the same model class in memory continues serving the stale
matrix indefinitely, with no observable error signal.

**Current behavior:**
In a multi-worker deployment (gunicorn multi-worker, multiple containers, multiple pods),
worker A writes an `EmbeddingField`-backed record and its cache is cleared. Workers B, C, …
are never notified and continue returning semantic search results that omit newly written
records or include deleted ones. There is no log warning, no stale flag, no detectable signal
that results are wrong.

**Desired outcome:**
All live worker processes sharing the same Popoto corpus invalidate (or refresh) their
embedding matrix within a documented staleness window after a write on any peer process.
Single-process deployments are unaffected. The staleness mechanism and its limitations are
documented in the `EmbeddingField` docstring and in `docs/fields.md`.

## Freshness Check

**Baseline commit:** `2640b4ffb1a5e0788ceebeaece67dbe928840291`
**Issue filed at:** 2026-05-25T10:18:16Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/fields/embedding_field.py:49` — `_embedding_cache = {}` module-level dict — **confirmed at line 49**
- `src/popoto/fields/embedding_field.py:114` — `invalidate_cache()` clears dict, no cross-process signal — **confirmed at lines 114–119**
- `src/popoto/pubsub/publisher.py` — `Publisher` class with `publish()` method using `POPOTO_REDIS_DB.publish()` — **confirmed**
- `src/popoto/pubsub/subscriber.py` — `Subscriber` ABC with `run_in_thread`-compatible polling via `get_message()` — **confirmed**

**Cited sibling issues/PRs re-checked:**
- PR #261 (ContentField and EmbeddingField) — merged 2026-03-23. Shipped the original single-process cache design. Root cause confirmed present.
- PR #315 (fix embedding cache filenames) — merged 2026-03-31. Addressed filename length issue; did not change invalidation semantics.

**Commits on main since issue was filed (touching referenced files):**
- None — `git log --oneline --since="2026-05-25T10:18:16Z" -- src/popoto/fields/embedding_field.py src/popoto/pubsub/` returned empty.

**Active plans in `docs/plans/` overlapping this area:** None — the `embedding-cache-filename.md` plan addressed filename length; it is complete and does not overlap the invalidation problem.

**Notes:** All issue claims verified against HEAD. Line numbers are stable.

## Prior Art

- **PR #261** (ContentField and EmbeddingField, merged 2026-03-23) — Introduced `EmbeddingField` with intentionally single-process cache semantics ("Cache is invalidated on save/delete within the same process"). No cross-process invalidation was in scope for that PR.
- **PR #315** (Fix embedding cache filenames, merged 2026-03-31) — Fixed SHA-256 filename hashing to avoid 255-byte filesystem limit. Updated `_index.json` sidecar. Did not change invalidation semantics.
- **PR #358** (Add OllamaProvider, merged 2026-04-17) — Added local embedding provider. No invalidation changes.

No prior attempt has been made to add cross-process cache invalidation to `EmbeddingField`.

## Research

**Queries used:**
1. "Redis pub/sub cross-process cache invalidation Python multi-worker daemon thread pattern 2025"
2. "file mtime version sentinel cache staleness check Python os.stat performance overhead"
3. "Python redis-py pubsub run_in_thread daemon subscribe background thread reconnect 2025"

**Key findings:**

- **Redis pub/sub invalidation bus pattern** ([Redis docs](https://redis.io/docs/latest/develop/use-cases/pub-sub/redis-py/), [oneuptime cache coherence](https://oneuptime.com/blog/post/2026-03-31-redis-cache-coherence-multi-node/)): The standard multi-node pattern publishes to a channel like `cache:invalidate:{ClassName}` on write; each worker subscribes in a background thread and drops its local cache entry on receipt. Two-tier (local + Redis) provides low-latency reads while keeping data eventually consistent after writes.

- **`run_in_thread(daemon=True)` is the right primitive** ([redis-py issue #816](https://github.com/andymccurdy/redis-py/issues/816), [redis-py docs](https://redis.io/docs/latest/develop/use-cases/pub-sub/redis-py/)): redis-py's `pubsub.run_in_thread(sleep_time=0.1, daemon=True, exception_handler=...)` creates a background `PubSubWorkerThread` that loops calling `get_message()` and dispatches to registered handlers.

- **CORRECTION (was wrong in the first draft): `exception_handler` does NOT keep the thread alive.** In redis-py the `PubSubWorkerThread.run()` loop catches an exception, calls `exception_handler(e, pubsub, thread)`, and then **stops the thread** (sets `_running = False` and breaks the loop). The handler is a notification/cleanup hook, not a retry mechanism. The thread terminates on the first unhandled connection error. **Implication for the plan:** we MUST add our own reconnect supervision. The `exception_handler` records the failure and clears the model entry from `_listener_threads` so the next `load_embeddings()` call re-runs `_start_invalidation_listener()` and spawns a fresh thread (lazy self-healing). This is the corrected design — the earlier "stays alive and retries on next poll" claim was false.

- **`socket_timeout` interaction** (verified against `redis_db.py:113,122`): `POPOTO_REDIS_DB` is constructed with `socket_timeout=5`. `run_in_thread(sleep_time=0.1)` uses the **non-blocking** `get_message(timeout=...)` path with a short poll, so the 5s socket timeout does not fire on an idle subscription the way a blocking `listen()` would. However, a dropped TCP connection still surfaces as a `ConnectionError`/`TimeoutError` inside the worker loop — which (per the correction above) stops the thread. The lazy-respawn-on-next-load behavior handles this; no separate keepalive is needed.

- **mtime sentinel performance** ([apenwarr mtime blog](https://apenwarr.ca/log/20181113), [GeeksforGeeks os.stat](https://www.geeksforgeeks.org/python/python-os-stat-method/)): `os.stat()` adds ~0.3–0.4 µs on a warm local filesystem (1000 iterations ≈ 0.4 ms). For a `semantic_search()` call that already does numpy matrix multiplication over N embeddings, one extra `os.stat()` on `_index.json` is negligible. The caveat is 1-second mtime granularity on some HFS+ volumes (macOS); ext4/APFS/XFS have nanosecond resolution. For a version counter in `_index.json` this is fine because the counter is an integer, not a timestamp.

- **Path B note** (version sentinel in `_index.json`): `_index.json` already exists per model class and is atomically written on every save/delete. Adding a `_version` integer to it costs zero new files. A worker reads `os.stat()` on the file before each search; if mtime changed since last load, it reloads.

## Spike Results

### spike-1: Which paths does `invalidate_cache()` get called from?
- **Assumption**: "invalidate_cache() is only called from on_save() and on_delete()"
- **Method**: code-read
- **Finding**: Confirmed. `invalidate_cache(model_class_name)` is called at `embedding_field.py:374` (on_save) and `embedding_field.py:406` (on_delete). No other call sites in the codebase.
- **Confidence**: high
- **Impact on plan**: The two hooks are the only insertion points for the cross-process signal. No other paths need to be patched.

### spike-2: Does `_index.json` support a version counter without schema breakage?
- **Assumption**: "Adding a top-level `_version` key to _index.json won't break existing readers"
- **Method**: code-read
- **Finding**: `_read_index()` at `embedding_field.py:78–94` reads the JSON and returns it as-is if it's a dict. Existing readers only look up filename keys (`filename.npy → redis_key`). A `_version` key at the top level won't collide with any `.npy`-suffixed filename and will be silently ignored by `load_embeddings()` (which iterates only `.npy` entries from `os.listdir`). Safe to add.
- **Confidence**: high
- **Impact on plan**: Path B version counter can live inside `_index.json` with zero schema migration; old readers treat it as an unrecognized key.

### spike-3: Is there a `run_in_thread` method already available on the Subscriber class?
- **Assumption**: "`Subscriber` exposes a way to start a background polling thread"
- **Method**: code-read
- **Finding**: `Subscriber` does NOT use `run_in_thread`. It exposes `__call__()` which polls once via `pubsub.get_message()`. For the cache invalidation use-case, we will call `pubsub.run_in_thread(daemon=True)` directly on the redis-py pubsub object, bypassing the `Subscriber` ABC. This keeps the invalidation mechanism self-contained in `embedding_field.py` without modifying the pub/sub layer.
- **Confidence**: high
- **Impact on plan**: Implementation uses raw `redis.pubsub().run_in_thread()` rather than `Subscriber`. No changes to `src/popoto/pubsub/`.

## Data Flow

**Path A — Pub/Sub invalidation (primary, single-host multi-worker):**

1. **Entry**: `model_instance.save()` or `model_instance.delete()` is called on worker A.
2. **on_save / on_delete hook**: `EmbeddingField.on_save()` / `on_delete()` writes/removes the `.npy` file and updates `_index.json`, then calls `invalidate_cache(model_class_name)` (clears worker A's cache — existing behavior unchanged).
3. **Publish**: After `invalidate_cache()`, the hook publishes the bare model class name as the message body to the per-class Valkey channel `popoto:embedding:invalidate:ClassName`. The channel name fully identifies the target model, so the message body is informational only — the handler is bound per-channel and does not parse the body. (Payload is the bare `model_class_name` string, NOT a JSON object — the earlier draft's `{"model":...,"action":...}` was dropped to keep publish/handler trivially consistent.)
4. **Background subscriber thread** (each worker): On first `load_embeddings()` call for a model class, a daemon thread is started (if not already running) via `pubsub.run_in_thread(daemon=True, exception_handler=...)`. The thread subscribes to `popoto:embedding:invalidate:ClassName`.
5. **Invalidation receipt**: When workers B, C receive the message, the handler calls `invalidate_cache(model_class_name)`. Next `load_embeddings()` (driven by `query.semantic_search()` at `query.py:719` / `query.py:1044`) on those workers reloads from disk.
6. **Output**: All workers serve fresh matrix within one Valkey round-trip plus the worker's `sleep_time` poll interval (≤ 100 ms + RTT). The publishing worker also receives its own message (loopback) and re-invalidates an already-cleared entry — a harmless no-op.

**Path B — File-mtime / version sentinel (secondary, batch/offline/NFS):**

1. **Entry**: `on_save()` / `on_delete()` completes `_write_index()`. The `_index.json` mtime is implicitly updated by the atomic rename.
2. **Read path**: Before returning a cached matrix, `load_embeddings()` calls `os.stat(_index_path(model_name)).st_mtime`. If the mtime differs from the cached `_index_mtime` stored alongside the matrix, the cache entry is dropped and reloaded.
3. **Reload**: New matrix is loaded from disk, normalized, and re-cached with the new mtime.
4. **Output**: Workers detect staleness on the next `semantic_search()` call after a write, with no Valkey connection required.

## Architectural Impact

- **New dependencies**: None. Uses existing redis-py `pubsub()` API and `POPOTO_REDIS_DB` connection. numpy already optional.
- **Interface changes**: None. `semantic_search()`, `load_embeddings()`, `on_save()`, `on_delete()` signatures unchanged. New `_start_invalidation_listener()` is internal.
- **Coupling**: `embedding_field.py` gains a soft dependency on the Valkey pub/sub feature. If `POPOTO_REDIS_DB.pubsub()` throws (e.g., ACL restriction), the listener fails to start and a WARNING is logged — Path B (mtime sentinel) continues to work without it.
- **Data ownership**: No change. `_embedding_cache` remains process-local; Path A/B are invalidation signals, not shared state.
- **Reversibility**: A feature flag `POPOTO_EMBEDDING_INVALIDATION=pubsub|mtime|none` (env var, default `pubsub`) allows rollback without code changes.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (scope alignment on dual-path vs. single path)
- Review rounds: 1

## Solution

The implementation ships **both paths** as opt-in modes controlled by an env var:

```
POPOTO_EMBEDDING_INVALIDATION=pubsub   # default (Path A)
POPOTO_EMBEDDING_INVALIDATION=mtime   # Path B
POPOTO_EMBEDDING_INVALIDATION=none    # opt-out / single-process behavior
```

Default is `pubsub`. Setting to `mtime` is for batch/offline deployments without a live Valkey connection. Setting to `none` restores pre-fix behavior for single-process apps that want zero overhead.

### Path A — Pub/Sub invalidation

**Channel naming:** `popoto:embedding:invalidate:{ModelClassName}`

**Publish side** (in `on_save` and `on_delete`, after `invalidate_cache()`):

```python
_INVALIDATION_MODE = os.environ.get("POPOTO_EMBEDDING_INVALIDATION", "pubsub")

def _publish_invalidation(model_class_name: str) -> None:
    if _INVALIDATION_MODE != "pubsub":
        return
    try:
        from ..redis_db import POPOTO_REDIS_DB
        channel = f"popoto:embedding:invalidate:{model_class_name}"
        POPOTO_REDIS_DB.publish(channel, model_class_name)
    except Exception as e:
        logger.warning(f"EmbeddingField: publish invalidation failed: {e}")
```

**Subscribe side** (lazy-started on first `load_embeddings()` call):

```python
_listener_threads: dict[str, Any] = {}  # model_class_name -> PubSubThread

def _start_invalidation_listener(model_class_name: str) -> None:
    if _INVALIDATION_MODE != "pubsub":
        return
    if model_class_name in _listener_threads:
        return
    try:
        from ..redis_db import POPOTO_REDIS_DB
        channel = f"popoto:embedding:invalidate:{model_class_name}"
        ps = POPOTO_REDIS_DB.pubsub()
        ps.subscribe(**{channel: lambda msg: invalidate_cache(model_class_name)})

        def _exception_handler(ex, ps_obj, worker):
            # redis-py STOPS the worker thread after this handler returns.
            # Drop the registry entry so the next load_embeddings() call
            # lazily respawns a fresh listener (self-healing reconnect).
            logger.warning(f"EmbeddingField invalidation listener error: {ex}")
            _listener_threads.pop(model_class_name, None)
            try:
                ps_obj.close()
            except Exception:
                pass

        thread = ps.run_in_thread(
            sleep_time=0.1, daemon=True, exception_handler=_exception_handler
        )
        _listener_threads[model_class_name] = thread
    except Exception as e:
        logger.warning(f"EmbeddingField: failed to start invalidation listener: {e}")
        _listener_threads.pop(model_class_name, None)
```

The thread is a daemon thread — it will not prevent process shutdown. Because
redis-py terminates the worker on any connection error, reconnect is handled by
the lazy-respawn pattern: the `exception_handler` removes the registry entry, and
the next `load_embeddings()` call starts a new listener. There is a bounded
staleness window equal to the time between the connection drop and the next
`semantic_search()` call on that worker — documented below.

**Teardown for tests and graceful shutdown:** add a module-level
`stop_invalidation_listeners()` that iterates `_listener_threads`, calls
`thread.stop()` on each `PubSubWorkerThread`, and clears the registry. The pytest
isolation fixture (DB 15 flush) must call this in teardown so listener threads do
not accumulate across tests or hold subscriptions against a flushed DB.

### Path B — version-counter sentinel (mtime as cheap pre-check)

`_write_index()` is extended to bump a monotonic integer `_version` key on every
write. Because spike-2 confirmed `_read_index()` returns the dict as-is and
`load_embeddings()` only iterates `.npy`-suffixed keys, a top-level `_version`
integer is schema-safe (old readers ignore it).

```python
def _write_index(model_class_name, index_dict):
    # ... existing atomic write, plus:
    index_dict["_version"] = index_dict.get("_version", 0) + 1
    # (write as before)
```

The cache entry stores both the `_index.json` mtime (cheap change pre-check) and
the `_version` integer (authoritative, granularity-proof) at load time:

```python
_st = os.stat(_index_path(model_name))
_embedding_cache[model_name] = {
    "matrix": matrix,
    "keys": keys,
    "index_mtime": _st.st_mtime,                 # cheap pre-check
    "index_version": _read_index(model_name).get("_version", 0),  # authoritative
}
```

At the top of `load_embeddings()`, before returning cached values (only when
`_INVALIDATION_MODE == "mtime"`):

```python
if model_name in _embedding_cache and _INVALIDATION_MODE == "mtime":
    entry = _embedding_cache[model_name]
    try:
        current_mtime = os.stat(_index_path(model_name)).st_mtime
        if current_mtime != entry.get("index_mtime"):
            # mtime moved — confirm via the authoritative version counter
            if _read_index(model_name).get("_version", 0) != entry.get("index_version"):
                _embedding_cache.pop(model_name, None)  # force reload
    except OSError:
        _embedding_cache.pop(model_name, None)  # index gone, reload
```

Note `_publish_invalidation()` is a no-op in `mtime` mode and the listener is not
started — `mtime` mode does no pub/sub and needs no live Valkey connection.

### Staleness window

| Mode | Staleness window | Notes |
|------|-----------------|-------|
| `pubsub` | Valkey RTT + worker poll interval (`sleep_time=0.1`), i.e. ≤ ~100 ms in practice | Requires live Valkey connection. After a connection drop, the window extends to the next `semantic_search()` call on that worker (lazy respawn). |
| `mtime` | Next `semantic_search()` call after the write lands on disk | One `os.stat()` per search call (~0.4 µs). **mtime-granularity hazard applies to ALL filesystems, not just NFS:** if a peer write and the cached read fall within the same mtime tick (1 s on HFS+/older macOS; ~1 ns on APFS/ext4/XFS), the staleness can be masked. Use a monotonic integer `_version` counter inside `_index.json` instead of raw mtime to eliminate the same-tick hazard (see below). |
| `none` | Never invalidated across processes | Pre-fix single-process behavior. Zero pub/sub overhead, zero extra threads, zero `os.stat()`. |

**mtime vs. version counter:** because mtime resolution can mask a same-tick write,
the `mtime` mode compares a monotonic integer `_version` stored in `_index.json`
(incremented on every `_write_index`) rather than the raw filesystem mtime. `os.stat()`
is still used as a cheap "did the file change at all" pre-check; on a hit it reads
the `_version` integer. This sidesteps the granularity hazard on every filesystem
while keeping the cost to one `os.stat()` (+ a small JSON read only when mtime moved).

## No-Gos

- No Redis modules — uses only core Valkey/Redis commands (`PUBLISH`, `SUBSCRIBE`).
- No new required dependencies. numpy is already gated; this change adds no new `pip install` requirements.
- No API changes to `semantic_search()`, `load_embeddings()`, `on_save()`, `on_delete()`.
- No changes to `src/popoto/pubsub/` — invalidation is self-contained in `embedding_field.py`.
- No changes to the `.npy` / `_index.json` file format on disk for the `mtime` path (mtime is read from the filesystem, not written into the file).

## Rabbit Holes

- **Cross-host NFS mtime granularity**: HFS+ (older macOS) has 1-second mtime resolution. Under pathological conditions (two writes within 1 second on two different hosts), a single mtime tick could mask both changes. Documented as a known limitation; not a production blocker for the common single-host multi-worker case.
- **Thread safety of `_embedding_cache`**: The dict is module-level and GIL-protected on CPython. The lambda handler in the daemon thread calls `invalidate_cache()` which does `_embedding_cache.pop(model_name, None)` — a GIL-atomic operation. No lock needed.
- **Gunicorn pre-fork model**: In pre-fork, workers fork after the parent imports the module. `_listener_threads` starts empty in every worker (fork creates a new process; threads are not inherited). The lazy start in `load_embeddings()` handles this correctly — each worker starts its own listener thread on first use.
- **Connection pool exhaustion**: Each worker-per-model-class creates one additional redis-py connection for pub/sub. In a 4-worker × 3-model-class deployment, that's 12 extra connections. Well within default pool limits (typically 50–100). Not a concern for normal deployments.

## Update System

No update system changes required. This is a library-internal change; the env var `POPOTO_EMBEDDING_INVALIDATION` defaults to `pubsub` with no user action required.

**Honest accounting of the default's cost for single-process apps** (the first draft understated this): with the default `pubsub` mode, a single-process deployment will now (a) start one daemon `PubSubWorkerThread` per model class on first search, (b) hold one extra Valkey connection per such class, and (c) issue one `PUBLISH` per save/delete that loops back and re-invalidates an already-cleared cache (a harmless no-op, but a real Valkey round-trip). This is NOT byte-for-byte "same as before." Apps that genuinely run single-process and want zero overhead should set `POPOTO_EMBEDDING_INVALIDATION=none`. The functional *results* are identical to pre-fix in all three modes for a single process; only the resource profile differs. This trade-off is documented in `docs/fields.md` so operators can choose `none` deliberately.

## Agent Integration

No agent integration required. This is a library-internal correctness fix with no MCP exposure needed.

## Documentation

### Inline Documentation
- [ ] Update `EmbeddingField` class docstring in `src/popoto/fields/embedding_field.py` with:
  - Description of the cross-process invalidation mechanism
  - Staleness window table (pubsub / mtime / none modes)
  - `POPOTO_EMBEDDING_INVALIDATION` env var documentation
- [ ] Update `_embedding_cache` module-level docstring comment to note the invalidation mode

### External Documentation Site
- [ ] Update `docs/fields.md` — `## EmbeddingField` section (around line 1273):
  - Add "Multi-Worker Deployments" subsection explaining the default pub/sub invalidation
  - Document `POPOTO_EMBEDDING_INVALIDATION` env var and its three modes
  - Add staleness window table

## Success Criteria

- [ ] A write to `EmbeddingField` on worker A causes all other live workers sharing the same corpus to invalidate their matrix within the documented staleness window (Valkey RTT + ~100 ms poll interval for `pubsub` mode) — verified by the two-cache simulation test in task 1b
- [ ] Single-process **results** are unaffected in all three modes; the resource profile of the default `pubsub` mode (one daemon thread + one connection per model class + one PUBLISH per write) is documented, and `none` mode is offered for zero-overhead single-process use
- [ ] `POPOTO_EMBEDDING_INVALIDATION=none` starts no listener thread and issues no PUBLISH (verified by test)
- [ ] `POPOTO_EMBEDDING_INVALIDATION=mtime` invalidates cache on next `load_embeddings()` after a peer write, using the `_version` counter (granularity-proof; verified including the same-mtime-tick case)
- [ ] Listener threads self-heal after a connection drop (exception_handler clears the registry; next load respawns) and are stopped by `stop_invalidation_listeners()` — no thread leak across the test suite
- [ ] The staleness mechanism and the default-mode cost trade-off are documented in the `EmbeddingField` docstring and `docs/fields.md`
- [ ] Tests pass: `pytest tests/test_embedding_field.py tests/test_embedding_field_gc.py tests/test_semantic_search.py tests/test_embedding_invalidation.py -x -q`
- [ ] No new required dependencies introduced
- [ ] Daemon thread does not block process shutdown (verified by confirming thread has `daemon=True`)

## Team Orchestration

### Team Members

- **Builder (embedding-invalidation)**
  - Name: embedding-builder
  - Role: Implement Path A (pub/sub) and Path B (mtime sentinel) in `embedding_field.py`; update docstrings
  - Agent Type: builder
  - Resume: true

- **Validator (embedding-invalidation)**
  - Name: embedding-validator
  - Role: Verify implementation correctness, run test suite, check docs
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Update `docs/fields.md` with multi-worker deployment section
  - Agent Type: documentarian
  - Resume: true

### Step by Step Tasks

#### 1. Implement cross-process cache invalidation
- **Task ID**: build-embedding-invalidation
- **Depends On**: none
- **Validates**: `tests/test_embedding_field.py`, `tests/test_embedding_field_gc.py`, `tests/test_semantic_search.py`
- **Informed By**: spike-1 (on_save/on_delete are the only call sites), spike-2 (mtime in cache entry is safe), spike-3 (use raw pubsub.run_in_thread, not Subscriber ABC)
- **Assigned To**: embedding-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `_INVALIDATION_MODE` module-level constant reading `POPOTO_EMBEDDING_INVALIDATION` env var (default `"pubsub"`)
- Add `_listener_threads: dict` module-level dict for tracking per-class daemon threads
- Implement `_publish_invalidation(model_class_name)`: publishes to `popoto:embedding:invalidate:{name}` via `POPOTO_REDIS_DB.publish()`, wrapped in try/except with WARNING log
- Implement `_start_invalidation_listener(model_class_name)`: lazy-start daemon thread via `pubsub.run_in_thread(sleep_time=0.1, daemon=True, exception_handler=...)` that calls `invalidate_cache()` on receipt; guarded by idempotency check and try/except
- Call `_publish_invalidation()` in `on_save()` after `invalidate_cache()` (line ~374)
- Call `_publish_invalidation()` in `on_delete()` after `invalidate_cache()` (line ~406)
- In `load_embeddings()`: call `_start_invalidation_listener(model_name)` before the cache check
- Extend `_embedding_cache` entries to include `index_mtime` key (set from `os.stat(_index_path()).st_mtime` after loading from disk)
- Add version-counter staleness check at top of `load_embeddings()` cache-hit path (mtime pre-check + `_version` confirm; only active when `_INVALIDATION_MODE == "mtime"`)
- Extend `_write_index()` to bump a monotonic integer `_version` key on every write
- Add module-level `stop_invalidation_listeners()` that stops every `PubSubWorkerThread` in `_listener_threads` and clears the registry (for test teardown + graceful shutdown)
- Ensure `exception_handler` removes the model entry from `_listener_threads` so the next `load_embeddings()` lazily respawns the listener (self-healing reconnect — redis-py STOPS the worker on connection error)
- Update `EmbeddingField` class docstring with staleness window table and env var docs
- Update module-level `_embedding_cache` comment

#### 1b. Add cross-process invalidation tests
- **Task ID**: test-embedding-invalidation
- **Depends On**: build-embedding-invalidation
- **Assigned To**: embedding-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_embedding_invalidation.py`
- **Two-cache simulation test (pubsub mode):** Populate a model, call `load_embeddings()` to warm `_embedding_cache`. Simulate a peer write by writing a new `.npy` + `_index.json` entry directly AND publishing to `popoto:embedding:invalidate:{Name}` via `POPOTO_REDIS_DB.publish()`. Assert that after a bounded wait (poll up to ~1 s) `_embedding_cache` no longer contains the stale entry, and the next `load_embeddings()` returns the new matrix shape. This exercises the real subscriber thread against DB 15.
- **mtime/version mode test:** Set `POPOTO_EMBEDDING_INVALIDATION=mtime`, warm the cache, bump `_version` in `_index.json` via `_write_index()`, and assert the next `load_embeddings()` reloads (entry dropped). Cover the same-tick case by forcing `_version` to differ while leaving mtime potentially equal — assert the version comparison still triggers reload.
- **none mode test:** Set `POPOTO_EMBEDDING_INVALIDATION=none`; assert no listener thread is started (`_listener_threads` empty after `load_embeddings()`) and `_publish_invalidation()` issues no PUBLISH (monkeypatch/count).
- **Teardown test:** Assert `stop_invalidation_listeners()` empties `_listener_threads` and the threads report not-alive.
- Register `stop_invalidation_listeners()` in the test isolation teardown (or add an autouse fixture in this file) so listener threads do not leak across the suite.

#### 2. Validate implementation
- **Task ID**: validate-embedding-invalidation
- **Depends On**: build-embedding-invalidation
- **Assigned To**: embedding-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_embedding_field.py tests/test_embedding_field_gc.py tests/test_semantic_search.py tests/test_embedding_invalidation.py -x -q` and confirm all pass
- Confirm the new cross-process invalidation test (1b) actually drives a real subscriber thread and observes cache drop (not just attribute checks)
- Confirm `_listener_threads` threads have `daemon=True` attribute
- Confirm `_publish_invalidation` is called from both `on_save` and `on_delete`
- Confirm `POPOTO_EMBEDDING_INVALIDATION=none` skips publish and listener start
- Confirm `POPOTO_EMBEDDING_INVALIDATION=mtime` skips publish but adds version-counter check
- Confirm `exception_handler` clears the `_listener_threads` entry (self-healing path)
- Confirm `stop_invalidation_listeners()` exists and is wired into test teardown (no thread leak across the suite)
- Confirm no new top-level imports outside the try/except guard

#### 3. Update docs/fields.md
- **Task ID**: document-embedding-invalidation
- **Depends On**: build-embedding-invalidation
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: true
- In `docs/fields.md` under `## EmbeddingField`, add a "Multi-Worker Deployments" subsection documenting: default pub/sub invalidation, `POPOTO_EMBEDDING_INVALIDATION` env var, staleness window table, NFS caveat

#### 4. Final validation
- **Task ID**: validate-all
- **Depends On**: validate-embedding-invalidation, document-embedding-invalidation, test-embedding-invalidation
- **Assigned To**: embedding-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Confirm `docs/fields.md` contains "Multi-Worker Deployments" section
- Confirm `EmbeddingField` docstring references `POPOTO_EMBEDDING_INVALIDATION`
- Report all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Embedding tests pass | `pytest tests/test_embedding_field.py tests/test_embedding_field_gc.py tests/test_semantic_search.py tests/test_embedding_invalidation.py -x -q` | exit code 0 |
| Cross-process invalidation tested | `grep -n "run_in_thread\|publish" tests/test_embedding_invalidation.py` | test drives a real subscriber thread |
| Version counter present | `grep -n "_version" src/popoto/fields/embedding_field.py` | output contains _version |
| Teardown helper present | `grep -n "stop_invalidation_listeners" src/popoto/fields/embedding_field.py` | output > 0 |
| Full test suite passes | `pytest tests/ -x -q` | exit code 0 |
| Invalidation mode env var present | `grep -n "POPOTO_EMBEDDING_INVALIDATION" src/popoto/fields/embedding_field.py` | output contains POPOTO_EMBEDDING_INVALIDATION |
| Listener threads are daemon | `grep -n "daemon=True" src/popoto/fields/embedding_field.py` | output contains daemon=True |
| Publish called from on_save | `grep -n "_publish_invalidation" src/popoto/fields/embedding_field.py` | output > 0 |
| docs/fields.md updated | `grep -n "Multi-Worker" docs/fields.md` | output contains Multi-Worker |

## Critique Results

Critique verdict: **NEEDS REVISION** (recorded 2026-06-01). Detailed findings were not
persisted by the critique tool; the revision below was driven by a fresh adversarial
re-read of the plan against the live source (`embedding_field.py`, `redis_db.py`,
`pubsub/`, `models/query.py`). Findings addressed:

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| High | Archaeologist | Research claimed `run_in_thread` `exception_handler` keeps the thread alive and retries — false; redis-py STOPS the worker after the handler returns | Research correction + lazy-respawn design | `exception_handler` clears `_listener_threads`; next `load_embeddings()` respawns |
| High | Skeptic | No test actually verified cross-process invalidation; validator only checked `daemon=True` and call-site presence | New task 1b (`test_embedding_invalidation.py`) | Two-cache simulation drives a real subscriber thread against DB 15 |
| Med | Adversary | mtime granularity hazard framed as NFS-only; same-tick writes on HFS+ (and any 1s-resolution FS) can mask a local write too | Version-counter sentinel in `_index.json` | mtime used as cheap pre-check; integer `_version` is authoritative |
| Med | Operator | Daemon listener threads leak across the test suite / no graceful shutdown | `stop_invalidation_listeners()` + teardown wiring | Stops every `PubSubWorkerThread`; wired into isolation teardown |
| Med | User | "Update System" claimed default is byte-for-byte same as before for single-process | Honest cost accounting + `none` mode guidance | Default adds a thread, a connection, and a loopback PUBLISH per write |
| Low | Simplifier | Data Flow said publish sends JSON `{model,action}` but code publishes a bare string — self-contradiction | Standardized on bare `model_class_name` body | Handler is per-channel; body is informational only |
| Low | Operator | `<10 ms` staleness claim ignored the `sleep_time=0.1` poll interval | Window restated as RTT + ≤100 ms poll | Matches `run_in_thread(sleep_time=0.1)` |

