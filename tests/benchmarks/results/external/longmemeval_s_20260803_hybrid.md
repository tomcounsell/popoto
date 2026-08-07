# Popoto External Benchmark: longmemeval-s

**Run date:** 2026-08-03  
**Retrieval mode:** hybrid  
**Python:** 3.12.13  
**Platform:** macOS-26.5.2-arm64-arm-64bit  
**Sample mode:** stratified  
**Seed:** 0  
**Limit:** 100  

## Summary

| Metric | Value |
|--------|-------|
| Questions evaluated | 100 / 100 |
| Errors | 0 |
| Skipped | 0 |
| Recall@1 | 0.9200 |
| Recall@5 | 0.9800 |
| Recall@10 | 0.9900 |
| MRR | 0.9425 |
| Latency p50 (ms) | 57.17 |
| Latency p95 (ms) | 68.27 |

## By question_type

| question_type | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| knowledge-update | 15 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| multi-session | 27 | 0.9630 | 1.0000 | 1.0000 | 0.9815 |
| single-session-assistant | 11 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| single-session-preference | 6 | 0.6667 | 0.8333 | 0.8333 | 0.7083 |
| single-session-user | 14 | 0.9286 | 0.9286 | 1.0000 | 0.9405 |
| temporal-reasoning | 27 | 0.8519 | 1.0000 | 1.0000 | 0.9012 |

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
