---
title: Git Best Practices
inclusion: always
---

# Git Best Practices

## Commit Messages
- Use conventional commit format: `type(scope): description`
- Types: feat, fix, docs, style, refactor, test, chore
- Keep first line under 50 characters
- Use imperative mood ("Add feature" not "Added feature")
- Include body for complex changes

## Branching
- Use feature branches for new development
- Keep main/master branch stable and deployable
- Use descriptive branch names (feature/user-auth, fix/login-bug)
- Delete merged branches to keep repository clean
- ALWAYS create a feature branch from an up-to-date `main`. Before branching: `git checkout main && git pull origin main`, then `git checkout -b <type>/<short-name>`.
- NEVER start a new feature branch from another feature branch, and never stack one unmerged feature branch on another. Each feature branch starts from the latest `main`.
- If work truly depends on another branch that is not yet merged, prefer waiting for that branch to merge to `main` first, then branch from the updated `main`. Only stack branches when the user explicitly asks, and call out the risk.

## Workflow
- Pull latest changes before starting work
- Commit frequently with logical chunks
- Use interactive rebase to clean up history before merging
- Review code before merging (pull requests)

## Feature Branch Workflow (follow every time)

Start of work:
1. `git checkout main`
2. `git pull origin main`  (start from the latest main)
3. `git checkout -b <type>/<short-descriptive-name>`

During work:
4. Commit in logical chunks with conventional-commit messages (only when the user asks to commit).
5. Stage specific files by name; do not `git add .`/`-A`. Never commit unrelated working-tree changes (e.g. editor/hook files, stray audio) — leave them unstaged.

Opening the PR:
6. Push with upstream tracking: `git push -u origin <branch>`.
7. Open the PR with `--base main` ALWAYS (e.g. `gh pr create --base main --head <branch> ...`). The PR base is `main` unless the user EXPLICITLY requests stacking on another branch.
8. Before requesting review, confirm the PR targets `main` and that the diff shows only the intended change.

After merge:
9. Verify the work actually landed on `main` (`git fetch origin && git branch -r --contains <merge-commit>` or check the PR merged into main), then delete the merged branch.

### Anti-patterns (do NOT do)
- Creating `feature-B` off `feature-A` while `feature-A` is still an open/unmerged PR.
- Opening a PR whose base is an unmerged feature branch (it can merge into a stale base and never reach main, especially if the base was squash-merged).
- Assuming a PR reached `main` because it shows "merged" — always confirm the merge target was `main`.
- Rebasing/force-pushing shared branches without the user's explicit go-ahead.

## Repository Management
- Use .gitignore to exclude build artifacts and secrets
- Keep repository size manageable (use Git LFS for large files)
- Tag releases with semantic versioning
- Document branching strategy in README

## Security
- Never commit secrets, API keys, or passwords
- Use environment variables for configuration
- Review commits for sensitive information
- Use signed commits when possible
