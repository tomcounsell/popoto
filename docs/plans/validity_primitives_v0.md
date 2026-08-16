---
status: Planning
type: feature
appetite: Large
owner: Valor Engels
created: 2026-08-16
tracking: https://github.com/tomcounsell/popoto/issues/580
last_comment_id: none
---

# V0 — Validity primitives: ValidityField, SupersessionProtocol, assembler validity gating

## Problem

An agent learns "user is on the free plan." Two weeks later it learns "user upgraded to
enterprise." Popoto today has no way to say the first fact stopped being true. The stale
record keeps its place in every index and only loses ground gradually, through
`DecayingSortedField`'s power-law decay. Until decay catches up, `ContextAssembler` packs
the stale fact into the agent's context at full confidence, alongside — or ahead of — the
correction.

**Current behavior:** Contradiction is a scalar nudge. `_apply_contradicted`
(`src/popoto/fields/observation.py:327-388`) weakens cycles, pushes a negative confidence
signal, and discards staged accesses. It records nothing about *what* contradicted the
record, and it does not remove the record from any index. `grep -rn
'valid_from\|invalid_at\|ingested_at\|supersede' src/` returns zero hits — the validity
axis does not exist.

**Benchmark evidence:** committed LongMemEval-S full run
(`tests/benchmarks/results/external/longmemeval_s_latest_hybrid.json`, 2026-08-07, n=500):
knowledge-update is n=78 at R@1 0.936 with R@10 = 1.000. The gold session is always in the
top ten and is outranked at rank 1 in five cases — the stale-fact-outranks-the-correction
shape exactly.

**Desired outcome:** A memory closed by a newer contradicting memory stops entering default
context assembly immediately, not after decay catches up, while remaining fully queryable in
historical mode. Point-in-time `as_of` reconstruction works. Supersession chains are
traversable in both directions. Salience and validity compose cleanly: validity decides
*membership*, decay decides *ordering among the valid*.

## Freshness Check

**Baseline commit:** `15b76e7` (`chore(deps): bump torch in the uv group across 1 directory (#478)`)
**Issue filed at:** 2026-08-16T08:08:53Z
**Disposition:** Minor drift (line numbers in the issue are stale; every claim still holds)

**File:line references re-verified:**

- `src/popoto/fields/observation.py:327` — "`_apply_contradicted` weakens confidence and
  records nothing about what contradicted it" — **still holds, exact line.** Verified the
  function body 327-388: no chain state written, no index removal.
- `src/popoto/models/query.py:1983-2013` — "`_sorted_pushdown_args`, condition 5 kills
  pushdown when any filter param survives the ordering field" — **drifted to
  `query.py:1944-2001`.** Condition 5 is the `remaining` computation at `1993-1999`;
  docstring states it verbatim at `1968-1973`. Sole call site is `query.py:2077-2079`
  inside `filter_for_keys_set`'s sorted-field loop.
- `src/popoto/models/base.py:1335-1403` — "indexed-field EVALs run eagerly before the
  internal pipeline that carries the sorted-set ZADDs" — **CONFIRMED, drifted to
  `base.py:1458-1487`** (full-save internal-pipeline branch) and `base.py:1263-1285`
  (partial-save branch). The in-code comment at `1459-1471` states the #476 rationale
  explicitly. ZADDs enter at `base.py:1525-1536`, commit at `base.py:1556`.
- `src/popoto/fields/field.py:696,734` — "the documented extension point
  `Field.get_filter_query_params` / `Field.filter_query`" — **drifted to `field.py:584-619`
  and `field.py:621-662`.** Contract unchanged; base `filter_query` raises `QueryException`,
  base `get_filter_query_params` returns `set()` and must be `super()`-unioned.
- `tests/benchmarks/results/external/longmemeval_s_latest_hybrid.json` — present, n=500,
  2026-08-07. Numbers as quoted.

**Cited sibling issues/PRs re-checked:**

- #560 (M1, journal) — open. Its append-only amendment is a hard constraint on this plan's
  chain-link storage (see Technical Approach decision D3).
- #564 (M5), #565 (M6) — open, downstream consumers only.
- #491 / PR #495 (confidence-modulated decay) — merged; `DECAY_SCORE_LUA` at
  `src/popoto/fields/decaying_sorted_field.py:60-174` is the artifact this plan extends.
- #476 (1.8.0 forward-incompat / eager-EVAL split) — the reason the atomicity constraint
  exists. Its fix is the precedent this plan follows.

**Commits on main since issue was filed (touching referenced files):** none.
`git log --oneline --since=2026-08-16T08:08:53Z` is empty.

**Active plans in `docs/plans/` overlapping this area:** none. No plan file mentions
validity, supersession, `valid_from`, or `invalid_at`.

**Notes:** Two recon claims in the issue turned out to be materially wrong about *mechanism*
(not about the underlying hazard). Both are corrected in Technical Approach and both change
what gets built:

1. **`top_by_decay` does not use `_sorted_pushdown_args` at all.** `_sorted_pushdown_args`
   is called from exactly one place (`query.py:2077`), on the `filter()` path.
   `top_by_decay` (`query.py:294-467`) passes `n` as `ARGV[3]` to `DECAY_SCORE_LUA`, which
   `ZRANGE key 0 -1 WITHSCORES` scans and sorts the whole partition and then truncates. The
   issue's acceptance criterion "limit pushdown on `top_by_decay(limit=N)` remains active"
   is asserting the preservation of something that never existed on that path. The real,
   preservable property — and the one this plan asserts by test — is that
   `filter(..., limit=N, order_by=<sorted field>)` pushdown stays active when validity
   gating is enabled, which holds precisely because gating is *not* a filter kwarg.
