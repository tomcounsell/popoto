---
status: Planning
type: feature
appetite: Small
owner: Valor
created: 2026-03-23
tracking: https://github.com/tomcounsell/popoto/issues/256
last_comment_id:
---

# Opt-In Error Reporting

## Problem

Popoto is a library used by external applications. When users hit bugs (connection failures, encoding errors, query edge cases), the library author has no visibility unless users manually file GitHub issues — which most don't.

**Current behavior:**
Exceptions are raised to the consuming application. The library author never learns about errors in the wild unless someone reports them.

**Desired outcome:**
Users who opt in can automatically report Popoto-specific errors back to the library author's Sentry project (yudame/popoto). The reporter must be completely invisible — if it fails, crashes, or sentry-sdk isn't installed, Popoto works exactly as before.

## Prior Art

No prior issues or PRs found related to error reporting or Sentry integration.

## Spike Results

### spike-1: Isolated Sentry client in sentry-sdk 2.x
- **Assumption**: "A library can create an isolated Sentry client that doesn't interfere with the app's own Sentry"
- **Method**: web-research
- **Finding**: Confirmed. `sentry_sdk.Client()` + `sentry_sdk.Scope()` with `set_client()` creates a fully isolated reporting channel. Setting `default_integrations=False` and `auto_enabling_integrations=False` prevents any global side effects.
- **Confidence**: high
- **Impact on plan**: This is the core approach — no design changes needed.

### spike-2: Non-blocking transport
- **Assumption**: "Sentry event submission won't add latency to library operations"
- **Method**: web-research
- **Finding**: Confirmed. The default `HttpTransport` uses a `BackgroundWorker` with a thread-based queue. Events are enqueued and sent asynchronously. If the queue is full, events are silently dropped.
- **Confidence**: high
- **Impact on plan**: No custom transport needed — the default is already non-blocking.

## Architectural Impact

- **New dependencies**: `sentry-sdk>=2.0.0` as optional dependency under `[monitoring]` extra
- **Interface changes**: One new public function `enable_error_reporting()` added to `popoto` namespace
- **Coupling**: Zero coupling — the module is self-contained, wraps everything in try/except, and is a no-op if not enabled
- **Reversibility**: Trivially removable — single module + one import + one pyproject.toml line

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

No prerequisites — sentry-sdk is optional and the DSN is public/client-side safe.

## Solution

### Key Elements

- **`src/popoto/_error_reporting.py`**: Self-contained module with isolated Sentry client, lazy init, and `capture_exception()` helper
- **`enable_error_reporting()`**: Public opt-in function exported from `popoto.__init__`
- **Exception hooks**: Instrument Popoto's custom exception classes to auto-report when enabled

### Technical Approach

1. **Isolated Sentry client**: Use `sentry_sdk.Client(dsn=..., default_integrations=False)` + `Scope()` — never calls `sentry_sdk.init()`, never touches the app's global scope

2. **Opt-in activation**: `popoto.enable_error_reporting()` initializes the isolated client. Without this call, zero code runs. The DSN is hardcoded (it's a public/client-side key — standard practice for Sentry).

3. **Exception instrumentation**: Override `__init_subclass__` or patch the base exception classes to call `capture_exception()` when raised. Alternatively, simpler: instrument the key raise sites in `Model.save()`, `Query.filter()`, etc. via a decorator or explicit calls.

   **Chosen approach**: Patch the custom exception `__init__` methods. When error reporting is enabled, constructing a `ModelException`, `QueryException`, etc. triggers a capture. This is the least invasive — no changes to any existing code paths, just a monkey-patch applied by `enable_error_reporting()`.

4. **Total isolation from library behavior**:
   - Every call wrapped in `try: ... except Exception: pass`
   - `sentry-sdk` import failure → silent no-op
   - Network failure → silently dropped by Sentry's background worker
   - Never re-raises, never logs, never delays

5. **Filtering**: `before_send` callback ensures only exceptions with popoto frames in the traceback are sent

### Flow

**User calls** `popoto.enable_error_reporting()` → **Lazy init** creates isolated Client + Scope → **Exception raised** in popoto code → **Patched `__init__`** calls `capture_exception()` → **Background worker** sends event to yudame/popoto Sentry → **Exception propagates normally** to the consuming app

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] The entire `_error_reporting.py` module is wrapped in defensive try/except — test that any internal failure is silently swallowed
- [ ] Test that `enable_error_reporting()` is a no-op when sentry-sdk is not installed

### Empty/Invalid Input Handling
- [ ] Test `enable_error_reporting()` with no DSN set and sentry-sdk not installed — must not raise
- [ ] Test `capture_exception(None)` — must not raise

### Error State Rendering
- No user-visible output from this feature

## Test Impact

No existing tests affected — this is a greenfield feature that adds a new module and patches exception classes only when explicitly enabled. All existing exception behavior is unchanged.

