# Popoto: Agent Memory on Redis and Valkey

Memory for LLM agents, as primitives you program rather than a service you call.
Records decay over time, confidence moves with evidence, associations form
between things mentioned together, and a context assembler packs the result into
a token budget before each turn.

It runs in your process against a Redis or Valkey server you already operate.
Your memory data stays in your database.

```bash
pip install popoto
```

Three packages, 8.7 MB of site-packages in a clean Python 3.12 venv, no API key.
Point it at Redis or Valkey on `localhost:6379` and you are running.

## Memory around an LLM turn

```python
from popoto.recipes import SubconsciousMemory

sm = SubconsciousMemory(agent_id="agent-1")

messages, assembly = sm.inject_context(messages)   # pre-turn: retrieve + inject
answer = call_your_llm(messages)                   # your LLM call
sm.extract_memories(answer, importance=0.6)        # post-turn: save what was learned
sm.report_outcomes(assembly, outcome="acted")      # feedback: reinforce what was used
```

`agent_id` is the only required argument. It partitions every index, and an
explicit `.filter(agent_id=...)` query always honors that partition — but
`inject_context`'s default retrieval path (lexical/BM25) does not yet filter
by it ([#576](https://github.com/tomcounsell/popoto/issues/576)), so two agents sharing one Redis via the default loop can
retrieve each other's memories. Leaving `model_class`
unset selects `DefaultMemory`, which ships the benchmarked configuration: decay,
confidence, a keyword index that makes retrieval respond to the query text, and
an association graph.

[Add memory to your agent](guides/agent-memory-quickstart.md) walks the same
loop up level by level, from a single decaying field to the full assembly.
Running inside Claude Code, Codex, Hermes, or OpenClaw instead of your own
loop? [Add memory to your harness](features/harness-integration.md) wires the
same primitives into hooks and MCP, no glue code required.

## What is measured

Every number here comes from a harness in this repository, with its result JSON
committed alongside. Method, per-category tables, and the runs that came out
badly are in [Benchmarks](benchmarks.md).

- **Retrieval quality.** LongMemEval-S, all 500 questions, hybrid BM25 + vector:
  Recall@1 **0.892**, Recall@5 0.986, MRR 0.931. Read the
  [granularity disclosure](benchmarks.md#retrieval-modes) before comparing this
  to another system: Popoto ranks turns and scores hits at the session level.
- **Retrieval latency.** p50 **3.0 ms at 1,000 records, 6.0 ms at 20,000**,
  in-process on the lexical path, on one Apple-silicon machine. Absolute
  milliseconds are machine-dependent; the shape of the curve is the durable
  part.
- **Install weight.** Three packages and no API key, verified in a clean venv.
  Nothing here calls out to a hosted service.

End-to-end judged answer accuracy trails retrieval quality by a wide margin.
Finding the right evidence is far more reliable than answering from it, and the
number, its interval, and its protocol are published in
[Benchmarks](benchmarks.md).

## Built on a full Redis and Valkey ORM

Every memory primitive is a field on an ordinary model, so the same Django-like
query syntax, indexes, TTLs, relationships, and pub/sub apply to memory records
and to everything else in your keyspace.

```python
from popoto import Model, KeyField, Field, SortedField, GeoField

class Restaurant(Model):
    name = KeyField()
    cuisine = Field(type=str)
    rating = SortedField(type=float)
    location = GeoField()

Restaurant.create(
    name="Burger Palace",
    cuisine="American",
    rating=4.5,
    location=(40.7128, -74.0060),
)

restaurant = Restaurant.query.get(name="Burger Palace")
print(f"{restaurant.name} serves {restaurant.cuisine} food.")
# => 'Burger Palace serves American food.'
```

Reading and writing happen at RAM speed. Popoto adds
[async operations](async.md), [multi-tenancy](multi-tenancy.md) via KeyField
namespacing, geometric distance search, timeseries for streaming data, Pandas
and Xarray interoperation, [pub/sub](pubsub.md) for message queues,
[content and embedding fields](features/content-and-embedding-fields.md) for
large content storage and semantic search, and
[generic export/import](guides/export-import.md) with per-field round-trip
fidelity for moving records between Redis instances.

Start at [Configuration](configuration.md) and
[Models and Fields](fields.md) for the ORM half of the library.

## Valkey is a first-class target

Popoto uses core Redis data types and commands only, with no Redis-module
dependency, and the suite carries explicit Valkey-safety tests asserting that
indexes stay on plain types. Point `REDIS_URL` at a
[Valkey](https://valkey.io) server and the same code runs.

```python
REDIS_URL = "redis://HOST[:PORT]/DATABASE[?password=PASSWORD]"
```

`REDIS_URL` is optional in local development. See
[Configuration](configuration.md) for the full connection options.

## Error reporting is opt-in

Library-specific exceptions can be reported to the maintainers through an
isolated Sentry client that never touches your own Sentry setup. It is off
until you turn it on.

```python
import popoto

popoto.enable_error_reporting()
```

Install `popoto[monitoring]` for the `sentry-sdk` dependency, or skip this
entirely. Popoto works the same either way.

---

![](/static/popoto.png)

Popoto is named after the [Māui dolphin](https://en.wikipedia.org/wiki/M%C4%81ui_dolphin),
the world's smallest dolphin subspecies. Dolphins are fast, agile, and work in
social groups. Popoto wraps Redis and Valkey in the same spirit.

For help building applications with Python and Redis, contact
[Tom Counsell](https://tomcounsell.com) on
[LinkedIn](https://linkedin.com/in/tomcounsell).
