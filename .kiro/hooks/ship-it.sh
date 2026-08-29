#!/usr/bin/env bash
# ship-it.sh — triggered by the "ship it" Kiro hook
# Workflow:
#   1. Parse the incoming prompt from stdin (JSON from Kiro)
#   2. Guard: only run when the user typed "ship it"
#   3. Verify SSH auth is available (fail fast, no hanging)
#   4. Fetch latest main from remote (no branch switch)
#   5. If on main/master: create a timestamped feature branch
#      Otherwise: stay on the current branch
#   6. Stage all changes and commit
#   7. Push branch to origin with upstream tracking
#   8. Open a PR via gh CLI (if available) or print the compare URL

set -euo pipefail

# ── 1. Read + parse the hook payload ────────────────────────────────────────
PAYLOAD=$(cat)
PROMPT=$(echo "$PAYLOAD" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('userMessage', data.get('prompt', '')).strip().lower())
" 2>/dev/null || echo "")

# ── 2. Guard — only run for "ship it" ───────────────────────────────────────
if [[ "$PROMPT" != "ship it" ]]; then
  exit 0
fi

REPO_DIR="/home/aaron/workspace/animatronic-v2"
cd "$REPO_DIR"

echo "🚢  Ship it! Starting release workflow..."

# ── 3. Pre-flight: verify SSH auth without blocking ─────────────────────────
if ! GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10" \
     git ls-remote --exit-code origin HEAD &>/dev/null; then
  echo "❌  Cannot reach origin — SSH agent not running or key not loaded." >&2
  echo "    Fix: run 'ssh-add ~/.ssh/id_ed25519' then try again." >&2
  exit 1
fi

# ── 4. Fetch latest main (remote-only, no local branch switch) ──────────────
git fetch origin main
echo "✅  Fetched latest main from origin"

# ── 5. Determine working branch ─────────────────────────────────────────────
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

if [[ "$CURRENT_BRANCH" == "main" || "$CURRENT_BRANCH" == "master" ]]; then
  # On a protected branch — create a new feature branch
  BRANCH="feat/ship-it-${TIMESTAMP}"
  git checkout -b "$BRANCH"
  echo "✅  Created branch: $BRANCH (was on $CURRENT_BRANCH)"
else
  # Already on a feature branch — commit here and PR against main
  BRANCH="$CURRENT_BRANCH"
  echo "✅  Using current branch: $BRANCH"
fi

# ── 6. Stage and commit ──────────────────────────────────────────────────────
# Check there is actually something to commit
if git diff --quiet && git diff --cached --quiet; then
  echo "⚠️   Nothing to commit — working tree is clean." >&2
  echo "    Make some changes first, then 'ship it'." >&2
  exit 1
fi

git add -A

CHANGED=$(git diff --cached --name-only | head -20 | tr '\n' ' ')
COMMIT_MSG="feat(ship-it): ship changes from ${TIMESTAMP}

Files changed: ${CHANGED}"

git commit -m "$COMMIT_MSG"
echo "✅  Committed: $CHANGED"

# ── 7. Push branch to origin ────────────────────────────────────────────────
git push -u origin "$BRANCH"
echo "✅  Pushed $BRANCH to origin"

# ── 8. Open PR ───────────────────────────────────────────────────────────────
REMOTE_URL=$(git remote get-url origin)
REPO_PATH=$(echo "$REMOTE_URL" | sed 's|git@github.com:||;s|\.git$||')

if command -v gh &>/dev/null; then
  gh pr create \
    --base main \
    --head "$BRANCH" \
    --title "feat(ship-it): changes from ${TIMESTAMP}" \
    --body "Automated PR created via 'ship it' Kiro hook.

**Branch:** \`${BRANCH}\`
**Base:** \`main\`
**Changed files:**
\`\`\`
${CHANGED}
\`\`\`"
  echo "✅  PR created via gh CLI"
else
  PR_URL="https://github.com/${REPO_PATH}/compare/main...${BRANCH}?expand=1"
  echo ""
  echo "📋  gh CLI not found — open this URL to create your PR:"
  echo "    $PR_URL"
  echo ""
fi

echo "🎉  Ship it complete! Branch: $BRANCH"
