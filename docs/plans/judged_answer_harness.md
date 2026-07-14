---
status: Planning
type: feature
appetite: Medium
owner: valorengels
created: 2026-07-14
tracking: https://github.com/tomcounsell/popoto/issues/458
last_comment_id:
revision_applied: true
---

# End-to-end Judged-Answer Harness (Tier 5): Mem0/GAM protocol, pinned gpt-4o-mini judge

## Problem

**Current behavior:** The external benchmark harness (`tests/benchmarks/run_external.py`,
#394) stops at **retrieval**. It reports Recall@1/5/10 + MRR + latency for LongMemEval-S and
LoCoMo, but never generates an answer and never judges answer accuracy. Every published
vendor leaderboard (Hindsight, Mem0, Zep, Memori, Backboard) reports **end-to-end judged
accuracy** (retrieve → generate → LLM-judge). Popoto's retrieval-recall numbers sit next to
those judged-accuracy numbers in the literature, which is the core apples-to-oranges error
(strategy §1.1).

**Desired outcome** (issue #458, strategy §1.2 "Tier 5"): add the generation + LLM-judge
stage the harness deliberately stops before, so Popoto is comparable to the leaderboards, and
do it honestly:

1. **Adopt the Mem0 / GAM evaluation protocol verbatim** (arXiv:2504.19413) with `gpt-4o-mini`
   as judge — the most widely reused protocol (MemPro et al. follow it). Do NOT invent a judge
   prompt; reuse is what makes Popoto's numbers line up with the published tables.
2. **Pin the judge model + prompt in-repo.** Judged accuracy drifts several points across judge
   models (Hindsight reports different overall scores under Gemini-3 vs OSS-120B for the *same*
   memory stack). The judge identity (model id + prompt hash) is recorded in the JSON artifact.
3. **Report retrieval recall AND judged accuracy side by side** per run — a differentiating
   honesty artifact (vendors publish only the flattering one). The two metric families are
   reported in **separate blocks** and are **never fused into a single ranking** (#453 framing).
4. Artifacts follow the existing convention: `{dataset}_{date}_judged.{json,md}` +
   `{dataset}_latest_judged.*` pointers. `--limit` / `--sample` / `--seed` reuse the existing
   CLI surface.
5. Generation + judge calls require an API key → the harness **skips gracefully** without one
   (the same posture as hybrid's model download), and a cost estimate is documented before full
   runs.

## Freshness Check

**Baseline commit:** `0b4c629` (origin/main, "Merge pull request #470 from …/release/v1.8.0").
**Issue filed:** part of epic #456 (Track A), strategy §1.2.
**Disposition:** Unchanged — the retrieval harness this extends is present and stable.

**File:line references re-verified (worktree of origin/main):**
- `tests/benchmarks/run_external.py` (838 lines) — CLI entrypoint; `run_item`,
  `compute_aggregate`, `build_markdown_report`, `save_reports`, `main`. Present as described.
- `tests/benchmarks/scenarios/external_base.py` (502 lines) — `ExternalScenario`; `run()`
  drives `ContextAssembler.assemble()` and returns a `ScenarioResult`. The retrieved **records**
  are available in `run()` (`assembly_result.records`) but their text content is **not** currently
  surfaced in `ScenarioResult.metadata` — this plan adds `retrieved_contents` there so the
  generator has evidence to answer from.
- `tests/benchmarks/datasets/__init__.py` — `BenchmarkItem` namedtuple; gold answer is in
  `item.metadata["answer"]` for **both** adapters (LoCoMo `qa["answer"]`/`adversarial_answer`,
  LongMemEval `record["answer"]`). Confirmed present.
- `pyproject.toml` — an `openai = ["openai>=1.0.0", "numpy>=1.23.1"]` optional extra already
  exists; the judged harness reuses it (no new hard dependency).

**Overlapping active plans:** `external_benchmark_harness.md` (#394, the retrieval base this
builds on), `deterministic_eval_harness.md` (#418, the deterministic no-LLM CI eval — orthogonal;
that one deliberately has *no* LLM judge). No plan currently targets the generation+judge stage.

## Prior Art

- **#394 / external harness** — the retrieval half this extends. Same artifact naming
  convention (`{dataset}_{date}[_suffix].{json,md}` + `_latest[_suffix]` pointers), same
  `--limit`/`--sample`/`--seed` CLI surface, same DB-14 isolation (`_select_bench_db`).
- **#442 / hybrid retrieval** — the model here is the graceful-skip precedent: hybrid needs a
  ~90 MB model download and skips cleanly without it. Judged mode needs an API key and skips the
  same way.
- **Mem0 / GAM** (arXiv:2504.19413) — the LLM-as-a-Judge accuracy protocol we reproduce
  verbatim. MemPro (arXiv:2606.00619) reuses the same `gpt-4o-mini` judge, which is exactly why
  reusing it makes numbers line up.

## Design

Two new pieces plus one small hook, all under `tests/benchmarks/`:

### 1. `tests/benchmarks/judge.py` — pinned judge + generation protocol

Pure, dependency-injectable module. **No `openai` import at module top** (it is an optional
extra and must not break test collection); the OpenAI client is imported lazily inside the
factory.

- `JUDGE_MODEL = "gpt-4o-mini"` — pinned. A test asserts this exact string so a silent model
  swap is a test failure (issue requirement 1/2).
- `GENERATION_MODEL = "gpt-4o-mini"` — the answer generator is **also pinned** (crit C3). #458
  requires pinning the judge; making the generator configurable is scope nobody asked for and
  would silently invalidate the committed cost figures. Both calls use the same pinned model, so
  the cost estimate stays correct.
- Both `generate_answer` and `judge_answer` call the model with **`temperature=0`** (crit C2) so
  judged_accuracy is reproducible run-to-run; the temperature is recorded in the `judge`
  identity block alongside the prompt hash.
- `LLM_JUDGE_PROMPT` — the Mem0/GAM `ACCURACY_PROMPT`, reproduced **verbatim** with a source
  citation (arXiv:2504.19413 + the mem0 evaluation repo). `{question}`/`{gold_answer}`/
  `{generated_answer}` placeholders.
- `ANSWER_GENERATION_PROMPT` — the answer-from-memories prompt (context = top-k retrieved turn
  texts; instructs a concise answer, and to say it cannot find the info when the memories don't
  contain it — needed so adversarial/unanswerable items aren't force-answered).
- `prompt_sha256(text) -> str` — stable hash used in the artifact to fingerprint the exact
  prompt text (so a future prompt edit is visible in the committed JSON).
- `JudgeProtocol` — a tiny structural interface (`chat(model, messages) -> str`) so tests
  inject a fake and **no test needs `openai` or an API key**.
- `build_openai_client()` — lazy `import openai`; reads `OPENAI_API_KEY`; returns a
  `JudgeProtocol` adapter.
- `is_judge_available() -> bool` — True iff `openai` importable AND `OPENAI_API_KEY` set.
  Mirrors hybrid's capability check.
- `generate_answer(client, question, context_texts) -> str` — `temperature=0`, pinned
  `GENERATION_MODEL`.
- `judge_answer(client, question, gold_answer, generated_answer) -> tuple[str, str]` — returns
  `(label, raw)` where `label ∈ {"CORRECT", "WRONG"}`. `temperature=0`, pinned `JUDGE_MODEL`.
  Parses the Mem0 protocol's JSON `{"label": ...}` first, falls back to a strict CORRECT/WRONG
  scan, and normalizes ambiguity to `"WRONG"` (the protocol's conservative default).
- `call_with_retry(fn, attempts=3)` — retry-with-backoff wrapper used around both LLM calls so a
  transient `RateLimitError`/`APIError`/timeout retries rather than aborting a paid run (crit B3).
  Raises the last error after `attempts`, which the caller catches per-item.
- `estimate_cost(n_items, ...) -> dict` — documented token/price assumptions → an estimated USD
  range, printed before a non-dry full run and reproduced in the plan/docs.

### 2. `--judged` stage wired into `run_external.py`

Reuse the existing CLI (`--dataset`, `--limit`, `--sample`, `--seed`, `--retrieval-mode`,
`--dry-run`, `--fixture`, `--output`). Add exactly one new flag:

- `--judged` (flag) — turn on the generation+judge stage on top of retrieval. The generator and
  judge are both pinned to `gpt-4o-mini` (no `--generation-model` flag — crit C3).

**Retrieval-mode support (crit B2):** `--judged` is supported only for the assembler paths
(`lexical`, `hybrid`), whose retrieved records already carry `.content`
(`external_base.py:388`). `--retrieval-mode vector --judged` is **rejected up front** with a
clear error (the vector path holds only `(redis_key, cosine)` pairs, no hydrated content;
hydration is deferred to a follow-up if real demand appears). This guard lives in `main()`
before any ingestion.

Flow when `--judged` is set:

1. **Graceful skip:** if `not is_judge_available()`, print the reason + the cost estimate and
   return 0 with a clear "skipped, no API key" message (matching hybrid's posture — a missing key
   is not a failure). No artifacts written.
2. For each item, after retrieval, **gate on `q_result.status == "ok"`** (crit C1) — non-ok /
   `skipped-empty` items have zero or no metadata, so they are counted as `judged_skipped` and
   never reach an API call. For ok items, feed the **retrieved** turn texts (new
   `metadata["retrieved_contents"]`, top-k) to `generate_answer`, then `judge_answer` vs
   `item.metadata["answer"]`.
3. **Per-item fault isolation (crit B3):** each item's generate+judge is wrapped in try/except.
   Transient errors retry via `call_with_retry`; a final failure records a `judge_status =
   "judge_error"` (distinct from CORRECT/WRONG) and the run **continues** rather than crashing —
   so a single 429/5xx never forces a full re-pay. Judge errors are tracked separately and do
   **not** count against the existing retrieval `--error-threshold` gate
   (`run_external.py:824`); a distinct `--judge-error-threshold` (default 0.25) governs them.
   All per-item results (incl. `generated_answer`, `judge_label`, `judge_status`) are written to
   the artifact at the end, so a completed run is fully inspectable.
4. Aggregate adds a `judged` block: `n_judged`, `n_correct`, `n_judge_errors`, `n_judged_skipped`,
   `judged_accuracy` (denominator = CORRECT+WRONG only, excluding errors and skips), and a
   per-`question_type` judged breakdown — kept **separate** from the retrieval `summary` block.
   **Adversarial items (LoCoMo cat-5) are excluded from the headline `judged_accuracy`** (crit
   C4): the verbatim Mem0/GAM prompt is a factual-match judge, not a refusal judge, so refusal
   answers judged against `adversarial_answer` have no defined correctness semantics. They are
   reported under a separate `judged_adversarial` key (count + labels), never folded into the
   headline number; the refusal *metric* remains #454.
5. A top-level `judge` identity block: `{judge_model, judge_prompt_sha256, generation_model,
   generation_prompt_sha256, temperature: 0, protocol: "mem0/gam",
   protocol_ref: "arXiv:2504.19413"}`.
6. Artifacts saved with a composed suffix (crit B1). `save_reports()` gains a `judged: bool`
   param and composes **both** dimensions:
   `suffix = ("" if mode=="lexical" else f"_{mode}") + ("_judged" if judged else "")`. So
   `lexical`+`judged` → `{slug}_{date}_judged.*`; `hybrid`+`judged` →
   `{slug}_{date}_hybrid_judged.*` (+ matching `_latest*` pointers) — never clobbering the plain
   `_hybrid` retrieval artifact or the lexical-judged artifact. The Markdown renders retrieval
   recall and judged accuracy in **two separate tables** under an explicit "these two metric
   families are not comparable" caveat (#453).

### 3. `ExternalScenario` hook

`run()` collects the retrieved records' `.content` in rank order into
`metadata["retrieved_contents"]` on the **assembler path only** (from `assembly_result.records`
at `external_base.py:388`). The vector path is not extended (it holds only `(redis_key, cosine)`
pairs and `--judged` rejects vector mode up front — crit B2). Zero behavior change to existing
retrieval scoring; purely additive metadata that the judged stage consumes.

## Testing

New `tests/benchmarks/test_judged.py`, all runnable with **no `openai` and no API key** via the
injected `JudgeProtocol` fake:

- **Model pin:** `JUDGE_MODEL == "gpt-4o-mini"` exactly (guards silent swap).
- **Prompt pin:** `prompt_sha256(LLM_JUDGE_PROMPT)` equals a committed constant + the verbatim
  text contains the protocol's signature phrasing (guards silent prompt drift).
- **Judge parsing:** JSON `{"label":"CORRECT"}`, bare `CORRECT`/`WRONG`, mixed/ambiguous →
  `WRONG`, case-insensitivity.
- **Determinism:** `generate_answer`/`judge_answer` pass `temperature=0` and the pinned models to
  the fake client (assert the call kwargs).
- **Generation:** fake client returns a canned answer; `generate_answer` passes the pinned model
  and includes the context.
- **Gating:** `is_judge_available()` False when key unset / openai absent (monkeypatched).
- **Retry / fault isolation:** a fake client that raises twice then succeeds → `call_with_retry`
  recovers; a fake that always raises → item recorded as `judge_error`, run does not crash, other
  items still judged.
- **Suffix composition (crit B1):** `save_reports(..., retrieval_mode="hybrid", judged=True)`
  writes `..._hybrid_judged.json`, not `..._judged.json`; lexical+judged writes `..._judged.json`.
- **Aggregate shape:** judged aggregate has a `judged` block AND a retrieval `summary` block,
  the top-level `judge` identity block is populated (incl. `temperature`), and the two families
  are **not** merged into one ranking (assert both blocks exist and are distinct).
- **Status gate (crit C1):** a non-ok / skipped-empty item is counted as `judged_skipped` and
  triggers no generation call (fake client call-count asserted).
- **Adversarial exclusion (crit C4):** an adversarial (cat-5) item does not move the headline
  `judged_accuracy` and is reported under `judged_adversarial`.
- **Vector rejection (crit B2):** `main(--retrieval-mode vector --judged)` exits with a clear
  error and writes no artifacts.
- **Graceful skip end-to-end:** `main(--judged)` with `is_judge_available()` monkeypatched
  False returns 0 cleanly and writes no artifacts.
- **End-to-end (mocked):** small fixture, injected fake judge+generator → judged accuracy
  computed and `_judged` artifacts written to a `tmp_path --output`.

Gate: `scripts/ci-local.sh` (tests + stress + docs). Only the benchmark judged tests are new;
they add no external dependency to the default suite.

## Cost estimate (documented before full runs)

Per judged item = 1 generation call + 1 judge call, both `gpt-4o-mini`
($0.15 / 1M input, $0.60 / 1M output as of 2026-07). With ~1.5k input + ~150 output tokens per
call, ~2 calls/item ≈ **$0.0007/item**. Full LoCoMo (1,986 QA) ≈ **~$1.4**; a `--limit 200`
representative slice ≈ **$0.15**. `estimate_cost()` prints this before a non-dry run; numbers are
assumption-based (documented), never presented as measured Popoto results.

## Out of Scope / Non-goals

- **Adversarial (LoCoMo cat-5) refusal metric** — adversarial items are *excluded* from the
  headline judged-accuracy and reported separately (crit C4); a dedicated refusal-precision
  metric is #454 (strategy §1.3), not this issue.
- **Cross-comparing recall vs judged accuracy** — explicitly forbidden (#453); the report keeps
  them in separate blocks with a caveat.
- **No committed live-judge numbers** — this PR ships the *harness*, not a self-benchmarked
  scoreboard (benchmark doctrine: no fabricated self-benchmark numbers). Live runs are operator-run
  with a key.
- **Judge AND generator model configurability** — both are pinned to `gpt-4o-mini`; no override
  flag (crit C3). Reopen in a follow-up if a real need appears.
- **Vector-mode judged** — rejected up front (crit B2); vector results carry no `.content` to
  answer from. Deferred to a follow-up if demand appears.
- **Checkpoint/resume of a killed run** — per-item fault isolation means a completed run never
  crashes and always writes its artifact; mid-run *process kill* resume (JSONL replay) is a
  follow-up, not this appetite. Documented so operators re-run from scratch if the process dies.
