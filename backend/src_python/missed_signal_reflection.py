#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
missed_signal_reflection.py — v321.3 P0: 把「漏检信号」真正写进反思系统 (需求 #7)
================================================================================

问题 (实测, 不是推测)
----------------------
用户需求 #7: 「归因页出现的行情, 分析页(实时信号台) 没识别出信号要进反思系统」。

现状: 需求只实现了**一半**。

  ✅ 已有: 前端三件套 (MissedSignalsByDimBanner / DimensionSummary / Panel)
           + app/missed-signals 独立页 + missed_signals 表的 A/B/C 分类器
  ❌ 缺失: 漏检信号**进不了反思表**。

实测 (生产库):
    ai_decisions.missed_signal = 1           1582 条   (全部是 HOLD 决策)
    trade_reflections_v2 里带 ai_decision_id 的   2013 条
    两者交集                                       0 条

根因
----
paper_reflection_worker._DEDUPE_SELECT:

    FROM ai_decisions a
    JOIN quick_sim_trades qs ON qs.decision_id = a.id   <- INNER JOIN
     WHERE a.paper_trading_run_id IS NOT NULL
       AND qs.settled = 1                                <- 只反思已结算的成交

HOLD 决策**永远不会开仓** -> 永远不进 quick_sim_trades -> INNER JOIN 把它们
全部排除 -> 漏检信号天然进不了反思系统。

而 HOLD 恰恰是漏检的定义本身 (「本应有 BUY/SELL, 但 AI 给了 HOLD」)。

修法
----
本模块走**独立写入路径**, 不依赖 quick_sim_trades:

  1. 取 ai_decisions WHERE missed_signal = 1
  2. 用**与 missed_signal_taxonomy 完全一致的证据规则**分类 A/B/C + 12 小类
     (同一套词表 -> 漏检面板与反思面板可以直接对齐统计)
  3. 写入 V482 起的独立记账表 `missed_signal_reflections`（**不再写 trade_reflections_v2**）：
     漏检没有成交、没有 pnl，混进反思表只会把"反思条数/深度分"拉平（实测 93% 是它）
  4. 幂等: ON CONFLICT(decision_id) DO UPDATE

分类规则 (按优先级, 都要有证据支撑)
----------------------------------
  1. C1 feed_missing          异动时刻该资产无任何行情数据
  2. C3 signal_delay          决策发生太久 (>30 天), 超出实时检测窗口
  3. B1 news_direction_inverted 同期有新闻, 但新闻方向与实际走势相反
  4. B2 event_chain_break      同期有同向新闻, 但事件链没把它转成决策
  5. C2 calibration_drift      置信度很高, 但后续实际几乎没动 (信号本身虚高)
  6. A2 market_regime_misread  该资产当时有因果链, 但信号没命中链的触发条件
  7. A1 pattern_recognition   以上都不是: 纯形态/强度信号, 无催化无链

用法
----
    python3 missed_signal_reflection.py --once                 # 增量 (最近 24h)
    python3 missed_signal_reflection.py --backfill --days 120  # 历史回填
"""
from __future__ import annotations

import argparse
import calendar
import datetime as _dt
import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

try:
    import db as _db
except ImportError:  # pragma: no cover
    _db = None  # type: ignore

try:  # v326: 词表单一权威源 —— 别在这里再抄一份 (两侧抄两遍就是 v325 前 A 类塌桶的成因之一)
    from review import reflection_dimension_map as _rdm
except ImportError:  # pragma: no cover
    _rdm = None  # type: ignore

try:
    from cem_constants import CROSS_ASSET_GROUPS as _CROSS_GROUPS
except ImportError:  # pragma: no cover
    _CROSS_GROUPS = {
        "crypto": ["BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "BNB"],
        "metal": ["XAU"], "energy": ["WTI"],
        "us_tech": ["NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META"],
        "memory": ["MU", "SKHYNIX"],
    }

# 受控词表统一取自 reflection_dimension_map (A/B/C 各小类); 导入失败才用内联兜底。
if _rdm is not None:
    _VALID_SUBS = {s for subs in _rdm.SUBCATEGORIES_BY_DIMENSION.values() for s in subs}
else:  # pragma: no cover
    _VALID_SUBS = {
        "market_regime_misread", "pattern_recognition", "psychological_anchor",
        "volume_price_divergence", "risk_limit_blocked", "batch_conflict_blocked",
        "strength_gate_blocked", "cause_effect_reversed", "event_chain_break",
        "macro_misattribute", "news_direction_inverted", "calibration_drift",
        "data_feed_timeout", "feed_missing", "signal_delay",
    }

# 同组联动判定 (v326 A 类细分): 同一分组内 >=MIN 个资产在 ±窗口 内同向异动,
# 才算"板块级波动"→ 市场状态读错; 否则算单资产形态没识别。
PEER_CO_MOVE_MIN = 2
PEER_WINDOW_SEC = 7200
PEER_MIN_MOVE_PCT = 1.5
_VALID_DIMS = {"A", "B", "C"}

# 漏检反思的标记 (区分本模块写的行 vs paper_reflection_worker 写的行)
MISSED_SIGNAL_NAME = "missed_signal"

OUTCOME_WINDOW_DAYS = 5          # 决策后 N 天算"实际结果"
HIT_MOVE_THRESHOLD_PCT = 1.0      # 实际涨跌超过这个幅度 -> hit=1 (这次漏检是真的)
CALIBRATION_DRIFT_MIN_CONF = 0.7  # 置信度高但后续没动 -> 校准漂移
CALIBRATION_DRIFT_MAX_MOVE_PCT = 0.3
SIGNAL_DELAY_DAYS = 30            # 超出这个天数的决策不再视为"实时漏检"


def _conn() -> sqlite3.Connection:
    if _db is not None:
        conn = _db.get_connection()
    else:
        conn = sqlite3.connect(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "trident_event_bus.db"), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def _norm_action(action: Any) -> str:
    a = str(action or "").strip().upper()
    if a in ("BUY", "LONG", "BULLISH"):
        return "BUY"
    if a in ("SELL", "SHORT", "BEARISH"):
        return "SELL"
    return ""


def _sign_of(value: Any) -> int:
    """取符号。**缺失就是缺失**, 绝不把 None 当 0.0 (v313 诚实宪法)。"""
    if value is None:
        return 0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 0

    return 0


def _has_price_bar(conn: sqlite3.Connection, asset: str, ts: int) -> Optional[bool]:
    """该资产在 ts 附近有没有行情数据。None = 查不到 (不是"没有")。"""
    if not asset or not ts:
        return None
    try:
        row = conn.execute(
            "SELECT 1 FROM kline_bars WHERE symbol=? AND ts BETWEEN ? AND ? LIMIT 1",
            (asset, (ts - 1800) * 1000, (ts + 1800) * 1000),
        ).fetchone()
        if row:
            return True
    except sqlite3.Error:
        pass
    try:
        row = conn.execute(
            "SELECT 1 FROM ohlcv_daily WHERE asset=? AND ts BETWEEN ? AND ? LIMIT 1",
            (asset, ts - 3 * 86400, ts + 3 * 86400),
        ).fetchone()
        if row:
            return True
    except sqlite3.Error:
        pass
    return False


def _forward_move(conn: sqlite3.Connection, asset: str, ts: int,
                  days: int = OUTCOME_WINDOW_DAYS) -> Optional[float]:
    """决策之后 N 天的真实涨跌幅 (%). 取不到就 None, 不猜。"""
    if not asset or not ts:
        return None
    end_ts = ts + days * 86400
    base = None
    for sql, params in (
        ("SELECT close FROM ohlcv_daily WHERE asset=? AND ts<=? ORDER BY ts DESC LIMIT 1",
         (asset.upper(), ts)),
        ("SELECT close FROM kline_bars WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",
         (asset.upper(), ts * 1000)),
    ):
        try:
            row = conn.execute(sql, params).fetchone()
            if row and row[0]:
                base = float(row[0])
                break
        except sqlite3.Error:
            continue
    if base is None:
        return None
    # v343: 这里原来取"决策之后的第一根 K 线"当 N 天后的结果 ——
    # 实际测的是几分钟的波动，而且同一资产在不同时点会落到同一对相邻 K 线，
    # 于是几十个事件算出同一个数（实测 XAU 18 行跨 6 个时点全是 1.1172 / 1.461）。
    # 现在锁到 ts+days 附近那根，并要求真实跨度足够，否则返回 None（还没到能评判的时候）。
    best = None
    for sql, params in (
        ("SELECT ts, close FROM ohlcv_daily WHERE asset=? AND ts BETWEEN ? AND ?",
         (asset.upper(), end_ts - 2 * 86400, end_ts + 2 * 86400)),
        ("SELECT ts, close FROM kline_bars WHERE symbol=? AND ts BETWEEN ? AND ?",
         (asset.upper(), (end_ts - 2 * 86400) * 1000, (end_ts + 2 * 86400) * 1000)),
    ):
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            continue
        if rows:
            best = min(rows, key=lambda r: abs(int(r[0]) - (end_ts if "kline" not in sql else end_ts * 1000)))
            break
    if best is None:
        return None
    fut = float(best[1])
    _span = (int(best[0]) - int(ts * 1000)) / 1000.0 if "kline" in sql else (int(best[0]) - int(ts))
    _min_span = max((days - 1) * 86400, days * 86400 * 0.5)
    if _span < _min_span:
        return None
    return round((fut - base) / base * 100.0, 4)


def _decision_ts(dec: Dict[str, Any]) -> int:
    ts = int(dec.get("ts") or 0)
    if ts:
        return ts
    created = str(dec.get("created_at") or "")
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", created)
    if not m:
        return 0
    try:
        y, mo, d = (int(m.group(i)) for i in (1, 2, 3))
        hh, mm = int(m.group(4)), int(m.group(5))
        return int(calendar.timegm(_dt.datetime(y, mo, d, hh, mm).timetuple()))
    except Exception:
        return 0


def _peer_co_move_count(conn: sqlite3.Connection, asset: str, ts: int,
                        move_sign: int) -> int:
    """同组 ≥PEER_MIN_MOVE_PCT 的同向异动资产数 (排除自己)。查不到就返回 0。"""
    if not ts or not asset or move_sign == 0:
        return 0
    peers: List[str] = []
    for members in _CROSS_GROUPS.values():
        if asset in members:
            peers = [a for a in members if a != asset]
            break
    if not peers:
        return 0
    ph = ",".join("?" for _ in peers)
    try:
        rows = conn.execute(
            "SELECT asset FROM critical_price_moves WHERE asset IN (%s) "
            "AND ts BETWEEN ? AND ? AND ABS(COALESCE(move_pct,0)) >= ? "
            "AND ((move_pct > 0 AND ? > 0) OR (move_pct < 0 AND ? < 0)) "
            "GROUP BY asset" % ph,
            (*peers, ts - PEER_WINDOW_SEC, ts + PEER_WINDOW_SEC,
             PEER_MIN_MOVE_PCT, move_sign, move_sign),
        ).fetchall()
    except sqlite3.Error:
        return 0
    return len(rows)


def classify_missed_decision(conn: sqlite3.Connection, dec: Dict[str, Any],
                             now_ts: Optional[int] = None) -> Dict[str, Any]:
    """给一条漏检决策定 (dimension, subcategory) + 理由。证据驱动。"""
    now = int(now_ts or time.time())
    asset = str(dec.get("target_asset") or "").upper()
    ts = _decision_ts(dec)
    move = dec.get("forward_move_pct")
    move_sign = _sign_of(move)
    conf = dec.get("confidence_at_time")
    if conf is None:
        conf = dec.get("sentiment_score")
    try:
        conf = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf = None

    # 1) 行情数据缺失 -> C1 feed_missing
    has_bar = _has_price_bar(conn, asset, ts)
    if has_bar is False:
        return {"dimension": "C", "subcategory": "feed_missing",
                "reason": "异动时刻该资产无任何行情数据 (数据缺失), 无法形成交易判断"}

    # 2) 决策太久 -> C3 signal_delay
    if ts and (now - ts) > SIGNAL_DELAY_DAYS * 86400:
        days = int((now - ts) / 86400)
        return {"dimension": "C", "subcategory": "signal_delay",
                "reason": "决策发生在 %d 天前, 超出实时检测窗口" % days}

    # 3/4) 同期新闻: 方向相反 -> B1; 同向但没转成决策 -> B2
    if ts and asset:
        try:
            news = conn.execute(
                "SELECT action FROM news_events "
                "WHERE asset=? AND ts BETWEEN ? AND ? AND action IN ('BUY','SELL') "
                "ORDER BY ts DESC LIMIT 3",
                (asset, ts - 12 * 3600, ts + 12 * 3600),
            ).fetchall()
        except sqlite3.Error:
            news = []
        same_dir = opposite = False
        for n in news:
            n_sign = 1 if _norm_action(n["action"]) == "BUY" else -1
            if move_sign == 0:
                continue
            if n_sign == move_sign:
                same_dir = True
            else:
                opposite = True
        if opposite:
            return {"dimension": "B", "subcategory": "news_direction_inverted",
                    "reason": "同期新闻方向与市场实际走势相反, 系统按反方向理解"}
        if same_dir:
            return {"dimension": "B", "subcategory": "event_chain_break",
                    "reason": "同期有同向新闻, 但事件链没把它转成可执行决策"}

    # 5) 高置信但实际没动 -> C2 calibration_drift
    if conf is not None and conf >= CALIBRATION_DRIFT_MIN_CONF:
        outcome = move
        if outcome is None and ts:
            outcome = _forward_move(conn, asset, ts, OUTCOME_WINDOW_DAYS)
        if outcome is not None and abs(float(outcome)) < CALIBRATION_DRIFT_MAX_MOVE_PCT:
            return {"dimension": "C", "subcategory": "calibration_drift",
                    "reason": "置信度 %.2f 但 %d 日实际波动仅 %.2f%% (信号本身虚高)"
                              % (conf, OUTCOME_WINDOW_DAYS, float(outcome))}

    # 5.5) v326: 拒单机制 (机器记录的事实) 优先于下面"有因果链"这种推断。
    #      旧版本没有这一步 → 三种完全不同的机制 (资产额度/同批连坐/强度闸门)
    #      被统一压成 market_regime_misread, A 类塌成一个桶。
    deny = str(dec.get("no_trade_reason") or "").strip()
    if deny and _rdm is not None:
        mapped = _rdm.classify_deny_reason(deny)
        if mapped:
            return {"dimension": mapped[0], "subcategory": mapped[1],
                    "reason": "%s (deny_reason=%s)" % (mapped[2], deny[:60])}

    # 6) 该资产当时有因果链, 但信号没命中链的触发条件 -> A (v326 细分)
    if ts and asset:
        try:
            chain = conn.execute(
                "SELECT id FROM causal_chains WHERE asset=? AND enabled=1 LIMIT 1",
                (asset,),
            ).fetchone()
        except sqlite3.Error:
            chain = None
        if chain:
            peers = _peer_co_move_count(conn, asset, ts, move_sign)
            if peers >= PEER_CO_MOVE_MIN:
                return {"dimension": "A", "subcategory": "market_regime_misread",
                        "reason": "链未触发且同组 %d 个资产同窗口同向异动 → 板块级状态没读到"
                                  % peers}
            return {"dimension": "A", "subcategory": "pattern_recognition",
                    "reason": "链未触发、同组无联动 → 单资产形态/强度信号没识别出来"}

    # 7) 兜底 -> A1 pattern_recognition
    return {"dimension": "A", "subcategory": "pattern_recognition",
            "reason": "无催化无链, 纯形态/强度信号未被识别"}


_INSERT_SQL = """
    INSERT INTO missed_signal_reflections
        (decision_id, asset, action, dimension, subcategory,
         root_cause, lesson, verification,
         move_pct, hit, confidence_at_time,
         reflection_text, evidence, created_at)
    VALUES (?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?)
    ON CONFLICT(decision_id) DO UPDATE SET
        asset=excluded.asset,
        action=excluded.action,
        dimension=excluded.dimension,
        subcategory=excluded.subcategory,
        root_cause=excluded.root_cause,
        lesson=excluded.lesson,
        verification=excluded.verification,
        move_pct=excluded.move_pct,
        hit=excluded.hit,
        confidence_at_time=COALESCE(excluded.confidence_at_time,
                                    missed_signal_reflections.confidence_at_time),
        reflection_text=excluded.reflection_text,
        evidence=excluded.evidence
"""


def _upsert_one(conn: sqlite3.Connection, dec: Dict[str, Any],
                paper_run_id: int, now_ts: int) -> Optional[int]:
    decision_id = int(dec.get("id") or 0)
    if not decision_id:
        return None
    asset = str(dec.get("target_asset") or "").upper()
    # v466 P0-0c：判定阶段走独立只读连接，避免分类/行情回看的多次 SELECT 挂在主写
    # 连接的 deferred 事务上（第一条写语句之前 SELECT 不会申请写锁，但连接一旦被
    # 复用到写，之前读阶段占用的快照会把事务跨度拉长）。写操作仍走传入的主连接。
    ro_conn: Optional[sqlite3.Connection] = None
    try:
        ro_conn = _conn()
        cls = classify_missed_decision(ro_conn, dec, now_ts=now_ts)
    finally:
        try:
            if ro_conn is not None:
                ro_conn.close()
        except sqlite3.Error:
            pass
    dim, sub = cls["dimension"], cls["subcategory"]
    if dim not in _VALID_DIMS or sub not in _VALID_SUBS:
        return None

    ts = _decision_ts(dec)
    outcome = dec.get("forward_move_pct")
    if outcome is None and ts:
        # 回看 SELECT 走独立只读连接，用完即关；主写连接 conn 上不做任何读操作。
        ro_forward: Optional[sqlite3.Connection] = None
        try:
            ro_forward = _conn()
            outcome = _forward_move(ro_forward, asset, ts, OUTCOME_WINDOW_DAYS)
        except sqlite3.Error:
            outcome = None
        finally:
            try:
                if ro_forward is not None:
                    ro_forward.close()
            except sqlite3.Error:
                pass
    move_sign = _sign_of(outcome)
    hit = 1 if (move_sign != 0 and outcome is not None
                and abs(float(outcome)) >= HIT_MOVE_THRESHOLD_PCT) else 0

    # v403：0.0 不是"置信度很低"，是"没算过" —— 落库必须写 NULL（铁律 4）
    conf = dec.get("confidence_at_time")
    if conf is None:
        conf = dec.get("sentiment_score")
    try:
        conf_val = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf_val = None
    if conf_val is not None and abs(conf_val) < 1e-9:
        conf_val = None

    root = cls["reason"]
    # v482：只有 hit=1（涨跌确实达到 HIT_MOVE_THRESHOLD_PCT）才配说"本应触发交易"。
    # 此前无论涨跌多少都写这句 —— 实测绝大多数样本 |move| < 0.3%，等于在编造漏检。
    if hit:
        lesson = ("漏检信号: 市场出现本应触发交易的方向(%s%% >= %.1f%%), 但系统给了 HOLD。"
                  "维度 %s/%s -> %s"
                  % (("%.2f" % float(outcome)) if outcome is not None else "n/a",
                     HIT_MOVE_THRESHOLD_PCT, dim, sub, root))
    else:
        lesson = ("HOLD 记账: 后续涨跌未达 %.1f%% 阈值, 不构成漏检, 仅留痕。"
                  "维度 %s/%s -> %s"
                  % (HIT_MOVE_THRESHOLD_PCT, dim, sub, root))
    verification = ("同类漏检再出现时, 该品种的信号强度阈值需下调或提前介入; "
                    "复核对齐 missed_signals 表与本表的小类分布。")
    text = ("[%s/%s] %s | 资产 %s | 实际涨跌 %s%%"
            % (dim, sub, root, asset or "-",
               ("%.2f" % float(outcome)) if outcome is not None else "n/a"))

    # v413: 来源与证据显式落库。
    # origin=template —— 这条反思是模板拼出来的，不是模型判出来的，统计时必须能切开。
    # error_layer='unattributed' —— 之前留空，所有 GROUP BY 都把空值当成"没有错误"，
    #   于是"94% 无明显错误"里混着一批"根本没归因"的行。空 = 判不了，必须写清楚。
    # evidence: 只写能复核的事实（资产/决策时刻/窗口内涨跌/取价方式），
    #   attribution 写 'rule_bucket'：有分桶规则，但没有逐条证据。
    evidence = json.dumps(
        {
            "asset": asset or "",
            "decision_ts": int(ts) if ts else None,
            "move_pct": (float(outcome) if outcome is not None else None),
            "window_days": OUTCOME_WINDOW_DAYS,
            "price_source": "forward_move",
            "attribution": "rule_bucket",
            "dimension": dim,
            "subcategory": sub,
        },
        ensure_ascii=False,
    )

    try:
        cur = conn.execute(
            _INSERT_SQL,
            (decision_id, asset, str(dec.get("suggested_action") or "HOLD"),
             dim, sub,
             root, lesson, verification,
             (float(outcome) if outcome is not None else None), hit, conf_val,
             text, evidence, time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        if cur.rowcount:
            conn.commit()
            return int(cur.lastrowid)
    except sqlite3.Error as exc:
        print(f"[MISSED-REFLECT] upsert decision={decision_id} failed: "
              f"{type(exc).__name__}: {exc}", flush=True)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    return None


def _load_candidates(conn: sqlite3.Connection, since_ts: int,
                     limit: int) -> List[Dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT id, target_asset, suggested_action, sentiment_score, impact_score, "
            "       forward_move_pct, ts, created_at, paper_trading_run_id, "
            "       event_strength, no_trade_reason "
            "FROM ai_decisions "
            "WHERE missed_signal = 1 AND (ts >= ? OR ts IS NULL) "
            "ORDER BY id DESC LIMIT ?",
            (since_ts, int(limit)),
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"[MISSED-REFLECT] select failed: {type(exc).__name__}: {exc}", flush=True)
        return []
    return [dict(r) for r in rows]


def run(conn: sqlite3.Connection, days: int = 1, limit: int = 500,
        now_ts: Optional[int] = None) -> Dict[str, Any]:
    """把漏检决策写进独立记账表。返回统计。"""
    now = int(now_ts or time.time())
    since = now - max(1, int(days)) * 86400
    # v482: 独立表必须存在才写。迁移由 db.migrate 负责，但引擎重启前可能先跑到这里，
    # 因此这里做一次幂等建表，避免"扫描成功但一条都没写进去"的静默失败。
    if _db is not None:
        try:
            conn.execute(_db._CREATE_MISSED_SIGNAL_REFLECTIONS)
            for _sql in _db._MISSED_SIGNAL_REFLECTIONS_INDEXES:
                conn.execute(_sql)
        except sqlite3.Error as _exc:
            print(f"[MISSED-REFLECT] ensure table failed: {_exc}", flush=True)
    cands = _load_candidates(conn, since, limit)
    written = 0
    skipped_degraded = 0
    dist: Dict[str, int] = {}
    try:
        import insight_pool as _ip  # v398: 降级决策不产出洞察
    except Exception:
        _ip = None  # type: ignore
    for dec in cands:
        if _ip is not None:
            try:
                if _ip.is_degraded_decision(conn, dec.get("id")):
                    skipped_degraded += 1
                    continue
            except Exception:
                pass
        # paper_run_id 是 INTEGER 列 —— 用该决策自己的模拟盘 run id;
        # 没有就落到 -1 哨兵 (仍是 NOT NULL, 借此与结算行区分)。
        try:
            run_id = int(dec.get("paper_trading_run_id") or 0)
        except (TypeError, ValueError):
            run_id = 0
        if run_id <= 0:
            run_id = -1
        rid = _upsert_one(conn, dec, run_id, now)
        if rid:
            written += 1
            cls = classify_missed_decision(conn, dec, now_ts=now)
            key = "%s/%s" % (cls["dimension"], cls["subcategory"])
            dist[key] = dist.get(key, 0) + 1
    return {"candidates": len(cands), "written": written,
            "since": since, "dist": dist,
            "skipped_degraded": skipped_degraded}


def run_once(days: int = 1, limit: int = 500) -> Dict[str, Any]:
    conn = _conn()
    try:
        stats = run(conn, days=days, limit=limit)
        print("[MISSED-REFLECT] 候选 %d, 写入反思 %d 条 %s"
              % (stats["candidates"], stats["written"], stats.get("dist") or {}),
              flush=True)
        return stats
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def run_days(days: int = 2, limit: int = 300) -> Dict[str, Any]:
    """自开连接的增量跑 (给常驻 worker 调用)。不打印, 由调用方决定怎么记。"""
    conn = _conn()
    try:
        return run(conn, days=days, limit=limit)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def backfill(days: int = 120, limit: int = 5000) -> Dict[str, Any]:
    conn = _conn()
    try:
        stats = run(conn, days=days, limit=limit)
        print("[MISSED-REFLECT-BACKFILL] 候选 %d, 写入反思 %d 条 (窗口 %d 天) %s"
              % (stats["candidates"], stats["written"], days, stats.get("dist") or {}),
              flush=True)
        return stats
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="漏检信号 -> 反思系统")
    ap.add_argument("--once", action="store_true", help="增量跑 (默认)")
    ap.add_argument("--backfill", action="store_true", help="历史回填")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()
    if args.backfill:
        backfill(days=args.days, limit=max(args.limit, 5000))
    else:
        run_once(days=max(1, args.days), limit=args.limit)
    return 0


__all__ = ["classify_missed_decision", "run", "run_once", "backfill",
           "MISSED_SIGNAL_NAME"]

if __name__ == "__main__":
    raise SystemExit(main())
