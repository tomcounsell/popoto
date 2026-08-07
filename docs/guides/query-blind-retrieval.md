# Query-Blind Retrieval: When Composite Mode Is Right

`ContextAssembler` has two shapes of pull path. One reads the query text and
ranks by it. The other ignores the query text and ranks by the record's own
state: how recent it is, how important it is, how confident the system is in
it, what it is associated with.

The second shape is **composite mode**, and it is query-blind on purpose. This
page is about when that is the ranking you want and when it will quietly cost
you the right answer.

## How a mode gets selected

`retrieval_mode="auto"` (the default) reads the fields declared on your model:

| Fields on the model | Effective mode | Query text |
|---|---|---|
| `BM25Field` | `lexical` | ranks the results |
| `BM25Field` + `EmbeddingField` | `hybrid` | ranks the results |
| neither | `composite` | accepted, then ignored |

The shipped [`DefaultMemory`](subconscious-memory-recipe.md#what-the-defaults-give-you)
declares a `BM25Field`, so the zero-configuration path is query-sensitive.
Authoring your own model without one lands on `composite`, and the assembler
logs a `WARNING` naming the model and the missing field. Passing
`retrieval_mode="composite"` explicitly affirms the choice and silences the
warning.

## When query-blind ranking is the right answer

Composite mode is the mode that answers "what should this agent be thinking
about right now" rather than "what matches these words".

- **Proactive injection.** A turn where the user never restates the memory it
  needs. If the agent is told in turn 3 that the user is allergic to shellfish
  and asked in turn 40 to book dinner, no keyword in turn 40 retrieves that
  fact. Ranking by importance, recency, and confidence does.
- **Cold-open turns.** The start of a session, a scheduled tick, a background
  consolidation pass. There is no query, so there is nothing for a lexical
  ranker to work with.
- **Preference and standing-instruction memory.** Records whose value comes
  from being persistently true rather than from matching this turn's topic.
- **Corpora with a narrow, curated keyspace.** When every record in the
  partition is relevant and the only question is which few fit the budget,
  score ordering is the whole job.

The [SIQ harness](../benchmarks.md#subconscious-injection-quality-siq-harness)
exists to measure exactly this regime, because no public retrieval benchmark
scores it.

## When query-blind ranking is the wrong answer

- **Any turn with a real question in it.** A user asking "what's our deployment
  strategy" gets whatever ranked highest by decay and confidence, which may be
  a lunch order. This is the [#409 failure
  mode](../benchmarks.md#deterministic-csr-harness): the ranked list is
  identical for a real query and for gibberish.
- **Large or heterogeneous corpora.** The more topics share one partition, the
  less a query-blind ranking narrows anything.
- **Anything you plan to benchmark against a retrieval dataset.** LongMemEval-S
  and LoCoMo are query-driven by construction; composite mode scores near the
  floor on them, and that number says nothing about the mode.

Adding `EmbeddingField` alone does **not** fix this. A model with embeddings
and no `BM25Field` still resolves to `composite`. Query-sensitive retrieval
requires the `BM25Field`.

## Getting query-sensitive retrieval

Declare a `BM25Field` on the content you want searchable:

```python
from popoto import BM25Field

class Memory(Model):
    ...
    content_bm25 = BM25Field(source="content")
```

Or import the model that already declares one:

```python
from popoto.recipes import DefaultMemory
```

`BM25Field` indexes on the `on_save()` hook, so records written before the
field existed are absent from the index. Re-save each once to backfill:

```python
for memory in Memory.query.filter():
    memory.save()
```

The operation is idempotent.

## Running both

The two shapes are not exclusive. `assemble()` runs the query-driven pull path
and the proactive push path in the same call, deduplicates, and applies one
budget across both. A model carrying `BM25Field` **and** `CyclicDecayField`
gets query-relevant results and proactively surfaced ones in a single ranked
list, with `AssemblyResult.proactive` naming which came from where.

## See also

- [ContextAssembler retrieval modes](../features/context-assembler.md#pull-path-modes-retrieval_mode)
  — the full mode table, fallback behavior, and construction-time validation
- [CSR regression gate](../benchmarks.md#deterministic-csr-harness) — the
  deterministic detector that distinguishes query-blindness from ordinary
  keyword dependence
- [SubconsciousMemory recipe](subconscious-memory-recipe.md) — the per-turn
  loop that consumes whichever mode your model resolves to
