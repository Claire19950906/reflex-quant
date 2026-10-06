# Case Study 2 — XAU/USD Long: Correctly NOT Flagged

**Date**: 2026-09-25 (illustrative)
**Asset**: XAU/USD (Gold)
**Direction**: LONG
**Outcome**: WIN +2.1%

---

## Trade setup

- **Entry**: 2034
- **Take profit**: 2070
- **Stop loss**: 2010
- **Holding period**: 48h

## What the LLM said

> *"DXY weakening, real yields falling, geopolitical risk premium returning
> — gold bid into reflation regime. Entry 2034, TP 2070, SL 2010."*

**Confidence**: MEDIUM-HIGH
**Evidence grade**: L2 (headline + 1 corroborating source)

## What the reflection engine saw

The LLM cited **XAU ↑** and **DXY ↓** in the same reasoning — which *naively*
looks like a contradiction to a simpler checker.

A naive reflection engine would say:

> ❌ "XAU and DXY moved in opposite directions. This is inconsistent. Flag
> E015 cross_asset_consistency."

Reflex Quant's **E015 cross_asset_consistency** instead checked its knowledge
graph first and recognized:

> ✅ "XAU and DXY have inverse correlation. The pattern 'XAU↑ + DXY↓' is the
> canonical safe-haven flow and is internally consistent. **PASS.**"

## Why this matters

Most quant AI systems flag this as inconsistent and refuse the trade. Reflex
Quant's `cross_asset` checker has **inverse-pair awareness** — it knows
which pairs to ignore.

This is the kind of false-positive prevention that saves traders from
missing winning trades. A naive system would have **blocked** this trade.

## What the system learned

The fact that we DIDN'T flag a winning trade is just as important as the
times we did.

This case is logged in the KG with tag `inverse_pair_correct_no_flag`. Future
reflection cycles validate the reflection engine's own judgment over time.

## How the engine knew

The knowledge graph (L4) has an `inverse_pair` cluster containing:

- XAU ↔ DXY (correlation: -0.78 over 3-yr window)
- BTC ↔ DXY (correlation: -0.42)
- Gold ↔ Real Yields (correlation: -0.91)
- SPX ↔ VIX (correlation: -0.85)

When E015 fires, it consults this cluster before reporting a contradiction.

## Quantitative result

- Trade won +2.1% in 48h
- 0 reflection errors
- 0 overrides needed
- KG tag: `inverse_pair_correct_no_flag`

## Lessons

1. **Naive contradiction detection is wrong.** Inverse-pair aware is required.
3. **False positives are just as expensive as false negatives.** A blocked
   winning trade is a missed opportunity.
3. **The KG is the moat.** Without it, E015 can't reason about which pairs
   to ignore.

## Related

- [Full case studies document](../docs/CASE_STUDIES.md)
- [Architecture: reflection engine](../docs/ARCHITECTURE.md)
- [Philosophy: why self-aware AI matters](../docs/PHILOSOPHY.md)

---

<sub>Case data illustrative; the inverse-pair awareness is a real feature.</sub>