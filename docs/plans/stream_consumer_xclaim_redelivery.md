---
status: Ready
type: bug
appetite: Medium
owner: valor
created: 2026-06-23
tracking: https://github.com/tomcounsell/popoto/issues/411
last_comment_id:
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
- `xclaim` / `xautoclaim` signatures.

**Key findings:**
- Installed `redis-py` is **8.0.0**; both `Redis.xautoclaim` and `redis.asyncio.Redis.xautoclaim` exist. `XAUTOCLAIM` is core since Redis 6.2 / Valkey 7.x — Valkey-safe, no modules. (Verified by introspection, not docs.)
- `XCLAIM` **without** `justid` returns full `[(entry_id, {field: value}), ...]` data — so claimed entries can be fed straight through the existing decode→handler→XACK path without a second read. `XCLAIM ... justid=True` returns ids only and does **not** increment the delivery counter — useful if we need an ownership probe that doesn't inflate retries.
- `xpending_range` already surfaces `times_delivered` per entry (the code reads it at `consumer.py:269`), so the true delivery count for dead-letter metadata is available without extra round-trips.

## Data Flow

Tracing one entry from a crashed consumer to reprocessing (target behavior):

1. **Producer**: `EventStreamMixin.save()` XADDs an entry to `stream:{name}`.
2. **Crash**: consumer A `XREADGROUP >` delivers the entry into A's PEL (`times_delivered=1`), runs handler, is SIGKILLed before XACK. Entry stays in A's PEL.
3. **Reclaim (B)**: consumer B's `process_batch()` calls `_claim_pending()`. `XPENDING` reports the entry idle ≥ `claim_timeout_ms`, `times_delivered ≤ max_retries`. `XCLAIM min_idle_time=claim_timeout_ms` transfers it to B's PEL and returns its full field data.
4. **Redelivery (the missing step)**: B feeds the claimed entry through the same decode path as new entries and invokes `self.handler(...)`.
5. **ACK / retry**: handler succeeds → `XACK` removes it from B's PEL (delivered exactly once net, despite the crash). Handler raises → entry stays pending; a later cycle re-claims and re-attempts, until real attempts exceed `max_retries`.
6. **Dead-letter**: only after `max_retries` real handler attempts, B XADDs to `dead:{stream}` with `failure_count` = actual `times_delivered`, then XACKs the source.

The current code stops the entry's life at step 3 and skips step 4 entirely; step 6 fires prematurely off inflated counts.

## Architectural Impact

- **New dependencies**: none. `XAUTOCLAIM` (if chosen) and `XCLAIM justid` are already in the installed `redis-py` and are core Redis/Valkey commands.
- **Interface changes**: `StreamConsumer.__init__` signature and public method names are unchanged. `process_batch()` return value semantics may broaden (reclaimed entries now counted as processed) — acceptable, streams layer is **beta**.
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

Run all checks: `python scripts/check_prerequisites.py docs/plans/stream_consumer_xclaim_redelivery.md`

## Solution

### Key Elements

- **PEL redelivery**: after `_claim_pending()` reclaims idle entries, those entries (with
  their full field data, which XCLAIM/XAUTOCLAIM already return) are decoded and passed
  through the *same* handler → XACK path used for new `>` entries. No claimed entry is left
  unprocessed.
- **Real-attempt gating**: the dead-letter threshold counts **handler invocations**, not
  claim cycles. A reclaimed entry is dead-lettered only when its real delivery count has
  exceeded `max_retries`. Ownership probes that must not inflate the count use
  `XCLAIM ... JUSTID` (or XAUTOCLAIM, which transfers + returns data in one call so the
  increment corresponds to an actual processing attempt).
- **Honest dead-letter metadata**: `failure_count` is set to the entry's actual
  `times_delivered` (available from `xpending_range`/claim response), not the `max_retries`
  constant.
