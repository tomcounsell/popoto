# StructMemEval track — latent-structure memory and the subconscious hint gap (#498)

**Issue:** #498 · **Epic:** #456 (Track A — Measure) · **Status:** plan
**Author:** lane-498 · **Date:** 2026-09-16

> **Verdict up front.** The *corpus* is on hand: StructMemEval's task data is Apache-2.0,
> anonymously downloadable, and needs no account, no API key, and no external service.
> The *head-to-head against Mem0 / mem-agent* named in the issue title is **blocked** on
> external infrastructure we do not have and should not vendor (§Freshness Check). This
> plan therefore delivers a **Popoto-only, within-system hint-gap study** on StructMemEval
> data, which is the part of the issue's thesis that is actually falsifiable here, and
> explicitly descopes the competitor arms. See §No-Gos and §Questions for the architect.
>
> **Second verdict, found while planning and more consequential than the first.** The
> issue's premise — that Popoto's typed primitives already give StructMemEval's structures
> as substrate — does not hold against the code. `ExtractedFact` has no slot for a typed
> value under *any* extraction provider, and `PredictionLedgerMixin` is a prediction-*error*
> ledger, not an accounting ledger. **There is no subconscious write path into a typed
> structure today** (§Spike Results). One of the three families has a free, informative arm
> (state machine, via `ValidityField`), one needs a paid extractor to be informative at all
> (tree), and one has nothing to route to (accounting). The plan reports that gap rather
> than scoring it as 0%, and Q6/Q7 ask whether to file the feature it implies.

---

## Problem

`tests/benchmarks/run_external.py` targets LongMemEval-S and LoCoMo — the two datasets
StructMemEval was built as a reaction against. Both score **retrieval recall@k / MRR
against gold document ids**, and both are answerable, in StructMemEval's framing, by
"recovering a few locally relevant snippets."

We have no measurement of the failure mode StructMemEval isolates: a task whose answer is
only recoverable if memory is maintained as a **latent structure** (a ledger, a state
machine, a graph) across the whole interaction, where no snippet — and no top-k set of
snippets — contains the answer.

Popoto's product constraint makes this more than a missing dataset. The paper's central
finding is that *the hint/no-hint gap is often larger than the gap between memory systems*:
models can use the right structure when told to, and fail to recognize they should
maintain one when not told. Popoto's architecture claims to make that gap structural
rather than prompted — the structure is chosen at schema-design time by the developer via
typed primitives, and `SubconsciousMemory` injects and extracts around the turn without
the agent being instructed. **That claim has never been measured.** Until it is, "Popoto is
an always-on structural hint" is a design intention, not a result.

### What is actually in question

Two hypotheses, stated so they can fail:

- **H1 (structure).** On StructMemEval's three structure families, a Popoto arm backed by
  typed primitives scores materially above a snippet-retrieval control run inside this same
  harness on the same items.
- **H2 (subconscious — the headline).** For Popoto, the **hint gap is ≈ 0**: accuracy with
  the structural hint in the prompt does not exceed accuracy without it, judged against the
  pre-registered criteria in §Research ("Pre-registered statistical criteria") — an exact
  McNemar test at alpha 0.05 plus a declared minimum detectable effect, so a small `n`
  cannot pass H2 by widening the interval. If H2 fails, the typed-substrate thesis is wrong
  as stated and we report that.

H2 is the one worth chasing because it is **measurable without a competitor**. It is an
intra-system, paired, same-metric-family difference computed over identical item ids. That
is the design's load-bearing property: it needs nothing we cannot run.

### Anti-hypothesis (where we expect to lose)

Issue question 4 is correct, and the spikes below turned it from a suspicion into a
confirmed structural gap. Nothing routes `"Alice: Paid €179 for museum - split with Bob"`
into a typed ledger entry, because (a) `ExtractedFact` has no slot for a typed value under
any provider, and (b) `PredictionLedgerMixin` is a prediction-*error* ledger, not an
accounting ledger — see §Spike Results, spike-1 and spike-2. StructMemEval's documented
accounting failure modes — omission, duplication, hallucination of transactions — land on
that extraction step, not on the aggregation step the issue assumed we were strong at.

