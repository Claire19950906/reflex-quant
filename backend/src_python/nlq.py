# -*- coding: utf-8 -*-
"""v381 · 自然语言问库（NLQ over the local event bus）

问题
----
真相都在 SQLite 里，但看一条结论要翻 5–8 个页面（信号台/归因/反思/漏检/信息源）。
对排障和对外演示都很贵。

设计取舍（很重要）
------------------
**不让 LLM 自由写 SQL。** 原因：库里 100+ 张表、口径各不相同（settled / truth_verdict /
hit 三套判定并存），LLM 拼出来的 SQL 既容易写错，也会因为表名猜错而编造结论。

这里走 **意图路由 + 白名单参数化查询**：
  问题 → 识别意图（表现/错误码/漏检/异动/来源）+ 抽取参数（品种/时间窗/数量）
       → 命中白名单模板（SQL 固定，只有参数是变量）
       → 模板生成中文结论（数字全部来自查询结果，不由模型想象）

LLM 只在最后一步可选做「润色/换说法」，且输入就是查询结果本身；
`NLQ_USE_LLM=0`（默认）时完全离线、零额度。

支持的问法（示例）
------------------
- 最近 7 天命中率怎么样？
- 黄金这周表现如何？ / BTC 最近 30 天亏了多少？
- 系统最常犯哪些错？ / 最近都踩了什么坑？
- 漏检了多少信号？都漏在哪？
- 黄金最近为什么跌？ / 哪个品种异动最多？
- 新闻来源哪家最不稳？
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# ── 品种别名（问库里用标准代码）──────────────────────────────────────────────
ASSET_ALIASES: Dict[str, str] = {
    "黄金": "XAU", "金价": "XAU", "白银": "XAG", "银价": "XAG",
    "比特币": "BTC", "大饼": "BTC", "以太": "ETH", "以太坊": "ETH",
    "原油": "WTI", "石油": "WTI", "油价": "WTI",
    "纳指": "IXIC", "纳斯达克": "IXIC", "标普": "SPX", "道指": "DJI",
    "特斯拉": "TSLA", "英伟达": "NVDA", "苹果": "AAPL", "微软": "MSFT",
}
_KNOWN_CODES = {"XAU", "XAG", "BTC", "ETH", "SOL", "WTI", "SPX", "IXIC", "DJI",
                "NDX", "TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "MU"}

# 时间窗：词 → 天数
_DAYS_WORDS = [
    ("今天", 1), ("今日", 1), ("昨天", 2), ("昨日", 2),
    ("本周", 7), ("这周", 7), ("一周", 7), ("最近7天", 7), ("最近一周", 7),
    ("本月", 30), ("这个月", 30), ("最近30天", 30), ("最近一个月", 30),
    ("近3天", 3), ("最近3天", 3), ("近90天", 90), ("最近90天", 90),
]

_INTENTS: List[Tuple[str, Tuple[str, ...]]] = [
    ("decision", ("这笔", "这一笔", "那笔", "诊断", "第", "#", "哪笔")),
    ("errors", ("错误码", "踩坑", "犯什么错", "常犯", "哪些错", "犯的错",
                "反复", "错在哪", "错误", "坑")),
    ("missed", ("漏检", "漏掉", "没抓到", "错过", "漏了")),
    ("pnl_why", ("为什么亏", "为什么赚", "亏在哪", "赚在哪", "亏损", "亏钱", "赚钱", "盈利")),
    ("drawdown", ("回撤", "水下")),
    ("moves", ("为什么", "异动", "涨了", "跌了", "大涨", "大跌", "波动")),
    ("sources", ("来源", "信息源", "新闻源", "哪家")),
    ("performance", ("命中率", "胜率", "表现", "赚", "亏", "盈亏", "绩效", "成绩")),
]


# ──────────────────────────────────────────────────────────────────────────────
# 解析
# ──────────────────────────────────────────────────────────────────────────────
def parse_question(q: str) -> Dict[str, Any]:
    text = (q or "").strip()
    low = text.lower()
    asset = ""
    up = text.upper()
    for code in _KNOWN_CODES:
        if re.search(rf"(?<![A-Z]){code}(?![A-Z])", up):
            asset = code
            break
    if not asset:
        for alias, code in ASSET_ALIASES.items():
            if alias in text:
                asset = code
                break

    days = 7
    for word, n in _DAYS_WORDS:
        if word in text:
            days = n
            break
    m = re.search(r"最近\s*(\d+)\s*天", text)
    if m:
        days = int(m.group(1))

    decision_id = 0
    m_id = re.search(r"#\s*(\d{2,7})|(\d{2,7})\s*(?:号|笔|单)", text)
    if m_id:
        decision_id = int(next(g for g in m_id.groups() if g))
    elif "笔" in text or "诊断" in text:
        # "11555 这笔为什么亏" 这种数字与量词被空格/助词隔开的写法
        m_any = re.search(r"\d{2,7}", text)
        if m_any:
            decision_id = int(m_any.group(0))

    intent = "unknown"
    for name, keys in _INTENTS:
        if any(k in text for k in keys):
            intent = name
            break
    if intent == "unknown":
        # 只报了品种（"黄金怎么样"）→ 默认看表现
        if asset:
            intent = "performance"
    return {"intent": intent, "asset": asset, "days": max(1, min(days, 365)),
            "decision_id": decision_id, "question": text}


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _ts_threshold(conn, table: str, col: str, days: int) -> int:
    """库内 ts 列同时存在「epoch 秒」和「epoch 毫秒」两种口径（不同表不同源），
    先看最大值量级再决定阈值单位 —— 直接按毫秒传会把秒表全部过滤成空。"""
    try:
        sample = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]
        unit = 1000 if sample is not None and float(sample) > 1e11 else 1
    except Exception:
        unit = 1
    return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * unit)


# ──────────────────────────────────────────────────────────────────────────────
# 白名单查询
# ──────────────────────────────────────────────────────────────────────────────
def _q_performance(conn, asset: str, days: int) -> Dict[str, Any]:
    since = _since(days)
    where = "settled=1 AND forward_pnl IS NOT NULL AND created_at >= ?"
    params: List[Any] = [since]
    if asset:
        where += " AND target_asset=?"
        params.append(asset)
    rows = conn.execute(
        f"""SELECT target_asset AS asset, COUNT(*) AS n,
                   SUM(CASE WHEN forward_pnl>0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(AVG(forward_pnl),3) AS avg_pnl,
                   ROUND(SUM(forward_pnl),2) AS total_pnl,
                   ROUND(MIN(forward_pnl),2) AS worst,
                   ROUND(MAX(forward_pnl),2) AS best
              FROM ai_decisions WHERE {where}
             GROUP BY target_asset ORDER BY n DESC""",
        params,
    ).fetchall()
    total_where = "settled=1 AND forward_pnl IS NOT NULL AND created_at >= ?"
    tparams: List[Any] = [since]
    if asset:
        total_where += " AND target_asset=?"
        tparams.append(asset)
    tot = conn.execute(
        f"""SELECT COUNT(*), SUM(CASE WHEN forward_pnl>0 THEN 1 ELSE 0 END),
                   ROUND(AVG(forward_pnl),3)
              FROM ai_decisions WHERE {total_where}""",
        tparams,
    ).fetchone()
    n = int(tot[0] or 0)
    wins = int(tot[1] or 0)
    winrate = round(wins / n * 100, 1) if n else None
    table = [{"品种": r[0], "笔数": r[1], "胜": r[2], "胜率%": round((r[2] or 0) / r[1] * 100, 1) if r[1] else None,
              "均盈亏%": r[3], "合计%": r[4], "最差%": r[5], "最好%": r[6]} for r in rows]
    if n == 0:
        answer = f"最近 {days} 天没有已结算的交易样本" + (f"（{asset}）" if asset else "") + "，暂无法给命中率。"
    else:
        scope = f"{asset} · " if asset else ""
        answer = (f"最近 {days} 天{scope}已结算 {n} 笔，胜 {wins} 笔，胜率 {winrate}%，"
                  f"平均每笔 {tot[2]:+.3f}%。")
        if n < 10:
            answer += " 样本不足 10 笔，结论只能当方向参考，不能当统计结论。"
    return {"answer": answer, "table": table, "n": n, "winrate": winrate,
            "sources": [{"表": "ai_decisions", "口径": "settled=1 且 forward_pnl 非空"}]}


def _q_errors(conn, asset: str, days: int) -> Dict[str, Any]:
    since = _since(days)
    rows = conn.execute(
        """SELECT error_code, COUNT(*) n, ROUND(AVG(weight),2) avg_w,
                  MAX(created_at) last_seen
             FROM decision_errors
            WHERE created_at >= ?
            GROUP BY error_code ORDER BY n DESC, avg_w DESC LIMIT 10""",
        (since,),
    ).fetchall()
    if not rows:
        return {"answer": f"最近 {days} 天没有新的错误码记录。", "table": [],
                "sources": [{"表": "decision_errors"}]}
    top = rows[0]
    table = [{"错误码": r[0], "次数": r[1], "平均权重": r[2], "最近一次": str(r[3])[:19]} for r in rows]
    answer = (f"最近 {days} 天最常见的是 {top[0]}（{top[1]} 次，平均权重 {top[2]}）；"
              f"共 {len(rows)} 类错误码有记录。先修最高频那一个，比泛泛复盘更划算。")
    return {"answer": answer, "table": table, "sources": [{"表": "decision_errors"}]}


def _q_missed(conn, asset: str, days: int) -> Dict[str, Any]:
    since = _ts_threshold(conn, "missed_signals", "created_at", days)
    where = "CAST(created_at AS INTEGER) >= ?"
    params: List[Any] = [since]
    if asset:
        where += " AND asset=?"
        params.append(asset)
    rows = conn.execute(
        f"""SELECT COALESCE(subcategory, dimension, 'unknown') AS k, COUNT(*) n,
                   SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) hit_n
              FROM missed_signals WHERE {where}
             GROUP BY k ORDER BY n DESC LIMIT 10""",
        params,
    ).fetchall()
    total = sum(int(r[1] or 0) for r in rows)
    if not rows:
        return {"answer": f"最近 {days} 天没有漏检记录。", "table": [], "sources": [{"表": "missed_signals"}]}
    table = [{"漏检类型": r[0], "条数": r[1], "其中确实该抓": r[2]} for r in rows]
    answer = (f"最近 {days} 天记录 {total} 条漏检，最集中的是 {rows[0][0]}（{rows[0][1]} 条，"
              f"其中 {rows[0][2]} 条后来证明该抓）。")
    return {"answer": answer, "table": table, "sources": [{"表": "missed_signals"}]}


def _q_decision(conn, asset: str, days: int, decision_id: int = 0) -> Dict[str, Any]:
    """单笔诊断：把「实际 / 不进场 / 反向 / 延迟 / 半仓」摊开，判断亏在哪一层。

    只陈述重放结果，不替用户下结论之外的话；没有 K 线就明说不能重放。
    """
    if not decision_id:
        return {"answer": "需要给一个决策编号，例如：11555 这笔为什么亏（编号见反事实页 / 历史回溯页）。",
                "table": [], "sources": [{"表": "ai_decisions"}]}

    try:
        import counterfactual as _cf
        res = _cf.four_legs(decision_id, conn=conn)
    except Exception as exc:
        return {"answer": f"重放失败：{type(exc).__name__}: {str(exc)[:80]}", "table": [],
                "sources": [{"表": "ai_decisions"}]}

    if not res.get("ok"):
        hint = res.get("hint") or res.get("reason") or "无法重放"
        return {"answer": f"#{decision_id} 无法重放：{hint}", "table": [],
                "sources": [{"表": "ai_decisions", "口径": "K 线缺失时不拿相邻 K 线冒充"}]}

    legs = res.get("legs") or []
    by_key = {lg.get("key"): lg for lg in legs}

    def pct(k: str) -> str:
        v = by_key.get(k, {}).get("pnl_pct")
        return f"{v:+.2f}%" if isinstance(v, (int, float)) else "—"

    actual = by_key.get("actual", {}).get("pnl_pct")
    reverse = by_key.get("reverse", {}).get("pnl_pct")
    delay = by_key.get("delay", {}).get("pnl_pct")
    half = by_key.get("half", {}).get("pnl_pct")

    bits: List[str] = [f"#{decision_id} {res.get('asset')} {res.get('action')}："
                       f"实际 {pct('actual')}，反向 {pct('reverse')}，延迟 {pct('delay')}，半仓 {pct('half')}。"]

    # 结论：只做「哪条腿更好 + 差多少」的算术，不臆测动机
    try:
        if isinstance(reverse, (int, float)) and isinstance(actual, (int, float)) and actual < 0 < reverse:
            bits.append("方向是反的：反着做就是正收益。")
        if isinstance(delay, (int, float)) and isinstance(actual, (int, float)) and delay > actual:
            bits.append(f"进早了：延迟 10 分钟少亏/多赚 {delay - actual:+.2f} 个百分点。")
        if isinstance(actual, (int, float)) and actual < 0 and isinstance(half, (int, float)):
            bits.append(f"仓位偏重：同方向半仓的盈亏是 {half:+.2f}%。")
        if isinstance(actual, (int, float)) and actual > 0:
            bits.append("这笔本身是赚的；四条腿用于看是否还能更好，不是找错。")
    except Exception:
        pass
    drift = res.get("drift_pct")
    if isinstance(drift, (int, float)) and abs(drift) > 1:
        bits.append(f"注意：重算与库内结算差 {drift:+.2f} 个百分点（结算用快照价，属正常口径差）。")

    table = [{"路径": lg.get("label"), "盈亏%": lg.get("pnl_pct"),
              "MFE%": lg.get("mfe_pct"), "MAE%": lg.get("mae_pct")} for lg in legs]
    return {"answer": " ".join(bits), "table": table,
            "sources": [{"表": "ai_decisions + kline_bars", "口径": f"{res.get('timeframe')} 重放，"
                                                                 f"窗口 {res.get('holding_horizon_minutes')} 分钟"}]}


def _q_moves(conn, asset: str, days: int) -> Dict[str, Any]:
    since_ms = _ts_threshold(conn, "critical_price_moves", "ts", days)
    where = "ts >= ?"
    params: List[Any] = [since_ms]
    if asset:
        where += " AND asset=?"
        params.append(asset)
    rows = conn.execute(
        f"""SELECT asset, COUNT(*) n, ROUND(SUM(CASE WHEN move_pct>0 THEN 1 ELSE 0 END)*100.0/COUNT(*),1) up_pct,
                  ROUND(AVG(ABS(move_pct)),2) avg_abs, ROUND(MAX(ABS(move_pct)),2) max_abs
              FROM critical_price_moves WHERE {where}
             GROUP BY asset ORDER BY n DESC LIMIT 10""",
        params,
    ).fetchall()
    if not rows and asset:
        # 指定品种在窗口内没有异动 → 放宽到 30 天找最近一次，避免「为什么跌」直接空答
        widened = max(30, days * 4)
        since2 = _ts_threshold(conn, "critical_price_moves", "ts", widened)
        rows = conn.execute(
            """SELECT asset, COUNT(*) n, ROUND(SUM(CASE WHEN move_pct>0 THEN 1 ELSE 0 END)*100.0/COUNT(*),1) up_pct,
                      ROUND(AVG(ABS(move_pct)),2) avg_abs, ROUND(MAX(ABS(move_pct)),2) max_abs
                 FROM critical_price_moves WHERE ts >= ? AND asset=?
                GROUP BY asset""",
            (since2, asset),
        ).fetchall()
        if rows:
            days = widened
        else:
            return {"answer": f"{asset} 最近 {days} 天（并放宽到 {widened} 天）都没有关键异动记录，"
                              "说明这段时间波动没到异动阈值，不是漏记。", "table": [],
                    "sources": [{"表": "critical_price_moves"}]}
    if not rows:
        return {"answer": f"最近 {days} 天没有记录到关键异动。", "table": [],
                "sources": [{"表": "critical_price_moves"}]}
    table = [{"品种": r[0], "异动次数": r[1], "上涨占比%": r[2], "平均幅度%": r[3], "最大幅度%": r[4]} for r in rows]
    top = rows[0]
    answer = (f"最近 {days} 天异动最多的是 {top[0]}（{top[1]} 次，平均 {top[3]}%，"
              f"最大 {top[4]}%，上涨占 {top[2]}%）。要看单次原因去「行情归因」页，"
              f"这里只给分布，不给因果猜测。") if asset == "" else (
        f"{asset} 最近 {days} 天记录 {top[1]} 次异动，平均幅度 {top[3]}%，最大 {top[4]}%，"
        f"上涨占 {top[2]}%。具体触发新闻见「行情归因」。")
    return {"answer": answer, "table": table,
            "sources": [{"表": "critical_price_moves", "口径": "异动分布，不含因果结论"}]}


def _q_pnl_why(conn, asset: str, days: int) -> Dict[str, Any]:
    """已结算模拟盘的盈亏拆解：按品种×方向聚合，指出最大拖累。
    只陈述账本事实，不猜因果；触发原因去「归因 K 线」看。"""
    since = _since(days)
    where = "settled=1 AND pnl_pct IS NOT NULL AND created_at >= ?"
    params: List[Any] = [since]
    if asset:
        where += " AND asset=?"
        params.append(asset)
    try:
        rows = conn.execute(
            f"""SELECT asset, action, COUNT(*) n,
                       SUM(CASE WHEN pnl_pct>0 THEN 1 ELSE 0 END) AS wins,
                       ROUND(SUM(pnl_pct),3) AS total,
                       ROUND(AVG(pnl_pct),3) AS avg,
                       ROUND(MIN(pnl_pct),3) AS worst,
                       ROUND(MAX(pnl_pct),3) AS best
                  FROM quick_sim_trades WHERE {where}
                 GROUP BY asset, action ORDER BY total ASC""",
            params,
        ).fetchall()
    except Exception:
        return {"answer": "账本表暂时读不到（quick_sim_trades 不可用）。", "table": [],
                "sources": [{"表": "quick_sim_trades"}]}
    if not rows:
        scope = f"（{asset}）" if asset else ""
        return {"answer": f"最近 {days} 天{scope}没有已结算的模拟盘记录，暂时算不出亏在哪。",
                "table": [], "sources": [{"表": "quick_sim_trades", "口径": "settled=1"}]}

    n = sum(int(r[2] or 0) for r in rows)
    wins = sum(int(r[3] or 0) for r in rows)
    total = round(sum(float(r[4] or 0) for r in rows), 3)
    winrate = round(wins / n * 100, 1) if n else None
    table = [{"品种": r[0], "方向": r[1], "笔数": r[2],
              "胜率%": round((r[3] or 0) / r[2] * 100, 1) if r[2] else None,
              "合计%": r[4], "均%": r[5], "最差%": r[6], "最好%": r[7]} for r in rows]
    worst = rows[0]
    answer = f"最近 {days} 天已结算 {n} 笔，胜率 {winrate}%，合计 {total:+.3f}%。"
    if float(worst[4] or 0) < 0:
        answer += f"拖累最大的是 {worst[0]} {worst[1]}（{worst[2]} 笔，合计 {float(worst[4]):+.3f}%）。"
    else:
        answer += f"全部为正；最好的是 {worst[0]} {worst[1]}（{worst[2]} 笔，合计 {float(worst[4]):+.3f}%）。"

    single = None
    try:
        single = conn.execute(
            f"""SELECT asset, action, ROUND(pnl_pct,3), created_at
                  FROM quick_sim_trades WHERE {where} ORDER BY pnl_pct ASC LIMIT 1""",
            params,
        ).fetchone()
    except Exception:
        single = None
    if single and float(single[2] or 0) < 0:
        answer += f" 单笔最差 {single[0]} {single[1]} {float(single[2]):+.3f}%（{str(single[3])[:16]}）。"
    if n < 10:
        answer += " 样本不足 10 笔，只能当方向参考。"
    answer += " 具体触发原因去「归因 K 线」看，这里只给账本口径。"
    return {"answer": answer, "table": table,
            "sources": [{"表": "quick_sim_trades", "口径": "settled=1 且 pnl_pct 非空"}]}


def _q_drawdown(conn, asset: str, days: int) -> Dict[str, Any]:
    """账户级回撤：当前权益、峰值、期内最深。组合口径，不按品种拆。"""
    try:
        latest = conn.execute(
            """SELECT ts, equity, peak_equity, drawdown_pct, n_settled, n_open
                  FROM sim_equity_snapshots ORDER BY ts DESC LIMIT 1"""
        ).fetchone()
    except Exception:
        latest = None
    if not latest:
        return {"answer": "还没有权益快照，回撤暂时算不出来。", "table": [],
                "sources": [{"表": "sim_equity_snapshots"}]}

    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    worst = None
    try:
        worst = conn.execute(
            "SELECT MIN(drawdown_pct) FROM sim_equity_snapshots WHERE ts >= ?", (since,)
        ).fetchone()
    except Exception:
        worst = None

    equity = float(latest[1] or 0)
    peak = float(latest[2] or 0)
    dd = float(latest[3] or 0)
    deep = float(worst[0]) if worst and worst[0] is not None else dd
    table = [{"当前权益": round(equity, 2), "峰值": round(peak, 2),
              "当前回撤%": round(dd, 3), "期内最深%": round(deep, 3),
              "已结算": latest[4], "未平": latest[5]}]
    answer = (f"账户权益 {equity:,.2f}，峰值 {peak:,.2f}，当前回撤 {dd:.3f}%；"
              f"最近 {days} 天最深回撤 {deep:.3f}%。")
    if abs(dd) >= 5:
        answer += " 回撤已过 5%，按预案先降仓，别加仓摊平。"
    elif abs(dd) >= 2:
        answer += " 回撤过 2%，检查是不是同一类交易在重复亏。"
    else:
        answer += " 回撤还在正常波动范围。"
    return {"answer": answer, "table": table,
            "sources": [{"表": "sim_equity_snapshots", "口径": "组合权益，不按品种拆"}]}


def _q_sources(conn, asset: str, days: int) -> Dict[str, Any]:
    since = _since(days)
    try:
        rows = conn.execute(
            """SELECT source, COUNT(*) n, MAX(created_at) last_seen
                 FROM raw_news WHERE created_at >= ?
                GROUP BY source ORDER BY n DESC LIMIT 10""",
            (since,),
        ).fetchall()
    except Exception:
        rows = []
    if not rows:
        return {"answer": f"最近 {days} 天没有新闻入库记录。", "table": [],
                "sources": [{"表": "raw_news"}]}
    table = [{"来源": r[0], "条数": r[1], "最近一条": str(r[2])[:19]} for r in rows]
    answer = (f"最近 {days} 天入库 {sum(int(r[1] or 0) for r in rows)} 条新闻，"
              f"{rows[0][0]} 贡献最多（{rows[0][1]} 条）。要看失败率去「信息源」页。")
    return {"answer": answer, "table": table, "sources": [{"表": "raw_news"}]}


def ask(question: str, conn=None) -> Dict[str, Any]:
    """入口：问题 → 意图 → 白名单查询 → 中文结论。永不抛异常。"""
    own = False
    if conn is None:
        import db as _db
        conn = _db.get_connection()
        own = True
    try:
        parsed = parse_question(question)
        intent = parsed["intent"]
        if intent == "unknown":
            return {
                "ok": True, "intent": "unknown", "parsed": parsed,
                "answer": "没听懂要问哪一类。可以这样问：命中率怎么样 / 最常犯哪些错 / "
                          "漏检了多少 / 黄金最近为什么跌 / 11555 这笔为什么亏 / 新闻来源哪家最多。",
                "table": [],
            }
        if intent == "decision":
            res = _q_decision(conn, parsed["asset"], parsed["days"], parsed.get("decision_id") or 0)
        else:
            handlers = {
                "performance": _q_performance, "errors": _q_errors,
                "missed": _q_missed, "moves": _q_moves, "sources": _q_sources,
                "pnl_why": _q_pnl_why, "drawdown": _q_drawdown,
            }
            res = handlers[intent](conn, parsed["asset"], parsed["days"])
        out = {"ok": True, "intent": intent, "parsed": parsed, **res}
        if os.getenv("NLQ_USE_LLM", "0").lower() in ("1", "true", "on", "yes"):
            out["answer"] = _polish(out)
        return out
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        if own:
            conn.close()


def _polish(res: Dict[str, Any]) -> str:
    """可选：让主通道把结构化结果换成人话。失败一律回退模板结论。"""
    base = str(res.get("answer") or "")
    try:
        from llm_budget import call_llm  # 若项目无此入口则直接回退
        prompt = ("把下面的量化结果改写成 2 句中文结论，数字必须原样保留，不得新增结论：\n"
                  + json.dumps({"answer": base, "table": res.get("table")}, ensure_ascii=False))
        text = call_llm(prompt)
        return str(text).strip() or base
    except Exception:
        return base


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="自然语言问库（只读）")
    ap.add_argument("question", nargs="*", help="问题，例如：黄金最近为什么跌")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    q = " ".join(args.question) or "最近 7 天命中率怎么样"
    res = ask(q)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(res.get("answer", res.get("error", "")))
        for row in res.get("table") or []:
            print("  " + " | ".join(f"{k}={v}" for k, v in row.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
