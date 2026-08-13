"""Reconciliation tests for popoto.transfer import/export.

Every test runs against live Redis (DB 15, flushed before each test by the
popoto pytest plugin). No mocks.

What is covered here:

- AC #4: every record line is accounted for in exactly one of the five
  outcome categories, with a reason on every non-landed record.
- Write-gate rejections are counted, never silent -- the issue's headline
  failure ("reports 1000 imported when only 600 landed").
- The precedence rule (critique BLOCKER 2): the ``save()`` return value is
  authoritative; a post-write ``EXISTS`` may only downgrade landed -> missing
  and may never upgrade rejected -> landed.
- The HSET-returns-0 trap: a successful overwrite returns 0 from ``save()``,
  so a truthiness test would misreport it as a rejection.
- All three ``on_conflict`` modes (AC #8).
- The ``partial`` category: saved, but state restore raised.
- Malformed input accounting and report rendering.
"""

import io
import json
import os
import sys
from contextlib import contextmanager

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402

from src import popoto  # noqa: E402
from src.popoto.exceptions import ModelException  # noqa: E402
from src.popoto.fields.write_filter import WriteFilterMixin  # noqa: E402
from src.popoto.transfer import export_records, import_records  # noqa: E402
from src.popoto.transfer.results import (  # noqa: E402
    CATEGORIES,
    ERRORED,
    LANDED,
    PARTIAL,
    REJECTED,
    SKIPPED,
)

# --- Test models -------------------------------------------------------


class ReconItem(popoto.Model):
    """Plain model: conflict modes, malformed input, reconciliation math."""

    name = popoto.UniqueKeyField()
    payload = popoto.StringField(default="")


class GateItem(WriteFilterMixin, popoto.Model):
    """Write-gated model.

    ``_wf_min_threshold`` is a plain class attribute (0.2) so records with a
    modest importance can be *saved on the source*; tests then raise the
    destination's threshold with :func:`gate_threshold` to model the real
    migration case -- restoring a faithful backup into a model whose gate has
    since tightened.
    """

    _wf_min_threshold = 0.2

    name = popoto.UniqueKeyField()
    payload = popoto.StringField(default="")
    importance = popoto.FloatField(default=0.0)

    def compute_filter_score(self):
        return self.importance or 0.0


class ExplodingStateField(popoto.IntField):
    """A field that carries state on export and blows up restoring it.

    This is what forces the ``partial`` category: the record's primary hash
    and every ``on_save``-rebuilt structure are already written by the time
    ``import_state`` runs, so reporting ``errored`` (nothing written) would be
    a lie and reporting ``landed`` would be worse.
    """

    roundtrip_policy = "carry"

    @classmethod
    def export_state(cls, model_instance, field_name, field_value, **kwargs):
        return {"aux": "carried-state"}

    @classmethod
    def import_state(cls, model_instance, field_name, state, **kwargs):
        raise RuntimeError("aux store unreachable")


class AuxItem(WriteFilterMixin, popoto.Model):
    """Write-gated model whose carried state cannot be restored."""

    _wf_min_threshold = 0.2

    name = popoto.UniqueKeyField()
    importance = popoto.FloatField(default=0.0)
    aux = ExplodingStateField(default=0)

    def compute_filter_score(self):
        return self.importance or 0.0


# --- Helpers -----------------------------------------------------------


@contextmanager
def gate_threshold(model_class, threshold):
    """Temporarily raise a model's write-gate minimum threshold.

    A plain class attribute shadows ``WriteFilterMixin``'s property because
    ``__getattribute__`` consults the subclass dict first.
    """
    missing = object()
    previous = model_class.__dict__.get("_wf_min_threshold", missing)
    model_class._wf_min_threshold = threshold
    try:
        yield
    finally:
        if previous is missing:
            delattr(model_class, "_wf_min_threshold")
        else:
            model_class._wf_min_threshold = previous


def export_text(model_class, **filters):
    """Export to a string (no stream), returning the JSONL text."""
    result = export_records(model_class, **filters)
    assert result.data is not None
    return result.data


def record_line_count(jsonl):
    """Number of record lines -- everything after the manifest line."""
    return len([line for line in jsonl.splitlines() if line.strip()]) - 1


def import_text(model_class, jsonl, **kwargs):
    return import_records(model_class, io.StringIO(jsonl), **kwargs)


def wipe(model_class):
    for instance in model_class.query.all():
        instance.delete()
    assert model_class.query.all() == []


def keys_in(report, category):
    return {outcome.key for outcome in report.by_category(category)}


def assert_fully_reconciled(report, expected_total):
    """AC #4: exactly one category per record, and reasons everywhere."""
    assert report.total == expected_total
    assert sum(report.count(category) for category in CATEGORIES) == report.total
    for outcome in report.outcomes:
        assert outcome.category in CATEGORIES
        if outcome.category != LANDED:
            assert outcome.reason, f"{outcome.key} has no reason"