So **extraction fidelity is a first-class measured output of this plan**, with its own
metric family, and the most likely outcome of the whole track is "the substrate exists, the
subconscious loop exists, and nothing connects them." That is a useful result — arguably
the most useful thing this benchmark can produce — and the plan is built so it can be
reported as a finding rather than laundered into a score (§Solution, "The zero-by-construction
rule").

---

## Freshness Check

Everything below was verified live on **2026-09-16**, not assumed from the issue text.

| Claim | Verified | Result |
|---|---|---|
| `github.com/yandex-research/StructMemEval` exists | yes | exists, **Apache-2.0**, described as "work in progress" |
| Paper arXiv:2602.11243 exists | yes | "Evaluating Memory Structure in LLM Agents", Shutova, Olenina, Vinogradov, Sinitsin (HSE / Yandex / YSDA / NES), submitted 2026-02-11, latest version Sept 2026 |
| Task data downloadable without auth | yes | plain `raw.githubusercontent.com` / GitHub contents API; no token, no account, no key |
| Data shape uniform across families | yes | every case is `{case_id, sessions[{session_id, topic, messages[{role, content}]}], queries[{question, reference_answer}]}` — **one loader covers all families** |
| Retrieval ground truth present | **no** | there are **no gold document ids** anywhere in the data. See §Metric Families — this is decisive |
| Hint / no-hint is data or prompt | prompt | shipped as text files, e.g. `benchmark/data/accounting/prompts/{mem0_agent_loading_hint.txt, mem0_agent_loading_no_hint.txt, mem0_agent_query_hint.txt, mem0_agent_query_no_hint.txt}`, plus `system_prompt{,_small_hint,_big_hint,_biggest_hint}.txt` — **the contrast is directly vendorable** |
| Upstream judge | binary LLM judge | `judge/run_all_judge.py` + `judge/prompt_new_2.txt`: single prompt, `max_completion_tokens=5`, `temperature=0`, returns literal `1`/`0`; default model `gpt-4o`, overridable by `JUDGE_MODEL` |
| Running *their* harness | **heavy** | their README requires cloning `mem-agent`, cloning `KevinSRR/EMem`, a running **Qdrant**, and `OPENAI_API_KEY` / `LLM_PROVIDER_API_KEY` |

### Corpus inventory, measured

Counted against upstream `main` at commit **`64d2c9b242deb394e3ef94a318868a55261e141b`**
(2026-06-29), which this plan **pins** — the repo is self-described work-in-progress and an
unpinned corpus makes two runs incomparable.

| Family | Path | Cases | Bytes | Per-case shape (sampled) |
|---|---|---:|---:|---|
| Count-based / accounting | `benchmark/data/accounting/` | 15 | 91 KB | 1 session, 50 msgs, 1 query, **3 alternative reference answers** |
| Tree / graph | `benchmark/data/tree_based/small_bench/` | 10 | 441 KB | 1 session, 250 msgs, **32 queries** |
| State machine | `benchmark/data/state_machine_location/small_bench/` | 14 | 108 KB | 5 sessions, 20 msgs, 1 query |
| Recommendations | `benchmark/data/recommendations/benchmark_data/` | 12 | 931 KB | (plus a parallel `benchmark_with_hints/`) |
| **Total (small bench)** | | **51** | **1.57 MB** | |
| Tree big bench | `tree_based/big_bench/` | 12 | 475 KB | out of scope |
| State big bench | `state_machine_location/big_bench/` | 42 | 239 KB | out of scope |

**The issue's "73 synthetic scenarios, 544 questions" does not match what upstream ships
today** — small bench is 51 scenarios, and question count is dominated by the tree family
(10 × 32 = 320) rather than spread evenly. The issue also omits the **recommendations**
family entirely. The repo has moved since the issue was written. Logged as Q1.

### What the Freshness Check settles

- **Corpus: available.** No external dependency beyond an anonymous HTTPS GET.
- **Competitor arms: unavailable.** Reproducing Mem0 / mem-agent numbers means standing up
  Qdrant, two third-party repos (one of which is not Yandex's), and paid keys — and then
  *maintaining* that against a WIP upstream. Descoped (§No-Gos, D2).
- **Citing the paper's published competitor table as our head-to-head is also refused.**
  Their numbers come from a different harness, different models, different prompts. Putting
  them in a table beside ours is precisely the cross-comparison the repo's doctrine bans
  (§Metric Families). They may appear as **prose context with a citation**, never as a row.

---

## Spike Results

These are **source-read spikes**, not executed runs: nothing below required Redis, an API
key, or a benchmark run, and none of it was assumed from the issue text. Each claim cites
the file and line that establishes it. They change the plan materially — see the corrected
routing table in §Research.

### spike-1: What does the subconscious write path actually emit?

`SubconsciousMemory.extract_memories()` (`src/popoto/recipes/subconscious_memory.py:427`)
delegates to an extraction provider. There are three:

| Provider | Location | Output |
|---|---|---|
| `HeuristicExtractionProvider` (**the default**) | `src/popoto/extraction/__init__.py:149` | splits text on `(?<=[.!?])\s+`, drops sentences under 10 chars, emits `ExtractedFact(text=sentence)` — **`entities=[]`, `importance=None`, `confidence=None`** |
| `RawTurnExtractionProvider` | `src/popoto/extraction/__init__.py:213` | whole turn as one fact |
| `ClaudeExtractionProvider` | `src/popoto/extraction/claude.py:112` | Anthropic structured output, `EXTRACTION_MODEL = "claude-opus-4-8"`, key from **`ANTHROPIC_API_KEY`** |

**The decisive finding is `ExtractedFact`'s shape** (`src/popoto/extraction/__init__.py`,
and `FACTS_SCHEMA` at `claude.py:83-90`, which requires exactly `["text", "entities",
"importance", "confidence"]`):

> `ExtractedFact` has twelve fields: `text`, `entities`, `importance`, `confidence`,
> `span_start`, `span_end`, `turn_id`, `candidate_id`, `generator_rule`, `verbatim`,
> `resolution_status`, `assumption`. Every one of them is `str` / `float` / `int` /
> `list[str]` — the only two numeric fields, `span_start` and `span_end`, are character
> offsets into the source turn (provenance), not a domain value.
> **None is a slot for a typed domain value** — no amount, no relation, no role.
> Neither provider, including the paid Claude one, can emit
> "payer=Alice, amount=179, split_with=[Bob]".

So the subconscious write path produces **untyped text facts with an optional entity list**,
for every provider. That is the ceiling.

### spike-2: Does `PredictionLedgerMixin` do what the issue claims?

**No.** The issue's mapping table lists it under "Ledger / count / netting". Its actual API
(`src/popoto/fields/prediction_ledger.py:309-624`) is `record_prediction(instance,
predicted)`, `resolve_prediction(instance, actual)`, `compute_prediction_error()`,
`get_highest_errors()`, `error_summary()`. It is a **prediction-error ledger** — a
calibration/regret structure — not an accounting ledger of debits and credits. It stores one
hash field per record at `$PL:{Class}:meta:{pk}` holding a predicted/actual pair.

**Popoto has no transaction-ledger primitive**, and the count-based family's whole task is
netting transactions. Nothing in this repo aggregates `+179 / -89.50` across parties.

### spike-3: What *does* route, and under what conditions?

| Family | Issue's claimed primitive | What is actually there | Verdict |
|---|---|---|---|
| Count-based / accounting | `PredictionLedgerMixin`, `ConfidenceField` ledger, `FrequencySketch` | prediction-error ledger (spike-2); `FrequencySketch` is a Count-Min *frequency* sketch, not a signed-amount accumulator | **no route exists** |
| State machine | `MemoryLifecycle`, `EventStreamMixin` | `MemoryLifecycle` (`recipes/memory_lifecycle.py:347`) is an episodic/semantic **tier-promotion and auto-forget policy** — nothing to do with domain state. The real match is **`ValidityField`** (`fields/validity_field.py:422`), which supersedes a record on an identity and keeps the old one queryable in historical mode — exactly "current state under 0–5 updates" | **routes, but via a different primitive than the issue names** |
| Tree / graph | `CoOccurrenceField`, `graph_traversal.traverse()` | genuinely wired subconsciously: `SubconsciousMemory._seed_associations()` (`recipes/subconscious_memory.py:824`) links co-mentioned entities as graph nodes on every extracted fact — **but it is a documented no-op unless `fact.entities` has ≥2 names**, and per spike-1 only `ClaudeExtractionProvider` ever populates `entities` | **routes only with paid extraction** |

### What the spikes change

1. **The accounting family has no subconscious arm to measure.** With the default provider,
   F3 would be zero *by construction* — an artifact of provider choice, not a finding. The
   critique was right that this is the difference between a measurement and a tautology.
2. **The tree family's arm is informative only with `ANTHROPIC_API_KEY` set**, because the
   heuristic provider emits `entities=[]` and `_seed_associations()` no-ops. A heuristic-only
   tree run is also zero by construction.
3. **The state-machine family is the one family with a free, informative arm**, via
   `ValidityField` supersession — and even there the write path must produce a supersession
   identity, which untyped text facts do not obviously carry.
4. **Two API keys, not one.** `ANTHROPIC_API_KEY` (extraction, `claude-opus-4-8` — an
   expensive tier, per-message during ingest) *and* `OPENAI_API_KEY` (judge). §Prerequisites
   and §Risks are corrected accordingly.

**This is the most important output of the plan so far, and it is a finding about Popoto,
not about StructMemEval.** The typed-substrate thesis has a missing middle: the substrate
exists (fields), the subconscious loop exists (inject/extract), and *there is no typed write
path connecting them*. The benchmark's honest role is to establish that, not to route around
it — see §Solution, "The zero-by-construction rule."

---

## Prior Art

### In this repo

| Thing | Path | What it gives us |
|---|---|---|
| External harness | `tests/benchmarks/run_external.py` (1623 lines) | recall@k/MRR against `relevant_ids`; hardcoded `DATASET_CHOICES = ("longmemeval-s", "locomo")` dispatch; `_resolve_bench_db()`; `save_reports()` artifact conventions |
| Item contract | `tests/benchmarks/datasets/__init__.py` | `BenchmarkItem(item_id, history, query, relevant_ids, metadata)`; adapters expose `iter_items(fixture_path, limit, sample, seed)` |
| Tier 5 judge | `tests/benchmarks/judge.py` | **`JudgeProtocol`** (`chat(model, messages, temperature) -> str`), `is_judge_available()`, `build_openai_client()`, `estimate_cost()`, `judge_identity()` recording prompt SHA-256s |
| SIQ | `tests/benchmarks/siq/` | the **arms** pattern: `SiqAdapter` Protocol + `ADAPTERS = {...}` registry + `--adapter` flag; `QueryOnlyStubAdapter` as a dependency-free control that scores ~0 *by construction*, proving the harness is not vacuous |
| RLT | `tests/benchmarks/rlt/` | the **DB discipline** pattern: `run_rlt.py` requires `--db` with **no default**, `FORBIDDEN_DBS = {0, 14, 15}`, `validate_db()` raising `RltDbError` |
| Docs generation | `docs/scripts/gen_benchmark_pages.py` | `Spec` objects map `*_latest.{json,md}` artifacts to generated pages; `_warn_orphan_artifacts()` prints (never raises) a stderr warning for an unspecced artifact, but only scans `results/external/` today, so it does not see `results/sme/` (§Architectural Impact, task 11) |

### The two verified harness facts this design must respect

1. **`_resolve_bench_db()` silently outranks `REDIS_URL`.** It reads `POPOTO_BENCH_DB`
   (default **14**) and repoints the pool *after* import. A new harness path that inherits
   this would be bound to a DB the operator never named. **Mitigation:** follow RLT, not
   `run_external.py` — require `--db` explicitly, and additionally **assert the resolved DB
   from inside the process** by reading it back off the live connection pool after all
   imports and after any repoint, failing loudly on mismatch. The asserted value is stamped
   into the artifact.
2. **Nothing in the repo asserts a shared-item-id relationship between artifacts.**
   "Like-for-like" is a documentation claim today. This design's entire headline (a
   *paired* hint gap) depends on comparability, so **this plan must establish it itself** —
   see §Data Flow, "Comparability contract."

### Why previous shapes do not fit

- **Extending `run_external.py` with a third `--dataset`** fails on the primary metric:
  its scoring path is `recall_at_k(retrieved_ids, relevant_ids)`. StructMemEval ships **no
  gold ids**, so `relevant_ids` would be empty for every item and every recall number would
  be a structurally guaranteed zero. Its `BenchmarkItem` also carries one `query` per item,
  while a tree case carries 32 against one 250-message session, and it has no notion of
  arms at all (its axes — retrieval-mode, extraction, supersession — are all Popoto-internal
  knobs on a single system under test).
