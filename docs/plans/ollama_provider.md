---
status: docs_complete
type: feature
appetite: Small
owner: valorengels
created: 2026-04-14
tracking: https://github.com/tomcounsell/popoto/issues/354
last_comment_id:
revision_applied: true
---

# OllamaProvider for Local Embeddings

## Problem

Every embedding operation in Popoto today requires a round-trip to a paid, rate-limited external API (Voyage AI or OpenAI). For high-volume agent-memory workloads this adds latency (100-500ms per call), cost (per-token pricing), and model drift risk (vendor rotates model versions, breaking vector compatibility).

**Current behavior:**
Developers must provision an external API key and accept network dependency, cost, and model-drift risk to use `EmbeddingField`.

**Desired outcome:**
A first-class `OllamaProvider` that speaks to a locally-running Ollama instance, eliminating latency, cost, and vendor-drift concerns. Developers can run Popoto's embedding features without any external API keys.

## Freshness Check

**Baseline commit:** `52f176dd1526f878d9f6d122416c5721e6e88bf9`
**Issue filed at:** 2026-04-14T15:38:34Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/embeddings/__init__.py` -- `AbstractEmbeddingProvider` ABC with `embed()`, `dimensions`, `max_batch_size` -- still holds as described in issue
- `src/popoto/embeddings/openai.py` -- `OpenAIProvider` pattern -- still holds
- `src/popoto/embeddings/voyage.py` -- `VoyageProvider` pattern -- still holds

**Cited sibling issues/PRs re-checked:**
- #259 (ContentField and EmbeddingField) -- closed 2026-03-23, merged as PR #261. Established the provider pattern this plan extends.
- #262 (Docs for ContentField/EmbeddingField) -- closed 2026-03-23, merged as PR #263.

**Commits on main since issue was filed (touching referenced files):**
- None. No commits to `src/popoto/embeddings/` since issue was filed.

**Active plans in `docs/plans/` overlapping this area:** `content_and_embedding_fields.md` exists but that plan is already shipped (PR #261 merged). No active overlap.

**Notes:** All references verified. No drift.

## Prior Art

- **Issue #259 / PR #261**: ContentField and EmbeddingField -- established the `AbstractEmbeddingProvider` pattern with `VoyageProvider` and `OpenAIProvider`. Succeeded. This plan directly extends that pattern.
- **Issue #262 / PR #263**: Documentation for ContentField/EmbeddingField -- added quickstart and RAG recipe docs. Succeeded. Docs will need updating to mention OllamaProvider.

No prior attempts at an Ollama integration found.

## Spike Results

### spike-1: Ollama /api/embed endpoint format
- **Assumption**: "Ollama exposes an embedding endpoint that accepts text and returns vectors"
- **Method**: web-research / code-read
- **Finding**: Ollama provides `/api/embed` (newer) and `/api/embeddings` (legacy). The `/api/embed` endpoint accepts `{"model": "nomic-embed-text", "input": ["text1", "text2"]}` and returns `{"model": "...", "embeddings": [[0.1, 0.2, ...], [0.3, ...]]}`. It supports batched input natively. The legacy `/api/embeddings` endpoint only accepts a single `"prompt"` string.
- **Confidence**: high
- **Impact on plan**: Use `/api/embed` (batch-capable) rather than `/api/embeddings` (single-text only). This simplifies the implementation -- one HTTP POST per batch, no looping.

### spike-2: HTTP client choice
- **Assumption**: "We can use stdlib `urllib.request` to avoid adding a new dependency"
- **Method**: code-read
- **Finding**: Both existing providers use their SDK's HTTP client (`openai.OpenAI`, `voyageai.Client`). The Ollama endpoint is a simple JSON POST -- `urllib.request` from stdlib handles this without adding any dependency. `httpx` is NOT a transitive dependency of Popoto.
- **Confidence**: high
- **Impact on plan**: Use `urllib.request` from stdlib. Zero new dependencies for the base provider. No optional extras needed.

### spike-3: Dimension auto-detection
- **Assumption**: "We can auto-detect embedding dimensions from the Ollama response"
- **Method**: web-research
- **Finding**: The `/api/embed` response includes the embedding vectors but no explicit dimension field. Dimensions can be inferred from `len(embeddings[0])`. Common models: `nomic-embed-text` = 768, `mxbai-embed-large` = 1024, `all-minilm` = 384.
- **Confidence**: high
- **Impact on plan**: Auto-detect dimensions on first `embed()` call by measuring the returned vector length. Fall back to user-supplied `dim` if provided. Cache the detected value.

## Data Flow

1. **Entry point**: User calls `model.save()` or `popoto.semantic_search()`
2. **EmbeddingField.on_save()**: Reads source field content, calls `provider.embed([text], input_type="document")`
3. **OllamaProvider.embed()**: Constructs JSON payload `{"model": "nomic-embed-text", "input": texts}`, POSTs to `http://localhost:11434/api/embed` via `urllib.request`
4. **Ollama server**: Runs local inference, returns `{"embeddings": [[...], ...]}`
5. **OllamaProvider.embed()**: Parses JSON response, returns `List[List[float]]`
6. **EmbeddingField.on_save()**: Saves vector as `.npy` file, stores dimension count in Redis

