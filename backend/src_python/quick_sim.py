"""快速模拟交易评测通道。

把 ai_decisions 里所有方向性信号（BUY/SELL）按真实行情建仓，经过
QUICK_SIM_HORIZON_MINUTES 后按真实行情结算，用于盈利率评测。

与正式模拟盘的区别：
- 不使用多证据闸门与策略闸门作为准入条件，闸门结论只作为分组标记保存；
- 持仓周期短（QUICK_SIM_HORIZON_MINUTES 配置，默认 20 分钟），只服务于统计评测，不写 ai_decisions。

盈亏一律来自真实行情，取不到价格就不建仓、不结算，绝不补造数据。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
import db
from review import gate_audit
import risk_guard
from feeds import news_sources
from position_sizing import compute_notional

_PASSED_REASON = config.TRADE_GATE_PASSED_REASON  # v231+ 单一来源, 改 config 即可


def _resolve_window(
    hours: Optional[int],
    from_ts: Optional[int],
    to_ts: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    """把 hours / from_ts / to_ts 三种入口归一为 (lo, hi)（UTC 秒）。

    v180 P0+：与 /api/history/news-trades 同一口径。
    - from_ts / to_ts 都给 → 直接用
    - 只给 from_ts → hi=None（开区间到当前）
    - 只给 to_ts   → lo=None（开区间到历史起点）
    - 给 hours     → lo = now - hours*3600（秒），hi=None
    - 都不给       → (None, None)，调用方走全量
    """
    if from_ts is not None or to_ts is not None:
        return from_ts, to_ts
    if hours is not None and hours > 0:
        return int(time.time()) - hours * 3600, None
    return None, None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.row_factory = previous


def _trade_window_sec() -> int:
    """建仓窗口（秒）：与主分析 ai_worker 同一口径，后台 settings 优先，默认 45 分钟。"""
    try:
        from paper_trading import get_settings as _gs
        v = _gs().get("news_trade_max_age_seconds")
        if isinstance(v, (int, float)) and v >= 30:
            return int(v)
    except Exception:
        pass
    return int(config.NEWS_TRADE_MAX_AGE_MINUTES * 60)


def _news_age_seconds(row: Dict[str, Any]) -> Optional[float]:
    """新闻距现在的秒数；拿不到可靠时间返回 None（按新鲜放行，与主分析同口径）。

    优先用平台发布时间 published_at；缺失/解析失败再回落入库时间 ts（epoch）。
    """
    pub = str(row.get("published_at") or "").strip()
    dt = config.parse_time_to_bj(pub) if pub else None
    if dt is None:
        ts = row.get("news_ts")
        try:
            if ts is None:
                return None
            dt = datetime.fromtimestamp(float(ts), config.TZ_SHANGHAI)
        except Exception:
            return None
    return (datetime.now(config.TZ_SHANGHAI) - dt).total_seconds()


def _news_fresh(row: Dict[str, Any], window_sec: int) -> bool:
    age = _news_age_seconds(row)
    return age is None or age <= window_sec


def pending_signals(
    conn: sqlite3.Connection,
    limit: int = 50,
    min_created_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """尚未进入快速模拟的方向性信号（仅近 120 分钟，避免把历史旧信号一次性补建仓，
    导致评测统计失真）。

    min_created_at：重置模拟盘后的时间戳（UTC，与 created_at 同格式）。
    非空时只评测该时间之后产生的信号——重置后快速评测从零开始，
    不会被重置前保留的历史信号重新建仓结算。

    v231+ (2026-10-10): 在 SQL 层加 R1/R2/R4/R7/R9 预过滤, 直接从源头堵掉
    ABLATION §D 揭示的 24/24 笔不合约 trade. 详 sample_quality_filter.pre_trade_check.
    关闭: env QUICK_SIM_QUALITY_FILTER=off (紧急回滚).
    """
    # v231+: 默认开质量门, env 可关
    import os
    filter_on = (os.environ.get('QUICK_SIM_QUALITY_FILTER', 'on') or 'on').strip().lower() \
        not in ('0', 'off', 'false', 'no')
    # 回滚路径（质量门关闭）不启用"已尝试"去重，保持旧行为
    skip_attempted_sql = ""

    # v487 修（2026-11-19 全量测试抓到）：_gate_sel / _gate_where 必须定义在分支之外。
    # 原来只在 filter_on 分支里赋值，而回滚分支（QUICK_SIM_QUALITY_FILTER=off）的 SQL
    # 也引用 {_gate_sel} → 一走到回滚分支就 UnboundLocalError 直接崩，纸面台账静默停摆。
    # v483：老库/测试库可能还没迁移 gate_unavailable 列，SQL 不能直接引用。
    try:
        _has_gate_col = any(
            str(r[1]) == "gate_unavailable"
            for r in conn.execute("PRAGMA table_info(raw_news)")
        )
    except Exception:
        _has_gate_col = False
    _gate_sel = ("COALESCE(rn.gate_unavailable, 0) AS gate_unavailable, "
                 "rn.gate_verdict AS gate_verdict") if _has_gate_col else \
                "0 AS gate_unavailable, '' AS gate_verdict"
    _gate_where = ("\n                   AND COALESCE(rn.gate_unavailable, 0) = 0"
                   if _has_gate_col else "")

    if filter_on:
        # v231+ (C3): 阈值/白名单统一读 config.py, 不再硬编码
        # 任何改 EVIDENCE_CONFIDENCE_GATE / SIM_ALLOWED_ASSETS 后, 重启 backend 生效
        allowed = config.SIM_ALLOWED_ASSETS
        if not allowed:
            raise RuntimeError(
                "config.SIM_ALLOWED_ASSETS 不能为空, 至少 1 个资产; "
                "改 env SIM_ALLOWED_ASSETS= 重启 backend 即可"
            )
        in_placeholders = ",".join("?" for _ in allowed)
        # v453 P0-5：已经"尝试过"的决策不再重复入选。
        # 事故：一个 ai_decision_id 在 22:32–22:34 写出 59 行 trade_decisions
        # （全部 REJECTED / R8 兄弟决策>3）—— 因为原查询只排除"已建 quick_sim_trades"
        # 的决策，拒单不建仓 → 下一轮（约 30 秒后）又被选中，同一信号反复打点。
        # 这里连同"已打点但没成交"的决策一起排除：一次信号只尝试一次。
        # 关闭：env QUICK_SIM_SKIP_ATTEMPTED=0（回滚到旧行为）。
        _skip_attempted = (os.environ.get('QUICK_SIM_SKIP_ATTEMPTED', '1') or '1').strip().lower() \
            not in ('0', 'off', 'false', 'no')
        skip_attempted_sql = ""
        if _skip_attempted:
            try:
                _tabs = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except Exception:
                _tabs = set()
            if "trade_decisions" in _tabs:
                skip_attempted_sql = (
                    "\n                   AND NOT EXISTS ("
                    "\n                       SELECT 1 FROM trade_decisions td"
                    "\n                       WHERE td.ai_decision_id = ad.id"
                    "\n                   )"
                )
        # v231+ (H1): R9 改白名单. 现状 db schema CHECK 允许 5 个 status
        # ('UNREAD','APPROVED','REJECTED','REVIEWED','AUTO_APPROVED'), 其中
        # REVIEWED = 用户已查 dashboard (api_server.mark-decisions-read),
        # APPROVED = 用户手动批准, 都仍需 trade. 只 REJECTED 拦.
        # 显式列 4 个白名单的好处: 未来加新 status (EXPIRED/SETTLED/CANCELLED)
        # 必须主动加进白名单, code review 时就能发现, 不会默默漏过黑名单.
        tradable_statuses = ("UNREAD", "APPROVED", "REVIEWED", "AUTO_APPROVED")
        tradable_placeholders = ",".join("?" for _ in tradable_statuses)
        # R1: gate_reason == _PASSED_REASON
        # R2: evidence_action != 'HOLD'
        # R4: target_asset in config.SIM_ALLOWED_ASSETS (动态 IN)
        # R7: evidence_confidence >= config.EVIDENCE_CONFIDENCE_GATE
        # R9: status IN tradable_statuses (白名单, 显式列 4 个)
        sql = f"""SELECT ad.id, ad.news_id, UPPER(ad.target_asset) AS asset,
                        UPPER(ad.suggested_action) AS action, ad.sentiment_score AS score,
                        ad.evidence_confidence, ad.trade_gate_reason, ad.evidence_action,
                        ad.strategy_id, ad.strategy_version_id, ad.paper_trading_run_id,
                        ad.event_strength, ad.event_phase, ad.status,
                        ad.trade_uuid,
                        COALESCE(ad.analysis_degraded, 0) AS analysis_degraded,
                        ad.analysis_degraded_reason,
                        rn.source AS news_source,
                        rn.ts AS news_ts, rn.published_at AS published_at,
                        {_gate_sel}
                 FROM ai_decisions ad
                 LEFT JOIN raw_news rn ON rn.id = ad.news_id
                 LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
                 WHERE qs.id IS NULL{skip_attempted_sql}
                   AND UPPER(ad.suggested_action) IN ('BUY', 'SELL')
                   AND UPPER(ad.target_asset) NOT IN ('', 'NONE')
                   AND datetime(ad.created_at, 'localtime') > datetime('now', 'localtime', '-120 minutes')
                   AND (? IS NULL OR datetime(ad.created_at, 'localtime') >= datetime(?, 'localtime'))
                   AND ad.trade_gate_reason = ?
                   AND UPPER(COALESCE(ad.evidence_action, 'HOLD')) != 'HOLD'
                   AND UPPER(ad.target_asset) IN ({in_placeholders})
                   AND COALESCE(ad.evidence_confidence, 0) >= ?
                   AND UPPER(COALESCE(ad.status, '')) IN ({tradable_placeholders})
                   -- R11 (v396 P3): 分析降级路径的"方向"不是判断, 不许建仓
                   AND COALESCE(ad.analysis_degraded, 0) = 0
                   -- v483：门卫未判定(gate_unavailable=2)的新闻只做分析，不进纸面台账
                   {_gate_where}
                 ORDER BY ad.id DESC LIMIT ?"""
        params = (min_created_at, min_created_at,
                  _PASSED_REASON, *allowed, config.EVIDENCE_CONFIDENCE_GATE,
                  *tradable_statuses, limit)
    else:
        # 紧急回滚路径: 旧 SQL, 无质量门
        sql = f"""SELECT ad.id, ad.news_id, UPPER(ad.target_asset) AS asset,
                        UPPER(ad.suggested_action) AS action, ad.sentiment_score AS score,
                        ad.evidence_confidence, ad.trade_gate_reason, ad.strategy_id,
                        ad.strategy_version_id, ad.paper_trading_run_id,
                        ad.event_strength, ad.event_phase,
                        ad.trade_uuid,
                        rn.source AS news_source,
                        rn.ts AS news_ts, rn.published_at AS published_at,
                        {_gate_sel}
                 FROM ai_decisions ad
                 LEFT JOIN raw_news rn ON rn.id = ad.news_id
                 LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
                 WHERE qs.id IS NULL{skip_attempted_sql}
                   AND UPPER(ad.suggested_action) IN ('BUY', 'SELL')
                   AND UPPER(ad.target_asset) NOT IN ('', 'NONE')
                   AND datetime(ad.created_at, 'localtime') > datetime('now', 'localtime', '-120 minutes')
                   AND (? IS NULL OR datetime(ad.created_at, 'localtime') >= datetime(?, 'localtime'))
                   {_gate_where}
                 ORDER BY ad.id DESC LIMIT ?"""
        params = (min_created_at, min_created_at, limit)

    rows = _rows(conn, sql, params)
    # 新闻时效闸：超过建仓窗口的老信号不进模拟台账（与主分析 auto_trade 同口径）。
    # 拿不到可靠时间的行按新鲜放行，避免误杀。
    window = _trade_window_sec()
    return [r for r in rows if _news_fresh(r, window)]


def gate_sell_signal(signal: Dict[str, Any]) -> Tuple[bool, str]:
    """P0.5b：建仓前的 SELL 信号门槛。

    返回 (allowed, reason)：
      - allowed=True  → 正常建仓
      - allowed=False → 拒签，reason 是简短标签（high_strength / asset_BTC
                        / asset_WTI / disabled）
    BUY 永远放行。门槛关闭时也放行。
    """
    if not config.SELL_GATE_ENABLED:
        return True, "disabled"
    action = (signal.get("action") or "").upper()
    if action != "SELL":
        return True, "buy_passthrough"
    if config.SELL_GATE_BLOCK_HIGH_STRENGTH and (
        (signal.get("event_strength") or "").lower() == "high"
    ):
        return False, "high_strength"
    asset = (signal.get("asset") or "").upper()
    if asset in config.SELL_GATE_BLOCKED_ASSETS:
        return False, f"asset_{asset}"
    return True, "passed"


def _pick_horizon(signal: Dict[str, Any]) -> int:
    """v453：按事件强度 / 事件阶段给持有期分档（不再一律 20 分钟）。

    依据（实测 2026-11-18，16 笔全 20 分钟）：
      平均最大有利位移 0.183%，平均往返成本 0.182% —— 20 分钟窗口里
      位移刚够付手续费，等于结构性负期望。强事件/早期事件需要更长的
      发酵窗口才能覆盖成本。

    规则（可用 env 覆盖，不写死）：
      - 基础档：high→60 / medium→40 / low→20（`QUICK_SIM_HORIZON_TIERS`）
      - event_phase=early → ×2（趋势刚起）；late → ×0.5（已走完）
      - 最终夹在 [20, 240] 分钟
    """
    base = int(getattr(config, "QUICK_SIM_HORIZON_MINUTES", 20) or 20)
    tiers = getattr(config, "QUICK_SIM_HORIZON_TIERS", None) or {}
    strength = str(signal.get("event_strength") or "").strip().lower()
    try:
        h = int(tiers.get(strength, base))
    except (TypeError, ValueError):
        h = base
    if h <= 0:
        h = base

    phase = str(signal.get("event_phase") or "").strip().lower()
    try:
        if phase == "early":
            h = int(h * float(getattr(config, "QUICK_SIM_HORIZON_EARLY_MULT", 2.0)))
        elif phase == "late":
            h = int(h * float(getattr(config, "QUICK_SIM_HORIZON_LATE_MULT", 0.5)))
    except Exception:
        pass

    lo = int(getattr(config, "QUICK_SIM_HORIZON_MIN_BOUND", 20) or 20)
    hi = int(getattr(config, "QUICK_SIM_HORIZON_MAX_BOUND", 240) or 240)
    return max(lo, min(hi, int(h)))


def _median_mfe_from_bars(
    conn: sqlite3.Connection,
    asset: str,
    horizon_minutes: int,
    lookback_days: int,
) -> tuple[Optional[float], int]:
    """v479：用本地 K 线算「同期同资产的历史典型位移」，给成本闸做兜底。

    为什么需要它：成本闸原本只认**已结算交易**的 mfe_pct，而系统没有历史成交 →
    samples 恒为 0 → 一律走 `edge_unverified` 放行。实测 BTC/XAU/WTI/ETH 四个品种
    全部 samples=0，等于这条闸从来没真正判过一次。

    口径（故意保守，不放水）：
      · 取该资产 1m（缺则 5m）K 线，按 horizon 切成不重叠窗口；
      · 每个窗口以首根收盘价为假设入场价，分别算向上/向下最大位移；
      · 向上、向下各自取中位数，最后取**两者中较小的那个** —— 方向未知时，
        只承认"较弱方向的典型最好走法"，避免用最乐观的一侧给成本找理由。
    """
    tf = "1m"
    try:
        n1 = conn.execute(
            "SELECT COUNT(*) FROM kline_bars WHERE symbol=? AND timeframe='1m'",
            (str(asset).upper(),)).fetchone()
        if not n1 or int(n1[0]) < 50:
            tf = "5m"
        rows = conn.execute(
            "SELECT ts, low, high, close FROM kline_bars WHERE symbol=? AND timeframe=? "
            "AND close>0 ORDER BY ts", (str(asset).upper(), tf)).fetchall()
    except sqlite3.Error:
        return None, 0
    if not rows:
        return None, 0
    # 时间过滤（ts 可能是 ms 或 s）
    now_s = int(time.time())
    cutoff = now_s - int(lookback_days) * 86400
    def _to_s(v):
        v = int(v)
        return v // 1000 if v > 10_000_000_000 else v
    rows = [r for r in rows if _to_s(r[0]) >= cutoff] or rows
    bar_min = 1 if tf == "1m" else 5
    step = max(1, int(horizon_minutes) // bar_min)
    ups, downs = [], []
    for i in range(0, len(rows) - step, step):
        entry = float(rows[i][3])
        if entry <= 0:
            continue
        seg = rows[i:i + step]
        hi = max(float(x[2]) for x in seg if x[2] is not None)
        lo = min(float(x[1]) for x in seg if x[1] is not None)
        ups.append((hi - entry) / entry * 100.0)
        downs.append((entry - lo) / entry * 100.0)
    n = min(len(ups), len(downs))
    if n <= 0:
        return None, 0

    def _med(v):
        v = sorted(v)
        mid = len(v) // 2
        return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2.0

    return float(min(_med(ups), _med(downs))), n


def _median_mfe_at_events(
    conn: sqlite3.Connection,
    asset: str,
    horizon_minutes: int,
    lookback_days: int,
    min_samples: int,
) -> tuple[Optional[float], int]:
    """v479：**事件条件**位移 —— 只从「真有新闻的那一刻」起算持有期。

    为什么要区分：无条件的 20 分钟位移是「全时段平均」，里面大半是半夜无事的时间，
    用它去比成本会得出"永远别交易"的结论；而系统实际只在**有事件**时才下单。
    所以口径要跟交易场景对齐：以 raw_news 的 ts 为起点，量 20 分钟内涨/跌了多少。

    仍然保守：涨、跌两个方向各取中位数，取较小的那个（方向未定时只认弱侧）。
    """
    try:
        tf = "1m"
        n1 = conn.execute("SELECT COUNT(*) FROM kline_bars WHERE symbol=? AND timeframe='1m'",
                          (str(asset).upper(),)).fetchone()
        if not n1 or int(n1[0]) < 50:
            tf = "5m"
        bars = conn.execute(
            "SELECT ts, low, high, close FROM kline_bars WHERE symbol=? AND timeframe=? "
            "AND close>0 ORDER BY ts", (str(asset).upper(), tf)).fetchall()
        anchors = conn.execute(
            "SELECT DISTINCT ts FROM raw_news WHERE ts IS NOT NULL "
            "AND ts >= ?", (int(time.time()) - int(lookback_days) * 86400,)).fetchall()
    except sqlite3.Error:
        return None, 0
    if not bars or not anchors:
        return None, 0

    bar_ts = []
    for r in bars:
        v = int(r[0])
        bar_ts.append(v // 1000 if v > 10_000_000_000 else v)
    bar_min = 1 if tf == "1m" else 5
    span = max(1, int(horizon_minutes) // bar_min)
    ups, downs = [], []
    import bisect as _bis
    for (a_ts,) in anchors:
        i = _bis.bisect_left(bar_ts, int(a_ts))
        if i <= 0 or i >= len(bars) - span:
            continue
        entry = float(bars[i][3])
        if entry <= 0:
            continue
        seg = bars[i:i + span]
        hi = max(float(x[2]) for x in seg if x[2] is not None)
        lo = min(float(x[1]) for x in seg if x[1] is not None)
        ups.append((hi - entry) / entry * 100.0)
        downs.append((entry - lo) / entry * 100.0)
    n = min(len(ups), len(downs))
    if n < max(1, int(min_samples)):
        return None, n

    def _med(v):
        v = sorted(v)
        mid = len(v) // 2
        return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2.0

    return float(min(_med(ups), _med(downs))), n


def _median_mfe_pct(
    conn: sqlite3.Connection,
    asset: str,
    horizon_minutes: int,
    lookback_days: int,
    min_samples: int,
) -> tuple[Optional[float], int]:
    """同资产、同持有期的历史「最大有利位移」中位数（真实成交样本）。

    只用已结算且有 mfe_pct 的行；样本不足返回 (None, n) —— 由调用方 abstain，
    **绝不拿估计值/默认值顶上**（0 号铁律）。
    """
    try:
        rows = conn.execute(
            """SELECT mfe_pct FROM quick_sim_trades
               WHERE UPPER(asset) = UPPER(?)
                 AND horizon_minutes = ?
                 AND mfe_pct IS NOT NULL
                 AND settled = 1
                 AND created_at >= datetime('now', ?)
            """,
            (asset, int(horizon_minutes), f"-{int(lookback_days)} days"),
        ).fetchall()
    except Exception:
        return None, 0, "none"
    vals = sorted(float(r[0]) for r in rows if r and r[0] is not None)
    if len(vals) < max(1, int(min_samples)):
        # ① 优先「事件条件」口径（跟实际下单时机一致）
        ev_med, ev_n = _median_mfe_at_events(
            conn, asset, horizon_minutes, lookback_days, min_samples)
        if ev_med is not None:
            return float(ev_med), int(ev_n), "event"
        # ② 事件样本不够 → 退到全时段 K 线位移
        bar_med, bar_n = _median_mfe_from_bars(conn, asset, horizon_minutes, lookback_days)
        if bar_med is not None and bar_n >= max(1, int(min_samples)):
            return float(bar_med), int(bar_n), "unconditional"
        return None, len(vals), "none"
    mid = len(vals) // 2
    med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
    return float(med), len(vals), "realized"


def _cost_edge_allows(
    conn: sqlite3.Connection,
    asset: str,
    horizon_minutes: int,
) -> tuple[bool, str, Dict[str, Any]]:
    """v453：成本覆盖闸 —— 该资产在该持有期的中位位移必须 ≥ 倍数 × 往返成本。

    返回 (allowed, reason, meta)。
      - allowed=False 只在「样本够 + 位移确实不够」时发生；
      - 样本不足 → 放行并标 `edge_unverified`（不猜、不拦）。
    """
    if not bool(getattr(config, "QUICK_SIM_COST_EDGE_ENFORCE", True)):
        return True, "disabled", {}
    try:
        from cost_model import round_trip_cost_bps
        cost_bps = float(round_trip_cost_bps(asset) or 0.0)
    except Exception:
        return True, "cost_model_unavailable", {}
    if cost_bps <= 0:
        return True, "zero_cost", {}

    mult = float(getattr(config, "QUICK_SIM_COST_EDGE_MULT", 1.5) or 1.5)
    lookback = int(getattr(config, "QUICK_SIM_COST_EDGE_LOOKBACK_DAYS", 90) or 90)
    min_samples = int(getattr(config, "QUICK_SIM_COST_EDGE_MIN_SAMPLES", 6) or 6)
    need_pct = mult * cost_bps / 100.0  # bps → %
    med, n, basis = _median_mfe_pct(conn, asset, horizon_minutes, lookback, min_samples)
    meta = {
        "horizon_minutes": int(horizon_minutes),
        "cost_bps": round(cost_bps, 2),
        "need_mfe_pct": round(need_pct, 4),
        "median_mfe_pct": None if med is None else round(med, 4),
        "samples": n,
        "basis": basis,   # realized=真实成交 / event=新闻条件 / unconditional=全时段 / none=无样本
    }
    if med is None:
        return True, "edge_unverified", meta
    if med < need_pct:
        return False, f"cost_edge_insufficient({med:.3f}%<{need_pct:.3f}%)", meta
    return True, "edge_ok", meta


def open_trades(
    conn: sqlite3.Connection,
    price_fn: Callable[[str], Optional[float]],
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """给未建仓的方向性信号按真实行情建仓，返回新建仓明细。

    v230+：cycle 级安全护栏（P0.3 日亏熔断 + P0.4 事件窗口）检查，
    任一拒绝时整批信号跳过、不建仓。失败时降级放行，不阻断正常流程。
    """
    horizon = config.QUICK_SIM_HORIZON_MINUTES  # 兼容旧口径：信号没给强度时的兜底
    notional = config.QUICK_SIM_NOTIONAL_USDT
    # v409.5: 单笔仓位上限（占权益比例）—— `sizing.max_position_pct` 的真实消费点。
    # 抽成模块级函数才能单测（内联 try/except 测不到，且异常必须静默放行）。
    notional = _apply_position_cap(conn, notional)
    sized_meta: List[Dict[str, Any]] = []
    created: List[Dict[str, Any]] = []
    sell_gated: List[Dict[str, Any]] = []
    cost_edge_rejected: List[Dict[str, Any]] = []  # v453: 成本覆盖闸拒绝
    _pe_skip_reasons: List[Dict[str, Any]] = []  # v208: policy_engine 拒绝
    _quality_rejected: List[Dict[str, Any]] = []  # v231+: sample_quality_filter 拒绝
    risk_rejected: List[Dict[str, Any]] = []      # v343: risk_guard 拒绝
    price_cache: Dict[str, float] = {}
    # 单源开关只过滤模拟盘交易（含快速评测建仓），不影响新闻采集。
    news_settings = news_sources.get_settings(conn)
    # 重置时间戳：重置后只评测重置之后产生的信号，旧信号不再重新建仓。
    reset_row = conn.execute(
        "SELECT quick_sim_reset_at FROM paper_trading_settings WHERE id=1"
    ).fetchone()
    min_created_at = reset_row[0] if reset_row is not None and reset_row[0] else None

    # ── v230+: cycle 级安全护栏 ──────────────────────────────────────────
    # 任一拒绝 → 整批跳过；失败降级放行，避免 bug 影响正常交易。
    try:
        from safety_guards import evaluate_all
        decision = evaluate_all(conn)
    except Exception as exc:
        print(f"[SAFETY] guard check failed, falling through: "
              f"{type(exc).__name__}: {exc}", flush=True)
        decision = None
    if decision is not None and not decision.allow:
        print(f"[SAFETY] open_trades skipped this cycle: "
              f"guard={decision.guard} reason={decision.reason} "
              f"metadata={decision.metadata}", flush=True)
        conn.commit()
        return []

    # ── v231+ (H4): 拦截持久化 - 把最近 2h 被 quality_filter 拒掉的 decision 落库
    # 用于 C1 假阳性 A/B 决策 / 后续 forensic. off 路径自动跳过 (filter 关闭时返 0).
    # 注意: 下面 for 循环里 per-signal pre_trade_check 仍跑 (R8 需要 per-decision),
    # 这里 audit 是"全景快照", 不是替代 per-signal 检查.
    try:
        import sample_quality_filter as _sqf
        _audit_n = _sqf.audit_recent_rejections(conn, since_minutes=120)
        if _audit_n:
            print(f"[H4] audit_recent_rejections: 新增 {_audit_n} 条拦截记录", flush=True)
    except Exception as _exc_audit:
        print(f"[H4] audit_recent_rejections 失败, 不影响 open_trades 主流程: "
              f"{type(_exc_audit).__name__}: {_exc_audit}", flush=True)

    for signal in pending_signals(conn, limit, min_created_at=min_created_at):
        if not news_sources.source_enabled(signal.get("news_source") or "", news_settings):
            continue
        # ── v231+ (2026-10-10): R8 兄弟决策数预检 (DEEP_REFLECTION §5) ──
        # SQL 层已滤 R1/R2/R4/R7/R9, R8 因需 per-decision sibling count
        # 留在 Python 层做. SQL 已被 filter, 这里做最后一道防御.
        # v231+ (C4): QUICK_SIM_QUALITY_FILTER=off 时整个质量门关闭, 包括 Python R1-R9,
        # 与 SQL 路径行为一致, 让"off=回到 V8 之前"完整生效.
        try:
            import sample_quality_filter as _sqf
            if not _sqf.is_pre_trade_filter_enabled():
                _qc = {"is_valid": True, "reasons": [], "flags": {}}  # off 路径: 跳过 R1-R9
            else:
                _qc = _sqf.pre_trade_check(signal, conn=conn)
            if not _qc["is_valid"]:
                _quality_rejected.append({
                    "decision_id": signal["id"], "asset": signal["asset"],
                    "action": signal["action"], "reasons": _qc["reasons"],
                })
                # v231+: 决策打点 — QUALITY_FILTER 拒绝
                try:
                    import trade_decisions as _td
                    _td.log_decision(
                        conn, asset=signal["asset"],
                        intended_action=signal["action"],
                        final_action="REJECTED", decision_type="REJECT",
                        news_id=signal.get("news_id"),
                        event_strength=signal.get("event_strength"),
                        deny_reason=";".join(_qc["reasons"])[:200],
                        guard="QUALITY_FILTER",
                        ai_decision_id=signal.get("id"),
                        context={"score": signal.get("score"),
                                 "evidence_confidence": signal.get("evidence_confidence"),
                                 "flags": _qc["flags"]},
                    )
                except Exception as _exc3:
                    print(f"[trade_decisions] QUALITY_FILTER 打点失败: {_exc3}",
                          flush=True)
                continue
        except Exception as _exc_qc:
            # 质量门自身出错 → 兜底放行 (宁放过不误杀, 与 SELL_GATE 兜底策略一致)
            print(f"[QUALITY_FILTER] 预检异常, 兜底放行: {_exc_qc}", flush=True)
        # ── v208: policy_engine — per-asset 自定义规则（先于 SELL_GATE） ──
        # v230.1: 修复 adjusted_notional_pct 未实际生效的 bug（之前 ADJUST_SIZE_PCT 规则被静默忽略）
        _pd_adj_pct: float = 1.0
        _pd_rule_id: Optional[int] = None
        _pd_rule_type: Optional[str] = None
        try:
            import policy_engine as _pe
            _pd = _pe.evaluate(
                conn,
                asset=signal["asset"],
                action=signal["action"],
                context={
                    "notional_usdt": signal.get("notional_usdt"),
                    "evidence_confidence": signal.get("evidence_confidence"),
                    "event_strength": signal.get("event_strength"),
                },
            )
            if not _pd.allow:
                _pe_skip_reasons.append({
                    "decision_id": signal["id"], "asset": signal["asset"],
                    "action": signal["action"], "reason": _pd.reason,
                    "rule_type": _pd.rule_type, "rule_id": _pd.rule_id,
                })
                try:
                    import trade_decisions as _td
                    _td.log_decision(
                        conn, asset=signal["asset"],
                        intended_action=signal["action"],
                        final_action="REJECTED", decision_type="REJECT",
                        news_id=signal.get("news_id"),
                        event_strength=signal.get("event_strength"),
                        deny_reason=_pd.reason, guard="POLICY_ENGINE",
                        ai_decision_id=signal.get("id"),
                        context={"rule_id": _pd.rule_id,
                                 "rule_type": _pd.rule_type,
                                 "score": signal.get("score"),
                                 "evidence_confidence": signal.get("evidence_confidence")},
                    )
                except Exception as _exc2:
                    print(f"[trade_decisions] POLICY_ENGINE 打点失败: {_exc2}", flush=True)
                continue
            # 记录软调整（ADJUST_SIZE_PCT 等），下面 compute_notional 之后乘上去
            if _pd.adjusted_notional_pct and _pd.adjusted_notional_pct < 1.0:
                _pd_adj_pct = float(_pd.adjusted_notional_pct)
                _pd_rule_id = _pd.rule_id
                _pd_rule_type = _pd.rule_type
        except Exception as _exc:
            # policy_engine 调用失败不要阻断正常交易（兜底放行）
            print(f"[policy_engine] 调用失败，兜底放行: {_exc}", flush=True)
        # P0.5b：SELL 信号门槛——建仓前拒签高强度 / 黑名单资产。
        allowed, gate_reason = gate_sell_signal(signal)
        if not allowed:
            sell_gated.append({
                "decision_id": signal["id"], "asset": signal["asset"],
                "action": signal["action"],
                "event_strength": signal.get("event_strength"),
                "reason": gate_reason,
            })
            # ── v208: 决策打点 — SELL 门槛拒绝 ──
            try:
                import trade_decisions as _td
                _td.log_decision(
                    conn, asset=signal["asset"],
                    intended_action=signal["action"],
                    final_action="REJECTED", decision_type="REJECT",
                    news_id=signal.get("news_id"),
                    event_strength=signal.get("event_strength"),
                    deny_reason=gate_reason, guard="SELL_GATE",
                    ai_decision_id=signal.get("id"),
                    context={"score": signal.get("score"),
                             "evidence_confidence": signal.get("evidence_confidence")},
                )
            except Exception as _exc:
                print(f"[trade_decisions] SELL_GATE 打点失败: {_exc}", flush=True)
            continue
        # 同一资产只取一次行情（避免每条信号都打外部价格源，CPU/请求量大减）
        if signal["asset"] not in price_cache:
            price_cache[signal["asset"]] = price_fn(signal["asset"])
        price = price_cache[signal["asset"]]
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        # ── v453: 持有期分档 + 成本覆盖闸 ──────────────────────────────
        # 先按事件强度/阶段定 horizon（强事件/早期事件给更长窗口），
        # 再问一句：这个资产在这个窗口里的历史中位位移，盖得住往返成本吗？
        # 盖不住就不开仓（留痕），盖得住或没样本才继续。
        _h = _pick_horizon(signal)
        _edge_ok, _edge_reason, _edge_meta = _cost_edge_allows(conn, signal["asset"], _h)
        if not _edge_ok:
            cost_edge_rejected.append({
                "decision_id": signal["id"], "asset": signal["asset"],
                "action": signal["action"], "reason": _edge_reason,
                "meta": _edge_meta,
            })
            try:
                import trade_decisions as _td_ce
                _td_ce.log_decision(
                    conn, asset=signal["asset"], intended_action=signal["action"],
                    final_action="REJECTED", decision_type="REJECT",
                    news_id=signal.get("news_id"),
                    deny_reason=_edge_reason[:200], guard="COST_EDGE",
                    ai_decision_id=signal.get("id"),
                    context={"cost_edge": _edge_meta, "score": signal.get("score"),
                             "evidence_confidence": signal.get("evidence_confidence")},
                )
            except Exception as _exc_ce:
                print(f"[trade_decisions] COST_EDGE 打点失败: {_exc_ce}", flush=True)
            print(f"[COST_EDGE] 拒绝 {signal['asset']} {signal['action']} "
                  f"h={_h}m: {_edge_reason} meta={_edge_meta}", flush=True)
            continue
        # P0.1: 按 evidence_confidence 分层调整名义金额（默认开启；env 关掉）
        signal_notional, sizing_meta = compute_notional(signal, notional, conn)
        # v230.1: policy_engine ADJUST_SIZE_PCT 软调整 — 在 evidence 分层之后再乘
        if _pd_adj_pct < 1.0:
            pre_policy_notional = signal_notional
            signal_notional = round(signal_notional * _pd_adj_pct, 2)
            sizing_meta["policy_adjust_pct"] = _pd_adj_pct
            sizing_meta["policy_rule_id"] = _pd_rule_id
            sizing_meta["policy_rule_type"] = _pd_rule_type
            sizing_meta["pre_policy_notional"] = pre_policy_notional
        # ── v343: 风控闸（risk_manager 首次接入下单链路）──────────────
        # 之前 risk_manager.evaluate 写好了但 quick_sim 从未调用 = 无风控。
        # 拒绝要留痕，否则"为什么没成交"又是黑箱。
        _rg_allow, _rg_size, _rg_reason = risk_guard.check(
            conn, signal["asset"], signal["action"], signal_notional)
        if not _rg_allow:
            risk_rejected.append({
                "decision_id": signal["id"], "asset": signal["asset"],
                "action": signal["action"], "notional": signal_notional,
                "reason": _rg_reason,
            })
            try:
                import trade_decisions as _td_r
                _td_r.log_decision(
                    conn, asset=signal["asset"], intended_action=signal["action"],
                    final_action="HOLD", decision_type="ENTRY", news_id=signal.get("news_id"),
                    size_usdt=signal_notional, ai_decision_id=signal.get("id"),
                    guard=_rg_reason, context={"risk_reject": True},
                )
            except Exception as _exc_r:
                print(f"[RISK] 拒绝留痕失败: {_exc_r}", flush=True)
            print(f"[RISK] 拒绝 {signal['asset']} {signal['action']} "
                  f"notional={signal_notional}: {_rg_reason}", flush=True)
            continue
        if _rg_size < signal_notional:
            sizing_meta["risk_capped_from"] = signal_notional
            signal_notional = _rg_size

        sized_meta.append({
            "decision_id": signal["id"], "asset": signal["asset"],
            "action": signal["action"],
            "confidence": signal.get("evidence_confidence"),
            **sizing_meta,
            "notional": signal_notional,
        })
        reason = signal["trade_gate_reason"] or ""
        entry_ts = int(time.time())
        # v453: 建仓前先确认决策行真实存在 —— 实测生产库出现过 decision_id=10009
        # 的成交，而 ai_decisions 里根本没有 10009（入场价还是 100.0 这种探针价），
        # 这种行无法复核、会污染绩效，属于铁律禁止的假数据。宁可不建仓。
        if not conn.execute("SELECT 1 FROM ai_decisions WHERE id = ?",
                            (signal["id"],)).fetchone():
            print(f"[quick_sim] 跳过孤儿信号 decision_id={signal['id']} "
                  f"{signal['asset']} {signal['action']}（ai_decisions 无此行）", flush=True)
            continue
        # v483: trade_uuid 全链路打通 —— 之前只在 admin 接口手动 backfill，班次里
        # 从不写入，导致 ai_decisions/quick_sim_trades 的 trade_uuid 全为 NULL，
        # 决策→成交→反思 无法按 uuid 串起来。这里建仓时补：优先继承决策已有的，
        # 没有就生成一个并回写决策行（回写只在该列为空时发生，不覆盖）。
        _trade_uuid = (signal.get("trade_uuid") or "").strip()
        if not _trade_uuid:
            import uuid as _uuid_mod
            _trade_uuid = _uuid_mod.uuid4().hex[:16]
            try:
                conn.execute(
                    "UPDATE ai_decisions SET trade_uuid = ? "
                    "WHERE id = ? AND (trade_uuid IS NULL OR trade_uuid = '')",
                    (_trade_uuid, signal["id"]),
                )
            except Exception as _exc_u:
                print(f"[quick_sim] 回写 trade_uuid 失败 decision={signal['id']}: {_exc_u}",
                      flush=True)
        # v477: 成交行记代码指纹 —— 先继承决策行的（同一代代码）；
        # 决策还没打标时用当前进程指纹兜底，绝不写空（空等于"来路不明"）。
        _code_fp = ""
        try:
            _fp_row = conn.execute(
                "SELECT code_fingerprint FROM ai_decisions WHERE id = ?",
                (signal["id"],),
            ).fetchone()
            _code_fp = str((_fp_row[0] if _fp_row else "") or "").strip()
        except Exception:
            _code_fp = ""
        if not _code_fp:
            try:
                from code_fingerprint import fingerprint as _cfp_fp
                _code_fp = _cfp_fp()
            except Exception:
                _code_fp = "unknown"
        cur = conn.execute(
            """INSERT OR IGNORE INTO quick_sim_trades
               (decision_id, news_id, asset, action, score, evidence_confidence,
                gate_passed, gate_reason, strategy_id, strategy_version_id,
                paper_trading_run_id, notional_usdt, entry_price, entry_ts, horizon_minutes,
                max_price, min_price, trade_uuid, code_fingerprint)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal["id"], signal["news_id"], signal["asset"], signal["action"],
                signal["score"], signal["evidence_confidence"],
                int(reason == _PASSED_REASON), reason,
                signal["strategy_id"], signal["strategy_version_id"],
                signal["paper_trading_run_id"], signal_notional,
                float(price), entry_ts, _h,
                float(price), float(price), _trade_uuid, _code_fp,
            ),
        )
        # P0: 反向同步 ai_decisions — 让下游 (backtest/reflection/risk_manager) 能读到 entry 数据
        # 用 COALESCE 保护：若 ai_decisions 已有 entry_price 不覆盖（avoid missing 是 replay）。
        # 但 horizon_minutes / entry_time 必须每次刷新以匹配最新 quick_sim 配置。
        if cur.rowcount > 0:
            from datetime import datetime as _dt
            entry_iso = _dt.fromtimestamp(entry_ts).isoformat(timespec="seconds")
            conn.execute(
                """UPDATE ai_decisions
                   SET entry_price             = COALESCE(entry_price, ?),
                       entry_time              = COALESCE(NULLIF(entry_time, ''), ?),
                       holding_horizon_minutes = COALESCE(holding_horizon_minutes, ?)
                   WHERE id = ?""",
                (float(price), entry_iso, _h, signal["id"]),
            )
            # ── v208: 决策打点 — ENTRY 成功 ──
            try:
                import trade_decisions as _td
                _td.log_decision(
                    conn, asset=signal["asset"],
                    intended_action=signal["action"],
                    final_action=signal["action"], decision_type="ENTRY",
                    news_id=signal.get("news_id"),
                    event_strength=signal.get("event_strength"),
                    entry_price=float(price),
                    size_usdt=signal_notional,
                    ai_decision_id=signal.get("id"),
                    guard="NONE",
                    context={"horizon_minutes": _h,
                             "evidence_confidence": signal.get("evidence_confidence"),
                             "strategy_id": signal.get("strategy_id"),
                             "strategy_version_id": signal.get("strategy_version_id")},
                )
            except Exception as _exc:
                print(f"[trade_decisions] ENTRY 打点失败: {_exc}", flush=True)
        created.append({
            "decision_id": signal["id"], "asset": signal["asset"],
            "action": signal["action"], "entry_price": float(price),
            "notional": signal_notional,
        })
    if sell_gated:
        print(f"[SELL_GATE] {len(sell_gated)} SELL signal(s) rejected"
              f" this cycle: {sell_gated[:5]}{'...' if len(sell_gated) > 5 else ''}",
              flush=True)
    if _pe_skip_reasons:
        print(f"[POLICY_ENGINE] {len(_pe_skip_reasons)} signal(s) blocked"
              f" this cycle: {_pe_skip_reasons[:5]}{'...' if len(_pe_skip_reasons) > 5 else ''}",
              flush=True)
    if _quality_rejected:
        print(f"[QUALITY_FILTER] {len(_quality_rejected)} signal(s) blocked"
              f" this cycle: {_quality_rejected[:5]}{'...' if len(_quality_rejected) > 5 else ''}",
              flush=True)
    # v231+ (C4): 在返回 list 上挂 quality_rejected 属性, 避免破坏现有 caller 的
    # `for x in created` / `len(created)` 协议. 想看拒绝明细的 caller 读
    # `created.quality_rejected` (类型: List[Dict], 每条含 decision_id/asset/action/reasons)
    # v314 P0: 普通 list 挂不了属性 (既有实现被 except AttributeError 静默吞掉,
    # 导致 run_cycle 读到的 quality_rejected_count 恒为 0) → 改用 list 子类。
    created = _ListWithMeta(
        created,
        quality_rejected=_quality_rejected,
        quality_rejected_count=len(_quality_rejected),
        risk_rejected=risk_rejected,
        risk_rejected_count=len(risk_rejected),
        cost_edge_rejected=cost_edge_rejected,
        cost_edge_rejected_count=len(cost_edge_rejected),
    )
    if risk_rejected:
        print(f"[RISK] {len(risk_rejected)} signal(s) blocked by risk_guard "
              f"sample={risk_rejected[:3]}", flush=True)
    if cost_edge_rejected:
        print(f"[COST_EDGE] {len(cost_edge_rejected)} signal(s) blocked by "
              f"cost-edge gate sample={cost_edge_rejected[:3]}", flush=True)
    if sized_meta:
        # 只打样本前 5 条 + 聚合计数，避免日志洪水
        sample = sized_meta[:5]
        n_low = sum(1 for m in sized_meta if m["tier"] == "low")
        n_mid = sum(1 for m in sized_meta if m["tier"] == "medium")
        n_high = sum(1 for m in sized_meta if m["tier"] == "high")
        print(f"[POSITION_SIZING] {len(sized_meta)} signal(s) sized "
              f"(low={n_low} mid={n_mid} high={n_high}) "
              f"sample={sample}",
              flush=True)
    conn.commit()
    return created


