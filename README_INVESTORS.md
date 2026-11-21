<div align="center">

# Reflex Quant 🪞
### *The self-reflective AI for quant trading desks.*

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](#-license)
[![Status](https://img.shields.io/badge/status-paper%20trading-orange)](#-traction-v225)
[![Paper trades](https://img.shields.io/badge/paper%20trades-3%20(1%20settled)-lightgrey)](#-honest-status-2026-11-16)
[![Error codes](https://img.shields.io/badge/error%20codes-E001–E033-blueviolet)](#-reflection-engine)
[![Reflection layers](https://img.shields.io/badge/reflection%20layers-7-yellowgreen)](#-v225-reflection-engine)
[![Tests](https://img.shields.io/badge/tests-3%2C596%20offline-success)](#-honest-status-2026-11-16)
[![Stars](https://img.shields.io/github/stars/stardust888888/reflex-quant?style=social)](https://github.com/stardust888888/reflex-quant)
[![Forks](https://img.shields.io/github/forks/stardust888888/reflex-quant?style=social)](https://github.com/stardust888888/reflex-quant/fork)

> **One-line pitch:** Reflex Quant is a self-aware quant AI that not only trades,
> but reflects on every decision — catching **33 classes of reasoning errors** no
> human PM has time to spot.

[**Why Reflex Quant**](#-why-reflex-quant) · [**Architecture**](#-architecture) · [**Honest status**](#-honest-status-2026-11-16) · [**Quick Start**](#-quick-start) · [**Pitch Deck**](./PITCH_DECK.md) · [**中文 README**](./README.md)

</div>

---

## ⭐ Star us if this resonates

> *Most quant systems are confident machines. We're building a self-aware one.*
>
> If you believe the next decade of quant will be defined by **reasoning quality**, not raw data, hit ⭐ — it costs nothing and tells the algo we should be on your feed.
>
> Every star = one more independent quant who sees the problem. That's our distribution channel.

---

---

## 🎯 The problem

Every quant desk has the same hidden leak:

> *PMs spend 80% of their day **watching** trades, and 20% **thinking** about why trades work or fail.*

Tools like Bloomberg, MetaTrader, or QuantConnect give you **execution and backtest**.
None of them tell you:

- *Why did the WTI short last Tuesday lose money?*
- *Is the LLM consistently skipping the cross-asset check?*
- *Are you over-confident on low-evidence trades?*

The "reflection" step — the most valuable 20% — is done in heads, in Notion pages,
in weekly meetings, and **in retrospect, after the damage is done.**

---

## 💡 Our solution

Reflex Quant is an **open-source quant trading stack with a built-in self-reflection engine.**

Three pieces, end-to-end:

1. **Real-time signal engine** — news → LLM filter → signal → 2-hour forward validation → WIN/LOSS settle.
2. **33-code error checker** — every signal is audited across **7 reflection layers**. Each error code (E001–E033) has explicit remediation guidance.
3. **Meta-reflection loop** — the system **reflects on its reflections**. When all 7 reasoning-logic checkers miss a bad trade, Reflex logs the *blindspot of the blindspot* and proposes a new checker (E028+).

> The moat: a quant system that gets **more honest about itself over time**, instead of less.

---
## 🪞 Why Reflex Quant

Most quant systems are *confident machines*: bigger models, more data, more leverage.

Reflex Quant is a *self-aware machine*. We bet on three structural advantages:

| Capability | Reflex Quant | Bloomberg | MetaTrader | QuantConnect |
|---|:---:|:---:|:---:|:---:|
| Self-host / OSS | ✓ | ✗ | partial | ✓ |
| Real-time LLM signals | ✓ | ✗ | ✗ | ✗ |
| 27-layer reflection | ✓ | ✗ | ✗ | ✗ |
| Meta-loop (reflects on reflection) | ✓ | ✗ | ✗ | ✗ |
| Cross-asset inverse-pair aware | ✓ | ✗ | ✗ | ✗ |
| Cron-automated KG + alerts | ✓ | partial | ✗ | ✗ |
| Open-source | ✓ | — | — | partial |
| Local-first (no cloud) | ✓ | ✗ | ✓ | ✓ |
| Price / mo | **$499** | $2k+ | $0–$200 | $0–$100 |

> **No existing product offers built-in reasoning-quality reflection for live LLM-driven trades.**

---

## 🏗 Architecture (TL;DR)

```
News (FinancialJuice WS / TreeNews) ─┐
                                     ▼
        L1 keyword + L2 LLM filter (realtime_filter.py)
                                     ▼
                       raw_news  (SQLite + WAL)
                                     ▼
              LLM analysis (Kimi K3 via OpenRouter)
                                     ▼
                       ai_decisions  (signals)
                                     ▼
              2h forward validation  (forward.py)
                                     ▼
              ┌──────────────────────────────────────────┐
              │  33-code reflection engine               │
              │   (reflection_checkers.py)               │
              │   → decision_errors   (E001–E033)        │
              │   → review_insights   (KG nodes)         │
              │   → reflection_cycles (meta-loop)        │
              │   → blindspot log    (META_REFLECTION.md)│
              └──────────────────────────────────────────┘
                                     ▼
                Next.js frontend + Feishu alerts + cron L3 automation
```

The **bottom row is what doesn't exist anywhere else in the quant world** — a self-aware system that writes its own improvement log.

---

## 📊 Honest status (2026-11-16)

> 这一节刻意只写**能被库里一行 SQL 复核**的数字。上一版写的是 "59 paper trades
> in 7 days / 43 pytest cases"，那是 v225（10 月初）的另一个库，留着只会让第一个
> 尽职调查问题就把整份材料作废。

| 指标 | 实测值 | 复核方式 |
|---|---|---|
| 模拟成交（累计） | **2 笔**（1 已结算，截至 2026-11-16 22:41） | `SELECT COUNT(*) FROM quick_sim_trades` |
| 其中已结算 | **1 笔**（**0 胜 1 负**，累计 -1.21 USDT，均值 -1.21%/笔） | `verdict` / `pnl_pct` |
| 其中废弃 | 0 笔 | `verdict='ABANDONED'` |
| 有效样本 | **1**（< 60 门槛） | 见 `backend/reports/funnel/` |
| 决策产出 | 478 条/日，HOLD 99.4%，降级 89% | `ai_decisions` 当日 |
| 反思样本 | 605 条，其中推理类约 100 条 | `trade_reflections_v2` + `sample_class` |
| 离线用例 | **3,596 条**（`--collect-only` 实测） | `python -m pytest --collect-only` |
| 错误码 | **E001–E033** | `grep -rho 'E0[0-9]{2}' backend/src_python` |

**已具备的技术事实（可验证）**

- 33 个错误码跨 7 层反思，每条带可执行修复建议；8 个推理子层（assumption /
  counterfactual / calibration / history / cross_asset / mechanism / confidence）。
  **分层台账 `error_code_ledger`：hard 15 / soft 8 / research 16** —— soft 是文本模板
  合规检查，research 待验证，只有 hard 参与闸门统计（避免"码的数量"被当成"码的有效性"）
- 跨品种反向对识别（不会把 "XAU ↑ + DXY ↓" 误判为矛盾）
- 元反思循环：每日 03:47 定时；准入为「≥5 笔已结算独立成交」，样本不足时明确报不足
- 数据真实性守门（`sample_quality_filter` R1–R11 + `data_authenticity` S1–S7）
- 每日漏斗/健康/周报自动落盘（`backend/reports/{funnel,health,weekly}`）

**尚未具备（必须说清）**

- **没有可验证的交易边际**：有效样本 1 笔，任何胜率都不成立
- 反思语料 82% 是行情断流（基础设施噪音），推理类命中率尚无统计意义
- 决策侧 99.4% HOLD，原因是上游降级（target_none / llm_empty 等），不是策略保守

---
## 🚀 Quick start

```bash
git clone https://github.com/stardust888888/reflex-quant.git
cd reflex-quant
cp .env.example backend/.env       # fill in OPENROUTER_API_KEY
cd backend && pip install -r requirements.txt
python engine.py                   # terminal 1: news → LLM → signals
uvicorn api_server:app --host 127.0.0.1 --port 8000   # terminal 2: API
cd ../frontend && npm ci && npm run dev              # terminal 3: UI
```

Open <http://localhost:3000> — live feed, reflection panel, meta-loop dashboard.

Full guide: [docs/INSTALL.md](./docs/QUICKSTART.md). Windows one-click: `scripts\dev_start.bat`.

---

## 🧠 What you actually see

| Panel | What it shows |
|---|---|
| **Signals feed** | Live BUY/SELL/HOLD with reasoning excerpts |
| **Reflection panel** | Each signal reviewed by 33 checkers; errors highlighted with severity + remediation |
| **Meta-loop dashboard** | Cycles over time, blindspots caught, proposed new checkers |
| **Knowledge graph** | KG nodes auto-grown from confirmed insights |
| **Excel export** | 5-sheet XLSX with frozen headers + auto-filter |

---

## 🛣 Roadmap

| Phase | Version | Time | Goal |
|---|---|---|---|
| **Now** | v225 | ✅ | Paper trading, 27-layer reflection, meta-loop |
| **Next 30 days** | v230 | days 0-30 | Live-trading testnet (Binance), 5-month live track record |
| **60 days** | v235 | days 30-60 | 1st paying pilot (quant fund, $5k MRR), public case study |
| **90 days** | v240 | days 60-90 | $20k MRR, 3 customers, GitHub 100+ stars |
| **6 months** | v250 | day 180 | $50k MRR, 10 customers, Series Seed deck |
| **12 months** | v300 | day 365 | AUM > $5M or 50M API decisions audited, Series A |

---

## 💼 Business model (provisional)

We are evaluating two paths. **Will pick one with the first 5 customers.**

| Route | Who pays | Pricing | Gross margin |
|---|---|---|---|
| **A. Self-hosted license** | Independent quants, family offices | $499 / desk / mo | ~85% |
| **B. Reflection-as-a-Service** (API) | Other quant startups | $0.05 / decision audited | ~70% |
| **Hybrid** | OSS community + paying tiers | Free / $499 / usage | mixed |

Target payback: 6 months. Target LTV: $6k–$30k per desk.

---
## 🧬 What makes us different (one-liners)

- **The reflection log is open-source.** Other systems have logs; we have a *self-improvement diary*.
- **Tools expose uncertainty, not decide for you.** We refuse to make the AI black-box.
- **Cron-automated, not "AI assistant" wannabe.** The system *runs* itself; you don't chat with it.
- **Built for the 80%, not the 1%.** Citadel doesn't need this. Independent quant with $5M AUM does.

---

## 👥 Team

*(fill in — placeholder)*

| | Background |
|---|---|
| **Founder / CEO** | [Name], [background: e.g. 6 yr crypto quant, ex-…] |
| **CTO** | [Name], [background] |
| **Quant Lead** | [Name], [background] |
| **Advisor** | [Quant fund PM] |
| **Advisor** | [OSS founder] |

---

## 📬 Get in touch

- **Email (founder):** research@example.com
- **Issues / feature requests:** [github.com/stardust888888/reflex-quant/issues](https://github.com/stardust888888/reflex-quant/issues)
- **Pitch deck:** [PITCH_DECK.md](./PITCH_DECK.md)
- **Discussion:** please open a GitHub Discussion or issue — we read everything

> *We're a one-person operation right now. Email goes straight to the founder. No Calendly, no SDR — if you want to talk, just write what you're working on and why Reflex Quant matters to you.*

---

## For Developers (technical appendix)

The technical README — env vars, deployment, schema migrations, testing — is
intentionally kept separate. See [backend/README.md](./backend/README.md) and
[deploy/README.md](./deploy/README.md) for the developer-grade docs.

---

<sub>v225 — 2026-10-06 — built with the assumption that the next decade of
quant will be defined not by who has the most data, but by who has the most
honest system.</sub>

---

## 📄 License

MIT — see [`LICENSE`](./LICENSE). Use it, fork it, ship your own reflection engine on top of it. We'd appreciate a ⭐ and a PR back if you fix a bug.
