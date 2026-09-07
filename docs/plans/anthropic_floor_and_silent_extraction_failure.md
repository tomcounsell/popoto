---
status: Planning
type: bug
appetite: Small
owner: Dev (SDLC lane sdlc-670)
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/670
last_comment_id:
---

# anthropic floor understates the code's real requirement, and the mismatch fails silently

## Problem

`pyproject.toml:70-71` declares the optional extra as `anthropic>=0.40.0`. Three
call sites pass `output_config={"format": {"type": "json_schema", ...}}` to
`messages.create`:

- `src/popoto/extraction/claude.py:165`
- `src/popoto/extraction/resolution.py:458`
- `src/popoto/extraction/verdict.py:304`

`output_config` does not exist on `messages.create` in anthropic 0.40.0. A
consumer who runs `pip install popoto[anthropic]` in an environment that
resolves near the declared floor gets a `TypeError: create() got an unexpected
keyword argument 'output_config'`.

**Two distinct defects, both in scope:**

1. **The declared floor is wrong**, by 37 minor versions (see Spike Results).
2. **`claude.py` converts the resulting error into a silent no-op.** The call is
   wrapped in a blanket `except Exception` at `claude.py:177-179` that logs at
   WARNING and returns `[]`. `[]` is also the success value for "this text
   contained no extractable facts", so a too-old SDK presents as *degraded
   extraction that looks like normal operation* rather than as a failure. This
   is the more damaging half: the floor error alone would be a loud crash.

The two sibling call sites are **not** silent in the same way, and this
distinction shapes the fix. `resolution.resolve_references` returns a
`ResolutionStatus`-carrying degraded result (`resolution.py:806-813`) and
`verdict.llm_verdict` returns `ReasonCode.LLM_UNAVAILABLE`
(`verdict.py:402-407`). Both return values are already distinguishable from
success by the caller. Only `claude.py`'s bare `[]` is ambiguous.

**Desired outcome:** the declared floor matches what the code actually calls,
and an SDK too old to satisfy that call fails in a way a caller can detect —
never as an empty fact list.

## Freshness Check

**Baseline commit:** `bcdfe883` (`chore(deps): bump sentence-transformers from
5.6.0 to 6.0.0 (#676)`)
**Issue filed at:** 2026-09-07T03:49:11Z
**Disposition:** **Unchanged**

**File:line references re-verified:**

- `pyproject.toml:70-71` — `anthropic = ["anthropic>=0.40.0"]` — **still holds**,
  verbatim.
- `src/popoto/extraction/claude.py:160-167` — the `messages.create` call with
  `output_config` — **still holds**, verbatim.
- `src/popoto/extraction/claude.py:177-179` — the blanket
  `except Exception as e: logger.warning(...); return []` — **still holds**,
  verbatim.
- `src/popoto/extraction/claude.py:25`, `resolution.py:47`, `verdict.py:55` —
  the three guarded `import anthropic` sites — **still hold**.
- `src/popoto/embeddings/openai.py:50,72-74` — the issue's claim that the
  sibling `openai` extra needs no action — **confirmed**; `OpenAI(api_key=...)`
  and `embeddings.create(input=..., model=...)` are stable since 1.0.0.

**Commits touching the referenced files since the issue was filed:** none.
`git log --since=2026-09-07T03:49:11Z -- src/popoto/extraction/ pyproject.toml`
is empty.

**Cited sibling issues/PRs re-checked:**

- **#667** — MERGED: `chore(deps): bump anthropic from 0.120.2 to 1.2.0`. Moves
  `uv.lock` only, not the declared floor. `uv.lock:211` is now `1.2.0` while
  `uv.lock:2996` still records `specifier = ">=0.40.0"`. This *widens* the gap
  between what developers run and what the floor permits — it confirms the
  issue's premise rather than overtaking it.
- **#669** — CLOSED: *"Root uv.lock is gated for self-consistency but never
  installed by CI, so dependency bumps land unexercised"*. The issue correctly
  notes this defect survives #669's fix, because it is a floor-declaration error
  and not a lockfile one.

**Active plan overlap:** none. No plan in `docs/plans/` touches
`src/popoto/extraction/` or the `anthropic` extra.

## Research

Anthropic SDK structured outputs, verified against the bundled `claude-api`
skill (authoritative over training priors, per its own header):