def settle_trades(
    conn: sqlite3.Connection,
    price_fn: Callable[[str], Optional[float]],
) -> List[Dict[str, Any]]:
    """到期的快速模拟仓位按真实行情结算，返回结算明细。

    P0 改造：结算时同时反向回填 ai_decisions 表的结算字段
    （is_correct/settled/forward_pnl/exit_price/mfe_pct/mae_pct）。
    这样下游模块（reflection/risk_manager/backtest/causal_graph/
    trade_reflection_llm/gate_reflection）继续按既有 SQL 就能读到数据。

    v314 P0（2026-10-29 事故修复）：结算价必须是**到期时刻**的价。
      旧实现取「扫描当刻的现价」，exit_ts 也写成扫描时刻。结算链一旦停摆
      （实测被 cut_bleeding_trades 的 TypeError 掐断 8.85 天），一笔 20 分钟
      的仓位会被拖成 12742.9 分钟才结算，收益被放大到 ±50% 量级。
      现在：exit_price = db.price_at(due_ts)，exit_ts = due_ts；
      到期价取不到时，宽限期内用现价兜底，超宽限期则熔断废弃 (ABANDONED)。
    """
    now = int(time.time())
    grace_sec = int(getattr(config, "QUICK_SIM_SETTLE_GRACE_MINUTES", 60)) * 60
    due = _rows(
        conn,
        """SELECT id, decision_id, asset, action, entry_price, entry_ts,
                  horizon_minutes, notional_usdt, max_price, min_price
           FROM quick_sim_trades
           WHERE settled = 0 AND entry_ts + horizon_minutes * 60 <= ?""",
        (now,),
    )
    settled: List[Dict[str, Any]] = []
    price_cache: Dict[str, float] = {}
    stats: Dict[str, int] = {"due_price": 0, "live_price": 0,
                             "overdue": 0, "abandoned": 0, "deferred": 0}
    for trade in due:
        entry_ts = _to_epoch(trade["entry_ts"])
        due_ts = entry_ts + int(trade["horizon_minutes"] or 0) * 60
        overdue_sec = now - due_ts
        # ── v314 P0: 结算价取「到期时刻」的价, 不是扫描当刻的现价 ──
        exit_price = db.price_at(conn, trade["asset"], due_ts)
        exit_ts = due_ts
        price_source = "due_price"
        if exit_price is None:
            # v386 P0: 超宽限期还拿不到到期价 → 直接废弃。
            # 旧实现会退到"现价兜底", 而结算链停摆时"现价"是几小时后甚至第二天的价 ——
            # 一笔 20 分钟 horizon 的仓按 12 小时后的价结算, 得到的 -1.21% 同样不是
            # 这笔交易的真实结果, 一样会污染胜率/盈亏比。过期仓一律不进统计。
            if overdue_sec > grace_sec:
                stats["abandoned"] += 1
                _abandon_trade(conn, trade, now)
                continue
            # 到期价不可得 → 宽限期内允许用现价兜底 (此时 now≈due_ts, 误差可接受)
            if trade["asset"] not in price_cache:
                price_cache[trade["asset"]] = price_fn(trade["asset"])
            price = price_cache[trade["asset"]]
            if not isinstance(price, (int, float)) or price <= 0:
                stats["deferred"] += 1   # 留到下一轮, 不猜价
                continue
            exit_price = float(price)
            exit_ts = now
            price_source = "live_price"
        elif overdue_sec > grace_sec:
            # 结算链停摆过 —— 仍按到期价结算 (这才是正确的 horizon 收益), 但记账告警
            stats["overdue"] += 1
        stats[price_source] += 1
        entry = float(trade["entry_price"])
        pnl_pct = (exit_price - entry) / entry * 100
        if trade["action"] == "SELL":
            pnl_pct = -pnl_pct
        # ── v301+ P0+：显式交易成本（手续费 + 滑点，往返）──
        # 扣完再落库；cost_bps 列存"如果当时走 fee/slip 模型该扣多少 bps"，用于审计。
        from cost_model import apply_costs, round_trip_cost_bps
        cost_bps = round_trip_cost_bps(trade["asset"])
        pnl_pct_net = apply_costs(pnl_pct, trade["asset"], trade["action"])
        pnl_usdt = float(trade["notional_usdt"]) * pnl_pct_net / 100
        verdict = "WIN" if pnl_pct_net > 0 else "LOSS" if pnl_pct_net < 0 else "BREAKEVEN"

        # ── P0: 计算 mfe_pct / mae_pct（用 update_open_extremes 已跟踪的 max/min）──
        # 注意：SQL UPDATE 时 max_price = MAX(COALESCE(max_price, entry_price), exit_price)
        # 所以"结算后最终 max_price" = max(trade["max_price"], exit_price)（fallback 到 entry 再 exit）。
        # 必须用"更新后"的 max/min 计算，不能直接 SELECT 旧值。
        # v314 P0: 超期单的极值可能横跨整个停摆期 (update_open_extremes 每轮都刷新),
        # 与真实持仓期不符 → 收敛到 entry/exit, 不让停摆期的极值污染 MFE/MAE。
        if overdue_sec > grace_sec:
            final_max = max(entry, exit_price)
            final_min = min(entry, exit_price)
        else:
            final_max = exit_price if trade["max_price"] is None else max(trade["max_price"], exit_price)
            final_min = exit_price if trade["min_price"] is None else min(trade["min_price"], exit_price)
        if trade["action"] == "BUY":
            mfe_pct = (final_max - entry) / entry * 100 if entry > 0 else 0.0
            mae_pct = (entry - final_min) / entry * 100 if entry > 0 else 0.0
        else:  # SELL
            mfe_pct = (entry - final_min) / entry * 100 if entry > 0 else 0.0
            mae_pct = (final_max - entry) / entry * 100 if entry > 0 else 0.0
        mfe_pct = max(0.0, round(mfe_pct, 4))
        mae_pct = max(0.0, round(mae_pct, 4))

        conn.execute(
            """UPDATE quick_sim_trades
               SET exit_price=?, exit_ts=?, pnl_pct=?, pnl_usdt=?, verdict=?, settled=1,
                   mfe_pct=?, mae_pct=?, cost_bps=?, gross_pnl_pct=?,
                   max_price = MAX(COALESCE(max_price, entry_price), ?),
                   min_price = MIN(COALESCE(min_price, entry_price), ?)
               WHERE id=?""",
            (round(exit_price, 6), int(exit_ts), round(pnl_pct_net, 6), round(pnl_usdt, 6),
             verdict, mfe_pct, mae_pct, cost_bps, round(pnl_pct, 6),
             float(exit_price), float(exit_price), trade["id"]),
        )

        # ── P0: 反向回填 ai_decisions ──
        # 用 COALESCE 保护：若 ai_decisions 已有 is_correct/exit_price/mfe/mae 不覆盖
        # （避免 replay 阶段被覆盖回旧数据）。forward_pnl/settled 总是最新值。
        # v210.1: 同步 max_price/min_price，否则 analysis_process.list_recent 拿不到 MFE。
        if trade["decision_id"] is not None:
            conn.execute(
                """UPDATE ai_decisions
                   SET is_correct   = COALESCE(NULLIF(is_correct, ''), ?),
                       settled      = 1,
                       forward_pnl  = ?,
                       exit_price   = COALESCE(exit_price, ?),
                       max_price    = COALESCE(max_price, ?),
                       min_price    = COALESCE(min_price, ?),
                       mfe_pct      = COALESCE(mfe_pct, ?),
                       mae_pct      = COALESCE(mae_pct, ?)
                   WHERE id = ?""",
                (verdict, round(pnl_pct_net, 4), round(exit_price, 6),
                 final_max, final_min,
                 mfe_pct, mae_pct, trade["decision_id"]),
            )

        settled.append({
            "id": trade["id"], "decision_id": trade["decision_id"],
            "asset": trade["asset"], "action": trade["action"],
            "entry_price": entry, "exit_price": exit_price,
            "exit_ts": int(exit_ts), "price_source": price_source,
            "hold_minutes": round((int(exit_ts) - entry_ts) / 60.0, 1),
            "pnl_pct": round(pnl_pct_net, 4), "verdict": verdict,
            "mfe_pct": mfe_pct, "mae_pct": mae_pct,
            "cost_bps": cost_bps,
        })
    conn.commit()
    # v314 P0: 结算口径统计挂到返回值上 (run_cycle 据此打告警日志)
    return _ListWithMeta(settled, settle_stats=stats)


