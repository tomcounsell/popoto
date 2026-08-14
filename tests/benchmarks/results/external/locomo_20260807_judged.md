# Popoto External Benchmark: locomo

**Run date:** 2026-08-07  
**Retrieval mode:** lexical  
**Ranking unit:** turn (gold-blind, first occurrence wins)  
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
| Recall@1 | 0.2900 |
| Recall@5 | 0.4700 |
| Recall@10 | 0.5600 |
| MRR | 0.3784 |
| Latency p50 (ms) | 12.28 |
| Latency p95 (ms) | 22.1 |

## By question_type

| question_type | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| 1 | 14 | 0.0714 | 0.0714 | 0.2143 | 0.1025 |
| 2 | 21 | 0.3333 | 0.6667 | 0.7143 | 0.4933 |
| 3 | 4 | 0.2500 | 0.2500 | 0.5000 | 0.2812 |
| 4 | 38 | 0.3947 | 0.5000 | 0.6053 | 0.4440 |
| 5 | 23 | 0.2174 | 0.5217 | 0.5652 | 0.3499 |

## Leaderboard-parity slice

Categories excluded: 5 (LoCoMo cat-5 'adversarial' — see docs/benchmarks.md for the evidence audit and caveat). Re-aggregated from the per-category breakdown; comparable to the no-adversarial leaderboard variant.

| Slice | n | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| Full (lexical) | 100 | 0.2900 | 0.4700 | 0.5600 | 0.3784 |
| Parity (lexical) | 77 | 0.3117 | 0.4545 | 0.5585 | 0.3869 |

## Judged Answer Accuracy (end-to-end)

> These are a **different metric family** from the retrieval recall above (retrieve→generate→LLM-judge). Retrieval recall and judged accuracy are reported side by side but MUST NOT be cross-compared or combined into a single ranking (#453).

**Judge:** `gpt-4o-mini` · **Generator:** `gpt-4o-mini` · **Protocol:** mem0/gam (arXiv:2504.19413) · **temperature:** 0  
**Judge prompt SHA-256:** `cb44a52ee9ba372f…`  

| Metric | Value |
|--------|-------|
| Judged accuracy | 0.3636 |
| Scored (CORRECT+WRONG) | 77 |
| Correct | 28 |
| Judge errors | 0 |
| Skipped (retrieval not ok) | 0 |
| Adversarial excluded | 23 |

### Judged accuracy by question_type

| question_type | n | Correct | Judged accuracy |
|---|---|---|---|
| 1 | 14 | 6 | 0.4286 |
| 2 | 21 | 3 | 0.1429 |
| 3 | 4 | 0 | 0.0000 |
| 4 | 38 | 19 | 0.5000 |

_Adversarial (cat-5): 23 items, excluded from the headline number. Adversarial (cat-5) items test refusal; the factual-match Mem0/GAM judge cannot score them meaningfully. Reported for transparency only, EXCLUDED from judged_accuracy. Refusal metric tracked in #463._

## Notes

- Retrieval mode: lexical — ContextAssembler.assemble() is the primary path; effective mode resolves to 'lexical'.
- Lexical uses BM25 (query-sensitive) only; no vector/embedding signal is fused in this mode.
- Ranking unit: turn — Every retrieved record is collapsed to its turn ID before scoring — gold and non-gold alike. The unit is fixed by the dataset's ground-truth granularity and resolved before retrieval, so the answer key affects only the final metric (issue #514).
- LoCoMo: image-only turns skipped (text-only evaluation).

## Reference Numbers

agentmemory BM25+Vector (all-MiniLM-L6-v2) on LongMemEval-S:
- Recall@5: 95.2%, Recall@10: 98.6%, MRR: 88.2%

Popoto BM25-only baseline on LongMemEval-S (any-hit, #438):
- Recall@5: 95.2%, Recall@10: 97.8%

This run used **lexical** retrieval (BM25 only). Re-run with `--retrieval-mode hybrid` to fuse the all-MiniLM-L6-v2 vector signal (RRF, k=60) and compare against the agentmemory reference.
