# -*- coding: utf-8 -*-
"""analysis_yield.py — 分析产能体检（v414，只读聚合）。

要解决的事（实测 2026-11-17）：
  日报里「降级 461 条 / 528」是一个数字，但它装着五种不同的事实：
  品种越界、证据没到、模型没答、结构不达标、只剩摘要。一个数字治不了五种病，
  「89% 降级」于是成了谁也动不了的指标。

做法：**只描述、不修改**。
  · 终态判定唯一来源是 `analysis/contract.py::derive_state`（不在本模块另立口径）；
  · 每个桶带「处置口径」（DISPOSITION），前端直接渲染，不在页面里再抄一份；
  · 所有比例都把分母写清楚；算不出来的一律 None，不补 0（0 号铁律）；
  · 不读绩效表、不推胜率——那是 `quick_sim_trades` 的口径，只在对的地方算一次。

用法：
  cd backend/src_python && PYTHONPATH=. python3 analysis_yield.py --days 7
  cd backend/src_python && PYTHONPATH=. python3 analysis_yield.py --days 7 --json
  cd backend/src_python && PYTHONPATH=. python3 analysis_yield.py --days 7 --export-retry /tmp/retry.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from analysis import contract as ac

# ── 处置口径（唯一真源；前端不再写第二份）─────────────────────────────────
# (人读名, 该不该算「分析失败」, 今天该做什么)
DISPOSITION: Dict[str, tuple[str, bool, str]] = {
    "complete":        ("真产能", False, "无需处理；方向性决策从这里出"),
    "compressed":      ("可观察产出", False, "有方向但八段没展开：只观察，不计失败；复盘可用"),
    "out_of_scope":    ("非分析失败", False, "从分母剔除；真要扩覆盖就扩品种池，不在这里救"),
    "data_unavailable": ("链路问题", True, "修数据源/快照时效（feed_stale、无本地源），不是模型问题"),
    "llm_failed":      ("可重试", True, "模型没返回：进重试清单，先查配额/超时/空输出"),
    "parse_failed":    ("契约问题", True, "输出结构不达标：改 prompt/解析契约，别原样重试"),
}

# 可以安全「重跑」的终态（模型侧失败）；契约问题重跑也大概率同样失败
RETRYABLE_STATES = ("llm_failed",)

# 方向性动作
DIRECTIONAL_ACTIONS = ("BUY", "SELL")

# 需要的列（老库缺列时降级为 None，而不是给默认值）
_NEEDED_COLUMNS = (
    "id", "ts", "created_at", "news_id", "target_asset", "suggested_action",
    "analysis_degraded", "analysis_degraded_reason", "hard_data_status",
    "trade_gate_reason", "evidence_confidence", "sentiment_score",
    "analysis_state",
)


# ── 取数 ────────────────────────────────────────────────────────────────
def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _window_since(days: int) -> int:
    import time
    return int(time.time()) - max(1, int(days)) * 86400


def load_rows(conn: sqlite3.Connection, days: int) -> List[Dict[str, Any]]:
    """读近 N 天的决策行。缺列当 None（不补默认值），headline 取不到就留空。"""
    cols = _cols(conn, "ai_decisions")
    if not cols:
        return []
    select = ", ".join(
        (f"d.{c}" if c in cols else f"NULL AS {c}") for c in _NEEDED_COLUMNS
    )
    has_news = "news_events" in {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    sql = f"SELECT {select}"
    if has_news:
        sql += ", n.headline AS headline"
    sql += " FROM ai_decisions d"
    if has_news:
        # news_events 对同一条新闻是**一对多**（实测 news 29108 有 4 行），
        # 直接 join 会把决策行放大 4 倍（实测 539 → 1012）——所有比例随之失真。
        # 先在子查询里按 news_id 收敛成一行（取最早那行的标题），再左连。
        sql += (" LEFT JOIN (SELECT news_id, headline FROM news_events"
                " WHERE rowid IN (SELECT MIN(rowid) FROM news_events GROUP BY news_id)) n"
                " ON n.news_id = d.news_id")
    sql += " WHERE COALESCE(d.ts, 0) >= ?"
    rows = conn.execute(sql, (_window_since(days),)).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = {k: r[k] for k in r.keys() if k != "headline"}
        d["headline"] = r["headline"] if has_news else None
        out.append(d)
    return out


def _hard_status(row: Dict[str, Any]) -> str:
    """只用库里写下的证据状态；空就是空（不现算、不猜）。"""
    return str(row.get("hard_data_status") or "").strip()


def classify(row: Dict[str, Any]) -> Dict[str, Any]:
    """单行 → 终态 + HOLD 原因。纯函数，判定全部来自 analysis/contract。"""
    degraded = int(row.get("analysis_degraded") or 0)
    reason = str(row.get("analysis_degraded_reason") or "")
    asset = str(row.get("target_asset") or "")
    hard = _hard_status(row)
    state = ac.derive_state(degraded=degraded, reason=reason, asset=asset, hard_status=hard)
    no_trade = ac.derive_no_trade_reason(
        action=str(row.get("suggested_action") or ""),
        state=state,
        gate_reason=str(row.get("trade_gate_reason") or ""),
        news_age_min=None,          # 库里没有新闻年龄，stale 判定留给漏斗日报
    )
    return {
        "id": row.get("id"),
        "ts": int(row.get("ts") or 0),
        "created_at": str(row.get("created_at") or ""),
        "news_id": row.get("news_id"),
        "asset": asset.upper(),
        "action": str(row.get("suggested_action") or "").upper(),
        "state": state,
        "no_trade_reason": no_trade,
        "degraded_reason": reason,
        "hard_status": hard,
        "head": (row.get("headline") or "").strip()[:120],
    }


# ── 聚合 ────────────────────────────────────────────────────────────────
def _rate(n: int, d: int) -> Optional[float]:
    """分母为 0 一律 None（不写 0.0 冒充「比例是 0」）。"""
    return (n / d) if d else None


def report(rows: Iterable[Dict[str, Any]], days: int = 7) -> Dict[str, Any]:
    """把终态行聚合成体检报告。纯函数，便于离线单测。"""
    items = [classify(r) for r in rows]
    n_all = len(items)
    states = Counter(i["state"] for i in items)
    actions = Counter(i["action"] for i in items)
    reasons = Counter(i["no_trade_reason"] for i in items)
    degraded_reasons = Counter(i["degraded_reason"] for i in items if i["degraded_reason"])

    n_out = states.get("out_of_scope", 0)
    n_in_scope = n_all - n_out
    n_fail = sum(states.get(s, 0) for s in ("data_unavailable", "llm_failed", "parse_failed"))
    n_directional = sum(actions.get(a, 0) for a in DIRECTIONAL_ACTIONS)

    # 证据就绪率：分母只算「库里确实写了证据状态的」，空的单独报未知
    known_hard = [i for i in items if i["hard_status"]]
    n_hard_ok = sum(1 for i in known_hard if i["hard_status"] == "ok")

    # 逐日趋势（created_at 是北京时间 TEXT；缺失的归 unknown，不并进某一天）
    daily: Dict[str, Counter] = defaultdict(Counter)
    for i in items:
        day = (i["created_at"] or "")[:10] or "unknown"
        daily[day][i["state"]] += 1

    buckets = []
    for st in ac.ANALYSIS_STATES:
        label, is_failure, todo = DISPOSITION[st]
        n = states.get(st, 0)
        buckets.append({
            "state": st,
            "label": label,
            "cn": ac.STATE_LABELS.get(st, st),
            "n": n,
            "share_of_all": _rate(n, n_all),
            "share_of_in_scope": None if st == "out_of_scope" else _rate(n, n_in_scope),
            "counts_as_failure": is_failure,
            "todo": todo,
            "retryable": st in RETRYABLE_STATES,
        })

    return {
        "days": int(days),
        "generated_at": int(__import__("time").time()),
        "totals": {
            "all": n_all,
            "out_of_scope": n_out,
            "in_scope": n_in_scope,
            "real_failure": n_fail,
            "directional": n_directional,
            "hard_status_known": len(known_hard),
            "hard_status_unknown": n_all - len(known_hard),
        },
        "rates": {
            # 分母写进字段名，避免「比例 89%」这种没有分母的数
            "complete_over_in_scope": _rate(states.get("complete", 0), n_in_scope),
            "usable_over_in_scope": _rate(states.get("complete", 0) + states.get("compressed", 0),
                                          n_in_scope),
            "real_failure_over_in_scope": _rate(n_fail, n_in_scope),
            "directional_per_day": _rate(n_directional, max(1, int(days))),
            "evidence_ready_over_known": _rate(n_hard_ok, len(known_hard)),
        },
        "buckets": buckets,
        "actions": dict(actions),
        "no_trade_reasons": dict(reasons),
        "degraded_reasons": dict(degraded_reasons),
        "daily": {d: dict(c) for d, c in sorted(daily.items())},
        "definitions": {
            "in_scope": "全部决策 - out_of_scope（品种不在服务范围）",
            "complete_over_in_scope": "complete / in_scope",
            "usable_over_in_scope": "(complete + compressed) / in_scope",
            "real_failure_over_in_scope": "(data_unavailable + llm_failed + parse_failed) / in_scope",
            "evidence_ready_over_known": "hard_data_status='ok' / 库里写了证据状态的行（空值单列为 unknown）",
            "directional_per_day": "(BUY + SELL) / 天数",
        },
    }


# ── 通道质量阈值（**未回测**，只用于把"明显异常"标出来，不用于排名）────
# 依据：analysis_state_audit.py 用 tokens_out<600 判「八段写不完」；
# 主线 MiniMax-M3 实测 18.2% 短输出，peer 通道 M2.7 实测 69.8%。
# 这两个阈值是"看板口径"，不是统计结论；要当准入规则必须先回测。
SHORT_OUT_TOKENS = 600
SHORT_RATE_WATCH = 0.20        # ≥20% 标「观察」
SHORT_RATE_BAD = 0.40          # ≥40% 标「不建议用于主分析」
CHANNEL_MIN_CALLS = 20         # 少于这么多调用不下结论（避免小样本误判）


def channel_quality(conn: sqlite3.Connection, days: int = 7) -> List[Dict[str, Any]]:
    """按 model_id 汇总「输出质量」——不看它写了多少 token，只看写够没写够。

    为什么单列：`llm_failed` 的根因不是调用失败（近 7 天 2868 次主分析调用只有
    5 次 429），而是**调用成功但输出太短**（<600 tokens 写不完八段）。这件事只有
    按通道拆才看得出来：主线 M3 短输出 18%，peer 通道 M2.7 短输出 70%。

    口径：
      · short_rate  = tokens_out < 600 / ok 调用
      · empty_rate  = tokens_out < 200 / ok 调用
      · 分母只算 status='ok' 的调用；失败率单独给（fail_n / n）
      · 调用数 < CHANNEL_MIN_CALLS 时 verdict 强制为「样本不足」
    """
    if "ai_call_stats" not in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        return []
    rows = conn.execute(
        """SELECT model_id, COUNT(*) AS n,
                  SUM(status='ok') AS ok_n,
                  SUM(status='ok' AND tokens_out < ?) AS short_n,
                  SUM(status='ok' AND tokens_out < 200) AS empty_n,
                  AVG(CASE WHEN status='ok' THEN tokens_out END) AS avg_out,
                  AVG(CASE WHEN status='ok' THEN latency_ms END) AS avg_ms,
                  SUM(tokens_in) AS tokens_in
             FROM ai_call_stats
            WHERE purpose='main' AND ts >= ?
            GROUP BY model_id ORDER BY n DESC""",
        (SHORT_OUT_TOKENS, _window_since(days)),
    ).fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        n = int(r["n"] or 0)
        ok_n = int(r["ok_n"] or 0)
        short_n = int(r["short_n"] or 0)
        empty_n = int(r["empty_n"] or 0)
        short_rate = _rate(short_n, ok_n)
        empty_rate = _rate(empty_n, ok_n)
        if n < CHANNEL_MIN_CALLS:
            verdict, why = "样本不足", f"调用 {n} 次 < {CHANNEL_MIN_CALLS}，不下结论"
        elif short_rate is not None and short_rate >= SHORT_RATE_BAD:
            verdict, why = "不建议用于主分析", f"短输出 {short_rate:.0%} ≥ {SHORT_RATE_BAD:.0%}（八段写不完）"
        elif short_rate is not None and short_rate >= SHORT_RATE_WATCH:
            verdict, why = "观察", f"短输出 {short_rate:.0%} ≥ {SHORT_RATE_WATCH:.0%}"
        else:
            verdict, why = "正常", ""
        out.append({
            "model_id": r["model_id"] or "(未标注)",
            "calls": n,
            "ok_calls": ok_n,
            "fail_rate": _rate(n - ok_n, n),
            "short_rate": short_rate,
            "empty_rate": empty_rate,
            "avg_out_tokens": None if r["avg_out"] is None else round(float(r["avg_out"])),
            "avg_latency_s": None if r["avg_ms"] is None else round(float(r["avg_ms"]) / 1000, 1),
            "tokens_in": int(r["tokens_in"] or 0),
            "verdict": verdict,
            "why": why,
        })
    return out


# ── 队列水位 ────────────────────────────────────────────────────────────
def queue_health(conn: sqlite3.Connection, hours: int = 24) -> Dict[str, Any]:
    """队列现在堵不堵、还要多久排空。

    口径（**代理口径，写清楚再用**）：
      · 入库速率 = raw_news 在近 N 小时新进来的条数 / N
      · 出队速率 = 近 N 小时离开 PENDING 的条数 / N
        —— **离开不等于出决策**：门卫否掉的会直接进 FILTERED，同样是"处理完"。
        —— 只按决策数算速率会严重低估（实测 24h 门卫过滤 2763 条，决策只有百余条），
          于是队列明明在掉，图表却说"积压在涨"。这是第一版的口径错误，已修正。
      · 预计排空 = PENDING / 出队速率；出队速率为 0 或无数据 → None（不猜）
    raw_news 没有完成时间戳，所以"出队"只能按 status 落在窗口内近似，这是**代理值**。
    没有 raw_news 表时返回 {"available": False}，不编数字。
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "raw_news" not in tables:
        return {"available": False, "reason": "raw_news 表不存在"}

    hours = max(1, int(hours))
    now_ts = int(__import__("time").time())
    since = now_ts - hours * 3600

    # v414 补二·修：PENDING 必须数全量。之前加了 is_noise=0 的过滤，
    # 漏掉 208 条 is_noise=1 却仍挂在 PENDING 的行（实测 52 vs 真值 262），
    # 于是「队列快排空」是个假象。噪音待分析单列，不混进总数。
    status = dict(conn.execute(
        "SELECT status, COUNT(*) FROM raw_news GROUP BY 1"
    ).fetchall())

    oldest = conn.execute(
        """SELECT MIN(ts) FROM raw_news
            WHERE status='PENDING' AND is_noise=0 AND ts IS NOT NULL"""
    ).fetchone()[0]
    oldest_age_min = None if not oldest else max(0, int((now_ts - float(oldest)) / 60))

    intake_n = conn.execute(
        "SELECT COUNT(*) FROM raw_news WHERE ts >= ?", (since,)
    ).fetchone()[0]
    # 出队 = 窗口内落在任一终态的条数（DONE/FILTERED/FAILED），不含 PENDING/PROCESSING
    out_n = conn.execute(
        """SELECT COUNT(*) FROM raw_news
            WHERE ts >= ? AND status IN ('DONE','FILTERED','FAILED')""", (since,)
    ).fetchone()[0]
    noise_pending = conn.execute(
        "SELECT COUNT(*) FROM raw_news WHERE status='PENDING' AND COALESCE(is_noise,0)=1"
    ).fetchone()[0]
    # 真正的"分析产出"另算：窗口内有决策的不同新闻条数（去多品种膨胀）
    decided_n = conn.execute(
        "SELECT COUNT(DISTINCT news_id) FROM ai_decisions"
        " WHERE ts >= ? AND news_id IS NOT NULL", (since,)
    ).fetchone()[0]

    intake_per_h = intake_n / hours
    out_per_h = out_n / hours
    decided_per_h = decided_n / hours
    pending = int(status.get("PENDING", 0))
    # 认领 SQL 只收 is_noise=0（见 ai_worker._BATCH_CLAIM_SQL）→ 噪音行不会被分析，
    # 只会等清理脚本归档。所以「要排的空」是 actionable 那一半。
    pending_actionable = max(0, pending - int(noise_pending))
    eta_h = (pending_actionable / out_per_h) if out_per_h > 0 else None

    if pending_actionable == 0:
        verdict = "队列已清空（剩 %d 条噪音滞留待清理）" % int(noise_pending)
    elif out_per_h <= 0:
        verdict = "停摆：近 %d 小时没有一条新闻离开 PENDING" % hours
    elif intake_per_h > out_per_h * 1.5:
        verdict = "积压在涨：进 %.1f 条/h > 出 %.1f 条/h" % (intake_per_h, out_per_h)
    else:
        verdict = "正常消化：进 %.1f 条/h ≈ 出 %.1f 条/h" % (intake_per_h, out_per_h)

    return {
        "available": True,
        "hours": hours,
        "pending": pending,
        "pending_actionable": pending_actionable,
        "noise_pending": int(noise_pending),
        "processing": int(status.get("PROCESSING", 0)),
        "done": int(status.get("DONE", 0)),
        "filtered": int(status.get("FILTERED", 0)),
        "oldest_pending_age_min": oldest_age_min,
        "intake_per_h": round(intake_per_h, 1),
        "out_per_h": round(out_per_h, 1),
        "decided_news_per_h": round(decided_per_h, 1),
        "eta_drain_h": None if eta_h is None else round(eta_h, 1),
        "verdict": verdict,
    }


