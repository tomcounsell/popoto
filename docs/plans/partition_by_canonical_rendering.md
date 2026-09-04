---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-03
tracking: https://github.com/tomcounsell/popoto/issues/575
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-04T06:44:04Z
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
render in the codebase. `base.py:3667` (orphan purge) appends the raw value, which `DB_key`
canonicalizes as of #548, so that path already disagrees with the seven `str()` sites for a
datetime partition. The scope and the fix are unchanged; the urgency framing is.

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
Recipe call sites (`recipes/context_assembler.py:627`, `recipes/default_memory.py:194`) and
`cyclic_decay_field.py:402,413,423,435` go through these builders and inherit the fix; they
need no edit.

### The plan's own inventory grep was defective — replaced

`git grep -n 'str(' … | grep -i partition` matches only lines containing the word
"partition", so it **misses sites 2 and 3** (`str(old_val)`). Following it would have produced
exactly the partial conversion this plan calls "worse than none". Use instead:

```bash
grep -rn "append(str(\|\[str(self\._filters\|str(query_params\[partition\|str(getattr(model_instance, partition" \
  src/popoto/fields/sorted_field_mixin.py src/popoto/models/query.py
```

and confirm the result is the seven rows above (modulo line drift) before editing.

### Premise correction: the sites are NOT all consistent today

`DB_key.__str__` (`src/popoto/models/db_key.py:276-281`) already renders **every non-DB_key
partial through `canonical_key_str`** as of #548. A partition value appended raw is therefore
already canonical; a value pre-rendered with `str()` arrives as a `str` and canonicalization
no-ops on it. `src/popoto/models/base.py:3667` (orphan-purge, `zset_key.append(values[name])`)
appends the **raw** value — so on current main the purge path and the write/query paths already
disagree for a `datetime` partition value. The issue's "all sites internally consistent, no bug
today" framing is stale: the defect is live (silently purging nothing) under the same
datetime-partition precondition, not merely latent. `base.py:3667` is the alignment target and
must NOT be changed.

Consequence for the fix shape: at these seven sites, `canonical_key_str(v)` and simply dropping
the `str()` wrapper produce identical bytes. Prefer the explicit `canonical_key_str(v)` so the
intent survives future refactors that stop routing through `DB_key`.

### New sibling site: ConfidenceField (scope decision)

`ConfidenceField` mirrors SortedField's `partition_by` API but builds its companion-hash key by
string concatenation off an already-rendered `redis_key`, bypassing `DB_key` entirely:
`src/popoto/fields/confidence_field.py:315` (`get_data_hash_key`), `:350`
(`get_data_hash_key_from_values`), `:374` (`get_old_data_hash_key`) — each `key += f":{val}"`.
Same bug class, same no-op property, three one-line changes.

**Decision: in scope.** Escape hatch: if converting these requires touching anything beyond the
three interpolations, drop them, file a follow-up issue, and say so in the PR body.

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

1. Convert all seven SortedField partition-value rendering sites (Freshness Check table) to
   `canonical_key_str(value)`; import from `..models.canonical_key` in `sorted_field_mixin.py`
   and `.canonical_key` in `query.py`. Do not touch `base.py:3667` — it is already canonical.
2. Convert the three `ConfidenceField` interpolations (`confidence_field.py:315,350,374`) to
   `key += f":{canonical_key_str(val)}"`, subject to the escape hatch in the Freshness Check.
3. Regression tests: a model with `SortedField(partition_by=<datetime field>)` — an aware
   datetime and its UTC equivalent land in the SAME partition; naive datetimes partition
   consistently; write-path and every read/query path agree on the partition key (round-trip
   through save → filter-by-partition). Plus a byte-identity test: for str/int/float partition
   values the rendered partition segment is unchanged versus `str(value)`.
4. Docs: note in `docs/query.md` (or the SortedField docs section) that datetime partition
   values are canonicalized; CHANGELOG entry.
5. PR body: `Closes #575` and `Closes #570`.

## No-Gos

- No migration/audit tooling — the no-op property is the scope guard; if it fails, stop.
- No change to KeyField identity or `canonical_key.py` itself.
- No new partition_by types or validation (option 2 in #570 — rejection at definition time —
  is NOT taken; canonicalization supersedes it).

## Risks / Rabbit Holes

- **Missed site** = split partitions. The original inventory grep in this plan already proved
  this risk is real — it silently omitted two of the seven sites (see Freshness Check). Use the
  replacement grep, and add a test exercising every path that renders a partition: write
  (`on_save`), partition-change cleanup (`on_save`/`on_delete` old-partition branches),
  `filter_query`, and the three `query.py` decay/index paths.
- **`base.py:3667` drift**: it is correct *because* it appends raw. A future "consistency"
  cleanup that wraps it in `str()` would reintroduce the divergence. Add a comment there
  pointing at `DB_key.__str__` rather than editing the call.
- **1.8.0/1.9.x forward-compat**: partition key bytes change only for datetime partitions,
  which the docs never advertised and no report uses. State this in the CHANGELOG anyway
  (lesson of #476).

## Success Criteria

- All 7 SortedField sites + 3 ConfidenceField sites converted; the replacement grep returns
  zero bare-`str()`/bare-`f":{val}"` partition renders.
- A datetime-partition test proves the write path and `base.py:3667`'s purge path now agree
  (the divergence the Freshness Check found).
- Byte-identity test: str/int/float partition segments unchanged versus `str(value)`.
- New tests green; full non-slow suite green; ruff/black clean; mypy delta 0 (same env,
  measured base-vs-branch per CLAUDE.md's redis-py caveat).

## Documentation

- CHANGELOG.md, `docs/query.md` SortedField/partition notes.

## Critique Results

### Round 1 (2026-09-04, main `73c6a48`) — verdict: NEEDS REVISION

Depth: FULL (3 lenses). All findings verified against working-tree source, not inferred.

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
