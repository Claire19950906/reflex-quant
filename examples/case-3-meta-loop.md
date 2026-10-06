# Case Study 3 — WTI Long: Meta-Loop Detected Missing Dimension

**Date**: 2026-09-28 (illustrative)
**Asset**: WTI Crude (CL)
**Direction**: LONG
**Outcome**: LOSE -3.4%

> **This is the most important case study.** Not because of the trade itself,
> but because the system caught its own blindspot.

---

## Trade setup

- **Entry**: 77.20
- **Take profit**: 80.00
- **Stop loss**: 75.50
- **Holding period**: 24h

## What the LLM said

> *"OPEC+ signaled supply discipline Tuesday; current price is below the
> implied floor. Long with tight stop. Entry 77.20, TP 80, SL 75.50."*

**Confidence**: MEDIUM-HIGH
**Evidence grade**: L2

## What the reflection engine caught

**Nothing.** All 7 reasoning-logic checkers passed. The trade looked clean.

The reflection engine output:

```
✅ E012 causal: PASS (multi-hop reasoning)
✅ E013 assumption: PASS (assumption explicit)
✅ E014 counterfactual: PASS (counter considered)
✅ E015 calibration: PASS (confidence matches evidence)
✅ E016 history: PASS (similar setups have positive history)
✅ E017 cross_asset: PASS (no contradictions)
✅ E018 mechanism: PASS (transmission identified)
```

## What the meta-loop caught

The trade settled at LOSE -3.4%. Meta-loop runs daily at 04:10.

It clustered the trade with 2 similar LOSE cases from the same week and ran:

```
CLUSTER ANALYSIS
- Decision #4092: WTI long @ 77.20, lose -3.4%, news age 36h
- Decision #4103: WTI long @ 76.85, lose -2.1%, news age 28h
- Decision #4118: WTI short @ 79.10, lose -1.8%, news age 42h

COMMON FEATURE: rationale cited news from 24-48h before trade entry.

MISSING DIMENSION: no checker currently tests for time-windowed context
validity. All 7 reasoning-logic checkers test the *content* of the
reasoning, but none test the *freshness* of the source.
```

## What got written to META_REFLECTION.md

```
On 2026-09-28, 3 WTI trades passed all 7 reasoning-logic checkers and
settled at LOSE. Pattern: rationale cited news from 24-48h before trade
entry. None of the 7 checkers tested for time-windowed context validity.

PROPOSED NEW CHECKER: E028 cross_temporal_consistency
  Logic: verify news age against decision horizon
    - Intraday (≤4h hold): news must be ≤ 1h old
    - Swing (4h–7d): news must be ≤ 24h old
    - Position (>7d): news must be ≤ 72h old

ACTION: PR #XX opened, awaiting review.
```

## What happened next

A human reviewer (the founder) looked at the proposal:

- ✅ The proposed logic matched the cluster pattern
- ✅ The check was specific enough to be testable
- ✅ The check did not conflict with existing checkers
- ✅ The check was small enough to implement in a single PR

**Decision**: Approve.

A new checker was added to v226 (E028 `cross_temporal_consistency`). Once
shipped, the engine will start flagging trades that cite stale news.

## Why this is the most important case

This is the system **catching what it doesn't know**.

We didn't have a checker for this pattern. The trade passed all existing
checks. We lost money.

The meta-loop's job is to make the reflection engine **more honest over
time** — by recognizing when its existing checks are blind to a dimension.

This is a small example. As more meta-loop cycles run, the system should
propose more checkers. The v225 status:

- **12 meta-loop proposals** to date
- **3 implemented** (E025 confidence_calibration, E026 reasoning_diversity,
  E027 source_time_recency)
- **9 under review**

## Lessons

1. **Even the best reflection engine is blind to something.** The meta-loop
   finds the blindspot.
2. **Human approval is the safety check on the safety check.** The meta-loop
   proposes; humans approve. We don't let the system rewrite its own
   safety checks autonomously.
3. **The system gets more honest over time** — this is the moat.

## What if the meta-loop had not run?

Without the meta-loop:
- The 3 losing trades would have stayed as "passed-all, but lost"
- No human would have connected the dots
- The pattern would have repeated

With the meta-loop:
- The 3 losing trades get clustered
- The cluster pattern is identified
- A new checker is proposed, reviewed, approved, implemented
- The next time a similar trade is suggested, it will be flagged.

---

<sub>Case data illustrative; the meta-loop is a real feature in v225.</sub>