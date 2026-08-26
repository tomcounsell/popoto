# Never-Record Firewall

A deterministic gate that runs before anything is written, and drops content
that must never become a memory: credential-shaped strings, explicitly
off-the-record turns, and a short list of enumerated sensitive categories.

It exists because the rest of the memory stack cannot make this promise. The
harness ([hook integration](harness-integration.md)) fires on every turn and
stores raw turn text, and terminal turns contain pasted API keys. The LLM
extraction path can be *asked* to skip secrets, but a prompt is an
instruction, not a guarantee. So the firewall is regex and entropy — no
model, no network, no judgment call. An LLM voter may be added later and may
only ever *add* drops; the guarantee below rests solely on the deterministic
core.

```python
from popoto import NeverRecordMixin, Model
from popoto.fields.shortcuts import AutoKeyField, KeyField, StringField

class Memory(NeverRecordMixin, Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")

Memory(agent_id="a1", content="my key is sk-ant-api03-AAAA...").save()
# False -- nothing written to Redis

Memory(agent_id="a1", content="The user prefers dark mode.").save()
# saved normally
```

`DefaultMemory` already carries the mixin, so the batteries-included path and
the Claude Code / Codex / Hermes / OpenClaw harness are gated with no code
change on your side.

## The guaranteed class

Content matching any of these never reaches Redis. Each drop records one
reason code.

| Reason code | What it catches |
|---|---|
| `off_the_record` | An explicit marker — "off the record", "do not record", "don't remember this", `<!-- no-memory -->`, `[[private]]`. Voids the **entire turn**, not a guessed span |
| `private_key_block` | PEM, OpenSSH, and PuTTY private key headers |
| `credential_prefix` | Vendor-prefixed tokens: Anthropic, OpenAI, GitHub, AWS, Google, Slack, GitLab, npm, HuggingFace, DigitalOcean, SendGrid, Stripe |
| `jwt` | Three-segment base64url JSON Web Tokens |
| `credential_assignment` | `password=`, `api_key:`, `token=` and friends followed by a real value |
| `url_userinfo` | `scheme://user:password@host` |
| `payment_card` | 13-19 digit runs that pass the Luhn checksum — not merely "any 16 digits" |
| `government_id` | US SSN-shaped strings, with area/group guards for never-issued ranges |
| `high_entropy` | The backstop for unknown-prefix tokens: long, credential-charset, high-Shannon-entropy strings |

Every pattern-based detector runs against two renderings of the input: the
raw text, and the text with all whitespace removed. A key split across a line
break is still caught.

Documentation placeholders are deliberately *not* treated as secrets —
`password=<your password here>`, `api_key = YOUR_API_KEY`, and
`export TOKEN=${GITHUB_TOKEN}` all store fine. A gate that eats the
quickstart's own examples teaches adopters to disable it.

## What this does NOT guarantee

This is a pattern gate, not an oracle. The holes are enumerated on purpose.

**Over-blocking is accepted and expected.** Long base64 blobs and random
identifiers in ordinary prose will be dropped. That is the deliberate price
of aiming at zero false negatives on the class above. If a memory you wanted
disappeared, check `never_record_counts()` — the drop is recorded even though
the content is not.

**The entropy backstop is deliberately narrowed, in three ways.** Shannon
entropy per character does not separate secrets from technical English at
these lengths: `conversational-adjacency` scores 3.69 bits/char and
`ExtractionProviderRegistry` 3.87, both over the 3.5 threshold. Measured
against this repo's own documentation, scoring whole tokens blocked 11.6% of
paragraphs over 200 characters — and at turn level one such token voids the
entire turn. Three structural cuts apply before scoring:

- *Canonical git SHAs and UUIDs are not scored.* Developer memory mentions
  commit SHAs constantly.
- *Tokens with no digit anywhere are not scored.* Encoded secrets draw from
  an alphabet containing digits; hyphenated compounds and CamelCase
  identifiers do not.
- *Only the longest separator-free run is scored, and it must clear the
  length threshold on its own.* Entropy is a property of a contiguous
  encoded run, not of a phrase. This is what separates
  `text-embedding-3-small` and `POPOTO_MEMORY_MAX_ITEMS=10` from
  `Zq7Z1kXpLm4TvB8NwR2yHc6JdFgA9sQe`.

