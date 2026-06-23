---
status: docs_complete
type: bug
appetite: Medium
owner: valor
created: 2026-06-23
tracking: https://github.com/tomcounsell/popoto/issues/411
last_comment_id:
revision_applied: true
---

# StreamConsumer XCLAIM Redelivery — Reprocess Crashed-Consumer Entries

## Problem

`StreamConsumer` is Popoto's Redis Streams consumer-group framework. The documented
contract (module docstring, `docs/features/agent-memory.md`, the original design plan
`docs/plans/stream_consumer.md`) is **at-least-once delivery with XCLAIM recovery**: if a
consumer crashes after a batch is delivered but before it XACKs, a surviving consumer in
the same group reclaims those entries and re-runs the handler.

**Current behavior:** The reclaim half is implemented (`_claim_pending()` XCLAIMs idle
pending entries) but the redelivery half is missing. The class's only `XREADGROUP` uses
the special id `>` (`src/popoto/streams/consumer.py:152-158`), which returns *only* entries
never delivered to anyone. Reclaimed entries land in the claiming consumer's PEL, but PEL
entries are only re-read by `XREADGROUP ... 0` — and no such read exists anywhere in the
class. So a claimed entry is **never handed back to the handler**.

Worse, the dead-letter logic punishes the entry for the framework's own inaction. Each
claim cycle re-XCLAIMs the entry (without JUSTID), which increments its `times_delivered`
counter and resets its idle clock. The threshold check at `consumer.py:275`
(`delivery_count > self.max_retries`) therefore trips purely from claim cycles, with **zero
handler executions**, and the entry is dead-lettered with the misleading reason
`"Exceeded max_retries (3)"`. A secondary defect: the dead-letter metadata hardcodes
`failure_count` to `max_retries` (`consumer.py:353`) instead of the actual delivery count —
contradicting the field's own documented meaning ("Number of delivery attempts",
`docs/features/agent-memory.md:1062`).

Net effect, confirmed by the June 2026 audit PoC: a SIGKILLed consumer's 5 delivered-but-
unACKed entries were reprocessed **0 times** and all 5 dead-lettered; 0 duplicates anywhere.
Effective post-crash semantics are **at-most-once** (silent message loss), the opposite of
the documented contract. This is CONC-1, the audit's only critical-severity finding.

**Desired outcome:** Entries pending from a crashed consumer are actually re-delivered to
the handler by a surviving consumer through the normal decode → handler → XACK path.
Dead-lettering happens only after `max_retries` **real handler attempts**. Dead-letter
`failure_count` records the true delivery count. The module docstring and docs state honest
semantics: at-least-once delivery; exactly-once requires handler idempotency.

## Freshness Check

**Baseline commit:** `4fa3e09` (`git rev-parse HEAD` at plan time)
**Issue filed at:** 2026-06-11T05:20:30Z
**Disposition:** Unchanged

**File:line references re-verified against current `main`:**
- `src/popoto/streams/consumer.py:152-158` — only XREADGROUP, uses `{self.stream_key: ">"}` — still holds (verbatim).
- `src/popoto/streams/consumer.py:275` — `if delivery_count > self.max_retries:` dead-letter threshold — still holds.
- `src/popoto/streams/consumer.py:305-311` — `_claim_pending()` XCLAIM (no JUSTID) for reclaim, nothing reads the result — still holds.
- `src/popoto/streams/consumer.py:353` — `dead_entry["failure_count"] = str(self.max_retries)` — still holds.
- `src/popoto/streams/consumer.py:11`, `:237-238` — docstring "reclaims … for reprocessing" claims — still hold.
- `docs/features/agent-memory.md:1067-1069` — "reclaimed via XCLAIM for reprocessing" — still holds.
- `docs/guides/popoto-memory-roadmap.md:681` — "Exactly-once processing via XACK after handler success" — still holds.
- `tests/test_stream_consumer.py` — header lists "XCLAIM recovery" but no test asserts a claimed entry reaches the handler — confirmed (the dead-letter tests at lines 289-376 manually re-XCLAIM to bump the counter; none exercise post-claim reprocessing).

**Cited sibling issues/PRs re-checked:** PR #220 (EventStreamMixin producer side) — merged, unchanged; it is the upstream producer and is not affected by this fix.

