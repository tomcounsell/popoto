---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-03
tracking: https://github.com/tomcounsell/popoto/issues/596
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-03T09:50:58Z
---

# #596 — DefaultMemory eviction: deploy-level kill switch, loud first eviction, data-loss docs

## Problem

PR #594 gave `DefaultMemory` a per-agent record cap (`Defaults.DEFAULT_MEMORY_MAX_RECORDS_PER_AGENT`
= 1000): after every successful non-pipelined save, records past the cap are deleted — stalest by
relevance-decay timestamp — via full `delete()` (index-clean, no tombstone). For a deployment
already above 1000 records per agent, the first save after upgrading silently deletes the *entire*
excess in one synchronous burst — `default_memory.py:161-165` computes `excess = zcard - cap` and
calls a full `delete()` on every record in `zrange(zset_key, 0, excess - 1)` inside that single
`save()`, not one record per save. An agent 50k over the cap therefore does 50k blocking `hgetall`
plus index deletes in one hook call, so the operator's reaction window is zero rather than gradual.
There is no deploy-level override, no distinguishable first-eviction
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

   *Parse order (integers first — critique C4).* `_TRUTHY = ("1", "true", "yes", "on")` at
   `constants.py:25` is an *on* set and cannot express this switch's disable words; worse, `"1"` is
   in it and is also a valid cap of one record. The reader therefore never consults `_TRUTHY`. It
   strips/lowercases the raw value and then, in order:
   - unset or empty → return `None` ("no opinion"); the class attribute applies;
   - `int(raw)` succeeds and is `>= 0` → return that integer (`0` means disabled, `1`
     unambiguously means a cap of one record);
   - `int(raw)` succeeds and is negative → malformed (see below);
   - otherwise, value in a new sibling `_FALSY = ("off", "false", "no")` → return `0` (disabled);
   - anything else → malformed: `logger.warning` naming the bad value, return `None`.

   Never raises; eviction must never fail a save. Return type is `int | None` where `None` means
   "defer to the class attribute" and `0` means "explicitly disabled" — the two must not collapse.

   *Malformed-value warning is deduped without caching the env read (critique C2).* Because the
   reader is called once per save, a typo'd value (`=1k`) would otherwise emit a WARNING on every
   save — the exact log flood §2 exists to prevent. Guard it with a module-level
   `_WARNED_BAD_ENV: set[str]` in `constants.py`, keyed on the raw stripped string, so each
   distinct bad value warns once per process. Do **not** wrap the reader itself in
   `functools.lru_cache`: that reintroduces the import-time-binding defect recorded for
   `VALIDITY_GATING_ENABLED` in `tests/benchmarks/test_defaults_sync.py:105-118`.

   *Precedence is asymmetric — a falsy class attribute is never re-armed (critique BLOCKER).*
   Three shipped docs (`docs/recipes.md:532`, `docs/features/harness-integration.md:417`,
   `docs/guides/subconscious-memory-recipe.md:74`) tell users that setting
   `_max_records_per_agent` falsy on a subclass is *the* way to turn eviction off. A symmetric
   "env value wins" rule would mean an operator exporting `=5000` merely to raise the cap
   process-globally re-arms hard `delete()` on every subclass that deliberately opted out —
   shipping a data-destroying regression inside the fix for a data-destroying default. So:

   | class attr | env unset / malformed | env `0` / falsy word | env positive int |
   |---|---|---|---|
   | truthy (e.g. 1000) | class attr | **0 — disabled** | **env value** |
   | falsy (0/None — explicit opt-out) | 0 — disabled | 0 — disabled | **0 — stays disabled** |

   In words: the env var may lower, raise, or disable the *default* cap; it may also disable a
   subclass's cap; it may **never** enable eviction on a subclass that turned it off. Resolution at
   `default_memory.py:153`:

   ```python
   attr = self._max_records_per_agent
   env = _read_default_memory_max_records()   # int | None
   if not attr:
       cap = attr                 # explicit library-author opt-out; env cannot re-arm
   elif env is not None:
       cap = env                  # deploy switch lowers, raises, or disables the default
   else:
       cap = attr
   ```

   *Placement:* a module-level `_read_default_memory_max_records() -> int | None` in
   `src/popoto/fields/constants.py`, beside the three existing `_read_*` helpers. It is **not**
   assigned to a `Defaults` class attribute — an import-time binding would defeat a
   runtime-flippable deploy switch. `save()` calls it each time; no memoization. One
   `os.environ.get` is free next to the Redis `ZCARD` round-trip that follows it.

