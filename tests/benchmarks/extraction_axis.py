"""Extraction-provider axis for the external/judged benchmark harness (#489).

The external harness historically ingests **raw conversation turns** — one
memory record per turn, verbatim. This module adds a selectable *ingest arm*
so a run can instead route each turn through an
``AbstractExtractionProvider`` (issue #461's ``popoto.extraction`` path)
before writing, and measures what that costs and what it buys.

Three arms:

- ``raw``       — status quo. One record per turn, content verbatim. This is
                  the arm every committed ``results/external/*`` artifact was
                  produced under, so it is the baseline the numbers compare to.
- ``heuristic`` — ``HeuristicExtractionProvider`` (stdlib sentence split +
                  min-length filter). Zero API cost.
- ``claude``    — LLM extraction via the Anthropic Messages API, reusing the
                  *shipped* ``EXTRACTION_PROMPT`` and ``FACTS_SCHEMA`` from
                  ``popoto.extraction.claude`` verbatim, with the model as a
                  run parameter so extraction tiers can be compared.

Why the model is a parameter here but pinned in ``src/``
--------------------------------------------------------
``popoto.extraction.claude.EXTRACTION_MODEL`` is a pinned magic number per
this project's convention (CLAUDE.md). This module does **not** change it and
does **not** change any default behavior — it constructs its own provider for
measurement so #489 can answer the parked maintainer question ("is a cheaper
tier good enough?") with numbers instead of a coin flip.

Cost control: the extraction cache
----------------------------------
Extraction is one API call **per turn**, but LoCoMo's 1986 QA items share
only 10 underlying dialogues — every item re-ingests its dialogue's full
~600-turn history. Extracting naively would mean ~1.19M calls (~$5.6k at the
Opus tier) to cover ground that contains only 5,882 *unique* turns.

Extraction is a pure function of (model, prompt, text), so results are cached
on disk keyed by a hash of exactly those three. That collapses the full
corpus to 5,882 calls, makes re-runs free, and makes the tier comparison
exact: every tier sees byte-identical inputs.
"""

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.popoto.extraction import (
    AbstractExtractionProvider,
    ExtractedFact,
    HeuristicExtractionProvider,
)
from src.popoto.extraction.claude import (
    EXTRACTION_MAX_TOKENS,
    EXTRACTION_PROMPT,
    FACTS_SCHEMA,
    _clamp01,
)
from src.popoto.fields.constants import Defaults

logger = logging.getLogger("POPOTO.Benchmark.Extraction")

ARM_CHOICES = ("raw", "heuristic", "claude")
"""Selectable ingest arms. ``raw`` is the committed baseline."""

DEFAULT_EXTRACTION_MODEL = "claude-opus-4-8"
"""Mirrors the pinned ``popoto.extraction.claude.EXTRACTION_MODEL``."""

CACHE_DIR = Path.home() / ".cache" / "popoto_benchmarks" / "extraction"
"""On-disk extraction cache root. One JSON file per (model, prompt-hash)."""

# USD per 1M tokens, first-party Anthropic API rates as of 2026-08-06.
# claude-sonnet-5 is at its introductory rate through 2026-08-31.
MODEL_PRICING = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


@dataclass
class ExtractionStats:
    """Token/cost/shape accounting for one benchmark run's ingest arm."""

    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    turns_seen: int = 0
    turns_dropped: int = 0
    facts_written: int = 0
    failures: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_call(self, input_tokens: int, output_tokens: int) -> None:
        """Record one live API call's usage.

        Guarded by a lock because cache warming fans out over a thread pool
        and ``+=`` on an int is a non-atomic load/add/store — unguarded it
        silently under-counts tokens, which would understate reported cost.
        """
        with self._lock:
            self.calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1

    def record_cache_hit(self) -> None:
        with self._lock:
            self.cache_hits += 1

    def cost_usd(self, model: str) -> float:
        """Estimated USD spend for the live (non-cached) calls made."""
        price_in, price_out = MODEL_PRICING.get(model, (0.0, 0.0))
        return self.input_tokens / 1e6 * price_in + self.output_tokens / 1e6 * price_out

    def to_dict(self, model: Optional[str] = None) -> dict:
        d = {
            "api_calls": self.calls,
            "cache_hits": self.cache_hits,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "turns_seen": self.turns_seen,
            "turns_dropped_no_facts": self.turns_dropped,
            "facts_written": self.facts_written,
            "extraction_failures": self.failures,
            "facts_per_turn": (
                round(self.facts_written / self.turns_seen, 4)
                if self.turns_seen
                else 0.0
            ),
            "turn_drop_rate": (
                round(self.turns_dropped / self.turns_seen, 4)
                if self.turns_seen
                else 0.0
            ),
        }
        if model:
            d["estimated_cost_usd"] = round(self.cost_usd(model), 4)
        return d


