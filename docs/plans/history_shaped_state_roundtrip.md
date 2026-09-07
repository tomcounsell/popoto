---
status: Planning
type: feature
appetite: Medium
tracking: https://github.com/tomcounsell/popoto/issues/556
---

# Round-tripping history-shaped state: six verdicts

## Problem

#554 shipped the per-field round-trip protocol (`roundtrip_policy` /
`roundtrip_note` / `export_state` / `import_state`, `src/popoto/fields/field.py:216-320`)
and carriers for `ConfidenceField`, `CyclicDecayField`, `EmbeddingField`, and
`AccessTrackerMixin`'s meta counters. It left six structures declared
`"partial"` with a note that ends `"see #556"` — a placeholder, not a contract.
A user reading an import report today cannot tell whether the gap is a bug
Popoto intends to close or the honest limit of what transfer can promise.

This issue closes that ambiguity. For each of the six, decide: is faithful carry
achievable, or is "rebuild from landed records" the honest contract? Then either
implement the carrier or convert the note from a TODO into a documented
permanent limitation.

The issue names the trap itself: replaying history-shaped state through the
normal write path re-runs resolution logic and fabricates timestamps. A carry
that produces a plausible-but-invented history is worse than a documented gap.

## Freshness Check

**Disposition: Unchanged.** Baseline `8151e8c0` (main at plan time).

#556 carries no `## Recon Summary` — it is a maintainer-written follow-up whose
claims are concrete file/key references. Every one was re-verified against the
tree directly rather than taken on trust:

| Issue claim | Verified at | Result |
|---|---|---|
| `EventStreamMixin` declares `"partial"` | `fields/event_stream.py:91-94` | Confirmed |
| `AccessTrackerMixin` declares `"partial"` | `fields/access_tracker.py:88-93` | Confirmed |
| `PredictionLedgerMixin` declares `"partial"` | `fields/prediction_ledger.py:120-124` | Confirmed |
| `CoOccurrenceField` declares `"partial"` | `fields/co_occurrence_field.py:260-264` | Confirmed |
| `ExistenceFilter` / `FrequencySketch` declare `"partial"` | `fields/existence_filter.py:352-356`, `:651-655` | Confirmed (both live in `existence_filter.py`; there is no `frequency_sketch.py`) |
| `$PL:{C}:meta:{pk}` hash + `$PL:{C}:errors:{partition}` ZSET | `prediction_ledger.py:150-184` | Confirmed |
| `$CoOcF:{C}:{field}:{pk}` ZSET | `co_occurrence_field.py:293-311` | Confirmed |
| `$AT:{C}:access_log:{key}` list | `access_tracker.py:145-156` | Confirmed |
| `$FS:{C}:{field}` counters | `existence_filter.py:675-682` | Confirmed |

**One issue claim is revised.** #556 says CoOccurrence "cannot be carried
per-record; it needs a whole-graph pass after all records land." The key format
proves otherwise: `get_edge_key()` (`co_occurrence_field.py:301`) appends the
source pk, so each record owns its own edge ZSET, and symmetric mode writes the
mirror into the *target's* per-pk key (`:387`). The graph is stored as a
per-record partition, not a shared structure. This changes the verdict for that
subsystem from "not carryable" to "carryable per-record, with dangling-edge
semantics to document" — see below.

`#554` is CLOSED, shipped in PR #558; nothing has reopened it. `git log --since`
over the six field files shows no commits since #556 was filed. No active plan
in `docs/plans/` touches `src/popoto/transfer/` or these six fields.

## Research

No external research was run. This is entirely internal: the work is about
Popoto's own Redis key layouts and its own transfer protocol, with no external
library, API, or ecosystem pattern in play. The one externally-grounded fact the
design leans on — that Redis Stream entry IDs are strictly monotonic per key and
`XADD` refuses an ID smaller than the stream's last — is verified below by a
spike against the running server rather than by search.

## Prior Art

- **#554 / PR #558** — the protocol itself. Its carriers set the precedent this
  plan follows: `ConfidenceField.export_state` / `import_state`
  (`fields/confidence_field.py:174-230`) read and write the field's companion
  hash *directly*, bypassing the field's own mutation API. That is the pattern —
  a raw structural restore, not a replay — and it is why the carriers below are
  safe from the fabrication trap.
- **`AccessTrackerMixin.import_state`** (`fields/access_tracker.py:122-143`)
  is the model-level twin of that precedent, and it documents its own reasoning:
  the counters are read-only properties, so it writes the meta hash directly so
  that a later `confirm_access()` continues the carried count rather than
  restarting it.
