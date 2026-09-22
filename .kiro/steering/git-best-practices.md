---
inclusion: always
---

# Git Best Practices

Follow these rules whenever you stage, commit, branch, push, or open pull requests in this repository.

## Golden Rules

- Only create commits when the user explicitly asks. Never commit unprompted.
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
