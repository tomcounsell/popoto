"""Entry point for running Popoto Kitchen as a module.

Usage:
    python -m popoto_kitchen
    python -m popoto_kitchen --seed  # Seed sample data first
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        description="Popoto Kitchen - Interactive TUI Demo"
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Seed the database with sample data before starting",
    )
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Only seed the database, don't start the app",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear all existing data before seeding",
    )
    args = parser.parse_args()

    # Check Redis connection first
    try:
        from popoto.redis_db import POPOTO_REDIS_DB

        POPOTO_REDIS_DB.ping()
    except Exception as e:
        print(f"Error: Cannot connect to Redis - {e}")
        print("Make sure Redis is running on localhost:6379 or set REDIS_URL")
        sys.exit(1)

    if args.seed or args.seed_only or args.clear:
        from .seed import seed_database, clear_database

        if args.clear:
            print("Clearing existing data...")
            clear_database()

        if args.seed or args.seed_only:
            print("Seeding database...")
            seed_database()
            print("Done!")

        if args.seed_only:
            return

    from .app import PopotoKitchen

    app = PopotoKitchen()
    app.run()


if __name__ == "__main__":
    main()
