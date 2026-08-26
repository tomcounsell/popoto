# Provenance Journal

An append-only record of exactly who said what, when, in what words — with
corrections stored as new entries pointing at the ones they correct, never as
edits.

An agent is told, in a Slack thread, "Tom said the launch slipped to the
30th." Two turns later Tom himself says "actually the 30th is wrong, it's the
27th." Stored as an ordinary mutable memory row, the second statement
overwrites the first: the original words are gone, nobody recorded *who* said
either one, and there is no way to ask what the agent believed last Tuesday.
`JournalEntry` and `ProvenanceJournal` are the substrate that fixes that: every
capture is immutable, every correction is a new entry, and the live belief set
is a query over [`ValidityField`](validity-and-supersession.md)'s indexes, not
a chain walk.

```python
from popoto.recipes import ProvenanceJournal, JournalEntry

first = ProvenanceJournal.append(
    agent_id="agent-1",
    speaker="tom",
    turn_id="t-41",
    verbatim="the launch slipped to the 30th",
    statement="Launch date is the 30th",
    subjects=["launch"],
).entry

ProvenanceJournal.supersede(
    first,
    agent_id="agent-1",
    speaker="tom",
    turn_id="t-43",
    verbatim="actually the 30th is wrong, it's the 27th",
    statement="Launch date is the 27th",
)

JournalEntry.query.filter(validity__current=True)  # the correction only
ProvenanceJournal.annotations_for(first)            # the correction, again
ProvenanceJournal.chain(first)                      # [first, correction]
```

Ran against a scratch Redis DB (`REDIS_URL=redis://localhost:6379/12`, set
before `import popoto`): `filter(validity__current=True)` returns only the
correction's `entry_id`; `annotations_for(first)` returns the correction
tagged `kind="supersede"`; `chain(first)` returns `[first, correction]` oldest
first. Re-saving `first` raises `AppendOnlyViolation`.

## Why append-only, why one model

