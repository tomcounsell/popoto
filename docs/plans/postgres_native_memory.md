---
status: Planning
type: feature
appetite: Large
owner: tomcounsell
created: 2026-10-03
tracking: https://github.com/tomcounsell/popoto/issues/755
last_comment_id: 5964094115
revision_applied: true
revision_applied_at: 2026-10-03
---

# Postgres-native agent memory (#755)

## Problem

Valor runs one memory model, `Memory` in `ai/models/memory.py`, on popoto's Redis memory fields. Every primitive in it is a Redis data structure maintained by Lua: a decay ZSET per project, a confidence companion hash, a BM25 inverted index in hashes and ZSETs, a vector store, a bloom filter, and access-tracker side keys. Each recall assembles a ranking out of temp ZSETs. Two consequences follow.

- Retrieval cannot be expressed as one query, nor inspected, joined, or backed up as ordinary data. Signals are fused across separate Redis structures in Python and Lua.
- The #631 POC showed that these semantics do run on Postgres. It ran them on a generic `popoto_record(key, field bytea, value bytea)` layout that copies Redis's shape, which pays a PL/pgSQL msgpack decode for every row on every ranking query. Measured costs: decayed rank with confidence ran 5.4× Redis, writes 4.9×, index swaps 7.0×. The same POC showed typed SQL is faster than Redis wherever no decode is needed: plain decay rank 0.2×, validity-gated rank 0.3×.

