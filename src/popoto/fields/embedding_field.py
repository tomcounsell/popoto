"""
EmbeddingField: Automatic embedding generation and storage.

Generates vector embeddings from a source field on save, stores them
as .npy files on the filesystem, and maintains an in-memory cache for
fast similarity search at query time.

Design:
    - on_save() reads the source field value, calls the provider to
      generate an embedding, and writes it as a .npy file.
    - The embedding cache is a class-level dict mapping model class names
      to pre-normalized numpy matrices for fast cosine similarity.
    - Cache is invalidated on save/delete within the same process.
    - numpy is optional -- follows the DataFrameField pattern.

Example:
    class Memory(popoto.Model):
        topic = popoto.KeyField()
        content = ContentField()
        embedding = EmbeddingField(source="content")

    m = Memory(topic="revenue", content="Revenue trends...")
    m.save()  # embedding generated automatically via provider
"""

import hashlib
import json
import logging
import os
import tempfile

from .field import Field

logger = logging.getLogger("POPOTO.EmbeddingField")

try:
    import numpy as np

    _numpy_available = True
except ImportError:
    _numpy_available = False

# Global default provider, set via popoto.configure()
_default_embedding_provider = None

# Global embedding cache: {model_class_name: {"matrix": np.ndarray, "keys": [redis_key, ...], "dirty": False}}
_embedding_cache = {}


def get_default_provider():
    """Get the configured default embedding provider."""
    return _default_embedding_provider


def set_default_provider(provider):
    """Set the default embedding provider. Called by popoto.configure()."""
    global _default_embedding_provider
    _default_embedding_provider = provider


def _get_embeddings_dir():
    """Get the base directory for embedding .npy files."""
    base = os.environ.get(
        "POPOTO_CONTENT_PATH",
        os.path.join(os.path.expanduser("~"), ".popoto", "content"),
    )
    return os.path.join(base, ".embeddings")


def _index_path(model_class_name: str) -> str:
    """Return the path to the _index.json sidecar for a model class."""
    base = _get_embeddings_dir()
    return os.path.join(base, model_class_name, "_index.json")


def _read_index(model_class_name: str) -> dict:
    """Read the _index.json sidecar, returning a dict mapping filenames to Redis keys.

    Returns an empty dict if the file is missing or corrupt.
    """
    path = _index_path(model_class_name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError):
        logger.warning(f"Could not read index file {path}, returning empty index")
        return {}