- **`docs/field-authoring.md:144-165`** already documents the three policy
  values and the rule that `"partial"` requires a note. No fourth value exists.
- **`docs/guides/export-import.md:192-206`** ("What is not carried") is the
  user-facing statement this plan must replace: it currently describes the gap
  as a blanket "Popoto does not attempt to carry these," which after this work
  is true of only three of the six.

No prior attempt to carry any of these six exists, so there is no
"why previous fixes failed" to write.

## Appetite

**Medium.** Three carriers (each ~40 lines of `export_state`/`import_state`
following an established precedent), three note rewrites, one docs page section,
and a fidelity matrix. The design work — which of the six, and why — is the bulk
of the value and is done in this document. If any single carrier turns out to
need a new hook in `popoto/transfer/`, that carrier is cut rather than growing
the appetite; see No-Gos.

## The decision rule

Every verdict below is derived from one rule, applied consistently:

> **Carry a structure when the exported bytes are a *fact about the record* that
> the destination can restore verbatim. Rebuild when the structure is a
> *property of the source deployment's history*, which the destination did not
> live through.**

Three tests fall out of it, and a subsystem must pass all three to be carried:

1. **Per-record decomposable.** The protocol's only hooks are per-record
   (`export.py:185-229`, `import_.py:151-196`). A structure shared across all
   records of a model has no per-record slice, so carrying it would either
   duplicate the whole structure N times or require a whole-model hook that does
   not exist.
2. **Restorable without replay.** There must be a raw write that reproduces the
   stored bytes without re-running derivation, resolution, or decay logic —
   otherwise the restore fabricates timestamps and fires side effects.
3. **Rebuild is not already better.** For a probabilistic structure that
   over-retains, the destination's own writes produce a *more* accurate
   structure than the source's. Carrying would import accumulated error.

## Per-subsystem verdicts

Three carried, three documented as permanent.

### 1. PredictionLedger — **CARRY**

`$PL:{C}:meta:{pk}` is per-record: `_meta_key()` builds it from
`instance.db_key.redis_key` (`prediction_ledger.py:150-164`), and the single
hash field inside is that same redis_key. One record, one hash, one entry. The
value is a msgpack blob of `{predicted, resolved, resolution_mode,
prediction_error, resolved_at, recorded_at}` — a factual record of a prediction
that was made and resolved, not an aggregate.

Test 1 passes. Test 2 passes because `record_prediction` is a plain `HSET`
(`:266`) and the msgpack shape is fully specified, so `export_state` can decode
the blob and `import_state` can re-encode and `HSET` it back **verbatim** —
carrying the original `recorded_at` and `resolved_at` rather than minting new
ones, and never touching `RESOLVE_PREDICTION_LUA`. That is exactly the trap
avoided: replaying through `resolve_prediction()` would recompute
`prediction_error` from the destination's data, stamp `resolved_at = time.time()`,
and fire two side effects (`_apply_confidence_feedback`, `_log_resolution_event`)
that belong to the source. A direct `HSET` fires none of them — and it must not,
because `ConfidenceField` state is already carried independently by #554's
carrier, so re-applying feedback would double-count it.

`$PL:{C}:errors:{partition}` is cross-record, but the *member* is per-record:
`ZADD error_key |prediction_error| member_key` (`:84`). The score is a pure
function of the carried meta entry, so `import_state` recomputes that one
member's score deterministically. That is derivation, not fabrication — the same
input yields the same score on both sides. Test 3 is vacuous here (nothing is
probabilistic).

Policy moves `"partial"` → `"carry"`.

### 2. CoOccurrence — **CARRY**

Per the Freshness Check, the edge graph is stored per-record. Test 1 passes.

Test 2 passes trivially: the structure is a plain ZSET of `{target_pk: weight}`
with no hidden encoding, no counters, no checksums. `export_state` is
`ZRANGE ... WITHSCORES`; `import_state` is `ZADD`. Weights land exactly as
exported, so `strengthen()`'s clamp arithmetic and `weaken_all()`'s decay
multiplication are never replayed. Note this is the *only* honest restore
available: `strengthen(delta=...)` and `weaken_all(factor=...)` accept relative
adjustments, never an absolute target weight, so any replay-based approach would
have to solve for a delta sequence that lands on the exported value — inventing
an interaction history that never happened.

Two normalizations to destination config are required, and both must be
reported rather than silent:

