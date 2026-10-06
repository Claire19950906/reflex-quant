#!/usr/bin/env bash
# deploy.sh — push reflex-quant-public to GitHub in one command.
# Usage:  ./deploy.sh <github-username> [repo-name]
#
# Robust version: every step prints, errors are visible, no silent aborts.

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

USER="${1:-}"
REPO="${2:-reflex-quant}"

if [ -z "$USER" ]; then
  cat <<EOF
Usage: ./deploy.sh <github-username> [repo-name]
EOF
  exit 1
fi

step() { echo; echo "==> $*"; }

# 1. init repo if not already
step "git init"
if [ ! -d .git ]; then
  git init -b main > /dev/null 2>&1 || git init > /dev/null
  echo "    -> initialized"
else
  echo "    -> already initialized"
fi

# 2. stage everything
step "git add ."
git add .

# 3. commit (only if there are staged changes)
step "git commit"
if git diff --cached --quiet; then
  echo "    -> nothing to commit (working tree clean)"
else
  git commit -m "v225: initial public release

- 30 docs + showcase + screenshots
- 7-layer reflection engine architecture
- 3 case studies (WTI short, XAU/USD long, WTI long + meta-loop)
- 6 screenshot library for pitch decks
- LICENSE-COMMERCIAL.html with interactive pricing calculator
- i18n (EN + 中文) on showcase + pricing
- Tier-1 GH assets: CHANGELOG, CODEOWNERS, FUNDING, CodeQL, Dependabot
- 404, robots, sitemap, social-preview, banner" 2>&1 | tail -3
fi

# 4. ask for token
# priority: $GITHUB_PAT env var > $2 arg > interactive prompt
if [ -n "$GITHUB_PAT" ]; then
  TOKEN="$GITHUB_PAT"
  echo "==> using GITHUB_PAT env var (${#TOKEN} chars)"
elif [ -n "$3" ]; then
  TOKEN="$3"
  echo "==> using token from arg 3 (${#TOKEN} chars)"
else
  echo
  echo "==> GitHub Personal Access Token"
  echo "    (input is hidden; paste then press Enter)"
  echo
  read -s TOKEN
  echo
fi

if [ -z "$TOKEN" ]; then
  echo "ERROR: No token provided. Aborting."
  exit 1
fi
echo "    -> got token (${#TOKEN} chars)"

# 5. create repo via API (idempotent)
step "creating repo '$REPO' on github.com/$USER (idempotent)"
HTTP_CODE=$(curl -sS --max-time 30 -o /tmp/_rq_create.json -w "%{http_code}" \
  -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/user/repos" \
  -d "{\"name\":\"$REPO\",\"description\":\"Self-aware AI for quant trading — 7-layer reflection + meta-loop. Catches the trades your risk engine misses.\",\"private\":false,\"has_issues\":true,\"has_wiki\":false}" 2>&1) || HTTP_CODE="000"

echo "    -> HTTP $HTTP_CODE"
if [ "$HTTP_CODE" = "201" ]; then
  echo "    -> repo created"
elif [ "$HTTP_CODE" = "422" ]; then
  echo "    -> repo already exists (continuing)"
elif [ "$HTTP_CODE" = "401" ]; then
  echo "    -> ERROR: bad credentials. Check PAT."
  cat /tmp/_rq_create.json 2>/dev/null
  exit 1
else
  echo "    -> WARNING: unexpected HTTP code. Continuing push anyway."
  cat /tmp/_rq_create.json 2>/dev/null
fi

# 6. push
step "git push to origin"
git remote remove origin 2>/dev/null || true
git remote add origin "https://${TOKEN}@github.com/${USER}/${REPO}.git"
if git push -u origin main --force 2>&1 | tail -8; then
  echo "    -> push OK"
else
  echo "    -> ERROR: push failed. See above."
  exit 1
fi

# 7. enable Pages
step "enabling GitHub Pages (source: main / root)"
sleep 2
HTTP_CODE=$(curl -sS --max-time 30 -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/repos/${USER}/${REPO}/pages" \
  -d '{"source":{"branch":"main","path":"/"}}' \
  -o /tmp/_rq_pages.json -w "%{http_code}" 2>&1) || HTTP_CODE="000"

echo "    -> HTTP $HTTP_CODE"
if [ "$HTTP_CODE" = "201" ]; then
  echo "    -> Pages enabled"
elif [ "$HTTP_CODE" = "409" ]; then
  echo "    -> Pages already enabled (that's fine)"
elif [ "$HTTP_CODE" = "404" ]; then
  echo "    -> ERROR: repo not found yet. Wait a few seconds and re-run."
else
  echo "    -> WARNING: unexpected code"
  cat /tmp/_rq_pages.json 2>/dev/null
fi

# 8. clean up token from git remote
step "scrubbing PAT from git remote"
git remote set-url origin "https://github.com/${USER}/${REPO}.git"
echo "    -> done"

# 9. poll Pages build status (max 2 min)
step "waiting for Pages build (max 2 min)"
for i in 1 2 3 4 5 6 7 8; do
  sleep 15
  STATUS=$(curl -sS --max-time 10 \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${USER}/${REPO}/pages" 2>/dev/null \
    | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    print(d.get("status","?"))
except: print("?")' 2>/dev/null)
  echo "    [$((i*15))s] Pages status: $STATUS"
  [ "$STATUS" = "built" ] && break
done

echo
echo "================================================================"
echo "DONE."
echo "Repo:     https://github.com/${USER}/${REPO}"
echo "Pages:    https://${USER}.github.io/${REPO}/"
echo "Showcase: https://${USER}.github.io/${REPO}/showcase/"
echo "Pricing:  https://${USER}.github.io/${REPO}/LICENSE-COMMERCIAL.html"
echo "================================================================"
echo "If 'Pages status: built' appeared above, visit the URLs now."
echo "If not, give it 1 more minute — GitHub Pages first build can take 2 min total."
