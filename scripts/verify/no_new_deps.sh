#!/bin/sh
# Anti-criterion: no new required dependency. Expected: match count == 0.
git diff main -- pyproject.toml | grep -c '^+.*dependencies'
