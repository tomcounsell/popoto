Deploy the popoto documentation site to GitHub Pages using mkdocs.

> **CI handles this automatically — use only as escape hatch.**
>
> The `Deploy Docs` GitHub Actions workflow (`.github/workflows/deploy-docs.yml`)
> deploys on every push to `main` that touches `docs/**`, `mkdocs.yml`, or
> `src/popoto/**`. Use this skill only when CI is broken or you need an
> out-of-band deploy.

## Steps

0. **Check CI is not running** — abort if a CI deploy is currently in flight.
   A manual `gh-deploy` racing the workflow's `gh-deploy` produces a
   non-deterministic gh-pages state. Wait for CI to finish, or use this skill
   only when CI is broken:
   ```
   gh run list --workflow=deploy-docs.yml --limit 1 --json status,conclusion
   ```
   Abort if `status` is `in_progress` or `queued`.

1. **Ensure the working tree is clean** — run `git status` and abort if there are uncommitted changes.

2. **Ensure you are on the main branch** — run `git branch --show-current` and abort if not on `main`.

3. **Install docs dependencies if needed** — the project ships a `[docs]`
   optional-deps group containing mkdocs/mkdocs-material/mkdocstrings/etc:
   ```
   uv pip install -e ".[docs]"
   ```
   (or `pip install -e ".[docs]"` if not using uv)

4. **Build the docs locally first** to catch any errors:
   ```
   python -m mkdocs build --strict
   ```
   If the build fails, fix any issues (e.g. nav referencing wrong filenames,
   docstring parse errors) and retry. Abort only if unfixable.

5. **Deploy to GitHub Pages**:
   ```
   python -m mkdocs gh-deploy --force
   ```
   This builds the docs and pushes them to the `gh-pages` branch.

6. **Confirm** — tell the user the docs site has been deployed and should be live shortly at https://popoto.io/
