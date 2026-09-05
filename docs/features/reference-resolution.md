# Reference Resolution

Turns a candidate span's pronouns, relative dates, and definite references
into an audited rewrite — or an audited abstention — without ever guessing
silently.

## The gap this closes

[Auditable Extraction](auditable-extraction.md) (M3) writes the **verbatim**
candidate span to the provenance journal, untouched, on purpose — distillation
was explicitly out of scope there. But "she wants the report filed by Friday"
stored verbatim is nearly useless to retrieve later: nothing in the record
says who "she" is, which Friday, or which report. Reference resolution (M4,
[#563](https://github.com/tomcounsell/popoto/issues/563)) is the module that
picks up exactly where M3 left off — it runs *after* a candidate is accepted
and *before* it reaches the journal, rewriting the span into a
self-contained `statement` while keeping the original `verbatim` intact next
to it.

## The four-way status ladder

`ResolutionStatus` (`src/popoto/extraction/resolution.py`) replaces a numeric
confidence with four cases, ordered worst-last so a candidate's aggregate
status is the worst status among its individual references
(`ResolutionStatus.worst_of`):

| Status | Meaning |
|---|---|
| `resolved` | The reference was anchored with no ambiguity. |
| `assumed` | The stage anchored it, but only by stating an assumption (e.g. picking the most recent antecedent). |
| `evidence_gap` | Multiple plausible antecedents exist and the stage cannot pick one; the candidates are recorded and a clarifying question is posed. |
| `indeterminate` | Nothing in the window resolves the reference. |

One worked example per status, all against the same setting — Tuesday
2026-09-01, speaker `user`, previous turn *"How's Dana settling in on
Atlas?"*:

**`resolved`** — candidate *"she's been on Atlas since March"*: the pronoun
`she` resolves against the prior turn's "Dana", and the `relative_time`
reference `March` resolves to 2026-03-01 with no ambiguity. `statement` =
*"Dana's been on Atlas since March 2026-03-01."*

**`assumed`** — candidate *"she wants the report filed by Friday"* where the
window contains two named people and the model has to pick the most recently
mentioned one as the antecedent for `she`: the reference resolves, but
carries a one-line `assumption` ("assumed 'she' refers to the most recently
mentioned person, Dana") rather than a clean `resolved`.

**`evidence_gap`** — the same candidate's `the report` reference: two
plausible antecedents exist ("the Atlas onboarding report", "the Q3 status
report") and nothing in the window picks between them. The reference carries
`candidates=["the Atlas onboarding report", "the Q3 status report"]` and
`question="Which report does Dana need filed by Friday?"`.

**`indeterminate`** — a candidate referencing "the meeting we discussed" with
no meeting mentioned anywhere in the window: nothing resolves it, and no
assumption is stated because there is no plausible antecedent to assume.

Putting the first three references from the running example together: the
candidate *"she wants the report filed by Friday"* aggregates to
`evidence_gap` (the worst of `resolved`/`assumed`/`evidence_gap` among its
three references), with `statement` = *"Dana wants the report filed by Friday
2026-09-04."*, `verbatim` = *"she wants the report filed by Friday"*, and
journal tag `res:evidence_gap`.

## `TurnContext` — the capture header

```python
from popoto.extraction.resolution import TurnContext, WindowTurn

context = TurnContext(
    speaker="user",
    captured_at=1756742400.0,  # epoch seconds
    timezone="America/Los_Angeles",
    window=(
        WindowTurn(turn_id="t-40", speaker="agent", text="How's Dana settling in on Atlas?"),
    ),
)
```

`TurnContext` carries a speaker, a capture instant (epoch seconds), an IANA
timezone, and a bounded window of prior turns. `TurnContext.now()` builds the
degraded default — no speaker, no window, UTC, current clock — for a caller
that has none of this.

This header exists because nothing else in the system records *when and by
whom* something was said, as opposed to when it was stored. `JournalEntry`
has a `captured_at` field, but before M4 nothing populated it with anything
but the save clock; `TurnContext` is the seam that finally closes that gap —
`speaker` and a true `captured_at` reach the journal entry whenever a
`TurnContext` supplies them (`decision_log.py`'s `_append_and_transition`
backfills both from `resolution.context` unless the caller already supplied
an explicit `speaker`).

`captured_at` is coerced defensively: a missing, `NaN`, or infinite value is
replaced with the current clock in `TurnContext.__post_init__`, with a
logged warning — this is an M4 guard, not M1 behaviour, closing a fail-open
hole where a non-finite float could otherwise propagate into `valid_from`.

`TurnContext.bounded_window()` truncates the window to
`Defaults.M4_WINDOW_MAX_TURNS` turns and `Defaults.M4_WINDOW_MAX_CHARS`
characters, whichever binds first, dropping the *oldest* turns first, and
reports whether truncation happened — distinguishing "the window did not
contain the antecedent" from "the model missed it."

## The onset rule for `valid_from`

`valid_from` is only emitted for a very specific case: **exactly one**
reference in a candidate is a `relative_time` reference, has status
`resolved` or `assumed`, and carries a temporal role in
`Defaults.M4_VALID_FROM_ROLES` — which is `("onset",)` by default.
`TemporalRole` has four values: `onset`, `deadline`, `mention`, `none`; only
`onset` is in the role set.

**Why a deadline must not emit `valid_from`.** Under V0 membership
(`valid_from <= t AND invalid_at > t`, see
[ValidityField and Supersession](validity-and-supersession.md)), `valid_from`
is not "a date the claim mentions" — it is the instant the claim *becomes
retrievable*. "File the report by Friday" is true the moment it's said, on
Tuesday. Emitting Friday as `valid_from` would make the fact **invisible to
as-of retrieval until Friday** — the deadline would silently hide the very
obligation it describes, for exactly the window in which it matters. That is
why *"she's been on Atlas since March"* (an `onset` role) emits `valid_from`
= 2026-03-01, while *"filed by Friday"* (a `deadline` role) emits nothing:
the entry's `valid_from` falls back to M1's default, the capture instant.

**Two or more onsets in one candidate abstain.** If a single clause carries
two competing onsets (e.g. "she's been on Atlas since March and lead since
June"), nothing in the reference list determines which sub-claim the
statement is *about*, so no `valid_from` is emitted, the aggregate status
floors at `assumed`, and a stated assumption line names the competing
onsets (`_compute_valid_from` in `resolution.py`). A wrong `valid_from` is
silent and near-undetectable — it shifts a record's entire retrieval window
— whereas an absent one only costs precision.

**The role set is a pinned constant, not a code literal**, so a reversal is
a one-tuple change: `Defaults.M4_VALID_FROM_ROLES = ("onset", "deadline")`
would make deadlines emit too, with no other code change required.

## The `res:{status}` subject tag contract

Every resolved candidate's journal entry gets exactly one `res:*` subject
tag, computed by `Resolution.subject_tag`:

```python
resolution.subject_tag  # "res:resolved" | "res:assumed" | "res:evidence_gap"
                         # | "res:indeterminate" | "res:degraded"
```

This flag rides on the journal entry itself — the same low-entropy tag
convention `cand:{candidate_id}` uses (see
[Auditable Extraction](auditable-extraction.md#the-candidate-generator)) — so
an `assumed` fact can never be surfaced without its flag: querying
`JournalEntry.query.filter(subjects__all=["res:assumed"])` needs no sidecar
join.

**The fifth literal, `res:degraded`, takes precedence over the status
literal.** `degraded` is set whenever the resolution stage failed open — a
missing `anthropic` client, a raising client, a malformed reply, or the
`M4_RESOLUTION_ENABLED` kill switch — and in every one of those cases the
model never actually rendered a verdict. Without a separate literal, a
degraded run and a genuine model abstention would both tag
`res:indeterminate`, and a downstream reader could not tell "the model said
it could not resolve this" from "the resolution stage never ran." Because
`degraded` takes precedence, the two stay distinguishable on the one channel
guaranteed to travel with the fact — no sidecar read required.

## The `ResolutionRecord` sidecar

`ResolutionRecord` (`src/popoto/extraction/resolution_log.py`) is a Popoto
model holding the full evidence M7 ([#566](https://github.com/tomcounsell/popoto/issues/566))
is expected to consume:

- **Composite key.** `agent_id` / `turn_id` / `candidate_id` are all
  `KeyField`s (never `AutoKeyField`), mirroring
  `DecisionRecord` — a second write for the same tuple transitions the row
  in place instead of minting a duplicate.
- **`references_json` is JSON, not msgpack, on purpose** — every other
  Popoto model field is msgpack-packed by the base encoding, but this one
  field is deliberately re-encoded as a JSON string so the reference detail
  (surface offsets, resolved text, assumptions, candidate lists, clarifying
  questions) stays readable with plain `redis-cli HGET`, not just from
  Python.
- **The `TurnContext` header**, denormalized onto the row: `speaker`,
  `captured_at`, `timezone`, `window_truncated`.
- **No TTL.** Matching M3's decision log, rows are unbounded and never
  expire; retention is deferred to M9
  ([#568](https://github.com/tomcounsell/popoto/issues/568)).

```python
from popoto.extraction.resolution_log import ResolutionLog

log = ResolutionLog()
row = log.get("agent-7", "t-41", "t-41:sentence:0")
```

`ResolutionLog.write()` never raises — a Redis error or a bad `resolution`
shape is caught, logged, and turned into a `False` return, because the
sidecar is not load-bearing: by the time it runs, the candidate's accept
outcome and its `res:*` journal tag are already committed
(`decision_log.py`'s `_append_and_transition` writes the sidecar only after
a successful append, so it can carry `entry_id`).

## `verbatim` is byte-identical to the candidate span, always

`Resolution.verbatim` is the original candidate text, unmodified, on
**every** record — including the degraded path. A resolved candidate's
`statement` may be a rewrite; its `verbatim` never is. This is what makes a
wrong resolution non-destructive: the source span survives on the same
record regardless of what the resolution stage concluded, so a bad
`statement` can always be checked against — or discarded in favor of — the
exact words that were said.

## The `M4_RESOLUTION_ENABLED` kill switch

`Defaults.M4_RESOLUTION_ENABLED` is read fresh on every call (not cached),
default `True`, and overridable with the `POPOTO_M4_RESOLUTION_ENABLED`
environment variable, read at import time — the same deploy-level kill
switch pattern as `NEVER_RECORD_ENABLED`, for a PyPI adopter who cannot edit
model code.

**With the switch off, the auditable path's output is byte-identical to
M3's.** `SubconsciousMemory._extract_memories_auditable` checks the flag
*before* calling into resolution at all — no provider call, no `res:` tag,
no sidecar row:

- `statement == verbatim` (`resolution` is `None`, so assembly falls back to
  `candidate.text` for both).
- `valid_from == captured_at` (no `at` is threaded through, so the entry
  falls back to the interval's implicit start, the save clock — M1's
  default, unless `TurnContext.captured_at` was already the same instant).
- No `res:*` tag is appended to `subjects`.
- No `ResolutionRecord` row is written.

A caller invoking `resolve_references()` directly (rather than through
`SubconsciousMemory`) gets the same fail-open contract one layer down: the
function itself checks `M4_RESOLUTION_ENABLED` and, when it's `False`,
returns a `degraded=True` `Resolution` whose `statement` is byte-identical to
`verbatim` — the same "quality loss, not corruption" guarantee M3
established for its own fail-open paths.

## Quickstart

```python
from popoto.extraction.resolution import TurnContext, WindowTurn, resolve_references
from popoto.extraction.candidates import generate_candidates

turn_text = "she's been on Atlas since March"
candidates = generate_candidates("t-41", turn_text)

context = TurnContext(
    speaker="user",
    timezone="America/Los_Angeles",
    window=(
        WindowTurn(turn_id="t-40", speaker="agent", text="How's Dana settling in on Atlas?"),
    ),
)

resolution = resolve_references(candidates[0], turn_text, context)
print(resolution.status.value)      # "resolved"
print(resolution.statement)         # "Dana's been on Atlas since March 2026-03-01." (illustrative)
print(resolution.verbatim)          # "she's been on Atlas since March"
print(resolution.subject_tag)       # "res:resolved"
print(resolution.valid_from)        # epoch float for 2026-03-01 in America/Los_Angeles
```

`resolve_references` requires the optional `anthropic` package and an API
key to produce a non-degraded result; without either it returns the same
degraded `Resolution` the kill switch produces, matching M3's fail-open
contract.

## See Also

- [Auditable Extraction](auditable-extraction.md) — the candidate generator,
  verdict stage, and decision log this module sits downstream of; M3's
  "distillation is M4's job" note points here.
- [Provenance Journal](provenance-journal.md) — the `statement` /
  `verbatim` field pair and the `captured_at` / `valid_from` axes this
  module is a producer for.
- [ValidityField and Supersession](validity-and-supersession.md) — the V0
  membership rule (`valid_from <= t AND invalid_at > t`) that motivates the
  onset-only emission rule.
- [Tuning Magic Numbers](../guides/tuning-magic-numbers.md#reference-resolution-m4) —
  all eleven `M4_*` constants, including the `POPOTO_M4_RESOLUTION_ENABLED`
  kill switch.
