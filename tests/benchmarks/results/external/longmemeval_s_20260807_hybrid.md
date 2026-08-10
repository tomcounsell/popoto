# Popoto External Benchmark: longmemeval-s

**Run date:** 2026-08-07  
**Retrieval mode:** hybrid  
**Ranking unit:** session (gold-blind, first occurrence wins)  
**Python:** 3.12.13  
**Platform:** macOS-26.5.2-arm64-arm-64bit  
**Sample mode:** stride  
**Seed:** 0  
**Limit:** all  

## Summary

| Metric | Value |
|--------|-------|
| Questions evaluated | 500 / 500 |
| Errors | 0 |
| Skipped | 0 |
| Recall@1 | 0.8920 |
| Recall@5 | 0.9860 |
| Recall@10 | 0.9920 |
| MRR | 0.9307 |
| Latency p50 (ms) | 57.01 |
| Latency p95 (ms) | 71.91 |

## By question_type

| question_type | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| knowledge-update | 78 | 0.9359 | 0.9872 | 1.0000 | 0.9634 |
| multi-session | 133 | 0.9023 | 0.9925 | 0.9925 | 0.9373 |
| single-session-assistant | 56 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| single-session-preference | 30 | 0.7000 | 0.9667 | 0.9667 | 0.8111 |
| single-session-user | 70 | 0.9000 | 0.9857 | 1.0000 | 0.9326 |
| temporal-reasoning | 133 | 0.8496 | 0.9774 | 0.9850 | 0.9019 |

## Notes

- Retrieval mode: hybrid — ContextAssembler.assemble() is the primary path; effective mode resolves to 'hybrid'.
- Hybrid fuses BM25 (lexical) + vector (all-MiniLM-L6-v2, 384-dim, in-process numpy cosine) via Reciprocal Rank Fusion (k=60).
- Ranking unit: session — Every retrieved record is collapsed to its session ID before scoring — gold and non-gold alike. The unit is fixed by the dataset's ground-truth granularity and resolved before retrieval, so the answer key affects only the final metric (issue #514).
- LoCoMo: image-only turns skipped (text-only evaluation).

## Reference Numbers

agentmemory BM25+Vector (all-MiniLM-L6-v2) on LongMemEval-S:
- Recall@5: 95.2%, Recall@10: 98.6%, MRR: 88.2%

Popoto BM25-only baseline on LongMemEval-S (any-hit, #438):
- Recall@5: 95.2%, Recall@10: 97.8%

This run used **hybrid** retrieval (BM25 + all-MiniLM-L6-v2 vector fused via RRF, k=60). Compare Recall@5/Recall@10 above against the BM25-only baseline and the agentmemory hybrid reference.
