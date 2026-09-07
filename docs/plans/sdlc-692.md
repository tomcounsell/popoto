---
status: Ready
type: feature
appetite: Medium
owner: Valor Engels
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/692
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-07T12:04:34Z
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
  2. `ContextAssembler` auto-mode resolution
     (`src/popoto/recipes/context_assembler.py:1499-1582` — note the path: the
     assembler lives under `recipes/`, not `models/`; all bare
     `context_assembler.py` citations in this plan resolve there)
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

### Scope decision: the CLI/reporting surface is in, deliberately (critique C3)

Task 8 builds more than the bare minimum AC2 needs — the minimum is an arm
toggle and a gate toggle. It additionally ships the `_sup-{arm}` / `_nogate`
artifact filename labels and the new aggregate rows. **This is an accepted,
intended scope decision, not scope creep**, on three grounds:

1. **Correctness, not convenience.** Without an arm label in the filename the
   three arms *overwrite each other on disk*. A three-arm design whose artifacts
   collide is not a three-arm design, and the collision would be discovered by
   #586 under run pressure, after hours of wall clock had already been spent.
2. **It is a copy, not an invention.** `extraction_axis.py` already established
   `ARM_CHOICES` + per-arm filename labels + per-arm stats rows (#489). Mirroring
   it costs a few dozen lines and adds no new convention to the harness; building
   a smaller ad-hoc toggle now and the real axis later would cost more in total
   and leave two conventions in the tree meanwhile.
3. **The aggregate rows are this issue's own deliverable.** `n_excluded_keys`,
   `n_excluded_hits`, `supersessions` and `producer_failures` are what turn AC2
   from an assertion into an observation (Success Criterion 2) and what make a
   silently-failing producer distinguishable from a producer that found nothing.
   They are not #586 infrastructure that happens to land early.

What remains firmly #586's: the n=500 run itself, the embedding-provider spend,
the published report, and any conclusion drawn from a corpus-scale delta.

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

**5. Teardown.** *(Shape settled per critique C1 — this is the single
authoritative statement; Task 4 restates it and must not diverge.)* After the
per-record delete loop and before the existing SCANs:

```python
# Guard on the DECLARED FIELD, not on the arm. Cleaning is driven by what the
# model class actually has, so a future arm that declares validity is cleaned
# without editing this branch, and an arm-"none" item takes a real no-op rather
# than a DEL of names that were never written.
if self._model_class is not None and "validity" in self._model_class._meta.fields:
    try:
        keys = ValidityField.get_all_keys(self._model_class, "validity")
        get_REDIS_DB().delete(*keys.values())
        # open-claim pointers are per-digest; SCAN the prefix
        prefix = ValidityField.get_prefix_db_key(
            self._model_class, "validity"
        ).redis_key
        # SCAN f"{prefix}:open:*" → DEL in batches
    except Exception:
        pass  # matches the existing teardown idiom (external_base.py:692-729)
```

Three points the builder must not re-litigate:

- **The predicate is `"validity" in self._model_class._meta.fields`, not
  `self._supersession_arm != "none"`.** Both are correct today (the field is
  declared iff the arm is active), but the field-presence form is the one that
  cannot drift if a future arm changes that coupling.
- **It must not be made unconditional.** `get_all_keys` / `get_prefix_db_key`
  (`src/popoto/fields/validity_field.py:702`, `:782`) only concatenate
  `_meta.db_class_key` + field name into key *strings*, so an unconditional
  `delete()` would not raise — that is exactly the hazard. A `DEL` of six names
  that arm `none` never created succeeds vacuously, and Verification row 1 ("no
  `$ValidityF:*` key created during a `none`-arm item") would then be checked
  against a keyspace the teardown had just swept regardless. Deleting keys that
  should not exist must never become the mechanism that hides them existing.
- **New code uses `get_REDIS_DB()`.** The surrounding teardown still holds the
  module-level `POPOTO_REDIS_DB` import (`external_base.py:706`, `:710`, `:721`,
  `:725`); do not copy that shape, and do not convert those lines here either —
  the conversion is a separate concern (see CLAUDE.md).

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
  - **Teardown on an arm-`none` item does not raise** and does not issue the
    validity `DEL` at all (critique C1). Assert both halves: no exception, and —
    via a spy on the client, or by pre-seeding a sentinel key under the shared
    `$ValidityF:ExternalBenchmarkMemory:validity:*` names and asserting it
    survives — that arm `none` performs no validity deletion. The second half is
    what keeps the guard honest: a teardown that swept those names
    unconditionally would make Verification row 1 vacuous.
  - The history-ordering helper returns `self.item.history` **unchanged** (same
    object identity or same element order) on arm `none`, and date-sorted on
    arm `content-identity` (critique C2) — asserted directly on the helper, so
    the byte-identity claim does not depend on an end-to-end diff.

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

**Tied dates (critique N1).** `haystack_dates` is minute-precision
(`"%Y/%m/%d (%a) %H:%M"`), so two sessions in one haystack can carry an
identical timestamp. Two facts, both verified in source rather than assumed:

- **`save_and_supersede(at=<equal>)` does not raise.** The `SUPERSEDE_LUA`
  guard is `if start_num ~= nil and close_at < start_num then ... CLOSE_BEFORE_START`
  (`src/popoto/fields/validity_field.py:368-370`) — a *strict* comparison. An
  equal `at` passes validation and stores a **zero-length interval**
  (`invalid_at == valid_from`) on the incumbent.
- **A zero-length interval is always excluded.** Membership is
  `valid_from <= t AND invalid_at > t` (`:47`, `:766-767`), which no `t` can
  satisfy when the two are equal, and the gate excludes on `invalid_at <= now`
  (`:920-932`). So on a tie the incumbent is dropped from retrieval — the
  producer's *intended* outcome, reached without an exception.

What a tie therefore does **not** do is fail loudly; what it *does* do is let
Python's stable sort decide which of the two tied claims is "current", by
haystack list position. That is arbitrary but harmless-by-construction here: the
loser is excluded either way, and which of two same-minute claims is called
current is not a distinction the corpus makes. It is stated rather than
mitigated, and Task 6 adds a tied-timestamp fixture case so the behavior is
pinned by a test instead of by this paragraph.

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

1. **Parse `haystack_dates`.** In
   `tests/benchmarks/datasets/longmemeval_s.py::_parse_record`, read
   `haystack_dates` (format `"%Y/%m/%d (%a) %H:%M"`), parse to an epoch float,
   and attach as `session_date` on every turn dict of the corresponding session.
   Missing / short / unparseable → `None`, never an exception, never a
   substituted timestamp. Update the docstring's schema block to say the field
   is now parsed.
2. **Create `tests/benchmarks/supersession_axis.py`.** `ARM_CHOICES =
   ("none", "content-identity")`; the pinned state-verb and preposition sets
   (with the CLAUDE.md magic-number rationale in a comment, and an explicit note
   that they do **not** belong in `Defaults`); `identity_of(unit_text)` with the
   one-positional-parameter signature; `route_write(...)`; a
   `SupersessionStats` dataclass (`units_seen`, `units_with_identity`,
   `identity_groups`, `plain_writes`, `identity_writes`, `supersessions`,
   `failures`). Module docstring carries the design decision verbatim.
3. **Wire the ingest arm** in `tests/benchmarks/scenarios/external_base.py`:
   new `supersession_arm` constructor kwarg (default `"none"`); declare
   `validity = ValidityField()` on all four model builders **when the arm is
   active**; **when the arm is active**, order sessions by `session_date`
   ascending while preserving intra-session turn order (Race 1); branch the
   write at `:479` through `route_write`; keep `session_key_map` /
   `turn_key_map` / graph-edge behavior identical in both arms.

   **Both "when the arm is active" qualifiers are load-bearing (critique C2).**
   The loop to guard is `for turn in self.item.history:` in
   `ExternalScenario.setup()` (`external_base.py:447`). On arm `none` that
   iteration order must be untouched: it is the insertion order into the
   `DecayingSortedField` and the BM25 index, so reordering it would move
   committed-baseline numbers and break Success Criterion 5 and Verification
   row 1. Implement as `history = self._ordered_history()` where the helper
   returns `self.item.history` unchanged on arm `none` and the date-sorted
   sequence otherwise — one branch, at one place, testable directly.

   **The graph-mode re-save is a second write path and stays one (critique
   N2).** `turn_first_instance.save()` at `external_base.py:522` (graph mode
   only, to attach `prev_turn`) does **not** route through `route_write`, and
   deliberately so: it re-saves a record already written, so it is not a new
   claim and must not open a new interval or close anything. It is safe because
   `ValidityField.on_save` in mode `"open"` writes `valid_from` / `ingested_at`
   / `invalid_at` with `ZADD NX` and only when the member's `invalid_at` is
   absent-or-`+inf` (`validity_field.py:403-410`), so a re-save can neither
   shift an existing interval nor resurrect a closed one. State this in a code
   comment at `:522`; it is the content of the "graph-edge behavior identical in
   both arms" promise, not an omission from it.
4. **Fix teardown** (spike-2 side finding): delete
   `ValidityField.get_all_keys(cls, "validity")` and `SCAN`/`DEL` the
   `{prefix}:open:*` pointers, **guarded on `"validity" in
   self._model_class._meta.fields`** and wrapped in the file's existing
   `except Exception: pass` teardown idiom — exactly the shape in Technical
   Approach step 5, which is authoritative. Do **not** make the delete
   unconditional: on arm `none` those key names were never written, and a
   vacuous `DEL` would mask the very leak Verification row 1 exists to catch.
   The guard must still be safe on error paths — teardown of a later item must
   not be aborted by a Redis failure here — which is what the `except` provides.
   Use `get_REDIS_DB()`, not the module-level `POPOTO_REDIS_DB` the surrounding
   teardown still imports.
5. **Emit the observability metadata** in `ExternalScenario.run()`:
   `n_supersessions`, `n_excluded_keys`, `n_excluded_hits` (read at the same
   `as_of` the assembler used — Race 2), plus the stats dataclass fields.
6. **Extend the LongMemEval-S fixture.** Add one record to
   `tests/benchmarks/datasets/fixtures/longmemeval_s_sample.json` with
   `question_type: "knowledge-update"` and two sessions whose `haystack_dates`
   differ and which assert the same slot with different values (e.g. an earlier
   "I work at X" and a later "I work at Y"). Include a deliberately
   **non-monotonic** date ordering relative to haystack order so Race 1 is
   exercised, **and a third session carrying a `haystack_dates` value tied to
   one of the other two** so the minute-precision tie case (Race 1, N1) is
   pinned by a test: assert the run completes without
   `ValidityCloseBeforeStartError` and that the tied loser is excluded (its
   zero-length interval is never a member). Confirm the three existing records'
   tests still pass.
7. **Add tests** per Test Impact, including the label-blindness signature
   assertion, the first-claim-must-supersede regression guard, the arm-`none`
   byte-identity check, the `--no-validity-gating` restore, and the
   no-leaked-`$ValidityF:*`-keys check.
8. **Add the CLI axis** in `tests/benchmarks/run_external.py` (scope explicitly
   accepted — see "Scope decision" under Appetite; mirror `extraction_axis.py`'s
   existing `ARM_CHOICES` + filename-label convention rather than inventing a
   second one):
   `--supersession` and `--no-validity-gating`; set
   `Defaults.VALIDITY_GATING_ENABLED` once in `main()` before any scenario
   construction (Risk 5); add the `_sup-{arm}` / `_nogate` artifact filename
   labels; add the new aggregate rows, including `producer_failures` printed
   even when zero.
9. **Run the demonstration** (AC2): a small-n run on the extended fixture across
   arms A/B/C. Capture `n_excluded_keys`, `n_excluded_hits`, `supersessions`,
   the identity firing rate, and the full environment. Verify the first item's
   `n_excluded_keys` is 0 (contamination check, Risk 3).
10. **Document**: `tests/benchmarks/README.md` `--supersession` section with the
    three-arm table and the verbatim does/does-not-establish paragraph.
11. **File the follow-up investigation issue** for the per-item key-isolation
    defect (spike-2 side finding), referencing this plan. Do not fix it here.
12. **Comment on #586** with the demonstration numbers and a pointer to the
    does/does-not-establish text, so the unblocking is explicit rather than
    implied by this issue closing.

## Verification

| # | Check | Command / method | Expected |
|---|---|---|---|
| 1 | Arm `none` is byte-identical | `pytest tests/benchmarks/test_external.py` | All existing cases green; no `$ValidityF:*` key created during a `none`-arm item |
| 2 | Dates parsed, LoCoMo untouched | `pytest tests/benchmarks/test_external.py -k "parse or dataset"` | `session_date` present on LME turns, absent-and-tolerated on LoCoMo |
| 3 | Producer is label-blind | `pytest tests/benchmarks/test_supersession_axis.py -k signature` | `inspect.signature(identity_of)` has one positional param; neither `relevant_ids` nor `question_type` in either function's signature |
| 4 | First claim registers an incumbent | `pytest tests/benchmarks/test_supersession_axis.py -k first_claim` | Exclusion set ≥ 1 after two identity-bearing writes |
| 5 | **AC2** — non-empty exclusion state | Fixture run, arm C | `n_excluded_keys >= 1` and `n_excluded_hits >= 1` in the emitted artifact; superseded record absent from assembled records, successor present |
| 6 | Arm B is executable | Same run with `--no-validity-gating` | Superseded record returns; identical stored state to arm C |
| 7 | Race 1 handled | Non-monotonic fixture item | Run completes; latest-dated claim is the open one; no `ValidityCloseBeforeStartError` |
| 7b | Tied dates tolerated | Tied-timestamp fixture session (N1) | Run completes, no `ValidityCloseBeforeStartError`; tied loser holds a zero-length interval and is excluded from assembled records |
| 8 | No key leak | Post-teardown `SCAN "$ValidityF:*"` | Empty |
| 8b | Arm-`none` teardown is a real no-op | `pytest tests/benchmarks/test_external.py -k teardown_none` | No exception raised, and a pre-seeded sentinel under the shared `$ValidityF:…:validity:*` names **survives** — proving row 1 is not vacuous |
| 8c | Arm-`none` ingest order untouched | Unit test on the history-ordering helper | Returns `self.item.history` unchanged on `none`; date-sorted on `content-identity` |
| 9 | Contamination check | First item of the demonstration run | `n_excluded_keys == 0` |
| 10 | Failure counting is visible | Report header on a run with zero supersessions | `producer_failures` and `supersessions` both printed |
| 11 | Repo gates | `ruff check src/`; `black --check src/ tests/`; `scripts/mypy_ratchet.py`; `mkdocs build --strict`; full `pytest` | All clean; ratchet count unchanged (no `src/` change) |
| 12 | Environment stated | PR body and report artifacts | Python, redis-py, platform, Redis DB, baseline SHA on every number |

## Critique Results

### Round 2 (2026-09-07) — verdict: READY TO BUILD (with concerns)

FULL depth, independent roster (Risk & Robustness, Scope & Value, History &
Consistency). **0 blockers, 2 concerns, 2 nits.** Critique cycle 2 of 2, so the
concerns below are accepted on the record; embed their implementation notes and
build.

**Round-1 resolutions verified as landed.** All three critics independently
re-checked C1, C2, C3, N1 and N2 against the plan *body* (not the resolution
table) and against source: the teardown shape is stated once and identically in
Technical Approach step 5 and Task 4; both "when the arm is active" qualifiers
are present in Task 3 and consistent with Success Criterion 5 and Verification
rows 1 / 8c; C3 is stated as an accepted scope decision; N1's tie analysis
matches `SUPERSEDE_LUA`'s strict `close_at < start_num` guard
(`validity_field.py:369`); N2's re-save safety matches the `ZADD NX` block gated
on `new_score == false or is_open(new_score)` (`validity_field.py:404-410`). No
residual contradiction survives anywhere in the plan.

#### R2-C1 — `n_excluded_hits` has no named computation path, and the obvious ones do not work

*Flagged independently by Risk & Robustness and Scope & Value — the strongest
signal in this round.*

AC2 and Verification row 5 make `n_excluded_hits >= 1` load-bearing, and
Technical Approach step 6 defines it as "how many of *this item's* retrieved
candidates the gate actually removed". Nothing in the shipped assembler exposes
that. `_scope_by_validity` (`src/popoto/recipes/context_assembler.py:1713-1726`)
returns a filtered list with no count;
`ValidityField.resolve_excluded_keys` (`src/popoto/fields/validity_field.py:863-931`)
returns the run-wide exclusion set, which per the spike-2 side finding is not
even item-scoped; and the server-side `DECAY_SCORE_LUA` layer drops rows before
they ever become client-side candidates.

**Two seams a builder would reach for are actually dead ends — check this before
implementing:**

- `all_pull_candidates` is **not** a pre-filter candidate list. It is rebound
  through `_scope_by_validity` at `context_assembler.py:1875-1876` before it
  reaches `AssemblyResult` at `:2078`, so it is already gated. The only
  pre-filter form is `_pull_path`'s return at `:1869`, which is not retained.
- `self._assembler._assembly_as_of` is **not** readable after `assemble()`
  returns. It is parked at `:1860` and reset to `None` at `:2087`.

**Resolution for the builder:** pass an explicit `as_of=t` into `assemble()` and
reuse that same `t` for `ValidityField.resolve_excluded_keys(..., as_of=t)` —
that satisfies Race 2 without touching a private attribute. For
`n_excluded_hits`, diff two `assemble()` calls over identical stored state:
```python
gated = self._assembler.assemble(query_cues=..., agent_id=..., as_of=t)
prev = Defaults.VALIDITY_GATING_ENABLED
Defaults.VALIDITY_GATING_ENABLED = False
try:
    ungated = self._assembler.assemble(query_cues=..., agent_id=..., as_of=t)
finally:
    Defaults.VALIDITY_GATING_ENABLED = prev
n_excluded_hits = len(_keys(ungated) - _keys(gated))
```
This captures removals from **all three** gating layers, not just the client-side
one. Guard it so the second call runs only on arm C (`supersession_arm !=
"none"` and gating currently on) — arm A and arm B have nothing to diff and the
extra call doubles `retrieval_ms` per item.

**This is a documented, bounded exception to Risk 5, not a violation of it.**
Risk 5's rule ("set once in `main()`, never per item") is about *which arm an
artifact reports*; a save/restore in a `finally` around a measurement-only second
call leaves the arm's flag exactly as `main()` set it. Say so at the call site,
and never let the ungated call's records reach `ScenarioResult.records`.

#### R2-C2 — `route_write`'s code sample has no `try`/`except`, so `producer_failures` would always read 0

*Risk & Robustness.*

Technical Approach step 3's snippet calls `SupersessionProtocol.save_and_supersede`
bare, while the paragraph below it promises exceptions are "caught per unit,
counted in `stats.failures`, and logged". As written the exception escapes
`route_write` into the ingest loop's pre-existing handler at
`tests/benchmarks/scenarios/external_base.py:506-510`, which logs but has no
reference to `stats` — that local is out of scope there. `producer_failures`
would therefore print 0 under total producer failure, reintroducing exactly the
"silently-failing producer masquerading as a producer that found nothing" defect
the same section says the metric prevents (Verification row 10).

**Resolution for the builder:** catch inside `route_write`, which is the function
that owns `stats`:

```python
try:
    result = SupersessionProtocol.save_and_supersede(
        instance, identity_key=identity, at=at
    )
except (SupersedeDeclinedError, ValidityMemberAbsentError,
        ValidityCloseBeforeStartError) as e:
    stats.failures += 1
    logger.warning("supersession producer failed: %s", e)
    return False
```

Import paths (three types, **two** modules — do not assume one):
`SupersedeDeclinedError` from `src/popoto/fields/supersession.py:106`;
`ValidityMemberAbsentError` and `ValidityCloseBeforeStartError` from
`src/popoto/fields/validity_field.py:128` and `:138`.

#### R2-N1 — the ingest loop is at `external_base.py:445`, not `:447`

*History & Consistency.* Task 3 and the `### C2` record both cite `:447` for
`for turn in self.item.history:`; the verified line is **445**. The plan's other
`external_base.py` citations (`:479`, `:522`, `:506-510`, `:706`/`:710`/`:721`/`:725`)
are all correct. Either fix both occurrences or drop the number and rely on the
quoted literal.

#### R2-N2 — the two `ValidityField` key-helper citations are swapped

*Structural check.* Technical Approach step 5 and the `### C1` record both cite
"`get_all_keys` / `get_prefix_db_key` (`src/popoto/fields/validity_field.py:702`,
`:782`)". The actual definitions are `get_prefix_db_key` at **`:702`** and
`get_all_keys` at **`:782`** — the names are in the reverse order of the line
numbers. Also note `get_all_keys` returns **five** keys, not six (its docstring
says so explicitly); the sixth is the per-digest open pointer the plan already
handles with a separate `SCAN`, so the snippet is correct and only the prose
"six names" is loose.

### Round 1 (2026-09-07) — verdict: READY TO BUILD (with concerns)

FULL depth, independent roster
(3 critics: Risk & Robustness, Scope & Value, History & Consistency).
0 blockers, 3 concerns, 2 nits.

### Resolution status (revision pass, 2026-09-07)

All five findings are resolved **in the plan body**; the sections below are kept
verbatim as the critique record. Where the body and this record differ, **the
body is authoritative** — that is the point of the revision.

| # | Finding | Resolved where | Disposition |
|---|---|---|---|
| C1 | Contradictory teardown shapes | Technical Approach step 5 (rewritten, marked authoritative); Task 4 (restated to match) | **Fixed.** One shape: guard on `"validity" in self._model_class._meta.fields`, wrapped in the existing `except Exception: pass` idiom, `get_REDIS_DB()` not `POPOTO_REDIS_DB`. Explicitly **not** unconditional, with the reason (a vacuous `DEL` would make Verification row 1 unable to detect a real leak). New tests: Test Impact "arm-`none` teardown" bullet, Verification row 8b. |
| C2 | Reordering not scoped to the active arm | Task 3 (both qualifiers made explicit) | **Fixed.** Implemented as a single `_ordered_history()` helper that returns `self.item.history` unchanged on arm `none`; the guarded loop is named (`external_base.py:447`) with the consequence (DecayingSortedField / BM25 insertion order). New test: Test Impact ordering bullet, Verification row 8c. |
| C3 | Task 8 ships #586's surface under a Medium appetite | Appetite → "Scope decision" subsection; Task 8 preamble | **Accepted as an intended scope decision, and now stated as one**, with three grounds (artifact collision is a correctness bug, not convenience; it copies #489's convention rather than inventing one; the aggregate rows are this issue's own AC2 deliverable). What stays #586's is enumerated. |
| N1 | Tied `haystack_dates` unaddressed | Race 1 → "Tied dates" paragraph; Task 6; Verification row 7b | **Fixed, and the open question answered from source rather than left open:** the `SUPERSEDE_LUA` guard is `close_at < start_num` (`validity_field.py:368-370`), a *strict* comparison — an equal `at` does **not** raise, it stores a zero-length interval, which membership (`valid_from <= t AND invalid_at > t`) can never satisfy, so the tied loser is excluded. Tie-break by list position is arbitrary but outcome-neutral. Pinned by a new fixture case. |
| N2 | Graph-mode re-save is a second write path | Task 3 → "graph-mode re-save" paragraph | **Fixed by statement, as the critique asked.** `:522` deliberately stays outside `route_write` (it re-saves an existing record, so it is not a new claim); safe because `on_save` mode `"open"` uses `ZADD NX` gated on `invalid_at` absent-or-`+inf` (`validity_field.py:403-410`), so it can neither shift nor resurrect an interval. A code comment at `:522` carries this. |
| — | `context_assembler.py` path | spike-1 item 2 | **Fixed.** Corrected to `src/popoto/recipes/context_assembler.py`, with a note that all bare citations in the plan resolve there. |

### C1 — Task 4 and Technical Approach step 5 give contradictory teardown shapes

*Flagged independently by Risk & Robustness and History & Consistency.*

Technical Approach step 5 gates the validity-keyspace deletion behind
`if self._supersession_arm != "none":`; Task 4 says to do it "even on the `none`
arm's error paths — it must be safe to call unconditionally". Both are plan text
and they cannot both be implemented as written. Followed literally, Task 4 calls
`ValidityField.get_all_keys(...)` against a model class that never declared the
field — a path neither spike exercised.

**Resolution for the builder:** `get_all_keys` / `get_prefix_db_key`
(`src/popoto/fields/validity_field.py:702`, `:782`) only concatenate
`_meta.db_class_key` + field name into key *strings*; they do not require the
field to be declared, so an unconditional `get_REDIS_DB().delete(*keys.values())`
does not raise on the `none` arm. Pick one shape and make both sections say it.
If unconditional, guard on `"validity" in self._model_class._meta.fields` (or
wrap in the existing `except Exception: pass` teardown idiom at
`external_base.py:692-729`) and add a test that teardown on an arm-`none` item
does not raise — deleting non-existent keys must not become a way to mask a real
leak that Verification row 1 is meant to catch. Note the file's teardown
currently uses the module-level `POPOTO_REDIS_DB` import
(`external_base.py:706`, `:710`, `:721`, `:725`); new code must use
`get_REDIS_DB()` per CLAUDE.md, and the plan's snippet already does.

### C2 — Task 3's session-date reordering is not scoped to the active arm

Task 3 attaches "when the arm is active" to the `ValidityField` bullet but not to
"order sessions by `session_date` ascending". Read literally that reorders ingest
for arm `none` too, which contradicts Success Criterion 5 (arm `none` byte-
identical) and Verification row 1. Race 1's prose and the Data Flow diagram both
scope reordering to `content-identity`.

**Resolution for the builder:** the reorder applies **only** when
`supersession_arm != "none"`. The loop to guard is `for turn in self.item.history:`
in `ExternalScenario.setup()` (`external_base.py:447`); changing its order on the
`none` arm changes insertion order into `DecayingSortedField` and the BM25 index
and will move committed-baseline numbers. Write the task text with the qualifier.

### C3 — Task 8 ships #586's corpus-scale CLI/reporting surface under this issue's Medium appetite

AC2 needs a small-n demonstration; Task 8 builds the full three-arm production
surface (`--supersession`, `--no-validity-gating`, `_sup-{arm}` / `_nogate`
artifact labels, new aggregate rows) while the plan explicitly scopes #586's run
out.

**Resolution for the builder:** this is accepted as an *intended* scope decision —
say so in the plan rather than leaving it incidental. The minimum AC2 needs is an
arm toggle and a gate toggle; the artifact-label scheme and aggregate rows exist
so #586 inherits ready infrastructure and so the three arms cannot overwrite each
other on disk. Mirror `extraction_axis.py`'s existing `ARM_CHOICES` + filename-
label precedent (#489) rather than inventing a second convention.

### N1 — Tied `haystack_dates` values are unaddressed

`haystack_dates` is minute-precision (`"%Y/%m/%d (%a) %H:%M"`), so two sessions can
share a timestamp. Python's stable sort then decides "current" by haystack list
position, and the plan does not state whether `save_and_supersede(at=<equal>)`
raises `ValidityCloseBeforeStartError` or silently accepts. The per-unit exception
handler absorbs it either way; note the tie case in Race 1, and consider a tied-
timestamp case alongside Task 6's non-monotonic one.

### N2 — The graph-mode re-save at `external_base.py:522` is a second write path

`turn_first_instance.save()` at `:522` (graph mode only) does not go through
`route_write`. It is very likely benign — `on_save` mode `"open"` uses `ZADD NX`,
so a re-save cannot reopen a closed interval — but the plan's Task 3 promise to
"keep graph-edge behavior identical in both arms" should say so explicitly rather
than leave a reader to derive it.

### Structural check notes

- Two referenced paths do not exist yet and are created by this plan
  (`tests/benchmarks/supersession_axis.py`, `tests/benchmarks/test_supersession_axis.py`).
- The plan cites `context_assembler.py:1499-1582` etc. without a path; the file is
  `src/popoto/recipes/context_assembler.py`, not under `models/`. Cited anchors
  verified: `_validity_field_name` at `:1401`/`:1437-1438`, `_resolve_excluded_keys`
  at `:1630`, `_scope_by_validity` at `:1713`.
- All other line citations re-verified at `4ac805f0`+: `validity_field.py:920-932`
  (gate), `external_base.py:479` and `:522` (the only two `.save()` sites),
  `SupersessionProtocol.save_and_supersede` at `supersession.py:363` with
  `SupersedeResult.closed_key` at `:152`.
- Tasks are numbered 1-12 with no gaps, no `Depends On` references, and no cycles.
  Per-task validation lives in the Verification table rather than on each task.

## Open Questions — settled

None block the build. Dispositions, for the record:

1. ~~**Is a Medium appetite that stops short of the n=500 run acceptable?**~~
   **Settled.** The critique reviewed the boundary and returned READY TO BUILD;
   C3's resolution states the line explicitly (see "Scope decision" under
   Appetite). This issue delivers the producer, the axis, the three-arm toggles,
   the observability, and a small-n demonstration; #586 keeps the corpus-scale
   run and its publication.
2. **Which extraction arm should the eventual #586 run use?** Still open, and
   deliberately so — it is **#586's question, not this plan's**, and it cannot be
   answered before this issue measures the identity firing rate on
   `--extraction raw` (Risk 1). Task 12 hands #586 that number. Nothing in this
   plan's build depends on the answer.
3. **Should the follow-up key-isolation issue (Task 11) block #586?** Still
   open, and again a **#586 prioritization call**, not a build input. The defect
   is contained here by the explicit teardown (Technical Approach step 5); the
   issue gets filed regardless (Task 11) with the evidence, and whoever schedules
   #586 decides. Not a prerequisite for this issue's completion.