# --- AC #4: full reconciliation ----------------------------------------


def test_every_record_is_accounted_for_in_exactly_one_category():
    """A mixed run: skipped, errored, and partial all in one file.

    (``rejected`` and ``landed`` get their own dedicated tests below; the
    point here is that the counts still sum to the number of record lines.)
    """
    for index in range(3):
        AuxItem(name=f"aux{index}", importance=0.9, aux=index).save()
    jsonl = export_text(AuxItem)
    assert record_line_count(jsonl) == 3

    wipe(AuxItem)
    # aux0 pre-exists on the destination -> skipped under on_conflict="skip".
    AuxItem(name="aux0", importance=0.9, aux=99).save()
    # One unparseable line -> errored, counted by line number.
    lines = jsonl.splitlines()
    lines.insert(2, "{ this is not json")
    spliced = "\n".join(lines) + "\n"

    with gate_threshold(AuxItem, 0.5):
        report = import_text(AuxItem, spliced, on_conflict="skip")

    assert_fully_reconciled(report, record_line_count(spliced))
    assert report.count(SKIPPED) == 1
    assert report.count(ERRORED) == 1
    # The remaining two records save, then their carried state fails to
    # restore -- partial, not landed and not errored.
    assert report.count(PARTIAL) == 2
    assert report.count(LANDED) == 0


def test_clean_run_accounts_for_everything_as_landed():
    for index in range(5):
        ReconItem(name=f"r{index}", payload=f"payload-{index}").save()
    jsonl = export_text(ReconItem)
    wipe(ReconItem)

    report = import_text(ReconItem, jsonl)

    assert_fully_reconciled(report, 5)
    assert report.count(LANDED) == 5
    assert len(ReconItem.query.all()) == 5


# --- Write gate: counted rejections, never silent drops -----------------


def test_write_gate_rejections_are_counted_not_silent():
    """The headline failure class: 1000 imported, only 600 landed.

    Two of four records fall below the destination's (raised) threshold.
    They must appear as counted ``rejected`` outcomes with a reason naming
    the write gate -- not as silent drops, and emphatically not as landed.
    """
    ReconItemScores = [("hi0", 0.9), ("hi1", 0.9), ("lo0", 0.3), ("lo1", 0.3)]
    for name, importance in ReconItemScores:
        GateItem(name=name, payload="from-source", importance=importance).save()
    jsonl = export_text(GateItem)
    assert record_line_count(jsonl) == 4
    wipe(GateItem)

    with gate_threshold(GateItem, 0.5):
        report = import_text(GateItem, jsonl)

    assert_fully_reconciled(report, 4)
    assert report.count(LANDED) == 2
    assert report.count(REJECTED) == 2
    assert keys_in(report, REJECTED) == {"GateItem:lo0", "GateItem:lo1"}
    for outcome in report.by_category(REJECTED):
        assert "write gate" in outcome.reason

    # Ground truth: the rejected records really are absent.
    assert {item.name for item in GateItem.query.all()} == {"hi0", "hi1"}
    assert report.count(LANDED) == len(GateItem.query.all())


def test_write_gate_bypass_lands_records_and_is_counted():
    for name, importance in [("hi", 0.9), ("lo0", 0.3), ("lo1", 0.3)]:
        GateItem(name=name, payload="from-source", importance=importance).save()
    jsonl = export_text(GateItem)
    wipe(GateItem)

    with gate_threshold(GateItem, 0.5):
        report = import_text(GateItem, jsonl, on_write_gate="bypass")

    assert_fully_reconciled(report, 3)
    assert report.count(LANDED) == 3
    assert report.count(REJECTED) == 0
    assert report.write_gate_bypassed == 3
    assert {item.name for item in GateItem.query.all()} == {"hi", "lo0", "lo1"}


# --- The precedence rule (critique BLOCKER 2) ---------------------------


