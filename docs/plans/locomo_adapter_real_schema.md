---
status: Complete
type: bug
appetite: Small
owner: Valor Engels
created: 2026-06-30
tracking: https://github.com/tomcounsell/popoto/issues/434
last_comment_id:
---

# LoCoMo benchmark adapter: parse the real snap-research/locomo schema

## Problem

The external-benchmark harness (`tests/benchmarks/`) ships two dataset adapters
that yield a uniform `BenchmarkItem` so Popoto Agent Memory retrieval can be
scored against published baselines. The **LoCoMo** adapter
(`tests/benchmarks/datasets/locomo.py`) was written against a *fictional* schema
— field names and shapes the author imagined — exactly the class of bug already
fixed for the LongMemEval-S sibling in PR #436. LoCoMo was never run (LongMemEval
was first), so the defect ships latent.

**Current behavior:** Two fatal bugs.

1. **Every dialogue is silently skipped → 0 items → 0.0 score.** `_parse_dialogue`
   requires `conversation` to be a `list` (`locomo.py:154-157`), but real LoCoMo's
   `conversation` is a **dict** keyed by `session_N` / `session_N_date_time` /
   `speaker_a` / `speaker_b`. The resulting `ValueError` is swallowed as a warning
   by `iter_items` (`locomo.py:268-272`), so all 10 conversations are dropped and
   a run *looks* successful while measuring nothing — the identical silent-skip
   failure mode as the LongMemEval bug.