# ── 门卫漏斗 ────────────────────────────────────────────────────────────
def gate_funnel(conn: sqlite3.Connection, days: int = 7) -> Dict[str, Any]:
    """新闻从进来到出决策，死在哪一层。

    为什么要有这个：产能体检只看"分析出来的那几十条"会得出"模型写不完"的结论，
    但把入口算上才发现——近 7 天进来 1 万多条，九成被拦在门外，真正进分析的只有零头。
    不看这一层，优化模型等于给漏水的桶换水龙头。

    「谁否掉的」按 gate_unavailable 拆（db.py 定义：0 正常判定 / 1 冻结待回放 /
    2 鉴权失效或额度耗尽→降级放行）：
      · filtered_by_gate     门卫**有判定**后否掉 —— 这是真正的过滤
      · filtered_gate_down   门卫没判定、降级放行后仍被否 —— 等于白丢
      · analyzed_gate_down   门卫没判定却出了决策 —— 决策缺 gate_context，质量要打问号
    这三类只报数，不下结论；改门卫之前必须先抽样看。
    另外单列 verdict_tradeable_but_filtered / filtered_with_asset 两个可疑项。
    """
    since = int(__import__("time").time()) - days * 86400
    inbound = conn.execute(
        "SELECT COUNT(*) FROM raw_news WHERE ts >= ? AND COALESCE(is_noise,0)=0",
        (since,)).fetchone()[0]
    auth = conn.execute(
        "SELECT COUNT(*) FROM raw_news WHERE ts >= ? AND COALESCE(is_noise,0)=0"
        " AND status='PENDING' AND gate_unavailable=1", (since,)).fetchone()[0]

    def _cnt(where: str, *params) -> int:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM raw_news WHERE ts >= ? AND COALESCE(is_noise,0)=0 AND {where}",
            (since, *params)).fetchone()[0] or 0)

    by_status = dict(conn.execute(
        """SELECT status, COUNT(*) FROM raw_news
            WHERE ts >= ? AND COALESCE(is_noise,0)=0 GROUP BY 1""", (since,)).fetchall())
    filtered = int(by_status.get("FILTERED", 0))
    analyzed = int(by_status.get("DONE", 0)) + int(by_status.get("PROCESSING", 0))

    filtered_by_gate = _cnt("status='FILTERED' AND COALESCE(gate_unavailable,0)=0")
    filtered_gate_down = _cnt("status='FILTERED' AND gate_unavailable=2")
    analyzed_gate_down = _cnt("status IN ('DONE','PROCESSING') AND gate_unavailable=2")
    tradeable_but_filtered = _cnt("status='FILTERED' AND gate_verdict='可交易'")
    with_asset = _cnt(
        "status='FILTERED' AND gate_main_asset IS NOT NULL AND gate_main_asset NOT IN ('','NONE')")
    # v419 新增：过了宽限才归档的门卫未判定行（归档原因单独留痕，可精确回放）
    expired_gate_down = _cnt("filter_reason='expired_gate_down'")
    # v433（P0-3）：门卫故障导致「退出建仓候选」的行 —— 必须与「噪音 / 超龄」分开报，
    # 否则 885 条/天会被读成「新闻旧了」，故障永远看不见。
    gate_down_archived = _cnt("filter_reason='gate_down'")
    # v441：归档理由补齐后，把「为什么没进分析」按真实原因拆开（原先这批全是 ''，
    # 报表只能写"未知"，等于看不见）。口径与 scripts/backfill_filter_reason.py 一致。
    archived_by_reason = dict(conn.execute(
        """SELECT CASE WHEN filter_reason IS NULL OR filter_reason='' THEN 'unknown'
                    ELSE filter_reason END r, COUNT(*) FROM raw_news
            WHERE ts >= ? AND status='FILTERED' AND COALESCE(is_noise,0)=0
            GROUP BY 1 ORDER BY 2 DESC""", (since,)).fetchall())
    decided_news = conn.execute(
        "SELECT COUNT(DISTINCT news_id) FROM ai_decisions WHERE ts >= ? AND news_id IS NOT NULL",
        (since,)).fetchone()[0]

    # v406 修复：这里原来是 def _rate(n)，与模块级 _rate(n, d) 同名 →
    # 局部遮蔽模块级绑定，函数内任何对模块级 _rate 的调用都会 UnboundLocalError。
    # 改名，语义不变（都是"占 inbound 的比例"）。
    def _share_of_inbound(n: int) -> Optional[float]:
        return None if not inbound else round(n / inbound, 4)

    return {
        "available": inbound > 0,
        "days": days,
        "inbound": inbound,
        "filtered": filtered,
        "analyzed": analyzed,
        "frozen": int(auth),
        "decided_news": int(decided_news or 0),
        "filter_rate": _share_of_inbound(filtered),
        "analyze_rate": _share_of_inbound(analyzed),
        "filtered_by_gate": filtered_by_gate,
        "filtered_gate_down": filtered_gate_down,
        "gate_down_share": _rate(filtered_gate_down, inbound),
        "analyzed_gate_down": analyzed_gate_down,
        "expired_gate_down": expired_gate_down,
        "gate_down_archived": gate_down_archived,
        "archived_by_reason": archived_by_reason,
        "verdict_tradeable_but_filtered": tradeable_but_filtered,
        "filtered_with_asset": with_asset,
        "note": "filtered_gate_down / analyzed_gate_down 是门卫不可用的连带损失，只用于抽样复核",
    }