def _write_index(model_class_name: str, index_dict: dict) -> None:
    """Atomically write the _index.json sidecar (temp file + rename)."""
    path = _index_path(model_class_name)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(index_dict, f, ensure_ascii=False)
        os.rename(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def invalidate_cache(model_class_name: str = None):
    """Invalidate the embedding cache for a model class (or all)."""
    if model_class_name:
        _embedding_cache.pop(model_class_name, None)
    else:
        _embedding_cache.clear()


class EmbeddingField(Field):
    """Field type for automatic embedding generation and storage.

    Generates a vector embedding from a source field value on save,
    stores it as a .npy file, and maintains a cached numpy matrix
    for fast similarity computation.

    Args:
        source: Name of the field to read content from for embedding.
        provider: An AbstractEmbeddingProvider instance, or None to use
            the globally configured default.
        auto_embed: If True (default), generate embeddings automatically
            on save. If False, only embed on explicit calls.
        cache: If True (default), cache embeddings in memory for fast
            similarity search. If False, load from disk per query.
        **kwargs: Standard Field keyword arguments.

    Requires numpy: pip install popoto[embeddings]

    Example:
        class Doc(popoto.Model):
            name = popoto.KeyField()
            content = ContentField()
            embedding = EmbeddingField(source="content")
    """

    def __init__(
        self,
        source: str = None,
        provider=None,
        auto_embed: bool = True,
        cache: bool = True,
        **kwargs,
    ):
        if not _numpy_available:
            raise ImportError(
                "numpy is required to use EmbeddingField. "
                "Install it with: pip install popoto[embeddings]"
            )
        kwargs.setdefault("type", int)  # Stores dimension count in Redis
        kwargs.setdefault("null", True)
        kwargs.setdefault("default", None)
        super().__init__(**kwargs)
        self.type = int  # Redis stores the dimension count
        self.source = source
        self._provider = provider
        self.auto_embed = auto_embed
        self.cache_enabled = cache

    @property
    def provider(self):
        """Get the embedding provider instance."""
        if self._provider is not None:
            return self._provider
        return get_default_provider()

    @classmethod
    def _embedding_path(cls, model_class_name: str, redis_key: str) -> str:
        """Return the .npy file path for a model instance's embedding.

        Uses SHA-256 hash of the Redis key to produce a fixed-length
        filename (64 hex chars + '.npy' = 68 bytes), avoiding the
        255-byte filesystem limit that hex encoding could exceed with
        long Redis keys.

        Since SHA-256 is one-way, a sidecar ``_index.json`` file maps
        hash filenames back to Redis keys for reverse lookup.
        """
        base = _get_embeddings_dir()
        hash_key = hashlib.sha256(redis_key.encode("utf-8")).hexdigest()
        return os.path.join(base, model_class_name, f"{hash_key}.npy")

    @classmethod
    def _legacy_embedding_path(cls, model_class_name: str, redis_key: str) -> str:
        """Return the legacy hex-encoded .npy file path for migration."""
        base = _get_embeddings_dir()
        hex_key = redis_key.encode("utf-8").hex()
        return os.path.join(base, model_class_name, f"{hex_key}.npy")

    @classmethod
    def on_save(
        cls,
        model_instance,
        field_name: str,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """Generate and store embedding on save.

        Reads the source field value, calls the provider to generate an
        embedding vector, saves it as a .npy file, and stores the
        dimension count in Redis.

        Args:
            model_instance: The Model instance being saved.
            field_name: Name of this field on the model.
            field_value: Current value (dimension count or None).
            pipeline: Redis pipeline for batched operations.
            **kwargs: Additional context.

        Returns:
            The pipeline.
        """
        field_instance = model_instance._meta.fields.get(field_name)
        if not isinstance(field_instance, EmbeddingField):
            return pipeline if pipeline else None

        if not field_instance.auto_embed:
            return pipeline if pipeline else None

        provider = field_instance.provider
        if provider is None:
            return pipeline if pipeline else None

        # Read source field content
        source_name = field_instance.source
        if source_name is None:
            return pipeline if pipeline else None

        source_value = getattr(model_instance, source_name, None)
        if source_value is None or source_value == "":
            return pipeline if pipeline else None

        # If source value is a $CF reference, we need the actual content
        # The ContentField descriptor should have already resolved it,
        # but if accessed via __dict__ it might still be a reference.
        if isinstance(source_value, str) and source_value.startswith("$CF:"):
            # Try to load via the content field's store
            source_field = model_instance._meta.fields.get(source_name)
            if hasattr(source_field, "store"):
                try:
                    content_bytes = source_field.store.load(source_value)
                    source_value = content_bytes.decode("utf-8")
                except Exception:
                    logger.warning(
                        f"Could not load content for embedding from {source_name}"
                    )
                    return pipeline if pipeline else None

        # Generate embedding
        try:
            vectors = provider.embed([source_value], input_type="document")
            if not vectors or not vectors[0]:
                return pipeline if pipeline else None
            embedding = vectors[0]
        except Exception as e:
            raise RuntimeError(
                f"Embedding provider failed for {field_name}: {e}"
            ) from e

        # Validate dimensions
        if len(embedding) != provider.dimensions:
            raise ValueError(
                f"Provider returned {len(embedding)} dimensions, "
                f"expected {provider.dimensions}"
            )

        # Save as .npy file
        vector = np.array(embedding, dtype=np.float32)
        redis_key = model_instance._redis_key or model_instance.db_key.redis_key
        npy_path = cls._embedding_path(model_instance.__class__.__name__, redis_key)

        directory = os.path.dirname(npy_path)
        os.makedirs(directory, exist_ok=True)

        # Atomic write: temp file + rename
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".npy")
        try:
            os.close(fd)
            np.save(tmp_path, vector)
            os.rename(tmp_path, npy_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        # Update the _index.json sidecar with the hash-to-key mapping
        model_class_name = model_instance.__class__.__name__
        npy_filename = os.path.basename(npy_path)
        index = _read_index(model_class_name)
        index[npy_filename] = redis_key
        _write_index(model_class_name, index)

        # Migrate legacy hex-encoded file if it exists
        legacy_path = cls._legacy_embedding_path(model_class_name, redis_key)
        if legacy_path != npy_path and os.path.exists(legacy_path):
            try:
                os.unlink(legacy_path)
                # Remove legacy filename from index if present
                legacy_filename = os.path.basename(legacy_path)
                if legacy_filename in index:
                    del index[legacy_filename]
                    _write_index(model_class_name, index)
            except OSError:
                logger.warning(f"Failed to remove legacy embedding file {legacy_path}")

        # Store dimension count in Redis field
        dim_count = len(embedding)
        setattr(model_instance, field_name, dim_count)

        # Update Redis with dimension count
        import msgpack
        from ..redis_db import ENCODING

        encoded_dim = msgpack.packb(dim_count, use_bin_type=True)
        if pipeline:
            pipeline.hset(redis_key, field_name.encode(ENCODING), encoded_dim)
        else:
            from ..redis_db import POPOTO_REDIS_DB

            POPOTO_REDIS_DB.hset(redis_key, field_name.encode(ENCODING), encoded_dim)

        # Invalidate the in-memory cache for this model class
        invalidate_cache(model_instance.__class__.__name__)

        return pipeline if pipeline else None

    @classmethod
    def on_delete(
        cls,
        model_instance,
        field_name: str,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """Remove the embedding .npy file on delete."""
        model_class_name = model_instance.__class__.__name__
        redis_key = (
            kwargs.get("saved_redis_key")
            or model_instance._redis_key
            or model_instance.db_key.redis_key
        )
        npy_path = cls._embedding_path(model_class_name, redis_key)

        if os.path.exists(npy_path):
            os.unlink(npy_path)

        # Remove entry from _index.json sidecar
        npy_filename = os.path.basename(npy_path)
        index = _read_index(model_class_name)
        if npy_filename in index:
            del index[npy_filename]
            _write_index(model_class_name, index)

        invalidate_cache(model_class_name)
        return pipeline if pipeline else None

    @classmethod
    def load_embeddings(cls, model_class) -> tuple:
        """Load all embeddings for a model class into a numpy matrix.

        Returns a pre-normalized matrix and corresponding Redis key list,
        using the in-memory cache when available.

        Args:
            model_class: The Model class to load embeddings for.

        Returns:
            Tuple of (matrix, keys) where matrix is a 2D numpy array
            of shape (N, dimensions) and keys is a list of Redis key
            strings. Returns (None, []) if no embeddings found.
        """
        if not _numpy_available:
            return None, []

        model_name = model_class.__name__

        # Check cache first
        if model_name in _embedding_cache:
            cached = _embedding_cache[model_name]
            return cached["matrix"], cached["keys"]

        # Load from disk
        emb_dir = os.path.join(_get_embeddings_dir(), model_name)
        if not os.path.isdir(emb_dir):
            return None, []

        vectors = []
        keys = []

        # Read the index for hash-to-key mapping
        index = _read_index(model_name)
        index_updated = False

        for filename in os.listdir(emb_dir):
            if not filename.endswith(".npy"):
                continue
            filepath = os.path.join(emb_dir, filename)
            try:
                vec = np.load(filepath)

                # Look up Redis key from the index first
                if filename in index:
                    redis_key = index[filename]
                else:
                    # Fallback: try legacy hex-decode for backward compatibility
                    hex_key = filename[:-4]  # strip .npy
                    try:
                        redis_key = bytes.fromhex(hex_key).decode("utf-8")
                        # Add to index for future lookups
                        index[filename] = redis_key
                        index_updated = True
                    except (ValueError, UnicodeDecodeError):
                        logger.warning(
                            f"Skipping unrecognized embedding file {filename}: "
                            "not in index and not a valid hex-encoded key"
                        )
                        continue

                vectors.append(vec.astype(np.float32))
                keys.append(redis_key)
            except Exception as e:
                logger.warning(f"Failed to load embedding {filepath}: {e}")
                continue

        # Persist any index updates from legacy fallback
        if index_updated:
            try:
                _write_index(model_name, index)
            except Exception:
                logger.warning(f"Failed to update index for {model_name}")

        if not vectors:
            return None, []

        matrix = np.stack(vectors)

        # Pre-normalize for cosine similarity (dot product on unit vectors)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)  # Avoid division by zero
        matrix = matrix / norms

        # Cache the result
        _embedding_cache[model_name] = {"matrix": matrix, "keys": keys}

        return matrix, keys

    @classmethod
    def garbage_collect(cls, model_class):
        """Remove orphaned .npy files not referenced by live model instances.

        Args:
            model_class: The Model class to garbage collect embeddings for.

        Returns:
            int: Number of orphaned files removed.
        """
        # Future enhancement
        return 0
