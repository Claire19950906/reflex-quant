# Architecture — Design Principles

> This document explains *how* Reflex Quant is structured.
> For the *why*, see [PHILOSOPHY.md](./PHILOSOPHY.md).
> For *specific case outcomes*, see [CASE_STUDIES.md](./CASE_STUDIES.md).

---

## Layered overview

Reflex Quant has **five layers**. Each layer has a single responsibility and
a clear interface to the next.

```
┌──────────────────────────────────────────────────────────┐
│  L1: News Ingestion    (real-time, multi-source)        │
├──────────────────────────────────────────────────────────┤
│  L2: LLM Analysis      (reasoning + signal generation)  │
├──────────────────────────────────────────────────────────┤
│  L3: Reflection        (27 checkers, 7 layers)          │
│       └─ Meta-loop     (reflects on reflection)          │
├──────────────────────────────────────────────────────────┤
│  L4: Knowledge Graph   (auto-grown from confirmed cases)│
├──────────────────────────────────────────────────────────┤
│  L5: Output / UX       (UI, alerts, exports)            │
└──────────────────────────────────────────────────────────┘
```

Each layer can be replaced independently. The LLM model in L2 is swappable
without touching L3. The reflection checkers in L3 are versioned (E001–E027)
and can be added to without rewriting the engine.

---

## L1: News Ingestion

**Responsibility**: Get news items into the system quickly and cleanly.

**Sources**:
- WebSocket feeds (FinnalJS, Polygon.io, etc.) — primary real-time
- Polling APIs (TreeNews, NewsAPI) — every 60s
- Cron-driven RAG pulls from academic / regulatory feeds — daily

**Design principles**:
- **De-dup by content hash, not timestamp.** Same news from two sources
  is one entry, not two.
- **Persist raw text, not parsed JSON.** When the parser is wrong, you want
  the original.
- **WAL + journal_mode=SYNC=NORMAL.** Survives crash without corrupting DB.

**Data shape** (`raw_news` table):
- `id`, `source`, `received_at`, `title`, `body`, `hash`, `lang`,
  `tickers_mentioned[]`, `sentiment_score`

---

## L2: LLM Analysis

**Responsibility**: Turn news into trade signals with **fully recorded**
reasoning.

**Approach**:
- Two-pass: L1 keyword filter → L2 LLM filter → LLM reasoning
- Reasoning is logged **verbatim** (the entire response text)
- Every signal has: `reasoning_text`, `confidence`, `evidence_grade`
  (L1/L2/L3), `sources_used[]`

**Design principles**:
- **Reasoning is a first-class artifact.** It is not derived; it is
  stored.
- **Evidence is graded, not guessed.** L1 = headline only; L2 = headline +
  1 source; L3 = headline + multiple sources + cross-checked.
- **Confidence is bounded.** We don't accept "98% confident" from the LLM
  on L1 evidence. Confidence gets capped by evidence_grade.---

## L3: Reflection Engine

**Responsibility**: Catch reasoning errors **before they become losses**;
record every caught error for later analysis.

**27 checkers across 7 layers**:

| Layer | Checkers | Catches |
|---|---|---|
| `event_logic` | E001–E004 | Wrong entity, wrong event, mis-extracted claim |
| `causal` | E005–E008 | Insufficient causal chain, single-hop reasoning |
| `sample_integrity` | E009–E011 | Too few sources, biased sources, time-clustered sources |
| `reasoning_logic` | E012–E020 | Assumption errors, counterfactual gaps, calibration issues |
| `cross_asset` | E021–E023 | Missing correlation, false inconsistency, time-lag mismatch |
| `confidence` | E024–E025 | Over-confident, under-confident |
| `meta` | E026–E027 | Reasoning-level issues detectable only by reflection-on-reflection |

**Sub-layers** (within `reasoning_logic`):
- `assumption`, `counterfactual`, `calibration`, `history`,
  `cross_asset_consistency`, `mechanism`, `confidence_evidence`

**Design principles**:
- **Every checker is auditable.** Each error code has an explicit
  remediation suggestion in the UI.
- **Checkers refuse false positives.** Cross-asset (E015) checks for
  inverse-pair awareness before reporting contradiction.
- **Severity, not boolean.** Errors are LOW / MEDIUM / HIGH, not pass/fail.
- **The PM can override.** Every error can be overridden with reason;
  overrides are logged and feed back into the meta-loop.

