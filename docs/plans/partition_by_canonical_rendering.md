---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-03
tracking: https://github.com/tomcounsell/popoto/issues/575
last_comment_id: none
---

# #575 / #570 — Route SortedField(partition_by=...) values through canonical_key_str()

## Problem

`SortedField(partition_by=...)` renders partition values with bare `str(value)` at multiple
sites in `src/popoto/fields/sorted_field_mixin.py` and `src/popoto/models/query.py`. The sites
are consistent with each other, so nothing is broken today — but a `datetime` partition value
carries exactly the aware/naive `str()` fragility #537/#538 fixed for `KeyField` identity in
PR #548: the same instant can land in different partitions depending on how it decoded. Silent,
no error, visible only as queries returning fewer rows than expected.

Two issues describe this: #575 (the generic finding, with the suggested `canonical_key_str`
fix) and #570 (the datetime-specific duplicate, filed with a measure-first framing). One fix
closes both — `canonical_key_str()` is a no-op (`str(value)`, byte-identical) for every
non-datetime value, so existing stored partition keys are unaffected unless someone already has
datetime partitions with mixed representations, which no report suggests exists.

## Freshness Check

To be re-verified at build time against current main: the cited sites were measured at PR #548
HEAD `154a9d6` (`sorted_field_mixin.py:475,546,629,753`; `query.py:369,1176,1218`) and
`query.py` has since been reshaped by #594 — **re-run the site inventory before editing**:

```bash
git grep -n 'str(' -- src/popoto/fields/sorted_field_mixin.py src/popoto/models/query.py | grep -i partition
```

Every partition-rendering site found must be converted; consistency across ALL sites is the
invariant (a partial conversion is worse than none — it splits partitions immediately).

**Disposition: Minor drift expected** (line numbers only; #548's `canonical_key_str` helper in
`src/popoto/models/canonical_key.py` is on main and stable).

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

1. Convert every partition-value rendering site to `canonical_key_str(value)`.
2. Regression tests: a model with `SortedField(partition_by=<datetime field>)` — an aware
   datetime and its UTC equivalent land in the SAME partition; naive datetimes partition
   consistently; write-path and every read/query path agree on the partition key (round-trip
   through save → filter-by-partition). Plus a byte-identity test: for str/int/float partition
   values the rendered partition segment is unchanged versus `str(value)`.
3. Docs: note in `docs/query.md` (or the SortedField docs section) that datetime partition
   values are canonicalized; CHANGELOG entry.
4. PR body: `Closes #575` and `Closes #570`.

## No-Gos

- No migration/audit tooling — the no-op property is the scope guard; if it fails, stop.
- No change to KeyField identity or `canonical_key.py` itself.
- No new partition_by types or validation (option 2 in #570 — rejection at definition time —
  is NOT taken; canonicalization supersedes it).

## Risks / Rabbit Holes

- **Missed site** = split partitions. Mitigate with the grep inventory plus a test that
  exercises every query path that renders a partition (filter, range query, count, delete/index
  cleanup if they render partitions — follow the inventory).
- **1.8.0/1.9.x forward-compat**: partition key bytes change only for datetime partitions,
  which the docs never advertised and no report uses. State this in the CHANGELOG anyway
  (lesson of #476).

## Success Criteria

- All sites converted (grep inventory returns zero bare-`str()` partition renders).
- New tests green; full non-slow suite green; ruff/black clean; mypy delta 0 (same env).

## Documentation

- CHANGELOG.md, `docs/query.md` SortedField/partition notes.
