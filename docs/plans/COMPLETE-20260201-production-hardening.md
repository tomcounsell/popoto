# Plan: Production Hardening - Critical Reliability Fixes + Test Harness

**Status**: ACTIVE
**Created**: 2026-02-01
**Priority**: CRITICAL
**Related Issues**: #65, #63

## Problem Statement

Analysis revealed 5 critical production-readiness gaps in the Redis layer that will cause outages or data integrity issues under load. This plan addresses them simultaneously with test coverage to validate fixes.

### Critical Issues Identified

1. **Race condition in unique_together** (`base.py:530-561`) - Non-atomic HGET→HSET allows duplicate unique values
2. **Zero Redis connection error handling** (`redis_db.py`) - Connection loss crashes application
3. **No pipeline failure recovery** (`base.py:608-669`) - Mid-batch failures have no rollback
4. **Pub/sub message loss scenarios** (`pubsub/*.py`) - Silent message drops, no backpressure handling
5. **Msgpack encoding failures** (`models/encoding.py`) - No error handling for corrupt data or oversized objects

## Strategy: Fix + Validate Pattern

For each critical issue, we implement the fix and the stress test that validates it **in the same PR**. This ensures:
- Fixes are proven to work under load
- No regression in future (test catches it)
- Clear validation criteria for each fix

## Phase 1: Unique Together Race Condition (CRITICAL)

**Issue**: `base.py:543` does non-atomic check-then-set:
```python
existing = REDIS.HGET(index_key, index_hash)  # Check
if existing: raise error
# ... time window where another thread can pass ...
REDIS.HSET(index_key, index_hash, redis_key)  # Set
```

### Fix: Atomic HSETNX

**File**: `src/popoto/models/base.py` lines 530-561

Replace HGET→check→HSET with atomic HSETNX:
```python
# Use HSETNX for atomic set-if-not-exists
result = REDIS.HSETNX(index_key, index_hash, redis_key)
if result == 0:  # Key already exists
    existing = REDIS.HGET(index_key, index_hash)
    if existing != redis_key:
        raise ModelException(f"unique_together constraint violated: {field_names}")
```

**Why this works**: HSETNX is atomic - only one thread succeeds, others get `0` return value.

### Test: Race Condition Validator

**File**: `tests/test_production_hardening.py`

```python
@pytest.mark.slow
def test_unique_together_race_condition():
    """Validate unique_together constraint under concurrent saves with same values."""

    class User(Model):
        username = KeyField(unique=True)
        email = Field()

        class Meta:
            unique_together = [('email',)]

    # Setup: 100 threads all trying to save same email simultaneously
    email = "duplicate@test.com"
    results = []

    def try_save(thread_id):
        try:
            u = User(username=f"user_{thread_id}", email=email)
            u.save()
            return ("success", thread_id)
        except ModelException as e:
            if "unique_together" in str(e):
                return ("rejected", thread_id)
            raise

    # Execute: 100 concurrent saves
    with ThreadPool(100) as pool:
        results = pool.map(try_save, range(100))

    # Verify: Exactly ONE success, 99 rejections
    successes = [r for r in results if r[0] == "success"]
    rejections = [r for r in results if r[0] == "rejected"]

    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}"
    assert len(rejections) == 99, f"Expected 99 rejections, got {len(rejections)}"

    # Verify: Only one record in database
    assert User.query.filter(email=email).count() == 1
```

**Performance threshold**: < 5 seconds for 100 concurrent threads

---

## Phase 2: Redis Connection Failure Recovery

**Issue**: `redis_db.py` has no retry logic, exponential backoff, or circuit breaker. Any Redis downtime = crash.

### Fix: Connection Resilience Layer

**File**: `src/popoto/redis_db.py` - Add retry decorator and connection health check

