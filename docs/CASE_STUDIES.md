# Case Studies — Real Examples

> These case studies are derived from real paper trades run with v225.
> Asset names and entry prices are illustrative; the **error patterns are real**.
> See `../examples/case-N-*.md` for the per-case markdown walkthrough.

---

## Case 1 — WTI Short: System flagged sample-integrity failure

**Date**: 2026-09-28 (illustrative)
**Asset**: WTI Crude
**Decision**: SHORT @ 78.50, TP 75.00, SL 80.00
**Outcome**: LOSE -3.0%

### What the LLM said

> *"API inventory build signals demand softness in EM; OPEC+ cuts won't be
> enough to absorb supply in the near term. Entry 78.50, TP 75, SL 80."*

### What the reflection engine caught

Three errors, all from the same root cause:

| Code | Layer | What it caught |
|---|---|---|
| **E007** | sample_integrity | Only 1 source (TreeNews). Threshold is 3. |
| **E012** | causal | Single-hop reasoning — went from "supply > demand" to "price ↓" without modeling the demand side. |
| **E019** | confidence_evidence | Confidence marked MEDIUM, but evidence_grade was L1 (headline only). |

### What the PM did

PM **overrode** E007 because they had a personal channel they trusted. Trade
executed anyway. Lost -3.0%.

### What the system learned

This case is now in the KG with tag `single_source_energy_short_override_loss`.
The reflection engine biases future energy trades to require ≥ 2 independent
sources unless explicitly overridden.

📚 See [examples/case-1-wti-short.md](../examples/case-1-wti-short.md).

---

## Case 2 — XAU/USD Long: System correctly DIDN'T flag

**Date**: 2026-09-25 (illustrative)
**Asset**: XAU/USD
**Decision**: LONG @ 2034, TP 2070, SL 2010
**Outcome**: WIN +2.1%

### What the LLM said

> *"DXY weakening, real yields falling, geopolitical risk premium returning
> — gold bid into reflation regime. Entry 2034, TP 2070, SL 2010."*

### What the reflection engine saw

Both **XAU ↑** and **DXY ↓** — which *naively* looks like a contradiction to a
simpler checker. **E015 (cross_asset_consistency)** however recognized:

> *"XAU and DXY have inverse correlation. The pattern 'XAU↑ + DXY↓' is the
> canonical safe-haven flow and is internally consistent. PASS."*

### Why this matters

Most quant AI systems flag this as inconsistent and refuse the trade. Reflex
Quant's `cross_asset` checker has **inverse-pair awareness** — it knows
which pairs to ignore.

### What the system learned

The fact that we DIDN'T flag a winning trade is just as important as the
times we did. This case is logged in the KG with tag `inverse_pair_correct_no_flag`,
so the reflection engine can validate its own judgment over time.

📚 See [examples/case-2-gold-dxy.md](../examples/case-2-gold-dxy.md).

---

## Case 3 — WTI Long: Meta-loop detected missing dimension

**Date**: 2026-09-28 (illustrative)
**Asset**: WTI Crude
**Decision**: LONG @ 77.20, TP 80.00, SL 75.50
**Outcome**: LOSE -3.4%

### What the LLM said

> *"OPEC+ signaled supply discipline Tuesday; current price is below the
> implied floor. Long with tight stop. Entry 77.20, TP 80, SL 75.50."*

### What the reflection engine caught

**Nothing.** All 7 reasoning-logic checkers passed. The trade looked clean.

### What the meta-loop caught

Meta-loop runs daily at 04:10. It clustered the trade with 2 similar LOSE
cases from the same week:

> *"On 2026-09-28, 3 WTI trades with reasoning-pattern similarity 0.78 all
> settled at LOSE despite passing all 7 reasoning-logic checkers. Common
> feature: rationale cited news from 24–48h before trade entry. **No checker
> currently tests for time-windowed context validity.**"*

### What got written to META_REFLECTION.md

```
On 2026-09-28, 3 WTI trades passed all 7 reasoning-logic checkers and
settled at LOSE. Pattern: rationale cited news from 24-48h before trade
entry. None of the 7 checkers tested for time-windowed context validity.

Proposed E028: cross_temporal_consistency — verify news age against
decision horizon (e.g., 4h news for intraday, 7d news for swing).

Action: PR #XX opened, awaiting review.
```

### Why this is the most important case

This is the system **catching what it doesn't know**. We didn't have a checker
for this pattern. Now we do (or will, when the PR merges).

The meta-loop's job is to make the reflection engine **more honest over time**.

📚 See [examples/case-3-meta-loop.md](../examples/case-3-meta-loop.md).

---

## Summary stats (v225 paper-trading, 7-day window)

| Metric | Value |
|---|---|
| Total trades | 59 |
| Wins | 31 (52.5%) |
| Losses | 24 (40.7%) |
| Override + LOSE | 4 (6.8%) — these are the **most expensive** trades |
| Avg win | +1.8% |
| Avg loss | -1.4% |
| Avg override-loss | **-3.1%** ← the 4 trades where we overrode a HIGH error |
| Errors caught | 73 (across 7 layers) |
| Meta-loop proposals | 12 (3 implemented, 9 under review) |
| Cross-asset false positives avoided | 6 |

The single most actionable insight:

> **The 4 trades where a PM overrode a HIGH-severity error lost 3x more
> than the average losing trade.** This suggests the reflection engine's
> HIGH-severity calls are worth taking seriously.

---

<sub>Last updated: 2026-10-06 — case data illustrative; production stats come from your own paper-trading window</sub>