def test_overwrite_gate_reject_precedence_reports_rejected_not_landed():
    """on_conflict="overwrite" + a write-gate rejection on an existing key.

    The trap: ``save()`` returns False from ``base.py`` *before any HSET*, so
    the destination's OLD hash is left completely untouched -- yet a naive
    post-write ``EXISTS`` returns 1 and would report the record as LANDED.
    That is the "1000 imported, 600 landed" failure reintroduced at the
    reconciliation step.

    The rule: the per-record ``save()`` return value is authoritative for
    classification; ``EXISTS`` is a corroborating check that may only
    downgrade landed -> missing, never upgrade rejected -> landed.
    """
    item = GateItem(name="k", payload="old", importance=0.9)
    item.save()

    # Export a *newer* version of the same key whose score is low.
    item.payload = "new"
    item.importance = 0.3
    item.save()
    jsonl = export_text(GateItem)

    # Put the destination back to the old, high-importance version, so the
    # key exists with known old values when the import runs.
    GateItem(name="k", payload="old", importance=0.9).save()
    assert GateItem.query.get(name="k").payload == "old"

    with gate_threshold(GateItem, 0.5):
        report = import_text(GateItem, jsonl, on_conflict="overwrite")

    assert_fully_reconciled(report, 1)
    # (a) rejected, NOT landed -- even though EXISTS is true afterwards.
    assert report.count(REJECTED) == 1
    assert report.count(LANDED) == 0
    assert "write gate" in report.by_category(REJECTED)[0].reason

    # (b) nothing was overwritten: the OLD values survive intact.
    survivor = GateItem.query.get(name="k")
    assert survivor.payload == "old"
    assert survivor.importance == 0.9


# --- The HSET-returns-0 trap -------------------------------------------


def test_identical_reimport_with_overwrite_reports_landed():
    """HSET returns 0 when every field already exists.

    ``save()`` returns that reply count on the success path, so a truthiness
    check would misclassify a successful no-op overwrite as a rejection.
    Re-importing an identical file must converge, not report failure.
    """
    for index in range(3):
        ReconItem(name=f"r{index}", payload=f"payload-{index}").save()
    jsonl = export_text(ReconItem)

    # Destination already holds byte-identical records: every HSET is a no-op.
    first = import_text(ReconItem, jsonl, on_conflict="overwrite")
    assert_fully_reconciled(first, 3)
    assert first.count(LANDED) == 3
    assert first.count(REJECTED) == 0

    # Idempotent re-run convergence: same again, still no duplicates.
    second = import_text(ReconItem, jsonl, on_conflict="overwrite")
    assert second.count(LANDED) == 3
    assert second.count(REJECTED) == 0
    assert len(ReconItem.query.all()) == 3


# --- AC #8: all three on_conflict modes ---------------------------------


def _seed_three_and_export():
    """Export r0/r1/r2, wipe, then re-create r1 with known OLD values."""
    for index in range(3):
        ReconItem(name=f"r{index}", payload=f"payload-{index}").save()
    jsonl = export_text(ReconItem)
    wipe(ReconItem)
    ReconItem(name="r1", payload="old-r1").save()
    return jsonl


def record_keys_in_file_order(jsonl):
    """Record keys as they appear in the file.

    Export resolves its key set in sorted order but hydrates each chunk
    through ``get_many_objects``, which takes a set -- so the *written* order
    is not the sorted order. Tests that care about ordering read it back from
    the file rather than assuming it.
    """
    return [json.loads(line)["key"] for line in jsonl.splitlines()[1:]]


def test_on_conflict_error_raises_and_is_not_atomic():
    for index in range(3):
        ReconItem(name=f"r{index}", payload=f"payload-{index}").save()
    jsonl = export_text(ReconItem)
    order = record_keys_in_file_order(jsonl)
    assert len(order) == 3
    wipe(ReconItem)

    # Make the SECOND record in file order the colliding one, so exactly one
    # record is written before the collision and one after it.
    before, collision, after = order
    ReconItem(name=collision.split(":", 1)[1], payload="old-collision").save()

    with pytest.raises(ModelException) as excinfo:
        import_text(ReconItem, jsonl, on_conflict="error")
    assert collision in str(excinfo.value)

    # Import is deliberately not atomic across records: records before the
    # collision are already written. Documented behavior, asserted here so a
    # future change to it is a test failure rather than a surprise.
    before_name = before.split(":", 1)[1]
    after_name = after.split(":", 1)[1]
    assert ReconItem.query.get(name=before_name).payload == f"payload-{before_name[1:]}"
    assert ReconItem.query.get(name=collision.split(":", 1)[1]).payload == (
        "old-collision"
    )
    assert ReconItem.query.get(name=after_name) is None


def test_on_conflict_skip_leaves_the_existing_record_untouched():
    jsonl = _seed_three_and_export()

    report = import_text(ReconItem, jsonl, on_conflict="skip")

    assert_fully_reconciled(report, 3)
    assert report.count(SKIPPED) == 1
    assert report.count(LANDED) == 2
    skipped = report.by_category(SKIPPED)[0]
    assert skipped.key == "ReconItem:r1"
    assert "exists" in skipped.reason

    assert ReconItem.query.get(name="r1").payload == "old-r1"
    assert ReconItem.query.get(name="r0").payload == "payload-0"
    assert ReconItem.query.get(name="r2").payload == "payload-2"