```python
import time
from functools import wraps
from redis.exceptions import ConnectionError, TimeoutError

# Configuration constants
MAX_RETRIES = 3
INITIAL_BACKOFF = 0.1  # 100ms
MAX_BACKOFF = 2.0      # 2 seconds
CIRCUIT_BREAKER_THRESHOLD = 5  # failures before opening circuit

# Circuit breaker state
_circuit_breaker = {"failures": 0, "last_failure": None, "is_open": False}

def redis_resilient(func):
    """Decorator adding retry logic and circuit breaker to Redis operations."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        # Check circuit breaker
        if _circuit_breaker["is_open"]:
            # Allow one retry attempt after 30 seconds
            if time.time() - _circuit_breaker["last_failure"] > 30:
                _circuit_breaker["is_open"] = False
                _circuit_breaker["failures"] = 0
            else:
                raise ConnectionError("Circuit breaker open - Redis connection unstable")

        backoff = INITIAL_BACKOFF
        last_exception = None

        for attempt in range(MAX_RETRIES):
            try:
                result = func(*args, **kwargs)
                # Success - reset circuit breaker
                _circuit_breaker["failures"] = 0
                return result
            except (ConnectionError, TimeoutError) as e:
                last_exception = e
                _circuit_breaker["failures"] += 1
                _circuit_breaker["last_failure"] = time.time()

                # Open circuit if threshold exceeded
                if _circuit_breaker["failures"] >= CIRCUIT_BREAKER_THRESHOLD:
                    _circuit_breaker["is_open"] = True
                    raise

                # Don't sleep on last attempt
                if attempt < MAX_RETRIES - 1:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF)

        raise last_exception

    return wrapper

# Apply to connection pool get_connection
_original_get_connection = redis.ConnectionPool.get_connection
redis.ConnectionPool.get_connection = redis_resilient(_original_get_connection)
```

**Alternative approach**: If monkey-patching is too invasive, wrap `POPOTO_REDIS_DB` in a resilient proxy class.

### Test: Connection Failure Recovery

**File**: `tests/test_production_hardening.py`

```python
@pytest.mark.slow
def test_redis_connection_recovery():
    """Validate Redis connection retry logic and circuit breaker."""

    class Item(Model):
        key = KeyField()
        value = Field()

    # Test 1: Transient connection error (should retry and succeed)
    with patch('redis.Redis.get') as mock_get:
        # Fail twice, then succeed
        mock_get.side_effect = [
            redis.exceptions.ConnectionError("Connection lost"),
            redis.exceptions.ConnectionError("Connection lost"),
            b"test_value"
        ]

        # Should eventually succeed after retries
        item = Item(key="test", value="data")
        item.save()
        assert Item.query.get(key="test").value == "data"

    # Test 2: Circuit breaker opens after repeated failures
    with patch('redis.Redis.get') as mock_get:
        mock_get.side_effect = redis.exceptions.ConnectionError("Redis down")

        # First 5 failures should retry
        for i in range(5):
            try:
                Item(key=f"fail_{i}", value="x").save()
            except redis.exceptions.ConnectionError:
                pass

        # 6th attempt should fail fast (circuit open)
        start = time.time()
        try:
            Item(key="fail_6", value="x").save()
        except redis.exceptions.ConnectionError as e:
            elapsed = time.time() - start
            assert "Circuit breaker open" in str(e)
            assert elapsed < 0.1  # Should fail fast, not retry
```

**Performance threshold**: < 100ms for circuit breaker fast-fail

---

## Phase 3: Pipeline Failure Recovery

**Issue**: `base.py:608-669` uses pipelines extensively but has no failure handling for mid-batch errors.

### Fix: Pipeline Transaction Wrapper

**File**: `src/popoto/models/base.py` - Add pipeline exception handling

```python
def _execute_pipeline_safely(pipeline, operation_name="pipeline"):
    """Execute Redis pipeline with error handling and partial failure detection."""
    try:
        results = pipeline.execute()

        # Check for partial failures (redis-py returns exceptions in result list)
        failures = [(i, r) for i, r in enumerate(results) if isinstance(r, Exception)]

        if failures:
            logger.error(f"{operation_name} had {len(failures)} failures: {failures}")
            # For critical operations (save, delete), raise exception
            # For reads, return partial results
            if operation_name in ["save", "delete", "index_update"]:
                raise ModelException(f"{operation_name} pipeline failed: {failures[0][1]}")

        return results
    except redis.exceptions.RedisError as e:
        logger.error(f"{operation_name} pipeline failed completely: {e}")
        raise ModelException(f"{operation_name} failed: {e}")
```

Replace all `pipeline.execute()` calls with `_execute_pipeline_safely(pipeline, "operation_name")`.

**Lines to update**: 608, 636, 669 in `base.py`

### Test: Pipeline Mid-Batch Failure

**File**: `tests/test_production_hardening.py`

