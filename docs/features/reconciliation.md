# Reconciliation — claim equivalence classes

`popoto.recipes.reconciliation` groups [provenance journal](provenance-journal.md)
entries that assert **one claim** into an *equivalence class*, resolves typed
contradictions inside a class through a per-type precedence table, and stores a
precedence tie as an explicit **disjunct pair** rather than picking an arbitrary
winner.

The journal records what an agent captured, immutably and with full attribution.
It does not notice that two entries say the same thing. That is this layer's job
— and only that: reconciliation groups and resolves, it never decides what a
reader should be shown.

## Three properties that shape everything here

**Nothing this module writes ever mutates a persisted `JournalEntry`.** An entry
composes `AppendOnlyMixin`, which refuses any re-save of an existing key —
including a partial `save(update_fields=[...])`. So class membership cannot be a
field on the entry: it lives in two ordinary models the module owns,
`ClaimMembership` and `ClaimClass`, where a relabel is an ordinary `save()`. The
only writes are `claim_type` (set by *capture*, before an entry's first and only
save), new appended annotation entries, and rows in those two models.

**The merge log is the source of truth; the two tables are a rebuildable
index.** Every outcome is appended to the journal as an immutable `merge` (or
`disjoin`) annotation carrying the class ids, a machine rationale, the timestamp,
the convention-book version and the claim slot. `replay()` discards the index and
recomputes it from those annotations alone. That is what makes reversibility
structural: retract a `merge` annotation, replay, and the pre-merge assignment is
back. A crash mid-relabel is a repair, not a corruption.