- **`max_edges`** (`co_occurrence_field.py:266`, default 500). A source with a
  larger cap exports more edges than the destination's `LINK_WITH_PRUNE_LUA`
  invariant permits. `import_state` truncates to the destination's cap keeping
  the highest weights — precisely what `ZREMRANGEBYRANK` does on the normal path.
- **`Defaults.CO_OCCURRENCE_WEIGHT_CAP`.** `propagate()` depends on a
  contraction invariant (class docstring, `:231-241`) that requires every stored
  weight ≤ cap. `import_state` clamps. An uncapped carry would make BFS
  propagation non-decaying — a correctness bug, not a fidelity one.

**Dangling edges are the known cost.** A filtered export carries A's edge to B
even when B is outside the filter; B's mirror edge does not land. This is not
fabrication — the edge genuinely existed — and it is the same class of
dangling reference that `Relationship` values already produce under a filtered
export, which the transfer machinery deliberately tolerates because keys are
always preserved. `get_linked(A)` will return a pk with no record;
`propagate()` traverses to an empty key and terminates. Documented, not fixed:
detecting it would need an `EXISTS` per edge (up to 500 per record) or a
whole-import pass that has no hook.

Policy moves `"partial"` → `"carry"`.

### 3. AccessTracker access log — **CARRY (mixin stays `"partial"`)**

`$AT:{C}:access_log:{key}` is per-record and is a list of `str(time.time())`
values capped at `_max_access_log` (`access_tracker.py:77`, default 100). Each
element is a timestamp at which the record was genuinely read on the source.
Copying it is fact-copying: tests 1 and 2 pass, and `import_state` already
writes the sibling meta hash directly (`:142`), so extending it to `DEL` + `RPUSH`
the log is the same move on the same precedent. The log is trimmed to the
*destination's* cap on restore, for the same reason as `max_edges`.

The **staged** list (`$AT:{C}:staged:{key}`) is deliberately still not carried,
and this is where the mixin's `"partial"` policy stays honest. Staged entries
are uncommitted, TTL-bounded reads awaiting `confirm_access()`. Dropping them is
observably identical to calling `discard_staged_access()` — an outcome the
system already supports as a first-class operation. An in-flight, unconfirmed
read not surviving a backup is correct behavior, not a gap.

So the policy stays `"partial"`, but the note stops citing #556 and instead
states the contract: confirmed log carried, in-flight staged reads intentionally
not.

### 4. EventStream — **PERMANENT LIMITATION**

Fails all three tests, and each failure is independently fatal.

**Test 1.** The key is `stream:{name}` or `stream:{name}:{partition}`
(`event_stream.py:109-120`) — one stream shared by every record (or every record
in a partition). There is no per-record slice. A per-record `export_state`
would emit the entire stream once per record and a per-record `import_state`
would write it N times.

**Test 2.** Both writers (`_xadd_mutation:162-201`, `_xadd_event:203-240`) call
`xadd()` with an auto-generated ID; nothing in the file XADDs an explicit ID.
Even if a raw writer were added, Redis stream IDs are strictly monotonic per
key, so entries whose IDs precede the destination stream's last entry are
refused outright — and `MAXLEN~` has already discarded an arbitrary prefix, so
the carried log is a truncated fragment to begin with.

**And the rebuild is semantically right.** The stream is a *mutation log*: it
records what happened to this deployment. Every imported record's `save()`
XADDs its own entry, so the destination's stream is not empty — it truthfully
records the import as the event it was. Carrying the source's entries would
assert that mutations happened on the destination that never did, and would
re-fire any live consumer reading the stream. (No consumer groups or PEL state
exist to worry about: the file issues no `xgroup_create`, `xreadgroup`, `xack`,
or `xpending` — the issue's concern there is anticipatory, not current.)

Policy stays `"partial"`; note becomes a stated contract.

### 5. FrequencySketch — **PERMANENT LIMITATION**

`$FS:{C}:{field}` is a single per-model hash of `"{row}:{col}" → count`
(`existence_filter.py:675-682`), so test 1 fails outright.

Test 2 would pass in isolation — an `HGETALL`/`HSET` restores it verbatim — but
that is precisely what makes carrying *harmful*: importing N records re-runs
`on_save`, which increments the sketch N times (`:711`). A carried sketch laid
on top would double-count every landed record. Nor can it be decomposed
per-record and subtracted: a CMS counter is a sum over colliding hashes, and
recovering one record's contribution is the exact thing the sketch's collisions
make impossible.

Test 3 is decisive. The destination's rebuilt sketch counts each landed record
once — which is the correct sketch *for the record set that landed*. What is
unrecoverable is the source's multiplicity (a record saved fifty times counted
fifty times) and the contributions of records deleted before export
(`on_delete` is a documented no-op at `:733-750`, so the source sketch
over-counts by design). Those are properties of the source deployment's write
history, not of any record. "Rebuild from landed records" is not a compromise
here; it is the more accurate answer.