- **`output_config: {format: {...}}` is the current GA shape** for structured
  outputs on `messages.create()`. The older top-level `output_format` parameter
  is deprecated. This confirms the call sites are written correctly and the
  remedy belongs in the floor, not in the call.
- Assistant-turn prefill — the obvious pre-structured-outputs fallback for
  getting JSON back — **returns a 400** on the entire Opus 4.6+/Sonnet 4.6+/
  Opus 5 family, which includes this code's pinned `EXTRACTION_MODEL`
  (`claude-opus-4-8`). The `claude.py` docstring at line 116-119 already records
  this ("no tool_use, no assistant-turn prefill (which 400s on this model
  family)").

**How this informs the approach:** it rules out the issue's fix direction 2
(feature-detect and fall back to a non-`output_config` request). The natural
fallback for an SDK old enough to lack `output_config` is prefill, and prefill
is rejected by the pinned model. A fallback path would have to be
prompt-instructed free-text JSON with no schema enforcement — a materially
different extraction quality, on a code path no test can exercise without a live
API key. See No-Gos.

## Spike Results

### spike-1: What is the exact minimum anthropic version providing `output_config`?

- **Assumption**: "The true floor lies in `(0.69.0, 0.100.0]`" (the issue's
  bracketing).
- **Method**: prototype — download every published wheel in `[0.60.0, 0.100.0]`
  and test for `output_config` in
  `anthropic/resources/messages/messages.py`; then confirm the boundary by
  installing the two adjacent versions into clean venvs and inspecting
  `inspect.signature(Anthropic(api_key=...).messages.create).parameters`.
- **Result**: **0.77.0**. Scanned 49 releases from 0.60.0 through 0.100.0; the
  transition is clean and monotonic with no re-introduction:
  - `0.60.0` … `0.76.0` — **absent** (22 releases)
  - `0.77.0` … `0.100.0` — **present** (27 releases)

  Signature confirmation in clean venvs on **Python 3.12.14**:

  | anthropic | `output_config` in `messages.create` signature |
  |---|---|
  | 0.76.0 | **False** |
  | 0.77.0 | **True** |

- **Confidence**: **high**. Two independent methods (wheel source scan, live
  signature inspection) agree, and the range scanned extends 16 releases below
  the boundary with no non-monotonicity.
- **Impact if false**: the floor number changes; nothing else in the plan does.

### spike-2: Is `claude.py` really the only silent call site?

- **Assumption**: "All three `output_config` call sites fail silently."
- **Method**: code-read of the three error paths.
- **Result**: **False — only `claude.py` is silent.** `resolution.py:806-813`
  returns a degraded `ResolutionStatus`; `verdict.py:402-407` returns
  `ReasonCode.LLM_UNAVAILABLE`. Both are distinguishable from success at the
  return value. `claude.py` returns `[]`, which collides with its own success
  value for "no facts found".
- **Confidence**: **high**.
- **Impact if false**: would have required the same return-value surgery in
  three modules instead of a construction-time guard in one.

### spike-3: Does the fix need to touch `uv.lock`?

- **Assumption**: "Changing a `pyproject.toml` specifier requires regenerating
  `uv.lock`."
- **Method**: code-read of `uv.lock` and the lock CI gate.
- **Result**: **True.** `uv.lock:2996` records
  `{ name = "anthropic", marker = "extra == 'anthropic'", specifier = ">=0.40.0" }`
  — the specifier is embedded in the lockfile's requirements manifest, and
  `lock-check.yml` runs `uv lock --check`. Editing `pyproject.toml` alone breaks
  that gate. The resolved pin (`uv.lock:211`, `1.2.0`) already satisfies
  `>=0.77.0`, so `uv lock` should rewrite the specifier line and nothing else.
- **Confidence**: **high**.
- **Impact if false**: an extra CI round.

## Prior Art

- **#667** (MERGED) — bumped the anthropic *lock pin* 0.120.2 → 1.2.0. Precedent
  that the resolved version moves freely; the floor has never been touched.
- **#669** (CLOSED) — established that the root `uv.lock` is gated for
  self-consistency but never installed by CI. This is why the 0.40.0 floor
  survived: every developer and CI environment resolved to a modern anthropic
  via the lock, so nothing ever exercised the floor.
- **No prior fix attempts.** The `anthropic` extra has been `>=0.40.0` since it
  was introduced. There is no "Why Previous Fixes Failed" section because there
  are no previous fixes.
