---
status: Ready
type: bug
appetite: Small
owner: valorengels
created: 2026-10-09
tracking: https://github.com/tomcounsell/popoto/issues/837
last_comment_id:
revision_applied: true
revision_applied_at: 2026-10-09T08:12:45Z
---

# popoto-transfer DB-0 fence arms regardless of startup binding

## Problem

`popoto-transfer` refuses to read from or write to Redis database 0 unless
`--allow-db0` is passed (#833 / #834). The refusal is enforced by `_Db0Fence`
in `src/popoto/transfer/cli.py`, which patches `get_connection` on every
redis-py pool class so that a checkout to the fenced database raises
`CLIError`.

**Current behavior:** the fence decides whether to arm from the *startup*
binding only (`src/popoto/transfer/cli.py:302-304`):

```python
self.db = _pool_db(get_REDIS_DB().connection_pool)
self.fenced_db = 0
self.active = self.db == 0 and not allow_db0
```

If the CLI starts bound to database N≠0 (`REDIS_URL=…/15`) and the `--model`
module — or anything it imports — rebinds the global client to database 0
(`set_REDIS_DB_settings(...)`, or its own `redis.Redis()` built from defaults
or an inherited URL), `active` is `False`, `__enter__` installs no guard, and
the post-resolution check `fence.active and _uses_redis(model_class)` is
skipped. The transfer then reads from or writes to the live DB-0 store with no
refusal. The fence already keys each refusal on the *pool's own* `db`, so the
guard would catch this; it just is never installed.

**Desired outcome:** a transfer that would touch database 0 is refused unless
`--allow-db0`, regardless of which database the process started on. A
Postgres-only transfer that never checks out a Redis connection still runs
without `--allow-db0` (the #833 contract).

## Freshness Check

**Baseline commit:** e7df3051
**Issue filed at:** 2026-10-09T04:31:34Z
**Disposition:** Unchanged

- `src/popoto/transfer/cli.py:277-352` (`_Db0Fence`) matches the issue's
  description verbatim; the arming line is at :304.
- `git log --since=<issue createdAt> -- src/popoto/transfer tests/test_transfer_cli.py`
  is empty: nothing touched the fence since filing.
- #834 (current fence) merged 2026-10-09T02:40Z, before the issue was filed;
  it is the code under change. #722 is still open and unrelated in mechanism
  (core ORM binding, not the CLI fence).
- Bug confirmed by code-read: no guard is installed when `self.db != 0`, so
  nothing downstream can refuse. Not reproduced live by design — reproducing
  means letting a transfer reach DB 0, which is a live store on this machine.
- No overlapping active plan in `docs/plans/`. No `xfail` in
  `tests/test_transfer_cli.py`.
- The two stale `from popoto.redis_db import POPOTO_REDIS_DB` snapshots
  CLAUDE.md mentions (`write_filter.py`, `cyclic_decay_field.py`) are gone;
  every transfer path resolves the client at call time, so a rebind is seen
  by the transfer.

## Prior Art

- **#833 / PR #834** — introduced the class-level pool guard and the
  "Redis-bound model refused on resolution" check; made Postgres-only
  transfers on DB 0 work without `--allow-db0`. This plan generalizes its
  arming condition; the guard mechanism is reused unchanged.
- **#722** (open) — the core ORM binds DB 0 without refusal when `REDIS_URL`
  is inherited. Same hazard family, different layer; not addressed here.
- **#577 / #584** — `Db0FlushRefusedError` guard on the client. Precedent for
  refusing DB 0 by inspecting the live binding rather than env vars.

## Research

No relevant external findings — internal change to an existing guard; the
redis-py behaviour it relies on (`connection_kwargs` with no `db` key means
database 0) was checked locally on redis-py 7.1.1:
`redis.ConnectionPool().connection_kwargs == {}`, which `_pool_db` already
maps to 0.

## Solution

Arm on intent, refuse on the pool's own database:

1. **Always arm unless opted in.** In `_Db0Fence.__init__`, set
   `self.active = not allow_db0`. `fenced_db` stays `0`. `__enter__` then
   installs the class-level guard for every transfer run without
   `--allow-db0`. The guard already refuses only checkouts whose pool `db`
   equals `fenced_db`, so a run on DB 15 is unaffected and a Postgres-only
   run (no checkout) is unaffected.
2. **Re-read the binding after the model resolves.** Replace the
   post-resolution check in both `_run_export` and `_run_import`
   (`fence.active and _uses_redis(model_class)`) with one that also requires
   the *current* global binding to be the fenced database — an inline
   expression `_pool_db(get_REDIS_DB().connection_pool) == fence.fenced_db`
   (or, if both call sites want it, a module-level helper beside `_pool_db`;
   not a single-use method on `_Db0Fence`). This refuses
   a Redis-bound model whose module rebound N→0 cleanly, before its first
   command and without the "partway" warning. A model module that builds a
   *separate* DB-0 client (not the global) is still refused by the pool guard
   at first checkout.
3. **Name the refused database, not the startup one.** `message()` currently
   prints `self.db` (the startup binding), which for the N→0 case would read
   "refusing to … Redis database 15". Print `self.fenced_db` instead. (The
   test fixture `fenced` sets `fenced_db = self.db`, so existing assertions
   are unaffected.)
4. Update the module docstring paragraph that says the guard applies "when
   Redis is on database 0" to say it applies to any Redis command that would
   reach database 0, whatever the startup binding.

`self.db` (startup binding) can be kept for the `fenced` test fixture, which
sets `fenced_db = self.db`; it is no longer used for the arming decision.

**Redis Cluster:** `_pool_db` already reads a cluster binding as database 0,
so a cluster run was already armed at startup. This change does not alter
cluster behaviour; only the N→0 rebind case and the message wording change.

## Test Impact

- [ ] `test_db0_fence_refuses_only_a_redis_bound_transfer`,
  `test_db0_fence_passes_with_allow_db0`,
  `test_db0_fence_refuses_after_a_trip_the_model_module_caught`,
  `test_db0_fence_refuses_a_client_the_model_module_builds` — UNCHANGED; the
  `fenced` fixture overrides `active`/`fenced_db` after `__init__`, so the
  new arming rule is transparent to them. The post-resolution check now also
  consults the live binding, which under the fixture equals `fenced_db`.
- [ ] `fenced` fixture (`tests/test_transfer_cli.py:682`) — UPDATE: its
  `self.active = not allow_db0` line is now a no-op (that is the production
  rule); drop it and rewrite the docstring to say the fixture only retargets
  `fenced_db` to the test lane's database, since arming is now unconditional
  without `--allow-db0`.
- [ ] `test_db0_fence_refuses_every_client_and_restores_the_pools` —
  UNCHANGED (sets `active`/`fenced_db` directly).
- [ ] `test_db0_refusal_via_subprocess_does_not_touch_db0`,
  `test_postgres_only_process_on_db0_transfers_without_allow_db0` —
  UNCHANGED; they must stay green (startup-on-0 and Postgres-only-on-0).
- [ ] NEW `test_db0_fence_refuses_a_model_module_that_rebinds_to_db0` —
  marked `@pytest.mark.redis_only(reason=...)`; subprocess, child started on
  `REDIS_URL=redis://127.0.0.1:1/5` (dead port, non-zero DB), `--model`
  points at a temp module that calls
  `popoto.redis_db.set_REDIS_DB_settings(host="127.0.0.1", port=1, db=0)`
  to rebind the global client to a dead-port DB 0 and then defines a
  Redis-bound model. Assert exit 1, `"refusing to"` and `"--allow-db0"` in
  stderr, `"database 0"` named in the message, `"partway"` **absent**, no
  `Traceback`. The dead port means a fence that fails to stop the command
  produces a connection error, not a write to a real DB 0, and the assertion
  on the refusal text tells the two apart. Run for both `export` and
  `import`.
  - **Child env must be Redis-pinned.** `_child_env`
    (`tests/test_transfer_cli.py:604`) copies the parent env wholesale, and
    the plugin pins the backend to Redis only in-process
    (`pytest_plugin.py:120-138`, `_pin_session_backend_to_redis`). The test
    builds its env from `_child_env` and then pops `POPOTO_BACKEND`,
    `POSTGRES_URL`, `POPOTO_POSTGRES_URL`, `POPOTO_POSTGRES_SCHEMA`,
    `POPOTO_POSTGRES_LISTEN_URL`, `POPOTO_POSTGRES_MAINTENANCE_URL`; the temp
    model also pins itself to Redis explicitly (Meta backend / `set_backend("redis")`
    in the temp module, whichever the model API offers), so a Postgres-lane
    parent environment cannot turn the case into a Postgres-only run that
    is never refused.
  - **Must prove step 2 is needed.** The temp module only rebinds and defines
    the model — no Redis command at module scope (no `save()`, `ping()`,
    query). That way the pool guard alone (step 1) would not trip until the
    transfer's first command, producing the "partway" warning; only the
    post-resolution binding re-check (step 2) gives the clean, pre-command
    refusal. Asserting `"partway"` absent makes the test fail if step 2 is
    reverted.
- [ ] NEW `test_db0_rebind_with_allow_db0_is_not_refused` — same subprocess
  and temp module, plus `--allow-db0`. Assert `"refusing to"` **not** in
  stderr. Do not assert exit 0: the dead port means the run then fails with a
  connection error, which is the expected, harmless outcome.
- [ ] NEW `test_postgres_only_rebind_to_db0_runs_without_allow_db0` —
  Postgres-leg only (skips when Postgres is unavailable, like the existing
  `test_postgres_only_process_on_db0_transfers_without_allow_db0`): child on
  a non-zero DB, temp module rebinds to DB 0 and defines a Postgres-backed
  model; run without `--allow-db0` and assert no `"refusing to"` and exit 0.
- [ ] NEW unit test: a `_Db0Fence(allow_db0=False, ...)` constructed while the
  process is bound to the test DB is `active`, and inside it a client built
  on a dead-port DB-0 URL raises `CLIError` on `ping()` while the test-DB
  client still pings; with `allow_db0=True` it is inactive and installs
  nothing.

## Rabbit Holes

- Do not try to fix #722 (core ORM binding DB 0 from an inherited
  `REDIS_URL`) here — that is a library-wide default change.
- Redis Cluster / Sentinel pools: a cluster binding already reads as
  database 0 via `_pool_db`, so it was already armed at startup; this change
  does not alter cluster behaviour. Do not add cluster special-casing.
- Do not refactor the guard into a context var or per-client hook — the
  class-level patch is what makes clients built by the model module fenced.

## Risks

- **Every non-opted-in run now patches pool classes.** Previously only runs
  starting on DB 0 did. The patch is restored in `__exit__` and costs one
  dict lookup per checkout. In-process tests calling `main()` on DB 15 now
  run under the guard; any test that legitimately touches DB 0 during
  `main()` would now fail. None exist today (the DB-0 probes in
  `pytest_plugin.py` run at session setup, outside `main()`), but the full
  `tests/test_transfer_cli.py` run is the check.
- A pool with no `db` key is treated as DB 0 (redis-py's own default). Now
  applied to every run, not just startup-on-0 — correct, but worth stating.

## No-Gos (Out of Scope)

- Fencing DB 0 outside `popoto-transfer` (the library-level gap is #722).
- Changing the `--allow-db0` flag name, semantics, or exit codes.
- Postgres-side fencing.

## Documentation

- [ ] Update the module docstring of `src/popoto/transfer/cli.py` and the
  `_Db0Fence` docstring to describe arming on every run without
  `--allow-db0`.
- [ ] Check `docs/guides/export-import.md` and
  `docs/features/postgres-backend.md` (both mention `--allow-db0`); adjust
  any sentence implying the refusal depends on the startup binding.
- [ ] CHANGELOG entry under Unreleased (Fixed), plus one line noting the
  refusal message now names the fenced database (0) rather than the startup
  binding, so an N→0 refusal reads "database 0", not "database N".

## Success Criteria

- [ ] A transfer started on DB N≠0 whose `--model` module rebinds the global
  client to DB 0 exits 1 with the `--allow-db0` refusal naming database 0,
  for both `export` and `import`.
- [ ] The refusal for the rebind case happens before any Redis command:
  `"partway"` does not appear in stderr.
- [ ] With `--allow-db0` the same run is not refused by the fence
  (`"refusing to"` absent from stderr; exit code not asserted — dead port),
  covered by `test_db0_rebind_with_allow_db0_is_not_refused`.
- [ ] A Postgres-only transfer whose model module rebinds the Redis client
  N→0 still runs without `--allow-db0`
  (`test_postgres_only_rebind_to_db0_runs_without_allow_db0`, Postgres leg).
- [ ] All existing `test_db0_*` tests and
  `test_postgres_only_process_on_db0_transfers_without_allow_db0` pass.
- [ ] `pytest tests/test_transfer_cli.py` green; `ruff check src/`,
  `black --check src/ tests/`, `scripts/mypy_ratchet.py` do not regress.

## Step by Step Tasks

1. In `_Db0Fence.__init__`, arm with `self.active = not allow_db0`.
2. In `_run_export` and `_run_import`, change the post-resolution refusal to
   `fence.tripped or (fence.active and _uses_redis(model_class) and _pool_db(get_REDIS_DB().connection_pool) == fence.fenced_db)`
   (inline, or via one module-level helper shared by both call sites).
3. Make `message()` name `self.fenced_db`.
4. Update the module and class docstrings.
5. Update the `fenced` fixture: drop the now-no-op `self.active` line and
   rewrite its docstring.
6. Add the subprocess rebind tests (refused export + import, `--allow-db0`
   not refused, Postgres-only rebind runs) and the unit test from Test
   Impact. Model modules live in `tmp_path` and are put on the child's
   `PYTHONPATH`; the child env drops `POPOTO_BACKEND` and the Postgres DSN
   vars, the temp model is pinned to Redis (except the Postgres-only case),
   and the Redis cases carry `redis_only`.
7. Update docs listed under Documentation and the CHANGELOG.
8. Run `pytest tests/test_transfer_cli.py`, then the full suite, ruff, black,
   mypy ratchet.

## Verification

- `pytest tests/test_transfer_cli.py -k db0` — all pass, including the new
  rebind tests.
- Revert step 1 locally and confirm the new rebind test fails with a
  connection error rather than the refusal (proves the test is not vacuous).
- Revert step 2 locally (keep step 1) and confirm the new rebind test fails
  on the `"partway"`-absent assertion — the pool guard trips at first
  command instead of the clean pre-command refusal (proves step 2 is needed).
- Run the new subprocess tests with `POPOTO_BACKEND=postgres` and
  `POSTGRES_URL` set in the parent shell and confirm the Redis cases still
  refuse (proves the child env scrub works).
- Full `pytest` with the Postgres backend available, so the parametrized
  `backend` cases of the fence tests both run.

## Open Questions

None blocking. Redis Cluster was already refused at startup (`_pool_db`
reads it as 0), so there is no behaviour change for it.
