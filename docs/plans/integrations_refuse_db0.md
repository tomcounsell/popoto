---
status: Planning
type: bug
appetite: Small
owner: Dev lane (session/integrations_refuse_db0)
created: 2026-09-02
tracking: https://github.com/tomcounsell/popoto/issues/584
last_comment_id: 
---

# Refuse Redis DB 0 in `popoto.integrations` unless explicitly opted in

## Problem

The library holds two contradictory positions about Redis database 0.

The pytest plugin refuses it outright. `_resolve_test_db`
(`src/popoto/pytest_plugin.py:194-198`) raises on `popoto_test_db=0` with
"DB 0 is typically production."

The harness integration writes there by default. `DEFAULT_URL`
(`src/popoto/integrations/config.py:42`) is `redis://localhost:6379/0`, used
whenever neither `POPOTO_MEMORY_URL` nor `REDIS_URL` is set. So the memory
layer's zero-configuration path — the path the whole package is designed
around, since a hook is a bare command string with no config file — targets
the one database the test harness considers too dangerous to touch.

This is not theoretical. During the SDLC run of 2026-08-13/14 a reviewer
scanned DB 0 on a developer machine and found 18 `*DefaultMemory*` keys plus
4 `$popoto_memory:*` keys, left by ordinary use of the integration. Working
as designed, which is the point. That same DB 0 held live agent state, and
two accidental `flushdb` calls destroyed it (#577).

For a PyPI adopter the exposure is worse: DB 0 is the default database of
every stock Redis and Valkey install, so it is the database most likely to
already hold unrelated application data. Defaulting there maximizes the
chance popoto's memory corpus shares a keyspace with something else, and
turns any `FLUSHDB` in that environment into a mutual-destruction event.

**Desired outcome:** `popoto.integrations` refuses to write agent memory to
database 0 and says so loudly, unless the operator opted in at deploy time.
Nothing is silently relocated.

## The Settled Decision

Recorded by the maintainer on #584 (2026-09-02). This is input to the plan,
not an open question, and is not reopened below.

**Option 2 — refuse DB 0 unless explicitly opted in.** Auto-detect a safe
database; refuse DB 0 loudly rather than writing there, mirroring the pytest
plugin so the library's two halves finally agree.

**Option 1 (silently repoint `DEFAULT_URL` to `/1`) was rejected** on its
failure mode: an existing adopter's corpus becomes unreachable with no error,
and the library reports an empty store. For a memory layer that reads as
amnesia, not as a config change. A loud refusal is the better failure.

**The opt-in escape hatch must be deploy-level (an environment variable),
not a constructor kwarg** — PyPI adopters cannot always edit model code. This
is the same doctrine as `POPOTO_MEMORY_ENABLED`: a kill switch that works
without touching Python.

**Pre-release blocker.** `popoto.integrations` does not exist in published
1.8.2; it first ships in 1.9.0. The DB 0 default has not reached a single
external user. Fixing it now is a sane default; shipping it and fixing it
later means taking exactly the silent-relocation break that was just
rejected, on users this release would have created.

Two consequences the implementation must honor:

- `DEFAULT_URL` stays `redis://localhost:6379/0`. It is not repointed to
  `/1` or to anything else. The refusal is what changes, not the resolved
  target.
- The escape hatch is `POPOTO_MEMORY_ALLOW_DB0`, an environment variable. No
  `allow_db0=` constructor parameter is added to any public callable. The
  field exists on `MemoryConfig` because that dataclass is the resolved
  *result* of reading the environment, and in-process callers already build
  it directly (`demo.py:53`) — but the documented, supported way to set it
  is the env var.

## Freshness Check

Baseline: `3a793d6` (main tip at plan time, 2026-08-31).

Issue #584 was filed 2026-08-17T02:54Z. Verifications performed:

| Claim | Status |
|---|---|
| `DEFAULT_URL = "redis://localhost:6379/0"` at `config.py:42` | **Unchanged.** Exact line, exact value. |
| pytest plugin rejects DB 0 | **Unchanged.** `pytest_plugin.py:194-198`, message verbatim as quoted. |
| `bind_connection` is the single connection chokepoint | **Unchanged.** `service.py:106` is still the only call; `cli.py:152-153` still documents deliberately *not* binding. |
| #577 (the guard gap this was split from) | **Still open.** Complementary, not superseding: #577 is about ad-hoc scripts bypassing the guard, #584 is about the library steering users into DB 0. No overlap in files. |
| Commits touching `src/popoto/integrations/` since 2026-08-17 | One: `3a793d6` (#592, `exclude_keys` suppression). It changed `MemoryService.assemble` and added `_injected_key` plumbing. It does **not** touch `config.py`, `bind_connection`, or any connection resolution. Irrelevant to this fix. |
| Active plans overlapping this area | None. `docs/plans/harness_integration.md` is the origin plan for this package and is `Complete`; no open plan touches `integrations/config.py`. |

**Disposition: Unchanged.** All issue claims hold against `3a793d6`.

## Prior Art

- **#546 / `docs/plans/harness_integration.md`** — built this package. Its
  `bind_connection` design notes are the direct ancestor of this work:
  binding swaps the *pool* in place rather than replacing the client,
  because most of Popoto imports `POPOTO_REDIS_DB` at module load
  (`config.py:198-260`). Any guard added here must respect that, and must
  not itself rebind.
- **`bind_connection`'s existing refusal** — `config.py:239-247` already
  raises `ValueError` when a URL carries no database number
  (`redis://localhost:6379/`), for exactly the reason this plan generalizes:
  "honouring it would silently leave every write on whatever database Popoto
  is already using." This is the pattern to extend, not to invent.
- **`tests/test_integrations_db0_isolation.py`** — an entire module already
  exists to prove DB 0 stays untouched, including
  `test_a_url_with_no_database_is_rejected` and
  `test_the_rejection_reaches_the_hook_as_exit_zero_plus_a_log`. New tests
  belong here and should copy its subprocess/`_db0()` idioms rather than
  invent parallel ones.
- **#577** — the sibling guard-gap issue. Not fixed by this plan.

**Why previous fixes did not cover this:** the harness plan solved
*misdirection* (writes landing somewhere other than the configured URL) and
proved it with the DB-0 isolation suite. It never questioned whether the
*configured* URL should be allowed to be DB 0 in the first place. The
isolation tests all set `POPOTO_MEMORY_URL` to the test DB explicitly, so
the zero-configuration path — the one that resolves to DB 0 — is precisely
the path they do not exercise.

## Research

No external research performed. The change touches no third-party API beyond
`redis.connection.parse_url` and `INFO keyspace`, both already used or
trivially documented, and both identical on Redis and Valkey. Per
`feedback_valkey_compatibility`: `INFO keyspace` is a core command, not a
module command, so the suggestion probe stays Valkey-safe.

## Data Flow

Every entry point reaches Redis through a `MemoryService`, which binds the
connection in `__init__`. That is the property the guard depends on.

```
env (POPOTO_MEMORY_URL | REDIS_URL | neither)
  └─> MemoryConfig.from_env()            config.py:125
        · url            = explicit or REDIS_URL or DEFAULT_URL
        · url_is_explicit= bool(POPOTO_MEMORY_URL)
        · url_source     = NEW: which of the three won
        · allow_db0      = NEW: POPOTO_MEMORY_ALLOW_DB0
  └─> MemoryService.__init__()           service.py:90
        └─> bind_connection(config)      config.py:198
              ├─ NEW: guard  ── refuse ──> Db0RefusedError
              └─ existing rebind path
```

Callers of `MemoryService(...)`, and what each does with the raise:

| Caller | Site | Behavior on refusal |
|---|---|---|
| Hook (Claude Code, Codex, Hermes) | `hooks.py:235` via `hooks.run` | `hooks.run` catches (`hooks.py:284-289`), writes the message to the log, returns `None`. `_cmd_hook` exits 0. **The turn survives; nothing is written to DB 0.** |
| MCP server | `mcp_server.py:177-190` | `dispatch` catches and returns `_error(str(exc))`, so the message reaches the agent as a tool error. |
| `popoto-memory doctor` | `cli.py:211-228` | Already catches `ValueError` and prints it (text and `--json`), exits 1. Works unchanged because the new error subclasses `ValueError`. |
| `popoto-memory demo` | `demo.py:49-53` | **Currently unguarded — would traceback.** Task below wraps it. |
| In-process host app / tests | direct construction | Raises. Correct: the caller chose its connection and can see the exception. |

The critical asymmetry: the *config's* URL and the *live connection's*
database are not the same thing when `POPOTO_MEMORY_URL` is unset. Under the
pytest plugin, `config.url` is `DEFAULT_URL` (database 0) while
`POPOTO_REDIS_DB` is on database 15. A guard that read `config.url` in that
case would refuse on every test in the suite. See Solution §2.

## Appetite

**Small.** One new env var, one new exception, one guard function, one
best-effort probe, and the tests to hold them. No schema change, no
migration, no change to any write path. The design work is already settled;
the risk is concentrated in getting the *effective database* resolution
right, which §2 pins down.

## Solution

### 1. The refusal (`src/popoto/integrations/config.py`)

Add, near the other module constants:

```python
ALLOW_DB0_ENV = "POPOTO_MEMORY_ALLOW_DB0"
"""Deploy-level opt-in for writing agent memory to Redis database 0.
An environment variable rather than a constructor argument on purpose:
a PyPI adopter running the hook cannot edit model code, and a hook is a
bare command string with no place to pass Python arguments. Accepts the
same truthy set as ``POPOTO_MEMORY_ENABLED`` (``1``/``true``/``yes``/``on``)."""
```

Add the exception:

```python
class Db0RefusedError(ValueError):
    """Raised when agent memory would be written to Redis database 0.

    Subclasses ``ValueError`` so the existing handlers keep working: the
    hook's blanket catch, the MCP dispatcher's, and ``doctor``'s explicit
    ``except ValueError`` (``cli.py:213``) all predate this error and all
    do the right thing with it.
    """
```

Add two `MemoryConfig` fields (appended, so positional construction in
existing callers is unaffected):

```python
url_source: str = "default"
"""Which input produced ``url``: ``"POPOTO_MEMORY_URL"``, ``"REDIS_URL"``,
or ``"default"``. Carried so the DB-0 refusal can name the variable the
operator actually has to change, and so ``doctor`` can show it."""

allow_db0: bool = False
"""Deploy-level opt-in, from ``POPOTO_MEMORY_ALLOW_DB0``. See
:data:`ALLOW_DB0_ENV`."""
```

Resolve both in `from_env`:

```python
explicit_url = env.get("POPOTO_MEMORY_URL", "").strip()
inherited_url = env.get("REDIS_URL", "").strip()
if explicit_url:
    url, url_source = explicit_url, "POPOTO_MEMORY_URL"
elif inherited_url:
    url, url_source = inherited_url, "REDIS_URL"
else:
    url, url_source = DEFAULT_URL, "default"
...
allow_db0=_as_bool(env.get(ALLOW_DB0_ENV), False),
```

`DEFAULT_URL` is **not** changed.

### 2. Effective-database resolution

The guard runs inside `bind_connection`, **before** the
`if not config.url_is_explicit: return False` early return at
`config.py:230-231` — that early return is exactly the zero-configuration
path this issue is about.

```python
def effective_db(config: "MemoryConfig") -> int:
    """The database number this service will actually write to.

    Two cases, and conflating them is the trap:

    * ``url_is_explicit`` — the caller named a URL, so its ``db`` is the
      answer. A URL with no ``db`` at all raises the existing
      "no database number" ``ValueError`` before we get here.
    * otherwise — Popoto's live connection is the answer, **not**
      ``config.url``. With no ``POPOTO_MEMORY_URL``, ``config.url`` is
      ``DEFAULT_URL`` (database 0) even when the process is on database
      15: the pytest plugin swaps ``POPOTO_REDIS_DB``'s pool in place and
      never touches ``MemoryConfig``. Reading ``config.url`` here would
      refuse on every test in the suite and on every host application
      that configured its own connection.
    """
```

Implementation:

```python
from ..redis_db import POPOTO_REDIS_DB

if config.url_is_explicit:
    wanted = parse_url(config.url)          # existing call, reused
    if "db" not in wanted:
        raise ValueError(...)               # existing message, unchanged
    return int(wanted["db"])
return int(
    POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db", 0) or 0
)
```

Behavior matrix this produces:

| Environment | Live connection | Effective db | Result |
|---|---|---|---|
| nothing set | db 0 (localhost default) | 0 | **refuse** |
| `REDIS_URL=.../0` | db 0 | 0 | **refuse** |
| `POPOTO_MEMORY_URL=.../0` | (any) | 0 | **refuse** |
| any of the above + `POPOTO_MEMORY_ALLOW_DB0=1` | — | 0 | allow, bind as before |
| `REDIS_URL=.../3` | db 3 | 3 | allow |
| `POPOTO_MEMORY_URL=.../3` | (any) | 3 | allow |
| under pytest plugin, no memory URL | db 15 | 15 | allow |
| host app bound db 4 in-process | db 4 | 4 | allow |

### 3. The safe-database suggestion (message only)

"Auto-detect a safe DB" is satisfied by *telling the operator which database
is free*, never by moving them to it. Silent relocation is the rejected
option; a concrete suggestion in the error is the loud equivalent.

```python
def suggest_free_db(client: Any = None) -> Optional[int]:
    """Lowest database in 1..15 that currently holds no keys, or None.

    Best effort and advisory only. Reads ``INFO keyspace`` (a core command
    on both Redis and Valkey — no modules, per the project's Valkey rule),
    which reports only non-empty databases, so anything in 1..15 absent
    from that report is empty. Any exception yields ``None`` and the error
    message falls back to a generic example; a diagnostic must never be
    the thing that fails.

    This function never rebinds anything.
    """
```

- Uses `sibling_client_kwargs(POPOTO_REDIS_DB.connection_pool.connection_kwargs)`
  (`redis_db.py`) so it inherits host/port/auth without tripping redis-py 8's
  pool-internal kwargs. Reuses the live client directly when possible.
- Range 1..15: 16 is the stock `databases` default and 15 is the pytest
  plugin's, so 1..15 covers every stock install without probing beyond it.
  If the server reports a smaller `databases`, the extra names are simply
  never suggested because the probe only offers a database it saw as absent
  from a keyspace report — a wrong suggestion here costs the operator one
  error message, not data.
- Wrapped in a short timeout inherited from the pool's existing
  `socket_timeout=5`. No new timeout knob.

### 4. Error message text

Built by `_db0_refusal_message(config)`. The first line names the input the
operator actually controls; the rest is constant.

Explicit `POPOTO_MEMORY_URL`:

```
POPOTO_MEMORY_URL=redis://localhost:6379/0 targets Redis database 0, and
Popoto refuses to write agent memory there. DB 0 is the default database of
every stock Redis/Valkey install, so it is the one most likely to already
hold another application's data, and a single FLUSHDB destroys both. The
pytest plugin refuses popoto_test_db=0 for the same reason; this is the
same rule on the write path.
Give the memory corpus a database of its own (database 3 is empty on this
server):
    export POPOTO_MEMORY_URL=redis://localhost:6379/3
Or, if database 0 really is where this corpus belongs, opt in at deploy time:
    export POPOTO_MEMORY_ALLOW_DB0=1
```

`REDIS_URL` inherited — first line becomes:

```
REDIS_URL=redis://localhost:6379/0 puts Popoto on Redis database 0, and
Popoto refuses to write agent memory there. [...]
```

and the fix line offers `POPOTO_MEMORY_URL` as the narrower override:

```
Point the memory corpus at a database of its own without moving the rest of
your application (database 3 is empty on this server):
    export POPOTO_MEMORY_URL=redis://localhost:6379/3
```

Neither set — first line becomes:

```
Popoto's default connection is Redis database 0, and Popoto refuses to write
agent memory there. [...]
```

When `suggest_free_db()` returns `None`, "(database 3 is empty on this
server)" is omitted and the example URL uses `/1`.

**Wording rules for the implementer:** the substring
`refuses to write agent memory` appears in all three variants and is what
tests assert on. `POPOTO_MEMORY_ALLOW_DB0` appears verbatim in all three.
Do not include a stack-trace-style prefix; `doctor` prints `str(exc)` raw.

### 5. How the refusal surfaces at each entry point

No entry point needs a new handler except `demo`. See the Data Flow table.
The one change:

`demo.py` — wrap the `MemoryConfig`/`MemoryService` construction
(`demo.py:49-56`) in `try/except ValueError`, print `str(exc)` to `out`, and
return 1. A demo that tracebacks on a first run teaches the wrong thing;
printing the guidance is the whole point of the demo.

`doctor` — no code change required (its `except ValueError` at `cli.py:213`
already covers `Db0RefusedError`). Add `url_source` to `status()`'s dict and
to the human-readable report so a healthy run also shows where the URL came
from. This is the only additive change to `doctor`.

### 6. Log-line hygiene (`hooks.py`)

`_log_hook_error` (`hooks.py:293-313`) writes
`f"{stamp} {operation} {type(exc).__name__}: {exc}\n"`. The refusal message
is multi-line, so it would write several physical lines per failure, and
`MemoryService.log_tail(lines=5)` would then show only its tail — cutting off
the sentence that says what to do.

Fix: collapse whitespace runs in the message before writing.

```python
detail = " ".join(str(exc).split())
handle.write(f"{stamp} {operation} {type(exc).__name__}: {detail}\n")
```

One log line per failure, `log_tail` stays honest, and every existing
single-line error is unaffected.

## Test Impact

All new tests go in `tests/test_integrations_db0_isolation.py`, which
already owns this invariant and already has the `_db0()` read-only sibling
client, the `_env()`/`_run()` subprocess helpers, and the `purge_test_agent`
fixture.

**Tests must not write to database 0 — including the tests for this
feature.** That is straightforward here because the feature under test is
precisely a refusal to write: the assertions are on the raised exception and
on the subprocess's stdout/log, never on Redis state in database 0. The
existing `db0_unchanged` fixture (key-signature comparison, not `DBSIZE`)
still guards every subprocess test. No new test opens a writable database-0
client.

The suite itself runs on DB 15 under the pytest plugin, and the guard's
non-explicit branch reads the *live* connection, so no existing test changes
behavior. Confirm by running the full integrations set before and after.

New cases:

| Test | Asserts |
|---|---|
| `test_db0_is_refused_when_explicitly_configured` | `MemoryConfig(url=".../0", url_is_explicit=True)` → `bind_connection` raises `Db0RefusedError`; message contains `refuses to write agent memory` and `POPOTO_MEMORY_ALLOW_DB0`. |
| `test_db0_refusal_names_the_variable_that_caused_it` | The three `url_source` variants each produce their own first line (`POPOTO_MEMORY_URL=`, `REDIS_URL=`, `Popoto's default connection`). |
| `test_the_optin_env_var_permits_db0` | Same config plus `POPOTO_MEMORY_ALLOW_DB0=1` → `bind_connection` returns without raising. Subprocess, so the bind targets DB 0 in a throwaway process; it constructs the service and exits **without calling save/assemble**, so nothing is written. `db0_unchanged` still applies. |
| `test_the_optin_accepts_the_documented_truthy_set` | `1`, `true`, `yes`, `on` (and case variants) opt in; `0`, `false`, empty, unset do not. Pure `MemoryConfig.from_env` — no Redis. |
| `test_a_nonzero_database_is_unaffected` | `.../15` binds as before; guard is invisible. |
| `test_the_existing_connection_is_consulted_not_the_default_url` | With no `POPOTO_MEMORY_URL` and the process on DB 15, constructing a `MemoryService` does **not** raise. This is the regression that would break the suite. |
| `test_the_refusal_reaches_the_hook_as_exit_zero_plus_one_log_line` | Subprocess `cli hook` with `POPOTO_MEMORY_URL=.../0`: exit 0, empty stdout, log exists, log contains the marker, **and the log gained exactly one line** (the §6 hygiene fix). |
| `test_doctor_reports_the_refusal` | `cli doctor` and `cli doctor --json` with `POPOTO_MEMORY_URL=.../0`: exit 1, message present, no traceback. |
| `test_demo_reports_the_refusal` | `cli demo` with `POPOTO_MEMORY_URL=.../0`: exit 1, message on stdout, no traceback. |
| `test_mcp_dispatch_reports_the_refusal` | `dispatch("memory_search", ...)` with no service → error result carrying the message (marked `mcp` extra, skipped without it, like the rest of `test_integrations_mcp.py`). |
| `test_suggest_free_db_is_advisory_and_never_raises` | Returns an int in 1..15 or `None`; with a client whose `info` raises, returns `None`. Never rebinds: `POPOTO_REDIS_DB.connection_pool.connection_kwargs["db"]` is identical before and after. |

## Rabbit Holes

- **Do not extend the guard to `popoto.redis_db`.** Making the ORM core
  refuse DB 0 changes behavior for every existing 1.8.x user of the plain
  ORM, which is a different (and much larger) decision than the one settled
  on #584. Scope is `popoto.integrations`. #577 is where the broader guard
  question lives.
- **Do not build a database allocator.** `suggest_free_db` returns a number
  for a human to paste. No reservation, no registry, no persistence, no
  retry-on-collision.
- **Do not add per-database key namespacing** as an alternative to the
  refusal. Prefixing does not stop `FLUSHDB`, which is the failure mode that
  motivated the issue.
- **Do not touch `bind_connection`'s pool-swap mechanics.** The in-place
  pool swap exists because module-level `POPOTO_REDIS_DB` imports are bound
  for the life of the process (`config.py:213-220`). The guard runs before
  it and returns or raises; it does not restructure it.

## Risks

| Risk | Mitigation |
|---|---|
| Guard reads `config.url` instead of the live connection in the non-explicit case, refusing on every test and every in-process host app. | §2 pins the two-branch resolution and `test_the_existing_connection_is_consulted_not_the_default_url` fails loudly if it regresses. |
| A hook fires every turn, so a refused configuration appends a log line per turn. | Intended: the log is the hook's only channel and `doctor` reads it. §6 keeps it to one line per failure. The log has no rotation today and gains none here; note it in the docs as the signal to fix the config. |
| `suggest_free_db` adds an `INFO` round-trip to the error path. | Error path only, never the happy path. Guarded by the pool's existing 5s socket timeout and a blanket `except` returning `None`. |
| `Db0RefusedError` escaping to a user who upgrades from a pre-release build and had been on DB 0. | Accepted and intended — this is the "loud failure over silent amnesia" trade the maintainer chose. `popoto.integrations` ships first in 1.9.0, so no published adopter is affected. Release notes carry the one-line opt-in. |
| Adding fields to the frozen `MemoryConfig` dataclass breaks a positional construction somewhere. | Both fields are appended with defaults; `demo.py:53` and the tests use keywords. Grep for `MemoryConfig(` to confirm. |

## No-Gos (Out of Scope)

- Changing `DEFAULT_URL` (`config.py:42`). Explicitly rejected on #584.
- Any auto-relocation of writes to a different database.
- A constructor kwarg as the opt-in mechanism.
- Fixing #577 (ad-hoc scripts bypassing the guard).
- Any change to `popoto.redis_db`'s import-time connection, or to the pytest
  plugin.
- Log rotation for `~/.popoto/memory.log`.

## Documentation

| File | Change |
|---|---|
| `docs/features/harness-integration.md:196` | Add a `POPOTO_MEMORY_ALLOW_DB0` row to the variable table (default `0`). Annotate the `POPOTO_MEMORY_URL` row: the resolved default is database 0 and **is refused** unless opted in. |
| `docs/features/harness-integration.md:230` | The sample `doctor` output shows `redis url redis://localhost:6379/0`, which is now a refused configuration. Change the sample to `/1` and add the `url source` line `status()` now reports. |
| `docs/features/harness-integration.md` (troubleshooting) | New short section: "Popoto refuses database 0" — the message, the two ways out, and the pointer to `doctor`. |
| `docs/guides/harness-claude-code.md`, `docs/guides/harness-codex.md` | Setup steps must set `POPOTO_MEMORY_URL` to a non-zero database, since the zero-config path now refuses. This is the highest-impact doc change: these are the copy-paste paths. |
| `plugins/codex/config.toml.fragment:14` | Commented example `POPOTO_MEMORY_URL = "redis://localhost:6379/0"` → `/1`. A comment in a config fragment, not a silent repoint of a default. |
| `examples/harness_memory/README.md:68` | Corrects the claim that the fallback is "`REDIS_URL` if set and `localhost:6379/0`" — still true as a resolution, but now refused. |
| `docs/configuration.md:398` | The env-var table documents `POPOTO_TEST_DB`'s DB-0 rejection. Add the `POPOTO_MEMORY_ALLOW_DB0` row beside it and cross-reference: the two halves of the library now agree. |
| `src/popoto/integrations/config.py` module docstring | Add the variable to the table at the top; that table is the package's real reference. |
| `CLAUDE.md` | No change. Its DB-0 paragraph is about ad-hoc scripts and `REDIS_URL` import-time binding, which remains exactly true. |

Run `/do-docs` before merge (required SDLC stage).

## Success Criteria

- With no `POPOTO_MEMORY_URL`, no `REDIS_URL`, and a stock localhost Redis,
  constructing a `MemoryService` raises `Db0RefusedError`; nothing is
  written to database 0.
- The same environment plus `POPOTO_MEMORY_ALLOW_DB0=1` constructs and works
  exactly as before this change.
- `POPOTO_MEMORY_URL=redis://localhost:6379/3` works, unchanged.
- A hook turn under a refused configuration exits 0, writes nothing to
  stdout, and appends exactly one log line naming the fix.
- `popoto-memory doctor` prints the guidance and exits 1 rather than
  tracebacking; `--json` carries it in `error`.
- `DEFAULT_URL` is still `redis://localhost:6379/0` in the merged diff.
- No new public constructor argument.
- Full suite green on DB 15 with no test-count change other than the new
  tests. `ruff check src/` exits 0; `mypy src/` shows no new errors (state
  the redis-py version alongside the count — see CLAUDE.md).
- Zero writes to database 0 across the whole suite (`db0_unchanged` fixture).

## Step by Step Tasks

1. Branch `session/integrations_refuse_db0` off `main` in a worktree.
   Install `.[dev,embeddings,benchmark,mcp]` and confirm the editable
   install resolves to *this* checkout (CLAUDE.md worktree rules).
2. Capture the baseline: run `pytest tests/test_integrations_*.py` on the
   branch point and record the count and the redis-py version.
3. `config.py`: add `ALLOW_DB0_ENV`, `Db0RefusedError`, and the
   `url_source` / `allow_db0` fields on `MemoryConfig`. Resolve both in
   `from_env`. Do not touch `DEFAULT_URL`.
4. `config.py`: add `effective_db(config)` per §2, with the two-branch
   docstring spelling out why the live connection is consulted.
5. `config.py`: add `suggest_free_db()` per §3 — `INFO keyspace`,
   `sibling_client_kwargs`, blanket `except` → `None`, never rebinds.
6. `config.py`: add `_db0_refusal_message(config)` producing the three
   variants in §4, and call the guard at the **top** of `bind_connection`,
   before the `url_is_explicit` early return.
7. `hooks.py`: collapse whitespace in `_log_hook_error`'s detail (§6).
8. `demo.py`: wrap construction, print `str(exc)`, return 1 (§5).
9. `cli.py` / `service.py`: add `url_source` to `status()` and to the
   human-readable `doctor` report. No other change.
10. Write the new tests in `tests/test_integrations_db0_isolation.py` per
    Test Impact, reusing `_env`, `_run`, `_db0`, `db0_unchanged`.
11. Run the full suite on DB 15. Compare against step 2's baseline; any
    delta beyond the new tests is a regression to fix, not to explain.
12. Update the docs listed above; run `/do-docs`.
13. `ruff check src/`, `black src/ tests/`, `mypy src/`.
14. Open the PR with `Closes #584`.

## Verification

```bash
# baseline and after — same command, same environment
pytest tests/test_integrations_db0_isolation.py tests/test_integrations_service.py \
       tests/test_integrations_cli.py tests/test_integrations_hooks.py \
       tests/test_integrations_mcp.py -q

# the zero-config path now refuses, and writes nothing
# (REDIS_URL deliberately unset; this is the whole point)
env -u REDIS_URL -u POPOTO_MEMORY_URL -u POPOTO_TEST_DB \
  python -c "
from popoto.integrations import MemoryService
try:
    MemoryService()
except ValueError as exc:
    print('REFUSED:'); print(exc)
"

# the deploy-level opt-in restores the old behavior
env -u REDIS_URL -u POPOTO_MEMORY_URL POPOTO_MEMORY_ALLOW_DB0=1 \
  python -c "from popoto.integrations import MemoryService; MemoryService(); print('bound')"

# doctor is a report, not a traceback
env -u REDIS_URL POPOTO_MEMORY_URL=redis://localhost:6379/0 popoto-memory doctor; echo "exit=$?"

# DB 0 untouched by the whole exercise
redis-cli -n 0 --scan --pattern '*DefaultMemory*' | head
redis-cli -n 0 --scan --pattern '$popoto_memory:*' | head

# the rejected option did not sneak back in
grep -n 'DEFAULT_URL = ' src/popoto/integrations/config.py   # must still be /0

ruff check src/ && mypy src/
```

The two `redis-cli` scans must return nothing. Note that the first two
`python -c` invocations bind Popoto's connection to database 0 in a
throwaway process; neither performs a save, and the opt-in case constructs
the service without touching the memory model, so neither writes. Run them
in a subshell, never inside a session that later runs ad-hoc Popoto scripts
(CLAUDE.md, #577).

## Open Questions

1. **Suggestion range.** `suggest_free_db` scans databases 1..15. Servers
   configured with `databases 1` (some managed Redis offerings) have no
   valid target at all, and the message would fall back to suggesting `/1`,
   which fails on connect. Should the refusal detect that case and say
   "this server has only one database; set `POPOTO_MEMORY_ALLOW_DB0=1` or
   use a separate server"? Planned as *not* handled (extra probe on an
   error path for a rare deployment), but it is the one environment where
   the guidance is actively wrong.

2. **Release-note prominence.** 1.9.0 is `popoto.integrations`' first
   published release, so there is nothing to migrate — but a developer who
   ran the pre-release build already has a corpus in database 0 and will
   hit the refusal with no way to recover it except the opt-in. Should the
   error message mention that reading an existing database-0 corpus is
   exactly what `POPOTO_MEMORY_ALLOW_DB0=1` is for? Currently the message
   frames the opt-in as "if database 0 really is where this corpus
   belongs", which covers it obliquely.
