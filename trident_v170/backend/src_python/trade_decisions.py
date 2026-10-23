"""trade_decisions — paper trade 决策打点 + 归因统计 + AI 闸门分析。

KK 必然 11 (Tracking) —— "一切都被记录是正在发生的趋势"。不做就掉队。

2026-10 双向同步合并了两条并行演进线 (此前同名文件互相覆盖, A 线功能一度丢失):

  A. 决策打点与统计线
     - init_schema / log_decision
     - get_stats_by_asset_action / get_overall_stats / get_mfe_mae_stats
     - get_decision_volume / get_data_quality_report
     调用方: quick_sim.py / weekly_reflection.py / daily_data_check.py

  B. AI 闸门分析线
     - get_ai_decision_gate_breakdown / get_gate_shadow_projection
     调用方: weekly_reflection.py / bridge_server.py / confidence_shadow.py

设计原则:
  - 不动现有业务逻辑, 只在关键决策点 log_decision()
  - 表结构最小但带 "context_json" 以备事后归因
  - 所有函数接受任意 sqlite3.Connection (含 :memory:), 行访问同时兼容
    tuple 与 sqlite3.Row (不依赖调用方是否设置 row_factory)

两个公开的闸门分析接口:
  1) get_ai_decision_gate_breakdown(conn, lookback_days=7)
     把 ai_decisions 的 BUY/SELL 行按闸门结果聚合:
       - buy_or_sell / gate_rejected / gate_passed / rejection_rate_pct
       - by_asset: 按 asset 维度拆
       - by_reason_template: 把 trade_gate_reason 里的数字归并成 "N" 模板
       - confidence_buckets: 按 evidence_confidence 分桶

  2) get_gate_shadow_projection(conn, lookback_days=7)
     4 个 "如果闸门换成 X 会怎样" 的 dry-run 投影:
       - 现状 (config.EVIDENCE_CONFIDENCE_GATE)
       - 闸门 → 20
       - boost (P2 #10 — confidence × 5 后 clamp 100)
       - combo (boost + gate 20)
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

# 默认闸门 (从 config 读, 缺省 10) — 与 bridge_server.py / confidence_shadow.py 对齐
try:
    import config as _config
    DEFAULT_GATE = float(_config.EVIDENCE_CONFIDENCE_GATE)
except (ImportError, AttributeError):
    DEFAULT_GATE = 10.0

LEGACY_GATE = 20.0  # 历史值, 用于 "闸门 → 20" 投影
BOOST_MULTIPLIER = 5  # P2 #10 boost 系数
CONFIDENCE_SATURATION = 100.0  # boost 后 clamp 上界

# confidence 桶边界 (左闭右开): 0-10, 10-20, 20-40, 40-60, 60-80, 80-100, 100+
_BUCKET_EDGES: List[Tuple[float, float]] = [
    (0.0, 10.0),
    (10.0, 20.0),
    (20.0, 40.0),
    (40.0, 60.0),
    (60.0, 80.0),
    (80.0, 100.0),
    (100.0, float("inf")),
]


def _row_get(row: Any, key: str, idx: int, default: Any = None) -> Any:
    """按 key 取值; row 为 tuple 时退回按下标取。

    让同一份 SQL 结果在 conn.row_factory = sqlite3.Row 与默认 tuple 两种
    模式下都能安全读取 —— 避免 ":memory:" 测试连接下出现 TypeError:
    tuple indices must be integers。
    """
    try:
        return row[key]
    except (TypeError, IndexError, KeyError):
        pass
    try:
        return row[idx]
    except (TypeError, IndexError, KeyError):
        return default


def _bucket_for_value(v: float) -> str:
    """把 evidence_confidence 映射到桶 label。

    左闭右开: 5.0→0-10, 8.0→0-10, 15.0→10-20, 50.0→40-60。
    """
    if v < 0:
        return "negative"
    if v < 10:
        return "0-10"
    if v < 20:
        return "10-20"
    if v < 40:
        return "20-40"
    if v < 60:
        return "40-60"
    if v < 80:
        return "60-80"
    if v < 100:
        return "80-100"
    return "100+"


# ─── reason template 归并 ───────────────────────────
# "统一置信度 5.0 低于闸门 40.0" → "统一置信度 N 低于闸门 N"
# 把所有浮点数 / 整数替换成 "N"
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _normalize_reason_template(reason: Any) -> str:
    """把 trade_gate_reason 里的数字归并成 N。"""
    if not reason:
        return ""
    s = str(reason)
    s = _NUM_RE.sub("N", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ─── gate breakdown ─────────────────────────────────
def _fetch_filtered(conn: sqlite3.Connection, since_ts: int) -> List[sqlite3.Row]:
    """拿 [since_ts, +∞) 区间内 BUY/SELL 决策。"""
    try:
        return conn.execute(
            "SELECT id, target_asset, suggested_action, evidence_confidence, "
            "       trade_gate_reason, ts "
            "FROM ai_decisions "
            "WHERE ts >= ? "
            "  AND UPPER(suggested_action) IN ('BUY', 'SELL') "
            "ORDER BY ts DESC",
            (since_ts,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def get_ai_decision_gate_breakdown(
    conn: sqlite3.Connection,
    lookback_days: int = 7,
) -> Dict[str, Any]:
    """闸门拒绝聚合 + 数字模板归并 + 置信度桶。"""
    since_ts = int(time.time()) - lookback_days * 86400
    rows = _fetch_filtered(conn, since_ts)

    buy_or_sell = len(rows)
    rejected = sum(1 for r in rows if (_row_get(r, "trade_gate_reason", 4) or "").strip())
    passed = buy_or_sell - rejected
    rate_pct = (rejected / buy_or_sell * 100.0) if buy_or_sell > 0 else 0.0

    # by_asset
    asset_agg: Dict[str, Dict[str, int]] = {}
    for r in rows:
        a = (_row_get(r, "target_asset", 1) or "NONE").upper()
        bucket = asset_agg.setdefault(a, {"n": 0, "rejected": 0, "passed": 0})
        bucket["n"] += 1
        if (_row_get(r, "trade_gate_reason", 4) or "").strip():
            bucket["rejected"] += 1
        else:
            bucket["passed"] += 1
    by_asset = [
        {"asset": a, "n": v["n"], "rejected": v["rejected"], "passed": v["passed"]}
        for a, v in sorted(asset_agg.items())
    ]

    # by_reason_template (只看 rejected)
    tmpl_agg: Dict[str, int] = {}
    for r in rows:
        if not (_row_get(r, "trade_gate_reason", 4) or "").strip():
            continue
        t = _normalize_reason_template(_row_get(r, "trade_gate_reason", 4))
        if not t:
            continue
        tmpl_agg[t] = tmpl_agg.get(t, 0) + 1
    by_reason_template = [
        {"template": t, "count": c}
        for t, c in sorted(tmpl_agg.items(), key=lambda kv: -kv[1])
    ]

    # confidence_buckets (全部 BUY/SELL)
    bucket_agg: Dict[str, int] = {}
    for r in rows:
        try:
            conf = float(_row_get(r, "evidence_confidence", 3, 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        b = _bucket_for_value(conf)
        bucket_agg[b] = bucket_agg.get(b, 0) + 1
    # 按桶顺序输出, 0 也补
    confidence_buckets = []
    for lo, hi in _BUCKET_EDGES:
        label = f"{int(lo)}+" if hi == float("inf") else f"{int(lo)}-{int(hi)}"
        confidence_buckets.append({"range": label, "n": bucket_agg.get(label, 0)})

    return {
        "buy_or_sell": buy_or_sell,
        "gate_rejected": rejected,
        "gate_passed": passed,
        "rejection_rate_pct": round(rate_pct, 2),
        "by_asset": by_asset,
        "by_reason_template": by_reason_template,
        "confidence_buckets": confidence_buckets,
        "lookback_days": lookback_days,
    }


# ─── shadow projection ──────────────────────────────
def _fetch_decisions_with_pnl(conn: sqlite3.Connection, since_ts: int) -> List[sqlite3.Row]:
    """拿 [since_ts, +∞) 内 BUY/SELL + 关联 quick_sim_trades pnl (LEFT JOIN)。"""
    try:
        return conn.execute(
            "SELECT a.id, a.target_asset, a.suggested_action, a.evidence_confidence, "
            "       a.ts, "
            "       q.pnl_pct AS pnl_pct "
            "FROM ai_decisions a "
            "LEFT JOIN quick_sim_trades q ON q.decision_id = a.id "
            "WHERE a.ts >= ? "
            "  AND UPPER(a.suggested_action) IN ('BUY', 'SELL') "
            "ORDER BY a.ts DESC",
            (since_ts,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _project(rows: List[sqlite3.Row], gate: float,
             *, boost: bool = False) -> Dict[str, Any]:
    """单 scenario 投影: 给定 gate/boost, 算 would_pass / projected_pnl / win_rate。"""
    would_pass = 0
    pnl_sum = 0.0
    wins = 0
    for r in rows:
        try:
            conf = float(_row_get(r, "evidence_confidence", 3, 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if boost:
            conf = min(CONFIDENCE_SATURATION, conf * BOOST_MULTIPLIER)
        if conf >= gate:
            would_pass += 1
            pnl_v = _row_get(r, "pnl_pct", 5)
            if pnl_v is not None:
                try:
                    pf = float(pnl_v)
                    pnl_sum += pf
                    if pf > 0:
                        wins += 1
                except (TypeError, ValueError):
                    pass
    win_rate = (wins / would_pass * 100.0) if would_pass > 0 else 0.0
    return {
        "would_pass": would_pass,
        "projected_pnl_pct": round(pnl_sum, 4),
        "win_rate_pct": round(win_rate, 2),
    }


def get_gate_shadow_projection(
    conn: sqlite3.Connection,
    lookback_days: int = 7,
) -> Dict[str, Any]:
    """闸门 / boost / combo 三种 dry-run 投影。

    scenarios 顺序固定 (测试按 index 访问):
        [0] 现状 (current gate)
        [1] 闸门 → 20
        [2] P2 #10 boost (current gate, boost=True)
        [3] combo (boost + gate 20)
    """
    since_ts = int(time.time()) - lookback_days * 86400
    rows = _fetch_decisions_with_pnl(conn, since_ts)
    total_buy_sell = len(rows)

    if total_buy_sell == 0:
        return {
            "total_buy_sell": 0,
            "scenarios": [],
            "lookback_days": lookback_days,
            "current_gate": DEFAULT_GATE,
        }

    current_gate = DEFAULT_GATE
    base = _project(rows, current_gate, boost=False)
    raised = _project(rows, LEGACY_GATE, boost=False)
    boosted = _project(rows, current_gate, boost=True)
    combo = _project(rows, LEGACY_GATE, boost=True)

    scenarios = [
        {
            "label": f"现状 (闸门 {current_gate:g})",
            "gate": current_gate,
            "boost": False,
            **base,
        },
        {
            "label": f"闸门 → {LEGACY_GATE:g}",
            "gate": LEGACY_GATE,
            "boost": False,
            **raised,
        },
        {
            "label": f"P2 #10 boost (×{BOOST_MULTIPLIER}, 闸门 {current_gate:g})",
            "gate": current_gate,
            "boost": True,
            **boosted,
        },
        {
            "label": f"combo (boost ×{BOOST_MULTIPLIER} + 闸门 → {LEGACY_GATE:g})",
            "gate": LEGACY_GATE,
            "boost": True,
            **combo,
        },
    ]

    return {
        "total_buy_sell": total_buy_sell,
        "scenarios": scenarios,
        "lookback_days": lookback_days,
        "current_gate": current_gate,
    }


# ─── 决策打点 (A 线) ─────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_ts REAL NOT NULL,
    asset TEXT NOT NULL,
    intended_action TEXT NOT NULL,
    final_action TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    news_id INTEGER,
    event_strength TEXT,
    news_star REAL,
    entry_price REAL,
    planned_stop REAL,
    planned_target REAL,
    size_usdt REAL,
    deny_reason TEXT,
    guard TEXT,
    context_json TEXT,
    quick_sim_trade_id INTEGER,
    ai_decision_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_decisions_asset_ts ON trade_decisions(asset, decision_ts);
CREATE INDEX IF NOT EXISTS idx_decisions_decision_type ON trade_decisions(decision_type);
CREATE INDEX IF NOT EXISTS idx_decisions_ai_decision ON trade_decisions(ai_decision_id);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等创建表结构。"""
    conn.executescript(_SCHEMA)
    conn.commit()


