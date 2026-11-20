#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reflection_scorecard.py — 反思机制成绩单（只读 + 可落盘）

为什么需要
----------
反思相关的数字散在 8 张表、5 个脚本里：看板只报"反思条数"，而这个数 90% 是 HOLD 模板。
于是"机制在进步"与"样本在积累"两件事长期分不清。本脚本用一个命令回答五个问题：

  1. 反思语料里，有多少是**真交易反思**（origin=llm/paper），多少是模板记账？
  2. 有多少条**判得出对错**（reflection_effective 1/0/NULL），一致率多少？
  3. 影子层（4h 真值）**今天/累计**积累了多少，方向命中率多少，按品种/方向/置信度怎么分？
  4. 元反思和策略补丁**最近一次真的有产出**是什么时候？
  5. 调度任务**最后一次成功**是什么时候（reflection_3layer / 影子层水位）？

刻意不做的事
------------
- 不把 NULL 当 0（判不了 ≠ 判错）；
- 不给 n<60 的命中率下结论，只打 `样本不足` 标记；
- 不写业务表，只读 + 落一份 Markdown/JSON 到 docs/reports/。

用法
    python3 reflection_scorecard.py            # 打印 + 落盘
    python3 reflection_scorecard.py --json     # 只打印 JSON
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db

MIN_CONCLUSIVE_N = 60


def _one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> Any:
    row = conn.execute(sql, args).fetchone()
    return row[0] if row else None


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> List[tuple]:
    return conn.execute(sql, args).fetchall()


