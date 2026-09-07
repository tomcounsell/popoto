---
status: Planning
type: feature
appetite: Medium
owner: Dev (sdlc-494)
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/494
last_comment_id:
---

# Tombstones as Negative Prior

## Problem

Popoto's agent-memory substrate forgets by **tombstoning** (#491): a record that
the agent keeps dismissing decays, leaves the active corpus, and its identity +
death metadata are archived under `$TOMB:{Model}:*`. Nothing reads those
tombstones. The next time the same worthless content arrives — and in
conversation-derived memory it *will*, because the same low-value patterns recur
across sessions — the corpus pays the full ingest → inject → dismiss → decay
cost again, and learns the same lesson from scratch.

The reference deployment (Valor, `tomcounsell/ai`) dismisses **82.1%** of
injections (`/memories/metrics.json`, 2026-07-24). Forgetting without
*remembering that it forgot* means that number never improves on repeats.

**Current behavior:**

- `MemoryLifecycle.tombstone()` (`src/popoto/recipes/memory_lifecycle.py:744`)
  writes a `Tombstone` carrying the record's ExistenceFilter fingerprint —
  explicitly "the identity token #494 will match new writes against"
  (`memory_lifecycle.py:723-742`). Nothing consumes it.
- `WriteFilterMixin._check_write_filter()`
  (`src/popoto/fields/write_filter.py:118`) is the single write gate. It calls
  `compute_filter_score()`, floors non-numerics to `0.0`, and raises
  `SkipSaveException` below `Defaults.WF_MIN_THRESHOLD`. It has no notion of
  "we have seen and buried this before."
- `ExistenceFilter` provides a fingerprint and an O(1) Bloom membership answer,
  but its membership test is **token-OR** (`existence_filter.py:478-500`): any
  single shared word returns `might_exist=True`. It answers "have we seen
  anything like this?", never "have we buried *this*?".

**Desired outcome:**

A new record whose content fingerprint matches a previously-tombstoned record is
admitted with **measurably reduced** write-filter score — escalating with the
number of times that fingerprint has been buried — automatically, with no human
step and no opt-in flag. Repeat burials can push the score under the existing
`WF_MIN_THRESHOLD` gate, at which point the existing machinery drops the write.
Every drawdown is counted and readable. Models without a content fingerprint
issue zero extra Redis commands.

## Freshness Check

**Baseline commit:** `808254ff` (origin/main at lane creation)
**Issue filed at:** 2026-07-26T06:42:30Z
**Disposition:** Minor drift

**File:line references re-verified:**

- `src/popoto/fields/write_filter.py:44` — issue claims `WriteFilterMixin` is a
  score gate with no historical awareness — **still holds, still line 44.** The
  gate body is `_check_write_filter` at `:118`.
- `src/popoto/fields/existence_filter.py:304` — issue claims `ExistenceFilter`
  computes a content fingerprint and answers `might_exist` / `definitely_missing`
  in O(1) — **still holds** (class at `:304`, `might_exist` at `:455`,
  `definitely_missing` at `:491`). **Correction to the issue's premise:**
  `might_exist` is token-OR, so it is not usable as the negative-prior matcher
  (see spike-1).
- Tombstone storage — the issue predates #649/#661. The raw-Redis tombstone
  bookkeeping the issue implies lives in `memory_lifecycle` was relocated to
  `src/popoto/fields/tombstone_store.py` (`TombstoneStore` at `:167`). The new
  code in this plan therefore lands in the **field layer**, next to
  `TombstoneStore`, not in the recipe.

**Cited sibling issues/PRs re-checked:**

- **#491** (hard prerequisite) — **CLOSED**, shipped as PR #495. Tombstones
  exist; the `Tombstone` dataclass carries `fingerprint`,
  `importance_at_death`, `dismissal_count`, `tombstoned_at`. The prerequisite is
  satisfied.
- **PR #417** — capped-evidence Bayesian ConfidenceField — merged; supplies
  `confidence_at_death` / `evidence_count` on the tombstone. Not modified here.

