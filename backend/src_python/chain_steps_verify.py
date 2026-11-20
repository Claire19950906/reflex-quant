#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chain_steps_verify.py — v329: 因果链步骤化 + 按真实结果纠正写回
===============================================================
问题 (2026-11-13 只读实测):
  causal_chains 719 条, chain_steps **全是 '[]'** —— 每条链只有一句"故事"
  (cause → effect), 没有可验证的中间步骤, 也没有任何一条被真实市场结果纠正过
  (version>1 = 0 条, last_verified 近 7 日 = 0 条)。

  已有 chain_steps_builder.py 会生成步骤, 但它:
    · 只写到 runtime/eval/eval.db 的 chain_steps_eval, **从不回写主库**;
    · 锚点用链的 created_at + 3 日行情, 而不是"这条链被触发后的真实结果";
    · 步骤1 判断是"触发词在全库新闻里出现过一次没有", 几乎必然"验证", 没有信息量。

本脚本换用**真实证据源**: causal_chain_outcomes (5096 条已评估触发记录,
每条都有 actual_pct_5d / direction_hit / magnitude_hit)。给每条链生成 3 步,
每步都带 proxy / threshold / window / verdict (铁律6 要求的结构), 然后:

  纠正写回 (只对"有足够样本"的链动手, 不动没证据的):
    · chain_steps   ← 三步 JSON (含每步证据)
    · version += 1
    · hit_count / miss_count ← 该链真实命中/未命中次数
    · confidence ← 与实测命中率向中间收敛 (0.2~0.95 夹紧)
    · last_verified_ts ← now
    · 步3 否证 且 n≥8 → enabled=0 / active=0 + invalidation 记下原因 (这条链在骗人)
    · 步3 验证 → 只提置信度, 不自动复活 (复活是人的决定)

用法:
    python3 chain_steps_verify.py --dry-run   # 默认: 只统计 + 抽样
    python3 chain_steps_verify.py --apply     # 真写主库
    python3 chain_steps_verify.py --apply --limit 50
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"

# 判定门槛 (写死并随步骤一起落库, 事后可复核口径有没有被改)
MIN_EVAL_FOR_VERDICT = 5      # 少于 5 次已评估 → 不做方向判定 (不可分离)
MIN_EVAL_FOR_OFF = 8          # 下线一条链至少要 8 次已评估
HIT_VERIFY = 0.55             # 命中率 ≥55% → 步3 验证
HIT_FALSIFY = 0.45            # 命中率 ≤45% → 步3 否证
MOVE_MIN_PCT = 0.5            # 步2: |5日涨跌| ≥0.5% 算"有传导"
MOVE_VERIFY_SHARE = 0.50      # 有传导比例 ≥50% → 步2 验证
MOVE_FALSIFY_SHARE = 0.30     # <30% → 步2 否证 (价格几乎没动, 传导不存在)
CONF_LO, CONF_HI = 0.20, 0.95
# v407: 方向为 BOTH 的链没有方向主张 —— 它的 hit_rate 是"幅度过没过线",
# 不是"方向对不对"(实测 BOTH 链命中率 93%, 用它收敛 confidence 会得到 0.95
# 的假高置信)。这类链只算"幅度链", confidence 封顶在 0.55, 且不上调。
CONF_BOTH_MAX = 0.55
# v407: 一次都没被触发评估过的链，没有资格自称 1.0 置信 —— 未验证先验一律压到 0.50。
# 旧库实测 450 条无样本链全是 confidence=1.0，这是"排序永远按噪声排"的根源。
CONF_UNVERIFIED_MAX = 0.50


