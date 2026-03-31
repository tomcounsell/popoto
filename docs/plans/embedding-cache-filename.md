---
status: Ready
type: bugfix
appetite: Small
owner: Valor
created: 2026-03-31
tracking: https://github.com/tomcounsell/popoto/issues/313
last_comment_id:
---

# Fix Embedding Cache Filenames Exceeding 255-Byte Limit

## Problem

When a Popoto model with an `EmbeddingField` has a long Redis key (e.g., a key containing a full file path), the embedding cache `.npy` filename exceeds the 255-byte filesystem limit on macOS and most Linux filesystems.

**Current behavior:**

`EmbeddingField._embedding_path()` generates cache filenames by hex-encoding the full Redis key:

```python
hex_key = redis_key.encode("utf-8").hex()
return os.path.join(base, model_class_name, f"{hex_key}.npy")
```

Hex encoding doubles byte length. A Redis key of ~125+ characters produces a filename over 255 bytes, causing `OSError: [Errno 63] File name too long` on `os.rename()`.

**Desired outcome:**

Embedding cache filenames must never exceed filesystem limits, regardless of Redis key length. The fix must also preserve the ability of `load_embeddings()` to map `.npy` files back to their Redis keys.

## Prior Art

- **Issue #313**: Reports the bug with a concrete failing key (272-character hex filename).
- No prior issues or PRs related to embedding filename encoding.

## Solution

### Key Elements

1. **Replace hex encoding with SHA-256 hash** in `_embedding_path()`:

```python
import hashlib
hash_key = hashlib.sha256(redis_key.encode("utf-8")).hexdigest()
return os.path.join(base, model_class_name, f"{hash_key}.npy")
```

This produces a fixed 68-byte filename (64 hex chars + `.npy`), well within the 255-byte limit. `hashlib` is in the standard library, so no new dependencies.

2. **Store a sidecar index file** to solve the reverse-lookup problem. Since SHA-256 is a one-way hash, `load_embeddings()` can no longer reconstruct the Redis key from the filename. A JSON index file (`_index.json`) in each model's embedding directory maps hash filenames to Redis keys:

```json
{
  "a1b2c3...64chars.npy": "ModelClass:key1:key2",
  "d4e5f6...64chars.npy": "ModelClass:other_key"
}
```

- `on_save()` updates the index after writing the `.npy` file.
- `on_delete()` removes the entry from the index after deleting the `.npy` file.
- `load_embeddings()` reads the index to map filenames back to Redis keys.
- The index is written atomically (write to temp file, then rename) to avoid corruption.

3. **Backward-compatible loading**: `load_embeddings()` applies a fallback strategy during the transition period. For any `.npy` file not found in `_index.json`, it attempts the legacy hex-decode. If that succeeds, the file is usable and gets added to the index. If hex-decode fails (e.g., the filename is already a SHA-256 hash but the index is missing), the file is skipped with a warning.

4. **Migration on access**: No separate migration script. When `on_save()` is called for a model instance, it checks whether a legacy hex-encoded `.npy` file exists for that Redis key. If so, it renames it to the new SHA-256 filename and updates the index. This provides lazy, zero-downtime migration as models are saved.

### Flow

1. **Save**: `on_save()` generates embedding vector, computes SHA-256 filename, writes `.npy` atomically, updates `_index.json` atomically, checks for and migrates any legacy hex file.
2. **Load**: `load_embeddings()` reads `_index.json` for hash-to-key mapping. Falls back to hex-decode for files not in the index (backward compat). Builds matrix and key list as before.
3. **Delete**: `on_delete()` computes SHA-256 filename, removes `.npy` file, removes entry from `_index.json`.

## Architectural Impact

- **New dependencies**: None. `hashlib` is in the Python standard library.
- **Interface changes**: None. `_embedding_path()` signature unchanged. `load_embeddings()` return type unchanged.
- **File format**: `.npy` file contents are identical (numpy array). Only the filename changes from hex-encoded to SHA-256.
- **New artifact**: `_index.json` file per model class in the embeddings directory.
- **Reversibility**: Easy. Removing the index and reverting the code restores legacy behavior (assuming keys are short enough). The lazy migration renames files in-place, so a reverse migration script would be needed for long keys.

## Appetite

**Size:** Small (single file change + tests)

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| numpy installed | `python -c "import numpy"` | EmbeddingField requires numpy |

## Scope

### In Scope

- Replace hex encoding with SHA-256 in `_embedding_path()`
- Add `_index.json` sidecar for reverse-lookup
- Update `on_save()`, `on_delete()`, `load_embeddings()` to use the index
- Backward-compatible loading of legacy hex-encoded files
- Lazy migration of legacy files on save
- Update existing tests

