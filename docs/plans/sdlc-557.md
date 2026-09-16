---
status: Ready
type: feature
owner: Valor Engels
created: 2026-09-16
tracking: https://github.com/tomcounsell/popoto/issues/557
last_comment_id: none
---

# Key-regenerating import (`preserve_keys=False`) with reference remapping

## Problem

`import_records` (`src/popoto/transfer/import_.py:315-424`) always preserves the
exported `key` on every record. This is correct for the mainline use case
(back up a database, restore it, migrate the same logical records to a new
host) and is why the docstring can promise "re-running an import converges
rather than duplicating" (`import_.py:324-326`).

It has no answer for a second, narrower use case: merging records from a
source into a destination that may already assign the same key strings to
unrelated data (or where auto-generated keys must not be treated as portable
identifiers across environments). Today that operator has no supported
path — every imported record collides on or overwrites whatever the
destination already stores at that key.

**Current behavior:** `import_records` has no `preserve_keys` parameter.
`Relationship.roundtrip_policy` is `"rebuild"` with a comment that reasons
explicitly from key preservation and cites this issue by number
(`src/popoto/fields/relationship.py:118-122`). No field defines
`remap_references`; the hook does not exist anywhere in `src/popoto/`
(`grep -rc "remap_references" src/popoto/` → 0, `grep -rc "preserve_keys"
src/popoto/` → 0, verified against this worktree at `5955d141`).

**Desired outcome:** `import_records(model_class, stream, ...,
preserve_keys=True, key_map=None)`. With `preserve_keys=True` (default),
behavior is unchanged apart from one reason string (see below). With
`preserve_keys=False`, every record
gets a freshly minted key, every `Relationship` field pointing at a
remapped key is rewritten to point at the new one, and the mapping used is
returned on the report so a caller can chain a second model's import through
the same `key_map`.

## Freshness Check

**Baseline commit:** `5955d1414160d11d30a0d59732874c11359ede05` (this
worktree, synced to `origin/main`).

**Disposition:** No drift requiring re-scoping. This plan is written against
a decision already made by the maintainer after re-reviewing the deferral
(see Prior Art); it does not re-litigate whether to build this.

**Issue claims re-verified:**

- `import_records` has no `preserve_keys` parameter (`import_.py:315-321`);
  `cli.py` has no `--preserve-keys`/`--regenerate-keys` flag
  (`grep -Ec -- '--preserve-keys|--regenerate-keys' src/popoto/transfer/cli.py`
  → 0); `Relationship.roundtrip_policy`'s comment cites #557 by number and
  reasons from key preservation (`relationship.py:118-122`).
- `AutoFieldMixin.get_new_auto_key_value` exists and is the only minting
  path in the codebase (`src/popoto/fields/auto_field_mixin.py:252`),
  inherited by `AutoKeyField(AutoFieldMixin, UniqueKeyField)`
  (`shortcuts.py:796`); `KeyField` (`shortcuts.py:691`) and `UniqueKeyField`
  (`shortcuts.py:744`) do not inherit it and have no minting method.
- `Model.db_key` composes from `sorted(self._meta.key_field_names)`
  (`base.py:727-771`) — every key field in that set contributes, not only
  a single field.
- `Model._get_auto_key_field_name()` returns a field name only when
  **exactly one** field has `auto=True`, and `None` for zero or multiple
  auto fields, including "composite KeyField models" (`base.py:3713-3762`,
  docstring explicit about both exclusions).
- The "already in storage format" branch that makes a plain-string
  `Relationship` rewrite round-trip through `save()` exists at
  `encoding.py:365-368`: `if isinstance(value, str): # Lazy-loaded
  redis_key string — already in storage format`.

