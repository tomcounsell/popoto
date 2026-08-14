"""Tests for ContentField — filesystem-backed content storage.

Tests cover:
- Save stores content on filesystem, reference in Redis
- Lazy-load on attribute access from queried model
- Save/load roundtrip preserving content
- None and empty string handling
- Update replaces content, archives previous version
- on_delete is no-op (append-only)
- ContentField with custom extension
- Error on missing content file
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.content_field import ContentField, set_default_store
from src.popoto.stores.filesystem import FilesystemStore
from src.popoto.redis_db import POPOTO_REDIS_DB

# --- Test Models ---


class ContentDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    body = ContentField()


class ContentMdDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    body = ContentField()


# --- Fixtures ---


@pytest.fixture(autouse=True)
def setup_store(tmp_path):
    """Use a temp directory for content storage in all tests."""
    store = FilesystemStore(base_path=str(tmp_path), extension=".txt")
    set_default_store(store)
    yield store
    set_default_store(None)


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up Redis keys after each test."""
    yield
    for model_class in [ContentDoc, ContentMdDoc]:
        keys = POPOTO_REDIS_DB.smembers(model_class._meta.db_class_set_key.redis_key)
        if keys:
            pipe = POPOTO_REDIS_DB.pipeline()
            for k in keys:
                pipe.delete(k)
            pipe.delete(model_class._meta.db_class_set_key.redis_key)
            pipe.execute()
        # Clean up special field keys
        for pattern in [f"${model_class.__name__}*", f"$*:{model_class.__name__}*"]:
            for k in POPOTO_REDIS_DB.scan_iter(match=pattern):
                POPOTO_REDIS_DB.delete(k)


class TestContentFieldSave:
    """Test saving content to filesystem via ContentField."""

    def test_save_stores_reference_in_redis(self):
        doc = ContentDoc(name="test1", body="Hello, world!")
        doc.save()
        # After save, body attribute should be a $CF reference
        raw = doc.__dict__.get("body", "")
        assert raw.startswith("$CF:")

    def test_save_writes_content_to_filesystem(self, setup_store):
        doc = ContentDoc(name="test2", body="File content here")
        doc.save()
        # Check that a file exists in the store
        ref = doc.__dict__["body"]
        content = setup_store.load(ref)
        assert content == b"File content here"

    def test_save_with_none_body(self):
        doc = ContentDoc(name="test3", body=None)
        doc.save()
        assert doc.__dict__.get("body") is None

    def test_save_with_empty_string(self):
        doc = ContentDoc(name="test4", body="")
        doc.save()
        assert doc.__dict__.get("body") == ""


class TestContentFieldLoad:
    """Test lazy-loading content from filesystem."""

    def test_query_returns_lazy_loaded_content(self):
        doc = ContentDoc(name="lazy1", body="Lazy load test content")
        doc.save()

        # Query back
        loaded = ContentDoc.query.get(name="lazy1")
        # Accessing .body should lazy-load from filesystem
        assert loaded.body == "Lazy load test content"

    def test_multiple_accesses_use_cache(self):
        doc = ContentDoc(name="cache1", body="Cached content")
        doc.save()

        loaded = ContentDoc.query.get(name="cache1")
        # First access
        result1 = loaded.body
        # Second access should use cache
        result2 = loaded.body
        assert result1 == result2 == "Cached content"

    def test_none_body_loads_as_none(self):
        doc = ContentDoc(name="none1", body=None)
        doc.save()

        loaded = ContentDoc.query.get(name="none1")
        assert loaded.body is None


class TestContentFieldUpdate:
    """Test updating content."""

    def test_update_content_changes_file(self, setup_store):
        doc = ContentDoc(name="upd1", body="Version 1")
        doc.save()
        ref1 = doc.__dict__["body"]

        doc.body = "Version 2"
        doc.save()
        ref2 = doc.__dict__["body"]

        # References should differ (different hashes)
        assert ref1 != ref2

        # Current content should be Version 2
        loaded = ContentDoc.query.get(name="upd1")
        assert loaded.body == "Version 2"

    def test_save_same_content_keeps_same_hash(self, setup_store):
        doc = ContentDoc(name="same1", body="Unchanged")
        doc.save()
        ref1 = doc.__dict__["body"]
        hash1 = ref1.split(":")[1]

        doc.body = "Unchanged"
        doc.save()
        ref2 = doc.__dict__["body"]
        hash2 = ref2.split(":")[1]

        assert hash1 == hash2


class TestContentFieldDelete:
    """Test that on_delete is a no-op."""

    def test_delete_model_does_not_remove_file(self, setup_store):
        doc = ContentDoc(name="del1", body="Keep this file")
        doc.save()
        ref = doc.__dict__["body"]

        # File exists
        assert setup_store.exists(ref)

        # Delete the model
        doc.delete()

        # File should still exist (append-only)
        assert setup_store.exists(ref)


class TestContentFieldCustomExtension:
    """Test ContentField with custom file extension."""

    def test_md_extension(self, tmp_path):
        md_store = FilesystemStore(base_path=str(tmp_path), extension=".md")
        # Manually set the store on the field
        for fname, field in ContentMdDoc._meta.fields.items():
            if isinstance(field, ContentField):
                field._store = md_store

        doc = ContentMdDoc(name="md1", body="# Markdown Title")
        doc.save()

        loaded = ContentMdDoc.query.get(name="md1")
        assert loaded.body == "# Markdown Title"

        # Clean up field store
        for fname, field in ContentMdDoc._meta.fields.items():
            if isinstance(field, ContentField):
                field._store = None


class TestContentFieldErrors:
    """Test error handling."""

    def test_missing_file_raises_on_access(self):
        doc = ContentDoc(name="err1", body="Will be deleted")
        doc.save()

        loaded = ContentDoc.query.get(name="err1")
        # Replace stored reference with a fake one and clear the content cache
        loaded.__dict__["body"] = "$CF:0000000000000000:ContentDoc/nonexistent.txt"
        loaded.__dict__.pop("_content_cache_body", None)

        with pytest.raises(FileNotFoundError, match="Content file missing"):
            _ = loaded.body


def get_default_store_for_test():
    """Helper to get the current default store."""
    from src.popoto.fields.content_field import get_default_store

    return get_default_store()
