# Auditable Extraction

An opt-in extraction path where every candidate fact — accepted, rejected,
privacy-blocked, or held back — ends up in a queryable decision log. Turn
extraction quality into a number you compute, not a number you guess at from
a hand-labelled sample.

## The gap this closes

`SubconsciousMemory.extract_memories()`'s default path (and the older
`ClaudeExtractionProvider` prompt) drop candidates silently: a sentence below
the length floor, a malformed model reply, a save that raises — each just
vanishes, with nothing recorded except maybe a `logger.warning`. There is no
way to compute precision or recall after the fact, because there is no
enumerated set of "things that were considered" to score against.

Auditable extraction (#562) replaces the single "extract and maybe drop"
step with three deterministic stages plus a write-side log:

```
response_text
  → candidates.py   deterministic candidate enumeration (no LLM, no drops)
  → M2 firewall      per-candidate scan_never_record()
  → verdict.py       one LLM call per surviving candidate, enum-only reply
  → decision_log.py  every candidate logged, accepted ones assembled into
                      the M1 provenance journal
```

Nothing here changes the default path. `SubconsciousMemory(agent_id=...)`
behaves byte-for-byte as before; this is `auditable_extraction=`, an opt-in
constructor argument.

## The candidate generator

`popoto.extraction.candidates.generate_candidates(turn_id, text)` is a pure
function: no Redis, no LLM, no network, no clock. Given the same
`(turn_id, text)` it always returns the same candidates, in the same order,
with the same ids — a decision log is only auditable if the thing it logs
decisions about is reproducible.

Two generator rules produce the v1 candidate set, in this order:

- **`sentence`** — one candidate per sentence span, using the same
  split regex as `HeuristicExtractionProvider`.
- **`entity`** — one candidate per pattern-lifted named entity (a run of
  capitalized tokens; single-token matches that open a sentence are skipped
  as orthography, not evidence).

Nothing is filtered here — short sentences, duplicate sentences, low-value
entities are all emitted, because *deciding to drop* a candidate is the
verdict stage's and firewall's job, not a silence produced by the generator.
An empty or whitespace-only turn produces zero candidates from this function;
the caller logs the single `reject`(`empty_turn`) row for it (see below).

Every candidate's `candidate_id` is `f"{turn_id}:{generator_rule}:{ordinal}"`
— deterministic and **deliberately low-entropy**. This matters beyond
readability: the id becomes a `cand:` subject tag written onto the
provenance journal entry (see [Candidate identity](#candidate-identity-on-the-journal-entry)
below), and the journal's own write-time firewall blocks high-entropy
subject tags (`popoto.privacy.never_record`'s `high_entropy` detector). A
hash-shaped id would be silently blocked there — never make `candidate_id` a
digest.

## The four terminal states, and the non-terminal `pending` marker

Every candidate ends in exactly one **terminal** state:

| State | Meaning |
|---|---|
| `firewall_drop` | Refused by the never-record privacy firewall — never stored. |
| `accept` | Content was written to the provenance journal, verbatim. |
| `reject` | The model (or trusted code) decided this isn't worth keeping. |
| `withhold` | The model judged it too hedged/ambiguous/context-dependent to store as-is. |

`pending` is a fifth value on the same row, but it is **not** a fifth
terminal state — it is an intent marker written *before* assembly calls
`ProvenanceJournal.append()`. This is the write-ordering guarantee the whole
module rests on:

> A candidate never reaches a side effect (a journal append) that has no
> decision-log row already describing it.

Concretely: `firewall_drop` / `reject` / `withhold` have no downstream side
effect, so their one write is directly terminal. `accept` is the only
two-phase path — the `pending` row is committed first, `ProvenanceJournal
.append()` is called second, and the *same* row is then transitioned to
exactly one terminal state based on what `append()` did:

| `append()` outcome | Terminal state | Reason code |
|---|---|---|
| succeeds | `accept` | `accepted` (row also carries the journal `entry_id`) |
| raises `JournalBlockedError` | `firewall_drop` | `post_accept_journal_block` |
| raises anything else | `reject` | `assembly_failed` |

Because the `pending` row is committed and durable before `append()` is
called, there is no path on which a candidate reaches the journal with zero
decision-log rows — and no path on which a crash between the two writes
loses the candidate's identity. A `pending` row that survives past
`extract_memories()` returning means a process died mid-assembly: a visible,
queryable, recoverable incident, which is the opposite of the silent drop
this module exists to close.

## Two `firewall_drop` reason codes

`firewall_drop` always means "the never-record privacy firewall refused
this," and the *reason code* is what tells you which of two firewall checks
fired — they run at different points against different values:

- **`pre_llm_candidate_block`** — M3's own per-candidate scan
  (`scan_never_record(candidate.text)`), run before the LLM ever sees the
  span. If this fires, the model never received the text.
- **`post_accept_journal_block`** — M1's write-time scan, which runs over
  values M3's per-candidate scan never sees (`agent_id`, the journal's
  subject tags, and the entry's own scan fields). The model *did* see the
  span and accepted it, and the journal refused the write anyway.

Both map to the same terminal `state` — `firewall_drop` never becomes a
generic bucket for write errors, so it keeps meaning exactly one thing
(privacy refusal). Every other assembly failure is `reject`(`assembly_failed`).
An offline query for "how often did privacy block content the model had
already agreed to store" is one `reason_code` filter.

## A third: the turn-level firewall block

The two reason codes above both pair with a *candidate* that exists. But
`extract_memories()` runs one more never-record check before candidates are
even generated: the **turn-level (M2) scan** over the whole `response_text`
(`popoto.privacy.never_record.scan_never_record`), inherited unchanged from
the non-auditable path (#561). An off-the-record marker anywhere in the turn
voids the *entire* turn — including facts that would have come from adjacent,
unrelated sentences — so it has to run before candidate generation, not
per-candidate.

On the auditable path, a turn-level block still produces exactly one
decision-log row rather than zero: `firewall_drop` /
**`turn_level_block`**, on a synthesized candidate
(`candidate_id=f"{turn_id}:turn_firewall:0"`, `generator_rule="turn"`), the
same pattern `_log_empty_turn` uses for a blank turn. `extract_memories()`
still returns `[]` and `last_extraction_privacy_dropped` is still set `True`
— nothing about the caller-visible return changes — but the decision log no
longer has a silent gap for the turn.

`turn_level_block` is a distinct reason code from `pre_llm_candidate_block`
on purpose: `pre_llm_candidate_block` means *candidates existed and M3's
per-candidate scan blocked one of their spans*; `turn_level_block` means
*M2's turn-level scan fired first and no candidates were ever generated for
this turn at all*. Both pair with the `firewall_drop` state, but only the
reason code tells you which scan — and which stage of the pipeline — did
the blocking.

## The enum-verdict contract

The LLM contributes exactly `{candidate_id, verdict, reason_code}` — three
enum-valued fields, never free text. `verdict.py`'s `_parse_reply()`
re-validates every field against the fixed vocabulary regardless of what the
request's JSON schema promised, so a provider that ignores or partially
honours the schema still cannot write an out-of-vocabulary value or a prose
fragment into the decision log. A reply naming a different `candidate_id`
than the one asked about is treated as malformed, not silently reassigned.

Accepted content is the **verbatim candidate span**, assembled by trusted
code — `verbatim` and `statement` on the journal entry are the same string
object as `candidate.text`, with no normalization, rewriting, or casing
change. Distillation (turning verbatim spans into cleaner stored statements)
is explicitly out of scope for M3 — it's M4's job.

The model may only emit `accept` / `reject` / `withhold` — never
`firewall_drop` (a trusted-code-only decision) or `pending` (an internal
write-ordering marker). Any reply claiming either is rejected at the parse
boundary and mapped to `reject`(`llm_unavailable`), the same bucket used for
a malformed reply, an unreachable API, or a raising client. That bucket is
deliberately separate from `not_a_fact` / `not_memorable` (a genuine model
rejection) so offline analysis never charges an infrastructure loss against
the model's measured recall.

## Retention policy

Decision-log detail rows are **unbounded** in v1, by deliberate decision —
there is no `LTRIM`, no cap, and no `Defaults` constant limiting them. The
right retention horizon can't be known until the M9 follow-on (below)
consumes the log at scale, so v1 declines to guess a number rather than
silently deleting audit evidence early.

The per-turn compact summary (`DecisionLog.turn_summary(agent_id, turn_id)`)
is a **convenience index over terminal states only** — an O(1) count instead
of a per-turn row scan — not a completeness fallback. If it ever disagrees
with the detail rows, the detail rows are right.

## The terminal-write conflict guard

Every terminal write — including the pre-LLM `firewall_drop`, which cannot
conflict in practice because no prior row exists for a fresh candidate — goes
through **one conditional Lua script**, run via `EVAL`. There is no
unconditional fast-path write anywhere in `decision_log.py`.

The rule it enforces: a terminal write must not overwrite a row that is
already terminal `accept` carrying a non-empty `entry_id`. If it would, the
write is refused, the existing `accept` row stands unchanged, and the
refusal is recorded on that same row as `detail_code = 'terminal_conflict_refused'`.
No second row, no new state, no exception raised to the caller.

This exists because the LLM verdict is **non-deterministic**: a retried
verdict call for the same candidate can resolve differently than the first
one did. Without the guard, a retry that resolves `reject` could clobber the
row of a candidate whose earlier `accept` had already been appended to the
journal — permanently splitting the decision log (the source of truth for
`compute_metrics`) from the journal (the source of truth for what's actually
stored). `MULTI`/`EXEC` can't implement this rule (it queues commands blind,
with nothing able to read `state` and branch on it), and the `SET NX`
assembly claim only buys mutual exclusion on a key, never inspects `state` —
hence the dedicated Lua script.

## The atomic assembly claim

Before writing a `pending` row, assembly claims the candidate with a single
atomic op:

```
SET popoto:m3:claim:{agent_id}:{turn_id}:{candidate_id} <token> NX PX <ttl>
```

`<ttl>` is `Defaults.M3_ASSEMBLY_CLAIM_TTL_MS` (30,000 ms), a pinned in-repo
constant — not a constructor kwarg, per this project's convention that
experimental-tuning numbers live in `popoto.fields.constants.Defaults`, not
as user-facing config surface.

The claim closes a TOCTOU window in the dedup probe: without it, two
runners racing the same `(agent_id, turn_id, candidate_id)` — a duplicated
delivery racing a crash-retry, both seeing a surviving `pending` row — could
both probe, both find nothing yet, and both append, producing two permanent
journal entries (the journal itself can't catch this: `append()` takes no
idempotency key and `AppendOnlyViolation` only fires when a record's Redis
key already exists). The claim's *loser* performs no journal write and no
row transition at all — it no-ops and leaves the candidate entirely to the
winner. Release is token-checked (a small Lua `if GET == token then DEL`) so
a runner can never delete a claim it no longer owns.

`SET NX PX` was chosen over `WATCH`/`MULTI` compare-and-set deliberately:
`WATCH` needs a dedicated connection held across the transaction plus a
retry loop, which doesn't compose with Popoto's shared connection pool.
Both are Valkey-safe (core commands and Lua only, no modules); `SET NX` is
smaller.

## Candidate identity on the journal entry

Every M3 journal append carries a `cand:{candidate_id}` subject tag. This is
the candidate's **identity** on the journal side, and it's what turns
reconciliation (matching a surviving `pending` row back to whatever the
prior, possibly-interrupted, assembly attempt did) into an identity lookup
rather than a text match:

```python
JournalEntry.query.filter(
    turn_id=candidate.turn_id,
    subjects__all=[f"cand:{candidate.candidate_id}"],
)
```

Matching on `verbatim` text instead is unsound: `verbatim` is not unique per
candidate within a turn by construction — a repeated sentence, or a
sentence span whose text happens to equal an entity-lifted span, produces
two candidates with identical `verbatim`. A `pending` row reconciled by text
could then attach to the *wrong* candidate's entry. Tag-based reconciliation
means two candidates with byte-identical text still reconcile onto their own
distinct entries. If the identity probe ever returns more than one match
(unreachable with correct tagging and per-turn-unique candidate ids, but
checked anyway), assembly refuses to guess: it writes `reject`
(`ambiguous_reconciliation`) with the matching entry ids recorded in
`detail_code`, and appends nothing.

## Recovering a stale `pending` row (manual in v1)

There is **no sweep, no TTL on decision rows, and no age alert** for a
`pending` row that outlives the process that wrote it. A TTL is
deliberately absent — it would delete the audit evidence this module exists
to keep. Recovery is a manual operator recipe:

```python
from popoto.extraction.decision_log import DecisionLog

log = DecisionLog()
for row in log.list_pending("agent-7", older_than=some_cutoff_timestamp):
    # oldest-first
    print(row.turn_id, row.candidate_id, row.written_at)
    # Re-invoke the auditable extraction path for this (agent_id, turn_id).
    # Assembly's identity probe (see "Candidate identity" above) reconciles
    # the stale row: it finds the prior append if it landed, or completes
    # it if it didn't.
```

A periodic age-keyed sweep, an alert threshold, or a dashboard over stale
`pending` rows is explicitly **not** v1 — it's a follow-on owned by M9
([#568](https://github.com/tomcounsell/popoto/issues/568)) or an ops
runbook, not this module.

## Wiring into SubconsciousMemory

`auditable_extraction=AuditableExtractionConfig(verdict_provider=..., journal=...)`
is a constructor argument on `SubconsciousMemory`, mutually exclusive with
`extraction_provider=` (the two pipelines don't compose). `journal` is
required — `AuditableExtractionConfig(journal=None)` (the field's own
default) raises `ValueError` **eagerly, at `SubconsciousMemory.__init__`**,
not on the first `extract_memories()` call: there is no journal to assemble
accepted candidates into, so a misconfiguration fails loud at construction
time rather than on whichever turn happens to produce the first `accept`.

`extract_memories(response_text, turn_id=None)` gained the `turn_id` keyword
for this path. It keys every candidate id, decision-log row, and journal
entry for the turn. Omit it and a fresh low-entropy id is generated per call
(`f"turn-{<epoch-ms>}"`); pass your own to correlate the decision log back
to an external turn/session id.

`importance=` (see [Tuning Magic Numbers](../guides/tuning-magic-numbers.md#subconsciousmemory-tier-4))
applies to the **default path only**. On the auditable path it is accepted
but silently ignored — every accepted candidate is assembled with
`importance=None`, because the whole point of this path is a decision log
that scores extraction quality itself; a caller-supplied importance opinion
would just be another unaudited number layered on top.

## Quickstart

```python
from popoto.extraction.decision_log import AuditableExtractionConfig, DecisionLog
from popoto.extraction.verdict import ReasonCode, Verdict, VerdictResult
from popoto.recipes.provenance_journal import ProvenanceJournal
from popoto.recipes.subconscious_memory import SubconsciousMemory


class KeywordVerdictProvider:
    """Toy stand-in for the real LLM verdict stage -- no API key required.

    Swap for `popoto.extraction.verdict.llm_verdict` (the default) once you
    have an ANTHROPIC_API_KEY configured.
    """

    def llm_verdict(self, candidate):
        if candidate.text.strip().endswith("."):
            return VerdictResult(candidate.candidate_id, Verdict.ACCEPT, ReasonCode.ACCEPTED)
        return VerdictResult(candidate.candidate_id, Verdict.REJECT, ReasonCode.NOT_A_FACT)


sm = SubconsciousMemory(
    agent_id="docs-example-agent",
    auditable_extraction=AuditableExtractionConfig(
        verdict_provider=KeywordVerdictProvider(),
        journal=ProvenanceJournal,
    ),
)

accepted = sm.extract_memories(
    "Popoto stores facts in Redis. Did Carol approve the release? "
    "The team shipped M3 today.",
    turn_id="docs-turn-1",
)
print(f"accepted {len(accepted)} candidate(s)")
for fact in accepted:
    print(f"  - [{fact.generator_rule}] {fact.text!r}")

# Precision/recall/F1 computed from the decision log alone -- no journal
# read, no LLM call, no other live Redis state.
gold_labels = {fact.candidate_id: True for fact in accepted}
metrics = DecisionLog().compute_metrics("docs-example-agent", gold_labels)
print(f"precision={metrics.precision:.2f} recall={metrics.recall:.2f} f1={metrics.f1:.2f}")
print(f"per_reason_code={metrics.per_reason_code}")
```

```
accepted 2 candidate(s)
  - [sentence] 'Popoto stores facts in Redis.'
  - [sentence] 'The team shipped M3 today.'
precision=1.00 recall=1.00 f1=1.00
per_reason_code={'accepted': 2, 'not_a_fact': 4}
```

Swap `verdict_provider` for `popoto.extraction.verdict.llm_verdict` (the
module default, requires `pip install popoto[anthropic]` and an API key) to
use the real per-candidate LLM verdict stage instead of the toy keyword
stand-in above.

## Why the v1 candidate shape is the one #510 measured worst — on purpose

[LLM Memory Extraction's evaluation section](llm-memory-extraction.md#evaluation-extraction-lost-to-raw-ingestion)
(PR #510) measured heuristic sentence-splitting at **0.2078** judged
accuracy against raw turn ingestion's **0.3636**, on the same slice. M3's v1
candidate shape is sentence spans (plus entity lifts) — the arm that
measured worst.

That tension isn't hand-waved. #510 measured a splitter that shipped with
*no per-candidate visibility*: every drop was invisible, so there was no way
to tell whether the loss came from over-fragmentation, from bad model
verdicts, or from something else entirely. M3 doesn't promote sentence spans
to the harness default — `RawTurnExtractionProvider` stays the default for
`popoto.integrations`, unchanged. What M3 does is make the measured-worse
shape's failure modes *queryable*: every drop now carries a `generator_rule`
and a `reason_code`, so "is fragmentation the problem, or is it something
else" becomes a `per_generator_rule` breakdown instead of a single opaque
aggregate number. Sentence spans are the smallest candidate shape that makes
the decision log informative — a raw-turn candidate is one candidate per
turn, with nothing left to discriminate between.

## See Also

- [LLM Memory Extraction](llm-memory-extraction.md) — the pluggable
  provider interface this path sits alongside, and the #510 evaluation in
  full.
- [Provenance Journal](provenance-journal.md) — the append-only store
  `accept`ed candidates are assembled into.
- [NeverRecordFirewall](never-record-firewall.md) — the privacy gate this
  module scans every candidate through, twice.
- [SubconsciousMemory Recipe](../guides/subconscious-memory-recipe.md) —
  where `auditable_extraction=` is wired in.
