---
status: Ready
type: feature
appetite: Medium
owner: Valor Engels
created: 2026-07-20
tracking: https://github.com/tomcounsell/popoto/issues/461
last_comment_id:
revision_applied: true
revision_applied_at: 2026-07-20T06:03:41Z
---

# First-class LLM memory-extraction path — entities, typed facts, importance-on-write

## Problem

A live agent using Popoto's `SubconsciousMemory` recipe extracts what it should
remember from each LLM turn via `extract_memories()`. Today that method is a
**sentence-splitting heuristic**: it regex-splits the response on `.!?`, drops
fragments shorter than 10 chars, and saves each surviving sentence as a flat
`Memory` record with a single caller-supplied importance value. The docstring
itself flags this as the override point ("For LLM-based extraction … override
this method").

**Current behavior:**
- The agent remembers *transcript shards* (raw sentences), not *facts*.
- No entities are identified, so `CoOccurrenceField` associations are never
  seeded at write time.
- Every record gets the same flat `importance` — there is no importance-on-write.
- `ConfidenceField` records all start at the field's fixed `initial_confidence`;
  extraction has no opinion on how certain a given fact is.

Competitors attribute their judged-accuracy lead largely to **structured
write-time extraction** (Memori explicitly; Zep/Hindsight structurally):
entities, typed facts, importance scoring at write time.

**Desired outcome:**
- A first-class, **opt-in** LLM-extraction path — a pluggable provider mirroring
  the `AbstractEmbeddingProvider` pattern — that produces typed memory records
  (text + entities + importance + confidence) which feed the existing primitives:
  `ConfidenceField` initial values, `CoOccurrenceField` associations, and
  importance for composite scoring.
- The zero-dependency sentence-splitting heuristic stays the **default**, with
  its current behavior preserved exactly. `import popoto` never requires an LLM
  SDK or an API key.
- Evaluated, not vibes: measure retrieval/judged-accuracy impact against the
  existing harness where a reasonable lift allows, or explicitly track evaluation
  as a follow-up — never fabricate benchmark numbers.

## Freshness Check

**Baseline commit:** `3cda1c1` (origin/main at plan time)
**Issue filed at:** 2026-07-10T10:03:14Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/recipes/subconscious_memory.py:192-235` — `extract_memories()` is
  still the sentence-splitting heuristic (`_split_sentences` at :267-283). Claim
  holds verbatim.
- `src/popoto/embeddings/__init__.py:24-88` — `AbstractEmbeddingProvider` ABC +
  eager-import of stdlib-only providers, lazy heavy deps. Pattern intact.
- `src/popoto/embeddings/openai.py:17-47` — try/except lazy `import openai`,
  `ImportError("… pip install popoto[openai]")`. Pattern intact.
- `src/popoto/fields/confidence_field.py` — `ConfidenceField.update_confidence`
  (:408) and `initial_confidence` (:161) intact. Note: there is no per-instance
  "set initial confidence" API; the companion hash is seeded with the field's
  fixed `initial_confidence` in `on_save` (:351-358). See Technical Approach §3.
- `src/popoto/fields/co_occurrence_field.py:319` — `link()` is an **instance
  method** `field.link(model_class, source_pk, target_pk, initial_weight=…)`
  (despite the docstring's `CoOccurrenceField.link(...)` example). Confirmed.
- `src/popoto/fields/constants.py` — central `Defaults` registry present; the
  place new extraction magic numbers belong.

**Cited sibling issues/PRs re-checked:**
- Epic #456 (Track B) — open; this is a Track B capability item.
- #458 judged-answer harness (Tier 5) — merged via **PR #475**; lives at
  `tests/benchmarks/judge.py` + `tests/benchmarks/test_judged.py`, pinned
  `gpt-4o-mini` judge/generator, `openai` optional and injected via a
  `JudgeProtocol` fake. This is the evaluation surface referenced below.
- #409 (BM25 first-class retrieval) — closed 2026-06-22; retrieval default is now
  query-dependent, so extraction quality can actually move recall.

**Commits on main since issue was filed (touching referenced files):**
- `f0de2fd` Bump version to 1.8.0 — touched `pyproject.toml` only (version
  string), not the `[project.optional-dependencies]` block. Irrelevant to the
  new `anthropic` extra.

**Active plans in `docs/plans/` overlapping this area:** None. The nearest
neighbours (`subconscious_memory_integration_tests.md`,
`subconscious_memory_constant_tuning*.md`) tune existing behavior and do not
touch `extract_memories()`'s extraction strategy.

**Notes:** No drift. Plan proceeds on the original premise.

## Prior Art

- **PR #265 / #276 / #272 / #268**: SubconsciousMemory recipe + constant tuning.
  Established the recipe and swept its numeric defaults, but never changed the
  extraction strategy — `extract_memories()` has been the heuristic since day one.
  Confirms this is greenfield with respect to extraction.
- **Issue #409 (closed)**: made retrieval query-dependent (BM25 first-class).
  Relevant because it removes the "retrieval is random anyway" confound — better
  write-time extraction can now show up in recall.
- **PR #475 (#458)**: end-to-end judged-answer harness (Tier 5). The evaluation
  vehicle for this work: `tests/benchmarks/judge.py`, `run_external.py`,
  `test_judged.py`. Pinned `gpt-4o-mini`, no hard `openai` dependency, fakes
  injected via `JudgeProtocol`.
- **Embeddings providers (`src/popoto/embeddings/*`)**: the exact structural
  template to mirror — ABC in `__init__.py`, concrete providers in separate
  files, stdlib-only default eagerly imported, heavy/optional deps imported
  lazily with an actionable `ImportError`.

No prior *failed* attempt at LLM extraction exists — this is additive greenfield.
The "Why Previous Fixes Failed" section is therefore omitted.

## Research

**Queries used:** None run — this work is internal to Popoto and mirrors two
in-repo patterns (embeddings providers, judged harness). The one external
surface (Anthropic structured output) is governed by the in-repo `claude-api`
skill, which is the authority the build must consult rather than a web search.

**Key findings:** No external findings — proceeding with codebase context and the
`claude-api` skill's guidance (see Technical Approach §4). The build stage MUST
open the `claude-api` skill to confirm the exact `anthropic` SDK call surface
before writing `ClaudeExtractionProvider`; the sketch below is directional.

## Data Flow

1. **Entry point**: agent finishes an LLM turn → calls
   `SubconsciousMemory.extract_memories(response_text, importance=…)`.
2. **Extraction**: `extract_memories` delegates to
   `self._extractor.extract(response_text)` → `list[ExtractedFact]`.
   - Heuristic provider (default): sentence-split + min-length filter →
     `ExtractedFact(text=sentence, entities=[], importance=None, confidence=None)`.
   - Claude provider (opt-in): one `anthropic` call with a pinned prompt + JSON
     schema → typed facts with entities, importance, confidence.
3. **Persist**: for each fact, build model kwargs and `instance.save()` —
   content = `fact.text`; importance = `fact.importance` if not `None` else the
   flat `importance` arg (importance-on-write).
4. **Seed primitives** (only when the model configures them AND the fact carries
   the signal):
   - `CoOccurrenceField`: if `len(fact.entities) >= 2`, link every entity-name
     pair via `field.link(model_class, entity_a, entity_b, initial_weight=…)`.
   - `ConfidenceField`: if `fact.confidence is not None`, apply it as the first
     `update_confidence(instance, field, signal=fact.confidence)` after save.
5. **Output**: list of saved `Model` instances (unchanged return contract), now
   with associations and confidence seeded when available.

## Architectural Impact

- **New dependencies**: one new *optional* extra, `anthropic>=0.40.0`, imported
  lazily. `import popoto` and `import popoto.extraction` stay dependency-free.
- **Interface changes**: additive only. `SubconsciousMemory.__init__` gains three
  optional kwargs (`extraction_provider`, `confidence_field`, `co_occurrence_field`)
  with defaults that preserve today's behavior byte-for-byte.
- **Coupling**: `recipes/subconscious_memory.py` gains a dependency on the new
  `popoto.extraction` package. Extraction has no dependency back on recipes.
- **Data ownership**: unchanged. Extraction produces plain dataclasses; the model
  and its fields still own all Redis state.
- **Reversibility**: high. The feature is a new package plus additive recipe
  kwargs; reverting is deleting the package and the kwargs.

## Appetite

**Size:** Medium

**Team:** Solo dev, plus code review.

**Interactions:**
- PM check-ins: 1-2 (confirm evaluation scope: wire harness now vs. tracked follow-up)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 (DB 15 via plugin) | `redis-cli -n 15 ping` | Test isolation per CLAUDE.md |
| `claude-api` skill available | `ls ~/.claude/skills 2>/dev/null; echo 'invoke /claude-api at build time'` | Authoritative Anthropic call surface |

The `anthropic` package is **not** a build prerequisite — the default path and
the entire unit-test suite must run without it. A live Claude extraction run
(optional, evaluation only) needs `pip install popoto[anthropic]` and an
`ANTHROPIC_API_KEY`; that is out of scope for merge (see No-Gos).

## Solution

### Key Elements

- **`popoto.extraction` package**: the pluggable extraction surface, mirroring
  `popoto.embeddings`.
  - `AbstractExtractionProvider` — ABC with a single `extract(text) -> list[ExtractedFact]`.
  - `ExtractedFact` — dataclass: `text`, `entities`, `importance`, `confidence`.
  - `HeuristicExtractionProvider` — wraps the **existing** sentence-split logic;
    zero-dependency; the **default**; preserves current behavior exactly.
  - `ClaudeExtractionProvider` — opt-in; lazy `import anthropic`; pinned model +
    pinned prompt; structured JSON-schema output.
- **Extraction constants** in the central `Defaults` registry — default
  importance, default confidence signal, entity-pair link weight — as
  experimental magic numbers, not user config.
- **`SubconsciousMemory` wiring** — `extract_memories()` consumes the provider
  and seeds `ConfidenceField` / `CoOccurrenceField` / importance-on-write.
- **`anthropic` optional extra** in `pyproject.toml`.

### Flow

Agent turn ends → `sm.extract_memories(response_text, importance=0.6)` →
provider yields typed facts → each fact saved as a `Memory` (importance =
fact's own or the flat default) → entity pairs linked, confidence seeded when the
model configures those fields → returns saved instances.

### Technical Approach

**§1 — Package layout (mirror `embeddings/` exactly).**

```
src/popoto/extraction/
    __init__.py        # ABC + ExtractedFact + HeuristicExtractionProvider (eager, stdlib-only)
    claude.py          # ClaudeExtractionProvider (lazy `import anthropic`)
```

`__init__.py`:
- `@dataclass ExtractedFact` with fields:
  - `text: str`
  - `entities: list[str] = field(default_factory=list)`
  - `importance: float | None = None`  # None ⇒ "no opinion; use caller default"
  - `confidence: float | None = None`  # None ⇒ "no opinion; leave field default"
- `class AbstractExtractionProvider(ABC)` with
  `@abstractmethod def extract(self, text: str) -> list[ExtractedFact]: ...`
- `class HeuristicExtractionProvider(AbstractExtractionProvider)` — see §2.
- Eagerly import nothing heavy. `ClaudeExtractionProvider` is imported lazily from
  `.claude` inside a factory or referenced by the caller directly (matching how
  `embeddings/__init__.py` only eagerly imports the stdlib-only providers).
- Do **not** import `anthropic` anywhere at package import time.

**§2 — `HeuristicExtractionProvider` preserves current behavior exactly.**
Move the existing `_split_sentences` regex and the `< extraction_min_length`
filter into the provider. Constructor: `HeuristicExtractionProvider(min_length=DEFAULT_EXTRACTION_MIN_LENGTH)`.
`extract(text)` returns one `ExtractedFact(text=sentence, entities=[],
importance=None, confidence=None)` per surviving sentence. Because `importance`
and `confidence` are `None` and `entities` is empty, the wiring in §3 produces
**identical** DB writes to today's heuristic path (same content, same flat
importance, no confidence update, no links). This equivalence is the acceptance
bar — pin it with a verbatim before/after test (see Test Impact).

**§3 — `extract_memories()` rewrite (behavior-preserving default).**
New `SubconsciousMemory.__init__` kwargs (all optional, all default to today's
behavior):
- `extraction_provider=None` → defaults to
  `HeuristicExtractionProvider(min_length=extraction_min_length)`.
- `confidence_field=None` → name of a `ConfidenceField` on the model, or `None`.
- `co_occurrence_field=None` → name of a `CoOccurrenceField` on the model, or `None`.

Rewritten method:
```
def extract_memories(self, response_text, importance=0.5):
    if not response_text or not response_text.strip():
        return []
    facts = self._extractor.extract(response_text)
    saved = []
    for fact in facts:
        eff_importance = fact.importance if fact.importance is not None else importance
        kwargs = {
            self.agent_id_field: self.agent_id,
            self.content_field: fact.text,
            self.importance_field: eff_importance,
        }
        try:
            instance = self.model_class(**kwargs)
            if instance.save() is not False:
                saved.append(instance)
                self._seed_associations(instance, fact)   # CoOccurrenceField
                self._seed_confidence(instance, fact)      # ConfidenceField
        except Exception as e:
            logger.warning("Failed to save extracted memory: %s", e)
    return saved
```
- `_seed_associations`: no-op unless `self.co_occurrence_field` is set, the field
  exists on the model, and there are `>= 2` **distinct** entities. **Dedupe and
  normalize `fact.entities` first** — the extractor (LLM or otherwise) can return
  duplicate/whitespace-variant names, and `itertools.combinations(["Alice",
  "Bob", "Alice"], 2)` yields the self-pair `("Alice", "Alice")`, which
  `CoOccurrenceField.link()` rejects with `ValueError: Cannot link a PK to itself`
  (`co_occurrence_field.py:353-354`). Build a de-duplicated, order-stable list
  before pairing:
  ```
  seen = set()
  norm_entities = []
  for e in fact.entities:
      e = (e or "").strip()
      if e and e not in seen:
          seen.add(e)
          norm_entities.append(e)
  if not self.co_occurrence_field or len(norm_entities) < 2:
      return
  field = getattr(self.model_class, self.co_occurrence_field)
  for a, b in itertools.combinations(norm_entities, 2):
      try:
          field.link(self.model_class, a, b,
                     initial_weight=Defaults.EXTRACTION_ENTITY_PAIR_LINK_WEIGHT)
      except Exception as exc:            # per-pair guard, INSIDE the loop
          logger.warning("entity link %r<->%r failed: %s", a, b, exc)
  ```
  The try/except is **per-pair, inside the loop** — one bad pair must not abort
  the remaining valid pairs (a loop-level guard would drop every subsequent link
  after the first failure). After dedup the self-pair can no longer occur; the
  guard is defense-in-depth for other link failures (e.g. transient Redis error).
- **Co-occurrence node convention (explicit decision).** Entity **name strings**
  are used as the `CoOccurrenceField` graph nodes — co-occurrence within one fact
  ⇒ association. This is a deliberate departure from the field's usual
  record-PK-as-node convention. **Decision: accept the departure**, because the
  co-occurrence graph is fundamentally an *entity-association* graph (the whole
  point of write-time extraction) and entity names are the natural nodes; there is
  no record PK for an abstract entity like "Alice". Consequences we accept and
  document: (a) these edges are queried via the co-occurrence graph API
  (`propagate`/edge-scan on entity names), NOT via normal `Model.query`, so they
  are intentionally not reachable from the record query path; (b) an entity name
  that happens to equal a real record PK would share an edge-set keyspace — this
  is acceptable because edges live under the `$CoOcF:{ClassName}:{field}:` prefix
  and represent semantic association regardless of node identity, but the feature
  doc must state that entity-name nodes and record-PK nodes coexist in one graph.
  This decision is recorded here (not left implicit) and echoed in Rabbit Holes.
- `_seed_confidence`: no-op unless `self.confidence_field` is set, the field
  exists, and `fact.confidence is not None`. Then
  `ConfidenceField.update_confidence(instance, self.confidence_field,
  signal=fact.confidence)`. **`update_confidence` does NOT store the signal
  verbatim** (`confidence_field.py:408-450`): the companion hash is seeded in
  `on_save` with the field's fixed `initial_confidence` (pseudo-count 1), and
  `update_confidence` computes the capped running mean over `{prior, signal}`. So
  the **first** seeding call with signal `s` yields a stored confidence of
  `(initial_confidence + s) / 2`, not `s`. Additionally, a signal `< 0.5` is
  treated as a *contradiction* and `>= 0.5` as a *corroboration*. The plan and
  tests MUST assert the **computed** stored value (`(initial_confidence + s)/2`),
  not `s`. Document this "first-update-blends-with-prior" nuance in the docstring
  and the feature doc so downstream users don't expect signal==stored. Same
  per-call try/except guard as above.

**§4 — `ClaudeExtractionProvider` (opt-in, lazy).** Mirror `embeddings/openai.py`:
module-level `try: import anthropic … except ImportError: _anthropic_available =
False`; constructor raises `ImportError("anthropic is required to use
ClaudeExtractionProvider. Install it with: pip install popoto[anthropic]")` when
unavailable. Pinned, non-user-configurable constants (magic numbers, per project
convention):
- `EXTRACTION_MODEL = "claude-opus-4-8"` (module constant; not a kwarg).
- `EXTRACTION_PROMPT = "…"` (module constant; the extraction system prompt,
  pinned in-repo per run; not user-configurable).
- A JSON schema describing `{"facts": [{"text","entities","importance","confidence"}]}`.

Call shape (directional — **confirm against the `claude-api` skill at build
time**): use the Messages API with structured output via
`output_config={"format": {"type": "json_schema", "schema": FACTS_SCHEMA}}` —
NOT the deprecated `output_format` param, and NOT assistant-turn prefill (prefill
400s on this model family). Parse the returned JSON into `ExtractedFact`s,
clamping `importance`/`confidence` into `[0, 1]` and defaulting missing/invalid
values to the `Defaults` constants. On any API/parse failure, log a warning and
fall back to returning `[]` (the caller keeps working; no crash).

**§5 — Constants in `Defaults` (`src/popoto/fields/constants.py`).** Add an
`# -- Extraction (extraction/) ---` group:
- `EXTRACTION_DEFAULT_IMPORTANCE = 0.5` (aligns with the current flat default).
- `EXTRACTION_DEFAULT_CONFIDENCE = 0.7` (signal applied when the LLM asserts a
  fact but returns no explicit confidence; a first-pass value, sweep-tunable).
- `EXTRACTION_ENTITY_PAIR_LINK_WEIGHT = 0.1` (matches
  `CO_OCCURRENCE_INITIAL_WEIGHT`; must stay `<= CO_OCCURRENCE_WEIGHT_CAP` or
  `link()` raises). Tag each inline as experimental tuning per the existing file
  convention.

**§5a — Mandatory `test_defaults_sync` registration (merge gate).** Adding any
uppercase attribute to `Defaults` makes
`tests/benchmarks/test_defaults_sync.py::test_all_defaults_covered_by_module_constants`
fail unless the new constant is either (a) registered in
`tests/benchmarks/overrides.py`'s `MODULE_CONSTANTS` with a real module-level
alias, or (b) listed in the `field_kwargs_and_class_attrs` exception set inside
`test_defaults_sync.py` (:51-80). **This plan chooses (b), unconditionally, for
all three constants.** Rationale: the extraction constants are read directly via
`Defaults.EXTRACTION_*` at their use sites (recipe wiring + Claude provider), with
no bare module-level alias — exactly the pattern already used for the
`CO_OCCURRENCE_*` and `LIFECYCLE_*` families that live in that exception set. The
build MUST add these three literal lines to `field_kwargs_and_class_attrs` in
`tests/benchmarks/test_defaults_sync.py`:
```python
# Extraction defaults (extraction/, recipes/subconscious_memory.py) — read via
# Defaults.EXTRACTION_* directly; no module-level alias, so not in MODULE_CONSTANTS.
"EXTRACTION_DEFAULT_IMPORTANCE",
"EXTRACTION_DEFAULT_CONFIDENCE",
"EXTRACTION_ENTITY_PAIR_LINK_WEIGHT",
```
This is a hard, unconditional edit — NOT "update if present". Do not add a
module-level alias and do not touch `MODULE_CONSTANTS` (adding to
`MODULE_CONSTANTS` without a matching module alias would fail the *other* test,
`test_module_alias_matches_defaults`). After the edit, `pytest tests/ -q` must be
green. Verified against the current test file: the set is a plain literal `{...}`
of string names, and `test_all_defaults_covered_by_module_constants` asserts
`defaults_attrs - field_kwargs_and_class_attrs ⊆ MODULE_CONSTANTS.keys()`.

**§6 — `pyproject.toml` extra.** Add, mirroring `openai`:
```
anthropic = ["anthropic>=0.40.0"]
```

**§7 — Exports.** Add `ExtractedFact`, `AbstractExtractionProvider`,
`HeuristicExtractionProvider` to `popoto.extraction.__all__`. Consider surfacing
them from the top-level package only if it does not risk importing `anthropic`
(it won't — those three are stdlib-only). `ClaudeExtractionProvider` stays behind
`popoto.extraction.claude` to keep the lazy-import boundary crisp.

**§8 — Evaluation ("evaluated, not vibes"). Fully deferrable — not a merge
blocker.** To be unambiguous: the "evaluated, not vibes" value claim is
**explicitly permitted to be an unvalidated-at-merge-time follow-up**. Merge is
gated on the extractor + unit tests + import-safety, NOT on a judged-accuracy or
recall lift. If the harness comparison is a reasonable lift, do it; if not,
documenting evaluation as a tracked epic-#456 Track B follow-up is a fully
accepted, complete outcome for this PR. This removes any ambiguity that the value
claim must be proven before merge. The build stage MUST inspect
`tests/benchmarks/` (specifically `judge.py`, `run_external.py`, `test_judged.py`
from PR #475) and decide:
- **If wiring a comparison run is a reasonable lift** (the harness accepts a
  pluggable write-time extractor without harness surgery): add a run comparing
  heuristic vs. Claude extraction on the existing fixture, record the delta with
  the model + prompt SHA pinned in the artifact, and cite it in the PR.
- **Otherwise**: ship the extractor plus solid unit tests and **explicitly state
  in the PR description that judged-accuracy/recall evaluation is a tracked
  follow-up** (reference epic #456 Track B). Do **not** fabricate or estimate
  benchmark numbers. Any live-API evaluation is opt-in and never runs in the
  default test suite.

**§9 — Valkey safety.** This feature touches **no Redis and no Lua** directly. It
produces Python dataclasses and calls existing primitive methods
(`Model.save()`, `CoOccurrenceField.link`, `ConfidenceField.update_confidence`),
which already own their (Valkey-safe, module-free) Lua. Nothing here introduces a
Redis module or server-specific command. If the build finds itself writing Lua or
a Redis command in the extraction package, that is a red flag — stop and reassess.

## Failure Path Test Strategy

### Exception Handling Coverage
- `extract_memories` keeps its per-record `except Exception: logger.warning(...)`
  (currently at :232). Add a test asserting a save failure on one fact does not
  abort the loop and emits a warning.
- `_seed_associations` wraps each `link()` call in a **per-pair** try/except +
  `logger.warning` (inside the loop); add a test that one failing pair does not
  lose the saved memory (still in `saved`) NOR abort the remaining valid pairs.
- `_seed_associations` dedupes `fact.entities` before pairing; add a test that
  duplicate entities (e.g. `["Alice","Bob","Alice"]`) produce no self-loop
  `ValueError` and exactly one edge pair.
- `_seed_confidence` wraps `update_confidence` in try/except + `logger.warning`;
  add a test that an `update_confidence` failure does not lose the saved memory.
- `ClaudeExtractionProvider.extract` swallows API/parse errors → returns `[]` and
  logs; add a unit test with a fake client raising, asserting `[]` + warning.

### Empty/Invalid Input Handling
- Empty / whitespace-only `response_text` → `[]` (preserved; test it).
- Heuristic: sentences below `min_length` are dropped (preserved; test it).
- Claude: malformed JSON, missing keys, out-of-range importance/confidence →
  clamp/default, never raise; test each with a fake client.
- Entities list with `< 2` members → no links attempted; test it.

### Error State Rendering
- Not applicable — no user-visible rendering surface. The observable failure
  signal is `logger.warning` + a graceful return, both asserted above.

## Test Impact

- `tests/` (SubconsciousMemory suite, if any references
  `extract_memories`) — **UPDATE**: the default-path behavior must be unchanged;
  existing assertions should still pass. If none exist, add
  `tests/test_extraction.py` and `tests/recipes/test_subconscious_extraction.py`.
- **New** `tests/test_extraction.py` — REPLACE/CREATE:
  - `HeuristicExtractionProvider` produces facts equivalent to the old
    sentence-split (verbatim before/after equivalence pin).
  - `import popoto` and `import popoto.extraction` succeed with `anthropic`
    **not** installed (assert no `anthropic` in `sys.modules` after import).
  - `ClaudeExtractionProvider()` raises the actionable `ImportError` when
    `anthropic` is absent (monkeypatch the availability flag, mirror the
    embeddings ImportError test).
  - `ClaudeExtractionProvider` parsing/clamping/failure paths via an injected
    fake client (no network, no key) — same seam style as `test_judged.py`'s
    `FakeClient`.
- **New** `tests/recipes/test_subconscious_extraction.py` — CREATE. `extract_memories()`
  currently bundles four concerns (extraction, importance-on-write, co-occurrence
  seeding, confidence seeding); tests MUST exercise **each seam independently**,
  not only via one big integration test:
  - **Default-path equivalence**: default `extract_memories` writes identical
    records to pre-change behavior (content, flat importance, no links, no
    confidence update).
  - **Fact with no entities** (seam: association no-op): fake provider returns a
    fact with `entities=[]` → no `link()` calls, memory still saved.
  - **Fact with 2 entities, co-occurrence field configured** (seam: association):
    exactly one edge pair created; assert on the edge weight
    (`EXTRACTION_ENTITY_PAIR_LINK_WEIGHT`).
  - **Fact with duplicate entities** (B2 regression): `entities=["Alice","Bob","Alice"]`
    → dedup yields one pair `("Alice","Bob")`, **no `ValueError`**, no self-loop.
  - **Model WITHOUT a co-occurrence field configured** (seam isolation): entities
    present but `co_occurrence_field=None` → no links attempted, no error.
  - **Fact with per-fact importance** (seam: importance-on-write): record
    importance overrides the flat default; a fact with `importance=None` falls back
    to the flat arg.
  - **Fact with confidence, confidence field configured** (seam: confidence, C1):
    assert the stored confidence equals `(initial_confidence + fact.confidence)/2`
    (the running-mean-with-prior result), **not** `fact.confidence`.
  - **Model WITHOUT a confidence field configured** (seam isolation): `fact.confidence`
    set but `confidence_field=None` → `update_confidence` never called, no error.
  - **Per-record save-failure isolation**: one failing fact does not abort the loop
    (others still saved, warning emitted).
  - **Link-failure isolation**: a single failing pair (monkeypatched `link` raising)
    does not lose the saved memory nor abort the remaining pairs.
- `tests/benchmarks/test_defaults_sync.py` — **UPDATE (mandatory)**: add the three
  new extraction constants to the `field_kwargs_and_class_attrs` exception set per
  §5a (unconditional, not "if present" — this test exists and gates merge).

## Rabbit Holes

- **Building a taxonomy of "typed facts".** The issue says "typed facts" but the
  primitives consume `(text, entities, importance, confidence)`. Do NOT invent a
  fact-type ontology/enum this round — carry entities + scores and stop.
- **Entity resolution / canonicalization.** Linking raw entity-name strings is
  enough for v1. Do not build an entity registry, alias table, or cross-fact
  entity merging — that is a separate project. (Note: **within-fact** dedup IS in
  scope — it is required to avoid the `link()` self-loop `ValueError`, see §3/B2 —
  but it is a simple `strip()`+set filter, not entity resolution.)
- **Entity-name-as-graph-node departure.** Using entity **name** strings (not
  record PKs) as `CoOccurrenceField` nodes is an intentional, documented decision
  (see §3 "Co-occurrence node convention"). Do NOT re-litigate it into a
  keyed-by-synthetic-PK scheme this round; accept the departure and document that
  entity-name edges are reachable via the co-occurrence graph API, not `Model.query`.
- **A global `configure(extraction_provider=…)` hook.** Resist wiring extraction
  into `popoto.configure()`. Constructor injection on `SubconsciousMemory` is
  narrower and avoids touching global embedding/content config. (Note as an Open
  Question, but default to constructor injection.)
- **Streaming / batching / retries in the Claude provider.** One synchronous call
  with graceful failure is the v1 bar.
- **Making the extraction prompt/model user-configurable.** Explicitly forbidden
  by project convention — they are pinned in-repo constants (magic numbers).
- **Harness surgery for evaluation.** If wiring the extractor into the Tier-5
  harness needs non-trivial refactoring, defer to a tracked follow-up rather than
  reshaping the harness inside this PR.

## Risks

### Risk 1: Behavioral drift in the default path
**Impact:** Existing `SubconsciousMemory` users see different memories written
after upgrade — silent regression.
**Mitigation:** `HeuristicExtractionProvider` wraps the *exact* existing regex +
min-length logic and returns `importance=None`/`confidence=None`/`entities=[]`, so
the wiring produces byte-identical writes. Pin with a verbatim before/after
equivalence test. Default all new kwargs to the current behavior.

### Risk 2: Accidental hard dependency on `anthropic`
**Impact:** `import popoto` breaks for users without the extra — violates the
"opt-in, no API key required" invariant.
**Mitigation:** `anthropic` imported only inside `extraction/claude.py` under
try/except; never referenced at package import time. Test asserts `import popoto`
and `import popoto.extraction` succeed and leave `anthropic` out of `sys.modules`.

### Risk 3: Wrong Anthropic call surface (deprecated param / prefill 400)
**Impact:** The provider 400s at runtime against `claude-opus-4-8`.
**Mitigation:** Build stage opens the `claude-api` skill and uses
`output_config.format` json_schema (not `output_format`, no prefill). Pin
`claude-opus-4-8` as a constant. The sketch here is explicitly directional and
must be confirmed against the skill + installed SDK version.

### Risk 4: `link()` raises on over-cap weight OR self-loop
**Impact:** Entity linking throws if `EXTRACTION_ENTITY_PAIR_LINK_WEIGHT >
CO_OCCURRENCE_WEIGHT_CAP`, or if a duplicated entity produces a self-pair
(`source_pk == target_pk`), which `link()` also rejects with `ValueError`
(`co_occurrence_field.py:353-354`).
**Mitigation:** Set the constant to `0.1` (== `CO_OCCURRENCE_INITIAL_WEIGHT`,
well under the `1.0` cap). **Dedupe/normalize `fact.entities` before pairing** so
self-pairs never reach `link()` (see §3). Guard each `link()` call in a
**per-pair** try/except (inside the loop, so one bad pair can't abort the rest);
add unit tests for both the duplicate-entity and link-failure paths.

## Race Conditions

No race conditions identified. Extraction is synchronous single-threaded Python.
The primitive writes it triggers (`Model.save`, `CoOccurrenceField.link` via
`LINK_WITH_PRUNE_LUA`, `ConfidenceField.update_confidence` via atomic Lua EVAL)
are each individually atomic in Redis/Valkey and already carry their own
concurrency guarantees; this feature adds no new shared mutable state.

## No-Gos (Out of Scope)

- [EXTERNAL] Live Claude extraction runs against the real Anthropic API (needs
  `ANTHROPIC_API_KEY` + `pip install popoto[anthropic]` on a machine the merge
  gate cannot reach). The default suite runs entirely on fakes.
- [SEPARATE-SLUG #456] Full judged-accuracy / SIQ leaderboard comparison of
  heuristic vs. LLM extraction is tracked under epic #456 Track B if not wired in
  this PR (see Technical Approach §8). This PR either includes a fixture-level
  comparison run or documents evaluation as a tracked follow-up — never fabricated
  numbers.
- Entity resolution / canonicalization, a typed-fact ontology, streaming/retry in
  the provider, and a global `configure()` extraction hook — deferred as design
  scope (see Rabbit Holes); not filed as separate issues because they are
  speculative until v1 lands and is measured.

## Update System

No update-system changes required — this feature is a library-internal package
plus an additive optional extra. No deployment/propagation step changes.

## Agent Integration

No agent/MCP integration required in this repo — `SubconsciousMemory` is a library
recipe consumed directly by downstream agent code. The "agent" here is the
library user's own agent loop, which already calls `extract_memories()`; the
change is transparent and opt-in via constructor kwargs.

## Documentation

### Feature Documentation
- [ ] Create `docs/features/llm-memory-extraction.md`: the provider interface, the
  heuristic default, opting into Claude extraction (`pip install popoto[anthropic]`),
  and the ConfidenceField "first-update-as-initial" nuance.
- [ ] Add an entry to the features index if one exists.

### External Documentation Site (MkDocs)
- [ ] Update the SubconsciousMemory / recipes docs page to document the new
  kwargs (`extraction_provider`, `confidence_field`, `co_occurrence_field`).
- [ ] `mkdocs build --strict` passes (docs gate).

### Inline Documentation
- [ ] Docstrings on `AbstractExtractionProvider`, `ExtractedFact`,
  `HeuristicExtractionProvider`, `ClaudeExtractionProvider`, and the rewritten
  `extract_memories` (call out behavior-preservation + the confidence nuance).

## Success Criteria

- [ ] `src/popoto/extraction/` exists with ABC + `ExtractedFact` +
  `HeuristicExtractionProvider` (default) + `ClaudeExtractionProvider` (opt-in).
- [ ] Default `extract_memories()` writes are byte-identical to pre-change
  behavior (verbatim equivalence test passes).
- [ ] `import popoto` and `import popoto.extraction` succeed with `anthropic` not
  installed; `anthropic` absent from `sys.modules` after import.
- [ ] `ClaudeExtractionProvider()` raises an actionable `ImportError` naming
  `pip install popoto[anthropic]` when the SDK is missing.
- [ ] `EXTRACTION_MODEL == "claude-opus-4-8"` and the extraction prompt are pinned
  in-repo constants (asserted by a test, mirroring the judge-pin tests).
- [ ] Extraction feeds importance-on-write, `CoOccurrenceField` links (>=2
  entities, when configured), and `ConfidenceField` signals (when configured).
- [ ] `anthropic = ["anthropic>=0.40.0"]` extra added to `pyproject.toml`.
- [ ] Evaluation either wired against the Tier-5 harness OR documented as a
  tracked follow-up in the PR — no fabricated numbers.
- [ ] New extraction constants live in `Defaults`, tagged as experimental, AND
  are listed in `field_kwargs_and_class_attrs` in `test_defaults_sync.py` so
  `test_all_defaults_covered_by_module_constants` passes.
- [ ] Duplicate entities within a fact produce no self-loop `ValueError` and one
  edge pair; one failing pair does not abort the rest (per-pair guard).
- [ ] Seeded confidence stores `(initial_confidence + signal)/2`, asserted
  explicitly (not `signal`).
- [ ] Each `extract_memories` seam is covered by an independent test (no-entities,
  2-entities, no-co-occurrence-field, importance-override, confidence,
  no-confidence-field).
- [ ] Tests pass (`/do-test`), lint/format clean.
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

The lead agent orchestrates; it does not build directly.

### Team Members

- **Builder (extraction-package)**
  - Name: `extraction-builder`
  - Role: create `src/popoto/extraction/` (ABC, dataclass, heuristic + Claude
    providers), add `Defaults` constants, add the `anthropic` extra.
  - Agent Type: builder
  - Domain: Redis/Popoto data + MCP-tool/API integration (Anthropic)
  - Resume: true

- **Builder (recipe-wiring)**
  - Name: `recipe-builder`
  - Role: rewrite `SubconsciousMemory.extract_memories` + new kwargs + seeding
    helpers; preserve default behavior exactly.
  - Agent Type: builder
  - Resume: true

- **Test Engineer (extraction-tests)**
  - Name: `extraction-tester`
  - Role: unit + recipe tests incl. the before/after equivalence pin, import-safety,
    ImportError, fake-client parse/failure paths, primitive-seeding.
  - Agent Type: test-engineer
  - Resume: true

- **Validator (final)**
  - Name: `extraction-validator`
  - Role: verify success criteria + Verification table.
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: `extraction-docs`
  - Role: feature doc + recipe docs page + docstrings; `mkdocs build --strict`.
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Create the extraction package
- **Task ID**: build-extraction-package
- **Depends On**: none
- **Validates**: tests/test_extraction.py (create)
- **Assigned To**: extraction-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/extraction/__init__.py`: `AbstractExtractionProvider` ABC,
  `ExtractedFact` dataclass, `HeuristicExtractionProvider` (wrap the existing
  `_split_sentences` + min-length logic; return `importance=None`,
  `confidence=None`, `entities=[]`). Eager imports stdlib-only.
- Create `src/popoto/extraction/claude.py`: `ClaudeExtractionProvider` with lazy
  `try/except import anthropic`, pinned `EXTRACTION_MODEL = "claude-opus-4-8"`,
  pinned `EXTRACTION_PROMPT`, JSON schema, `output_config.format` json_schema call
  (confirm surface via `/claude-api`), clamp+default+graceful-failure parsing.
- Add extraction constants to `Defaults` in `src/popoto/fields/constants.py`,
  tagged as experimental.
- **Register the three new `Defaults.EXTRACTION_*` constants in the
  `field_kwargs_and_class_attrs` exception set in
  `tests/benchmarks/test_defaults_sync.py` (per §5a)** — mandatory, unconditional;
  without it `test_all_defaults_covered_by_module_constants` fails.
- Add `anthropic = ["anthropic>=0.40.0"]` to `pyproject.toml`.

### 2. Wire the provider into SubconsciousMemory
- **Task ID**: build-recipe-wiring
- **Depends On**: build-extraction-package
- **Validates**: tests/recipes/test_subconscious_extraction.py (create)
- **Assigned To**: recipe-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `extraction_provider`, `confidence_field`, `co_occurrence_field` kwargs
  (defaults preserve current behavior; default provider =
  `HeuristicExtractionProvider(min_length=extraction_min_length)`).
- Rewrite `extract_memories()` per Technical Approach §3; add `_seed_associations`
  (dedupe entities first; per-pair try/except INSIDE the loop) and
  `_seed_confidence` (per-call try/except; document the running-mean-with-prior
  nuance), each guarded by `logger.warning`.
- Keep the return contract (`list` of saved instances) unchanged.

### 3. Tests
- **Task ID**: build-tests
- **Depends On**: build-recipe-wiring
- **Assigned To**: extraction-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- `tests/test_extraction.py`: heuristic equivalence, import-safety (no `anthropic`),
  ImportError message, Claude provider parse/clamp/failure via fake client, model
  + prompt pin assertions.
- `tests/recipes/test_subconscious_extraction.py`: exercise each seam
  independently (see Test Impact) — default-path equivalence, fact-with-no-entities,
  fact-with-2-entities, duplicate-entities (no self-loop `ValueError`),
  model-without-co-occurrence-field, importance-override, confidence seeding with
  the **computed** `(initial_confidence+signal)/2` assertion,
  model-without-confidence-field, per-record save-failure isolation, and
  per-pair link-failure isolation.
- Add the three `EXTRACTION_*` constants to `field_kwargs_and_class_attrs` in
  `tests/benchmarks/test_defaults_sync.py` and confirm `pytest tests/ -q` is green.

### 4. Evaluation decision
- **Task ID**: build-eval
- **Depends On**: build-tests
- **Assigned To**: extraction-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Inspect `tests/benchmarks/{judge.py,run_external.py,test_judged.py}`. If wiring a
  heuristic-vs-Claude fixture comparison is a reasonable lift, add it (pin model +
  prompt SHA in the artifact). Otherwise document evaluation as a tracked
  follow-up in the PR body. Never fabricate numbers; never call the live API in
  the default suite.

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: build-eval
- **Assigned To**: extraction-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/features/llm-memory-extraction.md`; update recipe docs page;
  docstrings; `mkdocs build --strict` passes.

### 6. Final validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: extraction-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the Verification table; confirm every Success Criterion; generate report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_extraction.py tests/recipes/test_subconscious_extraction.py -q` | exit code 0 |
| Full suite | `pytest tests/ -q` | exit code 0 |
| Defaults sync gate | `pytest tests/benchmarks/test_defaults_sync.py -q` | exit code 0 |
| import safe (no anthropic hard dep) | `python -c "import sys; import popoto, popoto.extraction; assert 'anthropic' not in sys.modules; print('ok')"` | output contains ok |
| Model pinned | `grep -rn 'claude-opus-4-8' src/popoto/extraction/claude.py` | output contains claude-opus-4-8 |
| anthropic extra present | `grep -n 'anthropic' pyproject.toml` | output contains anthropic>=0.40.0 |
| No Lua in extraction pkg | `grep -rn 'eval(\|redis.call\|EVAL' src/popoto/extraction/` | match count == 0 |
| No Redis-module cmds | `grep -rniE 'BF\.|CMS\.|TS\.|FT\.' src/popoto/extraction/` | match count == 0 |
| Lint clean | `python -m ruff check src/popoto/extraction/ src/popoto/recipes/subconscious_memory.py` | exit code 0 |

## Open Questions

1. **Provider wiring surface** — confirm constructor injection on
   `SubconsciousMemory` (default in this plan) rather than a global
   `popoto.configure(extraction_provider=…)` hook. Constructor injection is
   narrower and avoids touching embedding/content global config; is that the
   preferred posture?
2. **Evaluation in-PR vs. follow-up** — should the build attempt the Tier-5
   fixture comparison run in this PR, or is documenting it as an epic-#456 Track B
   follow-up acceptable for merge? (Plan allows either; picks the lower-risk path
   if harness wiring is non-trivial.)
3. **Top-level export** — surface `ExtractedFact` / `HeuristicExtractionProvider`
   from the top-level `popoto` namespace, or keep them under
   `popoto.extraction` only? (They are stdlib-only, so either is import-safe.)
