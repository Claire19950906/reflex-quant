#!/usr/bin/env python3
"""db_schema.py — db.py 里所有建表 / 建索引 SQL 常量（纯字符串，无副作用）。

为什么拆出来：
  db.py 6,556 行里，光 "_CREATE_xxx = \"\"\"…\"\"\"" 这类常量就占 1,880 行（29%），
  把真正的逻辑（migrate / insert_raw_news / 各 ensure_*）压到看不见。
  这些常量是纯字面量、彼此不引用，搬家零风险；db.py 用 `from db_schema import *`
  原样接回，所以 `db._CREATE_AI_DECISIONS` 这种外部引用完全不用改。
"""

__all__ = [
    "_CREATE_RAW_NEWS", "_CREATE_AI_DECISIONS", "_CREATE_BACKTEST_EVENT_OUTCOMES", "_CREATE_SIGNAL_WEIGHT_HISTORY", "_CREATE_SOURCE_STATS", "_CREATE_SIGNAL_WEIGHT_CRON_LOG", "_CREATE_IDX_SWH_DECISION", "_CREATE_IDX_SWH_COMPUTED", "_CREATE_IDX_SS_SOURCE", "_CREATE_DECISION_ACTIONS", "_CREATE_IDX_DEC_ACTIONS_DECISION", "_CREATE_IDX_DEC_ACTIONS_ASSET", "_CREATE_MARKET_TICKS", "_CREATE_FACTOR_SNAPSHOTS", "_CREATE_NEWS_EVENTS", "_CREATE_SIGNAL_PERFORMANCE", "_CREATE_STRATEGIES", "_CREATE_STRATEGY_VERSIONS", "_CREATE_PAPER_TRADING_SETTINGS", "_CREATE_PAPER_TRADING_RUNS", "_CREATE_QUICK_SIM_TRADES", "_CREATE_HERMES_OBSERVATIONS", "_CREATE_HERMES_SKILLS", "_CREATE_MACRO_SNAPSHOTS", "_CREATE_EQUITY_FUNDAMENTALS", "_CREATE_IDX_EQ_FUND", "_CREATE_EQUITY_EARNINGS", "_CREATE_IDX_EQ_EARNINGS", "_CREATE_CN_EQUITY_FUNDAMENTALS", "_CREATE_IDX_CN_EQ_FUND", "_CREATE_CN_VALUATION_SNAPSHOTS", "_CREATE_IDX_CN_VAL", "_CREATE_KLINE_BARS", "_CREATE_KLINE_SNAPSHOTS", "_CREATE_MARKET_STYLE_SNAPSHOT", "_CREATE_DECISION_REFLECTION", "_CREATE_BACKTEST_RUNS", "_CREATE_REVIEW_REPORTS", "_CREATE_REVIEW_INSIGHTS", "_CREATE_STRUCTURED_REFLECTIONS", "_CREATE_NEWS_TRADE_REVIEWS", "_CREATE_ATTRIBUTION_REVIEWS", "_CREATE_FILTER_KEYWORDS", "_CREATE_TRADE_REFLECTIONS", "_CREATE_EXPERIENCE_ENTRIES", "_CREATE_EXPERIENCE_APPLICATIONS", "_CREATE_GATE_REFLECTIONS_CYCLE", "_CREATE_TRADE_REFLECTIONS_V2", "_CREATE_DECISION_LAYER_SIGNALS", "_CREATE_REVERSE_ATTRIBUTIONS", "_CREATE_POLICY_PATCH_QUEUE", "_CREATE_IDX_PPQ_STATUS", "_CREATE_IDX_PPQ_PENDING", "_CREATE_SIBLING_DIFF_RULES", "_CREATE_IDX_SDR_STATUS", "_CREATE_IDX_SDR_FINGERPRINT", "_CREATE_FEED_HEALTH", "_CREATE_IDX_FEED_HEALTH_ASSET_TS", "_CREATE_IDX_FEED_HEALTH_KIND", "_CREATE_IDX_FEED_HEALTH_STATUS", "_CREATE_UNCLASSIFIED_EVENTS", "_CREATE_IDX_UNCLASSIFIED_PENDING", "_CREATE_IDX_UNCLASSIFIED_NEWS", "_CREATE_MISSED_SIGNAL_SCAN_STATE", "_CREATE_BACKTEST_DAILY_RUNS", "_CREATE_REFLECTION_DAILY_JOURNAL", "_CREATE_REFLECTION_CYCLE_LOG", "_CREATE_EXPERIENCE_APPLICATION_DAILY", "_CREATE_ERROR_CODE_LEDGER", "_CREATE_REFLECTION_CYCLES", "_CREATE_KLINE_DECISION_CACHE", "_CREATE_MONITOR_LLM_VALUE", "_CREATE_TRADE_AUDIT_LOG", "_CREATE_AUDIT_LOG", "_CREATE_DAILY_RESET_LOG", "_CREATE_SIGNAL_CHAIN_ARCHIVE", "_CREATE_DAILY_REFLECTION_SUMMARY", "_CREATE_STRATEGY_PARAM_ADJUSTMENTS", "_CREATE_DECISION_EVIDENCE", "_CREATE_SIGNAL_STATE", "_CREATE_AI_PROMPTS", "_CREATE_AI_MAIN_PROMPTS", "_CREATE_AI_ASSET_RULES", "_CREATE_CAUSAL_CHAINS", "_CREATE_ASSET_DRIVER_NODES", "_CREATE_ASSET_DRIVER_EDGES", "_CREATE_KG_IMPROVEMENTS", "_CREATE_IDX_KG_IMPROVEMENTS_STATUS", "_CREATE_WORKER_HEARTBEAT", "_CREATE_DLQ_EVENTS", "_CREATE_ABNORMAL_SIGNALS", "_CREATE_ABNORMAL_ATTRIBUTIONS", "_CREATE_IDX_ABN_ATTR_SIGNAL", "_CREATE_IDX_ABN_ATTR_ASSET_TS", "_CREATE_SOURCE_STATUS_CURRENT", "_CREATE_SOURCE_STATUS_HISTORY", "_CREATE_IDX_SOURCE_STATUS_HISTORY", "_CREATE_DIM_ASSET", "_CREATE_DIM_EVENT_TYPE", "_CREATE_EVENT_ATOM", "_CREATE_EVENT_TIME_CANDIDATES", "_CREATE_EVENT_IMPACT_CURVE", "_CREATE_COINGLASS_LSR_SAMPLES", "_COAINGLASS_INDEX_NAME", "_COAINGLASS_INDEX_NAME_WHALE", "_CREATE_COINGLASS_LSR_HEALTH", "_CREATE_EVENT_IMPACT_STAT", "_CREATE_STRATEGY_REFLECTIONS", "_CREATE_EVENT_KLINE_IMPACT", "_CREATE_MACRO_NOTIFY_STATE", "_CREATE_MACRO_CALENDAR", "_CREATE_QUALITY_REJECTIONS", "_CREATE_CROWD_SOURCE_ITEMS", "_CREATE_CROWD_TOPIC_DAILY", "_CREATE_CROWD_MONTHLY_RANKINGS", "_CREATE_CROWD_AUDIENCE_SEGMENTS", "_CREATE_CROWD_SOURCE_AUTHORITY", "_CREATE_CROWD_SOURCE_RUNS", "_CREATE_GATE_CALIBRATION", "_CREATE_GATE_EVALUATIONS", "_CREATE_DECISION_REF_ARCHIVE", "_AI_DECISIONS_AUTOTS_TRIGGER_DDL", "_AI_DECISIONS_GUARD_TRIGGER_DDL", "_RAW_NEWS_PROBE_GUARD_TRIGGER_DDL", "_QUICK_SIM_ORPHAN_GUARD_TRIGGER_DDL", "_AI_DECISIONS_TRADE_UUID_TRIGGER_DDL", "_T2_ORDER_DEDUP_TRIGGER_DDL", "_CALL_STATS_SQL",
]

_CREATE_RAW_NEWS = """
    CREATE TABLE IF NOT EXISTS raw_news (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source          TEXT    NOT NULL,
        content         TEXT    NOT NULL,
        timestamp       TEXT    NOT NULL DEFAULT (strftime('%s','now')),
        ts              INTEGER,
        status          TEXT    NOT NULL DEFAULT 'PENDING'
            -- v322: 'FILTERED' —— 「系统主动不再分析」终态（超龄 / 僵尸行）。
            --   与 'FAILED'（分析失败）是两回事，必须分开：
            --   FAILED = 试过了、没成；FILTERED = 根本没试（超龄不可交易）。
            --   ⚠️ 不加它就是 2026-11-07 事故：ai_worker._expire_stale_news 与
            --   scripts/cleanup_noise_pending.py 把僵尸行标成 'FILTERED'，
            --   **每条 UPDATE 都被 CHECK 拒绝** → 异常被 except 吞掉 →
            --   库里 0 条 FILTERED → 前端「待分析」4850 条永不下降。
            CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED', 'FILTERED')),
        is_noise        INTEGER NOT NULL DEFAULT 0,
        relevance_score REAL    NOT NULL DEFAULT 0.0,
        published_at    TEXT    NOT NULL DEFAULT '',
        gate_core_impact  TEXT  DEFAULT '',
        gate_is_material  INTEGER DEFAULT 0,
        gate_assets       TEXT  DEFAULT '',
        gate_main_asset   TEXT  DEFAULT '',
        gate_kind         TEXT  DEFAULT '',
        gate_timing       TEXT  DEFAULT '',
        gate_strength     INTEGER DEFAULT 0,
        gate_verdict      TEXT  DEFAULT '',
        gate_impact_1to100 INTEGER DEFAULT 0,           -- v181: L3 自评 1-100 影响力打分（<20 一律排除）
        filter_reason   TEXT    NOT NULL DEFAULT '',    -- v408: 不进分析队列的原因（stale_expired 等）
        content_fingerprint TEXT  NOT NULL DEFAULT '',  -- 归一化内容指纹（跨源相似溯源）
        first_seen_ts      TEXT   NOT NULL DEFAULT ''   -- 全网最早出现时间（同指纹最早采集时刻）
    );
"""

_CREATE_AI_DECISIONS = """
    CREATE TABLE IF NOT EXISTS ai_decisions (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id           INTEGER NOT NULL,
        sentiment_score   REAL    NOT NULL,
        suggested_action  TEXT    NOT NULL,
        reasoning         TEXT    NOT NULL,
        created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
        status            TEXT    NOT NULL DEFAULT 'UNREAD'
            CHECK (status IN ('UNREAD', 'APPROVED', 'REJECTED', 'REVIEWED', 'AUTO_APPROVED')),
        market_category   TEXT    NOT NULL DEFAULT '',
        target_asset      TEXT    NOT NULL DEFAULT 'NONE',
        parent_id         INTEGER DEFAULT NULL,
        child_count       INTEGER DEFAULT 0,
        aggregation_key   TEXT    DEFAULT '',
        cluster_size      INTEGER DEFAULT 1,
        reasoning_path    TEXT    DEFAULT '',
        vip_tag           TEXT    DEFAULT '',
        doubao_action     TEXT    DEFAULT 'HOLD',
        doubao_reasoning  TEXT    DEFAULT '',
        extra_models_consensus TEXT DEFAULT '',
        entry_price       REAL    DEFAULT NULL,
        -- v489：方向单为什么没落地。NULL=还没判断，空串=正常执行，
        -- 其它值=具体原因（missing_entry_price / rejected_by_gate / ...）
        exec_state        TEXT    DEFAULT NULL,
        exit_price        REAL    DEFAULT NULL,
        max_price         REAL    DEFAULT NULL,
        min_price         REAL    DEFAULT NULL,
        max_price_time    INTEGER DEFAULT 0,
        min_price_time    INTEGER DEFAULT 0,
        is_correct        TEXT    DEFAULT '',
        settled           INTEGER DEFAULT 0,
        entry_time        TEXT    DEFAULT '',
        prediction_type        TEXT    DEFAULT 'continuation',
        event_phase            TEXT    DEFAULT 'mid',
        market_confirmation    TEXT    DEFAULT 'unknown',
        expected_horizon       TEXT    DEFAULT '1-3d',
        invalidation_condition TEXT    DEFAULT '',
        decision_context       TEXT    DEFAULT '{}',
        mfe_pct           REAL    DEFAULT NULL,
        mae_pct           REAL    DEFAULT NULL,
        forward_pnl       REAL    DEFAULT NULL,
        mfe_time_mins     REAL    DEFAULT NULL,
        event_strength    TEXT    DEFAULT 'medium',
        direct_catalyst   INTEGER DEFAULT 0,
        timeframe_match   TEXT    DEFAULT 'intraday',
        paper_trading_run_id INTEGER DEFAULT NULL,
        agent_model_id       TEXT    DEFAULT '',
        evidence_confidence  REAL    DEFAULT NULL,
        evidence_action      TEXT    DEFAULT 'HOLD',
        trade_gate_reason    TEXT    DEFAULT '',
        partial_exit_price   REAL    DEFAULT NULL,
        partial_exit_ratio   REAL    DEFAULT 0.0,
        partial_pnl_pct      REAL    DEFAULT 0.0,
        insight_hit_ids      TEXT    DEFAULT '',
        no_trade_reason      TEXT    DEFAULT '',
        heat_score           REAL    DEFAULT NULL,
        impact_score         REAL    DEFAULT NULL,
        asset_impact         REAL    DEFAULT NULL,
        is_sibling           INTEGER NOT NULL DEFAULT 0,  -- 1=多品种副决策行（同新闻主决策的其它品种）
        asset_reasoning      TEXT    DEFAULT '',  -- AI 逐品种解释"为什么该新闻关联此品种"（主行存全量）
        asset_nature         TEXT    DEFAULT '',  -- 八步①资产性质（避险/投资/投机/抗通胀等）
        factor_ranking       TEXT    DEFAULT '',  -- 八步②影响涨跌因素排行(JSON)
        impact_magnitude     REAL    DEFAULT NULL,  -- 八步③影响数量级 1-100
        verification         TEXT    DEFAULT NULL,  -- 八步⑧其他模型校验回写(JSON)
        attention_score      REAL    DEFAULT NULL,  -- 需求5：0-100 全市场关注度得分
        elasticity_window    TEXT    DEFAULT '',     -- 需求5：弹性窗参数 JSON 字符串
        data_anchor_ts       TEXT    DEFAULT '',     -- 需求5：结论依据行情数据对应时间戳
        hard_data_evidence   TEXT    DEFAULT '',     -- 需求5：逐条结论附带时间的硬数据截图
        event_struct         TEXT    DEFAULT '',     -- 规则抽取事件原子 JSON（event_struct 抽取，供规则 vs LLM 交叉校验标黄）
        FOREIGN KEY (news_id) REFERENCES raw_news(id) ON DELETE CASCADE
    );
"""

