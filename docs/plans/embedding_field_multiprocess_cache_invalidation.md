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

Tracking issue: #403

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

- **CORRECTION (verified against the installed `redis==7.1.1`): `exception_handler` neither keeps the thread alive NOR stops it.** The actual `PubSubWorkerThread.run()` is:
  ```python
  while self._running.is_set():
      try:
          pubsub.get_message(ignore_subscribe_messages=True, timeout=sleep_time)
      except BaseException as e:
          if self.exception_handler is None:
              raise
          self.exception_handler(e, pubsub, self)
  pubsub.close()
  ```
  The handler runs *inside* the `except`; control then returns to the `while self._running.is_set()` check. redis-py does **not** clear `_running` for us. So on a persistent `ConnectionError` the loop immediately re-enters `get_message()`, re-raises, and re-invokes the handler — a **tight busy-loop** that spams WARNING and re-pops the registry every iteration, never terminating. (An earlier draft of this plan claimed redis-py "stops the thread (sets `_running = False`)" after the handler — that is also false; nothing sets it but us.)
  - **Implication for the plan:** the `_exception_handler` MUST call `worker.stop()` (the third positional arg; `stop()` does `self._running.clear()`) **before** popping `_listener_threads`. That actually terminates the run loop (which then runs its trailing `pubsub.close()` and returns the connection to the pool). Only once the thread is dead does the lazy-respawn become real: the next `load_embeddings()` finds no registry entry and spawns a fresh listener (self-healing reconnect). Without `worker.stop()` the thread busy-loops forever AND the registry-pop causes the next `load_embeddings()` to spawn a *second* live listener → thread + connection leak under any sustained outage.
  - **Version assumption:** this is internal redis-py behavior (`PubSubWorkerThread`), not a public API contract. Confirmed against `redis==7.1.1` (the pinned version in this env). If redis-py changes the run-loop semantics in a future major, re-verify; pin documented in No-Gos.

