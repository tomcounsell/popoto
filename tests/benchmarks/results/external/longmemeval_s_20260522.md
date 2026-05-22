# Popoto External Benchmark: longmemeval-s

**Run date:** 2026-05-22  
**Python:** 3.12.13  
**Platform:** macOS-26.3.1-arm64-arm-64bit  

## Summary

| Metric | Value |
|--------|-------|
| Questions evaluated | 3 / 3 |
| Errors | 0 |
| Skipped | 0 |
| Recall@1 | 0.6667 |
| Recall@5 | 1.0000 |
| Recall@10 | 1.0000 |
| MRR | 0.8333 |
| Latency p50 (ms) | 1.79 |
| Latency p95 (ms) | 2.19 |

## Notes

- Issue #395 run: BM25 lexical retrieval via BM25Field (issue #395 hybrid path).
- Model includes `turn_id = AutoKeyField()` and `content_index = BM25Field(source="content")`.
- ContextAssembler constructed with `retrieval_mode="auto"` — resolves to composite since no EmbeddingField configured; BM25 used directly via `BM25Field.search()`.
- Image-only turns skipped (text-only evaluation).

## Delta vs. Baseline (issue #394, all-zeros)

| Metric | Baseline (v1.6.3) | Issue #395 (BM25) | Delta |
|--------|-------------------|-------------------|-------|
| Recall@1 | 0.0000 | 0.6667 | **+66.7pp** |
| Recall@5 | 0.0000 | 1.0000 | **+100pp** |
| Recall@10 | 0.0000 | 1.0000 | **+100pp** |
| MRR | 0.0000 | 0.8333 | **+83.3pp** |

## Reference Numbers

agentmemory BM25+Vector (all-MiniLM-L6-v2) on LongMemEval-S:
- Recall@5: 95.2%, Recall@10: 98.6%, MRR: 88.2%

Popoto issue #395 (BM25 lexical only, fixture of 3 questions). Full vector hybrid retrieval requires an EmbeddingField with a configured provider — see issue #395 for roadmap.
