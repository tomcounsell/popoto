---
status: Ready
type: feature
appetite: Large
owner: Valor Engels
created: 2026-08-10
tracking: https://github.com/tomcounsell/popoto/issues/554
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-08-10T07:15:58Z
---

# Generic export/import with per-field round-trip fidelity

## Problem

A developer wants to move one Popoto model's records from one Redis instance to
another — migrating to a new machine, seeding staging from production, taking a
logical backup of a single model, or merging two datasets. Today they have two
options and both are bad.

An RDB copy or `DUMP`/`RESTORE` is all-or-nothing at the database level: it cannot
extract one model, cannot filter to a subset, and clobbers rather than merges. It
also forces the operator to reason about which of Popoto's many auxiliary Redis
keys belong to which model — exactly the internal detail Popoto exists to hide.

The alternative — `to_dict()` → `Model(**d).save()` — is 90% correct, which makes
it worse. Re-saving genuinely does rebuild derived structures, because every field
maintains its own via `on_save`. But four losses happen silently and the script
reports success:

1. **Identity is not preserved.** `AutoKeyField` generates a fresh UUID on
   instantiation, so every imported record gets a new primary key and every
   reference to the old key dangles.
2. **Learned state resets to its prior.** `ConfidenceField.on_save` seeds the
   companion hash with `field.initial_confidence`, not the exported value.
3. **Time-derived state restarts its clock.** `SortedFieldMixin.format_value_pre_save`
   stamps `time.time()` on `auto_now` fields, so a two-year-old record ranks as
   if written today.
4. **Write gates drop records without raising.** `WriteFilterMixin` rejects
   below-threshold records by making `save()` return `False`.

**Current behavior:** No export/import API exists anywhere in `src/popoto`
(confirmed by grep for `export` / `dump` / `serialize` / `from_dict`). Developers
write bespoke scripts that hit all four failure classes with no diagnostic signal.

**Desired outcome:** A supported, generic export/import that round-trips any
model — whatever combination of field types it uses — with either full fidelity
or an explicit, itemized report of what could not be preserved and why. A field
type Popoto does not yet have works correctly with no changes to the driver.

## Freshness Check

**Baseline commit:** `15b76e7`
**Issue filed at:** 2026-08-10T05:17:18Z
**Disposition:** Unchanged (references verified; two corrections noted)

**File:line references re-verified:**

- `src/popoto/models/base.py:2300` — `to_dict()` — **still holds.** Signature is
  `to_dict(include, exclude, relationships, max_depth, _seen)`.
- `src/popoto/fields/field.py:485` — `Field.on_save` classmethod — **still holds.**
- `src/popoto/fields/write_filter.py:110` — write gate — **holds with a
  correction.** Line 110 is `_check_write_filter`, which *raises*
  `SkipSaveException` (write_filter.py:135). The conversion to `return False`
  happens in the caller, `Model.save()` at base.py:1148-1153. The issue's framing
  ("returning `False` from `save()`") describes the observed behavior correctly
  but the mechanism is a raise-then-catch. This matters: the bypass lever must be
  added at base.py:1148, not in the mixin.
- `src/popoto/fields/shortcuts.py:790` — `AutoKeyField` — **still holds** (class
  at :790, extends `UniqueKeyField` with `null=False` forced at :786-790).
- `src/popoto/fields/confidence_field.py:292` — `ConfidenceField.on_save` — **still
  holds**, and confirms the `HSETNX` seeding of `initial_confidence` at :351-358.
- `src/popoto/models/base.py:2863` — `rebuild_indexes()` — **still holds.**
- `src/popoto/models/migrations.py` — **still holds.** 1092 lines, entirely one
  module docstring. Zero `def`, zero `class`, zero `__all__`. Prose only.

**Cited sibling issues/PRs re-checked:** none cited in the issue body.

**Commits on main since issue was filed (touching referenced files):** none.
Issue was filed the same day at 05:17Z; `git log --since` over `base.py` and
`field.py` returns nothing.

**Active plans in `docs/plans/` overlapping this area:** none. The nearest
neighbors (`update_kitchen_sink_v144.md`, `write_filter_mixin.md`,
`subconscious_memory_integration_tests.md`) touch the same field classes but none
touches serialization, export, or transfer.

## Prior Art

`gh issue list --state all --search "export import backup serialize dump migrate"`
and `gh pr list --state merged --search "export import serialization"` return
**no relevant prior work**. #554 is the only hit and is the spec for this work.
This is greenfield; there is no "Why Previous Fixes Failed" section because there
were no previous fixes.

Adjacent machinery to reuse rather than duplicate:

- **`Model.to_dict()`** (base.py:2300) — field-value extraction with
  `include`/`exclude`, relationship flattening to `redis_key` strings, and
  circular-reference detection via `_seen`. The export side builds on this.
  **Gap found:** it iterates `self._meta.explicit_fields`, so an implicit
  `_auto_key` is **not** in its output. Export must add implicit key fields
  explicitly — see Technical Approach.
- **`Model.bulk_create()`** (base.py:2579) — pipelined batch save. Deliberately
  **not** used by the importer; see the write-gate decision.
- **`Query.get_many_objects()`** (query.py:2832) — the only pipelined bulk-read
  entry point; export chunks through it.
- **`TYPE_ENCODER_DECODERS`** (encoding.py:135) — existing per-type encoder
  registry for `Decimal`, `tuple`, `set`, `datetime`, `date`, `time`, and
  (optionally) `DataFrame`. Every encoder already emits a JSON-primitive tagged
  dict. Reused as the export format's coercion hook.
- **`models/migrations.py`** — prose only. Its expand-migrate-contract framing
  and crash-safety expectations carry over to import; its code does not exist.

## Research

External research was skipped as not applicable. The work introduces no new
libraries, calls no external APIs, and implements no ecosystem-standard protocol —
the format is stdlib JSON over Popoto's own encoder registry, and every design
constraint comes from this codebase's internals. Nothing in a search result would
change the technical approach.

Per Phase 0.7: "No relevant external findings — proceeding with codebase context
and training data."

## Spike Results

Four read-only spikes ran in parallel against the worktree at `15b76e7`. All
four returned high-confidence findings that materially shaped the design.

### spike-1: Write-gate mechanics
- **Assumption**: "`save()` returns `False` on a write-gate rejection, and the
  importer can detect a rejection from the return value."
- **Method**: code-read
- **Finding**: Partly true, and the naive version is a trap.
  `Model.save()` (base.py:1049) returns `Union[Pipeline, int, bool]`. The gate at
  base.py:1148-1153 catches `SkipSaveException` and returns `pipeline if pipeline
  else False`. But the *success* path returns `db_response`, the **HSET reply
  count** (base.py:1576) — and `HSET` returns the number of *new* fields, which is
  **`0` when overwriting a record whose fields all already exist**. A truthiness
  test on the return value therefore misclassifies a successful overwrite as a
  rejection. There is no `force=` / `skip_write_filter=` kwarg anywhere;
  `SkipSaveException` is defined once (exceptions.py:53) and only
  `WriteFilterMixin` raises it. `access_tracker.py`, `existence_filter.py`, and
  `confidence_field.py` do **not** gate saves.
- **Confidence**: high
- **Impact on plan**: (a) rejection is detected as `result is False or result is
  None`, never by truthiness; (b) reconciliation is additionally ground-truthed by
  an `EXISTS` check per batch rather than trusting return values at all; (c) a new
  `skip_write_filter: bool = False` kwarg must be added to `Model.save()` at
  base.py:1148 to make bypass possible.

### spike-2: Per-field hidden-state inventory
- **Assumption**: "Only ConfidenceField, the decay fields, AutoKeyField and
  EmbeddingField carry state a plain value cannot capture."
- **Method**: code-read (all 16 `on_save` definitions plus the six no-`on_save`
  mixins)
