---
status: Ready
type: chore
appetite: Small
owner: Dev (sdlc-663)
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/663
last_comment_id:
revision_applied: true
---

# Make `mypy src/` resolve popoto's own package imports

## Problem

`setup.cfg` sets `ignore_missing_imports = True` and declares no `mypy_path`, so an absolute `import popoto` from inside `src/` does not resolve to this source tree — it degrades to `Any`. A typo'd attribute on the package is invisible to the gate.

#659 (merged `8151e8c0`) hid the PEP 562 `__getattr__` behind a `TYPE_CHECKING` guard so `attr-defined` checking works again for the `popoto` namespace. That fix is correct and verified at mypy 2.3.1, but its benefit is **latent** until the self-import resolves. This is the half with a cost.

**Desired outcome:** a planted typo on `popoto.<attr>` is an `attr-defined` error under a bare `mypy src/` — the actual gate, with no special invocation.

## Freshness Check

**Baseline commit:** `8151e8c0` (`main`, the #665/#659 merge)
**Issue filed at:** 2026-09-07, during #659's plan critique
**Disposition:** Unchanged. The issue was filed hours ago and its prerequisite (#659) merged since — which is the event that unblocks it. Every measurement in the issue body was re-taken against `8151e8c0` for this plan (see below); one of them has changed materially, and that change is the whole shape of this plan.

## Prior Art

- **#659 / PR #665** (merged `8151e8c0`) — the prerequisite. Without its `TYPE_CHECKING` guard, resolution buys nothing: a module `__getattr__` visible to the checker answers for every unknown attribute, so `popoto.Modle` would resolve to `Any` even with the import resolving. Verified both ways at plan time for #659.
- **#490 / PR #500** (merged) — introduced `redis_db.sibling_client_kwargs()`, the helper at the center of 20 of the 21 errors this plan must clear. Its `dict[str, object]` return type is deliberate and correct; the problem is only what happens when that dict is splatted into a typed constructor.
- **#506** — established that the mypy count is redis-py-version-dependent (52-error spread between 7.x and 8.x), which is why `scripts/mypy_ratchet.py` refuses to compare across environments and why every count below names its own.

**Why previous fixes did not cover this:** no failed prior attempt. The configuration has simply never been set, and until #659 there was no reason to set it.

### Which modules the change actually activates

#659's plan asserted that `pytest_plugin.py` is the only `src/` module importing the package absolutely. Re-checked at `8151e8c0`, that is wrong, and the correction is worth recording because it is easy to re-derive incorrectly. `grep -rn '^\s*\(import popoto\|from popoto\)' src/popoto/` returns roughly 75 hits, but the overwhelming majority are **indented inside docstrings** — the `Usage::` blocks in `redis_db.py` and the long migration guides in `models/migrations.py` are the two biggest sources, and mypy never sees any of them. Reading each hit in context leaves five real sites:

- `pytest_plugin.py:62` — `from popoto import redis_db` (module scope)
- `pytest_plugin.py:112`, `:126` — `import popoto as _popoto` / `as _canonical_popoto`, inside the alias-collapse helper
- `transfer/cli.py:218` — `from popoto import Model` (function-local)
- `transfer/cli.py:273` — `from popoto.redis_db import POPOTO_REDIS_DB` (function-local)

All five start being checked for real when resolution turns on. The measured 1042 already absorbs all five: `Model` and `redis_db` are genuine bindings in `popoto/__init__.py` and `POPOTO_REDIS_DB` is a genuine module attribute of `redis_db`, so none of them produces a new error. The two `transfer/cli.py` sites in particular gain real `attr-defined` coverage they did not have before, at zero cost. This does not change the delta — that remains the two sites in the table below — but the inventory above is the accurate map.

## The measurement that reshapes this plan

The issue says the config change "surfaces 21 pre-existing errors" and "needs a baseline re-bank". Re-measured at `8151e8c0`, the 21 is right and the conclusion is wrong. The delta is not 21 scattered problems — it is **two call sites**:

| site | count | error |
|---|---|---|
| `pytest_plugin.py:457` | **20** | `Argument 1 to "GuardedRedis" has incompatible type "**dict[str, object]"` — one per constructor parameter |
| `pytest_plugin.py:187` | **1** | `Cannot assign to a method  [method-assign]` |

Twenty of the twenty-one are a single `GuardedRedis(**pool_kwargs)` splat, reported once per parameter of the constructor. `sibling_client_kwargs()` returns `dict[str, object]` by design — it is a runtime whitelist, and mypy cannot know which key maps to which parameter. There is no annotation that makes this *check*; the only honest statement is that these are opaque connection parameters redis-py validates at call time.

Both fixes are one line each and both live in `pytest_plugin.py`, which this lane owns. Measured with both applied:

- `mypy src/` under the new config: **1042** — exactly the current baseline.
- `integrations` **0** and `privacy` **0**, so the ratchet's `clean` allowlist still holds.

**No baseline re-bank is required.** This matters beyond tidiness: a re-bank collides with every other lane measuring against 1042 (sdlc-556 is doing so right now). Landing this at a flat baseline costs no other lane anything.

## Solution

Three changes, all in files this lane owns.

**1. `setup.cfg`** — two keys in the `[mypy]` stanza:

```ini
[mypy]
mypy_path = src
explicit_package_bases = True
follow_imports = silent
...
```

`explicit_package_bases` is not optional decoration. `mypy_path = src` alone fails outright with `Source file found twice under different module names: "src.popoto.redis_db" and "popoto.redis_db"` — mypy cannot tell whether `src/popoto/` is the package `popoto` or the subpackage `popoto` of `src`. `explicit_package_bases` tells it to treat `mypy_path` entries as package roots.

**2. `pytest_plugin.py:450`** — annotate the local so the splat is not checked against `object`:

```python
pool_kwargs: dict[str, Any] = redis_db.sibling_client_kwargs(...)
```

Deliberately at the **call site**, not on `sibling_client_kwargs`'s return type. The helper's `dict[str, object]` is the stricter and more accurate signature, and widening it would weaken every other caller. `src/` has exactly one other caller — none; this is the only one (the rest are in `tests/`, which is `ignore_errors = True`). So the widening is confined to the single site that actually needs it, and a future caller starts from the strict signature.

**3. `pytest_plugin.py:187`** — a targeted ignore on a deliberate monkeypatch:

```python
pool.get_connection = wrapper  # type: ignore[method-assign]
```

This line is intentional: it arms the isolation tripwire by swapping a bound method on the pool. `method-assign` is mypy correctly reporting what the code deliberately does. A narrow, coded ignore is the right expression.

### The two changes are atomic — they cannot be split

`setup.cfg` sets `warn_unused_ignores = True`. Under the *current* config the `method-assign` ignore is unnecessary (the error does not fire, because `redis_db` degrades to `Any`), so adding it alone would itself be an error. The ignore is only valid once resolution is on. Config and fixes must land in the same commit; neither is separately mergeable.

## Scope: `py.typed` is explicitly NOT in this PR

The issue pairs self-import resolution with adding `src/popoto/py.typed`. This plan lands only the former. The marker is a published-API change that opts every downstream consumer into popoto's inline types, affecting people who cannot see this decision being made — it is not a call to make inside a lane. A recommendation with evidence is carried in the report to the supervisor and summarized under *Recommendation* below; the decision is theirs.

Part 1 is coherent without part 2. They target different audiences: resolution makes *this repo's own gate* see package-boundary typos, which is valuable whether or not any consumer ever type-checks against popoto.

## No-Gos

- Adding `src/popoto/py.typed`. Separate decision, separate owner.
- Annotating the public API surface (the 11 names enumerated under *Recommendation*). That is the `py.typed` prerequisite, not this.
- Fixing the 10 remaining `no-untyped-def` errors in `pytest_plugin.py`. They are pre-existing, present identically under both configs, and unrelated to resolution.
- Widening `sibling_client_kwargs()`'s return type.
- Re-banking `scripts/mypy_baseline.json`. Not needed — and doing it needlessly would disrupt sibling lanes.
- Touching `src/popoto/models/query.py`, `recipes/memory_lifecycle.py`, `fields/tombstone_store.py` (sdlc-649), or the `fields/` export/import carriers (sdlc-556).

## Rabbit Holes

- **Applying the `TYPE_CHECKING` guard to `integrations/` and `extraction/`.** Both still define unguarded module `__getattr__`s, so their namespaces stay suppressed. Real, and worth doing — but it is #659's shape applied to two more packages, not this issue. Measured: resolution does **not** move either package's count, because their suppression is internal to them and has nothing to do with import resolution. `integrations` stays at 0 and the `clean` pin holds.
- **Chasing the 1042.** The ratchet exists precisely so nobody has to.

## Test Impact

No new test file. This change has no runtime behavior and no API surface; its entire effect is on what `mypy src/` reports, and the ratchet already gates that.

The success criterion that matters — a planted typo produces `attr-defined` — is verified out-of-band with a throwaway probe (task 3), exactly as #659's was, because a permanent probe file in `src/` would be a deliberate error in the tree and would itself move the count.

Unchanged and must stay passing: `tests/test_pytest_plugin.py` (43), `tests/test_popoto_redis_db_rebind.py` (5), `tests/test_get_redis_rebind.py` (2), `tests/test_type_checking_guard.py` (3). The first is the one that matters — both edits are in the module it covers.

## Risks

- **`warn_unused_ignores` turns a later config revert into a hard error.** If someone removes `mypy_path`/`explicit_package_bases`, the `method-assign` ignore becomes unused and errors. This is a feature, not a hazard: it makes the coupling self-enforcing rather than a comment nobody reads. Noted at the ignore site. Both directions are covered — removing the *ignore* alone re-fires the error and pushes the total to 1043, above the 1042 ceiling, so the ratchet fails there too.
- **`dict[str, Any]` hides a real defect at that call site.** Accepted, and narrow: the dict is built by a whitelist from a live pool's `connection_kwargs` and splatted into a constructor that validates at runtime. Twenty type errors describing "mypy cannot correlate keys with parameters" are noise, not signal. The regression net is `tests/test_pytest_plugin.py`'s `sibling_client_kwargs` tests (#490), which check the actual key filtering.
- **A future `src/` module importing `popoto` absolutely inherits real checking and may surface new errors.** That is the point of the change. It raises the ratchet honestly if it happens.

## Step by Step Tasks

1. Add `mypy_path = src` and `explicit_package_bases = True` to `setup.cfg`'s `[mypy]` stanza.
   - Validate: `.venv/bin/python -m mypy src/ 2>&1 | tail -1` runs without `Source file found twice`.
2. Apply both `pytest_plugin.py` fixes, with a comment at the `method-assign` ignore naming the `warn_unused_ignores` coupling.
   - Validate: `.venv/bin/python scripts/mypy_ratchet.py --strict-ratchet` → **1042, at baseline 1042**, `integrations` and `privacy` both `0 [clean]`.
3. Out-of-band proof, recorded in the PR body. Write `src/popoto/_zz_probe.py` containing `import popoto`, `reveal_type(popoto.POPOTO_REDIS_DB)`, `popoto.Modle`; run a bare `.venv/bin/python -m mypy src/`; confirm `Revealed type is "popoto.redis_db.GuardedRedis"` and `Module has no attribute "Modle"`; **delete the probe**.
4. Run the narrow suite and the remaining gates.
   - Validate: `POPOTO_TEST_DB=7 .venv/bin/python -m pytest tests/test_pytest_plugin.py tests/test_popoto_redis_db_rebind.py tests/test_get_redis_rebind.py tests/test_type_checking_guard.py -q`; `ruff check src/`; `black --check src/ tests/`
5. Docs cascade: `CLAUDE.md` (the ratchet paragraph and the #659 continuation, which currently says `mypy src/` cannot resolve `import popoto` — that becomes false); `CHANGELOG.md` under Unreleased.
   - Validate: `mkdocs build --strict`

## Documentation

- `CLAUDE.md` — the ratchet paragraph gains the resolution fact; the #659 bullet continuation's "does not yet buy" clause must be corrected, since half of it stops being true.
- `CHANGELOG.md` — Unreleased.
- No user-facing docs page changes: this is repo-internal type checking configuration with no API surface.

## Recommendation on `py.typed` (for the supervisor — not shipped here)

**Recommend shipping it, but not yet, and not gated on the error total.**

The instinct is to refuse because `src/` measures 1042 errors. That is the wrong metric, and gating on it means never shipping: `py.typed` asserts that popoto's *public signatures* are honest, not that its internals type-check. Those are different questions.

Measured against the 92 names in `popoto.__all__` (Python 3.12.14, introspection of live signatures):

- **83 of 92 are already fully annotated** — 76 of 83 classes have a completely annotated `__init__`, and 5 of 9 module-level functions are complete.
- **The gap is 11 names, and it is enumerable:**
  - Functions: `get_redis` (no return type), `get_async_redis_db` (no return type), `configure` (`embedding_provider`, `content_store`, no return type), `report_outcomes` (four params, no return type).
  - Classes: `Expression`, `CombinedExpression`, `ContextAssembler`, `TelemetryRecorder`, `TelemetryAnalyzer`, `ContentField`, `EmbeddingField`.

`get_redis()` is the sharpest of these: #645 and #659 both route users to it as *the* correct accessor, and it currently promises nothing. A consumer who opts into popoto's types would get `Any` from the one call the docs tell them to make.

**Proposed sequence:** (1) close the 11-name gap as its own issue; (2) ship `py.typed` in a **minor** release with a changelog note, never a patch. The residual risk after (1) is *wrong* public annotations rather than missing ones — a missing annotation degrades to `Any` and is harmless to consumers, while a wrong one propagates into their code. No cheap audit covers that, which is the honest argument for a minor-release boundary and an explicit note rather than a silent marker.

## Success Criteria

- [ ] Under a bare `mypy src/`, a planted `popoto.Modle` is an `attr-defined` error and `popoto.POPOTO_REDIS_DB` reveals `popoto.redis_db.GuardedRedis` (task 3, recorded in the PR body).
- [ ] `scripts/mypy_ratchet.py --strict-ratchet` → 1042, at baseline 1042. **`scripts/mypy_baseline.json` unmodified.**
- [ ] `integrations` and `privacy` both report `0 [clean]`.
- [ ] `tests/test_pytest_plugin.py` passes unchanged (43).
- [ ] `ruff`, `black`, `mkdocs --strict` clean; CI green.
- [ ] No `py.typed` in the diff.
