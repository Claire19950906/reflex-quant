#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""experience_distiller.py — 反思 → 经验 自动提炼器 (v337)

为什么需要这个模块
------------------
`experience_entries` 一直是 0 行：

  · `experience_reflector.py` 只提供 load_experiences / build_prompt_block /
    apply_experience / record_root_cause —— **全是读**；
  · 唯一的写入口是 api_server 的 POST /api/v188/experiences/add（source='manual_v188'）；
  · 于是 6,174 条反思、3,405 条决策错误，一条经验都没沉淀下来，
    `/review?tab=experiences` 和分析过程第 6 段永远"暂无"。

本模块把两批**已经存在但没人读**的数据压成可执行的长期经验：

  来源 1  decision_errors（近 N 天）——按 error_code 聚合，出现 ≥ min_count 次的
          错误码生成一条 `kind='process'` 经验：根因 + 应做规则 + 复核方法。
          这批是"系统自己在 3,405 条决策里统计出来的硬事实"，权重最高。
  来源 2  trade_reflections_v2 ——按 signal_name 聚合（带 lesson 的），出现 ≥ 5 次的
          信号类型生成一条 `kind='correction'` 经验：沿用反思里已写下的教训原文。

写入遵守 `uq_experience_source_title(source,title)` 唯一索引：先查后更，不重复插入、
不覆盖 apply_count/hit_count（那是运行时累积的统计，不能被提炼器重置）。

用法:
    python3 experience_distiller.py --dry-run        # 只打印将生成的条目
    python3 experience_distiller.py                  # 写库（幂等，可反复跑）
    python3 experience_distiller.py --days 30 --min-count 100 --json
退出码: 0 = 正常；1 = 抛异常
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db

SOURCE = "reflection_distill_v337"


def current_fingerprint() -> str:
    """当前代码代次指纹; 取不到返回空串(不参与世代过滤)。"""
    try:
        from code_fingerprint import fingerprint as _fp
        v = str(_fp() or "")
        return "" if v.strip().lower() in ("", "unknown") else v
    except Exception:
        return ""


def source_tag() -> str:
    """v482: 经验来源带上代码代次 —— 换代后旧经验在注入侧自动失效。

    格式 `reflection_distill_v337:<指纹>`; 指纹缺失时退回老格式(不过滤)。
    """
    fp = current_fingerprint()
    return f"{SOURCE}:{fp}" if fp else SOURCE

# ── 错误码的人类可读根因 / 应做规则 / 复核方法 ───────────────────────────────
# 只覆盖实测出现量大的码；未列出的码回退到维度小类的通用文案。
CODE_PLAYBOOK = {
    "E001a": (
        "下单时 hard_data_evidence 为空 —— 只有观点没有事实（价格/量能/资金/事件任一硬数据）",
        "开仓前必须齐 2 条独立硬事实，且至少 1 条来自非价格源（资金流/持仓/宏观/事件原子）；"
        "只有 1 条硬事实时 confidence 上限 0.5",
        "回放这批判单，检查若强制补齐第 2 条事实，方向是否仍成立；统计补齐后 5 日命中率 lift",
    ),
    "E001c": (
        "缺因子方向 —— 新闻/事件给了，但没算出它对目标品种该往哪个方向推",
        "每条入模事件必须落 direction(+1/-1/0) 与传导路径（事件→品种→方向）；"
        "算不出方向的事件不得进入打分",
        "对无方向的决策抽样补算方向，比较补算后与原始 final_action 的一致率",
    ),
    "E001d": (
        "缺事件链关联 —— 决策没挂 news_id / atom_ids，事后无法回溯是哪条新闻驱动",
        "落单必须写 news_id 与 event_atom ids；无关联的决策一律视为无依据",
        "统计有/无事件关联两组决策的 5 日实际收益差",
    ),
    "E005": (
        "未引用任何硬铁律 —— 推理里没有一条不可违背的规则约束",
        "reasoning_path 必须显式引用 ≥1 条硬铁律（如单信源降权、跨资产背离降级、强度闸门）",
        "对无铁律决策抽样重算，若加铁律后被拒单，统计这些单的事后盈亏",
    ),
    "E014": (
        "推理链断裂 —— 八段推理缺段（事实→机制→传导→方向→幅度→反证→结论→触发条件）",
        "八段全齐才允许出方向；缺段一律 HOLD，并把缺的段名写进 decision_errors",
        "按缺段类型分组统计，看哪一段缺失最常导致方向错",
    ),
    "E028": (
        "自相矛盾 —— 结论与前面的证据/因子方向打架",
        "出结论前做一次自检：final_action 与 factor_ranking 方向一致率必须 100%",
        "抽样 50 条矛盾单，人工核对正确方向，校准自检规则",
    ),
    "E020": (
        "样本质量不满足硬规则 —— 用低质量样本支撑高置信",
        "置信度上限由样本量决定：n<10 时 confidence ≤0.4；n<5 不出方向",
        "画置信-实际命中率校准曲线，检查 0.6 以上档位的真实命中",
    ),
    "E007": (
        "事件过旧 / 市场已反向 —— 用过期事件解释当下的行情",
        "事件时效 ≤30min 才可作为触发依据；>2h 的事件只能作背景，不得进打分",
        "按事件年龄分组统计方向命中率，找出时效拐点",
    ),
}

