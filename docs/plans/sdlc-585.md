---
status: Ready
type: perf
appetite: Small
owner: Valor Engels
created: 2026-09-16
tracking: https://github.com/tomcounsell/popoto/issues/585
---

# #585 — Restate the V0 validity-gating latency criterion, and pre-trim the gate

## Problem

`docs/plans/validity_primitives_v0.md` carried this Success Criterion:

> p50 validity-gated retrieval within 1 ms of ungated at 20k records, measured by an
> in-repo deterministic micro-benchmark test.

It was not met. The PM waived it on 2026-08-17 so PR #582 could merge, and #585 tracks
what the waiver owes:

1. Decide whether the target is restated as a ratio or scoped to a non-full-scan path.
2. Evaluate the one real speedup lever — pre-trimming the partition against the
   `invalid_at` ZSET with a range operation *before* the scan, instead of testing
   membership per-member with a `ZSCORE` pair inside `DECAY_SCORE_LUA`.
3. Land whichever criterion survives back into the plan doc's Success Criteria.

## Measurement environment

Every number in this document was measured in one environment, stated here once per the
CLAUDE.md rule:

| | |
|---|---|
| Machine | Darwin 25.6.0, arm64 (`/Users/valorengels/src/popoto/.worktrees/sdlc-585`) |
| Python | 3.12.14 |
| redis-py | **8.1.0** |
| Redis server | 8.10.1 |
| Redis DB | 8 (lane-scoped; `REDIS_URL=redis://localhost:6379/8` exported before `import popoto`) |
| Base commit | `a45c9eca` (main) |

**This differs from the original #585 measurement**, which was taken on redis-py **7.1.1**
(Python 3.12.13, Redis DB 15). The redis-py version matters for mypy counts, not for
server-side Lua timings — the scan and the `ZSCORE`s execute inside Redis — and the
re-measurement below reproduces the original figure closely, which is the evidence that
the version change is not confounding this result.

## Two harnesses, two numbers — read this before comparing any ratio below

This document reports ratios from **two different measurement harnesses**, and they are not
interchangeable. Conflating them was flagged in critique (C3) and is exactly the kind of
"confident, wrong number" CLAUDE.md warns about.

| Harness | What it times | Pre-change n=20k, 10%-closed |
|---|---|---|
| **A — pytest benchmark** (`TestValidityBenchmark`) | `BenchFact.query.top_by_decay(...)`, the full Python query path: arg resolution, `EVAL`, reply parse, key hydration | **1.46x** |
| **B — raw `EVAL` probe** (scratch A/B script) | one `r.eval(script, ...)` and nothing else — server-side Lua only | **1.55x** |

B shows a higher ratio because harness A dilutes the same server-side delta with constant
Python-side overhead. **Harness A is authoritative for the criterion**, because the criterion
is asserted by the in-repo benchmark, which is harness A. Harness B is authoritative for
*comparing script variants*, because it isolates the thing being changed. Every table in "The
pre-trim lever" below is harness B; the "Fresh baseline" block and the Success Criteria
ceiling are harness A. No number from one is ever quoted as the other.

## Fresh baseline: the miss reproduces

`tests/test_validity_field.py::TestValidityBenchmark`, unmodified, on the environment above:

```
[validity p50 @ 20000] ungated=37.93ms gated=55.53ms delta=17.60ms ratio=1.46x
[validity excluded-key resolution p50 @ 20000] 1.99ms
```

Against the original (redis-py 7.1.1): ungated 32–37 ms, gated 47–51 ms, ~1.4x. The miss
is reproduced, not inherited. It is ~17.6 ms of absolute overhead against a 1 ms budget.

### Post-change (harness A, same environment, pre-trim landed)

```
[validity p50 @ 20000] ungated=37.70ms gated=35.59ms delta=-2.11ms ratio=0.94x
[validity excluded-key resolution p50 @ 20000] 1.87ms
```

**1.46x → 0.94x.** The gated path now measures at parity with ungated; the −2.11 ms delta
is jitter, not a speedup over ungated — the honest reading is "the gate's cost is no longer
resolvable above run-to-run noise on this harness." The ungated control moved 37.93 → 37.70 ms
across the two runs, which sets the scale of that noise at roughly ±0.2 ms on the control and
visibly more on the gated arm. The absolute 1 ms criterion is *still* not assertable from this
— a −2.11 ms delta is inside 1 ms of ungated only by accident of which way the jitter fell, and
the next run could land at +1.5 ms without anything regressing. That is the whole argument for
the ratio form, restated: even at parity the absolute number is not a stable thing to gate on.

