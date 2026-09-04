---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-03
tracking: https://github.com/tomcounsell/popoto/issues/575, https://github.com/tomcounsell/popoto/issues/570
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-04T06:57:30Z
---

# #575 / #570 — Route SortedField(partition_by=...) values through canonical_key_str()

## Problem

`SortedField(partition_by=...)` renders partition values with bare `str(value)` at multiple
sites in `src/popoto/fields/sorted_field_mixin.py` and `src/popoto/models/query.py`. The sites
are consistent with *each other*, but a `datetime` partition value carries exactly the
aware/naive `str()` fragility #537/#538 fixed for `KeyField` identity in PR #548: the same
instant can land in different partitions depending on how it decoded. Silent, no error, visible
only as queries returning fewer rows than expected.

The Freshness Check below revises one premise: they are **not** consistent with every partition
render in the codebase. The orphan-purge path in `Model._purge_orphan_keys` derives its zset key
from the row's **stored** redis key, whose KeyField segment has been canonical since #548 — so
for a datetime partition it already disagrees with the seven `str()` sites. The scope and the fix
are unchanged; the urgency framing is.

Two issues describe this: #575 (the generic finding, with the suggested `canonical_key_str`
fix) and #570 (the datetime-specific duplicate, filed with a measure-first framing). One fix
closes both — `canonical_key_str()` is a no-op (`str(value)`, byte-identical) for every
non-datetime value, so existing stored partition keys are unaffected unless someone already has
datetime partitions with mixed representations, which no report suggests exists.

## Freshness Check

**Re-verified 2026-09-04 against main `7f057f9`** (`fix(#571): apply the SortedField limit
pushdown on the async path (#602)`). The issue's site list was measured at PR #548 HEAD
`154a9d6`; since then #594 (agent-memory audit) and #571/PR #602 (async pushdown) reshaped
`query.py`. Commits touching the cited files since the issue was filed (2026-08-14):
`7f057f9`, `16aa702`, `07b7268`, `a4f7fbf`, `0ab47a1`. None of them changed the rendering.

**Disposition: Minor drift + one premise correction + one new sibling site.**

### Verified site inventory (main `7f057f9`)

Anchored by enclosing symbol, because line numbers drift under concurrent lanes:

| # | File | Line | Enclosing symbol | Current render |
|---|---|---|---|---|
| 1 | `src/popoto/fields/sorted_field_mixin.py` | 475 | `get_partitioned_sortedset_db_key` | `str(getattr(model_instance, partition_field_name))` |
| 2 | `src/popoto/fields/sorted_field_mixin.py` | 546 | `on_save` (old-partition cleanup) | `old_ss_key.append(str(old_val))` |
| 3 | `src/popoto/fields/sorted_field_mixin.py` | 629 | `on_delete` (old-partition cleanup) | `sortedset_db_key.append(str(old_val))` |
| 4 | `src/popoto/fields/sorted_field_mixin.py` | 753 | `filter_query` | `str(query_params[partition_field_name])` |
| 5 | `src/popoto/models/query.py` | 438 | `top_by_decay` | `[str(self._filters[pf]) for pf in field.partition_by]` |
| 6 | `src/popoto/models/query.py` | 1379 | `_resolve_index` | same comprehension |
| 7 | `src/popoto/models/query.py` | 1421 | `_materialize_decay_field` | same comprehension |

Seven SortedField sites, same count as the issue (its prose says "six", its list has seven).

**Sibling sites — corrected characterization (round-1 NIT).** Recipe call sites
(`recipes/context_assembler.py:627`, `recipes/default_memory.py:194`) and
`cyclic_decay_field.py:402,413` route through `get_partitioned_sortedset_db_key` and are true
passive inheritors. `cyclic_decay_field.py:423,435` (`get_cycles_hash_key_from_parts`,
`get_pressure_hash_key_from_parts`) are **not**: they pass *raw* partition values into
`get_sortedset_db_key`, which `DB_key.__str__` already canonicalizes as of #548. They are
therefore a **second live divergence today** from the seven `str()` sites, not sites that merely
inherit the fix. No edit is needed at those four lines — post-fix all of them converge on the
canonical form — but the plan's earlier description of them was wrong and is corrected here.

Two further sites in `src/popoto/fields/` render a partition value outside `DB_key` (round-1
CONCERN): `confidence_field.py:315,350,374` and `event_stream.py:119`. Both are in scope; see
below. `prediction_ledger._error_key` takes a caller-supplied label, not a model field, and is
genuinely out of scope.

