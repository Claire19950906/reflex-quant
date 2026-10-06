# Case Study 1 — WTI Short: Sample-Integrity Failure

**Date**: 2026-09-28 (illustrative)
**Asset**: WTI Crude (CL)
**Direction**: SHORT
**Outcome**: LOSE -3.0%

---

## Trade setup

- **Entry**: 78.50
- **Take profit**: 75.00
- **Stop loss**: 80.00
- **Holding period**: 24h

## What the LLM said

> *"API inventory build signals demand softness in EM; OPEC+ cuts won't be
> enough to absorb supply in the near term. Entry 78.50, TP 75, SL 80."*

**Confidence**: MEDIUM (per LLM self-report)
**Evidence grade**: L1 (headline only — only one source was checked)

## What the reflection engine caught

Three errors. All from the same root cause: **insufficient sourcing**.

| Code | Layer | Severity | What it caught |
|---|---|---|---|
| **E007** | sample_integrity | **HIGH** | Only 1 source (TreeNews). System threshold for energy shorts is ≥ 3 independent sources. |
| **E012** | causal | MEDIUM | Single-hop reasoning — went from "supply > demand" to "price ↓" without modeling the demand side. |
| **E019** | confidence_evidence | MEDIUM | Confidence marked MEDIUM, but evidence_grade was L1. The two are inconsistent. |

## How the PM responded

The PM read the LLM reasoning, agreed it was a sensible thesis, and **chose
to override E007** because they had a personal Bloomberg channel they
trusted. (This channel is not exposed to the reflection engine.)

Trade executed. Lost -3.0%.

## What the system learned

This case is now in the KG with tag `single_source_energy_short_override_loss`.

The reflection engine now biases future energy trades:
- Energy shorts require ≥ 2 independent sources unless explicitly overridden
- Override-with-loss is a high-weight training signal for the meta-loop
- The PM's personal channel count: weighted at 0.6× for energy shorts

## Why this case matters

> **The 4 trades where a PM overrode a HIGH-severity error lost 3x more than
> the average losing trade.**

The reflection engine is right more often than the PM's gut, when the engine
calls HIGH. This is the most actionable insight from v225.

## Lessons

1. **Single-source signals are dangerous** in commodity markets.
2. **Reasoning confidence must match evidence grade** — the LLM's
   self-reported confidence is unreliable on L1 evidence.
3. **Overriding a HIGH error is the highest-cost decision** a PM can make.

## Related

- [Full case studies document](../docs/CASE_STUDIES.md)
- [Architecture: reflection engine](../docs/ARCHITECTURE.md)
- [Philosophy: why self-aware AI matters](../docs/PHILOSOPHY.md)

---

<sub>Case data illustrative; the error pattern is real.</sub>