_CREATE_BACKTEST_EVENT_OUTCOMES = """
    CREATE TABLE IF NOT EXISTS backtest_event_outcomes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        type_code       TEXT    NOT NULL,
        asset           TEXT    NOT NULL DEFAULT '',
        sample_n        INTEGER NOT NULL DEFAULT 0,
        win_n           INTEGER NOT NULL DEFAULT 0,
        loss_n          INTEGER NOT NULL DEFAULT 0,
        avg_pnl_4h      REAL    NOT NULL DEFAULT 0.0,
        last_updated    INTEGER NOT NULL,
        UNIQUE (type_code, asset)
    );
"""

_CREATE_SIGNAL_WEIGHT_HISTORY = """
    CREATE TABLE IF NOT EXISTS signal_weight_history (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id     INTEGER NOT NULL,
        computed_at     INTEGER NOT NULL,                  -- UTC 秒
        trigger_kind    TEXT    NOT NULL DEFAULT 'insert', -- insert | cron_react | cron_settle | manual
        signal_weight   REAL    NOT NULL,
        weight_tier     TEXT    NOT NULL DEFAULT '',
        base_mult       REAL    NOT NULL DEFAULT 1.0,
        consensus_mult  REAL    NOT NULL DEFAULT 1.0,
        source_mult     REAL    NOT NULL DEFAULT 1.0,
        reaction_mult   REAL    NOT NULL DEFAULT 1.0,
        regime_mult     REAL    NOT NULL DEFAULT 1.0,
        time_mult       REAL    NOT NULL DEFAULT 1.0,
        impact_mult     REAL    NOT NULL DEFAULT 1.0,     -- v314: 预期市场影响乘子 (0.3~1.5)
        explain         TEXT    NOT NULL DEFAULT '',
        abnormal_id     INTEGER DEFAULT NULL,              -- 反应系数所用的 abnormal_signals.id
        source_stats_id INTEGER DEFAULT NULL               -- 源系数所用的 source_stats.id
    );
"""

_CREATE_SOURCE_STATS = """
    CREATE TABLE IF NOT EXISTS source_stats (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        news_source     TEXT    NOT NULL,
        asset           TEXT    NOT NULL DEFAULT '',
        sample_n        INTEGER NOT NULL DEFAULT 0,
        win_n           INTEGER NOT NULL DEFAULT 0,
        loss_n          INTEGER NOT NULL DEFAULT 0,
        avg_forward_pnl REAL    NOT NULL DEFAULT 0.0,
        hit_rate        REAL    NOT NULL DEFAULT 0.0,        -- win / (win+loss) ∈ [0,1]; sample<5 时为 NULL
        source_mult     REAL    NOT NULL DEFAULT 1.0,        -- 命中率→权重 (0.3-1.5)
        last_updated    INTEGER NOT NULL,
        UNIQUE (news_source, asset)
    );
"""

_CREATE_SIGNAL_WEIGHT_CRON_LOG = """
    CREATE TABLE IF NOT EXISTS signal_weight_cron_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at      INTEGER NOT NULL,
        finished_at     INTEGER,
        processed_n     INTEGER NOT NULL DEFAULT 0,
        updated_n       INTEGER NOT NULL DEFAULT 0,
        cold_n          INTEGER NOT NULL DEFAULT 0,
        error_msg       TEXT    NOT NULL DEFAULT ''
    );
"""

_CREATE_IDX_SWH_DECISION = "CREATE INDEX IF NOT EXISTS idx_swh_decision ON signal_weight_history(decision_id);"

_CREATE_IDX_SWH_COMPUTED = "CREATE INDEX IF NOT EXISTS idx_swh_computed ON signal_weight_history(computed_at);"

_CREATE_IDX_SS_SOURCE    = "CREATE INDEX IF NOT EXISTS idx_ss_source    ON source_stats(news_source, asset);"

_CREATE_DECISION_ACTIONS = """
    CREATE TABLE IF NOT EXISTS decision_actions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id  INTEGER NOT NULL,
        asset        TEXT    NOT NULL DEFAULT '',
        action       TEXT    NOT NULL,              -- silence_asset / ignore_decision / reverse
        duration_min INTEGER NOT NULL DEFAULT 0,    -- 静默时长（分钟），0=永久
        expires_at   INTEGER NOT NULL DEFAULT 0,    -- epoch 秒；0=永久
        note         TEXT    NOT NULL DEFAULT '',
        source       TEXT    NOT NULL DEFAULT 'ui', -- ui / lark / api
        scope        TEXT    NOT NULL DEFAULT 'asset', -- v390: asset=整品种，event=仅该事件类型
        event_type   TEXT    NOT NULL DEFAULT '',      -- v390: scope=event 时生效的事件类型码
        created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_CREATE_IDX_DEC_ACTIONS_DECISION = (
    "CREATE INDEX IF NOT EXISTS idx_dec_actions_decision ON decision_actions(decision_id);"
)

_CREATE_IDX_DEC_ACTIONS_ASSET = (
    "CREATE INDEX IF NOT EXISTS idx_dec_actions_asset ON decision_actions(asset, action, expires_at);"
)

_CREATE_MARKET_TICKS = """
    CREATE TABLE IF NOT EXISTS market_ticks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol       TEXT    NOT NULL,
        ts           INTEGER NOT NULL,
        price        REAL    NOT NULL,
        change24h    REAL    DEFAULT NULL,   -- v489：NULL=没取到昨收；0.0=真没波动
        source       TEXT    NOT NULL DEFAULT 'median',
        source_count INTEGER NOT NULL DEFAULT 0,
        UNIQUE (symbol, ts, source)
    );
"""

_CREATE_FACTOR_SNAPSHOTS = """
    CREATE TABLE IF NOT EXISTS factor_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id       INTEGER NOT NULL,
        asset         TEXT    NOT NULL DEFAULT 'NONE',
        ts            INTEGER NOT NULL,
        final_score   REAL    NOT NULL DEFAULT 0.0,
        confidence    REAL    NOT NULL DEFAULT 0.0,
        raw_action    TEXT    NOT NULL DEFAULT 'HOLD',
        gated_action  TEXT    NOT NULL DEFAULT 'HOLD',
        p_value       REAL    NOT NULL DEFAULT 1.0,
        chi_square    REAL    NOT NULL DEFAULT 0.0,
        sample_size   INTEGER NOT NULL DEFAULT 0,
        contradictions INTEGER NOT NULL DEFAULT 0,
        factors       TEXT    NOT NULL DEFAULT '{}',
        evidence      TEXT    NOT NULL DEFAULT '{}'
    );
"""

_CREATE_NEWS_EVENTS = """
    CREATE TABLE IF NOT EXISTS news_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id     INTEGER NOT NULL,
        decision_id INTEGER,
        ts          INTEGER NOT NULL,
        source      TEXT    NOT NULL DEFAULT '',
        asset       TEXT    NOT NULL DEFAULT 'NONE',
        action      TEXT    NOT NULL DEFAULT 'HOLD',
        score       REAL    NOT NULL DEFAULT 0.0,
        is_noise    INTEGER NOT NULL DEFAULT 0,
        status      TEXT    NOT NULL DEFAULT 'PENDING',
        UNIQUE (news_id, ts)
    );
"""

_CREATE_SIGNAL_PERFORMANCE = """
    CREATE TABLE IF NOT EXISTS signal_performance (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL,
        asset       TEXT    NOT NULL DEFAULT 'NONE',
        ts          INTEGER NOT NULL,
        action      TEXT    NOT NULL DEFAULT 'HOLD',
        is_correct  TEXT    NOT NULL DEFAULT '',
        forward_pnl REAL,
        mfe_pct     REAL,
        mae_pct     REAL,
        UNIQUE (decision_id, ts)
    );
"""

_CREATE_STRATEGIES = """
    CREATE TABLE IF NOT EXISTS strategies (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        slug        TEXT    NOT NULL UNIQUE,
        name        TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        status      TEXT    NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'archived')),
        is_current  INTEGER NOT NULL DEFAULT 0,
        current_version_id INTEGER DEFAULT NULL,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_CREATE_STRATEGY_VERSIONS = """
    CREATE TABLE IF NOT EXISTS strategy_versions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id INTEGER NOT NULL,
        version     INTEGER NOT NULL,
        params      TEXT    NOT NULL DEFAULT '{}',
        source      TEXT    NOT NULL DEFAULT 'manual'
            CHECK (source IN ('manual', 'seed', 'llm', 'backtest', 'reflection')),
        note        TEXT    NOT NULL DEFAULT '',
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE (strategy_id, version),
        FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE
    );
"""

_CREATE_PAPER_TRADING_SETTINGS = """
    CREATE TABLE IF NOT EXISTS paper_trading_settings (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        is_running  INTEGER NOT NULL DEFAULT 1,  -- 默认开启模拟盘 (v172+)
        tracks      TEXT    NOT NULL DEFAULT '[]',
        started_at  TEXT,
        active_run_id INTEGER DEFAULT NULL,
        gate_enabled INTEGER NOT NULL DEFAULT 1,
        news_enabled INTEGER NOT NULL DEFAULT 1,
        news_daily_target INTEGER NOT NULL DEFAULT 300,
        news_sources TEXT NOT NULL DEFAULT '{"financialjuice":true,"tree_news":true,"techflow":true,"eastmoney":true,"blockbeats":true,"jin10":true,"fred":true,"finnhub":true,"feishu_jin10":true}',
        news_trade_max_age_seconds INTEGER NOT NULL DEFAULT 2700,
        tp_enabled INTEGER NOT NULL DEFAULT 1,
        tp1_mult REAL NOT NULL DEFAULT 0.6,
        tp2_mult REAL NOT NULL DEFAULT 1.0,
        tp1_ratio REAL NOT NULL DEFAULT 0.5,
        tp2_ratio REAL NOT NULL DEFAULT 0.3,
        sl_mult REAL NOT NULL DEFAULT 0.5,
        max_position_pct REAL DEFAULT NULL,  -- v409.5: 单笔仓位上限(占权益)，NULL=不设限
        buy_min_score REAL NOT NULL DEFAULT 0.4,
        sell_min_score REAL NOT NULL DEFAULT 0.3,
        market_structure_enabled INTEGER NOT NULL DEFAULT 1,
        news_export_settings TEXT NOT NULL DEFAULT '{}',
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_CREATE_PAPER_TRADING_RUNS = """
    CREATE TABLE IF NOT EXISTS paper_trading_runs (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        tracks              TEXT    NOT NULL DEFAULT '[]',
        strategy_id         INTEGER NOT NULL,
        strategy_version_id INTEGER NOT NULL,
        agent_version       TEXT    NOT NULL,
        model_id            TEXT    NOT NULL,
        spec_version        TEXT    NOT NULL,
        activation_reason   TEXT    NOT NULL DEFAULT '',
        status              TEXT    NOT NULL DEFAULT 'RUNNING'
            CHECK (status IN ('RUNNING', 'STOPPED')),
        started_at          TEXT    NOT NULL DEFAULT (datetime('now')),
        stopped_at          TEXT,
        FOREIGN KEY (strategy_id) REFERENCES strategies(id),
        FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions(id)
    );
"""

_CREATE_QUICK_SIM_TRADES = """
    CREATE TABLE IF NOT EXISTS quick_sim_trades (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id         INTEGER NOT NULL UNIQUE,
        news_id             INTEGER,
        asset               TEXT    NOT NULL,
        action              TEXT    NOT NULL CHECK (action IN ('BUY', 'SELL')),
        score               REAL,
        evidence_confidence REAL,
        gate_passed         INTEGER NOT NULL DEFAULT 0,
        gate_reason         TEXT    NOT NULL DEFAULT '',
        strategy_id         INTEGER,
        strategy_version_id INTEGER,
        paper_trading_run_id INTEGER,
        notional_usdt       REAL    NOT NULL DEFAULT 0.0,
        entry_price         REAL    NOT NULL,
        entry_ts            INTEGER NOT NULL,
        horizon_minutes     INTEGER NOT NULL,
        exit_price          REAL,
        max_price           REAL,   -- 持仓期间最高价（过程质量，归因用事实反证）
        min_price           REAL,   -- 持仓期间最低价（过程质量，归因用事实反证）
        sl_price            REAL DEFAULT NULL,  -- v230+ P0.2 part B: TP1 后的追踪止损（breakeven-move）；NULL=沿用 entry×(1-sl_mult) 默认
        exit_ts             INTEGER,
        pnl_pct             REAL,
        pnl_usdt            REAL,
        verdict             TEXT    NOT NULL DEFAULT '',
        settled             INTEGER NOT NULL DEFAULT 0,
        created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
        mfe_pct             REAL    DEFAULT NULL,  -- 持仓期间最大浮盈（用于归因分析）
        mae_pct             REAL    DEFAULT NULL,  -- 持仓期间最大浮亏（用于归因分析）
        mfe_time_mins       REAL    DEFAULT NULL,  -- 到达 MFE 的耗时（分钟）—— 用于判定入场是否太早/太晚
        max_price_time      INTEGER DEFAULT 0,
        min_price_time      INTEGER DEFAULT 0,
        cost_bps            REAL    DEFAULT 0.0,   -- v301+ P0+: 本笔结算时扣减的交易成本（bps）
        code_fingerprint    TEXT    DEFAULT '',    -- v477: 产出该笔成交的代码代次（git短hash/参数hash）
        FOREIGN KEY (decision_id) REFERENCES ai_decisions(id) ON DELETE CASCADE
    );
"""

_CREATE_HERMES_OBSERVATIONS = """
    CREATE TABLE IF NOT EXISTS hermes_observations (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id          INTEGER,
        decision_id      INTEGER,
        ts               INTEGER NOT NULL,
        source           TEXT    NOT NULL DEFAULT '',
        asset            TEXT    NOT NULL DEFAULT 'NONE',
        observation_type TEXT    NOT NULL DEFAULT 'news',
        content          TEXT    NOT NULL DEFAULT '',
        payload          TEXT    NOT NULL DEFAULT '{}'
    );
