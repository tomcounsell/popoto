---
status: In Progress
type: feature
appetite: Medium
owner: valorengels
created: 2026-08-06
tracking: https://github.com/tomcounsell/popoto/issues/492
last_comment_id:
revision_applied: true
revision_applied_at: 2026-08-06T00:00:00Z
---

# TagField — Optional Multi-Value Scoping for a Central Shared Redis/Valkey

## Problem

A confirmed project goal (2026-07-22) is a **centrally hosted Redis/Valkey serving
many agents**, where a memory may be scoped by the agent it belongs to, a relevant
project, or arbitrary tags — and **every scoping dimension is optional**. Popoto's
only scoping primitive today is KeyField partitioning, which is the opposite of
optional: once declared, the partition value is part of the record's identity and
required at query time.

**Current behavior:**
- **KeyField partitioning is mandatory-once-declared.** KeyField values form the
  Redis key; you cannot sometimes scope and sometimes not, and a record cannot
  belong to several scopes at once.
- **`SetField` is unqueryable** — it msgpack-serializes a Python `set` into the
  model hash (`shortcuts.py:563`), maintains no Redis Set, and cannot be filtered.
- **`Query.filter()` has no tag arm** — AND across fields via client-side
  `set.intersection` (`query.py:2030`), OR within one field via `__in`
  (`key_field_mixin.py:430`), but no multi-value membership filter.
- **`ContextAssembler.assemble()` accepts only partition equality** (`agent_id`
  + `partition_filters` dict, `context_assembler.py:1480-1482`).
- **No `TagField` exists** anywhere in `src/` (verified by grep).

**Desired outcome:** models can declare an optional tag-style field; records with
zero tags transparently live in the shared pool; queries and context assembly can
filter by any combination of tag values (contains / any-of / all-of) or none —
without schema-level commitment to which dimensions exist. All set operations are
plain Redis/Valkey commands (no modules). Backward compatible: existing models are
untouched.

## Freshness Check

