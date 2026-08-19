# ValidityField and SupersessionProtocol

`ValidityField` gives popoto models a validity axis — a way to say *this record
stopped being true, since when, and what replaced it* — that is orthogonal to
everything else in the ORM. [`DecayingSortedField`](decaying-sorted-field.md)
and [`ConfidenceField`](confidence-field.md) answer "how important is this
memory, and how sure am I?"; `ValidityField` answers the prior question, "is
this memory still a member of the corpus at all?" `SupersessionProtocol` is
the write-side vocabulary on top of it: "this new claim replaces whatever was
previously believed about `(subject, predicate)`." Both feed
[`ContextAssembler`](context-assembler.md), which excludes superseded records
from default retrieval the moment they close, while `filter(validity__as_of=t)`
keeps them fully queryable for historical replay.

## Overview

An agent learns "user is on the free plan." Two weeks later it learns "user
upgraded to enterprise." Without a validity axis the stale fact keeps its
place in every index and only loses ground gradually, through
[decay](decaying-sorted-field.md) — so `ContextAssembler` can still pack it
into context ahead of the correction. `ValidityField` makes the first fact
stop being a *member* of default retrieval the instant it is closed.

The core split:

- **Validity decides membership.** A record is either a candidate for default
  retrieval or it isn't — closed, or not yet started.
- **Decay decides ordering among the valid.** Once membership is settled,
  [`DecayingSortedField`](decaying-sorted-field.md) and friends rank what
  remains.

The two axes compose because neither knows the other's constants: a
`ValidityField` never appears in a decay formula, and a decay rate never
gates membership.

```python
from popoto import Model, KeyField, ValidityField, SupersessionProtocol

class Fact(Model):
    fact_id = KeyField()
    validity = ValidityField()

identity = SupersessionProtocol.identity_key("user_42", "subscription_plan")

old = Fact(fact_id="free").save()
SupersessionProtocol.supersede(old, identity_key=identity)      # -> None (first claim, opens only)

new = Fact(fact_id="enterprise").save()
SupersessionProtocol.supersede(new, identity_key=identity)      # -> old's redis_key (closed it)

Fact.query.filter(validity__current=True)        # -> [new]
Fact.query.filter(validity__as_of=two_weeks_ago)  # -> [old]

SupersessionProtocol.chain(new)  # -> [old, new], oldest first
```

## Keyspace

`ValidityField` owns six Redis keys per model/field, all under the
`$ValidityF:{Model}:{field}` prefix. No bytes are written into the model's own
hash — chain links live in derived state so an append-only journal can adopt
the field unchanged.

| Key | Type | Contents |
|---|---|---|
| `$ValidityF:{Model}:{field}:valid_from` | ZSET | member = record redis_key, score = valid-from epoch |
| `$ValidityF:{Model}:{field}:invalid_at` | ZSET | member = record redis_key, score = close epoch, `+inf` when open |
| `$ValidityF:{Model}:{field}:ingested_at` | ZSET | member = record redis_key, score = ingest (transaction-time) epoch |
| `$ValidityF:{Model}:{field}:chain:fwd` | HASH | old redis_key → superseding redis_key |
| `$ValidityF:{Model}:{field}:chain:rev` | HASH | new redis_key → superseded redis_key |
| `$ValidityF:{Model}:{field}:open:{digest}` | STRING | identity digest → currently-open record's redis_key |

`+inf` is the open-interval sentinel stored as `invalid_at`'s score for any
record still believed true. It is native to both Redis and Valkey sorted
sets — `ZADD` stores it, `ZSCORE` returns the string `"inf"`, `ZRANGEBYSCORE
"(t" "+inf"` includes it, and Lua 5.1's `tonumber()` parses it via `strtod` —
so no read path needs special-case handling for an open record.

An as-of-`t` membership test is `valid_from <= t AND invalid_at > t`: two
`ZRANGEBYSCORE`s intersected client-side, or two `ZSCORE`s inside Lua.

## Export & import

`ValidityField` declares `roundtrip_policy = "carry"` (see
[Writing Custom Fields](../field-authoring.md) for the protocol, and
[Export & Import](../guides/export-import.md) for the user-facing guide). All
six derived keys above survive a `popoto.transfer` round trip:

| Carried | How |
|---|---|
| `valid_from`, `invalid_at`, `ingested_at` scores | exported per record, re-`ZADD`ed after `save()` |
| `chain:fwd` / `chain:rev` links for the record | exported per record, re-`HSET` |
| `open:{digest}` pointers naming the record | digests exported per record, re-`SET` |

This is not optional bookkeeping. Because no validity byte lives in the model
hash, `"rebuild"` — the `Field` default — would give every imported record a
brand-new *open* interval, and since all three gating layers are subtractive, a
record with no closure is fully retrievable. An export/import would therefore
silently resurrect every superseded record. Carrying the `open:{digest}`
pointer matters for the same reason one step later:
`SupersessionProtocol.supersede(new, identity_key=...)` resolves the incumbent
*only* through that pointer, so a round trip that dropped it would leave the
identity's next supersession closing nothing while repointing at the newcomer,
orphaning the incumbent open forever.

Cost: the digest is opaque and there is no record → identity reverse lookup, so
export `SCAN`s `{prefix}:open:*` once per record to find the pointers aimed at
it. Export is an admin-path operation and that cost is accepted deliberately —
the same trade `on_delete` already makes. Save and read paths are untouched.

Restore ordering does not matter: `import_state` runs *after* `save()` and
writes plain `ZADD`/`HSET`/`SET` (never the `NX` forms `on_save` uses, which
would no-op against the fresh interval `on_save` just seeded), and a chain link
whose counterpart has not landed yet reads as a chain end until it does.

## `ValidityField` API

```python
class Fact(Model):
    fact_id = KeyField()
    validity = ValidityField()
```

A plain `Field`, deliberately **not** a `SortedFieldMixin` — this is
load-bearing, not an oversight. `SortedFieldMixin` fields can win a query's
ordering; validity must never do that, since it decides membership, not
priority. As a plain field it also stays out of the reindex/migration loops
that iterate sorted fields.

`ValidityField.on_save` opens an interval automatically at save time using the
field's value as `valid_from` (or save time, if unset). `ValidityField.on_delete`
removes every trace of the record from the six keys — records are normally
*closed*, not deleted, so this only matters for an explicit `delete()`.

### Query filters

| Filter | Semantics |
|---|---|
| `{field}__current=True` | Records whose interval covers *now* |
| `{field}__current=False` | The **literal complement** of `current=True`: closed records AND records that have not yet started |
| `{field}__as_of=t` | Records whose interval covers epoch `t` |

```python
Fact.query.filter(validity__current=True)
Fact.query.filter(validity__current=False)
Fact.query.filter(validity__as_of=1755000000.0)
```

!!! warning "A record with no interval is returned by neither `current=True` nor `current=False`"
    `{field}__current` and `{field}__as_of` are *deliberate, positive* queries
    over `valid_from`/`invalid_at` membership. A record that has no entry in
    either ZSET — because a `ValidityField` was added to a model after that
    record was written, and the record has not been re-saved since — makes no
    claim about its own validity, so it does not satisfy `current=True` (it
    isn't provably valid) and it does not satisfy `current=False` either (that
    filter's complement is computed over the *union of every member with an
    interval entry*, which excludes it too). This is exactly why these filters
    are deliberate queries and not what gates default retrieval — see
    "The exclusion rule" below.

Because these are `filter()` params, using them consumes a filter slot and
therefore **disables sorted-range limit pushdown** on that query. That is
expected, and it is precisely why the default retrieval path (decay Lua,
composite mask, assembler post-filter) gates server-side instead of by
appending a filter kwarg: `filter(limit=N, order_by=<sorted field>)` pushdown
stays active with validity gating enabled, because gating is never a filter
param on the default path.

## The exclusion rule

**All gating in this feature is subtractive.** Every layer — the decay-Lua
gate, the composite mask, and the assembler post-filter — asks "is this
record *provably* closed or *provably* not yet started?" and drops it only on
a yes. A record with no entry in either interval ZSET is **unmanaged** and
stays fully retrievable everywhere.

