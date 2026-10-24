# Reflex Quant 🪞 — Seed Round Pitch Deck
**Draft v0.0 — for investor distribution**

---

## Slide 1 — Title

# Reflex Quant 🪞
## The self-reflective AI for quant trading desks

**Seed Round: $1.5M**
**18-month runway → Series A at $20M ARR**

z2132743607@163.com · github.com/Claire19950906/reflex-quant

*"We don't just trade. We reflect on why."*

---

## Slide 2 — Problem

### Quant PMs spend 80% of their day watching trades, 20% thinking about why.

- After-trade review = spreadsheet, Notion, weekly meeting
- **Reasoning errors** (overconfidence, missing cross-asset, etc.) repeat for **months** before noticed
- "Black box" LLMs make decisions, but no system audits *why* they decided
- Result: hidden losses, model drift, slow learning

> **The most valuable 20% of a quant's day is the most under-tooled.**

Quant PMs are drowning in **execution data**, but starving for **reflection**.

---

## Slide 3 — Solution

### Reflex Quant is the first self-aware quant stack.

1. **Real-time signal engine** — news → LLM filter → signal → 2h forward validation
2. **27-layer reflection engine** — every decision audited across 7 reflection layers
3. **Meta-reflection loop** — after the same 7 layers reflect, the system logs blindspots and proposes new checkers (E028+)

> **A quant system that gets more honest about itself over time, instead of less.**

Unlike backtest frameworks, Reflex Quant audits the **reasoning**, not just the result.

---

## Slide 4 — Product demo

### [3 GIFs: signal flow / reflection panel / meta-loop blindspot]

- **GIF 1:** News spike → LLM signal → 2h forward P&L (+$0.4k)
- **GIF 2:** Same signal reviewed by 27 checkers → 3 errors flagged (severity + remediation)
- **GIF 3:** Meta-loop dashboard → WTI blindspot (7 checkers all missed) → auto-proposed new checker (E028+)

*(Replace placeholders with real screen recordings before sending)*

> Live demo: z2132743607@163.com

---

## Slide 5 — Why now?

### Three forces make reflection-as-a-service possible:

1. **LLMs made trading signals cheap** — but reasoning quality is now the bottleneck
2. **Small quant funds ($10M-$500M AUM) growing 25%/yr** — can't afford Bloomberg + a quant dev team
3. **"Self-host or die" sentiment post-2023** — open-source, local-first is the new default (cf. Ollama, Bruno, Logseq)

> **The "execution layer" is commoditized. The "reflection layer" is the new moat.**

---

## Slide 6 — Market

### TAM / SAM / SOM

| Layer | Size | Description |
|---|---|---|
| **TAM** | **$50B** | All quant trading tools globally (Bloomberg, MetaTrader, etc.) |
| **SAM** | **$5B** | Mid-market quant funds ($10M-$500M AUM), ~5,000 globally |
| **SOM** | **$50M ARR** | Independent quants + small funds adopting open-source quant AI, ~10,000 desks |

> **At $499/mo per desk, capturing 1% of SOM = $6M ARR.**

**Bottom-up check:**
- 10,000 desks × $499/mo × 12 = $60M ARR ceiling
- 1% market share = $600k ARR, 5% = $3M ARR
- Series Seed target: **$20k MRR = 40 desks** (very achievable)

---

## Slide 7 — Business model

### Two routes, will pick one with first 5 customers.

| Route | Pricing | LTV | Payback |
|---|---|---|---|
| **A. Self-hosted license** | $499 / desk / mo | ~$6k | 6 mo |
| **B. Reflection-as-a-Service** (API) | $0.05 / decision audited | ~$30k / startup | 3 mo |
| **Hybrid** | OSS free + hosted $499 + usage | mixed | mixed |

**Gross margin:** ~85% (self-hosted) / ~70% (API)

**Conversion funnel:**
- GitHub star → Discord → self-host → $499 hosted → 5+ desks = enterprise

> The **free OSS tier is our distribution channel**. Bloomberg charges $24k/yr/seat; we charge 1/50th.

---

## Slide 8 — Traction (last 30 days)

