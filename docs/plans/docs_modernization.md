---
status: Planning
type: chore
appetite: Medium
owner: Tom
created: 2026-04-28
tracking: https://github.com/tomcounsell/popoto/issues/373
last_comment_id:
---

# Docs Modernization: Material Theme, Auto-API, Agent-Readiness

## Problem

The popoto docs site works but is dated and maintenance-heavy. Three concrete pains:

**Current behavior:**
- Theme is `readthedocs` — visually dated, limited customization, doesn't signal "modern Python lib" the way Pydantic/FastAPI/uv do.
- `docs/api-reference.md` is a **100KB hand-maintained Markdown file** that drifts from `src/popoto/` source. Any new field, recipe, or method is invisible until someone remembers to update this file.
- No `llms.txt` or `llms-full.txt` — agent-driven users (the primary audience for an agent-memory library!) have no high-signal ingestion point.
- Deploy is manual via `/deploy-docs` (`mkdocs gh-deploy --force`). Easy to forget after a docs change lands on main.
- `pyproject.toml` `Documentation` URL still points at `popoto.readthedocs.io` (no longer the canonical host); README badge does too.

**Desired outcome:**
A Pydantic-style docs site with a Material theme, auto-generated API reference from docstrings (no more 100KB hand-maintained file), `llms.txt` + `llms-full.txt` for agent ingestion, and a GitHub Actions workflow that auto-deploys on push to `main`. URL targets stay on GitHub Pages (already wired). One PR, no breaking changes to the docs URL surface.

## Freshness Check

**Baseline commit:** `27476d8` (HEAD of main at plan time)
**Issue filed at:** 2026-04-28T05:56:31Z (minutes before plan)
**Disposition:** Unchanged

The issue was authored by me in this same session against verified file:line references. No commits have landed since. Verified at plan time:

| Reference | State |
|---|---|
| `mkdocs.yml` theme: `readthedocs` (line 51) | **Confirmed.** Six-line theme block, no Material. |
| `docs/api-reference.md` size 100,276 bytes | **Confirmed.** Hand-maintained. |
| `docs/requirements.txt` only pins `mkdocs` + `jinja2` | **Confirmed.** No Material/mkdocstrings/griffe. |
| `pyproject.toml` `Documentation = "https://popoto.readthedocs.io"` | **Confirmed.** |
| `README.md` ReadTheDocs badge + 2 references | **Confirmed.** |
| `.github/workflows/` — no `deploy-docs.yml` | **Confirmed.** Five workflow files, none for docs. |
| `src/popoto/__init__.py` `__all__` at line 148 | **Confirmed.** Comprehensive top-level allowlist. |

No active plan in `docs/plans/` overlaps this area. Proceeding on unchanged premises.

## Prior Art

```
gh issue list --state closed --search "mkdocs material docs migration" --limit 10
gh pr list --state merged --search "docs material theme" --limit 10
```

No prior issues or PRs found attempting this migration. The earlier issue #353 was closed-as-stale in this same session — its premise (Sphinx/RST/ReadTheDocs) had already been resolved by an earlier (uncatalogued) migration to MkDocs. Issue #373 replaces #353 with the actual remaining scope.

## Research

**Queries used:**
- "mkdocstrings python griffe `__all__` allowlist filter members 2026"
- "llms.txt llms-full.txt specification format llmstxt.org 2026"
- "mkdocs gh-deploy github actions workflow on push main 2026"

**Key findings:**

