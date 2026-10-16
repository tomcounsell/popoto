# Plan: land PR #842 (huggingface-hub 1.32.0 -> 2.1.1)

Task f67b25bd6ed5. Tom picked the PR; the job is to confirm what popoto uses from
huggingface_hub still works on 2.x, fix anything that does not, and land it on main.

## What the PR actually moves

`uv.lock` only (Dependabot `lockfile-only` lane; `pyproject.toml` floors untouched,
so no downstream consumer is affected). Head `66b95e1`, based on main `5667f96`.
Four packages move, not one:

| package | before | after | note |
|---|---|---|---|
| huggingface-hub | 1.32.0 | 2.1.1 | the major; `httpx` dependency replaced by `httpx2` |
| sentence-transformers | 6.1.0 | **5.7.0** | a *downgrade* across a major, presumably because 6.x caps hub < 2 |
| transformers | 5.12.1 | 5.19.0 | minor |
| tokenizers | 0.22.2 | 0.23.3 | minor |

The sentence-transformers downgrade is the part a green CI does not speak to, so it
is checked as carefully as the hub bump itself.

## What popoto uses

- `src/` never imports huggingface_hub. `src/popoto/embeddings/sentence_transformers.py`
  uses only `SentenceTransformer(model_name)` and `model.encode(list).tolist()`,
  stable across sentence-transformers 5.x and 6.x.
- Tests and benchmarks call the hub directly:
  - `hf_hub_download(repo_id=, filename=, repo_type="dataset", local_dir=)` in
    `tests/benchmarks/datasets/longmemeval_s.py` and `locomo.py`.
  - `try_to_load_from_cache(repo_id=, filename=)` returning `str` when cached, in
    `tests/embeddings/test_sentence_transformers_provider.py` and
    `tests/benchmarks/test_external.py`. Both helpers swallow every exception and
    return False, so a renamed or broken API would **silently skip** the real-model
    tests rather than fail them. A green suite alone is therefore not evidence.

## What will be done

1. Build a fresh venv from the PR head's lock (`uv sync --locked` with `dev`,
   `embeddings`, `benchmark`, `mcp`) and confirm it resolves to this checkout.
2. Check the 2.x API directly, not through the swallowing helpers:
   - `try_to_load_from_cache("sentence-transformers/all-MiniLM-L6-v2", "config.json")`
     returns a `str` path (warming the HF cache first if needed), so the gated tests
     run instead of skipping.
   - `hf_hub_download` with the exact kwargs above fetches a small real dataset file
     into a temp `local_dir` and returns a path that exists.
   - Read the 2.x signatures/changelog for removed or renamed parameters on those two
     functions (e.g. `local_dir_use_symlinks`, `resume_download`, return types).
3. Run, on the 2.x venv with Redis DB 15:
   - `tests/embeddings/` with the model cached, confirming the real-encode tests
     report as passed, not skipped (`-rs` output).
   - the hybrid smoke in `tests/benchmarks/test_external.py` that is gated on the
     cache helper, same pass-not-skip check.
   - the full suite via `pytest`, compared against main on the same machine to
     separate pre-existing or DB-15-contention failures from new ones.
   - `scripts/check_lock_imports.py` and `uv lock --check`.
4. Outcome:
   - Nothing breaks: no code change. The PR head commit is brought onto the task
     branch so the merge lands exactly Dependabot's lock change.
   - Something breaks in a popoto call site: fix the call site (test or benchmark
     code) so it works on both the 0.23 floor and 2.x, with a test that exercises
     it, committed on the task branch on top of the lock change.
   - Something breaks that popoto cannot fix (e.g. sentence-transformers 5.7 fails
     with hub 2.x): do not land; report the evidence and the upstream blocker.
5. Merge to main through the kernel's merge; confirm #842 shows merged or is closed
   referencing the merge commit.

## Out of scope

- Raising any `pyproject.toml` floor (`huggingface_hub>=0.23`,
  `sentence-transformers>=2.7`); the lockfile-only doctrine in CLAUDE.md holds.
- Running the full LongMemEval/LoCoMo benchmarks; one real download per loader
  call shape is enough to prove the API.
- Re-pinning sentence-transformers back to 6.x; if 6.x requires hub < 2, the
  downgrade is the price of this bump and is named in the delivery.
- Making the cache helpers stop swallowing exceptions; noted as a product note only.

## Tests that show it works

- Real `try_to_load_from_cache` on 2.x returns a `str` (direct call, not via helper).
- Real `hf_hub_download(..., repo_type="dataset", local_dir=tmp)` on 2.x returns an
  existing path.
- `tests/embeddings/test_sentence_transformers_provider.py` real-encode test passes
  (not skipped) on sentence-transformers 5.7.0 + hub 2.1.1, producing 384-dim floats.
- `test_external.py` hybrid smoke passes, not skipped.
- Full suite on the 2.x venv: no new failures against main.
- Edge: the import-error path in the two dataset loaders is unchanged (no code moved).

## Stakes and loops

Low: lockfile-only change to dev/benchmark extras, no `src/` or published-floor
change, reverted by one commit. Critique 0, review 0.
