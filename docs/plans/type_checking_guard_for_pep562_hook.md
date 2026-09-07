---
status: Ready
type: chore
appetite: Small
owner: Dev (sdlc-651)
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/659
last_comment_id:
---

# Hide the PEP 562 hook from mypy with a `TYPE_CHECKING` guard

## Problem

PR #657 (#651) replaced `from .redis_db import POPOTO_REDIS_DB` in `src/popoto/__init__.py` with a module `__getattr__`, so the attribute resolves the live connection on every access. That fix is correct and stays.

Its cost, disclosed at the time: a module `__getattr__` annotated `-> Any` tells mypy that *every* attribute of the package exists and is `Any`. `popoto.Modle` stopped being an `attr-defined` error the moment #657 landed — for the whole namespace, not just the one dynamic name.

**Desired outcome:** `attr-defined` checking is restored for every attribute of `popoto` except `POPOTO_REDIS_DB`, which is typed as what it actually is, with zero runtime change.

## Freshness Check

**Baseline commit:** `af227894` (`main`, the #657 merge)
**Issue filed at:** 2026-09-07, off the back of PR #657
**Disposition:** Unchanged — the issue was filed hours ago against the commit this branch is based on.

## Solution

```python
if TYPE_CHECKING:
    from .redis_db import GuardedRedis

    POPOTO_REDIS_DB: GuardedRedis
else:

    def __getattr__(name: str) -> Any: ...
```

`__dir__` stays outside the guard — it is a genuine runtime hook with no static consequence.

Both halves are load-bearing, in opposite directions:

- The `if` branch must **declare**, never bind. `TYPE_CHECKING` is `False` at runtime so the branch does not execute, and a bare annotation binds nothing even where it does. `vars(popoto)` stays clear and the hook still fires. Replacing the annotation with an import would type-check identically and silently restore #651.
- The `else` branch must **hide** the hook. Declaring the name while leaving `__getattr__` at module level restores nothing: the hook still answers for every unknown attribute, so typos resolve to `Any` again.

`GuardedRedis` is the honest annotation: every assignment site in `redis_db.py` (lines 413, 425, 475, 480) constructs one, and `get_REDIS_DB()` returns that global (it is itself unannotated).

## Verification that the guard works at mypy 2.3.1

The issue's proof was measured at mypy 2.1.0; the gate pins 2.3.1. Re-verified in the gate's environment (Python 3.12.14, mypy 2.3.1, redis-py 8.1.0):

| | `reveal_type(pkg.DYNAMIC)` | `pkg.TYPO` |
|---|---|---|
| Hook at module level | `Any` | no error |
| Hook under `else` + declaration | concrete type | `attr-defined` error |

Against popoto itself, with the package resolvable: `Revealed type is "popoto.redis_db.GuardedRedis"` and `error: Module has no attribute "Modle"; maybe "Model"?  [attr-defined]`. The guard holds at 2.3.1.

## What this does NOT buy, today

Two separate facts keep the restored checking latent. Both are pre-existing and neither is caused or worsened by this change:

1. **The repo's own gate cannot see it.** `setup.cfg` sets `ignore_missing_imports = True` and declares no `mypy_path`, so an absolute `import popoto` inside `src/` does not resolve to this source tree — it degrades to `Any`. A deliberate `popoto.Modle` planted in `src/popoto/` produces no error under `mypy src/`, before or after this change.
2. **Downstream consumers get nothing either.** There is no `src/popoto/py.typed`, so PEP 561 makes type checkers ignore popoto's inline types for installed consumers.

Making (1) live means `explicit_package_bases = True` + `mypy_path = src`. Measured: that resolves the self-import correctly but surfaces **21 pre-existing errors**, all in `src/popoto/pytest_plugin.py` (the only `src/` module importing the package absolutely at runtime), taking `mypy src/` from 1042 to 1063. That is a real, separate change requiring a baseline re-bank; it is a No-Go here.

This change is therefore the **prerequisite half**, not a no-op: under that future config *without* this guard, `popoto.Modle` still resolves to `Any` and the config change buys nothing. The guard is what makes it worth making.

## No-Gos

- Changing `setup.cfg` mypy configuration. Separate change, +21 errors, needs a re-bank.
- Adding `src/popoto/py.typed`. A published-API decision, not a typing cleanup.
- Fixing the 21 `pytest_plugin.py` errors.
- Touching the runtime behavior of `__getattr__`, `__dir__`, or `get_redis()`.
- Modifying `tests/test_popoto_redis_db_rebind.py`.

## Rabbit Holes

- **Annotating `get_REDIS_DB()`.** Tempting, since it is the accessor the hook calls and it is unannotated. Out of scope; it would move the ratchet.
- **Making the ratchet enforce the restored checking.** Nothing to enforce until the config change lands.

## Test Impact

New: `tests/test_type_checking_guard.py`.

The behavioral half is weak on its own — lifting `__getattr__` out of the `else` breaks *nothing* at runtime, so no behavioral test can catch the regression this change exists to prevent. The file therefore asserts the source **shape** with `ast`:

1. `__getattr__` is in the guard's `orelse` and absent from module level.
2. `POPOTO_REDIS_DB` is a bare `AnnAssign` under the `if` — no value, and not an import.
3. Runtime: `TYPE_CHECKING is False`, `__getattr__` is in `vars(popoto)`, `POPOTO_REDIS_DB` is not, and both `popoto.__getattr__("POPOTO_REDIS_DB")` and `popoto.POPOTO_REDIS_DB` are `redis_db.get_REDIS_DB()`.

Unchanged and must stay passing: `tests/test_popoto_redis_db_rebind.py` (5), `tests/test_get_redis_rebind.py` (2), `tests/test_pytest_plugin.py` (43).

## Risks

- **A future editor collapses the distinction.** The `__init__.py` warning says the hook fires *only* because nothing above binds the name; a `TYPE_CHECKING` annotation does not bind, but that is exactly the nuance someone will flatten into an import. Mitigated by test (2) above and by comments at both the guard and the import block naming which test fails.
- **`GuardedRedis` drifts.** If `redis_db` ever assigns a plain `redis.Redis`, the annotation becomes a lie mypy cannot catch (the global is unannotated). Accepted; all four current assignment sites construct `GuardedRedis`.

## Step by Step Tasks

1. Add `TYPE_CHECKING` to the `typing` import; wrap the hook in `if TYPE_CHECKING: … else:` with the `GuardedRedis` declaration; leave `__dir__` at runtime. Update the comments at the guard, the hook docstring, and the import block so the "nothing above binds the name" warning stays accurate and names the annotation as a non-binding.
   - Validate: `python -c "import popoto; assert 'POPOTO_REDIS_DB' not in vars(popoto) and 'POPOTO_REDIS_DB' in dir(popoto)"`
2. Add `tests/test_type_checking_guard.py` per Test Impact.
   - Validate: `POPOTO_TEST_DB=7 pytest tests/test_type_checking_guard.py -q`
3. Run the regression net and the gates.
   - Validate: `POPOTO_TEST_DB=7 pytest tests/test_type_checking_guard.py tests/test_popoto_redis_db_rebind.py tests/test_get_redis_rebind.py tests/test_pytest_plugin.py -q`; `ruff check src/`; `black --check src/ tests/`; `scripts/mypy_ratchet.py --strict-ratchet`
4. Docs cascade: record in `CLAUDE.md` (the `popoto.POPOTO_REDIS_DB` bullet) that the hook is `TYPE_CHECKING`-guarded and why both halves must stay put; `CHANGELOG.md` under Unreleased.
   - Validate: `mkdocs build --strict`

## Documentation

- `CLAUDE.md` — extend the existing `popoto.POPOTO_REDIS_DB` bullet in the four-call-shapes list.
- `CHANGELOG.md` — Unreleased.
- No user-facing docs page changes: this is internal typing hygiene with no API surface.

## Success Criteria

- [ ] `popoto.POPOTO_REDIS_DB` reveals `popoto.redis_db.GuardedRedis` and `popoto.Modle` is an `attr-defined` error, when the package resolves.
- [ ] Runtime unchanged: name absent from `vars(popoto)`, present in `dir(popoto)`, resolves live.
- [ ] `tests/test_popoto_redis_db_rebind.py` passes **unchanged**.
- [ ] mypy ratchet flat at 1042 (Python 3.12.14, mypy 2.3.1, redis-py 8.1.0).
- [ ] `ruff`, `black`, `mkdocs --strict` clean; CI green.