Together these take the measured false-positive rate on that corpus (5282
paragraphs, including this repo's plan documents) to zero `high_entropy`
blocks, at 0.51% of paragraphs blocked overall — all by the named detectors,
mostly documentation that literally discusses off-the-record markers.

**These cuts cost the backstop a lot, and the number is worth knowing.** A
secret escapes `high_entropy` if it is a bare 40-hex-character string, if it
contains no digit at all, or if a separator (`-` `_` `/` `=` `+`) breaks it
so that no unbroken run reaches 20 characters. The separator case dominates,
because base64's own alphabet includes `-` and `_`. Measured over 200,000
random base64url tokens per length:

| Token length | Escapes the entropy backstop |
|---|---|
| 20 characters | **~49%** |
| 32 characters | **~27%** |
| 43 characters (256-bit) | **~12%** |

So the entropy backstop should be read as *opportunistic*, not as a second
guarantee. **No cut touches any other detector** — the same secret is still
caught as `password=<value>`, behind a vendor prefix, in a URL, or in any
other named shape, and those named detectors are where the guarantee lives.
The backstop exists to catch unknown formats sometimes; it is not a promise
that an unknown format will be caught.

**A novel credential format can pass** if it has no recognized prefix, no
assignment context, and entropy below the threshold. The detector corpus
lives in one module so it can be extended.

**Semantic sensitivity is out of scope.** The gate matches shape, not
meaning. It will not notice that a sentence is confidential because of what
it says.

**An unknown-prefix secret split across whitespace escapes the entropy
backstop.** Entropy is scored on the raw rendering only — on the
de-whitespaced rendering an ordinary paragraph collapses into one long
high-entropy token, so scoring it there blocked ordinary sentences. Split
*known* formats are still caught, because every vendor-prefix, JWT, and PEM
pattern does run against both renderings.

## Where the gate runs

Inside `Model.save()`, before the `WriteFilterMixin` check and before
`pre_save()` — therefore before serialization, the HSET, and every secondary
index, BM25 posting, embedding call, and co-occurrence edge. Ordering is the
whole guarantee: blocked content is never handed to any of those.

A blocked save returns `False`, the same contract `WriteFilterMixin` already
uses, and raises nothing at the call site. Internally it signals with
`NeverRecordException`, a subclass of `SkipSaveException`, so existing
handlers that already swallow a filtered save keep working unchanged.

The exception message carries only a reason code and detector name. It never
quotes the matched text, an offset, or a length — exception messages reach
plaintext log files, and a quoted match would leak through a side channel
that the tombstone design closes.

## What the gate scans

The mixin scans the instance's **non-key `str` field values**, and only those:

- **Key fields are excluded** — `KeyField` and `AutoKeyField` values are
  rendered into the Redis key *name*, which a later drop cannot retract. The
  scan skips them because blocking at `save()` would not unwrite a name that
  earlier records already embedded; a key field carrying secrets is a schema
  problem, not a save-time one.
- **Only `str` values are scanned.** A list-valued field — a `TagField`, say —
  is never seen by the mixin's scan, and neither are numeric or boolean
  fields. A model whose sensitive content can land in a non-`str` field must
  scan it itself before calling `save()`.

### Narrowing the surface on one model

`_never_record_scan_values()` is overridable, and the field-name iteration is
split into `_never_record_scan_field_names()` so an override can drop specific
fields **by name** without re-deriving the key-field rule:

```python
class JournalEntry(AppendOnlyMixin, NeverRecordMixin, EventStreamMixin, Model):
    _MACHINE_GENERATED_FIELDS = frozenset({"target"})

    def _never_record_scan_values(self):
        skip = self._MACHINE_GENERATED_FIELDS
        for field_name in self._never_record_scan_field_names():
            if field_name in skip:
                continue
            value = getattr(self, field_name, None)
            if isinstance(value, str) and value.strip():
                yield value
```

This exists for **machine-generated pointers**, not to soften the gate on
anything a human or a model wrote. `JournalEntry.target` holds a Redis key
Popoto itself generated (`JournalEntry:{agent_id}:{uuid4 hex}`), and scanning
it for payment cards is a category error with a measured cost: a uuid4 hex
sometimes contains a 13–19 digit run that passes the Luhn checksum, so the
firewall blocked the annotation as `reason='payment_card'`,
`detector='luhn'` — 5–8 blocked per 2000 random keys (~0.25–0.4%). Because the
block lands at `save()` step 0, which *returns* rather than raises, the
annotation was silently dropped.

Narrowing the mixin's surface moves the obligation, it does not remove it. A
model that excludes a field is responsible for anything the mixin can no
longer see: `ProvenanceJournal`'s pre-flight separately scans `agent_id` (a
`KeyField`, structurally invisible to the scan) and each `subjects` tag
individually, then reuses `entry._never_record_scan_values()` for the rest
rather than re-listing the fields — a hand-maintained list is what let `target`
go unscanned in the first place.

## Auditing drops

Every drop leaves a **content-free tombstone**: a random id and a reason
code, and nothing else.

```python
Memory.never_record_counts()
# {'credential_prefix': 3, 'off_the_record': 1}

Memory.never_record_log(limit=2)
# [{'id': 'c9f0...', 'reason': 'off_the_record',
#   'detector': 'marker_prose', 'at': 1786943286.478}, ...]
```

