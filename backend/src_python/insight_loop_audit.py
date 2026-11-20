# -*- coding: utf-8 -*-
"""v482: 洞察闭环一键体检 —— 一条命令看清"现在能不能测、卡在哪一环"。

为什么需要
----------
过去每次评估都要临时写 SQL 拼五六个口径，且容易漏（例如把 UTC 当 +08:00、
把越界算成失败）。更麻烦的是**数据进来之前无法判断功能是否合理** ——
只能看到"verified=0"，分不清是"算法没接上"还是"样本还没熟"。

本命令把整条链拆成 7 段，每段都给"当前值 / 缺口 / 下一动作"：
  1. 决策与分析        分母是否干净（降级拆分、无标的、HOLD）
  2. 洞察漏斗          生成 → 契约分级 → 有键 → 有方向 → 可注入 → 被采纳
  3. 逐条卡点          每条洞察具体缺什么（数值/方向/参数键/上下文）
  4. 验证进度          成熟决策数、最早可验证时刻、每个参数键还差几条
  5. 结算覆盖          多周期读数写没写、取价失败多少
  6. 反思闭环          结构化反思的状态与判定数
  7. 结论              三条可执行建议（按缺口大小排序）

用法
----
    python3 insight_loop_audit.py                 # 人读表格
    python3 insight_loop_audit.py --json          # 机读
    python3 insight_loop_audit.py --save          # 同时落 runtime/audit/insight_loop_*.json
    python3 insight_loop_audit.py --md out.md     # 生成 Markdown 报告

只读：全部走 `mode=ro` 连接，不写任何业务表。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    os.path.dirname(BASE), "trident_event_bus.db")

_KEY_IN_TEXT = re.compile(r"\[((?:sizing|score|filter|analysis)\.[a-z0-9_.]+)\]")

GATE_KEYS = ("score.", "filter.")      # 只有门槛类走决策级归因
NEED_PER_GROUP = 5                     # 放行组 / 拦截组各 ≥5
NEED_DECISIONS = NEED_PER_GROUP * 2
ATTR_HORIZON_H = int(os.environ.get("INSIGHT_ATTR_HORIZON_H", "24") or 24)


def _ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> Any:
    try:
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def _parse_ts(s: str) -> Optional[datetime]:
    """兼容三种写法：`2026-11-19T17:40:58.402+08:00` / `2026-11-19 17:40:58` / UTC 串。"""
    if not s:
        return None
    txt = str(s).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone(timedelta(hours=8)))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(txt[:26], fmt).replace(
                tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            continue
    return None


# ── 1. 决策与分析 ────────────────────────────────────────────────────────
def section_decisions(conn: sqlite3.Connection) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    out["total"] = _one(conn, "SELECT COUNT(*) FROM ai_decisions") or 0
    for k, expr in (
        ("degraded", "analysis_degraded=1"),
        ("hold", "suggested_action='HOLD'"),
        ("target_none", "analysis_degraded_reason='target_none'"),
        ("llm_empty", "analysis_degraded_reason='llm_empty'"),
        ("no_impact", "impact_score IS NULL"),
        ("mc_unknown",
         "market_confirmation IS NULL OR market_confirmation IN ('','unknown')"),
        ("no_hard_data", "hard_data_status IS NULL OR hard_data_status=''"),
        ("insight_hit", "COALESCE(insight_hit_ids,'') NOT IN ('','[]')"),
        ("no_fingerprint",
         "COALESCE(code_fingerprint,'')=''"),
    ):
        out[k] = _one(conn, f"SELECT COUNT(*) FROM ai_decisions WHERE {expr}") or 0
    out["min_ts"] = _one(conn, "SELECT MIN(created_at) FROM ai_decisions")
    out["max_ts"] = _one(conn, "SELECT MAX(created_at) FROM ai_decisions")
    dt = _parse_ts(out["min_ts"])
    out["oldest_age_h"] = round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 2) if dt else None
    out["mature_24h"] = _one(
        conn, "SELECT COUNT(*) FROM ai_decisions WHERE datetime(created_at) <= datetime('now', ?)",
        (f"-{ATTR_HORIZON_H} hours",)) or 0
    return out


# ── 2+3. 洞察漏斗与逐条卡点 ──────────────────────────────────────────────
def section_insights(conn: sqlite3.Connection) -> Dict[str, Any]:
    import insight_contract as ic

    rows = conn.execute(
        "SELECT id, rule, active, kind, origin, source_samples, verification_sample_n,"
        " verification_verdict, insight_param_key, insight_direction, observe_only,"
        " observe_reason, asset, created_at FROM review_insights").fetchall()
    levels = {"actionable": 0, "narrative": 0, "meta": 0, "reject": 0}
    items = []
    for r in rows:
        c = ic.check(r["rule"])
        levels[c["level"]] += 1
        items.append({
            "id": int(r["id"]), "rule": str(r["rule"] or "")[:120],
            "active": int(r["active"] or 0), "kind": int(r["kind"] or 0),
            "level": c["level"], "gaps": c["gaps"],
            "param_key": str(r["insight_param_key"] or "")
                          or (_KEY_IN_TEXT.search(str(r["rule"] or "")).group(1)
                              if _KEY_IN_TEXT.search(str(r["rule"] or "")) else ""),
            "direction": int(r["insight_direction"] or 0),
            "observe_only": int(r["observe_only"] or 0),
            "observe_reason": str(r["observe_reason"] or ""),
            "samples": max(int(r["source_samples"] or 0),
                           int(r["verification_sample_n"] or 0)),
            "origin": str(r["origin"] or "real"),
            "verdict": str(r["verification_verdict"] or ""),
            "asset": str(r["asset"] or ""),
        })
    active = [i for i in items if i["active"] == 1]
    hard = [i for i in active if i["kind"] == 0]
    soft = [i for i in active if i["kind"] == 1]
    # 人工种子(seeded)恒注入；自动沉淀需 已判定 或 证据量 >= REVIEW_INSIGHTS_MIN_SAMPLES(5)
    injectable = [i for i in soft if i["origin"] == "seeded"
                  or i["verdict"] in ("verified", "partial")
                  or i["samples"] >= 5]
    out = {
        "total": len(items), "active": len(active),
        "hard": len(hard), "soft": len(soft),
        "levels": levels,
        # 键/方向/观察/判定只对**软启发**有意义：硬铁律是强制约束，不参与可测性统计
        "with_key": sum(1 for i in soft if i["param_key"]),
        "with_dir": sum(1 for i in soft if i["direction"] != 0),
        "hard_inactive": sum(1 for i in items if i["kind"] == 0 and i["active"] == 0),
        "observe_only": sum(1 for i in soft if i["observe_only"] == 1),
        "verified": sum(1 for i in soft if i["verdict"] == "verified"),
        "failed": sum(1 for i in soft if i["verdict"] == "failed"),
        "injectable": len(injectable),
        "injectable_ids": [i["id"] for i in injectable],
        "items": items,
    }
    return out


def _stuck(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """每条只报"最靠前的一处卡点"，便于一眼看出该补什么。"""
    out = []
    for i in items:
        if i["kind"] == 0:            # 硬铁律是强制约束，不用"可测性"衡量
            continue
        if i["verdict"] in ("verified", "partial"):
            continue
        if not i["param_key"]:
            reason = "无参数键" + (f"（{i['observe_reason'][:40]}）" if i["observe_reason"] else "")
        elif i["direction"] == 0:
            reason = "有键无方向"
        elif i["level"] == "actionable" and i["samples"] < 5:
            reason = f"可执行但证据不足（n={i['samples']} < 5）"
        elif i["level"] in ("meta", "reject"):
            reason = f"契约分级 {i['level']}：{','.join(i['gaps']) or '—'}"
        else:
            reason = "可执行，等 24h 结算"
        out.append({"id": i["id"], "rule": i["rule"], "reason": reason,
                    "key": i["param_key"] or "—", "samples": i["samples"]})
    return out


# ── 4. 验证进度 ──────────────────────────────────────────────────────────
def section_verification(conn: sqlite3.Connection, ins: Dict[str, Any]) -> Dict[str, Any]:
    # 只统计门槛类（决策级归因唯一支持的类型）
    gate = [i for i in ins["items"] if i["active"] == 1 and i["param_key"].startswith(GATE_KEYS)
            and i["direction"] != 0]
    keys: Dict[str, Dict[str, Any]] = {}
    for i in gate:
        k = i["param_key"]
        keys.setdefault(k, {"key": k, "insights": 0, "decisions": 0})
        keys[k]["insights"] += 1
    # 决策总量按资产近似（真实分组要过门卫，这里只给可达样本上限）
    for k in keys:
        keys[k]["decisions"] = ins_decisions_cache["total"]
    mature = ins_decisions_cache["mature_24h"]
    per_key = []
    for k, v in sorted(keys.items(), key=lambda x: -x[1]["insights"]):
        per_key.append({
            "key": k, "insights": v["insights"],
            "need": NEED_DECISIONS, "have_mature": mature,
            "short": max(0, NEED_DECISIONS - mature),
        })
    earliest = None
    if ins_decisions_cache.get("min_ts"):
        dt = _parse_ts(ins_decisions_cache["min_ts"])
        if dt:
            earliest = (dt + timedelta(hours=ATTR_HORIZON_H)).strftime("%Y-%m-%d %H:%M")
    return {"gate_candidates": len(gate), "need_per_key": NEED_DECISIONS,
            "mature_decisions": mature, "earliest_verifiable": earliest,
            "keys": per_key}


# ── 5. 结算覆盖 ──────────────────────────────────────────────────────────
def section_settlement(conn: sqlite3.Connection) -> Dict[str, Any]:
    out = {
        "trade_decisions": _one(conn, "SELECT COUNT(*) FROM trade_decisions") or 0,
        "executed": _one(conn,
                         "SELECT COUNT(*) FROM trade_decisions WHERE final_action IN ('BUY','SELL')") or 0,
        "settled_main": _one(conn,
                             "SELECT COUNT(*) FROM trade_decisions WHERE actual_outcome_pct IS NOT NULL") or 0,
        "main_earliest": "",
        "horizon_rows": _one(conn, "SELECT COUNT(*) FROM decision_outcomes_horizon") or 0,
        "horizon_by_status": {},
        "horizon_by_h": {},
    }
    # 主口径要 5 天前向窗口才结算：把「还没到点」和「结算坏了」分开
    try:
        row = conn.execute(
            "SELECT MIN(decision_ts) FROM trade_decisions WHERE status='open'").fetchone()
        if row and row[0]:
            out["main_earliest"] = (datetime.fromtimestamp(float(row[0]))
                                    + timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    try:
        for r in conn.execute(
                "SELECT horizon, status, COUNT(*) c FROM decision_outcomes_horizon"
                " GROUP BY horizon, status"):
            out["horizon_by_status"][f"{r['horizon']}/{r['status']}"] = int(r["c"])
        for r in conn.execute(
                "SELECT horizon, COUNT(*) c FROM decision_outcomes_horizon"
                " WHERE status='settled' GROUP BY horizon"):
            out["horizon_by_h"][r["horizon"]] = int(r["c"])
    except sqlite3.OperationalError:
        pass
    # 行情覆盖：哪些标的**当下没有分钟线** —— 这直接决定 15m/1h 能不能结算
    out["price_gaps"] = []
    try:
        now = datetime.now().timestamp()
        rows = conn.execute(
            """SELECT td.asset AS a, MAX(oi.ts) AS last_ts
               FROM trade_decisions td
               LEFT JOIN ohlcv_intraday oi
                 ON oi.asset = td.asset AND oi.ts >= ?
               WHERE td.final_action IN ('BUY','SELL') AND td.decision_ts >= ?
               GROUP BY td.asset""",
            (int(now - 7 * 86400), int(now - 7 * 86400))).fetchall()
        for r in rows:
            asset = str(r["a"] or "")
            if not asset:
                continue
            last_ts = r["last_ts"]
            if last_ts is None:
                out["price_gaps"].append({"asset": asset, "last": "", "gap_h": None})
                continue
            gap_h = (now - float(last_ts)) / 3600.0
            if gap_h > 0.5:
                out["price_gaps"].append({
                    "asset": asset,
                    "last": datetime.fromtimestamp(float(last_ts)).strftime("%m-%d %H:%M"),
                    "gap_h": round(gap_h, 1)})
    except sqlite3.Error:
        pass
    out["horizon_reflection"] = {}
    try:
        for r in conn.execute(
                "SELECT verdict, COUNT(*) c FROM decision_horizon_reflection GROUP BY verdict"):
            out["horizon_reflection"][str(r["verdict"] or "none")] = int(r["c"])
    except sqlite3.OperationalError:
        pass
    return out


# ── 6. 反思闭环 ──────────────────────────────────────────────────────────
def section_reflections(conn: sqlite3.Connection) -> Dict[str, Any]:
    out = {
        "structured": _one(conn, "SELECT COUNT(*) FROM structured_reflections") or 0,
        "pending": _one(conn, "SELECT COUNT(*) FROM structured_reflections WHERE status='pending'") or 0,
        "promoted": _one(conn, "SELECT COUNT(*) FROM structured_reflections WHERE status='promoted'") or 0,
        "judged": _one(conn,
                       "SELECT COUNT(*) FROM structured_reflections WHERE verdict IN ('correct','wrong')") or 0,
        "trade_v2": _one(conn, "SELECT COUNT(*) FROM trade_reflections_v2") or 0,
        "errors": _one(conn, "SELECT COUNT(*) FROM decision_errors") or 0,
        "orphan_errors": _one(conn,
                              "SELECT COUNT(*) FROM decision_errors e LEFT JOIN ai_decisions d"
                              " ON d.id=e.decision_id LEFT JOIN trade_decisions t ON t.id=e.decision_id"
                              " WHERE d.id IS NULL AND t.id IS NULL") or 0,
    }
    return out


ins_decisions_cache: Dict[str, Any] = {}


def build() -> Dict[str, Any]:
    conn = _ro()
    try:
        dec = section_decisions(conn)
        ins_decisions_cache.update(dec)
        ins = section_insights(conn)
        ver = section_verification(conn, ins)
        stl = section_settlement(conn)
        ref = section_reflections(conn)
        stuck = _stuck(ins["items"])
    finally:
        conn.close()

    conclusions: List[str] = []
    if dec["mature_24h"] == 0:
        conclusions.append(
            f"验证窗口未开：最老决策只有 {dec['oldest_age_h']}h（门槛 {ATTR_HORIZON_H}h），"
            f"最早可验证 ≈ {ver['earliest_verifiable'] or '—'}；此刻 verified=0 是结构性为 0，"
            f"不代表算法有问题。")
    else:
        conclusions.append(
            f"已有 {dec['mature_24h']} 条成熟决策；每个门槛键需 {NEED_DECISIONS} 条才判得动"
            f"（放行/拦截各 {NEED_PER_GROUP}）。")
    if ins["observe_only"]:
        conclusions.append(
            f"{ins['observe_only']}/{ins['soft']} 条软启发只能观察"
            f"（无键 {sum(1 for i in ins['items'] if i['active'] and i['kind'] == 1 and not i['param_key'])} 条、"
            f"无方向 {sum(1 for i in ins['items'] if i['active'] and i['kind'] == 1 and i['direction'] == 0)} 条）——"
            f"这是注入区只有 {ins['injectable']} 条软启发的直接原因。")
    if stl["horizon_rows"] == 0:
        conclusions.append(
            "多周期读数为 0：15m/1h 这条「当天可读」的通道没跑，细节层只能等 24h。")
    else:
        conclusions.append(
            f"多周期读数 {stl['horizon_rows']} 行（settled "
            f"{sum(stl['horizon_by_h'].values())}），细节层已可读："
            f"{', '.join(f'{k}={v}' for k, v in stl['horizon_by_h'].items()) or '—'}。")
    if ins.get("hard_inactive"):
        conclusions.append(
            f"⚠️ {ins['hard_inactive']} 条硬铁律（kind=0）处于停用状态 —— 硬铁律本不应"
            f"参与自动停用，prompt 的「硬铁律」区会整段消失；先查 verifier_feedback 回写。")
    if ref["orphan_errors"]:
        conclusions.append(
            f"仍有 {ref['orphan_errors']} 条错误码指向已不存在的决策 —— 建议跑 "
            f"`python3 decision_orphans.py --apply`。")

    return {
        "generated_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "db": DB_PATH,
        "decisions": dec, "insights": ins, "verification": ver,
        "settlement": stl, "reflections": ref,
        "stuck": stuck, "conclusions": conclusions,
    }


def render_text(d: Dict[str, Any]) -> str:
    L: List[str] = []
    w = L.append
    dec, ins, ver, stl, ref = d["decisions"], d["insights"], d["verification"], d["settlement"], d["reflections"]
    w(f"洞察闭环体检 · {d['generated_at']} · {os.path.basename(d['db'])}")
    w("=" * 72)
    w(f"[1] 决策 {dec['total']} 条（最老 {dec['oldest_age_h']}h · 24h 成熟 {dec['mature_24h']}）")
    w(f"    降级 {dec['degraded']}（target_none {dec['target_none']} / llm_empty {dec['llm_empty']}）"
      f" · HOLD {dec['hold']} · impact空 {dec['no_impact']} · 硬数据空 {dec['no_hard_data']}")
    w(f"    经验被采纳 {dec['insight_hit']} 条 · 无指纹 {dec['no_fingerprint']}")
    w(f"[2] 洞察 {ins['total']} 条（active {ins['active']}：硬铁律 {ins['hard']} / 软启发 {ins['soft']}）")
    w(f"    契约分级 {ins['levels']} · 有键 {ins['with_key']} · 有方向 {ins['with_dir']}"
      f" · 只能观察 {ins['observe_only']} · 可注入 {ins['injectable']}")
    w(f"[3] 卡点（每条只报第一处）")
    for s in d["stuck"][:15]:
        w(f"    #{s['id']:<5} {s['reason']:<32} n={s['samples']:<3} {s['rule'][:46]}")
    w(f"[4] 验证：门槛类候选 {ver['gate_candidates']} 条 · 每键需 {ver['need_per_key']} 条"
      f" · 成熟 {ver['mature_decisions']} 条 · 最早 {ver['earliest_verifiable'] or '—'}")
    for k in ver["keys"][:10]:
        w(f"    {k['key']:<34} 洞察 {k['insights']:>2} · 还差 {k['short']:>3} 条")
    w(f"[5] 结算：trade {stl['trade_decisions']} / 执行 {stl['executed']} / 主口径已结 {stl['settled_main']}"
      + (f"（最早 {stl['main_earliest']} 才够 5 日前向窗口）" if stl.get("main_earliest") else ""))
    w(f"    多周期 {stl['horizon_rows']} 行 · " +
      (", ".join(f"{k}={v}" for k, v in stl["horizon_by_status"].items()) or "无"))
    if stl.get("price_gaps"):
        gaps = "、".join(f"{g['asset']}({g['last'] or '无分钟线'})"
                       for g in stl["price_gaps"][:6])
        w(f"    ⚠ 缺近 30 分钟行情：{gaps} —— 这些标的的 15m/1h 会落 no_price")
    w(f"    多周期反思 " + (json.dumps(stl["horizon_reflection"], ensure_ascii=False) or "{}"))
    w(f"[6] 反思：结构化 {ref['structured']}（pending {ref['pending']} / promoted {ref['promoted']}"
      f" / 判定 {ref['judged']}）· 交易反思 {ref['trade_v2']} · 错误码 {ref['errors']}"
      f"（孤儿 {ref['orphan_errors']}）")
    w("[7] 结论")
    for c in d["conclusions"]:
        w(f"    · {c}")
    return "\n".join(L)


def render_md(d: Dict[str, Any]) -> str:
    dec, ins, ver, stl, ref = d["decisions"], d["insights"], d["verification"], d["settlement"], d["reflections"]
    L = [f"# 洞察闭环体检 · {d['generated_at']}", "",
         "| 段 | 读数 |", "|---|---|",
         f"| 决策 | {dec['total']} 条（24h 成熟 {dec['mature_24h']}，最老 {dec['oldest_age_h']}h） |",
         f"| 降级拆分 | 总 {dec['degraded']}：target_none {dec['target_none']} / llm_empty {dec['llm_empty']} |",
         f"| 洞察 | {ins['total']}（active {ins['active']} · 硬 {ins['hard']} / 软 {ins['soft']}） |",
         f"| 契约分级 | actionable {ins['levels']['actionable']} / narrative {ins['levels']['narrative']}"
         f" / meta {ins['levels']['meta']} / reject {ins['levels']['reject']} |",
         f"| 注入 | 可注入软启发 {ins['injectable']} 条 · 被采纳决策 {dec['insight_hit']} |",
         f"| 验证 | 候选 {ver['gate_candidates']} · 成熟决策 {ver['mature_decisions']}"
         f" · 最早 {ver['earliest_verifiable'] or '—'} |",
         f"| 多周期 | {stl['horizon_rows']} 行 |",
         f"| 反思 | 结构化 {ref['structured']}（pending {ref['pending']} / 判定 {ref['judged']}）"
         f" · 孤儿错误码 {ref['orphan_errors']} |", "",
         "## 结论", ""]
    L += [f"- {c}" for c in d["conclusions"]]
    L += ["", "## 逐条卡点", "", "| id | 卡点 | 证据量 | 洞察 |", "|---|---|---|---|"]
    for s in d["stuck"]:
        L.append(f"| {s['id']} | {s['reason']} | {s['samples']} | {s['rule'][:60]} |")
    L += ["", "## 参数键进度", "", "| 参数键 | 洞察 | 已有成熟 | 还差 |", "|---|---|---|---|"]
    for k in ver["keys"]:
        L.append(f"| {k['key']} | {k['insights']} | {k['have_mature']} | {k['short']} |")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="洞察闭环一键体检（只读）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--save", action="store_true", help="落盘 runtime/audit/insight_loop_*.json")
    p.add_argument("--md", default="", help="同时生成 Markdown 到指定路径")
    args = p.parse_args(argv)

    d = build()
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(render_text(d))
    if args.save:
        out_dir = os.path.join(os.path.dirname(BASE), "runtime", "audit")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"insight_loop_{time.strftime('%Y%m%d_%H%M%S')}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
        print(f"\n[json] {path}")
    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(render_md(d))
        print(f"[md] {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
