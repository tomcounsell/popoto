"""Copy-me template for ad-hoc popoto repro scripts.

WHY THIS FILE EXISTS
---------------------
``popoto.redis_db`` binds its global ``POPOTO_REDIS_DB`` client from the
``REDIS_URL`` environment variable AT IMPORT TIME, falling back to database 0
when ``REDIS_URL`` is unset. Setting ``REDIS_URL`` (or any other env var)
*after* ``import popoto`` has already run does nothing — the connection pool
is already built. Database 0 on this machine is a LIVE agent data store, not
a scratch area. Twice, ad-hoc repro scripts defaulted to database 0 and ran a
blanket flush there, destroying live state (popoto issue #577).

This script demonstrates the safe ordering:

1. ``os.environ.setdefault("REDIS_URL", ...)`` to a NON-ZERO database,
   BEFORE ``import popoto``. This honors an already-exported ``REDIS_URL``
   (setdefault is a no-op if the caller already set one) while still
   guaranteeing a safe default when nothing is set.
2. Only *after* import, resolve which database actually got bound and
   verify it is not 0, before running a single Redis command.
3. Use TARGETED deletes (scan a narrow key prefix, then delete just those
   keys) instead of any blanket flush.

Note that popoto's own ``POPOTO_REDIS_DB`` client (a ``GuardedRedis``) now
refuses ``FLUSHDB`` when bound to database 0, and refuses ``FLUSHALL``
outright regardless of database, with an escape hatch env var
``POPOTO_ALLOW_DB0_FLUSH=1`` for the rare case that override is intentional.
That guard protects popoto's own client, but it does NOT protect
``redis-cli`` or any other raw client you might reach for in a terminal --
those remain completely unguarded. This script's own belt-and-suspenders
checks (safe default + post-import verification) are what keep *this*
script safe; don't rely on the GuardedRedis guard alone when reaching for a
different tool.

See: popoto issue #577.
"""

import os
import sys

# Step 1: pick a safe, non-zero database BEFORE importing popoto. setdefault
# means an already-exported REDIS_URL from the caller's shell is honored
# unchanged; we only supply this as a fallback.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

import popoto  # noqa: E402  (import must follow the REDIS_URL setdefault above)
from popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

# Step 2: resolve the database popoto ACTUALLY bound to (not just what we
# asked for -- a stray REDIS_URL from the environment could still point
# elsewhere) and refuse to proceed if it resolves to 0. This check must run
# before any Redis command below.
bound_db = POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db", 0) or 0
if bound_db == 0:
    sys.exit(
        "REFUSING TO RUN: popoto is bound to database 0, the LIVE agent "
        "data store. Set REDIS_URL to a non-zero database before running "
        "this script. See popoto issue #577."
    )

print(f"popoto bound to database {bound_db} -- safe to proceed.")

# Step 3: demonstrate a TARGETED delete instead of any blanket flush. We
# scope the scan to a narrow, script-specific key prefix so this script can
# never touch keys it did not create itself, even if run against a shared
# database that has other data in it.
SCRATCH_PREFIX = "scratch_repro_577:"

# Create a couple of throwaway keys under our own prefix to have something
# to clean up.
POPOTO_REDIS_DB.set(f"{SCRATCH_PREFIX}example:1", "value")
POPOTO_REDIS_DB.set(f"{SCRATCH_PREFIX}example:2", "value")

# Targeted deletion: scan only keys under our prefix, then delete exactly
# those keys. This is the pattern to copy -- never flushdb()/flushall() an
# ad-hoc repro script's way out of cleanup.
keys_to_delete = list(POPOTO_REDIS_DB.scan_iter(match=f"{SCRATCH_PREFIX}*", count=100))
if keys_to_delete:
    POPOTO_REDIS_DB.delete(*keys_to_delete)
    print(f"Deleted {len(keys_to_delete)} scratch key(s) under {SCRATCH_PREFIX!r}.")
else:
    print("No scratch keys found to delete.")

print("Done. This script never called flushdb() or flushall().")