"""

_CREATE_HERMES_SKILLS = """
    CREATE TABLE IF NOT EXISTS hermes_skills (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        skill_key        TEXT    NOT NULL UNIQUE,
        asset            TEXT    NOT NULL,
        action           TEXT    NOT NULL,
        prediction_type  TEXT    NOT NULL DEFAULT '',
        sample_size      INTEGER NOT NULL DEFAULT 0,
        wins             INTEGER NOT NULL DEFAULT 0,
        losses           INTEGER NOT NULL DEFAULT 0,
        win_rate         REAL,
        avg_pnl          REAL,
        note             TEXT    NOT NULL DEFAULT '',
        updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_CREATE_MACRO_SNAPSHOTS = """
    CREATE TABLE IF NOT EXISTS macro_snapshots (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         INTEGER NOT NULL,
        category   TEXT    NOT NULL,
        source     TEXT    NOT NULL DEFAULT '',
        metric_key TEXT    NOT NULL,
        value      REAL,
        unit       TEXT    NOT NULL DEFAULT '',
        status     TEXT    NOT NULL DEFAULT 'unavailable',
        payload    TEXT    NOT NULL DEFAULT '{}'
    );
"""

_CREATE_EQUITY_FUNDAMENTALS = """
    CREATE TABLE IF NOT EXISTS equity_fundamentals (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker            TEXT    NOT NULL,
        period_end        TEXT    NOT NULL,              -- 财报期末（YYYY-MM-DD）
        fiscal_year       TEXT    NOT NULL DEFAULT '',
        period            TEXT    NOT NULL DEFAULT '',   -- 'Q1'..'Q4' / 'FY'
        revenue           REAL,
        gross_profit      REAL,
        operating_income  REAL,
        net_income        REAL,
        eps_diluted       REAL,
        total_assets      REAL,
        total_liabilities REAL,
        total_equity      REAL,
        operating_cash_flow REAL,
        capex             REAL,
        source            TEXT    NOT NULL DEFAULT 'fmp',
        fetched_at        INTEGER NOT NULL DEFAULT 0,
        payload           TEXT    NOT NULL DEFAULT '{}',
        UNIQUE(ticker, period_end, source)
    );
"""

_CREATE_IDX_EQ_FUND = """
    CREATE INDEX IF NOT EXISTS idx_equity_fundamentals_ticker_period
        ON equity_fundamentals(ticker, period_end DESC);
"""

_CREATE_EQUITY_EARNINGS = """
    CREATE TABLE IF NOT EXISTS equity_earnings_events (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker            TEXT    NOT NULL,
        event_date        TEXT    NOT NULL,              -- YYYY-MM-DD（财报日，含未发布）
        eps_estimated     REAL,
        eps_actual        REAL,
        revenue_estimated REAL,
        revenue_actual    REAL,
        source            TEXT    NOT NULL DEFAULT 'fmp',
        fetched_at        INTEGER NOT NULL DEFAULT 0,
        last_updated      TEXT    NOT NULL DEFAULT '',
        UNIQUE(ticker, event_date)
    );
"""

_CREATE_IDX_EQ_EARNINGS = """
    CREATE INDEX IF NOT EXISTS idx_equity_earnings_date
        ON equity_earnings_events(event_date);
"""

_CREATE_CN_EQUITY_FUNDAMENTALS = """
    CREATE TABLE IF NOT EXISTS cn_equity_fundamentals (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker            TEXT    NOT NULL,               -- '600519.SH' / '00700.HK'
        market            TEXT    NOT NULL,               -- 'A' | 'HK'
        name              TEXT    NOT NULL DEFAULT '',
        industry          TEXT    NOT NULL DEFAULT '',
        report_date       TEXT    NOT NULL,               -- 报告期末 YYYY-MM-DD
        period_label      TEXT    NOT NULL DEFAULT '',    -- '三季报' / '2026年中报'
        eps_ttm           REAL,
        revenue_ttm       REAL,
        net_profit_ttm    REAL,
        revenue_yoy       REAL,                           -- 官方累计同比
        net_profit_yoy    REAL,
        roe               REAL,                           -- 当期（累计）加权ROE
        roe_annualized    REAL,                           -- 年化（A股用 4/季度数 折算）
        gross_margin      REAL,
        net_margin        REAL,
        bps               REAL,
        debt_asset_ratio  REAL,
        price             REAL,
        pe_ttm            REAL,                           -- price / EPS_TTM；亏损为 NULL
        pb                REAL,                           -- price / BPS
        source            TEXT    NOT NULL DEFAULT 'eastmoney',
        fetched_at        INTEGER NOT NULL DEFAULT 0,
        payload           TEXT    NOT NULL DEFAULT '{}',
        UNIQUE(ticker, market, report_date)
    );
"""

_CREATE_IDX_CN_EQ_FUND = """
    CREATE INDEX IF NOT EXISTS idx_cn_equity_fund_ticker
        ON cn_equity_fundamentals(ticker, market, report_date DESC);
"""

_CREATE_CN_VALUATION_SNAPSHOTS = """
    CREATE TABLE IF NOT EXISTS cn_valuation_snapshots (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker   TEXT    NOT NULL,
        market   TEXT    NOT NULL,
        ts       INTEGER NOT NULL,
        pe       REAL,
        pb       REAL,
        source   TEXT    NOT NULL DEFAULT 'eastmoney',
        UNIQUE(ticker, market, ts)
    );
"""

_CREATE_IDX_CN_VAL = """
    CREATE INDEX IF NOT EXISTS idx_cn_valuation_ticker_ts
        ON cn_valuation_snapshots(ticker, market, ts);
"""

_CREATE_KLINE_BARS = """
    CREATE TABLE IF NOT EXISTS kline_bars (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol     TEXT    NOT NULL,
        timeframe  TEXT    NOT NULL,               -- '1d' | '4h'（与 market_snapshot._MTF_PERIODS 对齐）
        ts         INTEGER NOT NULL,               -- 开盘时间 epoch ms
        open       REAL,
        high       REAL,
        low        REAL,
        close      REAL,
        volume     REAL,
        fetched_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(symbol, timeframe, ts)
    );
"""

_CREATE_KLINE_SNAPSHOTS = """
    CREATE TABLE IF NOT EXISTS kline_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol        TEXT NOT NULL,
        timeframe     TEXT NOT NULL,
        close         REAL,
        pos_pct_7d    REAL,                        -- 现价在近N日区间位置 0~1（<0.2低位/>0.8高位/0.4~0.6中性）
        range_high_7d REAL,
        range_low_7d  REAL,
        atr_pct       REAL,
        trend         TEXT,                        -- 趋势（复用 _compute_period_stats 判定）
        status        TEXT NOT NULL DEFAULT 'unavailable',  -- ok | unavailable
        payload       TEXT NOT NULL DEFAULT '{}',
        ts            INTEGER NOT NULL,
        UNIQUE(symbol, timeframe)
    );
"""

_CREATE_MARKET_STYLE_SNAPSHOT = """
    CREATE TABLE IF NOT EXISTS market_style_snapshot (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        asset           TEXT    NOT NULL DEFAULT '',      -- XAU/WTI/BTC/...
        regime          TEXT    NOT NULL DEFAULT 'unknown',  -- trending_up / trending_down / range_bound / volatile / crisis
        layer1_alerts   TEXT    NOT NULL DEFAULT '',      -- 价格结构 alert (JSON array)
        layer2_alerts   TEXT    NOT NULL DEFAULT '',      -- 资金流 alert
        layer3_alerts   TEXT    NOT NULL DEFAULT '',      -- 持仓结构 alert
        layer4_alerts   TEXT    NOT NULL DEFAULT '',      -- 宏观/利率 alert
        layer5_alerts   TEXT    NOT NULL DEFAULT '',      -- 地缘/事件 alert
        style_block     TEXT    NOT NULL DEFAULT '',      -- market_style_block 输出（注入 prompt 的整块）
        trigger         TEXT    NOT NULL DEFAULT '',      -- 本次变化的触发原因（"BTC 1h 跌穿 50EMA" 等）
        ts              INTEGER NOT NULL
    );
"""

_CREATE_DECISION_REFLECTION = """
    CREATE TABLE IF NOT EXISTS decision_reflection (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        asset           TEXT    NOT NULL DEFAULT '',      -- XAU/WTI/BTC/...
        window          TEXT    NOT NULL DEFAULT '1h',    -- 1h / 4h / 1d
        n_decisions     INTEGER NOT NULL DEFAULT 0,       -- 本窗口观察的决策条数
        win_n           INTEGER NOT NULL DEFAULT 0,       -- 真实"对"数（事后 K 线方向与决策一致）
        loss_n          INTEGER NOT NULL DEFAULT 0,
        flat_n          INTEGER NOT NULL DEFAULT 0,
        avg_pnl_pct     REAL    NOT NULL DEFAULT 0.0,
        bias_text       TEXT    NOT NULL DEFAULT '',      -- 反思结论（例：XAU 1h 窗口连 3 次 BUY 都错，下次起降一档）
        trigger         TEXT    NOT NULL DEFAULT '',      -- 触发原因（"3 连亏" / "胜率 < 30%" / "连续反向"）
        ts              INTEGER NOT NULL,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    );
"""

_CREATE_BACKTEST_RUNS = """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id    INTEGER NOT NULL,
        version_id     INTEGER NOT NULL,
        params         TEXT    NOT NULL DEFAULT '{}',
        result         TEXT    NOT NULL DEFAULT '{}',
        sample_size    INTEGER NOT NULL DEFAULT 0,
        taken          INTEGER NOT NULL DEFAULT 0,
        winrate        REAL,
        total_pnl_usdt REAL,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
        FOREIGN KEY (version_id) REFERENCES strategy_versions(id) ON DELETE CASCADE
    );
"""

_CREATE_REVIEW_REPORTS = """
    CREATE TABLE IF NOT EXISTS review_reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        scope       TEXT    NOT NULL,              -- today | week | all
        generated_at TEXT   NOT NULL DEFAULT (datetime('now')),
        summary     TEXT    NOT NULL DEFAULT '{}', -- 复盘总结 JSON
        failures    TEXT    NOT NULL DEFAULT '[]', -- 失败原因归类 JSON
        insights    TEXT    NOT NULL DEFAULT '[]', -- 沉淀的经验规则 JSON
        samples     INTEGER NOT NULL DEFAULT 0     -- 覆盖的交易笔数
    );
"""

_CREATE_REVIEW_INSIGHTS = """
    CREATE TABLE IF NOT EXISTS review_insights (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        rule       TEXT    NOT NULL,               -- 经验规则文本（注入 AI prompt）
        asset      TEXT    DEFAULT '',
        scope      TEXT    DEFAULT 'all',
        active     INTEGER NOT NULL DEFAULT 1,     -- 1=生效回灌 / 0=停用
        source_samples INTEGER DEFAULT 0,          -- 产生该经验时的复盘样本数（可靠性代理）
        origin     TEXT    DEFAULT 'real',         -- 来源：real=历史交易复盘 / quick_sim=快速评测提炼 / rule=归因规则
        kind            INTEGER NOT NULL DEFAULT 1, -- 0=硬铁律(强制，不参与胜率停用) / 1=软启发(可验证)
        counter_factual TEXT    DEFAULT '',          -- 反例：什么条件下该经验失效
        created_at TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_CREATE_STRUCTURED_REFLECTIONS = """
    CREATE TABLE IF NOT EXISTS structured_reflections (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id     INTEGER NOT NULL,
        news_id         INTEGER,
        asset           TEXT    NOT NULL DEFAULT '',
        event_type_code TEXT    NOT NULL DEFAULT '',
        verdict         TEXT    NOT NULL DEFAULT '',   -- fabricated / partial / ok
        score_coverage  REAL    NOT NULL DEFAULT 0.0,
        score_fabrication REAL  NOT NULL DEFAULT 0.0,
        entities_fabricated TEXT NOT NULL DEFAULT '[]', -- JSON list
        events_missing  TEXT    NOT NULL DEFAULT '[]',  -- JSON list
        status          TEXT    NOT NULL DEFAULT 'pending',  -- pending / promoted / failed / skipped
        retry_count     INTEGER NOT NULL DEFAULT 0,
        last_error      TEXT    NOT NULL DEFAULT '',
        promoted_insight_id INTEGER,                  -- review_insights.id when promoted
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(decision_id)                          -- 一个 decision 只入一次
    );
"""

_CREATE_NEWS_TRADE_REVIEWS = """
    CREATE TABLE IF NOT EXISTS news_trade_reviews (
        news_id      INTEGER PRIMARY KEY,            -- 关联 raw_news.id
        summary      TEXT    NOT NULL DEFAULT '{}',  -- AI 总结 JSON（含成功/失败原因）
        generated_at TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_CREATE_ATTRIBUTION_REVIEWS = """
    CREATE TABLE IF NOT EXISTS attribution_reviews (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        source         TEXT    NOT NULL,             -- news=新闻回溯归因 / review=复盘归因
        ref_id         INTEGER NOT NULL,             -- news_id 或 review_reports.id
        trade_id       INTEGER,                      -- quick_sim_trades.id 或 ai_decisions.id
        asset          TEXT    DEFAULT '',
        action         TEXT    DEFAULT '',
        verdict        TEXT    DEFAULT '',           -- WIN/LOSS
        pnl_pct        REAL,
        reason_code    INTEGER,
        reason_text    TEXT    DEFAULT '',
        confidence     REAL,
        human_status   TEXT    NOT NULL DEFAULT 'pending',  -- pending/agree/corrected
        corrected_code INTEGER,
        corrected_text TEXT    DEFAULT '',
        core_impact    TEXT    DEFAULT '',           -- 门卫四步核心影响（这条筋）
        reasons_json   TEXT    DEFAULT '',           -- 完整多原因归因数组(JSON)
        note           TEXT    DEFAULT '',
        rule_pushed    INTEGER NOT NULL DEFAULT 0,   -- 是否已回灌为 review_insights
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        reviewed_at    TEXT
    );
"""

_CREATE_FILTER_KEYWORDS = """
    CREATE TABLE IF NOT EXISTS filter_keywords (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        kind       TEXT    NOT NULL CHECK (kind IN ('junk', 'core')),
        keyword    TEXT    NOT NULL,
        builtin    INTEGER NOT NULL DEFAULT 0,   -- 1=内置种子（展示可停用）
        active     INTEGER NOT NULL DEFAULT 1,   -- 0=已停用（软删除）
        note       TEXT    NOT NULL DEFAULT '',
        created_at TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE (kind, keyword)
    );
"""

_CREATE_TRADE_REFLECTIONS = """
    CREATE TABLE IF NOT EXISTS trade_reflections (
        decision_id   INTEGER PRIMARY KEY,       -- 关联 ai_decisions.id
        news_id       INTEGER,
        asset         TEXT    NOT NULL DEFAULT '',
        action        TEXT    NOT NULL DEFAULT '',
        verdict       TEXT    NOT NULL DEFAULT '',
        pnl_pct       REAL,
        reason_code   INTEGER,
        reason_name   TEXT    NOT NULL DEFAULT '',
        reason_text   TEXT    NOT NULL DEFAULT '',
        confidence    REAL,
        status        TEXT    NOT NULL DEFAULT 'pending',  -- pending | done | failed
        created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        reflected_at  TEXT,
        retry_count   INTEGER NOT NULL DEFAULT 0,    -- LLM 失败重试次数（避免瞬时网络抖动永久丢失反思）
        last_error    TEXT    NOT NULL DEFAULT '',   -- 最近一次 LLM 失败原因
        last_error_at TEXT,                           -- 最近一次失败时间（用于限速重试）
        entry_accuracy REAL,                          -- v175: 买点识别准确性 0-1（反思评估维度）
        trend_fit      REAL                           -- v175: K线走势吻合度 0-1（反思评估维度）
    );
"""

_CREATE_EXPERIENCE_ENTRIES = """
    CREATE TABLE IF NOT EXISTS experience_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        applicable_assets TEXT DEFAULT '',
        applicable_kinds TEXT DEFAULT '',
        applicable_actors TEXT DEFAULT '',
        weight REAL DEFAULT 1.0,
        priority INTEGER DEFAULT 50,
        source TEXT DEFAULT 'manual',
        status TEXT DEFAULT 'active',
        apply_count INTEGER DEFAULT 0,
        hit_count INTEGER DEFAULT 0,
        last_applied_at TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
"""

_CREATE_EXPERIENCE_APPLICATIONS = """
    CREATE TABLE IF NOT EXISTS experience_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        experience_id INTEGER,
        decision_id INTEGER,
        applied_action TEXT,
        applied_at TEXT DEFAULT (datetime('now')),
        llm_invoked INTEGER DEFAULT 0,
        UNIQUE(experience_id, decision_id, applied_action)
    );
"""

_CREATE_GATE_REFLECTIONS_CYCLE = """
    CREATE TABLE IF NOT EXISTS gate_reflections_cycle (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        cycle_ts INTEGER NOT NULL,
        cycle_iso TEXT DEFAULT '',
        days INTEGER DEFAULT 3,
        precision REAL,
        pass_winrate REAL,
        n_judged INTEGER DEFAULT 0,
        n_decided INTEGER DEFAULT 0,
        n_win INTEGER DEFAULT 0,
        n_loss INTEGER DEFAULT 0,
        n_abnormal INTEGER DEFAULT 0,
        n_covered INTEGER DEFAULT 0,
        n_miss INTEGER DEFAULT 0,
        recall REAL,
        forward_json TEXT DEFAULT '[]',
        reverse_json TEXT DEFAULT '[]',
        summary_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(asset, cycle_ts)
    );
"""

_CREATE_TRADE_REFLECTIONS_V2 = """
    CREATE TABLE IF NOT EXISTS trade_reflections_v2 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER UNIQUE,
        paper_run_id INTEGER,
        asset TEXT,
        action TEXT,
        entry_price REAL,
        exit_price REAL,
        pnl_pct REAL,
        mfe_pct REAL,
        mae_pct REAL,
        settled_at TEXT,
        analysis_layer_scores TEXT,
        filter_layer_chain TEXT,
        reasoning_chain TEXT,
        counterfactual TEXT,
        error_layer TEXT,
        error_sub_layer TEXT,
        error_signal TEXT,
        root_cause TEXT,
        lesson TEXT,
        verification TEXT,
        applied_experience_ids TEXT DEFAULT '',
        depth_score INTEGER DEFAULT 0,
        llm_depth_reflection TEXT DEFAULT '',
        -- v309 P0-1: 三维度 (A/B/C) 归因列 — 口径见
        -- _TRADE_REFLECTIONS_V2_V309_DIMENSION_COLUMNS 注释。
        -- 新库直接建全; 老库由 migrate() 里的 _ensure_columns 幂等补齐。
        dimension          TEXT NOT NULL DEFAULT '',
        subcategory        TEXT NOT NULL DEFAULT '',
        hit                INTEGER NOT NULL DEFAULT 0,
        actual_outcome_pct REAL NOT NULL DEFAULT 0.0,
        -- v406: 生产库这一列是 NULL 允许的（pragma notnull=0），而这里的建表语句
        -- 写成了 NOT NULL DEFAULT 0.0 —— 新建库比生产库更严，fake_data.purge
        -- （把 0 占位清成 NULL 表示"这条是从没填过，不是打了 0 分"）在新建库上
        -- 直接 IntegrityError。以生产库为准，改为可空。
        confidence_at_time REAL,
        reflection_text    TEXT NOT NULL DEFAULT '',
        settled            INTEGER NOT NULL DEFAULT 0,

        -- v413: 反思来源与证据。
        -- origin: template(模板生成, 无归因) / llm(模型生成) / shadow(影子结算生成)
        --         / human(人工写入) / ''(v413 之前的历史行, 未分类)
        -- evidence: JSON, 至少要能回答「凭什么这么判」
        --         (asset / 决策或入场时刻 / 出场时刻 / 涨跌幅度 / 价格来源)。
        --         空串表示"没有证据"——这是可判定的缺失, 不是"没问题"。
        origin             TEXT NOT NULL DEFAULT '',
        evidence           TEXT NOT NULL DEFAULT '',

        -- v309 P1: created_at 落库格式统一为 **北京时区 + 'T' 分隔**
        -- ('YYYY-MM-DDTHH:MM:SS')。原为 DEFAULT (datetime('now')) ——
        -- 那是 **UTC + 空格分隔**, 与 paper_reflection_worker 写的
        -- 'T' 分隔本地时间**混在同一列** → 窗口过滤必然失真 (见 time_utils.py)。
        -- SQLite 的 datetime() 对无时区串按 UTC 解读, 故显式 +8 小时
        -- (中国无夏令时, 固定 +8 安全)。

        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '+8 hours')))
    );
"""

_CREATE_DECISION_LAYER_SIGNALS = """
    CREATE TABLE IF NOT EXISTS decision_layer_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        layer TEXT NOT NULL,
        sub_layer TEXT NOT NULL,
        signal_name TEXT NOT NULL,
        asset TEXT DEFAULT '*',
        action TEXT DEFAULT '*',
        n_total INTEGER DEFAULT 0,
        n_loss INTEGER DEFAULT 0,
        n_win INTEGER DEFAULT 0,
        last_seen_ts INTEGER DEFAULT 0,
        last_decision_id INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now')),
        UNIQUE(layer, sub_layer, signal_name, asset, action)
    );
"""

_CREATE_REVERSE_ATTRIBUTIONS = """
    CREATE TABLE IF NOT EXISTS reverse_attributions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        abnormal_ts INTEGER NOT NULL,
        abnormal_pct REAL,
        abnormal_kind TEXT DEFAULT '',
        related_decision_ids TEXT DEFAULT '',
        missed_signal_count INTEGER DEFAULT 0,
        forward_win_count INTEGER DEFAULT 0,
        forward_loss_count INTEGER DEFAULT 0,
        coverage_ratio REAL,
        detail_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(asset, abnormal_ts)
    );
"""

_CREATE_POLICY_PATCH_QUEUE = """
    CREATE TABLE IF NOT EXISTS policy_patch_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patch_type TEXT NOT NULL,        -- 'prompt_addition' | 'threshold_change' | 'rule_addition' | 'pattern_warning'
        target_module TEXT NOT NULL,     -- 'ai_worker' | 'risk_manager' | 'decision_prompt' | 'gate'
        patch_payload TEXT NOT NULL,     -- json.dumps({...具体补丁内容})
        patch_payload_hash TEXT NOT NULL,-- sha1(patch_payload) — 去重用
        source_kind TEXT NOT NULL,       -- 'error_code_pnl' | 'sibling_diff' | 'reflection_pattern' | 'llm_self_low'
        source_reflection_ids TEXT DEFAULT '[]',   -- json list of reflection ids
        source_error_codes  TEXT DEFAULT '[]',     -- json list of error codes
        sample_size INTEGER DEFAULT 0,            -- 支撑该候选的样本量
        cumulative_pnl_impact REAL DEFAULT 0.0,   -- 累计 pnl 影响 (负=亏)
        confidence_score REAL DEFAULT 0.0,         -- 0..1 自评置信度
        rationale TEXT DEFAULT '',                 -- 人类可读的"为什么提这个补丁"
        status TEXT NOT NULL DEFAULT 'pending'    -- 'pending' | 'approved' | 'rejected' | 'applied' | 'expired'
            CHECK (status IN ('pending','approved','rejected','applied','expired')),
        created_at INTEGER NOT NULL,
        reviewed_at INTEGER,
        reviewer_note TEXT DEFAULT '',
        applied_at INTEGER,
        expires_at INTEGER,                       -- 自动过期时间 (90d 后 expired 状态)
        supersedes_id INTEGER,                    -- 旧 patch 被新 patch 替换
        applied_count INTEGER DEFAULT 0,          -- 该补丁被 ai_worker / risk 注入的次数
        last_apply_at INTEGER,                    -- 最近一次注入时间
        UNIQUE(target_module, patch_payload_hash)
    );
"""

_CREATE_IDX_PPQ_STATUS = """
    CREATE INDEX IF NOT EXISTS idx_ppq_status_module
        ON policy_patch_queue(status, target_module, created_at DESC);
"""

_CREATE_IDX_PPQ_PENDING = """
    CREATE INDEX IF NOT EXISTS idx_ppq_pending_expires
        ON policy_patch_queue(status, expires_at)
        WHERE status IN ('pending','approved');
"""

_CREATE_SIBLING_DIFF_RULES = """
    CREATE TABLE IF NOT EXISTS sibling_diff_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_signature TEXT NOT NULL,    -- sha1(fingerprint + diff_features + asset)
        fingerprint TEXT NOT NULL,       -- content_fingerprint 同组的根
        asset TEXT NOT NULL,             -- 目标品种
        shared_features TEXT NOT NULL,    -- json: 兄弟决策共有的 feature vector
        winner_only_features TEXT DEFAULT '{}',  -- json: 赢家有输家没有的
        loser_only_features  TEXT DEFAULT '{}',  -- json: 输家有赢家没有的
        winner_decision_ids TEXT DEFAULT '[]',   -- json list
        loser_decision_ids  TEXT DEFAULT '[]',    -- json list
        n_pairs INTEGER DEFAULT 0,                 -- 兄弟对数
        consistency_score REAL DEFAULT 0.0,        -- 0..1, diff 重复出现的比例
        confidence_score REAL DEFAULT 0.0,        -- 自评: 0.4*consistency + 0.3*sample + 0.3*pnl_gap
        pnl_gap_avg REAL DEFAULT 0.0,             -- 赢家 - 输家的 pnl 差
        discovered_at INTEGER NOT NULL,
        last_seen_at INTEGER,                      -- 最近一次同 fingerprint 出现
        status TEXT NOT NULL DEFAULT 'candidate'  -- 'candidate' | 'promoted' | 'rejected'
            CHECK (status IN ('candidate','promoted','rejected')),
        promoted_at INTEGER,
        promoted_to TEXT DEFAULT '',              -- 'patch:id:42' 指向 policy_patch_queue 的某行
        UNIQUE(rule_signature)
    );
"""

_CREATE_IDX_SDR_STATUS = """
    CREATE INDEX IF NOT EXISTS idx_sdr_status_asset
        ON sibling_diff_rules(status, asset, discovered_at DESC);
"""

_CREATE_IDX_SDR_FINGERPRINT = """
    CREATE INDEX IF NOT EXISTS idx_sdr_fingerprint
        ON sibling_diff_rules(fingerprint, last_seen_at DESC);
"""

_CREATE_FEED_HEALTH = """
    CREATE TABLE IF NOT EXISTS feed_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        feed_kind TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        latency_ms INTEGER DEFAULT 0,
        value REAL,
        error TEXT,
        ts TEXT NOT NULL DEFAULT (datetime('now'))
    );
"""

_CREATE_IDX_FEED_HEALTH_ASSET_TS = (
    "CREATE INDEX IF NOT EXISTS idx_feed_health_asset_ts "
    "ON feed_health(asset, ts);"
)

_CREATE_IDX_FEED_HEALTH_KIND = (
    "CREATE INDEX IF NOT EXISTS idx_feed_health_kind "
    "ON feed_health(feed_kind);"
)

_CREATE_IDX_FEED_HEALTH_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_feed_health_status "
    "ON feed_health(status, ts);"
)

_CREATE_UNCLASSIFIED_EVENTS = """
    CREATE TABLE IF NOT EXISTS unclassified_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        news_id INTEGER DEFAULT 0,
        title TEXT DEFAULT '',
        source TEXT DEFAULT '',
        published_at TEXT DEFAULT '',
        target_asset TEXT DEFAULT '',
        llm_market_category TEXT DEFAULT '',
        llm_target_asset TEXT DEFAULT '',
        reason TEXT DEFAULT '',
        layer TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'resolved', 'dismissed')),
        resolved_at TEXT DEFAULT '',
        resolved_category TEXT DEFAULT '',
        resolved_target_asset TEXT DEFAULT '',
        resolver TEXT DEFAULT '',
        note TEXT DEFAULT ''
    );
"""

_CREATE_IDX_UNCLASSIFIED_PENDING = """
    CREATE INDEX IF NOT EXISTS idx_unclassified_pending
        ON unclassified_events(status, created_at DESC);
"""

_CREATE_IDX_UNCLASSIFIED_NEWS = """
    CREATE INDEX IF NOT EXISTS idx_unclassified_news
        ON unclassified_events(news_id);
"""

_CREATE_MISSED_SIGNAL_SCAN_STATE = """
    CREATE TABLE IF NOT EXISTS missed_signal_scan_state (
        asset TEXT PRIMARY KEY,
        last_decision_id INTEGER NOT NULL DEFAULT 0,
        last_scan_ts INTEGER NOT NULL DEFAULT 0,
        last_scan_count INTEGER NOT NULL DEFAULT 0,
        last_marked INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now'))
    )
"""

_CREATE_BACKTEST_DAILY_RUNS = """
    CREATE TABLE IF NOT EXISTS backtest_daily_runs (
        run_id TEXT PRIMARY KEY,
        asset_class TEXT NOT NULL DEFAULT 'all',
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        trigger TEXT DEFAULT 'auto_daily',
        decision_count INTEGER DEFAULT 0,
        archived INTEGER DEFAULT 0,
        note TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )
"""

_CREATE_REFLECTION_DAILY_JOURNAL = """
    CREATE TABLE IF NOT EXISTS reflection_daily_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        journal_date TEXT NOT NULL,                -- YYYY-MM-DD (本地日)
        run_id TEXT NOT NULL,
        n_decisions INTEGER DEFAULT 0,
        n_wins INTEGER DEFAULT 0,
        n_losses INTEGER DEFAULT 0,
        win_rate REAL,
        pnl_usdt REAL,
        ontology_breakdown TEXT DEFAULT '{}',       -- JSON: {narrative_bias: 5, ...}
        unknown_rate REAL,                         -- v188 unknown 兜底占比
        llm_invoked_count INTEGER DEFAULT 0,        -- llm_invoked=1 的应用次数
        top_experiences TEXT DEFAULT '',            -- 文本摘要 (top 5 应用经验)
        top_missed_signals TEXT DEFAULT '',         -- 文本摘要 (top 5 missed)
        reflections_summary TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(journal_date, run_id)
    )
"""

_CREATE_REFLECTION_CYCLE_LOG = """
    CREATE TABLE IF NOT EXISTS reflection_cycle_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL,
        run_id TEXT NOT NULL,
        reflection_id INTEGER,
        outcome TEXT DEFAULT 'pending',             -- pending / win / loss
        llm_invoked INTEGER DEFAULT 0,
        latency_ms INTEGER,
        ontology_signal TEXT DEFAULT '',
        error TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        -- v189-fix: 同一 decision+run_id 只允许一条, worker 重跑/锁失败重试不会重复写
        UNIQUE(decision_id, run_id)
    )
"""

_CREATE_EXPERIENCE_APPLICATION_DAILY = """
    CREATE TABLE IF NOT EXISTS experience_application_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        journal_date TEXT NOT NULL,
        run_id TEXT NOT NULL,
        experience_id INTEGER NOT NULL,
        n_applied INTEGER DEFAULT 0,
        n_llm_invoked INTEGER DEFAULT 0,
        applied_win_rate REAL,
        lift REAL,
        source TEXT DEFAULT 'paper_trading',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(journal_date, experience_id)
    )
"""

_CREATE_ERROR_CODE_LEDGER = """
    CREATE TABLE IF NOT EXISTS error_code_ledger (
        error_code          TEXT PRIMARY KEY,
        tier                TEXT NOT NULL DEFAULT 'research'
                            CHECK (tier IN ('hard','soft','research')),
        layer               TEXT NOT NULL DEFAULT '',
        first_seen_at       TEXT NOT NULL DEFAULT '',
        last_seen_at        TEXT NOT NULL DEFAULT '',
        triggers_total      INTEGER NOT NULL DEFAULT 0,   -- 累计触发次数 (decision_errors)
        triggers_30d        INTEGER NOT NULL DEFAULT 0,   -- 近 30 天触发
        confirmed_bad       INTEGER NOT NULL DEFAULT 0,   -- 触发后被结算证明为真错
        rejected_good       INTEGER NOT NULL DEFAULT 0,   -- 触发但该笔实际盈利 (误报)
        pnl_delta_pct       REAL,                          -- 触发组 vs 未触发组的均值差 (%)
        last_evaluated_at   TEXT NOT NULL DEFAULT '',
        note                TEXT NOT NULL DEFAULT ''
    );
"""

_CREATE_REFLECTION_CYCLES = """
    CREATE TABLE IF NOT EXISTS reflection_cycles (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_reflection_id INTEGER NOT NULL,         -- 父反思 ID (trade_reflections_v2.id 或上一级 cycle.id)
        child_reflection_id  INTEGER DEFAULT 0,        -- 子反思 ID (可能不存在: 仅产出 finding)
        parent_layer         TEXT NOT NULL DEFAULT '',-- 父反思的 (layer/sub_layer/signal)
        parent_signal        TEXT NOT NULL DEFAULT '',
        cycle_level          INTEGER NOT NULL DEFAULT 1,  -- 1=第一次元反思, 2=元元反思, ...
        finding_kind         TEXT NOT NULL DEFAULT '',-- confirmed / refined / invalidated / contradictory / lucky_or_skill
        finding_detail       TEXT NOT NULL DEFAULT '',-- 元反思结论文本
        evidence_json        TEXT NOT NULL DEFAULT '{}',
        lookback_days        INTEGER NOT NULL DEFAULT 7,
        source_kind          TEXT NOT NULL DEFAULT 'live',  -- v231+ watermark: 'live' | 'demo' | 'stress_test' | 'replay'
        source_run_id        TEXT NOT NULL DEFAULT '',      -- v231+ 触发本 cycle 的运行 ID (空=live)
        source_watermark     TEXT NOT NULL DEFAULT '',      -- v231+ HTTP 响应水印 (base64url, demo 才有)
        created_at           TEXT NOT NULL DEFAULT (datetime('now'))
    );
"""

_CREATE_KLINE_DECISION_CACHE = """
    CREATE TABLE IF NOT EXISTS kline_at_decision_cache (
        decision_id      INTEGER PRIMARY KEY,
        asset            TEXT NOT NULL DEFAULT '',
        decision_ts      INTEGER NOT NULL DEFAULT 0,
        ohlc_1h_json     TEXT NOT NULL DEFAULT '{}',
        ohlc_15m_json    TEXT NOT NULL DEFAULT '{}',
        ohlc_1d_json     TEXT NOT NULL DEFAULT '{}',
        atr_pct          REAL DEFAULT 0.0,
        pos_pct_7d       REAL DEFAULT NULL,    -- v300.5 P0+: 默认 NULL,不再 0.5 兜底 —— 0.5 会被读端误判为"价格中点"
        range_high_7d    REAL DEFAULT 0.0,
        range_low_7d     REAL DEFAULT 0.0,
        cross_asset_json TEXT NOT NULL DEFAULT '{}',
        pattern_label    TEXT NOT NULL DEFAULT 'unknown',
        pattern_detail   TEXT NOT NULL DEFAULT '',
        front_run_score  REAL DEFAULT 0.0,
        computed_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
"""

_CREATE_MONITOR_LLM_VALUE = """
    CREATE TABLE IF NOT EXISTS monitor_llm_value (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        window_days INTEGER NOT NULL,
        llm_n INTEGER NOT NULL DEFAULT 0,
        llm_win_rate REAL NOT NULL DEFAULT 0.0,
        llm_avg_pnl REAL NOT NULL DEFAULT 0.0,
        llm_avg_completeness REAL NOT NULL DEFAULT 0.0,
        not_llm_n INTEGER NOT NULL DEFAULT 0,
        not_llm_win_rate REAL NOT NULL DEFAULT 0.0,
        not_llm_avg_pnl REAL NOT NULL DEFAULT 0.0,
        not_llm_avg_completeness REAL NOT NULL DEFAULT 0.0,
        delta_win_rate REAL NOT NULL DEFAULT 0.0,
        delta_avg_pnl REAL NOT NULL DEFAULT 0.0,
        verdict TEXT NOT NULL DEFAULT 'insufficient_data',
            -- 'llm_positive_value' / 'llm_negative_value' / 'llm_neutral' / 'insufficient_data'
        action_recommended TEXT DEFAULT '',
        computed_at TEXT DEFAULT (datetime('now'))
    );
"""

_CREATE_AUDIT_LOG = """
    -- v483: R5 扩链/因果治理的动作审计。此前 causal_chain_expander 一直往这张
    -- 不存在的表 INSERT（只打一行 warning 就继续跑），"扩了什么链"没有任何留痕。
    CREATE TABLE IF NOT EXISTS audit_log (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        actor   TEXT NOT NULL DEFAULT '',
        action  TEXT NOT NULL DEFAULT '',
        target  TEXT NOT NULL DEFAULT '',
        detail  TEXT DEFAULT ''
    )
"""

_CREATE_TRADE_AUDIT_LOG = """
    CREATE TABLE IF NOT EXISTS trade_audit_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id   INTEGER NOT NULL,
        asset         TEXT NOT NULL DEFAULT '',
        symbol        TEXT NOT NULL DEFAULT '',
        side          TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL,                  -- ORDER_PLACED / ADAPTER_SKIPPED / RISK_REJECTED / ADAPTER_ERROR
        notional_usdt REAL    DEFAULT 0.0,
        quantity      REAL    DEFAULT 0.0,
        filled_price  REAL    DEFAULT 0.0,
        order_id      TEXT    DEFAULT '',
        adapter       TEXT    DEFAULT '',             -- mock / binance
        mode          TEXT    DEFAULT '',             -- off / paper / live_dry / live_real
        reason        TEXT    DEFAULT '',
        raw_json      TEXT    DEFAULT NULL,
        created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

_CREATE_DAILY_RESET_LOG = """
    CREATE TABLE IF NOT EXISTS daily_reset_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        reset_date    TEXT NOT NULL,
        reset_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        trigger       TEXT NOT NULL DEFAULT 'auto_daily'
            CHECK (trigger IN ('auto_daily', 'manual', 'startup', 'mode_change', 'kill_switch')),
        run_id        TEXT NOT NULL DEFAULT '',
        trade_mode    TEXT NOT NULL DEFAULT '',
        cleared_decisions   INTEGER DEFAULT 0,
        cleared_audit_rows  INTEGER DEFAULT 0,
        notes         TEXT DEFAULT '',
        created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

_CREATE_SIGNAL_CHAIN_ARCHIVE = """
    CREATE TABLE IF NOT EXISTS signal_chain_archive (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id       INTEGER NOT NULL UNIQUE,
        run_id            TEXT NOT NULL DEFAULT '',
        decision_ts       TEXT NOT NULL DEFAULT '',
        decision_date     TEXT NOT NULL DEFAULT '',
        news_id           INTEGER,
        news_published_at TEXT DEFAULT '',
        news_source       TEXT DEFAULT '',
        news_title        TEXT DEFAULT '',
        sentiment_score   REAL,
        target_asset      TEXT NOT NULL DEFAULT '',
        suggested_action  TEXT NOT NULL DEFAULT '',
        market_category   TEXT DEFAULT '',
        reasoning         TEXT DEFAULT '',
        agent_model_id    TEXT DEFAULT '',
        bias_after_reflection TEXT DEFAULT '',
        audit_status      TEXT DEFAULT '',
        audit_reason      TEXT DEFAULT '',
        order_id          TEXT DEFAULT '',
        symbol            TEXT DEFAULT '',
        side              TEXT DEFAULT '',
        notional_usdt     REAL DEFAULT 0.0,
        quantity          REAL DEFAULT 0.0,
        filled_price      REAL DEFAULT 0.0,
        entry_price       REAL,
        adapter           TEXT DEFAULT '',
        mode              TEXT DEFAULT '',
        is_correct        TEXT DEFAULT '',
        exit_price        REAL,
        forward_pnl       REAL,
        mfe_pct           REAL,
        mae_pct           REAL,
        settled_at        TEXT DEFAULT '',
        reflection_layer  TEXT DEFAULT '',
        reflection_signal TEXT DEFAULT '',
        experience_ids    TEXT DEFAULT '',
        raw_payload       TEXT DEFAULT NULL,
        created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

_CREATE_DAILY_REFLECTION_SUMMARY = """
    CREATE TABLE IF NOT EXISTS daily_reflection_summary (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        summary_date      TEXT NOT NULL,
        run_id            TEXT NOT NULL DEFAULT '',
        trade_mode        TEXT DEFAULT '',
        n_decisions       INTEGER DEFAULT 0,
        n_trades          INTEGER DEFAULT 0,
        n_skipped         INTEGER DEFAULT 0,
        n_risk_rejected   INTEGER DEFAULT 0,
        n_adapter_errors  INTEGER DEFAULT 0,
        n_pending         INTEGER DEFAULT 0,
        n_wins            INTEGER DEFAULT 0,
        n_losses          INTEGER DEFAULT 0,
        n_hold            INTEGER DEFAULT 0,
        n_unsettled       INTEGER DEFAULT 0,
        win_rate          REAL,
        total_pnl_usdt    REAL DEFAULT 0.0,
        avg_pnl_usdt      REAL DEFAULT 0.0,
        avg_mfe_pct       REAL,
        avg_mae_pct       REAL,
        max_drawdown_pct  REAL,
        asset_breakdown   TEXT DEFAULT '{}',
        action_breakdown  TEXT DEFAULT '{}',
        layer_breakdown   TEXT DEFAULT '{}',
        key_takeaways     TEXT DEFAULT '',
        lessons_learned   TEXT DEFAULT '',
        next_day_focus    TEXT DEFAULT '',
        reset_log_id      INTEGER,
        generated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        UNIQUE(summary_date, run_id)
    );
"""

_CREATE_STRATEGY_PARAM_ADJUSTMENTS = """
    CREATE TABLE IF NOT EXISTS strategy_param_adjustments (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        asset        TEXT    NOT NULL DEFAULT '*',     -- 资产类 / '*' = 全局
        action       TEXT    NOT NULL DEFAULT '*',     -- BUY/SELL/'*'
        param_key    TEXT    NOT NULL DEFAULT '',      -- 建议参数 key（buy_min_score/tp1_mult/...）
        direction    TEXT    NOT NULL DEFAULT '',      -- up / down / on / off
        magnitude    REAL    DEFAULT NULL,             -- 建议调整幅度 0~1
        sample_size  INTEGER NOT NULL DEFAULT 0,       -- 累计样本笔数
        avg_confidence REAL  DEFAULT NULL,             -- 平均置信度 0~1
        evidence_summary TEXT NOT NULL DEFAULT '',     -- 累计证据（最近 N 笔 rationale 拼接，最多 2KB）
        first_suggested_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_suggested_at  TEXT NOT NULL DEFAULT (datetime('now')),
        status       TEXT    NOT NULL DEFAULT 'pending'
            -- v339: 新增 'expired'（证据超过时效未审批，不再自动应用）。
            -- api_server 注释一直宣称有 expired，但 CHECK 从未允许 —— 文档与 schema 不一致。
            CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'expired')),
        applied_strategy_version_id INTEGER DEFAULT NULL,  -- 关联 add_version() 返回 id
        applied_setting_change TEXT DEFAULT '',            -- 关联 settings 改动（key=value,key=value）
        note         TEXT    NOT NULL DEFAULT '',            -- 审批备注 / 拒绝原因
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (asset, action, param_key, direction, status)
    );
