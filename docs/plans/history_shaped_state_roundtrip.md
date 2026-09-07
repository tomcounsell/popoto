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
## Data Flow
## Solution
## Failure paths
## Test impact
## Rabbit Holes
## Risks and races
## No-Gos
## Documentation
## Success Criteria
## Step by Step Tasks
## Open Questions
