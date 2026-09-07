---
status: Planning
type: feature
appetite: Medium
owner: Valor Engels
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/692
last_comment_id: none
---

# #692 — A label-blind supersession producer for the LongMemEval-S ingest arm

## Problem

The LongMemEval-S external harness writes memories and never closes one. Every
record is opened by `ValidityField.on_save` in mode `"open"`
(`valid_from = save_time`, `invalid_at = +inf`), and the gate excludes only on
`invalid_at <= now` or `valid_from > now`
(`src/popoto/fields/validity_field.py:920-932`). With no `supersede()` /
`invalidate()` call anywhere in the ingest arm, the exclusion set is
structurally always empty, so V0 validity gating (#580 / PR #582) is a
subtractive no-op and #586's owed before/after would print the same number
twice by construction.

Two independent facts make this concrete:

- No benchmark model declares a `ValidityField` at all.
  `tests/benchmarks/scenarios/external_base.py` builds four model variants —
  `_build_graph_model_class` (`:143-200`) and the three branches of
  `_build_external_model_class` (`:202-270`) — and none of them carries a
  validity axis. Its only writes are `.save()` at `:479` and `:522`.
- Even declaring one changes nothing on its own. Confirmed empirically in
  spike-2 below on the exact harness model shape: five plain `.save()`s produce
  an exclusion set of **0**; a properly registered
  `save_and_supersede` pair produces **1**. That is the same result #693 filed
  as a question about intended library behavior — this plan does not decide
  that question, it works within the answer as shipped.

So the harness needs a *producer* of supersessions. The issue's central
question is what should drive it, because the choice determines what the
number #586 publishes means.

## Freshness Check

**Disposition: Minor drift.** Baseline for this check: `4ac805f0` (main moved
during planning; the issue was filed at `dbbc66bb`).

| Claim in #692 | Re-verified | Result |
|---|---|---|
| `external_base.py` declares no `ValidityField` on any of four variants | `grep -rn "ValidityField\|supersede\|invalidate\|ProvenanceJournal\|journal"` over the file | Zero matches. Confirmed. |
| Only writes are `.save()` at `:479` and `:522` | `grep -n "\.save()"` | Exactly those two lines. Confirmed, no drift. |
| `run_external.py` has no supersession references | Same grep. Note the issue's path is loose — the file is `tests/benchmarks/run_external.py`, not under `scenarios/` | Zero matches. Confirmed. |
| Gate logic at `validity_field.py:920-931` | Read | **Drifted by one line** — the body now spans `:920-932`, unchanged in substance. `c046e1bd` (PR #697, merged 2026-09-07T11:32Z, *after* the issue was filed) touched this file, converting `POPOTO_REDIS_DB` imports to `get_REDIS_DB()` call sites. It moved lines; it did not change the gate. |
| `haystack_dates` exists in the raw schema but is never parsed into `BenchmarkItem` | Read `datasets/longmemeval_s.py:_parse_record` (`:93-186`) and the committed fixture | Confirmed both ways: the docstring documents `haystack_dates` in the schema, `_parse_record` never reads it, and `tests/benchmarks/datasets/fixtures/longmemeval_s_sample.json` carries the key on all 3 records. |
| `question_type` is the only category signal in `BenchmarkItem.metadata` | Read `:176-185` | Confirmed. |
| #560 closed having shipped `ProvenanceJournal` unused by the harness | `gh pr view 589` (merged 2026-08-19) | Confirmed. |

Commits touching the four relevant files since the issue was filed: exactly one
(`c046e1bd`), described above. No commit partially addresses the problem.

**Coordination, not a blocker:** `docs/plans/` has no active plan for this area
(`validity_primitives_v0.md` is the shipped V0 plan whose `[EXTERNAL]` No-Go is
the origin of this debt; `external_benchmark_harness.md` predates the validity
axis). Sibling issue **#693** is open and overlaps in subject but not in scope —
it asks the library-design question ("is save-only inertness intended?"), this
issue asks the harness-measurement question. This plan is deliberately written
to be correct under all three of #693's possible resolutions; see No-Gos.

## Prior Art

| Ref | Relevance |
|---|---|
| **#580 / PR #582** (merged 2026-08-17) | Shipped V0: `ValidityField`, `SupersessionProtocol`, three-layer assembler gating. Its plan's `[EXTERNAL]` No-Go is the explicit deferral that created this debt. |
| **#586** (open) | The issue this one blocks. Owes the n=500 before/after. Its own body states the run "would measure nothing today" — this plan is the fix for that. |
| **#693** (open) | Same audit. Save-only gating inertness as a library question. Overlaps; not a prerequisite (see No-Gos). |
| **#560 / PR #589** (merged 2026-08-19) | M1 provenance journal. Shipped `ProvenanceJournal` as a *primitive* with `supersede()` (`recipes/provenance_journal.py:706`) — a second producer surface. It satisfied #586's stated precondition on paper only, because nothing wired it into the harness. |
| **#588 / PR #601** (merged 2026-09-04) | Moved supersession membership into `SUPERSEDE_LUA` and added the combined `save_and_supersede` entry point this plan uses. Directly relevant: it is the reason a producer is one atomic call rather than a hand-assembled pipeline. |
| **#606 / PR #638** | Routed the journal's annotate-and-close through `SupersessionProtocol` — i.e. the two producer surfaces already converge on one implementation, so choosing `SupersessionProtocol` costs no coverage. |
| **#489** (`--extraction` axis) | The structural precedent this plan copies wholesale: a selectable *ingest arm* as a CLI axis, with `ARM_CHOICES`, per-arm stats, and an arm label baked into the report filename. `tests/benchmarks/extraction_axis.py`. |
| **#514** (`collapse_to_ranking_unit`) | The repo's own gold-blindness doctrine, and the direct precedent for this plan's design decision. That function takes no `relevant_ids` parameter *by construction*, so the answer key structurally cannot influence ranking. The same argument shape is what makes a label-blind producer defensible. |
| **#455 / #484 / #462** | Prior harness axes (vector diagnostic, graph arm), each of which documented in-report exactly what its number does and does not mean. Precedent for this plan's write-up obligation. |

**Why previous fixes failed:** there is no previous *fix* — this is the first
producer. But there is a previous *precondition claim* that failed: #586 declared
itself "runnable once #560 or #564 lands", and #560 landed. The failure mode was
treating "a primitive exists" as "a call site exists". This plan's success
criteria are therefore written as observed exclusion-set cardinality on a real
run, not as "the producer is implemented".

## Research

No WebSearch was run. This work is entirely internal: it wires two shipped
popoto primitives (`SupersessionProtocol`, `ValidityField`) into an in-repo
benchmark harness over a corpus already vendored as a fixture. No external
library, API, or ecosystem pattern is introduced. The one external artifact
involved — the LongMemEval-S schema published by `xiaowu0162/longmemeval-cleaned`
— was read directly from the committed fixture rather than from documentation,
which is strictly better evidence.

## The Design Decision (what reading this plan adopts)

**This plan adopts the faithful-modeling reading, and enforces it structurally
by making the producer label-blind.** The scored label — `question_type`, and in
particular the value `knowledge-update` — is never an input to the producer. It
is used only as a post-hoc reporting breakdown, in the same place
`--question-type` already slices results.

### Why, in three steps

**1. Driving on `question_type` buys almost nothing, and costs everything.**

The teaching-to-the-test objection is not a matter of taste here; it is a
question of what the category actually tells you. `question_type` is annotated
per *question*, not per *turn*. Knowing an item is `knowledge-update` tells you
that somewhere in its haystack a fact changed. It does not tell you **which
session carries the stale claim and which carries the current one** — and that
is exactly the pair a producer must identify. So a category-driven producer
still needs a within-haystack pair-selection heuristic, identical to the one a
label-blind producer needs. The category would only gate *whether* to run that
heuristic.

That is a bad trade. It adds no information the producer actually consumes,
while making the resulting number un-defendable: the ingest arm would behave
differently on the 78 items the benchmark scores as updates than on the other
422, driven by an annotation no deployed system receives. Under the issue's own
framing, the delta would then characterize the annotation, not the system.

**2. The repo already has a doctrine for this, and it is structural, not
promissory.**

`collapse_to_ranking_unit` (`external_base.py:271-330`, issue #514) takes no
`relevant_ids` parameter *by construction*, and its docstring says why: "the
answer key cannot reach this function, so it cannot influence which ID a record
emits." The defect it replaced was precisely a gold-consulting path that
inflated Recall@K. This plan applies the same construction to the ingest arm:
the producer is given `(content, session_id, session_date)` and nothing else.
`BenchmarkItem.relevant_ids` and `metadata["question_type"]` are not in its
signature, so no future edit can quietly start consulting them without changing
the signature — and a test asserts that signature.

**3. The corpus does carry an ingest-visible temporal signal, and it is not the
label.**

The issue notes that `haystack_dates` "exists in the raw HuggingFace schema but
is never parsed into `BenchmarkItem`". Verified: it is present on every fixture
record and dropped by `_parse_record`. Those are per-session wall-clock
timestamps (e.g. `"2023/05/20 (Sat) 02:21"`), parallel to
`haystack_session_ids`. They are exactly the signal a deployed agent memory
system *does* have — every write carries a time — and they are orthogonal to
what the benchmark scores. Parsing them is therefore not a concession to the
test; it is closing a gap where the harness was discarding a real ingest input.

Direction of time is the one thing a supersession producer cannot make up, and
this gives it honestly.

### What the producer actually is

A designed heuristic — the issue is right that no producer can be "neutral", and
this plan does not claim otherwise. The claim is narrower and checkable: it is
**label-blind**, meaning the benchmark's answer key and scored category are
structurally outside its inputs.

Concretely (details in Technical Approach): within one item's haystack, group
written records by a content-derived identity; where a group has more than one
member, order by the session's `haystack_dates` timestamp and route the writes
through `SupersessionProtocol.save_and_supersede` in that order, so the latest
claim is open and the earlier ones are closed. Groups of size one are written
with a plain `.save()` and never enter the validity axis.

The identity function is the whole heuristic and the whole risk. It is
deliberately conservative — a high-precision, low-recall rule that supersedes
rarely and predictably — because the gate is subtractive: a false supersession
*removes* a record from retrieval and can only cost recall, including on gold
sessions. Under-producing yields a small, honest delta; over-producing yields a
large, meaningless one.

## What the resulting delta does and does not establish

This section is a deliverable, not a caveat. It is written here so that #586
copies it verbatim into the published report rather than inventing a weaker
version under deadline.

### Three arms, because two would conflate

A two-arm (baseline vs. producer+gating) comparison cannot attribute its delta,
because the producer and the gate are two changes shipped together. The run is
therefore three arms over the identical item sample:

| Arm | Model declares `ValidityField` | Producer runs | `Defaults.VALIDITY_GATING_ENABLED` |
|---|---|---|---|
| **A — baseline** | no | no | n/a |
| **B — producer, gate off** | yes | yes | `False` |
| **C — producer, gate on** | yes | yes | `True` (default) |

- **A → B** isolates everything the change does *other than gate*: the extra
  per-save Lua command, the field declaration, the producer's write ordering.
  On a recall metric this should be ~0; a non-zero A→B is a finding about the
  harness, not about validity, and must be reported before C is read at all.
- **B → C** is the only pair that isolates the gate. Both arms have identical
  stored state; they differ only in whether the three gating layers subtract.

### What C − B does establish

- That V0's gating machinery, on a real corpus at real scale, subtracts the
  records a producer closed and does not subtract records it did not — i.e.
  the gate is live and its effect is measurable rather than structurally nil.
  That is the specific thing #692 exists to make possible.
- The *sign and magnitude of the retrieval cost or benefit of subtracting
  superseded records*, **conditional on this producer**. If C − B is negative,
  the gate is removing records the retriever wanted; that is a real finding
  about subtractive gating and must be published, per #586's criterion 4.
- Operational facts worth having regardless of sign: exclusion-set cardinality
  per item, per-item supersession counts, and the retrieval-latency cost of the
  two extra `ZRANGEBYSCORE` reads per `assemble()`.

### What it does not establish

- **It is not "V0 validity gating improves LongMemEval-S by X".** V0 ships no
  producer. Every number here is a property of the pair (this heuristic, V0's
  gate), and the heuristic is a harness artifact that nothing in `src/` uses.
  Attributing the delta to V0 alone is the metric-attribution error the issue
  names, and this plan's write-up must refuse it in those words.
- **It is not an upper bound, or a lower bound.** A better identity function
  would produce a different number in an unknown direction. Nothing here
  brackets the achievable effect.
- **It says nothing about the `knowledge-update` category specifically.**
  Per-category breakdowns will be reported because they are cheap and
  interesting, but with n=78 in the committed n=500 baseline, a per-category
  delta on a small effect is not separable from noise, and the plan does not
  pretend otherwise. Report the breakdown; draw no category-level conclusion
  without an interval that excludes zero.
- **It is not comparable to any judged-accuracy number.** Metric-family
  doctrine: the committed n=500 LongMemEval-S baseline is recall-family. The
  three arms are compared to each other within that family and to nothing else.
  `--judged` is explicitly out of scope for this issue (see No-Gos).
- **It does not resolve #693.** Whether save-only inertness is the right library
  default is untouched by this work; the producer here is an explicit imperative
  caller, which is exactly the shape #693 questions.

### Environment reporting

Every number produced under this plan — spike, demonstration run, or published
arm — carries Python version, redis-py version, platform, Redis DB, and the
baseline commit SHA, per repo doctrine. Numbers from different redis-py versions
are not compared.

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

### Key Elements

### Flow

### Technical Approach

## Failure Path Test Strategy

## Test Impact

## Rabbit Holes

## Risks

## Race Conditions

## No-Gos (Out of Scope)

## Documentation

## Success Criteria

## Step by Step Tasks

## Verification

## Critique Results

## Open Questions
