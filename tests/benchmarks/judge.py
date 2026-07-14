"""End-to-end judged-answer protocol (Tier 5) — Mem0/GAM, pinned gpt-4o-mini judge.

This module adds the *generation + LLM-judge* stage the retrieval harness
(``run_external.py``, #394) deliberately stops before, so Popoto's numbers are
comparable to the published judged-accuracy leaderboards (Hindsight, Mem0, Zep,
Memori, Backboard). Strategy: ``docs/plans/benchmarking_strategy_2026-07.md``
§1.2; plan: ``docs/plans/judged_answer_harness.md``; issue #458.

Design invariants (issue #458):

- **Judge model + prompt are PINNED in-repo.** ``JUDGE_MODEL == "gpt-4o-mini"``
  and ``LLM_JUDGE_PROMPT`` is the Mem0 / GAM ``ACCURACY_PROMPT`` reproduced
  verbatim (arXiv:2504.19413). Judged accuracy drifts several points across
  judge models, so the judge identity (model id + prompt SHA-256) is recorded in
  every artifact. Tests assert the model string and the prompt hash so a silent
  swap/edit is a test failure.
- **The generator is also pinned** to the same ``gpt-4o-mini`` so the documented
  cost figures stay valid; there is no model-override flag.
- **Both calls use ``temperature=0``** for run-to-run reproducibility.
- **No hard ``openai`` dependency.** ``openai`` is an optional extra; it is
  imported lazily inside :func:`build_openai_client`. Every unit test injects a
  :class:`JudgeProtocol` fake, so the default suite needs neither ``openai`` nor
  an API key. Live runs skip gracefully when the key/library is absent (the same
  posture as hybrid's model download).

Protocol provenance:
    The judge is the Mem0 LoCoMo ``ACCURACY_PROMPT`` (LLM-as-a-Judge), published
    with the Mem0 paper (arXiv:2504.19413) and reused verbatim by downstream
    harnesses (e.g. MemPro arXiv:2606.00619, MemTensor/MemOS). Reusing it is what
    makes Popoto's numbers line up with the published tables. The judge parses a
    trailing ``{"label": "CORRECT"|"WRONG"}`` JSON object exactly as the source
    harness does, and treats any ambiguous/unparseable response as ``WRONG`` (the
    conservative default).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from typing import List, Protocol, Tuple

# ---------------------------------------------------------------------------
# Pinned identity (issue #458 requirements 1 & 2)
# ---------------------------------------------------------------------------

#: Judge model — PINNED. A silent swap breaks the "numbers line up with the
#: published tables" contract, so a test asserts this exact string.
JUDGE_MODEL = "gpt-4o-mini"

#: Answer generator — also pinned to the judge model so the documented cost
#: figures (both calls priced at gpt-4o-mini) stay valid. No override flag.
GENERATION_MODEL = "gpt-4o-mini"

#: Sampling temperature for both the generation and judge calls. Pinned to 0 so
#: judged accuracy is reproducible run-to-run; recorded in the judge-identity
#: block of the artifact.
JUDGE_TEMPERATURE = 0

#: System prompt for the judge, verbatim from the Mem0/GAM LoCoMo grader.
JUDGE_SYSTEM_PROMPT = (
    "You are an expert grader that determines if answers to questions match a "
    "gold standard answer"
)

#: The Mem0 / GAM ``ACCURACY_PROMPT`` (LLM-as-a-Judge), reproduced VERBATIM from
#: the published protocol (arXiv:2504.19413; Mem0 & MemTensor/MemOS evaluation
#: harnesses). ``{question}``/``{gold_answer}``/``{generated_answer}`` are the
#: only substitutions. Do NOT edit this text — the committed SHA-256 pin
#: (asserted in test_judged.py) exists precisely to make any edit a loud failure,
#: because judged accuracy is not comparable across prompt variants.
LLM_JUDGE_PROMPT = """\
Your task is to label an answer to a question as 'CORRECT' or 'WRONG'. You will be given the following data:
    (1) a question (posed by one user to another user),
    (2) a 'gold' (ground truth) answer,
    (3) a generated answer
which you will score as CORRECT/WRONG.

The point of the question is to ask about something one user should know about the other user based on their prior conversations.
The gold answer will usually be a concise and short answer that includes the referenced topic, for example:
Question: Do you remember what I got the last time I went to Hawaii?
Gold answer: A shell necklace
The generated answer might be much longer, but you should be generous with your grading - as long as it touches on the same topic as the gold answer, it should be counted as CORRECT.

For time related questions, the gold answer will be a specific date, month, year, etc. The generated answer might be much longer or use relative time references (like "last Tuesday" or "next month"), but you should be generous with your grading - as long as it refers to the same date or time period as the gold answer, it should be counted as CORRECT. Even if the format differs (e.g., "May 7th" vs "7 May"), consider it CORRECT if it's the same date.

Now it's time for the real question:
Question: {question}
Gold answer: {gold_answer}
Generated answer: {generated_answer}

