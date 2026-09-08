---
status: Planning
type: chore
appetite: Medium
owner: valorengels
created: 2026-09-08
tracking: https://github.com/tomcounsell/popoto/issues/586
last_comment_id:
---

# LongMemEval-S n=500 three-arm supersession/validity run (#586)

## Problem

PR #582 (issue #580) shipped Wave A of the validity axis — `ValidityField`,
`SupersessionProtocol`, three-layer assembler gating — **unbenchmarked**. The PM
deferred the LongMemEval-S gate on 2026-08-17 so #582 could merge. #586 tracks
that debt.

The debt was then re-blocked twice, for a reason that is worth stating precisely
because it constrains what this plan may claim:

1. **2026-09-07** — declaring `ValidityField` on the benchmark model is *not*
   sufficient. `ValidityField.on_save` routes through
   `execute_supersede(..., mode="open")`, which `ZADD NX`s `valid_from =
   save_time` and `invalid_at = +inf`. `ValidityField.resolve_excluded_keys`
   excludes a member only when `invalid_at <= now` or `valid_from > now`; the
   `+inf` open sentinel never satisfies the first and a save-time `valid_from`
   never satisfies the second. Save-only ingestion therefore produces an
   **empty exclusion set for all 500 questions**, and "before" and "after"
   would be byte-identical *by construction*.
2. **2026-09-08** — #692 (PR #702) landed the missing piece: a label-blind
   content-identity supersession **producer**
   (`tests/benchmarks/supersession_axis.py`), wired into `run_external.py` as
   `--supersession {none,content-identity}` plus `--no-validity-gating`. Only a
   small-n, fixture-based demonstration was run. The real corpus run is still
   owed, and #692's own write-up says so explicitly.

**Current behavior:** the repo publishes an n=500 LongMemEval-S recall baseline
(`tests/benchmarks/results/external/longmemeval_s_latest.md`, run 2026-06-30:
Recall@1 0.8560 / @5 0.9520 / @10 0.9780, MRR 0.8987) that says nothing about
validity gating, and a validity axis that has never been measured at corpus
scale. Everything shipped about `ValidityField` is asserted by unit tests and a
4-item fixture.

**Desired outcome:** a committed, environment-stamped, three-arm n=500
LongMemEval-S measurement — A (baseline, no `ValidityField`), B (producer runs,
gate off), C (producer runs, gate on) — published with its per-category
breakdown, its exclusion-set operational statistics, and an explicit statement
of what the delta does and does not establish. Published whatever the sign.

### Reconciling the issue's two-arm framing with #692's three-arm design