- **Honest documentation**: module docstring, `docs/features/agent-memory.md`, and
  `docs/guides/popoto-memory-roadmap.md` state at-least-once delivery and the
  handler-idempotency requirement; the roadmap "Exactly-once" line (`:681`) is corrected.

### Flow

New entries:
`process_batch()` → `_claim_pending()` (reclaim + redeliver pending) → `XREADGROUP >` (new entries) → decode → `handler()` → `XACK`

Crashed-consumer entry:
A delivered + SIGKILLed (no XACK) → entry in A's PEL → B `_claim_pending()` reclaims (≤ max_retries real attempts) → B decode → `handler()` → success `XACK` (net exactly-once) **or** raise → stays pending → re-claimed next cycle → after max_retries real attempts → `dead:{stream}` with true `failure_count` → `XACK` source.

### Technical Approach

High-level direction; the builder picks the exact mechanism within these constraints.

- **Redelivery mechanism (decision point — see Open Questions):** prefer consolidating the
  reclaim-and-redeliver into a single **`XAUTOCLAIM`**-based pass (Redis 6.2+/Valkey 7+,
  confirmed available). `XAUTOCLAIM` atomically scans for entries idle ≥ `claim_timeout_ms`,
  transfers them to this consumer, and returns their full field data — so one call replaces
  the current `XPENDING` + per-entry `XCLAIM` loop and yields the entries ready to decode and
  hand to the handler. Each XAUTOCLAIM transfer then corresponds to exactly one real
  processing attempt, which is precisely the semantics the retry counter should track.
  Alternative if XAUTOCLAIM proves awkward: keep `XPENDING` + `XCLAIM` (no JUSTID), but feed
  the returned field data through the handler path instead of discarding it. Either way, the
  reclaimed entries MUST flow through decode → `handler()` → `XACK`.
- **Retry gating:** compute the dead-letter decision against the count of **real handler
  attempts**. With XAUTOCLAIM-then-process, each cycle that hands an entry to the handler is
  one real attempt, so comparing `times_delivered` (post-claim) to `max_retries` becomes
  meaningful again. Ensure the previous failure mode — incrementing the counter via claim
  cycles that never call the handler — cannot recur (no JUSTID-less re-claim that bumps the
  count without a handler call).
- **Dead-letter on real exhaustion:** when a reclaimed entry's real delivery count exceeds
  `max_retries`, dead-letter it with `failure_count = <actual times_delivered>` and the
  existing metadata fields; then XACK the source.
- **Decode reuse:** factor the bytes→str decode block (currently inline at
  `consumer.py:168-179`) so both the `>` read and the reclaim path share it — avoids drift
  between the two decode sites.
- **Error isolation:** keep `_claim_pending()`'s broad `try/except` from silently swallowing
  handler exceptions during redelivery. A handler raising during redelivery must leave the
  entry pending (for a future retry), not be caught-and-dropped as a "claim error". This is a
  real trap given the current `except Exception` wrapping the whole claim routine (see Failure
  Path Test Strategy).
- **Valkey parity:** core Streams commands only; no modules.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `_claim_pending()` currently wraps everything in `except Exception` (`consumer.py:318-324`) and only `logger.warning`s. After adding redelivery inside this method (or a new method), a handler exception during redelivery must NOT be swallowed there — add a test asserting that when the redelivery handler raises, the entry remains pending (not ACKed, not dead-lettered) and is retried on a later cycle. Decide deliberately whether redelivery lives inside or outside the broad catch; document the choice in code.
- [ ] `_dead_letter()`'s `except Exception` (`consumer.py:378-384`) logs an error and leaves the entry pending on XADD failure — add/confirm a test that a dead-letter XADD failure does not XACK the source (no silent loss).

