"""Result types returned by :mod:`popoto.transfer`.

:class:`ExportResult` reports what an export wrote. :class:`ImportReport` is
the reconciliation ledger: every record in the export file is accounted for in
exactly one of five outcome categories, with a reason string attached to every
non-landed record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

LANDED = "landed"
SKIPPED = "skipped"
REJECTED = "rejected"
ERRORED = "errored"
PARTIAL = "partial"

CATEGORIES = (LANDED, SKIPPED, REJECTED, ERRORED, PARTIAL)
"""The five outcome categories, in report order.

``landed``
    Saved and all carried state restored.
``skipped``
    Key already present on the destination and ``on_conflict="skip"``.
``rejected``
    Refused before any write -- the write gate said no, or construction or
    validation failed. Nothing was written.
``errored``
    Failed during save, or the write could not be confirmed afterwards.
    Nothing was written, or the write state is indeterminate.
``partial``
    Saved, but restoring carried state raised. **The record exists on the
    destination with rebuild-default auxiliary state.** This is the only
    category that leaves degraded data behind, so it is surfaced first in
    :meth:`ImportReport.summary`.
"""


@dataclass
class ExportResult:
    """Outcome of an :func:`popoto.transfer.export_records` call.

    Attributes:
        model: ``Model.__name__`` of the exported model.
        filter: Rendered provenance of the applied filter, or ``None`` when
            the export was unfiltered. Recorded before evaluation, so it is
            present even when nothing matched.
        filter_kwargs: The plain keyword filters that were forwarded.
        matched_count: Number of keys the filter resolved to, at resolution
            time. Compare against :attr:`record_count` to see how much the
            source shifted underneath a long export.
        record_count: Number of record lines actually written.
        vanished: Keys that resolved but no longer had a hash when their chunk
            was hydrated. Export is not a point-in-time snapshot, so this is a
            counted fact rather than an error.
        filtered_out: Records dropped by a client-side (unindexed) filter
            after hydration.
        warnings: Non-fatal notes, e.g. a client-side filter downgrade or a
            field whose ``export_state`` raised.
        errors: Records that could not be serialized, with the reason.
        data: The full JSONL text when no ``stream`` was supplied; ``None``
            when the caller provided its own stream.
    """

    model: str
    filter: "str | None" = None
    filter_kwargs: dict = field(default_factory=dict)
    matched_count: int = 0
    record_count: int = 0
    vanished: int = 0
    filtered_out: int = 0
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    data: "str | None" = None

    def summary(self) -> str:
        """Render a human-readable multi-line summary."""
        lines = [
            f"ExportResult for {self.model}",
            f"  filter:        {self.filter if self.filter is not None else '(none)'}",
            f"  matched:       {self.matched_count}",
            f"  written:       {self.record_count}",
        ]
        if self.filtered_out:
            lines.append(f"  filtered out:  {self.filtered_out} (client-side filter)")
        if self.vanished:
            lines.append(f"  vanished:      {self.vanished} (deleted mid-export)")
        for warning in self.warnings:
            lines.append(f"  warning:       {warning}")
        for error in self.errors:
            lines.append(f"  error:         {error}")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.summary()


@dataclass
class RecordOutcome:
    """One record's fate during import.

    Attributes:
        key: The record's redis_key, or ``"line <n>"`` when the line was too
            malformed to yield one.
        category: One of :data:`CATEGORIES`.
        reason: Why, for anything that is not ``landed``. Always carries the
            exception text when an exception was involved.
    """

    key: str
    category: str
    reason: "str | None" = None


@dataclass
class ImportReport:
    """Reconciliation ledger for an :func:`popoto.transfer.import_records` call.

    Every record line in the export file produces exactly one
    :class:`RecordOutcome`, so ``len(outcomes)`` equals the number of record
    lines read and the five category counts sum to it.

    Attributes:
        model: ``Model.__name__`` of the destination model.
        outcomes: Per-record outcomes in file order.
        fidelity: The manifest's per-field and per-mixin ``roundtrip_policy``
            roll-up, keyed by field or mixin name.
        warnings: Non-fatal notes (embedding provenance mismatches that were
            carried, and so on). Unknown field names in carried state are
            fatal for that record instead: they raise inside
            ``_restore_state`` and are classified ``partial``, not warned.
        write_gate_bypassed: How many records were saved with the destination's
            write gate deliberately bypassed.
        source_matched_count: The manifest's ``matched_count``, for comparison
            against the number of record lines actually present.
    """

    model: str
    outcomes: list = field(default_factory=list)
    fidelity: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    write_gate_bypassed: int = 0
    source_matched_count: "int | None" = None

    def add(
        self, key: str, category: str, reason: "str | None" = None
    ) -> RecordOutcome:
        """Record one record's outcome and return it."""
        if category not in CATEGORIES:
            raise ValueError(f"unknown outcome category {category!r}")
        outcome = RecordOutcome(key=key, category=category, reason=reason)
        self.outcomes.append(outcome)
        return outcome

    def by_category(self, category: str) -> "list[RecordOutcome]":
        """Return every outcome in ``category``."""
        return [o for o in self.outcomes if o.category == category]

    def count(self, category: str) -> int:
        """Return how many records landed in ``category``."""
        return sum(1 for o in self.outcomes if o.category == category)

    @property
    def total(self) -> int:
        """Number of record lines accounted for."""
        return len(self.outcomes)

    @property
    def landed(self) -> "list[RecordOutcome]":
        return self.by_category(LANDED)

    @property
    def skipped(self) -> "list[RecordOutcome]":
        return self.by_category(SKIPPED)

    @property
    def rejected(self) -> "list[RecordOutcome]":
        return self.by_category(REJECTED)

    @property
    def errored(self) -> "list[RecordOutcome]":
        return self.by_category(ERRORED)

    @property
    def partial(self) -> "list[RecordOutcome]":
        return self.by_category(PARTIAL)

    def _reason_lines(self, outcomes: Iterable[RecordOutcome]) -> "list[str]":
        return [f"    {o.key}: {o.reason or '(no reason recorded)'}" for o in outcomes]

    def summary(self) -> str:
        """Render counts, every non-landed reason, and the fidelity roll-up.

        ``partial`` is called out ahead of the ordinary counts because it is
        the one category that leaves a queryable record behind whose auxiliary
        state was never restored -- it needs attention, not just tallying.
        """
        lines = [f"ImportReport for {self.model}"]

        partial = self.partial
        if partial:
            lines.append(
                f"  ** PARTIAL: {len(partial)} record(s) saved but their carried "
                f"state was NOT restored. These records exist on the destination "
                f"with rebuild-default auxiliary state and need attention. **"
            )
            lines.extend(self._reason_lines(partial))

        lines.append(f"  records read:  {self.total}")
        if self.source_matched_count is not None:
            lines.append(f"  source matched: {self.source_matched_count}")
        for category in CATEGORIES:
            lines.append(f"  {category + ':':<14} {self.count(category)}")
        if self.write_gate_bypassed:
            lines.append(
                f"  write gate bypassed for {self.write_gate_bypassed} record(s)"
            )

        for label, category in (
            ("rejected", REJECTED),
            ("errored", ERRORED),
            ("skipped", SKIPPED),
        ):
            outcomes = self.by_category(category)
            if outcomes:
                lines.append(f"  {label} reasons:")
                lines.extend(self._reason_lines(outcomes))

        if self.fidelity:
            lines.append("  round-trip fidelity:")
            for name, info in sorted(self.fidelity.items()):
                policy = info.get("policy", "rebuild")
                note = info.get("note")
                class_name = info.get("class")
                label = f"{name} ({class_name})" if class_name else name
                if policy == "partial":
                    lines.append(
                        f"    {label}: partial -- {note or 'some state not carried'}"
                    )
                elif policy == "carry":
                    lines.append(f"    {label}: state carried and restored")
                else:
                    lines.append(f"    {label}: rebuilt on import")

        for warning in self.warnings:
            lines.append(f"  warning: {warning}")

        return "\n".join(lines)

    def __str__(self) -> str:
        return self.summary()
