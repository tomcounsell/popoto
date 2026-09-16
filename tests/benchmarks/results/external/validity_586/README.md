# LongMemEval-S — V0 validity gating, three-arm study (#586)

Artifacts for the before/after that PR #582 (issue #580) owed and the PM
deferred on 2026-08-17. Issue [#586](https://github.com/tomcounsell/popoto/issues/586).

**Read `tests/benchmarks/README.md` § "What the resulting delta does and does
not establish" before quoting any number here.** In particular: these are
*recall-family* numbers and must never be compared against judged-accuracy
figures, and nothing here is a statement of the form "V0 validity gating
improves LongMemEval-S by X" — V0 ships no supersession producer, so every
number is a property of the pair (this harness heuristic, V0's gate).

## Environment

Every number below was produced in this environment, and is only meaningful
in it (CLAUDE.md: *state the environment alongside any count*).

| | |
|---|---|
| Python | 3.12.14 |
| redis-py | 8.1.0 |
| Redis server | **not captured.** Do not read `machine.redis_version` as the server version — `run_external.py:147-159` sets it from `redis.__version__`, the client, so it duplicates the redis-py row above |
| Platform | macOS-26.6.2-arm64-arm-64bit, 10 cores (Apple silicon) |
| Bench DB | 12 (`POPOTO_BENCH_DB=12`; DB 0 is rejected by the harness) |
| Retrieval mode | `lexical` (BM25 only — **not** the hybrid mode the README headline quotes) |
| Ranking unit | session (LongMemEval-S ground truth is session IDs) |
| Corpus | full LongMemEval-S, 500/500 questions, no sampling |
| Arms A, B run date | 2026-09-08 |
| Arm C run date | 2026-09-16 |

Arms A and B ran on 2026-09-08 and arm C on 2026-09-16, on the same machine,
same corpus, same commit-relevant harness code, and the same bench DB. The
producer's one failure reproduces identically across B and C (see *Producer
failure* below), which is the main evidence that the eight-day gap did not
perturb the stored corpus. It is not a claim of bit-identity: one item's
interval bookkeeping does differ between the two runs, recorded in the same
section.

## The three commands

Run from the repository root. The `--output` flag is **mandatory** on all
three: without it the run repoints the *published* `longmemeval_s_latest`
pointers in the parent directory (Risk 2 in the plan).

```bash
# Arm A — baseline: no ValidityField, no producer
POPOTO_BENCH_DB=12 python -m tests.benchmarks.run_external \
    --dataset longmemeval-s --supersession none \
    --output tests/benchmarks/results/external/validity_586/

# Arm B — producer on, gate OFF
POPOTO_BENCH_DB=12 python -m tests.benchmarks.run_external \
    --dataset longmemeval-s --supersession content-identity --no-validity-gating \
    --output tests/benchmarks/results/external/validity_586/

# Arm C — producer on, gate ON
POPOTO_BENCH_DB=12 python -m tests.benchmarks.run_external \
    --dataset longmemeval-s --supersession content-identity \
    --output tests/benchmarks/results/external/validity_586/
```

## Files

| File | Arm |
|---|---|
| `longmemeval_s_20260908.{json,md}` | A — baseline (`longmemeval_s_latest` here points at it) |
| `longmemeval_s_20260908_sup-content-identity_nogate.{json,md}` | B — producer on, gate off |
| `longmemeval_s_20260916_sup-content-identity.{json,md}` | C — producer on, gate on |
| `arm_{A,B,C}_stdout.txt` | Full captured stdout for each run |

Named `.txt` rather than `.log` because the repo's `.gitignore` excludes
`*.log`, and these are intended artifacts rather than scratch output.

## Results

### Overall, n=500

| Metric | A (baseline) | B (producer, gate off) | C (producer, gate on) | A→B | **B→C** |
|---|---|---|---|---|---|
| Recall@1 | 0.8560 | 0.8560 | 0.8480 | +0.0000 | **−0.0080** |
| Recall@5 | 0.9520 | 0.9520 | 0.9480 | +0.0000 | **−0.0040** |
| Recall@10 | 0.9780 | 0.9780 | 0.9780 | +0.0000 | **+0.0000** |
| MRR | 0.8987 | 0.8987 | 0.8935 | +0.0000 | **−0.0052** |

**A→B is exactly zero on every metric, on all 500 questions individually.**
That is the result `tests/benchmarks/README.md` predicts ("on a recall metric
this should be ~0"), and it is what licenses reading B→C as the gate's effect
alone: the field declaration, the extra per-save Lua command, and the
producer's write ordering cost nothing measurable.

### The gate is live, not structurally nil

The anti-vacuity condition the 2026-09-07 attempt failed:

| Arm C statistic | Value |
|---|---|
| `n_excluded_keys_total` | 3699 |
| `n_excluded_hits_total` | **177** |
| `n_supersessions` | 3699 |
| `identity_writes` / `identity_groups` | 5473 / 1193 |
| `units_with_identity` / `units_seen` | 5474 / 246738 |

177 excluded *hits* (against arm B's 0, where keys are computed but nothing is
subtracted) is the evidence that the three gating layers actually subtract on a
real corpus at real scale. This is the study's primary positive finding: the
machinery works.

### Per category

Read with the flip counts beside the deltas — at these sample sizes a
percentage moves a lot per question.

| Category | n | R@1 B | R@1 C | Δ R@1 | MRR B | MRR C | Δ MRR | R@1 lost / gained |
|---|---|---|---|---|---|---|---|---|
| **knowledge-update** | 78 | 0.9487 | 0.9487 | **+0.0000** | 0.9679 | 0.9679 | **+0.0000** | 0 / 0 |
| **temporal-reasoning** | 133 | 0.8346 | 0.8195 | −0.0150 | 0.8778 | 0.8706 | −0.0071 | 2 / 0 |
| multi-session | 133 | 0.8271 | 0.8195 | −0.0075 | 0.8882 | 0.8806 | −0.0076 | 1 / 0 |
| single-session-user | 70 | 0.9286 | 0.9143 | −0.0143 | 0.9521 | 0.9413 | −0.0107 | 1 / 0 |
| single-session-assistant | 56 | 1.0000 | 1.0000 | +0.0000 | 1.0000 | 1.0000 | +0.0000 | 0 / 0 |
| single-session-preference | 30 | 0.4000 | 0.4000 | +0.0000 | 0.5443 | 0.5477 | +0.0033 | 0 / 0 |

The two categories issue #586 named are the first two rows.
**knowledge-update — the category subtractive validity gating exists to help —
moved by exactly zero on every metric, with zero questions changing rank.**

### Significance, stated honestly

Paired bootstrap over the 500 per-question deltas, 20,000 resamples, seed 0:

| Metric | Δ (C−B) | 95% CI | Excludes zero |
|---|---|---|---|
| Recall@1 | −0.0080 | [−0.0160, −0.0020] | yes |
| Recall@5 | −0.0040 | [−0.0100, +0.0000] | no |
| MRR | −0.0052 | [−0.0111, −0.0005] | yes |

The CI excludes zero for R@1 and MRR, but the two do so for different reasons
and the difference matters more than the verdict.

**Recall@1** is one-sided: across all 500 questions **4 lost their rank-1 hit
and 0 gained one**, so its interval excludes zero on direction rather than
magnitude. Recall@5 behaves the same way (0 gained, 2 lost).

**MRR is not one-sided.** Nine questions moved, 3 up and 6 down:

| Direction | Items |
|---|---|
| gained | `06878be2` +0.0091, `95228167` +0.1667, `6e984302` +0.0500 |
| lost | `46a3abf7` −0.9000, `bc8a6e93` −0.7500, `8077ef71` −0.5000, `gpt4_2487a7cb` −0.5000, `6d550036` −0.1071, `6b7dfb22` −0.0758 |

Its interval excludes zero because the losses are large relative to the gains,
not because nothing moved upward.

**Recall@10's +0.0000 is a wash, not stasis**: `06878be2` gained a hit
(0.0 → 1.0) and `6b7dfb22` lost one. This is the counterexample to the
intuition that a subtractive gate can only lose recall — removing a record
promotes lower-ranked records across a fixed cutoff *k*, and here that promoted
one question's gold session into the top 10. Per-item B→C directionality:

| Metric | improved | worsened |
|---|---|---|
| Recall@1 | 0 | 4 |
| Recall@5 | 0 | 2 |
| Recall@10 | 1 | 1 |
| MRR | 3 | 6 |

## Producer failure (1 of 246,738 units)

Both arms B and C report `producer_failures: 1`, `measurement_failures: 0`.
The plan's `producer_failures == 0` criterion is **not met and is not reachable
on this corpus**; it is recorded as deviation D3 in `docs/plans/sdlc-586.md`.

One unit failed with `POPOTO_VALIDITY_CLOSE_BEFORE_START`. `SUPERSEDE_LUA`
check 5 refuses to close an interval at a timestamp earlier than the
incumbent's own `valid_from`, because a zero-or-negative-length interval "is a
caller bug, not a state to store silently" — that guard is correct. But
LongMemEval-S haystack sessions are not monotonic in session date, so two
sessions sharing a content identity can arrive with the later-ingested one
carrying the earlier date; `supersession_axis.py` passes dates through in
ingest order without a monotonicity check, and the script correctly refuses.

Measured impact, computed from the committed per-question blocks rather than
asserted:

- Exactly one item differs in record count: `18bc8abd`, 436 records in arm A
  against 435 in arms B and C.
- That item scores 1.0 on every metric in all three arms — the dropped record
  was not the one the retriever needed.
- Zero of the 500 items differ on any metric between arms A and B.
- Arm C reproduced the failure identically (same count, same item), so the drop
  cancels in C−B.

### The arms are not bit-identical in interval state

Stated because the design leans on B and C behaving the same way, and the
artifacts show one place where they do not:

| | arm B | arm C |
|---|---|---|
| `n_supersessions` | 3698 | **3699** |
| `n_excluded_keys_total` | 3697 | **3699** |

Localized to a single item, `603deb26`: 7 supersessions in B against 8 in C,
with 6 vs 8 excluded keys, while `n_saved_records` is 472 in both. The record
set matches; the interval bookkeeping does not. `603deb26` is not among the
nine items whose metrics moved, so the measured impact on every number in this
directory is nil — but the producer is evidently not perfectly deterministic
across the two runs, and a reader should not take "same corpus" to mean more
than the arms actually demonstrate.

(Note that arm B's `n_excluded_keys_total` is 3697, not 0: excluded *keys* are
computed in both arms, and only the *hits* subtraction is behind the gating
flag. See `scenarios/external_base.py:879-886`.)

## What this does not establish

- **Not** "V0 validity gating improves (or costs) LongMemEval-S X%". V0 ships
  no producer; this is a property of the pair (this heuristic, V0's gate).
- **Not** comparable to the README/`docs/index.md` headline retrieval figures.
  Those are **hybrid** BM25+vector (Recall@1 0.892); every arm here is
  **lexical** (Recall@1 0.8560). Different retrieval mode, different number.
- **Not** comparable to the published 2026-06-30 n=500 baseline as a "before".
  Arm A is the before. The 2026-06-30 artifact ran in a different environment
  with an unrecorded redis-py version; it is context only (plan decision D2).
- **Not** a judged-accuracy result. No `_judged` artifact exists in this
  directory, and no number here may be read against one (metric-family
  doctrine).
- **Not** a generalization beyond content-identity supersession. A semantic or
  LLM-derived producer would close a different, probably larger, set.

## Provenance note: these arms predate #701

All three arms were run on a branch that forked from `main` **before**
[#701](https://github.com/tomcounsell/popoto/issues/701) landed (PR #705,
merged 2026-09-09). Under that older code every per-item benchmark model class
shared one Redis key namespace for its field keys — including `$ValidityF:` —
because `_meta.db_class_key` was captured at class-creation time, before the
post-hoc `__name__` rename reached it. `#701` replaced that with
`type(name, bases, ns)` so each item owns its namespace from creation.

This does **not** invalidate the comparison, for two reasons:

1. **All three arms ran under the same code**, so whatever the namespacing
   does, it does identically to A, B, and C. The deltas are internally
   consistent, and the deltas are what this study reports.
2. **Cross-item contamination was already contained** under the old code:
   `ExternalScenario.teardown()` deletes the shared keys after every item.
   That containment is exactly why #701 describes its own change as turning
   teardown into "cheap targeted cleanup rather than the sole thing preventing
   cross-item contamination".

What it does mean: a re-run on current `main` is not guaranteed to reproduce
these figures byte-for-byte, and anyone re-running should expect to re-measure
rather than diff against this directory. The branch carrying these artifacts
merges `main` (and therefore #701) *after* the runs, so the committed code and
the committed numbers come from different commits — stated here rather than
left for a reader to discover from `git log`.

## Schema note

The `machine` block in these artifacts carries `redis_version` and `bench_db`
in addition to `python_version`, `platform`, and `cpu_count`. Those two keys
were **added in this PR**, so artifacts dated before it carry only three keys
and are not directly diffable on that field.
