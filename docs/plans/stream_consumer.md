---
status: Shipped
type: feature
appetite: Medium
owner: Valor
created: 2026-03-19
tracking: https://github.com/tomcounsell/popoto/issues/229
last_comment_id:
---

# StreamConsumer — Background Processing Framework for Redis Streams

## Problem

Popoto's EventStreamMixin (shipped in PR #220) writes mutation entries to Redis Streams on every save/delete, but there is no framework for consuming those entries. Background processing — pattern detection, knowledge crystallization, dead-letter handling — requires a consumer group framework that handles the Redis Streams protocol (XREADGROUP, XACK, XCLAIM) so application developers don't have to.

**Current behavior:**
Stream entries accumulate in Redis with no built-in way to process them. Application developers must write their own XREADGROUP/XACK/XCLAIM boilerplate, handle consumer group creation, dead-letter queuing, and graceful shutdown manually.

**Desired outcome:**
A `StreamConsumer` class that handles the Redis Streams consumer group lifecycle — group creation, batch reading, acknowledgment, dead-letter handling, and pending entry recovery — while delegating actual processing logic to an application-provided handler function. This is Step 10 of the Popoto Memory Roadmap.

## Prior Art

- **PR #220**: Add EventStreamMixin — append-only mutation log via Redis Streams. Shipped and merged. This is the producer side that StreamConsumer consumes. Established the stream key patterns (`stream:{name}`, `stream:{name}:{partition}`) and entry format (model, pk, op, ts, changed_fields, metadata).

No prior issues or PRs found related to StreamConsumer or consumer group processing.

## Data Flow

1. **Entry point**: Application code creates a `StreamConsumer` with a stream key, group name, consumer name, and handler function
2. **Group creation**: `XGROUP CREATE stream_key group_name 0 MKSTREAM` — idempotent, handles "group already exists" gracefully
3. **Batch reading**: `XREADGROUP GROUP group_name consumer_name COUNT batch_size BLOCK block_ms STREAMS stream_key >` — reads new entries
4. **Handler invocation**: Application-provided `handler(entries)` processes the batch
5. **Acknowledgment**: `XACK stream_key group_name entry_id ...` — marks entries as processed
6. **Failure path**: If handler raises, entries remain pending. After `max_retries` failures (tracked via XPENDING delivery count), entries are XADDed to `dead:{stream_key}` and XACKed from the original stream
7. **Recovery**: `XPENDING` + `XCLAIM` reclaims entries from crashed consumers after `claim_timeout_ms`

## Architectural Impact

- **New dependencies**: None — Redis Streams consumer groups are core Redis 5.0+ commands, already available in Popoto's Redis dependency
- **Interface changes**: New `StreamConsumer` class in `src/popoto/streams/consumer.py`. New `popoto.streams` subpackage. `StreamConsumer` exported from `popoto.__init__`
- **Coupling**: Minimal — StreamConsumer reads streams that EventStreamMixin writes, but has no import dependency on EventStreamMixin. It operates on raw stream keys and entries
- **Data ownership**: StreamConsumer manages consumer group state (pending entries, acknowledgments). Stream data ownership remains with EventStreamMixin
- **Reversibility**: Easy — remove the `streams/` subpackage and the `__init__` export. No changes to existing code

## Appetite

**Size:** Medium

**Team:** Solo dev, PM

**Interactions:**
- PM check-ins: 1 (scope confirmation on async vs sync API)
- Review rounds: 1 (code review)

The async-first design with sync wrapper, dead-letter handling, and XCLAIM recovery add enough complexity to push this beyond Small, but the scope is well-defined by the Redis Streams protocol.

## Prerequisites

No prerequisites — this work uses only core Redis Streams commands already available in Popoto's Redis connection.

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis 5.0+ | `python -c "from src.popoto.redis_db import POPOTO_REDIS_DB; info = POPOTO_REDIS_DB.info('server'); v = info['redis_version']; assert tuple(int(x) for x in v.split('.')[:2]) >= (5, 0), f'Need Redis 5.0+, got {v}'"` | Redis Streams support |

## Solution

### Key Elements

- **StreamConsumer class**: Manages the consumer group lifecycle — creation, reading, acknowledgment, dead-letter, and recovery
- **Async-first with sync wrapper**: Core logic is `async def run()` and `async def process_batch()`, with synchronous wrappers using `asyncio.run()`
- **Dead-letter stream**: Entries exceeding `max_retries` are moved to `dead:{stream_key}` with failure metadata
- **Pending entry recovery**: `XCLAIM` reclaims entries from consumers that have been idle longer than `claim_timeout_ms`

### Flow

**Application creates consumer** → `StreamConsumer(stream_key, group, consumer, handler)` → **run()** starts loop → **XREADGROUP** reads batch → **handler(entries)** processes → **XACK** confirms → **loop continues** → **on failure** → retry tracking via XPENDING delivery count → **max_retries exceeded** → XADD to dead-letter stream + XACK original

