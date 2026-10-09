---
status: Ready
type: bug
appetite: Small
owner: valorengels
created: 2026-10-09
tracking: https://github.com/tomcounsell/popoto/issues/837
last_comment_id:
revision_applied: true
revision_applied_at: 2026-10-09T08:28:43Z
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
   the *current* global binding to be the fenced database. Route both
   binding reads (this one and `__init__`'s at cli.py:301) through one
   module-level helper beside `_pool_db` (not a method on `_Db0Fence`), e.g.
   `_bound_db()` returning
   `_pool_db(getattr(get_REDIS_DB(), "connection_pool", None))`, and compare
   `_bound_db() == fence.fenced_db`. The `getattr` is load-bearing: a client
   with no `connection_pool` (e.g. `redis.cluster.RedisCluster`, see
   `pytest_plugin.py:233-235`) makes the bare `.connection_pool` read raise
   `AttributeError`; `_pool_db(None)` returns 0, so a pool-less binding
   counts as database 0 and is refused without `--allow-db0`.

   **What step 2 is for: preventing over-refusal.** Once step 1 makes
   `active` true on every run without `--allow-db0`, the existing check
   `fence.active and _uses_redis(model_class)` is true for *every*
   Redis-bound model, so without the binding conjunct every Redis transfer
   started on DB N≠0 — the normal, safe case, and every in-process Redis
   test in `tests/test_transfer_cli.py` — would be refused. Step 2 narrows
   the post-resolution refusal back to "the global client is bound to the
   fenced database right now". It is *not* what makes the N→0 rebind
   refusal clean: with step 1 alone, the unnarrowed check already refuses the
   rebind model after resolution with `started` False. The witness for
   step 2 is therefore a Redis transfer on the non-zero test DB that must
   succeed (see Test Impact), not the rebind test.

   **Deliberate loosening: start on 0, rebind to N.** Today a run that starts
   on DB 0 and whose model module rebinds the global client to DB N≠0 is
   refused at cli.py:476/568 (keyed on the startup binding; confirmed live on
   739a9337 with a dead-port child). After step 2 it is allowed: the transfer
   no longer touches DB 0, and the step-1 pool guard still trips on any
   leftover DB-0 checkout. This is intended, tested, and gets a CHANGELOG
   line.

   **Known residual (unchanged, not a regression): a model module that builds
   its own DB-0 client while the global stays on N.** That shape passes the
   post-resolution check (the global is N) and is refused only by the pool
   guard at its first checkout. If that checkout happens at import time it is
   refused before `started` (covered today by
   `test_db0_fence_refuses_a_client_the_model_module_builds`,
   tests/test_transfer_cli.py:824). If it first happens during the transfer,
   it is refused after `fence.started = True` (cli.py:479), prints the
   "partway" warning, and may leave a partial export/import. Step 2 does not
   cover this and must not be widened to inspect non-global clients (Rabbit
   Holes). Before this change that shape was never refused at all when the
   run started on N≠0, so step 1 strictly improves it.
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

**Redis Cluster (correction):** `RedisCluster` has no `connection_pool`, so
today `_Db0Fence.__init__` (cli.py:301) raises `AttributeError` on a cluster
binding before any refusal; it was never "read as database 0". With the
`_bound_db()` helper both reads tolerate a pool-less client and treat it as
database 0: a cluster-bound run (startup, or rebound by the model module) is
refused cleanly without `--allow-db0` instead of crashing. No cluster
special-casing beyond the `getattr`; whether the class-level pool guard
covers cluster node pools is out of scope.

## Test Impact

- [ ] `test_db0_fence_refuses_only_a_redis_bound_transfer`,
  `test_db0_fence_passes_with_allow_db0`,
  `test_db0_fence_refuses_after_a_trip_the_model_module_caught`,
  `test_db0_fence_refuses_a_client_the_model_module_builds` — UNCHANGED; the
  `fenced` fixture overrides `active`/`fenced_db` after `__init__`, so the
  new arming rule is transparent to them. The post-resolution check now also
  consults the live binding, which under the fixture equals `fenced_db`.
  Because the fixture sets `fenced_db = self.db`, these tests **cannot**
  detect a revert of step 2; they are not step-2 evidence.
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

### Step-2 witness (over-refusal guard)

- [ ] Existing `test_round_trip_export_then_import`
  (`tests/test_transfer_cli.py:128`), Redis leg — UNCHANGED, now load-bearing:
  it calls `main(["export", ...])` and `main(["import", ...])` in-process on
  the non-zero test DB with no `fenced` fixture and no `--allow-db0`, and
  asserts exit 0. With step 1 kept and step 2 reverted it is refused (exit 1).
  The other unfenced in-process Redis tests (`test_filter_narrows_…`,
  `test_on_conflict_*`, `test_out_dash_*`) fail the same way.
- [ ] NEW `test_unfenced_redis_transfer_on_test_db_is_not_refused` —
  `redis_only` (on the Postgres leg `_uses_redis` is false, so it would not
  witness step 2), parametrized over `export`/`import`. No `fenced` fixture,
  no `--allow-db0`; asserts the live binding is non-zero, then runs `main()`
  on `MODEL_SPEC` and asserts exit 0 and `"refusing to"` absent from stderr.
  This pins the over-refusal guard by name so it does not rest on the
  round-trip test's incidental coverage.

### N→0 rebind refusal (steps 1 and 3)

Shared setup for the subprocess cases below: a temp model module in
`tmp_path`, put on the child's `PYTHONPATH`, that calls
`popoto.redis_db.set_REDIS_DB_settings(host="127.0.0.1", port=1, db=<target>)`
at module scope and then defines a Redis-pinned model — no Redis command at
module scope (no `save()`, `ping()`, query). Verified on 739a9337: defining a
model after the rebind issues no command, and on current main the N→0 case
exits with `Error 61 connecting to 127.0.0.1:1` and no refusal (the bug). The
dead port means any fence miss is a connection error, never a write to a real
database.

- **Child env must be Redis-pinned** (all cases except the Postgres-only
  one). `_child_env` (`tests/test_transfer_cli.py:604`) copies the parent env
  wholesale, and the plugin pins the backend to Redis only in-process
  (`pytest_plugin.py:120-138`). The test builds its env from `_child_env` and
  then pops `POPOTO_BACKEND`, `POSTGRES_URL`, `POPOTO_POSTGRES_URL`,
  `POPOTO_POSTGRES_SCHEMA`, `POPOTO_POSTGRES_LISTEN_URL`,
  `POPOTO_POSTGRES_MAINTENANCE_URL`; the temp model also pins itself to Redis
  explicitly (Meta backend / `set_backend("redis")`, whichever the model API
  offers), so a Postgres-lane parent cannot turn the case into a
  never-refused Postgres-only run.

- [ ] NEW `test_db0_fence_rebind_to_db0` — `redis_only`, parametrized over
  `verb in ("export", "import")` × `allow_db0 in (False, True)`. Child
  `REDIS_URL=redis://127.0.0.1:1/5` (dead port, non-zero DB), module rebinds
  to `db=0`. For `import`, the `--in` file is a manifest-only JSONL written by
  the parent (`build_manifest` + `dump_line`), so the child needs no Redis
  read to produce input.
  - `allow_db0=False`: assert exit 1, `"refusing to"` and `"--allow-db0"` in
    stderr, `"Redis database 0"` in stderr (step 3), `"partway"` **absent**,
    no `Traceback`.
  - `allow_db0=True`: assert `"refusing to"` **not** in stderr. Do not assert
    exit 0 — the dead port makes the run fail with a connection error, which
    is the expected, harmless outcome.
  - **What `"partway"`-absent proves, honestly:** only that *some*
    post-resolution check refuses before the transfer starts. It is satisfied
    by step 1 alone (the unnarrowed check) as well as by steps 1+2, so it is
    not evidence for step 2. Its matching ablation is removing the
    post-resolution refusal (condition reduced to `fence.tripped`) or keying
    it on the startup binding (`fence.db == 0`): then the pool guard trips
    only at the transfer's first command, after `started`, and `"partway"`
    appears (export case; the manifest-only import case issues no command,
    so under that ablation it fails on exit code instead).
- [ ] NEW `test_db0_fence_allows_rebind_from_db0_to_nonzero` — `redis_only`,
  export only. Child `REDIS_URL=redis://127.0.0.1:1/0`, module calls
  `set_REDIS_DB_settings(host="127.0.0.1", port=1, db=5)`, Redis-pinned
  model, no `--allow-db0`. Assert `"refusing to"` **not** in stderr and no
  `Traceback`; do not assert exit 0 (dead port). Pins the deliberate 0→N
  loosening; confirmed refused on 739a9337, so this test fails today and
  passes only with step 2. If it shows "refusing to" after the change, some
  DB-0 checkout happened before the rebind — that is a real finding to
  investigate, not a test to relax.
- [ ] NEW `test_postgres_only_rebind_to_db0_runs_without_allow_db0` —
  Postgres leg only (`pytest.skip` unless `backend.name == "postgres"`, like
  `test_postgres_only_process_on_db0_transfers_without_allow_db0`). Child env
  built exactly as that test does: `_child_env(...)` then
  `REDIS_URL="redis://127.0.0.1:1/5"`, `POPOTO_BACKEND="postgres"`,
  `POPOTO_POSTGRES_URL=pg.dsn`, `POPOTO_POSTGRES_SCHEMA=pg.schema` (`pg =
  get_backend(TransferCliItem)`). Temp module calls
  `set_REDIS_DB_settings(host="127.0.0.1", port=1, db=0)` and defines a
  model on the default (Postgres) backend. Run export without `--allow-db0`;
  assert exit 0 and no `"refusing"`. The dead-port DB-0 target means a fence
  miss that dialed Redis would surface as a connection error, not a write to
  a real DB 0.
- [ ] NEW unit test `test_db0_fence_arms_on_every_run_without_allow_db0` —
  shrunk to the arming rule only (pool-guard behaviour is already covered by
  `test_db0_fence_refuses_every_client_and_restores_the_pools`): with the
  process bound to the non-zero test DB, `_Db0Fence(False, "read from").active`
  is `True` and `_Db0Fence(True, "read from").active` is `False`.

## Rabbit Holes

- Do not try to fix #722 (core ORM binding DB 0 from an inherited
  `REDIS_URL`) here — that is a library-wide default change.