"""

_CREATE_DECISION_EVIDENCE = """
    CREATE TABLE IF NOT EXISTS decision_evidence (
        decision_id   INTEGER NOT NULL,         -- 关联 ai_decisions.id
        news_id       INTEGER NOT NULL,         -- 关联 raw_news.id
        used_for      TEXT    NOT NULL DEFAULT 'context',  -- context | trigger | confirm | cross_check
        captured_at   INTEGER NOT NULL,         -- 接入时间 (epoch sec)
        published_at_snapshot TEXT NOT NULL DEFAULT '',  -- 接入时这条新闻的"原始发布时间"快照（防新闻后续被更新/删除时丢失）
        fetched_at_snapshot   INTEGER DEFAULT 0,         -- 接入时这条新闻的"我们抓取时间"快照
        content_hash_snapshot TEXT NOT NULL DEFAULT '',  -- 接入时的内容指纹（防篡改）
        PRIMARY KEY (decision_id, news_id, used_for)
    );
"""

_CREATE_SIGNAL_STATE = """
    CREATE TABLE IF NOT EXISTS signal_state (
        signal_id     TEXT    PRIMARY KEY,        -- 事件指纹：content_fingerprint / cause_signature
        asset         TEXT    NOT NULL DEFAULT '',  -- 主品种（XAU/BTC/...）
        impact        REAL    NOT NULL DEFAULT 0.0,  -- 初始影响强度（0.0-1.0）
        decay_lambda  REAL    NOT NULL DEFAULT 1.0e-4,  -- 指数衰减系数
        created_at    INTEGER NOT NULL,          -- 创建时刻（epoch sec）
        last_seen_at  INTEGER NOT NULL,          -- 最近一次观察到同指纹的时刻（用于去重 + 触发衰减刷新）
        ttl_seconds   INTEGER NOT NULL DEFAULT 86400,  -- 24h TTL；到期 purger 清掉
        direction     TEXT    NOT NULL DEFAULT 'NEUTRAL',  -- BULL | BEAR | NEUTRAL
        reason        TEXT    NOT NULL DEFAULT ''  -- 残留原因（一句话说明，方便人工审查）
    );