def load_stats(conn: sqlite3.Connection) -> Dict[int, Dict]:
    """按 chain_id 聚合真实触发结果。只统计 direction_hit 非空的已评估行。"""
    rows = conn.execute(
        "SELECT chain_id, direction_hit, magnitude_hit, actual_pct_5d, trigger_source, "
        "       predicted_direction FROM causal_chain_outcomes "
        "WHERE direction_hit IS NOT NULL"
    ).fetchall()
    agg: Dict[int, Dict] = {}
    for cid, dh, mh, pct, src, pdir in rows:
        a = agg.setdefault(int(cid), {
            "n": 0, "hit": 0, "miss": 0, "mag_n": 0, "mag_hit": 0,
            "moved": 0, "sources": Counter(), "pdir": Counter(),
        })
        a["n"] += 1
        a["hit"] += 1 if dh == 1 else 0
        a["miss"] += 0 if dh == 1 else 1
        if mh is not None:
            a["mag_n"] += 1
            a["mag_hit"] += 1 if mh == 1 else 0
        if pct is not None and abs(float(pct)) >= MOVE_MIN_PCT:
            a["moved"] += 1
        if src:
            a["sources"][str(src)[:24]] += 1
        if pdir:
            a["pdir"][str(pdir).upper()] += 1
    return agg


def _wilson_lower(hits: int, n: int, z: float = 1.96) -> float:
    """命中率的 95% Wilson 下界（小样本不吹牛）。

    实测：n=5 全中时点估计 100%，下界只有 56% —— 用点估计写 confidence 会把
    "5 次蒙对"写成 0.95 的假高置信，决策侧就会拿它去否决真实交易。
    """
    if n <= 0:
        return 0.0
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, (c - s) / d)


