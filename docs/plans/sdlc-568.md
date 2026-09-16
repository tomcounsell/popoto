---
status: Ready
type: feature
appetite: Medium
owner: valorengels
created: 2026-09-16
tracking: https://github.com/tomcounsell/popoto/issues/568
---

# M9 — Seeded audit harness: planted facts and planted secrets validate the write path (#568)

## Problem

The write path now has two safety properties that nothing verifies continuously:

1. **Memory-worthy content is not silently dropped.** M3 (#562) made
   under-generation *measurable* — every candidate ends in one of four terminal
   states in a queryable decision log — but nothing *measures* it. The
   deterministic generator (`src/popoto/extraction/candidates.py`) is two
   regexes: a sentence splitter and a capitalised-run entity lifter. A regex
   edit that quietly stops producing candidates for a whole class of sentence
   would not fail a single existing test, because every existing test asserts
   on text it also authored.
2. **Secrets never persist.** M2 (#561) is a pattern list —
   `_OFF_THE_RECORD`, `_CREDENTIAL_PREFIX`, `_JWT`, `_CREDENTIAL_ASSIGNMENT`,
   `_URL_USERINFO`, `_CARD_CANDIDATE`, `_SSN`, plus a Shannon-entropy scan.
   Pattern lists rot: credential formats drift, prefixes change, and the entropy
   thresholds (`Defaults.NR_ENTROPY_MIN_BITS`, `NR_ENTROPY_MIN_TOKEN_LEN`) are
   pinned magic numbers tuned once. Nothing detects the rot between manual
   reviews, and `tests/test_never_record_firewall.py` asserts on the same
   fixtures the patterns were written against.

**Current behavior:** extraction quality is evaluated manually (#489).
`tests/benchmarks/extraction_axis.py` is the nearest precedent and is
deliberately *aggregate* — it counts facts per turn across ingest arms; it has
no per-item ground truth, so it cannot say *which* item was lost.

**Desired outcome:** a harness that plants items with known ground truth into
synthetic turns, drives the real write path, and joins the manifest against the
artifacts M2 and M3 actually produce — asserting that every planted
memory-worthy item reached the decision log, and that every planted secret
terminated as a content-free tombstone with zero trace anywhere in the keyspace.

## Freshness Check

The issue was written before M1–M6 landed and its dependency statement
("Depends on: M2 #561 and M3 #562") is now an understatement. Verified against
`origin/main` at `5955d141` on 2026-09-16:

| Module | Issue | State | What this harness can see |
|---|---|---|---|
| M1 provenance journal | #560 | CLOSED | `src/popoto/recipes/provenance_journal.py`; `JournalEntry(AppendOnlyMixin, NeverRecordMixin, EventStreamMixin, Model)` |
| M2 never-record firewall | #561 | CLOSED | `src/popoto/privacy/never_record.py`; `scan_never_record`, `NeverRecordMixin`, `write_tombstone` |
| M3 auditable extraction | #562 | CLOSED | `src/popoto/extraction/{candidates,verdict,decision_log}.py` |
| M4 reference resolution | #563 | CLOSED | `src/popoto/extraction/{resolution,resolution_log}.py` |
| M5 reconciliation | #564 | CLOSED | `src/popoto/recipes/reconciliation.py` |
| M6 belief-sheet resolver | #565 | CLOSED | `src/popoto/recipes/view_resolver.py`; `BeliefSheetResolver`, `resolve_entries` |

Four facts from that recon change the design away from the issue's sketch:

1. **The join key already exists and is not `candidate_id`.**
   `DecisionRecord` carries `text_hash` (`decision_log.py:187`) and the module
   exports `hash_candidate_text(text)` (`:295`). The manifest joins on that,
   not on `candidate_id` — which is generator-derived and therefore changes
   under exactly the generator regressions this harness exists to catch.
2. **`DecisionLog.compute_metrics(agent_id, gold_labels)` already exists**
   (`:1038`), returning a `Metrics` dataclass (`:1116`). It takes a
   `Dict[str, bool]` of gold labels — which is precisely a planted-item
   manifest. The harness supplies ground truth to an existing scorer rather
   than writing a second one.
3. **The firewall fires at three distinct points, not one.**
   `Model.save()` (`models/base.py:1399-1407`, gated on
   `Defaults.NEVER_RECORD_ENABLED`), per-candidate before the LLM call
   (`verdict.py`, which imports `scan_never_record` at module top), and
   turn-level in `SubconsciousMemory._log_turn_firewall_block`
   (`subconscious_memory.py:601`). A planted secret has three legitimate
   termination shapes, and the manifest must say which one it expects or the
   assertion is vacuous.
4. **Terminal `ACCEPT` requires a live model.** `llm_verdict` is the only
   producer of `Verdict.ACCEPT`, and `LLM_VERDICTS` deliberately excludes
   `FIREWALL_DROP` and `PENDING`. CI has no model. This is the issue's own
   open question, and it forces the split below: the harness asserts
   *generation* by default and *acceptance* only under an opt-in flag.

## Prior Art

- `tests/benchmarks/extraction_axis.py` — aggregate extraction measurement; the
  precedent this extends with per-item ground truth.
- PR #444 — CSR deterministic memory-recall eval harness; the "seeded corpus,
  deterministic assertions, no model" shape.
- `docs/plans/forget_guard_test_vacuity.md` — the repo's own precedent that a
  guard test which cannot fail is worse than no test. Every assertion in this
  harness ships with a paired anti-vacuity control.
- `tests/test_type_checking_guard.py` — the precedent for asserting source
  *shape* with `ast` when the property is not observable from behavior. Reused
  here for the no-secret-in-output rule.
- `docs/plans/auditable_extraction_m3.md`, `docs/plans/never_record_firewall.md`
  — the two artifacts this harness reads.

## Data Flow

```
manifest (PlantedItem[])          ground truth, authored in-repo
   │
   ├─ synthesize_turns() ────────► synthetic turn text (items embedded verbatim)
   │                                   │
   │                                   ▼
   │                        SubconsciousMemory(auditable_extraction=...)
   │                                   │
   │            ┌──────────────────────┼──────────────────────┐
   │            ▼                      ▼                      ▼
   │   turn-level firewall     generate_candidates()   NeverRecordMixin
   │   ($NR tombstone)          │                      on JournalEntry.save()
   │                            ▼                       ($NR tombstone)
   │                    per-candidate firewall
   │                            │
   │                    ┌───────┴────────┐
   │                    ▼                ▼
   │              FIREWALL_DROP    stub/live verdict
   │                    │                │
   │                    └───► DecisionLog rows ◄──┘
   │                                   │
   └── assertions ─────────────────────┤
          A: text_hash join ───────────┘   every memory-worthy item has a row
          B: $NR counts + drops list       every secret has a content-free tombstone
          C: full-keyspace fragment scan   zero occurrences, anywhere
```

## Architectural Impact

**None on `src/`.** This module is dev-only; its blast radius is the test tree,
`scripts/ci-local.sh`, and one docs page. It adds no import to `src/`, no
`Defaults` constant, and no public API.

The one place it touches a private seam is `SubconsciousMemory._verdict_for`
(`subconscious_memory.py:635`), which the harness overrides in a subclass to
supply deterministic verdicts without a model. That is a read-only coupling to
a private name and is recorded as a risk below, with a question for the
architect about promoting it to a public config field.

## Appetite

**Size:** Medium.

**Team:** Solo dev, code reviewer. No PM check-in required unless Q1 or Q3 is
answered in a way that changes scope.

**Interactions:** review rounds: 1–2. The code is a manifest, a generator, a
keyspace scanner and a test module — none of it hard. The cost is in the
anti-vacuity controls, which are where this class of harness usually fails
review, and in the discipline of never letting a planted secret reach stdout.

## Prerequisites

| Requirement | Check Command | Purpose |
|---|---|---|
| M2 + M3 present | `python -c "import popoto.privacy.never_record, popoto.extraction.decision_log"` | The two artifact producers this harness reads. |
| Redis/Valkey reachable | `redis-cli -n 15 PING` | The suite runs under the DB-15 isolation plugin. |
| Editable install resolves to this checkout | `scripts/ci-local.sh` prints it | Worktree gotcha #1 in CLAUDE.md. |
| Dev extras installed | `pip install -e '.[dev,embeddings,benchmark,mcp]'` | Worktree gotcha #2; otherwise ~95 tests deselect. |

No API key, no network, no optional model provider is required for the default
path. `anthropic` is **not** a prerequisite: `verdict.py` keeps
`anthropic_module` bound to `None` when absent and the harness never reaches
`llm_verdict` unless Q1's opt-in flag is set.

## Solution

### Key Elements

1. **`tests/benchmarks/seeded_audit.py`** — the harness library. Owns the
   manifest type, the corpus, the turn synthesizer, and the keyspace scanner.
   Importable, so a future module can reuse the scanner.
2. **`tests/benchmarks/test_seeded_audit.py`** — the pytest module. Owns the
   assertions and, one-for-one, their anti-vacuity controls.
3. **`tests/benchmarks/README.md`** — gains a section stating the coverage
   boundary in the issue's own words: a seeded audit covers only failure modes
   someone thought to plant; a green run bounds known patterns and does not
   prove the absence of novel ones.
4. **`scripts/ci-local.sh`** — a named `audit` gate. CI needs no change:
   `testpaths = ["tests"]` already collects `tests/benchmarks/`, and
   `.github/workflows/tests.yml` runs `pytest -m "not slow"` on both the Redis
   and Valkey jobs — so the harness runs on Valkey too, for free, which is
   where its Valkey-safety constraint actually gets exercised.

### Flow

```python
@dataclass(frozen=True)
class PlantedItem:
    item_id: str                  # the ONLY thing an assertion message prints
    text: str                     # exactly one sentence, verbatim in the turn
    kind: Literal["memory_worthy", "secret", "control"]
    expected_state: str | None    # a Verdict value, or None for turn-level block
    expected_detector: str | None # a NEVER_RECORD_REASONS member, for secrets
    fragments: tuple[str, ...]    # substrings that must never appear in Redis
```

Assertion A (no silent under-generation): for each `memory_worthy` item, a
`DecisionRecord` exists for its turn whose `text_hash ==
hash_candidate_text(item.text)`. Built as a set-difference of `item_id`s, so a
failure message names the missing items and never their text.

Assertion B (secrets terminate as content-free tombstones): for each `secret`
item, (i) the expected termination shape occurred — a `FIREWALL_DROP` row, or a
turn-level block, per `expected_state`; (ii)
`NeverRecordMixin.never_record_counts()` shows the expected reason incremented;
(iii) every entry in the `$NR:{class}:drops` list parses to exactly
`{id, reason, detector, at}` and each planted fragment is absent from the
serialized entry.

Assertion C (zero trace in the keyspace): a full `SCAN` of the bound database,
reading each key by `TYPE` and searching the raw bytes of both key name and
value for every planted fragment. Zero hits required.

### Technical Approach

**Valkey-safe scanning, no modules.** `SCAN` with a bounded `COUNT`, then per
`TYPE`: `string`→`GET`, `hash`→`HGETALL`, `list`→`LRANGE 0 -1`, `set`→
`SMEMBERS`, `zset`→`ZRANGE 0 -1`, `stream`→`XRANGE - +`. An unknown type is a
hard error, not a skip — a silently-skipped type is the exact hole a scanner
like this rots through. Connection via `get_REDIS_DB()`, never a
`POPOTO_REDIS_DB` import (CLAUDE.md).

**The scanner needs a positive control or it is vacuous.** Model instances are
msgpack-encoded; a raw byte search finds a UTF-8 string embedded in msgpack,
but would return a false *negative* against any encoding the scanner cannot
see through. So the manifest carries a third kind, `control`: a benign
high-entropy sentinel written through the same normal write path, which
assertion C must **find**. If the sentinel is not found, the scanner is broken
and the whole absence result is meaningless — the test fails loudly on that
rather than passing on an empty search.

**No planted secret ever reaches output.** Three mechanisms, because one is not
enough under `--tb=long`:
- Every assertion compares sets of `item_id` or integer counts. No assertion
  expression contains a secret string, so pytest's assertion rewriting has
  nothing to print.
- The scanner returns `(item_id, key_name_redacted, offset)` triples, never the
  matched bytes.
- An `ast`-based test (the `test_type_checking_guard.py` shape) asserts that no
  `assert` statement in `seeded_audit.py` or `test_seeded_audit.py` references
  the `.text` or `.fragments` attribute of a `PlantedItem`. This is a source
  *shape* assertion because the property is not observable from a passing run.

**Deterministic verdicts without a model.** The harness subclasses
`SubconsciousMemory` and overrides `_verdict_for(candidate)` to return a
`VerdictResult` derived from the manifest. The firewall still runs for real at
all three points — only the LLM stage is stubbed, and the stub is the thing
under no test here. Default path asserts *generation* (a row exists), not
*acceptance*. See Q1.

**Secret seeds cover the format variants the issue names,** each mapping to a
named detector so a detector that stops firing is attributable:
`_CREDENTIAL_PREFIX` (known prefix), `_JWT`, `_CREDENTIAL_ASSIGNMENT`
(`key = value` in prose), `_URL_USERINFO`, `_CARD_CANDIDATE` (Luhn-valid),
`_SSN`, `_OFF_THE_RECORD` (marked-up and prose forms), and the entropy scan
(high-entropy token with *no* known prefix — the guaranteed-class edge). Split
across whitespace is included as an item **expected to be missed** where the
detectors genuinely cannot see it; it is recorded as a known-gap item with
`expected_state` reflecting reality, not aspiration. See Q4.

## Failure Path Test Strategy

Every assertion ships with a control that proves it can fail:

| Assertion | Anti-vacuity control |
|---|---|
| A — generation | Monkeypatch `generate_candidates` to drop every other candidate; assert A fails. |
| B — tombstone | Monkeypatch `_REGEX_DETECTORS` to exclude one detector; assert B fails for exactly the items that detector owns. |
| C — absence | The `control` sentinel must be **found**; assert the scanner reports it, and that a scanner over an empty DB reports nothing. |
| No-secret-in-output | The `ast` test is itself checked against a fixture source string containing a violating `assert`. |

Exception handling: `write_tombstone` swallows Redis errors by design
(fail-closed on the drop, best-effort on the audit log). The harness must not
paper over that — if the tombstone is missing, assertion B fails; it does not
retry. Empty/invalid input: a turn synthesized to empty text exercises
`_log_empty_turn` and must produce a logged empty-turn record, not a silent
no-op.

## Test Impact

New: `tests/benchmarks/test_seeded_audit.py`. No existing test changes. The
module is not marked `slow` — it must run in the CI default selection or it
does not deliver continuous verification, which is its whole point. Runtime
budget: the keyspace scan is the only non-trivial cost and is bounded by the
scan cap (see Risks).

## Rabbit Holes

- **Making the seeds statistically indistinguishable from natural turns.** The
  issue's recon already dropped this. Deterministic manifest assertions deliver
  the regression value; realism delivers nothing measurable here.
- **Duplicating retrieval-quality measurement.** SIQ, CSR, LoCoMo and
  LongMemEval already own that axis. This harness asserts on the *write* path
  only.
- **Scoring the LLM's judgment.** #489 owns extraction quality. This harness
  measures whether the deterministic parts of the pipeline lost something, not
  whether a model judged well.
- **Fixing detector gaps found while writing the seeds.** Any real gap is filed
  as its own issue and its manifest item records the true expected state. A
  plan that both finds and fixes firewall holes is two plans.

## Risks

**R1 — The keyspace scan sees other lanes' data.** DB 15 is shared by every
worktree (CLAUDE.md worktree gotcha #4). Mitigation: fragments are unique
high-entropy tokens fixed in the manifest; no other suite can write them, so
cross-contamination cannot cause a false failure. A false *pass* is impossible
for the same reason. Cost, not correctness, is the exposure — mitigated by a
hard cap on keys scanned, with the cap being a test failure when exceeded
rather than a silent truncation.

**R2 — `_verdict_for` is private and may be renamed.** The override breaks
loudly (AttributeError / never called) rather than silently, because the
harness asserts its stub was invoked at least once per turn. Q2 asks whether to
promote the seam.

**R3 — The firewall's three call sites make "which one fired" ambiguous.**
Mitigated by `expected_state` on each item and by asserting the *count* of
tombstones, not merely their presence: a secret caught twice (candidate stage
and save stage) is a different fact from one caught once, and a manifest that
does not pin it would pass either way.

**R4 — A green run over-read as "no secrets can leak".** Mitigated by the
README boundary statement being a Success Criterion, not a nicety.

**R5 — Assertion A becomes tautological if a planted item is not a whole
sentence.** `generate_candidates` splits on `(?<=[.!?])\s+`; an item spanning
two sentences never matches a single `text_hash`. Mitigated by a harness
self-check that every `memory_worthy` item is exactly one sentence under that
same regex — asserted at manifest load, so authoring a bad item fails fast
instead of quietly weakening the suite.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG] Fixing any firewall detector gap this harness discovers.
- [SEPARATE-SLUG #489] LLM-vs-heuristic extraction quality evaluation.
- [ORDERED] Adding the harness to the *default* `scripts/ci-local.sh` gate set.
  It ships as a named gate and under `--all`; changing the default five is a
  developer-workflow call, not a build-time one.
- Any change to `src/`. If the harness cannot be written without one, that is a
  finding to report, not a licence to edit the library from a test plan.
- Any new `Defaults` constant. Harness tuning knobs live in the harness module.
  If one is nonetheless added, it must be registered in
  `tests/benchmarks/overrides.py`'s `MODULE_CONSTANTS` or
  `tests/benchmarks/test_defaults_sync.py` fails — and it fails only in CI,
  after review has already approved.

## Update System

No update-system changes. No dependency, no config file, no migration.

## Agent Integration

None. The harness is not reachable from any agent or tool surface and does not
change one.

## Documentation

- `tests/benchmarks/README.md` — new section: what the harness plants, what it
  asserts, how to add a seed, and the coverage-boundary statement.
- `docs/features/` — a short subsection appended to the existing never-record /
  auditable-extraction page rather than a new page: this is a verification
  mechanism for shipped features, not a feature of its own.
- Issue comment on #568 recording the manifest's known-gap items (the seeds
  that are expected to be *missed*), since those are the honest edge of the
  firewall's guarantee and belong in the record.

## Success Criteria

- [ ] Every `memory_worthy` manifest item is asserted present in the M3
      decision log by `text_hash` join; the set-difference is empty.
- [ ] A deliberately-broken `generate_candidates` makes assertion A fail (control
      test present and itself asserted).
- [ ] Every `secret` manifest item is asserted absent from **all** keys in the
      bound database and present as a content-free `$NR:` tombstone with the
      expected `detector`.
- [ ] A deliberately-weakened detector makes assertion B fail for exactly the
      items that detector owns, and for no others.
- [ ] The `control` sentinel is **found** by the keyspace scanner — the scan's
      positive control, without which assertion C is vacuous.
- [ ] Every `memory_worthy` item is exactly one sentence under
      `candidates._SENTENCE_SPLIT`, asserted at manifest load.
- [ ] No planted secret string appears in test output on a failing run:
      verified by an `ast` test asserting no `assert` in either module
      references `PlantedItem.text` or `.fragments`, and by a manual
      `pytest --tb=long` run against a deliberately failing control.
- [ ] The scanner handles all six Redis types and treats an unknown type as a
      hard error; uses only `SCAN`/`TYPE`/`GET`/`HGETALL`/`LRANGE`/`SMEMBERS`/
      `ZRANGE`/`XRANGE` — no Redis modules (Valkey doctrine).
- [ ] The suite runs under the DB-15 isolation plugin, binds via
      `get_REDIS_DB()`, and passes on both the Redis and Valkey CI jobs.
- [ ] `scripts/ci-local.sh audit` runs the module; `--all` includes it.
- [ ] `tests/benchmarks/README.md` states the known-patterns-only boundary.
- [ ] No file under `src/` is modified; no `Defaults` constant is added.
- [ ] `ruff check src/`, `black --check src/ tests/`, `mkdocs build --strict`,
      `scripts/mypy_ratchet.py` pass.

## Step by Step Tasks

### 1. Manifest and corpus — `tests/benchmarks/seeded_audit.py`
Define `PlantedItem`, `MANIFEST`, and `synthesize_turns()`. Include the
one-sentence self-check at load. Include the `control` sentinel. Seeds cover
every detector in `_REGEX_DETECTORS` plus the entropy scan, one item per
detector minimum, named by `expected_detector`.

### 2. Keyspace scanner — same module
`scan_for_fragments(fragments) -> list[Hit]`, six types plus hard-error
default, bounded scan cap, `get_REDIS_DB()` binding, redacted return shape.

### 3. Deterministic driver — same module
`AuditSubconsciousMemory(SubconsciousMemory)` overriding `_verdict_for`, and a
`run_audit(manifest)` that drives `extract_memories()` over the synthesized
turns with `AuditableExtractionConfig(journal=ProvenanceJournal)` and returns
the decision-log rows, tombstone state, and scan hits.

### 4. Assertions — `tests/benchmarks/test_seeded_audit.py`
A, B, C as above, each as its own test, each comparing `item_id` sets.

### 5. Anti-vacuity controls — same module
One per assertion, per the Failure Path table. These are not optional and are
the first thing review should read.

### 6. Output-safety `ast` test — same module
Assert no `assert` statement in either module reaches `.text`/`.fragments`.
Check the checker against a violating fixture source.

### 7. Wire the local gate — `scripts/ci-local.sh`
Add `audit` to the accepted-arg case, to `--all`, and a `gate_audit()` running
`$PYTEST tests/benchmarks/test_seeded_audit.py`. Do **not** add it to the
default five.

### 8. README and docs
Boundary statement; how to add a seed; the known-gap item list.

### 9. Verification
Run the suite; run it with each control's monkeypatch inverted to confirm the
controls fail when they should; run `pytest --tb=long` on a forced failure and
confirm no secret in output.

## Verification

| Claim | How it is verified |
|---|---|
| No silent under-generation | Assertion A green; control test red when generator broken |
| Secrets never persist | Assertion B + C green; control sentinel found; detector control red when weakened |
| No secret in output | `ast` test green; manual `--tb=long` failing run inspected |
| Valkey-safe | Passes the Valkey CI job, which runs the same `pytest -m "not slow"` |
| Honest scope | README boundary paragraph present, cited from the issue |

## Questions for the architect

1. **Do LLM-verdict assertions run by default, behind a flag, or not at all?**
   This is the issue's own stated open question and it is the one real design
   fork. `Verdict.ACCEPT` is producible only by `llm_verdict`, and CI has no
   model. **The plan proceeds on:** default path stubs `_verdict_for`
   deterministically and asserts *generation* only; a live-model acceptance
   assertion is gated behind an opt-in env flag and never runs in CI. If you
   want acceptance asserted continuously, that needs a model in CI and a cost
   decision, and the plan changes.

2. **Should `_verdict_for` be promoted to a public seam?** The harness
   overriding a private method is a real coupling. Options: leave it (cheapest,
   breaks loudly), add a `verdict_fn` field to `AuditableExtractionConfig`
   (small `src/` change, violates this plan's own no-`src/` No-Go), or expose a
   documented test hook. **The plan proceeds on:** leave it private and
   override, with R2's loud-failure guard. Your call whether the coupling is
   acceptable.

3. **Where does the harness live — `tests/benchmarks/` or `tests/`?** It is not
   a benchmark; it produces a pass/fail, not a number. But it sits beside
   `extraction_axis.py`, its closest kin, and `tests/benchmarks/` is already
   collected by `testpaths`. **The plan proceeds on:** `tests/benchmarks/`.
   Moving it to `tests/` is a one-line change if you prefer the honest naming.

4. **How should a seed the firewall genuinely cannot catch be recorded?** The
   issue asks for seeds "split across whitespace" — some of those the detectors
   provably miss. Recording them with an accurate `expected_state` makes the
   suite green and documents a real hole; omitting them hides it; recording
   them as expected-catch makes the suite permanently red. **The plan proceeds
   on:** record accurately as known-gap items, list them in the README and in
   an issue comment, and file any that look fixable as separate issues. Confirm
   that a green suite documenting known holes is what you want.

5. **Is the shared-DB-15 full-keyspace scan acceptable?** It is correct (R1)
   but it reads every key other lanes wrote. The alternative is a dedicated
   audit DB, which contradicts the issue's "runs under the DB-15 isolation
   plugin" and which the plugin binds before collection so a fixture cannot set
   it. **The plan proceeds on:** DB 15 with a hard scan cap. Say so if you want
   a dedicated database instead — that is an `addopts`/workflow change, not a
   harness change.

6. **Should the harness also assert arrival in the M1 journal, or stop at the
   M3 log?** The issue scopes it to the decision log, written before M1–M6 all
   landed. Asserting journal arrival would catch a whole extra failure class
   (accepted but never assembled) at the cost of coupling to M1's model.
   **The plan proceeds on:** the issue's scope — M3 log only — with journal
   arrival noted as a natural follow-up.
