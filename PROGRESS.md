# Progress: bm25_stable_tie_break (#446)

## Done
- Task 1 build-comparator: two-level Lua comparator + docstring (3d1ebd2)
- Task 2 build-tests: TestBM25TieOrdering, 4 regressions, RED-confirmed vs main (64adcb9)
- Task 3 build-docs-relax: README rule 3 -> best practice, default.py + test_csr docstring (a843e9c)
- Finishing: black scoped to touched files (232766b); mypy zero new errors (947 baseline == 947 after; per-file profiles identical main vs HEAD)

## In progress
(none — complete)

## Left
(none)

## Notes
- Tests in this worktree MUST run with PYTHONPATH=$PWD/src, else the pytest
  plugin collapses onto the editable install pointing at the MAIN checkout.
- Black 26.5.1 has repo-wide drift (37 files) pre-existing on main; only
  touched files formatted here.
