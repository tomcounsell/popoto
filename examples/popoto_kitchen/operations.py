"""Operational workflow demos for Popoto Kitchen.

Demonstrates v1.4.4 features that are useful for production operations:
- get_many() for bulk-loading instances in a single pipeline call
- check_indexes() for read-only index health checks
- clean_indexes() for removing orphaned index entries
- get_data_hash_key() for inspecting companion hash key structure

Run via: python -m popoto_kitchen --ops
"""

from popoto import ConfidenceField

from .models import (
    Customer,
    Driver,
    MenuItem,
    Order,
    Restaurant,
    ReviewScore,
)


def demo_get_many():
    """Demonstrate bulk-loading multiple model instances with get_many().

    get_many() uses a Redis pipeline to batch HGETALL calls, reducing N
    sequential round-trips to a single pipelined round-trip. Input order
    is preserved in the result list.
    """
    print("=" * 60)
    print("DEMO: get_many() - Bulk Instance Loading")
    print("=" * 60)

    # Collect some order keys to bulk-load
    all_orders = Order.query.filter(limit=5)
    if not all_orders:
        print("  No orders found. Run --seed first.")
        return

    # Extract the Redis keys from the order instances
    order_keys = [order.db_key.redis_key for order in all_orders]
    print(f"\n  Fetching {len(order_keys)} orders by key in one pipeline call...")
    for key in order_keys:
        print(f"    - {key}")

    # get_many() returns instances in the same order as the input keys.
    # None appears at positions where the key was missing.
    loaded_orders = Order.query.get_many(redis_keys=order_keys)
    print(f"\n  Results ({len(loaded_orders)} items):")
    for i, order in enumerate(loaded_orders):
        if order is not None:
            print(f"    [{i}] {order} - ${order.total:.2f}")
        else:
            print(f"    [{i}] None (key not found)")

    # Demonstrate skip_none=True mode: filters out missing entries
    print("\n  With skip_none=True (filters out None entries):")
    # Add a fake key to show the difference
    keys_with_missing = order_keys + ["Order:nonexistent-key"]
    loaded = Order.query.get_many(redis_keys=keys_with_missing, skip_none=True)
    print(f"    Input: {len(keys_with_missing)} keys")
    print(f"    Output: {len(loaded)} instances (None entries filtered)")


def demo_check_and_clean_indexes():
    """Demonstrate check_indexes() and clean_indexes() for index health.

    check_indexes() is a read-only health check that scans all index types
    (class set, key fields, sorted fields, geo fields, composite indexes)
    and counts orphaned entries -- references to instances that no longer
    exist in Redis.

    clean_indexes() is the write counterpart: it removes orphaned entries
    found by the same scan logic. Safe to run in production during
    low-traffic periods.
    """
    print("\n" + "=" * 60)
    print("DEMO: check_indexes() / clean_indexes() - Index Health")
    print("=" * 60)

    all_models = [Restaurant, MenuItem, Customer, Driver, Order, ReviewScore]

    for model in all_models:
        print(f"\n  Checking {model.__name__}...")
        result = model.check_indexes()

        total_orphans = result.get("total", 0)
        if total_orphans == 0:
            print("    All indexes healthy (0 orphans)")
        else:
            print(f"    Found {total_orphans} orphaned index entries:")
            if result.get("class_set"):
                print(f"      class_set: {result['class_set']}")
            for field_type in ("key_fields", "sorted_fields", "geo_fields"):
                field_data = result.get(field_type, {})
                for field_name, count in field_data.items():
                    if count > 0:
                        print(f"      {field_type}.{field_name}: {count}")

            # Clean up the orphans
            print("    Cleaning orphaned entries...")
            removed = model.clean_indexes()
            print(f"    Removed {removed} orphaned index entries")


def demo_companion_hash_keys():
    """Demonstrate get_data_hash_key() for inspecting Redis key structure.

    ConfidenceField stores metadata in a companion Redis hash separate from
    the model instance. get_data_hash_key() reveals the actual Redis key used,
    which is useful for monitoring, debugging, and direct Redis inspection.

    When partition_by is set, each partition gets its own hash key, keeping
    hash sizes manageable.
    """
    print("\n" + "=" * 60)
    print("DEMO: get_data_hash_key() - Companion Hash Key Inspection")
    print("=" * 60)

    # Find some ReviewScore instances to inspect
    reviews = ReviewScore.query.filter(limit=5)
    if not reviews:
        print("  No ReviewScore instances found. Run --seed first.")
        return

    score_field = ReviewScore._meta.fields["score"]

    print("\n  ReviewScore uses ConfidenceField(partition_by='restaurant')")
    print("  Each restaurant gets its own companion hash in Redis:\n")

    seen_keys = set()
    for review in reviews:
        # get_data_hash_key() shows the actual Redis key for this instance's
        # confidence data. With partition_by="restaurant", the key includes
        # the restaurant name.
        hash_key = score_field.get_data_hash_key(review, "score")
        instance_key = review.db_key.redis_key

        print(f"    Instance: {instance_key}")
        print(f"    Hash key: {hash_key}")

        # Read the confidence metadata
        data = ConfidenceField.get_confidence_data(review, "score")
        print(f"    Confidence: {data['confidence']:.4f}")
        print(
            f"    Evidence: {data['evidence_count']} "
            f"(+{data['corroborations']} / -{data['contradictions']})"
        )
        print()

        seen_keys.add(hash_key)

    print(f"  Unique companion hash keys seen: {len(seen_keys)}")
    print("  (One per restaurant partition)")

    # Also demonstrate get_data_hash_key_from_values() for query paths
    print("\n  Building hash key from explicit partition values:")
    explicit_key = score_field.get_data_hash_key_from_values(
        ReviewScore, "score", restaurant=reviews[0].restaurant
    )
    print(f"    restaurant='{reviews[0].restaurant}' -> {explicit_key}")


def run_all():
    """Run all operational demos with labeled output."""
    print("\nPopoto Kitchen - v1.4.4 Operations Demo")
    print("=" * 60)

    demo_get_many()
    demo_check_and_clean_indexes()
    demo_companion_hash_keys()

    print("\n" + "=" * 60)
    print("All operations demos complete.")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
