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

## Solution

### Key Elements

- **`TombstonePriorStore`** (`src/popoto/fields/tombstone_prior.py`, new) — owns
  the `$TOMBPRIOR:{Model}:*` keyspace: how many times a given content
  fingerprint has been buried, when it was last buried, and how much drawdown
  has been applied. Bounded by LRU-by-last-burial. Sibling of `TombstoneStore`,
  same conventions.
- **Burial recording** (`MemoryLifecycle.tombstone()` + a new
  `_content_fingerprint()`) — every forgetting increments the buried
  fingerprint's counter. Records with no content fingerprint are skipped, not
  keyed by redis_key.
- **Write-time drawdown** (`WriteFilterMixin._apply_tombstone_prior()`) — a
  multiplicative, escalating penalty on the write-filter score, applied inside
  the existing `_check_write_filter()` before the existing threshold comparison.
- **Telemetry** (`$TOMBPRIOR:{Model}:stats` + `TombstonePriorStore.stats()`) —
  count of penalized writes and total score drawdown, readable at any time.
- **Auto-detect + deploy kill switch** — on by default for any model with a
  content fingerprint; `POPOTO_TOMBSTONE_PRIOR_DISABLE` turns it off without a
  code change.

### Flow

Record is repeatedly dismissed → `MemoryLifecycle.tick()` forgets it →
`tombstone()` archives it **and** increments its fingerprint's burial count →
*(later, new session)* the same content arrives → `save()` →
`_check_write_filter()` computes the caller's score → `_apply_tombstone_prior()`
finds 1 prior burial → score halved, drawdown counted → still above threshold,
so it saves (with lower priority) → it is dismissed and forgotten again → burial
count 2 → next occurrence's score is quartered → falls under
`WF_MIN_THRESHOLD` → **existing** `SkipSaveException` drops the write silently,
and the telemetry counter records that it did.

### Technical Approach

**Storage — the core design question the issue left open.**

Rejected: a second Bloom filter. It cannot hold a per-entry burial count (needed
for escalation), its `delete` is a documented no-op so it cannot be bounded, and
the existing token-OR matching makes it a false-positive machine (spike-1).

Chosen: a **bounded hash + ZSET pair**, exactly mirroring `TombstoneStore`'s
data/index shape:

```
$TOMBPRIOR:{Model}:burials  — HASH: fingerprint digest -> burial count (int)
$TOMBPRIOR:{Model}:index    — ZSET: fingerprint digest -> last-burial timestamp
$TOMBPRIOR:{Model}:stats    — HASH: {penalized: int, drawdown_total: float}
```

The cost the issue flagged ("a bounded hash/ZSET can, at higher cost") is one
`HGET` per save on fingerprinted models and one pipelined `HINCRBY`+`ZADD` per
burial. Burials are rare (a `tick()` sweep, not a per-write path), so the write
path pays a single O(1) hash read. That is the right trade for storing the
weight the escalation criterion requires.

**Bound.** `Defaults.TOMBSTONE_PRIOR_LIMIT = 1000`, matching
`LIFECYCLE_TOMBSTONE_RETENTION_LIMIT` — the prior can never track more
fingerprints than there are retained tombstones, so the two bounds move
together. Enforcement is LRU-by-last-burial: on each `record_burial`, if
`ZCARD > limit`, `ZRANGE 0 excess-1` the oldest and pipeline `HDEL` + `ZREM`.
Copied from `_enforce_tombstone_retention` (`memory_lifecycle.py:825`), which
already has this exact shape and error posture.

**Matching — exact, normalized fingerprint digest.**

```python
digest = hashlib.blake2b(fingerprint.strip().lower().encode("utf-8"),
                         digest_size=16).hexdigest()
```

Deterministic; no similarity, no embedding, no retrieval at write time. Two
records collide only if their fingerprint strings are equal after
whitespace-strip and case-fold, or at a 2^-128 hash collision. This makes the
"a dissimilar record must not be penalized" criterion an exact property rather
than a tuned threshold. The digest (not the raw fingerprint) is the field name,
so the keyspace holds no record content — the fingerprint may be user text, and
`$TOMB:` already archives content only under an explicitly bounded, deliberately
out-of-model keyspace.

