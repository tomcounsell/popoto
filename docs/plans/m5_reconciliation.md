---
status: Ready
type: feature
appetite: Large
owner: agent-a773efcbae003f9f3
created: 2026-09-11
tracking: https://github.com/tomcounsell/popoto/issues/564
last_comment_id: 5537009267
revision_applied: true
revision_applied_at: 2026-09-14T03:04:34Z
critique_rounds: 2
---

# M5 — Reconciliation: claim equivalence classes, typed contradiction rules, explicit disjunctions

Tracking issue: #564.

## Problem

Popoto's agent-memory layer stores each extracted fact as an independent record;
nothing recognizes restatements or resolves contradictions structurally. After a
few turns the store holds three copies of "user prefers morning meetings" plus one
stale "user prefers evenings", and the read path injects all of them into the
prompt — the agent sees a self-contradicting brief with no signal about which
claim won.

**Current behavior:** Read-path dedupe is by redis key only
(`src/popoto/recipes/context_assembler.py:1951-1969`, drifted from the
`:1763-1781` cited in the issue), so two records asserting the same fact are both
injected. Contradiction handling in `ObservationProtocol._apply_contradicted`
(`src/popoto/fields/observation.py:373`, drifted from `:327`) now writes a
supersession edge when the model carries a `ValidityField` and the correcting
instance is supplied — but nothing ever supplies it from the memory path, so on
the journal it remains a scalar confidence nudge with no pointer to the winner.
The closest prior art, `TrajectoryMemory.crystallize()`
(`src/popoto/recipes/trajectory_memory.py:390-469`, tie-break at `:617-642`,
drifted from `:450-531` / `:637-643`), does fingerprint grouping plus a canonical
representative — but it is hardwired to trajectory sequences, has no class ids, no
merge log, and its tie-break always forces a single winner.

**Desired outcome:** After each turn's capture, same-subject same-type claims are
judged pairwise ("same claim under the convention book?") by the LLM against each
candidate class's representative. Sameness joins the class (a restatement confirms
instead of duplicating); a fired type-incompatibility rule triggers per-type
provenance precedence via `SupersessionProtocol.save_and_supersede`; a genuine tie
is stored as an explicit disjunct pair surfaced as uncertainty, never silently
broken. Every merge appends to an append-only merge log with rationale, making any
merge reversible by replay.

## Freshness Check

**Baseline commit:** `ea7fc584` (2026-09-11)
**Issue filed at:** 2026-08-13T06:28:40Z
**Disposition:** Minor drift

**File:line references re-verified:**
- `src/popoto/recipes/context_assembler.py:1763-1781` (key-only dedupe) — drifted
  to `:1951-1969`; claim still holds, dedupe is still by redis key only.
- `src/popoto/fields/observation.py:327` (`_apply_contradicted` scalar nudge) —
  drifted to `:373`; claim PARTIALLY STALE (see Notes). Function now writes a
  supersession edge when the model carries a `ValidityField` AND the correcting
  instance is supplied via `superseded_by` / `instance._superseded_by`.
- `src/popoto/recipes/trajectory_memory.py:450-531` (`crystallize`) — drifted to
  `:390-469`; tie-break drifted from `:637-643` to `:617-642`
  ("most recent trajectory wins"). Claim still holds: no class ids, no merge log,
  forced single winner.
- `src/popoto/fields/existence_filter.py:274` (probabilistic membership, no
  identity) — module restructured around `_compute_fingerprint_impl`; claim still
  holds, no identity resolution added.
- `grep -rn "class_id|merge_log"` over `src/popoto/` — zero hits. No equivalence
  classes, merge log, typed rules, or disjunction semantics exist anywhere.

**Cited sibling issues/PRs re-checked:**
- #560 (M1, dependency) — CLOSED 2026-08-19. `ProvenanceJournal` + `JournalEntry`
  landed with `stated` flag, `ValidityField`, `EventStreamMixin` ("journal"
  stream), and an explicit `register_kind` seam whose docstring anticipates "a
  merge/equivalence kind". Dependency now available; strictly strengthens plan.
- #580 (V0, dependency) — CLOSED 2026-08-17. `SupersessionProtocol` with
  `identity_key` / `save_and_supersede` / `SupersedeResult` landed. Available.
- #494 (adjacent dedupe signal) — CLOSED. Intentionally separate scope; unchanged.
- #563 (M4, sharpens sameness) — CLOSED. Reference resolution landed. Available.
- #588 / PR #601 (upstream notice, sole issue comment) — MERGED. Membership
  decision moved into `SUPERSEDE_LUA`; `supersede()` raises
  `ValidityMemberAbsentError` instead of returning `None`; new
  `save_and_supersede` combined entry point. Incorporated in Solution.
- Downstream #565 (M6), #566 (M7), #567 (M8) — all OPEN. No landscape change.