First, provide a short (one sentence) explanation of your reasoning, then finish with CORRECT or WRONG.
Do NOT include both CORRECT and WRONG in your response, or it will break the evaluation script.

Just return the label CORRECT or WRONG in a json format with the key as "label"."""

#: Answer-generation prompt. Not part of the pinned *judge* protocol, but pinned
#: here (and hashed into the artifact) for reproducibility. Instructs a grounded,
#: concise answer from the retrieved memories, and to admit when the answer is
#: not present — so unanswerable/adversarial items are not force-answered.
ANSWER_GENERATION_PROMPT = """\
You are a helpful assistant answering a question using ONLY the memories below, \
which are excerpts retrieved from a user's prior conversations.

Memories:
{context}

Question: {question}

Answer the question as concisely as possible using only the information in the \
memories. If the memories do not contain the information needed to answer, say \
you do not have that information. Do not invent facts that are not supported by \
the memories."""


# ---------------------------------------------------------------------------
# Prompt fingerprinting
# ---------------------------------------------------------------------------


def prompt_sha256(text: str) -> str:
    """Return the hex SHA-256 of ``text``.

    Used to fingerprint the exact judge / generation prompt in the committed
    artifact so any future edit is visible as a hash change.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Client protocol + OpenAI adapter (lazy import — no hard dependency)
# ---------------------------------------------------------------------------


class JudgeProtocol(Protocol):
    """Minimal chat interface the harness needs from an LLM client.

    Implemented by :class:`_OpenAIAdapter` for live runs and by a trivial fake in
    tests, so no test requires ``openai`` or an API key.
    """

    def chat(self, model: str, messages: List[dict], temperature: float) -> str:
        """Return the assistant message content for a chat completion."""
        ...


class _OpenAIAdapter:
    """Adapts an ``openai.OpenAI`` client to :class:`JudgeProtocol`."""

    def __init__(self, client) -> None:
        self._client = client

    def chat(self, model: str, messages: List[dict], temperature: float) -> str:
        resp = self._client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""


def is_judge_available() -> bool:
    """True iff ``openai`` is importable AND ``OPENAI_API_KEY`` is set.

    Mirrors hybrid's capability check: a missing key/library is a graceful skip,
    not a failure.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        return False
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


def build_openai_client() -> JudgeProtocol:
    """Construct a live OpenAI-backed :class:`JudgeProtocol`.

    Lazily imports ``openai`` (optional extra) and reads ``OPENAI_API_KEY`` from
    the environment. Call :func:`is_judge_available` first to skip gracefully.

    Raises:
        RuntimeError: If ``openai`` is not installed or no API key is set.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is not set — the judged harness needs an API key. "
            "Set it or run without --judged."
        )
    try:
        import openai
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise RuntimeError(
            "openai is required for --judged runs. Install it with: "
            "pip install -e '.[openai]'"
        ) from exc
    return _OpenAIAdapter(openai.OpenAI())


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------


def call_with_retry(
    fn, *, attempts: int = 3, base_delay: float = 1.0, sleep=time.sleep
):
    """Call ``fn()`` with exponential backoff on transient failures.

    A full LoCoMo judged run is ~4,000 serial API calls, so a transient
    429/5xx/timeout is near-certain; retrying rather than aborting avoids a full
    re-pay. Re-raises the last exception after ``attempts`` tries; the per-item
    caller catches it and records a ``judge_error`` so one bad item never crashes
    the run.

    Args:
        fn: Zero-arg callable performing the LLM call.
        attempts: Max attempts (default 3).
        base_delay: Base backoff seconds (doubled each retry).
        sleep: Injectable sleep (tests pass a no-op).
    """
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - transient API errors are broad
            last_exc = exc
            if i < attempts - 1:
                sleep(base_delay * (2**i))
    raise last_exc


# ---------------------------------------------------------------------------
# Generation + judging
# ---------------------------------------------------------------------------


def generate_answer(
    client: JudgeProtocol,
    question: str,
    context_texts: List[str],
) -> str:
    """Generate an answer to ``question`` grounded in ``context_texts``.

    Uses the pinned :data:`GENERATION_MODEL` at :data:`JUDGE_TEMPERATURE`.

    Args:
        client: A :class:`JudgeProtocol` (live or fake).
        question: The benchmark question.
        context_texts: Retrieved memory texts, in rank order (top-k evidence).

    Returns:
        The generated answer string (stripped).
    """
    context = "\n".join(f"- {t}" for t in context_texts) if context_texts else "(none)"
    prompt = ANSWER_GENERATION_PROMPT.format(context=context, question=question)
    content = client.chat(
        model=GENERATION_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=JUDGE_TEMPERATURE,
    )
    return (content or "").strip()


