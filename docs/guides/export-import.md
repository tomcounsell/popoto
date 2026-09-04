# Export & Import

`popoto.transfer` moves one model's records between Redis instances — migrating to a
new machine, seeding staging from production, taking a logical backup of a single
model, or merging two datasets. It exists because the two naive approaches both lose
data silently:

- An RDB copy or `DUMP`/`RESTORE` is all-or-nothing at the database level. It cannot
  extract one model, cannot filter to a subset, and clobbers rather than merges.
- `to_dict()` followed by `Model(**d).save()` looks correct and mostly is, but four
  things reset silently: an `AutoKeyField` generates a fresh key, learned state like
  a `ConfidenceField` score reseeds to its initial value, `auto_now` timestamps
  restart their clock, and a write-gate rejection makes `save()` return falsy with
  no exception — a script that ignores the return value reports success.

Export and import fix all four by round-tripping through a documented protocol (see
[Writing Custom Fields](../field-authoring.md) for the field-author side) and by
reconciling every record against Redis rather than trusting `save()`'s return value
alone.

## Exporting

```python
from popoto import Model, KeyField, Field, SortedField

class Memory(Model):
    memory_id = KeyField()
    content = Field(type=str)
    project_key = Field(type=str)
    relevance = SortedField(type=float)

with open("memories.jsonl", "w") as fh:
    result = Memory.export_records(project_key="ai", stream=fh)

print(result.summary())
```

With no arguments, `export_records()` exports every record of the model:

```python
with open("all_memories.jsonl", "w") as fh:
    result = Memory.export_records(stream=fh)
```

Filter arguments are forwarded verbatim to `Model.query.filter(...)` — plain keyword
filters, `Q` objects, or both. An unknown filter parameter raises `QueryException`
rather than being silently ignored, so a typo in a filter name fails loudly instead
of exporting everything.

If you omit `stream`, the JSON Lines text is returned on `result.data` instead of
being written to a file — convenient for small models or tests.

Export writes a manifest line followed by one JSON object per record. The manifest
records the model name, the applied filter (or `null` for an unfiltered export), the
number of records the filter matched at resolution time, and the round-trip policy
Popoto is about to apply to each field. This is what makes an empty model
distinguishable from a filter that matched nothing: an unfiltered empty model reports
`{"filter": null, "matched_count": 0}`, while a filter matching nothing reports
`{"filter": "Q(project_key='nope')", "matched_count": 0}`.

Export is **not** a point-in-time snapshot. The key set is resolved once and then
hydrated in chunks (500 keys at a time, by default), so a record deleted after key
resolution is counted as `vanished` and simply omitted, and a record created
afterward is absent from the export. `result.matched_count` versus
`result.record_count` shows you the gap if one exists.