**Full in-scope inventory: 11 sites** — 7 SortedField + 3 ConfidenceField + 1 EventStream.

### The plan's own inventory grep was defective — replaced

`git grep -n 'str(' … | grep -i partition` matches only lines containing the word
"partition", so it **misses sites 2 and 3** (`str(old_val)`). Following it would have produced
exactly the partial conversion this plan calls "worse than none". Use instead:

```bash
# widened to all of src/popoto/fields/ + query.py so a sibling site cannot hide (round-1 CONCERN)
grep -rn "append(str(\|\[str(self\._filters\|str(query_params\[partition\|str(getattr(model_instance, partition\|key += f\":{val}\"\|f\"{base_key}:{partition_value}\"" \
  src/popoto/fields/ src/popoto/models/query.py
```

Pre-edit this returns exactly **11** rows: the seven above, plus `confidence_field.py:315,350,374`
and `event_stream.py:119`. Confirm that count (modulo line drift) before editing.

### Premise correction: the sites are NOT all consistent today — and *why* (round-1 CONCERN)

The mechanism this plan gave in round 1 was **wrong** and is corrected here.
`Model._purge_orphan_keys` does *not* "append a raw datetime". It builds
`values: dict[str, str]` by parsing the row's already-stored redis key with
`DB_key.from_redis_key(key)` — the entries are already-unescaped **strings**, and
`canonical_key_str`/`DB_key.__str__` both no-op on a `str`.

The divergence is nonetheless real, with a different cause: **as of #548 the KeyField segment in
the stored redis key is written in canonical form**, so the string the purge path parses back out
is the *canonical* rendering, while the seven SortedField sites build their zset key from the
live attribute through `str(value)` — the legacy rendering. For a datetime partition the two
disagree, and the purge silently removes nothing. The issue's "all sites internally consistent,
no bug today" framing is stale: the defect is live under the datetime-partition precondition, not
merely latent.

The purge path is the **alignment target** and must NOT be changed. Anchor it by symbol, not line
(the round-1 citation `base.py:3667` was off by one — 3667 is the `for name in partition:` header,
the append is the next line): `Model._purge_orphan_keys`, the `for field_name in
meta.sorted_field_names:` loop, statement `zset_key.append(values[name])`.

**The round-1 Risks item derived from the wrong mechanism is retracted.** "It is correct *because*
it appends raw; a cleanup that wraps it in `str()` would reintroduce the divergence" is false —
`str()` on a `str` is a no-op. If a clarifying comment is added at that site it must point at
`DB_key.from_redis_key` + #548 KeyField canonicalization, **not** at `DB_key.__str__`.

Consequence for the fix shape: at the seven SortedField sites, `canonical_key_str(v)` and simply
dropping the `str()` wrapper produce identical bytes. Prefer the explicit `canonical_key_str(v)`
so the intent survives future refactors that stop routing through `DB_key`.

### Sibling site: ConfidenceField (scope decision — in scope, with a stated limit)

`ConfidenceField` mirrors SortedField's `partition_by` API but builds its companion-hash key by
string concatenation off an already-rendered `redis_key`, bypassing `DB_key` entirely:
`src/popoto/fields/confidence_field.py:315` (`get_data_hash_key`), `:350`
(`get_data_hash_key_from_values`), `:374` (`get_old_data_hash_key`) — each `key += f":{val}"`.
Same bug class, same no-op property, three one-line changes.

**Decision: in scope.** Two limits must be stated so BUILD does not over-reach (round-1 CONCERN):

1. **Escaping parity with `DB_key` is explicitly NOT attempted.** These keys are raw
   concatenation with no `DB_key.clean()`. The canonical datetime form contains colons, so the
   partition boundary remains ambiguous after the change — exactly as ambiguous as it is today,
   since `str(datetime)` also contains colons. The change aligns ConfidenceField keys with the
   *same* canonical rendering the SortedField sites will use; it does **not** make them
   byte-comparable to any `DB_key`-built key. Escaping is a separate, larger change and is a
   No-Go here.
2. **The `None` asymmetry must be preserved verbatim.** `get_data_hash_key` (:313-315) and
   `get_old_data_hash_key` (:372-374) *skip* a `None` partition value; `get_data_hash_key_from_values`
   (:344-350) *raises* `QueryException`. Call `canonical_key_str(val)` **inside** the existing
   `if val is not None:` / post-raise guards so neither behavior moves. Do not hoist the call
   above a guard — `canonical_key_str(None)` returns the string `"None"` and would turn a skip
   into an appended `:None` segment.

