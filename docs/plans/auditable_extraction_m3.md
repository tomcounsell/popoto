---
status: Ready
type: feature
appetite: Large
owner: Valor Engels
created: 2026-08-20
tracking: https://github.com/tomcounsell/popoto/issues/562
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-08-26T03:44:19Z
---

# M3 — Auditable extraction: deterministic candidate generator with a per-candidate decision log

## Problem

Popoto's memory extraction turns conversation text into stored facts, but **rejections are
invisible**, so extraction quality cannot be measured without hand-labeling. There is no
intermediate representation to hang a decision on: `ExtractedFact` is just
`(text, entities, importance, confidence)` — no span offsets, speaker, turn id, or candidate
identity. On the LLM path, rejection happens inside the model's prompt with zero visibility.

**Current behavior — three confirmed silent-drop sites, all re-verified at plan time:**

1. Heuristic provider's min-length drop — `src/popoto/extraction/__init__.py:129-130`
   (`if len(sentence) < self._min_length: continue`). A too-short sentence vanishes with no
   record that it was considered and rejected.
2. Claude provider's malformed-fact `continue`s and whole-call failure returns —
   `src/popoto/extraction/claude.py:157,175,178-184,189,192`. A malformed JSON reply, a
   missing `facts` list, or a non-dict/blank-text fact each silently produce `[]` or skip a
   fact.
3. `SubconsciousMemory`'s save loop downgrading failures to warnings —
   `src/popoto/recipes/subconscious_memory.py:408-432`. `except Exception` →
   `logger.warning(...)`, with the fact neither saved nor logged as rejected.

Telemetry (`AssemblyEvent`, `src/popoto/recipes/memory_telemetry.py`) is retrieval-side only;
there is no write-side event model. Issue #489 exists precisely because extraction quality
currently requires manual evaluation.

**Desired outcome:** deterministic code enumerates candidates exhaustively, the LLM is
confined to a per-candidate `accept`/`reject`/`withhold` verdict with a reason code from a
fixed enum (it writes **no free text** into the store), trusted code assembles accepted
candidates into provenance-journal entries, and every candidate terminates in exactly one
logged state: `firewall_drop | accept | reject | withhold`. Extraction precision and recall
become computable offline from the decision log alone — "audit by construction."

## Freshness Check

**Baseline commit:** `67965a1` (`feat(#560): M1 provenance journal ... (#589)`) — `git rev-parse HEAD` at plan time.
**Issue filed at:** 2026-08-13T06:26Z
**Disposition:** Minor drift

**File:line references re-verified:**

- `src/popoto/extraction/__init__.py:125-126` — min-length `continue` — **drifted to
  `:129-130`.** Claim holds: `if len(sentence) < self._min_length: continue` at `:129-130`,
  inside `HeuristicExtractionProvider.extract`.
- `src/popoto/extraction/claude.py:177-192` — malformed-fact `continue`s and whole-call
  `return []` — **drifted, still holds** across `:157,175,178-184,189,192`. `return []` on
  no-text-block, on exception, and on missing `facts`; `continue` on non-dict and on
  blank/missing `text`. The `logger.warning` swallow is present at `:178`.
- `src/popoto/recipes/subconscious_memory.py:367-368` — save loop downgrading failures to
  warnings — **drifted to `:408-432`.** Claim holds: `for fact in facts:` loop at `:408`,
  `except Exception as e: logger.warning("Failed to save extracted memory: %s", e)` at
  `:431-432`.
- `src/popoto/recipes/memory_telemetry.py:103` — retrieval-side only — **still holds.**
  `AssemblyEvent` is emitted by the context assembler (read path); no write-side event model
  exists.

**Cited sibling issues/PRs re-checked:**

- #560 (M1 provenance journal) — **closed/merged as PR #589 (`67965a1`)** since filing.
  Assembly target now exists. `ProvenanceJournal.append(...)` and `JournalEntry` are live.
- #561 (M2 never-record firewall) — **closed/merged as PR #587 (`337b3f0`)** since filing.
  `scan_never_record(text) -> NeverRecordVerdict` (`src/popoto/privacy/never_record.py:417`)
  is the reusable per-candidate firewall primitive. M2 also ships a capped-LTRIM tombstone log
  (`Defaults.NR_TOMBSTONE_LOG_MAX = 1000`) — noted here only as context for what exists in the
  codebase. **It is explicitly not M3's design**: the M3 decision log ships unbounded in v1
  (see Technical Approach → Decision-log retention).

**Commits on main since issue was filed (touching referenced files):**
- `67965a1` (#589, M1) — added `src/popoto/recipes/provenance_journal.py`; added the journal
  import to `subconscious_memory`-adjacent wiring. **Irrelevant to the drop sites
  themselves; adds the assembly target.**
- `337b3f0` (#587, M2) — added `src/popoto/privacy/never_record.py`; injected the turn-level
  firewall scan into `SubconsciousMemory.extract_memories()` (`subconscious_memory.py:388-397`)
  and `NeverRecordMixin` into the save path. **Partially addresses the problem**: it converts
  *blocked* content into a logged tombstone, but the three *rejection* sites above still drop
  silently. The M2 work moved the referenced line numbers by ~15 lines but did not resolve the
  audit gap.

**Active plans in `docs/plans/` overlapping this area:** none. `provenance_journal_m1.md` and
`never_record_firewall.md` are the two dependency plans, both `status: Ready` and already
shipped. No plan currently touches the candidate-generator / decision-log area.

**Notes:** The line drift is mechanical (M1/M2 landing moved code down). No claim was
invalidated. The M2 turn-level firewall and the M3 per-candidate firewall coexist: M2 voids
the whole turn when an off-the-record marker appears anywhere in it; M3 additionally scans
each candidate span so a secret confined to one sentence drops just that candidate. This plan
preserves both.

## Prior Art

- **#489** — "Evaluate LLM extraction vs heuristic" — the measurement this module makes cheap
  and continuous. Established the judged-accuracy framing and the `RawTurnExtractionProvider`
  default on the harness path. **Relevant:** M3's offline precision/recall computation serves
  this evaluation goal directly.
- **#461 / PR #481** — first-class LLM memory-extraction path (the current provider
  architecture). **Relevant:** M3 builds the deterministic-enumeration + verdict stage that
  replaces the current all-in-one-prompt rejection inside the model. No plan previously tried
  a per-candidate decision log.
- **PR #510** — evaluated raw vs heuristic vs Claude extraction on the same slice and chose raw
  turn ingestion as the harness default on judged accuracy. **The measured numbers, recorded
  here rather than paraphrased: heuristic sentence-splitting scored 0.2078 against raw turn
  ingestion's 0.3636** (stated verbatim in `RawTurnExtractionProvider`'s docstring,
  `src/popoto/extraction/__init__.py:159-166`). **Relevant, and uncomfortable:** M3's v1
  candidate shape is sentence spans — the arm #510 measured *worst*. That tension is not
  hand-waved; it is argued explicitly in Technical Approach → "Auditing the measured-worse
  candidate shape, on purpose." Short version: #510 measured a splitter that shipped with no
  per-candidate visibility, and M3 builds exactly the instrument that turns that single
  aggregate number into per-candidate, per-rule evidence — while leaving the harness default
  on raw turn ingestion, untouched.

## Research

No relevant external findings — the feature is purely internal (Popoto ORM + Redis/Valkey +
an LLM call the project already makes). The per-candidate firewall reuses the codebase's own
M2 primitive (`scan_never_record`); no external library informs the decision-log design.

## Spike Results

No spikes were required. All architectural assumptions were resolved by direct code reads of
the two shipped dependency modules (M1 `ProvenanceJournal.append`, M2 `scan_never_record`) and
the three silent-drop sites. The three open questions raised at draft time — candidate span
type (a), decision-log retention (b), and assembly content (c) — were design/judgment calls,
not verifiable assumptions needing a prototype. All three are now resolved by supervisor
decision and folded into the sections below; see Technical Approach and No-Gos.

## Data Flow

1. **Entry point**: `SubconsciousMemory.extract_memories(response_text, ...)` — the same
   post-turn hook the harness (`src/popoto/integrations/service.py:230`) already calls.
2. **Candidate generator** (new, `candidates.py`): deterministically enumerates the v1
   candidate set from `response_text` — one candidate per sentence span, plus one per
   pattern-lifted entity, each carrying turn id, span offsets, and a generator rule. A turn
   with no text or an empty candidate set produces a single logged `reject`(empty) decision,
   not a silent `[]`.
3. **M2 firewall, per candidate** (reuses `scan_never_record`): a candidate whose span trips
   the never-record gate is logged `firewall_drop` with the firewall reason code and is never
   sent to the LLM. Turn-level M2 voiding still runs first (whole-turn `off_the_record`
   marker), unchanged.
4. **LLM verdict stage** (new, `verdict.py`): for each surviving candidate, one LLM call
   returns an enum verdict (`accept | reject | withhold`) plus an enum reason code. The model
   returns the candidate's own id and a verdict — it writes **no free text**. Accepted content
   is byte-identical to the verbatim candidate span.
5. **Decision log — first write, always before any side effect** (new, `decision_log.py`):
   every candidate gets a row keyed by `(agent_id, turn_id, candidate_id)` *before* anything
   irreversible happens to it.
   - `firewall_drop`, `reject`, `withhold` have no downstream side effect, so their first
     write is also their **terminal** write. It is **not** an unconditional write: every
     non-`accept` terminal write is **guarded** — it is a single Lua script that reads the
     row's `state`/`entry_id` and refuses to overwrite a row already terminal `accept` with an
     `entry_id` (see Technical Approach → Terminal-write conflict guard). One row, one terminal
     state — but the write that produces it is conditional, never a bare `HSET`/`.save()`.
   - `accept` **atomically claims** the candidate with a single `SET ... NX PX` op and then
     writes a **`pending`** row (see Technical Approach → Write ordering and → Atomic assembly
     claim). `pending` is a non-terminal intent marker, not a fifth terminal state. A runner
     that loses the claim does **not** append — it no-ops and leaves the row to the winner.