DIM_FALLBACK = {
    ("C", "feed_missing"): ("数据缺失 —— 关键字段为空就出结论", "缺字段即拒单，并把缺的字段名落库", "统计缺字段决策的事后盈亏"),
    ("C", "signal_delay"): ("信号延迟 —— 数据过期仍被当实时使用", "所有入模数据带 data_anchor_ts，>30min 视为过期", "统计时延与方向命中的关系"),
    ("C", "calibration_drift"): ("置信未校准 —— 高置信却低命中", "按分档校准置信，每 30 天重算一次", "画 reliability diagram 检查校准"),
    ("B", "event_chain_break"): ("事件链断裂 —— 因果链缺环，方向推不出来", "链条必须首尾相接，缺环即 HOLD", "按缺环位置统计方向错误率"),
    ("B", "cause_effect_reversed"): ("因果倒置 / 依据不实 —— 把结果当原因", "每条因果边必须有机制解释与反例检查", "抽样复核因果方向"),
    ("B", "news_direction_inverted"): ("方向读反 —— 新闻利空当利好", "方向由事件原子+品种映射共同决定，单靠标题关键词不算", "对比标题法 vs 原子法的方向一致率"),
    ("B", "macro_misattribute"): ("宏观错配 —— 用不相干的宏观变量解释品种", "跨资产传导必须有数值互证（相关性/同向幅度）", "检查跨资产相关性是否支持该传导"),
    ("A", "market_regime_misread"): ("市场状态读错 —— 把趋势市当震荡市", "先定 regime（趋势/震荡/逼仓）再选策略", "按 regime 分组统计策略胜率"),
    ("A", "pattern_recognition"): ("形态/对象认错 —— 信号与品种不匹配", "信号必须绑定适用品种白名单", "统计白名单外信号的事后表现"),
    ("A", "psychological_anchor"): ("置信虚高 —— 证据少但分数高", "confidence 由证据量决定，不由叙事强度决定", "检查高置信组的证据条数分布"),
    ("A", "volume_price_divergence"): ("量价背离 —— 价格动了但量/资金没跟", "量价背离时只做减仓不做加仓", "按量价一致性分组统计盈亏"),
}


def _conn():
    conn = db.get_connection()
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _code_meta(code: str) -> dict:
    """错误码 → (维度, 小类, 根因, 应做, 复核)。"""
    dim, sub = "", ""
    try:
        from review import reflection_dimension_map as rdm  # noqa: WPS433
        for key in (code, code.upper(), (code or "").lower()):
            if key in rdm.E_CODE_TO_DIMENSION:
                dim, sub = rdm.E_CODE_TO_DIMENSION[key]
                break
    except Exception:
        pass
    play = CODE_PLAYBOOK.get(code)
    if play:
        root, lesson, verify = play
    else:
        root, lesson, verify = DIM_FALLBACK.get(
            (dim, sub),
            (f"{sub or '未分类'} —— {code}", "见决策错误明细", "抽样复核该错误码对应决策的事后盈亏"),
        )
    return {"dimension": dim, "subcategory": sub, "root": root, "lesson": lesson, "verify": verify}


def _assets_of(conn, code: str, days: int, limit: int = 6) -> list:
    rows = conn.execute(
        """SELECT d.asset, COUNT(*) c
             FROM decision_errors e
             JOIN trade_decisions d ON d.id = e.decision_id
            WHERE e.error_code = ? AND e.created_at >= strftime('%s','now') - ?*86400
            GROUP BY d.asset ORDER BY c DESC LIMIT ?""",
        (code, int(days), int(limit)),
    ).fetchall()
    return [str(r[0]) for r in rows if r[0]]


