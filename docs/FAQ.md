# FAQ — Frequently asked questions

---

## For investors

### Why not just raise a quant fund?

We considered it. A fund requires:
- 2 years of track record
- AUM ≥ $10M (typically)
- Compliance + audit overhead
- Investor trust: most LPs won't touch a 0-track-record fund

A **B2B tool** lets us:
- Generate revenue from day 1
- Serve thousands of desks (vs. one fund's capital)
- Keep the product IP clean
- Build a moat (reflection cycles + KG) that funds can't replicate

---

### What's your moat? Anyone can wrap an LLM in a trading UI.

Three things:
1. **27-layer reflection engine** — months of accumulated checker logic,
   each tied to real failures.
2. **Meta-loop** — the system proposes its own improvements. Not yet in
   any open-source project.
3. **Reflection cycles data** — months of real LLM reasoning, real
   outcomes, real errors. This data is the moat. A clone starts at zero.

Code is open. **Data isn't.**

---

### Why open-source at all? Won't AWS / Citadel clone you?

Three reasons:
1. **OSS = growth in quantum engineering talent.** OSS contributors → hires,
   potential acquihires, community goodwill.
2. **The reflection cycles stay local.** Cloning the code without the
   data = a shell, not a tool.
4. **AGPL-3.0 prevents closed-source SaaS forks.** Anyone trying to clone
   and SaaS-ify us must publish their source. This is a feature.

---

### What's your burn rate? When will you need Series A?

v225 → v250 (6 months) — burn is $15k/mo (1 founder + part-time help).
Fundraising target: $1.5M Seed, 18-month runway.
Series A trigger: $1.5M ARR (currently $0 ARR, paper trading).

---

## For potential customers

### We're a $20M-AUM family office. Can we use this?

Yes — design-partner tier. Email team@reflex-quant.ai.

We're currently piloting with 2 quant funds + 1 family office. Pricing for
the design partner program is **$499/mo self-hosted** with a 30-day
discount on the first invoice.

---

### Does it work on / not on macOS / Windows / Linux?

- ✅ **macOS**: fully supported (developed here)
- ✅ **Linux**: fully supported (Ubuntu 22.04+ verified)
- ✅ **Windows 11**: supported via WSL2

The frontend dashboard is cross-platform (Next.js). Backend is pure Python.

---

### What exchanges do you support for live trading?

v225: paper trading only
v230 (next 30 days): Binance testnet
v235 (60 days): Binance, OKX testnet
v240 (90 days): Interactive Brokers paper trading
v250+: real-money on testnet-verified exchanges (each exchange negotiated
separately; not all will be — we can't decide).

---

### How much does it cost?

| Tier | Price |
|---|---|
| **Free** (AGPL-3.0 self-hosted) | $0 — for individual quant / academic |
| **Pink** (commercial self-hosted) | $499 / desk / mo |
| **Growth** (multi-desk) | $25k / yr / company |
| **Scale** (white-label) | $100k / yr + revenue share |

See [../LICENSE-COMMERCIAL.md](../LICENSE-COMMERCIAL.md) for full terms.

---

### Do you see my trade data?

**No.** Reflex Quant runs locally. Your trades, reflection cycles, decision
errors, and KG nodes are stored in `~/.reflex-reflex/data.db` (or wherever
you configure). Nothing leaves your machine unless you explicitly turn on
remote features (none currently exist).

We have **zero telemetry**. We have **zero tracking**. We have **zero data
collection**. Verify by reading the source.

---

### What's your track record?

We don't have a live track record — we're at paper trading (v225). What we
have:

- **59 paper trades** in 7 days (paper-trading window for v225)
- **52.5% win rate** (above random)
- **27 error codes** caught and remediated
- **12 meta-loop proposals** (3 implemented, 9 under review)

The reflection engine is **provably useful** (it caught 73 errors in 59
trades), but we have not yet validated it on live capital.

---

### What happens if I stop paying?

Your local installation keeps working (AGPL-3.0 is forever). You lose:
- Updates after the cutoff date
- Support tickets
- New checker proposals (E028+)
- Commercial license to embed in your product

We will not remotely disable it. We can't. Your machine, your code.

---

## For developers

### How do I run it?

This is the **public showcase repo**. The full source is currently in our
**private repo** under design-partner agreement. To get access:

- Email team@reflex-quant.ai
- Sign an NDA
- Receive private-repo access (read-only)
- Optional: contribute under AGPL-3.0 to public repo

Or wait for v230, when we plan to open the reflection engine module to
the public repo.

---

### What's the tech stack?

See [ARCHITECTURE.md](./ARCHITECTURE.md). Short version:

- Python 3.11+
- SQLite (WAL mode)
- Next.js 14 + TypeScript
- TailwindCSS
- crontab / supervisord

No Kubernetes, no Redis, no Kafka, no Postgres, no ML framework bloat.

---

### How can I add a reflection checker (E028+)?

We love this. See [CONTRIBUTING.md](../CONTRIBUTING.md).

Process:
1. Open an issue with tag `checker-proposal`
2. Describe the real trade case + which layer
4. We collaborate on the design
5. Your checker ships as E028

---

### How do you handle LLM rate limits?

OpenRouter abstracts this. Set `MAX_TPM` in `.env`. Default: 1 request/sec.
If you exceed, we queue + retry with exponential backoff.

For production: rate limit + budget cap (USD/day).

---

## For partners

### We're a brokerage. Can we white-label?

Yes. Scale tier ($100k/yr + revenue share). Includes:
- White-label UI
- Co-branded onboarding
- Joint customer success
- Dedicated engineer
- Roadmap influence

Email: team@reflex-quant.ai.

---

### We're a quant research firm. Can we partner?

Yes — research partnership tier:
- We give you free self-hosted license
- You give us 1 published case study per quarter
- Joint technical blog post every 6 months
- Co-marketing on landing page

Email: team@reflex-quant.ai.

---

<sub>Last updated: 2026-10-06</sub>