Policy stays `"partial"`; note states the contract and names the multiplicity
loss explicitly.

### 6. ExistenceFilter bloom bits — **PERMANENT LIMITATION**

Same shape: `$EF:{C}:{field}` is a single per-model Redis string used as a bit
array (`existence_filter.py:384-391`). Test 1 fails.

Test 3 is again decisive, and more strongly than for the sketch. The filter's
whole correctness guarantee is **zero false negatives**. Import re-runs
`on_save` and sets the bits for every landed record, so the destination filter
satisfies that guarantee for exactly the set it holds — and, because
`on_delete` is a no-op (`:435-453`), it does so with *fewer* bits set and
therefore a **lower false-positive rate** than the source. Carrying the source
array would import its accumulated over-retention: bits for records deleted
before export and for records excluded by the export filter, degrading the
destination for no gain. This is the one case where carry is not merely
unavailable but strictly worse than rebuild.

(Both structures' sizing config — `error_rate`/`capacity` → `m`,`k` at
`:365-375`, and `width`/`depth` at `:658-659` — is declared statically on the
field and available at import time, so a mismatch would at least be detectable.
That does not rescue either carry; it only means the refusal could be made
loud. Not worth building for a carry we are declining on other grounds.)

Policy stays `"partial"`; note states the contract and the rebuild-is-better
reasoning.

### Verdict summary

| Subsystem | Per-record? | Raw restore? | Rebuild better? | Verdict |
|---|---|---|---|---|
| PredictionLedger | yes | yes (`HSET` msgpack verbatim) | no | **carry** |
| CoOccurrence | yes | yes (`ZADD` scores verbatim) | no | **carry** |
| AccessTracker log | yes | yes (`RPUSH` timestamps verbatim) | no | **carry** |
| AccessTracker staged | yes | n/a | n/a | intentionally dropped |
| EventStream | **no** | **no** (monotonic IDs) | yes | **permanent** |
| FrequencySketch | **no** | yes, but double-counts | **yes** | **permanent** |
| ExistenceFilter | **no** | yes, but imports error | **yes** | **permanent** |
## Spike Results

Two runtime claims the verdicts rest on were verified rather than assumed.

### spike-1: Are Redis stream IDs monotonic enough to refuse a replay?
- **Assumption**: "Carrying stream entries with their source IDs is blocked by Redis, not merely inadvisable."
- **Method**: `redis-cli -n 3` against the lane's own database (no popoto import, so no DB-0 binding risk).
- **Result**: `XADD k 100-1` succeeded; `XADD k 50-1` returned
  `ERR The ID specified in XADD is equal or smaller than the target stream top item`.
  So an explicit-ID carry is possible only into a stream whose top item is older
  than every carried entry — i.e. an empty destination stream, imported before
  any other write. Any real import writes its own mutation entries first
  (spike-2), so the window never exists.
- **Confidence**: high.
- **Impact if false**: none — test 1 (not per-record) already sinks EventStream
  independently.

### spike-2: Does `save()` on the import path write to the stream?
- **Assumption**: "The destination's stream is not empty after an import; it
  records the import itself."
- **Method**: code-read for callers of `_xadd_mutation`.
- **Result**: `src/popoto/models/base.py:1551, 1641, 1753, 1873, 2179` — every
  save and delete path fires it. `import_.py:252` calls `instance.save(...)`, so
  each landed record contributes its own entry. Confirmed.
- **Confidence**: high.
- **Impact if false**: the EventStream verdict's *framing* would weaken (the
  destination stream would simply be empty rather than truthful), but the
  verdict would not change.

### spike-3: Does `CoOccurrenceField` have an `on_save` that would clobber a restore?
- **Assumption**: "A plain `save()` does not touch `$CoOcF:` edge keys, so
  `import_state` writing after the save is safe and does not race a rebuild."
- **Method**: code-read.
- **Result**: `co_occurrence_field.py` defines `on_delete` (`:689`) and no
  `on_save`. Edges are written only by explicit `link`/`strengthen`/`unlink`/
  `weaken_all` calls. Confirmed.
- **Confidence**: high.
- **Impact if false**: `import_state` would need to overwrite rather than merge —
  which is what it does anyway (see Solution), so no plan change.

## Data Flow

Both hooks are per-record and the ordering is already fixed by the driver; the
carriers add no new stage.

