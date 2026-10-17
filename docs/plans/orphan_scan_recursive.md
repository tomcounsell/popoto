---
status: Planning
type: bug
appetite: Small
owner: valorengels
created: 2026-10-09
tracking: https://github.com/tomcounsell/popoto/issues/723
---

# Recursive orphan-artifact scan with an explicit "unpublished" list

## Problem

`_warn_orphan_artifacts` in `docs/scripts/gen_benchmark_pages.py` globs only
`external/*_latest*.md`. Anything one directory deeper is never looked at, so a
`_latest` artifact dropped into a subdirectory with no `Spec` goes unnoticed.

Verified on `main` (5667f96), run with `.venv/bin/python`:

- Today the scan warns on 5 top-level artifacts:
  `external/locomo_latest_judged` and the four
  `external/locomo_latest_ext-*_judged`.
- Nested `_latest*.md` files it never sees:
  - `external/validity_586/` — 3 (`longmemeval_s_latest`,
    `..._sup-content-identity`, `..._sup-content-identity_nogate`)
  - `external/graph_eval_484/` — 3 (`locomo_latest`, `_graph`, `_hybrid`)
  - `external/graph_eval_484/singlehop/` — 2 (`locomo_latest`, `_graph`)

Both subdirectories are study archives, cited in prose from
`docs/benchmarks.md` (lines 616, 892, 899) and, for validity_586,
`tests/benchmarks/README.md:282`. Neither is meant to get a generated page.

## Solution

1. **Recursive scan.** Replace `external_dir.glob("*_latest*.md")` with
   `external_dir.rglob("*_latest*.md")`. The stem is already computed relative
   to `root`, so nested stems come out as e.g.
   `external/validity_586/longmemeval_s_latest` and compare correctly against
   `Spec.stem`. Scope stays `external/` (what the issue names); `csr/`, `rlt/`,
   `siq/` are untouched.

2. **Explicit unpublished list, beside `SPECS`.** Add a module-level mapping

   ```python
   UNPUBLISHED_DIRS: dict[str, str] = {
       "external/validity_586": "#586 three-arm validity study; cited from benchmarks.md, not a page",
       "external/graph_eval_484": "#484 graph-eval study; cited from benchmarks.md, not a page",
   }
   ```

   keyed by `RESULTS_ROOT`-relative directory, value is the reason. A
   directory listed here, and everything below it, is skipped by the scan.
   `_warn_orphan_artifacts` gains a parameter
   `unpublished: Mapping[str, str] = UNPUBLISHED_DIRS` next to the existing
   injectable `root`, so tests pass their own.

   Why a list in code rather than a marker file dropped into the directory:
   it matches the existing design (published set is an explicit `SPECS` list,
   not a directory convention), puts every "deliberately not published"
   decision in one reviewable place with its reason, and a dotfile marker is
   easy to miss in review and easy to copy along with a directory by accident.

   Matching is by path prefix on whole segments
   (`stem == d or stem.startswith(d + "/")`), so `external/validity_586`
   does not also silence a hypothetical `external/validity_5860/`.

3. **Docstrings.** Update the function docstring and the module docstring
   lines 62–66 to say the scan is recursive and how to mark a directory
   unpublished. Update the test module docstring bullet to match.

No change to warning text, return value, or the never-raise behavior.

## Governance note

This extends the coverage of an existing, non-blocking build-time warning
that Tom asked for in the Brief; it adds no new check, gate, hook, validator,
or review step, never raises, and cannot fail a build. The incident is #723
itself (nested artifacts are invisible to the scan). Flagged here so the
verifier sees the reasoning rather than infers it.

## Out of scope

- The 5 existing top-level `*_judged` warnings. They are real current output;
  whether they should get `Spec`s or be listed as unpublished is a separate
  decision (they are files, not a subdirectory). Named in the delivery as a
  product note.
- Scanning `csr/`, `rlt/`, `siq/` or anything outside `external/`.
- Publishing any of the nested artifacts.

## Tests (`tests/benchmarks/test_gen_benchmark_pages.py`)

Existing four orphan tests stay and must pass unchanged (they call with
`gen.SPECS, root=tmp_path` and rely on the default `unpublished`; their tmp
trees have no subdirectories, so the default list is irrelevant to them).

New, all against `tmp_path`:

1. **Nested orphan warns** — `external/study_x/foo_latest.md`, empty
   `unpublished` → returned stems contain `external/study_x/foo_latest`;
   stderr has `WARNING` and the stem.
2. **Two levels deep warns** — `external/a/b/foo_latest.md` → reported.
3. **Marked directory stays quiet** — same file as (1) with
   `unpublished={"external/study_x": "reason"}` → `[]`, no `WARNING`.
4. **Marking covers subdirectories** — `external/study_x/sub/foo_latest.md`
   with `external/study_x` marked → quiet.
5. **Marking is segment-exact** — `external/study_x10/foo_latest.md` with
   `external/study_x` marked → still warns.
6. **Marking one directory leaves siblings and top level loud** — marked
   `study_x` plus an orphan in `study_y/` and at `external/` top level → both
   reported, `study_x` one not.
7. **Nested lone `.json` ignored** — `external/study_x/bar_latest.json` only →
   quiet (recursive scan keeps the `.md`-only rule).
8. **Default list matches the committed tree** — with the real `RESULTS_ROOT`
   and default `unpublished`, no returned stem starts with
   `external/validity_586/` or `external/graph_eval_484/`, and every key of
   `UNPUBLISHED_DIRS` is an existing directory under `RESULTS_ROOT` (a
   stale entry for a moved/deleted directory is caught). Read-only on the
   committed tree.

Suites to run: `pytest tests/benchmarks/test_gen_benchmark_pages.py` with the
docs extra installed (the module `importorskip`s `mkdocs_gen_files`; `.venv`
has it), plus `mkdocs build --strict` if it runs locally, to confirm the
build stays green and the warning set is exactly the 5 known top-level
`*_judged` stems.

## Tasks

1. Edit `_warn_orphan_artifacts`: `rglob`, `unpublished` parameter, prefix
   skip.
2. Add `UNPUBLISHED_DIRS` after `SPECS` with the two entries and reasons.
3. Update docstrings (module, function, test module).
4. Add tests 1–8.
5. Run the test file and the docs build; confirm warning output unchanged
   from today's 5 lines.
