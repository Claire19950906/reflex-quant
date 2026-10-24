# Changelog

All notable changes to **Reflex Quant** are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

> **Detail changelog for the engine is in
> [`backend/CHANGELOG.md`](./backend/CHANGELOG.md)** (77KB, full per-commit
> engineering log since v100). This top-level file tracks **user-visible
> milestones** only.

## [Unreleased]


### Added
- **Pattern deep-self-audit chain (v233)** (`backend/src_python/review_agent.py`):
  recursive 5-why / conditions-apply / overfit / fabricated-evidence audit on the
  top 3 patterns emitted by `summary.patterns`. New schema field
  `summary.pattern_audits?: PatternAudit[]` carrying
  `{ claim, factual, evidence, conditions_apply, conditions_fail, overfit_risks,
  confidence, caution, why_chain, depth_reached, stop_reason, fabricated_evidence }`.
  Cap: 3 patterns × 5 depth = 15 LLM calls per report (`stop_reason=max_depth`).
  Anti-fabrication: `_validate_audit_evidence` flags evidence whose `#id` does
  not exist in the supplied trade set, forces `confidence=0.3` and appends a
  "疑似编造" caution (preserved as long as LLM did not already self-flag).
  New `stop_reason=llm_failed` distinguishes "LLM actually unavailable" from
  `no_further_why` (semantic fix from smoke [8]). 18 pytest cases in
  `tests/test_review_pattern_audit.py` (all green; adjacent test files still
  pass). Frontend: `PatternAudit` type + `<PatternAuditItem>` and
  `<PatternAuditBlock>` in `frontend/components/replay/review-panel.tsx`,
  rendered as a collapsible block under the 规律发现 ListBlock. Visible bands:
  confidence (高/中/低) + stop_reason label + caution callout (色调区分
  contradicted / fabricated / no_llm) + why_chain 折叠 + 三列
  (适用/反条件/过拟合) + 证据折叠 (fabricated 划线).

### Added
- **Logic-analysis reflection (v231)** (`backend/src_python/reflection_logic_analysis.py`):
  6 programmatic detectors for formal-logic validity in reasoning_path — fills
  the third leg of the reflection tripod alongside `reflection_deep` (data-driven)
  and `meta_reflection` (system self). New error codes E028–E033 (CRITICAL ×2 +
  WARN ×4): self_contradiction, non_sequitur, false_dichotomy, circular_reasoning,
  equivocation, post_hoc. New `logic_analysis` layer in ontology (weight 3,
  between event_logic and reasoning_logic). New `trade_reflections_v2` columns:
  `logic_integrity_score` (0–1), `logic_findings_json` (JSON),
  `logic_checked_at` (ISO8601). Wired into `reflection_scheduler.py` step6.
  32 unit tests, fully offline. See `backend/META_REFLECTION.md` §1.5.
- Causal graph expansion: data-driven `correlates_with` weights (BTC↔ETH,
  WTI↔XAU, etc.) — replaces guesswork with rolling-window correlation.
- 18 cross-asset edges in `asset_driver_edges` (CPI / DXY / NDX / SPX /
  US10Y / XAG / etc.).
- 594 reasoning-graph nodes + 619 edges produced by LLM 5-why chain
  expansion across 13 assets.

### Changed
- `verify_hypothesis` now sources-aware: matches `ai_decisions` asset
  directly and falls back to indicator-name match in `factor_snapshots`.
- `_chain_infers_direction` vocabulary expanded (24 up-words, 24
  down-words) — LLM-derived Chinese causal chains now correctly infer
  direction.
- `verify_all_pending` shares a single DB connection + 1 commit per cycle
  — 18× faster (5.3s → 0.3s on the 87-node suite).

### Fixed
- **Yahoo Finance 实时 K 线 for 归因页** (`backend/src_python/api_server.py`):
  修复 `/api/replay/signal/{id}/kline` 与 `/api/replay/kline/live` 两个端点对
  `US_HYNIX` / `SKHYNIX` / `MU` / `NVDA` / `TSLA` / `AAPL` / `MSFT` / `META`
  等美股标的退化为「模拟反推」的 bug。`_fetch_binance_klines` 增加 `yahoo`
  分支（interval 1h→60m/4h→60m，limit 按周期缩放），`_fetch_real_klines` 解
  除「非 binance_fapi/okx_swap 即 None」的硬卡，`get_replay_signal_kline` 动
  态返回 `market_type="yahoo"`；前端 `kline-chart.tsx` 增加 Yahoo Finance 蓝
  色角标（与既有 `binance_perp`/`simulated` 并列）。多源 fallback
  yahoo→yfinance→asia 由现有 `multi_source_fetcher.fetch_stock_history_cached`
  承担（v?? 海力士 cap）。
