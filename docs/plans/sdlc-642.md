# sdlc-642 — Reviewer-authored checkbox commits invalidate their own REVIEW verdict trailer

Issue: [#642](https://github.com/tomcounsell/popoto/issues/642)

## Problem

`/do-pr-review` records its verdict with `sdlc-tool verdict finalize`, which pins a
`head_sha` to the commit it reviewed. Two things then move the head past that SHA:

1. The **reviewer's own** `docs(#NNN): sync plan checkboxes with review verdict` commit.
2. `/do-docs`, a **mandatory** SDLC stage that by design runs *after* REVIEW and commits.

Both invalidate the trailer, because every consumer compares the recorded SHA to the PR's
**live** head with strict equality. Observed on #635 / PR #637.

## Where the defect actually lives (acceptance criterion 1)

Traced by reading the components, not guessed:

| Component | Verdict |
|---|---|
| `~/.claude/skills/do-pr-review/sub-skills/post-review.md` § 2.5 | **This authors the commit.** Its "generic default" branch makes the tick as a surgical text edit, then `git add`/`git commit -m "docs(#N): sync plan checkboxes with review verdict"`/`git push origin HEAD:$BRANCH`. |
| `docs/sdlc/do-pr-review.md` (this repo) | Not the author. It lists "plan-checkbox updater" under *"Not declared here (use the generic defaults)"* — which is precisely what selects the generic branch above. |
| `~/.claude/skills/do-sdlc/SKILL.md` | Not the author; it is the *victim* — § 5d.4 HALTs the loop on `ok: false`. |
| `sdlc-tool` (`~/.local/bin/sdlc-tool`) | Not the author. A 95-line bash dispatcher to `tools.sdlc_*` in `$AI_REPO_ROOT` (`~/src/ai`). The equality comparison lives in `tools/sdlc_review_finalize.py::check_review_persistence` and `tools/merge_predicate.py::_check_verdict_freshness`. |

So the commit is authored by the **global skill**, and the hard failure is enforced by the
**control plane** (`~/src/ai`). Neither is in this repository; this repo only *declares* that
it uses the generic default.

## Why the maintainer's preferred direction is necessary but not sufficient

The roadmap preference — "decide whether the reviewer keeps write access to the branch, and if
not, move checkbox syncing to `/do-docs`" — is correct and adopted here. It fixes soundness:
with the reviewer no longer writing, the recorded `head_sha` is the commit a reviewer actually
inspected (criterion 3), and `finalize`'s atomicity claim stops being a lie (issue §"Why it
matters" 1).

It does **not** on its own satisfy criterion 2 ("a review-then-docs lane reaches `/do-merge`
with `selfcheck` `ok: true`"). `/do-docs` still commits after REVIEW, and strict SHA equality
against the live head fails on *any* later commit. So this plan does both:

- **Part A — the reviewer stops writing to the branch.** Checkbox syncing moves to `/do-docs`.
- **Part B — post-review drift that is provably documentation-only stops counting as
  staleness.** Everything else still fails closed.

Part B is issue direction 3, but mechanically defined rather than advisory: it is gated on
*ancestry* (the live head must descend from the reviewed SHA) **and** on every changed path in
the range being documentation. A source file changed after approval remains a hard
`REVIEW_TRAILER_MISSING` — that commit genuinely was not reviewed.

Direction 2 ("trailer pins the reviewed code tree") was rejected: a tree hash over non-docs
paths is a second identity for the same commit that no other tool in the pipeline speaks, and
it would silently accept a *rewritten* history (force-push) that the ancestry check in Part B
catches.

## The docs-only path set

Reused from this repo's already human-ratified definition in
`.github/workflows/guard-main-push.yml` (`^(docs/|CLAUDE\.md|\.claude/commands/)`), generalised
one notch for a control plane that serves several repos:

- anything under `docs/`
- any top-level `*.md` (`README.md`, `CLAUDE.md`, `AGENTS.md`, `CHANGELOG.md`)
- anything under `.claude/commands/`

Anything else — `src/`, `tests/`, `mkdocs.yml`, `.github/` — is code drift.

**Known limitation, deliberately accepted:** a `/do-docs` cascade that fixes a *docstring in a
source file* (a real pattern in this repo — see the `#709`/`#564` docstring lesson in
`CLAUDE.md`) produces code drift and will still fail closed. That is the correct answer, not a
gap: the file was not in the reviewed diff. The remedy is to re-run REVIEW, and the failure
message says so.

## Design

### Part A — reviewer no longer writes to the branch

`/do-pr-review` § 2.5 stops editing, committing, and pushing the plan file. Instead, on an
APPROVED verdict it emits a machine-readable marker in the review body, next to the existing
`REVIEW_CONTEXT` marker:

```html
<!-- PLAN_CHECKBOX_SYNC {"plan":"docs/plans/{slug}.md","criteria":[{"text":"...","verdict":"pass"}]} -->
```

`verdict` is the existing four-value contract (`pass` / `fail` / `acknowledged` / `n/a`) with
the existing write mapping (tick / untick / untick / leave alone) — the mapping moves, its
semantics do not.

`/do-docs` gains a step that reads the latest `## Review:` body for the PR, parses that marker,
and applies the ticks to the plan file. It needs no new commit: the cascade's existing
`git add -A && git commit` in its Step 5 carries them.

The "commit-then-post-review is non-negotiable" rationale in § 2.5 is rewritten rather than
deleted. It existed because `/do-merge`'s date-based freshness leg filters reviews older than
the newest commit. With the tick commit gone, the review is posted after every commit that
exists at review time, which is what that invariant actually wanted; and the SHA leg (which
takes precedence over the date leg in `_check_verdict_freshness`) is handled by Part B.

### Part B — docs-only drift is not staleness

New `~/src/ai/tools/sdlc_review_drift.py`:

```python
classify_head_drift(repo, base_sha, head_sha) -> "identical" | "docs_only" | "code" | "unknown"
```

Implemented over `gh api repos/{repo}/compare/{base}...{head}` so it works from any cwd and
needs no local checkout holding both SHAs. It returns `code` (fail closed) unless:

- `status == "ahead"` (head strictly descends from base — a force-push/divergence is never
  waved through), **and**
- `files` is present, non-empty, not truncated (the compare API caps at 300 files), and every
  `filename` matches the docs-only set.

Any error, timeout, unparseable payload, or missing field returns `unknown`, which callers
treat exactly like `code`.

Two consumers change, both at the point where they already have the mismatch in hand:

- `tools/sdlc_review_finalize.py::check_review_persistence` — on mismatch, classify; on
  `docs_only` set `trailer_matches_head: True` and report `head_drift: "docs_only"` in the
  returned dict so the tolerance is visible rather than silent.
- `tools/merge_predicate.py::_check_verdict_freshness` — on mismatch, classify; on `docs_only`
  append a note and pass instead of appending to `failed`.

`finalize`'s own self-verification is unaffected: at finalize time head == recorded, so the
classifier is never reached on that path.

## Tasks

- [x] Identify the authoring component (table above)
- [x] Part A1 — rewrite `post-review.md` § 2.5: no git write, emit `PLAN_CHECKBOX_SYNC`
- [x] Part A2 — `code-review.md`: list the new marker among the review-body parts
- [x] Part A3 — `do-docs/SKILL.md`: new plan-checkbox-sync step before the cascade commit
- [x] Part A4 — `do-sdlc/SKILL.md` § 5d.4: document the `head_drift` key
- [x] Part B1 — new `tools/sdlc_review_drift.py`
- [x] Part B2 — wire into `check_review_persistence`
- [x] Part B3 — wire into `_check_verdict_freshness`
- [x] Part B4 — unit tests for the classifier and both consumers
- [x] Repo-local: `docs/sdlc/do-pr-review.md` declares the no-branch-write contract
- [x] Repo-local: this plan doc

## Success Criteria

- [x] The component authoring the checkbox commit is named, with evidence (criterion 1)
- [x] `/do-pr-review` no longer runs `git commit` / `git push` on any path
- [x] The recorded `head_sha` is the commit the reviewer read (criterion 3)
- [x] A review-then-docs lane's `selfcheck` returns `ok: true` when the post-review drift is
      documentation-only (criterion 2)
- [x] Non-docs drift after review still fails `REVIEW_TRAILER_MISSING` (no self-clearing)
- [x] A force-push / divergent head after review still fails closed

## Verification

| Check | Command | Expected |
|---|---|---|
| Classifier unit tests | `cd ~/src/ai && uv run pytest tests/unit/test_sdlc_review_drift.py` | pass |
| Consumer unit tests | `cd ~/src/ai && uv run pytest tests/unit/test_sdlc_review_finalize.py tests/unit/test_merge_predicate.py` | pass |
| Reviewer writes nothing | `grep -nE '^\s*git (commit|push)' ~/.claude/skills/do-pr-review/sub-skills/*.md ~/.claude/skills/do-pr-review/SKILL.md` | no matches |
| Repo docs gate | `mkdocs build --strict` | exit 0 |

## No-Gos

- **[DESTRUCTIVE]** Do not make `selfcheck` pass on a *code* diff after approval. Self-clearing
  a review gate is the failure mode this whole gate exists to prevent.
- **[DESTRUCTIVE]** Do not have `/do-pr-review` re-run `finalize` to "refresh" the trailer after
  post-review commits — that mints APPROVED against an uninspected head.
- **[SEPARATE-SLUG]** Do not change the `head_sha`-in-its-own-field storage shape (#2769) or the
  legacy in-token trailer fallback.
- **[EXTERNAL]** The Part B changes land in `~/src/ai`, a different repository, on branch
  `session/sdlc-642`. This popoto PR cannot merge them.

## Note (out of scope): a lane cannot trust its inherited Redis binding

Observed while this lane ran, and recorded here because it is the same defect class as #642 —
a pipeline step whose environment is not what the operator assumes. It is **not** fixed by this
PR and should get its own slug.

The session runner injects `REDIS_URL=redis://localhost:6379/0` into the harness process. It is
not in `~/.zshrc`, `~/.zshenv`, `~/.zprofile` or any `settings*.json`, so this is not the
documented "unset falls back to `DEFAULT_URL`" hazard in CLAUDE.md — it is an *actively bound*
value no lane can correct from the inside. Two consequences the existing docs do not cover:

- `scripts/scratch_repro.py` is **not** affected, and it is worth saying so explicitly because the
  `os.environ.setdefault("REDIS_URL", ...)` on its step 1 *is* a no-op here and reads like a hole.
  It is not one: the no-op is intentional and documented ("honors an already-exported `REDIS_URL`"),
  and the protection is step 2, which resolves the database popoto actually bound to and
  `sys.exit`s on 0 before issuing a command. Its code comment names this exact case — "a stray
  `REDIS_URL` from the environment could still point elsewhere". Under the injected binding a
  script copied from the template refuses to run, which is the design working. The only residual
  is wording: the docstring's opening paragraph frames DB 0 as the fallback *when `REDIS_URL` is
  unset*, and never describes an actively-bound DB 0. The guard covers both regardless.
- Shell state does not persist between tool calls, so `export REDIS_URL=…` does not stick; each
  command must be prefixed inline.
- A second binder exists further in: the benchmark harness's `_resolve_bench_db()` overrides the
  pool onto `POPOTO_BENCH_DB` (default 14), silently taking precedence over `REDIS_URL`.

If the SDLC lane-setup surface gains a startup step, it should **assert** the bound database and
fail, rather than inherit whatever is present. Deliberately kept out of this PR: #642 is a
verdict-trailer defect and lane-setup work shares none of its files.

## Questions for the architect

1. **Merge authority for `~/src/ai`.** Part B is implemented on a `session/sdlc-642` branch in
   `~/src/ai` and left unmerged — that repo has its own SDLC and I was not routed to it.
   Criterion 2 is not live until it merges.
2. **Docs-only path set.** Is top-level `*.md` (beyond `CLAUDE.md`) acceptable to wave through,
   and should `mkdocs.yml` count as documentation? I excluded it — it is build configuration.
3. **Source-docstring cascades.** Accepted above as fail-closed. If you would rather those be
   tolerated too, the rule would have to become content-aware (docstring-only AST diff), which
   is a materially larger change and a separate slug.