def distill_from_errors(conn, days: int, min_count: int, max_items: int = 8) -> list:
    rows = conn.execute(
        """SELECT error_code, COUNT(*) c
             FROM decision_errors
            WHERE created_at >= strftime('%s','now') - ?*86400
              AND COALESCE(error_code,'')<>''
            GROUP BY error_code HAVING c >= ?
            ORDER BY c DESC LIMIT ?""",
        (int(days), int(min_count), int(max_items)),
    ).fetchall()
    items = []
    for r in rows:
        code, n = str(r[0]), int(r[1])
        meta = _code_meta(code)
        assets = _assets_of(conn, code, days)
        title = f"[{code}] {meta['root'].split(' —— ')[0][:40]}"
        content = "\n".join([
            f"ROOT_CAUSE: {meta['root']}（近 {days} 天 {n} 条）",
            f"LESSON: {meta['lesson']}",
            f"VERIFICATION: {meta['verify']}",
        ])
        weight = round(min(1.0, 0.5 + n / 2000.0), 3)
        items.append({
            "kind": "process", "title": title, "content": content,
            "assets": assets, "weight": weight, "priority": 75,
            "evidence_n": n, "code": code, "source": source_tag(),
        })
    return items


def distill_from_reflections(conn, days: int, min_count: int = 5, max_items: int = 8) -> list:
    rows = conn.execute(
        """SELECT signal_name, COUNT(*) c
             FROM trade_reflections_v2
            WHERE COALESCE(signal_name,'')<>'' AND COALESCE(lesson,'')<>''
              AND created_at >= datetime('now', ?)
            GROUP BY signal_name HAVING c >= ?
            ORDER BY c DESC LIMIT ?""",
        (f"-{int(days)} days", int(min_count), int(max_items)),
    ).fetchall()
    items = []
    for r in rows:
        sig, n = str(r[0]), int(r[1])
        sample = conn.execute(
            """SELECT lesson, verification, root_cause
                 FROM trade_reflections_v2
                WHERE signal_name = ? AND COALESCE(lesson,'')<>''
                ORDER BY id DESC LIMIT 1""",
            (sig,),
        ).fetchone()
        lesson = (sample[0] or "").strip() if sample else ""
        verify = (sample[1] or "").strip() if sample else ""
        root = (sample[2] or "").strip() if sample else ""
        assets = [str(x[0]) for x in conn.execute(
            """SELECT asset, COUNT(*) c FROM trade_reflections_v2
                WHERE signal_name = ? AND COALESCE(asset,'')<>''
                GROUP BY asset ORDER BY c DESC LIMIT 6""",
            (sig,),
        ).fetchall() if x[0]]
        title = f"[{sig}] 重复出现的信号类型"
        fallback_verify = "同类信号再出现时, 对比该品种接下来 5 次的方向命中"
        content = "\n".join([
            f"ROOT_CAUSE: {root or sig}（近 {days} 天 {n} 条反思指向它）",
            f"LESSON: {lesson}",
            f"VERIFICATION: {verify or fallback_verify}",
        ])
        items.append({
            "kind": "correction", "title": title, "content": content,
            "assets": assets, "weight": round(min(1.0, 0.4 + n / 500.0), 3),
            "priority": 60, "evidence_n": n, "code": sig, "source": source_tag(),
        })
    return items


def distill_from_wins(conn, days: int, min_count: int = 3, max_items: int = 5) -> list:
    """v482: 从**赢单**掘正向经验 —— 经验池不能只有"别做什么"。

    只取已结算、扣费后 pnl >= +0.5% 的反思(settled=1 且非模板行)。
    优先用 LLM 归因里写下的 keep_rule / invalidation_condition;
    没有 LLM 反思时退回模板 lesson + 可量化失效条件。
    """
    rows = conn.execute(
        """SELECT signal_name, COUNT(*) c, SUM(pnl_pct) s
             FROM trade_reflections_v2
            WHERE COALESCE(signal_name,'')<>'' AND settled = 1
              AND pnl_pct >= 0.005
              AND COALESCE(origin,'')<>'template'
              AND created_at >= datetime('now', ?)
            GROUP BY signal_name HAVING c >= ?
            ORDER BY c DESC LIMIT ?""",
        (f"-{int(days)} days", int(min_count), int(max_items)),
    ).fetchall()
    items = []
    for r in rows:
        sig, n, pnl_sum = str(r[0]), int(r[1]), float(r[2] or 0.0)
        sample = conn.execute(
            """SELECT lesson, llm_depth_reflection, asset
                 FROM trade_reflections_v2
                WHERE signal_name = ? AND pnl_pct >= 0.005 AND settled = 1
                -- 优先取带 LLM 归因的那条(keep_rule 才有内容), 再按新到旧
                ORDER BY (COALESCE(llm_depth_reflection,'') <> '') DESC, id DESC
                LIMIT 1""",
            (sig,),
        ).fetchone()
        lesson = (sample[0] or "").strip() if sample else ""
        keep_rule, invalid = "", ""
        if sample and (sample[1] or "").strip():
            try:
                j = json.loads(sample[1])
                keep_rule = str(j.get("keep_rule") or "").strip()
                invalid = str(j.get("invalidation_condition") or "").strip()
                lesson = str(j.get("lesson_candidate") or lesson).strip()
            except Exception:
                pass
        assets = [str(x[0]) for x in conn.execute(
            """SELECT asset, COUNT(*) c FROM trade_reflections_v2
                WHERE signal_name = ? AND asset <> '' AND pnl_pct >= 0.005
                GROUP BY asset ORDER BY c DESC LIMIT 6""",
            (sig,),
        ).fetchall() if x[0]]
        avg_pnl = pnl_sum / n if n else 0.0
        content = "\n".join([
            f"WIN_CASE: 该信号近 {days} 天有 {n} 次盈利, 平均净收益 {avg_pnl:+.4f}%",
            f"KEEP_RULE: {keep_rule or lesson or sig}",
            f"INVALIDATION: {invalid or '同 layer 近 20 条命中率跌破 50% 时本条失效'}",
        ])
        items.append({
            "kind": "principle", "title": f"[{sig}] 可复用的成功条件",
            "content": content, "assets": assets,
            "weight": round(min(1.0, 0.5 + n / 100.0), 3),
            "priority": 70, "evidence_n": n, "code": sig,
        })
    return items


