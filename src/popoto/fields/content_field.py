"""
ContentField: Large content storage with filesystem offloading.

Routes large content values (documents, text, binary data) to filesystem
storage via a pluggable ContentStore backend. Redis stores only a compact
reference string ($CF:{hash}:{path}), keeping memory usage minimal.

Content is lazy-loaded on attribute access -- queried model instances
start with the reference string and only read from the filesystem when
the content attribute is actually accessed.

Design:
    - on_save() writes content to filesystem BEFORE pipeline.execute(),
      then adds an HSET command to store the reference in Redis.
    - The descriptor (__get__) detects $CF: references and transparently
      loads from the filesystem, caching on the instance dict.
    - on_delete() is a no-op (append-only storage). Use garbage_collect()
      to clean orphaned files.

Example:
    class Document(popoto.Model):
        name = popoto.KeyField()
        body = ContentField()

    doc = Document(name="readme", body="# Hello World")
    doc.save()  # body written to filesystem, $CF reference in Redis
"""

import logging

from .field import Field

logger = logging.getLogger("POPOTO.ContentField")

# Global default store instance, set via popoto.configure()
_default_content_store = None


def get_default_store():
    """Get or create the default FilesystemStore."""
    global _default_content_store
    if _default_content_store is None:
        from ..stores.filesystem import FilesystemStore

        _default_content_store = FilesystemStore()
    return _default_content_store


def set_default_store(store):
    """Set the default content store. Called by popoto.configure()."""
    global _default_content_store
    _default_content_store = store


class ContentField(Field):
    """Field type for storing large content on the filesystem.

    Values assigned to a ContentField are written to a configurable
    content store (default: FilesystemStore) on save. Redis stores only
    a compact reference string. On attribute access, the content is
    lazy-loaded from the store.

    Args:
        store: A ContentStore instance or "filesystem" (default).
        **kwargs: Standard Field keyword arguments.

    Example:
        class Memory(popoto.Model):
            topic = popoto.KeyField()
            content = ContentField(store="filesystem")

        m = Memory(topic="revenue", content="# Revenue Analysis\\n...")
        m.save()
    """

    # Export/import: the store reference string is the field's plain value
    # (captured by to_dict()); on_save() writes the content to the store from
    # that same value, so no separate carried state is needed.
    roundtrip_policy: str = "rebuild"

    def __init__(self, store="filesystem", **kwargs):
        kwargs.setdefault("type", str)
        kwargs.setdefault("null", True)
        kwargs.setdefault("default", None)
        super().__init__(**kwargs)
        self.type = str

        if store == "filesystem" or store is None:
            self._store = None  # will use default
        else:
            self._store = store

    @property
    def store(self):
        """Get the content store instance."""
        if self._store is not None:
            return self._store
        return get_default_store()

    def __set_name__(self, owner, name):
        """Called when the field is assigned to a model class attribute."""
        self.name = name
        self.attr_name = name

    def __get__(self, instance, owner):
        """Descriptor that lazy-loads content from the store.

        When accessed on a class, returns the field itself (for query expressions).
        When accessed on an instance, checks if the stored value is a $CF: reference
        and loads content from the filesystem if so.
        """
        if instance is None:
            return self

        # Check instance __dict__ for cached loaded content
        cache_key = f"_content_cache_{self.name}"
        if cache_key in instance.__dict__:
            return instance.__dict__[cache_key]

        value = instance.__dict__.get(self.name)

        if value is None:
            return None

        if isinstance(value, str) and value.startswith("$CF:"):
            try:
                content_bytes = self.store.load(value)
                content_str = content_bytes.decode("utf-8")
                instance.__dict__[cache_key] = content_str
                return content_str
            except FileNotFoundError:
                raise FileNotFoundError(
                    f"Content file missing for {self.name} on "
                    f"{instance.__class__.__name__}. Reference: {value}"
                )

        return value

    def __set__(self, instance, value):
        """Set content value on an instance."""
        if instance is None:
            return

        # Clear the loaded content cache
        cache_key = f"_content_cache_{self.name}"
        instance.__dict__.pop(cache_key, None)

        instance.__dict__[self.name] = value

    def format_value_pre_save(self, field_value, **kwargs):
        """Pass through the value. Filesystem write happens in on_save().

        We cannot write to filesystem here because we lack access to the
        model instance (needed for key values and class name). The value
        passes through unchanged; on_save() handles the filesystem write
        and Redis reference update.
        """
        if field_value is None:
            return None
        if field_value == "":
            return ""
        return field_value

    @classmethod
    def on_save(
        cls,
        model_instance,
        field_name: str,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """Write content to filesystem and update Redis with reference.

        This hook runs after HSET is queued on the pipeline but before
        pipeline.execute(). It writes content to filesystem first, then
        adds an HSET command to overwrite the field value with the $CF
        reference string.

        Args:
            model_instance: The Model instance being saved.
            field_name: Name of this field on the model.
            field_value: Current value (raw content or existing $CF reference).
            pipeline: Redis pipeline for batched operations.
            **kwargs: Additional context.

        Returns:
            The pipeline with reference update command added.
        """
        if field_value is None or field_value == "":
            return pipeline if pipeline else None

        # Already a reference -- nothing to do
        if isinstance(field_value, str) and field_value.startswith("$CF:"):
            return pipeline if pipeline else None

        # Get the field instance from the model's meta
        field_instance = model_instance._meta.fields.get(field_name)
        if not isinstance(field_instance, ContentField):
            return pipeline if pipeline else None

        store = field_instance.store

        # Build a key value from the model's key fields for file naming
        key_parts = []
        for kf_name in sorted(model_instance._meta.key_field_names):
            kv = getattr(model_instance, kf_name, None)
            if kv is not None:
                key_parts.append(str(kv))
        key_value = ":".join(key_parts) if key_parts else "default"

        model_class_name = model_instance.__class__.__name__

        # Convert to bytes for storage
        if isinstance(field_value, str):
            content_bytes = field_value.encode("utf-8")
        elif isinstance(field_value, bytes):
            content_bytes = field_value
        else:
            content_bytes = str(field_value).encode("utf-8")

        # Write to filesystem FIRST (filesystem-before-Redis ordering)
        reference = store.save(
            content_bytes,
            key=key_value,
            model_class_name=model_class_name,
        )

        # Update instance attribute to the reference
        instance_dict = model_instance.__dict__
        instance_dict[field_name] = reference
        cache_key = f"_content_cache_{field_name}"
        instance_dict.pop(cache_key, None)

        # Overwrite the field in Redis with the reference string
        import msgpack
        from ..redis_db import ENCODING

        redis_key = model_instance._redis_key or model_instance.db_key.redis_key
        encoded_ref = msgpack.packb(reference, use_bin_type=True)

        if pipeline:
            pipeline.hset(redis_key, field_name.encode(ENCODING), encoded_ref)
            return pipeline
        else:
            from ..redis_db import POPOTO_REDIS_DB

            POPOTO_REDIS_DB.hset(redis_key, field_name.encode(ENCODING), encoded_ref)
            return None

    @classmethod
    def on_delete(
        cls,
        model_instance,
        field_name: str,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """No-op on delete. Content files are append-only.

        Use garbage_collect() to remove orphaned content files.
        """
        return pipeline if pipeline else None

    @classmethod
    def garbage_collect(cls, model_class):
        """Remove orphaned content files not referenced by any live model.

        Args:
            model_class: The Model class to garbage collect content for.

        Returns:
            int: Number of orphaned files removed.
        """
        # Future enhancement: walk content directory and compare with live refs
        return 0
