Deploy the popoto documentation site to GitHub Pages using mkdocs.

## Steps

1. **Ensure the working tree is clean** — run `git status` and abort if there are uncommitted changes.

2. **Ensure you are on the main branch** — run `git branch --show-current` and abort if not on `main`.

3. **Install mkdocs if needed** — check if `python -m mkdocs --version` works, and if not install it:
   ```
   uv pip install mkdocs
   ```

4. **Build the docs locally first** to catch any errors:
   ```
   python -m mkdocs build --strict
   ```
   If the build fails, fix any issues (e.g. nav referencing wrong filenames) and retry. Abort only if unfixable.

5. **Deploy to GitHub Pages**:
   ```
   python -m mkdocs gh-deploy --force
   ```
   This builds the docs and pushes them to the `gh-pages` branch.

6. **Confirm** — tell the user the docs site has been deployed and should be live shortly at https://tomcounsell.github.io/popoto/