Escape hatch: if converting these requires touching anything beyond the three interpolations,
drop them, file a follow-up issue, and say so in the PR body.

### Sibling site: EventStreamMixin (scope decision — in scope)

`src/popoto/fields/event_stream.py:119` builds the partitioned stream key as
`base_key = f"{base_key}:{partition_value}"` from a raw model attribute, bypassing `DB_key` — the
identical bug class ConfidenceField was scoped in for. Round 1 flagged that leaving it out while
the Success-Criteria grep covered only `sorted_field_mixin.py` + `query.py` would make the grep
return zero *while the site remained*: the "partial conversion is worse than none" outcome this
plan names as its own top risk.

**Decision: in scope.** Convert to `f"{base_key}:{canonical_key_str(partition_value)}"` **inside**
the existing `if partition_value is not None:` guard (same None-preservation rule as
ConfidenceField). The widened grep above covers `src/popoto/fields/` so this site can no longer
hide from the criteria.

### Helper unchanged

`canonical_key_str` (`src/popoto/models/canonical_key.py:46`) still dispatches on
`datetime.datetime` only; every other type — including `date` and `time` — returns `str(value)`
byte-for-byte. The no-op property this plan depends on holds on current main.

## Prior Art

- PR #548 (#537/#538) — `canonical_key_str()` and the KeyField conversion this mirrors; its
  "Task-2 audit / Deliberately deferred" sections are the provenance of both issues.
- #570's measure-first argument is honored by the no-op property: for all values that exist in
  the wild today (non-datetime partitions), bytes are identical, so no migration story is
  needed. If the build discovers any site where the no-op property does NOT hold (e.g. a type
  whose canonical form differs from `str()` beyond datetimes — check `canonical_key_str`'s full
  dispatch), STOP and report before proceeding — that would make this a key-migration change,
  which is out of appetite.

## Solution

Route every partition-value rendering site through `canonical_key_str(value)` — 11 sites across
three files — so datetime partitions address one partition regardless of how the value decoded,
and so all sites agree with the #548-canonical stored key that the orphan purge parses back out.

1. Convert the seven SortedField sites (Freshness Check table) to `canonical_key_str(value)`;
   import from `..models.canonical_key` in `sorted_field_mixin.py` and `.canonical_key` in
   `query.py`. Do **not** touch `Model._purge_orphan_keys` — it is already canonical.
2. Convert the three `ConfidenceField` interpolations (`confidence_field.py:315,350,374`) inside
   their existing `None` guards, subject to the escape hatch and the two stated limits.
3. Convert `event_stream.py:119` inside its existing `if partition_value is not None:` guard.
4. Regression tests (see `## Test Impact`).
5. Docs: `docs/query.md` SortedField/partition note + CHANGELOG entry.
6. PR body must contain **both** `Closes #575` and `Closes #570`.

**Call shape, at all 11 sites: `canonical_key_str(val)` — no `force` kwarg, ever.** See Risks.

### Build environment (inherited by `/do-build`)

- Build in a **dedicated git worktree** with its own venv installed as
  `.[dev,embeddings,benchmark,mcp]`. Before trusting any test number, verify the editable install
  resolves to *that* checkout (`python -c "import popoto, sys; print(popoto.__file__)"`) — see
  CLAUDE.md "Verifying in a worktree".
- All test runs use `POPOTO_TEST_DB=9`. **Never DB 0** — it is a LIVE agent store on this machine.
  Any subprocess test must pin an explicit non-zero-db `REDIS_URL`
  (e.g. `REDIS_URL=redis://localhost:6379/9`) set *before* `import popoto`.
- Note the environment (redis-py major version) alongside any mypy/test count.

## Step by Step Tasks

1. **`sorted_field_mixin.py` — sites 1-4.** Add `from ..models.canonical_key import
   canonical_key_str` and replace `str(...)` with `canonical_key_str(...)` at
   `get_partitioned_sortedset_db_key` (:475), `on_save` old-partition cleanup (:546), `on_delete`
   old-partition cleanup (:629), and `filter_query` (:753). No `force` kwarg.
   *Verify:* `ruff check src/` clean; `grep -n "str(getattr(model_instance, partition\|append(str(\|str(query_params\[partition" src/popoto/fields/sorted_field_mixin.py` returns **0** rows.
2. **`query.py` — sites 5-7.** Add `from .canonical_key import canonical_key_str` and replace the
   three `[str(self._filters[pf]) for pf in field.partition_by]` comprehensions (`top_by_decay`
   :438, `_resolve_index` :1379, `_materialize_decay_field` :1421) with
   `[canonical_key_str(self._filters[pf]) for pf in field.partition_by]`.
   *Verify:* `grep -n "\[str(self\._filters" src/popoto/models/query.py` returns **0** rows;
   `ruff check src/` clean.
