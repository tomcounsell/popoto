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
  the structural hint in the prompt does not exceed accuracy without it by more than the
  paired confidence interval. If H2 fails, the typed-substrate thesis is wrong as stated
  and we report that.

H2 is the one worth chasing because it is **measurable without a competitor**. It is an
intra-system, paired, same-metric-family difference computed over identical item ids. That
is the design's load-bearing property: it needs nothing we cannot run.

### Anti-hypothesis (where we expect to lose)

Issue question 4 is correct and this plan treats it as the likely negative result.
`PredictionLedgerMixin` guarantees clean aggregation **once entries are in it**. Nothing
proves `SubconsciousMemory.extract_memories()` routes `"Alice: Paid €179 for museum -
split with Bob"` into a typed ledger entry without a human writing the schema mapping.
StructMemEval's documented accounting failure modes — omission, duplication, hallucination
of transactions — land on that extraction step, not on the aggregation step we are strong
at. So **extraction fidelity is a first-class measured output of this plan**, with its own
metric family, and a plausible outcome of the whole track is "the substrate is right and
the write path does not populate it." That is a useful result and the plan must be able to
report it without being called a failure.

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

## Prior Art

### In this repo

| Thing | Path | What it gives us |
|---|---|---|
| External harness | `tests/benchmarks/run_external.py` (1623 lines) | recall@k/MRR against `relevant_ids`; hardcoded `DATASET_CHOICES = ("longmemeval-s", "locomo")` dispatch; `_resolve_bench_db()`; `save_reports()` artifact conventions |
| Item contract | `tests/benchmarks/datasets/__init__.py` | `BenchmarkItem(item_id, history, query, relevant_ids, metadata)`; adapters expose `iter_items(fixture_path, limit, sample, seed)` |
| Tier 5 judge | `tests/benchmarks/judge.py` | **`JudgeProtocol`** (`chat(model, messages, temperature) -> str`), `is_judge_available()`, `build_openai_client()`, `estimate_cost()`, `judge_identity()` recording prompt SHA-256s |
| SIQ | `tests/benchmarks/siq/` | the **arms** pattern: `SiqAdapter` Protocol + `ADAPTERS = {...}` registry + `--adapter` flag; `QueryOnlyStubAdapter` as a dependency-free control that scores ~0 *by construction*, proving the harness is not vacuous |
| RLT | `tests/benchmarks/rlt/` | the **DB discipline** pattern: `run_rlt.py` requires `--db` with **no default**, `FORBIDDEN_DBS = {0, 14, 15}`, `validate_db()` raising `RltDbError` |
| Docs generation | `docs/scripts/gen_benchmark_pages.py` | `Spec` objects map `*_latest.{json,md}` artifacts to generated pages; unspecced artifacts raise a loud orphan warning |

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
| 4 | Which primitive routes each family, and does the write path populate it? | **Measured, never assumed** — extraction fidelity is its own deliverable and its own metric family | The accounting family's messages are template-generated (`"{who}: Paid €{amt} for {what} - split {with}"`), so a **gold transaction list is derivable by parsing the corpus**. That gives us a write-path ground truth *upstream does not have*, and is the single highest-value thing in this plan. |
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
  sme/runner.py  --arm {native,native_instructed,snippet_baseline} --hint {on,off}
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
- **`docs/scripts/gen_benchmark_pages.py` gains `Spec` entries** for the SME artifacts.
  Without them the generator emits an orphan-artifact warning; with them the results page
  is generated at build time from the committed artifact, per #453.
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
| `OPENAI_API_KEY` | **needed for lane 2 only** | same dependency `--judged` already has; `is_judge_available()` already degrades gracefully |
| Qdrant / `mem-agent` / `EMem` | **not needed** | only required by upstream's own harness, which we are not running (§No-Gos D2) |
| A free Redis DB | needed | `--db` is explicit and mandatory; `{0, 14, 15}` forbidden, and this plan adds **13** to the forbidden set (the examples smoke test owns it) |
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
   inspectable rather than buried: accounting → `PredictionLedgerMixin` / ledger aggregation;
   state_machine → `MemoryLifecycle` + `EventStreamMixin` current-state; tree → `CoOccurrenceField`
   + `recipes/graph_traversal.traverse()`. Recommendations is **not routed** in this phase
   (Q2) and its items are loaded but skipped with `status="skipped-unrouted"`, counted, and
   reported — never silently dropped.