"""

_CREATE_AI_PROMPTS = """
    CREATE TABLE IF NOT EXISTS ai_prompts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL DEFAULT '',        -- 显示名
        content    TEXT    NOT NULL,                   -- prompt 内容（注入 AI 分析的指令）
        enabled    INTEGER NOT NULL DEFAULT 1,         -- 1=注入 0=停用
        kind       TEXT    NOT NULL DEFAULT 'custom',  -- 预留：custom / template
        created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

_CREATE_AI_MAIN_PROMPTS = """
    CREATE TABLE IF NOT EXISTS ai_main_prompts (
        kind       TEXT PRIMARY KEY,  -- system_prompt / json_force_prompt / market_structure_block / doubao_prompt
        name       TEXT NOT NULL DEFAULT '',
        content    TEXT NOT NULL,
        base_version INTEGER NOT NULL DEFAULT 0,  -- 保存时代码内置主 prompt 代际（落后即屏蔽该覆盖）
        updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

_CREATE_AI_ASSET_RULES = """
    CREATE TABLE IF NOT EXISTS ai_asset_rules (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        asset      TEXT    NOT NULL,                   -- 品种代码（XAU/WTI/BTC/ETH/NVDA…）或 all=全品种兜底
        asset_name TEXT    NOT NULL DEFAULT '',        -- 显示名（黄金/原油/比特币…，可选）
        content    TEXT    NOT NULL,                   -- 该品种专项分析规则（用户自由输入）
        enabled    INTEGER NOT NULL DEFAULT 1,         -- 1=生效 0=停用
        priority   INTEGER NOT NULL DEFAULT 0,         -- 同品种多条规则排序（小的在前）
        created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

_CREATE_CAUSAL_CHAINS = """
    CREATE TABLE IF NOT EXISTS causal_chains (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT    NOT NULL DEFAULT '',   -- 显示名（可选）
        cause         TEXT    NOT NULL,               -- 因：如"美元加息/降息"
        effect        TEXT    NOT NULL,               -- 果：如"黄金/原油/加密承压或走强"
        trigger_words TEXT    NOT NULL DEFAULT '',    -- 触发词（逗号/空格分隔）：新闻命中才注入
        note          TEXT    NOT NULL DEFAULT '',    -- 备注（可选）
        enabled       INTEGER NOT NULL DEFAULT 1,     -- 1=参与注入 0=停用
        origin        TEXT    NOT NULL DEFAULT 'manual',  -- manual=手工添加 / auto=复盘自动沉淀
        weight        REAL    NOT NULL DEFAULT 1,     -- 权重：越大在新闻分析中占比越大（命中排序优先+注入标注）
        mechanism     TEXT    NOT NULL DEFAULT '',    -- 传导机理：A 为什么会造成 B（中间物理/金融机理）
        precondition  TEXT    NOT NULL DEFAULT '',    -- 生效前提：什么条件下 A→B 成立
        invalidation  TEXT    NOT NULL DEFAULT '',    -- 失效边界：什么场景下 A→B 失效/反转
        created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

_CREATE_ASSET_DRIVER_NODES = """
    CREATE TABLE IF NOT EXISTS asset_driver_nodes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        asset       TEXT    NOT NULL DEFAULT 'all',   -- 归属品种（XAU/WTI/BTC/ETH...）或 all=跨品种宏观
        label       TEXT    NOT NULL,                 -- 因素/节点名（归一化后，用于同义合并）
        kind        TEXT    NOT NULL DEFAULT 'factor',-- asset=品种 / factor=因素 / core=核心驱动 / condition=条件因素
        is_core     INTEGER NOT NULL DEFAULT 0,       -- 1=核心因素（最底层驱动）
        confidence  REAL    NOT NULL DEFAULT 0.5,     -- 置信度（新数据证实上调/证伪下调）
        evidence    TEXT    NOT NULL DEFAULT '',      -- 支撑证据摘要
        version     INTEGER NOT NULL DEFAULT 1,       -- 演进版本（每次被重新推理 +1）
        active      INTEGER NOT NULL DEFAULT 1,       -- 0=已被证伪/失效
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        UNIQUE (asset, label)
    );
