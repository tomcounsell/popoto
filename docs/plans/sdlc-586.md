---
status: Planning
type: chore
appetite: Medium
owner: valorengels
created: 2026-09-08
tracking: https://github.com/tomcounsell/popoto/issues/586
last_comment_id: 5570962918
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

### Key Elements

- **Three-arm run configuration**: A `--supersession none`; B `--supersession
  content-identity --no-validity-gating`; C `--supersession content-identity`.
  All at n=500 (no `--limit`), `sample=stride seed=0`, `--retrieval-mode
  lexical`, same commit, same machine, same session.
- **A quarantined artifact directory**: `--output
  tests/benchmarks/results/external/validity_586/` on every arm, so no run can
  repoint the canonical `longmemeval_s_latest` pointer or emit docs-build orphan
  warnings.
- **Three pre-run harness hardenings** (small, additive, `tests/benchmarks/`
  only): `$ValidityF:*` in the stale-key sweep; the teardown's silent
  `except Exception: pass` becomes a logged warning; `redis_version` and
  `bench_db` added to the report's `machine` block.
- **A findings write-up** that reports A→B *before* B→C, gives the
  knowledge-update and temporal-reasoning per-category breakdowns, carries the
  operational statistics (exclusion-set cardinality, supersession counts,
  producer failures, retrieval latency), states the environment, and copies
  #692's "what it does not establish" limits verbatim.

### Flow

Build lane → harden the harness (3 edits) → verify with the existing fixture
tests → run arm A (~24 min) → run arm B → run arm C → commit artifacts under
`validity_586/` → write findings (A→B first, then B→C) → publish as a
`docs/benchmarks.md` section + an issue comment on #586 → PR.

### Technical Approach

1. **Do not use the committed 2026-06-30 artifact as the "before".** It was
   measured under Python 3.12.13, an unrecorded redis-py version, and a codebase
   four months and six relevant PRs older (#588's `SUPERSEDE_LUA` membership
   change among them). Arm A is **re-run** at plan-commit HEAD so that A, B, and
   C differ in exactly one axis. The 2026-06-30 numbers are quoted only as
   context, and any A-vs-2026-06-30 difference is reported as an environment
   observation, never as a validity finding.
2. **Report A→B before reading C.** If |A→B| on Recall@1/5/10 or MRR is not
   ~0, that is a harness finding and must be stated first; C is then interpreted
   in its light rather than silently absorbing it.
3. **Run all 500 questions, report the categories.** `--question-type` filtering
   is available but is *not* used: the harness's `by_question_type` block already
   yields the knowledge-update (n=78) and temporal-reasoning (n=133) slices from
   a full run, and a full run keeps the overall number comparable across arms.
4. **Draw no category-level conclusion without an interval that excludes zero.**
   n=78 on knowledge-update is small; per README doctrine, report the breakdown
   and stop there.
5. **Recall-family only.** `--judged` is not supported with `--supersession` and
   must not be attempted. No judged-accuracy number appears anywhere in the
   write-up.
6. **Run the three arms back-to-back in one session**, so Python, redis-py,
   platform, Redis DB, and commit SHA are provably identical across them.
7. **Publish whatever the sign.** Acceptance criterion 4 is binding here in a
   way it was not on 2026-09-07: spike-2 shows the gate fires at scale
   (`excluded hits 11/25 items`), so a flat or negative delta is now a *measured*
   finding, not a tautology.

## Failure Path Test Strategy

### Exception handling coverage

- `tests/benchmarks/scenarios/external_base.py:962-963` — a bare
  `except Exception: pass` around the validity-key cleanup. **This plan converts
  it to a logged warning** (task build-2) and adds a test that asserts the
  observable behavior: patch the delete path to raise, assert a `logger.warning`
  is emitted (and that teardown still returns rather than propagating). This is
  the only `except Exception: pass` in the scope of this work.
- The driver loop's per-item error swallow (`base.py:108-114` →
  `run_external.py:1431-1436`) is *already* observable: it produces
  `status="error"`, logs, and is surfaced in the report's `n_errors` and gated
  by `--error-threshold 0.10`. No change; the run is required to finish with
  `n_errors == 0` (Success Criteria).
- The producer's own failures are counted, not swallowed: the report prints
  `producer failures` and `measurement fails` **even when zero**, so "found
  nothing" is distinguishable from "errored on everything". Both must be `0`.

### Empty/invalid input handling

- `identity_of(unit_text)` on empty/whitespace-only text: covered by #702's
  existing unit tests; unchanged here.
- The new `machine` block fields must degrade rather than crash if
  `redis.__version__` is unavailable — the builder reports `"unknown"` instead of
  raising, with a test.