This is what makes adding a `ValidityField` to an existing model safe. Every
record written before the field existed has no `valid_from`/`invalid_at`
entry until it is next saved (or explicitly supersedes/is superseded). Under
a subtractive rule those records keep showing up in retrieval exactly as
before. Under an *inclusive* rule (a whitelist of provably-valid keys) they
would all silently vanish the moment gating turned on — a data-visibility
regression dressed up as "stricter" behavior.

!!! warning "`ValidityField.resolve_valid_keys` is a whitelist — do not use it for gating"
    `ValidityField.resolve_valid_keys(model, field_name, as_of=t)` intersects
    `valid_from <= t` with `invalid_at > t` and returns the records that
    *positively claim* validity at `t`. That is the opposite selection from
    every gating layer, which computes an *exclusion* set instead. Retained as
    a public helper for callers that genuinely want "which records claim
    validity right now" — audit and provenance tooling — never for retrieval
    gating. Passing its result to a whitelist-style filter would hide every
    unmanaged record. The gating call sites are
    `QueryBuilder._apply_validity_mask` (composite path) and
    `ContextAssembler._resolve_excluded_keys` (assembler path); both compute
    exclusion sets, not whitelists.

## `SupersessionProtocol`

`SupersessionProtocol` is a stateless coordinator of `@staticmethod`s —
never a mixin, never instantiated — mirroring
[`ObservationProtocol`](observation-protocol.md)'s shape.

### Identity normalization

```python
SupersessionProtocol.identity_key(subject, predicate)
```

