---
status: Ready
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

Traced end-to-end at baseline `bb38f42`. **Bold** steps are new in M4; everything else exists.

1. **Entry point** — `SubconsciousMemory.extract_memories(response_text, importance=0.5,
   turn_id=None, `**`context=None`**`)` (`src/popoto/recipes/subconscious_memory.py:426`). The new
   `context` is a `TurnContext` (speaker, capture instant, IANA timezone, bounded prior-turn
   window). Omitted → **`TurnContext.now()`**: capture instant from the clock, `UTC`, no speaker,
   empty window. The stage still runs — degraded, never skipped.
2. Empty-turn check (`:481-484`) and the turn-level never-record firewall (`:494-501`) — unchanged,
   and both run **before** any context is used, so an off-the-record turn never reaches the
   resolution LLM.
3. `_extract_memories_auditable(response_text, turn_id, `**`context`**`)` (`:655-719`) →
   `generate_candidates(turn_id, response_text)` (`extraction/candidates.py:77-104`) — unchanged,
   still pure and context-free.
4. Per candidate: `_verdict_for(candidate)` (`:622-653`) → `VerdictResult`. Non-`accept` →
   `write_terminal(...)`, `continue`. **Resolution never runs on a rejected candidate**, so its
   cost scales with the acceptance rate, not the candidate count.
5. **On `accept`: `_resolve_for(candidate, response_text, context)`** → one structured LLM call
   (`extraction/resolution.py`). Inputs: the candidate span, the *full* turn text (the span's own
   context, always available), the bounded window, and the context header
   (`now` as ISO-8601 in `context.timezone`, weekday name, speaker). Output: a `Resolution`
   (`statement`, `references`, aggregate `status`, `valid_from`, `degraded`).
6. **Python re-validation** (`_parse_reply` analogue): every `surface` must be a literal substring
   of the candidate span at its declared offsets; every status/kind/role must be a known enum
   member; per-status required fields must be present and bounded; ISO-8601 dates are parsed
   *in Python* against `context.timezone` into epoch floats. Anything malformed **drops that
   reference**, not the whole result; a malformed envelope drops to
   `statement = verbatim, status = indeterminate, degraded = True`.
7. `DecisionLog.assemble(agent_id, candidate, journal, speaker=…, `**`resolution=…`**`)`
   (`extraction/decision_log.py:609-695`) — unchanged ordering: claim → probe → `pending` →
   append → terminal → release. The resolution rides along as one new keyword.
