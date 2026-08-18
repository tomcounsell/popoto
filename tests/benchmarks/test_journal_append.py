"""Append micro-benchmark for the provenance journal (#560, plan Risk 2).

Lives here rather than in ``tests/test_provenance_journal.py`` and is marked
``slow``/``benchmark``: the pytest plugin flushes the DB before every test, so a
20k-entry benchmark in the default suite would add minutes to every run.

Measures:
- p50/p99 wall time of ``ProvenanceJournal.append()`` at the epic's 20k-record
  scale target
- the same for a control model identical in every way except that it does not
  compose ``AppendOnlyMixin``, which isolates the guard's ``EXISTS`` round trip
  from the cost of the field set
- the eager-EVAL count per non-pipelined ``save()`` — the five inherited
  eager-EVAL sites (four ``IndexedField``s plus the ``TagField``) the plan's
  Race 1 names, reported rather than asserted because it is a property of the
  field set, not a budget

The assertions are deliberately loose ceilings against a same-process control,
not wall-clock budgets: an absolute millisecond gate on shared hardware is a
coin flip, while a ratio against a control run on the same data catches the
regressions that matter (a second round trip, a per-field probe).
"""

import os
import statistics
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(os.path.dirname(SCRIPT_DIR)))

import pytest
from src import popoto
from src.popoto.fields.event_stream import EventStreamMixin
from src.popoto.fields.shortcuts import (
    AutoKeyField,
    BooleanField,
    FloatField,
    IndexedField,
    KeyField,
    StringField,
    TagField,
)
from src.popoto.fields.validity_field import ValidityField
from src.popoto.privacy.never_record import NeverRecordMixin
from src.popoto.recipes.provenance_journal import JournalEntry, ProvenanceJournal
from src.popoto.redis_db import POPOTO_REDIS_DB

BENCH_N = 20_000
BENCH_AGENT = "bench-agent"

#: Ceiling on the append-only/control p50 ratio. The guard adds exactly one
#: ``EXISTS`` round trip to a write that already costs a pipeline round trip
#: plus five eager EVAL sites, so the expected ratio is well under 2x.
#: Calibrated with headroom so it fails on a real regression (a second probe,
#: a per-field existence check) and not on jitter.
BENCH_MAX_RATIO = 2.5


class PlainEntry(NeverRecordMixin, EventStreamMixin, popoto.Model):
    """Control: the journal's field set with **no** ``AppendOnlyMixin``.

    Identical composition otherwise, so the p50 delta against ``JournalEntry``
    isolates the append-only guard's ``EXISTS`` round trip rather than measuring
    the field set twice.
    """

    entry_id = AutoKeyField()
    agent_id = KeyField()
    captured_at = FloatField(null=True)
    turn_id = IndexedField(type=str, null=True)
    speaker = IndexedField(type=str, null=True)
    verbatim = StringField(default="")
    statement = StringField(default="")
    subjects = TagField(null=True)
    stated = BooleanField(default=True)
    kind = IndexedField(type=str, null=True)
    target = IndexedField(type=str, null=True)
    validity = ValidityField()

    _stream_name = "bench_journal"
    _stream_partition_field = None
    _stream_metadata_fields = ("agent_id", "kind", "target")
    _stream_max_length = 10000


def _percentile(samples, fraction):
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _report(label, samples):
    p50 = statistics.median(samples)
    p99 = _percentile(samples, 0.99)
    print(
        f"\n[journal append @ {len(samples)}] {label}: "
        f"p50={p50:.3f}ms p99={p99:.3f}ms "
        f"mean={statistics.mean(samples):.3f}ms "
        f"max={max(samples):.3f}ms"
    )
    return p50, p99


def _time_appends(fn, n=BENCH_N):
    samples = []
    for i in range(n):
        start = time.perf_counter()
        fn(i)
        samples.append((time.perf_counter() - start) * 1000.0)
    return samples


@pytest.mark.slow
@pytest.mark.benchmark
class TestJournalAppendBenchmark:
    def test_p50_p99_append_at_20k_entries(self):
        """20k real appends through the façade, p50/p99, against a control."""

        def journal_append(i):
            ProvenanceJournal.append(
                agent_id=BENCH_AGENT,
                speaker=f"speaker-{i % 32}",
                turn_id=f"t-{i // 4}",
                verbatim="the launch slipped to the 30th",
                statement=f"Launch date claim {i}",
                subjects=[f"topic-{i % 16}"],
            )

        def control_append(i):
            PlainEntry(
                agent_id=BENCH_AGENT,
                captured_at=time.time(),
                speaker=f"speaker-{i % 32}",
                turn_id=f"t-{i // 4}",
                verbatim="the launch slipped to the 30th",
                statement=f"Launch date claim {i}",
                subjects=[f"topic-{i % 16}"],
                stated=True,
                kind="assert",
                target=None,
            ).save()

        journal_samples = _time_appends(journal_append)
        control_samples = _time_appends(control_append)

        journal_p50, journal_p99 = _report(
            "JournalEntry (append-only)", journal_samples
        )
        control_p50, control_p99 = _report("PlainEntry (control)", control_samples)

        ratio = journal_p50 / control_p50 if control_p50 else float("inf")
        print(
            f"[journal append @ {BENCH_N}] append-only overhead: "
            f"p50 ratio={ratio:.2f}x "
            f"absolute={journal_p50 - control_p50:+.3f}ms; "
            f"p99 {journal_p99:.3f}ms vs {control_p99:.3f}ms"
        )

        assert JournalEntry.query.filter(agent_id=BENCH_AGENT, speaker="speaker-0")
        assert ratio < BENCH_MAX_RATIO, (
            f"append-only guard cost {ratio:.2f}x the control p50 at {BENCH_N} "
            f"entries ({journal_p50:.3f}ms vs {control_p50:.3f}ms) -- expected "
            f"one extra EXISTS round trip, not a per-field probe"
        )

    def test_eager_eval_count_per_non_pipelined_append(self, monkeypatch):
        """Report the Race 1 eager-EVAL exposure per append.

        Reported, not budgeted: the count is a property of D4's field set (four
        ``IndexedField``s plus the ``TagField``), and the plan accepts it rather
        than re-solving the inherited mechanism. The assertion only pins that
        the exposure has not silently multiplied.
        """
        counts = {"eval": 0}
        real_eval = POPOTO_REDIS_DB.eval

        def counting_eval(*args, **kwargs):
            counts["eval"] += 1
            return real_eval(*args, **kwargs)

        monkeypatch.setattr(POPOTO_REDIS_DB, "eval", counting_eval)
        JournalEntry(
            agent_id=BENCH_AGENT,
            captured_at=time.time(),
            speaker="tom",
            turn_id="t-1",
            statement="a claim",
            subjects=["launch"],
            kind="assert",
        ).save()
        monkeypatch.undo()

        print(
            f"\n[journal append] eager EVALs per non-pipelined save: "
            f"{counts['eval']}"
        )
        assert counts["eval"] <= 8, (
            f"eager-EVAL sites per append grew to {counts['eval']}; the plan "
            f"accounts for five (four IndexedFields plus the TagField)"
        )