"""

_CREATE_ASSET_DRIVER_EDGES = """
    CREATE TABLE IF NOT EXISTS asset_driver_edges (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        from_id     INTEGER NOT NULL,                 -- 上游节点（因）
        to_id       INTEGER NOT NULL,                 -- 下游节点（果）
        kind        TEXT    NOT NULL DEFAULT 'causes',-- causes=因果 / conditions=条件触发 / supports=证实 / contradicts=证伪
        weight      REAL    NOT NULL DEFAULT 1.0,     -- 边权重（证据越多越强）
        evidence    TEXT    NOT NULL DEFAULT '',
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        UNIQUE (from_id, to_id, kind)
    );
"""

_CREATE_KG_IMPROVEMENTS = """
    CREATE TABLE IF NOT EXISTS kg_improvements (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        kind          TEXT    NOT NULL,                -- 改进动作：见 _KG_PROPOSAL_KINDS
        target        TEXT    NOT NULL DEFAULT '',     -- 操作目标定位串
        reason        TEXT    NOT NULL DEFAULT '',     -- 触发理由（LLM 摘要或人工备注）
        confidence    REAL    NOT NULL DEFAULT 0.0,    -- 0..1 自动挖掘的可信度
        sample_size   INTEGER NOT NULL DEFAULT 0,       -- 支撑样本数
        impact        TEXT    NOT NULL DEFAULT '',      -- 预期效果摘要
        payload       TEXT    NOT NULL DEFAULT '{}',   -- JSON：kind 需要的额外参数
        status        TEXT    NOT NULL DEFAULT 'pending'  -- pending | approved | rejected
            CHECK (status IN ('pending', 'approved', 'rejected')),
        source        TEXT    NOT NULL DEFAULT 'auto',  -- auto=触发标注 / manual=人工 / system=系统
        first_seen_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        last_seen_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        resolved_at   TEXT,                            -- 审批/驳回时间
        note          TEXT    NOT NULL DEFAULT '',     -- 审批人备注（驳回原因等）
        applied_at    TEXT,                            -- reconcile worker 应用时间（v302+）
        apply_error   TEXT    NOT NULL DEFAULT ''      -- reconcile worker 应用失败原因
    );
"""

_CREATE_IDX_KG_IMPROVEMENTS_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_kg_improvements_status "
    "ON kg_improvements(status, last_seen_at DESC);"
)

_CREATE_WORKER_HEARTBEAT = """
    CREATE TABLE IF NOT EXISTS worker_heartbeat (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        worker_name  TEXT    NOT NULL,                -- 'ingest' | 'ai' | 'reflection' | 'forward'
        last_tick_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        last_status  TEXT    NOT NULL DEFAULT 'ok'
            CHECK (last_status IN ('ok', 'busy', 'error')),
        last_error   TEXT    NOT NULL DEFAULT '',
        processed    INTEGER NOT NULL DEFAULT 0,      -- 本轮处理条目数（采样速率观察）
        detail       TEXT    NOT NULL DEFAULT '',     -- JSON：worker 自定义负载摘要
        UNIQUE (worker_name)
    );
"""

_CREATE_DLQ_EVENTS = """
    CREATE TABLE IF NOT EXISTS dlq_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT    NOT NULL,                -- 'ingest_parse' | 'reflection_retry_exhausted' | 'ai_unreachable' | ...
        source       TEXT    NOT NULL DEFAULT '',     -- 出处标识（'ingest:cointelegraph' / 'reflection:trade_reflections' 等）
        payload      TEXT    NOT NULL DEFAULT '',     -- 原 JSON 负载（截断 4KB 防止 DB 膨胀）
        last_error   TEXT    NOT NULL DEFAULT '',     -- 最后一次失败的错误文本
        retry_count  INTEGER NOT NULL DEFAULT 0,      -- 已经重试几次后才到 DLQ
        created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        resolved_at  TEXT,                           -- 人工 / 后续修复后再处理的时间
        resolved     INTEGER NOT NULL DEFAULT 0       -- 0=待处理，1=已恢复
    );
"""

_CREATE_ABNORMAL_SIGNALS = """
    CREATE TABLE IF NOT EXISTS abnormal_signals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          INTEGER NOT NULL,               -- 触发 unix 秒（时间锚）
        asset       TEXT    NOT NULL DEFAULT '',    -- 资产；全板块异动记 'SECTOR'
        signal_kind TEXT    NOT NULL DEFAULT '',    -- 'single' | 'multi_factor' | 'sector'
        direction   TEXT    NOT NULL DEFAULT '',    -- bullish | bearish | ''（未明）
        anchor_ts   INTEGER,                        -- 触发 K 线 ts_ms（single/multi 有，sector 为 NULL）
        detail_json TEXT    NOT NULL DEFAULT '{}',  -- 因子详情（factors/rsi/divergence/同向资产…）
        hint        TEXT    NOT NULL DEFAULT '',    -- 归因注入提示语（只在触发时生成）
        status      TEXT    NOT NULL DEFAULT 'open',-- open | attributed | dismissed
        created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

_CREATE_ABNORMAL_ATTRIBUTIONS = """
    CREATE TABLE IF NOT EXISTS abnormal_attributions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id       INTEGER NOT NULL,
        asset           TEXT    NOT NULL DEFAULT '',
        signal_kind     TEXT    NOT NULL DEFAULT '',
        anchor_ts       INTEGER,
        summary         TEXT    NOT NULL DEFAULT '',
        drivers_json    TEXT    NOT NULL DEFAULT '[]',
        cross_json      TEXT    NOT NULL DEFAULT '[]',
        actionable      TEXT    NOT NULL DEFAULT '观望',
        severity_hint   INTEGER NOT NULL DEFAULT 1,
        source_model    TEXT    NOT NULL DEFAULT '',
        used_news_n     INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (signal_id) REFERENCES abnormal_signals(id) ON DELETE CASCADE
    );
"""

_CREATE_IDX_ABN_ATTR_SIGNAL = "CREATE INDEX IF NOT EXISTS idx_abn_attr_signal ON abnormal_attributions(signal_id);"

_CREATE_IDX_ABN_ATTR_ASSET_TS = "CREATE INDEX IF NOT EXISTS idx_abn_attr_asset_ts ON abnormal_attributions(asset, anchor_ts);"

_CREATE_SOURCE_STATUS_CURRENT = """
    CREATE TABLE IF NOT EXISTS source_status (
        source_id         TEXT PRIMARY KEY,
        category          TEXT NOT NULL DEFAULT '',
        label             TEXT NOT NULL DEFAULT '',
        description       TEXT NOT NULL DEFAULT '',
        status            TEXT NOT NULL DEFAULT 'unknown'
            CHECK (status IN ('online', 'error', 'stale', 'unknown')),
        last_result       TEXT NOT NULL DEFAULT '',
        last_ok_ts        TEXT,
        last_fail_ts      TEXT,
        last_error        TEXT NOT NULL DEFAULT '',
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        total_ok          INTEGER NOT NULL DEFAULT 0,
        total_fail        INTEGER NOT NULL DEFAULT 0,
        last_latency_ms   REAL,
        last_tick_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        detail            TEXT NOT NULL DEFAULT ''
    );
"""

_CREATE_SOURCE_STATUS_HISTORY = """
    CREATE TABLE IF NOT EXISTS source_status_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id   TEXT NOT NULL,
        ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        ok          INTEGER NOT NULL,
        latency_ms  REAL,
        error       TEXT NOT NULL DEFAULT '',
        detail      TEXT NOT NULL DEFAULT ''
    );
"""

