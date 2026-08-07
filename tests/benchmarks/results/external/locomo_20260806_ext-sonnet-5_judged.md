# Popoto External Benchmark: locomo

**Run date:** 2026-08-06  
**Retrieval mode:** lexical  
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
| Recall@1 | 0.2600 |
| Recall@5 | 0.5300 |
| Recall@10 | 0.6000 |
| MRR | 0.3711 |
| Latency p50 (ms) | 5.12 |
| Latency p95 (ms) | 5.57 |

## By question_type

| question_type | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| 1 | 14 | 0.0714 | 0.2857 | 0.3571 | 0.1494 |
| 2 | 21 | 0.3810 | 0.7619 | 0.8571 | 0.5489 |
| 3 | 4 | 0.0000 | 0.7500 | 0.7500 | 0.2375 |
| 4 | 38 | 0.2632 | 0.4737 | 0.5263 | 0.3480 |
| 5 | 23 | 0.3043 | 0.5217 | 0.6087 | 0.4052 |

## Leaderboard-parity slice

Categories excluded: 5 (LoCoMo cat-5 'adversarial' — see docs/benchmarks.md for the evidence audit and caveat). Re-aggregated from the per-category breakdown; comparable to the no-adversarial leaderboard variant.

| Slice | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| Full (lexical) | 100 | 0.2600 | 0.5300 | 0.6000 | 0.3711 |
| Parity (lexical) | 77 | 0.2468 | 0.5325 | 0.5974 | 0.3609 |

## Judged Answer Accuracy (end-to-end)

> These are a **different metric family** from the retrieval recall above (retrieve→generate→LLM-judge). Retrieval recall and judged accuracy are reported side by side but MUST NOT be cross-compared or combined into a single ranking (#453).

**Judge:** `gpt-4o-mini` · **Generator:** `gpt-4o-mini` · **Protocol:** mem0/gam (arXiv:2504.19413) · **temperature:** 0  
**Judge prompt SHA-256:** `cb44a52ee9ba372f…`  

| Metric | Value |
|--------|-------|
| Judged accuracy | 0.1948 |
| Scored (CORRECT+WRONG) | 77 |
| Correct | 15 |
| Judge errors | 0 |
| Skipped (retrieval not ok) | 0 |
| Adversarial excluded | 23 |

### Judged accuracy by question_type

| question_type | n | Correct | Judged accuracy |
|---|---|---|---|
| 1 | 14 | 6 | 0.4286 |
| 2 | 21 | 2 | 0.0952 |
| 3 | 4 | 0 | 0.0000 |
| 4 | 38 | 7 | 0.1842 |

_Adversarial (cat-5): 23 items, excluded from the headline number. Adversarial (cat-5) items test refusal; the factual-match Mem0/GAM judge cannot score them meaningfully. Reported for transparency only, EXCLUDED from judged_accuracy. Refusal metric tracked in #463._

## Notes

- Retrieval mode: lexical — ContextAssembler.assemble() is the primary path; effective mode resolves to 'lexical'.
- Lexical uses BM25 (query-sensitive) only; no vector/embedding signal is fused in this mode.
- LoCoMo: image-only turns skipped (text-only evaluation).

## Reference Numbers

agentmemory BM25+Vector (all-MiniLM-L6-v2) on LongMemEval-S:
- Recall@5: 95.2%, Recall@10: 98.6%, MRR: 88.2%

Popoto BM25-only baseline on LongMemEval-S (any-hit, #438):
- Recall@5: 95.2%, Recall@10: 97.8%

This run used **lexical** retrieval (BM25 only). Re-run with `--retrieval-mode hybrid` to fuse the all-MiniLM-L6-v2 vector signal (RRF, k=60) and compare against the agentmemory reference.