**Export** (`transfer/export.py:242-363`), once per record:
`Query.get_many_objects` hydrates a chunk → `_record_values` collects field
values → `_field_state` (`:185-206`) iterates `_meta.fields` and calls each
field's `export_state(instance, field_name, value)`, keying results by field
name → `_model_state` (`:209-229`) walks the MRO and calls `export_state(instance)`
on any class declaring it in its own `__dict__`, keying by class name → one
JSONL line. A raise inside either hook is caught and appended to
`result.warnings`; the record still exports without that state.

- `CoOccurrenceField` (a `Field` subclass) enters via the **field** pass.
- `PredictionLedgerMixin` and `AccessTrackerMixin` (model-level mixins) enter
  via the **MRO** pass.

**Import** (`transfer/import_.py:209-312`), once per record: EXISTS conflict
check → `model_class(**values)` → `instance.save(skip_auto_now=True)` → **then**
`_restore_state` (`:151-196`). The ordering is load-bearing and already
documented at `:158-162`: `on_save` seeds or clobbers companion structures, so
restoring first would be undone. All three new carriers write *after* the save
and overwrite rather than merge, for the same reason `ConfidenceField.import_state`
does (`confidence_field.py:224-230`). A raise inside `_restore_state` classifies
the record `PARTIAL` — record present, auxiliary state absent — which is the
correct degradation and needs no new machinery.

## Solution

### Shape of each carrier

All three follow the `ConfidenceField` precedent verbatim
(`confidence_field.py:176-235`), including its two structural moves: resolve the
field instance from inside the classmethod via
`model_instance._meta.fields.get(field_name)` plus an `isinstance` guard (needed
because `max_edges` and friends are set in `__init__`, not as class attributes,
so `cls` cannot reach them), and write with a raw Redis command rather than the
field's own mutation API.

**`PredictionLedgerMixin`** (`fields/prediction_ledger.py`), model-level, so the
signature drops `field_name`:
- `export_state(cls, model_instance, **kwargs)`: `HGET` `_meta_key(instance)` at
  field `instance.db_key.redis_key`; `msgpack.unpackb`; return the dict, plus
  the resolved `partition` so import knows which errors ZSET the member belongs
  to. Return `None` when there is no entry.
- `import_state(cls, model_instance, state, **kwargs)`: `msgpack.packb` and
  `HSET` the blob back unchanged — `recorded_at` and `resolved_at` are carried,
  never re-stamped. If `state["resolved"]` and `prediction_error` is not None,
  `ZADD` the member into `_error_key(model_class, partition)` at score
  `abs(prediction_error)`. `RESOLVE_PREDICTION_LUA` is never invoked, so
  `_apply_confidence_feedback` and `_log_resolution_event` do not fire.
- `roundtrip_policy` → `"carry"`; `roundtrip_note` → `None`.

**`CoOccurrenceField`** (`fields/co_occurrence_field.py`), field-level:
- `export_state(cls, model_instance, field_name, field_value, **kwargs)`:
  `ZRANGE(edge_key, 0, -1, withscores=True)` → `{"edges": {target_pk: weight},
  "max_edges": <source cap>}`. Return `None` on an empty set.
- `import_state(cls, model_instance, field_name, state, **kwargs)`: `DELETE` the
  edge key, then `ZADD` every edge with its weight **clamped** to
  `Defaults.CO_OCCURRENCE_WEIGHT_CAP` and the set **truncated** to the
  destination field's `max_edges`, keeping the highest weights. Symmetric
  mirroring is *not* performed — each record carries its own edge set, so the
  mirror arrives with the partner record if the partner is in the export.
- `roundtrip_policy` → `"carry"`; `roundtrip_note` → `None`.

**`AccessTrackerMixin`** (`fields/access_tracker.py`), model-level, extends the
existing carrier rather than adding a new one:
- `export_state`: add `"access_log": [float, ...]` from
  `LRANGE(_at_key("access_log"), 0, -1)`. Omit the key entirely when the list is
  empty, so an instance with counters but no log exports exactly as it does
  today.
- `import_state`: when `state` carries `access_log`, `DELETE` then `RPUSH` the
  timestamps, trimmed to the destination's `_max_access_log` keeping the most
  recent — the same trim `CONFIRM_ACCESS_LUA` applies (`:47`). Tolerate the key
  being absent: a file exported by #554-era code has no `access_log`, and that
  must import cleanly rather than raise.
- `roundtrip_policy` stays `"partial"`; note rewritten (below).

### Shape of each permanent-limitation note

