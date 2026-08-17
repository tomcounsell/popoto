---
status: Planning
type: feature
appetite: Large
owner: Valor Engels
created: 2026-08-17
tracking: https://github.com/tomcounsell/popoto/issues/560
last_comment_id: none
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

An append-only journal where every capture is immutable and fully attributed — speaker, turn
id, verbatim span, subjects, stated-vs-inferred — and corrections are *new entries* pointing
at prior entries. Nothing is destroyed. Given any entry, every annotation targeting it is one
query away. When a `supersede` or `retract` annotation lands, its target leaves
`validity__current` membership in the same transaction, with no chain walk at read time.

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
`grep -rn 'supersede|provenance|speaker|verbatim' src/` returns zero hits. It now returns
**132**. Breakdown measured at `9180680`:

| Token | Hits | What they are |
|---|---|---|
| `provenance` | ~20 | Pre-existing and unrelated — export/import manifest provenance (`transfer/export.py`, `transfer/import_.py`, `transfer/format.py`, `transfer/results.py`) and "identity provenance" comments in `models/encoding.py:418,442,493,630`. These predated the issue; the claim was inaccurate as written. |
| `supersede`/`superseded` | ~25 | Genuinely new via #582 — `validity_field.py` (`SUPERSEDE_LUA:157`, `validity__current:66`), `supersession.py`, `query.py:427`, `constants.py:131`. Plus pre-existing `_superseded_by` plumbing in `observation.py:167-483`. |
| `verbatim` | ~8 | All prose/behavioral, no stored field — `extraction/__init__.py:16,189`, `integrations/mcp_server.py:87`, `integrations/service.py:115`, `default_memory.py:5,59`, `trajectory_memory.py:361`. |
| `speaker` | **0** | Still completely absent. |
| `journal`/`JournalEntry` | **0** | No append-only substrate exists. |

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
2. **Privacy firewall** — `JournalEntry` composes `NeverRecordMixin`, so `Model.save()` step
   0 (`base.py:1232-1244`) scans every non-key string field, including `verbatim` and
   `statement`. A blocked entry writes a content-free tombstone and never reaches Redis. This
   is the first gate; nothing downstream sees the content.
3. **Append-only guard** — `AppendOnlyMixin.save()` runs before `super().save()` and refuses
   any save whose redis key already exists.
4. **Persistence** — `Model.save()` writes the entry hash, the class Set, the
   `IndexedField` Sets for `turn_id`/`speaker`/`kind`/`target`, the `TagField` Sets for
   `subjects`, and `ValidityField.on_save` opens the entry's interval
   (`valid_from`, `invalid_at=+inf`, `ingested_at`) via `SUPERSEDE_LUA` in `mode="open"`.
5. **Annotation** — `confirm(entry, ...)` appends a `kind="confirm"` entry with
   `target=<entry redis key>` and stops there; membership is unaffected.
   `supersede(entry, ...)` / `retract(entry, ...)` open one pipeline, queue the annotation's
   `save(pipeline=pipe)`, queue `ValidityField.execute_supersede(mode="invalidate",
   old_member=<target>, new_member=<annotation>, close_at=<instant>, pipeline=pipe)`, and
   `execute()`. One MULTI/EXEC.
