"""Issue #658 — MemoryLifecycle importance score must honour ``partition_by``.

``SortedFieldMixin.on_save`` writes the member to the *partitioned* sorted-set
key. Until #658, ``_get_importance_score`` read the *base, unpartitioned* key
(``score(..., partitioned=False)``), so for an importance field declared with
``partition_by`` the ``ZSCORE`` could never find the member: it returned
``None``, the ``if raw_score is not None`` guard failed, and the function
silently degraded to a direct-attribute read. The sorted-set path was dead for
partitioned models, and it feeds ``_default_should_forget``, so it moved real
retention decisions.

Every test here is written to be **discriminating rather than merely green**:
the in-memory attribute is set to a value the sorted-set path cannot produce,
so a fallback read is detectable from the return value alone. A test that only
asserted "a score came back" would pass off the fallback path with the ZSCORE
path still dead — precisely the defect.

These tests drive ``_get_importance_score`` / ``_default_should_forget``
directly rather than through ``tick()``: ``tick()`` re-hydrates its corpus and
filters, which can make a guard structurally unreachable while its test still
passes.
"""

import time

import pytest

from src import popoto
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.shortcuts import KeyField
from src.popoto.recipes.memory_lifecycle import (
    MemoryLifecycle,
    _default_should_forget,
    _get_importance_score,
)
from src.popoto.redis_db import get_REDIS_DB

# One day back, so the normalized importance is a distinctive 0.5:
#   normalized = max(0.0, 1.0 - elapsed / (2 * 86400))
ONE_DAY = 86400.0
ZSET_NORMALIZED = 0.5

# A value the sorted-set path cannot produce for a score planted ONE_DAY back.
# If this comes out of _get_importance_score, the attribute fallback ran.
FALLBACK_SENTINEL = 0.9


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class PartitionedMemory(popoto.Model):
    """Importance field partitioned by ``agent`` — the defect's shape."""

    key = popoto.AutoKeyField()
    tier = KeyField(type=str, default="episodic")
    agent = KeyField(type=str, default="a1")
    relevance = DecayingSortedField(decay_rate=0.5, partition_by="agent")


class UnpartitionedMemory(popoto.Model):
    """Control: no ``partition_by``, so both key derivations must agree."""

    key = popoto.AutoKeyField()
    tier = KeyField(type=str, default="episodic")
    relevance = DecayingSortedField(decay_rate=0.5)


@pytest.fixture(autouse=True)
def clean_db():
    for model in (PartitionedMemory, UnpartitionedMemory):
        for instance in model.query.all():
            instance.delete()
    yield
    for model in (PartitionedMemory, UnpartitionedMemory):
        for instance in model.query.all():
            instance.delete()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _keys_for(record):
    """Return (partitioned_key, base_key) for the record's importance field."""
    field = type(record)._meta.fields["relevance"]
    return (
        field.get_partitioned_sortedset_db_key(record, "relevance").redis_key,
        field.get_sortedset_db_key(type(record), "relevance").redis_key,
    )


def _plant_stale_score(record):
    """Overwrite the record's sorted-set score with a timestamp ONE_DAY old.

    Written to the key ``on_save`` actually used (the partitioned one), and
    paired with a contradictory in-memory attribute so the two paths are
    distinguishable by their return value.
    """
    partitioned_key, _ = _keys_for(record)
    get_REDIS_DB().zadd(partitioned_key, {record.pk: time.time() - ONE_DAY})
    record.relevance = FALLBACK_SENTINEL


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_partitioned_importance_reads_the_sorted_set():
    """The score comes from the partitioned ZSET, not the attribute."""
    record = PartitionedMemory(tier="episodic", agent="a1")
    record.save()
    _plant_stale_score(record)

    score = _get_importance_score(record, "relevance")

    assert score == pytest.approx(ZSET_NORMALIZED, abs=1e-3), (
        "importance did not come from the partitioned sorted set; "
        f"got {score!r} (the attribute fallback would return "
        f"{FALLBACK_SENTINEL})"
    )
    assert score != pytest.approx(FALLBACK_SENTINEL, abs=1e-3)


def test_partitioned_importance_does_not_read_the_base_key():
    """The ZSCORE names the partitioned key, and the base key is empty.

    Guards the failure mode directly rather than through the return value: a
    ZSCORE against the base key cannot find the member ``on_save`` wrote, so
    reading it at all is the bug regardless of what the caller does next.
    """
    record = PartitionedMemory(tier="episodic", agent="a1")
    record.save()
    partitioned_key, base_key = _keys_for(record)

    client = get_REDIS_DB()
    assert partitioned_key != base_key, "model is not actually partitioned"
    assert client.zcard(base_key) == 0, "on_save wrote the base key unexpectedly"
    assert client.zcard(partitioned_key) == 1

    observed = []
    real_zscore = client.zscore

    def spy(key, member):
        observed.append(key)
        return real_zscore(key, member)

    client.zscore = spy
    try:
        _get_importance_score(record, "relevance")
    finally:
        client.zscore = real_zscore

    assert observed == [partitioned_key], (
        f"expected one ZSCORE against {partitioned_key!r}, saw {observed!r}"
    )


def test_unpartitioned_importance_is_unchanged():
    """Control: for a field with no ``partition_by`` the two keys are equal.

    This is the byte-identity argument that makes the fix a no-op for every
    existing model — including every model in ``tests/test_memory_lifecycle.py``.
    """
    record = UnpartitionedMemory(tier="episodic")
    record.save()
    partitioned_key, base_key = _keys_for(record)

    assert partitioned_key == base_key

    _plant_stale_score(record)
    score = _get_importance_score(record, "relevance")
    assert score == pytest.approx(ZSET_NORMALIZED, abs=1e-3)


def test_forget_decision_moves_for_partitioned_model():
    """The dead path moved retention, not just a reported number.

    With the floor between the two candidate scores, the sorted-set value
    (0.5) forgets and the attribute value (0.9) does not. ``PartitionedMemory``
    has no ``ConfidenceField``, so the confidence disjunct is ``False`` and the
    importance comparison alone decides.
    """
    record = PartitionedMemory(tier="episodic", agent="a1")
    record.save()
    _plant_stale_score(record)

    lifecycle = MemoryLifecycle(
        model_class=PartitionedMemory,
        importance_field="relevance",
    )
    # Any idle time qualifies (the guard is idle > FORGET_IDLE_SECONDS).
    lifecycle.FORGET_IDLE_SECONDS = -1.0
    # Strictly between ZSET_NORMALIZED and FALLBACK_SENTINEL.
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.75

    assert ZSET_NORMALIZED < lifecycle.FORGET_IMPORTANCE_FLOOR < FALLBACK_SENTINEL
    assert _default_should_forget(record, lifecycle) is True, (
        "forget decision was driven by the attribute fallback, not the ZSET"
    )