- **Writing a Popoto adapter into upstream's harness** is the more defensible shape for a
  *competitive* claim and is the right thing to do the day we can run competitors. Today it
  buys a dependency on Qdrant + two third-party repos + paid keys, to produce numbers we
  have already decided not to publish as a head-to-head. Deferred, not rejected (Q4).

---

## Research

### Resolving the issue's five open questions

| # | Question | Decision | Rationale |
|---|---|---|---|
| 1 | Adapter vs reimplementation | **(a) vendor the task data, score through our own path** | (b) requires Qdrant + `mem-agent` + `EMem` + paid keys and couples us to a WIP repo. (a) costs one loader — the data shape is uniform across all four families. Consequence stated plainly: **(a) cannot produce competitor numbers**, so the issue's acceptance criterion "head-to-head" is not met in this phase. |
| 2 | How is "long-running agentic work" operationalized? | **Scenario-level all-queries-correct under an un-augmented agent prompt** — a binary per *scenario*, explicitly labelled a **proxy** | StructMemEval scenarios have no goal state, no tools, and no termination condition. Building a real agent loop means inventing the task objective, which makes the benchmark ours rather than theirs and puts an unbounded agent-framework dependency in the critical path. The proxy is honest, cheap, and computable from data we already produce. The true trajectory version belongs to **PTR**. |
| 3 | Instruction-parity protocol | **Make it a 2×2 factor matrix, and name the asymmetry as the measured variable** | See §Solution, "Arm matrix." Because competitors are descoped, the asymmetry is measured *within Popoto*, which removes the confound argument entirely: every cell is the same system, same corpus, same items. |
| 4 | Which primitive routes each family, and does the write path populate it? | **Answered by spike-1/2/3, and the answer is mostly "it does not."** Extraction fidelity stays a first-class deliverable, now with the corrected routing table and the zero-by-construction rule | The accounting family's messages are template-generated (`"{who}: Paid €{amt} for {what} - split {with}"`), so a **gold transaction list is derivable by parsing the corpus** — a write-path ground truth upstream does not have. But per spike-2 there is no ledger primitive to populate, and per spike-1 no provider emits a typed value at all. So F3's first job is to *quantify the missing middle*, not to grade a working path. |
| 5 | Relationship to #456 | **A distinct StructMemEval track that feeds PTR** — not filed as PTR | PTR is defined as trajectory recall on task success / steps-to-completion for agentic tasks. This has no trajectories and no steps. It contributes the goal-completion *scoring vocabulary* PTR will reuse. |

### The comparison that is defensible

```
                       structural hint in prompt
                         absent          present
memory named    absent   A  (native)  |  B          <- hint gap for Popoto = B - A   [H2]
in agent prompt present   C            |  D  (upper bound / upstream parity)

control:  snippet_baseline, no typed substrate, cell A conditions            [H1]
```

- **H2 = B − A**, paired over identical item ids, same judge, same model, same corpus
  fingerprint. This is the headline. It is an *intra-system difference*, so it is immune to
  every "you ran a different harness than they did" objection.
- **H1 = A(native) − A(snippet_baseline)**, same pairing.
- **Cell D** exists to show we can reach the regime upstream reports as strong, so that a
  small B−A cannot be dismissed as "everything scored low, of course the gap was small."
  A floor check: if D ≈ A ≈ 0 the run is uninformative and must be reported as such, not as
  "gap closed."

### Pre-registered statistical criteria

These are fixed **now**, before any live run, so an ambiguous number cannot be litigated
after the fact. The corpus sizes are small and lopsided (accounting ≈ 45 judged items with
alternative references; state machine 14; tree ≈ 320), so a naive "within the confidence
interval" test would let a wide CI swallow a real effect — the escape hatch the critique
correctly identified.

- **Test:** two-sided **exact McNemar** on the discordant pairs (`hint_only` vs
  `native_only`), per family and pooled. Paired, because every cell runs the same item ids.
- **Alpha:** 0.05.
- **Minimum detectable effect (MDE), declared per family before the run:** computed from
  that family's `n` at alpha 0.05 and power 0.80, and **printed in the report**. If a
  family's MDE exceeds 15 accuracy points, that family is declared **underpowered** and
  reports its point estimate with an explicit "cannot support or refute H2 at this n" line —
  never a "gap closed" verdict.
- **H2 is supported** only when the McNemar test fails to reject **and** the family is not
  underpowered **and** the observed |B − A| is below the declared MDE. All three, or the
  verdict is "inconclusive." A non-significant result from an underpowered family is
  explicitly *not* evidence for H2.
- **"Materially above" (the cell-D floor check) is defined numerically:** D − A ≥ 15
  accuracy points **and** McNemar significant at alpha 0.05. If D does not clear both, the
  run is reported as **uninformative** and no H2 verdict is issued at all.
- Multiple families are tested, so pooled-plus-per-family reporting uses
  **Holm–Bonferroni** across the family-level tests; unadjusted p-values are shown alongside.

**The snippet control is a control, not a competitor.** It is this harness's analogue of
SIQ's `QueryOnlyStubAdapter` — a dependency-free arm that should score near the floor *by
construction*, proving the harness can distinguish structure from retrieval at all. It must
never be labelled "RAG", "Mem0", or "baseline system" in any artifact or docs page.

---

## Metric Families

This repo's doctrine: **recall numbers are never cross-compared against judged-accuracy
numbers.** This track introduces a third and a fourth family. All four are defined here and
the harness enforces the separation mechanically.

| # | Family | Unit | Range | Produced by | Never compared to |
|---|---|---|---|---|---|
| **F1** | **Judged accuracy** | one `(question, reference_answer)` pair | binary 0/1 → mean | LLM judge, upstream's prompt, `temperature=0` | F2, F3, and *any* recall@k from `results/external/` |
| **F2** | **Goal completion** | one **scenario** (case file) | binary 0/1 → rate | all F1 units in that scenario correct | F1 (different unit — 32 tree questions collapse to 1), F3 |
| **F3** | **Extraction fidelity** | one **gold transaction** | counts: correct / omitted / duplicated / hallucinated | parsing the accounting corpus vs reading back the typed store | everything — these are *counts of write events*, not answer quality |
| **F4** | **Retrieval recall@k** | — | — | **NOT PRODUCED. StructMemEval ships no gold document ids.** | n/a |

F4 is listed to make the absence explicit: this benchmark is *structurally incapable* of
emitting a recall number, which removes the cross-family hazard by construction rather than
by discipline. Any future change that adds a synthetic `relevant_ids` to this corpus
reintroduces the hazard and must be reviewed against this section.

### Enforcement, not just documentation

- The artifact JSON nests every metric under an explicit family key:
  `{"f1_judged": {...}, "f2_goal": {...}, "f3_extraction": {...}}`. There is **no flat
  top-level `summary` with mixed scalars**, because a flat summary is what makes a wrong
  table easy to write.
- Each family block carries `"unit"` and `"never_compare_to"` strings, copied verbatim into
  the generated Markdown report header.