2. **Ground truth is unrecoverable even if bug 1 is fixed.** The adapter never
   preserves `dia_id` (LoCoMo's canonical per-turn id, e.g. `"D1:11"`). It reads
   `turn_id`/`id` (`locomo.py:171`) — which don't exist — then synthesizes a
   sequential integer (`locomo.py:172`, `str(len(history))`). But `qa[].evidence`
   references real `dia_id`s, so the synthesized integer `turn_id`s in `history`
   can never intersect `relevant_ids` → scoring is 0.0 even for records that parse.

Secondary field mismatches (all `locomo.py`): reads `role` (line 174) not the real
`speaker`; `session` per-turn (line 173) which doesn't exist; `type` (line 220) not
`category`; `dialogue_id` (line 150) not `sample_id`; and drops empty-text turns
(lines 167-170) without reading `blip_caption`, making image-turn evidence targets
unreachable. The fixture (`fixtures/locomo_sample.json`) was hand-built to match
the *buggy* adapter (flat list, `turn_id`, `role: speaker1`, `evidence_turn_id`,
`type`), so its passing tests prove nothing about real-dataset behavior.

**Desired outcome:** The adapter parses the real `snap-research/locomo` dataset,
yields one `BenchmarkItem` per QA pair with non-empty `history` and `relevant_ids`
populated from `dia_id`s, and is validated by a fixture mirroring the real schema
— including a test that proves the `dia_id` intersection between `relevant_ids`
and `history` turn_ids actually holds.

## Freshness Check

**Baseline commit:** `13d21d04691d8611efcd890c9c2efe8807b4b6e0`
**Issue filed at:** 2026-06-29T08:27:08Z
**Disposition:** Unchanged

**File:line references re-verified (all still hold verbatim on current main):**
- `tests/benchmarks/datasets/locomo.py:154-157` — `if not isinstance(raw_conversation, list): raise ValueError(...)` — still holds.
- `tests/benchmarks/datasets/locomo.py:268-272` — `except ValueError ... logger.warning("Skipping malformed dialogue...") ; continue` — still holds (the silent-skip swallow).
- `tests/benchmarks/datasets/locomo.py:171-172` — `raw_turn_id = turn.get("turn_id", turn.get("id", ""))` then `str(len(history))` synthesis — still holds.
- `tests/benchmarks/datasets/locomo.py:173` — `session = turn.get("session", 1)` — still holds (always defaults to 1).
- `tests/benchmarks/datasets/locomo.py:174` — `role = turn.get("role", "user")` — still holds.
- `tests/benchmarks/datasets/locomo.py:150` — `dialogue_id = dialogue.get("dialogue_id", ...)` — still holds.
- `tests/benchmarks/datasets/locomo.py:204` — `qa.get("evidence_turn_id", qa.get("evidence", ""))` — still holds; the `evidence` fallback key already exists (handy: switch it to primary).
- `tests/benchmarks/datasets/locomo.py:220` — `"question_type": qa.get("type", "")` — still holds.
- `tests/benchmarks/datasets/fixtures/locomo_sample.json` — still the fictional flat-list / `turn_id` / `evidence_turn_id` / `role: speaker1` shape.

**Cited sibling issues/PRs re-checked:**
- #436 — **MERGED** 2026-06-29T08:58:14Z. `longmemeval_s.py` is the corrected reference shape (real parallel-array schema, `required` guard, real `relevant_ids` from `answer_session_ids`). Use it as the structural template.

**Commits on main since issue was filed (touching referenced files):**
- `a72de90` fix(#433): external benchmark Recall@k as any-hit hit-rate (#438) — touched `metrics/retrieval.py` and `test_external.py` (added `fractional_recall_at_k`/`recall_at_k`/`mean_reciprocal_rank` imports). **Did not touch `locomo.py` or the fixture.** Relevant only because it means LoCoMo now inherits the corrected any-hit metric path automatically (so once the adapter yields real `dia_id` ground truth, the `MRR ≤ Recall@k` invariant holds for free).

**Active plans in `docs/plans/` overlapping this area:** `external_recall_at_k_any_hit.md`
(#433, now Complete) fixed the shared metric; it explicitly tagged this LoCoMo work
as `[SEPARATE-SLUG #434]` and noted LoCoMo "will inherit the any-hit fix
automatically." No overlap with the adapter itself.

**Notes:** All bugs confirmed present and unmodified. The only drift since filing is
favorable — the metric path is now corrected (#438) and the sibling adapter is the
proven reference (#436).

## Prior Art

- **PR #436** (MERGED 2026-06-29) — "LongMemEval-S adapter to real dataset schema":
  fixed the *identical* class of bug on the sibling adapter. Established the
  corrected pattern: a `required` field guard, real-schema field reads, ground
  truth built from the dataset's real id fields, and a real-schema fixture with
  updated tests. **This plan applies the same recipe to LoCoMo.**
- **PR #438 / #433** (MERGED 2026-06-29) — converted `recall_at_k` to any-hit and
  added `fractional_recall_at_k`. Shared metric path; LoCoMo inherits it.
- **PR #394** — original external-benchmark harness that introduced both fictional
  adapters. Root origin of the defect.
- No prior attempt to fix the LoCoMo adapter specifically.

## Research

No new external research needed — the issue already did primary-source schema
recon (snap-research/locomo README, DeepWiki, arXiv:2402.17753) and the corrected
sibling `longmemeval_s.py` is in-repo. The real schema is documented inline below
and in the issue.

**Confirmed real LoCoMo schema** (each top-level sample):
```
{
  "sample_id": ...,
  "conversation": {                    # a DICT, not a list
    "speaker_a": "<name>",
    "speaker_b": "<name>",
    "session_1_date_time": "...",
    "session_1": [ <turn>, <turn>, ... ],
    "session_2_date_time": "...",
    "session_2": [ ... ],
    ...
  },
  "qa": [ <qa>, ... ]
}
```
Turn: `{ "speaker": "<name>", "dia_id": "D1:11", "text": "...", "img_url": <opt>, "blip_caption": <opt> }`.
QA: `{ "question": "...", "answer": "...", "category": <int 1-5>, "evidence": ["D1:11", ...] }`.
The **adversarial** category uses `adversarial_answer` instead of `answer` and has **no** `evidence`.

## Data Flow

1. **Entry point**: `iter_items(fixture_path=...)` (tests) or `iter_items()` →
   `_download_dataset()` (real run); loads a JSON **array** of samples.
2. **Per-sample parse** (`_parse_dialogue`): read `sample_id`; iterate
   `conversation` **as a dict** — select `session_N` keys sorted by integer `N`,
   skip `*_date_time` / `speaker_a` / `speaker_b`; for each turn build a history
   row keyed by `dia_id` (the `turn_id`), text from `text` or `blip_caption`,
   role from `speaker`.
3. **Per-QA emit**: one `BenchmarkItem` per `qa` entry; `relevant_ids` = set of
   `evidence` `dia_id`s (empty for adversarial); `query` = `question`; metadata
   carries `answer`/`adversarial_answer`, `category`, `sample_id`, `dataset`.
4. **Metrics** (`metrics/retrieval.py`, already corrected by #438): `recall_at_k`
   (any-hit) and `mean_reciprocal_rank` intersect retrieved turn_ids against
   `relevant_ids` (the `dia_id`s). Intersection now possible because `history`
   turn_ids ARE `dia_id`s.
5. **Output**: aggregate report (future LoCoMo run; not produced in this plan).

The fix is entirely at layer 2-3 (the adapter) plus the fixture/tests. Layer 4 is
already correct.

## Appetite

**Size:** Small

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0 (issue is crisply specified with a verified solution sketch and a merged sibling to copy).
- Review rounds: 1

This is a contained rewrite of one function + one fixture + test updates, with a
merged reference implementation to mirror.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli ping` | Test suite needs Redis (DB 15) |
| Sibling reference present | `test -f tests/benchmarks/datasets/longmemeval_s.py` | Mirror the merged #436 shape |

No dataset download is required for this work — all changes are validated by the
fixture-based unit tests in `test_external.py`. A real `locomo10.json` run is **not**
in scope (see No-Gos).

## Solution

### Key Elements

- **Dict-iterating `_parse_dialogue`**: build `history` by selecting `session_N`
  keys from the `conversation` dict, sorted by the integer `N`, skipping
  `*_date_time`, `speaker_a`, `speaker_b`. Each `session_N` value is a list of
  turns.
- **`dia_id` as `turn_id`**: every history row uses the turn's `dia_id` verbatim
  as `turn_id` (and as `session_id` source via the `D<session>:<turn>` prefix, or
  the `session_N` key). This is the single change that makes scoring non-zero.
- **`speaker` → role**: read `speaker`; map the two speaker names to
  `user`/`assistant` deterministically (e.g. `speaker_a` name → `user`,
  `speaker_b` name → `assistant`), preserving the original name in metadata or the
  row if useful.
- **Real QA fields**: `relevant_ids` from `qa[].evidence` (list of `dia_id`s);
  `category` (int) into metadata as `question_type`; answer from `answer`, falling
  back to `adversarial_answer` for the adversarial category.
- **Adversarial handling**: adversarial QAs have no `evidence` → `relevant_ids`
  is the empty set; still emit the item (one BenchmarkItem per QA), flagged in
  metadata (`category` + an `adversarial: True` marker).
- **Image-turn policy (recommended)**: ingest `blip_caption` as the turn's
  `content` when `text` is absent, so image turns referenced by `evidence` stay
  reachable. Only skip a turn when it has neither `text` nor `blip_caption`.
- **`sample_id` not `dialogue_id`**: read `sample_id` for the item-id prefix.
- **Real-schema fixture + updated tests**: replace
  `fixtures/locomo_sample.json` with a dict-shaped sample (≥2 samples, multiple
  `session_N`, at least one image turn with `blip_caption`, at least one
  adversarial QA), and update the fixture-coupled tests.

### Flow

`iter_items(fixture)` → load JSON array → for each sample: `_parse_dialogue` reads
`sample_id`, walks `conversation` dict sessions in `N` order building `history`
keyed by `dia_id` → for each `qa` emit `BenchmarkItem(relevant_ids = set(evidence))`
→ tests assert `relevant_ids ⊆ {row["turn_id"] for row in history}` for
non-adversarial items.

### Technical Approach

- Mirror `longmemeval_s.py`'s structure: keep a `required = ["conversation", "qa"]`
  guard, but **remove** the `isinstance(conversation, list)` check and replace it
  with dict iteration. Keep the `iter_items` top-level-array guard and the
  `try/except ValueError → warning` skip (it is correct error handling; the bug was
  the wrong shape check, not the swallow itself — but see Failure Path Test
  Strategy: add a test that a *genuinely* malformed sample is the only thing that
  skips).
- Session-key selection: `sorted((k for k in conversation if k.startswith("session_")
  and not k.endswith("_date_time")), key=lambda k: int(k.split("_")[1]))`.
- The existing `qa.get("evidence_turn_id", qa.get("evidence", ""))` already reads
  `evidence` as a fallback — switch `evidence` to primary and drop the fictional
  `evidence_turn_id`.
- Update the module docstring's fictional schema block to the real schema.
- Update the `note` metadata: image turns are now **ingested via blip_caption**,
  not skipped.

## Failure Path Test Strategy

### Exception Handling Coverage
- `iter_items` keeps `except ValueError → logger.warning → continue` (`locomo.py`
  ~268-272). Add a test that a sample missing a `required` field IS skipped with a
  warning (assert via `caplog`) AND that a well-formed real-schema sample is NOT
  skipped — directly guarding against the silent-skip regression that hid bug 1.

### Empty/Invalid Input Handling
- A `session_N` list with a turn that has neither `text` nor `blip_caption` → that
  turn is skipped (document and test). A QA with empty/missing `evidence`
  (adversarial) → `relevant_ids == set()`, item still emitted (test it).
- Empty `conversation` dict (no `session_*` keys) → empty `history`; the sample's
  QAs would have unreachable evidence. Decide: emit with empty history or skip.
  Recommended: emit (matches sibling), tests assert history can be empty only when
  no sessions exist.

### Error State Rendering
- No user-visible rendering. The "error state" here is the 0-items silent success;
  the caplog skip-test above is the guard.

## Test Impact

All in `tests/benchmarks/test_external.py::TestLoCoMoAdapter` (fixture-coupled):

- [ ] `test_multiple_qa_per_dialogue` — **UPDATE**: asserts exactly `6` items
  (`2 dialogues × 3 QA`) against the old fixture. Recompute for the new fixture's
  sample/QA counts.
- [ ] `test_history_shared_across_qa` — **UPDATE**: references `locomo_001`/
  `locomo_002` and slices items 0-2 / 3-5. Rewrite to the new `sample_id`s and
  per-sample QA counts.
- [ ] `test_text_only_turns` — **UPDATE/REPLACE**: currently asserts all history
  content is non-empty under the "image turns skipped" policy. Under the new
  blip_caption-ingest policy, image turns appear with caption content; keep the
  non-empty assertion but rename/reframe (turns are skipped only when both `text`
  and `blip_caption` are absent).
- [ ] `test_relevant_ids_is_set`, `test_yields_benchmark_items`,
  `test_item_id_present`, `test_query_is_string`, `test_history_is_list_of_dicts`,
  `test_metadata_has_dataset_key`, `test_limit_respected` — **UPDATE** only as
  needed for the new fixture (most pass unchanged; verify counts/keys).
- [ ] **ADD** `test_dia_id_intersection` — for every non-adversarial item,
  `item.relevant_ids` is non-empty AND `item.relevant_ids ⊆
  {row["turn_id"] for row in item.history}`. This is the test the old fixture
  could not express and the core proof of the fix.
- [ ] **ADD** `test_adversarial_item_empty_evidence` — adversarial QA yields an
  item with `relevant_ids == set()` and an answer sourced from `adversarial_answer`.
- [ ] **ADD** `test_malformed_sample_skipped_with_warning` (caplog) and an
  implicit assertion that valid samples are NOT skipped.

## Rabbit Holes

- **Downloading and parsing the real `locomo10.json`.** Out of scope — validation
  is fixture-based, matching how the sibling was fixed. A real LoCoMo benchmark run
  + report is a separate effort.
- **Over-modeling speaker identity.** LoCoMo has named speakers, not user/assistant.
  Map them deterministically to the two role slots and move on; don't build a
  general N-speaker model.
- **Reworking the metric path.** Already corrected by #438 — do not touch
  `metrics/retrieval.py`.
- **Reconstructing session timestamps / `session_summary` / `event_summary`.** Not
  needed for retrieval scoring; ignore those keys.

## Risks

### Risk 1: Real-dataset field shape differs subtly from the documented schema
**Impact:** Adapter parses the fixture but mis-parses real `locomo10.json` (e.g.
`dia_id` casing, evidence as ints).
**Mitigation:** Build the fixture directly from the issue's primary-source schema;
keep reads defensive (`str(dia_id)`, coerce evidence members to `str`). A real run
is deferred (No-Go), so any residual mismatch surfaces in that separate effort, not
this PR.

### Risk 2: Image-turn policy choice (ingest vs. skip) is wrong for scoring
**Impact:** If evidence points at image turns and we skip them, those targets are
unreachable (recall capped < 1). If we ingest junk captions, history is noisier.
**Mitigation:** Recommended policy is ingest `blip_caption` (issue's
recommendation) so evidence `dia_id`s stay reachable; the `test_dia_id_intersection`
test enforces reachability. (See Open Question 1.)

## Race Conditions

No race conditions identified — the adapter is pure, synchronous file parsing with
no shared state or concurrency.

## No-Gos (Out of Scope)

- [EXTERNAL] A real `locomo10.json` benchmark run and committed LoCoMo report
  artifact — requires downloading the full snap-research/locomo dataset from
  HuggingFace onto the build machine, which is not assumed present here. This plan
  fixes the adapter and validates it with a real-schema fixture only; producing the
  actual benchmark numbers is a separate effort (the LoCoMo analogue of the
  report-regeneration step that #433 did for LongMemEval).
- The metric functions in `metrics/retrieval.py` — already fixed by #438; untouched
  here.

## Update System

No update system changes required — this is a test-harness-internal adapter fix.

## Agent Integration

No agent integration required — the LoCoMo adapter is a benchmark dataset loader,
not an agent-facing capability.

## Documentation

### Feature Documentation
- [ ] No `docs/features/` entry — bug fix to an existing adapter, not a new feature.

### External Documentation Site
- [ ] None — adapters are not part of the published docs site. Confirm with
  `grep -rn -i "locomo" docs/` and update only if a page cites the fictional schema.

### Inline Documentation
- [ ] Replace the fictional schema block in `locomo.py`'s `_parse_dialogue`
  docstring with the real dict-based schema.
- [ ] Update the module-level docstring's "image turns are skipped" note to reflect
  blip_caption ingestion.

## Success Criteria

- [ ] `_parse_dialogue` iterates `conversation` as a dict (`session_N` keys sorted
  by `N`, skipping `*_date_time`/`speaker_a`/`speaker_b`); the
  `isinstance(conversation, list)` check is gone.
- [ ] Each history row's `turn_id` is the turn's `dia_id`; no synthesized integer
  turn_ids remain.
- [ ] The new real-schema fixture yields items with **non-empty `history`** and
  **`relevant_ids` populated from `dia_id`s** for non-adversarial QAs.
- [ ] `test_dia_id_intersection` passes: `relevant_ids ⊆ history turn_ids` for every
  non-adversarial item (the intersection the scorer needs actually holds).
- [ ] Adversarial QAs emit items with `relevant_ids == set()` and answer from
  `adversarial_answer`; covered by a test.
- [ ] Image turns are ingested via `blip_caption` (reachable evidence), not dropped.
- [ ] `sample_id` (not `dialogue_id`), `speaker` (not `role`), `category` (not
  `type`), `evidence` (not `evidence_turn_id`) are all read.
- [ ] Fixture replaced; all `TestLoCoMoAdapter` tests updated and passing; a caplog
  test proves malformed-only skipping (no silent 0-item success).
- [ ] The `MRR ≤ Recall@k` invariant holds on the shared metric path (inherited
  from #438) when scoring fixture items — sanity-checked in a test.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

Small fix — solo builder + validator, mirroring the merged #436 work.

### Team Members

- **Builder (locomo-adapter)**
  - Name: locomo-builder
  - Role: Rewrite `_parse_dialogue` for the real dict schema, replace the fixture, update + add tests, refresh docstrings.
  - Agent Type: builder
  - Resume: true

- **Validator (locomo-adapter)**
  - Name: locomo-validator
  - Role: Verify dia_id intersection, adversarial handling, blip_caption ingestion, caplog skip-test, and the MRR ≤ Recall sanity check.
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Rewrite the LoCoMo adapter for the real schema
- **Task ID**: build-adapter
- **Depends On**: none
- **Validates**: tests/benchmarks/test_external.py::TestLoCoMoAdapter
- **Informed By**: PR #436 (`tests/benchmarks/datasets/longmemeval_s.py` corrected shape)
- **Assigned To**: locomo-builder
- **Agent Type**: builder
- **Parallel**: false
- In `tests/benchmarks/datasets/locomo.py`, replace the `isinstance(conversation, list)` branch with dict iteration: select `session_N` keys (skip `*_date_time`, `speaker_a`, `speaker_b`), sorted by `int(N)`.
- Build `history` rows with `turn_id = dia_id`, `content = text or blip_caption` (skip only when both absent), `role` mapped deterministically from `speaker`, `session_id` from the `session_N` key or the `D<session>:` prefix.
- Read `sample_id` for the item-id prefix; per QA, set `relevant_ids = {str(e) for e in qa.get("evidence", [])}`, `question_type = qa.get("category")`, answer from `answer` or `adversarial_answer`; emit one BenchmarkItem per QA (adversarial → empty relevant_ids, `adversarial: True` in metadata).
- Update the module + `_parse_dialogue` docstrings to the real schema; update the image-turn `note`.

### 2. Replace the fixture with a real-schema sample
- **Task ID**: build-fixture
- **Depends On**: none
- **Validates**: tests/benchmarks/test_external.py::TestLoCoMoAdapter
- **Assigned To**: locomo-builder
- **Agent Type**: builder
- **Parallel**: true
- Rewrite `tests/benchmarks/datasets/fixtures/locomo_sample.json` as a JSON array of ≥2 samples, each with `sample_id`, a dict `conversation` (`speaker_a`/`speaker_b`, ≥2 `session_N` + matching `session_N_date_time`, turns with `speaker`/`dia_id`/`text`), at least one image turn (`img_url` + `blip_caption`, no `text`) whose `dia_id` is referenced by some evidence, and a `qa` list mixing normal QAs (`question`/`answer`/`category`/`evidence`) with at least one adversarial QA (`adversarial_answer`, no `evidence`).

### 3. Update and add tests
- **Task ID**: build-tests
- **Depends On**: build-adapter, build-fixture
- **Validates**: tests/benchmarks/test_external.py
- **Assigned To**: locomo-builder
- **Agent Type**: builder
- **Parallel**: false
- Update fixture-coupled tests (`test_multiple_qa_per_dialogue`, `test_history_shared_across_qa`, `test_text_only_turns`) to the new fixture's counts/ids/policy.
- Add `test_dia_id_intersection`, `test_adversarial_item_empty_evidence`, `test_malformed_sample_skipped_with_warning` (caplog), and a `MRR ≤ recall_at_k` sanity test over fixture items using the existing `mean_reciprocal_rank`/`recall_at_k` imports.

### 4. Validation
- **Task ID**: validate-all
- **Depends On**: build-adapter, build-fixture, build-tests
- **Assigned To**: locomo-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/benchmarks/test_external.py -q` and confirm all `TestLoCoMoAdapter` cases pass.
- Verify success criteria: dia_id intersection holds, adversarial items have empty relevant_ids, no synthesized integer turn_ids, no `isinstance(conversation, list)` check remains.
- Run `black --check` on the touched files.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| LoCoMo adapter tests pass | `pytest tests/benchmarks/test_external.py::TestLoCoMoAdapter -q` | exit code 0 |
| Conversation iterated as dict (list-check removed) | `grep -n "conversation. must be a list" tests/benchmarks/datasets/locomo.py` | exit code 1 |
| dia_id used as turn_id | `grep -n "dia_id" tests/benchmarks/datasets/locomo.py` | exit code 0 |
| No synthesized integer turn_id | `grep -n "str(len(history))" tests/benchmarks/datasets/locomo.py` | exit code 1 |
| Real QA evidence key read | `grep -n "\"evidence\"\|'evidence'\|.get(\"evidence\"" tests/benchmarks/datasets/locomo.py` | exit code 0 |
| Fictional evidence_turn_id gone | `grep -n "evidence_turn_id" tests/benchmarks/datasets/locomo.py` | exit code 1 |
| sample_id read | `grep -n "sample_id" tests/benchmarks/datasets/locomo.py` | exit code 0 |
| dia_id intersection test present | `grep -n "def test_dia_id_intersection" tests/benchmarks/test_external.py` | exit code 0 |
| Fixture is real-schema (dict conversation) | `grep -n "session_1\|sample_id" tests/benchmarks/datasets/fixtures/locomo_sample.json` | exit code 0 |
| Fixture no longer fictional | `grep -n "evidence_turn_id\|dialogue_id" tests/benchmarks/datasets/fixtures/locomo_sample.json` | exit code 1 |
| Format clean | `black --check tests/benchmarks/datasets/locomo.py tests/benchmarks/test_external.py` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

_Resolved 2026-06-30 (orchestrator, recommended defaults accepted):_

1. **Image-turn policy — RESOLVED: ingest `blip_caption`.** Image turns are ingested
   via `blip_caption` so evidence `dia_id`s stay reachable; `test_dia_id_intersection`
   enforces reachability.
2. **Speaker→role mapping — RESOLVED: `speaker_a`→`user`, `speaker_b`→`assistant`**,
   original name preserved in metadata.

Both were confirmatory; neither blocked the build. Plan is Ready.