6. **Assembly** (trusted code): `accept`ed candidates are written to the M1 provenance journal
   via `ProvenanceJournal.append(agent_id=<agent_id>, kind='assert', verbatim=<span>,
   statement=<span>, speaker=..., turn_id=..., subjects=[*topic_tags, f"cand:{candidate_id}"])`.
   `agent_id` is keyword-only and **required** — a `None` renders the literal `"None"` into the
   record's Redis key (`provenance_journal.py:562-577`). The `cand:` subject tag is the entry's
   **candidate identity**, and it is what makes recovery reconciliation an identity lookup
   rather than a text match (see Technical Approach → Candidate identity on the journal entry).
   No model-generated free text enters the store; the stored `statement` is the verbatim
   candidate span (distillation is M4's job).
7. **Decision log — terminal transition**: the same `(agent_id, turn_id, candidate_id)` row is
   transitioned out of `pending` to exactly one of the four terminal states, based on what
   `append()` did: success → `accept` (recording the returned `entry_id`);
   `JournalBlockedError` → `firewall_drop`(`post_accept_journal_block`); any other exception →
   `reject`(`assembly_failed`). The atomic claim key is released (`DEL`) in the same step. The
   per-turn compact summary is updated from terminal states only.
8. **Output**: existing behavior preserved behind a flag/adapter. With the auditable path
   off, `extract_memories()` behaves byte-for-byte as today.

## Why Previous Fixes Failed

| Prior Fix | What It Did | Why It Was Incomplete |
|-----------|-------------|------------------------|
| PR #481 (first-class LLM extraction) | Moved extraction into an LLM prompt that returns a `facts` list | Rejection stayed inside the model's prompt — zero visibility, non-deterministic, and the malformed-reply paths still returned `[]` silently. No audit trail. |
| PR #587 (M2 firewall) | Added a deterministic gate that turns *blocked* content into logged tombstones | Correctly solved the privacy-drop audit gap but not the *rejection* audit gap: a candidate that is merely too short, malformed, or LLM-rejected still vanishes without a logged terminal state. |

**Root cause pattern:** every prior fix added a *gate* but not an *enumerated audit record*.
There was never an intermediate representation on which to hang "this candidate was
considered and decided, state = X, reason = Y."

## Architectural Impact

- **New dependencies**: none external. New internal modules under
  `src/popoto/extraction/` (`candidates.py`, `verdict.py`, `decision_log.py`) and a new model
  for decision records (or a keyed Redis structure — see Technical Approach). Reuses
  `popoto.privacy.never_record.scan_never_record` and `ProvenanceJournal.append`.
- **Interface changes**: `ExtractedFact` gains optional span/candidate fields (backward
  compatible — new fields have defaults). `SubconsciousMemory` gains an opt-in constructor
  flag/adapter for the auditable path; the default path's signature and return type are
  unchanged.
- **Coupling**: decreases. The all-in-one LLM prompt is split into deterministic enumeration
  (testable, no LLM) + a narrow verdict stage (no free text), so the write path no longer
  depends on the model's free-form prose.
- **Data ownership**: the decision log is owned by the extraction layer (like M2's tombstone
  log), distinct from the M1 journal keyspace. The journal owns accepted entries. One additional
  extraction-owned keyspace: the ephemeral assembly-claim keys `popoto:m3:claim:{agent_id}:
  {turn_id}:{candidate_id}`, which carry a TTL and hold no audit content — losing them costs at
  most a re-probe, never a record.
- **Reversibility**: high. A flag flips the default path back; the new modules are additive
  and do not alter existing keyspaces. `AppendOnlyMixin` on the journal means accepted
  entries are not retroactively modifiable (M1 contract).

## Appetite

**Size:** Large

**Team:** Solo dev, PM, code reviewer

**Interactions:**
- PM check-ins: 2 (scope alignment on candidate-set boundary, retention policy, and assembly
  content — all three settled; see Resolved Decisions)
- Review rounds: 2 (design review on the decision-log model and the enum-verdict contract;
  code review on the assembly path)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| M1 shipped (#589) | `python -c "import popoto.recipes.provenance_journal"` | Assembly target exists |
| M2 shipped (#587) | `python -c "import popoto.privacy.never_record"` | Per-candidate firewall primitive exists |
| Redis/Valkey on :6379 | `redis-cli ping` | Test suite requirement |

The two shipped dependencies are already on `main`; this plan only needs them importable.

## Solution

### Key Elements

- **Candidate** — a deterministic span of the input (sentence or lifted entity) carrying turn
  id, character offsets, and the generator rule that produced it. The atomic unit the rest of
  the pipeline decides on.
- **Candidate generator** — pure, deterministic, LLM-free enumeration. Given `(turn_id,
  text)`, yields the complete v1 candidate set. Empty turns are represented, not skipped.
- **Decision record** — the persisted per-candidate row: candidate identity, state, and an
  enum reason code. Its **terminal** state is always one of the four in #562's acceptance
  criteria — `firewall_drop | accept | reject | withhold`. It may transiently hold the
  non-terminal marker `pending` between the accept verdict and the journal write; a `pending`
  row is a *visible unfinished write*, which is the opposite of a silent drop.
- **Decision log** — the write-side audit store: a complete, **unbounded** per-candidate detail
  row set, plus a per-turn compact summary that serves as a cheap query index over it. Offline
  precision/recall are computable from the detail rows alone.
- **Verdict stage** — per-candidate LLM call returning only enum verdict + enum reason code;
  never free text.
- **Assembly adapter** — trusted code that maps `accept`ed candidates onto
  `ProvenanceJournal.append(...)`.

### Flow

```
response_text
  → Candidate generator  (deterministic; enumerates sentences + entities)
  → M2 firewall (scan_never_record per span) ──blocked──> DecisionLog[firewall_drop]  (TERMINAL, GUARDED write; not sent to LLM)
  → Verdict stage (LLM, one candidate at a time, enum verdict + enum reason)
        reject  → DecisionLog[reject]                                    (TERMINAL, GUARDED write)
        withhold→ DecisionLog[withhold]                                  (TERMINAL, GUARDED write)
        accept  → ATOMIC CLAIM: SET m3:claim:{agent}:{turn}:{cand} <token> NX PX <ttl>
                        claim lost  → no-op this runner; the winner owns the append (no second entry)
                        claim won   ↓
                  → DecisionLog[pending]            <-- written BEFORE any side effect
                  → dedup probe (has this candidate already been appended?)
                        JournalEntry.query.filter(turn_id=..., subjects__all=[f"cand:{cand}"])
                  → ProvenanceJournal.append(agent_id=..., kind=assert, verbatim=span, statement=span,
                                             subjects=[*topic_tags, f"cand:{cand}"])
                        ok                 → DecisionLog[pending → accept]        (TERMINAL, + entry_id, claim DEL)
                        JournalBlockedError→ DecisionLog[pending → firewall_drop] (TERMINAL, reason=post_accept_journal_block)
                        any other raise    → DecisionLog[pending → reject]        (TERMINAL, reason=assembly_failed)
  → Per-turn compact summary updated from TERMINAL states only
  → SubconsciousMemory.extract_memories() returns (behind the opt-in flag)
```

**GUARDED write** above means exactly one thing throughout this plan: the terminal write is
issued by the single conditional Lua script of Technical Approach → Terminal-write conflict
guard, which refuses to overwrite a row already terminal `accept` with an `entry_id`. Every
non-`accept` terminal write — including the pre-LLM `firewall_drop`, which cannot in practice
conflict because no prior row exists for a fresh candidate — goes through that same helper.
There is no "fast path" unconditional write anywhere in `decision_log.py`.

Read the diagram against Data Flow steps 5-7: they now describe the same ordering. The
decision log is written **first** on every path; for `accept` that first write is the
non-terminal `pending` row, and the terminal write follows assembly. There is no path on
which a candidate reaches `append()` with zero decision-log rows.

### Technical Approach

- **Module layout** (all under `src/popoto/extraction/`, following the provider conventions):
  - `candidates.py` — `Candidate` dataclass (`text`, `turn_id`, `candidate_id`, `start`,
    `end`, `generator_rule`) and the single function
    `generate_candidates(turn_id, text) -> List[Candidate]`. **No `CandidateGenerator`
    pluggable-rule class**: both plausible second rules are banned by this plan's own No-Gos
    (LLM-driven generation; multi-sentence/cross-turn windows), so a rule-registry abstraction
    would have exactly one implementation and no caller. Adding a rule later is a change to
    one pure function, not a plugin-point migration. Sentence splits reuse the heuristic's
    `_split_sentences` regex; entity lifting is deterministic (noun-phrase/entity regex, not
    an LLM — a named-entity lift stays deterministic so the candidate set is exhaustive and
    reproducible).
  - `verdict.py` — `Verdict`/`ReasonCode` enums, `llm_verdict(candidate) -> VerdictResult`,
    and the prompt/schema confining the model to `{candidate_id, verdict, reason_code}`.
  - `decision_log.py` — `DecisionRecord` model + `DecisionLog` writer/reader.
  - `__init__.py` — re-export the new surface; keep `AbstractExtractionProvider` /
    `ExtractedFact` intact and backward compatible.
- **ExtractFact extension**: add optional fields (`span_start`, `span_end`, `turn_id`,
  `candidate_id`, `generator_rule`) all defaulting to `None` so existing provider outputs and
  existing tests (`tests/test_extraction.py`, 33 tests — see Test Impact for the measurement
  environment) pass unmodified. The auditable path
  constructs facts from accepted candidates, carrying these fields through to the decision
  log.
- **`DecisionRecord` keying — composite `KeyField`, and NOT `AutoKeyField` (round-4 C3). Read
  this before writing the model.** The entire two-phase design rests on one property: the row for
  `(agent_id, turn_id, candidate_id)` must be retrievable and overwritable **in place** across two
  separate transactions. Therefore:

  > `agent_id`, `turn_id` and `candidate_id` are **all** `KeyField`s on `DecisionRecord`. The
  > composite key *is* the candidate's identity, so re-saving the same tuple **transitions the
  > existing row in place** rather than creating a second row.

  - **Do NOT copy `JournalEntry` here.** The only sibling model uses
    `entry_id = AutoKeyField()` (`src/popoto/recipes/provenance_journal.py:299`), which mints a
    **brand-new row on every save** — correct for an append-only journal, catastrophic here. A
    builder pattern-matching on that sibling would silently break *every* idempotency guarantee
    in this plan at once: the `pending` → terminal transition would leave two rows, the
    terminal-write conflict guard would read an empty row and never refuse, and `list_pending`
    would return already-reconciled rows forever. **`AutoKeyField` is forbidden on
    `DecisionRecord`.**
  - **Composite `KeyField`s are supported** — multiple `KeyField`s combine into the Redis key
    (`src/popoto/models/base.py:497-506`, `src/popoto/models/db_key.py:60`) and
    `_meta.key_field_names` is a set (`base.py:169,234-235`).
  - **Caveat that determines the actual key shape: KeyFields join ALPHABETICALLY, not in
    declaration order** (`base.py:284-301`, `get_db_key_index_position`: "KeyFields are sorted
    alphabetically in the Redis key"). So regardless of the order the fields are declared, the
    Redis key is `DecisionRecord:agent_id:candidate_id:turn_id` — `candidate_id` in the middle.
    Any code or test that parses the key by position, and anyone eyeballing `redis-cli KEYS`,
    must expect that order.
  - **Second caveat: `DB_key` escapes colons inside values** (`db_key.py:86-88`), so a
    `candidate_id` of `t-41:sent:0` renders escaped inside the key segment. That is correct
    behavior — do not "fix" it because it looks odd in `redis-cli`.
  - **Test that pins it:** write the `pending` row, then transition it to a terminal state, and
    assert **exactly one** Redis row exists for that composite key (and that its `state` is the
    terminal one). A model that mints a row per save fails this test immediately.

- **`detail_code` schema — one free-form diagnostic string, written only by trusted code
  (round-4 C5).** `detail_code` is asked to carry three structurally different payloads: a fixed
  literal (`terminal_conflict_refused`), a dynamic exception class name (on `assembly_failed`),
  and a list of journal `entry_id`s (on `ambiguous_reconciliation`). Those cannot all be enum
  values, so the "enum-safe" wording used in earlier revisions is **withdrawn**. The schema,
  stated once: `detail_code = StringField(default="")` — the same convention `verbatim` /
  `statement` use (`provenance_journal.py:304-305`) — **not** an enum type, and the
  ambiguous-reconciliation case serializes as `",".join(entry_ids)`. This does not weaken the
  LLM-writes-only-enums constraint: `state` and `reason_code` remain genuine single-value enums,
  and `detail_code` is written exclusively by trusted assembly code, never by the model.

- **Write ordering: the decision log is written before every irreversible side effect.**
  This is the module's central invariant, and getting it backwards would reintroduce the exact
  silent-drop class M3 exists to close. The rule, stated once and implemented once:

  > **A candidate never reaches a side effect that has no decision-log row already describing
  > it.**

  Concretely, per candidate:

  1. `firewall_drop` / `reject` / `withhold` produce **no** downstream side effect. One write,
     directly terminal — but that write is **conditional, not unconditional**: it is issued
     through the guarded Lua helper, which reads the same row and refuses to clobber an
     existing terminal `accept` carrying an `entry_id` (see → Terminal-write conflict guard).
     "One write" describes the *count*, not an absence of a conflict check.
  2. `accept` is the only two-phase path:
     - **Phase 1 — `pending`.** Write the row keyed `(agent_id, turn_id, candidate_id)` with
       `state='pending'`, the accept verdict, the reason code, the span offsets and a hash of
       `candidate.text`. This write is committed (not pipelined with the append) *before*
       `ProvenanceJournal.append()` is called.
     - **Phase 2 — terminal transition.** Call `append()`, then transition the **same row**
       (never a second row) to exactly one terminal state:

       | `append()` outcome | Terminal state | Reason code | Notes |
       |---|---|---|---|
       | returns `AnnotationResult` | `accept` | `accepted` | records the entry's `entry_id` on the row |
       | raises `JournalBlockedError` | `firewall_drop` | `post_accept_journal_block` | privacy refusal, see below |
       | raises `ValueError` / `TypeError` / `AppendOnlyViolation` / connection error | `reject` | `assembly_failed` | plus the exception class name in the free-form `detail_code` string field (see → `detail_code` schema) |

  **How this still satisfies Success Criterion 1 and #562's four-state AC.** The AC's
  vocabulary is a **terminal** vocabulary and it is unchanged: every candidate ends in exactly
  one of `firewall_drop | accept | reject | withhold`. `pending` is not a fifth terminal state
  — it is a non-terminal marker on the same row, overwritten in place by the terminal write.
  Success Criterion 1 is therefore restated as "exactly one **terminal** state per candidate,"
  with a companion criterion that no `pending` row survives a completed `extract_memories()`
  call. A `pending` row that *does* survive means the process died mid-assembly: that is a
  visible, queryable, recoverable incident with the candidate's full identity on it — which is
  precisely the outcome the blocker asked for, and the opposite of a candidate that vanished
  with zero rows.

- **Post-accept journal firewall block — mapped onto `firewall_drop`, deliberately.**
  `ProvenanceJournal` runs its *own* never-record scan at write time over values M3's
  per-candidate `scan_never_record(candidate.text)` never sees: `agent_id`, `subject_tags`, and
  `entry._never_record_scan_values()` (`provenance_journal.py:969`; `append()` documents
  "Nothing is issued or queued" on this raise, `:610-611`). So an LLM-accepted candidate can
  still be refused at assembly. It maps to **`firewall_drop`**, not to a new fifth state,
  because the *semantics are identical to the pre-LLM drop*: content was refused by the
  never-record firewall and nothing was stored. The four-state AC vocabulary stays intact.
  The two are distinguished by **reason code**, never by state:
  - `firewall_drop` + `pre_llm_candidate_block` — M3's per-candidate span scan; the LLM never
    saw the text.
  - `firewall_drop` + `post_accept_journal_block` — M1's write-time scan over agent/tag/entry
    values; the LLM did see the span and accepted it, and the journal refused the write.
  Every *other* assembly failure is `reject`(`assembly_failed`) — so `firewall_drop` continues
  to mean exactly one thing (privacy refusal) and never becomes a dumping ground for generic
  write errors. Offline analysis that wants "how often did privacy block us *after* the model
  had already said yes" reads one reason code.

- **Assembly idempotency (closes the Race 2 gap — `append()` has no idempotency key).**
  `ProvenanceJournal.append()` takes no per-call idempotency key and `JournalEntry.entry_id`
  is an `AutoKeyField`, so `AppendOnlyViolation` fires only when a record's Redis key already
  exists — which is never true for a fresh append. A naive retry after a crash between a
  successful `append()` and the terminal decision-log write therefore creates a **duplicate
  journal entry**, permanently, because M1 is append-only and offers no delete path. Assembly
  owns the dedup; it cannot be delegated to the journal:
  0. **Claim the candidate atomically first** (see → Atomic assembly claim below). A runner
     that does not win the claim performs no journal write at all, so steps 1-4 only ever
     execute for one runner per `(agent_id, turn_id, candidate_id)` at a time.
  1. Before calling `append()`, read the decision row for `(agent_id, turn_id, candidate_id)`.
     - Terminal `accept` with an `entry_id` → **already assembled; skip entirely.**
     - Terminal non-`accept` → already decided; skip.
     - `pending` → this is a retry of an interrupted assembly. Go to step 2.
     - Absent → fresh candidate. Write `pending`, then `append()`.
  2. **Reconcile the `pending` case before re-appending, by candidate identity.** Query the
     journal for this agent's entries on this `turn_id` carrying this candidate's identity tag:
     `JournalEntry.query.filter(turn_id=<turn_id>, subjects__all=[f"cand:{candidate_id}"])`
     (`turn_id` is an `IndexedField` and `subjects` is a `TagField` whose `__all` lookup is a
     `SINTER` over Redis Sets — both cheap indexed reads, both Valkey-safe, no new journal API
     needed). Exactly one match → the prior `append()` landed: transition the row to `accept`
     with that `entry_id` and **do not append again**. Zero matches → the prior append did not
     land: proceed with `append()`. More than one match → see step 4.
  3. **Why identity, not text.** An earlier revision matched on `turn_id` + exact `verbatim`
     equality and argued byte-identical storage made this sound. It is not: `JournalEntry`
     carries no candidate-identity field at all (`entry_id`/`agent_id`/`captured_at`/`turn_id`/
     `speaker`/`verbatim`/`statement`/`subjects`/`stated`/`kind`/`target`/`validity`,
     `provenance_journal.py:299-310`), and `verbatim` is *not* unique per candidate within a
     turn by construction — a repeated sentence, or a sentence span whose text equals an
     entity-lifted span, produces two candidates with identical `verbatim`. A `pending` row
     could then reconcile onto the *other* candidate's entry and record the wrong `entry_id`,
     breaking Success Criterion 4. That argument is **withdrawn**; the `cand:` subject tag
     replaces it. See → Candidate identity on the journal entry.
  4. **Ambiguity rule (defence in depth).** If the identity probe returns more than one match,
     assembly does **not** take the first: it writes the terminal state
     `reject`(`ambiguous_reconciliation`) with the matching `entry_id`s recorded in
     `detail_code`, and appends nothing. With correct tagging and per-turn-unique candidate ids
     (both guaranteed by Task 1) this is unreachable, so it is an assertion in row form — a
     loud, queryable state instead of a silent wrong-`entry_id` write.
  A test asserts that running the auditable path twice over the same `(agent_id, turn_id)`
  produces exactly one journal entry per accepted candidate, including when the first run is
  interrupted between `append()` and the terminal write, **and** including a turn containing two
  candidates with byte-identical text (the C2 case: each must reconcile onto its own entry).

- **Atomic assembly claim — closes the TOCTOU window in the dedup probe (round-2 C1).** The
  four-case probe above is read-then-act. Without a claim, two concurrent runners over the same
  `(agent_id, turn_id, candidate_id)` — a duplicated delivery racing a crash-retry, both seeing
  a surviving `pending` — can both probe, both find nothing (neither has committed `append()`
  yet), and both append. That produces **two permanent journal entries**, and the journal cannot
  catch it: `append()` takes no idempotency key and `AppendOnlyViolation` fires only when a
  record's Redis key already exists, which is never true for a fresh `AutoKeyField` append
  (`provenance_journal.py:562-620`). Race 1's `MULTI`/`EXEC` covers the record+summary write,
  not a cross-process claim on a candidate.

  **The claim is a single atomic Redis op, not a read followed by a write:**

  ```
  claim_key   = f"popoto:m3:claim:{agent_id}:{turn_id}:{candidate_id}"
  claim_token = uuid4().hex            # this runner's identity, for safe release
  won = <redis>.set(claim_key, claim_token, nx=True,
                    px=Defaults.M3_ASSEMBLY_CLAIM_TTL_MS)
  ```

  - `SET ... NX PX` is one round trip and a **core command** on both Redis and Valkey — no
    modules, per the standing constraint.
  - **Winner** (`won` truthy): writes the `pending` row, runs the identity probe, calls
    `append()`, writes the terminal row, then releases the claim in the same step as the
    terminal write. Release is token-checked — a tiny Lua `if GET == token then DEL` (Lua is
    core, Valkey-safe) — so a runner can never delete a claim it no longer owns.
  - **Loser** (`won` falsy): performs **no** journal write and no row transition. It no-ops and
    returns; the winner owns this candidate's terminal state. A loser never leaves the candidate
    row-less: either the winner writes `pending`/terminal, or the claim expires and a later run
    re-claims and reconciles.
  - **Chosen over `WATCH`/`MULTI` compare-and-set on the row's `state` field** (the critic's
    other option) deliberately: `WATCH` requires a dedicated connection held across the
    transaction plus a retry loop, which does not compose with Popoto's shared connection pool
    and adds a failure mode of its own. `SET NX` gives the same mutual exclusion in one op with
    no connection affinity. Both are Valkey-safe; `SET NX` is smaller.
  - **`Defaults.M3_ASSEMBLY_CLAIM_TTL_MS`** is a pinned in-repo magic number
    (`popoto/fields/constants.py`), not a constructor kwarg, per the repo's magic-number rule.
    It is a *liveness* bound, not a correctness bound: long enough that the common case never
    expires mid-flight, finite so a crashed runner's claim cannot wedge the candidate forever.
    Start at `30_000` ms.
  - **Residual window, stated honestly:** if the TTL expires while the winner is still between
    the probe and `append()`, a second runner can claim and probe. That case is now *sound
    rather than duplicating*, because the probe is an identity lookup on the `cand:` tag: if the
    first `append()` landed, the second runner finds exactly that entry and reconciles; if it
    did not, appending is correct. The claim removes the common-case race; the identity tag
    makes the residual race converge instead of duplicating. Neither mechanism alone suffices,
    which is why both ship.
  - Test: two runners racing the same `(agent_id, turn_id, candidate_id)` produce exactly one
    journal entry and exactly one terminal row, and the loser appended nothing.

- **Concurrency posture — measured, and settled (round-3 C1 + C2).** Both round-3 concerns
  turn on one question — *does a concurrent or at-least-once caller of `extract_memories()`
  exist today?* — so it is answered once, with a measurement, and both are resolved against
  that single answer.

  **Measurement (supervisor-verified at HEAD `d9d9127`; main checkout
  `/Users/valorengels/src/popoto`, venv resolving `popoto` to `src/popoto/__init__.py`,
  redis-py 7.1.1, Redis on `localhost:6379`):** `grep -rn "extract_memories(" src/popoto/`
  yields exactly **one** real call site — `src/popoto/integrations/service.py:230`,
  **synchronous**, once per turn (every other hit is a docstring or a comment).
  `grep -rln "multithreading" src/popoto/` yields **no importers**:
  `src/popoto/utils/multithreading.py` is imported nowhere in `src/popoto/`. **So no
  concurrent and no at-least-once caller exists today.** Re-measure before quoting this; do
  not relay it without re-running in the environment you are in.

  **Round-3 Concern 2 — RESOLVED: the atomic claim is KEPT as specified. Do not descope it.**
  The claim protocol (`SET ... NX PX` + uuid4 token + token-checked Lua release +
  `Defaults.M3_ASSEMBLY_CLAIM_TTL_MS` + Race 4 + its two Verification rows) stays exactly as
  written above. Rationale, recorded so it is not re-argued at build time: it is already fully
  specified, it is Valkey-safe (core commands plus Lua, no modules), and it costs **one `SET NX`
  per accepted candidate** — a single round trip on the opt-in path only. It is cheap insurance
  for the at-least-once retry that **M9 (#568)'s audit harness may introduce**, and descoping it
  now would only mean rebuilding it there. This is a **deliberate acceptance of mild
  over-engineering against a named, expected future caller** — not speculative generality. The
  builder implements the claim; it is not optional and it is not a stretch goal.

- **Terminal-write conflict guard — non-`accept` terminal writes must not clobber an assembled
  row (round-3 C1, ACCEPTED as a build-time defect to fix).** This is the real residue of round
  3 and it is a genuine defect in the design as written, not a hypothetical. Only the `accept`
  path is claim-protected: `firewall_drop`, `reject` and `withhold` write terminally with **no
  claim, no CAS, and no read-before-write**. Race 2's mitigation ("a re-write of the same key
  with the same terminal state is a no-op") silently assumes a retried verdict call returns the
  *same* verdict — but the LLM verdict is **non-deterministic**, and a retried call is Race 2's
  own trigger. So a retry that resolves `reject` can overwrite the row of a candidate whose
  `accept` path already appended to the journal, permanently splitting the decision log (the
  source of truth for `compute_metrics`) from the journal (the source of truth for stored
  memories) — exactly the discrepancy `compute_metrics` is trusted never to have.

  **The guard (a guarded write plus a test — deliberately NOT a redesign):**

  > A terminal write must not overwrite a row that is **already terminal `accept` carrying an
  > `entry_id`**. Before writing any of `firewall_drop` / `reject` / `withhold`, the writer
  > reads the existing `(agent_id, turn_id, candidate_id)` row; if that row is already
  > `state == 'accept'` **and** carries a non-empty `entry_id`, the write is **refused** — the
  > existing `accept` row stands unchanged, and the refusal is recorded on that same row in the
  > free-form `detail_code` string field as `terminal_conflict_refused`. No second row, no new state,
  > no exception to the caller.

  **Implementation shape: ONE conditional Lua script, run via `EVAL` (round-4 C1).** The two
  mechanisms named in the round-3 revision — "inside the existing `MULTI`/`EXEC`" and "route the
  terminal write through the `SET NX` claim key" — are **both deleted, not offered as options**,
  because neither can implement the rule above:
  - `MULTI`/`EXEC` queues commands blind. Nothing inside the transaction can read `state` and
    branch on it; that needs `WATCH` (already rejected in → Atomic assembly claim, for the
    dedicated-connection and retry-loop reasons) or Lua.
  - The `SET NX` claim key only buys mutual exclusion on a *key*; it never inspects `state`. And
    non-`accept` verdicts take **no claim at all** (the claim is on the `accept` path only, per
    Task 3 and the Flow diagram), so a retried `reject` would trivially win `SET NX` on an
    unclaimed key and write unguarded — the exact round-3 defect, reintroduced.

  The replacement is a single read-and-conditionally-write Lua script, the direct companion to
  the token-checked release script already specified in → Atomic assembly claim. It is invoked
  for **every** terminal write of `firewall_drop` / `reject` / `withhold`, unconditionally — it
  is never gated on whether a claim key happens to exist:

  ```lua
  -- KEYS[1] = the DecisionRecord hash for (agent_id, turn_id, candidate_id)
  -- ARGV    = the terminal state, reason_code, written_at, and the other row fields
  if redis.call('HGET', KEYS[1], 'state') == 'accept'
     and (redis.call('HGET', KEYS[1], 'entry_id') or '') ~= '' then
      redis.call('HSET', KEYS[1], 'detail_code', 'terminal_conflict_refused')
      return 0                      -- refused; the accept row stands unchanged
  else
      redis.call('HSET', KEYS[1], <terminal state, reason_code, written_at, ...>)
      return 1                      -- written
  end
  ```

  Notes that bound the scope:
  - **The assembled row always wins**, because a journal entry physically exists for it: keeping
    `accept` is the only choice that leaves the log agreeing with the journal.
  - **Valkey-safe.** `EVAL` plus `HGET`/`HSET` are core commands with no modules, and `EVAL` is
    an established in-repo pattern (`src/popoto/models/query.py:445,463`;
    `src/popoto/fields/existence_filter.py:430`), so this adds no new primitive.
  - **Atomicity comes from the script, not from a surrounding transaction.** Redis/Valkey execute
    a Lua script atomically, so the read and the conditional write cannot interleave. The Race 1
    `MULTI`/`EXEC` still wraps the **record + per-turn-summary** pair; the guarded terminal write
    is the record half of that pair and must be issued as this script rather than as a queued
    blind `HSET`.
  - **This does not add a fifth terminal state and does not touch the AC vocabulary.** The
    conflict is recorded in `detail_code` on an existing row.
  - **Do not expand this into a full terminal-state CAS redesign.** Non-conflicting cases still
    result in exactly one write; the script simply makes that write conditional.

- **Candidate identity on the journal entry — the `cand:` subject tag (round-2 C2).** Assembly
  passes `subjects=[*topic_tags, f"cand:{candidate_id}"]` to `append()`, so every M3-written
  entry carries the identity of the candidate that produced it. This is chosen over the critic's
  alternative (detect >1 match and record `ambiguous_reconciliation`) because that alternative
  only *detects* the identity failure after the fact and leaves the candidate stuck, whereas the
  tag makes reconciliation correct by construction. The `ambiguous_reconciliation` outcome is
  kept anyway, demoted to the unreachable-assertion role in step 4 above. Constraint checks,
  each verified against the code rather than assumed:
  - **Settled decision 3 is untouched.** `statement` and `verbatim` stay byte-identical to the
    span; the tag lives in `subjects`, a different field. Accepted *content* does not change.
  - **M1 append-only semantics are untouched.** The tag is supplied at `append()` time as part
    of the initial write. No entry is ever mutated and no new journal API is added.
  - **M2 firewall scan surface — checked, and it constrains the id format.**
    `_scan_or_block(agent_id, *subject_tags, *entry._never_record_scan_values())`
    (`provenance_journal.py:969`) scans **every subject tag**, so a badly-shaped `candidate_id`
    would make the journal refuse M3's own writes. Measured at `f6e8525` with
    `scan_never_record()`: `"cand:t-41:sent:0"` → clean, `"cand:t-41:ent:12"` → clean,
    `"cand-0007"` → clean, but a 64-char hex digest → **blocked, `reason='high_entropy'`**
    (git-SHA shapes are entropy-exempt only up to 40 chars, `never_record.py:244`). Therefore
    `candidate_id` **must not be a hash or digest**: it is a structured, low-entropy,
    deterministic token `{turn_id}:{generator_rule}:{ordinal}` (Task 1), and a test asserts
    `scan_never_record(f"cand:{candidate_id}").blocked is False` for every generated candidate.
    A content hash still lives on the *decision row*, where nothing scans it — only the tag
    shape is constrained.
  - **Tag semantics.** `subjects` is "convention over schema and explicitly **not** a security
    boundary" (`provenance_journal.py:70-73`); a prefixed `cand:` tag is exactly the documented
    convention (`agent:`, `project:`). One consequence to state: M3-written entries are never in
    `TagField`'s untagged pool, since they always carry at least the `cand:` tag. Readers that
    select the untagged pool will not see M3 entries; readers filtering by topic tags are
    unaffected.

- **Stale `pending` recovery is manual in v1 — position taken (round-2 N1).** Nothing sweeps,
  expires, or alerts on a `pending` row: a turn that crashes mid-assembly and is never replayed
  stays `pending` indefinitely. That is **deliberate for v1** and is the right trade at this
  stage — a `pending` row is a *visible, queryable, fully-identified* unfinished write (the
  opposite of the silent drop M3 exists to close), and the correct sweep cadence is exactly the
  kind of number this plan refuses to guess before M9 (#568) consumes the log, in line with the
  same reasoning used for retention. Concretely:
  - **A TTL on decision rows is explicitly rejected** — it would delete audit evidence, which is
    the one thing this module exists to keep.
  - **The operator query ships as documentation, not as machinery.** The `decision_log.py`
    docstring and the docs page carry the recovery recipe: list rows with
    `state == 'pending'` for an agent, oldest-first by the row's write timestamp
    (`DecisionLog.list_pending(agent_id, older_than=None) -> List[DecisionRecord]`, a thin
    reader over the existing rows — no new keyspace), then re-invoke the auditable path for each
    stale `(agent_id, turn_id)`, which reconciles it through the probe above.
  - **`list_pending` is the only code this NIT adds.** A periodic age-keyed sweep, an alert
    threshold, and any dashboard over it are named as M9/ops-runbook follow-ons, not v1.

- **Auditing the measured-worse candidate shape, on purpose.** PR #510 measured heuristic
  sentence-splitting at **0.2078** judged accuracy against raw turn ingestion's **0.3636**
  (`src/popoto/extraction/__init__.py:159-166`), and v1's candidate set is sentence spans plus
  pattern-lifted entities — the arm that lost. That is a real tension and the plan does not
  pretend otherwise. Why it is still the right v1 investment:
  1. **The 0.2078 was measured blind.** It is a single aggregate number over a splitter that
     shipped with *no per-candidate record of what was considered and dropped*. It establishes
     that sentence-splitting **as shipped** underperforms; it cannot say *which* spans, *which*
     generator rule, or *which* rejection reason drove the loss, because that data was never
     recorded. M3 is the instrument that produces it.
  2. **A raw-turn audit trail would have almost nothing to audit.** One candidate per turn
     yields one accept/reject row per turn — an audit log that records the same aggregate #510
     already has, at per-candidate granularity of exactly one. The decision log only pays for
     itself when there is more than one candidate to discriminate between; sentence spans are
     the smallest shape that makes "which part of this turn was worth keeping" a question the
     log can answer.
  3. **M3 does not promote the worse shape into production.** The harness default stays
     `RawTurnExtractionProvider`; the auditable path is default-off and this plan explicitly
     No-Gos switching the harness (see No-Gos). Nothing that users get today moves onto the
     0.2078 path because of this module.
  4. **It makes the shape choice correctable with evidence instead of another blind bake-off.**
     Adding a raw-turn candidate rule, or bounded windows behind M4, is a change to one pure
     function (`generate_candidates`) plus a `generator_rule` tag on each row. Once the log
     exists, comparing shapes is a query over rows grouped by `generator_rule` — not a new
     experiment harness. Choosing the shape *first* and the instrument *second* is how #510's
     number ended up unactionable.
  The supervisor-settled v1 candidate set is unchanged by this argument (sentence spans +
  pattern-lifted entities; windows behind M4). What changes is that the plan now carries the
  #510 number and the justification in the open, rather than citing #510 for the opposite
  conclusion.

- **Decision-log retention (resolves open question b — no cap in v1):**
  - **Per-candidate detail rows ship UNBOUNDED.** Keyed `(agent_id, turn_id, candidate_id)`
    with the terminal state and reason code, written in the same pass and **never trimmed**.
    v1 introduces **no `Defaults` cap constant** for this log and performs **no LTRIM**.
  - **Why no cap:** the correct retention horizon cannot be known until M9 (the seeded audit
    harness, #568) actually consumes this log. v1 deliberately declines to guess a number.
    Shipping the full corpus means M9 reasons over complete data, and any later trimming policy
    is designed against **measured** growth rather than a guessed constant.
  - **The tradeoff this accepts, stated plainly:** an unbounded key set in a *central shared
    Redis* at the project's 20k-memory scale. Candidates are per-sentence-plus-entity, so the
    row count grows roughly `O(turns x candidates_per_turn)` — materially faster than the
    journal itself. This is a knowingly accepted, temporary cost of getting M9 a real corpus.
  - **Explicit trigger for revisiting:** growth must be *measured*, not assumed. Report
    decision-log key count and total memory footprint (`redis-cli --bigkeys` / `MEMORY USAGE`
    over the decision-log keyspace) at M9 planning time, and again whenever a benchmark run
    exercises the auditable path at scale. **Signals that force the revisit before M9:** (a)
    the decision-log keyspace exceeds the M1 journal's footprint by more than ~10x on any real
    agent, (b) measured growth projects past a few hundred MB at the 20k-memory target, or
    (c) decision-log writes show up as a latency cost on the write path. Any one of these
    re-opens retention as a design item with real numbers in hand — which is the entire point
    of not guessing now.
  - **Per-turn compact summary — kept, on convenience/query-cost grounds only.** With full
    per-candidate detail retained, the summary is **a convenience index, not a completeness
    fallback**; the earlier justification (that it carries completeness forward after detail
    rows age out) no longer applies and is withdrawn — nothing ages out. It is kept for one
    reason: computing per-turn terminal-state counts otherwise requires scanning every
    candidate row for that turn, and both the offline precision/recall computation and the
    growth measurement above want a cheap per-turn rollup. One small hash per
    `(agent_id, turn_id)` holding terminal-state counts and reason-code distribution makes
    those O(1) per turn at O(turns) storage. **Correctness never depends on it**: the detail
    rows remain the sole source of truth, and any summary/detail disagreement is resolved in
    favor of the detail rows.
  - **Valkey-safe**: a `HashField`/`SortedSetField`-backed structure, or a plain model with
    the standard indexes — no Redis modules.
- **Offline precision/recall — what "computable offline" concretely means here, and how v1
  demonstrates it for real.** The critique is fair that a `grep` proving the flag exists is not
  a demonstration, and that with harness wiring No-Go'd nothing outside tests drives the
  pipeline in v1. Two honest halves:
  - **What ships is a capability, and the plan says so.** Success Criterion 3 is reworded from
    a claim about realized measurement to a claim about a working, exercised computation. The
    realization step (opting real traffic in) is the named follow-on already recorded in
    No-Gos; v1 does not pretend to have taken it.
  - **The demonstration is made concrete and non-trivial.** `decision_log.py` exposes
    `DecisionLog.compute_metrics(agent_id, gold_labels) -> Metrics` (precision, recall, F1,
    plus per-`reason_code` and per-`generator_rule` breakdowns). **Named consumer for each
    breakdown dimension (round-2 N2) — neither is decoration:** per-`generator_rule` is what the
    #510 argument's point 4 rests on (comparing candidate shapes becomes a query over rows
    grouped by rule instead of a new bake-off). Per-`reason_code` has exactly one consumer named
    here: **separating privacy drops from model rejects in the same metric run** — a
    `firewall_drop`(`pre_llm_candidate_block` / `post_accept_journal_block`) is not an extraction
    quality failure and must not be charged against the LLM's precision, whereas a
    `reject`(`llm_unavailable`) or `reject`(`assembly_failed`) is an *infrastructure* loss that
    must not be charged against recall either. Without the breakdown, a run whose recall dropped
    cannot distinguish "the model got worse" from "the firewall got stricter" from "Redis was
    flaky" — which is the M9 (#568) audit's first question. The backing test asserts the
    breakdown separates exactly those three cases. It reads **only** decision-log
    rows and a caller-supplied gold-label mapping `{candidate_id: should_accept}` — no journal
    read, no LLM, no live Redis state beyond the log itself. The test that proves this
    (`test_precision_recall_computable_from_log_alone`) does not hand-assemble a few rows: it
    runs the full auditable path over a **multi-turn fixture transcript** with a stubbed
    verdict provider whose accept/reject pattern is fixed, hand-labels the expected candidate
    set, and asserts **exact numeric** precision/recall/F1 values plus the per-rule breakdown.
    A second assertion pins the isolation property — the computation is re-run against a
    process with the journal keyspace flushed and produces identical numbers, proving the log
    alone suffices. A dedicated Verification row runs this test by name.
  - The docs page (see Documentation) carries a runnable snippet that enables
    `auditable_extraction`, runs a turn, and prints the metrics, so an adopter can realize the
    benefit without waiting for the harness wiring decision.

- **Enum verdicts only** — the LLM writes only `{candidate_id, verdict, reason_code}`. The
  assembly stage never reads free text from the model; accepted content is the verbatim span.
  `withhold` is a terminal logged state and never triggers an automatic user interaction.
- **Assembly content (resolves open question c): `statement` stays BYTE-IDENTICAL to the
  verbatim span.** No light deterministic normalization in v1 — not whitespace collapsing, not
  punctuation stripping, not casing changes. This is not a judgment call: issue #562's own
  acceptance criteria require that "accepted memory content is byte-identical to a verbatim
  candidate span," so any normalization would violate the AC. Distillation and normalization
  are M4's job. Concretely, assembly calls
  `ProvenanceJournal.append(agent_id=<agent_id>, kind='assert', verbatim=candidate.text,
  statement=candidate.text, speaker=..., turn_id=...,
  subjects=[*topic_tags, f"cand:{candidate.candidate_id}"])` — the same string object for both
  `verbatim` and `statement` — and a test asserts
  `entry.statement == entry.verbatim == candidate.text`. The `cand:` tag carries candidate
  identity (see → Candidate identity on the journal entry) and does **not** touch content, so
  settled decision 3 is preserved exactly.
  `agent_id` is keyword-only and required (`provenance_journal.py:562-577`); it is sourced from
  the `SubconsciousMemory` instance's agent id and is never allowed to be `None`, because a
  `None` renders the literal `"None"` into the record's Redis key.
- **Behavior preservation (acceptance criterion 4):** `SubconsciousMemory` gains an opt-in
  `auditable_extraction: Optional[AuditableExtractionConfig] = None` constructor arg. When
  `None` (default), `extract_memories()` runs the exact current path — provider →
  save-loop, including the M2 turn-level scan — with no change. When set, the candidate /
  verdict / decision-log / assembly path runs instead and returns the accepted set. The
  harness (`service.py`) does **not** switch by default in this plan; switching is a follow-on
  wiring decision.
  **`AuditableExtractionConfig`, defined here rather than left dangling (round-4 N1):** a small
  frozen dataclass in `src/popoto/extraction/decision_log.py`, exported from
  `popoto.extraction`, with exactly two fields in v1 —
  `verdict_provider` (the callable/object implementing `llm_verdict(candidate)`, so tests can
  inject a stubbed verdict provider) and `journal` (the `ProvenanceJournal` instance assembly
  appends to). No numeric knobs: `Defaults.M3_ASSEMBLY_CLAIM_TTL_MS` stays a pinned in-repo
  constant, not a config field, per the magic-number rule. Passing `None` (the default) is the
  only thing the default path ever sees, and its behavior is byte-for-byte unchanged.
- **Firewall interaction:** the existing M2 turn-level `scan_never_record(response_text)`
  voiding (`subconscious_memory.py:388-397`) stays first. Then, per candidate, M3 calls
  `scan_never_record(candidate.text)`; a `blocked` verdict logs `firewall_drop` with the
  reason and skips the LLM. This does not regress M2 semantics — it adds a finer-grained,
  logged drop on top of the coarser turn-level gate.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `src/popoto/extraction/claude.py:157-179` whole-call failures return `[]` and
  `logger.warning` — under the auditable path this must become a logged `reject`
  (LLM_unavailable) decision, not a silent `[]`. Add a test asserting the decision log records
  a `reject` with reason `llm_unavailable` when the client raises.
- [ ] `src/popoto/recipes/subconscious_memory.py:431-432` save-loop `except Exception` — in
  the auditable path, a journal-write exception must surface as a logged terminal state (or a
  loud raise), never a silent `logger.warning`. Add tests for the assembly-write failure path
  covering **both** branches of the Phase-2 transition table: `JournalBlockedError` →
  `firewall_drop`(`post_accept_journal_block`), and `ValueError`/`AppendOnlyViolation`/
  connection error → `reject`(`assembly_failed`). Both must assert the row was `pending`
  *before* `append()` was invoked (so the failure could never have produced a zero-row
  candidate) and terminal *after*.
- [ ] Interrupted assembly — simulate a process death between a successful `append()` and the
  terminal decision-log write. Assert (a) the `pending` row survives with full candidate
  identity, and (b) a retry reconciles it to `accept` via the `cand:`-tag identity probe
  **without** creating a second journal entry. Also assert the stale claim key has expired or is
  re-claimable, so a crashed runner cannot wedge the candidate.
- [ ] Concurrent assembly (Race 4) — two runners over the same
  `(agent_id, turn_id, candidate_id)`, both reaching the claim. Assert exactly one wins, exactly
  one journal entry exists, exactly one terminal row exists, and the loser issued no `append()`.
- [ ] **Conflicting terminal verdicts (round-3 C1)** — a candidate resolves `accept`, assembly
  appends and writes the terminal `accept` row with an `entry_id`; a retried verdict call then
  resolves `reject` for the same `(agent_id, turn_id, candidate_id)`. Assert the `reject` write
  is **refused**: exactly one terminal state survives, it is `accept`, its `entry_id` still
  resolves to the journal entry, and `detail_code == 'terminal_conflict_refused'`. The decision
  log must never disagree with the journal about whether an entry exists.
- [ ] Ambiguous reconciliation — force the >1-match branch (e.g. by writing two entries with the
  same `cand:` tag out of band) and assert the row lands terminal
  `reject`(`ambiguous_reconciliation`) with the matching `entry_id`s in `detail_code` and that
  **no** further `append()` is issued.
- [ ] State: no `except Exception: pass` exists in the new modules; each handler logs or
  records an observable decision.

### Empty/Invalid Input Handling
- [ ] Empty or whitespace `response_text` — assert a single logged `reject`(empty_turn)
  decision, and `extract_memories` returns `[]` (matching current behavior).
- [ ] A turn whose candidate set is empty (e.g. all sentences below min length) — assert the
  decision log still records the empty-candidate rejection rather than writing nothing.
- [ ] Verify an empty LLM verdict reply does not loop silently — the verdict stage treats a
  malformed/empty reply as `reject`(llm_unavailable), logs it, and terminates.

### Error State Rendering
- [ ] No user-visible UI is involved (library + docs); error propagation is the logged
  terminal states above. Assert the decision-log reader can render every recorded state.

## Test Impact

**Baseline measurement environment (per repo doctrine — state the environment alongside any
count):** counts below were re-measured at plan-revision time via
`python -m pytest <file> --collect-only -q` in the **main checkout**
`/Users/valorengels/src/popoto` (not a worktree), at commit `f1eb5e0`, venv `.venv` resolving
`popoto` to `/Users/valorengels/src/popoto/src/popoto/__init__.py`, **redis-py 7.1.1**,
Redis/Valkey on `localhost:6379`. The critique's numbers (33 / 57 / 101 at `76d649a`) reproduce
exactly; the plan's original 33 → *26*, 57 → *31*, 101 → *93* figures were stale and are
corrected here. Re-measure before quoting these; do not trust either the old or the new number
without re-running in the environment you are in.

- [ ] `tests/test_extraction.py` (**33** tests) — NO CHANGE required: `ExtractedFact` new fields
  default to `None`. Re-run to confirm green.
- [ ] `tests/test_subconscious_memory.py` (**15**),
  `tests/test_subconscious_memory_integration.py` (**27**) — NO CHANGE: default path behavior
  is preserved byte-for-byte. Re-run to confirm.
- [ ] `tests/test_never_record_firewall.py` (**57** tests) — NO CHANGE: turn-level firewall
  semantics untouched. Re-run to confirm.
- [ ] `tests/test_provenance_journal.py` (**101** tests) — NO CHANGE: journal API untouched; M3
  only calls `append(kind='assert')` and reads the `turn_id` index plus the `subjects` tag
  index (`subjects__all=["cand:..."]`) for the assembly dedup
  probe. Re-run to confirm.
- New: `tests/test_auditable_extraction.py` — candidate enumeration determinism, per-candidate
  firewall drops, enum-verdict confinement, decision-log completeness (including the
  `pending` → terminal transition and the no-surviving-`pending` assertion), post-accept
  `JournalBlockedError` → `firewall_drop`(`post_accept_journal_block`), assembly-failure →
  `reject`(`assembly_failed`), assembly retry idempotency (one journal entry per accepted
  candidate across a re-run and across an interrupted run), **concurrent-claim exclusivity
  (Race 4) with the loser appending nothing**, **duplicate-text reconciliation via the
  `cand:` identity tag**, **candidate ids passing `scan_never_record` unblocked**,
  **terminal-write conflict refusal (a retried `reject` must not clobber an assembled `accept`
  row)**, **in-place composite-key transition (write `pending`, transition to terminal, assert
  exactly ONE Redis row exists for that `(agent_id, turn_id, candidate_id)` — round-4 C3)**,
  **`written_at` stamped on both writes with `list_pending` ordering oldest-first**,
  `list_pending` stale-row recovery, offline precision/recall computation from the log alone
  (including the per-`reason_code` separation of privacy vs LLM vs infrastructure losses),
  default-path preservation.

## Rabbit Holes

- **Multi-sentence / cross-turn windows** — resolving open question (a) *against* them for v1.
  Tempting because recon flagged cross-sentence facts as a recall gap, but they multiply LLM
  verdict calls per turn, interact with the still-unbuilt M4 reference-resolution stage
  (cross-sentence anaphora), and — most importantly — the v1 decision log already makes recall
  *measurable*, so a window-expansion pass can be data-driven later. This is a genuine
  deferral with a strong reason, not laziness.
- **Named-entity recognition via an LLM** — would make the candidate set non-deterministic and
  defeat the "exhaustive enumeration" goal. Entity lifting must stay a deterministic
  (regex/heuristic) pass; anything semantic belongs in the verdict stage.
- **Building a full reconciliation/consolidation engine on top of the decision log** — out of
  scope; M3 only *produces* the log and computes precision/recall from it.
- **Designing a trimming/retention policy now** — v1 ships the detail log unbounded on purpose
  (see Technical Approach). Inventing a cap constant here would be a guess that destroys the
  corpus M9 (#568) needs to pick a real number. Retroactive trimming UI and operational
  dashboards over the log likewise belong to M9, not this module.

## Risks

### Risk 1: LLM verdict-call cost per turn
**Impact:** Sentence + entity candidates could make the verdict stage issue many calls per
turn (roughly O(sentences + entities)), increasing latency and token cost on the opt-in path.
**Mitigation:** The path is behind a flag, so non-auditable users are unaffected. The verdict
stage is per-candidate but stateless and can pipeline; v1 optimizes for correctness and the
decision log exposes the true candidate volume so the follow-on window module can size from
data.
**Explicitly NOT mitigated by a candidate cap.** An earlier draft of this risk promised
"`Defaults` gains a tunable per-turn candidate cap." That sentence is **deleted**, not
deferred: no task implemented it, no Verification row checked it, and — decisively — a cap
directly contradicts Success Criterion 1 ("every generated candidate appears in the decision
log with exactly one terminal state") and the exhaustive-enumeration goal the whole module
rests on. A capped-out candidate would be a candidate that was generated and then silently
dropped, i.e. the exact defect M3 exists to eliminate, reintroduced as a performance
optimization. The mitigation rests on default-off alone. A Verification anti-criterion pins
the absence of a cap so it cannot creep back in during build.

### Risk 2: Backward-compat regression on the default path
**Impact:** If the flag plumbing leaks into the default path, existing
`extract_memories()` behavior changes silently for current users — a hard acceptance-criteria
violation.
**Mitigation:** The flag defaults to `None` and the default branch is the unmodified existing
code path (extractor → save loop). The existing 33 extraction + 15/27 subconscious-memory tests
(counts and environment recorded in Test Impact) assert byte-for-byte
behavior; the plan keeps them green and adds a dedicated default-path-preservation test.

### Risk 3: Firewall regression
**Impact:** M3's per-candidate scan could bypass or double-apply the M2 guarantee, or a
candidate's span could carry content the turn-level scan missed.
**Mitigation:** Turn-level M2 voiding stays first and unchanged; per-candidate `scan_never_record`
is an *additional* gate that only ever adds `firewall_drop` decisions. A test asserts a
secret confined to one sentence drops only that candidate and does not leak to the LLM or the
journal.

### Risk 4: Unbounded decision-log growth on a busy agent [ACCEPTED, NOT MITIGATED]
**Impact:** v1 ships the per-candidate detail log with **no cap and no trimming**, in a central
shared Redis. On a busy agent the keyspace grows `O(turns x candidates_per_turn)` — faster than
the M1 journal — and at the project's 20k-memory scale this is a real, ongoing storage cost.
**Disposition:** This risk is **knowingly accepted**, not mitigated. The alternative (a guessed
cap constant) would destroy exactly the corpus M9 (#568) needs to decide the real horizon.
**What stands in for mitigation:** measurement plus named revisit triggers — decision-log key
count and `MEMORY USAGE` reported at M9 planning time and after any at-scale benchmark run,
with revisit forced if the decision-log footprint exceeds ~10x the journal's, projects past a
few hundred MB at the 20k target, or shows up as write-path latency (see Technical Approach →
Decision-log retention). The path is also default-off, so only opted-in deployments pay it.
A trimming policy, when designed, is additive: it changes no schema and no offline computation.

## Race Conditions

### Race 1: Concurrent verdict + decision-log write
**Location:** `decision_log.py` write; `verdict.py` calls per candidate.
**Trigger:** Two turns (or two candidates) for the same `agent_id` landing in the same
process pipeline, or a concurrent read of the per-turn summary while a candidate write is
mid-flight.
**Data prerequisite:** The per-turn summary must aggregate all candidates for that turn.
**State prerequisite:** The summary reflects every candidate written for the turn.
**Mitigation:** Write the per-candidate record and update the per-turn summary as one
`MULTI`/`EXEC` per candidate (or per turn), so no interleaving reader sees a partial turn's
summary. The per-turn summary is keyed by `(agent_id, turn_id)` so turns are independent.
This mirrors the M1 journal's atomicity stance (documented, not an impossibility claim).

### Race 2: LLM verdict stage idempotency
**Location:** `verdict.py`.
**Trigger:** A retry of a candidate whose verdict call timed out but whose reply actually
landed.
**State prerequisite:** A candidate is decided exactly once.
**Mitigation:** The decision record is written once keyed by `(agent_id, turn_id,
candidate_id)`; the writer is idempotent — a re-write of the same key with the same terminal
state is a no-op, and the per-turn summary is updated by the terminal state only. No
automatic user interaction on `withhold`, so no external side-effect is double-fired.
**Correction (round-3 C1) — the same-verdict assumption is withdrawn.** "Same terminal state"
is *not* guaranteed on a retry: the LLM verdict is non-deterministic, so a retried call can
return `reject` where the first returned `accept`. Idempotency therefore rests on the
**terminal-write conflict guard** (Technical Approach → Terminal-write conflict guard), not on
verdict stability: a non-`accept` terminal write is refused against a row already terminal
`accept` with an `entry_id`, so the decision log can never disagree with the journal.
**Scope limit, stated explicitly:** this covers the *decision-log* write only. The verdict call
itself is side-effect-free (the model writes nothing to the store), so a duplicated verdict
costs tokens, not correctness. The *assembly* write is a different problem and is handled by
Race 3 — do not read Race 2 as covering it.

### Race 3: Assembly retry duplicates a journal entry
**Location:** the assembly step in `decision_log.py` / the `SubconsciousMemory` auditable path.
**Trigger:** The process dies (or the connection drops) after `ProvenanceJournal.append()`
commits but before the `pending → accept` terminal write lands. The pipeline is re-run for the
same `(agent_id, turn_id)`.
**Data prerequisite:** Whether a journal entry already exists for this candidate must be
knowable *before* a second `append()` is issued.
**State prerequisite:** Exactly one journal entry per accepted candidate, ever.
**Why the journal cannot solve this itself:** `append()` accepts no idempotency key
(`provenance_journal.py:562-577`) and `JournalEntry.entry_id` is an `AutoKeyField`, so
`AppendOnlyViolation` — which fires only when the record's Redis key already exists — is never
triggered by a fresh append. M1 is append-only with no delete path, so a duplicate is
permanent.
**Mitigation:** Assembly claims the candidate atomically, then does its own dedup, keyed by
`(agent_id, turn_id, candidate_id)`, before every `append()`:
- **Atomic claim first** — `SET popoto:m3:claim:{agent_id}:{turn_id}:{candidate_id} <token> NX
  PX Defaults.M3_ASSEMBLY_CLAIM_TTL_MS`. Lost claim → no-op, no journal write.
- Terminal `accept` row present → skip (already assembled).
- Terminal non-`accept` row present → skip (already decided).
- `pending` row present → probe the journal by **candidate identity**:
  `JournalEntry.query.filter(turn_id=..., subjects__all=[f"cand:{candidate_id}"])`. Exactly one
  match → transition to `accept` with that `entry_id`, no re-append. Zero → the prior append
  never landed; append now. More than one → `reject`(`ambiguous_reconciliation`), append
  nothing.
- No row → fresh candidate; write `pending`, then append.
This closes the window rather than shrinking it: the worst residual outcome is a `pending` row
that a later run reconciles, never a duplicate entry and never a zero-row candidate. See
Technical Approach → Assembly idempotency for the full sequence.

### Race 4: Two concurrent runners claim the same candidate (round-2 C1)
**Location:** `decision_log.py`, the pre-append claim/probe step.
**Trigger:** A duplicated delivery of the same turn racing a crash-retry, or two processes
invoking the auditable path for the same `(agent_id, turn_id)` concurrently. Both observe a
surviving `pending` row, both probe the journal, both find nothing (neither has committed
`append()` yet), and both append.
**Data prerequisite:** Exactly one runner may proceed from probe to `append()` for a given
`(agent_id, turn_id, candidate_id)`.
**State prerequisite:** Exactly one journal entry per accepted candidate, ever — the same
invariant Race 3 protects, under concurrency rather than serial retry.
**Why the read-then-act probe alone is insufficient:** it is a check followed by a separate
write, with no mutual exclusion between them; Race 1's `MULTI`/`EXEC` covers the record+summary
write, not a cross-process claim on a candidate; and the journal cannot catch the duplicate
because `append()` has no idempotency key and `AppendOnlyViolation` never fires on a fresh
`AutoKeyField` append.
**Mitigation:** A **single atomic Redis op** — `SET <claim_key> <token> NX PX <ttl>` — gates
entry to the probe. Exactly one runner wins; the loser writes nothing. Release is a token-checked
Lua `if GET == token then DEL`, executed with the terminal write. Core commands and Lua only —
Valkey-safe, no modules. The residual TTL-expiry window converges instead of duplicating because
the probe is an identity lookup on the `cand:` tag (Race 3 / Technical Approach → Candidate
identity). Full rationale, the rejected `WATCH`/`MULTI` alternative, and the TTL constant are in
Technical Approach → Atomic assembly claim.

No other concurrency concerns: candidate generation is pure and deterministic; assembly calls
`ProvenanceJournal.append`, which is append-only by M1 contract.

## No-Gos (Out of Scope)

- [ORDERED] **Multi-sentence and cross-turn candidate windows** — deferred to a follow-on
  module sequenced behind M4 (reference resolution) and the epic's consolidation/reflection
  pass. They require M4's cross-sentence anaphora machinery and explode the LLM verdict call
  count; v1's decision log already makes recall measurable so the follow-on is data-driven.
  This resolves open question (a) in favor of sentence-level spans + pattern-lifted entities
  only.
- [SEPARATE-SLUG] **Post-save reconciliation/consolidation engine** over the decision log —
  a distinct feature with its own measurement goal, filed under epic #456's
  "consolidation/reflection pass" item. M3 only produces the log.
- [SEPARATE-SLUG] **Switching the harness default to the auditable path** — the harness
  (`src/popoto/integrations/service.py`) keeps its current extraction default; opting it into
  the auditable path is a separate wiring decision once M4/M9 land. M3 ships the adapter and
  the flag; enabling it broadly is not part of this module.
- [SEPARATE-SLUG] **M9 seeded audit UI/dashboard** over the decision log — a downstream module
  that asserts against this log; M3 only writes and reads it programmatically.
- **LLM-driven candidate generation or entity detection** — would make the candidate set
  non-deterministic and break exhaustive enumeration. Entity lifting stays deterministic.
- **Retroactive modification of journal entries** — the M1 journal is append-only by
  contract; M3 does not add an edit path.
- **Capping or trimming the decision log** — no `Defaults` cap constant and no LTRIM for the
  per-candidate detail log in v1. Retention is deferred to M9 (#568) and will be designed
  against measured growth. This resolves open question (b).
- **Capping the per-turn candidate count** — no `Defaults` candidate cap, no truncation of the
  generated candidate set. A cap would silently drop generated candidates and contradict
  Success Criterion 1 and the exhaustive-enumeration goal. Verdict-call cost is managed by the
  path being default-off (see Risk 1).
- **Adding a fifth terminal state** — the terminal vocabulary stays exactly
  `firewall_drop | accept | reject | withhold` per #562's acceptance criteria. `pending` is a
  non-terminal intent marker on the same row, and post-accept journal refusals are
  disambiguated by reason code, not by a new state.
- **Normalizing or distilling accepted content** — `statement` is byte-identical to the
  verbatim span. Any normalization, canonicalization, or distillation is M4's scope, and doing
  it here would violate #562's own acceptance criteria. This resolves open question (c).

## Update System

No update-system changes required — this is an additive library module behind a default-off
flag. No new config files to propagate, no deploy steps. New `Defaults` constants are pinned
in-repo (`fields/constants.py`), consistent with the magic-numbers convention.

## Agent Integration

No new agent/tool/MCP surface is required by this module. The auditable path is reached
through the existing `SubconsciousMemory` constructor (a new opt-in flag), which the harness
already calls; M3 does not wire the harness onto it by default (see No-Gos). A grep check
`grep -n 'auditable_extraction' src/popoto/recipes/subconscious_memory.py` confirms the
surface exists; no MCP registration is changed.

## Documentation

### Feature Documentation
- [ ] Create `docs/features/auditable-extraction.md` describing the candidate generator, the
  four terminal states, the non-terminal `pending` marker and the write-ordering guarantee it
  provides, the two `firewall_drop` reason codes (pre-LLM vs post-accept journal block), the
  enum-verdict contract, and the retention policy.
- [ ] Include a runnable snippet that enables `auditable_extraction`, runs a turn, and prints
  `DecisionLog.compute_metrics(...)` — so an adopter can realize the measurement benefit
  without waiting on the harness-wiring follow-on.
- [ ] Record the #510 numbers (heuristic 0.2078 vs raw 0.3636) and the "audit the shape to make
  it correctable" rationale on the page, so readers are not surprised that the audited v1 shape
  is not the harness default.
- [ ] Add entry to `docs/features/README.md` index table.

### External Documentation Site
- [ ] Update the extraction/memory page on the mkdocs site (`docs/`) to describe the opt-in
  auditable path.
- [ ] Verify docs build passes (`mkdocs build --strict`).

### Inline Documentation
- [ ] Docstrings on the new `candidates.py`, `verdict.py`, `decision_log.py` modules and the
  `SubconsciousMemory.auditable_extraction` flag.
- [ ] Note the retention position in the decision-log module docstring: detail rows are
  unbounded in v1 by deliberate decision, the per-turn summary is a convenience index (not a
  completeness fallback), and the revisit triggers are measured growth signals, not a constant.
- [ ] **Stale-`pending` recovery is manual in v1 (round-2 N1)** — state it in the
  `decision_log.py` module docstring and on the docs page: no sweep, no TTL on decision rows
  (a TTL would delete audit evidence), no age alert. Give the operator recipe verbatim —
  `DecisionLog.list_pending(agent_id, older_than=...)` oldest-first, then re-invoke the auditable
  path for each stale `(agent_id, turn_id)` to reconcile it — and name the follow-on owner for a
  periodic age-keyed scan and alerting (M9 #568 or an ops runbook).
- [ ] **Document the terminal-write conflict guard (round-3 C1 / round-4 C1)** in the
  `decision_log.py` module docstring: a non-`accept` terminal write is refused against a row
  already terminal `accept` with an `entry_id`, and the refusal surfaces as
  `detail_code = 'terminal_conflict_refused'`. State why: the LLM verdict is non-deterministic,
  so a retried verdict must never be able to split the decision log from the journal. State the
  mechanism too — a single conditional Lua script run via `EVAL`, which every terminal write goes
  through; there is no unconditional fast path.
- [ ] **Document `DecisionRecord`'s composite key (round-4 C3)** in the model's docstring:
  `agent_id` + `turn_id` + `candidate_id` are all `KeyField`s so re-saving transitions the row in
  place; `AutoKeyField` is forbidden here (unlike `JournalEntry`); KeyFields join alphabetically,
  so the Redis key is `DecisionRecord:agent_id:candidate_id:turn_id`; and colons inside
  `candidate_id` render escaped in the key.
- [ ] Document `detail_code` as a **free-form diagnostic string** (`StringField(default="")`),
  distinct from the `state`/`reason_code` enums, and written only by trusted code.
- [ ] Document `DecisionRecord.written_at` — stamped on both the `pending` write and the
  terminal transition, and the field `list_pending(agent_id, older_than=...)` sorts on.
- [ ] Document the `cand:{candidate_id}` subject-tag convention on written entries, the
  low-entropy `candidate_id` format requirement (a digest-shaped id is blocked by the journal's
  own firewall as `high_entropy`), and the `SET ... NX PX` assembly claim with its
  `Defaults.M3_ASSEMBLY_CLAIM_TTL_MS` liveness bound.

## Success Criteria

- [ ] For any input turn, every generated candidate appears in the decision log with exactly
  one **terminal** state (`firewall_drop | accept | reject | withhold`) and a reason code —
  proven by a test that constructs a turn, runs the auditable path, and asserts log
  completeness. **Companion assertion (the blocker fix):** after `extract_memories()` returns,
  **no row is left in the non-terminal `pending` state**, and every `accept` row carries a
  journal `entry_id`. `pending` is not a fifth terminal state — it is an intent marker written
  *before* assembly, so no candidate can ever reach `append()` with zero decision-log rows.
  A separate test asserts the `pending` row exists at the moment `append()` is entered.
- [ ] The LLM contributes only enum verdicts; accepted memory content is byte-identical to a
  verbatim candidate span — proven by a test asserting no free text is persisted and the
  journal `statement`/`verbatim` equals the span.
- [ ] **The offline precision/recall computation ships working and exercised** (a capability,
  not a claim of realized production measurement — the harness is not wired in this module).
  `DecisionLog.compute_metrics(agent_id, gold_labels)` reads decision-log rows plus a
  gold-label mapping and nothing else — demonstrated by
  `test_precision_recall_computable_from_log_alone`, which runs the full auditable path over a
  multi-turn fixture transcript, asserts exact precision/recall/F1 values and the
  per-`generator_rule` breakdown, and re-computes identical numbers with the journal keyspace
  absent. A docs snippet shows an adopter enabling the flag and printing the metrics.
- [ ] Exactly one journal entry exists per accepted candidate across re-runs, including a run
  interrupted between `append()` and the terminal decision-log write — proven by the assembly
  idempotency test (Race 3). **Two companion assertions added in round 2:** (a) *under
  concurrency* — two runners racing the same `(agent_id, turn_id, candidate_id)` produce exactly
  one entry, because the claim is a single `SET ... NX PX` and the loser appends nothing
  (Race 4); (b) *under duplicate text* — a turn containing two candidates with byte-identical
  text reconciles each `pending` row onto its **own** entry, because reconciliation matches the
  `cand:{candidate_id}` subject tag rather than `verbatim`.
- [ ] Existing `SubconsciousMemory.extract_memories()` behavior is preserved behind a
  flag/adapter — proven by a default-path test asserting byte-for-byte current behavior, and
  the existing extraction (33) + subconscious-memory (15 + 27) tests staying green in the
  environment recorded in Test Impact.
- [ ] Valkey-safe: no Redis modules used — covered by code review and the standard test suite.
- [ ] Tests pass (`/do-test`).
- [ ] Documentation updated (`/do-docs`).
- [ ] Tests at `tests/test_auditable_extraction.py`; docs page under `docs/features/`.

## Team Orchestration

When this plan is executed, the lead agent orchestrates work using Task tools. The lead never
builds directly — it deploys team members and coordinates.

### Team Members

- **Builder (candidate-gen)** — Name: `candidate-builder`. Agent Type: builder. Role: candidate
  generator + `Candidate` dataclass. Resume: true.
- **Builder (verdict-log)** — Name: `verdict-builder`. Agent Type: builder. Role: verdict
  stage + decision-log model + `SubconsciousMemory` flag wiring + assembly. Resume: true.
- **Validator (extraction)** — Name: `extraction-validator`. Agent Type: validator. Role:
  verifies candidate determinism, enum-verdict confinement, decision-log completeness
  (including write ordering: `pending` before `append()`, no surviving `pending`), assembly
  idempotency (Race 3), the offline metrics computation, and default-path preservation.
  Resume: true.
- **Documentarian** — Name: `doc-author`. Agent Type: documentarian. Role: docs page + docs
  site + inline docstrings. Resume: true.

### Available Agent Types

**Tier 1 — Core:** `builder`, `validator`, `code-reviewer`, `test-engineer`, `documentarian`.
**Domain expertise:** this is Popoto-ORM + Redis/Valkey data-modeling and an LLM call already
made by the project. Assign a `builder` (or `code-reviewer` for review-only) with a
`Domain: popoto-data-modeling` line. *(An earlier draft cited a `DOMAIN_FRAMING.md` as the
source of these rules; no such file exists anywhere in the repo — the citation was wrong and is
removed.)* The domain rules, stated inline so there is no dangling reference:
- **Valkey-safe only** — no Redis modules (`BF.*`, `CMS.*`, `TOPK.*`, `TS.*`); every feature
  must run on both Redis and Valkey (`CLAUDE.md`).
- **Untrusted input** — LLM output is untrusted; it may contribute enum verdicts and enum
  reason codes only, never free text into the store.
- **Magic numbers are pinned in-repo** — numeric constants live in
  `popoto.fields.constants.Defaults`, not constructor kwargs (`CLAUDE.md`).
- **Popoto model conventions** — public model attributes must be `Field` instances; private
  attrs take an underscore prefix; field names start lowercase; `limit`, `order_by`, `values`
  are reserved.

## Step by Step Tasks

### 1. Candidate generator
- **Task ID**: build-candidate
- **Depends On**: none
- **Validates**: tests/test_auditable_extraction.py (create), tests/test_extraction.py (unchanged, green)
- **Informed By**: recon of `HeuristicExtractionProvider._split_sentences` regex
- **Assigned To**: candidate-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `Candidate` dataclass (`text`, `turn_id`, `candidate_id`, `start`, `end`,
  `generator_rule`) in `src/popoto/extraction/candidates.py`.
- Add `generate_candidates(turn_id, text) -> List[Candidate]` enumerating sentence spans
  (reuse the heuristic split regex) + deterministic entity-lifted candidates. Pure,
  deterministic, no LLM. Empty turns produce zero candidates (the caller logs the
  `reject`(`empty_turn`) row).
- **`candidate_id` format is load-bearing — it becomes a journal subject tag (round-2 C2).**
  Emit `candidate_id = f"{turn_id}:{generator_rule}:{ordinal}"`: unique within a turn (two
  candidates with byte-identical text still get different ids), deterministic, and
  **low-entropy**. It must **not** be a hash or digest: the journal's write-time firewall scans
  every subject tag (`_scan_or_block(agent_id, *subject_tags, ...)`,
  `provenance_journal.py:969`), and a 64-char hex digest is measurably blocked as
  `high_entropy`, which would make M3's own writes fail. Add a test asserting
  `scan_never_record(f"cand:{c.candidate_id}").blocked is False` for every candidate generated
  from a representative corpus.
- **Do NOT add a `CandidateGenerator` pluggable-rule class** — one implementation, no second
  caller, both plausible extra rules banned by No-Gos. The single function is the surface.
- **Do NOT add any per-turn candidate cap** (no `Defaults` constant, no truncation): a cap
  would silently drop generated candidates and violate Success Criterion 1. Pinned by a
  Verification anti-criterion.

### 2. Verdict stage
- **Task ID**: build-verdict
- **Depends On**: none (parallel with candidate-gen; consumes its API)
- **Validates**: tests/test_auditable_extraction.py (create)
- **Informed By**: recon of `claude.py` provider conventions and the enum-verdict contract
- **Assigned To**: verdict-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `Verdict` / `ReasonCode` enums in `src/popoto/extraction/verdict.py` with terminal states
  `firewall_drop | accept | reject | withhold` and the fixed reason-code vocabulary. The
  reason-code vocabulary must include `pre_llm_candidate_block`, `post_accept_journal_block`
  (both under `firewall_drop`), `assembly_failed`, `ambiguous_reconciliation` (both under
  `reject`), `llm_unavailable`, `empty_turn`, and `accepted`.
- Add the non-terminal `pending` marker to the *state* enum only, clearly annotated as
  non-terminal and excluded from any terminal-state aggregation. The LLM's verdict vocabulary
  stays `accept | reject | withhold` — the model never emits `pending` or `firewall_drop`.
- Add `llm_verdict(candidate)` returning only `{candidate_id, verdict, reason_code}`; malformed/
  empty replies map to `reject`(llm_unavailable).
- Run `scan_never_record(candidate.text)` per candidate before the LLM; on `blocked`, log
  `firewall_drop` and do not call the LLM.

> **Task 3 was split into 3a and 3b (round-4 C4).** Across three revision rounds Task 3 accreted
> roughly a dozen responsibilities inside a single `Parallel: false` task, with no boundary
> between concurrency-control work and public-API-surface work. The split follows the boundary
> Data Flow already draws — step 5 (the log itself) versus steps 6-7 (assembly and wiring). The
> **same builder (`verdict-builder`) owns both task IDs**; this buys two validation checkpoints,
> not two people. One deliberate deviation from the critique's suggested split: the **guarded
> terminal-write helper lives in 3a**, not 3b, because it *is* the decision-log writer — putting
> it in 3b would leave 3a shipping the bare unconditional writes the guard exists to forbid.

### 3a. Decision log core
- **Task ID**: build-decision-log-core
- **Depends On**: build-candidate, build-verdict
- **Validates**: tests/test_auditable_extraction.py (create)
- **Assigned To**: verdict-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `DecisionRecord` model + `DecisionLog` writer/reader in `src/popoto/extraction/decision_log.py`.
  The record carries: `agent_id`, `turn_id`, `candidate_id`, `state`, `reason_code`,
  `generator_rule`, span offsets, a hash of `candidate.text`, an optional `entry_id` (set on
  terminal `accept`), a `detail_code` (see the schema bullet below), and
  **`written_at = FloatField(...)` (round-3 N3)** — the row's write timestamp, mirroring
  `JournalEntry.captured_at` (`provenance_journal.py:301`). `written_at` is set on **every**
  write: the `pending` write and the terminal transition both stamp it. Without it
  `list_pending`'s `older_than` has nothing to compare and "oldest-first" has nothing to sort
  by.
- **KEYING — get this right first; everything else depends on it (round-4 C3).** Declare
  `agent_id`, `turn_id` and `candidate_id` **all** as `KeyField` on `DecisionRecord`, so the
  composite key identifies the row and re-saving the same tuple **transitions that row in
  place**. **Do NOT use `AutoKeyField` on this model.** Do **not** copy the sibling
  `JournalEntry`, which uses `entry_id = AutoKeyField()`
  (`src/popoto/recipes/provenance_journal.py:299`): that mints a **new row per save**, which is
  right for an append-only journal and fatal here — it would silently break the `pending` →
  terminal transition, the terminal-write conflict guard (which would read an empty row and
  never refuse), the Race 3/4 reconciliation, and `list_pending`. Two gotchas to expect:
  (a) composite `KeyField`s are supported (`src/popoto/models/base.py:497-506`,
  `src/popoto/models/db_key.py:60`) but join **ALPHABETICALLY, not in declaration order**
  (`base.py:284-301`), so the real Redis key is `DecisionRecord:agent_id:candidate_id:turn_id`;
  (b) `DB_key` escapes colons inside values (`db_key.py:86-88`), so a `candidate_id` like
  `t-41:sent:0` renders escaped in the key — correct, do not "fix" it.
  **Test that pins it (required):** write the `pending` row for one
  `(agent_id, turn_id, candidate_id)`, transition it to a terminal state, then assert **exactly
  one** Redis row exists for that composite key and that its `state` is the terminal one.
- **`detail_code` schema (round-4 C5):** `detail_code = StringField(default="")` — a **free-form
  diagnostic string**, not an enum type, following the `verbatim`/`statement` convention
  (`provenance_journal.py:304-305`). It carries three different payload shapes by design: the
  literal `terminal_conflict_refused`, an exception class name on `assembly_failed`, and
  `",".join(entry_ids)` on `ambiguous_reconciliation`. `state` and `reason_code` remain genuine
  enums; `detail_code` is written only by trusted code, never by the model.
- **Implement the guarded terminal-write helper — the ONLY way any terminal state is written
  (round-4 C1/C2).** Every terminal write of `firewall_drop` / `reject` / `withhold` goes through
  a single `EVAL`'d Lua script that reads the row and conditionally writes:
  `if HGET(state)=='accept' and HGET(entry_id)~='' then HSET detail_code='terminal_conflict_refused'; return 0
  else HSET <terminal state, reason_code, written_at, ...>; return 1 end`
  (full script in Technical Approach → Terminal-write conflict guard). It is invoked for
  **every** non-`accept` terminal write, including the pre-LLM M2 `firewall_drop` (which cannot
  conflict in practice, since no prior row exists for a fresh candidate) — **do NOT add a
  "fast path" bare `HSET` or model `.save()` for the non-conflicting case.** `EVAL` is an
  established in-repo pattern (`models/query.py:445,463`;
  `fields/existence_filter.py:430`); core commands plus Lua only, Valkey-safe. **Do NOT try to
  implement this inside `MULTI`/`EXEC`** (which queues blind and cannot branch on a read) **and
  do NOT implement it by routing the write through the `SET NX` claim key** (which never
  inspects `state`, and which non-`accept` paths never take) — both shapes were named in an
  earlier revision and are now explicitly deleted.
- Write per-candidate detail rows **unbounded** — no LTRIM, and do **not** add a
  `Defaults` cap constant for this log. Add the per-turn compact summary hash as a
  convenience/query index only; detail rows remain the sole source of truth. The summary
  aggregates **terminal states only** — `pending` rows are never counted into it.
- **Implement the two-phase write ordering (blocker fix).** `firewall_drop`/`reject`/`withhold`
  write once, terminally — via the guarded helper above, never unconditionally. `accept` writes a
  committed `pending` row **before** calling `append()`, then transitions the *same*
  `(agent_id, turn_id, candidate_id)` row to
  `accept` / `firewall_drop`(`post_accept_journal_block`) / `reject`(`assembly_failed`) per the
  Technical Approach transition table. No code path may call `append()` before its `pending`
  row is committed.
- Add `DecisionLog.list_pending(agent_id, older_than=None) -> List[DecisionRecord]` — the
  operator recovery reader for stale `pending` rows (round-2 N1). A thin reader over existing
  rows; it filters and sorts on the `written_at` field added above (round-3 N3), returning rows
  oldest-first; **no TTL on decision rows** (that would delete audit evidence) and no sweep
  daemon in v1.
- Write the per-candidate record + per-turn summary update in one `MULTI`/`EXEC` (this applies
  to each write — the `pending` write and the terminal transition are separate transactions by
  design; collapsing them would defeat the ordering guarantee). The record half of a *terminal*
  write is the guarded `EVAL` above, not a blind queued `HSET`; the script is atomic on its own,
  so the transaction exists only to pair it with the summary update.

### 3b. Assembly concurrency + wiring
- **Task ID**: build-assembly-wiring
- **Depends On**: build-decision-log-core
- **Validates**: tests/test_auditable_extraction.py (create); existing
  test_subconscious_memory*.py stay green
- **Assigned To**: verdict-builder
- **Agent Type**: builder
- **Parallel**: false
- **Implement the atomic assembly claim (Race 4 / round-2 C1) — this gates the probe.** Before
  the `pending` write, take the claim with a **single Redis op**:
  `SET popoto:m3:claim:{agent_id}:{turn_id}:{candidate_id} <uuid4 token> NX PX
  Defaults.M3_ASSEMBLY_CLAIM_TTL_MS`. Add `Defaults.M3_ASSEMBLY_CLAIM_TTL_MS = 30_000` to
  `src/popoto/fields/constants.py` (pinned magic number, not a constructor kwarg). A runner that
  loses the claim **returns without appending and without transitioning any row**. Release the
  claim with a token-checked Lua `if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('DEL', KEYS[1]) end` in the same step as the terminal write. **Do NOT implement
  this as a `GET`-then-`SET`, and do NOT use `WATCH`/`MULTI`** (rejected in Technical Approach →
  Atomic assembly claim: it needs a dedicated connection and a retry loop). `SET NX PX`, `DEL`
  and `EVAL` are core commands — Valkey-safe, no modules.
- **Implement the assembly dedup probe by candidate identity (Race 3 / round-2 C2).** Before
  every `append()`, read the row; on a surviving `pending`, probe
  `JournalEntry.query.filter(turn_id=<turn_id>, subjects__all=[f"cand:{candidate_id}"])`.
  Exactly one match → reconcile to `accept` with that `entry_id`, no re-append. Zero → append.
  More than one → terminal `reject`(`ambiguous_reconciliation`) recording the matching
  `entry_id`s in `detail_code`, append nothing. **Do NOT match on `verbatim` text** — two
  candidates in one turn can be byte-identical, so a text match can record the wrong
  `entry_id`.
- **Wire every terminal write through 3a's guarded helper — the terminal-write conflict guard
  (round-3 C1, mechanism corrected in round-4 C1).** The rule is unchanged: a non-`accept`
  terminal write must never overwrite a row already terminal `accept` with a non-empty
  `entry_id`; on conflict the `accept` row and its `entry_id` stand and
  `detail_code = 'terminal_conflict_refused'` is recorded on that same row — no second row, no
  new state, no exception to the caller. It matters because only `accept` is claim-protected and
  the LLM verdict is **non-deterministic**, so a retried verdict resolving `reject` would
  otherwise permanently split the decision log from the journal. In 3b the work is to make sure
  **no call site bypasses the helper**: the M2 pre-LLM `firewall_drop`, the verdict-stage
  `reject`/`withhold`, the `llm_unavailable` and `empty_turn` rows, the
  `assembly_failed` / `post_accept_journal_block` transitions and the
  `ambiguous_reconciliation` write all route through it. **Do NOT expand this into a general
  terminal-state CAS redesign** — non-conflicting writes still result in exactly one write.
  **Test that pins it:** two verdict resolutions for one candidate — `accept` (which appends and
  writes the terminal `accept` row with an `entry_id`) followed by a retried `reject` — assert
  exactly one terminal state survives, it is `accept`, its `entry_id` still resolves to the
  journal entry, `detail_code == 'terminal_conflict_refused'`, and the decision log agrees with
  whether a journal entry exists.
- Extend `ExtractedFact` with optional span/candidate fields (default `None`).
- Add `SubconsciousMemory(auditable_extraction=...)` opt-in flag; default path unchanged.
- Add trusted assembly: `accept`ed candidates → `ProvenanceJournal.append(agent_id=<agent_id>,
  kind='assert', verbatim=<span>, statement=<span>, speaker=..., turn_id=...,
  subjects=[*topic_tags, f"cand:{candidate_id}"])`. `agent_id` is keyword-only and required —
  never `None`. The `cand:` tag is **mandatory on every M3 append** — it is the identity the
  Race 3 probe looks up; omitting it silently reintroduces the text-match unsoundness.
- Add `DecisionLog.compute_metrics(agent_id, gold_labels) -> Metrics` (precision, recall, F1,
  per-`reason_code` and per-`generator_rule` breakdowns) reading decision-log rows only. The
  per-`reason_code` dimension exists to separate privacy drops, LLM rejects, and infrastructure
  failures in one metric run (see Technical Approach → Offline precision/recall); assert that
  separation in the backing test.
- Add the `AuditableExtractionConfig` frozen dataclass (round-4 N1) in `decision_log.py`, with
  exactly two v1 fields — `verdict_provider` and `journal` — exported from `popoto.extraction`.
  No numeric knobs on it; `Defaults.M3_ASSEMBLY_CLAIM_TTL_MS` stays a pinned in-repo constant.
  `auditable_extraction=None` (the default) must leave `extract_memories()` byte-for-byte
  unchanged.

### 4. Validation
- **Task ID**: validate-extraction
- **Depends On**: build-candidate, build-verdict, build-decision-log-core, build-assembly-wiring
- **Assigned To**: extraction-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify candidate enumeration is deterministic and exhaustive for representative turns.
- Verify the LLM contributes only enum verdicts (no free text persisted).
- Verify every candidate has exactly one terminal state + reason code in the decision log, and
  that no `pending` row survives a completed run.
- Verify the `pending` row is committed before `append()` is entered, and that both assembly
  failure branches (`JournalBlockedError` → `firewall_drop`/`post_accept_journal_block`; other
  raises → `reject`/`assembly_failed`) land terminally on the same row.
- Verify assembly idempotency: one journal entry per accepted candidate across a re-run and
  across a run interrupted between `append()` and the terminal write.
- Verify the **atomic claim** (Race 4): two concurrent runners over the same
  `(agent_id, turn_id, candidate_id)` yield exactly one journal entry and one terminal row, the
  loser appends nothing, and the claim is taken with a single `SET ... NX PX` (not a
  `GET`-then-`SET`, not `WATCH`/`MULTI`).
- Verify **candidate identity**: every M3 append carries `cand:{candidate_id}` in `subjects`;
  reconciliation on a turn with two byte-identical candidate texts maps each `pending` row to
  its own entry; `scan_never_record(f"cand:{candidate_id}")` is unblocked for every generated
  candidate (no digest-shaped ids).
- Verify the **terminal-write conflict guard** (round-3 C1, round-4 C1): a retried `reject`
  against a candidate already terminal `accept` with an `entry_id` is **refused** — the `accept`
  row and its `entry_id` survive unchanged, `detail_code == 'terminal_conflict_refused'`, exactly
  one terminal state exists, and the decision log agrees with the journal on whether an entry
  exists. Also verify (a) the guard is implemented as a single conditional Lua script run via
  `EVAL`, **not** as a blind `MULTI`/`EXEC` write and **not** by routing the write through the
  `SET NX` claim key, and (b) **no** non-`accept` terminal write path bypasses it — no bare
  `HSET`/`.save()` "fast path" exists for terminal states in `decision_log.py`.
- Verify **`DecisionRecord` keying** (round-4 C3): `agent_id`, `turn_id` and `candidate_id` are
  all `KeyField`s, no `AutoKeyField` appears on the model, and writing `pending` then
  transitioning to a terminal state leaves **exactly one** Redis row for that composite key.
  Confirm the observed key shape is `DecisionRecord:agent_id:candidate_id:turn_id` (alphabetical
  KeyField ordering), not declaration order.
- Verify `DecisionRecord.written_at` is stamped on both the `pending` write and the terminal
  transition, and that `list_pending(agent_id, older_than=...)` filters and sorts on it
  oldest-first (round-3 N3).
- Verify `DecisionLog.list_pending` returns stale `pending` rows and that re-invoking the path
  for a stale `(agent_id, turn_id)` reconciles them without a second journal entry.
- Verify offline precision/recall computes from the decision log alone (exact numbers, plus
  the journal-absent re-computation).
- Verify the default path is byte-for-byte unchanged (existing tests green).
- Run the gates: `pytest tests/test_auditable_extraction.py -x -q`, `mypy src/`,
  `mkdocs build --strict`.

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-extraction
- **Assigned To**: doc-author
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/features/auditable-extraction.md`; add to `docs/features/README.md`.
- Update the extraction/memory page on the docs site; verify `mkdocs build --strict`.
- Add docstrings to the new modules and the `auditable_extraction` flag.

### 6. Final validation
- **Task ID**: validate-all
- **Depends On**: validate-extraction, document-feature
- **Assigned To**: extraction-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all verification gates; confirm every Success Criterion is met (including docs).
- Generate the final report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| New auditable tests pass | `pytest tests/test_auditable_extraction.py -x -q` | exit code 0 |
| Offline metrics demonstrated | `pytest tests/test_auditable_extraction.py -k precision_recall_computable_from_log_alone -q` | exit code 0, 1 test selected |
| No zero-row candidate: pending precedes append | `pytest tests/test_auditable_extraction.py -k "pending_written_before_append or no_pending_survives" -q` | exit code 0, 2 tests selected |
| Assembly idempotency (Race 3) | `pytest tests/test_auditable_extraction.py -k assembly_idempoten -q` | exit code 0 |
| Atomic claim under concurrency (Race 4) | `pytest tests/test_auditable_extraction.py -k "concurrent_claim or claim_loser_appends_nothing" -q` | exit code 0, 2 tests selected |
| Terminal-write conflict guard (round-3 C1 / round-4 C1) | `pytest tests/test_auditable_extraction.py -k "terminal_conflict_refused or conflicting_terminal_verdicts" -q` | exit code 0, at least 1 test selected |
| Guard is a conditional Lua script, not MULTI/EXEC or SET NX | `grep -rn "eval\|register_script" src/popoto/extraction/decision_log.py` | at least 1 match, on the terminal-write guard script |
| `DecisionRecord` row is updated in place, not duplicated (round-4 C3) | `pytest tests/test_auditable_extraction.py -k "decision_record_in_place or single_row_per_candidate" -q` | exit code 0, at least 1 test selected |
| `DecisionRecord` never uses `AutoKeyField` [anti-criterion, code-only] | `grep -nE "^\s*\w+\s*=\s*AutoKeyField\(" src/popoto/extraction/decision_log.py` | match count == 0 |
| `DecisionRecord` keys on the full tuple [anti-criterion] | `grep -c "= KeyField(" src/popoto/extraction/decision_log.py` | count == 3 (`agent_id`, `turn_id`, `candidate_id`) |
| `written_at` stamped + `list_pending` ordering (round-3 N3) | `pytest tests/test_auditable_extraction.py -k "written_at or list_pending_oldest_first" -q` | exit code 0, at least 1 test selected |
| Candidate identity + duplicate-text reconciliation | `pytest tests/test_auditable_extraction.py -k "candidate_identity_tag or duplicate_text_reconcile" -q` | exit code 0, 2 tests selected |
| Candidate ids survive the journal firewall | `pytest tests/test_auditable_extraction.py -k candidate_id_not_firewall_blocked -q` | exit code 0 |
| Existing extraction tests unaffected | `pytest tests/test_extraction.py -x -q` | exit code 0 |
| Existing memory tests unaffected | `pytest tests/test_subconscious_memory.py tests/test_subconscious_memory_integration.py -x -q` | exit code 0 |
| M2 firewall tests unaffected | `pytest tests/test_never_record_firewall.py -x -q` | exit code 0 |
| M1 journal tests unaffected | `pytest tests/test_provenance_journal.py -x -q` | exit code 0 |
| Type checks | `mypy src/` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| Opt-in surface present | `grep -n "auditable_extraction" src/popoto/recipes/subconscious_memory.py` | output contains `auditable_extraction` |
| No free-text verdict persists [anti-criterion] | `grep -rn "write_free\|verdict_text\|free_text" src/popoto/extraction/` | match count == 0 |
| No Redis module usage [anti-criterion] | `grep -rn "BF\.\|CMS\.\|TOPK\.\|TS\." src/popoto/extraction/` | match count == 0 |
| Decision log is uncapped [anti-criterion, code-only] | `grep -rn "\.ltrim(\|DECISION_LOG_MAX" src/popoto/extraction/` | match count == 0 |
| No per-turn candidate cap [anti-criterion] | `grep -rniE "candidate_(per_turn_)?cap\|MAX_CANDIDATES" src/popoto/extraction/ src/popoto/fields/constants.py` | match count == 0 |
| No pluggable-rule class [anti-criterion] | `grep -rn "class CandidateGenerator" src/popoto/extraction/` | match count == 0 |
| Assembly passes required `agent_id` [anti-criterion, multiline-aware, scoped to M3, real call shape] | `perl -0777 -ne 'while (/journal\.append\s*\(/g) { print "MISSING\n" unless substr($_, pos($_), 200) =~ /agent_id/ }' src/popoto/extraction/*.py` | match count == 0 |
| Every M3 append carries a candidate identity tag [anti-criterion, real call shape] | `perl -0777 -ne 'while (/journal\.append\s*\(/g) { print "MISSING\n" unless substr($_, pos($_), 300) =~ /cand:/ }' src/popoto/extraction/*.py` | match count == 0 |
| Reconciliation never matches on text [anti-criterion] | `grep -rn "verbatim ==\|verbatim=candidate.text)" src/popoto/extraction/decision_log.py` | match count == 0 |
| Claim is a single atomic op, not read-then-act [anti-criterion] | `grep -rn "\.watch(\|pipeline().watch" src/popoto/extraction/` | match count == 0 |
| Claim uses `SET NX PX` | `grep -rn "nx=True" src/popoto/extraction/decision_log.py` | at least 1 match, on the claim key |

**Why the `agent_id` row changed shape (round-2 C3).** Round 1 added
`grep -rn "ProvenanceJournal.append(" src/popoto/ | grep -v "agent_id"` expecting 0 matches. That
row **could never pass**: verified at `f6e8525`, it returns **2 matches**
(`src/popoto/recipes/provenance_journal.py:20` and `:351`) *before any M3 code exists* — both are
correct, `agent_id`-passing calls that black wrapped across lines, so a line-oriented grep never
sees `agent_id` on the call's own line. M3's assembly call is long enough that black will wrap it
identically, adding a third permanent false positive. The replacement is multiline-aware
(`perl -0777`, matching within a 200-character window after each call's open paren rather than
parsing balanced parens) and **scoped to `src/popoto/extraction/`**, never `src/popoto/` at
large. Measured at `f6e8525` on the current tree (`src/popoto/extraction/` = `__init__.py`,
`claude.py`): **match count == 0**, as expected. Three controls were run to prove the check is
not a no-op: (a) the same check over `src/popoto/recipes/provenance_journal.py` returns **0**,
i.e. it correctly recognizes the two black-wrapped calls the old grep false-positived on;
(b) a synthetic `ProvenanceJournal.append(\n kind=..., statement=...\n)` with no `agent_id`
returns **1** (caught); (c) a nested-call form
`ProvenanceJournal.append(statement=fmt(x), agent_id=a)` returns **0** (no false positive from
the inner parenthesis). The window form was chosen over a `(.*?)\)` capture precisely because the
non-greedy capture stops at the first inner `)` and false-positives on control (c).

**Round-5 correction (PR #591 review, tech-debt 1) — anchor pattern was the class name, not the
real call shape.** The four checks above (`AutoKeyField`, `ltrim|DECISION_LOG_MAX`, and both
`ProvenanceJournal\.append` perl checks) were **simultaneously failing and inert** as originally
written: they matched module-docstring prose, not code, so they returned nonzero (reporting a
false "FAIL") while never being capable of catching a real regression (a genuine violation would
also just add more prose-shaped matches, or in the append checks' case, could never match at all
— the real call site is the **instance** call `journal.append(...)` at `decision_log.py:755`,
never the literal string `ProvenanceJournal.append`). All four rows above are corrected to
code-only anchors (`^\s*\w+\s*=\s*AutoKeyField\(`, `\.ltrim\(\|DECISION_LOG_MAX`, and
`journal\.append\s*\(` in place of `ProvenanceJournal\.append\s*\(`).

Re-run at `6586bff` in `.worktrees/auditable_extraction_m3` (the corrected commands and their
real output, green-state):

```
$ grep -nE "^\s*\w+\s*=\s*AutoKeyField\(" src/popoto/extraction/decision_log.py
(no output, exit 1)                                                    # match count == 0 ✅

$ grep -rn "\.ltrim(\|DECISION_LOG_MAX" src/popoto/extraction/
(no output, exit 1)                                                    # match count == 0 ✅

$ perl -0777 -ne 'while (/journal\.append\s*\(/g) { print "MISSING\n" unless substr($_, pos($_), 200) =~ /agent_id/ }' src/popoto/extraction/*.py
(no output)                                                            # match count == 0 ✅

$ perl -0777 -ne 'while (/journal\.append\s*\(/g) { print "MISSING\n" unless substr($_, pos($_), 300) =~ /cand:/ }' src/popoto/extraction/*.py
(no output)                                                            # match count == 0 ✅
```

Red-state proof, one deliberate break per check, each reverted immediately after (not committed):

```
$ # AutoKeyField: add "    rogue_field = AutoKeyField()" after the real agent_id = KeyField() line
$ grep -nE "^\s*\w+\s*=\s*AutoKeyField\(" src/popoto/extraction/decision_log.py
179:    rogue_field = AutoKeyField()                                    # FAILS as required ✅

$ # ltrim: insert "self._redis.ltrim(SUMMARY_KEY_PREFIX, 0, 999)" before the journal.append( call
$ grep -rn "\.ltrim(\|DECISION_LOG_MAX" src/popoto/extraction/
src/popoto/extraction/decision_log.py:755:            self._redis.ltrim(SUMMARY_KEY_PREFIX, 0, 999)   # FAILS ✅

$ # agent_id: drop agent_id=agent_id from the real journal.append(...) call
$ perl -0777 -ne 'while (/journal\.append\s*\(/g) { print "MISSING\n" unless substr($_, pos($_), 200) =~ /agent_id/ }' src/popoto/extraction/*.py
MISSING                                                                 # FAILS as required ✅

$ # cand: drop the f"cand:{candidate.candidate_id}" tag from subjects=[...] in the same call
$ perl -0777 -ne 'while (/journal\.append\s*\(/g) { print "MISSING\n" unless substr($_, pos($_), 300) =~ /cand:/ }' src/popoto/extraction/*.py
MISSING                                                                 # FAILS as required ✅
```

The properties themselves were never in doubt — `test_candidate_identity_tag_on_assembled_entries`
asserts both `agent_id` and the `cand:` tag against the real call at `decision_log.py:755` — this
correction repairs the **grep safety net** so it can independently catch a future regression, not
the underlying behavior. The `= KeyField(` count-== 3 row was already code-only (it only matches
an actual field assignment, never the module docstring's double-backtick prose) and needed no
change.

## Critique Results

War room run 2026-08-25 at baseline `76d649a`. Depth: FULL (forced by `appetite: Large`).
Roster: Risk & Robustness, Scope & Value, History & Consistency. Verdict: **NEEDS REVISION**.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | History & Consistency + Risk & Robustness | Write-ordering contradiction: Data Flow lists decision log (5) before assembly (6), but the Flow diagram shows `accept → Assembly → append(...) → DecisionLog[accept]`. On the diagram's order, an `append()` raise leaves an LLM-accepted candidate with **zero** decision-log rows — a new silent-drop site on the exact root cause the plan claims to close. | **FIXED.** Technical Approach → **"Write ordering: the decision log is written before every irreversible side effect"** (new subsection with the transition table); Data Flow steps 5-7 rewritten (log-`pending` → assemble → terminal transition); Flow diagram rewritten to match, with an explicit "read the diagram against Data Flow steps 5-7" reconciliation note; Success Criterion 1 restated as "exactly one **terminal** state" plus a companion no-surviving-`pending` criterion; Task 3 gains an implement-the-two-phase-ordering bullet; Failure Path Test Strategy gains both failure branches; Verification gains a `pending_written_before_append or no_pending_survives` row. `pending` is explicitly non-terminal, so #562's four-state terminal vocabulary is unchanged. | `append()` raises `JournalBlockedError` (firewall over `agent_id`/`subject_tags`/`_never_record_scan_values()`, `provenance_journal.py:969`), `ValueError` (missing `agent_id`, empty content, backdated `at`, non-transactional pipeline), `TypeError`, `AppendOnlyViolation` — `provenance_journal.py:561-618`. Fix: write a `pending` row *before* `append()`, transition to `accept` or `assembly_failed` after; reconcile the diagram to the numbered list. |
| CONCERN | Risk & Robustness | The journal runs its **own** firewall scan over `agent_id`, `subject_tags` and all entry values — fields M3's per-candidate `scan_never_record(candidate.text)` never checks. A candidate can pass verdict `accept` then have assembly raise `JournalBlockedError`, a state the fixed four-state enum has no slot for. | **FIXED.** Technical Approach → **"Post-accept journal firewall block — mapped onto `firewall_drop`, deliberately"**: no fifth state is added; `JournalBlockedError` maps to `firewall_drop` because the semantics are identical (never-record refusal, nothing stored), and the two are distinguished by reason code — `pre_llm_candidate_block` vs `post_accept_journal_block`. Every *other* assembly failure maps to `reject`(`assembly_failed`), so `firewall_drop` never becomes a generic error bucket. Reason codes enumerated in Task 2; both branches tested per Failure Path Test Strategy. | `_scan_or_block(agent_id, *subject_tags, *entry._never_record_scan_values())` at `provenance_journal.py:969`; `Raises: JournalBlockedError ... Nothing is issued or queued` at `:610-611`. Add a fifth terminal state (`post_accept_block`) or map the exception onto an existing state explicitly in Technical Approach. |
| CONCERN | Risk & Robustness | Race 2's idempotency claim covers only the decision-log write, not assembly. `append()` takes no idempotency key, so a crash between a successful `append()` and the `DecisionLog[accept]` write lets a retry create a **duplicate journal entry**. | **FIXED — position taken: assembly owns dedup, the journal cannot.** New **Race 3: Assembly retry duplicates a journal entry** (Race Conditions), plus Technical Approach → **"Assembly idempotency"** with the four-case pre-append probe and the `turn_id`-`IndexedField` reconciliation for a surviving `pending` row (exact `verbatim` equality is sound because v1 stores the span byte-identically). Race 2 now carries an explicit scope limit saying it does *not* cover assembly. Task 3 gains the dedup-probe bullet; new Success Criterion + Verification row + interrupted-run test in Failure Path Test Strategy. | `append()` signature (`provenance_journal.py:562-577`) has no per-call idempotency key; `AppendOnlyViolation` fires only if the record's Redis key already exists, which is never true for a fresh append. Assembly must do its own dedup check keyed by `(agent_id, turn_id, candidate_id)` before calling `append()`. |
| CONCERN | Scope & Value | The candidate generator's span basis is the heuristic `_split_sentences` regex, but PR #510 measured sentence-splitting at **0.2078** judged accuracy against raw turn ingestion's **0.3636** — which is why `RawTurnExtractionProvider` is the harness default. M3 spends a Large appetite auditing the measured-worst candidate shape. | **ADDRESSED, decision unchanged (supervisor-settled: v1 = sentence spans + pattern-lifted entities).** Prior Art's #510 entry now records the measured numbers verbatim (0.2078 vs 0.3636, with the `__init__.py:159-166` citation) instead of citing #510 for the opposite conclusion. Technical Approach → **"Auditing the measured-worse candidate shape, on purpose"** argues the tension head-on in four points: (1) 0.2078 was measured blind — one aggregate over a splitter with no per-candidate record, so it cannot say *which* spans lost; (2) a raw-turn audit trail has one candidate per turn and therefore nothing to discriminate — sentence spans are the smallest shape that makes the log informative; (3) M3 does not promote the worse shape — the harness default stays `RawTurnExtractionProvider` and the path is default-off; (4) shape becomes correctable by query over `generator_rule` rather than another blind bake-off. Docs page must carry the same numbers. | `src/popoto/extraction/__init__.py:159-166` states the measurement verbatim. Either add a raw-turn candidate rule (one candidate = whole turn span) as a first-class v1 rule, or state in the plan why the audited shape should diverge from the harness's measured-best strategy. Note the plan's own Prior Art cites #510 for the opposite conclusion. |
| CONCERN | Scope & Value | The stated payoff ("precision and recall become computable offline") needs real traffic, but the flag defaults to `None` and a No-Go defers harness wiring. In v1 nothing exercises the pipeline outside tests, so the benefit is unrealized pending an unscoped follow-on. | **FIXED — both halves of the critic's "either/or" taken.** *Reframed:* Success Criterion 3 now claims a working, exercised **capability**, not realized production measurement, and names the realization step (harness wiring, already in No-Gos). *Made real:* Technical Approach → **"Offline precision/recall — what 'computable offline' concretely means here"** specifies `DecisionLog.compute_metrics(agent_id, gold_labels)` reading log rows only, demonstrated by `test_precision_recall_computable_from_log_alone` over a **multi-turn fixture transcript** asserting exact precision/recall/F1 and the per-`generator_rule` breakdown, plus a re-computation with the journal keyspace absent to pin the isolation property. Added as a Task 3 bullet, a Task 4 validation bullet, a named Verification row (replacing the flag-existence grep as the evidence for this criterion), and a runnable docs snippet so an adopter can opt in today. | The only Verification row for this surface is `grep -n "auditable_extraction" src/popoto/recipes/subconscious_memory.py` — it proves the flag exists, not that anything ever sets it non-`None`. Either scope a minimal opt-in traffic slice, or reframe Success Criteria as shipping a capability rather than a realized measurement. |
| CONCERN | History & Consistency | Risk 1's mitigation asserts "`Defaults` gains a tunable per-turn candidate cap," but no task, Technical Approach bullet, or Verification row implements or checks it. The cap also contradicts Success Criterion 1 ("every generated candidate appears in the decision log") and the "exhaustive enumeration" goal. | **FIXED — resolved by deleting the cap, not implementing it.** Risk 1's mitigation sentence is removed and replaced by an explicit **"Explicitly NOT mitigated by a candidate cap"** paragraph stating why: a capped-out candidate is a generated-then-silently-dropped candidate, i.e. the exact defect M3 exists to eliminate. Mitigation now rests on default-off alone. Task 1 gains a "**Do NOT add any per-turn candidate cap**" bullet, and Verification gains the anti-criterion `grep -rniE "candidate_(per_turn_)?cap\|MAX_CANDIDATES"` over `src/popoto/extraction/` and `fields/constants.py` so it cannot creep back in during build. | Either add `Defaults.CANDIDATE_PER_TURN_CAP` to Step 1 (`candidates.py`) plus a Verification anti-criterion mirroring the existing `grep -rni "ltrim\|DECISION_LOG_MAX"` row **and** state how capped-out candidates are logged, or delete the sentence from Risk 1 and rest the mitigation on default-off alone. |
| CONCERN | Scope & Value | `CandidateGenerator` with "pluggable rules" is an abstraction for a single use case: the plan's own No-Gos ban both plausible second rules (LLM-driven generation; multi-sentence/cross-turn windows). | **FIXED — cut as recommended.** The `CandidateGenerator` pluggable-rule class is removed from the `candidates.py` module-layout bullet in Technical Approach and from Task 1, with the reasoning recorded inline (one implementation, no second caller, both plausible extra rules banned by this plan's own No-Gos; adding a rule later is a change to one pure function). `generate_candidates(turn_id, text) -> List[Candidate]` is the whole surface. Verification gains the anti-criterion `grep -rn "class CandidateGenerator" src/popoto/extraction/` == 0. | Cut the `CandidateGenerator` class from Step 1 and the `candidates.py` module-layout bullet; the already-specified `generate_candidates(turn_id, text) -> List[Candidate]` satisfies every Success Criterion and every named test. |
| CONCERN | Structural check | Test Impact counts are stale: plan claims 26 / 31 / 93 for `test_extraction.py` / `test_never_record_firewall.py` / `test_provenance_journal.py`; actual collection at `76d649a` is **33 / 57 / 101**. Success Criterion 4 cites "the existing 26 + integration tests." | **FIXED — re-measured, not copied.** Per repo doctrine ("reproduce a subagent's metric before relaying it"), counts were re-run rather than trusting either the plan's or the critique's number: `python -m pytest <file> --collect-only -q` in the **main checkout** at `f1eb5e0`, venv `.venv` resolving `popoto` to `/Users/valorengels/src/popoto/src/popoto/__init__.py`, **redis-py 7.1.1**, Redis on `localhost:6379` → **33 / 57 / 101** (+ `test_subconscious_memory.py` **15**, `test_subconscious_memory_integration.py` **27**). The critique's figures reproduce exactly. Corrected at all four call sites (Test Impact ×3 → now ×4 with the subconscious files counted, Success Criteria ×1) and in the Technical Approach `ExtractedFact` bullet. Test Impact now opens with the measurement environment and a "re-measure before quoting" instruction. | `pytest <file> --collect-only -q` at `76d649a`. Correct all four call sites (Test Impact ×3, Success Criteria ×1) so "stays green" is verified against the real baseline. |
| CONCERN | Structural check | The assembly call is written as `ProvenanceJournal.append(kind='assert', verbatim=<span>, statement=<span>, speaker=..., turn_id=..., subjects=...)` in both Data Flow step 6 and Task 3, omitting the **required keyword-only `agent_id`**. | **FIXED.** `agent_id=<agent_id>` added to every assembly call site in the plan text: Data Flow step 6, the Flow diagram, Technical Approach → Assembly content, and Task 3 — each noting that `agent_id` is keyword-only, required, and never `None` (a `None` renders the literal `"None"` into the record's Redis key). Verification gains the anti-criterion `grep -rn "ProvenanceJournal.append(" src/popoto/ \| grep -v "agent_id"` == 0. | `append()` is `*`-keyword-only with `agent_id: str` required and no default (`provenance_journal.py:562-577`); the docstring notes a `None` "would render the literal `"None"` into the record's Redis key." Add `agent_id=self.agent_id` to both call sites in the plan text. |
| NIT | Structural check | Team Orchestration cites `DOMAIN_FRAMING.md` ("the project's salvaged domain signal") as an existing document; no such file exists anywhere in the repo. | **FIXED.** Confirmed absent (`find . -name "DOMAIN_FRAMING*"` → no results). The citation is removed from Team Orchestration → Available Agent Types and replaced by the four domain rules stated inline (Valkey-safe/no Redis modules; LLM output untrusted — enums only; magic numbers pinned in `Defaults`; Popoto model conventions), each sourced from `CLAUDE.md` rather than a nonexistent file. | Verified absent at plan-revision time; no file was created to satisfy the reference — the reference was wrong, so it was deleted. |

### Revision pass — 2026-08-25T05:16:13Z

All 10 findings addressed above; every "Addressed By" cell points at the specific section or
task that changed. The three supervisor-settled decisions were **not** reopened: v1 candidate
set stays sentence spans + pattern-lifted entities (windows behind M4), the decision log stays
**unbounded** with no cap constant and no LTRIM, and `statement` stays byte-identical to
`verbatim`. The existing constraints are likewise preserved: Valkey-safe (no Redis modules),
LLM writes only enum verdicts and enum reason codes, M2 firewall runs pre-LLM on every
candidate, and the default `extract_memories()` path is unchanged byte-for-byte behind the
opt-in flag.

Two findings were resolved by *removing* something rather than adding it — the per-turn
candidate cap (Risk 1) and the `CandidateGenerator` pluggable-rule class (Task 1) — each now
pinned by a Verification anti-criterion so build cannot reintroduce it.

### War room round 2 — 2026-08-25, baseline `6574af1`

Depth: FULL (forced by `appetite: Large`). Roster: Risk & Robustness, Scope & Value,
History & Consistency (3/3 reported, all grounded). Verdict: **READY TO BUILD (with concerns)**
— 0 blockers, 3 concerns, 2 nits.

**Round-1 fix verification:** the round-1 BLOCKER **HOLDS** (the two-phase `pending`-before-
`append()` write genuinely closes the zero-row window; Data Flow steps 5-7, the Flow diagram,
Success Criterion 1, Task 2, Task 3, the Failure Path Test Strategy and the Verification row
were read against each other literally and are mutually consistent). 8 of the 9 round-1
CONCERNs/NITs **HOLD**. One is **PARTIAL**: the assembly-idempotency fix handles the *serial*
crash-retry it was written for but not concurrent retries or duplicate-text candidates
(concerns 1 and 2 below).

**Environment for every count quoted below** (repo doctrine — reproduced, not relayed): main
checkout `/Users/valorengels/src/popoto` at `6574af1`, venv resolving `popoto` to
`src/popoto/__init__.py`, redis-py **7.1.1**, Python 3.12.13, Redis on `localhost:6379` (PONG).
The plan's Test Impact counts reproduce **exactly**: `test_extraction.py` 33,
`test_never_record_firewall.py` 57, `test_provenance_journal.py` 101,
`test_subconscious_memory.py` 15, `test_subconscious_memory_integration.py` 27.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | Risk & Robustness | **TOCTOU in the Race 3 dedup probe.** The four-case pre-append probe is read-then-act with no atomic claim. Two concurrent runs over the same `(agent_id, turn_id, candidate_id)` — a duplicated delivery racing a crash-retry, both seeing a surviving `pending` — can both probe the journal, both find nothing (neither has committed `append()` yet), and both append, producing two permanent entries. Race 3 frames its trigger as a *serial* re-run; Race 1's `MULTI`/`EXEC` covers the record+summary write, not a cross-process claim on a candidate. | **FIXED — single atomic claim op, `SET ... NX PX`.** New Technical Approach subsection **"Atomic assembly claim"** specifies the exact op: `SET popoto:m3:claim:{agent_id}:{turn_id}:{candidate_id} <uuid4 token> NX PX Defaults.M3_ASSEMBLY_CLAIM_TTL_MS` (30_000 ms, pinned in `fields/constants.py`), token-checked Lua release (`if GET == token then DEL`) executed with the terminal write, and the loser performing **no** journal write and **no** row transition. `WATCH`/`MULTI` CAS was considered and rejected in writing (needs a dedicated connection + retry loop; does not compose with Popoto's shared pool). All ops are core Redis/Valkey commands plus Lua — no modules. Threaded through: Assembly idempotency step 0, Data Flow step 5, the Flow diagram (`ATOMIC CLAIM` line with the claim-lost branch), Task 3 (with explicit "do NOT `GET`-then-`SET`, do NOT `WATCH`/`MULTI`"), Task 4, new **Race 4**, Failure Path Test Strategy (concurrent-assembly case), Success Criterion 4 companion (a), Verification rows *Atomic claim under concurrency* + two anti-criteria (`.watch(` == 0; `nx=True` present). Residual TTL-expiry window is stated explicitly and converges via the C2 identity probe rather than duplicating. | `ProvenanceJournal.append()` (`provenance_journal.py:562-620`) takes no idempotency key and `AppendOnlyViolation` fires only when a record's Redis key already exists — never true for a fresh `AutoKeyField` append — so the journal cannot catch this. The guard must live in `decision_log.py`'s pending step and be a **single atomic Redis op**, not a read followed by a write: `SET <agent_id>:<turn_id>:<candidate_id>:claiming ... NX` (short TTL), or `WATCH`/`MULTI` compare-and-set on the row's `state` field. The loser waits or no-ops. |
| CONCERN | Risk & Robustness | **Reconciliation is identity-unsound for byte-identical spans.** The probe matches on `turn_id` + exact `verbatim` equality, but `JournalEntry` carries no candidate identity. Two candidates in one turn with identical text — a repeated sentence, or a sentence span whose text equals an entity-lifted span — are indistinguishable: a surviving `pending` row can reconcile onto the *other* candidate's entry and record the wrong `entry_id`, breaking the "exactly one journal entry per accepted candidate" invariant in Success Criterion 4. The plan's "exact string equality, not a fuzzy match" argument (Technical Approach → Assembly idempotency, point 3) assumes this case away. | **FIXED — fold `candidate_id` into `subjects` (fix option 1 chosen).** New Technical Approach subsection **"Candidate identity on the journal entry"**: assembly passes `subjects=[*topic_tags, f"cand:{candidate_id}"]`, and reconciliation becomes an identity lookup `JournalEntry.query.filter(turn_id=..., subjects__all=[f"cand:{candidate_id}"])` (`TagField.__all` = `SINTER`, Valkey-safe). **Why this over the `ambiguous_reconciliation` option:** detection only flags the identity failure after the fact and leaves the candidate stuck, whereas the tag makes reconciliation correct by construction; `ambiguous_reconciliation` is kept anyway, demoted to an unreachable assertion for the >1-match branch (step 4). The old "exact equality, not fuzzy match" argument is **explicitly withdrawn** in step 3. **Settled-decision checks, all verified in code:** decision 3 untouched (`statement`/`verbatim` still byte-identical; the tag is a different field); M1 append-only untouched (tag supplied at `append()` time, no mutation, no new journal API). **M2 firewall scan surface checked at `provenance_journal.py:969` — and it constrains the format:** subject tags *are* scanned, and measured at `f6e8525` a 64-char hex digest is **blocked** (`reason='high_entropy'`) while `cand:t-41:sent:0` / `cand:t-41:ent:12` / `cand-0007` are clean, so Task 1 mandates a low-entropy `{turn_id}:{generator_rule}:{ordinal}` id (**never a digest**) with a test asserting `scan_never_record(f"cand:{candidate_id}").blocked is False`. Threaded through: Data Flow step 6, the Flow diagram, Assembly content bullet, Race 3, Task 1, Task 3 ("`cand:` tag mandatory on every M3 append"; "do NOT match on `verbatim` text"), Task 4, Failure Path (interrupted + ambiguous cases), Success Criterion 4 companion (b), Test Impact, Documentation, and three Verification rows (identity-tag anti-criterion, duplicate-text reconciliation test, firewall-unblocked test). | `JournalEntry`'s full field set is `entry_id`/`agent_id`/`captured_at`/`turn_id`/`speaker`/`verbatim`/`statement`/`subjects`/`stated`/`kind`/`target`/`validity` (`provenance_journal.py:299-310`) — no `candidate_id`-shaped field. `verbatim = StringField(default="")` (`:304`) is the only matchable content field and is **not unique per candidate within a turn** by construction, since v1 stores spans byte-identically. Fix: fold `candidate_id` into `subjects` as a tag at `append()` time so reconciliation is an identity lookup; short of that, detect the >1-match case explicitly and record a distinct `ambiguous_reconciliation` reason code rather than silently taking the first match. |
| CONCERN | History & Consistency + Structural check | **The `agent_id` anti-criterion can never pass.** Verification row `grep -rn "ProvenanceJournal.append(" src/popoto/ \| grep -v "agent_id"` expects match count == 0, but returns **2 matches today**, before any M3 code exists — `provenance_journal.py:20` and `:351`, both real `agent_id=`-passing calls that black wrapped across lines, so the single-line grep never sees `agent_id` on the same line. M3's own assembly call is long enough that black will wrap it identically, adding a third permanent false positive. The row added in round 1 to prove a round-1 fix cannot pass, by construction. | **FIXED — row replaced with a multiline-aware, M3-scoped check that was RUN at HEAD before being written down.** Confirmed the old row fails: at `f6e8525` the grep returns **2 matches** (`provenance_journal.py:20`, `:351`). Replacement: `perl -0777 -ne 'while (/ProvenanceJournal\.append\s*\(/g) { print "MISSING\n" unless substr($_, pos($_), 200) =~ /agent_id/ }' src/popoto/extraction/*.py`, expected **match count == 0** — **measured 0 at `f6e8525`**. Three controls were run to prove it is not a vacuous no-op and are recorded in a note under the Verification table: (a) the same check over `provenance_journal.py` returns **0**, correctly recognizing the two black-wrapped calls the old grep false-positived on; (b) a synthetic `append(` with no `agent_id` returns **1** (caught); (c) `append(statement=fmt(x), agent_id=a)` returns **0** (no nested-paren false positive). The 200-char-window form was chosen over a `(.*?)\)` capture *because* the non-greedy capture fails control (c). A sibling row applies the same technique to the new `cand:` identity tag. | Verified live at `6574af1`: the grep exits 0 (non-empty) with those two hits. Replace with a multiline check scoped to the **new module only**: `perl -0777 -ne 'while (/ProvenanceJournal\.append\((.*?)\)/gs) { print "$&\n" unless $1 =~ /agent_id/ }' src/popoto/extraction/*.py` — run only after Task 3 lands, against `src/popoto/extraction/`, never `src/popoto/` at large. Or drop the grep and rely on mypy plus a constructor test, which fails build on a real omission. |
| NIT | Risk & Robustness | **Stale `pending` recovery is manual and unsignalled.** "No `pending` row survives a completed `extract_memories()`" is proven by a test, but nothing enforces it operationally: a crashed turn reconciles only if something re-invokes the path for the same `(agent_id, turn_id)`. With no sweep, TTL, or age alert, a one-shot turn that is never replayed stays `pending` forever with no operator signal. | **ADDRESSED — position taken: manual for v1, with the operator query named and one small reader shipped.** New Technical Approach bullet **"Stale `pending` recovery is manual in v1"**: no sweep daemon, no age alert, and **a TTL on decision rows is explicitly rejected** (it would delete audit evidence — the one thing the module exists to keep); the correct sweep cadence is deferred to M9 (#568) on the same "don't guess the number" reasoning as retention. What ships: `DecisionLog.list_pending(agent_id, older_than=None) -> List[DecisionRecord]` (Task 3), a thin reader over existing rows with no new keyspace, plus the recovery recipe stated verbatim in the `decision_log.py` docstring and on the docs page (list oldest-first, re-invoke the auditable path per stale `(agent_id, turn_id)`, which reconciles through the identity probe). Task 4 verifies it. Periodic scanning/alerting is named as an M9-or-ops-runbook follow-on. | Note in the `decision_log.py` module docstring (already planned under Documentation → Inline Documentation) that stale-`pending` recovery is manual/opt-in in v1, and name the follow-on (M9 or an ops runbook) for a periodic age-keyed scan. |
| NIT | Scope & Value | **Per-`reason_code` breakdown has no named consumer.** `compute_metrics`'s per-`generator_rule` dimension is load-bearing (it is the mechanism the #510 argument's point 4 relies on), but the per-`reason_code` dimension is asserted in the Success Criteria and the backing test with no stated consumer — scope slightly beyond #562's AC 3, which asks only for precision/recall. | **ADDRESSED — consumer named, dimension kept.** Technical Approach → Offline precision/recall now names exactly one consumer for the per-`reason_code` dimension: **separating privacy drops from model rejects from infrastructure losses inside a single metric run.** A `firewall_drop`(`pre_llm_candidate_block`/`post_accept_journal_block`) is not an extraction-quality failure and must not be charged against the LLM's precision; a `reject`(`llm_unavailable`/`assembly_failed`) is an infrastructure loss and must not be charged against recall. Without it, a run whose recall dropped cannot distinguish "the model got worse" from "the firewall got stricter" from "Redis was flaky" — the M9 (#568) audit's first question. Task 3 requires the backing test to assert that three-way separation. | Not a build blocker and implies no code beyond Task 3 as scoped. Either name the consumer (e.g. "separates privacy drops from LLM rejects in the offline metric") or drop the dimension from the `Metrics` shape and leave it to the builder. |

### Revision pass — round 2 (concern embedding)

All 3 round-2 concerns and both nits are embedded as **implementable plan content**, not
acknowledgement prose; every "Addressed By" cell above names the sections and tasks that
changed. Summary of the mechanisms chosen:

- **C1 (TOCTOU):** the pre-append claim is a **single atomic Redis op** —
  `SET popoto:m3:claim:{agent_id}:{turn_id}:{candidate_id} <uuid4 token> NX PX
  Defaults.M3_ASSEMBLY_CLAIM_TTL_MS` — with a token-checked Lua release. The loser appends
  nothing. `WATCH`/`MULTI` CAS was considered and rejected in writing. Core commands + Lua only,
  so Valkey-safe. Pinned by Race 4, Task 3, two Verification anti-criteria, and a concurrency
  test.
- **C2 (identity):** `candidate_id` is folded into `subjects` as a `cand:{candidate_id}` tag at
  `append()` time, making reconciliation an identity lookup instead of a `verbatim` text match.
  Chosen over the detect-and-flag alternative because it is correct by construction rather than
  after the fact; `ambiguous_reconciliation` is retained as an unreachable assertion. The M2
  firewall's tag scan (`provenance_journal.py:969`) was checked and **constrains the id format**:
  digest-shaped ids are blocked as `high_entropy`, so `candidate_id` is a low-entropy
  `{turn_id}:{generator_rule}:{ordinal}` token, asserted by a test.
- **C3 (unrunnable anti-criterion):** the line-oriented grep was confirmed to return **2
  matches** at `f6e8525` and is replaced by a multiline-aware `perl -0777` window check scoped to
  `src/popoto/extraction/`, **measured at `f6e8525` returning 0**, with three controls (wrapped
  real calls → 0; synthetic omission → 1; nested-paren call → 0) recorded under the Verification
  table.
- **N1:** manual stale-`pending` recovery for v1, TTL on decision rows explicitly rejected,
  `DecisionLog.list_pending()` plus a documented operator recipe shipped, sweeping named as an
  M9/ops follow-on.
- **N2:** the per-`reason_code` dimension is kept with a named consumer (separating privacy
  drops from LLM rejects from infrastructure losses in one metric run) and a test that asserts
  the separation.

Nothing settled was reopened: v1 candidate set is unchanged (sentence spans + pattern-lifted
entities, windows behind M4); the decision log remains **unbounded** with no cap constant and no
LTRIM (the claim key's TTL is a liveness bound on an ephemeral lock holding no audit content —
decision rows themselves are never expired); `statement` remains byte-identical to `verbatim`
(the `cand:` tag lives in `subjects`, a different field). Standing constraints hold: Valkey-safe
(no modules), LLM emits enum verdicts/reason codes only, M2 firewall pre-LLM on every candidate,
default `extract_memories()` byte-for-byte unchanged behind the opt-in flag, M1 append-only
intact, and the terminal vocabulary is still exactly
`firewall_drop | accept | reject | withhold` with `pending` non-terminal.

**Explicitly upheld, not relitigated:** the three supervisor-settled decisions (v1 candidate
set; unbounded log; byte-identical `statement`). Scope & Value independently verified the
#510 argument is **honest rather than rhetorical** — `RawTurnExtractionProvider.extract()`
does return exactly one fact per turn (`extraction/__init__.py:188-204` — **corrected in the
round-3 pass** from the round-2 summary's `:216-232`, which pointed past EOF; the file is 212
lines at HEAD `d9d9127`, unchanged since `6574af1`, and the method body runs `:188-204`. The
claim itself was and is correct — only the range was wrong), so "a raw-turn audit
trail would have almost nothing to audit" is literally correct; and the harness default is
verified to remain `RawTurnExtractionProvider` (`integrations/service.py:112-125`), so the
plan does not promote the measured-worse shape. The revision was judged **not** to have
inflated scope: the two-phase write and Race 3 probe are unavoidable consequences of M1
shipping `append()` with no idempotency key.

### War room round 3 — 2026-08-25, baseline `79ffbde`

Depth: FULL (forced by `appetite: Large`). Roster: Risk & Robustness, Scope & Value,
History & Consistency (3/3 reported, all grounded). Verdict: **READY TO BUILD (with concerns)**
— 0 blockers, 2 concerns, 2 nits.

**Environment for every count quoted below** (repo doctrine — reproduced by the driver, not
relayed): main checkout `/Users/valorengels/src/popoto` at `79ffbde`, venv resolving `popoto` to
`src/popoto/__init__.py`, redis-py **7.1.1**, Redis on `localhost:6379` (PONG). Test Impact
counts reproduce **exactly** (33 / 57 / 101 / 15 / 27). All 20 Verification-table rows were
executed at HEAD: every anti-criterion returns its expected value, and the round-2 replacement
`perl -0777` `agent_id` check measures **0** over `src/popoto/extraction/*.py` while correctly
returning **0** (not a false positive) over `provenance_journal.py`, where the round-1 grep it
replaced still returns **2**.

**Round-2 fix verification — all three HOLD, checked against real APIs, not assumed:**
- **C1 (atomic claim):** `POPOTO_REDIS_DB` is a standard `redis-py` client (`redis_db.py:143`),
  so `.set(key, token, nx=True, px=...)` is a real call, and `EVAL` is already the established
  Lua pattern in this codebase (`models/query.py:445,463`; `fields/existence_filter.py:430`).
- **C2 (identity tag):** `subjects__all` resolves to `SINTER` (`tag_field.py:376,385,414`) and
  `Query.filter_for_keys_set()` intersects per-field key sets, so a single
  `.filter(turn_id=..., subjects__all=[...])` really does AND across an `IndexedField` and a
  `TagField`. `DB_key` escapes colons (`models/db_key.py:86-88`), so a multi-colon
  `cand:{turn_id}:{rule}:{ordinal}` tag value is safe as a Redis key segment.
- **C3 (anti-criterion runnability):** re-measured above; holds.
- **Two-phase write ordering** (the round-1 BLOCKER fix) reads consistently across Data Flow
  5-7, the Flow diagram, Races 3/4, Task 3, Success Criterion 1 and the Verification table.

**Both round-3 concerns land on the same component — the concurrency machinery — and point in
opposite directions.** They share one resolving question: *does a concurrent or
at-least-once-delivery caller of `extract_memories()` exist or is one committed?* Measured at
HEAD: **no.** `grep -rn "extract_memories(" src/popoto/` yields exactly one production call
site (`integrations/service.py:230`, synchronous, once per turn); `src/popoto/utils/
multithreading.py` is imported nowhere in `src/popoto/`. Answer that question once and both
concerns resolve together — the answer determines whether the claim protocol is extended
(Concern 1) or descoped (Concern 2). Answering it in opposite directions independently would be
incoherent.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | Risk & Robustness | **Only the `accept` path is claim-protected; conflicting terminal verdicts can clobber.** `firewall_drop`/`reject`/`withhold` write terminally with no claim, no CAS and no read-before-write guard. Race 2's mitigation narrows itself to "a re-write of the same key with the same terminal state is a no-op", which assumes a retried verdict call returns the *same* verdict — but the LLM verdict is non-deterministic (unlike the candidate generator), and Race 2's own trigger is precisely a retried call. A delivery resolving `reject` can overwrite the row of a candidate the `accept` path already appended to the journal, leaving the decision log (source of truth for `compute_metrics`) permanently disagreeing with the journal (source of truth for stored memories). | **ACCEPTED as a build-time implementation note — supervisor-settled.** This is the real residue of round 3 and the fix is a **guarded write plus a test**, not a redesign. New Technical Approach subsection **"Terminal-write conflict guard"** states the guard verbatim: before writing **any** of `firewall_drop`/`reject`/`withhold`, read the existing `(agent_id, turn_id, candidate_id)` row; if it is already `state == 'accept'` **with a non-empty `entry_id`**, **refuse the write** — the `accept` row and its `entry_id` stand unchanged and `detail_code = 'terminal_conflict_refused'` is recorded on that same row. No second row, no fifth state, no exception to the caller; the assembled row always wins because a journal entry physically exists for it. Implemented inside the existing per-candidate `MULTI`/`EXEC` or (preferred, smaller) by routing every terminal write through the `SET NX` claim key already built for `accept` — core commands only, Valkey-safe. Race 2's "same terminal state" assumption is **explicitly withdrawn** in its Mitigation (the LLM verdict is non-deterministic). Threaded through: Task 3 (dedicated implement-the-guard bullet with the pinning test), Task 4 validation bullet, Failure Path Test Strategy → *Conflicting terminal verdicts*, Test Impact, and a Verification row (`terminal_conflict_refused or conflicting_terminal_verdicts`). Explicitly bounded: **do NOT expand into a general terminal-state CAS redesign** — non-conflicting writes keep single-write behavior. **[MECHANISM SUPERSEDED by round-4 C1 — the `MULTI`/`EXEC` and `SET NX` shapes named above are deleted and replaced by ONE conditional Lua script run via `EVAL`. The guard's RULE is unchanged. Build from Technical Approach → Terminal-write conflict guard and Task 3a, not from this historical cell.]** | In `decision_log.py`'s writer, before writing **any** terminal state (not only `accept`), read the existing row; if it is already terminal and the new state differs, do not overwrite — record a `detail_code`-annotated conflict, or route every terminal write through the same `SET NX` claim key so the first terminal write per candidate always wins. Test: two concurrent verdict calls for one candidate resolving `accept` and `reject`; assert exactly one terminal state survives and it agrees with whether a journal entry exists. |
| CONCERN | Scope & Value | **The claim protocol is specified at command level for a trigger this codebase does not have.** Round 2 added a `SET ... NX PX` + uuid4 token protocol, a Lua release script body, `Defaults.M3_ASSEMBLY_CLAIM_TTL_MS = 30_000`, a dedicated `popoto:m3:claim:*` keyspace, Race 4, a Task 3 "do NOT `GET`-then-`SET`, do NOT `WATCH`/`MULTI`" prescription and 2 Verification rows — all to guard "two concurrent runners" / "a duplicated delivery racing a crash-retry". Measured at HEAD, no such caller exists: one synchronous `extract_memories()` call site, no queue, no worker pool, `utils/multithreading.py` unused. Issue #562 never mentions concurrency; its four ACs are about per-candidate terminal-state completeness. | **RESOLVED — supervisor-settled: KEEP the atomic claim exactly as specified. NOT descoped.** The measurement is accepted and now recorded in the plan (Technical Approach → **"Concurrency posture — measured, and settled"**): supervisor-verified at HEAD `d9d9127`, exactly **one** real `extract_memories()` call site (`integrations/service.py:230`, synchronous) and `utils/multithreading` imported **nowhere** in `src/popoto/`, so no concurrent or at-least-once caller exists today. The conclusion drawn from it is nevertheless *keep*: the protocol is already fully specified, Valkey-safe (core commands + Lua, no modules), and costs **one `SET NX` per accepted candidate** on the opt-in path — cheap insurance for the at-least-once retry that **M9 (#568)'s audit harness may introduce**, where descoping now would only mean rebuilding it. Recorded explicitly as a **deliberate acceptance of mild over-engineering against a named, expected future caller — not speculative generality**. Nothing is removed: `Defaults.M3_ASSEMBLY_CLAIM_TTL_MS`, Race 4, the Task 3 sub-protocol and both Verification rows all stay, and the claim is not optional for the builder. The `cand:` identity tag (round-2 C2) stays regardless, as the critic noted. | Verified: `grep -rn "extract_memories(" src/popoto/` → one production call site, `integrations/service.py:230`; `grep -rln "multithreading" src/popoto/` → no importers. Either name the concrete current-or-committed concurrent/redelivery caller in the plan, or label Race 4 + the atomic-claim subsection an explicit forward-looking assumption and ship Race 3's serial-retry probe alone for v1 — removing a `Defaults` constant, a Task 3 sub-protocol and 2 Verification rows without touching any AC. Note the `cand:` identity tag (C2) must stay either way: it is what makes the residual window converge. |
| NIT | Risk & Robustness | **`DecisionRecord` has no timestamp field, but `list_pending` is specified to sort by one.** The recovery recipe documents `list_pending(agent_id, older_than=None)` returning rows "oldest-first by the row's write timestamp", yet Task 3's enumerated field list (`agent_id`, `turn_id`, `candidate_id`, `state`, `reason_code`, `generator_rule`, span offsets, text hash, `entry_id`, `detail_code`) contains no timestamp — so `older_than` has nothing to compare and "oldest-first" has nothing to sort by. | **FIXED as recommended.** Task 3's `DecisionRecord` field list now carries **`written_at = FloatField(...)`**, explicitly mirroring `JournalEntry.captured_at` (`provenance_journal.py:301`, verified at HEAD `d9d9127`), stamped on **every** write — both the `pending` write and the terminal transition. Task 3's `list_pending` bullet now states that the reader filters and sorts on `written_at`, oldest-first. Task 4 gains a validation bullet, Test Impact names the test, and a Verification row (`written_at or list_pending_oldest_first`) pins it. | Add `written_at = FloatField(...)` to Task 3's `DecisionRecord` field list, set on every write (both the `pending` write and the terminal transition), and have `list_pending` filter/sort on it — mirroring `captured_at` on `JournalEntry` (`provenance_journal.py:301`). |
| NIT | History & Consistency | **Citation past end of file.** `extraction/__init__.py:216-232`, cited in the round-2 revision summary as evidence that `RawTurnExtractionProvider.extract()` returns exactly one fact per turn, points past EOF — the file is **212** lines at HEAD (unchanged since round-2 baseline `6574af1`). The method is at `:188-205`. | **FIXED — corrected after re-verifying at HEAD.** Re-measured at `d9d9127`: `wc -l src/popoto/extraction/__init__.py` → **212**; `RawTurnExtractionProvider` is declared at `:152` and its `extract()` runs **`:188-204`** (`return [ExtractedFact(text=content)]` at `:204`; `:205` is blank, `__all__` starts at `:207`). The round-2 revision summary's `:216-232` is replaced with **`:188-204`** in place, with a note that the range — not the claim — was wrong. The underlying claim is confirmed correct: `extract()` returns `[]` on empty/whitespace text and otherwise a **one-element** list. | Not build-blocking — the citation lives in the historical Critique Results log, not in Technical Approach/Tasks/Verification, so no builder-facing text is affected. The underlying claim is correct (the method returns a one-element list or `[]`); only the range is wrong. Change `:216-232` → `:188-205`. |

### Revision pass — round 3 (concern embedding) — 2026-08-26

Short, final embedding pass. **No fourth critique round** — the round-3 critic explicitly
recommended building now, and the supervisor made every call below. Nothing was reopened.

**The one measurement both round-3 concerns turned on, supervisor-verified at HEAD `d9d9127`**
(main checkout `/Users/valorengels/src/popoto`, venv resolving `popoto` to
`src/popoto/__init__.py`, redis-py 7.1.1, Redis on `localhost:6379`): there is exactly **one**
real `extract_memories()` call site — `src/popoto/integrations/service.py:230`, **synchronous** —
and `utils/multithreading` is imported **nowhere** in `src/popoto/`. **No concurrent and no
at-least-once caller exists today.** Both concerns are resolved against that single answer,
recorded in Technical Approach → "Concurrency posture — measured, and settled".

- **C1 (only `accept` is claim-protected) — ACCEPTED as a build-time implementation note.** The
  real residue. The guard: a terminal write must **not** overwrite a row that is already
  terminal `accept` with a non-empty `entry_id`; such a `firewall_drop`/`reject`/`withhold`
  write is refused and recorded as `detail_code = 'terminal_conflict_refused'` on the existing
  row. Race 2's non-deterministic-verdict assumption is withdrawn. A guarded write plus a test —
  **not** a redesign.
- **C2 (claim over-specified) — RESOLVED: KEEP the atomic claim, do not descope.** Cheap
  (one `SET NX` per accepted candidate), Valkey-safe, already fully specified, and insurance for
  the at-least-once retry M9 (#568)'s audit harness may introduce. A **deliberate acceptance of
  mild over-engineering against a named, expected future caller** — not speculative generality.
- **N3 — `written_at` added** to `DecisionRecord` (mirrors `JournalEntry.captured_at`,
  `provenance_journal.py:301`), stamped on both the `pending` write and the terminal transition;
  `list_pending` filters and sorts on it.
- **N4 — past-EOF citation fixed.** `:216-232` → **`:188-204`**, re-verified at HEAD (file is
  212 lines; the method body ends at `:204`). The claim was correct; only the range was wrong.

**All settled decisions preserved, none reopened:** v1 candidate set = sentence spans +
pattern-lifted entities (windows behind M4); decision log **unbounded** in v1 — no cap constant,
no LTRIM, tradeoff [ACCEPTED, NOT MITIGATED]; `statement` byte-identical to `verbatim`; the
low-entropy `cand:{turn_id}:{generator_rule}:{ordinal}` identity tag folded into `subjects`
(a hex digest would trip M2's `high_entropy` firewall check). **Standing constraints hold:**
Valkey-safe (no Redis modules); the LLM writes only enum verdicts and reason codes, never free
text into the store; the M2 firewall runs pre-LLM on every candidate path; the default
`SubconsciousMemory.extract_memories()` behavior is preserved byte-for-byte behind the opt-in
flag; M1 append-only intact; four TERMINAL states
(`firewall_drop | accept | reject | withhold`) with `pending` non-terminal.

**Status stays `Ready`.** Next stage: BUILD.

---

### War room round 4 (FINAL) — 2026-08-26, baseline `7405ee3`

Depth: FULL (forced by `appetite: Large`). Roster: Risk & Robustness, Scope & Value,
History & Consistency (3/3 reported, all grounded). Verdict: **READY TO BUILD (with concerns)**
— **0 blockers**, 5 concerns, 1 nit.

**This is the last critique round.** `concern_round_count` reached the
`MAX_CONCERN_RECRITIQUE_ROUNDS` bound of 3, so the concern re-critique loop latches here: the
plan proceeds to BUILD regardless of verdict, since no blocker was found. The findings below are
recorded as **build-time implementation notes for the builder**, not as another revision gate.

**Environment for every count quoted below** (repo doctrine — reproduced by the driver, not
relayed): main checkout `/Users/valorengels/src/popoto` at `7405ee3`, venv resolving `popoto` to
`src/popoto/__init__.py`, redis-py **7.1.1**, Redis on `localhost:6379` (PONG). The plan body
quotes HEAD `d9d9127`; `7405ee3` is one commit later and is the round-3 plan revision itself —
no source file changed between the two, so every `d9d9127` citation still holds verbatim.

**Round-3 embedding verification — all four landed; two are PRESENT BUT INCOHERENT:**

1. **Terminal-write conflict guard (round-3 C1) — PRESENT, PARTIALLY INCOHERENT.** The
   invariant is stated verbatim and correctly at lines 500-506, and is threaded through all six
   promised sites (Technical Approach subsection, Task 3, Task 4, Failure Path Test Strategy,
   Verification row, Race 2's withdrawn assumption). It introduces no fifth state and does not
   conflict with the two-phase `pending` write. **But** its two named implementation shapes are
   both unimplementable as written (round-4 C1), and three earlier sections still contradict it
   (round-4 C2). The guard's *intent* is unambiguous; its *mechanics* need one correction.
2. **Concurrency posture (round-3 C2) — PRESENT AND COHERENT.** Recorded in Technical Approach
   → "Concurrency posture — measured, and settled" with the environment note, and the atomic
   claim is explicitly KEPT as deliberate acceptance of mild over-engineering against M9 (#568).
   **Measurement independently re-verified at `7405ee3`:** `grep -rn "extract_memories" src/`
   yields exactly **one** invocation site, `src/popoto/integrations/service.py:230` (all other
   hits are docstrings or the definition at `recipes/subconscious_memory.py:344`);
   `grep -rn "multithreading" src/popoto/` exits **1** with no output — imported nowhere.
3. **`written_at` (round-3 N3) — PRESENT AND COHERENT.** In Task 3's `DecisionRecord` field
   list as `FloatField`, mirroring `JournalEntry.captured_at` (`provenance_journal.py:301`),
   stamped on both the `pending` write and the terminal transition, with `list_pending`
   filtering and sorting on it oldest-first, a Task 4 validation bullet and a Verification row.
4. **Citation fix (round-3 N4) — PRESENT AND CORRECT.** Re-measured: `wc -l` on
   `src/popoto/extraction/__init__.py` is **212**, and `RawTurnExtractionProvider.extract`
   spans exactly **188-204** (`return [ExtractedFact(text=content)]` at `:204`).

**Nothing regressed.** Re-verified at `7405ee3`: the round-1 blocker fix (two-phase
`pending`-before-`append`) is intact in Data Flow steps 5-7, the Write-ordering subsection and
the Flow diagram; C1's `SET ... NX PX` claim + token-checked Lua release is intact
(Technical Approach → Atomic assembly claim, Race 4, Task 3, 2 Verification rows), and `EVAL` is
confirmed as an established codebase pattern (`models/query.py:445,463`); C2's
`cand:{turn_id}:{generator_rule}:{ordinal}` tag is intact in the `subjects` argument
(Data Flow step 6, Flow diagram, Task 3). **C3's anti-criteria were RUN, not relayed:**
`perl -0777` `agent_id` check over `src/popoto/extraction/*.py` → **0**; the sibling `cand:`
check → **0**; the control over `provenance_journal.py` → **0** (correctly not a false positive);
the superseded round-1 single-line grep still returns **2**, confirming round-2 C3 was right.

**Standing constraints re-confirmed:** Valkey-safe (core commands plus Lua only, no modules —
including every shape proposed below); the LLM writes only enum verdicts and reason codes, never
free text; the M2 firewall runs pre-LLM on every candidate path; the default
`extract_memories()` behavior is preserved byte-for-byte behind the opt-in flag; M1 append-only
intact; four TERMINAL states with `pending` non-terminal.

**Addressed By (revision pass, 2026-08-26).** Although this round latched the concern
re-critique bound and no further critique follows, all six findings were folded into the plan in
one final revision pass before BUILD. The `Addressed By` column records where.

| Severity | Critic | Finding | Build-time implementation note | Addressed By |
|---|---|---|---|---|
| CONCERN | Risk & Robustness | **C1 — The terminal-write conflict guard's two named implementation shapes cannot implement its own invariant.** Lines 511-515 offer the guard as a read-then-conditional-write "inside the existing `MULTI`/`EXEC` per-candidate write" **or** by "routing the terminal write through the same `SET NX` claim key". Neither performs the stated read-then-conditional-refuse. `MULTI`/`EXEC` queues commands blind — nothing can read `state` and branch on it later in the same transaction (that needs `WATCH`, already rejected in the same section, or Lua). And the `SET NX` route only buys mutual exclusion on a key; it never inspects `state`. Worse, a candidate resolving `firewall_drop`/`reject`/`withhold` never takes a claim at all — Task 3 and the Flow diagram (lines 269-272) take it only on the `accept` path — so a retried non-`accept` writer trivially wins `SET NX` on an unclaimed key and writes unguarded, which is exactly the round-3 defect. The invariant at lines 500-506 is correct; only the mechanics are wrong. | Collapse the "or" into **one** shape: take/verify the claim for mutual exclusion **and** run a single Lua script that reads the row and conditionally writes. Companion to the already-specified release script, run via `EVAL` for **every** terminal write (not gated on whether a claim key happens to exist): `if redis.call('HGET', KEYS[1],'state')=='accept' and redis.call('HGET', KEYS[1],'entry_id')~='' then <set detail_code='terminal_conflict_refused'> else <write terminal state> end`. `EVAL` is already the established Lua pattern here (`models/query.py:445,463`; `fields/existence_filter.py:430`), so this is Valkey-safe and adds no new primitive. Delete the `MULTI`/`EXEC` and bare-`SET NX` alternatives from lines 511-515 rather than leaving them as options. | **FIXED.** Technical Approach → Terminal-write conflict guard now carries an "Implementation shape: ONE conditional Lua script, run via `EVAL`" block with the script inlined, and explicitly *deletes* both prior alternatives with the reason each fails. Task 3a's guarded-terminal-write bullet is the build instruction; Task 4 verifies the shape; a Verification row greps for the script. The guard's RULE is unchanged. |
| CONCERN | History & Consistency | **C2 — Three sections still describe non-`accept` writes as unconditional, contradicting the round-3 guard.** The guard landed late and was never back-ported into the three sections a builder reads first: Data Flow step 5 (lines 159-160) — "their first write is already their **terminal** write — one row, one state, done"; the Flow diagram (lines 267-268) — `reject`/`withhold` annotated `(TERMINAL, single write)`; Write-ordering item 1 (lines 322-323) — "One write, directly terminal." A builder implementing those literally, special-casing only `accept`, reproduces exactly the defect the guard exists to close. Task 3's own bullet (lines 1211-1220) carries the correct instruction, so the plan is self-contradictory rather than silently wrong — which is why this is a concern and not a blocker. | Amend all three statements to say the write is **conditional on a same-row conflict check against an existing terminal `accept`**, with a forward reference to → Terminal-write conflict guard. In `decision_log.py`, the writer for `firewall_drop`/`reject`/`withhold` must never be a bare `HSET` or model `.save()`; every call site — including the pre-LLM M2 `firewall_drop`, which cannot actually conflict since no prior row exists for a fresh candidate — routes through the same guarded helper. Do **not** add a "fast path" literal single write that the stale prose would suggest is still valid. | **FIXED.** All three sections reconciled: Data Flow step 5 now says the non-`accept` terminal write is guarded/conditional; the Flow diagram annotates `firewall_drop`/`reject`/`withhold` as `TERMINAL, GUARDED write` with a paragraph under it defining the term; Write-ordering item 1 now says "One write" describes the *count*, not an absence of a conflict check. Task 3a forbids a bare `HSET`/`.save()` fast path and Task 3b enumerates every call site that must route through the helper. |
| CONCERN | Risk & Robustness | **C3 — `DecisionRecord`'s keying is never specified, and the whole two-phase design rests on it.** The two-phase `pending`→terminal write, the conflict guard, and Races 3/4 all require `DecisionRecord` to be retrievable and overwritable **in place** by the exact tuple `(agent_id, turn_id, candidate_id)` across two separate transactions. Task 3 enumerates the fields but never says which are `KeyField`. The only sibling model, `JournalEntry` (`provenance_journal.py:299-310`), uses one `KeyField` plus an `AutoKeyField` — which mints a brand-new row per save, the exact opposite of what `DecisionRecord` needs. A builder pattern-matching on the sibling gets this wrong and every idempotency guarantee in the plan silently evaporates. | Task 3 must declare `agent_id`, `turn_id` and `candidate_id` **all** as `KeyField`, plus a test that saving two `DecisionRecord`s with the same tuple yields **one** Redis row, not two. Composite keys are supported and verified at HEAD: multiple `KeyField`s combine into the Redis key (`models/base.py:497-506`, `models/db_key.py:60`) and `_meta.key_field_names` is a set (`base.py:169,234-235`). Two gotchas to write down: (a) KeyFields are joined **alphabetically**, not in declaration order (`base.py:284-301`), so the key segment order is `agent_id:candidate_id:turn_id`; (b) `DB_key` escapes colons inside values (`db_key.py:86-88`), so a `candidate_id` of `t-41:sent:0` renders escaped in the key — correct, and must not be "fixed" when it looks wrong in `redis-cli`. | **FIXED — supervisor-verified at HEAD.** New Technical Approach bullet "`DecisionRecord` keying — composite `KeyField`, and NOT `AutoKeyField`" states the rule, the alphabetical-join caveat (real key: `DecisionRecord:agent_id:candidate_id:turn_id`), the colon-escaping caveat, and a one-line "do not copy `JournalEntry` here, and why". Task 3a carries it as the first build instruction with the required in-place-transition test; Task 4 verifies it; Test Impact lists the test; three Verification rows pin it (in-place test, no-`AutoKeyField` anti-criterion, three-`KeyField` count). |
| CONCERN | Scope & Value | **C4 — Task 3 has accreted roughly a dozen responsibilities across three revision rounds.** A single builder (`verdict-builder`, `Parallel: false`) now owns the `DecisionRecord` model + `written_at`, two-phase write ordering, the atomic claim with token-checked Lua release, the candidate-identity dedup probe, the terminal-write conflict guard, `list_pending`, the `ExtractedFact` extension, the `SubconsciousMemory` opt-in flag, the `ProvenanceJournal.append` wiring, and `compute_metrics` with two breakdown dimensions. Every round of hardening landed inside the task that was already largest, with no boundary between concurrency-control work and public-API-surface work. | Split into **3a. Decision log core** (`DecisionRecord` + keying + `written_at`, two-phase ordering, `list_pending`) and **3b. Assembly concurrency + wiring** (claim, dedup probe, conflict guard, opt-in flag, journal wiring, `compute_metrics`). The boundary is the one Data Flow already draws — step 5 versus steps 6-7. 3a needs only `build-candidate`/`build-verdict`; 3b adds `ProvenanceJournal.append` and sequences strictly after 3a (`Depends On: build-decision-log-core`). The same builder can own both Task IDs — this buys two validation checkpoints, not two people. | **FIXED.** Task 3 split into **3a. Decision log core** (`build-decision-log-core`) and **3b. Assembly concurrency + wiring** (`build-assembly-wiring`, `Depends On: build-decision-log-core`), both owned by `verdict-builder`, with a note recording the split's rationale. One deliberate deviation, stated in the plan: the guarded terminal-write helper lives in 3a (it *is* the log writer) rather than 3b. Task 4's `Depends On` updated to both IDs. |
| CONCERN | Scope & Value | **C5 — `detail_code` is asked to carry three structurally different payloads while being called "enum-safe".** It is specified to hold a fixed literal (`terminal_conflict_refused`), a dynamic exception class name (on `assembly_failed`, line 336), and an unbounded list of journal `entry_id`s (on `ambiguous_reconciliation`) — three shapes that cannot simultaneously be enum-safe, with no stated schema. It sits on the same row as `state` and `reason_code`, which are genuine single-value enums, so the label invites a builder to type it as one. | Either drop the "enum-safe" wording and document it as a free-form diagnostic string, or split into a true enum `detail_code` plus a separate free-text `detail`. If kept as one field, type it `StringField(default="")` — the convention `verbatim`/`statement` already use (`provenance_journal.py:304-305`) — **not** an enum type, and serialize the ambiguous-reconciliation case as `",".join(entry_ids)`. State the schema explicitly in Task 3 rather than leaving it inferred from three examples scattered across the document. This does not weaken the LLM-writes-only-enums constraint: `detail_code` is written by trusted code, never by the model. | **FIXED.** "enum-safe" wording withdrawn everywhere (Technical Approach transition table, the guard's rule statement, Task 3a). New Technical Approach bullet "`detail_code` schema" and a Task 3a bullet both specify `detail_code = StringField(default="")` — a free-form diagnostic string, not an enum — with `",".join(entry_ids)` for the ambiguous-reconciliation case. `state`/`reason_code` remain enums. |
| NIT | History & Consistency | **N1 — `AuditableExtractionConfig` is referenced but never defined.** Technical Approach → Behavior preservation types the constructor arg `auditable_extraction: Optional[AuditableExtractionConfig] = None`, but that type name appears nowhere else — not in the module-layout bullets, not in Task 3 (which says only `SubconsciousMemory(auditable_extraction=...)` with no type), not in Key Elements. | Either drop the type name and describe the flag's shape inline, or add one bullet to Task 3 naming its fields. Either way the default path must stay byte-for-byte identical when the arg is `None`. | **FIXED.** `AuditableExtractionConfig` is now defined in Technical Approach → Behavior preservation as a frozen dataclass in `decision_log.py` with exactly two v1 fields (`verdict_provider`, `journal`) and no numeric knobs, and Task 3b carries a matching build bullet. `None` remains the default and the default path stays byte-for-byte unchanged. |

**Structural checks: all PASS.** Required sections present and non-empty; Tasks 1-6 with no gaps;
all 6 Task IDs resolve with no cycles and every task has a validation target; every referenced
file path exists except the 5 intentionally-new ones (`candidates.py`, `verdict.py`,
`decision_log.py`, `tests/test_auditable_extraction.py`,
`docs/features/auditable-extraction.md`); prerequisites met (M1 and M2 import OK, `redis-cli
ping` → PONG); 9 Success Criteria all map to tasks and no No-Go or Rabbit Hole appears as planned
work; Test Impact counts reproduce **exactly** (33 / 57 / 101 / 15 / 27); all 9 runnable
anti-criteria return their expected values. One cosmetic citation drift, claim unaffected:
`subconscious_memory.py:388-397` for the M2 turn-level firewall is slightly off — the
`if Defaults.NEVER_RECORD_ENABLED:` guard is at `:397`, `write_tombstone` at `:400`, `return []`
at `:402`; the real range is ~`:390-402`.

**Concern-count trajectory: 3 → 2 → 5.** This is not deterioration. Two of the five (C1, C2) are
rough edges in the terminal-write conflict guard that **round 3 itself added**, and three (C3,
C4, C5) are lower-stakes specification gaps — model keying, task decomposition, field schema —
that earlier rounds never reached because they were occupied with the write-ordering blocker and
the concurrency protocol. No earlier round's fix regressed.

**C1, C2 and C3 are the three that must reach the builder.** Each would otherwise produce
working-looking code that reintroduces a defect a prior round already paid to find: C1 leaves the
guard unimplementable, C2 tells the builder to skip it, and C3 silently breaks the in-place row
update that every idempotency guarantee depends on. C4, C5 and N1 are quality-of-life.

**Revision pass applied 2026-08-26 — all six findings folded in; see the `Addressed By` column.**
The three builder-critical items now land where a builder cannot miss them:
1. **`DecisionRecord` keying (C3)** — Technical Approach → "`DecisionRecord` keying" bullet, the
   first build instruction in Task 3a, a Task 4 validation bullet, a listed test in Test Impact,
   and three Verification rows.
2. **The guard's Lua mechanism (C1)** — Technical Approach → Terminal-write conflict guard
   ("Implementation shape" block with the script inlined and both prior shapes deleted), Task 3a's
   guarded-terminal-write bullet, Task 3b's no-bypass bullet, Task 4, and a Verification row.
3. **The three contradicting sections (C2)** — Data Flow step 5, the Flow diagram (`GUARDED write`
   plus the paragraph defining it), and Write-ordering item 1 are now consistent with the guard.

**No settled decision and no standing constraint was disturbed by this pass.** v1 candidate set,
the unbounded decision log, byte-identical `statement`, the low-entropy `cand:` identity tag, the
`SET NX` assembly claim, and the guard's RULE all stand exactly as settled; only the guard's
implementation mechanism changed, as directed. Valkey-safety is preserved — the new mechanism is
`EVAL` plus `HGET`/`HSET`, core commands and Lua, no modules.

**Status stays `Ready`.** Next stage: BUILD.

---

## Resolved Decisions (supervisor, 2026-08-25)

The three questions raised at draft time are settled; each is folded into the sections above.

1. **Candidate span type (a) — deferral accepted.** v1 = sentence-level spans + pattern-lifted
   entities only. Bounded multi-sentence and cross-turn windows go to a follow-on module
   sequenced behind M4 reference resolution. Rationale: the v1 decision log is precisely what
   makes recall measurable, so a later window pass can be data-driven instead of speculative.
   (See Rabbit Holes and No-Gos.)
2. **Decision-log retention (b) — no cap in v1.** This reverses the draft's two-tier capped
   design: the per-candidate detail log ships unbounded, with no new `Defaults` constant and no
   LTRIM. Rationale: the correct retention horizon cannot be known until M9 (#568) consumes the
   log, so v1 declines to guess; M9 gets a full corpus and a trimming policy is designed later
   against measured growth. The accepted tradeoff (an unbounded key set in a central shared
   Redis at 20k-memory scale) and the named revisit triggers are stated in Technical Approach →
   Decision-log retention and Risk 4. M2's `NR_TOMBSTONE_LOG_MAX = 1000` is context only, not
   M3's design. The per-turn summary is retained explicitly as a convenience/query index, not
   as a completeness fallback.
3. **Assembly content (c) — `statement` stays byte-identical to the verbatim span.** No
   normalization in v1. This was settled by #562's own acceptance criteria, not a judgment
   call; distillation is M4's job. (See Technical Approach and No-Gos.)
