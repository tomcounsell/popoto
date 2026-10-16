# Plan: popoto 1.10.1 (LoCoMo dataset note + version bump)

Patch release carrying one docs fix. No code changes. No tag, no release: the
lead tags `v1.10.1` after merge.

## Premise (verified against the tree at `ce04dbe`, equal to `origin/main`)

- The LoCoMo loader (`tests/benchmarks/datasets/locomo.py`) downloads
  `locomo10.json` from the Hugging Face repo `snap-research/locomo`. That repo
  now returns "not found" (observed while carrying PR #842, independent of hub
  version), so a run without `--fixture` or a pre-placed cache file fails.
- Where readers are told how to fetch or run LoCoMo:
  - `docs/benchmarks.md` "Running the Benchmark" block: `# Full LoCoMo run:`
    followed by `run_external --dataset locomo` (around line 91).
  - `tests/benchmarks/README.md`: "External benchmark (requires dataset
    download + Redis)" block, `run_external --dataset locomo` (around line 85).
  Other mentions (`docs/benchmarks.md` ~374 and ~471) describe the dataset a
  past run used, not how to fetch it; they stay as written.
- Version locations: `pyproject.toml` (`version = "1.10.0"`), `uv.lock`
  (popoto package entry), `examples/uv.lock` (editable popoto entry; would go
  stale under `examples.yml`'s `uv lock --check` if left), `CHANGELOG.md`.
  `popoto.__version__` reads installed metadata (`importlib.metadata`), so it
  has no literal to edit.

## What will be built

1. `docs/benchmarks.md`: under the `# Full LoCoMo run:` comment, one or two
   sentences (as a note right after the code block, or a comment line inside
   it) saying the `snap-research/locomo` dataset is no longer available on
   Hugging Face, so the full run works only from a copy of `locomo10.json`
   already at `~/.cache/popoto_benchmarks/locomo.json` or passed with
   `--fixture`; the bundled fixture still runs offline. No new source named.
2. `tests/benchmarks/README.md`: the same fact in one comment line next to the
   `--dataset locomo` command.
3. Version 1.10.0 → 1.10.1 in `pyproject.toml`, `uv.lock`, and
   `examples/uv.lock` (by hand-editing the popoto entry, then confirming with
   `uv lock --check` in both directories that nothing else would change).
4. `CHANGELOG.md`: rename the existing `## [Unreleased]` section to
   `## [1.10.1] - 2026-10-16`, add a `### Documentation` entry for the LoCoMo
   note, and open a fresh empty `## [Unreleased]` above it. The #832/#837
   entries already in Unreleased are on main and ship in 1.10.1 too.

## Out of scope

- The loader's docstring and `HF_REPO_ID` in `locomo.py` (code; the brief says
  no code changes). Noted in the delivery as follow-up debt.
- Finding or naming a replacement source for the dataset.
- Tagging, building, or publishing.
- Historical mentions of the HF release in results and `docs/plans/`.

## Stakes

Docs text and version strings only, fully reversible by one revert, with no
runtime behaviour change.

Critique rounds: 0. Review rounds: 0.

## Tech debt added

None.

## Tests that show it works

- `pytest tests/test_version.py` after `pip install -e .`/`uv sync`: installed
  `popoto.__version__` starts with `1.10.1`, matching `pyproject.toml`.
- `uv lock --check` passes at the root and in `examples/` (lock consistent
  with the bumped version; the case beyond the obvious one is the examples
  lock, which a root-only bump would leave stale).
- `grep -rn "1\.10\.0"` over `pyproject.toml uv.lock examples/uv.lock` returns
  nothing; `grep` of `docs/benchmarks.md` and `tests/benchmarks/README.md`
  shows the note beside each LoCoMo run command.
- `pytest tests/test_docs_redis_url.py tests/benchmarks/test_gen_benchmark_pages.py`
  (doc-shape tests that read these files) pass.
- `mkdocs build --strict` succeeds (the note renders, no broken anchors).
- `git diff --stat` touches only the six files above plus this plan: no
  `src/` change.