2. **First-eviction notice — logged *before* the deletes, plus a durable marker.**

   *Timing (critique C3).* The notice fires immediately after `excess = zcard(zset_key) - cap` and
   the `if excess <= 0: return result` guard, **before** the `zrange` delete loop — phrased "cap
   exceeded, deleting N". Warning only after a successful loop would (a) announce the loss of
   records that are already unrecoverable (no tombstone) and (b) be swallowed entirely by the
   surrounding `except Exception: logger.warning(...)` if a mid-loop error hit after some deletes —
   the loudest case producing the quietest log. `_EVICTION_WARNED` is marked at the same point, so
   a partial-failure path still leaves the notice behind.

   *In-process dedupe.* A module-level `_EVICTION_WARNED: set[tuple[str, str]]` in
   `default_memory.py`, keyed by `(model class name, agent_id)` so a subclass with a different cap
   warns on its own. First sight of a key → `WARNING` naming agent_id, count about to be deleted,
   the cap in effect, and `POPOTO_DEFAULT_MEMORY_MAX_RECORDS` as the way to change or disable it.
   Subsequent evictions for that key log at `DEBUG`. Private module-level name, cleared by a test
   fixture — no public reset API.

   *Durable marker (critique C3).* A log record alone does not reach the population this is written
   for: nothing in the package configures a handler (`hooks.py:32` only calls `getLogger`), so in a
   Claude Code / Codex hook subprocess the record goes to Python's last-resort stderr, which the
   harness suppresses for a hook exiting 0. "Once per process" is also wrong in both directions —
   it degrades to once-per-save in a per-invocation hook process and goes near-silent in a
   long-lived server. So the eviction also `INCRBY`s a Redis counter:

   ```
   $popoto_memory:counter:{agent_id}:evicted   += excess
   ```

   *The counter counts records **selected** for eviction, not records deleted (round-2 C2).* The
   `INCRBY` is fixed at `excess` and fires before the loop (the timing property of the previous
   paragraph is load-bearing and must not be traded away). The loop can legitimately delete fewer
   than `excess`: it `continue`s when `victim == own_key` (`default_memory.py:164-166`), it routes a
   missing hash to `_purge_orphan_keys` (an index repair, not a memory deletion), and a mid-loop
   exception aborts it after fewer deletes. So the counter's contract — stated in the
   `EVICTION_COUNTER_PREFIX` docstring, the doctor line, and the docs — is "records the cap selected
   for eviction", and the invariant is `counter >= records actually deleted`, with equality on the
   clean path. Do **not** "fix" this by moving the `INCRBY` after the loop; that reintroduces the
   round-1 timing concern.

   This is the exact shape of the existing one-time notice precedent
   (`MemoryService._warn_heuristic_cost`, `service.py:703-716`, which `SETNX`s
   `{COUNTER_KEY_PREFIX}:{agent_id}:heuristic_notice`). Choosing the *counter* prefix means
   `MemoryService._read_counters()` (`service.py:678-688`) already scans `…:counter:{agent_id}:*`
   and int-parses the values, so the number surfaces in `status()`, in the MCP `memory_status`
   tool, and in `popoto-memory doctor` **with no change to `service.py`**. `bind_connection()`
   swaps the pool in place on the shared client (`config.py:376-382`), so the recipe's
   `POPOTO_REDIS_DB` writes and doctor's reads hit the same database.

   *But both renderers would mislabel it as a failure (round-2 C1).* `cli.py:290` builds
   `failures = {k: v for k, v in counters.items() if not k.endswith("_ok")}` and prints them under
   `FAILURES`; `mcp_server.py:281` applies the identical filter and prints `failures: {...}`. Left
   alone, the data-loss report ships as `FAILURES evicted=12` / `failures: {"evicted": 12}` —
   mislabeled as an integration error and duplicated alongside the new doctor line. So this plan
   *does* make one addition to `service.py`: a shared
   `NON_FAILURE_COUNTERS = frozenset({"evicted", "heuristic_notice"})` beside `COUNTER_KEY_PREFIX`
   (`service.py:49`), imported by both renderers and subtracted from both `failures`
   comprehensions. `heuristic_notice` is included because the `SETNX` precedent this design copies
   is already mis-bucketed the same way; fixing only `evicted` would leave the two renderers
   inconsistent. (`_read_counters()` itself still needs no change.)

   Layering: `recipes/` must not import from `integrations/`, so `default_memory.py` defines its
   own `EVICTION_COUNTER_PREFIX = "$popoto_memory:counter"` with a docstring cross-referencing
   `service.COUNTER_KEY_PREFIX`, and a test asserts the two strings are equal so the coupling
   cannot drift silently. The `INCRBY` goes inside the existing `try/except` — a counter failure
   must never fail a save. `cli.py::_cmd_doctor` gains one line: when `counters["evicted"]` is
   non-zero, print a data-loss line naming the count ("N records selected for eviction") and
   `POPOTO_DEFAULT_MEMORY_MAX_RECORDS`.

3. **Docs prominence** — the file list is verified, not guessed (see Documentation section):
   the env-var table in `docs/configuration.md`, the existing cap paragraphs in `docs/recipes.md`
   (~line 529), `docs/features/harness-integration.md` (~407–417) and
   `docs/guides/subconscious-memory-recipe.md` (~74), plus a `!!! danger` data-loss admonition on
   the upgrade path; CHANGELOG's existing 1.9.0 bullet (line 16) upgraded to a **BREAKING /
   data-loss** callout naming the env var, matching the treatment the Q-object refusal and the
   plugin opt-in got.

## Step by Step Tasks

Every task carries its own green signal; run it before moving on.

1. Add `_read_default_memory_max_records() -> int | None` and `_FALSY = ("off", "false", "no")` and
   `_WARNED_BAD_ENV: set[str]` to `src/popoto/fields/constants.py`, beside the existing `_read_*`
   switch helpers. Integer parse first, then `_FALSY`, then malformed→dedupe-warn→`None`. Docstring
   cites #596, states the parse order, states why `_TRUTHY` is not used (`"1"` is ambiguous), and
   states why it is not a `Defaults` attribute. The docstring's first line leads with the scope:
   "Cap on records **per `agent_id`** kept by `DefaultMemory`; `0`/`off` disables eviction" — the
   env var name omits `PER_AGENT` for table brevity, so the scope must be stated in prose (round-2
   nit).
   → `REDIS_URL=redis://localhost:6379/15 python -c "import os,popoto.fields.constants as c; [os.environ.__setitem__('POPOTO_DEFAULT_MEMORY_MAX_RECORDS',v) or print(v, c._read_default_memory_max_records()) for v in ('','0','1','5','off','FALSE','no','1k','-3')]"`
   (expect `None,0,1,5,0,0,0,None,None` and exactly two warnings).
