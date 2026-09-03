# Agent memory production audit

Date: 2026-09-02. HEAD `35782e4`, version 1.8.2.
Method: static read of all of `src/`, `tests/`, `plugins/`, `docs/`, CI and
packaging, split across five review passes (core ORM, memory fields,
recipes, integrations, engineering health). Every finding below was
re-verified against the source before inclusion. Numbers come from the
environments stated with them. No tests were run for the correctness
findings; one pass ran the suite (Python 3.11, redis-py 8.1.0, Redis 7 on
DB 15: 3194 passed, 37 skipped, 3m04s).

## Verdict

The repo has two products sharing one package: a solid, well-tested Redis
ORM, and an agent-memory research programme layered on top of it. The
research programme is what is being shipped, and it has outgrown the
engineering underneath it.

- **About 40% of `src/` has no consumer outside tests and docs.** Of the
  ~11k lines in `recipes/` + `extraction/`, roughly 6k (AdaptiveAssembler,
  TelemetryRecorder, TrajectoryMemory, MemoryLifecycle, PolicyCache,
  graph_traversal, the auditable-extraction decision log) are reachable only
  through kwargs no shipped caller passes. Of the 17 advertised primitives,
  `DefaultMemory` uses 5; `examples/` uses 1; FrequencySketch and
  PredictionLedger have zero functional callers in `src/`.
- **The shipped per-turn path has the real problems, and they are not in the
  optional machinery.** Cross-agent memory leakage (#576, PR #593 open), an
  unpipelined per-candidate write loop on every retrieval, blanket exception
  swallowing that makes a Redis outage indistinguishable from "no relevant
  memories", and no eviction of any kind.
- **Three shipped behaviors are hazards for anyone who installs the
  package.** The pytest plugin auto-activates in every downstream project
  and flushes DB 15 of their `REDIS_URL` before each test. The harness hook
  defaults to DB 0 and blocks the user's prompt for ~25 seconds when Redis
  is unroutable. Opt-in Sentry reporting ships with `send_default_pii=True`
  and a hardcoded maintainer DSN.
- **Roadmap velocity exceeds hardening velocity.** Milestones M4 through M9
  (reference resolution, reconciliation, belief sheets, question queues,
  feedback loops, seeded audits) are planned while mypy sits at 1126 errors
  ungated, the CI matrix is one Python and one redis-py version, and the
  changelog is missing headings for seven tagged releases.

Recommendation: freeze M4 through M9. Spend the next cycle on the P0 list,
then delete or quarantine the unconsumed layers, then resume features on a
smaller surface.

## P0: fix before calling this production

1. **Cross-agent leak in the default retrieval path** (#576).
   `ContextAssembler._pull_path_hybrid` drops `filters` on the BM25 branch,
   so `agent_id` scoping does not hold on the path `DefaultMemory` selects.
   Confirmed end-to-end through the Claude Code hook. PR #593 exists; land
   it and add a cross-agent isolation test to the harness fixtures.

2. **pytest plugin flushes strangers' databases.**
   `pyproject.toml:113` registers a `pytest11` entry point;
   `pytest_plugin.py:235` is an `autouse=True` fixture that `flushdb()`s
   DB 15 of whatever `REDIS_URL` resolves to. Any project that depends on
   popoto and runs pytest gets this with no opt-in. Make it opt-in
   (`-p popoto`, or a separate `pytest-popoto` package).

3. **Harness hook stalls the user on Redis outage.**
   `integrations/config.py:257` sets a 5s connect timeout, and one
   `UserPromptSubmit` makes up to five independent connection attempts
   (injected-keys read, BM25, composite fallback, touch, failure counter).
   Measured 25.3s synchronous stall against an unroutable host. Neither
   `plugins/claude-code/hooks/hooks.json` nor the Codex equivalent sets a
   hook `timeout`. Fix: one connection attempt per hook invocation,
   sub-second connect timeout for the hook path, explicit hook timeout.

4. **DB 0 by default for the integration.** `config.py:42` still points at
   `redis://localhost:6379/0`; #584 is plan-only (the last two commits
   touched only `docs/plans/`). `popoto-memory demo` writes there and its
   cleanup leaves counter keys behind. Land #584.

5. **Outages are silent.** 179 `except Exception` sites in `src/`.
   `SubconsciousMemory.inject_context` returns an empty `AssemblyResult`
   on any exception (`subconscious_memory.py:381`); the four pull paths,
   post-effects, per-fact extraction, and outcome reporting all do the
   same. Let `ConnectionError`/`TimeoutError` propagate; swallow only
   retrieval-quality failures, and put the fallback reason in
   `AssemblyResult.metadata`.

6. **Query state lives on a class-level singleton.** `Model.query` is one
   object per class (`base.py:484`), and `filter_for_keys_set` writes
   ordering, limit and pushdown state onto `self`
   (`query.py:2255-2261`). A stress test (four threads, 6,000
   interleaved queries with different limits and orderings) did not
   produce wrong results, because limit and order are re-applied from
   each call's own kwargs. So this is a maintainability hazard rather
   than a demonstrated bug: any future read of that state after I/O
   becomes a race. Make per-call state a per-call object, but it is not
   a blocker.

7. **No eviction on the default path.** `DefaultMemory` has no TTL;
   `MemoryLifecycle` is never wired into the harness or `SubconsciousMemory`;
   `extract_memories` writes one record per sentence. Growth is linear in
   turns forever, plus BM25 posting lists, a confidence hash, and
   co-occurrence edges per record. Validity keys are documented as "closed,
   never deleted". Ship a default cap (TTL or `ZREMRANGEBYRANK` per agent
   partition on save).

