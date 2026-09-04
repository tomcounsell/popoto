---
status: Planning
type: feature
appetite: Large
owner: Valor Engels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/563
last_comment_id: 5537006156
---

# M4 — Reference resolution at capture: perishable context with typed abstention

## Problem

A user says, on a Tuesday: *"Dana's been on Atlas since March — she wants the report filed by
Friday."* M3 (#562) enumerates that turn into candidates, an LLM verdict accepts them, and M1
(#560) appends them to the provenance journal **byte-identically**. What gets stored is
`"she wants the report filed by Friday"`. Six weeks later that record is unusable: *she* who,
*the report* which, *Friday* when. The information that would have answered all three — the
previous turn, the wall clock, the speaker's timezone — existed for the duration of one function
call and was never written down. This is **perishable work**: no amount of later compute
recovers it.

**Current behavior — re-verified at plan time against `bb38f42`:**

1. **No resolution code exists anywhere.** Zero hits in `src/` for coreference, anaphora, or
   relative-date parsing. There is no `dateparser`/`dateutil` dependency. The only mitigation is
   a prompt sentence asking Claude for text "written so it makes sense on its own without the
   surrounding conversation" (`src/popoto/extraction/claude.py:58-59`) — an unverified
   instruction on a path (`ClaudeExtractionProvider`) that the auditable pipeline does not even
   use.
2. **The auditable path stores the raw span and says so.** `DecisionLog._append_and_transition`
   (`src/popoto/extraction/decision_log.py:752-777`) passes `statement=candidate.text` and
   `verbatim=candidate.text` — *the same string object* — with the comment "Distillation is M4's
   job, and doing any of it here would violate #562's own acceptance criteria."
3. **No capture context is recorded.** `Candidate`
   (`src/popoto/extraction/candidates.py:48-74`) carries `text/turn_id/candidate_id/start/end/
   generator_rule` — no speaker, no timestamp, no timezone, no neighbouring turns.
   `SubconsciousMemory.extract_memories` (`src/popoto/recipes/subconscious_memory.py:426`)
   receives exactly one `response_text` string and no conversation history at all.
   `JournalEntry.speaker` exists (`src/popoto/recipes/provenance_journal.py:304`) and
   `DecisionLog.assemble` already accepts a `speaker=` argument
   (`src/popoto/extraction/decision_log.py:614`) — **the pipeline never passes it**
   (`subconscious_memory.py:696`). `captured_at` defaults to the clock at write time
   (`provenance_journal.py:929`), which is the *storage* instant, not the *utterance* instant.
4. **Ambiguity has no vocabulary.** A reference the evidence does not determine is either
   silently guessed by whatever model touched it, or silently kept vague. Neither outcome is
   distinguishable from a confident, correct resolution when the fact is later retrieved.
5. **`valid_from` is always "now".** `ProvenanceJournal.append` sets `validity=instant` where
   `instant = _coerce_instant(at)` and `at` defaults to the write clock
   (`provenance_journal.py:902, 944`). "Since March" opens its validity interval in September.
   Nothing upstream ever passes `at=`.

**Desired outcome:** between M3's `accept` verdict and M1's journal append, a resolution stage
runs one structured LLM call over *(the candidate span + its full turn + a bounded window of
recent turns + a context header of capture time / timezone / speaker)* and emits, per reference:

- `resolved` — the evidence in the window determines it;
- `assumed` — committed to, but flagged, with the assumption stated in one line;
- `evidence_gap` — 2–4 candidate referents plus the single question that would settle it, stored
  as data and never asked here (M7 #566 consumes it);
- `indeterminate` — the speaker never settled it; the vague form is stored honestly.

Verbatim always survives untouched alongside any resolution, so a wrong resolution destroys
nothing. When a reference is an **onset** anchor ("since March"), the stage emits `valid_from`
for the V0 #580 / M1 #560 validity interval.

## Freshness Check

**Baseline commit:** `bb38f42` (`test(#549): resolve the expected test DB instead of hardcoding
15 (#605)`) — `git rev-parse main` at plan time.
**Issue filed at:** 2026-08-13T06:28:38Z (three weeks before planning; every dependency landed in
that window).
**Disposition:** **Minor drift** — every claim still holds, one is now under-stated, and all four
cited dependencies have merged.

**File:line references re-verified:**

- `src/popoto/extraction/claude.py:58-59` — the "makes sense on its own" prompt instruction —
  **still holds exactly.** Confirmed inside `EXTRACTION_PROMPT` (`:53-70`), pinned as
  non-configurable (`:38-45`).
- "`ExtractedFact` is `text/entities/importance/confidence` only" — **drifted, and the drift is
  in this plan's favour.** M3 added `span_start`, `span_end`, `turn_id`, `candidate_id`,
  `generator_rule` (`src/popoto/extraction/__init__.py:60-75`), populated only on the auditable
  path. The *substance* of the claim — no speaker, no timestamp, no timezone, no turn context —
  is unchanged.
- "zero coreference or relative-date resolution code in `src/`" — **still holds.** Confirmed by
  full-tree survey; no date-NLP dependency exists.
- `src/popoto/recipes/provenance_journal.py:1119-1131` (from the 2026-09-04 comment) — the
  `execute_supersede` call site naming #563 as the owner of the `save_and_supersede` conversion —
  **drifted to `:1102-1142`**, claim holds verbatim; the in-code comment names "#563" at `:1127`.

**Cited sibling issues/PRs re-checked — all four merged since filing:**

- **#580 V0 validity primitives** — closed 2026-08-17, PR #582 (`a4f7fbf`). `ValidityField`,
  `SupersessionProtocol`, assembler gating are live.
- **#561 M2 never-record firewall** — closed 2026-08-17, PR #587 (`337b3f0`). Relevant because
  every new subject tag M4 writes is scanned by it (see spike-2).
- **#560 M1 provenance journal** — closed 2026-08-19, PR #589 (`67965a1`). `ProvenanceJournal.
  append(...)`, `JournalEntry`, `at=`/`captured_at=` are the write surface M4 targets.
- **#562 M3 auditable extraction** — closed 2026-08-26, PR #591 (`1467095`). The candidate
  pipeline M4 inserts into. **The degraded "standalone attachment to the legacy path" fallback
  the issue allowed is no longer needed** — M3 shipped, so M4 builds only the integrated stage.
- **#588 supersession membership guard** — closed 2026-09-04, PR #601 (`90fc3d3`), the subject of
  the issue's only comment. `save_and_supersede`/`save_and_invalidate` and
  `ValidityValidFromConflictError` are live; `get_valid_from()` reads the effective (index) start.
- **#564 M5** and **#566 M7** — both still **open**. M4 is upstream of both; nothing to coordinate
  beyond leaving `evidence_gap` records readable.

**Commits on main since the issue was filed (touching referenced files):**

- `1467095` feat(#562) M3 — **changed the insertion point**: the stage now inserts into
  `_extract_memories_auditable`, not into the legacy provider path.
- `67965a1` feat(#560) M1 — **created the write target**.
- `337b3f0` feat(#561) M2 — constrains subject-tag entropy (see Risks).
- `a4f7fbf` feat(#580) V0 — **created `valid_from`**, which the 2026-08-16 amendment then assigned
  to M4.
- `90fc3d3` fix(#588) — added the two design questions in the issue comment, both answered below
  (Technical Approach → "Answers to the #588 comment").
- `16aa702`, `3a793d6` — production audit and `exclude_keys` suppression; touch the assembler and
  recipes, not the extraction write path. Irrelevant.

**Active plans in `docs/plans/` overlapping this area:** none. `auditable_extraction_m3.md`,
`provenance_journal_m1.md`, `validity_primitives_v0.md` and
`supersession_membership_guard_in_lua.md` are all `Complete`/shipped and are read here as
contracts, not as competing work. No open plan touches `src/popoto/extraction/`.

**Notes:** the corrected pointers (`decision_log.py:752-777`, `subconscious_memory.py:682-713`,
`provenance_journal.py:1102-1142`) are used throughout Technical Approach and Step by Step Tasks.

## Prior Art

- **#562 / PR #591 — M3 auditable extraction** (merged 2026-08-26): built the deterministic
  candidate generator, the `Verdict`/`ReasonCode` enums, the guarded decision log, and the
  claim → pending → append → terminal assembly ordering. **M4 reuses all of it and copies its
  shape**: a pinned model + pinned prompt + JSON-schema-constrained call, re-validated in Python,
  never raising, with an injectable provider seam for tests
  (`AuditableExtractionConfig.verdict_provider`, `decision_log.py:1047-1071`). M4's resolution
  provider is the same seam pattern.
- **#560 / PR #589 — M1 provenance journal** (merged 2026-08-19): `statement` vs `verbatim` are
  two distinct fields precisely so a later stage can populate the first without touching the
  second. M4 is that stage. `append(at=...)` is the `valid_from` inlet.
- **#580 / PR #582 — V0 validity primitives** (merged 2026-08-17): interval semantics
  (`valid_from <= t AND invalid_at > t`), `ZADD NX` on `valid_from`, and the open sentinel.
- **#588 / PR #601** (merged 2026-09-04): moved membership into `SUPERSEDE_LUA`; made a
  conflicting re-assertion of `valid_from` a typed `ValidityValidFromConflictError` instead of a
  silent `ZADD NX` loss. This is why M4 must never "retry with the effective value".
- **#461 / PR #481 — first-class LLM extraction path** (merged): the status-quo
  `ClaudeExtractionProvider`, and the origin of the prompt sentence M4 replaces with a mechanism.
- **#489 / PR #510 — extraction-provider eval axis**: the harness that makes extraction quality
  measurable offline. Not extended here (see No-Gos).
- **#521 / #519 — datetime tzinfo losses**: prior evidence that this repo has been bitten by
  timezone-naive handling. M4 therefore carries an explicit IANA timezone string in the context
  header and stores instants as epoch floats, matching the extraction/journal convention
  (`time.time()` everywhere on this path).

No prior attempt at reference resolution exists — this is greenfield, so there is no
"Why Previous Fixes Failed" section.

## Research

**Queries used:**

- "LLM coreference and relative date resolution at extraction time typed abstention structured
  output"
- "Anthropic structured outputs json_schema output_config best practices 2026"

**Key findings:**

- **Coreference belongs *before* extraction consolidation, as a typed, rule-scoped prompt stage**
  — CORE-KG ([arxiv.org/pdf/2510.26512](https://arxiv.org/pdf/2510.26512)) resolves mentions in a
  dedicated module with per-type resolution rules and explicit type definitions, rather than
  hoping a single extraction prompt does it. *Informs:* M4 is its own stage with its own pinned
  prompt, not another sentence bolted onto `EXTRACTION_PROMPT`.
- **The "verbatim + resolution + rewritten text" triple is the established safe schema** —
  BioCoref ([arxiv.org/pdf/2510.25087](https://arxiv.org/pdf/2510.25087)) requires each mention to
  appear *verbatim* in the source (no invention, no rephrasing), resolves it to an antecedent, and
  emits the rewritten passage as a separate field. *Informs:* the `Reference.surface` field must
  be a literal substring of the candidate span and is **validated in Python** (offset check), and
  `statement` (rewritten) never replaces `verbatim`.
- **Relative dates resolve against an explicit reference anchor supplied in the prompt, and
  normalize to ISO-8601** — CLINES ([medrxiv](https://www.medrxiv.org/content/10.64898/2025.12.01.25341355.full.pdf))
  anchors on document metadata; the general practice
  ([Medium: dates in structured output](https://medium.com/@jamestang/best-practices-for-handling-dates-in-structured-output-in-llm-2efc159e1854))
  is that relative strings are useless in stored data and ISO-8601 is the one unambiguous form.
  *Informs:* the context header carries an explicit `now` + IANA timezone; the model emits
  ISO-8601 (`YYYY-MM-DD` or full datetime), and **Python** converts to an epoch float — the model
  never emits an epoch number.
- **"Where enough information exists" is exactly the abstention branch, and typed abstention is
  not covered by the literature** — the closest prior art is ambiguity-flagging with both
  interpretations. *Informs:* the four-way ladder is a genuine design contribution, so it must be
  spelled out in the prompt with one example per status and tested per status (AC #5).
- **Anthropic structured outputs: use the GA `output_config={"format": {"type": "json_schema",
  "schema": ...}}` path with `additionalProperties: false`; no beta header**
  ([platform.claude.com](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)).
  Format adherence is guaranteed; **content correctness is not** — semantic validation stays
  downstream. Grammar compilation adds 100–300 ms on first use, then caches ~24 h. Schema support
  is not full JSON Schema; deeply nested/exotic constructs may be unsupported. *Informs:* M4
  copies `verdict.py:283-309`'s call shape exactly (already GA-path), keeps the schema **one
  level of nesting deep** (an object with a flat array of flat objects), and re-validates every
  field in Python (`_parse_reply` analogue) instead of trusting the schema.

## Spike Results

All spikes were prototypes run against a live Redis on **DB 13** (`REDIS_URL=redis://localhost:6379/13`
exported *before* `import popoto`, per CLAUDE.md's DB-0 hazard rule), at baseline `bb38f42`, then
`FLUSHDB`'d. Script: throwaway, not committed.

### spike-1: Can `ProvenanceJournal.append()` open a **backdated** validity interval?
- **Assumption**: "`append(at=<3 months ago>)` on a targetless `kind='assert'` stores that instant
  as the entry's `valid_from`, rather than raising or being overwritten by the save clock."
- **Method**: prototype
- **Finding**: **Yes, exactly.** Declared `at = now - 90d` → `ValidityField.get_valid_from(...)`
  returned the *same float, delta 0.0*. The `_write` backdate guard (`provenance_journal.py:995-1013`)
  is inside the `if target_key is not None` annotation branch, so it never fires for a plain
  capture. `captured_at` was stored independently (delta 0.0 from the supplied value), confirming
  the two clocks are separable: **`captured_at` = when it was said, `at`/`valid_from` = when the
  claim became true.**
- **Confidence**: high
- **Impact on plan**: the `valid_from` emission needs **no change to M1** — it is a new keyword
  argument threaded from the resolution result through `DecisionLog.assemble` into an existing
  parameter. This is the single largest scope reduction in the plan.

### spike-1b: Is a **future** `valid_from` accepted?
- **Assumption**: "'starting next Friday' can open an interval in the future."
- **Method**: prototype
- **Finding**: accepted, stored verbatim, `valid_from > now`. Under V0 membership
  (`valid_from <= t`) the entry is simply not a member until that instant arrives — which is the
  semantically correct behaviour for a future onset, not a bug.
- **Confidence**: high
- **Impact on plan**: future onsets are in scope and get a test; no clamping to `now`.

### spike-2: Do `res:*` subject tags survive the M2 never-record firewall?
- **Assumption**: "a status tag on the journal entry is a viable flag-transport mechanism."
- **Method**: prototype
- **Finding**: all four of `res:resolved`, `res:assumed`, `res:evidence_gap`,
  `res:indeterminate` appended cleanly alongside the existing `cand:` tag, and
  `JournalEntry.query.filter(agent_id=..., subjects__all=["res:assumed"])` retrieved the tagged
  entry. Low-entropy literals, so the firewall's `high_entropy` rule (which forbids uuid4-shaped
  tags, per `subconscious_memory.py:547-556`) is not triggered.
- **Confidence**: high
- **Impact on plan**: the status flag travels **on the entry itself** as an indexed tag, not only
  in a sidecar. This is the mitigation for the design study's top threat ("assumption surfaced
  without its flag") — see Risk 1.

### spike-3: Can a re-capture of the same candidate conflict on `valid_from`?
- **Assumption**: "a second resolution supplying a different `valid_from` raises
  `ValidityValidFromConflictError` (the shape the #588 comment asks about)."
- **Method**: prototype
- **Finding**: **No — the conflict is unreachable on this path.** `JournalEntry.entry_id` is an
  `AutoKeyField`, so a second `append` mints a *new* member key with its *own* interval; the
  second `valid_from` was stored on the new member and the first was untouched. Double-capture is
  prevented one layer up by M3's decision-log claim + `pending`/terminal probe
  (`decision_log.py:609-695`), not by the validity index.
- **Confidence**: high
- **Impact on plan**: answers question (1) of the #588 comment definitively — there is no
  "retry with the effective value" decision to make, because there is no conflict to absorb. The
  plan still adds an explicit regression test asserting M4 never calls `get_valid_from()` to
  reconcile, so a future refactor cannot quietly reintroduce the absorb-a-conflict pattern.

### spike-4: Is `statement != verbatim` accepted by M1's write path?
- **Assumption**: "M1 will store a resolved rewrite in `statement` while keeping the raw span in
  `verbatim`."
- **Method**: prototype (observed as part of spike-1)
- **Finding**: yes — both fields stored independently, no equality constraint anywhere in
  `_write`. Only the "neither statement nor verbatim" case raises
  (`provenance_journal.py:624-629`).
- **Confidence**: high
- **Impact on plan**: no M1 change needed for the rewrite either.

## Data Flow

## Architectural Impact

## Appetite

## Prerequisites

## Solution

## Failure Path Test Strategy

## Test Impact

## Rabbit Holes

## Risks

## Race Conditions

## No-Gos (Out of Scope)

## Update System

## Agent Integration

## Documentation

## Success Criteria

## Team Orchestration

## Step by Step Tasks

## Verification

## Critique Results

---

## Open Questions
