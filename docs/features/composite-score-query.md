# CompositeScoreQuery

Multi-factor retrieval for Popoto models. Combines N sorted set indexes with configurable weights via Redis `ZUNIONSTORE` and returns top-K results ranked by composite score.

## Quick start

```python
from popoto import Model, KeyField, Field
from popoto.fields import DecayingSortedField, ConfidenceField
from popoto.fields.access_tracker import AccessTrackerMixin
from popoto.fields.write_filter import WriteFilterMixin

class Memory(AccessTrackerMixin, WriteFilterMixin, Model):
    agent_id = KeyField()
    content = Field(type=str)
    importance = Field(type=float, default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    certainty = ConfidenceField(initial_confidence=0.5)

    def compute_filter_score(self):
        return self.importance or 0.0

# Retrieve top-10 by weighted composite
results = Memory.query.filter(agent_id="agent-1").composite_score(
    indexes={
        "relevance": 0.4,
        "certainty": 0.3,
        "access_count": 0.2,
        "priority": 0.1,
    },
    limit=10,
)
```

## API reference

### `QueryBuilder.composite_score()`

```python
def composite_score(
    self,
    indexes: dict[str, float],
    limit: int = 10,
    aggregate: str = "SUM",
    min_score: float = None,
    post_filter: Optional[Callable[[str, float], bool]] = None,
    co_occurrence_boost: dict = None,
    temperature: float = 1.0,
) -> list:
```

Also available as `Query.composite_score()` (convenience method that creates a QueryBuilder internally).

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `indexes` | `dict[str, float]` | *required* | Field names mapped to weights. Relative ratios matter, not absolute values. |
| `limit` | `int` | `10` | Maximum results to return. |
| `aggregate` | `str` | `"SUM"` | Score combination mode: `"SUM"`, `"MIN"`, or `"MAX"`. |
| `min_score` | `float` | `None` | Minimum composite score threshold. |
| `post_filter` | `Callable[[str, float], bool]` | `None` | `(redis_key, score) -> bool` filter applied after scoring. |
| `co_occurrence_boost` | `dict` | `None` | `{redis_key: weight}` from `CoOccurrenceField.propagate()`. |
| `temperature` | `float` | `1.0` | Score scaling factor. Divides each composite score by this value. Low values (0.02-0.1) sharpen discrimination; high values (2.0+) flatten scores. Must be > 0. |

### Supported index types

| Index name | Source | How it resolves |
|------------|--------|----------------|
| `DecayingSortedField` name | Model field | Materializes decay-computed scores into temp ZSET via Lua |
| `CyclicDecayField` name | Model field | Same as above, uses cyclic decay Lua script |
| `SortedField` name | Model field | Uses existing sorted set directly |
| `ConfidenceField` name | Model field | Materializes confidence from companion hash |
| `"access_count"` | `AccessTrackerMixin` | Materializes access count from meta hashes |
| `"access_score"` | `AccessTrackerMixin` | Same as `"access_count"` |
| `"priority"` | `WriteFilterMixin` | Uses `$WF:{Class}:priority` sorted set directly |

## How it works

1. **Index resolution**: Each named index maps to a Redis sorted set key. Native ZSET fields resolve directly. Non-ZSET sources (ConfidenceField, AccessTracker) are materialized into temporary sorted sets.

2. **ZUNIONSTORE**: Redis combines all resolved sorted set keys into a single temporary sorted set with the specified weights and aggregate mode.

3. **ZREVRANGE**: Top-K members extracted from the composite sorted set. If `min_score` is set, uses `ZREVRANGEBYSCORE` instead.

4. **Temperature scaling**: Each score is divided by the `temperature` value. When `temperature=1.0` (default), scores are unchanged. Lower temperatures amplify score differences; higher temperatures compress them.

5. **Post-filter**: Optional callback filters results before hydration.

6. **Cleanup**: All temporary keys deleted immediately. Keys also have a 5-second EXPIRE as a safety net.

> **Scaling note:** The `access_count`/`access_score` index uses `SMEMBERS` to discover all model instances. For models with 100K+ instances, this scan can be expensive. Use `post_filter` or partitioned queries to narrow the result set at that scale.

7. **Hydration**: Redis keys passed to existing Query infrastructure for model instance loading.

## CoOccurrence boost

Inject graph propagation scores as an additional scoring signal:

```python
from popoto.fields.co_occurrence_field import CoOccurrenceField

assoc_field = Memory._meta.fields["associations"]
co_scores = assoc_field.propagate(Memory, seed_pks=["key1"], depth=2)

results = Memory.query.filter(agent_id="agent-1").composite_score(
    indexes={"relevance": 0.3, "certainty": 0.3},
    co_occurrence_boost=co_scores,
    limit=10,
)
```

## Temperature scaling

The `temperature` parameter controls score discrimination after composite scoring:

```python
# Sharp retrieval -- top result dominates (scores amplified 10x)
results = Memory.query.composite_score(
    indexes={"relevance": 0.4, "certainty": 0.3},
    temperature=0.1,
    limit=5,
)

# Default -- unchanged behavior (score / 1.0 = score)
results = Memory.query.composite_score(
    indexes={"relevance": 0.4, "certainty": 0.3},
    limit=5,
)

# Exploratory -- diverse spread (scores compressed 3x)
results = Memory.query.composite_score(
    indexes={"relevance": 0.4, "certainty": 0.3},
    temperature=3.0,
    limit=5,
)
```

Since dividing all scores by a positive constant preserves ordering, temperature affects score *values* but not ranking. This is useful for downstream consumers (e.g., probability-based selection or adaptive thresholding).

## Error handling

| Condition | Behavior |
|-----------|----------|
| Empty `indexes` dict | `QueryException` |
| Unknown field name | `QueryException` with valid field list |
| Field without sorted set | `QueryException` |
| `"priority"` without `WriteFilterMixin` | `QueryException` |
| `"access_count"` without `AccessTrackerMixin` | `QueryException` |
| Missing partition filter | `QueryException` |
| `temperature <= 0` | `QueryException` |
| `limit=0` | Returns `[]` |
| No matching records | Returns `[]` |

## Temp key conventions

All temporary keys follow the pattern `$CSQ:{ModelName}:{type}:{uid}` where `uid` is a random 8-character hex string. Keys are deleted in a `finally` block and also set with `EXPIRE 5` as a safety net against process crashes.
