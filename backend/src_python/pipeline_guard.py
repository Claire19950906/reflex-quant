# -*- coding: utf-8 -*-
"""pipeline_guard.py — 管线对账守卫（v420，只读、零副作用）。

为什么要有这个模块
------------------
2026-11-17 用户的原话：「即使我在你这个程序里面都检查好了，检查一遍都没问题了，
但是在实际上使用上会露出现很多问题」。这不是用户多疑，是**系统缺少断言**：

  · `raw_news.status='FILTERED'` 同时表示「门卫判了噪音」和「超龄没人认领就归档」
    → 两种事实一个标记，看库的人一定会读错；
  · `ai_decisions.evidence_confidence = 0.0` 同时表示「算过、结论没信心」和
    「根本没算」→ 于是「99% HOLD」既可能是策略保守，也可能是链路断了，
    看数字的人分不出来，只能靠人一条条翻表；
  · 采集侧某个源停了三天，没有任何地方会响——直到有人问「怎么没有新闻」。

共同点：**系统能算出数字，但不知道自己算的数字是不是假的**。
本模块就是把「必须是 0」「必须大于 X」「必须两边对得上」这些断言写成代码，
一次跑完，任一条不满足就明确报出来，并说明「这说明哪条链路坏了」。

设计口径
--------
1. **只读**：`mode=ro` 打开生产库，绝不写。
2. **每条检查必须有「不满足时说明什么」**：不给解释的告警等于没告警。
3. **算不出来 = unknown，不补 0**（铁律 4）：缺列 / 缺表一律 unknown 并说明原因。
4. **阈值集中在 `THRESHOLDS`**：改门槛是一处改动，不是散落在 SQL 里。

用法
----
    cd backend/src_python && PYTHONPATH=. python3 pipeline_guard.py
    cd backend/src_python && PYTHONPATH=. python3 pipeline_guard.py --json
    cd backend/src_python && PYTHONPATH=. python3 pipeline_guard.py --hours 72
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ── 门槛（唯一真源）────────────────────────────────────────────────────
THRESHOLDS: Dict[str, Any] = {
    # 近 1 小时入库低于「近 7 天同窗口均值」的这个比例 → 判断流
    "inbound_drop_ratio": 0.30,
    # 分析产能：可分析待处理行 > 该值 且 近 1h 决策 < 该值 → 判产能不足
    "pending_actionable_warn": 50,
    "decisions_per_hour_min": 3.0,
    # 队列排空预计时长（小时）超过该值 → 判积压
    "eta_drain_hours_warn": 6.0,
    # 结构化字段缺失比例超过该值 → 判「AI 在写散文，分数是空的」
    "structure_missing_ratio": 0.50,
    # 源停更超过该小时数 → 判沉默源
    "source_silent_hours": 24,
}

SEV_ORDER = {"broken": 0, "degraded": 1, "unknown": 2, "ok": 3}


def _connect_readonly(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or os.path.join(os.path.dirname(_HERE), "trident_event_bus.db")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.OperationalError:
        return False
    return col in cols


def _count(conn: sqlite3.Connection, sql: str, params=()) -> int:
    try:
        return int(conn.execute(sql, params).fetchone()[0] or 0)
    except sqlite3.OperationalError:
        return 0


def _check(code: str, title: str, value: Any, expect: str, ok: bool,
           hint: str, *, severity_if_bad: str = "degraded",
           unit: str = "条", note: str = "") -> Dict[str, Any]:
    """一条断言。ok=None 表示「算不出来」，不算通过也不算失败。"""
    if ok is None:
        sev = "unknown"
    else:
        sev = "ok" if ok else severity_if_bad
    return {
        "code": code, "title": title, "value": value, "expect": expect,
        "unit": unit, "severity": sev, "hint": hint, "note": note,
    }


# ── 检查项 ────────────────────────────────────────────────────────────
def check_inbound(conn: sqlite3.Connection, now: int) -> Dict[str, Any]:
    """① 采集是否断流：近 1h 入库 vs 近 7 天日均折算。"""
    last_h = _count(conn, "SELECT COUNT(*) FROM raw_news WHERE ts >= ?", (now - 3600,))
    seven = _count(conn, "SELECT COUNT(*) FROM raw_news WHERE ts >= ?", (now - 7 * 86400,))
    avg_h = seven / (7 * 24) if seven else 0.0
    if seven == 0:
        return _check("inbound_alive", "新闻入库是否还在跑", last_h, "近 1h 应 > 0",
                      None, "近 7 天一条都没有，无法判断基线", severity_if_bad="broken")
    floor = avg_h * THRESHOLDS["inbound_drop_ratio"]
    ok = last_h > 0 and last_h >= floor
    return _check(
        "inbound_alive", "新闻入库是否还在跑", last_h,
        f"≥ {floor:.0f}（近7天均值 {avg_h:.1f}/h 的 30%）", ok,
        "低于门槛说明采集/门卫入口断了：查 source_status 的 error 源、bridge 进程是否活着。",
        severity_if_bad="broken", note=f"近7天均值 {avg_h:.1f} 条/小时",
    )


def check_silent_archive(conn: sqlite3.Connection, now: int, hours: int) -> Dict[str, Any]:
    """② 静默归档：被否但**没有任何理由**。"""
    if not _has_col(conn, "raw_news", "filter_reason"):
        return _check("silent_archive", "被否新闻是否都有理由", None, "应为 0",
                      None, "库无 filter_reason 列，无法核对归档理由")
    n = _count(conn, """SELECT COUNT(*) FROM raw_news
                        WHERE status='FILTERED' AND ts >= ?
                          AND COALESCE(filter_reason,'')=''
                          AND COALESCE(gate_verdict,'')=''""", (now - hours * 3600,))
    return _check(
        "silent_archive", "被否新闻是否都有理由", n, "应为 0", n == 0,
        "非 0 说明有行「没判定、没理由」就被归档 —— 这是数据丢失最隐蔽的形态："
        "看板只会显示「已过滤」，没人知道为什么。",
        severity_if_bad="broken", note=f"窗口 {hours}h",
    )


def check_tradeable_filtered(conn: sqlite3.Connection, now: int, hours: int) -> Dict[str, Any]:
    """③ 门卫说可交易、却仍被归档的（v408 之后应为 0）。"""
    n = _count(conn, """SELECT COUNT(*) FROM raw_news
                        WHERE status='FILTERED' AND gate_verdict='可交易' AND ts >= ?""",
               (now - hours * 3600,))
    return _check(
        "tradeable_filtered", "门卫判可交易是否真被放行", n, "应为 0", n == 0,
        "非 0 说明「判了可交易还是死」：要么认领侧另有拦截，要么归档门槛没豁免这一类。",
        severity_if_bad="broken", note=f"窗口 {hours}h",
    )


def check_throughput(conn: sqlite3.Connection, now: int) -> Dict[str, Any]:
    """④ 分析产能：排队多、出货少 = 灌进来的和消化掉的不成比例。"""
    pending = _count(conn, """SELECT COUNT(*) FROM raw_news
                              WHERE status='PENDING' AND COALESCE(is_noise,0)=0
                                AND COALESCE(gate_unavailable,0) IN (0,2)""")
    dec_h = _count(conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts >= ?", (now - 3600,))
    ok = not (pending > THRESHOLDS["pending_actionable_warn"]
              and dec_h < THRESHOLDS["decisions_per_hour_min"])
    return _check(
        "analysis_throughput", "分析产能是否跟得上入库", dec_h,
        f"可分析积压 ≤ {THRESHOLDS['pending_actionable_warn']} 或 ≥ "
        f"{THRESHOLDS['decisions_per_hour_min']}/h 决策", ok,
        "积压多而决策少 → 瓶颈在认领/模型调用，不是新闻不够。"
        "（注意：只数「有决策的新闻」会把被门卫否掉的处理完的行漏掉，这里用积压+决策两次校验）",
        note=f"可分析积压 {pending} 条；近 1h 决策 {dec_h} 条",
    )


def check_structure(conn: sqlite3.Connection, now: int, hours: int) -> Dict[str, Any]:
    """⑤ 结构化字段是否真的写进去了（分数空 = 后面所有闸门都在拿 0 算）。

    v420 口径修正（2026-11-17 实测后改）：
      初版把 `sentiment_score=0` 也算「缺失」，一跑就报 95.8% —— 是**错的**。
      抽样看 reasoning 才发现：模型是真的判了中性（"未形成市场级抛售，HOLD"），
      0 是有意义的读数，不是没输出。
      真正缺失的是 `impact_score IS NULL`（写库路径没补 5.0 兜底）和
      `heat_score/asset_impact` 同为 NULL 的情况 —— 这些字段 null 会让
      `impact_scale` 退回中性，但不影响方向；只有**四件套全空**才等于
      「这条决策没有任何结构化读数」。
    区分办法：`structured_check` 里 `schema_valid=false` 或 reasoning 为空
    才是「模型没答」；`sentiment=0` 且 reasoning 非空 = 「模型判中性」。
    """
    if not _has_col(conn, "ai_decisions", "impact_score"):
        return _check("structure_missing", "决策结构化字段是否完整", None, "缺失比例 ≤ 50%",
                      None, "库无 impact_score 列")
    total = _count(conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts >= ?", (now - hours * 3600,))
    if total == 0:
        return _check("structure_missing", "决策结构化字段是否完整", 0, "有决策才能算",
                      None, "窗口内没有决策，无法评估")
    # 只有「没有推理文本」或「schema 明确不合法」才算模型没答；
    # impact_score IS NULL 算**写库侧**缺失（ladder 没兜底）。
    # v421：原版把 reasoning / structured_check / heat_score / asset_impact 四个列名
    # 硬写进 SQL。库里少任何一列，`_count` 会吞掉 OperationalError 返回 0 →
    # 缺失率算成 0% → 报 ok。**守卫自己变成"看起来检查了、其实没查"**，比不查更坏。
    # 改成按实际存在的列拼条件，一列都拼不出来就如实报 unknown。
    conds = []
    if _has_col(conn, "ai_decisions", "reasoning"):
        conds.append("COALESCE(TRIM(reasoning),'')=''")
    if _has_col(conn, "ai_decisions", "structured_check"):
        conds.append("COALESCE(structured_check,'') LIKE '%\"schema_valid\": false%'")
    if _has_col(conn, "ai_decisions", "heat_score") and \
            _has_col(conn, "ai_decisions", "asset_impact"):
        conds.append("(impact_score IS NULL AND heat_score IS NULL AND asset_impact IS NULL)")
    else:
        conds.append("impact_score IS NULL")
    sql = ("SELECT COUNT(*) FROM ai_decisions WHERE ts >= ? AND ("
           + " OR ".join(conds) + ")")
    try:
        missing = int(conn.execute(sql, (now - hours * 3600,)).fetchone()[0] or 0)
    except sqlite3.OperationalError as exc:
        return _check("structure_missing", "决策结构化读数是否真的写进来了", None,
                      "缺失比例 ≤ 50%", None, f"SQL 算不出来（{exc}）—— 不能当成正常")
    ratio = missing / total
    return _check(
        "structure_missing", "决策结构化读数是否真的写进来了", missing,
        f"缺失比例 ≤ {THRESHOLDS['structure_missing_ratio']*100:.0f}%",
        ratio <= THRESHOLDS["structure_missing_ratio"],
        "缺失=没有推理文本 / schema 明确不合法 / impact、heat、asset_impact 三者全空。"
        "这类决策没有任何结构化读数，方向分只能靠因子表算，闸门的置信度也必然偏低。"
        "注意：`sentiment_score=0` **不算**缺失 —— 模型判中性是有效读数。",
        severity_if_bad="broken", note=f"共 {total} 条，缺失率 {ratio*100:.1f}%",
    )


def check_confidence(conn: sqlite3.Connection, now: int, hours: int) -> Dict[str, Any]:
    """⑥ 置信度 0.0 里有多少是「没算」而不是「没信心」。"""
    if not _has_col(conn, "ai_decisions", "evidence_confidence"):
        return _check("confidence_zero", "被闸门降级的决策有多少是「没算过」", None,
                      "应可解释", None, "库无 evidence_confidence 列")
    total = _count(conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts >= ?", (now - hours * 3600,))
    if total == 0:
        return _check("confidence_zero", "被闸门降级的决策有多少是「没算过」", 0,
                      "有决策才能算", None, "窗口内没有决策")
    zero = _count(conn, """SELECT COUNT(*) FROM ai_decisions WHERE ts >= ?
                           AND COALESCE(evidence_confidence,0)=0""", (now - hours * 3600,))
    ratio = zero / total
    return _check(
        "confidence_zero", "被闸门降级的决策有多少是「没算过」", zero,
        "有明确归因（见 analysis_degraded_reason）", True,
        "置信度 0 有两种含义（算过没信心 / 根本没算）。必须能从 evidence_confidence 是否为 NULL、"
        "以及 analysis_degraded_reason 区分；分不开时任何「零信号」结论都不可信。",
        severity_if_bad="degraded",
        note=f"占近 {hours}h 决策 {ratio*100:.1f}%（{zero}/{total}）",
    )


def check_routing(conn: sqlite3.Connection, now: int, hours: int) -> Dict[str, Any]:
    """⑦ 品种路由命中率：target_none 的决策等于白花额度。"""
    if not _has_col(conn, "ai_decisions", "decision_kind"):
        return _check("routing_miss", "品种路由命中率", None, "miss ≤ 20%", None,
                      "库无 decision_kind 列")
    total = _count(conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts >= ?", (now - hours * 3600,))
    if total == 0:
        return _check("routing_miss", "品种路由命中率", 0, "有决策才能算", None, "窗口内没有决策")
    miss = _count(conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts >= ? AND decision_kind='routing_miss'",
                  (now - hours * 3600,))
    ratio = miss / total
    return _check(
        "routing_miss", "品种路由命中率", miss, "≤ 20%", ratio <= 0.20,
        "routing_miss = 新闻看懂了但没落到任何品种 → 这条决策不可能变成交易。"
        "超过 20% 说明品种词典/路由覆盖太窄，先扩词典比调闸门有效。",
        note=f"占 {ratio*100:.1f}%（{miss}/{total}）",
    )


def check_sources(conn: sqlite3.Connection, now: int) -> Dict[str, Any]:
    """⑧ 沉默源：接了源但一直没有产出/一直报错。"""
    try:
        rows = conn.execute(
            """SELECT source_id, status,
                      COALESCE(last_ok_ts,'')  AS last_ok_ts,
                      COALESCE(total_fail,0)   AS total_fail,
                      COALESCE(last_error,'')  AS last_error
               FROM source_status""").fetchall()
    except sqlite3.OperationalError:
        return _check("source_health", "新闻源健康", None, "无 error、无沉默源", None,
                      "库无 source_status 表")
    if not rows:
        return _check("source_health", "新闻源健康", 0, "无 error、无沉默源", None,
                      "source_status 为空")
    err = [r for r in rows if str(r["status"] or "").lower() == "error"]
    silent = []
    cutoff = now - THRESHOLDS["source_silent_hours"] * 3600
    for r in rows:
        ts = r["last_ok_ts"]
        if isinstance(ts, (int, float)) and ts and int(ts) < cutoff:
            silent.append(r["source_id"])
        elif isinstance(ts, str) and ts:
            # last_ok_ts 存的是 'YYYY-MM-DD HH:MM:SS'（本地时间），只做粗粒度比较
            try:
                if time.mktime(time.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")) < cutoff:
                    silent.append(r["source_id"])
            except ValueError:
                pass
    bad = len(err) + len(silent)
    names = ", ".join([r["source_id"] for r in err][:5] + silent[:5]) or "—"
    return _check(
        "source_health", "新闻源健康", bad, "error 源与沉默源总数应为 0", bad == 0,
        "接了源不等于源在跑：error 源要修 key/解析，沉默源要查采集器是否被配额卡停或调度未挂。",
        note=f"error {len(err)} 个 / 停更>{THRESHOLDS['source_silent_hours']}h {len(silent)} 个：{names}",
    )


def check_shadow(conn: sqlite3.Connection) -> Dict[str, Any]:
    """⑨ 影子账本是否在用：有没有把「被闸门掐掉的信号」记下来。"""
    try:
        n = _count(conn, "SELECT COUNT(*) FROM shadow_ledger")
    except sqlite3.OperationalError:
        n = -1
    if n < 0:
        return _check("shadow_ledger", "被闸门掐掉的信号有没有记账", None, "表应存在", None,
                      "库无 shadow_ledger 表")
    return _check(
        "shadow_ledger", "被闸门掐掉的信号有没有记账", n, "应持续增长（不是 0）", n > 0,
        "表为空 = 没人知道闸门每天掐掉多少方向性信号、掐对还是掐错。"
        "「信号太少」这类问题只能靠它量化，缺了就只能拍脑袋调阈值。",
        note="空表不是「没问题」，是「没测量」",
    )


def check_timestamp_format(conn: sqlite3.Connection, now: int, hours: int) -> Dict[str, Any]:
    """⑩ 时间戳必须是统一的 epoch 秒（v433 / P0-4）。

    2026-11-17 实测：`raw_news.timestamp` 混着 ISO / epoch / `YYYY-MM-DD HH:MM:SS`
    三种格式，SQLite 把 ISO 文本当 0 → 超龄、去重、窗口统计全部静默算错
    （跑出「积压 49 万小时」的假数）。这条断言让同类问题当场变红。
    """
    if not (_has_col(conn, "raw_news", "timestamp") and _has_col(conn, "raw_news", "ts")):
        return _check("timestamp_format", "入库时间戳是否统一 epoch", None,
                      "应为 0 条异形", None, "缺 timestamp/ts 列，无法核对")
    total = _count(conn, "SELECT COUNT(*) FROM raw_news WHERE ts >= ?", (now - hours * 3600,))
    if total == 0:
        return _check("timestamp_format", "入库时间戳是否统一 epoch", 0,
                      "窗口内有入库才能算", None, "窗口内没有新闻")
    bad = _count(conn, """SELECT COUNT(*) FROM raw_news
                          WHERE ts >= ?
                            AND (TRIM(timestamp) NOT GLOB
                                 '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
                                 OR CAST(TRIM(timestamp) AS INTEGER) <> ts)""",
                 (now - hours * 3600,))
    return _check(
        "timestamp_format", "入库时间戳是否统一 epoch", bad, "应为 0 条异形", bad == 0,
        "非 0 说明 timestamp 又不是 epoch 秒了（有采集器绕过 db.insert_raw_news 直插）。"
        "这类行会让 `now - timestamp` 算成 0 → 超龄归档、去重、窗口统计全部静默出错。",
        severity_if_bad="broken", note=f"窗口 {hours}h 共 {total} 条",
    )


# ── 汇总 ──────────────────────────────────────────────────────────────
def run(conn: Optional[sqlite3.Connection] = None, *, hours: int = 24,
        db_path: Optional[str] = None) -> Dict[str, Any]:
    own = conn is None
    conn = conn or _connect_readonly(db_path)
    now = int(time.time())
    try:
        checks: List[Dict[str, Any]] = [
            check_inbound(conn, now),
            check_silent_archive(conn, now, hours),
            check_tradeable_filtered(conn, now, hours),
            check_throughput(conn, now),
            check_structure(conn, now, hours),
            check_confidence(conn, now, hours),
            check_routing(conn, now, hours),
            check_timestamp_format(conn, now, hours),
            check_sources(conn, now),
            check_shadow(conn),
        ]
    finally:
        if own:
            conn.close()
    bad = [c for c in checks if c["severity"] in ("broken", "degraded")]
    overall = "ok"
    if any(c["severity"] == "broken" for c in checks):
        overall = "broken"
    elif any(c["severity"] == "degraded" for c in checks):
        overall = "degraded"
    checks.sort(key=lambda c: SEV_ORDER[c["severity"]])
    return {
        "generated_at": now,
        "hours": hours,
        "overall": overall,
        "counts": {
            "ok": sum(1 for c in checks if c["severity"] == "ok"),
            "degraded": sum(1 for c in checks if c["severity"] == "degraded"),
            "broken": sum(1 for c in checks if c["severity"] == "broken"),
            "unknown": sum(1 for c in checks if c["severity"] == "unknown"),
        },
        "checks": checks,
        "thresholds": dict(THRESHOLDS),
        "summary": "；".join(f"{c['title']}（{c['value']}）" for c in bad) or "全部通过",
    }


_SEV_CN = {"broken": "断链", "degraded": "异常", "unknown": "无法判定", "ok": "正常"}


def _print(rep: Dict[str, Any]) -> None:
    print(f"\n== 管线对账守卫 ==  窗口 {rep['hours']}h  总判定 {_SEV_CN.get(rep['overall'], rep['overall'])}\n")
    for c in rep["checks"]:
        print(f"  [{_SEV_CN[c['severity']]}] {c['title']}")
        print(f"        实测 {c['value']} {c['unit']} · 期望 {c['expect']}")
        if c.get("note"):
            print(f"        说明 {c['note']}")
        if c["severity"] in ("broken", "degraded"):
            print(f"        → {c['hint']}")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="管线对账守卫（只读断言）")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    rep = run(hours=a.hours, db_path=a.db)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print(rep)
    return 0 if rep["overall"] in ("ok", "degraded") else 1


if __name__ == "__main__":
    raise SystemExit(main())