3. **`confidence_field.py` — 3 sites, guards preserved.** Replace `key += f":{val}"` with
   `key += f":{canonical_key_str(val)}"` at :315, :350, :374, each **inside** its existing
   `if val is not None:` guard (:350 sits after the `QueryException` raise — leave the raise
   untouched). Import `from ..models.canonical_key import canonical_key_str`.
   *Verify:* `pytest tests/test_confidence_field.py tests/test_partitioned_confidence.py -q`
   (with `POPOTO_TEST_DB=9`) green; diff shows no change to any `if val is not None` /
   `raise QueryException` line.
4. **`event_stream.py` — 1 site.** Replace `base_key = f"{base_key}:{partition_value}"` with
   `base_key = f"{base_key}:{canonical_key_str(partition_value)}"`, inside the existing
   `if partition_value is not None:` guard. Import as above.
   *Verify:* `pytest tests/test_event_stream_mixin.py -q` green.
5. **Inventory gate.** Re-run the widened grep from the Freshness Check over
   `src/popoto/fields/ src/popoto/models/query.py`.
   *Verify:* returns **0** rows. (It returned 11 before task 1.)
6. **Regression tests** — new file `tests/test_partition_canonical_rendering.py`, contents per
   `## Test Impact`.
   *Verify:* `POPOTO_TEST_DB=9 pytest tests/test_partition_canonical_rendering.py -q` green.
7. **Full-suite + typing gate.**
   *Verify:* `POPOTO_TEST_DB=9 pytest -q -m "not slow"` green; `ruff check src/` exit 0;
   `black --check src/ tests/`; `mypy src/` delta 0 measured base-vs-branch **in the same env**
   (CLAUDE.md redis-py caveat — state the redis-py version with the number).
8. **Docs + CHANGELOG.** `docs/query.md` SortedField/partition section gains a note that datetime
   partition values are canonicalized to UTC (aware/naive/offset variants collapse to one
   partition); CHANGELOG entry naming the key-bytes change for datetime partitions only.
   *Verify:* `mkdocs build --strict` succeeds (or `scripts/ci-local.sh docs`).
9. **PR body.** Contains `Closes #575` and `Closes #570`, plus the escape-hatch disclosure if any
   in-scope site was dropped.
   *Verify:* `gh pr view --json body -q .body | grep -c "Closes #5"` returns 2.

## Test Impact

**New file: `tests/test_partition_canonical_rendering.py`.**

| Test | What it pins | Why |
|---|---|---|
| `test_aware_and_utc_equivalent_share_partition` | A model with `SortedField(partition_by=("ts",))` where `ts` is a **`KeyField(type=datetime)`**; `12:00+07:00` and its UTC equivalent `05:00+00:00` produce the *same* zset key and the same query result set. | The core #570 defect. |
| `test_naive_datetime_partitions_consistently` | A naive datetime partitions to the same key as its UTC-aware twin (canonical doctrine: naive is assumed UTC). | Matches `canonical_key_str` semantics; prevents a "naive is a third partition" regression. |
| `test_write_and_purge_paths_agree` | `get_partitioned_sortedset_db_key(instance, field)` equals the zset key `Model._purge_orphan_keys` derives for that same row. **The partition field MUST be a `KeyField`** — `_purge_orphan_keys` populates `values` only `for field_name in meta.key_field_names` and `continue`s when any partition name is absent, so a plain (non-key) datetime partition never reaches the purge branch and the assertion is vacuous (round-1 CONCERN). | Proves the live divergence is closed, non-vacuously. |
| `test_byte_identity_for_non_datetime_partitions` | For `str` / `int` / `float` / `bool` / `date` / `time` partition values, the rendered partition segment is byte-identical to `str(value)`. | The scope guard. **If any non-datetime type is NOT byte-identical, BUILD must STOP and report** — that is a key migration, out of appetite. |
| `test_partition_change_cleanup_paths` | Old-partition cleanup on `on_save` and `on_delete` targets the canonical old key (exercises sites 2 and 3, the two the original defective grep missed). | Missed-site insurance. |
| `test_query_paths_agree` | `filter_query`, `top_by_decay`, `_resolve_index`, `_materialize_decay_field` all resolve the same partition key the write path used, via a save → filter-by-partition round trip. | Covers sites 4-7. |
| `test_confidence_field_partition_canonical` | `get_data_hash_key`, `get_data_hash_key_from_values`, `get_old_data_hash_key` agree with each other for a datetime partition; `None` still **skips** in the first and third and still **raises** `QueryException` in the second. | Sites 8-10 + the preserved asymmetry. |
| `test_event_stream_partition_canonical` | The partitioned stream key uses the canonical rendering; a `None` partition value still yields the unpartitioned `base_key`. | Site 11 + preserved guard. |