- `tests/benchmarks/test_sme.py` asserts that the report Markdown contains a metric-families
  section naming all three produced families and their units, and that no single table in
  the report contains a column from two different families. (Same spirit as the existing
  leaderboard-parity framing requirements from #453.)

---

## Data Flow

```
upstream @ 64d2c9b (pinned)
  benchmark/data/{accounting, tree_based/small_bench,
                  state_machine_location/small_bench, recommendations/benchmark_data}
  benchmark/data/*/prompts/*.txt          (hint / no-hint prompt variants)
        |
        |  sme/corpus.py : download (anonymous HTTPS, SHA-pinned tarball)
        |                  -> ~/.cache/popoto_benchmarks/structmemeval/<sha>/
        |                  or --fixture -> tests/benchmarks/sme/fixtures/*.json (committed)
        v
  SmeItem(item_id, family, case_id, sessions, question, reference_text, ref_index, metadata)
        |
        |  canonical item_id = f"{family}/{case_id}#q{qi}a{ai}"
        |  corpus_fingerprint = sha256(sorted(item_id + sha256(question+reference_text)))
        v
  sme/runner.py  --arm {native,instructed,snippet_baseline} --hint {on,off}
        |
        |  ingest phase : adapter.ingest_session(session)   [per case, isolated store]
        |  query phase  : adapter.answer(question, hint_text) -> str
        v
  sme/judge_adapter.py  -> reuses tests/benchmarks/judge.py JudgeProtocol
        |                  (live OpenAI, or offline deterministic stub)
        v
  artifact: tests/benchmarks/results/sme/sme_{YYYYMMDD}_{arm}_{hint}.json / .md
            + sme_latest_{arm}_{hint}.json / .md
        |
        v
  sme/compare.py  : paired join on item_id across two artifacts
                    REFUSES if corpus_fingerprint differs  <-- the comparability contract
        v
  sme_gap_latest.json / .md   ->  docs/scripts/gen_benchmark_pages.py Spec
```

### Comparability contract (the thing nothing in the repo does today)

Because H2 is a *paired difference*, "like-for-like" cannot be a sentence in a report. It is
enforced in three places:

1. **Canonical ids.** `item_id = f"{family}/{case_id}#q{qi}a{ai}"` is computed by the loader
   from the pinned corpus and is deterministic — no ordering, sampling, or arm can change it.
   Note `a{ai}`: the accounting family ships a **list** of alternative `reference_answer`
   objects (3 for the sampled case) and upstream expands `(query, answer_idx)` into separate
   judged records. We do the same, and the answer index is part of the id.
2. **Corpus fingerprint.** SHA-256 over the sorted `item_id`s, each combined with a hash of
   its question and reference text. Stamped into every artifact alongside the upstream commit
   SHA and the `--fixture` path if any.
3. **A refusal, not a warning.** `sme/compare.py` raises `SmeComparabilityError` when two
   artifacts' fingerprints differ, and prints the symmetric difference of item id sets. A
   test plants a mutated corpus and asserts the refusal. **This is the acceptance test for
   the whole "head-to-head" idea** — if we cannot refuse an invalid pairing, we cannot make a
   valid one.

Additionally the comparison reports **per-item paired outcomes** (`both_correct`,
`hint_only`, `native_only`, `both_wrong`) rather than only two means, because the
interesting quantity for H2 is the discordant cells (McNemar), not the difference of
averages.

---

## Architectural Impact

- **New package `tests/benchmarks/sme/`.** Nothing under `src/popoto/` changes. This is a
  benchmark track, not a library feature.
- **`run_external.py` is not touched.** Its `DATASET_CHOICES` stays at two entries. The
  reason is recorded in §Prior Art, "Why previous shapes do not fit," and repeated in the
  package docstring so a later editor does not "unify" the two.
- **`tests/benchmarks/judge.py` gains no new behavior**, only a new caller. If StructMemEval's
  prompt needs a second prompt constant, it lives in `sme/`, and `judge_identity()`-style
  prompt SHA-256 recording is replicated for it so the upstream prompt text is auditable in
  the artifact.
- **`docs/scripts/gen_benchmark_pages.py` gains `Spec` entries** for the SME artifacts, and
  its `_warn_orphan_artifacts()` scan is extended (task 11) to also glob
  `tests/benchmarks/results/sme/*_latest*.md` — today it only scans `results/external/`, so
  an SME artifact without a `Spec` currently produces **no** warning at all, silently rather
  than loudly. With the `Spec` entries in place, the results page is generated at build time
  from the committed artifact, per #453. The scan, even after the extension, only ever
  `print`s to stderr and never raises, so it cannot be what makes `mkdocs build --strict`
  fail (see the test-based check in §Success Criteria and §Verification instead). That
  test-based check is only falsifiable if the directory it scans is non-empty at the point
  it runs: task 11 commits the `--judge stub` artifact that Success Criteria bullet 1
  already produces, under `results/sme/`, before `test_sme_artifacts_have_spec_entries` is
  added, and the test itself asserts the glob is non-empty (not just that every match has a
  `Spec`) so it cannot pass vacuously against an empty results directory the way the
  original `mkdocs build --strict` claim did.
- **Third-party data enters the repo.** Vendored fixtures are Apache-2.0 upstream material
  and require attribution: a `tests/benchmarks/sme/fixtures/UPSTREAM.md` recording the repo,
  commit SHA, license, and the paper citation, and a matching entry in `NOTICE` if the repo
  has one (check at build; create only if a convention already exists — do not invent one).
- **Valkey-safe by construction.** The adapters use existing Popoto primitives only; no
  Redis modules are introduced. Asserted by the standing epic constraint, re-checked in the
  review checklist.

---

## Appetite

**Two build lanes, sequential.** This is a measurement track with a genuinely uncertain
outcome, so it is scoped so that stopping after lane 1 still leaves something true in the
repo.

- **Lane 1 — harness, offline (no API key, no network, no cost).** Loader, item ids,
  fingerprint, adapters, arm matrix plumbing, comparison + refusal, artifact schema, report
  writer, committed fixture, CI smoke. Ends with a green `pytest tests/benchmarks/test_sme.py`
  and a `--judge stub` run producing a real artifact. **This is where the defensible
  engineering is**, and it is fully schedulable today.
- **Lane 2 — the live run.** Requires `OPENAI_API_KEY` and the maintainer's go-ahead on
  spend. Produces the four cells + control, the gap report, docs pages, and the headline
  propagation. Cost estimate in §Risks.

Lane 1 must not block on lane 2's key. The extraction-fidelity work (F3) sits in lane 1
where possible — measuring what the write path stores does **not** require a judge.

---

## Prerequisites

| Prerequisite | Status | Notes |
|---|---|---|
| StructMemEval corpus | **available** | Apache-2.0, anonymous HTTPS, pinned at `64d2c9b2` |
| `OPENAI_API_KEY` | **needed for lane 2 only** | judge. Same dependency `--judged` already has; `is_judge_available()` already degrades gracefully |
| `ANTHROPIC_API_KEY` | **needed for lane 2, tree family only** | extraction. Per spike-1/3, `ClaudeExtractionProvider` (`EXTRACTION_MODEL = "claude-opus-4-8"`) is the only provider that populates `fact.entities`, without which `_seed_associations()` no-ops and the tree arm is zero by construction. Not needed for the state-machine family. **This dependency was missing from the issue and is the second key, not a variant of the first.** |
| Qdrant / `mem-agent` / `EMem` | **not needed** | only required by upstream's own harness, which we are not running (§No-Gos D2) |
| A free Redis DB | needed | **this harness reserves DB 3.** `--db` is explicit and mandatory; `{0, 14, 15}` forbidden, and this plan adds **13** to the forbidden set (the examples smoke test owns it in CI). See §DB discipline |
| Maintainer sign-off on judge spend | **needed for lane 2** | Q5 |

---

## Solution

### Key Elements

1. **`sme/corpus.py`** — SHA-pinned download + fixture loading + `SmeItem` + canonical ids +
   fingerprint. `iter_items(fixture_path=None, families=None, limit=None, sample="stride",
   seed=0)` — deliberately mirroring the `iter_items` signature the two existing dataset
   adapters expose, so the conventions match even though the registry does not.
2. **`sme/adapters.py`** — `SmeAdapter` Protocol, following SIQ exactly:
   ```python
   class SmeAdapter(Protocol):
       name: str
       def ingest_session(self, session: SmeSession) -> None: ...
       def answer(self, question: str, hint: str | None) -> str: ...
       def teardown(self) -> None: ...
   ```
   Implementations: `NativeAdapter` (SubconsciousMemory, memory *not* named in the agent
   prompt), `InstructedAdapter` (same substrate, memory named — cells C/D), and
   `SnippetBaselineAdapter` (plain top-k over raw turns, no typed substrate — the control).
   `ADAPTERS = {"native": ..., "instructed": ..., "snippet_baseline": ...}`.
3. **Family → primitive routing**, declared in one table in `sme/routing.py` so the claim is
   inspectable rather than buried. Corrected per spike-3 — this is **not** the mapping the
   issue proposes:

   | Family | Route | Requires | Informative without it? |
   |---|---|---|---|
   | state_machine | `ValidityField` supersession (current value on an identity, history retained) | nothing | **yes** — the one free, informative arm |
   | tree | `CoOccurrenceField` seeded by `SubconsciousMemory._seed_associations()`, read by `recipes/graph_traversal.traverse()` | `ANTHROPIC_API_KEY` (only `ClaudeExtractionProvider` populates `fact.entities`) | **no** — heuristic provider emits `entities=[]`, so the graph stays empty |
   | accounting | **none — no ledger primitive exists** (spike-2) | — | **no** — reported as a structural gap, not scored as a Popoto failure |
   | recommendations | not routed this phase (Q2) | — | loaded, skipped with `status="skipped-unrouted"`, counted, reported — never silently dropped |

4. **The zero-by-construction rule.** Any arm whose score is forced to a constant by a
   missing capability rather than by a measured behavior is reported as
   `status="unroutable"` with the reason, and is **excluded from every accuracy denominator
   and from every H1/H2 test**. Concretely: an accounting arm with no ledger primitive, or a
   tree arm run with the heuristic extractor, does not get to be a "0% score." The runner
   refuses to emit an accuracy number for such a cell and emits the gap description instead.
   A test plants an unroutable configuration and asserts the refusal. Without this rule the
   benchmark would manufacture impressive-looking zeros that say nothing about Popoto's
   behavior, which is the inverse of the vacuity trap this repo already guards against.
5. **`sme/extraction_audit.py`** — F3. Parses the accounting corpus's templated messages into
   a gold transaction list, then reads back what the native arm's store actually contains and
   emits correct / omitted / duplicated / hallucinated counts per case. Runs without a judge
   and without an API key. Per spike-1/2 its expected finding is that **no typed transaction
   is recoverable at all** — so it reports, separately from the four counts, a
   `structural_gap` block naming what was missing (no typed slot on `ExtractedFact`; no
   ledger primitive). The four counts are computed against whatever *is* stored (text facts),
   so "the amount appears in a stored sentence but nothing sums it" is distinguishable from
   "the message was never stored."
6. **`sme/runner.py` + `sme/run_sme.py`** — orchestration and CLI.
7. **`sme/compare.py`** — the paired join, the fingerprint refusal, the exact-McNemar tests
   and MDE/power lines from §Research, and the gap artifact.
8. **`sme/report.py`** — JSON + Markdown writer with the family-segregated schema.

### Ingest granularity (pinned, not left to the build)

`SmeAdapter.ingest_session(session)` takes a session, but `extract_memories()` operates on
one text blob per call, and F3's per-transaction accounting depends on which. **Pinned: one
`extract_memories()` call per message**, with `turn_id` set to
`f"{case_id}:{session_id}:{msg_index}"`. Rationale: the corpus's accounting messages are one
transaction per message, so per-message extraction gives F3 a 1:1 gold-to-turn mapping and
makes omission attributable to a specific message. Concatenating a session would make every
omission unattributable and would additionally cross the heuristic provider's sentence
splitter over unrelated transactions. Any deviation invalidates the F3 numbers and must be
re-planned, not improvised.

### CLI

```
python -m tests.benchmarks.sme.run_sme \
    --db 3 \                              # REQUIRED, no default; forbidden: 0, 13, 14, 15
    --arm {native,instructed,snippet_baseline} \
    --hint {on,off} \
    --family accounting,state_machine,tree \
    --fixture tests/benchmarks/sme/fixtures/sme_sample.json \
    --limit 20 --sample stride --seed 0 \
    --judge {stub,live} \                  # default: stub
    --judge-model gpt-4o-mini \
    --dry-run \
    --output tests/benchmarks/results/sme/
```

`--judge stub` is the default on purpose: the expensive, key-requiring, spend-incurring mode
must be the one you opt into by name. `--judge live` with no `OPENAI_API_KEY` exits non-zero
with a clear message rather than silently degrading — the opposite of `run_external.py`'s
`--judged` skip-and-return-0, because here the judge *is* the metric rather than an extra
stage on top of one that already ran.

**Why `--judge-model` is configurable here when `judge.py` refuses to be.** `judge.py`'s
docstring pins its model and states "there is no model-override flag" — because its numbers
claim **Mem0/GAM leaderboard parity**, and a leaderboard comparison is only meaningful at the
protocol's pinned model. This track makes no leaderboard claim: every comparison is
intra-system and paired (§Research), so what matters is that *both sides of a pair used the
same judge*, not that the judge matches someone else's protocol. `compare.py` enforces
exactly that by refusing artifacts with mismatched judge identity. The invariant that
justified pinning does not transfer; the invariant that replaces it is enforced mechanically.
The flag's default is nonetheless `gpt-4o-mini` (matching `judge.py`, cheaper than upstream's
`gpt-4o`), and any deviation from upstream's default is recorded in the artifact and stated
in the report's limits paragraph.

