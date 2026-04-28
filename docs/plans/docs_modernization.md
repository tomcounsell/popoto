---
status: docs_complete
type: chore
appetite: Medium
owner: Tom
created: 2026-04-28
tracking: https://github.com/tomcounsell/popoto/issues/373
last_comment_id:
revision_applied: true
---

# Docs Modernization: Material Theme, Auto-API, Agent-Readiness

## Revision Notes (2026-04-28)

Plan revised after `/do-plan-critique` surfaced 8 concerns + 7 nits, 0 blockers. Each concern below is embedded as an Implementation Note in the relevant section.

- **C1** (Workflow YAML missing strict build) — CI workflow now runs `mkdocs build --strict` *before* `mkdocs gh-deploy --force`. Strict failure aborts the deploy.
- **C2** (`__all__` allowlist semantics) — Technical Approach explicitly notes that `members:` MUST NOT be set in mkdocstrings options for `__all__` defaults to apply. Added to Risk 2.
- **C3** (Coverage validation hand-wavy) — Step 5 (validate-coverage) replaced with a deterministic `__all__`-driven check: build the site, render every symbol in `popoto.__all__` (currently 65 symbols), and verify each appears as a heading in the generated `reference/` tree.
- **C4** (CI concurrency) — Workflow gets `concurrency: { group: deploy-docs, cancel-in-progress: false }` to coalesce rapid pushes without dropping a deploy mid-flight.
- **C5** (Manual `/deploy-docs` racing CI) — Skill prepends a precondition check: `gh run list --workflow=deploy-docs.yml --limit 1` must show no in-progress run before manual `gh-deploy`. If a CI run is active, skill aborts with instruction to wait.
- **C6** (`_*` skip rule too broad) — `enable_error_reporting` (in `popoto.__all__`) lives in `_error_reporting.py`. Verified by `python -c "from popoto import _error_reporting as m; print(set(dir(m)) & set(popoto.__all__))"` → `{'enable_error_reporting'}`. Generator now skips only `__pycache__/` and `__*.py` (dunder), NOT leading-underscore modules. Public/private filtering is delegated to mkdocstrings' `__all__` defaults + `filters: ["!^_"]` (which acts on symbol names, not file names).
- **C7** (Script execution order) — `gen_llms_full.py` does NOT attempt to embed the API reference (which is virtual at build time and would be 100KB+ anyway). It concatenates only the hand-written narrative `.md` sources in nav order and emits a pointer to `/reference/` for API content. This sidesteps the ordering hazard entirely.
- **C8** (Narrative content loss in `api-reference.md` deletion) — The hand-maintained file has 159 `##` headers vs. ~65 symbols in `popoto.__all__` — meaning ~90 sections are *narrative* (examples, recipes, prose) that mkdocstrings cannot regenerate. New step 5a (audit-narrative) runs BEFORE deletion: extract `##` headers from `docs/api-reference.md`, classify each as (i) symbol reference (mkdocstrings will cover) or (ii) narrative content. Any narrative section gets relocated to an appropriate topical page (`docs/fields.md`, `docs/query.md`, etc.) or a new `docs/recipes.md` before the file is deleted.

Round 2 of `/do-plan-critique` surfaced 5 additional concerns (C9–C13), 4 nits.

