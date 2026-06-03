#!/usr/bin/env bash
#
# ci-local.sh — Run the meaningful CI gates locally, before pushing.
#
# Mirrors the GitHub Actions workflows so you can catch failures without
# burning Actions minutes (and without depending on GitHub being up):
#
#   Gate      Mirrors workflow        What it runs
#   ----      ----------------        ------------
#   tests     test-valkey.yml         full pytest suite against local Redis
#   stress    stress-tests.yml        pytest tests/test_stress.py (+durations)
#   docs      deploy-docs.yml         mkdocs build --strict (the deploy gate)
#   build     release.yml             python -m build (sdist + wheel sanity)
#   guard     guard-main-push.yml     warns if a main push isn't docs-only
#
# Valkey is intentionally skipped: redis-py talks to Redis and Valkey
# identically, and the project rule is "never use Redis modules", so the
# suite against local Redis covers the same ground. GitHub still runs the
# real Valkey job on PR/merge as the final word.
#
# Usage:
#   scripts/ci-local.sh              # default gates: tests + stress + docs
#   scripts/ci-local.sh --all        # everything, incl. build + guard
#   scripts/ci-local.sh --fast       # tests only (skip stress + docs)
#   scripts/ci-local.sh tests docs   # run only the named gates
#
# Flags:
#   --all     run every gate (tests stress docs build guard)
#   --fast    run only the test suite
#   -h|--help show this help

set -uo pipefail

cd "$(dirname "$0")/.." || exit 2
ROOT="$(pwd)"
PY="${ROOT}/.venv/bin/python"
PYTEST="${ROOT}/.venv/bin/pytest"
MKDOCS="${ROOT}/.venv/bin/mkdocs"
REDIS_URL="${REDIS_URL:-redis://localhost:6379}"
export REDIS_URL

# --- colors (no-op if not a tty) ---------------------------------------------
if [ -t 1 ]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; X=$'\e[0m'
else
  R=''; G=''; Y=''; B=''; X=''
fi

step()  { printf '\n%s━━ %s ━━%s\n' "$B" "$1" "$X"; }
ok()    { printf '%s✓ %s%s\n' "$G" "$1" "$X"; }
warn()  { printf '%s! %s%s\n' "$Y" "$1" "$X"; }
fail()  { printf '%s✗ %s%s\n' "$R" "$1" "$X"; }

# --- arg parsing -------------------------------------------------------------
GATES=()
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac
for arg in "$@"; do
  case "$arg" in
    --all)  GATES=(tests stress docs build guard) ;;
    --fast) GATES=(tests) ;;
    tests|stress|docs|build|guard) GATES+=("$arg") ;;
    *) fail "unknown argument: $arg"; exit 2 ;;
  esac
done
# Default gate set when nothing specified.
if [ "${#GATES[@]}" -eq 0 ]; then
  GATES=(tests stress docs)
fi

# --- preflight ---------------------------------------------------------------
[ -x "$PY" ] || { fail "no venv at .venv/ — run: uv venv && uv pip install -e '.[dev]'"; exit 2; }

# Redis is required by every test gate; check once up front.
needs_redis=false
for g in "${GATES[@]}"; do
  [ "$g" = tests ] || [ "$g" = stress ] && needs_redis=true
done
if $needs_redis; then
  if ! redis-cli -u "$REDIS_URL" ping >/dev/null 2>&1; then
    fail "Redis not reachable at $REDIS_URL (start it, or set REDIS_URL)"
    exit 2
  fi

  # CI installs the package fresh every run, so its metadata version always
  # matches pyproject. A local editable install goes stale after a version
  # bump -- popoto.__version__ reads from importlib.metadata, so the
  # test_version check then fails locally while CI is green. Refresh the
  # editable metadata to match CI before running any test gate.
  pyproj_ver="$(awk -F'"' '/^version = /{print $2; exit}' pyproject.toml)"
  inst_ver="$("$PY" -c "from importlib.metadata import version; print(version('popoto'))" 2>/dev/null || echo "")"
  if [ -n "$pyproj_ver" ] && [ "$pyproj_ver" != "$inst_ver" ]; then
    warn "installed popoto metadata ($inst_ver) != pyproject ($pyproj_ver) — refreshing editable install"
    if command -v uv >/dev/null 2>&1; then
      VIRTUAL_ENV="$ROOT/.venv" uv pip install -e . --no-deps -q
    else
      "$PY" -m pip install -e . --no-deps -q
    fi
  fi
fi

declare -a RESULTS
overall=0

run_gate() {
  local name="$1"; shift
  step "$name"
  if "$@"; then
    ok "$name passed"
    RESULTS+=("${G}✓${X} $name")
  else
    fail "$name failed"
    RESULTS+=("${R}✗${X} $name")
    overall=1
  fi
}

# --- gate implementations ----------------------------------------------------
gate_tests()  { "$PYTEST" -v --tb=short; }
gate_stress() { "$PYTEST" tests/test_stress.py -v --tb=short --durations=10; }
gate_docs()   { "$MKDOCS" build --strict; }
gate_build()  {
  rm -rf "${ROOT}/dist"
  # Mirror release.yml's `python -m build`. Prefer `uv build` since the
  # uv-managed venv has no pip; fall back to the build module otherwise.
  if command -v uv >/dev/null 2>&1; then
    uv build
  elif "$PY" -c "import build" 2>/dev/null; then
    "$PY" -m build
  else
    fail "neither uv nor the 'build' module is available"
    return 1
  fi
}
gate_guard() {
  # Mirror guard-main-push.yml: a *direct* push to main may only touch
  # docs/, CLAUDE.md, .claude/commands/. Compare the current branch's
  # changes against origin/main and warn on disallowed paths.
  local base changed non_docs
  base="$(git merge-base HEAD origin/main 2>/dev/null)" || {
    warn "no origin/main to compare against — skipping guard"
    return 0
  }
  changed="$(git diff --name-only "$base" HEAD)"
  if [ -z "$changed" ]; then
    ok "no changes vs origin/main"
    return 0
  fi
  non_docs="$(printf '%s\n' "$changed" | grep -vE '^(docs/|CLAUDE\.md|\.claude/commands/)' || true)"
  if [ -n "$non_docs" ]; then
    warn "not docs-only — a DIRECT push to main would be rejected by the guard:"
    printf '%s\n' "$non_docs" | sed 's/^/    /'
    warn "(fine if you're merging via PR — the guard only blocks direct pushes)"
  else
    ok "changes are docs-only — direct push to main allowed"
  fi
  return 0
}

# --- run ---------------------------------------------------------------------
printf '%sLocal CI%s  ·  gates: %s  ·  %s\n' "$B" "$X" "${GATES[*]}" "$REDIS_URL"
for g in "${GATES[@]}"; do
  case "$g" in
    tests)  run_gate tests  gate_tests  ;;
    stress) run_gate stress gate_stress ;;
    docs)   run_gate docs   gate_docs   ;;
    build)  run_gate build  gate_build  ;;
    guard)  run_gate guard  gate_guard  ;;
  esac
done

# --- summary -----------------------------------------------------------------
step "summary"
for line in "${RESULTS[@]}"; do printf '  %s\n' "$line"; done
if [ "$overall" -eq 0 ]; then
  printf '\n%sAll gates passed.%s\n' "$G" "$X"
else
  printf '\n%sSome gates failed.%s\n' "$R" "$X"
fi
exit "$overall"
