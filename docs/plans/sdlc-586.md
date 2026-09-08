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

All spikes ran at plan time on the lane's isolated database.

**Spike environment (stated per repo doctrine):** Python 3.12.14, redis-py
**7.1.1**, macOS-26.6.2-arm64-arm-64bit (Darwin 25.6.0), Redis bench DB 9
(`POPOTO_BENCH_DB=9`), repo commit `24e8f8cd`. Note the redis-py version: it is
the `uv.lock`-resolved 7.1.1, not the 8.1.0 used for the 2026-09-07 issue probe.

### spike-1: Does #701 block the real n=500 run?

- **Assumption**: "The shared `$ValidityF:ExternalBenchmarkMemory:validity:*`
  key namespace (#701) contaminates results across items at n=500."
- **Method**: code-read (read-only subagent over `external_base.py`,
  `run_external.py`, `supersession_axis.py`, `base.py`, `test_external.py`).
- **Finding**: **DOES NOT BLOCK.**
  - `Scenario.execute()` (`tests/benchmarks/scenarios/base.py:95-116`) wraps
    `setup`/`run` in `try/except Exception` + **`finally: self.teardown()`**, so
    teardown runs on success, on error, and on `KeyboardInterrupt`. A raising
    item becomes a `status="error"` result and the driver loop
    (`run_external.py:1396-1436`) continues.
  - Teardown (`external_base.py:919-964`), guarded on `"validity" in
    self._model_class._meta.fields`, deletes the five fixed keys from
    `ValidityField.get_all_keys()` (`validity_field.py:782-796`) **plus** a
    `SCAN {prefix}:open:*` sweep — i.e. all six key shapes the field documents
    (`validity_field.py:38-42`). Item N+1 therefore reads empty ZSETs.
  - Items are strictly sequential and synchronous; no ingest write for item N+1
    can precede item N's `DEL`. Containment is O(1) in keys per item and does
    not degrade with item count.
  - Asserted by `tests/benchmarks/test_external.py:1204-1224`
    (`test_no_leaked_validity_keys_after_teardown`) and the anti-vacuity control
    at `:1226-1249` (`test_teardown_on_arm_none_is_real_noop`).
- **Two residual caveats the spike surfaced** (neither blocks; both are cheap to
  close and are folded into this plan's tasks):
  1. `_STALE_KEY_PATTERNS` (`run_external.py:112-116`) does **not** include
     `$ValidityF:*`, so neither the startup nor the exit sweep removes validity
     keys. A previously SIGKILLed `content-identity` run leaves ZSETs that
     **item 1 of the next run can see** (items 2..500 are clean because item 1's
     own teardown clears them). → **task build-1**.
  2. The validity cleanup is wrapped in a bare `except Exception: pass`
     (`external_base.py:962-963`), so a failed `DEL` leaks silently with no log
     line — the one path where #701 becomes observable mid-run. → **task
     build-2**.
- **Confidence**: high.
- **Impact on plan**: #701 is **not** a blocker and this plan does not wait on
  it; the two caveats become two small pre-run hardening tasks.

### spike-2: Is the n=500 run actually feasible in this environment?

- **Assumption**: "The run needs the external corpus, an embedding provider, and
  hours of wall clock" — the issue's own stated reason for deferral, restated in
  the dispatch as a possible `[EXTERNAL]` gate.
- **Method**: prototype (real runs of `run_external.py` against the real corpus
  at `--limit 5` and `--limit 25`, `--dry-run`, `POPOTO_BENCH_DB=9`).
- **Finding**: **the premise is stale on all three counts. The run is fully
  runnable in this environment and is NOT an `[EXTERNAL]` gate.**
  - **Corpus**: cached on disk at
    `~/.cache/popoto_benchmarks/longmemeval_s_cleaned.json` (277,383,467 bytes,
    2026-06-29). The harness logs *"using cached file"*; no download, no
    network.
  - **Embedding provider**: not required. `--retrieval-mode` defaults to
    `lexical` (BM25 only, "needs no model download",
    `run_external.py:1077-1093`), which is the mode the committed n=500 baseline
    was produced in. Only `hybrid`/`vector` need the ~90MB all-MiniLM-L6-v2
    download, and this plan uses neither. No `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`
    either — those are `--judged` and `--extraction claude`, both excluded.
  - **Wall clock**: measured, not estimated.

    | Run | Items | Wall clock (`/usr/bin/time -p real`) | Harness-reported | s/item |
    |---|---|---|---|---|
    | `--limit 5 --supersession content-identity --dry-run` | 5 | 16.36 s | 14.2 s | 2.84 |
    | `--limit 25 --supersession content-identity --dry-run` | 25 | 74.87 s | ~72 s | 2.88 |

    Linear in items (2.84 → 2.88 s/item across a 5× range), with a fixed ~2 s
    corpus-load cost. **n=500 projects to ≈ 24 minutes per arm, ≈ 75 minutes for
    all three arms** — not "hours", and comfortably inside one build session.
- **Secondary finding — the gate is live and non-vacuous at scale.** The n=25
  arm-C run reported `identity writes 281 (groups=62
  units_with_identity=281/12222)`, `supersessions 193`, `excluded keys/hits: 193
  / 11`, `producer failures 0`. Eleven excluded *hits* means the gate actually
  subtracted records the retriever would otherwise have returned — the exact
  condition whose absence re-blocked this issue on 2026-09-07. (The n=5 run
  showed `32 / 0`: keys excluded, no hits. So hits scale with n and a small
  sample is not evidence of an inert gate.)
- **Confidence**: high (measured on the real corpus, not the fixture).
- **Impact on plan**: removes the `[EXTERNAL]` framing entirely; the run is an
  in-scope build task. Also sets the Risk-4 time budget.

### spike-3: Where can arm artifacts land without damaging the published baseline?

- **Assumption**: "Committing three arms' artifacts is harmless."
- **Method**: code-read (`run_external.py:1021-1034`,
  `docs/scripts/gen_benchmark_pages.py`, `mkdocs.yml`) plus inspection of the
  committed results tree.
- **Finding**: **not harmless by default — two distinct hazards.**
  1. **Clobber.** `run_external.py:1021-1034` rewrites
     `{dataset_slug}_latest{suffix}.{json,md}` after every run. Arm A
     (`--supersession none`) has an **empty suffix**, so a plain arm-A run
     repoints `longmemeval_s_latest` away from `longmemeval_s_20260630.*` — the
     artifact `docs/scripts/gen_benchmark_pages.py:120` publishes as the
     headline recall page on the docs site. Arms B and C are safe from this
     (`_sup-content-identity[_nogate]` suffix), but arm A is not.
  2. **Orphan warnings.** `_warn_orphan_artifacts`
     (`gen_benchmark_pages.py:555-600`) globs `external/*_latest*.md` and prints
     a build-time WARNING for any stem no `Spec` publishes. Committing
     `longmemeval_s_latest_sup-content-identity.md` and
     `..._sup-content-identity_nogate.md` at top level would emit two warnings
     on every docs deploy. It warns rather than raises, so `mkdocs build
     --strict` stays green — but the log noise is real and self-inflicted.
  - **Both are avoided by one decision**: `--output
    tests/benchmarks/results/external/validity_586/`. The glob is
    **non-recursive**, so subdirectory artifacts produce no orphan warnings, and
    a subdirectory `longmemeval_s_latest.*` cannot touch the canonical one. This
    is exactly the precedent set by
    `tests/benchmarks/results/external/graph_eval_484/` (#484), whose numbers
    are cited from a hand-authored `docs/benchmarks.md` section.
- **Confidence**: high.
- **Impact on plan**: `--output` to a `validity_586/` subdirectory is
  **mandatory on all three arms**, and is an anti-criterion in Verification.

### spike-4: Does the report record enough environment to satisfy repo doctrine?

- **Assumption**: "The harness already stamps every number with its
  environment", as `tests/benchmarks/README.md` claims for this axis (*"Every
  number produced under this axis carries Python version, redis-py version,
  platform, Redis DB, and the baseline commit SHA"*).
- **Method**: code-read (`run_external.py:597-601`) + inspection of the
  committed baseline JSON.
- **Finding**: **the claim is false today.** The `machine` block is exactly
  `{python_version, platform, cpu_count}`. **redis-py version is absent, the
  resolved bench DB is absent, and the commit SHA is absent** — confirmed
  against `longmemeval_s_20260630.json`, whose `machine` block is
  `{"python_version": "3.12.13", "platform": "macOS-26.3.1-arm64-arm-64bit",
  "cpu_count": 10}`. CLAUDE.md's rule (*"state the environment alongside any
  count"*) and the redis-py-version-dependent-metric rule both bite here: a
  committed artifact that does not name its redis-py version cannot be compared
  to a later one.
- **Confidence**: high.
- **Impact on plan**: adding `redis_version` and `bench_db` to the `machine`
  block is a **prerequisite of the run**, not a nice-to-have — the artifacts
  this plan commits are the first ones the README's promise is measured against.
  → **task build-3**.

## Data Flow

Per arm, per item (500 items, strictly sequential):

1. **Entry point**: `python -m tests.benchmarks.run_external --dataset
   longmemeval-s --supersession {none|content-identity} [--no-validity-gating]
   --output tests/benchmarks/results/external/validity_586/`.
2. **DB bind**: `_select_bench_db()` (`run_external.py:201-223`) resolves
   `POPOTO_BENCH_DB` (DB 0 rejected), repoints the Popoto connection, and sweeps
   `_STALE_KEY_PATTERNS`.
3. **Corpus load**: `LongMemEvalS` adapter reads the cached
   `longmemeval_s_cleaned.json` and yields items under `sample=stride seed=0`.
4. **Per item — setup**: `_build_external_model_class(safe_prefix, ...,
   with_validity=<arm != none>)` builds an `ExtMem<hash>` model class.
   *(#701: with validity on, the field's Redis keys resolve under the pre-rename
   `ExternalBenchmarkMemory` namespace regardless of `safe_prefix` — contained
   by teardown, see spike-1.)*
5. **Per item — ingest**: each conversational turn becomes one record. Arm A
   calls `.save()`. Arms B/C route through
   `supersession_axis.identity_of(unit_text)` (one positional, text-only
   parameter — structurally label-blind) and, for identity-bearing units,
   `SupersessionProtocol.save_and_supersede`, which closes any prior claim
   sharing the identity key (`invalid_at` set to a finite score).
6. **Per item — retrieve**: the question is issued through `ContextAssembler`.
   In arm C, `_resolve_excluded_keys` → `ValidityField.resolve_excluded_keys`
   subtracts closed/not-yet-started members via two `ZRANGEBYSCORE` reads. In
   arm B, `Defaults.VALIDITY_GATING_ENABLED = False` skips the subtraction —
   **identical stored state, different read path**. In arm A the field does not
   exist.
7. **Per item — teardown**: `finally: self.teardown()` deletes the item's
   `ExtMem*` keys, the five fixed validity keys, and the `open:*` pointers.
8. **Aggregate**: Recall@1/5/10 + MRR overall and `by_question_type`, latency
   percentiles, and the `supersession` block (`identity_writes`,
   `identity_groups`, `n_supersessions`, `n_excluded_keys_total`,
   `n_excluded_hits_total`, `producer_failures`).
9. **Output**: `{dataset}_{date}[_sup-{arm}][_nogate].{json,md}` plus
   `_latest*` pointers, written **into `validity_586/`**.

## Architectural Impact

Deliberately near-zero. This is a measurement run, not a feature.

- **New dependencies**: none. No new package, no network call, no API key.
- **Interface changes**: none in `src/`. **This plan does not touch `src/` at
  all.** Three small additive edits under `tests/benchmarks/`: one tuple entry
  (`_STALE_KEY_PATTERNS`), one bare-except → logged warning, two keys added to
  the report's `machine` block.
- **Coupling**: unchanged. The producer remains a harness artifact that nothing
  in `src/` imports.
- **Data ownership**: unchanged. Artifacts land in a new results subdirectory.
- **Reversibility**: trivial — revert the three edits; the artifacts are inert
  data files.

## Appetite

**Size:** Medium

**Team:** Solo dev, PM (for the publish-the-result call if the sign is
awkward), code reviewer.

**Interactions:**
- PM check-ins: 1-2 (confirming the three-arm reinterpretation of criterion 2;
  reviewing the finding before it is published if C − B is negative).
- Review rounds: 1.

The coding is small (three additive harness edits). The cost is the ~75 minutes
of measured wall clock, the care required not to damage the published baseline,
and the discipline of writing an honest findings section that does not overclaim
— which is where this issue has failed twice already.

## Prerequisites

| Requirement | Check Command | Purpose |
|---|---|---|
| LongMemEval-S corpus cached locally | `test -s ~/.cache/popoto_benchmarks/longmemeval_s_cleaned.json` | The 265MB corpus; without it the harness attempts a download. |
| Redis/Valkey reachable | `redis-cli -n 9 PING` | The harness needs a live server on localhost:6379. |
| Isolated bench DB for this lane | `test "$POPOTO_BENCH_DB" != "" -a "$POPOTO_BENCH_DB" != "0"` | Other SDLC lanes are live; DB 0 is the production agent store. This lane uses **9**. |
| No embedding provider needed | `python -c "import sys; sys.exit(0)"` | Recorded as satisfied-by-construction: `--retrieval-mode` stays at its `lexical` default. |

**Explicitly NOT required** (contradicting the issue's stated deferral reason):
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, the `[embeddings]`/`[benchmark]` extras,
and any network access.

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