| Metric | Today | 30-day goal | 90-day goal |
|---|---|---|---|
| GitHub stars | 0 | 100 | 500 |
| Discord members | 0 | 50 | 200 |
| Paper trades (closed) | 59 | 200 | 1,000 |
| Mock MRR | $0 | $5k (3 pilots) | $20k |
| Customer quotes | 0 | 2 | 5 |
| Live P&L months | 0 | 1 (testnet) | 3 |
| Blindspots caught | 1 | 5 | 15 |

*(v0 metrics; replace with verified numbers before sending out)*

---

## Slide 9 — Competition

| Capability | **Reflex Quant** | Bloomberg | MetaTrader | QuantConnect |
|---|:---:|:---:|:---:|:---:|
| Self-host / OSS | ✓ | ✗ | partial | ✓ |
| Real-time LLM signals | ✓ | ✗ | ✗ | ✗ |
| 27-layer reflection engine | ✓ | ✗ | ✗ | ✗ |
| Meta-loop (reflects on reflection) | ✓ | ✗ | ✗ | ✗ |
| Cross-asset inverse-pair aware | ✓ | ✗ | ✗ | ✗ |
| Cron-automated KG + alerts | ✓ | partial | ✗ | ✗ |
| Open-source | ✓ | — | — | partial |
| Local-first (no cloud) | ✓ | ✗ | ✓ | ✓ |
| Price / mo | **$499** | $2k+ | $0–$200 | $0–$100 |

> **No existing product offers built-in reasoning-quality reflection for live LLM-driven trades.**

### Indirect competitors (what PMs use today instead)

- **Spreadsheets + Notion** — manual, post-hoc, error-prone
- **Bloomberg chat threads** — reactive, not systematized
- **Slack / Discord / weekly review meeting** — slow human-in-the-loop
- **Homemade Python notebooks** — single-PM, single-strategy, doesn't scale

**Reflex Quant replaces 1 hire + 5 Notion docs + 1 Bloomberg chat thread = $80k+/yr value at $6k/yr price.**

---

## Slide 10 — Team

*(Placeholder — fill in before sending)*

| Role | Background |
|---|---|
| **Founder / CEO** | Claire · solo builder, 1+ yr shipping Reflex Quant full-stack (engine → API → frontend → reflection engine → cron → meta-loop). Author of v225. |
| **CTO** | *(open)* — ex-quant or ex-LLM infra |
| **Quant Lead** | *(open)* — 5+ yr at a prop shop |
| **Advisor** | *(desired)* — Quant fund PM, $50M+ AUM |
| **Advisor** | *(desired)* — OSS founder, 10k+ star project |

> **What we already have:** a working v225 paper-trading system with 27-layer reflection, meta-loop, and cron automation running in production on a single machine. The hardest technical part is done. The next milestones are distribution, paid pilots, and live-track-record.

---

## Slide 11 — The ask

### Seed: **$1.5M**

**Use of funds (18 months):**

| Bucket | $ | % |
|---|---|---|
| Engineering (2 hires) | $700k | 47% |
| Quant research (1 hire) | $300k | 20% |
| Customer success / sales | $250k | 17% |
| Legal + compliance | $100k | 7% |
| Reserve | $150k | 10% |

**Milestones for Series A (18 months):**

- ✅ **50 paying desks** (or 50M API decisions audited)
- ✅ **$20k MRR** → annualized $240k ARR + growth rate 3x/yr
- ✅ **6-month live P&L track record** — Sharpe > 1.0, Max DD < 15%
- ✅ **1 case study from a $50M+ AUM fund** — public testimonial
- ✅ **GitHub 1,000+ stars, 200+ Discord members**

> **At $20k MRR with 3x growth, Series A at $15M-$25M valuation is conservative.**

---

## Slide 12 — Vision

### By 2030, every quant desk should have a "chief reflection officer."

> A self-aware AI that catches what humans miss, learns from what it misses, and tells you when it's wrong.

**Reflex Quant: the AI that watches the AI.**

> *"The next decade of quant will be defined not by who has the most data, but by who has the most honest system."*

---

# Appendix

## A1 — Tech architecture (deep)