**Commits on main since the issue was filed (touching referenced files):**

- `16aa702e` Agent memory production audit (#594) — touched `write_filter.py`;
  did not change the gate's shape. Irrelevant to the root premise.
- `31535a35` generic export/import (#558) — added `roundtrip_policy = "rebuild"`
  to `WriteFilterMixin`. **Relevant:** the negative-prior counters are derived
  state and must follow the same `rebuild` posture — they are not exported.
- `926953c9` ruff config + CI lint gate (#542) — formatting only.
- PR #661 (#649) relocated tombstone bookkeeping into
  `fields/tombstone_store.py`. **This is the minor drift**: the new store
  belongs beside it.

**Active plans in `docs/plans/` overlapping this area:**

- `forget_guard_test_vacuity.md` (issue #674, lane active) — audits vacuous
  guard tests in `tests/test_memory_lifecycle.py`. **Coordination, not a
  blocker.** Mitigation: all new tests for this work go in a **new file**
  (`tests/test_tombstone_prior.py`); this plan does not restructure
  `tests/test_memory_lifecycle.py`.

**Notes:** The issue's "Solution Sketch" left the mechanism open and asked
`/do-plan` to resolve it. Spikes 1–3 below resolve it.

## Prior Art

- **#491 / PR #495** — *Confidence-modulated decay: outcome evidence changes how
  fast a memory is forgotten.* Shipped the tombstone. Succeeded. This work is its
  intended consumer; `memory_lifecycle.py:728` names #494 by number.
- **PR #417** — *Capped-evidence Bayesian ConfidenceField.* Shipped. Provides
  the "contradicted" end of the evidence model that drives records to death.
- **PR #661 (#649)** — *Route recipes/memory_lifecycle through the field layer.*
  Shipped `TombstoneStore`. Establishes the module and keyspace conventions this
  plan copies (out-of-model-keyspace prefix, pipelined pairs, `get_REDIS_DB()`).
- **#492 (TagField optional scoping)** — established the default-ON +
  auto-detect + `POPOTO_*_DISABLE` deploy switch pattern that
  `Defaults` documents at `constants.py:245-252`. This plan reuses it verbatim.
- **#673** — *Name the empty capture in command-spy assertions.* Merged. The
  reason every new module here uses `get_REDIS_DB()` rather than a
  `POPOTO_REDIS_DB` import.

No prior attempt to consume tombstones exists — this is the first.

## Research

No external research performed. The work uses no external library and no
external API: it is core Redis commands (`HGET`/`HINCRBY`/`ZADD`/`ZCARD`/
`ZRANGE`/`ZREM`/`HDEL`/`HINCRBYFLOAT`) plus `hashlib` from the standard library.
The binding constraint is a repo-internal one (Valkey parity: no Redis modules),
which is settled by using only core commands and is verified by an in-repo grep
check rather than by external documentation.

## Spike Results

### spike-1: Can the existing `ExistenceFilter` Bloom filter serve as the negative-prior matcher?

- **Assumption**: "`ExistenceFilter.might_exist` gives an O(1), Valkey-safe
  'have we buried this?' answer, so the negative prior can be a second Bloom
  filter."
- **Method**: code-read (`src/popoto/fields/existence_filter.py:400-500`)
- **Finding**: **No — invalidated.** `on_save` tokenizes the fingerprint and adds
  each *word* separately; `might_exist` returns True if **ANY** token is present
  (`existence_filter.py:495-499`). Two records sharing one common word match. As
  a negative prior that would penalize nearly every write after a handful of
  burials — precisely the false-positive failure the issue's "False-positive
  safety" criterion forbids. Separately, a Bloom filter cannot store a per-entry
  burial count, which the escalation criterion requires, and `delete` is a
  documented no-op (`:444-453`), so a bounded/evictable structure is impossible.
- **Confidence**: high
- **Impact on plan**: The negative prior gets its **own** structure — a bounded
  hash + ZSET keyed on an exact, normalized fingerprint digest. `ExistenceFilter`
  is used only as the *source* of the fingerprint string, never as the matcher.

### spike-2: Is `_check_write_filter()` a single, sufficient seam, and does a reduced score compose with the existing gate?

- **Assumption**: "Lowering the score inside `_check_write_filter` is enough; no
  other call site needs to change."
- **Method**: code-read (`src/popoto/models/base.py`, grep for
  `_check_write_filter` / `_tag_priority`)
- **Finding**: **Confirmed.** `_check_write_filter()` is called exactly once, at
  `base.py:1416-1419`, guarded by `isinstance(self, WriteFilterMixin) and not
  skip_write_filter`. It caches the result on `self._write_filter_score`, which
  the four `_tag_priority()` call sites (`base.py:1550, 1640, 1752, 1872`) later
  read for the priority ZSET. So a single multiplicative drawdown applied inside
  `_check_write_filter` propagates to **both** consequences for free: the
  `SkipSaveException` gate *and* priority-set membership. `skip_write_filter=True`
  already bypasses the whole gate, giving an existing per-record escape hatch.
- **Confidence**: high
- **Impact on plan**: One edit point. No change to `base.py` at all.

### spike-3: Does the burial site have a *content* fingerprint available, and can it tell one from the redis_key fallback?

- **Assumption**: "`MemoryLifecycle.tombstone()` can record a content
  fingerprint at burial time."
- **Method**: code-read (`memory_lifecycle.py:723-745`)
- **Finding**: **Confirmed, with a required refinement.** `_fingerprint()`
  returns `_compute_fingerprint_impl(field, record)` when the model carries an
  `ExistenceFilter`, and otherwise falls back to `record.db_key.redis_key`. The
  fallback is **useless as a negative prior** — a new record has a new key, so it
  could never match, and recording it would only consume the bound. The burial
  path therefore needs a sibling that returns `Optional[str]`: the content
  fingerprint, or `None` when only the key fallback is available.
- **Confidence**: high
- **Impact on plan**: Add `_content_fingerprint()` beside `_fingerprint()`;
  record a burial only when it returns non-`None`. This is also the **auto-detect
  predicate** for the read side: a model with no `ExistenceFilter.fingerprint_fn`
  has no content identity, so the write path skips the consult entirely and
  issues zero extra Redis commands — which is exactly the "byte-identical write
  behavior" acceptance criterion, satisfied structurally rather than by a flag.

### spike-4: Are all required commands Valkey-safe (no Redis modules)?

- **Assumption**: "A bounded hash + ZSET with float accumulation needs no Redis
  module."
- **Method**: code-read of the command set against the existing
  `TombstoneStore`, which is already documented Valkey-safe.
- **Finding**: **Confirmed.** Required: `HGET`, `HINCRBY`, `HINCRBYFLOAT`, `HDEL`,
  `HGETALL`, `ZADD`, `ZCARD`, `ZRANGE`, `ZREM`, `DEL`. All are core Redis
  commands present in Valkey; `TombstoneStore` already uses seven of the ten.
  `HINCRBYFLOAT` is the only one not already used in the tombstone keyspace and
  is core (Redis 2.6+, Valkey 7+).
- **Confidence**: high
- **Impact on plan**: No module dependency. A grep-based verification row asserts
  no `BF.`/`CMS.`/`TOPK.` string appears in the new code.

## Data Flow

**Burial side (writes the negative evidence):**

1. **Entry point**: `MemoryLifecycle.tick()` decides a record should be forgotten
   (`memory_lifecycle.py:309` policy) and calls `tombstone(record)`.
2. **`MemoryLifecycle.tombstone()`**: archives the record via `TombstoneStore`
   (unchanged), then calls `self._content_fingerprint(record)`.
3. **If a content fingerprint exists**: `TombstonePriorStore.record_burial(fp)`
   digests it and, in one pipeline, `HINCRBY`s the burial count and `ZADD`s the
   digest at the death timestamp.
4. **Bound enforcement**: `ZCARD` over the limit → `ZRANGE` the oldest excess →
   pipelined `HDEL` + `ZREM`. Same shape as
   `_enforce_tombstone_retention` (`memory_lifecycle.py:825`).
5. **Output**: `$TOMBPRIOR:{Model}:burials` (digest → count) and
   `$TOMBPRIOR:{Model}:index` (digest → last-burial ts), both bounded.

**Write side (consumes the negative evidence):**

1. **Entry point**: a caller does `instance.save()`.
2. **`Model.save()`** (`base.py:1416`): `isinstance(self, WriteFilterMixin)` and
   not `skip_write_filter` → `self._check_write_filter()`.
3. **`_check_write_filter()`**: computes and normalizes `score` exactly as today,
   then calls `self._apply_tombstone_prior(score)`.
4. **`_apply_tombstone_prior()`** — the auto-detect fork:
   - Kill switch on (`POPOTO_TOMBSTONE_PRIOR_DISABLE` truthy) → return `score`
     unchanged, **zero** Redis commands.
   - Model has no `ExistenceFilter` with a `fingerprint_fn` (cached per class) →
     return `score` unchanged, **zero** Redis commands.
   - Otherwise → one `HGET` for the digest's burial count.
5. **Drawdown**: `burials == 0` → `score` unchanged, no telemetry write.
   `burials >= 1` → `penalty = max(FLOOR, DECAY ** burials)`;
   `adjusted = score * penalty`.
6. **Telemetry**: pipelined `HINCRBY penalized 1` + `HINCRBYFLOAT drawdown_total
   (score - adjusted)` on `$TOMBPRIOR:{Model}:stats`, plus a `DEBUG` log line.
7. **Output**: the adjusted score is cached on `self._write_filter_score` and
   returned into the *existing* comparison against `_wf_min_threshold`. If it now
   falls below, the existing `SkipSaveException` drops the write; otherwise the
   record saves with a lower priority-set score. **No new rejection path is
   introduced.**

## Architectural Impact

- **New dependencies**: none beyond the standard library (`hashlib`).
- **Interface changes**: purely additive. New module
  `src/popoto/fields/tombstone_prior.py` (`TombstonePriorStore`); one new private
  method on `WriteFilterMixin`; one new private method on `MemoryLifecycle`. No
  public signature changes; no change to `models/base.py`.
- **Coupling**: `write_filter` gains an import of `tombstone_prior` (field →
  field, same layer, matching `tombstone_store`'s position). `tombstone_prior`
  imports only `redis_db`, `batch`, and `constants` — no model imports, so no
  cycle. The `ExistenceFilter` import inside the auto-detect is function-local,
  mirroring `memory_lifecycle._fingerprint`.
- **Data ownership**: a new keyspace `$TOMBPRIOR:{Model}:*`, deliberately outside
  the model keyspace for the same reason `$TOMB:` is — no query, index scan, or
  key-set walk can surface it. It is **derived state**: it can be dropped
  entirely and the system degrades to today's behavior. `roundtrip_policy =
  "rebuild"` on `WriteFilterMixin` already covers it (nothing to export).
- **Reversibility**: high. Setting `POPOTO_TOMBSTONE_PRIOR_DISABLE=1` restores
  today's behavior at deploy level with no code change; `purge_all()` drops the
  keyspace.

## Appetite

**Size:** Medium

**Team:** Solo dev (Dev lane), plus code-reviewer for the PR gate.

**Interactions:**

- PM check-ins: 1 (the storage/matching fork, resolved in-plan by spikes 1–3;
  escalate only if critique reopens it)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -u redis://localhost:6379/7 PING` | Lane's isolated DB 7 |
| Worktree venv resolves to this checkout | `python -c "import popoto; assert '.worktrees/sdlc-494' in popoto.__file__, popoto.__file__"` | Guards the "wrong package under test" trap |
| Full extras installed | `python -c "import numpy, sentence_transformers, mcp"` | Guards the ~95-test silent deselection |
| #491 landed | `python -c "from popoto.fields.tombstone_store import Tombstone; assert 'fingerprint' in Tombstone.__dataclass_fields__"` | Hard prerequisite: tombstones carry a fingerprint |