- `_extract_facts` schema mismatch on `factor_snapshots`: old code queried
  `(captured_at, factor_name, value, prev_value)`; real schema is
  `(ts, final_score, gated_action)`. Now derives direction from
  `final_score` delta + `gated_action` fallback. BTC facts: 67 → 228.

### Added
- **Safety guards** (`backend/src_python/safety_guards.py`): two
  cycle-level guards for `quick_sim.open_trades` — daily realized-loss
  kill switch (`check_daily_drawdown`, default limit = one full notional)
  and high-impact event window (`check_event_window`, default ±300 s,
  configurable via `MACRO_EVENTS_FILE`). Both default ON, env-toggleable
  (`DAILY_LOSS_GUARD_ENABLED` / `EVENT_WINDOW_GUARD_ENABLED`); failures
  log + fall through, never break the cycle. 13 unit tests pass.
- **Accuracy baseline CLI** (`backend/src_python/accuracy_baseline.py`):
  reads `quick_sim_trades` (settled=1), computes win rate / avg pnl /
  profit factor / pay-off ratio / max drawdown / per-trade Sharpe /
  per-asset + per-action + per-event-strength breakdown / entry-delay
  buckets. Use `--days 30` / `--from 2026-09-01` / `--asset BTC` /
  `--json` / `--output report.json`. First run on real DB (last 30 days,
  59 trades): overall WR 50.85 %, BUY 55.56 % vs SELL 43.48 %, max DD
  92 % — SELL side now has a measurable baseline to attack.

### Known gap (v230)
- _none_

### Added
- **MAE early-cut (P0.5)** (`backend/src_python/quick_sim.py:cut_bleeding_trades`):
  cycle-level soft stop-loss that closes any open `quick_sim_trades` row
  whose current unrealised loss exceeds `EARLY_LOSS_CUT_PCT` (default
  `0.3 %`). Tuned from real-DB analysis (59 trades / 30 d): SELL WIN max
  MAE `0.251 %`, SELL LOSS max MAE `0.931 %`, BUY LOSS max `0.373 %` —
  threshold `0.3 %` catches 4 LOSS / 0 WIN on historical replay (saved
  bleed ≈ 0.96 percentage points). Wired into `run_cycle` between
  `update_open_extremes` and `settle_trades`; ai_decisions synced
  (COALESCE pattern, matches `settle_trades`). Toggle off via
  `EARLY_LOSS_CUT_ENABLED=0` or `EARLY_LOSS_CUT_PCT=0`. 10 new tests
  cover disabled / below / exact / above / multi-asset / no-decision-id
  paths.

### Added
- **SELL signal gate (P0.5b)** (`backend/src_python/quick_sim.py:gate_sell_signal`):
  rejects SELL orders pre-trade when `event_strength == 'high'` OR asset
  is in `SELL_GATE_BLOCKED_ASSETS` (default `BTC,WTI`). Tuned from real-DB
  (23 SELL trades / 90 d) — historical replay of dropped subset: high
  strength 33 % WR (n=3), BTC 33 % (n=6), WTI 37.5 % (n=8); kept subset
  `n=8 / WR 62.5 % / avg +0.007 %` is the only positive-expected-value
  region (vs SELL overall -0.06 % avg). Counter-intuitively, high-strength
  bearish signals are *anti-predictive* — strongest SELL signals arrive
  after the market has priced in the move. Toggle off via
  `SELL_GATE_ENABLED=0`; adjust asset list via
  `SELL_GATE_BLOCKED_ASSETS=ETH,SOL`. Rejected trades log via
  `[SELL_GATE]` prefix. 12 new tests cover disabled / BUY passthrough /
  high / BTC / WTI / XAU allowed / case-insensitive / custom asset list /
  open_trades integration.
