# Use Postgres

Popoto's agent memory runs on PostgreSQL as well as on Redis and Valkey. It
is the same model API: decay, confidence, keyword and vector search, the
association graph and `SubconsciousMemory`'s per-turn loop. On Postgres each
model is a typed table with native indexes. This page takes you from an empty
database to a working memory loop.

The full reference is the [Postgres backend](../features/postgres-backend.md)
page. It covers every supported field, what the schema looks like, the outage
contract and the [documented divergences from
Redis](../features/postgres-backend.md#documented-divergences).

## What you need

- **PostgreSQL 18 or newer.** Popoto checks `server_version_num` on first use
  and refuses an older server with `BackendCapabilityError`.
- **A UTF8 database.** A database with any other `server_encoding` is refused
  the same way. `createdb` uses the cluster's default encoding, so name it to
  be sure:

    ```bash
    createdb --encoding=UTF8 --template=template0 agents
    ```

- **The pgvector extension**, if any model has an `EmbeddingField`. Popoto
  never creates or drops an extension, so a role that is allowed to must run
  this once per database:

    ```sql
    CREATE EXTENSION IF NOT EXISTS vector;
    ```

    The extension's schema must be on the connection's `search_path`. The
    default, `public`, is. The `pgvector/pgvector:pg18` Docker image ships the
    extension. `DefaultMemory` has no `EmbeddingField`, so the loop below runs
    without it, but installing it up front means adding embeddings later
    needs no database change.

- **Python 3.10 or newer.**

Redis is not needed. A process whose models are all bound to Postgres sends
no Redis commands.

## Install

```bash
pip install 'popoto[postgres]'
```

The `postgres` extra adds `psycopg[binary,pool]`, `psycopg-pool`, `pgvector`,
`numpy` (for `EmbeddingField`) and `greenlet` (for native async). `import
popoto` never imports any of them. They load on the first use of a
Postgres-bound model.

## Configure

Popoto reads its own environment variables and never a generic one such as
`DATABASE_URL`:

| Variable | Required | What it does |
|---|---|---|
| `POPOTO_BACKEND` | no | `postgres` binds every model without its own `Meta.backend` to Postgres. Unset, the default is `redis`. |
| `POPOTO_POSTGRES_URL` | yes, for Postgres | The DSN, e.g. `postgresql://app@db.internal:5432/agents`. |
| `POPOTO_POSTGRES_SCHEMA` | no | The Postgres schema popoto's tables live in. Default `popoto`. |
| `POPOTO_SCHEMA_AUTO` | no | Default `1`: the first use of a model creates its table, or applies an additive change (a new nullable column, a new index). `0` turns this off, for deployments that run the DDL themselves. |
| `POPOTO_POSTGRES_MAINTENANCE_URL` | no | A direct (or session-mode) DSN to the same database, for the DDL, `REINDEX` and the `LISTEN` session. Set it when `POPOTO_POSTGRES_URL` points at PgBouncer in transaction mode. See [maintenance DSN](../features/postgres-backend.md#a-maintenance-dsn-for-pgbouncer-transaction-mode). |
| `POPOTO_POSTGRES_GRANT_MAIN_ROLE` | no | `1` makes popoto grant the main DSN's role access to the tables its maintenance-DSN DDL creates. Off by default: popoto grants nothing unless you opt in. See [two roles](../features/postgres-backend.md#two-roles-an-application-role-and-an-owner-role). |

```bash
export POPOTO_BACKEND=postgres
export POPOTO_POSTGRES_URL=postgresql://app@db.internal:5432/agents
```

Selecting Postgres without `POPOTO_POSTGRES_URL`, or without the extra
installed, raises `BackendUnavailableError` naming what is missing. You can
also configure the backend in code with
`popoto.backends.set_backend(PostgresBackend(dsn=..., schema=...))`.

## The memory loop on Postgres

With `POPOTO_BACKEND=postgres` set, the default model goes to Postgres with no
code change. This is the [quickstart's Level
0](agent-memory-quickstart.md#level-0-import-the-defaults), with a stand-in
for the LLM call so it runs as is:

```python
from popoto.recipes import SubconsciousMemory


def call_your_llm(messages):
    # Stand-in for your model call.
    return "Rollbacks re-point the load balancer at the previous colour."


sm = SubconsciousMemory(agent_id="agent-1")
sm.extract_memories(
    "Deploys use a blue-green strategy behind the load balancer.", importance=0.8
)

messages = [{"role": "user", "content": "What is our deploy strategy?"}]
messages, assembly = sm.inject_context(messages)   # pre-turn: retrieve + inject
answer = call_your_llm(messages)                   # your LLM call
sm.extract_memories(answer, importance=0.6)        # post-turn: save what was learned
sm.report_outcomes(assembly, outcome="acted")      # feedback: reinforce what was used

print([m.content for m in assembly.records])
```

The first call creates the tables in the `popoto` schema: `default_memory`,
the side tables of its keyword index and association graph, and popoto's own
registry, `popoto_schema`. To check where the records went:

```python
from popoto.backends import get_backend
from popoto.recipes import DefaultMemory

print(get_backend(DefaultMemory))   # <PostgresBackend schema='popoto'>
print(DefaultMemory.query.filter(agent_id="agent-1").count())
```

`agent_id` partitions every index on Postgres as it does on Redis, so many
agents can share one database.

## Your own model on Postgres

To put one model on Postgres while the rest of the process stays on Redis,
declare `Meta.backend` on it. This is the README's model, bound to Postgres:

```python
from popoto import (
    Model, AutoKeyField, KeyField, StringField, FloatField,
    DecayingSortedField, ConfidenceField, BM25Field,
)
from popoto.recipes import SubconsciousMemory


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
    content_bm25 = BM25Field(source="content")

    class Meta:
        backend = "postgres"


sm = SubconsciousMemory(
    model_class=Memory,
    agent_id="agent-1",
    score_weights={"relevance": 0.6, "confidence": 0.3},
)
sm.extract_memories("The staging database is rebuilt every Monday.", importance=0.7)
messages, assembly = sm.inject_context(
    [{"role": "user", "content": "When is the staging database rebuilt?"}]
)
print([m.content for m in assembly.records])
```

`Meta.backend` beats `POPOTO_BACKEND`. Declaring the class never touches the
network. Popoto checks the fields against the backend's capability table at
declaration and refuses a field Postgres cannot store with
`BackendCapabilityError`, so a model never fails halfway through. The
connection, the version and encoding checks and the DDL all run on the
model's first query or save.

Add an `EmbeddingField` and configure an embedding provider, and
`SubconsciousMemory` switches to hybrid (BM25 plus pgvector) retrieval. See
[Embeddings](../features/postgres-backend.md#embeddings).

## Async

On a Postgres-bound model every `async_*` method runs natively on
`psycopg.AsyncConnection` on the running event loop:

```python
import asyncio

import popoto
from popoto.backends.postgres.aio import get_async_backend


class Note(popoto.Model):
    owner = popoto.KeyField()
    slug = popoto.KeyField()
    body = popoto.StringField(default="")

    class Meta:
        backend = "postgres"


async def main():
    await Note.async_create(owner="a", slug="1", body="first")
    async with get_async_backend(Note).transaction() as uow:
        await Note(owner="a", slug="2", body="second").async_save(pipeline=uow)
        await Note(owner="a", slug="3", body="third").async_save(pipeline=uow)
    rows = await Note.query.async_filter(owner="a")
    print(sorted(n.slug for n in rows))


asyncio.run(main())
```

The `transaction()` commits both saves or, on an exception, neither. See
[Async](../features/postgres-backend.md#async-m5) for `popoto.batch()` from
coroutines and the rules for mixing sync and async calls.

## When the database is down

An unreachable server, or one that refuses the connection, raises
`BackendUnavailableError` (from `popoto.backends`). The message carries the
server's own reason, for example `FATAL: database "agents" does not exist`,
and never the DSN's password. `get_backend(Model).health` counts consecutive
failures and dropped writes. See [the outage
contract](../features/postgres-backend.md#topology-and-the-outage-contract).

## Not on Postgres yet

- **Harness integration.** The `popoto-memory` hook, the MCP server and
  `popoto-memory doctor` still talk only to Redis
  ([#814](https://github.com/tomcounsell/popoto/issues/814)). Use them with a
  Redis-backed process for now.
- **`DataFrameField`** is refused on Postgres. Store the frame's JSON in a
  `DictField` or a `BytesField`.

The full list of behavioural differences is in [Documented
divergences](../features/postgres-backend.md#documented-divergences).

## Moving existing memory from Redis

[Redis to Postgres Migration](../features/redis-to-postgres-migration.md) is
a one-off tool that copies a Redis snapshot (an RDB file) into the Postgres
backend, with a verification report. It is installed as a console script:

```bash
popoto-migrate-redis-to-postgres --help
```
