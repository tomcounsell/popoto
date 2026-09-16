# Plan: issue #693 — ValidityField declared + save-only = gate never fires

**Status:** decision pending (architect). Documentation arm shipped.
**Issue:** [#693](https://github.com/tomcounsell/popoto/issues/693)
**Related:** #580, #586, #648, #692 (PR #702), PR #582

This plan does not decide anything. It records what was verified against
`main`, lays out the three options the issue names, and asks the architect one
question. The only thing it ships unconditionally is the documentation fix,
which is correct under all three options.

---

## 1. Verification against main

Reproduced in worktree `session/sdlc-693` (base `a45c9eca`), Redis DB 14,
`REDIS_URL` exported before `import popoto`.

| Arm | What it does | `valid_from` card | `invalid_at` card | Exclusion set |
|---|---|---|---|---|
| A — save-only | 5 × `.save()` | 5 | 5 | **0** |
| B — `supersede()` on both claims | control | 2 | 2 | **1** |

Claim-by-claim:

**Claim 1 — "intervals are only ever opened, never closed" on the save path.
CONFIRMED.** `ValidityField.on_save` (`src/popoto/fields/validity_field.py`)
calls `execute_supersede(..., mode="open", assert_valid_from=declared)`. The
gate, `ValidityField.resolve_excluded_keys`, drops a member only on
`invalid_at <= now` or `valid_from > now`; the `+inf` open sentinel matches
neither, as the source comment says. Only `mode="supersede"` / `"invalidate"`
write a finite `invalid_at`, and only `SupersessionProtocol` and
`ProvenanceJournal` pass those modes. Measured: after five plain saves both
ZSETs hold 5 members and the exclusion set is empty.

**Claim 2 — "a plain `.save()` never registers a record as the incumbent for
any identity." CONFIRMED.** `a.save()` then
`SupersessionProtocol.supersede(b, identity_key=ident)` returned `None` and
`ValidityField.is_valid_at(..., a_key)` stayed `True`. The identity's
`{prefix}:open:{digest}` pointer is written only by `SUPERSEDE_LUA` itself, and
`supersede` resolves the incumbent *solely* through that pointer
(`old_member=''`, `GET KEYS[4]`). The behaviour matches the documented contract
in `supersession.py` ("`None` when there was no incumbent … the first claim
about an identity simply opens").

**Independent corroboration from #692 (PR #702).**
`tests/benchmarks/supersession_axis.py` reached the same conclusion from the
producer side and encoded it in a comment: every identity-bearing write,
*including the first claim of a group*, routes through
`save_and_supersede`, because "a plain `.save()` never registers an incumbent,
so a first-claim-plain / second-claim-supersede sequence would close nothing
and reproduce the exact defect #692 exists to fix, one level down." So #693's
mechanism is now load-bearing in shipped benchmark code, and the benchmark
harness is *not* an example of the save-only shape working.

**Claim 3 — the failure is quiet. CONFIRMED.** Both ZSETs are populated after
plain saves, so an operator inspecting Redis sees non-empty validity state.
No exception, no log line. `Defaults.VALIDITY_GATING_ENABLED` is `True` and
`ContextAssembler._resolve_excluded_keys` does run — it returns an empty set,
which the assembler correctly distinguishes from `None`, but the distinction
is invisible to an adopter.

**Correction to one of the issue's own counter-readings.** The issue offers
`observation.py:528` (`_apply_supersession` / `_apply_contradicted`) as a
possible "non-imperative producer … the intended auto-detect surface."
It is not auto-detect. Reading
`src/popoto/fields/observation.py`, that path fires only when the application
(a) calls `ObservationProtocol.on_context_used` explicitly, (b) passes the
outcome string `"contradicted"`, and (c) has set the private
`stale._superseded_by = corrected` attribute beforehand — the docstring calls
that attribute "the documented public mechanism," and with no attribute the
outcome "behaves exactly as before." So it is a *second imperative surface*,
not an inference. It does not weaken the issue's case; it strengthens it,
because it means popoto currently ships **zero** producers that fire without an
explicit per-write application call.

**Not in dispute and worth restating:** the gate is *subtractive* by design.
A record with no entry in either interval ZSET is unmanaged and stays fully
retrievable — that is what keeps adding a `ValidityField` to an existing model
from hiding every pre-existing record. Any option below must preserve it.

**Blast radius today: zero shipped models declare a `ValidityField`**
(`constants.py` says so at the kill switch, and it still holds). Whatever is
decided, nothing in this repo regresses; the cost is entirely borne by future
adopters and by the benchmark harnesses.

---

## 2. Options

### Option 1 — intended as-is, document it

**Sketch.** No code change. State plainly, on the class docstring, on
`docs/features/validity-and-supersession.md`, and in `docs/query.md`'s filter
section, that declaring the field opens intervals and nothing more: exclusion
requires a supersession producer (`SupersessionProtocol.supersede` /
`save_and_supersede` / `invalidate`, `ProvenanceJournal`, or a
`_superseded_by`-tagged `on_context_used` call).

**Blast radius.** Nil. Docs only.

**Against doctrine?** Partly. "Capabilities default ON via auto-detect" is
about capabilities popoto can *infer*. Supersession is a semantic claim —
"this fact replaced that one" — and popoto has no identity concept to infer it
from. Reading the doctrine as "must fire on a bare save" would require popoto
to guess identity, and a wrong guess hides live records: strictly worse than
inertness. The honest reading is that this capability has no auto-detectable
signal *today*, which is exactly what Option 2 supplies.

**What it leaves.** The quiet failure. An adopter who reads "declare the field
and it works" is wrong, and finds out only by measuring, as #586 did.

### Option 2 — declared identity on the field, auto-supersede on save

**Sketch.** Add an optional constructor kwarg naming the model fields that
constitute the record's identity:

```python
class Fact(Model):
    subject = KeyField()
    predicate = Field()
    value = Field()
    validity = ValidityField(identity=("subject", "predicate"))
```

`on_save` then reads those attributes off the instance, derives the digest via
`SupersessionProtocol.identity_key`, and calls `execute_supersede` with
`mode="supersede"`, `old_member=""` and that digest instead of `mode="open"`.
`SUPERSEDE_LUA` already resolves the incumbent from the pointer and already
handles the no-incumbent case (first claim opens, writes no chain link), so the
Lua is unchanged — this is a routing change in `on_save` plus digest
derivation. `identity=None` (the default) keeps today's `mode="open"` exactly.

**Why this is auto-detect and not guessing.** The identity signal is
*declared* by the model author, and the capability then fires on every write
with no per-call-site opt-in — which is the shape the doctrine asks for: the
deploy-level `VALIDITY_GATING_ENABLED` kill switch already exists for adopters
who cannot edit model code, and `identity=` is exactly the kind of one-line
model declaration popoto expects.

**Blast radius.** Larger than it looks; four things need care.

1. **Export/import.** `ValidityField.roundtrip_policy = "carry"` restores all
   six derived keys *after* `save()` has run. With auto-supersede, importing N
   records that share an identity would close each predecessor and rewrite the
   pointer and chain links during the import, before `carry` overwrites the
   interval scores. The scores would end up right; the chain HASHes and the
   `open:` pointer are written by a different code path and may not be.
   **This needs a dedicated test before Option 2 could ship**, and possibly an
   import-time suppression of the auto path.
2. **`assert_valid_from` / `pre_save_validate`.** A re-save that declares a
   different `valid_from` is deliberately loud today (plan D3). Under
   auto-supersede, re-saving the *same* record would look like a new claim on
   its own identity. The routing must skip when `new_member` already equals the
   pointer's current value.
3. **Wall-clock close times.** `supersession_axis.py` already documents the
   trap: passing `time.time()` as `at` opens and closes every interval within
   the same second. `on_save` has only save time available, so a bulk backfill
   would produce zero-length intervals unless `at` comes from a declared field
   value.
4. **`ProvenanceJournal` / append-only models** would start superseding
   themselves if they declare an identity; the interaction needs a look.

**Fallback.** Option 2 leaves the `identity=None` case exactly as inert as
today, so it does not on its own remove the quiet failure — it wants Option 3's
warning alongside it.

### Option 3 — warn when a ValidityField has no reachable producer

**Sketch.** Follow the existing `warn_if_ttl` precedent in the same file: a
module-level `_WARNED` marker set keyed by `(model_name, field_name)` and a
one-time `logger.warning` from `on_save`. The condition is the hard part.
"No reachable producer" cannot be determined statically; the cheap, honest
proxy is: on the Nth plain-`mode="open"` save for a model/field where the
`{prefix}:open:*` keyspace is empty and `chain:fwd` is empty, warn once that
the validity gate is declared but has never excluded anything and name the
producer entry points. Cost is one `EXISTS`/`SCAN` on a threshold crossing,
not per save.

**Blast radius.** Small and contained to a log line. Risk is false positives
(a genuinely young store, or a model where every record legitimately stays
open forever) — which is why it must warn, never raise, exactly as
`warn_if_ttl` does and for the same stated reason.

---

## 3. Recommendation

**Option 2, with Option 3's warning as its `identity=None` arm — i.e. both,
staged.** Reasoning:

- Option 1 alone leaves a capability that is opt-in per write site, which is
  the shape the repo's doctrine exists to rule out. It is not *wrong* — the
  counter-reading about guessing identity is sound — but it treats "we have no
  identity signal" as permanent when Option 2 shows it is a one-kwarg fix.
- Option 2 supplies the missing signal without guessing: the author declares
  identity, popoto does the rest. It reuses `SUPERSEDE_LUA` unchanged, so the
  atomicity guarantees (#588) carry over for free.
- Option 3 covers the residual case Option 2 cannot — a field declared with no
  identity and no producer — and is cheap enough to ship either way.

Staging: ship the documentation arm now (done, see §4); ship Option 3's warning
next as a standalone low-risk change; ship Option 2 behind a plan of its own,
gated on the export/import test in blast-radius item 1.

If the architect prefers to keep supersession strictly imperative, Option 1 +
Option 3 is a coherent and defensible endpoint, and §4 has already landed the
documentation half of it.

---

## 4. Shipped now (correct under all three options)

The docstrings and docs let a reader believe "declare the field and it works."
Fixed in this PR:

- `ValidityField` class docstring (`src/popoto/fields/validity_field.py`): a
  "Declaring the field is not enough" section immediately under the
  declaration example, naming the four producer entry points.
- `src/popoto/fields/validity_field.py` module docstring: the example already
  shows an explicit `execute_supersede`; a sentence now says why that call is
  not optional.
- `docs/features/validity-and-supersession.md`: an admonition in the overview.
- `docs/query.md` `## ValidityField Filters`: a note that `current=True`
  returns everything until a producer closes something.

No behaviour change. `black` and `ruff check src/` clean.

---

## Questions for the architect

**Pick one:** should `ValidityField` gain an optional `identity=(...)`
declaration that makes a plain `.save()` supersede the incumbent automatically
(Option 2, plus the Option 3 warning for the `identity=None` case) — or does
supersession stay strictly imperative, with the inertness documented (already
done) and made loud by a one-time warning only (Option 1 + Option 3)?