def _abandon_trade(conn: sqlite3.Connection, trade: Dict[str, Any], now_ts: int) -> None:
    """v314 P0: 熔断废弃 —— 既取不到到期价也取不到现价, 且已超结算宽限期。

    标记 settled=1 / verdict=ABANDONED / pnl=0，**不回填 ai_decisions**，
    于是该决策既不进「真实持仓战绩」的已结算口径，也不会伪造一个收益值。
    （旧实现里它会一直挂在 settled=0，直到某天用几天后的现价结算。）
    """
    conn.execute(
        """UPDATE quick_sim_trades
           SET settled = 1, exit_price = NULL, exit_ts = ?,
               pnl_pct = 0, pnl_usdt = 0, verdict = 'ABANDONED'
           WHERE id = ? AND settled = 0""",
        (int(now_ts), int(trade["id"])),
    )


class _ListWithMeta(list):
    """可挂元数据的 list 子类。

    v314 P0: 既有代码用 `created.quality_rejected = ...` 往 list 挂属性，
    但**普通 list 不支持属性赋值** → 抛 AttributeError 后被 `except AttributeError:
    pass` 静默吞掉 → run_cycle 里 `getattr(opened, 'quality_rejected_count', 0)`
    恒为 0，面板上的 QUALITY_FILTER 拦截数一直是假的。改用子类后正常。
    """

    def __init__(self, items=(), **meta):
        super().__init__(items)
        self.__dict__.update(meta)