_CREATE_IDX_SOURCE_STATUS_HISTORY = """
    CREATE INDEX IF NOT EXISTS idx_source_status_history_ts
        ON source_status_history(source_id, ts);
"""

_CREATE_DIM_ASSET = """
    CREATE TABLE IF NOT EXISTS dim_asset (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        code        TEXT    NOT NULL UNIQUE,        -- 自然键：'BTC' / 'ETH' / 'XAU' / 'WTI' / 'SPX' / '000300.SS'
        name        TEXT    NOT NULL DEFAULT '',    -- 显示名：'Bitcoin' / '原油' / '沪深300'
        kind        TEXT    NOT NULL DEFAULT 'crypto'  -- crypto | commodity | equity_index | fx | bond
            CHECK (kind IN ('crypto','commodity','equity_index','fx','bond','other')),
        venue       TEXT    NOT NULL DEFAULT '',    -- 主交易场所：'Binance' / 'CME' / 'Sina'
        active      INTEGER NOT NULL DEFAULT 1,     -- 0=停用，1=启用
        created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

_CREATE_DIM_EVENT_TYPE = """
    CREATE TABLE IF NOT EXISTS dim_event_type (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        code            TEXT    NOT NULL UNIQUE,    -- 自然键：'rate_hike' / 'cpi' / 'nfp' / 'opec_cut'
        name_zh         TEXT    NOT NULL DEFAULT '',  -- 中文显示名：'加息'
        name_en         TEXT    NOT NULL DEFAULT '',  -- 英文：'Rate Hike'
        category        TEXT    NOT NULL DEFAULT 'macro'  -- macro | commodity | crypto | geopolitics
            CHECK (category IN ('macro','commodity','crypto','geopolitics','other')),
        -- 规则方向先验 JSON：{"XAU":"利空","BTC":"利空"} — 与 event_struct._DIRECTION_BIAS 对齐
        direction_bias  TEXT    NOT NULL DEFAULT '{}',
        active          INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

_CREATE_EVENT_ATOM = """
    CREATE TABLE IF NOT EXISTS event_atom (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_news_id             INTEGER NOT NULL,
        event_type_id           INTEGER NOT NULL,
        asset_id                INTEGER NOT NULL DEFAULT 0,
        event_time              INTEGER NOT NULL,
        event_time_confidence   REAL    NOT NULL DEFAULT 1.0,
        event_time_source       TEXT    NOT NULL DEFAULT 'platform_published_at',
        magnitude_value         REAL,
        magnitude_unit          TEXT    NOT NULL DEFAULT '',
        actor                   TEXT    NOT NULL DEFAULT '',
        direction               TEXT    NOT NULL DEFAULT 'unknown'
            CHECK (direction IN ('bullish','bearish','neutral','mixed','unknown')),
        direction_confidence    REAL    NOT NULL DEFAULT 0.0,
        direction_source        TEXT    NOT NULL DEFAULT 'rule_bias',
        evidence_count          INTEGER NOT NULL DEFAULT 1,
        parser_version          TEXT    NOT NULL DEFAULT 'event_struct.v1',
        cluster_id              INTEGER,
        reasoning               TEXT    NOT NULL DEFAULT '',
        created_at              INTEGER NOT NULL,
        UNIQUE (raw_news_id, event_type_id, event_time, asset_id)
    );
"""

_CREATE_EVENT_TIME_CANDIDATES = """
    CREATE TABLE IF NOT EXISTS event_time_candidates (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_news_id     INTEGER NOT NULL,
        candidate_ts    INTEGER NOT NULL,
        tz              TEXT    NOT NULL DEFAULT 'Asia/Shanghai',
        source          TEXT    NOT NULL DEFAULT ''
            CHECK (source IN ('platform_published_at','ai_inferred','cross_net','human_annotated','other',
                              'ai_worker_review','llm_fallback','backfill_v181')),
        confidence      REAL    NOT NULL DEFAULT 1.0,
        reasoning       TEXT    NOT NULL DEFAULT '',
        is_primary      INTEGER NOT NULL DEFAULT 0,
        parser_version  TEXT    NOT NULL DEFAULT 'event_struct.v1',
        created_at      INTEGER NOT NULL
    );
"""

_CREATE_EVENT_IMPACT_CURVE = """
    CREATE TABLE IF NOT EXISTS event_impact_curve (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        event_atom_id   INTEGER NOT NULL,
        asset_id        INTEGER NOT NULL DEFAULT 0,
        window_label    TEXT    NOT NULL
            CHECK (window_label IN ('5m','30m','2h','1d','extended')),
        sample_ts       INTEGER NOT NULL,           -- UTC epoch seconds
        price           REAL,                       -- 该时刻价格（可能为 null 仅存 delta）
        delta_pct       REAL,                       -- 该时刻价格变化百分比（保留 4 位小数）
        volume_ratio    REAL,                       -- 量能比（>1 放量）
        note            TEXT    NOT NULL DEFAULT '',
        created_at      INTEGER NOT NULL,
        UNIQUE (event_atom_id, asset_id, window_label, sample_ts)
    );
"""

_CREATE_COINGLASS_LSR_SAMPLES = """
    CREATE TABLE IF NOT EXISTS coinglass_lsr_samples (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              INTEGER NOT NULL,            -- 入库时刻 (epoch s)
        source_ts       REAL    NOT NULL,            -- 服务端 calculated_at (epoch s, 可为浮点)
        updated_at      REAL    NOT NULL,            -- 服务端 updated_at (epoch s)
        cache_age_s     REAL    NOT NULL DEFAULT 0,  -- 缓存新鲜度 (秒, <1s 为新鲜)
        mode            TEXT    NOT NULL,            -- 'trader' | 'whale'
        symbol          TEXT    NOT NULL,            -- 'BTC' | 'ETH' | 'SOL' (裸符号, 不用 BTCUSDT)
        rank            INTEGER NOT NULL DEFAULT 0,  -- 排行榜排名 (1~200+)
        ratio           REAL    NOT NULL DEFAULT 0,  -- 多/空持仓比
        ratio_pct       TEXT    NOT NULL DEFAULT '', -- 文本格式, 例 "+316.68%"
        ratio_30m       REAL    NOT NULL DEFAULT 0,  -- 30 分钟前 ratio (用于斜率)
        delta_2m        REAL    NOT NULL DEFAULT 0,
        delta_2m_pct    TEXT    NOT NULL DEFAULT '',
        delta_30m       REAL    NOT NULL DEFAULT 0,
        delta_30m_pct   TEXT    NOT NULL DEFAULT '',
        delta_4h        REAL    NOT NULL DEFAULT 0,
        delta_4h_pct    TEXT    NOT NULL DEFAULT '',
        oi_mcap_ratio   REAL    NOT NULL DEFAULT 0,
        ov_ratio        REAL    NOT NULL DEFAULT 0,
        ov_long_qty     REAL    NOT NULL DEFAULT 0,
        ov_short_qty    REAL    NOT NULL DEFAULT 0,
        ov_long_avg_entry  REAL NOT NULL DEFAULT 0,
        ov_short_avg_entry REAL NOT NULL DEFAULT 0,
        ov_long_whales_qty  REAL NOT NULL DEFAULT 0,
        ov_short_whales_qty REAL NOT NULL DEFAULT 0,
        ov_total_traders   INTEGER NOT NULL DEFAULT 0,
        ov_long_traders    INTEGER NOT NULL DEFAULT 0,
        ov_short_traders   INTEGER NOT NULL DEFAULT 0,
        ov_long_whales     INTEGER NOT NULL DEFAULT 0,
        ov_short_whales    INTEGER NOT NULL DEFAULT 0,
        ov_long_profit_traders  INTEGER NOT NULL DEFAULT 0,
        ov_short_profit_traders INTEGER NOT NULL DEFAULT 0,
        raw_payload     TEXT    NOT NULL DEFAULT '{}', -- 原始 JSON 备份 (调试用)
        UNIQUE (mode, symbol, source_ts)            -- 同币同源秒级幂等
    );
"""

_COAINGLASS_INDEX_NAME = "idx_coinglass_lsr_symbol_ts"

_COAINGLASS_INDEX_NAME_WHALE = "idx_coinglass_lsr_whale_symbol_ts"

_CREATE_COINGLASS_LSR_HEALTH = """
    CREATE TABLE IF NOT EXISTS coinglass_lsr_health (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              INTEGER NOT NULL,            -- 入库时刻 (epoch s)
        endpoint        TEXT    NOT NULL DEFAULT '', -- '/api/lsr/signal/BTC'
        ok              INTEGER NOT NULL DEFAULT 0,  -- 1=ok / 0=fail
        status          INTEGER NOT NULL DEFAULT 0,  -- HTTP 状态码
        latency_ms      INTEGER NOT NULL DEFAULT 0,  -- 本次拉取耗时
        rate_limit      INTEGER NOT NULL DEFAULT 0,  -- 配额 (X-Rate-Limit-Limit-User)
        rate_used       INTEGER NOT NULL DEFAULT 0,  -- 已用
        rate_remaining  INTEGER NOT NULL DEFAULT 0,  -- 剩余
        key_expires_at  TEXT    NOT NULL DEFAULT '', -- 过期日 (ISO8601)
        error_kind      TEXT    NOT NULL DEFAULT '', -- 错误种类 (rate_limited/transient/...)
        error_msg       TEXT    NOT NULL DEFAULT ''  -- 错误摘要
    );
"""

_CREATE_EVENT_IMPACT_STAT = """
    CREATE TABLE IF NOT EXISTS event_impact_stat (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        date_local          TEXT    NOT NULL,
        date_epoch_start    INTEGER NOT NULL,
        impact_curve_filled INTEGER NOT NULL DEFAULT 0,
        event_atoms_total   INTEGER NOT NULL DEFAULT 0,
        fallback_used       INTEGER NOT NULL DEFAULT 0,
        avg_window_minutes  REAL    NOT NULL DEFAULT 0,
        clock_jumps         INTEGER NOT NULL DEFAULT 0,
        created_at          INTEGER NOT NULL,
        UNIQUE (date_local)
    );
"""

_CREATE_STRATEGY_REFLECTIONS = """
    CREATE TABLE IF NOT EXISTS strategy_reflections (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        period_start TEXT   NOT NULL DEFAULT '',
        period_end  TEXT    NOT NULL DEFAULT '',
        period_days INTEGER NOT NULL DEFAULT 30,
        sample_size INTEGER NOT NULL DEFAULT 0,
        source_summary_json TEXT NOT NULL DEFAULT '',
        llm_summary         TEXT NOT NULL DEFAULT '',
        strategy_patterns_json TEXT NOT NULL DEFAULT '[]',
        recommendations_json   TEXT NOT NULL DEFAULT '[]',
        affected_dimension  TEXT DEFAULT '',
        evidence_summary    TEXT DEFAULT '',
        overall_confidence  REAL DEFAULT 0.5,
        status       TEXT    NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'expired')),
        admin_note          TEXT DEFAULT '',
        approved_at         TEXT DEFAULT '',
        applied_strategy_version_id INTEGER DEFAULT NULL,
        llm_model_id        TEXT DEFAULT '',
        UNIQUE (period_start, period_end)
    );
"""

_CREATE_EVENT_KLINE_IMPACT = """
    CREATE TABLE IF NOT EXISTS event_kline_impact (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        event_source    TEXT    NOT NULL DEFAULT '',
        event_id        TEXT    NOT NULL DEFAULT '',
        event_title     TEXT    NOT NULL DEFAULT '',
        event_time      INTEGER NOT NULL DEFAULT 0,
        importance      TEXT    DEFAULT '',
        asset           TEXT    NOT NULL DEFAULT '',
        pre_event_close REAL    DEFAULT NULL,
        max_high        REAL    DEFAULT NULL,
        min_low         REAL    DEFAULT NULL,
        impact_pct      REAL    NOT NULL DEFAULT 0.0,
        impact_window_start INTEGER NOT NULL DEFAULT 0,
        impact_window_end   INTEGER NOT NULL DEFAULT 0,
        impact_severity TEXT    NOT NULL DEFAULT 'low'
            CHECK (impact_severity IN ('low', 'medium', 'high')),
        direction       TEXT    DEFAULT ''
            CHECK (direction IN ('up', 'down', 'neutral')),
        processed_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE (event_source, event_id, asset)
    );
"""

_CREATE_MACRO_NOTIFY_STATE = """
    CREATE TABLE IF NOT EXISTS macro_notify_state (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id     TEXT    NOT NULL DEFAULT '',
        event_title  TEXT    NOT NULL DEFAULT '',
        event_time   INTEGER NOT NULL DEFAULT 0,  -- 秒级 UTC
        star         INTEGER NOT NULL DEFAULT 0,
        window       TEXT    NOT NULL DEFAULT 'lead_2h'
            CHECK (window IN ('lead_2h', 'at_time')),
        sent_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        channel      TEXT    NOT NULL DEFAULT 'all',  -- all / serverchan / mail / pushplus
        UNIQUE (event_id, window)
    );
"""

_CREATE_MACRO_CALENDAR = """
    CREATE TABLE IF NOT EXISTS macro_calendar (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        source       TEXT    NOT NULL DEFAULT 'jin10',  -- 'ff' | 'jin10'
        event_id     TEXT    NOT NULL DEFAULT '',
        country      TEXT    NOT NULL DEFAULT '',       -- FF: USD/EUR/CNY/JPY...; jin10: 国家名
        title        TEXT    NOT NULL DEFAULT '',
        impact       TEXT    NOT NULL DEFAULT 'low',     -- 'high' / 'med' / 'low' (FF 原始枚举；jin10 映射：star≥3→high, 2→med, 1→low)
        star         INTEGER NOT NULL DEFAULT 0,         -- 1-5 颗星 (v215+: FF impact 映射为 4/3/2; jin10 原值)
        event_time   INTEGER NOT NULL DEFAULT 0,         -- 秒级 UTC
        forecast     TEXT    NOT NULL DEFAULT '',
        previous     TEXT    NOT NULL DEFAULT '',
        actual       TEXT    NOT NULL DEFAULT '',
        fetched_at   TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE (source, event_id)
    );
"""

_CREATE_QUALITY_REJECTIONS = """
    CREATE TABLE IF NOT EXISTS quality_rejections (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id     INTEGER NOT NULL,
        rule            TEXT    NOT NULL,            -- 'R1' / 'R2' / 'R4' / 'R7' / 'R8' / 'R9'
        reason          TEXT    NOT NULL,            -- 完整 reason (e.g. 'R7:confidence=0.0<20.0')
        payload_json    TEXT    NOT NULL DEFAULT '{}',  -- decision 关键字段快照
        filter_version  TEXT    NOT NULL DEFAULT 'v231+',
        created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        UNIQUE(decision_id, rule)
    );
"""

_CREATE_CROWD_SOURCE_ITEMS = """
    CREATE TABLE IF NOT EXISTS crowd_source_items (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source          TEXT    NOT NULL,             -- baidu_hot / baidu_finance / ths_hot / coingecko / gnews
        external_id     TEXT    NOT NULL,             -- 源内唯一 id（去重键）
        item_kind       TEXT    NOT NULL DEFAULT 'theme',  -- asset | theme | news | coin
        title           TEXT    NOT NULL,
        body            TEXT    NOT NULL DEFAULT '',
        url             TEXT    NOT NULL DEFAULT '',
        media_channel   TEXT    NOT NULL DEFAULT '',  -- 媒体名 / 平台
        heat            REAL    NOT NULL DEFAULT 0,   -- 源给出的热度（或 rank 派生代理）
        rank_no         INTEGER NOT NULL DEFAULT 0,
        published_at    TEXT    NOT NULL DEFAULT '',
        collected_at    TEXT    NOT NULL,
        day             TEXT    NOT NULL,             -- YYYY-MM-DD
        month           TEXT    NOT NULL,             -- YYYY-MM
        topic_key       TEXT    NOT NULL,             -- 跨源话题归并键
        meta_json       TEXT    NOT NULL DEFAULT '{}',
        UNIQUE(source, external_id)
    );
"""

_CREATE_CROWD_TOPIC_DAILY = """
    CREATE TABLE IF NOT EXISTS crowd_topic_daily (
        topic_key       TEXT    NOT NULL,
        day             TEXT    NOT NULL,
        display_name    TEXT    NOT NULL,
        topic_kind      TEXT    NOT NULL DEFAULT 'theme',
        source          TEXT    NOT NULL,
        platform        TEXT    NOT NULL DEFAULT '',  -- 平台（baidu/ths/coingecko/gnews）—— 跨源共现按平台计
        heat            REAL    NOT NULL DEFAULT 0,
        rank_no         INTEGER NOT NULL DEFAULT 0,
        mentions        INTEGER NOT NULL DEFAULT 1,
        asset_class     TEXT    NOT NULL DEFAULT '',
        themes_json     TEXT    NOT NULL DEFAULT '[]',
        market          TEXT    NOT NULL DEFAULT '',
        sentiment       TEXT    NOT NULL DEFAULT '',
        time_window     TEXT    NOT NULL DEFAULT '',
        influence_tier  TEXT    NOT NULL DEFAULT '',
        media_channel   TEXT    NOT NULL DEFAULT '',
        media_tier      TEXT    NOT NULL DEFAULT '',
        PRIMARY KEY(topic_key, day, source)
    );
"""

_CREATE_CROWD_MONTHLY_RANKINGS = """
    CREATE TABLE IF NOT EXISTS crowd_monthly_rankings (
        month           TEXT    NOT NULL,             -- YYYY-MM
        topic_key       TEXT    NOT NULL,
        display_name    TEXT    NOT NULL,
        topic_kind      TEXT    NOT NULL DEFAULT 'theme',
        rank_no         INTEGER NOT NULL DEFAULT 0,
        authority_score REAL    NOT NULL DEFAULT 0,   -- 权威度（可解释，见 crowd_pulse.authority_explain）
        heat_total      REAL    NOT NULL DEFAULT 0,
        source_count    INTEGER NOT NULL DEFAULT 0,   -- 跨源共现数（权威度核心）
        days_active     INTEGER NOT NULL DEFAULT 0,
        momentum        REAL    NOT NULL DEFAULT 0,   -- 相对上月的热度变化
        media_weight    REAL    NOT NULL DEFAULT 0,
        sources_json    TEXT    NOT NULL DEFAULT '[]',
        explain         TEXT    NOT NULL DEFAULT '',  -- 人可读的权威度解释
        asset_class     TEXT    NOT NULL DEFAULT '',
        themes_json     TEXT    NOT NULL DEFAULT '[]',
        market          TEXT    NOT NULL DEFAULT '',
        PRIMARY KEY(month, topic_key)
    );
"""

_CREATE_CROWD_AUDIENCE_SEGMENTS = """
    CREATE TABLE IF NOT EXISTS crowd_audience_segments (
        month           TEXT    NOT NULL,
        dimension       TEXT    NOT NULL,   -- platform/market/asset_class/theme/sentiment/time_window/influence_tier/media_tier/media_channel
        segment         TEXT    NOT NULL,
        label           TEXT    NOT NULL DEFAULT '',
        item_count      INTEGER NOT NULL DEFAULT 0,
        heat_total      REAL    NOT NULL DEFAULT 0,
        share           REAL    NOT NULL DEFAULT 0,   -- 该分群在当月的热度占比
        rank_no         INTEGER NOT NULL DEFAULT 0,
        sample_sources  TEXT    NOT NULL DEFAULT '[]',
        PRIMARY KEY(month, dimension, segment)
    );
"""

_CREATE_CROWD_SOURCE_AUTHORITY = """
    CREATE TABLE IF NOT EXISTS crowd_source_authority (
        month           TEXT    NOT NULL,
        source          TEXT    NOT NULL,
        media_channel   TEXT    NOT NULL DEFAULT '',
        items           INTEGER NOT NULL DEFAULT 0,
        topics          INTEGER NOT NULL DEFAULT 0,
        cross_verified  INTEGER NOT NULL DEFAULT 0,   -- 该源的话题被其它源复现的总次数
        authority_score REAL    NOT NULL DEFAULT 0,
        PRIMARY KEY(month, source, media_channel)
    );
"""

_CREATE_CROWD_SOURCE_RUNS = """
    CREATE TABLE IF NOT EXISTS crowd_source_runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at      TEXT    NOT NULL,
        finished_at     TEXT    NOT NULL DEFAULT '',
        month           TEXT    NOT NULL,
        day             TEXT    NOT NULL,
        sources_json    TEXT    NOT NULL DEFAULT '{}',   -- 各源条数
        errors_json     TEXT    NOT NULL DEFAULT '{}',   -- 各源错误（如实记录，不静默）
        items_total     INTEGER NOT NULL DEFAULT 0,
        status          TEXT    NOT NULL DEFAULT 'ok'    -- ok / partial
    );
"""

_CREATE_GATE_CALIBRATION = """
    CREATE TABLE IF NOT EXISTS gate_calibration (
        asset        TEXT    NOT NULL,
        action       TEXT    NOT NULL,          -- BUY / SELL
        n_samples    INTEGER NOT NULL,          -- 参与统计的已结算决策数
        n_hits       INTEGER NOT NULL,          -- 方向命中数
        hit_rate     REAL    NOT NULL,          -- 命中率
        base_rate    REAL    NOT NULL,          -- 同方向基础概率 (随机基准)
        lift_pp      REAL    NOT NULL,          -- 提升 (百分点), = (hit-base)*100
        avg_abs_move REAL    NOT NULL DEFAULT 0,-- 平均绝对涨跌 (%)
        window_days  INTEGER NOT NULL DEFAULT 0,
        computed_at  INTEGER NOT NULL,
        applied      INTEGER NOT NULL DEFAULT 0, -- 1 = 已被闸门采纳为门槛覆盖
        note         TEXT    NOT NULL DEFAULT '',
        PRIMARY KEY (asset, action)
    )
"""

_CREATE_GATE_EVALUATIONS = """
    CREATE TABLE IF NOT EXISTS gate_evaluations (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        evaluated_at        INTEGER NOT NULL,           -- epoch 秒
        ai_decision_id      INTEGER UNIQUE,             -- 幂等键: 同一条决策只留一行
        news_id             INTEGER,
        asset               TEXT    NOT NULL DEFAULT '',
        action              TEXT    NOT NULL DEFAULT '',-- AI 建议 BUY/SELL/HOLD
        score               REAL,                       -- sentiment_score
        evidence_confidence REAL,
        evidence_action     TEXT,
        trade_gate_reason   TEXT,
        status              TEXT,
        passed              INTEGER NOT NULL DEFAULT 0, -- 1 = 通过全部闸门, 进候选
        reject_code         TEXT    NOT NULL DEFAULT '',-- 首个未通过的条件 (见 gate_audit)
        reject_detail       TEXT    NOT NULL DEFAULT '',
        context_json        TEXT    NOT NULL DEFAULT '{}'
    )
"""

_CREATE_DECISION_REF_ARCHIVE = """
    CREATE TABLE IF NOT EXISTS decision_ref_archive (
        decision_id    INTEGER PRIMARY KEY,   -- trade_decisions.id
        batch          TEXT    NOT NULL,       -- 重置批次 (时间戳)
        asset          TEXT    NOT NULL DEFAULT '',
        decision_ts    REAL,
        action         TEXT    NOT NULL DEFAULT '',
        news_id        INTEGER,
        ai_decision_id INTEGER,
        entry_price    REAL,
        outcome_pct    REAL,
        status         TEXT    NOT NULL DEFAULT '',
        created_at     INTEGER NOT NULL,
        note           TEXT    NOT NULL DEFAULT ''
    )
"""

_AI_DECISIONS_AUTOTS_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_ai_decisions_autots
AFTER INSERT ON ai_decisions
FOR EACH ROW
WHEN NEW.ts IS NULL
BEGIN
    -- SQLite BEFORE INSERT 触发器无法改 NEW.*, 必须用 AFTER INSERT + UPDATE 回写.
    -- 解析策略 (与 _backfill_ai_decisions_ts 对齐):
    --   1) created_at 含显式时区偏移 ('+'/'-'/'Z' 任一在日期后) → strftime 直接给 UTC 秒;
    --   2) 无时区串 → strftime 按 SQLite 本地 tz 解析再 -8*3600 矫正 (假设 Beijing);
    --   3) created_at 缺/过短 → 兜底用 NOW UTC 秒.
    UPDATE ai_decisions
       SET ts = CASE
           WHEN NEW.created_at IS NOT NULL
                AND length(NEW.created_at) >= 19
                AND (instr(NEW.created_at, '+') > 10
                     OR instr(NEW.created_at, '-') > 10
                     OR instr(NEW.created_at, 'Z') > 10) THEN
               CAST(strftime('%s', NEW.created_at) AS INTEGER)
           WHEN NEW.created_at IS NOT NULL AND length(NEW.created_at) >= 10 THEN
               CAST(strftime('%s', substr(NEW.created_at, 1, 19)) AS INTEGER) - 8 * 3600
           ELSE
               CAST(strftime('%s', 'now') AS INTEGER)
       END
     WHERE id = NEW.id;
