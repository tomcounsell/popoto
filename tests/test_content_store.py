"""Tests for AbstractContentStore and FilesystemStore.

Tests cover:
- FilesystemStore save/load/delete lifecycle
- Content-addressable storage (same content = same hash)
- Atomic writes (temp file + rename)
- Version archiving on content update
- Reference string format ($CF:{hash}:{relative_path})
- exists() check for live and archived content
- Error handling: missing content, invalid references
- Custom base_path and extension
- Filename sanitization for unsafe characters
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src.popoto.stores import AbstractContentStore
from src.popoto.stores.filesystem import FilesystemStore


@pytest.fixture
def store(tmp_path):
    """Create a FilesystemStore with a temp directory base path."""
    return FilesystemStore(base_path=str(tmp_path))


@pytest.fixture
def md_store(tmp_path):
    """Create a FilesystemStore with .md extension."""
    return FilesystemStore(base_path=str(tmp_path), extension=".md")


class TestFilesystemStoreBasics:
    """Test basic save/load/delete lifecycle."""

    def test_save_returns_reference_string(self, store):
        ref = store.save(b"hello world", key="greeting", model_class_name="Memory")
        assert ref.startswith("$CF:")
        assert "Memory/greeting.txt" in ref

    def test_save_and_load_roundtrip(self, store):
        content = b"the quick brown fox"
        ref = store.save(content, key="fox", model_class_name="TestModel")
        loaded = store.load(ref)
        assert loaded == content

    def test_load_missing_reference_raises(self, store):
        with pytest.raises(FileNotFoundError):
            store.load("$CF:deadbeef:Missing/file.txt")

    def test_delete_removes_live_file(self, store):
        ref = store.save(b"delete me", key="temp", model_class_name="TestModel")
        assert store.exists(ref)
        store.delete(ref)
        # Live file is gone; exists checks both live and version
        live_path = os.path.join(store.base_path, "TestModel", "temp.txt")
        assert not os.path.exists(live_path)

    def test_delete_missing_file_raises(self, store):
        with pytest.raises(FileNotFoundError):
            store.delete("$CF:deadbeef:Missing/file.txt")

    def test_exists_returns_true_for_saved_content(self, store):
        ref = store.save(b"check me", key="item", model_class_name="TestModel")
        assert store.exists(ref) is True

    def test_exists_returns_false_for_unknown_reference(self, store):
        assert store.exists("$CF:unknown:TestModel/missing.txt") is False

    def test_exists_handles_invalid_reference(self, store):
        assert store.exists("not-a-reference") is False


class TestContentAddressableStorage:
    """Test SHA-256 content addressing."""

    def test_same_content_produces_same_hash(self, store):
        ref1 = store.save(b"identical", key="a", model_class_name="M")
        ref2 = store.save(b"identical", key="b", model_class_name="M")
        # Same hash in reference, different paths
        hash1 = ref1.split(":")[1]
        hash2 = ref2.split(":")[1]
        assert hash1 == hash2

    def test_different_content_produces_different_hash(self, store):
        ref1 = store.save(b"content A", key="a", model_class_name="M")
        ref2 = store.save(b"content B", key="b", model_class_name="M")
        hash1 = ref1.split(":")[1]
        hash2 = ref2.split(":")[1]
        assert hash1 != hash2

    def test_hash_is_sha256_hex(self, store):
        import hashlib

        content = b"test content"
        ref = store.save(content, key="item", model_class_name="M")
        ref_hash = ref.split(":")[1]
        expected = hashlib.sha256(content).hexdigest()
        assert ref_hash == expected


class TestVersionArchiving:
    """Test automatic archiving of previous content versions."""

    def test_update_archives_previous_version(self, store):
        # Save initial content
        ref1 = store.save(b"version 1", key="doc", model_class_name="Notes")
        hash1 = ref1.split(":")[1]

        # Update with new content
        ref2 = store.save(b"version 2", key="doc", model_class_name="Notes")
        hash2 = ref2.split(":")[1]
        assert hash1 != hash2

        # Live file has new content
        loaded = store.load(ref2)
        assert loaded == b"version 2"

        # Old version is archived and still loadable
        version_path = store._version_path(hash1)
        assert os.path.exists(version_path)
        old_content = open(version_path, "rb").read()
        assert old_content == b"version 1"

    def test_same_content_update_does_not_create_version(self, store):
        store.save(b"same", key="doc", model_class_name="Notes")
        store.save(b"same", key="doc", model_class_name="Notes")
        # No version files should exist since content didn't change
        versions_dir = os.path.join(store.base_path, ".versions")
        if os.path.exists(versions_dir):
            version_files = []
            for root, dirs, files in os.walk(versions_dir):
                version_files.extend(files)
            assert len(version_files) == 0


class TestCustomConfiguration:
    """Test custom base_path and extension."""

    def test_custom_extension(self, md_store):
        ref = md_store.save(b"# Title", key="readme", model_class_name="Docs")
        assert ref.endswith(".md")
        loaded = md_store.load(ref)
        assert loaded == b"# Title"

    def test_env_var_base_path(self, tmp_path, monkeypatch):
        env_path = str(tmp_path / "env_store")
        monkeypatch.setenv("POPOTO_CONTENT_PATH", env_path)
        store = FilesystemStore()
        assert store.base_path == env_path

    def test_default_base_path(self, monkeypatch):
        monkeypatch.delenv("POPOTO_CONTENT_PATH", raising=False)
        store = FilesystemStore()
        expected = os.path.join(os.path.expanduser("~"), ".popoto", "content")
        assert store.base_path == expected


class TestFilenameSanitization:
    """Test that unsafe characters in keys are sanitized."""

    def test_special_characters_replaced(self, store):
        ref = store.save(b"content", key="a/b:c*d", model_class_name="M")
        assert "a_b_c_d" in ref

    def test_unicode_characters_replaced(self, store):
        ref = store.save(b"content", key="emoji\u2603", model_class_name="M")
        # Should not crash; unsafe chars replaced
        loaded = store.load(ref)
        assert loaded == b"content"


class TestReferenceFormat:
    """Test reference string parsing."""

    def test_parse_valid_reference(self):
        h, path = FilesystemStore._parse_reference("$CF:abc123:Model/key.txt")
        assert h == "abc123"
        assert path == "Model/key.txt"

    def test_parse_invalid_prefix_raises(self):
        with pytest.raises(ValueError):
            FilesystemStore._parse_reference("INVALID:abc:path")

    def test_parse_missing_parts_raises(self):
        with pytest.raises(ValueError):
            FilesystemStore._parse_reference("$CF:nocolon")


class TestAbstractInterface:
    """Verify AbstractContentStore cannot be instantiated directly."""

    def test_abstract_store_cannot_instantiate(self):
        with pytest.raises(TypeError):
            AbstractContentStore()

    def test_filesystem_store_is_subclass(self):
        assert issubclass(FilesystemStore, AbstractContentStore)