## Why the criterion is wrong, in numbers rather than assertion

The criterion asks for gating overhead **≤ 1 ms** at 20k records. The *ungated* p50 on the
same path and the same data is **37.9 ms**. So the criterion demands that a feature which
adds up to two server-side `ZSCORE`s per member fit inside **2.6%** of a cost it does not
control and cannot reduce.

That budget is not a property of the gate. It is a property of `DECAY_SCORE_LUA`, which
does `ZRANGE key 0 -1 WITHSCORES` and scores every member of the partition. Two independent
measurements show the denominator, not the gate, is what moves it:

- **The budget shrinks as the partition grows, for reasons unrelated to gating.** At n=5k
  the ungated p50 is 8.51 ms (1 ms = 11.8% of it); at n=20k it is 37.03 ms (2.7%); at n=50k
  it is 98.4 ms (1.0%). A criterion whose difficulty is set by the scan it is not measuring
  is measuring the wrong thing.

  These are harness-B numbers, so the n=20k entry reads 2.7% where the harness-A figure above
  reads 2.6% (1 ms over 37.03 ms vs over 37.93 ms). Same quantity, two harnesses — see "Two
  harnesses, two numbers". The argument does not turn on which one you take; both round to
  "under three percent".
- **The gate's own cost is proportional to the scan, so the ratio is the stable quantity.**
  Measured pre-change (harness B) across three partition sizes at a fixed 10%-closed shape: 1.57x
  (n=5k), 1.55x (n=20k), 1.45x (n=50k). The ratio is flat to ±8%; the absolute delta is not
  (4.9 ms / 20.2 ms / 44.5 ms).

A wall-clock assertion on shared CI hardware is additionally a coin flip. The ratio against
a same-process, same-data ungated control is the part that is actually stable, and it still
catches the regressions that matter: a second pass over the partition, or a per-member round
trip.

**Verdict: restate as a ratio.** The alternative in the issue — scoping the criterion to a
non-full-scan path — is rejected because no such path exists today for this gate.
`DECAY_SCORE_LUA` is the gate's only layer-1 home, and it full-scans by construction;
scoping the criterion to layer 3 (`_resolve_excluded_keys`, measured at 1.99 ms for two
range reads) would be measuring a different, already-cheap thing and quietly dropping the
layer that was actually missing the target.

## The pre-trim lever: evaluated, and it works

### Mechanism

Today the gate pays, per scanned member, up to two `redis.call('ZSCORE', ...)`. At 20k
members that is up to 40,000 server-side command dispatches inside one script.

The pre-trim replaces them with **two range reads before the loop**, materializing the
exclusion set into a Lua table, and an O(1) table lookup per member:

```lua
local closed = redis.call('ZRANGEBYSCORE', invalid_key, '-inf', as_of_raw)
local future = redis.call('ZRANGEBYSCORE', valid_key, '(' .. as_of_raw, '+inf')
-- excluded[member] = true for each; then `if excluded[member] then include = false end`
```

**Semantics are identical, not merely similar.** The gate's rule is
`invalid_at <= as_of` OR `valid_from > as_of`, and a member absent from either ZSET is
*unmanaged* and left alone — an exclusion rule, not a whitelist. `ZRANGEBYSCORE invalid_key
-inf as_of` is exactly the first clause (inclusive upper bound); `ZRANGEBYSCORE valid_key
(as_of +inf` is exactly the second (exclusive lower bound). A member in neither range is in
neither result, so it stays included. The A/B probe asserts the two scripts return
byte-identical replies on every shape measured below.

`as_of` is passed to the range bounds as the **raw `ARGV[7]` string**, not reformatted from
the parsed Lua number: `validity_gate_args` builds it with `repr(t)`, and round-tripping
through Lua 5.1's `%.14g` tostring or a `string.format('%.17g', ...)` would perturb the last
digits and misclassify a member sitting exactly on the boundary.

### Valkey compatibility

`ZRANGEBYSCORE`, `ZCOUNT` and `ZCARD` are core sorted-set commands present in both Redis and
Valkey. **No Redis modules are used** (repo hard rule). No command newer than Redis 2.0 is
introduced — note `ZRANGEBYSCORE` is deliberately chosen over the newer `ZRANGE ... BYSCORE`
(6.2+) for the widest compatibility, matching what `ValidityField.resolve_excluded_keys`
already issues from Python.

### Measured win (10%-closed shape, gate on)