8. `_append_and_transition(...)` (`:752-822`) → `ProvenanceJournal.append(...)`
   (`recipes/provenance_journal.py:563`) with, **new**: `statement=resolution.statement` (was
   `candidate.text`), `captured_at=context.captured_at` (was: journal's write clock),
   `at=resolution.valid_from` (was: `None` → write clock), and one extra subject tag
   `res:{status}` beside the existing `cand:{candidate_id}`. `verbatim=candidate.text` is
   **unchanged and remains the raw span**.
9. `ProvenanceJournal._write` (`:879-960`) — unchanged: builds `JournalEntry` with
   `validity=_coerce_instant(at)`, runs the D7 pre-flight and the never-record scan over all
   values *including the new `res:` tag*, then saves. `ValidityField.on_save` `ZADD NX`s
   `valid_from`.
10. **Sidecar write: `ResolutionLog.write(agent_id, candidate, resolution, entry_id)`** — a
    `ResolutionRecord` keyed `(agent_id, turn_id, candidate_id)`, holding the full reference list
    (JSON), the assumption lines, the `evidence_gap` candidates + questions, and the context
    header. Written **after** a successful append so it can carry `entry_id`; a failure here is a
    logged warning, never a rollback (the status flag already travels on the entry's tag).
11. `write_terminal(..., ACCEPT, ACCEPTED, entry_id=...)` (`:815-821`) — unchanged.
12. **Output**: `ExtractedFact` gains `verbatim`, `resolution_status`, and `assumption`; its
    `text` is now `resolution.statement` (the resolved form) rather than the raw span.
13. **Downstream consumers**: M5 (#564) reads `statement` for equivalence judgments; M7 (#566)
    reads `ResolutionRecord`s with `status == evidence_gap` for its question source; the context
    assembler surfaces the entry, and the `res:assumed` tag travels with it.

## Architectural Impact

- **New dependencies**: none. `zoneinfo` and `datetime` are stdlib (repo is 3.10+); the
  `anthropic` client is already an optional dependency probed exactly the way `verdict.py:54-63`
  probes it. **No Redis modules** — the sidecar is a plain Popoto `Model`, Valkey-compatible.
- **Interface changes** (all additive, all defaulted):
  - `SubconsciousMemory.extract_memories(..., context=None)`
  - `AuditableExtractionConfig.resolution_provider: Any = None` (a frozen dataclass field with a
    default, so existing positional/keyword construction is unaffected)
  - `DecisionLog.assemble(..., resolution=None)` and `_append_and_transition(..., resolution)`
  - `ExtractedFact` gains `verbatim`, `resolution_status`, `assumption` (all `None` by default;
    the legacy provider path never sets them)
  - **Behavioural change, deliberate**: on the auditable path `ExtractedFact.text` and
    `JournalEntry.statement` become the *resolved* form. `verbatim` is unchanged on both.
- **Coupling**: M4 depends on M3 and M1; neither depends on M4. `resolution.py` is pure (no
  Redis import) and `resolution_log.py` owns the only Redis surface, so the resolution logic is
  unit-testable without a database.
- **Data ownership**: M4 owns a new keyspace (`ResolutionRecord`) and becomes the **only** writer
  of a non-default `valid_from` on the capture path. M1 keeps ownership of the journal; M3 keeps
  ownership of the decision log and of assembly ordering.
- **Reversibility**: high. `Defaults.M4_RESOLUTION_ENABLED = False` (a deploy-level env kill
  switch, per the repo's default-on doctrine) restores M3's exact behaviour —
  `statement == verbatim`, `valid_from == now`, no `res:` tag, no sidecar write. Already-written
  records stay readable; nothing is migrated.

## Appetite

**Size:** Large

**Team:** Solo dev, PM, code reviewer

**Interactions:**
- PM check-ins: 1-2 (the `valid_from` onset-vs-deadline rule and the four-way prompt vocabulary
  are product decisions as much as technical ones — see Open Questions)
- Review rounds: 2+ (a new LLM stage on a write path that touches validity intervals; M1/M3 both
  took multiple rounds)

Large because it is a new module, a new persisted model, a new pinned prompt/schema, a new
context object threaded through three call layers, and a `valid_from` emission that changes what
the validity index means for captured entries — with a four-status test matrix on top.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -n 15 PING` | The suite and every journal/sidecar test need a live server |
| Test DB pinned (not 15, not 0) | `python -c "import os,sys; d=os.environ.get('POPOTO_TEST_DB'); sys.exit(0 if d and d not in ('0','15') else 1)"` | Every worktree shares DB 15; concurrent lanes produce phantom failures (CLAUDE.md) |
| Editable install resolves to this checkout | `python -c "import popoto,pathlib,sys; sys.exit(0 if pathlib.Path(popoto.__file__).resolve().is_relative_to(pathlib.Path.cwd().resolve()) else 1)"` | A stale editable install silently tests another tree |
| Full extras installed | `python -c "import numpy, sentence_transformers"` | `.[dev]` alone deselects ~95 tests |

`ANTHROPIC_API_KEY` is **not** a prerequisite: every test injects a fake client or a stub
resolution provider, exactly as `tests/test_auditable_extraction.py:80-111, 188-206` does. The
absent-dependency path is itself a test case.

## Solution

### Key Elements

- **`ResolutionStatus` — the four-way ladder.** `resolved | assumed | evidence_gap |
  indeterminate`, a `str`-valued enum, ordered worst-last so a candidate's aggregate status is the
  worst of its references' statuses. Deliberately replaces a numeric confidence: downstream only
  ever branches on these four cases.
- **`TurnContext` — the perishable header.** Speaker, capture instant (epoch float), IANA
  timezone, and a bounded window of prior turns. Constructed by the caller, or defaulted to
  "now, UTC, no speaker, no window". This is the only thing in the system that records *when and
  by whom* something was said, as opposed to when it was stored.
- **`resolve_references(...)` — the stage.** One structured LLM call per accepted candidate,
  schema-constrained, re-validated in Python, never raising, always returning a usable
  `Resolution`.
- **`Resolution` — the carrier.** `statement` (resolved rewrite, or verbatim when nothing
  resolved), `references` (per-reference records), aggregate `status`, `valid_from` (optional),
  `degraded` (whether the LLM path failed open).
- **`ResolutionRecord` / `ResolutionLog` — the sidecar.** A Popoto model keyed by candidate
  identity holding the full structure: every reference's surface, offsets, kind, status,
  resolution, assumption line, `evidence_gap` candidate list and clarifying question, plus the
  context header. M7 (#566) reads it; nothing else has to.
- **The `res:{status}` subject tag — the flag that travels.** One low-entropy tag on the journal
  entry itself, so an `assumed` fact cannot be surfaced anywhere without its flag being on the
  record that carries it.
- **`Defaults.M4_*` — pinned constants and a deploy kill switch.** Window bounds, reference caps,
  string caps; plus `M4_RESOLUTION_ENABLED`, on by default, disableable by environment for
  PyPI adopters who cannot edit model code.

### Flow

Turn arrives → `extract_memories(text, context=TurnContext(...))` → candidates enumerated →
per candidate: **verdict** → *(accept)* → **resolution call** → typed `Resolution` →
journal entry written with resolved `statement`, untouched `verbatim`, `res:{status}` tag,
true `captured_at`, and `valid_from` when an onset was anchored → sidecar `ResolutionRecord`
written with the full evidence → `ExtractedFact` returned carrying both forms and the status.

Worked example — Tuesday 2026-09-01, speaker `user`, previous turn *"How's Dana settling in on
Atlas?"*, candidate span *"she wants the report filed by Friday"*:

| Reference | kind | status | outcome |
|---|---|---|---|
| `she` | pronoun | `resolved` | → "Dana" (prior turn names her) |
| `Friday` | relative_time | `resolved` | → 2026-09-04, role `deadline` → **no `valid_from`** |
| `the report` | definite_reference | `evidence_gap` | candidates: ["the Atlas onboarding report", "the Q3 status report"]; question: "Which report does Dana need filed by Friday?" |

Aggregate status `evidence_gap`; `statement` = *"Dana wants the report filed by Friday
2026-09-04."*; `verbatim` = *"she wants the report filed by Friday"*; tag `res:evidence_gap`.
A second candidate, *"Dana's been on Atlas since March"*, yields one `relative_time` reference
with role `onset` → **`valid_from` = 2026-03-01T00:00 in the speaker's timezone**.

### Technical Approach

**1. New module `src/popoto/extraction/resolution.py` (pure — no Redis import).**

Types: `ResolutionStatus`, `ReferenceKind` (`pronoun | relative_time | definite_reference`),
`TemporalRole` (`onset | deadline | mention | none`), `Reference`, `TurnContext`, `WindowTurn`,
`Resolution` — all frozen dataclasses / `str` enums, matching `verdict.py:68-202`'s shape.

Pinned, non-user-configurable constants in-module (precedent: `verdict.py:205-263`,
`claude.py:38-70`): `RESOLUTION_MODEL = "claude-haiku-4-5-20251001"` (same tier as the verdict
call — this runs per accepted candidate), `RESOLUTION_MAX_TOKENS`, `RESOLUTION_PROMPT`,
`RESOLUTION_SCHEMA`.

The call copies `verdict.py:283-309` exactly — GA structured-output path,
`output_config={"format": {"type": "json_schema", "schema": RESOLUTION_SCHEMA}}`, no beta header,
`additionalProperties: False`, and **one level of nesting only** (an object holding a flat array
of flat objects), because Anthropic's schema support is not full JSON Schema (Research).

**2. Python re-validation is the contract, not the schema.** Format adherence is guaranteed;
content correctness is not (Research). `_parse_reply` enforces, per reference:

- `candidate.text[start:end] == surface` — the BioCoref verbatim constraint, mechanically checked.
  A model that invents or rephrases a surface loses that reference.
- known enum members for `kind`, `status`, `temporal_role`;
- `resolved` requires a non-empty `resolved_text` (and, for `relative_time`, a parseable ISO-8601
  `resolved_iso`); `assumed` additionally requires a one-line `assumption`
  (≤ `M4_ASSUMPTION_MAX_CHARS`, no newlines); `evidence_gap` requires 2–4 `candidates` **and** a
  non-empty `question` (≤ `M4_QUESTION_MAX_CHARS`); `indeterminate` requires all of those to be
  absent;
- at most `M4_MAX_REFERENCES_PER_CANDIDATE` references;
- `statement` non-empty and length-bounded relative to `verbatim`
  (`M4_STATEMENT_MAX_GROWTH_FACTOR` × len + `M4_STATEMENT_MAX_GROWTH_CHARS`), so the model cannot
  turn a clause into a paragraph of invention.

A failing reference is dropped and the aggregate status floors at `indeterminate`; a failing
envelope yields the degraded `Resolution`. **Dates are never trusted as numbers from the model** —
it emits ISO-8601 and Python converts with `datetime.fromisoformat` + `zoneinfo.ZoneInfo(
context.timezone)`, attaching the context zone when the value is naive. This is the direct lesson
of #519/#521.

**3. Fail-open, per M3's precedent.** `resolve_references` never raises and never returns `None`.
Missing `anthropic`, a client exception, a malformed reply, or `M4_RESOLUTION_ENABLED = False` all
produce `Resolution(statement=verbatim, status=indeterminate, references=(), valid_from=None,
degraded=True)` — the entry is still captured, byte-identical to M3's output. M4 is explicitly
"quality loss, not corruption" when unavailable.

**4. `valid_from` emission — the onset rule.** `valid_from` is the instant a claim *became true*,
not every date the claim mentions. Emitting it for a deadline would be a data error: *"file it by
Friday"* is true **now**, not from Friday. So the model classifies each `relative_time` reference
with a `temporal_role`, and the stage emits `valid_from` **only** when exactly one reference has
`kind == relative_time`, `status in {resolved, assumed}`, and `role == onset`. Zero onsets → no
emission (M1's default, the capture instant). Two or more onsets → **no emission**, and the
aggregate status floors at `assumed` with a stated assumption — abstaining is cheaper than
guessing which onset owns the interval. Threaded as `journal.append(at=resolution.valid_from)`;
spike-1 proved a backdated `at` is stored exactly, and spike-1b that a future one is legal.

**5. Answers to the #588 comment's two questions.**

- *(1) What does the stage do when a re-resolution supplies a different `valid_from` for an
  already-open interval?* **The situation is unreachable, and the plan keeps it that way.**
  `JournalEntry.entry_id` is an `AutoKeyField`, so every capture is a fresh member with its own
  interval (spike-3); double-capture of one candidate is prevented by M3's claim + `pending`/
  terminal probe, not by the validity index. M4 therefore **never** calls
  `ValidityField.get_valid_from()` to reconcile and never retries with an effective value —
  absorbing a conflict is exactly the silent behaviour #588 removed. A `ValidityValidFromConflict
  Error` reaching this path would be a genuine bug and is left to M3's existing
  `reject(assembly_failed)` handler with the exception class name in `detail_code`
  (`decision_log.py:790-810`). A grep-based anti-criterion in Verification asserts the absorb
  pattern never appears.
- *(2) Does M4 take on the `execute_supersede` → `save_and_supersede` conversion at
  `provenance_journal.py:1127`?* **No — explicitly declined**, and the in-code comment naming
  #563 is corrected as part of this work. The conversion is on M1's *annotation* path, which M4
  does not use (M4 appends targetless `assert` entries). `SupersessionProtocol` still cannot
  express the explicit `old_member` and `assert_valid_from=False` that path needs, and the D7
  pre-flight (`:960-1013`) carries firewall, cross-agent-ownership and kind/target checks the
  protocol cannot express. Converting it would add risk to a shipped write path for zero M4
  benefit. Task 7 rewrites the comment to record the declination and points it at a follow-up
  issue instead of at #563.

**6. Where the status lives — both places, deliberately** (the issue's open question). The full
structure lives in the `ResolutionRecord` sidecar, because `JournalEntry` must not be subclassed
and its field set is guarded by `_require_journal_shape` (`provenance_journal.py:1227-1264`) —
adding fields to a shipped, append-only model is a migration this plan will not take on. But a
sidecar alone fails AC #4 ("`assumed` records carry a one-line stated assumption **retrievable
with the fact**") and walks straight into the design study's top threat, so the *status* also
rides on the entry as the indexed subject tag `res:{status}`, verified retrievable in spike-2.
Flag transport is on the record; evidence is in the sidecar.

**7. Plumbing, in three small widenings.** `AuditableExtractionConfig` gains
`resolution_provider: Any = None` (the test seam, identical in spirit to `verdict_provider`);
`DecisionLog.assemble`/`_reconcile_pending`/`_append_and_transition` gain `resolution=None` and
derive `speaker`/`captured_at` from `resolution.context` when the caller did not pass them;
`extract_memories`/`_extract_memories_auditable` gain `context=None`. No signature loses a
parameter and no default changes.

**8. Constants** go in `src/popoto/fields/constants.py` under a new
`# -- reference resolution (extraction/resolution.py, #563) --` banner, `M4_`-prefixed (precedent:
`M3_ASSEMBLY_CLAIM_TTL_MS`, `:455`): `M4_RESOLUTION_ENABLED` (env-backed kill switch built with
the module's existing `_read_*` helper convention, `:44-126`), `M4_WINDOW_MAX_TURNS`,
`M4_WINDOW_MAX_CHARS`, `M4_MAX_REFERENCES_PER_CANDIDATE`, `M4_EVIDENCE_GAP_MIN_CANDIDATES`,
`M4_EVIDENCE_GAP_MAX_CANDIDATES`, `M4_ASSUMPTION_MAX_CHARS`, `M4_QUESTION_MAX_CHARS`,
`M4_STATEMENT_MAX_GROWTH_FACTOR`, `M4_STATEMENT_MAX_GROWTH_CHARS`. Each carries an inline comment
stating why it is not a tunable. `tests/benchmarks/test_defaults_sync.py` must be updated in the
same commit.

**9. Window bounding is oldest-first truncation.** The window is capped at
`M4_WINDOW_MAX_TURNS` turns **and** `M4_WINDOW_MAX_CHARS` characters; when both are exceeded the
oldest turns are dropped first, and truncation is recorded on the `ResolutionRecord` so a later
audit can tell "the window did not contain the antecedent" from "the model missed it".

## Failure Path Test Strategy

### Exception Handling Coverage

M4 introduces four broad `except` sites, and each gets a test asserting **observable** behaviour
(a returned degraded `Resolution`, a logged warning, or a written record) rather than "it didn't
crash":

- [ ] `resolve_references` — client raises → returns `Resolution(degraded=True,
      status=indeterminate, statement == verbatim)`; assert `caplog` carries a
      `POPOTO.extraction` warning naming the candidate id.
- [ ] `resolve_references` — `anthropic` import unavailable (monkeypatch
      `resolution.anthropic_module = None`, mirroring `test_auditable_extraction.py:367-369`) →
      same degraded result, and **no network call attempted**.
- [ ] `_parse_reply` — malformed JSON, wrong `candidate_id`, unknown enum member, a `surface`
      that is not a substring at its offsets, an `evidence_gap` with 1 or 5 candidates, an
      `assumed` with an empty or multi-line assumption, a `statement` that exceeds the growth
      bound → each asserted individually, each producing either a dropped reference (with the
      others surviving) or a degraded envelope.
- [ ] `ResolutionLog.write` raises → warning logged, `assemble` still returns the `entry_id`, the
      journal entry still exists **and still carries its `res:` tag**. This is the invariant that
      makes the sidecar non-load-bearing for the flag guarantee.
- [ ] `SubconsciousMemory._resolve_for` — a resolution provider that raises is an infrastructure
      loss, not a rejection: it maps to the degraded `Resolution` and the candidate is still
      captured. Mirrors `_verdict_for`'s contract (`subconscious_memory.py:622-653`).

No `except Exception: pass` blocks are introduced; every handler logs and returns a typed value.

### Empty/Invalid Input Handling

- [ ] `resolve_references` with an empty/whitespace candidate span → returns the degraded
      `Resolution` without calling the client (the candidate cannot reach here anyway — M3 rejects
      it as `empty_turn` — so this is a defence-in-depth test).
- [ ] `TurnContext` with an empty window, a `None` speaker, and an unknown timezone string → the
      unknown zone falls back to UTC with a logged warning; nothing raises.
- [ ] `TurnContext.captured_at` of `None`/`NaN`/`inf` → coerced to the clock, matching
      `_coerce_instant`'s rejection semantics rather than propagating a bad float into a ZSET
      score.
- [ ] A model reply with an **empty** `references` array → valid: `status = resolved`,
      `statement == verbatim`, no `valid_from`. "Nothing needed resolving" is a legitimate,
      non-degraded outcome and must not be conflated with `indeterminate`.
- [ ] `valid_from` candidates: zero onsets, exactly one, and two-or-more — the three-branch rule
      is tested exhaustively.

### Error State Rendering

M4 has no user-visible UI. Its "rendering" surface is the stored record, so the equivalent tests
assert that an error state is **legible in the data**: a degraded resolution stores
`degraded=True` on the `ResolutionRecord` and tags the entry `res:indeterminate`, so a downstream
reader can always distinguish "the model said it could not resolve this" from "the resolution
stage never ran".

## Test Impact

New file `tests/test_reference_resolution.py` carries the bulk. Existing tests affected:

- [ ] `tests/test_auditable_extraction.py` (1445 lines) — **UPDATE**. Assembly-path tests assert
      `statement == verbatim` today. With the default (no resolution provider configured, no
      `anthropic` client in tests) the stage fails open and `statement` stays byte-identical, so
      most tests are expected to pass **unchanged** — that is itself the compatibility assertion.
      The tests that construct `AuditableExtractionConfig` and the `assemble(...)` call-shape
      tests need the new keyword threaded. Any test asserting the exact `subjects` list must be
      updated for the added `res:` tag.
- [ ] `tests/test_auditable_extraction.py::` the `_FakeJournal` double (`:136-186`) — **UPDATE**:
      must accept and record `captured_at` and `at` so the new arguments can be asserted.
- [ ] `tests/test_subconscious_memory.py`, `tests/test_subconscious_memory_integration.py` —
      **UPDATE**: add coverage for the new `context=` argument; existing calls omit it and must
      keep working.
- [ ] `tests/test_extraction.py` — **UPDATE**: `ExtractedFact`'s new fields default to `None` on
      the legacy provider path; add one assertion pinning that, so the additive-only promise is
      mechanically enforced.
- [ ] `tests/benchmarks/test_defaults_sync.py` — **UPDATE**: add the ten `M4_*` constants.
- [ ] `tests/test_provenance_journal.py`, `tests/test_validity_field.py` — **no change expected**.
      M4 adds no M1/V0 behaviour; if either suite moves, that is a regression signal, not a
      migration.

No expected-failure markers exist for this work (`grep -rn 'pytest.mark.xfail\|pytest.xfail('
tests/` finds nothing related to resolution, coreference, or `valid_from`), so there are no
xfail conversions.

## Rabbit Holes

- **Cross-session entity linking at write time.** Building an entity graph so "she" resolves to a
  person record across conversations. Both the issue's recon and the design study reject it: it is
  reconstructable later from `verbatim` + context, and at write time it turns a bounded per-turn
  call into an unbounded retrieval problem. The window is the conversation, full stop.
- **A calibrated numeric confidence per resolution.** Already dropped in recon. Sub-35B models
  emit uncalibrated floats; four enum statuses are what downstream actually branches on. Do not
  quietly reintroduce it as a "score" field.
- **Writing a date parser.** No `dateparser`/`dateutil`, no regex date grammar. The model emits
  ISO-8601; Python does `fromisoformat` + `ZoneInfo`. Anything the model cannot express as
  ISO-8601 is `indeterminate` — a two-line branch instead of a natural-language-date library.
- **Batching all accepted candidates of a turn into one call.** Tempting for cost, but it
  re-couples candidates that M3 deliberately isolated ("one flaky candidate must not cost the
  audit trail of the others") and makes per-candidate `candidate_id` verification much weaker.
  Revisit only with measured cost data.
- **Rewriting `EXTRACTION_PROMPT`.** `claude.py`'s "makes sense on its own" sentence lives on the
  *legacy* provider path, which the auditable pipeline does not use. Leave it alone; deleting or
  editing it changes a path M4 does not own.
- **Making `TurnContext` a stateful conversation buffer inside `SubconsciousMemory`.** Owning
  turn history means owning eviction, ordering, and multi-process coherence. The caller passes the
  window; the recipe does not accumulate one.
- **Adding fields to `JournalEntry`.** The shape guard (`provenance_journal.py:1227-1264`) and the
  no-subclassing rule exist for good reasons, and M1 is append-only with no delete path. The
  sidecar plus one subject tag gets the same result with no migration.

## Risks

### Risk 1: An `assumed` fact is surfaced without its assumption
**Impact:** the highest-severity failure mode in the design study. A guess that reads as a fact
is worse than no fact — it is confidently wrong, and the flag being "available in a sidecar"
means nothing if every retrieval path forgets to join it.
**Mitigation:** the status rides on the entry itself as the indexed `res:{status}` subject tag
(spike-2 proved it survives the firewall and is queryable), so the flag is transported by the same
object as the content. Test: retrieve an `assumed` entry through the ordinary journal read path
and assert the tag is present without any sidecar lookup. The assumption *text* is in the sidecar,
but the *fact that there is one* is inseparable from the record.

### Risk 2: The model rewrites the statement into something the speaker did not say
**Impact:** silent fabrication in the field the rest of the system reads.
**Mitigation:** three layers. (a) `verbatim` is never touched, so the original always exists;
(b) each reference's `surface` must be a literal substring at its declared offsets, mechanically
checked; (c) `statement` length is bounded relative to `verbatim`. A violation degrades to
verbatim rather than storing the rewrite. Tested with a deliberately over-expanding fake reply.

### Risk 3: `valid_from` is emitted for a deadline, corrupting membership
**Impact:** *"file it by Friday"* would open its interval on Friday, so the fact would be
**invisible to as-of retrieval until Friday** — a silently missing memory, the hardest kind to
notice.
**Mitigation:** the onset rule (Technical Approach §4): emission requires exactly one
`relative_time` reference with `role == onset`. Deadlines and mentions never emit. Three tests
(zero/one/many onsets) plus one explicitly asserting that a deadline-only candidate stores
`valid_from == captured_at`.

### Risk 4: Per-candidate LLM cost on a write path
**Impact:** one extra call per *accepted* candidate roughly doubles capture-time LLM spend for the
auditable pipeline.
**Mitigation:** resolution runs only after `accept`, so it is bounded by the acceptance rate, not
the candidate count; the model is the cheap tier (same as the verdict call); `max_tokens` is
pinned low; and `M4_RESOLUTION_ENABLED=false` is a deploy-level kill switch that restores M3's
exact behaviour. Batching is explicitly deferred (Rabbit Holes) pending measured data.

### Risk 5: Timezone handling repeats #519/#521
**Impact:** an onset resolved in the wrong zone shifts `valid_from` by hours to a day — enough to
put an entry on the wrong side of an as-of query boundary.
**Mitigation:** the context header carries an explicit IANA zone; the model emits ISO-8601 only;
Python attaches `ZoneInfo(context.timezone)` to naive values and converts to an epoch float
exactly once. An unknown zone falls back to UTC **with a warning and `degraded=True`**, never
silently. Tests cover a non-UTC zone, a DST boundary date, and an unknown zone string.

### Risk 6: DB 15 contention makes the new suite look broken
**Impact:** phantom failures (73–158 historically) mis-read as regressions in a plan that touches
Redis-backed models.
**Mitigation:** pin `POPOTO_TEST_DB` per run (Prerequisites), and state the DB alongside every
count. Remember the six tests that fail by construction on a non-15 DB (`do-sdlc.md`); that exact
set is expected noise.

## Race Conditions

### Race 1: Two runners resolve and assemble the same candidate concurrently
**Location:** `src/popoto/extraction/decision_log.py:609-695` (claim/probe), plus the new
resolution call in `subconscious_memory.py:682-713`.
**Trigger:** two processes replaying the same `(agent_id, turn_id)`.
**Data prerequisite:** the `pending` row and the `cand:` subject tag must exist before any
reconciliation probe reads them — already guaranteed by M3's ordering.
**State prerequisite:** exactly one journal entry per candidate.
**Mitigation:** unchanged from M3 — the claim (`M3_ASSEMBLY_CLAIM_TTL_MS`) serialises assembly and
the loser no-ops. M4 deliberately runs the resolution call **outside** the claim (it is a slow
network call and holding a 30 s claim across it would expire the claim mid-flight); the wasted
duplicate LLM call is the accepted cost, and the loser's `Resolution` is discarded without a
write. **No new lock is introduced.**

### Race 2: Journal append succeeds, sidecar write fails
**Location:** the new `ResolutionLog.write` call in `_append_and_transition`
(`decision_log.py:752-822`).
**Trigger:** a Redis hiccup between the two writes; they are deliberately not one transaction
(the journal append may itself already be a pipelined multi-command write).
**Data prerequisite:** none — the sidecar is evidence, not the flag.
**State prerequisite:** an `assumed`/`evidence_gap` entry must never be indistinguishable from a
`resolved` one.
**Mitigation:** ordering is chosen so the **entry carries its `res:` tag before the sidecar
exists**, making the failure mode "flagged but the evidence is missing" rather than "unflagged
guess". A missing sidecar for a tagged entry is detectable by a join and is the sweep target for
M9 (#568). Tested by forcing the sidecar write to raise.

### Race 3: Clock skew between `captured_at` and `valid_from`
**Location:** `provenance_journal.py:902, 929, 944`.
**Trigger:** a caller supplies a `TurnContext.captured_at` from a different host than the one
running the write.
**Data prerequisite:** none.
**State prerequisite:** `valid_from` for a *non-onset* capture should equal the capture instant.
**Mitigation:** both values come from the **same** `TurnContext` object in a single call, so they
cannot disagree with each other; only their relation to the server clock can drift, which V0's
membership semantics already tolerate (`valid_from <= t`). No mitigation code; documented so a
reviewer does not mistake it for a defect.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #566] **Asking any clarifying question.** `evidence_gap` records are written as
  data and never surfaced as a prompt, a question, or a side effect from this module. M7 owns the
  rationed question channel and its value-of-information gate.
- [SEPARATE-SLUG #564] **Cross-entry reconciliation, equivalence classes, and contradiction
  detection.** M4 resolves references *within* one capture; deciding that two captures say the
  same or opposite things is M5's job.
- [SEPARATE-SLUG #606] **Converting `provenance_journal.py:1127`'s `execute_supersede` to
  `save_and_supersede`.** Explicitly declined here (Technical Approach §5) and filed as its own
  issue; this plan only re-points the stale in-code comment at it.
- [SEPARATE-SLUG #489] **Extending the extraction-eval harness to score resolution quality.**
  Measuring resolution accuracy against a labelled corpus belongs with the existing eval axis, not
  in the module that produces it.
- [SEPARATE-SLUG #568] **A sweep for tagged entries with a missing sidecar** (Race 2's detection
  path) and for stale `pending` rows. M9's seeded audit harness owns write-path auditing.

Everything else the issue asks for is in scope for this plan.

## Update System

No update-system changes required. Popoto is a library plus an MkDocs site: there is no deployment
topology, no config file to propagate, and no migration. M4 adds a new Redis keyspace
(`ResolutionRecord:*`) that is created lazily on first write; existing installations that never
enable the auditable path never create it, and existing journal entries are untouched and remain
readable. The one operator-facing knob is the `M4_RESOLUTION_ENABLED` environment kill switch,
which is documented in `docs/guides/tuning-magic-numbers.md`.

## Agent Integration

No agent/MCP integration required. This is an internal write-path stage: the capability is reached
through `SubconsciousMemory.extract_memories(...)`, which harness integrations
(`src/popoto/integrations/`) already call. No new MCP tool, no new entry point, and nothing in
`mcp_servers/`/`.mcp.json` (which this repo does not have) needs to change. The new public names
are exported through `popoto.extraction`'s existing PEP-562 lazy `__getattr__`
(`src/popoto/extraction/__init__.py:243-268`) so that importing `popoto.extraction` still does not
probe for `anthropic`.

## Documentation

### Feature Documentation
- [ ] Create `docs/features/reference-resolution.md` — the four-way status ladder with one worked
      example per status, the `TurnContext` header, the onset rule for `valid_from`, the
      `res:{status}` tag contract, the `ResolutionRecord` shape for M7 consumers, and the
      `M4_RESOLUTION_ENABLED` kill switch with its degraded behaviour spelled out.
- [ ] Add the row to `docs/features/README.md`'s index table.
- [ ] Cross-link from `docs/features/auditable-extraction.md` (M3 hands off here — and its
      "distillation is M4's job" note now has a destination) and from
      `docs/features/provenance-journal.md` (`statement` vs `verbatim`, and who sets `valid_from`).
- [ ] `docs/features/validity-and-supersession.md` — one paragraph: capture-time `valid_from` now
      has a producer, and why a capture never conflicts on an interval (spike-3).

### External Documentation Site
- [ ] Add `features/reference-resolution.md` to `mkdocs.yml` nav, in the memory-primitives group
      immediately after `features/auditable-extraction.md` (`mkdocs.yml:52`).
- [ ] Document the ten `M4_*` constants in `docs/guides/tuning-magic-numbers.md`, including the
      env kill switch.
- [ ] `mkdocs build --strict` passes.

### Inline Documentation
- [ ] Module docstring on `resolution.py` stating the four statuses, the fail-open contract, and
      that the model never emits an epoch number.
- [ ] Docstrings on every new public name; the `valid_from` onset rule spelled out where it is
      computed, not only in the plan.
- [ ] Rewrite the stale comment at `provenance_journal.py:1119-1142` to point at #606 instead of
      #563, recording that M4 declined the conversion and why.
- [ ] Update `decision_log.py:760-767`'s "Distillation is M4's job" comment to describe what M4
      now actually does on that line.

## Success Criteria

- [ ] Given a turn containing a pronoun, a relative date, and a definite reference, the stage
      emits resolutions with correct absolute anchoring when the window determines them
      (issue AC #1).
- [ ] When the window does not determine a referent, the output is a typed abstention —
      `evidence_gap` with 2–4 candidates and one question, or `indeterminate` — never a silent
      guess (issue AC #2).
- [ ] `verbatim` is byte-identical to the candidate span on every record regardless of resolution
      outcome, including the degraded path (issue AC #3).
- [ ] `assumed` records carry a one-line stated assumption in the sidecar **and** a `res:assumed`
      tag on the journal entry itself, retrievable without a sidecar join (issue AC #4).
- [ ] `tests/test_reference_resolution.py` covers all four statuses (issue AC #5).
- [ ] An onset reference emits `valid_from`; a deadline reference does not; two onsets emit none.
- [ ] With `M4_RESOLUTION_ENABLED = False`, the auditable path's output is byte-identical to
      M3's: `statement == verbatim`, `valid_from == captured_at`, no `res:` tag, no sidecar row.
- [ ] `speaker` and a true `captured_at` reach the journal entry when a `TurnContext` supplies
      them — closing the seam M3 left unused.
- [ ] Full suite green on a pinned test DB, with the DB stated alongside the count.
- [ ] `mypy src/` shows no new errors versus base, measured in the same redis-py environment.
- [ ] Tests pass (`/do-test`), documentation updated (`/do-docs`).

## Team Orchestration

The lead agent coordinates and never builds directly.

### Team Members

- **Builder (resolution core)**
  - Name: `resolution-builder`
  - Role: the pure module — types, prompt, schema, the LLM call, and Python re-validation
  - Agent Type: builder
  - Resume: true

- **Builder (persistence + plumbing)**
  - Name: `plumbing-builder`
  - Role: `ResolutionRecord`/`ResolutionLog`, the `Defaults.M4_*` block, and the three call-layer
    widenings
  - Agent Type: builder
  - Domain: Redis/Popoto data — models must declare `Field` instances only, key patterns follow
    `ClassName:key_value`, no Redis modules (Valkey compatibility), constants pinned in
    `Defaults` not exposed as kwargs
  - Resume: true

- **Test engineer**
  - Name: `resolution-tester`
  - Role: `tests/test_reference_resolution.py` and the updates to the four existing suites
  - Agent Type: test-engineer
  - Resume: true

- **Validator**
  - Name: `resolution-validator`
  - Role: verifies acceptance criteria and the kill-switch parity claim against a pinned DB
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: `resolution-documentarian`
  - Role: feature doc, nav, cross-links, constants guide, and the two stale in-code comments
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

Tasks 1 and 2 are independent and parallel; everything downstream serialises, because tasks 3–5
all edit call sites that tasks 1–2 define.

### 1. Resolution core module
- **Task ID**: build-resolution-core
- **Depends On**: none
- **Validates**: `tests/test_reference_resolution.py` (create)
- **Informed By**: spike-4 (statement ≠ verbatim is accepted by M1); Research (GA
  `output_config` path, flat schema, BioCoref verbatim constraint, ISO-8601-only dates)
- **Assigned To**: resolution-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/extraction/resolution.py` with **no Redis import**: `ResolutionStatus`,
  `ReferenceKind`, `TemporalRole`, `Reference`, `WindowTurn`, `TurnContext`, `Resolution`.
- Pin `RESOLUTION_MODEL = "claude-haiku-4-5-20251001"`, `RESOLUTION_MAX_TOKENS`,
  `RESOLUTION_PROMPT`, `RESOLUTION_SCHEMA` in-module under a "Pinned, non-user-configurable
  constants" header, copying `verdict.py:205-263`'s convention verbatim in spirit.
- Implement `_request_resolution(client, candidate, turn_text, context)` mirroring
  `verdict.py:283-309`: GA `output_config={"format": {"type": "json_schema", ...}}`,
  `additionalProperties: False`, one nesting level.
- Implement `_parse_reply(...)` with every check in Technical Approach §2, including the
  substring-at-offsets check and the per-status required-field matrix.
- Implement `_to_epoch(iso, tz)` using `datetime.fromisoformat` + `zoneinfo.ZoneInfo`; naive values
  take the context zone; an unknown zone falls back to UTC with a warning and `degraded=True`.
  **The model never emits an epoch number.**
- Implement the `valid_from` onset rule: exactly one `relative_time` + `resolved|assumed` +
  `onset` → emit; zero or 2+ → `None` (and 2+ floors the aggregate status at `assumed`).
- Implement `resolve_references(candidate, turn_text, context, client=None) -> Resolution`:
  never raises, never returns `None`, honours `Defaults.M4_RESOLUTION_ENABLED`, and uses the same
  monkeypatchable `anthropic_module` probe as `verdict.py:54-63`.
- Export the public names through `popoto.extraction.__getattr__` (`__init__.py:243-268`) so
  importing the package still does not probe `anthropic`.

### 2. Constants block
- **Task ID**: build-constants
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_defaults_sync.py`
- **Assigned To**: plumbing-builder
- **Agent Type**: builder
- **Parallel**: true
- Add the `# -- reference resolution (extraction/resolution.py, #563) --` banner to
  `src/popoto/fields/constants.py` after the M3 block (`:445-455`), with the ten `M4_*` constants
  from Technical Approach §8, each carrying an inline comment stating why it is not a tunable.
- Build `M4_RESOLUTION_ENABLED` with the module's existing `_read_*` env-helper convention
  (`:44-126`), default **True**, so PyPI adopters get a deploy-level kill switch.
- Update `tests/benchmarks/test_defaults_sync.py` in the same commit.

### 3. Sidecar model and log
- **Task ID**: build-resolution-log
- **Depends On**: build-resolution-core, build-constants
- **Validates**: `tests/test_reference_resolution.py`
- **Informed By**: spike-2 (`res:*` tags survive the firewall and are queryable)
- **Assigned To**: plumbing-builder
- **Agent Type**: builder
- **Domain**: Redis/Popoto data
- **Parallel**: false
- Create `src/popoto/extraction/resolution_log.py` with `ResolutionRecord(Model)`: `agent_id`,
  `turn_id`, `candidate_id` as `KeyField`s (composite identity — **never** `AutoKeyField`, per
  `decision_log.py:13-23`), plus non-indexed `status`, `statement`, `verbatim`, `references_json`,
  `valid_from`, `entry_id`, `speaker`, `captured_at`, `timezone`, `window_truncated`, `degraded`,
  `written_at`.
- Serialise `references` with `json.dumps` into a `StringField` (**not** msgpack — the record is
  meant to be readable by M7 and by a human with `redis-cli`).
- Implement `ResolutionLog.write(...)` (idempotent by composite key) and
  `ResolutionLog.get(agent_id, turn_id, candidate_id)`; `write` failures log a warning and return
  `False` rather than raising.
- No TTL, matching M3's decision log; retention/sweeps are M9 (#568).

### 4. Pipeline plumbing
- **Task ID**: build-plumbing
- **Depends On**: build-resolution-log
- **Validates**: `tests/test_auditable_extraction.py`, `tests/test_subconscious_memory.py`
- **Informed By**: spike-1 (`append(at=<past>)` stores the backdated `valid_from` exactly);
  spike-1b (future onsets legal)
- **Assigned To**: plumbing-builder
- **Agent Type**: builder
- **Parallel**: false
- `AuditableExtractionConfig` (`decision_log.py:1047-1071`): add `resolution_provider: Any = None`
  and document it as the test seam.
- `DecisionLog.assemble` (`:609`), `_reconcile_pending` (`:697`) and `_append_and_transition`
  (`:752`): add `resolution=None`; derive `speaker`/`captured_at` from `resolution.context` when
  the caller did not supply them; pass `statement=resolution.statement`,
  `at=resolution.valid_from`, `captured_at=…`, and append `f"res:{resolution.status.value}"` to
  `subjects`. `verbatim=candidate.text` **must not change**.
- Call `ResolutionLog.write(...)` **after** a successful append so it can carry `entry_id`; a
  failure is a logged warning and never changes the return value (Race 2).
- `SubconsciousMemory.extract_memories` (`subconscious_memory.py:426`) and
  `_extract_memories_auditable` (`:655`): add `context=None`, default to `TurnContext.now()`, and
  add `_resolve_for(candidate, turn_text, context)` mirroring `_verdict_for`'s
  provider-or-callable dispatch and its raise→degrade contract (`:622-653`).
- `ExtractedFact` (`extraction/__init__.py:39-75`): add `verbatim`, `resolution_status`,
  `assumption`, all defaulting to `None`; set `text=resolution.statement` on the auditable path.
- When `Defaults.M4_RESOLUTION_ENABLED` is false, skip the call entirely — no tag, no sidecar, no
  `at=` — so the path is byte-identical to M3.

### 5. Tests
- **Task ID**: build-tests
- **Depends On**: build-plumbing
- **Validates**: `tests/test_reference_resolution.py` (create), plus the five files in Test Impact
- **Assigned To**: resolution-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Create `tests/test_reference_resolution.py` with hand-rolled fakes (no `unittest.mock`),
  following `test_auditable_extraction.py:80-206`: a `FakeClient` returning canned JSON and a
  `_StubResolution` provider.
- One test class per status: `resolved`, `assumed`, `evidence_gap`, `indeterminate` — each
  asserting the stored `statement`, the untouched `verbatim`, the `res:` tag on the entry, and the
  sidecar contents.
- The `valid_from` matrix: onset / deadline / mention / zero onsets / two onsets / future onset /
  non-UTC zone / DST-boundary date / unknown zone.
- Every bullet in Failure Path Test Strategy.
- The kill-switch parity test: with `M4_RESOLUTION_ENABLED = False`, assert `statement ==
  verbatim`, no `res:` tag, no sidecar row (restore the flag in an autouse fixture, following
  `test_provenance_journal.py:146-159`).
- Update the five existing files per Test Impact, including teaching `_FakeJournal` about
  `captured_at`/`at`.

### 6. Validation
- **Task ID**: validate-resolution
- **Depends On**: build-tests
- **Assigned To**: resolution-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the Verification table on a **pinned** `POPOTO_TEST_DB` (not 15, not 0) and report the DB
  alongside every count.
- Confirm the editable install resolves to this checkout and that the full extras are installed
  before trusting any number (CLAUDE.md's five worktree gotchas).
- Measure `mypy src/` against base in the same redis-py environment and report the delta with the
  redis-py version stated.

### 7. Stale-comment corrections
- **Task ID**: build-comment-fixes
- **Depends On**: build-plumbing
- **Assigned To**: resolution-documentarian
- **Agent Type**: documentarian
- **Parallel**: false
- Rewrite `provenance_journal.py:1119-1142`'s comment: M4 declined the `save_and_supersede`
  conversion, the work is tracked as **#606**, and the reason is that M4 never reaches the
  annotation branch. Remove the "#563" attribution.
- Update `decision_log.py:760-767` to describe what M4 now does rather than promising it.

### 8. Documentation
- **Task ID**: document-feature
- **Depends On**: build-comment-fixes, validate-resolution
- **Assigned To**: resolution-documentarian
- **Agent Type**: documentarian
- **Parallel**: false
- Everything in the Documentation section.

### 9. Final validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: resolution-validator
- **Agent Type**: validator
- **Parallel**: false
- Re-run the Verification table, confirm every Success Criterion, and produce the final report
  with the environment stated.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| New suite passes | `python -m pytest tests/test_reference_resolution.py -q` | exit code 0 |
| M3 suite still passes | `python -m pytest tests/test_auditable_extraction.py -q` | exit code 0 |
| M1/V0 suites unmoved | `python -m pytest tests/test_provenance_journal.py tests/test_validity_field.py -q` | exit code 0 |
| Constants sync | `python -m pytest tests/benchmarks/test_defaults_sync.py -q` | exit code 0 |
| Full suite | `python -m pytest -q` | exit code 0 |
| Lint clean | `python -m ruff check src/` | exit code 0 |
| Format clean | `python -m black --check src/ tests/` | exit code 0 |
| Type check | `python -m mypy src/` | exit code 0 |
| Docs build | `python -m mkdocs build --strict` | exit code 0 |
| All four statuses tested | `grep -c -E "evidence_gap\|indeterminate\|res:assumed\|res:resolved" tests/test_reference_resolution.py` | output > 3 |
| Resolution core is Redis-free | `grep -cE "^from \.\.(fields\|models)\|POPOTO_REDIS_DB\|import redis" src/popoto/extraction/resolution.py` | match count == 0 |
| No conflict-absorbing retry (anti-criterion, #588) | `grep -c "get_valid_from" src/popoto/extraction/resolution.py src/popoto/extraction/resolution_log.py src/popoto/extraction/decision_log.py` | match count == 0 |
| No entity-graph rabbit hole (anti-criterion) | `grep -ciE "entity_graph\|cross_session\|entity_link" src/popoto/extraction/resolution.py` | match count == 0 |
| No date-parser dependency (anti-criterion) | `grep -cE "dateparser\|dateutil" src/popoto/extraction/resolution.py pyproject.toml` | match count == 0 |
| No numeric confidence field reintroduced (anti-criterion) | `grep -ciE "confidence *[:=]" src/popoto/extraction/resolution.py src/popoto/extraction/resolution_log.py` | match count == 0 |
| verbatim never rewritten (anti-criterion) | `grep -c "verbatim=resolution" src/popoto/extraction/decision_log.py` | match count == 0 |
| M4 constants pinned in Defaults | `grep -c "M4_" src/popoto/fields/constants.py` | output > 9 |
| Kill switch exists | `python -c "from popoto.fields.constants import Defaults; print(Defaults.M4_RESOLUTION_ENABLED)"` | output contains True |
| Stale #563 attribution removed | `grep -c "#563" src/popoto/recipes/provenance_journal.py` | match count == 0 |
| Feature doc in nav | `grep -c "features/reference-resolution.md" mkdocs.yml` | output > 0 |

Run every `pytest` row with `POPOTO_TEST_DB` pinned to a free database (not 15, not 0) and state
the database alongside the result. The six tests that fail by construction on a non-15 DB
(`docs/sdlc/do-sdlc.md`) are expected noise, not regressions.

## Critique Results

<!-- Populated by /do-plan-critique. Empty until critique runs. -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

The issue listed four questions for the planner. Three are answered above and are recorded here
with their answers; only the ones marked **needs input** are still open.

1. **Window size?** *Answered:* `M4_WINDOW_MAX_TURNS = 8` turns **and**
   `M4_WINDOW_MAX_CHARS = 4000` characters, whichever binds first, truncating oldest-first and
   recording that truncation on the record. Pinned magic numbers, not kwargs. Two bounds rather
   than one because a turn count alone does not bound a prompt and a character count alone can
   slice a turn in half.
2. **Does the four-way status live on the journal entry or in a sidecar?** *Answered:* **both,
   split by role.** The status rides on the entry as the indexed `res:{status}` subject tag (so
   the flag is inseparable from the fact); the evidence — assumption lines, `evidence_gap`
   candidates, questions, the context header — lives in the `ResolutionRecord` sidecar. Adding
   fields to `JournalEntry` is refused: it is shipped, append-only, guarded by
   `_require_journal_shape`, and explicitly not subclassable.
3. **How are `assumed` flags guaranteed to travel with the fact when surfaced?** *Answered:* by
   the tag in (2), verified retrievable in spike-2 through the ordinary journal query path with no
   sidecar join. A test asserts it.
4. **The `valid_from` onset rule — needs input.** The 2026-08-16 amendment lists both *"since
   March"* and *"by Friday"* as `valid_from` producers. This plan emits for **onsets only**: a
   deadline would open the interval in the future and make the fact invisible to as-of retrieval
   until then (Risk 3). I believe the amendment's phrasing was illustrative rather than
   prescriptive, but it is a deliberate narrowing of a written requirement — please confirm.
5. **Two-or-more onsets: abstain or pick the earliest? — needs input.** The plan abstains (no
   `valid_from`, status floored at `assumed`). Picking the earliest is defensible and loses less
   information; abstaining is safer and cheaper to reverse. Which is preferred?
6. **Should a `TurnContext` with no window be allowed to run the stage at all? — needs input.**
   The plan says yes (degraded: the candidate's own full turn plus the clock still resolves most
   relative dates and some pronouns), on the default-ON doctrine. The alternative is to skip
   resolution when no window is supplied, which is more conservative but makes the capability
   invisible to every existing caller — none of which pass a window today.