- An **empty exclusion set at n=500** is the specific "empty output" hazard for
  this work: it is exactly the vacuity that re-blocked the issue. If
  `n_excluded_keys_total == 0` on arm C, the run is **not** published as a
  finding — it is reported as a defect (see Risk 3 and the Verification
  anti-criterion).

### Error state rendering

- User-visible output is the committed `.md` report and the write-up. A run
  ending with `n_errors > 0` or `producer_failures > 0` must be visible in the
  report body (it already is) and must block publication.

## Test Impact

- `tests/benchmarks/test_external.py::TestSupersessionArm::*` — **UPDATE (no
  behavior change expected)**: re-run to confirm the three harness edits keep
  `test_arm_none_is_byte_identical`,
  `test_content_identity_produces_non_empty_exclusion_set`,
  `test_no_leaked_validity_keys_after_teardown`, and
  `test_teardown_on_arm_none_is_real_noop` green. The stale-key-pattern addition
  touches a module constant these tests do not assert on; if any of them *does*
  assert the tuple's contents, update the expectation.
- `tests/benchmarks/test_external.py` (report-shape assertions) — **UPDATE if
  present**: any test asserting the exact key set of the `machine` block must
  gain `redis_version` and `bench_db`.
- **New**: a test asserting the teardown cleanup failure is logged rather than
  swallowed (see Failure Path Test Strategy).
- **New**: a test asserting `machine` carries `redis_version` and `bench_db`,
  and that an unresolvable redis-py version yields `"unknown"` rather than an
  exception.
- `docs/scripts/gen_benchmark_pages.py` tests (`_warn_orphan_artifacts`) —
  **no change**, but re-run: the committed `validity_586/` subdirectory must
  produce **zero** new orphan warnings. This is an anti-criterion.
- No `src/` tests are affected — `src/` is not modified.

## Rabbit Holes

- **Fixing #701 as part of this run.** Tempting because it is right there and
  the spike explains it. It is a separate lane being planned concurrently
  (`docs/plans/sdlc-701.md`), and spike-1 shows containment is real and tested.
  Touching `_build_external_model_class` here would collide with that lane and
  put a namespace refactor inside a measurement PR.
- **Improving the identity heuristic.** `identity_of` recognizes a narrow "I
  `<verb>` [`<preposition>`] ..." pattern and finds identity in ~2.3% of units
  (281/12222 at n=25). A better heuristic would produce a different number in an
  unknown direction — which is precisely why the README forbids reading the
  result as a bound. Improving it is a different study.
- **Answering #693.** Whether save-only inertness should be the library default
  is a design question this measurement deliberately does not settle; the
  producer here is an explicit imperative caller, the shape #693 questions.
- **Running the hybrid or judged arms.** `hybrid` adds a 90MB model download and
  a second confound; `--judged` is unsupported with `--supersession` and is a
  different metric family. Both are out.
- **Refreshing the published `longmemeval_s_latest` baseline** because arm A
  produces a newer number in a newer environment. That is a separate editorial
  decision about the docs site's headline figures, with its own review.
- **Statistical machinery.** Bootstrapping confidence intervals over 500
  questions is a tempting rigor upgrade. Report point estimates and the n per
  category, state that no category-level conclusion is drawn without an interval
  excluding zero, and leave interval estimation alone.

## Risks

### Risk 1: Using the committed 2026-06-30 baseline as the "before"