def collect(conn: sqlite3.Connection) -> Dict[str, Any]:
    out: Dict[str, Any] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # ── 1. 语料构成 ────────────────────────────────────────────────
    origin_rows = {str(r[0] or ""): int(r[1]) for r in _rows(
        conn, "SELECT COALESCE(origin,''), COUNT(*) FROM trade_reflections_v2 GROUP BY 1")}
    total = sum(origin_rows.values())
    template_n = origin_rows.get("template", 0)
    real_total = total - template_n
    out["corpus"] = {
        "total": total,
        "real": real_total,
        "template": template_n,
        "llm": origin_rows.get("llm", 0),
        "paper": origin_rows.get("paper", 0),
        "real_ratio": round(real_total / total, 4) if total else None,
        # v482：模板行已迁到 missed_signal_reflections，表里的历史行不再参与统计
        "template_note": "v482 起 HOLD 漏检写入 missed_signal_reflections，不再进本表",
        "settled": int(_one(
            conn, "SELECT COUNT(*) FROM trade_reflections_v2"
                  " WHERE settled=1 AND COALESCE(origin,'') <> 'template'") or 0),
    }
    # v482: 赢单归因是否真的在跑 + 教训是否带代次(旧代不再学)
    try:
        out["corpus"]["win"] = int(_one(
            conn, "SELECT COUNT(*) FROM trade_reflections_v2"
                  " WHERE settled=1 AND pnl_pct >= 0.005"
                  "   AND COALESCE(origin,'') <> 'template'") or 0)
        out["corpus"]["loss"] = int(_one(
            conn, "SELECT COUNT(*) FROM trade_reflections_v2"
                  " WHERE settled=1 AND pnl_pct < -0.005"
                  "   AND COALESCE(origin,'') <> 'template'") or 0)
    except Exception:
        out["corpus"]["win"] = None
        out["corpus"]["loss"] = None
    try:
        cur_fp = ""
        try:
            from code_fingerprint import fingerprint as _fp
            cur_fp = str(_fp() or "")
        except Exception:
            cur_fp = ""
        cols = {r[1] for r in _rows(conn, "PRAGMA table_info(trade_reflections_v2)")}
        if "code_fingerprint" in cols:
            out["corpus"]["fp_current"] = cur_fp
            out["corpus"]["fp_tagged"] = int(_one(
                conn, "SELECT COUNT(*) FROM trade_reflections_v2"
                      " WHERE COALESCE(code_fingerprint,'') <> ''") or 0)
            out["corpus"]["fp_other_gen"] = int(_one(
                conn, "SELECT COUNT(*) FROM trade_reflections_v2"
                      " WHERE COALESCE(code_fingerprint,'') <> ''"
                      "   AND code_fingerprint <> ?", (cur_fp,)) or 0)
        else:
            out["corpus"]["fp_tagged"] = None
            out["corpus"]["fp_other_gen"] = None
    except Exception:
        out["corpus"]["fp_tagged"] = None
        out["corpus"]["fp_other_gen"] = None

    # ── 2. 可判定率 / 一致率（v482：只统计非模板行）─────────────────
    eff = {"1": 0, "0": 0, "NULL": 0}
    for v, n in _rows(
            conn,
            "SELECT COALESCE(CAST(reflection_effective AS TEXT),'NULL'), COUNT(*)"
            "  FROM trade_reflections_v2"
            " WHERE COALESCE(origin,'') <> 'template' GROUP BY 1"):
        eff[str(v)] = int(n)
    judged = eff["1"] + eff["0"]
    out["judged"] = {
        "judged": judged,
        "verified": eff["1"],
        "falsified": eff["0"],
        "unknown": eff["NULL"],
        "template_excluded": template_n,
        "judge_rate": round(judged / real_total, 4) if real_total else None,
        "consistency": round(eff["1"] / judged, 4) if judged else None,
    }

    # ── 3. 覆盖率 ────────────────────────────────────────────────
    decisions = int(_one(conn, "SELECT COUNT(*) FROM ai_decisions") or 0)
    covered = int(_one(
        conn,
        "SELECT COUNT(DISTINCT r.decision_id) FROM trade_reflections_v2 r"
        "  JOIN ai_decisions d ON d.id=r.decision_id"
        " WHERE COALESCE(r.origin,'') <> 'template'") or 0)
    out["coverage"] = {
        "decisions": decisions,
        "with_reflection": covered,
        "rate": round(covered / decisions, 4) if decisions else None,
        "real_trade_reflections": origin_rows.get("llm", 0) + origin_rows.get("paper", 0),
    }

    # ── 4. 影子层（4h 真值）──────────────────────────────────────
    shadow_rows = int(_one(conn, "SELECT COUNT(*) FROM shadow_reflections") or 0)
    by_verdict = {str(r[0]): int(r[1]) for r in _rows(
        conn, "SELECT verdict, COUNT(*) FROM shadow_reflections GROUP BY 1")}
    ok = by_verdict.get("direction_ok", 0)
    wrong = by_verdict.get("direction_wrong", 0)
    settled_s = ok + wrong
    out["shadow"] = {
        "rows": shadow_rows,
        "ok": ok,
        "wrong": wrong,
        "flat": by_verdict.get("flat", 0),
        "insufficient": by_verdict.get("insufficient", 0),
        "hit_rate": round(ok / settled_s, 4) if settled_s else None,
        "hit_rate_note": ("样本不足(<%d)，不构成结论" % MIN_CONCLUSIVE_N)
        if settled_s < MIN_CONCLUSIVE_N else "样本达标",
        "rows_24h": int(_one(
            conn,
            "SELECT COUNT(*) FROM shadow_reflections"
            " WHERE created_at >= datetime('now','-1 day')") or 0),
    }
    out["shadow"]["by_asset"] = [
        {"asset": str(r[0]), "n": int(r[1]), "ok": int(r[2] or 0),
         "hit_rate": round(int(r[2] or 0) / int(r[1]), 4) if int(r[1]) else None}
        for r in _rows(
            conn,
            "SELECT asset, COUNT(*), SUM(reflection_effective)"
            "  FROM shadow_reflections WHERE verdict IN ('direction_ok','direction_wrong')"
            " GROUP BY asset ORDER BY 2 DESC") if int(r[1] or 0) >= 5
    ]
    out["shadow"]["by_direction"] = [
        {"direction": str(r[0]), "n": int(r[1]), "ok": int(r[2] or 0)}
        for r in _rows(
            conn,
            "SELECT claimed_direction, COUNT(*), SUM(reflection_effective)"
            "  FROM shadow_reflections WHERE verdict IN ('direction_ok','direction_wrong')"
            " GROUP BY claimed_direction ORDER BY 2 DESC")
    ]

    # 影子账本（闸门前方向 + 4h 真值）的边际
    ledger_n = int(_one(conn, "SELECT COUNT(*) FROM shadow_ledger WHERE settled_at IS NOT NULL") or 0)
    ledger_hit = int(_one(conn, "SELECT COALESCE(SUM(hit),0) FROM shadow_ledger WHERE settled_at IS NOT NULL") or 0)
    out["shadow"]["ledger"] = {
        "settled": ledger_n, "hit": ledger_hit,
        "hit_rate": round(ledger_hit / ledger_n, 4) if ledger_n else None,
        "avg_move_pct": _one(conn, "SELECT ROUND(AVG(move_pct),3) FROM shadow_ledger WHERE settled_at IS NOT NULL"),
    }

    # ── v490: 命中率单独报一定被误读（单边行情里"无脑做空"也有 60%+）──
    # 这里把同批样本的"市场基线 + 平衡准确率 + 扣费净期望"一起挂上，
    # 让每日成绩单一句话就能看出"这是能力还是 beta"。
    try:
        import judgment_benchmark as _jb
        samples = _jb.load_shadow_ledger(conn)
        bench_src = "shadow_ledger"
        if not samples:
            samples = _jb.load_shadow_reflections(conn)
            bench_src = "shadow_reflections"
        bench = _jb.benchmark(samples)
        bench["source"] = bench_src if samples else "none"
        if bench["n_settled"] == 0:
            bench["throughput"] = _jb.throughput(conn)
        out["shadow"]["bench"] = bench
    except Exception:
        out["shadow"]["bench"] = None
    # 置信度校准
    out["shadow"]["by_confidence"] = [
        {"bucket": str(r[0]), "n": int(r[1]), "hit": int(r[2] or 0)}
        for r in _rows(
            conn,
            "SELECT CASE WHEN confidence>=0.8 THEN '>=0.8' WHEN confidence>=0.5 THEN '0.5-0.8' ELSE '<0.5' END,"
            "       COUNT(*), SUM(hit)"
            "  FROM shadow_ledger WHERE settled_at IS NOT NULL GROUP BY 1")
    ]

    # 24h 二次判决：4h 窗口回答"即时冲击"，24h 才回答"新闻是否真的走出来"
    try:
        h24 = {str(r[0]): int(r[1]) for r in _rows(
            conn,
            "SELECT COALESCE(NULLIF(verdict_24h,''),'pending'), COUNT(*)"
            "  FROM shadow_reflections GROUP BY 1")}
        settled24 = h24.get("direction_ok", 0) + h24.get("direction_wrong", 0)
        flipped = int(_one(
            conn,
            "SELECT COUNT(*) FROM shadow_reflections"
            " WHERE verdict='direction_wrong' AND verdict_24h='direction_ok'") or 0)
        out["shadow"]["h24"] = {
            "ok": h24.get("direction_ok", 0),
            "wrong": h24.get("direction_wrong", 0),
            "flat": h24.get("flat", 0),
            "insufficient": h24.get("insufficient", 0),
            "pending": h24.get("pending", 0),
            "settled": settled24,
            "hit_rate": round(h24.get("direction_ok", 0) / settled24, 4) if settled24 else None,
            "flipped_from_4h_wrong": flipped,
            "note": "4h 判错但 24h 判对 = 窗口太短，不是判断错",
        }
    except sqlite3.Error:
        out["shadow"]["h24"] = None

    # ── 4b. 反思 × 4h/24h 真值（v482）─────────────────────────────
    # 反思自己"对不对"必须看 4h/24h，不能拿 20~40 分钟纸上单的 pnl 代替。
    # 这一节回答：真反思里有多少已盖章、4h 方向对不对、平均净收益多少。
    try:
        _trv2_cols = {r[1] for r in _rows(conn, "PRAGMA table_info(trade_reflections_v2)")}
        if "truth_4h_verdict" not in _trv2_cols:
            # 老库未跑 v482 迁移：如实标注，不伪装成"0 条已盖章"
            out["horizon_truth"] = {"pending_migration": True}
            raise sqlite3.OperationalError("truth columns not migrated")
        t4 = {str(r[0] or ""): int(r[1]) for r in _rows(
            conn,
            "SELECT truth_4h_verdict, COUNT(*) FROM trade_reflections_v2"
            " WHERE COALESCE(origin,'') <> 'template' GROUP BY 1")}
        t24 = {str(r[0] or ""): int(r[1]) for r in _rows(
            conn,
            "SELECT truth_24h_verdict, COUNT(*) FROM trade_reflections_v2"
            " WHERE COALESCE(origin,'') <> 'template' GROUP BY 1")}
        ok4 = t4.get("direction_ok", 0)
        wrong4 = t4.get("direction_wrong", 0)
        judged4 = ok4 + wrong4
        out["horizon_truth"] = {
            "real_reflections": real_total,
            "stamped_4h": judged4,
            "ok_4h": ok4,
            "wrong_4h": wrong4,
            "flat_4h": t4.get("flat", 0),
            "insufficient_4h": t4.get("insufficient", 0),
            "unjudged_4h": t4.get("", 0),
            "judge_rate_4h": round(judged4 / real_total, 4) if real_total else None,
            "hit_rate_4h": round(ok4 / judged4, 4) if judged4 else None,
            "avg_net_pct_4h": _one(
                conn, "SELECT ROUND(AVG(truth_4h_net_pct),4) FROM trade_reflections_v2"
                      " WHERE COALESCE(origin,'') <> 'template' AND truth_4h_net_pct IS NOT NULL"),
            "stamped_24h": t24.get("direction_ok", 0) + t24.get("direction_wrong", 0),
            "hit_rate_24h": (
                round(t24.get("direction_ok", 0)
                      / (t24.get("direction_ok", 0) + t24.get("direction_wrong", 0)), 4)
                if (t24.get("direction_ok", 0) + t24.get("direction_wrong", 0)) else None),
            "basis": "shadow_reflections 4h/24h（扣往返成本后的净收益）",
            "note": ("样本不足(<%d)，不构成结论" % MIN_CONCLUSIVE_N)
            if judged4 < MIN_CONCLUSIVE_N else "样本达标",
        }
    except sqlite3.Error:
        if out.get("horizon_truth") is None:
            out["horizon_truth"] = None

    # ── 5. 元反思 / 补丁 ─────────────────────────────────────────
    cycles = int(_one(conn, "SELECT COUNT(*) FROM reflection_cycles") or 0)
    last_cycle = _one(conn, "SELECT MAX(created_at) FROM reflection_cycles")
    kinds = {str(r[0]): int(r[1]) for r in _rows(
        conn, "SELECT finding_kind, COUNT(*) FROM reflection_cycles GROUP BY 1")}
    out["meta"] = {"cycles": cycles, "last": last_cycle, "kinds": kinds}
    out["patches"] = {
        "pending": int(_one(conn, "SELECT COUNT(*) FROM policy_patch_queue WHERE status='pending'") or 0),
        "rejected": int(_one(conn, "SELECT COUNT(*) FROM policy_patch_queue WHERE status='rejected'") or 0),
        "applied": int(_one(conn, "SELECT COUNT(*) FROM policy_patch_queue WHERE status='applied'") or 0),
    }

    # ── 6. 调度最后成功时间 ──────────────────────────────────────
    out["scheduler"] = {
        "verifier_last_evaluated": _one(conn, "SELECT MAX(evaluated_at) FROM reflection_3layer")
        if _table_exists(conn, "reflection_3layer") else None,
        "shadow_watermark": _one(
            conn, "SELECT watermark_ts FROM shadow_reflection_state WHERE name='shadow_reflections_watermark'"),
        "shadow_floor": _one(
            conn, "SELECT watermark_ts FROM shadow_reflection_state WHERE name='shadow_reflections_floor'"),
        "enrich_last_ok": _one(
            conn, "SELECT MAX(enriched_at) FROM trade_reflections_v2 WHERE enrich_status='ok'"),
    }

    # ── 7. 注入面 ────────────────────────────────────────────────
    out["injection"] = {
        "rules_total": int(_one(conn, "SELECT COUNT(*) FROM review_insights") or 0),
        "observable_only": int(_one(conn, "SELECT COUNT(*) FROM review_insights WHERE observe_only=1") or 0),
        "injectable": int(_one(
            conn, "SELECT COUNT(*) FROM review_insights WHERE active=1 AND observe_only=0") or 0),
        "experience_applications": int(_one(conn, "SELECT COUNT(*) FROM experience_applications") or 0),
        "decisions_with_hit_ids": int(_one(
            conn, "SELECT COUNT(*) FROM ai_decisions WHERE COALESCE(insight_hit_ids,'')<>''") or 0),
    }
    # 命中来源：model=模型真声明，auto=关键词兜底（假回流），''=历史未知
    try:
        src = {str(r[0]): int(r[1]) for r in _rows(
            conn,
            "SELECT COALESCE(NULLIF(insight_hit_source,''), 'unknown'), COUNT(*)"
            "  FROM ai_decisions WHERE COALESCE(insight_hit_ids,'')<>'' GROUP BY 1")}
    except sqlite3.Error:
        src = {}
    out["injection"]["hit_sources"] = src

    # ── 8. 回流 A/B ─────────────────────────────────────────────
    try:
        from reflection_ab import evaluate as _ab_eval
        out["ab"] = _ab_eval(14)
    except Exception as exc:  # A/B 失败不能拖垮成绩单
        out["ab"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _bench_lines(sh: Dict[str, Any]) -> List[str]:
    """v490: 把"命中率"升级成"命中率 + 市场基线 + 平衡准确率 + 净期望"。

    为什么必须一起报：2026-11-04→11-19 单边下跌里，影子层 4h 命中 56.7%，
    而同一批样本"闭眼喊 SELL"的基线是 62.9% —— 只报命中率，读起来像"及格"，
    实际是**跑输一个没有判断力的策略**。这一行就是防止这种误读。
    """
    b = sh.get("bench")
    if not b:
        return []
    v = b.get("verdict") or {}
    one = lambda x, u="%": "—" if x is None else f"{x:.1f}{u}"

    if v.get("code") in ("empty", None):
        out = [f"- **去偏置口径**：{v.get('headline', '样本不足')}"]
        tp = b.get("throughput") or {}
        if tp.get("settleable_per_hour") is not None:
            eta = tp.get("eta_days")
            eta_txt = "永远到不了（池内方向决策为 0）" if eta is None else f"{eta:.0f} 天"
            out.append(f"- 产能：近 24h 决策 {tp['decisions']} 条，池内可结算 {tp.get('settleable', 0)} 条"
                       f"（{one(tp.get('settleable_per_hour'), '/小时')}），"
                       f"距 n≥{b.get('gate')} 约 {eta_txt}")
        out.append("- 读数：0 样本 **不等于能力为 0**；先看产能，再谈准不准。")
        return out

    base, exp_ = b["baseline"], b["expectancy"]
    ci = b["ba_label"]["ci"]
    return [
        f"- 同批市场基线：无脑做空 {one(base['always_sell'])} · 同配比盲选 {one(base['mix_implied'])}"
        f" · 裸命中 − 无脑做空 {one(b['excess_vs_always_sell'], 'pt')}",
        f"- 去偏置判别力：**BA {one(b['ba_label']['value'])}**"
        f"（区间 {one(ci[0])}~{one(ci[1])}）· 对照 BA' {one(b['ba_pred'])}",
        f"- 扣费净期望 {one(exp_['net_bps'], 'bps')}/笔（fee {exp_['fee_bps']:.0f}bps）",
        f"- 一句话：{v.get('headline', '—')}",
    ]


def render_md(data: Dict[str, Any]) -> str:
    c, j, cov, sh = data["corpus"], data["judged"], data["coverage"], data["shadow"]
    pct = lambda x: "—" if x is None else f"{x*100:.1f}%"
    lines = [
        f"# 反思机制成绩单 · {data['at'][:10]}",
        "",
        "> 自动生成，只读。NULL 表示“判不了”，不按 0 计。",
        "",
        "## 一、语料",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 反思总数 | {c['total']} |",
        f"| **真反思**（llm/paper） | **{c['real']}**（{pct(c['real_ratio'])}） |",
        f"| 历史 HOLD 模板行（已迁出统计） | {c['template']} |",
        f"| 已结算（真反思） | {c['settled']} |",
        f"| 其中：盈利单(需做赢单归因) | {c.get('win', '—')} |",
        f"| 其中：亏损单 | {c.get('loss', '—')} |",
        f"| 带代码代次戳 / 其中旧代 | {c.get('fp_tagged', '—')} / {c.get('fp_other_gen', '—')} |",
        "",
        "## 二、判定能力",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 判得出对错 | {j['judged']} / {c['real']}（{pct(j['judge_rate'])}） |",
        f"| 其中：判对（错因被证实） | {j['verified']} |",
        f"| 其中：判错（反思自己错了） | {j['falsified']} |",
        f"| 判不了（NULL） | {j['unknown']} |",
        f"| 一致率（n={j['judged']}） | {pct(j['consistency'])} |",
        "",
        "## 三、覆盖",
        "",
        f"- 决策 {cov['decisions']} 条，有真反思 {cov['with_reflection']} 条（{pct(cov['rate'])}）",
        f"- 其中真交易反思仅 {cov['real_trade_reflections']} 条",
        "",
        "## 四、影子层（4 小时真值）",
        "",
        f"- 累计 {sh['rows']} 条（近 24h +{sh['rows_24h']}）：对 {sh['ok']} / 错 {sh['wrong']} / 平 {sh['flat']}",
        f"- 方向命中率 **{pct(sh['hit_rate'])}**（{sh['hit_rate_note']}）",
        *_bench_lines(sh),
        "",
        "| 品种 | n | 命中 | 命中率 |",
        "|---|---|---|---|",
    ]
    for a in sh["by_asset"]:
        lines.append(f"| {a['asset']} | {a['n']} | {a['ok']} | {pct(a['hit_rate'])} |")
    lines += [
        "",
        "| 方向 | n | 命中 |",
        "|---|---|---|",
    ]
    for d in sh["by_direction"]:
        lines.append(f"| {d['direction'] or '—'} | {d['n']} | {d['ok']} |")

    ledger = sh["ledger"]
    lines += [
        "",
        f"- 影子账本累计：{ledger['settled']} 条，命中 {ledger['hit']}（{pct(ledger['hit_rate'])}），"
        f"平均移动 {ledger['avg_move_pct']}%",
        "",
        "| 置信度桶 | n | 命中 |",
        "|---|---|---|",
    ]
    for b in sh["by_confidence"]:
        lines.append(f"| {b['bucket']} | {b['n']} | {b['hit']} |")

    h24 = sh.get("h24")
    if h24:
        lines += [
            "",
            f"- **24h 二次判决**：已结算 {h24['settled']} 条（对 {h24['ok']} / 错 {h24['wrong']}），"
            f"命中率 {pct(h24['hit_rate'])}；未到期/缺价 {h24['insufficient']} 条",
            f"- 其中 **4h 判错、24h 判对**：{h24['flipped_from_4h_wrong']} 条 —— "
            f"这部分是窗口太短，不是判断错",
        ]

    ht = data.get("horizon_truth")
    if ht and ht.get("pending_migration"):
        lines += [
            "",
            "### 反思 × 4h/24h 真值（v482）",
            "",
            "> 当前库未跑 v482 迁移（缺 `truth_*` 列），本节暂不可算；重启 engine 后自动补齐。",
        ]
    elif ht:
        lines += [
            "",
            "### 反思 × 4h/24h 真值（v482 新口径）",
            "",
            "| 项 | 值 |",
            "|---|---|",
            f"| 真反思 | {ht['real_reflections']} |",
            f"| 已盖 4h 章（判得出方向） | {ht['stamped_4h']}（判对 {ht['ok_4h']} / 判错 {ht['wrong_4h']}） |",
            f"| 4h 无法判定 | 平 {ht['flat_4h']} / 证据不足 {ht['insufficient_4h']} / 未盖章 {ht['unjudged_4h']} |",
            f"| 可判定率（4h） | {pct(ht['judge_rate_4h'])} |",
            f"| 4h 方向命中率 | {pct(ht['hit_rate_4h'])}（{ht['note']}） |",
            f"| 4h 平均净收益（扣费后） | {ht['avg_net_pct_4h']} 个百分点 |",
            f"| 24h 可判定 | {ht['stamped_24h']}，命中率 {pct(ht['hit_rate_24h'])} |",
            "",
            f"> 口径：{ht['basis']}。反思自己的 `pnl_pct` 是 20~40 分钟执行损益，"
            f"不再作为“判断对错”的依据。",
        ]

    m, p, s = data["meta"], data["patches"], data["scheduler"]
    lines += [
        "",
        "## 五、闭环",
        "",
        f"- 元反思：{m['cycles']} 条，最近 {m['last'] or '—'}；类型 {json.dumps(m['kinds'], ensure_ascii=False)}",
        f"- 策略补丁：pending {p['pending']} / rejected {p['rejected']} / applied {p['applied']}",
        f"- 经验应用：{data['injection']['experience_applications']} 条"
        f"（可注入规则 {data['injection']['injectable']} / 仅观察 {data['injection']['observable_only']}）",
        "",
        "## 六、调度",
        "",
        f"- 验证器最近评估：{s['verifier_last_evaluated'] or '—'}",
        f"- 影子层水位：{s['shadow_watermark'] or '—'}（floor {s['shadow_floor'] or '—'}）",
        f"- 富化最近成功：{s['enrich_last_ok'] or '—'}",
        "",
        "## 七、回流 A/B（批次级集群随机）",
        "",
    ]
    ab = data.get("ab") or {}
    if ab.get("groups"):
        lines += ["| 组 | 批次数 | 决策数 | 有4h真值 | 命中 | 命中率 | 平均置信 | 有向单占比 |",
                  "|---|---|---|---|---|---|---|---|"]
        for g, v in ab["groups"].items():
            lines.append(
                f"| {g} | {v['batches']} | {v['n_decisions']} | {v['n_shadow']} | {v['ok']} | "
                f"{pct(v['hit_rate'])} | {v['avg_confidence'] or '—'} | {pct(v['directional_ratio'])} |")
        lines += ["", f"分组比例 {ab.get('rate')}；命中率 n<60 只记录不结论。"]
    else:
        lines += [f"尚未形成对照样本：{ab.get('note') or ab.get('error') or '无分配记录'}"]
    hs = data["injection"].get("hit_sources") or {}
    if hs:
        lines += ["", "命中来源（`insight_hit_source`）："
                  + "、".join(f"{k}={v}" for k, v in hs.items())
                  + "。auto 是关键词兜底，不是模型采纳，统计时不能算绩效。"]
    lines += [
        "",
        "---",
        "",
        "读数规则：命中率 n<60 只记录、不结论；`reflection_effective` 的 1/0 都只在"
        "“判得出”的样本上计算，NULL 永不参与分母。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args()

    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        data = collect(conn)
    finally:
        conn.close()

    out_dir = Path(args.out_dir) if args.out_dir else (
        HERE.parent.parent / "docs" / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    day = data["at"][:10]
    json_path = out_dir / f"reflection-scorecard-{day}.json"
    md_path = out_dir / f"reflection-scorecard-{day}.md"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_md(data), encoding="utf-8")

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_md(data))
        print(f"[written] {md_path}")
        print(f"[written] {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