2. In `src/popoto/recipes/default_memory.py:153`, replace `cap = self._max_records_per_agent` with
   the asymmetric resolution block from Solution §1 (falsy class attribute short-circuits before
   the env var is consulted). Keep the existing `if not cap: return result` early exit so a
   resolved `0` skips the `ZCARD` too.
   → `ruff check src/ && mypy src/popoto/recipes/default_memory.py`
3. Move the notice ahead of the delete loop: after `excess = ... ; if excess <= 0: return result`,
   emit the once-per-`(class, agent_id)` WARNING (agent_id, `excess`, cap in effect, env-var name),
   mark `_EVICTION_WARNED` at the same point, and demote repeats to `DEBUG`. Add
   `EVICTION_COUNTER_PREFIX` and the `INCRBY …:{agent_id}:evicted` by `excess`, inside the existing
   `try/except`.
   → `pytest tests/test_default_memory_eviction.py -k "notice or counter"` (after task 5)
4. Add the doctor surface, and stop both renderers filing it under failures (round-2 C1):
   - add `NON_FAILURE_COUNTERS = frozenset({"evicted", "heuristic_notice"})` beside
     `COUNTER_KEY_PREFIX` in `src/popoto/integrations/service.py:49`, with a docstring saying these
     are reports, not integration errors;
   - subtract it from the `failures` comprehension at `cli.py:290` **and** the identical one at
     `mcp_server.py:281` (import the set from `service`; do not re-spell the literal in two places);
   - in `cli.py::_cmd_doctor`, when `info["counters"].get("evicted")` is non-zero, print a data-loss
     line with the count ("N records selected for eviction") and
     `POPOTO_DEFAULT_MEMORY_MAX_RECORDS`.
   `_read_counters()` still needs no change — it already scans the prefix.
   → `pytest tests/ -k "doctor or memory_status" -q`, plus an assertion that with `evicted`
   non-zero the doctor output shows the data-loss line and `evicted` does **not** appear in the
   `FAILURES` line (nor in the MCP `failures:` payload).
5. New `tests/test_default_memory_eviction.py` (in-process, `monkeypatch.setenv`/`delenv`, fixture
   clearing `_EVICTION_WARNED` and `_WARNED_BAD_ENV`):
   - env unset → cap 1000 enforced;
   - `=0`, `off`, `false`, `no` → no eviction *and* no `ZCARD` issued;
   - `=5` → cap 5; `=1` → cap 1 (not "enabled");
   - invalid value (`1k`, `-3`) → exactly one warning per distinct value across *several* saves,
     class-attribute cap still applied, save still succeeds;
   - **subclass with falsy `_max_records_per_agent` stays disabled even when
     `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=5000`** (the BLOCKER's regression test) — and also when it
     is `=0`;
   - subclass with a *truthy* non-default cap: honored when env unset, overridden when env set;
   - env var set **after** `import popoto` still takes effect on the next save — the discriminating
     anti-regression for call-time reading (setting it before import passes even under an
     import-time binding);
   - notice fires once per `(class, agent_id)` (`caplog`), a different agent warns again, and it is
     emitted even when the delete loop raises mid-way (monkeypatch `zrange`/`delete` to raise);
   - the `…:{agent_id}:evicted` counter reports records *selected* for eviction (round-2 C2):
     assert `counter == excess` on the clean path, `counter >= records actually deleted` always,
     an explicit case where the saving record's own key falls inside the eviction window (loop
     `continue`s, so deleted `< counter`), and the mid-loop-raise case (deleted `< counter`, counter
     still incremented). Do not assert exact counter/deleted equality in general;
   - `default_memory.EVICTION_COUNTER_PREFIX == service.COUNTER_KEY_PREFIX`.
   → `pytest tests/test_default_memory_eviction.py -q`
