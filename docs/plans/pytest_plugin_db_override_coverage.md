---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/549
last_comment_id: none
---

# #549 — test_pytest_plugin.py: stop hardcoding DB 15; cover env-override and inert states

## Problem

`tests/test_pytest_plugin.py` hardcodes `== 15` in 5 assertions, so any run using the
documented `POPOTO_TEST_DB=<n>` override reports exactly 5 failures that must be manually
classified as benign — defeating the override's purpose (concurrent worktrees avoiding DB-15
contention, CLAUDE.md gotcha 4). Post-#594 the plugin has three states (env-override / ini /
inert) and the file covers none by resolution: the ini state passes only by literal.

## Freshness Check

Verified 2026-09-04 against main `ab48b0b`:

- The 5 hardcoded sites still exist (issue cites lines 74, 89, 194, 316, 380 — re-grep
  `== 15` / `_DB_AT_IMPORT_TIME == 15` at build time; #595's merge (PR #597) added ~7 warning
  tests to this file, so lines have drifted).
- Reproduced THIS session: a patch builder running `POPOTO_TEST_DB=14 pytest
  tests/test_pytest_plugin.py` got 5 failures, all `assert ... == 15`; on DB 15 the file is
  fully green (41 passed at the time).
- #595 merged (PR #597): the inert path now has warning-behavior tests (one-shot
  PopotoIsolationWarning, silence when opted in / unused). What #549 still owes on the inert
  state: an assertion that the plugin performs **no swap and no flush** when inert (the
  isolation-plumbing contract, distinct from the warning contract).

**Disposition: Minor drift** (line numbers moved with #597; all claims re-verified).

## Solution

1. Replace the 5 hardcoded assertions with the resolved expectation: read the expected DB via
   `_resolve_test_db(request.config)` (or equivalently `int(os.environ.get("POPOTO_TEST_DB")
   or config.getini("popoto_test_db"))`) in a small helper/fixture `expected_test_db`, so the
   file passes on any documented override.
2. Add an env-override subprocess test: run a minimal pytest session in a subprocess with
   `POPOTO_TEST_DB=7` (and the repo ini present) asserting the session lands on DB 7 — env
   wins over ini. Follow the existing subprocess-test style in this file.
3. Add an inert-state plumbing test (subprocess, neither opt-in set, ini neutralized via a
   temp ini/`-c` config): seed a marker key in the connection's DB before the inner session,
   run a test that touches popoto, and assert afterwards that (a) the connection's DB never
   changed and (b) the marker key survived (no flush). Complements #595's warning tests.
4. Keep DB numbers non-zero everywhere; subprocess targets must set `REDIS_URL` with an
   explicit non-zero db before importing popoto (CI exports a db-less REDIS_URL — see #603:
   never let a subprocess inherit it unpinned).

## No-Gos

- No plugin behavior changes — test-only (if a test exposes a real defect, file an issue,
  don't fix inline).
- Don't touch #595's warning tests except where a hardcoded 15 sits inside one.

## Success Criteria

- `POPOTO_TEST_DB=7 pytest tests/test_pytest_plugin.py` and `POPOTO_TEST_DB=14 ...` both
  fully green (the exact repro that fails today).
- Default run (`pytest tests/test_pytest_plugin.py`) fully green.
- Full non-slow suite green on the default DB; ruff/black clean; mypy delta 0 (same env).

## Documentation

- CHANGELOG entry (test-infrastructure note). CLAUDE.md gotcha 4 already documents the
  override; no change needed unless wording references the 5-failure symptom.
