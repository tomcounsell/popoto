# Plan: Document TTL Support

**Issue:** https://github.com/tomcounsell/popoto/issues/321
**Slug:** ttl-docs
**Type:** Documentation only (no ORM code changes)

## Problem

Popoto supports model-level and instance-level TTL, but users report not knowing the feature exists. The `meta.md` page documents TTL alongside other Meta options, but there is no dedicated TTL page in the docs navigation. Users scanning the sidebar for "TTL" or "expiration" will not find it. The API reference also omits the `_ttl` and `_expire_at` instance attributes.

## Solution

Create a dedicated `docs/ttl.md` page that consolidates all TTL documentation into one discoverable location. Add a practical session-model example with 90-day TTL. Update the API reference to document `_ttl` and `_expire_at` as instance attributes. Add the new page to `mkdocs.yml` nav.

## Tasks

- [x] Create `docs/ttl.md` with the following sections:
  - Overview: what TTL does and when to use it
  - Model-level TTL via `class Meta: ttl = N` with explanation of `EXPIRE` during `save()`
  - TTL reset behavior on every `save()` call
  - Per-instance TTL override via `instance._ttl` (including setting to `None` to make permanent)
  - Absolute expiration via `instance._expire_at` with `datetime` examples
  - Mutual exclusion constraint: cannot set both `_ttl` and `_expire_at`
  - Complete example: `AgentSession` model with 90-day TTL showing creation, override, and absolute expiration
  - Cross-references to `meta.md` and `api-reference.md`
- [x] Update `mkdocs.yml` nav to add TTL page between "Model Meta Options" and "Indexed Fields"
- [x] Update `docs/api-reference.md` to document `_ttl` and `_expire_at` as instance attributes in the Model class section
- [x] Cross-link from `docs/meta.md` TTL section to the new dedicated page

## No-Gos

- No changes to ORM source code (`src/popoto/`)
- No changes to tests
- Do not duplicate the full Meta reference content; the dedicated page should be self-contained but link back to `meta.md` for the complete Meta options reference
- Do not invent behavior not present in the codebase (e.g., do not claim validation rejects both `_ttl` and `_expire_at` being set unless the code actually raises -- check `base.py:1174-1180` which uses `elif`, meaning `_ttl` simply takes precedence)

## Update System

No update system changes required -- this is a documentation-only change to the popoto library's docs site.

## Agent Integration

No agent integration required -- this is documentation for the popoto ORM library, not the agent system.

## Failure Path Test Strategy

- Run `mkdocs serve` locally to verify the new page renders correctly and navigation links work
- Verify all cross-references between `ttl.md`, `meta.md`, and `api-reference.md` resolve
- Confirm code examples are syntactically valid Python

## Test Impact

No existing tests affected -- this is a documentation-only change with no code modifications.

## Documentation

- [x] Create `docs/ttl.md` as the primary deliverable (this is a docs-only issue)
- [x] Update `docs/api-reference.md` with `_ttl` and `_expire_at` instance attribute docs
- [x] Update cross-references in `docs/meta.md`

## Rabbit Holes

- Temptation to add TTL validation logic (rejecting both `_ttl` and `_expire_at`) -- that is a code change, out of scope for this issue
- Over-documenting Redis EXPIRE internals -- keep focus on Popoto's API surface
- Rewriting the existing `meta.md` TTL sections -- leave them in place, just add a cross-link to the new dedicated page

## Acceptance Criteria

From the issue:
- [x] Plan explains model-level TTL (`class Meta: ttl = N`)
- [x] Plan covers per-instance TTL override (`instance._ttl`)
- [x] Plan covers absolute expiration (`instance._expire_at`)
- [x] Plan notes the mutual exclusion between `_ttl` and `_expire_at`
- [x] Plan includes at least one complete code example (session model with 90-day TTL)
- [x] Plan updates docs site navigation
