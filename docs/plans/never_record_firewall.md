---
status: Ready
revision_applied: true
revision_applied_at: 2026-08-17T05:03:17Z
type: feature
appetite: Medium
owner: Dev lane (session/never_record_firewall)
created: 2026-08-17
tracking: https://github.com/tomcounsell/popoto/issues/561
last_comment_id:
---

# M2 — Never-record firewall: deterministic pre-storage privacy gate

## Problem

Popoto now ships a subconscious memory harness that fires on **every turn** of
Claude Code, Codex, Hermes, and OpenClaw (#515, merged as e220b2e). The harness
default is `RawTurnExtractionProvider` — raw turn ingestion, chosen because it
measured 0.3636 judged accuracy against the heuristic splitter's 0.2078. Raw
ingestion means the literal text of a developer's terminal turn is written to
Redis.

Nothing in `src/` inspects that text for secrets.

**Current behavior:**

- `HeuristicExtractionProvider` stores any sentence at or above `_min_length`
  characters (`src/popoto/extraction/__init__.py:125-131`) — a pasted
  `sk-ant-api03-...` line is persisted verbatim.
- `ClaudeExtractionProvider`'s pinned prompt says only "skip greetings, filler,
  and purely conversational scaffolding" (`src/popoto/extraction/claude.py:68-70`).
  A model instruction is not a guarantee.
- `DefaultMemory` deliberately omits `WriteFilterMixin`
  (`src/popoto/recipes/default_memory.py`, "Deliberately **not** included"), so
  the batteries-included model has no write gate of any kind.
- The only privacy posture in the codebase is telemetry's ids-only capture
  default (`src/popoto/recipes/memory_telemetry.py:25-29`) — a read-path
  concern, not a write gate.
- A repo-wide grep for `secret|credential|redact|pii|off.the.record` over `src/`
  returns zero functional hits. Greenfield.

**Desired outcome:**

A deterministic predicate runs in the save path *before* any model, field hook,
or serializer sees the content, and hard-drops a narrow, precisely enumerated
guaranteed class:

1. credential-shaped strings (provider-prefixed tokens, key blocks, credential
   assignments, URL userinfo, high-entropy tokens),
2. off-the-record-marked content (voids the **entire turn**, not a guessed span),
3. an enumerated set of sensitive categories (private key blocks, Luhn-valid
   payment card numbers, US SSN-shaped strings).

Each drop leaves a **content-free tombstone** — a random id plus a reason code —
so drop volume stays auditable while the content itself never reaches Redis.
Zero false negatives on the guaranteed class is the design goal; over-blocking
is explicitly accepted.

## Freshness Check

**Baseline commit:** `e220b2e` (origin/main at plan time)
**Issue filed at:** 2026-08-13T06:26:50Z
**Disposition:** Minor drift

Re-verified every file:line reference in issue #561 against `e220b2e`:

| Issue reference | Status at e220b2e | Note |
|---|---|---|
| `src/popoto/models/base.py:1146-1153` (WriteFilter seam) | **Drifted** to `base.py:1217-1224` | Claim holds exactly: `if isinstance(self, WriteFilterMixin) and not skip_write_filter:` then `try: self._check_write_filter() / except SkipSaveException: return pipeline if pipeline else False` |
| `base.py:1198` (hset_mapping construction) | **Drifted**; `pre_save(...)` is now at `base.py:1233`, full-save encode at `base.py:1438` (`:1259-1260` is the `update_fields` partial path) | Claim holds: serialization happens after the WriteFilter block on both paths |
| `src/popoto/extraction/__init__.py:125-126` | **Drifted** to `:125-131` | Claim holds verbatim |
| `src/popoto/extraction/claude.py:68` | **Unchanged** | Prompt text confirmed |
| `src/popoto/recipes/memory_telemetry.py:25-29` | **Unchanged** | ids-only default confirmed |
| `src/popoto/recipes/subconscious_memory.py:367-368` (broad `except Exception`) | **Drifted** to `:380` | Claim holds: `except Exception as e: logger.warning("Failed to save extracted memory: %s", e)`; `:376-378` is the `if instance.save() is not False:` block above it |
| `src/popoto/fields/write_filter.py:82-159` | **Unchanged** | `compute_filter_score` / `_check_write_filter` confirmed |
| `src/popoto/recipes/memory_lifecycle.py:120` (payload-archiving `Tombstone`) | **Drifted** to `:119-145` | Claim holds: the dataclass carries `fingerprint`, `importance_at_death`, etc. |

**Material change since filing (not in the issue):** commit `e220b2e`
(PR #546, #515) landed on 2026-08-17 and added `src/popoto/integrations/`
— a per-turn hook + MCP server writing through `DefaultMemory`. This is a new,
high-volume, high-risk write surface that did not exist when #561 was written.
It funnels through a single call site,
`src/popoto/integrations/service.py:230` → `SubconsciousMemory.extract_memories`,
so it is covered by this plan's design without new integration points — but it
raises the stakes and it is why the plan makes `DefaultMemory` gated by default
rather than leaving the firewall opt-in.

**Sibling issues:** #456 (epic) OPEN. #580/PR #582 (V0 validity primitives) is
OPEN and unmerged; this plan is independent of it and bases on `origin/main`.
No `docs/plans/` entry overlaps this area (checked all 100 plan docs; nearest
neighbors `write_filter_mixin.md` and `memory_lifecycle.md` describe the two
mechanisms this plan deliberately does *not* extend).

## Prior Art

- **#456** — Epic: SOTA memory system for live agents. This is module M2, Wave A.
- **`docs/plans/write_filter_mixin.md`** / `src/popoto/fields/write_filter.py` —
  the salience gate. Recon in #561 considered extending it and rejected it; this
  plan honors that rejection (see No-Gos).
- **`docs/plans/memory_lifecycle.md`** — introduces `Tombstone`, which
  deliberately archives the payload so forgetting stays reversible. The
  never-record tombstone is the opposite: content-free by construction. Two
  different objects, two different key namespaces.
- **#513 / PR #526** — `DefaultMemory`; its docstring documents why
  `WriteFilterMixin` was excluded (silent data loss is a bad default). That
  reasoning does not transfer: a firewall's whole job is to drop, and it drops
  loudly into an auditable counter.
- **#515 / PR #546** — the harness that makes this urgent.
- Prior-art search over closed issues and merged PRs for
  `secret|credential|privacy|redact|pii` found no previous attempt. There is no
  "Why Previous Fixes Failed" section because there are no previous fixes.

## Research

No external research was required: the deterministic detectors are
self-contained pattern/entropy code with no new dependency, and the credential
prefixes are published vendor formats already widely encoded in open-source
secret scanners. Two constraints came from repo memory rather than the web:
Valkey compatibility forbids Redis modules (so tombstone storage uses only
HASH + LIST primitives), and numeric constants must be pinned in `Defaults`
rather than exposed as constructor kwargs.

## Spike Results

No spikes. The work is greenfield deterministic code with no external
dependency, no async surface, and no unverified integration assumption — the
one integration seam (`Model.save()` before the WriteFilter block) was read
directly and is quoted in the Freshness Check.

## Data Flow

Two write paths reach Redis today, and both are covered:

**Path A — harness / MCP (the high-volume path):**

```
Claude Code / Codex hook  or  MCP memory_save
  → popoto.integrations.service.MemoryService.capture (service.py:230)
  → SubconsciousMemory.extract_memories(response_text)      [TURN-LEVEL GATE #1]
      → provider.extract(text) -> [ExtractedFact, ...]
      → for fact: model_class(**kwargs).save()               [SAVE-LEVEL GATE #2]
          → NeverRecordMixin._check_never_record()  <-- NEW, first thing in save()
          → WriteFilterMixin._check_write_filter()
          → pre_save() -> encode -> HSET + index hooks
```

**Path B — direct ORM save** (`instance.save()` on any model): reaches
GATE #2 only, and only when the model carries `NeverRecordMixin`.

The turn-level gate (#1) exists because the off-the-record marker must void the
**whole turn**, including facts the extractor derived from adjacent sentences —
a per-record gate cannot do that, since the marker may live in a sentence that
produced no fact. The save-level gate (#2) exists because it is the only place
that catches content arriving by any other route.

Ordering guarantee: the gate runs before `pre_save()`, therefore before
`encode_popoto_model_obj`, therefore before any HSET, index write, BM25
tokenization, embedding call, or co-occurrence edge. Dropped content is never
serialized and never leaves the process.

## Architectural Impact

- **New package** `src/popoto/privacy/` — `never_record.py` (scanner + mixin)
  and `__init__.py`. Placed outside `fields/` because a firewall is not a field:
  it has no descriptor, no Redis-backed value, and no per-field storage. The
  mixin is a model mixin like `WriteFilterMixin`, but the scanner is a pure
  function that must be importable with no ORM context (M3 will call it on
  extraction candidates before an LLM sees them).
- **`Model.save()`** gains one guarded block, ~6 lines, before the existing
  WriteFilter block. Cost for models without the mixin is a single `isinstance`
  check — the same shape the WriteFilter and EventStream blocks already pay.
- **`DefaultMemory`** gains `NeverRecordMixin` in its bases. This is a behavior
  change for existing adopters of `DefaultMemory`: saves containing
  credential-shaped content now return `False` instead of persisting.
  Intentional, documented in CHANGELOG, reversible via the kill switch.
- **New Redis key namespace** `$NR:` — disjoint from `$WF:` (write filter) and
  `$TOMB:` (lifecycle tombstones).
- **New exception** `NeverRecordException(SkipSaveException)`. Subclassing
  `SkipSaveException` means every existing handler that already swallows a
  filtered save — including `subconscious_memory.py:376-378`'s broad
  `except Exception` — behaves correctly with no change, while callers that want
  to distinguish a privacy drop from a salience skip can catch the subclass.

## Appetite

**Medium.** One new module (~350 lines incl. patterns), one 6-line seam in
`save()`, one mixin added to `DefaultMemory`, one turn-level check in
`extract_memories`, constants in `Defaults`, a test file, and docs. No new
dependency, no async, no migration, no benchmark run.

## Prerequisites

- None. The module has no dependency on other M-wave modules or on #580/PR #582.
- One pre-requisite *within* the codebase, called out by the issue's recon: the
  broad `except Exception` at `subconscious_memory.py:376-378`. Handled by
  design (the gate signals via `SkipSaveException`, and `save()` catches it and
  returns `False` — control never reaches that handler), so no change to the
  handler is required. Verified by test.

## Solution

### 1. The scanner (pure, deterministic, no I/O)

`src/popoto/privacy/never_record.py`

```
NeverRecordVerdict = namedtuple-ish dataclass:
    blocked: bool
    reason: str | None      # stable reason code, e.g. "credential_prefix"
    detector: str | None    # which detector fired
scan_never_record(text: str) -> NeverRecordVerdict
```

Detectors, evaluated in order, first hit wins:

| Reason code | What it matches |
|---|---|
| `off_the_record` | An explicit marker: `off the record`, `off-the-record`, `do not record`, `don't record`, `do not remember`, `no memory`, `<!-- no-memory -->`, `[[private]]` (case-insensitive, word-boundary) |
| `private_key_block` | `-----BEGIN ... PRIVATE KEY-----`, `-----BEGIN OPENSSH PRIVATE KEY-----`, `PuTTY-User-Key-File` |
| `credential_prefix` | Vendor-prefixed tokens: `sk-ant-`, `sk-`/`sk_live_`/`rk_live_`, `ghp_ gho_ ghu_ ghs_ ghr_ github_pat_`, `AKIA`/`ASIA` + 16 upper-alnum, `AIza` + 35, `xox[baprs]-`, `glpat-`, `npm_`, `hf_`, `dop_v1_`, `SG.`, `ya29.` |
| `jwt` | `eyJ` + base64url `.` base64url `.` base64url |
| `credential_assignment` | `(api[_-]?key\|secret\|password\|passwd\|token\|access[_-]?key\|private[_-]?key\|credential)` followed by `=`/`:` and a non-trivial value |
| `url_userinfo` | `scheme://user:password@host` |
| `payment_card` | 13-19 digit run (separators tolerated) passing the Luhn check |
| `government_id` | US SSN shape `\d{3}-\d{2}-\d{4}` with a valid-area guard |
| `high_entropy` | Any whitespace token of length ≥ `NR_ENTROPY_MIN_TOKEN_LEN` whose charset is credential-like (base64/hex alphabet) and whose Shannon entropy ≥ `NR_ENTROPY_MIN_BITS`, **after** the structural exclusions below |

**Structural exclusions ahead of `high_entropy` (critique C1).** Before entropy
scoring, a token is skipped if it fully matches a known non-secret *shape*:
canonical git SHA (`^[0-9a-f]{7,40}$`) or canonical UUID
(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`, any case),
raw rendering only. These are structural shape rules, not corpus tuning — they
carry no evidence from a dataset and cannot drift. They are also an enumerated,
deliberate hole in the guarantee: a 40-hex-char secret is not caught by the
entropy backstop. Documented as such. Every other detector still applies to
these tokens (a git SHA in `password=<sha>` still trips `credential_assignment`).

**Adversarial normalization.** Every detector runs against *two* renderings of
the input: (a) the raw text, and (b) a de-whitespaced rendering with all
`\s` runs removed. (b) defeats "key split across whitespace/newlines", the
adversarial case named in the issue's acceptance criteria. Offsets are never
reported, so the rendering choice has no observable side effect beyond the
boolean.

**`high_entropy` false positives are expected and accepted.** Git SHAs, UUIDs,
and base64 blobs will be dropped. The issue states over-blocking is accepted;
that trade is the price of a zero-false-negative claim, and it is documented in
the user-facing docs so an adopter is never surprised.

### 2. The mixin

`NeverRecordMixin` — a plain model mixin, NOT a `WriteFilterMixin` subclass.

- `_check_never_record()` — scans every non-empty string-valued field on the
  instance (field-agnostic on purpose: a secret pasted into a metadata field is
  still a secret), **each field independently under both renderings**. On a hit,
  writes the tombstone, then raises `NeverRecordException`.

  **Key fields are excluded from the scanned set (critique C2).**
  `AutoKeyField`/`KeyField` values are generated UUID/hex shapes — precisely
  what the entropy detector targets — so a model could otherwise block on its
  own identity. Scan `self._meta.fields` minus `self._meta.key_field_names`,
  mirroring the key-field iteration the `KeyMutationError` guard already uses at
  `base.py:1203-1207`. A regression test asserts a generated key never trips a
  detector. (The structural UUID/SHA exclusion above makes this belt-and-braces,
  but the two protect against different failures: exclusions can be narrowed,
  key semantics cannot — a key is never user content.)

  **No cross-field concatenated pass (critique C3).** An earlier draft scanned
  the joined string of all fields. Dropped: with the de-whitespaced rendering,
  any join separator is either removed (so two innocuous adjacent values can
  merge into a credential-shaped or high-entropy token neither contains) or must
  be a normalization-surviving sentinel, which is just per-field scanning with
  extra steps. The issue names only *intra-value* splitting ("keys split across
  whitespace") as the adversarial case, and per-field dual rendering covers it.

  **The exception message is content-free (critique C4).** `NeverRecordException`
  carries only the reason code and detector name — never the matched text, an
  offset, or a length. This is load-bearing, not cosmetic: `_record_failure`
  writes `f"... {type(exc).__name__}: {exc}\n"` into a **plaintext log file**
  (`service.py:544-552`) and `subconscious_memory.py:380-381` logs the exception
  — a message quoting the matched span would defeat the entire guarantee through
  a side channel. Asserted by test.
- `never_record_counts()` (classmethod) → `{reason_code: count}` from
  `$NR:{ClassName}:counts`.
- `never_record_log(limit)` (classmethod) → most recent drop entries from
  `$NR:{ClassName}:drops`.
- `roundtrip_policy = "rebuild"` — the `$NR:` counters are audit telemetry, not
  record state; export/import carries nothing (matches `WriteFilterMixin`'s
  precedent).

### 3. The tombstone (content-free by construction)

Two keys, both plain Redis/Valkey types — no modules:

- `$NR:{ClassName}:counts` — HASH, `HINCRBY reason_code 1`.
- `$NR:{ClassName}:drops` — LIST, `LPUSH` of
  `{"id": "<uuid4>", "reason": "<code>", "detector": "<name>", "at": <ts>}`
  then `LTRIM 0 NR_TOMBSTONE_LOG_MAX-1`.

The id is `uuid4()` — **not** a hash or fingerprint of the content. A
content-derived fingerprint would be a confirmation oracle: an attacker holding
a candidate secret could hash it and check for a match. This is a deliberate
divergence from `memory_lifecycle.Tombstone`, which stores an
`ExistenceFilter` fingerprint precisely so writes can be matched against it.

No field of the tombstone contains, derives from, or is length-correlated with
the dropped text.

### 4. Default-on wiring and the kill switch

Per the repo's default-on doctrine, the firewall is ON wherever popoto owns the
model:

- `DefaultMemory` gains `NeverRecordMixin`. Every harness/MCP write and every
  quickstart adopter is gated with no code change on their side.
- `SubconsciousMemory.extract_memories()` gains a **turn-level** pre-check: it
  scans `response_text` *before* invoking the extraction provider. On a hit it
  writes one tombstone, returns `[]`, and never calls the provider — so on the
  `ClaudeExtractionProvider` path the secret is never even sent to the LLM API.
  This gate applies regardless of the caller's `model_class`, which is what
  makes the guarantee independent of "did someone remember the mixin".
- Custom ORM models opt in by adding the mixin. Popoto is a general-purpose
  Redis ORM; silently dropping a user's `Credential` model rows would be a
  defect, not a feature. The firewall covers popoto's memory surface, not every
  `Model` in the world. **This boundary is the plan's single most consequential
  judgment call — flagged for critique.**

### 4b. A privacy drop must not be reported as a harness outage (critique BLOCKER)

`MemoryService.capture()` treats an empty result from non-empty text as an
outage. Verified at `service.py:242-251`:

```python
if keys:
    self._touch("capture")
else:
    # Non-empty text always yields at least one fact, so reaching
    # here means the save was rejected or the server is gone.
    self._record_failure(
        "capture", RuntimeError("no record written for a non-empty turn")
    )
```

That premise ("non-empty text always yields at least one fact") is exactly what
this module invalidates. Left alone, **every off-the-record turn and every
credential paste would increment the `doctor` failure counter and append a line
to the plaintext failure log** — the same signal an operator uses to detect
"Redis is gone". A working firewall would look like a broken write path, and the
noise would be proportional to how well the firewall works.

So the plan's earlier claim of "no change to `service.py`" was wrong and is
retracted. The fix:

- `SubconsciousMemory.extract_memories()` records, per call, whether its empty
  return was caused by a privacy drop — a turn-level drop, or every candidate
  fact dropped at save level. Exposed as a read-only property
  `last_extraction_privacy_dropped`, reset at the top of every
  `extract_memories()` call so it always describes the immediately preceding
  call.
- `MemoryService.capture()` consults it before `_record_failure`. On a drop it
  returns `[]` **without** touching the failure counter or the log, because
  nothing failed. The event is already recorded in `$NR:{ClassName}:counts`,
  which is the correct counter for it.

This keeps the "silently stopped working" alarm intact for its real cause (a
rejected save or an unreachable server) while a deliberate drop stays out of the
failure channel. Asserted by test in both directions: a drop must not increment
`{COUNTER_KEY_PREFIX}:{agent_id}:capture` or write a log line, and a genuine
empty-result failure still must.

Kill switch (`Defaults.NEVER_RECORD_ENABLED`, read from env
`POPOTO_NEVER_RECORD_DISABLE` at import, assignable at runtime): a deploy-level
escape hatch for adopters who cannot edit model code, matching the
`DATETIME_KEY_LEGACY` precedent. Default is enabled.

### 5. Constants (pinned in `Defaults`)

```
NR_ENTROPY_MIN_TOKEN_LEN = 20    # shortest token considered for entropy
NR_ENTROPY_MIN_BITS      = 3.5   # Shannon bits/char threshold
NR_TOMBSTONE_LOG_MAX     = 1000  # capped LPUSH/LTRIM audit log length
NR_ASSIGNMENT_MIN_VALUE_LEN = 6  # shortest value after key=/key: that counts
NEVER_RECORD_ENABLED = <env-derived bool>   # kill switch, not a tuning constant
```

Pattern lists are module-level constants in `never_record.py`, not `Defaults`
entries — they are a security-relevant corpus, not a tuning dial, and exposing
them as a mutable registry would let a caller weaken the guarantee.

## Failure Path Test Strategy

Every failure path is asserted, not just the happy path:

1. **Nothing persists.** After a blocked `save()`, assert the model's Redis key
   does not exist, the class set has no member, and — the strong form — a
   `SCAN`/`KEYS` sweep of the test DB finds the secret substring in **no key and
   no value**. This is the acceptance criterion "never persists in any Redis
   key", tested literally rather than by proxy.
2. **Tombstone is content-free.** Assert the serialized tombstone contains no
   substring of the dropped text of length ≥ 4, and that the id is not
   reproducible from the content (two drops of identical text yield different
   ids).
3. **The swallow-handler path.** Drive a drop through
   `SubconsciousMemory.extract_memories` and assert the record count is
   unchanged, `saved == []`, and no `logger.warning` was emitted — proving the
   gate signalled cleanly via `SkipSaveException` and did not fall into the
   broad `except Exception` at `subconscious_memory.py:376-378`.
4. **Off-the-record voids the whole turn.** A turn where sentence 1 says
   "off the record" and sentences 2-4 are innocuous facts must produce zero
   records — not three.
5. **Provider never sees it.** With a recording fake extraction provider,
   assert `provider.extract` was never called for an off-the-record turn.
6. **Kill switch.** With `Defaults.NEVER_RECORD_ENABLED = False`, a
   credential-bearing save succeeds — proving the escape hatch is real.
7. **Adversarial.** Key split across whitespace/newlines; key split across a
   line continuation; high-entropy token embedded mid-sentence; base64 blob;
   Luhn-valid vs Luhn-invalid 16-digit runs (the latter must NOT be dropped, so
   the payment detector is not just "any 16 digits").
8. **No false-positive on ordinary prose.** A corpus of ordinary memory
   sentences must pass ungated — a firewall that blocks everything would pass
   criteria 1-7 trivially. Includes a bare git SHA and a bare UUID in prose
   (the structural exclusions), and a record whose generated `AutoKeyField`
   value is UUID-shaped (the key-field exclusion).
9. **A drop is not an outage.** After a dropped capture, assert
   `{COUNTER_KEY_PREFIX}:{agent_id}:capture` did **not** increment and no line
   was appended to `config.log_path`; and, in the other direction, that a
   genuine non-privacy empty result still records the failure.
10. **No content in the exception message.** Assert the dropped secret, and any
    ≥4-char substring of it, is absent from `str(exc)` — the plaintext-log side
    channel described in §2.

## Test Impact

New file: `tests/test_never_record_firewall.py`. No existing test is expected to
change — `DefaultMemory` gains the mixin, but existing `DefaultMemory` tests use
ordinary prose content. **If any existing test breaks, that is signal, not
noise, and must be reported rather than patched around** (it would mean the
detectors fire on ordinary content).

Redis DB: tests run under the `popoto.pytest_plugin` auto-isolation on DB 15.
No test may touch DB 0.

## Rabbit Holes

- **Do not build a redaction/masking engine.** The gate is a boolean drop, not a
  transform. Partial redaction requires span-accurate detection, which is
  exactly the property a zero-false-negative design refuses to depend on.
- **Do not tune entropy against a corpus.** Pick defensible thresholds, pin
  them, accept over-blocking. A sweep is a separate issue.
- **Do not add an LLM voter.** The issue permits one *later*, add-only. Out of
  scope here; adding it now would blur where the guarantee comes from.
- **Do not attempt to gate `pipeline`-batched writes at the pipeline level.**
  The per-instance `save()` gate already runs before commands are queued.

## Risks

| Risk | Mitigation |
|---|---|
| **Over-blocking eats real memories.** `high_entropy` will drop git SHAs and UUIDs. | Documented explicitly; per-reason drop counters make it measurable in production; kill switch exists. Accepted by the issue. |
| **Behavior change for existing `DefaultMemory` adopters** — saves now return `False`. | CHANGELOG entry, docs page, kill switch. `save()` already had a documented `False` return path (WriteFilter), so the return contract is unchanged. |
| **Latency on the per-turn hook path.** The read hook has a 400 ms budget (p95 measured 200 ms). The gate is on the *write* path, but raw-turn text can be long. | Regexes are precompiled at import; scanning is a single linear pass. Measure scan time on a 10 KB turn and report it; if it exceeds ~5 ms, report rather than silently accept. |
| **A missed credential format is a silent false negative** — the failure mode the whole module exists to prevent. | The detector list is explicit and versioned in one module; `high_entropy` is the catch-all backstop for unknown-prefix tokens. M9's seeded audit is the ongoing measurement. |
| **Concurrent lanes share Redis DB 15**, so a mass failure may be contention, not regression. | Compare against base in the same worktree before diagnosing, per repo doctrine. |

## Race Conditions

None material. The gate is synchronous, in-process, and reads only instance
state already materialized before `save()`. Tombstone writes are `HINCRBY` /
`LPUSH` + `LTRIM` — the `LTRIM` may transiently leave the list one over the cap
under concurrency, which is cosmetic. No compare-and-set, no cross-process
ordering dependency, no Lua needed.

## No-Gos (Out of Scope)

- **Not an extension of `WriteFilterMixin`.** The issue's recon rejected it and
  this plan honors that: `compute_filter_score()` is a single per-class slot
  already claimed by salience scoring in the documented pattern, and a scalar
  score is the wrong shape for a boolean content predicate.
- **No quarantined side-log of dropped content.** The issue dropped this
  explicitly: an encrypted store of every secret the firewall ever saw is a
  breach surface wearing an audit label. Content-free tombstones plus M9's
  seeded audit measure false negatives without retaining anything.
- **No LLM voter** (M3's concern, add-only when it arrives).
- **No gating of arbitrary user models by default.**
- **No changes to `memory_lifecycle.Tombstone`** — different object, different
  namespace, deliberately different content policy.
- **No dependency on #580 / PR #582.**
- **No merge.** PR opens for review; merge is the PM's call.

## Update System

Not applicable — no scheduled job, migration, or background updater.

## Agent Integration

The harness (`popoto.integrations`) picks the gate up through `DefaultMemory` +
`SubconsciousMemory` with no change to `hooks.py` or `mcp_server.py` — but
`service.py` **does** change, see §4b: `capture()`'s empty-result branch must
learn to distinguish a privacy drop from an outage, or the firewall's successes
are logged as failures. The MCP `memory_save` tool (`mcp_server.py:234` →
`service.capture(...)`) returns its normal "saved nothing" shape when a payload
is dropped; the drop is visible in `$NR:DefaultMemory:counts` and absent from
the `doctor` failure counters.

## Documentation

- New page `docs/agent-memory/never-record-firewall.md`: what is guaranteed,
  what is explicitly NOT guaranteed (this is the honesty-critical part —
  over-blocking, and the fact that a novel credential format with low entropy
  and no known prefix could slip), the reason codes, how to read the counters,
  how to disable.
- `mkdocs.yml` nav entry.
- `CHANGELOG.md` entry flagging the `DefaultMemory` behavior change.
- Docstring in `never_record.py` stating the guarantee and its boundary.
- `/do-docs` runs before any merge request (repo requirement).

## Success Criteria

- [ ] A save carrying a credential-shaped string leaves **no trace** in Redis —
      verified by a full-keyspace substring sweep of the isolated test DB, not
      by checking the model key alone.
- [ ] An off-the-record-marked turn produces **zero** records, and the
      extraction provider is never invoked.
- [ ] Every drop increments a per-reason counter and appends a tombstone whose
      serialization shares no ≥4-char substring with the dropped text.
- [ ] The gate runs before `WriteFilterMixin._check_write_filter()` and before
      `pre_save()` — asserted by test, not by reading.
- [ ] Ordinary prose saves unaffected (no-false-positive corpus passes).
- [ ] Kill switch demonstrably restores pre-change behavior.
- [ ] `ruff check src/` exits 0; mypy delta 0 vs `e220b2e` measured in the same
      environment (python + redis-py version stated).
- [ ] CI green on both the Redis and Valkey service-container jobs.

## Team Orchestration

Single lane. The module is small and tightly coupled (scanner ↔ mixin ↔ seam ↔
tests); splitting it across builders would cost more in interface churn than it
saves. Fan-out is used for the independent review pass only.

## Step by Step Tasks

1. **Constants.** Add `NR_*` tuning constants and the `NEVER_RECORD_ENABLED`
   kill switch (with `_read_*_switch()` env reader, mirroring
   `DATETIME_KEY_LEGACY`) to `src/popoto/fields/constants.py`.
2. **Scanner.** Create `src/popoto/privacy/__init__.py` and
   `src/popoto/privacy/never_record.py`: `NeverRecordVerdict`, precompiled
   pattern corpus, Shannon-entropy helper, Luhn helper, dual-rendering
   `scan_never_record(text)`.
3. **Exception.** Add `NeverRecordException(SkipSaveException)` to
   `src/popoto/exceptions.py` with a docstring explaining why it subclasses
   (existing handlers keep working).
4. **Tombstone writer.** Implement `$NR:{ClassName}:counts` HINCRBY and the
   capped `$NR:{ClassName}:drops` LIST, plus `never_record_counts()` /
   `never_record_log()` readers. uuid4 ids only — assert in code review that no
   content-derived value is written.
5. **Mixin.** Implement `NeverRecordMixin` with `_check_never_record()` scanning
   all string-valued **non-key** fields, each independently under both
   renderings. No cross-field concatenated pass. Exception message carries
   reason code + detector only.
6. **Seam.** Insert the guarded block in `Model.save()` immediately before the
   `WriteFilterMixin` block (currently `base.py:1217`), honoring
   `Defaults.NEVER_RECORD_ENABLED`, catching `NeverRecordException` and
   returning `pipeline if pipeline else False`.
7. **Default-on wiring.** *(Provisional on Open Question #1 — see its blast
   radius note.)* Add `NeverRecordMixin` to `DefaultMemory`'s bases; update its
   "Deliberately not included" docstring section to explain why the firewall
   *is* included where `WriteFilterMixin` is not.
8. **Turn-level gate.** Add the `response_text` pre-check at the top of
   `SubconsciousMemory.extract_memories()` (before `self._extractor.extract`),
   writing one tombstone and returning `[]`. Track
   `last_extraction_privacy_dropped` across both the turn-level and
   all-facts-dropped cases.
8b. **Harness failure-channel fix (blocker).** Guard the `else` branch at
   `service.py:242-251` on `last_extraction_privacy_dropped` so a deliberate
   drop does not call `_record_failure`.
9. **Exports.** Surface `NeverRecordMixin` and `scan_never_record` from
   `popoto/__init__.py` (and `popoto.privacy`).
10. **Tests.** Write `tests/test_never_record_firewall.py` covering all eight
    Failure Path Test Strategy items.
11. **Docs.** New docs page, mkdocs nav, CHANGELOG, then `/do-docs`.
12. **Verification.** Run the gates in the Verification section; record
    environment alongside every count.

## Verification

Run from the session worktree `.worktrees/never_record_firewall` with its own
venv (`.[dev,embeddings,benchmark,mcp]` — **not** `dataframe`, which breaks
`test_dataframe_field.py` collection). The `mcp` extra is required, not
optional: omitting it silently deselects the MCP server tests (CLAUDE.md
worktree pitfall #2), and the MCP write path is the surface the blocker fix
touches. Confirm the editable install resolves to this checkout first:

```bash
.venv/bin/python -c "import popoto; print(popoto.__file__)"   # must be THIS worktree
.venv/bin/ruff check src/
.venv/bin/pytest tests/test_never_record_firewall.py -q
.venv/bin/pytest tests/test_default_memory.py tests/test_subconscious_memory.py \
                 tests/test_write_filter.py tests/test_models.py \
                 tests/test_integrations_service.py tests/test_mcp_server.py -q
.venv/bin/mypy src/            # compare against e220b2e in the SAME venv
```

Baseline measured at `e220b2e` in this venv (Python 3.12.13, redis-py 8.1.0,
Redis DB 15): **mypy 1110 errors in 65 files**, `ruff check src/` clean.
The branch must not move either number.

Narrow-scope tests only — a full-suite run from a worktree collides with
concurrent lanes on Redis DB 15. mypy is measured base-vs-branch in one
environment because the delta is redis-py-version-dependent (7.x flags sites
8.x narrows).

## Critique Results

`/do-plan-critique` run against `e220b2e`, FULL depth, three critics (Risk &
Robustness, Scope & Value, History & Consistency). Verdict: **NEEDS REVISION**
— 1 blocker, 5 concerns, 1 nit. All seven addressed in this revision:

| # | Finding | Resolution |
|---|---|---|
| BLOCKER | Every privacy drop logged as a harness failure by `capture()` (`service.py:242-251`) | Verified against the code and confirmed. New §4b + task 8b: `last_extraction_privacy_dropped` signal guards the `_record_failure` branch. The "no change to `service.py`" claim is retracted in Agent Integration. |
| C1 | `high_entropy` ships default-on dropping git SHAs and UUIDs | Structural shape exclusions (git SHA, canonical UUID) added ahead of the entropy detector, documented as a deliberate enumerated hole. |
| C2 | Key/auto-key fields in the scanned set could block a model on its own identity | Key fields excluded from the scanned set; regression test added to Failure Path item 8. |
| C3 | Cross-field concatenation scan had an unspecified join → boundary false positives | Pass removed entirely, with the reasoning recorded in §2. |
| C4 | `NeverRecordException` message could leak content into the plaintext failure log | Message pinned to reason code + detector; asserted by Failure Path item 10. |
| C5 | Verification omitted the `mcp` extra, silently deselecting MCP tests | Install list now `.[dev,embeddings,benchmark,mcp]`; MCP + service test modules added to the narrow-scope list. |
| C6 | Open Question #1 unresolved while tasks assumed it settled | Task 7 marked provisional with its two-artifact blast radius named; OQ #1 kept open for the PM. |
| Nit | Freshness Check line drift (`subconscious_memory.py:380`, `base.py:1438`) | Corrected in the Freshness Check table. |

Critic result files: `scratchpad/critique_561/`. No `docs/sdlc/do-plan-critique.md`
exists in this repo, so the critique ran on the generic path and recorded no
verdict to any substrate — this table is the record.

## Open Questions

Both remaining questions are decisions for the PM/principal, not blockers on
the build. The build proceeds on the stated default; each is cheap to reverse.

1. **Scope of default-on (the load-bearing decision).** This plan gates
   `DefaultMemory` and the `SubconsciousMemory` turn path by default, and leaves
   arbitrary user models opt-in via the mixin. The issue's recon argues "a gate
   that only fires when someone remembered a mixin is not a firewall." The plan
   answers that for popoto's memory surface but not for the general ORM. The
   alternative — gate every `Model.save()` in popoto — would silently break any
   adopter legitimately storing credentials in Redis, which for a general-purpose
   ORM is a defect rather than a feature.
   **Blast radius of a "no" answer: exactly two artifacts** — `NeverRecordMixin`
   in `DefaultMemory`'s bases (task 7) and the behavior-change CHANGELOG entry.
   Everything else (scanner, mixin, seam, turn-level gate, tombstones) is
   unaffected.
2. **`high_entropy` structural exclusions.** Per critique C1 the build excludes
   canonical git SHAs and UUIDs from entropy scoring. This is an enumerated,
   deliberate hole: a 40-hex-char secret with no known prefix is not caught by
   the entropy backstop (it is still caught by `credential_assignment` if it
   appears as `password=<sha>`). The alternative is dropping every commit SHA a
   developer's memory ever mentions. Confirm the trade.
3. **Plan-doc location.** Repo convention commits plan docs to `main`. This lane
   runs in an isolated worktree with concurrent lanes active on the shared
   checkout, so the plan is carried on the session branch and lands with the
   implementation PR instead. Confirm this is acceptable or say whether the plan
   should be pushed to `main` separately.
