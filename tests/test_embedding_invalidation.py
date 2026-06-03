"""Tests for EmbeddingField cross-process cache invalidation (issue #403).

Covers the three POPOTO_EMBEDDING_INVALIDATION modes:
- pubsub: a peer PUBLISH drops the local cache (real subscriber thread vs DB 15)
- mtime: an on-disk _version bump forces a reload on next load_embeddings()
- none: no listener thread, no PUBLISH

Plus the pubsub backstop (listener-down → disk _version check), loopback
self-skip (per-process _WORKER_ID, not pid), read-path no-version-bump, and
the stop_invalidation_listeners() teardown helper.

The invalidation mode is a module-level constant read at import. Tests that
need a non-default mode monkeypatch the module attribute directly (and the
helper closures read it live), then restore it.
"""

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest

np = pytest.importorskip("numpy")

from src import popoto
from src.popoto.fields import embedding_field as ef
from src.popoto.fields.content_field import ContentField, set_default_store
from src.popoto.fields.embedding_field import (
    EmbeddingField,
    set_default_provider,
    invalidate_cache,
    stop_invalidation_listeners,
    _read_index,
    _write_index,
)
from src.popoto.stores.filesystem import FilesystemStore
from src.popoto.embeddings import AbstractEmbeddingProvider
from src.popoto.redis_db import POPOTO_REDIS_DB


class MockProvider(AbstractEmbeddingProvider):
    """Deterministic mock embedding provider."""

    def __init__(self, dim=4):
        self._dim = dim

    def embed(self, texts, input_type=None):
        out = []
        for text in texts:
            seed = sum(ord(c) for c in text) % 1000
            rng = np.random.RandomState(seed)
            out.append(rng.randn(self._dim).tolist())
        return out

    @property
    def dimensions(self):
        return self._dim

    @property
    def max_batch_size(self):
        return 32


class InvDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    content = ContentField()
    embedding = EmbeddingField(source="content")


@pytest.fixture(autouse=True)
def setup_providers(tmp_path):
    provider = MockProvider(dim=4)
    set_default_provider(provider)
    store = FilesystemStore(base_path=str(tmp_path / "content"), extension=".txt")
    set_default_store(store)
    os.environ["POPOTO_CONTENT_PATH"] = str(tmp_path / "content")
    yield provider
    set_default_provider(None)
    set_default_store(None)
    invalidate_cache()
    stop_invalidation_listeners()
    os.environ.pop("POPOTO_CONTENT_PATH", None)


@pytest.fixture
def mode(monkeypatch):
    """Return a setter that flips the module-level invalidation mode safely."""

    def _set(value):
        monkeypatch.setattr(ef, "_INVALIDATION_MODE", value)

    return _set


def _cleanup_redis():
    keys = POPOTO_REDIS_DB.smembers(InvDoc._meta.db_class_set_key.redis_key)
    if keys:
        pipe = POPOTO_REDIS_DB.pipeline()
        for k in keys:
            pipe.delete(k)
        pipe.delete(InvDoc._meta.db_class_set_key.redis_key)
        pipe.execute()


@pytest.fixture(autouse=True)
def cleanup():
    yield
    _cleanup_redis()


# --- Conftest fixture is actually collected (not just text) ---


def test_listener_registry_empty_at_test_start():
    """If the conftest autouse teardown fixture is collected and running, no
    listener threads leak into a fresh test."""
    assert ef._listener_threads == {}


# --- none mode ---


def test_none_mode_starts_no_listener_and_no_publish(mode, monkeypatch):
    mode("none")
    published = []
    monkeypatch.setattr(
        POPOTO_REDIS_DB,
        "publish",
        lambda ch, msg: published.append((ch, msg)),
    )

    doc = InvDoc(name="none1", content="hello world")
    doc.save()  # on_save → _publish_invalidation (should be a no-op in none mode)

    EmbeddingField.load_embeddings(InvDoc)  # would start a listener in pubsub

    assert ef._listener_threads == {}
    assert published == []


# --- pubsub mode: real subscriber thread drops the cache on peer PUBLISH ---


def test_pubsub_peer_publish_invalidates_cache(mode):
    mode("pubsub")
    doc = InvDoc(name="pub1", content="alpha")
    doc.save()

    matrix, keys = EmbeddingField.load_embeddings(InvDoc)
    assert matrix is not None
    assert "InvDoc" in ef._embedding_cache
    # Listener thread should be running for this class.
    assert "InvDoc" in ef._listener_threads
    assert ef._listener_threads["InvDoc"].daemon is True

    # Simulate a peer write: publish from a DIFFERENT worker id.
    channel = "popoto:embedding:invalidate:InvDoc"
    POPOTO_REDIS_DB.publish(channel, "InvDoc:peerworkerid_not_ours")

    # Poll up to ~2s for the subscriber thread to drop the cache.
    deadline = time.time() + 2.0
    while time.time() < deadline and "InvDoc" in ef._embedding_cache:
        time.sleep(0.05)

    assert "InvDoc" not in ef._embedding_cache, "peer publish did not drop cache"


# --- pubsub loopback self-skip uses the per-process UUID, not pid ---


