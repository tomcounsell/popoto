# Popoto External Benchmark: locomo

**Run date:** 2026-08-06  
**Retrieval mode:** graph  
**Python:** 3.12.13  
**Platform:** macOS-26.5.2-arm64-arm-64bit  
**Sample mode:** stride  
**Seed:** 0  
**Limit:** 200  

## Summary

| Metric | Value |
|--------|-------|
| Questions evaluated | 200 / 200 |
| Errors | 0 |
| Skipped | 0 |
| Recall@1 | 0.0800 |
| Recall@5 | 0.7500 |
| Recall@10 | 0.8600 |
| MRR | 0.3094 |
| Latency p50 (ms) | 19.55 |
| Latency p95 (ms) | 22.98 |

## By question_type

| question_type | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| 4 | 200 | 0.0800 | 0.7500 | 0.8600 | 0.3094 |

## Notes

- Retrieval mode: graph — ContextAssembler.assemble() with the #462/#483 graph-traversal arm enabled (graph_traversal_relationship_fields=['prev_turn']).
- Same BM25 lexical arm as the 'lexical' baseline, plus a graph arm fused via RRF: CoOccurrenceField.propagate() + graph_traversal.expand_relationships() over a self-referential Relationship, seeded from the BM25 top-5, confidence/decay-modulated hop admission.
- EDGE CAVEAT: LoCoMo/LongMemEval ship no annotated entity graph, so the harness builds CONVERSATIONAL-ADJACENCY edges at ingest (turn i <-> turn i-1 within a session). This measures the traversal mechanism over adjacency edges, NOT a semantic association graph.
- LoCoMo: image-only turns skipped (text-only evaluation).

## Reference Numbers

agentmemory BM25+Vector (all-MiniLM-L6-v2) on LongMemEval-S:
- Recall@5: 95.2%, Recall@10: 98.6%, MRR: 88.2%

Popoto BM25-only baseline on LongMemEval-S (any-hit, #438):
- Recall@5: 95.2%, Recall@10: 97.8%

This run used **lexical** retrieval (BM25 only). Re-run with `--retrieval-mode hybrid` to fuse the all-MiniLM-L6-v2 vector signal (RRF, k=60) and compare against the agentmemory reference.
