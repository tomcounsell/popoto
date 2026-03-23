"""
Content storage backends for Popoto ContentField.

This module provides the AbstractContentStore interface and built-in
implementations for persisting large content outside of Redis.

Content is stored on the filesystem using content-addressable storage
(SHA-256 hashing), keeping Redis lean while enabling lazy-loaded access
to large values like documents, images, and agent memory artifacts.
"""

from abc import ABC, abstractmethod


class AbstractContentStore(ABC):
    """Abstract interface for content storage backends.

    Implementations handle the physical persistence of content values
    that are too large or inappropriate for Redis storage. Redis holds
    only a reference string; the store holds the actual bytes.

    The interface is deliberately minimal to enable diverse backends
    (filesystem, S3, GCS, etc.) while maintaining a consistent contract.

    Methods:
        save: Persist content bytes and return a reference string.
        load: Retrieve content bytes by reference.
        delete: Remove content by reference.
        exists: Check if content exists for a reference.
    """

    @abstractmethod
    def save(self, content: bytes, key: str, model_class_name: str) -> str:
        """Persist content and return a reference string.

        The reference string is stored in Redis and used later to
        retrieve the content. Its format is implementation-specific.

        Args:
            content: Raw content bytes to store.
            key: The model instance's key value (for human-readable naming).
            model_class_name: The model class name (for directory organization).

        Returns:
            A reference string that can be used to retrieve the content.

        Raises:
            IOError: If the content cannot be written.
        """
        ...

    @abstractmethod
    def load(self, reference: str) -> bytes:
        """Retrieve content bytes by reference.

        Args:
            reference: The reference string returned by save().

        Returns:
            The raw content bytes.

        Raises:
            FileNotFoundError: If the referenced content does not exist.
        """
        ...

    @abstractmethod
    def delete(self, reference: str) -> None:
        """Remove content by reference.

        Args:
            reference: The reference string returned by save().

        Raises:
            FileNotFoundError: If the referenced content does not exist.
        """
        ...

    @abstractmethod
    def exists(self, reference: str) -> bool:
        """Check whether content exists for a reference.

        Args:
            reference: The reference string returned by save().

        Returns:
            True if the content exists, False otherwise.
        """
        ...


from .filesystem import FilesystemStore

__all__ = ["AbstractContentStore", "FilesystemStore"]
