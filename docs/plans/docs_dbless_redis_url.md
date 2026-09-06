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

### Task 1 — `docs/features/confidence-field.md`: use popoto's own connection
**Depends on:** none

- In the "Inspecting Companion Hash Keys" snippet (lines ~159-184), drop
  `import redis` and replace `r = redis.from_url("redis://localhost:6379")` with
  `r = popoto.get_redis()`.
- Add `import popoto` to the snippet's imports if the existing `from popoto
  import …` line does not already make the module available (it does not — check
  and add).
- Add one short line of prose after the snippet: reading the companion hash
  through `get_redis()` guarantees the same database popoto wrote it to.
- **Validation:** `grep -n 'from_url' docs/features/confidence-field.md` → no
  output.

### Task 2 — `docs/configuration.md`: state what a missing database means
**Depends on:** none

- Annotate the hosted-provider example at line 46 inline (it keeps its db-less
  URL — that is what providers issue).
- Add one line beneath the examples block: a URL with no `/database` component
  selects database 0.
- Update the env-var table row at line ~403 so "Falls back to localhost:6379"
  names database 0.
- **Validation:** `grep -n 'database 0' docs/configuration.md` → at least one
  hit in the URL-format section.

### Task 3 — `tests/test_docs_redis_url.py`: regression guard
**Depends on:** Tasks 1, 2

- `test_no_user_facing_doc_constructs_a_dbless_client` — scan the doc set,
  assert zero db-less `from_url(` calls.
- `test_the_guard_scans_a_nonempty_doc_set` — assert the scanned set is
  non-empty **and** contains `docs/features/confidence-field.md` and
  `docs/configuration.md`. Kills vacuity mode 1.
- `test_the_matcher_detects_a_dbless_url` — run the matcher over a synthetic
  db-less snippet and assert it fires; run it over `redis://h:6379/15` and
  assert it does not. Kills vacuity mode 2.
- **Validation:** the three commands in the Verification table.

### Task 4 — `CLAUDE.md`: record the third remedy
**Depends on:** Tasks 1-3

- The file already explains why `tests.yml` and `ci-local.sh` took opposite
  remedies (#639). Add one sentence for the docs case — the accessor removes the
  database rather than naming it — so the three do not later read as
  inconsistent.
- **Validation:** `grep -n 'get_redis' CLAUDE.md` → one hit.

## Success Criteria

- `grep -rn 'from_url("redis://localhost:6379")' docs/` returns nothing outside
  `docs/plans/` (issue acceptance 1).
- The rest of the user-facing doc set is swept, with the boundary between
  "hazard" and "prose" written down rather than left to the next reader (issue
  acceptance 2).
- The confidence-field example explains why it uses popoto's connection, so a
  reader has a reason not to substitute a raw client back in (issue acceptance 3).
- A reader of `docs/configuration.md` can learn what a db-less URL does without
  leaving the page.
- The guard fails if any of the above is undone, and is demonstrably not
  vacuous.
- Both CI jobs stay green.

## Verification

Run from the lane worktree with `POPOTO_TEST_DB=6`.

| Check | Command | Expected |
|---|---|---|
| No db-less client in user-facing docs | `grep -rn 'from_url("redis://[^"]*:6379")' docs/ README.md \| grep -v '^docs/plans/'` | exit code 1 |
| Confidence-field example uses the accessor | `grep -c 'popoto.get_redis()' docs/features/confidence-field.md` | output contains 1 |
| Configuration explains the missing database | `grep -ci 'database 0' docs/configuration.md` | non-zero |
| Guard passes | `POPOTO_TEST_DB=6 .venv/bin/pytest tests/test_docs_redis_url.py -q` | 3 passed |
| Guard is not vacuous — it catches a reintroduction | `printf 'r = redis.from_url("redis://localhost:6379")\n' >> docs/features/confidence-field.md; POPOTO_TEST_DB=6 .venv/bin/pytest tests/test_docs_redis_url.py -q; git checkout docs/features/confidence-field.md` | the run **fails** before the checkout restores the file |
| Sibling guard still green | `POPOTO_TEST_DB=6 .venv/bin/pytest tests/test_ci_workflow_redis_url.py -q` | 3 passed |
| Docs build | `.venv/bin/mkdocs build --strict 2>&1 \| tail -3` | no error |

The mutation row is the one that matters: a guard that has never been observed
failing has not been observed at all.

## Risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | The guard is vacuous and nobody notices for months (the #643 lesson). | Two dedicated anti-vacuity tests plus the mutation row above, which must be run and its failure observed. |
| 2 | `get_redis()` is not importable the way the snippet writes it, so the example is broken in a new way. | Execute the edited snippet end-to-end against DB 6 before the PR opens — not merely eyeball it. |
| 3 | The exclusion of `docs/plans/` is read later as an oversight and someone "fixes" the archive. | The exclusion is stated in No-Gos *and* encoded in the guard with a comment giving the reason. |
| 4 | Scope creep into `docs/configuration.md`'s `/0` recommendation. | Named as a rabbit hole and raised as an Open Question instead of being acted on. |
| 5 | Lane runs on the shared main checkout for the plan doc while other lanes are active. | Plan committed after each section (already done); code work stays in `.worktrees/sdlc-645`. |

## Open Questions

1. **`docs/configuration.md` recommends `redis://localhost:6379/0` in four
   examples, while #584 makes `MemoryConfig.from_env` *refuse* DB 0.** A reader
   who follows the configuration page and then uses the memory integrations hits
   `Db0RefusedError`. That is a genuine documentation/behaviour contradiction,
   but resolving it means deciding what popoto's recommended default database
   *is* — a maintainer call with release-note consequences, and one that
   `adhoc_db0_guard.md` already declined once. **This plan does not touch it.**
   Flagging for a separate issue if wanted.