END
"""

_CALL_STATS_SQL = (
    "INSERT INTO ai_call_stats (ts, purpose, key_alias, model_id,"
    " tokens_in, tokens_out, latency_ms, status, error_code, call_site,"
    " cached_tokens, error_body)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# v464(P0)：探针行硬拦截。11-19 事故：pytest 用例（test_backtest_stats /
# iso8601_strict）把 reasoning='test' 的 4 条假决策写进了生产库，进而被执行器成交。
# 直接写库的用例绕过 conftest 的临时库隔离，所以必须在 SQLite 内核层拦一道：
# 任何 reasoning 恰好为 'test' 的行静默丢弃（RAISE(IGNORE) 中止该行插入，不影响
# 同批次其他正常行）。
_AI_DECISIONS_GUARD_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_ai_decisions_reject_probe
BEFORE INSERT ON ai_decisions
FOR EACH ROW
WHEN lower(trim(COALESCE(NEW.reasoning, ''))) = 'test'
BEGIN
    SELECT RAISE(IGNORE);
END
"""

# v465(P0+): 拦截探针新闻行。11-19 12:15 事故：新增的 tests/test_write_guard.py
# 直连生产库 INSERT 了 4 条 source='test' / content='test news N' 的行，ai_worker
# 把它们当真实新闻认领 → 主分析得到"无标的白板"，全部降级 llm_empty → 决策产出 0。
# 与决策行同样的兜底思路：在 SQLite 内核层拦，任何测试直连写法都过不去。
_RAW_NEWS_PROBE_GUARD_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_raw_news_reject_probe
BEFORE INSERT ON raw_news
FOR EACH ROW
WHEN lower(trim(COALESCE(NEW.source, ''))) = 'test'
   OR lower(trim(COALESCE(NEW.content, ''))) LIKE 'test news%'
BEGIN
    SELECT RAISE(IGNORE);
END
"""

# v466(P0+): 拦截「孤儿成交」—— quick_sim_trades 的 decision_id 必须在 ai_decisions 里真实存在。
# 11-19 13:01 事故：库里又出现 decision_id=10009 的成交（ai_decisions 无此行、entry_price=100.0
# 探针价、exit=101 却记 pnl=-5 亏损），来源是测试脚本直连生产库。quick_sim.py 已有 Python 侧
# 校验（v453），但测试脚本绕过了它 —— 所以在内核层再拦一道：孤儿信号不建仓。
_QUICK_SIM_ORPHAN_GUARD_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_quick_sim_reject_orphan
BEFORE INSERT ON quick_sim_trades
FOR EACH ROW
WHEN NEW.decision_id IS NULL
   OR NOT EXISTS (SELECT 1 FROM ai_decisions WHERE id = NEW.decision_id)
BEGIN
    SELECT RAISE(IGNORE);
END
"""

_AI_DECISIONS_TRADE_UUID_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_ai_decisions_fill_trade_uuid
AFTER INSERT ON ai_decisions
FOR EACH ROW
WHEN NEW.trade_uuid IS NULL OR trim(NEW.trade_uuid) = ''
BEGIN
    UPDATE ai_decisions
       SET trade_uuid = lower(hex(randomblob(8)))
     WHERE id = NEW.id;
END
"""

_T2_ORDER_DEDUP_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_t2_orders_one_per_decision
BEFORE INSERT ON t2_orders
FOR EACH ROW
WHEN NEW.decision_id IS NOT NULL
   AND NEW.decision_id > 0
   AND EXISTS (SELECT 1 FROM t2_orders
               WHERE decision_id = NEW.decision_id
                 AND status IN ('filled','pending','partially_filled','working'))
BEGIN
    SELECT RAISE(IGNORE);
END
"""


