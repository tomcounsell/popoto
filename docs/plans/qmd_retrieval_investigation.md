---
status: Complete
type: investigation
appetite: Small
owner: Valor
created: 2026-05-19
updated: 2026-05-25
tracking: https://github.com/tomcounsell/popoto/issues/397
labels: agent-memory, question
last_comment_id:
---

# Investigation: QMD-Style Retrieval Pipeline on Valkey Substrate

## Purpose

This is a design investigation, not a feature plan. The goal is a go/no-go recommendation
per pipeline stage based on evidence — code inspection, spikes, benchmarks, and collision
math. The output of this investigation feeds the v2 memory retrieval roadmap; it does not
directly produce shippable code (though spikes may become the foundation for follow-up plans).

## Background

[QMD](https://github.com/tobi/qmd) by Tobi Lütke implements a four-stage retrieval pipeline
over SQLite (FTS5 + sqlite-vec):

```
Query → Expansion (LLM rewrites) →
  Parallel Retrievers (BM25 lexical + ANN vector) →
  RRF Fusion (dependency-free reciprocal rank fusion) →
  LLM Rerank (top-N)
  → Final ranked results
```

QMD claims meaningful recall/precision gains on vague queries versus single-pipeline scoring,
with position-aware blend weights (75/25 lexical/semantic at rank 1, 40/60 at tail).

Popoto's `ContextAssembler` currently retrieves via a single `CompositeScoreQuery`
(decay × importance scoring) with no lexical/semantic fusion and no reranking. Issue #395
adds BM25 + vector + graph via RRF as the ContextAssembler default — this investigation
examines whether additional QMD stages (query expansion, position-aware weighting,
content-hash docids, LLM rerank) are worth building on Popoto's Valkey-native substrate.

**Constraint:** No Redis modules. All features must work on both Redis and Valkey per
`feedback_valkey_compatibility.md`. RediSearch is excluded wholesale.

---

## Findings Summary

| Stage | Description | Verdict | Effort | Dependency |
|-------|-------------|---------|--------|------------|
| 1 — RRF fusion | BM25 + vector + graph via RRF | **SHIPPED** (#395, commit 737cea8, 2026-05-21) | — | None |
| 2 — Position-aware weights | Blend lexical/semantic by rank | **Spike when #394 lands** | 1 day + benchmark | #394 (benchmark harness) |
| 3 — Content-hash docids | 6-char dedupe-friendly IDs | **NO-GO** (collision risk) | 0.5 day (math only) | None |
| 4 — BM25 on Valkey | Substrate validation | **COMPLETE** — BM25Field ships, Valkey-native | 0 days | None |
| 4 — Vector / ANN on Valkey | Corpus sizing / ANN threshold | **Green ≤50K; spike >50K** | 0 days analysis | None |
| 5 — LLM rerank | Haiku rerank over top-N | **Defer; opt-in only** | 2 days | Stage 1 |
| 6 — Query expansion | LLM rewrites before retrieval | **Defer** | 1 day | Stages 1–5 |

---

## Stage 1 — RRF Fusion

**Status:** SHIPPED in #395 / commit `737cea8` on 2026-05-21 (`context_assembler_hybrid_default.md`).

**Finding (from code):** `QueryBuilder.fuse()` (`src/popoto/models/query.py:894–989`) already
ships the RRF primitive with the standard formula `score(d) = Σ 1 / (k + rank_i)`, k=60
default, matching Cormack et al. exactly. `BM25Field` ships full inverted-index BM25 on
Valkey sorted sets (Lua scripts, no Redis modules). `EmbeddingField` ships brute-force
cosine via in-memory numpy matrices. `CoOccurrenceField.propagate()` already returns
`{redis_key: weight}` dicts compatible with `fuse()`'s `**ranked_lists` parameter.

The wiring inside `ContextAssembler._pull_path()` landed with #395.

**Verdict: SHIPPED via #395 / commit `737cea8`.** No further investigation needed here.

---

## Stage 2 — Position-Aware Blend Weights

**Description:** QMD uses 75/25 lexical/semantic blend at rank 1 shifting to 40/60 at the
tail. This is a weighted-RRF variant that biases signal mix based on rank position.

**Finding:** The current `fuse()` implementation uses uniform list weights (each list
contributes equally at every rank). Adding position-aware weighting requires:

1. A `position_weights` dict argument to `fuse()`: `{"keyword": [0.75, 0.6, 0.5, ...], "vector": [0.25, 0.4, 0.5, ...]}`.
2. Modified formula: `score(d) = Σ w_i(rank) / (k + rank_i)` instead of uniform `Σ 1/(k + rank_i)`.
3. No storage changes whatsoever — pure Python computation.

**Questions answered:**

- *Is it feasible?* Yes. Pure Python change to `fuse()`. Dependency-free.
- *Is the benefit measurable without QMD's exact weights?* Unknown. Tobi's 75/25 values
  are tuned for personal-notes queries (short, keyword-dense). Agent memory queries via
  `query_cues` dicts are more structured; lexical signals may matter less at rank 1.
- *What is the correct approach?* Implement position-aware RRF as an opt-in mode, then
  A/B on the LongMemEval-S benchmark harness (#394). Ship only if R@5 delta ≥ 1%.

**Spike plan:**
1. Add `position_weights` dict to `fuse()` — pure Python, no storage changes.
2. Run #394 benchmark harness: equal-weight RRF vs. position-aware (75/25→40/60).
3. If delta < 1% R@5: defer indefinitely.
4. If delta ≥ 1% R@5: plan as a follow-up to #395.

**Estimated effort:** 1 day (formula change + 1 benchmark run).

**Dependency:** Stage 1 (#395) shipped 2026-05-21 — no longer a blocker. The real
prerequisite is the LongMemEval-S / LoCoMo benchmark harness in #394 (OPEN); without it,
the "R@5 delta ≥ 1%" ship criterion has no instrument.

**Recommendation: Spike unblocked as of 2026-05-21; queue when #394 benchmark harness lands.**

---

## Stage 3 — Content-Hash Docids (6-char)

**Description:** QMD uses 6-character content hashes as stable, dedupe-friendly document
IDs. Popoto currently uses `AutoKeyField` which generates 32-character UUID4 hex strings
(128-bit random namespace).

**Collision math (verified with Python, birthday-problem approximation):**

For 6-char hex: keyspace = 16^6 = 16,777,216.

P(collision) ≈ 1 − e^(−n²/(2m))

| Corpus size (n) | P(collision) |
|-----------------|--------------|
| 1,000           | 2.94%        |
| 10,000          | 94.9%        |
| 50,000          | ~100%        |
| 100,000+        | 100%         |

**6-char hex is unsafe at 10K+ memories.** Tobi's use case is a personal notes corpus
(likely < 5K documents). For agent memory systems that accumulate across sessions,
6-char hex IDs are not viable.

**For comparison — UUID4 (32-char hex, 128-bit):**

P(collision) at N=1,000,000 is effectively 0 (~1.47e-24).

**Additional findings:**

- `AutoKeyField` already uses UUID4 by default (32-char hex), which provides collision
  safety up to planetary-scale deployments.
- Popoto's Redis keys encode model class + field values: `ClassName:uuid_value`. The
  full key is the stable identity; there is no separate "docid" concept to replace.
- Deduplication at write time is a separate concern from ID generation. If content-hash
  deduplication is wanted, the right pattern is a side-effect field that checks whether
  a hash already exists in a ZSET index before saving — not replacing the primary key.
- Migration cost for 6-char content-hash primary keys would be extreme: rewriting every
  redis_key (RENAME in Redis), rebuilding every ZSET index, rebuilding all inverted BM25
  indexes, all cross-reference sets. Not justified by any measurable benefit.

**Recommendation: NO-GO for content-hash docids.** UUID4 AutoKeyField is already safe
at all realistic Popoto corpus sizes. If deduplication is wanted, implement a separate
`ContentDedupeField` that maintains a hash → redis_key index. Track as a new issue only
if dedup requests appear from users. **No code changes from this stage.**

---

## Stage 4 — BM25 and Vector Storage on Valkey Substrate

### 4a — BM25 / Lexical

**Finding (from code):** `BM25Field` (`src/popoto/fields/bm25_field.py`) ships a complete
BM25(k1=1.2, b=0.75) implementation using only Valkey-native sorted sets and strings:

- Inverted index: `$BM25:{Class}:{field}:inv:{term}` — ZSET `{doc_key: tf}`
- Forward index: `$BM25:{Class}:{field}:tf:{doc_key}` — ZSET `{term: tf}`
- Document frequency: `$BM25:{Class}:{field}:df` — ZSET `{term: df}`
- Document lengths: `$BM25:{Class}:{field}:dl` — ZSET `{doc_key: length}`
- Corpus stats: `n`, `avgdl` — STRING keys

Scoring is done entirely via Lua scripts (BM25_SAVE_LUA, BM25_SEARCH_LUA, BM25_DELETE_LUA).
ZMSCORE (Redis 6.2+, Valkey-compatible) is used for batch IDF lookups with single-ZSCORE
fallback. No Redis modules required.

**Write cost per document:** 1 Lua eval touching O(unique_terms) ZSET entries.
**Search cost:** 1 Lua eval scanning the inverted index for each query term.
For typical agent memories (50–200 tokens), inverted index cardinality per term is modest.

**BM25 verdict: Fully implemented, Valkey-native, production-ready. No gaps.**

### 4b — Vector / ANN

**Finding (from code):** `EmbeddingField` (`src/popoto/fields/embedding_field.py`) stores
embeddings as `.npy` files on the local filesystem (not in Valkey). It maintains an
in-memory numpy matrix cache for fast brute-force cosine similarity. Key design notes:

- Embeddings stored as `~/.popoto/content/.embeddings/{ModelName}/{sha256_hash}.npy`
- An `_index.json` sidecar maps hash filenames back to Redis keys
- `load_embeddings()` loads all `.npy` files into a pre-normalized numpy matrix cache
- `semantic_search()` computes: `similarities = matrix @ query_vec` (dot product on unit vectors)
- Cache is invalidated on save/delete within the same process
- numpy is optional (`pip install popoto[embeddings]`)

**This is not stored in Valkey** — it is filesystem + in-process numpy. This is a
significant architectural distinction from BM25Field. It means:

1. Embeddings are not shared across processes or hosts without a shared filesystem.
2. Multi-process agents (separate pods/containers) cannot share the cosine similarity cache.
3. There is no Redis/Valkey key for embeddings — only the dimension count is stored in the hash.

**Brute-force cosine latency estimates (384-dim float32, numpy BLAS).** The numbers below
are **dot-product compute only**. End-to-end `semantic_search()` p95 also includes query
embedding generation (5–15ms warm OpenAI/local call; 50–300ms cold), `_index.json` resolution,
and Valkey hydration of result instances. The compute budget below is necessary but not
sufficient; treat the "well within 50ms p95" conclusion as conditional on a hot embedding-model
client and warm result-hydration paths.

| Corpus size | Conservative (5 GFLOP/s) | Optimistic (20 GFLOP/s) |
|-------------|--------------------------|--------------------------|
| 1,000       | 0.15 ms                  | 0.04 ms                  |
| 5,000       | 0.77 ms                  | 0.19 ms                  |
| 10,000      | 1.54 ms                  | 0.38 ms                  |
| 50,000      | 7.68 ms                  | 1.92 ms                  |
| 100,000     | 15.36 ms                 | 3.84 ms                  |

Brute-force cosine is well within the 50ms p95 budget for all realistic single-agent
corpus sizes (≤ 50K memories). At 100K memories with numpy BLAS, p95 likely stays under
20ms — still within budget.

**In-memory cache overhead (384-dim float32):**

| Corpus size | Memory (float32 matrix) |
|-------------|-------------------------|
| 10,000      | 15.4 MB                 |
| 50,000      | 76.8 MB                 |
| 100,000     | 153.6 MB                |

For a single-agent process with ≤ 50K memories, the in-memory cache overhead (~77 MB) is
acceptable for a server process but noteworthy for edge deployments.

**ANN threshold and path forward:**

At the 50K–100K boundary, disk I/O to load the full `.npy` matrix at cache-miss becomes
the bottleneck, not the dot product itself (O(N*D) disk read vs O(N*D) computation).
Cache-miss load time for 100K × 384 float32 = 153.6 MB disk read; at typical SSD speeds
(500 MB/s) = ~300ms, well above the 50ms p95 budget on a cache miss.

Mitigation: `load_embeddings()` is already cached in-process — cache misses only occur
on process restart or after saves/deletes. For steady-state operation (hot cache), brute-
force cosine is fine up to 100K+ memories.

**Multi-process / distributed gap (correctness concern today, not a v3 sizing issue):**
`EmbeddingField` invalidates its in-memory numpy cache only within the current process.
A second worker (gunicorn worker, second container, second pod) holding the same corpus
will silently serve a stale matrix after writes from another process — there is no
cross-process invalidation signal. Because Popoto is server-side ORM where multi-worker
deployment is the default expectation (not an edge case), this is a deployment-time
silent-staleness bug, not a future scaling tradeoff. Single-process deployments are safe;
multi-worker / multi-container setups need either (a) shared NFS for
`~/.popoto/content/.embeddings/` plus a cache-bust signal, or (b) the out-of-process ANN
path described below. File a v2 issue to track shared-storage support.

**Valkey native vector roadmap:** Valkey has no native vector index support as of v8.x.
Redis Stack's `FT.SEARCH` with vector fields is excluded by the no-modules constraint.
The path when ANN is eventually needed at >100K corpus is an out-of-process ANN index
(e.g., FAISS, hnswlib, or usearch) with Popoto storing IDs and the ANN index storing
vectors. This is a follow-up issue, not a current blocker.

**Vector verdict:**
- Brute-force cosine is sufficient up to ~50K memories **on hot cache, single-process only**.
- File I/O overhead is only a concern on cold starts at >50K memories.
- **Multi-worker / multi-container deployments will silently serve stale embeddings** —
  file a v2 issue for shared-storage + cache-invalidation support before recommending
  Popoto's vector path for production multi-worker setups.
- No action needed now for single-process. If corpus sizes routinely exceed 50K, file an
  ANN tier issue.

---

## Stage 5 — LLM Rerank

**Description:** QMD adds an LLM (Haiku-class) rerank pass over the top-N RRF results.

**ContextAssembler current pipeline latency (code-derived estimate):**

The `assemble()` method does, in order:
1. ExistenceFilter pre-check: 1–2 Redis round-trips (BLOOM lookup) → ~1–3ms
2. `composite_score()` pull path: ZUNIONSTORE + ZREVRANGE → ~2–5ms per run
3. CoOccurrence propagation + re-run: 2–4 more Redis round-trips → ~3–8ms
4. Push path `composite_score()`: 1 more ZUNIONSTORE → ~1–3ms
5. Post-effects pipeline (RPUSH, ConfidenceField updates): ~1–2ms
6. Format output: ~0.1ms

**Estimated baseline p95 (no rerank):** ~10–25ms for a corpus of 1K–20K memories.
This estimate assumes a local Valkey instance. Network latency to a remote Valkey would
add ~1–5ms per round-trip.

With Stage 1 (#395) hybrid path, add:
- BM25 search Lua eval: ~2–5ms
- EmbeddingField cosine (hot cache, 10K corpus): ~1–2ms
- Extra fuse() step (Python-only): ~0.1ms
- **Revised baseline p95 with hybrid: ~15–35ms**

**Haiku rerank cost estimate:**
- API call latency to Claude Haiku: 300–800ms p50, 1–2s p95 (network + model)
- Token cost: 10 records × ~200 tokens/record + 50-token query = ~2050 input tokens
- At current Haiku pricing (~$0.25/M input), $0.0005 per rerank call

**Finding:** LLM rerank adds 300ms–2s p95 latency on top of a 15–35ms baseline.
This is a 10–100x latency multiplication. It is not viable for inline subconscious injection
(target: p95 < 100ms end-to-end).

**Where LLM rerank could be viable:**
- Offline/batch session priming (pre-retrieval before a conversation, not during)
- User-initiated long-horizon recall (user explicitly asks "what do you remember about X?")
  where a 500ms–1s wait is acceptable
- Asynchronous background re-ranking that populates a "recently primed" sorted set

**Benefit estimate without benchmarks:**
QMD's rerank gain comes from intent-disambiguation on ambiguous free-text queries. Popoto's
`query_cues` are structured dicts (`{"topic": "deployment", "agent_id": "agent-1"}`), not
free-form text. LLM rerank benefit for structured-cue retrieval is likely small — the main
value would be on semantic query paths where `query_text` is ambiguous.

**Recommendation:**
- **NO-GO for inline retrieval.** Latency is prohibitive.
- **Defer for batch/offline priming.** Implement as `assemble(rerank=True)` opt-in
  only after Stage 1 benchmarks show where precision gaps actually are.
- Do NOT implement until #394 benchmark harness shows measurable R@5 gap that isn't
  closed by BM25 + vector RRF alone.

**Flip conditions (re-open this stage when any of the following is observed):**
- #394 benchmark shows R@5 < 0.6 on long-context queries even with hybrid + position-aware RRF.
- Users explicitly request a long-horizon recall mode where 500ms–1s wait is acceptable.
- An async/background rerank path becomes viable (write into a "recently primed" sorted set
  consumed on the next `assemble()` call); evaluate when #396 session memory lifecycle lands.

---

## Stage 6 — Query Expansion

**Description:** QMD uses LLM rewrites to generate query variants, expanding recall for
ambiguous or terse queries.

**Finding:** Popoto's `query_cues` are already structured dicts, not free-form text.
Query expansion makes the most sense for free-text `semantic_search()` queries where the
user's intent is ambiguous. For structured cue retrieval, expansion would require
generating alternative values for each cue field — a less well-defined problem.

**Cost:** An LLM call before retrieval adds 200–500ms minimum, pushing total pipeline
latency above 500ms for inline injection. This is worse than Stage 5's rerank problem.

**Caching:** Per-query-text expansion results could be cached (e.g., in a ZSET with TTL),
but cache effectiveness depends on query repetition rates which are unknown.

**Recommendation: Defer indefinitely.** Lowest priority of all stages. The structured-cue
nature of Popoto's retrieval workload means query expansion provides the least benefit
here vs. a free-text retrieval system like QMD.

**Flip conditions (re-open this stage when any of the following is observed):**
- `semantic_search()` over free-text `query_text` becomes the primary retrieval path
  (rather than structured `query_cues` dicts).
- #394 benchmark shows R@5 < 0.5 on ambiguous-query workloads after hybrid retrieval lands.
- A cache-hit-rate measurement on `query_text` shows >40% repetition (which would make a
  TTL-cached expansion cost-effective).

---

## Cross-Cutting Findings

### What Popoto already has that QMD doesn't

| Feature | Popoto | QMD |
|---------|--------|-----|
| BM25 inverted index | Yes (`BM25Field`) | Yes (SQLite FTS5) |
| Vector similarity | Yes (`EmbeddingField`) | Yes (sqlite-vec) |
| RRF fusion primitive | Yes (`QueryBuilder.fuse()`) | Yes (built-in) |
| Decay × importance scoring | Yes (`CompositeScoreQuery`) | No |
| Graph propagation | Yes (`CoOccurrenceField`) | No |
| Proactive surfacing | Yes (`CyclicDecayField`) | No |
| Session memory lifecycle | Planned (#396) | No |
| Metacognitive quality signals | Yes (`RetrievalQuality`) | No |

Popoto's memory system is significantly richer than QMD in the post-retrieval dimension.
The gap is on the pre-retrieval side (hybrid default wiring), which #395 closes.

### ContextAssembler latency budget

| Component | Estimated latency | Budget impact |
|-----------|-------------------|---------------|
| Pull path (composite, no hybrid) | 6–15ms | Baseline |
| Pull path (hybrid BM25+vec+RRF) | 15–30ms | +9–15ms |
| Push path | 3–8ms | Always present |
| Post-effects pipeline | 1–3ms | Always present |
| **Total inline p95 (hybrid)** | **~20–45ms** | Well under 100ms |
| LLM rerank (Haiku, top-10) | +300–2000ms | **Exceeds budget** |
| Query expansion (LLM, 3 variants) | +200–500ms | **Exceeds budget** |

The hybrid retrieval from #395 fits comfortably within the 100ms p95 target for inline
injection. Stages 5 and 6 (LLM calls) do not.

---

## Working Notes

### Valkey substrate constraints (from `feedback_valkey_compatibility.md`)

- No Redis modules: BF.*, CMS.*, FT.* (RediSearch), etc. are all excluded.
- All features must work on plain Redis and plain Valkey.
- `BM25Field` uses only sorted sets and strings — compliant.
- `EmbeddingField` stores vectors as `.npy` files on filesystem, not in Valkey — compliant
  (and has an architectural tradeoff: no cross-process sharing without shared FS).
- ANN (approximate nearest neighbor) would require either brute-force scan or an
  out-of-process service. Neither is a Redis/Valkey module dependency.

### Corpus size estimates for Popoto deployments

Typical agent memory workloads:
- Single-session demo: ~100–500 memories
- Active agent (weeks of sessions): ~5K–20K memories
- Long-running multi-agent system: ~50K–200K memories

Retrieval budget (target): p95 < 100ms end-to-end for inline subconscious injection.

### QMD architecture notes

```
- Storage: SQLite FTS5 (lexical) + sqlite-vec (vectors)
- Position-aware blend weights: 75/25 lexical/semantic at rank 1, 40/60 at tail
- Docids: 6-char content hashes (stable, dedupe-friendly)
```

RRF itself is dependency-free and directly transferable. The SQLite storage layer does not
transfer — Popoto's Valkey primitives cover the equivalent role. QMD's docid scheme is
safe only for small (< 5K) corpora and is explicitly not suitable for Popoto deployments.

---

## Deliverables Status

- [x] Stage 1: SHIPPED (#395 / commit 737cea8, 2026-05-21)
- [x] Stage 2: spike plan documented; unblocked since #395 shipped, queue when #394 lands
- [x] Stage 3: collision math computed (Python, birthday problem), NO-GO finding documented
- [x] Stage 4 (BM25): code inspection complete, Valkey-native, no gaps
- [x] Stage 4 (vector): latency + memory estimates computed, green ≤ 50K, spike plan for > 50K
- [x] Stage 5: latency analysis complete, NO-GO for inline, defer for offline opt-in
- [x] Stage 6: defer decision documented

## References

- QMD repo: https://github.com/tobi/qmd
- PR #366: v1 memory system (shipped 2026-04-21)
- Issue #394: benchmark harness (LongMemEval-S + LoCoMo)
- Issue #395: ContextAssembler hybrid default (stage 1 — RRF)
- `src/popoto/recipes/context_assembler.py` — current scoring pipeline
- `src/popoto/models/query.py:894` — `QueryBuilder.fuse()` — RRF primitive
- `src/popoto/fields/bm25_field.py` — BM25 inverted index on Valkey sorted sets
- `src/popoto/fields/embedding_field.py` — vector similarity via numpy + filesystem
- `feedback_valkey_compatibility.md` — no Redis modules constraint
- `project_ai_memory_system.md` — current memory system state
