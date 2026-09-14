"""Concurrency tests for CyclicDecayField's companion-hash atomicity (#699).

Before this change, ``CyclicDecayField.on_save`` and
``Model._adjust_cycle_amplitudes`` (behind ``strengthen_cycle``/
``weaken_cycle``) each did an unsynchronized client-side
HGET -> mutate -> HSET against the cycles/pressure companion hashes. Two
concurrent writers on the same member could interleave between the read and
the write and the later HSET would silently clobber the earlier one's
update — a classic lost-update race, with no exception and no log line.

#699 replaces both round trips with one atomic server-side Lua script each
(``CYCLES_MERGE_LUA``, ``CYCLES_ADJUST_LUA``), so the whole
read-decide-write sequence is a single indivisible step. These tests prove
the race is closed by driving many real, concurrent threads against a real
Redis connection (real network round trips, real thread-scheduling jitter)
rather than by mocking the interleaving — a test that cannot fail against
the pre-fix implementation would not be proving anything.

Test A and Test B were both run against pre-fix `main` (via `git stash`)
before this file was accepted, and both failed there — see the PR
description for the captured red-state output.
"""

import os
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import msgpack
import pytest
from src import popoto
from src.popoto.fields.constants import TemporalPeriod
from src.popoto.redis_db import get_REDIS_DB

# --- Test Models (isolated from tests/test_cyclic_decay_field.py's models) ---

DECLARED_AMPLITUDE = 2.0


class AtomicCycles(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = popoto.CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.DAILY, DECLARED_AMPLITUDE, 0)],
    )


class AtomicPressure(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = popoto.CyclicDecayField(
        decay_rate=0.5,
        pressure_rate=0.1,
    )


class TestConcurrentAmplitudeAdjustment:
    """Test A: concurrent strengthen_cycle() calls must not lose updates."""

    def test_concurrent_strengthen_cycle_multiplies_exactly_k_times(self):
        item = AtomicCycles.create(name="race-amplitude")
        item.save()

        factor = 1.1
        num_threads = 20
        barrier = threading.Barrier(num_threads)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=5)
                item.strengthen_cycle("relevance", factor=factor)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"worker thread(s) raised: {errors!r}"

        field = item._meta.fields["relevance"]
        cycles_hash_key = field.get_cycles_hash_key(item, "relevance")
        raw = get_REDIS_DB().hget(cycles_hash_key, item.db_key.redis_key)
        assert raw is not None, "cycles entry must still exist"
        stored = msgpack.unpackb(raw, raw=False)
        final_amplitude = stored[0][1]

        # Multiplication by a fixed factor is commutative/associative in
        # value, so the expected result is deterministic regardless of
        # thread interleaving order -- PROVIDED no update is lost. Under the
        # pre-#699 race, concurrent HGET/HSET pairs clobber each other and
        # the final amplitude reflects fewer than `num_threads` multiplies.
        expected = DECLARED_AMPLITUDE * (factor**num_threads)
        assert final_amplitude == pytest.approx(expected, rel=1e-9), (
            f"lost update detected: expected {expected} "
            f"({num_threads} concurrent x{factor} multiplies from "
            f"{DECLARED_AMPLITUDE}), got {final_amplitude}"
        )


class TestConcurrentPressureResolution:
    """Test B: save() interleaved with resolve_pressure() must never let
    last_resolved regress to a stale, previously-read value."""

    def test_last_resolved_never_regresses_under_concurrent_save_and_resolve(
        self,
    ):
        item = AtomicPressure.create(name="race-pressure")
        item.save()

        field = item._meta.fields["relevance"]
        pressure_hash_key = field.get_pressure_hash_key(item, "relevance")
        member_key = item.db_key.redis_key

        stop = threading.Event()
        samples = []
        sample_lock = threading.Lock()
        errors = []

        def sampler():
            while not stop.is_set():
                raw = get_REDIS_DB().hget(pressure_hash_key, member_key)
                if raw is not None:
                    data = msgpack.unpackb(raw, raw=False)
                    with sample_lock:
                        samples.append(data["last_resolved"])
                time.sleep(0.001)

        def saver():
            try:
                while not stop.is_set():
                    item.save()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def resolver():
            try:
                while not stop.is_set():
                    item.resolve_pressure("relevance")
                    time.sleep(0.001)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        sampler_thread = threading.Thread(target=sampler)
        # ONE resolver thread, by design. resolve_pressure() is a blind HSET
        # of a client-side time.time() (spike-4 left it untouched), so two
        # concurrent resolvers can read the clock in one order and write in
        # the reverse -- a microsecond-scale inversion no save()-side fix can
        # remove, and NOT the #699 defect (a stale-cache clobber is
        # millisecond-scale). With one resolver this test's contract is
        # exact: the only way last_resolved can regress is a racing save()
        # overwriting a fresher resolve, which is precisely the
        # read-modify-write CYCLES_MERGE_LUA closes.
        worker_threads = [
            threading.Thread(target=saver),
            threading.Thread(target=saver),
            threading.Thread(target=resolver),
        ]

        sampler_thread.start()
        for t in worker_threads:
            t.start()

        time.sleep(0.3)
        stop.set()
        for t in worker_threads:
            t.join(timeout=10)
        sampler_thread.join(timeout=10)

        assert not errors, f"worker thread(s) raised: {errors!r}"
        assert len(samples) > 1, "sampler collected too few observations"

        regressions = [
            (before, after)
            for before, after in zip(samples, samples[1:])
            if after < before
        ]
        assert not regressions, (
            f"last_resolved regressed (a racing save() overwrote a fresher "
            f"resolve_pressure() write with a stale cached value): "
            f"{regressions[:5]!r}"
        )


class TestNoClientSideReadDuringSave:
    """Test C: on_save() issues zero client-side HGET calls -- the merge
    decision is made entirely inside CYCLES_MERGE_LUA now, closing the
    lost-update window that a client-side read-then-write would reopen."""

    def test_save_issues_no_client_side_hget(self, monkeypatch):
        from src.popoto.fields import cyclic_decay_field as cdf

        item = AtomicCycles.create(name="no-read-on-save")
        item.save()  # seed a stored entry so a read would have something to see

        real = cdf.get_REDIS_DB()
        calls = []

        class CountingClient:
            def __getattr__(self, attr):
                return getattr(real, attr)

            def hget(self, key, member):
                calls.append(key)
                return real.hget(key, member)

        monkeypatch.setattr(cdf, "get_REDIS_DB", lambda: CountingClient())
        item.save()

        assert calls == [], (
            f"on_save() must not issue any client-side HGET against the "
            f"cycles/pressure companion hashes: {calls}"
        )