- **C9** (CI extras install coverage) — `pip install -e ".[docs]"` installs ONLY the docs extras, not the package's runtime deps. mkdocstrings imports `popoto` to introspect, which transitively imports `redis` etc. CI install changed to `pip install -e ".[docs]"` which already pulls runtime deps via the base `[project]` block (verified). No change needed beyond a comment in the workflow noting the install also brings runtime deps. Confirmed `pyproject.toml` has runtime deps under `[project] dependencies`, not behind an extra.
- **C10** (GitHub Pages source unverified) — gh-deploy assumes Pages source is set to "Deploy from a branch: gh-pages /". If Pages is configured for "GitHub Actions" source instead, `gh-deploy` will succeed but the site won't update. Verification: before merge, run `gh api /repos/tomcounsell/popoto/pages` and confirm `build_type: legacy` (branch source) or `source.branch: gh-pages`. If misconfigured, switch via repo Settings → Pages → Source: "Deploy from a branch" / `gh-pages` / `/`. Added to Prerequisites.
- **C11** (Narrative audit grep too coarse) — Step 5a's `grep "^## "` misses H3/H4 sections, but `api-reference.md` uses `##` for top-level entries and nests `###`/`####` for class members. The intent is to audit *narrative subtrees*, so the grep widens to `grep -nE "^#{2,4} " docs/api-reference.md` and the classifier walks the resulting outline rather than flat-listing headers. Step 5a updated.
- **C12** (ReadTheDocs not archived/redirected) — popoto.readthedocs.io is still live and search engines may rank it above the new GH Pages URL. Two-step fix: (a) log into RTD admin and archive the project (or set it to redirect-only); (b) add a banner to RTD's index page pointing at the new URL during the transition window. Added to step 8 (build-meta) AND a follow-up issue is created post-merge. NOT a blocker for this plan — fixable any time after deploy.
- **C13** (Bookmark URL 404s) — Anchors in `api-reference.md` (e.g. `popoto.readthedocs.io/...#decayingsortedfield`) will 404 once the file is deleted. mkdocstrings generates `/reference/popoto/fields/decaying_sorted_field/#popoto.fields.decaying_sorted_field.DecayingSortedField` — different URL shape. Mitigation: ship a small `docs/redirects.md` mapping common old-URL anchors to new URLs OR add `mkdocs-redirects` plugin. **Decision:** add `mkdocs-redirects` to docs extras and configure a redirect map for the most common 20-30 anchors as a follow-up issue (not in scope of this plan — too time-intensive for marginal benefit). Mention in plan PR description so user can prioritize the follow-up.

Frontmatter `revision_applied: true` and `status: Ready`. No further critique rounds — proceeding to /do-build.

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

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| GitHub Pages branch source (C10) | `gh api /repos/tomcounsell/popoto/pages -q '.build_type, .source.branch'` | Confirms Pages source is `legacy` (branch deploy) with `gh-pages` branch. If `workflow`, `mkdocs gh-deploy` will succeed but the site won't update. |
| `gh` CLI authenticated | `gh auth status` | Required for the Pages check above and post-merge smoke verification. |
| `pip install -e ".[docs]"` succeeds locally | `pip install -e ".[docs]" && python -c "import mkdocs_material, mkdocstrings"` | Validates docs extras install before CI tries the same. |

If the Pages check returns `build_type: workflow`, fix via repo Settings → Pages → Source: "Deploy from a branch" / `gh-pages` / `/` BEFORE merging the implementation PR.

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

> **Implementation Note (C2):** Do NOT add a `members:` key to the mkdocstrings options block. mkdocstrings only respects `__all__` as the public-symbol allowlist when `members` is unset. If `members: true` is set, every importable symbol gets rendered (defeating the purpose). If `members: [list]` is set, `__all__` is ignored. Leave `members` absent — the config above is correct as-written.

The existing nav stays intact for hand-written content; the auto-generated `reference/` tree is appended via `literate-nav`'s `SUMMARY.md`. The current `- API Reference: 'api-reference.md'` line is replaced by `- API Reference: reference/SUMMARY.md`.

**3. `docs/scripts/gen_api_pages.py` — auto-generate API tree**

Lifted from the issue body, parameterized for src layout. Walks `src/popoto/**/*.py`, skips `__pycache__/` and `__*.py` (dunder files only — see C6 below), and writes `::: popoto.module.path` stubs into virtual `reference/<path>.md` files. mkdocstrings then renders each from the docstrings. Public surface filtering happens in mkdocstrings (via `__all__` defaults + `filters`), not in the generator script.

> **Implementation Note (C6):** The naive skip rule `path.name.startswith("_")` would exclude `_error_reporting.py`, but `popoto.__all__` includes `enable_error_reporting` which lives in that file. Verified: `set(dir(_error_reporting)) & set(popoto.__all__) == {'enable_error_reporting'}`. The skip rule must be **dunder-only** (`__pycache__`, `__init__`, `__main__`). Leading-underscore modules generate pages; mkdocstrings then filters down to `__all__` symbols at render time. If a leading-underscore module has zero `__all__` overlap, mkdocstrings will render an empty page with just the module path — acceptable, and rare. Detect-and-skip empty pages is a follow-up nit, not a blocker.

