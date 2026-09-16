# Plan — #569: Full-1986 LoCoMo hybrid benchmark refresh

## Goal

Replace the 250-question stratified sample that currently backs every published
LoCoMo `hybrid` number with a full-1986 unsampled pass, and update every surface
that carries the sample qualifier.

## Status

- [x] Confirm the LoCoMo corpus is present locally
      (`~/.cache/popoto_benchmarks/locomo.json`, 2.8 MB, the cached
      `snap-research/locomo` `locomo10.json` release). No download or credential
      needed.
- [x] Confirm the command's real flags match the issue
      (`--dataset locomo --retrieval-mode hybrid`, no `--limit`).
- [x] Launch the run.
- [ ] Run completes; artifact written.
- [ ] Commit artifact + `_latest_hybrid` pointer.
- [ ] Docs edits.
- [ ] Re-pin `test_harness.py` hybrid parity test.
- [ ] Headline propagation decision recorded.
- [ ] PR opened, reviewed, merged.

## Redis DB: harness convention vs lane assignment

The lane assignment is DB 10. The external harness has its **own** convention
(`run_external._resolve_bench_db()`): it points the Popoto connection at
`POPOTO_BENCH_DB`, defaulting to **14**, rejecting 0. Lane DB 14 is held by
another concurrent lane, so the default would have collided.

Resolution: drive the harness's own mechanism to the lane's DB rather than
fighting it —

```
REDIS_URL=redis://localhost:6379/10 POPOTO_BENCH_DB=10 \
  python -m tests.benchmarks.run_external --dataset locomo --retrieval-mode hybrid
```

`REDIS_URL` covers the import-time bind; `POPOTO_BENCH_DB` covers the harness's
own in-place pool swap. Verified from the run log:

```
Benchmark isolation: Popoto connection pointed at DB 10
Swept 0 stale benchmark key(s) from DB 10 at startup.
```

The harness never issues a blanket `flushdb` — startup and teardown both SCAN+DEL
only its own `_STALE_KEY_PATTERNS` — so DB 10 is safe to share and DB 0 is never
touched.

## Surfaces to update

Named in the issue:

1. `docs/benchmarks.md` leaderboard-parity table (L466-477) — drop
   `**250-question SAMPLE**` / `**SAMPLE**` from the two `hybrid` rows, re-pin n
   and the four metrics.
2. `docs/benchmarks.md` hybrid-vs-lexical comparison table (L370-381) — drop the
   "identical 250-question stratified sample (seed 0). SAMPLE, not a full run"
   framing. At full coverage the natural lexical comparison row becomes the
   committed full-1986 `locomo_latest.json` row rather than a 250-item
   re-aggregation, which also removes the re-aggregation explanation paragraph.
3. `docs/benchmarks.md` coverage table (L339) — `250 of 1986, stratified, seed 0
   | **Sampled**` → `1986 of 1986 | **Full**`.
4. `docs/benchmarks.md` prose carrying the caveat: L225, L315, and the whole
   `!!! warning "The corrected hybrid run is a 250-question sample..."` admonition
   at L319-327.
5. Environment stated alongside the new numbers (CLAUDE.md rule).

Found by repo-wide grep, **not** named in the issue but broken by the same change:

6. `tests/benchmarks/test_harness.py::TestLeaderboardParity::test_hybrid_parity_slice_is_a_labelled_sample`
   (L213-234) — asserts `n == 194` and the four sampled metrics. Its own
   docstring says asserting the sampled n is deliberate "so a later full re-run
   trips this test instead of silently swapping a sample for a full run". This
   run is that re-run: rename and re-pin to the full-corpus parity slice
   (expected n = 1540).
7. `docs/scripts/gen_benchmark_pages.py` — the `locomo_hybrid` `Spec.note`
   (L209-219) emits a `!!! warning "This page is a 250-question SAMPLE, not a
   full run"` admonition onto the generated site page, and the LoCoMo section
   preamble (L428-431) repeats the coverage-differs prose. Both must change or
   the published site keeps claiming a sample.

8. The artifact files and the pointer mechanics themselves. `locomo_latest_hybrid.json`
   and `.md` are **git symlinks** (mode `120000`) pointing at
   `locomo_20260807_hybrid.*`; the harness rewrites them. The superseded dated
   artifact stays committed, following the precedent set by
   `locomo_20260708_hybrid.json` (`docs/benchmarks.md:395`) — it is never deleted.

## Critique findings folded in (plan-reviewer pass)

