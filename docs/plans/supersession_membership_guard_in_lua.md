---
status: Ready
type: bug
appetite: Medium
owner: Valor Engels
created: 2026-09-02
tracking: https://github.com/tomcounsell/popoto/issues/588
last_comment_id: 5508044139
revision_applied: true
revision_applied_at: 2026-09-03T09:43:27Z
---

# #588 — Move the supersession membership guard into SUPERSEDE_LUA, and make valid-time single-writer

## Problem

An agent wants to record a correction the way every bitemporal store records one: append the
new claim and close the old claim's validity interval in a single MULTI/EXEC, so no reader
ever sees both open or neither present. The obvious spelling of that is:

```python
pipe = popoto.get_redis().pipeline()
new.save(pipeline=pipe)
SupersessionProtocol.invalidate(old, superseded_by=new, pipeline=pipe)
pipe.execute()
```

It does nothing. No EVAL is queued, no exception is raised, no line is logged. `old` stays
`validity__current=True` and both chain hashes stay empty.

**Current behavior:**

- `SupersessionProtocol._member_key` (`src/popoto/fields/supersession.py:376-394`) resolves an
  instance to its Redis key and then gates on `POPOTO_REDIS_DB.exists(member)`. When the
  successor's `HSET` is only *queued* on the caller's pipeline, `EXISTS` returns 0, so
  `_member_key` reports the instance as unsaved and `invalidate` takes its documented
  "unsaved successor -> no-op" branch (`supersession.py:246-253`).
- That branch returns `None`, which is **byte-identical to the normal pipeline-mode return**
  (`_closed_key`, `supersession.py:471-481`, returns `None` whenever a pipeline was supplied,
  because the script has not run yet). The caller has no signal at all.
- The defect is not specific to pipelines. The check runs client-side *before* the write is
  queued; the write it protects happens later, inside `SUPERSEDE_LUA` at EXEC time. Immediate
  mode has the same defect with a millisecond-wide TOCTOU window.