6. Subprocess test proving the switch works with no Python seam, modeled on
   `tests/test_pytest_plugin.py::test_isolated_db_subprocess`:
   `subprocess.run([sys.executable, "-c", script], env={**os.environ, "REDIS_URL":
   "redis://localhost:6379/15", "POPOTO_DEFAULT_MEMORY_MAX_RECORDS": "0"})`. `REDIS_URL` **must**
   be in the child env before `import popoto` or the script writes the live DB 0 (CLAUDE.md, #577).
   Assert the child's record count exceeds 1000 with nothing evicted.
   → `pytest tests/test_default_memory_eviction.py -k subprocess -q`
7. Harden `tests/test_production_contracts.py::TestConsistency::test_default_memory_growth_is_bounded`
   (line ~540): it hardcodes `cap = 1000`, so an operator or CI box with the new env var exported
   would silently change what that contract asserts. Add `monkeypatch.delenv("POPOTO_DEFAULT_MEMORY_MAX_RECORDS",
   raising=False)` so the contract keeps testing the default.
   Optionally also assert inside the test that the resolved cap is 1000, so a *smaller* ambient
   value cannot make the contract vacuous.
   → `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=0 pytest tests/test_production_contracts.py::TestConsistency::test_default_memory_growth_is_bounded -q`
   — `=0` is the discriminating value (round-2 C3): with eviction disabled and no `delenv` the store
   holds 1100 > 1000, the assertion fires and the test fails; with the `delenv` it passes. A value
   of `=5` does **not** discriminate: the test only asserts when `count > 1000`, so under a cap of 5
   it passes with or without the fix.
8. Docs + CHANGELOG per the Documentation section, including the asymmetric-precedence rule stated
   explicitly in each place the falsy-subclass escape hatch is currently documented. The `!!! danger`
   admonition must state that the first save on an over-cap deployment deletes the entire excess at
   once — synchronously, inside that one save — rather than trimming gradually, so a reader sizes
   the exposure as "everything above the cap, now" and not "one record per save".
   → `mkdocs build --strict`

## No-Gos

- No change to the eviction policy, ordering (stalest-by-decay), or the hard-`delete()` mechanism
  — #494 owns the tombstone design.
- No acknowledgement-gate (issue item 4) — deferred to the maintainer policy discussion. The
  `evicted` counter added in Solution §2 is a *report*, never a gate: eviction proceeds whether or
  not anyone reads it, and nothing blocks a save waiting for acknowledgement.
- The cap constant stays in `Defaults` (magic-number doctrine); no constructor kwarg.

## Risks / Rabbit Holes

- **Do not cache the env read.** The obvious "optimization" (`functools.lru_cache`) reintroduces
  the import-time-binding defect the repo has already written down twice
  (`VALIDITY_GATING_ENABLED`, `POPOTO_NEVER_RECORD_DISABLE` notes) and makes every test need a
  cache-clear. A dict lookup per save is noise beside the `ZCARD`.
- **Contract-test coupling**: `test_default_memory_growth_is_bounded` hardcodes 1000. Task 7 exists
  because leaving it alone makes the new switch able to silently disarm a production contract test.
  The task's green signal must use `=0` (not `=5`), or the check passes with and without the fix.
- **The `evicted` counter is a report, not a failure.** Both `cli.py:290` and `mcp_server.py:281`
  bucket every non-`_ok` counter under failures; without `NON_FAILURE_COUNTERS` the data-loss
  signal ships mislabeled as an integration error in two surfaces at once.
- **`counter == deleted` is false by construction.** Own-key `continue`, orphan purges, and
  mid-loop aborts all make deletions fewer than `excess`. The counter means "selected for
  eviction"; asserting exact equality produces a flaky test.
- **`test_defaults_sync.py` allowlist**: only needs an entry if a *new* `Defaults` constant is
  added. This plan adds none — the reader is a module-level function — so the file should not need
  touching. If the build ends up adding a constant anyway, the allowlist entry is mandatory or that
  test fails.
- **The first eviction is an unbounded synchronous burst, not a trickle.** `excess = zcard - cap`
  is deleted in full inside one `save()`, so an agent far above the cap blocks that save for
  roughly 0.7 ms per record (measured) — ~35 s at 50k over — inside a hook the same release caps
  at 10 s (`CHANGELOG.md:16`). The docs warning must describe this burst, not a gradual drain;
  understating it is the difference between an operator who can react and one who cannot. Fixing
  the burst itself (batching, throttling) is **out of scope** — it is an eviction-policy change
  owned by the maintainer discussion and #494.
- **Warning set growth**: bounded by distinct `(class, agent_id)` pairs per process — fine for a
  hook process; a long-lived multi-tenant server with unbounded agent ids would grow it slowly.
  Acceptable at this appetite; note it in the code comment rather than adding an LRU.
- **Don't touch pipeline-mode saves**: eviction already only runs on non-pipelined saves; keep it
  that way.
- **Precedence is asymmetric on purpose, and the asymmetry is the load-bearing safety property.**
  A positive env value overrides the *default* cap but must never re-arm eviction on a subclass
  that set `_max_records_per_agent` falsy — that opt-out is the escape hatch three shipped docs
  currently advertise, and a symmetric rule would let `=5000` (exported to *raise* a cap)
  process-globally re-enable hard `delete()` on a model that had handed forgetting to
  `MemoryLifecycle`. Task 5 carries the explicit regression test. State the rule in the reader's
  docstring, the configuration table, and every recipes/harness doc that mentions the falsy
  attribute.
- **`0` vs `None` must not collapse** in the reader's return type: `0` is "explicitly disabled",
  `None` is "no opinion, defer to the class attribute". A truthiness test on the return value
  conflates them and breaks both the disable path and the falsy-subclass guard.
- **Malformed-value log flood**: the reader runs per save, so a bad value warns per save unless
  deduped by `_WARNED_BAD_ENV`. The tempting fix (`lru_cache` on the reader) is forbidden — it is
  the import-time binding this whole design exists to avoid.
- **Counter-prefix coupling**: `default_memory.EVICTION_COUNTER_PREFIX` duplicates
  `service.COUNTER_KEY_PREFIX` to respect the `recipes/` → `integrations/` layering direction. The
  equality assertion in Task 5 is what keeps the duplication honest; without it a rename in
  `service.py` silently removes the eviction count from `doctor`.

## Success Criteria

- All new tests green; full suite green on an isolated DB (`POPOTO_TEST_DB=<n>`; the six
  DB-15-hardcoding tests listed in `docs/sdlc/do-sdlc.md` are expected noise on a non-15 DB).
- `ruff check src/` exits 0; `black src/ tests/` clean; `mypy src/` delta 0 vs main, measured in
  the same environment (redis-py version stated alongside the number).
- `mkdocs build --strict` passes.
- `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=0` demonstrably disables eviction in the Task 6 subprocess
  test shaped like a hook invocation, proving the switch works without any Python seam — and the
  Task 5 after-import case proves the value is read at call time, not bound at import.
- A subclass with a falsy `_max_records_per_agent` evicts nothing under
  `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=5000` (Task 5).
- The first-eviction WARNING is present in `caplog` even when the delete loop raises after the
  first victim (Task 5).
- `popoto-memory doctor` reports a non-zero `evicted` count ("selected for eviction") and names the
  env var after an over-cap save, and `evicted` appears in **neither** doctor's `FAILURES` line nor
  the MCP `memory_status` `failures:` payload (Task 4).
- The `evicted` counter equals `excess` on the clean path and is `>=` the number of records actually
  deleted in the own-key and mid-loop-raise cases (Task 5).
- `test_default_memory_growth_is_bounded` still asserts the 1000 default regardless of ambient env,
  verified with the discriminating `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=0` exported (Task 7).

## Data Flow

`DefaultMemory.save()` → `super().save()` (writes hash + `relevance` partition ZADD) → early exit
if pipelined or write-filtered → **[new] cap resolution: falsy class attribute short-circuits;
else env reader (`int | None`) else class attribute** → `ZCARD` on the agent's `relevance`
partition → `excess = zcard - cap`; return if `<= 0` → **[new] first-eviction WARNING for this
`(class, agent_id)` (DEBUG thereafter), `_EVICTION_WARNED` marked, and `INCRBY
$popoto_memory:counter:{agent_id}:evicted excess` — all BEFORE any delete** → `zrange(0, excess-1)`
(stalest by decay timestamp) → per victim `hgetall` +
`decode_popoto_model_hashmap(...).delete()` (full index cleanup, no tombstone), or
`_purge_orphan_keys` when the hash is already gone → return the original save result. The whole
eviction block stays inside `except Exception: logger.warning(...)` so no new failure mode reaches
a save — which is precisely why the notice is emitted before the loop rather than after it.

Read-back path: `MemoryService._read_counters()` scans `$popoto_memory:counter:{agent_id}:*` →
`status()["counters"]["evicted"]` → `popoto-memory doctor` data-loss line **[new]** and the MCP
`memory_status` tool. Both renderers first subtract the **[new]** `service.NON_FAILURE_COUNTERS`
from their `failures` comprehension (`cli.py:290`, `mcp_server.py:281`) so the count is reported as
data loss, not as an integration failure.

## Documentation

Verified targets (each already contains cap text that will otherwise contradict the new switch):

- `docs/configuration.md` — new row in the environment-variable table (~line 402), alongside
  `POPOTO_NEVER_RECORD_DISABLE` and `POPOTO_JOURNAL_COUPLING_DISABLE`. The row's description leads
  with the scope, since the name omits it: "Cap on records **per `agent_id`** kept by
  `DefaultMemory`; `0`/`off` disables eviction."
- `docs/recipes.md` (~529–532) — currently says "set `_max_records_per_agent` falsy on your
  subclass"; add the env var and a data-loss admonition about the first save after upgrading.
- `docs/features/harness-integration.md` (~407–417) — same correction, this is the doc hook users
  read.
- `docs/guides/subconscious-memory-recipe.md` (~74) — already warns that an over-cap corpus starts
  deleting on upgrade; add the override.
- `docs/guides/tuning-magic-numbers.md` — the kill-switch listing (see the
  `POPOTO_NEVER_RECORD_DISABLE` entry ~line 87) gains the new switch.
- `CHANGELOG.md` (line 16) — upgrade the existing 1.9.0 bullet to a BREAKING / data-loss callout.

Each of `recipes.md`, `harness-integration.md` and `subconscious-memory-recipe.md` currently
documents the falsy-`_max_records_per_agent` opt-out; each must gain the explicit statement that
the env var never re-arms that opt-out (only lowers/raises/disables the default), so the shipped
guarantee and the new switch agree.

Also document the `evicted` counter where `doctor`'s output is described (`docs/features/harness-integration.md`
doctor section and any `memory_status` field list), so an operator can find the data-loss signal
without reading logs — worded as "records **selected for eviction**", and noted as a report rather
than a failure counter.

