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

Three changes.

**A. `docs/features/confidence-field.md` — stop constructing a client.** Replace
the raw `redis.from_url(...)` with `popoto.get_redis()`. This is not "pin a
database number here too": it removes the database from the example entirely,
because the accessor returns whatever connection popoto is currently bound to.
The snippet becomes correct for every reader rather than only for readers on
DB 0, and there is no number left for a copier to strip back out — which is the
issue's third acceptance criterion satisfied structurally instead of by a
comment.

**B. `docs/configuration.md` — say what a missing database means.** The
hosted-provider example genuinely has no database, because Heroku/Render/Railway
hand out URLs in exactly that shape; rewriting it to `/0` would misrepresent
what those providers give you. So the example stays and gains an inline
annotation, plus one line under the block stating that a URL with no
`/database` selects DB 0. This is the honest fix: the page currently shows the
shape and explains nothing.

**C. A regression guard.** `tests/test_docs_redis_url.py` fails if any
user-facing doc reintroduces a client constructed from a db-less URL.

## Technical Approach

### The accessor is the right instrument (verified)

`popoto.get_redis()` (`src/popoto/__init__.py:118`) is public, documented for
precisely this use ("Use this when you need Redis primitives not exposed by
Popoto models"), and returns the live global. It is **rebind-safe** in a way
`from popoto.redis_db import POPOTO_REDIS_DB` is not: `set_REDIS_DB_settings()`
rebinds the module-level `POPOTO_REDIS_DB` at four sites
(`src/popoto/redis_db.py:413,425,475,480`), so a name imported once goes stale,
while the accessor re-reads it on every call. Documenting the import form would
therefore hand readers a second, subtler bug.

### The db-less URL resolves to DB 0 — but not where you would look

Probed against the lane venv's redis-py, **without issuing a single command**
(constructing a `Connection` does not open a socket, so nothing binds DB 0):

```
'redis://localhost:6379'                        -> connection_kwargs['db'] = None
'redis://localhost:6379/'                       -> connection_kwargs['db'] = None
'redis://default:abc123@some-host.cloud:6379'   -> connection_kwargs['db'] = None
AbstractConnection.__init__ db default = 0   →   effective db = 0
```

Worth recording because it is a trap for the guard test in task 3: a db-less URL
does **not** show up as `db == 0` in `connection_kwargs` — it shows up as *absent*,
and the 0 only materialises from `AbstractConnection`'s signature default. Any
guard that tries to detect the hazard by inspecting a constructed client's
kwargs will read `None`, not `0`. The guard must therefore match on the **URL
string**, which is also the only form that appears in a markdown file.

### Guard scope, and how it avoids being vacuous

PR #643's reviewer found that lane's first guard was **vacuously green** — it
would have passed on a workflow with the setting deleted outright. The same
trap applies here in two ways, and both are designed out rather than hoped away:

1. *Nothing to scan.* If the file glob silently matches zero docs (a rename, a
   directory move), a "no hits" assertion passes. The guard therefore asserts a
   non-empty scanned-file set **and** that the known-good file it was written
   for is in it.
2. *Pattern never matches anything.* A regex that is subtly wrong is
   indistinguishable from a clean tree. The guard therefore carries a **positive
   control**: it runs its own matcher over a synthetic db-less snippet and
   asserts it fires.

Scope: `docs/**/*.md` and `README.md`, **excluding** `docs/plans/` and
`docs/sdlc/` (see No-Gos). Match target: a `from_url(` call whose URL literal is
`redis://…` with no `/<db>` path component. Prose mentions of `localhost:6379`
as a *server address* are not matched and are not the hazard — the guard keys on
the call, not the port.

Stdlib only (`re`, `pathlib`), no `tomllib`, consistent with
`tests/test_ci_workflow_redis_url.py` and with `requires-python = ">=3.10"`.

## Rabbit Holes

- **Auditing every `6379` in the tree.** There are ~40 hits across `docs/`.
  Most are prose ("Redis running on `localhost:6379`") describing where the
  server listens, which is correct and carries no database semantics. Chasing
  them turns a two-file fix into a re-read of the whole doc set.
- **Rewriting `docs/configuration.md`'s connection guidance.** The page
  recommends `/0` in four examples. That is a real tension with #584 (which
  refuses DB 0 for integrations from-env binds) and it deserves an answer — but
  the answer is a maintainer policy decision about what popoto's default
  database should be, not a docs edit. Raised in Open Questions; not touched.
- **Making the guard a general "docs code blocks must be runnable" linter.**
  Tempting adjacency, unbounded scope, and it would fail on dozens of
  intentionally partial snippets.

## No-Gos

- **`docs/plans/**` is not edited.** It is a historical archive. Several plans
  (`ci_local_redis_url_fallback.md`, `tests_yml_dbless_redis_url.md`,
  `adhoc_db0_guard.md`) quote the db-less URL *because it is the defect under
  discussion*; "fixing" those quotations would falsify the record of why the
  decisions were made. This is also why the guard excludes the directory rather
  than the guard being satisfied by an edit sweep.
- **`docs/sdlc/**` is not edited or scanned** — process docs for this repo's own
  pipeline, not user-facing API teaching.
- **No change to popoto's default database or to `DEFAULT_URL`.** Out of scope
  and explicitly rejected once already (`adhoc_db0_guard.md`, option A).
- **No `src/` docstring sweep and no `examples/` sweep.** `examples/` in
  particular cannot have its lockfile regenerated (#611).
- **No change to the `redis` import in unrelated snippets.**

## Test Impact

New file: `tests/test_docs_redis_url.py`. Three tests, no Redis server required
(pure file reads) — so it runs in both CI jobs and adds no DB contention.

No existing test changes. `tests/test_ci_workflow_redis_url.py` (from #639) is
the sibling guard and stays untouched; the two do not overlap — that one reads
`.github/workflows/`, this one reads `docs/`.

## Step by Step Tasks

## Success Criteria

## Verification

## Risks

## Open Questions