The id is a `uuid4`, deliberately **not** a hash or fingerprint of the
content. A content-derived id would be a confirmation oracle: anyone holding
a candidate secret could reproduce the id and check the log for a match. This
is the precise point where a never-record tombstone diverges from
[`MemoryLifecycle`](../recipes.md)'s `Tombstone`, which stores a fingerprint
on purpose so writes can be matched against it.

### Architecture

| Key | Type | Contents |
|---|---|---|
| `$NR:{ClassName}:counts` | HASH | `reason_code -> count`, via `HINCRBY`. Unbounded and authoritative |
| `$NR:{ClassName}:drops` | LIST | Recent tombstones, `LPUSH` + `LTRIM` capped at `NR_TOMBSTONE_LOG_MAX` |

Both are plain Redis types. No Redis modules are used, so the firewall
behaves identically on Redis and Valkey. The namespace is disjoint from
`$WF:` (write filter) and `$TOMB:` (lifecycle tombstones).

Tombstone writes are best-effort: if Redis rejects the audit write, the drop
is still enforced. A firewall that failed open when its log was unwritable
would fail in the one direction it must never fail in.

## With SubconsciousMemory

`SubconsciousMemory.extract_memories()` runs the same scan at the **turn**
level, before the extraction provider is invoked. Two consequences:

- An off-the-record marker voids the whole turn, including facts derived from
  adjacent sentences. A per-record gate cannot do that, because the marker
  may sit in a sentence that produced no fact.
- On the `ClaudeExtractionProvider` path, blocked text is never sent to the
  API.

The turn-level gate applies regardless of which `model_class` you configured,
so this half of the guarantee does not depend on anyone remembering to add
the mixin.

Passing `auditable_extraction=` (see [Auditable Extraction](auditable-extraction.md))
adds a **second**, per-candidate scan on top of this turn-level one: every
candidate span is scanned again, individually, before the LLM verdict call
ever sees it. A turn-level block still produces one `firewall_drop` row
(`turn_level_block`); a per-candidate block produces its own
(`pre_llm_candidate_block`). Both reason codes map to the same `state`, but
the auditable path is the only one that logs either as a queryable row
instead of a `logger.warning`.

`last_extraction_privacy_dropped` reports whether the last call returned
empty because of a drop rather than a failure. `MemoryService.capture()`
reads it so a deliberate drop is not written to the harness failure log — an
empty result from non-empty text otherwise reads as "the write path has
silently stopped working", and without the flag every credential paste would
look like an outage.

## Configuration

`POPOTO_NEVER_RECORD_DISABLE=1` disables the firewall entirely, including the
turn-level gate. It is read at import and exists because a PyPI adopter
cannot always edit model code to remove a mixin. Unset means on, per Popoto's
default-on doctrine.

Equivalently, at runtime:

```python
from popoto.fields.constants import Defaults
Defaults.NEVER_RECORD_ENABLED = False
```

The numeric constants live in `Defaults` and are pinned, not constructor
kwargs. **None of them are sweep-backed** — there is no benchmark evidence
for these values, unlike most constants in
[Tuning Magic Numbers](../guides/tuning-magic-numbers.md). They are
deliberately conservative in the over-blocking direction.

| Constant | Value | Meaning |
|---|---|---|
| `NR_ENTROPY_MIN_TOKEN_LEN` | `20` | Shortest token the entropy backstop considers |
| `NR_ENTROPY_MIN_BITS` | `3.5` | Shannon bits/char threshold. Random base64 sits near 5.5-6.0, random hex near 4.0, English text over the same alphabet well below 3.5 |
| `NR_ASSIGNMENT_MIN_VALUE_LEN` | `6` | Shortest value after `password=` that counts |
| `NR_TOMBSTONE_LOG_MAX` | `1000` | Cap on the drop-log LIST |
| `NEVER_RECORD_ENABLED` | env-derived | Kill switch, not a tuning constant |

## Using the scanner directly

`scan_never_record()` is pure — no Redis, no model, no clock — so it can gate
a pipeline before anything is constructed.

```python
from popoto import scan_never_record

verdict = scan_never_record("my key is sk-ant-api03-AAAA...")
verdict.blocked    # True
verdict.reason     # 'credential_prefix'
verdict.detector   # 'vendor_token'
```

## See Also

- [NeverRecordMixin reference](../fields.md#neverrecordmixin)
- [Agent Memory](agent-memory.md)
- [Harness Integration](harness-integration.md) — the write path this protects
- [Memory Telemetry](memory-telemetry.md) — the read-path privacy default
- [Auditable Extraction](auditable-extraction.md) — the opt-in path that
  scans every candidate through this firewall a second time, individually,
  and logs a queryable row for each of the two `firewall_drop` reason codes
