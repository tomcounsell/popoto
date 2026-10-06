"""Expire a saved record *now*, on either conformance leg, without sleeping
(#772).

The parity tests for record expiry used to save with a one-second TTL and
``time.sleep(1.6)`` past it, so whatever the test did between the save and
the sleep (a confidence update, an edge link, a ledger entry, a cycle
adjustment) raced the record's expiry, and a loaded machine could lose that
race. :func:`lapse` makes the lapse a step of the test instead: the record is
saved with a TTL long enough to outlive any setup, and its expiry instant is
then moved into the past on that leg's own clock, leaving the state an
elapsed TTL leaves and nothing more:

* **Redis:** ``PEXPIREAT`` a past instant. The server drops the hash -- and
  only the hash, since the save issues ``EXPIRE`` on nothing else -- leaving
  its index entries and companion keys behind exactly as an elapsed TTL does.
* **Postgres:** ``_expires_at`` set just before the server's *now*
  (``ttl.now_sql()``, so a ``frozen_clock`` is honoured). The read filter
  hides the row, a save over the key purges it, and the reaper sees it as
  expired, exactly as when ``now + ttl`` has passed.

Each leg first checks that the save really gave the record an expiry, so a
save path that stopped writing one still fails the test.
"""

import popoto

#: Seconds a record meant to expire is saved to live: long enough that it
#: cannot lapse on its own while a test is still setting up, however loaded
#: the machine. Tests end its life with :func:`lapse`.
SHORT = 60


def lapse(backend, rec):
    """Make the TTL ``rec`` was saved with run out now.

    ``backend`` is the conformance ``backend`` fixture, or the PG-only
    tests' ``pg`` fixture (a ``PostgresBackend``, which has no ``is_redis``).
    """
    key = rec.db_key.redis_key
    if getattr(backend, "is_redis", False):
        client = popoto.get_redis()
        assert client.pttl(key) > 0, "the save set no TTL on the hash"
        assert client.pexpireat(key, 1)  # epoch + 1 ms: long past
        assert client.exists(key) == 0
        return
    from popoto.backends import get_backend
    from popoto.backends.postgres import ttl as ttl_mod

    pg = get_backend(type(rec))
    table = pg._table(type(rec)._meta.spec)
    col = ttl_mod.EXPIRES_COL
    _rows, count = pg._run(
        f'UPDATE {table.qualified} SET "{col}" = {ttl_mod.now_sql()} - 1 '
        f'WHERE "_pk" = %s AND "{col}" IS NOT NULL',
        [key],
        write=True,
    )
    assert count == 1, "the save set no _expires_at on the row"