Casefolds and strips each component, collapses internal whitespace, joins
the two with a `\x00` separator, and hashes the result with
`blake2b(digest_size=8)` into 16 lowercase hex characters. The `\x00` join
prevents delimiter-collision false merges (`("ab", "c")` cannot collide with
`("a", "bc")`); the digest keeps raw user text out of the Redis keyspace.
Deterministic and LLM-free by design — semantic identity normalization ("is
`plan` the same predicate as `subscription_tier`?") is a downstream, opt-in
concern, not core.

### Mutations

```python
SupersessionProtocol.supersede(new_instance, *, identity_key=..., at=None)
SupersessionProtocol.invalidate(instance, at=None, superseded_by=None)
```

`supersede()` closes whichever record is currently open for `identity_key`,
chains it to `new_instance`, and repoints the identity's open pointer — one
atomic `EVAL` (`SUPERSEDE_LUA`). The first claim about a new identity simply
opens and writes no chain link, returning `None`. `invalidate()` is the
direct, identity-free form: close one specific record, optionally chaining it
to whatever replaced it.

Both route through `ValidityField.execute_supersede`, the single seam that
knows `SUPERSEDE_LUA`'s KEYS/ARGV order. Key mutation properties, all
enforced inside the one script:

- **Closed, never deleted.** Superseding a record closes its interval; the
  record and its chain links survive for provenance and `as_of` replay.
- **Atomic.** Interval closure, chain-link writes, and open-pointer repoint
  happen in one `EVAL`. There is no observable state where a record is
  interval-closed but still index-visible, or closed but unchained.
- **Idempotent under retry.** A `ZSCORE != +inf` guard refuses to re-close an
  already-closed record, so two writers racing the same identity serialize
  into a two-link chain rather than forking.
- **Graceful degradation on unsaved instances.** Key resolution is wrapped in
  `except (TypeError, ValueError)`; an unsaved instance degrades to a no-op
  *before* any write is issued, so no partial index state (no `valid_from`
  entry, no chain link, no pointer) is ever left behind.

### Bidirectional chain traversal

```python
SupersessionProtocol.superseded_by(instance)  # one hop forward, or None at the head
SupersessionProtocol.supersedes(instance)     # one hop backward, or None at the tail
SupersessionProtocol.chain(instance)          # full chain, oldest first, from any anchor
```

`chain()` walks backward to the oldest ancestor and forward to the newest
descendant, so it is recoverable from any member, not just an endpoint.
Traversal terminates on a cycle (a `seen` set) and on a dangling link — a
chain HASH entry naming a record whose `valid_from` entry no longer exists,
which happens when `on_delete` has scrubbed that record's own chain *fields*
but a neighbor's link still names it as a *value*.

## Three gating layers

`ContextAssembler` never calls `top_by_decay` — every retrieval call is
`composite_score` or `fuse`, and the BM25 and graph-propagation arms bypass
the `filters` dict entirely. That single fact is why validity gating is not
one mechanism but three, each with a distinct, non-overlapping job:

**Layer 1 — decay-Lua gate.** `DECAY_SCORE_LUA` grows `KEYS[3]` (`invalid_at`),
`KEYS[4]` (`valid_from`), and `ARGV[7]` (as-of). Per member, before the
base-score `HGET` and before any decay math, up to two `ZSCORE`s decide
inclusion: skip if `invalid_at <= as_of` (closed) or `valid_from > as_of` (not
yet started). Every `KEYS[n]` read is guarded `KEYS[n] or ''`, so a caller
that passes a short `numkeys` (existing hand-`eval` test call sites included)
gets `nil` → `''` → gate disabled, with byte-identical scores to the
pre-#580 script. This layer is **authoritative for `top_by_decay` on a plain
`DecayingSortedField`**, whose result is the member list itself with no later
union — the one path where "skip in the range read" *is* "excluded from the
result." It does **not** cover `top_by_decay` on a `CyclicDecayField`, which
runs `CYCLIC_DECAY_LUA` instead; that script was deliberately left ungated —
see "Known limitations".

**Layer 2 — the `composite_score` mask (`QueryBuilder._apply_validity_mask`).**
`composite_score` merges its per-index temp ZSETs with `ZUNIONSTORE ...
AGGREGATE SUM`. A member the decay Lua skips is merely *absent from the decay
arm* — under `SUM` its decay contribution becomes `0`, but it can still
surface in the composite result on the strength of any other weighted arm (a
`ConfidenceField` index, `co_occurrence_boost`, `similarity_boost`). Skipping
is not excluding. After the union, `_apply_validity_mask` runs four core
commands — `ZRANGESTORE` the closed set, `ZRANGESTORE` the not-yet-started
set, union them into an exclusion set, and `ZDIFFSTORE` that exclusion set out
of the composite key — which is what actually enforces membership on this
path, and the only layer that reaches a bare `Model.query.composite_score()`
call outside the assembler.

**Layer 3 — the assembler post-filter (`ContextAssembler._resolve_excluded_keys`
/ `_scope_by_validity`).** Covers the `fuse`, BM25, and graph-propagation
arms, none of which route through `composite_score`'s `ZUNIONSTORE` or
consult the `filters` dict at all. Two read-only `ZRANGEBYSCORE`s per
`assemble()` call produce an exclusion set; `_scope_by_validity` drops any
candidate record whose key is in it. Mirrors the tag-scoping pattern
(`_scope_by_tags`) already established for issue #492.

No layer is load-bearing for a path another layer already covers — Layer 1 is
the only mechanism for `top_by_decay` on a plain `DecayingSortedField`; Layer 2
is the only one that enforces membership on the composite path; Layer 3 is the
only one that reaches `fuse`/BM25/graph. The one path no layer covers is a
direct `top_by_decay` on a `CyclicDecayField` — see "Known limitations".

## Point-in-time reconstruction

```python
result = ContextAssembler(Fact).assemble(query_cues={"topic": "plan"}, as_of=two_weeks_ago)
```

`assemble()` gains a keyword-only `as_of: float | None = None`. The default
`None` means "now" — only currently-valid records. Passing an epoch
reconstructs what the agent believed at that instant, superseded records
included, applied consistently across all three gating layers (the same
instant is threaded through the decay-Lua gate, the composite mask, and the
post-filter). `composite_score` and `top_by_decay` accept the same
keyword-only `as_of` directly, for callers that bypass the assembler.

A model with no `ValidityField` — every shipped model today — makes `as_of`
and all three gating layers a pure passthrough; retrieval stays byte-identical
to pre-#580 behavior.

## Kill switch

```python
from popoto.fields.constants import Defaults
Defaults.VALIDITY_GATING_ENABLED = False  # restores byte-identical pre-#580 retrieval
```

`Defaults.VALIDITY_GATING_ENABLED` is a deploy-level boolean, default `True`,
read **at call time** in every gating layer — never captured at import — so
it takes effect at runtime for adopters who cannot edit model code. With it
off, interval and chain *maintenance* still runs (the six keys stay correct),
but no retrieval path consults them: `filter(validity__current=...)` and
`filter(validity__as_of=...)` still work, since those are deliberate queries
this switch does not govern. The blast radius of leaving it on by default is
zero until a model actually declares a `ValidityField` — no shipped model,
including `DefaultMemory`, does.

## Known limitations

- **A direct `Model.query.top_by_decay()` on a `CyclicDecayField` is not
  gated.** #580 extended `DECAY_SCORE_LUA` only; `CYCLIC_DECAY_LUA` was
  deliberately left unmodified (an explicit plan No-Go), so it takes no
  `invalid_at`/`valid_from` `KEYS` and no as-of `ARGV`. A `top_by_decay` call
  that resolves to a `CyclicDecayField` therefore receives gating from none of
  the three layers: Layer 1 never reaches the cyclic script, Layer 2 only acts
  after `composite_score`'s union, and Layer 3 only runs inside
  `ContextAssembler`. A superseded record will be returned by such a call.

    Scope: this is a docs/direct-caller gap, **not** a live retrieval bug.
    `ContextAssembler` never calls `top_by_decay`; its push path uses
    `composite_score` (Layer 2), and every candidate it assembles is
    post-filtered by `_scope_by_validity` (Layer 3), including results from its
    cyclic decay proxy. `composite_score` on a `CyclicDecayField` is likewise
    covered by Layer 2's mask. Only a caller reaching past the assembler
    straight to `top_by_decay` on a cyclic field sees the gap; such a caller
    should use `composite_score`, or intersect with
    `filter(validity__current=True)`. Pinned by
    `tests/test_validity_field.py::TestCyclicDecayGatingGap`, which fails
    loudly if the gate is ever added — update this entry then.
- **Gating costs up to two `ZSCORE`s per member inside the decay Lua**, and
  `DECAY_SCORE_LUA` full-scans its partition regardless of gating (a
  pre-existing property, not introduced here). Measured locally on a 20k-record
  partition: `top_by_decay` at ~1.4x wall time gated vs. ungated (~37ms
  ungated / ~51ms gated). The cost scales with partition size, not with how
  many records are actually closed.
- **The TTL warning fires on first save, not at model-definition time.**
  `ValidityField.warn_if_ttl` logs once per `(model, field)` pair the first
  time a record on a `Meta.ttl`-bearing model is saved, not when the class
  body executes — so the warning is observable in test output and logs, not
  at import time.
- **A TTL on a `ValidityField`-bearing model truncates chains and breaks
  `as_of` correctness.** Redis expires the record's hash on its own schedule;
  the record's interval and chain-link entries do not expire with it, so a
  chain walk or an `as_of` reconstruction can reference a record that no
  longer exists. The warning is deliberately advisory, not a raised
  exception — refusing outright would break adopters who legitimately want
  bounded history.

## See Also

- [Provenance Journal](provenance-journal.md) — this feature's first real
  consumer: `JournalEntry` composes `ValidityField` for its membership axis,
  calls `execute_supersede` directly on the annotate-and-close write path
  (never `SupersessionProtocol`, which no-ops against a same-pipeline
  successor), and uses `chain()` for provenance display only, never for
  membership
- [ObservationProtocol](observation-protocol.md) — the outcome vocabulary
  that reports contradiction; `_apply_contradicted` writes provenance through
  this protocol when the model has a `ValidityField`
- [DecayingSortedField](decaying-sorted-field.md) — the ordering axis
  validity composes with
- [ContextAssembler](context-assembler.md) — the retrieval-to-injection
  bridge that auto-detects a model's `ValidityField` and applies `as_of`
- [ConfidenceField](confidence-field.md) — the arm whose contribution the
  composite mask (Layer 2) must subtract out, not merely zero
