# Popoto External Benchmark: locomo

**Run date:** 2026-08-07  
**Retrieval mode:** hybrid  
**Ranking unit:** turn (gold-blind, first occurrence wins)  
**Python:** 3.12.13  
**Platform:** macOS-26.5.2-arm64-arm-64bit  
**Sample mode:** stride  
**Seed:** 0  
**Limit:** all  

## Summary

| Metric | Value |
|--------|-------|
| Questions evaluated | 282 / 282 |
| Errors | 0 |
| Skipped | 0 |
| Recall@1 | 0.1312 |
| Recall@5 | 0.2979 |
| Recall@10 | 0.4220 |
| MRR | 0.2182 |
| Latency p50 (ms) | 60.55 |
| Latency p95 (ms) | 77.03 |

## By question_type

| question_type | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| 1 | 282 | 0.1312 | 0.2979 | 0.4220 | 0.2182 |

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