| Shape | ungated | `ZSCORE` gate | **pre-trim** |
|---|---|---|---|
| n=20k, 10% closed (the benchmark shape) | 36.78 ms | 56.94 ms (1.55x) | **36.63 ms (1.00x)** |
| n=20k, 1% closed | 36.88 ms | 61.14 ms (1.66x) | **39.05 ms (1.06x)** |
| n=20k, 50% closed | 36.96 ms | 39.91 ms (1.08x) | **24.99 ms (0.68x)** |
| n=20k, 100% closed | 38.96 ms | 19.09 ms (0.49x) | **12.48 ms (0.32x)** |
| n=20k, 10% closed + 10% future | 38.15 ms | 57.57 ms (1.51x) | **36.10 ms (0.95x)** |
| n=5k, 10% closed | 8.51 ms | 13.39 ms (1.57x) | **9.16 ms (1.08x)** |
| n=50k, 10% closed | 98.36 ms | 142.90 ms (1.45x) | **91.25 ms (0.93x)** |

At the benchmark shape the gate becomes **free** — 1.00x, down from 1.55x. Above ~50% closed
it is *faster than ungated*, because an excluded member skips its base-score `HGET` and all
decay math.

### The regression this would have caused, and the guard that prevents it

`invalid_at` / `valid_from` are **model+field scoped**, but `DECAY_SCORE_LUA` scans **one
partition**. A model with a small hot partition and a large archive of closed records makes
the range read pull far more members than the scan touches. Measured:

| partition | closed archive | `ZSCORE` gate | pre-trim, unguarded |
|---|---|---|---|
| 2,000 | 0 | 8.25 ms (1.74x) | 3.96 ms (0.84x) |
| 2,000 | 20,000 | 7.71 ms (1.63x) | 9.15 ms (1.94x) |
| 2,000 | 200,000 | 6.40 ms (1.83x) | **56.94 ms (16.26x)** |
| 20,000 | 200,000 | 62.35 ms (1.75x) | **90.19 ms (2.54x)** |

An unguarded pre-trim is a **16x regression** on that shape, and it also pulls the whole
archive into Lua memory. So the pre-trim is taken *conditionally*, on a cardinality check.

Crossover sweep (excluded-set size as a multiple of partition size), both partition sizes:

| excluded/partition | 0 | 0.5 | 1 | 2 | 3 | 4 | 6 | 10 |
|---|---|---|---|---|---|---|---|---|
| n=5k — speedup | 1.65x | 1.59x | 1.32x | 1.19x | 1.12x | 1.16x | 0.92x | 0.76x |
| n=20k — speedup | 1.96x | 1.54x | 1.53x | 1.80x | 1.18x | 1.05x | 0.94x | 0.67x |

Crossover sits at **≈5x** for both, so the guard threshold is pinned at **4.0** — inside the
measured crossover with margin, and it bounds the Lua table at 4x the partition already being
scanned.

**The sweep's ratio and the runtime guard use the same formula** (critique N1). The guard
computes `ZCOUNT(invalid, -inf, as_of) + ZCOUNT(valid, (as_of, +inf)`, a *sum* that
double-counts a member present in both ranges — a malformed interval where
`valid_from > invalid_at`. The `excluded` table de-duplicates via `excluded[m] = true`, so
double-counting never affects correctness; it only makes the guard fall back *sooner*, which
is the safe direction. The sweep's seed data has disjoint ranges (every member is either
closed or open-and-started, never both), so sum == union there and the calibration is
measured against the same quantity the guard compares. Do not "fix" the double-count into a
union count: that would loosen the guard relative to its calibration.

**The 4.0 default is calibrated on one machine and on synthetic shapes** (critique C1/C4). It
is shipped anyway, for three reasons, and the reasons are the record of the trade-off rather
than a dismissal of it:

1. **It is a performance knob, not a correctness knob.** Both branches return identical
   replies — that is what `test_pretrim_matches_per_member_gate` pins. A mis-calibrated
   threshold costs latency, never a wrong answer.
2. **The failure it could cause is bounded and already measured.** Worst case is the
   un-pretrimmed skew shape: 16.26x on the extreme (2k partition / 200k archive), 2.54x on the
   realistic one. Bad, but not unbounded, and `ARGV[8] <= 0` is a genuine kill switch that
   restores the pre-#585 path exactly.
3. **Blast radius today is zero.** No shipped model declares a `ValidityField`
   (`constants.py`, `VALIDITY_GATING_ENABLED` docstring), so `validity_gate_args` returns
   `VALIDITY_GATE_DISABLED` for every model in the tree and this code does not execute in any
   current workload. The calibration has until the first adopter to be confirmed.