## Architectural Impact

- **New dependencies**: None. Uses `urllib.request` from stdlib.
- **Interface changes**: None. `OllamaProvider` implements `AbstractEmbeddingProvider` exactly as `OpenAIProvider` and `VoyageProvider` do.
- **Coupling**: No new coupling. Purely additive -- a new provider module alongside the existing two.
- **Data ownership**: No change. Embeddings still stored as `.npy` files.
- **Reversibility**: Trivially reversible -- delete `ollama.py` and remove exports.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

The implementation mirrors the existing `openai.py` almost exactly, with `urllib.request` replacing the OpenAI SDK. Straightforward.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| No external prerequisites | N/A | Uses stdlib only. Ollama is a runtime dependency, not a build dependency. |

## Solution

### Key Elements

- **`OllamaProvider` class**: New provider in `src/popoto/embeddings/ollama.py` implementing `AbstractEmbeddingProvider`
- **Batch embedding via `/api/embed`**: Single HTTP POST per batch, using stdlib `urllib.request`
- **Dimension auto-detection**: First `embed()` call detects dimensions from response; caches the result
- **Clear error messages**: Connection refused -> points to `ollama serve`; model not found -> points to `ollama pull <model>`

### Flow

**User code** -> `popoto.configure(embedding_provider=OllamaProvider())` -> **model.save()** -> `EmbeddingField.on_save()` -> `OllamaProvider.embed()` -> **HTTP POST to localhost:11434** -> vector returned -> `.npy` file saved

### Technical Approach

- Mirror the structure of `openai.py`: same constructor pattern, same `embed()` signature, same property implementations
- Use `urllib.request.urlopen()` with `json.dumps().encode()` for the POST body and `json.loads()` for the response
- Wrap `URLError` (connection refused) in a `RuntimeError` with "ollama serve" hint; wrap `HTTPError` with JSON body parsing (see below)
- Default model: `nomic-embed-text`, default base_url: `http://localhost:11434`
- `max_batch_size`: 32 (conservative for local inference — local forward-pass with 512 texts can OOM on modest hardware; users who need higher throughput can subclass and override)
- No retry/backoff -- local inference should fast-fail
- No `input_type` handling -- Ollama ignores it
- **`dimensions` property guard (C1):** When `self._dim is None` (i.e., `embed()` has not yet been called and no `dim` was passed to the constructor), the `dimensions` property MUST raise `RuntimeError("OllamaProvider: dimensions unknown — call embed() first or pass dim=<n> to the constructor")`. Never return `None` or `0` silently — the `AbstractEmbeddingProvider` contract types `dimensions` as `int`.
- **HTTP error discrimination (C2):** Non-2xx responses from Ollama raise `urllib.error.HTTPError`. Handle explicitly: `except urllib.error.HTTPError as e: body = e.read().decode(); parsed = json.loads(body) if body else {}; msg = parsed.get("error", ""); if "not found" in msg.lower() or "model" in msg.lower(): raise RuntimeError(f"Model '{self._model}' not found. Run: ollama pull {self._model}") from e; else: raise RuntimeError(f"Ollama HTTP {e.code}: {msg}") from e`. This ensures "not found" and "other HTTP error" paths produce distinct, actionable messages.

## Failure Path Test Strategy

### Exception Handling Coverage
- [x] `urllib.error.URLError` when Ollama is not running -- test that `RuntimeError` is raised with "ollama serve" message
- [x] HTTP error responses (e.g., model not found) -- test that `RuntimeError` includes the model name and "ollama pull" hint
- [x] Malformed JSON response -- test graceful error

### Empty/Invalid Input Handling
- [x] Empty text list returns `[]` (matches existing provider pattern)
- [x] None or empty string in texts list -- verify behavior

### Error State Rendering
- [x] Error messages include actionable instructions (not just stack traces)

## Test Impact

