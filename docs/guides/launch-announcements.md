# Popoto 1.0.0 Launch Announcements

## Hacker News — Show HN

### How to post
1. Go to https://news.ycombinator.com/submit
2. Use the title and URL below
3. Post Tuesday–Thursday, 9–11am ET for best visibility
4. Immediately add your top-level comment (below)
5. Stay online for the first 2 hours to reply to comments — HN rewards fast engagement

### Title
```
Show HN: Popoto – A Redis/Valkey ORM with Django-like syntax for Python
```

### URL
```
https://github.com/tomcounsell/popoto
```

### Your first comment (post immediately after submitting)

> Hey HN, I'm Tom. I built Popoto because I wanted Django-model ergonomics for Redis without the overhead of Redis OM or hand-rolling SCAN/pipeline logic everywhere.
>
> Popoto gives you a declarative model layer on top of Redis (or Valkey):
>
> ```python
> from popoto import Model, KeyField, SortedField, Q
>
> class Restaurant(Model):
>     name = KeyField()
>     cuisine = Field()
>     rating = SortedField(type=float)
>
> Restaurant.create(name="Burger Palace", cuisine="American", rating=4.5)
>
> # Chainable queries, Q objects, expression syntax
> results = Restaurant.query.filter(
>     Q(cuisine="Italian") | Q(cuisine="Japanese"),
>     rating__gte=4.0
> ).order_by("-rating").limit(10)
> ```
>
> What makes it different:
>
> - **Django-like API** — KeyField, SortedField, GeoField, Relationship, Q objects, `get_or_create()`, `bulk_create()`
> - **Full async/await** — native `redis.asyncio`, not `asyncio.to_thread()` wrappers
> - **Atomic saves** via internal Redis pipelines
> - **Works with both Redis and Valkey** out of the box
> - **Geo search, time-series, pub/sub** built in
> - **Zero required dependencies beyond redis-py and msgpack** — pandas is optional
>
> We've been using it in production for streaming financial data and sensor networks. 1.0.0 is the first stable release after two betas and significant index integrity hardening.
>
> Docs: https://tomcounsell.github.io/popoto
> PyPI: `pip install popoto`
>
> Happy to answer any questions about the internals or how it compares to Redis OM.

---

## Product Hunt

### How to post
1. Go to https://www.producthunt.com/posts/new
2. You need a maker account (sign up or log in)
3. Launch at 12:01am PT on a Tuesday, Wednesday, or Thursday
4. Fill in the fields below
5. Have 5–10 supporters ready to upvote and comment in the first hour
6. Engage with every comment throughout launch day

### Product name
```
Popoto
```

### Tagline (60 chars max)
```
Django-like ORM for Redis and Valkey in Python
```

### Topics/tags
```
Developer Tools, Open Source, Python, Databases
```

### Description

> Popoto is a Python ORM that brings Django-model syntax to Redis and Valkey. Define your data models with fields, relationships, and indexes — Popoto handles serialization, querying, and persistence automatically.
>
> **Why Popoto?**
>
> Redis is incredibly fast, but using it as a primary data store means writing a lot of boilerplate — key naming conventions, serialization, index management, pipeline batching. Popoto eliminates all of that with a familiar, declarative API.
>
> **Key features:**
>
> 🔍 Chainable queries with Q objects and expression syntax
> ⚡ Full async/await with native redis.asyncio
> 🌍 Geographic distance search with GeoField
> 📊 Time-series data with SortedField
> 🔗 Model relationships with lazy loading
> 📦 Bulk create/update/delete via Redis pipelines
> 🔄 Works with both Redis and Valkey
>
> **Who is it for?**
>
> Python developers who use Redis for more than caching — real-time applications, streaming data pipelines, IoT sensor networks, or anywhere you want Redis speed with ORM convenience.
>
> 1.0.0 is production-stable after two beta releases and extensive index integrity hardening.

### Links
```
GitHub: https://github.com/tomcounsell/popoto
Docs:   https://tomcounsell.github.io/popoto
PyPI:   https://pypi.org/project/popoto/
```

### Maker comment (post on launch)

> Hi Product Hunt! I'm Tom, the creator of Popoto.
>
> I built this because every Redis project I worked on ended up with the same boilerplate — key naming, msgpack serialization, maintaining secondary indexes by hand. I wanted something that felt like Django models but ran on Redis.
>
> Popoto started as an internal tool for processing streaming financial data, where we needed sub-millisecond reads and Django-like query ergonomics. After 3+ years in production and two beta releases, 1.0.0 is the first stable release.
>
> I'd love to hear how you'd use it — or what's missing. Feedback welcome!

---

## General tips

- **Lead with the problem, not the product.** "Redis has no good ORM" resonates more than "here's my ORM."
- **A demo GIF is worth 1000 words.** Consider recording a 30-second terminal session showing model definition → create → query → result. Tools: asciinema, vhs, or a simple screen recording.
- **Don't cross-post on the same day.** Do HN first (higher risk, higher reward), then Product Hunt a few days later with learnings from HN feedback.
- **GitHub README is your landing page.** Make sure it looks sharp before posting.
