"""
Filesystem-based content store using content-addressable storage.

Files are organized under a configurable base path with two-level
directory sharding based on the SHA-256 hash of the content. Writes
are atomic (temp file + os.rename) to prevent partial-write corruption.

Directory structure:
    {base_path}/{ModelClassName}/{key_value}.{ext}     <- live file
    {base_path}/.versions/{hash_prefix}/{hash}.{ext}   <- archived versions
"""

import hashlib
import os
import tempfile

from . import AbstractContentStore


class FilesystemStore(AbstractContentStore):
    """Content-addressable filesystem storage backend.

    Stores content files organized by model class name and key value.
    Uses SHA-256 hashing for content addressing and versioning.

    Args:
        base_path: Root directory for content storage.
            Defaults to ~/.popoto/content/ or POPOTO_CONTENT_PATH env var.
        extension: Default file extension for stored content. Default ".txt".

    Example:
        store = FilesystemStore(base_path="/data/content")
        ref = store.save(b"hello world", key="greeting", model_class_name="Memory")
        content = store.load(ref)
        assert content == b"hello world"
    """

    def __init__(self, base_path: str = None, extension: str = ".txt"):
        if base_path is None:
            base_path = os.environ.get(
                "POPOTO_CONTENT_PATH",
                os.path.join(os.path.expanduser("~"), ".popoto", "content"),
            )
        self.base_path = os.path.abspath(base_path)
        self.extension = extension

    def _compute_hash(self, content: bytes) -> str:
        """Compute SHA-256 hex digest of content bytes."""
        return hashlib.sha256(content).hexdigest()

    def _live_path(self, model_class_name: str, key: str) -> str:
        """Return the path for the live (human-readable) file."""
        safe_key = self._sanitize_filename(key)
        return os.path.join(
            self.base_path, model_class_name, f"{safe_key}{self.extension}"
        )

    def _version_path(self, content_hash: str) -> str:
        """Return the path for an archived version by content hash."""
        prefix = content_hash[:2]
        return os.path.join(
            self.base_path,
            ".versions",
            prefix,
            f"{content_hash}{self.extension}",
        )

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Replace unsafe characters in filenames."""
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(name))

    def _atomic_write(self, path: str, content: bytes) -> None:
        """Write content atomically using temp file + rename."""
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory)
        fd_closed = False
        try:
            os.write(fd, content)
            os.close(fd)
            fd_closed = True
            os.rename(tmp_path, path)
        except Exception:
            if not fd_closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def save(self, content: bytes, key: str, model_class_name: str) -> str:
        """Persist content to filesystem and return a reference string.

        If a live file already exists at the target path, its current
        content is archived to .versions/ before the new content is written.

        Reference format: $CF:{sha256_hash}:{model_class_name}/{sanitized_key}{ext}

        Args:
            content: Raw content bytes to store.
            key: The model instance's key value (for human-readable naming).
            model_class_name: The model class name (for directory organization).

        Returns:
            Reference string in the format $CF:{hash}:{relative_path}

        Raises:
            IOError: If the content cannot be written to the filesystem.
        """
        content_hash = self._compute_hash(content)
        live = self._live_path(model_class_name, key)

        # Archive existing live file if it differs
        if os.path.exists(live):
            with open(live, "rb") as f:
                existing_content = f.read()
            existing_hash = self._compute_hash(existing_content)
            if existing_hash != content_hash:
                version_path = self._version_path(existing_hash)
                if not os.path.exists(version_path):
                    self._atomic_write(version_path, existing_content)

        # Write new content to live path
        self._atomic_write(live, content)

        # Build relative path for the reference
        safe_key = self._sanitize_filename(key)
        relative_path = f"{model_class_name}/{safe_key}{self.extension}"
        return f"$CF:{content_hash}:{relative_path}"

    def load(self, reference: str) -> bytes:
        """Load content from filesystem by reference string.

        Attempts to read from the live path first. If the live file's hash
        doesn't match the reference, falls back to the versioned archive.

        Args:
            reference: Reference string in $CF:{hash}:{relative_path} format.

        Returns:
            The raw content bytes.

        Raises:
            FileNotFoundError: If the referenced content cannot be found.
        """
        content_hash, relative_path = self._parse_reference(reference)
        live_path = os.path.join(self.base_path, relative_path)

        # Try live path first
        if os.path.exists(live_path):
            with open(live_path, "rb") as f:
                content = f.read()
            if self._compute_hash(content) == content_hash:
                return content

        # Fall back to version archive
        version_path = self._version_path(content_hash)
        if os.path.exists(version_path):
            with open(version_path, "rb") as f:
                return f.read()

        raise FileNotFoundError(
            f"Content not found for reference {reference}. "
            f"Checked: {live_path}, {version_path}"
        )

    def delete(self, reference: str) -> None:
        """Remove a content file by reference.

        Removes the live file only. Archived versions are left intact
        (append-only). Use garbage_collect() to clean orphaned versions.

        Args:
            reference: Reference string in $CF:{hash}:{relative_path} format.

        Raises:
            FileNotFoundError: If the live file does not exist.
        """
        _, relative_path = self._parse_reference(reference)
        live_path = os.path.join(self.base_path, relative_path)
        if os.path.exists(live_path):
            os.unlink(live_path)
        else:
            raise FileNotFoundError(f"Content file not found: {live_path}")

    def exists(self, reference: str) -> bool:
        """Check if content exists for the given reference.

        Mirrors load() logic: checks the live path hash first, then falls
        back to the version archive. This ensures exists() returning True
        guarantees that load() will succeed.

        Args:
            reference: Reference string in $CF:{hash}:{relative_path} format.

        Returns:
            True if the content can be loaded, False otherwise.
        """
        try:
            content_hash, relative_path = self._parse_reference(reference)
        except ValueError:
            return False

        live_path = os.path.join(self.base_path, relative_path)
        if os.path.exists(live_path):
            with open(live_path, "rb") as f:
                live_hash = self._compute_hash(f.read())
            if live_hash == content_hash:
                return True

        version_path = self._version_path(content_hash)
        return os.path.exists(version_path)

    @staticmethod
    def _parse_reference(reference: str) -> tuple:
        """Parse a $CF reference string into (hash, relative_path).

        Args:
            reference: String in format $CF:{hash}:{relative_path}

        Returns:
            Tuple of (content_hash, relative_path)

        Raises:
            ValueError: If the reference format is invalid.
        """
        if not reference.startswith("$CF:"):
            raise ValueError(f"Invalid content reference: {reference}")
        parts = reference[4:].split(":", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid content reference format: {reference}")
        return parts[0], parts[1]
