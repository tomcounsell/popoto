# Popoto External Benchmark: longmemeval-s

**Run date:** 2026-06-30  
**Python:** 3.12.13  
**Platform:** macOS-26.3.1-arm64-arm-64bit  
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
| Latency p50 (ms) | 0.7 |
| Latency p95 (ms) | 2.37 |

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

- Baseline run: relevance-only scoring (DecayingSortedField).
- No vector/embedding retrieval wired in (BM25-style baseline).
- LoCoMo: image-only turns skipped (text-only evaluation).

## Reference Numbers

agentmemory BM25+Vector (all-MiniLM-L6-v2) on LongMemEval-S:
- Recall@5: 95.2%, Recall@10: 98.6%, MRR: 88.2%

Popoto baseline is score-only (no embedding retrieval). Issue #395 will add hybrid BM25+vector retrieval to close this gap.