### Empty/Invalid Input Handling
- [ ] No pending entries → reclaim path is a no-op and `process_batch()` proceeds to the `>` read unchanged (existing `pending_count == 0` early return at `consumer.py:251`). Add an assertion that an empty PEL produces zero extra handler calls.
- [ ] Claimed entry with empty/odd field data decodes without error (reuse the shared decode helper; covered by decode-reuse refactor).

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
**Mitigation:** Use only documented core XAUTOCLAIM semantics (cursor `0-0` start, bounded `count`); add a test that all pending entries from a crash are eventually reclaimed across multiple cycles. CI runs the real Valkey job as the final word.

### Risk 2: Off-by-one in real-attempt counting reintroduces premature or never dead-lettering
**Impact:** Either crashed entries loop forever (never dead-lettered) or are dead-lettered one attempt early/late.
**Mitigation:** A dedicated test asserts the *exact* number of handler invocations before dead-letter equals `max_retries`; another asserts an entry that succeeds on re-delivery is never dead-lettered.

### Risk 3: Redelivery handler exception swallowed by `_claim_pending`'s broad catch
**Impact:** Silent loss returns through a different door — exceptions during redelivery get logged-and-dropped, the entry ACKed or stranded.
**Mitigation:** Failure-path test (above) asserting a raising redelivery handler leaves the entry pending and retriable; place redelivery handler invocation outside the swallowing catch.

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
- [ ] Update `docs/features/agent-memory.md` "XCLAIM recovery" section (lines ~1067-1069): state that reclaimed entries are re-delivered to the handler (not just "reclaimed"), and state at-least-once delivery + handler-idempotency requirement. Confirm the `failure_count` row (`:1062`, "Number of delivery attempts") now matches the code.
- [ ] Update `docs/guides/popoto-memory-roadmap.md:681`: correct "Exactly-once processing via XACK after handler success" to "At-least-once processing; exactly-once requires idempotent handlers." Review `:682`/`:696` reliability claims for consistency.

