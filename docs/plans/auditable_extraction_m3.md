---
status: Ready
type: feature
appetite: Large
owner: Valor Engels
created: 2026-08-20
tracking: https://github.com/tomcounsell/popoto/issues/562
last_comment_id: none
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
- **PR #510** — evaluated raw vs heuristic vs Claude extraction; chose raw as harness default
  for accuracy. **Relevant:** context for why the candidate generator must handle raw whole-turn
  spans, not only sentence splits.

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
5. **Decision log** (new, `decision_log.py`): every candidate is written once, keyed by
   `(agent_id, turn_id, candidate_id)`, with terminal state + reason code; a per-turn compact
   summary is written in the same pass.
6. **Assembly** (trusted code): `accept`ed candidates are written to the M1 provenance
   journal via `ProvenanceJournal.append(kind='assert', verbatim=<span>, statement=<span>,
   speaker=..., turn_id=..., subjects=...)`. No model-generated free text enters the store;
   the stored `statement` is the verbatim candidate span (distillation is M4's job).
7. **Output**: existing behavior preserved behind a flag/adapter. With the auditable path
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
  log), distinct from the M1 journal keyspace. The journal owns accepted entries.
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
- **Decision record** — the persisted per-candidate terminal verdict: candidate identity,
  terminal state (`firewall_drop | accept | reject | withhold`), and an enum reason code.
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
  → M2 firewall (scan_never_record per span) ──blocked──> DecisionLog[firewall_drop]  (not sent to LLM)
  → Verdict stage (LLM, one candidate at a time, enum verdict + enum reason)
        accept  → Assembly → ProvenanceJournal.append(kind=assert, verbatim=span)  → DecisionLog[accept]
        reject  → DecisionLog[reject]
        withhold→ DecisionLog[withhold]
  → Per-turn compact summary written
  → SubconsciousMemory.extract_memories() returns (behind the opt-in flag)
