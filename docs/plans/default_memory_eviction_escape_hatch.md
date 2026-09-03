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

1. **Env-var kill switch** `POPOTO_MEMORY_MAX_RECORDS` (name mirrors `POPOTO_MEMORY_ALLOW_DB0`):
   - unset → class attribute (default 1000) applies;
   - `0` or empty-after-set or `off` → eviction disabled entirely;
   - positive integer → overrides the cap at that value;
   - invalid → one `logger.warning`, fall back to the class attribute (never fail a save).
   Read at save time via a small cached resolver (module-level, `functools.lru_cache` cleared in
   tests), not at import time, so hook processes that set the var in config pick it up.
2. **First-eviction warning, once per agent per process**: a module-level `set` of agent_ids that
   have already warned. On the first eviction for an agent, log at `WARNING` naming: agent_id,
   count deleted this call, current cap, and the override env var. Subsequent evictions for that
   agent log at `DEBUG`.
3. **Docs prominence**: a data-loss callout (admonition box) in `docs/recipes.md` (DefaultMemory
   section) and both harness guides (`docs/guides/harness-claude-code.md`, `harness-codex.md`);
   CHANGELOG entry marked **BREAKING/data-loss** under the next release, matching the treatment
   the Q-object refusal and plugin opt-in got.

## Step by Step Tasks

1. Add `_resolve_max_records()` to `src/popoto/recipes/default_memory.py` implementing the
   env-var semantics above; `save()` consults it instead of reading the class attribute directly.
   Subclass override still wins when the env var is unset (resolver takes the instance's class
   attribute as its fallback input).
2. Add the once-per-agent-per-process WARNING with the specified fields; demote repeats to DEBUG.
3. Tests in `tests/` (extend the #594 contract/recipe suites): env unset → cap 1000 enforced;
   `POPOTO_MEMORY_MAX_RECORDS=0` → no eviction ever; `=5` → cap 5; invalid value → warning +
   default cap; first eviction warns once per agent, second eviction does not re-warn; subclass
   attribute override still honored with env unset. Use monkeypatch.setenv + resolver cache clear.
4. Docs: admonitions + CHANGELOG data-loss callout.

## No-Gos

- No change to the eviction policy, ordering (stalest-by-decay), or the hard-`delete()` mechanism
  — #494 owns the tombstone design.
- No acknowledgement-gate (issue item 4) — deferred to the maintainer policy discussion.
- The cap constant stays in `Defaults` (magic-number doctrine); no constructor kwarg.

## Risks / Rabbit Holes

- **Env caching**: reading `os.environ` on every save is cheap but the resolver is still cached
  to keep the hot path clean; tests must clear the cache around monkeypatch. Keep the cache key
  trivial (no per-instance state).
- **Warning set growth**: bounded by distinct agent_ids per process — fine.
- **Don't touch pipeline-mode saves**: eviction already only runs on non-pipelined saves; keep it
  that way.

## Success Criteria

- All new tests green; full non-slow suite green; ruff/black clean; mypy delta 0 vs main
  (measured same-environment).
- `POPOTO_MEMORY_MAX_RECORDS=0` demonstrably disables eviction in a hook-shaped subprocess test.

## Documentation

- `docs/recipes.md`, `docs/guides/harness-claude-code.md`, `docs/guides/harness-codex.md`,
  `CHANGELOG.md` (BREAKING/data-loss callout).

## Open Questions

None blocking this scope. Policy questions (silent deletion vs soft-forget, cap value,
acknowledgement gate) deferred to the maintainer per the Scope decision above.