- **Expected-failure search**: `grep -rn 'pytest.mark.xfail\|pytest.xfail('
  tests/` returns nothing. No xfail markers to convert.

## Data Flow

The failure path, end to end, on a too-old SDK:

```
caller
  └─> ClaudeExtractionProvider(api_key=...)          # claude.py:139-146
        └─> anthropic_module.Anthropic(api_key=...)  # succeeds on ALL versions
  └─> provider.extract(text)                          # claude.py:148
        └─> self._client.messages.create(..., output_config=...)   # :160-167
              └─> TypeError: unexpected keyword argument 'output_config'
        └─> except Exception -> logger.warning(...) -> return []    # :177-179
  <─ []      # INDISTINGUISHABLE from "no facts in this text"
```

The key observation is on the second line: **`Anthropic(api_key=...)` succeeds
on every version from 0.40.0 through 1.2.0**, so construction is currently a
silent pass-through. That makes `__init__` the correct seam — it is the last
point before the ambiguous return value exists, and moving the failure there
costs nothing that currently works.

The two sibling modules build their client in a `_default_client()` helper
(`resolution.py:413-420`, `verdict.py:273-280`) which is the equivalent seam,
and which already raises `ImportError` for the missing-package case. Adding the
version check there puts both failure modes in one place per module.

## Solution

Four parts, all Small.

### 1. Raise the declared floor

`pyproject.toml`: `anthropic>=0.40.0` → `anthropic>=0.77.0`, then regenerate
`uv.lock` (spike-3). The resolved pin does not move.

**Published-library cost, stated explicitly** (the issue's second acceptance
criterion): popoto is published to PyPI, and a raised floor in
`[project.optional-dependencies]` propagates to every downstream consumer who
installs `popoto[anthropic]`. Per `CLAUDE.md`, this is exactly why Dependabot's
root lane is `versioning-strategy: lockfile-only` and never machine-edits
floors. This raise is nevertheless correct, because the current floor does not
describe a *working* configuration — it describes a range in which the feature
is broken. Raising it removes broken installs from the resolvable set rather
than removing working ones. The cost is borne only by a consumer pinned below
0.77.0, and for that consumer `popoto[anthropic]` does not currently function.

The raise is to **0.77.0, the measured minimum** — not to the locked 1.2.0 and
not to latest. Every version from 0.77.0 up satisfies all three call sites.

### 2. A shared capability check

New module `src/popoto/extraction/_anthropic_compat.py`, private, holding the
one fact all three call sites depend on:

- `MINIMUM_ANTHROPIC_VERSION = "0.77.0"` — with the spike-1 evidence recorded in
  the docstring, so the number is never re-derived by guess.
- `REQUIRED_CREATE_PARAMS: frozenset[str]` — the exact keyword arguments the
  three call sites pass to `messages.create`: `model`, `max_tokens`, `system`,
  `messages`, `output_config`.
- `class AnthropicVersionError(RuntimeError)` — a typed, catchable error
  distinct from `ImportError` (package absent) so a caller can tell "not
  installed" from "installed but too old".
- `def assert_messages_create_supported(client) -> None` — introspects
  `inspect.signature(client.messages.create)` and raises
  `AnthropicVersionError` naming the missing parameters and the minimum version.

Two deliberate escape hatches in the check, both load-bearing for the existing
test suite:

- **A signature accepting `**kwargs` (`VAR_KEYWORD`) passes unconditionally.** A
  callable that accepts arbitrary keywords cannot be shown to reject
  `output_config`. Every fake client in `tests/` is of this shape, and treating
  them as too-old would break tests that have nothing to do with versions.
- **If the signature cannot be introspected at all** (`ValueError`/`TypeError`
  from `inspect`, e.g. a C-implemented or heavily proxied callable), the check
  returns without raising. The check exists to catch a specific, known
  incompatibility, not to police client objects.

### 3. Wire the check into the three seams

- `ClaudeExtractionProvider.__init__` (`claude.py:139-146`), immediately after
  constructing the client. This is the fix for the silent-`[]` defect: the
  provider can no longer be constructed against an SDK that cannot serve it, so
  a version mismatch can never reach `extract()` and can never present as an
  empty fact list. **`extract()`'s documented "never raises" contract is
  preserved unchanged** — the raise happens strictly earlier.