### DB discipline (harness fact 1)

**This harness reserves database 3.** The Redis database map, recorded here so the next
author does not have to rediscover it:

| DB | Owner | Kind of claim |
|---:|---|---|
| **0** | the live agent store on a developer machine | **never touch** — `FLUSHDB` is refused in popoto's own client |
| **3** | **this harness (SME)** | new reservation — advisory, see below |
| 1, 2, 4, 11, 12 | ad-hoc repro/scratch | **conventional *and* liable to ad-hoc lane use.** Several `docs/plans/` lanes name them for one-off scripts and narrow-scope `POPOTO_TEST_DB` runs, and a parallel session assigns them to lanes at runtime; a committed doc is not the only way one gets taken |
| **13** | `examples/tests/test_kitchen_smoke.py` | **standing CI reservation** — `.github/workflows/examples.yml:71` sets `REDIS_URL: redis://localhost:6379/13` on the job env |
| **14** | `run_external.py` | `BENCH_DB_DEFAULT = 14`, the `POPOTO_BENCH_DB` default |
| **15** | pytest plugin | `popoto_test_db = "15"` in `pyproject.toml` |
| *any* | **`POPOTO_BENCH_DB`** | **the row that outranks every other row.** Operators point `run_external` at arbitrary databases — a full-corpus LoCoMo run held DB 10 for ~6 hours on 2026-09-16 via `POPOTO_BENCH_DB=10`, while nothing committed claimed 10 at all |

**A reservation table records intent, not enforcement.** There is no registry, no lock, and no
mechanism that stops a second process from binding the database this harness picked;
`POPOTO_BENCH_DB` alone means any run can land anywhere. So the table above is a courtesy to
the next author, and **the runtime assertion below is the only thing that actually protects a
run.** Do not read a row here as a guarantee, and do not skip the read-back because the table
says a database is free.

3 is chosen as a low database with no standing claim: no workflow, no `pyproject.toml` key, no
`run_external` default, and no live lane binds it. That is weaker than "unclaimed" — a past
lane names `POPOTO_TEST_DB=3` in `docs/plans/history_shaped_state_roundtrip.md`, as some
committed plan doc does for essentially every low database — which is precisely why the claim
this plan makes is "assert it at runtime", not "3 is safe".

```python
FORBIDDEN_DBS = frozenset({0, 13, 14, 15})   # 0 live store, 13 examples smoke (CI),
                                             # 14 run_external bench, 15 pytest plugin
```

**Deliberately four, not more: the harness does not forbid its own neighbours.** 1, 2, 4, 11
and 12 stay *allowed* as `--db` values. The four in the set are each a standing claim by a
named, reproducible owner, so binding one is unambiguously a mistake and a static refusal is
right. The scratch databases have no standing owner — their occupancy is a property of what is
running right now, which a frozenset written today cannot know. Forbidding them would trade a
real capability (an operator deliberately isolating a run on a free low database) for a check
that still could not tell an occupied database from an empty one. Occupancy is the read-back
assertion's job, not the allowlist's; the honest division is **static refusal for standing
claims, runtime assertion for everything else.**

`--db` required with **no default**, validated against the set, then — **after every import
and after any repoint** — the runner reads the database number back off the live connection
pool and asserts it equals `--db`, raising `SmeDbError` otherwise. This specifically catches
`POPOTO_BENCH_DB` or a stale `REDIS_URL` having moved the pool underneath us. The
**asserted** value (not the requested one) is stamped into the artifact as
`environment.redis_db`, next to `redis_py_version` and `popoto_version`.