- **TP/SL hard-exit (P0.2)** (`backend/src_python/quick_sim.py:check_tp_sl`):
  enforces hardened exits on every `quick_sim` cycle. Reads
  `paper_trading_settings.tp_enabled + tp1_mult / tp2_mult / sl_mult
  + tp1_ratio / tp2_ratio` (defaults `0.6 % / 1.0 % / 0.5 %`, ratios
  `0.5 / 0.3`); trigger evidence = `max_price / min_price` from
  `update_open_extremes` + current price. SL has priority (capital
  protection). **TP1 hit** → write partial exit (`partial_exit_price /
  partial_exit_ratio / partial_pnl_pct`) to `ai_decisions`; trade stays
  open. **TP2 hit** → full close at blended price (TP1 partial + remainder
  at TP2), `verdict = WIN`. **SL hit** → full close at SL price,
  `verdict = LOSS` (LOSS gets blended pnl = TP1 partial + remainder at
  SL). Backwards-compatible DB migration adds `sl_mult` column (default
  `0.5`); reflection_ontology referenced `sl_mult` but it was a bare
  name before — now real. New config knobs:
  `TP_SL_ENABLED=0|1` (global kill-switch, default 1) and
  `TP_SL_DEFAULT_TP1_MULT / TP2_MULT / SL_MULT / TP1_RATIO / TP2_RATIO`
  (defaults, overridable per-run via settings). Wired into `run_cycle`
  between `update_open_extremes` and `cut_bleeding_trades`. 20 new tests
  cover global / per-run kill-switches / bad config (zero mults, sum
  ratios > 1) / BUY-SELL TP2 / SL / partial-blend / max-price trigger
  / idempotent partial / mixed-cycle priority / price-unavailable
  fallback. **Known gap (P0.2 part B)**: per-trade breakeven-move after
  TP1 not implemented — would need a `sl_price` column on
  `quick_sim_trades`. Logged for future iteration.

- **Breakeven-move after TP1 (P0.2 part B)** (`backend/src_python/quick_sim.py:check_tp_sl`):
  closes the only remaining TP/SL gap. Adds `quick_sim_trades.sl_price
  REAL DEFAULT NULL` (auto-migrated; NULL = legacy formula). When a trade
  hits TP1, `_record_tp1_partial` writes `sl_price = entry × (1 ±
  offset × sgn)` to that row if not already set (idempotent on repeat
  TP1s). Subsequent `check_tp_sl` cycles see the override and use it
  instead of `entry × (1 ∓ sl_mult)`. New env knobs:
  `TP_SL_BREAKEVEN_ENABLED=0|1` (default 1 — production-safe: starts
  locked to entry, never worse than legacy), `TP_SL_BREAKEVEN_OFFSET_PCT=0`
  (set positive to lock in micro-profit e.g. `0.01`). Out dict from TP1
  now exposes `breakeven_applied` and `breakeven_sl_price`. Idempotency:
  existing override is preserved across re-runs. 7 new tests cover
  disabled / enabled (entry) / with-offset / override-uses-not-original /
  blended PnL at breakeven / SELL symmetry / idempotent re-fire. The
  full quick_sim suite (early_cut + sell_gate + tp_sl + db_migrate_order)
  is **52/52 green**.

### Added
- **Confidence-Tier Sizing (P0.1 v1)** (`backend/src_python/position_sizing.py`,
  hooked in `backend/src_python/quick_sim.py:open_trades`): scales each
  trade's `notional_usdt` by a tiered multiplier based on
  `ai_decisions.evidence_confidence`. Defaults: low conf `< 0.3` →
  `0.5×` base, medium `[0.3, 0.7)` → `1.0×`, high `≥ 0.7` → `1.5×`.
  Clamped to `[POSITION_SIZING_MIN_MULT, POSITION_SIZING_MAX_MULT]`
  (defaults `0.25×` / `2.0×`) to prevent runaway. Action-agnostic (v1
  keeps BUY/SELL same multiplier); action-aware scaling deferred to v2
  pending SELL data. New env knobs:
  `POSITION_SIZING_ENABLED=0|1` (default 1),
  `POSITION_SIZING_TIERS_JSON` (override default 3-tier),
  `POSITION_SIZING_MIN_MULT` / `POSITION_SIZING_MAX_MULT`. Missing or
  out-of-range `evidence_confidence` falls back to base × 1.0
  transparently (never blocks a trade). Returns
  `(scaled_notional, metadata dict)` for audit. New `[POSITION_SIZING]`
  log line per cycle shows tier counts + sample. 12 new tests cover
  kill switch / 3-tier mapping / clamp / missing / out-of-range /
  custom JSON tiers / invalid JSON fallback / BUY-SELL parity /
  end-to-end `open_trades` integration. The full P0.* suite
  (early_cut + sell_gate + tp_sl + sizing) is **61/61 green**.

## [v225] — 2026-10-06

### Added
- 27-layer reflection engine (E001–E027) covering event_logic, causal,
  sample_integrity, and reasoning_logic (7 sub-checkers).
- Meta-reflection loop: when all 7 reasoning-logic checkers miss a bad
  trade, log the blindspot and propose a new checker (E028+).
- Cron-driven L3 automation: daily KG review (04:00), causal expansion
  (04:10), 10-min scheduler.
- 5-sheet Excel export with frozen headers + auto-filter.
- 189-test pytest suite (all offline, CI-ready).

### Notes
- This is the first public release of Reflex Quant.

[Unreleased]: https://github.com/Claire19950906/reflex-quant/compare/v225...HEAD
[v225]: https://github.com/Claire19950906/reflex-quant/releases/tag/v225