No new `roundtrip_policy` value is introduced. `"partial"` remains accurate for
all three — something genuinely is not carried — and the distinction between
"TODO" and "contract" is carried by the note text, which is already surfaced in
the manifest (`export.py:129-138`), in `ImportReport.fidelity`
(`import_.py:388-391`), and in the docs table this plan adds. A fourth enum value
would have to be taught to the manifest, the report, `docs/field-authoring.md`,
and every third-party field author, to encode a distinction that one sentence
already carries. Rejected as churn; recorded here so critique can challenge it.

Each of the three notes changes from `"... not carried or rebuilt by import;
see #556"` to a sentence that (a) names what the destination gets instead and
(b) says why that is correct. Concretely:

- **EventStream**: "Mutation history is per-deployment and is not carried; the
  destination stream records the import itself. Stream IDs are monotonic per
  key, so source entries cannot be replayed without fabricating a mutation
  history that never happened."
- **FrequencySketch**: "Counters are rebuilt from the records that land, which
  counts each imported record once. The source's save multiplicity and the
  contributions of records deleted before export are not recoverable and are
  not carried."
- **ExistenceFilter**: "Bits are rebuilt from the records that land, preserving
  the zero-false-negative guarantee for that set at a lower false-positive rate
  than the source. The source's over-retained bits are deliberately not
  carried."
- **AccessTracker**: "access_count, last_accessed, and the confirmed access log
  are carried. Staged (unconfirmed, TTL-bounded) reads are not — dropping an
  in-flight read is equivalent to discard_staged_access()."

`src/popoto/fields/field.py`'s `roundtrip_note` docstring (`:246-253`) currently
offers `"history not carried, see #556"` as its example, which after this work
teaches the wrong shape. It gains one sentence: a note should state the
destination's contract, and should cite a tracking issue only while the gap is
still open work.

### Not touched

`src/popoto/transfer/` needs **no change**. Every carrier fits the existing
per-record hooks. `src/popoto/privacy/never_record.py:603` declares
`roundtrip_policy = "rebuild"` and is correct as-is — it is listed here only
because it is one of the two ratchet-pinned-at-zero packages, so it must be
confirmed untouched rather than assumed.

## Failure paths

| Path | Behavior |
|---|---|
| `export_state` raises (corrupt msgpack, unreadable key) | Caught at `export.py:198-203`; appended to `result.warnings`; record exports without that state. Each carrier additionally guards its own decode and returns `None` on garbage, matching `confidence_field.py:194-200`. |
| `import_state` raises | Caught at `import_.py:290-297`; record classified `PARTIAL` with the exception in the reason. Record exists, auxiliary state does not. |
| Destination model lacks the mixin whose state was carried | `_restore_state:190-195` raises `ModelException` → `PARTIAL`. Pre-existing behavior, unchanged. |
| `#554`-era export (no `access_log` key) imported by new code | Key absent → log restore skipped, counters restored as before. Explicitly tested. |
| New export imported by `#554`-era code | Same `FORMAT_VERSION`; the extra dict key is ignored by the older `import_state`. No version bump needed. |
| Carried CoOccurrence edge whose target record is absent | Edge lands, dangling. `get_linked` returns a pk with no record; `propagate` traverses an empty key and stops. Documented, not detected. |
| Source `max_edges` > destination's | Truncated to destination cap, highest weights kept. |
| Source weight > destination `CO_OCCURRENCE_WEIGHT_CAP` | Clamped. Preserves the `propagate` contraction invariant. |
| PL entry unresolved (`resolved: False`) | Meta blob carried; no ZSET member written, because the source has none either. |

## Test impact

New file `tests/test_transfer_history_state.py` (keeping
`tests/test_transfer_fidelity_fields.py` for the #554 carriers, which it already
covers). Cases:

1. **PL round-trip, resolved** — record + resolve a prediction, export, flush the
   model, import; assert the meta blob is byte-identical after re-pack, that
   `recorded_at`/`resolved_at` match the source exactly (this is the
   fabricated-timestamp guard), and that the errors ZSET member and score match.
2. **PL round-trip, unresolved** — no ZSET member on either side.
3. **PL no side effects** — a model combining `PredictionLedgerMixin` with a
   `ConfidenceField`: assert the confidence companion hash after import equals
   the source's, i.e. `_apply_confidence_feedback` did not re-fire. This is the
   double-count guard.
4. **CoOccurrence round-trip** — build a symmetric graph over three records,
   export all, import; assert every edge key's `ZRANGE ... WITHSCORES` matches
   exactly.