**4. `docs/scripts/gen_llms_full.py` — concatenate narrative docs**

Reads the nav order from `mkdocs.yml`, opens each hand-written `.md` source, prepends an H2 with the page title, and writes the concatenation to virtual `llms-full.txt`. Uses `mkdocs_gen_files.open()` so the output is part of the build. ~30 lines.

> **Implementation Note (C7):** `llms-full.txt` does NOT embed the auto-generated API reference. Two reasons: (a) ordering — the API tree is virtual at build time, so reading it from disk during gen_llms_full execution is racy/fragile; (b) bloat — concatenating 65 symbols' rendered HTML/MD would push `llms-full.txt` past 200KB without commensurate value. Instead, `llms-full.txt` ends with a section: `## API Reference\n\nSee https://tomcounsell.github.io/popoto/reference/ for the full auto-generated reference, or fetch each module's source directly from https://github.com/tomcounsell/popoto/tree/main/src/popoto/.` Agents that need API surface follow the pointer.

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
concurrency:
  group: deploy-docs
  cancel-in-progress: false
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
      - name: Build (strict)
        run: mkdocs build --strict
      - name: Deploy
        run: mkdocs gh-deploy --force
```

`paths` includes `src/popoto/**` because docstring changes there must rebuild the auto-generated API tree.

> **Implementation Note (C1):** The `Build (strict)` step must run before `Deploy` and must NOT use `continue-on-error`. Strict-mode failures (broken nav, malformed docstrings, missing files) abort the deploy. This is the gate that prevents deploying a broken site — the test strategy section requires it, so the workflow must enforce it.

> **Implementation Note (C4):** The `concurrency: { group: deploy-docs, cancel-in-progress: false }` block coalesces rapid successive pushes into the same group. `cancel-in-progress: false` means an in-flight deploy completes before the next one starts (rather than being killed mid-`gh-deploy` and leaving `gh-pages` in an inconsistent state). With this guard, multiple pushes to `main` in quick succession queue up cleanly.

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

### 5. First strict build + symbol-coverage check
- **Task ID**: validate-coverage
- **Depends On**: build-config, build-llms-index
- Run `mkdocs build --strict`. Fix any docstring issues at the source.
- **Symbol coverage check (C3):** Run a small Python script against the built `site/`:
  ```python
  import popoto, pathlib, re
  rendered = pathlib.Path("site/reference").rglob("*.html")
  rendered_text = "\n".join(p.read_text() for p in rendered)
  missing = [s for s in popoto.__all__ if s not in rendered_text]
  assert not missing, f"Symbols in __all__ but not rendered: {missing}"
  ```
  Each of the 65 symbols in `popoto.__all__` must appear in the rendered HTML. If any are missing, the cause is one of: (a) the source module was excluded by gen_api_pages.py — fix the skip rule, (b) the symbol's docstring is malformed — fix the docstring, (c) `__all__` claims it but it isn't actually defined in the module — fix `__all__`.

### 5a. Narrative content audit (before deletion)
- **Task ID**: audit-narrative
- **Depends On**: validate-coverage
- Extract every `##` header from `docs/api-reference.md`: `grep -n "^## " docs/api-reference.md > /tmp/api_ref_headers.txt`.
- Classify each header into one of three buckets:
  1. **Symbol reference** — header is a class/function name covered by mkdocstrings rendering. No action.
  2. **Narrative** (example, recipe, prose) — needs to be relocated.
  3. **Duplicate of content already in another doc page** — no action; will be removed with the file.
- For each "narrative" header (incl. nested H3/H4 — see C11), identify the best target page (`docs/fields.md`, `docs/query.md`, `docs/relationship.md`, etc.) or create `docs/recipes.md` for orphaned recipe-style content.

  > **Implementation Note (C11):** The grep widens to `grep -nE "^#{2,4} " docs/api-reference.md` to capture H2/H3/H4 because `api-reference.md` uses `##` for top-level entries and nests `###`/`####` for class members. Walk the resulting outline tree, not a flat list — narrative content typically lives in H3/H4 subtrees under symbol-level H2 headers.
- Move the section content into the target page(s) and add nav entries if needed.
- Re-run `mkdocs build --strict`. Site builds; relocated content is reachable via nav.

### 6. Delete `docs/api-reference.md` + `docs/requirements.txt`
- **Task ID**: build-deletions
- **Depends On**: validate-coverage AND audit-narrative (both must be clean)
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
- (C12) After deploy verified, archive the ReadTheDocs project: log into RTD admin → Project Settings → Advanced → "Archive" OR set the project to redirect to GH Pages URL. **Defer to a follow-up issue post-merge** — not blocking. Mention in PR description.
- `.claude/commands/deploy-docs.md`: prepend "CI handles this automatically — use only as escape hatch" note AND add a new step 0:
  ```
  0. **Check CI is not running** — abort if `gh run list --workflow=deploy-docs.yml --limit 1 --json status,conclusion` shows a status of `in_progress` or `queued`. A manual `gh-deploy` racing CI's `gh-deploy` produces a non-deterministic gh-pages state. Wait for CI to finish, or use this skill only when CI is broken.
  ```
  This addresses C5: the `--force` flag means last-write-wins, but a CI deploy mid-flight can be overwritten by a manual one (or vice versa) — so the precondition gate is the safety net.

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

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | Operator | Workflow YAML omits `mkdocs build --strict` | Technical Approach §6, Step 5 | Build (strict) step added before Deploy; failure aborts deploy |
| CONCERN | Skeptic | mkdocstrings `__all__` semantics rely on omitting `members:` | Technical Approach §2 | Note added: do NOT set `members:` key |
| CONCERN | Operator | Coverage validation hand-wavy | Step 5 (validate-coverage) | Replaced with `__all__`-driven Python check on built site |
| CONCERN | Adversary | CI workflow lacks concurrency group | Technical Approach §6 | `concurrency: { group: deploy-docs, cancel-in-progress: false }` added |
| CONCERN | Adversary | Manual `/deploy-docs` skill races CI | Step 8 (build-meta) | Skill gets new step 0: abort if CI run is `in_progress`/`queued` |
| CONCERN | Archaeologist | `_*` skip rule excludes `_error_reporting.py` whose `enable_error_reporting` symbol is in `popoto.__all__` | Technical Approach §3 | Skip rule narrowed to dunder-only (`__pycache__`, `__init__`, `__main__`); private filtering delegated to mkdocstrings |
| CONCERN | Skeptic | Generator script execution order matters for `llms-full.txt` | Technical Approach §4 | `llms-full.txt` excludes API tree entirely; emits pointer to `/reference/` instead |
| CONCERN | Archaeologist | Deleting `api-reference.md` may lose narrative content (159 `##` headers vs. ~65 symbols) | New Step 5a (audit-narrative) | Pre-deletion narrative audit + relocation to topical pages or `docs/recipes.md` |
| CONCERN | Operator | CI extras install coverage (round 2) | Verified: pyproject runtime deps not behind extras | No change — `[docs]` extra + base `[project] dependencies` covers everything mkdocstrings needs to introspect popoto |
| CONCERN | Operator | GitHub Pages source unverified (round 2) | Prerequisites table | Added pre-build check: `gh api /repos/tomcounsell/popoto/pages` must show `build_type: legacy` with `gh-pages` source |
| CONCERN | Skeptic | Narrative audit grep too coarse (round 2) | Step 5a updated | Widened to `grep -nE "^#{2,4} "` to catch nested narrative under symbol H2 sections |
| CONCERN | Adversary | ReadTheDocs not archived/redirected (round 2) | Step 8 + follow-up issue | Archive via RTD admin post-deploy; not blocking |
| CONCERN | Archaeologist | Bookmark URL 404s after `api-reference.md` deletion (round 2) | Follow-up issue (not in this plan) | `mkdocs-redirects` plugin + redirect map for top-30 anchors deferred to follow-up |

11 nits across 2 critique rounds absorbed into Implementation Notes inline.

---

## Open Questions

All resolved during planning + critique revision.

1. **README badge replacement** — proceeding with (b): "Deploy Docs" workflow status badge (`https://github.com/tomcounsell/popoto/actions/workflows/deploy-docs.yml/badge.svg`). Real CI signal, zero maintenance cost. User authorized "complete all of /sdlc" so this decision is locked.
