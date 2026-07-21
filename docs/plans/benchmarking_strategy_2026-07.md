> **Provenance:** external benchmarking-strategy review received 2026-07-10, adopted as the
> roadmap of record for epic [#456](https://github.com/tomcounsell/popoto/issues/456)
> (SOTA memory system for live agents). Issue map: §1.3 → #454 · §1.4 → #455 · §1.2 → #458 ·
> §2.1 → #459 · §2.2 → #460 · §3.1 → #457 · §3.2 → #462 · §3.3 → #461 · §3.6 → #463 ·
> §2.3 (MDF) / §2.4 (PTR) / §3.4 / §3.5 → future issues tracked in #456.
> Companion methodology review (metric families, MEMTIER anchor) is captured in the
> #453 framing-requirements comment.

# Popoto Benchmarking Strategy — Fair Comparison, Native Metrics, and Research Directions

**Date:** 2026-07-10
**Scope:** Three asks — (1) make apples-to-apples comparison possible, (2) design measurements that reflect Popoto's real goal (subconscious memory, low-latency retrieval), (3) list research directions where competitors lead.
**Grounding:** popoto.io docs (primitives, recipes, retrieval modes), github.com/tomcounsell/popoto, and the benchmark literature surveyed 2026-07-10.

---

## The framing that drives everything below

Popoto is **not primarily a query-answering retriever.** The docs make its actual design explicit: three memory primitives families (decay, confidence, association), a `ContextAssembler` with **three retrieval modes**, and three recipes — `SubconsciousMemory`, `TrajectoryMemory`, `PolicyCache`. The headline feature is *automatic memory injection and extraction around every LLM turn* — memory that "works silently" without the application issuing an explicit query.

The single most important architectural fact for benchmarking:

> Popoto has a **query-blind composite retrieval mode**. With neither `BM25Field` nor `EmbeddingField` on the model, retrieval is ranked by importance / confidence / decay — *not* by relevance to the user's query.

LoCoMo and LongMemEval are **query-answering** benchmarks: given an explicit question, did you retrieve/answer correctly. Popoto's composite (subconscious) mode is *architecturally incapable* of winning them, because it doesn't rank by the query. That is why hybrid inverted on LoCoMo, and it's why chasing the 90% judge-accuracy leaderboards would optimize Popoto *away* from its actual purpose.

So the strategy is not "catch up on LoCoMo." It's: **(a) run the query-answering benchmarks fairly, in the mode that's designed for them, to prove parity; then (b) build the native benchmarks that measure the thing Popoto is actually best at, which no public benchmark measures today.**

---

## Part 1 — Making apples-to-apples comparison possible

### 1.1 Decide the comparison contract first (the fork)

There are two honest comparison targets, and Popoto should serve both explicitly rather than blur them:

- **Retrieval-level** (Popoto's current metric): any-hit Recall@k, MRR, nDCG over retrieved evidence. Comparable to MEMTIER's retrieval numbers and the LoCoMo-original F1 baselines. This is where Popoto is measured *as a retrieval layer.*
- **End-to-end judged-answer** (every vendor leaderboard): retrieve → generate → LLM-judge. Comparable to Hindsight / Mem0 / Zep / Memori / Backboard.

Recommendation: **support both, label both, never cross-compare them.** Today's `benchmarks.md` reports retrieval recall but sits next to systems reporting judged accuracy, which is the core apples-to-oranges error.

### 1.2 Build the missing end-to-end harness (Tier 5)

To be comparable to the leaderboards at all, Popoto needs a generation+judge stage it doesn't currently have. Concretely:

1. Adopt an existing published judge protocol verbatim rather than inventing one. The **Mem0 / GAM evaluation protocol with `gpt-4o-mini` as judge** is the most widely reused; MemPro and others follow it. Reusing it makes numbers directly line up with published tables.
2. Pin the judge model and prompt in-repo (judged accuracy drifts several points across judge models — Hindsight reports different overall scores under Gemini-3 vs OSS-120B for the *same* memory stack).
3. Report retrieval recall **and** judged accuracy side by side for each run. This is itself a differentiating, honest artifact — most vendors only publish the flattering one.

### 1.3 Normalize the dataset variant (immediate, cheap)

The 1986-vs-350 surprise is a version mismatch, not a bug. Lock and document:

- **State the variant in `benchmarks.md`:** Popoto runs the 10-dialogue, 5-category (incl. adversarial), 1986-QA LoCoMo configuration (matches Omni-SimpleMem, arXiv:2604.01007). The common leaderboard variant is 1,540-QA, 4-category, no-adversarial (Dakera, MemPro held-out).
- **Publish a 4-category "leaderboard-parity" slice** alongside the full 5-category run, so a reader can line Popoto up against the no-adversarial boards without you having to re-run.
- **Fix the adversarial handling.** Cat-5 questions test *refusal of unanswerable queries.* Scoring them as any-hit retrieval (and getting 0.3341) means the harness is matching evidence spans instead of testing refusal — that's measuring the wrong thing, not a strength. Either score adversarial with a refusal metric (precision of "no-answer" decisions) or exclude it from the retrieval table and report it separately.

### 1.4 Standardize the retrieval-comparability knobs

Apples-to-apples at the retrieval level requires parity on things that silently move recall:

- **Chunk/turn granularity parity** — the BM25 index and the embedding index must rank the *same units*. If turns are embedded at one granularity and BM25-tokenized at another, RRF fuses non-comparable lists. (This is the leading suspect for the LoCoMo hybrid regression.)
- **k reporting parity** — always report the full R@1 / R@5 / R@10 / MRR / nDCG vector, never a single k. Vendors cherry-pick k; a full curve is unspoofable.
- **Vector-only baseline** — currently missing. Publish lexical-only, vector-only, and hybrid so the fusion's contribution is isolable. Without vector-only you cannot tell whether the LoCoMo regression is the vector arm or the RRF fusion.
- **Zero-tuning declaration** — keep the "measurement-only, no retrieval tuning" flag visible per-run. It's an honesty signal and prevents over-reading an untuned loss.

### 1.5 Publish the reproducibility surface

The credible academic systems (Hindsight, MEMTIER) release eval code; the vendors mostly don't, and Hindsight notes several vendor numbers couldn't be reproduced. Popoto's cheapest credibility win is to **out-transparency the vendors**: committed artifacts (already done), pinned dataset hashes, pinned judge model+prompt, and a one-command `make bench` that regenerates every number in `benchmarks.md`.

---

## Part 2 — New measurements that reflect Popoto's real goal

No public benchmark measures subconscious (query-blind) memory or retrieval latency. These are Popoto's differentiators, so Popoto has to define the benchmarks — ideally publishable as an open harness others adopt (the way LoCoMo itself became a standard). Four proposed benchmark families:

### 2.1 Subconscious Injection Quality (SIQ) — the flagship

Measures the thing composite mode actually does: *without an explicit query,* did the right memory get injected into context at the right turn?

- **Setup:** a multi-turn agent trace where certain later turns have a "should-have-recalled" memory that was established earlier, but the current user message does *not* lexically cue it (coreference, implication, or need-to-know that the user never states). This is exactly where query-blind importance/confidence/decay ranking should beat query-driven retrieval.
- **Metrics:**
  - *Injection Precision / Recall @ budget* — of the memories `ContextAssembler` injected under the `max_items`/`max_tokens` budget, how many were the ground-truth-useful ones.
  - *Anticipation lead time* — how many turns *before* the memory becomes explicitly relevant did it start being injected (rewards proactive recall; a pure query-retriever scores ~0 here by construction).
  - *Budget efficiency* — useful-tokens / injected-tokens, tying quality to the token budget the recipe already enforces.
- **Why it's fair to competitors:** Mem0/Zep/Hindsight can be run in the same harness; they'll do fine when the query cues the memory and poorly when it doesn't — which is the point. This benchmark makes Popoto's design advantage legible.

### 2.2 Retrieval Latency & Throughput (RLT) — the low-latency claim

Popoto's whole substrate premise is RAM-speed Redis/Valkey. That claim is currently unbenchmarked against competitors.

- **Metrics:** p50/p95/p99 retrieval latency per query; assembly latency (retrieve→assemble→inject end to end); throughput (queries/sec) at a fixed corpus size; latency-vs-corpus-size scaling curve (10³ → 10⁷ memories).
- **Comparators:** Mem0 (managed), Zep (Postgres+graph), a vector-DB baseline (e.g. pgvector / Qdrant). Popoto's expected win is p99 and cold-start, because there's no separate vector service round-trip.
- **Report latency *jointly* with recall.** A Pareto frontier (recall vs p99) is the honest artifact: "at equal p99, who recalls more; at equal recall, who's faster." This is where Popoto likely dominates and where no leaderboard currently looks.

### 2.3 Memory Dynamics Fidelity (MDF) — decay, confidence, association

Popoto's primitives (`DecayingSortedField`, `CyclicDecayField`, `ConfidenceField`, `CoOccurrenceField`, `PredictionLedger`, `ObservationProtocol`) encode a *temporal, self-updating* memory model. Static QA benchmarks can't see any of it.

- **Contradiction/update handling:** establish fact A, later contradict with A′; does confidence on A weaken and A′ strengthen (via the `acted/dismissed/contradicted` outcome loop)? Measure post-update retrieval correctness over time. (LongMemEval's "knowledge-update" category is the closest existing proxy — worth reporting that slice specifically.)
- **Decay calibration:** does relevance decay track actual future usefulness? Measure whether decayed-out memories were in fact not needed (true-negative decay) vs prematurely dropped (regret).
- **Association recall:** with `CoOccurrenceField`, does retrieving X surface associated Y that shares no lexical overlap? Measures the associative path competitors' flat stores lack.

### 2.4 Procedural / Trajectory Recall (PTR) — "what worked last time"

`TrajectoryMemory` is fingerprint-keyed procedural memory — cluster completed task trajectories, recall the successful one on a similar new task. This is an agentic capability, not a conversational-QA one, and it's arguably Popoto's most defensible niche.

- **Metric:** on a new task, does recalling the nearest successful past trajectory improve task success rate / reduce steps-to-completion vs no-memory and vs naive-RAG baselines?
- **This maps to real agent value** (Yudame's multi-agent orchestration is exactly this use case) and has essentially no competition in the LoCoMo/LongMemEval framing.

> **Strategic note:** SIQ + RLT are the two to build first and publish as an open harness. They're where Popoto wins, they're reproducible, and "the benchmark that measures proactive low-latency memory" is a category-defining position — the same move LoCoMo's authors made.

---

## Part 3 — Research directions (where competitors are ahead)

Ordered by leverage. Each ties to a specific gap the survey surfaced.

### 3.1 Fix hybrid fusion (highest-leverage, near-term)

The LoCoMo regression (hybrid 0.1667 < lexical 0.2986 at R@1) is a real defect in the fusion, and it's *the* thing making Popoto look weak on a benchmark it should at least tie on. Directions:

- **Weighted / learned fusion instead of vanilla RRF.** Unweighted RRF gives a weak-but-confident vector arm equal say; on coreference-heavy multi-session dialogue the dense arm retrieves topically-similar-but-wrong turns and displaces correct BM25 hits. Move to weighted RRF or a small learned reranker over the two arms' scores.
- **Query-adaptive arm selection.** Detect when a query is name/date/token-specific (BM25 wins) vs paraphrastic/semantic (vector wins) and weight accordingly. LongMemEval favored hybrid; LoCoMo favored lexical — the system should learn that split rather than fix one blend.
- **Embedding domain fit.** Dense retrieval underperforms on casual multi-session dialogue; evaluate dialogue/coreference-tuned embeddings, and test embedding at the right granularity.

### 3.2 Structured/graph memory (where Zep, Hindsight, ByteRover lead)

The top judged-accuracy systems win multi-hop via **entity-graph traversal** (Zep), **four logical networks + retain-recall-reflect** (Hindsight), and **context-tree architectures** (ByteRover). Popoto has `RelationshipField` and `CoOccurrenceField` but no graph-traversal retrieval. Multi-hop is where flat retrieval structurally loses. Research a lightweight graph/traversal layer over the existing association primitives — this is the biggest *capability* gap, not just a tuning gap.

**Status (#462, PR #483):** `CoOccurrenceField.propagate()`-based BFS graph
traversal was already wired into `ContextAssembler` (both the composite and
hybrid/lexical pull paths) prior to this issue being scoped. PR #483 closes
the remaining gap: a new `popoto.recipes.graph_traversal` module extends
that graph arm with (1) `RelationshipField` edge expansion — 1-2 hop
forward/reverse traversal of self-referential `Relationship` fields, bounded
fan-out via `SRANDMEMBER` — and (2) confidence/decay-modulated hop
admission, where a candidate's own `ConfidenceField`/decaying-field state
scales its survival weight. Opt-in via `ContextAssembler(...,
graph_traversal_relationship_fields=[...])`; see
[Graph Traversal](../features/agent-memory.md#graph-traversal) in the
agent-memory feature doc for the full mechanism and API. **The LoCoMo
multi-hop slice + association-recall evaluation this section calls for is
still outstanding** — PR #483 implements and unit-tests the traversal
mechanism only; the judged-accuracy/recall lift is unmeasured and tracked as
a follow-up under epic #456 Track B, matching how §3.3's extraction-provider
evaluation gap is tracked.

### 3.3 LLM-based extraction & structured memory writes

Competitors' big lever is *structuring unstructured chat into semantic memory* on write (Memori explicitly attributes its lead to this). Popoto's default `extract_memories()` is still a **sentence-splitting heuristic** for backward compatibility, but as of #461/PR #481 it is now pluggable: `SubconsciousMemory` accepts an `extraction_provider` (see [LLM Memory Extraction](../features/llm-memory-extraction.md)) that can return entities, typed facts, and importance/confidence scoring on write, feeding `CoOccurrenceField` and `ConfidenceField` directly. The built-in `ClaudeExtractionProvider` covers the LLM-extraction mechanism; **judged-accuracy/recall evaluation of it vs. the heuristic default is still outstanding**, tracked under epic #456 Track B, since the existing benchmark harness has no extraction-provider seam yet. That evaluation -- not the mechanism -- is what remains to confirm the SIQ/MDF lift this section predicted.

### 3.4 Temporal reasoning (hardest category industry-wide)

Temporal is the acknowledged weak category across all systems (humans ~92 F1 vs LLMs ~20). Popoto's decay primitives give it an unusual angle — it already models time natively. Research whether `DecayingSortedField` + event-time metadata can support explicit before/after / duration reasoning, not just recency ranking. Potential differentiator rather than catch-up.

### 3.5 Reflection / consolidation (retain-recall-**reflect**)

Hindsight and RGMem gain from a consolidation pass (summaries, reflection, "memory evolution"). Popoto has the outcome loop (`ObservationProtocol`) and a compaction concept in the roadmap but no evaluated consolidation stage. Research an offline consolidation/compaction pass and measure it via MDF (does consolidated memory retrieve better and cost fewer tokens).

### 3.6 Adversarial / refusal handling

Popoto currently mis-scores adversarial as retrieval. Beyond the harness fix (1.3), the *capability* — knowing when to return nothing — is a real research item and a category most systems drop entirely. Confidence-gated retrieval (return nothing below a `ConfidenceField` threshold) is a natural fit for the existing primitives and would let Popoto *report* an adversarial number honestly when others can't.

---

## Suggested sequencing

| Phase | Work | Payoff |
|---|---|---|
| **Now (days)** | 1.3 variant/adversarial doc fix; 1.4 vector-only + full-k baselines | Stops the apples-to-oranges misread; unblocks honest `benchmarks.md` |
| **Near (weeks)** | 3.1 weighted/query-adaptive fusion; 1.2 end-to-end judge harness (reuse Mem0/GAM protocol) | Fixes the visible regression; enables leaderboard-comparable numbers |
| **Mid (weeks–months)** | 2.1 SIQ + 2.2 RLT open harness; 3.3 LLM extraction | Establishes Popoto's *own* winning category; lifts all metrics |
| **Longer (months)** | 3.2 graph traversal; 2.3 MDF + 2.4 PTR; 3.4/3.5/3.6 | Closes the multi-hop capability gap; differentiates on dynamics & procedure |

---

## Sources

- Popoto docs — popoto.io (home/features, retrieval modes, SubconsciousMemory / TrajectoryMemory / PolicyCache recipes, primitives list, memory roadmap); github.com/tomcounsell/popoto.
- MEMTIER — arXiv:2605.03675 (retrieval-vs-comprehension distinction; LoCoMo retrieval-level F1/R@1).
- Hindsight — arXiv:2512.12818 (judged-accuracy leaderboard incl. Zep/Mem0/Memobase/LangMem/Backboard; judge-model sensitivity; released eval code).
- Omni-SimpleMem — arXiv:2604.01007 (1986-QA / 5-category LoCoMo variant definition).
- Mem0 — arXiv:2504.19413 (GAM/judge protocol; adversarial category note).
- MemPro — arXiv:2606.00619 (1,540-QA held-out protocol; gpt-4o-mini judge).
- RGMem — arXiv:2510.16392; Memori — memorilabs.ai; ByteRover — byterover.dev; Dakera — dakera.ai/benchmark.
- LoCoMo original — Maharana et al. 2024, arXiv:2402.17753; LongMemEval — Wu et al. 2024, arXiv:2410.10813.

*Vendor caveat: ByteRover/Backboard/Dakera/Memori figures are self-published; per Hindsight, several are not independently reproduced. Weight arXiv-with-code sources (Hindsight, MEMTIER) more heavily.*
