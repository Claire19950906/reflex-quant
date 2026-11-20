<div align="center">

# Cut the Trades You Shouldn't Take

### Raise your decision accuracy. Raise your odds.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11–3.14-green)](./backend/requirements.txt)
[![Stage](https://img.shields.io/badge/stage-paper%20only-orange)](#boundaries-up-front)

**English** | [中文](./README.md)

</div>

It speaks twice around every order.

**Before the order** — what the situation actually is, whether this order belongs, and why.
**After the order** — if it went wrong, where.
**Next order** — last order's lesson becomes this order's gate.

---

## 1. Before the order: the situation, not a call

Not a buy/sell signal. A picture you can read:

- **Market state** — how stale the quote is, how wide the volatility, whether this symbol is doing something abnormal today.
- **Account state** — does the ledger reconcile, is this a duplicate run, how much leverage is used, what the worst case is if this order fills.
- **Evidence** — how many hard facts support it, how many contradict it, how wide the disagreement is.

All three must pass before a signal is emitted. One missing → score cut. Two missing → no signal. **The ones it refuses to emit are its "don't take this trade."**

Real output looks like this:

> **BTC** — spot ETF net outflow USD 3.713 bn over 20 days (6th percentile), on-chain active addresses still expanding.
> Money leaving, usage growing, dispersion 0.64 → **no direction**. Suggestion: no add, wait for flows to turn.

---

## 2. After the order: where it went wrong

Every loss is filed under one of 33 error codes, each with an executable fix — not "bad psychology".

> **2026-11-19 · BTC long −2.1%**
> Cause: E002 stale quote — decided on the 18:40 print, filled after 19:26
> Missed: the 19:00 volume breakdown was not detected
> Next time: reject any order whose quote is older than 5 minutes

Winners are booked the same way — **what worked must be repeatable, what failed must be blocked.**

---

## 3. Next order: the lesson becomes a gate

Attribution is not an archive. The codes are tiered:

- `hard` (15) → enters the gate; the same setup is rejected next time;
- `soft` (8) → warning only;
- `research` (16) → needs statistical validation, not trusted on small samples.

Accuracy does not come from "I'll be careful next time". It comes from **last mistake being physically unable to pass this time.**

---

## 4. Verify it yourself

```bash
cd backend && python3 scripts/iteration_check.py    # 40s, three lines
python3 scripts/offline_selftest.py                  # 30s, never touches the production DB
```

![live signal desk](.shots/home_equity3.png)

---

## Boundaries, up front

- **Paper + MockAdapter. No real money. It never places an order for you.**
- Win rate and edge are reported as `NULL` — below the sample threshold, no number is printed.
- **No return promises.** Only this: lose less of what you were going to lose anyway, and let probability do the rest.

---

## Quick start

```bash
cp backend/.env.example backend/.env      # your keys; every field may stay empty (degrades gracefully)
cd backend && pip install -r requirements.txt
python3 src_python/api_server.py          # :8000

cd frontend && npm install && npm run dev # :3000
```

Or `docker compose up`. Zero-cost deployment: [`DEPLOY.md`](./DEPLOY.md).

---

## Dig deeper

| Looking for | Where |
|---|---|
| Technical detail, measured tables, known gaps | [`docs/README-详细版.md`](./docs/README-详细版.md) |
| Changes | [`CHANGELOG.md`](./CHANGELOG.md) |
| Deploy / security / contributing | [`DEPLOY.md`](./DEPLOY.md) · [`SECURITY.md`](./SECURITY.md) · [`CONTRIBUTING.md`](./CONTRIBUTING.md) |