Near-duplicate / paraphrase matching is the issue's own "staged approach" second
half and is explicitly **out of scope** (see No-Gos): it needs a retrieval at
write time and a false-positive policy that this plan's exact matcher does not.

**Response strength — multiplicative and escalating, never an outright reject.**

```python
penalty = max(Defaults.TOMBSTONE_PRIOR_FLOOR,
              Defaults.TOMBSTONE_PRIOR_DECAY ** burials)
adjusted = score * penalty
```

with `TOMBSTONE_PRIOR_DECAY = 0.5` and `TOMBSTONE_PRIOR_FLOOR = 0.05`. One
burial halves the score, two quarter it, and the floor stops the sequence at 5%
so a genuinely important record buried many times for situational reasons is
suppressed but never mathematically annihilated. The escalation criterion is met
and its shape is explicit.

Crucially the drawdown **does not introduce a rejection path**. It only feeds the
existing `score < self._wf_min_threshold` comparison. This composes with
per-model `_wf_min_threshold` overrides, with `apply_overrides()` sweeps, and
with `Model.save(skip_write_filter=True)` — all of which keep working unchanged.

**Auto-detect, default-ON, deploy kill switch.**

The read is gated on a per-class cached predicate: does this model define an
`ExistenceFilter` field with a non-`None` `fingerprint_fn`? A model without one
has no content identity, so there is nothing to match and the consult is skipped
before any Redis command is issued. This is what makes "deployments not using
tombstones see byte-identical write behavior" structurally true rather than
flag-dependent — and it is auto-detect, not opt-in, so the subconscious-operation
constraint holds.

The cache is `type(self).__dict__`-scoped (a private `_wf_tombstone_fp_field`
attribute set on the class on first access) so subclasses resolve independently
and a class whose fields never change pays the field scan once.

The deploy-level switch is `POPOTO_TOMBSTONE_PRIOR_DISABLE`, read **at call
time** via `_read_tombstone_prior_switch()` in `constants.py` — a module-level
function, not a `Defaults` class attribute, for exactly the reason
`_read_decode_quarantine_switch` documents at `constants.py:63-85`: a class-body
read binds at import and makes a deploy-time flip (or a `monkeypatch.setenv`) a
no-op. Phrased as a `_DISABLE` so unset means ON, per the default-on doctrine.

