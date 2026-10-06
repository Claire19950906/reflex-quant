#!/usr/bin/env bash
# deploy.sh — push reflex-quant-public to GitHub in one command.
# Usage:  ./deploy.sh <github-username> [repo-name]
# Example: ./deploy.sh reflex-quant reflex-quant
#
# Requires:  GitHub Personal Access Token (fine-grained, repo scope)
# Get one:  https://github.com/settings/tokens?type=beta
#           → Generate new token → Name: "reflex-quant-deploy"
#           → Repository access: only select repos → pick the new repo (or "all")
#           → Permissions: Contents (read+write), Metadata (read-only)

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

USER="${1:-}"
REPO="${2:-reflex-quant}"

if [ -z "$USER" ]; then
  cat <<EOF
Usage: ./deploy.sh <github-username> [repo-name]

Example:
  ./deploy.sh reflex-quant reflex-quant
  ./deploy.sh yourname reflex-quant

You'll need a GitHub Personal Access Token (PAT).
Get one at: https://github.com/settings/tokens?type=beta
  - Generate new token
  - Repository access: select repos you want to deploy to
  - Permissions: Contents = read+write, Metadata = read-only

The token is read securely (no echo) and used only for this push.
EOF
  exit 1
fi

# 1. init repo if not already
if [ ! -d .git ]; then
  echo "==> git init"
  git init -b main > /dev/null
fi

# 2. stage everything (respects .gitignore)
echo "==> git add ."
git add .

# 3. commit (only if there are staged changes)
if git diff --cached --quiet; then
  echo "==> nothing to commit (working tree clean)"
else
  echo "==> git commit"
  git commit -m "v225: initial public release

- 30 docs + showcase + screenshots
- 7-layer reflection engine architecture
- 3 case studies (WTI short, XAU/USD long, WTI long + meta-loop)
- 6 screenshot library for pitch decks
- LICENSE-COMMERCIAL.html with interactive pricing calculator
- Netlify + GitHub Pages deploy configs
- Tech blog: 'How a self-aware AI caught its own blindspot'" \
    --quiet
fi

# 4. ask for token
echo ""
echo "==> GitHub Personal Access Token"
echo "    (input is hidden; paste then press Enter)"
echo ""
read -s TOKEN
echo ""

if [ -z "$TOKEN" ]; then
  echo "No token provided. Aborting."
  exit 1
fi

# 5. create repo via API (idempotent: if it exists, this fails silently and we push anyway)
echo "==> creating repo '$REPO' on github.com/$USER"
HTTP_CODE=$(curl -s -o /tmp/_rq_create.json -w "%{http_code}" \
  -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/user/repos" \
  -d "{\"name\":\"$REPO\",\"description\":\"Self-aware AI for quant trading — 7-layer reflection + meta-loop. Catches the trades your risk engine misses.\",\"private\":false,\"has_issues\":true,\"has_wiki\":false}")

if [ "$HTTP_CODE" = "201" ]; then
  echo "    -> repo created."
elif [ "$HTTP_CODE" = "422" ]; then
  echo "    -> repo already exists (that's fine, continuing)."
else
  echo "    -> HTTP $HTTP_CODE. Inspect /tmp/_rq_create.json. Continuing push anyway."
fi

# 6. set remote and push
echo "==> git push"
git remote remove origin 2>/dev/null || true
git remote add origin "https://${TOKEN}@github.com/${USER}/${REPO}.git"
git push -u origin main --force

# 7. enable Pages
echo ""
echo "==> enabling GitHub Pages (source: main / root)"
sleep 2  # give github a moment to register the repo
curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/repos/${USER}/${REPO}/pages" \
  -d '{"source":{"branch":"main","path":"/"}}' \
  -o /tmp/_rq_pages.json -w "    -> HTTP %{http_code}\n"

# 8. clean up token from git remote (best practice — don't leave in .git/config)
git remote set-url origin "https://github.com/${USER}/${REPO}.git"

echo ""
echo "================================================================"
echo "Done."
echo "Repo:    https://github.com/${USER}/${REPO}"
echo "Pages:   https://${USER}.github.io/${REPO}/"
echo "Showcase: https://${USER}.github.io/${REPO}/showcase/"
echo "Pricing: https://${USER}.github.io/${REPO}/LICENSE-COMMERCIAL.html"
echo "================================================================"
echo ""
echo "Next steps:"
echo "  1. Visit the repo, add topics: ai, quant-trading, self-reflection,"
echo "     trading-bot, meta-learning, fintech, knowledge-graph"
echo "  2. Settings -> About -> add website URL (your demo / domain)"
echo "  3. Settings -> Pages -> confirm Pages is enabled (may take 1 min)"
echo "  4. Optionally: drag the folder onto https://app.netlify.com/ for"
echo "     instant Netlify deploy (uses the included netlify.toml)"
echo ""