### Out of Scope

- Implementing `garbage_collect()` (remains a stub, separate issue)
- Standalone migration CLI tool (lazy migration on save is sufficient)
- Compression or encryption of cache files

## No-Gos

- Do not change the `.npy` file format or contents
- Do not add external dependencies
- Do not change the public API of `EmbeddingField`
- Do not store Redis keys inside the `.npy` files (would change the array format and break existing consumers)

## Update System

No update system changes required. This is an internal library change to `popoto`. Users install popoto via pip/uv; the fix ships with the next release. No configuration files, environment variables, or migration scripts need to be propagated.

## Agent Integration

No agent integration required. This is a bugfix internal to the `popoto` library's `EmbeddingField`. No MCP servers, bridge changes, or tool wrappers are involved.

## Tasks

- [ ] Add `import hashlib` to `embedding_field.py`
- [ ] Replace hex encoding with SHA-256 in `_embedding_path()`
- [ ] Add `_write_index()` helper: atomically write `_index.json` (temp file + rename)
- [ ] Add `_read_index()` helper: read `_index.json`, return dict (empty dict if missing)
- [ ] Update `on_save()`: after writing `.npy`, update index; check for and migrate legacy hex file
- [ ] Update `on_delete()`: after removing `.npy`, remove entry from index
- [ ] Update `load_embeddings()`: read index for key lookup, fall back to hex-decode for unindexed files
- [ ] Update tests in `tests/test_embedding_field.py`

## Failure Path Test Strategy

| Failure | Expected behavior | Test |
|---------|-------------------|------|
| Redis key >125 chars | SHA-256 filename is 68 bytes, file saves successfully | `test_long_redis_key_filename` |
| Legacy hex file exists on save | Migrated to SHA-256 filename, index updated | `test_legacy_migration_on_save` |
| `_index.json` missing or corrupt | `load_embeddings()` falls back to hex-decode, logs warning | `test_load_without_index_falls_back` |
| Concurrent writes to `_index.json` | Atomic write via temp+rename prevents corruption | Covered by existing atomic write pattern |
| `.npy` file in dir with no index entry and non-hex name | File skipped with warning | `test_unrecognized_npy_skipped` |

## Test Impact

- [ ] `tests/test_embedding_field.py::TestEmbeddingFieldSave::test_save_stores_npy_file` -- UPDATE: verify SHA-256 filename format instead of hex
- [ ] `tests/test_embedding_field.py::TestEmbeddingFieldDelete::test_delete_removes_npy_file` -- UPDATE: verify index entry removed
- [ ] `tests/test_embedding_field.py::TestEmbeddingCache::test_load_embeddings_returns_matrix` -- UPDATE: verify keys returned correctly from index-based lookup
- [ ] `tests/test_embedding_field.py::TestEmbeddingCache::test_load_embeddings_matrix_is_normalized` -- UPDATE: may need minor adjustment if key reconstruction changes
- [ ] `tests/test_embedding_field.py::TestEmbeddingCache::test_cache_invalidated_on_save` -- UPDATE: verify cache works with new filename scheme

New tests to add:
- [ ] `test_long_redis_key_filename` -- save with a 200-char Redis key, verify no OSError
- [ ] `test_legacy_migration_on_save` -- create a hex-encoded `.npy` file, save same key, verify old file migrated
- [ ] `test_load_without_index_falls_back` -- create hex-encoded files without `_index.json`, verify `load_embeddings()` still works
- [ ] `test_index_json_written_on_save` -- verify `_index.json` exists and contains correct mapping after save
- [ ] `test_index_entry_removed_on_delete` -- verify `_index.json` entry removed after delete

## Documentation

- [ ] Update docstring on `_embedding_path()` to describe SHA-256 hashing (replacing hex encoding description)
- [ ] Add inline comments explaining the `_index.json` sidecar pattern
- [ ] No external documentation changes needed -- this is an internal implementation detail with no public API changes

## Rabbit Holes

- **Hash collisions**: SHA-256 collision probability is astronomically low (~1 in 2^128 for birthday attack). Not a practical concern. Do not add collision-handling logic.
- **Index file locking**: File-level locking for concurrent processes is complex and fragile. The atomic write pattern (temp + rename) is sufficient for single-process use. Multi-process embedding writes are not a supported use case.
- **Storing keys inside `.npy` files**: This would change the numpy array format, breaking any code that loads `.npy` files directly. The sidecar index is cleaner.