class _ExtractionCache:
    """Disk-backed cache mapping content hash -> serialized ExtractedFact list.

    Keyed by ``sha256(model \\x00 prompt \\x00 text)`` so a prompt or model
    change never silently reuses stale extractions. Loaded once per process
    and flushed on ``save()``; concurrent writers are not supported (the
    harness is single-threaded per run).
    """

    def __init__(self, model: str, prompt: str):
        self._model = model
        prompt_sig = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        safe_model = model.replace("/", "_")
        self._path = CACHE_DIR / f"{safe_model}.{prompt_sig}.json"
        self._prefix = f"{model}\x00{prompt}\x00"
        self._data: Dict[str, list] = {}
        self._dirty = False
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
                logger.info(
                    "Extraction cache: loaded %d entries from %s",
                    len(self._data),
                    self._path,
                )
            except Exception as e:
                logger.warning("Extraction cache unreadable (%s); starting empty", e)
                self._data = {}

    def key(self, text: str) -> str:
        return hashlib.sha256((self._prefix + text).encode("utf-8")).hexdigest()

    def get(self, text: str) -> Optional[List[ExtractedFact]]:
        raw = self._data.get(self.key(text))
        if raw is None:
            return None
        return [
            ExtractedFact(
                text=f["text"],
                entities=f.get("entities", []),
                importance=f.get("importance"),
                confidence=f.get("confidence"),
            )
            for f in raw
        ]

    def put(self, text: str, facts: List[ExtractedFact]) -> None:
        self._data[self.key(text)] = [
            {
                "text": f.text,
                "entities": f.entities,
                "importance": f.importance,
                "confidence": f.confidence,
            }
            for f in facts
        ]
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data))
        tmp.replace(self._path)
        self._dirty = False
        logger.info(
            "Extraction cache: saved %d entries to %s", len(self._data), self._path
        )


class TieredClaudeExtractionProvider(AbstractExtractionProvider):
    """Claude extraction with the model as a measurement parameter.

    Reuses ``EXTRACTION_PROMPT``, ``FACTS_SCHEMA``, ``EXTRACTION_MAX_TOKENS``,
    and the clamping/parse semantics of the shipped
    ``ClaudeExtractionProvider`` verbatim — the only difference is that
    ``model`` is injectable and token usage is recorded, so #489 can compare
    extraction tiers without mutating the pinned constant in ``src/``.

    Fail-open, like the shipped provider: any API or parse error logs a
    warning, increments ``stats.failures``, and returns ``[]``.
    """

    def __init__(
        self,
        model: str = DEFAULT_EXTRACTION_MODEL,
        stats: Optional[ExtractionStats] = None,
        use_cache: bool = True,
        api_key: Optional[str] = None,
    ):
        import anthropic

        self._model = model
        self._client = anthropic.Anthropic(api_key=api_key)
        self.stats = stats or ExtractionStats()
        self._cache = _ExtractionCache(model, EXTRACTION_PROMPT) if use_cache else None

    @property
    def model(self) -> str:
        return self._model

    def flush(self) -> None:
        if self._cache is not None:
            self._cache.save()

    def extract(self, text: str) -> List[ExtractedFact]:
        if not text or not text.strip():
            return []

        if self._cache is not None:
            cached = self._cache.get(text)
            if cached is not None:
                self.stats.record_cache_hit()
                return cached

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=EXTRACTION_MAX_TOKENS,
                system=EXTRACTION_PROMPT,
                messages=[{"role": "user", "content": text}],
                output_config={
                    "format": {"type": "json_schema", "schema": FACTS_SCHEMA}
                },
            )
            usage = getattr(response, "usage", None)
            self.stats.record_call(
                getattr(usage, "input_tokens", 0) or 0 if usage else 0,
                getattr(usage, "output_tokens", 0) or 0 if usage else 0,
            )
            raw_text = next(
                (b.text for b in response.content if b.type == "text"), None
            )
            if raw_text is None:
                logger.warning("extraction[%s]: no text block", self._model)
                self.stats.record_failure()
                return []
            parsed = json.loads(raw_text)
        except Exception as e:
            logger.warning("extraction[%s] failed: %s", self._model, e)
            self.stats.record_failure()
            return []

        raw_facts = parsed.get("facts") if isinstance(parsed, dict) else None
        if not isinstance(raw_facts, list):
            self.stats.record_failure()
            return []

        facts: List[ExtractedFact] = []
        for rf in raw_facts:
            if not isinstance(rf, dict):
                continue
            ft = rf.get("text")
            if not isinstance(ft, str) or not ft.strip():
                continue
            ents = rf.get("entities")
            ents = (
                [e for e in ents if isinstance(e, str)]
                if isinstance(ents, list)
                else []
            )
            facts.append(
                ExtractedFact(
                    text=ft.strip(),
                    entities=ents,
                    importance=_clamp01(
                        rf.get("importance"), Defaults.EXTRACTION_DEFAULT_IMPORTANCE
                    ),
                    confidence=_clamp01(
                        rf.get("confidence"), Defaults.EXTRACTION_DEFAULT_CONFIDENCE
                    ),
                )
            )

        if self._cache is not None:
            self._cache.put(text, facts)
        return facts


