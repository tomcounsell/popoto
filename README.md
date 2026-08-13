### Status
[![pypi package](https://badge.fury.io/py/popoto.svg)](https://pypi.org/project/popoto)
[![total downloads](https://pepy.tech/badge/popoto)](https://pepy.tech/project/popoto)
[![deploy docs](https://github.com/tomcounsell/popoto/actions/workflows/deploy-docs.yml/badge.svg)](https://github.com/tomcounsell/popoto/actions/workflows/deploy-docs.yml)

### Documentation: [**popoto.io**](https://popoto.io/)


# Popoto: Agent Memory on Redis and Valkey

Memory for LLM agents, as primitives you program rather than a service you call. Records decay over time, confidence moves with evidence, associations form between things mentioned together, and a context assembler packs the result into a token budget before each turn.

It runs in your process against a Redis or Valkey server you already operate. Your memory data stays in your database, and the whole install is three packages with no API key.

Underneath, Popoto is a full Redis/Valkey ORM with Django-like model syntax. The memory system is built on it, and the [ORM half is documented below](#redis--valkey-orm).

## Install

```
pip install popoto
```

That pulls `popoto`, `redis`, and `msgpack`: 3 packages, 8.7 MB of site-packages measured in a clean Python 3.12 venv. Point it at Redis or Valkey on `localhost:6379` and you are running.

## Memory around an LLM turn

`SubconsciousMemory` wraps a chat loop. It retrieves before the model call and stores after it, while your application keeps its own message list.

```python
from popoto import (
    Model, AutoKeyField, KeyField, StringField, FloatField,
    DecayingSortedField, ConfidenceField, BM25Field,
)
from popoto.recipes.subconscious_memory import SubconsciousMemory

class Memory(Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    confidence = ConfidenceField(initial_confidence=0.5)
    content_bm25 = BM25Field(source="content")  # makes retrieval query-sensitive

memory = SubconsciousMemory(
    model_class=Memory,
    agent_id="agent-1",
    score_weights={"relevance": 0.6, "confidence": 0.3},
)

Memory(
    agent_id="agent-1",
    content="Deploys use a blue-green strategy behind the load balancer.",
    importance=2.0,
).save()

messages = [{"role": "user", "content": "What is our deployment strategy?"}]

# Pre-turn: relevant memories are retrieved and injected into the system message
messages, assembly = memory.inject_context(messages)

# ... call your LLM with `messages`, get back `response_text` ...

# Post-turn: facts in the response become new memories
memory.extract_memories(response_text, importance=0.6)

# Outcome: report which injected memories the agent actually used
memory.report_outcomes(assembly)
```

`BM25Field` is what makes retrieval respond to the query text. Leave it off and `SubconsciousMemory` ranks by importance and confidence alone, which is query-blind by design and right for some workloads. The [SubconsciousMemory recipe](https://popoto.io/guides/subconscious-memory-recipe/) covers when each mode applies.

Next steps: the [Agent Memory Quickstart](https://popoto.io/guides/agent-memory-quickstart/) builds the primitives up level by level, and the [Agent Memory overview](https://popoto.io/features/agent-memory/) is the full reference.

## What is measured

Every number below comes from a harness in this repository, with its result JSON committed alongside. Method, per-category tables, and the runs that came out badly are in [Benchmarks](https://popoto.io/benchmarks/).

**Retrieval quality.** On LongMemEval-S (500 questions, hybrid BM25 + vector with unweighted RRF): Recall@1 0.894, Recall@5 0.986, MRR 0.932. Read the granularity before comparing this to anything: Popoto indexes one record per conversation turn, and a retrieved turn counts as a hit for its parent session, so these are session-level recall figures produced by turn-level ranking. Systems that rank whole sessions are answering a differently shaped question and the numbers are not interchangeable.

**Retrieval latency.** p50 3.0 ms at 1,000 records rising to 6.0 ms at 20,000 (p99 5.8 ms and 15.3 ms). In-process on the lexical path, one Apple-silicon machine, one representative run. Absolute milliseconds are machine-dependent; the shape of the curve is the durable part.

**LLM extraction was measured, and left off.** We ran Claude-based fact extraction against plain turn ingestion on the judged-answer harness, across four models plus a heuristic sentence splitter. Raw turn ingestion beat every extraction arm. The mechanism is measured rather than guessed: the extraction prompt's instruction to skip filler discards a large share of the turns that hold the ground-truth evidence, so the answer is gone before retrieval ever runs. Extraction therefore ships as a documented opt-in that stays off by default. The absolute accuracy figures come from the LoCoMo harness, which is being re-measured after a defect was found in its gold-aware ID selection, so those numbers are held back until the re-run lands. Method and code: [LLM Memory Extraction](https://popoto.io/features/llm-memory-extraction/).

**Judged end-to-end accuracy trails retrieval quality by a wide margin.** Finding the right evidence is far more reliable than answering from it, and closing that gap is the open work. Those figures also come from the LoCoMo harness under re-measurement; they get published with their confidence interval once the re-run lands.

**Valkey is a first-class target.** Popoto uses core Redis data types and commands only, with no Redis-module dependency, and the suite carries explicit Valkey-safety tests asserting that indexes stay on plain types. The same code runs against either server.

## Redis / Valkey ORM

Popoto started as an ORM and still is one. Every memory primitive above is a field on an ordinary model, so the same query syntax, indexes, TTLs, and pub/sub apply.

```python
from popoto import Model, KeyField, Field, SortedField

class Restaurant(Model):
    name = KeyField()
    cuisine = Field()
    rating = SortedField(type=float)

Restaurant.create(name="Burger Palace", cuisine="American", rating=4.5)

restaurant = Restaurant.query.get(name="Burger Palace")

print(f"{restaurant.name} serves {restaurant.cuisine} food.")
# => "Burger Palace serves American food."
```

### Features

 - very fast stores and queries
 - familiar syntax, similar to Django models
 - Async operations for asyncio-based applications
 - Geometric distance search
 - Timeseries for streaming data
 - compatible with Pandas, Xarray for N-dimensional matrix search
 - PubSub for message queues, streaming data processing
 - **Full Redis and Valkey support** - works with both out of the box
 - **[Agent Memory](https://popoto.io/features/agent-memory/)** - programmable memory primitives for AI agents (decay, confidence, associations, context assembly)
 - **[Content & Embeddings](https://popoto.io/features/content-and-embedding-fields/)** - large content storage, vector embeddings, and semantic search
 - **[Export & Import](https://popoto.io/guides/export-import/)** - move records between Redis instances with per-field round-trip fidelity and conflict/write-gate/embedding-mismatch policies

**Popoto** is ideal for streaming data. The pub/sub module allows you to trigger state updates in real time.
Currently being used in production for:

 - trigger buy/sell actions from streaming price data
 - robots sending each other messages for teamwork
 - compressing sensor data and training neural networks

### Relationships, TTLs, and Meta options

``` python
import popoto
from popoto import Relationship, DatetimeField

class Restaurant(popoto.Model):
    name = popoto.KeyField()
    cuisine = popoto.Field()
    rating = popoto.SortedField(type=float)
    location = popoto.GeoField()

class Order(popoto.Model):
    order_id = popoto.AutoKeyField()
    restaurant = Relationship(Restaurant)
    total = popoto.SortedField(type=float)
    status = popoto.Field(default="pending")
    created_at = DatetimeField(auto_now_add=True)

    class Meta:
        order_by = "-created_at"
        ttl = 2592000  # 30 days
```

### Save instances

``` python
restaurant = Restaurant(name="Burger Palace")
restaurant.cuisine = "American"
restaurant.rating = 4.5
restaurant.location = (40.7128, -74.0060)
restaurant.save()

order = Order.create(restaurant=restaurant, total=24.99)
```

### Queries

``` python
from datetime import datetime, timedelta

midtown = (40.7549, -73.9840)
yesterday = datetime.now() - timedelta(days=1)

nearby_restaurants = Restaurant.query.filter(
    location=midtown,
    location_radius=5, location_radius_unit='km',
    rating__gte=4.0
)

print(len(nearby_restaurants))
# => 1

recent_orders = Order.query.filter(
    created_at__gte=yesterday,
    total__gte=10.00
)
```

Full ORM reference: [Models and Fields](https://popoto.io/fields/), [Making Queries](https://popoto.io/query/), [Async Operations](https://popoto.io/async/), [TTL](https://popoto.io/ttl/), [PubSub](https://popoto.io/pubsub/), [Multi-Tenancy](https://popoto.io/multi-tenancy/).

## Running locally

Popoto is a library, not a standalone service. It runs inside your application and talks to a Redis or Valkey server. To exercise it locally, and to run the test suite, you need a Redis/Valkey server listening on `localhost:6379`.

```bash
# 1. Start Redis (or Valkey), e.g. on macOS via Homebrew:
redis-server                     # or: brew services start redis

# 2. Install Popoto with dev dependencies:
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"

# 3. Run the test suite (auto-isolated on Redis DB 15):
pytest
```

By default Popoto connects to `localhost:6379`; set `REDIS_URL` to point at a different server. The pytest plugin isolates tests on Redis DB 15 (override with `POPOTO_TEST_DB=<n>`).


# Documentation

Documentation is available at [**popoto.io**](https://popoto.io/)

Please create new feature and documentation related issues at [github.com/tomcounsell/popoto/issues](https://github.com/tomcounsell/popoto/issues) or make a pull request with your improvements.


# License

Popoto is released under the MIT Open Source license.


# Popoto Community

Questions, bug reports, and feature requests are welcome on [GitHub Issues](https://github.com/tomcounsell/popoto/issues) and [GitHub Discussions](https://github.com/tomcounsell/popoto/discussions). Contributions via pull request are encouraged.

![Popoto](https://raw.githubusercontent.com/tomcounsell/popoto/main/static/popoto.png)

Popoto gets its name from the [Maui dolphin](https://en.wikipedia.org/wiki/M%C4%81ui_dolphin) subspecies, the world's smallest dolphin subspecies.
Because dolphins are fast moving, agile, and work together in social groups. In the same way, Popoto wraps Redis and Valkey to make it easy to manage streaming timeseries data and object persistence.
