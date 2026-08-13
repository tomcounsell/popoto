"""Generic export/import with per-field round-trip fidelity.

Moves one model's records between Redis instances -- migrating a machine,
seeding staging from production, taking a logical backup of a single model --
either with full fidelity or with an explicit, itemized report of what could
not be preserved and why.

The driver is model-generic: it holds no knowledge of any concrete field or
mixin type. Fields and model-level mixins declare their own fidelity via the
round-trip protocol on :class:`popoto.Field` (``roundtrip_policy``,
``roundtrip_note``, ``export_state``, ``import_state``), all of which have
working no-op defaults, so a field type Popoto does not yet have round-trips
correctly with no change here.

Example:
    with open("memories.jsonl", "w") as fh:
        Memory.export_records(project_key="ai", stream=fh)

    with open("memories.jsonl") as fh:
        report = Memory.import_records(fh, on_conflict="overwrite")
    print(report.summary())
"""

from .export import DEFAULT_CHUNK_SIZE, export_records
from .format import FORMAT_VERSION
from .import_ import import_records
from .results import CATEGORIES, ExportResult, ImportReport, RecordOutcome

__all__ = [
    "export_records",
    "import_records",
    "ExportResult",
    "ImportReport",
    "RecordOutcome",
    "CATEGORIES",
    "FORMAT_VERSION",
    "DEFAULT_CHUNK_SIZE",
]