Two design choices carry the whole feature, and the plan (`docs/plans/provenance_journal_m1.md`,
issue [#560](https://github.com/tomcounsell/popoto/issues/560)) treats both as decisions with a
stated cost rather than free wins.

**Corrections are new entries, not mutations.** Nothing in `models/base.py`
otherwise forbids re-saving or deleting a record — the only immutability
precedent before this feature was `KeyMutationError`, which guards *key*
mutation, not *value* mutation. `AppendOnlyMixin` closes that gap for any model
that wants write-once semantics; `JournalEntry` is its first consumer, not its
only possible one.

**One record type, not two.** The issue left open whether annotations should
be a separate model from captures. Popoto ships one: every row is a
`JournalEntry`, and `kind` + `target` distinguish a capture from an
annotation. The reasons: annotations must themselves be annotatable (a
retraction of a mistaken retraction, a confirmation of a supersession, which a
separate annotation model turns into a second self-referential pointer type);
`ValidityField`'s chain hashes are keyed by Redis key and are type-agnostic,
so one model keeps `chain()` a straight walk; and the belief-sheet view reads
`validity__current` over one keyspace instead of unioning two. The cost, paid
explicitly rather than hidden: `kind`/`target` consistency is a **runtime**
validation (in `JournalEntry.pre_save` and again in `ProvenanceJournal`'s
pre-flight), not a type-system guarantee.

## The field set

| Field | Type | Why |
|---|---|---|
| `entry_id` | `AutoKeyField` | Immutable UUID identity, assigned at `__init__` — so the record's Redis key is concrete before save, which is what lets the append-only guard check the right key and makes two independent appends unable to collide |
| `agent_id` | `KeyField` | Partition key, mirroring `DefaultMemory`. Required non-null by `ProvenanceJournal.append()`: a `None` renders the literal string `"None"` into the record key |
| `captured_at` | `FloatField` | Wall-clock capture time of the source turn |
| `turn_id` | `IndexedField(str)` | "Everything from turn T" in one query |
| `speaker` | `IndexedField(str)` | "Everything attributed to S" in one query. Attribution, not authentication |
| `verbatim` | `StringField` | The exact source span. Privacy-sensitive — the reason `NeverRecordMixin` is mandatory rather than optional |
| `statement` | `StringField` | The atomic natural-language claim distilled from the span |
| `subjects` | `TagField` | Multi-value: one entry can concern several people or topics. Convention over schema, explicitly **not** a security boundary, inheriting `TagField`'s framing verbatim |
| `stated` | `BooleanField` | Stated (`True`) vs inferred (`False`); feeds downstream conflict-precedence resolution |
| `kind` | `IndexedField(str)` | `assert` / `confirm` / `supersede` / `retract`, validated against the model's kind vocabulary. Indexed so "every retraction" is one query |
| `target` | `IndexedField(str, null=True)` | The annotated entry's Redis key. A plain indexed scalar, not a `Relationship` — the target is already addressed by Redis key, and `Relationship`'s lazy-load machinery plus its heavier save buys nothing here |
| `validity` | `ValidityField` | The `valid_from` / `invalid_at` / `ingested_at` axes |

### `captured_at`, deliberately not `ingested_at`

`ValidityField.on_save` hardcodes the **save clock** into its own
`$ValidityF:{Model}:validity:ingested_at` ZSET and ignores any model field
entirely — there is no hook that lets a model supply its own ingest time. If
`JournalEntry` also had a field named `ingested_at`, the two would silently
disagree: the model field would carry the caller's notion of "when this was
ingested" and the validity ZSET would carry the save clock, and a downstream
reader would get a different answer depending on which one it read, with no
error to signal the split. Naming the model field `captured_at` instead makes
the two axes impossible to confuse: `captured_at` is always the wall-clock
time of the *source turn* (settable, arbitrary, part of the record); the
validity ingest axis is always the save clock (fixed, per-write, part of
`ValidityField`'s own bookkeeping).

### `filter(validity=t)` is a trap

`validity` is a legal field name — `validity__current` and `validity__as_of`
are query-param suffixes derived from it, not a name collision — but
`ValidityField.filter_query` only handles those two suffixed forms. A bare
`filter(validity=t)` matches neither and silently returns an empty result.
Verified:

```pycon
>>> import time
>>> JournalEntry.query.filter(validity=time.time())
[]
```

Use `filter(validity__current=True)` or `filter(validity__as_of=t)`. `target`,
`kind`, `speaker`, and `turn_id` collide with nothing — the only reserved field
names in Popoto are `limit`, `order_by`, and `values`.

## The four annotation kinds

| Kind | Carries `target` | Closes the target's interval |
|---|---|---|
| `assert` | No — an original capture | No |
| `confirm` | Yes | No — corroboration only |
| `supersede` | Yes | Yes |
| `retract` | Yes | Yes |

`confirm` is evidence, not a membership change: the target keeps its open
interval, and downstream readers use the annotation count as corroboration.
`supersede` and `retract` are mechanically identical — both append an
annotation and close the target's interval in the same transaction — and
differ only in semantics: a supersession replaces a claim with a better one,
a retraction withdraws it with no replacement.

### The extension seam: `register_kind`, and the reader rule

The core vocabulary — `Defaults.JOURNAL_KINDS = ("assert", "confirm",
"supersede", "retract")` — is a frozen tuple, not a tunable: changing it would
reclassify already-stored entries. Downstream modules that need more kinds (a
merge/equivalence kind, a queue-able kind, an exposure kind) register them
instead:

```python
JournalEntry.register_kind("merge", closing=True)

claim = ProvenanceJournal.append(
    agent_id="agent-1", statement="Launch date is the 30th"
).entry

ProvenanceJournal.append(
    agent_id="agent-1",
    kind="merge",
    target=claim,
    statement="Folded into the canonical launch-date claim",
)   # -> target_closed=True: appends the annotation AND closes claim's
    #    interval, exactly like a supersede
```

`register_kind` is a **registration call, not a model subclass** — see
[Do not subclass `JournalEntry`](#do-not-subclass-journalentry) for why a
subclass seam cannot work in this ORM. Registration is process-global and
purely additive: it can never remove or reclassify a core kind.

The two flags are the point of the call, not decoration. They record the kind's
*behavior*, which a bare vocabulary list cannot:

| Flag | Meaning | Core kinds with it |
|---|---|---|
| `targetless=True` | An original capture that carries no `target` | `assert` |
| `closing=True` | An entry of this kind closes its target's validity interval | `supersede`, `retract` |

Defaults are `targetless=False, closing=False`: target required, membership
untouched — the `confirm` shape. `register_kind` raises `ValueError` on an
empty name, on a core kind (`Defaults.JOURNAL_KINDS` is frozen), on
`targetless` and `closing` together (a kind with no target has nothing to
close), and on re-registering an existing name under *different* flags, since
that would reclassify entries already stored under it. Re-registering with the
same flags is a no-op.

The reader rule this seam is built around: **an entry whose `kind` a reader
does not recognize is inert for membership.** A reader that only knows the
core four must treat a `merge`-kind entry as "not a supersede, not a retract"
and leave the target's membership alone — never silently promote an unknown
kind to `supersede`/`retract` behavior. This is what lets the vocabulary grow
across modules without every existing reader needing a simultaneous upgrade.

### Do not subclass `JournalEntry`

Popoto's `ModelBase` metaclass does **not** inherit `Field` attributes from a
base model class. A `JournalEntry` subclass therefore has an *empty* field set
and would persist nothing at all:

```pycon
>>> class SubEntry(JournalEntry): pass
>>> sorted(JournalEntry._meta.fields)
['agent_id', 'captured_at', 'entry_id', 'kind', 'speaker', 'stated',
 'statement', 'subjects', 'target', 'turn_id', 'validity', 'verbatim']
>>> sorted(SubEntry._meta.fields)
[]
```

This is an ORM-level limitation, not a journal one, and it applies to every
Popoto model — it is recorded here because the journal is the module whose
extension story it changes. `ProvenanceJournal` refuses such a model rather
than filling a keyspace with empty records:

```pycon
>>> class SubJournal(ProvenanceJournal): entry_model = SubEntry
>>> SubJournal.append(agent_id="agent-1", statement="would be lost")
Traceback (most recent call last):
    ...
TypeError: SubEntry is not a usable journal entry model: it declares no
'statement' field, so every record it writes would persist nothing. ...
```

To extend the annotation vocabulary, call `JournalEntry.register_kind()`. For a
separate keyspace or a different field set (an `EmbeddingField`, say), declare
your own `Model` with the same mixins and the same fields and point a
`ProvenanceJournal` subclass at it.

## The `ProvenanceJournal` API

`ProvenanceJournal` is a stateless façade — every method is a `classmethod`,
there is no instance state — and it is the **only supported** read and write
API.

| Method | Effect |
|---|---|
| `append(*, agent_id, statement=, verbatim=, speaker=, turn_id=, subjects=, stated=, captured_at=, at=, ...)` | Appends a `kind="assert"` capture. Never changes another entry's membership |
| `confirm(target, *, agent_id, ...)` | Appends a `kind="confirm"` annotation. Membership is unaffected |
| `supersede(target, *, agent_id, ...)` | Appends a `kind="supersede"` annotation and closes the target's validity interval, in one transaction |
| `retract(target, *, agent_id, ...)` | Appends a `kind="retract"` annotation and closes the target's validity interval, in one transaction |
| `annotations_for(entry)` | Every entry annotating `entry`, in one `filter(target=...)` call |
| `chain(entry)` | The supersession chain through `entry`, oldest first — a display/replay read, walking `ValidityField`'s chain hashes, **not** the membership query |

Every mutating method returns an `AnnotationResult(entry, target_closed,
coupling_enabled, pipeline, close_index)` — a typed result readable without
touching Redis, so a caller never has to issue a read just to find out whether
an annotation actually changed membership.

`target_closed` is `Optional[bool]`, and which of the three values you get
depends on who owns the pipeline:

- **The journal owns it** (no `pipeline=` argument): a real `bool`, read from
  the supersede script's own reply. `False` for `append`/`confirm` (neither
  changes membership), `False` when the
  [validity coupling switch](#the-kill-switch-popoto_journal_coupling_disable)
  is off, and `False` when the target was *already* closed by a concurrent
  annotation — which is the honest answer, since this call closed nothing
  (both annotations are real provenance, one close applies).
- **The caller supplied one**: `None` — *unknown until you execute*. Nothing
  has run, so no truthful `bool` exists. A re-close of an already-closed
  target queues exactly like a first close and applies nothing, so reporting
  `True` for a queued close would be affirmatively wrong. Read the real
  outcome from your own `execute()`:

```python
pipe = popoto.get_redis().pipeline()
result = ProvenanceJournal.supersede(first, agent_id="agent-1",
                                     statement="...", pipeline=pipe)
assert result.target_closed is None          # nothing has executed yet
results = pipe.execute()
closed = bool(results[result.close_index])   # the real answer
```

`close_index` is the index of the queued interval-close command in the
caller's pipeline. It is `None` whenever no close was queued — always so on
the journal-owned path, where the pipeline has already executed and
`target_closed` carries the answer directly.

Every method also raises before writing anything on: a firewall-blocked
value (`JournalBlockedError`), an out-of-vocabulary `kind` or an inconsistent
`kind`/`target` pairing, a missing `agent_id`, a nonexistent or unsaved
target, a cross-agent target, a backdated `at`, or a non-transactional
caller-supplied pipeline (`ValueError` in each of those cases). See the
docstrings on each method for the exact raise conditions.

## The one-transaction annotate-and-close sequence, stated precisely

`supersede()` and `retract()` queue two things into a single Redis
`MULTI`/`EXEC`: the annotation's `save()`, and `ValidityField.execute_supersede(
..., mode="invalidate", old_member=<target key>, new_member=<annotation key>)`.
The write path calls `execute_supersede` directly and never routes through
`SupersessionProtocol` — `SupersessionProtocol.invalidate` resolves its member
keys with `POPOTO_REDIS_DB.exists(...)`, and inside a pipeline the successor's
`HSET` is only *queued*, not executed, so `EXISTS` returns 0 and the call
silently takes its "unsaved successor → no-op" branch: no invalidate script
runs, the target stays open, and nothing signals the failure. `execute_supersede`
has no such existence check, so it is the correct seam for a write that has
not committed yet.

**The property that holds:** no interleaving reader observes the annotation
without the close. Between the transaction opening and `EXEC`, no other
client can see the annotation entry (it isn't written yet) or a closed target
with no annotation (the invalidate script hasn't run yet); when `EXEC`
returns, both are true together.

**The property that does not hold, and is not claimed: rollback.** Redis
`MULTI`/`EXEC` does not roll back sibling commands when one command errors at
execute time — this is documented behavior, and it is the same rationale the
ORM's own eager-EVAL design (issue #476) already depends on. A command-level
error inside `EXEC` can therefore leave the annotation appended with the
target still open. `ProvenanceJournal` narrows the window that produces such
an error — its pre-flight validates the target exists, belongs to the same
agent, and that the requested instant is not before the target's stored
`valid_from`, all *before* anything is queued — but it does not close it to
zero. The residual is a documented, tested boundary, not an impossibility
claim.

## Relationship to `ValidityField`: two different questions

- **Membership** — "is this claim part of the current belief set?" — comes
  entirely from `JournalEntry.query.filter(validity__current=True)`, which
  reads `ValidityField`'s interval indexes. No chain walk is involved.
- **Chains** — `ProvenanceJournal.chain(entry)` — are for provenance display
  and replay verification only: "show me the sequence of corrections that led
  here" or "reconstruct what was believed at time *t*". A chain walk never
  decides membership.

This split is why `annotations_for()` and `chain()` cost different numbers of
Redis round trips for the same question asked two ways, and why the
membership query stays O(1) index reads regardless of how deep a chain has
grown.

## The append-only boundary, stated honestly

**Immutability here is an ORM-layer contract, not a storage guarantee.** It
holds against every Python write path in `src/popoto/models/base.py` —
`save`, `create`, `get_or_create`, `update_or_create`, both bulk-save sites,
`delete`, and `delete_all()` (which routes through `instance.delete()` per
instance, so the guard fires there too). Verified directly:

```pycon
>>> first.save()
Traceback (most recent call last):
    ...
popoto.exceptions.AppendOnlyViolation: JournalEntry is append-only: a record
already exists at JournalEntry:agent/-1:c220f21ce4f44bcaadcfc8b31656d2ab. ...
```

It does **not** hold against a raw Redis client, and the repo's own migration
cookbook (`src/popoto/models/migrations.py:277-295`) teaches a `delete()` +
re-`hset()` recipe for renaming a field — a pattern that bypasses the guard
by construction, because it never goes through `Model.save()`. Redis and
Valkey have no per-key write-once mode; `SETNX`/`HSETNX` are the only atomic
create-if-absent primitives, and neither covers a multi-field `HSET` plus the
index writes a Popoto model performs on save. Closing that gap at the storage
layer would mean an `HSETNX`-based write path duplicating `Model.save()`'s
index handling — a deliberate rabbit hole the implementation does not chase.

Two TOCTOU shapes follow directly from "the guard is an `EXISTS`-then-`save`
sequence read outside any pipeline," and neither is claimed as closed:

1. **Cross-process concurrent first-save.** Two writers save the same Redis
   key at the same time. Both `EXISTS` calls can return `0` before either
   `HSET` lands, so both saves proceed and the second silently overwrites the
   first — the exact violation the guard exists to prevent. Narrowed
   structurally rather than locked: `entry_id` is an `AutoKeyField` (UUID), so
   two independent `ProvenanceJournal.append()` calls cannot produce the same
   key by construction. The window is only reachable when a caller supplies
   an explicit, colliding key — a programming error the guard still catches
   in every non-concurrent case.
2. **Two appends of the same key queued into one pipeline.** No concurrency
   required, and deterministically reproducible: the guard's `EXISTS` runs
   immediately against `POPOTO_REDIS_DB`, and it has no way to see a command
   that is queued on a pipeline but not yet executed. Two `save(pipeline=pipe)`
   calls on the same key both pass the `EXISTS` check, both get queued, and
   `EXEC` applies the second `HSET` over the first.

`save(migrate_key=True)` and a set `obsolete_redis_key` are refused
unconditionally, independent of the `EXISTS` check — a key migration would
otherwise `DELETE` the record's previous key after writing the new one,
destroying the entry through a supported public kwarg that the `EXISTS` guard
cannot see coming (the new key doesn't exist yet, so the guard would pass).

## Privacy: composing `NeverRecordMixin`, and its coverage gap

`JournalEntry` stores `verbatim` human speech, which is precisely the surface
[the never-record firewall](never-record-firewall.md) exists to protect, so
`JournalEntry` composes `NeverRecordMixin` directly rather than leaving it
optional. Verified — a capture whose content matches a credential pattern
raises rather than persisting anything:

```pycon
>>> ProvenanceJournal.append(agent_id="agent-1", statement="my key is sk-ant-api03-...")
Traceback (most recent call last):
    ...
popoto.exceptions.JournalBlockedError: never-record: credential_prefix (vendor_token); nothing was written
```

**`subjects` is outside the firewall's scan surface, and this is a real,
documented gap — not an oversight papered over.** `NeverRecordMixin`'s
`_never_record_scan_values` yields only values where `isinstance(value, str)`
is true; a `TagField` value like `subjects` is a **list**, so it is never
scanned by the mixin at all, no matter what names or identifiers a caller puts
into it. `ProvenanceJournal.append()` (and every annotation method) closes
that gap itself, ahead of the mixin, by calling `scan_never_record()` on each
subject tag explicitly, as part of its own pre-flight — before any command is
issued or queued. This is not a formality: `Model.save()` returns the
*pipeline itself* when the firewall fires inside pipeline mode, which is
indistinguishable from a successful queue, so a naive annotate-and-close that
relied on the mixin alone could close a target's interval against an
annotation the firewall actually refused to write.

**`target` is deliberately *excluded* from the scan surface, because it is a
machine-generated pointer rather than content.** `target` holds a Redis key
Popoto itself rendered — `JournalEntry:<agent_id>:<uuid4 hex>` — and scanning
it for payment cards is a category error with a measured cost: a uuid4 hex
sometimes contains a 13–19 digit run that passes the **Luhn** checksum, so the
firewall flagged the annotation as `payment_card`/`luhn` at a rate of roughly
1 target key in 250.

```python
scan_never_record("JournalEntry:agent/-under/-test:8ef1fc6db384458286216656bfb2cf04")
# NeverRecordVerdict(blocked=True, reason='payment_card', detector='luhn')
```

Because that block lands at a `save()` gate that *returns* instead of raising,
the effect was a silently dropped annotation whose target's interval got closed
anyway. The narrowing is exactly one field — `verbatim`, `statement`,
`speaker`, `turn_id`, `kind`, `agent_id` and `subjects` are all still scanned,
and the firewall is not weakened for anything a human or a model ever wrote.

The pre-flight derives its scanned values from the *same method* the mixin uses
(`_never_record_scan_values`) rather than a parallel list, so the two cannot
drift apart and let the mixin block something the pre-flight passed. If a
`save()` in the annotate-and-close path ever does return falsy, the journal
raises `RuntimeError` rather than proceeding to close the target.

**A blocked capture leaves only a content-free tombstone, and that is the
whole signal.** A capture the firewall drops never reaches the journal at
all: it leaves a random id and a reason code in the `$NR:` keyspace (see
[Never-Record Firewall](never-record-firewall.md#auditing-drops)), which is
not part of the journal and is returned by no journal query. There is no
journal-side record of *what* was blocked, only that *something* was — the
only signal visible from inside the journal is a gap in `turn_id` coverage
for a conversation that otherwise has none.

## The kill switch: `POPOTO_JOURNAL_COUPLING_DISABLE`

```python
from popoto.fields.constants import Defaults
Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED = False  # or set the env var before import
```

`POPOTO_JOURNAL_COUPLING_DISABLE`, read at import time (mirroring
`_read_never_record_switch`'s "phrased as a disable, so default-on holds when
unset" convention), turns off only the *validity coupling* — not the
append-only invariant, which has no kill switch by design (disabling
immutability would silently convert the journal into a mutable table while
every downstream consumer keeps assuming immutability; this was raised and
left as a deliberate open question in the plan rather than resolved with a
switch).

With the coupling disabled, `supersede()` and `retract()` still append their
annotation entries and still write `target` — the provenance record is
unaffected — but they do **not** close the target's validity interval.
Membership degrades to "everything ever appended," which is the same
behavior the journal would have with no validity coupling at all. The
degraded mode is observable without reading Redis:
`AnnotationResult.target_closed` is `False` and `AnnotationResult
.coupling_enabled` is `False`. The first uncoupled `supersede`/`retract` call
in a process also logs a warn-once message.

## Growth and the `hard_delete` retention seam

Append-only plus never-delete means the keyspace only grows — this is a
documented characteristic of the design, not a bug: bytes per entry times
append rate, unbounded, with no built-in retention policy. That is
acceptable at moderate scale and a real operational concern for a long-lived
production agent generating an entry per captured fact.

`AppendOnlyMixin.hard_delete(instance)` is the one deliberate, greppable hole
in the append-only contract, reserved for retention and erasure — never for
test teardown (the pytest plugin already flushes the test database before
every test) and never for correcting a mistaken capture (append a `retract`
instead). It exists specifically because `POPOTO_NEVER_RECORD_DISABLE=1` is a
supported deployment action, and with the firewall off, verbatim human
speech — including credentials, if the firewall is disabled — lands in a
keyspace whose only removal path is this classmethod.

`hard_delete` sweeps **the erased record's own derived state**, not just its
hash — this matters because a sweep that only deleted the hash would leave
orphaned index and chain entries pointing at nothing, exactly the
corrupted-index shape the ORM's query layer has to treat as
readable-but-skippable elsewhere:

1. The record hash, the class Set, and every `$IndexedF:`/`$TagF:` index Set,
   via `Model.delete()` itself (reached past the mixin's own delete-refusing
   override), so field `on_delete` hooks do the cleanup rather than a second,
   drifting copy of them.
2. For each `ValidityField` on the model: the three interval ZSETs, the
   record's own entry in both chain hashes, and any `{prefix}:open:*` pointer
   still naming it.
3. The *value* side of both chain hashes — the case step 1 does not cover.
   `ValidityField.on_delete` removes the record as a chain hash *field*, but a
   neighbor's link may still name the erased record as a *value* (`chain:fwd`
   holding `old_entry -> erased_entry`). Left behind, that is a dangling link
   into a record that no longer exists.

Verified: after `hard_delete()` on the correction entry from the example
above, `annotations_for(first)` (a `filter(target=...)` read) returns no
results, and the erased entry's Redis key appears in no `$*`-prefixed key's
*contents*.

**Two things survive, and the scope is "the erased record's own derived
state", not "every trace of it anywhere".** Neither carries the erased
record's `verbatim` or `statement` content, so an erasure motivated by
removing content achieves that — but an erasure motivated by removing every
occurrence of the record's *key* does not:

1. **An index key whose NAME embeds the erased record's Redis key.** An
   annotation that targeted the erased entry owns a
   `$IndexF:JournalEntry:target:<escaped erased key>` Set — observed intact
   after the sweep as
   `$IndexF:JournalEntry:target:JournalEntry{&#58;}agent///-1{&#58;}c1077a2e…`.
   That Set belongs to the *annotating* record, not the erased one, so the
   sweep does not touch it and the erased key survives, escaped, inside its
   name. Erase the annotations too if that matters.
2. **`stream:journal` entries.** Every journal mutation is `XADD`ed to the
   event stream carrying the record's `pk` and its `target` metadata field,
   retained up to `_stream_max_length` (10,000 entries) regardless of
   `hard_delete`. Trim or delete the stream separately if required.

## Gotchas

- **`filter(validity=t)` silently returns nothing.** `ValidityField.filter_query`
  only handles the `__current` and `__as_of` suffixes; a bare exact-value
  filter on the field name matches neither and returns an empty result with
  no error. Use `filter(validity__current=True)` or `filter(validity__as_of=t)`.
- **`on_conflict="overwrite"` is unsupported on append-only models.**
  `AppendOnlyMixin` declares `roundtrip_policy = "rebuild"`. The transfer
  import path calls `instance.save()` for every record, so any collision on
  an append-only model raises `AppendOnlyViolation` and the record is
  classified `ERRORED` rather than overwritten. `"skip"` is the supported
  conflict mode for re-importing into a journal that already has entries.
- **`agent_id` must be non-null.** `agent_id` is a `KeyField`; a `None` value
  would render the literal string `"None"` into the record's Redis key rather
  than raising at construction, so `ProvenanceJournal.append()` (and every
  annotation method) checks for it explicitly and raises `ValueError` before
  writing anything.
- **No `EmbeddingField`.** Deliberate, mirroring `DefaultMemory`: an embedding
  field pulls an optional extra (an API key or a local Ollama), and
  similarity search over the journal was not part of this feature's scope.
  Adding one means declaring your own model with the journal field set plus
  the embedding field — **not** subclassing `JournalEntry`, which loses every
  field (see [Do not subclass `JournalEntry`](#do-not-subclass-journalentry)).
- **`target_closed` is `None`, not `False`, on a caller-supplied pipeline.**
  Nothing has executed at that point, so the journal reports "unknown" rather
  than guessing. Read `results[close_index]` after your own `execute()`.

## See Also

- [ValidityField and SupersessionProtocol](validity-and-supersession.md) — the
  membership and chain mechanism this feature is the journal's first real
  consumer of
- [Never-Record Firewall](never-record-firewall.md) — the privacy gate
  `JournalEntry` composes, and the `subjects`/`TagField` coverage gap this
  page's `append()` closes explicitly
- [Auditable Extraction](auditable-extraction.md) — the opt-in candidate
  pipeline whose `accept`ed candidates are the ones calling `append()` here,
  each carrying a `cand:{candidate_id}` subject tag for identity
  reconciliation
- [Agent Memory](agent-memory.md) — the primitive map this feature sits
  alongside
