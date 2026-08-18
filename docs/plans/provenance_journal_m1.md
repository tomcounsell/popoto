---
status: Ready
type: feature
appetite: Large
owner: Valor Engels
created: 2026-08-17
tracking: https://github.com/tomcounsell/popoto/issues/560
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-08-18T03:07:09Z
---

# M1 — Provenance journal: append-only entry model with confirm/supersede/retract annotations

## Problem

An agent is told, in a Slack thread, "Tom said the launch slipped to the 30th." Two turns
later Tom himself says "actually the 30th is wrong, it's the 27th." Today Popoto stores both
as `DefaultMemory` rows carrying `agent_id, content, importance, relevance, confidence`
(`src/popoto/recipes/default_memory.py:116-127`). Nothing records *who* said each one,
*which turn* it came from, the *exact words* used, or that the second statement corrects the
first. If the extraction pipeline later revises a memory, it overwrites the row: the original
words are gone and there is no way to audit what the agent believed at any past moment.

**Current behavior:**

- `SubconsciousMemory.extract_memories()` writes each fact with `instance.save()` and no
  pipeline (`src/popoto/recipes/subconscious_memory.py:408-432`), populating only the field
  set above. There is no speaker, turn id, verbatim source span, subject tagging, or
  stated-vs-inferred marker anywhere on a memory model. `grep -rn 'speaker' src/` returns
  zero hits.
- Records are mutable rows. `Model.save()` on an existing key overwrites it. Nothing in
  `src/popoto/models/base.py` forbids re-save or delete for a model that wants immutability;
  the only immutability precedent is `KeyMutationError` (`base.py:1217-1230`), which guards
  key mutation, not value mutation.
- No memory can reference another as confirming, superseding, or retracting it. V0 (#580)
  shipped supersession *chains* as derived state (`$ValidityF:{Model}:{field}:chain:fwd|rev`
  hashes), but there is no entry model to hang them on and no annotation record type.
- "Contradiction" is a scalar counter on `ConfidenceField`, with `_apply_contradicted`
  (`src/popoto/fields/observation.py:327-388`) recording nothing about *what* contradicted
  the record.

**Desired outcome:**

An append-only journal where every capture *that clears the never-record firewall* is
immutable and fully attributed — speaker, turn id, verbatim span, subjects,
stated-vs-inferred — and corrections are *new entries* pointing at prior entries. Given any
entry, every annotation targeting it is one query away. When a `supersede` or `retract`
annotation lands, its target leaves `validity__current` membership in the same transaction,
with no chain walk at read time.

Two boundaries stated up front, because the rest of the plan is written against them rather
than around them:

- **"Nothing is destroyed" is scoped to what gets stored.** A capture blocked by the M2
  firewall is dropped *before* storage and leaves only a content-free tombstone in the
  `$NR:` keyspace, which is not part of the journal and is not returned by any journal
  query. The journal-side signal is a gap in `turn_id` coverage, nothing more. M1
  deliberately writes no placeholder entry (see D8).
- **Immutability is an ORM-layer contract, not a storage guarantee.** It holds against every
  Python write path in `models/base.py`. It does not hold against a raw Redis client, and
  the repo's own migration cookbook (`src/popoto/models/migrations.py:277-295`) teaches a
  `delete` + re-`hset` recipe that bypasses it by construction. This is documented on the
  feature page, not papered over.

## Freshness Check

**Baseline commit:** `9180680` (`docs: pin the REDIS_URL-only connection binding hazard for ad-hoc scripts (re #577)`)
**Issue filed at:** 2026-08-13T06:26Z
**Disposition:** Minor drift

**File:line references re-verified:**

- `src/popoto/recipes/default_memory.py:97-107` — "extracted memories carry only
  agent_id/content/importance/relevance/confidence" — **drifted to `:116-127`** (`:97-107`
  is now the class docstring; the class declaration moved to `:96`). Claim still holds, with
  one addition the issue predates: the class is now
  `DefaultMemory(NeverRecordMixin, AccessTrackerMixin, Model)` as of #587. No speaker, turn,
  verbatim, or subject field exists.
- `src/popoto/fields/prediction_ledger.py:90` — `PredictionLedgerMixin` owns "ledger" —
  **still holds, exact line.** The "do not name this a ledger" constraint stands.
- `src/popoto/recipes/memory_lifecycle.py:120` — `Tombstone` — **still holds, exact line**
  (`@dataclass class Tombstone`).
- `src/popoto/fields/event_stream.py:61` — `EventStreamMixin` — **still holds, exact line.**
- `src/popoto/fields/event_stream.py:185-190` — XADD failures swallowed — **drifted to
  `:189-200`.** Behavior identical: `except Exception` at `:189`, pipeline re-raise at
  `:193-196`, non-pipeline `logger.warning` swallow at `:196-200`.