- `resolution._default_client()` (`resolution.py:413-420`) and
  `verdict._default_client()` (`verdict.py:273-280`), alongside the existing
  `ImportError`. These two already surface failure distinguishably, so this is
  not a behavior fix — it converts a confusing downstream `TypeError` in the
  log into a precise "anthropic X is too old, need >= 0.77.0" message at the
  point of construction. Their blanket handlers still catch it and still return
  the same degraded value, so no caller contract changes.

### 4. Tests that keep the floor honest

New `tests/test_anthropic_floor.py`. The load-bearing one is TC2: it is the
issue's fourth acceptance criterion, and it must work in CI where `anthropic` is
**not installed** (it is not in the `dev` extra), so it is written as an AST scan
of the source rather than a live signature check.

## Rabbit Holes

- **Bisecting by installing all 49 versions.** Wheel-source scanning found the
  boundary in one pass; only the two adjacent versions needed real venvs. Do not
  re-run the full install sweep.
- **Auditing every other anthropic API surface for floor accuracy.** The issue
  scopes this to `output_config`. `Anthropic(api_key=...)` and `messages.create`
  themselves are stable across 0.40.0 → 1.2.0, including the 0.x → 1.x major.
  Widening to a full SDK-surface audit is a different, larger issue.
- **Making `extract()` return a richer type** so *all* failures are
  distinguishable from "no facts", not just version mismatches. Defensible, but
  it is a public API change to `AbstractExtractionProvider` affecting every
  provider. The acceptance criterion asks only that a *version mismatch* be
  distinguishable, and construction-time raising achieves that without touching
  the interface.
- **Fixing the blanket `except Exception` generally.** Narrowing it properly
  means enumerating what the Anthropic SDK can raise, which is a moving target
  and would make `extract()` raise where it currently promises not to.

## No-Gos