8. **TTL leaves permanent index ghosts.** `Meta.ttl` applies `EXPIRE` only
   to the hash (`base.py:1487-1492`); class set, sorted, indexed and
   composite index keys never expire. After expiry, queries return orphans
   and log an error per query pointing at `repair_indexes()`, which does not
   exist (`query.py:2848`, `2866`; no definition anywhere). Either expire
   index members alongside the hash or declare TTL unsupported on indexed
   models.

9. **Credentials leak into model context.** `MemoryService.status()` returns
   `redis_url` verbatim; the MCP `memory_status` tool returns that dict as
   structured output and `doctor` prints it. A `redis://:password@host`
   URL lands in transcripts. Redact.

10. **Sentry with PII on.** `_error_reporting.py:118` sets
    `send_default_pii=True` against a hardcoded DSN (`:27`), and
    monkeypatches exception `__init__`s so every message (which embeds
    keys and field values) ships. It is opt-in, but it contradicts the
    README's "your memory data stays in your database". Set PII off and
    strip messages, or remove the module.

Every item above except 6 has a test in `tests/test_production_contracts.py`.
On main at `35782e4`, 13 of 15 were red (Python 3.11, redis-py 8.1.0, Redis
7.0.15 on DB 15). The branch that carries this document turns all 15 green;
the tests keep the `contract` marker so they can be run on their own with
`-m contract`. The `Meta.ttl` fix is partial by design: reads self-heal the
indexes derivable from the key, and `clean_indexes()` remains the tool for
partitions on non-key fields.

## The per-turn path, as it actually executes

Default `SubconsciousMemory(agent_id=...)` on `DefaultMemory`, lexical mode.

| Step | Redis work | Note |
|---|---|---|
| BM25 search | 1 EVAL | Whole last user message is the query; Lua walks the full posting list of every term, no term cap. A pasted 500-token tool output scales linearly. |
| Co-occurrence propagate | 1 EVAL | Seeded from top 5. |
| Fuse + load | 1 pipeline | RRF in Python over ~60 items, then HGETALL of 20. Fine. |
| Empty-hit fallback | 2 more composite EVALs | Silent, query-blind, fires every turn on a fresh store. |
| Access staging | 1 pipeline | RPUSH+EXPIRE per selected record; discarded after 24h unless `confirm_access` is called, which the harness never does. |
| Competitive suppression | 2 sequential RTs per losing candidate | `context_assembler.py:2507-2519` calls `update_confidence` per non-selected candidate; it does EXISTS then EVAL and ignores the pipeline kwarg (`confidence_field.py:541-546`). With 20 fetched and 10 selected, ~20 round trips before the LLM call. Scales with fetch width, not output. |
| Extraction | ~2-3 RTs per sentence, unbatched | Heuristic sentence splitter is the library default (`subconscious_memory.py:241`) even though the README reports raw ingestion beat it; only the harness picks `RawTurnExtractionProvider`. |
| Outcomes | ~5 RTs per record | Harness never calls `report_outcomes` per turn, so the only confidence signal written on the shipped path is negative (suppression). |

