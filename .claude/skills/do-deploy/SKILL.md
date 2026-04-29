---
name: do-deploy
description: "Deploy popoto: run tests, bump version, tag release, publish to PyPI, deploy docs. Triggered by 'deploy', 'do-deploy', or any request to publish a new version."
argument-hint: "<bump-type: patch|minor|major> (default: minor)"
---

# Deploy

Release a new version of popoto to PyPI and deploy documentation.

## How releases work

- Version is bumped manually in `pyproject.toml` and `setup.cfg`
- A release branch + PR is created and merged
- Pushing a `v*` tag triggers `release.yml` which publishes to PyPI via OIDC
- Docs are deployed to GitHub Pages via mkdocs

## Instructions

### Step 0: Determine bump type

Use `$ARGUMENTS` if provided (`patch`, `minor`, or `major`). Default to `minor` if empty.

### Step 1: Pre-flight checks

```bash
cd ~/src/popoto

# Must be on main
BRANCH=$(git branch --show-current)
if [ "$BRANCH" != "main" ]; then
  echo "ERROR: Must be on main branch (currently on $BRANCH)"
  exit 1
fi

# Must be clean
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: Working tree has uncommitted changes"
  exit 1
fi

# Pull latest
git pull origin main
```

Abort if any check fails.

### Step 2: Run tests

```bash
cd ~/src/popoto && pytest
```

Abort if any tests fail. Do not proceed with a broken test suite.

### Step 3: Calculate new version

Read current version from `pyproject.toml` (`version` field under `[project]`).

Calculate new version:
- `patch`: 1.4.0 → 1.4.1
- `minor`: 1.4.0 → 1.5.0
- `major`: 1.4.0 → 2.0.0

Confirm the new version with the user before proceeding.

### Step 4: Update version files

Update the version in both:
- `pyproject.toml`: the `version` field under `[project]`
- `setup.cfg`: the `version` field under `[metadata]`

### Step 5: Create release branch and PR

```bash
cd ~/src/popoto
git checkout -b release/v{new_version}
git add pyproject.toml setup.cfg
git commit -m "Bump version to {new_version}"
git push origin release/v{new_version}
gh pr create --title "Release v{new_version}" --body "Bump version to {new_version}"
```

### Step 6: Merge, tag, and push

```bash
gh pr merge --merge
git checkout main
git pull origin main
git tag v{new_version}
git push origin v{new_version}
```

The tag push triggers the `release.yml` GitHub Actions workflow, which builds and publishes to PyPI via OIDC trusted publishing.

### Step 7: Create GitHub release

```bash
gh release create v{new_version} --title "v{new_version}" --generate-notes
```

### Step 8: Deploy documentation

```bash
cd ~/src/popoto
python -m mkdocs build --strict
python -m mkdocs gh-deploy --force
```

If mkdocs is not installed: `uv pip install mkdocs`

### Step 9: Verify

1. Check the GitHub Actions release workflow: `gh run list --limit 1`
2. Report the new version, PyPI publish status, and docs URL (https://popoto.io/)

## Troubleshooting

### PyPI publish fails
Check the GitHub Actions run: `gh run view --log-failed`
Common issues: OIDC token permissions, build errors.

### Docs deploy fails
```bash
python -m mkdocs build --strict  # Check for errors first
uv pip install mkdocs            # Reinstall if needed
```

### Need to roll back a release
```bash
git tag -d v{version}
git push origin :refs/tags/v{version}
```
Then yank the version on PyPI if already published.