**Environment requirements for these tests:**
- Every datetime test must **explicitly clear `POPOTO_DATETIME_KEY_LEGACY`** (e.g. a
  `monkeypatch.delenv(..., raising=False)` fixture plus reloading/patching `Defaults.DATETIME_KEY_LEGACY`)
  rather than inheriting ambient env — otherwise the suite's verdict depends on the shell.
- Runs under `POPOTO_TEST_DB=9`; any subprocess pins `REDIS_URL=redis://localhost:6379/9`.

**Touched existing tests (expected green, no edits anticipated):** `tests/test_sortedfield.py`,
`tests/test_sorted_field_ordering.py`, `tests/test_decaying_sorted_field.py`,
`tests/test_sorted_range_pushdown.py`, `tests/test_confidence_field.py`,
`tests/test_partitioned_confidence.py`, `tests/test_confidence_modulated_decay.py`,
`tests/test_event_stream_mixin.py`. If any of these needs an assertion changed, that is a
key-bytes change for a **non**-datetime type — stop and report (scope guard).

**No xfail markers** related to this bug exist in `tests/` (searched `pytest.mark.xfail` /
`pytest.xfail(`); nothing to convert.

## No-Gos

- No migration/audit tooling — the no-op property is the scope guard; if it fails, stop.
- No change to KeyField identity or `canonical_key.py` itself.
- No new partition_by types or validation (option 2 in #570 — rejection at definition time —
  is NOT taken; canonicalization supersedes it).
- **No `DB_key.clean()` / escaping parity for ConfidenceField or EventStream keys.** Those keys
  stay raw concatenations; the change aligns their *rendering*, not their escaping.
- No `force=True` anywhere. No change to the `POPOTO_DATETIME_KEY_LEGACY` switch's semantics.

## Risks / Rabbit Holes

- **Missed site** = split partitions. The original inventory grep in this plan already proved
  this risk is real — it silently omitted two of the seven `str(old_val)` sites, and round-1
  critique found it also hid `event_stream.py:119` (see Freshness Check). Use the widened grep
  over `src/popoto/fields/ src/popoto/models/query.py`, gate on it returning 0 (task 5), and
  test every path that renders a partition.
- **`Defaults.DATETIME_KEY_LEGACY` / `POPOTO_DATETIME_KEY_LEGACY` — the #476 mixed-deploy hazard
  (round-1 CONCERN, previously unmitigated).** `canonical_key_str` is gated on that switch
  (`canonical_key.py:92-94`); only the read-only #537/#538 audit passes `force=True`. If BUILD
  writes `canonical_key_str(v, force=True)` at *any* of the 11 sites, the write path diverges
  from `DB_key.__str__` and from the purge path for a fleet mid-rollout with the switch set —
  precisely the #476 forward-incompatibility shape.
  *Mitigation:* (a) call `canonical_key_str(val)` with **no `force` kwarg** at all 11 sites;
  (b) task 5's gate grep is complemented by `grep -rn "canonical_key_str(.*force" src/popoto/fields/ src/popoto/models/query.py`
  returning **0**; (c) the datetime regression tests explicitly clear
  `POPOTO_DATETIME_KEY_LEGACY` rather than inheriting ambient env, so a set switch cannot make
  the suite pass vacuously.
- **~~`base.py:3667` drift~~ — RETRACTED (round-1 CONCERN).** The round-1 risk "it is correct
  *because* it appends raw; a cleanup wrapping it in `str()` would reintroduce the divergence"
  was derived from the wrong mechanism and is false: `values` holds `str` already, so `str()`
  there is a no-op. Nothing to guard. If a clarifying comment is added at that site, it must
  point at `DB_key.from_redis_key` + #548 KeyField canonicalization.
- **Non-datetime byte drift = out of appetite.** `canonical_key_str` is byte-identical to
  `str(value)` for every non-datetime type on current main (re-confirmed by round-1 critique).
  If BUILD finds any type where it is not, **STOP and report** — that converts this into a key
  migration.