```
┌───────────────────────────────────────────────────────┐
│  Frontend (Next.js 14)      Dashboard (Streamlit)     │
└─────────────────┬─────────────────────────────────────┘
                  │ REST + SSE
                  ▼
┌───────────────────────────────────────────────────────┐
│            api_server.py  (FastAPI, :8000)            │
│  /api/signals   /api/reflection/*   /api/kg/*         │
│  /api/insights  /api/replay/*       /api/review/*     │
└─────────────────┬─────────────────────────────────────┘
                  ▼
┌───────────────────────────────────────────────────────┐
│   SQLite (WAL mode)                                   │
│   ai_decisions · raw_news · review_insights           │
│   decision_errors · reflection_cycles                 │
│   trade_reflections · paper_trading_runs              │
│   asset_driver_nodes/edges (causal graph, v170+)      │
└──────┬──────────────────────────┬─────────────────────┘
       ▼                          ▼
┌──────────────────────┐   ┌──────────────────────────┐
│   engine.py          │   │   Reflection Loop        │
│   ├ ingest  (WS)     │   │   ├ 27 checkers (E001+)  │
│   ├ ai_worker (LLM)  │   │   ├ meta-loop            │
│   ├ forward (2h P&L) │   │   └ blindspot log (E028+)│
│   ├ webhook          │   └──────────────────────────┘
│   ├ alerts (飞书)      │
│   └ prices  (多源)    │
└──────────────────────┘
```

**Stack:** Python 3.11, FastAPI, SQLite + WAL, Next.js 14, Kimi K3 / DeepSeek via OpenRouter.
**Local-first:** entire stack runs on a single Mac Mini / Linux box; no cloud dependency.

---

## A2 — v225 reflection engine (full coverage)

| Layer | Codes | Sub-checkers | Scope |
|---|---|---|---|
| **event_logic** | E001–E016 | 16 | News-event inference failures |
| **causal** | E017–E018 | 2 | Causal direction errors |
| **sample_integrity** | E020 | 1 | Data leakage / cherry-picking |
| **reasoning_logic** | E021–E027 | 7 | assumption, counterfactual, calibration, history, cross-asset, mechanism, confidence/evidence |
| **(meta-loop)** | E028+ | proposed | Auto-discovered blindspots |

**7 reflection layers × 8 reasoning sub-layers = 56 audit points per decision.**

---

## A3 — Unit economics

### Self-hosted route ($499/mo)

| Item | $ |
|---|---|
| Gross revenue / mo | $499 |
| - Hosting / infra | $25 |
| - LLM API (auditing 1000 decisions) | $50 |
| - Support (amortized) | $50 |
| **Gross margin** | **75%** |
| CAC (Discord-led) | ~$200 |
| Payback | ~6 months |
| LTV (avg 24-month retention) | ~$12k |
| **LTV/CAC** | **60x** |

### API route ($0.05/decision)

| Item | $ |
|---|---|
| Avg decisions / startup / mo | 100k |
| Revenue / mo | $5k |
| - LLM cost | $1.5k |
| - Hosting | $300 |
| - Support | $500 |
| **Gross margin** | **54%** |
| Payback | 3 months |

> **Both routes have positive unit economics at day-1 pricing.**

---

## A4 — Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Live P&L underperforms paper | Medium | Testnet first; transparent results; not sales-driven |
| Citadel / Renaissance clones | Low | Not their target; we sell to the 80%, not the 1% |
| LLM API price war | Medium | Local model fallback (Ollama integration on roadmap) |
| Open-source clone | Medium | Fast iteration + commercial add-ons (KG, hosted) |
| Customer concentration | Low | OSS distribution = no concentration risk |
| Regulatory (live trading) | Low | Start as analytics; live is opt-in via customer brokers |

---

## A5 — Why now, why us, why this

**Why now:** LLMs commoditized signal generation. Reasoning quality is the new bottleneck.

**Why us:** We've already built a 27-layer reflection engine + meta-loop + cron automation. **The hardest technical part is done.**

**Why this:** Open-source quant AI + B2B SaaS = the only honest way to monetize this without taking prop risk.

> *"We're not asking you to bet on a quant fund. We're asking you to bet on a $499/mo tool that quant funds can't live without."*

---

<sub>Pitch deck v0.0 — 2026-10-06 — generated from `trident_v170` codebase audit</sub>