**Commits on main since issue was filed (touching referenced files):**
- `a4f7fbf4` feat(#580) V0 validity primitives (#582) — enables deterministic tier;
  does not implement classes.
- `90fc3d30` fix(#588) supersession membership in LUA (#601) — enables atomic
  winner-write; does not implement classes.
- `1d50bd83` assembler through field layer (#656), `c046e1bd` call-time client
  (#697), `f0c3d29f` unsaved-instance guards (#615), `16aa702e` production audit
  (#594) — all irrelevant to root cause.

**Active plans in `docs/plans/` overlapping this area:** none. No plan slug
mentions reconciliation, equivalence, disjunction, or contradiction.

**Notes:** The `_apply_contradicted` drift is the only substantive change: V0 gave
contradiction a provenance shape, but the memory write path never supplies the
corrector, so journal contradiction is still winner-less in practice. M5 remains
the module that detects the contradiction and names both sides. All corrected
line numbers are used inline in Technical Approach.

## Prior Art

- **#582 (V0 validity primitives, merged):** `SupersessionProtocol` in
  `src/popoto/fields/supersession.py` — `identity_key(subject, predicate)`,
  `supersede` / `invalidate`, combined `save_and_supersede` /
  `save_and_invalidate`, typed errors under `ValidityError`. This IS the
  deterministic tier's write path; M5 must call it, never reimplement closing.
- **#601 (supersession membership guard in LUA, merged):** membership decided
  atomically inside `SUPERSEDE_LUA`; absent member raises
  `ValidityMemberAbsentError`. M5's reconcile loop must catch this (the loser's
  *live membership* closed between shortlist and write — nothing is deleted; see
  Race 2) instead of relying on the old `None` contract.
- **`TrajectoryMemory.crystallize()`** (`src/popoto/recipes/trajectory_memory.py:390-469`):
  group → threshold → canonical representative → watermark idempotence. The shape
  M5 borrows; the specifics M5 rejects (fingerprint equality instead of judged
  sameness, no class ids, forced single winner at `:617-642`).
- **`ProvenanceJournal`** (`src/popoto/recipes/provenance_journal.py:535+`):
  `append` / `confirm` / `supersede` / `retract` façade over immutable
  `JournalEntry`; `register_kind` seam explicitly names "a merge/equivalence
  kind" as a future consumer. M5 registers merge kinds here rather than adding a
  parallel annotation store.
- **`src/popoto/extraction/verdict.py::llm_verdict`:** the in-repo LLM-judge
  pattern — firewall checks before the call, single call with JSON-schema
  `output_config`, re-validation of every reply field against fixed vocabularies,
  never raises / never returns `None` (every failure maps to a logged verdict).
  M5's sameness judge copies this contract shape.
- **`EmbeddingField`** (`src/popoto/fields/embedding_field.py`): provider-set
  embeddings cached as in-memory matrices for cosine shortlist; numpy optional.
  Note `JournalEntry` carries NO `EmbeddingField` today, and per D4 it does not
  gain one: the shortlist embeds reconciler-side over the entry's `statement`,
  with the bounded index-scan fallback when numpy/the provider is absent.
- **#558 (export/import round-trip, merged):** per-field round-trip fidelity
  precedent — the one new field M5 adds (`claim_type`) must round-trip through
  it. Class membership and disjunction links live in M5's own two
  reconciliation models, not as fields on `JournalEntry`, so they are covered
  by the replay-rebuilds-index check instead.

No prior issues found attempting equivalence classes themselves — no
**Why Previous Fixes Failed** section (greenfield module, not a re-fix).

## Research

No relevant external findings — proceeding with codebase context and training
data. Skipped per Phase 0.7 (purely internal work): the LLM-judge contract
(`extraction/verdict.py`), the embedding shortlist (`fields/embedding_field.py`),
and the stream trigger (`streams/consumer.py`, `fields/event_stream.py`) are all
in-repo patterns; no external library docs, ecosystem patterns, or migration
guides bear on the design.

## Data Flow

1. **Entry point**: post-turn background window, **stream-first** (D5). The
   production trigger is a `StreamConsumer` on the `"journal"` stream
   (metadata: `agent_id`, `kind`, `target`), waking on new `kind="assert"`
   entries. A public `reconcile_entry(...)` direct call exists as a *thin
   adapter over the same reconcile function* — the path tests drive, and the
   path a host that does not run a consumer can call. It is an entry point, not
   a second pipeline. Input either way is one fresh `JournalEntry`
   (`statement`, `subjects`, `stated`, `turn_id`). Both entry points funnel
   into the same reconcile function and run under the **single-writer
   invariant** (one reconciler per agent, sequential passes — Race 1), so
   there is no concurrent-entry-point hazard to arbitrate.
2. **Deterministic tier (zero LLM calls)**: compute the entry's identity key
   `subject|predicate` per the V0 amendment. Singleton-slot type rules (one
   birthdate per subject) and same-target deadline supersession express as
   identity-key collisions and resolve immediately through
   `SupersessionProtocol.save_and_supersede` — one MULTI/EXEC writing the
   winner, closing the loser, linking the chain, repointing the pointer.
3. **Embedding shortlist**: entries that find no deterministic collision are
   shortlisted to same-subject + same-type candidate classes via cosine
   similarity over the entry embedding (bounded N; judge calls scale with
   shortlist size, never with class count).
4. **LLM sameness judge**: the new claim is compared ONLY against each candidate
   class's most-confirmed member ("same claim under the convention book?"),
   prompted with the versioned convention book. Verdicts: same (join class,
   confirmation count += 1 via `ProvenanceJournal.confirm`), incompatible-type
   (fire the per-type rule → precedence → `save_and_supersede`), or genuine tie
   (write disjunct pair with shared disjunction id).
5. **Merge log**: every join / supersede / disjoin appends
   `{class_a, class_b, rationale, ts, judge_version}` as an immutable journal
   annotation (registered merge kind, naming a target entry). Replay = revoke
   entry + recompute class assignment. Classes are tracked by a plain relabeled
   `class_id` on M5's own `ClaimMembership` / `ClaimClass` rows (never a field
   on the append-only entry), never union-find. The merge log is authoritative;
   those two models are a rebuildable index over it.
6. **Output**: M6 reads one representative per class — the most-confirmed
   member among validity-open entries (`validity__current=True`; closed
   losers stay in-class for audit but are never selected and accrue no
   confirmations); without M5 it falls back to per-record. M7 consumes
   disjunct pairs as question sources; M8 pools causal estimates by
   `class_id`, falling back to by-type.

## Architectural Impact

- **New dependencies**: none vendored. New recipe module
  (`src/popoto/recipes/reconciliation.py`, name TBD at build) depends on
  `ProvenanceJournal`, `SupersessionProtocol`, `EmbeddingField`, and the
  `extraction/verdict.py`-shaped LLM client (anthropic extra, already optional).
  No new Redis modules — Valkey-safe by construction (memory:
  `feedback_valkey_compatibility`).
- **Interface changes**: additive only. `JournalEntry` gains exactly ONE new
  field, `claim_type` (IndexedField, assigned pre-first-save); `class_id` and
  disjunction links live on M5's two new models (`ClaimMembership`,
  `ClaimClass`) and in annotation payloads, not as fields on `JournalEntry`,
  because that model is append-only. New merge/disjoin kinds via
  `register_kind`. `ProvenanceJournal` gains no signature changes (reconciler
  calls existing `confirm` / `supersede`). M6/M7/M8 read `ClaimClass` (M6) and
  `ClaimMembership` (M7/M8) through an M5 accessor rather than querying a
  `JournalEntry` field; all degrade to per-record behavior when those tables
  are absent or empty.
- **Coupling**: reconciliation owns sameness; V0 owns closing; M1 owns
  annotation vocabulary. The amendment's invariant holds structurally: the
  deterministic tier calls `save_and_supersede`, so there is exactly one
  supersession mechanism and `class_id` subsumes identity keys (a deterministic
  hit is a class of size 2 merged without a judge call).
- **Data ownership**: the append-only merge log owns class membership
  authoritatively; the reconciler owns a rebuildable derived index over it, as
  the two models `ClaimMembership` and `ClaimClass`. Nothing to "keep in sync"
  — a divergent index is recomputed from the log, not reconciled against it.
  Single writer: one reconciler per agent. The derived tier holds **no claim
  content**: `ClaimMembership` carries a one-way `claim_slot` digest, never the
  subject text or `claim_type` (see the privacy rule in Key Elements), because
  `JournalEntry.hard_delete()` sweeps only the record's own derived state and
  would never reach content copied into a sibling model
  (`src/popoto/fields/append_only.py:242-294`).
- **Reversibility**: merge-log replay reproduces pre-merge assignment
  (acceptance criterion 4) — structurally, since the index is derived from the
  log. The module itself is removable: entries remain valid journal records
  (`claim_type` is inert without a reconciler) and the two models can be
  dropped wholesale. That is also the break-glass procedure — delete all
  `ClaimMembership` and `ClaimClass` rows (`Model.query.filter(...)` +
  `delete()`, or `DEL` on the two model key families) and M6/M7/M8 fall back to
  per-record behavior with zero journal data loss, because no journal record
  was ever mutated.

## Appetite

**Size:** Large

**Team:** Solo dev. The PM sign-off this plan originally budgeted for —
convention-book wording, type slots, precedence tables — is **spent**: those are
decided as D2 and D3 and written into the Solution section verbatim, so build
does not wait on them.

**Interactions:**
- PM check-ins: 0 required before build. A check-in is needed only to *change* a
  decision (Convention Book v1 wording is versioned config — a change is a
  version bump per Risk 2, not an edit).
- Review rounds: 2+ (judge-prompt wording, atomicity of class writes)

Solo dev work is fast — the bottleneck is alignment and review. Appetite measures communication overhead, not coding time.

## Prerequisites

No prerequisites — this work has no external dependencies. The LLM judge reuses
the existing optional `anthropic` extra; without it the deterministic tier still
functions and judge calls degrade to logged abstentions (same contract as
`llm_verdict`'s `LLM_UNAVAILABLE`). No API keys, services, or infra changes.

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli ping` | test isolation (DB 15 via pytest plugin) |
| embeddings extra for shortlist tests | `python -c "import numpy"` | cosine shortlist coverage |

## Solution

### Key Elements

- **Equivalence classes**: a `class_id` label, relabeled on merge, held in
  **two reconciliation-owned mutable Popoto models — never as a mutable field on
  `JournalEntry`**. `JournalEntry` composes `AppendOnlyMixin`
  (`src/popoto/recipes/provenance_journal.py:282`), whose `save()` raises
  `AppendOnlyViolation` on any re-save of an existing key — including a partial
  `save(update_fields=[...])`, which its own docstring calls out as "still an
  overwrite and ... still refused"
  (`src/popoto/fields/append_only.py:166-167`, guard at `:202-207`). A
  post-hoc `class_id` write to a persisted entry is therefore impossible
  through the ORM. The two-tier split follows V0's own precedent:
  `save_and_supersede` never re-saves the incumbent either — it closes it
  through `ValidityField.execute_supersede` against state the incumbent does
  not own (`src/popoto/fields/supersession.py:720-737`). The mutable tier is
  two ordinary (non-append-only) Popoto models M5 owns outright, so a relabel
  is an ordinary `save()` rather than a hand-rolled key write:

  ```python
  class ClaimMembership(Model):          # one row per reconciled entry
      entry_redis_key = KeyField()       # the JournalEntry's redis_key
      class_id = IndexedField(type=str)
      claim_slot = IndexedField(type=str)            # digest, see below
      disjunction_id = IndexedField(type=str, null=True)

  class ClaimClass(Model):               # one row per class — M6's read surface
      class_id = KeyField()
      agent_id = IndexedField(type=str)
      representative_key = IndexedField(type=str)
      member_count = IntField(default=1)
      updated_at = FloatField(null=True)
  ```

  Two things make this preferable to the hand-rolled companion hashes and sets
  an earlier draft specified. First, every read M6/M7/M8 needs is an ORM query
  against an index (`ClaimClass.query.filter(agent_id=...)`,
  `ClaimMembership.query.filter(class_id=...)`) instead of an accessor wrapping
  `HGET`/`SMEMBERS`, so `ClaimClass` is a *real* read surface for M6 rather
  than a key convention M6 has to trust. Second, `KeyField` gives the
  membership row the exact identity semantics wanted — one row per entry,
  keyed by the entry's own `redis_key`. The colons inside that key are a
  non-issue: `DB_key.clean()` escapes `":"` to `COLON_ESCAPE`
  (`src/popoto/models/db_key.py:43` and `:191`) before it reaches the
  keyspace, so a composite key value cannot forge a key boundary.

  **Privacy rule — `ClaimMembership` stores a digest, never claim content.**
  `claim_slot = sha256(f"{agent_id}|{subject}|{claim_type}").hexdigest()[:32]`,
  computed at reconcile time and stored one-way. It must NOT store the subject
  string or `claim_type` in plaintext, and neither must `ClaimClass`. The
  reason is `hard_delete()`'s documented scope: it erases "a record and every
  trace of its own **derived state**", and its docstring says outright that the
  scope "is **not** 'every trace of the record anywhere in the keyspace'", the
  surviving references being safe precisely because "neither carries the
  record's field *values*"
  (`src/popoto/fields/append_only.py:242-294`). A plaintext subject on a
  mutable sibling model would be exactly such a field-value copy, outside the
  reach of the only erasure primitive the append-only record has — a privacy
  regression, and a sharper one because `JournalEntry` also composes
  `NeverRecordMixin` (`src/popoto/recipes/provenance_journal.py:282`), i.e.
  this data is already governed as never-record. A digest keeps what
  reconciliation actually needs (slot *equality*, for grouping sibling claims)
  and discards what it does not (the text). The matching erasure cascade is
  specified in **Documentation** and gated by a named test.

  A restatement joins the class
  and increments its confirmation count via `ProvenanceJournal.confirm`, which
  is already append-only-safe: it appends a new `kind="confirm"` annotation and
  leaves the target untouched (`src/popoto/recipes/provenance_journal.py:646-661`),
  so the count is *derived* by counting annotations, never stored on a record.
- **Per-entry claim type**: `claim_type = IndexedField(type=str, null=True)`
  on `JournalEntry` — this one IS a real field, and legally so: capture assigns
  it *before the entry's first and only `save()`*, so append-only is satisfied.
  It is the only new `JournalEntry` field M5 adds; `class_id` and the
  disjunction links live in the two reconciliation models per the bullet above.
  **Write-once-at-append, never back-filled**: capture assigns it at capture
  time from the extractor's type label (never inferred later), and M5 never
  writes it — there is no code path that could, so "back-fill the type on old
  entries" is not a build option. The corollary the reconciler MUST implement:
  every entry captured before this ships has `claim_type=None`, so the
  reconciler **tolerates `None` and falls back to `note`** (the rule-free
  catch-all) rather than skipping the entry or raising. The deterministic
  tier derives the V0 predicate as `(subjects[0], claim_type)` — zero LLM
  calls, zero guessing. It round-trips through export/import per the #558
  precedent, and it is the only new *field* needing that coverage. The two
  reconciliation models are plain Models, so they neither need nor may have a
  `roundtrip_policy` declaration — the transfer declaration guards target
  `Field` subclasses and model-level *mixins*, not plain Models
  (`tests/test_transfer_roundtrip.py:729` and `:754`) — and their round-trip
  correctness is covered by the `replay_rebuilds_index` check instead, because
  the merge log they derive from is journal data that already exports with the
  journal.
- **Convention book**: short versioned config standard for "same claim"
  (converse phrasings merge, restatements confirm). The only prompt the judge
  sees; version recorded on every merge-log entry so replays are reproducible.
  The concrete v1 text is fixed below in **Convention Book v1** — build it
  verbatim rather than paraphrasing it (D2).
- **Frozen type enum**: `preference | deadline | trait | relationship | goal |
  procedure | note`, where `note` is the rule-free catch-all that absorbs the
  tail (decidability of rules dies with an open enum — recon Dropped bucket).
  `note` never fires a type rule at all: it has no incompatibility check and no
  precedence row, so a `note` claim can only ever join, disjoin, or stay a
  singleton via the judge path (D2/D3).
- **Typed contradiction rules**: per-type decidable checks with zero LLM calls
  (singleton slots, same-target deadline supersession). Firing a rule resolves
  through the per-type precedence table and writes via
  `SupersessionProtocol.save_and_supersede` — exactly one supersession
  mechanism.
- **Provenance precedence**: per-type ordering, given in full in **Precedence
  Table (v1)** below (D2). Rule 0 is global — self-stated beats inferred for
  every type (consumes M1's `stated` flag) — and the per-type row breaks ties
  under it: recency for the supersession family (`deadline`), confirmation count
  for the stable family (`preference`, `trait`, `relationship`, `goal`,
  `procedure`) with recency as the final tiebreak. The table is **total**: when
  every column ties, the outcome is a disjunct pair, never an arbitrary winner.
- **Disjunct pairs**: precedence ties stored as a **`disjoin` merge-log
  annotation entry naming both sides plus a shared disjunction id** — an
  *append*, not a field write on either entry (the second side would otherwise
  hit the same `AppendOnlyViolation`). The annotation is authoritative and is
  the complete history; for retrieval, both sides' `ClaimMembership` rows carry
  the shared `disjunction_id`, so M7 gets a pair with one indexed query
  (`ClaimMembership.query.filter(disjunction_id=...)` → two rows). v1
  simplification: the membership row holds the *most recent* open disjunction
  for that entry, so an entry disjoined a second time repoints it; the full set
  of pairs is always recoverable from the `disjoin` annotations
  (`JournalEntry.query.filter(kind="disjoin")` — `kind` is an `IndexedField`,
  `src/popoto/recipes/provenance_journal.py:309`), which is what replay reads.
  This module's own contribution —
  V0 always produces a deterministic winner within one identity key, so ties
  arise only at the judgment layer and stay here.
- **Append-only merge log**: `{class_a, class_b, rationale, ts, judge_version}`
  as immutable journal annotations (registered merge kind). Replay reproduces
  pre-merge assignment.

### Convention Book v1

Resolved in this revision (D2). This is the literal v1 standard: it ships as a
versioned config constant (`CONVENTION_BOOK_V1`, version string `"v1"`), it is
the only thing the judge is prompted with besides the two claims, and its
version is recorded on every merge-log entry so a replay pins the wording that
produced the merge. Changing any line of it is a version bump, not an edit —
Risk 2 depends on that.

> **Same-claim convention book, v1.**
>
> Two claims are the **same claim** when they assert the same thing about the
> same subject, such that a reader who believed one would consider the other a
> restatement rather than new information. Specifically:
>
> 1. **Converse phrasings are the same claim.** "A reports to B" and "B manages
>    A" assert one fact from two directions.
> 2. **Restatements and paraphrases are the same claim**, including changes in
>    wording, tense, politeness, verbosity, or the presence of hedging.
> 3. **Differences in precision are the same claim** when the less precise
>    statement is entailed by the more precise one and the claims do not
>    conflict ("prefers mornings" / "prefers meetings before 10am").
> 4. **Different subjects are never the same claim**, even under identical
>    predicates.
> 5. **Different values in the same slot are NOT the same claim** — they are a
>    conflict, and the type rule decides, not this judge ("prefers mornings" /
>    "prefers evenings").
> 6. **Different predicates about one subject are NOT the same claim**, however
>    related ("lives in Berlin" / "works in Berlin").
> 7. **A claim about a moment and a claim about a pattern are NOT the same
>    claim** ("was late today" / "is often late").
> 8. **When the two claims are not clearly on one side of the rules above,
>    answer `different`.** Abstaining costs one extra class; a wrong `same`
>    merges two beliefs irreversibly from the reader's point of view.

Rule 8 is the load-bearing one for Risk 1: the judge's default is to *not*
merge, so the failure mode under uncertainty is a duplicate class (today's
behavior) rather than a mega-class.

### Precedence Table (v1)

Resolved in this revision (D2). Applied ONLY after a type rule fires — this
table never decides sameness, and it never runs on a `note`.

**Rule 0 (global, all types):** a self-stated claim beats an inferred one
(M1's `stated` flag). Evaluated first for every type; the per-type row below
applies only when both claims agree on `stated`.

| `claim_type` | Family | Per-type order (after Rule 0) |
|---|---|---|
| `deadline` | supersession | recency (later `captured_at` wins) |
| `preference` | stable | confirmation count, then recency |
| `trait` | stable | confirmation count, then recency |
| `relationship` | stable | confirmation count, then recency |
| `goal` | stable | confirmation count, then recency |
| `procedure` | stable | confirmation count, then recency |
| `note` | rule-free | n/a — no incompatibility rule, so precedence never runs |

The three rows the critique flagged as unspecified (`relationship`, `goal`,
`procedure`) resolve to the **stable** family: all three describe standing facts
that get restated, so a claim confirmed many times should not lose to a single
fresh mention. `deadline` is the only member of the supersession family, and
membership is not a judgment call — it is exactly the set of types whose
deterministic rule is same-target supersession, which is what makes "the newest
assertion is the truth" correct for it and wrong for the others.

**Totality:** if Rule 0 ties, the family order ties, and recency ties (identical
`captured_at`), the outcome is a **disjunct pair**, not a coin flip. This is
what makes AC3's "no silent winner" hold as a property of the table rather than
as a hope about the data.

### Flow

**Post-turn capture** → new assert entry → **Deterministic tier** (identity-key
collision? → `save_and_supersede`, done, zero LLM calls) → **Embedding
shortlist** (same subject + type, bounded N) → **Judge vs each class
representative** (most-confirmed validity-open member only) → same → **join + confirm** /
rule fires → **precedence + supersede** / tie → **disjunct pair** → **append
merge-log entry** → M6/M7/M8 read classes downstream.

The new/revision/restatement/contradiction classification falls out of pipeline
order and is never asked as its own question.

### Technical Approach

- **Class membership lives in two M5-owned models, and the merge log is
  authoritative.** The shape is declared in Key Elements: `ClaimMembership`
  (keyed by `entry_redis_key`, carrying `class_id`, `claim_slot`,
  `disjunction_id`) and `ClaimClass` (keyed by `class_id`, carrying `agent_id`,
  `representative_key`, `member_count`, `updated_at`). Both are plain
  `Model`s — no `AppendOnlyMixin` — so every write is an ordinary `save()` and
  every read is an ordinary indexed query. No hand-rolled key layout, no
  `HSET`/`SMEMBERS`/`SUNIONSTORE` sequence, and therefore nothing outside the
  ORM to keep Valkey-safe: popoto's own writer issues only core commands. Where
  M5 does touch Redis directly it goes through `get_redis()` /
  `get_REDIS_DB()`, never a `POPOTO_REDIS_DB` import (memory:
  `project_new_code_must_use_get_redis_db`).

  The **merge log is the source of truth**; these two tables are a *rebuildable
  index* derived from it. That is what answers the "no sidecar to keep in sync"
  objection the earlier draft raised against itself: there is nothing to keep in
  sync, because a divergent index is discarded and recomputed from the log
  rather than reconciled against it. It also makes acceptance criterion 4
  (reversibility by replay) structural rather than a property to be tested into
  existence — replay rebuilds the index by construction. A crash mid-relabel is
  therefore a repair, not a corruption: rerun replay for that class.
  Relabel-on-merge is `ClaimMembership.query.filter(class_id=loser)` → set each
  row's `class_id` to the winner and `save()` → update the winner's
  `ClaimClass` counts → delete the loser's `ClaimClass` row. Safe under the
  single-writer invariant (one reconciler per agent) and idempotent on re-run,
  since a row already relabeled matches the winner's filter instead.
- **Nothing M5 writes ever mutates a persisted `JournalEntry`.** Build-time
  rule, stated once so no builder has to rediscover it: the only legal writes
  are (a) fields set before an entry's first `save()`, (b) new appended
  annotation entries, (c) rows in M5's own two models above. Any design
  that needs to "update" an entry is wrong by construction — `AppendOnlyMixin`
  refuses it, and `hard_delete()` is scoped to "retention and erasure, not for
  test teardown" (`src/popoto/fields/append_only.py:86-93`) and is not a route
  around this.
- **Register the merge kinds at `reconciliation.py` import, and document it.**
  `_REGISTERED_KINDS` is a module-global, non-persisted registry
  (`src/popoto/recipes/provenance_journal.py:228`), and *writing* an entry with
  an unregistered kind raises `ValueError` in `pre_save` — its docstring records
  this as a real bug found in PR #589 review, noting "**A restoring or importing
  process must call the same** `register_kind` **calls before importing**"
  (`:360-371`). So: call `JournalEntry.register_kind("merge", closing=False)` /
  `("disjoin", closing=False)` at module import of `reconciliation.py`.

  Both flags matter, and `closing` is NOT the docstring's example value
  (`register_kind("merge", closing=True)` at `:351`). `closing=False`
  because a join or a disjoin does **not** close anybody's validity interval —
  only the deterministic/precedence path closes, and it does so through
  `save_and_supersede`, not through a merge kind. And `targetless=False` (the
  default) means **every merge-log annotation MUST name a `target`**:
  `validate_kind_and_target` raises `ValueError` — "a {kind!r} entry annotates
  another entry and must name a target" — on a falsy target for a non-targetless
  kind (`src/popoto/recipes/provenance_journal.py:527-531`), and it is called
  from `pre_save`, so this fails at write time, not at review time. Target the
  entry the record is *about*: for `merge`, the joining entry E; for `disjoin`,
  one side of the pair, with the other side and the shared disjunction id
  carried in the annotation payload. `targetless=True` is not an option —
  `register_kind` rejects `targetless and closing` together, and a targetless
  kind must carry no target at all, which would strand the annotation with
  nothing to hang off. Also say, in the feature docs, that any process which
  *writes* or *imports* merge-log
  annotations (a transfer restore, a backfill, a rolling deploy running the old
  image) must import `reconciliation.py` first. Reading is unaffected — an
  unrecognized kind reads back inert for membership per the reader rule, so an
  old reader degrades safely rather than failing.
- **Deterministic tier is a special case of `class_id`, never a parallel
  mechanism** (V0 amendment): an identity-key hit assigns both entries the same
  `class_id` and writes the outcome through `save_and_supersede`. Singleton-slot
  rules (one birthdate per subject) express as identity keys. The judge is the
  escalation path for claims normalization cannot equate.
- **Judge verdicts are not transitive — budget one symmetry re-check.**
  Recon and the design study both flag compounding false-"same" verdicts
  (mega-classes) as the top threat. The plan decision (confirmed as D1): the forward ask
  compares entry E against class C's representative in that order
  ("is E the same claim as C's representative?"); after a forward "same",
  the symmetry probe re-asks once with SWAPPED claim order
  ("is C's representative the same claim as E?"). The join commits only on
  same/same; ANY split (forward-same/probe-different, or probe abstention)
  routes to a disjunct pair instead of a join. Cost is bounded at most 2x
  the shortlist cap per entry (one forward + one probe ask per candidate),
  and the probe converts the worst failure (silent mega-class) into the safe
  failure (explicit uncertainty). Full N-way transitivity closure is a No-Go —
  quadratic judge calls for no additional safety over the symmetry probe.
- **Representative discipline**: the judge always sees the class's
  most-confirmed member among validity-open entries only
  (`validity__current=True` filter, per the `JournalEntry` docstring's own
  query precedent) — validity-closed losers stay in-class for audit but are
  excluded from representative selection AND from confirmation counts.
  Ties in confirmation count break by recency (mirrors `crystallize`'s
  modal+recency shape without copying its forced-winner semantics).
- **`ValidityMemberAbsentError` handling** (per #601 notice): the reconcile loop
  catches it around `save_and_supersede` — a loser whose live membership closed
  between shortlist and write is re-read; if it is no longer a live validity
  member, the merge-log records `loser-absent` and the winner stands. Never a
  silent skip, never a crash. Per the Race 2 note, the loser's hash is still
  present throughout: the condition is closed membership, not deletion, so the
  detection is the caught exception and never an `EXISTS` check.
- **Replay cost bounded by watermark**: merge-log replay reprocesses only
  entries newer than the last replay watermark (borrow `crystallize`'s watermark
  shape: strict-`>` filter on `captured_at`, stored per reconciler run). Full
  from-genesis replay stays available as an explicit repair operation, not the
  steady state.
- **Embeddings are reconciler-side; `JournalEntry` stays embedding-free** (D4).
  M5 adds NO `EmbeddingField` to M1's model. The shortlist computes/caches
  embeddings over the entry's `statement` inside the reconciler and keys them by
  `entry_redis_key` in reconciler-owned state, alongside the two reconciliation
  models. This is the same ownership line the append-only remedy draws — M5
  owns derived state, M1 owns the record — and it keeps `claim_type` the only
  new field, which is what makes the "exactly one new field" claim in
  Architectural Impact true rather than approximately true. The cost is that a
  re-embed is needed after a cache loss; that is acceptable because the cache is
  derived state with a correctness-preserving fallback (below), not a source of
  truth. An `EmbeddingField` on `JournalEntry` would also have been legal under
  append-only (it populates on the first and only `save()`), so this is a
  scope/ownership decision, not a constraint — recorded so a builder does not
  "fix" it by adding the field. One consequence follows from the Key Elements
  privacy rule: an embedding is a lossy encoding of `statement`, so the
  reconciler-side cache is content-derived state in a store `hard_delete()`
  does not reach, and it joins the erasure cascade — erasing an entry drops its
  cached vector along with its `ClaimMembership` row.
- **Embedding fallback**: if numpy/provider is absent, the shortlist degrades
  to same-subject + same-type index scan (bounded by `Defaults` cap) with zero
  judge-call change — recall narrows, correctness properties hold.
- **Numeric tuning constants live in `Defaults`** (per repo magic-numbers
  doctrine) and every new one is registered in
  `tests/benchmarks/test_defaults_sync.py`: shortlist cap, symmetry-probe
  on/off, replay watermark field, judge model/token caps (mirroring
  `VERDICT_MODEL` / `VERDICT_MAX_TOKENS`), and `MEGA_CLASS_VELOCITY_ALERT`
  (Risk 1 telemetry). Registration is not optional bookkeeping:
  `test_all_defaults_covered_by_module_constants` collects *every* uppercase
  `Defaults` attribute and fails on any that has neither a module-level alias
  in `MODULE_CONSTANTS` nor an entry in that test's explicit
  exemption set (`tests/benchmarks/test_defaults_sync.py:38-201`) — and a
  narrow lane test selection never runs it, so the failure surfaces in CI after
  review (memory: `project_defaults_sync_gate`). This is also why the
  round-2 `RECONCILE_LOCK_TTL_SECONDS` constant is **not** in that list: with
  the advisory lock withdrawn (Race 3) the constant has no reader, and an
  unregistered constant is a red gate rather than harmless dead weight.
- **Judge failure contract copies `llm_verdict`**: firewall before the call,
  JSON-schema output, re-validate every field, never raise — failures map to a
  logged abstention that leaves the entry unclassified (new singleton class),
  never to a guessed join.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] The reconcile loop's `except` around `save_and_supersede`
  (`ValidityMemberAbsentError` → re-read → `loser-absent` log) must have a test
  asserting the merge-log entry, not a silent pass. Same for the judge
  abstention path (logged, entry stays singleton).
- [ ] Audit `except Exception` blocks in touched journal/supersession call sites
  during build; each needs an observable-behavior assertion.

### Empty/Invalid Input Handling
- [ ] Empty/whitespace-only `statement` never reaches the judge (firewall, same
  as `llm_verdict`'s `reject`/`empty_turn`); test asserts zero judge calls.
- [ ] Entries with empty `subjects` skip the deterministic tier (no identity key
  computable) and enter the judge path with subject-unbounded shortlist capped
  by `Defaults`; test covers the cap.
- [ ] Malformed judge replies (wrong schema, verdict outside fixed vocabulary,
  reply about a different class) map to abstention — copy `verdict.py`'s
  `_parse_reply` adversarial tests.

### Error State Rendering
- [ ] Disjunct pairs and supersession pointers must surface in the M6-facing
  representative read as explicit uncertainty markers; the test asserts the
  flag on M5's own representative-selection return value — NOT at M6's
  formatting layer (M6 is #565's module per the No-Gos). No silent winner at
  write or read time.

## Test Impact

New module, one additive `JournalEntry` field, and two new M5-owned models; no
existing behavior changes, so no UPDATE/DELETE — but three adjacent suites
constrain the build:

- [ ] `tests/` journal suites (`test_provenance_journal.py` et al) — MUST PASS
  UNCHANGED: `claim_type` defaults NULL, no entry is ever mutated, merge kinds
  are additive registrations.
  Any failure here is a regression, not an expected update.
- [ ] `tests/test_ci_workflow_redis_url.py`-style env contracts — new tests bind
  via the pytest plugin (`popoto_test_db`), never `REDIS_URL` + DB 0.
- [ ] `tests/benchmarks/test_defaults_sync.py` — MUST UPDATE: every new
  `Defaults` constant registered (shortlist cap, probe flag, watermark,
  judge caps). This gate fails only in CI under narrow test selection, so the
  build must run it explicitly.
- [ ] New `tests/test_reconciliation_m5.py` (name per issue's
  `tests/test_<name>.py` criterion): restatement-joins, rule-fires-supersedes,
  tie-disjoins, replay-reverses, zero-LLM-calls-for-rules, judge-call bound,
  plus the Verification-table suites — `append_only` (no entry is ever
  mutated), `replay_rebuilds_index`, `no_m6_surfacing`, `command_allowlist`,
  `membership_row_holds_no_claim_content`, `hard_delete_cascades` — and the two
  Race 3 cases (siblings processed in sequence land in one class; the sibling is
  found by `claim_slot` equality with the embedding shortlist stubbed empty).

No existing tests affected otherwise — greenfield recipe with additive-only
journal changes.

## Rabbit Holes

- Full N-way transitivity closure over classes (quadratic judge calls; the
  symmetry probe buys the safety cheaper).
- General entailment checking ("does claim A entail claim B?") — unreliable on
  small judges and unneeded; narrow sameness + type rules cover all four
  outcomes (recon Dropped bucket).
- LLM-extensible type schema — decidability of rules dies with an open enum;
  `note` absorbs the tail (recon Dropped bucket).
- Persisted union-find for classes — plain `class_id` relabel + merge log at
  one-person scale (recon Revised bucket).
- Rewriting `crystallize()` to share code with the reconciler — trajectory
  fingerprints and claim sameness only rhyme; a shared abstraction couples both
  to neither's benefit. Borrow the shape (watermark, modal+recency), not the code.
- Backfilling classes over the entire historical journal at ship time — steady
  state is incremental; genesis replay is an explicit repair op.

## Risks

### Risk 1: Judge non-transitivity compounds into mega-classes
**Impact:** One class absorbs unrelated claims; downstream belief sheets present
merged falsehoods as confirmed fact.
**Mitigation:** Symmetry probe on every join (split verdict → disjunct pair);
representative discipline (most-confirmed member only); merge log makes any bad
join reversible by replay. Mega-class detector (class size velocity alert) as
telemetry, not a gate.

### Risk 2: Convention-book wording silently moves the sameness boundary
**Impact:** Same code, different merges after a wording tweak; irreproducible
history.
**Mitigation:** Convention book is versioned config; version recorded on every
merge-log entry; replay pins the entry's version. Wording changes need PM
sign-off (appetite check-ins).

### Risk 3: Post-turn background window overruns under burst capture
**Impact:** Reconciliation lags; read path serves unreconciled duplicates.
**Mitigation:** Judge calls bounded by shortlist cap; deterministic tier absorbs
the common collisions with zero calls; lag is safe (unreconciled reads are
today's status quo, and M6 falls back to per-record).

### Risk 4: Embedding provider absent in consumer environments
**Impact:** No cosine shortlist; judge path falls back to coarse index scan.
**Mitigation:** Bounded subject+type scan fallback (correctness holds, recall
narrows); deterministic tier unaffected. Documented in feature docs.

## Race Conditions

### Race 1: Two reconciler runs process the same fresh entry
**Location:** new `recipes/reconciliation.py` run loop
**Trigger:** Overlapping post-turn triggers (stream redelivery + direct call)
both shortlist and judge the same entry concurrently.
**Data prerequisite:** Entry persisted in journal before either run reads it.
**State prerequisite:** No `ClaimMembership` row for the fresh entry's key at
both runs' read time.
**Mitigation — the single-writer invariant, and nothing else.** M5 runs
**at most one reconciler per agent, processing entries sequentially** (mirrors
`crystallize`'s one-crystallizer-per-partition). Under that invariant there is
no second run to race: both entry points (stream consumer and the `D5` direct
adapter) funnel into the same reconcile function behind the same single writer,
so "two runs" is a deployment error, not a state the code arbitrates. Merge-log
append stays idempotent per `(entry_id, run_watermark)`, which makes a
crash-and-rerun of the *same* writer safe — the property actually needed.
**The round-2 `HSETNX` claim-by-write is withdrawn**, along with the round-1
WATCH/MULTI-or-Lua-CAS spike it replaced. The spike existed only because an
early draft stored `class_id` as a field on an append-only record, where a
conditional NULL→id write has no primitive; `HSETNX` then became available once
membership moved out of the entry. But under a single sequential reconciler it
arbitrates a collision that cannot occur, and it is not free: it fixes a
hand-rolled hash layout that the `ClaimMembership` model no longer uses, so
keeping it would mean writing one piece of membership state outside the ORM for
a benefit only a second writer could collect.
**Deferred, not forgotten:** reintroducing a second reconciler (sharding by
agent range, or a parallel pass) **requires reintroducing an atomic claim** at
the same time — a `ClaimMembership` row created under a create-if-absent
primitive rather than a plain `save()`. Whoever proposes the second writer owns
that change; the single-writer invariant is the load-bearing assumption here and
must be named in the feature docs, not just in this plan.

### Race 2: Loser leaves live membership between shortlist and `save_and_supersede`
**Location:** reconcile loop → `SupersessionProtocol.save_and_supersede`
**Trigger:** A retraction or supersession closes the loser's validity interval
after the shortlist read.
**Data prerequisite:** Shortlist holds a key that is no longer a live validity
member at write time.
**State prerequisite:** None beyond the close landing first.
**Mitigation:** `ValidityMemberAbsentError` catch → re-read → `loser-absent`
merge-log entry; winner stands. Specified in Technical Approach; tested.
**Wording, because it changes the test:** the earlier draft said "deleted" /
"removed", which would send a builder to check key existence. Nothing deletes
here — the journal is append-only and `retract` "remove[s] the target from live
membership while leaving it fully readable historically"
(`src/popoto/recipes/provenance_journal.py:788-793`), which is also why AC2 says
the loser is "superseded (never deleted)". The trigger to assert on is a
**closed live-membership with the hash still present**, and the detection is
catching `ValidityMemberAbsentError` from `save_and_supersede` — never an
`EXISTS` check, which would pass and hide the case.

### Race 3: Two sibling entries of the same claim each create a singleton class
**Location:** new `recipes/reconciliation.py`, shortlist-read → commit-write span
**Trigger:** Burst capture produces two *different* fresh entries asserting the
same claim, and both are shortlisted before either has committed a `class_id`,
so each finds no candidate class and each creates its own singleton. The two
entries have different keys, so no per-entry claim primitive would arbitrate
them — both writes are legitimate.
**Data prerequisite:** Two persisted entries, same `agent_id`, same
`claim_type`, overlapping `subjects`, neither yet holding a `ClaimMembership`
row.
**State prerequisite:** Both shortlisted inside one interleaved
shortlist→commit span — i.e. *only reachable if the single-writer invariant is
broken*.
**Why it matters:** this is the one failure the merge log cannot repair by
replay, because no merge was ever *attempted* between the siblings — replay
faithfully reproduces two separate classes. It is a permanent silent
duplication, which is precisely the bug M5 exists to fix.
**Mitigation — the single-writer invariant, made effective by `claim_slot`.**
A sequential reconciler commits entry A's `ClaimMembership` row *before* it
shortlists entry B, so the interleaving this race needs never occurs. What
makes the invariant sufficient rather than merely hopeful is that B's lookup
does not depend on the embedding shortlist finding A: B computes its own
`claim_slot` digest and queries `ClaimMembership.query.filter(claim_slot=...)`
first. A sibling committed in the same pass is therefore found by exact slot
equality — an indexed lookup, not a similarity guess — and B joins A's class
instead of creating a second singleton. (This is the functional half of the
digest whose privacy half is argued in Key Elements: slot *equality* is all
reconciliation needs, which is why a one-way digest costs nothing here.)
**The round-2 advisory lock is withdrawn**, and `Defaults.RECONCILE_LOCK_TTL_SECONDS`
with it. Under a single sequential writer the lock is never contended, so it
would buy nothing while adding an unread `Defaults` constant — which is a CI
failure, not dead weight (see the `test_defaults_sync.py` note in Technical
Approach).
**Deferred, not forgotten:** a second reconciler re-opens this race in the form
the lock addressed, and reintroducing one requires reintroducing per-claim-slot
serialization along with Race 1's atomic claim. Same owner, same change.
**Test:** two sibling entries with identical `claim_type` + `subjects`,
processed in sequence by one reconciler, land in ONE class (the
single-writer-invariant test); plus a test that the second entry is joined via
the `claim_slot` lookup with the embedding shortlist stubbed to return nothing,
so the test fails if the slot index is skipped.

No other concurrency: merge-log appends are immutable inserts; representative
reads are last-write-wins on confirmation counts and tolerate staleness.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #565] Belief-sheet representative surfacing — M5 guarantees one
  representative per class is *selectable* (most-confirmed member) and that
  per-record fallback works without it; the reader-facing view is M6's module.
- [SEPARATE-SLUG #566] Disjunct-pair question generation — M5 stores ties as
  retrievable-together pairs with a shared id; turning them into clarifying
  questions is M7's module.
- [SEPARATE-SLUG #567] Causal pooling by class — M5 guarantees `class_id`
  stability for M8 to pool on; the pooling itself is M8's module.

## Update System

No update system changes required — this feature is purely additive library
code. No migration: new journal fields default NULL, merge kinds register
additively, and the reconciler runs only when the host invokes it. Downstream
adoption is a normal version bump; unreconciled journals read exactly as today.

## Agent Integration

No agent-tool surface. The reconciler is library code the host process invokes
post-turn — the `StreamConsumer` on the `"journal"` stream is the production
trigger, with `reconcile_entry(...)` available as a direct call (D5). No MCP
wrapper, no bridge import, no new tool. Integration
tests verify the consumer-driven trigger path end to end (stream append →
reconcile → class assigned), which is the closest equivalent to "the agent can
invoke it".

## Documentation

### Feature Documentation
- [ ] Create `docs/features/reconciliation.md`: equivalence classes, convention
  book (with version), frozen type enum + per-type rules + precedence tables,
  disjunct-pair semantics, merge-log replay procedure, embedding-fallback behavior.
- [ ] **Document the erasure cascade as an operator procedure, not a footnote.**
  `JournalEntry.hard_delete()` is the retention/erasure primitive, and its
  documented scope is the record plus "every trace of its **own** derived
  state" — explicitly "**not** 'every trace of the record anywhere in the
  keyspace'" (`src/popoto/fields/append_only.py:242-294`). M5 adds derived
  state the primitive therefore does not reach, so the page must say: to erase
  a reconciled entry, call M5's `erase_entry(entry)`, which (1) `hard_delete()`s
  the journal record, (2) deletes its `ClaimMembership` row, (3) deletes its
  cached reconciler-side embedding, and (4) recomputes the affected
  `ClaimClass` row — reselecting `representative_key` and decrementing
  `member_count`, or dropping the row when the class is left empty. Calling
  `hard_delete()` directly leaves a dangling membership row and, worse, a
  `ClaimClass` whose representative points at an erased key. State plainly why
  the membership row is survivable at all: it carries only the one-way
  `claim_slot` digest and no claim content, by the Key Elements invariant —
  which matters because `JournalEntry` also composes `NeverRecordMixin`
  (`src/popoto/recipes/provenance_journal.py:282`).
- [ ] Document the **single-writer invariant** as a deployment constraint, with
  the consequence named: running a second reconciler per agent re-opens Races 1
  and 3 and requires an atomic claim plus per-claim-slot serialization.
- [ ] Add entry to `docs/features/` index (mkdocs nav if index-driven — follow
  the precedent of the validity/journal feature pages).

### External Documentation Site
- [ ] Verify `mkdocs serve` builds with the new page (docs gate runs at merge).

### Inline Documentation
- [ ] Convention-book rationale comments on each per-type rule (why this
  precedence, not just what).
- [ ] Docstrings on the reconciler entry points, judge contract, and replay
  procedure (watermark semantics mirror `crystallize`'s documented limits).

## Success Criteria

- [ ] A restatement of a stored claim increments its class's confirmation count
  and creates no duplicate class (issue AC1).
- [ ] A firing type rule resolves by the per-type precedence table, marking the
  loser superseded (never deleted) with a pointer to the winner, written through
  `save_and_supersede` (issue AC2).
- [ ] A precedence tie produces a disjunct pair retrievable together; no silent
  winner at write or read time (issue AC3).
- [ ] Any merge is reversible: revoking a merge-log entry and replaying
  reproduces the pre-merge class assignment (issue AC4).
- [ ] Type rules run with zero LLM calls; judge calls are at most 2x the
  shortlist cap per entry — one forward ask plus one swapped-order symmetry
  probe per candidate class (issue AC5).
- [ ] Tests at `tests/test_reconciliation_m5.py`; docs page under
  `docs/features/` (issue AC6).
- [ ] No claim content leaves the append-only record: `ClaimMembership` /
  `ClaimClass` hold the `claim_slot` digest and no subject text or plaintext
  `claim_type`, and `erase_entry` cascades an erasure to the membership row, the
  cached embedding, and the `ClaimClass` recomputation (see Verification).
- [ ] Tests pass (`/do-test`); Documentation updated (`/do-docs`); every new
  `Defaults` constant registered in `tests/benchmarks/test_defaults_sync.py`.
- [ ] Anti-criterion: M6 surfacing stays out (see Verification).

## Team Orchestration

When this plan is executed, the lead agent orchestrates work using Task tools. The lead NEVER builds directly - they deploy team members and coordinate.

### Team Members

- **Builder (journal-model)**
  - Name: model-builder
  - Role: `claim_type` field, the `ClaimMembership` / `ClaimClass` models + M5 accessor, merge-kind registration, Defaults constants
  - Agent Type: builder
  - Resume: true

- **Builder (deterministic-tier)**
  - Name: rules-builder
  - Role: identity-key mapping, frozen type enum, per-type rules + precedence, `save_and_supersede` wiring + `ValidityMemberAbsentError` path
  - Agent Type: builder
  - Resume: true
  - Domain: Redis/Popoto data (single-writer atomicity via SUPERSEDE_LUA; Valkey-safe, no modules)

- **Builder (judge-loop)**
  - Name: judge-builder
  - Role: convention book config, sameness judge (`llm_verdict` contract), embedding shortlist + fallback, symmetry probe, reconcile loop + watermark + merge log + replay
  - Agent Type: builder
  - Resume: true
  - Domain: Redis/Popoto data (single-writer invariant — one reconciler per agent, sequential passes; `claim_slot` equality lookup before the embedding shortlist)

- **Validator (reconciliation)**
  - Name: recon-validator
  - Role: Verifies all success criteria, failure-path coverage, Defaults sync registration
  - Agent Type: validator
  - Resume: true

- **Documentarian (reconciliation)**
  - Name: recon-docs
  - Role: Feature docs page, index entry, mkdocs build check
  - Agent Type: documentarian
  - Resume: true

### Available Agent Types

Tier 1 core (`builder`, `validator`, `code-reviewer`, `test-engineer`,
`documentarian`, `plan-maker`, `frontend-tester`) per the skill baseline. No
standing specialists — domain work is prompted Tier 1 with a `Domain:` line as
above.

## Step by Step Tasks

### 1. Journal model surface
- **Task ID**: build-model
- **Depends On**: none
- **Validates**: `tests/test_reconciliation_m5.py::test_claim_type_defaults_null`, `::test_reconcile_never_mutates_an_entry`, `::test_membership_row_holds_no_claim_content`, `::test_hard_delete_cascades_to_membership_and_class` (create), existing journal suites unchanged
- **Assigned To**: model-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `claim_type = IndexedField(type=str, null=True)` to `JournalEntry` — the
  ONLY new field (assigned by capture from the extractor's type label before the
  entry's first `save()`; the deterministic tier reads it as the
  `(subjects[0], claim_type)` predicate)
- Do **NOT** add `class_id` or disjunction-link fields to `JournalEntry`: it is
  `AppendOnlyMixin` and any post-hoc field write raises `AppendOnlyViolation`.
  Add the two M5-owned plain models instead, exactly as declared in Key
  Elements — `ClaimMembership` (`entry_redis_key` KeyField; `class_id`,
  `claim_slot`, `disjunction_id` indexed) and `ClaimClass` (`class_id`
  KeyField; `agent_id`, `representative_key`, `member_count`, `updated_at`) —
  plus the M5 accessor M6/M7/M8 read class membership through. Neither model
  gets `AppendOnlyMixin`: a relabel must be an ordinary `save()`
- **Privacy invariant, enforced by test:** `ClaimMembership` stores the
  `claim_slot` **digest** (`sha256(f"{agent_id}|{subject}|{claim_type}")`,
  32 hex chars), never the subject text and never `claim_type` in plaintext;
  `ClaimClass` stores no claim content either. Implement the erasure cascade in
  the same task: an `erase_entry(entry)` helper that calls
  `JournalEntry.hard_delete()`, deletes that entry's `ClaimMembership` row and
  cached embedding, and recomputes the affected `ClaimClass` row (drop it if
  the class is now empty, otherwise reselect `representative_key` and decrement
  `member_count`). `hard_delete()` alone does **not** reach these — its scope
  is the record's own derived state, explicitly "not 'every trace of the record
  anywhere in the keyspace'" (`src/popoto/fields/append_only.py:242-294`)
- Register merge/disjoin annotation kinds via `register_kind("merge",
  closing=False)` / `("disjoin", closing=False)` **at `reconciliation.py`
  module import**; both kinds require a `target` (non-targetless), enforced in
  `pre_save`
- Add numeric constants to `Defaults`; register each in `tests/benchmarks/test_defaults_sync.py`
- Add export/import round-trip coverage for `claim_type` (precedent: #558) — the
  only new field, and the only new state needing export coverage; the two
  reconciliation models are deliberately not exported (they rebuild from the
  merge log, and they are plain Models, so the transfer declaration that guards
  `Field` subclasses and model-level mixins does not apply to them —
  `tests/test_transfer_roundtrip.py:729` and `:754`)

### 2. Deterministic tier
- **Task ID**: build-rules
- **Depends On**: build-model
- **Validates**: `tests/test_reconciliation_m5.py::test_singleton_rule_zero_llm_calls`, `::test_deadline_supersession_uses_save_and_supersede` (create)
- **Assigned To**: rules-builder
- **Agent Type**: builder
- **Parallel**: false
- Frozen type enum (`preference | deadline | trait | relationship | goal | procedure | note`)
- Per-type incompatibility rules + the precedence table **exactly as fixed in
  Solution → Precedence Table (v1)**: Rule 0 self-stated > inferred globally,
  then `deadline` recency / the five stable types confirmation-count-then-recency,
  `note` rule-free (never fires, never reaches precedence). Implement the
  all-column tie as a disjunct pair, not a fallback winner
- All outcomes through `SupersessionProtocol.save_and_supersede`; `ValidityMemberAbsentError` → re-read → `loser-absent` log
- Assert zero judge calls on this path (spy on judge client)

### 3. Judge + shortlist + symmetry probe
- **Task ID**: build-judge
- **Depends On**: build-model
- **Validates**: `tests/test_reconciliation_m5.py::test_malformed_reply_abstains`, `::test_split_verdict_disjoins` (create; malformed-reply cases mirror `verdict.py` adversarial tests)
- **Assigned To**: judge-builder
- **Agent Type**: builder
- **Parallel**: true
- Versioned convention-book config — ship **Convention Book v1 verbatim** from
  the Solution section as `CONVENTION_BOOK_V1` (version string `"v1"`), recorded
  on every merge-log entry; sameness judge copying `llm_verdict`'s contract
  (firewall, JSON schema, re-validate, never raise → abstention leaves singleton
  class)
- Embedding shortlist (same subject + type, capped), **reconciler-side — do NOT
  add an `EmbeddingField` to `JournalEntry`** (D4), with bounded subject+type
  index-scan fallback when numpy/provider absent
- Symmetry probe on join (split verdict → disjunct pair, never a join)
- Empty/whitespace statements short-circuit with zero judge calls

### 4. Reconcile loop + merge log + replay
- **Task ID**: build-loop
- **Depends On**: build-rules, build-judge
- **Validates**: `tests/test_reconciliation_m5.py::test_restatement_confirms`, `::test_replay_reverses_merge`, `::test_single_writer_invariant_joins_siblings`, `::test_sibling_found_by_claim_slot_without_shortlist` (create)
- **Assigned To**: judge-builder
- **Agent Type**: builder
- **Parallel**: false
- `StreamConsumer`-on-`"journal"` trigger as the **production** trigger (D5),
  plus a public `reconcile_entry(...)` direct call that is a thin adapter over
  the same reconcile function — one loop, two entry points, never two
  pipelines, both behind the **single-writer invariant** (one reconciler per
  agent, entries processed sequentially — Races 1 and 3). No claim-by-write and
  no advisory lock: both were withdrawn, and neither `HSETNX` nor
  `SET ... NX EX` should appear in this module. Merge-log append
  `{class_a, class_b, rationale, ts, judge_version}`; watermark-bounded replay +
  explicit genesis repair op that rebuilds both reconciliation tables from the
  log
- Look up candidate siblings by exact `claim_slot` equality
  (`ClaimMembership.query.filter(claim_slot=...)`) **before** the embedding
  shortlist, so a sibling committed earlier in the same pass is found by index
  rather than by similarity — this is what makes the single-writer invariant
  sufficient against Race 3
- Restatement path routes through `ProvenanceJournal.confirm` (confirmation count)
- Mega-class detector: emit a class-size-velocity telemetry signal when a class
  grows past `Defaults.MEGA_CLASS_VELOCITY_ALERT` joins per reconciler pass.
  Telemetry only, never a gate (Risk 1 promises this; without a task line it
  would ship unbuilt)

### 5. Validate reconciliation
- **Task ID**: validate-recon
- **Depends On**: build-loop
- **Assigned To**: recon-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full new suite + adjacent journal suites + `test_defaults_sync.py` explicitly (fails only in CI under narrow selection)
- Verify judge-call bound (at most 2x shortlist cap per entry) under burst
  capture; verify the disjunct/uncertainty flag on M5's
  representative-selection return value (not at M6's formatting layer)
- Report pass/fail status

### N-1. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-recon
- **Assigned To**: recon-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/features/reconciliation.md`; index entry; `mkdocs` build check

### N. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: recon-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all validation commands
- Verify all success criteria met (including documentation)
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| New suite passes | `pytest tests/test_reconciliation_m5.py -q` | exit code 0 |
| Journal suites unregressed | `pytest tests/ -q -k "journal or provenance or supersession or validity"` | exit code 0 |
| Defaults sync registered | `pytest tests/benchmarks/test_defaults_sync.py -q` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Type ratchet holds | `scripts/mypy_ratchet.py` | exit code 0 |
| No post-hoc writes to append-only entries | `pytest tests/test_reconciliation_m5.py -q -k "append_only"` | exit 0 — a test that reconciles an entry and then asserts `JournalEntry.get(entry_id)` is byte-identical to its pre-reconcile state, and that a deliberate `entry.save()` still raises `AppendOnlyViolation` |
| Derived index is rebuildable from the log | `pytest tests/test_reconciliation_m5.py -q -k "replay_rebuilds_index"` | exit 0 — delete every `ClaimMembership` and `ClaimClass` row, replay from genesis, assert class assignment is identical |
| Membership rows hold no claim content | `pytest tests/test_reconciliation_m5.py -q -k "membership_row_holds_no_claim_content"` | exit 0 — reconcile an entry whose subject is a unique sentinel string, then assert the sentinel and the `claim_type` value appear in no field of its `ClaimMembership` row or its `ClaimClass` row (assert over the persisted field values, not over source text) |
| Erasure cascades past `hard_delete` | `pytest tests/test_reconciliation_m5.py -q -k "hard_delete_cascades"` | exit 0 — reconcile two entries into one class, erase one via the M5 `erase_entry` helper, assert its `ClaimMembership` row and cached embedding are gone and the surviving `ClaimClass` row has a recomputed `representative_key` and `member_count`; erasing the last member drops the `ClaimClass` row |
| No withdrawn concurrency primitives | `pytest tests/test_reconciliation_m5.py -q -k "single_writer_invariant"` | exit 0 — two sibling entries processed in sequence by one reconciler land in ONE class, and the client spy records no `HSETNX` and no `SET ... NX` (the withdrawn Race 1 / Race 3 mechanisms) |
| No M6 surfacing in M5 module | `pytest tests/test_reconciliation_m5.py -q -k "no_m6_surfacing"` | exit 0 — asserts M5's representative-selection returns a plain `(entry, uncertainty_flag)` value and that `reconciliation.py` imports nothing from M6's module |
| Valkey-safe command set | `pytest tests/test_reconciliation_m5.py -q -k "command_allowlist"` | exit 0 — spies the client and asserts the reconciler issues only core commands (no `BF.*`/`CMS.*`/module calls) |

## Critique Results

### Round 2 (2026-09-14) — FULL roster: Risk & Robustness, Scope & Value, History & Consistency

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | driver (structural, verified) | `JournalEntry` composes `AppendOnlyMixin`, so the plan's mutable `class_id` IndexedField — "relabeled on merge", plus a compare-and-set NULL→id write — is impossible: `save()` refuses any re-save of an existing key, and `update_fields` is explicitly "still an overwrite and ... still refused". Disjunction-link fields have the same defect. | Key Elements (equivalence classes, claim type, disjunct pairs); Technical Approach (companion-state contract + no-mutation rule); Data Flow 5; Architectural Impact (interface changes, data ownership, reversibility); Race 1; Task 1; Task 4; Verification | Class membership moves to reconciler-owned companion keys — `Reconciliation:_class_of:{agent_id}` (HASH `entry_key → class_id`), `_class_members:{agent_id}:{class_id}` (SET), `_disjunction:{agent_id}:{disjunction_id}` (SET) — with the merge log authoritative and the index rebuildable from it. Precedent is V0's own: `save_and_supersede` never re-saves the incumbent, it closes it via `ValidityField.execute_supersede` against companion keys (`src/popoto/fields/supersession.py:720-737`). `claim_type` stays a real field because capture sets it before the first `save()`. Guard: `src/popoto/fields/append_only.py:202-207`; docstring `:166-167`. **Mechanism revised 2026-09-14 (remedy unchanged):** membership moved out of `JournalEntry` as this row requires, but into two M5-owned plain Popoto models, `ClaimMembership` and `ClaimClass`, rather than hand-rolled companion keys — ORM-native indexed reads, `KeyField` identity semantics, and a real read surface for M6. The colons in the entry `redis_key` used as `ClaimMembership`'s key value are safe: `DB_key.clean()` escapes `":"` to `COLON_ESCAPE` (`src/popoto/models/db_key.py:43`, `:191`). |
| CONCERN | Risk & Robustness | Race 1 only arbitrates two runs racing the *same* entry. Two *different* sibling entries asserting the same claim in one burst both find no candidate class and each create a singleton — permanent silent duplication that replay cannot repair, because no merge was ever attempted between them. | New Race 3; Task 4; Test Impact | Advisory lock over the shortlist→commit span: `SET Reconciliation:_lock:{agent_id}:{digest} {run_id} NX EX Defaults.RECONCILE_LOCK_TTL_SECONDS`, digest over `(agent_id, subjects[0], claim_type)` (the identity shape the deterministic tier already computes — no new identity notion). On `NX` miss, **defer to the next pass, never drop** (lag is safe per Risk 3; duplication is not). Release via `DEL` guarded on the stored `run_id` so an overrunning pass cannot delete a successor's lock. Correctness floor if the lock is lost: two singleton classes — today's behavior, never corruption. **Mitigation revised 2026-09-14 (hazard still documented):** the advisory lock is withdrawn and `Defaults.RECONCILE_LOCK_TTL_SECONDS` with it — under the single-writer invariant the lock is never contended, and an unread `Defaults` constant fails `test_all_defaults_covered_by_module_constants`. Race 3 now names the invariant as its mitigation, made sufficient by an exact `claim_slot` equality lookup ahead of the embedding shortlist so a sibling committed earlier in the same pass is found by index. A second reconciler re-opens the hazard and must reintroduce per-claim-slot serialization — deferred, not forgotten. |
| CONCERN | Risk & Robustness | The kind registry is process-global and non-persisted, and *writing* an unregistered kind raises `ValueError` in `pre_save` (a bug already hit in PR #589 review). The plan never said where the reconciler registers `merge`/`disjoin`, leaving a rolling-deploy / transfer-restore / backfill window where writes fail. | Technical Approach (register-the-merge-kinds bullet); Task 1 | Register at `reconciliation.py` **module import**: `register_kind("merge", closing=False)` and `("disjoin", closing=False)`. `closing=False` because a join/disjoin closes no validity interval (only `save_and_supersede` closes). `targetless=False` (default) means every merge-log annotation **MUST** name a `target` — `validate_kind_and_target` raises "a {kind!r} entry annotates another entry and must name a target" (`src/popoto/recipes/provenance_journal.py:527-531`), called from `pre_save`, so this fails at write time. Target the joining entry E for `merge`, one side for `disjoin` with the partner + disjunction id in the payload. `targetless=True` is not available: `register_kind` rejects `targetless and closing` together and a targetless kind must carry no target at all. Reading is safe — an unrecognized kind reads back inert for membership, so old readers degrade rather than fail. |
| CONCERN | Scope & Value | Risk 1 promises a "mega-class detector (class size velocity alert) as telemetry", but no task builds it — grep for detector/telemetry/velocity across the task bodies returned nothing, so it would have shipped unbuilt. | Task 4; Technical Approach (Defaults list) | Add to build-loop: emit a class-size-velocity signal when a class exceeds `Defaults.MEGA_CLASS_VELOCITY_ALERT` joins per reconciler pass. Telemetry only, never a gate — a gate would block legitimate large classes. Register the constant in `tests/benchmarks/test_defaults_sync.py` (that gate fails only in CI under narrow test selection). |
| CONCERN | Scope & Value | Task 4 commits to building *both* trigger shapes plus a spike to manage the race they jointly create, while the plan itself notes that resolving Open Q5 to stream-only dissolves that race — i.e. it builds the more complex answer by default before the cheaper question is asked. | Race 1 (spike withdrawn); Resolved Decision D5 | Half resolved structurally: the spike is gone and this is no longer a correctness risk — at round 2 because of an `HSETNX` claim-by-write, and since the 2026-09-14 revision because both entry points funnel into one reconcile function behind the single-writer invariant, which needs no arbitration primitive at all. The **scope** half was left open at round 2 as a PM question. **Closed in the 2026-09-14 revision as D5 (stream-first):** the `StreamConsumer` is the production trigger and the direct call is a thin adapter over the same reconcile function — exactly the builder guidance this row gave, now adopted as the decision, so a later stream-only answer deletes an entry point rather than restructuring the loop. |
| CONCERN | History & Consistency | The two grep rows in the Verification table checked the not-yet-written `reconciliation.py` for absence of identifier strings the builder alone chooses, so equivalent logic under different names passes trivially — they cannot detect the properties they claim to gate. | Verification table (both rows replaced by four behavioral suites) | Replaced with tests that can actually fail: `append_only` (reconcile an entry, assert it is byte-identical afterwards and that `entry.save()` still raises `AppendOnlyViolation`), `replay_rebuilds_index` (delete every `ClaimMembership`/`ClaimClass` row, replay from genesis, assert identical assignment), `no_m6_surfacing` (assert the `(entry, uncertainty_flag)` return shape and that `reconciliation.py` imports nothing from M6's module), `command_allowlist` (spy the client, assert core commands only — no `BF.*`/`CMS.*`). |
| NIT | History & Consistency | Race 2's "deleted"/"removed" wording contradicts retract semantics and AC2's "superseded (never deleted)", and would send a builder to an `EXISTS` check. | Race 2 (retitled + wording note) | Nothing is deleted: `retract` "remove[s] the target from live membership while leaving it fully readable historically" (`src/popoto/recipes/provenance_journal.py:788-793`). Assert on closed live-membership with the hash still present; detect by catching `ValidityMemberAbsentError` from `save_and_supersede`, never via `EXISTS` (which would pass and hide the case). |

### Round 1 (2026-09-11) — folded in by revision commit `9699e4df`

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | round 1 | Symmetry probe underspecified — "re-ask with E's phrasing included" did not define what varies between the two asks, so the probe could be implemented as a no-op re-ask. | Technical Approach (judge non-transitivity); Resolved Decision D1 | The probe swaps **claim order**: forward asks "is E the same claim as C's representative?", probe asks "is C's representative the same claim as E?". Join commits only on same/same; any split — including probe abstention — routes to a disjunct pair. Bound: at most 2x shortlist cap per entry. |
| CONCERN | round 1 | Race 1's conditional `class_id` write was asserted without evidence that Popoto could express it. | Race 1 pre-build spike | Spike the NULL→id write before build-loop, or resolve Open Q5 to stream-only. **Superseded in round 2**: the spike is withdrawn — once membership left the append-only record there was an atomic core primitive (`HSETNX`) needing no transaction or Lua. **Superseded again 2026-09-14**: the primitive itself is withdrawn too. Membership is now an ordinary `save()` on `ClaimMembership`, and the single-writer invariant means there is no concurrent write to make conditional. |
| CONCERN | round 1 | The deterministic tier needed a V0 predicate but the plan never said where the claim type came from, leaving it to be inferred at reconcile time. | Key Elements (per-entry claim type); Task 1 | `claim_type = IndexedField(type=str, null=True)`, assigned by capture from the extractor's type label, never inferred later; predicate is `(subjects[0], claim_type)`. Round-trips per the #558 precedent. |
| CONCERN | round 1 | "Most-confirmed member" as representative did not say how validity-closed losers are treated, so a superseded claim could be selected as its class's representative. | Data Flow 6; Technical Approach (representative discipline); Flow | Filter `validity__current=True`. Closed losers stay in-class for audit but are excluded from representative selection **and** from confirmation counts. Confirmation-count ties break by recency. |
| NIT | round 1 | AC5 said judge calls are "bounded by the embedding shortlist size", which the symmetry probe makes false. | Success Criteria AC5 | Reworded to "at most 2x the shortlist cap per entry — one forward ask plus one swapped-order symmetry probe per candidate class". |
| NIT | round 1 | The uncertainty-marker test targeted M6's formatting layer, which is #565's module and out of scope per the No-Gos. | Failure Path Test Strategy (error state rendering); Task 5 | Assert the flag on M5's own representative-selection return value, not at M6's formatting layer. |

---

## Resolved Decisions (no open questions remain)

**Status: all five questions that were open after critique round 2 are RESOLVED
in this revision (2026-09-14). Nothing in this plan is blocked on a human.**
These are plan-level decisions, not discoveries — a builder implements them as
written. Each is cross-referenced from the body as `D1`–`D5`; where a decision
changes body text, the body is authoritative and this section is the rationale.

### D1 — Symmetry probe: KEEP as specified

One swapped-order symmetry re-check per candidate class. Forward ask compares
E against C's representative; the probe re-asks with the order swapped. The join
commits only on same/same; any split — including a probe abstention — routes to
a disjunct pair.

*Rationale:* the probe converts the worst failure mode (a silent mega-class,
Risk 1) into the safe one (explicit uncertainty), at a bounded cost of 2x the
shortlist cap per entry. The rejected alternative, single-verdict joins with
mega-class telemetry only, detects the damage after it is done and leaves no
structural barrier to it; telemetry is kept anyway (Task 4) as a second line,
not as the first. See Technical Approach → judge non-transitivity, and AC5.

### D2 — Convention book v1 and the precedence table: WRITTEN INTO THE PLAN

The literal v1 standard is in Solution → **Convention Book v1**, and the
complete per-type ordering is in Solution → **Precedence Table (v1)**. Both are
build inputs to be implemented verbatim.

*Rationale:* the three rows the critique flagged as unspecified
(`relationship`, `goal`, `procedure`) all describe standing facts that get
restated, so they join the **stable** family (confirmation count, then recency)
rather than the supersession family. Family membership is not a taste call: the
supersession family is exactly the set of types whose deterministic rule is
same-target supersession, which today is `deadline` alone, and that rule is what
makes "newest wins" right for it. Global Rule 0 (self-stated beats inferred) is
set by the issue and applies to every type before any per-type row. `note` is
rule-free — no incompatibility check, no precedence row — because a rule-free
catch-all is the whole reason the enum can be frozen and stay decidable. The
table is made **total** (an all-column tie yields a disjunct pair) so AC3's "no
silent winner" is a property of the table rather than an assumption about data.

### D3 — Frozen 7-type enum: ACCEPTED as proposed

`preference | deadline | trait | relationship | goal | procedure | note`.

*Rationale:* every type except `note` needs a decidable incompatibility rule and
a precedence row, and D2 now supplies both for all six. `note` absorbs the tail,
which is what lets the enum be frozen at build without an escape hatch that
would reintroduce the open-enum decidability problem (recon Dropped bucket). No
slot is merged or split: merging `goal` into `preference` would put an
aspiration and a standing preference under one rule, and splitting any slot adds
a rule family with no claim shape asking for it yet.

### D4 — Embedding placement: RECONCILER-SIDE

`JournalEntry` gains no `EmbeddingField`. The shortlist embeds the entry's
`statement` inside the reconciler and keys the cache by `entry_redis_key`,
alongside the two reconciliation models.

*Rationale:* this holds the ownership line the append-only remedy already drew —
M5 owns derived state, M1 owns the record — and it keeps `claim_type` the only
new `JournalEntry` field, which is what makes Architectural Impact's "exactly
ONE new field" literally true. Note this is a scope/ownership decision and not a
constraint: an `EmbeddingField` would have been legal under append-only, since
it populates on the entry's first and only `save()`. Recorded explicitly so a
builder does not "fix" the absence by adding the field. Cost: a cache loss means
a re-embed, acceptable because the cache is derived state behind a
correctness-preserving fallback (Risk 4). One obligation follows: an embedding
is a lossy encoding of `statement`, so the cache is content-derived state
outside `hard_delete()`'s scope and belongs in the erasure cascade
(Documentation), exactly like the `ClaimMembership` row.

### D5 — Trigger shape: STREAM-FIRST, direct call as a thin adapter

The `StreamConsumer` on the `"journal"` stream is the production trigger. A
public `reconcile_entry(...)` direct call exists as a thin adapter over the same
reconcile function — the path tests drive, and the path a host without a
consumer can call. One loop, two entry points; never two pipelines.

*Rationale:* this is the shape the round-2 critique itself recommended, adopted
as the decision rather than left as guidance. Correctness does not depend on the
answer — both entry points are the same reconcile function behind the same
single writer, so there is no concurrency for a second trigger to introduce
(Race 1), and Race 3 is unaffected either way — so the only thing at stake was
scope. Structuring it this way means a later stream-only answer *deletes an entry
point* instead of restructuring the loop, which is strictly cheaper than
discovering the question mid-build.