def build_steps(chain: Dict, stat: Optional[Dict]) -> Tuple[List[Dict], Dict]:
    """返回 (三步 JSON, 写回补丁)。没有样本 → 三步全是"不可分离"。"""
    cid = int(chain["id"])
    direction = str(chain.get("expected_direction") or "BOTH").upper()
    built_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    if not stat:
        steps = [
            {"idx": 1, "step": "事件触发", "mechanism": "链设定的触发事件是否真的发生过",
             "proxy": "causal_chain_outcomes 已评估触发数", "threshold": "≥1 次",
             "window": "全部历史", "verdict": "不可分离",
             "evidence": "该链从未被触发评估过 (0 条 outcomes)"},
            {"idx": 2, "step": "路径传导", "mechanism": "事件是否推动该资产出现有效移动",
             "proxy": f"|5日累计涨跌| ≥{MOVE_MIN_PCT}% 的比例", "threshold": f"≥{MOVE_VERIFY_SHARE:.0%}",
             "window": "5日", "verdict": "不可分离", "evidence": "无已评估样本"},
            {"idx": 3, "step": "结果兑现", "mechanism": "价格是否朝链设定方向兑现",
             "proxy": "direction_hit 命中率", "threshold": f"≥{HIT_VERIFY:.0%} 验证 / ≤{HIT_FALSIFY:.0%} 否证",
             "window": "5日", "verdict": "不可分离", "evidence": "无已评估样本"},
        ]
        return steps, {"no_data": True, "built_at": built_at,
                       "steps_json": json.dumps(steps, ensure_ascii=False),
                       # v407: 无样本不许保留 1.0 假置信
                       "confidence": min(float(chain.get("confidence") or 1.0),
                                         CONF_UNVERIFIED_MAX)}

    n, hit = stat["n"], stat["hit"]
    hit_rate = hit / n if n else 0.0
    moved_share = stat["moved"] / n if n else 0.0
    src_txt = "/".join(k for k, _ in stat["sources"].most_common(2)) or "—"

    # 步1 事件触发: 真被触发过就是"验证" (这一步只证明链在跑, 不证明它对)
    v1 = "验证" if n > 0 else "不可分离"
    e1 = "已评估触发 %d 次 (来源: %s)" % (n, src_txt)

    # 步2 路径传导: 价格到底动没动
    if n >= MIN_EVAL_FOR_VERDICT:
        if moved_share >= MOVE_VERIFY_SHARE:
            v2 = "验证"
        elif moved_share < MOVE_FALSIFY_SHARE:
            v2 = "否证"
        else:
            v2 = "不可分离"
    else:
        v2 = "不可分离"
    e2 = "有 ≥%.1f%% 移动的比例 %.0f%% (n=%d)" % (MOVE_MIN_PCT, moved_share * 100, n)

    # 步3 结果兑现: 方向对不对
    if direction in ("UP", "DOWN") and n >= MIN_EVAL_FOR_VERDICT:
        if hit_rate >= HIT_VERIFY:
            v3 = "验证"
        elif hit_rate <= HIT_FALSIFY:
            v3 = "否证"
        else:
            v3 = "不可分离"
        e3 = "方向命中 %d/%d = %.0f%%, 预期 %s" % (hit, n, hit_rate * 100, direction)
    else:
        v3 = "不可分离"
        e3 = ("预期方向为 %s (不可判)" % direction) if direction not in ("UP", "DOWN") \
            else "样本 %d 次, 少于 %d 次不下结论" % (n, MIN_EVAL_FOR_VERDICT)

    steps = [
        {"idx": 1, "step": "事件触发", "mechanism": "链设定的触发事件是否真的发生过",
         "proxy": "causal_chain_outcomes 已评估触发数", "threshold": "≥1 次",
         "window": "全部历史", "verdict": v1, "evidence": e1},
        {"idx": 2, "step": "路径传导", "mechanism": "事件是否推动该资产出现有效移动",
         "proxy": f"|5日累计涨跌| ≥{MOVE_MIN_PCT}% 的比例", "threshold": f"≥{MOVE_VERIFY_SHARE:.0%}",
         "window": "5日", "verdict": v2, "evidence": e2},
        {"idx": 3, "step": "结果兑现", "mechanism": "价格是否朝链设定方向兑现",
         "proxy": "direction_hit 命中率", "threshold": f"≥{HIT_VERIFY:.0%} 验证 / ≤{HIT_FALSIFY:.0%} 否证",
         "window": "5日", "verdict": v3, "evidence": e3},
    ]

    old_conf = float(chain.get("confidence") or 0.5)
    # 只在样本够时改置信度: 与实测命中率各让一半, 夹紧到 [0.2, 0.95]
    new_conf = old_conf
    if n >= MIN_EVAL_FOR_VERDICT:
        if direction in ("UP", "DOWN"):
            # v407: Beta(2,2) 后验均值 (hit+2)/(n+4) —— 幂等、自带收缩，
            # 不再用"与旧置信度各让一半"(重复跑会指数衰减, 也压不住 5/5 的假高)。
            new_conf = max(CONF_LO, min(CONF_HI, (hit + 2.0) / (n + 4.0)))
        else:
            # v407: BOTH 没有方向主张, 命中率不可用来抬置信度 (见 CONF_BOTH_MAX)
            new_conf = min(float(old_conf), CONF_BOTH_MAX)
    else:
        # v407: 样本不足 5 次同样不算证据 —— 不许保留 1.0 假置信
        new_conf = min(float(old_conf), CONF_UNVERIFIED_MAX)

    off = (v3 == "否证" and n >= MIN_EVAL_FOR_OFF)
    patch = {
        "no_data": False,
        "steps_json": json.dumps(steps, ensure_ascii=False),
        "version_inc": 1,
        "hit_count": hit,
        "miss_count": stat["miss"],
        "confidence": round(new_conf, 4),
        "last_verified_ts": int(time.time()),
        "enabled": 0 if off else None,
        "invalidation": ("v329 步3否证: 方向命中 %d/%d=%.0f%% (预期 %s, 门槛 ≤%.0f%%) → 纠下线"
                         % (hit, n, hit_rate * 100, direction, HIT_FALSIFY * 100)) if off else None,
        "built_at": built_at,
        "hit_rate": hit_rate,
        "steps": steps,
    }
    return steps, patch