**One reconciler per agent, processing entries sequentially.** See
[the single-writer invariant](#deployment-the-single-writer-invariant) — it is a
deployment constraint, not an implementation detail.

## Capture assigns the claim type

`claim_type` is the only new field on `JournalEntry`, and it is *write-once at
capture*:

```python
from popoto.recipes.provenance_journal import ProvenanceJournal

ProvenanceJournal.append(
    agent_id="a1",
    statement="prefers morning meetings",
    subjects=["dana"],
    claim_type="preference",
)
```

The type vocabulary is a **frozen** seven-value enum:

| type | family | deterministic rule |
| --- | --- | --- |
| `deadline` | supersession | a same-slot collision *is* a conflict; newest wins |
| `preference` | stable | conflict only on a judged "different"; most-confirmed wins |
| `trait` | stable | as `preference` |
| `relationship` | stable | as `preference` |
| `goal` | stable | as `preference` |
| `procedure` | stable | as `preference` |
| `note` | rule-free | none — can only join, disjoin, or stay a singleton |

Frozen rather than open because decidability dies with an open enum: every type
but `note` needs a decidable incompatibility rule *and* a precedence row.
`note` is the rule-free catch-all that absorbs the tail, which is what lets the
enum be frozen without an escape hatch.

Omitting `claim_type` is legal and stores `None` — every entry captured before
this feature shipped has it, and nothing can back-fill a write-once field on an
append-only record. `normalize_claim_type()` maps `None`, `""`, and any
unrecognized string onto `note`, so such an entry is classified rather than
skipped.

## Claim slots carry no claim content

A *claim slot* is `sha256("{agent_id}|{subject}|{claim_type}")`, truncated to 32
hex characters. Grouping needs only slot *equality*, so the slot is stored
one-way and neither reconciliation model holds a subject string or a plaintext
type.

That is a correctness requirement, not caution. `JournalEntry.hard_delete()` is
the only erasure primitive an append-only record has, and its documented scope is
the record plus every trace of *its own* derived state — explicitly **not**
"every trace of the record anywhere in the keyspace". A plaintext subject on a
mutable sibling model would be exactly such an out-of-reach field-value copy, and
`JournalEntry` also composes `NeverRecordMixin`, so this data is already governed
as never-record.

## The two tiers

**Deterministic tier, zero LLM calls.** An exact `claim_slot` equality lookup
runs *before* any embedding work. For `deadline` — the only singleton-slot type —
a same-slot collision is a conflict by definition, so the precedence table
resolves it with the judge never consulted.

**Judge tier.** For the five stable types a same-slot pair may be a restatement
*or* a conflict, so a sameness judge distinguishes them under a pinned
convention book. Same-slot classes are offered first; an embedding shortlist,
capped at `Defaults.M5_SHORTLIST_CAP`, then supplies cross-slot candidates such
as a converse phrasing that names the subjects the other way round. With no
embedding provider configured the shortlist degrades to a bounded same-subject +
same-type index scan: recall narrows, every correctness property holds, and the
call bound is unchanged.

### The judge abstains rather than guesses

The judge follows the same contract as
[auditable extraction](auditable-extraction.md)'s verdict call: the never-record
firewall runs **before** the request, output is confined by a JSON schema and
then re-validated field by field, and the function never raises. A blank
statement costs **zero** calls. A malformed reply, an unreachable provider, or a
raising client all abstain — and an abstention leaves the entry a new singleton
class. Rule 8 of the convention book makes that the judge's default under
uncertainty too: abstaining costs one extra class, whereas a wrong "same" merges
two beliefs irreversibly from a reader's point of view.

### The symmetry probe

Judge verdicts are not transitive, and compounding false "same" verdicts into a
mega-class is the top threat to this design. So a forward "same" is re-asked
**once with the claim order swapped**, and the join commits only on same/same.
Any split — forward-same then probe-different, or a probe abstention — becomes an
explicit disjunct pair instead of a silent non-merge. Cost is bounded at two
calls per candidate class, so an entry costs at most `2 × M5_SHORTLIST_CAP`
calls. Full N-way transitivity closure is deliberately not implemented:
quadratic calls, no additional safety over this probe.

A second, non-gating line of defence reports damage the probe lets through: a
class absorbing more than `Defaults.MEGA_CLASS_VELOCITY_ALERT` joins in one pass
logs a warning. It is telemetry only and never refuses the join — a legitimately
large class must not be blocked.

## Precedence, and the tie that is not a coin flip

Once a type rule has fired, precedence resolves it:

1. **Rule 0, global and first** — a self-stated claim beats an inferred one
   (`JournalEntry.stated`). Applied for every type.
2. **Family order** — recency for the supersession family; confirmation count
   *then* recency for the stable family, so a claim corroborated many times does
   not lose to a single fresh mention.

The table is **total**: when Rule 0 ties, the family order ties and recency ties,
the outcome is an explicit disjunct pair. There is no arbitrary winner to hand a
reader, which is why `representative_for()` returns the uncertainty flag from
M5's own selection call rather than leaving it to a formatting layer:

```python
from popoto.recipes.reconciliation import ClaimClass, representative_for

for claim_class in ClaimClass.query.filter(agent_id="a1"):
    entry, uncertain = representative_for(claim_class.class_id)
```

The representative is the class's most-confirmed member **among validity-open
entries only**, ties broken by recency. A superseded loser stays in-class for
audit but is excluded from selection and from confirmation counts.

### Exactly one supersession mechanism

Every resolved conflict closes the loser through `ProvenanceJournal.supersede()`,
which appends a `supersede` annotation and closes the target's interval in one
`MULTI`/`EXEC`. It has to be that call rather than `save_and_supersede()` applied
to the winner: the winner is already persisted, so re-saving it would raise
`AppendOnlyViolation` — the same rule as "nothing here mutates a persisted
entry".

If the loser left live membership between the shortlist read and the write,
`ValidityMemberAbsentError` is caught, the loser re-read, and the merge log
records `loser-absent` while the winner stands. Nothing is deleted — the journal
is append-only — so the condition is a closed membership with the hash still
present, which an `EXISTS` check would pass straight through.

## Replay

```python
from popoto.recipes.reconciliation import replay

replay("a1", since=last_watermark)   # steady state
replay("a1", rebuild=True)           # from-genesis repair
```

`since` filters on `captured_at` with a strict `>`. `rebuild=True` deletes the
agent's index rows first, which is what a true from-genesis rebuild needs;
without it a replay is additive. Only `validity__current=True` annotations are
read, so a retracted `merge` is simply no longer in the log.

## Operator procedure: erasing a reconciled entry

**Use `erase_entry()`, not `JournalEntry.hard_delete()` directly.** The primitive
reaches the record and its own derived state; reconciliation adds derived state
outside that scope, so a bare `hard_delete()` leaves a dangling membership row
and a `ClaimClass` whose `representative_key` points at an erased key.

```python
from popoto.recipes.reconciliation import erase_entry

erase_entry(entry)
```

Four legs, in order:

1. `JournalEntry.hard_delete()` on the entry.
2. Delete its `ClaimMembership` row.
3. Delete its cached reconciler-side embedding — an embedding is a lossy encoding
   of `statement`, so the cache is content-derived state the primitive does not
   reach.
4. Recompute the affected `ClaimClass`: reselect `representative_key` and
   `member_count`, or drop the row when the class is left empty.

## Deployment: the single-writer invariant

The `StreamConsumer` on the journal's `"journal"` stream is the **only**
production trigger, and the deployment contract is **one consumer per agent**:

```python
from popoto.recipes.reconciliation import reconciliation_consumer

consumer = reconciliation_consumer(agent_id="a1", consumer_name="worker-1")
await consumer.run()
```

Entries are processed sequentially, and being the sole writer is what *produces*
the invariant: an entry's membership row is committed before the next entry is
shortlisted, so the interleaved shortlist-to-commit span the concurrent-join race
needs never occurs. That invariant is the sole mitigation for that race — an
atomic membership claim (`HSETNX`) and an advisory lock were both considered and
**withdrawn** in its favour, and neither appears in the module.

`reconcile_entry()` exists for tests to drive the same reconcile function
directly. It is **not** a production entry point: a host calling it from
concurrent turn handling has nothing establishing the sequencing, which re-opens
the race. Running a second reconciler for one agent is a deployment error that
requires reintroducing an atomic membership claim *and* per-claim-slot
serialization at the same time.

## Importers must register the kinds first

`merge` and `disjoin` are registered with `JournalEntry.register_kind()` at
`reconciliation.py` **import** time, because `_REGISTERED_KINDS` is
process-global and non-persisted: writing an unregistered kind raises from
`pre_save`, and a worker that reads the log without importing this module sees
those entries' kinds as unregistered. Both are registered `closing=False` — a
join or a disjoin closes nobody's interval — and non-targetless, so every
merge-log annotation must name a target.

Because the registry is process-global, the kind names are effectively reserved:
a second module registering `merge` with different flags is refused.

## Constants

All tuning values live in `popoto.fields.constants.Defaults` per the
[magic-numbers doctrine](../guides/tuning-magic-numbers.md), with module-level
aliases the code reads by name: `M5_SHORTLIST_CAP`, `M5_SYMMETRY_PROBE_ENABLED`,
`M5_JUDGE_MODEL`, `M5_JUDGE_MAX_TOKENS`, `M5_REPLAY_WATERMARK_FIELD`, and
`MEGA_CLASS_VELOCITY_ALERT`.

`CONVENTION_BOOK_VERSION` is recorded on every merge-log annotation. Changing any
line of `CONVENTION_BOOK_V1` is a **version bump, not an edit**: replay pins the
wording that produced a merge, so a silent reword would make history
irreproducible.