The maintainer has decided (2026-10-03, #755) that Postgres is the substrate for agent memory, built Postgres-native, and that Redis memory is frozen.

**Current behavior:**
- Valor's `Memory` lives in Redis DB 0 on this machine.
- Ranking is Lua over ZSETs, and fusion is Python over several round trips.
- `poc/backend-seam` carries a 46-method protocol plus a Redis-path refactor across 13 `src/` modules. Both exist to make memory *dual-backend*, a goal now ruled out.

**Desired outcome:**
- A Postgres-native memory model, `popoto.pg`. A popoto `Model` class body compiles to one typed table per model with pgvector, a BM25 postings table, and typed decay/confidence/access columns.
- Recall (scope filter, BM25, vector, decay×confidence ranking, and RRF fusion) is a single SQL statement.
- Store, retrieve, validate and prune all happen inside normal calls, with no cron and no manual curation.
- Valor's `Memory` can switch by changing its base class and import line, keeping the same declarative fields. Its schema is concrete enough for #756 to migrate into.

## Freshness Check

**Baseline commit:** `57c29ebf` (`origin/main`). The POC branch was read at `origin/poc/backend-seam` = `efda14a3`.
**Issue filed at:** 2026-10-03T01:26:23Z
**Disposition:** Unchanged

**File:line references re-verified:** the issue cites no `main` file:line. Its POC claims were re-read on the branch:
- The 46-method `Backend` protocol is at `backends/__init__.py:110`. It still holds.
- The `PostgresBackend` generic `bytea` tables are in `SCHEMA_DDL`, `backends/postgres.py:369-950`. Still holds.
- The "3–7× slower on writes and confidence-modulated ranking" figures come from POC report §4. They still hold: `save_record` 4.9×, `swap_index` 7.0×, `decayed_rank` with confidence 5.4×, all arms 6.2×.

**Cited sibling issues/PRs re-checked:**
- #631: open. The POC is complete, and its last three PRs merged into the branch at 2026-10-03T05:31+07: #752 WS3d, #754 family-H mark strip, #753 WS4 report. #754 closes register item TD-16 from the report.
- #756 (migration tool): open, companion, out of scope.
- #750 (B1 crossing-chain deadlock): merged into the branch. TD-2 (cross-operation deadlock) remains open as a register item.
- #747 (phase-checker vacuity): open. It affects only the Redis Lua path, which this plan does not touch.
- #630 (recipes through the field layer): closed.
- #739 (selector footgun): merged into the branch as a test-time pin. The production rule is answered in §Solution.

**Commits on main since the issue was filed:** none. `origin/main` was last committed 2026-10-02T14:57Z. Two `main` commits are not on the branch: `dbf270d4` (#730, question queue) and `57c29ebf` (#729, bench). Neither touches the memory fields this plan ports. #730 adds `recipes/question_queue.py`, another Redis-only recipe. Valor does not use it (see Spike Results).

**Active plans in `docs/plans/` overlapping this area:** `sdlc-631.md` (the POC plan) is the predecessor and is now complete. No other plan touches the Postgres storage layout.

## Prior Art

- **#631 / `poc/backend-seam`**: the POC, 22 PRs (#732–#754) on a dedicated branch.
  - It proved the seam: 644 Postgres-leg conformance ids passed, 0 failed.
  - It also proved the generic `bytea` layout is the wrong storage. It measured 3–7× costs wherever SQL has to decode msgpack.
  - It recommended per-model typed tables (report §5, §9 Q1), which the maintainer then adopted.
  - This plan reuses its harness ideas and its findings. Its Redis refactor is not reused (see §Solution, decision D1).
- **PR #731**: the POC plan (`docs/plans/sdlc-631.md`). It holds the enumerated protocol and the three findings: the unit of work is public API, `jsonb` cannot hold msgpack, and tooling has to be hand-maintained.
- **PRs #738 / #739**: the `POSTGRES_URL` auto-selection footgun. Report §6 recommends that selection be explicit. This plan goes one step further: the backend binding belongs to the model class, not to the process.
- **PR #750**: crossing supersede chains deadlocked under per-key advisory locks. It was fixed with a prefix lock. This is the lesson behind the lock-ordering rule in §Race Conditions.
- **#457 / PR #479** (fusion), **#496 / #499** (BM25 retrieval flake): the lexical arm and the RRF fusion were tuned against popoto's own tokenizer and BM25 math. That is why this plan keeps exact BM25 rather than `ts_rank` (decision D4).
- **#408–#416** (June audit), **#417** (ConfidenceField capped Bayesian): these set the confidence semantics the typed columns must reproduce.

## Research

**Queries used:**
- pg_search ParadeDB BM25 vs tsvector ts_rank, Postgres 18 Homebrew availability
- pgvector 0.8 HNSW iterative index scan, filtered queries
- psycopg 3 ConnectionPool thread safety, transaction per connection

**Key findings:**
- **pg_search** (ParadeDB, Tantivy-based BM25) is not in Homebrew core. On macOS it installs from a `.pkg`, and it needs `shared_preload_libraries` plus a restart. Since 0.25 it also requires pgvector. Sources: https://docs.paradedb.com/deploy/self-hosted/extension and https://github.com/paradedb/paradedb/tree/main/pg_search.
  - Built-in `ts_rank` / `ts_rank_cd` has no IDF and cannot do top-N without scoring every match. Source: https://www.paradedb.com/learn/search-in-postgresql/bm25.
  - Other writers argue `ts_rank_cd` is "good enough" for the lexical arm of an RRF hybrid. Source: https://www.benmoataz.com/posts/hybrid-search-pgvector-bm25/.
  - **Informs D4:** neither extension BM25 nor `ts_rank`. Popoto's own BM25, held in a plain postings table, keeps the tuned lexical arm and adds no server extension.
- **pgvector 0.8 iterative scans** (`hnsw.iterative_scan = relaxed_order`, `hnsw.max_scan_tuples` default 20k) fix filtered HNSW queries that return fewer rows than `LIMIT`. With a selective filter, Postgres can also scan the tenant's rows by btree and rank them exactly. Sources: https://github.com/pgvector/pgvector and https://docs.pgedge.com/pgvector/v0-8-1/filtering/.
  - The local server has `vector` 0.8.7 available, checked via `pg_available_extensions`.
  - **Informs D5:** an exact scan within the scope comes first, and HNSW is added only past a measured threshold. spike-4 measured it: exact is 2.6 ms at about 1k rows in scope and 37 ms at about 12k rows.
- **psycopg 3 pools**: one process-global `ConnectionPool`, with one connection per unit of work. Connections can be shared across threads, but sharing them shares the transaction. Call `pool.wait()` at startup to fail fast. Source: https://www.psycopg.org/psycopg3/docs/advanced/pool.html.
  - **Informs TD-3:** the POC's single shared autocommit connection is exactly the shape the docs warn about.

## Spike Results

### spike-1: What Valor actually uses (decides scope)
- **Assumption**: "Valor exercises most of the memory feature set, so the port has to cover it."
- **Method**: code-read of `/Users/valorengels/src/ai` (read-only). popoto is installed at 1.9.0 from PyPI (`pyproject.toml:21`, `uv.lock`).
- **Finding**: the assumption is false. Valor's production surface is narrow.
  - **One model.** `Memory(WriteFilterMixin, AccessTrackerMixin, Model)` (`models/memory.py:113`). It has an `AutoKeyField`, two `KeyField`s (`agent_id`, `project_key`), six `StringField`s, a `FloatField importance`, and a `DictField metadata`. It also has the memory fields `DecayingSortedField(base_score_field="importance", partition_by="project_key")`, `ConfidenceField(initial_confidence=0.5)`, `BM25Field(source="content")`, `GracefulEmbeddingField(source="content")` (an `EmbeddingField` subclass that persists without a vector when the provider fails), and `ExistenceFilter(fingerprint_fn=content)`.
  - **Embeddings** come from `OpenAIProvider`, 1536 dimensions (`agent/embedding_provider.py`). Today they are stored as `.npy` files, not in Redis.
  - **Query shapes used:**
    - `filter(project_key=)`, `filter(memory_id=)` with `.first()` or `[0]`, `filter(agent_id=)` (a count), `filter(source=)`
    - `.all()`, `.no_track()`, `.get(redis_key)`
    - `save()`, `save(update_fields=[...])`, `save(migrate_key=True)` (one script), `delete()`, `safe_save()`
  - **Retrieval**: `ContextAssembler(Memory, retrieval_mode="auto").assemble(query_cues={"query": q}, partition_filters={"project_key": pk})`. That resolves to the **hybrid** path: BM25 plus vector, fused with RRF k=60 (`agent/memory_retrieval.py:317`). There is also an optional `ContextAssembler.assess` composite probe (relevance 0.6, confidence 0.3).
  - **Feedback**: `ObservationProtocol.on_context_used`. `acted` touches `relevance`, confirms staged access, and applies a confidence signal of 0.9. `dismissed` and `deferred` discard staged access. `_post_effects` stages reads and applies competitive suppression (`update_confidence(0.3)`).
  - **Field statics used**: `BM25Field.search`, `ConfidenceField.get_confidence`, `EmbeddingField.load_embeddings` / `garbage_collect`, `ExistenceFilter.might_exist`. The bloom is one global filter, with ≥2 token hits required.
  - **Raw Redis access in Valor that has to change at cutover:**
    - `EXISTS` (`models/memory.py:68`)
    - `ZREVRANGE` / `ZRANGEBYSCORE` on the relevance ZSET and `HGETALL` on the confidence hash, in the four-signal RRF fallback (`agent/memory_retrieval.py:116-163, 514-520`)
    - index-orphan scripts
    - the `{pk}:memory-gate:*` / `{pk}:memory-distill:*` counters. These are not memory records and stay in Redis.
  - **Not used by Valor**:
    - **Query API**: async, `popoto.batch()`, `order_by`, `values=`, `.count()`, `top_by_decay`, `semantic_search`, `keyword_search`, `might_exist_batch`.
    - **Fields and mixins**: `ValidityField`, `CyclicDecayField`, `CoOccurrenceField`, `PredictionLedgerMixin`, `EventStreamMixin`, `FrequencySketch`, `GeoField`, `TagField`.
    - **Recipes**: every recipe except `ContextAssembler`. That includes `memory_lifecycle`, so the tombstone prior always reads 0 burials (only `memory_lifecycle.py:868` records one). `question_queue` is also unused.
    - **WriteFilter's priority tier** (`$WF:…:priority`) has no reader.
  - **Valor-side defects surfaced, recorded for the cutover and not fixed here:**
    - `memory-decay-prune` reads a nonexistent `created_at`, so it never prunes.
    - `apply_defaults()`'s three outcome-signal overrides are read too late to take effect (`fields/observation.py:65-80` copies them at import).
    - `record.confidence` on a hydrated `Memory` is the stale 0.5 baseline, because the live value is in the companion hash.
- **Confidence**: high. Every claim has a file:line in the survey.
- **Impact on plan**: the build covers the spike-1 surface and nothing else (see "Cut by Valor usage" in §No-Gos). Three of the defects are fixed *by construction* by the typed schema:
  - `created_at` becomes an engine-owned column.
  - Confidence becomes a row column, so it is never stale.
  - Defaults are read at call time.

### spike-2: How each used feature is stored and scored today (decides the column mapping)
- **Assumption**: "Every Valor feature has a direct typed-column or table equivalent that preserves its math."
- **Method**: code-read of `src/popoto` on `main`.
- **Finding**: confirmed. The math the typed schema must reproduce exactly:
  - **Decay**: `elapsed_days = max((now-ts)/86400, 0.01)`, `score = sign(b)·|b|·elapsed^-rate`, with `rate` = `Defaults.DECAY_RATE` (Valor sets 0.3).
    - Confidence modulation: `eff = rate·2^(2s(c0-c))`, `score *= max(elapsed,1)^-(eff-rate)`, with s=0.5 and c0=0.5.
    - Ties break by key ascending (`decaying_sorted_field.py:79-301`).
  - **Confidence**: capped running mean. `n_eff = min(n+1, 20)`, `c' = clamp(c + (signal-c)/(n_eff+1), 0, 1)`, and `signal ≥ 0.5` counts as a corroboration (`confidence_field.py:63-123`).
  - **BM25**: k1=1.2, b=0.75, `idf = ln((N-df+0.5)/(df+0.5)+1)`. The tokenizer is lowercase → `\W+` split → drop tokens under 3 characters and ~52 stopwords (`fields/_tokenizer.py`). N and avgdl are corpus-wide, not per partition.
  - **Embedding**: cosine over pre-normalised float32 vectors, keeping scores > 0.
  - **Bloom**: token-level membership over the fingerprint's tokens. It cannot forget, and it has about 1% false positives.
  - **Access tracker**: every hydrate stages a read (24 h TTL). Confirm moves staged reads into `access_count` and `last_accessed`. Discard drops them.
  - **RRF**: `Σ w_i/(k + rank_i + 1)`, with 0-based rank and k=60.
- **Confidence**: high.
- **Impact on plan**: this is the source for the schema in §Solution. Each formula becomes a SQL expression over typed columns. The parity tests (§Test Impact) use the Redis implementation as the oracle.

### spike-3: What on `poc/backend-seam` is reusable (decides the branch's fate)
- **Assumption**: "Continuing on the branch is cheaper than starting from `main`."
- **Method**: code-read of `origin/poc/backend-seam` @ `efda14a3`, plus `git diff --stat origin/main...origin/poc/backend-seam`.
- **Finding**: false for the storage engine. Mostly false for the Redis refactor. True for the test harness and the lessons.
  - The branch is 65 files and +18,600/−1,989 lines. 13 `src/` modules were rewritten to route Redis through the protocol: `models/base.py` 880 changed lines, `validity_field.py` 551, `indexed_field_mixin.py` 497, and so on.
  - The protocol is *opaque-index-name and msgpack-bytes in, bytes out* (`backends/__init__.py:110-601`). A typed schema needs the opposite contract: decoded values plus `(model, field)`. Cut points listed by the spike: `save_record`, `swap_index` / `swap_tags`, `load_record*`, and `decayed_rank` / `confidence_update`. Each is a signature change on all 46 methods' callers.
  - `PostgresBackend`'s storage (`bytea` rows, 10 PL/pgSQL functions, the msgpack decoder in SQL, per-key advisory locks) is the layout the maintainer rejected.
  - **Reusable**:
    - The pytest harness *design*: schema-per-run `popoto_test_<hex>`, the `public` refusal, `TRUNCATE` per test, the fixture-scoped binding, and pinning the session default to Redis (`pytest_plugin.py` branch diff).
    - The CI job shape: `postgres` service plus a Redis service for the plugin.
    - The `psycopg` entry in `check_lock_imports.py`.
    - `scripts/bench_backend_seam.py`'s measurement method.
    - The report's tech-debt lessons: TD-2, TD-3, TD-8, TD-9, TD-12, TD-13.
  - The branch is 103 commits ahead of `main` and 2 behind. #730 adds `recipes/question_queue.py`, a new Redis-only consumer the seam does not cover. The only textual conflict is `mkdocs.yml`.
- **Confidence**: high.
- **Impact on plan**: decision D1. The branch is not rebased or merged; it is archived and the reusable pieces are harvested.

### spike-4: SQL BM25, pgvector and single-statement fusion at Valor's scale (prototype)
- **Assumption**: "At 20k rows, exact BM25 in plain SQL, an exact (unindexed) vector scan within one project, and a one-statement RRF are all fast enough for a per-turn recall. Neither pg_search nor HNSW is needed yet."
- **Method**: prototype in a throwaway schema on local PostgreSQL 18.6 with pgvector 0.8.7. No popoto, no Redis.
- **Setup**:
  - **Hardware and server**: Apple M4, default `postgresql.conf`, psycopg3 prepared statements over localhost.
  - **Runs**: 200 timed runs after 20 warm-up runs, after `VACUUM ANALYZE`.
  - **Data**: 20,000 rows across five projects (59.8% / 20.4% / 9.9% / 5.1% / 4.9%). Docs are 40–80 tokens from a 30k-term Zipf vocabulary, giving 985k postings. Vectors are synthetic 768-d clusters.
  - **Teardown**: the throwaway `spike755_*` schemas were dropped afterwards. The scripts are kept in the session scratchpad and are not committed.
- **Finding**: partly true. The scope key has to be part of the postings key, and the largest project needs HNSW.

  | top-50 within one project | 60% project p50 / p95 | 5% project p50 / p95 |
  |---|---|---|
  | BM25, postings `(term, id)`, live N/avgdl | 46.1 / 69.6 ms | 46.5 / 62.3 ms |
  | **BM25, postings `(scope, term, id)`, live per-scope stats** | **21.4 / 38.6 ms** | **1.9 / 3.3 ms** |
  | BM25, same postings, maintained stats row | 26.0 / 54.7 ms | 1.7 / 5.6 ms |
  | `tsvector` + GIN, `ts_rank_cd` | 52.4 / 83.2 ms | 6.4 / 10.1 ms |
  | Exact cosine (btree on scope) | 37.1 / 58.6 ms | **2.6 / 5.6 ms** |
  | HNSW, `iterative_scan=relaxed_order`, ef_search=40 | **0.9 / 1.2 ms** | 9.3 / 13.2 ms (forced) |
  | Decay `ORDER BY` computed expression (btree on scope) | 4.2 / 13.6 ms | 0.35 / 0.8 ms |
  | RRF k=60 over BM25 (scoped postings) + exact vector + decay | 95.5 / 207 ms | 5.8 / 14.2 ms |

  - **HNSW recall@50 against exact**:
    - Small project: about 0.90, with a worst query of 0.80. The planner already picks the exact path there unless forced.
    - Large project: mean ≥ 0.99, but one query in 100 returned **0.0** at ef_search=100.
  - **Write cost**: one memory plus ~50 postings in one transaction is 2.9–3.1 ms p50 (5.9 ms p95) with every index, and 1.4–2.2 ms without HNSW. A maintained stats row added no measurable latency, but it is a single-row hot spot.
  - **Size at 20k rows**: heap plus vector TOAST 183 MB, postings 96 MB, HNSW 78 MB.
  - **Not measured**: RRF with HNSW for the large project. It is estimated at 25–30 ms as the sum of its arms.
- **Confidence**:
  - High for the relative shapes.
  - Medium for absolute numbers. Vectors were 768-d synthetic, against Valor's 1536-d real ones, and each configuration ran once.
- **Impact on plan**:
  - **D4**: postings are keyed `(scope, term, record_id)`, with live per-scope statistics and no stats row. `tsvector` is rejected.
  - **D5**: the vector arm chooses exact or HNSW by scope size, and HNSW has a recall guard.
  - **Recall budget**: set from these numbers (§Success Criteria).
  - **Build measurement**: the 0.0-recall pathology is re-measured on real 1536-d data before the threshold is pinned (§Rabbit Holes).

## Data Flow

All three flows below are for a model bound to Postgres by its base class, `popoto.pg.Model`.

**Write: `Memory.safe_save(content=…, project_key=…, importance=…)`**
1. `Model.save()` → `WriteFilterMixin` gate in Python. `compute_filter_score()` below `_wf_min_threshold` returns `False`, and nothing is written. This step is unchanged.
2. The engine encodes typed values from the field declarations. NUL bytes are refused at the field, before any SQL.
3. The engine calls the embedding provider for `EmbeddingField` *outside* the transaction. On failure or timeout the vector is `NULL` and the save proceeds: the graceful behaviour becomes the default.
4. One transaction, all rows locked in a fixed global order:
   1. `INSERT … ON CONFLICT (pk) DO UPDATE` on the model row. This writes every typed column. Decay `relevance` is stamped on insert and on full save, matching today's `auto_now`. Confidence columns are set only on insert. `bm25_len`, `created_at` and `updated_at` are set too.
   2. BM25 postings: delete the old terms for this row, then insert the new ones, sorted by term.
   3. ExistenceFilter membership: delete this record's old token rows, then insert the new ones. These rows belong to this record alone, so no row is shared with another writer.
5. After commit, piggyback maintenance (§Solution D7) may run one bounded batch that embeds a few `NULL`-vector rows. It never runs inside the caller's transaction.

**Recall: `ContextAssembler(Memory).assemble(query_cues={"query": q}, partition_filters={"project_key": pk})`**
1. `_pull_path_hybrid` sees a pg-bound model and calls `Memory.query.recall(q, scope={"project_key": pk}, limit=5*max_items, weights=…)`.
2. `recall` embeds `q` (`input_type="query"`) and issues **one SQL statement** with three CTEs: BM25 top-k over postings joined to the scope, vector top-k by `<=>` within the scope, and RRF `Σ w/(60+rank)`. It returns typed `(instance, rrf_score)` pairs with full rows, so there is no second hydrate round trip.
3. The assembler's model-agnostic Python is unchanged: dedup, superseded filter, token budget, FOK score, and formatting.
4. `_post_effects` takes a pg branch *before* it opens its Redis `batch()` pipeline (§D8). Staged reads become one `UPDATE … SET staged_reads = staged_reads + 1, staged_at = now()` over the selected keys. Competitive suppression is one `UPDATE` applying the confidence expression with signal 0.3 to the unselected keys. Both run in PK order in one transaction.

**Outcome: `ObservationProtocol.on_context_used(memories, {key: outcome})`**
1. `acted` keys, in one statement: set `relevance = now()`, `access_count += staged_reads` (when staged within 24 h), `last_accessed_at = now()`, `staged_reads = 0`, and apply the confidence expression with signal 0.9 (see the D2 note on `Defaults`).
2. `used` keys: confirm staged reads only, with no confidence or decay effect.
3. `dismissed` / `deferred` keys: `staged_reads = 0`. `contradicted` keys: `staged_reads = 0` plus the contradicted confidence signal.
3. Everything is one transaction, with rows locked in PK order.

## Architectural Impact

- **New dependencies**: `psycopg[binary,pool]>=3.2` and `pgvector` (the Python adapter), both behind a `postgres` extra. On the server: PostgreSQL ≥ 16 with the `vector` extension ≥ 0.8. No `pg_search`, `pg_cron`, `bloom` or PL/pgSQL functions.
- **Interface changes**: additive. The new `popoto.pg` package is opt-in by base class. The existing `popoto.Model`, every Redis field, and every Redis wire command are **unchanged**.
  - Five static entry points Valor calls gain a one-line early dispatch to the pg engine when the model is pg-bound: `ObservationProtocol.on_read` / `on_context_used`, `ConfidenceField.update_confidence` / `get_confidence`, `BM25Field.search`, `ExistenceFilter.might_exist`, `EmbeddingField.garbage_collect`.
  - `ContextAssembler` gains a pg branch in each of `_pull_path_hybrid`, `assess` and `_post_effects`.
- **Coupling**: the pg engine depends on field *declarations* (class, constructor args), never on field hooks. The Redis field layer does not import `popoto.pg`. Dispatch goes one way only, through a lazy import inside the five entry points.
- **Data ownership**: Valor's memory moves from Redis DB 0, plus `.npy` files under `~/.popoto/content`, to one Postgres schema (`popoto` by default). The Redis gate/distill counters stay in Redis and remain Valor's.
- **Reversibility**: high until Valor cuts over. The package is unused unless a model subclasses `popoto.pg.Model`. After cutover, reverting means Valor switching its base class back and #756 run in reverse, which is not planned.

## Appetite

**Size:** Large

**Team:** Solo dev (lead plus builders), with code reviewer and validator passes.

**Interactions:**
- PM check-ins: 1–2. One after the core engine to confirm the schema with #756's author, and one before the Valor cutover handoff.
- Review rounds: 2+. The schema and engine get one; retrieval and parity get one.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Local PostgreSQL ≥ 16 reachable | `psql "${POPOTO_POSTGRES_URL:-postgresql://localhost:5432/postgres}" -Atc 'select current_setting($$server_version_num$$)::int >= 160000'` | engine and tests |
| pgvector ≥ 0.8 available | `psql "${POPOTO_POSTGRES_URL:-postgresql://localhost:5432/postgres}" -Atc "select default_version from pg_available_extensions where name='vector'"` | `EmbeddingField` columns, iterative scans |
| Server encoding UTF8 | `psql "${POPOTO_POSTGRES_URL:-postgresql://localhost:5432/postgres}" -Atc 'show server_encoding'` | text collation / NUL rule (TD-13) |
| Redis on DB 15 for the oracle legs | `redis-cli -n 15 ping` | parity tests use the Redis implementation as oracle |
| Dev extras | `python -c "import psycopg, psycopg_pool, pgvector"` | after `pip install -e '.[dev,embeddings,mcp,postgres]'` |

## Solution

### Key Elements

- **`popoto.pg`**: a new subpackage. A model opts in by subclassing `popoto.pg.Model` instead of `popoto.Model`.
  - It reuses popoto's existing `Field` classes as *declarations*.
  - Each supported field type has a **field compiler** that maps the declaration to columns, companion tables, indexes and SQL expressions.
  - The Redis `Model`, Redis fields, and Redis wire behaviour are untouched.
- **Engine**: one psycopg 3 connection pool per DSN per process. It handles DDL ownership, transactions, lock ordering and bounded retry.
- **Recall**: one SQL statement fusing BM25, vector, and optionally decay and confidence arms with RRF. It returns typed `(instance, score)` pairs.
- **Dispatch seams**: one-line early-outs in the five static entry points Valor calls, and in `ContextAssembler`'s hybrid pull and `assess`. They route pg-bound models to the engine.
- **Subconscious maintenance**: schema creation, embedding backfill and staged-read expiry ride on normal reads and writes. There are no daemons, cron jobs or required CLI steps.

### D1: The POC branch is archived, not merged

`poc/backend-seam` is tagged `archive/poc-backend-seam-631` and not rebased (spike-3).
- **Brought to `main` as documentation**: the POC report, `docs/plans/postgres_backend_poc.md`, whose tech-debt register this plan cites.
- **Dropped**: the report's §8.1 "zero-behaviour-change Redis minor release". It has no consumer now that memory does not need to run on both backends, and it carries the TD-23 wire changes.
- **Harvested as patterns, re-implemented fresh in the build**:
  - the schema-per-session test harness, which refuses `public`
  - the CI `postgres` service job
  - the `check_lock_imports.py` entry
  - the benchmark method

### D2: "Protocol v2" is a field-compiler contract, not a storage protocol

The POC's protocol was opaque index names and msgpack bytes. v2 is declarative instead. For each supported `Field` class, resolved by MRO so `GracefulEmbeddingField` inherits `EmbeddingField`'s compiler, a compiler supplies the following:

| Contract member | Purpose |
|---|---|
| `columns(field) -> list[ColumnSpec]` | typed columns on the model table |
| `companions(field, model) -> list[TableSpec]` | side tables (postings, bloom membership), always `ON DELETE CASCADE` |
| `indexes(field, model) -> list[IndexSpec]` | btree / GIN / HNSW |
| `to_db(field, value)` / `from_db(field, row)` | value transform. The NUL refusal (Q4) lives here. |
| `write_sql(field, ctx)` | extra statements run inside the save transaction (postings, bloom membership) |
| `rank_sql(field, params) -> SqlFragment` | optional ranking expression or CTE used by `recall` |

**v1 supported surface**: exactly spike-1's list.
- **Fields**: exactly what Valor's `Memory` declares after cutover. That is `AutoKeyField`, `KeyField`, `StringField`, `FloatField`, `DatetimeField` (for `superseded_at`), `DictField` (as `jsonb`), `DecayingSortedField`, `ConfidenceField`, `BM25Field`, `EmbeddingField` and its subclasses, `ExistenceFilter`.
- **Mixins**: `WriteFilterMixin` and `AccessTrackerMixin`.
- **Anything else raises `TypeError` at class creation**, naming the field and the reason. This covers a `Relationship`, a `ValidityField`, `Meta.ttl`, or a field option the compiler does not handle (TD-9: fail at declaration, not at first use).

**Selection is per class, not per process.** Valor keeps Redis models such as `CorpusSizeBaseline` and the gate counters in the same process as a pg `Memory`, so the POC's process-global `POPOTO_BACKEND` / auto-select-on-`POSTGRES_URL` (TD-7) is not carried over.
- **DSN**: the DSN comes only from `POPOTO_POSTGRES_URL` (or `popoto.pg.configure(url=…)`), and the schema from `POPOTO_POSTGRES_SCHEMA`, default `popoto`.
- **Missing DSN**: the first use of a pg model with no DSN raises `popoto.pg.ConfigurationError`. It never silently falls back to Redis.
- **Generic env vars**: a generic `POSTGRES_URL` or `DATABASE_URL` in the environment is never read.

**Popoto owns the DDL.** On first use per process, the engine takes a transaction-scoped advisory lock (`pg_advisory_xact_lock(hashtext('popoto:ddl:'||schema||'.'||table))`) and compares the model's compiled schema fingerprint against `popoto_schema_versions`.
- **New table**: it is created.
- **Additive change** (new nullable column, new companion table or index): it is applied automatically. `CREATE INDEX` on an existing table is not concurrent, which is acceptable at Valor's scale.
- **Destructive or ambiguous change** (dropped or retyped column, changed vector dimension): it raises `SchemaDriftError` with the diff. The operator then runs `python -m popoto.pg migrate <dotted.Model>`, the only manual path.
- **Lifetime**: the check runs once per process per model. This fixes TD-8's per-connection DDL.

**Behavioural decisions taken Postgres-first** (POC report §9):
- **Q2 (transactions)**:
  - Every unit of work is one `READ COMMITTED` transaction.
  - Multi-row writes lock rows in primary-key order (`SELECT … ORDER BY pk FOR UPDATE`).
  - `DeadlockDetected` and `SerializationFailure` are retried up to 3 times with jitter, then re-raised.
  - Deadlock is prevented by ordering and only *also* detected (TD-2).
- **Q3 (TTL)**: not supported in v1. Valor's `Memory` has no TTL. Declaring one is a class-creation error. See §No-Gos.
- **Q4 (NUL)**: a `\x00` in any text value raises `ValueError` naming the field before SQL is built. Postgres `text` cannot store it (TD-12).
- **Q5 (return type)**: new pg-native APIs (`recall`, `top_by_relevance`) return `list[tuple[Model, float]]`. Dispatched *statics* keep their existing Redis return shape, with the primary-key string where the Redis key was, so Valor's call sites only change where they parse Redis keys.
- **`Defaults` at call time**: pg code paths read `Defaults.*` (decay rate, confidence cap, outcome signals) when called, never at import. That makes Valor's `apply_defaults()` overrides effective on the pg path. The Redis path's import-time copy is frozen with the rest of Redis memory.
- **Access tracking without writes on read**: a pg hydrate never writes. Reads are staged only by `ObservationProtocol.on_read`, which `ContextAssembler._post_effects` already calls for the memories it injects. Staged reads expire by comparison at confirm time (`staged_at > now() - interval '24 hours'`), so no reaper is needed. `.no_track()` is accepted and is a no-op. The per-record access log (`$AT:…:log`) has no reader in Valor and is not carried over.

### D3: Schema (concrete; the target for #756)

This is the schema `popoto.pg` compiles from Valor's `Memory` *after* the cutover declaration change. That change is a Valor-repo edit, listed in §No-Gos (`[EXTERNAL]`): the base class switches to `popoto.pg.Model`, and `retired_reason = StringField(null=True)` and `superseded_at = DatetimeField(null=True)` are added. `superseded_by` stops carrying the sentinels `dismissal-prune`, `decay-prune-tier2` and `cleanup-junk-extraction`, which move to `retired_reason`. The compiler emits this DDL deterministically, and a test pins it.

```sql
CREATE EXTENSION IF NOT EXISTS vector;          -- once per database; engine checks, never drops
CREATE SCHEMA IF NOT EXISTS popoto;

-- engine bookkeeping
CREATE TABLE popoto.popoto_schema_versions (
  table_name  text PRIMARY KEY,
  model       text NOT NULL,                    -- dotted path, e.g. models.memory.Memory
  fingerprint text NOT NULL,                    -- sha256 of the compiled spec
  spec        jsonb NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE popoto.memory (
  -- AutoKeyField / KeyFields
  memory_id     text PRIMARY KEY CHECK (memory_id ~ '^[0-9a-f]{32}$'),   -- uuid4 hex, unchanged from Redis
  agent_id      text,
  project_key   text,
  -- plain fields (StringField default "" -> NOT NULL DEFAULT '')
  content       text NOT NULL DEFAULT '',
  title         text NOT NULL DEFAULT '',
  source        text NOT NULL DEFAULT 'agent',
  reference     text NOT NULL DEFAULT '',
  importance    double precision NOT NULL DEFAULT 1.0,
  metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- includes outcome_history (<=10), dismissal_count, last_outcome
  superseded_by text,                                  -- replacement memory_id; soft reference, no FK (see note)
  superseded_by_rationale text NOT NULL DEFAULT '',
  retired_reason text,                                 -- 'dismissal-prune' | 'decay-prune-tier2' | 'cleanup-junk-extraction' | NULL
  superseded_at timestamptz,
  -- DecayingSortedField(base_score_field="importance", partition_by="project_key")
  relevance     timestamptz NOT NULL DEFAULT now(),    -- last-touched instant (Redis ZSET score)
  -- ConfidenceField(initial_confidence=0.5)
  confidence                double precision NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  confidence_evidence       integer NOT NULL DEFAULT 0,
  confidence_corroborations integer NOT NULL DEFAULT 0,
  confidence_contradictions integer NOT NULL DEFAULT 0,
  -- BM25Field(source="content")
  bm25_len      integer NOT NULL DEFAULT 0,            -- token count after popoto tokenizer
  -- EmbeddingField(source="content")   (dimension from the provider at class creation)
  embedding       vector(1536),                        -- NULL = not yet embedded (graceful default)
  embedding_model text,                                -- e.g. 'openai:text-embedding-3-small'
  embedded_hash   text,                                -- md5(content) the vector was computed from
  -- AccessTrackerMixin
  access_count     integer NOT NULL DEFAULT 0,
  last_accessed_at timestamptz,
  staged_reads     integer NOT NULL DEFAULT 0,
  staged_at        timestamptz,
  -- engine-owned (every pg table)
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  estimated_fields text[],          -- columns whose value was inferred on import; engine never clears
  migrated_from    jsonb            -- import provenance {machine, snapshot, run_id}; engine sets NULL on every native write
);
CREATE INDEX memory_project_key_idx ON popoto.memory (project_key);       -- KeyField; scope btree (spike-4)
CREATE INDEX memory_agent_id_idx    ON popoto.memory (agent_id);          -- KeyField
CREATE INDEX memory_superseded_idx  ON popoto.memory (superseded_by) WHERE superseded_by IS NOT NULL;
CREATE INDEX memory_embedding_hnsw  ON popoto.memory USING hnsw (embedding vector_cosine_ops);

-- BM25Field companion: postings keyed by scope first (spike-4)
CREATE TABLE popoto.memory__bm25 (
  scope     text NOT NULL,          -- coalesce(project_key, '')
  term      text NOT NULL,
  record_id text NOT NULL REFERENCES popoto.memory (memory_id) ON DELETE CASCADE,
  tf        integer NOT NULL CHECK (tf > 0),
  PRIMARY KEY (scope, term, record_id)
);
CREATE INDEX memory__bm25_record_idx ON popoto.memory__bm25 (record_id);

-- ExistenceFilter companion: exact membership instead of a bloom
CREATE TABLE popoto.memory__bloom (
  token     text NOT NULL,
  record_id text NOT NULL REFERENCES popoto.memory (memory_id) ON DELETE CASCADE,
  PRIMARY KEY (token, record_id)
);
```

**Schema notes for #756 and the build:**
- **Scope.** The scope column comes from `DecayingSortedField.partition_by` (Valor: `project_key`). That one partition is used for decay ranking, BM25 postings and statistics, and the vector arm's filter. With no `partition_by`, scope is `''`. There is no separate `Meta.scope` option. A recall filter matches the scope as `scope = coalesce($pk, '')`, never `scope = $pk`, because NULL never matches. Other `KeyField`s, such as `agent_id`, are plain btree filters that compose with the scope in `recall(…, filters={…})`.
- **No FK on `superseded_by`.** Valor deletes losers in consolidation and pruning. An `ON DELETE SET NULL` foreign key would erase lineage, and `RESTRICT` would block pruning. It is an indexed soft reference instead.
- **The ranking formula is computed at query time, not stored.** Only its inputs are persisted (`relevance`, `importance`, the confidence columns). A stored score would go stale every second.
- **`bm25_len`, the postings and the bloom rows are derived from `content`.** #756 does not need to write them. Loading through the engine's save path, or calling `python -m popoto.pg reindex <Model>`, rebuilds them.
- **Derived rows follow partial saves.** A `save(update_fields=…)` whose set intersects `{content, <scope column>}` rewrites that record's postings and bloom rows, and `bm25_len`, in the same transaction. If only the scope column changed, it runs `UPDATE {t}__bm25 SET scope=$new WHERE record_id=$pk`. A test saves, runs `save(update_fields=["project_key"])`, then asserts recall finds the record only in the new scope. `save(migrate_key=True)` (primary-key change) is not supported on pg and raises.
- **Vector dimension and stale vectors.** The dimension is fixed at class creation from the declared provider's `dimensions`. A mismatched vector, such as 768-d legacy data, is written as `NULL` and is re-embedded by D7's backfill.
- **`migrated_from` contract.** Every engine `INSERT` or `UPDATE` writes `migrated_from = NULL`. A delta re-load guards with `ON CONFLICT (memory_id) DO UPDATE … WHERE popoto.memory.migrated_from IS NOT NULL`, so it can never overwrite a natively touched row.
- **`estimated_fields`** lets #756 flag `created_at` and `superseded_at` values it inferred, since uuid4 ids carry no time.
- **Not in this schema**:
  - **The access log**: there is no reader, so it is dropped.
  - **The `{pk}:memory-gate:*` / `memory-distill:*` counters**: they are Valor app state, not memory, and stay in Redis (see Open Questions).
  - **The WriteFilter priority ZSET**: it has no reader. The `WriteFilterMixin` gate still runs. Its priority tier is a no-op on pg.

### D4: Lexical arm = popoto's BM25 in plain SQL over scope-keyed postings

`BM25Field.search` / `recall` compute exactly `idf = ln((N-df+0.5)/(df+0.5)+1)` and `tf·(k1+1)/(tf+k1·(1-b+b·len/avgdl))`, with k1=1.2 and b=0.75, using the same `popoto.fields._tokenizer.tokenize` on the query.
- **Statistics are live and per scope**: `N = count(*)`, `avgdl = avg(bm25_len)` and `df` are all taken within the scope. They are computed in the statement, with no stats row: spike-4 measured no latency gain and a write hot spot.
- **This deliberately differs from Redis**, whose N and avgdl are corpus-wide even for a scoped search. Per-scope IDF is the correct statistic for a scoped search. Parity is therefore asserted as ranking overlap on a fixture, not equal scores (§Test Impact).
- **Unscoped search**, a Valor fallback call site only, uses corpus-wide statistics. It relies on PostgreSQL 18's btree skip scan over the scope-leading key. On 16 and 17 it degrades to a postings scan, which is acceptable for a fallback path.
- **Rejected alternatives**: `pg_search` (an extension Valor's machines would have to preload) and `ts_rank` / `ts_rank_cd` (no IDF; 52 ms in spike-4, and not BM25). See §Rabbit Holes for the hot-term lever if the large-scope budget is missed.

### D5: Vector arm = pgvector, exact inside small scopes and HNSW past a pinned threshold

The HNSW index always exists, at about 1 ms extra write cost.
- **Choosing the path**: `recall` reads the scope's row count, which it already computes for BM25's N.
  - **At or below `Defaults.PG_VECTOR_EXACT_MAX`** (a magic number, initially 5,000, re-pinned from the build's real-data measurement), it forces the exact path by ordering on `(embedding <=> $q) + 0`. The HNSW index cannot match that expression. This path measured 2.6 ms at ~1k rows, and the planner already prefers it.
    - It deliberately does **not** use `SET LOCAL enable_indexscan = off`. That setting is statement-wide, so it would also strip index access from the BM25 and decay CTEs of the fused statement.
  - **Above the threshold**, it orders on the bare `embedding <=> $q` with `SET LOCAL hnsw.ef_search` and `hnsw.iterative_scan = relaxed_order`. Those settings affect only HNSW.
  - **The count** that drives both the threshold and the guard is `count(*) FILTER (WHERE embedding IS NOT NULL)` within the scope, not the total scope size.
- **Recall guard**: if the HNSW arm returns fewer than `min(limit, embedded_count)` rows (spike-4's 0.0-recall query returned short), the arm is re-run exactly. This bounds the pathology to a latency cost, never a silent empty arm.
  - The guard does not count rows dropped by the `1 - distance > 0` filter.
  - A benchmark case with 20% `NULL` embeddings in a 12k-row scope asserts the guard fires 0 times.
  - `EXPLAIN` on the fused statement asserts that the postings and decay CTEs keep index or bitmap access.
- **Distance and filter**: distance is `<=>` (cosine), and the arm keeps `1 - distance > 0` to match Redis's positive-cosine filter.
- **No vector**: when the query embedding fails, the arm is omitted and fusion proceeds with the remaining arms, as in today's hybrid fallback.

### D6: Engine runtime (pool, fork safety, transactions)
- **Pool**: one `psycopg_pool.ConnectionPool` per (DSN, pid). It opens lazily on first use, uses `min_size=1`, and has `max_size=Defaults.PG_POOL_MAX` (4). The pool is recreated when `os.getpid()` changes, which makes it fork-safe; Valor's workers fork.
- **Unit of work**: `with pool.connection() as c, c.transaction():`. No connection is ever shared across threads (TD-3).
- **No public `popoto.pg.transaction()`**: the internal save, outcome and post-effect paths each own a private transaction. Valor uses no `popoto.batch()` (TD-5), so no public multi-operation transaction API is built.
- **Async**: not provided (TD-6). Valor uses none.
- **Encoding check**: once per pool, the engine asserts `server_encoding = 'UTF8'` (TD-13) and that the `vector` extension is at least 0.8.
- **Postgres down**: errors surface as `popoto.pg.UnavailableError`, which is a member of `OUTAGE_ERRORS`.
  - `ContextAssembler` re-raises it, the same contract as a Redis outage.
  - Valor's recall wrapper and `safe_save` already catch `Exception`, so the turn proceeds without memory.
- **Health**: since that degrade is silent to the agent, the engine keeps a process-level health record.
  - `popoto.pg.health() -> {ok, last_ok_at, consecutive_failures, dropped_writes}` is updated in the `UnavailableError` handler and on every success.
  - It logs at ERROR once per outage window (on the transition to not-ok), not once per call.
  - Writes are not spooled. A dropped write is counted, not queued, so there is no unbounded buffer.

### D7: Subconscious maintenance (no daemon, no cron, no required CLI)
- **Schema**: created or extended on first use (D2).
- **Embedding backfill** piggybacks on writes. After a save whose own embedding call *succeeded*, which means the provider is healthy, the engine embeds up to `Defaults.PG_BACKFILL_BATCH` (4) rows in the same scope where `embedding IS NULL` or `embedding_model` differs from the current one. It writes each with `UPDATE … WHERE memory_id=$1 AND md5(content)=$hash`, so a concurrent content edit is never overwritten with a stale vector. It runs after commit, outside the caller's transaction, and swallows its own errors. Valor's `memory-embedding-backfill` reflection keeps working and becomes optional.
- **Garbage collection disappears**: rows cascade to their postings and bloom rows. On a pg model, `EmbeddingField.garbage_collect` and `sweep_stale_tempfiles` return 0.
- **Staged reads**: expire by comparison at confirm time (D2).
- **Created time**: `created_at` is engine-owned, which unblocks Valor's decay-prune reflection. It currently reads a nonexistent field.

### D8: Integration seams (the only edits to existing modules)

| Entry point (current file) | pg behaviour |
|---|---|
| `ContextAssembler._pull_path_hybrid` (`recipes/context_assembler.py:2406`) | If pg-bound: `recall(query, scope=partition_filters, weights={bm25, vector})`, returning the same candidate list shape. **Zero signal**: when both arms return nothing, the Redis path falls back to the query-blind `_pull_path_composite` (`:2530`). The pg branch does the same through `top_by_relevance(scope, limit)`, and a test pins it. **Outage**: `popoto.pg.UnavailableError` is added to `OUTAGE_ERRORS`, so it re-raises like a Redis outage (`:2471-2472`) and is never read as "no memories". |
| `ContextAssembler.assess` composite probe (`:2828`) | if pg-bound: decay×confidence weighted sum (0.6/0.3) computed in SQL for the candidate keys |
| `ContextAssembler._post_effects` (`:2634`) | **pg branch before `pipeline = batch()` (`:2641`)**, guarded by `getattr(self.model_class, "_popoto_pg", False)`. It collects the selected and unselected keys and makes two bulk calls, `pg.stage_reads(keys)` and `pg.suppress(keys, signal=0.3)`, in **one** PK-ordered transaction. It never runs the per-record `on_read(record, pipeline=…)` loop (`:2644-2646`), and never opens a Redis pipeline. A test patches the Redis client to raise and asserts recall plus post-effects make zero Redis calls. |
| `ObservationProtocol.on_read` / `on_context_used` | One transaction of PK-ordered `UPDATE`s, grouped by outcome. The rows below cover all five `VALID_OUTCOMES` (`observation.py:57`). Cyclic-decay and prediction effects are absent because those fields are not compiled. |
| — `acted` | confirm staged reads, touch `relevance`, confidence `Defaults.ACTED_CONFIDENCE_SIGNAL` |
| — `used` | confirm staged reads only. It does **not** touch confidence or decay (`observation.py:20-25`). |
| — `dismissed`, `deferred` | `staged_reads = 0` |
| — `contradicted` | `staged_reads = 0`, confidence `Defaults.CONTRADICTED_CONFIDENCE_SIGNAL` |
| `ExistenceFilter.definitely_missing` (`:2426`, `:2807`) | covered by dispatching `might_exist`, since it is `return not self.might_exist(…)` (`existence_filter.py:510`). A test asserts that `assemble()` on a populated pg model does not short-circuit to `[]`. |
| `ConfidenceField.update_confidence` / `get_confidence` | `UPDATE … SET confidence = least(greatest(confidence + ($s-confidence)/(least(confidence_evidence+1,20)+1),0),1), confidence_evidence = confidence_evidence+1, confidence_corroborations = confidence_corroborations + ($s>=0.5)::int, confidence_contradictions = confidence_contradictions + ($s<0.5)::int` / column read |
| `BM25Field.search` | D4 statement; returns `[(memory_id, score)]` |
| `ExistenceFilter.might_exist(model, token)` | `EXISTS (SELECT 1 FROM {t}__bloom WHERE token=$1)`: exact, no false positives, forgets on delete |
| `EmbeddingField.load_embeddings` / `garbage_collect` | read vectors from the column / return 0 |
| new `Model.top_by_relevance(scope, limit)` | decay expression `sign(i)·abs(i)·greatest(e,0.01)^(-r) · greatest(e,1)^(-(r·2^(2·0.5·(0.5-c)) - r))`, `e` = days since `relevance`; replaces Valor's raw `ZREVRANGE` fallback |
| new `Model.exists(pk)` | replaces Valor's `POPOTO_REDIS_DB.exists(db_key)` in its `save()` override |

The illustration below shows the cutover declaration Valor will make. It is not code to write in this repo:

```python
class Memory(WriteFilterMixin, AccessTrackerMixin, popoto.pg.Model):
    ...  # every existing field unchanged, plus:
    retired_reason = StringField(null=True)
    superseded_at = DatetimeField(null=True)
```

### Flow

Valor's turn → `ContextAssembler.assemble` → `Memory.recall` (one SQL statement) → injected memories → `_post_effects` stages reads → the agent acts or dismisses → `ObservationProtocol.on_context_used` runs one `UPDATE` transaction → the next save piggybacks the backfill.

### Technical Approach
- Build order is bottom-up, so each layer is testable alone: compiler and DDL → engine (pool, transactions, CRUD, query) → field compilers → recall → seams → parity and benchmark.
- The pg engine imports field classes. Field modules import the pg engine only lazily inside the dispatched statics, behind `getattr(model_class, "_popoto_pg", False)`, so `import popoto` never requires psycopg.
- Each SQL statement is built with `psycopg.sql.Identifier` / `Literal`. There is no string interpolation of identifiers.
- The parity oracle is the existing Redis implementation on DB 15. The same operation sequence runs against both backends and the observable results are compared (§Test Impact).

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] **Backfill swallows its own errors** (D7). A test makes the provider raise during backfill and asserts three things:
  - the caller's `save()` returned success
  - a `logger.warning` was emitted
  - the row's `embedding` is still `NULL`
- [ ] **Outage contract.** With Postgres unreachable (pool pointed at a closed port):
  - `ContextAssembler.assemble` on a pg model re-raises `UnavailableError`, since it is in `OUTAGE_ERRORS`.
  - A Valor-shaped `safe_save` wrapper returns `None`.
  - N failed saves raise `health()["dropped_writes"]` by N, and `health()["ok"]` is False.
  - ERROR is logged once per outage window.
- [ ] **Bounded retry re-raises.** A forced `DeadlockDetected` is retried exactly 3 times and then re-raised. A test counts the attempts with a fault-injecting connection wrapper.
- [ ] **No silent passes in new code.** The new modules contain no bare `except Exception: pass`. A grep test over `src/popoto/pg/` enforces it.

### Empty/Invalid Input Handling
- [ ] **Empty or stopword-only queries.** `recall("")` and a query of only stopwords run no SQL arms, return `[]`, and still let the vector arm run when the embedding exists.
- [ ] **Empty content.** Saving `content=""` writes `bm25_len=0` and no postings or bloom rows. The embedding is `NULL`, and the provider is not called.
- [ ] **NUL bytes.** A `\x00` in any text field raises `ValueError` naming the field, and nothing is written.
- [ ] **Wrong vector dimension.** A provider returning the wrong dimension stores `NULL` and logs it, rather than raising out of `save()`.
- [ ] **Bad declarations.** An unsupported field, `Meta.ttl`, or a `Relationship` on a pg model raises `TypeError` at class creation.

### Error State Rendering
- [ ] **Missing DSN.** It raises `ConfigurationError` with a message naming `POPOTO_POSTGRES_URL`. A test asserts the text.
- [ ] **Schema drift.** `SchemaDriftError` lists every destructive diff entry and the exact `python -m popoto.pg migrate <Model>` command.

## Test Impact

No existing tests are affected. The work is additive: a new `popoto.pg` package, plus early-dispatch branches that are taken only when `model_class._popoto_pg` is true.
- **Regression guard on the existing suites.** The dispatch branches sit in the Redis-path functions covered by:
  - `tests/test_context_assembler.py`, `tests/test_context_assembler_hybrid.py` and `tests/test_context_assembler_token_budget.py`
  - `tests/test_observation_protocol.py`
  - `tests/test_confidence_field.py`
  - `tests/test_bm25_field.py`
  - `tests/test_existence_filter.py`
  - `tests/test_embedding_field.py` and `tests/test_embedding_field_gc.py`

  These must pass **unchanged**. They are the regression guard for the frozen Redis memory path.
- **New tests** (all created):
  - `tests/pg/conftest.py`
  - `test_pg_compiler.py`, which pins the D3 DDL
  - `test_pg_engine.py`
  - `test_pg_fields.py`
  - `test_pg_recall.py`
  - `test_pg_observation.py`
  - `test_pg_parity.py`, which uses the Redis-on-DB-15 oracle
  - `test_pg_concurrency.py`
  - `test_pg_failure_paths.py`
- **Skipping and isolation.** The `tests/pg/` tests skip cleanly when `POPOTO_POSTGRES_URL` is unset, the same rule as the POC. They run in a per-session schema `popoto_test_<hex>`, with `TRUNCATE` per test and a refusal of `public` and `popoto`.

## Rabbit Holes

- **A generic SQL query DSL.** Valor uses equality filters, `.all()`, `.first()` and `.get(pk)`. `order_by`, `values=` and range lookups beyond equality and `__in` are out of scope. Do not port `Query`.
- **Making Redis and pg scores numerically identical.** D4's per-scope IDF diverges on purpose. Parity is ranking overlap plus exact formula unit tests on a hand-computed fixture, not float equality across backends.
- **Tuning HNSW on synthetic data.** Pin `PG_VECTOR_EXACT_MAX` and `ef_search` from one real 1536-d measurement: a copy of Valor-shaped data generated via the provider on DB-15-derived fixtures, or #756's rehearsal dump. More sweeps are waste.
- **Hot-term BM25 pruning.** For example, skipping terms with df/N above some threshold, or WAND. Do this only if the large-scope recall budget (§Success Criteria) is missed after HNSW. It is the next lever spike-4 named, not part of the default build.
- **`pg_search`, `pg_cron`, PL/pgSQL functions, triggers.** All rejected. Everything is plain SQL issued by the engine, so nothing server-side needs installing beyond `vector`.
- **Porting the POC seam's Redis refactor.** Rejected by D1.

## Risks

### Risk 1: Large-scope recall latency
- **Impact:** spike-4 measured the fused query at 96 ms p50 for a 12k-row scope with an exact vector scan. If Valor's biggest project grows there, every turn pays for it.
- **Mitigation:**
  - D5 switches to HNSW past the threshold. The estimate is 25–30 ms, and the build measures it.
  - The budget is a Success Criterion checked by `scripts/bench_pg_recall.py`.
  - The hot-term lever is held in reserve (§Rabbit Holes).

### Risk 2: HNSW returns short or empty results under filtering
- **Impact:** spike-4 saw one 0.0-recall query in 100. A silent empty vector arm would degrade retrieval quality invisibly.
- **Mitigation:** D5's recall guard re-runs the arm exactly when it returns short. A test forces the HNSW path on a tiny scope and asserts the guard fires.

### Risk 3: Connection exhaustion across Valor's processes
- **Impact:** the bridge, workers and Claude Code hooks each open pools, and `max_connections` (default 100) can be hit.
- **Mitigation:** `PG_POOL_MAX=4` per process with lazy open. Hooks are short-lived, and their pool is opened only if they touch memory. The figure is documented in the feature doc's deployment notes.

### Risk 4: #756 and this schema drift apart
- **Impact:** the migration tool targets columns that do not exist.
- **Mitigation:**
  - D3 is pinned by `test_pg_compiler.py`, and the DDL above is the contract.
  - #756's requirements (`migrated_from`, `estimated_fields`, the `retired_reason` split, `superseded_at`, the evidence columns, 1536-d) are already in D3.
  - Any schema change after merge pings #756.

### Risk 5: Valor's cutover breaks raw-Redis call sites
- **Impact:** after cutover, the retrieval fallback's `ZREVRANGE` / `HGETALL` silently return nothing, because the keys no longer exist in Redis.
- **Mitigation:** the cutover checklist (§No-Gos) enumerates every raw call site from spike-1 with its pg replacement (`top_by_relevance`, the `confidence` column, `Model.exists`).

## Race Conditions

### Race 1: Concurrent saves of the same record
- **Location:** engine save path, `src/popoto/pg/engine.py` (new)
- **Trigger:** two processes `save()` the same `memory_id`. Their postings delete/insert interleave.
- **Data prerequisite:** the model row exists or is being inserted.
- **State prerequisite:** the postings and bloom rows must reflect exactly one version of `content`.
- **Mitigation:**
  - The save transaction first runs `INSERT … ON CONFLICT DO UPDATE`, which takes the row lock.
  - It then rewrites the postings and bloom rows for that `record_id` only.
  - The second writer blocks on the row lock until the first commits, and its rewrite then fully replaces the first writer's rows.
  - Companion rows are never shared between records, so there is no cross-record lock.

### Race 2: Backfill vs content edit
- **Location:** D7 backfill
- **Trigger:** the backfill embeds content A, and meanwhile a save changes it to B.
- **Data prerequisite:** the vector must correspond to the current content.
- **State prerequisite:** `embedded_hash = md5(content)`.
- **Mitigation:** the backfill updates with `WHERE memory_id=$1 AND md5(content)=$hash_of_A`. The stale write affects 0 rows. B's own save embedded B, or left `NULL` for a later backfill.

### Race 3: Outcome and suppression updates on overlapping keys
- **Location:** `ObservationProtocol` dispatch, `_post_effects` dispatch
- **Trigger:** two turns confirm and suppress overlapping memory sets in opposite orders, which can deadlock.
- **Data prerequisite:** none.
- **State prerequisite:** confidence updates must be serialisable per row, because they are read-modify-write.
- **Mitigation:**
  - Each confidence update is a single `UPDATE` expression, so it is atomic per row.
  - Multi-row batches lock in PK order, which prevents deadlock.
  - The bounded retry covers anything residual.

### Race 4: First-use DDL from several processes
- **Location:** D2 schema check
- **Trigger:** several Valor processes start at once against an empty schema.
- **Data prerequisite:** the table exists before the first DML.
- **State prerequisite:** exactly one DDL application.
- **Mitigation:** the check-and-apply runs in one transaction under `pg_advisory_xact_lock`, and it re-reads `popoto_schema_versions` after acquiring the lock.

### Race 5: Fork after pool creation
- **Location:** D6 pool
- **Trigger:** a Valor worker forks with an open pool, and parent and child then share sockets.
- **Mitigation:** the pool is keyed by `os.getpid()`. A child creates its own and never touches the inherited one.

## No-Gos (Out of Scope)

- **[SEPARATE-SLUG #756] Moving existing data.** Moving Valor's Redis memories, and the `.npy` embeddings, into the D3 schema belongs to #756's migration tool. This plan only guarantees the schema and the `migrated_from` / `estimated_fields` contract.
- **[EXTERNAL] Valor-repo cutover.** This repo's instruction is that `/Users/valorengels/src/ai` is read-only for this work. The cutover is a change in the `ai` repo, coordinated with #756's runbook:
  - Switch `Memory`'s base class to `popoto.pg.Model`.
  - Add `retired_reason` and `superseded_at`, and stop writing sentinels into `superseded_by`.
  - Replace `_key_exists`'s `POPOTO_REDIS_DB.exists` with `Memory.exists`.
  - Replace the raw `ZREVRANGE` / `ZRANGEBYSCORE` / `HGETALL` in `agent/memory_retrieval.py:116-163,514-520` with `Memory.top_by_relevance` and the `confidence` column.
  - Change the `redis_key`-keyed maps passed to `on_context_used` to primary-key strings.
  - Set `POPOTO_POSTGRES_URL` on every machine.
- **[EXTERNAL] Valor defects from spike-1.** `apply_defaults()` is read too late, decay-prune reads a nonexistent `created_at`, and `record.confidence` is stale. They live in the `ai` repo. The pg path removes the root cause of the last two by construction, and D2 handles the first by reading `Defaults` at call time, but the Valor-side code changes are theirs.
- **[ORDERED] Freezing or deprecating Redis memory in the docs and code.** It waits for Valor's cutover to land (the human-gated #756 runbook). Until then, Redis memory is Valor's live store and must not warn on every call.
- **Rejected, not deferred: fields and recipes Valor does not use.** Per the 2026-10-03 decision ("memory primitives need NOT run on both backends", "Valor is the only adopter"), these get no pg compiler, and declaring them on a pg model raises:
  - `ValidityField` / `tstzrange`. The issue names tstzrange as an available tool, but Valor declares no validity window. The compiler table has the slot (`tstzrange` + GiST via `btree_gist`) if one appears.
  - `CyclicDecayField`, `CoOccurrenceField`, `PredictionLedgerMixin`, `EventStreamMixin`, `FrequencySketch`, `GeoField`, `TagField`, `Relationship`.
  - TTL.
  - Async.
  - The WriteFilter priority tier.
  - The access log.
  - The tombstone prior and `memory_lifecycle`.
  - `question_queue`.

  None of these is tracked as future work. A new adopter need would come in as a new issue.

## Update System

- **Installing.** Valor installs popoto from PyPI, so the new code ships in the next minor release with a `postgres` extra (`psycopg[binary,pool]>=3.2`, `pgvector>=0.3`).
  - `examples/` is untouched.
  - `scripts/check_lock_imports.py` gains `psycopg`, `psycopg_pool` and `pgvector`. That omission would otherwise be review-blocking, per CLAUDE.md.
  - `uv.lock` is regenerated.
- **Server.** Each machine, or the central server (see Open Questions), needs PostgreSQL ≥ 16 with `vector` ≥ 0.8 available, and `POPOTO_POSTGRES_URL` set.
- **Schema.** There is no migration step for this package itself. The schema self-creates on first use (D2). Data migration is #756.
- **CI.** `.github/workflows/tests.yml` gains a `postgres` job: a `pgvector/pgvector:pg17` service plus the existing Redis service. It sets `POPOTO_POSTGRES_URL`, and `REDIS_URL` as `redis://localhost:6379/15`, following #639's pin-where-a-binder-must-exist rule.

## Agent Integration

No agent or MCP integration is required in this repo. The capability is reached through the library API Valor already calls (`ContextAssembler`, `ObservationProtocol`, the `Memory` model). Its integration surface is the D8 dispatch table, and `test_pg_recall.py` exercises it end to end through `ContextAssembler.assemble`. The `popoto` MCP server (`popoto[mcp]`) exposes Redis models only and is unchanged.

## Documentation

### Feature Documentation
- [ ] Create `docs/features/postgres-memory.md` covering:
  - how to opt in by base class, and the env vars
  - the supported fields and what each compiles to (the D3 table)
  - recall and its weights
  - subconscious maintenance
  - the deployment notes: pool sizing, `max_connections`, and pgvector install
  - the failure modes
- [ ] Add it to `mkdocs.yml` nav and `docs/features/README.md`, if the index exists.
- [ ] Bring `docs/plans/postgres_backend_poc.md` to `main` (D1), with a header noting that the branch is archived and which decisions superseded it.

### External Documentation Site
- [ ] `mkdocs build --strict` passes.

### Inline Documentation
- [ ] Each field compiler's docstring states the Redis formula it reproduces and the file:line of the Redis original.
- [ ] `CLAUDE.md` gains a short paragraph covering:
  - `POPOTO_POSTGRES_URL` binding and test-schema isolation, the Postgres analogue of the DB-15 rule
  - "never point tests at `public` or `popoto`"

## Success Criteria

- [ ] **Valor's schema compiles.** A `pg.Model` declared with Valor's exact `Memory` fields, plus the two cutover fields, compiles to the D3 DDL byte-for-byte (`test_pg_compiler.py`).
- [ ] **Valor's entry points behave correctly on pg.** Every spike-1 call shape passes a behaviour test: `filter`, `get`, `first`, `save(update_fields=)`, `delete`, `safe_save`, `BM25Field.search`, `ConfidenceField.get_confidence`, `ExistenceFilter.might_exist`, `ContextAssembler.assemble` (hybrid) and `.assess`, `ObservationProtocol.on_context_used`, `EmbeddingField.load_embeddings` / `garbage_collect`.
- [ ] **Parity with Redis.** On a shared 500-memory fixture, Redis-on-DB-15 and pg agree on:
  - confidence after an identical signal sequence, to 1e-9
  - decay ordering, identical top-20 for a frozen clock
  - BM25 top-10, with Jaccard ≥ 0.8 under scoped search
- [ ] **What the agent sees is preserved.** 30+ query cues are run through `ContextAssembler(Memory, retrieval_mode="auto").assemble(query_cues={"query": q}, partition_filters={"project_key": pk})` on both backends over the same Valor-shaped fixture. When #756's rehearsal dump exists, it replaces the fixture.
  - The fused top-5 overlap must meet a floor pinned from the first measurement, starting at a mean of 0.6.
  - The `definitely_missing` short-circuit decisions must match, except where exact membership removes a bloom false positive.
- [ ] **No Redis on the pg path.** With the Redis client patched to raise, a pg `assemble()` plus `on_context_used()` completes.
- [ ] **Recall latency.** On a 20k-row, 1536-d corpus, measured by `scripts/bench_pg_recall.py` on the dev machine, with the environment stated:
  - a 5%-scope recall is ≤ 15 ms p95
  - a 60%-scope recall is ≤ 60 ms p95
- [ ] **Fully subconscious.** A fresh empty database plus a first `safe_save` from a new process creates the schema with no CLI step. `NULL` embeddings are backfilled by later saves alone.
- [ ] **No new failures in the existing suite.** The Redis memory tests listed in §Test Impact pass unchanged. `ruff check src/`, `black --check src/ tests/` and `scripts/mypy_ratchet.py` (which must not rise) all pass.
- [ ] **`import popoto` works without psycopg** (a subprocess test).
- [ ] Tests pass (`/do-test`).
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

### Team Members

- **Builder (schema compiler + engine)**
  - Name: pg-engine-builder
  - Role: `popoto.pg` package: field-compiler contract, DDL ownership, pool, transactions, CRUD, filter/get
  - Agent Type: builder
  - Domain: Redis/Popoto data, concurrency
  - Resume: true
- **Builder (memory field compilers + recall)**
  - Name: pg-recall-builder
  - Role: Decay, Confidence, BM25, Embedding, ExistenceFilter, AccessTracker compilers, plus `recall`
  - Agent Type: builder
  - Resume: true
- **Builder (dispatch seams)**
  - Name: pg-seams-builder
  - Role: the D8 early-dispatch branches in existing modules
  - Agent Type: builder
  - Resume: true
- **Test engineer (parity + concurrency + bench)**
  - Name: pg-test-engineer
  - Role: the pytest harness, the Redis-oracle parity tests, concurrency tests, the bench script, and the CI job
  - Agent Type: test-engineer
  - Resume: true
- **Validator**
  - Name: pg-validator
  - Role: checks every Success Criterion and Verification row
  - Agent Type: validator
  - Resume: true
- **Documentarian**
  - Name: pg-docs
  - Role: the §Documentation items
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Test harness and CI job
- **Task ID**: build-harness
- **Depends On**: none
- **Validates**: tests/pg/conftest.py (create), `.github/workflows/tests.yml`
- **Informed By**: spike-3 (the POC harness pattern is reusable)
- **Assigned To**: pg-test-engineer
- **Agent Type**: test-engineer
- **Parallel**: true
- Write the per-session `popoto_test_<hex>` schema fixture. It refuses `public` and `popoto`, `TRUNCATE`s per test, and skips when `POPOTO_POSTGRES_URL` is unset.
- Add the CI `postgres` job with a pgvector image and Redis on DB 15.
- Add the `postgres` extra, update `check_lock_imports.py`, and regenerate `uv.lock`.

### 2. Compiler contract and engine
- **Task ID**: build-engine
- **Depends On**: build-harness
- **Validates**: tests/pg/test_pg_compiler.py, tests/pg/test_pg_engine.py, tests/pg/test_pg_failure_paths.py (create)
- **Informed By**: spike-3 (TD-2/3/7/8/9/12/13 lessons)
- **Assigned To**: pg-engine-builder
- **Agent Type**: builder
- **Parallel**: false
- Build `popoto.pg.Model` and its metaclass, which validates fields at class creation.
- Add compilers for the plain fields.
- Write the DDL emitter with fingerprint, advisory-locked first-use apply, the `SchemaDriftError` diff, and `python -m popoto.pg migrate` / `reindex`.
- Build the pool (pid-keyed), `health()`, the bounded retry, `save` / `save(update_fields=)` / `delete` / `exists` / `filter` / `get` / `first` / `all`, the NUL refusal, and the UTF8 and vector-version checks.
- Make the engine own the `created_at`, `updated_at`, `migrated_from` and `estimated_fields` semantics.

### 3. Memory field compilers and recall
- **Task ID**: build-recall
- **Depends On**: build-engine
- **Validates**: tests/pg/test_pg_fields.py, tests/pg/test_pg_recall.py (create)
- **Informed By**: spike-2 (exact formulas), spike-4 (scope-keyed postings, exact vs HNSW, recall guard)
- **Assigned To**: pg-recall-builder
- **Agent Type**: builder
- **Parallel**: false
- Add compilers for `DecayingSortedField`, `ConfidenceField`, `BM25Field` (postings plus live per-scope stats), `EmbeddingField` (vector column, graceful `NULL`, `embedded_hash`), `ExistenceFilter` (membership table), and `AccessTrackerMixin` columns.
- Make `WriteFilterMixin` work on pg, with the priority tier as a no-op.
- Build `recall()` as one statement with optional arms, the D5 path choice, and the recall guard. Add `top_by_relevance`.
- Add the D7 backfill.

### 4. Dispatch seams
- **Task ID**: build-seams
- **Depends On**: build-recall
- **Validates**: tests/pg/test_pg_observation.py (create); the existing memory suites listed in §Test Impact (unchanged)
- **Assigned To**: pg-seams-builder
- **Agent Type**: builder
- **Parallel**: false
- Add the D8 branches in `context_assembler.py`, `observation.py`, `confidence_field.py`, `bm25_field.py`, `existence_filter.py` and `embedding_field.py`. Each uses a lazy import, and each pg path reads `Defaults` at call time.

### 5. Parity, concurrency, benchmark
- **Task ID**: build-parity
- **Depends On**: build-seams
- **Validates**: tests/pg/test_pg_parity.py, tests/pg/test_pg_concurrency.py (create), scripts/bench_pg_recall.py (create)
- **Informed By**: spike-4 (budget numbers, 0.0-recall pathology)
- **Assigned To**: pg-test-engineer
- **Agent Type**: test-engineer
- **Parallel**: false
- Run the Redis-oracle parity tests on DB 15. Any ad-hoc script sets `REDIS_URL=redis://localhost:6379/15` before importing popoto.
- Add multi-process save, outcome and DDL race tests.
- Run the 1536-d benchmark and pin `PG_VECTOR_EXACT_MAX`, `ef_search` and `PG_BACKFILL_BATCH` from the measurement.

### 6. Validate
- **Task ID**: validate-all
- **Depends On**: build-parity
- **Assigned To**: pg-validator
- **Agent Type**: validator
- **Parallel**: false
- Check every Success Criterion and Verification row, and report pass/fail with the environment stated.

### 7. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-all
- **Assigned To**: pg-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Complete the §Documentation items, bring the POC report to `main`, and tag the `poc/backend-seam` archive.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| pg tests | `POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres pytest tests/pg -q` | exit code 0 |
| Redis memory suites unchanged | `pytest tests/test_context_assembler.py tests/test_context_assembler_hybrid.py tests/test_observation_protocol.py tests/test_confidence_field.py tests/test_bm25_field.py tests/test_existence_filter.py tests/test_embedding_field.py -q` | exit code 0 |
| Full suite | `pytest -q` | exit code 0 |
| Lint | `ruff check src/` | exit code 0 |
| Format | `black --check src/ tests/` | exit code 0 |
| Types (ratchet) | `scripts/mypy_ratchet.py` | exit code 0 |
| Lock imports | `python scripts/check_lock_imports.py` | exit code 0 |
| No psycopg needed for import | `python -c "import sys; sys.modules['psycopg']=None; import popoto"` | exit code 0 |
| No POC seam on main (anti-criterion) | `test -d src/popoto/backends` | exit code 1 |
| No PL/pgSQL or triggers (anti-criterion) | `grep -rniE 'create (or replace )?(function\|trigger)' src/popoto/pg` | exit code 1 |
| Generic env var never read (anti-criterion) | `grep -rnE "['\"](POSTGRES_URL\|DATABASE_URL)['\"]" src/popoto/pg` | exit code 1 |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

Full-depth war room run on 2026-10-03, with three independent critics (sonnet): Risk & Robustness, Scope & Value, and History & Consistency. **Verdict before revision: NEEDS REVISION** (1 blocker, 7 concerns). The cited source lines were re-verified before revising. Every finding below has been applied to this plan.

| # | Severity | Critic | Finding | Resolution |
|---|---|---|---|---|
| 1 | BLOCKER | History & Consistency | D8 called `_post_effects` "unchanged Python", but it opens a Redis `batch()` and loops `on_read` per record (`context_assembler.py:2641-2646`). Pg recall would still have touched Redis. | D8 now has a pg branch before `batch()`, with bulk stage and suppress calls in one transaction. A new success criterion requires zero Redis calls on the pg path. |
| 2 | CONCERN | History & Consistency | `contradicted` and `used` (`observation.py:57`) had no pg semantics. | D8 now has a row for each of the five outcomes. `used` confirms only. |
| 3 | CONCERN | History & Consistency | The `definitely_missing` pre-check (`:2426`, `:2807`) was not dispatched. | Covered: it is `not might_exist` (`existence_filter.py:510`). A test asserts no false short-circuit. |
| 4 | CONCERN | History & Consistency, Risk & Robustness | The outage contract conflicted with `OUTAGE_ERRORS` re-raise, there was no zero-signal fallback, and the silent degrade had no health signal. | `UnavailableError` joins `OUTAGE_ERRORS`. Zero signal falls back to `top_by_relevance`. `popoto.pg.health()` logs ERROR once per outage window. |
| 5 | CONCERN | Risk & Robustness | `SET LOCAL enable_indexscan=off` is statement-wide, and the recall guard counted `NULL` embeddings. | The exact path now orders by `(embedding <=> $q) + 0`. The guard counts only non-`NULL` embeddings. A benchmark case and an `EXPLAIN` assertion were added. |
| 6 | CONCERN | Risk & Robustness | `save(update_fields=[project_key])` left postings in the old scope, and a `NULL` scope filter was unspecified. | Derived rows are rewritten when content or scope is in `update_fields`, with a test. Recall filters on `coalesce($pk,'')`. `migrate_key` raises. |
| 7 | CONCERN | Scope & Value | General machinery beyond Valor's needs: the public `transaction()`, `Meta.scope`, and the Int, Boolean and List compilers. | All three were removed. The supported fields are now exactly Valor's post-cutover declaration. |
| 8 | CONCERN | Scope & Value | Parity checked BM25 on a synthetic fixture only, not what the agent sees. | Added a success criterion on fused `assemble()` top-5 overlap (30+ cues) and on short-circuit agreement. |

## Open Questions

1. **One central Postgres, or one per machine?** The memory note on Yudame says "central shared Redis with optional scoping", and #756 asks the same question. It changes pool-sizing guidance, and whether `project_key` is enough of a scope or `agent_id` / machine must join it. The engine supports either. Only the deployment differs.
2. **The `{pk}:memory-gate:*` / `memory-distill:*` counters.** This plan leaves them in Redis as Valor app state, so Valor keeps a Redis dependency after cutover. Should they move to a Postgres counters table too? If so, is that table in popoto's schema or Valor's?
3. **The minimum Postgres version.** This plan says ≥ 16. The unscoped-BM25 fallback is only fast on 18, which has btree skip scan. Is 18 everywhere Valor runs, so the floor can be 18?