- **The analysis prose at `docs/benchmarks.md:385-402` is empirically contingent
  on the specific 250 questions and must be re-derived, not edited.** It claims
  the two arms are "indistinguishable except for one question at rank 10 and a
  rounding difference in MRR", and separately concedes "the two runs also differ
  in coverage (1986 vs 250)". Neither sentence survives the refresh unexamined:
  the first is a measured property of the sample, the second becomes false. Do
  not carry either over; recompute the delta from the new full numbers and write
  what is actually true.
- **`docs/benchmarks.md:474-477` contains a claim that flips, not a number that
  moves.** It currently reads that the hybrid rows "are shown here for the
  category-5 comparison, **not** as a leaderboard-parity claim" *because* they are
  a sample. At full 1986 the hybrid parity slice becomes a legitimate
  leaderboard-parity number, symmetric with the lexical rows above it. Editing
  only the figures leaves that sentence contradicting its own table.
- **Nothing in the repo asserts a relationship between `locomo_latest.json` and
  `locomo_latest_hybrid.json`** (no shared-item-id or shared-n test). The
  "like-for-like" claim is documentation-only and self-policed, so verify it by
  hand: same corpus file, same scoring, same n, before writing the comparison.
- **No ratchet or defaults-sync gate covers benchmark artifacts.**
  `tests/benchmarks/ratchet.py` and `test_defaults_sync.py` were both checked and
  neither applies, so there is no registration step to miss here.
- **Final verification step (added):** after the docs edits, re-run the repo-wide
  grep for `0.3400`, `0.5120`, `0.5880`, `0.4172`, `0.3041`, `0.4846`, `0.5619`,
  `0.3836`, `194`, `250-question`, `250 of 1986`, `SAMPLE` and confirm zero hits
  outside historical/superseded artifacts and `docs/plans/`. Eight numeric
  literals across four files is exactly the shape that leaves one stale.

## Headline surfaces (issue's propagation requirement)

Checked by grep for the sampled figures and for `LoCoMo`:

- `README.md` — mentions LoCoMo only in the judged-answer and LLM-extraction
  paragraphs (0.3636 judged accuracy). No retrieval hybrid numbers. Out of scope.
- `docs/index.md` — its retrieval-quality bullet cites **LongMemEval-S**, not
  LoCoMo. No LoCoMo numbers at all.
- `docs/llms.txt` — links the hybrid-retrieval feature page; carries no LoCoMo
  numbers.

So unless the full-corpus numbers move enough to change a qualitative claim
elsewhere, the expected outcome is an explicit **"no headline change"** record,
which the issue permits.

## Environment of this run — and how it differs from the 2026-08-07 block

Measured in the lane venv (`.worktrees/sdlc-569/.venv`), to be stated alongside
the new numbers per the CLAUDE.md rule:

| | This run (2026-09-16) | The 2026-08-07 block on the page |
|---|---|---|
| Python | 3.12.14 | 3.12.13 |
| Platform | macOS-26.6.2-arm64, 10-core Apple silicon | macOS-26.5.2-arm64, 10-core Apple silicon |
| Redis server | 8.10.1 | 8.6.2 |
| redis-py | 8.1.0 | 8.1.0 |
| numpy | 2.5.3 | 2.5.1 |
| sentence-transformers | **6.0.1** | **5.7.0** |
| Model | all-MiniLM-L6-v2 on CPU | all-MiniLM-L6-v2 on CPU |
| Redis DB | 10 (`POPOTO_BENCH_DB=10`) | 14 (harness default) |

**The sentence-transformers major bump 5.7.0 → 6.0.1 must be disclosed, not
buried.** It means the full-1986 hybrid figures differ from the 250-question
sample on *two* axes, coverage and embedding-library version, so any movement
between them cannot be attributed to coverage alone. This is the same
two-variables-moved caveat the page already applies to the
`locomo_20260708_hybrid` comparison (`docs/benchmarks.md:396-399`), and it gets
the same treatment: state both, attribute neither.

The hybrid-vs-lexical comparison is less exposed — the lexical arm does not touch
sentence-transformers at all — but its committed baseline (`locomo_20260807.json`)
was still measured under the older Python/Redis/numpy, so the new table needs its
own environment line rather than inheriting the 2026-08-07 block's.

## Non-goals (per the issue)

- Graph arm at full scale.
- Judged arm.
- The RRF-labeling nit from PR #553 review.

## Risk

The single real risk is the run not finishing or erroring past the 10% default
`--error-threshold`. Mitigation: the run is logged to the lane scratchpad and
progress is checked; if it does not finish, report exactly where it got to and
land nothing. No number is ever extrapolated from the partial run.
