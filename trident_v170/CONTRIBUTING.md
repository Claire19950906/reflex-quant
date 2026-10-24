# Contributing to Reflex Quant 🪞

Thanks for being here. Reflex Quant is open because **the meta-reflection loop
only gets smarter if more people feed it real-world reasoning failures.**

## Quick links

- 🐞 **Bug reports & feature requests** → [open an issue](../../issues)
- 💬 **Questions / design discussion** → [GitHub Discussions](../../discussions)
- 🔒 **Security issues** → see [`SECURITY.md`](./SECURITY.md) — *do not* open a public issue
- 📧 **Email the founder directly:** z2132743607@163.com

## What we especially welcome

1. **New reflection checkers (E028+)** — the meta-loop proposes them, but
   you have to ship them. Format: a `checker_xxx.py` module + a unit test
   that demonstrates a real false-negative the existing 27 missed.
2. **Cross-asset test fixtures** — `ai_decisions` rows where the trade was
   validated by the BTC/ETH/XAU/WTI universe. More universes → broader audit.
3. **Reasoning-quality research** — papers / blog posts / datasets on
   LLM trading-deception, overconfidence, or self-correction. Drop them
   in `docs/research/` and open a PR.
4. **Frontend polish** — the Next.js app has known rough edges (loading
   states, mobile responsive). Visual PRs welcome.
5. **Localization** — README is in Chinese + English. Translations to
   日本語 / 한국어 / Español / Português are very welcome.

## Development setup

```bash
git clone https://github.com/Claire19950906/reflex-quant.git
cd reflex-quant

# Backend
cp .env.example backend/.env       # fill in OPENROUTER_API_KEY
pip install -r backend/requirements.txt
cd backend && python -m pytest tests/ -q    # all tests should pass offline

# Frontend
cd ../frontend && npm ci && npm run dev     # http://localhost:3000

# Engine
cd ../backend/src_python && python engine.py
```

## Coding conventions

- **Python:** PEP 8 + type hints on every new function. `ruff` is your friend.
- **Commits:** conventional commits (`feat:`, `fix:`, `refactor:`, `docs:`,
  `test:`, `chore:`). One concern per commit.
- **PRs:** keep them small (< 400 lines diff). Reference the issue they close.
- **Tests:** new logic ships with a pytest case. We have 189 today;
  don't make it 188.
- **DB schema:** *never* hand-edit SQLite. All migrations live in
  `backend/src_python/db.py` (idempotent `migrate()`).

## Pull request process

1. Fork + branch (`git checkout -b feat/e028-cross-asset-leakage`)
2. Make your change + add a test
3. Run `pytest -q` — must stay green
4. Run `ruff check backend/` — must stay clean
5. Open a PR, fill in the template, link the issue
6. Wait for review. The founder reviews personally; turnaround is 1–3 days.

## What we will *not* merge

- New dependencies that bloat install size without clear ROI
- "Drive-by" refactors with no behavior change
- Live-trading code paths without a corresponding paper-trading fixture
- Anything that weakens the audit log (the reflection engine is the whole moat)

## Code of Conduct

By participating, you agree to [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md).
Be kind. We are all here because quant trading is hard enough already.

<sub>v225 — 2026-10-06</sub>
