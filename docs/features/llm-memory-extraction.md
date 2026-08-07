# LLM Memory Extraction

A pluggable provider interface for turning raw LLM turn text into typed memory facts, used by `SubconsciousMemory.extract_memories()`.

## Overview

`SubconsciousMemory.extract_memories()` originally saved memories by splitting an LLM's response into sentences and filtering out short ones -- a zero-dependency heuristic with no notion of entities, importance, or confidence beyond what the caller passed in.

The extraction path is now pluggable. `popoto.extraction` defines an `AbstractExtractionProvider` interface and an `ExtractedFact` dataclass; any provider that implements `extract(text) -> list[ExtractedFact]` can be passed to `SubconsciousMemory(extraction_provider=...)`. The built-in `HeuristicExtractionProvider` wraps the original sentence-splitting behavior exactly, so the default experience is unchanged. An opt-in `ClaudeExtractionProvider` (`popoto.extraction.claude`) calls the Anthropic Messages API with structured JSON-schema output to extract facts along with their entities, importance, and confidence.

## The Provider Interface

```python
from popoto.extraction import AbstractExtractionProvider, ExtractedFact
```

### `ExtractedFact`

A single fact extracted from turn text:

| Field | Type | Description |
|-------|------|--------------|
| `text` | `str` | The fact's text content -- becomes the saved Memory record's content. |
| `entities` | `list[str]` | Proper nouns / named entities mentioned in the fact. Empty if none identified (or the provider doesn't extract entities). |
| `importance` | `float \| None` | Provider's opinion on importance, `[0.0, 1.0]`. `None` means "no opinion; use the caller-supplied default importance". |
| `confidence` | `float \| None` | Provider's opinion on certainty, `[0.0, 1.0]`. `None` means "no opinion; leave the model's `ConfidenceField` at its default initial value". |

### `AbstractExtractionProvider`

```python
class AbstractExtractionProvider(ABC):
    @abstractmethod
    def extract(self, text: str) -> list[ExtractedFact]:
        ...
```

Implementations turn raw text into `ExtractedFact` records. Provider implementations should not raise for ordinary extraction failures -- return an empty list instead (see `ClaudeExtractionProvider`'s fail-open contract below). Write your own provider by subclassing this ABC and implementing `extract()`.

### Shipped providers

| Provider | Records per turn | Dependencies | Default for |
|---|---|---|---|
| `RawTurnExtractionProvider` | 1, verbatim | none | the harness path (`popoto.integrations`) |
| `HeuristicExtractionProvider` | one per sentence | none | `SubconsciousMemory` |
| `ClaudeExtractionProvider` | model's choice | `popoto[anthropic]`, API key | opt-in only |

The two defaults differ deliberately. `SubconsciousMemory` keeps the
heuristic for backward compatibility -- changing it would silently alter the
write behavior of existing code. The harness path is new surface with no
compatibility obligation, so it takes the arm that
[measured best](#evaluation-extraction-lost-to-raw-ingestion).

## `RawTurnExtractionProvider`

Verbatim pass-through: one turn in, one `ExtractedFact` out. No sentence
splitting, no length filter, no rewriting -- only surrounding whitespace is
stripped.

```python
from popoto.extraction import RawTurnExtractionProvider
from popoto.recipes import SubconsciousMemory

sm = SubconsciousMemory(
    agent_id="agent-1",
    extraction_provider=RawTurnExtractionProvider(),
)
```

**This is the harness default**, because a hook fires on every turn and the
[evaluation below](#evaluation-extraction-lost-to-raw-ingestion) put raw
ingestion at 0.3636 judged accuracy against the heuristic's 0.2078 on the
same slice. Defaulting the highest-volume write path in the product to the
measured-worst arm would fill a new user's corpus with the weakest records
Popoto knows how to make. `POPOTO_MEMORY_INGEST=heuristic` opts back into
splitting and logs that cost on first use. See
[Harness Integration](harness-integration.md).

It states no importance or confidence opinion, so the caller-supplied
`importance` is used and `ConfidenceField` stays at its prior. An optional
`max_chars` truncates very long turns; leave it unset unless turns are large
enough to threaten Redis value limits, since truncating is the same
information loss the provider exists to avoid.

## The `SubconsciousMemory` Default: `HeuristicExtractionProvider`

Zero-dependency, stdlib-only, and used automatically when no `extraction_provider` is passed to `SubconsciousMemory`. It splits text on sentence-ending punctuation (`.!?`) followed by whitespace or end-of-string, strips each sentence, and drops any shorter than `min_length` (default 10 chars). Every surviving sentence becomes an `ExtractedFact` with no entities and `importance=None`, `confidence=None` -- so downstream wiring always falls back to the caller-supplied `importance` and never touches `confidence_field`.

**Behavior-preservation guarantee**: this reproduces `SubconsciousMemory.extract_memories()`'s pre-extraction-package behavior byte-for-byte. Existing code that doesn't pass any of the three new kwargs (`extraction_provider`, `confidence_field`, `co_occurrence_field`) sees no behavior change.

## Opting Into Claude Extraction

Install the optional extra:

```bash
pip install popoto[anthropic]
```

Construct a `ClaudeExtractionProvider` and pass it to `SubconsciousMemory`:

```python
from popoto.extraction.claude import ClaudeExtractionProvider
from popoto.recipes.subconscious_memory import SubconsciousMemory

sm = SubconsciousMemory(
    model_class=Memory,
    agent_id="agent-1",
    score_weights={"relevance": 0.6, "confidence": 0.3},
    extraction_provider=ClaudeExtractionProvider(api_key="your-key"),
)
```

`ClaudeExtractionProvider` calls the Anthropic Messages API once per `extract()` call with structured JSON-schema output, so facts, entities, importance, and confidence come back typed -- no ad hoc parsing. `api_key` defaults to the `ANTHROPIC_API_KEY` env var (via the Anthropic SDK's normal resolution) if not passed.

The model and prompt are pinned module constants (`EXTRACTION_MODEL`, `EXTRACTION_PROMPT` in `popoto/extraction/claude.py`) rather than constructor kwargs -- consistent with this project's convention that experimental-tuning constants live in-repo, not as user config surface.

**`popoto.extraction.claude` is imported lazily.** `import popoto` and `import popoto.extraction` never import the `anthropic` package; only importing `popoto.extraction.claude` itself does. Constructing `ClaudeExtractionProvider` without `anthropic` installed raises `ImportError` with an install hint.

**Fail-open on API/parse errors.** If the API call or response parsing fails for any reason, `extract()` logs a warning and returns an empty list rather than raising -- a flaky extraction call never crashes the caller's turn loop.

## Seeding `confidence_field` and `co_occurrence_field`

Two more optional `SubconsciousMemory` kwargs let extracted facts feed other memory-system primitives. Both are opt-in and no-ops unless set:

```python
sm = SubconsciousMemory(
    model_class=Memory,
    agent_id="agent-1",
    score_weights={"relevance": 0.6, "confidence": 0.3},
    extraction_provider=ClaudeExtractionProvider(),
    confidence_field="certainty",         # name of a ConfidenceField on Memory
    co_occurrence_field="associations",   # name of a CoOccurrenceField on Memory
)
```

- **`confidence_field`**: if set, and a fact carries a `confidence` opinion (not `None`), `extract_memories()` calls [`ConfidenceField.update_confidence()`](confidence-field.md) on the saved instance with that opinion as the signal.
- **`co_occurrence_field`**: if set, and a fact names two or more distinct entities, every unordered pair of (deduplicated) entity names is linked via [`CoOccurrenceField.link()`](co-occurrence-field.md). Entity name strings -- not the saved record's PK -- are used as graph nodes, since co-mention within one fact is an association between entities, and there is no record PK for an abstract entity.

Since `HeuristicExtractionProvider` never sets `entities` or `confidence`, both kwargs are effectively inert with the default provider -- they only do work once an entity/confidence-emitting provider (like `ClaudeExtractionProvider`) is configured.

### The Confidence Blend Nuance

`ConfidenceField` has no per-instance "set initial value" API. When a `Memory` record is saved, its companion confidence hash is seeded with the field's fixed `initial_confidence` (a prior pseudo-observation), not with anything from the extracted fact. `_seed_confidence()`'s call to `update_confidence(signal=fact.confidence)` is therefore the **first evidence update against that prior**, not a hard override.

Concretely, for the default `initial_confidence=0.5`, seeding with a fact confidence of `s` yields a stored confidence of:

```
(0.5 + s) / 2
```

not `s` verbatim. A fact extracted with `confidence=0.9` produces a stored confidence of `0.7`, not `0.9`. See [ConfidenceField's update formula](confidence-field.md#update-formula) for the full running-mean derivation -- the same blending applies here, it's just the first update rather than a later one.

Don't write code that asserts `ConfidenceField.get_confidence(instance, field) == fact.confidence` after extraction -- it won't hold except by coincidence.

## Example: End-to-End

```python
from popoto import (
    Model, AutoKeyField, KeyField, StringField, FloatField,
    DecayingSortedField, ConfidenceField,
)
from popoto.fields.co_occurrence_field import CoOccurrenceField
from popoto.extraction.claude import ClaudeExtractionProvider
from popoto.recipes.subconscious_memory import SubconsciousMemory

class Memory(Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(base_score_field="importance", partition_by="agent_id")
    certainty = ConfidenceField(initial_confidence=0.5)
    associations = CoOccurrenceField(symmetric=True)

sm = SubconsciousMemory(
    model_class=Memory,
    agent_id="agent-1",
    score_weights={"relevance": 0.6, "confidence": 0.3},
    extraction_provider=ClaudeExtractionProvider(),  # requires popoto[anthropic]
    confidence_field="certainty",
    co_occurrence_field="associations",
)

new_memories = sm.extract_memories(
    "Alice met Bob at the AI conference in Paris. They discussed the new product launch."
)
# Each saved Memory's `certainty` field is now seeded (blended with the 0.5 prior),
# and entity pairs mentioned together (e.g. "Alice"/"Bob", "Alice"/"Paris") are
# linked in `associations`.
```

## Writing a Custom Provider

Any object implementing `AbstractExtractionProvider.extract()` works -- you're not limited to the two built-ins:

```python
from popoto.extraction import AbstractExtractionProvider, ExtractedFact

class MyProvider(AbstractExtractionProvider):
    def extract(self, text: str) -> list[ExtractedFact]:
        facts = my_extraction_call(text)  # e.g. a different LLM, a local NER model, ...
        return [
            ExtractedFact(text=f["text"], entities=f.get("entities", []),
                           importance=f.get("importance"), confidence=f.get("confidence"))
            for f in facts
        ]

sm = SubconsciousMemory(model_class=Memory, agent_id="agent-1",
                         score_weights={"relevance": 1.0}, extraction_provider=MyProvider())
```

## Evaluation: extraction lost to raw ingestion

Extraction has been measured against plain turn ingestion on the judged-answer
harness, and it lost on every arm. Keep it off unless you have a reason from
your own data.

LoCoMo, lexical retrieval, `gpt-4o-mini` as both generator and judge at
temperature 0, 77 scored items, identical corpus and sample across all five
arms:

| Ingestion path | Judged accuracy | Correct / 77 | Turns dropped | Records per turn |
|---|---:|---:|---:|---:|
| Raw turn ingestion | **0.3636** | 28 | 0% | 1.00 |
| `HeuristicExtractionProvider` | 0.2078 | 16 | 0.3% | 3.00 |
| `ClaudeExtractionProvider`, Sonnet | 0.1948 | 15 | 27.3% | 2.32 |
| `ClaudeExtractionProvider`, Opus | 0.1429 | 11 | 36.9% | 1.71 |
| `ClaudeExtractionProvider`, Haiku | 0.0519 | 4 | 63.4% | 0.98 |

Two mechanisms, pulling the same direction.

**The Claude arms discard evidence.** Accuracy falls monotonically with the
turn drop rate. `EXTRACTION_PROMPT` instructs the model to skip filler, and a
large share of the turns it judges to be filler are the ones carrying the
ground-truth evidence. Haiku drops 63% of turns and takes the accuracy with
them. The answer is gone before retrieval ever runs.

**The heuristic arm fragments context.** It drops almost nothing but splits
each turn into roughly three sentences. A sentence retrieved without the turn
around it does not give the generator enough to answer from, and that costs 16
points on its own.

Artifacts: `tests/benchmarks/results/external/locomo_latest_ext-*_judged.json`
and `locomo_latest_judged.json` for the raw-ingestion baseline. Method,
sampling scope, and the confidence interval on the baseline are in
[Benchmarking](../benchmarks.md#extraction-measured).

**What this does not say.** It measures extraction as a *replacement* for
storing the turn, on one dataset of multi-session personal dialogue where the
ground truth is a specific turn. It does not measure extraction as an addition
alongside raw turns, and it does not generalise to corpora where the raw unit
is too long or too noisy to retrieve directly. Those are the cases worth
testing on your own data before ruling extraction out.

## See Also

- [SubconsciousMemory Recipe](../guides/subconscious-memory-recipe.md) -- the recipe that consumes extraction providers
- [ConfidenceField](confidence-field.md) -- update formula and blending behavior
- [CoOccurrenceField](co-occurrence-field.md) -- association graph seeded from co-mentioned entities
- [Harness Integration](harness-integration.md) -- where `RawTurnExtractionProvider` is the default write path