**Baseline commit:** 8699eb9 (`git rev-parse HEAD` at plan time)
**Issue filed at:** 2026-07-22T04:33:29Z
**Disposition:** Minor drift (line numbers moved; every claim still holds; the one
cited prerequisite, #476, has since *shipped* — favorable).

**File:line references re-verified:**
- `src/popoto/fields/shortcuts.py:563` — SetField msgpack-into-hash, unqueryable — **still holds** (SetField at 563-590, trivial `type=set` wrapper, no hooks).
- `src/popoto/models/query.py:371` (partitions mandatory) — claim holds; the AND-intersection engine is now `filter_for_keys_set` at `query.py:1886-2036` (client-side `set.intersection` at `:2030`, not Redis SINTER; a `# todo: use redis.SINTER` marker sits at `:1936`).
- `src/popoto/recipes/context_assembler.py:1254-1256` — the `partition_filters`/`agent_id` handling **drifted** into `assemble()` at `context_assembler.py:1480-1482`; the `filters` dict is threaded into every retrieval mode via `query.filter(**filters)` (`:1802, :1854, :1921, :1994, :2020, :2231`). Claim holds.
- `src/popoto/fields/key_field_mixin.py` per-value Set index — confirmed at `:185-310` (`SADD`/`SREM` of `db_key.redis_key` into `$KeyF:Model:field:value`).

**Cited sibling issues/PRs re-checked:**
- #476 (1.8.0 index-pointer forward-incompat) — **RESOLVED in 1.8.1 (PR #477)**. The atomic index path now lives in `indexed_field_mixin.py::INDEX_SWAP_LUA` with a server-authoritative pointer **side key** (`\x00idxptr\x00`), not an in-hash field. This is the path TagField must ride. The issue named #476 as a prerequisite for *production central hosting*, not for building this field — and it has now shipped.

**Commits on main since issue was filed (touching referenced files):**
- `fdcc901` (#491 confidence-modulated decay) — touched context_assembler; irrelevant to tag scoping (additive gate).
- `8699eb9` (#457 hybrid fusion) — touched assembler pull paths; the `query.filter(**filters)` seam this plan uses is preserved in the hybrid path (`:1921`).
- 1.8.1 release (#477/#500) — established the INDEX_SWAP_LUA side-key path TagField extends.

**Active plans in `docs/plans/` overlapping this area:** none. (Nearest neighbors — `weighted_query_adaptive_hybrid_fusion.md`, `vector_retrieval_mode.md` — touch assembler ranking, not scoping/filtering.)

## Prior Art

- **IndexedFieldMixin (`indexed_field_mixin.py`)** — the direct template. Secondary
  Set index for a *single-valued* non-key field, maintained atomically via
  `INDEX_SWAP_LUA` with a pointer side key (#476/#477). TagField is the
  *multi-valued* generalization: one record belongs to N value-Sets at once.
- **KeyFieldMixin `__in` (`key_field_mixin.py:430`)** — server-side `SUNION` over
  per-value Sets is exactly the OR primitive `tags__any` needs.
- **PR #477 / issue #476** — the atomic Lua index path + side-key pointer scheme
  this field extends to multi-value.
- No prior `TagField` attempt exists (grep-confirmed). No failed prior fixes.

## Research

No relevant external findings — this is entirely internal to popoto's ORM and
Redis command surface. Redis Set commands used (`SADD`, `SREM`, `SMEMBERS`,
`SUNION`, `SINTER`, `SDIFF`, `DEL`) are all core commands present in both Redis
and Valkey (verified against the project's Valkey-parity constraint — no `BF.*`,
`CMS.*`, `FT.*`, `JSON.*`, or any module command).

## Spike Results

### spike-1: Colon-in-tag-value index-key collision
- **Assumption**: "Convention tag values like `agent:valor` will collide with the `:` DB_key separator and corrupt index Set key names."
- **Method**: code-read + prototype (ran `DB_key.clean` and `DB_key(...).redis_key` in the worktree venv).
- **Finding**: `DB_key.clean("agent:valor")` → `"agent{&#58;}valor"`; the index key becomes `$TagF:Model:tags:agent{&#58;}valor` — **no collision**. Bare tags (`urgent`) and `None` also render safely. The `$TagF` prefix is auto-derived by the `FieldBase` metaclass: `'TagField'.strip('Field')` → `'Tag'` → `field_class_key = "$TagF"`.
- **Confidence**: high.
- **Impact on plan**: Convention-over-schema prefixes (`agent:`, `project:`) are safe with zero extra escaping. Build reuses `DB_key(prefix, tag).redis_key` for every per-tag Set key; the Lua receives **fully-constructed set keys from Python**, so it never needs to reimplement `DB_key.clean` colon-escaping.

### spike-2: Riding the atomic index path without base.py surgery
- **Assumption**: "A multi-value tag field can hook the existing eager-atomic-index machinery in `base.py` without modifying save()/delete()."
- **Method**: code-read (`base.py:1130-1294` save paths; `:1719-1737` delete path).
- **Finding**: The eager-atomic loop and the plain-HSET exclusion both key off `isinstance(field, IndexedFieldMixin)` (`base.py:1140, 1215`). If `TagFieldMixin` **subclasses `IndexedFieldMixin`**, TagField is automatically (a) excluded from the plain HSET mapping and (b) run eagerly with its own atomic `EVAL` (`pipeline=None`) before the internal pipeline — same #476 unique-conflict-window fix. `on_delete` for every field already runs *before* the hash `DELETE` (`:1719-1737`), so the pointer side key is still readable at delete time.
- **Confidence**: high.
- **Impact on plan**: **Zero `base.py` changes.** `TagFieldMixin(IndexedFieldMixin)` overrides `on_save`/`on_delete`/`filter_query`/`get_filter_query_params` entirely; because its Lua writes the hash field bytes itself (HSET inside `TAG_SWAP_LUA`), the plain-HSET exclusion is exactly what we want.

## Data Flow

**Write path (`instance.save()`):**
1. `Model.save()` iterates fields; TagField is `isinstance IndexedFieldMixin`, so it is excluded from the plain HSET mapping and run eagerly.
2. `TagFieldMixin.on_save()` normalizes the tag value → sorted unique `list[str]`, computes the set of per-tag index-Set keys via `DB_key(prefix, tag).redis_key`, msgpack-packs the list for the hash field, and runs **`TAG_SWAP_LUA`** (internal path: `POPOTO_REDIS_DB.eval`; external path: queued into caller pipeline).
3. `TAG_SWAP_LUA` atomically: reads the record's current tag-set membership from the **pointer side key** (a Redis SET of full index-Set keys), diffs old vs new → `SREM` member from removed Sets, `SADD` member to added Sets, resets the pointer set, and `HSET`s the packed list into the model hash. Single atomic server-side script → no orphans, no cross-process race.

**Delete path (`instance.delete()`):**
1. `Model.delete()` runs `field.on_delete()` for every field **before** the hash `DELETE` (`base.py:1719-1737`).
2. `TagFieldMixin.on_delete()` `SMEMBERS` the pointer side key → `SREM`s the member from each named Set → `DEL`s the pointer side key (all queued in the delete pipeline).

**Query path (`Model.query.filter(tags__all=[...])`):**
1. `QueryBuilder.filter_for_keys_set` routes tag params to `TagFieldMixin.filter_query` via `filter_query_params_by_field` (`query.py:1977-1991`).
2. `filter_query`: `__contains` → `SMEMBERS(one set)`; `__any` → `SUNION(set keys)`; `__all` → `SINTER(set keys)`. Returns a key-set that is AND-intersected client-side with other filters (`query.py:2030`).
3. Absent tag param → TagField's `filter_query` is never invoked → results are unscoped (shared pool, including untagged records).

**Assembler path (`ContextAssembler.assemble(tags=..., tag_match=...)`):**
1. `assemble()` auto-detects `_tag_field_name` (a TagField on the model). If `tags` given and `Defaults.TAG_SCOPING_ENABLED`, injects `filters[f"{name}__{all|any}"] = list(tags)`.
2. The existing `query.filter(**filters)` seam applies it uniformly across composite / hybrid / lexical / push paths. Omitting `tags` leaves `filters` bit-identical to today → current behavior preserved exactly.

## Architectural Impact

- **New dependencies:** none (core Redis commands only).
- **Interface changes:** additive only — new `TagField` export, new `tags`/`tag_match` kwargs on `ContextAssembler.assemble()` and `.assess()` (both default `None`, back-compat), new `Defaults.TAG_SCOPING_ENABLED` constant.
- **Coupling:** TagField subclasses `IndexedFieldMixin` (reuses its metaclass-derived key prefix and eager-index integration) but overrides all behavior; no coupling added to `base.py`.
- **Data ownership:** TagField owns per-tag Redis Sets (`$TagF:Model:field:value`) and one pointer side key per record (`{redis_key}\x00tagptr\x00{field}`). Tags are metadata, never part of record identity.
- **Reversibility:** high — additive; removing a TagField from a model orphans only its `$TagF:*` Sets (cleaned lazily like other index Sets).

## Appetite

**Size:** Medium

**Team:** Solo dev (single executor), PM (team lead) for the AND-vs-OR default decision, code reviewer (PR gate).

**Interactions:**
- PM check-ins: 1-2 (confirm assembler default `tag_match` semantics; report at PR)
- Review rounds: 1 (do-pr-review gate before merge)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -n 10 ping` | Index Sets + tests (isolated DB 10) |
| Worktree editable install | `.venv/bin/python -c "import popoto; assert '.claude/worktrees' in popoto.__file__"` | Tests exercise THIS checkout, not site-packages |
| Full extras installed | `.venv/bin/python -c "import numpy, sentence_transformers"` | Avoid ~95 silently-deselected tests |

## Solution

### Key Elements

- **`TagFieldMixin(IndexedFieldMixin)`** (`src/popoto/fields/tag_field.py`): multi-value Set-index field. Stores a normalized `list[str]` in the model hash and maintains one Redis Set per tag value.
- **`TAG_SWAP_LUA`**: atomic multi-value index diff — removes membership from dropped tags, adds to new tags, resets the pointer side key, writes hash bytes; single server-side script.
- **Pointer side key** (`{redis_key}\x00tagptr\x00{field}`): a Redis SET holding the full index-Set keys the record currently belongs to — the server-authoritative source of truth for diffing on re-save (mirrors #476's side-key pointer, generalized to a set).
- **`TagField(TagFieldMixin, Field)`** shortcut (`shortcuts.py`) + export in `src/popoto/__init__.py`.
- **Query lookups**: `tags__contains="agent:valor"` (membership), `tags__any=[...]` (SUNION / OR), `tags__all=[...]` (SINTER / AND).
- **`ContextAssembler`**: auto-detected `_tag_field_name`; new `tags` + `tag_match` kwargs on `assemble()` and `assess()`; deploy kill switch `Defaults.TAG_SCOPING_ENABLED`.
- **RLT micro-benchmark**: measures tag-filtered vs unfiltered assemble latency; asserts bounded overhead.

### Flow

Declare `tags = TagField()` on a model → `m = Model(...); m.tags = ["agent:valor", "project:popoto"]; m.save()` (per-tag Sets populated atomically) → `Model.query.filter(tags__all=["agent:valor"])` returns scoped records → `Model(...).save()` with no tags → lands in shared pool, returned by unscoped queries → `assembler.assemble(query_cues=..., tags=["agent:valor"])` scopes retrieval across all modes; `assemble(query_cues=...)` (no tags) behaves exactly as today.

### Technical Approach

- **Field type**: accept `list | set | tuple` of hashable scalars; normalize to `sorted(set(map(str, value)))` for deterministic serialization and stable index keys. `None`/empty → `[]` (untagged; shared pool). Validation rejects non-iterable/non-scalar-element inputs.
- **Subclass `IndexedFieldMixin`** for the `isinstance` integration (eager-atomic loop + HSET exclusion), but fully override the four hook methods. `get_filter_query_params` returns **only** `{field}__contains|__any|__all` (NOT the inherited single-value exact/`__in`/`__startswith` params — exact-match on a list is meaningless).
- **`TAG_SWAP_LUA` contract** (all Set/index keys are pre-built by Python, colon-safe per spike-1):
  - `KEYS[1]` = model hash key; `KEYS[2]` = pointer side key.
  - `ARGV[1]` = field name; `ARGV[2]` = member key (record redis_key); `ARGV[3]` = msgpack new-list bytes; `ARGV[4]` = count of new set keys; `ARGV[5..]` = the new per-tag index-Set keys.
  - Logic: `old = SMEMBERS(ptr)`; `new = {ARGV[5..]}`; `SREM member` from `old\new`; `SADD member` to `new\old`; `DEL ptr` then `SADD ptr new...` (skip if empty); `HSET model_key field new_bytes`; return 1. Idempotent re-save is naturally a no-op (empty diffs, HSET rewrites identical bytes).
- **`on_delete`**: `SMEMBERS(ptr)` → `SREM member` per set → `DEL ptr`, queued in the delete pipeline (runs before hash DELETE).
- **`filter_query`**: build set keys with `DB_key(prefix, tag).redis_key`; `SMEMBERS`/`SUNION`/`SINTER` accordingly; multiple tag params AND-intersect client-side (consistent with the engine).
- **ContextAssembler**: in `__init__`, detect `self._tag_field_name = next((n for n,f in model_class._meta.fields.items() if isinstance(f, TagFieldMixin)), None)`. In `assemble`/`assess`, after building `filters`: `if tags and self._tag_field_name and Defaults.TAG_SCOPING_ENABLED: filters[f"{self._tag_field_name}__{'all' if tag_match=='all' else 'any'}"] = list(tags)`. No change to any `_pull_path_*` body — the seam is the shared `filters` dict.
- **Kill switch**: `Defaults.TAG_SCOPING_ENABLED = True` in `constants.py`. When `False`, the assembler skips tag auto-application (subconscious retrieval-time scoping is disabled deploy-wide without editing model code — the PyPI-adopter escape hatch). Index maintenance stays on for correctness; direct `filter(tags__all=...)` still works (explicit query, not subconscious).

## Failure Path Test Strategy

### Exception Handling Coverage
- The internal `on_save` path maps a Lua error to `ModelException` (mirroring IndexedFieldMixin); TagField has no uniqueness check, so the only failure is a Redis/Lua error, which propagates. Test: a malformed tag element (non-scalar) raises at `is_valid`/normalize time, asserted with `pytest.raises(ModelException)`. No `except Exception: pass` blocks introduced.

### Empty/Invalid Input Handling
- `tags = None`, `tags = []`, `tags = set()` → record saved as untagged, appears in unscoped queries, membership Sets untouched. Explicit tests.
- `filter(tags__any=[])` / `filter(tags__all=[])` → empty set-key list; assert well-defined result (empty match, no crash — mirror KeyField's empty-`__in` guard at `:436-439`).
- Whitespace/duplicate tags → normalized (dedup); `["a","a"]` indexes once. Test.

### Error State Rendering
- No user-visible rendering surface. Assembler with `tags` but no TagField on the model → tags ignored (no crash), documented; test asserts identical result to omitting `tags`.

## Test Impact

No existing tests affected — purely additive (new field type, new optional kwargs
that default to today's behavior). New test file `tests/test_tag_field.py` (mirrors
`tests/test_indexed_fields.py`) plus assembler-integration cases in the existing
context-assembler test module.

## Rabbit Holes

- **Reimplementing `DB_key.clean` colon-escaping inside Lua** — avoided: Python
  passes fully-built set keys to the script (spike-1).
- **`tags__isnull` / "untagged-only" query** — would require an all-members-minus-union
  SDIFF or a dedicated "untagged" Set. Not in the acceptance criteria; untagged
  records are reachable via unscoped queries. Out of scope (see No-Gos).
- **Making KeyField partitions optional** — explicitly rejected in the issue's
  recon ("Revised: 1"); partition identity semantics are load-bearing. Do not touch.
- **Tag-based access control / permissions** — tags are cooperative scoping between
  trusted agents, NOT a security boundary. Docs must say so; no enforcement code.
- **Server-side Redis SINTER for the whole filter pipeline** — the engine currently
  intersects client-side (`query.py:2030`); do not refactor that here. `tags__all`
  uses SINTER *within* the tag field only.

## Risks

### Risk 1: Multi-value diff leaves orphaned Set members on re-save
**Impact:** Stale keys returned by tag queries after a record's tags change.
**Mitigation:** The pointer side key is the server-authoritative old-membership
source (never a stale in-memory snapshot); `TAG_SWAP_LUA` diffs old vs new inside
one atomic script. Dedicated test: save `["a","b"]` → re-save `["b","c"]` → assert
`a`-Set no longer contains the member, `c`-Set does, `b`-Set unchanged.

### Risk 2: Concurrent writers race on the same record's tag membership
**Impact:** Interleaved SADD/SREM could corrupt membership.
**Mitigation:** All mutations happen inside a single `EVAL` (atomic on the Redis
server) plus the eager-atomic-index path (`pipeline=None`) that #476/#477
established. Mirror `test_concurrent_index_integrity.py` for a tag variant.

### Risk 3: Assembler `tags` kwarg silently changes behavior for existing callers
**Impact:** Regression in current retrieval results.
**Mitigation:** `tags` defaults to `None`; when `None`, `filters` is byte-identical
to today. Test asserts `assemble(query_cues=X)` == `assemble(query_cues=X, tags=None)`.

## Race Conditions

### Race 1: tag re-save membership diff
**Location:** `tag_field.py::TAG_SWAP_LUA` / `on_save`
**Trigger:** Two saves of the same record with different tag lists.
**Data prerequisite:** Pointer side key reflects the last committed membership.
**State prerequisite:** Diff (SREM removed / SADD added) is computed against
server state, not a client snapshot.
**Mitigation:** Single atomic `EVAL`; pointer read + diff + writes are one script.
Eager path uses `pipeline=None` so it commits before the outer pipeline builds
(same window fix as #476).

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #492] `tags__isnull` / untagged-only filtering — not in the
  acceptance criteria; untagged records are reachable via unscoped queries.
  Tracked under the parent issue #492 for a possible follow-up.
- Making KeyField partitioning optional — rejected in issue recon (identity
  semantics are load-bearing). This is a design decision, permanently out of scope.
- Tag-based access control / permission enforcement — tags are cooperative scoping,
  not a security boundary; documented as such, no code.

## Update System

No update-system changes required — this is a library feature. No new config files
or migration steps for existing installations (existing models without a TagField
are untouched; the field is opt-in).

## Agent Integration

No agent/MCP integration required in this repo — TagField is an ORM primitive. The
downstream consumer is the Valor `ai` repo (`models/memory.py`), which will later
migrate `agent_id`/`metadata.tags` onto a TagField alongside its existing
`project_key` partition (the two mechanisms coexist). That migration is out of
scope for this popoto-side plan.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/agent-memory.md` — add TagField to the primitives
  overview: API (contains/any/all), convention-over-schema (`agent:`/`project:`
  prefixes, popoto stays agnostic), the **not-a-security-boundary** caveat, and
  the shared-pool semantics for untagged records.
- [ ] Add a short section to `docs/guides/agent-memory-quickstart.md` showing
  optional scoping for a central-hosting deployment.

### External Documentation Site
- [ ] `mkdocs build` passes (docs are part of the site).

### Inline Documentation
- [ ] Module + class docstrings on `tag_field.py` (mirror IndexedFieldMixin's depth,
  including the `TAG_SWAP_LUA` contract comment).
- [ ] Docstring updates on `ContextAssembler.assemble()`/`.assess()` for `tags`/`tag_match`.

## Success Criteria

- [ ] A model can declare `tags = TagField()`; saving with zero tags is valid and such records appear in unscoped queries.
- [ ] `filter(tags__contains=...)`, `filter(tags__any=[...])`, `filter(tags__all=[...])` work and compose with other `filter()` conditions; all ops are plain Redis/Valkey commands (grep shows no `BF.`/`CMS.`/`FT.`/`JSON.`).
- [ ] `ContextAssembler.assemble()` accepts `tags`/`tag_match` in all retrieval modes; omitting them yields byte-identical results to current behavior.
- [ ] Tag indexes maintained atomically on save/delete; re-save and delete leave no orphaned Set members (dedicated tests).
- [ ] Docs state convention-over-schema and not-a-security-boundary.
- [ ] RLT micro-benchmark reports bounded tag-filtered assembly overhead.
- [ ] `black --check src/ tests/` clean; `mypy src/` no new errors vs base.
- [ ] Tests pass on isolated DB (`POPOTO_TEST_DB=10`), redis-py version stated.

## Step by Step Tasks

### 1. TagField core (field + Lua + index maintenance)
- **Task ID**: build-tagfield
- **Depends On**: none
- **Validates**: tests/test_tag_field.py (create)
- **Informed By**: spike-1 (colon-safe keys), spike-2 (subclass IndexedFieldMixin, no base.py changes)
- **Assigned To**: solo dev
- **Parallel**: false
- Create `src/popoto/fields/tag_field.py`: `TagFieldMixin(IndexedFieldMixin)`, `TAG_SWAP_LUA`, normalize helper, `on_save`/`on_delete`/`filter_query`/`get_filter_query_params`.
- Add `TagField(TagFieldMixin, Field)` shortcut to `shortcuts.py`; export in `src/popoto/__init__.py` (`__all__` + import block).
- Add `Defaults.TAG_SCOPING_ENABLED = True` to `constants.py` with docstring.

### 2. Query filtering + assembler integration
- **Task ID**: build-query-assembler
- **Depends On**: build-tagfield
- **Validates**: tests/test_tag_field.py, context-assembler test module
- **Assigned To**: solo dev
- **Parallel**: false
- Verify `filter_query_params_by_field` auto-registers tag params (no query.py change expected; confirm by test).
- Add auto-detected `_tag_field_name` + `tags`/`tag_match` to `ContextAssembler.assemble()` and `.assess()`; gate on `Defaults.TAG_SCOPING_ENABLED`.

### 3. Tests (field, indexing, query, assembler, concurrency, benchmark)
- **Task ID**: build-tests
- **Depends On**: build-tagfield, build-query-assembler
- **Assigned To**: solo dev / test-engineer
- **Parallel**: false
- `tests/test_tag_field.py`: create/index, delete cleanup, re-save diff (no orphans), untagged→shared-pool, contains/any/all, compose-with-KeyField, empty-list guards, normalize/dedup, invalid input raises, kill-switch off.
- Assembler tests: tags across composite/hybrid/lexical; `tags=None` == today; `tags` with no TagField ignored.
- RLT micro-benchmark asserting bounded overhead.

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: build-tests
- **Assigned To**: documentarian
- **Parallel**: false
- Update `docs/features/agent-memory.md` + quickstart; verify `mkdocs build`.

### 5. Final validation
- **Task ID**: validate-all
- **Depends On**: all above
- **Assigned To**: solo dev
- **Parallel**: false
- `POPOTO_TEST_DB=10 pytest tests/test_tag_field.py <assembler tests>`; `black --check`; `mypy src/` base-vs-branch; state redis-py version.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| TagField tests pass | `POPOTO_TEST_DB=10 .venv/bin/python -m pytest tests/test_tag_field.py -q` | exit code 0 |
| Format clean | `.venv/bin/python -m black --check src/popoto/fields/tag_field.py tests/test_tag_field.py` | exit code 0 |
| No Redis modules used | `grep -rEc 'BF\.|CMS\.|FT\.|JSON\.|TOPK\.|TDIGEST\.' src/popoto/fields/tag_field.py` | match count == 0 |
| TagField exported | `.venv/bin/python -c "from popoto import TagField; print(TagField.__name__)"` | output contains TagField |
| Kill switch present | `grep -c 'TAG_SCOPING_ENABLED' src/popoto/fields/constants.py` | output > 0 |

## Critique Results

Verdict: **READY TO BUILD (with concerns)** — 0 blockers, 5 concerns, 3 nits. All
resolved in the implementation (below).

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | History | C1: shipped default `tag_match` could resolve to OR while plan ratifies AND | Resolved | `_resolve_tag_keys` maps `tag_match` to `"any" if == "any" else "all"`, so `None`/default → `all` (AND). Matches Open Question #1 default. |
| CONCERN | Risk | C2: `on_delete` reading SMEMBERS *inside* the queued pipeline would orphan members | Resolved | `on_delete` reads `POPOTO_REDIS_DB.smembers(ptr_key)` EAGERLY, then queues per-Set `srem` + `delete(ptr)` — mirrors IndexedFieldMixin. |
| CONCERN | Risk | C3: TAG_SWAP_LUA passed value-Set keys as ARGV, violating the scripting key contract | Resolved | Value-Set keys are now `KEYS[3..]`; `numkeys = 2 + len(set_keys)`. Cluster caveat documented (popoto index model is inherently single-node). |
| CONCERN | Risk | C4: bare `filter(tags=[...])` silently degrades to client-side list-equality | Resolved | `get_filter_query_params` includes the bare name; `filter_query` raises `QueryException` directing to `__contains`/`__any`/`__all`. Test: `test_bare_exact_match_filter_raises`. |
| CONCERN | Scope | C5: `assess()` tag scoping is scope-creep beyond #492's retrieval focus | Resolved | Dropped `tags`/`tag_match` from `assess()`; tag scoping lands on `assemble()` only. |
| NIT | Scope | N1: RLT criterion had no numeric threshold | Resolved | Benchmark asserts `scoped < max(base*3.0, base+0.05)` and prints the ratio (measured 1.01x). |
| NIT | History | N2: `_tag_field_name` detection attributed to two sites | Resolved | Cached once in `__init__`; read by `assemble()`. |
| NIT | History | N3: atomicity mis-credited to the #476 "unique-conflict window" | Resolved | Reworded: TagField inherits orphan-free membership diffing via the eager `pipeline=None` commit ordering (no uniqueness check). |

**Design change surfaced during build (recorded for reviewers):** the plan's
"inject tags into the shared `filters` dict" seam does NOT scope composite mode —
`composite_score` only honors a sorted-field *partition*, not an arbitrary field
filter (verified against `query.py:551-562`). The implementation instead resolves
the tag-allowed key set once (`_resolve_tag_keys`) and post-filters retrieved
candidates in `assemble()` (`_scope_by_tags`), which is uniform across composite /
hybrid / lexical / push and preserves "omitting tags == identical behavior."

Out-of-scope observation from the critique: `existence_filter.py` matches the
module-command grep (`BF.`) — pre-existing, unrelated to this field, left untouched.

---

## Open Questions

1. **Assembler default `tag_match` semantics (AND vs OR).** The *query* API is fully
   specified by the issue (contains/any/all — no fork there). The one genuine fork is
   the **default** for `ContextAssembler.assemble(tags=[...])` when the caller doesn't
   pass `tag_match`. This plan defaults to **`"all"` (AND / SINTER)** — the intuitive
   reading of "scope to agent:valor AND project:popoto." A maintainer might prefer
   **`"any"` (OR)** if the common case is "surface memories tagged with any of these
   dimensions." Both are one-line supported; only the default differs. **Requesting the
   team lead confirm `all` (AND) as the default, or switch to `any` (OR).**
