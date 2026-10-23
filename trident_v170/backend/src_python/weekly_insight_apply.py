#!/usr/bin/env python3
"""v190 #4 — weekly_insight_apply：周级把 review_insights 推到 strategy_param_adjustments。

为什么需要这个脚本：
- review_agent 已经把"软启发"沉淀到 review_insights 表（active=1 AND kind=1）；
- 但 review_insights.rule 是自然语言，没有结构化 (param_key, direction, magnitude)，
  不能直接被 paper_trading_settings 消费。
- v182/v188/v189 reflection.py 的 _aggregate_improvements_v2 已经自动聚合 trade_reflections_v2
  → strategy_param_adjustments，但只看 trade_reflections_v2 的 layer/sub_layer/signal_name，
  **完全不看 review_insights**。这是两个独立数据源。
- 每周一次把 source_samples >= 10 的 active=1 kind=1 insight 转成 adjustment 提议（pending），
  让管理员通过 `/api/paper/adjustments/{id}/approve` 审批（与 v188 同审批链路）。

设计原则：
1. 严格门槛：active=1 AND kind=1 AND source_samples >= 10 (默认) — 远高于 v182 reflection
   的 _V182_ADJUSTMENT_SAMPLE_THRESHOLD=5，避免"软启发"自动放大成参数调整。
2. 结构化提取：从 rule 文本中抽取 (param_key, direction, action) — 抽取不到 param_key
   的（非结构化规则）跳过 + 计入 "skipped_no_param"，不阻塞其它 insight。
3. 去重：相同 (asset, action, param_key, direction) 已存在 pending → 累加 sample_size +
   evidence_summary，跳过 INSERT。
4. 幂等：连续跑 N 次只产生 1 个 pending adjustment（不会重复 INSERT）。
5. 不调 LLM：纯结构化正则提取 — LLM 提取放在未来 endpoint `/api/admin/insight/parse`
   手动触发（成本可控 + 人工可审）。

使用：
    python3 src_python/weekly_insight_apply.py            # 立即跑一次
    python3 src_python/weekly_insight_apply.py --daemon   # 7 天一次循环
    python3 src_python/weekly_insight_apply.py --dry-run  # 只打印不写库
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import config
import db

_WEEK_SECONDS = 7 * 24 * 3600
_DEFAULT_MIN_SAMPLES = 10  # 软启发门槛（比 v182 reflection=5 更严）

# 已知 param_key 库（layer.sub_layer.signal_name 模式）。不在此表的全部跳过，
# 防止把 "XAU做多在强避险..." 这种纯文字规则误转成 filter.* 之类的虚假 param。
_KNOWN_PARAM_KEYS: List[str] = [
    # filter.*
    "filter.asset_match.main_asset_missed",
    "filter.asset_match.cross_asset_misroute",
    "filter.contradiction_check.sentiment_vs_market",
    "filter.event_atoms.asset_mismatch",
    "filter.gate_rejected",
    # analysis.*
    "analysis.knowledge_inject.exp_misinterpreted",
    "analysis.knowledge_inject.no_trade",
    "analysis.llm_output.unknown",
    "analysis.llm_output.correctly_held",
    # sizing/risk.*
    "sizing.position_weight",
    "sizing.stop_loss_pct",
    "sizing.take_profit_pct",
    "sizing.max_position_pct",
    # score.*
    "score.confidence_threshold",
    "score.event_strength_threshold",
    "score.buy_min_score",
    "score.sell_min_score",
]

# v191 #1: 模糊中文关键词 → param_key 反查表
# review_agent 提炼的 insight 多为「自然语言策略规则」（如「加密主流仓位权重低于贵金属」）
# 而不是直白的「sizing.position_weight 应下调」；regex 命中 _KNOWN_PARAM_KEYS 尾部失败时
# 在此表查模糊语义。键为出现顺序在前的优先级越高。
#
# v200 P0-A1: 扩到覆盖 XAU/能源/BTC 反向模式 / 阈值 / 演员经验 / 同向预期证伪 等高频规则。
# 之前 30 条 skipped_no_param 中的 XAU BUY 反向 / 阈值上调 / 跨资产证伪 类规则都靠这层兜住。
_KEYWORD_TO_PARAM: List[Tuple[str, str]] = [
    # ── 仓位相关 ──
    ("仓位权重", "sizing.position_weight"),
    ("仓位比例", "sizing.position_weight"),
    ("单笔仓位", "sizing.position_weight"),
    ("最大仓位", "sizing.max_position_pct"),
    ("头寸规模", "sizing.position_weight"),
    ("头寸权重", "sizing.position_weight"),
    # ── 止损 / 止盈 ──
    ("动态止损", "sizing.stop_loss_pct"),
    ("止损", "sizing.stop_loss_pct"),
    ("止盈", "sizing.take_profit_pct"),
    ("take_profit", "sizing.take_profit_pct"),
    ("stop_loss", "sizing.stop_loss_pct"),
    # ── 置信度 / 信号分 ──
    ("置信度阈值", "score.confidence_threshold"),
    ("置信度门槛", "score.confidence_threshold"),
    ("置信门槛", "score.confidence_threshold"),
    ("信心门槛", "score.confidence_threshold"),
    ("事件强度阈值", "score.event_strength_threshold"),
    ("事件强度门槛", "score.event_strength_threshold"),
    ("buy_min_score", "score.buy_min_score"),
    ("sell_min_score", "score.sell_min_score"),
    # ── 信号过滤类 ──
    ("主资产漏报", "filter.asset_match.main_asset_missed"),
    ("资产匹配", "filter.asset_match.main_asset_missed"),
    ("矛盾信号", "filter.contradiction_check.sentiment_vs_market"),
    ("新闻质量", "filter.news_quality.partial_skipped"),
    # ── v200 P0-A1 新增：XAU/能源/BTC 反向模式 & 阈值调整类 ──
    # XAU BUY 关闭 / 反向类
    ("BUY 关闭", "score.buy_min_score"),
    ("BUY信号关闭", "score.buy_min_score"),
    ("关闭 BUY", "score.buy_min_score"),
    ("禁用 BUY", "score.buy_min_score"),
    ("BUY 反向", "score.buy_min_score"),
    ("BUY方向反向", "score.buy_min_score"),
    ("持续反向", "score.buy_min_score"),
    ("BUY 持续", "score.buy_min_score"),
    # 阈值上调 / 下调（覆盖 score.* 系列）
    ("阈值上调", "score.buy_min_score"),
    ("阈值下调", "score.confidence_threshold"),
    ("门槛上调", "score.buy_min_score"),
    ("门槛下调", "score.confidence_threshold"),
    ("BUY 置信度应大幅下调", "score.confidence_threshold"),
    ("BUY 阈值区间", "score.buy_min_score"),
    ("BUY 置信度", "score.confidence_threshold"),
    ("该阈值区间", "score.buy_min_score"),
    ("信号质量过滤", "filter.news_quality.partial_skipped"),
    ("同向预期证伪", "filter.contradiction_check.sentiment_vs_market"),
    ("同向预期", "filter.contradiction_check.sentiment_vs_market"),
    ("预期证伪", "filter.contradiction_check.sentiment_vs_market"),
    ("跨资产证伪", "filter.asset_match.cross_asset_misroute"),
    ("新闻驱动", "filter.news_quality.partial_skipped"),
    # 演员 / 模型经验类
    ("可适度提高权重", "sizing.position_weight"),
    ("该方向可适度", "sizing.position_weight"),
    ("胜率高", "sizing.position_weight"),
    ("胜率稳定", "sizing.position_weight"),
    ("模型在", "filter.knowledge_inject.exp_misinterpreted"),
    ("模型胜率", "sizing.position_weight"),
    # SELL 关闭 / 反向类
    ("SELL 关闭", "score.sell_min_score"),
    ("关闭 SELL", "score.sell_min_score"),
    ("SELL 反向", "score.sell_min_score"),
]

# direction 关键词映射（按优先级匹配，匹配到就停；down 先于 up，避免"上调"匹配"下调"误判）
_DIR_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("off", re.compile(r"关闭|禁用|停用|disable|turn\s*off|(?<![A-Za-z])off(?![A-Za-z])", re.IGNORECASE)),
    ("on", re.compile(r"开启|启用|enable|turn\s*on|应设|激活|(?<![A-Za-z])on(?![A-Za-z])", re.IGNORECASE)),
    ("down", re.compile(r"下调|减少|降低|放宽|低于|应低于|decrease|lower|relax|-\s*0\.0\d|(?<![A-Za-z])down(?![A-Za-z])", re.IGNORECASE)),
    ("up", re.compile(r"上调|增加|提高|收紧|应高于|increase|raise|tighten|\+\s*0\.0\d|(?<![A-Za-z])up(?![A-Za-z])", re.IGNORECASE)),
]

# action 关键词映射（按优先级匹配；用 ASCII lookbehind/lookahead 替代 \b，
# 防止 unicode \w 把中文算进 word boundary，例如 "SELL方向" 里 \bSELL\b 不匹配。
# 也避免 "threshold" 末尾的 "hold" 误触发 HOLD。）
_ACTION_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("BUY", re.compile(r"做多|买入|建仓多|多单|(?<![A-Za-z])BUY(?![A-Za-z])|(?<![A-Za-z])long(?![A-Za-z])", re.IGNORECASE)),
    ("SELL", re.compile(r"做空|卖出|建仓空|空单|(?<![A-Za-z])SELL(?![A-Za-z])|(?<![A-Za-z])short(?![A-Za-z])", re.IGNORECASE)),
    ("HOLD", re.compile(r"观望|持有|不交易|跳过|避免|(?<![A-Za-z])hold(?![A-Za-z])", re.IGNORECASE)),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _extract_param_key(rule: str) -> Optional[str]:
    """从 rule 文本抽取 layer.sub_layer.signal_name 形态的 param_key。"""
    if not rule:
        return None
    # 1) 直接搜 "xxx.yyy.zzz" 三段点分
    direct = re.findall(r"\b([a-zA-Z][\w]*\.[a-zA-Z][\w]*\.[a-zA-Z][\w]*)\b", rule)
    for d in direct:
        if d in _KNOWN_PARAM_KEYS:
            return d
    # 2) 反向拼：找尾部 signal_name，命中 _KNOWN_PARAM_KEYS 任一即返回
    for kp in _KNOWN_PARAM_KEYS:
        signal = kp.split(".")[-1]
        if signal and signal in rule:
            return kp
    # 3) v191 #1: 模糊中文关键词映射
    # review_agent 提炼的 insight 多为「自然语言策略规则」,无法精确命中尾部 signal_name。
    # 在 _KEYWORD_TO_PARAM 反查,命中最先出现的 keyword。
    for keyword, param_key in _KEYWORD_TO_PARAM:
        if keyword in rule:
            return param_key
    return None


def _extract_direction(rule: str) -> Optional[str]:
    if not rule:
        return None
    for tag, pat in _DIR_PATTERNS:
        if pat.search(rule):
            return tag
    return None


def _extract_action(rule: str) -> str:
    if not rule:
        return "*"
    for tag, pat in _ACTION_PATTERNS:
        if pat.search(rule):
            return tag
    return "*"


def _compute_magnitude(source_samples: int) -> float:
    """按 source_samples 决定 magnitude（confidence proxy）。"""
    if source_samples >= 30:
        return 0.10
    if source_samples >= 20:
        return 0.07
    if source_samples >= 10:
        return 0.05
    return 0.03


def apply_once(
    conn: sqlite3.Connection,
    min_samples: int = _DEFAULT_MIN_SAMPLES,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """跑一次 weekly promotion。返回 dict 描述本次操作统计。"""
    conn.row_factory = sqlite3.Row
    out: Dict[str, Any] = {
        "scanned": 0,
        "inserted": [],
        "merged": [],
        "skipped_no_param": [],
        "skipped_no_direction": [],
        "skipped_kind0": [],
        "skipped_insufficient_n": [],
        "dry_run": dry_run,
        "ts": _now_iso(),
    }
    rows = conn.execute(
        """SELECT id, rule, asset, source_samples, origin, kind
             FROM review_insights
            WHERE active = 1
              AND COALESCE(manual_deactivated, 0) = 0
         ORDER BY source_samples DESC, id DESC"""
    ).fetchall()
    out["scanned"] = len(rows)

    for r in rows:
        iid = int(r["id"])
        rule = str(r["rule"] or "")
        asset = str(r["asset"] or "") or "*"
        n = int(r["source_samples"] or 0)
        origin = str(r["origin"] or "real")
        kind = int(r["kind"] or 0)

        # 硬铁律 (kind=0) 不转 — 那是强制约束，不是参数调整建议
        if kind != 1:
            out["skipped_kind0"].append(iid)
            continue
        # 样本门槛
        if n < min_samples:
            out["skipped_insufficient_n"].append((iid, n))
            continue

        param_key = _extract_param_key(rule)
        if not param_key:
            out["skipped_no_param"].append((iid, rule[:80]))
            continue

        direction = _extract_direction(rule)
        if not direction:
            out["skipped_no_direction"].append((iid, rule[:80]))
            continue

        action = _extract_action(rule)
        magnitude = _compute_magnitude(n)
        avg_conf = round(min(1.0, max(0.0, n / 30.0)), 4)
        evidence = f"[insight #{iid} n={n} origin={origin}] {rule[:200]}"

        if dry_run:
            out["inserted"].append({
                "insight_id": iid, "asset": asset, "action": action,
                "param_key": param_key, "direction": direction,
                "magnitude": magnitude, "n": n,
            })
            continue

        # 去重：相同 (asset, action, param_key, direction) 已 pending → 累加
        existing = conn.execute(
            """SELECT id, sample_size, avg_confidence, evidence_summary
                 FROM strategy_param_adjustments
                WHERE asset=? AND action=? AND param_key=? AND direction=? AND status='pending'
                ORDER BY id DESC LIMIT 1""",
            (asset, action, param_key, direction),
        ).fetchone()
        if existing:
            old_n = int(existing["sample_size"] or 0)
            old_c = float(existing["avg_confidence"] or 0.0)
            old_id = int(existing["id"])
            new_n = old_n + n
            new_c = (old_c * old_n + avg_conf * n) / new_n if new_n else avg_conf
            new_evi = (str(existing["evidence_summary"] or "") + " | " + evidence)[-2000:]
            conn.execute(
                """UPDATE strategy_param_adjustments
                      SET sample_size=?, avg_confidence=?, evidence_summary=?,
                          last_suggested_at=?, updated_at=?
                    WHERE id=?""",
                (new_n, round(new_c, 4), new_evi, _now_iso(), _now_iso(), old_id),
            )
            out["merged"].append({
                "insight_id": iid, "adj_id": old_id,
                "new_sample_size": new_n,
            })
            continue

        # 新 INSERT
        cur = conn.execute(
            """INSERT INTO strategy_param_adjustments
                  (asset, action, param_key, direction, magnitude, sample_size,
                   avg_confidence, evidence_summary, first_suggested_at,
                   last_suggested_at, status, applied_strategy_version_id,
                   applied_setting_change, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, '',
                        'v190_weekly_promote', ?, ?)""",
            (
                asset, action, param_key, direction, magnitude, n,
                avg_conf, evidence, _now_iso(), _now_iso(),
                _now_iso(), _now_iso(),
            ),
        )
        out["inserted"].append({
            "insight_id": iid, "adj_id": int(cur.lastrowid),
            "asset": asset, "action": action,
            "param_key": param_key, "direction": direction,
            "magnitude": magnitude,
        })

    if not dry_run and (out["inserted"] or out["merged"]):
        conn.commit()
    return out


def _print_summary(result: Dict[str, Any]) -> None:
    print()
    print("=" * 70)
    print(f"[WEEKLY-INSIGHT-APPLY] @ {result['ts']}  dry_run={result['dry_run']}")
    print(f"  scanned={result['scanned']}")
    print(f"  inserted={len(result['inserted'])}")
    print(f"  merged={len(result['merged'])}")
    print(f"  skipped_kind0={len(result['skipped_kind0'])}")
    print(f"  skipped_insufficient_n={len(result['skipped_insufficient_n'])}")
    print(f"  skipped_no_param={len(result['skipped_no_param'])}")
    print(f"  skipped_no_direction={len(result['skipped_no_direction'])}")
    if result["inserted"]:
        print()
        print("  --- INSERTED ---")
        for x in result["inserted"][:10]:
            if isinstance(x, dict) and "adj_id" in x:
                print(
                    f"    insight#{x['insight_id']} → adj#{x['adj_id']}: "
                    f"{x['asset']}/{x['action']}/{x['param_key']} {x['direction']} "
                    f"mag={x['magnitude']}"
                )
            elif isinstance(x, dict):
                print(
                    f"    insight#{x['insight_id']} → DRY: "
                    f"{x['asset']}/{x['action']}/{x['param_key']} {x['direction']} "
                    f"mag={x['magnitude']} (n={x['n']})"
                )
    if result["merged"]:
        print()
        print("  --- MERGED (累加到已有 pending) ---")
        for x in result["merged"][:10]:
            print(
                f"    insight#{x['insight_id']} → adj#{x['adj_id']}: "
                f"new_sample_size={x['new_sample_size']}"
            )
    if result["skipped_no_param"]:
        print()
        print("  --- SKIPPED no_param (前 5) ---")
        for iid, rule in result["skipped_no_param"][:5]:
            print(f"    insight#{iid}: {rule}")
    print()


def _daemon_loop(min_samples: int) -> None:
    print(
        f"[WEEKLY-INSIGHT-APPLY] daemon started | "
        f"interval={_WEEK_SECONDS}s | min_samples={min_samples}"
    )
    while True:
        try:
            conn = db.get_connection()
            try:
                result = apply_once(conn, min_samples=min_samples, dry_run=False)
            finally:
                conn.close()
            _print_summary(result)
        except Exception as e:
            print(f"[WEEKLY-INSIGHT-APPLY] loop error: {e!r}")
        time.sleep(_WEEK_SECONDS)


def main() -> None:
    ap = argparse.ArgumentParser(description="v190 #4 weekly_insight_apply")
    ap.add_argument("--daemon", action="store_true", help="7 天一次循环跑")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument(
        "--min-samples",
        type=int,
        default=int(
            getattr(config, "WEEKLY_INSIGHT_MIN_SAMPLES", _DEFAULT_MIN_SAMPLES)
            or _DEFAULT_MIN_SAMPLES
        ),
        help="active insight 累计 source_samples 门槛",
    )
    args = ap.parse_args()

    if args.daemon:
        _daemon_loop(min_samples=args.min_samples)
        return

    conn = db.get_connection()
    try:
        result = apply_once(conn, min_samples=args.min_samples, dry_run=args.dry_run)
    finally:
        conn.close()
    _print_summary(result)


if __name__ == "__main__":
    main()