**Data shapes**:
- `decision_errors`: `decision_id`, `error_code`, `severity`, `remediation_text`
- `reflection_cycles`: `decision_id`, `started_at`, `finished_at`, `n_errors`,
  `overrides[]`

---

## L3.5: Meta-Loop (sub-component of L3)

**Responsibility**: When all reasoning-logic checkers pass but the trade
loses, ask whether we need a *new* checker.

**Trigger condition**:
- Decision has 0 errors in `reasoning_logic` layer
- Decision settled at LOSE
- Meta-loop runs daily at 04:10 (cron)

**Process**:
1. Cluster recent losses with 0 errors by feature similarity (cosine on
   the reasoning text)
2. Look for a *missing dimension* (e.g., all 7 checkers tested
   reasoning_logic, but none tested *time-windowed context*)
3. If a missing dimension is detected, write to `META_REFLECTION.md`:
   ```
   On YYYY-MM-DD, 7 reasoning-logic checkers passed on TICKER
   decision #XXX. The trade lost -X.X%. Pattern: <description>.
   Missing dimension: <what no checker tests for>.
   Proposed new checker: E028 <name> — <one-line logic>
   ```
4. The system does **not auto-add** the checker; it proposes. A human
   reviews, then approves.

**Why this design**:
- We don't trust the system to rewrite its own safety checks
- We do trust the system to identify *where* it might be missing checks
- Human-in-the-loop keeps the meta-loop honest---

## L4: Knowledge Graph

**Responsibility**: Grow a persistent graph of *confirmed* insights so the
reflection engine gets smarter over time.

**Approach**:
- Each `review_insight` (PM-validated reflection outcome) becomes a node
- Nodes are linked by: same ticker / same pattern / same error code
- KG is queried at LLM-analysis time (RAG) to bias new decisions toward
  patterns that worked before

**Design principles**:
- **Only validated insights become nodes.** No LLM-generated noise.
- **KG is stored locally.** Same WAL protections as raw_news.
- **KG nodes are versioned.** When a pattern is invalidated, we mark
  it as `superseded`, not delete (audit trail).

---

## L5: Output / UX

**Responsibility**: Make all the above **auditable to humans**.

**Components**:
- Next.js dashboard: live signals, reflection panel, meta-loop, KG view
- Feishu (飞书) alerts for HIGH-severity errors in real-time
- Excel export (5-sheet XLSX with frozen headers + auto-filter)
- Cron-driven L3 automation (daily KG review, causal expansion, 10-min
  scheduler)

**Design principles**:
- **No dark patterns.** Every signal has full reasoning visible.
- **Overridable, but logged.** PM can override; override is forever in
  the audit trail.
- **Local-first rendering.** The dashboard talks to the local API
  server (127.0.0.1:8000); no data leaves your machine.

---

## Replace-in-place guarantees

You can:
- Swap the LLM model in L2 (OpenRouter supports 30+ models) without
  touching L3
- Add a checker in L3 (E028+) without changing L2 or L4
- Replace the KG embedding model without changing L5
- Run the dashboard on any framework that speaks HTTP

You cannot (without writing new code):
- Make L3 trust L2 blindly — by design, L3 always checks L2
- Make the meta-loop write its own checkers — human approval required
- Move data off-device without explicit configuration

---

## Tech stack

| Layer | Tech |
|---|---|
| L1 | Python 3.11+, asyncio, websockets |
| L2 | OpenRouter API (Kimi K3, GPT-4, Claude, DeepSeek — your pick) |
| L3 | Python 3.11+, pure stdlib + minimal deps |
| DB | SQLite (WAL mode) — single file, easy to back up |
| L4 | SQLite + NetworkX-style adjacency in-app |
| L5 | Next.js 14, TailwindCSS |
| Cron | crontab + supervisord (or systemd) |

**We deliberately avoid**:
- ❌ Postgres / heavy infra (SQLite + WAL is enough for paper trading)
- ❌ Kafka / Redis / message queues (overkill for $499/mo target)
- ❌ Cloud-only services (we are local-first)
- ❌ ML frameworks we don't need (PyTorch, etc. — checkers are deterministic)

---

<sub>Last updated: 2026-10-06 — see [CASE_STUDIES.md](./CASE_STUDIES.md) for specific examples of these layers in action</sub>