**Impact:** The comparison silently absorbs four months of unrelated code change
(#588 supersession membership, #494 tombstones, #648 field-layer routing, #563)
plus a different Python and an unrecorded redis-py version. Any delta would be
uninterpretable, and would be published as a validity finding.
**Mitigation:** Arm A is re-run at the same commit, machine, and session as B
and C. The 2026-06-30 figures appear in the write-up only as context, explicitly
labelled as a different environment.

### Risk 2: Clobbering the published baseline artifact

**Impact:** An arm-A run without `--output` repoints `longmemeval_s_latest` and
silently changes the headline recall numbers on the public docs site
(`gen_benchmark_pages.py:120`). This is a live, one-command mistake.
**Mitigation:** `--output tests/benchmarks/results/external/validity_586/` on
**every** arm, following the `graph_eval_484/` precedent; a Verification
anti-criterion asserts the `longmemeval_s_latest` symlink still resolves to
`longmemeval_s_20260630.md` in the PR diff.

### Risk 3: A vacuous result (empty exclusion set) published as a finding

**Impact:** Repeats the exact 2026-09-07 failure — `delta = 0.0` read as
"validity gating does not help" when the gate never fired.
**Mitigation:** spike-2 already measured a non-empty, hit-producing exclusion
set at n=25 (`193 keys / 11 hits`). The run is required to report
`n_excluded_keys_total > 0` **and** `n_excluded_hits_total > 0` on arm C; if
either is zero at n=500, the result is reported as a harness defect, not as a
finding about validity. Encoded as a Verification row.

### Risk 4: Wall clock overrunning the build session

**Impact:** A partially-run set of arms is worthless; three arms must share one
environment.
**Mitigation:** Measured at ≈24 min/arm, ≈75 min total (spike-2). Arms are run
sequentially in one session and each writes its artifact on completion, so a
crash costs at most one arm. If an arm dies, re-run **that arm only** and record
that it was re-run — the harness environment is deterministic given the same
commit and seed.

### Risk 5: Cross-run validity residue reaching item 1

**Impact:** A previously interrupted `content-identity` run leaves
`$ValidityF:*` keys that item 1 of the next run can see, inflating its
`n_excluded_keys` (spike-1, caveat 1).
**Mitigation:** task build-1 adds `$ValidityF:*` to `_STALE_KEY_PATTERNS`, so
both the startup and exit sweeps clear it. Additionally, the run uses a lane-
private bench DB (`POPOTO_BENCH_DB=9`) that no other lane touches.

### Risk 6: Concurrent SDLC lanes contending on Redis

**Impact:** Other lanes are live in this repo; a shared DB produces phantom
failures and, worse here, foreign keys inside a benchmark measurement.
**Mitigation:** `POPOTO_BENCH_DB=9` for the runs and `POPOTO_TEST_DB=9` for the
test suite, both lane-private. DB 0 is never touched (the harness rejects it,
and the guard raises `Db0FlushRefusedError` independently).

### Risk 7: An artifact that cannot be compared later

**Impact:** Committing three reports whose `machine` block omits the redis-py
version reproduces the defect CLAUDE.md's ratchet notes warn about — a number
whose environment is not recoverable from the artifact.
**Mitigation:** task build-3 adds `redis_version` and `bench_db` to the
`machine` block **before** the runs, so all three committed artifacts carry
them. Verified by a Verification row reading the committed JSON.

## Race Conditions

**No race conditions identified.** The benchmark driver is synchronous and
single-threaded: items are iterated in a plain `for` loop
(`run_external.py:1396`), each item's `setup`/`run`/`teardown` completes inside
`Scenario.execute()`'s `try/finally` (`base.py:95-116`) before the next item
begins, and all Redis calls are synchronous. The only background threads are the
embedding-cache invalidation listeners, which are stopped per item
(`external_base.py:1010`) *after* the validity cleanup, so pool pressure cannot
precede the `DEL`.

The one ordering property that matters — *item N's validity keys are deleted
before item N+1's ingest writes* — is guaranteed by that sequential structure
and asserted by
`tests/benchmarks/test_external.py::TestSupersessionArm::test_no_leaked_validity_keys_after_teardown`.

Cross-*process* contention (another SDLC lane writing to the same Redis DB) is
not a race in the code but is a real hazard; it is handled as Risk 6 by a
lane-private `POPOTO_BENCH_DB`.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #701] Fixing the shared `ValidityField` key namespace in
  `_build_external_model_class`. spike-1 establishes it does not block this run
  (per-item teardown deletes all six key shapes, tested). A lane is planning it
  concurrently.
- [SEPARATE-SLUG #693] Deciding whether save-only inertness is the right library
  default. This run measures an explicit imperative producer; it does not touch
  the default.
- [SEPARATE-SLUG #564] The M5 reconciliation work. Not a prerequisite any more
  and not exercised here.
- [ORDERED] Refreshing the published `longmemeval_s_latest` headline baseline to
  a 2026-09 environment. That changes public docs figures and is a PM/editorial
  call gated on this run's arm-A number being reviewed first; it must not ride
  along in this PR.

**Explicitly NOT a No-Go, contrary to the issue's framing:** the n=500 run
itself. The issue deferred it as needing "the external LongMemEval-S corpus, an
embedding provider, and hours of wall clock". spike-2 measured all three
premises false — corpus cached locally (277 MB, on disk), no embedding provider
required in the default `lexical` mode, ≈75 minutes for all three arms. It is
therefore an in-scope build task and **not** an `[EXTERNAL]` gate. If a future
executor finds the corpus missing or Redis unreachable, that is a Prerequisites
failure to report, not a licence to re-defer.

## Update System

No update-system changes required. popoto is a published library plus an mkdocs
site; this work adds no dependency, no config file, and no migration. The one
deploy-adjacent effect is the docs site, covered under Documentation.

## Agent Integration

No agent integration required. Nothing here is reachable by, or intended for, an
agent/tool surface — the supersession producer is a benchmark-harness artifact
that nothing in `src/` imports, and this plan does not change that.

## Documentation

### Feature documentation

- [ ] Add a **"Validity gating: three-arm supersession axis (#586)"** section to
      `docs/benchmarks.md`, following the `graph_eval_484` precedent
      (`docs/benchmarks.md:707-720`): prose framing plus a table citing the
      committed `validity_586/*.json` artifacts by path. Must include the
      environment line (Python, redis-py, platform, Redis DB, commit SHA), the
      A→B result stated *before* the B→C result, the knowledge-update and
      temporal-reasoning breakdowns, and the "what it does not establish" limits
      copied from `tests/benchmarks/README.md`.
- [ ] Add a short `tests/benchmarks/results/external/validity_586/README.md`
      naming the three arms, the exact commands that produced them, and the
      environment — so the directory is self-describing without the plan.
- [ ] Update `tests/benchmarks/README.md`'s "External-harness supersession axis
      (#692)" section to point at the real n=500 result, replacing the
      "that run is a follow-up" framing.

### External documentation site

- [ ] `mkdocs build --strict` passes.
- [ ] The build emits **no new** `[gen_benchmark_pages] WARNING: unmapped
      artifact` lines (guaranteed by the `validity_586/` subdirectory; verified,
      not assumed).
- [ ] The published headline recall page is unchanged — `longmemeval_s_latest`
      still resolves to `longmemeval_s_20260630.*`.

### Inline documentation

- [ ] Comment on the `$ValidityF:*` stale-key pattern explaining it exists
      because per-item teardown protects items 2..N but not item 1 after an
      interrupted run.
- [ ] Comment on the `machine` block additions naming CLAUDE.md's
      redis-py-version-dependent-metric rule as the reason.

### Issue comment

- [ ] Post the findings summary as a comment on #586 (the issue's own history is
      where the two prior re-blocks were recorded; the resolution belongs in the
      same thread).

## Success Criteria

- [ ] All three arms run at **n=500** (`n_total == 500`, `n_ok == 500`,
      `n_errors == 0`) at the same commit, machine, and session.
- [ ] Artifacts for all three arms committed under
      `tests/benchmarks/results/external/validity_586/`, and **nothing else**
      under `results/external/` is added or modified.
- [ ] `longmemeval_s_latest.{json,md}` still resolves to the 2026-06-30 run.
- [ ] Arm C reports `n_excluded_keys_total > 0` **and**
      `n_excluded_hits_total > 0` — the anti-vacuity condition that failed on
      2026-09-07.
- [ ] `producer_failures == 0` and `measurement_failures == 0` on arms B and C.
- [ ] Arm A reports the zeroed supersession block with `arm == "none"`.
- [ ] Every committed artifact's `machine` block carries `python_version`,
      `platform`, `cpu_count`, **`redis_version`**, and **`bench_db`**.
- [ ] The write-up reports **A→B before B→C**, gives per-category
      knowledge-update (n=78) and temporal-reasoning (n=133) numbers, and draws
      no category-level conclusion absent an interval excluding zero.
- [ ] The write-up contains no judged-accuracy number and no cross-family
      comparison; no `_judged` artifact exists in `validity_586/`.
- [ ] The result is published regardless of sign (criterion 4).
- [ ] Existing benchmark tests pass under `POPOTO_TEST_DB=9`, including the four
      `TestSupersessionArm` tests.
- [ ] New tests exist for the logged teardown-cleanup failure and the `machine`
      block additions.
- [ ] `ruff check src/`, `black --check src/ tests/`, `mkdocs build --strict`,
      and `scripts/mypy_ratchet.py` pass.
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

### Team members

- **Builder (harness hardening)**
  - Name: `harness-builder`
  - Role: the three additive `tests/benchmarks/` edits plus their tests
  - Agent Type: `builder` — Domain: Redis/Popoto data
  - Resume: true

- **Builder (benchmark runner)**
  - Name: `bench-runner`
  - Role: execute the three arms, commit artifacts, capture stdout logs
  - Agent Type: `builder`
  - Resume: true

- **Documentarian**
  - Name: `bench-documentarian`
  - Role: the `docs/benchmarks.md` section, the subdirectory README, the
    `tests/benchmarks/README.md` update, the #586 comment
  - Agent Type: `documentarian`
  - Resume: true

- **Validator**
  - Name: `bench-validator`
  - Role: verify the anti-criteria — baseline pointer intact, no artifacts
    outside `validity_586/`, non-vacuous exclusion set, environment stamped,
    no cross-family comparison in the prose
  - Agent Type: `validator`
  - Resume: true

## Step by Step Tasks

### 1. Add `$ValidityF:*` to the stale-key sweep

- **Task ID**: build-1
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_external.py`
- **Informed By**: spike-1 (caveat 1: startup/exit sweeps miss validity keys, so
  a SIGKILLed prior run is visible to item 1)
- **Assigned To**: `harness-builder`
- **Agent Type**: builder
- **Parallel**: true
- Add `"$ValidityF:*"` to `_STALE_KEY_PATTERNS` (`run_external.py:112-116`),
  with a comment explaining that per-item teardown protects items 2..N but not
  item 1 after an interrupted run.
- Note in the comment that `$` is not a Redis SCAN metacharacter (the existing
  comment already establishes this for `$BM25:ExtMem*`).
- Add/extend a test asserting a pre-seeded `$ValidityF:*` key is swept at
  startup.

### 2. Make the teardown cleanup failure observable

- **Task ID**: build-2
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_external.py`
- **Informed By**: spike-1 (caveat 2: bare `except Exception: pass` at
  `external_base.py:962-963` makes the one #701-observable path silent)
- **Assigned To**: `harness-builder`
- **Agent Type**: builder — Domain: Redis/Popoto data
- **Parallel**: true
- Replace the bare `except Exception: pass` (`external_base.py:962-963`) with
  `except Exception: logger.warning(...)`. The message **must contain the exact
  substring `validity key cleanup failed`** (the Verification row greps for it)
  and must name the key prefix that failed to clear plus the exception.
- Keep it non-raising — teardown must still not propagate, since it runs in a
  `finally`.
- Add a test that patches the delete to raise and asserts the warning is
  emitted and teardown returns normally.

### 3. Stamp redis-py version and bench DB into the report

- **Task ID**: build-3
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_external.py`
- **Informed By**: spike-4 (the `machine` block is `{python_version, platform,
  cpu_count}`; the README's promise that every number carries its redis-py
  version is currently false)
- **Assigned To**: `harness-builder`
- **Agent Type**: builder
- **Parallel**: true
- Add `redis_version` (from `redis.__version__`, falling back to `"unknown"`
  rather than raising) and `bench_db` (the resolved `POPOTO_BENCH_DB`) to the
  `machine` block at `run_external.py:597-601`.
- Surface both in the rendered `.md` header alongside Python and Platform.
- Add a test asserting both keys are present and that an unresolvable version
  yields `"unknown"`.
- Comment the addition with CLAUDE.md's redis-py-version-dependent-metric rule.

### 4. Validate the hardening before spending an hour of wall clock

- **Task ID**: validate-harness
- **Depends On**: build-1, build-2, build-3
- **Assigned To**: `bench-validator`
- **Agent Type**: validator
- **Parallel**: false
- `POPOTO_TEST_DB=9 pytest tests/benchmarks/test_external.py -q` — all green,
  including the four `TestSupersessionArm` tests.
- `ruff check src/`, `black --check src/ tests/`.
- Smoke the three arms on the fixture
  (`tests/benchmarks/datasets/fixtures/longmemeval_s_sample.json`) and confirm
  the #692 demonstration numbers reproduce, so a harness regression is caught
  before the corpus runs.

### 5. Run arm A — baseline, no ValidityField

- **Task ID**: run-arm-a
- **Depends On**: validate-harness
- **Assigned To**: `bench-runner`
- **Agent Type**: builder
- **Parallel**: false
- `POPOTO_BENCH_DB=9 python -m tests.benchmarks.run_external --dataset
  longmemeval-s --supersession none --output
  tests/benchmarks/results/external/validity_586/`
- **The `--output` flag is mandatory**: without it this command repoints the
  published `longmemeval_s_latest` pointer (Risk 2).
- Expect ≈24 minutes. Capture full stdout to the artifact directory.
- Confirm `n_total == 500`, `n_errors == 0`, supersession block zeroed with
  `arm == "none"`.

### 6. Run arm B — producer on, gate off

- **Task ID**: run-arm-b
- **Depends On**: run-arm-a
- **Assigned To**: `bench-runner`
- **Agent Type**: builder
- **Parallel**: false
- `POPOTO_BENCH_DB=9 python -m tests.benchmarks.run_external --dataset
  longmemeval-s --supersession content-identity --no-validity-gating --output
  tests/benchmarks/results/external/validity_586/`
- Confirm `n_excluded_keys_total > 0` with `n_excluded_hits_total == 0` (gate
  off: keys are computed but nothing is subtracted) and `producer_failures == 0`.

### 7. Run arm C — producer on, gate on

- **Task ID**: run-arm-c
- **Depends On**: run-arm-b
- **Assigned To**: `bench-runner`
- **Agent Type**: builder
- **Parallel**: false
- `POPOTO_BENCH_DB=9 python -m tests.benchmarks.run_external --dataset
  longmemeval-s --supersession content-identity --output
  tests/benchmarks/results/external/validity_586/`
- Confirm the anti-vacuity condition: `n_excluded_keys_total > 0` **and**
  `n_excluded_hits_total > 0`. If either is zero, **stop** and report a harness
  defect rather than publishing a delta (Risk 3).

### 8. Commit artifacts

- **Task ID**: commit-artifacts
- **Depends On**: run-arm-c
- **Assigned To**: `bench-runner`
- **Agent Type**: builder
- **Parallel**: false
- `git add tests/benchmarks/results/external/validity_586/` and commit.
- Verify the diff adds **nothing** elsewhere under `results/external/`, and that
  `longmemeval_s_latest.md` still points at `longmemeval_s_20260630.md`.

### 9. Write the findings

- **Task ID**: document-findings
- **Depends On**: commit-artifacts
- **Assigned To**: `bench-documentarian`
- **Agent Type**: documentarian
- **Parallel**: false
- `docs/benchmarks.md` section (structure per the Documentation section):
  environment line first, **A→B before B→C**, per-category knowledge-update and
  temporal-reasoning tables, operational statistics, then the verbatim "what it
  does not establish" limits.
- `validity_586/README.md` with the three exact commands and the environment.
- Update `tests/benchmarks/README.md` to cite the real n=500 result.
- Draft the #586 issue comment.
- **Publish the result whatever the sign** — a negative C − B means the gate
  removed records the retriever wanted, which is a real finding about subtractive
  gating.

### 10. Final validation

- **Task ID**: validate-all
- **Depends On**: document-findings
- **Assigned To**: `bench-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run every row of the Verification table.
- Read the prose for cross-family contamination: no judged-accuracy number, no
  comparison against LoCoMo `*_judged` results, no claim of the form "V0
  validity gating improves LongMemEval-S by X".
- Confirm every published number is accompanied by its environment.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Benchmark harness tests pass | `POPOTO_TEST_DB=9 python -m pytest tests/benchmarks/test_external.py -q` | exit code 0 |
| Lint clean | `python -m ruff check src/` | exit code 0 |
| Format clean | `python -m black --check src/ tests/` | exit code 0 |
| Docs build strict | `python -m mkdocs build --strict` | exit code 0 |
| Three arms committed (3 dated + 3 latest `.md`) | `ls tests/benchmarks/results/external/validity_586/*.md \| wc -l` | output > 5 |
| Arm A ran at n=500 with no errors | `python -c "import json;d=json.load(open('tests/benchmarks/results/external/validity_586/longmemeval_s_latest.json'));s=d['summary'];print(s['n_total']==500 and s['n_ok']==500 and s['n_errors']==0)"` | output contains True |
| Arm C ran at n=500 with no errors | `python -c "import json;d=json.load(open('tests/benchmarks/results/external/validity_586/longmemeval_s_latest_sup-content-identity.json'));s=d['summary'];print(s['n_total']==500 and s['n_ok']==500 and s['n_errors']==0)"` | output contains True |
| **Anti-vacuity**: arm C excluded keys AND hits are non-zero | `python -c "import json;b=json.load(open('tests/benchmarks/results/external/validity_586/longmemeval_s_latest_sup-content-identity.json'))['supersession'];print(b['n_excluded_keys_total']>0 and b['n_excluded_hits_total']>0)"` | output contains True |
| Producer did not fail | `python -c "import json;b=json.load(open('tests/benchmarks/results/external/validity_586/longmemeval_s_latest_sup-content-identity.json'))['supersession'];print(b['producer_failures']==0 and b['measurement_failures']==0)"` | output contains True |
| Arm A is a true baseline (zeroed block, arm "none") | `python -c "import json;b=json.load(open('tests/benchmarks/results/external/validity_586/longmemeval_s_latest.json'))['supersession'];print(b['arm']=='none' and b['n_supersessions']==0)"` | output contains True |
| Environment stamped in every committed artifact | `python -c "import json,glob;ps=[p for p in glob.glob('tests/benchmarks/results/external/validity_586/*_latest*.json')];print(all(set(('python_version','platform','cpu_count','redis_version','bench_db'))<=set(json.load(open(p))['machine']) for p in ps) and len(ps)>=3)"` | output contains True |
| **Anti-criterion (Risk 2)**: published baseline pointer untouched | `readlink tests/benchmarks/results/external/longmemeval_s_latest.md` | output contains longmemeval_s_20260630.md |
| **Anti-criterion (Risk 2)**: no results touched outside `validity_586/` | `git diff --name-only origin/main...HEAD -- tests/benchmarks/results/external/ \| grep -vc '^tests/benchmarks/results/external/validity_586/'` | match count == 0 |
| **Anti-criterion (metric family)**: no judged artifact in the study | `ls tests/benchmarks/results/external/validity_586/ \| grep -c judged` | match count == 0 |
| **Anti-criterion (No-Go #701)**: `_build_external_model_class` untouched | `git diff origin/main...HEAD -- tests/benchmarks/scenarios/external_base.py \| grep -c '_build_external_model_class'` | match count == 0 |
| Stale-key sweep covers validity keys | `grep -c '\$ValidityF:\*' tests/benchmarks/run_external.py` | output > 0 |
| Teardown validity-cleanup failure is logged, not swallowed | `grep -c 'validity key cleanup failed' tests/benchmarks/scenarios/external_base.py` | output > 0 |

## Critique Results

**Round 1 — 2026-09-08. Depth: FULL. Mode: independent roster (3 critics —
Risk & Robustness, Scope & Value, History & Consistency).**
**Verdict: READY TO BUILD (with concerns) — 0 blockers, 4 concerns, 1 nit.**

Structural checks: all required sections present; task IDs build-1..validate-all
contiguous with valid, acyclic `Depends On` edges; every cited file path exists;
all cited line references re-verified at `c2e1102b`
(`validity_field.py:915-931`, `:781-797`; `base.py:95-116`;
`external_base.py:919-1012`; `run_external.py:112-116`, `:597-601`,
`:1021-1034`). Prerequisites re-run and passing: corpus present at
277,383,467 bytes; `redis-cli -n 9 PING` → PONG with `DBSIZE` 0. The
Verification table's artifact filenames were re-derived from the harness's own
suffix composition (`run_external.py:999-1003`, `:1559-1563`) and are correct
for all three arms, including arm B's `_sup-content-identity_nogate`.

### C1 — `stop_invalidation_listeners()` is the one unguarded step in teardown

- **Severity**: CONCERN — *Risk & Robustness*
- **Location**: Race Conditions / spike-1
- **Finding**: `external_base.py:1010` calls `stop_invalidation_listeners()`
  bare, while every sibling cleanup block in the same `teardown()` is wrapped in
  `try/except Exception: pass`. `Scenario.execute()`'s `except Exception`
  (`base.py:107-114`) wraps only `setup()`/`run()`; `teardown()` runs in the
  `finally`, so a raise there propagates out of `execute()` uncaught and kills
  the whole driver loop mid-arm rather than producing the `status="error"`
  single-item containment spike-1 claims.
- **Suggestion**: Wrap the call in the file's existing `try/except Exception:
  pass` idiom, or state in the plan why it is safe left bare.
- **Implementation Note**: The exposure is real but latent for this run — the
  code's own comment at `external_base.py:1009` says *"No-op in lexical mode (no
  listener ever starts)"*, and Key Elements pins `--retrieval-mode lexical` on
  all three arms. That safety rests on an inline comment, not a cited test.
  Fold one line into **build-2** (it is already editing this method): wrap
  `stop_invalidation_listeners()` in `try/except Exception:` logging a warning,
  keeping teardown non-raising. Do not move the call or reorder it relative to
  the validity cleanup — Race Conditions depends on it running *after* the
  `DEL`.

### C2 — Risk 2's `--output` mitigation is detection, and it is checked ~75 minutes late

- **Severity**: CONCERN — *Risk & Robustness*
- **Location**: Risk 2 / tasks run-arm-a, commit-artifacts
- **Finding**: `--output` is per-invocation with no code-level guard (`global
  RESULTS_DIR; if args.output: RESULTS_DIR = args.output`,
  `run_external.py:1216-1218`), so the mitigation depends on the flag being typed
  correctly on three separately-issued commands. The anti-criterion that would
  catch an omission is not evaluated until task 8, after all three ~24-minute
  arms — a clobbered `longmemeval_s_latest` pointer from arm A stays undetected
  for the full session, and the plan states no recovery procedure.
- **Suggestion**: Check the pointer inline immediately after run-arm-a, before
  starting run-arm-b.
- **Implementation Note**: Add to **run-arm-a** as a blocking step: `readlink
  tests/benchmarks/results/external/longmemeval_s_latest.md` must still print
  `longmemeval_s_20260630.md`. Recovery does **not** require re-running the arm:
  `save_reports()` still writes the correctly-dated, uniquely-named
  `longmemeval_s_{date}.{json,md}` even when `--output` is omitted, so the fix is
  `ln -sf longmemeval_s_20260630.md tests/benchmarks/results/external/longmemeval_s_latest.md`
  (and the `.json` twin) plus moving the misplaced dated pair into
  `validity_586/`. Record that recovery path in the plan so an operator does not
  spend 24 minutes re-running arm A.

### C3 — build-3 changes a harness-wide report schema inside a single-study plan

- **Severity**: CONCERN — *Scope & Value*
- **Location**: Solution / Key Elements ("Three pre-run harness hardenings"), task build-3
- **Finding**: The `machine` dict at `run_external.py:597-601` lives in the
  single shared `aggregate` builder used by **every** dataset run through
  `run_external.py`, not just this study. Adding `redis_version`/`bench_db`
  therefore diverges every future benchmark report (LoCoMo, the extraction axis,
  graph-eval) from every artifact committed before this PR, and no Risk, No-Go,
  or Documentation entry says so.
- **Suggestion**: Either split build-3 into its own reviewable change, or keep it
  and add an explicit note that the `machine` block schema now diverges from all
  previously committed artifacts.
- **Implementation Note**: The breakage half of this was checked during critique
  and is **absent** — no test or renderer enumerates the exact `machine` key set;
  `build_markdown_report` reads only `machine['python_version']` and
  `machine['platform']` (`run_external.py:803-804`), and the RLT harness uses a
  separate `build_machine_metadata` (`rlt/run_rlt.py`), so the addition is purely
  additive and safe. What remains is disclosure, not risk: add one line to the
  Documentation section (and the `validity_586/README.md`) recording that
  artifacts dated before this PR carry a three-key `machine` block and are not
  directly diffable against the new five-key one. Test Impact's *"UPDATE if
  present"* can be resolved to "no such assertion exists".

### C4 — Open Question 1 is load-bearing but gates no task

- **Severity**: CONCERN — *History & Consistency*
- **Location**: Open Questions item 1 vs. Appetite / Step by Step Tasks
- **Finding**: Open Question 1 says of the three-arm reinterpretation of
  acceptance criterion 2 that *"Everything downstream follows from that call"*,
  and Appetite budgets a PM check-in to confirm it — but no task carries a
  `Depends On` edge to that confirmation. Tasks 1-10, including the ~75 minutes
  of wall clock, run unconditionally.
- **Suggestion**: Either gate the runs on the confirmation, or state that the
  plan proceeds on a self-approved assumption regardless of the answer.
- **Implementation Note**: Insert a zero-cost task `confirm-reinterpretation`
  (`Depends On: validate-harness`, `Parallel: false`) and add it to
  **run-arm-a**'s `Depends On`, so the question fails fast *before* the wall
  clock is spent rather than after arm C is committed. Placing it after
  `validate-harness` rather than at the top keeps the three harness edits — which
  are correct under either framing — off the critical path of the PM answer.

### N1 — Verification has no arm-B row

- **Severity**: NIT — structural cross-reference check
- **Location**: Verification table vs. Success Criteria
- **Finding**: Success Criteria require *all three* arms at `n_total == 500`,
  `n_ok == 500`, `n_errors == 0`, but the Verification table carries rows for
  arms A and C only; arm B is checked inline in task 6 and nowhere else. Add the
  row against
  `validity_586/longmemeval_s_latest_sup-content-identity_nogate.json` (filename
  re-derived from `run_external.py:1559-1563` and confirmed).

### Checked and cleared

- The n=5 → n=25 → n=500 wall-clock extrapolation. Both spike runs used
  `--supersession content-identity` with gating on, i.e. arm-C-equivalent cost
  including the second measurement-only `assemble()`
  (`external_base.py:844-882`), applied uniformly to arms A and B — the error is
  in the safe direction. `--dry-run` was confirmed to skip only report saving
  (`run_external.py:982-983`), not the run, so the timings are representative.
- The anti-vacuity inference from `193 keys / 11 hits` at n=25. Sound as an
  inductive step; note it is a low bar that is near-certain to pass, so it
  establishes the gate *fires*, not that the delta is meaningful.
- spike-1's #701 containment argument. Verified against `base.py:95-116` and
  `external_base.py:919-964`: `finally: teardown()` does run on every exit path,
  the cleanup is guarded on the declared field, and it deletes all six key
  shapes. The one hole is C1, above.
- Metric-family and environment doctrine. The plan never cross-compares
  judge-accuracy with recall, forbids `--judged`, and does not cross-compare its
  redis-py 7.1.1 spike numbers against the 2026-09-07 redis-py 8.1.0 probe.

---

## Open Questions

1. **Is the three-arm reinterpretation of acceptance criterion 2 accepted?**
   The issue asks for a two-arm "before/after" against the committed n=500
   baseline; #692's README mandates three arms and this plan re-runs arm A
   rather than reusing the 2026-06-30 artifact. Everything downstream follows
   from that call.
2. **If arm A's n=500 number differs materially from the published 2026-06-30
   baseline** (four months and six relevant PRs apart, different Python and
   redis-py), is that reported as an incidental environment observation inside
   this study, or filed as its own issue? This plan assumes the former and
   treats refreshing the published headline as an `[ORDERED]` No-Go.
3. **Does #701 need to land first?** spike-1 says no (per-item teardown deletes
   all six validity key shapes, tested), and this plan proceeds without it. The
   lane is being planned concurrently; if the PM wants strict sequencing, say so
   before the runs are spent.
4. **Should the run also be repeated under redis-py 8.1.0?** The lane's
   `uv.lock` environment resolves 7.1.1. The three arms are internally
   consistent either way, but the repo has a documented history of
   redis-py-version-dependent numbers. This plan runs one version and stamps it;
   a second version is a doubling of wall clock for a comparison nobody has
   asked for yet.
