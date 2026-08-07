# Popoto External Benchmark: longmemeval-s

**Run date:** 2026-08-07  
**Retrieval mode:** lexical  
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
| Recall@1 | 0.8560 |
| Recall@5 | 0.9520 |
| Recall@10 | 0.9780 |
| MRR | 0.8987 |
| Latency p50 (ms) | 11.07 |
| Latency p95 (ms) | 16.84 |

## By question_type

| question_type | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| knowledge-update | 78 | 0.9487 | 0.9872 | 0.9872 | 0.9679 |
| multi-session | 133 | 0.8271 | 0.9549 | 0.9774 | 0.8882 |
| single-session-assistant | 56 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| single-session-preference | 30 | 0.4000 | 0.7000 | 0.9333 | 0.5443 |
| single-session-user | 70 | 0.9286 | 0.9857 | 1.0000 | 0.9521 |
| temporal-reasoning | 133 | 0.8346 | 0.9474 | 0.9624 | 0.8778 |

## Notes

- Retrieval mode: lexical — ContextAssembler.assemble() is the primary path; effective mode resolves to 'lexical'.
- Lexical uses BM25 (query-sensitive) only; no vector/embedding signal is fused in this mode.
- Ranking unit: session — Every retrieved record is collapsed to its session ID before scoring — gold and non-gold alike. The unit is fixed by the dataset's ground-truth granularity and resolved before retrieval, so the answer key affects only the final metric (issue #514).
- LoCoMo: image-only turns skipped (text-only evaluation).

## Reference Numbers

agentmemory BM25+Vector (all-MiniLM-L6-v2) on LongMemEval-S:
- Recall@5: 95.2%, Recall@10: 98.6%, MRR: 88.2%

Popoto BM25-only baseline on LongMemEval-S (any-hit, #438):
- Recall@5: 95.2%, Recall@10: 97.8%

This run used **lexical** retrieval (BM25 only). Re-run with `--retrieval-mode hybrid` to fuse the all-MiniLM-L6-v2 vector signal (RRF, k=60) and compare against the agentmemory reference.