# ── 重试清单 ────────────────────────────────────────────────────────────
def retry_candidates(conn: sqlite3.Connection, days: int = 7,
                     limit: int = 200) -> List[Dict[str, Any]]:
    """模型侧失败、且之后没有成功分析的新闻 → 可重跑清单（按 news_id 去重）。

    只挑 `llm_failed`：模型没返回，重跑有概率成；`parse_failed` 是契约问题，
    原样重跑大概率还是失败，宁可改 prompt，不放进清单里凑数。
    """
    rows = [classify(r) for r in load_rows(conn, days)]
    failed: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    recovered: set[int] = set()
    for i in rows:
        nid = i["news_id"]
        if not nid:
            continue
        if i["state"] in ("complete", "compressed"):
            recovered.add(int(nid))
        elif i["state"] in RETRYABLE_STATES:
            failed[int(nid)].append(i)

    out: List[Dict[str, Any]] = []
    for nid, items in failed.items():
        if nid in recovered:
            continue                      # 之后已经分析出来了，不用重跑
        items.sort(key=lambda x: x["ts"])
        last = items[-1]
        out.append({
            "news_id": nid,
            "attempts": len(items),
            "last_ts": last["ts"],
            "last_at": last["created_at"],
            "reason": last["degraded_reason"] or "llm_failed",
            "asset": last["asset"],
            "head": last["head"],
        })
    out.sort(key=lambda x: (-x["attempts"], -x["last_ts"]))
    return out[:max(1, int(limit))]


