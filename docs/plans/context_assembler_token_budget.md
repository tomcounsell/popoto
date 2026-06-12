---
status: docs_complete
type: bug
appetite: Medium
owner: Valor Engels
created: 2026-06-11
tracking: https://github.com/tomcounsell/popoto/issues/408
last_comment_id: none
revision_applied: true
---

# ContextAssembler Token Budget Honesty

## Problem

`ContextAssembler.assemble()` promises to pack retrieved memory records into a
`max_tokens` budget. The default token counter is `lambda r: len(str(r)) // 4`
— but `Model.__str__` returns the **Redis key**, not the record's content. A
record holding 2,000 characters of content is counted as ~12 "tokens" (key
length ÷ 4) while its formatted JSON is 399 real cl100k tokens. Every record
costs the same ~12–14 "tokens" regardless of size, so any budget above
`max_items × ~14` never engages.

**Current behavior:**
- `max_tokens=4000` with the default counter admits everything `max_items`
  allows: measured overshoot ranges from −0.1% (English prose, by coincidence)
  to **+3,091% (emoji)** on the audit PoC corpus.
- `metadata["token_count"]` underreports by up to ~1,063×.
- The docs' own "fix" (`token_counter=lambda record: len(enc.encode(str(record)))`
  at `docs/features/agent-memory.md`) tokenizes the Redis key too — 23 tokens
  for a 399-token record.
- The exception fallback repeats the broken heuristic in two places
  (`context_assembler.py:888-889` and `:897-901`).
- The budget loop is break-not-skip (`:891-892`): one huge mid-ranked record
  terminates packing and discards later records that would fit.
- First record is always admitted (`and budget_selected` guard, `:891`) —
  undocumented unbounded single-record overshoot.

**Desired outcome:**
`max_tokens` honestly bounds what is handed to the LLM. The default counter
measures the serialized record content that gets injected (not the key),
`metadata["token_count"]` reflects reality within a small known tolerance, the
docs example produces a correct count when copy-pasted, and the packing edge
semantics (first-record admission, skip-vs-break) are deliberate and
documented. Non-English, code-heavy, and emoji-heavy content stays within
budget like English prose does.

## Freshness Check

**Baseline commit:** `c1bd02f`
**Issue filed at:** 2026-06-11T05:20:23Z (same day as planning)
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/recipes/context_assembler.py:742` — default counter
  `lambda r: len(str(r)) // 4` — still holds verbatim
- `src/popoto/models/base.py:676-678` — `__str__` returns `str(self.db_key)` —
  still holds verbatim
- `src/popoto/recipes/context_assembler.py:881-895` — budget loop with
  `and budget_selected` guard and `break` — still holds
- `src/popoto/recipes/context_assembler.py:888-889` and `:897-901` —
  duplicated fallback heuristic — still holds
- `src/popoto/recipes/context_assembler.py:707,712-713` — docstring claims
  "Records are dropped to fit" / default `len(str(r)) // 4` — still holds
- `docs/features/agent-memory.md` custom-counter example
  (`enc.encode(str(record))`) — still present
