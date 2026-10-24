# Roadmap — What's coming

> This is the public roadmap. Items here may shift based on customer feedback
> and what we learn in production. Subscribe to GitHub Releases to be notified.

---

## v225 (Now ✅)

**Status**: Paper-trading, ready for design partner demos.

What's in:
- ✅ Real-time news ingestion (L1)
- ✅ LLM analysis via OpenRouter (L2) — Kimi K3, GPT-4, Claude, DeepSeek
- ✅ 27-layer reflection across 7 categories (L3)
- ✅ Meta-loop with daily cron (L3.5)
- ✅ Knowledge graph with versioning (L4)
- ✅ Next.js dashboard, Feishu alerts, Excel export (L5)
- ✅ 43 pytest cases, CI-ready

What's missing:
- ⚠️ Live trading wired (testnet on Binance is the next step)
- ⚠️ Multi-user / team dashboard
- ⚠️ Cloud-hosted option (still local-only)
- ⚠️ Mobile UI

---

## v230 (Next 30 days) — Live testnet

**Goal**: First live track record.

- ☐ Binance testnet adapter for live order flow
- ☐ Position management with safety brackets
- ☐ Latency-aware LLM routing (cache repeated tickers)
- ☐ First **public case study** from live testnet

**Exit criterion**: 30-day live track record, sharpe reported publicly.

---

## v235 (60 days) — First paying pilot

**Goal**: Validate the $499/mo self-hosted tier.

- ☐ License-key issuance (commercial license path)
- ☐ Onboarding flow (Stripe checkout → key delivery → first run)
- ☐ 3-channel support (email / Discord / GitHub issues)
- ☐ Per-customer reflection log (so they can see their own history)
- ☐ First public **customer testimonial**

**Exit criterion**: 1 paying customer at $5k MRR, testimonial in writing.

---

## v240 (90 days) — Series Seed prep

**Goal**: Build the funnel for Series Seed.

- ☐ 3 paying customers
- ☐ $20k MRR
- ☐ GitHub 100+ stars
- ☐ Discord 100+ members
- ☐ 1 published technical paper or blog post on the meta-loop

**Exit criterion**: Updated pitch deck + product-market fit signal.

---

## v250 (6 months) — Series Seed

**Goal**: Raise $1.5M Seed at $10M post.

- ☐ $50k MRR
- ☐ 10 customers
- ☐ Expand team (engineer #2, ops #1)
- ☐ Launch B2B SaaS tier (managed hosting)
- ☐ 3 published case studies

---

## v300 (12 months) — Series A

**Goal**: $1.5M ARR, ready for $20M Series A.

- ☐ $1.5M ARR
- ☐ 50 customers
- ☐ 5-figure self-reflection cycles accumulated
- ☐ OSS contributor community 100+
- ☐ Patent filed on meta-loop

---

## ❓ Open questions

We don't yet know the right answer to:

- **How many checker layers is enough?** Currently 7. We may go to 12.
- **Should the meta-loop auto-write checkers?** Currently: no. Future: maybe,
  with strong human approval gates.
- **Self-hosted vs SaaS split?** Currently: self-hosted focus. SaaS may be
  50% of revenue by v300.
- **Cross-asset layer expansion?** Currently 3 checkers; FX options / credit
  / commodities are unexplored.

---

## 💬 How to influence the roadmap

- **Customers** at $499+/mo tier get a roadmap vote (1 vote per desk)
- **OSS contributors** with merged PRs get an advisory seat at roadmap
  discussions (no formal vote, but heard)
- **GitHub Discussions** is the public forum — open a thread

---

<sub>Last updated: 2026-10-06 — this doc is reviewed monthly</sub>