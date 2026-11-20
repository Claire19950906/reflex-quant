#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v404 · 错误码有效性台账（error_code_ledger）

问题
----
README 说"33 个错误码 / 7 层反思"，但**没有任何一个码回答过**：
加了它之后，命中率 / 回撤 / 错单率改善了吗？
`decision_errors` 30 天只有 51 条，E003/E014–E017/E021–E027 全为 0。
码的数量是叙事资产，不是运行资产 —— 台账就是把它变成后者的最小工具。

分层（tier）
------------
· hard     = 客观可机器验证，理应进闸门 / 能单独否决一笔决策
             （缺数据、数据过期、标的不匹配、规则违反、方向与因子排序矛盾、
              跨资产方向错、置信度无证据支撑）
· soft     = 文本表述启发式（关键词/结构检查），只提示、不改判断
             （E021/E022 这类"有没有写反向预案"的模板合规检查最典型）
· research = 需要统计验证才能确认有效性，当前**不得**参与胜率/闸门统计
             （因果解释是否成立、叙事是否过度、置信度是否校准…）

用法
----
    python3 error_code_ledger_build.py            # 重建台账并打印
    python3 error_code_ledger_build.py --dry-run  # 只打印，不落库
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

import db

# code → (tier, layer, note)
REGISTRY: Dict[str, Tuple[str, str, str]] = {
    "E001":  ("hard", "data", "缺硬证据（总码）"),
    "E001a": ("hard", "data", "缺品种事实 hard_data_evidence"),
    "E001b": ("hard", "data", "缺/坏时间锚点"),
    "E001c": ("hard", "data", "有 action 但无因子方向"),
    "E001d": ("hard", "data", "缺事件链关联"),
    "E001e": ("hard", "data", "缺跨资产验证"),
    "E001f": ("hard", "data", "回测样本不足（与 E006 重叠，保留 code 以便追溯）"),
    # v409: E001a 按原因拆细，避免 85% 的错误码挤在一个"缺硬数据"里
    "E001g": ("hard", "data", "该品种本地无行情源（结构性，改代码解决不了）"),
    "E001h": ("hard", "data", "本地有源但断档/过期（可修）"),
    "E001i": ("hard", "data", "符号无法识别或未配映射（可修）"),
    "E001j": ("hard", "data", "休市时段无新价（市场事实，非管道故障）"),
    "E002":  ("hard", "data", "数据过期（决策所用报价早于窗口）"),
    "E003":  ("hard", "logic", "方向与因子排序矛盾"),
    "E004":  ("research", "causal", "因果图谱不匹配 — 图谱本身覆盖率低，误报率高"),
    "E005":  ("hard", "rule", "违反 review_insights 现行规则"),
    "E006":  ("research", "sample", "样本不足 — 属统计前提，不是决策错误"),
    "E007":  ("hard", "market", "事件过旧"),
    "E008":  ("hard", "asset", "标的不匹配"),
    "E009":  ("research", "calibration", "置信度虚高 — 需与结算结果对照才成立"),
    "E010":  ("research", "narrative", "无反预案"),
    "E011":  ("research", "narrative", "事实陈述错误 — 需人工/外部核验"),
    "E012":  ("research", "narrative", "推理链过长"),
    "E013":  ("research", "narrative", "叙事偏差"),
    "E014":  ("research", "event_logic", "因果链错位"),
    "E015":  ("research", "event_logic", "概念错位/反共识缺位"),
    "E016":  ("hard", "event_logic", "跨资产传导方向错（有行情即可验证）"),
    "E017":  ("hard", "event_logic", "跨资产互证缺位"),
    "E018":  ("research", "event_logic", "历史同类新闻走势相反"),
    "E019":  ("research", "reasoning_logic", "（待定语义）"),
    "E020":  ("research", "reasoning_logic", "（待定语义）"),
    # E021/E022 是关键词存在性检查 —— 测的是"模板写没写"，不是"判断对不对"
    "E021":  ("soft", "reasoning_logic", "模板合规：未明示假设/前提"),
    "E022":  ("soft", "reasoning_logic", "模板合规：未写反向预案"),
    "E023":  ("research", "reasoning_logic", "置信度未校准"),
    "E024":  ("research", "reasoning_logic", "历史形态误读"),
    "E025":  ("research", "reasoning_logic", "跨资产误读"),
    "E026":  ("research", "reasoning_logic", "缺因果机制"),
    "E027":  ("hard", "reasoning_logic", "置信度与证据不匹配（可机械核对）"),
    "E028":  ("soft", "logic_analysis", "自相矛盾（方向词同现）"),
    "E029":  ("soft", "logic_analysis", "推不出结论（结构启发式）"),
    "E030":  ("soft", "logic_analysis", "假二择一"),
    "E031":  ("soft", "logic_analysis", "循环论证（Jaccard 相似）"),
    "E032":  ("soft", "logic_analysis", "术语歧义"),
    "E033":  ("soft", "logic_analysis", "后此谬因"),
}