- **Finding**: Broadly confirmed, with four corrections that change the design.
  1. **`AutoKeyField` needs no carrier** — `Model.__init__` lets kwargs override
     the generated default (base.py:542-544), so `Model(_auto_key=<32-char>)`
     persists under that key. But `AutoFieldMixin.is_valid` (auto_field_mixin.py:203-237)
     enforces an exact length per strategy (uuid4:32, ulid:26, ksuid:27, or
     `auto_uuid_length`), and a bad length raises `ModelException` from
     `__init__`, before `save()`. Key restore is free; key *validation* failures
     surface as construction errors the importer must catch and report.
  2. **`DecayingSortedField` needs no carrier either** — the decay reference
     timestamp *is* the field value and `to_dict()` captures it. It is destroyed
     by `SortedFieldMixin.format_value_pre_save` (sorted_field_mixin.py:299,
     :325-330) stamping `time.time()`, because `DecayingSortedField.__init__`
     forces `auto_now=True` (decaying_sorted_field.py:236). The existing
     `save(skip_auto_now=True)` kwarg (base.py:1053) preserves it. This is a
     **driver-level flag, not a field-specific branch** — exactly what the
     "generic driver" constraint requires.
  3. **`CyclicDecayField` is worse than the issue describes.** Two hashes:
     `...:cycles` (cyclic_decay_field.py:280, msgpack `[[period, amplitude,
     phase]]`) and `...:pressure` (:291, msgpack `{rate, last_resolved}`).
     `on_save` **unconditionally overwrites cycles with the class defaults**
     (:356-361), wiping per-member amplitudes that `strengthen_cycle` /
     `weaken_cycle` (base.py:2154, :2174) accumulated. That is a live in-place
     bug on any ordinary save, not just an export gap. `pressure.last_resolved`
     *is* preserved by `on_save` when the entry exists (:363-379) but seeds to
     `now` on a fresh key, discharging accumulated urgency on import.
  4. **`EmbeddingField` stores no vector in Redis at all** — the Redis field
     holds only the dimension count (embedding_field.py:561-575); the vector is a
     `.npy` file at `sha256(redis_key)` (:421-434) with an `_index.json` sidecar.
     **There is no provider or model-identity marker anywhere** — only
     `len(embedding)` is checked against `provider.dimensions` (:516). The
     silent-wrong-vector-space corruption the issue fears is real and currently
     undetectable.

  Fully derivable (no carrier needed, `on_save` rebuilds exactly): `KeyFieldMixin`,
  `IndexedFieldMixin`, `SortedFieldMixin`, `GeoField`, `Relationship`, `TagField`,
  capped `ListField`, `BM25Field` (the Lua at bm25_field.py:66-85 strips the doc's
  old terms first, so re-save is idempotent), `ContentField`, `DataFrameField`,
  and the `WriteFilterMixin` priority ZSET.

  Carriers needed: `ConfidenceField` (`{confidence, evidence_count,
  corroborations, contradictions}` in `$ConfidencF:{Model}:{field}:data`, with
  `evidence_count`/`corroborations`/`contradictions` existing nowhere else),
  `CyclicDecayField` (cycles + pressure), `EmbeddingField` (vector + provenance),
  `AccessTrackerMixin` (`access_count`, `last_accessed` from `$AT:{C}:meta:{key}`).

  Honestly-not-carryable: `EventStreamMixin`, `PredictionLedgerMixin`,
  `CoOccurrenceField`, the AccessTracker access *log*, and `FrequencySketch` /
  `ExistenceFilter` counters (a function of save *history*, with `on_delete` a
  documented no-op).
- **Confidence**: high
- **Impact on plan**: produced the `roundtrip_policy` taxonomy and the exact
  carrier list; moved AutoKeyField and DecayingSortedField out of the protocol
  entirely (driver flags handle them); made the EmbeddingField provenance marker a
  *new* artifact this plan must invent rather than read; filed #556 for the
  history-shaped subsystems.

### spike-3: Query and iteration API
- **Assumption**: "Filtering can be expressed through the existing query API and
  the applied filter can be rendered into the report."
- **Method**: code-read
- **Finding**: Confirmed, with a scaling caveat and one important semantic.
  `Query.filter(*args, **kwargs)` (query.py:2232) accepts `Q` objects and
  `Expression` objects in `*args`; `Q` is a plain tree (`filters`, `connector`,
  `children`, `negated`, q.py:90-93) and **`Q.__repr__` already renders
  `(Q(status='active') OR ~Q(rating__lt=2.0))`** (q.py:159-171) — a ready-made
  provenance string. Unknown filter params raise `QueryException`
  (query.py:2158-2162), but a *known* field with no index is silently downgraded
  to a client-side equality filter (`_pending_client_filters`, query.py:2143-2166)
  that forces loading every key (query.py:2168-2172). Scaling: `Query.keys()` is
  a single blocking `SMEMBERS` with **no batching and no default limit**
  (query.py:1841-1845); `get_many_objects` pipelines every `HGETALL` in one shot
  with no internal chunking. `Query.count()` is `SCARD` when unfiltered
  (query.py:2810-2814) but hydrates everything when Q objects are present
  (query.py:1480-1482). Serialization is msgpack via encoding.py; there is no
  `to_json`, but every `TYPE_ENCODER_DECODERS` encoder emits a JSON-primitive
  tagged dict.
- **Confidence**: high
- **Impact on plan**: filter provenance uses `Q.__repr__` and the builder's
  `_filters` / `_q_objects` (never `QueryBuilder.__repr__`, which drops Q objects,
  query.py:1539); export chunks the key list itself before calling
  `get_many_objects`; the report warns when a client-side filter was used; the
  export format is JSON Lines with `TYPE_ENCODER_DECODERS` as the coercion hook.

### spike-4: Prior art and API conventions
- **Assumption**: "There is a natural home for this API and a test model that
  already stacks the hard field types."
- **Method**: code-read + `gh` search
- **Finding**: No prior art. `src/popoto/__init__.py` is a flat re-export with an
  explicit `__all__` (:155-226), optional deps guarded by `try/except ImportError`
  (:55-63), and module-level functions (`get_redis` :89, `configure` :109) — so
  both a sub-package and top-level functions are idiomatic. `recipes/`, `stores/`,
  `streams/` establish the sub-package precedent. **No `[project.scripts]` exists
  in `pyproject.toml`** — a CLI would be a new packaging precedent. `Model`
  classmethod convention is verb_noun with a one-for-one `async_` twin
  (`bulk_create` :2579 / `async_bulk_create` :2806). **No "writing custom fields"
  doc exists** anywhere in `docs/`. **No existing test model stacks all seven
  required field types** — `tests/test_kitchen_sink.py` is a legacy bare-assert
  script with only key/sorted/geo fields; the closest analogue is
  `tests/benchmarks/scenarios/external_base.py`.
- **Confidence**: high
- **Impact on plan**: API lands as `popoto/transfer/` + thin `Model` delegates,
  CLI deferred to #555; AC #2 needs a brand-new fixture model; AC #10 needs a
  brand-new `docs/field-authoring.md`.

## Data Flow

**Export**

1. **Entry point** — `Model.export_records(*q_objects, **filters)` (or
   `popoto.transfer.export_records(Model, ...)`), optionally with `stream=` a
   file-like object.
2. **Query resolution** — filter args forward verbatim to `Model.query.filter(...)`;
   with no args, `Model.query` is used unfiltered. The applied filter is captured
   as a provenance string from `Q.__repr__` plus the plain-kwarg dict *before*
   evaluation, so it is reported even when the match count is zero.
3. **Key enumeration** — the builder resolves to a key set. Export chunks that set
   (default 500) and feeds each chunk to `Query.get_many_objects`, so peak memory
   is one chunk of hydrated instances, not the whole model.
4. **Per-record extraction** — for each instance: `to_dict()` for explicit field
   values, **plus** implicit key fields that `to_dict()` omits, plus the record's
   `redis_key`. Then, for each field in `_meta.fields`, call
   `field.export_state(instance, field_name, value)`. Non-`None` returns land in
   the record's `state` sub-object keyed by field name.
5. **Encoding** — values are coerced to JSON via `TYPE_ENCODER_DECODERS` encoders;
   binary payloads (embedding `.npy` bytes) are base64 strings inside the JSON.
6. **Output** — a manifest line followed by one JSON object per record, written to
   the stream. `ExportResult` (counts, filter provenance, warnings) is returned.

**Import**

1. **Entry point** — `Model.import_records(stream, ...)` with `on_conflict`,
   `on_write_gate`, `on_embedding_mismatch`. Keys are always preserved in v1.
