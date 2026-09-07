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

Two spikes, both resolved. Appetite is Medium (cap 4); two sufficed.

### spike-1: Declaring `ValidityField` on the harness models is inert on the existing path

- **Assumption**: adding `validity = ValidityField()` to the four benchmark
  model variants does not perturb the committed lexical/hybrid/vector/graph
  behavior, and `Defaults.VALIDITY_GATING_ENABLED` is a usable ablation switch
  reaching *all* gating layers.
- **Method**: code-read.
- **Result / Confidence: high.**
  1. `on_save` (`validity_field.py:1133-1190`) queues one extra
     `EVAL SUPERSEDE_LUA` (mode `"open"`: three `ZADD NX`) onto the *same*
     internal pipeline `Model.save()` already executes
     (`models/base.py:1614-1629`). **Zero extra round trips per save.**
     `pre_save_validate` does an eager `ZSCORE` only when the caller passes a
     non-`None` `validity=` value (`:1215-1216`); the harness never does, so it
     returns immediately.
  2. `ContextAssembler` auto-mode resolution (`context_assembler.py:1499-1582`)
     branches purely on BM25/embedding field presence. `_validity_field_name` is
     detected separately at `:1401`/`:1437-1438` and never enters mode
     resolution. **Mode resolution is unchanged** — lexical stays lexical,
     hybrid stays hybrid.
  3. The kill switch reaches both layers and is read **at call time** at exactly
     two sites, neither memoized: `decaying_sorted_field.py:602` (in
     `validity_gate_args`, which returns the empty-string triple → `gate = false`
     inside `DECAY_SCORE_LUA`, byte-identical to pre-#580) and
     `context_assembler.py:1683` (in `_resolve_excluded_keys`, returning `None`,
     which makes `_scope_by_validity` a passthrough at `:1724-1725`).
     Note the correction to the issue's mental model: `validity_gate_args` lives
     in `fields/decaying_sorted_field.py:576-616`, not in the assembler.
  4. `warn_if_ttl` (`:1099-1127`) fires only when `model._meta.ttl is not None`.
     The harness models declare no `Meta`, so **no per-item log noise**.
  5. With gating on, `assemble()` costs **two extra `ZRANGEBYSCORE` round
     trips**, and `record.delete()` costs five extra `ZREM`/`HDEL` plus one
     `SCAN {prefix}:open:*`.
- **Impact if false**: would have forced a separate model variant per arm
  instead of one variant plus a flag. It is not false.

### spike-2: `save_and_supersede` works on the harness model shape, and the gate then excludes

- **Assumption**: `SupersessionProtocol.save_and_supersede` composes with
  `AutoKeyField` + `KeyField` + `DecayingSortedField(partition_by=...)` +
  `ConfidenceField` + `BM25Field`, and the assembler actually drops the
  superseded record.
- **Method**: prototype, against a model built to the exact shape of
  `_build_external_model_class(with_bm25=True, with_embedding=False)` plus a
  `ValidityField`.
- **Environment**: Python 3.12.14, redis-py **7.1.1**, Darwin 25.6.0 arm64,
  Redis **DB 11**, git HEAD `4ac805f0`. (Note the redis-py version differs from
  the 8.1.0 the issue's own probe used; nothing measured here is version
  sensitive, but per doctrine it is stated rather than elided.)
- **Result / Confidence: high.**
  1. Five plain `.save()`s → `resolve_excluded_keys` returns `set()`, **size 0**.
     Reproduces the issue's baseline exactly.
  2. **The first claim must also go through the protocol.** `a1.save()` then
     `save_and_supersede(a2, identity_key=…)` returns `closed_key=None` and the
     exclusion set stays **0** — `a1` was never registered as the incumbent.
     `save_and_supersede(b1, …)` then `save_and_supersede(b2, …)` returns
     `closed_key=None` on the first (correct: first claim opens) and
     `closed_key=<b1 key>` on the second, exclusion set **1**. This is the single
     most load-bearing finding for the implementation: **every member of an
     identity group, including the first, routes through `save_and_supersede`.**
  3. `ContextAssembler(retrieval_mode="auto").assemble()` on the correctly
     registered pair returned **only `b2`**; `b1` absent. `_effective_mode`
     resolved to `"lexical"` as expected.
  4. With `Defaults.VALIDITY_GATING_ENABLED = False`, `b1` **came back**. The
     three-arm design above is therefore executable as a runtime flag rather
     than a second code path.
  5. No exceptions or warnings on any path. Save overhead with vs. without the
     field, 100 saves × 3 trials: 1.264–1.613 ms/save vs. 0.884–1.062 ms/save.
     Treat as order-of-magnitude only (no warmup, small N); direction is
     consistent — roughly +20–50% per save.
- **Impact if false**: would have blocked the whole approach. It is not false.

### spike-2 side finding — a real hazard this plan must handle

The spike surfaced something not asked for, and it changes the implementation:
**`_build_external_model_class`'s per-item key isolation does not hold for
special-use field keys.** The builders rename `__name__`/`__qualname__` *after*
the class body, but `_meta.db_class_key` is captured by the metaclass at class
creation from the original name `ExternalBenchmarkMemory`, and
`Field.get_special_use_field_db_key` (`fields/field.py:630`) builds from
`model._meta.db_class_key`. Only `BM25Field` reads `cls.__name__` lazily and so
actually picks up the rename.

Verified against the source: the validity keys for *every* item in a run would
collapse onto the same six key names —

```
$ValidityF:ExternalBenchmarkMemory:validity:valid_from   (and :invalid_at,
    :ingested_at, :chain:fwd, :chain:rev, :open:{digest})
```

For the pre-existing fields this is masked, because `DecayingSortedField` and
`KeyField` are `partition_by`/value scoped and every query filters on
`agent_id`. **`ValidityField` is deliberately not a `SortedFieldMixin` and has
no `partition_by`**, so it is not masked: `resolve_excluded_keys` reads the
whole ZSET and would return every record superseded by every *previous item* in
the run. And the harness's `teardown()` cannot clean it — its two `SCAN`
patterns are `"{class_name}:*"` (anchored at the start, while these keys start
`$ValidityF:`) and `"*{agent_prefix}*"` (the agent id appears nowhere in them).

Consequences if unhandled, in order of severity: the exclusion set grows
linearly across a 500-item run, inflating `retrieval_ms` monotonically and
making the latency figure an artifact of item order; the ZSETs grow to O(all
records in the run) — on the order of 10^5 members — with no bound; and the keys
survive the run. The plan's answer is in Technical Approach step 5: teardown
explicitly deletes `ValidityField.get_all_keys(...)` plus a `SCAN` of the
`:open:*` pointers. The deeper defect — a docstring claiming per-item isolation
that only one field type honors — is out of scope here and gets filed
separately (see No-Gos).

## Data Flow

```
run_external.py --dataset longmemeval-s --supersession {none|content-identity}
  │
  ├─ datasets/longmemeval_s.py::_parse_record
  │     NEW: parse haystack_dates → per-turn `session_date` (epoch float)
  │          carried on each history entry, parallel to session_id/turn_id.
  │          question_type continues to land in metadata (reporting only).
  │
  ├─ scenarios/external_base.py::ExternalScenario.setup()
  │     builds the per-item model class (NEW: + `validity = ValidityField()`
  │       when the supersession arm is not "none")
  │     for each turn → _extract_units() → for each unit:
  │        ├─ arm "none":             instance.save()          [today's path]
  │        └─ arm "content-identity": supersession_axis.route_write(...)
  │              ├─ identity = identity_of(unit_text)   ← label-blind
  │              ├─ identity is None (no group)  → instance.save()
  │              └─ identity is not None         →
  │                     SupersessionProtocol.save_and_supersede(
  │                         instance, identity_key=identity,
  │                         at=session_date)
  │        (session_key_map / turn_key_map updated identically in both arms)
  │
  ├─ ExternalScenario.run()
  │     ContextAssembler.assemble(query_cues={"topic": query}, agent_id=…)
  │        ├─ server-side layer: validity_gate_args → DECAY_SCORE_LUA
  │        └─ client-side layer: _resolve_excluded_keys → _scope_by_validity
  │     both no-ops when Defaults.VALIDITY_GATING_ENABLED is False (arm B)
  │     NEW metadata: n_supersessions, n_excluded_keys, exclusion_hit_count
  │
  └─ ExternalScenario.teardown()
        record.delete() loop  (now also ZREMs this record's intervals)
        NEW: delete ValidityField.get_all_keys(cls, "validity").values()
             + SCAN/DEL "{validity_prefix}:open:*"
```

The write ordering matters and is the one race-shaped concern: within an
identity group the writes must reach `save_and_supersede` in ascending
`session_date` order, or a later-dated claim gets closed by an earlier-dated one
(and `ValidityCloseBeforeStartError` may raise). Turns arrive from
`_parse_record` in haystack order, which is *not* guaranteed to be date-ordered.
See Race Conditions.

## Architectural Impact

Confined to `tests/benchmarks/`. No `src/` change. Specifically:

- **New module** `tests/benchmarks/supersession_axis.py`, structured as a direct
  sibling of `extraction_axis.py`: `ARM_CHOICES`, a stats dataclass, the
  identity function, and the write router. Keeping it out of
  `external_base.py` is deliberate — the identity heuristic is the part most
  likely to be revised, and it should be revisable and testable without
  touching the ingest loop.
- **`external_base.py`**: one new constructor kwarg, one conditional field
  declaration, one branch at the write site (`:479`), teardown additions, and
  new result metadata.
- **`datasets/longmemeval_s.py`**: `_parse_record` gains `session_date`. This is
  additive on a dict that already carries `role`/`content`/`turn_id`/`session_id`,
  so LoCoMo and every existing consumer are untouched (LoCoMo simply never sets
  it).
- **`run_external.py`**: one CLI flag pair, arm-labelled output filenames,
  new aggregate reporting rows.
- **No public API change**, no `src/` behavior change, no mypy-ratchet exposure
  (`tests/` is not in `mypy src/`), no new dependency.

The one architectural claim worth stating: this deliberately does **not** add a
producer to `src/`. A shipped auto-producer is #693's question, and answering it
inside a benchmark harness would be the wrong place and the wrong evidence.

## Appetite

**Medium.** The producer, the axis module, the dates parse, the teardown fix,
the three-arm flag, tests, and one *small-n demonstration run* with its
write-up. That is a coherent, shippable unit.

What Medium explicitly excludes: the full n=500 three-arm run and its published
report. That is #586's job and its cost is hours of wall clock plus an
embedding provider. This issue's job is to make that run *mean something*, and
its acceptance criterion 2 is satisfied by a demonstrated non-empty exclusion
set, not by a corpus-scale publication.

## Prerequisites

None blocking. All of the following are already merged and verified present:

- `ValidityField` + `SupersessionProtocol` (#580 / PR #582).
- `save_and_supersede` as a single atomic entry point (#588 / PR #601).
- The three-layer gate and its call-time kill switch (spike-1).
- The `--extraction` axis as a structural template (#489).
- A committed LongMemEval-S fixture carrying `haystack_dates`
  (`tests/benchmarks/datasets/fixtures/longmemeval_s_sample.json`).

One **non-blocking gap**: that fixture's three records are all
`single-session-user`, so it contains no `knowledge-update` item and — more to
the point — no two sessions asserting the same fact at different dates. A
fixture item that exercises the producer must be added (Task 6). This does not
block anything; it is work inside this plan.

## Solution

### Key Elements

1. **`tests/benchmarks/supersession_axis.py`** — new. Owns `ARM_CHOICES`, the
   label-blind identity function, the write router, and per-run stats. Sibling
   in shape to `extraction_axis.py`.
2. **`session_date` on parsed turns** — `_parse_record` reads `haystack_dates`
   and attaches a parsed epoch to each history entry.
3. **Conditional `ValidityField` + branched write site** in `external_base.py`.
4. **Teardown that actually cleans the validity keyspace** — required by the
   spike-2 side finding, not optional.
5. **`--supersession` CLI axis + arm-labelled artifacts** in `run_external.py`,
   plus `--no-validity-gating` for arm B.
6. **Observability**: exclusion-set cardinality and supersession counts reach
   `ScenarioResult.metadata` and the aggregate report, so acceptance criterion 2
   is *demonstrated by an emitted number*.

### Flow

Per benchmark item: parse turns (now with dates) → build model class (with
`ValidityField` iff the arm is active) → for each written unit, ask the identity
function for an identity; no identity means a plain `.save()`, an identity means
`save_and_supersede` at that session's date → retrieve through the assembler,
which gates or does not gate per the runtime flag → emit result metadata
including exclusion-set size → teardown deletes records *and* the six validity
keys *and* the open pointers.

### Technical Approach

**1. `supersession_axis.ARM_CHOICES = ("none", "content-identity")`**, default
`"none"`. `"none"` is byte-identical to today: no `ValidityField` declared, no
protocol call, so every committed artifact remains the baseline it was produced
under — the same contract `--extraction raw` holds.

**2. The identity function — the whole heuristic, isolated and label-blind.**

```python
def identity_of(unit_text: str) -> Optional[tuple[str, str]]:
    """Return a (subject, predicate) identity, or None to write plainly.

    Label-blind by construction: takes text and nothing else. The caller has
    the answer key and the question_type; this function's signature makes it
    impossible for either to arrive here.
    """
```

Rule, deliberately high-precision / low-recall:

- Split `unit_text` into sentences with a pinned stdlib-only splitter (no new
  dependency). Take the **first** sentence matching the pattern below; at most
  one identity per written unit, so a unit can join at most one group.
- Match first-person stateful assertions only: a leading `I` (case-folded)
  followed by a verb drawn from a **pinned** state-verb set, optionally followed
  by a pinned preposition. The predicate token is `verb` or `verb_prep`
  (`"I now work at Acme"` → `("i", "work_at")`; `"I live in Boston"` →
  `("i", "live_in")`). Everything else returns `None`.
- The subject is the literal `"i"` — per-item namespaces are already
  agent-scoped, so no cross-speaker collision is possible within an item.
- The returned pair is handed to `SupersessionProtocol.save_and_supersede` as
  `identity_key=`, which normalizes and hashes it to a 16-hex digest itself
  (#580 plan D7) — raw text never reaches the keyspace.

Why this shape and not something cleverer: the gate is subtractive, so identity
*recall* costs nothing but identity *precision* errors delete live records and
can only depress the measured number. A rule that fires on a slot ("where I
work") and treats the latest dated claim as current is the narrowest thing that
still models a genuine update. It will be wrong sometimes (a person can hold two
jobs); that is a stated property of the heuristic, reported alongside the number,
not a bug to be patched by widening it.

The verb and preposition sets are **magic numbers in the CLAUDE.md sense** —
pinned in-repo for experimental tuning, not exposed as constructor kwargs or
CLI options. They live in `supersession_axis.py`, **not** in
`popoto.fields.constants.Defaults`: they are harness-local, and adding them to
`Defaults` would both leak a benchmark artifact into the library surface and
trip the `tests/benchmarks/test_defaults_sync.py` registration gate for no
benefit.

**3. The write router.**

```python
def route_write(instance, *, identity, at, stats) -> bool:
    if identity is None:
        stats.plain_writes += 1
        return bool(instance.save())
    result = SupersessionProtocol.save_and_supersede(
        instance, identity_key=identity, at=at
    )
    stats.identity_writes += 1
    if result.closed_key:
        stats.supersessions += 1
    return True
```

Two non-obvious requirements, both from spike-2:

- **The first claim of a group also goes through `save_and_supersede`.** A plain
  `.save()` never registers an incumbent, so a first-claim-plain / second-claim-
  supersede sequence closes nothing and produces an exclusion set of 0 — the
  exact defect this issue exists to fix, reintroduced one level down. There is
  no branch on "is this the first"; every identity-bearing write takes the same
  call, and `closed_key is None` on the first is the expected, counted outcome.
- **`at=` must be the session's date, not `time.time()`.** Passing wall-clock
  now would make every interval open-and-close within the same run second,
  which both destroys the bitemporal structure and risks
  `ValidityCloseBeforeStartError` on ties.

Exceptions from the protocol (`SupersedeDeclinedError`,
`ValidityMemberAbsentError`, `ValidityCloseBeforeStartError`) are caught per
unit, counted in `stats.failures`, and logged — matching how the ingest loop
already treats a failed save — but the count is **reported in the run artifact**,
so a producer that is silently failing cannot masquerade as a producer that
found nothing.

**4. Date parsing and ordering.** `_parse_record` parses `haystack_dates[i]`
(format `"%Y/%m/%d (%a) %H:%M"`) into an epoch float and attaches it to each turn
of session `i` as `session_date`. A missing or unparseable date yields `None`,
and a `None` date disables the producer for that turn (it falls back to a plain
`.save()`) — the producer never invents a timestamp. The ingest loop then writes
identity-bearing units **in ascending `session_date` order**; see Race
Conditions for why the corpus order is not sufficient.

**5. Teardown.** After the per-record delete loop and before the existing SCANs:

```python
if self._supersession_arm != "none":
    keys = ValidityField.get_all_keys(self._model_class, "validity")
    get_REDIS_DB().delete(*keys.values())
    # open-claim pointers are per-digest; SCAN the prefix
    prefix = ValidityField.get_prefix_db_key(self._model_class, "validity").redis_key
    # SCAN f"{prefix}:open:*" → DEL in batches
```

This is mandatory, not hygiene: per the spike-2 side finding these six keys are
**shared across every item in a run** (the post-hoc `__name__` rename does not
reach `_meta.db_class_key`), and the harness's existing two SCAN patterns cannot
match them. Without this the exclusion set grows monotonically across the run.

**6. CLI and reporting.** `--supersession {none,content-identity}` (default
`none`) and `--no-validity-gating` (sets `Defaults.VALIDITY_GATING_ENABLED =
False` once at startup, before any assemble — spike-1 confirms both layers read
it at call time). Arm C is `--supersession content-identity`; arm B adds
`--no-validity-gating`; arm A is the flagless default. Artifacts gain a
`_sup-{arm}` / `_nogate` filename label exactly as `--extraction` does, so the
three arms cannot overwrite each other.

The report gains, per item and in aggregate: `units_seen`,
`units_with_identity`, `identity_groups`, `supersessions`, `producer_failures`,
`n_excluded_keys` (cardinality of the gate's exclusion set at retrieval time),
and `n_excluded_hits` (how many of *this item's* retrieved candidates the gate
actually removed). The last two are what turn acceptance criterion 2 from an
assertion into an observation, and `n_excluded_hits` is the one that
distinguishes "the gate had state" from "the gate did something".

A **firing-rate caveat that must be measured, not assumed**: under
`--extraction raw` (the committed baseline arm) a written unit is an entire
conversational turn, so the sentence-level identity rule may match rarely. Under
`--extraction heuristic`/`claude` the units are atomic facts and it should match
far more often. The demonstration run therefore reports the firing rate
explicitly; a near-zero rate on the raw arm is a publishable finding about the
raw arm, and it is precisely why Task 6's fixture item exists as an independent
proof that the mechanism works.

## Failure Path Test Strategy

### Exception Handling Coverage

- `save_and_supersede` raising `SupersedeDeclinedError` mid-item: assert the
  item completes, `stats.failures` increments, and the remaining turns still
  write — one failed unit never aborts an item (mirrors the existing
  `except Exception` around `.save()` at `external_base.py:479`).
- `ValidityCloseBeforeStartError` from an out-of-order `at`: assert the ordering
  guarantee prevents it, by constructing a fixture whose `haystack_dates` are
  deliberately non-monotonic relative to haystack order and asserting the run
  completes with the *latest-dated* claim open.
- Teardown key-deletion failing (Redis error) must not abort teardown of
  subsequent items.

### Empty/Invalid Input Handling

- `haystack_dates` absent, shorter than `haystack_sessions`, or unparseable →
  `session_date is None` → producer disabled for those turns, run completes,
  arm degrades to plain saves. Assert no exception and a counted stat.
- Empty / whitespace unit text → `identity_of` returns `None`.
- An item where no unit yields an identity → zero supersessions, exclusion set
  0, result still `ok`. This is a legitimate outcome, not an error.

### Error State Rendering

- The aggregate report must print `producer_failures` even when zero, so a
  reader can distinguish "producer found nothing" from "producer errored on
  everything". A run with `supersessions == 0` must say so prominently rather
  than reporting a flat delta as if it were a measurement.

## Test Impact

New, in `tests/benchmarks/`:

- `test_supersession_axis.py`
  - `identity_of` signature takes exactly one positional parameter — the
    structural label-blindness assertion, in the spirit of #514's gold-blind
    test. Also assert by `inspect.signature` that neither `relevant_ids` nor
    `question_type` appears in `identity_of` or `route_write`.
  - Identity rule table: positive cases, negative cases, at-most-one-identity
    per unit, `None` on empty text.
  - `route_write` sends the **first** claim of a group through
    `save_and_supersede` (regression guard for the spike-2 finding — a test that
    asserts an exclusion set of ≥1 after two identity-bearing writes, which the
    plain-first-save bug would fail).
- `test_external.py` additions
  - `_parse_record` attaches `session_date` to every turn; LoCoMo items are
    unaffected (no `session_date` key required).
  - A fixture-driven end-to-end item under arm `content-identity` produces a
    **non-empty** exclusion set and the superseded record is absent from the
    assembled records while the successor is present.
  - Arm `none` is byte-identical to today: no `ValidityField` on the model
    class, no validity keys in Redis after setup.
  - `--no-validity-gating` restores the superseded record.
  - Teardown leaves no `$ValidityF:*` key behind for the model — the direct
    regression test for the key-leak hazard.

Existing tests expected unchanged: all of `test_external.py`'s current cases,
`test_harness.py`, `test_gold_blind_scoring.py`. Arm `none` being the default is
what makes that a real expectation rather than a hope.

Suite run under `POPOTO_TEST_DB` per lane; environment stated with every count.

## Rabbit Holes

- **Building a real fact extractor.** The temptation is to reach for an LLM to
  get true (subject, predicate) identities. That is #489's axis, already shipped,
  and this producer composes with it — it is not this issue's job to improve it.
- **Fixing the per-item key-isolation defect properly.** The right fix is
  probably making the model builders set `_meta.db_class_key` (or constructing
  the class with the right name), which touches every field type's keyspace and
  invalidates comparability with committed artifacts. Out of scope; file it.
- **Widening the identity rule until the delta gets big.** Tuning a heuristic
  against the number it produces is the teaching-to-the-test failure re-entering
  through the back door. The rule is fixed before the demonstration run and any
  later change is a separate, disclosed revision.
- **Running the n=500 three-arm comparison inside this issue.** That is #586.
- **Answering #693.** Tempting, since the producer makes the inertness vivid.
  Not this issue.
- **Reconciling with `ProvenanceJournal`'s `supersede()`.** Two producer
  surfaces exist; #606/PR #638 already converged them onto
  `SupersessionProtocol`. Using the protocol directly is correct and choosing
  between them is not a live question.

## Risks

### Risk 1: The producer fires too rarely on the raw arm to move any number

Most likely outcome, and the one to plan for. Mitigation: the firing rate is a
reported statistic, Task 6's fixture proves the mechanism independently of
corpus firing rate, and a near-zero rate is written up as a finding about
verbatim-turn ingestion rather than hidden. #586 can then choose to run its
before/after on an extraction arm where units are atomic.

### Risk 2: A false supersession deletes a gold session's only evidence

The gate is subtractive, so precision errors cost recall directly. Mitigations:
the conservative rule; `n_excluded_hits` makes every actual removal countable;
and the A→B→C decomposition means a recall drop is attributable to the gate
rather than smeared across the whole change.

### Risk 3: Cross-item validity key sharing corrupts the measurement

Addressed by Technical Approach step 5. The residual risk is a *crashed* run
leaving keys behind for the next run to inherit. Mitigation: the demonstration
must run on a dedicated DB and report `n_excluded_keys` at the first item, which
must be 0 — a non-zero first-item exclusion set is proof of contamination.

### Risk 4: Per-save overhead distorts the latency figures

Spike-2 measured +20–50% per save (rough). Ingestion latency is not the reported
metric (`retrieval_ms` is), but the run gets slower. Stated, not mitigated.

### Risk 5: `Defaults.VALIDITY_GATING_ENABLED` is process-global

Setting it mutates a module-level default for the whole process. Safe because
one arm runs per process invocation. Mitigation: set it once in `main()` before
any scenario is constructed, never per item, and echo its value into the report
header so an artifact always states which arm produced it.

## Race Conditions

### Race 1: Corpus order is not date order

`_parse_record` flattens `zip(haystack_session_ids, haystack_sessions)` in
haystack order. Nothing guarantees `haystack_dates` is ascending in that order —
the fixture's three records happen to be ascending, which is exactly the kind of
sample-of-three that should not be generalized from. If a later-dated claim is
written before an earlier-dated one, the earlier write supersedes the later, the
*stale* claim ends up open, and `save_and_supersede` may raise
`ValidityCloseBeforeStartError` on the close.

**Prevention:** the ingest loop must not rely on corpus order. Identity-bearing
writes are ordered by `session_date` ascending before being routed. Because
`session_key_map`/`turn_key_map` are keyed by id rather than position, and graph
mode's adjacency edges are built per session from `prev_by_session`, reordering
*across* sessions is safe; reordering must never occur *within* a session, since
graph adjacency is positional. Implementation: sort sessions by date, preserve
turn order inside each session.

### Race 2: Reading the exclusion set after retrieval

`resolve_excluded_keys` is documented as a point-in-time snapshot. The harness is
single-threaded and ingest fully precedes retrieval per item, so there is no
concurrent writer. The reported `n_excluded_keys` must be read *at the same
`as_of`* the assembler used, not at a later wall-clock instant, or the reported
cardinality will not be the one that gated.

### Race 3: Concurrent worktree lanes on one Redis DB

Standing repo hazard, made worse here because the validity keys are not
agent-scoped and not model-name-isolated (spike-2 side finding): a second
concurrent run writes into *the same six keys*. The demonstration run must pin
its own `POPOTO_TEST_DB` / `REDIS_URL` database and say which one in the report.

## No-Gos (Out of Scope)

- **The n=500 three-arm before/after run and its published report** — #586.
- **Any `src/` change**, including any auto-producer or warning for the inert
  save-only case — #693 owns that decision. This plan is written to stay correct
  under all three of #693's outcomes: if save-only stays inert the producer is
  required as designed; if it gains auto-detection the producer becomes
  redundant and the arm is deleted; if it gains a warning nothing here changes.
- **Fixing per-item key isolation across all field types** — file a separate
  investigation issue with the spike-2 side finding (`_meta.db_class_key`
  captured pre-rename; only `BM25Field` honors the rename; the docstring at
  `external_base.py:202-220` claims isolation that does not hold).
- **`--judged` interaction.** Judged mode is a different metric family; mixing
  it in would invite exactly the cross-family comparison doctrine forbids.
- **LoCoMo.** The producer is dataset-agnostic in principle, but LoCoMo carries
  no per-session dates and is scored at turn granularity. Not measured here.
- **Tuning the identity rule against observed deltas.**

## Documentation

- `tests/benchmarks/README.md`: a `--supersession` axis section mirroring the
  existing `--extraction` section, including the three-arm table and the
  verbatim "what the delta does and does not establish" paragraph.
- Module docstring in `supersession_axis.py` carrying the design decision and
  its justification, so a reader of the code meets the reasoning at the code.
- A comment at the `ValidityField` declaration in `external_base.py` pointing at
  this plan and at #693.
- No user-facing `docs/` change: this is harness-internal. `mkdocs build
  --strict` must still pass.

## Success Criteria

Mapped to #692's four acceptance criteria.

1. **AC1 — reading stated.** The Design Decision section above states
   faithful-modeling, label-blind, with the three-step justification. Enforced
   in code by `identity_of`'s signature and asserted by a test.
2. **AC2 — non-empty exclusion state demonstrated, not asserted.** A run on the
   extended fixture under `--supersession content-identity` emits
   `n_excluded_keys >= 1` and `n_excluded_hits >= 1` into the result artifact,
   and the superseded record is verifiably absent from the assembled records
   while its successor is present. The emitted numbers are pasted into the PR
   description, not paraphrased.
3. **AC3 — the write-up exists.** The "does and does not establish" section is
   in this plan, in `tests/benchmarks/README.md`, and quoted in the PR body,
   ready for #586 to copy verbatim.
4. **AC4 — environment with every number.** Python version, redis-py version,
   platform, Redis DB, and baseline SHA accompany every figure in the spikes
   above, in the demonstration run, and in the PR body.

Additionally:

5. Arm `none` (the default) is byte-identical to the committed baseline path —
   no `ValidityField` declared, no validity keys created, existing tests green.
6. `--no-validity-gating` demonstrably restores superseded records (arm B is
   executable).
7. No `$ValidityF:*` keys survive teardown.
8. Full suite green; `ruff check src/` clean; `black --check src/ tests/` clean;
   `scripts/mypy_ratchet.py` unchanged (no `src/` change).

## Step by Step Tasks

## Verification

## Critique Results

## Open Questions