#586's acceptance criterion 2 asks for a *"before/after"* on the
knowledge-update and temporal-reasoning categories. **That framing is superseded
and this plan does not honor it literally.** A two-arm comparison (committed
baseline vs. producer+gate) confounds two changes shipped together — the
producer's writes and the gate's subtraction — so its delta cannot be attributed
to the gate. `tests/benchmarks/README.md`'s "External-harness supersession axis
(#692)" section mandates three arms:

- **A → B** isolates everything the change does *other than gate* (the extra
  per-save Lua command, the field declaration, the producer's write ordering).
  On a recall metric it should be ~0. **A non-zero A→B is a finding about the
  harness, not about validity, and must be reported before C is read at all.**
- **B → C** is the only pair that isolates the gate.

Mapping to the issue's criteria:

| #586 criterion | Disposition in this plan |
|---|---|
| 1. Declare `ValidityField` on the benchmark model | Satisfied by arms B and C (`--supersession content-identity` declares it); arm A deliberately does not. |
| 2. Run before/after on knowledge-update and temporal-reasoning vs. the committed n=500 baseline | **Revised to three arms.** All 500 questions are run (all six categories); knowledge-update and temporal-reasoning are the *reported focus* via the per-category breakdown the harness already emits. The committed 2026-06-30 baseline is **not** used as the "before" — see Risk 1. |
| 3. Report per metric-family doctrine | Satisfied: all three arms are recall-family, compared only to each other. `--judged` is forbidden. |
| 4. Flat/negative delta is a finding to publish | Satisfied and binding. See No-Gos. |

## Freshness Check

**Baseline commit:** `24e8f8cd` (`git rev-parse HEAD` at plan time; the last
code commit is `22c2320f`, PR #702 for #692)
**Issue filed at:** 2026-08-17T04:54:31Z
**Disposition:** **Minor drift + Overlap**

**File:line references re-verified:**

- `src/popoto/fields/validity_field.py:920-931` (from the 2026-09-07 issue
  comment) — claimed: `resolve_excluded_keys` excludes only on `invalid_at <=
  now` or `valid_from > now`, and the `+inf` open sentinel never matches.
  **Still holds**, drifted to `validity_field.py:915-931` on `24e8f8cd`; the
  source comment *"The +inf open sentinel never matches"* is intact at :922.
- `tests/benchmarks/scenarios/external_base.py:479,522` (claimed: the harness's
  only writes are `.save()`) — **superseded by #702**: the arm-aware write path
  now routes through `SupersessionProtocol.save_and_supersede` when the
  supersession arm is active. This is the change that unblocks the issue.
- `tests/benchmarks/results/external/longmemeval_s_latest.md` — **still holds**;
  symlink resolves to `longmemeval_s_20260630.md`, Recall@1 0.8560 / @5 0.9520 /
  @10 0.9780, MRR 0.8987, knowledge-update n=78, temporal-reasoning n=133.

**Cited sibling issues/PRs re-checked:**

- **#560** — CLOSED 2026-08-19. Shipped `ProvenanceJournal` as a primitive; it
  was never wired into the external harness, so it did **not** unblock this
  issue (the 2026-09-07 re-block).
- **#564** (M5) — still OPEN. Not a prerequisite any more.
- **#692** — CLOSED 2026-09-08 via PR #702. **This is the real unblocker.**
- **#701** — OPEN. External-harness `ValidityField` shared-key-namespace defect.
  See spike-1: does not block.
- **#693** — OPEN. Whether save-only inertness is the right library default.
  Explicitly untouched by this work.
- **#580 / PR #582** — CLOSED/merged 2026-08-17, the origin of the debt.

**Commits on main since the issue was filed (touching referenced files):**

- `22c2320f` bench(#692): supersession producer (PR #702) — **changed the
  premise**: the producer this issue was blocked on now exists. Drove the
  two-arm → three-arm reconciliation above.
- `c046e1bd` refactor(#655): call-time Redis client resolution — irrelevant to
  the measurement, but it is why `validity_field.py` line numbers moved.
- `542c11c0` (#494 tombstones), `1d50bd83` (#648), `3cf8c2d0` (#563),
  `90fc3d30` (#588 SUPERSEDE_LUA membership) — all landed **after** the
  committed 2026-06-30 baseline was measured. `90fc3d30` in particular changed
  supersession membership semantics. This is the direct evidence for Risk 1:
  the 2026-06-30 artifact is not a valid contemporaneous "before".

**Active plans in `docs/plans/` overlapping this area:**

- `docs/plans/sdlc-701.md` — **live lane, being planned concurrently.** #701 is
  the shared-`db_class_key` defect in the same harness factory this plan
  exercises. Coordination, not a blocker: spike-1 establishes containment, and
  this plan's only harness edits (`_STALE_KEY_PATTERNS`, teardown logging,
  `machine` block) do not touch `_build_external_model_class`. **If #701 merges
  first, re-run is unnecessary — its fix is namespace-only and cannot change a
  contained result; if this lane merges first, #701 rebases over three additive
  edits.**
- `docs/plans/sdlc-692.md` — the producer's own plan; the authority for the
  three-arm design and the "what it does not establish" text.

**Notes:** No `pytest.xfail` markers relate to this work (`grep -rn
'pytest.mark.xfail\|pytest.xfail(' tests/` returns nothing tied to validity or
the external harness). Not a bug fix; nothing to reproduce.

## Prior Art

- **#692 / PR #702** — *"LongMemEval-S harness has no supersession producer"*.
  **Succeeded, 2026-09-08.** Shipped `tests/benchmarks/supersession_axis.py`,
  the `--supersession` / `--no-validity-gating` flags, artifact-name suffixing
  (`_sup-{arm}[_nogate]`), the `supersession` report block, and the three-arm
  doctrine in `tests/benchmarks/README.md`. **This plan runs what #702 built and
  adds nothing to the measurement device itself.**
- **#580 / PR #582** — V0 validity primitives. **Succeeded but shipped the
  benchmark debt this issue is.**
- **#560 / PR #589** — provenance journal. **Succeeded as a primitive, failed as
  an unblocker**: never wired into the external harness, so #586's precondition
  was satisfied on paper and false in fact.
- **#588 / PR #601** — moved supersession membership into `SUPERSEDE_LUA`.
  Relevant because it post-dates the committed baseline.
- **#484 graph-traversal evaluation** — the closest *methodological* precedent:
  a multi-arm sub-study whose artifacts live in a subdirectory
  (`tests/benchmarks/results/external/graph_eval_484/`) and are cited from a
  hand-authored `docs/benchmarks.md` section rather than published as generated
  result pages. **This plan copies that shape** (see Risk 2 / Solution).
- **#489 extraction axis** — precedent for an orthogonal ingest axis with
  suffixed artifact names, and for publishing a negative result (LLM extraction
  losing to raw ingestion).

### Why previous fixes failed

| Prior attempt | What it did | Why it failed / was incomplete |
|---|---|---|
| #580 / PR #582 | Shipped the validity gate | Shipped with the benchmark gate deferred; no producer existed, so the gate could not be measured at all. |
| Declaring `ValidityField` on the benchmark model (the issue's criterion 1, attempted 2026-09-07) | Would populate `valid_from`/`invalid_at` | Save-only writes leave `invalid_at = +inf`, so the exclusion set is empty for every question and the "after" equals the "before" by construction. |
| Treating #560's close as the unblock | Trusted the issue's stated precondition | #560 shipped a primitive that the harness never calls; `grep` over `external_base.py`/`run_external.py` returned zero matches. |

**Root cause pattern:** every prior attempt measured the *declaration* of the
gate rather than the *firing* of it. The fix is not a better metric; it is an
explicit producer arm plus a third arm that separates producer from gate — which
#702 supplies and this plan consumes.

## Research

**Skipped by the skill's own rule** — this work introduces no external library,
API, or ecosystem pattern. It runs an in-repo harness against a corpus already
cached on disk, in the repo's default `lexical` retrieval mode, with no network
dependency and no provider SDK.

No relevant external findings — proceeding with codebase context. The one
"external" fact that mattered (the LongMemEval-S corpus itself) was verified
locally rather than researched: see spike-2.

## Spike Results

<!-- skeleton -->

## Data Flow

<!-- skeleton -->

## Architectural Impact

<!-- skeleton -->

## Appetite

<!-- skeleton -->

## Prerequisites

<!-- skeleton -->

## Solution

<!-- skeleton -->

## Failure Path Test Strategy

<!-- skeleton -->

## Test Impact

<!-- skeleton -->

## Rabbit Holes

<!-- skeleton -->

## Risks

<!-- skeleton -->

## Race Conditions

<!-- skeleton -->

## No-Gos (Out of Scope)

<!-- skeleton -->

## Update System

<!-- skeleton -->

## Agent Integration

<!-- skeleton -->

## Documentation

<!-- skeleton -->

## Success Criteria

<!-- skeleton -->

## Team Orchestration

<!-- skeleton -->

## Step by Step Tasks

<!-- skeleton -->

## Verification

<!-- skeleton -->

## Critique Results

<!-- Populated by /do-plan-critique. -->

---

## Open Questions

<!-- skeleton -->