**Constants are pinned in-repo, never constructor kwargs** (CLAUDE.md "Key
Patterns"). Three new entries in `Defaults`, in a
`# -- Tombstone negative prior (fields/tombstone_prior.py, issue #494)`
block with the rationale for each value inline:

| Constant | Value | Rationale |
|---|---|---|
| `TOMBSTONE_PRIOR_LIMIT` | `1000` | Matches `LIFECYCLE_TOMBSTONE_RETENTION_LIMIT`; the prior can never usefully track more fingerprints than there are retained tombstones. |
| `TOMBSTONE_PRIOR_DECAY` | `0.5` | One burial halves the score. Chosen so a single burial is recoverable (0.5 clears the 0.1 `WF_MIN_THRESHOLD` for any score ≥ 0.2) and the third burial is not. |
| `TOMBSTONE_PRIOR_FLOOR` | `0.05` | Suppression asymptote. Below the 0.1 min threshold, so a heavily-buried pattern is reliably dropped, but non-zero so the value is still observable and a raised per-model threshold is still what decides. |

**Redis client shape.** All new code uses `get_REDIS_DB()`, never
`from ..redis_db import POPOTO_REDIS_DB` (#655, and the reason #673 existed).
As part of this change, `write_filter.py`'s two existing `POPOTO_REDIS_DB` call
sites (`_tag_priority`, `_delete_write_filter_keys`) are converted to
`get_REDIS_DB()` — otherwise this one file mixes both shapes, and a spy test
written against the module attribute would capture nothing (the vacuity trap
this repo has hit repeatedly). This is a two-line behavior-preserving conversion
in a file this plan already modifies, not scope creep.

**Interaction provenance.** `InteractionWeight` is not touched. The drawdown is
a multiplier applied *after* `compute_filter_score()`, which is where a model
factors human-vs-agent source weighting in. A human-sourced record therefore
enters the multiplier with a higher score and survives the same number of
burials longer than an agent-sourced one — the existing weighting is preserved by
construction rather than re-implemented.

## Failure Path Test Strategy

### Exception Handling Coverage

`TombstonePriorStore` follows `TombstoneStore`/`MemoryLifecycle`'s established
posture: **the negative prior must never break a save.** Every Redis call on the
write path is wrapped and, on failure, degrades to "no penalty" — a memory
system whose write path dies because a telemetry hash is unreachable is worse
than one that occasionally misses a drawdown.

- [ ] `_apply_tombstone_prior` on a Redis error → logs a `warning` and returns the
      **unmodified** score. Test asserts both: the save succeeds *and*
      `caplog` captured the warning. No bare `except Exception: pass` is
      introduced — every handler logs.
- [ ] `record_burial` on a Redis error → logs a `warning`; the tombstone itself
      is still written (burial recording is best-effort and must not roll back
      an archive). Test asserts the tombstone exists after a failing prior write.
- [ ] Bound-enforcement sweep failure → logged and swallowed, matching
      `_enforce_tombstone_retention` (`memory_lifecycle.py:842`).

### Empty/Invalid Input Handling

- [ ] `fingerprint_fn` returning `""` or whitespace-only → normalizes to an empty
      string; `record_burial`/`consult` treat it as **no fingerprint** and skip
      (no digest of the empty string entering the keyspace). Tested both sides.
- [ ] `fingerprint_fn` returning `None` → `_compute_fingerprint_impl` already
      raises `ValueError` (`existence_filter.py:296-299`); the auto-detect wraps
      it and treats it as "no fingerprint", returning the score unchanged.
- [ ] A corrupt burial count (non-integer bytes in the hash) → coerced to `0`
      (no penalty) with a warning, never an exception into `save()`.
- [ ] `compute_filter_score()` returning `None`/non-numeric → already floored to
      `0.0` by existing code *before* the multiplier; `0.0 * penalty == 0.0`, so
      behavior is unchanged. Asserted so a later refactor cannot reorder it.

### Error State Rendering

No user-visible UI. The observable surface is the telemetry hash and the log
line; both are asserted directly (`stats()` returns the incremented counters; a
`DEBUG` record names the model, digest, burial count, and penalty).

## Test Impact

All new tests live in a **new** file, `tests/test_tombstone_prior.py`, to avoid
colliding with the active `forget_guard_test_vacuity.md` lane (#674) in
`tests/test_memory_lifecycle.py`.

- [ ] `tests/test_write_filter.py` — **UPDATE (verify-only expected).** The
      existing suite uses models with no `ExistenceFilter`, so the auto-detect
      short-circuits and behavior is unchanged. Run it to prove the
      "byte-identical for non-adopters" criterion against real existing tests
      rather than only against a new one.
- [ ] `tests/test_memory_lifecycle.py` — **UPDATE, minimal.** `tombstone()` gains
      a burial-recording side effect. Existing tombstone tests must still pass
      untouched; if any asserts an exact Redis command count on the burial path,
      it is updated with a comment naming this issue. No restructuring.
- [ ] `tests/test_tombstone_prior.py` — **CREATE.** Full coverage below.

**Non-vacuity protocol (mandatory, per lane brief).** For every test in the new
file, prove it can fail: delete or invert the specific behavior the test names,
confirm the test FAILS, restore, confirm it PASSES. The resulting
test → mutation → observed-failure table goes in the PR body. Two specific traps
this repo has been burned by, and how each is avoided here:

1. **The spy-on-a-stale-global trap.** No test spies on
   `popoto.redis_db.POPOTO_REDIS_DB`. All assertions read real Redis state
   through `get_REDIS_DB()` on the lane's DB 7, or spy on
   `TombstonePriorStore` methods directly.
2. **The structurally-unreachable-guard trap.** Every test that exercises the
   drawdown asserts a *changed* value (a specific expected score, a specific
   counter delta) rather than merely "no exception raised", so a guard that never
   executes fails the assertion instead of passing vacuously. Any test that
   depends on a burial existing first asserts the burial count *before* asserting
   the drawdown, so a filtered-out or never-written precondition surfaces as a
   failure at that line rather than as a silently-skipped guard.
