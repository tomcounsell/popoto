---
status: Planning
type: feature
appetite: Large
owner: agent-a773efcbae003f9f3
created: 2026-09-11
tracking: https://github.com/tomcounsell/popoto/issues/564
last_comment_id: pending
---

# M5 — Reconciliation: claim equivalence classes, typed contradiction rules, explicit disjunctions

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
  `ValidityMemberAbsentError`. M5's reconcile loop must catch this (loser deleted
  between shortlist and write) instead of relying on the old `None` contract.
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
  Note `JournalEntry` carries NO `EmbeddingField` today — the shortlist needs one
  (source `"statement"`) or a bounded fallback.
- **#558 (export/import round-trip, merged):** per-field round-trip fidelity
  precedent — any new field M5 adds (`class_id`, disjunction links) must
  round-trip through it.

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

1. **Entry point**: post-turn background window. A `StreamConsumer` on the
   `"journal"` stream (metadata: `agent_id`, `kind`, `target`) wakes on new
   `kind="assert"` entries, or the capture pipeline calls the reconciler
   directly off M1's event emission. Input is one fresh `JournalEntry`
   (`statement`, `subjects`, `stated`, `turn_id`).
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
   annotation (registered merge kind). Replay = revoke entry + recompute class
   assignment. Classes are tracked by plain relabeled `class_id`, never
   union-find.
6. **Output**: M6 reads one representative per class (most-confirmed member);
   without M5 it falls back to per-record. M7 consumes disjunct pairs as question
   sources; M8 pools causal estimates by `class_id`, falling back to by-type.

## Architectural Impact

- **New dependencies**: none vendored. New recipe module
  (`src/popoto/recipes/reconciliation.py`, name TBD at build) depends on
  `ProvenanceJournal`, `SupersessionProtocol`, `EmbeddingField`, and the
  `extraction/verdict.py`-shaped LLM client (anthropic extra, already optional).
  No new Redis modules — Valkey-safe by construction (memory:
  `feedback_valkey_compatibility`).
- **Interface changes**: additive only. `JournalEntry` gains `class_id`
  (IndexedField) and disjunction-link fields; new merge/disjoin kinds via
  `register_kind`. `ProvenanceJournal` gains no signature changes (reconciler
  calls existing `confirm` / `supersede`). M6/M7/M8 read the new fields; all
  degrade to per-record behavior without them.
- **Coupling**: reconciliation owns sameness; V0 owns closing; M1 owns
  annotation vocabulary. The amendment's invariant holds structurally: the
  deterministic tier calls `save_and_supersede`, so there is exactly one
  supersession mechanism and `class_id` subsumes identity keys (a deterministic
  hit is a class of size 2 merged without a judge call).
- **Data ownership**: class membership lives on entries (`class_id`) + the
  append-only merge log; no sidecar store to keep in sync.
- **Reversibility**: merge-log replay reproduces pre-merge assignment
  (acceptance criterion 4); the module itself is removable — entries remain
  valid journal records with `class_id` NULL.

## Appetite

TODO

## Prerequisites

TODO

## Solution

TODO

## Failure Path Test Strategy

TODO

## Test Impact

TODO

## Rabbit Holes

TODO

## Risks

TODO

## Race Conditions

TODO

## No-Gos (Out of Scope)

TODO

## Update System

TODO

## Agent Integration

TODO

## Documentation

TODO

## Success Criteria

TODO

## Team Orchestration

TODO

## Step by Step Tasks

TODO

## Verification

TODO

## Critique Results

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

TODO