```python
@pytest.mark.slow
def test_pipeline_mid_batch_failure():
    """Validate pipeline handles mid-batch failures gracefully."""

    class Item(Model):
        key = KeyField()
        data = Field()

    # Create 100 items successfully
    items = [Item(key=f"item_{i}", data=f"data_{i}") for i in range(100)]
    for item in items:
        item.save()

    # Test: Simulate pipeline failure at position 50
    original_execute = redis.client.Pipeline.execute

    def failing_execute(self):
        # Inject failure into pipeline
        results = []
        for i, cmd in enumerate(self.command_stack):
            if i == 50:
                results.append(redis.exceptions.ResponseError("READONLY You can't write"))
            else:
                results.append(None)  # Success
        return results

    with patch.object(redis.client.Pipeline, 'execute', failing_execute):
        # Bulk delete should detect failure
        with pytest.raises(ModelException, match="pipeline failed"):
            Item.query.all().delete()

    # Verify: Some items still exist (partial failure handled)
    remaining = Item.query.count()
    assert remaining > 0, "All items deleted despite pipeline failure"
```

**Performance threshold**: < 1 second to detect and raise pipeline failure

---

## Phase 4: Pub/Sub Reliability

**Issue**: `pubsub/subscriber.py` can silently drop messages on exception, no backpressure handling, subscribers miss messages published before they connect.

### Fix: Message Queue + Replay

**File**: `src/popoto/pubsub/subscriber.py`

```python
import logging
from collections import deque

logger = logging.getLogger("POPOTO.pubsub")

class Subscriber:
    def __init__(self, channel, maxsize=10000):
        self.channel = channel
        self.message_queue = deque(maxlen=maxsize)  # Bounded queue
        self.dropped_messages = 0
        # ... existing init ...

    def __call__(self, message):
        """Handle incoming message with error recovery."""
        try:
            # Decode message
            data = msgpack.unpackb(message['data'], raw=False)

            # Add to queue (drops oldest if full)
            if len(self.message_queue) >= self.message_queue.maxlen:
                self.dropped_messages += 1
                logger.warning(f"Message queue full, dropping oldest message (total dropped: {self.dropped_messages})")

            self.message_queue.append(data)

            # Process message
            self.handle(data)

        except msgpack.exceptions.FormatError as e:
            logger.error(f"Failed to decode message: {e}")
            # Don't crash - log and continue
        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
            # Don't crash - queue for retry if possible
            if hasattr(self, 'dead_letter_queue'):
                self.dead_letter_queue.append((message, str(e)))
```

**Also add**: Dead letter queue for failed message processing, configurable via `PUBSUB_DLQ_SIZE` env var.

### Test: Pub/Sub Message Loss

**File**: `tests/test_production_hardening.py`

```python
@pytest.mark.slow
def test_pubsub_high_throughput():
    """Validate pub/sub handles 1000 msg/sec without loss."""

    received_messages = []

    class TestSubscriber(Subscriber):
        def handle(self, message):
            received_messages.append(message)

    # Setup subscriber
    sub = TestSubscriber(channel="test_channel")
    sub_thread = sub.listen_in_thread()

    # Publish 1000 messages rapidly
    pub = Publisher(channel="test_channel")
    for i in range(1000):
        pub.publish({"id": i, "data": f"message_{i}"})

    # Wait for processing
    time.sleep(2)
    sub_thread.stop()

    # Verify: All messages received (or detect drops)
    received_ids = {msg['id'] for msg in received_messages}
    missing_ids = set(range(1000)) - received_ids

    if missing_ids:
        logger.warning(f"Dropped {len(missing_ids)} messages: {sorted(missing_ids)[:10]}...")

    # Allow up to 1% message loss (10 messages) - pub/sub is best-effort
    assert len(received_messages) >= 990, f"Too many lost messages: {1000 - len(received_messages)}"
```

**Performance threshold**: < 1% message loss at 1000 msg/sec

---

## Phase 5: Msgpack Encoding Safety

**Issue**: `models/encoding.py` has no error handling for corrupt data, oversized objects, or decoder failures.

### Fix: Encoding Error Boundaries

**File**: `src/popoto/models/encoding.py`