def _parse_label(text: str) -> str:
    """Parse a CORRECT/WRONG label from a judge response.

    Follows the Mem0/GAM parser: prefer a trailing ``{"label": "VALUE"}`` JSON
    object (single or double quoted), else fall back to a bare CORRECT/WRONG
    token. Any ambiguity (both tokens present, neither present, unparseable)
    normalizes to ``"WRONG"`` — the conservative default.

    Returns:
        Either ``"CORRECT"`` or ``"WRONG"``.
    """
    if not text:
        return "WRONG"

    # 1. Preferred: trailing {"label": "..."} JSON (matches the source harness's
    #    extract_label_json regex; supports single/double quotes).
    m = re.search(r'\{\s*"label"\s*:\s*["\']([^"\']*)["\']\s*\}', text)
    if m:
        label = m.group(1).strip().lower()
        if label == "correct":
            return "CORRECT"
        return "WRONG"

    # 2. Fallback: a bare, unambiguous CORRECT/WRONG token. If BOTH appear, the
    #    protocol says the response is broken → WRONG.
    upper = text.upper()
    has_correct = re.search(r"\bCORRECT\b", upper) is not None
    has_wrong = re.search(r"\bWRONG\b", upper) is not None
    if has_correct and not has_wrong:
        return "CORRECT"
    return "WRONG"


def judge_answer(
    client: JudgeProtocol,
    question: str,
    gold_answer: str,
    generated_answer: str,
) -> Tuple[str, str]:
    """Judge ``generated_answer`` against ``gold_answer`` via the Mem0/GAM prompt.

    Uses the pinned :data:`JUDGE_MODEL` + :data:`LLM_JUDGE_PROMPT` at
    :data:`JUDGE_TEMPERATURE`.

    Args:
        client: A :class:`JudgeProtocol` (live or fake).
        question: The benchmark question.
        gold_answer: The ground-truth answer.
        generated_answer: The model's generated answer.

    Returns:
        ``(label, raw)`` where ``label`` is ``"CORRECT"`` or ``"WRONG"`` and
        ``raw`` is the judge's full response text.
    """
    prompt = LLM_JUDGE_PROMPT.format(
        question=question,
        gold_answer=gold_answer,
        generated_answer=generated_answer,
    )
    raw = client.chat(
        model=JUDGE_MODEL,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=JUDGE_TEMPERATURE,
    )
    return _parse_label(raw or ""), (raw or "")


# ---------------------------------------------------------------------------
# Cost estimation (documented before full runs — issue #458 requirement 5)
# ---------------------------------------------------------------------------

# gpt-4o-mini pricing (USD per 1M tokens), as of 2026-07. Assumption-based; the
# estimate is documented, never presented as a measured Popoto result.
_PRICE_INPUT_PER_1M = 0.15
_PRICE_OUTPUT_PER_1M = 0.60
# Rough per-call token assumptions: generation sees retrieved context (~1.5k in,
# ~150 out); judge sees the prompt + both answers (~0.5k in, ~30 out).
_GEN_INPUT_TOKENS = 1500
_GEN_OUTPUT_TOKENS = 150
_JUDGE_INPUT_TOKENS = 500
_JUDGE_OUTPUT_TOKENS = 30


def estimate_cost(n_items: int) -> dict:
    """Estimate the USD cost of judging ``n_items`` (1 generation + 1 judge each).

    Returns a dict with the per-item and total estimate plus the token/price
    assumptions used, so the number is inspectable and clearly assumption-based.
    """
    in_tokens = _GEN_INPUT_TOKENS + _JUDGE_INPUT_TOKENS
    out_tokens = _GEN_OUTPUT_TOKENS + _JUDGE_OUTPUT_TOKENS
    per_item = (
        in_tokens / 1_000_000 * _PRICE_INPUT_PER_1M
        + out_tokens / 1_000_000 * _PRICE_OUTPUT_PER_1M
    )
    return {
        "model": JUDGE_MODEL,
        "n_items": n_items,
        "calls_per_item": 2,
        "usd_per_item": round(per_item, 6),
        "usd_total_estimate": round(per_item * max(n_items, 0), 4),
        "assumptions": {
            "price_input_per_1m_usd": _PRICE_INPUT_PER_1M,
            "price_output_per_1m_usd": _PRICE_OUTPUT_PER_1M,
            "gen_input_tokens": _GEN_INPUT_TOKENS,
            "gen_output_tokens": _GEN_OUTPUT_TOKENS,
            "judge_input_tokens": _JUDGE_INPUT_TOKENS,
            "judge_output_tokens": _JUDGE_OUTPUT_TOKENS,
        },
    }


def judge_identity() -> dict:
    """Return the pinned judge-identity block recorded in every judged artifact."""
    return {
        "protocol": "mem0/gam",
        "protocol_ref": "arXiv:2504.19413",
        "judge_model": JUDGE_MODEL,
        "generation_model": GENERATION_MODEL,
        "temperature": JUDGE_TEMPERATURE,
        "judge_prompt_sha256": prompt_sha256(LLM_JUDGE_PROMPT),
        "generation_prompt_sha256": prompt_sha256(ANSWER_GENERATION_PROMPT),
    }