**Two merged anti-criteria targeted for retirement by this work** (see
No-Gos-retired below): `docs/plans/generic_export_import_roundtrip.md:1116`
(`` Anti-criterion: `remap_references` not shipped (#557) ``, verified
present) and `docs/plans/transfer_cli.md:929` (`` No `--preserve-keys` flag
(anti-criterion, #557) ``, verified present).

## Prior Art

- **#554 / PR #558** — shipped the export/import API this plan extends.
  `roundtrip_policy` and the `export_state`/`import_state` protocol on
  `Field` are the precedent for `remap_references`: a classmethod hook with
  an identity base implementation, opted into per field type.
- **#555 / transfer CLI plan** — `docs/plans/transfer_cli.md` deliberately
  left `--preserve-keys`/`--regenerate-keys` unclaimed, reasoning that
  fixing either polarity before #557 decided the option's shape would be
  premature (`transfer_cli.md:429-435`). This plan is that decision;
  `--regenerate-keys` follows that section's own naming rule (affirmative of
  the non-default behavior).
- **#557 deferral comment (2026-09-16)** — the maintainer re-reviewed the
  original "defer, no consumer" analysis and scheduled this work anyway. Its
  four "what would unblock this" criteria are answered here, not re-argued:
  (1) *named consumer* — none exists; recorded as an open question, not a
  blocker; (2) *plain-string pointers* — they dangle, documented loudly
  (Solution point 5); (3) *non-auto key fields* — refused outright (Solution
  point 4); (4) *memory budget* — bounded key map, records re-read via seek
  or a spooled temp file (Solution point 3).

## Architectural Impact

- **New dependencies:** none; `tempfile` (stdlib) for the non-seekable spool
  path.
- **Interface changes:** `import_records` gains two optional keyword
  parameters (backward compatible); `ImportReport` gains
  `key_map: dict[str, str]`; `Field` gains one new classmethod,
  `remap_references`, identity by default (existing subclasses unaffected
  unless they override it); `Relationship` overrides it, keeping
  `roundtrip_policy = "rebuild"` (comment rewritten); `popoto-transfer
  import` gains `--regenerate-keys`.
- **Coupling:** `import_.py` gains a dependency on `tempfile` and the new
  hook. No change to `export.py`'s format or `format.py`'s wire shape — the
  record's `"key"` field is still a redis_key string; only *which* string
  is written differs.
- **Data ownership:** unchanged; minted keys use the destination model's
  existing key-generation path (`get_new_auto_key_value` → `Model.db_key`),
  no new Redis key pattern.
- **Reversibility:** high — `preserve_keys` defaults to `True`, so no
  existing caller's behavior changes without explicit opt-in.

## Appetite

**Size:** Medium. **Team:** Solo dev, code reviewer. **Review rounds:** 1
(the partial-remap guarantee and the two-pass memory trade-off are the two
things most likely to draw a CONCERN; both are argued explicitly below
rather than left implicit).

Not Small: a new two-pass mode with a temp-file fallback, a new `Field`
protocol hook exercised by at least one concrete field, a pre-flight refusal
path, and a CLI flag are more than a one-file change. Not Large: no new
dependency, no wire-format change, no change to any field's `on_save`/
index-maintenance behavior, and `preserve_keys=True` stays untouched in
every code path — kept as a separate branch rather than a generalized
"regenerate always, with an identity map" implementation, which would touch
the hot path this plan is forbidden from changing.

## Solution

### Key Elements

- **`import_records(model_class, stream, ..., preserve_keys=True, key_map=None)`**
  — `preserve_keys=True` is the existing single-forward-pass code path,
  completely unchanged. `preserve_keys=False` selects the new two-pass path.
  `key_map` seeds the old→new mapping from a prior run against a *different*
  model; passing it with `preserve_keys=True` raises `ValueError` immediately
  (nothing to remap into on the preserved-key path).
- **`Field.remap_references(cls, field_name, field_value, key_map, **kwargs)`**
  — new classmethod alongside `export_state`/`import_state`
  (`src/popoto/fields/field.py:265-330`). Base implementation returns
  `field_value` unchanged. Called once per field, per record, during pass 2,
  before `model_class(**values)` is constructed.
- **`Relationship.remap_references`** override: `key_map.get(field_value,
  field_value)` when `field_value` is a non-empty `str`, else identity.
  Never dereferences or hydrates a related instance.
- **Pre-flight refusal** for destination models whose key field(s) have no
  minting path, raised before any record is read.
- **`ImportReport.key_map`** — the seed merged with every mapping minted this
  run, for the caller to feed into the next model's import.
- **Dangling-reference accounting** — the report's warnings name the count of
  `Relationship` values remapped and the count left pointing at a key not in
  the map.
- **`popoto-transfer import --regenerate-keys`** — CLI passthrough,
  default off.

### Deviation from the issue's sketched signature

The issue sketches `remap_references(cls, model_instance, field_name,
field_value, key_map)`. This plan drops `model_instance`. Remapping runs
*before* construction: pass 2 rewrites the value inside the record's
`values` dict and only then calls `model_class(**values)`
(`import_.py:243`, the construction line this plan edits), so no instance
exists yet to pass. A parameter that is always `None` at every call site is
dead weight and would invite a future override to reach for it and get a
confusing `None`. This is also recorded under Questions for the architect.

### Flow

**`preserve_keys=True` (default, unchanged):** identical to today —
`iter_lines(stream)` consumed once, forward-only, batched at `BATCH_SIZE`
(`import_.py:55`). No behavior in this plan touches this branch's code path.

**`preserve_keys=False`:**

1. **Pre-flight refusal.** Before reading a byte of the record stream (after
   the manifest is already validated, mirroring `_validate_manifest`'s
   "refuse before writing" contract at `import_.py:70-101`), check
   the eligibility predicate below. On failure raise `ModelException` naming
   the model, its key field name(s) (`model_class._meta.key_field_names`),
   and the reason ("no minting path; `preserve_keys=False` requires the
   model's key to be exactly one `auto=True` field").
2. **Pass 1 — mint.** Spool every record line to a
   `tempfile.TemporaryFile(mode="w+", encoding="utf-8")` while reading
   forward once (see "Two-pass mechanics" — this is unconditional, not a
   fallback). For each record, parse just enough to read `"key"`, mint a new
   key value by calling `get_new_auto_key_value()` **on the `Field` instance
   already registered at `model_class._meta.fields[auto_key_name]`** (it is
   an instance method on a field object constructed at class-definition
   time; do not construct and discard a `Model` instance per mint), then
   compose the redis_key through the model's own key-generation path — never
   hand-format a `ClassName:value` string. Record `old_key -> new_key` in an
   in-memory `dict[str, str]` seeded from the caller's `key_map`.
3. **Pass 2 — construct, remap, write.** Re-read record lines by seeking the
   spooled temp file back to 0. For each record: rewrite every
   field's value via `model_field.remap_references(field_name, value,
   key_map)` (where `Relationship` values get rewritten); rewrite the
   record's own key-field values from the pass-1 mapping; construct, gate,
   save, restore carried state — same per-record logic `_process_batch`
   already runs, with key and reference fields pre-rewritten.
4. **Report.** `ImportReport.key_map` is the seed merged with every minted
   pair. `ImportReport.warnings` gains one line per model run: count of
   `Relationship` values remapped, and count left unmapped (dangling).

### Technical Approach

**Two-pass mechanics and the memory answer.** Resident memory for
`preserve_keys=False` holds only the key map — `O(records)` short strings,
not the records themselves. The record lines are re-read from a spooled
`tempfile.TemporaryFile(mode="w+", encoding="utf-8")` written during pass 1,
**unconditionally — there is no `seek()`/`tell()` fast path on the caller's
stream.**

A `tell()`-and-rewind design was the obvious optimization and was measured
before being rejected, not assumed away. `iter_lines` drives the stream
through the iteration protocol (`for line_number, raw in enumerate(stream,
start=1)`, `format.py:226`), and `import_records` already consumes the
manifest line through it (`import_.py:375-384`) before any rewind point
could be recorded. CPython's `io.TextIOWrapper` disables positional
reporting once a file has been advanced by `__next__`, because of its
internal decode-readahead buffer. Measured on CPython 3.12.14 against a real
2000-line file: `tell()` after one `next()` raises `OSError: telling position
disabled by next() call`. `stream.seekable()` still returns `True` for that
handle, so the failure is invisible to a seekability probe. `io.StringIO`
has no decode buffer and is unaffected — which is exactly the trap, since
**every existing transfer test uses `io.StringIO`** (`test_transfer_roundtrip.py`
and siblings), so a seek-based design would have passed the whole suite and
failed only on `popoto-transfer import`'s real opened file.

Spooling unconditionally removes the failure mode rather than routing around
it, and collapses two code paths into one, so the seekable and non-seekable
cases cannot diverge in behavior or in test coverage.

This deliberately trades away `export.py:46-54`'s single-forward-pass,
bounded-peak-memory chunking property, because a two-pass import must know
the *complete* key map before rewriting any record's references — a
`Relationship` on record 3 may point at the key of record 4000. The cost is
confined to this one branch: `preserve_keys=True` creates no temp file and
holds no key map beyond the batch already in flight.

**Which models may regenerate.** The eligibility predicate is **two
conditions, not one**:

```python
auto_name = model_class._get_auto_key_field_name()
eligible = auto_name is not None and set(model_class._meta.key_field_names) == {auto_name}
```

`_get_auto_key_field_name()` alone is **not** sufficient, and the difference
is a real model shape rather than a hypothetical. That helper returns a name
whenever exactly one field has `auto=True` (`base.py:3739-3762`); it never
consults `key_field_names`. `_meta.add_field` populates `key_field_names` on
`isinstance(field, KeyFieldMixin)` and `auto_field_names` on
`isinstance(field, AutoFieldMixin)` independently (`base.py:259-263`), with
nothing forbidding both on one model. Measured on this worktree:

| model | `_get_auto_key_field_name()` | `key_field_names` | eligible |
|---|---|---|---|
| no declared KeyField (implicit) | `_auto_key` | `{_auto_key}` | yes |
| `id = AutoKeyField()` | `id` | `{id}` | yes |
| `slug = KeyField()` | `None` | `{slug}` | no |
| `slug = KeyField()` + `id = AutoKeyField()` | **`id`** | `{slug, id}` | **no** |
| `a = KeyField()` + `b = KeyField()` | `None` | `{a, b}` | no |

Row 4 is the one the single-condition check gets wrong: it would accept the
model, mint a new `id`, carry `slug` through verbatim, and write a record
whose declared key component still collides with the source record's — a
"regenerated" key that is only half regenerated. `db_key` composes from *all*
`key_field_names` (`base.py:765-771`), so minting the auto component alone
does not fully determine the redis_key. Refused, with the same
`ModelException` as the other ineligible shapes, naming the declared key
field(s) that have no minting path. Multiple `auto=True` fields are already
`None` from the helper (ambiguous) and fail the first condition.

**The partial-remap guarantee.** Only fields that *declare* a reference —
today, only `Relationship` — get remapped. A plain `Field(type=str)` holding
an application-level pointer (e.g. a string the app interprets as "the key
of some other record") is indistinguishable from ordinary text and is never
scanned or rewritten. It dangles silently after regeneration. This is
**never** solved with a heuristic that guesses whether a string "looks like"
a key — that class of guess is unsound (a legitimate text field could match)
and would convert a loud, documented limitation into an occasional silent
corruption. The guarantee is documented in three places: the
`import_records` docstring, `remap_references`'s docstring, and
`docs/guides/export-import.md`.

Cross-model references are remapped only to the extent the caller's
`key_map` covers the target model. A `Relationship` pointing at a model not
included in any prior run's `key_map` keeps its old value and is counted in
the report's "left unmapped" total — the report makes this loud, not
silent.

**Non-idempotency.** `preserve_keys=False` mints a fresh key on every run,
so re-running an import against the same destination creates a second copy
of every record rather than converging — the opposite of every other mode.
Stated in the `import_records` docstring, the `import --help` text, and the
docs page. `on_conflict` is still honored in this mode (a freshly minted key
essentially never collides with an existing one), documented as covering the
vanishing-probability collision case rather than the normal-path merge
semantics it has under `preserve_keys=True`.

There is deliberately **no re-mint retry loop and no new tunable constant**.
A minted key that collides with a key already on the destination is a uuid4
(or ULID/KSUID) collision; it falls through to the existing `on_conflict`
policy, which already has defined, tested behavior for "this key exists".
A retry bound would mean a new `Defaults` constant requiring registration in
`tests/benchmarks/test_defaults_sync.py`, for a branch unreachable without
monkeypatching the minter — cost with no coverage.

**`ValidityField` and `CoOccurrenceField` embed cross-record redis_keys in
carried state — named risk, not fixed by this plan.** A sweep of every
`export_state`/`import_state` implementation in `src/popoto/fields/`
(`confidence_field.py`, `access_tracker.py`, `cyclic_decay_field.py`,
`validity_field.py`, `prediction_ledger.py`, `embedding_field.py`,
`co_occurrence_field.py`) found two whose carried state contains a
*different record's* redis_key, restored via a path `remap_references`
never touches: `ValidityField.export_state`/`import_state`
(`validity_field.py:510-604`) carries `chain_fwd`/`chain_rev` — the
supersession-chain neighbor's redis_key (`hget`/`hset` against
`keys["chain_fwd"]`/`keys["chain_rev"]`, `validity_field.py:573-574,
668-674`), written back verbatim; and `CoOccurrenceField.export_state`/
`import_state` (`co_occurrence_field.py:267-360+`) carries `{"edges":
{target_pk: weight}}`, also restored verbatim. Note the two carry
*different shapes* of cross-record identity: `ValidityField` carries a full
`ClassName:value` redis_key, while `CoOccurrenceField`'s `target_pk` is the
bare pk component (`str(target_pk)`, `co_occurrence_field.py:304-310`), not
a redis_key. `key_map` is keyed by full redis_key, so a future fix cannot
apply one remap to both — the follow-up must convert before looking up. If
the referenced neighbor is regenerated in the same run, both go stale —
pointing at the *old* identity.

Both are `roundtrip_policy = "carry"` fields restored *after* `save()` by
`_restore_state` (`import_.py:151-196`), which has no access to `key_map`;
`import_state`'s signature (`field.py:302-330`) is untouched by this plan.
Extending it to accept `key_map` is out of scope (see No-Gos); this plan
pins the gap with a regression test asserting the *current* stale behavior,
filed as follow-up rather than papered over with an undocumented partial fix.

## Data Flow

End-to-end call order (mechanics detailed in Solution > Flow above):
`import_records(..., preserve_keys=False, key_map=seed)` (or the CLI with
`--regenerate-keys`) → `ValueError` guard on the `key_map`+`preserve_keys=True`
combination → manifest read/validated exactly as today
(`_validate_manifest`, `import_.py:70-101`) → **new** auto-key eligibility
check (`ModelException` here means nothing past the manifest line was read
or written) → **new** pass 1 mints `old_key -> new_key` into `key_map` →
**new** pass 2 re-reads and remaps every field via `remap_references` before
`model_class(**values)` → construction/write-gate/save/carried-state
restoration proceed unchanged (`_process_batch`, `import_.py:209-312`) →
`ImportReport` returned with `key_map` populated and the two new warning
lines appended.

## Failure Path Test Strategy

Every failure mode below is refused (or reported) rather than silently
mis-imported; full scenario detail (fixtures, assertions) lives in Test
Impact to avoid restating it twice:

- Auto-key refusal is pre-flight — no write occurs, key count unchanged.
- Composite auto+declared key refusal — same pre-flight guard, names both
  fields.
- A **real opened file** (not `io.StringIO`) imports correctly — the arm
  that a `tell()`-based design would have failed while the whole existing
  suite stayed green.
- A **non-seekable stream** (a shim whose `seekable()` is `False`) produces
  results identical to the file arm, with the spooled temp file cleaned up.
- An unremapped `Relationship` keeps its old value and is counted in
  `ImportReport.warnings`, never silently dropped.
- A plain-string pointer is asserted byte-identical after import — pinning
  the partial-remap guarantee as a regression, not an incidental pass.
- `ValidityField`/`CoOccurrenceField` staleness is pinned as a passing
  test that documents the *current* (stale) behavior, so a future fix
  changes an explicit assertion instead of quietly flipping an unnoticed one.

## Test Impact

New file: `tests/test_transfer_key_regeneration.py`. Existing transfer tests
(`test_transfer_roundtrip.py`, `test_transfer_reconciliation.py`,
`test_transfer_fidelity_fields.py`, `test_transfer_cli.py`) are unmodified
except for the two anti-criterion lines retired (see No-Gos-retired) — no
other existing assertion changes, because `preserve_keys=True` is a distinct,
untouched code path.

`tests/test_transfer_key_regeneration.py` covers:

- **Self-referential `Relationship`**, same database: imported copies point
  at new keys, originals untouched, no eager hydration (assert via a
  spy/counter on `field.model.query.get`/instance construction — the
  related object is never dereferenced during remap, only the string is
  rewritten).
- **Circular reference pair (A→B, B→A)** survives regeneration with both
  pointers remapped — catches an eager-resolution regression, since eager
  resolution of A's pointer to B would recurse into resolving B's pointer
  to A.
- A `Relationship` target **absent from the key map** is reported as
  unremapped in `ImportReport.warnings` and keeps its old value.
- A plain-string application-level pointer is **not** remapped.
- A `Relationship` whose stored value is `None` (legal — `null=True`,
  `relationship.py:116`) passes through `remap_references` unchanged and
  raises nothing. Pins the null branch of the one concrete override shipping
  here.
- A model with a plain `KeyField` is **refused before any write** (assert the
  destination key count is unchanged).
- A model with a **mixed composite key** (`slug = KeyField()` plus
  `id = AutoKeyField()`) is **refused before any write**. This is the case a
  single-condition `_get_auto_key_field_name() is not None` check accepts;
  without this test the predicate bug is invisible.
- The three input shapes agree: a **real opened file**, an `io.StringIO`, and
  a **non-seekable shim** all produce the same set of imported records. The
  real-file arm is mandatory — every existing transfer test uses
  `io.StringIO`, which is precisely the stream type that hides the
  `tell()`-after-`next()` failure.
- `preserve_keys=True` keeps the old record flow: no temp file created
  (assert via monkeypatching `tempfile.TemporaryFile` to raise if called on
  this path) and re-import converges. The one deliberate difference is the
  `ERRORED` reason for a record with no usable `key`, now the shared
  `_NO_KEY_REASON` text so both paths explain it the same way.
- `key_map` **seeding across two models** chains correctly: import model A
  with `preserve_keys=False`, feed `report_a.key_map` into model B's import,
  assert B's `Relationship` fields pointing at A's old keys resolve to A's
  new keys.
- `key_map` passed together with `preserve_keys=True` raises `ValueError`.

Tests run with `POPOTO_TEST_DB=6` in this lane (per this lane's assignment;
never DB 0).

## Rabbit Holes

- **Extending carried-state (`export_state`/`import_state`) to accept
  `key_map`.** Would fix the named staleness, but changes a second
  protocol's signature in the same PR as introducing the first, doubles the
  reviewer's surface, and risks the same eager-resolution recursion trap
  `Relationship` was designed to avoid. Left as named follow-up work.
- **A `key_factory` callable for non-auto-key models.** The obvious
  extension once refusal ships. Deliberately not built here — see Questions
  for the architect; refusal is the conservative default until a real need
  surfaces.
- **Heuristically detecting application-level string pointers.** Rejected
  outright, not deferred — see the partial-remap guarantee above.
- **Making `preserve_keys=False` the new default.** Would break every
  current caller's idempotency guarantee. Not considered.

## Risks

1. **Two-pass import trades away the streaming design's memory bound.** A
   very large export held in one `preserve_keys=False` run keeps an
   `O(records)` key map resident, and a non-seekable input additionally
   spools the full record body to disk. Mitigation: documented in Technical
   Approach as a deliberate, scoped trade confined to this one branch; a
   future disk-backed key map is out of appetite, not fixed here.
2. **`ValidityField`/`CoOccurrenceField` carried state goes stale under
   regeneration.** A supersession chain or co-occurrence edge silently
   points at a key that no longer exists after regenerating a model that
   uses either field. Mitigation: named above with file:line citations,
   pinned with a regression test documenting the *current* stale behavior,
   filed as follow-up rather than folded in under appetite pressure.
3. **Minted-key collision within a single run.** Two records could
   theoretically mint the same new key. Mitigation:
   `get_new_auto_key_value`'s strategies (uuid4/ulid/ksuid) are
   collision-resistant by construction; a bounded retry against the in-run
   map and seeded `key_map` values closes the remaining gap. Tested by
   forcing a collision via monkeypatch.

## Race Conditions

No new race condition beyond what `import_records` already documents
(`import_.py:357-360`: "Import is not atomic across records"). Pass 1 and
pass 2 both run within a single `import_records` call with no concurrent
writer assumed, same as today. The only new timing fact: between pass 1
minting a key and pass 2 writing the record under it, nothing else in this
process touches that key, so no new collision window is introduced beyond
Risk 3 above (which is about two records within the *same* pass 1 minting
the same key, not a cross-process race).

## No-Gos (Out of Scope)

- Extending `Field.export_state`/`import_state` to accept `key_map` (Risk 2 /
  Rabbit Holes). Filed as follow-up, not built here.
- A `key_factory` callable letting non-auto-key models regenerate (Questions
  for the architect). Refusal only, this plan.
- Any heuristic scan for application-level string pointers that "look like"
  keys. Rejected outright, not merely deferred.
- Changing `preserve_keys=True`'s code path in any way. It must remain the
  exact single-forward-pass, no-buffering, no-temp-file behavior that exists
  today.
- Changing the JSON Lines wire format (`format.py`). Records still carry a
  `"key"` string field; only which string is written under regeneration
  changes.

### No-Gos retired by this work

- `docs/plans/generic_export_import_roundtrip.md:1116` — ``Anti-criterion:
  `remap_references` not shipped (#557)`` is retired: `remap_references`
  ships as a `Field` classmethod in this PR.
- `docs/plans/transfer_cli.md:929` — ``No `--preserve-keys` flag
  (anti-criterion, #557)`` is retired: `--regenerate-keys` ships on
  `popoto-transfer import` in this PR (named as the affirmative of the
  non-default behavior, per that plan's own naming rule at
  `transfer_cli.md:429-435`, matching the existing `store_true` idiom used
  by `--allow-db0`/`--json` in `cli.py:184-193`).

## Documentation

### Feature Documentation

- [x] `docs/guides/export-import.md` — new subsection under `## Importing`
      documenting `preserve_keys`/`key_map`, the non-idempotency warning,
      the partial-remap guarantee (plain-string-pointer caveat stated
      prominently), and the multi-model `key_map` chaining pattern with a
      worked two-model example; extend `## From the command line` (#555)
      with `--regenerate-keys` and its non-idempotency warning.
- [x] `CHANGELOG.md` — `### Added` entry under `[Unreleased]` naming
      `preserve_keys`, `key_map`, `remap_references`, and
      `--regenerate-keys`, flagging the non-idempotency change relative to
      every other import mode.

### Inline Documentation

- [x] `import_records` docstring: `preserve_keys`/`key_map` args, a
      non-idempotency `Note:`, and the partial-remap guarantee restated
      verbatim — this is guarantee-location 1 of the required 3.
- [x] `Field.remap_references` docstring: signature, identity default, when
      to override, and the partial-remap guarantee restated — location 2 of 3.
- [x] `docs/guides/export-import.md`'s guarantee text above is location 3
      of 3; `Relationship.remap_references` docstring separately explains
      why it rewrites the string without dereferencing, citing
      `encoding.py`'s "already in storage format" branch as the round-trip
      proof.
- [x] Rewrite `Relationship.roundtrip_policy`'s comment
      (`relationship.py:118-122`) to drop "since v1 always preserves keys
      ... see #557 for the deferred opt-out" — the value is unchanged
      (`"rebuild"`), but the reasoning must state that the reverse-lookup
      Set is rebuilt by `on_save` from the (possibly remapped) stored value,
      true under both `preserve_keys` settings.

### Build finding: construction resolves relationship strings eagerly

Measured on this branch (CPython 3.12.14, redis-py 8.1.0), and the reason
`_process_batch` re-asserts reference values after construction.

`Model.__init__` (`src/popoto/models/base.py:646-656`) does **not** honor the
documented lazy-loading contract for a `Relationship` supplied as a redis_key
string: it calls `field.model.query.get(redis_key=...)` immediately and
substitutes `None` when the target is not present. Records are written one at
a time in file order, so under any import the first half of a cycle -- and
every forward reference -- points at a key that does not exist yet at its own
construction time, and the reference is silently dropped.

This is pre-existing and **not** introduced by #557: an import with the
default `preserve_keys=True` into an empty database loses the same reference,
which was reproduced directly before any of this work was written. It is left
unfixed at its source (that is a change to `Model.__init__`'s semantics,
outside this appetite) and worked around inside the regenerating path only:
`_remap_record` stashes the rewritten strings under
`REMAPPED_REFERENCES_KEY`, and `_process_batch` re-assigns them onto the
instance after construction, which stores them verbatim. The preserving path
is untouched, and a value of that name read off a file is discarded rather
than honored.

`tests/test_transfer_key_regeneration.py::test_circular_pair_is_remapped_in_both_directions`
is the regression pin.

## Success Criteria

- [x] `preserve_keys=True` (default) keeps the pre-existing behavior: no
      temp file created, no `key_map` computation, identical record flow to
      `main` before this change. Only the no-`key` `ERRORED` reason string
      changed, deliberately and in both paths.
- [x] `preserve_keys=False` refuses before any write for a model whose key
      field(s) have no minting path (plain `KeyField`/`UniqueKeyField`, or a
      composite mixing an auto field with declared key fields), naming the
      model, the key field(s), and the reason.
- [x] `preserve_keys=False` round-trips a self-referential `Relationship`
      into the same database: imported copies point at new keys, originals
      untouched, no eager hydration.
- [x] A circular reference pair (A→B, B→A) survives regeneration with both
      pointers correctly remapped.
- [x] A `Relationship` target absent from `key_map` is reported by name/count
      in `ImportReport.warnings` and keeps its old value.
- [x] A plain-string application-level pointer is never rewritten.
- [x] A real opened file, an `io.StringIO`, and a non-seekable shim all
      produce the same imported records via the unconditional temp-file spool
      path, with the temp file cleaned up. No `seek()`/`tell()` is called on
      the caller's stream.
- [x] `key_map` seeds correctly across two chained model imports.
- [x] `key_map` passed with `preserve_keys=True` raises `ValueError`.
- [x] `ValidityField`/`CoOccurrenceField` cross-record key staleness under
      regeneration is pinned by an explicit regression test, not silently
      passing or silently "fixed".
- [x] `popoto-transfer import --regenerate-keys` wraps `preserve_keys=False`
      and its `--help` text states the non-idempotency warning.
- [x] Both merged anti-criteria (`generic_export_import_roundtrip.md:1116`,
      `transfer_cli.md:929`) are retired: `remap_references` exists in
      `src/popoto/`, and `--regenerate-keys` exists in `cli.py`.
- [x] `mypy_ratchet.py` does not regress (or is explicitly re-banked per
      `CLAUDE.md`'s ratchet instructions).
- [x] `ruff check src/` and `black --check src/ tests/` pass.
- [x] Tests pass (`/do-test`), including
      `tests/test_transfer_key_regeneration.py` and the unmodified existing
      transfer suite, run with `POPOTO_TEST_DB=6`.
- [x] Documentation updated (`/do-docs`).

## Questions for the architect

The architect is AFK; these are recorded for the record, not blocking:

1. **No named consumer.** This ships the mechanism the issue's own deferral
   comment argues has no requester — the sweep of all open issues found
   none, and the original motivating consumer (#554's `Memory` migration)
   requires the opposite (key preservation). We proceed because the
   maintainer re-reviewed that analysis and scheduled the work anyway.
2. **Dropped `model_instance` parameter.** The issue sketches
   `remap_references(cls, model_instance, field_name, field_value,
   key_map)`. This plan drops `model_instance` because remapping runs before
   `model_class(**values)` is called — no instance exists yet at that point
   in pass 2, so the parameter would always be `None`. Flag if a future
   caller shape needs the instance (e.g., a hook that wants to compare
   against other fields already on the record) — the parameter can be added
   back once a concrete need exists.
3. **Refusal vs. a `key_factory` callable for non-auto-key models.** This
   plan refuses `preserve_keys=False` outright for any model without a
   single unambiguous auto-key field. The obvious future extension is a
   caller-supplied `key_factory(model_class, old_values) -> new_key_value`
   callable that lets a `KeyField` model participate. Not built here because
   no consumer has asked for it (see question 1) and refusal is the
   conservative default; flag if this should be built proactively instead
   of on demand.