```python
import logging

logger = logging.getLogger("POPOTO.encoding")

# Maximum object size (default 10MB, configurable via env var)
MAX_OBJECT_SIZE = int(os.environ.get('POPOTO_MAX_OBJECT_SIZE', 10 * 1024 * 1024))

def encode_popoto_model_obj(obj):
    """Encode object to msgpack with size and error checking."""
    try:
        encoded = msgpack.packb(obj, default=default_encode, use_bin_type=True)

        # Check size
        if len(encoded) > MAX_OBJECT_SIZE:
            raise ValueError(f"Object size {len(encoded)} exceeds max {MAX_OBJECT_SIZE}")

        return encoded
    except Exception as e:
        logger.error(f"Failed to encode object: {e}", exc_info=True)
        raise ModelException(f"Encoding failed: {e}")

def decode_popoto_model_obj(data):
    """Decode msgpack data with error recovery."""
    try:
        return msgpack.unpackb(data, raw=False, max_bin_len=MAX_OBJECT_SIZE)
    except msgpack.exceptions.ExtraData as e:
        logger.error(f"Corrupt msgpack data: extra data after valid object: {e}")
        raise ModelException(f"Corrupt data: {e}")
    except msgpack.exceptions.FormatError as e:
        logger.error(f"Invalid msgpack format: {e}")
        raise ModelException(f"Invalid data format: {e}")
    except Exception as e:
        logger.error(f"Decoding failed: {e}", exc_info=True)
        raise ModelException(f"Decoding failed: {e}")
```

### Test: Encoding Failures

**File**: `tests/test_production_hardening.py`

```python
@pytest.mark.slow
def test_msgpack_encoding_failures():
    """Validate msgpack encoding handles edge cases safely."""

    class Item(Model):
        key = KeyField()
        data = Field()

    # Test 1: Extremely large object (>10MB default limit)
    huge_data = "x" * (11 * 1024 * 1024)  # 11MB string
    item = Item(key="huge", data=huge_data)

    with pytest.raises(ModelException, match="exceeds max"):
        item.save()

    # Test 2: Corrupt msgpack data in Redis
    item = Item(key="corrupt", data="test")
    item.save()

    # Manually corrupt the data in Redis
    redis_key = item.db_key.redis_key
    POPOTO_REDIS_DB.set(redis_key, b'\x93\xff\xff\xff')  # Invalid msgpack

    with pytest.raises(ModelException, match="Invalid data format"):
        Item.query.get(key="corrupt")

    # Test 3: Unencodable type
    class CustomType:
        pass

    item = Item(key="custom", data=CustomType())

    with pytest.raises(ModelException, match="Encoding failed"):
        item.save()
```

**Performance threshold**: < 10ms to detect and raise encoding errors

---

## Implementation Order

1. **Phase 1** (unique_together race) - 2 hours
   - Most critical data integrity issue
   - Simple atomic operation fix
   - Clear test validation

2. **Phase 2** (connection recovery) - 3 hours
   - Prevents production outages
   - Moderate complexity (retry logic)
   - Test requires mocking

3. **Phase 5** (msgpack safety) - 2 hours
   - Prevents data corruption
   - Straightforward error boundaries
   - Easy to test

4. **Phase 3** (pipeline recovery) - 3 hours
   - Important but less critical
   - Requires auditing all pipeline.execute() calls
   - Test needs careful setup

5. **Phase 4** (pub/sub reliability) - 4 hours
   - Lower priority (pub/sub is best-effort by design)
   - Most complex (queue management, backpressure)
   - Test requires threading

**Total estimated time**: 14 hours → 2 days with testing and review

---

## Success Criteria

Each phase considered complete when:
1. ✅ Fix implemented and code reviewed
2. ✅ Stress test written and passing
3. ✅ Performance threshold met
4. ✅ Documentation updated (docstrings, comments)
5. ✅ CI pipeline includes new test

---

## Files Modified

**Fixes**:
- `src/popoto/models/base.py` (Phase 1, 3)
- `src/popoto/redis_db.py` (Phase 2)
- `src/popoto/models/encoding.py` (Phase 5)
- `src/popoto/pubsub/subscriber.py` (Phase 4)
- `src/popoto/pubsub/publisher.py` (Phase 4 - optional)

**Tests**:
- `tests/test_production_hardening.py` (new file, all phases)

**Config**:
- `pyproject.toml` (add pytest markers)
- `.github/workflows/stress-tests.yml` (add hardening tests)

---

## Risk Mitigation

- Each phase is independently deployable
- Tests validate before merging
- Fixes are minimal, targeted changes (not refactors)
- Performance thresholds prevent regressions
- Existing tests continue passing (backward compatible)

---

## Post-Implementation

After all 5 phases complete:
1. Update CLAUDE.md with new best practices
2. Add reliability section to docs
3. Consider making error handling configurable (strict vs permissive mode)
4. Benchmark before/after performance impact
5. Run full stress test suite overnight to validate