2. **Manifest validation** — format version, model name, and per-field
   `roundtrip_policy` are checked against the destination model. Embedding
   provenance is compared against the destination's configured provider.
   Mismatches resolve per `on_embedding_mismatch`.
3. **Per-batch conflict check** — a pipelined `EXISTS` over the batch's target
   redis_keys classifies each record as new or colliding; collisions resolve per
   `on_conflict` before any write.
4. **Construction** — `Model(**values)` including the preserved key. A
   `ModelException` here (e.g. a key that fails length validation) becomes a
   counted rejection, not a crash.
5. **Save** — `instance.save(skip_auto_now=True, skip_write_filter=<bypass>)`,
   one record at a time with no external pipeline, so each record's outcome is
   individually observable. `on_save` rebuilds every derived structure for free.
6. **State restore** — *after* the save (companion hashes are keyed by
   `redis_key` and `on_save` seeds/clobbers them), each field with carried state
   gets `field.import_state(instance, field_name, state)`.
7. **Reconciliation** — a pipelined `EXISTS` re-check over the batch confirms
   ground truth. Every exported record is accounted for as landed, skipped,
   rejected, or errored.
8. **Output** — `ImportReport` with per-category counts, a reason per rejection,
   and the per-field `roundtrip_policy` roll-up ("BM25Field: rebuilt on import";
   "EventStreamMixin: history not carried, see #556").

## Architectural Impact

- **New dependencies**: none. JSON Lines uses stdlib `json`; base64 uses stdlib
  `base64`; the embedding carrier uses `numpy`, which is already an optional
  dependency gated behind `EmbeddingField`'s own availability (`__init__.py:55-63`),
  so it adds nothing to the required set.
- **Interface changes**: one additive `Model.save()` kwarg is the only change to
  an existing signature — `skip_write_filter: bool = False` (`skip_auto_now`
  already exists). Note that `save()` already accepts `**kwargs` (base.py:1049-1057),
  so a misspelled `skip_write_filter=` at a call site is silently swallowed rather
  than raising — the driver must spell it correctly and the docstring must say so.
  Two new optional classmethods and two class attributes on `Field`, all with
  working defaults. New `popoto.transfer` sub-package. New
  `Model.export_records` / `Model.import_records` classmethods. Nothing existing
  changes behavior when the new kwargs are left at their defaults.
- **Coupling**: *decreases* relative to any alternative. The driver holds zero
  knowledge of concrete field types; field-specific knowledge lives on the field
  class, mirroring `on_save`. Adding a field type requires no driver change.