### External Documentation Site
- [ ] `mkdocs build --strict` passes (run via `scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Module docstring (`consumer.py:1-37`) and `_claim_pending` docstring (`:232-239`): describe the redelivery step and honest at-least-once semantics; drop the implication that XCLAIM alone achieves reprocessing.

## Success Criteria

- [ ] Kill-consumer test: a consumer's delivered-but-unACKed entries (simulated crash or multiprocess SIGKILL) are re-executed by a surviving consumer for **all** of those entries within its claim cycles.
- [ ] Dead-lettering occurs only after `max_retries` **handler invocations**: a test with an always-raising handler shows exactly `max_retries` handler calls before the entry appears in `dead:{stream_key}`; a test where the handler succeeds on redelivery shows the entry processed, not dead-lettered.
- [ ] Dead-letter metadata `failure_count` equals the actual delivery count, not the `max_retries` constant.
- [ ] Regression test derived from the issue PoC added to `tests/test_stream_consumer.py` (multi-process or simulated-crash variant).
- [ ] Module docstring and docs state at-least-once semantics and handler-idempotency; the roadmap "exactly-once" claim is corrected.
- [ ] No Redis-module commands introduced (Valkey-compatible); full suite green on Redis.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

The lead agent orchestrates; it does not build directly.

### Team Members

- **Builder (consumer-core)**
  - Name: consumer-builder
  - Role: Implement redelivery of reclaimed entries through the handler path, real-attempt retry gating, and the `failure_count` fix in `consumer.py`.
  - Agent Type: async-specialist
  - Resume: true

- **Builder (tests)**
  - Name: consumer-test-builder
  - Role: Write the crash-simulation regression test + dead-letter-after-real-attempts tests; update the two existing dead-letter tests.
  - Agent Type: test-engineer
  - Resume: true

- **Validator (consumer)**
  - Name: consumer-validator
  - Role: Verify all success criteria, run full suite on Redis, confirm no Redis-module commands.
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: consumer-docs
  - Role: Update module docstring, agent-memory.md, roadmap.md; confirm mkdocs strict build.
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Implement redelivery + retry gating + failure_count fix
- **Task ID**: build-core
- **Depends On**: none
- **Validates**: tests/test_stream_consumer.py
- **Informed By**: Research (XAUTOCLAIM available in redis-py 8.0, returns full data; XCLAIM justid suppresses counter)
- **Assigned To**: consumer-builder
- **Agent Type**: async-specialist
- **Parallel**: false
- Factor the bytes→str decode block (`consumer.py:168-179`) into a shared helper used by both the `>` read and the reclaim path.
- Replace/augment `_claim_pending()` so reclaimed entries are decoded and passed through `handler()` → `XACK`; prefer a single XAUTOCLAIM pass (idle ≥ `claim_timeout_ms`) over XPENDING+XCLAIM, but either is acceptable provided reclaimed entries reach the handler.
- Gate dead-lettering on real handler attempts (no counter inflation from claim cycles that never call the handler); dead-letter only when actual `times_delivered` exceeds `max_retries`.
- Set `failure_count` to the actual delivery count (`consumer.py:353`).
- Ensure a redelivery handler exception leaves the entry pending (not swallowed by the broad `_claim_pending` catch).
- Keep core Streams commands only (Valkey-safe).

### 2. Tests: crash redelivery + real-attempt dead-letter + failure_count
- **Task ID**: build-tests
- **Depends On**: build-core
- **Validates**: tests/test_stream_consumer.py
- **Assigned To**: consumer-test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Add a crash-simulation test: deliver a batch to one consumer, do not XACK, advance idle past `claim_timeout_ms`, run a surviving consumer, assert all crash-batch entries reach its handler and are ACKed (a multiprocess SIGKILL variant adapted from the PoC is welcome but a deterministic in-process simulation is acceptable).
- Add a test asserting exactly `max_retries` handler invocations precede a dead-letter entry (always-raising handler).
- Add a test asserting an entry that succeeds on redelivery is never dead-lettered.
- Add a test asserting dead-letter `failure_count` equals the real delivery count.
- Add a failure-path test: a raising redelivery handler leaves the entry pending and retriable (not swallowed/ACKed).
- UPDATE the two existing dead-letter tests to drive dead-lettering via real handler failures rather than manual XCLAIM count inflation; update the file header comment.

### 3. Validate core + tests
- **Task ID**: validate-core
- **Depends On**: build-core, build-tests
- **Assigned To**: consumer-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_stream_consumer.py -q` and the full suite.
- Confirm no Redis-module commands introduced (`grep -nE 'BF\.|CMS\.|TS\.|JSON\.|FT\.' src/popoto/streams/consumer.py` → none).
- Verify each Success Criterion mechanically; report pass/fail.

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-core
- **Assigned To**: consumer-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Update module docstring + `_claim_pending` docstring for honest redelivery semantics.
- Update `docs/features/agent-memory.md` XCLAIM-recovery section and confirm `failure_count` row matches code.
- Correct `docs/guides/popoto-memory-roadmap.md:681` exactly-once claim.
- Run `scripts/ci-local.sh docs` (`mkdocs build --strict`).

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: build-core, build-tests, validate-core, document-feature
- **Assigned To**: consumer-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full suite + docs strict build.
- Verify every Success Criterion incl. documentation.
- Generate final report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Stream consumer tests pass | `pytest tests/test_stream_consumer.py -q` | exit code 0 |
| Full suite passes | `pytest -q` | exit code 0 |
| No Redis modules introduced | `grep -nE 'BF\.\|CMS\.\|TS\.\|JSON\.\|FT\.' src/popoto/streams/consumer.py` | exit code 1 |
| Redelivery test present | `grep -nE 'def test_.*(crash\|reclaim\|redeliver)' tests/test_stream_consumer.py` | output contains test |
| Docs build strict | `mkdocs build --strict` | exit code 0 |
| No "Exactly-once" roadmap claim | `grep -n 'Exactly-once processing via XACK' docs/guides/popoto-memory-roadmap.md` | exit code 1 |

## Critique Results

Critique run 2026-06-23 (SDLC pipeline, in-session war-room — 3 critic lenses: concurrency-correctness, acceptance-completeness, adversarial-risk). Plan judged fundamentally sound; findings are build-steering refinements, no re-plan blockers.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| Major | concurrency | Dead-letter gating needs `times_delivered`, which `xpending_range` returns (consumer.py:269) but XAUTOCLAIM does **not** return; XAUTOCLAIM's 3-tuple `(cursor, entries, deleted_ids)` arity is a runtime-shape risk on a beta-but-shipped class. | Resolves Open Q1 → use XPENDING+XCLAIM | Keep the existing XPENDING-driven loop; **add the handler call** in the reclaim branch. XCLAIM (no JUSTID) returns full field data, so no second read needed. |
| Major | concurrency | Off-by-one in retry gating: must yield **exactly `max_retries`** real handler calls before dead-letter. | build-core + build-tests | Read `delivery_count` from `xpending_range`; check the dead-letter threshold **before** invoking the handler; pin the exact operator with a count-asserting test written first (TDD). |
| Major | risk | The broad `except Exception` in `_claim_pending` (consumer.py:318) would swallow a redelivery handler exception as a "claim error" and, because the per-entry loop is inside the try, block reclaim of batch-mates for that cycle. | build-core | Per-entry error isolation: a raising redelivery handler leaves **that** entry pending/retriable and does not abort the loop or get logged-and-dropped. Failure-path test required (already in plan). |
| Major | risk | Infinite-redelivery risk if the reclaim that feeds the handler uses JUSTID (counter never rises → never dead-letters). | build-core | The redelivery reclaim **must increment** `times_delivered` (no JUSTID). JUSTID only for pure ownership probes never followed by a handler call. Guarded by the always-raising dead-letter test. |
| Minor | risk | `failure_count` must use the `delivery_count` observed from `xpending_range`, not a value re-read after the fetch-XCLAIM (which would itself be inflated). | build-core | Capture `delivery_count` from the pending-range entry and pass it through to `_dead_letter` (consumer.py:353). |
| Minor | risk | Shared decode helper must tolerate `(id, None)` entries (an entry XDEL'd from the stream but still in a PEL is returned with `None` fields by XCLAIM/XAUTOCLAIM). | build-core | Guard the decode helper against `None`/empty field data; skip or dead-letter rather than crash. |
| Minor | completeness | The two existing dead-letter tests do not strictly break **if** the builder checks the threshold before the handler call (past-threshold entries dead-letter without a handler call). They would break under a handler-before-threshold ordering. | build-tests | Rewrite both to drive dead-lettering via **real handler failures** (the plan's instinct is correct and safe regardless of ordering). |

---

## Open Questions

_All three resolved during the 2026-06-23 critique (see Critique Results)._

1. **Redelivery mechanism:** ✅ RESOLVED → **XPENDING+XCLAIM**, not XAUTOCLAIM. The dead-letter gating requires `times_delivered` (available from `xpending_range`, already read at consumer.py:269; XAUTOCLAIM does not return it), and XAUTOCLAIM's version-dependent 3-tuple arity is an unnecessary runtime risk on a beta-but-shipped class. The minimal, lower-risk fix keeps the existing XPENDING loop and adds the handler→XACK call in the reclaim branch (XCLAIM without JUSTID already returns full field data).
2. **Poison-message follow-up:** ✅ RESOLVED → file a one-line follow-up issue during build so the No-Go tag becomes a real `[SEPARATE-SLUG #NNN]` (low cost, keeps the out-of-scope boundary auditable).
3. **Crash test form:** ✅ RESOLVED → **deterministic in-process simulation** (deliver to a "crashed" consumer via raw XREADGROUP, run the StreamConsumer with `claim_timeout_ms=0`), matching the existing dead-letter tests' pattern. The multiprocess SIGKILL PoC is a welcome bonus but not required (issue says "multi-process or simulated-crash variant acceptable").
