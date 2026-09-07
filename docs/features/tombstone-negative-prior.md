# Tombstones as a Negative Prior

A tombstone records that a memory was forgotten. On its own that is an epitaph:
the record is gone from retrieval, the death is archived, and nothing reads the
archive again. So the same low-value content could be re-ingested, re-injected,
re-dismissed and re-forgotten forever — the corpus relearned the same lesson
every time, at full cost.

The negative prior makes that evidence transfer forward. Popoto remembers how
many times a given piece of content has been buried, and when a new record
arrives carrying the same content, its write-filter score is drawn down before
the existing gate sees it. Bury something once and a future duplicate has to be
about twice as important to get in. Bury it repeatedly and the bar keeps
rising.

It is on by default and there is nothing to wire up.

## How it engages

The capability auto-detects. A model participates when it carries an
[`ExistenceFilter`](existence-filter.md) with a `fingerprint_fn` — that
function is what tells Popoto which part of a record is its *content*, as
opposed to its key or its bookkeeping:

```python
import popoto
from popoto.fields.existence_filter import ExistenceFilter
from popoto.fields.write_filter import WriteFilterMixin


class Memory(WriteFilterMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.Field(type=str)
    importance = popoto.FloatField(default=0.0)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.content,
    )

    def compute_filter_score(self):
        return self.importance or 0.0
```

A model with no `ExistenceFilter`, or one whose filter has no `fingerprint_fn`,
has no content identity — there is nothing a buried record could match against.
Those models skip the consult entirely and issue **zero** extra Redis commands
on `save()`. Write behavior is byte-identical to a Popoto without this feature,
structurally rather than because a flag happens to be off.

## What it does to a score

Burials are recorded automatically when
[`MemoryLifecycle`](../recipes.md#memorylifecycle) tombstones a record:

```python
from popoto.recipes.memory_lifecycle import MemoryLifecycle

lifecycle = MemoryLifecycle(Memory, importance_field="relevance")
lifecycle.tombstone(record)     # archives the death AND records the burial
```

Afterwards, a new record whose fingerprint matches gets its
`compute_filter_score()` result multiplied by a penalty that compounds with
each prior burial:

| Prior burials | Penalty | A 0.8 score becomes |
|---|---|---|
| 0 | 1.0 | 0.80 — untouched |
| 1 | 0.5 | 0.40 |
| 2 | 0.25 | 0.20 |
| 3 | 0.125 | 0.10 |
| 6+ | 0.05 (floor) | 0.04 — below the 0.1 gate, so the write is dropped |

The drawn-down score feeds the **existing** `WriteFilterMixin` gate rather than
a second rejection path: a score that falls under `WF_MIN_THRESHOLD` raises the
same `SkipSaveException` as any other low score, and a caller that already
handles low-value writes needs no new code.

Suppression is asymptotic, never absolute. The floor exists because a memory
buried for situational reasons — true then, irrelevant now, relevant again
later — should be drawn down rather than annihilated. A genuinely important
duplicate can still clear the bar; it just has to earn it.

## Matching is exact, not fuzzy

Fingerprints are whitespace-stripped, case-folded, and hashed. Two records
match when their fingerprints are equal under that normalization, and not
otherwise. `"  Coffee Break  "` and `"coffee break"` are the same content;
anything else is not.

That is a deliberate limit. It makes "a dissimilar record is not penalized" an
exact property of the system rather than a tuned threshold with a false-positive
rate — nothing gets suppressed because it happened to share a word with
something you once forgot. Near-duplicate and paraphrase matching would need a
retrieval at write time and a false-positive policy of its own, and is out of
scope here.

Note also what the `ExistenceFilter` itself is *not* doing: its Bloom membership
test is token-based and answers "true" when any single token matches, which
would penalize nearly every write if used as the matcher. It is used here only
as the source of the fingerprint string.

## Telemetry

Drawdowns are counted, not silent:

```python
from popoto.fields.tombstone_prior import TombstonePriorStore

store = TombstonePriorStore(Memory)
store.stats()          # {"penalized": 12, "drawdown_total": 4.35}
store.count()          # distinct buried fingerprints currently tracked
store.burial_count("coffee break")   # burials recorded for one fingerprint
```

`penalized` counts writes that were drawn down; `drawdown_total` sums the score
those writes gave up. Both move together in one transaction, so they cannot
disagree.

## Storage

Three keys per model, all deliberately **outside** the model's own keyspace, so
no query, index scan, or key-set walk can surface them:

| Key | Type | Holds |
|---|---|---|
| `$TOMBPRIOR:{Model}:burials` | hash | fingerprint digest → burial count |
| `$TOMBPRIOR:{Model}:index` | zset | fingerprint digest → last burial timestamp |
| `$TOMBPRIOR:{Model}:stats` | hash | `penalized`, `drawdown_total` |

The hash field name is the digest, never the raw fingerprint: a fingerprint may
be user text, and this keyspace has no business holding content.

Retention is bounded at `TOMBSTONE_PRIOR_LIMIT` (1000) distinct fingerprints
per model; the least recently buried age out first, and re-burying a fingerprint
refreshes its recency. This is plain hash and zset work — no Redis modules — so
it behaves identically on Redis and Valkey.

The whole structure is derived state. Dropping it degrades the system to its
pre-feature behavior and loses nothing else:

```python
TombstonePriorStore(Memory).purge_all()
```

To inspect the keys by hand, use Popoto's own accessor rather than building a
client, so you are looking at the database Popoto is actually bound to:

```python
import popoto

popoto.get_redis().hgetall("$TOMBPRIOR:Memory:stats")
```

## Failure behavior

Every Redis call on this path is best-effort. If the bookkeeping is unreachable
or a stored count is unreadable, the write is admitted **unchanged** and a
warning is logged — a memory system whose `save()` dies because a telemetry hash
is down is worse than one that occasionally misses a drawdown. Symmetrically, a
burial that fails to record never rolls back a tombstone that has already
archived and removed its record.

## Turning it off

Set `POPOTO_TOMBSTONE_PRIOR_DISABLE=1` (or `true`/`yes`/`on`) in the
environment. The switch is read on every call, not at import, so it can be
flipped at the deployment level without editing model code — which matters
because an installed-from-PyPI adopter cannot always edit the models they are
running. With it set, scores pass through untouched and the burial registry is
left alone.

## Constants

Pinned in `popoto.fields.constants.Defaults`, not exposed as constructor
kwargs — they are experimental tuning knobs, not per-model configuration:

| Constant | Default | Meaning |
|---|---|---|
| `TOMBSTONE_PRIOR_LIMIT` | 1000 | Distinct fingerprints retained per model |
| `TOMBSTONE_PRIOR_DECAY` | 0.5 | Multiplier compounded per prior burial |
| `TOMBSTONE_PRIOR_FLOOR` | 0.05 | Strongest suppression possible |

## See also

- [ExistenceFilter](existence-filter.md) — the source of the content fingerprint
- [MemoryLifecycle](../recipes.md#memorylifecycle) — tombstoning, restore, and retention
- [Agent Memory](agent-memory.md) — how this fits with the other primitives
