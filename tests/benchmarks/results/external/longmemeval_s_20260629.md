# Popoto External Benchmark: longmemeval-s

**Run date:** 2026-06-29  
**Python:** 3.12.13  
**Platform:** macOS-26.3.1-arm64-arm-64bit  

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
| Latency p50 (ms) | 0.57 |
| Latency p95 (ms) | 1.46 |

## Notes

- Baseline run: relevance-only scoring (DecayingSortedField).
- No vector/embedding retrieval wired in (BM25-style baseline).
- LoCoMo: image-only turns skipped (text-only evaluation).

## Reference Numbers

agentmemory BM25+Vector (all-MiniLM-L6-v2) on LongMemEval-S:
- Recall@5: 95.2%, Recall@10: 98.6%, MRR: 88.2%

Popoto baseline is score-only (no embedding retrieval). Issue #395 will add hybrid BM25+vector retrieval to close this gap.