4. **`sme/extraction_audit.py`** — F3. Parses the accounting corpus's templated messages into
   a gold transaction list, reads back what the native arm's typed store actually contains,
   and emits correct / omitted / duplicated / hallucinated counts per case. Runs without a
   judge and without an API key.
5. **`sme/runner.py` + `sme/run_sme.py`** — orchestration and CLI.
6. **`sme/compare.py`** — the paired join, the fingerprint refusal, McNemar-style discordant
   cells, and the gap artifact.
7. **`sme/report.py`** — JSON + Markdown writer with the family-segregated schema.

### CLI

```
python -m tests.benchmarks.sme.run_sme \
    --db 12 \                              # REQUIRED, no default; forbidden: 0, 13, 14, 15
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

### DB discipline (harness fact 1)

```python
FORBIDDEN_DBS = frozenset({0, 13, 14, 15})   # 0 prod-shaped, 13 examples smoke,
                                             # 14 run_external bench, 15 pytest
```
`--db` required, validated against the set, then — **after every import and after any
repoint** — the runner reads the database number back off the live connection pool and
asserts it equals `--db`, raising `SmeDbError` otherwise. This specifically catches
`POPOTO_BENCH_DB` or a stale `REDIS_URL` having moved the pool underneath us. The
**asserted** value (not the requested one) is stamped into the artifact as
`environment.redis_db`, next to `redis_py_version` and `popoto_version`.

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
| `test_multi_reference_expansion` | an accounting case with 3 reference answers yields 3 items with distinct `a{ai}` suffixes |
| `test_all_families_load_from_fixture` | one loader handles all shipped family shapes |
| `test_forbidden_dbs` | 0, 13, 14, 15 rejected; a valid db accepted |
| `test_db_assertion_catches_repoint` | with `POPOTO_BENCH_DB` set to a different db, the runner raises rather than proceeding |
| `test_stub_judge_end_to_end` | full run on the committed fixture with `--judge stub` produces a schema-valid artifact |
| `test_report_families_are_segregated` | no table mixes F1/F2/F3 columns; metric-families section present |
| `test_status_counts_sum_to_total` | no silently dropped items |
| `test_empty_selection_is_an_error` | `n=0` is a failure, not a 0.0 score |
| `test_extraction_audit_on_synthetic_ledger` | known omission / duplication / hallucination are each detected |
| `test_upstream_attribution_present` | `fixtures/UPSTREAM.md` exists and names the pinned SHA and the Apache-2.0 license |

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

### Risk 5 — Judge spend runs away
Rough order: 51 scenarios but question counts are lopsided (tree alone ≈ 320 questions), so
the full small bench is ≈ 400–600 judged units per cell. Five cells ≈ 2–3k generation +
judge call pairs. At `gpt-4o-mini` rates that is single-digit dollars; at `gpt-4o` (upstream's
default judge) it is materially more. **Mitigation:** print a cost estimate and require
confirmation before a live run, as `run_external.py` already does; default the judge model
to `gpt-4o-mini` and record the deviation from upstream's `gpt-4o` default in the artifact
and in the report's limits paragraph. LLM-based *extraction* is off by default — turning it
on multiplies the ingest cost by the message count, which is the larger number.

### Risk 6 — Concurrent SDLC lanes contend on Redis
Standing repo hazard. **Mitigation:** explicit `--db` per lane, key sweep scoped to `Sme*`
prefixes, and no flush of any kind.

### Risk 7 — Someone tabulates F1 next to a LongMemEval recall number
The exact thing doctrine forbids, and the most likely way this track produces a wrong claim.
**Mitigation:** §Metric Families is enforced by test, the schema is family-nested with no
flat summary, and the "never_compare_to" strings are carried into the rendered report.

### Risk 8 — Publishing a headline from the smoke fixture
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

- [ ] `python -m tests.benchmarks.sme.run_sme --fixture tests/benchmarks/sme/fixtures/sme_sample.json --db 12 --judge stub` exits 0, performs **zero network requests**, requires **no API key**, and writes a schema-valid artifact.
- [ ] `pytest tests/benchmarks/test_sme.py` passes; every test in §Test Impact exists.
- [ ] `sme/compare.py` raises `SmeComparabilityError` on a fingerprint mismatch **and** on a judge-identity mismatch, each covered by a test.
- [ ] Every artifact records: upstream commit SHA, corpus fingerprint, **asserted** `redis_db`, judge model + prompt SHA-256, `popoto` + `redis-py` versions, `n` per family, and the full status breakdown summing to the item total.
- [ ] The artifact JSON has **no flat mixed-family summary**; `f1_judged` / `f2_goal` / `f3_extraction` are separate blocks each carrying `unit` and `never_compare_to`.
- [ ] F3 reports correct / omitted / duplicated / hallucinated counts for the accounting family against a corpus-derived gold transaction list, with the audit's own detection verified on a synthetic case.
- [ ] `fixtures/UPSTREAM.md` records repo, commit SHA, Apache-2.0, and the paper citation.
- [ ] `docs/scripts/gen_benchmark_pages.py` has `Spec` entries for the SME artifacts (no orphan warning) and `mkdocs build --strict` passes.
- [ ] No file under `src/popoto/` is modified; `run_external.py` is unchanged.

Lane 2 (needs `OPENAI_API_KEY` + maintainer go-ahead):

- [ ] All four cells plus the snippet control run on the full pinned small-bench corpus, with committed artifacts under `tests/benchmarks/results/sme/`.
- [ ] A gap report states **B − A per family** with paired discordant counts, an explicit verdict on H1 and H2, and — if cell D is not materially above cell A — an "uninformative run" verdict instead of a gap claim.
- [ ] Limits are disclosed in the same breath as every number: Popoto-only, no competitor arms, judge model deviates from upstream's default, proxy goal-completion definition, pinned corpus SHA.
- [ ] Headline propagation per #511 is either done or explicitly recorded as "no headline change" in the closing comment.

---

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
4. `sme/adapters.py` + `sme/routing.py`: Protocol, three adapters, the family→primitive routing table.
5. `sme/judge_adapter.py`: reuse `judge.py`'s `JudgeProtocol`; vendored upstream prompt + its SHA; deterministic stub judge.
6. `sme/extraction_audit.py` (F3) + its synthetic-case test.
7. `sme/report.py`: family-segregated schema + Markdown writer with the status table first.
8. `sme/runner.py` + `sme/run_sme.py`: CLI, orchestration, cost estimate, `--dry-run`.
9. `sme/compare.py`: paired join, both refusal conditions, discordant cells, gap artifact.
10. `tests/benchmarks/test_sme.py`: the full table in §Test Impact.
11. `gen_benchmark_pages.py` `Spec` entries; `mkdocs build --strict`.
12. Narrow-scope test run + lint/format/mypy-ratchet; PR.

**Lane 2** (gated on Q5)

13. Confirm cost estimate, get the go-ahead, run the five cells on the pinned corpus.
14. Commit artifacts; run `compare.py`; write the gap report with the H1/H2 verdict.
15. Docs pages; headline propagation per #511; close with the verdict comment.

---

## Verification

1. `pytest tests/benchmarks/test_sme.py -q`
2. `python -m tests.benchmarks.sme.run_sme --fixture tests/benchmarks/sme/fixtures/sme_sample.json --db 12 --judge stub --dry-run`
3. Same without `--dry-run`; inspect the artifact for every field in §Success Criteria.
4. `POPOTO_BENCH_DB=9 python -m tests.benchmarks.sme.run_sme --db 12 ...` → must raise `SmeDbError`, not run.
5. `ruff check src/` (unchanged), `black --check src/ tests/`, `scripts/mypy_ratchet.py`.
6. `mkdocs build --strict` → no orphan-artifact warning.
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

**Q5 — Judge model and spend.** Upstream defaults to `gpt-4o`; our `judge.py` pins
`gpt-4o-mini`. I have planned for `gpt-4o-mini` (cheaper, consistent with our Tier 5 harness)
and recording the deviation as a disclosed limit. Confirm, and confirm the go-ahead for
roughly 2–3k generation+judge call pairs for the full five-cell run.

**Q6 — Is the proxy goal-completion definition acceptable?** F2 is "all questions in a
scenario judged correct," which is a scenario-level collapse of F1, not an independent
signal. It is honest but it is not "did the agent finish a task." If a real task-completion
signal is wanted, that is PTR and a different corpus.

**Q7 — Forbidding DB 13.** This plan adds 13 to the forbidden set because the examples
smoke test owns it. Confirm no other lane expects to use 13 for benchmarks.