**This guard is not invented for this harness — it is the shipped `examples/` pattern.**
`examples/tests/conftest.py` already does exactly this and for exactly the reason that
applies here: its module docstring records that popoto's `pytest11` plugin binds the
connection in `pytest_configure`, *before collection*, so "by the time a fixture runs, the
connection is already bound — setting the variable here would be a no-op that reads as a
safety net." Its `require_isolated_redis` fixture therefore only **asserts** the binding,
fails loudly when `REDIS_URL` is unset, and refuses database 0 outright ("this suite seeds
and clears data; refusing to touch database 0"). The SME runner's read-back assertion is the
same guard moved from a fixture to a CLI entry point, plus the `POPOTO_BENCH_DB` case, which
`examples/` does not have to contend with because it opts the plugin out entirely
(`addopts = "-p no:popoto"`).

### Flow

1. Resolve corpus (fixture or pinned download), build items, compute fingerprint.
2. Validate and assert the DB; sweep stale `Sme*` keys.
3. Per case: fresh isolated store → ingest all sessions through the adapter → answer each
   query (with or without the hint text) → record raw response.
4. Judge every `(question, reference_text, response)` triple → F1.
5. Collapse to scenario level → F2.
6. For accounting cases on the native arm, run the extraction audit → F3.
7. Write artifact. Repeat per cell.
8. `compare.py` pairs cells and writes the gap artifact.

---

## Failure Path Test Strategy

### Exception handling coverage

- Download failure / upstream 404 / SHA mismatch → `SmeCorpusError` naming the pinned SHA;
  never falls back to "whatever `main` is today."
- `--judge live` without `OPENAI_API_KEY` → non-zero exit, explicit message.
- Judge returns something other than `1`/`0` → counted as `judge_malformed`, item marked
  `status="error"`, excluded from the F1 denominator, surfaced in the report, and gated by a
  `--judge-error-threshold` (default 0.25, matching `run_external.py`'s convention).
- Adapter raises during ingest → the case is marked errored **as a whole** (a partially
  ingested ledger produces a confidently wrong answer, which is worse than no answer).
- `compare.py` on mismatched fingerprints → `SmeComparabilityError` + symmetric difference.
- `--db` in `FORBIDDEN_DBS`, or post-import assertion mismatch → `SmeDbError`.

### Empty / invalid input handling

- Zero items after `--family` / `--limit` filtering → **hard error**, never a vacuous
  green run reporting `accuracy=0.0` over `n=0`.
- A case with zero queries, or a query with an empty `reference_answer` → skipped with a
  named status and counted.
- Every skip category appears in the report with its count; the sum of all statuses must
  equal the item total, asserted in a test.

### Error state rendering

The Markdown report leads with a status table (`ok / error / skipped-unrouted /
skipped-empty / judge_malformed`) **before** any accuracy number, so a run that mostly
failed cannot be read as a run that mostly scored low.

---

## Test Impact

New file `tests/benchmarks/test_sme.py`, offline, no network, no API key, DB-free where
possible:

| Test | Asserts |
|---|---|
| `test_item_ids_canonical_and_stable` | ids are deterministic across two loads and across `--sample` modes |
| `test_fingerprint_changes_on_corpus_mutation` | mutating one reference answer changes the fingerprint |
| `test_compare_refuses_mismatched_fingerprint` | `SmeComparabilityError` raised; symmetric difference reported |
| `test_compare_refuses_mismatched_judge_identity` | `SmeComparabilityError` raised when two artifacts' judge model or prompt SHA-256 differ (Risk 2's second refusal condition) |
| `test_multi_reference_expansion` | an accounting case with 3 reference answers yields 3 items with distinct `a{ai}` suffixes |
| `test_all_families_load_from_fixture` | one loader handles all shipped family shapes |
| `test_forbidden_dbs` | 0, 13, 14, 15 rejected; a valid db accepted |
| `test_db_assertion_catches_repoint` | with `POPOTO_BENCH_DB` set to a different db, the runner raises rather than proceeding |
| `test_stub_judge_end_to_end` | full run on the committed fixture with `--judge stub` produces a schema-valid artifact |
| `test_report_families_are_segregated` | no table mixes F1/F2/F3 columns; metric-families section present |
| `test_status_counts_sum_to_total` | no silently dropped items |
| `test_empty_selection_is_an_error` | `n=0` is a failure, not a 0.0 score |
| `test_extraction_audit_on_synthetic_ledger` | known omission / duplication / hallucination are each detected |
| `test_unroutable_cell_emits_no_accuracy` | an accounting cell, and a tree cell with the heuristic extractor, each yield `status="unroutable"` with a reason and no accuracy number, and are excluded from the denominator |
| `test_mcnemar_and_power_on_fixed_table` | exact McNemar, MDE, underpowered flag and Holm–Bonferroni reproduce known values on a fixed synthetic contingency table |
| `test_ingest_granularity_is_per_message` | `NativeAdapter.ingest_session` issues one `extract_memories()` call per message with the pinned `turn_id` shape |
| `test_upstream_attribution_present` | `fixtures/UPSTREAM.md` exists and names the pinned SHA and the Apache-2.0 license |
| `test_sme_artifacts_have_spec_entries` | the `tests/benchmarks/results/sme/*_latest*.md` glob is non-empty (fails on an empty results dir) **and** every matched stem has a matching `Spec` in `gen_benchmark_pages.py` — both conjuncts required so the check cannot pass vacuously (the falsifiable orphan-artifact check — see §Architectural Impact) |

Narrow-scope run for this lane: `pytest tests/benchmarks/test_sme.py`. Note the standing
repo gotcha — if any `Defaults` constant is added, it must be registered in
`tests/benchmarks/test_defaults_sync.py`, which narrow selection will not reach.

---

## Rabbit Holes

- **Building a real agent loop with tools and a goal state.** This is the biggest one and it
  is why question 2 is answered with a proxy. StructMemEval has no goal state; inventing one
  makes this our benchmark, not theirs, and drags an agent framework into the critical path.
- **Vendoring / wrapping upstream's harness to get competitor numbers.** Qdrant + two
  third-party repos + paid keys + a WIP upstream. Deferred to a separate issue.
- **Reimplementing Mem0's retrieval "so the control is fair."** The control is a control.
  The moment it is presented as a competitor, every methodological objection in the book
  applies and the number is worthless. Keep it a floor check.
- **Chasing the recommendations family.** 931 KB, no obvious Popoto primitive routing, and
  it is absent from the issue's framing. Load it, skip it, count it, and revisit.
- **`big_bench`.** 54 more cases, larger contexts, materially more spend, and no additional
  hypothesis tested. Out.
- **Unifying `sme/` back into `run_external.py`.** Recorded as a non-goal in the package
  docstring precisely because it will look like duplication to a later reader.
- **Tuning Popoto until the gap closes.** If H2 fails we report that it failed. Tuning the
  arm until the headline holds is the failure mode this whole epic exists to avoid.

---

## Risks

### Risk 1 — The negative result is the likely result
Extraction may simply not route conversational turns into the typed ledger, making every
Popoto cell low and the "gap" trivially small because *everything* is near the floor.
**Mitigation:** cell D is the floor check (§Research); F3 measures the write path directly
and independently of the judge, so a low F1 is diagnosable rather than mysterious; and the
report template has a pre-agreed "uninformative run" verdict that is a legitimate outcome.

### Risk 2 — Judge variance swamps the effect
A binary judge at `temperature=0` still varies across model versions, and B−A may be smaller
than that variance. **Mitigation:** paired McNemar on discordant cells rather than a
difference of means; identical judge model and prompt SHA recorded in both artifacts and
asserted equal by `compare.py` (a second refusal condition alongside the fingerprint).

### Risk 3 — Upstream moves and two runs stop being comparable
The repo is self-described work-in-progress and has *already* diverged from the issue's
description (51 vs 73 scenarios, an unmentioned fourth family). **Mitigation:** pin the
commit SHA, stamp it in every artifact, and make the fingerprint a refusal condition.

### Risk 4 — A stale `POPOTO_BENCH_DB` or `REDIS_URL` silently lands the run on the wrong DB
Verified live behavior, not hypothetical. **Mitigation:** mandatory `--db`, forbidden set,
and the post-import read-back assertion; the asserted value is what gets recorded.

### Risk 5 — Extraction spend, which is the larger number
Per spike-1/3 the tree family needs `ClaudeExtractionProvider` at `claude-opus-4-8` — a
top-tier model — called **once per message** (pinned granularity). The tree family is 10
cases × 250 messages = **2,500 extraction calls per ingest**, repeated for every cell that
ingests. This dwarfs the judge cost and was invisible in the issue's framing.
**Mitigations:** (a) extraction results are cached by `(case_id, session_id, msg_index,
provider, model)` and reused across cells, so ingest is paid **once**, not per cell — the
arms differ in the *query* phase, not the write phase, for every cell sharing an adapter
substrate; (b) a cost estimate is printed and confirmed before any live extraction; (c) the
extraction model tier is already an open maintainer decision under #489, so this run should
not settle it unilaterally — see Q5. Note the **recommendations family is loaded but not
routed (D6), and therefore is never ingested**, so its 931 KB contributes nothing to spend.

### Risk 6 — Judge spend runs away
Rough order: 51 scenarios but question counts are lopsided (tree alone ≈ 320 questions), so
the full small bench is **≈ 379 judged units per cell** (accounting 15 × 1 × 3 refs = 45,
state machine 14, tree 10 × 32 = 320; recommendations is loaded but never ingested per D6
and contributes 0). Five cells ≈ 2–3k generation +
judge call pairs. At `gpt-4o-mini` rates that is single-digit dollars; at `gpt-4o` (upstream's
default judge) it is materially more. **Mitigation:** print a cost estimate and require
confirmation before a live run, as `run_external.py` already does; default the judge model
to `gpt-4o-mini` and record the deviation from upstream's `gpt-4o` default in the artifact
and in the report's limits paragraph. LLM-based *extraction* is off by default — turning it
on multiplies the ingest cost by the message count, which is the larger number.

### Risk 7 — Concurrent SDLC lanes contend on Redis
Standing repo hazard. **Mitigation:** explicit `--db` per lane, key sweep scoped to `Sme*`
prefixes, and no flush of any kind.

### Risk 8 — Someone tabulates F1 next to a LongMemEval recall number
The exact thing doctrine forbids, and the most likely way this track produces a wrong claim.
**Mitigation:** §Metric Families is enforced by test, the schema is family-nested with no
flat summary, and the "never_compare_to" strings are carried into the rendered report.

### Risk 9 — Publishing a headline from the smoke fixture
The committed fixture is a handful of cases; a number from it is not a result.
**Mitigation:** artifacts record `fixture` and `n`, and the docs `Spec` for the headline page
reads only the full-corpus artifact. A fixture-derived artifact is marked
`"provisional": true` and the report writer refuses to emit headline-claim language for it.

---

## Race Conditions

- Two arms writing to the same Redis DB concurrently would interleave stores. Arms run
  sequentially, or on distinct `--db` values; the runner takes no lock and does not pretend
  to. Documented.
- `*_latest_{arm}_{hint}` symlink updates from two concurrent runs of the *same* cell:
  mitigated by the same date-stamped-file + symlink pattern `save_reports()` already uses,
  including its `shutil.copy2` fallback on `OSError`.
- Judge calls are independent and safe to parallelize; if a worker pool is used, results are
  keyed and reassembled by `item_id`, never by completion order.

---

## No-Gos (Out of Scope)

| # | Not doing | Why |
|---|---|---|
| D1 | A real agentic tool loop with a goal state | No goal state in the data; unbounded scope; belongs to PTR |
| D2 | Running Mem0 / mem-agent / upstream's harness | Requires Qdrant + `mem-agent` + `EMem` + paid keys against a WIP upstream |
| D3 | Publishing any Popoto-vs-competitor head-to-head table | Nothing in this phase produces a competitor number under our conditions; the paper's numbers are a different harness and may appear only as cited prose |
| D4 | Producing any recall@k number from this corpus | No gold document ids exist (§Metric Families, F4) |
| D5 | `big_bench` splits | 54 extra cases, more spend, no extra hypothesis |
| D6 | Routing the recommendations family | Loaded, skipped, counted; revisit after Q2 |
| D7 | Modifying `run_external.py` | Its scoring path is recall-based and does not apply |
| D8 | Any change under `src/popoto/` | This is a benchmark track, not a library feature |
| D9 | Redis modules | Standing epic constraint |
| D10 | Tuning Popoto's arm until H2 holds | Defeats the purpose of the measurement |

---

## Success Criteria

Lane 1 (schedulable now, no key, no spend):

- [ ] `python -m tests.benchmarks.sme.run_sme --fixture tests/benchmarks/sme/fixtures/sme_sample.json --db 3 --judge stub` exits 0, performs **zero network requests**, requires **no API key**, and writes a schema-valid artifact under `tests/benchmarks/results/sme/`; this artifact is committed in task 11 so the orphan-`Spec` check below has a real target.
- [ ] `pytest tests/benchmarks/test_sme.py` passes; every test in §Test Impact exists.
- [ ] `sme/compare.py` raises `SmeComparabilityError` on a fingerprint mismatch **and** on a judge-identity mismatch, each covered by a test.
- [ ] Every artifact records: upstream commit SHA, corpus fingerprint, **asserted** `redis_db`, judge model + prompt SHA-256, `popoto` + `redis-py` versions, `n` per family, and the full status breakdown summing to the item total.
- [ ] The artifact JSON has **no flat mixed-family summary**; `f1_judged` / `f2_goal` / `f3_extraction` are separate blocks each carrying `unit` and `never_compare_to`.
- [ ] F3 reports correct / omitted / duplicated / hallucinated counts for the accounting family against a corpus-derived gold transaction list, with the audit's own detection verified on a synthetic case, **plus** a `structural_gap` block naming the missing typed slot and the absent ledger primitive (§Spike Results).
- [ ] The zero-by-construction rule is enforced: a cell configured unroutably (accounting with no ledger primitive; tree with the heuristic extractor) emits `status="unroutable"` with a reason and **no accuracy number**, and is excluded from every denominator. Covered by a test that plants such a configuration.
- [ ] `compare.py` computes exact McNemar per family and pooled, prints the declared MDE and the underpowered flag per family, and applies Holm–Bonferroni across families — all covered by a test with a fixed synthetic contingency table.
- [ ] `fixtures/UPSTREAM.md` records repo, commit SHA, Apache-2.0, and the paper citation.
- [ ] `docs/scripts/gen_benchmark_pages.py` has `Spec` entries for the SME artifacts, its `_warn_orphan_artifacts()` scan is extended to cover `results/sme/`, and `test_sme_artifacts_have_spec_entries` (a new test asserting the `results/sme/*_latest*.md` glob is non-empty and every matched stem has a matching `Spec`) passes against the stub artifact committed by task 11 — this is the falsifiable check; `mkdocs build --strict` passing is necessary but cannot by itself detect a missing `Spec`, since the scan only ever prints to stderr.
- [ ] No file under `src/popoto/` is modified; `run_external.py` is unchanged.

Lane 2 (needs `OPENAI_API_KEY` + maintainer go-ahead):

- [ ] All four cells plus the snippet control run on the full pinned small-bench corpus, with committed artifacts under `tests/benchmarks/results/sme/`.
- [ ] A gap report states **B − A per family** with paired discordant counts, an explicit verdict on H1 and H2 against the pre-registered criteria, and — if cell D does not clear the numeric floor check (D − A ≥ 15 points **and** McNemar significant) — an "uninformative run" verdict instead of a gap claim.
- [ ] Every family whose declared MDE exceeds 15 points is labelled **underpowered** in the report and issues no H2 verdict.
- [ ] Limits are disclosed in the same breath as every number: Popoto-only, no competitor arms, judge model deviates from upstream's default, proxy goal-completion definition, pinned corpus SHA.
- [ ] Headline propagation per #511 is either done or explicitly recorded as "no headline change" in the closing comment.

---

## Update System

**N/A — stated rather than omitted.** This track adds no migration, no schema version, and no
stored format that an existing deployment would need to upgrade through. Nothing under
`src/popoto/` changes (D8), so no released behavior moves. The only versioned thing it
introduces is the **pinned upstream corpus SHA**, and its "upgrade path" is deliberate: a new
SHA produces a new corpus fingerprint, which `compare.py` treats as a hard refusal rather
than a migration (§Data Flow).

## Agent Integration

**N/A — stated rather than omitted.** No agent-facing surface changes: no new MCP tool, no
new public recipe, no change to `SubconsciousMemory`'s call shape. The track *measures* the
agent integration that already exists. If its findings justify a typed write path (the
likely outcome per §Spike Results), that is a separate feature issue with its own plan —
see Q7.

## Team Orchestration

| Role | Scope |
|---|---|
| builder-1 | Lane 1 tasks 1–3 (corpus, fixture + attribution, DB discipline) — self-contained, no adapter dependency |
| builder-2 | Lane 1 tasks 4–6 (routing table, adapters, judge adapter, extraction audit) — depends on task 1's `SmeItem` |
| builder-3 | Lane 1 tasks 7–9 (report, runner/CLI, compare + statistics) — depends on the artifact schema from task 7 |
| test-engineer | Task 10, the full §Test Impact table, especially the vacuity guards (zero-by-construction refusal, empty-selection error, fingerprint refusal) |
| documentarian | Task 11 (docs `Spec` entries, framing page) and the Lane 2 writeup |
| validator | §Verification end to end, plus confirming `git diff --stat main` touches nothing under `src/popoto/` |

Builders share one worktree with disjoint file sets (one module each), so commits do not
interleave. Tasks 1–3 and 7 can start in parallel; 4–6 and 8–9 serialize behind them.

## Documentation

- **Feature docs:** a new `docs/benchmarks/` page for the track, generated from the artifact
  by `gen_benchmark_pages.py`, plus a short hand-written framing page stating what
  StructMemEval measures, what our version does and does not claim, and the metric families.
- **Plan doc:** this file.
- **Inline:** `sme/__init__.py` docstring records (a) why this is not a `run_external.py`
  dataset, (b) the pinned upstream SHA, (c) the metric-family rule, so a later editor hits
  all three before "simplifying."
- **Issue comment:** a closing comment recording the verdict on H1/H2 and, per #511, either
  the propagated headline claims or "no headline change."
- **`docs/plans/benchmarking_strategy_2026-07.md`** gains a line placing this track relative
  to PTR.

---

## Step by Step Tasks

**Lane 1**

1. `sme/corpus.py`: SHA-pinned download + fixture load + `SmeItem` + canonical ids + fingerprint. Tests: id stability, fingerprint sensitivity, multi-reference expansion, all-families load.
2. Commit the curated fixture (2 cases per routed family, ≈ 60 KB) + `fixtures/UPSTREAM.md`.
3. `sme/db.py`: `FORBIDDEN_DBS`, `validate_db()`, post-import read-back assertion, `SmeDbError`. Tests including the `POPOTO_BENCH_DB` repoint case.
4. `sme/adapters.py` + `sme/routing.py`: Protocol, three adapters, the corrected family→primitive routing table, and the zero-by-construction refusal. **Before writing the adapters, run the one live spike the source read could not settle:** call `SubconsciousMemory(...).extract_memories()` on one real accounting message and one real tree message with the heuristic provider, on an explicit scratch DB, and record what is actually stored. Source reading says `entities=[]` and untyped text; confirm it rather than build on it.
5. `sme/judge_adapter.py`: reuse `judge.py`'s `JudgeProtocol`; vendored upstream prompt + its SHA; deterministic stub judge.
6. `sme/extraction_audit.py` (F3) + its synthetic-case test.
7. `sme/report.py`: family-segregated schema + Markdown writer with the status table first.
8. `sme/runner.py` + `sme/run_sme.py`: CLI, orchestration, cost estimate, `--dry-run`.
9. `sme/compare.py`: paired join, both refusal conditions, discordant cells, gap artifact.
10. `tests/benchmarks/test_sme.py`: the full table in §Test Impact.
11. Commit the `--judge stub` run's artifact (produced per Success Criteria bullet 1, once task 8's runner exists) under `tests/benchmarks/results/sme/`, giving the glob below a real target; `gen_benchmark_pages.py` `Spec` entries for it; extend `_warn_orphan_artifacts()`'s glob to also cover `tests/benchmarks/results/sme/`; add `test_sme_artifacts_have_spec_entries` (asserting the glob is non-empty and every stem has a matching `Spec`); `mkdocs build --strict`.
12. Narrow-scope test run + lint/format/mypy-ratchet; PR.

