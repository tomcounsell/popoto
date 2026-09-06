---
status: Planning
type: bug
appetite: Small
tracking: https://github.com/tomcounsell/popoto/issues/645
last_comment_id:
---

# Docs teach a db-less `redis.from_url` in example code

## Problem

`docs/features/confidence-field.md:180` hands a reader this, inside a runnable
example:

```python
r = redis.from_url("redis://localhost:6379")
raw_data = r.hgetall(hash_key)
```

The URL names no database. Two things follow, and only the first is what the
issue reported.

**1. It teaches the db-less shape.** Every other layer of this codebase now
refuses that shape or pins around it: `Db0RefusedError` for from-env integration
binds (#584), `Db0FlushRefusedError` for flushes on popoto's own client (#577),
an explicit `/15` in CI (#639), and no `REDIS_URL` export at all in the local
gate script (#635). The docs are the last place still modelling it, and they are
the one place a reader copies from.

**2. The example is also wrong on its own terms.** It prints a companion hash
key that popoto wrote through *popoto's* connection, then opens a **second,
unrelated** client to read it back. Those are the same database only when the
reader's popoto happens to be bound to DB 0. Anyone with `REDIS_URL=…/1`, anyone
running under the pytest plugin, anyone who has called
`set_REDIS_DB_settings()` — all of them get `{}` from this snippet and no
indication why. The db-less URL is not merely unsafe here; it is the reason the
example silently does not work.

A smaller instance sits in `docs/configuration.md:46`, where the hosted-provider
example (`redis://default:abc123@some-host.cloud:6379`) carries no database and
nothing on the page says what that means.

## Appetite

**Small.** Two documentation edits and one regression guard. The judgement in
this plan is about *where the boundary is* — which of the 40-odd `6379` hits in
`docs/` are the hazard and which are prose or historical record — not about the
edits themselves.

## Freshness Check

**Disposition: Unchanged.** Baseline `7bd18de9` (`origin/main`).

The formal skip condition applies (issue filed 2026-09-06T11:08:22Z, plan
started 11:12Z, **zero** commits on main in between — `git log --since` is
empty), but the cited reference was verified directly rather than assumed:

- `docs/features/confidence-field.md:180` — read; still reads
  `r = redis.from_url("redis://localhost:6379")`. Confirmed.
- Sibling issues cited in the issue body: #635 CLOSED 10:19:35Z, #639 CLOSED
  11:07:17Z, both today; #584 and #577 closed earlier. Each resolution was read
  — all four moved *tooling and library code* off the db-less/DB-0 shape and
  none of them touched `docs/features/` or `docs/configuration.md`, so none
  pre-empts this work.
- No active plan in `docs/plans/` overlaps: the two adjacent plans
  (`ci_local_redis_url_fallback.md`, `tests_yml_dbless_redis_url.md`) are both
  shipped, and both explicitly recorded a **no-sweep no-go** that deferred
  exactly this docs work.

## Research

No external research. Every claim this plan rests on is a property of code in
this repo or of the pinned redis-py in the lane venv, and each was verified
locally — which is stronger evidence than a search result. See Technical
Approach for the two probes and their output.

## Prior Art

| Ref | Relevance |
|---|---|
| #635 / PR #637 | `scripts/ci-local.sh` exported a db-less `REDIS_URL` fallback. Remedy: **delete the export**, let the pytest plugin be the sole binder. Rejected pinning `/15` because it would silently contradict a lane's `POPOTO_TEST_DB`. |
| #639 / PR #643 | `tests.yml` set a db-less `REDIS_URL` in both jobs. Remedy: the **opposite** — pin `redis://localhost:6379/15`, because deleting it falls through to `DEFAULT_URL` (`/0`) and no workflow sets `POPOTO_TEST_DB`, so there is no override to contradict. Added `tests/test_ci_workflow_redis_url.py`. |
| #584 / PR #601 | `MemoryConfig.from_env` refuses a db-less URL (`_no_db_message`) and refuses DB 0. This is the layer that makes the documented shape actively fail for integrations users. |
| #577 / PR #623 | `Db0FlushRefusedError` — popoto's own client refuses a destructive flush of DB 0. |
| #596 / PR #603 | Paid for the passes-locally/fails-in-CI defect a db-less URL produces. Cited here because it is the concrete cost of the shape, not a hypothetical. |

**Why the two prior remedies differ, and why this one differs again:** each lane
picked the fix that removes the ambiguity *at that layer*. `ci-local.sh` had a
binder that should not have existed (delete it). `tests.yml` had a binder that
had to exist (name the database). This case has a third shape: the snippet
should not be constructing a client **at all**, because popoto already owns one.
The pattern across all three is "make the database unambiguous," not "always
pin" or "always delete."

## Solution

## Technical Approach

## Rabbit Holes

## No-Gos

## Test Impact

## Step by Step Tasks

## Success Criteria

## Verification

## Risks

## Open Questions