`ValidityField` declares `roundtrip_policy = "carry"` and carries all six of its
derived Redis keys (`valid_from`, `invalid_at`, `ingested_at`, `chain:fwd`,
`chain:rev`, and the per-identity `open:{digest}` pointer) through export and
import — without it, a round trip would silently reopen every superseded record.
Because there is no reverse index from record to identity digest, exporting a
record with a `ValidityField` costs one extra `SCAN` over that field's
`open:*` pointers to find any aimed at it; this only runs on the export path, not
on save or read. See
[ValidityField and SupersessionProtocol](../features/validity-and-supersession.md#export-import)
for the full accounting.

### `ExportResult`

`export_records()` returns an `ExportResult`:

| Attribute | Meaning |
|---|---|
| `matched_count` | Keys the filter resolved to, at resolution time. |
| `record_count` | Record lines actually written. |
| `vanished` | Keys that resolved but no longer had a hash by the time their chunk was hydrated. |
| `filtered_out` | Records dropped by a client-side (unindexed) filter, applied after hydration. |
| `warnings` | Non-fatal notes — a client-side filter downgrade, or a field whose `export_state` raised. |
| `errors` | Records that could not be serialized, with the reason. |

`result.summary()` renders all of this as human-readable text.

## Importing

```python
with open("memories.jsonl") as fh:
    report = Memory.import_records(fh, on_conflict="overwrite")

print(report.summary())
```

Keys are always preserved on import — the imported record lands at the same Redis
key it exported from. This is what makes `Relationship` values and any
application-level string holding a Redis key keep pointing at the right record, and
it is what makes `on_conflict="overwrite"` an idempotent way to resume an interrupted
import.

### The three policy flags

`import_records` takes three flags, each with a default chosen to fail safely rather
than silently:

**`on_conflict`** — what to do when the destination already holds a key.

- `"error"` (default) — refuses on the first collision. The only mode that cannot
  clobber existing data. Costs nothing on a fresh destination, since there are no
  collisions to refuse.
- `"skip"` — leaves the existing record untouched and reports it as `skipped`. Use
  this for a merge that must not disturb what is already there.
- `"overwrite"` — replaces the existing record. Safe specifically because keys are
  preserved, and it is what makes re-running an import after an interruption
  converge instead of duplicating.

**`on_write_gate`** — how to handle the destination model's `WriteFilterMixin` gate,
if it has one.

- `"reject"` (default) — honors the gate. A record the gate refuses is reported as
  `rejected`, not silently dropped.
- `"bypass"` — writes around Popoto's own gate. This is a deliberate keystroke for
  restoring a faithful backup into a model whose threshold has since risen; the
  report states how many records used it (`report.write_gate_bypassed`). Note the
  limit: `"bypass"` only disables Popoto's `WriteFilterMixin` gate. An
  application-level `save()` override that returns falsy for its own reasons cannot
  be bypassed from the library — those records still appear as rejections.

**`on_embedding_mismatch`** — how to handle an `EmbeddingField` whose exported
provider fingerprint (`provider`, `model`, `dimensions`) differs from the
destination's configured provider.

- `"error"` (default) — refuses. Carrying a vector into a different vector space is
  silent corruption; this turns it into a loud, cheap check naming both fingerprints.
- `"carry"` — imports the vectors anyway, for when you know both sides run
  compatible models despite the fingerprint mismatch.
- `"regenerate"` — drops the carried vector so `on_save` re-embeds from the source
  text on the destination.

### Reading an `ImportReport`

Every exported record ends up in exactly one of five categories:

| Category | Meaning |
|---|---|
| `landed` | Saved, and all carried state restored. |
| `skipped` | Key already present, `on_conflict="skip"`. |
| `rejected` | Refused before any write — write gate, or construction/validation failure. Nothing written. |
| `errored` | Failed during save, or the write could not be confirmed afterward. |
| `partial` | Saved, but restoring carried state raised. **The record exists on the destination with rebuild-default auxiliary state.** |

`partial` is the one category that leaves degraded data behind rather than a clean
absence, so `report.summary()` surfaces it first. `report.fidelity` carries the
per-field `roundtrip_policy` roll-up from the manifest, so the report can tell you
which fields were fully restored and which were only ever declared `"partial"` on
the source side (for example, `CoOccurrenceField` or `EventStreamMixin` — see
[Writing Custom Fields](../field-authoring.md) for the full policy taxonomy).

Rejection is never detected by truthiness. `Model.save()` returns the `HSET` reply
count on success, and `HSET` returns `0` when every field already existed — so a
successful overwrite would look identical to a rejection under a truthiness check.
Only `save()` returning `False` or `None` counts as a refusal. A batch `EXISTS`
check afterward corroborates `landed` records and can *downgrade* one to `errored`
if the write did not actually survive, but it can never *upgrade* a rejection to
`landed` — a write-gate rejection under `on_conflict="overwrite"` leaves the
destination's old record in place, and a naive `EXISTS`-only check would have
misreported that as success.

## Resuming an interrupted import

Import is not atomic across records — a crash partway through leaves some records
written and some not. The recovery path is to re-run the same file with
`on_conflict="overwrite"`: already-landed records overwrite themselves with identical
values (a no-op in effect), and records that had not yet been written land normally.
Because keys are always preserved, this converges rather than producing duplicates.

```python
with open("memories.jsonl") as fh:
    report = Memory.import_records(fh, on_conflict="overwrite")

if report.errored or report.partial:
    print(report.summary())  # inspect what needs attention
```

## What is not carried

A handful of Redis structures are shaped by *history* — an event stream, a
prediction ledger, an access log, a frequency-sketch counter — rather than by a
snapshot of current state. Popoto does not attempt to carry these; the affected
fields declare `roundtrip_policy = "partial"` with a `roundtrip_note` explaining
what is lost, and that note appears in the import report rather than the loss
happening silently. See the field-level policy table on the destination model, or
[Writing Custom Fields](../field-authoring.md) if you are deciding how to declare
this for your own field.

Async twins (`async_export_records` / `async_import_records`) are not part of this
API; the driver is a synchronous Python function you call from your own script or an
async wrapper. A CLI front-end is available — see
[From the command line](#from-the-command-line) below.

## From the command line

The `popoto-transfer` console script ships with the package (no extra install step
beyond `pip install popoto`) and wraps `export_records` / `import_records` with a
reconciliation summary, an exit code a script can act on, and a refusal to touch
Redis database 0 by accident.

```console
$ popoto-transfer --help
$ popoto-transfer export --help
$ popoto-transfer import --help
```

### `export`

```console
$ popoto-transfer export --model myapp.models:Memory --filter project_key=ai \
    --out memories.jsonl
ExportResult for Memory
  filter:        Q(project_key='ai')
  matched:       1284
  written:       1284
```

`--model module.path:ClassName` (one colon) names the model to export. The named
module is **imported** to resolve the class, so this runs whatever module-level code
the operator's model module contains — the same caution as any other Python import.
The current working directory is added to `sys.path` first, so `--model
myapp.models:Memory` resolves from the operator's own project root.

`--filter key=value` (repeatable) narrows the export with an equality filter; each
value is parsed as JSON first (so `0.5`, `true`, `null` carry their type), falling
back to a raw string otherwise. `Q` objects and lookup operators (`__gte`, `__in`,
and friends) are not expressible on the command line — use the Python API
(`export_records` / `Model.export_records`) for those. `--chunk-size` controls how
many keys are hydrated per round trip (default 500).

`--out PATH` writes JSON Lines to `PATH` (default `-` for stdout); a failed export
never truncates a pre-existing file at `PATH`, since the export is written to a
sibling temporary file and promoted only on success.

### `import`

```console
$ popoto-transfer import --model myapp.models:Memory --in memories.jsonl \
    --on-conflict overwrite
ImportReport for Memory
  records read:  1284
  landed:        1284
  ...
$ echo $?
0
```

`--in PATH` reads JSON Lines from `PATH` (default `-` for stdin). `--on-conflict`
(`error` default, `skip`, `overwrite`), `--on-write-gate` (`reject` default,
`bypass`), and `--on-embedding-mismatch` (`error` default, `carry`, `regenerate`)
mirror the three Python API policy flags described above exactly. Keys are always
preserved on import, so a re-run with `--on-conflict overwrite` converges rather than
duplicating a partially completed import.

### `--json` and where the summary goes

The human-readable summary always goes to **stderr**, so `--out -` can stream JSON
Lines on stdout without the summary corrupting it:

```console
$ popoto-transfer export --model myapp.models:Memory --out - | gzip > backup.jsonl.gz
```

`--json` writes a machine-readable summary (the result/report as JSON, plus a
`counts` object) to **stdout** instead, and is refused together with `--out -` since
both would claim stdout.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Ran to completion; every record accounted for as landed or skipped, no errors. |
| 1 | The run failed: bad `--model`, the database-0 refusal, an unreadable file, a manifest mismatch, a query error, a connection error, or an `on_conflict="error"` collision — which may have written earlier records before raising. |
| 2 | An `argparse` usage error (argparse's own convention). |
| 3 | The run completed, but at least one record did not land: any `rejected`, `errored`, or `partial` import outcome, or any export error. A `skipped` import outcome is clean and does not trigger this. |

### Refusing database 0

Both subcommands refuse to run when the effective Redis database is 0, unless
`--allow-db0` is passed:

```console
$ popoto-transfer import --model myapp.models:Memory --in memories.jsonl
popoto-transfer: refusing to write to database 0 -- this is often a live store, not
a test database.
  Pass --allow-db0 to proceed anyway, or point at a different database, e.g.
  REDIS_URL=redis://localhost:6379/1
```

The check reads the database off the live connection pool, not an environment
variable, so it catches the unset-`REDIS_URL` fallback (which also binds database 0)
as well as an explicit `…/0` URL. It runs before the operator's `--model` module is
imported and before any Redis command is issued.
