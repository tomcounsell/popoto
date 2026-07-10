# Popoto External Benchmark: locomo

**Run date:** 2026-07-08  
**Retrieval mode:** hybrid  
**Python:** 3.12.13  
**Platform:** macOS-26.3.1-arm64-arm-64bit  
**Sample mode:** stride  
**Seed:** 0  
**Limit:** all  

## Summary

| Metric | Value |
|--------|-------|
| Questions evaluated | 1986 / 1986 |
| Errors | 0 |
| Skipped | 0 |
| Recall@1 | 0.1667 |
| Recall@5 | 0.4235 |
| Recall@10 | 0.5403 |
| MRR | 0.2835 |
| Latency p50 (ms) | 63.21 |
| Latency p95 (ms) | 110.29 |

## By question_type

| question_type | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| 1 | 282 | 0.0674 | 0.3014 | 0.4220 | 0.1707 |
| 2 | 321 | 0.1589 | 0.4268 | 0.5389 | 0.2851 |
| 3 | 96 | 0.0729 | 0.1771 | 0.2708 | 0.1247 |
| 4 | 841 | 0.1926 | 0.4602 | 0.5707 | 0.3116 |
| 5 | 446 | 0.2063 | 0.4821 | 0.6166 | 0.3349 |

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
