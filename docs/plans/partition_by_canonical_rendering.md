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
