"""PolicyCache — Learned action selection from crystallized patterns.

A reference recipe composing all shipped Popoto memory primitives into an
RL-style action selection cache. Agents accumulate state-action-outcome events;
a StreamConsumer crystallization handler detects repeated successful patterns
and creates PolicyEntry records. Agents query policies via CompositeScoreQuery
for action selection. Outcomes update Q-values via temporal difference learning.

Components:
    - PolicyEntry: Model composing DecayingSortedField, ConfidenceField,
      CoOccurrenceField, ExistenceFilter, EventStreamMixin, AccessTrackerMixin,
      and PredictionLedgerMixin.
    - update_q_value(): Atomic Q-value TD update via Lua script.
    - compute_fingerprint(): Stable state fingerprint from feature dicts.
    - wilson_ci_lower(): Wilson score confidence interval lower bound.
    - chi_squared_uniform(): Chi-squared test against uniform distribution.
    - crystallization_handler(): StreamConsumer handler for pattern detection.
    - temporal_discovery_handler(): StreamConsumer handler for cycle discovery.

Dependencies:
    All 12 shipped Popoto primitives (Steps 1-10 of the memory roadmap).
    No external dependencies beyond Popoto itself.

Example:
    from popoto.recipes.policy_cache import (
        PolicyEntry, compute_fingerprint, update_q_value,
        crystallization_handler,
    )
    from popoto.streams import StreamConsumer

    # Create a policy manually
    policy = PolicyEntry(
        agent_id="agent-1",
        state_fingerprint=compute_fingerprint({"task": "deploy", "env": "staging"}),
        state_features={"task": "deploy", "env": "staging"},
        action_type="run_playbook",
        action_spec={"playbook": "deploy.yml"},
    )
    policy.save()

    # Or let crystallization detect patterns automatically
    consumer = StreamConsumer(
        stream_key="stream:policy_mutations",
        group_name="crystallizer",
        consumer_name="worker-1",
        handler=crystallization_handler,
    )
"""

import hashlib
import json
import logging
import math
import time


from ..fields.access_tracker import AccessTrackerMixin
from ..fields.co_occurrence_field import CoOccurrenceField
from ..fields.confidence_field import ConfidenceField
from ..fields.constants import TemporalPeriod
from ..fields.decaying_sorted_field import DecayingSortedField
from ..fields.event_stream import EventStreamMixin
from ..fields.existence_filter import ExistenceFilter
from ..fields.field import Field
from ..fields.prediction_ledger import PredictionLedgerMixin
from ..fields.shortcuts import AutoKeyField, KeyField
from ..models.base import Model
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger("POPOTO.PolicyCache")

# ---------------------------------------------------------------------------
# Tuning Constants (experimental — see issue #234 for post-ship tuning)
# ---------------------------------------------------------------------------

MIN_EVENTS_FOR_CRYSTALLIZATION = 3
"""Minimum events with same (state_fingerprint, action_type) before
considering crystallization. Can be set as low as 1 for eager mode
in high-confidence environments."""

WILSON_CI_THRESHOLD = 0.6
"""Wilson confidence interval lower bound that must be exceeded for
crystallization to trigger. Higher values require stronger evidence."""

TD_ALPHA = 0.1
"""Q-value learning rate for temporal difference updates. Controls how
much new reward information overrides the existing Q-value estimate."""

TD_GAMMA = 0.95
"""Q-value discount factor for temporal difference updates. Controls
the importance of future expected rewards relative to immediate reward."""

CHI_SQUARED_P_THRESHOLD = 0.05
"""p-value threshold for temporal pattern discovery. Clusters must
exceed this significance level to be recorded as cyclical patterns."""

INITIAL_CYCLE_AMPLITUDE = 0.5
"""Initial amplitude for discovered temporal cycles. Cycles strengthen
or weaken over time via CyclicDecayField entrainment."""