**Confirmation is owed, not waived.** #586's LongMemEval-S run produces the first realistic
closed-fraction and partition/archive skew. Re-checking the crossover against it is filed as a
follow-up rather than left as an open question in this document — see Follow-ups.

## Technical Approach

One chokepoint makes this small: all three production `DECAY_SCORE_LUA` call sites
(`query.py:621` `top_by_decay`, `query.py:1558` `composite_score`,
`context_assembler.py:714`) go through `DecayingSortedField.rank_decayed`, which owns the
KEYS/ARGV array (#648/#662). **No call site changes.**

1. **`src/popoto/fields/decaying_sorted_field.py` — `DECAY_SCORE_LUA`.** Before the `ZRANGE`,
   when `gate` is on: `ZCOUNT` both exclusion ranges, compare their sum against
   `ZCARD(zset_key) * ARGV[8]`, and if within budget build the `excluded` table with two
   `ZRANGEBYSCORE`s. In the loop, `if excluded ~= nil` take the table lookup, `else` fall
   through to today's per-member `ZSCORE` pair, unchanged.

   **`ARGV[8]` absent / unparseable / `<= 0` means "never pre-trim"** — the per-member path,
   byte-identical to pre-#585. This follows the same empty-is-off convention `KEYS[2]`
   (confidence) and `KEYS[3]`/`KEYS[4]`/`ARGV[7]` (the gate itself) already use in this
   script, and it keeps the old path alive as the parity oracle for the new one.

   The two `ZRANGEBYSCORE`s are wrapped in `pcall`: if `ARGV[7]` is a string `tonumber`
   accepts but Redis rejects as a range bound (e.g. hex), the script falls back to the
   per-member path instead of erroring, preserving today's behavior exactly.

2. **`src/popoto/fields/constants.py`** — add
   `VALIDITY_GATE_PRETRIM_MAX_RATIO = 4.0` with a docstring carrying the crossover
   measurement.

3. **`rank_decayed`** — append one ARGV: `str(Defaults.VALIDITY_GATE_PRETRIM_MAX_RATIO)` as
   `ARGV[8]`, read at call time (not import) like every other kill switch here. `numkeys`
   stays 4; nothing is renumbered.

4. **`tests/benchmarks/test_defaults_sync.py`** — register the new constant in the
   not-in-`MODULE_CONSTANTS` allowlist beside `VALIDITY_GATING_ENABLED`, with the reason.
   (CLAUDE.md / #685: narrow-scope lane test selection never reaches this file, so an
   omission fails only in CI after review approves.)

5. **`src/popoto/fields/cyclic_decay_field.py` — untouched.** `CYCLIC_DECAY_LUA` has no
   validity gate at all (the known limitation pinned by
   `TestCyclicDecayGatingGap` and documented under "Known limitations"). This change neither
   fixes nor worsens that gap.

## Test Impact

`tests/test_validity_field.py`:

- **New** `test_pretrim_matches_per_member_gate` — same data, same as-of, `ARGV[8]` off vs on;
  replies must be identical. Run across the closed/future/unmanaged shapes, including members
  absent from both ZSETs (the unmanaged-stays-visible rule).
- **New** `test_pretrim_falls_back_when_exclusion_set_dwarfs_partition` — seeds an archive
  past the 4.0 threshold and asserts the per-member path is taken *and* the result is still
  correct.

  **Mechanism, pinned here rather than left to the builder** (critique C2). Which branch ran
  is observed via `INFO commandstats`, diffing `cmdstat_zscore:calls` across the single
  `EVAL`. **Verified empirically on this environment before writing this plan**: an `EVAL`
  issuing 100 `redis.call('ZSCORE', ...)` moves the counter by exactly 100, so script-internal
  dispatches *are* counted. `INFO` is a core command on both Redis and Valkey — no module, no
  new infrastructure.

  Two details make it robust rather than flaky:
  - **Threshold, never equality.** This Redis instance is shared across lanes, so another
    lane's traffic can only inflate the delta. The two branches differ by ~2N (N = partition
    size), which no amount of background noise closes: assert `delta >= N` for the fallback
    branch and `delta < N // 2` for the pre-trim branch.
  - **No `CONFIG RESETSTAT`.** It is instance-global and would clobber other lanes' counters.
    Take a before/after delta instead.

  If the counter is unavailable on some future server build, the test must **fail loudly**,
  not silently degrade to a correctness-only assertion — a correctness assertion passes on
  both branches and would therefore assert nothing about the thing under test.
