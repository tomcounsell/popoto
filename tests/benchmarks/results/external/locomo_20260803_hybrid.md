# Popoto External Benchmark: locomo

**Run date:** 2026-08-03  
**Retrieval mode:** hybrid  
**Python:** 3.12.13  
**Platform:** macOS-26.5.2-arm64-arm-64bit  
**Sample mode:** stratified  
**Seed:** 0  
**Limit:** 200  

## Summary

| Metric | Value |
|--------|-------|
| Questions evaluated | 200 / 200 |
| Errors | 0 |
| Skipped | 0 |
| Recall@1 | 0.1850 |
| Recall@5 | 0.4600 |
| Recall@10 | 0.5500 |
| MRR | 0.3062 |
| Latency p50 (ms) | 57.48 |
| Latency p95 (ms) | 66.12 |

## By question_type

| question_type | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| 1 | 28 | 0.0714 | 0.2857 | 0.3571 | 0.1394 |
| 2 | 32 | 0.0938 | 0.4688 | 0.5938 | 0.2487 |
| 3 | 10 | 0.0000 | 0.2000 | 0.3000 | 0.0894 |
| 4 | 85 | 0.2941 | 0.5529 | 0.6000 | 0.4098 |
| 5 | 45 | 0.1556 | 0.4444 | 0.6000 | 0.3032 |

## Leaderboard-parity slice

Categories excluded: 5 (LoCoMo cat-5 'adversarial' — see docs/benchmarks.md for the evidence audit and caveat). Re-aggregated from the per-category breakdown; comparable to the no-adversarial leaderboard variant.

| Slice | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| Full (hybrid) | 200 | 0.1850 | 0.4600 | 0.5500 | 0.3062 |
| Parity (hybrid) | 155 | 0.1935 | 0.4645 | 0.5355 | 0.3070 |

## Notes

- Retrieval mode: hybrid — ContextAssembler.assemble() is the primary path; effective mode resolves to 'hybrid'.
- Hybrid fuses BM25 (lexical) + vector (all-MiniLM-L6-v2, 384-dim, in-process numpy cosine) via Reciprocal Rank Fusion (k=60).
- LoCoMo: image-only turns skipped (text-only evaluation).

## Reference Numbers

agentmemory BM25+Vector (all-MiniLM-L6-v2) on LongMemEval-S:
- Recall@5: 95.2%, Recall@10: 98.6%, MRR: 88.2%

Popoto BM25-only baseline on LongMemEval-S (any-hit, #438):
- Recall@5: 95.2%, Recall@10: 97.8%

This run used **hybrid** retrieval (BM25 + all-MiniLM-L6-v2 vector fused via RRF, k=60). Compare Recall@5/Recall@10 above against the BM25-only baseline and the agentmemory hybrid reference.