- `docs/features/context-assembler.md:3,147` — contract language ("within
  token budgets", "Budget-select: Fit within `max_items` and `max_tokens`
  constraints") — still present

**Cited sibling issues/PRs re-checked:**
- #407 — closed 2026-06-11 via PR #417 (`c1bd02f`). Sibling audit finding,
  independent defect; its merge touched `context_assembler.py` only for
  TrajectoryMemory crystallize watermark — no overlap with the budget loop.
- #394 (benchmark harness) — adjacent only; this fix does not depend on it.

**Commits on main since issue was filed (touching referenced files):**
- `c1bd02f` feat(confidence): capped-evidence Bayesian update (#407) (#417) —
  irrelevant to the budget loop; all cited line numbers verified unchanged.

**Active plans in `docs/plans/` overlapping this area:** none active.
`context_assembler.md` (shipped #244/#245) and
`context_assembler_hybrid_default.md` (shipped #400) are complete; neither is
in flight.

**Notes:** All audit claims reproduced against current main; zero drift.

## Prior Art

- **#233 / PR #244/#245**: "Add ContextAssembler — retrieval-to-injection
  bridge with token budgets" (merged 2026-03-20) — introduced the budget loop
  and the defective default counter. The bug shipped unnoticed because chars/4
  over ASCII English prose coincidentally lands near tiktoken counts, and the
  original tests only asserted the counter's arithmetic on a raw string
  (`tests/test_context_assembler.py:228`), never on a model instance.
- **PR #400**: "Default ContextAssembler to hybrid retrieval" (merged
  2026-05-22) — rewrote the pull path but did not touch budget selection.
- **PR #366**: Metacognitive layer / AdaptiveAssembler — consumes
  `AssemblyResult.metadata`, including `token_count`; downstream beneficiary
  of an honest count, no interface change needed.
- No prior attempt to fix the token counter exists — this is the first fix,
  so no "Why Previous Fixes Failed" section is needed.

## Research

**Queries used:**
- "token count estimation heuristic chars per token CJK emoji without tiktoken accuracy"

**Key findings:**
- English ≈ 4 chars/token; CJK ≈ 1.5–2 tokens/char; emoji ≈ 2–4 tokens each
  (BPE tokenizes non-ASCII roughly per UTF-8 byte). Zero-dependency estimators
  reach ~±5–15% on English prose, wider on CJK/minified code —
  [tokenx](https://github.com/johannschopplich/tokenx),
  [GPT for Work tokenizer](https://gptforwork.com/tools/tokenizer). Informs
  the shape of the stdlib default counter and the documented accuracy
  expectations.
- Underestimating is the dangerous direction for budget enforcement
  (overshoot reaches the LLM); heuristic constants should be biased to
  overestimate. Informs the spike's selection criterion (b) below.

## Spike Results

### spike-1: Zero-dependency heuristic can meet the ±25% accuracy criterion
- **Assumption**: "A ~5-line stdlib heuristic over the *formatted* record
  string can stay within ±25% of tiktoken cl100k_base on English and avoid
  catastrophic underestimates on code/CJK/URLs/emoji."
- **Method**: prototype (isolated /tmp venv with tiktoken; measured over the
  PoC corpus wrapped in the actual `json.dumps(..., default=str, indent=2)`
  envelope)
- **Finding**: **Plain chars/4 fails** (+42.5% on English over the formatted
  envelope; −57.5% CJK; −59.8% emoji). Critical discovery: `json.dumps`
  defaults to `ensure_ascii=True`, so non-ASCII content is escaped to
  `\uXXXX` — the structured-format payload is **pure ASCII**, and `\uXXXX`
  hex runs tokenize at ~3.7 tokens per 6-char escape. Any naive
  "non-ASCII-aware" heuristic never fires on the structured format. The
  winning heuristic is escape-aware and character-class weighted:
  ```python
  _UESC = re.compile(r"\\u[0-9a-fA-F]{4}")
  _LOW = re.compile(r"[a-z\s]")

  def estimate_tokens(s: str) -> int:
      escapes = len(_UESC.findall(s))
      rest = _UESC.sub("", s)
      low = len(_LOW.findall(rest))    # prose-like: ~5 chars/token
      other = len(rest) - low          # digits/symbols/uppercase: dense
      non_ascii_bytes = sum(
          len(c.encode("utf-8")) for c in rest if ord(c) > 127
      )  # raw unicode (xml/natural formats don't escape)
      return round(
          LOW_CHARS_PER_TOKEN_WEIGHT * low
          + OTHER_CHARS_PER_TOKEN_WEIGHT * other
          + UESC_TOKEN_WEIGHT * escapes
          + NON_ASCII_BYTE_TOKEN_WEIGHT * non_ascii_bytes
      )
  ```
  with weights 0.2 / 0.85 / 3.7 / 0.75 respectively (non-ASCII chars counted
  in the byte term are excluded from `low`/`other`). Measured error over the
  formatted envelope: english **+20.3%**, code **+20.6%**, cjk **+4.5%**,
  urls **−15.0%**, emoji **−1.1%**. Worst-case underestimate −15.0% (URLs);
  all other errors are overestimates — the safe direction.
- **Confidence**: high (direct measurement against cl100k_base on the exact
  AC corpus and envelope)
- **Baseline note (critique follow-up)**: the failing chars/4 baseline above
  WAS measured over the `json.dumps` envelope, not raw input — +42.5% on
  English and −59.8% on emoji both exceed ±25%, so "just `len(s)//4` on the
  serialized string" is ruled out by direct measurement, not assumption.
- **Double-escape check (critique follow-up)**: content that already contains
  a literal `\uXXXX` sequence serializes as `\\uXXXX`; the unanchored
  `_UESC` regex still matches the trailing `\uXXXX` (verified:
  `_UESC.findall(json.dumps({"c": "\\u0041"}))` → 1 match), so it is counted
  at 3.7 plus 0.85 for the leftover backslash — no systematic undercount. A
  pre-escaped-content fixture in the regression corpus locks this in.
- **Impact on plan**: fixes the default-counter formula and constants;
  mandates that the counter measure the **formatted/serialized string** (the
  escape behavior is invisible at the record level); adds the raw non-ASCII
  byte term so xml/natural formats (which do NOT escape) degrade gracefully.

## Data Flow

1. **Entry point**: `ContextAssembler.assemble(query_cues, agent_id, ...)`
2. **Retrieval**: pull path (hybrid/composite) + push path (CyclicDecayField)
   produce candidate records; merge + dedupe.
3. **Budget selection** (`context_assembler.py:877-901`) — the defective
   layer: `max_items` slice, then token-budget loop calling
   `self._token_counter(record)` per record. **Fix lands here**: per-record
   serialization → counter over the serialized string → skip-not-break
   packing → honest `total_tokens`.
4. **Post-effects**: ObservationProtocol on_read, competitive suppression
   (unchanged).
5. **Format**: `format_structured` / `format_xml` / `format_natural` compose
   `result.formatted` from the **pre-serialized strings captured at
   count-time** (wrapper framing added around them) — output is identical to
   today's for unmutated records, and counting can never diverge from
   emission even if post-effects mutate record state.
6. **Output**: `AssemblyResult(records, proactive, formatted, metadata)` —
   `metadata["token_count"]` now reflects the serialized content.

## Architectural Impact

- **New dependencies**: none at runtime (stdlib `re` only). `tiktoken` added
  to the `dev` extra exclusively, so accuracy regression tests can run in CI;
  tests gate on `pytest.importorskip("tiktoken")`.
- **Interface changes** (beta substrate — breaking OK per maintainer
  decision 2026-06-11): `token_counter` contract changes from
  `callable(record) -> int` to `callable(serialized_text: str) -> int` —
  **decided, not open** (formerly Open Question 1; the spike and serializer
  design both assume string-only, and a two-arg variant re-opens the door to
  record-based counting, the exact category error being fixed). The
  assembler serializes each record (per the active `output_format`) and
  passes the **string** to the counter, so user tokenizers measure exactly
  what the formatter emits and the category error becomes unrepresentable.
  Old-contract counters get a **loud construction-time signal**: `__init__`
  probes the callable with a sentinel string and emits a
  `DeprecationWarning` naming the new contract if it raises
  TypeError/AttributeError.
  `_estimate_tokens` and `_serialize_record` stay **module-private** — the
  bug fix needs neither as public API; users interact via the documented
  `token_counter(str)` contract only (promoting them is a separate scope
  decision if ever requested).
- **Coupling**: budget selection now reuses the formatter's
  `_record_to_dict` serialization — counting and emission can no longer
  drift apart (they share one serialization path).
- **Data ownership**: unchanged.
- **Reversibility**: easy — single module plus docs/tests; no storage format
  or schema changes.

## Appetite

**Size:** Medium

**Team:** Solo dev (builder + test-engineer + documentarian agents), PM

**Interactions:**
- PM check-ins: 1-2 (counter-contract breaking change, packing semantics)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli ping` | Test suite backend (tests auto-use DB 15) |

(tiktoken is NOT a prerequisite — task 1 adds it to the dev extras; task 2
verifies `.venv/bin/python -c "import tiktoken"` at its start, after
re-running `uv pip install -e ".[dev]"`. Listing it here would gate the plan
on its own first task.)

## Solution

### Key Elements

- **`_estimate_tokens(text: str) -> int`** (module-private in
  `context_assembler.py`): the spike's escape-aware character-class
  heuristic. Zero-dependency default counter. Constants are module-level
  named experimental-tuning constants (per project magic-number policy), not
  user config.
- **`_serialize_record(record, output_format) -> str`** (module-private):
  produces the **exact per-record string slice the formatter emits**,
  including indentation — structured:
  `json.dumps(_record_to_dict(r), default=str, indent=2)` re-indented to the
  array's nesting level; xml: the `  <record>...</record>` block with its
  two-space prefix; natural: the per-record line including its terminator.
  The contract is compositional and pinned by a golden test:
  `formatter(records) == wrapper_prefix + sep.join(_serialize_record(r) for r
  in records) + wrapper_suffix` with **exact strings** per format. Wrapper
  framing (array brackets, `<records>` envelope) is the only excluded
  residual — documented and bounded (a fixed handful of tokens per assembly,
  independent of record count or size).
- **Count-time strings are format-time strings**: the budget loop stores
  each admitted record's serialized string in a parallel list and the
  formatter **composes the final output from those exact strings** instead
  of re-serializing. This closes the `_post_effects` divergence window (see
  Race Conditions) and makes the golden composition identity hold by
  construction.
- **Counter contract**: `token_counter(serialized_text: str) -> int`. The
  budget loop computes `_serialize_record(record, self.output_format)` once
  per record and feeds the string to the counter. Default:
  `_estimate_tokens`. **Construction-time probe**: `__init__` calls the
  user-supplied counter with a sentinel string inside try/except; on
  TypeError/AttributeError it emits
  `DeprecationWarning("token_counter now receives the serialized record
  string (str), not the record object — update your counter", stacklevel=2)`
  so old-contract counters fail loudly in the caller's own test suite, not
  silently in production.
- **Single fallback path**: one private `_count_record_tokens(record)` helper
  wraps serialize + counter + validation + `except Exception` fallback to
  `_estimate_tokens(serialized)`. The return value is validated —
  `if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
  raise TypeError(...)` inside the try — so `None`/negative/float returns
  hit the same fallback instead of corrupting budget arithmetic. The fallback
  warning is diagnostic: it includes the exception type and the first 80
  chars of the serialized text
  (`logger.warning("token_counter raised %s on serialized text (first 80
  chars: %r); falling back to _estimate_tokens. Contract:
  callable(str) -> int.", type(e).__name__, serialized[:80])`). Both the
  budgeted branch and the `max_tokens is None` accounting branch call it —
  the duplicated heuristic at `:888-889`/`:897-901` disappears.
- **Packing semantics — skip, not break**: a record that doesn't fit is
  skipped and the loop continues, so later smaller records that fit are
  admitted (greedy first-fit in rank order). Documented.
- **First-record admission — keep, document**: the audit explicitly lists
  "never returns zero records" as a positive finding to preserve. A single
  oversized record can still overshoot; with an honest counter the overshoot
  is now *visible* in `metadata["token_count"]`. Documented as a guarantee
  with its tradeoff.
- **Benchmark helper parity**: `tests/benchmarks/metrics/token_efficiency.py`
  duplicates the same `len(str(r)) // 4` defect at line 44; its default
  switches to `_estimate_tokens(_serialize_record(r, "structured"))` (its
  pluggable `callable(record)` signature stays — it is a benchmark metric,
  not the assembler; importing the private helpers is fine for an in-repo
  test utility).

### Flow

`assemble()` → retrieval/merge (unchanged) → `max_items` slice →
**for each record: `_serialize_record` → `token_counter(text)` (validated;
fallback `_estimate_tokens`) → fits? admit + keep string : skip (first
record always admitted)** → post-effects → formatter composes from the kept
strings → `AssemblyResult` with honest `metadata["token_count"]`.

### Technical Approach

- All changes in `src/popoto/recipes/context_assembler.py` except docs/tests
  and the benchmark metric helper.
- `_serialize_record` refactors the existing formatter internals so
  per-record serialization is shared and the formatters compose from the
  count-time strings: `format_structured`/`format_xml`/`format_natural`
  keep emitting byte-identical output for unmutated records (the audit
  verified no mid-record truncation; preserve that). The golden composition
  test pins the exact wrapper prefix/separator/suffix per format.
- Heuristic constants (`0.2`, `0.85`, `3.7`, `0.75`) live as named
  module-level constants (e.g. `LOW_CHARS_PER_TOKEN_WEIGHT = 0.2`) with a
  comment citing the spike measurement — experimental tuning constants, not
  config.
- Docstring updates at `:707` (`max_tokens`) and `:712-713`
  (`token_counter`) to state the new contract.
- No Redis-module usage anywhere (Valkey-identical); pure Python change.
- Performance: serialization now happens per candidate record during budget
  selection and again in the final formatter. Audit baseline is 48ms median
  at 10k candidates; the budget loop only serializes the `max_items` slice
  (default 10), so the added cost is negligible. Do not build a
  serialization cache (see Rabbit Holes).

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] The budget loop's `except Exception` (counter failure → fallback) gets
  a test: a raising `token_counter` must (a) fall back to
  `_estimate_tokens(serialized)` — assert the resulting `token_count` reflects
  content scale, not key scale — and (b) emit `logger.warning` (assert via
  `caplog`).
- [ ] The `max_tokens is None` accounting branch with a raising counter gets
  the same fallback assertion.
- [ ] A counter returning `None`, a negative int, a float, or a bool hits the
  validation guard and falls back (assert `token_count` is a sane non-negative
  int and the diagnostic warning fired) — invalid returns are not exceptions
  and must be caught explicitly.
- [ ] An old-contract `callable(record)` counter (e.g.
  `lambda r: r.importance * 10`) triggers the construction-time
  `DeprecationWarning` (assert via `pytest.warns(DeprecationWarning)`).

### Empty/Invalid Input Handling
- [ ] `_estimate_tokens("")` returns 0; `_serialize_record` on a record with
  all-None fields returns a small valid envelope and a small nonzero count.
- [ ] `assemble()` with zero candidate records still returns an empty result
  with `token_count == 0` (existing behavior preserved).

### Error State Rendering
- No user-visible error surface — library API. Counter failures degrade to
  the heuristic with a warning (tested above), never raise out of
  `assemble()`.

## Test Impact

- [ ] `tests/test_context_assembler.py::test_default_token_counter` (line
  228) — REPLACE: currently asserts the broken contract
  (`_token_counter("hello world") == len("hello world") // 4`). Rewrite to
  assert the default is `_estimate_tokens` and that it measures serialized
  content (a model instance with 2,000 chars of content counts hundreds of
  tokens, not ~12).
- [ ] `tests/test_context_assembler.py` constructor test (line 226,
  `assembler._token_counter is counter`) — UPDATE: identity assertion may
  survive, but the test must ALSO verify the new contract semantically — a
  spy counter records its argument and the test asserts it received a `str`
  (the serialized record), not a record object. Storage identity alone
  verifies nothing about the contract change.
- [ ] `tests/test_context_assembler.py::test_max_tokens_cap` (line 335) —
  UPDATE: strengthen from the weak
  `token_count <= 50 or len(records) <= 1` disjunction to a **conjunction**:
  `token_count` within budget AND `len(records) >= 1`. Add a mixed-size
  scenario (one oversized record ranked between small ones) asserting the
  small record after the oversized one is admitted (skip-not-break) and
  `token_count` reflects only admitted records. The old disjunction passes
  even with the budget loop completely broken.
- [ ] NEW structural guard (closes the gap that let the original bug ship):
  an end-to-end test that instantiates a real Popoto Model with a large
  content field, calls `assemble()`, and asserts
  `result.metadata["token_count"] >= 10 * len(str(record))` — i.e. the count
  reflects content scale, not Redis-key scale. This is the minimal assertion
  that would have caught the original defect.
- [ ] `tests/benchmarks/test_tier4.py` (line 180 passes an explicit
  `token_counter=lambda r: 100`) — verify unaffected; UPDATE only if other
  call sites rely on the metric helper's default counter.
- [ ] `tests/test_adaptive_assembler.py` — verify: consumes
  `metadata["token_count"]` values? If it asserts absolute token numbers,
  UPDATE the expectations to the honest counts.

## Rabbit Holes

- **Bundling tiktoken or making it a runtime dependency** — explicitly
  forbidden by the issue constraints. The default stays stdlib-only.
- **Building a perfect tokenizer-parity heuristic** — ±25% on English with
  bounded error elsewhere is the bar. Chasing single-digit error across all
  content types is open-ended tuning; the constants are named and documented
  for future experimental tuning instead.
- **Serialization caching / formatting once and slicing** — premature
  optimization; assembly latency is already excellent (48ms median at 10k
  candidates) and the budget loop touches ≤ `max_items` records.
- **Optimal bin-packing (admitting lower-ranked records "optimally")** —
  greedy first-fit in rank order is the documented semantic; knapsack-style
  reordering changes relevance guarantees and is out of scope.
- **Counting the exact wrapper framing per format** — measured small by the
  audit; document the exclusion instead of complicating the per-record
  contract.
- **Fixing `Model.__str__`** — returning the Redis key from `__str__` is
  long-standing, documented Popoto behavior with its own consumers (`key_fn`
  defaults, debug output). Changing it has repo-wide blast radius and is not
  needed once the counter measures serialized content.

## Risks

### Risk 1: Breaking change to `token_counter` contract silently mis-measures for existing callers
**Impact:** A user's existing `callable(record)` counter would receive a
string after upgrade; `len(enc.encode(str(record)))`-style counters would
*start working correctly* (str of a str is identity), but counters accessing
record attributes (e.g. `record.content`) would raise `AttributeError` —
caught by the fallback, degrading silently to the heuristic with only a
warning.
**Mitigation:** The substrate is beta and breaking changes are sanctioned
(maintainer decision 2026-06-11). Three layers, from loud to quiet:
(1) construction-time sentinel probe emits `DeprecationWarning` naming the
new contract — catchable by the user's own test suite via warning filters;
(2) the per-call fallback warning is diagnostic (exception type + first 80
chars of the serialized text, so a log reader can see their counter is
receiving a string); (3) prominent documentation in both docs pages, the
docstring, and the release notes.

### Risk 4: Honest budgets shrink upgraders' context windows silently
**Impact:** Every existing caller who set `max_tokens` gets fewer records
injected into their prompts the day they upgrade — same code, smaller LLM
context — because the budget now actually engages. For agents where recall
quality matters this is a silent behavior change; no type checker or unit
test in the caller's codebase will flag it.
**Mitigation:** Purely communicative — there is no code guard for "the bug
you relied on is fixed." A prominent **behavior-change callout** ships in
the same PR: release-notes entry plus an admonition box in
`docs/features/context-assembler.md` stating that `max_tokens` is now
enforced for real, with guidance to audit (or temporarily raise) configured
budgets after upgrading. Task 4 explicitly owns this callout; Success
Criteria include it so it cannot be silently dropped.

### Risk 2: Heuristic constants tuned on the PoC corpus may drift on real-world content
**Impact:** Budgets could under/over-fill by more than the documented
tolerance on content unlike the corpus (e.g., base64-heavy or minified
content; the URL case already underestimates −15%).
**Mitigation:** Document accuracy expectations per content type and the
recommendation to (a) use a real tokenizer via `token_counter` for hard
budgets, or (b) set the budget with a safety margin (e.g. 85% of the true
limit) for URL/hash-heavy memories. Constants are named for experimental
tuning. The first-record guarantee and skip semantics are independent of
constant accuracy.

### Risk 3: `_serialize_record` refactor changes formatter output byte-for-byte
**Impact:** Downstream consumers of `result.formatted` (LLM prompts,
AdaptiveAssembler, docs examples) could see altered output; the audit's
"never truncates mid-record" positive finding could regress.
**Mitigation:** Refactor formatters to *compose* the shared per-record
serialization; add a test asserting `format_structured`/`format_xml`/
`format_natural` output is unchanged for a fixture set (golden comparison
against the pre-refactor implementation's output captured in the test).

## Race Conditions

No multi-process race conditions — the budget-selection and formatting path
is synchronous, single-threaded, and operates on already-fetched in-memory
records. No new Redis commands are introduced by this change.

**In-process divergence window (closed by design):** `_post_effects` runs
between budget selection and formatting (`context_assembler.py:905-910`) and
can mutate in-memory record state (on-read side effects, competitive
suppression, lazy Relationship loading). If the formatter re-serialized
records after that point, count-time and format-time strings could diverge.
The design eliminates the window structurally: the formatter composes
`result.formatted` from the serialized strings captured at count-time, so
what was counted is byte-for-byte what is emitted. A test asserts the
composition identity on records with post-effect-active fields.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #394] Reusing the adversarial content corpus in the external
  benchmark harness — the corpus lands here as regression-test fixtures;
  harness integration belongs to #394.
- [SEPARATE-SLUG #409] Other June-2026 audit defects (retrieval query-blind,
  Q-slot aliasing, PEL redelivery, etc.) are filed as #409–#416 and planned
  independently — this plan touches only the token-budget defect (#408).

## Update System

No update system changes required — popoto is a published library; this is an
internal code/docs/tests change shipped via the normal release process. The
only dependency change is `tiktoken` in the `dev` extra (contributors
re-run `uv pip install -e ".[dev]"`).

## Agent Integration

No agent integration required — this is a library-internal fix with no MCP
surface or bridge involvement.

## Documentation

### Feature Documentation
- [ ] `docs/features/context-assembler.md`: add a **Token Budget Semantics**
  section — counter contract (receives the serialized per-record string),
  default heuristic with per-content-type accuracy table from the spike,
  first-record-always-admitted guarantee (and its overshoot tradeoff),
  skip-not-break packing semantics, wrapper-framing exclusion, and the
  hard-budget recommendation (real tokenizer + safety margin).
- [ ] `docs/features/agent-memory.md`: fix the custom token counter example
  to `token_counter=lambda text: len(enc.encode(text))` and show what `text`
  contains.
- [ ] **Upgrade-impact callout** (Risk 4): release-notes entry + admonition
  in `docs/features/context-assembler.md` — `max_tokens` is now enforced for
  real; upgraders who set it will receive fewer records per assembly and
  should audit (or temporarily raise) their budget values. Old-contract
  `callable(record)` counters now trigger a `DeprecationWarning` at
  construction and fall back to the heuristic at call time.

### External Documentation Site
- [ ] `mkdocs build --strict` passes (docs gate of `scripts/ci-local.sh`).

### Inline Documentation
- [ ] Docstrings for `_estimate_tokens`, `_serialize_record`, the
  `token_counter` arg (`context_assembler.py:712-713`), and `max_tokens`
  (`:707`) updated to the new contract.
- [ ] Named heuristic constants carry a comment citing the spike measurement
  and tuning intent.

## Success Criteria

(Mirrors the issue's acceptance criteria.)

- [ ] Default counter measures serialized record content: for a record with
  2,000 chars of English content, the default count is within ±25% of
  tiktoken cl100k_base over that record's formatted output (spike-validated:
  +20.3%).
- [ ] On the PoC corpus (english/code/cjk/urls/emoji/long-sentence; 20 ×
  2,000-char records, `max_items=10`, `max_tokens=4000`), actual tiktoken
  tokens of `result.formatted` stay within budget +25% for every content type
  when using a tiktoken-based `token_counter` per the fixed docs example.
  **Precondition**: no single record exceeds `max_tokens` (holds on this
  corpus: ~400–500 tokens/record vs 4,000 budget) — the first-record
  guarantee makes single-oversized-record overshoot unbounded by design and
  it is covered by a separate visibility criterion, not this one. Wrapper
  framing is excluded from counting; its residual is fixed per format
  (independent of record count/size) and asserted small (< 20 tokens) by the
  golden composition test.
- [ ] `metadata["token_count"]` is within 25% of actual tiktoken tokens of
  `result.formatted` on the same corpus with a tiktoken-based counter.
- [ ] The docs custom-counter example yields an accurate count when
  copy-pasted.
- [ ] First-record-admission and skip-not-break packing are implemented and
  documented in `docs/features/context-assembler.md`; a single oversized
  record's overshoot is **visible** in `metadata["token_count"]` (asserted
  by a dedicated test).
- [ ] Old-contract `callable(record)` counters trigger a
  `DeprecationWarning` at construction (asserted via `pytest.warns`).
- [ ] Upgrade-impact guidance ships in the same PR: release-notes entry +
  behavior-change admonition in `docs/features/context-assembler.md`
  (Risk 4).
- [ ] Trimmed PoC reproduction lives in `tests/` as regression tests and
  passes against local Redis (Valkey-identical commands; CI runs the real
  Valkey job).
- [ ] No new required dependency: tiktoken remains optional (dev extra only);
  the default counter is stdlib-only.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (budget core)**
  - Name: budget-builder
  - Role: estimator, serialization helper, budget-loop rewrite, benchmark
    helper parity, dev-extra dependency
  - Agent Type: builder
  - Resume: true

- **Test Engineer (regression corpus)**
  - Name: budget-test-engineer
  - Role: corpus regression tests, failure-path tests, existing-test updates
  - Agent Type: test-engineer
  - Resume: true

- **Documentarian (budget semantics)**
  - Name: budget-documentarian
  - Role: docs pages, docstrings, mkdocs gate
  - Agent Type: documentarian
  - Resume: true

- **Validator (token budget)**
  - Name: budget-validator
  - Role: verify success criteria, run verification table
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Core fix: estimator, serializer, budget loop
- **Task ID**: build-budget-core
- **Depends On**: none
- **Validates**: tests/test_context_assembler.py (existing suite must still pass apart from the dispositions in Test Impact)
- **Informed By**: spike-1 (escape-aware heuristic, weights 0.2/0.85/3.7/0.75; counter must receive the serialized string because `ensure_ascii=True` escaping is invisible at record level)
- **Assigned To**: budget-builder
- **Agent Type**: builder
- **Parallel**: true
- Add module-private `_estimate_tokens(text)` with named tuning constants
  per spike-1.
- Add module-private `_serialize_record(record, output_format)` returning
  the exact per-record slice (incl. indentation); refactor
  `format_structured`/`format_xml`/`format_natural` to compose from the
  count-time serialized strings (byte-identical output for unmutated
  records; exact wrapper prefix/separator/suffix per format).
- Rewrite budget selection: per-record serialize → `token_counter(text)`
  (default `_estimate_tokens`) → return-value validation (reject
  None/negative/float/bool into the fallback) → skip-not-break packing,
  first record always admitted, admitted strings kept for the formatter;
  single `_count_record_tokens` helper used by both branches (removes
  duplicated fallback at `:888-889`/`:897-901`); fallback warning is
  diagnostic (exception type + `serialized[:80]` + the new contract).
- Construction-time sentinel probe in `__init__` (`:742`): old-contract
  counters emit `DeprecationWarning` naming the new contract.
- Update docstrings (`:707`, `:712-713`).
- Switch `tests/benchmarks/metrics/token_efficiency.py:44` default to the
  new estimator over serialized content (keep its `callable(record)`
  signature).
- Add `tiktoken` to the `dev` extra in `pyproject.toml`.

### 2. Regression and failure-path tests
- **Task ID**: build-budget-tests
- **Depends On**: build-budget-core
- **Validates**: tests/test_context_assembler_token_budget.py (create), tests/test_context_assembler.py
- **Informed By**: spike-1 (per-type expected error figures for assertion tolerances)
- **Assigned To**: budget-test-engineer
- **Agent Type**: test-engineer
- **Parallel**: false
- First: re-run `uv pip install -e ".[dev]"` and verify
  `.venv/bin/python -c "import tiktoken"` (task 1 added the dev extra).
- Create `tests/test_context_assembler_token_budget.py` from the trimmed
  issue PoC: corpus fixtures (english/code/cjk/urls/emoji, plus a
  pre-escaped-backslash fixture per the spike's double-escape check),
  default-counter content-vs-key assertion, budget-adherence with tiktoken
  counter (`pytest.importorskip("tiktoken")` for accuracy tests; behavior
  tests run without it), `metadata["token_count"]` accuracy, skip-not-break
  admission, first-record guarantee, oversized-single-record overshoot
  visibility, end-to-end Model structural guard
  (`token_count >= 10 * len(str(record))`).
- Failure-path tests per the Failure Path Test Strategy section (raising
  counter → fallback + `caplog` diagnostic warning; invalid return values;
  construction-time `DeprecationWarning` via `pytest.warns`; empty inputs).
- Golden composition test per format: exact-string identity
  `formatter(records) == wrapper_prefix + sep.join(_serialize_record(r)) +
  wrapper_suffix`, asserting the wrapper residual is < 20 tokens, plus
  byte-identity of `_serialize_record` before/after `_post_effects` on
  records with post-effect-active fields.
- Apply the dispositions in Test Impact (REPLACE/UPDATE the listed tests,
  incl. the conjunction rewrite of `test_max_tokens_cap` and the str-spy
  constructor assertion).

### 3. Core validation
- **Task ID**: validate-budget-core
- **Depends On**: build-budget-tests
- **Assigned To**: budget-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the Verification table commands; confirm Success Criteria 1–3, 5–7.
- Confirm no `len(str(r)) // 4` remains in `src/` or benchmark metrics.

### 4. Documentation
- **Task ID**: document-budget-semantics
- **Depends On**: validate-budget-core
- **Assigned To**: budget-documentarian
- **Agent Type**: documentarian
- **Parallel**: false
- Execute the Documentation section (both docs pages, docstring audit,
  `mkdocs build --strict`).
- Fix the agent-memory.md example to the new contract and verify it
  copy-pastes correctly against the test corpus.
- Write the Risk-4 upgrade-impact callout (release-notes entry +
  behavior-change admonition in context-assembler.md) — this item is a
  Success Criterion and must not be dropped.

### 5. Final validation
- **Task ID**: validate-all
- **Depends On**: document-budget-semantics
- **Assigned To**: budget-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all Verification commands and the full suite.
- Verify every Success Criterion including documentation items.
- Report pass/fail per criterion.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Full suite passes | `pytest -q` | exit code 0 |
| Budget regression tests pass | `pytest tests/test_context_assembler_token_budget.py -q` | exit code 0 |
| Assembler suite passes | `pytest tests/test_context_assembler.py -q` | exit code 0 |
| Broken default eradicated | `grep -rn "len(str(r)) // 4" src/ tests/benchmarks/` | exit code 1 |
| Docs example fixed | `grep -n "enc.encode(str(record))" docs/features/agent-memory.md` | exit code 1 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| tiktoken stays out of runtime deps | `python -c "import tomllib;d=tomllib.load(open('pyproject.toml','rb'));assert not any('tiktoken' in x for x in d['project']['dependencies'])"` | exit code 0 |

## Critique Results

War room run 2026-06-11 (7 critics). 3 BLOCKERs, 8 CONCERNs, 2 NITs — all
addressed by revision; one BLOCKER rebutted with measured evidence.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Adversary | Double-escaped `\\uXXXX` content allegedly unmatched by `_UESC` → systematic undercount | REBUTTED with measurement: `json.dumps({"c": "A"-literal})` serializes to `\\u0041` and the unanchored regex matches the trailing `\uXXXX` (1 match, verified) — counted at 3.7 + 0.85 for the leftover backslash. Spike Results records the check; a pre-escaped fixture in the corpus locks it in | Regression fixture in test corpus (Task 2) |
| BLOCKER | User | Honest budgets silently shrink upgraders' context windows; no migration guidance | New Risk 4 + Documentation upgrade-impact callout + Success Criterion; Task 4 explicitly owns it | Release-notes entry + admonition in context-assembler.md |
| BLOCKER | Consistency | Success Criterion 2 ("within budget +25%") contradicted first-record bypass + wrapper exclusion | Criterion 2 restated with precondition (no single record exceeds `max_tokens`) + bounded wrapper residual (< 20 tokens, asserted by golden test); oversized-record overshoot covered by separate visibility criterion | Plan-text fix; visibility test in Task 2 |
| CONCERN | Skeptic | "Byte-identical" formatter claim glossed over per-format framing (xml envelope, 2-space indent) | `_serialize_record` contract pinned to exact per-record slices incl. indentation; golden composition test with exact wrapper prefix/sep/suffix per format | Formatters compose from count-time strings |
| CONCERN | Skeptic | Old-contract counters degrade silently; fallback warning doesn't indicate contract mismatch | Construction-time sentinel probe → `DeprecationWarning` naming new contract | Probe at `__init__` (`:742`), `pytest.warns` test |
| CONCERN | Operator | Fallback warning lacked diagnostics | Warning now includes exception type + `serialized[:80]` + contract statement | In `_count_record_tokens` |
| CONCERN | Operator | `test_max_tokens_cap` disjunction passes even with broken loop | Rewritten as conjunction + mixed large/small skip-not-break scenario | Test Impact updated |
| CONCERN | Archaeologist | No end-to-end Model test guarding the original structural gap | New structural-guard test: `token_count >= 10 * len(str(record))` via `assemble()` on a real Model | Task 2 |
| CONCERN | Adversary | `_post_effects` mutation between count and format diverges serializations | Closed by design: formatter composes from count-time strings; before/after byte-identity test | Race Conditions section rewritten |
| CONCERN | Simplifier | Public `estimate_tokens`/`serialize_record` exceeds bug-fix mandate | Both module-private (`_estimate_tokens`, `_serialize_record`); benchmark helper imports privately | Public promotion deferred to a real feature request |
| CONCERN | Simplifier | Four-term heuristic over-specified; `len//4` on serialized ASCII might suffice | REBUTTED: spike DID measure chars/4 against the dumps envelope — +42.5% English, −59.8% emoji, both outside ±25%; baseline note added to Spike Results | No change to formula |
| CONCERN | Consistency | tiktoken prerequisite is chicken-and-egg with Task 1 | Prerequisite removed; post-task-1 import check moved to Task 2's opening step | — |
| NIT | Archaeologist | Open Question 1 contradicted the already-committed string-only contract | Q1 closed as decided (string-only); Architectural Impact records the decision | — |
| NIT | Adversary | Counter returning None/negative/float corrupts arithmetic without triggering fallback | Explicit return-value validation raises into the fallback path | `isinstance(int)`, not bool, `>= 0` guard |

---

## Open Questions

None — both questions raised during planning are closed:

1. **Counter contract — RESOLVED (string-only)**: `token_counter` receives
   the serialized per-record string (`callable(str) -> int`). A
   `(record, serialized_text)` variant would re-admit record-based counting,
   the exact category error being fixed. Old-contract counters get a
   construction-time `DeprecationWarning` (critique follow-up).
2. **Skip-not-break — RESOLVED (skip)**: issue #408 lists break-not-break's
   discard of later fitting records under *Current behavior* as part of the
   defect, so greedy first-fit skip semantics are the issue's intent, not a
   new policy decision. Admitted records are no longer a strict rank-prefix;
   documented in context-assembler.md.