def log_decision(
    conn: sqlite3.Connection,
    asset: str,
    intended_action: str,
    final_action: str,
    decision_type: str,
    *,
    news_id: Optional[int] = None,
    event_strength: Optional[str] = None,
    news_star: Optional[float] = None,
    entry_price: Optional[float] = None,
    planned_stop: Optional[float] = None,
    planned_target: Optional[float] = None,
    size_usdt: Optional[float] = None,
    deny_reason: Optional[str] = None,
    guard: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    quick_sim_trade_id: Optional[int] = None,
    ai_decision_id: Optional[int] = None,
) -> int:
    """记录一条决策点，返回新插入的 id。

    decision_type: ENTRY / EXIT / REJECT / HOLD / ADJUST
    guard: SAFETY_GUARD / SELL_GATE / POLICY_ENGINE / MANUAL / NONE
    """
    cur = conn.execute(
        """INSERT INTO trade_decisions
           (decision_ts, asset, intended_action, final_action, decision_type,
             news_id, event_strength, news_star,
             entry_price, planned_stop, planned_target, size_usdt,
             deny_reason, guard, context_json,
             quick_sim_trade_id, ai_decision_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            time.time(),
            asset.upper(),
            intended_action,
            final_action,
            decision_type,
            news_id,
            event_strength,
            news_star,
            entry_price,
            planned_stop,
            planned_target,
            size_usdt,
            deny_reason,
            guard,
            json.dumps(context or {}, ensure_ascii=False),
            quick_sim_trade_id,
            ai_decision_id,
        ),
    )
    conn.commit()
    return cur.lastrowid or 0


def get_stats_by_asset_action(
    conn: sqlite3.Connection,
    lookback_days: int = 30,
    min_samples: int = 3,
    gate_passed_only: bool = False,
) -> List[Dict[str, Any]]:
    """返回 [(asset, action, n, win_rate, avg_pnl, ...)] 列表，按 lookback_days 过滤。

    JOIN quick_sim_trades 用 entry_ts（实际成交时间）。

    gate_passed_only=True → 只统计 evidence 闸门真正放过的"真实交易"
                          （gate_passed=1）；
                          默认 False → 含 gate_passed=0 的反事实"假设交易"。
    """
    sql = """SELECT
              qs.asset,
              qs.action,
              COUNT(*) as n,
              ROUND(100.0 * SUM(CASE WHEN qs.pnl_pct > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
              ROUND(AVG(qs.pnl_pct) * 100, 2) as avg_pnl,
              ROUND(MIN(qs.pnl_pct) * 100, 2) as worst,
              ROUND(MAX(qs.pnl_pct) * 100, 2) as best,
              ROUND(SUM(qs.pnl_pct) * 100, 2) as sum_pnl
           FROM quick_sim_trades qs
           WHERE qs.settled = 1
             AND qs.entry_ts > strftime('%s','now', ?)"""
    if gate_passed_only:
        sql += " AND qs.gate_passed = 1"
    sql += """
           GROUP BY qs.asset, qs.action
           HAVING COUNT(*) >= ?
           ORDER BY qs.asset, qs.action"""
    rows = conn.execute(
        sql,
        (f"-{int(lookback_days)} day", int(min_samples)),
    ).fetchall()
    return [
        {
            "asset": r[0], "action": r[1], "n": r[2],
            "win_rate": r[3], "avg_pnl": r[4],
            "worst": r[5], "best": r[6], "sum_pnl": r[7],
        }
        for r in rows
    ]


def get_overall_stats(
    conn: sqlite3.Connection,
    lookback_days: int = 30,
    gate_passed_only: bool = False,
) -> Dict[str, Any]:
    """整体统计：n / win_rate / avg_pnl / sum_pnl。

    gate_passed_only=True → 只统计 evidence 闸门真正放过的"真实交易"。
    """
    sql = """SELECT
              COUNT(*) as n,
              ROUND(100.0 * SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
              ROUND(AVG(pnl_pct) * 100, 2) as avg_pnl,
              ROUND(SUM(pnl_pct) * 100, 2) as sum_pnl
           FROM quick_sim_trades
           WHERE settled = 1
             AND entry_ts > strftime('%s','now', ?)"""
    if gate_passed_only:
        sql += " AND gate_passed = 1"
    r = conn.execute(
        sql,
        (f"-{int(lookback_days)} day",),
    ).fetchone()
    return {
        "lookback_days": lookback_days,
        "n": r[0] or 0,
        "win_rate": r[1] or 0.0,
        "avg_pnl": r[2] or 0.0,
        "sum_pnl": r[3] or 0.0,
    }


def get_mfe_mae_stats(
    conn: sqlite3.Connection,
    lookback_days: int = 30,
    gate_passed_only: bool = False,
) -> List[Dict[str, Any]]:
    """MFE / MAE 统计：仓位生命周期内的最大有利偏移 / 最大不利偏移。

    数据来源：quick_sim_trades.max_price / min_price（每个持仓每轮 watcher 刷新）。

    含义：
      MFE (Maximum Favorable Excursion) — "如果在此刻平仓能赚的最大%"
      MAE (Maximum Adverse Excursion)   — "过程中最大浮亏%"
      mfe_realization_pct = avg_pnl / avg_mfe * 100
        → 接近 100% 说明仓位拿到顶；接近 0% 说明"坐过山车"卖飞了。

    用途：仓位管理政策依据（若 avg_mae 远大于 avg_mfe → 需要 trailing stop）。
    """
    sql = """SELECT
              asset,
              action,
              COUNT(*) as n,
              -- BUY:  MFE = (max - entry)/entry, MAE = (entry - min)/entry
              -- SELL: MFE = (entry - min)/entry, MAE = (max - entry)/entry
              ROUND(AVG(
                  CASE
                      WHEN action = 'BUY'  THEN (max_price - entry_price) / entry_price * 100
                      WHEN action = 'SELL' THEN (entry_price - min_price) / entry_price * 100
                  END
              ), 2) AS avg_mfe_pct,
              ROUND(AVG(
                  CASE
                      WHEN action = 'BUY'  THEN (entry_price - min_price) / entry_price * 100
                      WHEN action = 'SELL' THEN (max_price - entry_price) / entry_price * 100
                  END
              ), 2) AS avg_mae_pct,
              ROUND(AVG(pnl_pct) * 100, 2) AS avg_pnl_pct
           FROM quick_sim_trades
           WHERE settled = 1
             AND entry_price IS NOT NULL AND entry_price > 0
             AND max_price IS NOT NULL AND min_price IS NOT NULL
             AND entry_ts > strftime('%s','now', ?)"""
    if gate_passed_only:
        sql += " AND gate_passed = 1"
    sql += " GROUP BY asset, action HAVING COUNT(*) >= 3 ORDER BY asset, action"
    rows = conn.execute(sql, (f"-{int(lookback_days)} day",)).fetchall()
    out = []
    for r in rows:
        # mfe_realization_pct = avg_pnl_pct / avg_mfe_pct * 100
        # 在 Python 里算（避免 SQL 重复表达式）
        if r[3] and abs(r[3]) > 0.0001:
            mfe_real = round(float(r[5]) / float(r[3]) * 100, 1)
        else:
            mfe_real = None
        out.append({
            "asset": r[0], "action": r[1], "n": r[2],
            "avg_mfe_pct": r[3], "avg_mae_pct": r[4],
            "mfe_realization_pct": mfe_real,
            "avg_pnl_pct": r[5],
        })
    return out


def get_decision_volume(
    conn: sqlite3.Connection,
    lookback_days: int = 30,
) -> Dict[str, int]:
    """trade_decisions 表本身的打点量统计。"""
    rows = conn.execute(
        """SELECT decision_type, COUNT(*) as n
           FROM trade_decisions
           WHERE decision_ts > strftime('%s','now', ?)
           GROUP BY decision_type""",
        (f"-{int(lookback_days)} day",),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def get_data_quality_report(
    conn: sqlite3.Connection,
    lookback_days: int = 30,
) -> Dict[str, Any]:
    """数据质量自检 — KK 必然 11 的具体落地。

    每天跑一次，看:
      1. trade_decisions 本身有没有在打点（如果没 → 打点坏了）
      2. quick_sim_trades 是否每天都有数据（如果某天 0 → 漏结算）
      3. asset 名是否统一（XAU vs xau vs Gold）
      4. decision_type 是否有分布（如果全 REJECT → 业务挂了）
    """
    out: Dict[str, Any] = {}

    # 1. 打点量
    out["decision_counts"] = get_decision_volume(conn, lookback_days)

    # 2. quick_sim_trades 各天分布（找空白日）
    rows = conn.execute(
        """SELECT strftime('%Y-%m-%d', entry_ts, 'unixepoch') as day,
                  COUNT(*) as n
           FROM quick_sim_trades
           WHERE entry_ts > strftime('%s','now', ?)
           GROUP BY day ORDER BY day DESC""",
        (f"-{int(lookback_days)} day",),
    ).fetchall()
    days_present = {r[0]: r[1] for r in rows}
    out["quick_sim_by_day"] = days_present
    out["quick_sim_total_days"] = len(days_present)

    # 3. asset 大小写一致性
    rows = conn.execute(
        """SELECT asset, COUNT(*) as n
           FROM quick_sim_trades
           WHERE entry_ts > strftime('%s','now', ?)
           GROUP BY asset ORDER BY asset""",
        (f"-{int(lookback_days)} day",),
    ).fetchall()
    out["quick_sim_assets"] = {r[0]: r[1] for r in rows}

    # 4. missing fields 占比
    r = conn.execute(
        """SELECT
              COUNT(*) as total,
              SUM(CASE WHEN context_json IS NULL OR context_json = '{}' THEN 1 ELSE 0 END) as empty_ctx
           FROM trade_decisions
           WHERE decision_ts > strftime('%s','now', ?)""",
        (f"-{int(lookback_days)} day",),
    ).fetchone()
    total = r[0] or 0
    empty = r[1] or 0
    out["decisions_total"] = total
    out["decisions_empty_ctx"] = empty
    out["decisions_empty_ctx_pct"] = (
        round(100.0 * empty / total, 1) if total else 0.0
    )

    # 5. 打点缺失告警
    out["alerts"] = []
    if total == 0:
        out["alerts"].append(
            "⚠️ trade_decisions 没有打点！打点可能挂了"
        )
    if out["decisions_empty_ctx_pct"] > 50:
        out["alerts"].append(
            f"⚠️ {out['decisions_empty_ctx_pct']}% 的打点 context 为空，归因会失败"
        )
    if len(days_present) < lookback_days * 0.5:
        out["alerts"].append(
            f"⚠️ 最近 {lookback_days} 天只有 {len(days_present)} 天有 quick_sim 记录"
        )
    return out


__all__ = [
    # A 线 — 决策打点与统计
    "init_schema",
    "log_decision",
    "get_stats_by_asset_action",
    "get_overall_stats",
    "get_mfe_mae_stats",
    "get_decision_volume",
    "get_data_quality_report",
    # B 线 — AI 闸门分析
    "get_ai_decision_gate_breakdown",
    "get_gate_shadow_projection",
    "DEFAULT_GATE",
    "LEGACY_GATE",
    "BOOST_MULTIPLIER",
]