def upsert(conn, item: dict, dry_run: bool) -> str:
    title = item["title"]
    content = item["content"]
    src = item.get("source") or SOURCE
    assets = json.dumps(item["assets"], ensure_ascii=False)
    existing = conn.execute(
        "SELECT id FROM experience_entries WHERE source = ? AND title = ?",
        (src, title),
    ).fetchone()
    if dry_run:
        return "would_update" if existing else "would_insert"
    if existing:
        conn.execute(
            """UPDATE experience_entries
                  SET content = ?, applicable_assets = ?, weight = ?, priority = ?,
                      kind = ?, status = 'active', updated_at = datetime('now')
                WHERE id = ?""",
            (content, assets, float(item["weight"]), int(item["priority"]),
             item["kind"], int(existing[0])),
        )
        return "updated"
    conn.execute(
        """INSERT INTO experience_entries
              (kind, title, content, applicable_assets, applicable_kinds,
               applicable_actors, weight, priority, source, status,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, '[]', '[]', ?, ?, ?, 'active',
                    datetime('now'), datetime('now'))""",
        (item["kind"], title, content, assets, float(item["weight"]),
         int(item["priority"]), src),
    )
    return "inserted"


def run(days: int = 30, min_count: int = 100, dry_run: bool = False) -> dict:
    t0 = time.time()
    conn = _conn()
    out = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "days": days,
           "min_count": min_count, "dry_run": dry_run,
           "inserted": 0, "updated": 0, "items": [], "errors": []}
    try:
        before = conn.execute("SELECT COUNT(*) FROM experience_entries").fetchone()[0]
        items = (distill_from_errors(conn, days, min_count)
                 + distill_from_reflections(conn, days, 5)
                 + distill_from_wins(conn, days, 3))   # v482: 赢单也要沉淀正向经验
        for it in items:
            try:
                act = upsert(conn, it, dry_run)
                if act in ("inserted", "updated", "would_insert", "would_update"):
                    out[act] = out.get(act, 0) + 1
                out["items"].append({
                    "kind": it["kind"], "title": it["title"], "n": it["evidence_n"],
                    "weight": it["weight"], "assets": it["assets"], "action": act,
                })
            except sqlite3.IntegrityError as exc:
                out["errors"].append(f"{it['title']}: UNIQUE {exc}")
            except Exception as exc:
                out["errors"].append(f"{it['title']}: {type(exc).__name__}: {exc}")
        if not dry_run:
            conn.commit()
        out["total_before"] = int(before)
        out["total_after"] = int(conn.execute("SELECT COUNT(*) FROM experience_entries").fetchone()[0])
        out["elapsed_sec"] = round(time.time() - t0, 1)
        return out
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-count", type=int, default=100, help="错误码至少出现多少次才提炼")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        res = run(days=args.days, min_count=args.min_count, dry_run=args.dry_run)
    except Exception as exc:
        print(f"[distill] FAIL {type(exc).__name__}: {exc}")
        return 1
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(f"[distill] {res['total_before']} -> {res['total_after']} 条经验 "
              f"· inserted={res.get('inserted', 0)} updated={res.get('updated', 0)} "
              f"· {res['elapsed_sec']}s")
        for it in res["items"]:
            print(f"  {it['action']:<14} {it['kind']:<10} n={it['n']:<5} {it['title']}")
        if res["errors"]:
            print("  errors:", "; ".join(res["errors"][:3]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