5. **CoOccurrence truncation and clamping** — export from a field with
   `max_edges=10`, import into `max_edges=3`; assert exactly the top 3 by weight
   survive. Separately assert an over-cap weight is clamped.
6. **CoOccurrence dangling edge** — export one endpoint only; assert the edge
   lands and `propagate` terminates without raising.
7. **AccessTracker log round-trip** — confirm several accesses, export, import;
   assert the log matches and that `confirm_access()` afterwards continues from
   the carried count.
8. **AccessTracker forward compat** — hand-build a `state` dict with no
   `access_log` key; assert `import_state` restores counters and does not raise.
9. **AccessTracker staged not carried** — stage a read without confirming;
   assert the destination staged key is absent.
10. **Policy declarations** — assert the three carriers now report `"carry"`
    with `roundtrip_note is None`, and that the three permanent ones report
    `"partial"` with a note that does **not** contain `"see #556"`. This is the
    test that makes the docs claim enforceable.

`tests/test_transfer_roundtrip.py:144` already enforces that every field
declares a `roundtrip_policy`; that test should keep passing untouched.

**Test-writing hazard (carried from the #649 lane).** Field modules bind their
client at import time via `from ..redis_db import POPOTO_REDIS_DB`, and
`tests/test_connection.py` rebinds that attribute. These tests assert on Redis
*contents* (ZRANGE/HGET/LRANGE results) rather than spying on issued commands,
which sidesteps the trap entirely — no command-spy patching is used. If a spy
ever becomes necessary, every distinct client object must be patched and the
capture asserted non-empty on its own line before any content assertion.
Regardless, the new file is run after `tests/test_connection.py` in a
full-suite-ordered run before the result is trusted.

## Rabbit Holes

- **Building a whole-model export hook** so the global structures could be
  carried once. It would let EventStream/FrequencySketch/ExistenceFilter be
  carried in principle — but all three fail on other grounds too (replay
  fabrication; double-counting; imported error), so the hook would buy nothing
  this issue wants. Out of scope; if a future field genuinely needs it, that is
  its own issue.
- **Detecting dangling CoOccurrence edges at import.** Needs an `EXISTS` per
  edge (up to 500 per record) or an end-of-import pass with no hook. Documented
  instead.
- **A fourth `roundtrip_policy` value.** Considered and rejected in Solution.
- **Making the ExistenceFilter/FrequencySketch config mismatch loud.** The
  sizing params are available at import time, so a mismatch *could* be detected
  — but only matters if we were carrying, which we are not.
- **Touching `src/popoto/transfer/`.** If any carrier appears to need a driver
  change, that carrier is cut rather than the driver widened.

## Risks and races

- **`import_state` overwrites.** All three carriers `DELETE`/overwrite rather
  than merge. Under `on_conflict="overwrite"` the record's values are being
  replaced wholesale, so replacing its auxiliary state is the consistent choice;
  under `"skip"` the hook is never reached (`import_.py:235-237`); under
  `"error"` the import has already raised. A destination record that had
  accumulated its own edges or access log *and* is being overwritten loses them
  — correct, and stated in the docs.
- **Concurrent writes during import.** `import_records` already documents that
  it assumes the destination is not under concurrent write for the model
  (`import_.py:357-360`). The carriers inherit that assumption and add no new
  window: each is a single raw write after the record's own save.
- **No new async surface.** Every write is a synchronous command on the same
  client the surrounding code uses. No pipeline is introduced, so no partial-
  pipeline failure mode is added.
- **Lane isolation.** Tests run with `POPOTO_TEST_DB=3`. Any ad-hoc repro script
  sets `REDIS_URL=redis://localhost:6379/3` *before* `import popoto`, per
  `scripts/scratch_repro.py`. The spike above used `redis-cli -n 3` and imported
  nothing.
- **No Redis modules.** Every command used — `HGET`, `HSET`, `ZRANGE`, `ZADD`,
  `DEL`, `LRANGE`, `RPUSH`, `LTRIM` — is core, so Valkey CI passes for the same
  reason Redis CI does.

## No-Gos

- No change to `src/popoto/transfer/`.
- No new `roundtrip_policy` enum value.
- No whole-model / end-of-import hook.
- No replay-based restore anywhere: no carrier may call `resolve_prediction`,
  `auto_resolve`, `link`, `strengthen`, `weaken_all`, `confirm_access`, or
  `XADD`.
- No `FORMAT_VERSION` bump.
- Do not touch `src/popoto/models/query.py`,
  `src/popoto/recipes/memory_lifecycle.py`,
  `src/popoto/fields/tombstone_store.py`, or `src/popoto/__init__.py` — each is
  under another concurrent lane.
- No attempt to make `ExistenceFilter`/`FrequencySketch` deletable or
  decrementable; their no-op `on_delete` is a deliberate guarantee, not a bug.

## Documentation

- **`docs/guides/export-import.md`** — "What is not carried" (`:192-206`) is
  rewritten. Its current blanket claim becomes a fidelity matrix: the six
  subsystems, what each does on import, and whether it is a permanent contract.
  The three now-carried rows move out of "not carried."
- **`docs/field-authoring.md`** — the `roundtrip_policy` section (`:151-165`)
  gains the guidance that a `"partial"` note should state the destination's
  contract, and cite a tracking issue only while the gap is open work. The three
  new carriers are usable as worked examples alongside the existing ones.
- **`docs/fields.md`** — the AccessTracker key table (`:1958-1965`) currently
  implies all three keys are equally transient; add that the confirmed log is
  carried by export/import and the staged list is not.
- **`docs/features/co-occurrence-field.md`** — add the export/import section:
  edges carried verbatim, clamped and truncated to destination config, dangling
  edges under a filtered export.
- **`CLAUDE.md`** — no change. The round-trip protocol is not one of the
  invariants that file tracks.

## Success Criteria

- All six subsystems have a verdict recorded in this document with reasoning
  traceable to the decision rule, and the code matches it.
- `PredictionLedgerMixin` and `CoOccurrenceField` declare `roundtrip_policy =
  "carry"` with `roundtrip_note = None` and implement both hooks.
- `AccessTrackerMixin` carries the confirmed access log; its note names staged
  reads as an intentional, permanent exclusion.
- `EventStreamMixin`, `ExistenceFilter`, and `FrequencySketch` retain
  `"partial"` with notes that state the destination's contract and contain no
  `"see #556"`.
- `tests/test_transfer_history_state.py` passes, including the
  timestamp-fidelity, no-side-effect, and note-text assertions.
- Existing `tests/test_transfer_*.py` pass unchanged.
- `ruff check src/` exits 0; `black --check src/ tests/` passes.
- `scripts/mypy_ratchet.py` at or below baseline 1042, with `integrations/` and
  `privacy/` still at exactly zero. Environment stated with the number.
- CI green on both Redis and Valkey.
- PR body carries `Closes #556` and closes nothing else.

## Step by Step Tasks

1. Branch `session/sdlc-556` off current main (already created).
2. `PredictionLedgerMixin`: add `export_state` / `import_state`; set
   `roundtrip_policy = "carry"`, `roundtrip_note = None`; replace the
   lines 114-119 comment with the carry rationale.
3. `CoOccurrenceField`: add `export_state` / `import_state` with clamping and
   `max_edges` truncation; set `"carry"`, note `None`; replace the lines 255-259
   comment.
4. `AccessTrackerMixin`: extend `export_state` with `access_log`; extend
   `import_state` to restore it, tolerating the key's absence; rewrite the note.
5. `EventStreamMixin`, `ExistenceFilter`, `FrequencySketch`: rewrite the three
   notes and their preceding comments as permanent contracts.
6. `fields/field.py`: update the `roundtrip_note` docstring guidance.
7. Write `tests/test_transfer_history_state.py` (10 cases above).
8. Run the narrow test set with `POPOTO_TEST_DB=3`, then re-run with
   `tests/test_connection.py` ordered ahead of it.
9. Docs cascade: `docs/guides/export-import.md` fidelity matrix,
   `docs/field-authoring.md`, `docs/fields.md`, `docs/features/co-occurrence-field.md`.
10. Gates: ruff, black, mypy ratchet (state environment), narrow tests.
11. Open PR with `Closes #556`; `/do-pr-review`; patch; `/do-docs`; `/do-merge`.

## Open Questions

1. **Is "documented permanent limitation" acceptable for three of six?** The
   issue explicitly authorizes it, and the reasoning above argues each is not
   just unavailable but *wrong* to carry. Flagging it because it is the plan's
   central judgment call.
2. **No fourth `roundtrip_policy` value** — the TODO-vs-contract distinction
   lives in note text plus the docs matrix. If a machine-readable flag is wanted
   (e.g. `roundtrip_permanent: bool`), say so now; it is cheap before build and
   expensive after, since it touches the manifest and the report.
3. **CoOccurrence import overwrites the destination's existing edges.** Chosen
   for consistency with `on_conflict="overwrite"` replacing the record wholesale.
   Merging (union, max weight) is the alternative. Confirm the overwrite reading.