**Lane 2** (gated on Q5)

13. Confirm cost estimate, get the go-ahead, run the five cells on the pinned corpus.
14. Commit artifacts; run `compare.py`; write the gap report with the H1/H2 verdict.
15. Docs pages; headline propagation per #511; close with the verdict comment.

---

## Verification

1. `pytest tests/benchmarks/test_sme.py -q`
2. `python -m tests.benchmarks.sme.run_sme --fixture tests/benchmarks/sme/fixtures/sme_sample.json --db 3 --judge stub --dry-run`
3. Same without `--dry-run`; inspect the artifact for every field in §Success Criteria.
4. `POPOTO_BENCH_DB=9 python -m tests.benchmarks.sme.run_sme --db 3 ...` → must raise `SmeDbError`, not run.
5. `ruff check src/` (unchanged), `black --check src/ tests/`, `scripts/mypy_ratchet.py`.
6. `pytest tests/benchmarks/test_sme.py -k test_sme_artifacts_have_spec_entries` → the `results/sme/*_latest*.md` glob is non-empty (the stub artifact committed by task 11) and every matched stem has a matching `Spec` (the falsifiable check; `mkdocs build --strict` cannot detect a missing `Spec` on its own — see §Architectural Impact).
7. Confirm `git diff --stat main` touches nothing under `src/popoto/`.

