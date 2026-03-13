Perform a release of popoto. The bump type is: $ARGUMENTS (if empty, ask the user whether this is a major, minor, or patch release).

## Steps

1. **Read the current version** from `pyproject.toml` (the `version` field under `[project]`).

2. **Calculate the new version** based on the bump type:
   - `patch`: increment the patch number (e.g. 0.9.0 → 0.9.1)
   - `minor`: increment the minor number, reset patch (e.g. 0.9.1 → 0.10.0)
   - `major`: increment the major number, reset minor and patch (e.g. 0.10.0 → 1.0.0)

3. **Ensure the working tree is clean** — run `git status` and abort if there are uncommitted changes.

4. **Run tests** — run `pytest` and abort if any tests fail.

5. **Update the version** in both files:
   - `pyproject.toml`: the `version` field under `[project]` (line ~7)
   - `setup.cfg`: the `version` field under `[metadata]` (line ~3)

6. **Commit the version bump**:
   ```
   git add pyproject.toml setup.cfg
   git commit -m "Bump version to {new_version}"
   ```

7. **Create a release branch and PR** (main is protected):
   ```
   git checkout -b release/v{new_version}
   git push origin release/v{new_version}
   gh pr create --title "Release v{new_version}" --body "Bump version to {new_version}"
   ```

8. **Merge the PR**:
   ```
   gh pr merge --merge
   ```

9. **Tag and push** — switch back to main, pull, then tag:
   ```
   git checkout main
   git pull origin main
   git tag v{new_version}
   git push origin v{new_version}
   ```
   The tag push triggers the GitHub Actions `release.yml` workflow, which builds and publishes the package to PyPI automatically via OIDC trusted publishing.

10. **Post release notes** on GitHub using `gh release create`. Include a summary of changes since the last release and a full changelog link:
    ```
    gh release create v{new_version} --title "v{new_version}" --generate-notes
    ```

11. **Confirm** — tell the user the release is published and they can monitor the GitHub Actions workflow for PyPI publishing status.
