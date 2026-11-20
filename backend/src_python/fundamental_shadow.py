#!/usr/bin/env python3
"""基本面权重（0.08）的影子账本 —— 让"这条权重到底有没有用"变成可结算的数字。

为什么必须单独记一本账：
  · 决策表里 `decision_context` 是市场快照，`factor_snapshots.factors` 只存分数，
    **没有一处把"基本面到底贡献了多少"落下来** —— 实测 870 条快照里 fundamental
    出现 386 次、值全为 0.0，复盘根本看不出它是"没数据"还是"有数据但没影响"。
  · 0.08 这个权重是先验拍出来的（v405），没有任何回测支撑。要把它从"拍脑袋"
    变成"有证据"，唯一可行的路子是：决策时同时记下**含基本面**和**不含基本面**
    两个方向分，事后按同一批行情结算，比较两组的命中率。

计算口径（与 decision_guard 保持一致）：
  dir_with    = Σ(score·weight，含 fundamental) / Σweight(仅可得因子)
  dir_without = Σ(score·weight，不含 fundamental) / Σweight(不含 fundamental 的可得因子)
  action_*    用闸门同一组阈值（guard["thresholds"]）还原，避免两套尺子。

结算：用 `ohlcv_daily` 的日线收盘算 +1/+3/+5/+10/+20 天的前向收益；HOLD 不计命中，
        缺行情就保持 settled=0（不拿 0 当结果）。

v454 修正三处结构性问题（实测 164 行、`fund_available` 全 0、`settled` 全 0）：
  1) **适用性**：原来 XAU/WTI/BTC/NONE 这些"基本面本来就不适用"的决策也照样入账，
     164 行里只有 1 行是 US_STOCK —— 样本池被不适用行稀释，"含/不含"永远比不出来。
     现在记 `applicable`（= 基本面当时是否可用）与 `reason`（不可用的原因），
     结算只认 applicable=1，不适用行标 settled=-1 后不再重扫。
  2) **结算真的会跑**：原来 asset='US_STOCK' 但 ticker 为空 → 永远找不到行情 →
     永远 settled=0。现在结算按 ticker 优先、asset 兜底，且没有 ticker 的票直接判不适用。
  3) **周期错配**：0.08 权重被塞进一个 2 小时前向验证的漏斗，而季报是季度慢变量。
     账本自己的结算基准改为 **+10 天**（`_PRIMARY_HORIZON`），1/3/5/20 天只作对照 ——
     用 3 天命中率去判季度因子的死活，本身就是错配。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

_PREFIX = "fundamental"
_FUND = "fundamental"
_MIN_N = 30          # 少于 30 笔不下结论（README 的 60 笔门槛对"组间比较"更严，这里先给方向）
_HORIZONS = (1, 3, 5, 10, 20)
_PRIMARY_HORIZON = 10   # 季报级慢变量：主判据用 +10 天，不用 3 天（v454）

# v490：深度护栏白名单。只认这些标签，避免把 growth_premium / earnings_window
# 这类"状态标签"误当成生意质量恶化。
_DEPTH_FLAGS = {
    "gm_down", "opm_down", "negative_fcf", "high_leverage", "negative_equity",
    "rev_down", "np_down", "roe_down",
}


def _depth_fields(comp: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[int]]:
    """从基本面因子输出里抽「深度护栏 / 盈利位置 / 样本期数」。

    两个来源：
      · 美股 `components["depth_flags"]`（company_depth 直接给的）；
      · A/H `flags`（cn_depth 把护栏放在顶层 flags）。
    只保留白名单标签；`earnings_peak` 不是风险信号，不进来。
    """
    comps = comp.get("components") or {}
    raw = comps.get("depth_flags")
    if not isinstance(raw, list):
        raw = comp.get("flags") or []
    flags = sorted({str(f) for f in raw if str(f) in _DEPTH_FLAGS})
    zone = comps.get("depth_earnings_zone") or comps.get("earnings_zone") or comp.get("earnings_zone")
    points = comps.get("depth_points")
    try:
        points = int(points) if points is not None else None
    except (TypeError, ValueError):
        points = None
    return json.dumps(flags, ensure_ascii=False), (str(zone) if zone else None), points


def _connect(readonly: bool = False) -> sqlite3.Connection:
    import config as _cfg
    if readonly:
        return sqlite3.connect(f"file:{_cfg.DB_PATH}?mode=ro", uri=True, timeout=5)
    import db as _db
    return _db.get_connection()


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fundamental_shadow (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER,
            news_id INTEGER,
            ts INTEGER,
            asset TEXT,
            ticker TEXT,
            action TEXT,              -- 实际落库的动作
            action_with TEXT,         -- 含基本面权重还原的动作
            action_without TEXT,      -- 剔除基本面权重还原的动作
            dir_with REAL,
            dir_without REAL,
            fund_score REAL,
            fund_available INTEGER,
            flags TEXT,
            components TEXT,
            ret_1d REAL,
            ret_3d REAL,
            ret_5d REAL,
            ret_10d REAL,
            ret_20d REAL,
            hit_with INTEGER,
            hit_without INTEGER,
            applicable INTEGER DEFAULT 0,   -- v454：基本面当时是否真的可用
            reason TEXT,                    -- v454：不可用的原因（non_equity/placeholder/...）
            settled INTEGER DEFAULT 0,
            eval_ts INTEGER,
            UNIQUE(decision_id)
        )"""
    )
    # v454 迁移：老库没有这几列 —— 幂等补列，绝不重建表（重建会丢历史行）。
    have = {row[1] for row in conn.execute("PRAGMA table_info(fundamental_shadow)")}
    for col, decl in (("ret_10d", "REAL"), ("ret_20d", "REAL"),
                      ("applicable", "INTEGER DEFAULT 0"), ("reason", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE fundamental_shadow ADD COLUMN {col} {decl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fundamental_shadow_ts ON fundamental_shadow(ts)"
    )
    # v476：生效门槛必须能查 —— v475 的门槛只在决策返回值里出现，复盘看不到
    # "这次为什么该 BUY 却没 BUY"。表独立于影子账本（门槛是闸门属性，不是基本面属性）。
    conn.execute(
        """CREATE TABLE IF NOT EXISTS decision_threshold_snapshot (
               decision_id INTEGER PRIMARY KEY,
               ts INTEGER NOT NULL,
               asset TEXT,
               action TEXT,
               buy_min REAL,
               sell_min REAL,
               buy_min_calibrated REAL,
               sell_min_calibrated REAL,
               buy_min_effective REAL,
               sell_min_effective REAL,
               confidence REAL,
               confidence_penalty REAL,
               action_before_gate TEXT,
               action_after_gate TEXT,
               gate_changed INTEGER DEFAULT 0
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_threshold_snapshot_ts ON decision_threshold_snapshot(ts)"
    )


def _dispersion_from_components(fund_score: Optional[float],
                                comps: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """v476：写入前的兜底重算 —— 与 v472/v474 完全同一公式。

    为什么需要：`_record_snapshot` 原先从 `comp`（= guard["fundamental"]）里取
    dispersion，但它属于顶层键；某些路径（分量不足/老版本读数）到落库时是 None，
    结果 205 行全空 —— 列建了却永远为空，等于没建。
    这里按分量 score/weight 重算一遍，保证"有分量就一定有分歧度"。
    """
    vals = []
    for c in (comps or {}).values():
        if not isinstance(c, dict):
            continue
        s, w = c.get("score"), c.get("weight")
        if s is None or w is None:
            continue
        try:
            vals.append((float(s), float(w)))
        except (TypeError, ValueError):
            continue
    usable = [(s, w) for s, w in vals if w > 0]
    if len(usable) < 2:
        return None
    wsum = sum(w for _, w in usable)
    mean = sum(s * w for s, w in usable) / wsum
    disp = (sum(w * (s - mean) ** 2 for s, w in usable) / wsum) ** 0.5
    level = "高" if disp >= 0.60 else ("中" if disp >= 0.35 else "低")
    return {
        "dispersion": round(disp, 3),
        "dispersion_level": level,
        "confidence": round(max(0.0, 1.0 - disp / 1.5), 3),
        "bull_weight": round(sum(w for s, w in usable if s > 0.05), 2),
        "bear_weight": round(sum(w for s, w in usable if s < -0.05), 2),
    }


def _record_thresholds(conn: sqlite3.Connection, decision_id: int, asset: str,
                       guard: Dict[str, Any]) -> None:
    """v476：把这次决策实际生效的门槛落一行。

    复盘最常问的三个问题——"为什么没买 / 是不是被可信度压掉了 / 原始门槛是多少"，
    以前都只能从 reasoning 文字里猜。这里三个数并排存（校准前 / 校准后 / 生效）。
    """
    try:
        th = guard.get("thresholds") or {}
        fund = guard.get("fundamental") or {}
        before = th.get("action_before_confidence_gate")
        after = str(guard.get("action") or "")
        conn.execute(
            """INSERT OR REPLACE INTO decision_threshold_snapshot
               (decision_id, ts, asset, action, buy_min, sell_min,
                buy_min_calibrated, sell_min_calibrated,
                buy_min_effective, sell_min_effective,
                confidence, confidence_penalty, action_before_gate, action_after_gate,
                gate_changed)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(decision_id), int(time.time()), str(asset or "").upper(), after,
             th.get("buy_min"), th.get("sell_min"),
             th.get("buy_min_calibrated"), th.get("sell_min_calibrated"),
             th.get("buy_min_effective"), th.get("sell_min_effective"),
             fund.get("confidence"), th.get("confidence_penalty"),
             str(before) if before else None, after,
             1 if (before and str(before) != after) else 0),
        )
    except Exception as exc:
        # v452：不许静默 —— 门槛快照落库失败时必须留痕，否则"为什么没买"
        # 这条复盘线索会无声消失（scripts/audit_silent_except.py 会直接判红）。
        print(f"  [SHADOW] 决策门槛快照落库跳过: {type(exc).__name__}: {str(exc)[:80]}",
              flush=True)


def _action_from_dir(direction: float, thresholds: Dict[str, Any]) -> str:
    """用闸门自己的阈值还原动作，不另造一套。"""
    try:
        buy = float(thresholds.get("buy_min", 0.35))
        sell = float(thresholds.get("sell_min", -0.35))
    except (TypeError, ValueError):
        buy, sell = 0.35, -0.35
    if direction >= buy:
        return "BUY"
    if direction <= sell:
        return "SELL"
    return "HOLD"


def compute_ab(guard: Dict[str, Any]) -> Dict[str, Any]:
    """从闸门输出还原「含/不含基本面」两个方向分。纯函数，便于离线测试。"""
    factors = guard.get("factors") or {}
    thresholds = guard.get("thresholds") or {}
    num_with = num_without = 0.0
    w_with = w_without = 0.0
    for name, item in factors.items():
        if not isinstance(item, dict) or not item.get("available"):
            continue
        try:
            score = float(item.get("score") or 0.0)
            weight = float(item.get("weight") or 0.0)
        except (TypeError, ValueError):
            continue
        if weight <= 0:
            continue
        num_with += score * weight
        w_with += weight
        if name != _FUND:
            num_without += score * weight
            w_without += weight
    dir_with = round(num_with / w_with, 4) if w_with else 0.0
    dir_without = round(num_without / w_without, 4) if w_without else 0.0
    fund = factors.get(_FUND) or {}
    return {
        "dir_with": dir_with,
        "dir_without": dir_without,
        "action_with": _action_from_dir(dir_with, thresholds),
        "action_without": _action_from_dir(dir_without, thresholds),
        "fund_available": 1 if fund.get("available") else 0,
        "fund_score": round(float(fund.get("score") or 0.0), 4) if fund else 0.0,
        "fund_weight": round(float(fund.get("weight") or 0.0), 4) if fund else 0.0,
        "fund_detail": str(fund.get("explanation") or "")[:120],
        "coverage_with": round(w_with, 4),
        "coverage_without": round(w_without, 4),
    }


def record(conn: sqlite3.Connection, decision_id: Optional[int], news_id: Optional[int],
           asset: str, guard: Dict[str, Any], action: str,
           flags: Optional[List[str]] = None,
           components: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """决策时落一行影子账。任何异常都吞掉 —— 记账不得影响主判定。

    v454：同时记 `applicable` 与 `reason`。不适用行照样入账（审计要能看见"这条决策
    为什么没有基本面"），但不进样本池 —— 否则 164 行里只有 1 行真样本，报告却写
    "样本 164"。
    """
    if not decision_id:
        return None
    try:
        ab = compute_ab(guard)
        comp = components or {}
        ticker = str(comp.get("ticker") or "") or None
        reason = str(comp.get("reason") or comp.get("detail") or "")[:120]
        ensure_table(conn)
        applicable = 1 if ab["fund_available"] else 0
        conn.execute(
            """INSERT OR IGNORE INTO fundamental_shadow
               (decision_id, news_id, ts, asset, ticker, action, action_with, action_without,
                dir_with, dir_without, fund_score, fund_available, flags, components,
                applicable, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(decision_id), int(news_id) if news_id else None, int(time.time()),
             str(asset or "").upper(), ticker,
             str(action or "HOLD"), ab["action_with"], ab["action_without"],
             ab["dir_with"], ab["dir_without"], ab["fund_score"], ab["fund_available"],
             json.dumps(flags or [], ensure_ascii=False),
             json.dumps(comp, ensure_ascii=False, default=str),
             applicable, reason),
        )
        conn.commit()
        _record_snapshot(conn, int(decision_id), asset, ticker, ab, comp)
        # v476：门槛与分数线一起落 —— 复盘要能回答"是被门槛挡住还是本来就没到"
        _record_thresholds(conn, int(decision_id), asset, guard)
        return 1
    except Exception as exc:
        print(f"  [SHADOW] 基本面影子记录跳过: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
        return None


def sizing_multiplier(fund_score: Optional[float], available: bool,
                      confidence: Optional[float] = None) -> float:
    """v456 仓位建议乘数（**影子**：只记账，不改实际下注）。

    为什么先做影子：基本面现在只进方向分（权重 0.08），一个"估值处历史最贵 + 盈利下修"
    的票和"最便宜 + 上修"的票下注一样大 —— 这是明显的信息浪费。但直接改仓位是改交易
    逻辑，没有证据就先动，代价不对称（错一次是真金白银）。所以：先把"如果按基本面调
    仓位，会调成几倍"记下来，等 A/B 有了样本再决定是否放开。

    口径（简单到能被复核）：
        乘数 = (1 + 0.20 × fund_score) × (0.75 + 0.25 × confidence)，截到 [0.70, 1.20]
    score ∈ [-1,1] → 最多 ±20%；confidence ∈ [0,1]（v472 的分歧度可信度）→ 最多再打 0.75 折。

    为什么加第二项（v473）：同一个分数，"全体一致"和"一半极多一半极空"不是一回事。
    分歧大的品种信息量低，仓位跟着打折才合理；confidence 为 None（公司侧老路径）
    时该项恒为 1.0，行为与 v456 完全一致 —— 不给公司侧偷偷改行为。
    """
    if not available or fund_score is None:
        return 1.0
    try:
        mult = 1.0 + 0.20 * float(fund_score)
    except (TypeError, ValueError):
        return 1.0
    mult = max(0.80, min(1.20, mult))          # 先按 v456 口径截断，公司侧行为必须逐位一致
    if confidence is not None:
        try:
            conf = max(0.0, min(1.0, float(confidence)))
            mult *= (0.75 + 0.25 * conf)
        except (TypeError, ValueError):
            pass
        mult = max(0.70, min(1.20, mult))      # 打折后才放开到 0.70 下限
    return round(mult, 4)


def _record_snapshot(conn: sqlite3.Connection, decision_id: int, asset: str,
                     ticker: Optional[str], ab: Dict[str, Any],
                     comp: Dict[str, Any]) -> None:
    """v456 埋点：把"决策当时的基本面上下文"单独落一行，供离线 A/B 用。

    为什么要这张表：`ai_decisions` **没有**标的代码列（实际只有 target_asset /
    asset_impact / asset_route 等文本字段），所以历史决策没法跟"当时那只票的分数"
    对上号 —— 这是离线 A/B 一直做不了的前置条件，不是算法问题。从这张表开始，
    A/B 就有"决策 ID ↔ ticker ↔ 当时分数及各分量"的确定映射，且不依赖回填猜测。
    """
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS decision_fundamental_snapshot (
                   decision_id INTEGER PRIMARY KEY,
                   ts INTEGER NOT NULL,
                   asset TEXT,
                   ticker TEXT,
                   market TEXT,
                   fund_score REAL,
                   fund_available INTEGER,
                   valuation REAL,
                   valuation_basis TEXT,
                   peer_rel_score REAL,
                   peer_sector TEXT,
                   macro_tilt REAL,
                   pb_excluded TEXT,
                   reason TEXT,
                   sizing_mult REAL,
                   components TEXT,
                   dispersion REAL,
                   dispersion_level TEXT,
                   confidence REAL,
                   bull_weight REAL,
                   bear_weight REAL,
                   depth_flags TEXT,
                   depth_zone TEXT,
                   depth_points INTEGER
               )""")
        try:
            # 老库幂等补列（v456 之前建的表没有 sizing_mult）
            conn.execute("ALTER TABLE decision_fundamental_snapshot ADD COLUMN sizing_mult REAL")
        except sqlite3.OperationalError:
            pass
        # v473：分歧度也要落库 —— 复盘时才能区分"这个仓位是按共识给的"还是"按吵架给的"
        for _col, _typ in (("dispersion", "REAL"), ("dispersion_level", "TEXT"),
                           ("confidence", "REAL"), ("bull_weight", "REAL"),
                           ("bear_weight", "REAL")):
            try:
                conn.execute(f"ALTER TABLE decision_fundamental_snapshot ADD COLUMN {_col} {_typ}")
            except sqlite3.OperationalError:
                pass
        # v490：深度护栏 + 盈利位置 + 样本期数（幂等补列，绝不重建）
        for _col, _typ in (("depth_flags", "TEXT"), ("depth_zone", "TEXT"),
                           ("depth_points", "INTEGER")):
            try:
                conn.execute(f"ALTER TABLE decision_fundamental_snapshot ADD COLUMN {_col} {_typ}")
            except sqlite3.OperationalError:
                pass
        # v476：分歧度优先用现成读数；缺失则按分量重算（否则列永远是空）
        _d = {
            "dispersion": comp.get("dispersion"),
            "dispersion_level": comp.get("dispersion_level"),
            "confidence": comp.get("confidence"),
            "bull_weight": comp.get("bull_weight"),
            "bear_weight": comp.get("bear_weight"),
        }
        # v497：comp 顶层只带 dispersion/confidence，不带 bull/bear；若仅因
        # dispersion 已有值就跳过重算，bull_weight/bear_weight 会永远留 0。
        # 只要分歧度、多空权重任一缺失，就按分量重算补齐（不覆盖已有读数）。
        if (_d["dispersion"] is None or _d["bull_weight"] is None
                or _d["bear_weight"] is None):
            _re = _dispersion_from_components(ab.get("fund_score"), comp.get("components") or {})
            if _re:
                for _k in ("dispersion", "dispersion_level", "confidence",
                           "bull_weight", "bear_weight"):
                    if _d[_k] is None:
                        _d[_k] = _re[_k]
        _depth_flags, _depth_zone, _depth_points = _depth_fields(comp)
        conn.execute(
            """INSERT OR REPLACE INTO decision_fundamental_snapshot
               (decision_id, ts, asset, ticker, market, fund_score, fund_available,
                valuation, valuation_basis, peer_rel_score, peer_sector, macro_tilt,
                pb_excluded, reason, sizing_mult, components,
                dispersion, dispersion_level, confidence, bull_weight, bear_weight,
                depth_flags, depth_zone, depth_points)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(decision_id), int(time.time()), str(asset or "").upper(),
             ticker or None, str(comp.get("market") or "") or None,
             ab.get("fund_score"), 1 if ab.get("fund_available") else 0,
             comp.get("valuation_percentile"), json.dumps(
                 comp.get("valuation_basis") or [], ensure_ascii=False),
             comp.get("peer_rel_score"), comp.get("peer_sector"),
             comp.get("macro_tilt"), comp.get("pb_excluded"),
             str(comp.get("reason") or comp.get("detail") or "")[:120],
             sizing_multiplier(ab.get("fund_score"), bool(ab.get("fund_available")),
                               comp.get("confidence")),
             json.dumps(comp, ensure_ascii=False, default=str),
             _d.get("dispersion"), _d.get("dispersion_level"), _d.get("confidence"),
             _d.get("bull_weight"), _d.get("bear_weight"),
             _depth_flags, _depth_zone, _depth_points),
        )
        conn.commit()
    except Exception as exc:
        print(f"  [SHADOW] 决策基本面快照跳过: {type(exc).__name__}: {str(exc)[:60]}", flush=True)


def _close_at_or_before(conn: sqlite3.Connection, sym: str, ts: int) -> Optional[Tuple[int, float]]:
    """该时刻**之前最后一根**日线收盘（含当日）。

    原来的实现是"在 ts±12h 里找最接近的一根" —— 决策发生在 20:00、日线在 00:00 时
    差 20 小时，直接找不到 → base 为 None → 所有行永远 settled=0。实测 164 行
    全军覆没就是这个原因之一。用"之前最后一根"既保证取得到，也不引入未来数据。
    """
    try:
        row = conn.execute(
            """SELECT ts, close FROM ohlcv_daily
               WHERE asset=? AND ts <= ? AND close > 0
               ORDER BY ts DESC LIMIT 1""",
            (sym, int(ts)),
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return int(row[0]), float(row[1])


_STOCK_MARKETS = ("US_STOCK", "HK_STOCK", "CN_STOCK")


def backfill_reason(conn: sqlite3.Connection) -> int:
    """给 v454 之前入账的老行补上「为什么没有基本面」。

    老行只有 `applicable`（默认 0）和 `fund_available`，没有 `reason` → 汇总里 164 行
    全落进 "unknown"，读起来像"原因不明"，其实原因就在 asset 列写着（XAU/BTC/WTI
    本来就没有财报）。这里只做**分类**，不改任何分数、不改适用性判定，也不动已有 reason 的行。
    """
    try:
        ensure_table(conn)
        rows = conn.execute(
            """SELECT id, asset, fund_available FROM fundamental_shadow
               WHERE reason IS NULL OR reason = ''""").fetchall()
    except Exception as exc:
        print(f"  [SHADOW] 原因回填跳过: {type(exc).__name__}: {str(exc)[:60]}", flush=True)
        return 0
    plan = []
    for rid, asset, avail in rows:
        a = str(asset or "").upper()
        if int(avail or 0):
            continue                      # 当时基本面可用 → 不需要"不可用原因"
        elif a in ("NONE", "", "NULL"):
            plan.append((rid, "no_asset"))
        elif a in ("CN_STOCK", "HK_STOCK"):
            plan.append((rid, "placeholder_cn"))
        elif a in _STOCK_MARKETS:
            plan.append((rid, "no_fund_data"))
        else:
            plan.append((rid, "non_equity"))
    if plan:
        conn.executemany("UPDATE fundamental_shadow SET reason=? WHERE id=?",
                         [(r, i) for i, r in plan])
        conn.commit()
    return len(plan)


def evaluate(conn: sqlite3.Connection, days: int = 30, now_ts: Optional[int] = None) -> int:
    """回填前向收益与命中。

    · 不适用行（`applicable=0`）标 settled=-1，不再重扫 —— 它们不该占样本池，
      但必须留痕（"这条决策为什么没有基本面"本身是缺口证据）；
    · 适用行用「决策前最后一根收盘」当基准，按 +1/+3/+5/+10/+20 天结算；
      主判据取 +10 天（`_PRIMARY_HORIZON`），其余只作对照；
    · 缺行情保持 settled=0（不拿 0 冒充结果）。
    """
    now = int(now_ts or time.time())
    cutoff = now - days * 86400
    ensure_table(conn)          # v454 新列在老库上要靠这一步幂等 ALTER 出来
    backfill_reason(conn)       # 老行补原因分类（只分类、不改判定）
    rows = conn.execute(
        """SELECT id, ts, asset, ticker, action_with, action_without,
                  COALESCE(applicable, 0), COALESCE(fund_available, 0)
           FROM fundamental_shadow
           WHERE settled=0 AND ts <= ? AND ts >= ?""",
        (now - 86400, cutoff),
    ).fetchall()
    updated = 0
    for rid, ts, asset, ticker, a_with, a_without, applicable, avail in rows:
        if not (applicable or avail):
            conn.execute("UPDATE fundamental_shadow SET settled=-1, eval_ts=? WHERE id=?",
                         (now, rid))
            continue
        sym = str(ticker or "").upper() or str(asset or "").upper()
        if not sym or sym in ("NONE", "US_STOCK", "CN_STOCK", "INDIVIDUAL_STOCK"):
            conn.execute("UPDATE fundamental_shadow SET settled=-1, eval_ts=? WHERE id=?",
                         (now, rid))
            continue
        base = _close_at_or_before(conn, sym, int(ts))
        if base is None or base[1] <= 0:
            continue
        base_ts, base_px = base
        rets: Dict[int, Optional[float]] = {}
        for h in _HORIZONS:
            # 窗口从**决策时刻**起算（不是从基准那根 K 线起算）：
            # 用 base_ts 起算会把 10 天变成 9 天 23 小时，10 天那根落在窗口外取不到。
            target = int(ts) + h * 86400
            bar = _close_at_or_before(conn, sym, target)
            if bar is None:
                # 还没走到这个窗口（不是"没数据"，是不该结算）→ 留空
                rets[h] = None
                continue
            # 该根必须确实落在 h 天之后的大半段，否则 1d 和 3d 会取到同一根
            if bar[0] < int(ts) + max(1, h // 2) * 86400:
                rets[h] = None
                continue
            rets[h] = (bar[1] / base_px - 1.0) * 100.0
        primary = next((rets[h] for h in (10, 5, 3, 1) if rets.get(h) is not None), None)
        if primary is None:
            continue

        def _hit(act: Optional[str]) -> Optional[int]:
            if not act or act == "HOLD":
                return None                      # HOLD 不参与命中统计
            return 1 if ((act == "BUY" and primary > 0) or (act == "SELL" and primary < 0)) else 0

        conn.execute(
            """UPDATE fundamental_shadow SET ret_1d=?, ret_3d=?, ret_5d=?, ret_10d=?, ret_20d=?,
                      hit_with=?, hit_without=?, settled=1, eval_ts=? WHERE id=?""",
            (rets[1], rets[3], rets[5], rets[10], rets[20],
             _hit(a_with), _hit(a_without), now, rid),
        )
        updated += 1
    conn.commit()
    return updated


def _hit_of(ret_pct: Optional[float], act: Optional[str]) -> Optional[int]:
    if ret_pct is None or not act or act == "HOLD":
        return None
    return 1 if ((act == "BUY" and ret_pct > 0) or (act == "SELL" and ret_pct < 0)) else 0


def _rate(hits: List[int]) -> Optional[float]:
    return round(100.0 * sum(hits) / len(hits), 1) if hits else None


def summarize(conn: sqlite3.Connection, days: int = 30) -> Dict[str, Any]:
    """账本快照。样本池只算 applicable（基本面真的可用的行），其余单列不适用数。"""
    cutoff = int(time.time()) - days * 86400
    try:
        conn.row_factory = sqlite3.Row      # 下面按列名取值；tuple 连接也能用
    except Exception:
        pass
    rows = conn.execute(
        """SELECT action, action_with, action_without, fund_available, fund_score,
                  applicable, reason, ret_1d, ret_3d, ret_5d, ret_10d, ret_20d, settled
           FROM fundamental_shadow WHERE ts >= ?""", (cutoff,),
    ).fetchall()

    def _get(row: Any, key: str) -> Any:
        try:
            return row[key]
        except Exception:
            return row[0] if isinstance(row, (tuple, list)) else None

    total = len(rows)
    applicable = [r for r in rows if (_get(r, "applicable") or 0) or (_get(r, "fund_available") or 0)]
    reasons: Dict[str, int] = {}
    for r in rows:
        if (r["applicable"] or 0) or (r["fund_available"] or 0):
            continue
        key = (r["reason"] or "unknown")[:36]
        reasons[key] = reasons.get(key, 0) + 1

    by_horizon: Dict[str, Any] = {}
    for h in _HORIZONS:
        ret_key = f"ret_{h}d"
        hw = [_hit_of(r[ret_key], r["action_with"]) for r in applicable if r["applicable"]]
        hv = [_hit_of(r[ret_key], r["action_without"]) for r in applicable if r["applicable"]]
        hw = [x for x in hw if x is not None]
        hv = [x for x in hv if x is not None]
        by_horizon[f"{h}d"] = {
            "n_with": len(hw), "n_without": len(hv),
            "hit_with": _rate(hw), "hit_without": _rate(hv),
            "delta": (round(_rate(hw) - _rate(hv), 1)
                      if hw and hv and _rate(hw) is not None and _rate(hv) is not None
                      else None),
        }

    settled_rows = [r for r in applicable if (_get(r, "settled") or 0) == 1]
    changed = [r for r in settled_rows if r["action_with"] != r["action_without"]]
    primary = by_horizon.get(f"{_PRIMARY_HORIZON}d") or {}
    return {
        "days": days,
        "samples": total,
        "applicable": len(applicable),
        "not_applicable": total - len(applicable),
        "not_applicable_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:5]),
        "settled": len(settled_rows),
        "fund_available": sum(1 for r in rows if r["fund_available"]),
        "actions_changed": len(changed),
        "primary_horizon": f"{_PRIMARY_HORIZON}d",
        "hit_rate_with": primary.get("hit_with"),
        "hit_rate_without": primary.get("hit_without"),
        "n_with": primary.get("n_with"),
        "n_without": primary.get("n_without"),
        "by_horizon": by_horizon,
        "verdict": _verdict(primary.get("n_with") or 0, primary.get("n_without") or 0,
                            [1] * 0, [1] * 0, primary.get("delta")),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _verdict(n_with: int, n_without: int, hw: List[int], hv: List[int],
             delta: Optional[float] = None) -> Dict[str, Any]:
    if min(n_with, n_without) < _MIN_N:
        return {"code": "insufficient",
                "text": f"样本不足（含基本面 {n_with} / 不含 {n_without}，门槛 {_MIN_N}），"
                        f"权重 0.08 仍属先验"}
    if delta is None:
        return {"code": "insufficient", "text": "命中率无法计算（缺前向收益）"}
    if delta >= 5:
        return {"code": "helps", "delta": round(delta, 1),
                "text": f"含基本面命中率高 {delta:+.1f}pt"}
    if delta <= -5:
        return {"code": "hurts", "delta": round(delta, 1),
                "text": f"含基本面命中率低 {delta:+.1f}pt"}
    return {"code": "flat", "delta": round(delta, 1),
            "text": f"两组命中率差 {delta:+.1f}pt，权重基本没筛出东西"}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(description="基本面权重影子账本：结算 + 报告")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    conn = _connect()
    try:
        ensure_table(conn)
        updated = evaluate(conn, days=args.days)
        stats = summarize(conn, days=args.days)
        out_path = args.report or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "reports", "fundamental_shadow", "latest.json")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(stats, fh, ensure_ascii=False, indent=2)
        print(f"[基本面影子] 结算 {updated} 条 · 样本 {stats['samples']} · "
              f"命中(含/不含)= {stats['hit_rate_with']}/{stats['hit_rate_without']} · "
              f"{stats['verdict']['text']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