### Technical Approach

- Use `redis.asyncio` (aioredis) for the async path, with `POPOTO_REDIS_DB` (sync redis-py) available for the sync wrapper
- Consumer group creation via `XGROUP CREATE ... MKSTREAM` with `BUSYGROUP` error handling (idempotent)
- Batch reading via `XREADGROUP` with `BLOCK` for efficient waiting
- Dead-letter entries include original entry data plus `original_stream`, `original_id`, `failure_count`, `last_error` metadata
- Graceful shutdown via `consumer.stop()` setting a flag checked between batches
- No external dependencies — pure Redis commands

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] Handler exceptions are caught per-batch — failed entries stay pending, not lost
- [ ] XADD to dead-letter stream failure is logged but doesn't crash the consumer loop
- [ ] Redis connection errors during XREADGROUP are caught with backoff retry
- [ ] Consumer group creation errors (non-BUSYGROUP) are raised immediately

### Empty/Invalid Input Handling
- [ ] Empty stream (no entries) — XREADGROUP blocks for `block_ms` then returns empty, loop continues
- [ ] Handler receives empty batch — should not happen (XREADGROUP returns None), but guarded
- [ ] Invalid stream key — Redis error propagated on first XREADGROUP

### Error State Rendering
- [ ] Not applicable — StreamConsumer is a backend framework with no user-visible output
- [ ] Errors are logged via Python logging module (`POPOTO.StreamConsumer` logger)

## Test Impact

No existing tests affected — this is a greenfield feature adding a new `popoto.streams` subpackage. The existing `test_event_stream_mixin.py` tests the producer side and will not be modified.

## Rabbit Holes

- **Implementing compaction logic inside StreamConsumer** — The consumer is a generic framework. Pattern extraction and PolicyCache crystallization (Step 11) are application-layer concerns. Don't build them here.
- **Multi-stream fan-in** — Reading from multiple streams simultaneously is a valid XREADGROUP pattern but adds complexity. Defer to a future enhancement; one consumer per stream is sufficient.
- **Consumer auto-scaling** — Dynamically spawning/killing consumers based on stream depth is infrastructure, not ORM. Out of scope.
- **Exactly-once semantics** — Redis Streams provide at-least-once delivery. Exactly-once requires application-level idempotency, which is the handler's responsibility, not the framework's.

## Risks

### Risk 1: Async Redis client compatibility
**Impact:** If `redis.asyncio` is not available or behaves differently from sync `redis-py`, the async path breaks.
**Mitigation:** Popoto already depends on `redis>=4.0` which includes `redis.asyncio`. Add explicit version check in consumer init. Provide sync-only fallback if async is unavailable.

### Risk 2: Dead-letter stream unbounded growth
**Impact:** If many entries fail processing, the dead-letter stream grows indefinitely.
**Mitigation:** Apply the same `MAXLEN ~` approximate trimming to the dead-letter stream. Default to 10x the source stream's max_length. Document that operators should monitor dead-letter stream depth.

## Race Conditions

### Race 1: Consumer group creation by multiple consumers simultaneously
**Location:** `StreamConsumer._ensure_group()`
**Trigger:** Two consumers start at the same time, both try `XGROUP CREATE`
**Data prerequisite:** Stream may or may not exist yet
**State prerequisite:** Group must exist before XREADGROUP
**Mitigation:** `XGROUP CREATE ... MKSTREAM` is idempotent — if group exists, Redis returns `BUSYGROUP` error which is caught and ignored. Both consumers proceed correctly.

### Race 2: XCLAIM contention between recovery workers
**Location:** `StreamConsumer._claim_pending()`
**Trigger:** Two consumers both try to XCLAIM the same pending entry
**Data prerequisite:** Entry must be in pending state with idle time > claim_timeout
**State prerequisite:** Entry ownership transfers atomically
**Mitigation:** XCLAIM is atomic — exactly one consumer gets the entry. The other gets an empty response. No duplicate processing.

## No-Gos (Out of Scope)

- **Pattern extraction / compaction logic** — That's Step 11 (PolicyCache), built on top of StreamConsumer
- **Multi-stream consumption** — Single stream per consumer instance
- **Consumer orchestration / auto-scaling** — Application-layer concern
- **Backpressure signaling to producers** — EventStreamMixin writes are fire-and-forget by design
- **Stream partitioning logic** — Already handled by EventStreamMixin's `_stream_partition_field`

## Update System

No update system changes required — Popoto is a library (pip package), not a deployed service. Users install via `pip install popoto` and import StreamConsumer.

## Agent Integration

