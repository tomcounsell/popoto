---
status: Planning
type: feature
appetite: Large
owner: tomcounsell
created: 2026-10-03
tracking: https://github.com/tomcounsell/popoto/issues/755
last_comment_id: 5964094115
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
  - **Informs D5:** an exact scan within the scope comes first, and HNSW is added only past a measured threshold (spike-4).
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
- **Finding**: _pending — filled in when the spike returns._
- **Confidence**: _pending_
- **Impact on plan**: decides D4 (BM25 storage and stats) and D5 (HNSW threshold).

## Data Flow

All three flows below are for a model bound to Postgres by its base class, `popoto.pg.Model`.

**Write: `Memory.safe_save(content=…, project_key=…, importance=…)`**
1. `Model.save()` → `WriteFilterMixin` gate in Python. `compute_filter_score()` below `_wf_min_threshold` returns `False`, and nothing is written. This step is unchanged.
2. The engine encodes typed values from the field declarations. NUL bytes are refused at the field, before any SQL.
3. The engine calls the embedding provider for `EmbeddingField` *outside* the transaction. On failure or timeout the vector is `NULL` and the save proceeds: the graceful behaviour becomes the default.
4. One transaction, all rows locked in a fixed global order:
   1. `INSERT … ON CONFLICT (pk) DO UPDATE` on the model row. This writes every typed column. Decay `relevance` is stamped on insert and on full save, matching today's `auto_now`. Confidence columns are set only on insert. `bm25_len`, `created_at` and `updated_at` are set too.
   2. BM25 postings: delete the old terms for this row, then insert the new ones, sorted by term.
   3. ExistenceFilter vocabulary: refcount upserts, sorted by token.
5. After commit, piggyback maintenance (§Solution D7) may run one bounded batch: embed a few `NULL`-vector rows and reap expired rows. It never runs inside the caller's transaction.

**Recall: `ContextAssembler(Memory).assemble(query_cues={"query": q}, partition_filters={"project_key": pk})`**
1. `_pull_path_hybrid` sees a pg-bound model and calls `Memory.query.recall(q, scope={"project_key": pk}, limit=5*max_items, weights=…)`.
2. `recall` embeds `q` (`input_type="query"`) and issues **one SQL statement** with three CTEs: BM25 top-k over postings joined to the scope, vector top-k by `<=>` within the scope, and RRF `Σ w/(60+rank)`. It returns typed `(instance, rrf_score)` pairs with full rows, so there is no second hydrate round trip.
3. The assembler's model-agnostic Python is unchanged: dedup, superseded filter, token budget, FOK score, and formatting.
4. `_post_effects`: staged reads become one `UPDATE … SET staged_reads = staged_reads + 1, staged_at = now()` over the selected keys. Competitive suppression is one `UPDATE` applying the confidence expression with signal 0.3 to the unselected keys. Both run in PK order in one transaction.

**Outcome: `ObservationProtocol.on_context_used(memories, {key: outcome})`**
1. `acted` keys, in one statement: set `relevance = now()`, `access_count += staged_reads` (when staged within 24 h), `last_accessed_at = now()`, `staged_reads = 0`, and apply the confidence expression with signal 0.9 (see the D2 note on `Defaults`).
2. `dismissed` / `deferred` keys: `staged_reads = 0`.
3. Everything is one transaction, with rows locked in PK order.

## Architectural Impact

- **New dependencies**: `psycopg[binary,pool]>=3.2` and `pgvector` (the Python adapter), both behind a `postgres` extra. On the server: PostgreSQL ≥ 16 with the `vector` extension ≥ 0.8. No `pg_search`, `pg_cron`, `bloom` or PL/pgSQL functions.
- **Interface changes**: additive. The new `popoto.pg` package is opt-in by base class. The existing `popoto.Model`, every Redis field, and every Redis wire command are **unchanged**.
  - Five static entry points Valor calls gain a one-line early dispatch to the pg engine when the model is pg-bound: `ObservationProtocol.on_read` / `on_context_used`, `ConfidenceField.update_confidence` / `get_confidence`, `BM25Field.search`, `ExistenceFilter.might_exist`, `EmbeddingField.garbage_collect`.
  - `ContextAssembler` gains one pg branch in `_pull_path_hybrid` and `assess`.
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
TBD

## Failure Path Test Strategy
TBD

## Test Impact
TBD

## Rabbit Holes
TBD

## Risks
TBD

## Race Conditions
TBD

## No-Gos (Out of Scope)
TBD

## Update System
TBD

## Agent Integration
TBD

## Documentation
TBD

## Success Criteria
TBD

## Team Orchestration
TBD

## Step by Step Tasks
TBD

## Verification
TBD

## Critique Results
TBD

## Open Questions
TBD
