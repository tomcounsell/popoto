"""Tests for EmbeddingField.garbage_collect and sweep_stale_tempfiles.

Covers:
- ``_compute_expected_keep`` reads the canonical ``$Class:{Name}`` key
  (NOT the legacy ``{Name}:_all`` key — that data-destruction bug
  caused this whole feature to exist).
- Opt-in marker (``__embedding_garbage_collect__``) is enforced.
- Orphan files not in the expected_keep set are removed.
- Mtime guard: files newer than ``min_age_seconds`` are kept.
- Tempfile sweep: ``tmp*.npy`` older than the cutoff is removed,
  newer is kept.
- Missing embedding directory returns 0 (no exception).
- ``_index.json`` is reconciled in the same pass.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest

np = pytest.importorskip("numpy")
from src import popoto
from src.popoto.fields.content_field import ContentField, set_default_store
from src.popoto.fields.embedding_field import (
    EmbeddingField,
    _compute_expected_keep,
    _read_index,
    _write_index,
    invalidate_cache,
    set_default_provider,
)
from src.popoto.embeddings import AbstractEmbeddingProvider
from src.popoto.redis_db import POPOTO_REDIS_DB
from src.popoto.stores.filesystem import FilesystemStore


class _MockProvider(AbstractEmbeddingProvider):
    def __init__(self, dim=4):
        self._dim = dim

    def embed(self, texts, input_type=None):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    @property
    def dimensions(self):
        return self._dim

    @property
    def max_batch_size(self):
        return 32


class GcMemory(popoto.Model):
    """Test model that opts into embedding garbage collection."""

    __embedding_garbage_collect__ = True
    name = popoto.UniqueKeyField()
    content = ContentField()
    embedding = EmbeddingField(source="content")


class GcMemoryNoOptIn(popoto.Model):
    """Test model that does NOT opt in — gc must be a no-op."""

    name = popoto.UniqueKeyField()
    content = ContentField()
    embedding = EmbeddingField(source="content")


@pytest.fixture(autouse=True)
def _providers(tmp_path):
    set_default_provider(_MockProvider(dim=4))
    set_default_store(
        FilesystemStore(base_path=str(tmp_path / "content"), extension=".txt")
    )
    os.environ["POPOTO_CONTENT_PATH"] = str(tmp_path / "content")
    yield
    set_default_provider(None)
    set_default_store(None)
    invalidate_cache()
    os.environ.pop("POPOTO_CONTENT_PATH", None)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for cls in (GcMemory, GcMemoryNoOptIn):
        keys = POPOTO_REDIS_DB.smembers(cls._meta.db_class_set_key.redis_key)
        if keys:
            pipe = POPOTO_REDIS_DB.pipeline()
            for k in keys:
                pipe.delete(k)
            pipe.delete(cls._meta.db_class_set_key.redis_key)
            pipe.execute()


def _emb_dir_for(model_class) -> str:
    return os.path.join(
        os.environ["POPOTO_CONTENT_PATH"],
        ".embeddings",
        model_class.__name__,
    )


def _make_npy(path: str, mtime_offset_seconds: float = 0.0) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.save(path, np.array([0.0, 0.0, 0.0, 0.0], dtype=np.float32))
    if mtime_offset_seconds:
        old = time.time() - mtime_offset_seconds
        os.utime(path, (old, old))


# ---------------------------------------------------------------------------
# _compute_expected_keep
# ---------------------------------------------------------------------------


class TestComputeExpectedKeep:
    def test_uses_canonical_class_set_key(self):
        """B-A regression pin: canonical key is ``$Class:{Name}``.

        The legacy ``{Name}:_all`` key is empty in production. Reading
        from the wrong key would compute ``expected_keep = {}`` and
        treat every live record's embedding as orphan — a
        data-destruction bug.
        """
        doc = GcMemory(name="canon", content="hello")
        doc.save()
        try:
            redis_key = doc._redis_key or doc.db_key.redis_key
            expected = _compute_expected_keep(GcMemory)

            sha = hashlib.sha256(redis_key.encode("utf-8")).hexdigest()
            assert f"{sha}.npy" in expected
            assert len(expected) == 1
        finally:
            doc.delete()

    def test_legacy_memory_all_key_returns_empty(self):
        """B-A regression pin: ensure the legacy ``Memory:_all`` key is empty.

        We don't read from it in production code anymore, but pin a test
        that proves a query against the wrong key would yield an empty
        set — guarding against future regressions that try to "fix"
        the helper by switching back to the legacy key.
        """
        doc = GcMemory(name="legkey", content="hi")
        doc.save()
        try:
            # Live records exist (canonical key has 1)
            assert len(_compute_expected_keep(GcMemory)) == 1
            # The legacy key MUST be empty — production parity check
            legacy = POPOTO_REDIS_DB.smembers("GcMemory:_all")
            assert legacy == set() or legacy == frozenset()
        finally:
            doc.delete()

    def test_empty_class_returns_empty_set(self):
        # No saves — class set is empty
        assert _compute_expected_keep(GcMemory) == set()


# ---------------------------------------------------------------------------
# Opt-in marker
# ---------------------------------------------------------------------------


class TestOptInMarker:
    def test_no_marker_returns_zero_with_no_unlinks(self):
        """Models without ``__embedding_garbage_collect__`` MUST be no-op.

        Verified by spying on os.unlink — zero calls means the gc body
        is short-circuited before any deletion can happen, regardless
        of disk contents.
        """
        # Place a stray file in the no-opt-in model's dir
        emb_dir = _emb_dir_for(GcMemoryNoOptIn)
        os.makedirs(emb_dir, exist_ok=True)
        stray = os.path.join(emb_dir, "a" * 64 + ".npy")
        _make_npy(stray, mtime_offset_seconds=99999)

        with mock.patch("src.popoto.fields.embedding_field.os.unlink") as spy:
            removed = EmbeddingField.garbage_collect(GcMemoryNoOptIn)

        assert removed == 0
        assert (
            spy.call_count == 0
        ), "garbage_collect must NEVER unlink for non-opted-in models"
        assert os.path.exists(stray), "stray file must survive"

    def test_marker_set_enables_gc(self):
        emb_dir = _emb_dir_for(GcMemory)
        os.makedirs(emb_dir, exist_ok=True)
        stray = os.path.join(emb_dir, "f" * 64 + ".npy")
        _make_npy(stray, mtime_offset_seconds=99999)

        removed = EmbeddingField.garbage_collect(GcMemory)
        assert removed == 1
        assert not os.path.exists(stray)


# ---------------------------------------------------------------------------
# garbage_collect behavior
# ---------------------------------------------------------------------------


class TestGarbageCollect:
    def test_orphans_removed_live_records_kept(self):
        live = GcMemory(name="alive", content="present")
        live.save()
        try:
            redis_key = live._redis_key or live.db_key.redis_key
            live_path = EmbeddingField._embedding_path("GcMemory", redis_key)
            assert os.path.exists(live_path)

            emb_dir = _emb_dir_for(GcMemory)
            stray1 = os.path.join(emb_dir, "1" * 64 + ".npy")
            stray2 = os.path.join(emb_dir, "2" * 64 + ".npy")
            _make_npy(stray1, mtime_offset_seconds=99999)
            _make_npy(stray2, mtime_offset_seconds=99999)

            # Age the live file beyond the mtime guard so it is reachable
            old = time.time() - 99999
            os.utime(live_path, (old, old))

            removed = EmbeddingField.garbage_collect(GcMemory)
            assert removed == 2
            assert os.path.exists(live_path)
            assert not os.path.exists(stray1)
            assert not os.path.exists(stray2)
        finally:
            live.delete()

    def test_mtime_guard_protects_recent_orphans(self):
        emb_dir = _emb_dir_for(GcMemory)
        os.makedirs(emb_dir, exist_ok=True)
        recent = os.path.join(emb_dir, "9" * 64 + ".npy")
        _make_npy(recent, mtime_offset_seconds=10)  # 10s old, fresh

        removed = EmbeddingField.garbage_collect(GcMemory, min_age_seconds=300)
        assert removed == 0
        assert os.path.exists(recent), "fresh orphan must survive mtime guard"

    def test_missing_directory_returns_zero(self):
        # No saves, no directory created
        removed = EmbeddingField.garbage_collect(GcMemory)
        assert removed == 0

    def test_skips_tmp_files(self):
        emb_dir = _emb_dir_for(GcMemory)
        os.makedirs(emb_dir, exist_ok=True)
        tmpfile = os.path.join(emb_dir, "tmpABC123.npy")
        _make_npy(tmpfile, mtime_offset_seconds=99999)

        removed = EmbeddingField.garbage_collect(GcMemory)
        assert removed == 0, "garbage_collect must not touch tmp*.npy files"
        assert os.path.exists(tmpfile)

    def test_index_reconciled(self):
        emb_dir = _emb_dir_for(GcMemory)
        os.makedirs(emb_dir, exist_ok=True)
        # Pre-seed the index with an orphan entry
        _write_index("GcMemory", {"orphan_filename.npy": "ghost:redis:key"})
        stray = os.path.join(emb_dir, "orphan_filename.npy")
        _make_npy(stray, mtime_offset_seconds=99999)

        EmbeddingField.garbage_collect(GcMemory)
        idx = _read_index("GcMemory")
        assert "orphan_filename.npy" not in idx


# ---------------------------------------------------------------------------
# sweep_stale_tempfiles
# ---------------------------------------------------------------------------


class TestSweepStaleTempfiles:
    def test_old_tmp_removed(self):
        emb_dir = _emb_dir_for(GcMemory)
        os.makedirs(emb_dir, exist_ok=True)
        old_tmp = os.path.join(emb_dir, "tmpZZZ.npy")
        _make_npy(old_tmp, mtime_offset_seconds=7200)  # 2 hours ago

        removed = EmbeddingField.sweep_stale_tempfiles(GcMemory, max_age_seconds=3600)
        assert removed == 1
        assert not os.path.exists(old_tmp)

    def test_recent_tmp_kept(self):
        emb_dir = _emb_dir_for(GcMemory)
        os.makedirs(emb_dir, exist_ok=True)
        new_tmp = os.path.join(emb_dir, "tmpQQQ.npy")
        _make_npy(new_tmp, mtime_offset_seconds=10)

        removed = EmbeddingField.sweep_stale_tempfiles(GcMemory, max_age_seconds=3600)
        assert removed == 0
        assert os.path.exists(new_tmp)

    def test_missing_directory_returns_zero(self):
        removed = EmbeddingField.sweep_stale_tempfiles(GcMemory)
        assert removed == 0