# Critical values for chi-squared test at p=0.05 for common degrees of freedom.
# df = number_of_buckets - 1
# NOTE: This table only covers df values used by the built-in bucket configs
# (day_of_week=7, week_of_month=4, month_of_year=12) plus two common extras
# (quarters=3, hours=24). If you add custom bucket configs with different
# bucket counts, extend this table with the appropriate critical value.
# Unlisted df values are silently skipped by temporal_discovery_handler.
CHI_SQUARED_CRITICAL_VALUES = {
    2: 5.991,  # 3 buckets (quarters)
    3: 7.815,  # 4 buckets (weeks-in-month)
    6: 12.592,  # 7 buckets (days-of-week)
    11: 19.675,  # 12 buckets (months-of-year)
    23: 35.172,  # 24 buckets (hours-of-day)
}
"""Chi-squared critical values at p=0.05 for common degrees of freedom.
If the test statistic exceeds the critical value, the null hypothesis
(uniform distribution) is rejected — the pattern is significant."""


# ---------------------------------------------------------------------------
# Lua Scripts
# ---------------------------------------------------------------------------

TD_UPDATE_LUA = """
-- td_update.lua: Temporal difference Q-value update
-- KEYS[1] = DecayingSortedField sorted set key
-- ARGV[1] = reward (float)
-- ARGV[2] = alpha (learning rate)
-- ARGV[3] = gamma (discount factor)
-- ARGV[4] = max_future_q (float)
-- ARGV[5] = member key (instance redis_key)
--
-- Returns: td_error as string

local current_q = tonumber(redis.call('ZSCORE', KEYS[1], ARGV[5]))
if current_q == nil then
    current_q = 0
end
local reward = tonumber(ARGV[1])
local alpha = tonumber(ARGV[2])
local gamma = tonumber(ARGV[3])
local max_future_q = tonumber(ARGV[4])

local td_error = reward + gamma * max_future_q - current_q
local new_q = current_q + alpha * td_error

redis.call('ZADD', KEYS[1], new_q, ARGV[5])
return tostring(td_error)
"""


# ---------------------------------------------------------------------------
# PolicyEntry Model
# ---------------------------------------------------------------------------


class PolicyEntry(EventStreamMixin, AccessTrackerMixin, PredictionLedgerMixin, Model):
    """Reference model for learned action selection policies.

    Composes all shipped Popoto memory primitives into a single model that
    stores state -> action -> expected_value triples. Agents query policies
    by state_fingerprint and select actions based on expected_value (Q-value)
    weighted by confidence and co-occurrence.

    Fields:
        entry_id: Auto-generated unique key.
        agent_id: Partition key scoping policies per agent.
        state_fingerprint: SHA-256 hash of state features for fast lookup.
        state_features: Original state feature dict (JSON-serializable).
        action_type: Category of the action (e.g., "run_playbook").
        action_spec: Full action specification dict (JSON-serializable).
        expected_value: Q-value with temporal decay, partitioned by agent.
        confidence: Bayesian confidence growing with successful outcomes.
        related_policies: Weighted co-occurrence graph between policies.
        bloom: Bloom filter for fast state_fingerprint pre-checks.

    Mixins:
        EventStreamMixin: Logs all mutations to Redis Streams.
        AccessTrackerMixin: Tracks read patterns for proactive surfacing.
        PredictionLedgerMixin: Records and resolves outcome predictions.

    Note:
        WriteFilterMixin is intentionally excluded — the crystallization
        handler IS the write gate (Wilson CI > threshold). Having gating
        logic in two places makes debugging harder.
    """

    entry_id = AutoKeyField()
    agent_id = KeyField()
    state_fingerprint = KeyField()
    state_features = Field()  # JSON dict
    action_type = KeyField()
    action_spec = Field()  # JSON dict

    expected_value = DecayingSortedField(
        partition_by="agent_id",
    )
    confidence = ConfidenceField(initial_confidence=0.5)
    related_policies = CoOccurrenceField(symmetric=True, max_edges=100)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.state_fingerprint,
    )

    _stream_name = "policy_mutations"
    _stream_partition_field = "agent_id"
    _pl_partition = "default"


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------