- Redis Cluster / Sentinel pools: beyond the `getattr(..., "connection_pool",
  None)` in `_bound_db()` (which turns today's `AttributeError` into a
  DB-0-style refusal), do not add cluster special-casing or try to fence
  cluster node pools.
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
  `--allow-db0`, the post-resolution refusal keyed on the *current* global
  binding, and the known residual (a model-built DB-0 client first used
  mid-transfer is refused with the "partway" warning).
- [ ] Check `docs/guides/export-import.md` and
  `docs/features/postgres-backend.md` (both mention `--allow-db0`); adjust
  any sentence implying the refusal depends on the startup binding.
- [ ] CHANGELOG under Unreleased, three lines:
  - Fixed: a transfer started on DB N≠0 whose `--model` module rebinds the
    client to DB 0 is now refused without `--allow-db0` (#837).
  - Changed: a transfer started on DB 0 whose `--model` module rebinds the
    global client to DB N≠0 is no longer refused — it no longer touches
    database 0; any remaining DB-0 command is still refused by the pool
    guard.
  - Changed: the refusal message names the fenced database (0) rather than
    the startup binding, so an N→0 refusal reads "database 0", not
    "database N".

## Success Criteria

- [ ] A transfer started on DB N≠0 whose `--model` module rebinds the global
  client to DB 0 exits 1 with the `--allow-db0` refusal naming
  "Redis database 0", for both `export` and `import`, with `"partway"`
  absent (`test_db0_fence_rebind_to_db0`, `allow_db0=False` cases).
- [ ] With `--allow-db0` the same run is not refused by the fence
  (`"refusing to"` absent; exit code not asserted — dead port)
  (`test_db0_fence_rebind_to_db0`, `allow_db0=True` cases).
- [ ] An unfenced Redis transfer on the non-zero test DB without
  `--allow-db0` exits 0 and is not refused, for both `export` and `import`
  (`test_unfenced_redis_transfer_on_test_db_is_not_refused`, and the Redis
  leg of `test_round_trip_export_then_import`) — the step-2 witness.
- [ ] A transfer started on DB 0 whose model module rebinds the global client
  to DB N≠0 is not refused (`test_db0_fence_allows_rebind_from_db0_to_nonzero`)
  — the deliberate loosening.
- [ ] A Postgres-only transfer whose model module rebinds the Redis client
  N→0 still runs without `--allow-db0`
  (`test_postgres_only_rebind_to_db0_runs_without_allow_db0`, Postgres leg).
- [ ] `_Db0Fence(False, …)` is active and `_Db0Fence(True, …)` is not, on a
  non-zero binding (`test_db0_fence_arms_on_every_run_without_allow_db0`).
- [ ] All existing `test_db0_*` tests and
  `test_postgres_only_process_on_db0_transfers_without_allow_db0` pass.
- [ ] `pytest tests/test_transfer_cli.py` green; `ruff check src/`,
  `black --check src/ tests/`, `scripts/mypy_ratchet.py` do not regress.

## Step by Step Tasks

1. Add module-level `_bound_db()` returning
   `_pool_db(getattr(get_REDIS_DB(), "connection_pool", None))`. In
   `_Db0Fence.__init__`, set `self.db = _bound_db()` and arm with
   `self.active = not allow_db0`.
2. In `_run_export` and `_run_import`, change the post-resolution refusal to
   `fence.tripped or (fence.active and _uses_redis(model_class) and _bound_db() == fence.fenced_db)`.
3. Make `message()` name `self.fenced_db`.
4. Update the module and class docstrings (including the known residual).
5. Update the `fenced` fixture: drop the now-no-op `self.active` line and
   rewrite its docstring.
6. Add the tests from Test Impact: the step-2 witness
   (`test_unfenced_redis_transfer_on_test_db_is_not_refused`), the
   parametrized `test_db0_fence_rebind_to_db0` (export/import ×
   allow_db0), `test_db0_fence_allows_rebind_from_db0_to_nonzero`,
   `test_postgres_only_rebind_to_db0_runs_without_allow_db0`, and the shrunk
   unit test. Temp model modules live in `tmp_path` on the child's
   `PYTHONPATH` and rebind to the dead port `127.0.0.1:1`; Redis cases scrub
   the child's backend/Postgres env, pin the model to Redis, and carry
   `@pytest.mark.redis_only(reason="...")` — always with `reason=`, as at
   tests/test_transfer_cli.py:739.
7. Update docs listed under Documentation and the CHANGELOG (three lines).
8. Run `pytest tests/test_transfer_cli.py`, then the full suite, ruff, black,
   mypy ratchet, then the ablations under Verification.

## Verification

Each ablation names the test that must turn red; restore the code after
each.

- `pytest tests/test_transfer_cli.py -k "db0 or rebind or round_trip or unfenced"`
  — all pass.
- **Ablate step 1** (`self.active = self.db == 0 and not allow_db0`): the
  `allow_db0=False` cases of `test_db0_fence_rebind_to_db0` fail — no guard,
  no post-resolution refusal. The export case dies with `Error 61 connecting
  to 127.0.0.1:1` instead of "refusing to" (what current main does, observed
  on 739a9337); the import case, whose manifest-only input issues no Redis
  command, most likely exits 0 — it fails on the exit-code and
  "refusing to" assertions, not on "Error 61".
- **Ablate step 2** (keep step 1, restore the unnarrowed
  `fence.tripped or (fence.active and _uses_redis(model_class))`):
  `test_unfenced_redis_transfer_on_test_db_is_not_refused` and the Redis leg
  of `test_round_trip_export_then_import` fail with exit 1 and "refusing to";
  `test_db0_fence_allows_rebind_from_db0_to_nonzero` fails (refused). The
  rebind-refusal test stays green under this ablation, as expected — it is
  not step-2 evidence.
- **Ablate the post-resolution refusal entirely** (keep step 1, condition
  reduced to `fence.tripped`): `test_db0_fence_rebind_to_db0`
  (`allow_db0=False`) fails. The export case fails on the `"partway"`-absent
  assertion — the pool guard trips only at the first transfer command, after
  `started`. The import case issues no Redis command on its manifest-only
  input, so nothing trips and it fails on the exit-code / "refusing to"
  assertions, not "partway".
- **Ablate step 3** (message prints `self.db`): `test_db0_fence_rebind_to_db0`
  fails on `"Redis database 0"` (message says database 5).
- Run the new subprocess tests with `POPOTO_BACKEND=postgres` and
  `POSTGRES_URL` set in the parent shell and confirm the Redis cases still
  refuse (proves the child env scrub works).
- Full `pytest` with the Postgres backend available, so the parametrized
  `backend` cases of the fence tests both run.

## Open Questions

None blocking. A Redis Cluster binding crashes today with `AttributeError`
at cli.py:301; after `_bound_db()` it is refused as database 0 unless
`--allow-db0` (small, intended behaviour change). The 0→N loosening is
treated as intended (see Solution step 2); flag at review if that is wrong.

## Critique Results

Critique round 2 (2026-10-09), FULL depth, independent roster (3 critics). Verdict: NEEDS REVISION.

| Severity | Critics | Finding | Addressed By | Implementation Note |
|----------|---------|---------|--------------|---------------------|
| BLOCKER | Risk & Robustness, History & Consistency (also verified by the critique runner) | The step-2 ablation is false. If step 2 is reverted while step 1 is kept, the check at cli.py:476/568 reads `fence.tripped or (fence.active and _uses_redis(model_class))` with `active = not allow_db0`, which is True. That check already refuses the rebind model cleanly after resolution, with `started` False, so "partway" never appears. The new rebind test passes with or without step 2, so neither it nor the "Revert step 2" Verification bullet shows that step 2 is needed. This was prior-critique concern (2), and it is still unmet. | Solution step 2 ("What step 2 is for"); Test Impact "Step-2 witness" (`test_unfenced_redis_transfer_on_test_db_is_not_refused` + Redis leg of `test_round_trip_export_then_import`); "partway" recast in `test_db0_fence_rebind_to_db0`; Verification ablations rewritten | Step 2 exists to prevent over-refusal: without the binding conjunct, every Redis-bound transfer on DB N≠0 is refused. Use as the step-2 witness a non-vacuous test with no `fenced` fixture and no `--allow-db0`: an in-process `main(["export", "--model", MODEL_SPEC, ...])` on the test DB that must exit 0 with no "refusing to" (the existing unfenced round-trip tests also qualify; name them). The `fenced` fixture sets `fenced_db = self.db`, so fixture-based tests cannot detect the revert. Recast the "partway"-absent assertion as proof that a post-resolution check exists at all. Its matching ablation is replacing the post-resolution condition with `fence.tripped` alone, or keying it on the startup `fence.db == 0`. Fix the Test Impact sentence "only the post-resolution binding re-check (step 2) gives the clean, pre-command refusal". |
| CONCERN | Scope & Value | Step 2 loosens an existing refusal without saying so. A run that starts on DB 0 and whose model module rebinds the global client to DB N≠0 is refused today at cli.py:476/568. After the change it runs against DB N. That is probably correct, since the transfer no longer touches DB 0, but the plan has no success criterion, test, or CHANGELOG line for it. | Solution step 2 ("Deliberate loosening", confirmed refused today on 739a9337); `test_db0_fence_allows_rebind_from_db0_to_nonzero`; Success Criteria; CHANGELOG "Changed" line | State the 0→N case as deliberately allowed. The pool guard still trips on any leftover DB-0 checkout, so the change is safe. Add a subprocess test: `REDIS_URL=redis://127.0.0.1:1/0`, and a temp module calling `set_REDIS_DB_settings(host="127.0.0.1", port=1, db=5)` that defines a Redis-pinned model. Assert "refusing to" is not in stderr; do not assert exit 0 (dead port). Add a CHANGELOG sentence. |
| CONCERN | Risk & Robustness | The Solution step 2 sentence about a model that builds a separate DB-0 client (the global binding stays N) presents that case as covered. It passes the post-resolution check and is refused only at first checkout, after `fence.started = True` (cli.py:479). That run prints the "partway" warning and may leave a partial export or import. This is not a regression, but the plan does not say it. | Solution step 2 ("Known residual"); Documentation (docstring states residual) | Say plainly that this shape refuses with "partway". `test_db0_fence_refuses_a_client_the_model_module_builds` (tests/test_transfer_cli.py:824) covers only an import-time command, before `started` is set. If you pin the in-transfer shape, assert `"partway" in err`. Do not widen step 2 to inspect non-global clients (Rabbit Holes). |
| NIT | Scope & Value | The test additions are heavy for a one-line arming change plus one conjunct. The new unit test partly duplicates `test_db0_fence_refuses_every_client_and_restores_the_pools`. | Test Impact: `test_db0_fence_rebind_to_db0` parametrized export/import × allow_db0; unit test shrunk to `test_db0_fence_arms_on_every_run_without_allow_db0` | Parametrize the rebind refusal over export/import and fold in the `--allow-db0` case. Shrink the unit test to asserting that `_Db0Fence(False, ...)` on a non-zero DB is `active` and `_Db0Fence(True, ...)` is not. |
| NIT | Critique runner | The Postgres-only rebind test does not name its dead-port rebind target or the Postgres env it needs (POPOTO_BACKEND/POPOTO_POSTGRES_URL/SCHEMA, as in `test_postgres_only_process_on_db0_transfers_without_allow_db0`). | Test Impact: `test_postgres_only_rebind_to_db0_runs_without_allow_db0` names target and env | Rebind with `set_REDIS_DB_settings(host="127.0.0.1", port=1, db=0)`, so a fence miss shows up as a connection error rather than a write to DB 0. Set the Postgres env the same way the existing test does. |

Critique round 3 (2026-10-09): READY TO BUILD (with concerns), 0 blockers.

| Severity | Finding | Addressed By |
|----------|---------|--------------|
| CONCERN | Cluster claim wrong: `RedisCluster` has no `connection_pool`, so cli.py:301 already raises `AttributeError`, and step 2's expression would repeat it. | Solution step 2 (`_bound_db()` helper with `getattr(..., None)`); Redis Cluster correction; Rabbit Holes; Open Questions; Tasks 1-2 |
| NIT | Import ablation wording: manifest-only import issues no Redis command, so it fails on exit code (not "partway") and likely exits 0 under step-1 revert (not "Error 61"). | Verification ablations; Test Impact "partway" note |
| NIT | New tests must pass `reason=` to `redis_only`. | Task 6 |