## Rabbit Holes

- **Custom transport**: The default async transport is already non-blocking. Don't build a custom one.
- **Instrumenting every raise site**: Patching exception `__init__` is simpler than touching 74 raise sites across 17 files.
- **User-identifiable data scrubbing**: For v1, Sentry's default PII scrubbing is sufficient. Don't build custom scrubbers.
- **Configuration options** (sample rates, environment tags): Keep it simple — one function, no arguments.

## Risks

### Risk 1: Patched exception `__init__` adds latency to exception creation
**Impact:** Slower exception paths in hot loops
**Mitigation:** The capture call is fire-and-forget (enqueue to background thread). The overhead is a single function call + queue append — microseconds. If sentry client init failed, it's a no-op.

### Risk 2: Users unaware their errors are being sent
**Impact:** Privacy/trust concerns
**Mitigation:** Requires explicit `enable_error_reporting()` call — impossible to accidentally enable. Docstring clearly states what it does.

## Race Conditions

No race conditions identified — the isolated client uses Sentry's thread-safe BackgroundWorker internally, and `enable_error_reporting()` is idempotent (second call is a no-op).

## No-Gos (Out of Scope)

- No `sentry_sdk.init()` — library never touches the global Sentry state
- No configuration options beyond enable/disable
- No PII scrubbing beyond Sentry defaults
- No performance tracing (`traces_sample_rate=0`)
- No breadcrumbs or custom context
- No disable function (restart the process to disable)

## Documentation

### Feature Documentation
- [ ] Add error reporting section to `docs/configuration.md`

### Inline Documentation
- [ ] Docstring on `enable_error_reporting()` explaining what it does and that it's opt-in

## Success Criteria

- [ ] `popoto.enable_error_reporting()` exists and is callable
- [ ] When enabled, raising a `ModelException` sends an event to Sentry (verified in test with mock transport)
- [ ] When not enabled, zero Sentry code executes
- [ ] When sentry-sdk is not installed, `enable_error_reporting()` silently does nothing
- [ ] Internal failures in the reporter never propagate to the consuming app
- [ ] The app's own `sentry_sdk.init()` is unaffected
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (error-reporting)**
  - Name: reporter-builder
  - Role: Implement `_error_reporting.py` module and integrate into `__init__.py`
  - Agent Type: builder
  - Resume: true

- **Validator (error-reporting)**
  - Name: reporter-validator
  - Role: Verify isolation, no-op behavior, and non-interference
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Create error reporting module
- **Task ID**: build-reporter
- **Depends On**: none
- **Validates**: tests/test_error_reporting.py (create)
- **Assigned To**: reporter-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/_error_reporting.py` with isolated Client + Scope
- Add `before_send` filter for popoto-only exceptions
- Implement `enable_error_reporting()` that patches exception `__init__` methods
- Add `sentry-sdk>=2.0.0` to `[monitoring]` extra in pyproject.toml
- Export `enable_error_reporting` from `__init__.py`

### 2. Write tests
- **Task ID**: build-tests
- **Depends On**: build-reporter
- **Validates**: tests/test_error_reporting.py
- **Assigned To**: reporter-builder
- **Agent Type**: builder
- **Parallel**: false
- Test enable with mock transport captures events
- Test enable without sentry-sdk installed is silent no-op
- Test internal reporter failure doesn't propagate
- Test app's own Sentry init is unaffected

### 3. Validate
- **Task ID**: validate-reporter
- **Depends On**: build-tests
- **Assigned To**: reporter-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite
- Verify `_error_reporting.py` has no bare imports of sentry_sdk at module level
- Verify every code path is wrapped in try/except
- Verify no global Sentry state is modified

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-reporter
- **Assigned To**: reporter-builder
- **Agent Type**: documentarian
- **Parallel**: false
- Add error reporting section to `docs/configuration.md`
- Include install command: `pip install popoto[monitoring]`

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check .` | exit code 0 |
| Module exists | `python -c "from popoto._error_reporting import enable_error_reporting"` | exit code 0 |
| Public API | `python -c "import popoto; popoto.enable_error_reporting"` | exit code 0 |
| No global init | `grep -r 'sentry_sdk.init' src/popoto/` | exit code 1 |
| No bare sentry import at module level | `python -c "import popoto"` | exit code 0 |

---

## Open Questions

1. **DSN source**: Should we hardcode the yudame/popoto DSN (standard for client-side Sentry keys) or require `POPOTO_SENTRY_DSN` env var? Hardcoded is simpler and truly zero-config for opt-in users. Env var gives you the option to change it without a release.

2. **What gets sent**: Beyond the exception type/message/traceback, should we include the Popoto version and Python version? (Sentry captures these by default, just confirming that's sufficient.)

3. **Disable mechanism**: Current plan has no `disable_error_reporting()`. Is restarting the process acceptable, or do you need runtime disable?