- M1 (#560) already routes around it: `provenance_journal.py:1118-1138` calls
  `ValidityField.execute_supersede` directly with a load-bearing comment saying *do not
  simplify this back to the protocol*. The workaround is documented in the source, which is
  the clearest possible statement that the protocol's own pipeline API is unusable.
- **Secondary defect, same root.** `execute_supersede` writes the *index* and never the *hash*.
  `SUPERSEDE_LUA`'s newcomer block uses `ZADD ... NX` (`validity_field.py:213-215`), so a
  `valid_from` passed to `execute_supersede` for a record that already has an interval is
  silently discarded. The external reporter (issue comment, probe at
  [`DanceNitra/agora@37b4a79`](https://github.com/DanceNitra/agora/blob/37b4a79/probes/popoto_valid_from_hash_and_index_disagree.py))
  measured the reachable form of this: save a record with no event time (hash `validity` is
  nil, index takes the save clock), then re-save it carrying the corrected event time. The
  hash accepts the correction — `Model.save()` HSETs the new field value — and the index
  refuses it, because `ZADD NX` already holds a score. Measured **30.0 days apart**, and the
  save returned normally. `filter(validity__as_of=t)` for a `t` inside the corrected interval
  returns nothing while `.validity` on that same record says the fact was true at `t`. One
  record, two readable surfaces, different answers, no error anywhere.

**Desired outcome:**

The membership check evaluates at the instant of the write, inside the script, so pipeline
mode and immediate mode behave identically and a caller who asks for something impossible
gets a typed error instead of silence. `Model:None` is still rejected — not by a client-side
probe, but because the record genuinely does not exist when the script looks. Valid-time has
exactly one writer, and an assertion that disagrees with the stored start is a typed error
rather than a 30-day silent divergence between the hash and the index. And the shape M4–M6
want — append the successor, close the incumbent, one transaction — is a single supported
call instead of a pipeline the caller assembles by hand.

## Freshness Check

**Baseline commit:** `d8914fc` (`origin/main`, 2026-09-03) — superseding the first-pass
baseline `44abc17` and the post-#594 baseline `c7fc167`. `git diff --stat c7fc167 d8914fc --
src/ tests/` is **empty**: every commit between them (`4612883`, `a6c81ce`, `1494969`,
`593edc4`, `a2266bc`, `7ad9c8f`, `0d4dc66`, `d8914fc`) is a plan-document edit for #588,
#595, or #596. So every line number verified at `c7fc167` is still exact at `d8914fc`, and
the branch may be cut from **`c7fc167` or later** — but the baseline suite and mypy counts
must be *measured* at the commit actually checked out (Task 4). See **Post-#594
re-verification** below for the authoritative line-number table; where this section and that
table disagree, the table wins.
**Issue filed at:** 2026-08-17T09:12:23Z
**Disposition:** Minor drift (root cause and all decisions unchanged; `models/base.py` and
`tests/test_validity_field.py` line numbers moved under PR #594)

<!-- Re-baselined from 60aa730 to 44abc17 during the critique revision pass. The single
     intervening commit touches bm25_field.py / query.py / context_assembler.py /
     test_hybrid_retrieval.py — none of them in this plan's blast radius. Every line
     reference below was re-verified at 44abc17. -->

Three line references were corrected during the critique revision pass and are marked
**[corrected]** below; the first-pass plan cited `base.py`'s internal-pipeline branch without
the eager-indexed branch above it, `provenance_journal.py:1197-1231` for the pipeline
validations, and `observation.py:484` for the `except` line.

**File:line references re-verified:**

- `src/popoto/fields/supersession.py:376-394` — `_member_key` gates on
  `POPOTO_REDIS_DB.exists(member)` — **still holds, exact lines.** The `EXISTS` call is at
  `:391`.
- `src/popoto/fields/supersession.py:246-253` — `invalidate`'s "unsaved successor -> no-op"
  branch — **still holds, exact lines.**
- `src/popoto/fields/supersession.py:471-481` — `_closed_key` returns `None` for any pipeline
  call — **still holds, exact lines.**
- `src/popoto/fields/validity_field.py:157-221` — `SUPERSEDE_LUA` body — **still holds.**
  `ZADD NX` on the newcomer at `:213-215`; the `CLOSE_BEFORE_START` `error_reply` at `:197`;
  `return closed` at `:220`.
- `src/popoto/fields/validity_field.py:704-809` — `execute_supersede` — **still holds.**
  Pipeline branch `:796-798`, `ResponseError` -> `ValueError` remap `:801-807`.
- `src/popoto/fields/validity_field.py:849-896` — `on_save` in mode `'open'` — **still holds.**
  `field_value` -> `valid_from` coercion at `:883-886`.
- `src/popoto/recipes/provenance_journal.py:1118-1138` — M1's "use `execute_supersede`, NOT
  `SupersessionProtocol` (#588)" comment and direct call — **still holds, exact lines.**
- `src/popoto/models/base.py:1383-1432` — the internal-pipeline save path: `HSET` queued at
  `:1386`, field `on_save` hooks queued at `:1412-1424`, `execute()` at `:1432` — held at
  `44abc17`; **moved to `:1405-1453` / `:1407` / `:1430-1445` / `:1453` by PR #594** (see the
  re-verification table). This ordering is load-bearing for D5 below.
- `src/popoto/fields/observation.py:480-486` **[corrected]** — `_apply_supersession` delegates to
  `SupersessionProtocol.invalidate` inside `except (TypeError, ValueError): pass` — **still
  holds, exact lines.**

**Cited sibling issues/PRs re-checked:**

- #580 (V0, PR #582) — **closed.** Shipped `ValidityField`, `SupersessionProtocol`,
  `SUPERSEDE_LUA`, assembler gating. This plan edits its output.
- #560 (M1, PR #589/#591) — **closed.** Shipped `ProvenanceJournal` with the documented
  workaround for this bug baked into its source.
- #576 — **open**, PR #593; landed on this branch as `60aa730`, extended by `44abc17`. Sequencing prerequisite, met.
- #584 — **open.** Sequenced alongside this issue by the maintainer decision; touches
  `popoto.integrations` DB selection, no overlap with the validity keyspace.
- #563 (M4) — **open.** Sequenced *after* this plan, and is the consumer that makes the
  combined entry point worth building.

**Commits on main since issue was filed (touching referenced files):**

- `1467095` feat(#562) M3 — added `provenance_journal.py` candidate-generator surface; did not
  touch `_write`'s supersede path. *Irrelevant to root cause.*
- `3a793d6` feat(recipes) exclude_keys — `ContextAssembler` only. *Irrelevant.*
- `60aa730` fix(#576) — retrieval scoping. *Irrelevant.*
- `44abc17` fix(#576) — `fuse()` scoping for unindexed plain-`Field` filters; touches `bm25_field.py`,
  `query.py`, `context_assembler.py`, `test_hybrid_retrieval.py`. *Irrelevant — none in this
  plan's blast radius.*
- No commit has touched `supersession.py` or `validity_field.py` since the issue was filed.

**Active plans in `docs/plans/` overlapping this area:** `validity_primitives_v0.md` (the V0
plan whose D1/D4/D7 decisions this amends) and `provenance_journal_m1.md` (whose D7 pre-flight
this plan partially subsumes — see Migration below). Neither is in flight.

**Notes:** No drift. Every line reference in the issue body and in the maintainer decision is
still exact at `44abc17`.

### Post-#594 Addendum (added 2026-09-03, before build)

Everything above was verified at `44abc17`. **PR #594 ("Agent memory production audit:
contracts and P0 fixes") is open and expected to merge first**, and it touches this plan's
blast radius in one specific way. Re-verify line numbers against `main` after #594 lands; the
*decisions* below are unaffected, but two of them now describe the wrong mechanism.

**What #594 changes here:**

1. **All 38 raw `eval` sites go through a cached `Script` registry.** `src/popoto/redis_db.py`
   gains `lua_script(text)` and `run_lua(client, text, numkeys, *keys_and_args)`; the latter is
   a drop-in for `client.eval(...)` that sends `EVALSHA`. `execute_supersede`
   (`validity_field.py:794-800` at #594's HEAD) is converted:
   `pipeline.eval(SUPERSEDE_LUA, 6, *args)` → `run_lua(pipeline, SUPERSEDE_LUA, 6, *args)`, and
   the same for the immediate branch. **Write the new call sites as `run_lua`, not `eval`** —
   a build that reintroduces a raw `eval` will pass its own tests and regress the registry.

2. **The `SCRIPT LOAD`/`EVALSHA` rabbit hole is resolved, not rejected.** The Rabbit Holes
   section below rejects registering `SUPERSEDE_LUA` with `EVALSHA` on the grounds that it
   "would introduce the mixed-version window that embedding-by-value currently makes
   impossible". #594 makes that call repo-wide, so the decision is no longer this plan's to
   make; the bullet stands only as the reason **not to do additional registry work here**.
   redis-py's `Script.__call__` adds the script to a pipeline's `scripts` set so `EXEC` is
   preceded by `SCRIPT EXISTS`/`SCRIPT LOAD` — worth knowing for D1's round-trip accounting,
   since it is one extra round trip on the *first* pipeline per process, not per call.

3. **Risk 4's grep claim is stale.** "A grep confirms `execute_supersede` is the only non-test
   `eval` site" was true at `44abc17`. After #594 it is the only non-test **`run_lua`** site
   for `SUPERSEDE_LUA`; re-run the grep for both spellings.

4. **Risk 5's proof tests were rewritten by #594.**
   `test_validity_field.py::TestAtomicity::test_supersede_issues_exactly_one_mutating_call`
   (`:635`) and `::test_invalidate_issues_exactly_one_mutating_call` (`:649`) now count
   `eval` **plus `evalsha`** and assert on the sum, and
   `test_fault_after_eval_leaves_no_torn_state` monkeypatches `POPOTO_REDIS_DB.evalsha`
   rather than `.eval`. The call-count contract is unchanged (still exactly one mutating
   call), so these remain the proof that D1 removes a round trip rather than moving one — but
   the Test Impact section's line numbers and patch shapes must be re-read against the merged
   file, not the ones quoted here.

5. **`_decay_eval_numkeys` in `test_validity_field.py:859` now matches both spellings**
   (`eval(DECAY_SCORE_LUA` and `run_lua(<client>, DECAY_SCORE_LUA`). Any new Lua call site
   this plan adds must remain matchable by that regex or the guard silently stops guarding.

**What #594 does not change:** `supersession.py` is untouched, `SUPERSEDE_LUA`'s body is
untouched, and the `_member_key` `EXISTS` probe at `:391` — the actual bug — is untouched.
The root cause, the decisions, and the D1–D7 approach all still hold.

**Sibling issue status refresh (2026-09-03):** #584 is closed by PR #594, so the "sequenced
alongside" note under Cited sibling issues is now historical. #576 is closed (PR #593 merged).
#563 (M4) remains open and still sequences after this plan.

### Post-#594 re-verification (2026-09-03, PLAN stage)

**PR #594 has merged** as `16aa702`. **The authoritative baseline for this plan is `c7fc167`
or later**, not `44abc17`; `origin/main` is `d8914fc` as of 2026-09-03 and `src/`/`tests/` are
byte-identical across that range (N1). Every file:line reference in this document was
re-read against `origin/main`. The root cause, the decisions, and D1–D7 are unaffected.

**Still exact at `c7fc167` — and, per N1, still exact at `d8914fc` — no edit needed:**
`supersession.py:376-394` / `:391` / `:246-253` / `:471-481`;
`validity_field.py:157` (`SUPERSEDE_LUA =`) / `:197` / `:213-215` / `:220` / `:229` /
`:704-809` (`execute_supersede`, def at `:705`) / `:796-798` (pipeline branch) /
`:801-807` (remap) / `:849-896` `on_save` with the coercion at `:883-886` / `:764-772`
(client-side close-before-start pre-check);
`provenance_journal.py:299-310` (four `IndexedField`s + `ValidityField`) / `:906-964` (D7
pre-flight) / `:1015-1057` (the three pipeline validations, at `:1018-1022`, `:1023-1028`,
`:1046-1057`) / `:1085-1102` (falsy-save `RuntimeError`) / `:1118-1138` (M1's false `#588`
comment, still at `:1118`, call at `:1126`);
`observation.py:480-486` with the `except` at `:485`;
`test_provenance_journal.py:326-336` (`_supersede_mode`) / `:1369` / `:1373-1374` / `:1453` /
`:1501` / `:1540` / `:1840`.

**Drifted — use these numbers, the ones cited elsewhere in this plan are pre-#594.** Three
further anchors were added by the round-2 revision pass and are verified at `d8914fc`:
`base.py:1282-1292` (the `pre_save` gate, B1's single dispatch site), `base.py:1325` /
`:1382` and `base.py:1503` / `:1578` (the external-pipeline arms and their returns),
`supersession.py:292` / `:323` / `:330` (`chain`, its anchor gate, and the
`get_interval_keys` call B2 hoists), and `provenance_journal.py:1155` (`results =
pipe.execute()` — C2 cited `:1157`, which is one line inside the comment above it).

| Cited elsewhere as | Correct at `c7fc167` | What it is |
|---|---|---|
| `base.py:1559-1584` | **`base.py:1581-1607`** | full-save eager `IndexedFieldMixin` phase; the #476 rationale comment is `:1581-1592`, the dict `:1593-1597`, the loop `:1598+`. The `pre_save_validate` dispatch goes immediately before `:1593`. |
| `base.py:1591` (HSET queued) | **`base.py:1609`** `internal_pipeline`, **`:1612`** `HSET` | full-save pipelined phase |
| `base.py:1625-1634` (execute) | **`base.py:1678`** | full-save `internal_pipeline.execute()` |
| `base.py:1367-1381` | **`base.py:1384-1394`** | partial-save eager phase (`_eager_indexed_update_fields`); dispatch goes immediately before `:1388` |
| `base.py:1383-1432` / `:1386` / `:1412-1424` / `:1432` | **`base.py:1405-1453`** / `:1407` / `:1430-1445` / `:1453` | partial-save internal pipeline: construct, `HSET`, queued `on_save` hooks, `execute()` |
| `validity_field.py:30-32` | **`:30-34`** | the Valkey-safe command list (`EXISTS` gets added here) |
| `validity_field.py:483-487` | **`:485-489`** | `import_state`'s plain `SET` of open pointers (Risk 3) |
| `provenance_journal.py:1157-1161` | **`:1157-1162`** | `bool(results[close_index])` is on `:1162` |
| `test_validity_field.py:1388` | **`:1401`** | `test_unsaved_instance_degrades_with_no_partial_state` (REPLACE) |
| `test_validity_field.py:1406` | **`:1419`** | `test_invalidate_with_an_unsaved_successor_is_a_no_op` (REPLACE) |
| `test_validity_field.py:1595` | **`:1608`** | `test_unsaved_successor_degrades_with_no_partial_state` (**frozen**, D7) |
| `test_validity_field.py:1609` | **`:1622`** | `test_unsaved_contradicted_instance_degrades_with_no_partial_state` (**frozen**, D7) |
| `test_validity_field.py:649` | **`:655`** | `test_invalidate_issues_exactly_one_mutating_call` (`:635` is unchanged) |
| `test_validity_field.py:859` | **`:855`** | `_decay_eval_numkeys` |
| `test_validity_field.py:1840-1970` | **`:1853-1983`** | `TestTransferRoundTrip`; return-shape assertions are now `:588, :646, :1460, :1880, :1977` |

**Confirmed converted by #594:** `execute_supersede` now calls
`run_lua(pipeline, SUPERSEDE_LUA, 6, *args)` at `:797` and
`run_lua(POPOTO_REDIS_DB, SUPERSEDE_LUA, 6, *args)` at `:800`; `redis_db.lua_script` is at
`:594` and `redis_db.run_lua` at `:613`. **Write the new call sites as `run_lua`, never
`eval`.**

**Every anti-criterion and red-state row below was proven at `44abc17`.** #594 touched none of
the greps' subjects (`_member_key`'s probe, the `MUTATION PHASE` marker, M1's comment, the
`ARGV[8]` string, `CHANGELOG.md`, `popoto.SupersessionProtocol`'s attribute set), so the
red-state readings carry forward unchanged. Re-run them at `c7fc167` at build start anyway —
they are one-line commands and the table is the PR's paper trail.

**Baseline for Task 4's test counts must be re-measured at the branch point (`d8914fc` or
later).** A count taken at `44abc17` is not comparable: #594 changed `test_validity_field.py`
and added files. A count taken at `c7fc167` *is* comparable to one at `d8914fc` (`src/` and
`tests/` are byte-identical between them), but measure at the commit you actually check out
rather than assuming.

## Prior Art

- **#580 / PR #582** — V0 validity primitives. Shipped the `EXISTS`-in-`_member_key` guard
  deliberately, with the rationale recorded in the docstring: an unsaved instance yields
  `"Model:None"`, and opening an interval for it would leave permanent orphan index state.
  The guard was guarding the right thing; it was placed where it could not stay true.
  *Succeeded at its stated goal, at the wrong layer.*
- **#560 / PR #589, #591** — M1 provenance journal. Hit this bug during its spike and shipped
  a D7 client-side pre-flight (`provenance_journal.py:906-964`) that re-implements target
  existence, cross-agent ownership, and backdate checks in Python before issuing anything,
  plus a direct `execute_supersede` call that bypasses the protocol. *Worked, by not using
  the broken API.* Its pre-flight reads (target `EXISTS`, target's stored `valid_from`) are
  the same two reads this plan moves into the script — M1 keeps its versions for the
  ownership and firewall checks the script cannot make.
- **PR #476 / the atomic-index work** — established the repo pattern this plan follows:
  a client-side pre-check for a good error message, plus an authoritative check inside the
  Lua that closes the race. `indexed_field_mixin.py:168` returns
  `redis.error_reply('POPOTO_UNIQUE_CONFLICT')` and `:427-432` maps it to a typed
  `ModelException`. That is the error-encoding convention this plan extends.
- **No prior attempt to fix #588 exists.** It was filed as an investigation and left open
  pending the two maintainer questions, both of which are now answered.

## Research

No relevant external findings — this is entirely internal to Popoto's Lua contract and
redis-py's `ResponseError` surface. The one external input is the reporter's probe, which is
treated as a reproduction (cited above), not as research.

**Valkey-safety re-confirmed from the repo's own constraint** (`validity_field.py:30-32`):
every command this plan adds to the script is `EXISTS`, `ZSCORE`, and `redis.error_reply` —
core keyspace commands and a Lua 5.1 builtin. No Redis-module command (`BF.`/`CMS.`/`TS.`/
`TOPK.`) is introduced. The script continues to run byte-identically on Redis and Valkey.

## Data Flow

The change is entirely on the write path. Traced for the shape that is currently broken:

1. **Entry point**: caller invokes `SupersessionProtocol.save_and_supersede(new, identity_key=...)`
   (new API) or `SupersessionProtocol.supersede(new, identity_key=...)` (existing API).
2. **`_member_key`** (`supersession.py`): resolves `instance.db_key.redis_key` to a string.
   **No Redis call.** Returns `None` only when key resolution itself raises.
3. **`execute_supersede`** (`validity_field.py`): assembles KEYS[1..6] / ARGV[1..8], applies
   the cheap client-side pre-checks (close-before-start, and the new valid-from assertion
   pre-check), then either queues the EVAL on the caller's pipeline or runs it immediately.
4. **`SUPERSEDE_LUA` validation phase** (new): resolve the incumbent (ARGV[7], else `GET`
   KEYS[4]); `EXISTS` the successor and the explicitly-named incumbent; compare the asserted
   `valid_from` against the stored `ZSCORE`. Any failure returns an `error_reply` **before the
   first write command**, so the script is all-or-nothing without needing rollback.
5. **`SUPERSEDE_LUA` mutation phase** (unchanged): close the incumbent, write both chain
   links, open the newcomer with `NX`, repoint the pointer.
6. **Output**: bulk string — the closed member's key, or `''`. Unchanged reply shape.
   Failures arrive as `ResponseError`, mapped by `execute_supersede` to a typed exception.

The instant that matters is between (2) and (4). Today the existence question is answered at
(2) and acted on at (5), with a caller-controlled amount of time in between. After this change
it is answered and acted on inside the same script invocation.

## Architectural Impact

- **New dependencies**: none.
- **New dependency on `models/base.py`** (added in the critique revision): a generic
  `Field.pre_save_validate` hook and its dispatch, needed because `ValidityField` is not an
  `IndexedFieldMixin` and so cannot otherwise run before the eager indexed-field write phase
  (D5 half 1, Risk 6). Dispatched from a **single** site above the partial/full save split, so
  it covers the external-pipeline arms too (round-2 B1). This is the only edit outside
  `validity_field.py`, `supersession.py`, `observation.py`, and two narrow edits in
  `provenance_journal.py` (a comment correction, plus D8's `pipe.execute()` wrapper).
- **Interface changes**:
  - `SUPERSEDE_LUA` gains `ARGV[8]` and three error replies. Additive: a caller passing 7 ARGV
    still works (`ARGV[8] or ''` -> not asserted), which matters because the script is embedded
    by value, not by SHA, so there is no mixed-version deployment window.
  - `execute_supersede` gains `assert_valid_from: bool = False`. Default preserves today's
    behavior for every existing caller.
  - `SupersessionProtocol.supersede` / `.invalidate` now **raise** where they previously
    returned `None`, for a member that does not exist. This is the intended behavior change
    (issue Q1: "If the latter, it should raise rather than silently no-op").
  - New: `SupersessionProtocol.save_and_supersede`, `.save_and_invalidate`, and the
    `SupersedeResult` dataclass.
  - New exception hierarchy, all subclassing `ValueError` (see D4) so no existing
    `pytest.raises(ValueError)` or `except (TypeError, ValueError)` handler changes meaning.
- **Coupling**: *decreases*. `_member_key` stops depending on `POPOTO_REDIS_DB`, so key
  resolution becomes pure. The protocol layer stops needing to know whether a record is
  durable; only the script does.
- **Data ownership**: unchanged — `ValidityField` still owns all six derived keys and
  `execute_supersede` remains the single place that knows the script's KEYS/ARGV order.
- **Reversibility**: high. The script is a module-level string; reverting is a revert of two
  files plus the new tests. No stored data shape changes, no migration.

## Appetite

**Size:** Medium

**Team:** Solo dev, PM, code reviewer

**Interactions:**
- PM check-ins: 1 (confirm the behavior break in `supersede`/`invalidate` is acceptable to ship
  in the beta window — the maintainer decision already says yes, this is a courtesy checkpoint)
- Review rounds: 2 (the blast radius crosses five test files and one shipped recipe)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -n 15 PING` | Test suite runs on DB 15 |
| Editable install resolves to this checkout | `python -c "import popoto,os;print(os.path.realpath(popoto.__file__))"` | CLAUDE.md worktree gate 1 |
| Full extras installed — **satisfied 2026-09-02** | `python -c "import numpy, sentence_transformers, mcp"` | CLAUDE.md worktree gate 2. `mcp` was genuinely missing from this venv and has now been installed, so `.[dev,embeddings,benchmark,mcp]` is satisfied and the ~95 previously-deselected tests are collected again. Re-run the check before trusting any count; do not compare a baseline taken before this to a post-change number. |
| Baseline suite green | `pytest tests/test_validity_field.py tests/test_provenance_journal.py -q` | Establish the pre-change count before touching anything, **at the commit the branch was cut from (`d8914fc` or later)** — a count taken at `44abc17` predates PR #594's test edits and is not comparable |

## Solution

### Key Elements

- **`_member_key` resolves, it does not verify.** One `try`/`except` around
  `instance.db_key.redis_key`, returning the string or `None`. No Redis round trip.
- **`SUPERSEDE_LUA` gains a validation phase** that runs entirely before the first write:
  incumbent resolution, `EXISTS` on caller-asserted members, and the valid-from agreement
  check. Failures are `error_reply`s with stable tokens.
- **Typed exception hierarchy** rooted at `ValidityError(ValueError)`, with a token ->
  exception dispatch table so every Lua error reply has exactly one Python counterpart.
- **`SupersessionProtocol.save_and_supersede` / `.save_and_invalidate`** — the combined entry
  point. Owns its pipeline by default, so the M4–M6 shape is atomic by construction; accepts
  a caller pipeline for composition, returning a `SupersedeResult` that reports `close_index`
  rather than lying about the outcome.
- **Valid-time single writer**, enforced: the field value at construction writes valid-time;
  anything else that *asserts* a disagreeing start gets `ValidityValidFromConflictError`
  instead of losing silently to `ZADD NX`.

### Flow

**Caller has a correction** → `save_and_supersede(new, identity_key=(subject, predicate))` →
**one MULTI/EXEC** → `new`'s hash + indexes + open interval written, incumbent's interval
closed, both chain links written, pointer repointed → **`SupersedeResult(closed_key=<old key>)`**

Failure branches, all typed, none silent:

- successor names a record that does not exist at EXEC time → `ValidityMemberAbsentError`
- explicitly-named incumbent does not exist → `ValidityMemberAbsentError`
- close instant precedes the incumbent's stored start → `ValidityCloseBeforeStartError`
- asserted `valid_from` disagrees with the stored start → `ValidityValidFromConflictError`

### Technical Approach

#### D1 — `_member_key` resolves the key string only

```python
def _member_key(instance: Any) -> Optional[str]:
    """Return an instance's Redis key string, or ``None`` if it cannot be resolved.

    Resolution only. This function issues **no Redis command** — membership is
    decided inside ``SUPERSEDE_LUA`` at the instant of the write (#588). The
    ``EXISTS`` probe that used to live here answered the right question at a
    moment when the answer could not stay true: the write it guarded happens
    later, inside the script, at EXEC time. In pipeline mode "later" is
    unbounded, which is how a same-transaction successor came back as
    ``0`` and turned an ``invalidate`` into a silent no-op.

    ``"Model:None"`` (a model with an unset ``KeyField``) is returned from here
    like any other string and is rejected by the script's ``EXISTS`` check,
    because it genuinely does not exist.
    """
    try:
        member = instance.db_key.redis_key
    except (TypeError, ValueError):
        return None
    return member or None
```

`chain()` and `_walk_one()` also call `_member_key`. **`_walk_one` is unchanged in
substance** — `superseded_by`/`supersedes` on an unsaved instance now cost one `HGET` against
the chain hash for `"Model:None"`, which returns nil, so both still return `None` and
`test_validity_field.py:1604` (`superseded_by(old) is None`) is unaffected. Reads never raise.

**`chain()` is NOT unchanged, and must be fixed explicitly (round-2 BLOCKER B2).** Today
`_member_key(unsaved)` returns `None` because the `EXISTS` fails, so `chain()` short-circuits
at `supersession.py:323-324` and returns `[]`. Once `_member_key` resolves without the probe,
the anchor becomes `"ValidFact:None"`, `_walk_links` finds no links in either direction, and
the function falls through to `chain.append(instance)` — returning `[unsaved]`. That breaks
`assert SupersessionProtocol.chain(unsaved) == []` at **`test_validity_field.py:1650`**, which
sits inside `test_unsaved_contradicted_instance_degrades_with_no_partial_state` (`:1622`) —
one of the two tests D7 **freezes** and whose editing this plan defines as proof that D7 was
implemented wrong. The same assertion also sits at `:1407` (in a test we *do* replace), and
`chain()`'s own docstring promises `[]` "when … the instance is unsaved".

So the unsaved contract moves *into* `chain()` rather than being inherited from
`_member_key`, using `_walk_links`' existing dangling-link rule (a member with no `valid_from`
score is not a chain participant) rather than a reintroduced `EXISTS`. This requires hoisting
the `get_interval_keys` lookup — currently `supersession.py:330`, below the anchor gate —
above it:

```python
# SupersessionProtocol.chain, replacing supersession.py:323-330
model = type(instance)
valid_from_key, _ = ValidityField.get_interval_keys(model, resolved)

anchor = _member_key(instance)
if anchor is None:
    return []
# Membership, not resolvability. `_member_key` no longer probes (D1), so the
# "unsaved instance -> []" contract this method documents has to live here.
# `ZSCORE` rather than `EXISTS` on purpose: it is the same rule `_walk_links`
# already applies to a dangling link, so an anchor and a link are judged by
# one criterion. Read-only path; `_member_key` still issues zero commands.
if POPOTO_REDIS_DB.zscore(valid_from_key, anchor) is None:
    return []

fwd_key = ValidityField.get_chain_fwd_key(model, resolved)
rev_key = ValidityField.get_chain_rev_key(model, resolved)
```

Cost: one `ZSCORE` on a read-only traversal. The Success Criterion "`_member_key` issues zero
Redis commands" is unaffected — the command lives in `chain()`, and the command-counter test
(test 7) scopes to `_member_key`. `test_validity_field.py:1650` and `:1407` both keep passing
unedited, and a new test (test 19) pins `chain(unsaved) == []` directly rather than only as a
side assertion inside a frozen observation test.

#### D2 — The revised `SUPERSEDE_LUA` contract

```
KEYS[1] = valid_from ZSET
KEYS[2] = invalid_at ZSET
KEYS[3] = ingested_at ZSET
KEYS[4] = open-identity pointer STRING ('' for identity-free direct invalidation)
KEYS[5] = chain:fwd HASH
KEYS[6] = chain:rev HASH

ARGV[1] = new member redis_key ('' for a pure invalidate)
ARGV[2] = now (epoch seconds)
ARGV[3] = valid_from for the new member ('' -> now)
ARGV[4] = ingested_at for the new member ('' -> now)
ARGV[5] = mode: 'open' | 'supersede' | 'invalidate'
ARGV[6] = explicit close-at ('' -> now)
ARGV[7] = explicit old member, bypassing the pointer ('' -> resolve via KEYS[4])
ARGV[8] = NEW. '1' when ARGV[3] is a caller *assertion* about the new member's
          valid-time; '' or '0' otherwise. Only an assertion can conflict.
```

**Reply shapes.** The success replies are **unchanged**, and that is load-bearing:
`ProvenanceJournal._write` reads `bool(results[close_index])` (`provenance_journal.py:1157-1161`)
and `tests/test_provenance_journal.py:1300,1840` assert truthiness of the raw pipeline reply.

| Reply | Meaning |
|---|---|
| bulk string `<old_member>` | The incumbent was closed by this call |
| bulk string `''` | Nothing was closed: no incumbent, or already closed (idempotency guard) |
| error `POPOTO_VALIDITY_MEMBER_ABSENT <role> <key>` | **NEW.** `role` is `successor` or `incumbent` |
| error `POPOTO_VALIDITY_CLOSE_BEFORE_START` | Unchanged token, unchanged meaning |
| error `POPOTO_VALIDITY_VALID_FROM_CONFLICT <stored> <requested>` | **NEW.** Asserted start disagrees with the stored one |

**Encoding convention** (extends `POPOTO_UNIQUE_CONFLICT`, `indexed_field_mixin.py:168`):
the reply is a single space-separated string whose **first token** is a stable
`POPOTO_VALIDITY_*` constant and whose remaining tokens are diagnostic detail. Python-side
dispatch matches on the token as a **substring** of `str(ResponseError)`, never on equality —
Redis versions differ on whether `error_reply` output is prefixed, and the existing
`CLOSE_BEFORE_START_ERROR in str(e)` check (`validity_field.py:802`) is already
version-proof for that reason. Detail tokens are for humans and must never be parsed.

**Script structure.** The script is reorganized into a validation phase and a mutation phase,
with a hard rule stated in the comment block: *no `redis.call` that writes may appear above
the `-- MUTATION PHASE` marker.* Redis Lua has no rollback, so all-or-nothing is achieved by
ordering, not by transactions.

```lua
-- VALIDATION PHASE -- reads and error_reply only. No writes above the marker.

local vf_assert = (ARGV[8] or '') == '1'
local asserted_old = old_member ~= ''   -- ARGV[7] was supplied by the caller

if mode ~= 'open' then
  if old_member == '' and ptr_key ~= '' then
    local pointed = redis.call('GET', ptr_key)
    if pointed and pointed ~= false then old_member = pointed end
  end

  -- A caller-named successor must exist at the instant of the write. This is
  -- the whole of #588: in a pipeline the HSET has already applied by the time
  -- this script body runs inside MULTI, so a same-transaction successor is
  -- visible here even though a client-side EXISTS ahead of the queue was not.
  if new_member ~= '' and redis.call('EXISTS', new_member) == 0 then
    return redis.error_reply('POPOTO_VALIDITY_MEMBER_ABSENT successor ' .. new_member)
  end

  if old_member ~= '' then
    if redis.call('EXISTS', old_member) == 0 then
      if asserted_old then
        -- The caller named this record. A missing record is a caller error.
        return redis.error_reply('POPOTO_VALIDITY_MEMBER_ABSENT incumbent ' .. old_member)
      end
      -- Resolved from the open pointer, which is a hint and not an assertion.
      -- A pointer left naming a hard-deleted record means "no incumbent", the
      -- same reading `chain()` already gives a dangling link.
      old_member = ''
    end
  end

  if old_member ~= '' and old_member ~= new_member then
    local old_score = redis.call('ZSCORE', ia_key, old_member)
    if is_open(old_score) then
      local old_start = redis.call('ZSCORE', vf_key, old_member)
      if old_start and old_start ~= false then
        local start_num = tonumber(old_start)
        if start_num ~= nil and close_at < start_num then
          return redis.error_reply('POPOTO_VALIDITY_CLOSE_BEFORE_START')
        end
      end
      will_close = true
    end
  end
end

if new_member ~= '' and vf_assert then
  -- Valid-time has one writer. ZADD NX below would drop a disagreeing start
  -- on the floor and leave the hash and the index answering differently
  -- (#588 secondary observation, measured at 30 days).
  local stored_vf = redis.call('ZSCORE', vf_key, new_member)
  if stored_vf and stored_vf ~= false then
    local s = tonumber(stored_vf)
    if s ~= nil and s ~= valid_from then
      return redis.error_reply(
        'POPOTO_VALIDITY_VALID_FROM_CONFLICT ' .. tostring(s) .. ' ' .. tostring(valid_from))
    end
  end
end

-- MUTATION PHASE -- every check above has passed.
```

The mutation phase is the existing body with the incumbent's re-resolution and re-checking
removed (it now uses `will_close` / the already-resolved `old_member`). The `is_open`
helper, the `NX` newcomer writes, and `return closed` are unchanged.

#### D3 — Which callers assert `valid_from`

`assert_valid_from` is a new keyword on `execute_supersede`, **defaulting to `False`**, and
the mapping is deliberate:

| Caller | `assert_valid_from` | Why |
|---|---|---|
| `ValidityField.on_save`, `field_value is not None` | `True` | The field value at construction *is* the single authoritative writer. A re-save that declares a different start is the reporter's bug and must be loud. |
| `ValidityField.on_save`, `field_value is None` | `False` | No assertion was made; the save clock is a default. `NX` idempotence on re-save is preserved. |
| `SupersessionProtocol.supersede` / `.invalidate` | `False` | **Load-bearing.** `at=` is a *close-time* assertion about the incumbent, not a start-time assertion about the successor. The successor was normally saved moments earlier with its own clock, so asserting `at` for it would raise on every ordinary supersede. To set a successor's valid-time, construct it with `validity=t`. |
| `ProvenanceJournal._write` | `False` | M1 already sets valid-time at construction (`provenance_journal.py:928-940`) precisely because of this. Unchanged. |
| Direct `execute_supersede` callers | caller's choice | Documented on the method. |

This is exactly the maintainer's Q2 answer expressed as a wire flag: the *field value at
construction* remains the one writer, and any other writer that asserts a disagreeing value
gets a typed error rather than silently losing to `ZADD NX`.

#### D4 — Typed errors

New module-level constants and exceptions in `validity_field.py`:

```python
MEMBER_ABSENT_ERROR = "POPOTO_VALIDITY_MEMBER_ABSENT"
CLOSE_BEFORE_START_ERROR = "POPOTO_VALIDITY_CLOSE_BEFORE_START"   # unchanged
VALID_FROM_CONFLICT_ERROR = "POPOTO_VALIDITY_VALID_FROM_CONFLICT"


class ValidityError(ValueError):
    """Base for every typed failure raised by SUPERSEDE_LUA.

    Subclasses ``ValueError`` deliberately. Two shipped contracts depend on it:
    ``ObservationProtocol._apply_supersession`` degrades on
    ``except (TypeError, ValueError)`` (observation.py:485), and the V0 test
    suite asserts ``pytest.raises(ValueError)`` for close-before-start. Widening
    those to a new base class would be a silent behavior change on a signal path.
    """


class ValidityMemberAbsentError(ValidityError): ...
class ValidityCloseBeforeStartError(ValidityError): ...
class ValidityValidFromConflictError(ValidityError): ...
```

The dispatch table replaces the single `if CLOSE_BEFORE_START_ERROR in str(e)` branch:

```python
_LUA_ERROR_MAP = (
    (MEMBER_ABSENT_ERROR, ValidityMemberAbsentError),
    (CLOSE_BEFORE_START_ERROR, ValidityCloseBeforeStartError),
    (VALID_FROM_CONFLICT_ERROR, ValidityValidFromConflictError),
)
```

Ordered tuple, not a dict: matching is by substring and the tokens must be tested in a
declared order so a future token that is a prefix of another cannot shadow it.

All three are exported from `popoto/__init__.py` alongside `ValidityField`.

**Pipeline-mode caveat, unchanged and documented.** The remap lives on the non-pipeline
branch of `execute_supersede`. On a caller pipeline the error surfaces from `pipe.execute()`
as a raw `redis.exceptions.ResponseError`, because redis-py raises during result parsing and
`execute_supersede` has already returned. `tests/test_provenance_journal.py:1453` pins this
inverse explicitly. The new combined entry point (D6) is where a caller gets the typed
exception in pipeline shape, because it owns the `execute()`.

#### D5 — Closing the hash/index divergence

The reporter's finding is that `execute_supersede` writes the index and never the hash, so
whichever writer wins, the other writes half the record. Two halves, handled separately:

**Half 1 — both surfaces set, disagreeing.** This is the 30-day case and it is now impossible
to *write*: an asserted `valid_from` that disagrees with the stored score returns
`VALID_FROM_CONFLICT`. But an error inside `EXEC` does not roll back the `HSET` that
`Model.save()` queued at `base.py:1407` (partial save) / `:1612` (full save), so a
script-level rejection alone would still leave
the hash corrected and the index refusing — loudly, but still divergent. The check therefore
has to happen before the save writes anything, and **where** it runs is the whole of this
decision.

**[corrected — the first-pass plan got this wrong.]** It is not enough to raise from
`ValidityField.on_save`. `Model.save()` has *four* write arms and *two* write phases:

1. **Eager phase** — `base.py:1581-1607` (full save) and `base.py:1384-1394` (partial save)
   run every `IndexedFieldMixin` field's `on_save` **eagerly, with `pipeline=None`, directly
   against live Redis**, before `internal_pipeline` is even constructed. That ordering is
   deliberate and is the #476 unique-conflict fix; its rationale is written out at
   `base.py:1581-1592`.
2. **Pipelined phase** — `base.py:1609-1612` onward queues the `HSET` and then every remaining
   field's `on_save`, executing at `base.py:1678` (full save) / `base.py:1453` (partial save).

`ValidityField` is `class ValidityField(Field)` (`validity_field.py:229`), deliberately **not**
an `IndexedFieldMixin` (plan D2 of V0 — see the class docstring). Its `on_save` therefore runs
in phase 2, *after* the indexed fields have already committed their hash values and index
entries in phase 1. `JournalEntry` (`provenance_journal.py:299-310`) declares four
`IndexedField`s — `turn_id`, `speaker`, `kind`, `target` — alongside its `ValidityField`, so
the shipped reference model hits this exactly: a rejected declared re-save would commit four
indexed field values and their index entries before the validity check ever ran.

**The four arms, and why the dispatch site is a single one (round-2 BLOCKER B1).** Both eager
loops named above live inside the **`else:` arm** of an `isinstance(pipeline,
redis.client.Pipeline)` test. Verified at `d8914fc`:

| Arm | Branch test | Ends at | Eager loop? |
|---|---|---|---|
| partial + external pipeline | `base.py:1325` | `return pipeline` at `:1382` | **no** |
| partial + internal pipeline | `else:` at `:1383` | `:1453` | yes, `:1388-1404` |
| full + external pipeline | `base.py:1503` | `return pipeline` at `:1578` | **no** |
| full + internal pipeline | `else:` at `:1580` | `:1678` | yes, `:1593-1608` |

So a dispatch placed "immediately before `:1593`" and "immediately before `:1388`" — as the
round-1 revision specified — **never runs at all when the caller supplies a pipeline**, because
both external-pipeline arms have already returned. That would leave D5's external-pipeline
guarantee false, make D6's own `save_and_supersede` inert (step 4 is `new_instance.save(
pipeline=pipe)`, i.e. the external arm), and make Task 3 test 17's pipeline arm
unimplementable.

The dispatch is therefore **a single site, before the partial/full split**, immediately after
the `pre_save` gate at `base.py:1282-1292` and before `new_db_key = DB_key(self.db_key)` at
`:1294`:

```python
# popoto/models/base.py, immediately after the pre_save gate (:1289-1292)
# and before `new_db_key = ...` (:1294). ONE site, deliberately: the two eager
# indexed-field loops (:1388, :1593) both sit inside the `else:` arm of an
# external-pipeline test that returns at :1382 / :1578, so a per-loop dispatch
# would skip every caller-supplied-pipeline save -- including
# SupersessionProtocol.save_and_supersede's own (#588 round-2 B1).
_validate_names = (
    update_fields if update_fields is not None else self._meta.fields.keys()
)
for field_name in _validate_names:
    self._meta.fields[field_name].pre_save_validate(
        self,
        field_name=field_name,
        field_value=getattr(self, field_name),
        **kwargs,
    )
```

Placing it after the `pre_save` gate (rather than at the top of `save()`) is deliberate: the
never-record firewall (`:1260-1265`), the write filter (`:1269-1273`), and `pre_save`'s own
early return (`:1289-1290`) all *decline* a save by returning rather than raising. A save that
was declined must not raise a `ValidityValidFromConflictError` on its way out — declining
comes first, validating comes second.

Iterating `update_fields` on the partial-save path rather than the full field map is the
round-2 **C1(a)** fix; see the paragraph on pre-existing divergence below.

This is the same treatment #476 gave the unique-conflict window: a pre-scan ahead of both
phases. A new optional field hook, defaulting to a no-op:

```python
# popoto/fields/field.py — new, alongside on_save / on_delete / export_state
@classmethod
def pre_save_validate(cls, model_instance, field_name, field_value, **kwargs) -> None:
    """Raise to abort a save before ANY write is issued or queued.

    Runs from ONE site in ``Model.save()`` -- after the ``pre_save`` gate
    (base.py:1289-1292), before the partial/full split -- so it covers all
    four save arms, including the two external-pipeline arms that return at
    base.py:1382 / :1578 without ever reaching an eager loop. That is what
    distinguishes it from ``on_save``, which on the internal-pipeline arms has
    already let every ``IndexedFieldMixin`` field commit, and on the
    external-pipeline arms runs only as a queued command. Default: no-op.
    """
```

`Model.save()` invokes it from the single site shown above — over `update_fields` on a
partial save, over `self._meta.fields` otherwise. `ValidityField` is the only implementor in
this PR:

```python
# ValidityField.pre_save_validate
if field_value is None:
    return  # defaulted: no assertion, nothing to conflict with
try:
    declared = float(field_value)
except (TypeError, ValueError):
    return  # on_save falls back to the save clock; not an assertion either
stored = POPOTO_REDIS_DB.zscore(
    cls.get_valid_from_key(model_instance, field_name),
    model_instance.db_key.redis_key,
)
if stored is not None and float(stored) != declared:
    raise ValidityValidFromConflictError(...)
```

This is the same two-layer pattern `execute_supersede` already uses for close-before-start
(`validity_field.py:764-772`): a cheap client-side pre-check for the common case and a good
message, plus the authoritative check in the script for the race (Race 4).

**The guarantee, stated exactly.** On the **non-pipeline** save path (`base.py:1383+` partial,
`:1580+` full), a rejected declared re-save writes nothing at all — no hash field, no index
entry, no interval — because the single pre-scan site raises before phase 1. On the
**external-pipeline** path (`base.py:1325-1382` partial, `:1503-1578` full) the pre-scan raises
from the *same* site, before that branch is even entered, so `save()` queues **nothing** onto
the caller's pipeline for this call and nothing is applied. (Commands the caller queued
*before* calling `save()` are of course still theirs to discard or execute.) This is only true
because the dispatch is the single pre-split site of B1's fix; the per-eager-loop placement
would have made the external-pipeline half of this sentence false.

**Pre-existing divergence, and how an operator gets out of it (round-2 CONCERN C1).** The
guarantee above is about divergence that *cannot be newly written*. Divergence already stored
— exactly the reporter's population — needs a stated exit, or those records become permanently
unsaveable:

- **Partial saves of unrelated fields must not trip it.** The dispatch iterates `update_fields`
  when one is supplied (see the snippet above), so `obj.save(update_fields=["speaker"])` on a
  record whose `validity` diverges does not call `ValidityField.pre_save_validate` at all. This
  is C1(a), and it is the difference between "one field is stuck" and "the record is bricked".
- **A full re-save of a diverged record raises, by design, and is remediable in two lines.**
  The declared hash value and the index score genuinely disagree; the save is the moment to
  reconcile, not to pick a winner silently. The operator reads the *effective* start through
  the D5 half-2 seam and either adopts it or overwrites the index:

  ```python
  # Adopt the index (the value every as_of query already answers against):
  effective = ValidityField.get_valid_from(
      JournalEntry, "validity", member_key=obj.db_key.redis_key
  )
  obj.validity = effective
  obj.save()

  # Or make the declared value authoritative -- plain ZADD, no NX, so it
  # overwrites the score ZADD NX refused to update:
  vf_key, _ = ValidityField.get_interval_keys(JournalEntry, "validity")
  POPOTO_REDIS_DB.zadd(vf_key, {obj.db_key.redis_key: float(obj.validity)})
  obj.save()
  ```

  This recipe ships in the CHANGELOG next to the adopter fix, and a test pins the failure mode
  (Task 3, test 20) so it is a contract rather than a discovery.
- **"No data migration" is restated precisely** in Update System: no stored key, score, or hash
  field changes *shape*, and no backfill runs — but an existing record carrying a divergence
  will refuse a full re-save until reconciled. That is an operational consequence, and it is
  documented rather than papered over.

**Fallback if review rejects the `base.py` hook.** If the reviewer judges a new generic field
hook too wide a change for a bug fix (see Risk 6), the fallback is to keep the check in
`ValidityField.on_save` and **narrow the guarantee in the docs, the Success Criteria, and the
CHANGELOG to "no `ValidityField`-owned byte is written"** — acknowledging that on a model with
indexed fields, a rejected re-save leaves those fields committed. The pre-scan is preferred
because the narrowed guarantee is a half-fix for the reporter's exact complaint, which was
about two surfaces of one record disagreeing.

**Half 2 — hash nil, index set (the defaulted record).** A record saved without an explicit
event time reads back `.validity is None` while the index holds the save clock. The reporter
correctly reads this as provenance ("declared" vs "defaulted"), not as a contradiction, and
backfilling the hash from `on_save` would make `on_save` a second writer of valid-time —
exactly what the maintainer decision forbids, and a mutation an append-only M1 entry cannot
accept. So this half is **documented, not changed**, and given a read seam:

```python
@classmethod
def get_valid_from(cls, model, field_name, member_key=None) -> Optional[float]:
    """Return the record's *effective* valid-from — the index score.

    ``instance.validity`` is the **declared** value: ``None`` means "not
    declared, defaulted to the save clock". This returns what the index
    actually holds, which is what every ``as_of`` query answers against.
    """
```

#### D6 — The combined save+supersede entry point

**Why this ships now, with no consumer in this PR.** The critique is factually right that
D1+D2+D4 alone fix the issue's reproduction, and that D6's only planned caller is deferred to
#563/M4 by this plan's own No-Gos. Shipping unconsumed public API is normally the wrong
trade. It is made here anyway, for one reason: **the maintainer decision of 2026-09-02 names
it as part of the settled fix**, not as a follow-on — *"**Additionally**, add a combined
save+supersede entry point, so the shape M4–M6 want is atomic by construction instead of
composed across a pipeline by the caller."* That is settled input, so the option to cut it
does not exist at this layer.

Two things make the early ship defensible on its own terms, and they are the reason the plan
reasons the opposite way about wiring M1:

- **It is additive, not a migration.** Nothing else changes shape to accommodate it, and
  nothing existing is rewritten to call it. Wiring M1 *would* be a migration — M1's pre-flight
  carries firewall, cross-agent, and kind/target checks the protocol cannot express, so
  converging them changes shipped, tested behavior. Adding a method does not.
- **It is the only place a caller can get a typed error in pipeline shape** (D4: the
  `ResponseError` remap cannot live on `execute_supersede`'s pipeline branch). Without it,
  this PR fixes the silent no-op but leaves pipeline callers parsing raw `ResponseError`s —
  which is half of the reported signal problem left standing.

It is fully tested in this PR (Task 3, tests 16 and 18), so it does not ship unexercised.

```python
@dataclass(frozen=True)
class SupersedeResult:
    instance: Any                     # the successor, saved
    closed_key: Optional[str]         # superseded record's key; None if nothing closed
    pipeline: Optional[Pipeline]      # the caller's pipeline, unexecuted; None otherwise
    close_index: Optional[int]        # index of the queued EVAL in the caller's pipeline


@staticmethod
def save_and_supersede(new_instance, *, identity_key, at=None,
                       field_name=None, pipeline=None) -> SupersedeResult: ...

@staticmethod
def save_and_invalidate(new_instance, *, closes, at=None,
                        field_name=None, pipeline=None) -> SupersedeResult: ...
```

Behavior, mirroring `ProvenanceJournal._write`'s established shape rather than inventing one:

1. Resolve `field_name`; raise `ValueError` if the model declares no `ValidityField` (this
   entry point is explicit, so silence would be wrong — unlike `supersede`, which is also
   reached from the observation signal path).
2. Validate the pipeline if given: must be a `redis.client.Pipeline`, must have
   `transaction is True`, and must not be `watching and not explicit_transaction`. Same three
   checks and same reasoning as `provenance_journal.py:1015-1057` **[corrected]** — type check
   at `:1018-1022`, `transaction is not True` at `:1023-1028`, `watching and not
   explicit_transaction` at `:1046-1057`. (`:1197-1231`, cited in the first-pass plan, is
   `_REQUIRED_ENTRY_FIELDS` / `_require_journal_shape` and is unrelated.)
3. `pipe = POPOTO_REDIS_DB.pipeline()` when none supplied.
4. `saved = new_instance.save(pipeline=pipe)`; raise `RuntimeError` if falsy — `Model.save()`
   has early-return gates (never-record firewall, write filter) that return rather than raise,
   and queuing the close behind a record that was never written is the exact failure mode M1's
   defence-in-depth raise exists to prevent (`provenance_journal.py:1085-1102`).
5. Queue the supersede EVAL via `execute_supersede(..., pipeline=pipe, assert_valid_from=False)`,
   recording `close_index = len(pipe.command_stack)` beforehand.
6. If the caller supplied the pipeline: return `SupersedeResult(closed_key=None,
   pipeline=pipe, close_index=...)`. `closed_key=None` here means *unknown until you execute*,
   and `close_index` is how the caller learns the truth — the same honest-unknown contract as
   `AnnotationResult.target_closed=None`.
7. Otherwise `results = pipe.execute()`, remap any `ResponseError` through `_LUA_ERROR_MAP`,
   and return `SupersedeResult(closed_key=<decoded results[close_index] or None>)`.

Step 7 is the piece that dissolves the reported signal problem end to end: with the script
owning the membership decision *and* this method owning the `execute()`, "declined" is a typed
exception and "nothing to close" is `closed_key=None`, and the two are no longer the same
value.

#### D7 — `ObservationProtocol` keeps its silence, explicitly

`_apply_supersession` (`observation.py:480-486`, the `except` at `:485`) is a **signal** path: `on_context_used`
reports outcomes for a batch of memories and must not raise because one of them was never
saved. Two shipped tests pin that (`test_validity_field.py:1608,1622`), and
`on_context_used` passes an internal pipeline, so a queued EVAL that errors at `EXEC` would
surface *after* the batch's other effects had applied.

So the `EXISTS` probe does not disappear from the codebase — it **moves from `_member_key`,
where every caller inherited it, to the one caller whose documented contract is "degrade
silently"**:

```python
# observation.py::_apply_supersession, before delegating
for role, obj in (("instance", instance), ("successor", successor)):
    key = getattr(getattr(obj, "db_key", None), "redis_key", None)
    if not key or not POPOTO_REDIS_DB.exists(key):
        logger.debug("supersession: %s %r not persisted, degrading", role, key)
        return
```

This is safe *here* and nowhere else, for a reason worth writing down: the observation path
never has a same-pipeline successor. Both records are, by construction, already-saved memories
the agent was shown. The TOCTOU window that makes the probe wrong in the general case does not
exist on this path, and the alternative — a typed error escaping a telemetry callback — is
worse. The existing `except (TypeError, ValueError): pass` stays as a second layer, and it
still catches the new exceptions because they subclass `ValueError` (D4).

#### D8 — `ProvenanceJournal._write`'s failure mode changes, and is made typed

**[added by the round-2 revision pass — CONCERN C2.]** The No-Gos say M1 is unchanged. That is
true of its *design* and false of its *failure mode*, and the difference has to be on the
record.

M1 calls `execute_supersede(..., old_member=target_key, pipeline=pipe)`
(`provenance_journal.py:1126-1137`) with an **explicit** `old_member`. Under D2's new
asserted-vs-hinted rule, an explicit `old_member` is a caller *assertion*, so if the target is
hard-deleted between M1's D7 pre-flight (`:906-964`) and `EXEC`, the script now returns
`POPOTO_VALIDITY_MEMBER_ABSENT incumbent …` where it previously took the idempotent no-op
branch. Redis does not roll back the rest of the `MULTI`, so:

- the journal entry's `HSET` **commits** (it is above the EVAL in the queue), and
- `pipe.execute()` raises a raw `redis.exceptions.ResponseError` out of `_write`
  (`provenance_journal.py:1155`), which has no handler,

so the caller gets neither an `AnnotationResult` nor a typed error. The window is narrow but
real, and shipping it unexercised on an append-only model is not acceptable.

**Decision, recorded rather than deferred: the committed-entry-plus-raised-error outcome is
the intended contract, and the error is made typed.** The entry is real provenance — an
annotation genuinely was written, and suppressing it to match a failed close would lose
information an append-only journal exists to keep. What is not acceptable is the *raw*
`ResponseError`, which forces the caller to string-match Lua tokens. So `_write` gains exactly
one defensive wrapper, and nothing else:

```python
# provenance_journal.py, replacing the bare `results = pipe.execute()` at :1155
try:
    results = pipe.execute()
except ResponseError as e:                    # noqa: perf — cold path only
    # The entry HSET is above the EVAL in this MULTI and has already applied;
    # Redis does not roll back a transaction when one command errors. The
    # annotation is real and stays. Only the close failed -- surface which,
    # typed, rather than a raw Lua token string (#588 round-2 C2).
    raise _map_lua_error(e) from e
```

where `_map_lua_error` is the `_LUA_ERROR_MAP` dispatch already being extracted for D4,
exported from `validity_field.py` so both call sites share one table. If no token matches, it
re-raises the original unchanged — same rule as D4.

This does **not** contradict `test_provenance_journal.py:1453`
(`test_bypassing_the_pre_flight_surfaces_a_raw_response_error`) as long as that test drives
`execute_supersede` on a caller pipeline directly rather than through `_write`; the builder
must check which, and if it goes through `_write`, update it to expect the typed subclass and
say so in the PR description. It also does not change M1's *success* path, its pre-flight, or
its `AnnotationResult` shape — the No-Go on converting M1 to `save_and_supersede` stands.

## Failure Path Test Strategy

### Exception Handling Coverage

- `observation.py:485` `except (TypeError, ValueError): pass` — in scope, and its behavior is
  deliberately preserved. Covered by `test_validity_field.py:1608,1622` (existing, must keep
  passing unchanged) plus a **new** test asserting the `logger.debug` degradation line fires,
  so "silently degraded" is observable rather than merely asserted-by-absence.
- `supersession.py:536-541` `_hydrate`'s narrow `except` — in scope but untouched; already
  covered by the dangling-link tests.
- `validity_field.py:801-807` `except ResponseError` — rewritten into the dispatch table.
  Every token gets a test that the right subclass is raised, plus one that an *unrecognized*
  `ResponseError` still re-raises unchanged (the `raise` at `:807`).
- No new `except Exception: pass` is introduced anywhere in this change.

### Empty/Invalid Input Handling

- `_member_key` with an instance whose `db_key.redis_key` raises → `None` (existing test).
- `_member_key` returning `""` → treated as unresolvable, `None`. New test.
- `new_member=''` / `old_member=''` reaching the script → the `EXISTS` guards are skipped
  (nothing was asserted); mode `'invalidate'` with no successor still closes. Existing
  behavior, new explicit test.
- `"Model:None"` as `new_member` → `ValidityMemberAbsentError`. New test, and it is the
  literal case the removed probe existed to prevent, so it is the anti-criterion for the
  removal (see Verification).
- `ARGV[8]` absent entirely (a 7-ARGV caller) → `ARGV[8] or ''` → not asserted. New test that
  calls the raw script with 7 ARGV.

### Error State Rendering

- Every new exception message names the member key and the two disagreeing values, so the
  message alone identifies the record and the divergence. Asserted by `match=` in the tests.
- `SupersedeResult` renders the honest-unknown case (`closed_key=None` + non-`None`
  `close_index`) rather than a plausible-looking `False`. Asserted in the caller-pipeline test.

## Test Impact

**Total suite touched: 5 test files + 2 benchmark files.** Counts below are from a full
enumeration at `44abc17`; the line numbers have been re-verified and corrected at `c7fc167`
(post-#594).

Tests that **must change** (they pin the behavior this plan deliberately reverses):

- [ ] `tests/test_validity_field.py::TestFailurePaths::test_unsaved_instance_degrades_with_no_partial_state` (`:1401`) — **REPLACE.** Currently asserts `supersede(unsaved) is None` and `invalidate(unsaved) is None`. Becomes `pytest.raises(ValidityMemberAbsentError)` for both, with the same six-key "no partial state" assertions retained verbatim — the *no-write* guarantee is unchanged and is the more important half of this test.
- [ ] `tests/test_validity_field.py::TestFailurePaths::test_invalidate_with_an_unsaved_successor_is_a_no_op` (`:1419`) — **REPLACE.** Becomes `pytest.raises(ValidityMemberAbsentError)`; keeps the `invalid_at == inf` assertion (the incumbent must still be untouched).
- [ ] `tests/test_provenance_journal.py::TestAnnotationAtomicity::test_supersession_protocol_silently_no_ops_for_a_pipelined_successor` (`:1501`) — **REPLACE.** This test *is* the bug report. It inverts: the pipelined successor now produces a queued `invalidate` EVAL, `invalid_at` finite after `execute()`, and both chain hashes populated. Rename to `test_supersession_protocol_closes_a_pipelined_successor`. Its docstring must stop citing the `POPOTO_REDIS_DB.exists(...)` probe.
- [ ] `src/popoto/recipes/provenance_journal.py:1155` — **UPDATE (source, not test), per D8.**
  Wrap the bare `results = pipe.execute()` so a `ResponseError` is remapped through the shared
  `_LUA_ERROR_MAP`. New coverage in `tests/test_provenance_journal.py` (Task 3, test 21): hard-
  delete the target after `_pre_flight` returns and before `pipe.execute()`, assert
  `ValidityMemberAbsentError` **and** that the entry hash is present — the
  committed-entry-plus-raised-error contract D8 records. Also re-read
  `test_provenance_journal.py:1453` and confirm whether it drives `_write` or
  `execute_supersede` directly; only the former needs updating.
- [ ] `src/popoto/recipes/provenance_journal.py:1118-1131` — **UPDATE (source, not test).** The "use `execute_supersede`, NOT `SupersessionProtocol` (#588)" comment is now false. Rewrite it to record *why the direct call is still correct* (M1 needs `old_member` explicit and `assert_valid_from=False`; it does not need the identity pointer) rather than "the protocol is broken". Do **not** switch M1 to the protocol in this PR — see No-Gos.

Tests that **must keep passing unchanged** (the plan is designed around them):

- [ ] `tests/test_validity_field.py::TestContradictedSupersessionWiring::test_unsaved_successor_degrades_with_no_partial_state` (`:1608`) and `::test_unsaved_contradicted_instance_degrades_with_no_partial_state` (`:1622`) — the observation path must stay silent (D7). If either needs editing, D7 was implemented wrong.
- [ ] `tests/test_provenance_journal.py::TestAnnotationAtomicity::test_valid_time_is_taken_from_construction_not_from_the_supersede_argv` (`:1540`) — pins Q2's answer. Its second half asserts `raw_valid_from != requested` for a raw `execute_supersede(valid_from=...)` — that still holds because `SupersessionProtocol` and this raw shape pass `assert_valid_from=False`. **UPDATE** only to add a third arm asserting `assert_valid_from=True` raises `ValidityValidFromConflictError` on the same input.
- [ ] `tests/test_provenance_journal.py:326-336` `_supersede_mode` helper — **UPDATE.** It asserts `len(args) >= 16` and reads mode at `args[13]`. Adding ARGV[8] makes the list 17 long; the positional indices for KEYS and ARGV[1..7] are unchanged, so only the `>= 16` bound needs revisiting (it still passes, but the helper should assert the new exact length to stay a real oracle). Consumed by ~8 tests.
- [ ] `tests/test_provenance_journal.py:1369,1373,1374` — `_numkeys == 6`, `args[9]` new member, `args[15]` old member. **Unchanged** — ARGV[8] appends.
- [ ] `tests/test_provenance_journal.py:1300` (`bool(results[close_index]) is True`) and `:1840` (`assert not results[second.close_index]`) — depend on the *unchanged* reply shape. If either breaks, D2's "reply shapes are unchanged" was violated.
- [ ] `tests/test_provenance_journal.py:1453` `test_bypassing_the_pre_flight_surfaces_a_raw_response_error` — pins that the pipeline branch does **not** remap. Unchanged by design (D4).
- [ ] `tests/test_validity_field.py` return-shape assertions at `:588, :646, :1460, :1880, :1977` (re-enumerated at `c7fc167`) — all assert `closed == old.db_key.redis_key` or `is None`. Unchanged.
- [ ] `tests/test_validity_field.py::TestAtomicity::test_supersede_issues_exactly_one_mutating_call` (`:635`) and `::test_invalidate_issues_exactly_one_mutating_call` (`:655`) — count `eval` **plus `evalsha`** (PR #594) plus 13 mutating client methods. The removed `EXISTS` is a *read*, so the counts are unchanged; the new script-internal `EXISTS` is invisible to the counter. **These two tests are the proof that D1 removed a round trip rather than moving one.**
- [ ] `tests/test_validity_field.py::TestTransferRoundTrip` (`:1853-1983`, 4 tests) — `import_state` writes plain `ZADD`/`HSET`/`SET`, never through the script, so it is untouched. `:1936`'s post-import supersede now goes through the new validation phase against a restored pointer; the D2 "pointer is a hint, not an assertion" rule is what keeps it passing.

Tests **unaffected but re-run as regression** (validity consumers, no supersede mutation
under test): `tests/test_context_assembler.py::TestAssemblerValidityGating` (7),
`tests/test_decaying_sorted_field.py::TestValidityGating` (5),
`tests/test_composite_score_query.py::TestCompositeValidityMask` (6),
`tests/benchmarks/test_journal_append.py` (2, `@slow`),
`tests/benchmarks/test_defaults_sync.py` (2 — will fail if a new `Defaults` constant is added,
which this plan does not do).

**New tests: 21 total** — `tests/test_validity_field.py::TestMembershipGuardInLua` (new class,
tests 1-20) plus one in `tests/test_provenance_journal.py` (test 21). 16 came from the
first-pass plan; the round-1 critique added test 17 (eager-indexed phase) and test 18
(`save_and_invalidate`); the round-2 critique adds test 19 (`chain(unsaved) == []`, B2),
test 20 (pre-existing hash/index divergence, C1) and test 21 (M1's `MEMBER_ABSENT` failure
mode, C2).

**One new model** in `tests/test_validity_field.py`: `IndexedValidFact`, declaring one
`IndexedField` alongside a `ValidityField`. None of the existing models in that file pairs the
two, which is why the D5 gap was invisible to the first-pass test list.
Enumerated in Step 3 below.

## Rabbit Holes

- **Rewriting M1 to use the new combined entry point.** M1 ships, is tested, and has a
  pre-flight (firewall scan, cross-agent ownership, kind/target validation) the protocol
  cannot express. Converging them is a real piece of work and belongs to M4, not here.
- **Making the pipeline branch of `execute_supersede` remap `ResponseError`.** It cannot:
  redis-py raises during `pipe.execute()` result parsing, long after `execute_supersede`
  returned. `test_provenance_journal.py:1453` pins the current behavior deliberately. The
  combined entry point is the supported way to get a typed error in pipeline shape.
- **Registering `SUPERSEDE_LUA` with `SCRIPT LOAD`/`EVALSHA`.** Tempting while editing the
  script, unrelated to this bug, and it would introduce the mixed-version window that
  embedding-by-value currently makes impossible. **Superseded by PR #594**, which moves every
  Lua site in the repo onto a cached `Script` registry (`redis_db.run_lua`) — so the decision
  is made elsewhere and the call sites this plan writes must use `run_lua`. The rabbit hole
  still applies to *additional* registry work: do not extend, tune, or special-case the
  registry from this plan. See the Post-#594 Addendum under Freshness Check.
- **Backfilling the model hash with the resolved `valid_from`.** Makes `on_save` a second
  writer of valid-time (forbidden by the maintainer decision) and mutates an append-only M1
  entry after construction. D5 half 2 documents the asymmetry and adds a read seam instead.
- **Adding a seventh derived key for a record -> identity reverse lookup.** Already rejected
  in V0 (plan D1, `validity_field.py:290-314`). Nothing here needs it.
- **Epsilon-comparing floats in the valid-from conflict check.** Both sides are IEEE doubles
  that originate from the same `repr(float)` -> `tonumber` -> `ZADD` -> `ZSCORE` round trip,
  which is exact. An epsilon would create a band of silently-accepted divergence, which is
  the bug.

## Risks

### Risk 1: Gating mode `'open'` would break every save on an unusual write path

**Impact:** `ValidityField.on_save` runs in mode `'open'` with `new_member` = the record being
saved. If the script required `EXISTS(new_member)` there, any save path where the hash is
written *after* the field hooks — or not at all, as on the EVAL-only path where
`hset_mapping` is empty and the indexed-field EVALs write the hash — would start failing
every save on the model.

**Mitigation:** the `EXISTS` guards run **only in modes `'supersede'` and `'invalidate'`**,
never in `'open'`. This is stated in the script comment as a rule, not an accident: mode
`'open'` is co-transactional with the record's own hash write by construction, so there is
nothing to verify. A Verification row greps the script to assert the guard sits inside the
`mode ~= 'open'` block.

### Risk 2: The observation signal path starts raising

**Impact:** `on_context_used` is telemetry. A `ValidityMemberAbsentError` escaping it would
break callers reporting outcomes for a batch containing one stale instance, and in pipeline
mode would abort effects that had already been queued for the rest of the batch.

**Mitigation:** D7 — the `EXISTS` probe is retained on that one path, plus the existing
`except (TypeError, ValueError)`, plus the new exceptions subclassing `ValueError`. Three
layers, and `test_validity_field.py:1608,1622` must pass **unedited** as the gate.

### Risk 3: A restored open-pointer names a hard-deleted record

**Impact:** `import_state` restores open-claim pointers with a plain `SET`
(`validity_field.py:483-487`). If the pointed-at record was not carried in the same transfer,
the next supersede on that identity would resolve an incumbent that does not exist. Erroring
there would make a partial import permanently un-supersedable.

**Mitigation:** D2's asserted-vs-hinted distinction. A pointer-resolved incumbent that does
not exist means "no incumbent" (`old_member = ''`), matching how `chain()` already reads a
dangling link. Only an incumbent named explicitly in ARGV[7] — a caller assertion — errors.
Covered by a dedicated test.

### Risk 4: `ARGV[8]` breaks a caller that passes 7 ARGV

**Impact:** any code path or test that hand-evals `SUPERSEDE_LUA`.

**Mitigation:** `ARGV[8] or ''` (the nil-safety convention already used for every KEYS/ARGV
read, `validity_field.py:154-156`) degrades to "not asserted", which is today's behavior. A
grep confirms `execute_supersede` is the only non-test `eval` site (after PR #594, grep for
`run_lua` as well — see the Post-#594 Addendum). A test calls the raw
script with 7 ARGV and asserts identical behavior.

### Risk 5: One extra `EXISTS` per supersede inside the script

**Impact:** `SupersessionProtocol.supersede` was 1 client `EXISTS` + 1 `EVAL`; it becomes
1 `EVAL` containing up to 2 server-side `EXISTS`. Net: one fewer network round trip, two more
in-script key lookups.

**Mitigation:** strictly faster on the wire, and `TestAtomicity`'s call counters
(`test_validity_field.py:635,655`) prove the round trip is gone rather than moved. After PR
#594 those counters sum `eval` **and** `evalsha`; the contract is unchanged but the assertion
shape is not — see the Post-#594 Addendum. The
benchmark gates in `tests/benchmarks/test_journal_append.py` are re-run as the p50 check.

### Risk 6: The D5 pre-scan adds a generic hook to `Model.save()`

**Impact:** `Field.pre_save_validate` is invoked on **every** save of **every** model, from a
single site above the partial/full split — so it now covers the two external-pipeline arms as
well (B1), which is *more* reach than the round-1 placement, not less. A bug in the dispatch
(not in `ValidityField`'s implementation) would affect the whole ORM, and the blast radius of
this bug fix widens from two field modules to `models/base.py`. The single site is also what
makes the failure mode uniform: there is one place to read, one place to break, and one
`grep` (the two new ANTI rows) that proves it did not get duplicated back into the eager
loops.

**Mitigation:** the default implementation is a bare `return None` on `Field`, so for every
model that does not declare a `ValidityField` the added work is one `isinstance`-free method
call per field and no Redis command — measurable against
`tests/benchmarks/test_journal_append.py`'s existing per-append EVAL-count and p50 budgets,
which are re-run as a gate. `ValidityField.pre_save_validate` issues **at most one `ZSCORE`,
and only when the field value is a declared, numeric, non-`None` value** — a defaulted save
returns before touching Redis, so the common path costs nothing. Risk 5's call-count tests
(`test_validity_field.py:635,655`) bound the total.

**If review rejects this:** D5 states the fallback explicitly (keep the check in `on_save`,
narrow the guarantee to "no `ValidityField`-owned byte", reword the Success Criterion, the
CHANGELOG, and the feature docs to match). The fallback is a documented half-fix, not a
silent one.

## Race Conditions

### Race 1: The check-then-write window this issue is about

**Location:** `src/popoto/fields/supersession.py:391` (the `EXISTS`) versus
`src/popoto/fields/validity_field.py:210-218` (the write it guards).
**Trigger:** anything that delays the write relative to the check — a caller pipeline
(unbounded delay, 100% reproduction rate), or ordinary scheduling in immediate mode
(millisecond window). A record deleted between check and write produces the inverse: an
interval opened for a record that no longer exists.
**Data prerequisite:** the successor's hash must exist at the moment the interval opens.
**State prerequisite:** none beyond that.
**Mitigation:** the check moves inside the script. Redis executes a Lua script atomically
against the keyspace, so check and write are the same instant by construction. This is the
whole plan.

### Race 2: Two writers supersede the same identity concurrently

**Location:** `SUPERSEDE_LUA` mutation phase.
**Trigger:** two `supersede` calls on one `identity_key`.
**Mitigation:** unchanged from V0 (plan Race 1). The idempotency guard (`is_open(old_score)`)
means the second close is a no-op and returns `''`. The new validation phase runs *before* the
guard, so a second writer whose successor is valid still gets the idempotent no-op rather than
an error — which is correct: nothing was wrong with its request.

### Race 3: Save and supersede interleave for the same record

**Location:** `on_save` mode `'open'` versus a concurrent `supersede`.
**Mitigation:** unchanged from V0 (plan Race 2). The `ZSCORE invalid_at` / `NX` guards still
prevent a save from resurrecting a closed record. D3 keeps `on_save`'s defaulted path
non-asserting, so a re-save of a closed record still cannot error its way into resurrection.

### Race 4: New — a declared re-save races another writer's `valid_from`

**Location:** the D5 client-side pre-check (`ZSCORE`) versus the script's authoritative check.
**Trigger:** two processes re-saving the same record with different declared `validity` values.
**Data prerequisite:** a stored `valid_from` score for the member.
**State prerequisite:** none.
**Mitigation:** the client-side `ZSCORE` is explicitly a *pre-check for a good error message
and to protect the queued HSET*, not the boundary. The authoritative comparison is in the
script under `ARGV[8] == '1'`, evaluated atomically. A loser gets
`ValidityValidFromConflictError` from `execute()`; the winner's start stands. The pre-check
racing is harmless — it can only produce a false negative, which the script then catches.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #563] Converting `ProvenanceJournal._write` to `save_and_supersede`. M4 is
  the consumer that will exercise the combined entry point; M1's pre-flight has firewall and
  cross-agent checks the protocol does not express, and merging them is M4's design work, not
  this PR's. This PR makes exactly two edits to `provenance_journal.py`, both narrow: it
  corrects M1's now-false `#588` source comment, and per **D8** it wraps the bare
  `pipe.execute()` at `:1155` so the newly-reachable `MEMBER_ABSENT` failure surfaces as a
  typed exception instead of a raw `ResponseError`. M1's pre-flight, its success path, its
  `AnnotationResult` shape, and its direct `execute_supersede` call are all unchanged.
- [SEPARATE-SLUG #584] Anything touching `popoto.integrations` DB selection. Sequenced
  alongside this issue by the maintainer decision, separate plan, separate PR.
- [ORDERED] Merging ahead of #576/PR #593 and #584/PR #594. **Both have now merged** (#593 as
  `07b7268`, #594 as `16aa702`), so this plan's branch must be cut from `origin/main` at
  `c7fc167` or later. The maintainer order was #576 → #588/#584 → #563; the first two legs are
  done and #563 (M4) still follows this plan.

Nothing else is deferred. The combined entry point, the typed exception hierarchy, the
hash/index divergence, and the full test rewrite are all in scope for this plan.

## Update System

No update system changes required — this is a library-internal change to an embedded Lua
string and two Python modules. No new dependency, no new config file, no new environment
variable, and no `Defaults` constant (deliberately: `tests/benchmarks/test_defaults_sync.py`
would require an exemption-list edit, and there is no tunable here — the guard is not
optional).

**No data migration, stated precisely** (round-2 C1). No stored key, score, or hash field
changes shape; existing intervals, chains, and pointers are read and written identically; no
backfill or rewrite step runs at upgrade. What *does* change is an operational consequence: a
record that already carries a hash/index `valid_from` divergence will refuse a **full**
`save()` with `ValidityValidFromConflictError` until an operator reconciles it. Partial saves
(`update_fields=[...]`) of unrelated fields are unaffected, because the `pre_save_validate`
dispatch is scoped to `update_fields` on that path. The two remediation recipes are in D5 and
are reproduced in the CHANGELOG.

## Agent Integration

No agent integration required — `SupersessionProtocol` and `ValidityField` are ORM-layer
primitives with no MCP surface. The MCP server (`src/popoto/mcp/`) exposes model CRUD and
retrieval, not validity mutation, and this plan adds no tool. The new
`save_and_supersede` / `save_and_invalidate` methods are exported from `popoto/__init__.py`
via the already-exported `SupersessionProtocol` class, so no `__all__` change is needed for
them; the three new exception classes **do** need `__all__` entries.

## Documentation

### Feature Documentation

- [ ] Update `docs/features/validity-and-supersession.md`:
  - Rewrite the membership-guard section: the guard is server-side, evaluated at write time.
  - New "Typed errors" section documenting all four reply tokens, their exception classes, and
    the pipeline-mode caveat (raw `ResponseError` from `execute()`).
  - New "Same-transaction successor" section with the working example from the issue —
    the shape that was broken is now the recommended one.
  - New "Valid-time has one writer" section: construct with `validity=t`; `at=` is close-time;
    `.validity` is the *declared* value and `get_valid_from()` is the *effective* one.
  - New "Reconciling a record that already diverges" subsection (round-2 C1): both two-line
    remediations, and the note that partial saves of unrelated fields are unaffected.
  - Note in the traversal section that `chain()` returns `[]` for an unsaved instance and that
    the guard now lives in `chain()` itself, not in `_member_key` (B2).
- [ ] Update `docs/features/provenance-journal.md`: replace the "#588 trap" note with a
  pointer to the fixed protocol and to `save_and_supersede`, explain why M1 still calls
  `execute_supersede` directly, and document D8's failure mode — a target hard-deleted between
  pre-flight and `EXEC` now raises `ValidityMemberAbsentError` from `_write` while the journal
  entry commits.
- [ ] Confirm `docs/features/README.md` index entries still describe both pages accurately.

### CHANGELOG (breaking change — the library is on PyPI at 1.8.2, this ships in 1.9.0)

- [ ] Add a `### Breaking` block under `## [Unreleased]` in `CHANGELOG.md`:
  - `SupersessionProtocol.supersede` / `.invalidate` **raise `ValidityMemberAbsentError`**
    (a `ValueError` subclass) where they previously returned `None` for a member that does not
    exist. **Adopter fix:** replace `if result is None:` with
    `except ValidityMemberAbsentError:` — code testing `result is None` to mean "declined"
    breaks silently otherwise, because `None` now means only "nothing was closed".
  - The same-pipeline successor shape that silently no-opped now works and is the recommended
    spelling; include the issue's four-line reproduction as the example.
  - A declared `validity=` that disagrees with an already-stored valid-from now raises
    `ValidityValidFromConflictError` instead of silently losing to `ZADD NX` (the reporter's
    30-day hash/index divergence). State the guarantee precisely per D5, including the
    external-pipeline caveat.
  - New public names: `ValidityError`, `ValidityMemberAbsentError`,
    `ValidityCloseBeforeStartError`, `ValidityValidFromConflictError`,
    `SupersessionProtocol.save_and_supersede` / `.save_and_invalidate`, `SupersedeResult`,
    `ValidityField.get_valid_from`.
  - **Remediating a record that already diverges** (D5 / round-2 C1): a full `save()` of such a
    record now raises. Include both two-line exits verbatim — adopt the effective start via
    `ValidityField.get_valid_from(Model, "validity", member_key=...)`, or make the declared
    value authoritative with a plain `ZADD` (no `NX`) before re-saving. Note that a partial
    save (`update_fields=[...]`) of an unrelated field is unaffected.
  - `ProvenanceJournal._write` now raises `ValidityMemberAbsentError` (was: a raw
    `redis.exceptions.ResponseError`) if its annotation target is hard-deleted between the
    pre-flight and `EXEC`; the journal entry still commits (D8).
  - **No data migration** — no stored key, score, or hash field changes shape, and no backfill
    runs. Records with a pre-existing divergence refuse a *full* re-save until reconciled; see
    the remediation bullet above.

### External Documentation Site

- [ ] `mkdocs build --strict` passes (both pages are in the nav).
- [ ] Any `mkdocs.yml` nav entry unchanged — no new page.

### Inline Documentation

- [ ] `_member_key` docstring fully rewritten (it currently *teaches* the removed probe as the
  reason the unsaved path is a true no-op — the most misleading comment in the file after this
  change).
- [ ] `SUPERSEDE_LUA`'s header comment block: the KEYS/ARGV contract gains ARGV[8], the
  numbered Logic list gains the validation phase, and the "no writes above the marker" rule is
  stated explicitly with its reason (Redis Lua has no rollback).
- [ ] `execute_supersede` docstring: `assert_valid_from`, the four `Raises:` entries, and the
  pipeline-mode remap caveat.
- [ ] `supersession.py` module docstring: the "Graceful degradation, narrowly scoped" bullet
  (`:30-34`) is now wrong — unsaved instances raise. Rewrite it.
- [ ] `validity_field.py` module docstring: add `EXISTS` to the Valkey-safe command list at
  `:30-32`.

## Success Criteria

- [ ] The issue's exact reproduction — `e2.save(pipeline=pipe)` then
      `SupersessionProtocol.invalidate(e1, superseded_by=e2, pipeline=pipe)` then
      `pipe.execute()` — closes `e1`, writes both chain links, and leaves
      `filter(validity__current=True) == [e2]`.
- [ ] `SupersessionProtocol.supersede`/`.invalidate` raise `ValidityMemberAbsentError` for a
      member that does not exist, in **both** immediate and (via `save_and_supersede`) pipeline
      mode, with identical outcomes.
- [ ] `"Model:None"` as a successor is rejected by the script, with no interval, chain link, or
      pointer written — verified by asserting all six keys are untouched.
- [ ] `_member_key` issues zero Redis commands (asserted with a command counter).
- [ ] An asserted `valid_from` that disagrees with the stored start raises
      `ValidityValidFromConflictError`; the reporter's 30-day divergence cannot be written.
- [ ] After a rejected declared re-save on the **non-pipeline** path, nothing was written at
      all: the record's hash and index still agree, **and** a sibling `IndexedField` on the
      same model still holds its pre-save value with no new index entry (D5 half 1 — this is
      the criterion the eager-indexed phase at `base.py:1581-1607` would otherwise defeat).
      On the **external-pipeline** path the same criterion holds and for the same reason: the
      single pre-split dispatch site raises before `save()` queues anything for this call.
- [ ] `SupersessionProtocol.chain(unsaved) == []` and the two D7-frozen tests pass unedited
      (B2) — the unsaved-instance contract survives the removal of `_member_key`'s probe.
- [ ] A record carrying a **pre-existing** hash/index divergence still accepts a partial save
      of an unrelated field, raises `ValidityValidFromConflictError` on a full save, and
      accepts a full save after either documented remediation (C1).
- [ ] `ProvenanceJournal._write` raises `ValidityMemberAbsentError` — not a raw
      `ResponseError` — when its target is hard-deleted between pre-flight and `EXEC`, and the
      journal entry is present afterwards (C2/D8).
- [ ] `save_and_supersede` performs the save and the close in one MULTI/EXEC (exactly one
      `pipe.execute()`, `transaction is True`, zero mutating commands issued outside it).
- [ ] All three Lua error tokens map to their typed exception; an unrecognized `ResponseError`
      re-raises unchanged.
- [ ] `test_validity_field.py:1608` and `:1622` pass **unedited** (anti-criterion row, proven
      red at `44abc17`; the grep is name-anchored, not line-anchored, so it survives the drift).
- [ ] `CHANGELOG.md` carries a `### Breaking` entry under `## [Unreleased]` naming
      `SupersessionProtocol.supersede`/`.invalidate`, `ValidityMemberAbsentError`, and the
      one-line adopter fix — the library is published on PyPI at 1.8.2 and this ships in
      1.9.0.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)
- [ ] All modified tests are hard assertions — no `xfail`, no `skip`.

## Team Orchestration

### Team Members

- **Builder (lua-and-field)**
  - Name: `validity-builder`
  - Role: `validity_field.py` — script contract, exceptions, `execute_supersede`, `on_save`
  - Agent Type: builder
  - Domain: Redis/Popoto data
  - Resume: true

- **Builder (protocol)**
  - Name: `protocol-builder`
  - Role: `supersession.py` + `observation.py` — `_member_key`, combined entry point, D7 probe
  - Agent Type: builder
  - Domain: Redis/Popoto data
  - Resume: true

- **Test engineer**
  - Name: `validity-tester`
  - Role: rewrite the 3 inverted tests, add `TestMembershipGuardInLua`
  - Agent Type: test-engineer
  - Resume: true

- **Validator**
  - Name: `validity-validator`
  - Role: verify blast radius — the five consumer test files and both benchmark files
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: `validity-docs`
  - Role: the two feature pages plus the inline-doc checklist
  - Agent Type: documentarian
  - Resume: true

### Available Agent Types

Tier 1 as listed in the plan template. Both builders carry `Domain: Redis/Popoto data` from
`DOMAIN_FRAMING.md`; the Lua work in particular must be read against
`validity_field.py:30-32`'s Valkey constraint on every edit.

## Step by Step Tasks

### 1. Script contract, exceptions, and `execute_supersede`
- **Task ID**: build-lua
- **Depends On**: none
- **Validates**: `tests/test_validity_field.py`
- **Assigned To**: validity-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `MEMBER_ABSENT_ERROR`, `VALID_FROM_CONFLICT_ERROR`, the `ValidityError` hierarchy, and
  `_LUA_ERROR_MAP` to `validity_field.py`; export all three exceptions from `popoto/__init__.py`
  and add them to `__all__`.
- Restructure `SUPERSEDE_LUA` into validation and mutation phases per D2, with the
  `-- MUTATION PHASE` marker and the "no writes above the marker" rule in the comment block.
  Guards apply only when `mode ~= 'open'` (Risk 1).
- Implement the asserted-vs-hinted incumbent rule (Risk 3) and the `ARGV[8]` valid-from
  conflict check.
- Update the header comment block: KEYS/ARGV contract, the numbered Logic list, the reply
  table, and `EXISTS` added to the Valkey-safe command list in the module docstring.
- Keep both `execute_supersede` script invocations on `redis_db.run_lua` (`redis_db.py:613`),
  the cached-`Script` seam PR #594 introduced — **never reintroduce a raw `client.eval(...)`**;
  a build that does will pass its own tests and silently regress the registry.
- Add `assert_valid_from: bool = False` to `execute_supersede`, append ARGV[8], and replace the
  single `if CLOSE_BEFORE_START_ERROR in str(e)` branch with the ordered dispatch table;
  preserve the bare `raise` for unrecognized errors.
- Add `Field.pre_save_validate` (default no-op) to `fields/field.py` and dispatch it from
  **exactly one site** in `models/base.py`: immediately after the `pre_save` gate
  (`base.py:1289-1292`) and before `new_db_key = DB_key(self.db_key)` (`:1294`) — i.e. above
  the `if update_fields is not None:` split at `:1296`. Iterate `update_fields` when one is
  supplied, `self._meta.fields` otherwise (C1(a)). **Do not** place it at the two eager loops
  (`:1388`, `:1593`) as the round-1 plan said: both sit inside the `else:` arm of an
  external-pipeline test that returns at `:1382` / `:1578`, so that placement silently skips
  every caller-supplied-pipeline save, including `save_and_supersede`'s own (D5, round-2 B1).
  Implement the hook on `ValidityField` per D5 half 1. Pass `assert_valid_from` per D3.
  This is the one edit outside the two field modules; keep it to the dispatch call plus the
  hook definition, and take the D5 fallback rather than growing it if review objects.
- **Per-task verification for the dispatch site** (run before handing off to Task 2):
  `grep -n 'pre_save_validate' src/popoto/models/base.py` must return **exactly one** line, and
  its number must be `> 1292` and `< 1296` (offset by the lines this task adds). A second hit
  means the round-1 two-site placement crept back in.
- Extract the `_LUA_ERROR_MAP` dispatch into a module-level `_map_lua_error(e)` helper in
  `validity_field.py` (used by `execute_supersede`'s non-pipeline branch and, per D8, by
  `ProvenanceJournal._write`). It re-raises the original unchanged when no token matches.
- Add `ValidityField.get_valid_from()` (D5 half 2).
- Add `scripts/check_supersede_lua_phases.py` — the executable anti-criterion for the phase
  rule. It parses `SUPERSEDE_LUA`, prints `BAD` when the `MUTATION PHASE` marker is missing,
  when any `ZADD`/`HSET`/`SET`/`ZREM`/`HDEL`/`DEL` `redis.call` appears above it, or when the
  first `EXISTS` is not inside the `mode ~= 'open'` block; prints `OK` otherwise. **Red-state
  proven at `44abc17`**: the same logic prints `BAD: no MUTATION PHASE marker` against the
  pre-change script, so the row cannot false-pass.

### 2. Protocol layer, combined entry point, observation path
- **Task ID**: build-protocol
- **Depends On**: build-lua
- **Validates**: `tests/test_validity_field.py`, `tests/test_provenance_journal.py`
- **Assigned To**: protocol-builder
- **Agent Type**: builder
- **Parallel**: false
- Rewrite `_member_key` per D1, including the full docstring rewrite.
- Move the unsaved-instance contract into `SupersessionProtocol.chain` per D1's B2 fix: hoist
  the `get_interval_keys` lookup (`supersession.py:330`) above the anchor gate (`:323-324`) and
  add the `zscore(valid_from_key, anchor) is None -> []` guard. `_walk_one` is left alone.
  **Verification for this bullet:** `tests/test_validity_field.py:1650` and `:1407` must pass
  with `chain(unsaved) == []` unedited.
- Rewrite the `supersession.py` module docstring's "Graceful degradation, narrowly scoped"
  bullet and the `Returns:`/`Raises:` blocks on `supersede` and `invalidate`.
- Add `SupersedeResult`, `save_and_supersede`, `save_and_invalidate` per D6, including the
  three pipeline validations and the falsy-save `RuntimeError`.
- Add the D7 `EXISTS` probe and `logger.debug` to `observation.py::_apply_supersession`, with
  the comment explaining why it is correct on that path and nowhere else.
- Correct the false `#588` comment at `provenance_journal.py:1118-1131`; do **not** change M1's
  pre-flight, success path, or `AnnotationResult` shape.
- Per D8, wrap `results = pipe.execute()` (`provenance_journal.py:1155`) in
  `try/except ResponseError: raise _map_lua_error(e) from e`, and record in the comment that
  the entry hash has already committed by then. Re-read
  `test_provenance_journal.py:1453` and update it **only** if it drives `_write` rather than
  `execute_supersede` directly.

### 3. Tests
- **Task ID**: build-tests
- **Depends On**: build-protocol
- **Validates**: `tests/test_validity_field.py`, `tests/test_provenance_journal.py`
- **Assigned To**: validity-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- REPLACE the three inverted tests listed in Test Impact; UPDATE `_supersede_mode` and
  `test_valid_time_is_taken_from_construction_not_from_the_supersede_argv`.
- Add `TestMembershipGuardInLua` to `tests/test_validity_field.py`:
  1. same-pipeline successor closes the incumbent (the issue's reproduction, verbatim)
  2. **pipeline/immediate parity**: the same supersede run both ways produces byte-identical
     state across all six keys (the parity assertion, not two separate assertions)
  3. **pipeline/immediate parity on failure**: an absent successor raises
     `ValidityMemberAbsentError` in immediate mode and, via `save_and_supersede` with a caller
     pipeline, at `execute()` — with the same six keys untouched in both
  4. `"Model:None"` successor rejected, six keys untouched
  5. absent explicit `old_member` (ARGV[7]) raises `ValidityMemberAbsentError`
  6. absent pointer-resolved incumbent is treated as "no incumbent", returns `None`, no error
  7. `_member_key` issues zero Redis commands (command counter over the client)
  8. `_member_key("")` and a raising `db_key` both return `None`
  9. mode `'open'` is never guarded: save on a model whose every field is indexed still opens
  10. raw script call with 7 ARGV behaves identically to `ARGV[8]=''`
  11. asserted `valid_from` disagreement raises `ValidityValidFromConflictError`
  12. asserted `valid_from` *agreement* does not raise (equality is exact, no epsilon)
  13. the reporter's scenario end to end: defaulted save, then declared re-save with a
      30-day-earlier event time → raises, and hash + index still agree afterwards
  14. unasserted `valid_from` disagreement is still an `NX` no-op (Q2's answer, unchanged)
  15. each of the three tokens maps to its exception; an unrecognized `ResponseError` re-raises
  16. `save_and_supersede` atomicity: one `execute()`, `transaction is True`, zero mutating
      commands outside the pipeline; and with a caller pipeline, `closed_key is None` with a
      non-`None` `close_index` that reads truthy from the caller's own results
  17. **the eager-indexed-phase test (BLOCKER 1).** A new model declaring one `IndexedField`
      *and* one `ValidityField`. Save it with `validity=t0` and an indexed value `"a"`; re-save
      with `validity=t0 - 30*86400` and indexed value `"b"`; assert it raises
      `ValidityValidFromConflictError` **and** that the hash still reads `"a"`, that the
      `$IndexedF:` index still points at `"a"` and has no `"b"` entry, and that the interval
      triple is byte-identical to before. Then the pipeline-path arm: same conflict against an
      external pipeline, assert the raise happens before `execute()` and that discarding the
      pipeline leaves the keyspace untouched. Red-state note for the builder: run this test
      against a D5 implementation that lives in `on_save` instead of `pre_save_validate` and
      confirm it FAILS on the `"a"`/`"b"` assertion — that failure is the proof the pre-scan is
      load-bearing, and it belongs in the PR description.
  18. `save_and_invalidate` end to end: successor saved and incumbent closed in one
      MULTI/EXEC, both chain links written, `closed_key` == the incumbent's key.
  19. **`chain(unsaved) == []` (round-2 BLOCKER B2).** Direct assertion, not inherited from a
      frozen observation test: `SupersessionProtocol.chain(ValidFact(name="never-saved")) == []`,
      plus `superseded_by(unsaved) is None` and `supersedes(unsaved) is None` (which hold via
      `_walk_one`'s nil `HGET`, not via a probe). Red-state note for the builder: run this
      against a D1 implementation that does *not* carry the `chain()` `ZSCORE` gate and confirm
      it returns `[unsaved]` — that failure is the proof the gate is load-bearing, and
      `test_validity_field.py:1650` inside the frozen test is the second witness.
  20. **Pre-existing hash/index divergence (round-2 CONCERN C1).** Construct the divergence
      directly (save a record, then `ZADD` the `valid_from` key to a value 30 days from the
      hash's, with no `NX`). Assert three things: (a) `obj.save(update_fields=["<some other
      field>"])` **succeeds** — the partial-save dispatch scopes to `update_fields`; (b) a full
      `obj.save()` raises `ValidityValidFromConflictError`; (c) after applying either
      remediation from D5 (adopt `get_valid_from()`, or plain `ZADD` the declared value), the
      full save succeeds and hash and index agree. This is the operator's exit, pinned.
  21. **M1's `MEMBER_ABSENT` failure mode (round-2 CONCERN C2)** — in
      `tests/test_provenance_journal.py`, not in `TestMembershipGuardInLua`. Hard-delete the
      annotation target after `_pre_flight` returns and before `pipe.execute()` (monkeypatch the
      pre-flight's return, or `POPOTO_REDIS_DB.delete(target_key)` from a patched seam). Assert
      `pytest.raises(ValidityMemberAbsentError)` from `_write` **and** that the entry hash
      exists afterwards — the committed-entry-plus-typed-error contract D8 records. A test that
      only asserts the raise would let a future change silently start swallowing the entry.
- Add the D7 degradation-logging test (`caplog` on `POPOTO.ObservationProtocol`).

### 4. Blast-radius validation
- **Task ID**: validate-blast-radius
- **Depends On**: build-tests
- **Assigned To**: validity-validator
- **Agent Type**: validator
- **Parallel**: false
- Run, and report counts against a baseline measured at the branch point (`d8914fc` or
  later — record the SHA in the report) in the *same* environment
  (CLAUDE.md worktree rule — state redis-py version alongside every number):
  `tests/test_validity_field.py`, `tests/test_provenance_journal.py`,
  `tests/test_context_assembler.py`, `tests/test_decaying_sorted_field.py`,
  `tests/test_composite_score_query.py`, `tests/benchmarks/`.
- Confirm `test_validity_field.py:1608` and `:1622` are **byte-identical** to their baseline.
- Confirm `TestAtomicity`'s two call-count tests still pass with the same counts.
- Run `mypy src/` and report the base-vs-branch delta with the redis-py version stated.

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-blast-radius
- **Assigned To**: validity-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Execute the Documentation checklist above, **including the `CHANGELOG.md` `### Breaking`
  block** — it is the only adopter-facing record of a breaking change to a published library,
  and its two Verification rows are red at `44abc17`.
- Run `mkdocs build --strict`.

### 6. Final validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: validity-validator
- **Agent Type**: validator
- **Parallel**: false
- Run every row in Verification and report pass/fail per row.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Validity suite passes | `pytest tests/test_validity_field.py -q` | exit code 0 |
| Journal suite passes | `pytest tests/test_provenance_journal.py -q` | exit code 0 |
| Validity consumers pass | `pytest tests/test_context_assembler.py tests/test_decaying_sorted_field.py tests/test_composite_score_query.py -q` | exit code 0 |
| Full suite passes | `pytest -q` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Types clean | `mypy src/` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| ANTI: no client-side EXISTS left in `_member_key` | `sed -n '/^def _member_key/,/^def _resolve_field_name/p' src/popoto/fields/supersession.py \| grep -c 'POPOTO_REDIS_DB'` | match count == 0 |
| ANTI: validation phase writes nothing, and EXISTS is inside the non-`'open'` block | `python scripts/check_supersede_lua_phases.py` | output contains OK |
| ANTI: no Redis-module command in the script | `python -c "s=open('src/popoto/fields/validity_field.py').read();b=s.split('SUPERSEDE_LUA = \"\"\"')[1].split('\"\"\"')[0];print(sum(b.count(t) for t in ('BF.','CMS.','TS.','TOPK.','JSON.')))"` | output contains 0 |
| ANTI: M1's false `#588` comment is gone | `grep -c 'SupersessionProtocol resolves its member keys through' src/popoto/recipes/provenance_journal.py` | match count == 0 |
| ANTI: the two D7-frozen tests are unedited | `git diff -U0 origin/main -- tests/test_validity_field.py \| grep -cE '^[+-].*def test_unsaved_(successor\|contradicted_instance)_degrades_with_no_partial_state\('` | match count == 0 |
| CHANGELOG records the breaking change | `grep -c 'ValidityMemberAbsentError' CHANGELOG.md` | output > 0 |
| CHANGELOG names the adopter fix | `grep -c 'result is None' CHANGELOG.md` | output > 0 |
| Combined entry point exists and is exported | `python -c "import popoto;print(hasattr(popoto.SupersessionProtocol,'save_and_supersede') and hasattr(popoto.SupersessionProtocol,'save_and_invalidate'))"` | output contains True |
| Typed exceptions exported | `python -c "import popoto;print(all(hasattr(popoto,n) for n in ('ValidityError','ValidityMemberAbsentError','ValidityCloseBeforeStartError','ValidityValidFromConflictError')))"` | output contains True |
| Exceptions subclass ValueError | `python -c "import popoto;print(issubclass(popoto.ValidityMemberAbsentError,ValueError))"` | output contains True |
| No stale xfails | `grep -rn 'xfail\|@pytest.mark.skip' tests/test_validity_field.py tests/test_provenance_journal.py` | exit code 1 |
| ARGV[8] is nil-safe | `grep -c "ARGV\[8\] or ''" src/popoto/fields/validity_field.py` | output > 0 |
| No new Defaults constant | `git diff origin/main -- src/popoto/fields/constants.py \| grep -c '^+.*VALIDITY'` | match count == 0 |
| ANTI: `pre_save_validate` is dispatched from exactly one site, above the partial/full split (B1) | `grep -n 'pre_save_validate' src/popoto/models/base.py` | exactly one line, numbered above the `if update_fields is not None:` split |
| ANTI: the dispatch is not inside either eager-loop arm (B1) | `python -c "import re;s=open('src/popoto/models/base.py').read().split(chr(10));i=[n for n,l in enumerate(s,1) if 'pre_save_validate' in l];j=[n for n,l in enumerate(s,1) if 'if update_fields is not None:' in l];print(i[0]<j[0])"` | output contains True |
| `chain()` keeps the unsaved contract (B2) | `pytest tests/test_validity_field.py -q -k 'chain or degrades_with_no_partial_state'` | exit code 0 |
| M1 surfaces a typed error, not a raw ResponseError (C2/D8) | `grep -c '_map_lua_error' src/popoto/recipes/provenance_journal.py` | output > 0 |
| CHANGELOG carries the divergence remediation recipe (C1) | `grep -c 'get_valid_from' CHANGELOG.md` | output > 0 |

### Red-state proof (run at `44abc17`, critique revision pass)

Every anti-criterion above was executed against the unmodified tree to confirm it detects the
violation rather than false-passing on an errored command. Paste this table into the PR
description as the paper trail.

| Row | Result at `44abc17` | Reading |
|---|---|---|
| no client-side EXISTS in `_member_key` | `1` | RED — the probe is present, as expected pre-fix |
| validation phase writes nothing | `BAD: no MUTATION PHASE marker` | RED — marker absent pre-fix |
| M1's false `#588` comment is gone | `1` | RED — the comment is present pre-fix |
| combined entry point exported | `False` | RED |
| ARGV[8] is nil-safe | `0` | RED |
| CHANGELOG records the breaking change | `0` | RED |
| CHANGELOG names the adopter fix | `0` | RED |
| no Redis-module command in the script | `0` | GREEN pre-fix — a true invariant, not a progress marker |
| No new Defaults constant | `0` | GREEN pre-fix — same |

The **two D7-frozen tests** row (BLOCKER 2's fix) was proven three ways, because the row it
replaces went red on a correct implementation:

| Scenario | Count | Reading |
|---|---|---|
| clean tree | `0` | PASSES — correct baseline |
| a frozen test (`:1595` or `:1609`) edited | `2` | FAILS — the row actually detects the violation |
| `:1388` replaced, as Test Impact mandates | `0` | PASSES — the Verification/Test Impact contradiction is gone |

Reproduced by temporarily renaming the relevant `def` lines in a scratch copy and restoring
the file; `git status` was verified clean afterwards.

## Critique Results

<!-- Populated by /do-plan-critique (war room), 2026-09-02, FULL depth (3 critics). -->

**Verdict: NEEDS REVISION** — 2 blockers, 2 concerns, 2 nits.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Risk & Robustness | D5's "Raising from `on_save` therefore aborts before a single byte is written" is false for any model that also carries an `IndexedFieldMixin` field. `base.py:1567-1584` (full save) and `base.py:1367-1381` (partial save) run every indexed field's `on_save` **eagerly, with `pipeline=None`, against live Redis, before `internal_pipeline` is built**. `ValidityField(Field)` is not an `IndexedFieldMixin`, so its D5 pre-check is only reached later at `base.py:1625-1634` / `:1413-1424`. `JournalEntry` (`provenance_journal.py:299-310`) has four `IndexedField`s next to its `ValidityField`, so the shipped reference model hits this. Success Criterion "the `HSET` never reached Redis" is unachievable as written. | D5 (Technical Approach); Success Criteria bullet 6; new test in Task 3 | Either (a) run the `assert_valid_from` conflict check as a pre-scan over `self._meta.fields` **before** the `_eager_indexed_fields` loop — the same treatment #476 gave the unique-conflict window, whose rationale is written at `base.py:1560-1571` — or (b) narrow the guarantee to "no `ValidityField`-owned byte is written" and reword the Success Criterion. Add a test: a model with one `IndexedField` + one `ValidityField`, re-saved with a conflicting declared `validity=`; assert the indexed field's value in the hash. None of the 16 enumerated `TestMembershipGuardInLua` tests uses a second indexed field. |
| BLOCKER | History & Consistency | Verification ANTI row `git diff -U0 origin/main -- tests/test_validity_field.py \| grep -E '^[+-]' \| grep -c 'degrades_with_no_partial_state'` expects 0, but the substring matches **three** functions (`tests/test_validity_field.py:1388, :1595, :1609`) and the plan's own Test Impact section mandates REPLACING `:1388` (`test_unsaved_instance_degrades_with_no_partial_state`). The row fails on a fully correct implementation — a Verification row that contradicts Test Impact in the same plan. | Verification table (ANTI: observation degradation tests unedited) | Anchor the grep on the two frozen function definitions only: `grep -cE '^[+-].*def test_unsaved_(successor\|contradicted_instance)_degrades_with_no_partial_state\('` expecting 0, leaving `:1388` and `:1406` free to change. Re-verify the row goes red if `:1595`/`:1609` are edited (currently it has no red-state proof). |
| CONCERN | Scope & Value | D6 (`SupersedeResult`, `save_and_supersede`, `save_and_invalidate`, three pipeline validations) ships net-new public API with **zero consumers in this PR** — the No-Gos explicitly defer its only consumer to #563/M4, and the issue's own reproduction (plan lines 19-24) is fixed by D1+D2+D4 alone. The plan reasons the opposite way about the closely related question of wiring M1 to it. | D6; No-Gos | Either cut D6 to #563/M4 (which introduces it with its first real caller), or add an explicit No-Gos-style justification for shipping unconsumed public API now. If kept, D6 step 2's pipeline validations must be copied from the corrected location — see the next row. |
| CONCERN | Scope & Value | `SupersessionProtocol.supersede`/`.invalidate` changing from returning `None` to raising is a breaking change to a published PyPI library (v1.8.2), and `CHANGELOG.md` exists at the repo root but appears nowhere in the plan's Documentation checklist, task list, or Verification table. | Documentation section; Task 5 (`document-feature`) | Add a `CHANGELOG.md` "Breaking" entry to the Task 5 checklist naming `SupersessionProtocol.supersede`/`.invalidate`, the new `ValidityMemberAbsentError`, and the one-line adopter fix (catch `ValidityMemberAbsentError` instead of testing `result is None`). Add a Verification row so the `/do-docs` gate covers it. |
| NIT | History & Consistency | D6 step 2 cites `provenance_journal.py:1197-1231` for "the same three checks and same reasoning"; the three pipeline validations are actually at `:1017-1057` (type check `:1018-1022`, `transaction is not True` `:1023-1028`, `watching and not explicit_transaction` `:1046-1057`). `:1197-1231` is `_REQUIRED_ENTRY_FIELDS` / `_require_journal_shape`, an unrelated block. Also `observation.py:484` (cited in D4 and Failure Path Test Strategy) is the closing paren; the `except (TypeError, ValueError)` is at `:485`. | D6 step 2; D4; Failure Path Test Strategy | Correct both citations before the builder uses them as anchors. |
| NIT | Structural check | Freshness Check declares baseline `60aa730`, but the branch HEAD is `44abc17` (`fix(#576): scope fuse() for unindexed plain-Field filters too`), touching `bm25_field.py`, `query.py`, `context_assembler.py`, `test_hybrid_retrieval.py` — none in this plan's blast radius. Every cited line number was re-verified exact at `44abc17`. | Freshness Check | Restate the baseline as `44abc17` (or note the delta) so Task 4's "baseline measured at `60aa730` in the same environment" instruction names a commit that is actually checked out. |

### Revision applied (critique pass, 2026-09-02)

| Finding | Resolution |
|---|---|
| BLOCKER 1 — D5's "no byte is written" false on indexed models | Took option (a). D5 half 1 rewritten around the two write phases (`base.py:1559-1584` eager, `base.py:1591+` pipelined) and the fact that `ValidityField(Field)` is not an `IndexedFieldMixin`. Check moves to a new `Field.pre_save_validate` hook dispatched ahead of the eager loop in **both** save branches. Success Criterion reworded to assert the sibling `IndexedField`'s value and index entry. New test 17 with a new `IndexedValidFact` model, including a red-state instruction (run it against an `on_save`-based implementation and confirm it fails). Option (b) retained in the plan as an explicit, documented fallback if review rejects the `base.py` edit; new Risk 6 covers the widened blast radius. |
| BLOCKER 2 — ANTI row contradicts Test Impact | Row re-anchored on the two frozen `def` lines only, so replacing `:1388` no longer trips it. Proven three ways (clean / frozen-test-edited / `:1388`-replaced) in the new Red-state proof section. |
| CONCERN — D6 ships unconsumed public API | **Kept**, per the maintainer decision, which names the combined entry point as part of the settled fix rather than a follow-on. Added an explicit justification: it is additive rather than a migration (which is why wiring M1 is reasoned about differently), and it is the only place a caller can get a typed error in pipeline shape. Tested in this PR via tests 16 and 18. |
| CONCERN — no CHANGELOG entry | New `### CHANGELOG` subsection in Documentation with the `### Breaking` block contents, including the one-line adopter fix (`result is None` -> `except ValidityMemberAbsentError`). Added to Task 5, to Success Criteria, and as two Verification rows, both proven red at `44abc17`. |
| NIT — `provenance_journal.py:1197-1231` | Corrected to `:1015-1057` with per-check line numbers, verified against the file. |
| NIT — `observation.py:484` | Corrected to `:485` in all three places; the block reference is now `:480-486`. |
| NIT — stale baseline `60aa730` | Re-baselined to `44abc17` throughout, including Task 4's baseline instruction and the anti-criteria proof commit. Intervening commit recorded in Freshness Check as out of blast radius. |
| Environment (team lead) | Prerequisites row updated: `mcp` is now installed, the extras set is satisfied, and the ~95 previously-deselected tests are collected — with a warning not to compare a pre-install baseline against a post-change count. |

### Critique round 2 (2026-09-03, FULL depth, baseline `a6c81ce`)

<!-- Second war room, dispatched because the 2026-09-02 pass left no verdict in the SDLC
ledger. The six round-1 findings above were verified as actually landed in the current plan
text and are not re-litigated; scrutiny focused on the post-#594 re-baseline, the corrected
line anchors, and the newly-introduced `pre_save_validate` design. -->

**Verdict: NEEDS REVISION** — 2 blockers, 2 concerns, 1 nit. All round-1 resolutions confirmed
present. Neither blocker invalidates D1–D7; both are plan-text corrections.

| Severity | Finding | Addressed By | Implementation Note |
|----------|---------|--------------|---------------------|
| BLOCKER | **B1 — the `pre_save_validate` dispatch site named in Task 1 is unreachable on the external-pipeline save path.** Task 1 pins the dispatch "immediately before `:1593`" (full save) and "immediately before `:1388`" (partial save). Both eager loops live inside the `else:` arm of the external-pipeline branch — `base.py:1503` (full) returns at `:1578` and `base.py:1325` (partial) returns at `:1382`, both *before* the eager loops. A save that receives a caller pipeline therefore never invokes `pre_save_validate` at all. This contradicts D5's stated guarantee ("On the external-pipeline path … the pre-scan raises before the caller's `execute()`"), makes D6's own `save_and_supersede` inert for valid-from conflict detection (step 4 calls `new_instance.save(pipeline=pipe)`, i.e. the external-pipeline branch), and makes Task 3 test 17's "pipeline-path arm" fail as specified. | Task 1 (`build-lua`); D5 half 1; Success Criteria bullet 6; Task 3 test 17 | Dispatch **once** in `Model.save()` after the `pre_save` gate (`base.py:1282-1292`, i.e. after `if not pipeline_or_success: return pipeline or False`) and before the `if update_fields:` / full-save split: `for field_name, field in self._meta.fields.items(): field.pre_save_validate(self, field_name=field_name, field_value=getattr(self, field_name), **kwargs)`. That single site covers all four arms (partial+external `:1325`, partial+internal `:1388`, full+external `:1503`, full+internal `:1593`). Keeping the two-site placement instead would require deleting D5's external-pipeline paragraph, Success Criterion 6's second sentence, and test 17's pipeline arm — but then `save_and_supersede` ships with the guard disabled, the opposite of D6's purpose. |
| BLOCKER | **B2 — D1 breaks `SupersessionProtocol.chain(unsaved)`, which a D7-frozen test asserts.** D1 claims `chain()`/`_walk_one()` are "unchanged in substance"; not true for `chain()`. Today `_member_key(unsaved)` returns `None` because the `EXISTS` fails, so `chain()` returns `[]` (`supersession.py:326-329`). After D1 the anchor resolves to `"ValidFact:None"`, `_walk_links` finds no links, and `chain()` falls through to `chain.append(instance)`, returning `[unsaved]`. `tests/test_validity_field.py:1650` — `assert SupersessionProtocol.chain(unsaved) == []` — sits inside `test_unsaved_contradicted_instance_degrades_with_no_partial_state` (`:1622`), one of the two tests the plan *freezes* and whose editing it defines as proof "D7 was implemented wrong". The same assertion also sits at `:1407` in a test the plan does replace, and `chain()`'s docstring promises `[]` for an unsaved instance. | D1 (`_member_key` resolves only); Test Impact ("must keep passing unchanged"); Success Criteria (`:1608`/`:1622` pass unedited) | Preserve the unsaved contract inside `chain()` rather than inheriting it from `_member_key`. In `SupersessionProtocol.chain` (`supersession.py:326-330`), after `anchor = _member_key(instance)`, gate on membership using `_walk_links`' existing dangling-link rule rather than a new `EXISTS`: `if anchor is None or POPOTO_REDIS_DB.zscore(valid_from_key, anchor) is None: return []` (requires hoisting the `valid_from_key` lookup above the anchor gate). Keeps `:1650` and `:1407` passing unedited and the docstring true, at one `ZSCORE` on a read-only path — the Success Criterion "`_member_key` issues zero Redis commands" still holds, since the command lives in `chain()`. Add an explicit `chain(unsaved) == []` test to `TestMembershipGuardInLua`. |
| CONCERN | **C1 — records that already carry a hash/index valid-from divergence become permanently unsaveable, with no remediation.** The plan guarantees the 30-day divergence "cannot be written" but is silent on divergence already stored — precisely the reporter's population. After the change, loading such a record and saving it sends the corrected hash value as a declared `validity=`, `pre_save_validate` finds a disagreeing `ZSCORE`, and the save raises; every subsequent save fails, including partial saves of unrelated fields if the dispatch iterates `self._meta.fields` (as Task 1 specifies) rather than `update_fields`. "No data migration" is true of key *shapes*, false of the operational consequence. | D5 half 1; Update System; CHANGELOG section | (a) In the partial-save arm, scope the dispatch to `update_fields` so a partial save of an unrelated column cannot trip a pre-existing divergence. (b) Ship a reconciliation recipe in the CHANGELOG next to the adopter fix, using the D5 half-2 read seam: `ValidityField.get_valid_from(Model, "validity", member_key=obj.db_key.redis_key)` returns the effective (index) start; the operator either re-declares that value on the instance or reconciles the index with an explicit `ZADD` (no `NX`) before re-saving. Add a "record with pre-existing divergence" test so the failure mode is a pinned contract, not a discovery. |
| CONCERN | **C2 — `ProvenanceJournal._write`'s failure mode changes, though No-Gos asserts M1 is unchanged.** M1 calls `execute_supersede(..., old_member=target_key, pipeline=pipe)` (`provenance_journal.py:1126-1137`) with an explicit `old_member`, which the new D2 rule classifies as an *assertion*. If the target is hard-deleted between M1's pre-flight (`:906-964`) and `EXEC`, the script now returns `POPOTO_VALIDITY_MEMBER_ABSENT incumbent …` instead of the previous silent no-op. Redis does not roll back the rest of the MULTI, so the journal entry commits and `pipe.execute()` raises a raw `ResponseError` (D4: the pipeline branch does not remap) out of `_write`, which has no handler — the caller gets neither an `AnnotationResult` nor a typed error, on an append-only model. Test Impact lists no `test_provenance_journal.py` coverage, so this ships unexercised. | No-Gos; Test Impact; Task 3 | Add to Task 3 a test that hard-deletes the target between `_pre_flight` and `pipe.execute()` (monkeypatch, or direct `POPOTO_REDIS_DB.delete(target_key)` after the pre-flight returns) asserting the exception type *and* that the entry hash is present — deciding on the record whether "committed entry plus raised error" is the intended contract. If not, wrap `results = pipe.execute()` at `provenance_journal.py:1157` in `try/except redis.exceptions.ResponseError` and re-raise through `_LUA_ERROR_MAP` so M1 surfaces `ValidityMemberAbsentError`. |
| NIT | **N1 — freshness baseline names `c7fc167`, but `origin/main` is now `a6c81ce`.** `origin/main` has advanced two commits past `c7fc167` (`4612883`, `a6c81ce`), both plan-document edits. `git diff --stat c7fc167 a6c81ce -- src/ tests/` is empty, so every cited line number is still exact — but Task 4 instructs the validator to measure a baseline at a commit that is no longer HEAD. | Post-#594 re-verification; Task 4 | Restate the baseline as `a6c81ce` (or "`c7fc167` or later; `src/` and `tests/` are byte-identical through `a6c81ce`") so the validator's baseline command names a checked-out commit. |

**Structural checks (round 2)**: required sections PASS (all 23 present and non-empty); task
numbering PASS (1–6, no gaps); dependencies PASS (linear chain, all IDs resolve, no cycles,
every task carries a validation target); file paths PASS (15 of 16 exist — only
`scripts/check_supersede_lua_phases.py` is absent, and it is Task 1's own deliverable); line
references PASS (spot-verified at `a6c81ce`, including `test_provenance_journal.py:326-336`'s
`>= 16` bound and `args[13]`/`args[9]`/`args[15]` indices surviving the new ARGV[8]);
prerequisites PASS (3 of 4 — Redis DB 15 PONG, editable install resolves to this checkout,
`numpy`/`sentence_transformers`/`mcp` all import; baseline suite not run by the critique);
cross-references PASS.

**Process note**: this war room ran at FULL depth as three in-process lenses (Risk &
Robustness, Scope & Value, History & Consistency) rather than three spawned critic subagents —
no Agent tool was exposed in the executing context, so no roster result-files or membership
gate were produced. Every blocker and concern above is grounded in source read directly at
`a6c81ce`.

### Revision applied (round 2, 2026-09-03)

All five findings addressed. Every anchor the critique named was re-verified independently
against `origin/main` at `d8914fc` before being written in; two of the critique's own line
numbers were off by a little and are corrected below.

| Finding | Resolution |
|---|---|
| **B1** — dispatch site unreachable on the external-pipeline path | **Accepted, took the proposed fix.** Verified the four arms myself at `d8914fc`: `:1325` returns at `:1382`, `:1503` returns at `:1578`, and the eager loops (`:1388`, `:1593`) are inside the `else:` arms — so the round-1 two-site placement did skip both external-pipeline arms. D5 half 1 now carries a four-arm table and a **single** dispatch site, placed after the `pre_save` gate (`:1289-1292`) and before `new_db_key` (`:1294`) — above the `if update_fields is not None:` split at `:1296`, with the code snippet written out. Added a paragraph on why *after* the `pre_save` gate rather than at the top of `save()` (the firewall, write filter, and `pre_save` all decline by returning; declining must precede validating). `Field.pre_save_validate`'s docstring, Risk 6, Task 1, and Success Criterion 6's external-pipeline sentence all rewritten to match. Task 1 gains a per-task verification (`grep` must return exactly one hit, above the split), and two ANTI rows enforce it. |
| **B2** — D1 breaks `chain(unsaved)` | **Accepted, took the proposed fix.** Verified: `chain()`'s anchor gate is `supersession.py:323-324`, `get_interval_keys` is at `:330` (below it), and `assert SupersessionProtocol.chain(unsaved) == []` is at `tests/test_validity_field.py:1650` inside the frozen `test_unsaved_contradicted_instance_degrades_with_no_partial_state` (`:1622`), with a second copy at `:1407`. D1 rewritten: `_walk_one` is genuinely unchanged (nil `HGET` on `"Model:None"`, so `superseded_by(old) is None` at `:1604` also holds), but `chain()` is **not**, and gets the `ZSCORE`-based membership gate with the `get_interval_keys` lookup hoisted above the anchor gate — `ZSCORE` rather than `EXISTS` so an anchor and a dangling link are judged by one rule. The Success Criterion "`_member_key` issues zero Redis commands" is preserved (the command lives in `chain()`). Task 2 gains the bullet, and new test 19 pins `chain(unsaved) == []` directly with a red-state instruction. |
| **C1** — pre-existing divergence makes records unsaveable | **Accepted, took both proposed parts.** (a) The single dispatch iterates `update_fields` when one is supplied, so a partial save of an unrelated column cannot trip a pre-existing divergence. (b) D5 gains a "Pre-existing divergence, and how an operator gets out of it" block with two two-line remediations (adopt `get_valid_from()`, or plain `ZADD` with no `NX`), reproduced in the CHANGELOG. Update System's "No data migration" is restated precisely — shapes unchanged, but a diverged record refuses a full re-save until reconciled. New test 20 pins all three arms (partial save succeeds, full save raises, remediated full save succeeds); new Verification row greps the CHANGELOG for the recipe; the feature docs gain a "Reconciling a record that already diverges" subsection. |
| **C2** — M1's failure mode changes and ships unexercised | **Accepted; decided on the record rather than deferred.** Verified M1's explicit `old_member=target_key` at `provenance_journal.py:1126-1137` and the bare `results = pipe.execute()` at **`:1155`** (the critique cited `:1157`, which is inside the comment above it). New **D8** section: committed-entry-plus-typed-error *is* the intended contract — the annotation is real provenance and an append-only journal must keep it — but the raw `ResponseError` is not, so `_write` gains exactly one `try/except ResponseError: raise _map_lua_error(e) from e`. Task 1 extracts `_map_lua_error` from D4's dispatch table so both call sites share one table; Task 2 does the wrap; new test 21 (in `test_provenance_journal.py`) asserts the exception type **and** the entry's presence. No-Gos amended to name the two narrow `provenance_journal.py` edits explicitly, and `test_provenance_journal.py:1453` is flagged for a read-and-decide rather than a blind edit. |
| **N1** — stale baseline `c7fc167` | **Accepted.** Re-verified: `git diff --stat c7fc167 d8914fc -- src/ tests/` is empty, and all eight intervening commits are plan-document edits for #588/#595/#596. Baseline restated as `d8914fc` in the Freshness Check header, the Post-#594 re-verification header, the Prerequisites baseline-suite row, the Task 4 baseline instruction, and the Task 4 test-count note — with the "`c7fc167` or later" equivalence stated so an already-cut branch stays valid. The `44abc17` red-state proof table is left as-is: it is a historical record of when those rows were proven, not a baseline instruction. |

### Critique round 3 (2026-09-03, FULL depth, baseline `13e3ee5`)

<!-- Third war room, dispatched over the round-2-revised plan. All five round-2 resolutions
(B1/B2/C1/C2/N1) were independently re-verified against source at `13e3ee5` and confirmed
landed and correct; scrutiny focused on the newly-added verification rows, the D8 wrapper,
and the single-dispatch-site mechanics. Lenses ran in-process (no Agent tool exposed in the
executing context), same as round 2. -->

**Verdict: NEEDS REVISION** — 1 blocker, 0 concerns, 2 nits. The blocker is a one-line
verification-row correction; no design decision (D1–D8) is questioned.

| Severity | Finding | Addressed By | Implementation Note |
|----------|---------|--------------|---------------------|
| BLOCKER | **B1v — the new automated ANTI row for the dispatch site fails on a fully correct implementation.** The row "ANTI: the dispatch is not inside either eager-loop arm (B1)" computes `i[0] < j[0]` where `j` collects every line matching `'if update_fields is not None:'` — but that string occurs **twice** in `models/base.py`: at `:1031` (inside `pre_save`'s `update_fields` name validation) and at `:1296` (the save-path partial/full split the row means). `j[0]` is therefore `1031`, and a dispatch correctly placed at `~:1294` — exactly where Task 1 and D5 mandate — makes the row print `False`. Reproduced against `13e3ee5`: `j = [1031, 1296]`, `1294 < 1031` → `False`. This is the same defect class as round-1 BLOCKER 2 (a Verification row that contradicts the plan's own mandate) recurring in the row added to fix round-2 B1. | Verification table (second B1 ANTI row); Task 1 per-task verification | Anchor on the unique line instead of the ambiguous one: `new_db_key = DB_key(self.db_key)` occurs exactly once in `base.py` (`:1294`, verified at `13e3ee5`). Corrected row: `python -c "s=open('src/popoto/models/base.py').read().split(chr(10));i=[n for n,l in enumerate(s,1) if 'pre_save_validate' in l];k=[n for n,l in enumerate(s,1) if 'new_db_key = DB_key(self.db_key)' in l];print(len(i)==1 and i[0]<k[0])"` expecting `True` — this also folds in the "exactly one site" check, making the separate manual-judgment grep row redundant (keep it or drop it, but the automated row is the gate). While editing, also strip the literal `> 1292 and < 1296` line numbers from Task 1's per-task verification — after the ~10-line dispatch block is inserted they are self-invalidating; anchor that check on the same `new_db_key` text, not on numbers. Prove the corrected row both ways before recording: `True` with a single pre-split dispatch, `False` with a dispatch pasted inside either eager loop. |
| NIT | **N2 — `_map_lua_error`'s no-match contract is stated two incompatible ways.** Task 1 says the helper "re-raises the original unchanged when no token matches", but both call sites are spelled `raise _map_lua_error(e) from e`, which requires the helper to *return* an exception — if it raises internally on no-match, the call-site expression never completes (harmless but confusing); if it returns `None` on no-match, the call site is a `TypeError`. | Task 1 (`_map_lua_error` bullet); D8 snippet | Pin one contract: `_map_lua_error(e)` **returns** the mapped exception instance, or returns `e` itself when no token matches; call sites always `raise _map_lua_error(e) from e`. One sentence in Task 1 and the D8 comment. |
| NIT | **N3 — baseline drift, again, and again benign.** The Freshness header names `d8914fc`; `origin/main` is now `13e3ee5` (five further commits, all plan-document edits for #588/#595/#596). Verified `git diff --stat d8914fc 13e3ee5 -- src/ tests/` is empty, so every cited line number is still exact. Task 4's "d8914fc or later — record the SHA" wording already tolerates this; only the header sentence lags. | Freshness Check header | Either restate as `13e3ee5` or reword the header to the same "`d8914fc` or later; `src/`+`tests/` byte-identical through `13e3ee5`" form Task 4 uses, so the header stops needing an edit every time a sibling pipeline commits a plan doc. |

**Round-2 resolutions verified against source at `13e3ee5` (all confirmed):** B1 — the four-arm
table's anchors are exact (`pre_save` gate `:1289-1292`, `new_db_key` `:1294`, split `:1296`;
partial-external returns `:1382`, full-external returns `:1578`; eager loops `:1388-1404` /
`:1593-1608` inside the `else:` arms). B2 — `chain()`'s anchor gate at `supersession.py:323-325`
with `get_interval_keys` below it at `:330`, exactly as the fix describes; the hoist is coherent
(`resolved` is bound at `:320`, above the moved lookup). C1 — dispatch snippet iterates
`update_fields` when supplied; remediation recipe and test 20 present. C2/D8 — the bare
`results = pipe.execute()` is at `provenance_journal.py:1155` as cited; independently confirmed
that both `test_provenance_journal.py:1413` and `:1453` drive `execute_supersede` directly on a
caller pipeline (not through `_write`), so D8's wrapper contradicts neither test as written.
N1 — drift tolerance wording present in Task 4.

**Structural checks (round 3)**: required sections PASS; task numbering PASS (1–6, linear,
no cycles, every task carries a validation target); file paths PASS (only
`scripts/check_supersede_lua_phases.py` absent — Task 1's own deliverable); prerequisites PASS
(Redis DB 15 PONG; editable install resolves to the main checkout; `numpy`/
`sentence_transformers`/`mcp` import; baseline suite not run by the critique); cross-references
PASS (every Success Criterion maps to a task; No-Gos and Rabbit Holes appear nowhere as planned
work).

**Recording note**: `sdlc-tool` is on PATH but this critique was dispatched without a `run_id`,
which `verdict record` hard-requires (`RUN_ID_REQUIRED`); per the #588 session's instruction the
refusal is reported rather than worked around, and the verdict is recorded here and in the
critique report instead.

### Revision applied (round 3)

_Not yet applied — the table above is the work order for the next `/do-plan` pass._

---

## Open Questions

None. Both of the issue's open questions were answered by the maintainer decision of
2026-09-02 and are recorded as settled input in the Solution section:

- **Q1** — the `EXISTS` gate is neither the intended hard boundary nor correct where it sits.
  It moves into `SUPERSEDE_LUA` (D2), and the member-absent case raises rather than no-oping
  (D1, D4).
- **Q2** — the field value at construction remains the single authoritative writer of
  valid-time. A disagreeing assertion is a typed error (D3, D5), not a silent loss to
  `ZADD NX`.

Two implementation-level judgments were made *within* those answers rather than escalated, and
are flagged here so a reviewer can see them rather than discover them:

1. `assert_valid_from` defaults to `False`, and `SupersessionProtocol`'s `at=` is treated as a
   close-time assertion about the incumbent, not a start-time assertion about the successor
   (D3). Asserting it for the successor would raise on every ordinary supersede, since the
   successor's stored start is its save clock. This is the reading that makes "one writer"
   implementable; the alternative reading makes the API unusable.
2. The `EXISTS` probe is retained on exactly one caller —
   `ObservationProtocol._apply_supersession` (D7) — because that path is telemetry, must not
   raise, and by construction never has a same-transaction successor. The probe is deleted
   from the shared `_member_key` where every caller inherited it.
