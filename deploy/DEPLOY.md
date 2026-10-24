# Deploying Reflex Quant public site

This repo is a static site — no build step needed. Three deployment options:

## Option 1: Netlify (recommended, ~60 seconds)

```bash
# from repo root
brew install netlify-cli   # if not installed
netlify login
netlify init               # creates site, links repo
netlify deploy --prod      # publishes to *.netlify.app
```

Or zero-CLI: go to https://app.netlify.com/, drag the `reflex-quant-public/` folder onto the deploy widget.

## Option 0: One-shot GitHub + Pages deploy (~2 minutes)

We bundled a one-shot script that does everything: `init` → `commit` → `create repo via API` → `push` → `enable Pages`. It will create the GitHub repo with the description we wrote, enable GitHub Pages, and print your final URLs.

```bash
cd ~/Desktop/reflex-quant-public
./deploy.sh <your-github-username>
```

You'll be prompted for a [GitHub Personal Access Token](https://github.com/settings/tokens?type=beta) (fine-grained, repo scope). The token is read securely (no echo) and used only for this push.

That's it. Final output will be:
```
Repo:    https://github.com/Claire19950906/reflex-quant
Pages:   https://Claire19950906.github.io/reflex-quant/
Showcase: https://Claire19950906.github.io/reflex-quant/showcase/
Pricing: https://Claire19950906.github.io/reflex-quant/LICENSE-COMMERCIAL.html
```

The included `netlify.toml` configures:
- Security headers (X-Frame-Options, X-Content-Type-Options)
- Cache rules (showcase 1h, assets 24h)
- Redirects: `/demo` → `/showcase/`, `/pricing` → `/LICENSE-COMMERCIAL.html`, `/blog` → `/docs/blog/`

After first deploy, add a custom domain in Netlify's Domain settings. SSL is automatic.

## Option 2: GitHub Pages (free, ~5 minutes)

```bash
git init
git add .
git commit -m "Initial public release v225"
# on github.com, create repo (e.g. yourname/reflex-quant)
git remote add origin https://github.com/yourname/reflex-quant.git
git push -u origin main

# then on github.com: Settings → Pages → Source: main / root → Save
# your site is live at https://yourname.github.io/reflex-quant/
```

The included `.github/workflows/pages.yml` does this automatically on every push to `main` if you enable GitHub Actions for Pages.

## Option 3: Cloudflare Pages / Vercel

Both work the same way — drag the folder, or connect the git repo. No config needed; `netlify.toml` is ignored.

## What gets deployed

```
/
├── README.md                           ← landing page (github pages) or homepage
├── LICENSE                             ← AGPL-3.0 + Commercial summary
├── LICENSE-COMMERCIAL.html             ← /pricing — interactive calculator
├── showcase/index.html                 ← /demo — interactive showcase
├── docs/PHILOSOPHY.md                  ← philosophy
├── docs/ARCHITECTURE.md                ← architecture
├── docs/CASE_STUDIES.md                ← case studies
├── docs/ROADMAP.md                     ← roadmap
├── docs/FAQ.md                         ← FAQ
├── docs/CONTRIBUTORS.md                ← contributors
├── docs/blog/                          ← blog
├── examples/                           ← case studies in detail
├── assets/screenshots/                 ← PNG screenshots (6)
├── frontend/README.md                  ← UI reference
├── .github/                            ← issue/PR templates
└── netlify.toml                        ← Netlify config
```

## What does NOT get deployed

The `.gitignore` blocks the following from ever reaching your public repo:
- `.db`, `.sqlite` — knowledge graph / reflection cycle databases
- `.env`, `.env.local` — API keys
- `*.log`, `llm_calls/` — audit logs
- `META_REFLECTION.md` — private self-reflection notes
- `kg_*` — knowledge graph snapshots
- `reflex_cycles/`, `traces/` — training traces

If you're paranoid (good), run `git status` before `git push` and confirm there are no files matching those patterns.

## Adding a custom domain

After first deploy, both Netlify and GitHub Pages support custom domains via:
- Netlify: Domain settings → Add domain → follow DNS instructions
- GitHub Pages: Settings → Pages → Custom domain → add CNAME file at repo root

SSL is automatic for both. Plan ~10 minutes for DNS propagation.

## Analytics (optional)

We do **not** include analytics by default. If you want to add Plausible / Fathom / Cloudflare Analytics, edit the `[[headers]]` block in `netlify.toml` or add the JS snippet to your homepage.

Recommended: Cloudflare Web Analytics (free, no cookies, GDPR-friendly). One line in your HTML:
```html
<script defer src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{"token": "your-token"}'></script>
```

## Need help?

Open an issue at the repo, or email z2132743607@163.com. Response within 24h on weekdays.