2. **`ContextAssembler` never calls `top_by_decay`.** Every retrieval call is
   `composite_score` or `fuse` (`context_assembler.py:2007-2015`, `2059-2066`,
   `2199-2209`, `2225-2238`), and the BM25 and graph-propagation arms bypass the `filters`
   dict entirely (`context_assembler.py:2110-2115`). A `filters`-dict gate would therefore
   leak superseded records through the lexical arm. The repo has already solved this exact
   shape once, for tag scoping (#492): a candidate-key post-filter, `_scope_by_tags`
   (`context_assembler.py:1607-1615`), applied at `1689/1691/1761`, whose docstring at
   `1584-1589` states why the filter dict is not a sufficient seam. This plan mirrors that
   pattern.

## Prior Art

`gh pr list --state merged --search "validity supersession temporal"` returns `[]`. There is
no prior attempt at a validity axis. The relevant prior art is structural, not thematic —
four merged patterns this plan reuses rather than reinvents:

- **PR #495 / issue #491** — confidence-modulated decay. Established `DECAY_SCORE_LUA`, the
  `MODULATION_DISABLED` convention (`decaying_sorted_field.py:251`: empty key + zero
  strength makes the disabled path byte-identical), and `Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED`
  as a deploy-level kill switch (`constants.py:66`). This plan extends that script and copies
  both conventions.
- **PR for issue #476** — the eager-EVAL/internal-pipeline split (`base.py:1458-1487`) and
  `INDEX_SWAP_LUA` (`indexed_field_mixin.py:112-152`). Establishes "one script owns all
  state for one logical mutation," which is the whole basis for `SUPERSEDE_LUA`.
- **Issue #492 (tag scoping)** — `_resolve_tag_keys` / `_scope_by_tags`
  (`context_assembler.py:1577-1615`) plus `Defaults.TAG_SCOPING_ENABLED`
  (`constants.py:97-107`). The mode-agnostic assembler seam and the kill-switch comment
  style are copied directly.
- **`TAG_SWAP_LUA` (`tag_field.py:125`, `on_save` at `287-290`)** — the canonical
  `pipeline=`-threading shape for a Lua-backed field hook.

**No prior fixes failed**, so the `## Why Previous Fixes Failed` section is omitted.

## Research

External research skipped by the Phase 0.7 rule: this work introduces no new libraries, no
new services, and no external APIs. It is core Redis/Valkey commands plus Lua 5.1 over
structures the repo already maintains. The only external-surface question — whether
`ZADD`/`ZSCORE`/`ZRANGEBYSCORE` handle `+inf` scores identically on Redis and Valkey — is
answered inside the repo's own constraints (`redis_db.py:8-11`: redis-py works against both;
core commands only) and is verified by a test in this plan rather than by a web search.

No relevant external findings — proceeding with codebase context.

## Data Flow

**Write path (a new claim arrives that supersedes an old one):**

1. **Entry point:** application calls `SupersessionProtocol.supersede(new_instance,
   identity_key=..., at=None)` — or `old_instance.invalidate(at=..., superseded_by=...)`
   for the direct form.
2. **Identity resolution:** `identity_key` is normalized (casefold, whitespace-collapse,
   `subject \x00 predicate`) and hashed to a 16-hex digest, producing the open-claim pointer
   key `$ValidityF:{Model}:{field}:open:{digest}`.
3. **`SUPERSEDE_LUA` — one EVAL, six KEYS:** reads the open pointer, closes the incumbent's
   interval (`ZADD invalid_at <old> <now>`, guarded so an already-closed record is never
   re-closed), opens the newcomer (`ZADD valid_from`, `ZADD invalid_at <new> +inf`,
   `ZADD ingested_at`), writes both chain links (`HSET chain:fwd <old> <new>`,
   `HSET chain:rev <new> <old>`), and repoints the open pointer at the newcomer. Returns the
   closed member key or an empty string.
4. **Storage:** three ZSETs + two HASHes + one STRING per model/field. No model hash is
   mutated (see D3).

**Read path (default assembly):**

1. **Entry point:** `ContextAssembler.assemble(query_cues=..., agent_id=...)`.
2. **Query layer:** `composite_score` / `fuse` / `top_by_decay` reach `DECAY_SCORE_LUA`,
   which now takes `KEYS[3]`=`invalid_at` ZSET, `KEYS[4]`=`valid_from` ZSET and `ARGV[7]`=as-of
   timestamp. Per member, one `ZSCORE` against each: a member whose `invalid_at <= as_of` or
   whose `valid_from > as_of` is skipped before its base-score `HGET` and before the decay
   math. Server-side, inside the same range read — no extra round-trip, no filter kwarg.
3. **Assembler post-filter:** `_resolve_valid_keys()` runs two read-only
   `ZRANGEBYSCORE`s once per `assemble()` call and `_scope_by_validity()` intersects every
   arm's candidate list — covering the BM25 and graph arms the Lua gate cannot see.
4. **Output:** `assemble()` returns only currently-valid records. `assemble(as_of=t)`
   returns what the agent believed at `t`.

**Read path (historical / deliberate query):**

`Model.query.filter(validity__as_of=t)` and `filter(validity__current=True)` route through
`ValidityField.filter_query` (registered via `get_filter_query_params`) and return a key
`set` from the same two `ZRANGEBYSCORE`s. This is a *deliberate* query, it consumes a filter
param, and it therefore disables limit pushdown — documented as expected, and precisely why
the default path does not use it.