def test_pubsub_loopback_self_skip(mode):
    mode("pubsub")
    doc = InvDoc(name="loop1", content="beta")
    doc.save()
    EmbeddingField.load_embeddings(InvDoc)
    assert "InvDoc" in ef._embedding_cache

    channel = "popoto:embedding:invalidate:InvDoc"
    # Our own loopback (worker-id == _WORKER_ID): must NOT drop the cache.
    POPOTO_REDIS_DB.publish(channel, f"InvDoc:{ef._WORKER_ID}")
    time.sleep(0.4)
    assert "InvDoc" in ef._embedding_cache, "loopback message wrongly dropped cache"

    # A different worker-id: must drop the cache.
    POPOTO_REDIS_DB.publish(channel, "InvDoc:some_other_worker")
    deadline = time.time() + 2.0
    while time.time() < deadline and "InvDoc" in ef._embedding_cache:
        time.sleep(0.05)
    assert "InvDoc" not in ef._embedding_cache, "peer message did not drop cache"


# --- mtime / version-counter mode ---


def test_mtime_mode_version_bump_forces_reload(mode):
    mode("mtime")
    doc = InvDoc(name="mt1", content="gamma")
    doc.save()

    matrix, keys = EmbeddingField.load_embeddings(InvDoc)
    assert matrix is not None
    assert "InvDoc" in ef._embedding_cache
    # No listener thread in mtime mode.
    assert ef._listener_threads == {}

    cached_version = ef._embedding_cache["InvDoc"]["index_version"]

    # Simulate a peer write bumping the on-disk version counter.
    index = _read_index("InvDoc")
    _write_index("InvDoc", index)  # default bump_version=True
    new_version = _read_index("InvDoc").get("_version")
    assert new_version == cached_version + 1

    # Force the mtime pre-check to register a change even on coarse-grained FS.
    entry = ef._embedding_cache["InvDoc"]
    entry["index_mtime"] = (entry.get("index_mtime") or 0) - 10

    EmbeddingField.load_embeddings(InvDoc)  # should detect staleness, reload
    # After reload the cached version matches the new on-disk version.
    assert ef._embedding_cache["InvDoc"]["index_version"] == new_version


def test_mtime_same_tick_version_still_triggers(mode):
    """Even if mtime is unchanged-looking, a differing _version forces reload.

    We assert the authoritative comparison is the integer version: drop the
    cache when version differs regardless of the (possibly coarse) mtime.
    """
    mode("mtime")
    doc = InvDoc(name="mt2", content="delta")
    doc.save()
    EmbeddingField.load_embeddings(InvDoc)
    entry = ef._embedding_cache["InvDoc"]

    # Bump the on-disk version but make the cached mtime stale so the cheap
    # pre-check fires and the version comparison (the granularity-proof part)
    # decides the reload.
    index = _read_index("InvDoc")
    _write_index("InvDoc", index)
    entry["index_mtime"] = -1.0  # guaranteed != current mtime

    EmbeddingField.load_embeddings(InvDoc)
    assert ef._embedding_cache["InvDoc"]["index_version"] == _read_index("InvDoc").get(
        "_version"
    )


# --- pubsub backstop: listener down → disk _version check still invalidates ---


def test_pubsub_backstop_when_listener_absent(mode, monkeypatch):
    mode("pubsub")
    # Simulate a failed listener start: load never registers a thread.
    monkeypatch.setattr(ef, "_start_invalidation_listener", lambda name: None)

    doc = InvDoc(name="back1", content="epsilon")
    doc.save()
    EmbeddingField.load_embeddings(InvDoc)
    assert "InvDoc" not in ef._listener_threads  # listener never came up
    entry = ef._embedding_cache["InvDoc"]
    cached_version = entry["index_version"]

    # Peer write bumps the on-disk version.
    index = _read_index("InvDoc")
    _write_index("InvDoc", index)
    entry["index_mtime"] = -1.0  # force the cheap pre-check to fire

    EmbeddingField.load_embeddings(InvDoc)  # backstop should reload
    assert ef._embedding_cache["InvDoc"]["index_version"] == cached_version + 1


# --- read path must not bump the version ---


def test_read_path_no_version_bump(mode):
    mode("mtime")
    doc = InvDoc(name="read1", content="zeta")
    doc.save()
    EmbeddingField.load_embeddings(InvDoc)
    version_after_save = _read_index("InvDoc").get("_version")

    # A pure read that triggers the legacy-fallback _write_index(bump=False)
    # must not advance the counter. Drop the cache and reload several times.
    for _ in range(3):
        invalidate_cache("InvDoc")
        EmbeddingField.load_embeddings(InvDoc)
        assert _read_index("InvDoc").get("_version") == version_after_save


def test_write_index_bump_version_flag():
    """_write_index bumps _version by default and not when bump_version=False."""
    name = "InvDoc"
    _write_index(name, {})  # default True → _version = 1
    assert _read_index(name).get("_version") == 1
    idx = _read_index(name)
    _write_index(name, idx, bump_version=False)
    assert _read_index(name).get("_version") == 1  # unchanged
    idx = _read_index(name)
    _write_index(name, idx)  # True again
    assert _read_index(name).get("_version") == 2


# --- teardown helper ---


def test_stop_invalidation_listeners_clears_registry(mode):
    mode("pubsub")
    doc = InvDoc(name="td1", content="eta")
    doc.save()
    EmbeddingField.load_embeddings(InvDoc)
    assert "InvDoc" in ef._listener_threads
    thread = ef._listener_threads["InvDoc"]

    stop_invalidation_listeners()
    assert ef._listener_threads == {}

    # The stopped thread should report not-alive shortly after stop().
    deadline = time.time() + 2.0
    while time.time() < deadline and thread.is_alive():
        time.sleep(0.05)
    assert not thread.is_alive()