def run(apply: bool, limit: Optional[int]) -> int:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        stats = load_stats(conn)
        chains = [dict(r) for r in conn.execute(
            "SELECT id, cause, expected_direction, confidence, enabled, active, version "
            "FROM causal_chains ORDER BY id"
        ).fetchall()]
        if limit:
            chains = chains[:limit]

        rows = []
        verdict_tally = Counter()
        offs = 0
        no_data = 0
        conf_moves = []
        for ch in chains:
            steps, patch = build_steps(ch, stats.get(int(ch["id"])))
            for st in steps:
                verdict_tally[st["verdict"]] += 1
            if patch.get("no_data"):
                no_data += 1
                conf_moves.append(patch["confidence"] - float(ch["confidence"] or 0.5))
                rows.append((patch["steps_json"], 0, ch["id"],
                             None, None, patch["confidence"], None, None, None))
                continue
            if patch.get("enabled") == 0:
                offs += 1
            conf_moves.append(patch["confidence"] - float(ch["confidence"] or 0.5))
            rows.append((
                patch["steps_json"], patch["version_inc"], ch["id"],
                patch["hit_count"], patch["miss_count"], patch["confidence"],
                patch["last_verified_ts"], patch["enabled"], patch["invalidation"],
            ))

        n_struct = sum(1 for ch in chains)  # 每条都写了 3 步
        print("因果链 %d 条 → 写 %d 条链的步骤 (每条 3 步)" % (len(chains), len(rows)))
        print("  步 verdict 分布: 验证 %d / 否证 %d / 不可分离 %d"
              % (verdict_tally["验证"], verdict_tally["否证"], verdict_tally["不可分离"]))
        print("  有真实样本的链 %d · 无样本(不可分离) %d"
              % (len(chains) - no_data, no_data))
        print("  纠下线(步3否证且 n≥%d) %d 条" % (MIN_EVAL_FOR_OFF, offs))
        if conf_moves:
            up = sum(1 for d in conf_moves if d > 0.005)
            dn = sum(1 for d in conf_moves if d < -0.005)
            print("  置信度调整: 上调 %d / 下调 %d / 基本不动 %d"
                  % (up, dn, len(conf_moves) - up - dn))

        if not apply:
            print("\n[DRY-RUN] 未写主库。加 --apply 生效。")
            print("抽样 (前 4 条有样本的链):")
            shown = 0
            for ch in chains:
                st = stats.get(int(ch["id"]))
                if not st or shown >= 4:
                    continue
                _steps, patch = build_steps(ch, st)
                print("  #%s %s 方向=%s n=%d 命中率=%.0f%% 置信 %.2f→%.2f%s"
                      % (ch["id"], str(ch["cause"])[:26], ch["expected_direction"], st["n"],
                         patch["hit_rate"] * 100, float(ch["confidence"] or 0.5),
                         patch["confidence"],
                         "  [下线]" if patch.get("enabled") == 0 else ""))
                for s in patch["steps"]:
                    print("      %d %s → %s | %s" % (s["idx"], s["step"], s["verdict"], s["evidence"]))
                shown += 1
            return 0

        cur = conn.cursor()
        for rid, (steps_json, vinc, cid, *rest) in enumerate(rows):
            hit_n, miss_n, conf, verified_ts, off, inval = rest
            if hit_n is None:  # 无样本: 补步骤 + 压掉假置信, 不动版本/命中数
                cur.execute(
                    "UPDATE causal_chains SET chain_steps=?, confidence=?, updated_at=? "
                    "WHERE id=?",
                    (steps_json, conf, time.strftime("%Y-%m-%d %H:%M:%S"), cid),
                )
                continue
            cur.execute(
                "UPDATE causal_chains SET chain_steps=?, version=version+?, hit_count=?, "
                "miss_count=?, confidence=?, last_verified_ts=?, "
                "enabled=COALESCE(?, enabled), active=COALESCE(?, active), "
                "invalidation=CASE WHEN ? IS NOT NULL THEN ? ELSE invalidation END, "
                "updated_at=? WHERE id=?",
                (steps_json, vinc, hit_n, miss_n, conf, verified_ts, off, off,
                 inval, inval, time.strftime("%Y-%m-%d %H:%M:%S"), cid),
            )
        conn.commit()
        print("\n[APPLY] 已写回 %d 条链" % len(rows))
        n_v2 = conn.execute("SELECT COUNT(*) FROM causal_chains WHERE version>1").fetchone()[0]
        print("  version>1 的链: %d 条" % n_v2)
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="因果链步骤化 + 纠正写回 (v329)")
    ap.add_argument("--apply", action="store_true", help="真写主库 (默认 dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写 (默认)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条")
    args = ap.parse_args()
    return run(apply=bool(args.apply) and not bool(args.dry_run),
               limit=args.limit or None)


if __name__ == "__main__":
    raise SystemExit(main())