Every Lua script except one is sent with `eval` rather than a registered
`Script` (38 `.eval(` sites, 1 `register_script`). `DECAY_SCORE_LUA` is
7.3 KB and is uploaded on every `top_by_decay`. It also does
`ZRANGE 0 -1` over the whole partition and then per-member HGET/ZSCORE
calls inside the script, so decay ranking is O(N) Redis ops per query
inside one blocking EVAL.

## Complexity to remove

Estimates are lines in `src/` with no behavior change to the shipped path.

| Target | Lines | What |
|---|---|---|
| Unconsumed recipes | ~6,000 | AdaptiveAssembler, TelemetryRecorder/Analyzer, TrajectoryMemory, MemoryLifecycle, PolicyCache, graph_traversal, extraction decision_log/candidates/verdict. Move to `contrib/` or `examples/`, drop from `popoto/__init__.py`. |
| Docstring essays | ~1,300 | `models/migrations.py` is 1,294 lines and contains one AST node (a string). `base.py` and `query.py` are ~60% prose, much of it issue archaeology. Move cookbooks to `docs/`. |
| `save()` four-way copy | ~280 | `base.py:1275-1677` is partial/full × external/internal pipeline. Always write into a pipeline and execute if owned. |
| Sync/async duplication | ~350 | 12 `to_thread` wrappers plus a divergent async query core (`query.py:3187-3585`) that lacks Q objects, limit pushdown, and honors `no_track` differently. Pick one. |
| Legacy index-pointer compat | ~120 | `INDEX_SWAP_LUA` reads three generations of pointer keys and DELs two on every save; `on_delete` does three GETs. One-shot migration, then drop. |
| Primitives | ~2,500 | FrequencySketch (0 callers). PredictionLedger (0 callers; `auto_resolve` always early-returns). SupersessionProtocol (541-line facade over one `ValidityField` call). CyclicDecayField's Lua is a copy of the decay script minus the validity gate. ExistenceFilter's only use is answerable by a ZSCORE on the BM25 `df` key. |
| `utils/` | ~980 | Django auth backend and `multithreading` import Django inside a Redis ORM; zero tests, zero consumers; `sigfigs.py` references a finance module that does not exist. |
| Harness plugins | ~300 + 900 doc lines | Four plugins; only Claude Code verified live, Codex schema-only, Hermes and OpenClaw never run (`tests/fixtures/harness_payloads/README.md`). Hermes handler calls sync redis inside the gateway's event loop. Ship two. |
| Kill switches and inert constants | ~150 | Three deploy-level flags for default-on behaviors no shipped model triggers; 15 of ~40 constants self-annotate as "empirically inert". |
| Deprecated aliases and dead code | ~200 | `Query.keys(catchall/clean)` on `KEYS`, `sort_by` property pair, commented RedisGraph, `PopotoException` that never calls `super().__init__`. |

Assembler and facade knobs with zero callers outside tests:
`confidence_gate_threshold`, `confidence_gate_mode`,
`graph_traversal_relationship_fields`, `token_counter`,
`surfacing_threshold`, `propagation_depth`, `output_format="xml"|"natural"`,
`position="system"`, `tags`/`tag_match`, `auditable_extraction`,
`extraction_min_length`. `ContextAssembler.__init__` has 13 params,
`SubconsciousMemory.__init__` 15. There are ten entry points that assemble
context and two `report_outcomes` functions. Cut to one of each.

## Correctness bugs worth a ticket each

- `FieldBase.field_class_key` uses `name.strip('Field')`, which strips a
  character set, not a suffix (`field.py:128`). `FloatField` becomes
  `$oatF`, `UniqueField` becomes `$UniquF`, and `ModelField`/`MoField`
  collide. This is the on-disk index namespace, so it is now a frozen
  storage format with a collision hazard for any new field class.
- `SortedFieldMixin.get_filter_query_params` matches parameters by
  substring (`sorted_field_mixin.py:714`). The caller pre-filters kwargs
  by exact field membership, so this does not misroute today (verified:
  fields `at` and `created_at` filter independently). It is a latent
  trap for any new caller of the helper, not a bug.
- `Query.get()` by non-key field executes the query three times
  (`query.py:1906-1911`; `len`, `len`, `[0]` each re-run). Every
  `get_or_create` pays it.
- Indexed/unique fields run their Lua eagerly before the save pipeline
  (`base.py:1572-1585`); a pipeline failure leaves a hash holding only the
  indexed field plus an index pointing at it. `clean_indexes` was later
  taught to detect exactly this.
- Unique checks are read-then-write (`base.py:1049-1100`);
  `get_or_create` recovers by substring-matching exception text.