def compute_fingerprint(
    features: dict,
    include_fields: list = None,
    include_timestamp: bool = False,
) -> str:
    """Generate a stable fingerprint from state features.

    Creates a SHA-256 hash (truncated to 16 hex chars) from a dict of
    state features. The hash is deterministic for the same input, making
    it suitable for grouping events by state.

    Args:
        features: Dict of state features to fingerprint. Values must be
            JSON-serializable.
        include_fields: Optional list of field names to include. If None,
            all fields are included. Useful for per-model customization
            of which features define "same state."
        include_timestamp: If True, includes current hour-bucket timestamp
            for time-unique fingerprints. The bucket is the current hour
            (Unix timestamp truncated to 3600s).

    Returns:
        str: SHA-256 truncated to 16 hex chars.

    Examples:
        >>> compute_fingerprint({"task": "deploy", "env": "staging"})
        'a1b2c3d4e5f6a7b8'

        >>> compute_fingerprint({"task": "deploy", "env": "staging"},
        ...                     include_fields=["task"])
        'f8e7d6c5b4a39281'  # Different — only 'task' included

        >>> compute_fingerprint({"task": "deploy"}, include_timestamp=True)
        '1234567890abcdef'  # Different per hour
    """
    if include_fields is not None:
        features = {k: features[k] for k in include_fields}

    if include_timestamp:
        features = dict(features)
        features["__hour_bucket__"] = int(time.time()) // 3600

    # Sort keys for deterministic ordering
    canonical = json.dumps(features, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def wilson_ci_lower(successes: int, total: int, z: float = 1.96) -> float:
    """Wilson score confidence interval lower bound.

    Computes the lower bound of the Wilson score interval, which gives
    a conservative estimate of the true success rate. Unlike naive
    success/total, this handles small sample sizes correctly.

    Args:
        successes: Number of successful outcomes.
        total: Total number of outcomes.
        z: Z-score for confidence level. 1.96 = 95% CI (default).

    Returns:
        float: Lower bound of Wilson CI (0.0 to 1.0).
    """
    if total == 0:
        return 0.0
    p_hat = successes / total
    denominator = 1 + z**2 / total
    center = p_hat + z**2 / (2 * total)
    spread = z * (p_hat * (1 - p_hat) / total + z**2 / (4 * total**2)) ** 0.5
    return (center - spread) / denominator


def chi_squared_uniform(observed: list, expected_per_bucket: float) -> float:
    """Chi-squared statistic against uniform distribution.

    Tests whether observed counts across buckets differ significantly
    from a uniform distribution. Compare the returned statistic against
    CHI_SQUARED_CRITICAL_VALUES for the appropriate degrees of freedom.

    Args:
        observed: List of observed counts per bucket.
        expected_per_bucket: Expected count per bucket under uniform
            distribution (total_events / num_buckets).

    Returns:
        float: Chi-squared test statistic. Higher values indicate
            stronger deviation from uniform.
    """
    if expected_per_bucket <= 0:
        return 0.0
    return sum((o - expected_per_bucket) ** 2 / expected_per_bucket for o in observed)


def update_q_value(
    instance,
    reward: float,
    max_future_q: float = 0.0,
    alpha: float = TD_ALPHA,
    gamma: float = TD_GAMMA,
) -> float:
    """Update a PolicyEntry's Q-value via temporal difference learning.

    Atomically updates the expected_value (Q-value) in the sorted set
    using the TD(0) update rule:

        Q(s,a) ← Q(s,a) + α [r + γ max Q(s',a') - Q(s,a)]

    Args:
        instance: A saved PolicyEntry instance.
        reward: Observed reward signal (float).
        max_future_q: Maximum Q-value for next state's best action.
            Default 0.0 (no future state, terminal).
        alpha: Learning rate. Default TD_ALPHA (0.1).
        gamma: Discount factor. Default TD_GAMMA (0.95).

    Returns:
        float: The TD error (positive = better than expected,
            negative = worse than expected).

    Raises:
        ValueError: If the instance has no redis_key (unsaved).
    """
    redis_key = _get_redis_key(instance)
    sortedset_key = _get_sortedset_key(instance)

    td_error = POPOTO_REDIS_DB.eval(
        TD_UPDATE_LUA,
        1,  # num keys
        sortedset_key,  # KEYS[1]
        str(reward),  # ARGV[1]
        str(alpha),  # ARGV[2]
        str(gamma),  # ARGV[3]
        str(max_future_q),  # ARGV[4]
        redis_key,  # ARGV[5]
    )

    return float(td_error)


def initialize_q_value(instance, initial_q: float = 0.0) -> None:
    """Set the initial Q-value for a PolicyEntry in the sorted set.

    After save(), DecayingSortedField stores the current timestamp as score.
    This function overrides that with the desired initial Q-value.

    Args:
        instance: A saved PolicyEntry instance.
        initial_q: The initial Q-value to set. Default 0.0.

    Raises:
        ValueError: If the instance is unsaved.
    """
    redis_key = _get_redis_key(instance)
    sortedset_key = _get_sortedset_key(instance)
    POPOTO_REDIS_DB.zadd(sortedset_key, {redis_key: initial_q})


def _get_redis_key(instance) -> str:
    """Get the Redis key for a model instance, raising if unsaved."""
    redis_key = getattr(instance, "_redis_key", None)
    if not redis_key:
        try:
            redis_key = instance.db_key.redis_key
        except Exception:
            raise ValueError("Cannot operate on unsaved PolicyEntry")
    return redis_key


def _get_sortedset_key(instance) -> str:
    """Get the sorted set key for the expected_value field.

    Note: Accesses ``instance._meta.fields`` which is a Popoto internal.
    If the ``_meta`` structure changes in a future Popoto release, this
    will need updating. The test_q_value_update test validates the key
    format implicitly.
    """
    field = instance._meta.fields["expected_value"]
    return field.__class__.get_partitioned_sortedset_db_key(
        instance, "expected_value"
    ).redis_key


# ---------------------------------------------------------------------------
# StreamConsumer Handlers
# ---------------------------------------------------------------------------


async def crystallization_handler(entries):
    """StreamConsumer handler that detects repeated patterns and crystallizes PolicyEntry records.

    Groups events by (state_fingerprint, action_type), counts successes
    and failures, and creates a PolicyEntry when evidence threshold is met
    (min events AND Wilson CI lower bound > threshold).

    Expected entry fields (all strings per Redis Streams spec):
        - state_fingerprint: Hash identifying the state.
        - action_type: Category of the action taken.
        - outcome: "success" or "failure".
        - state_features: JSON string of original state features (optional).
        - action_spec: JSON string of action specification (optional).
        - agent_id: Agent identifier (optional, defaults to "default").

    Entries missing state_fingerprint or action_type are skipped with a warning.

    Args:
        entries: List of (entry_id, fields_dict) tuples from StreamConsumer.
    """
    if not entries:
        return

    # Group events by (state_fingerprint, action_type)
    groups = {}
    for entry_id, fields in entries:
        fp = fields.get("state_fingerprint")
        action = fields.get("action_type")

        if not fp or not action:
            logger.warning(
                "Skipping entry %s: missing state_fingerprint or action_type",
                entry_id,
            )
            continue

        key = (fp, action)
        if key not in groups:
            groups[key] = {
                "successes": 0,
                "total": 0,
                "latest_fields": fields,
            }

        groups[key]["total"] += 1
        if fields.get("outcome") == "success":
            groups[key]["successes"] += 1

    # Check each group for crystallization threshold
    for (fp, action), stats in groups.items():
        total = stats["total"]
        successes = stats["successes"]

        if total < MIN_EVENTS_FOR_CRYSTALLIZATION:
            continue

        ci_lower = wilson_ci_lower(successes, total)
        if ci_lower <= WILSON_CI_THRESHOLD:
            continue

        # Pre-check via ExistenceFilter to reduce duplicate crystallization.
        # Note: Bloom filters have a false positive rate (~1% at configured
        # error_rate=0.01), so ~1% of legitimate crystallizations may be
        # silently skipped. Acceptable for a reference recipe — applications
        # requiring zero missed crystallizations should add a secondary check.
        if not PolicyEntry.bloom.definitely_missing(PolicyEntry, fp):
            logger.debug(
                "Bloom filter says fingerprint %s may exist, skipping crystallization",
                fp,
            )
            continue

        # Crystallize: create PolicyEntry
        fields = stats["latest_fields"]
        agent_id = fields.get("agent_id", "default")

        # Parse JSON fields with fallbacks
        state_features = fields.get("state_features", "{}")
        try:
            state_features = json.loads(state_features)
        except (json.JSONDecodeError, TypeError):
            state_features = {}

        action_spec = fields.get("action_spec", "{}")
        try:
            action_spec = json.loads(action_spec)
        except (json.JSONDecodeError, TypeError):
            action_spec = {}

        policy = PolicyEntry(
            agent_id=agent_id,
            state_fingerprint=fp,
            state_features=state_features,
            action_type=action,
            action_spec=action_spec,
        )
        policy.save()

        # Initialize Q-value (overrides timestamp set by DecayingSortedField)
        try:
            initialize_q_value(policy, initial_q=ci_lower)
        except Exception as e:
            logger.warning("Failed to set initial Q-value for %s: %s", fp, e)

        logger.info(
            "Crystallized PolicyEntry: fp=%s action=%s ci=%.3f (n=%d)",
            fp,
            action,
            ci_lower,
            total,
        )


async def temporal_discovery_handler(entries):
    """StreamConsumer handler that discovers cyclical patterns from event timestamps.

    Buckets event timestamps by day-of-week (7 buckets), week-of-month
    (4 buckets), and month-of-year (12 buckets). Performs chi-squared test
    against uniform distribution. Significant clusters (p < 0.05) are
    logged as discovered temporal patterns.

    This handler identifies WHEN events tend to occur, which can be used
    to add cycles to CyclicDecayField instances in application code.

    Expected entry fields:
        - ts: Unix timestamp string (from EventStreamMixin).
        - state_fingerprint: Hash identifying the state (optional).

    Args:
        entries: List of (entry_id, fields_dict) tuples from StreamConsumer.

    Returns:
        list: Discovered cycles as (period, amplitude, phase) tuples.
            Empty list if no significant patterns found.
    """
    if not entries:
        return []

    # Collect timestamps
    timestamps = []
    for entry_id, fields in entries:
        ts_str = fields.get("ts")
        if not ts_str:
            continue
        try:
            timestamps.append(float(ts_str))
        except (ValueError, TypeError):
            continue

    if len(timestamps) < 3:
        return []

    discovered_cycles = []

    # Bucket definitions: (name, num_buckets, period_constant, bucket_fn)
    bucket_configs = [
        (
            "day_of_week",
            7,
            TemporalPeriod.WEEKLY,
            lambda ts: time.gmtime(ts).tm_wday,
        ),
        (
            "week_of_month",
            4,
            TemporalPeriod.MONTHLY,
            lambda ts: min(time.gmtime(ts).tm_mday // 7, 3),
        ),
        (
            "month_of_year",
            12,
            TemporalPeriod.YEARLY,
            lambda ts: time.gmtime(ts).tm_mon - 1,
        ),
    ]

    for name, num_buckets, period, bucket_fn in bucket_configs:
        buckets = [0] * num_buckets
        for ts in timestamps:
            idx = bucket_fn(ts)
            buckets[idx] += 1

        expected = len(timestamps) / num_buckets
        if expected < 1:
            continue

        chi2 = chi_squared_uniform(buckets, expected)
        df = num_buckets - 1
        critical = CHI_SQUARED_CRITICAL_VALUES.get(df)

        if critical is None:
            continue

        if chi2 > critical:
            # Find the peak bucket for phase calculation
            peak_bucket = buckets.index(max(buckets))
            phase = (peak_bucket / num_buckets) * 2 * math.pi

            cycle = (period, INITIAL_CYCLE_AMPLITUDE, phase)
            discovered_cycles.append(cycle)

            logger.info(
                "Temporal pattern discovered: %s chi2=%.2f > %.2f "
                "(df=%d), peak_bucket=%d, cycle=%s",
                name,
                chi2,
                critical,
                df,
                peak_bucket,
                cycle,
            )

    return discovered_cycles