- **`socket_timeout` interaction** (verified against `redis_db.py:113,122`): `POPOTO_REDIS_DB` is constructed with `socket_timeout=5`. `run_in_thread(sleep_time=0.1)` uses the **non-blocking** `get_message(timeout=...)` path with a short poll, so the 5s socket timeout does not fire on an idle subscription the way a blocking `listen()` would. However, a dropped TCP connection still surfaces as a `ConnectionError`/`TimeoutError` inside the worker loop — which (per the correction above) invokes our handler, which calls `worker.stop()` to terminate the thread. The lazy-respawn-on-next-load behavior then handles reconnect; no separate keepalive is needed.

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
3. **Publish**: After `invalidate_cache()`, the hook publishes the message body `f"{model_class_name}:{_WORKER_ID}"` to the per-class Valkey channel `popoto:embedding:invalidate:ClassName`, where `_WORKER_ID` is a per-process UUID generated at import. The channel name fully identifies the target model; the **worker-id suffix** lets a receiving worker recognize and skip its own loopback message (see item 6). (Body is a bare `name:worker_id` string, NOT a JSON object — the earlier draft's `{"model":...,"action":...}` was dropped to keep publish/handler trivially consistent. A UUID is used rather than `os.getpid()` because pids collide across containers/pods.)
4. **Background subscriber thread** (each worker): On first `load_embeddings()` call for a model class, a daemon thread is started (if not already running) via `pubsub.run_in_thread(daemon=True, exception_handler=...)`. The thread subscribes to `popoto:embedding:invalidate:ClassName`. On any connection error the handler calls `worker.stop()` to terminate the thread (redis-py does NOT auto-stop it — see Research correction) and pops the registry entry; the next `load_embeddings()` respawns a fresh listener (self-healing reconnect).
5. **Invalidation receipt**: When workers B, C receive the message, the handler parses the worker-id suffix; if it is *not* this worker's own `_WORKER_ID`, it calls `invalidate_cache(model_class_name)`. Next `load_embeddings()` (driven by `query.semantic_search()` at `query.py:719` / `query.py:1044`) on those workers reloads from disk.
6. **Output**: All workers serve fresh matrix within one Valkey round-trip plus the worker's `sleep_time` poll interval (≤ 100 ms + RTT). The publishing worker also receives its own message (loopback) but the handler **skips it** because the message's worker-id matches the local `_WORKER_ID` — avoiding a redundant second disk reload on a write-then-immediately-search hot path (the agent-memory workload).

**Path B — File-mtime / version sentinel (secondary, batch/offline/NFS):**

1. **Entry**: `on_save()` / `on_delete()` completes `_write_index()`. The `_index.json` mtime is implicitly updated by the atomic rename.
2. **Read path**: Before returning a cached matrix, `load_embeddings()` calls `os.stat(_index_path(model_name)).st_mtime` as a cheap pre-check. If the mtime differs from the cached `index_mtime`, it confirms via the authoritative integer `_version` in `_index.json` (granularity-proof against same-tick writes); if the version also differs, the cache entry is dropped and reloaded.
3. **Reload**: New matrix is loaded from disk, normalized, and re-cached with the new mtime.
4. **Output**: Workers detect staleness on the next `semantic_search()` call after a write, with no Valkey connection required.

## Architectural Impact

- **New dependencies**: None. Uses existing redis-py `pubsub()` API and `POPOTO_REDIS_DB` connection. numpy already optional.
- **Interface changes**: None. `semantic_search()`, `load_embeddings()`, `on_save()`, `on_delete()` signatures unchanged. New `_start_invalidation_listener()` is internal.
- **Coupling**: `embedding_field.py` gains a soft dependency on the Valkey pub/sub feature. If `POPOTO_REDIS_DB.pubsub()` throws (e.g., ACL restriction, Valkey down, pool exhaustion), the listener fails to start and a WARNING is logged. Crucially, this does NOT silently re-expose the stale-cache bug: when no live listener exists for a model in `pubsub` mode, `load_embeddings()` falls back to the on-disk `_version` staleness check (the backstop in the Solution), so cross-process invalidation degrades to mtime-style correctness rather than to the original defect.
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
import uuid

_INVALIDATION_MODE = os.environ.get("POPOTO_EMBEDDING_INVALIDATION", "pubsub")
# Process-global unique identity. os.getpid() is NOT unique across containers/
# pods (the issue's multi-container target) — two remote workers routinely share
# a pid, which would cause a real peer write to be silently skipped as "our own"
# loopback, re-exposing the stale-cache bug. A per-process UUID is collision-free.
_WORKER_ID = uuid.uuid4().hex

def _publish_invalidation(model_class_name: str) -> None:
    if _INVALIDATION_MODE != "pubsub":
        return
    try:
        from ..redis_db import POPOTO_REDIS_DB
        channel = f"popoto:embedding:invalidate:{model_class_name}"
        # body carries our worker id so receivers skip only their own loopback
        POPOTO_REDIS_DB.publish(channel, f"{model_class_name}:{_WORKER_ID}")
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

        def _on_message(msg):
            # body is "ClassName:worker_id"; skip our own loopback to avoid a
            # redundant reload on a write-then-search hot path. The id is a
            # per-process UUID (NOT pid) so it never collides across containers.
            data = msg.get("data")
            if isinstance(data, bytes):
                data = data.decode()
            if isinstance(data, str) and data.rsplit(":", 1)[-1] == _WORKER_ID:
                return
            invalidate_cache(model_class_name)

        ps = POPOTO_REDIS_DB.pubsub()
        ps.subscribe(**{channel: _on_message})

        def _exception_handler(ex, ps_obj, worker):
            # redis-py does NOT stop the worker after this handler returns —
            # the run loop re-enters get_message() and busy-loops. We MUST
            # call worker.stop() (clears _running) to terminate it. THEN drop
            # the registry entry so the next load_embeddings() lazily respawns
            # a fresh listener (self-healing reconnect). Order matters: stop
            # before pop, else a respawn races a still-running thread.
            logger.warning(f"EmbeddingField invalidation listener error: {ex}")
            try:
                worker.stop()  # _running.clear(); run loop exits + closes pubsub
            except Exception:
                pass
            _listener_threads.pop(model_class_name, None)

        thread = ps.run_in_thread(
            sleep_time=0.1, daemon=True, exception_handler=_exception_handler
        )
        _listener_threads[model_class_name] = thread
    except Exception as e:
        logger.warning(f"EmbeddingField: failed to start invalidation listener: {e}")
        _listener_threads.pop(model_class_name, None)
```

The thread is a daemon thread — it will not prevent process shutdown. redis-py
does **not** terminate the worker on a connection error (the run loop re-enters
`get_message()` and would busy-loop), so the `_exception_handler` explicitly calls
`worker.stop()` to clear `_running` and end the loop, then removes the registry
entry. Reconnect is then handled by the lazy-respawn pattern: the next
`load_embeddings()` call finds no entry and starts a fresh listener. There is a
bounded staleness window equal to the time between the connection drop and the next
`semantic_search()` call on that worker — documented below.

**Teardown for tests and graceful shutdown:** add a module-level
`stop_invalidation_listeners()` that iterates `_listener_threads`, calls
`thread.stop()` on each `PubSubWorkerThread`, and clears the registry.

Wire teardown via an **autouse fixture in `tests/conftest.py`** (function-scoped,
yields then calls `stop_invalidation_listeners()`) — NOT in
`src/popoto/pytest_plugin.py`. The plugin ships inside the installed package, so
editing it would change every downstream consumer's test runs; `conftest.py` is
test-only and covers the whole suite. This matters because in the default `pubsub`
mode, *any* existing embedding test that reaches `load_embeddings()` (e.g.
`test_embedding_field.py`, `test_semantic_search.py`) now spawns a daemon listener
thread — so the conftest fixture is **mandatory, not optional**, to keep those
pre-existing suites from leaking threads against the flushed DB 15.

**`tests/conftest.py` is currently comment-only** (no `import pytest`, no
fixtures — it just documents the `popoto.pytest_plugin` entry-point fixtures). The
teardown fixture is therefore **net-new**, not an edit to an existing fixture: the
task must add `import pytest` plus a module-level
`@pytest.fixture(autouse=True)` that `yield`s then calls
`stop_invalidation_listeners()`. Omitting `import pytest` turns the decorator into
a `NameError` that pytest surfaces as a *collection* error — so verify the fixture
is actually collected (e.g. a test asserting `_listener_threads` is empty at
start), not merely that the file contains the string.

### Path B — version-counter sentinel (mtime as cheap pre-check)

`_write_index()` is extended to bump a monotonic integer `_version` key — but
**only on genuine corpus mutations**, NOT on every call. Because spike-2 confirmed
`_read_index()` returns the dict as-is and `load_embeddings()` only iterates
`.npy`-suffixed keys, a top-level `_version` integer is schema-safe (old readers
ignore it).

**Critical: `_write_index()` is also called on the read path.** It is invoked from
five sites (`embedding_field.py:342`, `:353`, `:404`, `:485`, `:630`); the call at
**`:485` is the legacy-fallback READ path** inside `load_embeddings()` (it
reconciles an orphan hex-named `.npy` into the index during a pure read). If
`_version` bumped on every `_write_index`, a read on worker A would bump the disk
version, which workers B/C would see as a phantom "peer write" and reload — and
worker A could self-invalidate on its next search. On a corpus with persistent
un-reconcilable orphans this becomes a reload storm. The bump must therefore be
gated to mutation calls only:

```python
def _write_index(model_class_name, index_dict, bump_version: bool = True):
    if bump_version:
        index_dict["_version"] = index_dict.get("_version", 0) + 1
    # ... existing atomic write (unchanged)
```

The save/delete/GC sites (`:342`, `:353`, `:404`, `:630`) call `_write_index(...)`
as before (default `bump_version=True`). The **read-path call at `:485` MUST pass
`bump_version=False`** so a read never advances the version counter.

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

At the top of `load_embeddings()`, before returning cached values, run the
version-counter staleness check when either (a) we are in `mtime` mode, or (b) we
are in `pubsub` mode but no live listener thread exists for this model
(`model_name not in _listener_threads`). Case (b) is the **silent-degradation
backstop**: if the pub/sub listener failed to start (Valkey unreachable, ACL
restriction, pool exhaustion), `pubsub` mode would otherwise re-expose the exact
stale-cache bug this plan fixes — with only a transient-looking WARNING. The
backstop falls back to the on-disk `_version` check so the cache still invalidates:

```python
def _should_version_check(model_name):
    if _INVALIDATION_MODE == "mtime":
        return True
    # pubsub backstop: listener never came up → fall back to disk version check
    if _INVALIDATION_MODE == "pubsub" and model_name not in _listener_threads:
        return True
    return False

if model_name in _embedding_cache and _should_version_check(model_name):
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

Because the backstop can fire in `pubsub` mode, the cache entry must carry
`index_mtime` and `index_version` in **all modes except `none`** (not just
`mtime`) — they are populated unconditionally at load time (cost: one `os.stat`
already done + one cheap dict read). In `none` mode the check never runs, so the
fields are harmless if present. `_publish_invalidation()` remains a no-op in
`mtime` mode and the listener is not started — `mtime` mode does no pub/sub and
needs no live Valkey connection.

### Staleness window

| Mode | Staleness window | Notes |
|------|-----------------|-------|
| `pubsub` | Valkey RTT + worker poll interval (`sleep_time=0.1`), i.e. ≤ ~100 ms in practice | Requires live Valkey connection. After a connection drop, the window extends to the next `semantic_search()` call on that worker (lazy respawn). If the listener never starts (Valkey unreachable), the on-disk `_version` backstop takes over → degrades to the `mtime` window, never to the stale-cache bug. |
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
- No changes to the `.npy` / `_index.json` file format on disk for the `mtime` path (a `_version` integer is added to `_index.json`, which spike-2 confirmed is schema-safe — old readers ignore it; mtime itself is read from the filesystem, not written into the file).
- **redis-py version assumption (documented, not pinned in `pyproject.toml`):** the self-healing reconnect relies on `PubSubWorkerThread` run-loop semantics in `redis==7.1.1` (the version in this env) — specifically that the handler must call `worker.stop()` to terminate the loop. This is internal redis-py behavior, not a public contract. The `worker.stop()` call is defensive (it is a documented public method on `PubSubWorkerThread` across redis-py versions), so it remains correct even if a future redis-py changes whether the loop auto-stops. No new version cap is added; the assumption is recorded here and in the Research section so a future redis-py bump triggers a re-verify.

## Rabbit Holes

- **Cross-host NFS / HFS+ mtime granularity**: HFS+ (older macOS) and some NFS mounts have 1-second mtime resolution, so two writes within one tick could mask a change. **This is why `mtime` mode (and the `pubsub` backstop) compares the authoritative integer `_version` counter in `_index.json`, not the raw mtime** — mtime is only a cheap "did anything change at all" pre-check. The version counter eliminates the same-tick hazard on every filesystem. (Raw-mtime-only comparison was the cycle-2 concern; resolved.)
- **Thread safety of `_embedding_cache`**: The dict is module-level and GIL-protected on CPython. The lambda handler in the daemon thread calls `invalidate_cache()` which does `_embedding_cache.pop(model_name, None)` — a GIL-atomic operation. No lock needed.
- **Gunicorn pre-fork model**: In pre-fork, workers fork after the parent imports the module. `_listener_threads` starts empty in every worker (fork creates a new process; threads are not inherited). The lazy start in `load_embeddings()` handles this correctly — each worker starts its own listener thread on first use.
- **Connection pool exhaustion**: Each worker-per-model-class creates one additional redis-py connection for pub/sub. In a 4-worker × 3-model-class deployment, that's 12 extra connections. Well within default pool limits (typically 50–100). Not a concern for normal deployments.

## Update System

No update system changes required. This is a library-internal change; the env var `POPOTO_EMBEDDING_INVALIDATION` defaults to `pubsub` with no user action required.

**Honest accounting of the default's cost for single-process apps** (the first draft understated this): with the default `pubsub` mode, a single-process deployment will now (a) start one daemon `PubSubWorkerThread` per model class on first search, (b) hold one extra Valkey connection per such class, and (c) issue one `PUBLISH` per save/delete that loops back to itself — but the handler recognizes its own per-process `_WORKER_ID` in the message body and skips it, so no redundant reload occurs (still a real Valkey round-trip for the PUBLISH). This is NOT byte-for-byte "same as before." Apps that genuinely run single-process and want zero overhead should set `POPOTO_EMBEDDING_INVALIDATION=none`. The functional *results* are identical to pre-fix in all three modes for a single process; only the resource profile differs. This trade-off is documented in `docs/fields.md` so operators can choose `none` deliberately.

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
- [ ] Listener threads self-heal after a connection drop: the `exception_handler` calls `worker.stop()` (terminating the run loop — redis-py does NOT auto-stop it) then clears the registry so the next load respawns; threads are stopped by `stop_invalidation_listeners()` — no thread leak or busy-loop across the test suite
- [ ] In `pubsub` mode, if the listener fails to start (no entry in `_listener_threads`), `load_embeddings()` falls back to the on-disk `_version` staleness check — a Valkey outage degrades to mtime-style correctness, never to the original stale-cache bug (verified by test)
- [ ] The publishing worker skips its own loopback message (per-process `_WORKER_ID` match, not pid) — a write-then-search on the same worker does not trigger a redundant second disk reload, and a remote peer write is never mistaken for loopback (verified by test)
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
- Add `_WORKER_ID = uuid.uuid4().hex` module-level constant (per-process identity for loopback skip; NOT `os.getpid()`, which collides across containers/pods)
- Add `_listener_threads: dict` module-level dict for tracking per-class daemon threads
- Implement `_publish_invalidation(model_class_name)`: publishes body `f"{model_class_name}:{_WORKER_ID}"` to `popoto:embedding:invalidate:{name}` via `POPOTO_REDIS_DB.publish()`, wrapped in try/except with WARNING log
- Implement `_start_invalidation_listener(model_class_name)`: lazy-start daemon thread via `pubsub.run_in_thread(sleep_time=0.1, daemon=True, exception_handler=...)`; the message handler decodes the `name:worker_id` body and **skips messages whose worker-id matches the local `_WORKER_ID`** (loopback self-skip) before calling `invalidate_cache()`; guarded by idempotency check and try/except
- In the `exception_handler`: **call `worker.stop()` FIRST** (redis-py does NOT auto-stop the run loop — without this it busy-loops), THEN `_listener_threads.pop(model_class_name, None)` so the next `load_embeddings()` lazily respawns a fresh listener (self-healing reconnect)
- Call `_publish_invalidation()` in `on_save()` after `invalidate_cache()` (line ~374)
- Call `_publish_invalidation()` in `on_delete()` after `invalidate_cache()` (line ~406)
- In `load_embeddings()`: call `_start_invalidation_listener(model_name)` before the cache check
- Extend `_embedding_cache` entries to include `index_mtime` AND `index_version` keys (set from `os.stat(_index_path()).st_mtime` and `_read_index().get("_version", 0)` after loading from disk) — populated in all modes except `none` so the pubsub backstop can use them
- Add version-counter staleness check at top of `load_embeddings()` cache-hit path (mtime pre-check + `_version` confirm) gated by `_should_version_check()`: active when `_INVALIDATION_MODE == "mtime"` OR (`pubsub` AND `model_name not in _listener_threads`) — the silent-degradation backstop
- Extend `_write_index()` with a `bump_version: bool = True` param that increments a monotonic integer `_version` key. Mutation sites (`:342`, `:353`, `:404`, `:630`) use the default; the **read-path call at `:485` MUST pass `bump_version=False`** so a pure read never advances the version counter (prevents a reload storm on corpora with legacy orphan files)
- Add module-level `stop_invalidation_listeners()` that stops every `PubSubWorkerThread` in `_listener_threads` and clears the registry (for test teardown + graceful shutdown)
- Update `EmbeddingField` class docstring with staleness window table and env var docs
- Update module-level `_embedding_cache` comment at `embedding_field.py:48`: drop the phantom `"dirty": False` field (never written) and document the real `index_mtime` / `index_version` keys (NIT)
- Opportunistic cleanup: delete the unreachable `return 0` after `return removed_count` at `embedding_field.py:703–704` while editing the module (NIT)

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
- **pubsub backstop test:** Set `pubsub` mode but simulate a failed listener (e.g. ensure `model_name not in _listener_threads`, or monkeypatch `_start_invalidation_listener` to no-op); warm the cache, bump `_version` on disk, and assert the next `load_embeddings()` still reloads via the version-check backstop (proves a Valkey-down deployment degrades to mtime correctness, not the stale-cache bug).
- **loopback self-skip test:** In `pubsub` mode, publish a message whose worker-id suffix equals the local `_WORKER_ID` and assert the cache entry is NOT dropped; publish one with a different worker-id and assert it IS dropped. (Guards against the pid-collision regression — the skip token must be the per-process UUID, not pid.)
- **read-path no-bump test:** Confirm that a `load_embeddings()` call which triggers the legacy-fallback `_write_index(..., bump_version=False)` at `:485` does NOT advance `_version` (so reads never look like peer writes).
- **Teardown test:** Assert `stop_invalidation_listeners()` empties `_listener_threads` and the threads report not-alive.
- Add the `stop_invalidation_listeners()` teardown as an **autouse fixture in `tests/conftest.py`** (function-scoped, yield → stop), NOT in `src/popoto/pytest_plugin.py` (which ships to consumers). The file is comment-only today, so this is net-new: add `import pytest` + a module-level `@pytest.fixture(autouse=True)`. This covers the whole suite — mandatory because default `pubsub` mode means existing embedding tests also spawn listener threads. Verify the fixture is actually collected (assert `_listener_threads` empty at test start), not just present as text.

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
- Confirm `exception_handler` calls `worker.stop()` BEFORE popping `_listener_threads` (self-healing path; no busy-loop)
- Confirm the message handler skips loopback messages whose worker-id matches the local `_WORKER_ID` (per-process UUID, NOT pid)
- Confirm `_version` is bumped only on mutation calls — the read-path `_write_index` at `:485` passes `bump_version=False`
- Confirm `tests/conftest.py` adds `import pytest` and a collected autouse fixture (not just the string)
- Confirm the pubsub backstop fires: when `model_name not in _listener_threads` in `pubsub` mode, `load_embeddings()` runs the `_version` staleness check
- Confirm `stop_invalidation_listeners()` exists and is wired into `tests/conftest.py` (NOT `pytest_plugin.py`) — no thread leak across the suite
- Confirm the phantom `"dirty"` field is gone from the `_embedding_cache` comment and the dead `return 0` at the end of `sweep_stale_tempfiles` is removed
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
| Self-heal calls worker.stop | `grep -n "worker.stop\|\.stop()" src/popoto/fields/embedding_field.py` | exception_handler stops the worker |
| Teardown wired in conftest (not plugin) | `grep -n "stop_invalidation_listeners" tests/conftest.py; grep -c "stop_invalidation_listeners" src/popoto/pytest_plugin.py` | present in conftest.py, absent (0) in pytest_plugin.py |
| Loopback skip uses worker UUID (not pid) | `grep -n "_WORKER_ID\|uuid" src/popoto/fields/embedding_field.py` | publish + handler reference `_WORKER_ID`; no `getpid` used for skip |
| Read-path skips version bump | `grep -n "bump_version" src/popoto/fields/embedding_field.py` | `_write_index` has `bump_version` param; `:485` passes `False` |
| conftest fixture collectable | `grep -n "import pytest\|autouse" tests/conftest.py` | `import pytest` + `@pytest.fixture(autouse=True)` present |
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
| High | Archaeologist | Research claimed `run_in_thread` `exception_handler` keeps the thread alive and retries — false | Research correction + lazy-respawn design | ⚠️ **Superseded by cycle 2:** this cycle-1 fix asserted redis-py auto-stops the worker, which is ALSO wrong. See the cycle-2 Blocker row — the handler must call `worker.stop()` itself. |
| High | Skeptic | No test actually verified cross-process invalidation; validator only checked `daemon=True` and call-site presence | New task 1b (`test_embedding_invalidation.py`) | Two-cache simulation drives a real subscriber thread against DB 15 |
| Med | Adversary | mtime granularity hazard framed as NFS-only; same-tick writes on HFS+ (and any 1s-resolution FS) can mask a local write too | Version-counter sentinel in `_index.json` | mtime used as cheap pre-check; integer `_version` is authoritative |
| Med | Operator | Daemon listener threads leak across the test suite / no graceful shutdown | `stop_invalidation_listeners()` + teardown wiring | Stops every `PubSubWorkerThread`; wired into isolation teardown |
| Med | User | "Update System" claimed default is byte-for-byte same as before for single-process | Honest cost accounting + `none` mode guidance | Default adds a thread, a connection, and a loopback PUBLISH per write |
| Low | Simplifier | Data Flow said publish sends JSON `{model,action}` but code publishes a bare string — self-contradiction | Standardized on bare `model_class_name` body | Handler is per-channel; body is informational only |
| Low | Operator | `<10 ms` staleness claim ignored the `sleep_time=0.1` poll interval | Window restated as RTT + ≤100 ms poll | Matches `run_in_thread(sleep_time=0.1)` |

### Critique cycle 2 (recorded 2026-06-02)

Re-critique of the revised plan. Verdict **NEEDS REVISION** — the cycle-1 revision's central correction was itself factually wrong against the installed `redis==7.1.1`. Findings verified directly against `redis/client.py` `PubSubWorkerThread.run()`/`.stop()` and the live `embedding_field.py`. All addressed:

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| Blocker | Archaeologist / Adversary | `exception_handler` does NOT stop the worker (cycle-1 claimed it auto-stops). The run loop re-enters `get_message()` after the handler returns → busy-loop; registry-pop then spawns a *second* live listener → thread/connection leak | Handler now calls `worker.stop()` (clears `_running`) BEFORE popping the registry | Verified against `redis==7.1.1` run loop; `stop()` is a public `PubSubWorkerThread` method. Research, Data Flow item 4, Solution code, narrative, and Success Criterion all corrected; version assumption documented in No-Gos |
| Concern | Operator / Skeptic | Default `pubsub` mode silently degrades to the original stale-cache bug if the listener fails to start (Valkey down / ACL / pool exhaustion) — only a transient-looking WARNING | On-disk `_version` backstop: `_should_version_check()` fires the staleness check in `pubsub` mode when `model_name not in _listener_threads` | `index_mtime`/`index_version` now populated in all modes except `none`; documented in Architectural Impact + staleness table; covered by a backstop test |
| Concern | Operator / Adversary | Teardown wiring under-specified and pointed at the shipped `pytest_plugin.py`; other embedding suites also spawn listener threads | Autouse fixture in `tests/conftest.py` (NOT `pytest_plugin.py`) | Mandatory because default `pubsub` mode spawns threads in `test_embedding_field.py`/`test_semantic_search.py` too |
| Concern | Adversary / Simplifier | Loopback PUBLISH re-invalidates the just-warmed cache on the writing worker → redundant reload on write-then-search (the agent-memory hot path) | Publish body carries `os.getpid()`; handler skips messages whose pid matches its own | Keeps the bare-string design (now `name:pid`); no JSON reintroduced |
| Nit | Simplifier | Dead `return 0` after `return removed_count` in `sweep_stale_tempfiles` (`embedding_field.py:703–704`) | Opportunistic deletion added to task 1 | Pre-existing; builder touches this file anyway |
| Nit | Consistency Auditor | `_embedding_cache` comment (`embedding_field.py:48`) documents a `"dirty": False` field that is never written | task 1 now drops `dirty`, documents `index_mtime`/`index_version` | Cache writes store only `matrix`/`keys` today |

### Critique cycle 3 (recorded 2026-06-02)

Re-critique of the cycle-2 revision. Verdict **READY TO BUILD (with concerns)** — 0 blockers. The three concerns below were folded into the plan before build (the nits were already addressed in cycle 2). All verified against HEAD source.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| Concern | Adversary / Skeptic | `_version` bumped on every `_write_index`, but `:485` is the legacy-fallback READ path → a read bumps the version, peers see a phantom write and reload (reload storm on orphan corpora) | `_write_index(..., bump_version=True)`; read-path `:485` passes `False` | Bump gated to mutation sites `:342`/`:353`/`:404`/`:630` only |
| Concern | Adversary / Operator | Loopback self-skip used `os.getpid()` — pids collide across containers/pods (the issue's explicit target), so a remote peer write sharing the local pid is silently skipped → stale-cache bug returns | Per-process `_WORKER_ID = uuid.uuid4().hex` skip token | Correctness fix, not just an optimization; pid was insufficient. (This was a flaw introduced by the cycle-2 pid suffix.) |
| Concern | Operator | `tests/conftest.py` is comment-only today; the autouse teardown fixture is net-new and needs `import pytest` or it fails collection | Plan now states conftest is comment-only; task adds `import pytest` + collected autouse fixture; verify collection | Autouse fixture is the only form that runs unnamed; a missing import is a collection error, not a silent skip |
| Nit | Consistency Auditor / Simplifier | Phantom `"dirty"` field + dead `return 0` | Already in cycle-2 task 1 | Confirmed still accurate |

**Structural note (cycle 3):** the critique flagged the absence of a literal `## Test Impact` header. Test coverage is fully specified across Tasks 1b/2/4 and the Verification table, so this is cosmetic; not adding a redundant header.