## Architectural Impact

- **New dependencies:** none. `hashlib.blake2b` (stdlib) for identity digests; everything
  else is existing imports.
- **Interface changes:** additive only. `DECAY_SCORE_LUA` grows `KEYS[3]`, `KEYS[4]`,
  `ARGV[7]`; all three call sites (`query.py:427`, `query.py:1257`,
  `context_assembler.py:749`) must bump `numkeys` from 2 to 4. Passing an extra key without
  bumping `numkeys` silently shunts it into ARGV — this is the single highest-risk mechanical
  edit in the plan and gets its own verification row.
- **Coupling:** decreases at the semantic layer (validity is orthogonal to salience; neither
  needs to know the other's constants) and increases mechanically at exactly one point — the
  decay script now reads two keys it does not own. Mitigated by the `MODULATION_DISABLED`
  precedent: empty key strings make the gate short-circuit and the disabled path
  bit-identical to today.
- **Data ownership:** `ValidityField` owns six new Redis keys per model/field. It owns no
  bytes inside any model hash (D3), which is what makes it adoptable by M1's append-only
  journal unchanged.
- **Reversibility:** high. `Defaults.VALIDITY_GATING_ENABLED = False` restores byte-identical
  pre-#580 retrieval. Removing the field from a model orphans six keys; the standard
  `on_delete` hook cleans per-record entries.

## Appetite

**Size:** Large

**Team:** Solo dev (this agent) + parallel builder subagents + code reviewer.

**Interactions:**
- PM check-ins: 1-2 (the benchmark-gate scope question below; the RelationshipField deviation)
- Review rounds: 2+ (Lua correctness and the `numkeys` edit warrant a careful pass)

## Prerequisites

| Requirement | Check Command | Purpose |
|---|---|---|
| Redis/Valkey on localhost:6379 | `redis-cli -n 15 ping` | Test suite (DB 15, auto-isolated) |
| Worktree venv resolves to THIS checkout | `python -c "import popoto,os;print(os.path.realpath(popoto.__file__))"` | CLAUDE.md hazard 1: wrong package under test |
| Full extras installed | `python -c "import numpy, sentence_transformers"` | CLAUDE.md hazard 2: `.[dev]` alone deselects ~95 tests |
| No concurrent worktree suite on DB 15 | `redis-cli -n 15 dbsize` before/after | CLAUDE.md hazard 4: phantom failures |

## Solution

### Key Elements

- **`ValidityField`** — a plain `Field` (deliberately *not* a `SortedFieldMixin`) that owns
  the valid-time / transaction-time interval for each record of its model and maintains it as
  ZSET index state. Declared as `validity = ValidityField()`.
- **`SUPERSEDE_LUA`** — one script that performs interval closure, chain-link writing, and
  open-pointer repointing as a single server-side atomic step. There is no code path that
  closes an interval without also updating the indexes, because they are the same command.
- **`SupersessionProtocol`** — a stateless static-method coordinator mirroring
  `ObservationProtocol`'s shape: identity normalization, `supersede()`, `invalidate()`, and
  bidirectional chain traversal. Wired into `_apply_contradicted` so the existing
  "this memory is wrong" signal writes provenance instead of only weakening a scalar.
- **Decay-Lua validity gate** — two optional `KEYS` on `DECAY_SCORE_LUA`; membership decided
  server-side inside the existing range read, never as a filter param.
- **Assembler validity scoping** — `_resolve_valid_keys` / `_scope_by_validity`, the
  mode-agnostic post-filter mirroring tag scoping, plus `assemble(as_of=t)`.
- **`Defaults.VALIDITY_GATING_ENABLED`** — deploy-level kill switch, default `True`.

### Flow

Agent learns a contradicting fact → `SupersessionProtocol.supersede(new_memory,
identity_key=("user_42", "subscription_plan"))` → old record's interval closes at now, chain
links written, open pointer moves → next `assemble()` returns only the new record → 
`assemble(as_of=<two weeks ago>)` still returns the old one → 
`SupersessionProtocol.chain(new_memory)` walks back to it for provenance.

### Technical Approach

**D1 — Keyspace.** `ValidityField` inherits automatic namespacing from the `FieldBase`
metaclass (`field.py:118-124`), giving prefix `$ValidityF`. Six keys per model/field:

| Key | Type | Contents |
|---|---|---|
| `$ValidityF:{Model}:{field}:valid_from` | ZSET | member = record redis_key, score = valid-from epoch seconds |
| `$ValidityF:{Model}:{field}:invalid_at` | ZSET | member = record redis_key, score = close epoch, `+inf` when open |
| `$ValidityF:{Model}:{field}:ingested_at` | ZSET | member = record redis_key, score = ingest epoch |
| `$ValidityF:{Model}:{field}:chain:fwd` | HASH | old redis_key → superseding redis_key |
| `$ValidityF:{Model}:{field}:chain:rev` | HASH | new redis_key → superseded redis_key |
| `$ValidityF:{Model}:{field}:open:{digest}` | STRING | identity digest → currently-open record's redis_key |

An as-of-`t` membership test is `valid_from <= t AND invalid_at > t`: two
`ZRANGEBYSCORE`s intersected, or two `ZSCORE`s in Lua. `+inf` as an open-interval sentinel is
native to both Redis and Valkey sorted sets and needs no special-casing on either read shape.

**D2 — Not a `SortedFieldMixin`.** This is load-bearing. `base.py:191-192` classifies any
`SortedFieldMixin` into `_meta.sorted_field_names`, which puts it in `filter_for_keys_set`'s
first loop (`query.py:2060-2119`), where a returned *list* becomes `_sorted_field_order` and
the field can become the ordering field. `ValidityField` must never win ordering — validity is
membership, not priority. As a plain `Field` it lands in the second loop (`query.py:2121-2146`)
and returns a `set`. This also keeps it out of the reindex/migration loops at `base.py:2904`,
`3259`, `3444`, which iterate `sorted_field_names` and would otherwise need to learn about it.

**D3 — Chain links live in derived hashes, not in model hashes or `Relationship` fields.**
The issue sketch says `superseded_by` / `supersedes` as `Relationship` fields. That is
rejected, for a reason settled in the #456 build-order comment: *"entry hashes are never
mutated, the zsets are derived index state."* A `Relationship` value is stored inside the
model hash (`relationship.py:56-59`), so writing `superseded_by` onto the incumbent mutates
the incumbent's hash — which M1's append-only journal forbids. Two derived HASHes give the
same O(1) bidirectional traversal, are writable from inside `SUPERSEDE_LUA` (so they land in
the same atomic step, which a Python-level `Relationship.on_save` could not), and are
journal-mode-clean. `Relationship` also carries a footgun here: its value is a bare `str`
redis_key until lazily resolved (`relationship.py:70-74`), so every consumer would need
three-type handling. Traversal is exposed as `SupersessionProtocol.superseded_by(instance)`,
`.supersedes(instance)`, `.chain(instance)` — the ORM-facing API the issue actually asked for.
**Flagged as Open Question 1** since it deviates from the issue's written sketch.

**D4 — `SUPERSEDE_LUA`, one EVAL.** KEYS 1-6 in the table order above; ARGV carries new
member, now, valid_from, ingested_at, mode (`open` | `supersede` | `invalidate`), explicit
close-at, and an explicit old-member override for identity-free direct invalidation. The
already-closed guard reads `ZSCORE invalid_at <old>` and refuses to re-close anything whose
score is not `+inf`, making the script idempotent under retry. Registration follows the house
pattern established by every other script in the repo: module-level `SUPERSEDE_LUA = """..."""`
constant preceded by a KEYS/ARGV contract comment, raw `POPOTO_REDIS_DB.eval(SCRIPT, 6, *keys,
*argv)`, no `EVALSHA`, no SHA caching, `pipeline.eval(...)` on the external-pipeline branch
per `tag_field.py:287-290`.

The atomicity criterion falls out of the design rather than being defended by a lock: the
`invalid_at` ZSET *is* the gating index, so closing an interval and removing a record from
retrieval are literally the same `ZADD`. There is no window because there is no second step.

**D5 — Decay-Lua gate.** In `DECAY_SCORE_LUA`, `KEYS[3]` = `invalid_at`, `KEYS[4]` =
`valid_from`, `ARGV[7]` = as-of. The check goes immediately after
`local member = members[i]` (`decaying_sorted_field.py:82-84`) — the cheapest position,
skipping the base-score `HGET` and all decay math for excluded members. Lua 5.1 has no `goto`,
so the existing body is wrapped in `if include then ... end` rather than short-circuited.
Empty key strings disable the gate and make the path bit-identical to today, per the
`MODULATION_DISABLED` precedent. The comments at `decaying_sorted_field.py:62-66` and
`cyclic_decay_field.py:66-71` explicitly forbid renumbering existing KEYS; appending 3 and 4
respects that. `CYCLIC_DECAY_LUA` is **not** modified (its KEYS 1-4 are taken and its
consumers are covered by the assembler post-filter) — see No-Gos.

**D6 — Assembler.** `_resolve_valid_keys(as_of)` returns `set[str] | None` (None = gating
disabled or model has no `ValidityField`), computed from two read-only `ZRANGEBYSCORE`s;
`_scope_by_validity(records, allowed)` is a pure passthrough when `allowed is None`. Applied
at the same three points tag scoping uses (`context_assembler.py:1689`, `1691`, `1761`), and
`assemble()` gains `as_of: float | None = None`. Auto-detection of the model's `ValidityField`
follows the `_cyclic_decay_field_name` / `_bm25_field_name` pattern already in `__init__`.

**D7 — Identity normalization.** Core stays LLM-free: `SupersessionProtocol.identity_key(subject,
predicate)` casefolds, strips, collapses internal whitespace, joins with `\x00`, and
`blake2b(digest_size=8).hexdigest()`s the result into a 16-hex key segment. The `\x00` join
prevents delimiter-collision false merges (`("ab","c")` vs `("a","bc")`); the digest prevents
key injection from user text. Semantic/LLM normalization is explicitly a downstream opt-in
recipe (M5 #564), not core.

**D8 — Constants.** New `# -- ValidityField (fields/validity_field.py, issue #580) ---`
section in `constants.py`: `VALIDITY_GATING_ENABLED = True` (boolean kill switch, not swept,
carrying the TAG_SCOPING_ENABLED-style rationale comment) and
`VALIDITY_OPEN_SENTINEL = float("inf")`. Nothing numeric and tunable is exposed as a
constructor kwarg, per CLAUDE.md.

**D9 — TTL interaction.** A TTL on a `ValidityField`-bearing model truncates chains and
breaks as-of correctness silently. Emit a one-time `logger.warning` at model-definition time
when both are present. Warn, do not raise: refusing would break adopters who legitimately
want bounded history.

**D10 — Decay of invalidated records: freeze.** No decay-score adjustment on closure. A closed
record is simply not scored (it is skipped before the math). Reopening is not a supported
operation in V0.

## Failure Path Test Strategy

### Exception Handling Coverage
`observation.py` uses `except (TypeError, ValueError): pass` for graceful degradation on
unsaved instances (`observation.py:358-359`, `369-370`, `387-388`). The supersession hook
added to `_apply_contradicted` follows the same shape and gets an explicit test asserting
observable behavior: superseding from an unsaved instance degrades silently and leaves *no*
partial index state (no `valid_from` entry, no chain link, no open pointer). New code in
`validity_field.py` / `supersession.py` adds no bare `except Exception: pass`; the one
`try/except` around the `POPOTO_UNIQUE_CONFLICT`-style error mapping re-raises, mirroring
`indexed_field_mixin.py:345-370`.

### Empty/Invalid Input Handling
- `identity_key("", "")` and whitespace-only components → `ValueError`, tested.
- `supersede()` on an identity with no incumbent → opens the newcomer, returns `None`, writes
  no chain link. Tested.
- `invalidate(at=t)` where `t` precedes the record's own `valid_from` → `ValueError` (a
  zero-or-negative-length interval is a caller bug, not a silently-stored state). Tested.
- `assemble(as_of=t)` with no `ValidityField` on the model → passthrough, no crash. Tested.
- Empty `invalid_at`/`valid_from` key strings in the decay Lua → gate off, byte-identical
  output. Tested by comparing scores against the pre-change script.

### Error State Rendering
Not user-facing UI. The observable failure surfaces are exceptions (`ValueError`,
`QueryException`) and the TTL `logger.warning` (D9); each is asserted, the warning via
`caplog`.

## Test Impact

New file `tests/test_validity_field.py` carries the bulk. Existing tests affected:

- [ ] `tests/test_decaying_sorted_field.py` — UPDATE: it imports `DECAY_SCORE_LUA` directly
  (`test_decaying_sorted_field.py:26`). Any test that `eval`s the script by hand must bump
  `numkeys` 2 → 4 and pass two empty key strings. Add a gate-disabled score-parity test.
- [ ] `tests/test_cyclic_decay_field.py` — UPDATE if it asserts on `DECAY_SCORE_LUA` KEYS
  arity; `CYCLIC_DECAY_LUA` itself is unchanged.
- [ ] `tests/test_context_assembler.py` — UPDATE: `assemble()` gains an `as_of` kwarg
  (keyword-only, defaulted) — additive, but assert the no-`ValidityField` passthrough stays
  byte-identical.
- [ ] `tests/test_default_recipe_wiring.py` — UPDATE only if `DefaultMemory`'s field list is
  asserted exhaustively; `DefaultMemory` is **not** modified by this plan (see No-Gos).
- [ ] `tests/test_agent_memory_e2e.py` — UPDATE: verify the e2e path still passes with gating
  on and a model that has no `ValidityField` (the overwhelmingly common case).

No test is DELETEd or REPLACEd. All changes are additive or mechanical `numkeys` bumps.

## Rabbit Holes

- **Rewriting `top_by_decay` to do real limit pushdown.** The script full-scans and full-sorts
  the partition today; that is a genuine O(N log N) inefficiency, and it is tempting to fix it
  while inside the file. It is a separate performance issue with its own correctness surface.
  Do not.
- **Modifying `CYCLIC_DECAY_LUA` for symmetry.** Its KEYS 1-4 are occupied, its comments warn
  against renumbering, and its consumers are already covered by the assembler post-filter.
  Symmetry here buys nothing and risks the exact silent `cmsgpack.unpack`-the-wrong-key
  corruption the file warns about.
- **Semantic identity normalization.** "Is `plan` the same predicate as `subscription_tier`?"
  is an LLM question, it is M5's job, and answering it here drags an API key into the core path.
- **Reopening a closed interval.** Bitemporal databases support it; agents mostly do not need
  it; supporting it means the open-pointer becomes a stack and the idempotency guard becomes
  a state machine.
- **Making `Relationship` writable from Lua.** Tempting for D3 fidelity to the issue sketch.
  It would mean encoding the reverse-index SADD *and* the msgpack model-hash write inside
  `SUPERSEDE_LUA` and keeping them in sync with `relationship.py` forever.

## Risks

### Risk 1: `numkeys` not bumped at one of the three `DECAY_SCORE_LUA` call sites
**Impact:** The extra key silently becomes an ARGV entry. The script reads `KEYS[3]` as nil,
gating silently no-ops, and `ARGV` indices shift — corrupting `base_score_field` or the
confidence parameters. Fails silently, not loudly. This is the single most dangerous edit here.
**Mitigation:** All three sites (`query.py:427`, `query.py:1257`, `context_assembler.py:749`)
are named in one build task assigned to one builder. A `Verification` row greps for
`eval(\s*DECAY_SCORE_LUA,\s*2` and requires zero matches. A test asserts confidence modulation
still produces its pre-change scores with the gate disabled.

### Risk 2: `+inf` ZSET score behaves differently across Redis/Valkey or across redis-py versions
**Impact:** Open intervals mis-read as closed; every record vanishes from assembly.
**Mitigation:** Direct test asserting `ZADD k +inf m` then `ZSCORE`, `ZRANGEBYSCORE k (t +inf`,
and the Lua `tonumber(ZSCORE)` comparison all treat it as open. CI already runs the suite
against both Redis and Valkey service containers (PR #544, merged as `d675218`), so both
engines are covered without extra harness work.

### Risk 3: Post-filter shrinks assembled context below `max_items`
**Impact:** Gating removes candidates *after* the `max_items * 2` limit was spent, so a
partition with many superseded records returns short results.
**Mitigation:** Same hazard tag scoping already has, and the same shape of answer — the
existing `max_items * 2` overfetch absorbs it. The Lua gate handles the composite/decay arms
*before* truncation, so the post-filter's residual job is only the BM25/graph arms. Add a test
asserting a heavily-superseded partition still fills `max_items` when enough valid records
exist.

### Risk 4: The benchmark acceptance criterion is not runnable in this dispatch
**Impact:** An acceptance criterion goes unmet at merge.
**Mitigation:** Escalated as Open Question 2 rather than silently dropped. See No-Gos.

## Race Conditions

### Race 1: Two writers supersede the same identity concurrently
**Location:** `SUPERSEDE_LUA`, all six keys.
**Trigger:** Two processes each learn a contradicting fact for `("user_42", "plan")` within the
same millisecond.
**Data prerequisite:** The open pointer must reflect one incumbent at the moment of read.
**State prerequisite:** Exactly one record ends open for the identity; no interval is closed
twice; no chain fork.
**Mitigation:** Redis/Valkey `EVAL` is single-threaded and the entire read-decide-write sequence
lives inside one script, so the two writers serialize. The second writer sees the first's
newcomer as the incumbent and chains onto it — producing a two-link chain, which is the correct
outcome, not a fork. The `ZSCORE != +inf` guard makes re-close a no-op.

### Race 2: Save and supersede interleave for the same record
**Location:** `base.py:1458-1487` (eager EVAL) vs. `base.py:1525-1556` (internal pipeline).
**Trigger:** `SUPERSEDE_LUA` closes a record while that record's `save()` is between its eager
indexed-field EVALs and its internal pipeline `execute()`.
**Data prerequisite:** The record's hash and its ZSET entries.
**State prerequisite:** Validity index state must not be reverted by a concurrent save.
**Mitigation:** `ValidityField.on_save` writes the interval **only when the record has no
existing `invalid_at` entry** (open-or-absent), using the same `ZSCORE`-guard as
`SUPERSEDE_LUA`. A save on an already-closed record therefore cannot resurrect it. This is why
`on_save` routes through the same script (mode `open`) rather than issuing a bare `ZADD`.

### Race 3: `_resolve_valid_keys` snapshot vs. concurrent supersession mid-`assemble()`
**Location:** `context_assembler.py:1680`-ish.
**Trigger:** A record is superseded after the valid-key snapshot is taken but before scoping runs.
**Mitigation:** Accepted and documented, not prevented. The assembler already takes a
point-in-time view of a live store (tag scoping has the identical property). The worst case is
one stale record in one assembly, which the next call corrects — versus the cost of holding a
consistent snapshot across a multi-arm retrieval. The Lua gate re-checks at score time, so the
composite arm is tighter than the post-filter regardless.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #560] Journal-mode adoption — `ValidityField` under M1's append-only journal,
  where supersede/retract *annotation entries* drive index updates. V0 ships the standalone
  mode and the journal-compatible storage shape (D3); wiring it to a journal that does not
  exist yet is #560's job, per its amendment.
- [SEPARATE-SLUG #564] LLM/semantic identity normalization (triple normalization). Core keeps
  caller-defined identity with deterministic normalization; M5 owns the semantic tier and
  writes through this protocol.
- [SEPARATE-SLUG #565] Provenance citation in assembled output ("previously believed X, revised
  at t"). The chain is traversable in V0; rendering it into context is M6's surface.
- [EXTERNAL] LongMemEval-S before/after on knowledge-update and temporal-reasoning (n=500) and
  the 20k-record p50 latency gate. The run needs the external benchmark corpus, an embedding
  provider, and hours of wall-clock on a machine this agent does not control; it is also
  meaningless until a benchmark model actually declares a `ValidityField` and a supersession
  producer populates it, which no wave-A unit does. Escalated as Open Question 2 — the plan
  ships a deterministic p50 micro-benchmark test in its place and defers the LongMemEval gate.
- [SEPARATE-SLUG #560] Adding `ValidityField` to `DefaultMemory`. Turning it on for the shipped
  default model changes retrieval for every existing adopter with no supersession producer to
  populate intervals, so every record would be perpetually open — carrying the cost with none
  of the benefit. `DefaultMemory` adoption lands with the journal, which is its producer.
- [SEPARATE-SLUG #562] Reopening a closed interval / interval editing. Not required by any
  wave-A or wave-B consumer.

## Update System

No update-system changes required. This is a pure library addition: no new dependencies, no
services, no config files, no deployment topology change. Existing installations gain the
field only by declaring it; nothing changes for models that do not.

## Agent Integration

No agent integration required. This is ORM-internal. The consumer surface is the Python API
(`popoto.ValidityField`, `popoto.SupersessionProtocol`) exported from `src/popoto/__init__.py`
alongside `ObservationProtocol` (`__init__.py:49`, `__all__` at `:192`), and the
`ContextAssembler` gating that fires automatically.

## Documentation

### Feature Documentation
- [ ] Create `docs/features/validity-and-supersession.md` — no frontmatter, opens with `# H1`,
  follows `observation-protocol.md`'s shape: prose lede → `## Overview` → keyspace table →
  API table → worked example → interaction-with-decay section → kill-switch note.
- [ ] Add to `docs/features/README.md` index table.
- [ ] Add to `mkdocs.yml` nav under `Understand & Tune → Primitives`, after
  `ObservationProtocol` (`mkdocs.yml:42`).

### External Documentation Site
- [ ] `mkdocs build --strict` passes (`docs/hooks.py` filters griffe warnings; a new public
  module can reintroduce them).

### Inline Documentation
- [ ] `SUPERSEDE_LUA` preceded by the house-style KEYS/ARGV contract comment + numbered
  "Logic:" steps + `#580` rationale, matching `indexed_field_mixin.py:63-111`.
- [ ] `DECAY_SCORE_LUA`'s header comment (`decaying_sorted_field.py:43-59`) updated for
  `KEYS[3]`/`KEYS[4]`/`ARGV[7]`, including the "empty string = disabled" convention.
- [ ] Module docstring on `validity_field.py` and `supersession.py` with an `Example:` block,
  mirroring `observation.py:1-46`.
- [ ] `Defaults.VALIDITY_GATING_ENABLED` gets the multi-line kill-switch rationale comment in
  `constants.py`.
- [ ] CHANGELOG entry.

## Success Criteria

- [ ] `ValidityField` on any model yields `validity__current=True` and `validity__as_of=t`
      filters returning interval-correct membership from plain sorted sets; core commands and
      Lua only, no Redis modules.
- [ ] An invalidated record never appears in `assemble()` output, effective on the next call,
      and is still returned by `filter(validity__as_of=<before closure>)`.
- [ ] `invalidate()` and same-identity supersession execute as a single `EVAL`; a
      fault-injection test asserts no observable state where a record is interval-closed but
      still index-visible.
- [ ] `filter(limit=N, order_by=<sorted field>)` pushdown remains active with validity gating
      enabled — asserted directly on `_pushdown_limit` — because gating is not a filter param.
      (Supersedes the issue's `top_by_decay` phrasing; see Freshness Check note 1.)
- [ ] Supersession chains traverse both directions from any record; records are closed, never
      deleted.
- [ ] Gate-disabled decay scores are identical to pre-change scores (byte-parity test).
- [ ] p50 validity-gated retrieval within 1 ms of ungated at 20k records, measured by an
      in-repo deterministic micro-benchmark test.
- [ ] All new ops accept `pipeline=`; tuning constants in `Defaults`; tests at
      `tests/test_validity_field.py`; docs page under `docs/features/`.
- [ ] Tests pass (`/do-test`), narrow scope: the files named in Test Impact plus the new file.
- [ ] Documentation updated (`/do-docs`).
- [ ] `mypy src/` error count not increased vs. base, measured in the same environment.

## Team Orchestration

### Team Members

- **Builder (field + Lua core)** — Name: `validity-core-builder`; Role: `ValidityField`,
  `SUPERSEDE_LUA`, constants; Agent Type: `builder`; Resume: true
- **Builder (protocol)** — Name: `supersession-builder`; Role: `SupersessionProtocol`,
  identity normalization, `_apply_contradicted` wiring, exports; Agent Type: `builder`;
  Resume: true
- **Builder (query + assembler)** — Name: `gating-builder`; Role: `DECAY_SCORE_LUA` gate,
  all three `numkeys` bumps, assembler scoping, `assemble(as_of=)`; Agent Type: `builder`;
  Resume: true
- **Test engineer** — Name: `validity-tester`; Role: `tests/test_validity_field.py` plus the
  Test Impact updates; Agent Type: `test-engineer`; Resume: true
- **Documentarian** — Name: `validity-docs`; Role: feature page, nav, README index, CHANGELOG;
  Agent Type: `documentarian`; Resume: true
- **Validator** — Name: `validity-validator`; Role: runs the Verification table, reproduces
  every metric; Agent Type: `validator`; Resume: true

All builders share the single session worktree with **disjoint file sets** so commits never
interleave: core owns `fields/validity_field.py` + `fields/constants.py`; protocol owns
`fields/supersession.py` + `fields/observation.py` + `__init__.py`; gating owns
`fields/decaying_sorted_field.py` + `models/query.py` + `recipes/context_assembler.py`.

### Step by Step Tasks

#### 1. Field + `SUPERSEDE_LUA` core
- **Task ID**: build-validity-core
- **Depends On**: none
- **Validates**: `tests/test_validity_field.py` (create)
- **Assigned To**: validity-core-builder — **Agent Type**: builder — **Parallel**: true
- Create `src/popoto/fields/validity_field.py`: `ValidityField(Field)`, key helpers per D1,
  `on_save` (mode `open`, ZSCORE-guarded per Race 2), `on_delete` (ZREM ×3, HDEL ×2, pointer
  cleanup), `get_filter_query_params` (`super()`-unioned), `filter_query` returning a `set`.
- Write `SUPERSEDE_LUA` per D4 with the house-style contract comment; raw `eval`, 6 KEYS,
  `pipeline.eval` on the external-pipeline branch per `tag_field.py:287-290`.
- Add the `constants.py` section per D8.
- Add the D9 TTL warning.

#### 2. `SupersessionProtocol`
- **Task ID**: build-supersession
- **Depends On**: build-validity-core
- **Validates**: `tests/test_validity_field.py`
- **Assigned To**: supersession-builder — **Agent Type**: builder — **Parallel**: false
- Create `src/popoto/fields/supersession.py` mirroring `observation.py`'s module shape:
  docstring w/ `Example:`, `Defaults` aliases block, static-method class, `_apply_*` helpers.
- `identity_key()` per D7; `supersede()`, `invalidate()`, `superseded_by()`, `supersedes()`,
  `chain()`.
- Wire into `_apply_contradicted` (`observation.py:327`) behind the same graceful-degradation
  `except (TypeError, ValueError)` shape, only when the instance's model has a `ValidityField`.
- Export from `src/popoto/__init__.py` (import line + `__all__`).

#### 3. Decay-Lua gate + assembler scoping
- **Task ID**: build-gating
- **Depends On**: build-validity-core
- **Validates**: `tests/test_validity_field.py`, `tests/test_decaying_sorted_field.py`,
  `tests/test_context_assembler.py`
- **Assigned To**: gating-builder — **Agent Type**: builder — **Parallel**: true
- `DECAY_SCORE_LUA`: append `KEYS[3]`/`KEYS[4]`/`ARGV[7]` per D5; update the header comment.
- Bump `numkeys` 2 → 4 and pass the two keys (or `''`) at **all three** call sites:
  `query.py:425-436`, `query.py:1255-1266`, `context_assembler.py:747-757`.
- `_resolve_valid_keys` / `_scope_by_validity` per D6; apply at `context_assembler.py:1689`,
  `1691`, `1761`; add `as_of` to `assemble()`; auto-detect the model's `ValidityField` in
  `__init__`.
- Do **not** touch `CYCLIC_DECAY_LUA`.

#### 4. Tests
- **Task ID**: build-tests
- **Depends On**: build-supersession, build-gating
- **Assigned To**: validity-tester — **Agent Type**: test-engineer — **Parallel**: false
- `tests/test_validity_field.py` following `test_decaying_sorted_field.py` conventions
  (`SCRIPT_DIR` preamble, `from src import popoto`, module-scope test models, no manual DB
  fixtures): interval correctness, `as_of`, `+inf` semantics on both engines, chain traversal
  both directions, single-EVAL/fault-injection atomicity, pushdown-preservation assertion on
  `_pushdown_limit`, gate-disabled score parity, kill-switch off-path, 20k p50 micro-benchmark,
  and every Failure Path case.
- Apply the Test Impact updates.

#### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: build-tests
- **Assigned To**: validity-docs — **Agent Type**: documentarian — **Parallel**: false
- Per the Documentation section.

#### 6. Final validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: validity-validator — **Agent Type**: validator — **Parallel**: false
- Run every Verification row; reproduce test and mypy counts and state the environment
  alongside each, per CLAUDE.md's worktree rules.

## Verification

| Check | Command | Expected |
|---|---|---|
| Package under test is this checkout | `python -c "import popoto,os;print(os.path.realpath(popoto.__file__))"` | output contains `.worktrees/dev-49da033b` |
| New tests pass | `pytest tests/test_validity_field.py -q` | exit code 0 |
| Decay tests still pass | `pytest tests/test_decaying_sorted_field.py tests/test_cyclic_decay_field.py -q` | exit code 0 |
| Assembler tests still pass | `pytest tests/test_context_assembler.py -q` | exit code 0 |
| No stale `numkeys=2` decay EVAL | `grep -rn "DECAY_SCORE_LUA,$" -A1 src/ \| grep -c "^\s*2,"` | match count == 0 |
| Gate is not a filter kwarg on the default path | `grep -c "validity__current" src/popoto/recipes/context_assembler.py` | match count == 0 |
| No Redis-module commands introduced | `grep -rnE "\b(BF\.\|CMS\.\|TOPK\.\|TS\.)" src/popoto/fields/validity_field.py src/popoto/fields/supersession.py` | exit code 1 |
| `CYCLIC_DECAY_LUA` untouched | `git diff main --stat -- src/popoto/fields/cyclic_decay_field.py \| wc -l \| tr -d ' '` | output contains `0` |
| No model-hash mutation for chain links (D3) | `grep -cE "HSET.*supersed" src/popoto/fields/supersession.py` | match count == 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Type check | `mypy src/` | error count not increased vs. base in the same env |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique. -->

---

## Open Questions

1. **Chain links as derived HASHes instead of `Relationship` fields (D3).** The issue sketch
   names `superseded_by` / `supersedes` as `RelationshipField`s. A `Relationship` value lives
   inside the model hash, so writing it onto the incumbent mutates the incumbent — which the
   #456 build-order comment's append-only rule forbids, and which cannot be done inside
   `SUPERSEDE_LUA` anyway. The plan uses two derived HASHes and exposes ORM-level traversal
   helpers instead. Same bidirectional O(1) traversal, journal-mode-clean, atomic. Confirming
   this is the intended reading of "one supersession mechanism."
2. **The LongMemEval-S acceptance criterion.** The AC asks for a before/after on
   knowledge-update and temporal-reasoning vs. the committed n=500 baseline. That run needs the
   external corpus, an embedding provider, and hours of wall clock — and it would measure
   nothing today, because no benchmark model declares a `ValidityField` and no wave-A unit
   produces supersessions to populate one. Proposing: ship V0 with the deterministic p50
   micro-benchmark and defer the LongMemEval gate to the first wave that has a producer
   (M1 #560 or M5 #564). Needs Tom's call — it is a written acceptance criterion, so I am not
   self-clearing it.
3. **`assemble()` default: gate on from day one, or opt-in for one release?** The issue says
   "behind a flag for one release." The repo has no one-release-deprecation convention — the
   nearest precedent is a default-`True` kill switch (`TAG_SCOPING_ENABLED`,
   `DECAY_CONFIDENCE_MODULATION_ENABLED`), which also matches the project's documented
   default-ON/auto-detect doctrine. The plan ships `VALIDITY_GATING_ENABLED = True`. Note this
   is a no-op for every model without a `ValidityField`, which is all of them today —
   including `DefaultMemory` (see No-Gos) — so the blast radius of "on by default" is zero at
   merge and grows only as adopters opt in by declaring the field.