# ── 连接 ────────────────────────────────────────────────────────────────
def _connect_readonly(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or os.path.join(os.path.dirname(_HERE), "trident_event_bus.db")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


# ── CLI ────────────────────────────────────────────────────────────────
def _fmt_pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _print_report(rep: Dict[str, Any], retry: Sequence[Dict[str, Any]]) -> None:
    t = rep["totals"]
    r = rep["rates"]
    print(f"\n== 分析产能体检 ==  近 {rep['days']} 天  决策 {t['all']} 条\n")
    print(f"  范围内 {t['in_scope']} 条（剔除越界 {t['out_of_scope']} 条），真实失败 {t['real_failure']} 条")
    print(f"  完整率 {_fmt_pct(r['complete_over_in_scope'])} · 可用率 {_fmt_pct(r['usable_over_in_scope'])}"
          f" · 证据就绪 {_fmt_pct(r['evidence_ready_over_known'])}（未知 {t['hard_status_unknown']} 条）")
    print(f"  方向性 {t['directional']} 条 = {r['directional_per_day']:.2f} 条/日\n"
          if r["directional_per_day"] is not None else "")
    print(f"{'终态':<18}{'条数':>6}{'占范围内':>10}  处置")
    for b in rep["buckets"]:
        share = "—" if b["share_of_in_scope"] is None else f"{b['share_of_in_scope'] * 100:.1f}%"
        print(f"  {b['cn']:<16}{b['n']:>6}{share:>10}  {b['todo']}")
    print(f"\n可重试（模型没返回、之后没成功过）：{len(retry)} 条新闻")
    for x in retry[:5]:
        print(f"  · news={x['news_id']} attempts={x['attempts']} {x['head'][:46]}")

    q = rep.get("queue") or {}
    if q.get("available"):
        eta = "—" if q["eta_drain_h"] is None else f"{q['eta_drain_h']}h"
        print(f"\n队列：PENDING {q['pending']} / 进 {q['intake_per_h']}条·h / 出 {q['out_per_h']}条·h"
              f" / 排空约 {eta} → {q['verdict']}")

    fu = rep.get("funnel") or {}
    if fu.get("available"):
        print(f"\n门卫漏斗（近 {fu['days']} 天）：进来 {fu['inbound']} → 否掉 {fu['filtered']}"
              f" ({fu['filter_rate'] * 100:.1f}%) → 进分析 {fu['analyzed']} → 出决策新闻 {fu['decided_news']}")
        print(f"  分层：门卫有判定后否 {fu['filtered_by_gate']}"
              f" / 门卫不可用(降级放行)后被否 {fu['filtered_gate_down']}"
              f" / 门卫不可用仍出决策 {fu['analyzed_gate_down']}"
              f" / 冻结待回放 {fu['frozen']}")
        abr = fu.get("archived_by_reason") or {}
        if abr:
            detail = " / ".join(f"{k} {v}" for k, v in list(abr.items())[:6])
            print(f"  归档理由：{detail}")
        print(f"  过宽限归档(expired_gate_down)：{fu['expired_gate_down']} 条"
              f"（v419 起这类才允许归档，且留痕可回放）")
        print(f"  门卫故障退出建仓候选(gate_down)：{fu.get('gate_down_archived', 0)} 条"
              f"（v433 起单列；不是噪音、不是旧闻，门卫没判 → 可回放）")
        print(f"  可疑项：门卫判可交易却被否 {fu['verdict_tradeable_but_filtered']}"
              f" / 有标的被否 {fu['filtered_with_asset']}（{fu['note']}）")

    ch = rep.get("channels") or []
    if ch:
        print("\n模型通道质量（分母只算 status=ok；阈值未回测，只用于抓异常）")
        print(f"  {'model_id':<34}{'调用':>6}{'短输出':>8}{'平均输出':>10}  判定")
        for c in ch:
            sr = "—" if c["short_rate"] is None else f"{c['short_rate'] * 100:.1f}%"
            print(f"  {c['model_id']:<34}{c['calls']:>6}{sr:>8}"
                  f"{str(c['avg_out_tokens'] or '—'):>10}  {c['verdict']} {c['why']}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="分析产能体检（只读）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db", default=None, help="默认 backend/trident_event_bus.db")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--export-retry", default="", help="把重试清单写成 JSONL")
    a = ap.parse_args(argv)

    try:
        conn = _connect_readonly(a.db)
    except FileNotFoundError as e:
        print(f"[FATAL] 库不存在：{e}")
        return 2
    try:
        rows = load_rows(conn, a.days)
        rep = report(rows, days=a.days)
        retry = retry_candidates(conn, a.days)
        rep["channels"] = channel_quality(conn, a.days)
        rep["queue"] = queue_health(conn, hours=min(24, a.days * 24))
        rep["funnel"] = gate_funnel(conn, a.days)
    finally:
        conn.close()

    rep["retry"] = {"n": len(retry), "items": retry[:50]}
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_report(rep, retry)
    if a.export_retry:
        with open(a.export_retry, "w", encoding="utf-8") as f:
            for x in retry:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
        print(f"\n重试清单已写出：{a.export_retry}（{len(retry)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
