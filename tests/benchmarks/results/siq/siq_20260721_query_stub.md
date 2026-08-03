# SIQ Benchmark — adapter: `query_stub`

Subconscious Injection Quality (issue #459). Deterministic committed fixtures; no runtime RNG. See `docs/benchmarks.md`.

## Aggregate

| metric | value |
|---|---|
| precision @ budget | n/a |
| recall @ budget | 0.000 |
| budget efficiency | n/a |
| mean anticipation lead (turns) | n/a |
| anticipation misses | 5 / 5 targets |

## Per trace

| trace | recall | precision | efficiency | mean lead | misses |
|---|---|---|---|---|---|
| coreference_relocation | 0.000 | n/a | n/a | n/a | 1/1 |
| implication_deadline | 0.000 | n/a | n/a | n/a | 1/1 |
| multi_recall_preferences | 0.000 | n/a | n/a | n/a | 2/2 |
| need_to_know_allergy | 0.000 | n/a | n/a | n/a | 1/1 |
