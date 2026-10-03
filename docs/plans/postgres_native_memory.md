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
TBD

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
TBD

## Data Flow
TBD

## Architectural Impact
TBD

## Appetite
TBD

## Prerequisites
TBD

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