def _connect(retries: int = 5) -> sqlite3.Connection:
    path = db.get_db_path()
    last: Optional[Exception] = None
    for _ in range(retries):
        conn = sqlite3.connect(path, timeout=90.0)
        try:
            conn.execute("PRAGMA busy_timeout=90000")
            conn.execute("SELECT 1 FROM decision_errors LIMIT 1").fetchone()
            return conn
        except sqlite3.OperationalError as exc:
            last = exc
            conn.close()
            time.sleep(5)
    raise last if last else RuntimeError("connect failed")


def collect(conn: sqlite3.Connection) -> List[Dict[str, object]]:
    rows = conn.execute(
        """
        SELECT de.error_code                     AS code,
               COUNT(*)                          AS triggers_total,
               SUM(CASE WHEN de.created_at >= datetime('now','-30 days')
                        THEN 1 ELSE 0 END)       AS triggers_30d,
               MIN(de.created_at)                AS first_seen,
               MAX(de.created_at)                AS last_seen,
               -- 触发后该笔已结算且亏 → 说明这条码指对了
               SUM(CASE WHEN q.settled = 1 AND q.pnl_pct < 0 THEN 1 ELSE 0 END) AS confirmed_bad,
               SUM(CASE WHEN q.settled = 1 AND q.pnl_pct > 0 THEN 1 ELSE 0 END) AS rejected_good,
               AVG(CASE WHEN q.settled = 1 THEN q.pnl_pct END)                  AS pnl_triggered
          FROM decision_errors de
          LEFT JOIN quick_sim_trades q ON q.decision_id = de.decision_id
         GROUP BY de.error_code
        """
    ).fetchall()

    # 未触发的决策（对照组）平均收益：让"触发组 vs 未触发组"可比
    base_avg = conn.execute(
        "SELECT AVG(pnl_pct) FROM quick_sim_trades WHERE settled = 1"
    ).fetchone()[0]

    out: List[Dict[str, object]] = []
    for r in rows:
        code = r[0]
        tier, layer, note = REGISTRY.get(code, ("research", "", "未在注册表中登记，按假设处理"))
        trig = r[7]
        delta = None
        if trig is not None and base_avg is not None:
            delta = round(float(trig) - float(base_avg), 4)
        out.append({
            "error_code": code, "tier": tier, "layer": layer,
            "triggers_total": int(r[1] or 0), "triggers_30d": int(r[2] or 0),
            "first_seen_at": r[3] or "", "last_seen_at": r[4] or "",
            "confirmed_bad": int(r[5] or 0), "rejected_good": int(r[6] or 0),
            "pnl_delta_pct": delta,
            "note": note,
            "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    # 注册表里有、但库里一次都没触发的码也要落台账（0 次 ≠ 无效）
    seen = {x["error_code"] for x in out}
    for code, (tier, layer, note) in REGISTRY.items():
        if code in seen:
            continue
        out.append({
            "error_code": code, "tier": tier, "layer": layer,
            "triggers_total": 0, "triggers_30d": 0,
            "first_seen_at": "", "last_seen_at": "",
            "confirmed_bad": 0, "rejected_good": 0, "pnl_delta_pct": None,
            "note": note + "（当前无触发样本：无样本 ≠ 无效，需先有样本再评估）",
            "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    return out


UPSERT = """
    INSERT INTO error_code_ledger
      (error_code, tier, layer, first_seen_at, last_seen_at,
       triggers_total, triggers_30d, confirmed_bad, rejected_good,
       pnl_delta_pct, last_evaluated_at, note)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(error_code) DO UPDATE SET
       tier=excluded.tier, layer=excluded.layer,
       first_seen_at=excluded.first_seen_at, last_seen_at=excluded.last_seen_at,
       triggers_total=excluded.triggers_total, triggers_30d=excluded.triggers_30d,
       confirmed_bad=excluded.confirmed_bad, rejected_good=excluded.rejected_good,
       pnl_delta_pct=excluded.pnl_delta_pct,
       last_evaluated_at=excluded.last_evaluated_at, note=excluded.note
"""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="重建错误码台账")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        items = collect(conn)
        for it in sorted(items, key=lambda x: (-int(x["triggers_total"]), x["error_code"])):
            print(f"{it['error_code']:6s} tier={it['tier']:8s} layer={it['layer']:14s} "
                  f"total={it['triggers_total']:3d} 30d={it['triggers_30d']:3d} "
                  f"bad={it['confirmed_bad']:2d} good={it['rejected_good']:2d} "
                  f"delta={it['pnl_delta_pct']}")
        if args.dry_run:
            print("[ledger] dry-run，未写入")
            return 0
        with conn:
            for it in items:
                conn.execute(UPSERT, (
                    it["error_code"], it["tier"], it["layer"],
                    it["first_seen_at"], it["last_seen_at"],
                    it["triggers_total"], it["triggers_30d"],
                    it["confirmed_bad"], it["rejected_good"],
                    it["pnl_delta_pct"], it["evaluated_at"], it["note"],
                ))
        n = conn.execute("SELECT COUNT(*) FROM error_code_ledger").fetchone()[0]
        print(f"[ledger] 已写入 {n} 行")
        for tier in ("hard", "soft", "research"):
            c = conn.execute(
                "SELECT COUNT(*) FROM error_code_ledger WHERE tier=?", (tier,)
            ).fetchone()[0]
            print(f"  {tier:8s} {c} 个码")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