- **No feature-detect-and-fall-back path** (the issue's fix direction 2).
  Rejected on Research evidence: the fallback for a pre-0.77.0 SDK would be
  assistant-turn prefill, which returns 400 on the pinned `claude-opus-4-8`, so
  the only remaining fallback is unenforced prompt-instructed JSON — different
  extraction quality, on a path untestable without a live API key. Keeping one
  well-tested path beats two paths where the second is unexercised.
- **No change to the `openai` extra.** Verified stable; the issue says so and
  the Freshness Check confirms it.
- **No change to the resolved `uv.lock` pin.** It is already 1.2.0 and satisfies
  the new floor; only the recorded specifier line moves.
- **No release, no tag, no PyPI publish.** The issue itself notes a
  release/publish decision belongs to Tom. This lane changes the floor and adds
  the guard; it does not ship them.
- **No change to `EXTRACTION_MODEL` or any pinned constant.**

## Risks

- **The floor raise breaks a downstream consumer pinned below 0.77.0.** Real but
  narrow: such a consumer's `popoto[anthropic]` is already non-functional, so
  the raise converts a silent failure into an honest resolver error. Mitigation
  is the CHANGELOG entry.
- **The capability check false-positives on a legitimate client.** Mitigated by
  the two escape hatches in Solution §2 (`**kwargs` passes; un-introspectable
  passes). TC4 pins the `**kwargs` case so a future tightening cannot silently
  break every fake client in the suite.
- **`uv lock` rewrites more than the one specifier line.** Mitigated by
  inspecting the diff before committing and reverting anything unrelated; the
  resolved pin already satisfies the new floor so there should be nothing to
  re-resolve.
- **The AST scan in TC2 goes stale if a call site is restructured** (e.g. kwargs
  built into a dict and splatted). The test would stop seeing the parameters
  rather than fail. Mitigated by asserting the scan actually *found* all three
  call sites and a non-empty kwarg set at each, so a restructure fails loudly
  instead of silently passing.

## Success Criteria

- `pyproject.toml` declares `anthropic>=0.77.0`; `uv lock --check` passes.
- Constructing `ClaudeExtractionProvider` against a client whose
  `messages.create` lacks `output_config` raises `AnthropicVersionError` —
  it does not return, and `extract()` is never reached.
- `extract()` still returns `[]` (never raises) for blank input, no-facts
  responses, and API failures — the existing contract is unchanged.
- `tests/test_anthropic_floor.py` passes with `anthropic` **not installed**
  (the CI condition).
- Existing `tests/test_extraction.py`, `tests/test_auditable_extraction.py`, and
  `tests/test_raw_turn_extraction.py` pass unchanged — the `**kwargs` escape
  hatch means no existing fake client needs editing.
- `ruff check src/` exits 0; `black --check src/ tests/` passes.
- `scripts/mypy_ratchet.py` does not exceed the baseline read at measurement
  time.

### Test cases

| ID | Test | Asserts |
|---|---|---|
| TC1 | floor matches constant | the `anthropic>=` specifier parsed from `pyproject.toml` equals `MINIMUM_ANTHROPIC_VERSION`. Catches the two drifting apart in either direction. |
| TC2 | call sites stay within the floor's surface | AST-scan `claude.py`, `resolution.py`, `verdict.py` for `messages.create` calls; every keyword passed is in `REQUIRED_CREATE_PARAMS`; all three call sites are found with a non-empty kwarg set. **This is the issue's AC4** — a call site gaining a parameter fails here. |
| TC3 | version mismatch is not an empty result | a fake client whose `messages.create` has an explicit signature *without* `output_config` makes `ClaudeExtractionProvider(...)` raise `AnthropicVersionError`. Directly pins AC3. |
| TC4 | `**kwargs` clients are accepted | a fake client with `def create(self, **kwargs)` constructs fine. Guards against breaking every existing test fake. |
| TC5 | un-introspectable clients are accepted | a client whose `messages.create` defeats `inspect.signature` constructs fine. |
| TC6 | sibling seams are guarded | `resolution._default_client` / `verdict._default_client` raise `AnthropicVersionError` on a too-old monkeypatched `anthropic_module`, and `resolve_references` / `llm_verdict` still return their degraded values (contract unchanged). |
| TC7 | live signature agreement (skipped in CI) | `skipif` anthropic not installed: the installed version is `>= MINIMUM_ANTHROPIC_VERSION` and every name in `REQUIRED_CREATE_PARAMS` is in the real `messages.create` signature. |

## Step by Step Tasks

1. **`src/popoto/extraction/_anthropic_compat.py`** — new module with
   `MINIMUM_ANTHROPIC_VERSION = "0.77.0"` (spike-1 evidence in the docstring),
   `REQUIRED_CREATE_PARAMS`, `AnthropicVersionError`, and
   `assert_messages_create_supported(client)` including both escape hatches.
2. **`src/popoto/extraction/claude.py`** — call
   `assert_messages_create_supported` in `__init__` after building the client.
   Update the class docstring's failure-mode paragraph to record that a
   too-old SDK now raises at construction while `extract()` still never raises.
3. **`src/popoto/extraction/resolution.py`** and
   **`src/popoto/extraction/verdict.py`** — call the check in each
   `_default_client()`.
4. **`pyproject.toml`** — floor to `anthropic>=0.77.0`.
5. **`uv lock`** — regenerate; inspect the diff and confirm only the specifier
   line moved.
6. **`tests/test_anthropic_floor.py`** — TC1–TC7.
7. **Run the narrow test set**: `tests/test_anthropic_floor.py`,
   `tests/test_extraction.py`, `tests/test_auditable_extraction.py`,
   `tests/test_raw_turn_extraction.py`, under `POPOTO_TEST_DB=6`.
8. **Gates**: `ruff check src/`, `black --check src/ tests/`,
   `scripts/mypy_ratchet.py` (read `scripts/mypy_baseline.json` at measurement
   time; state the redis-py and mypy versions alongside the count).
9. **`CHANGELOG.md`** — Fixed entry naming the floor raise as a
   consumer-visible change, with the measured minimum and the reason.
10. **Docs cascade** via `/do-docs`.

## Documentation

- **`CHANGELOG.md`** — required. The floor raise is consumer-visible.
- **`docs/`** — a grep for `anthropic` outside `docs/plans/` returns nothing
  version-related, so no user-facing doc names the floor. To be re-confirmed
  during the DOCS stage rather than assumed here.
- **This plan** — the durable record of *why* 0.77.0 and not 0.40.0 or 1.2.0.

## Open Questions

None blocking. The one judgment call — floor-raise over feature-detect — is
resolved on evidence in Research and recorded in No-Gos, and the issue framed it
as a decision to be made rather than a decision already made. If the maintainer
prefers to keep the floor low and carry a fallback path, that reverses No-Go #1
and roughly triples the size of this change; flagging it here rather than
blocking on it.