```

### Technical Approach

- **Module layout** (all under `src/popoto/extraction/`, following the provider conventions):
  - `candidates.py` — `Candidate` dataclass (`text`, `turn_id`, `start`, `end`,
    `generator_rule`), `generate_candidates(turn_id, text) -> List[Candidate]`, and
    `CandidateGenerator` with pluggable rules. Sentence splits reuse the heuristic's
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
  existing tests (`tests/test_extraction.py`, 26 tests) pass unmodified. The auditable path
  constructs facts from accepted candidates, carrying these fields through to the decision
  log.
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
- **Enum verdicts only** — the LLM writes only `{candidate_id, verdict, reason_code}`. The
  assembly stage never reads free text from the model; accepted content is the verbatim span.
  `withhold` is a terminal logged state and never triggers an automatic user interaction.
- **Assembly content (resolves open question c): `statement` stays BYTE-IDENTICAL to the
  verbatim span.** No light deterministic normalization in v1 — not whitespace collapsing, not
  punctuation stripping, not casing changes. This is not a judgment call: issue #562's own
  acceptance criteria require that "accepted memory content is byte-identical to a verbatim
  candidate span," so any normalization would violate the AC. Distillation and normalization
  are M4's job. Concretely, assembly passes the same string object to both `verbatim=` and
  `statement=`, and a test asserts `entry.statement == entry.verbatim == candidate.text`.
- **Behavior preservation (acceptance criterion 4):** `SubconsciousMemory` gains an opt-in
  `auditable_extraction: Optional[AuditableExtractionConfig] = None` constructor arg. When
  `None` (default), `extract_memories()` runs the exact current path — provider →
  save-loop, including the M2 turn-level scan — with no change. When set, the candidate /
  verdict / decision-log / assembly path runs instead and returns the accepted set. The
  harness (`service.py`) does **not** switch by default in this plan; switching is a follow-on
  wiring decision.
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
  loud raise), never a silent `logger.warning`. Add a test for the assembly-write failure path.
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

- [ ] `tests/test_extraction.py` (26 tests) — NO CHANGE required: `ExtractedFact` new fields
  default to `None`. Re-run to confirm green.
- [ ] `tests/test_subconscious_memory.py`, `tests/test_subconscious_memory_integration.py` —
  NO CHANGE: default path behavior is preserved byte-for-byte. Re-run to confirm.
- [ ] `tests/test_never_record_firewall.py` (31 tests) — NO CHANGE: turn-level firewall
  semantics untouched. Re-run to confirm.
- [ ] `tests/test_provenance_journal.py` (93 tests) — NO CHANGE: journal API untouched; M3
  only calls `append(kind='assert')`. Re-run to confirm.
- New: `tests/test_auditable_extraction.py` — candidate enumeration determinism, per-candidate
  firewall drops, enum-verdict confinement, decision-log completeness, offline
  precision/recall computation, default-path preservation.

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
data. `Defaults` gains a tunable per-turn candidate cap (pinned in-repo, not user-facing).

### Risk 2: Backward-compat regression on the default path
**Impact:** If the flag plumbing leaks into the default path, existing
`extract_memories()` behavior changes silently for current users — a hard acceptance-criteria
violation.
**Mitigation:** The flag defaults to `None` and the default branch is the unmodified existing
code path (extractor → save loop). The existing 26 + integration tests assert byte-for-byte
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
  four terminal states, the enum-verdict contract, and the retention policy.
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

## Success Criteria

- [ ] For any input turn, every generated candidate appears in the decision log with exactly
  one terminal state (`firewall_drop | accept | reject | withhold`) and a reason code — proven
  by a test that constructs a turn, runs the auditable path, and asserts log completeness.
- [ ] The LLM contributes only enum verdicts; accepted memory content is byte-identical to a
  verbatim candidate span — proven by a test asserting no free text is persisted and the
  journal `statement`/`verbatim` equals the span.
- [ ] Precision/recall of the verdict stage is computable offline from the decision log alone
  — demonstrated in a test that seeds the log and computes precision/recall with no other input.
- [ ] Existing `SubconsciousMemory.extract_memories()` behavior is preserved behind a
  flag/adapter — proven by a default-path test asserting byte-for-byte current behavior, and
  the existing 26 + integration tests staying green.
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
  verifies candidate determinism, enum-verdict confinement, decision-log completeness,
  default-path preservation. Resume: true.
- **Documentarian** — Name: `doc-author`. Agent Type: documentarian. Role: docs page + docs
  site + inline docstrings. Resume: true.

### Available Agent Types

**Tier 1 — Core:** `builder`, `validator`, `code-reviewer`, `test-engineer`, `documentarian`.
**Domain expertise:** this is Popoto-ORM + Redis/Valkey data-modeling and an LLM call already
made by the project. Assign a `builder` (or `code-reviewer` for review-only) with a
`Domain: popoto-data-modeling` line and the matching rules from
`DOMAIN_FRAMING.md` — the project's salvaged domain signal for Redis/Valkey data modeling and
untrusted-input handling.

## Step by Step Tasks

### 1. Candidate generator
- **Task ID**: build-candidate
- **Depends On**: none
- **Validates**: tests/test_auditable_extraction.py (create), tests/test_extraction.py (unchanged, green)
- **Informed By**: recon of `HeuristicExtractionProvider._split_sentences` regex
- **Assigned To**: candidate-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `Candidate` dataclass (`text`, `turn_id`, `start`, `end`, `generator_rule`) in `src/popoto/extraction/candidates.py`.
- Add `generate_candidates(turn_id, text)` enumerating sentence spans (reuse the heuristic
  split regex) + deterministic entity-lifted candidates. Empty turns produce zero candidates.
- Add `CandidateGenerator` with pluggable rules; keep it pure and deterministic (no LLM).

### 2. Verdict stage
- **Task ID**: build-verdict
- **Depends On**: none (parallel with candidate-gen; consumes its API)
- **Validates**: tests/test_auditable_extraction.py (create)
- **Informed By**: recon of `claude.py` provider conventions and the enum-verdict contract
- **Assigned To**: verdict-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `Verdict` / `ReasonCode` enums in `src/popoto/extraction/verdict.py` with terminal states
  `firewall_drop | accept | reject | withhold` and the fixed reason-code vocabulary.
- Add `llm_verdict(candidate)` returning only `{candidate_id, verdict, reason_code}`; malformed/
  empty replies map to `reject`(llm_unavailable).
- Run `scan_never_record(candidate.text)` per candidate before the LLM; on `blocked`, log
  `firewall_drop` and do not call the LLM.

### 3. Decision log + flag + assembly
- **Task ID**: build-decision-log
- **Depends On**: build-candidate, build-verdict
- **Validates**: tests/test_auditable_extraction.py (create); existing
  test_subconscious_memory*.py stay green
- **Assigned To**: verdict-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `DecisionRecord` model + `DecisionLog` writer/reader in `src/popoto/extraction/decision_log.py`.
- Write per-candidate detail rows **unbounded** — no LTRIM, and do **not** add a
  `Defaults` cap constant for this log. Add the per-turn compact summary hash as a
  convenience/query index only; detail rows remain the sole source of truth.
- Extend `ExtractedFact` with optional span/candidate fields (default `None`).
- Add `SubconsciousMemory(auditable_extraction=...)` opt-in flag; default path unchanged.
- Add trusted assembly: `accept`ed candidates → `ProvenanceJournal.append(kind='assert',
  verbatim=<span>, statement=<span>, speaker=..., turn_id=..., subjects=...)`.
- Write the per-candidate record + per-turn summary in one `MULTI`/`EXEC`.

### 4. Validation
- **Task ID**: validate-extraction
- **Depends On**: build-candidate, build-verdict, build-decision-log
- **Assigned To**: extraction-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify candidate enumeration is deterministic and exhaustive for representative turns.
- Verify the LLM contributes only enum verdicts (no free text persisted).
- Verify every candidate has exactly one terminal state + reason code in the decision log.
- Verify offline precision/recall computes from the decision log alone.
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
| Existing extraction tests unaffected | `pytest tests/test_extraction.py -x -q` | exit code 0 |
| Existing memory tests unaffected | `pytest tests/test_subconscious_memory.py tests/test_subconscious_memory_integration.py -x -q` | exit code 0 |
| M2 firewall tests unaffected | `pytest tests/test_never_record_firewall.py -x -q` | exit code 0 |
| M1 journal tests unaffected | `pytest tests/test_provenance_journal.py -x -q` | exit code 0 |
| Type checks | `mypy src/` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| Opt-in surface present | `grep -n "auditable_extraction" src/popoto/recipes/subconscious_memory.py` | output contains `auditable_extraction` |
| No free-text verdict persists [anti-criterion] | `grep -rn "write_free\|verdict_text\|free_text" src/popoto/extraction/` | match count == 0 |
| No Redis module usage [anti-criterion] | `grep -rn "BF\.\|CMS\.\|TOPK\.\|TS\." src/popoto/extraction/` | match count == 0 |
| Decision log is uncapped [anti-criterion] | `grep -rni "ltrim\|DECISION_LOG_MAX" src/popoto/extraction/` | match count == 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| — | — | — | — | — |

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