- **mkdocstrings respects `__all__`** ([mkdocstrings-python usage](https://mkdocstrings.github.io/python/usage/)). When `members: true` is unset, the handler defaults to symbols listed in `__all__`. Setting `load_external_modules: true` resolves aliases recursively when made public via `__all__`. This means popoto's existing top-level `__all__` in `src/popoto/__init__.py:148` can drive the public-surface allowlist with zero additional config — exactly what was answered in the open questions.

- **`llms.txt` spec is two files** ([llmstxt.org](https://llmstxt.org/)). `/llms.txt` is a navigation-style markdown index (H1 + blockquote summary + H2-delimited link sections); `/llms-full.txt` is a single-file concatenation of all docs for batch ingestion. Required: H1 with project name. Recommended: blockquote summary, H2 sections of file lists. This drives the split between hand-curated `llms.txt` and script-generated `llms-full.txt`.

- **Material's recommended deploy is `mkdocs gh-deploy --force` in a GitHub Actions workflow** ([Material publishing guide](https://squidfunk.github.io/mkdocs-material/publishing-your-site/)). Standard pattern: checkout → setup-python → cache pip → install docs deps → configure git as `github-actions[bot]` → `mkdocs gh-deploy --force`. Trigger on `push` to `main` filtered to `docs/**` and `mkdocs.yml` paths. This replaces the manual `/deploy-docs` skill.

- **Design reference: Pydantic** ([docs.pydantic.dev](https://docs.pydantic.dev/latest/)). Confirmed by user. Closest analog to popoto in shape (data-modeling lib + deep API reference + mkdocstrings auto-API + agent-friendly). Their navigation tree, palette, and mkdocstrings rendering are the visual targets.

## Spike Results

No spikes needed. All five components are mechanical:
1. Theme swap (config-only)
2. Plugin install + config (config-only, mkdocstrings docs are explicit)
3. Generated API pages (script lifted directly from issue body, validated by mkdocstrings docs)
4. `llms.txt` spec is small and well-defined
5. CI workflow is a known pattern

The only uncertainty (whether `__all__` allowlist is sufficient) was resolved by Phase 0.7 research above.

## Solution

### Key Elements

- **Theme:** `readthedocs` → `material` with palette toggle, `navigation.tabs`, `navigation.sections`, `content.code.copy`, dark/light mode.
- **API reference:** mkdocstrings + griffe + `mkdocs-gen-files` + `mkdocs-literate-nav`. A small `docs/scripts/gen_api_pages.py` walks `src/popoto/`, respects `__all__` declarations, and emits `::: popoto.module` stubs into a `reference/` virtual tree. The hand-maintained `docs/api-reference.md` is **deleted**.
- **`llms.txt`:** Hand-maintained at `docs/llms.txt` — H1 + blockquote + H2-delimited link sections per [llmstxt.org](https://llmstxt.org/) spec. Small enough to keep in version control directly.
- **`llms-full.txt`:** Generated by `docs/scripts/gen_llms_full.py` at build time — concatenates the markdown sources in nav order with H2 separators. Avoids drift.
- **CI auto-deploy:** `.github/workflows/deploy-docs.yml`. Triggers on push to `main` when `docs/**` or `mkdocs.yml` changes. Runs `mkdocs gh-deploy --force`. Manual `/deploy-docs` skill stays as an escape hatch.
- **URL hygiene:** `pyproject.toml` `Documentation` and `README.md` badge/links updated to `https://tomcounsell.github.io/popoto/`.

### Flow

**Author writes a docstring in `src/popoto/`** → push to a feature branch → PR merges to `main` → GitHub Actions detects `docs/**` or `mkdocs.yml` change → builds with mkdocstrings/griffe → publishes to `gh-pages` branch → live on `https://tomcounsell.github.io/popoto/`. No manual deploy step.

**Agent loads docs** → fetches `https://tomcounsell.github.io/popoto/llms.txt` → follows curated links to canonical pages, or fetches `llms-full.txt` for a single high-signal ingestion.

### Technical Approach

**1. `pyproject.toml` — add `docs` optional-deps group**

```toml
[project.optional-dependencies]
docs = [
  "mkdocs>=1.6",
  "mkdocs-material>=9.5",
  "mkdocstrings[python]>=0.25",
  "griffe>=0.45",
  "mkdocs-gen-files>=0.5",
  "mkdocs-literate-nav>=0.6",
]
```

Also update `Documentation = "https://tomcounsell.github.io/popoto/"`.

**2. `mkdocs.yml` — Material theme + plugins**

Replace the `theme:` block (currently `readthedocs`) with Material. Add plugins block:

```yaml
plugins:
  - search
  - gen-files:
      scripts:
        - docs/scripts/gen_api_pages.py
        - docs/scripts/gen_llms_full.py
  - literate-nav:
      nav_file: SUMMARY.md
  - mkdocstrings:
      handlers:
        python:
          paths: [src]
          options:
            docstring_style: google
            show_source: true
            show_root_heading: true
            members_order: source
            filters: ["!^_"]
```

`paths: [src]` is critical — popoto uses src layout. `filters: ["!^_"]` hides leading-underscore symbols even when they're not in `__all__`.

The existing nav stays intact for hand-written content; the auto-generated `reference/` tree is appended via `literate-nav`'s `SUMMARY.md`. The current `- API Reference: 'api-reference.md'` line is replaced by `- API Reference: reference/SUMMARY.md`.

**3. `docs/scripts/gen_api_pages.py` — auto-generate API tree**

Lifted from the issue body, parameterized for src layout. Walks `src/popoto/**/*.py`, skips `_*` files, and writes `::: popoto.module.path` stubs into virtual `reference/<path>.md` files. mkdocstrings then renders each from the docstrings. Public surface filtering happens in mkdocstrings (via `__all__` defaults + `filters`), not in the generator script.

**4. `docs/scripts/gen_llms_full.py` — concatenate full docs**

Reads the nav order from `mkdocs.yml`, opens each `.md` source, prepends an H2 with the page title, and writes the concatenation to virtual `llms-full.txt`. Uses `mkdocs_gen_files.open()` so the output is part of the build. ~30 lines.

**5. `docs/llms.txt` — hand-maintained index**

```
# Popoto

> Python Redis/Valkey ORM with Django-like syntax. Provides agent-memory
> primitives (decay, confidence, co-occurrence, prediction ledger) plus
> standard model/query/pubsub features.

## Documentation

- [Configuration](https://tomcounsell.github.io/popoto/configuration/)
- [Models and Fields](https://tomcounsell.github.io/popoto/fields/)
...

## Agent Memory

- [Overview](https://tomcounsell.github.io/popoto/features/agent-memory/)
- [DecayingSortedField](https://tomcounsell.github.io/popoto/features/decaying-sorted-field/)
...

## API Reference

- [Full reference](https://tomcounsell.github.io/popoto/reference/)
- [llms-full.txt](https://tomcounsell.github.io/popoto/llms-full.txt)
```

**6. `.github/workflows/deploy-docs.yml` — CI auto-deploy**

```yaml
name: Deploy Docs
on:
  push:
    branches: [main]
    paths: ["docs/**", "mkdocs.yml", "src/popoto/**", ".github/workflows/deploy-docs.yml"]
permissions:
  contents: write
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: "pip" }
      - run: pip install -e ".[docs]"
      - run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
      - run: mkdocs gh-deploy --force
```

`paths` includes `src/popoto/**` because docstring changes there must rebuild the auto-generated API tree.

**7. `README.md` — replace ReadTheDocs badge + links**

Three locations: badge image at top, "Documentation:" link, second documentation link. Replace with GitHub Pages URLs.

## Failure Path Test Strategy

This work is config + scripts + docs — no production runtime code paths. Failure modes are:

### Build-Time Failures
- [ ] `mkdocs build --strict` must pass locally before merge — strict mode catches broken nav references, missing files, and (critically) docstring syntax errors that mkdocstrings can't parse. Add this as a verification step.
- [ ] CI workflow runs `mkdocs build --strict` *first*, then `mkdocs gh-deploy` only if strict passes — never deploy a broken site.

### Generator Script Failures
- [ ] `gen_api_pages.py` and `gen_llms_full.py` are invoked during `mkdocs build`. If either raises, the build fails — that's the intended failure mode. No silent swallowing.

### URL Drift
- [ ] After deploy, smoke-test `https://tomcounsell.github.io/popoto/` and `/llms.txt` and `/llms-full.txt` return 200. CI does NOT do this (would require waiting for gh-pages propagation); manual smoke check in the PR validation step is sufficient.

## Test Impact

No existing tests affected — this is documentation infrastructure, not Python source. The verification is build-based, not pytest-based:

- `mkdocs build --strict` — exit 0 means the entire site (including generated API tree) builds without warnings.
- The deployed site is the integration test.

If `gen_api_pages.py` reveals docstring formatting errors in `src/popoto/`, those will surface as mkdocstrings warnings under `--strict`. **Fix at the docstring source, not by suppressing the warnings** — that's the whole point of moving to auto-generation.

## Rabbit Holes

- **Custom Material theme overrides (CSS, partials, color tokens beyond the palette toggle).** Tempting because Pydantic has a polished feel — but the polish comes from their *content* and *information density*, not from custom CSS. Stay on stock Material with palette toggle. Defer custom theming to a follow-up if the result feels generic.
- **Search backend swap (Algolia, Meilisearch).** Material's built-in search is fine for a library this size. Don't.
- **Per-subpackage `__all__` audits.** popoto already has `__all__` at the top level of `__init__.py` (comprehensive) and in 6 subpackages (`embeddings`, `streams`, `stores`, `models`, `recipes`). `fields/` has no `__all__` but is fully re-exported by the top-level. **Don't** start adding `__all__` to every internal module — that's a separate cleanup. mkdocstrings + the existing top-level `__all__` is enough.
- **Restructuring docs/ content.** The existing nav (Configuration, Models and Fields, etc.) is fine. Don't reshuffle as part of this work.
- **Custom domain (popoto.dev or similar).** Stay on `tomcounsell.github.io/popoto/`. If a custom domain is desired, it's a separate one-line CNAME change and a Cloudflare/Squarespace task — not part of this plan.
- **Migrating off `mkdocs gh-deploy` to `actions/deploy-pages` (Pages artifacts).** Newer pattern, but `gh-deploy` is what the manual skill already uses and works fine. Don't change two things at once.

## Risks

### Risk 1: Auto-generated API tree omits something the hand-maintained 100KB file documented

**Impact:** Users searching for a method that was documented in `api-reference.md` find nothing on the new site.

**Mitigation:** Before deleting `docs/api-reference.md`, do a one-pass diff: build the new site, render the `reference/` tree, and grep `docs/api-reference.md` for any class/method names that don't appear in the new tree. If something's missing, it's likely (a) missing `__all__` on a subpackage, (b) a missing docstring, or (c) a private symbol that was documented anyway. Each is a clear fix at the source. Only delete `api-reference.md` after this diff is clean.

### Risk 2: Docstring style is inconsistent across the codebase

**Impact:** mkdocstrings with `docstring_style: google` will render some functions correctly and butcher others.

**Mitigation:** Already verified Google style in `src/popoto/models/expressions.py`, `query.py`, `encoding.py` (Args:/Returns: sections present). If `mkdocs build --strict` flags any docstrings as malformed, fix at the source. This is a *feature* of the migration, not a risk to the migration — pre-existing inconsistency was hidden by hand-maintenance.

### Risk 3: `mkdocs gh-deploy` from CI conflicts with manual `gh-deploy` invocations

**Impact:** Two pushes to `gh-pages` racing each other; gh-pages branch state diverges from what CI expects on next run.

**Mitigation:** After this lands, the manual `/deploy-docs` skill becomes "break-glass only — CI should be doing this." Document that in the skill body. The `--force` flag on `gh-deploy` makes the racing case self-healing (last write wins) but the operational habit needs to shift to "let CI do it."

### Risk 4: PRs that touch only docstrings now trigger a full docs rebuild + deploy

**Impact:** Very minor — adds ~1-2 min to merge feedback loop on docstring-only PRs. Worth it for the auto-update guarantee.

**Mitigation:** None needed. This is the desired behavior.

## Race Conditions

No race conditions identified. CI deploys are serialized by the `gh-pages` push; concurrent runs would be rare (single-maintainer repo) and `--force` resolves any race deterministically.

## No-Gos (Out of Scope)

- Migrating to Cloudflare Pages, Vercel, or Netlify. **GitHub Pages stays.**
- Custom domain setup.
- Restructuring `docs/` content (guides, features, etc.) beyond the nav swap.
- Custom Material theme CSS/templates beyond the palette/feature toggles.
- Adding `__all__` to every internal module in `src/popoto/`.
- Versioned docs (mike). Single-version site for now.
- Docs i18n.
- Removing or rewriting the `/deploy-docs` skill — it stays as a manual escape hatch.

## Update System

No update system changes required — this affects the docs site only, not the deployed application or `update` skill.

## Agent Integration

No agent integration changes required. `llms.txt` / `llms-full.txt` make popoto's docs more agent-readable, but no MCP server or bridge changes are involved.

## Documentation

This *is* the documentation work. Specific outputs:

### Source Documentation
- [ ] Delete `docs/api-reference.md` (the hand-maintained 100KB file) **only after** the new auto-generated tree is verified to cover every public symbol.
- [ ] Add `docs/scripts/gen_api_pages.py` (lifted from issue, parameterized for `src/` layout).
- [ ] Add `docs/scripts/gen_llms_full.py`.
- [ ] Add `docs/llms.txt` (hand-curated).
- [ ] Update `docs/requirements.txt` to mirror the new `[docs]` optional-deps group, OR remove it and document `pip install -e ".[docs]"` as the install command. **Decision: remove `docs/requirements.txt`** and standardize on the optional-deps group. Cleaner.

### Repo Metadata
- [ ] `pyproject.toml`: add `[docs]` optional-deps group; update `Documentation` URL.
- [ ] `README.md`: replace the ReadTheDocs badge image with a GitHub Pages-equivalent (or remove if no good native badge exists); update "Documentation:" link x2.
- [ ] `CLAUDE.md`: update the "Commands" section's `mkdocs serve` line if needed (currently fine — `mkdocs serve` still works).

### Skill Updates
- [ ] `.claude/commands/deploy-docs.md`: prepend a "Note: CI handles this automatically on push to main. Use this skill only when CI is broken or you need an out-of-band deploy" header. Don't delete — keep as escape hatch.

## Success Criteria

- [ ] `mkdocs build --strict` exits 0 locally and in CI
- [ ] `https://tomcounsell.github.io/popoto/` renders with the Material theme (dark/light toggle visible, navigation tabs visible)
- [ ] API reference is auto-generated; `docs/api-reference.md` is deleted; the new `reference/` tree covers every symbol previously documented (verified by grep diff before deletion)
- [ ] `https://tomcounsell.github.io/popoto/llms.txt` returns 200 with correct format (H1 + blockquote + H2 sections per llmstxt.org spec)
- [ ] `https://tomcounsell.github.io/popoto/llms-full.txt` returns 200 with full docs concatenation
- [ ] Pushing a docstring change to `main` triggers `.github/workflows/deploy-docs.yml` and the change appears on the live site within ~3 min
- [ ] `pyproject.toml` `Documentation` field points at the GitHub Pages URL
- [ ] README badge + 2 doc links point at the GitHub Pages URL
- [ ] `pip install -e ".[docs]"` installs everything needed to build the site

## Step by Step Tasks

Single feature branch (`docs/material-modernization`), single PR. Execution order matters because the `--strict` build is the gate at each step.

### 1. Add `[docs]` optional-deps group + URL fix
- **Task ID**: build-deps
- **Depends On**: none
- **File**: `pyproject.toml`
- Add `docs` group with mkdocs, mkdocs-material, mkdocstrings[python], griffe, mkdocs-gen-files, mkdocs-literate-nav.
- Update `Documentation` URL.
- Verify: `pip install -e ".[docs]"` succeeds.

### 2. Author the two generator scripts
- **Task ID**: build-generators
- **Depends On**: build-deps
- **Files**: `docs/scripts/gen_api_pages.py`, `docs/scripts/gen_llms_full.py`
- `gen_api_pages.py`: walk `src/popoto/`, skip `_*` files, emit `::: popoto.module` stubs into virtual `reference/`.
- `gen_llms_full.py`: read mkdocs nav, concatenate `.md` sources with H2 separators into virtual `llms-full.txt`.

### 3. Swap theme + add plugins in `mkdocs.yml`
- **Task ID**: build-config
- **Depends On**: build-generators
- **File**: `mkdocs.yml`
- Replace `theme: readthedocs` block with Material config (palette toggle, navigation.tabs, navigation.sections, content.code.copy).
- Add `plugins:` block (search, gen-files, literate-nav, mkdocstrings with `paths: [src]` and Google docstring style).
- Replace `- API Reference: 'api-reference.md'` nav line with `- API Reference: reference/SUMMARY.md`.

### 4. Author hand-curated `llms.txt`
- **Task ID**: build-llms-index
- **Depends On**: build-config (so URLs are stable)
- **File**: `docs/llms.txt`
- H1 + blockquote summary + H2-delimited link sections (Documentation, Agent Memory, API Reference, External).

### 5. First strict build + diff against old API reference
- **Task ID**: validate-coverage
- **Depends On**: build-config, build-llms-index
- Run `mkdocs build --strict`. Fix any docstring issues at the source.
- Build the new `reference/` tree locally. Grep symbol names from `docs/api-reference.md` (classes, public methods) and confirm each appears in the generated tree. Surface gaps; fix by adding to `__all__` or correcting docstrings — **not** by re-adding to a hand-maintained file.

### 6. Delete `docs/api-reference.md` + `docs/requirements.txt`
- **Task ID**: build-deletions
- **Depends On**: validate-coverage (must be clean)
- Delete `docs/api-reference.md` (100KB).
- Delete `docs/requirements.txt` (replaced by `[docs]` extra).
- Re-run `mkdocs build --strict`.

### 7. CI workflow
- **Task ID**: build-ci
- **Depends On**: validate-coverage
- **File**: `.github/workflows/deploy-docs.yml`
- Trigger on push to `main` filtered to `docs/**`, `mkdocs.yml`, `src/popoto/**`, and the workflow file itself.
- Steps: checkout → setup-python 3.12 → `pip install -e ".[docs]"` → configure git as `github-actions[bot]` → `mkdocs gh-deploy --force`.

### 8. README + CLAUDE.md + skill updates
- **Task ID**: build-meta
- **Depends On**: build-ci
- `README.md`: replace ReadTheDocs badge + 2 links with GitHub Pages URLs.
- `CLAUDE.md`: confirm `mkdocs serve` line is still accurate (no change expected).
- `.claude/commands/deploy-docs.md`: prepend "CI handles this automatically — use only as escape hatch" note.

### 9. PR + merge + smoke test
- **Task ID**: validate-deploy
- **Depends On**: all build-* tasks
- Open PR. After merge, watch the `Deploy Docs` workflow run on `main`. Smoke-test:
  - `curl -sI https://tomcounsell.github.io/popoto/ | head -1` → 200
  - `curl -sI https://tomcounsell.github.io/popoto/llms.txt | head -1` → 200
  - `curl -sI https://tomcounsell.github.io/popoto/llms-full.txt | head -1` → 200
  - Visual check: dark/light toggle works, navigation tabs render, API reference tree appears under `/reference/`.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Docs deps install | `pip install -e ".[docs]"` | exit code 0 |
| Strict build passes | `mkdocs build --strict` | exit code 0 |
| API reference deleted | `test ! -f docs/api-reference.md` | exit code 0 |
| `llms.txt` exists | `test -f docs/llms.txt` | exit code 0 |
| Generator scripts exist | `test -f docs/scripts/gen_api_pages.py && test -f docs/scripts/gen_llms_full.py` | exit code 0 |
| CI workflow exists | `test -f .github/workflows/deploy-docs.yml` | exit code 0 |
| pyproject URL updated | `grep "tomcounsell.github.io/popoto" pyproject.toml` | exit code 0 |
| ReadTheDocs refs gone | `grep -rn "popoto.readthedocs.io" pyproject.toml README.md` | exit code 1 |
| Built site has reference tree | `test -d site/reference` | exit code 0 |
| Built site has llms-full | `test -f site/llms-full.txt` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique. Empty until critique is run. -->

---

## Open Questions

All major decisions resolved during planning. One residual question:

1. **README badge replacement.** The current README has a ReadTheDocs status badge at the top. Options:
   - (a) Remove the badge entirely (simplest, no per-repo badge for GitHub Pages out of the box).
   - (b) Replace with a "Deploy Docs" workflow status badge (`https://github.com/tomcounsell/popoto/actions/workflows/deploy-docs.yml/badge.svg`) — shows CI green/red, useful signal.
   - (c) Use shields.io to make a custom "docs: passing" badge linked to the site.

   Recommendation: **(b)** — gives a real CI signal at zero maintenance cost. OK to proceed with that?