No existing tests affected -- this is a greenfield feature adding a new provider module. Existing provider tests in `tests/test_embedding_provider.py` test the abstract interface and the existing providers; those remain unchanged.

New test file: `tests/test_ollama_provider.py`

## Rabbit Holes

- **Async support**: Ollama has async endpoints but Popoto's `AbstractEmbeddingProvider.embed()` is synchronous. Async is out of scope.
- **Streaming embeddings**: Ollama supports streaming for completions but not embeddings. Not relevant here.
- **Model management**: Pulling/listing models from Ollama. That's Ollama CLI's job, not Popoto's.
- **Connection pooling**: `urllib.request` creates a new connection per call. For local requests this is negligible. Do not add `httpx` or `requests` for connection pooling.

## Risks

### Risk 1: Ollama API changes
**Impact:** `/api/embed` endpoint format could change in future Ollama versions.
**Mitigation:** Pin to the well-documented `/api/embed` endpoint. Ollama maintains backward compatibility. If it changes, the error message will surface clearly.

### Risk 2: Dimension mismatch after model swap
**Impact:** If a user changes the Ollama model without re-embedding, stored vectors become incomparable.
**Mitigation:** This is inherent to any embedding model change. Document it clearly. The `dim` parameter allows explicit dimension declaration as a safety check.

## Race Conditions

No race conditions identified. All operations are synchronous HTTP calls to a local server. The `embed()` method is stateless except for caching the auto-detected dimension count, which is always the same value for a given model.

## No-Gos (Out of Scope)

- Async provider variant
- Model management (pull, list, delete)
- Connection pooling or keep-alive
- GPU configuration or model quantization options
- Ollama server health monitoring
- Optional `httpx` or `requests` dependency -- stdlib only

## Update System

No update system changes required -- this is a Popoto library feature, not a deployed service.

## Agent Integration

No agent integration required -- this is a Popoto library embedding provider used via `popoto.configure()`.

## Documentation

### Feature Documentation
- [x] Update `docs/features/content-and-embedding-fields.md` to add OllamaProvider section
- [x] Update `docs/configuration.md` to show OllamaProvider in configure() examples
- [x] Update `docs/fields.md` Embedding Providers list (cascade: three providers, OllamaProvider subsection)

### External Documentation Site
- [x] Update `docs/guides/agent-memory-quickstart.md` with Ollama setup option
- [x] Verify docs build passes with `mkdocs build`

### Inline Documentation
- [x] Docstrings on `OllamaProvider` class and all public methods
- [x] Code comments on error handling logic

## Success Criteria

- [x] `src/popoto/embeddings/ollama.py` ships with working `OllamaProvider` class
- [x] `OllamaProvider` is importable: `from popoto.embeddings.ollama import OllamaProvider`
- [x] `popoto.embeddings.__init__` exports `OllamaProvider` in `__all__`
- [x] Clear error message when Ollama is not running (mentions `ollama serve`)
- [x] Clear error message when model is not found (mentions `ollama pull <model>`)
- [x] Tests pass with mock HTTP responses (no real Ollama required for CI)
- [x] Tests pass (`/do-test`)
- [x] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (ollama-provider)**
  - Name: provider-builder
  - Role: Implement OllamaProvider class and tests
  - Agent Type: builder
  - Resume: true