- Decay uses client `time.time()` as ARGV; a client behind the writer's
  clock gets a negative age clamped to 0.01 days, a 1.58x boost for
  future-stamped records. Use `redis.call('TIME')`.
- `AutoKeyField` assigns its value by mutating the shared Field's
  `default` (`auto_field_mixin.py:313`) from `Model.__init__`; two
  concurrent inits can hand out one UUID.
- Cycle amplitude and confidence partition moves are HGET-then-HSET
  outside any transaction. Eight `pipeline=` parameters are accepted and
  ignored, so the documented "every operation is pipeline-safe" claim is
  false.
- `ContentField.garbage_collect` returns 0 and `on_delete` is a no-op;
  content files leak forever. Embedding index is a single JSON rewritten
  in full per save with no lock; concurrent workers lose entries.
- `SupersessionProtocol.supersede` silently no-ops when the successor is
  in the same pipeline (#588).

## Engineering health

- **mypy**: 1126 errors in 67 files with a strict `[mypy]` block in
  `setup.cfg` nothing enforces (#506). Gate at baseline or delete the
  config.
- **CI matrix**: Python 3.12 only, redis-py resolved unlocked (8.x) while
  `uv.lock` pins 7.1.1 and `dependencies` claims `>=4.4.4`. Add 3.10 and
  3.13 legs and a `redis<8` leg; raise the floor to something tested.
- **Benchmarks inside tests**: `tests/benchmarks/` is 478 collected tests
  and 48 MB of committed result JSON, in the default suite. Move to
  `benchmarks/`, mark ratchet/split slow, stop committing multi-MB
  results.
- **Sleep-driven tests**: 88 sleep calls (~12s total) to advance
  `time.time()`. Inject a clock.
- **Test coupling**: 260 references to `._meta`, 53 asserts on private
  methods. Any refactor of `ModelMeta` breaks ~100 tests.
- **Docs**: `docs/sdlc/` (internal agent-workflow instructions) is
  published as orphan pages on popoto.io; add to `exclude_docs`.
  `.claude/commands/prime-agent-memory.md` references three files that no
  longer exist. `.readthedocs.yaml` installs a `docs/requirements.txt`
  that does not exist. CHANGELOG has no headings for 1.8.0, 1.8.1, 1.7.0,
  1.6.x, 1.4.x.
- **Release process**: `do-deploy` step 4 and `release.md` both say to bump
  `setup.cfg`, which has no version field. Two overlapping procedures;
  `.claude-plugin/marketplace.json` carries its own version bumped by hand.
- **Config sprawl**: 21 env vars across 8 modules, two unprefixed (`ENV`,
  `BEGINNING_OF_TIME`), `ENV` undocumented, `POPOTO_CONTENT_PATH` parsed
  in two places, `POPOTO_MEMORY_URL` and `REDIS_URL` bind through different
  mechanisms. CLAUDE.md has to warn that `POPOTO_REDIS_DB` is not an env
  var. One `popoto.config` module with one table.
- **Observability**: 229 logger calls across five inconsistent logger
  names (`popoto`, `POPOTO.field`, `POPOTO-REDIS_DB`, ...), no
  `NullHandler`, no structured fields, no metrics or callback hook. The
  only "what did memory do" surface is the unwired telemetry recipe.
  Standardize on `logging.getLogger(__name__)` and add an event callback.
- **Import cost**: `import popoto` is 237 ms (27 ms bare interpreter),
  ~104 ms of it `redis.asyncio`, pulled eagerly by the hook that never
  uses it. Half the hook's 400 ms p95 budget is imports.

## Suggested order

1. Land #593 (leak) and #584 (DB 0). Add hook timeout and single-attempt
   connect. Redact `status()`. One week.
2. pytest plugin opt-in. Sentry PII off. Propagate connection errors from
   the recipe layer. Default eviction cap on `DefaultMemory`.
3. Batch the suppression loop into one Lua over all candidates, or drop
   suppression from the read path. Register Lua scripts once. Cap BM25
   query terms. Batch extraction saves into one pipeline per turn.
4. Move unconsumed recipes and primitives out of the package namespace.
   Delete `utils/`, Hermes and OpenClaw plugins, docstring-only modules.
5. Per-call query state. Collapse `save()`. Decide sync-or-async core.
6. Gate mypy at baseline; add the CI matrix; move benchmarks out of tests.
7. Then reopen M4 through M9 against a surface a third the current size.