No agent integration required — StreamConsumer is a Popoto ORM primitive, not a tool in the Valor AI system. It will be used by application developers building agent memory systems on top of Popoto.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/agent-memory.md` — add StreamConsumer section with usage examples, configuration options, and dead-letter handling
- [ ] Add StreamConsumer to the primitives overview table in agent-memory.md

### Inline Documentation
- [ ] Comprehensive docstrings on `StreamConsumer` class and all public methods
- [ ] Code comments on XCLAIM recovery logic and dead-letter flow

## Success Criteria

- [ ] `StreamConsumer` class with `async run()` blocking loop and `async process_batch()` single-batch method
- [ ] Sync wrappers: `run_sync()` and `process_batch_sync()`
- [ ] Consumer group creation is idempotent (handles BUSYGROUP)
- [ ] Batch reading via XREADGROUP with configurable batch_size and block_ms
- [ ] XACK after successful handler execution
- [ ] Dead-letter handling: entries failing max_retries times moved to `dead:{stream_key}`
- [ ] XCLAIM recovery for entries pending longer than claim_timeout_ms
- [ ] Graceful shutdown via `consumer.stop()`
- [ ] Synergy test: EventStreamMixin saves → StreamConsumer processes entries end-to-end
- [ ] All tests pass — unit tests for consumer lifecycle, integration tests with live Redis
- [ ] Documentation updated in `docs/features/agent-memory.md`
- [ ] Valkey compatible (no Redis modules, only core commands)
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (stream-consumer)**
  - Name: consumer-builder
  - Role: Implement StreamConsumer class, async/sync API, dead-letter handling, XCLAIM recovery
  - Agent Type: builder
  - Resume: true

- **Builder (tests)**
  - Name: test-builder
  - Role: Implement unit and integration tests for StreamConsumer lifecycle
  - Agent Type: test-writer
  - Resume: true

- **Validator (stream-consumer)**
  - Name: consumer-validator
  - Role: Verify StreamConsumer implementation against acceptance criteria
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Update agent-memory.md with StreamConsumer documentation
  - Agent Type: documentarian
  - Resume: true

### Available Agent Types

See plan template for full list.

## Step by Step Tasks

### 1. Create StreamConsumer class
- **Task ID**: build-consumer
- **Depends On**: none
- **Validates**: tests/test_stream_consumer.py (create)
- **Assigned To**: consumer-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/streams/__init__.py` with StreamConsumer export
- Create `src/popoto/streams/consumer.py` with StreamConsumer class
- Implement `_ensure_group()` — idempotent XGROUP CREATE with MKSTREAM
- Implement `async process_batch()` — XREADGROUP + handler + XACK
- Implement `async run()` — blocking loop calling process_batch()
- Implement `_claim_pending()` — XPENDING + XCLAIM for recovery
- Implement `_dead_letter()` — XADD to dead:{key} + XACK original
- Implement `stop()` — graceful shutdown flag
- Add sync wrappers: `run_sync()`, `process_batch_sync()`
- Export `StreamConsumer` from `src/popoto/__init__.py`

### 2. Write tests
- **Task ID**: build-tests
- **Depends On**: build-consumer
- **Validates**: tests/test_stream_consumer.py
- **Assigned To**: test-builder
- **Agent Type**: test-writer
- **Parallel**: false
- Consumer group creation — verify XGROUP CREATE called, idempotent on retry
- Batch processing — handler receives entries, XACK sent after success
- Dead-letter — entry failing max_retries times appears in dead:{key}
- XCLAIM recovery — pending entries from crashed consumers are reclaimed
- Graceful shutdown — stop() causes run() to exit after current batch
- Synergy: EventStreamMixin model save → StreamConsumer processes the entry
- Empty stream — consumer blocks and returns gracefully
- Handler exception — entries stay pending, consumer loop continues

### 3. Validate implementation
- **Task ID**: validate-consumer
- **Depends On**: build-tests
- **Assigned To**: consumer-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_stream_consumer.py -v`
- Verify all success criteria are met
- Verify no Redis module commands used (Valkey compatible)
- Verify async and sync APIs both work

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-consumer
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Add StreamConsumer section to `docs/features/agent-memory.md`
- Add entry to primitives overview table
- Include usage examples for both async and sync APIs
- Document dead-letter handling and recovery

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: consumer-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify lint: `python -m ruff check .`
- Verify format: `python -m ruff format --check .`
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_stream_consumer.py -v` | exit code 0 |
| All tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check .` | exit code 0 |
| Format clean | `python -m ruff format --check .` | exit code 0 |
| StreamConsumer importable | `python -c "from src.popoto.streams import StreamConsumer; print('OK')"` | output contains OK |
| No Redis modules | `grep -rn 'BF\.\|CF\.\|CMS\.\|TDIGEST\.\|TS\.' src/popoto/streams/` | exit code 1 |

---

## Open Questions (Resolved)

1. **Async Redis client**: Use existing `get_async_redis_db()` from `redis_db.py` for the async path. Use `POPOTO_REDIS_DB` for sync wrappers. No new connection management needed.

2. **Claim timeout default**: **180,000ms (3 minutes)**. Safe for most handler durations, recovers faster than the originally proposed 5 minutes.

3. **`process_batch()` return value**: Return **count** (int). Tests capture entries through the handler callback pattern.