def _to_epoch(value: Any) -> int:
    """v314 P0: 把 entry_ts 归一为 epoch 秒（容错 datetime 字符串）。

    quick_sim_trades.entry_ts 生产上一直是 int(time.time())；但历史/测试数据
    里存在 datetime 字符串写法，直接 int() 会抛 ValueError 把结算链打断
    —— 这正是 v313 事故里"一个 helper 抛错 → 整条结算链停摆"的同款陷阱。
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    try:
        from datetime import datetime as _dt, timezone as _tz
        dt = _dt.fromisoformat(s)
        if dt.tzinfo is None:
            # SQLite 的 datetime('now') 是 **UTC**；naive 字符串一律按 UTC 解析，
            # 否则本机时区(+08:00)会把 entry_ts 平移 8 小时 → 仓位被误判成
            # 「已过期 8 小时」→ 过期保护误伤正常仓位。
            dt = dt.replace(tzinfo=_tz.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def _is_expired(row: Dict[str, Any], now_ts: int) -> bool:
    """v314 P0: 仓位是否已过期（超过 horizon + 结算宽限期）。

    过期仓**不能**再用「现价」结算 —— 那是本次事故的根因（实测一笔 20 分钟的
    仓位被拖成 12742.9 分钟才结算，收益被放大到 ±50% 量级）。
    过期仓统一交给 settle_trades 按「到期时刻价」结算，或被熔断废弃。
    """
    entry_ts = _to_epoch(row.get("entry_ts"))
    if not entry_ts:
        return False
    try:
        horizon = int(row.get("horizon_minutes") or 0)
    except (TypeError, ValueError):
        horizon = 0
    grace_sec = int(getattr(config, "QUICK_SIM_SETTLE_GRACE_MINUTES", 60)) * 60
    return now_ts > entry_ts + horizon * 60 + grace_sec


def settle_figures(gross_pnl_pct: float, asset: str,
                   action: str) -> tuple[float, float, float]:
    """v482：所有结算路径的唯一"毛→净"入口。

    背景（2026-11-19 实测）：到期结算（settle_trades）扣了 16/30bp 成本并写
    `gross_pnl_pct`，但止损（_settle_sl）、止盈（_settle_tp2）、强制砍仓
    （cut_bleeding_trades）三条路径只写毛值、`cost_bps=0`、`gross_pnl_pct=NULL`。
    结果是**同一张 quick_sim_trades 里一半净收益、一半毛收益**，而反思层又直接
    拿这个 pnl 当"判断对错"的真值 —— 标签本身不可比。

    口径（与 cost_model 一致）：净 = 毛 − round_trip_cost_bps/100（1bp = 0.01%）。
    部分止盈（partial_pnl_pct）在库里仍是毛值，最终结算按 blended 一次性扣费，
    不重复计费（成本按百分比线性，加权后等价）。

    Returns:
        (net_pnl_pct, cost_bps, cost_pct)
    """
    try:
        from cost_model import apply_costs, round_trip_cost_bps
        cost_bps = float(round_trip_cost_bps(asset) or 0.0)
        net = float(apply_costs(float(gross_pnl_pct), asset, action))
    except Exception as exc:
        print(f"[quick_sim] cost model unavailable asset={asset}: {exc}", flush=True)
        return float(gross_pnl_pct), 0.0, 0.0
    return net, cost_bps, round(cost_bps / 100.0, 6)


def cut_bleeding_trades(
    conn: sqlite3.Connection,
    price_fn: Callable[[str], Optional[float]],
    max_loss_pct: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """v230+ P0.5：MAE-based soft stop-loss。

    经验观察（30 天 / 59 笔 quick_sim_trades）：

        ┌──────────┬────────────────┬────────────────┐
        │ action   │ verdict        │ max_mae_pct    │
        ├──────────┼────────────────┼────────────────┤
        │ BUY      │ WIN  n=20      │ 0.224 %        │
        │ BUY      │ LOSS n=16      │ 0.373 %        │
        │ SELL     │ WIN  n=10      │ 0.251 %        │
        │ SELL     │ LOSS n=12      │ 0.931 %        │
        └──────────┴────────────────┴────────────────┘

    max_mae_pct = 0.3 时，回放历史可砍 4 笔 LOSS（1 BUY + 3 SELL）、
    0 笔 WIN 误杀；累计浮亏回收约 0.96 个百分点。

    行为：
    1. 取所有 settled=0 的未平仓；
    2. 用当前行情计算单边浮亏（BUY 看 PAX，SELL 看 LIX）；
    3. 浮亏 > max_loss_pct → 以当前价记为当前价，强制 settled=1、
       verdict=LOSS，同步更新 ai_decisions。

    返回被砍的明细列表；max_loss_pct 为 None / 0 / 负 → 返回空。
    错误隔离：不抛异常；价格取不到 → 跳过该资产。
    """
    if max_loss_pct is None or max_loss_pct <= 0:
        return []
    # v313 P0 (根因修复): 必须用 _rows() 取行, 不能直接 conn.execute(...).fetchall()。
    # db.get_connection() 的 row_factory 是 None → 直接 fetchall 拿到的是 tuple,
    # 下面 row["asset"] 会抛 "TypeError: tuple indices must be integers or slices",
    # 而这个异常发生在 run_cycle 中间 —— 结算链 settle_trades 被整个掐断,
    # 于是 ai_decisions.forward_pnl 永远不更新 (实测 4572 条决策只有 7 条有真值),
    # 事前权重也拿不到真值去标定。其余函数 (settle_trades/check_tp_sl/
    # update_open_extremes) 都走 _rows(), 只有这里漏了。
    open_rows = _rows(
        conn,
        """SELECT id, decision_id, asset, action, entry_price, notional_usdt,
                  max_price, min_price, entry_ts, horizon_minutes
           FROM quick_sim_trades
           WHERE settled = 0""",
    )
    if not open_rows:
        return []
    price_cache: Dict[str, Optional[float]] = {}
    cut: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    for row in open_rows:
        # v314 P0: 过期保护 —— 过期仓不用现价砍 (否则用几天后的价结算 20 分钟的单)
        if _is_expired(row, now_ts):
            continue
        asset = row["asset"]
        if asset not in price_cache:
            price_cache[asset] = price_fn(asset)
        cur_price = price_cache[asset]
        if not isinstance(cur_price, (int, float)) or cur_price <= 0:
            continue
        entry = float(row["entry_price"])
        action = row["action"]
        if action == "BUY":
            loss_pct = (entry - cur_price) / entry * 100.0
            pnl_pct = (cur_price - entry) / entry * 100.0
        else:
            loss_pct = (cur_price - entry) / entry * 100.0
            pnl_pct = (entry - cur_price) / entry * 100.0
        if loss_pct < max_loss_pct:
            continue
        # v482：毛/净分开。触发条件仍看毛浮亏（早砍语义不变），落库写净收益。
        gross_pnl_pct = pnl_pct
        net_pnl_pct, cost_bps, _ = settle_figures(gross_pnl_pct, asset, action)
        verdict = "WIN" if net_pnl_pct > 0 else "LOSS" if net_pnl_pct < 0 else "BREAKEVEN"
        notional = float(row["notional_usdt"] or 0.0)
        pnl_usdt = notional * net_pnl_pct / 100.0 if notional else 0.0

        # v230.2 P0.2 平行修：MAE 用 update_open_extremes 已跟踪的极值，
        # 不用 cur_price（cur_price 只是触发条件，本轮内若 min_price/max_price
        # 更深，则实际 MAE 更大）。同 _settle_sl/_settle_tp2 的 final_* 算法。
        stored_max = row["max_price"]
        stored_min = row["min_price"]
        final_max = max(float(stored_max) if stored_max is not None else entry, cur_price)
        final_min = min(float(stored_min) if stored_min is not None else entry, cur_price)
        if action == "BUY":
            mae_pct = (entry - final_min) / entry * 100.0
        else:  # SELL
            mae_pct = (final_max - entry) / entry * 100.0
        mae_pct = round(max(0.0, mae_pct), 4)

        conn.execute(
            """UPDATE quick_sim_trades
               SET settled = 1,
                   exit_price = ?,
                   exit_ts = ?,
                   pnl_pct = ?,
                   pnl_usdt = ?,
                   verdict = ?,
                   mae_pct = ?,
                   cost_bps = ?,
                   gross_pnl_pct = ?,
                   max_price = MAX(COALESCE(max_price, entry_price), ?),
                   min_price = MIN(COALESCE(min_price, entry_price), ?)
               WHERE id = ? AND settled = 0""",
            (float(cur_price), now_ts, round(net_pnl_pct, 4),
             round(pnl_usdt, 6), verdict, mae_pct, cost_bps,
             round(gross_pnl_pct, 4),
             float(cur_price), float(cur_price), int(row["id"])),
        )
        if row["decision_id"] is not None:
            conn.execute(
                """UPDATE ai_decisions
                   SET is_correct  = COALESCE(NULLIF(is_correct, ''), ?),
                       settled     = 1,
                       forward_pnl = COALESCE(forward_pnl, ?),
                       exit_price  = COALESCE(NULLIF(exit_price, 0), ?),
                       max_price   = COALESCE(NULLIF(max_price, 0), ?),
                       min_price   = COALESCE(NULLIF(min_price, 0), ?),
                       mae_pct     = COALESCE(mae_pct, ?)
                   WHERE id = ?""",
                (verdict, round(net_pnl_pct, 4), float(cur_price),
                 final_max, final_min,
                 mae_pct, int(row["decision_id"])),
            )
        cut.append({
            "id": row["id"],
            "asset": asset,
            "action": action,
            "entry_price": entry,
            "exit_price": float(cur_price),
            "pnl_pct": round(net_pnl_pct, 4),
            "gross_pnl_pct": round(gross_pnl_pct, 4),
            "cost_bps": cost_bps,
            "pnl_usdt": round(pnl_usdt, 6),
            "loss_pct": round(loss_pct, 4),
            "mae_pct": mae_pct,
            "reason": f"mae>={max_loss_pct}%",
        })
        # ── v208: 决策打点 — EXIT 强制平仓（loss cap） ──
        try:
            import trade_decisions as _td
            _td.log_decision(
                conn, asset=asset, intended_action=action,
                final_action="CLOSE", decision_type="EXIT",
                news_id=None, entry_price=entry,
                size_usdt=notional if notional else None,
                ai_decision_id=row.get("decision_id"),
                quick_sim_trade_id=int(row["id"]),
                guard="MAX_LOSS",
                context={"exit_price": float(cur_price),
                         "pnl_pct": round(net_pnl_pct, 4),
                         "gross_pnl_pct": round(gross_pnl_pct, 4),
                         "cost_bps": cost_bps,
                         "mae_pct": mae_pct,
                         "reason": f"mae>={max_loss_pct}%"},
            )
        except Exception as _exc:
            print(f"[trade_decisions] EXIT(loss_cap) 打点失败: {_exc}", flush=True)
    if cut:
        conn.commit()
    return cut


def check_tp_sl(
    conn: sqlite3.Connection,
    price_fn: Callable[[str], Optional[float]],
) -> List[Dict[str, Any]]:
    """v230+ P0.2：TP/SL 强制结算（enforce hardened exits）。

    读 paper_trading_settings（tp_enabled + tp1/tp2_mult + sl_mult +
    tp1/tp2_ratio），对每个未平仓触发：
      1. 触发器用 max_price / min_price（每轮 update_open_extremes 累计）+ 当前价；
      2. **TP1 命中**（当前价 OR min/max 到达阈值）且该 trade partial 尚未发生
         → 在 ai_decisions 上写部分平仓（partial_exit_price/ratio/pnl_pct），
         trade 仍保留 settled=0 等 TP2 或 SL 触发（或到期）。
      3. **TP2 命中**（trade 已 TP1 过，或价格一步到位） → 全平：混合 pnl =
         partial_pnl_pct（已锁定的 TP1） + (1 - partial_exit_ratio) × tp2_pnl_pct，
         verdict=WIN。
      4. **SL 命中**（价格不利方向越界） → 全平：混合 pnl = partial_pnl_pct +
         (1 - partial_exit_ratio) × sl_pnl_pct，verdict=LOSS。
      5. 优先级：先判 SL，再判 TP1，再判 TP2。同一 tick 不可能既 hit SL 又
         hit TP1（方向相反）。

    全局开关：config.TP_SL_ENABLED=0 → 整个函数立刻 return []，便于回滚。
    运行级开关：paper_trading_settings.tp_enabled=0 → 跳过。
    返回本轮触发明细（含 TP1_partial / TP2_close / SL_close 三类）。
    """
    if not getattr(config, "TP_SL_ENABLED", True):
        return []
    settings_row = _rows(
        conn,
        "SELECT tp_enabled, tp1_mult, tp2_mult, tp1_ratio, tp2_ratio, "
        "sl_mult FROM paper_trading_settings WHERE id = 1",
    )
    if not settings_row:
        return []
    s = settings_row[0]
    if not s.get("tp_enabled"):
        return []
    tp1_mult = float(s.get("tp1_mult") or 0)
    tp2_mult = float(s.get("tp2_mult") or 0)
    tp1_ratio = float(s.get("tp1_ratio") or 0)
    tp2_ratio = float(s.get("tp2_ratio") or 0)
    sl_mult = float(s.get("sl_mult") or 0)
    if tp1_mult <= 0 or tp2_mult <= 0 or sl_mult <= 0:
        return []
    if tp1_ratio <= 0 or tp2_ratio <= 0:
        return []
    if tp1_ratio + tp2_ratio > 1.0:
        return []
    open_rows = _rows(
        conn,
        """SELECT t.id, t.decision_id, t.asset, t.action, t.entry_price,
                  t.notional_usdt, t.max_price, t.min_price,
                  t.entry_ts, t.horizon_minutes,
                  t.sl_price AS sl_price_override,
                  COALESCE(d.partial_exit_ratio, 0.0) AS partial_exit_ratio,
                  COALESCE(d.partial_pnl_pct, 0.0)    AS partial_pnl_pct,
                  d.partial_exit_price                 AS partial_exit_price
           FROM quick_sim_trades t
           LEFT JOIN ai_decisions d ON d.id = t.decision_id
           WHERE t.settled = 0""",
    )
    if not open_rows:
        return []
    price_cache: Dict[str, Optional[float]] = {}
    out: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    for row in open_rows:
        # v314 P0: 过期保护 —— 过期仓不再用「现价」判 TP/SL。
        # (旧实现: 停摆 8.85 天后价格某天碰到 SL 价位, 就用那天现价结算,
        #  一笔 20 分钟的单被算成 ±50% 量级 —— 实测 id=226 即此路径。)
        # 过期仓交给 settle_trades 按「到期时刻价」结算, 或被熔断废弃。
        if _is_expired(row, now_ts):
            continue
        asset = row["asset"]
        if asset not in price_cache:
            price_cache[asset] = price_fn(asset)
        cur_price = price_cache[asset]
        if not isinstance(cur_price, (int, float)) or cur_price <= 0:
            continue
        cur_price = float(cur_price)
        entry = float(row["entry_price"])
        if entry <= 0:
            continue
        action = row["action"]
        sgn = 1 if action == "BUY" else -1
        max_price = row["max_price"]
        min_price = row["min_price"]
        ref_max = max(max_price or entry, cur_price, entry)
        ref_min = min(min_price or entry, cur_price, entry)
        tp1_price = entry * (1.0 + tp1_mult / 100.0 * sgn)
        tp2_price = entry * (1.0 + tp2_mult / 100.0 * sgn)
        # P0.2 part B: TP1 后的 breakeven-move 覆写默认 SL。
        # 若该 trade 已 TP1 过，sl_price_override 由 _record_tp1_partial 写入 entry±offset；
        # 未 TP1 时仍按 entry×(1±sl_mult) 公式。
        sl_override = row.get("sl_price_override")
        if sl_override is not None and float(sl_override) > 0:
            sl_price = float(sl_override)
        else:
            sl_price = entry * (1.0 - sl_mult / 100.0 * sgn)
        if sgn == 1:
            tp1_hit = ref_max >= tp1_price
            tp2_hit = ref_max >= tp2_price
            sl_hit = ref_min <= sl_price
        else:
            tp1_hit = ref_min <= tp1_price
            tp2_hit = ref_min <= tp2_price
            sl_hit = ref_max >= sl_price
        if sl_hit:
            _settle_sl(conn, row, cur_price, entry, sgn, sl_price,
                       tp1_ratio, out, now_ts)
            continue
        if tp2_hit:
            _settle_tp2(conn, row, cur_price, entry, sgn, tp1_price,
                        tp2_price, tp1_ratio, tp2_ratio, out, now_ts)
            continue
        if tp1_hit:
            _record_tp1_partial(conn, row, cur_price, entry, sgn, tp1_price,
                                tp1_ratio, out)
    if out:
        conn.commit()
    return out


def _settle_sl(conn, row, cur_price, entry, sgn, sl_price,
               tp1_ratio_unused, out, now_ts):
    """SL 全平 helper（被 check_tp_sl 内部调用）。

    tp1_ratio_unused 是签名占位（SL 路径只看 partial_exit_ratio，不看 ratio 配置）。
    """
    partial_ratio = float(row["partial_exit_ratio"] or 0.0)
    partial_pnl = float(row["partial_pnl_pct"] or 0.0)
    sl_pnl_theory = sgn * (sl_price - entry) / entry * 100.0
    if partial_ratio == 0.0:
        blended = sl_pnl_theory
    else:
        blended = partial_pnl + (1.0 - partial_ratio) * sl_pnl_theory
    # v482：blended 是毛值；净收益必须扣往返成本（此前这里只写毛值 → cost_bps=0）。
    gross_pnl_pct = blended
    net_pnl_pct, cost_bps, _ = settle_figures(gross_pnl_pct, row["asset"], row["action"])
    # v230.2 P0.1：verdict 由 blended pnl 实际符号决定，不再强制 LOSS。
    # 场景：TP1 已锁利 + SL 在 breakeven 触发时，blended = partial_pnl > 0，
    # 旧逻辑写 LOSS 会让"实际赚钱"的交易被错判，下游 winrate 与 is_correct 全错位。
    verdict = "WIN" if net_pnl_pct > 0 else "LOSS" if net_pnl_pct < 0 else "BREAKEVEN"
    notional = float(row["notional_usdt"] or 0.0)
    pnl_usdt = notional * net_pnl_pct / 100.0 if notional else 0.0
    # v230.2 P0.2：mae 用 update_open_extremes 已跟踪的 max/min 计算。
    # row["max_price"/"min_price"] 在 check_tp_sl 之前已被 update_open_extremes
    # 用本轮 cur_price 更新过（fallback 到 entry_price），所以直接拿来算即可。
    # 旧逻辑 abs(sgn*(cur_price-entry)/entry*100) 把 cur_price 当 mae，
    # 在 breakeven-move SL 场景下会写出 mae≈0，掩盖真实回撤幅度。
    final_max = float(row["max_price"] or entry)
    final_min = float(row["min_price"] or entry)
    if sgn == 1:  # BUY：adverse = entry - min
        mae_now = max(0.0, (entry - final_min) / entry * 100.0)
    else:  # SELL：adverse = max - entry
        mae_now = max(0.0, (final_max - entry) / entry * 100.0)
    conn.execute(
        """UPDATE quick_sim_trades
           SET settled = 1, exit_price = ?, exit_ts = ?,
               pnl_pct = ?, pnl_usdt = ?,
               verdict = ?, mae_pct = ?,
               cost_bps = ?, gross_pnl_pct = ?,
               max_price = MAX(COALESCE(max_price, entry_price), ?),
               min_price = MIN(COALESCE(min_price, entry_price), ?)
           WHERE id = ? AND settled = 0""",
        (float(cur_price), now_ts, round(net_pnl_pct, 4),
         round(pnl_usdt, 6), verdict, round(mae_now, 4),
         cost_bps, round(gross_pnl_pct, 4),
         float(cur_price), float(cur_price), int(row["id"])),
    )
    if row["decision_id"] is not None:
        conn.execute(
            """UPDATE ai_decisions
               SET is_correct  = COALESCE(NULLIF(is_correct, ''), ?),
                   settled     = 1,
                   forward_pnl = COALESCE(forward_pnl, ?),
                   exit_price  = COALESCE(NULLIF(exit_price, 0), ?),
                   max_price   = COALESCE(NULLIF(max_price, 0), ?),
                   min_price   = COALESCE(NULLIF(min_price, 0), ?),
                   mae_pct     = COALESCE(mae_pct, ?)
               WHERE id = ?""",
            (verdict, round(net_pnl_pct, 4), float(cur_price),
             final_max, final_min,
             round(mae_now, 4), int(row["decision_id"])),
        )
    out.append({
        "id": int(row["id"]),
        "asset": row["asset"],
        "action": row["action"],
        "trigger": "SL",
        "entry_price": entry,
        "sl_price": round(sl_price, 6),
        "exit_price": cur_price,
        "partial_exit_ratio": partial_ratio,
        "pnl_pct": round(net_pnl_pct, 4),
        "gross_pnl_pct": round(gross_pnl_pct, 4),
        "cost_bps": cost_bps,
        "pnl_usdt": round(pnl_usdt, 6),
    })


def _settle_tp2(conn, row, cur_price, entry, sgn, tp1_price, tp2_price,
                tp1_ratio, tp2_ratio, out, now_ts):
    """TP2 全平 helper：blended pnl。

    若 partial 未发生：按理论 tp1/tp2 pnl 加权（一步到位假设）。
    若 partial 已发生：partial_pnl + (1 - partial_ratio) × cur_pnl。
    """
    exit_pnl_pct = sgn * (cur_price - entry) / entry * 100.0
    partial_ratio = float(row["partial_exit_ratio"] or 0.0)
    partial_pnl = float(row["partial_pnl_pct"] or 0.0)
    tp1_pnl_theory = sgn * (tp1_price - entry) / entry * 100.0
    tp2_pnl_theory = sgn * (tp2_price - entry) / entry * 100.0
    if partial_ratio == 0.0:
        blended = tp1_ratio * tp1_pnl_theory + tp2_ratio * tp2_pnl_theory
    else:
        blended = partial_pnl + (1.0 - partial_ratio) * exit_pnl_pct
    # v482：blended 是毛值；净收益必须扣往返成本（此前这里只写毛值 → cost_bps=0）。
    gross_pnl_pct = blended
    net_pnl_pct, cost_bps, _ = settle_figures(gross_pnl_pct, row["asset"], row["action"])
    # v230.2 P0.1：verdict 由 blended pnl 实际符号决定，不再强制 WIN。
    # 场景：TP1 partial 后价格反转跌破 TP1 → 在更低价触发 TP2 时 blended 可能为负，
    # 旧逻辑写 WIN 会把"实际亏钱"的交易错判为胜。
    verdict = "WIN" if net_pnl_pct > 0 else "LOSS" if net_pnl_pct < 0 else "BREAKEVEN"
    notional = float(row["notional_usdt"] or 0.0)
    pnl_usdt = notional * net_pnl_pct / 100.0 if notional else 0.0
    # v230.2 P0.2：mfe 用 update_open_extremes 已跟踪的 max/min 计算（同 mae 原理）。
    # 旧逻辑把 cur_price 当 mfe，会在 TP2 触发瞬间低估"持仓期间最大有利偏移"，
    # 尤其是先冲到 TP1 中途后回撤再冲 TP2 的场景。
    final_max = float(row["max_price"] or entry)
    final_min = float(row["min_price"] or entry)
    if sgn == 1:  # BUY：favorable = max - entry
        mfe_now = max(0.0, (final_max - entry) / entry * 100.0)
    else:  # SELL：favorable = entry - min
        mfe_now = max(0.0, (entry - final_min) / entry * 100.0)
    conn.execute(
        """UPDATE quick_sim_trades
           SET settled = 1, exit_price = ?, exit_ts = ?,
               pnl_pct = ?, pnl_usdt = ?,
               verdict = ?, mfe_pct = ?,
               cost_bps = ?, gross_pnl_pct = ?,
               max_price = MAX(COALESCE(max_price, entry_price), ?),
               min_price = MIN(COALESCE(min_price, entry_price), ?)
           WHERE id = ? AND settled = 0""",
        (float(cur_price), now_ts, round(net_pnl_pct, 4),
         round(pnl_usdt, 6), verdict, round(mfe_now, 4),
         cost_bps, round(gross_pnl_pct, 4),
         float(cur_price), float(cur_price), int(row["id"])),
    )
    if row["decision_id"] is not None:
        conn.execute(
            """UPDATE ai_decisions
               SET is_correct  = COALESCE(NULLIF(is_correct, ''), ?),
                   settled     = 1,
                   forward_pnl = COALESCE(forward_pnl, ?),
                   exit_price  = COALESCE(NULLIF(exit_price, 0), ?),
                   max_price   = COALESCE(NULLIF(max_price, 0), ?),
                   min_price   = COALESCE(NULLIF(min_price, 0), ?),
                   mfe_pct     = COALESCE(mfe_pct, ?)
               WHERE id = ?""",
            (verdict, round(net_pnl_pct, 4), float(cur_price),
             final_max, final_min,
             round(mfe_now, 4), int(row["decision_id"])),
        )
    out.append({
        "id": int(row["id"]),
        "asset": row["asset"],
        "action": row["action"],
        "trigger": "TP2",
        "entry_price": entry,
        "tp2_price": round(tp2_price, 6),
        "exit_price": cur_price,
        "partial_exit_ratio": partial_ratio,
        "pnl_pct": round(net_pnl_pct, 4),
        "gross_pnl_pct": round(gross_pnl_pct, 4),
        "cost_bps": cost_bps,
        "pnl_usdt": round(pnl_usdt, 6),
    })


def _record_tp1_partial(conn, row, cur_price, entry, sgn, tp1_price,
                        tp1_ratio, out):
    """TP1 部分平仓 helper：trade 保持 open，仅写 ai_decisions.partial_*.

    v230+ P0.2 part B：breakeven-move。
    TP1 命中后把 quick_sim_trades.sl_price 设为 entry × (1 ± offset × sgn)，
    后续轮次的 check_tp_sl 看到该 override 后即把 SL 上移到 breakeven。
    """
    partial_ratio = float(row["partial_exit_ratio"] or 0.0)
    if partial_ratio >= tp1_ratio:
        return
    tp1_pnl_actual = sgn * (cur_price - entry) / entry * 100.0
    # v482：partial_pnl_pct 存**毛值**（成本在最终结算按 blended 一次性扣，
    # 避免同一笔仓位被扣两次往返成本）。字段名沿用历史，语义见本注释。
    partial_pnl_added = tp1_ratio * tp1_pnl_actual
    notional = float(row["notional_usdt"] or 0.0)
    partial_usdt = notional * partial_pnl_added / 100.0 if notional else 0.0
    mfe_now = abs(sgn * (cur_price - entry) / entry * 100.0)
    if row["decision_id"] is not None:
        conn.execute(
            """UPDATE ai_decisions
               SET partial_exit_price = ?,
                   partial_exit_ratio = ?,
                   partial_pnl_pct    = COALESCE(partial_pnl_pct, 0) + ?,
                   max_price         = COALESCE(max_price, ?),
                   min_price         = COALESCE(min_price, ?),
                   mfe_pct            = COALESCE(mfe_pct, ?)
               WHERE id = ?""",
            (round(tp1_price, 6), float(tp1_ratio),
             round(partial_pnl_added, 4),
             float(cur_price), float(cur_price),
             round(mfe_now, 4),
             int(row["decision_id"])),
        )
    breakeven_applied = False
    be_sl_price: Optional[float] = None
    if getattr(config, "TP_SL_BREAKEVEN_ENABLED", True):
        offset_pct = float(getattr(config, "TP_SL_BREAKEVEN_OFFSET_PCT", 0.0) or 0.0)
        be_sl_price = entry * (1.0 + offset_pct / 100.0 * sgn)
        # 仅在该 trade 尚未设置过 override 时覆写（保持幂等：第二轮 TP1 不会把价格重写）。
        existing = row.get("sl_price_override")
        if existing is None:
            conn.execute(
                "UPDATE quick_sim_trades SET sl_price = ? WHERE id = ?",
                (round(be_sl_price, 6), int(row["id"])),
            )
            breakeven_applied = True
    out.append({
        "id": int(row["id"]),
        "decision_id": row["decision_id"],
        "asset": row["asset"],
        "action": row["action"],
        "trigger": "TP1_partial",
        "entry_price": entry,
        "tp1_price": round(tp1_price, 6),
        "exit_price": cur_price,
        "partial_exit_ratio": float(tp1_ratio),
        "partial_pnl_pct": round(partial_pnl_added, 4),
        "partial_usdt": round(partial_usdt, 6),
        "breakeven_applied": breakeven_applied,
        "breakeven_sl_price": round(be_sl_price, 6) if be_sl_price is not None else None,
    })


def update_open_extremes(
    conn: sqlite3.Connection,
    price_fn: Callable[[str], Optional[float]],
) -> int:
    """给未结算持仓按当前行情刷新期间最高/最低价（每轮 watcher 调用，累计过程质量）。

    只用真实行情，取不到价就跳过对应资产；返回更新的持仓行数。

    v314 P0：**过期仓不再刷新极值**。旧实现每轮都用现价刷新 max/min，
    停摆期间累积的是「几天的极值」，与真实持仓期（horizon 分钟）完全不符，
    会把 MFE/MAE 一起污染。过期仓交给 settle_trades 结算时收敛。
    """
    now_ts = int(time.time())
    grace_sec = int(getattr(config, "QUICK_SIM_SETTLE_GRACE_MINUTES", 60)) * 60
    open_rows = _rows(
        conn,
        """SELECT DISTINCT asset FROM quick_sim_trades
           WHERE settled = 0 AND entry_ts + horizon_minutes * 60 + ? > ?""",
        (grace_sec, now_ts),
    )
    # v470：先在事务外取行情（price_fn 可能走缓存/回退源），凑好 (asset, price)
    # 映射；持仓/资产本来就是个位数，事务内按资产各一条 UPDATE、单事务单 commit。
    # 取价这步可能走外网/缓存，放在事务外就不会占着写锁等行情。取不到价的资产跳过。
    priced: List[tuple] = []
    price_cache: Dict[str, float] = {}
    for row in open_rows:
        asset = row["asset"]
        if asset not in price_cache:
            price_cache[asset] = price_fn(asset)
        price = price_cache[asset]
        if isinstance(price, (int, float)) and price > 0:
            priced.append((asset, float(price)))

    if not priced:
        return 0

    updated = 0
    for asset, price in priced:
        cur = conn.execute(
            """UPDATE quick_sim_trades
               SET max_price = CASE WHEN ? > COALESCE(max_price, entry_price)
                                     THEN ? ELSE COALESCE(max_price, entry_price) END,
                   min_price = CASE WHEN ? < COALESCE(min_price, entry_price)
                                     THEN ? ELSE COALESCE(min_price, entry_price) END
               WHERE settled = 0 AND asset = ?
                 AND entry_ts + horizon_minutes * 60 + ? > ?""",
            (price, price, price, price, asset, grace_sec, now_ts),
        )
        updated += cur.rowcount or 0
    conn.commit()
    return updated


def _profitability(rows: List[Dict[str, Any]], notional: float) -> Dict[str, Any]:
    wins = sum(row["verdict"] == "WIN" for row in rows)
    losses = sum(row["verdict"] == "LOSS" for row in rows)
    decided = wins + losses
    total_pnl_pct = sum(float(row["pnl_pct"] or 0.0) for row in rows)
    total_pnl_usdt = sum(float(row["pnl_usdt"] or 0.0) for row in rows)
    invested = sum(float(row["notional_usdt"] or 0.0) for row in rows) or notional * len(rows)
    return {
        "settled": len(rows),
        "wins": wins,
        "losses": losses,
        "breakeven": len(rows) - decided,
        "winrate_pct": round(wins / decided * 100, 2) if decided else None,
        "avg_pnl_pct": round(total_pnl_pct / len(rows), 4) if rows else None,
        "total_pnl_pct": round(total_pnl_pct, 4),
        "total_pnl_usdt": round(total_pnl_usdt, 4),
        "return_on_notional_pct": round(total_pnl_usdt / invested * 100, 4) if invested else None,
        "profitable": total_pnl_usdt > 0 if rows else None,
    }


def evaluation(
    conn: sqlite3.Connection,
    recent_limit: int = 20,
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """快速模拟交易的盈利率评测，全部来自真实结算记录。

    v180 P0+：新增 hours / from_ts / to_ts（UTC 秒）三选一过滤，与
    /api/history/news-trades 同一套口径 —— 今日/近7天/近30天用 hours，
    自定义时段直接传 UTC 秒对。三个参数都缺省时维持原"全量聚合"行为，
    避免影响旧调用方（如 e2e_channel_trace.py）。
    """
    # 窗口 → (lo, hi)；缺省 = (-inf, +inf)，等价于全量
    lo, hi = _resolve_window(hours, from_ts, to_ts)
    has_window = lo is not None or hi is not None
    settled_sql = (
        """SELECT qt.asset, qt.action, qt.gate_passed, qt.verdict, qt.pnl_pct,
                  qt.pnl_usdt, qt.notional_usdt,
                  COALESCE(NULLIF(ad.agent_model_id, ''), '(未知)') AS model
           FROM quick_sim_trades qt
           LEFT JOIN ai_decisions ad ON ad.id = qt.decision_id
           WHERE qt.settled = 1"""
    )
    settled_params: tuple = ()
    if lo is not None:
        settled_sql += " AND qt.exit_ts >= ?"
        settled_params = (lo,) + settled_params
    if hi is not None:
        settled_sql += " AND qt.exit_ts <= ?"
        settled_params = settled_params + (hi,)
    settled_rows = _rows(conn, settled_sql, settled_params)

    open_rows_sql = (
        """SELECT id, decision_id, asset, action, entry_price, entry_ts, horizon_minutes,
                  gate_passed, gate_reason
           FROM quick_sim_trades WHERE settled = 0"""
    )
    open_rows_params: tuple = ()
    # v302+ P0+【修复】open 也要按窗口过滤 —— 否则 B 卡片"待结算 4 · 已结算 2"
    # 会把全库的 open 单全塞进来,total/avg_pnl 口径被 open 单污染。
    # 与 settled_rows 用 exit_ts 一致(open 单没有 exit_ts,用 entry_ts)。
    if lo is not None:
        open_rows_sql += " AND entry_ts >= ?"
        open_rows_params = (lo,) + open_rows_params
    if hi is not None:
        open_rows_sql += " AND entry_ts <= ?"
        open_rows_params = open_rows_params + (hi,)
    open_rows_sql += " ORDER BY entry_ts DESC"
    open_rows = _rows(conn, open_rows_sql, open_rows_params)
    # recent：默认按 entry_ts 过滤窗口（如设了窗口），保证 "近 N 笔" 严格在窗口内
    recent_sql = (
        """SELECT id, decision_id, asset, action, entry_price, exit_price, pnl_pct,
                  pnl_usdt, verdict, gate_passed, gate_reason, entry_ts, exit_ts, settled
           FROM quick_sim_trades"""
    )
    recent_params: tuple = ()
    if has_window:
        clauses = []
        if lo is not None:
            clauses.append("entry_ts >= ?")
            recent_params = (lo,) + recent_params
        if hi is not None:
            clauses.append("entry_ts <= ?")
            recent_params = recent_params + (hi,)
        recent_sql += " WHERE " + " AND ".join(clauses)
    recent_sql += " ORDER BY id DESC LIMIT ?"
    recent = _rows(conn, recent_sql, recent_params + (recent_limit,))
    notional = config.QUICK_SIM_NOTIONAL_USDT
    by_asset: Dict[str, List[Dict[str, Any]]] = {}
    for row in settled_rows:
        by_asset.setdefault(row["asset"], []).append(row)
    # 按模型归因（JOIN ai_decisions.agent_model_id）→ 对比各模型分析质量的快样本口径
    by_model_raw: Dict[str, List[Dict[str, Any]]] = {}
    for row in settled_rows:
        by_model_raw.setdefault(row["model"], []).append(row)
    by_model = [
        {"model": model, **_profitability(rows, notional)}
        for model, rows in sorted(by_model_raw.items(), key=lambda kv: -len(kv[1]))
    ]
    overall = _profitability(settled_rows, notional)
    # 口径统一：与 /api/replay/stats 的 paper overall 字段对齐（total=建仓总笔数，
    # open=跟踪中、settled=已结算），让「快速评测」与「真实持仓」以同一字段结构呈现，
    # 前端反思系统 / 模拟盘两处可直接同源复用。
    overall["total"] = len(settled_rows) + len(open_rows)
    overall["open"] = len(open_rows)
    return {
        "enabled": config.QUICK_SIM_ENABLED,
        "horizon_minutes": config.QUICK_SIM_HORIZON_MINUTES,
        "notional_usdt": notional,
        "open_trades": len(open_rows),
        "overall": overall,
        "gate_passed": _profitability([row for row in settled_rows if row["gate_passed"]], notional),
        "gate_rejected": _profitability([row for row in settled_rows if not row["gate_passed"]], notional),
        "by_model": by_model,
        "by_asset": [
            {"asset": asset, **_profitability(rows, notional)}
            for asset, rows in sorted(by_asset.items())
        ],
        "open": open_rows[:recent_limit],
        "recent": recent,
        "method": "全部 BUY/SELL 信号按真实行情建仓，到期按真实行情结算；闸门结论仅作分组标记",
    }


def run_cycle(price_fn: Callable[[str], Optional[float]]) -> Dict[str, Any]:
    """一次完整周期：建仓 + 结算，返回本轮变化。"""
    if not config.QUICK_SIM_ENABLED:
        return {"opened": [], "settled": []}
    conn = db.get_connection()
    try:
        # v313 P0 (健壮性修复): 每个子步骤**独立隔离**。
        # 旧实现是一条直线: 任何一个子步骤抛异常, 整个 run_cycle 就中断,
        # 后面的 settle_trades (真正的结算 + 回填 ai_decisions.forward_pnl) 永远不跑。
        # 实测就是被 cut_bleeding_trades 的一个 TypeError 掐断的 —— 结算链静默停摆,
        # 而 api 侧只看到一行 "[QUICK-SIM] TypeError: ..." (累计 3414 次)。
        # 现在: 辅助步骤失败只记日志并降级, **结算永远要跑**。
        opened: List[Dict[str, Any]] = []
        tp_sl_hits: List[Dict[str, Any]] = []
        cut: List[Dict[str, Any]] = []
        settled: List[Dict[str, Any]] = []

        def _step(name, fn, default):
            try:
                result = fn()
                # v440 P0-0f：每个步骤结束就提交。
                # 事故（2026-11-17 22:24 长事务哨兵）：quick_sim 一轮里
                # gate_audit/build/settle 等步骤各自写库但都不提交，事务一直开着，
                # 实测 17.0s / 34.5s，同期行情写入线程要排队 30s 才写进一条 tick。
                # 步骤之间互不依赖（各自幂等），逐步骤提交不影响语义。
                try:
                    conn.commit()
                except Exception:
                    pass
                return result
            except Exception as exc:
                print(f"[quick_sim] {name} 失败(已隔离): {type(exc).__name__}: {str(exc)[:120]}",
                      flush=True)
                try:
                    conn.rollback()
                except Exception:
                    pass
                return default

        # v321.4 P0: 闸门评估留痕 (观测层, 不参与判定)。
        # 放在最前: 即使建仓步骤挂了, "这轮看过哪些候选、被什么条件拦下"仍要留档。
        # trade_decisions 只记最终动作 → 分数门槛的判别力在数据层是空的
        # (实测 3187 条 REJECT 全是规则型, 分数型 0 条)。
        _step(
            "gate_audit",
            lambda: gate_audit.record_gate_funnel(
                conn, minutes=180,
                passed_reason=_PASSED_REASON,
                allowed_assets=config.SIM_ALLOWED_ASSETS,
                conf_gate=config.EVIDENCE_CONFIDENCE_GATE,
                tradable_statuses=("UNREAD", "APPROVED", "REVIEWED", "AUTO_APPROVED"),
            ),
            None,
        )
        opened = _step("open_trades", lambda: open_trades(conn, price_fn), opened)
        _step("update_open_extremes", lambda: update_open_extremes(conn, price_fn), 0)
        # v230+ P0.2：TP/SL 强制结算（TP1 partial + TP2/SL 全平）
        tp_sl_hits = _step("check_tp_sl", lambda: check_tp_sl(conn, price_fn), tp_sl_hits)
        # v230+ P0.5：先砍失控单，再让 settle_trades 处理到期单
        cut = _step(
            "cut_bleeding_trades",
            lambda: cut_bleeding_trades(
                conn, price_fn,
                max_loss_pct=(config.EARLY_LOSS_CUT_PCT if config.EARLY_LOSS_CUT_ENABLED else None),
            ),
            cut,
        )
        # 结算是**最不能失败**的一步 —— 它回填 ai_decisions.forward_pnl (真值来源)。
        settled = _step("settle_trades", lambda: settle_trades(conn, price_fn), settled)
        # v314 P0: 结算口径告警 —— 超期(结算链停摆)/熔断废弃/现价兜底都要能看见。
        # 实测事故 (停摆 8.85 天) 当年只有一行 TypeError 日志, 没人看见数字。
        settle_stats = getattr(settled, "settle_stats", None) or {}
        if settle_stats.get("overdue") or settle_stats.get("abandoned") or settle_stats.get("live_price"):
            print(f"[QUICK-SIM] SETTLE-ALERT {settle_stats}", flush=True)
        return {"opened": opened, "tp_sl_hits": tp_sl_hits,
                "cut": cut, "settled": settled,
                # v314 P0: 结算口径统计 (due_price/live_price/overdue/abandoned/deferred)
                "settle_stats": settle_stats,
                # v231+ (C4): 报告 QUALITY_FILTER 拦截数给 API/dashboard
                "quality_rejected_count": getattr(opened, 'quality_rejected_count', 0),
                "quality_rejected_sample": getattr(opened, 'quality_rejected', [])[:5]}
    finally:
        conn.close()


def snapshot(
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """给 API 用的评测快照。

    v180 P0+：转发 hours / from_ts / to_ts 到 evaluation()，让反思页"快速评测"
    B 卡能按用户选定的时段过滤；保留无参调用以兼容旧缓存 / e2e 测试。
    """
    conn = db.get_connection()
    try:
        return evaluation(conn, hours=hours, from_ts=from_ts, to_ts=to_ts)
    finally:
        conn.close()

def _apply_position_cap(conn, notional: float) -> float:
    """v409.5: 按 paper_trading_settings.max_position_pct 给单笔名义封顶。

    · 列值 NULL / 0 → 原样返回（不设限，保持既有行为）；
    · 有值 → 上限 = 最新 sim_equity_snapshots.equity × 该比例；
    · 老库没列 / 没有权益快照 / 任何异常 → 原样返回，绝不搞挂建仓。
    """
    try:
        row = conn.execute(
            "SELECT max_position_pct FROM paper_trading_settings WHERE id=1"
        ).fetchone()
        pct = float(row[0]) if row and row[0] else 0.0
        if pct <= 0:
            return notional
        eq = conn.execute(
            "SELECT equity FROM sim_equity_snapshots WHERE equity IS NOT NULL "
            "AND equity > 0 ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not eq or not eq[0]:
            return notional
        cap = round(float(eq[0]) * pct, 2)
        if cap < notional:
            print(f"  [POSITION-CAP] 单笔名义 {notional} > 权益上限 {cap}"
                  f"（{pct:.2%} × 权益）→ 按上限下单", flush=True)
            return cap
    except Exception:
        return notional
    return notional