def resolve_arm(
    arm: str,
    model: str = DEFAULT_EXTRACTION_MODEL,
    use_cache: bool = True,
) -> Tuple[Optional[AbstractExtractionProvider], ExtractionStats, dict]:
    """Build the ingest arm's provider, stats sink, and identity block.

    Args:
        arm: One of ``ARM_CHOICES``.
        model: Claude model ID (``claude`` arm only).
        use_cache: Whether to use the on-disk extraction cache.

    Returns:
        ``(provider, stats, identity)``. ``provider`` is ``None`` for the
        ``raw`` arm, which writes turns verbatim. ``identity`` is committed
        into the artifact so a number can always be traced to the exact
        arm/model/prompt that produced it.

    Raises:
        ValueError: If ``arm`` is unknown.
        RuntimeError: If the ``claude`` arm is selected without an API key.
    """
    if arm not in ARM_CHOICES:
        raise ValueError(f"Unknown extraction arm {arm!r}; choose from {ARM_CHOICES}")

    stats = ExtractionStats()
    prompt_sha = hashlib.sha256(EXTRACTION_PROMPT.encode("utf-8")).hexdigest()

    if arm == "raw":
        return (
            None,
            stats,
            {
                "arm": "raw",
                "description": "One record per turn, content verbatim (committed baseline).",
            },
        )

    if arm == "heuristic":
        return (
            HeuristicExtractionProvider(),
            stats,
            {
                "arm": "heuristic",
                "provider": "popoto.extraction.HeuristicExtractionProvider",
                "min_length": 10,
                "description": "Sentence split + min-length filter; no API cost.",
            },
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "extraction arm 'claude' requires ANTHROPIC_API_KEY in the environment"
        )
    provider = TieredClaudeExtractionProvider(
        model=model, stats=stats, use_cache=use_cache
    )
    return (
        provider,
        stats,
        {
            "arm": "claude",
            "provider": "tests.benchmarks.extraction_axis.TieredClaudeExtractionProvider",
            "model": model,
            "prompt_sha256": prompt_sha,
            "prompt_source": "popoto.extraction.claude.EXTRACTION_PROMPT (verbatim)",
            "max_tokens": EXTRACTION_MAX_TOKENS,
            "structured_output": "json_schema (FACTS_SCHEMA, verbatim)",
            "cache": "on-disk, keyed by sha256(model+prompt+text)",
            "pricing_usd_per_mtok": MODEL_PRICING.get(model),
        },
    )


def arm_label(arm: str, model: str = DEFAULT_EXTRACTION_MODEL) -> str:
    """Filename-safe suffix label for an ingest arm.

    ``raw`` returns ``""`` so the committed baseline artifact names are never
    perturbed by this axis, matching how ``lexical`` keeps the unsuffixed
    retrieval-mode name.
    """
    if arm == "raw":
        return ""
    if arm == "heuristic":
        return "ext-heuristic"
    return "ext-" + model.replace("claude-", "").replace(".", "")
