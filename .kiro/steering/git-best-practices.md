---
inclusion: always
---

# Git Best Practices

Follow these rules whenever you stage, commit, branch, push, or open pull requests in this repository.

## Who these rules bind (read first)

These rules apply to **every agent that touches git in this repo** — the
orchestrator AND any delegated workflow/sub-agent (e.g. `wf-coder`,
`wf-planner`, reviewers). "Implementing a feature" does **not** imply committing
it. An agent's job ends with the working-tree changes made and verified; it
leaves the commit to the user unless the user explicitly asked for a commit.

When work is delegated to a workflow:

- The orchestrator MUST state the git policy in the workflow brief: **do not
  commit, do not create or switch branches, leave changes in the working tree**
  unless the user explicitly requested a commit. If the user did request a
  commit, the brief must name the feature branch to use (never `main`).
- A delegated agent that was NOT told to commit MUST stop at verified
  working-tree changes and report what it changed. It MUST NOT run
  `git commit`, `git checkout -b`, `git merge`, or `git push` on its own
  initiative.

## Golden Rules

- **Only create commits when the user explicitly asks. Never commit unprompted** —
  this applies to delegated workflow agents exactly as it applies to the
  orchestrator. Finishing an implementation is not permission to commit.
- **Never commit directly to `main`.** `main` is not a commit target for
  feature work under any circumstance. All work goes on a `<type>/<name>`
  feature branch cut from an up-to-date `main`, even when the user has asked for
  a commit.
- Every feature branch starts from an up-to-date `main`. Never branch off another unmerged feature branch.
- Stage files by name. Never use `git add .` or `git add -A`.
- Never force-push, rebase, or reset shared branches without explicit user approval.
- Never commit secrets, credentials, or API keys.

## Commit Messages

- Use conventional commits: `type(scope): description` (types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`).
- Keep the subject line under 50 characters and in imperative mood ("Add feature", not "Added feature").
- Add a body to explain the "why" for non-trivial changes.

## Feature Branch Workflow

Follow this sequence every time.

Start of work:
1. `git checkout main`
2. `git pull origin main` — always branch from the latest `main`.
3. `git checkout -b <type>/<short-descriptive-name>` (e.g. `feat/jaw-sync`, `fix/servo-clamp`).

During work:
4. Commit in logical chunks with conventional-commit messages (only when the user asks).
5. Stage specific files by name. Leave unrelated working-tree changes unstaged — in this repo that especially means editor/hook files, `app.log`, `.servo.lock`, and stray `audio/*.wav` recordings.

Opening the PR:
6. Push with upstream tracking: `git push -u origin <branch>`.
7. Open the PR against `main`: `gh pr create --base main --head <branch> ...`. The base is `main` unless the user EXPLICITLY asks to stack on another branch.
8. Before requesting review, confirm the PR targets `main` and the diff shows only the intended change.
9. Keep PR titles under ~70 characters; put detail in the description.

After merge:
10. Confirm the work landed on `main` (`git fetch origin && git branch -r --contains <merge-commit>`, or verify the PR merged into `main`), then delete the merged branch.

## Anti-Patterns (do NOT do)

- **Committing without the user asking** — including a delegated workflow agent
  committing its own implementation because the task "felt done." No commit
  unless the user explicitly requested one.
- **Committing to `main`** — committing feature work straight onto `main`
  instead of a feature branch. This has happened via delegated agents; the ban
  applies to them too.
- Delegating implementation work in a brief that is silent on git, letting the
  agent choose to commit. The brief must state the no-commit (or
  commit-on-named-branch) policy explicitly.
- Branching `feature-B` off `feature-A` while `feature-A` is still an open PR.
- Opening a PR whose base is an unmerged feature branch (it can merge into a stale base and never reach `main`, especially after a squash-merge).
- Assuming a PR reached `main` just because it shows "merged" — always confirm the merge target.
- Rebasing or force-pushing a shared branch without the user's go-ahead.

## Repository Hygiene

- Keep `main` stable and deployable at all times.
- Use `.gitignore` to exclude build artifacts, virtual envs (`.venv`), logs, and secrets.
- Use environment variables for configuration; never hardcode secrets in tracked files.
- Review each commit's diff for sensitive information before pushing.
- Tag releases with semantic versioning.
- Preserve git hooks — do not skip them with `--no-verify` unless the user asks.
- Leave `git config` unchanged.
