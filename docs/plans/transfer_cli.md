---
status: Ready
type: feature
appetite: Medium
owner: Valor Engels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/555
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-04
---

# `popoto-transfer` — a CLI front-end for `popoto.transfer`

## Problem

Moving one model's records between two Redis instances works today, but only from
inside Python. The operator has to write a throwaway script:

```python
import popoto                      # binds the connection from REDIS_URL at import time
from myapp.models import Memory

with open("memories.jsonl", "w") as fh:
    print(Memory.export_records(project_key="ai", stream=fh).summary())
```

Three things go wrong with that in practice, and all three are already documented
hazards in this repo:

1. **The connection is bound at import time and defaults to database 0.** A scratch
   script that forgets to export `REDIS_URL` before `import popoto` lands on DB 0 —
   the exact mistake that destroyed live agent state twice (see `CLAUDE.md` and
   #577). An export is read-only, but an *import* on that path writes.
2. **The script is written fresh every time**, so the reconciliation report
   (`ImportReport.summary()`) is routinely printed and ignored, or never printed at
   all. `import_records` deliberately reports partial records — saved, but with
   auxiliary state not restored — and a throwaway script has nowhere to surface that.
3. **There is no exit code.** A migration inside a shell script or CI job cannot tell
   "every record landed" from "40 records were rejected by the write gate" without
   parsing prose.

**Current behavior:**

`popoto.transfer` ships as a Python API only (`export_records` / `import_records`,
plus `Model.export_records` / `Model.import_records` delegates at
`src/popoto/models/base.py:2784` and `:2819`). `pyproject.toml` declares one console
script, `popoto-memory` — the agent-memory front-end — and nothing that reaches the
transfer API.

**Desired outcome:**

```console
$ popoto-transfer export --model myapp.models:Memory --filter project_key=ai --out memories.jsonl
ExportResult for Memory
  filter:        Q(project_key='ai')
  matched:       1284
  written:       1284

$ popoto-transfer import --model myapp.models:Memory --in memories.jsonl --on-conflict overwrite
ImportReport for Memory
  records read:  1284
  landed:        1284
  ...
$ echo $?
0
```

A refusal instead of a wipe when the effective database is 0, a machine-readable
`--json` summary, and a non-zero exit when any record fails to land.

## Freshness Check

**Baseline commit:** `0dbce75917ec7d4db79a5de6908d1f980b5ee9eb`
**Issue filed at:** 2026-08-10T07:02:03Z (no comments)
**Disposition:** Minor drift

The issue was filed the day before #554's implementation merged, and one of its three
stated premises has since become false.

**Issue claims re-verified:**

- *"`pyproject.toml` has no `[project.scripts]` today, so adding one is a new
  packaging precedent and a distribution decision"* — **no longer true.**
  `pyproject.toml:110-111` now declares `popoto-memory = "popoto.integrations.cli:main"`,
  added by PR #546 (`e220b2e`, 2026-08-12). Console scripts are established
  precedent; what remains open is only the *name* of the second one. This shrinks the
  scope of #555 and is why the plan can settle the naming question in-plan rather
  than treating it as a distribution decision needing a separate ruling.
- *"The motivating consumer runs a Python script and needs no shell entry point"* —
  still true. This work is not blocking the `tomcounsell/ai` `Memory` migration; it
  is for the next operator.
- *"A CLI needs model discovery (import a module path, find Model subclasses) which
  is a separate problem from the transfer protocol itself"* — still true, and still
  the substantive design work in this plan. Nothing in `src/` resolves a dotted model
  path today (no `importlib` model-resolution helper exists anywhere).
- `popoto.transfer` public API — unchanged since #558 merged. `export_records`
  (`src/popoto/transfer/export.py:214`) and `import_records`
  (`src/popoto/transfer/import_.py:278`) have the signatures the issue assumed.

**Cited sibling issues/PRs re-checked:**

- #554 — closed 2026-08-13, implemented by PR #558 (`31535a3`). The API this CLI
  wraps. Read in full; its five outcome categories and three import policy arguments
  are the CLI's flag surface.
- #556 — open. History-shaped state (EventStream, PredictionLedger, CoOccurrence,
  the AccessTracker log). Interface impact on this plan: none at the flag level. It
  changes what the manifest's fidelity roll-up reports, which the CLI must print
  verbatim rather than summarize, so a future upgrade from `partial` to `carry` shows
  up in CLI output with no CLI change.
- #557 — open. `preserve_keys=False` with reference remapping. Interface impact: it
  will want a flag pair in the `import` subcommand. This plan therefore leaves the
  names `--preserve-keys` and `--regenerate-keys` **unclaimed** (see Technical
  Approach; there is a Verification anti-criterion asserting the flag is absent).
- #572 — open, being built concurrently in the same package. Annotations only. See
  Risks for the coordination rule.

**Commits on main since the issue was filed (touching `src/popoto/transfer/`,
`src/popoto/integrations/cli.py`, or `pyproject.toml`):**