- **Validator (ollama-provider)**
  - Name: provider-validator
  - Role: Verify implementation matches AbstractEmbeddingProvider contract
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Create OllamaProvider module
- **Task ID**: build-ollama-provider
- **Depends On**: none
- **Validates**: tests/test_ollama_provider.py (create)
- **Informed By**: spike-1 (confirmed: /api/embed supports batch input), spike-2 (confirmed: urllib.request is sufficient), spike-3 (confirmed: dimension auto-detection via vector length)
- **Assigned To**: provider-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/embeddings/ollama.py` mirroring the structure of `openai.py`
- Implement `OllamaProvider(base_url="http://localhost:11434", model="nomic-embed-text", dim=None)`
- Implement `embed()` using `urllib.request.urlopen()` POST to `/api/embed` with JSON body `{"model": model, "input": texts}`
- Parse response JSON `{"embeddings": [[...], ...]}` and return the embeddings list
- Auto-detect dimensions from first response vector length; cache in `self._dim`; if `dim` is provided to constructor, use it directly (skip auto-detection)
- `dimensions` property: if `self._dim is None`, raise `RuntimeError("OllamaProvider: dimensions unknown — call embed() first or pass dim=<n> to the constructor")` — never return `None` silently
- Raise `RuntimeError` with "ollama serve" message on `URLError` (connection refused)
- For `HTTPError`: read body, parse JSON `{"error": "..."}`, branch on "not found"/"model" keywords to raise `RuntimeError(f"Model '{self._model}' not found. Run: ollama pull {self._model}")`, else raise generic `RuntimeError(f"Ollama HTTP {e.code}: {msg}")`
- Return `[]` for empty input list
- Set `max_batch_size` to 32 (conservative for local inference; users may subclass to override)

### 2. Update embeddings __init__ exports
- **Task ID**: build-exports
- **Depends On**: build-ollama-provider
- **Validates**: tests/test_embedding_provider.py (existing, verify no regressions)
- **Assigned To**: provider-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `OllamaProvider` to `__all__` in `src/popoto/embeddings/__init__.py`
- Add OllamaProvider to the module docstring's "Available providers" list

### 3. Create tests
- **Task ID**: build-tests
- **Depends On**: build-ollama-provider
- **Validates**: tests/test_ollama_provider.py (create)
- **Assigned To**: provider-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_ollama_provider.py` following the pattern from `tests/test_embedding_provider.py`
- Test: `OllamaProvider` is a subclass of `AbstractEmbeddingProvider`
- Test: `embed()` with mocked HTTP response returns correct vectors
- Test: `embed([])` returns `[]`
- Test: `dimensions` property returns auto-detected value after first call
- Test: `dimensions` property returns constructor-supplied `dim` if given
- Test: `max_batch_size` returns 512
- Test: `ConnectionRefusedError` / `URLError` raises `RuntimeError` mentioning "ollama serve"
- Test: HTTP error response raises `RuntimeError` mentioning model name
- Test: `input_type` parameter is accepted but ignored
- Mock HTTP calls using `unittest.mock.patch` on `urllib.request.urlopen`

### 4. Validate implementation
- **Task ID**: validate-provider
- **Depends On**: build-tests
- **Validates**: `pytest tests/test_ollama_provider.py tests/test_embedding_provider.py -v`
- **Assigned To**: provider-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify `OllamaProvider` implements all three abstract methods
- Verify error messages contain actionable instructions
- Run `pytest tests/test_ollama_provider.py tests/test_embedding_provider.py -v`
- Verify no regressions in existing embedding tests

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-provider
- **Assigned To**: provider-builder
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/content-and-embedding-fields.md` with OllamaProvider section
- Update `docs/configuration.md` with Ollama example
- Update `docs/guides/agent-memory-quickstart.md` with local Ollama option
- Verify docs build with `mkdocs build`

### 6. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: provider-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify all success criteria met
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Ollama tests pass | `pytest tests/test_ollama_provider.py -v` | exit code 0 |
| Existing embedding tests pass | `pytest tests/test_embedding_provider.py -v` | exit code 0 |
| Import works | `python -c "from popoto.embeddings.ollama import OllamaProvider; print('OK')"` | output contains OK |
| Lint clean | `python -m ruff check src/popoto/embeddings/ollama.py` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | Skeptic, Adversary | C1: `dimensions` returns `None` before first `embed()` — type contract violation against `AbstractEmbeddingProvider.dimensions -> int` | Technical Approach; Task 1 | Raise `RuntimeError` in `dimensions` property when `self._dim is None`: "OllamaProvider: dimensions unknown — call embed() first or pass dim=<n> to the constructor" |
| CONCERN | Skeptic, Adversary | C2: HTTP error discrimination unreliable — plan lacked spec for parsing Ollama's JSON error body from `HTTPError` | Technical Approach; Task 1 | `except HTTPError as e: body = e.read().decode(); parsed = json.loads(body) if body else {}; msg = parsed.get("error", ""); branch on "not found"/"model" keywords for specific vs generic RuntimeError` |
| CONCERN | Skeptic, Operator | C3: `max_batch_size=512` unvalidated for local inference — may OOM on modest hardware | Technical Approach; Task 1 | Lower to `max_batch_size=32`; document that users may subclass to override |
| NIT | Archaeologist | N1: Minimum Ollama version for `/api/embed` not documented | ollama.py (inline comment) | Add one-line comment in `ollama.py` noting minimum Ollama version that introduced `/api/embed` |
| NIT | Operator | N2: Tasks 4–6 missing `**Validates**:` fields — inconsistent with tasks 1–3 | Task 4 | Add `**Validates**: pytest tests/test_ollama_provider.py tests/test_embedding_provider.py -v` to Task 4 |

---

## Open Questions

No open questions. The implementation is straightforward -- mirror `openai.py` with `urllib.request` and target `/api/embed`. All assumptions have been validated via spikes.