- **Data ownership**: unchanged. The transfer layer reads and writes through
  existing field APIs and, where none exists (ConfidenceField's data hash,
  CyclicDecayField's cycles hash), through the field's own public key-builder
  methods (`get_data_hash_key` at confidence_field.py:208, `get_cycles_hash_key`) —
  so the field still owns its key layout.
- **Reversibility**: high. The sub-package is deletable; the `Field` hooks are
  no-op defaults; the `save()` kwarg defaults to today's behavior.

## Appetite

**Size:** Large

**Team:** Solo dev (Dev agent) fanning out to builder subagents, plus PM, plus
code reviewer.

**Interactions:**
- PM check-ins: 2-3 (the five open questions are decided in this plan, but the
  write-gate and embedding-carry decisions are policy calls the PM may overturn)
- Review rounds: 2 (plan critique, then PR review)

The coding is mechanical once the protocol shape is fixed. The cost is in
alignment on five policy defaults and in the breadth of the test matrix — a
seven-field stacked fixture model against live Redis.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli -n 15 ping` | Live round-trip tests (repo policy: no mocks) |
| Editable install resolves to this worktree | `python -c "import popoto,os;assert os.path.realpath(popoto.__file__).startswith(os.path.realpath('.')),popoto.__file__"` | CLAUDE.md worktree gotcha 1 — wrong package under test |
| Full extras installed | `python -c "import numpy, sentence_transformers"` | CLAUDE.md worktree gotcha 2 — `.[dev]` alone deselects ~95 tests including every embedding test |

**Critique found 2 of 3 currently FAILING** and these are hard gates on Task 4,
not advisory. The ambient interpreter resolves `popoto` to
`/Users/valorengels/src/ai/.venv/.../popoto` — a different checkout entirely —
and `sentence_transformers` is absent, which would deselect every embedding test,
i.e. AC #2's hardest field. **First action of the build stage, before any test is
run:** create a worktree-local venv and `pip install -e '.[dev,embeddings,benchmark]'`
(not `dataframe` — it pulls pandas, which breaks `test_dataframe_field.py`
collection on 3.x). No test count is reported to the PM without stating the
environment alongside it.

## Solution

### Key Elements

- **Round-trip protocol, applied at two levels** — two optional classmethods
  (`export_state`, `import_state`) and two class attributes (`roundtrip_policy`,
  `roundtrip_note`), declared on `Field` **and** honored on Model-level mixins.
  All defaults are working no-ops, so every existing and third-party `Field`
  subclass round-trips correctly with zero changes. This is the single mechanism
  that keeps the driver generic. See "Two levels, one protocol" below — this is
  the correction that came out of critique.
- **`popoto.transfer` driver** — `export_records` / `import_records` plus the
  `ExportResult` and `ImportReport` result types. Iterates `_meta.fields` for
  field-level state and walks `type(instance).__mro__` for model-level state.
  Contains no `isinstance` check against any concrete field or mixin type; both
  passes are duck-typed on the presence of the protocol members.
- **JSON Lines export format** — a manifest line plus one record per line.
  Streamable, diffable, stdlib-only, and human-inspectable.
- **Reconciliation ledger** — every exported record is accounted for as landed,
  skipped, rejected, or errored, with a reason string per non-landed record, and
  ground-truthed against Redis rather than against `save()` return values.
- **Four carriers** — three field-level (`ConfidenceField`, `CyclicDecayField`,
  `EmbeddingField`) and one model-level (`AccessTrackerMixin`). Everything else
  declares its policy and emits nothing.
- **Two driver-level fidelity flags** — `save(skip_auto_now=True)` preserves decay
  clocks and `auto_now` timestamps; preserved keys are passed to the constructor.
  Both are generic; neither is a per-field branch.

### Flow

Source Redis → `Model.export_records(project_key="ai")` → **records.jsonl**
(manifest + N lines) → transfer the file → `Model.import_records(open(...))` →
per-record save + state restore on destination Redis → **`ImportReport`** printed:
`1000 exported / 994 landed / 4 skipped (key exists) / 2 rejected (write gate:
score 0.31 < 0.5)`, with a per-field fidelity roll-up.

### Technical Approach

**1. The protocol (`src/popoto/fields/field.py`)**

Mirrors `on_save`'s existing shape (classmethod, takes the instance and field
name, resolves its own config via `model_instance._meta.fields[field_name]`):

- `roundtrip_policy: str = "rebuild"` — class attribute, one of `"rebuild"`
  (auxiliary state is fully reconstructed by `on_save`; emit nothing),
  `"carry"` (emits explicit state that import restores), `"partial"` (some state
  is carried or rebuilt and some is knowingly not — `roundtrip_note` explains).
- `roundtrip_note: str | None = None` — human-readable text surfaced in the
  import report for `"partial"` fields.
- `export_state(cls, model_instance, field_name, field_value, **kwargs) -> dict | None`
  — base returns `None`.
- `import_state(cls, model_instance, field_name, state, **kwargs) -> None`
  — base is a no-op.
The base defaults are what make AC #3 (an out-of-tree `Field` subclass with no
round-trip support) pass with no code.

**1b. Two levels, one protocol** *(added after critique — BLOCKER 1)*

Critique caught a real gap: four of the things this feature must account for are
**not `Field` subclasses**. `AccessTrackerMixin` (access_tracker.py:55),
`EventStreamMixin` (event_stream.py:61), `PredictionLedgerMixin`
(prediction_ledger.py:90), and `WriteFilterMixin` (write_filter.py:44) are plain
Model-level mixins. `_meta.fields` is `{**explicit_fields, **hidden_fields}`
(base.py:213) and contains only `Field` instances, so a driver that iterates
`_meta.fields` never reaches them — yet the plan listed `AccessTrackerMixin` as a
carrier and promised a report line for `EventStreamMixin`. (`ExistenceFilter`,
`FrequencySketch`, and `CoOccurrenceField` *are* `Field` subclasses and were
never affected.)

Resolution: keep one protocol, apply it at two levels.

- **Field level** — the driver iterates `_meta.fields` and calls
  `field.export_state(...)`. State is keyed by field name.
- **Model level** — the driver walks `type(instance).__mro__` and, for any class
  that defines `export_state` **as its own attribute** (`"export_state" in
  cls.__dict__`), calls it with the same signature minus `field_name`. State is
  keyed by the mixin's class name. The `Model` base itself defines no
  `export_state`, so a model with no mixins yields nothing.

The MRO walk is duck-typed on `cls.__dict__`, not on `isinstance` against a named
class, so the generic-driver constraint holds and the plan's own anti-criterion
still passes. It also means a third-party Model mixin with independent Redis
state can participate with no driver change — the same property the field-level
protocol gives field authors.

Scope consequence: `AccessTrackerMixin` is a genuine model-level carrier
(`roundtrip_policy = "carry"` for `access_count` / `last_accessed`, `"partial"`
for the uncarried access log). `EventStreamMixin` and `PredictionLedgerMixin`
declare `"partial"` with a `roundtrip_note` citing #556 and emit nothing — which
is now actually reachable and actually prints. `WriteFilterMixin` declares
`"rebuild"`: its priority ZSET is recomputed by `_tag_priority` on every save
(write_filter.py:139-165).

Risk 3's enforcement test is widened accordingly: it enumerates every `Field`
subclass in `src/popoto/fields/` **and** every Model-level mixin in the same
package (a class that is not a `Field` subclass and defines `on_save`,
`_check_write_filter`, or any `$`-prefixed key builder), asserting each declares
an explicit `roundtrip_policy`.

**2. The driver (`src/popoto/transfer/`)**

`__init__.py`, `export.py`, `import_.py`, `format.py`, `results.py`. Re-exported
from `popoto/__init__.py` and its `__all__` per the flat convention. `Model` gets
thin delegates `export_records` / `import_records` (named to avoid the `import`
keyword) following the existing verb_noun classmethod style. Async twins are out
of scope for v1 (see No-Gos).

**3. Format**

```
{"popoto_export": 1, "model": "Memory", "exported_at": "...", "popoto_version": "...",
 "filter": "Q(project_key='ai')" | null, "matched_count": 1000,
 "fields": {"certainty": {"class": "ConfidenceField", "policy": "carry"}, ...},
 "embedding_provenance": {"vector": {"provider": "...", "model": "...", "dimensions": 384}}}
{"key": "Memory:abc...", "values": {...}, "state": {"certainty": {...}}}
```

The manifest carries `filter` and `matched_count` separately, which is what makes
AC #7 work: a filter that matched nothing is `{"filter": "Q(x='y')",
"matched_count": 0}` while an empty model is `{"filter": null, "matched_count": 0}`.

**4. Implicit key fields**

`to_dict()` iterates `_meta.explicit_fields`, so a model relying on the
auto-generated `_auto_key` produces a dict that cannot reconstruct its own key.
Export takes `to_dict()` and adds every name in `_meta.fields` that is a key field
and absent from the dict. `Model._get_auto_key_field_name()` (base.py:3003)
already exists for this.

**5. Rejection detection and its precedence rule** *(precedence added after
critique — BLOCKER 2)*

Never truthiness. `result is False or result is None` → rejected (spike-1: HSET
returns `0` on a pure overwrite). This is why the importer does **not** use
`bulk_create` — an external pipeline makes `save()` return the pipeline for every
record and destroys per-record observability. Throughput loss is accepted; AC #4
makes reconciliation a hard requirement.

The plan originally named two sources of truth — the `save()` return value and a
batch `EXISTS` — without saying which wins, and critique found a case where they
disagree and the wrong one is louder. Under `on_conflict="overwrite"` into a
destination that already holds the key, a gate rejection returns `False` *before
any HSET* (base.py:1146-1153), leaving the **old** hash intact. A post-write
`EXISTS` returns 1, and a naive reconciliation counts the record as landed while
none of the imported values were written — the exact "1000 imported, 600 landed"
failure this feature exists to eliminate, reintroduced at the reconciliation step.

**The rule: the per-record `save()` return value is authoritative for
classification. `EXISTS` is a corroborating check that may only downgrade
landed → missing. It may never upgrade rejected → landed.**

Concretely: classify from the return value first, then run the batch `EXISTS`
over **only** the records already classified as landed, to catch a write that
vanished. For a record on the collision path, `EXISTS` was already true at the
pre-write conflict check, so its post-write value carries no information and is
never consulted. A dedicated test covers `on_conflict="overwrite"` combined with
a write-gate rejection and asserts the record is reported as rejected, not landed.

**5b. Outcome categories**

Five, not four *(fifth added after critique — CONCERN)*. State restore runs after
`save()`, so a raise inside `import_state` leaves the primary hash and every
`on_save`-rebuilt structure already written — including `ConfidenceField`'s
`HSETNX`-seeded `initial_confidence` and `CyclicDecayField`'s clobbered cycles.
Reporting that as `errored` would imply nothing landed, when in fact a queryable
record now exists with degraded auxiliary state.

| Category | Meaning |
|---|---|
| `landed` | Saved and all carried state restored. |
| `skipped` | Key already present, `on_conflict="skip"`. |
| `rejected` | Refused before any write — write gate, or construction/validation failure. Nothing written. |
| `errored` | Failed during save. Nothing written, or write state indeterminate. |
| `partial` | Saved, but state restore raised. **The record exists on the destination with rebuild-default auxiliary state.** |

The per-record loop uses two separate `try` blocks — one around construction and
`save()`, one around `import_state` — so the handler knows which stage failed. A
single `try` around both cannot distinguish `rejected`/`errored` from `partial`.
`partial` is surfaced prominently in `ImportReport.summary()` and documented in
the user guide as "needs attention", since it is the one category that leaves
degraded data behind.

**6. New `save()` kwarg**

`skip_write_filter: bool = False`, guarding only base.py:1148-1153. Default
preserves today's behavior exactly.

**7. EmbeddingField provenance**

No provider/model marker exists today, so export creates one: at export time the
field reads its configured provider and records `{provider, model, dimensions}`
into the manifest. Import compares against the destination's provider. This
marker is new metadata in the export file only — it does not change what
`EmbeddingField` stores in Redis, keeping the change additive.

### The five open questions — decided

**Q1. Write gates: bypass, honor, or caller choice? → Honor by default, bypass
available, always reported.** `on_write_gate: Literal["reject", "bypass"] =
"reject"`.

*Rationale.* The gate is destination policy. Silently writing around it turns
import into a way to plant records the destination model would refuse, which is
a durable correctness hazard, not a one-time inconvenience. The failure the issue
actually names — "reports 1000 imported when only 600 landed" — is a *reporting*
failure, and it is fixed by the reconciliation ledger, not by bypassing.
Restoring a faithful backup into a model whose threshold has since risen is a
legitimate need, so `"bypass"` exists; it is a deliberate keystroke, and the
report states how many records used it. Note the limit honestly in the docs:
`"bypass"` only disables Popoto's own `WriteFilterMixin` gate. An application's
own `save()` override returning falsy cannot be bypassed from the library, and
those records appear as rejections with reason `save() returned falsy`.

**Q2. Key preservation: default or opt-in? → Default `preserve_keys=True`.**

*Rationale.* Every reason to move records — migrate a machine, seed staging,
resume an interrupted import — assumes the record is the *same* record on the
other side. Regenerating keys breaks `Relationship` values and every
application-level pointer stored as a plain string, and it makes re-running an
import produce duplicates instead of converging. Preservation is also what makes
`on_conflict="overwrite"` an idempotent resume. The overwrite hazard the question
raises is real but is the *merge semantic's* job (Q4), not a reason to corrupt
identity. Spike-1 confirmed preservation works with no new machinery:
`Model(_auto_key=...)` overrides the generated default (base.py:542-544).
*Scope, revised after critique.* `preserve_keys=True` is the **only** mode in
v1; the key-regenerating opt-out is filed as #557. Critique correctly noted that
the plan was building the opt-out while its own Open Questions section said "no
identified consumer wants it" — the same rationale used to confidently defer the
CLI. Applying that standard consistently removes the `remap_references` hook,
one protocol member, one partial guarantee, and one whole failure mode from the
v1 surface. `preserve_keys` is not exposed as a parameter at all; if a caller
needs regeneration, #557 adds it against a settled API.

**Q3. Carry EmbeddingField vectors by default? → Yes, carry by default, guarded
by a provider fingerprint that errors on mismatch.**
`on_embedding_mismatch: Literal["error", "carry", "regenerate"] = "error"`.

*Rationale.* Carrying is exact, fast, and the only option that works at all for
records whose source text is absent or has since changed, or where
`auto_embed=False` (spike-2: with `auto_embed=False` the field persists a
dimension count with no vector, so regeneration produces nothing). The corruption
the question fears is not caused by carrying — it is caused by carrying
*blind*. The fix is a fingerprint, not a policy retreat: export records
`{provider, model, dimensions}`, import compares, and a mismatch is a hard error
by default rather than a silent landing. That converts the issue's "corruption
with no error and no easy detection" into a loud, cheap check. `"regenerate"`
is the escape hatch for a deliberate re-embed under a new model. Cost: the
fingerprint is new metadata this plan must invent, since none exists today.

**Q4. Merge semantic on key collision? → `on_conflict: Literal["error", "skip",
"overwrite"] = "error"`.**

*Rationale.* AC #8 requires that import "does not silently clobber", and the only
default that cannot clobber is one that refuses. For the primary use case — a
fresh destination — there are no collisions, so the strict default costs nothing.
For the two real collision cases the caller states intent: `"overwrite"` for an
idempotent re-run or resume (safe precisely because keys are preserved), `"skip"`
for a merge that must not disturb existing records. Every conflict is itemized in
the report regardless of mode, so `"skip"` never hides how much was skipped.
Detection is a pipelined `EXISTS` per batch before any write. Import is not
atomic across records; the recovery path is `"overwrite"` on re-run, which the
docs give as a recipe.

**Q5. API surface? → `popoto.transfer` sub-package with thin `Model` classmethod
delegates. No CLI in v1 (filed as #555).**

*Rationale.* The driver is model-generic and owns its own result types
(`ExportResult`, `ImportReport`) and format module; hanging that off `Model`
would bloat an already 3600-line `base.py`. `recipes/`, `stores/`, and `streams/`
establish the sub-package precedent, and the flat re-export in `__init__.py`
means discoverability costs nothing. The classmethod delegates exist because
`Model.export_records(...)` is what users will reach for by analogy with
`bulk_create` and `rebuild_indexes`. The CLI is deferred, not dismissed:
`pyproject.toml` has no `[project.scripts]` today, so adding one is a packaging
precedent plus a model-discovery problem, and the motivating consumer runs a
Python script.

## Failure Path Test Strategy

### Exception Handling Coverage
- The only broad handler this work introduces is the per-record guard in the
  import loop. It must catch `ModelException` / `QueryException` / `Exception`
  around construction, save, and state restore, and every catch must convert to a
  **counted `ImportReport` entry carrying the exception text** — never a bare
  `pass`, never a log-only. A test asserts that a record that raises during
  `import_state` appears in the report as `errored` with the message.
- Existing `except Exception: pass` blocks in touched files: none in
  `field.py`/`base.py` within the modified regions. The `save()` change wraps an
  existing `except SkipSaveException` whose observable behavior (return `False`)
  is already asserted by the write-filter tests.

### Empty/Invalid Input Handling
- Empty model (zero records) → valid export with a manifest and no record lines;
  import of that file returns a zero-count report, not an error.
- Filter matching zero records → distinguishable from the above (AC #7); asserted
  as a dedicated test.
- Truncated / malformed JSONL line → counted as `errored` with the line number;
  import continues. A file with no manifest, or a manifest whose `model` does not
  match, raises before writing anything.
- `None` / null field values, empty strings, and absent optional fields round-trip
  as themselves; a null in a `null=False` field surfaces as a construction
  rejection with the validation message.
- A record whose key fails `AutoFieldMixin.is_valid` length checking raises
  `ModelException` from `__init__` (spike-2) — asserted as a counted rejection,
  not a crash.

### Error State Rendering
- `ImportReport` has a `__str__` / `summary()` that renders counts, every
  rejection reason, and the per-field fidelity roll-up. Tested directly: a run
  with one gate rejection, one conflict skip, and one `"partial"` field must
  produce all three in the rendered output.
- `on_conflict="error"` and an embedding-provenance mismatch both raise with a
  message naming the offending key / provider pair. Tested.

## Test Impact

No existing tests are affected. Every change is additive: new optional `Field`
classmethods with no-op defaults, a new `save()` kwarg defaulting to today's
behavior, and a new sub-package. No existing behavior or interface changes.

The two field classes whose `on_save` is *read* by this work
(`ConfidenceField`, `CyclicDecayField`) are not modified in their save paths —
only extended with new protocol methods — so `tests/test_confidence_field.py` and
the cyclic-decay tests keep passing unchanged.

Note for the builder: spike-2 found a **pre-existing bug** — `CyclicDecayField.on_save`
unconditionally overwrites learned cycle amplitudes with class defaults
(cyclic_decay_field.py:356-361). This plan does **not** fix it (see No-Gos); the
importer works around it by restoring cycles *after* save. If a builder is
tempted to fix it in passing, don't — it changes in-place save semantics and
belongs in its own issue.

New test files:
- `tests/test_transfer_roundtrip.py` — plain-field fidelity, out-of-tree field
  subclass, empty model, zero-match filter, format/manifest.
- `tests/test_transfer_fidelity_fields.py` — the seven-field stacked fixture
  model; per-carrier assertions.
- `tests/test_transfer_reconciliation.py` — write-gate rejections, conflict
  modes, error accounting, report rendering.

## Rabbit Holes

- **Building a pipelined bulk-import fast path.** `bulk_create` is right there and
  it is 10x faster. It also makes `save()` return the pipeline for every record,
  which destroys the per-record reconciliation AC #4 demands. Do not chase
  throughput in v1.
- **Fixing the `CyclicDecayField.on_save` amplitude clobber.** It is a genuine
  bug found by spike-2, it is adjacent, and it will be tempting. It changes
  in-place save semantics for every existing user of that field. Out of scope.
- **Designing a general reference-remapping graph.** Keys are always preserved in
  v1, so references never move. Building a full old-key→new-key rewrite across
  arbitrary string fields means heuristically guessing which strings are keys —
  unbounded and unsafe. Deferred wholesale to #557.
- **Making the export format a stable public interchange contract.** Versioning
  the manifest is in scope; promising cross-version compatibility, writing a
  formal schema, or supporting import of a future version's file is not.
- **Cross-model / whole-database export.** One model per file. Multi-model
  orchestration is a caller's loop.
- **`msgpack` instead of JSON for the format.** It is already a dependency and it
  is tempting for the binary embedding payloads. It is not streamable for hand
  inspection, and an operator debugging a failed migration needs to read the file.

## Risks

### Risk 1: Export memory blows up on large models
**Impact:** `Query.keys()` is a single unbatched `SMEMBERS` and `get_many_objects`
pipelines every `HGETALL` at once (spike-3). A million-record model would hydrate
entirely in memory before a byte is written.
**Mitigation:** Export chunks the key list itself (default 500) and writes each
chunk to the stream before hydrating the next, so peak memory is one chunk. The
key set itself is still one `SMEMBERS` — documented as a known ceiling, not
solved in v1.

### Risk 2: The embedding provenance marker is invented, not observed
**Impact:** Export must read `{provider, model, dimensions}` off the configured
provider object. If a deployment has no provider configured (`auto_embed=False`),
there is nothing to read and the fingerprint is `unknown`.
**Mitigation:** `unknown` provenance is carried and import errors by default,
requiring explicit `on_embedding_mismatch="carry"`. An operator who knows both
sides run the same model types one flag; nobody gets a silent wrong vector space.

### Risk 3: `roundtrip_policy` declarations drift out of true
**Impact:** A field declares `"rebuild"` and later grows independent state; the
report then lies confidently, which is the exact failure mode this issue exists
to eliminate.
**Mitigation:** A test enumerates every `Field` subclass in `src/popoto/fields/`
and asserts each has an explicit `roundtrip_policy` declaration (inherited
`"rebuild"` from the base is allowed only for classes with no `on_save` override
of their own). This turns a future omission into a test failure. Documented in
`docs/field-authoring.md` as a field-author obligation.

### Risk 4: Worktree environment produces a confident wrong test number
**Impact:** Per CLAUDE.md, a `.worktrees/` checkout can silently test the wrong
package, deselect ~95 tests for missing extras, or collide with a peer suite on
Redis DB 15 — all five shapes cost a review round on PR #495.
**Mitigation:** The Prerequisites table gates on the editable-install and extras
checks. Test runs are narrow-scoped to the three new files. Any count reported to
the PM states the environment alongside it.

### Risk 5: The strict `on_conflict="error"` default surprises the motivating consumer
**Impact:** A partially-completed import cannot be resumed without passing a flag.
**Mitigation:** Intentional. The error message names the colliding key and states
the two resume recipes (`"overwrite"` / `"skip"`), and the docs carry a
"resuming an interrupted import" section.

## Race Conditions

### Race 1: Source model mutates during export
**Location:** `src/popoto/transfer/export.py`, the chunked key-iteration loop.
**Trigger:** Export resolves the key set once, then hydrates chunks over seconds
or minutes. A concurrent writer deletes a record between key resolution and
hydration, or writes one after key resolution.
**Data prerequisite:** Each key must still resolve to a live hash when its chunk
is hydrated.
**State prerequisite:** None — export is deliberately not a point-in-time
snapshot.
**Mitigation:** A key that no longer resolves is counted as `vanished` in
`ExportResult` and omitted from the file, not an error. Records created after key
resolution are simply absent. The manifest states that the export is not a
consistent snapshot, and `matched_count` is recorded at resolution time so the
gap between it and the written count is visible.

### Race 2: State restore lands between save and a concurrent destination write
**Location:** `src/popoto/transfer/import_.py`, the save-then-`import_state`
sequence.
**Trigger:** `on_save` seeds `ConfidenceField`'s hash via `HSETNX` and clobbers
`CyclicDecayField`'s cycles; `import_state` then overwrites both. A concurrent
writer on the destination touching the same record in that window sees the
seeded-not-yet-restored intermediate state.
**Data prerequisite:** The record's hash must exist before `import_state` writes
its companion hashes, since those are keyed by `redis_key`.
**State prerequisite:** No concurrent writer on the destination for the records
being imported.
**Mitigation:** Documented precondition — import targets a destination not under
concurrent write for the model being imported (the normal migration case). Not
solved with locking in v1; the ordering (save first, restore second) is mandatory
and cannot be inverted, so a window necessarily exists.

### Race 3: Conflict check is not atomic with the write
**Location:** `src/popoto/transfer/import_.py`, batch `EXISTS` then per-record save.
**Trigger:** A record is created on the destination between the batch's `EXISTS`
and that record's `save()`; `on_conflict="error"` fails to fire and the record is
overwritten.
**Data prerequisite:** The `EXISTS` result must still hold at write time.
**State prerequisite:** Same as Race 2 — no concurrent destination writer.
**Mitigation:** Same documented precondition. Deliberately not solved with
`WATCH`/`MULTI`: per-record optimistic locking would cost a round trip per record
for a hazard that only exists under a precondition violation.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #555] CLI front-end (`popoto export ...`). Needs a
  `[project.scripts]` packaging precedent and a model-discovery mechanism, neither
  of which the library API requires; the motivating consumer runs a Python script.
- [SEPARATE-SLUG #556] Carrying history-shaped state: `EventStreamMixin` streams,
  `PredictionLedgerMixin` ledgers, `CoOccurrenceField` association graphs, the
  `AccessTrackerMixin` access log, and `FrequencySketch`/`ExistenceFilter`
  counters. Each declares `roundtrip_policy = "partial"` with a note in the
  report, so nothing is silently lost — it is loudly not carried.
- [SEPARATE-SLUG #556] Fixing `CyclicDecayField.on_save` clobbering learned cycle
  amplitudes (cyclic_decay_field.py:356-361). A real pre-existing bug found by
  spike-2, but fixing it changes in-place save semantics for existing users.
- [SEPARATE-SLUG #557] Key-regenerating import (`preserve_keys=False`) and the
  `remap_references` protocol hook. Cut after critique for consistency with the
  CLI deferral: no identified consumer, and it is the only mode needing a third
  protocol member and carrying a partial guarantee.
- [SEPARATE-SLUG #555] `async_export_records` / `async_import_records` twins. The
  repo's `async_` convention would normally require them, but the whole driver is
  I/O-bound on a per-record save loop and the async path would duplicate every
  reconciliation branch; deferred with the CLI so both land against a settled
  API.
- [SEPARATE-SLUG #572] Full `mypy src/popoto/transfer/` cleanliness. The plan's
  "Types clean: exit code 0" Verification row was never actually satisfied; 2
  genuine Optional-narrowing bugs were fixed (commit `744c3dc`) but ~49 errors
  in waived categories (missing annotations, bare generics) remain, waived by
  maintainer ruling rather than blocking merge:
  https://github.com/tomcounsell/popoto/pull/558#issuecomment-5277221524

Anti-criteria for the code-level No-Gos appear as inverse rows in the
Verification table below.

## Update System

No update-system changes required. This is a library feature with no new
dependency, no config file, no service, and no deployment step. Existing
installations gain the API on upgrade with no migration; models and third-party
`Field` subclasses keep working unchanged because every new hook has a working
default.

## Agent Integration

No agent integration required. `popoto` is a library consumed by application code;
this repo has no bridge, no MCP server, and no `.mcp.json`. The motivating
consumer (`tomcounsell/ai`) will call the Python API from a migration script it
owns.

## Documentation

### Feature Documentation
- [ ] Create `docs/field-authoring.md` — the round-trip protocol for field
      authors: what `roundtrip_policy` means, when to override `export_state` /
      `import_state`, the rule that a field with independent Redis state must
      declare `"carry"` or `"partial"`, and the fact that the same protocol is
      honored on Model-level mixins via the MRO walk. This is
      AC #10 and there is **no existing custom-field-authoring doc** to extend
      (spike-4).
- [ ] Create `docs/guides/export-import.md` — user-facing: exporting with and
      without filters, the four policy flags and their defaults with the rationale
      for each, reading an `ImportReport`, and the "resuming an interrupted
      import" recipe.
- [ ] Add both to the `mkdocs.yml` nav under the "Redis ORM" section
      (mkdocs.yml:68-82).

### External Documentation Site
- [ ] `mkdocs build --strict` passes.
- [ ] `docs/fields.md` gains a pointer to `field-authoring.md`.

### Inline Documentation
- [ ] Docstrings on all five protocol members of `Field`, each stating the
      default behavior and when to override.
- [ ] Docstrings on `export_records` / `import_records` / `ExportResult` /
      `ImportReport` (they surface via mkdocstrings in `docs/reference/`).
- [ ] A comment at the rejection-detection site explaining why truthiness is
      wrong (HSET returns 0 on overwrite) — this is the single most re-derivable
      mistake in the file.

## Success Criteria

- [ ] AC #1 — a plain-field model round-trips with identical field values, and
      `grep -c "isinstance" src/popoto/transfer/*.py` finds no check against a
      concrete field class.
- [ ] AC #2 — a fixture model stacking `AutoKeyField`, `DecayingSortedField`,
      `BM25Field`, `EmbeddingField`, `ConfidenceField`, `ExistenceFilter`, and
      `WriteFilterMixin` round-trips; every field either restores its state or is
      reported with its `roundtrip_policy`.
- [ ] AC #3 — a `Field` subclass defined inside the test file, with no round-trip
      support, exports and imports correctly via the base default.
- [ ] AC #4 — `ImportReport` accounts for every exported record as landed,
      skipped, rejected, errored, or partial, with a reason per non-landed
      record; a write-gate drop appears as a counted rejection.
- [ ] AC #5 — `export_records()` with no arguments exports all records of a model
      that has no grouping field.
- [ ] AC #6 — filtering goes through `Model.query.filter`; an unqueryable
      predicate raises rather than being ignored.
- [ ] AC #7 — a zero-match filter is distinguishable from an empty model in the
      manifest.
- [ ] AC #8 — importing into a populated destination has a tested semantic for
      all three `on_conflict` modes.
- [ ] AC #9 — all round-trip tests run against live Redis, no mocks.
- [ ] AC #10 — `docs/field-authoring.md` documents the protocol.
- [ ] Every `Field` subclass in `src/popoto/fields/` has an explicit
      `roundtrip_policy` (enforced by test).
- [ ] Tests pass (`/do-test`), narrow-scoped to the three new files.
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

- **Builder (protocol)**
  - Name: `protocol-builder`
  - Role: `Field` base-class protocol + `save(skip_write_filter=)` kwarg + the
    `roundtrip_policy` declarations across all field modules
  - Agent Type: builder
  - Resume: true

- **Builder (driver)**
  - Name: `driver-builder`
  - Role: `src/popoto/transfer/` — export, import, format, result types
  - Agent Type: builder
  - Resume: true

- **Builder (carriers)**
  - Name: `carrier-builder`
  - Role: the four `export_state`/`import_state` overrides — three field-level
    (Confidence, CyclicDecay, Embedding) and one model-level (AccessTracker) —
    plus those four modules' own `roundtrip_policy` declarations
  - Agent Type: builder
  - Resume: true

- **Test engineer**
  - Name: `transfer-tester`
  - Role: the three new test files including the seven-field fixture model
  - Agent Type: test-engineer
  - Resume: true

- **Documentarian**
  - Name: `transfer-docs`
  - Role: `docs/field-authoring.md`, `docs/guides/export-import.md`, mkdocs nav
  - Agent Type: documentarian
  - Resume: true

- **Validator**
  - Name: `transfer-validator`
  - Role: verify all ten ACs and the Verification table
  - Agent Type: validator
  - Resume: true

All builders work in the single session worktree on disjoint file sets:
`protocol-builder` owns `fields/field.py`, `models/base.py`, and the
**non-carrier** field/mixin modules; `driver-builder` owns `transfer/*` only;
`carrier-builder` owns exactly `confidence_field.py`, `cyclic_decay_field.py`,
`embedding_field.py`, and `access_tracker.py`. No file is written by two agents —
verified against Tasks 1 and 3 after critique flagged the original split.

## Step by Step Tasks

### 1. Round-trip protocol on `Field`
- **Task ID**: build-protocol
- **Depends On**: none
- **Validates**: tests/test_transfer_roundtrip.py (create)
- **Informed By**: spike-1 (no bypass kwarg exists; gate lives at base.py:1148),
  spike-2 (policy taxonomy and which fields need carriers)
- **Assigned To**: protocol-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `roundtrip_policy`, `roundtrip_note`, `export_state`, `import_state` to
  `Field` in `src/popoto/fields/field.py` with working no-op defaults and full
  docstrings. (No `remap_references` — deferred to #557.)
- Add `skip_write_filter: bool = False` to `Model.save()`, guarding only the
  `_check_write_filter` call at base.py:1148-1153. Default behavior unchanged;
  docstring notes the `**kwargs` swallow hazard.
- Declare an explicit `roundtrip_policy` on the **non-carrier** classes only —
  `"rebuild"` for the derivable set, `"partial"` (with `roundtrip_note` citing
  #556) for `ExistenceFilter`, `FrequencySketch`, `CoOccurrenceField`,
  `EventStreamMixin`, `PredictionLedgerMixin`, and `"rebuild"` for
  `WriteFilterMixin`. **The four carrier modules are Task 3's** — do not edit
  `confidence_field.py`, `cyclic_decay_field.py`, `embedding_field.py`, or
  `access_tracker.py` in this task. (Critique caught that the original split
  broke the disjoint-ownership invariant while Tasks 1 and 2 run in parallel.)

### 2. Transfer driver
- **Task ID**: build-driver
- **Depends On**: none (codes against the protocol signatures fixed in this plan)
- **Validates**: tests/test_transfer_roundtrip.py, tests/test_transfer_reconciliation.py (create)
- **Informed By**: spike-3 (Q.__repr__ provenance, SMEMBERS has no batching,
  unknown params raise / unindexed known params degrade silently), spike-1 (HSET
  returns 0 on overwrite — never use truthiness)
- **Assigned To**: driver-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/transfer/` with `export.py`, `import_.py`, `format.py`,
  `results.py`, `__init__.py`.
- Export: forward filters to `Model.query.filter`, capture provenance from
  `Q.__repr__` + the plain-kwarg dict (never `QueryBuilder.__repr__`), chunk the
  key set at 500 through `Query.get_many_objects`, build each record from
  `to_dict()` **plus implicit key fields**, call `export_state` per field, coerce
  through `TYPE_ENCODER_DECODERS`, write manifest + JSONL.
- Import: validate manifest, batch `EXISTS` conflict check, construct, save with
  `skip_auto_now=True`, call `import_state` **after** save, ground-truth landed
  count with a second `EXISTS`, build `ImportReport`.
- Implement `on_conflict` / `on_write_gate` / `on_embedding_mismatch` per the
  decisions in Technical Approach. Do **not** add a `preserve_keys` parameter
  (#557); keys are always preserved.
- Two passes for carried state: `_meta.fields` for field-level, and an MRO walk
  over `type(instance).__mro__` for model-level mixins, duck-typed on
  `"export_state" in cls.__dict__`. No `isinstance` against a concrete type.
- Rejection detection is `result is False or result is None` with an explanatory
  comment; per-record exceptions become counted report entries, never `pass`.
- Re-export from `popoto/__init__.py` + `__all__`; add `Model.export_records` /
  `Model.import_records` delegates.

### 3. Field carriers
- **Task ID**: build-carriers
- **Depends On**: build-protocol
- **Validates**: tests/test_transfer_fidelity_fields.py (create)
- **Informed By**: spike-2 (exact hash keys, msgpack shapes, and the absence of
  setters for confidence data, cycles, and vectors)
- **Assigned To**: carrier-builder
- **Agent Type**: builder
- **Parallel**: false
- `ConfidenceField`: export/import `{confidence, evidence_count, corroborations,
  contradictions}` per partition via `get_data_hash_key` (confidence_field.py:208).
- `CyclicDecayField`: export/import the `:cycles` amplitudes and `:pressure`
  `{rate, last_resolved}`. Restore runs after save specifically because `on_save`
  clobbers cycles — comment this.
- `EmbeddingField`: export the `.npy` bytes base64-encoded plus
  `{provider, model, dimensions}` provenance; import writes the `.npy` +
  `_index.json` entry and calls `invalidate_cache`.
- `AccessTrackerMixin`: **model-level** carrier reached by the MRO walk, not by
  `_meta.fields`. Export/import `access_count` and `last_accessed` from the meta
  hash; `roundtrip_policy = "partial"` with a note naming the uncarried access
  log.
- Each carrier module also declares its own `roundtrip_policy` /
  `roundtrip_note` in this task (Task 1 deliberately leaves these four files
  alone so ownership stays disjoint).

### 4. Tests
- **Task ID**: build-tests
- **Depends On**: build-driver, build-carriers
- **Validates**: the three new test files
- **Assigned To**: transfer-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Build the seven-field stacked fixture model for AC #2.
- Define an out-of-tree-style `Field` subclass inside the test file for AC #3.
- Cover: plain round-trip, empty model vs zero-match filter, all three
  `on_conflict` modes, both `on_write_gate` modes, embedding provenance mismatch,
  malformed JSONL line accounting, `ImportReport` rendering, and the
  "every field and mixin declares a policy" enforcement test.
- Cover the two critique blockers explicitly: (a) a model-level mixin carrier
  (`AccessTrackerMixin`) round-trips via the MRO walk, and a mixin declaring
  `"partial"` actually appears in the report; (b) `on_conflict="overwrite"` into
  an existing key **plus** a write-gate rejection reports `rejected`, not
  `landed`, and the destination still holds the pre-import values.
- Cover the `partial` outcome category: force `import_state` to raise and assert
  the record is reported as `partial` with the record present but its carried
  state not restored.
- Live Redis only, no mocks. Narrow scope: run only these three files.

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: build-tests
- **Assigned To**: transfer-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/field-authoring.md` and `docs/guides/export-import.md`; wire both
  into `mkdocs.yml` nav; cross-link from `docs/fields.md`.

### 6. Final validation
- **Task ID**: validate-all
- **Depends On**: build-protocol, build-driver, build-carriers, build-tests, document-feature
- **Assigned To**: transfer-validator
- **Agent Type**: validator
- **Parallel**: false
- Run every row of the Verification table and confirm all ten ACs.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Transfer tests pass | `pytest tests/test_transfer_roundtrip.py tests/test_transfer_fidelity_fields.py tests/test_transfer_reconciliation.py -q` | exit code 0 |
| No regression in touched field tests | `pytest tests/test_confidence_field.py tests/test_write_filter.py -q` | exit code 0 |
| Format clean | `black --check src/popoto/transfer/ src/popoto/fields/field.py` | exit code 0 |
| Types clean | `mypy src/popoto/transfer/` | **Waived by maintainer ruling** (https://github.com/tomcounsell/popoto/pull/558#issuecomment-5277221524), not exit code 0. Of the original 53 errors, the 2 genuine Optional-narrowing bugs at `import_.py:350-356` were fixed (commit `744c3dc`); ~49 errors in waived categories (missing annotations, bare generics) remain and are tracked in #572. |
| Docs build | `mkdocs build --strict` | exit code 0 |
| Protocol default is a no-op (AC #3) | `python -c "from popoto.fields.field import Field; assert Field.export_state.__func__(Field, None,'x',1) is None; print('ok')"` | output contains ok |
| Every field and mixin declares a policy | `pytest tests/test_transfer_roundtrip.py -k policy_declared -q` | exit code 0 |
| Precedence rule tested (BLOCKER 2) | `pytest tests/test_transfer_reconciliation.py -k overwrite_gate_reject -q` | exit code 0 |
| Anti-criterion: no new required dependency | `scripts/verify/no_new_deps.sh` | match count == 0 |
| Anti-criterion: generic driver | `scripts/verify/generic_driver.sh` | exit code 1 |
| Anti-criterion: no CLI entry point added (#555) | `grep -c "project.scripts" pyproject.toml` | match count == 0 |
| Anti-criterion: `CyclicDecayField.on_save` body untouched (#556) | `scripts/verify/cyclic_on_save_untouched.sh` | match count == 0 |
| Anti-criterion: no silent swallow in import loop | `scripts/verify/no_silent_swallow.sh` | match count == 0 |
| Anti-criterion: `remap_references` not shipped (#557) | `grep -rc "remap_references" src/popoto/` | match count == 0 |

The five multi-pipe anti-criteria are shell scripts rather than table cells,
because a markdown table cell must escape `|` as `\|` and a builder copy-pasting
the cell verbatim gets a broken command (critique NIT). Each script is one line
and lives in `scripts/verify/`:

```sh
# scripts/verify/generic_driver.sh — no isinstance against a concrete field/mixin type
grep -rn "isinstance" src/popoto/transfer/ | grep -E "Field|Mixin"

# scripts/verify/no_new_deps.sh
git diff main -- pyproject.toml | grep -c '^+.*dependencies'

# scripts/verify/cyclic_on_save_untouched.sh — hunk CONTENT, not --stat
# (--stat prints only a filename and a change bar, so the original check was vacuous)
git diff main -U0 -- src/popoto/fields/cyclic_decay_field.py | grep -c '^[+-].*def on_save'

# scripts/verify/no_silent_swallow.sh
grep -rn -A1 "except.*:" src/popoto/transfer/import_.py | grep -c "pass$"
```

## Critique Results

Run 2026-08-10 at FULL depth (forced by `appetite: Large`), three critics.
Verdict: **NEEDS REVISION** — 2 blockers, 3 concerns, 3 nits. All eight are
addressed below; the plan has been revised and re-committed.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Scope & Value | Protocol is `Field`-only, but `AccessTrackerMixin`, `EventStreamMixin`, `PredictionLedgerMixin`, `WriteFilterMixin` are Model-level mixins the `_meta.fields` loop never reaches — so a listed carrier and a promised report line were unreachable | Technical Approach §1b "Two levels, one protocol" — driver adds an MRO walk keyed by class name | Duck-typed on `"export_state" in cls.__dict__`, never `isinstance`, so the generic-driver anti-criterion still passes. `ExistenceFilter`/`FrequencySketch`/`CoOccurrenceField` are real `Field` subclasses and were never affected |
| BLOCKER | Risk & Robustness | `save()` return value and batch `EXISTS` are two sources of truth with no precedence rule; under `on_conflict="overwrite"` a gate rejection leaves the old hash and `EXISTS` counts it landed | Technical Approach §5 — explicit precedence rule + dedicated test | Return value classifies; `EXISTS` may only downgrade landed→missing, never upgrade rejected→landed. On the collision path the post-write `EXISTS` carries zero information and is not consulted |
| CONCERN | Risk & Robustness | An `import_state` failure leaves a queryable record with rebuild-default state, but "errored" implies nothing landed | Technical Approach §5b — fifth outcome category `partial` | Two separate `try` blocks in the per-record loop; a single `try` cannot distinguish the stages |
| CONCERN | History & Consistency | "No file is written by two agents" was false — Task 1 declared policies on the four carrier modules Task 3 owns | Task 1 scoped to non-carrier classes; Task 3 owns its four modules' declarations; ownership paragraph corrected | Tasks 1 and 2 are `Parallel: true`, so the overlap was a real concurrent-write hazard, not just bookkeeping |
| CONCERN | Scope & Value | `preserve_keys=False` was being built while Open Questions said no consumer wants it — the same rationale used to defer the CLI | Cut from v1, filed as #557; `preserve_keys` is not a parameter at all | Removes `remap_references`, one protocol member, one partial guarantee, one failure mode. `on_conflict`/`on_write_gate` path untouched |
| NIT | History & Consistency | `git diff --stat \| grep "on_save"` is vacuous — `--stat` never prints function names | Replaced with a `-U0` hunk-content check in `scripts/verify/` | The original check could never catch a regression to the #556-deferred fix |
| NIT | History & Consistency | Multi-pipe anti-criteria in table cells are not copy-pasteable (markdown `\|` escaping) | Five anti-criteria moved to one-line scripts in `scripts/verify/` | The commands themselves were correct; only copy-paste safety was at risk |
| NIT | structural | "two additive `Model.save()` kwargs" then lists one; `**kwargs` silently swallows a misspelled kwarg | Architectural Impact reworded; swallow hazard noted for the docstring | `save()` accepts `**kwargs` at base.py:1049-1057, so `skip_write_filtr=True` would be silently ignored |
| FAIL | structural (prereqs) | 2 of 3 prerequisites failing — `popoto` resolves to a different checkout, `sentence_transformers` absent | Prerequisites section now names venv setup as the build stage's first action | Not a plan defect; an environment gate that must clear before any test number is reported |

---

## Open Questions

The issue's five open questions are **decided in Technical Approach with
rationale**, not deferred. Critique resolved the second of the two questions this
plan had left open (`preserve_keys=False` is cut to #557). One remains for the
supervisor, and it is a policy call about the motivating consumer rather than a
design gap:

1. **Is `on_write_gate="reject"` the right default for the motivating consumer?**
   Migrating `tomcounsell/ai`'s `Memory` model between machines will hit the
   `WriteFilterMixin` gate on any record whose score has since fallen below the
   destination's threshold, and those records will be *reported but not
   transferred*. If the intent is a faithful machine-to-machine move, that
   migration should pass `on_write_gate="bypass"` at the call site — confirm that
   is acceptable rather than flipping the library default. Build is not blocked on
   this: the flag exists either way, and only the recommended recipe in
   `docs/guides/export-import.md` changes.