- `src/popoto/fields/event_stream.py:192` — `_xadd_event` — **drifted to `:202`** (shifted by
  31535a3, #554 export/import).
- `src/popoto/streams/consumer.py:84` — `StreamConsumer` — **drifted:** the class is at
  `:51`; `:84` is now mid-`__init__` signature. Class exists and is usable as cited.
- `src/popoto/fields/tag_field.py:23-25` — convention prefixes endorsed — **still holds,
  exact lines**, including the "not a security boundary" caveat that constrains how
  `subjects` may be described in docs.

**The issue's grep claim is stale and should not be quoted forward.** The issue says
`grep -rn 'supersede|provenance|speaker|verbatim' src/` returns zero hits.

Re-measured at `9180680` in this worktree, case-insensitive, counting **matching lines** (a
line with two hits counts once — which is why the per-token rows below do not sum to the
total; several lines match more than one token):

| Command | Matching lines |
|---|---|
| `grep -rniE 'supersede\|provenance\|speaker\|verbatim' src/` | **137** |
| `grep -rniE 'provenance' src/` | 41 |
| `grep -rniE 'supersede' src/` | 80 |
| `grep -rniE 'verbatim' src/` | 18 |
| `grep -rniE 'speaker' src/` | **0** |
| `grep -rniE 'journal' src/` | **2** |

What each bucket actually is:

- **`provenance` (41)** — pre-existing and unrelated to memory entries: export/import manifest
  provenance (`transfer/export.py`, `transfer/import_.py`, `transfer/format.py`,
  `transfer/results.py`) and "identity provenance" comments in `models/encoding.py:418,442,493,630`.
  These predated the issue, so the "zero hits" claim was inaccurate when written.
- **`supersede` (80)** — almost entirely new via #582: `validity_field.py`
  (`SUPERSEDE_LUA:157`, `validity__current:66`), `supersession.py`, `query.py:427`,
  `constants.py:131`. Plus pre-existing `_superseded_by` plumbing in `observation.py:167-483`.
- **`verbatim` (18)** — all prose and behavioral description, **no stored field**:
  `extraction/__init__.py:16,189`, `integrations/mcp_server.py:87`, `integrations/service.py:115`,
  `default_memory.py:5,59`, `trajectory_memory.py:361`.
- **`speaker` (0)** — completely absent.
- **`journal` (2)** — both are forward references to this issue, not an implementation:
  `supersession.py:25` and `validity_field.py:29` each say the primitive is shaped so "an
  append-only journal (#560) can adopt this field/protocol unchanged."

The accurate form of the claim, which this plan builds on: **no memory model stores speaker,
turn id, a verbatim source span, subject tags, a stated-vs-inferred marker, or an
entry-to-entry annotation pointer, and no model in `src/` is append-only.** The CREATE
verdict stands.

**Cited sibling issues/PRs re-checked:**

- **#580 (V0, ValidityField)** — CLOSED 2026-08-17T05:58 via **PR #582 (merged, `a4f7fbf`)**.
  Shipped `src/popoto/fields/validity_field.py` (incl. `SUPERSEDE_LUA`, bi-temporal
  `valid_from`/`invalid_at`/`ingested_at`, `validity__current` filter),
  `src/popoto/fields/supersession.py`, assembler gating, query integration, docs at
  `docs/features/validity-and-supersession.md`, tests at `tests/test_validity_field.py`.
  Both symbols exported from `popoto/__init__.py:56-57`. **The #560 Amendment (2026-08-16) is
  therefore satisfiable as written — its prerequisite is on main.**
- **#561 (M2, never-record firewall)** — CLOSED 2026-08-17T08:59 via **PR #587 (merged,
  `337b3f0`)**. Shipped `src/popoto/privacy/never_record.py` (`NeverRecordMixin`), gated at
  `base.py:1232-1244` as step 0 of `save()`. **The issue predates M2 and does not mention
  it. This plan composes it** — a journal that stores verbatim human speech is precisely the
  surface the firewall exists to protect.
- **#564 (M5), #565 (M6), #562 (M3), #563 (M4)** — all open, downstream consumers only.
- **#588** — filed by this plan's spike (see Spike Results); it records a V0 pipeline trap
  that M1 routes around.

**Commits on main since issue was filed (touching referenced files):**

- `337b3f0` (#561/PR #587) — added `NeverRecordMixin` to `DefaultMemory`, touched
  `models/base.py`, `recipes/subconscious_memory.py` — **changes the composition surface**
  (M1 must compose the firewall).
- `a4f7fbf` (#580/PR #582) — **supplies M1's validity dependency**, as designed.
- `e220b2e` — touched `default_memory.py` — irrelevant to the field set.
- `31535a3` (#554) — touched `event_stream.py` — line-number drift only.
- `3b21c7c` (#540) — touched `tag_field.py`, `consumer.py`; moved index pointer side keys
  outside the model key space. **Relevant to test teardown**: derived index keys now live
  under `$*`, so raw-key cleanup must sweep `$*`, not just `Model:*`.
- `prediction_ledger.py`, `memory_lifecycle.py` — untouched.

**Active plans in `docs/plans/` overlapping this area:** none claim the journal.
`validity_primitives_v0.md` (the V0 dependency) and `never_record_firewall.md` are adjacent
and were both read in full before writing this plan. Nothing conflicts.

**Expected-failure test search:** `grep -rn 'pytest.mark.xfail\|pytest.xfail(' tests/`
returns **zero hits**. No xfail placeholders are staked out for this work, so there are no
xfail-to-assertion conversions in scope.

**Notes:** Three line references need correcting when quoted (`default_memory.py:116-127`,
`event_stream.py:189-200` and `:202`, `consumer.py:51`), and the grep sentence must be
restated as above rather than repeated verbatim.

## Prior Art

`gh issue list --state closed --search "provenance journal append-only"` returns only #580;
`gh pr list --state merged --search "provenance journal append-only"` returns only #582.
**There is no prior attempt at an append-only journal.** The relevant prior art is
structural — four merged patterns this plan reuses rather than reinvents:

- **#580 / PR #582 (V0 validity primitives)** — the direct dependency. Established
  `SUPERSEDE_LUA`, the `execute_supersede()` classmethod as the single seam that knows the
  script's KEYS/ARGV order, key-helper classmethods on the field, `get_interval_keys()` as
  the stable consumer seam, and the doctrine that **validity gating is subtractive, never a
  whitelist** (`validity_field.py:621-637`). V0 explicitly anticipates M1:
  `validity_field.py:29` and `supersession.py:24-27` state that chain links are kept as
  derived state in two hashes *specifically so* "an append-only journal (#560) can adopt this
  field/protocol unchanged." Outcome: shipped, and M1 adopts it unchanged.
- **#561 / PR #587 (never-record firewall)** — established the mixin-gated-in-`save()`
  pattern (`base.py:1232-1244`), the `_read_*_switch()` env-backed deploy kill switch
  (`POPOTO_NEVER_RECORD_DISABLE`), and the "audit failure must never fail the gate open"
  discipline. Outcome: shipped; M1 composes `NeverRecordMixin` directly.
- **#492 / TagField** — established multi-value tag storage as one Redis Set per tag value
  with a pointer side key, `filter(tags__contains=...)` / `__any` / `__all`, and the
  "convention over schema, **not a security boundary**" framing (`tag_field.py:22-31`).
  Outcome: shipped; M1's `subjects` is a `TagField` and inherits that framing verbatim.
- **#476 / PR #477 (index-pointer forward-incompat)** — the reason V0's atomicity constraint
  exists and the reason indexed-field EVALs run eagerly ahead of the internal pipeline
  (`base.py:1364-1379`, `:1562-1583`). Outcome: shipped; it constrains M1's choice of
  `IndexedField` for `target` (see Race 1).

No prior fix failed here, so there is no **Why Previous Fixes Failed** section.

## Research

**Queries used:**

- append-only event-sourced provenance store on Redis — immutability enforcement patterns
- bi-temporal valid-time vs transaction-time modeling for agent memory / knowledge stores
- provenance annotation models: confirm / supersede / retract as first-class records

**Key findings:**

- **Immutability in a key-value store is an application-layer contract, not a storage
  primitive.** Redis and Valkey have no per-key write-once mode; `SETNX`/`HSETNX` are the
  only atomic "create-if-absent" primitives and neither covers a multi-field HSET plus the
  index writes a Popoto model performs. The industry pattern (Kafka log compaction off,
  EventStoreDB append-only streams, Datomic's immutable datoms) is the same shape this plan
  takes: enforce at the write API, keep corrections as new facts, and compute the current
  view at read time. Informs the Technical Approach decision to enforce in a mixin's
  `save()`/`delete()` override and to accept that the guard is advisory against a caller
  that bypasses the ORM (documented, not silently assumed).
- **Bi-temporal modeling separates valid time from transaction time**, and the near-universal
  failure mode is conflating them — backdating a correction and having the store record the
  backdate as the ingest time, or vice versa. V0 already models both axes
  (`valid_from`/`invalid_at` vs `ingested_at`). Informs the decision to make `ingested_at`
  the entry timestamp and `valid_from` the journal's valid-time axis, exactly as the #560
  amendment specifies, and to test the backdating case explicitly (Spike B found a live
  silent-skew hazard here — see #588).
- **Annotation-as-record ("reification") vs typed annotation tables** is a long-settled
  tradeoff in provenance systems (W3C PROV, RDF reification). The single-record-type form
  wins when annotations must themselves be annotatable — a retraction of a retraction, a
  confirmation of a supersession. Informs the resolution of the issue's open question in
  favor of one model with `kind` + `target`.

No external library or API dependency is added by this work, so there is no version or
migration-guide finding to carry.

## Spike Results

Three spikes ran against this worktree at `9180680`, Python 3.12.13, redis-py 8.1.0, with
`REDIS_URL=redis://localhost:6379/12`. No repo file was touched; all test keys were removed
(`redis-cli -n 12 dbsize` → 0 afterward).

### spike-1: Can append-only be enforced by a mixin overriding `save()`/`delete()`, and what is the reliable "already persisted" signal?

- **Assumption**: "`self._db_content` or `self._saved_field_values` tells us an instance is
  already persisted, and a mixin override covers every write path."
- **Method**: prototype
- **Finding**: **The assumption is false for the state attributes and true for the
  override.** Only an `EXISTS` on `self.db_key.redis_key` catches all three re-save shapes:

  | Case | `_db_content` | `_saved_field_values` | `EXISTS` | caught? |
  |---|---|---|---|---|
  | fresh `.save()` | False | False | False | allowed (correct) |
  | same instance `.save()` again | True | True | True | yes |
  | `Entry(entry_id="e1").save()` — new object, colliding key | **False** | **False** | True | **`EXISTS` only** |
  | `query.get(...)` then `.save()` | **False** | True | True | **`_db_content` fails** |
  | `.delete()` | — | — | — | raised |
  | `Model.delete_all()` | — | — | — | **raised — override is honored** |

  Two traps to name in the build: `_db_content` is **empty on a query-loaded instance**,
  despite `base.py:1259`'s `_is_create = ... not self._db_content`; that logic is
  EventStream-specific and is not a persisted-ness signal. And `_saved_field_values` is empty
  on a fresh Python object with a colliding key — the exact shape a retry or duplicate ingest
  takes.

  **Nothing escapes the override.** `delete_all()` → `bulk_delete()` (`base.py:2919`) calls
  `instance.delete(pipeline=...)` per instance, so the guard fires (verified: keys survived,
  exception raised). Every write path — `create` (`:1702`), `get_or_create` (`:1773`),
  `update_or_create` (`:1789`), both bulk-save sites (`:2717`, `:2861`) — routes through
  `instance.save()`.
- **Confidence**: high
- **Impact on plan**: `AppendOnlyMixin.save()` gates on `EXISTS`, not on instance state.
  Two consequences become explicit plan content: (a) the `EXISTS` check costs one extra round
  trip and is **TOCTOU-racy** — two concurrent first-saves of the same key both see 0 (Race 2);
  (b) `delete_all()` becomes unusable on the journal model, which breaks the standard test
  teardown idiom, so the plan must ship an explicit admin/retention escape hatch.

### spike-2: Can "append the annotation entry and close the target's interval" be one atomic operation?

- **Assumption**: "`SupersessionProtocol.invalidate(target, superseded_by=new, pipeline=pipe)`
  queued after `new.save(pipeline=pipe)` closes the target atomically."
- **Method**: prototype
- **Finding**: **The assumption is false, and it fails silently.**
  `SupersessionProtocol._member_key` (`supersession.py:376-394`) gates on
  `POPOTO_REDIS_DB.exists(member)`. The successor's `HSET` is only *queued*, so `EXISTS`
  returns 0, `invalidate` takes its "unsaved successor → no-op" branch
  (`supersession.py:246-253`), and returns `None` — **indistinguishable from its normal
  pipeline-mode return**. Observed: 3 queued commands (`HSET`, `SADD`, and the `mode=open`
  EVAL from the successor's own `on_save`) — **no invalidate EVAL**; afterward the target is
  still `validity__current=True` and both chain hashes are empty.

  Dropping to `ValidityField.execute_supersede(...)` directly — it does no `EXISTS` gating —
  works:

  ```python
  pipe = popoto.get_redis().pipeline()      # transaction=True by default -> real MULTI/EXEC
  annotation.save(pipeline=pipe)
  ValidityField.execute_supersede(
      JournalEntry, "validity",
      new_member=annotation.db_key.redis_key, mode="invalidate",
      now=t, valid_from=t, ingested_at=t, close_at=t,
      old_member=target.db_key.redis_key, pipeline=pipe,
  )
  pipe.execute()
  ```

  Verified after `execute()` (4 queued commands, `pipe.transaction is True`):
  `invalid_at[target]` finite, `invalid_at[annotation] == inf`, `chain_fwd = {target:
  annotation}`, `chain_rev = {annotation: target}`, `filter(validity__current=True)` returns
  the annotation and excludes the target, and `SupersessionProtocol.superseded_by(target)`
  hydrates the annotation.

  **Secondary finding:** in that shape the `valid_from` passed to `execute_supersede` does
  **not** set the successor's valid-time. The successor's own `on_save` EVAL runs earlier in
  the pipeline and `ZADD NX` makes the supersede script's write a no-op — so the successor
  silently gets its save-clock instead of the requested instant (1.7 ms skew observed; with a
  deliberately backdated `at`, an arbitrarily large silent bitemporal error). The working
  route is to pass the instant as the **field value at construction**
  (`JournalEntry(validity=t)`), because `ValidityField.on_save` uses `field_value` as
  `valid_from`. Verified exact: requested `1786954203.198366`, stored `1786954203.198366`.
- **Confidence**: high
- **Impact on plan**: the annotate-and-close operation is one recipe helper that calls
  `ValidityField.execute_supersede` directly and **never routes through
  `SupersessionProtocol`** for the write path (traversal reads are fine). Valid-time is set
  at construction. Both findings are filed as **#588** so M4/M5/M6 do not rediscover them,
  and both get regression tests in M1's suite.

### spike-3: Which field type gives "all annotations targeting entry X" in one query?

- **Assumption**: "A `Relationship` is needed for entry-to-entry pointers."
- **Method**: prototype + code-read
- **Finding**: **False — a plain indexed scalar is sufficient and cheapest.** `target` is a
  legal field name (confirmed; reserved names are only `limit`, `order_by`, `values`). All
  three candidates resolve in 2 Redis round trips (index Set ∩ class Set; hydration is a
  separate pipeline, so it is O(1) index reads regardless of result size):

  | Declaration | Query | Cmds on save | Multi-target? |
  |---|---|---|---|
  | indexed scalar holding the target redis key | `filter(target="JournalEntry:...")` | 2 | no |
  | `TagField` | `filter(subjects__contains=...)` | 3 | yes |
  | `Relationship(model=...)` | `filter(target=instance)` | 4 | no |

  Composition with validity also works in one call:
  `filter(target=..., validity__current=True)` (6 round trips). All three support
  `pipeline=`.
- **Confidence**: high
- **Impact on plan**: `target` is an `IndexedField(type=str, null=True)` holding the target's
  redis key. `Relationship` buys nothing — the journal target is already addressed by redis
  key, and `Relationship`'s lazy-load machinery plus its 4-command save are pure cost.
  Annotations are 1:1 with a target by design (a correction spanning several claims is N
  annotation entries, which keeps `kind`/`target` semantics unambiguous and keeps M6's
  resolution logic a simple per-target lookup).

## Data Flow

1. **Entry point** — a caller (today: a test or an application; after M3, the auditable
   extractor) calls `ProvenanceJournal.append(...)` with speaker, turn id, verbatim span,
   statement, subjects, stated-vs-inferred, and optionally an explicit valid-time instant.
2. **Pre-flight validation (D7)** — before any Redis write is issued or queued, `append()`
   and every annotation op validate: content clears the firewall (an explicit
   `scan_never_record()` call, which also covers `subjects`, see D8), `kind` is legal, the
   `kind`/`target` combination is consistent, the target exists, the requested instant is
   not before the target's stored `valid_from`, and any caller-supplied pipeline is
   transactional. Every one of these raises before a single command is issued or queued.
3. **Append-only guard** — `AppendOnlyMixin.save()` runs *before* `super().save()` because it
   is first in the MRO, so the `EXISTS` check precedes the firewall gate. The ordering is
   MRO-determined and harmless: `AppendOnlyViolation` messages carry only the redis key,
   never content, and blocked content is still never written.
4. **Privacy firewall** — `JournalEntry` composes `NeverRecordMixin`, so `Model.save()` step
   0 (`base.py:1232-1244`) re-scans every non-key **string** field, including `verbatim` and
   `statement`, as defence in depth behind step 2. A blocked entry writes a content-free
   tombstone and never reaches Redis.
5. **Persistence** — `Model.save()` writes the entry hash, the class Set, the
   `IndexedField` Sets for `turn_id`/`speaker`/`kind`/`target`, the `TagField` Sets for
   `subjects`, and `ValidityField.on_save` opens the entry's interval
   (`valid_from`, `invalid_at=+inf`, `ingested_at`) via `SUPERSEDE_LUA` in `mode="open"`.
6. **Annotation** — `confirm(entry, ...)` appends a `kind="confirm"` entry with
   `target=<entry redis key>` and stops there; membership is unaffected.
   `supersede(entry, ...)` / `retract(entry, ...)` run step 2's pre-flight, then open one
   transactional pipeline, queue the annotation's `save(pipeline=pipe)`, queue
   `ValidityField.execute_supersede(mode="invalidate", old_member=<target>,
   new_member=<annotation>, close_at=<instant>, pipeline=pipe)`, and `execute()` — one
   MULTI/EXEC — then return a typed `AnnotationResult` (D3).
7. **Notification** — `EventStreamMixin` `XADD`s the mutation onto the single unpartitioned
   key `stream:journal` inside the same pipeline, so a downstream `StreamConsumer` (the M5
   reconciler's wake-up channel) sees the entry, and the record and the stream entry commit
   together. The stream is deliberately **not** partitioned by `agent_id`: `StreamConsumer`
   takes exactly one `stream_key` (`streams/consumer.py:75-87`) with no partition-discovery
   mechanism, so a partitioned stream would ship a channel the named consumer cannot read.
   `agent_id` rides in `_stream_metadata_fields` instead, so consumers can filter without
   hydrating.
8. **Output** — `annotations_for(entry)` is one `filter(target=<redis key>)`;
   `chain(entry)` walks V0's `chain:fwd`/`chain:rev` hashes for display/replay;
   `JournalEntry.query.filter(validity__current=True)` is the live membership view (M6's
   input) and excludes superseded and retracted targets with no chain walk.

## Architectural Impact

- **New dependencies**: none external. Internal: hard dependency on `ValidityField` /
  `SUPERSEDE_LUA` (#580) and on `NeverRecordMixin` (#561); soft dependency on `TagField`,
  `IndexedField`, `EventStreamMixin`, `StreamConsumer`.
- **Interface changes**: purely additive. One new field-layer mixin
  (`AppendOnlyMixin` + `AppendOnlyViolation`), one new recipe module (`JournalEntry`,
  `ProvenanceJournal`), new `Defaults` entries, new exports. **No existing signature
  changes; `models/base.py` is not modified** — the issue's constraint ("enforcement must
  live in the model/recipe layer") is met by overriding `save()`/`delete()` rather than
  adding a fifth `isinstance` gate to `save()`.
- **Coupling**: increases coupling from the recipes layer to `ValidityField` — deliberately,
  per the epic's "one supersession mechanism" rule. M1 calls `execute_supersede` and never
  issues its own `ZADD`/`HSET` against validity keys, mirroring the constraint
  `supersession.py:27-29` places on itself.
- **Data ownership**: the journal becomes the owner of the valid-time axis (`valid_from`),
  which the epic's build-order comment names as new to the program. Entry hashes are owned by
  M1 and are **never mutated**; the validity zsets and chain hashes remain owned by V0 as
  derived index state.
- **Substrate, not sidecar** — stated explicitly because a plan critic correctly noted the
  field list has no pointer back to a `DefaultMemory` row, and whether that is a gap depends
  entirely on this answer. Issue #560's Desired outcome is that "the working belief set
  becomes a view computed at read time (module M6)," and the epic's build-order comment puts
  membership in the validity indexes. **The journal is the memory substrate**: M3 (#562)
  writes entries, M6 (#565) computes the belief sheet over `validity__current`, and there is
  no second row type for an entry to point at. M1 therefore ships **no** `record_key` /
  `source_record` field, deliberately. The `[ORDERED]` backfill No-Go is a one-way migration
  of legacy `DefaultMemory` rows into the journal after M6 lands a reader — not evidence of a
  sidecar design.
- **Reversibility**: **high before data exists, low after.** Reverting the code is deleting
  two modules, their exports, their `Defaults` entries, and their keys; no existing model or
  query path changes behavior. But the D1 one-model decision is only cheaply reversible while
  the keyspace is empty: validity and chain keys are namespaced per model
  (`$ValidityF:{Model}:{field}:...`, `validity_field.py:589`) and every record's redis key
  embeds the class name, so a later split into `JournalEntry` + `Annotation` fragments the
  zsets and both chain hashes across two keyspaces and requires rewriting immutable records
  under new keys — a data migration of a store that by contract cannot be mutated or deleted.
  Mitigation, adopted as a stated contract rather than left to chance: **`ProvenanceJournal`
  is the only documented read and write API**, so a future split stays behind the façade.
- **Transfer/export**: `AppendOnlyMixin` declares `roundtrip_policy = "rebuild"`, and
  `on_conflict="overwrite"` is unsupported on append-only models (`transfer/import_.py:215`
  calls `instance.save()`, so collisions raise and are classified `ERRORED`). `"skip"` is the
  supported conflict mode.

## Appetite

**Size:** Large

**Team:** Solo dev, PM, code reviewer

**Interactions:**
- PM check-ins: 1-2 (the annotation-model question and the kill-switch scope question below)
- Review rounds: 2+ (this is the wave keystone; four downstream modules bind to its API)

## Prerequisites

All checks below are written against the worktree venv interpreter, `.venv/bin/python`. Bare
`python` in this worktree is the system interpreter and has neither `popoto` nor
`sentence_transformers` installed — running these with bare `python` produces a false
failure.

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -n 15 ping` | Test suite and index writes |
| Editable install resolves to this checkout | `.venv/bin/python -c "import popoto,os,sys; sys.exit(0 if os.path.realpath(popoto.__file__).startswith(os.path.realpath('src')) else 1)"` | Prevents testing another tree (CLAUDE.md gate 1) |
| Full extras installed | `.venv/bin/python -c "import numpy, sentence_transformers, mcp"` | Prevents ~95 silently deselected tests (CLAUDE.md gate 2) |
| V0 on main | `.venv/bin/python -c "from popoto import ValidityField, SupersessionProtocol"` | Hard dependency (#580) |
| M2 on main | `.venv/bin/python -c "from popoto import NeverRecordMixin"` | Hard dependency (#561) |

## Solution

### Key Elements

- **`AppendOnlyMixin` + `AppendOnlyViolation`** (`src/popoto/fields/append_only.py`) — a
  generic model mixin that refuses to overwrite an existing record and refuses deletion. It
  is not journal-specific; any model that wants write-once semantics composes it. Ships with
  one explicit, greppable escape hatch for retention and test teardown.
- **`JournalEntry`** (`src/popoto/recipes/provenance_journal.py`) — the append-only entry
  model. One record type. Every capture and every annotation is a `JournalEntry`; annotations
  are distinguished by `kind` and `target`.
- **`ProvenanceJournal`** (same module) — a thin stateless façade with `append`, `confirm`,
  `supersede`, `retract`, `annotations_for`, and `chain`. It owns the one-transaction
  annotate-and-close sequence so callers never have to know about `execute_supersede`'s
  KEYS/ARGV contract or the #588 traps.
- **`Defaults` entries** — pinned tuning/policy constants plus one deploy-level kill switch
  scoped to the validity coupling (not to the append-only invariant).

### Flow

`Turn arrives` → **`ProvenanceJournal.append(speaker, turn_id, verbatim, statement,
subjects, stated)`** → *entry persisted, interval open, stream notified* → `Agent later
learns the fact is wrong` → **`ProvenanceJournal.supersede(entry, statement="...", at=t)`** →
*one MULTI/EXEC: new entry appended + target's `invalid_at` closed at `t` + chain links
written* → `Read time` → **`JournalEntry.query.filter(validity__current=True)`** → *belief
set, superseded entry absent, original entry still fully readable by key and still returned
by `filter(validity__as_of=<t-1>)`*

### Technical Approach

**D1 — One record type, not two.** The issue leaves this open ("one record type where
annotations are entries with kind+target vs separate entry/annotation models"). **Decision:
one model.** Rationale: (a) annotations must themselves be annotatable — a retraction of a
mistaken retraction, a confirmation of a supersession — which a separate annotation model
turns into a second self-referential pointer type; (b) V0's chain hashes are keyed by redis
key and are type-agnostic, so a single model keeps `chain()` a straight walk; (c) M6's
belief-sheet view (`#565`) reads `validity__current` over one keyspace, and two models would
force it to union two queries and reconcile two membership sets. Cost of the decision, stated
honestly: `kind`/`target` consistency is a runtime validation rather than a type-system
guarantee, so the plan pays for it with explicit validation in `pre_save` (an `assert`-kind
entry must have `target=None`; a `confirm`/`supersede`/`retract` entry must have a `target`
naming an existing `JournalEntry`) and a test per invalid combination.

**D2 — Enforcement by `save()`/`delete()` override, keyed on `EXISTS`.** Per spike-1.
`models/base.py` is not touched. The guard reads `self.db_key.redis_key` and refuses if the
key exists. It always reads `POPOTO_REDIS_DB` directly, **never** a caller-supplied pipeline —
an `EXISTS` queued on a pipeline returns a `Pipeline` object, which is always truthy and would
block every save. `delete()` unconditionally raises.

Two additional refusals the `EXISTS` check cannot express, both mandatory:

- **`save(migrate_key=True)` always raises**, regardless of `EXISTS`. `agent_id` is a
  `KeyField`; mutating it and calling `save(migrate_key=True)` makes `EXISTS` on the *new*
  key return 0, the guard pass, and `base.py:1517`/`:1623` `DELETE` the old key — destroying
  an entry through a supported public kwarg. The guard also refuses whenever
  `self.obsolete_redis_key` is set.
- **`on_conflict="overwrite"` is unsupported on append-only models.** `transfer/import_.py:215`
  calls `instance.save(...)`, so every colliding record raises `AppendOnlyViolation` and is
  classified `ERRORED`. Documented; `"skip"` is the supported mode.

`AppendOnlyMixin` declares `roundtrip_policy = "rebuild"` (it owns no Redis state of its
own). This is not optional: the mixin lives in `src/popoto/fields/`, is named `*Mixin`, and
touches `POPOTO_REDIS_DB`, which is exactly the predicate
`tests/test_transfer_roundtrip.py:656-696` uses to collect stateful model-level mixins, and
`test_every_model_level_mixin_has_a_policy_declared` (`:752-767`) requires the attribute in
the class's own `__dict__`.

The escape hatch is a named classmethod — `AppendOnlyMixin.hard_delete(instance)` —
documented as retention/admin-only and greppable, rather than a `skip_append_only=True`
kwarg that would require modifying `base.py:1114`'s signature. `hard_delete` **must sweep
derived state**, not just the record hash: the validity zsets, both chain hashes, the
`$TagF:`/`$IndexedF:` index keys, and the class Set. A `hard_delete` that leaves index and
chain state behind is worse than no escape hatch, because it produces exactly the orphan
shape Race 1 describes.

**D3 — Valid-time is set at construction; the write path calls `execute_supersede`
directly; every annotation op returns a typed result.** Per spike-2 and #588.
`ProvenanceJournal` constructs the annotation with `validity=<instant>` so
`ValidityField.on_save` writes the intended `valid_from`, then queues `save(pipeline=pipe)`
and `ValidityField.execute_supersede(..., mode="invalidate", old_member=<target key>,
new_member=<annotation key>, close_at=<instant>, pipeline=pipe)` into the same transactional
pipeline. `SupersessionProtocol` is used only for **read-side** traversal (`superseded_by`,
`supersedes`, `chain`), never on the write path.

Every mutating op returns `AnnotationResult(entry, target_closed: bool, coupling_enabled:
bool)`. This exists specifically so the kill switch cannot reproduce #588's failure shape: a
caller must be able to distinguish "target closed" from "target not closed" **without reading
Redis**. The first uncoupled `supersede`/`retract` in a process also emits a warn-once
`logger.warning`.

**Atomicity, stated precisely.** This deviates from the #560 amendment's literal wording
("in the same script") in favor of "in the same MULTI/EXEC transaction," because forking
`SUPERSEDE_LUA` would violate the epic's "one supersession mechanism" rule. The property
that holds, and that the amendment actually asks for, is: **no interleaving reader observes
the annotation without the close.** The property that does **not** hold, and that this plan
does not claim, is rollback — Redis `MULTI/EXEC` does not roll back sibling commands when one
command errors at execute time, which `base.py:1563-1571` documents verbatim as the rationale
for #476's eager EVALs. A command-level error inside `EXEC` can therefore leave the
annotation appended with the target still open. D7's pre-flight exists to make that window
unreachable in practice, and the residual is a documented, tested boundary rather than an
impossibility claim.

**D4 — Field list.**

| Field | Type | Why |
|---|---|---|
| `entry_id` | `AutoKeyField()` | Immutable UUID identity; makes the TOCTOU window in D2 practically unreachable for appends. `AutoKeyField` assigns at `__init__`, before save, so `db_key.redis_key` is concrete pre-save and D2's guard reads the right key |
| `agent_id` | `KeyField()` | Partition key, mirroring `DefaultMemory:117`; keeps multi-agent journals separable. Required non-null by `append()`: a `None` value renders the literal `"None"` into the record key |
| `captured_at` | `FloatField()` | Wall-clock capture time of the source turn. **Deliberately not named `ingested_at`**: `ValidityField.on_save` hardcodes `ingested_at=now` (the save clock) into `$ValidityF:...:ingested_at` and ignores any model field (`validity_field.py:882-896`), so two fields under one name would silently disagree and M5/M6 would get different answers depending on which they read. The validity ingest axis is always the save clock; this field is the source turn's clock |
| `turn_id` | `IndexedField(type=str, null=True)` | "All entries from turn T" in one query |
| `speaker` | `IndexedField(type=str, null=True)` | "All entries attributed to S" in one query |
| `verbatim` | `StringField(default="")` | The exact source span. Privacy-sensitive — the reason `NeverRecordMixin` is mandatory |
| `statement` | `StringField(default="")` | The atomic natural-language claim |
| `subjects` | `TagField(null=True)` | Multi-value; one entry can concern several people/topics |
| `stated` | `BooleanField(default=True)` | Stated (True) vs inferred (False); feeds M5's conflict precedence |
| `kind` | `IndexedField(type=str, ...)` | `assert`/`confirm`/`supersede`/`retract`, validated against a frozen tuple; indexed so "all retractions" is one query |
| `target` | `IndexedField(type=str, null=True)` | Target entry's redis key. Per spike-3, gives the AC's one-query reverse lookup |
| `validity` | `ValidityField()` | `valid_from` / `invalid_at` / `ingested_at` axes per the #560 amendment |

Composition: `class JournalEntry(AppendOnlyMixin, NeverRecordMixin, EventStreamMixin, Model)`,
with `_stream_name = "journal"`, **no** `_stream_partition_field` (see Data Flow step 7 — a
partitioned stream is unreadable by `StreamConsumer`'s single-key API), and
`_stream_metadata_fields = ("agent_id", "kind", "target")` so the M5 reconciler can filter
without hydrating. `_stream_max_length` is set directly on the class body as a pinned
constant with a rationale comment — `EventStreamMixin` already exposes it as a per-model class
attribute (`event_stream.py:82`), so routing it through `Defaults` would add a sync-test
exemption and buy nothing.

**Note on `filter(validity=...)`:** `validity` is the correct field name (the query params
`validity__current` / `validity__as_of` are derived from it, so there is no collision), but
`ValidityField.filter_query` handles only those two suffixes and returns an empty `set()` for
a bare exact-value filter — `filter(validity=t)` silently returns nothing. `target` and `kind`
collide with nothing; the only reserved names are `limit`, `order_by`, `values`. This is
documented on the feature page so a downstream module does not write `filter(validity=t)` and
get a silent empty result.

**No `EmbeddingField`.** Deliberate, mirroring `default_memory.py:62-72`: it pulls an
optional extra, and similarity lookup over the journal is not on M1's acceptance list. The
module documents the subclass-with-an-`EmbeddingField` recipe instead, so the extension point
is explicit rather than a gap.

**D5 — Constants and kill switch.** New `Defaults` entries, following the
`{PRIMITIVE}_{WHAT}` convention with sweep-provenance comments and a section header:

- `JOURNAL_VALIDITY_COUPLING_ENABLED = _read_journal_coupling_switch()` — **deploy-level kill
  switch, env-backed.** Reads `POPOTO_JOURNAL_COUPLING_DISABLE`, phrased as a *disable* so
  default-on holds when unset, exactly matching `_read_never_record_switch`
  (`constants.py:32-39`) and `DATETIME_KEY_LEGACY`. A plain class attribute would not qualify
  as deploy-level: PyPI adopters who cannot edit model code could not flip it, which is the
  specific failure `feedback_default_on_design` calls out. When disabled,
  `supersede`/`retract` still append their annotation entries and still write `target`, but
  do not close the target's interval; membership degrades to "everything ever appended,"
  which is pre-M1 behavior. The degraded mode is **observable**: `AnnotationResult.target_closed`
  is `False` and `coupling_enabled` is `False` (D3). Boolean, not swept.
- `JOURNAL_KINDS` — the core kind vocabulary, a frozen tuple of the four the issue names. Not
  a tunable; changing it reclassifies stored entries. **It ships with an extension seam**,
  because M5 (#564) will want merge/equivalence kinds, M7 (#566) a queue-able kind, and M8
  (#567) an exposure kind, and none of them should have to edit a core constant declared
  frozen: a `JournalEntry` subclass may set `_journal_kinds`, validated at class definition
  as a **superset** of the core four. The reader rule is stated as a contract: an entry whose
  `kind` a reader does not recognize is **inert for membership** — never silently treated as
  `supersede` or `retract`.

Registration note (verified against the suite, and the reverse of what an earlier draft of
this plan said): kill switches are **exempted** in `tests/benchmarks/test_defaults_sync.py`'s
`field_kwargs_and_class_attrs` set, **not** registered in `tests/benchmarks/overrides.py`'s
`MODULE_CONSTANTS`. Every comparable switch — `VALIDITY_GATING_ENABLED`,
`NEVER_RECORD_ENABLED`, `TAG_SCOPING_ENABLED`, `DATETIME_KEY_LEGACY` — is handled that way,
with an in-file comment stating that an import-time alias "would defeat the whole point of a
runtime-flippable deploy switch." Registering them in `MODULE_CONSTANTS` would make
`test_module_alias_matches_defaults` fail with `AttributeError`, since `getattr(mod, attr)`
would look for a module-level alias that must not exist.

**There is deliberately no kill switch on the append-only invariant.** It is the model's
defining contract, not a capability: a deployment that disables it silently converts the
provenance journal into a mutable table while every downstream module still assumes
immutability. This is the one place this plan departs from the repo's "every capability gets
a deploy-level kill switch" default, and it is flagged as an open question rather than
assumed.

**D6 — Test teardown needs almost nothing.** An earlier draft of this plan claimed the suite
needed a bespoke derived-key wipe because `delete_all()` raises. That premise is false:
`popoto.pytest_plugin` installs an **autouse, function-scoped** `_popoto_flush_db` fixture
that calls `flushdb()` before every test (`src/popoto/pytest_plugin.py:234-250`). Isolation is
already total. The suite therefore needs only a `Defaults` restore fixture (to undo kill-switch
flips), matching the tail of `tests/test_validity_field.py`'s `clean_state`. The `hard_delete`
seam is justified on **retention** grounds alone (Risk 4b), not on test-teardown grounds.

**D7 — Pre-flight validation, before any command is issued or queued.** This is the plan's
answer to the failure shape all three plan critics independently flagged: `Model.save()`
returns `pipeline if pipeline else False` when the never-record gate fires
(`base.py:1240-1244`) — **nothing is queued, and in pipeline mode the return value is
indistinguishable from success.** A naive `supersede()` would then queue the invalidate EVAL
anyway and commit, closing the target's interval against an annotation that was never
written: a membership change with zero provenance, in the one module whose entire purpose is
provenance. Every annotation op therefore validates, in order, *before opening the pipeline*:

1. `scan_never_record()` over the annotation's content — including `subjects` (D8). Blocked
   content raises; nothing is queued.
2. `kind` ∈ the model's kind vocabulary, and the `kind`/`target` combination is consistent
   (an `assert` entry has no `target`; a `confirm`/`supersede`/`retract` entry has one).
3. The target exists (`EXISTS` on its redis key) and, if `agent_id` scoping is enforced,
   belongs to the same agent — cross-agent targets are **rejected**, since `target` is a full
   redis key that can name another agent's partition.
4. The requested instant is not before the **target's stored `valid_from`**, read via
   `ValidityField.get_interval_keys(...)`. This check must be done here and cannot be
   delegated: `execute_supersede` only remaps `CLOSE_BEFORE_START` → `ValueError` on the
   non-pipeline branch, and its client-side pre-check at `validity_field.py:764-772` compares
   `close_at` against the *caller-supplied* `valid_from` (which D3 sets to the same instant),
   so it never fires. Without this pre-read, a genuine backdate surfaces as a raw
   `redis.exceptions.ResponseError` from `pipe.execute()` with the annotation already written.
5. Any caller-supplied pipeline has `transaction is True`. `POPOTO_REDIS_DB.pipeline(transaction=False)`
   is legal and would silently void the atomicity guarantee; M1 raises `ValueError` instead.

**D8 — `subjects` is outside the firewall's scan surface, and M1 closes that gap itself.**
`NeverRecordMixin._never_record_scan_values` yields only `isinstance(value, str)` values
(`never_record.py:624-629`); a `TagField` value is a **list**, so `subjects` is never scanned
by the mixin. Subject tags are populated from human speech by M4 (#563) and are a plausible
carrier of names and identifiers. M1 does not leave this to the mixin: `append()` calls
`scan_never_record()` on each subject tag explicitly as part of D7 step 1. The residual mixin
hole is stated on the feature page and pinned by a test asserting current behavior, so it
cannot drift silently. Related: `target` survives the entropy detector only because a redis
key contains `:`, which is outside `_ENTROPY_CHARSET` (`never_record.py:253`) — a regression
test asserts a `target`-carrying annotation is never firewall-blocked, so a future
single-segment key rendering cannot silently start dropping annotations.

## Failure Path Test Strategy

### Exception Handling Coverage

- `EventStreamMixin._xadd_mutation` swallows exceptions in non-pipeline mode
  (`event_stream.py:196-200`) and re-raises in pipeline mode (`:193-196`). M1's annotation
  path is always pipelined, so a stream failure must abort the whole annotation. **Test:**
  fault-inject an `XADD` failure inside the annotation pipeline and assert the exception
  propagates AND the target's interval was not closed.
- `NeverRecordMixin.write_tombstone` wraps its audit write in `try/except: pass`
  (`never_record.py:501`) so audit failure never fails the firewall open. M1 adds no handler
  of its own here. **Test:** with the tombstone-write path fault-injected, a blocked
  `append()` still raises `JournalBlockedError` and persists nothing.
- **The `append()` / annotation return contract on a privacy block is explicit, not a
  "sentinel."** An earlier draft left this undefined, which is untenable for an API four
  modules bind to — and the two save modes genuinely differ (`base.py:1244` returns `False`
  without a pipeline and the *pipeline itself* with one, the latter indistinguishable from
  success). M1 does not expose that ambiguity: D7 scans before any save, and blocked content
  **raises `JournalBlockedError`** in both modes. **Test:** both modes raise; nothing is
  persisted; the exception message carries the reason and detector only, never the matched
  content (`never_record.py:646-652` discipline).
- **A firewall-blocked annotation must close nothing.** This is the highest-severity failure
  path in the plan (D7). **Test:** `supersede()` with never-record-triggering content raises,
  the target remains in `validity__current`, `chain_fwd` has no entry for it, and
  `annotations_for(target)` is empty.
- `ValidityField.execute_supersede` remaps `CLOSE_BEFORE_START` to `ValueError` **only on the
  non-pipeline branch** (`validity_field.py:796-807`), and its client-side pre-check
  (`:764-772`) compares `close_at` to the caller-supplied `valid_from`, which D3 sets to the
  same instant — so it never fires. Uncorrected, a backdated close surfaces as a raw
  `redis.exceptions.ResponseError` from `pipe.execute()` with the annotation already written.
  **M1 pre-reads the target's stored `valid_from` (D7 step 4) and raises `ValueError` before
  queuing anything.** **Test:** `supersede(entry, at=<before the target's valid_from>)` raises
  `ValueError`, no command is issued (asserted by call counter), and both intervals are
  untouched. A second test pins the underlying V0 behavior — bypassing the pre-flight raises
  `redis.exceptions.ResponseError` from `execute()` with the annotation appended — so the
  reason the pre-flight exists cannot be refactored away.
- **A non-transactional caller pipeline raises.** `pipeline(transaction=False)` is legal and
  would silently void atomicity (D7 step 5). **Test:** passing one raises `ValueError` before
  any queueing.
- M1 introduces **no bare `except Exception: pass`** of its own. Verified as an anti-criterion
  row in Verification.

### Empty/Invalid Input Handling

- `append()` with an empty/whitespace-only `statement` **and** empty `verbatim` — documented
  behavior: raises `ValueError` (an entry with no content is not a provenance record).
  Tested.
- `append(subjects=[])` / `subjects=None` — allowed; entry is in the shared pool, mirroring
  `TagField`'s zero-tag semantics. Tested.
- `confirm`/`supersede`/`retract` with an unsaved or nonexistent target — raises
  `ValueError` **before** any write, mirroring `supersession.py`'s "resolve keys before any
  mutation" discipline (`_member_key:376`). Tested for both shapes (unsaved instance, and a
  redis key that does not exist).
- `kind` outside `JOURNAL_KINDS` — raises `ValueError` in `pre_save`. Tested per invalid
  value.
- `append(..., at=<non-numeric>)` — `ValidityField` raises; asserted.

### Error State Rendering

The journal has no user-visible rendering surface. Its "user" is a downstream module, and the
error contract is exception type + message. Every documented raise above is asserted by type
and by the invariant it protects (nothing partially written). `AppendOnlyViolation` messages
name the offending redis key and never the record content — the same side-channel discipline
`never_record.py:646-652` applies.

## Test Impact

"No existing tests are affected" would be wrong — an earlier draft claimed it. Two existing
suites have guards that a new model-level mixin and new `Defaults` entries trip by design.
The module is otherwise greenfield: it adds two new source files, touches no existing model,
changes no existing signature, and modifies no existing behavior; the only edits to existing
source files are additive.

- [ ] `tests/benchmarks/test_defaults_sync.py::test_all_defaults_covered_by_module_constants`
  — **UPDATE**: add `JOURNAL_VALIDITY_COUPLING_ENABLED` and `JOURNAL_KINDS` to the
  `field_kwargs_and_class_attrs` exemption set **inside this test file** (`:110-135`), with a
  rationale comment matching the `VALIDITY_GATING_ENABLED` / `NEVER_RECORD_ENABLED`
  precedent.
- [ ] `tests/benchmarks/overrides.py` (`MODULE_CONSTANTS`) — **NO CHANGE.** Registering the
  kill switch here would create a required module-level alias that must not exist and would
  make `test_module_alias_matches_defaults` fail with `AttributeError` (`:28-36`). An earlier
  draft of this plan asserted the opposite; it was verified wrong against the suite.
- [ ] `tests/test_transfer_roundtrip.py::test_every_model_level_mixin_has_a_policy_declared`
  — **UPDATE (by source change, not test change)**: `AppendOnlyMixin` lives in
  `src/popoto/fields/`, is named `*Mixin`, and references `POPOTO_REDIS_DB`, which is exactly
  the collector predicate at `:656-696`. It must declare `roundtrip_policy = "rebuild"` in
  its own `__dict__` (`:752-767`). No edit to the test file itself.
- [ ] `tests/test_provenance_journal.py` — **CREATE**: the behavioral suite (one test file per
  primitive, per the issue's constraints).
- [ ] `tests/benchmarks/` — **CREATE**: the 20k append p50/p99 benchmark lives here behind a
  marker, not in the unit-test file. With `flushdb` running before every test, a 20k-entry
  benchmark inside the default suite would add minutes to every run.
- [ ] `tests/test_provenance_journal.py` — **CREATE**: the new suite (one test file per
  primitive, per the issue's constraints).

## Rabbit Holes

- **Reimplementing supersession in M1's own Lua.** Tempting because the #560 amendment says
  "in the same script," and because a bespoke script could append and close in one EVAL. It
  would fork `SUPERSEDE_LUA`, violate the epic's "one supersession mechanism" rule, and
  create two writers of the same zsets. A transactional pipeline gets the same atomicity.
  Avoid.
- **Making the append-only guard bulletproof against direct Redis access.** The guard is an
  ORM-layer contract; a caller with a raw client can always `HSET` over an entry. Chasing a
  storage-level guarantee means `HSETNX`-per-field or a Lua write path that duplicates
  `Model.save()`'s index handling. Document the boundary; do not chase it.
- **Building the belief-sheet view.** `filter(validity__current=True)` already returns the
  live set. Ranking, deduplication, conflict precedence, and assembly are **M6 (#565)** and
  **M5 (#564)**. M1 stops at "membership is correct and one query away."
- **A `subjects` taxonomy.** `TagField` is explicitly convention-over-schema and explicitly
  not a security boundary (`tag_field.py:22-31`). Designing a subject ontology, normalizing
  person names, or resolving `he`/`she`/`they` to subjects is **M4 (#563)**. M1 stores what
  it is given.
- **Backfilling `DefaultMemory` into the journal.** A migration is a real piece of work with
  its own data-safety profile and no consumer until M6 lands. Not in this plan.
- **Wiring `SubconsciousMemory.extract_memories()` to write journal entries.** That is
  **M3 (#562)**, which is ship-blocked on M2 and depends on M1's API. Doing it here would
  couple M1's merge to the extractor's behavior change and make this PR unreviewable.

## Risks

### Risk 1: The one-transaction annotate-and-close depends on undocumented V0 internals

**Impact:** `ProvenanceJournal` calls `ValidityField.execute_supersede` with an explicit
seven-argument contract. If V0 changes that signature or the KEYS/ARGV order, M1 breaks
silently in the same way #588 describes — a no-op that looks like success.
**Mitigation:** M1 asserts the contract structurally, but by **shape, not by count**. An
earlier draft said "4 queued commands for a supersede," a number taken from spike-2's toy
`KeyField + ValidityField` model. Real `JournalEntry` queues the HSET, the class SADD, four
`IndexedField` EVALs, the `TagField` commands, the validity `open` EVAL, the XADD, and the
invalidate EVAL — roughly ten, and brittle to any field-set change. The assertions are
therefore: exactly one queued EVAL with `ARGV[5] == "invalidate"` and both member arguments
non-empty; exactly one with `ARGV[5] == "open"`; `numkeys == 6` on the supersede EVAL;
`pipeline.transaction is True`; and zero mutating calls issued outside the pipeline. This
mirrors `tests/test_validity_field.py:598-655`'s `_CallCounter` pattern rather than inventing
a new one. Plus the regression tests for both #588 findings.

### Risk 2: The `EXISTS` guard adds a round trip to every append

**Impact:** The journal is the write-hot path for the whole wave — every extracted fact
becomes an entry. One extra RTT per append is a real cost at ingest volume — and it is not
the only one: D4's four `IndexedField`s plus the `TagField` are five inherited eager-EVAL
sites per non-pipeline append (Race 1), so the honest per-append overhead is the `EXISTS` RTT
*plus* five eager EVALs, not the single RTT an earlier draft attributed to the guard alone.
**Mitigation:** Measure it. The plan includes a p50/p99 append micro-benchmark at the epic's
20k-record scale target, reported with the environment, comparing `JournalEntry.save()`
against an identical model without `AppendOnlyMixin`, and separately reporting the eager-EVAL
count per append.

**The fallback an earlier draft proposed is withdrawn as unsafe.** "Skip the `EXISTS` when
the key came from `AutoKeyField`" would disable the guard for spike-1 matrix rows 2 (same
instance re-saved) and 4 (`query.get(...)` then `.save()`) — both have AutoKeyField-derived
keys, and they are the only shapes the guard catches in practice. If overhead proves
material, the correct direction is to fold the existence check **into** the write
(`HSETNX` on a sentinel field, or a `SETNX` claim key queued in the same pipeline), which
also closes Race 2. Never to skip the check.

### Risk 3: One record type makes `kind`/`target` consistency a runtime concern

**Impact:** Nothing at the type level stops an `assert` entry from carrying a `target`, or a
`supersede` entry from targeting a nonexistent key. Downstream M6 resolution would then see
malformed provenance.
**Mitigation:** `pre_save` validation with a test per invalid combination (D1), and target
existence checked before any write. Accepted as the stated cost of D1 rather than papered
over.

### Risk 4a: Journal growth is unbounded by design

**Impact:** Append-only plus never-delete means the keyspace only grows. At the epic's 20k
target this is fine; a long-lived production agent is a different curve.
**Mitigation:** Out of scope to solve, in scope to measure and document. The docs page states
the growth characteristic explicitly (bytes per entry × append rate). A retention *policy* is
downstream work with its own data-safety review.

### Risk 4b: Append-only × firewall-disabled × no erasure path = un-erasable secrets

**Impact:** Qualitatively different from growth, and split out for that reason.
`POPOTO_NEVER_RECORD_DISABLE=1` is a supported, documented deployment action. With the
firewall off, verbatim human speech — including credentials — lands in a keyspace whose only
removal path is an admin classmethod. "Documented, not solved" is the right answer for growth
and the wrong answer for erasure.
**Mitigation:** In scope for M1, not deferred. `hard_delete` ships with a complete
derived-state sweep (validity zsets, both chain hashes, `$TagF:`/`$IndexedF:` keys, class
Set) — per D2, a `hard_delete` that leaves index or chain state behind is worse than none —
and a Success Criterion asserts that after `hard_delete`, `filter()` returns nothing and no
`$*` key retains the record's redis key.

## Race Conditions

### Race 1: Indexed-field EVALs run eagerly, ahead of the internal pipeline

**Location:** `src/popoto/models/base.py:1364-1379` and `:1562-1583`
**Trigger:** A no-external-pipeline `save()` of a `JournalEntry`. `IndexedFieldMixin` fields
(`turn_id`, `speaker`, `kind`, `target`) and `TagField` (`subjects`) run their own EVAL
eagerly with `pipeline=None`, before the internal pipeline carrying the hash write and the
`ValidityField` open commits. A crash between the two leaves index entries pointing at a
nonexistent hash.
**Data prerequisite:** The entry hash must exist before any reader resolves an index hit.
**State prerequisite:** This is the exact hazard #476 traded deliberately (eager EVAL closes
the unique-conflict window at the cost of this ordering).
**Mitigation:** The *mechanism* is inherited and not re-solved; the *exposure* is created by
D4's field choice. Stated accurately: M1 accepts **five** inherited eager-EVAL sites per
non-pipeline append (four `IndexedField`s plus the `TagField`) on the wave's write-hot path,
and adds no `UniqueField`. That count feeds Risk 2's benchmark. The plan asserts the resulting orphan-index behavior is *readable*: an index
hit whose hash is missing must be skipped by the query layer, not raise. Tested by
constructing the orphan state directly (index write without the hash) and asserting
`filter(target=...)` returns an empty set rather than erroring.

### Race 2: TOCTOU in the append-only guard

**Location:** `src/popoto/fields/append_only.py` (new), the `EXISTS`-then-`save` sequence
**Trigger:** Two processes save the same redis key concurrently; both `EXISTS` calls return 0
before either `HSET` lands, so both saves proceed and the second silently overwrites the
first — the exact violation the guard exists to prevent.
**Data prerequisite:** none.
**State prerequisite:** Two writers producing the same key.
**Second, more reachable shape (no concurrency required):** two appends of the same key
queued into **one** pipeline. The guard's `EXISTS` executes immediately against
`POPOTO_REDIS_DB` and cannot see a command already queued-but-unexecuted on the pipeline, so
both pass and the second overwrites. This one *is* deterministically testable and is an
explicit row in the re-save test matrix.
**Mitigation:** Structurally narrowed rather than locked. `entry_id` is an `AutoKeyField`
(UUID), so two independent appends cannot produce the same key; the window is only reachable
when a caller supplies an explicit colliding key, which is a programming error the guard
still catches in every non-concurrent, non-pipelined case. Both limitations are documented in
the mixin docstring and on the docs page as known boundaries — **not** claimed as a hard
guarantee. A storage-level guarantee would need an `HSETNX`-based write path (see Rabbit
Holes and Risk 2's withdrawn-fallback note). Tested to the extent each shape is testable: the
sequential cases from spike-1's matrix and the intra-pipeline duplicate are all asserted; the
cross-process concurrent case is documented, not asserted, because a passing test would be a
race itself.

### Race 3: Annotation targets an entry that is superseded concurrently

**Location:** `ProvenanceJournal.supersede` / `retract`
**Trigger:** Two annotations close the same target at once.
**Data prerequisite:** The target's `invalid_at` score must be `+inf` for the close to apply.
**State prerequisite:** Exactly one close per target.
**Mitigation:** Already handled inside `SUPERSEDE_LUA` — its idempotency guard requires
`ZSCORE invalid_at == +inf` before closing (`validity_field.py:157-221`), so the second
annotation's close is a no-op at the script level while its entry still appends. The
resulting state is correct: two annotation entries exist (both are real provenance), one
close applied. Asserted by test — the plan does **not** add a second guard on top.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #588] Fixing `SupersessionProtocol.supersede`/`invalidate`'s silent no-op
  when the successor is created in the same pipeline, and the `ZADD NX` valid-time skew.
  M1 routes around both via D3; the fix is a V0 change with its own maintainer questions
  (should it raise instead of no-op? should `execute_supersede`'s `valid_from` win?).
- [SEPARATE-SLUG #562] Wiring `SubconsciousMemory.extract_memories()` to write journal
  entries — that is M3's auditable-extraction work, which is ship-blocked on M2 and consumes
  M1's API.
- [SEPARATE-SLUG #563] Reference resolution: normalizing speakers, resolving pronouns to
  subjects, and populating `valid_from` from natural-language time expressions. M1 is the
  home of the valid-time axis; M4 is its producer.
- [SEPARATE-SLUG #564] Reconciliation: equivalence classes, duplicate merging, and
  type-rule/precedence conflict outcomes. M1 stores; M5 decides.
- [SEPARATE-SLUG #565] The belief-sheet view. M1 guarantees `validity__current` membership is
  correct; assembling, ranking, and presenting the sheet is M6.
- [ORDERED] Backfilling existing `DefaultMemory` records into the journal. Blocked on M6
  landing a reader — a migration with no consumer is unverifiable, and the cutover is a
  human-gated deploy decision.

## Update System

No update-system changes required. This is a pure library addition: no new external
dependency, no config file, no service, no deployment topology change. Existing installations
gain two importable symbols and three `Defaults` entries; nothing changes behavior until a
caller declares a `JournalEntry`. The one deploy-relevant knob,
`JOURNAL_VALIDITY_COUPLING_ENABLED`, follows the existing `Defaults` override convention
(set before model definition or at runtime) and needs no propagation mechanism.

## Agent Integration

No agent integration required in this plan. `src/popoto/integrations/mcp_server.py` exposes
memory operations over MCP, but the journal has no caller until M3 writes to it and M6 reads
from it; exposing an unpopulated journal as an agent tool would ship a surface with nothing
behind it. The MCP surface is M6's concern, when there is a belief sheet worth asking for.
Stated here so the omission is deliberate rather than overlooked.

## Documentation

### Feature Documentation
- [ ] Create `docs/features/provenance-journal.md` — the model, the append-only contract and
  its documented boundary (Race 2), the annotation kinds, the one-transaction
  annotate-and-close sequence, the relationship to `ValidityField` (membership comes from
  validity indexes; chains are for display and replay verification only), the growth
  characteristic, and the `hard_delete` retention seam.
- [ ] Add the page to `docs/features/README.md`'s index table.
- [ ] Add a nav entry to `mkdocs.yml` under the Agent Memory primitives group, adjacent to
  `ValidityField and Supersession` (`mkdocs.yml:49`).
- [ ] Cross-link from `docs/features/validity-and-supersession.md` (the journal is V0's first
  real consumer) and from `docs/features/agent-memory.md`.

### External Documentation Site
- [ ] `mkdocs build --strict` passes (gated by `scripts/ci-local.sh`).

### Inline Documentation
- [ ] Module docstring on `provenance_journal.py` enumerating the field set and the rationale
  for each, in the style of `default_memory.py:19-83`, including the explicit
  "why no `EmbeddingField`" note and the subclass extension recipe.
- [ ] Module docstring on `append_only.py` stating the ORM-layer-contract boundary and the
  TOCTOU limitation without overclaiming.
- [ ] Docstrings on every public `ProvenanceJournal` method, each naming its raises.
- [ ] Inline comment at the `execute_supersede` call site pointing at #588, so the next
  reader does not "simplify" it back to `SupersessionProtocol`.

## Success Criteria

- [ ] `JournalEntry` and `ProvenanceJournal` exported from `popoto.recipes`;
  `AppendOnlyMixin`, `AppendOnlyViolation` and `JournalBlockedError` exported from `popoto` —
  all importable from a fresh interpreter
- [ ] Re-saving an existing entry raises `AppendOnlyViolation` for all six shapes in spike-1's
  matrix as extended: same instance re-save, colliding fresh object, `query.get()`-then-save,
  `delete()`, `delete_all()`, and **`save(migrate_key=True)`**; plus the intra-pipeline
  duplicate append (Race 2) documented and asserted
- [ ] Entries persist and round-trip `speaker`, `turn_id`, `verbatim`, **`statement`**,
  `subjects[]`, `stated`, `kind`, `target`, `captured_at`, and the validity interval
- [ ] `ProvenanceJournal.annotations_for(entry)` returns every annotation targeting it in one
  `filter()` call — asserted by wrapping `POPOTO_REDIS_DB.smembers`/`sinter` in the
  `_CallCounter` pattern and counting index reads, not by inspection
- [ ] After a `supersede` or `retract`, the target is absent from
  `filter(validity__current=True)` immediately and still returned by
  `filter(validity__as_of=<before the close>)` — **with no chain walk**, asserted by
  monkeypatching `hget`/`hgetall` on `ValidityField.get_all_keys(...)["chain_fwd"|"chain_rev"]`
  and requiring zero reads
- [ ] The annotate-and-close sequence is queued into a single `transaction=True` pipeline:
  `pipeline.transaction is True`; exactly one queued EVAL with `ARGV[5] == "invalidate"` and
  `numkeys == 6`; exactly one with `ARGV[5] == "open"`; zero mutating calls outside the
  pipeline. A fault injected **before** `execute()` applies nothing. **The documented boundary
  is asserted too**: a command-level error inside `EXEC` can leave the annotation appended
  with the target open — this is tested as a known state, not claimed impossible
- [ ] A never-record-blocked `append()` raises `JournalBlockedError` and persists nothing; a
  never-record-blocked `supersede()` raises, closes nothing, writes no chain link, and leaves
  `annotations_for(target)` empty
- [ ] `chain()` returns a 3-deep supersession chain in oldest→newest order
- [ ] Every invalid `kind`/`target` combination, every out-of-vocabulary `kind`, every
  cross-agent target, and every backdated `at` raises **before any command is issued**
  (asserted by call counter, not just by exception type)
- [ ] Regression tests for both #588 findings (pipeline no-op; valid-time skew) pass against
  M1's write path
- [ ] Valkey-safe: no Redis-module command anywhere in the new code (core commands + reuse of
  V0's Lua only) — asserted by an anti-criterion grep
- [ ] Every `ProvenanceJournal` mutating op accepts `pipeline=` and returns the pipeline
  unexecuted when given one (asserted by `POPOTO_REDIS_DB` call count == 0 plus a non-empty
  `command_stack`), and rejects a `transaction=False` pipeline with `ValueError`
- [ ] With `POPOTO_JOURNAL_COUPLING_DISABLE=1`, a `supersede()` issues exactly the command set
  of a bare `append()` and nothing more — asserted with the `_CallCounter` pattern from
  `tests/test_validity_field.py:598-655` (the invalidate EVAL is absent; EVAL count equals the
  plain-append count; the target's `invalid_at` is unchanged at `+inf`) — and the caller can
  detect the degraded mode from `AnnotationResult.target_closed is False` **without reading
  Redis**. "Byte-identical" is explicitly not claimed: `execute_supersede` puts a clock value
  into ARGV, so two runs differ by construction, and the repo has no byte-comparison precedent
- [ ] After `hard_delete`, `filter()` returns nothing and no `$*` key retains the record's
  redis key (Risk 4b)
- [ ] Append p50/p99 measured at 20k entries in `tests/benchmarks/`, reported with
  python/redis-py versions, including the per-append eager-EVAL count
- [ ] Tests pass (`/do-test`), narrow scope: `tests/test_provenance_journal.py` plus
  `tests/test_validity_field.py`, `tests/test_transfer_roundtrip.py`, and
  `tests/benchmarks/test_defaults_sync.py`
- [ ] mypy error delta is 0 vs the merge-base. **Baseline already measured for this plan:
  1119 errors in 66 files (93 source files checked) at `9180680`**, using a clean detached
  sibling worktree and this worktree's venv — Python 3.12.13, redis-py 8.1.0, mypy 2.3.1,
  Redis server 8.6.2. The branch total must be 1119; **any excess is enumerated line-by-line
  in the PR body with the redis-py version measured**, per CLAUDE.md gate 5, rather than
  quietly relaxed. Two new modules that build redis pipelines are precisely the shape that
  gate warns produces version-dependent `Awaitable[T] | T` errors
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (append-only mixin)**
  - Name: `append-only-builder`
  - Role: `src/popoto/fields/append_only.py` + its tests
  - Agent Type: builder
  - Resume: true

- **Builder (journal model + recipe)**
  - Name: `journal-builder`
  - Role: `src/popoto/recipes/provenance_journal.py`, `Defaults` entries, exports
  - Agent Type: builder
  - Resume: true

- **Test engineer (journal suite)**
  - Name: `journal-tester`
  - Role: `tests/test_provenance_journal.py`, including the #588 regressions, the
    fault-injection atomicity tests, and the append micro-benchmark
  - Agent Type: test-engineer
  - Resume: true

- **Documentarian**
  - Name: `journal-documentarian`
  - Role: `docs/features/provenance-journal.md`, index, nav, cross-links
  - Agent Type: documentarian
  - Resume: true

- **Validator**
  - Name: `journal-validator`
  - Role: verifies every Success Criterion and reproduces every reported number
  - Agent Type: validator
  - Resume: true

### Available Agent Types

Per the standard roster. Domain framing for `journal-builder` and `journal-tester`:
**Domain: Redis/Popoto data** — every op accepts `pipeline=`, core commands + Lua only (no
Redis modules), derived index keys live outside the model key space since #540, and any
ad-hoc script must set `REDIS_URL` to a non-zero DB before importing popoto.

## Step by Step Tasks

### 1. Append-only mixin
- **Task ID**: build-append-only
- **Depends On**: none
- **Validates**: `tests/test_provenance_journal.py` (append-only cases)
- **Informed By**: spike-1 (EXISTS is the only reliable signal; `delete_all()` routes through
  the override; `_db_content` is empty on query-loaded instances)
- **Assigned To**: append-only-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/fields/append_only.py` with `AppendOnlyViolation` (in
  `src/popoto/exceptions.py` if that is where the repo keeps exception types) and
  `AppendOnlyMixin` overriding `save()` and `delete()`
- Gate on `EXISTS self.db_key.redis_key` read from `POPOTO_REDIS_DB` **directly, never from a
  caller-supplied pipeline**; never on `_db_content` or `_saved_field_values`
- Raise unconditionally on `save(migrate_key=True)` and whenever `obsolete_redis_key` is set
- Declare `roundtrip_policy = "rebuild"` in the class body — required by
  `tests/test_transfer_roundtrip.py::test_every_model_level_mixin_has_a_policy_declared`
- Add `hard_delete` classmethod as the documented retention/admin escape hatch, sweeping
  derived state (validity zsets, both chain hashes, `$TagF:`/`$IndexedF:` keys, class Set)
- Module docstring states the ORM-layer boundary, both TOCTOU shapes (cross-process and
  intra-pipeline), and that `on_conflict="overwrite"` is unsupported
- Export from `src/popoto/__init__.py` (import block + `__all__`)

### 2. Journal model, recipe, and constants
- **Task ID**: build-journal
- **Depends On**: build-append-only
- **Validates**: `tests/test_provenance_journal.py`
- **Informed By**: spike-2 (call `execute_supersede` directly, never `SupersessionProtocol`,
  on the write path; set valid-time at construction), spike-3 (`IndexedField` for `target`)
- **Assigned To**: journal-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `src/popoto/recipes/provenance_journal.py` with `JournalEntry` (field list per D4,
  composing `AppendOnlyMixin, NeverRecordMixin, EventStreamMixin, Model`), the
  `ProvenanceJournal` façade, `AnnotationResult`, and `JournalBlockedError`
- Implement **D7's pre-flight in full** — firewall scan (incl. `subjects`, per D8), kind
  vocabulary, `kind`/`target` consistency, target existence, same-agent target, `at >=`
  target's stored `valid_from` read via `get_interval_keys`, and `pipeline.transaction is
  True` — all raising **before any command is issued or queued**
- `pre_save` validation as defence in depth for `kind` and the `kind`/`target` rules
- `supersede`/`retract` build one transactional pipeline: annotation constructed with
  `validity=<instant>`, `save(pipeline=pipe)`, then `ValidityField.execute_supersede(...,
  mode="invalidate", ...)`, then `execute()` — with an inline comment citing #588 explaining
  why `SupersessionProtocol` is not used here
- Return `AnnotationResult(entry, target_closed, coupling_enabled)` from every mutating op;
  warn-once on the first uncoupled annotation
- Set `_stream_max_length` on the class body with a pinning comment; **no**
  `_stream_partition_field`; `_stream_metadata_fields = ("agent_id", "kind", "target")`
- Add `JOURNAL_VALIDITY_COUPLING_ENABLED` (env-backed via `_read_journal_coupling_switch`,
  reading `POPOTO_JOURNAL_COUPLING_DISABLE`) and `JOURNAL_KINDS` to
  `src/popoto/fields/constants.py` under a new section header, **and exempt both in
  `tests/benchmarks/test_defaults_sync.py`'s `field_kwargs_and_class_attrs` set** with a
  rationale comment matching the `VALIDITY_GATING_ENABLED` precedent. **Do not touch
  `tests/benchmarks/overrides.py`** — a `MODULE_CONSTANTS` entry would require a module-level
  alias that must not exist for a runtime-flippable switch.
- Support the `_journal_kinds` subclass extension seam, validated as a superset of the core
  four; unknown kinds are inert for membership
- Export from `src/popoto/recipes/__init__.py` (alphabetized, both the import and `__all__`)

### 3. Test suite
- **Task ID**: build-tests
- **Depends On**: build-journal
- **Validates**: `tests/test_provenance_journal.py`
- **Informed By**: spike-1 (the full re-save matrix), spike-2 (#588 regressions), the V0
  suite's teardown pattern
- **Assigned To**: journal-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Teardown: **a `Defaults` restore fixture only.** `popoto.pytest_plugin`'s autouse
  `_popoto_flush_db` already flushes before every test (`pytest_plugin.py:234-250`), so no
  derived-key wipe is needed — see D6
- A control model without `AppendOnlyMixin`, per the repo's convention
- Cover: the extended re-save matrix (six shapes incl. `migrate_key=True`, plus the
  intra-pipeline duplicate); full field round-trip incl. `statement`; `annotations_for` in one
  query via `_CallCounter`; membership exclusion after supersede/retract with `as_of` still
  returning the target and zero chain-hash reads; `chain()` over a 3-deep chain; every D7
  pre-flight rejection asserted to issue zero commands; the blocked-annotation-closes-nothing
  case; the kill switch via `_CallCounter` plus `AnnotationResult.target_closed`; the
  `target`-survives-the-entropy-detector regression; `hard_delete`'s derived-state sweep; the
  orphan-index read (Race 1); concurrent double-close idempotency (Race 3)
- Atomicity/fault-injection work is a distinct skill from round-trip coverage; split it into
  its own test class within the file
- The 20k p50/p99 append benchmark goes in `tests/benchmarks/` behind a marker, **not** in
  this file

### 4. Validation
- **Task ID**: validate-journal
- **Depends On**: build-tests
- **Validates**: `.venv/bin/python -m pytest tests/test_provenance_journal.py tests/test_validity_field.py tests/benchmarks/test_defaults_sync.py -q` plus every row of the Verification table
- **Assigned To**: journal-validator
- **Agent Type**: validator
- **Parallel**: false
- Reproduce every number independently before it is relayed; state python and redis-py
  versions with each
- Run the Verification table
- Confirm the editable install resolves to this checkout before trusting any count

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: build-journal
- **Validates**: `.venv/bin/python -m mkdocs build --strict` and `grep -q 'features/provenance-journal.md' mkdocs.yml`
- **Assigned To**: journal-documentarian
- **Agent Type**: documentarian
- **Parallel**: true (runs alongside build-tests and validate-journal — the docs are written
  against the plan and the built API, and do not need the test results)
- Everything in the Documentation section

### 6. Final validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Validates**: every row of the Verification table, every Success Criterion, and the mypy delta (`.venv/bin/python -m mypy src/` in this worktree vs the same command against a clean sibling checkout of the merge-base, using this same venv)
- **Assigned To**: journal-validator
- **Agent Type**: validator
- **Parallel**: false
- All Success Criteria, `mkdocs build --strict`, mypy delta vs merge-base in the same venv

## Verification

**Command-shape rules these rows obey** (each was empirically wrong in the first draft and is
corrected here):

- **No shell pipes.** A `|` inside a markdown table cell must be escaped as `\|`, and a
  builder copying the row verbatim gets a literal backslash-pipe. Every row below is
  pipe-free, so what is written is what runs.
- **No `grep -c` with multiple files.** `grep -c PAT a b` prints `a:N` / `b:M` per file, never
  a scalar, so a "match count == 0" expectation is unsatisfiable by that command shape.
- **No `grep -c` for absence at all.** `grep -c` exits 1 on zero matches, so an
  absence-expectation row reports failure under any `set -e` runner even when it passes.
  Absence is expressed as `grep -q ...` with `exit code != 0`.
- **No `\|` alternation inside ERE.** In `grep -E`, `\|` is a literal pipe character, not
  alternation — the first draft's Valkey regex matched nothing real and would have passed
  vacuously. Alternation is written bare, as `(A|B)`, inside a single-quoted pattern with no
  table escaping.
- **`.venv/bin/python`, never bare `python`** — see Prerequisites.

| Check | Command | Expected |
|-------|---------|----------|
| Journal tests pass | `.venv/bin/python -m pytest tests/test_provenance_journal.py -q` | exit code 0 |
| V0 suite still passes | `.venv/bin/python -m pytest tests/test_validity_field.py -q` | exit code 0 |
| Defaults sync passes | `.venv/bin/python -m pytest tests/benchmarks/test_defaults_sync.py -q` | exit code 0 |
| Transfer roundtrip guards pass | `.venv/bin/python -m pytest tests/test_transfer_roundtrip.py -q` | exit code 0 |
| Exports importable | `.venv/bin/python -c "from popoto import AppendOnlyMixin, AppendOnlyViolation, JournalBlockedError; from popoto.recipes import JournalEntry, ProvenanceJournal"` | exit code 0 |
| Lint clean | `.venv/bin/python -m ruff check src/` | exit code 0 |
| Format clean | `.venv/bin/python -m black --check src/popoto/recipes/provenance_journal.py src/popoto/fields/append_only.py src/popoto/fields/constants.py src/popoto/__init__.py src/popoto/recipes/__init__.py` | exit code 0 |
| Docs build | `.venv/bin/python -m mkdocs build --strict` | exit code 0 |
| Docs page exists in nav | `grep -q 'features/provenance-journal.md' mkdocs.yml` | exit code 0 |
| No Redis-module commands (anti-criterion) | `grep -rqE '(^\|[^A-Za-z])(BF\|CMS\|TOPK\|TDIGEST\|JSON\|FT\|TS)\.[A-Z]' src/popoto/recipes/provenance_journal.py src/popoto/fields/append_only.py` | exit code != 0 |
| No silent exception swallowing (anti-criterion) | `.venv/bin/python -c "import re,sys; src=open('src/popoto/recipes/provenance_journal.py').read()+open('src/popoto/fields/append_only.py').read(); sys.exit(1 if re.search(r'except[^\n]*:\s*\n\s*(pass\|\.\.\.)\s*\n', src) else 0)"` | exit code 0 |
| Write path never uses SupersessionProtocol (anti-criterion, guards the #588 workaround) | `grep -qE 'SupersessionProtocol\.(supersede\|invalidate)\(' src/popoto/recipes/provenance_journal.py` | exit code != 0 |
| No extraction-path wiring leaked in (anti-criterion for the M3 No-Go) | `git diff --quiet $(git merge-base HEAD origin/main) -- src/popoto/recipes/subconscious_memory.py` | exit code 0 |
| `models/base.py` untouched (anti-criterion for D2) | `git diff --quiet $(git merge-base HEAD origin/main) -- src/popoto/models/base.py` | exit code 0 |
| `overrides.py` untouched (anti-criterion: kill switches are exempted in the test file, never aliased) | `git diff --quiet $(git merge-base HEAD origin/main) -- tests/benchmarks/overrides.py` | exit code 0 |

The two `git diff` rows use `$(git merge-base HEAD origin/main)` rather than `origin/main`
directly: diffing against a moving `origin/main` false-fails the moment another lane lands a
commit touching those files.

Note on the `(A\|B)` patterns above: every `\|` in the table is **markdown table-cell escaping
only**. The patterns that actually run are `(BF|CMS|TOPK|TDIGEST|JSON|FT|TS)\.[A-Z]`,
`SupersessionProtocol\.(supersede|invalidate)\(`, and `except[^\n]*:\s*\n\s*(pass|\.\.\.)\s*\n`
— bare `|` alternation. A builder transcribing these must unescape the table pipes, not copy
the backslashes.

Polarity note: every row above is written so the **passing** state is the natural one for its
command — `exit code 0` where a command succeeds on success, `exit code != 0` only for the two
`grep -q` absence checks, where "grep found nothing" genuinely is a nonzero exit. No row
inverts a Python `sys.exit` to achieve its expectation, so a later reader cannot "correct" a
row into a real inversion.

## Critique Results

War room run 2026-08-18 against plan revision `16978e9`, three `plan-reviewer` critics with
disjoint lenses (integration correctness / failure modes & honesty / scope & downstream
contracts). All three returned **NEEDS REVISION**. Every BLOCKER and every CONCERN below is
addressed in this revision; nothing was waived.

Three findings were independently reproduced against source before acting, because each
reversed something the plan asserted: the defaults-sync registration mechanism
(`test_defaults_sync.py:110-135` vs `overrides.py:37`), the model-level-mixin policy guard
(`test_transfer_roundtrip.py:656-696, 752-767`), and the autouse flush fixture
(`pytest_plugin.py:234-250`).

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | all three | A never-record-blocked annotation still closes the target: `save()` returns the pipeline (not `False`) in pipeline mode, so nothing is queued but the invalidate EVAL commits anyway — membership change with zero provenance | **D7** pre-flight scan before opening the pipeline; `JournalBlockedError` raised | The single highest-severity finding of the round. Independently reported by all three critics from different angles |
| BLOCKER | integration, honesty | "Partial application is impossible" is false — Redis MULTI/EXEC does not roll back siblings on a command error; `base.py:1563-1571` documents this verbatim as #476's rationale | **D3** atomicity restated as "no interleaving reader observes the annotation without the close"; rollback explicitly not claimed | The residual is now a tested, documented state rather than an impossibility claim |
| BLOCKER | integration, honesty | `CLOSE_BEFORE_START` → `ValueError` remap does not apply on the pipeline path, and the client-side pre-check compares against the caller-supplied `valid_from`, so it never fires | **D7 step 4** pre-reads the target's stored `valid_from` and raises before queuing | A second test pins the raw V0 behavior so the pre-flight cannot be refactored away as redundant |
| BLOCKER | integration, scope | The `MODULE_CONSTANTS` registration instruction was backwards and would have broken `test_module_alias_matches_defaults` with `AttributeError` | **D5** registration note; Test Impact marks `overrides.py` **NO CHANGE**; anti-criterion row added | This error was introduced by an earlier driver-side "fix" of mine. Verified directly against the suite before reversing |
| BLOCKER | honesty | `save(migrate_key=True)` destroys entries through a supported public kwarg and the `EXISTS` guard cannot see it | **D2** raises unconditionally on `migrate_key=True` and on a set `obsolete_redis_key` | Added as a sixth row of the re-save matrix |
| BLOCKER | honesty | Risk 2's performance fallback ("skip `EXISTS` for AutoKeyField keys") would disable the guard for precisely the two shapes it catches in practice | **Risk 2** fallback withdrawn; `HSETNX`/`SETNX` named as the correct direction | |
| BLOCKER | honesty | The kill-switch "byte-identical" criterion is unachievable — a clock value enters ARGV, and the repo has no byte-comparison precedent | **Success Criteria** restated on the `_CallCounter` pattern from `test_validity_field.py:598-655` | |
| BLOCKER | scope | No link from a `JournalEntry` to the memory record it justifies; plan never states substrate vs sidecar | **Architectural Impact** states the journal *is* the substrate, per #560's "belief set becomes a view computed at read time" | Decided in-plan, not escalated. No `record_key` field is added, deliberately |
| CONCERN | honesty, scope | The kill switch reproduced #588's own silent-no-op shape by design | **D3** typed `AnnotationResult(entry, target_closed, coupling_enabled)` + warn-once | Caller can detect degraded mode without reading Redis |
| CONCERN | honesty | `JOURNAL_VALIDITY_COUPLING_ENABLED` was called deploy-level but was a plain class attribute an adopter cannot flip | **D5** env-backed `_read_journal_coupling_switch()` / `POPOTO_JOURNAL_COUPLING_DISABLE` | Matches `_read_never_record_switch` convention |
| CONCERN | honesty | `subjects` (a `TagField`) is never scanned by the firewall — `_never_record_scan_values` yields only `str` | **D8** explicit `scan_never_record()` on each tag in `append()`; residual mixin hole documented and pinned by test | |
| CONCERN | integration | Two different `ingested_at` values under one name — the model field and the validity ZSET would silently disagree | **D4** model field renamed `captured_at`; validity ingest axis documented as always the save clock | |
| CONCERN | scope | `_stream_partition_field = "agent_id"` produces per-agent stream keys that `StreamConsumer`'s single-key API cannot consume | **D4 / Data Flow 7** partition field dropped; `agent_id` moved into `_stream_metadata_fields` | M1 was about to ship a channel its named consumer could not read |
| CONCERN | integration, honesty | "4 queued commands for a supersede" was arithmetic on the spike's toy model; real `JournalEntry` queues ~10 | **Risk 1** asserts shape (`ARGV[5]`, `numkeys`, `transaction`), never a literal count | |
| CONCERN | integration | `AppendOnlyMixin` trips `test_every_model_level_mixin_has_a_policy_declared`; "no existing tests affected" was wrong | **D2** declares `roundtrip_policy = "rebuild"`; Test Impact lists the suite | |
| CONCERN | integration, scope | D6's whole premise was false — the pytest plugin already flushes before every test | **D6** rewritten; teardown reduced to a `Defaults` restore; Open Question 2 withdrawn | |
| CONCERN | honesty | `on_conflict="overwrite"` breaks on append-only models via `import_.py:215` | **Architectural Impact** documents `"skip"` as the supported mode | |
| CONCERN | honesty | Caller-supplied `pipeline(transaction=False)` silently voids atomicity | **D7 step 5** raises `ValueError` | |
| CONCERN | honesty | Race 2 has a second, deterministically testable shape: two appends of one key in one pipeline | **Race 2** documents it and adds it to the test matrix | |
| CONCERN | scope | `JOURNAL_KINDS` frozen with no extension seam blocks M5/M7/M8 | **D5** `_journal_kinds` subclass override + "unknown kinds are inert for membership" reader rule | |
| CONCERN | scope | D1 reversibility overstated — per-model key namespacing makes a later split a migration of an immutable store | **Architectural Impact** restated: high before data, low after; `ProvenanceJournal` as sole API is the mitigation | |
| CONCERN | scope | Success Criteria omitted `statement`, never-record composition, `chain()`, and Risk 3's validation | **Success Criteria** all four added | |
| CONCERN | honesty | Several criteria were prose-only with no stated method | **Success Criteria** each now names its mechanism (`_CallCounter`, chain-hash monkeypatch, call counts) | |
| CONCERN | honesty | mypy delta asserted, not budgeted | **Success Criteria** excess must be enumerated line-by-line with redis-py version, per CLAUDE.md gate 5 | |
| CONCERN | honesty | Freshness grep arithmetic did not sum (claimed 132, rows summed ~53) | **Freshness Check** re-measured: 137 matching lines; per-token counts given exactly with a note that lines matching two tokens count once | |
| CONCERN | scope | 20k benchmark in the unit-test file would add minutes to every run; docs task serialized unnecessarily | **Test Impact / Tasks** benchmark moved to `tests/benchmarks/`; docs task now parallel from `build-journal` | |
| NIT | honesty | Spike-1's "nothing escapes the override" overclaims — raw clients and the migration cookbook bypass it | **Problem / D2** scoped to "every Python write path in `models/base.py`", cookbook cited | |
| NIT | honesty | Valkey anti-criterion regex unanchored, matches `PARTS.X` | **Verification** anchored with `(^\|[^A-Za-z])` | |
| NIT | honesty, scope | Two verification rows diffed against a moving `origin/main`; `black --check` covered only 2 of 5 edited files | **Verification** `$(git merge-base HEAD origin/main)`; black row extended | |
| NIT | integration, scope | `JOURNAL_STREAM_MAX_LENGTH` duplicated `_stream_max_length` and was never wired | **D4/D5** constant dropped; class attribute set directly with a pinning comment | |
| NIT | integration | `filter(validity=t)` silently returns nothing | **D4** documented on the feature page | |
| NIT | integration | `agent_id = KeyField()` with `None` collapses the stream key and renders `"None"` into the record key | **D4** `append()` requires `agent_id` non-null | |
| NIT | scope | Cross-agent supersession silently permitted | **D7 step 3** rejects cross-agent targets | |
| BLOCKER (method) | scope | The scope critic had no `gh` access and could not verify the literal AC walk or that the No-Go slug targets exist | Verified by me directly: #562, #563, #564, #565, #588 all exist, are OPEN, and cover the deferred work; #560's five ACs plus the Amendment acceptance addition each map to a Success Criterion | |

---

## Open Questions

Two questions, both genuinely policy-level. A third question in an earlier draft — whether
`hard_delete` was the right test-teardown seam — has been **decided in-plan rather than
escalated**, because its premise was false: the pytest plugin already flushes the DB before
every test (D6), so no teardown seam is needed. `hard_delete` ships, justified on Risk 4b
erasure grounds alone, with a full derived-state sweep.

1. **Should the append-only invariant have a deploy-level kill switch?** The repo's default
   is that every capability ships with one (`feedback_default_on_design`). This plan argues
   append-only is a *contract*, not a capability — disabling it silently converts the
   provenance journal into a mutable table while M3/M5/M6 still assume immutability. The
   plan currently ships **no** switch on the invariant and one env-backed switch on the
   validity coupling (`POPOTO_JOURNAL_COUPLING_DISABLE`). Confirm, or specify the switch's
   semantics.
2. **Is "one MULTI/EXEC transaction" an acceptable reading of the #560 amendment's "in the
   same script"?** D3 explains why forking `SUPERSEDE_LUA` is worse. The property the
   amendment observably asks for — no interleaving reader sees the annotation without the
   close — is preserved exactly; rollback is not, and the plan says so rather than claiming
   otherwise. Flagging because it is a literal deviation from written wording on the wave
   keystone.