- `31535a3` feat(#554): generic export/import — **created the API this wraps.**
- `e220b2e` feat(#515): subconscious memory — **added `[project.scripts]`**, which
  invalidates the issue's first premise (above).
- `16aa702` Agent memory production audit (#594) — added `Db0RefusedError` and the
  `POPOTO_MEMORY_ALLOW_DB0` opt-in in `src/popoto/integrations/config.py`. This plan
  mirrors the semantics; it does not import or extend that machinery.
- `edf71ad` fix(#596): eviction kill switch — touched `integrations/cli.py` only.
  Relevant as a convention example (`doctor` exit codes, `--json`), not as a conflict.

**Active plans in `docs/plans/` overlapping this area:**

- `transfer_type_cleanliness.md` (#572) — same package, annotations only, no file
  this plan writes. Coordination handled in Risks, not by blocking.
- `adhoc_db0_guard.md` (#577) — adds a core-ORM flush guard with the env var
  `POPOTO_ALLOW_DB0_FLUSH`. Different mechanism (refuses `FLUSHDB`/`FLUSHALL`),
  different variable. This plan deliberately introduces **no new environment
  variable**, so the two cannot collide.

**Notes:** No file:line reference in the issue body drifted, because the issue cites
no file:line references. Every pointer in this plan was read at the baseline commit.

## Prior Art

- **#554 / PR #558** — *Generic export/import with per-field round-trip fidelity*.
  Merged 2026-08-13. Shipped the entire API this plan wraps, and explicitly carved
  the CLI out of scope, filing #555. Succeeded; nothing to redo.
- **#515 / PR #546** — *Subconscious memory for Claude Code, Codex, Hermes,
  OpenClaw*. Merged. Introduced `src/popoto/integrations/cli.py` and the first
  `[project.scripts]` entry. This is the convention template: `argparse` with
  subparsers, lazy imports inside each subcommand, `main(argv=None) -> int`,
  `--json` for machine output, exit 1 on a diagnosed failure.
- **#584 / PR #594** — *Refuse Redis database 0 for agent memory*. Merged.
  `Db0RefusedError` + `POPOTO_MEMORY_ALLOW_DB0`. Source of the refusal semantics this
  CLI mirrors: refuse by default, name the database, name the opt-in, suggest a way
  forward.
- **#596 / PR #598** — *Eviction kill switch*. Merged. Example of `doctor` rendering
  a data-loss fact as its own labeled line rather than burying it in a counter list —
  the pattern the import summary follows for `partial` records.

`gh issue list --state all --search "CLI console script entry point"` returned no
results other than the above, so there is no earlier abandoned attempt at a popoto
CLI to learn from.

## Research

No external research was needed. The work uses only the standard library
(`argparse`, `importlib`, `json`, `sys`), wraps an in-repo API, and follows an
in-repo CLI convention. No new dependency, no external service, no ecosystem pattern
whose current documentation could contradict training data.

Console-script entry points are declared through `[project.scripts]` in
`pyproject.toml`, already exercised in this repo by `popoto-memory`, so the packaging
mechanics are verified locally rather than from documentation.

## Architectural Impact

- **New dependencies:** none. Standard library only.
- **Interface changes:** none to existing code. One new console script name
  (`popoto-transfer`) and one new module (`src/popoto/transfer/cli.py`). No existing
  file changes except `pyproject.toml` (one line), `CHANGELOG.md`, `README.md`, and
  `docs/guides/export-import.md`.
- **Coupling:** the CLI depends on `popoto.transfer` and on `popoto.redis_db`; the
  dependency is one-directional and nothing in the library learns about the CLI.
  Notably it does **not** depend on `popoto.integrations` — the transfer CLI must
  work for an ORM user who has never touched agent memory, so the DB-0 refusal is
  reimplemented in a handful of lines rather than imported from
  `integrations/config.py` (which would drag `MemoryConfig` and its environment
  contract into a generic ORM path).
- **Data ownership:** unchanged. The CLI writes files and Redis records that the
  library already writes.
- **Reversibility:** high for the module, lower for the *name*. A console script
  name is published in package metadata and downstream scripts and CI jobs come to
  depend on it; renaming later is a breaking change for them. This is the one
  decision in the plan worth getting right up front, which is why Technical Approach
  argues it explicitly.

## Appetite

**Size:** Medium

**Team:** Solo dev, PM (naming ruling), code reviewer

**Interactions:**
- PM check-ins: 1 (confirm the entry-point name before it ships; it is hard to
  reverse once published)
- Review rounds: 1

Not Small, for four reasons that each add a round of thinking rather than lines of
code: a published console-script name is effectively permanent; model resolution
imports arbitrary caller code and needs `sys.path` handling that a console script
does not get for free (unlike `python -m`); the DB-0 refusal has to be argued
separately for a read path and a write path; and subprocess tests must reproduce the
pytest plugin's database binding inside a child process, which is a known-sharp edge
in this repo. Not Large: no new dependency, no schema change, no change to any
existing code path, and the wrapped API is stable and already tested.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis or Valkey reachable | `redis-cli -n 15 ping` | The suite and the subprocess tests need a live server |
| Non-zero test database | `python -c "import os,sys; db=os.environ.get('POPOTO_TEST_DB','15'); sys.exit(0 if db not in ('0','') else 1)"` | DB 0 is a live agent store on the maintainer's machine; the CLI's own refusal would also fail the tests |
| Editable install resolves to this checkout | `python -c "import popoto,pathlib,sys; sys.exit(0 if pathlib.Path(popoto.__file__).resolve().is_relative_to(pathlib.Path.cwd()) else 1)"` | A worktree venv pointed at another tree silently tests the wrong package (`CLAUDE.md`) |

**On that third row.** `VIRTUAL_ENV` in this environment points at the primary
checkout's venv, so a bare `uv pip install` or `pip install -e .` run from a worktree
installs into the *primary* venv and repoints its editable `popoto` at the worktree —
it does not populate the worktree's own venv. Always pass the interpreter explicitly:

```console
$ uv pip install --python .venv/bin/python -e '.[dev,embeddings,benchmark,mcp]'
```

Verified for this lane: `.venv/bin/python -c "import popoto; print(popoto.__file__)"`
resolves to `.worktrees/sdlc-555/src/popoto/__init__.py`, and the primary venv still
points at `/Users/valorengels/src/popoto/src`. A critic reading `popoto.__file__` from
the *ambient* interpreter will see the primary checkout and wrongly conclude this
prerequisite fails; the check is only meaningful against the worktree venv's
interpreter.

## Solution

### Key Elements

- **`src/popoto/transfer/cli.py`** — a new module, the only new source file. Holds
  the parser, model resolution, the DB-0 guard, and rendering. It edits none of
  `export.py`, `import_.py`, `format.py`, or `results.py`.
- **Model resolution** — turns `--model myapp.models:Memory` into a class, with
  errors that say what went wrong at each of the three failure points (module not
  importable, attribute missing, attribute is not a Popoto `Model`).
- **DB-0 guard** — reads the database off the live connection and refuses to run
  unless `--allow-db0` is passed. Names the database, the flag, and the alternative,
  in the shape #584 established.
- **`export` subcommand** — wraps `export_records`, with `--filter k=v` (repeatable),
  `--chunk-size`, and `--out PATH|-`.
- **`import` subcommand** — wraps `import_records`, with `--in PATH|-` and one
  passthrough flag per policy argument (`--on-conflict`, `--on-write-gate`,
  `--on-embedding-mismatch`), each restricted to the API's own allowed values.
- **Rendering and exit codes** — the human summary is whatever `result.summary()` /
  `report.summary()` already produces; `--json` emits the dataclass. Exit code
  distinguishes "the run failed" from "the run finished and some records did not
  land".

### Flow

**Shell** → `popoto-transfer export --model pkg.mod:Model --out file.jsonl` →
resolve model → check effective DB → stream JSONL to the file → summary on stderr →
**exit 0**

**Shell** → `popoto-transfer import --model pkg.mod:Model --in file.jsonl
--on-conflict overwrite` → resolve model → check effective DB → read JSONL →
reconciliation report on stderr → **exit 0, or 3 if any record was rejected,
errored, or landed partial**

Refusal path: **Shell** → `popoto-transfer import ...` on database 0 → refusal
message naming `--allow-db0` → **exit 1**, nothing read, nothing written.

### Technical Approach

**Entry-point name: a second console script, `popoto-transfer`.** Three candidates
were weighed.

| Option | Case for | Case against | Verdict |
|---|---|---|---|
| `popoto-transfer` (new script) | Matches the shipped `popoto-<domain>` precedent; claims a name nobody else will want; independently documentable; can later become a thin alias if an umbrella command ever appears | Two scripts in `[project.scripts]` instead of one namespace | **Recommended** |
| `popoto export …` (bare umbrella) | Literally what the issue's example shows; leaves room for future `check-indexes` / `clean-indexes` subcommands, which already have plans in `docs/plans/` | Claiming the bare `popoto` name on PATH for every install is a bigger and less reversible distribution decision than #555 needs to make; and it would immediately raise the question of whether `popoto-memory` should be folded in and deprecated — a much wider change | Rejected for this scope |
| `popoto-memory export …` (subcommand of the existing script) | No new packaging entry at all | Wrong domain. `popoto-memory` is the agent-memory front-end; its module docstring says so, and its subcommands are `hook`/`mcp`/`doctor`/`demo`. `popoto.transfer` is a generic ORM capability with no memory dependency, and burying it under a memory command would make it undiscoverable for the ORM user it is written for | Rejected |

If a bare `popoto` umbrella is ever wanted, it can dispatch to
`popoto.transfer.cli:main` and keep `popoto-transfer` working. Nothing here forecloses
it.

**Model resolution.** Accept `--model dotted.module.path:ClassName` (colon-separated,
one form only — the dotted-only form `a.b.C` is ambiguous between a module and an
attribute, and guessing produces confusing errors). Resolution:

1. Insert the current working directory at the front of `sys.path` if it is not
   already there. A console script does **not** get the CWD on `sys.path` the way
   `python -m` does, so without this, `--model myapp.models:Memory` fails from the
   operator's own project root — the single most likely first invocation.
2. `importlib.import_module(module_path)`, then `getattr` for the class name.
3. Verify the result is a class and a subclass of `popoto.Model`. A non-model
   attribute must be refused by name, not fail later with an `AttributeError` deep
   inside the query layer.

Each failure raises the same CLI-level error type, caught in `main`, printed to
stderr, exit 1. Importing the operator's module executes their code; that is inherent
to model discovery and is stated in the `--help` epilog and the guide, not guarded
against.

**DB-0 refusal.** The database is read off the live client:

```python
db = int(POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db", 0) or 0)
```

This judges the connection that will actually be used, which is the rule PR #594
converged on after the alternative (judging a URL) let a swapped-in connection mask
the real target. `REDIS_URL` is read by `popoto.redis_db` at import time, so by the
time `main` runs, the live client is the truth.

Both subcommands refuse `db == 0` unless `--allow-db0` is passed. **One flag, both
subcommands** — not an export-only opt-in — for two reasons: an export on DB 0 is
read-only but still a disclosure (it writes every record of a production model to a
file the operator names), and a single flag is one thing to document and one thing to
audit in a shell history. The asymmetry is carried in the *message* instead: the
export refusal says the run would read from database 0, the import refusal says it
would write to it. No new environment variable is introduced — a CLI flag is
sufficient at an interactive surface, and it keeps this clear of
`POPOTO_MEMORY_ALLOW_DB0` (#584) and `POPOTO_ALLOW_DB0_FLUSH` (#577).

The refusal message follows #584's shape: what would happen, on which database, the
exact flag to pass, and the alternative (`REDIS_URL=redis://localhost:6379/1`).

**Streams and where the summary goes.** `--out -` writes JSONL to stdout, which means
the summary must not also go to stdout or the file the operator pipes into is
corrupted. Rule:

- The human-readable summary always goes to **stderr**.
- `--json` writes the machine-readable summary to **stdout**, and is refused
  together with `--out -` (exit 1, before anything runs) because both would claim
  stdout.

This keeps `popoto-transfer export --model … --out - | gzip > backup.jsonl.gz`
working while the operator still sees the summary on their terminal.

**A failed export must not destroy the previous one.** Opening `--out PATH` with
`"w"` truncates it immediately, but `export_records` does not resolve the filter
until after the stream is handed to it, so a typo'd `--filter` raises
`QueryException` against an already-zeroed file. That is the same data-loss class the
DB-0 guard exists to prevent, moved to a different surface. The export path therefore
writes to a sibling temporary file and promotes it only on success:

1. Open `PATH + ".part"` for writing (same directory, so the promotion is a rename
   within one filesystem).
2. Run `export_records` into it.
3. On success, `os.replace(PATH + ".part", PATH)` — atomic on POSIX.
4. On **any** exception, close and `os.unlink` the partial file, leave `PATH`
   untouched, and let the error path print and exit 1.

`--out -` needs none of this; stdout has no previous contents to protect. A
Verification row asserts that a failed export leaves a pre-existing destination file
byte-identical and leaves no `.part` file behind.

**JSON output.** `dataclasses.asdict(result)` on `ExportResult` / `ImportReport`,
then `json.dumps(..., indent=2, default=str)` — the same call shape
`popoto-memory doctor --json` uses. `ImportReport.outcomes` is a list of
`RecordOutcome` dataclasses and nests cleanly; `ExportResult.filter_kwargs` values
have already been passed through `to_jsonable` by the library. The computed
properties (`total`, `landed`, …) are not fields, so the JSON payload adds an
explicit `counts` object keyed by the five categories so a consumer does not have to
re-tally `outcomes`.

**Filters.** `--filter key=value`, repeatable, forwarded as plain keyword filters to
`export_records`. Each value is parsed as JSON first (`5`, `0.5`, `true`, `null`,
`"quoted"`), falling back to the raw string when JSON parsing fails, so
`--filter relevance=0.5` filters on a float and `--filter project_key=ai` on a
string. An unknown filter name already raises `QueryException` from the query layer;
the CLI catches it and exits 1 with the message rather than a traceback. `Q` objects
and lookup operators are not expressible on the command line — see Rabbit Holes.

**Exit codes.**

| Code | Meaning |
|---|---|
| 0 | Ran to completion, every record accounted for as landed or skipped, no errors |
| 1 | The run failed: bad `--model`, DB-0 refusal, unreadable file, manifest mismatch, `QueryException`, connection error |
| 2 | `argparse` usage error (argparse's own convention; not chosen here) |
| 3 | The run completed, but records did not land: any `rejected`, `errored`, or `partial` outcome on import, or any `ExportResult.errors` entry on export |

3 is separate from 1 on purpose. A migration script needs to distinguish "the command
did not run" from "the command ran and 40 of 1284 records were refused by the write
gate", and those need different operator responses. `--strict` is not added; 3 is
always returned, and a caller that does not care can ignore it.

**Exit 1 does not promise that nothing was written.** This is the one place where the
plain reading of the table above is wrong, and it must be reflected in the CLI's own
wording. `on_conflict="error"` does not produce an outcome category; it raises
`ModelException` from *inside* the per-record loop
(`src/popoto/transfer/import_.py:190-198`), after every earlier record in the file has
already been saved. The library's own message says so: "records before this one are
already written." So a collision aborts mid-import and lands on exit **1**, not 3.

Two consequences the builder must honor:

- The generic exception handler must not print a message implying the run was a no-op.
  When a `ModelException` escapes `import_records`, the CLI prints the library's
  message verbatim, which already carries the partial-write warning and the
  `on_conflict='overwrite'` recovery path.
- **A conflict collision cannot be used to test exit 3.** Only outcome categories
  produce exit 3, and the reachable ones are: `REJECTED` from a construction failure
  (`import_.py:203-208`) or a write-gate refusal (`import_.py:236-245`), `ERRORED`
  from a save failure (`import_.py:213-217`), and `PARTIAL` from a carried-state
  failure. `SKIPPED` is a clean outcome and must **not** trigger exit 3 — it is what
  `on_conflict="skip"` returns for a record deliberately left alone.

Mapping the two, so the table above is unambiguous:

| Library path | Category or exception | CLI exit |
|---|---|---|
| record saved, state restored | `LANDED` | 0 |
| `on_conflict="skip"`, key exists | `SKIPPED` | 0 |
| `on_conflict="error"`, key exists | `ModelException` mid-loop | 1 (earlier records already written) |
| construction raises | `REJECTED` | 3 |
| `save()` raises | `ERRORED` | 3 |
| `save()` returns `False`/`None` (write gate) | `REJECTED` | 3 |
| saved, carried state failed | `PARTIAL` | 3 |

**#557 forward compatibility.** No `--preserve-keys` flag is added, not even as a
`store_true` that only accepts the current behavior. A flag whose only legal value is
the default is dead surface, and worse, it would fix the *polarity* of the eventual
#557 option before #557 decides whether it wants `--preserve-keys/--no-preserve-keys`
or `--regenerate-keys`. Instead the `import --help` text states that keys are always
preserved and re-running converges. A Verification anti-criterion asserts the flag
name stays unused.

**#556 forward compatibility.** The import summary prints the manifest's fidelity
roll-up verbatim (it already does — `ImportReport.summary()` renders it). When #556
upgrades a subsystem from `partial` to `carry`, CLI output changes with no CLI edit.

## Data Flow

1. **Entry point**: the operator runs `popoto-transfer <sub> …`; the console script
   calls `popoto.transfer.cli:main(sys.argv[1:])`.
2. **Parse**: `argparse` validates flags and choice values. Bad usage exits 2 before
   anything is imported.
3. **Import popoto**: the first heavy import happens here, inside the subcommand.
   `popoto.redis_db` binds the global client from `REDIS_URL` (or the
   `127.0.0.1:6379/0` fallback) at this moment.
4. **Guard**: the effective database is read off `POPOTO_REDIS_DB`'s pool. If it is 0
   and `--allow-db0` was not passed, print the refusal to stderr and return 1. No
   Redis command has been issued yet.
5. **Resolve model**: CWD onto `sys.path`, `import_module`, `getattr`, subclass
   check. Failure returns 1.
6. **Export path**: open `--out` (or `sys.stdout`) → `export_records(model, stream=…,
   chunk_size=…, **filters)` → the library resolves the key set, hydrates in chunks,
   and writes a manifest line plus one line per record → `ExportResult` returns.
7. **Import path**: open `--in` (or `sys.stdin`) → `import_records(model, stream, …)`
   → the library validates the manifest, then per record: construct, gate, save,
   restore state → `ImportReport` returns with one outcome per record line.
8. **Output**: summary to stderr (human) or stdout (`--json`).
9. **Exit**: 0, or 3 when the result carries failed records.

## Failure Path Test Strategy

### Exception Handling Coverage

The new module introduces exactly three `try`/`except` regions, and none of them
swallow:

- Model resolution: catches `ImportError`/`AttributeError`, re-raises as a CLI error
  that is printed. Tested by asserting the message names the module and the class.
- Library call: catches `ModelException`, `QueryException`, and
  `redis.exceptions.ConnectionError`/`TimeoutError`, prints the message, returns 1.
  Tested per exception type by pointing at a bad manifest, a bad filter name, and an
  unreachable port respectively.
- File open: catches `OSError`, prints the path and the reason, returns 1.

No `except Exception: pass` is permitted in the new module; a Verification row
asserts its absence.

### Empty/Invalid Input Handling

- `--model` empty, missing the colon, colon with an empty half → each is a distinct
  refusal message, each tested.
- `--filter` without `=`, or with an empty key → refused, tested.
- An empty import file (zero bytes) → the library raises `ModelException` for the
  missing manifest; the CLI must render it as a message and exit 1, not a traceback.
- An import file with a manifest and zero record lines → a valid run: report with
  `total == 0`, exit 0.
- Export matching zero records → a valid run: manifest written, `record_count == 0`,
  exit 0. An empty result is not an error.

### Error State Rendering

- Every refusal path is asserted to print to **stderr** and to contain no
  `"Traceback"`, mirroring `tests/test_integrations_cli.py`'s convention.
- The DB-0 refusal is asserted to name the database number, the `--allow-db0` flag,
  and an alternative database.
- The `partial` line from `ImportReport.summary()` must reach the operator: a test
  forces a restore failure and asserts the CLI's stderr carries the `** PARTIAL:`
  block and that the exit code is 3.

## Test Impact

No existing tests are affected. This work adds one new module and one line to
`pyproject.toml`; it modifies no existing code path, no existing signature, and no
existing behavior. `tests/test_transfer_roundtrip.py`,
`tests/test_transfer_reconciliation.py`, and `tests/test_transfer_fidelity_fields.py`
exercise the library API this CLI calls and continue to pass unchanged — they are the
regression net proving the CLI added no behavior of its own to the transfer path.

New file: `tests/test_transfer_cli.py`.

## Rabbit Holes

- **A general query language for `--filter`.** Q objects, `OR`, negation, and
  `__lt`/`__gte` lookups are all expressible in the Python API and none of them have
  an obvious shell syntax. Building a mini-parser for them would cost more than the
  rest of this plan combined and would need its own escaping rules. Keyword equality
  filters cover the migration cases (`--filter project_key=ai`); anything richer is a
  four-line Python script that the guide will show.
- **Model auto-discovery.** "Scan the package and find all `Model` subclasses" sounds
  friendlier than `pkg.mod:Class` and is a trap: it imports arbitrary modules
  eagerly, produces ambiguity when two models share a name, and has no good answer
  for models defined inside functions. Explicit resolution, always.
- **Progress bars and `--verbose` tiers.** `export_records` chunks internally and the
  library reports counts at the end. A live progress display means reaching into the
  library's loop, which would mean editing `export.py` — the one thing this plan
  promises not to do while #572 is in flight.
- **Making the export a point-in-time snapshot.** The library is explicit that it is
  not one, and reports `vanished` for the gap. Adding locking or `WAIT` at the CLI
  layer would be solving a library-level problem at the wrong layer.
- **Multi-model export in one invocation.** `--model` repeated, or a manifest of
  models, invites questions about ordering and cross-model relationships that the
  format does not answer. One model per file is the format's contract; a shell loop
  covers the rest.

## Risks

### Risk 1: The console-script name is effectively permanent

**Impact:** Downstream CI jobs, deployment scripts, and documentation come to depend
on `popoto-transfer`. Renaming it later breaks them silently — the command simply
stops existing.
**Mitigation:** The name is argued explicitly in Technical Approach against two
alternatives, and the PM check-in in the Appetite section exists to confirm it before
the PR merges. The chosen name is also the least-claiming of the three: it does not
take the bare `popoto` name, so a future umbrella command remains available.

### Risk 2: Concurrent work in the same package (#572)

**Impact:** #572 is re-annotating `export.py`, `import_.py`, `format.py`, and
`results.py`. A merge conflict, or a CLI written against a signature #572 changes.
**Mitigation:** Two rules. (a) This work creates `cli.py` and edits none of those
four files, so the only shared surface is `CHANGELOG.md`. (b) **Rebase on `main`
before opening the PR** and re-run `mypy src/popoto/transfer/cli.py` afterwards.
`setup.cfg` sets `follow_imports = silent` globally, so mypy on the new file reports
only that file's errors regardless of whether #572 has landed — the type gate is
independent of the merge order. The CLI calls only `export_records` and
`import_records`, whose signatures #572 annotates but does not change.

### Risk 3: The CLI lands on database 0 anyway

**Impact:** The scenario the guard exists to prevent — an operator runs an import
without `REDIS_URL` set and writes into a live database.
**Mitigation:** The guard reads the live pool, not an environment variable, so it
catches the default-fallback path (`127.0.0.1:6379/0`) as well as an explicit
`REDIS_URL=…/0`. It runs before any Redis command is issued, and before the model
module is imported (so a model module with import-time side effects on Redis cannot
sneak a write past it). Tested with a subprocess whose `REDIS_URL` names database 0,
asserting exit 1 and that `DBSIZE` on database 0 is unchanged.

### Risk 4: Subprocess tests do not see the pytest plugin's database

**Impact:** A child process starts fresh, reads `REDIS_URL`, and binds a different
database than the parent test seeded — the test then asserts against an empty
database and either fails spuriously or, worse, passes vacuously.
**Mitigation:** Tests derive the database number from the live connection
(`POPOTO_REDIS_DB.connection_pool.connection_kwargs["db"]`) and pass
`REDIS_URL=redis://<host>:<port>/<that db>` explicitly in the child's environment.
Never hardcode 15 or 1 — this repo pins 15 in `pyproject.toml` but lanes override
`POPOTO_TEST_DB` per-lane. A guard assertion at the top of the subprocess tests fails
loudly if the derived database is 0.

### Risk 5: Importing the operator's module executes arbitrary code

**Impact:** `--model` runs whatever is at module scope in the named module. A model
module that connects, migrates, or writes at import time does so before the CLI's own
work begins.
**Mitigation:** Inherent to model discovery and not solvable at this layer; the same
is true of `django-admin` and `alembic`. It is stated in the `--help` epilog and in
the guide. The DB-0 guard runs *before* the model import, so the worst case is
bounded by whatever database the operator's `REDIS_URL` names — the guard has already
refused database 0 by then.

## Race Conditions

No new race conditions. The CLI is synchronous, single-threaded, and issues no
concurrent operations of its own.

Two pre-existing library-level timing facts are surfaced, not introduced, by this
work:

### Race 1: Export is not a point-in-time snapshot

**Location:** `src/popoto/transfer/export.py:284-290` (key-set resolution) and the
chunked hydration that follows.
**Trigger:** A record is deleted between key-set resolution and the hydration of its
chunk.
**Data prerequisite:** None — the library already handles it.
**State prerequisite:** None.
**Mitigation:** Already mitigated in the library: the key is counted as `vanished`
and omitted, and `matched_count` is captured at resolution time so the gap is
visible. The CLI's job is only to *print* `vanished` rather than hide it, which
`ExportResult.summary()` already does and the `--json` payload carries as a field.

### Race 2: Import is not atomic across records

**Location:** `src/popoto/transfer/import_.py` per-record loop.
**Trigger:** The CLI process is interrupted (Ctrl-C, SIGTERM) partway through an
import.
**Data prerequisite:** None.
**State prerequisite:** The destination is not under concurrent write for the model —
the library documents this assumption.
**Mitigation:** The documented recovery path is a re-run with
`--on-conflict overwrite`, which converges because keys are preserved. The CLI states
this in the `import --help` text so the operator reads it at the moment they need it,
and a `KeyboardInterrupt` is caught to print "interrupted after N records; re-run
with --on-conflict overwrite" and exit 1 rather than dump a traceback.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #557] Key-regenerating import (`preserve_keys=False`) and reference
  remapping, and any CLI flag named `--preserve-keys` or `--regenerate-keys`. The
  library has no such mode; adding a flag now would fix the option's polarity before
  #557 chooses it. Anti-criterion in Verification asserts the flag name is unused.
- [SEPARATE-SLUG #556] Carrying history-shaped state (EventStream, PredictionLedger,
  CoOccurrence, the AccessTracker access log, FrequencySketch). The CLI prints the
  fidelity roll-up the manifest already carries; it does not change what is carried.
- [SEPARATE-SLUG #572] Fixing the waived mypy errors in `export.py`, `import_.py`,
  `format.py`, and `results.py`. This plan writes one new module and holds it to a
  clean bar; it does not touch the four existing files. Anti-criterion in
  Verification asserts they are unmodified.

## Update System

No update-system changes required beyond one line of packaging. `[project.scripts]`
gains `popoto-transfer = "popoto.transfer.cli:main"`; the script appears on the next
`pip install -e .` or release install. Existing installations get it when they
upgrade — nothing to migrate, no config file, no new dependency, and no environment
variable to propagate.

The CHANGELOG entry must say plainly that a **new console script** is installed, since
that is the one user-visible packaging change and it can collide with a same-named
script in a crowded environment.

## Agent Integration

No agent integration required. `popoto.transfer` is an ORM capability, not an agent
capability: the MCP server in `src/popoto/integrations/mcp_server.py` exposes
discretionary *memory* tools to a running agent, and bulk export/import of a model is
an operator action, not something an agent should be able to invoke mid-turn. Exposing
an import tool over MCP would hand a model the ability to overwrite arbitrary records
from a file path it chooses.

The `popoto-memory` console script and its subcommands are untouched.

## Documentation

### Feature Documentation

- [ ] `docs/guides/export-import.md` — add a `## From the command line` section after
      the existing Python `## Importing` section: install note, the two subcommands
      with a worked example each, the `--model pkg.mod:Class` form and the fact that
      it imports the operator's module, the exit-code table, `--json`, and the DB-0
      refusal with `--allow-db0`. Also state that keys are always preserved and that
      `Q`-object filters need the Python API.
- [ ] `docs/guides/export-import.md:203` currently reads "Async twins
      (`async_export_records` / `async_import_records`) **and a CLI front-end** are not
      part of this API." Adding the CLI section without touching this sentence leaves
      the guide contradicting itself two screens apart. Narrow it to the async twins
      only, and point the reader at the new section.
- [ ] No new page and no `mkdocs.yml` nav change — `guides/export-import.md` is
      already in the nav (`mkdocs.yml:93`), and splitting the CLI into its own page
      would separate it from the concepts it depends on.

### External Documentation Site

- [ ] `mkdocs build --strict` passes (run via `scripts/ci-local.sh docs`).
- [ ] `README.md` — extend the existing **Export & Import** feature bullet
      (`README.md:127`) to mention the `popoto-transfer` command. There is no CLI
      table in the README to add a row to; the feature bullet is the right hook.
- [ ] `CHANGELOG.md` — an `### Added` entry under `[Unreleased]` naming the new
      console script, both subcommands, the exit-code contract, and the DB-0 refusal.

### Inline Documentation

- [ ] Module docstring on `src/popoto/transfer/cli.py` in the style of
      `integrations/cli.py`: what the subcommands are, why imports are lazy, and why
      the summary goes to stderr.
- [ ] Docstrings on `main`, the resolver, and the guard, with `Args:`/`Returns:`/
      `Raises:` sections matching the repo's Google style.
- [ ] `--help` epilog with two runnable examples, per `USAGE_EPILOG` in
      `integrations/cli.py`.

## Success Criteria

- [ ] `popoto-transfer export --model pkg.mod:Class --out FILE` writes a JSONL file
      that `popoto-transfer import` reads back with every record landed.
- [ ] `--filter k=v` narrows the export and is reported in the summary's filter line.
- [ ] `--out -` streams JSONL on stdout with the summary on stderr, so the output can
      be piped without corruption.
- [ ] `--json` emits a parseable object carrying the counts, on stdout, and is refused
      together with `--out -`.
- [ ] Exit 0 on a clean run, 1 on an operational failure, 3 when any record fails to
      land.
- [ ] Both subcommands refuse to run on database 0 without `--allow-db0`, before any
      Redis command and before the model module is imported; the message names the
      database, the flag, and an alternative.
- [ ] Every failure path prints a message and no traceback.
- [ ] A failed export leaves a pre-existing `--out` file byte-identical and no `.part`
      file behind.
- [ ] An `--on-conflict error` collision exits 1 (not 3) and its message carries the
      library's partial-write warning; an `--on-conflict skip` collision exits 0.
- [ ] `mypy src/popoto/transfer/cli.py` reports zero errors.
- [ ] `export.py`, `import_.py`, `format.py`, `results.py` are byte-identical to
      `main`.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (cli)**
  - Name: `transfer-cli-builder`
  - Role: `src/popoto/transfer/cli.py`, the `pyproject.toml` entry, and
    `tests/test_transfer_cli.py`
  - Agent Type: builder
  - Resume: true

- **Validator (cli)**
  - Name: `transfer-cli-validator`
  - Role: Verifies exit codes, the DB-0 refusal, stream separation, and that the four
    pre-existing transfer modules are unmodified
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: `transfer-cli-docs`
  - Role: guide section, README bullet, CHANGELOG entry
  - Agent Type: documentarian
  - Resume: true

### Available Agent Types

Standard Tier 1 set. The builder carries `Domain: redis-popoto` framing for the DB-0
guard and the subprocess-test database derivation (see `DOMAIN_FRAMING.md`).

## Step by Step Tasks

### 1. Build the CLI module

- **Task ID**: build-cli
- **Depends On**: none
- **Validates**: `tests/test_transfer_cli.py` (create)
- **Assigned To**: `transfer-cli-builder`
- **Agent Type**: builder
- **Parallel**: false
- Create `src/popoto/transfer/cli.py` with a module docstring in the style of
  `src/popoto/integrations/cli.py`, and `main(argv: list[str] | None = None) -> int`.
- Build the parser: `prog="popoto-transfer"`, subparsers `export` and `import`, a
  `USAGE_EPILOG` with two runnable examples. Note `import` is a Python keyword, so the
  subparser name is the string `"import"` and dispatch is on `args.command`, never an
  attribute name.
- Shared flags on both subparsers: `--model` (required), `--json`, `--allow-db0`.
- `export`: `--out PATH` (default `-`), `--filter k=v` (`action="append"`),
  `--chunk-size` (default `DEFAULT_CHUNK_SIZE`, imported from `popoto.transfer`).
- `import`: `--in PATH` (default `-`), `--on-conflict` (`choices=["error","skip",
  "overwrite"]`, default `error`), `--on-write-gate` (`choices=["reject","bypass"]`,
  default `reject`), `--on-embedding-mismatch` (`choices=["error","carry",
  "regenerate"]`, default `error`). Mirror the library defaults exactly.
- `import --help` text states that keys are always preserved, that a re-run with
  `--on-conflict overwrite` converges, and that import is not atomic across records.
- Implement `resolve_model(spec)`: split on `:` (exactly one colon, both halves
  non-empty), prepend `os.getcwd()` to `sys.path` if absent, `import_module`,
  `getattr`, assert `isinstance(obj, type)` and `issubclass(obj, Model)`. Distinct
  message per failure point.
- Implement the DB-0 guard reading
  `POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db", 0)`. Refuse unless
  `--allow-db0`. The refusal message names the database, the flag, and
  `REDIS_URL=redis://localhost:6379/1`; export and import word the consequence
  differently (reads from / writes to). Run the guard **before** resolving the model.
- Implement `--json` rendering with `dataclasses.asdict` +
  `json.dumps(..., indent=2, default=str)`, adding an explicit `counts` object for
  the five import categories. Refuse `--json` together with `--out -` (exit 1).
- Human summary to stderr, always. JSONL data to `--out` / stdout.
- For `--out PATH` (not `-`), write to `PATH + ".part"` and `os.replace` it onto
  `PATH` only after `export_records` returns; unlink the partial on any exception so a
  failed run never truncates a previous export.
- Exit codes 0 / 1 / 3 per the Technical Approach table. Catch `ModelException`,
  `QueryException`, `redis.exceptions.ConnectionError`, `TimeoutError`, `OSError`,
  and `KeyboardInterrupt`; print a message, never a traceback.
- Keep every heavy import (`popoto`, `redis`) inside the subcommand functions, per
  the startup-latency convention in `integrations/cli.py`.
- Add `if __name__ == "__main__": raise SystemExit(main())` so the module is runnable
  as `python -m popoto.transfer.cli` in tests without depending on the installed
  console script.
- Do **not** edit `export.py`, `import_.py`, `format.py`, `results.py`, or
  `transfer/__init__.py`.

### 2. Declare the console script

- **Task ID**: build-entrypoint
- **Depends On**: build-cli
- **Validates**: `tests/test_transfer_cli.py::test_console_script_help` (the `--help` smoke check); the packaging line itself is covered by the "Console script declared" Verification row
- **Assigned To**: `transfer-cli-builder`
- **Agent Type**: builder
- **Parallel**: false
- Add `popoto-transfer = "popoto.transfer.cli:main"` under the existing
  `[project.scripts]` in `pyproject.toml` (below `popoto-memory`). One line; change
  nothing else in the file.
- Reinstall editable **into the worktree venv, with the interpreter named explicitly**
  (`uv pip install --python .venv/bin/python -e '.[dev,embeddings,benchmark,mcp]'`) so
  the console script exists for the test run, and confirm `popoto-transfer --help`
  exits 0. A bare `pip install -e .` here would install into the primary venv (see
  Prerequisites) and validate a checkout where `cli.py` does not exist.
- Tests invoke `python -m popoto.transfer.cli`, never the console script, so the suite
  does not depend on the reinstall having happened. The `--help` check above is the
  only place the installed script itself is exercised.

### 3. Tests

- **Task ID**: build-tests
- **Depends On**: build-cli
- **Assigned To**: `transfer-cli-builder`
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_transfer_cli.py` with a module-scope model defined in the test
  file and a helper module written to `tmp_path` for the resolution tests.
- In-process tests calling `main([...])` with `capsys`, following
  `tests/test_integrations_cli.py`: exit codes, refusal messages, absence of
  `"Traceback"`, stderr-vs-stdout separation.
- Subprocess tests invoking `[sys.executable, "-m", "popoto.transfer.cli", ...]`
  with an explicit `REDIS_URL` derived from
  `POPOTO_REDIS_DB.connection_pool.connection_kwargs` (host, port, db). Assert the
  derived db is non-zero before running; never hardcode a database number. Set `cwd`
  to the repo root and `PYTHONPATH` so the child resolves this checkout.
- Round-trip test: seed N records, export to `tmp_path`, delete them, import back,
  assert every record landed and the objects match.
- Filter test: `--filter` narrows the export; the summary names the filter.
- DB-0 test: subprocess with `REDIS_URL=redis://localhost:6379/0`, assert exit 1,
  assert the message names `--allow-db0`, and assert `DBSIZE` on database 0 is
  unchanged across the call. **This test must never write to database 0** — it only
  reads `DBSIZE` for the assertion.
- Exit-code-3 test: import a file whose records the destination's **write gate**
  refuses (`save()` returns falsy), assert exit 3 and that the reasons reach stderr.
  Do **not** use an `on-conflict error` collision for this: that path raises
  `ModelException` from inside the per-record loop and exits 1, never 3. See the
  library-path-to-exit-code table in Technical Approach.
- Exit-code-1 collision test: import a file whose first key already exists with
  `--on-conflict error`, assert exit 1, assert stderr carries the library's
  "records before this one are already written" warning and the
  `on_conflict='overwrite'` recovery hint, and assert no traceback.
- `--on-conflict skip` test: a colliding key is reported `SKIPPED` and the run exits
  **0**, proving a deliberate skip is not miscounted as a failure to land.
- `--json` test: parse stdout, assert the counts object sums to the records read.
- `--out -` test: assert the JSONL lands on stdout and the summary on stderr, and
  that `--out - --json` exits 1.
- Empty-input tests: zero-byte import file, manifest-only import file, zero-match
  export.
- Failed-export-preserves-destination test: write a sentinel file at the `--out` path,
  run an export with a bad `--filter`, assert exit 1, assert the sentinel file is
  byte-identical afterwards, and assert no `.part` file is left in the directory.
- Resolution tests: no colon, empty half, missing module, missing attribute,
  attribute that is not a Model — one distinct message each.
- Run the suite with `POPOTO_TEST_DB` set to a non-zero database.

### 4. Validate

- **Task ID**: validate-cli
- **Depends On**: build-cli, build-entrypoint, build-tests
- **Assigned To**: `transfer-cli-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run every command in the Verification table and report each result.
- Confirm the four pre-existing transfer modules are unmodified against `main`.
- Confirm no `except Exception: pass` and no `--preserve-keys` in the new module.
- Reproduce the DB-0 refusal by hand and confirm nothing was written.

### 5. Documentation

- **Task ID**: document-feature
- **Depends On**: validate-cli
- **Assigned To**: `transfer-cli-docs`
- **Agent Type**: documentarian
- **Parallel**: false
- Add the `## From the command line` section to `docs/guides/export-import.md`.
- Extend the **Export & Import** bullet in `README.md`.
- Add the `### Added` entry to `CHANGELOG.md` under `[Unreleased]`.
- Run `mkdocs build --strict`.

### 6. Final validation

- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: `transfer-cli-validator`
- **Agent Type**: validator
- **Parallel**: false
- Rebase on `main` (picking up #572 if it has landed) and re-run the full Verification
  table.
- Verify every Success Criteria checkbox.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| CLI tests pass | `POPOTO_TEST_DB=1 python -m pytest tests/test_transfer_cli.py -q` | exit code 0 |
| Transfer suite unbroken | `POPOTO_TEST_DB=1 python -m pytest tests/test_transfer_roundtrip.py tests/test_transfer_reconciliation.py tests/test_transfer_fidelity_fields.py -q` | exit code 0 |
| Full suite passes | `POPOTO_TEST_DB=1 python -m pytest -q -m "not slow"` | exit code 0 |
| Lint clean | `python -m ruff check src/` | exit code 0 |
| Format clean | `python -m black --check src/ tests/` | exit code 0 |
| New module type-clean | `python -m mypy src/popoto/transfer/cli.py` | output contains no issues found |
| Console script declared | `grep -c 'popoto-transfer = "popoto.transfer.cli:main"' pyproject.toml` | output > 0 |
| Help works | `python -m popoto.transfer.cli --help` | exit code 0 |
| DB-0 refusal names the flag | `REDIS_URL=redis://localhost:6379/0 python -m popoto.transfer.cli export --model popoto:Model --out - 2>&1` | output contains --allow-db0 |
| DB-0 refusal exits non-zero | `REDIS_URL=redis://localhost:6379/0 python -m popoto.transfer.cli export --model popoto:Model --out - > /dev/null 2>&1` | exit code 1 |
| No `--preserve-keys` flag (anti-criterion, #557) | `grep -Ec -- '--preserve-keys|--regenerate-keys' src/popoto/transfer/cli.py` | match count == 0 |
| Pre-existing transfer modules untouched (anti-criterion, #572) | `git diff --name-only main...HEAD -- src/popoto/transfer/export.py src/popoto/transfer/import_.py src/popoto/transfer/format.py src/popoto/transfer/results.py \| grep -c .` | match count == 0 |
| Failed export preserves the destination | `POPOTO_TEST_DB=1 python -m pytest tests/test_transfer_cli.py -q -k "preserves_destination"` | exit code 0 |
| No silent excepts | `grep -A1 'except Exception:' src/popoto/transfer/cli.py \| grep -c '^ *pass$'` | match count == 0 |
| Guide documents the CLI | `grep -c 'popoto-transfer' docs/guides/export-import.md` | output > 0 |
| Guide no longer denies a CLI exists | `grep -c 'and a CLI front-end' docs/guides/export-import.md` | output is `0` |
| README mentions the CLI | `grep -c 'popoto-transfer' README.md` | output > 0 |
| CHANGELOG entry present | `grep -c 'popoto-transfer' CHANGELOG.md` | output > 0 |
| Docs build | `python -m mkdocs build --strict` | exit code 0 |

## Critique Results

**Critique run:** 2026-09-04, FULL depth. **Verdict:** READY TO BUILD (with concerns) — 0 blockers, 5 concerns, 2 nits. **Revision applied** 2026-09-04: all 7 findings addressed in plan text. One critique round only, per the routing instruction. On concern 4 the appetite was kept as-is and the decision recorded in Open Questions item 3. On concern 5 the prerequisite was re-checked against the worktree venv's interpreter and passes; the critic read the ambient interpreter.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | Risk & Robustness + structural | `on_conflict="error"` collision raises `ModelException` from **inside** the per-record loop (`src/popoto/transfer/import_.py:190-198`), after earlier records have already been saved. The CLI's generic `except ModelException` then returns **1**, so exit 1 on import can mean "partially landed", contradicting the exit-code table's framing of 1 as "the run failed". Separately, Step 3's exit-code-3 test names "an `on-conflict error` collision" as a way to produce exit 3 — it cannot, that path raises rather than recording a `rejected` outcome. | Technical Approach (exit codes), Step 3 | The library raises at `import_.py:190-198` with text "records before this one are already written"; there is no progress count on the exception, and fixing it properly would require editing `import_.py` (a No-Go for #572 coordination). In-scope fix is text-only: (a) drop the "`on-conflict error` collision" option from the Step 3 exit-code-3 test and keep only the write-gate path (`on_write_gate="reject"` produces a `REJECTED` outcome at `import_.py:239-242`, which is what actually yields exit 3); (b) state in the exit-code table and in `import --help` that exit 1 on import may mean "partially landed — re-run with `--on-conflict overwrite`". |
| CONCERN | Risk & Robustness | `--out PATH` is opened (and truncated) before `export_records` runs, but a bad `--filter` name raises `QueryException` from the query layer *after* that. A typo'd filter therefore zeroes the operator's existing backup file while the CLI exits 1 with a clean message — the same class of data loss the DB-0 guard exists to prevent, relocated. | Data Flow step 6; Technical Approach (Filters) | In the `export` handler, resolve the filter before touching the destination: call `model_class.query.filter(**filters)` (or `export_records` with `stream=None` deferred) so `QueryException` surfaces first, THEN `open(args.out, "w")`. Alternative: write to `args.out + ".part"` and `os.replace()` into place only on success. Either fix is entirely inside the new `cli.py`; `export.py` is not touched. |
| CONCERN | History & Consistency | `docs/guides/export-import.md:203` currently asserts "Async twins (`async_export_records` / `async_import_records`) **and a CLI front-end** are not part of this API." The plan inserts `## From the command line` earlier in the same file but never updates that sentence, so the page would both document and deny the CLI. | Documentation > Feature Documentation | Edit `docs/guides/export-import.md:203` to drop the "and a CLI front-end" clause, leaving the async-twin exclusion intact. Add this as an explicit checkbox under Task 5 (`document-feature`) so the documentarian does not miss it. |
| CONCERN | Scope & Value | The plan's own Freshness Check states this is not blocking any consumer ("it is for the next operator"), yet it ships `--json`, a three-way exit-code taxonomy, stream-separation rules, and a subprocess test matrix the Appetite section itself calls "a known-sharp edge in this repo" — speculative surface for an unconfirmed user. | Freshness Check / Appetite | If the appetite is trimmed: keep `resolve_model`, the DB-0 guard, both subcommands, and the stderr human summary; drop `--json`, the `counts` object, and the exit-code-3 branch (collapse to 0/1), plus their Verification rows and Step 3 tests. If the appetite is kept as-is (the recommended reading — `--json` and exit 3 are the plan's own answer to hazards 2 and 3 in the Problem section), record that decision in Open Questions so it is not re-litigated at review. |
| CONCERN | Structural check | Prerequisite 3 ("editable install resolves to this checkout") **fails in this worktree**: `popoto.__file__` resolves to `/Users/valorengels/src/popoto/src/popoto/__init__.py`, not `.worktrees/sdlc-555`. Task 2 asks the builder to `pip install -e .` and confirm `popoto-transfer --help` exits 0 — done in this state, that would install the console script pointing at the *main* checkout, where `cli.py` does not exist. | Prerequisites; Step 2 | Before Task 2, run `pip install -e '.[dev,embeddings,benchmark,mcp]'` **from the worktree root** and re-assert prerequisite 3. Use `python -m popoto.transfer.cli` (which the plan already mandates via the `__main__` guard) for every test invocation so the suite never depends on which checkout owns the installed console script. |
| NIT | History & Consistency | The anti-criterion `grep -c -- '--preserve-keys\|--regenerate-keys'` uses BRE `\|` alternation, a GNU extension. | Verification table | — |
| NIT | Structural check | Task 2 (`build-entrypoint`) is the only task with no `Validates:` field. | Step by Step Tasks | — |

---

## Open Questions

None blocking. Two decisions are made in-plan and recorded here so a reviewer can
overturn either without re-deriving the reasoning:

1. **Entry-point name** — `popoto-transfer`, a second console script, rather than a
   bare `popoto` umbrella or a `popoto-memory` subcommand. Argued in Technical
   Approach. Overturning this changes one line of `pyproject.toml` and the docs, and
   is cheapest before the PR merges because the name is published in package
   metadata.
2. **One `--allow-db0` flag covering both read and write** rather than an export-only
   opt-in. Argued in Technical Approach: an export on database 0 is a disclosure even
   though it is read-only, and one flag is one thing to audit.

3. **Scope: why a Medium and not a thin wrapper.** Nothing is blocked on this work —
   the Freshness Check says plainly it is "for the next operator" — yet the plan ships
   `--json`, a three-way exit-code taxonomy, and a subprocess test matrix the Appetite
   section itself calls a known-sharp edge. That is deliberate, and each piece answers
   a hazard named in Problem rather than being speculative polish: `--json` and the
   exit codes exist because Problem item 3 is "there is no exit code" and item 2 is
   "the reconciliation report is printed and ignored", and the subprocess tests exist
   because the DB-0 guard is the whole point of Problem item 1 and cannot be proven
   in-process, where the connection is already bound to the test database. Recorded
   here so a reviewer can overturn the scope on its merits instead of it being
   re-litigated at review time. Overturning it means dropping `--json` and collapsing
   exit 3 into 0; the DB-0 guard and its subprocess test are not negotiable, since
   they are the reason the CLI is safer than the throwaway script it replaces.