- **1.8.0/1.9.x forward-compat**: partition key bytes change only for datetime partitions,
  which the docs never advertised and no report uses. State this in the CHANGELOG anyway
  (lesson of #476).
- **DB 0 hazard**: ad-hoc repro scripts default to DB 0, a LIVE agent store. Use
  `POPOTO_TEST_DB=9` and pin `REDIS_URL=redis://localhost:6379/9` before `import popoto` (#577).

## Success Criteria

- All **11** sites converted (7 SortedField + 3 ConfidenceField + 1 EventStream); the **widened**
  grep over `src/popoto/fields/ src/popoto/models/query.py` returns **0** rows (it returns 11
  pre-change), and `grep -rn "canonical_key_str(.*force" src/popoto/fields/ src/popoto/models/query.py`
  returns 0.
- A datetime-partition test with the partition field declared as a **`KeyField(type=datetime)`**
  proves `get_partitioned_sortedset_db_key` and the key `_purge_orphan_keys` derives for the same
  row are equal. (A non-key partition field makes this criterion vacuous — `_purge_orphan_keys`
  skips it — which is why the KeyField requirement is part of the criterion.)
- Byte-identity test: str/int/float/bool/date/time partition segments unchanged versus
  `str(value)`.
- ConfidenceField `None` asymmetry preserved: skip in `get_data_hash_key` /
  `get_old_data_hash_key`, `QueryException` in `get_data_hash_key_from_values`. EventStream `None`
  partition still yields the unpartitioned key.
- New tests green under `POPOTO_TEST_DB=9`; full non-slow suite green; ruff/black clean; mypy
  delta 0 (same env, base-vs-branch, redis-py version stated per CLAUDE.md's caveat); editable
  install verified to resolve to the build worktree.
- PR body contains both `Closes #575` and `Closes #570`.

## Documentation

- CHANGELOG.md, `docs/query.md` SortedField/partition notes.

## Critique Results

### Round 1 (2026-09-04, main `73c6a48`) — verdict: NEEDS REVISION

Depth: FULL (3 lenses). All findings verified against working-tree source, not inferred.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | scope-value + history-consistency | No `## Step by Step Tasks` section and no `## Test Impact`; `## Solution` is five prose bullets and no task carries a validation command, so `/do-build` has nothing to consume. | `## Step by Step Tasks` (9 numbered tasks, each with a validation command) + `## Test Impact` added — revision 2026-09-04. | Split into numbered tasks — (1) `sorted_field_mixin.py` sites 1-4, (2) `query.py` sites 5-7, (3) `confidence_field.py` 315/350/374, (4) tests, (5) docs/CHANGELOG — each with a validation command; add `## Test Impact`. |
| CONCERN | risk-robustness | The `base.py:3667` mechanism in the Freshness Check is wrong: `_purge_orphan_keys` builds `values: dict[str, str]` from `DB_key.from_redis_key(key)`, i.e. already-unescaped strings, so the "appends raw datetime" cause and the derived "wrapping in `str()` would reintroduce the divergence" risk are both false. | Freshness Check §*Premise correction* rewritten: cause is #548 KeyField canonicalization of the **stored** key, not `DB_key.__str__`. The derived Risks item is explicitly **retracted**. | Fix the prose; attribute the divergence to #548 KeyField canonicalization of the stored key, not to `DB_key.__str__` at that site. |
| CONCERN | risk-robustness | Success Criterion 2 is vacuous unless the partition field is a KeyField — `_purge_orphan_keys` only populates `values` for `meta.key_field_names` and `continue`s otherwise (`base.py:3663-3665`), so a non-key datetime partition never reaches the purge branch. | Success Criteria now **requires** the partition field be `KeyField(type=datetime)`; `## Test Impact` row `test_write_and_purge_paths_agree` states the same. | Regression model must declare the partition field as `KeyField(type=datetime)`; assert `get_partitioned_sortedset_db_key` equals the purge-derived key for the same row. |
| CONCERN | risk-robustness + history-consistency | The `POPOTO_DATETIME_KEY_LEGACY` / `Defaults.DATETIME_KEY_LEGACY` gate on `canonical_key_str` is unmentioned; a `force=True` call at any of the ten sites recreates the #476 mixed-deploy hazard. | New Risks item *`Defaults.DATETIME_KEY_LEGACY`*: no `force` kwarg at any of the 11 sites, a `force`-grep gate, and tests must clear `POPOTO_DATETIME_KEY_LEGACY`. | Call `canonical_key_str(val)` with no `force` kwarg at all ten sites; the datetime regression test must clear `POPOTO_DATETIME_KEY_LEGACY` rather than inherit ambient env. |
| CONCERN | scope-value | Sibling audit stops one site short: `src/popoto/fields/event_stream.py:119` (`base_key = f"{base_key}:{partition_value}"`) is the same bug class, and the Success Criteria grep covers only `sorted_field_mixin.py` + `query.py` so it returns zero while that site remains — the "partial conversion is worse than none" outcome. | `event_stream.py:119` brought **in scope** (task 4, 11 sites total); Success-Criteria grep widened to `src/popoto/fields/ src/popoto/models/query.py`. | Convert it inside the existing `if partition_value is not None` guard, or record an explicit out-of-scope decision plus follow-up issue; widen the criteria grep to `src/popoto/fields/`. |
| CONCERN | risk-robustness | ConfidenceField conversion aligns those keys only with themselves — raw `key += f":{val}"` concatenation with no `DB_key.clean()`, so escaping parity is not achieved; and `get_data_hash_key`/`get_old_data_hash_key` skip `None` while `get_data_hash_key_from_values` raises. | Freshness Check §*ConfidenceField* now states escaping parity is **not** attempted, and requires `canonical_key_str` **inside** the existing `None` guards. | Put `canonical_key_str(val)` *inside* the existing `if val is not None:` guards to preserve the asymmetry; state that `DB_key` escaping parity is explicitly not attempted. |
| NIT | history-consistency | `base.py:3667` is off by one — 3667 is `for name in partition:`, the append is 3668; cited four times while the plan itself warns line numbers drift. | All line citations replaced by symbol anchors (`Model._purge_orphan_keys`, the `sorted_field_names` loop). | Use symbol anchors. |
| NIT | history-consistency | `cyclic_decay_field.py:423,435` are mischaracterized: they pass raw values into `get_sortedset_db_key`, which `DB_key.__str__` already canonicalizes — a second live divergence today, not sites that merely "inherit the fix". | Corrected in Freshness Check §*Sibling sites*: :423/:435 are a **second live divergence** today, not passive inheritors. No edit needed. | Description only; no edit needed at those lines (they converge post-fix). |
| NIT | history-consistency | Frontmatter `tracking:` names only #575 while the title and Solution item 5 claim both #575 and #570. | `tracking:` now lists both #575 and #570. | Add #570 to `tracking:`. |

### Detail

**BLOCKER — No "Step by Step Tasks" section; no task carries a validation command.**
`## Solution` is five prose bullets. Every comparable plan in this repo
(`pytest_plugin_inert_warning.md`, `datetime_keyfield_canonical_identity.md`) carries
`## Step by Step Tasks` with per-task validation plus `## Test Impact`; `/do-build` consumes
those. *Implementation Note:* split into numbered tasks — (1) sorted_field_mixin.py sites 1-4,
(2) query.py sites 5-7, (3) confidence_field.py 315/350/374, (4) tests, (5) docs/CHANGELOG —
each with a validation command (e.g. `pytest tests/test_sorted_field_partition_canonical.py -q`,
and the replacement grep returning exactly seven rows). Add `## Test Impact` naming the new/
touched test files.

**CONCERN — the `base.py:3667` mechanism in the Freshness Check is wrong.**
The purge path does *not* "append the raw value". `_purge_orphan_keys` builds
`values: dict[str, str]` from `DB_key.from_redis_key(key)` (`base.py:3629-3648`), i.e. already-
unescaped *strings* parsed out of the row's stored redis key. `canonical_key_str` no-ops on a
`str`. The divergence the plan found is real, but its cause is "the KeyField segment in the
stored key is canonical as of #548", not "DB_key canonicalizes a raw datetime here". Consequence:
the Risks item "it is correct *because* it appends raw … a cleanup that wraps it in `str()`
would reintroduce the divergence" is false — `str()` on a `str` is a no-op. *Implementation
Note:* fix the prose, and if a comment is added at that site, point at
`DB_key.from_redis_key` + #548 KeyField canonicalization, not at `DB_key.__str__`.

**CONCERN — Success Criterion 2 is vacuous unless the partition field is a KeyField.**
`_purge_orphan_keys` populates `values` only `for field_name in meta.key_field_names` and skips
any sorted field whose partition names are not all present (`if any(name not in values for name
in partition): continue`, `base.py:3663-3665`). A test using a plain (non-key) datetime
`SortedField(partition_by=...)` never reaches the purge branch and passes trivially. *Implementation
Note:* the regression model must declare the partition field as a `KeyField(type=datetime)`;
assert the zset key built by `get_partitioned_sortedset_db_key` equals the one
`_purge_orphan_keys` derives for the same row.

**CONCERN — the `POPOTO_DATETIME_KEY_LEGACY` gate is unmentioned.**
`canonical_key_str` is gated on `Defaults.DATETIME_KEY_LEGACY` (`canonical_key.py:92-94`) and
only the read-only audit passes `force=True`. If the build writes `canonical_key_str(v,
force=True)` at any of the ten sites, the write path diverges from `DB_key.__str__` and from
the purge path for a fleet mid-rollout with the switch set — the #476 mixed-deploy hazard.
*Implementation Note:* call `canonical_key_str(val)` with no `force` kwarg at all ten sites, and
make the datetime regression test explicitly unset/clear `POPOTO_DATETIME_KEY_LEGACY` rather
than inheriting ambient env.

**CONCERN — the sibling audit stops one site short: `EventStreamMixin`.**
`src/popoto/fields/event_stream.py:119` builds the partitioned stream key as
`base_key = f"{base_key}:{partition_value}"` from a raw model attribute, bypassing `DB_key` —
the identical bug class the plan scoped ConfidenceField in for. The Success Criteria grep only
covers `sorted_field_mixin.py` and `query.py`, so it returns zero while this site remains: the
"partial conversion is worse than none" outcome the plan names. *Implementation Note:* either
convert it (`f"{base_key}:{canonical_key_str(partition_value)}"`, inside the existing
`if partition_value is not None` guard) or record an explicit out-of-scope decision plus a
follow-up issue, and widen the criteria grep to `src/popoto/fields/` so the omission is visible.
(`prediction_ledger._error_key` takes a caller-supplied label, not a model field — genuinely
out of scope.)

**CONCERN — the ConfidenceField conversion aligns those keys only with themselves.**
`get_data_hash_key` and friends build `key = base_key.redis_key + ":data"` then `key += f":{val}"`
— raw concatenation with no `DB_key.clean()`. The canonical datetime form contains colons, so the
partition boundary stays ambiguous after the change; this is not a regression (`str(datetime)`
has colons too) but it means these keys still do not agree byte-for-byte with any `DB_key`-built
key. Also `get_data_hash_key` (:313-315) and `get_old_data_hash_key` (:372-374) skip `None`
values while `get_data_hash_key_from_values` (:344-350) raises. *Implementation Note:* put
`canonical_key_str(val)` *inside* the existing `if val is not None:` guards so the None
asymmetry is preserved verbatim; state in the plan that escaping parity with `DB_key` is
explicitly not attempted here.

**NIT — `base.py:3667` is off by one.** Line 3667 is `for name in partition:`; the append is
3668. The plan cites 3667 four times while itself warning that line numbers drift.

**NIT — `cyclic_decay_field.py:423,435` are mischaracterized.** They call
`get_sortedset_db_key(model_class, field_name, *partition_values)` with *raw* values, which
`DB_key.__str__` already canonicalizes — so they are a second live divergence from the seven
`str()` sites today, not merely sites that "inherit the fix". (Post-fix they converge; no edit
needed, only the description is wrong.) Lines 402/413 do go through
`get_partitioned_sortedset_db_key` as described.

**NIT — frontmatter `tracking:` names only #575** while the plan title and Solution item 5 claim
both #575 and #570.

Verified-clean: all seven SortedField line anchors match exactly on `73c6a48`; the replacement
grep returns exactly those seven rows; `confidence_field.py:315/350/374`,
`canonical_key.py:46`, `db_key.py:276-281`, `context_assembler.py:627`,
`default_memory.py:194` all match as cited; no partition render sites exist outside the files
the plan enumerates plus `event_stream.py:119`; the no-op property of `canonical_key_str` for
non-datetime values holds on current main.

### Revision applied — 2026-09-04T06:57:30Z

All 9 round-1 findings (1 BLOCKER, 5 CONCERNs, 3 NITs) addressed; see the *Addressed By* column.
Re-verified on `9c4908d` while revising: the widened grep returns exactly 11 rows
(7 + `confidence_field.py:315,350,374` + `event_stream.py:119`); `_purge_orphan_keys` does build
`values: dict[str, str]` from `DB_key.from_redis_key` and does gate on `meta.key_field_names`;
`canonical_key_str` remains gated on `Defaults.DATETIME_KEY_LEGACY` with `force=True` reserved for
the audit; no bug-related `xfail` markers exist in `tests/`. Scope guard reaffirmed: byte-identity
of `canonical_key_str` for every non-datetime type is a **build-stop condition** if it fails.
Appetite unchanged: **Small** (11 one-line edits + one test file + docs).
