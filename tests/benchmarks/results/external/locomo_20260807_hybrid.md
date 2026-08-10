# Popoto External Benchmark: locomo

**Run date:** 2026-08-07  
**Retrieval mode:** hybrid  
**Ranking unit:** turn (gold-blind, first occurrence wins)  
**Python:** 3.12.13  
**Platform:** macOS-26.5.2-arm64-arm-64bit  
**Sample mode:** stratified  
**Seed:** 0  
**Limit:** 250  

## Summary

| Metric | Value |
|--------|-------|
| Questions evaluated | 250 / 250 |
| Errors | 0 |
| Skipped | 0 |
| Recall@1 | 0.3400 |
| Recall@5 | 0.5120 |
| Recall@10 | 0.5880 |
| MRR | 0.4172 |
| Latency p50 (ms) | 61.78 |
| Latency p95 (ms) | 83.06 |

## By question_type

| question_type | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| 1 | 36 | 0.1111 | 0.1667 | 0.3056 | 0.1589 |
| 2 | 40 | 0.3750 | 0.5500 | 0.6750 | 0.4533 |
| 3 | 12 | 0.0000 | 0.1667 | 0.2500 | 0.0778 |
| 4 | 106 | 0.3774 | 0.6038 | 0.6415 | 0.4682 |
| 5 | 56 | 0.4643 | 0.6071 | 0.6786 | 0.5338 |

## Leaderboard-parity slice

Categories excluded: 5 (LoCoMo cat-5 'adversarial' — see docs/benchmarks.md for the evidence audit and caveat). Re-aggregated from the per-category breakdown; comparable to the no-adversarial leaderboard variant.

| Slice | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| Full (hybrid) | 250 | 0.3400 | 0.5120 | 0.5880 | 0.4172 |
| Parity (hybrid) | 194 | 0.3041 | 0.4846 | 0.5619 | 0.3836 |

## Notes

- Retrieval mode: hybrid — ContextAssembler.assemble() is the primary path; effective mode resolves to 'hybrid'.
- Hybrid fuses BM25 (lexical) + vector (all-MiniLM-L6-v2, 384-dim, in-process numpy cosine) via Reciprocal Rank Fusion (k=60).
- Ranking unit: turn — Every retrieved record is collapsed to its turn ID before scoring — gold and non-gold alike. The unit is fixed by the dataset's ground-truth granularity and resolved before retrieval, so the answer key affects only the final metric (issue #514).
- LoCoMo: image-only turns skipped (text-only evaluation).

## Reference Numbers

agentmemory BM25+Vector (all-MiniLM-L6-v2) on LongMemEval-S:
- Recall@5: 95.2%, Recall@10: 98.6%, MRR: 88.2%

Popoto BM25-only baseline on LongMemEval-S (any-hit, #438):
- Recall@5: 95.2%, Recall@10: 97.8%

This run used **hybrid** retrieval (BM25 + all-MiniLM-L6-v2 vector fused via RRF, k=60). Compare Recall@5/Recall@10 above against the BM25-only baseline and the agentmemory hybrid reference.
