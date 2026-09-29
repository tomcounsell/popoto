# Popoto External Benchmark: locomo

**Run date:** 2026-09-18  
**Retrieval mode:** hybrid  
**Ranking unit:** turn (gold-blind, first occurrence wins)  
**Python:** 3.12.14  
**Platform:** macOS-26.6.2-arm64-arm-64bit  
**Sample mode:** stride  
**Seed:** 0  
**Limit:** all  

## Summary

| Metric | Value |
|--------|-------|
| Questions evaluated | 1986 / 1986 |
| Errors | 0 |
| Skipped | 0 |
| Recall@1 | 0.2966 |
| Recall@5 | 0.5292 |
| Recall@10 | 0.6037 |
| MRR | 0.3996 |
| Latency p50 (ms) | 63.21 |
| Latency p95 (ms) | 86.69 |

## By question_type

| question_type | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| 1 | 282 | 0.1312 | 0.2979 | 0.4255 | 0.2180 |
| 2 | 321 | 0.3458 | 0.6168 | 0.6729 | 0.4570 |
| 3 | 96 | 0.0938 | 0.2604 | 0.3438 | 0.1728 |
| 4 | 841 | 0.3365 | 0.5719 | 0.6409 | 0.4402 |
| 5 | 446 | 0.3341 | 0.5897 | 0.6525 | 0.4455 |

## Leaderboard-parity slice

Categories excluded: 5 (LoCoMo cat-5 'adversarial' — see docs/benchmarks.md for the evidence audit and caveat). Re-aggregated from the per-category breakdown; comparable to the no-adversarial leaderboard variant.

| Slice | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| Full (hybrid) | 1986 | 0.2966 | 0.5292 | 0.6037 | 0.3996 |
| Parity (hybrid) | 1540 | 0.2857 | 0.5117 | 0.5896 | 0.3863 |

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