def test_on_conflict_overwrite_replaces_the_existing_record():
    jsonl = _seed_three_and_export()

    report = import_text(ReconItem, jsonl, on_conflict="overwrite")

    assert_fully_reconciled(report, 3)
    assert report.count(LANDED) == 3
    assert report.count(SKIPPED) == 0
    assert ReconItem.query.get(name="r1").payload == "payload-1"
    assert len(ReconItem.query.all()) == 3


# --- The partial category ----------------------------------------------


def test_state_restore_failure_is_partial_not_errored_or_landed():
    """Save succeeded, ``import_state`` raised: the record exists degraded."""
    for index in range(2):
        AuxItem(name=f"aux{index}", importance=0.9, aux=index).save()
    jsonl = export_text(AuxItem)
    wipe(AuxItem)

    report = import_text(AuxItem, jsonl)

    assert_fully_reconciled(report, 2)
    assert report.count(PARTIAL) == 2
    assert report.count(ERRORED) == 0
    assert report.count(LANDED) == 0
    assert report.count(REJECTED) == 0

    for outcome in report.by_category(PARTIAL):
        assert "RuntimeError" in outcome.reason
        assert "aux store unreachable" in outcome.reason
        assert "rebuild-default" in outcome.reason

    # The record really is on the destination and queryable -- which is why
    # "errored" (nothing written) would be the wrong report.
    assert {item.name for item in AuxItem.query.all()} == {"aux0", "aux1"}
    assert AuxItem.query.get(name="aux1") is not None


# --- Malformed input ----------------------------------------------------


def test_malformed_line_is_errored_with_line_number_and_import_continues():
    for index in range(3):
        ReconItem(name=f"r{index}", payload=f"payload-{index}").save()
    jsonl = export_text(ReconItem)
    wipe(ReconItem)

    lines = jsonl.splitlines()
    # Line 1 is the manifest, line 2 the first record; truncate line 3.
    lines.insert(2, '{"key": "ReconItem:trunc", "values": {"name": ')
    spliced = "\n".join(lines) + "\n"

    report = import_text(ReconItem, spliced)

    assert_fully_reconciled(report, 4)
    assert report.count(ERRORED) == 1
    errored = report.by_category(ERRORED)[0]
    assert errored.key == "line 3"
    assert "malformed JSON line" in errored.reason

    # Import continued past the bad line: every later record still landed.
    assert report.count(LANDED) == 3
    assert len(ReconItem.query.all()) == 3


def test_stream_without_a_manifest_raises_before_writing_anything():
    for index in range(2):
        ReconItem(name=f"r{index}", payload=f"payload-{index}").save()
    jsonl = export_text(ReconItem)
    wipe(ReconItem)

    without_manifest = "\n".join(jsonl.splitlines()[1:]) + "\n"

    with pytest.raises(ModelException) as excinfo:
        import_text(ReconItem, without_manifest)
    assert "manifest" in str(excinfo.value)

    assert ReconItem.query.all() == []


# --- Report rendering ---------------------------------------------------


def test_summary_renders_rejection_skip_and_partial():
    AuxItem(name="keep", importance=0.9, aux=1).save()
    AuxItem(name="lowscore", importance=0.3, aux=2).save()
    AuxItem(name="fresh", importance=0.9, aux=3).save()
    jsonl = export_text(AuxItem)

    wipe(AuxItem)
    AuxItem(name="keep", importance=0.9, aux=99).save()

    with gate_threshold(AuxItem, 0.5):
        report = import_text(AuxItem, jsonl, on_conflict="skip")

    assert_fully_reconciled(report, 3)
    assert report.count(SKIPPED) == 1
    assert report.count(REJECTED) == 1
    assert report.count(PARTIAL) == 1

    rendered = report.summary()
    assert rendered == str(report)

    # All three categories are named.
    assert "skipped" in rendered
    assert "rejected" in rendered
    assert "partial" in rendered.lower()

    # The rejection reason text itself is surfaced, not just a count.
    rejection_reason = report.by_category(REJECTED)[0].reason
    assert rejection_reason in rendered
    assert "AuxItem:lowscore" in rendered

    # And the skip reason.
    assert report.by_category(SKIPPED)[0].reason in rendered

    # partial is surfaced prominently: before the ordinary counts, and loud.
    assert "PARTIAL" in rendered
    assert rendered.index("PARTIAL") < rendered.index("records read")
    assert "need attention" in rendered
    assert report.by_category(PARTIAL)[0].reason in rendered


# --- Invalid policy arguments ------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"on_conflict": "clobber"},
        {"on_write_gate": "ignore"},
        {"on_embedding_mismatch": "shrug"},
        {"on_conflict": None},
    ],
)
def test_invalid_policy_arguments_raise_value_error(kwargs):
    with pytest.raises(ValueError):
        import_records(ReconItem, io.StringIO(""), **kwargs)