---

## Questions for the architect

**Q1 — The corpus does not match the issue's description.** The issue says "73 synthetic
scenarios, 544 questions" and names three families. Upstream at `64d2c9b2` ships **51**
small-bench scenarios across **four** families (accounting 15, tree 10, state machine 14,
**recommendations 12**), with question counts dominated by the tree family (10 × 32 = 320),
plus a `big_bench` split of 54 more cases. I have planned against what upstream actually
ships and pinned the SHA. Confirm that is right, and whether the recommendations family
should be routed rather than skipped.

**Q2 — Recommendations family: route it or drop it?** It is 931 KB (60% of the corpus), has
no obvious Popoto primitive mapping, and is absent from the issue's framing. This plan loads
it, skips it with a counted status, and does not score it. Alternative: drop it from the
loader entirely, or find a routing.

**Q3 — Is the Popoto-only hint-gap study an acceptable deliverable for this issue?** The
issue's title and acceptance criteria say "head-to-head" and competitor arms. I am asserting
those are not reachable without Qdrant + `mem-agent` + `EMem` + paid keys against a WIP
upstream, and that citing the paper's table beside ours violates the cross-comparison
doctrine. If the head-to-head is non-negotiable, this issue is **blocked**, not merely
descoped, and I should stop rather than build lane 1.

**Q4 — Should the competitor work be filed as a separate issue now?** I would file "Popoto
adapter into upstream's StructMemEval harness (Qdrant + mem-agent + EMem)" as a distinct
issue so the dependency is tracked rather than lost in this plan's No-Gos.

**Q5 — Spend, on two keys not one.** Judge: upstream defaults to `gpt-4o`, our `judge.py`
pins `gpt-4o-mini`; I have planned for `gpt-4o-mini` with the deviation disclosed, roughly
2–3k generation+judge call pairs for the full five-cell run. **Extraction is the bigger
line and was missing from the issue:** the tree family needs `ANTHROPIC_API_KEY` with
`claude-opus-4-8` at one call per message — 10 cases × 250 messages = 2,500 top-tier calls
per ingest, cached across cells (Risk 5). Confirm both keys are available and the spend is
approved, and note this brushes against #489's still-open extraction-model-tier decision,
which I do not think this run should settle unilaterally.

**Q6 — The issue's primitive mapping is wrong, and the gap it hides is the real finding.**
Per spike-2, `PredictionLedgerMixin` is a prediction-*error* ledger, not an accounting
ledger, and per spike-1 no extraction provider — including the paid Claude one — can emit a
typed value at all. `ExtractedFact` (`src/popoto/extraction/__init__.py:48`) has twelve
fields — `text`, `entities`, `importance`, `confidence`, `span_start`, `span_end`,
`turn_id`, `candidate_id`, `generator_rule`, `verbatim`, `resolution_status`, `assumption`
— and every one of them is `str` / `float` / `int` / `list[str]`; the only two numeric
fields (`span_start`, `span_end`) are character offsets into the source turn, i.e.
provenance, not a domain value — none is a slot for a typed *domain* value, relation, or
role. So there is **no subconscious write path into any typed structure**, and the
count-based family has nothing to route to. I have planned to report that as a structural
gap rather than score it as 0%. Confirm that is the wanted disposition. The alternative —
hand-writing a benchmark-local structured extractor so the accounting arm has something to
measure — would be measuring code written for the benchmark, not Popoto, and I recommend
against it.

**Q7 — Should a feature issue be filed for typed/structured extraction?** The gap above is
arguably the highest-value thing this investigation found, and it is a *feature*, not a
benchmark. It also touches #489 (extraction model-tier decision, still open). I would file
"structured write path: typed slots on ExtractedFact + a transaction-ledger primitive" as a
separate issue and note that #498 motivates it. Confirm before I file.

**Q8 — Is the proxy goal-completion definition acceptable?** F2 is "all questions in a
scenario judged correct," which is a scenario-level collapse of F1, not an independent
signal. It is honest but it is not "did the agent finish a task." If a real task-completion
signal is wanted, that is PTR and a different corpus.