6. **Notification** — `EventStreamMixin` `XADD`s the mutation onto `stream:journal` inside
   the same pipeline, so a downstream `StreamConsumer` (the M5 reconciler's wake-up channel)
   sees the entry and the stream entry commit or fail together.
7. **Output** — `annotations_for(entry)` is one `filter(target=<redis key>)`;
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
- **Reversibility**: high. The journal is a new keyspace with no reader outside its own
  tests until M3/M5/M6 land. Reverting is deleting two modules, their exports, their
  `Defaults` entries, and their keys. No existing model or query path changes behavior.

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
key exists. `delete()` unconditionally raises. The escape hatch is a named classmethod —
`JournalEntry.hard_delete(instance)` / `AppendOnlyMixin.hard_delete` — documented as
retention/admin-only and greppable, rather than a `skip_append_only=True` kwarg that would
require modifying `base.py:1114`'s signature.

**D3 — Valid-time is set at construction; the write path calls `execute_supersede`
directly.** Per spike-2 and #588. `ProvenanceJournal` constructs the annotation with
`validity=<instant>` so `ValidityField.on_save` writes the intended `valid_from`, then
queues `save(pipeline=pipe)` and `ValidityField.execute_supersede(..., mode="invalidate",
old_member=<target key>, new_member=<annotation key>, close_at=<instant>, pipeline=pipe)`
into the same transactional pipeline. `SupersessionProtocol` is used only for **read-side**
traversal (`superseded_by`, `supersedes`, `chain`), never on the write path. This deviates
from the #560 amendment's literal wording ("in the same script") in favor of "in the same
MULTI/EXEC transaction", because forking `SUPERSEDE_LUA` would violate the epic's "one
supersession mechanism" rule. The atomicity property the amendment asks for — target
excluded from `validity__current` the instant the annotation is visible — holds exactly, and
is asserted by test.

**D4 — Field list.**

| Field | Type | Why |
|---|---|---|
| `entry_id` | `AutoKeyField()` | Immutable UUID identity; makes the TOCTOU window in D2 practically unreachable for appends |
| `agent_id` | `KeyField()` | Partition key, mirroring `DefaultMemory:117`; keeps multi-agent journals separable |
| `ingested_at` | `FloatField()` | Transaction time — the entry timestamp the issue names. Distinct from `valid_from` |
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
with `_stream_name = "journal"`, `_stream_partition_field = "agent_id"`, and
`_stream_metadata_fields = ("kind", "target")` so the M5 reconciler can filter without
hydrating.

**No `EmbeddingField`.** Deliberate, mirroring `default_memory.py:62-72`: it pulls an
optional extra, and similarity lookup over the journal is not on M1's acceptance list. The
module documents the subclass-with-an-`EmbeddingField` recipe instead, so the extension point
is explicit rather than a gap.

**D5 — Constants and kill switch.** New `Defaults` entries, following the
`{PRIMITIVE}_{WHAT}` convention with sweep-provenance comments and a section header:

- `JOURNAL_VALIDITY_COUPLING_ENABLED = True` — **deploy-level kill switch.** When `False`,
  `supersede`/`retract` still append their annotation entries and still write `target`, but
  do not close the target's interval; membership degrades to "everything ever appended,"
  which is exactly pre-M1 behavior. Boolean, not swept.
- `JOURNAL_STREAM_MAX_LENGTH` — the `EventStreamMixin` trim bound for `stream:journal`, pinned
  rather than inherited, because the reconciler's wake-up channel has different volume
  characteristics from the generic mutation stream.
- `JOURNAL_KINDS` — the frozen kind vocabulary. Not a tunable; changing it reclassifies stored
  entries.

**There is deliberately no kill switch on the append-only invariant.** It is the model's
defining contract, not a capability: a deployment that disables it silently converts the
provenance journal into a mutable table while every downstream module still assumes
immutability. This is the one place this plan departs from the repo's "every capability gets
a deploy-level kill switch" default, and it is flagged as an open question rather than
assumed.

**D6 — Test-teardown consequence.** Because `delete_all()` raises (spike-1), the test suite
uses `hard_delete` plus an explicit derived-key wipe modeled on
`tests/test_validity_field.py:206-254` — `ValidityField.get_all_keys(...)` values, the
`{prefix}:open:*` glob, the `$TagF:`/`$IndexedF:` index keys (which, per #540, live outside
the model key space), and a `Defaults` restore in an autouse fixture.

## Failure Path Test Strategy

### Exception Handling Coverage

- `EventStreamMixin._xadd_mutation` swallows exceptions in non-pipeline mode
  (`event_stream.py:196-200`) and re-raises in pipeline mode (`:193-196`). M1's annotation
  path is always pipelined, so a stream failure must abort the whole annotation. **Test:**
  fault-inject an `XADD` failure inside the annotation pipeline and assert the exception
  propagates AND the target's interval was not closed.
- `NeverRecordMixin.write_tombstone` wraps its audit write in `try/except: pass`
  (`never_record.py:501`) so audit failure never fails the firewall open. M1 adds no handler
  of its own here. **Test:** assert a blocked `append()` returns the documented sentinel and
  persists nothing, with the tombstone-write path fault-injected.
- `ValidityField.execute_supersede` remaps a `CLOSE_BEFORE_START` Lua error to `ValueError`
  (`validity_field.py:801-807`). **Test:** `supersede(entry, at=<before entry.valid_from>)`
  raises `ValueError` and leaves both entries' intervals untouched.
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

No existing tests are affected. This is a greenfield module: it adds two new source files,
touches no existing model, changes no existing signature, and modifies no existing behavior.
The only edits to existing files are additive — imports plus `__all__` entries in
`src/popoto/__init__.py` and `src/popoto/recipes/__init__.py`, and new constants in
`src/popoto/fields/constants.py`.

- [ ] `tests/benchmarks/test_defaults_sync.py::test_all_defaults_covered_by_module_constants`
  — **UPDATE**: this is the specific assertion that fails when a `Defaults` entry is added
  without a corresponding registration. It is the one existing test that will fail without a
  change.
- [ ] `tests/benchmarks/overrides.py` (`MODULE_CONSTANTS`) — **UPDATE**: the data the
  assertion above reads. Adding the three `JOURNAL_*` constants to `constants.py` without
  registering them here is what makes the test fail; editing the test file alone does not fix
  it. Both files must change together.
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
**Mitigation:** M1 asserts the contract structurally, not just behaviorally: a test that
counts the commands queued on the pipeline (4 for a supersede) and a test that asserts
`execute_supersede` is called with `mode="invalidate"` and both member arguments non-empty.
This mirrors `tests/test_validity_field.py`'s "numkeys is 4 at all three call sites"
structural assertion. Plus the regression tests for both #588 findings.

### Risk 2: The `EXISTS` guard adds a round trip to every append

**Impact:** The journal is the write-hot path for the whole wave — every extracted fact
becomes an entry. One extra RTT per append is a real cost at ingest volume.
**Mitigation:** Measure it. The plan includes a p50/p99 append micro-benchmark at the epic's
20k-record scale target, reported with the environment, comparing `JournalEntry.save()`
against an identical model without `AppendOnlyMixin`. If the overhead is material, the
documented fallback is to skip the `EXISTS` when the key came from `AutoKeyField` (a
freshly-generated UUID cannot collide) — but that is a measured decision, not an assumed one,
and the number goes in the PR body either way.

### Risk 3: One record type makes `kind`/`target` consistency a runtime concern

**Impact:** Nothing at the type level stops an `assert` entry from carrying a `target`, or a
`supersede` entry from targeting a nonexistent key. Downstream M6 resolution would then see
malformed provenance.
**Mitigation:** `pre_save` validation with a test per invalid combination (D1), and target
existence checked before any write. Accepted as the stated cost of D1 rather than papered
over.

### Risk 4: Journal growth is unbounded by design

**Impact:** Append-only plus never-delete means the keyspace only grows. At the epic's 20k
target this is fine; a long-lived production agent is a different curve.
**Mitigation:** Out of scope to solve, in scope to measure and document. The docs page states
the growth characteristic explicitly (bytes per entry × append rate) and names `hard_delete`
as the retention seam. A retention policy is downstream work with its own data-safety review.

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
**Mitigation:** Inherited, not re-solved — M1 introduces no new eager-EVAL site and adds no
`UniqueField`. The plan asserts the resulting orphan-index behavior is *readable*: an index
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
**Mitigation:** Structurally narrowed rather than locked. `entry_id` is an `AutoKeyField`
(UUID), so two independent appends cannot produce the same key; the window is only reachable
when a caller supplies an explicit colliding key, which is a programming error the guard
still catches in every non-concurrent case. The limitation is documented in the mixin
docstring and on the docs page as a known boundary — **not** claimed as a hard guarantee. A
storage-level guarantee would need an `HSETNX`-based write path (see Rabbit Holes). Tested to
the extent it is testable: the sequential cases from spike-1's matrix are all asserted; the
concurrent case is documented, not asserted, because a passing test would be a race itself.

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
  `AppendOnlyMixin` and `AppendOnlyViolation` exported from `popoto` — all four importable
  from a fresh interpreter
- [ ] Re-saving an existing entry raises `AppendOnlyViolation`, for all four re-save shapes in
  spike-1's matrix; `delete()` and `delete_all()` both raise
- [ ] Entries persist and round-trip `speaker`, `turn_id`, `verbatim`, `subjects[]`, `stated`,
  `kind`, `target`, `ingested_at`, and the validity interval
- [ ] `ProvenanceJournal.annotations_for(entry)` returns every annotation targeting it in one
  `filter()` call
- [ ] After a `supersede` or `retract`, the target is absent from
  `filter(validity__current=True)` immediately, with no chain walk — and still returned by
  `filter(validity__as_of=<before the close>)`
- [ ] The annotate-and-close sequence is one MULTI/EXEC: asserted by queued-command count and
  by a fault-injection test showing partial application is impossible
- [ ] Regression tests for both #588 findings (pipeline no-op; valid-time skew) pass against
  M1's write path
- [ ] Valkey-safe: no Redis-module command anywhere in the new code (core commands + reuse of
  V0's Lua only) — asserted by an anti-criterion grep
- [ ] Every `ProvenanceJournal` mutating op accepts `pipeline=` and returns the pipeline
  unexecuted when given one
- [ ] `JOURNAL_VALIDITY_COUPLING_ENABLED = False` makes annotation writes byte-identical to
  the no-validity path (asserted by command-level comparison, mirroring V0's kill-switch test)
- [ ] Append p50/p99 measured at 20k entries and reported with python/redis-py versions
- [ ] Tests pass (`/do-test`), narrow scope: `tests/test_provenance_journal.py` plus
  `tests/test_validity_field.py` and `tests/benchmarks/test_defaults_sync.py`
- [ ] mypy error delta is 0 vs the merge-base. **Baseline already measured for this plan:
  1119 errors in 66 files (93 source files checked) at `9180680`**, using a clean detached
  sibling worktree and this worktree's venv — Python 3.12.13, redis-py 8.1.0, mypy 2.3.1,
  Redis server 8.6.2. The branch number must equal 1119; any other number is stated with its
  environment, never relayed without one
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
- Gate on `EXISTS self.db_key.redis_key`; never on `_db_content` or `_saved_field_values`
- Add `hard_delete` classmethod as the documented retention/admin escape hatch
- Module docstring states the ORM-layer boundary and the TOCTOU limitation plainly
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
  composing `AppendOnlyMixin, NeverRecordMixin, EventStreamMixin, Model`) and the
  `ProvenanceJournal` façade
- `pre_save` validation for `kind` ∈ `JOURNAL_KINDS` and the `kind`/`target` consistency rules
- `supersede`/`retract` build one transactional pipeline: annotation constructed with
  `validity=<instant>`, `save(pipeline=pipe)`, then `ValidityField.execute_supersede(...,
  mode="invalidate", ...)`, then `execute()` — with an inline comment citing #588
- Every mutating op accepts `pipeline=` and returns it unexecuted when supplied
- Add `JOURNAL_VALIDITY_COUPLING_ENABLED`, `JOURNAL_STREAM_MAX_LENGTH`, `JOURNAL_KINDS` to
  `src/popoto/fields/constants.py` under a new section header, **and register all three in
  `tests/benchmarks/overrides.py`'s `MODULE_CONSTANTS`** — that registry, not the test file,
  is what `test_defaults_sync.py::test_all_defaults_covered_by_module_constants` reads. Both
  files change together or the suite fails.
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
- Teardown fixture: `hard_delete` + derived-key wipe (`ValidityField.get_all_keys`,
  `{prefix}:open:*`, `$TagF:`/`$IndexedF:` keys) + `Defaults` restore, modeled on
  `tests/test_validity_field.py:206-254`
- A control model without `AppendOnlyMixin`, per the repo's convention
- Cover: the spike-1 re-save matrix; field round-trip; `annotations_for` in one query;
  membership exclusion after supersede/retract with `as_of` still returning the target;
  queued-command-count and fault-injection atomicity; the kill switch producing a
  byte-identical no-validity path; every Failure Path case; the orphan-index read (Race 1);
  concurrent double-close idempotency (Race 3); the p50/p99 append benchmark at 20k

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
- **Depends On**: validate-journal
- **Validates**: `.venv/bin/python -m mkdocs build --strict` and `grep -q 'features/provenance-journal.md' mkdocs.yml`
- **Assigned To**: journal-documentarian
- **Agent Type**: documentarian
- **Parallel**: false
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
| Exports importable | `.venv/bin/python -c "from popoto import AppendOnlyMixin, AppendOnlyViolation; from popoto.recipes import JournalEntry, ProvenanceJournal"` | exit code 0 |
| Lint clean | `.venv/bin/python -m ruff check src/` | exit code 0 |
| Format clean | `.venv/bin/python -m black --check src/popoto/recipes/provenance_journal.py src/popoto/fields/append_only.py` | exit code 0 |
| Docs build | `.venv/bin/python -m mkdocs build --strict` | exit code 0 |
| Docs page exists in nav | `grep -q 'features/provenance-journal.md' mkdocs.yml` | exit code 0 |
| No Redis-module commands (anti-criterion) | `grep -rqE '(BF\|CMS\|TOPK\|TDIGEST\|JSON\|FT\|TS)\.[A-Z]' src/popoto/recipes/provenance_journal.py src/popoto/fields/append_only.py` | exit code != 0 |
| No silent exception swallowing (anti-criterion) | `.venv/bin/python -c "import re,sys; src=open('src/popoto/recipes/provenance_journal.py').read()+open('src/popoto/fields/append_only.py').read(); sys.exit(0 if re.search(r'except[^\n]*:\s*\n\s*(pass|\.\.\.)\s*\n', src) else 1)"` | exit code != 0 |
| Write path never uses SupersessionProtocol (anti-criterion, guards the #588 workaround) | `grep -qE 'SupersessionProtocol\.(supersede\|invalidate)\(' src/popoto/recipes/provenance_journal.py` | exit code != 0 |
| No extraction-path wiring leaked in (anti-criterion for the M3 No-Go) | `git diff --quiet origin/main -- src/popoto/recipes/subconscious_memory.py` | exit code 0 |
| `models/base.py` untouched (anti-criterion for D2) | `git diff --quiet origin/main -- src/popoto/models/base.py` | exit code 0 |

Note on the two `(A\|B)` patterns above: the `\|` is **markdown table-cell escaping only**.
The pattern that runs is `(BF|CMS|TOPK|TDIGEST|JSON|FT|TS)\.[A-Z]` and
`SupersessionProtocol\.(supersede|invalidate)\(` respectively — bare `|` alternation inside
single quotes. A builder transcribing these must unescape the table pipes, not copy the
backslashes.

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. **Should the append-only invariant have a deploy-level kill switch?** The repo's default
   is that every capability ships with one (`feedback_default_on_design`). This plan argues
   append-only is a *contract*, not a capability — disabling it silently converts the
   provenance journal into a mutable table while M3/M5/M6 still assume immutability. The
   plan currently ships **no** switch on the invariant and one on the validity coupling
   (`JOURNAL_VALIDITY_COUPLING_ENABLED`). Confirm, or specify the switch's semantics.
2. **`hard_delete` as the retention seam — acceptable, or should M1 ship no deletion path at
   all?** Tests need *some* teardown because `delete_all()` raises (spike-1). The
   alternative to a named classmethod is that tests reach for raw `DEL`, which is worse
   because it leaves derived index state. Confirm `hard_delete` is the right shape and the
   right name.
3. **Is "one MULTI/EXEC transaction" an acceptable reading of the #560 amendment's "in the
   same script"?** D3 explains why forking `SUPERSEDE_LUA` is worse. The observable
   atomicity property the amendment specifies is preserved exactly. Flagging because it is a
   literal deviation from written wording on the wave keystone.
