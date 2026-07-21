# SIQ Benchmark — adapter: `native`

Subconscious Injection Quality (issue #459). Deterministic committed fixtures; no runtime RNG. See `docs/benchmarks.md`.

## Aggregate

| metric | value |
|---|---|
| precision @ budget | 0.667 |
| recall @ budget | 1.000 |
| budget efficiency | 0.706 |
| mean anticipation lead (turns) | 4.000 |
| anticipation misses | 0 / 5 targets |

## Per trace

| trace | recall | precision | efficiency | mean lead | misses |
|---|---|---|---|---|---|
| coreference_relocation | 1.000 | 1.000 | 1.000 | 4.000 | 0/1 |
| implication_deadline | 1.000 | 0.500 | 0.553 | 4.000 | 0/1 |
| multi_recall_preferences | 1.000 | 0.667 | 0.700 | 4.000 | 0/2 |
| need_to_know_allergy | 1.000 | 0.500 | 0.571 | 4.000 | 0/1 |