**Commits on main since issue was filed (touching referenced files):** none touch `src/popoto/streams/consumer.py`, `tests/test_stream_consumer.py`, or the two referenced docs. (Commits since: #427 PolicyCache, #419 context-assembler, #417 confidence — all unrelated subsystems.)

**Active plans in `docs/plans/` overlapping this area:** `stream_consumer.md` (the original design plan, shipped) — not active; it is the historical spec this fix restores. No active plan overlaps.

**Notes:** All audit line numbers match current source exactly. No drift.

## Prior Art

- **`docs/plans/stream_consumer.md`** (original StreamConsumer design): explicitly scoped at-least-once delivery with XCLAIM recovery, and at `:117` correctly states "Exactly-once semantics — Redis Streams provide at-least-once delivery. Exactly-once requires application-level idempotency, which is the handler's responsibility." The implementation built `_claim_pending()` but never wired the redelivery read. **No prior *fix* attempt exists** — this is the first time the gap is being closed.
- No closed issues / merged PRs found addressing StreamConsumer redelivery (`gh issue list --state closed --search "StreamConsumer redeliver"` / `XCLAIM` returned nothing relevant). This is greenfield-fix territory on a beta subsystem.

## Research

Internal-protocol work (Redis Streams consumer-group commands), but one ecosystem fact is load-bearing and was verified locally rather than from memory:

**Queries / checks used:**
- `redis-py` version + `xautoclaim` availability (sync and async).
- `xclaim` / `xautoclaim` signatures and **return shape**.

**Key findings:**
- Installed `redis-py` is **8.0.0**; both `Redis.xautoclaim` and `redis.asyncio.Redis.xautoclaim` exist. `XAUTOCLAIM` is core since Redis 6.2 / Valkey 7.x — Valkey-safe, no modules. (Verified by introspection, not docs.)
- `XCLAIM` **without** `justid` returns full `[(entry_id, {field: value}), ...]` data — so claimed entries can be fed straight through the existing decode→handler→XACK path without a second read. `XCLAIM ... justid=True` returns ids only and does **not** increment the delivery counter — useful if we need an ownership probe that doesn't inflate retries.
- **`xautoclaim()` on redis-py 8 returns a 3-tuple** `(next_cursor, claimed_messages, deleted_message_ids)` — NOT a 2-tuple. `deleted_message_ids` are entries that have been removed from the stream (XDEL / MAXLEN trim) yet still linger in the PEL; they carry no field data. The builder must unpack all three, iterate only `claimed_messages` through decode→handler, and **XACK** `deleted_message_ids` to evict them from the PEL (never feed them to the handler — they have no fields and would crash decode). This is BLOCKER-3 from critique.
- `xpending_range` already surfaces `times_delivered` per entry (the code reads it at `consumer.py:269`), so the true delivery count for dead-letter metadata is available without extra round-trips.

## Data Flow

Tracing one entry from a crashed consumer to reprocessing (target behavior):

1. **Producer**: `EventStreamMixin.save()` XADDs an entry to `stream:{name}`.
2. **Crash**: consumer A `XREADGROUP >` delivers the entry into A's PEL (`times_delivered=1`), runs handler, is SIGKILLed before XACK. Entry stays in A's PEL.
3. **Reclaim (B)**: consumer B's `process_batch()` calls `_claim_pending()`. `XPENDING` reports the entry idle ≥ `claim_timeout_ms`, `times_delivered ≤ max_retries`. `XCLAIM min_idle_time=claim_timeout_ms` transfers it to B's PEL and returns its full field data.
4. **Redelivery (the missing step)**: B feeds the claimed entry through the same decode path as new entries and invokes `self.handler(...)`.
5. **ACK / retry**: handler succeeds → `XACK` (using the entry's **own reclaimed id list**, separate from the `>` batch) removes it from B's PEL (delivered exactly once net, despite the crash). Handler raises → entry stays pending; a later cycle re-claims and re-attempts, until real attempts exceed `max_retries`.
6. **Dead-letter**: only after `max_retries` real handler attempts, B XADDs to `dead:{stream}` with `failure_count` = actual `times_delivered`, then XACKs the source.

The current code stops the entry's life at step 3 and skips step 4 entirely; step 6 fires prematurely off inflated counts.

## Architectural Impact

- **New dependencies**: none. `XAUTOCLAIM` (if chosen) and `XCLAIM justid` are already in the installed `redis-py` and are core Redis/Valkey commands.
- **Interface changes**: `StreamConsumer.__init__` signature and public method names are unchanged. `process_batch()` return value semantics broaden (reclaimed entries now counted as handled) — this is now an **explicit, pinned contract** (see Technical Approach), not a silent broadening. Acceptable; streams layer is **beta**.
- **Coupling**: unchanged. Self-contained to `consumer.py`.
- **Data ownership**: unchanged.
- **Reversibility**: high — revert the single file plus doc edits.

## Appetite

**Size:** Medium

**Team:** Solo dev, async-specialist (review), validator, documentarian

**Interactions:**
- PM check-ins: 1-2 (confirm the delivery-count gating approach and the XAUTOCLAIM-vs-XCLAIM choice)
- Review rounds: 1 (async/concurrency correctness review)

Medium, not Small: the fix touches async consumer-group recovery logic and requires a
crash-simulation regression test (the current suite has none), which is fiddly to make
deterministic. Not Large: the root cause is precisely located and the producer/happy-path
foundation is correct.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey 6.2+ on localhost:6379 | `python -c "from src.popoto.redis_db import POPOTO_REDIS_DB; v=POPOTO_REDIS_DB.info('server')['redis_version']; assert tuple(int(x) for x in v.split('.')[:2]) >= (6,2), v"` | XAUTOCLAIM support + Streams |
| redis-py has xautoclaim | `python -c "import redis; assert hasattr(redis.Redis(), 'xautoclaim')"` | Reclaim API available |

There is **no** `scripts/check_prerequisites.py` in this repo (`scripts/` contains only
`ci-local.sh`). Run the two checks directly:

```bash
python -c "from src.popoto.redis_db import POPOTO_REDIS_DB; v=POPOTO_REDIS_DB.info('server')['redis_version']; assert tuple(int(x) for x in v.split('.')[:2]) >= (6,2), v; print('OK redis', v)"
python -c "import redis; assert hasattr(redis.Redis(), 'xautoclaim'); print('OK xautoclaim')"
```

Both verified passing at plan-revision time (Redis 8.6.2; redis-py 8.0 exposes `xautoclaim`).

## Solution

### Key Elements

- **PEL redelivery**: after `_claim_pending()` reclaims idle entries, those entries (with
  their full field data, which XCLAIM/XAUTOCLAIM already return) are decoded and passed
  through the *same* handler → XACK helper used for new `>` entries. No claimed entry is left
  unprocessed — **and each reclaimed entry is XACKed via its own id list** so it cannot remain
  permanently pending (see Technical Approach, BLOCKER-1).
- **Real-attempt gating**: the dead-letter threshold counts **handler invocations**, not
  claim cycles, with the comparison and ordering pinned numerically (BLOCKER-2). A reclaimed
  entry is dead-lettered only when its real delivery count has exceeded `max_retries`.
- **Honest dead-letter metadata**: `failure_count` is set to the entry's actual
  `times_delivered` (available from `xpending_range`/claim response), not the `max_retries`
  constant.
- **Honest documentation**: module docstring, `docs/features/agent-memory.md`, and
  `docs/guides/popoto-memory-roadmap.md` state at-least-once delivery and the
  handler-idempotency requirement; the roadmap "Exactly-once" line (`:681`) is corrected; the
  module docstring's "Redis Commands Used" list is updated to match the commands actually
  issued.

### Flow

New entries:
`process_batch()` → `_claim_pending()` (reclaim + redeliver pending, capped per cycle) → `XREADGROUP >` (new entries) → decode → `handler()` → `XACK`

Crashed-consumer entry:
A delivered + SIGKILLed (no XACK) → entry in A's PEL → B `_claim_pending()` reclaims (≤ max_retries real attempts) → B decode → `handler()` → success `XACK` (via reclaimed-id list; net exactly-once) **or** raise → stays pending → re-claimed next cycle → after max_retries real attempts → `dead:{stream}` with true `failure_count` → `XACK` source.

### Technical Approach

High-level direction; the builder picks the exact mechanism within these constraints.

- **Redelivery mechanism (decision point — see Open Questions):** prefer consolidating the
  reclaim-and-redeliver into a single **`XAUTOCLAIM`**-based pass (Redis 6.2+/Valkey 7+,
  confirmed available). `XAUTOCLAIM` atomically scans for entries idle ≥ `claim_timeout_ms`,
  transfers them to this consumer, and returns their full field data — so one call replaces
  the current `XPENDING` + per-entry `XCLAIM` loop and yields the entries ready to decode and
  hand to the handler. Alternative if XAUTOCLAIM proves awkward: keep `XPENDING` + `XCLAIM`
  (no JUSTID), but feed the returned field data through the handler path instead of discarding
  it. Either way, the reclaimed entries MUST flow through decode → `handler()` → `XACK`.

- **XAUTOCLAIM 3-tuple unpacking (BLOCKER-3 fix — redis-py 8):** `xautoclaim(...)` returns
  **three** elements: `(next_cursor, claimed_messages, deleted_message_ids)`. Unpack all
  three. Iterate only `claimed_messages` for decode→handler→XACK. **Never** feed
  `deleted_message_ids` to decode/handler (they have no field data and would crash) — instead
  `XACK` them immediately to clear them from the PEL. If a fallback to older redis-py (2-tuple)
  is ever needed, code defensively, but the pinned target is the 3-tuple.

- **Reclaimed-entry XACK is SEPARATE from the `>` batch (BLOCKER-1 fix):** the existing `>`
  path builds its `entry_ids` list **only** from the `>` read. Routing reclaimed entries
  through the shared decode helper does NOT get them ACKed by that list — they would stay
  permanently pending and be reclaimed every cycle forever (an infinite reprocessing storm —
  the silent-loss bug inverted). The builder MUST keep a **distinct id list for reclaimed
  entries** and call `xack(stream_key, group_name, *reclaimed_ids)` for them separately, after
  a successful redelivery, independent of the `>` batch ACK. Preserve the **original returned
  id (bytes)** for the XACK even when the field payload is decoded bytes→str for the handler —
  do not ACK with a re-encoded/decoded id. A successful redelivery must remove the entry from
  XPENDING; tests assert the entry is **gone from XPENDING**, not merely that the handler ran.

- **Retry gating — pin the semantics numerically (BLOCKER-2 fix):** the server's
  `times_delivered` starts at **1** on first delivery and is server-incremented on each
  (non-JUSTID) claim. Do NOT leave the gate as a vague "real attempts" notion — pin it one of
  two ways and document the choice in code:
  - **Preferred — explicit handler-attempt counter:** increment a counter exactly once per
    `await self.handler(...)` redelivery invocation, and gate on that counter. This decouples
    the dead-letter decision from server-side claim bookkeeping and is immune to the original
    off-by-one.
  - **Alternative — pin `times_delivered`:** evaluate the gate **before** running the handler
    as `delivery_count > max_retries`, where `delivery_count` is the post-claim
    `times_delivered`, and document that `max_retries` counts **deliveries including the
    original**. The check ordering (gate-then-handle vs handle-then-gate) MUST be fixed and
    stated in code; ambiguous ordering is what produced the original off-by-one.
  Whichever is chosen, the contract is asserted numerically in tests:
  `handler.call_count == max_retries` before the entry appears in `dead:{stream_key}`, and
  dead-letter `failure_count == str(<actual delivery_count>)`, **parametrized over multiple
  `max_retries` values** (not just the default 3). Ensure the previous failure mode —
  incrementing the counter via claim cycles that never call the handler — cannot recur (no
  JUSTID-less re-claim that bumps the count without a handler call).

- **Dead-letter on real exhaustion:** when a reclaimed entry's delivery count exceeds the
  gate, dead-letter it with `failure_count = <actual times_delivered>` and the existing
  metadata fields; then XACK the source.

- **Per-cycle cap + monitoring (CONCERN-storm fix):** do NOT drain the whole PEL inline in one
  cycle ahead of the `>` read — a large PEL or a poison-pill entry would starve new-message
  progress and amplify load during degradation. Pass `count=batch_size` to `xautoclaim` and
  advance its cursor **across cycles** rather than looping until the PEL is empty within a
  single cycle. Emit a log line (and/or counter) per cycle reporting entries reclaimed and
  entries dead-lettered, so redelivery/dead-letter volume is observable during an incident.

- **`process_batch()` return contract (CONCERN-return-count fix):** routing reclaimed entries
  through the handler broadens the return count from "new entries" to "new + reclaimed".
  **Pin the contract explicitly** in build-core: either return total handled, or return a
  `(new, reclaimed)` breakdown — pick one, document it in the method docstring and in
  `agent-memory.md`, and add a build-tests assertion on the return value of a reclaim-cycle
  test so the meaning is locked.

- **Decode + handler + XACK reuse (NIT fix):** factor the bytes→str decode block (currently
  inline at `consumer.py:168-179`) plus the handler-invoke and XACK into **one** shared helper
  used by both the `>` read and the reclaim path. This avoids a wide `_claim_pending()`
  god-method (reclaim + retry-gate + decode + handler + dead-letter + redelivery under a broad
  try/except) and keeps the two paths from drifting. The helper takes the entry's own id list
  for its XACK so the reclaim path and `>` path each ACK their own ids.

- **Error isolation:** keep `_claim_pending()`'s broad `try/except` (`consumer.py:318-324`)
  from silently swallowing handler exceptions during redelivery. The redelivery handler
  invocation MUST live **outside** that `except Exception`. A handler raising during
  redelivery must leave the entry pending (for a future retry), not be caught-and-dropped as a
  "claim error" (see Failure Path Test Strategy).

- **Valkey parity:** core Streams commands only; no modules.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `_claim_pending()` currently wraps everything in `except Exception` (`consumer.py:318-324`) and only `logger.warning`s. After adding redelivery, a handler exception during redelivery must NOT be swallowed there — add a test asserting that when the redelivery handler raises, the entry remains pending (not ACKed, not dead-lettered) and is retried on a later cycle. The redelivery handler invocation lives **outside** the broad catch; document the choice in code.
- [ ] `_dead_letter()`'s `except Exception` (`consumer.py:378-384`) logs an error and leaves the entry pending on XADD failure — add/confirm a test that a dead-letter XADD failure does not XACK the source (no silent loss).

### Empty/Invalid Input Handling
- [ ] No pending entries → reclaim path is a no-op and `process_batch()` proceeds to the `>` read unchanged (existing `pending_count == 0` early return at `consumer.py:251`). Add an assertion that an empty PEL produces zero extra handler calls.
- [ ] Claimed entry with empty/odd field data decodes without error (reuse the shared decode helper; covered by decode-reuse refactor).
- [ ] `deleted_message_ids` from XAUTOCLAIM (entries trimmed/deleted but still in PEL) are XACKed and never fed to the handler — add an assertion that they produce zero handler calls and are gone from XPENDING.

### Error State Rendering
- Not user-facing UI. Observable failure surfaces are: the dead-letter stream contents and `logger` output. Tests assert on dead-letter stream entries and (where relevant) on the absence of premature dead-lettering.

## Test Impact

- [ ] `tests/test_stream_consumer.py::TestDeadLetter::test_entries_exceeding_max_retries_are_dead_lettered` — UPDATE: this test manually re-XCLAIMs to inflate `times_delivered` *without* handler runs, then asserts dead-lettering. Under the fix, claim cycles must no longer inflate the count, so this exact manipulation may no longer trip the threshold. Rewrite it to drive dead-lettering via repeated **real handler failures** (handler always raises), asserting exactly `max_retries` handler invocations precede the dead-letter entry.
- [ ] `tests/test_stream_consumer.py::TestDeadLetter::test_dead_letter_max_length` — UPDATE: same pattern (manual re-XCLAIM bump); adjust to produce dead-letters via real failures while keeping the MAXLEN assertion.
- [ ] `tests/test_stream_consumer.py` header comment (lines 1-13) — UPDATE: the "XCLAIM recovery" line currently overstates coverage; align with the new redelivery tests.
- [ ] All other tests (group creation, batch processing, handler-exception-leaves-pending, shutdown, synergy, multi-consumer, import) are unaffected — they exercise the `>` happy path which is unchanged.

## Rabbit Holes

- **Poison-message batch blast radius** — one failing entry dead-lettering its innocent
  batch-mates is a *separate* batch-granularity concern explicitly dropped in the issue's
  recon. Do not try to solve per-entry batch isolation here.
- **Rewriting the sync/async connection plumbing** (`_with_fresh_connection`) — the
  per-`asyncio.run` connection reset is unrelated; leave it.
- **A general retry/backoff policy framework** — the fix is "redeliver and count real
  attempts", not a configurable retry-strategy engine.
- **Multiprocessing test infra perfection** — a deterministic in-process crash simulation
  (read+don't-ACK, advance idle clock) is acceptable and far cheaper than a real SIGKILL
  multiprocess harness; the PoC's multiprocess form is a *bonus*, not a requirement (issue
  says "multi-process or simulated-crash variant acceptable").

## Risks

### Risk 1: XAUTOCLAIM cursor / partial-scan behavior differs subtly across Redis vs Valkey
**Impact:** Reclaim could miss or re-scan entries, causing under- or over-delivery.
**Mitigation:** Use only documented core XAUTOCLAIM semantics (cursor `0-0` start, bounded `count`); advance the cursor across cycles; add a test that all pending entries from a crash are eventually reclaimed across multiple cycles. CI runs the real Valkey job as the final word.

### Risk 2: Off-by-one in real-attempt counting reintroduces premature or never dead-lettering
**Impact:** Either crashed entries loop forever (never dead-lettered) or are dead-lettered one attempt early/late. This is the **same defect class as the original bug** — two critics independently flagged it (BLOCKER-2).
**Mitigation:** Pin the gate semantics and comparison/ordering numerically in code (explicit handler-attempt counter preferred). A dedicated **parametrized** test asserts the *exact* number of handler invocations before dead-letter equals `max_retries` across multiple `max_retries` values; another asserts an entry that succeeds on re-delivery is never dead-lettered; a third asserts `failure_count == str(actual delivery_count)`.

### Risk 3: Redelivery handler exception swallowed by `_claim_pending`'s broad catch
**Impact:** Silent loss returns through a different door — exceptions during redelivery get logged-and-dropped, the entry ACKed or stranded.
**Mitigation:** Failure-path test (above) asserting a raising redelivery handler leaves the entry pending and retriable; place redelivery handler invocation outside the swallowing catch (`consumer.py:318-324`).

### Risk 4: Reclaimed entries never XACKed → infinite reprocessing storm (BLOCKER-1)
**Impact:** Routing reclaimed entries through the shared helper without a separate XACK leaves them permanently pending; every cycle re-reclaims and re-runs them — the silent-loss bug inverted into a runaway-load bug.
**Mitigation:** Maintain a distinct reclaimed-id list and XACK it separately after successful redelivery, preserving the original bytes id. Per-cycle `count=batch_size` cap bounds the blast radius even if a regression slips in. Test asserts the entry is gone from XPENDING after a successful redelivery (recurrence guard), not merely that the handler ran.

## Race Conditions

### Race 1: Two surviving consumers reclaim the same pending entry
**Location:** `_claim_pending()` reclaim call (`consumer.py:305-311`, or its XAUTOCLAIM replacement).
**Trigger:** Two consumers in the group run `_claim_pending()` concurrently against the same idle entry.
**Data prerequisite:** Entry idle ≥ `claim_timeout_ms` in the original (crashed) consumer's PEL.
**State prerequisite:** Both consumers see it via XPENDING/XAUTOCLAIM scan.
**Mitigation:** XCLAIM/XAUTOCLAIM are atomic — exactly one consumer wins ownership; the loser gets an empty result and processes nothing. This is the same guarantee the original design relied on (`stream_consumer.md` Race 2). Re-confirm with a two-consumer reclaim test asserting the entry is handled exactly once.

### Race 2: Entry reclaimed mid-flight by B while A (slow, not actually dead) is still in its handler
**Location:** redelivery path.
**Trigger:** A is merely slow (idle > timeout) rather than crashed; B reclaims and reprocesses while A also eventually finishes.
**Data prerequisite:** A's handler has side effects.
**State prerequisite:** `claim_timeout_ms` shorter than A's real handler duration.
**Mitigation:** This is the inherent at-least-once duplicate window; the fix's contract is at-least-once + handler idempotency, which the documentation will now state honestly. No code mitigation beyond accurate docs and a sane default `claim_timeout_ms` (3 min, unchanged).

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #411] Poison-message batch blast radius (one failing entry dead-letters its batch-mates) — explicitly dropped in the issue recon as a separate batch-granularity concern; to be filed separately if pursued. (Tracked-for-now under this issue's "Dropped" bucket; not addressed by this plan.)
- Nothing else deferred — the redelivery fix, retry-count correction, failure_count fix, and documentation corrections are all in scope for this plan.

> Note: the poison-message item does not yet have its own issue number. If the validator
> requires a filed `[SEPARATE-SLUG #NNN]`, file a one-line follow-up issue during build and
> update this tag; otherwise it remains a documented out-of-scope note carried from the
> issue's own "Dropped" section.

## Update System

No update system changes required — this is a purely internal library fix in `src/popoto/streams/consumer.py` plus documentation. No new dependencies or config to propagate.

## Agent Integration

No agent integration required — `StreamConsumer` is a Popoto library class consumed directly by application/background-worker code, not exposed via MCP. The agent-memory mutation stream consumes it internally; no `.mcp.json` or bridge change.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/agent-memory.md` "XCLAIM recovery" section (lines ~1067-1069): state that reclaimed entries are re-delivered to the handler (not just "reclaimed"), and state at-least-once delivery + handler-idempotency requirement. Confirm the `failure_count` row (`:1062`, "Number of delivery attempts") now matches the code. Note the `process_batch()` return-count meaning change (new + reclaimed).
- [ ] Update `docs/guides/popoto-memory-roadmap.md:681`: correct "Exactly-once processing via XACK after handler success" to "At-least-once processing; exactly-once requires idempotent handlers." Review `:682`/`:696` reliability claims for consistency.

### External Documentation Site
- [ ] `mkdocs build --strict` passes (run via `scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Module docstring (`consumer.py:1-37`) and `_claim_pending` docstring (`:232-239`): describe the redelivery step and honest at-least-once semantics; drop the implication that XCLAIM alone achieves reprocessing.
- [ ] **Module docstring "Redis Commands Used" list (CONCERN-doc-drift fix):** if the builder adopts XAUTOCLAIM, ADD `XAUTOCLAIM` to the command list at `consumer.py:1-37` (it currently names XCLAIM only). The docstring must name exactly the commands the code issues — the precise drift this plan exists to fix.

## Success Criteria

- [ ] Kill-consumer test: a consumer's delivered-but-unACKed entries (simulated crash or multiprocess SIGKILL) are re-executed by a surviving consumer for **all** of those entries within its claim cycles, and each is **gone from XPENDING** afterward (separate-XACK / recurrence guard, BLOCKER-1).
- [ ] Dead-lettering occurs only after `max_retries` **handler invocations**: a **parametrized** test (multiple `max_retries` values) with an always-raising handler shows exactly `max_retries` handler calls before the entry appears in `dead:{stream_key}`; a test where the handler succeeds on redelivery shows the entry processed, not dead-lettered (BLOCKER-2).
- [ ] Dead-letter metadata `failure_count` equals the actual delivery count (asserted as `str(actual delivery_count)`), not the `max_retries` constant.
- [ ] `process_batch()` return-count contract is pinned (total handled, or `(new, reclaimed)`) and a reclaim-cycle test asserts the returned value.
- [ ] XAUTOCLAIM `deleted_message_ids` are XACKed and never reach the handler (if XAUTOCLAIM is used).
- [ ] Regression test derived from the issue PoC added to `tests/test_stream_consumer.py` (multi-process or simulated-crash variant), using a tiny `claim_timeout_ms` so `min_idle_time` (server-evaluated, unmockable by freezegun) is exceeded deterministically.
- [ ] Module docstring and docs state at-least-once semantics and handler-idempotency; the roadmap "exactly-once" claim is corrected; the module docstring command list names exactly the commands issued.
- [ ] No Redis-module commands introduced (Valkey-compatible); full suite green on Redis.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

The lead agent orchestrates; it does not build directly.

### Team Members

- **Builder (consumer-core)**
  - Name: consumer-builder
  - Role: Implement redelivery of reclaimed entries through the shared decode→handler→XACK helper (separate reclaimed-id XACK), real-attempt retry gating, per-cycle cap + monitoring, return-count contract, and the `failure_count` fix in `consumer.py`.
  - Agent Type: async-specialist
  - Resume: true

- **Builder (tests)**
  - Name: consumer-test-builder
  - Role: Write the crash-simulation regression test + parametrized dead-letter-after-real-attempts tests + deleted-ids test + return-count test; update the two existing dead-letter tests.
  - Agent Type: test-engineer
  - Resume: true

- **Validator (consumer)**
  - Name: consumer-validator
  - Role: Verify all success criteria, run full suite on Redis, confirm no Redis-module commands, confirm every Redis command named in the module docstring is actually called by the code.
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: consumer-docs
  - Role: Update module docstring (incl. command list), agent-memory.md, roadmap.md; confirm mkdocs strict build.
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Implement redelivery + retry gating + failure_count fix
- **Task ID**: build-core
- **Depends On**: none
- **Validates**: tests/test_stream_consumer.py
- **Informed By**: Research (XAUTOCLAIM available in redis-py 8.0, returns a **3-tuple** with `deleted_message_ids`; XCLAIM justid suppresses counter)
- **Assigned To**: consumer-builder
- **Agent Type**: async-specialist
- **Parallel**: false
- Factor the bytes→str decode block (`consumer.py:168-179`) **plus** handler-invoke + XACK into a single shared helper used by both the `>` read and the reclaim path; the helper takes the entry's own id list for its XACK (NIT — avoids a god-method).
- Replace/augment `_claim_pending()` so reclaimed entries are decoded and passed through the shared helper; prefer a single XAUTOCLAIM pass (idle ≥ `claim_timeout_ms`) over XPENDING+XCLAIM, but either is acceptable provided reclaimed entries reach the handler.
- **BLOCKER-1:** keep a distinct reclaimed-id list and XACK it separately from the `>` batch after successful redelivery, preserving the original returned bytes id. A successful redelivery must remove the entry from XPENDING.
- **BLOCKER-3:** if using XAUTOCLAIM, unpack the 3-tuple `(next_cursor, claimed_messages, deleted_message_ids)`; iterate only `claimed_messages`; XACK `deleted_message_ids` without handing them to the handler.
- **BLOCKER-2:** gate dead-lettering on real handler attempts via an explicit handler-attempt counter (preferred) or a pinned `delivery_count > max_retries` gate evaluated before the handler, with the ordering documented in code. No counter inflation from claim cycles that never call the handler.
- **CONCERN-storm:** pass `count=batch_size` to xautoclaim and advance the cursor across cycles (do not drain the whole PEL in one cycle); emit a per-cycle log/metric of reclaimed + dead-lettered counts.
- **CONCERN-return-count:** pin `process_batch()`'s return contract (total handled, or `(new, reclaimed)`) and document it in the docstring.
- Set `failure_count` to the actual delivery count (`consumer.py:353`).
- Ensure a redelivery handler exception leaves the entry pending — redelivery handler invocation lives **outside** the broad `_claim_pending` `except Exception` (`consumer.py:318-324`).
- Keep core Streams commands only (Valkey-safe).

### 2. Tests: crash redelivery + real-attempt dead-letter + failure_count
- **Task ID**: build-tests
- **Depends On**: build-core
- **Validates**: tests/test_stream_consumer.py
- **Assigned To**: consumer-test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Add a crash-simulation test using a **tiny `claim_timeout_ms`** (e.g. 0–50ms, since `min_idle_time` is server-evaluated and unmockable by freezegun): deliver a batch to one consumer, do not XACK, let the idle clock exceed the timeout, run a surviving consumer, assert all crash-batch entries reach its handler, are ACKed, and are **gone from XPENDING** (recurrence guard, BLOCKER-1). A multiprocess SIGKILL variant adapted from the PoC is welcome but a deterministic in-process simulation is acceptable.
- Add a **parametrized** test (over multiple `max_retries` values) asserting exactly `max_retries` handler invocations precede a dead-letter entry (always-raising handler) — BLOCKER-2.
- Add a test asserting an entry that succeeds on redelivery is never dead-lettered.
- Add a test asserting dead-letter `failure_count == str(actual delivery_count)`.
- Add a failure-path test: a raising redelivery handler leaves the entry pending and retriable (not swallowed/ACKed).
- Add a test (if XAUTOCLAIM used) asserting `deleted_message_ids` are XACKed and produce zero handler calls.
- Add a test asserting the `process_batch()` return value matches the pinned contract on a reclaim cycle.
- UPDATE the two existing dead-letter tests to drive dead-lettering via real handler failures rather than manual XCLAIM count inflation; update the file header comment.

### 3. Validate core + tests
- **Task ID**: validate-core
- **Depends On**: build-core, build-tests
- **Assigned To**: consumer-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_stream_consumer.py -q` and the full suite.
- Confirm no Redis-module commands introduced (`grep -nE 'BF\.|CMS\.|TS\.|JSON\.|FT\.' src/popoto/streams/consumer.py` → none).
- **CONCERN-doc-drift:** confirm every Redis command named in the module docstring "Redis Commands Used" list is actually issued by the code (and vice-versa) — flag if XAUTOCLAIM is used but not listed.
- Verify each Success Criterion mechanically; report pass/fail.

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-core
- **Assigned To**: consumer-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Update module docstring + `_claim_pending` docstring for honest redelivery semantics; **update the "Redis Commands Used" list to include XAUTOCLAIM if adopted** (CONCERN-doc-drift).
- Update `docs/features/agent-memory.md` XCLAIM-recovery section, confirm `failure_count` row matches code, and note the `process_batch()` return-count meaning change.
- Correct `docs/guides/popoto-memory-roadmap.md:681` exactly-once claim.
- Run `scripts/ci-local.sh docs` (`mkdocs build --strict`).

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-core, build-tests, validate-core, document-feature
- **Assigned To**: consumer-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full suite + docs strict build.
- Verify every Success Criterion incl. documentation and the docstring-command-list match.
- Generate final report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Stream consumer tests pass | `pytest tests/test_stream_consumer.py -q` | exit code 0 |
| Full suite passes | `pytest -q` | exit code 0 |
| No Redis modules introduced | `grep -nE 'BF\.\|CMS\.\|TS\.\|JSON\.\|FT\.' src/popoto/streams/consumer.py` | exit code 1 |
| Redelivery test present | `grep -nE 'def test_.*(crash\|reclaim\|redeliver)' tests/test_stream_consumer.py` | output contains test |
| Reclaimed entry gone from XPENDING asserted | `grep -nE 'xpending' tests/test_stream_consumer.py` | output contains a post-redelivery XPENDING assertion |
| Docstring command list matches code | docstring "Redis Commands Used" lists exactly the commands issued (manual/validate-core) | match |
| Docs build strict | `mkdocs build --strict` | exit code 0 |
| No "Exactly-once" roadmap claim | `grep -n 'Exactly-once processing via XACK' docs/guides/popoto-memory-roadmap.md` | exit code 1 |

## Critique Results

<!-- Populated by /do-plan-critique (war room) 2026-06-23. Verdict: NEEDS REVISION (2 blockers). Revision applied 2026-06-23. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Risk & Robustness | Reclaimed entries handled but never XACKed: the `>` path builds `entry_ids` only from the `>` batch, so routing reclaimed entries through the shared decode helper without their own XACK leaves them permanently pending → infinite reprocessing storm (silent-loss inverted). | RESOLVED — Technical Approach "Reclaimed-entry XACK is SEPARATE" bullet; build-core task; Risk 4; Success Criteria + Verification XPENDING assertion. | Keep two id lists: decode bytes→str for the handler payload but retain the original returned id (bytes) for `xack`. After a successful redelivery `xack(stream_key, group_name, *reclaimed_ids)` separately from the `>` batch. Assert the entry is gone from XPENDING after success, not merely that the handler ran. |
| BLOCKER | Risk & Robustness + History & Consistency | Retry gate conflates server `times_delivered` (starts at 1 on first delivery, server-incremented per claim) with handler-attempt count; gate `times_delivered > max_retries` neither guarantees "exactly max_retries real handler runs" nor is the run-vs-dead-letter ordering pinned. Same off-by-one class as the original bug. Two critics independently flagged. | RESOLVED — Technical Approach "Retry gating — pin the semantics numerically" bullet; build-core + build-tests tasks; Risk 2; parametrized Success Criterion. | Track real handler attempts explicitly (counter incremented once per `await self.handler(...)` redelivery), OR pin the gate as "dead-letter when delivery_count > max_retries, evaluated BEFORE running the handler" and document that max_retries counts deliveries incl. the original. Assert numerically: `handler.call_count == max_retries` AND dead-letter `failure_count == str(actual delivery_count)`, parametrized on max_retries. |
| CONCERN | Risk & Robustness | Test determinism + XAUTOCLAIM deleted-ids: `min_idle_time` is server-evaluated (unmockable by freezegun); kill-consumer test can't advance the 3-min idle clock. XAUTOCLAIM returns a 3rd `deleted_message_ids` element (redis-py 8) the plan never handles — feeding those to decode→handler crashes on absent fields. | RESOLVED — Research key findings; Technical Approach "XAUTOCLAIM 3-tuple unpacking" bullet; build-tests tiny-`claim_timeout_ms` + deleted-ids tests; Empty/Invalid Input bullet. | `xautoclaim(...)` returns `(next_cursor, claimed_messages, deleted_message_ids)`; unpack all three, iterate only `claimed_messages`, and XACK `deleted_message_ids` to clear them from PEL. Test fixtures must set a tiny `claim_timeout_ms` (e.g. 0–50ms) since `min_idle_time` is server-evaluated. |
| CONCERN | Risk & Robustness | Reprocessing storm / no cap or monitoring: once redelivery runs handlers inline before the `>` read, a large PEL or poison-pill entry blocks new-message progress and amplifies load during degradation; no metric for redelivery/dead-letter volume. | RESOLVED — Technical Approach "Per-cycle cap + monitoring" bullet; build-core task. | Pass `count=batch_size` to `xautoclaim` and iterate the cursor across cycles rather than draining the whole PEL in one cycle. Emit a log/metric for entries reclaimed + dead-lettered per cycle. Keep redelivery handler invocation outside the `except Exception` at consumer.py:318-324. |
| CONCERN | History & Consistency | XAUTOCLAIM doc drift: module docstring "Redis Commands Used" (consumer.py:1-37) lists XCLAIM and omits XAUTOCLAIM; the Documentation task never updates the command list. If the builder picks XAUTOCLAIM the docs name a command the code no longer issues — the exact drift this plan exists to fix. | RESOLVED — Documentation "Redis Commands Used" bullet; document-feature task; validate-core docstring-command-list check; Verification row. | Add a Documentation bullet: if XAUTOCLAIM is chosen, add it to the consumer.py:1-37 command list and reword agent-memory ~1067-1069 / `_claim_pending` docstring. Add a validate-core check that every Redis command named in the docstring is actually called by the code. |
| CONCERN | Scope & Value | `process_batch()` return count silently broadens from "new entries" to "new + reclaimed"; callers using it as a drain signal/throughput metric get a different number after crash recovery. Success criteria are all-technical with no validation of this changed semantic. | RESOLVED — Technical Approach "`process_batch()` return contract" bullet; build-core + build-tests + document-feature tasks; Success Criterion + Architectural Impact note. | Pin the return contract in build-core (total handled, or a (new, reclaimed) breakdown); add a build-tests assertion on the return value of a reclaim-cycle test; note the count-meaning change in the docstring and agent-memory.md. |
| NIT | Scope & Value | Folding handler-invoke + XACK into `_claim_pending()` makes it a wide method (reclaim + retry-gate + decode + handler + dead-letter + redelivery) under a broad try/except. | RESOLVED — Technical Approach "Decode + handler + XACK reuse" bullet; build-core task. | Route reclaimed entries through the same decode→handler→XACK helper used by `process_batch()` so both paths share one code path. |
| NIT | History & Consistency | Recurrence guard: success criteria assert the happy redelivery path but not that the reclaim result is non-empty/consumed, so a future refactor could silently drop the read again (the original defect). | RESOLVED — Success Criteria "gone from XPENDING" + handler-call-count guard; Verification XPENDING row; build-tests crash-sim test. | Add an assertion that the reclaimed batch is actually iterated (e.g. handler call count equals the number of crashed entries). |
| STRUCTURAL | Automated structural check | Prerequisites line referenced `scripts/check_prerequisites.py` which does not exist (only `scripts/ci-local.sh` exists), though both underlying prerequisite checks pass when run directly. | RESOLVED — Prerequisites section now runs the two checks directly; bogus script reference removed; both verified passing (Redis 8.6.2, redis-py 8.0). | — |

---

<!-- Open Questions resolved during revision; recommendations adopted as the plan's pinned approach. The builder retains the documented XAUTOCLAIM-vs-XCLAIM fallback latitude. -->