`docs/guides/harness-claude-code.md` / `harness-codex.md` mention `DefaultMemory` but not the cap;
touch them only if the build finds cap-relevant text there.

## Critique Results

<!-- Populated by /do-plan-critique (war room), 2026-09-03. Verdict: NEEDS REVISION (1 blocker, 4 concerns, 1 nit). -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | history-consistency + risk-robustness | Env value override silently re-arms hard deletion on subclasses that deliberately disabled it. Three shipped docs (`docs/recipes.md:532`, `docs/features/harness-integration.md:417`, `docs/guides/subconscious-memory-recipe.md:74`) tell users that a falsy `_max_records_per_agent` on a subclass is *the* way to turn eviction off. Under Solution §1's precedence rule ("positive integer → overriding both the constant and any subclass attribute"), an operator exporting `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=5000` merely to *raise* the cap also process-globally re-arms hard `delete()` on every subclass that opted out — including one that handed forgetting to `MemoryLifecycle`. Task 4 encodes this as a tested requirement, so a builder following the plan ships a data-destroying regression inside the fix for a data-destroying default. Risks treats the precedence as documentation-only. | **Addressed** — Solution §1 *Precedence is asymmetric* (truth table + resolution snippet, falsy `attr` short-circuits before the env var); Task 5 bullet "subclass falsy cap stays disabled even when `=5000`"; Risks first bullet rewritten. | Make the disable direction asymmetric: the env var may lower, raise, or disable the *default* cap, but a falsy class attribute is an explicit library-author opt-out a positive env value never overrides. At `default_memory.py:153`: `attr = self._max_records_per_agent; env = _read_default_memory_max_records()`; if env disables → `cap = 0`; elif env is a positive int **and** `attr` is truthy → `cap = env`; else → `cap = attr`. Guard is `if not attr: cap = attr` before consulting a positive env. Rewrite Task 4's assertion to "subclass falsy cap stays disabled even when `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=5000`". |
| CONCERN | risk-robustness + history-consistency | Invalid-value warning fires once per save, contradicting the plan's own no-memoization rule. Solution §1 promises "one `logger.warning` naming the bad value", but *Placement* mandates the reader is called each save with no `lru_cache`. A typo'd value (`=1k`) then emits a WARNING on every save — precisely the per-save log flood Solution §2 exists to prevent. | **Addressed** — Solution §1 *Malformed-value warning is deduped without caching*: `_WARNED_BAD_ENV: set[str]` in `constants.py`, no `lru_cache`; Task 1 + Task 5 assert one warning per distinct bad value across several saves. | Dedupe the malformed-value warning without caching the env read: module-level `_WARNED_BAD_ENV: set[str]` in `constants.py` keyed on the raw stripped string, warn only on first sight of each distinct bad value. Do NOT wrap the reader in `lru_cache` — that reintroduces the import-time-binding defect recorded for `VALIDITY_GATING_ENABLED` in `tests/benchmarks/test_defaults_sync.py:105-118`. |
| CONCERN | risk-robustness | The "loud" first-eviction WARNING is invisible to the hook population it is written for. Nothing in the package configures a logging handler (no `basicConfig`/`StreamHandler`/`addHandler` anywhere in `src/popoto/integrations/` or `default_memory.py`; `hooks.py:32` only does `getLogger`). In a Claude Code / Codex hook subprocess the record reaches Python's last-resort stderr handler, and a hook exiting 0 has its stderr suppressed by the harness. "Once per process" also degrades to "once per save" in a per-invocation hook process while going near-silent in a long-lived server — wrong in both directions. | **Addressed** — Solution §2 *Durable marker*: `INCRBY $popoto_memory:counter:{agent_id}:evicted`, which `_read_counters()` already surfaces in `status()`/`memory_status`/`doctor`; Task 4 adds the doctor line; layering handled by `EVICTION_COUNTER_PREFIX` + equality test. | Add a second, durable surface: on first eviction for a `(class, agent_id)`, `SETNX` a one-time marker key (e.g. `popoto:eviction_notice:{class}:{agent_id}` carrying count, cap, env-var name) and have `MemoryService.status()` / `popoto-memory doctor` report its presence. Reuses the issue's item-4 marker idea without the acknowledgement gate the No-Gos exclude. |
| CONCERN | scope-value | Plan warns strictly *after* deletion, but the issue requires discoverability *before* it. Task 3 says "warn only when at least one record was actually deleted"; records are unrecoverable (no tombstone) by the time anything is logged. Interaction: because the eviction block sits inside `except Exception: logger.warning(...)`, a mid-loop exception after some deletes suppresses the first-eviction notice entirely — the loudest case produces the quietest log. | **Addressed** — Solution §2 *Timing* and Task 3: notice + `_EVICTION_WARNED` marking + counter all fire after the `excess <= 0` guard and before the `zrange` loop; Task 5 tests the mid-loop-raise case; Data Flow updated. | In `default_memory.py`, emit the WARNING immediately after `excess = zcard(zset_key) - cap` / `if excess <= 0: return result`, BEFORE the `zrange` delete loop, phrased "cap exceeded, deleting N". Mark `_EVICTION_WARNED` at the same point so a partial-failure path still leaves the notice behind. |
| CONCERN | history-consistency | `_TRUTHY` reuse cannot express the plan's disable tokens, and `1` is ambiguous. `constants.py:25` defines `_TRUTHY = ("1", "true", "yes", "on")` — an *on* set; the reader needs a *falsy* set (`0`/`off`/`false`/`no`), so "reusing `_TRUTHY`" is not implementable as written. Worse, `"1"` is in `_TRUTHY` and is also a valid positive integer, making `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=1` ambiguous between "enabled" and "cap of one record". | **Addressed** — Solution §1 *Parse order*: `int(raw)` first (so `1` = cap of one, `0` = disabled), then a new `_FALSY = ("off", "false", "no")`; `_TRUTHY` is never consulted. Task 1's validation command exercises `1` and `0` explicitly. | Parse integers FIRST (`int(raw)` in a `try`) so `1` unambiguously means a cap of 1 and `0` means disabled; only fall through to a new sibling `_FALSY = ("off", "false", "no")` for the non-numeric disable words. Never consult `_TRUTHY` on this path. Document the order in the docstring. |
| CONCERN | structural (check 2e) + history-consistency | Orphaned success criterion: bullet 4 requires "`POPOTO_DEFAULT_MEMORY_MAX_RECORDS=0` demonstrably disables eviction in a subprocess test shaped like a hook invocation", but Task 4 specifies only in-process `monkeypatch.setenv`/`delenv` tests — no task creates that subprocess test, so the plan cannot be verified against its own criteria. Separately, setting the env *before* `import popoto` is the weaker test: it passes even under the import-time binding the plan forbids; the discriminating case is setting it *after* import. | **Addressed** — Task 6 is the subprocess test (modeled on `test_isolated_db_subprocess`, `REDIS_URL` in the child env per CLAUDE.md/#577); Task 5 adds the set-**after**-import case as the real call-time-read anti-regression. Success Criteria bullets now map 1:1 onto Tasks 4–7. | Model the subprocess test on `tests/test_pytest_plugin.py::test_isolated_db_subprocess` (`subprocess.run([sys.executable, "-c", ...], env={**os.environ, "REDIS_URL": "redis://localhost:6379/15", "POPOTO_DEFAULT_MEMORY_MAX_RECORDS": "0"})`) — CLAUDE.md requires `REDIS_URL` be set before `import popoto` or the script writes live DB 0. Add the after-import in-process case to Task 4 as the real anti-regression for call-time reading. |
| NIT | structural (check 2b) | No task carries its own validation command. Tasks 1–6 have verification only in the global Success Criteria, so there is no per-task green signal during the build. | **Addressed** — all 8 tasks now carry a `→` validation command. | Append a one-line check to each task, e.g. Task 5 → `pytest tests/test_production_contracts.py::TestGrowth::test_default_memory_growth_is_bounded`. |

**Structural checks**: required sections PASS (all 14 present and non-empty); task numbering PASS (1–6, no gaps); dependencies PASS (none declared); task validation commands FAIL (0 of 6 — see NIT); file paths PASS (13 of 14 exist; `tests/test_default_memory_eviction.py` intentionally new; every line anchor verified accurate — `default_memory.py:137,153`, `constants.py:286-293`, `recipes.md:529-532`, `harness-integration.md:417`, `subconscious-memory-recipe.md:74`, `configuration.md:402-403`, `tuning-magic-numbers.md:87`, `CHANGELOG.md:16`, `test_defaults_sync.py:105-108`, `test_production_contracts.py:540`); prerequisites PASS (#594 merged as `16aa702`, 2026-09-03T09:17Z; freshness commit `b9cd9b2` is a real main commit — the #595 plan commit rather than the #594 merge, harmless, code claims re-verified against current main); cross-references FAIL (1 orphaned success criterion; No-Gos/Rabbit Holes correctly absent from planned work).

**Revision pass (2026-09-03)**: all 7 findings addressed above; the two structural FAILs are
cleared — task validation commands now 8 of 8 (each task carries a `→` line), and the orphaned
success criterion is resolved by Task 6 (subprocess test) plus the Success Criteria rewrite, which
now maps every bullet onto a numbered task. Task numbering is 1–8, no gaps. New source anchors
introduced by this revision were verified against main: `service.py:678-688` (`_read_counters`),
`service.py:703-716` (`_warn_heuristic_cost` SETNX precedent), `service.py:49`
(`COUNTER_KEY_PREFIX = "$popoto_memory:counter"`), `config.py:376-382` (in-place pool swap),
`cli.py:206` (`_cmd_doctor`), `constants.py:25` (`_TRUTHY`).

**Method note**: no Agent/Task dispatch was available in the critique context, so the three FULL-depth lenses (Risk & Robustness, Scope & Value, History & Consistency) were executed in-driver as distinct passes against the plan and the verified source files rather than as forked subagents.

### Round 2 (2026-09-03) — verdict: READY TO BUILD (with concerns), 0 blockers, 3 concerns, 3 nits

The round-1 BLOCKER is verified addressed (asymmetric precedence: truth table + `if not attr` short-circuit in Solution §1, Task 5 regression bullet, Risks bullet). All round-1 concerns are addressed. New findings, all introduced by the round-1 remediation (the `evicted` counter and the two new tasks):

| Severity | Critic | Finding | Implementation Note |
|----------|--------|---------|---------------------|
| CONCERN | risk-robustness + history-consistency | **The `evicted` counter surfaces as a FAILURE, in both readers.** Solution §2 claims the counter lands in `status()`, `memory_status` and `doctor` "with no change to `service.py`". True for `status()`, but both renderers bucket counters by suffix: `cli.py:290` builds `failures = {k: v for k, v in counters.items() if not k.endswith("_ok")}` and prints them under `FAILURES`, and `mcp_server.py:281` does the identical filter and prints `failures: {...}`. So a data-loss report ships as `FAILURES evicted=12` in doctor and `failures: {"evicted": 12}` over MCP — mislabeled as an integration error, and duplicated with Task 4's new line. Task 4 explicitly scopes the change to one `cli.py` line and no `service.py` change, so nothing in the plan corrects it. | Add a shared non-failure counter set (e.g. `NON_FAILURE_COUNTERS = frozenset({"evicted", "heuristic_notice"})` in `service.py`, imported by both renderers) and subtract it from the `failures` comprehension at `cli.py:290` **and** `mcp_server.py:281` before adding the Task 4 data-loss line. `heuristic_notice` is already mis-bucketed the same way by the `SETNX` precedent the plan is copying — fixing only `evicted` leaves the renderer inconsistent. Extend Task 4's `pytest -k doctor` check to assert `evicted` does *not* appear in the FAILURES line. |
| CONCERN | risk-robustness | **`INCRBY excess` before the loop does not equal records deleted, but Task 5 asserts equality.** Solution §2 fixes the increment at `excess` and emits it before the `zrange` loop (correct for the timing concern), yet the loop can delete fewer than `excess`: it `continue`s when `victim == own_key` (`default_memory.py:164-166`), it routes a missing hash to `_purge_orphan_keys` (an index repair, not a memory deletion), and a mid-loop exception — the case Task 5 deliberately simulates — aborts after fewer deletes. Task 5's bullet "the `…:{agent_id}:evicted` counter equals the number of records evicted" is therefore false as specified; the builder will either ship a flaky test or quietly weaken the assertion. | Keep the pre-loop `INCRBY excess` (the timing property is load-bearing) and restate the counter's contract as *records the cap selected for eviction*, not records deleted — say so in the `EVICTION_COUNTER_PREFIX` docstring and the doctor/docs wording ("selected for eviction"). Rewrite the Task 5 bullet to assert `counter >= deleted` and `counter == excess` on the clean path, and add an explicit own-key-in-window case rather than asserting exact equality. Do not "fix" this by moving the `INCRBY` after the loop — that reintroduces the round-1 C3 concern. |
| CONCERN | scope-value | **Task 7's validation command cannot fail, so it does not verify Task 7.** `test_default_memory_growth_is_bounded` (`tests/test_production_contracts.py:540-558`) seeds 1100 and only asserts anything when `count > cap` (1000). Under `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=5` the store holds ~5 records, the `if` never fires, and the test passes **identically with and without** the `monkeypatch.delenv` the task adds — the plan's own "must still pass with the env var exported — that is the point of the task" is satisfied by the unfixed code. The vacuous-pass hazard the task exists to close is invisible to its green signal. | Change Task 7's validation command to `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=0 pytest tests/test_production_contracts.py::TestGrowth::test_default_memory_growth_is_bounded -q`. With `=0` and no `delenv`, eviction is disabled, `count` is 1100 > 1000, the TTL assertion fires and fails (DefaultMemory sets no TTL); with the `delenv` it passes. That is the discriminating check. Optionally also assert inside the test that the resolved cap is 1000, so a *smaller* ambient value cannot make the contract vacuous. |
| NIT | history-consistency | Stale task number in Risks: the "Contract-test coupling" bullet says "Task 5 exists because leaving it alone…", but the contract-test hardening is Task 7 (Task 5 is the new test module). Left over from the round-1 renumbering. | Change "Task 5 exists" to "Task 7 exists" in the second Risks bullet. |
| NIT | risk-robustness | Task 1's validation one-liner imports popoto with no `REDIS_URL` pinned, so it binds the live DB-0 store (CLAUDE.md / #577). It only reads env and prints — no writes — but the repo's own rule is that ad-hoc scripts pin the DB before `import popoto`. | Prefix the command with `REDIS_URL=redis://localhost:6379/15`. |
| NIT | scope-value | The env var is `POPOTO_DEFAULT_MEMORY_MAX_RECORDS` while the cap it overrides is *per `agent_id`* (`DEFAULT_MEMORY_MAX_RECORDS_PER_AGENT`). An operator reading `=5000` can reasonably take it for a whole-store cap. | Keep the name (shorter is better in a table) but make the `docs/configuration.md` row and the reader docstring lead with "per `agent_id`", e.g. "Cap on records **per agent_id** kept by `DefaultMemory`; `0`/`off` disables eviction." |

**Round-2 revision pass (2026-09-03)**: all 3 concerns and all 3 nits above are folded in; no
round-2 blockers existed and no settled round-1 design was reopened.

| Round-2 finding | Addressed by |
|---|---|
| C1 — `evicted` renders under FAILURES in both `cli.py:290` and `mcp_server.py:281` | Solution §2 *But both renderers would mislabel it as a failure*; Task 4 rewritten (shared `service.NON_FAILURE_COUNTERS`, subtracted in both comprehensions, doctor line, assertion that `evicted` is absent from FAILURES / MCP `failures:`); Data Flow read-back path; new Risks bullet; new Success Criteria bullet. Task 4's former "no change to `service.py`" claim is withdrawn — one constant is added there. |
| C2 — pre-loop `INCRBY excess` ≠ records deleted, so Task 5's equality assertion is wrong | Solution §2 *The counter counts records selected for eviction*; Task 5 counter bullet restated (`== excess` on the clean path, `>= deleted` always, explicit own-key and mid-loop-raise cases); Risks bullet; Success Criteria bullet; doctor/docs wording changed to "selected for eviction". Increment stays pre-loop. |
| C3 — Task 7's `=5` validation command cannot fail | Task 7's command changed to `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=0 …`, with the reason recorded inline; optional in-test assertion that the resolved cap is 1000; Risks and Success Criteria updated. |
| NIT — stale "Task 5 exists" in Risks | Corrected to "Task 7 exists". |
| NIT — Task 1's one-liner imports popoto against live DB 0 | Command prefixed with `REDIS_URL=redis://localhost:6379/15`. |
| NIT — env-var name omits the per-`agent_id` scope | Task 1 docstring requirement and the `docs/configuration.md` row now lead with "per `agent_id`"; name unchanged. |

Source anchors re-verified against main for this pass: `cli.py:290-291` (failure/success
comprehensions), `mcp_server.py:279-282` (identical filter), `service.py:49` (`COUNTER_KEY_PREFIX`,
where `NON_FAILURE_COUNTERS` will sit), `service.py:705` (`heuristic_notice` marker),
`default_memory.py:153-180` (cap read, `excess`, own-key `continue`, `_purge_orphan_keys`,
enclosing `except`).

**Structural checks (round 2)**: required sections PASS (15 present, non-empty); task numbering PASS (1–8, no gaps); dependencies PASS (none declared); per-task validation commands PASS (8 of 8 — see the Task 7 concern for one that does not discriminate); file paths PASS (all exist except `tests/test_default_memory_eviction.py`, intentionally new); line anchors PASS — re-verified `default_memory.py:137,153`, `constants.py:25,292`, `service.py:49,678,703`, `config.py:364-386`, `cli.py:206`, `mcp_server.py:281`, `test_defaults_sync.py:105-108`, `test_production_contracts.py:540`, `recipes.md:532`, `harness-integration.md:417-419`, `subconscious-memory-recipe.md:74`, `configuration.md:402`, `tuning-magic-numbers.md:85-87`, `CHANGELOG.md:16`; prerequisites PASS (#594 merged `16aa702`, 2026-09-03T09:17Z); cross-references PASS (every Success Criteria bullet maps to a numbered task; No-Gos and Rabbit Holes absent from planned work).

---

## Open Questions

None blocking this scope. Policy questions (silent deletion vs soft-forget, cap value,
acknowledgement gate) deferred to the maintainer per the Scope decision above.