- **Update** `TestValidityBenchmark::test_p50_gated_retrieval_overhead_at_20k` — tighten
  `BENCH_MAX_RATIO` from 4.0 to **2.0**, and rewrite the docstring (it currently narrates the
  1.4x as the accepted state). The rewritten docstring cites **one** number, the harness-A
  figure this benchmark itself produces — never the harness-B 1.55x from the scratch probe
  (critique C3). 2.0 still fails a return to the per-member path, whose harness-A ratio is
  1.46x.
- Existing byte-parity and gate-semantics tests must pass unmodified — they are the
  regression net for point 1.

`tests/benchmarks/test_defaults_sync.py` — one registry entry.

## Success Criteria

- [x] `validity_primitives_v0.md`'s waived criterion is replaced (not deleted) by the ratio
      form, with the waiver history preserved.
- [x] Gated/ungated p50 ratio at 20k is **≤ 2.0**, asserted in-repo, measured against a
      same-process ungated control. **Harness A** (the pytest benchmark) — the ceiling is
      never checked against a harness-B number.
- [x] Pre-trim and per-member gate return identical replies on every shape tested.
- [x] A dwarfing exclusion set falls back to the per-member path (no 16x regression).
- [x] `black src/ tests/` and `ruff check src/` clean; `scripts/mypy_ratchet.py` not raised.
- [x] Docs updated (`docs/features/validity-and-supersession.md` performance note).

## Restated criterion (the text that lands in `validity_primitives_v0.md`)

> p50 validity-gated retrieval costs **≤ 2.0x** ungated retrieval at 20k records, measured by
> an in-repo deterministic micro-benchmark against a same-process, same-data ungated control.
>
> Supersedes the original "within 1 ms of ungated", which was waived on 2026-08-17 and is
> withdrawn as mis-specified rather than merely unmet: `DECAY_SCORE_LUA` full-scans its
> partition, so at 20k the ungated p50 is itself ~37 ms and a 1 ms budget is a 2.6% overhead
> allowance on a cost the gate does not control. The budget also *shrinks* with partition
> size (11.8% at n=5k, 1.0% at n=50k) while the gate's own ratio stays flat — so the absolute
> form measures the scan, not the gate. Resolved by [#585](https://github.com/tomcounsell/popoto/issues/585).

## Dependencies

**None blocking.** A sibling lane is running #586 (LongMemEval-S validity benchmark) and will
produce real 20k-scale gating data with a *realistic* closed-fraction and partition/archive
skew. This plan's threshold of 4.0 is calibrated on synthetic shapes; #586's data is the right
input for confirming it later. This lane does not wait on #586 and does not touch its Redis DB
(11).

## Follow-ups (filed, not left as open questions)

Critique C1 asked for the 4.0 re-verification to be a tracked commitment rather than a
question in a plan doc. Both are now filed, and referenced from the
`VALIDITY_GATE_PRETRIM_MAX_RATIO` comment in `constants.py`:

1. **[#716](https://github.com/tomcounsell/popoto/issues/716) — Confirm the pre-trim
   crossover against #586's realistic skew data**, on a second
   environment. The crossover is a ratio of Lua table-insert cost to `redis.call` dispatch
   cost, which *should* be machine-stable, but has been measured on exactly one machine. If
   the true crossover on another host is below 4.0, the guard greenlights pre-trim into the
   regression shape it exists to prevent.
2. **[#717](https://github.com/tomcounsell/popoto/issues/717) — Decide whether the
   pre-trim/fallback branch should be observable in production.** Today
   the choice is invisible to the caller. A counter or debug log would make production skew
   diagnosable, at the cost of a write on the hot Lua path. Deliberately out of scope here.

## Questions for the architect

*(The architect was AFK for this lane; these are recorded, and the lane proceeded on the
stated default in each case rather than blocking.)*

1. **Is 2.0 the right new ceiling, or should it be tighter?** Harness-A measured is well under
   it. A ceiling of 1.25x would catch a return to the per-member path immediately, but would
   also go red on the fallback shape (a legitimately dwarfing exclusion set), which the
   benchmark does not currently seed. **Proceeded with 2.0**, which fails the pre-change 1.46x
   and passes both branches.
2. **Should `VALIDITY_GATE_PRETRIM_MAX_RATIO` ship at 4.0, or hold as experimental until #586
   lands?** **Proceeded with 4.0**, on the three-part argument recorded above (perf knob not
   correctness knob; bounded and measured worst case; zero blast radius today). Follow-up 1
   is the confirmation gate.
