---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-03
tracking: https://github.com/tomcounsell/popoto/issues/596
last_comment_id: none
---

# #596 — DefaultMemory eviction: deploy-level kill switch, loud first eviction, data-loss docs

## Problem

PR #594 gave `DefaultMemory` a per-agent record cap (`Defaults.DEFAULT_MEMORY_MAX_RECORDS_PER_AGENT`
= 1000): after every successful non-pipelined save, records past the cap are deleted — stalest by
relevance-decay timestamp — via full `delete()` (index-clean, no tombstone). For a deployment
already above 1000 records per agent, the first save after upgrading silently deletes memories and
keeps deleting one per save. There is no deploy-level override, no distinguishable first-eviction
signal, and no docs callout. The stated escape hatch (subclass and override
`_max_records_per_agent`) does not reach the population most at risk: hook users on the shipped
Claude Code / Codex integrations use `DefaultMemory` directly and cannot edit model code — the
exact reasoning that made `POPOTO_MEMORY_ALLOW_DB0` an environment variable in the same PR.

## Freshness Check

Verified 2026-09-03 against main `b9cd9b2` (re-verified after PR #594 merged at 09:17Z — the
issue was filed at 02:58Z while #594 was still open, so the code it describes is now on main):

- `src/popoto/recipes/default_memory.py:137` — `_max_records_per_agent = Defaults.DEFAULT_MEMORY_MAX_RECORDS_PER_AGENT`;
  eviction inside `save()` (lines 139–182), wrapped `except Exception` → `logger.warning`, exactly
  as the issue describes. No env override, no first-eviction distinction. Confirmed: eviction is
  skipped when `pipeline is not None or result is False`, and skipped entirely when `cap` is falsy.
- `src/popoto/fields/constants.py:286–293` — the constant, value 1000, with the "safety rail, not a
  tuning constant" comment.
- Related: #494 (tombstones as negative prior) OPEN — hard `delete()` vs tombstone interplay is
  its concern; #584 CLOSED via #594.
- No `xfail`/`pytest.xfail()` markers relate to this bug (searched `tests/`).
- No other active plan in `docs/plans/` touches `default_memory.py` eviction.

**Disposition: Unchanged** (the only movement is #594 landing, which is the premise, not a drift).

## Prior Art

- **PR #594** (merged 2026-09-03) introduced the cap and, in the same PR, made
  `POPOTO_MEMORY_ALLOW_DB0` an environment variable precisely because hook adopters have no Python
  seam (`src/popoto/integrations/config.py:46`). That reasoning is the direct precedent here.
- **Three existing deploy-level kill switches already establish the shape**, all in
  `src/popoto/fields/constants.py:23–50`: `_read_legacy_datetime_key_switch`
  (`POPOTO_DATETIME_KEY_LEGACY`, #537/#538, PR #548), `_read_journal_coupling_switch`
  (`POPOTO_JOURNAL_COUPLING_DISABLE`, #560), `_read_never_record_switch`
  (`POPOTO_NEVER_RECORD_DISABLE`, #561). Each is a module-level `_read_*` helper over a shared
  `_TRUTHY` tuple, documented in the `docs/configuration.md` env-var table (lines 390–402). This
  plan follows that convention rather than inventing a new one.
- **`tests/benchmarks/test_defaults_sync.py:105–109`** already carries an explicit allowlist entry
  for `DEFAULT_MEMORY_MAX_RECORDS_PER_AGENT` ("a safety rail read as a class attribute … not a
  swept constant"), and the neighbouring `VALIDITY_GATING_ENABLED` entry records the rule that
  matters most here: *a module-level alias bound at import time defeats a runtime-flippable deploy
  switch.* Hence the call-time read below.
- No prior failed attempt at this fix exists — this is the first follow-up on #594.

## Research

No external research needed: the change is entirely internal (stdlib `os.environ` plus existing
in-repo conventions), so per the skill's skip rule no WebSearch was run.

## Scope decision (recorded, not open)

This plan ships the issue's suggested-fix items 1–3 (deploy-level override, loud first eviction,
docs prominence). It does NOT change the eviction policy itself: item 4 (require explicit
acknowledgement before the first over-cap eviction) and the maintainer question (is silent
deletion right at all vs a soft signal; is 1000 right; #494 tombstone interplay) are policy
decisions that stay with the maintainer — the issue remains open for that discussion if the
maintainer wants it after this ships, or this PR's `Closes #596` stands and #494 carries the
tombstone design. Per repo doctrine (memory: default-ON capabilities need a deploy-level kill
switch; numeric constants are pinned magic numbers, not config), the cap value stays a pinned
constant; only the kill switch is an env var.

## Appetite

Small.

## Solution

1. **Env-var kill switch `POPOTO_DEFAULT_MEMORY_MAX_RECORDS`.**

   *Name decision (recorded, not open):* `POPOTO_MEMORY_*` is the harness-integration config
   namespace, parsed exclusively by `src/popoto/integrations/config.py`; a core-recipe switch must
   not squat there, or `popoto-memory doctor` and the integration config dataclass become liars by
   omission. The core-side switches are named for the thing they gate
   (`POPOTO_NEVER_RECORD_DISABLE`, `POPOTO_JOURNAL_COUPLING_DISABLE`), so this one is named for
   `DefaultMemory`. It is a *value* override rather than a bare disable because the issue asks for
   both "turn it off" and "hook users can't edit model code to raise it".

   Semantics:
   - unset or empty → the class attribute (`_max_records_per_agent`, default 1000) applies, so a
     subclass override still wins when the env var is absent;
   - `0`, `off`, `false`, `no` → eviction disabled entirely (cap resolves falsy, `save()` returns
     before the `ZCARD`);
   - positive integer → that value becomes the cap, overriding both the constant and any subclass
     attribute (a deploy-level switch outranks library code, same as the other three);
   - anything else → one `logger.warning` naming the bad value, then fall back to the class
     attribute. Never raises; eviction must never fail a save.

   *Placement:* a module-level `_read_default_memory_max_records() -> int | None` in
   `src/popoto/fields/constants.py`, beside the three existing `_read_*` helpers and reusing
   `_TRUTHY`. It is **not** assigned to a `Defaults` class attribute — an import-time binding would
   defeat a runtime-flippable deploy switch, exactly the reasoning recorded for
   `VALIDITY_GATING_ENABLED` in `tests/benchmarks/test_defaults_sync.py`. `save()` calls it each
   time; no `functools.lru_cache`, no memoization. One `os.environ.get` is free next to the Redis
   `ZCARD` round-trip that follows it, and caching would reintroduce the import-time problem plus
   test-fixture fragility.

2. **First-eviction warning, once per agent per process**: a module-level
   `_EVICTION_WARNED: set[tuple[str, str]]` in `default_memory.py`, keyed by
   `(model class name, agent_id)` so a subclass with a different cap warns on its own. On the first
   eviction for a key, log at `WARNING` naming: agent_id, count deleted this call, the cap in
   effect, and `POPOTO_DEFAULT_MEMORY_MAX_RECORDS` as the way to change or disable it. Subsequent
   evictions for that key log at `DEBUG`. The set is module-level so tests can clear it; expose it
   as a private name and clear it in a fixture rather than adding a public reset API.

3. **Docs prominence** — the file list is verified, not guessed (see Documentation section):
   the env-var table in `docs/configuration.md`, the existing cap paragraphs in `docs/recipes.md`
   (~line 529), `docs/features/harness-integration.md` (~407–417) and
   `docs/guides/subconscious-memory-recipe.md` (~74), plus a `!!! danger` data-loss admonition on
   the upgrade path; CHANGELOG's existing 1.9.0 bullet (line 16) upgraded to a **BREAKING /
   data-loss** callout naming the env var, matching the treatment the Q-object refusal and the
   plugin opt-in got.

## Step by Step Tasks

1. Add `_read_default_memory_max_records()` to `src/popoto/fields/constants.py` beside the existing
   `_read_*` switch helpers, returning `int | None` per the semantics above. Docstring cites #596
   and states why it is not a `Defaults` attribute.
2. In `src/popoto/recipes/default_memory.py:153`, replace `cap = self._max_records_per_agent` with
   a resolution that consults the env reader first and falls back to the class attribute. Keep the
   existing `if not cap: return result` early exit so `0` disables the `ZCARD` too.
3. Add the once-per-`(class, agent_id)`-per-process WARNING with the specified fields; demote
   repeats to `DEBUG`. Warn only when at least one record was actually deleted, not merely when the
   cap was consulted.
4. Tests (new `tests/test_default_memory_eviction.py`, or extend the #594 recipe suite): env unset
   → cap 1000 enforced; `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=0` → no eviction ever *and* no `ZCARD`;
   `off`/`false` likewise; `=5` → cap 5; invalid value → one warning + class-attribute cap, save
   still succeeds; subclass `_max_records_per_agent` still honored when the env var is unset, and
   overridden when it is set; first eviction warns once, second eviction for the same agent does
   not re-warn (`caplog`), a different agent does warn. Use `monkeypatch.setenv`/`delenv` and clear
   `_EVICTION_WARNED` in a fixture.
5. Harden `tests/test_production_contracts.py::TestGrowth::test_default_memory_growth_is_bounded`
   (line ~540): it hardcodes `cap = 1000`, so an operator or CI box with the new env var exported
   would silently change what that contract asserts. Add `monkeypatch.delenv(...,
   raising=False)` (or resolve the cap through the same reader) so the contract keeps testing the
   default.
6. Docs + CHANGELOG per the Documentation section.

## No-Gos

- No change to the eviction policy, ordering (stalest-by-decay), or the hard-`delete()` mechanism
  — #494 owns the tombstone design.
- No acknowledgement-gate (issue item 4) — deferred to the maintainer policy discussion.
- The cap constant stays in `Defaults` (magic-number doctrine); no constructor kwarg.

## Risks / Rabbit Holes

- **Do not cache the env read.** The obvious "optimization" (`functools.lru_cache`) reintroduces
  the import-time-binding defect the repo has already written down twice
  (`VALIDITY_GATING_ENABLED`, `POPOTO_NEVER_RECORD_DISABLE` notes) and makes every test need a
  cache-clear. A dict lookup per save is noise beside the `ZCARD`.
- **Contract-test coupling**: `test_default_memory_growth_is_bounded` hardcodes 1000. Task 5 exists
  because leaving it alone makes the new switch able to silently disarm a production contract test.
- **`test_defaults_sync.py` allowlist**: only needs an entry if a *new* `Defaults` constant is
  added. This plan adds none — the reader is a module-level function — so the file should not need
  touching. If the build ends up adding a constant anyway, the allowlist entry is mandatory or that
  test fails.
- **Warning set growth**: bounded by distinct `(class, agent_id)` pairs per process — fine for a
  hook process; a long-lived multi-tenant server with unbounded agent ids would grow it slowly.
  Acceptable at this appetite; note it in the code comment rather than adding an LRU.
- **Don't touch pipeline-mode saves**: eviction already only runs on non-pipelined saves; keep it
  that way.
- **Precedence surprise**: an env var that outranks a subclass attribute is deliberate (deploy
  beats library code) but is the one behavior a reader could reasonably expect the other way round.
  It must be stated explicitly in the docstring, the configuration table, and the recipes doc.

## Success Criteria

- All new tests green; full suite green on an isolated DB (`POPOTO_TEST_DB=<n>`; the six
  DB-15-hardcoding tests listed in `docs/sdlc/do-sdlc.md` are expected noise on a non-15 DB).
- `ruff check src/` exits 0; `black src/ tests/` clean; `mypy src/` delta 0 vs main, measured in
  the same environment (redis-py version stated alongside the number).
- `mkdocs build --strict` passes.
- `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=0` demonstrably disables eviction in a subprocess test shaped
  like a hook invocation (env set before `import popoto`), proving the switch works without any
  Python seam.
- `test_default_memory_growth_is_bounded` still asserts the 1000 default regardless of ambient env.

## Data Flow

`DefaultMemory.save()` → `super().save()` (writes hash + `relevance` partition ZADD) → early exit
if pipelined or write-filtered → **[new] cap resolution: env reader → class attribute** → `ZCARD`
on the agent's `relevance` partition → `zrange(0, excess-1)` (stalest by decay timestamp) → per
victim `hgetall` + `decode_popoto_model_hashmap(...).delete()` (full index cleanup, no tombstone),
or `_purge_orphan_keys` when the hash is already gone → **[new] first-eviction WARNING for this
`(class, agent_id)`, DEBUG thereafter** → return the original save result. The whole eviction block
stays inside `except Exception: logger.warning(...)` so no new failure mode reaches a save.

## Documentation

Verified targets (each already contains cap text that will otherwise contradict the new switch):

- `docs/configuration.md` — new row in the environment-variable table (~line 402), alongside
  `POPOTO_NEVER_RECORD_DISABLE` and `POPOTO_JOURNAL_COUPLING_DISABLE`.
- `docs/recipes.md` (~529–532) — currently says "set `_max_records_per_agent` falsy on your
  subclass"; add the env var and a data-loss admonition about the first save after upgrading.
- `docs/features/harness-integration.md` (~407–417) — same correction, this is the doc hook users
  read.
- `docs/guides/subconscious-memory-recipe.md` (~74) — already warns that an over-cap corpus starts
  deleting on upgrade; add the override.
- `docs/guides/tuning-magic-numbers.md` — the kill-switch listing (see the
  `POPOTO_NEVER_RECORD_DISABLE` entry ~line 87) gains the new switch.
- `CHANGELOG.md` (line 16) — upgrade the existing 1.9.0 bullet to a BREAKING / data-loss callout.

`docs/guides/harness-claude-code.md` / `harness-codex.md` mention `DefaultMemory` but not the cap;
touch them only if the build finds cap-relevant text there.

## Critique Results

<!-- Populated by /do-plan-critique (war room), 2026-09-03. Verdict: NEEDS REVISION (1 blocker, 4 concerns, 1 nit). -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | history-consistency + risk-robustness | Env value override silently re-arms hard deletion on subclasses that deliberately disabled it. Three shipped docs (`docs/recipes.md:532`, `docs/features/harness-integration.md:417`, `docs/guides/subconscious-memory-recipe.md:74`) tell users that a falsy `_max_records_per_agent` on a subclass is *the* way to turn eviction off. Under Solution §1's precedence rule ("positive integer → overriding both the constant and any subclass attribute"), an operator exporting `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=5000` merely to *raise* the cap also process-globally re-arms hard `delete()` on every subclass that opted out — including one that handed forgetting to `MemoryLifecycle`. Task 4 encodes this as a tested requirement, so a builder following the plan ships a data-destroying regression inside the fix for a data-destroying default. Risks treats the precedence as documentation-only. | _pending revision_ (Solution §1; Task 4; Risks "Precedence surprise") | Make the disable direction asymmetric: the env var may lower, raise, or disable the *default* cap, but a falsy class attribute is an explicit library-author opt-out a positive env value never overrides. At `default_memory.py:153`: `attr = self._max_records_per_agent; env = _read_default_memory_max_records()`; if env disables → `cap = 0`; elif env is a positive int **and** `attr` is truthy → `cap = env`; else → `cap = attr`. Guard is `if not attr: cap = attr` before consulting a positive env. Rewrite Task 4's assertion to "subclass falsy cap stays disabled even when `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=5000`". |
| CONCERN | risk-robustness + history-consistency | Invalid-value warning fires once per save, contradicting the plan's own no-memoization rule. Solution §1 promises "one `logger.warning` naming the bad value", but *Placement* mandates the reader is called each save with no `lru_cache`. A typo'd value (`=1k`) then emits a WARNING on every save — precisely the per-save log flood Solution §2 exists to prevent. | _pending revision_ (Solution §1) | Dedupe the malformed-value warning without caching the env read: module-level `_WARNED_BAD_ENV: set[str]` in `constants.py` keyed on the raw stripped string, warn only on first sight of each distinct bad value. Do NOT wrap the reader in `lru_cache` — that reintroduces the import-time-binding defect recorded for `VALIDITY_GATING_ENABLED` in `tests/benchmarks/test_defaults_sync.py:105-118`. |
| CONCERN | risk-robustness | The "loud" first-eviction WARNING is invisible to the hook population it is written for. Nothing in the package configures a logging handler (no `basicConfig`/`StreamHandler`/`addHandler` anywhere in `src/popoto/integrations/` or `default_memory.py`; `hooks.py:32` only does `getLogger`). In a Claude Code / Codex hook subprocess the record reaches Python's last-resort stderr handler, and a hook exiting 0 has its stderr suppressed by the harness. "Once per process" also degrades to "once per save" in a per-invocation hook process while going near-silent in a long-lived server — wrong in both directions. | _pending revision_ (Solution §2) | Add a second, durable surface: on first eviction for a `(class, agent_id)`, `SETNX` a one-time marker key (e.g. `popoto:eviction_notice:{class}:{agent_id}` carrying count, cap, env-var name) and have `MemoryService.status()` / `popoto-memory doctor` report its presence. Reuses the issue's item-4 marker idea without the acknowledgement gate the No-Gos exclude. |
| CONCERN | scope-value | Plan warns strictly *after* deletion, but the issue requires discoverability *before* it. Task 3 says "warn only when at least one record was actually deleted"; records are unrecoverable (no tombstone) by the time anything is logged. Interaction: because the eviction block sits inside `except Exception: logger.warning(...)`, a mid-loop exception after some deletes suppresses the first-eviction notice entirely — the loudest case produces the quietest log. | _pending revision_ (Task 3) | In `default_memory.py`, emit the WARNING immediately after `excess = zcard(zset_key) - cap` / `if excess <= 0: return result`, BEFORE the `zrange` delete loop, phrased "cap exceeded, deleting N". Mark `_EVICTION_WARNED` at the same point so a partial-failure path still leaves the notice behind. |
| CONCERN | history-consistency | `_TRUTHY` reuse cannot express the plan's disable tokens, and `1` is ambiguous. `constants.py:25` defines `_TRUTHY = ("1", "true", "yes", "on")` — an *on* set; the reader needs a *falsy* set (`0`/`off`/`false`/`no`), so "reusing `_TRUTHY`" is not implementable as written. Worse, `"1"` is in `_TRUTHY` and is also a valid positive integer, making `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=1` ambiguous between "enabled" and "cap of one record". | _pending revision_ (Solution §1 *Placement*) | Parse integers FIRST (`int(raw)` in a `try`) so `1` unambiguously means a cap of 1 and `0` means disabled; only fall through to a new sibling `_FALSY = ("off", "false", "no")` for the non-numeric disable words. Never consult `_TRUTHY` on this path. Document the order in the docstring. |
| CONCERN | structural (check 2e) + history-consistency | Orphaned success criterion: bullet 4 requires "`POPOTO_DEFAULT_MEMORY_MAX_RECORDS=0` demonstrably disables eviction in a subprocess test shaped like a hook invocation", but Task 4 specifies only in-process `monkeypatch.setenv`/`delenv` tests — no task creates that subprocess test, so the plan cannot be verified against its own criteria. Separately, setting the env *before* `import popoto` is the weaker test: it passes even under the import-time binding the plan forbids; the discriminating case is setting it *after* import. | _pending revision_ (Task 4 + new task) | Model the subprocess test on `tests/test_pytest_plugin.py::test_isolated_db_subprocess` (`subprocess.run([sys.executable, "-c", ...], env={**os.environ, "REDIS_URL": "redis://localhost:6379/15", "POPOTO_DEFAULT_MEMORY_MAX_RECORDS": "0"})`) — CLAUDE.md requires `REDIS_URL` be set before `import popoto` or the script writes live DB 0. Add the after-import in-process case to Task 4 as the real anti-regression for call-time reading. |
| NIT | structural (check 2b) | No task carries its own validation command. Tasks 1–6 have verification only in the global Success Criteria, so there is no per-task green signal during the build. | _pending revision_ (Step by Step Tasks) | Append a one-line check to each task, e.g. Task 5 → `pytest tests/test_production_contracts.py::TestGrowth::test_default_memory_growth_is_bounded`. |

**Structural checks**: required sections PASS (all 14 present and non-empty); task numbering PASS (1–6, no gaps); dependencies PASS (none declared); task validation commands FAIL (0 of 6 — see NIT); file paths PASS (13 of 14 exist; `tests/test_default_memory_eviction.py` intentionally new; every line anchor verified accurate — `default_memory.py:137,153`, `constants.py:286-293`, `recipes.md:529-532`, `harness-integration.md:417`, `subconscious-memory-recipe.md:74`, `configuration.md:402-403`, `tuning-magic-numbers.md:87`, `CHANGELOG.md:16`, `test_defaults_sync.py:105-108`, `test_production_contracts.py:540`); prerequisites PASS (#594 merged as `16aa702`, 2026-09-03T09:17Z; freshness commit `b9cd9b2` is a real main commit — the #595 plan commit rather than the #594 merge, harmless, code claims re-verified against current main); cross-references FAIL (1 orphaned success criterion; No-Gos/Rabbit Holes correctly absent from planned work).

**Method note**: no Agent/Task dispatch was available in the critique context, so the three FULL-depth lenses (Risk & Robustness, Scope & Value, History & Consistency) were executed in-driver as distinct passes against the plan and the verified source files rather than as forked subagents.

---

## Open Questions

None blocking this scope. Policy questions (silent deletion vs soft-forget, cap value,
acknowledgement gate) deferred to the maintainer per the Scope decision above.
