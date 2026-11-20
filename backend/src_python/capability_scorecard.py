#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capability_scorecard.py — 两层能力记分卡 (结构层 / 统计层)

为什么需要它
------------
`capability_acceptance.py` 回答的是「**有没有**、**断没断**」——这类判据是
有限的, 可以修到全绿。但把「**准不准**」也塞进同一张 PASS/FAIL 表, 就必然
永远红:

  · 准确率是**统计量**, 需要样本量; n<100 时 54.7% 和 60% 在统计上没区别,
    判 FAIL 只是把"样本不够"说成"能力不行";
  · 每次改口径, 历史样本被重算 → 分母归零 → 看起来"又退步了";
  · 页面 / 验收脚本 / 临时查询各算一套口径, 同一件事三个数,
    谁都能得出"绝大多数不达标"的结论 (实测: 事件方向命中率按"日格平均"
    算是 33.5%, 按"事件加权"算是 48.8% —— 差 15pp, 全是口径差不是能力差).

本模块把两件事彻底分开:

  结构层 (STRUCT)   —— 链路、表、调度、端点自洽、枚举合法。**必须全绿**,
                       复用 capability_acceptance 的结果, 不重复实现。
  统计层 (STAT)     —— 方向命中、权重判别力、归因确认率、真值覆盖率…
                       **只报「值 + 样本量 + 95% 区间 + 相对毕业线」**,
                       永不输出 FAIL。样本不足就写"样本不足", 不下结论。

三条防"假进步/假退步"的机制
----------------------------
1. **口径指纹**: 每个指标带 definition hash。口径一改, 旧对比自动作废并
   标注「口径变更」, 不允许拿新口径和旧数值比。
2. **快照增量**: 每次运行落一条 JSONL 快照, 报告显示 Δ值 与 Δn。
   "进步"用区间和样本增量证明, 不用颜色。
3. **毕业线**: 每个指标预先写死 target / gate / baseline, 达标需要
   「点估计过线 **且** 95% 区间下界高过基准」——不满足就是"接近达标",
   而不是 FAIL。

用法
----
    python3 capability_scorecard.py                  # 两层总览 (会自动跑结构层)
    python3 capability_scorecard.py --no-structure    # 只看统计层 (秒级)
    python3 capability_scorecard.py --json            # 机读
    python3 capability_scorecard.py --history 10      # 附最近 10 次快照趋势
    python3 capability_scorecard.py --reset-history   # 清空快照 (慎用)

退出码: 0 = 结构层无 FAIL; 1 = 结构层有 FAIL; 统计层**不影响**退出码。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent
DEFAULT_DB = Path(os.environ.get("TRIDENT_DB_PATH") or (BACKEND_DIR / "trident_event_bus.db"))
RUNTIME_DIR = BACKEND_DIR / "runtime" / "capability_scorecard"
HISTORY_PATH = RUNTIME_DIR / "history.jsonl"
ACCEPTANCE_JSON = RUNTIME_DIR / "acceptance.json"

# v490: 「判断能力 vs 市场」基准层。它把"命中率"换成三个必须一起看的数
# （裸命中 / 同批样本市场基线 / 平衡准确率 + 扣费净期望）。缺模块时整个
# 新指标降级为"样本不足"，不影响既有 20 项 —— 记分卡不能因为一个可选
# 依赖缺失就整张表失效。
try:
    import judgment_benchmark as _jb
except Exception:      # pragma: no cover - 仅用于极端部署环境
    _jb = None

# ── 统计层状态词 ──────────────────────────────────────────────────────────
ST_GRAD = "达标"
ST_NEAR = "接近达标"
ST_BELOW = "未达线"
ST_THIN = "样本不足"


# ─────────────────────────────────────────────────────────────────────────────
# 基础统计
# ─────────────────────────────────────────────────────────────────────────────
def wilson_interval(k: float, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score 95% 区间 (比例)。n=0 返回 (0,1)。"""
    if n <= 0:
        return (0.0, 1.0)
    p = max(0.0, min(1.0, float(k) / float(n)))
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * (max(p * (1 - p), 0.0) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def definition_hash(*parts: Any) -> str:
    """口径指纹: 只对**定义**做哈希, 不含数据。"""
    blob = "|".join(str(p) for p in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


@dataclass
class Metric:
    id: str
    name: str
    kind: str                 # ratio | spearman | share
    target: float             # 毕业线 (ratio/share 为百分数, spearman 为 ρ)
    gate: int                 # 样本门槛
    baseline: float           # 基准 (抛硬币=50, 无相关=0)
    higher_better: bool
    unit: str
    query: Callable[[sqlite3.Connection], Tuple[float, int]]
    definition: str
    note: str = ""
    # v398: 把「排除了多少」这类样本池事实写进 detail。
    # 静态 note 无法表达随库变化的条数，改由 detail_fn 现算。
    detail_fn: Optional[Callable[[sqlite3.Connection], str]] = None
    # v490: 比例类指标的区间默认由 Wilson 从 (k, n) 推; 但「平衡准确率」不是
    # 二项比例 (两类召回加权), Wilson 会给出偏差区间。需要真区间的指标挂 ci_fn,
    # 由它返回 (low, high) 百分数 (内部用 bootstrap, 固定种子可回归)。
    ci_fn: Optional[Callable[[sqlite3.Connection], Tuple[Optional[float], Optional[float]]]] = None


@dataclass
class MetricResult:
    id: str
    name: str
    kind: str
    value: Optional[float]
    n: int
    ci_low: Optional[float]
    ci_high: Optional[float]
    target: float
    gate: int
    baseline: float
    status: str
    unit: str
    definition: str
    detail: str
    delta: Optional[float] = None
    delta_n: Optional[int] = None
    definition_changed: bool = False
    history_n: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# 指标查询 (全部只读; 分母口径写死在这里, 不允许下游另算)
# ─────────────────────────────────────────────────────────────────────────────
def _q_dir_event_1d(conn: sqlite3.Connection) -> Tuple[float, int]:
    """事件方向命中(1日): 按**独立日格**加权, 一格 = (日期, 资产)。

    v334 口径重写。老口径按"事件数"加权, 实测 1343 个原子样本只落在
    **30 个 (日期×资产) 格**里 —— 单日几十条同题材新闻共享同一根 K 线,
    彼此高度相关。把 1200 条当独立样本算, 95% 置信区间会窄得离谱
    ([47.4, 53.0]), 那是虚假精度; 而且某一天的逆向行情能一次性翻转几百条。

    一格怎么判: 格内多数事件方向 vs 该格平均实际涨跌。
    实测: 原子加权 51.5% / 日格加权 46.7% —— 真实样本只有 30, 无方向优势。
    """
    row = conn.execute(
        """
        WITH cell AS (
          SELECT date(e.event_time, 'unixepoch') AS d, a.code AS asset,
                 SUM(CASE WHEN e.direction='bullish' THEN 1 ELSE 0 END) AS bull,
                 COUNT(*) AS n,
                 AVG(c.delta_pct) AS avgd
            FROM event_impact_curve c
            JOIN event_atom e ON e.id = c.event_atom_id
            JOIN dim_asset a ON a.id = e.asset_id
           WHERE c.window_label='1d' AND c.delta_pct IS NOT NULL
             AND e.direction IN ('bullish','bearish')
           GROUP BY 1, 2
        )
        SELECT COUNT(*),
               SUM(CASE WHEN (bull * 2 >= n AND avgd > 0)
                          OR (bull * 2 <  n AND avgd < 0) THEN 1 ELSE 0 END)
          FROM cell
        """
    ).fetchone()
    n = int(row[0] or 0)
    k = float(row[1] or 0)
    return (k, n)


def _q_open_direction(conn: sqlite3.Connection) -> Tuple[float, int]:
    """真实开仓方向命中: 只算 ENTRY 且已结算、方向明确的单子。"""
    row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN "
        "  (intended_action='BUY'  AND actual_outcome_pct>0) OR "
        "  (intended_action='SELL' AND actual_outcome_pct<0) THEN 1 ELSE 0 END) "
        "FROM trade_decisions "
        "WHERE decision_type='ENTRY' AND actual_outcome_pct IS NOT NULL "
        "  AND intended_action IN ('BUY','SELL')"
    ).fetchone()
    return (float(row[1] or 0), int(row[0] or 0))


def _q_causal_direction(conn: sqlite3.Connection) -> Tuple[float, int]:
    """因果链**方向**命中: 只统计方向可判的链。

    v342: 剔除 predicted_direction='BOTH'。这类链的预测是"若…则涨；若…则跌"，
    评估器按 |5日涨跌| ≥ mag_lo 就算"命中"，本质是**波动命中**而不是方向命中；
    混进来会把方向命中率从 47.8% 虚抬到 77.1%（实测旧纪元 2,975/5,096 条 BOTH）。
    含 BOTH 的宽口径单独作为 S3b 报，不再冒充方向能力。
    """
    row = conn.execute(
        "SELECT COUNT(*), SUM(direction_hit) FROM causal_chain_outcomes "
        "WHERE status IN ('HIT','HIT_WEAK','MISS') AND direction_hit IS NOT NULL "
        "AND COALESCE(predicted_direction,'') <> 'BOTH'"
    ).fetchone()
    return (float(row[1] or 0), int(row[0] or 0))


def _q_causal_any(conn: sqlite3.Connection) -> Tuple[float, int]:
    """因果链**波动**命中 (含 BOTH, 宽口径, 仅作对照)。"""
    row = conn.execute(
        "SELECT COUNT(*), SUM(direction_hit) FROM causal_chain_outcomes "
        "WHERE status IN ('HIT','HIT_WEAK','MISS') AND direction_hit IS NOT NULL"
    ).fetchone()
    return (float(row[1] or 0), int(row[0] or 0))


def _q_signal_weight(conn: sqlite3.Connection) -> Tuple[float, int]:
    """权重判别力 (Spearman ρ, 桶内超额口径)。复用验收脚本的唯一实现, 避免第二套口径。

    v335: 主口径从"混合超额"换成"桶内超额" —— 逐小时算 ρ 再按样本量加权。
    混合口径把平静时段 (±0.2%) 和剧烈时段 (±3%) 混在一起排秩, 小时之间的量级差
    会盖过同一小时内权重差异的效果 (实测 0.197 vs 桶内 0.288)。
    """
    try:
        import capability_acceptance as ca
        st = ca._weight_eval_stats(conn)
        rb = st.get("rho_bucket")
        nb = int(st.get("rho_bucket_n") or 0)
        if rb is not None and nb:
            return (float(rb), nb)
        n = int(st.get("ex_ante_n_excess") or 0)
        v = st.get("rho_excess")
        return (float(v) if v is not None else 0.0, n)
    except Exception:
        return (0.0, 0)


def _q_attr_confirm(conn: sqlite3.Connection) -> Tuple[float, int]:
    row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN ai_explanation LIKE '[确认%' THEN 1 ELSE 0 END) "
        "FROM event_move_attribution WHERE ai_called=1"
    ).fetchone()
    return (float(row[1] or 0), int(row[0] or 0))


def _q_attr_causal_share(conn: sqlite3.Connection) -> Tuple[float, int]:
    """归因的"真因果"占比: 新闻发生在异动前 (lag>=0) 且能对上事件。"""
    row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN time_lag_sec>=0 AND ev_news_id IS NOT NULL "
        "THEN 1 ELSE 0 END) FROM event_move_attribution"
    ).fetchone()
    return (float(row[1] or 0), int(row[0] or 0))


def _q_attr_coverage(conn: sqlite3.Connection) -> Tuple[float, int]:
    """异动被归因覆盖的比例 (没归因的异动 = 这条链是瞎的)。

    v330: 只用 `move_id` 关联会**低估**覆盖率 —— `backfill_critical_moves.py`
    回填时是"按 context 删旧行 + 重新插入", 同一根 K 线会拿到**新的自增 id**,
    于是历史归因行的 move_id 变成孤儿 (实测 176 行归因里 62 行指向已删 id,
    覆盖率被算成 114/615=18.5%, 真实是 183/615=29.8%)。
    归因表本来就存了 `asset` + `move_ts` 这两个**稳定键**, 所以按 (资产, 时间)
    兜底匹配, id 只作快路径。

    v334: 分母再收一次窗口。`critical_price_moves` 跨 244 天, 而 `news_events`
    只有 40 天存档 —— 40 天前的异动**永远不可能被归因** (没有新闻可对)。
    把它们留在分母里, 覆盖率必然虚低 (244 天全算 = 35.8%, 只算有新闻的时段 ≈ 49%)。
    分子分母必须用同一把尺子: 只统计新闻存档开始之后的异动。
    """
    row = conn.execute(
        """
        WITH win AS (
          SELECT (SELECT MIN(ts) FROM news_events) AS news_min
        )
        SELECT COUNT(*), SUM(CASE WHEN EXISTS(
                 SELECT 1 FROM event_move_attribution a
                  WHERE a.move_id = m.id
                     OR (a.asset = m.asset AND a.move_ts = m.ts)
               ) THEN 1 ELSE 0 END)
          FROM critical_price_moves m, win
         WHERE win.news_min IS NOT NULL AND m.ts >= win.news_min
        """
    ).fetchone()
    return (float(row[1] or 0), int(row[0] or 0))


# "资产桶"标签: 不是可交易的具体标的, 拿它下单也无法结算 (上游打标缺口)。
# 计入真值分母会让指标永远达不到目标 —— 但它们必须被**单独报出来**, 不能被悄悄忽略。
_BUCKET_ASSETS = ("", "NONE", "INDIVIDUAL_STOCK", "US_STOCK", "CN_STOCK", "HK_STOCK",
                  "AGGS", "AGGREGATE", "ALL", "个股", "美股", "A股")


def _q_truth_coverage(conn: sqlite3.Connection) -> Tuple[float, int]:
    """真值覆盖率 = 已判胜负的**可结算有方向决策** / 全部可结算有方向决策。

    v333: 分母从"全部 ai_decisions"改成"BUY/SELL 且标的是具体品种"。两个原因:
      ① 全表 8865 条里 8476 条是 HOLD —— HOLD 没押方向, 没有对错, 不可能有真值。
         拿全表当分母, 覆盖率上限只有 4.4% (389/8865), 90% 目标天生不可达,
         会把"指标口径错"伪装成"数据不够"。
      ② 389 条有方向里还有 34 条标的是 INDIVIDUAL_STOCK / NONE 这类"桶",
         不是可交易品种, 永远结算不了 —— 那是上游打标缺口, 不该算进分母,
         但也不能装作不存在 (scorecard 的 detail 会单独报)。
    真实答案: 355 条可结算有方向决策里已判 328 条。
    """
    row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN truth_verdict IS NOT NULL AND truth_verdict<>'' "
        "THEN 1 ELSE 0 END) FROM ai_decisions "
        f"WHERE {_live(conn)} AND suggested_action IN ('BUY','SELL') "
        f"AND upper(COALESCE(target_asset,'')) NOT IN "
        f"({','.join('?' * len(_BUCKET_ASSETS))})",
        _BUCKET_ASSETS,
    ).fetchone()
    return (float(row[1] or 0), int(row[0] or 0))


def _q_truth_bucket_gap(conn: sqlite3.Connection) -> Tuple[float, int]:
    """参考口径: 有方向但标的是"桶/空"的决策数 (永远无法结算的那批)。"""
    n = conn.execute(
        f"SELECT COUNT(*) FROM ai_decisions WHERE {_live(conn)} "
        f"AND suggested_action IN ('BUY','SELL') "
        f"AND upper(COALESCE(target_asset,'')) IN "
        f"({','.join('?' * len(_BUCKET_ASSETS))})",
        _BUCKET_ASSETS,
    ).fetchone()[0]
    return (float(n), n)


def _q_market_truth_coverage(conn: sqlite3.Connection) -> Tuple[float, int]:
    """参考口径: 决策后真实行情 (forward_move_pct) 覆盖了多少条决策。

    与上一条的区别: 这条对 HOLD 也成立 (HOLD 之后市场也会走), 用来衡量
    "我们有没有把每一条决策之后的市场走势都记下来", 不参与达标判定。
    """
    row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN forward_move_pct IS NOT NULL THEN 1 ELSE 0 END) "
        f"FROM ai_decisions WHERE {_live(conn)} AND target_asset IS NOT NULL "
        "AND target_asset NOT IN ('', 'NONE')"
    ).fetchone()
    return (float(row[1] or 0), int(row[0] or 0))


def _q_missed_settle(conn: sqlite3.Connection) -> Tuple[float, int]:
    row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN hit IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM missed_signals WHERE actual_outcome_5d IS NOT NULL"
    ).fetchone()
    return (float(row[1] or 0), int(row[0] or 0))


def _q_reflection_concentration(conn: sqlite3.Connection) -> Tuple[float, int]:
    """反思维度的**集中度**: 最大小类占比。越接近 100% 说明"细分"没落地。

    v398: 只统计 A/B 两类交易错因。C 类是技术故障（data_feed_timeout /
    signal_delay），与"交易错因细分"不是同一件事；实测 11-15 一次抓取失败
    批量写入 499 条 C 类，占 82.5%，把本指标顶到 88.2%。排除后分母只含
    真正需要细分能力的样本，被排除条数由 insight_pool.s10_detail 写进报告。
    """
    rows = conn.execute(
        "SELECT subcategory, COUNT(*) FROM trade_reflections_v2 "
        "WHERE dimension IN ('A','B') AND subcategory IS NOT NULL "
        "AND subcategory<>'' GROUP BY 1"
    ).fetchall()
    total = sum(r[1] for r in rows)
    top = max((r[1] for r in rows), default=0)
    return (float(top), int(total))


def _s10_detail(conn: sqlite3.Connection) -> str:
    """S10 的样本池说明：显式列出被排除的技术错误条数（v398）。"""
    try:
        import insight_pool as _ip
    except Exception:
        return ""
    try:
        return _ip.s10_detail(_ip.reflection_pool_stats(conn))
    except Exception:
        return ""


def _q_independent_direction(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S11: 去重口径的方向命中 —— 同一小时内同品种同方向只算一次。

    v336: 发现"全样本方向命中 17.2%"是分母造假：一次行情被重复计成 300+ 样本
    （11-05 有 305 条 BUY 只中 2 条、11-08 有 413 条 BUY 一条没中）。
    同一条新闻在同小时对同品种发 300 次同向建议，本质是 1 个独立信号。
    去重后 17.2% → 41.1%（n=73）。这是"交易信号准不准"唯一可比的口径。
    """
    try:
        import opt_metrics as _om
    except Exception:
        return (float("nan"), 0)
    rows = _om.load_trade_rows(conn)
    rep = _om.summarize(rows, "S11")
    # 注意: Metric.query 的约定是返回 (命中数, 样本数) 的**计数**,
    # 由 evaluate_metric 自己算百分比 (k/n*100)。返回百分比会被再除一次。
    return (float(rep["independent"]["k"]), int(rep["independent"]["n"]))


def _q_real_analysis_share(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S12: 决策里"真有 LLM 分析文本"的比例（兜底套话不算）。

    v336: 8865 条决策中 6627 条（74.8%）的 reasoning_path 是兜底模板 ——
    其中 6459 条写着"事件不直接命中核心监控品种"，而其中 3692 条标的是
    XAU/WTI/BTC/ETH 这类核心品种。也就是说"模型判断无方向"绝大多数其实是
    LLM 空输出。这条指标把"有没有真分析"与"准不准"分开度量。

    返回 (真分析条数, 总决策数) —— Metric.query 的契约是计数, 不是百分比。
    """
    fallback = ("reasoning_path LIKE '%核心监控品种%' "
                "OR reasoning_path LIKE '%LLM 未返回可用分析%' "
                "OR reasoning_path LIKE '%服务端兜底%'")
    try:
        row = conn.execute(
            f"SELECT SUM(CASE WHEN NOT ({fallback}) THEN 1 ELSE 0 END), COUNT(*) "
            f"FROM ai_decisions WHERE {_live(conn)} AND {_not_degraded(conn)}"
        ).fetchone()
    except sqlite3.Error:
        return (0.0, 0)
    return (float(int(row[0] or 0)), int(row[1] or 0))


def _q_evidence_headline_share(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S13: 归因里"标题级命中"的占比 —— 这才是原因，不是背景。

    v336: 242 行归因中标题主语=标的只有 84 条（34.7%），
    42.1% 连这个标的都没提到（宏观/汇总），13.2% 只在正文里出现
    （正文常夹带行情，实测出现「艾罗能源分红 → BTC -7.56%」这种组合）。

    返回 (headline 级条数, 总归因行数) —— Metric.query 契约是计数。
    """
    try:
        total = conn.execute("SELECT COUNT(*) FROM event_move_attribution").fetchone()[0]
        n = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution "
            "WHERE algo_reasons LIKE '%evidence=headline%'").fetchone()[0]
    except sqlite3.Error:
        return (0.0, 0)
    return (float(n or 0), int(total or 0))


def _q_weight_resolution(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S14: 权重的分辨率 —— 有多少条决策拥有**互不相同**的权重值。

    为什么单列一个指标（2026-11-14 查 S4 时发现）：
      决策时点 4529 条配对只有 663 个不同权重值（14.6%）。最粗的一组
      396 条决策权重完全相同（0.4521），而它们的实际波动从 0.00% 到 14.59%
      （中位 5.81%）—— 权重对这批决策**零区分力**，S4 的 ρ 被这些并列值稀释。
    根因不是公式，是输入粒度：base 只有 121 个取值、impact 173 个，
    组合后落库再 round 到 4 位，分辨率进一步塌陷。

    返回 (不同权重值个数, 配对总数) —— Metric.query 契约是计数。
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT h.signal_weight) d "
            "FROM signal_weight_history h JOIN ai_decisions a ON a.id=h.ai_decision_id "
            "WHERE h.trigger_kind='insert' AND h.decision_space='ai' "
            f"AND {_live(conn, 'a.')} "
            "AND a.forward_move_pct IS NOT NULL "
            # v399 P2: 无标的决策权重天然并列, 不进分辨率分母（否则 S14 被"没路由到品种"拉低）
            "AND upper(COALESCE(a.target_asset,'')) NOT IN ('NONE','')"
        ).fetchone()
        return (float(row[1] or 0), int(row[0] or 0))
    except sqlite3.Error:
        return (0.0, 0)


def _q_impact_real_share(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S15: 影响力维"真的打过分"的占比（impact_score 不等于默认 5.0）。

    2026-11-14 实测：8865 条决策里 8379 条（94.5%）impact_score 恒为 5.0 ——
    那是模型没返回该字段时的兜底值，不是"判断为中性"。这直接让 base_mult
    在 88.7% 的决策上是常数 0.550，权重分辨率 S14 只有 14.6%。
    已用门卫分（raw_news.gate_impact_1to100）回填 7263 条 → 12.5%。

    返回 (非默认条数, 总决策数) —— Metric.query 契约是计数。
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN ABS(COALESCE(impact_score,5.0)-5.0)>1e-9 "
            f"THEN 1 ELSE 0 END) k FROM ai_decisions WHERE {_live(conn)} "
            f"AND {_not_degraded(conn)}").fetchone()
        return (float(row[1] or 0), int(row[0] or 0))
    except sqlite3.Error:
        return (0.0, 0)


def _s17_detail(conn: sqlite3.Connection) -> str:
    """S17 的样本池说明：把"历史欠账"从分母里显式拆出来（v399）。

    98 条 A/B 反思里有 83 条来自 `analysis_degraded=1` 的决策 —— 那些决策在
    v398 之前就写下了反思（维度靠 ontology 兜底），而校验器对降级决策**按设计
    不跑**，所以它们永远不可能有错误码。不改口径（headline 仍按全部 A/B 算），
    只把这条结构性事实写进 detail，避免 headline 被误读成"精确归因失败"。
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, "
            "SUM(CASE WHEN EXISTS(SELECT 1 FROM decision_errors de "
            "        WHERE de.decision_id = r.decision_id) THEN 1 ELSE 0 END) k "
            "FROM trade_reflections_v2 r "
            "JOIN ai_decisions a ON a.id = r.decision_id "
            "WHERE r.dimension IN ('A','B') AND r.subcategory IS NOT NULL "
            "  AND r.subcategory<>'' AND COALESCE(a.analysis_degraded,0)=0"
        ).fetchone()
        n = int(row[0] or 0)
        k = int(row[1] or 0)
        pct = f"{k * 100.0 / n:.1f}%" if n else "—"
        base = conn.execute(
            "SELECT COUNT(*) FROM trade_reflections_v2 "
            "WHERE dimension IN ('A','B') AND subcategory IS NOT NULL "
            "  AND subcategory<>''"
        ).fetchone()[0]
        skipped = max(0, int(base or 0) - n)
        return (f"非降级池 {k}/{n} = {pct}；另有 {skipped} 条 A/B 反思来自降级决策，"
                f"校验器按设计不跑，结构上不可能有错误码")
    except sqlite3.Error:
        return ""


def _q_hard_data_share(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S20: 硬数据证据覆盖率（非降级决策里 hard_data_evidence 非空的比例）。

    v399 复测 v397：承诺"硬数据前置注入", 实测非降级池只有 44.2%。缺口集中在
    **个股与桶标签**（CN_STOCK / HK_STOCK / INDIVIDUAL_STOCK / US_STOCK 本地无行情源）
    与 XAU/WTI 的偶发断档 —— 由 `hard_data_status` 列精确到原因。
    """
    try:
        row = conn.execute(
            f"SELECT COUNT(*) n, SUM(CASE WHEN COALESCE(hard_data_evidence,'')<>'' "
            f"THEN 1 ELSE 0 END) k FROM ai_decisions "
            f"WHERE {_live(conn)} AND {_not_degraded(conn)}").fetchone()
        return (float(row[1] or 0), int(row[0] or 0))
    except sqlite3.Error:
        return (0.0, 0)


def _s20_detail(conn: sqlite3.Connection) -> str:
    try:
        cols = {c[1] for c in conn.execute("PRAGMA table_info(ai_decisions)")}
    except sqlite3.Error:
        return ""
    if "hard_data_status" not in cols:
        return "hard_data_status 未回填（旧库）"
    try:
        rows = conn.execute(
            f"SELECT COALESCE(NULLIF(hard_data_status,''),'(未标)') s, COUNT(*) n "
            f"FROM ai_decisions WHERE {_live(conn)} AND {_not_degraded(conn)} "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 5").fetchall()
        parts = " / ".join(f"{r[0]} {int(r[1])}" for r in rows) or "—"
        top = conn.execute(
            f"SELECT COALESCE(target_asset,'(空)') t, COUNT(*) n "
            f"FROM ai_decisions WHERE {_live(conn)} AND {_not_degraded(conn)} "
            "AND COALESCE(hard_data_evidence,'')='' "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 4").fetchall()
        lacks = " / ".join(f"{r[0]} {int(r[1])}" for r in top) or "—"
        return f"状态: {parts}；缺得最多的品种: {lacks}"
    except sqlite3.Error:
        return ""


def _q_no_target_share(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S18: 无标的（target_asset=NONE）决策占比 —— 越低越好。

    v399 P2: 142/504 = 28.2% 的决策没有标的。它们既不能结算（S8 已单独剔除），
    也让 S14 的权重分辨率天然并列 —— 留在分母里就是把"没路由到品种"算进
    "权重不够精细"。这里把占比单列，分子分母都用有效行（排除 quarantine）。
    """
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM ai_decisions WHERE {_live(conn)}").fetchone()[0]
        none_n = conn.execute(
            f"SELECT COUNT(*) FROM ai_decisions WHERE {_live(conn)} "
            "AND upper(COALESCE(target_asset,''))='NONE'").fetchone()[0]
        return (float(none_n or 0), int(total or 0))
    except sqlite3.Error:
        return (0.0, 0)


def _s18_detail(conn: sqlite3.Connection) -> str:
    """S18 的拆解：原因分布 + 路由字段缺失率（区分"真没标的"与"路由漏了"）。"""
    try:
        rows = conn.execute(
            f"SELECT COALESCE(analysis_degraded_reason,'') r, COUNT(*) n "
            f"FROM ai_decisions WHERE {_live(conn)} "
            "AND upper(COALESCE(target_asset,''))='NONE' "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 4").fetchall()
        parts = " / ".join(f"{(r[0] or '(无原因)')} {int(r[1])}" for r in rows) or "—"
        try:
            cols = {c[1] for c in conn.execute("PRAGMA table_info(ai_decisions)")}
        except sqlite3.Error:
            cols = set()
        if "asset_route" in cols:
            missing = conn.execute(
                f"SELECT COUNT(*) FROM ai_decisions WHERE {_live(conn)} "
                "AND upper(COALESCE(target_asset,''))='NONE' "
                "AND COALESCE(asset_route,'')=''").fetchone()[0]
            none_n = conn.execute(
                f"SELECT COUNT(*) FROM ai_decisions WHERE {_live(conn)} "
                "AND upper(COALESCE(target_asset,''))='NONE'").fetchone()[0]
            return (f"原因分布: {parts}；其中 asset_route 为空 {int(missing)}/{int(none_n)}"
                    f"（路由字段没写 = 分不清'真没有标的'与'路由漏了'）")
        return f"原因分布: {parts}"
    except sqlite3.Error:
        return ""


def _insight_hypothesis():
    """v409: 两种跑法下都能拿到 insight_hypothesis 模块。

    直接 `python3 capability_scorecard.py` 时它在 sys.path；以 `-m src_python.*`
    跑时不在 —— 以前这里被 try/except 吞掉，S19 静默变成 n=0。
    """
    try:
        import insight_hypothesis as _m
        return _m
    except ImportError:
        from src_python import insight_hypothesis as _m
        return _m


def _q_observe_only_share(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S19: 观察型洞察占比 —— 只能观察、无法落成可测假设的洞察占比（越低越好）。

    v399 P2: 洞察池里没有参数键 / 参数键单位不等价 / 没说预期方向的，一律标
    observe_only，不进晋升队列。它们的占比就是"洞察的可执行度"——
    低于 30% 才说明这个闭环真的在转。
    """
    try:
        _ih = _insight_hypothesis()
    except Exception:
        _ih = None
    if _ih is not None:
        try:
            s = _ih.pool_stats(conn)
            return (float(s.get("observe_only") or 0), int(s.get("active") or 0))
        except sqlite3.Error:
            pass
    # v409: 模块取不到（例如以 `-m src_python.capability_scorecard` 跑，顶层
    # `insight_hypothesis` 不在 sys.path）时**退回直接查库**。
    # 旧实现是 except 后 return (0, 0) —— 指标静默显示 n=0「样本不足」，
    # 看起来像"没有数据"，实际是"代码没读到"，这种假阴性比报错更贵。
    try:
        row = conn.execute(
            "SELECT SUM(COALESCE(observe_only,0)), COUNT(*) FROM review_insights "
            "WHERE COALESCE(active,0)=1 AND COALESCE(manual_deactivated,0)=0"
        ).fetchone()
        return (float(row[0] or 0), int(row[1] or 0))
    except sqlite3.Error:
        return (0.0, 0)


def _s19_detail(conn: sqlite3.Connection) -> str:
    try:
        import insight_hypothesis as _ih
        return _ih.observe_detail(_ih.pool_stats(conn))
    except Exception:
        return ""


def _q_error_code_coverage(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S17: 交易错因反思里，有多少条背后有**程序化错误码**（decision_errors）。

    v399: 错误码是"最精确那条归因路径"（E001a–E033）。此前全库只写了 23 条
    （4.7%），维度解析几乎永远回落到 ontology 兜底，于是 80% 挤在
    pattern_recognition 一个泛类。这条指标度量的是"精确归因真的跑起来了没有"。
    分母只取 A/B 两类，与技术故障 C 类无关。
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, "
            "SUM(CASE WHEN EXISTS(SELECT 1 FROM decision_errors de "
            "        WHERE de.decision_id = r.decision_id) THEN 1 ELSE 0 END) k "
            "FROM trade_reflections_v2 r "
            "WHERE r.dimension IN ('A','B') AND r.subcategory IS NOT NULL "
            "  AND r.subcategory<>''"
        ).fetchone()
        return (float(row[1] or 0), int(row[0] or 0))
    except sqlite3.Error:
        return (0.0, 0)


def _q_degraded_share(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S16: 降级路径占比（越低越好）。

    v398: 这条单列出来，是因为它决定了其余所有指标的解释力 ——
    89.5% 的决策走降级时，S12/S15 的低分不是"模型答得差"，而是"模型没被问"。
    分子分母都只统计有效行（quarantine 已排除）。
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN COALESCE(analysis_degraded,0)=1 "
            f"THEN 1 ELSE 0 END) k FROM ai_decisions "
            f"WHERE {_live(conn)} AND {_analysis_pool(conn)}").fetchone()
        return (float(row[1] or 0), int(row[0] or 0))
    except sqlite3.Error:
        return (0.0, 0)


# v336: 重复分析产出的复制品决策已打隔离标记
# （quarantine_reason='dup_reanalysis'）。同一份 LLM 输出被写 10 次时，
# 若不排除就会把 S11/S12/S14/S15 的分母全部撑大、把 S4 的秩相关稀释。
# 老库/极简测试表可能没有该列 → 探测后降级成 1=1，不让指标整体失效。
def _live(conn: "sqlite3.Connection", alias: str = "") -> str:
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")}
    except Exception:
        return "1=1"
    if "quarantined_at" not in cols:
        return "1=1"
    return f"COALESCE({alias}quarantined_at, 0) = 0"


# v398: 降级决策不产出真分析，本来就不该进 S12/S15 的分母 ——
# 否则「模型没被正常调用」会被当成「模型答得不好」，两个问题混成一个分数。
# 老库/极简测试表缺列 → 1=1，不拦。
def _not_degraded(conn: "sqlite3.Connection", alias: str = "") -> str:
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")}
    except Exception:
        return "1=1"
    if "analysis_degraded" not in cols:
        return "1=1"
    base = f"COALESCE({alias}analysis_degraded, 0) = 0"
    pool = _analysis_pool(conn, alias)
    return base if pool == "1=1" else f"({base} AND {pool})"


# v409: "无标的"不是"分析降级"，两者必须分池。
#   target_asset=NONE 的决策根本没有可分析对象（2026-11-17 实测 161/516），
#   它们把 S16 降级率顶到 87.2%，让"模型没被问"与"路由没打上"混成一件事。
#   decision_kind='routing_miss' 的行走 S18 单列，不进 S12/S15/S16/S20 的分母。
# 老库/极简测试表缺列 → 1=1，不拦（历史行由回填脚本补标）。
def _analysis_pool(conn: "sqlite3.Connection", alias: str = "") -> str:
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")}
    except Exception:
        return "1=1"
    if "decision_kind" not in cols:
        return "1=1"
    return f"COALESCE({alias}decision_kind, 'analysis') = 'analysis'"


# ─────────────────────────────────────────────────────────────────────────────
# v490: 判断力 vs 市场 —— 影子层三个口径
#
# 为什么不再拿"命中率"当结论: 2026-11-04→11-19 这段单边下跌里, 影子层 4h 命中
# 56.7%, 而"同一批样本、闭眼喊 SELL"的基线是 62.9% —— 裸命中率低于一个没有
# 判断力的策略。所以这里挂三个必须一起读的数:
#   S21 裸命中 (现状, 只作参考)      S22 平衡准确率(去偏置, 判"有没有判别力")
#   S23 扣费净期望 (bps/笔, 判"能不能赚钱")
# 三个都从 shadow_ledger 同一批样本出, 时间点/品种/持有期天然对齐,
# 不存在"基线窗口和系统窗口对不上"的近似误差。
# ─────────────────────────────────────────────────────────────────────────────
def _shadow_samples(conn: sqlite3.Connection):
    if _jb is None:
        return []
    try:
        return _jb.load_shadow_ledger(conn)
    except Exception:
        return []


def _q_shadow_hit(conn: sqlite3.Connection) -> Tuple[float, int]:
    s = _shadow_samples(conn)
    return (float(sum(x.hit for x in s)), len(s))


def _q_shadow_ba(conn: sqlite3.Connection) -> Tuple[float, int]:
    if _jb is None:
        return (0.0, 0)
    s = _shadow_samples(conn)
    ba = _jb.balanced_accuracy(s)
    if ba is None or not s:
        return (0.0, 0)
    # 用 (值×n, n) 表达比率, 复用 evaluate_metric 的比例渲染; 区间由 ci_fn 给真值
    return (ba / 100.0 * len(s), len(s))


def _q_shadow_ba_ci(conn: sqlite3.Connection) -> Tuple[Optional[float], Optional[float]]:
    if _jb is None:
        return (None, None)
    s = _shadow_samples(conn)
    lo, hi = _jb.ba_interval(s)
    return (lo, hi) if lo is not None else (None, None)


def _q_shadow_net(conn: sqlite3.Connection) -> Tuple[float, int]:
    if _jb is None:
        return (0.0, 0)
    s = _shadow_samples(conn)
    net = _jb.net_expectancy(s)["net_bps"]
    if net is None:
        return (0.0, 0)
    return (float(net), len(s))


def _q_shadow_net_ci(conn: sqlite3.Connection) -> Tuple[Optional[float], Optional[float]]:
    if _jb is None:
        return (None, None)
    s = _shadow_samples(conn)
    if not s:
        return (None, None)
    return _jb.expectancy_interval(s, _jb.DEFAULT_FEE_BPS)


def _s23_detail(conn: sqlite3.Connection) -> str:
    """把"毛期望 → 扣费 → 净"拆开, 免得净期望为负时不知道该怪成本还是怪判断。"""
    try:
        pack = _jb.net_expectancy(_shadow_samples(conn))
    except Exception:
        return ""
    if pack.get("gross_bps") is None:
        return ""
    return (f"毛 {pack['gross_bps']:+.1f}bps − 成本 {pack['fee_bps']:.0f}bps "
            f"= 净 {pack['net_bps']:+.1f}bps")


METRICS: List[Metric] = [
    Metric(
        id="S1", name="事件方向命中 (1日·独立日格)", kind="ratio",
        # v334: gate 200 → 60。老口径的分母是"事件数", 1343 条原子只对应 30 个
        # 独立日格; 按原子数设门槛等于让指标永远"达标有样本", 实际毫无统计意义。
        # 日格是每天每资产 1 格, 60 格 ≈ 两个月多资产样本。
        target=55.0, gate=60, baseline=50.0, higher_better=True, unit="%",
        query=_q_dir_event_1d,
        definition="按 (日期,资产) 独立格统计: 格内多数事件方向 vs 该格平均实际涨跌; "
                   "window=1d",
        note="v334 口径重写: 同一根 K 线被几十条同题材新闻共享, 原子数会虚增样本量 "
             "并给出假精度区间 (1200 条原子实际只有 30 个独立日格)。当前 30 格, "
             "且方向由 event_type×asset 的固定偏好表决定, 无逐条判断能力。",
    ),
    Metric(
        id="S2", name="真实开仓方向命中", kind="ratio",
        target=55.0, gate=100, baseline=50.0, higher_better=True, unit="%",
        query=_q_open_direction,
        definition="trade_decisions ENTRY, settled, intended_action∈{BUY,SELL}",
        note="样本 75 时任何结论都不稳健; gate=100 前只报区间",
    ),
    Metric(
        id="S3", name="因果链方向命中 (超额口径)", kind="ratio",
        target=60.0, gate=200, baseline=50.0, higher_better=True, unit="%",
        query=_q_causal_direction,
        definition="causal_chain_outcomes direction_hit, status∈{HIT,HIT_WEAK,MISS}",
        note="已剥离同期等权市场组合收益 (v322); v342 已剔除 BOTH 双向链, 只算真方向",
    ),
    Metric(
        id="S3b", name="因果链波动命中 (含双向, 对照)", kind="ratio",
        target=60.0, gate=200, baseline=50.0, higher_better=True, unit="%",
        query=_q_causal_any,
        definition="causal_chain_outcomes direction_hit, status∈{HIT,HIT_WEAK,MISS}, 含 BOTH",
        note="宽口径: BOTH 只要求幅度达标, 不代表方向判对; 与 S3 一起看落差即'虚高部分'",
    ),
    Metric(
        id="S4", name="权重判别力 (Spearman ρ·桶内超额)", kind="spearman",
        target=0.20, gate=200, baseline=0.0, higher_better=True, unit="ρ",
        query=_q_signal_weight,
        definition="逐小时 |forward_move - 当小时中位数| 的秩相关, 按样本量加权; "
                   "ex_ante weight, trigger_kind='insert'",
        note="ρ=0.2 只解释 4% 方差; 是'排序可用', 不是'幅度可预测'。"
             "v335 换口径: 混合口径 0.197 → 桶内 0.288 (小时之间量级差原先淹掉了权重信号)",
    ),
    Metric(
        id="S5", name="归因 AI 确认率", kind="ratio",
        target=30.0, gate=100, baseline=0.0, higher_better=True, unit="%",
        query=_q_attr_confirm,
        definition="ai_explanation LIKE '[确认%' / ai_called=1",
        note="当前 6.4%; 压窗到 ±60min 后这条应显著上升",
    ),
    Metric(
        id="S6", name="归因真因果占比 (事前)", kind="ratio",
        target=80.0, gate=200, baseline=50.0, higher_better=True, unit="%",
        query=_q_attr_causal_share,
        definition="time_lag_sec>=0 AND ev_news_id NOT NULL",
        note="事后拟合(lag<0) 不算因果, 但可以留作解释",
    ),
    Metric(
        id="S7", name="异动被归因覆盖率", kind="ratio",
        target=80.0, gate=200, baseline=50.0, higher_better=True, unit="%",
        query=_q_attr_coverage,
        definition="新闻存档时段内的异动中, 存在归因行的比例 (id 快路径 + (资产,时间) 兜底)",
        note="当前约五成; 覆盖不到一半时'归因页'只是半个真相。慢涨/慢跌是最难覆盖的形态",
    ),
    Metric(
        id="S8", name="快照真值覆盖率", kind="ratio",
        # v333: gate 500 → 300。全库"可结算有方向决策"总量只有 389 条,
        # 门槛比整个候选池还高 = 定义错误 (永远"样本不足"), 不是质量要求。
        target=90.0, gate=300, baseline=50.0, higher_better=True, unit="%",
        query=_q_truth_coverage,
        definition="可结算有方向决策 (BUY/SELL 且标的是具体品种) 中 truth_verdict 非空的比例",
        note="v333 口径修正: ① HOLD 不押方向, 无胜负可言, 不进分母 (老口径分母是全表, "
             "上限只有 4.4%, 90% 目标天生不可达); ② 标的是 NONE/US_STOCK 这类"
             "「桶」的有方向决策无法结算, 移出分母但单独计数 (当前 34 条, 上游打标缺口); "
             "③ gate 从 500 降到 300 —— 候选池总共 389 条, 门槛高于全体等于永远不达标。",
    ),
    Metric(
        id="S9", name="漏检命中判定覆盖率", kind="ratio",
        target=90.0, gate=100, baseline=50.0, higher_better=True, unit="%",
        query=_q_missed_settle,
        definition="missed_signals hit 非空 / actual_outcome_5d 非空",
        note="当前 2.5%; '漏得对不对'目前无判据",
    ),
    Metric(
        id="S10", name="反思维度细分集中度 (越低越好)", kind="share",
        target=40.0, gate=500, baseline=25.0, higher_better=False, unit="%",
        query=_q_reflection_concentration,
        definition="max(A/B 类 subcategory 占比) / A+B 类反思数; C 类技术错误排除",
        note="v398: 只统计 A/B 交易错因。C 类是技术故障(data_feed_timeout/signal_delay), "
             "11-15 一次抓取失败批量写入 499 条占 82.5%, 曾把本指标顶到 88.2%。"
             "排除后分母只含真正需要细分能力的样本; 排除条数见 detail。",
        detail_fn=_s10_detail,
    ),
    Metric(
        id="S11", name="独立信号方向命中 (去重口径)", kind="ratio",
        target=50.0, gate=60, baseline=50.0, higher_better=True, unit="%",
        query=_q_independent_direction,
        definition="trade_decisions settled; 按 (asset,direction,小时) 去重后再计命中率",
        note="v336: 老口径 17.2% 的分母把一次行情重复计了 300+ 次; 去重后 41.1% (n=73)。"
             "这条直接回答'交易信号准不准', 低于 50% 即低于抛硬币。",
    ),
    Metric(
        id="S12", name="实质分析覆盖率 (兜底套话不算)", kind="ratio",
        target=80.0, gate=300, baseline=50.0, higher_better=True, unit="%",
        query=_q_real_analysis_share,
        definition="ai_decisions（非隔离、非降级）中 reasoning_path 非兜底模板"
                   "（不含'核心监控品种'/'LLM 未返回可用分析'/'服务端兜底'）的比例",
        note="v336: 74.8% 的决策没有真分析文本; 其中 3692 条核心品种被兜底写成"
             "'不在监控范围' → 30.8% 的 HOLD 是空输出而非判断。"
             "v398: 降级决策(analysis_degraded=1)移出分母 —— 它们本来就没被正常调用,"
             "不该被算成'答得不好'; 降级率由 S16 单独度量。",
    ),
    Metric(
        id="S13", name="归因标题级命中占比 (证据分层)", kind="ratio",
        target=60.0, gate=100, baseline=50.0, higher_better=True, unit="%",
        query=_q_evidence_headline_share,
        definition="event_move_attribution 中 algo_reasons 含 evidence=headline 的比例",
        note="v336: 标题主语=标的 34.7%、跨品种 9.9%、只在正文出现 13.2%、"
             "标题根本没提到 42.1%。低于 60% 说明归因页上的'原因'多数只是背景，"
             "因果图谱会学错。分档标记不改任何筛选逻辑。",
    ),
    Metric(
        id="S14", name="权重分辨率 (互不相同的权重占比)", kind="ratio",
        target=50.0, gate=200, baseline=30.0, higher_better=True, unit="%",
        query=_q_weight_resolution,
        definition="signal_weight_history 决策时点配对中, distinct(signal_weight)/配对数",
        note="v336: 4529 条只有 663 个不同值 (14.6%); 最大一组 396 条同权重而"
             "实际波动 0%~14.6%。低于 50% 说明权重在多数决策上不携带信息, "
             "S4 的 ρ 会被并列值稀释 —— 先看分辨率, 再看相关系数。",
    ),
    Metric(
        id="S15", name="影响力维实质化占比 (impact_score≠5)", kind="ratio",
        target=80.0, gate=200, baseline=50.0, higher_better=True, unit="%",
        query=_q_impact_real_share,
        definition="ai_decisions（非隔离、非降级）中 impact_score 不等于默认值 5.0 的比例",
        note="v336: 原始 94.5% 的 impact_score 恒为 5.0（缺省兜底，不是中性判断），"
             "导致 88.7% 的决策 base_mult 恰为 0.550。用门卫分回填 7263 条后"
             "降到 12.5%。低于 80% 说明'影响力'这个维度名存实亡。"
             "v398: 降级决策移出分母(降级路径不产出影响力判断), 由 S16 单独度量。",
    ),
    Metric(
        id="S16", name="降级路径占比 (越低越好)", kind="share",
        target=20.0, gate=300, baseline=50.0, higher_better=False, unit="%",
        query=_q_degraded_share,
        definition="ai_decisions（非隔离、decision_kind='analysis'）中 "
                   "analysis_degraded=1 的比例；无标的决策走 S18 单列，不进本分母",
        note="v398: 442/494 = 89.5% 的决策走降级路径, 输出 99.4% HOLD。降级决策"
             "不写反思、不进 S12/S15 分母; 这条单列, 用来判断其余指标还有多少解释力。"
             "v409: 分母剔除 decision_kind='routing_miss'（无标的）—— 它们没有可分析对象,"
             "把'路由没打上'算进'模型没被问'会把两个问题混成一个分数。",
    ),
    Metric(
        id="S17", name="错误码归因覆盖率 (A/B 反思)", kind="ratio",
        target=60.0, gate=200, baseline=50.0, higher_better=True, unit="%",
        query=_q_error_code_coverage,
        definition="A/B 两类反思中, 该决策在 decision_errors 里有程序化错误码的比例"
                   "（E001a–E033 为最精确那条归因路径）",
        note="v399: 全库只写过 23 条错误码(4.7%), 维度解析只能回落 ontology 兜底,"
             "于是 80% 挤在 pattern_recognition。反思落库前即时补跑校验器后,"
             "这条指标度量'精确归因到底跑起来没有'。分母含历史遗留行, 故 detail"
             "单列非降级池的覆盖率, headline 低不等于精确归因失败。",
        detail_fn=_s17_detail,
    ),
    Metric(
        id="S18", name="无标的决策占比 (越低越好)", kind="share",
        target=10.0, gate=300, baseline=20.0, higher_better=False, unit="%",
        query=_q_no_target_share,
        definition="ai_decisions（有效行）中 target_asset='NONE' 的比例",
        note="v399 P2: 142/504 = 28.2% 的决策没有标的, 既不能结算, 也让权重分辨率"
             "天然并列。它不进 S14 分母(S14 已加 NONE 过滤), 这里单列, 用来盯"
             "'是不是路由/打标断了'。detail 拆原因分布 + asset_route 缺失率。",
        detail_fn=_s18_detail,
    ),
    Metric(
        id="S19", name="观察型洞察占比 (越低越好)", kind="share",
        target=30.0, gate=50, baseline=50.0, higher_better=False, unit="%",
        query=_q_observe_only_share,
        definition="review_insights（active）中 observe_only=1 的比例 —— 没有参数键 /"
                   "单位不等价 / 没声明预期方向的洞察, 不进晋升队列",
        note="v399 P2: 洞察必须先成为可测假设(参数键 + 预期方向 + 观测窗口)。"
             "判据复用 param_key_aliases(审批链路同一把尺子), 分类器在 insight_hypothesis.py。"
             "detail 单列可执行池的验证通过数。",
        detail_fn=_s19_detail,
    ),
    Metric(
        id="S20", name="硬数据证据覆盖率", kind="ratio",
        target=60.0, gate=300, baseline=50.0, higher_better=True, unit="%",
        query=_q_hard_data_share,
        definition="非降级决策中 hard_data_evidence 非空的比例（硬数据 = 事实, 不是模型输出）",
        note="v399 复测 v397: 承诺'硬数据前置注入', 非降级池实测 44.2% 未达 60%。"
             "缺口不是模型不写: 个股与桶标签(CN_STOCK/HK_STOCK/INDIVIDUAL_STOCK)"
             "靠 Yahoo 外部源(实测 429/连接重置, 熔断 90s), 本地无缓存; detail 按 hard_data_status 精确到原因。",
        detail_fn=_s20_detail,
    ),
    Metric(
        id="S21", name="影子层裸方向命中 (现状口径)", kind="ratio",
        target=55.0, gate=200, baseline=50.0, higher_better=True, unit="%",
        query=_q_shadow_hit,
        definition="shadow_ledger 已结算行: hit / n; 方向 = 闸门前的 ungated_action",
        note="v490: 只作参考。单边行情里它能被'无脑做空'白送, 必须和 S22 一起读 —— "
             "2026-11-04→11-19 实测 56.7%(n=61), 同期闭眼喊 SELL 的基线是 62.9%。",
    ),
    Metric(
        id="S22", name="影子层平衡准确率 (去偏置)", kind="ratio",
        target=55.0, gate=200, baseline=50.0, higher_better=True, unit="%",
        query=_q_shadow_ba, ci_fn=_q_shadow_ba_ci,
        definition="BA = (P(喊BUY|真涨) + P(喊SELL|真跌))/2, 按真实标签两类各自算召回; "
                   "shadow_ledger 已结算行; 区间用 bootstrap(固定种子)",
        note="v490: 唯一回答'有没有方向判别力'的数。多空喊得再偏都不影响它; "
             "只喊一个方向时它恒为 50 —— 这是正确定义(确实没有区分涨跌的能力), "
             "要区分'没信息'和'反着来'时看 judgment_benchmark 的 BA'。"
             "达标要求: 点估计 ≥55 且区间下界 >50。",
    ),
    Metric(
        id="S23", name="影子层净期望 (扣费 bps/笔)", kind="expectancy",
        target=0.0, gate=200, baseline=0.0, higher_better=True, unit="bps",
        query=_q_shadow_net, ci_fn=_q_shadow_net_ci, detail_fn=_s23_detail,
        definition="mean(direction_sign × move_pct)×100 − fee_bps(默认 12bps 往返); "
                   "shadow_ledger 已结算行",
        note="v490: 判对方向 ≠ 赚钱。方向对了但幅度不够覆盖手续费+滑点, 净期望照样为负。"
             "这条是'能不能赚钱'的最小判据; 成本假设见 detail, 换假设要改口径串。",
    ),
]



# ─────────────────────────────────────────────────────────────────────────────
# 参考口径（v337b）: 天花板与拆分 —— 只报值, 不参与达标判定
# ─────────────────────────────────────────────────────────────────────────────
# 背景见 docs/archive/口径天花板-论文-2026-11-14.md:
# 比率型指标的分母里若混入"定义上永远不可能满足分子"的样本, 该指标就存在
# 一个低于名义目标的上限。把 SC 显式打出来, 避免把"数据覆盖不足"误判成
# "实现质量不行" —— 也避免用改分母的方式把指标刷绿。
def _q_attr_ceiling(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S7 可用天花板 = 窗口内有同品种新闻的异动 / 窗口内全部异动。"""
    row = conn.execute(
        """
        WITH win AS (SELECT (SELECT MIN(ts) FROM news_events) AS news_min)
        SELECT COUNT(*),
               SUM(CASE WHEN EXISTS(
                     SELECT 1 FROM news_events n
                      WHERE n.asset = m.asset
                        AND n.ts BETWEEN m.ts - 86400 AND m.ts + 3600
                   ) THEN 1 ELSE 0 END)
          FROM critical_price_moves m, win
         WHERE win.news_min IS NOT NULL AND m.ts >= win.news_min
        """
    ).fetchone()
    return (float(row[1] or 0), int(row[0] or 0))


def _q_attr_on_satisfiable(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S7 在"可满足样本"上的真实表现 = 已归因 / 有同品种新闻的异动。"""
    row = conn.execute(
        """
        WITH win AS (SELECT (SELECT MIN(ts) FROM news_events) AS news_min)
        SELECT COUNT(*),
               SUM(CASE WHEN EXISTS(
                     SELECT 1 FROM event_move_attribution a
                      WHERE a.move_id = m.id
                         OR (a.asset = m.asset AND a.move_ts = m.ts)
                   ) THEN 1 ELSE 0 END)
          FROM critical_price_moves m, win
         WHERE win.news_min IS NOT NULL AND m.ts >= win.news_min
           AND EXISTS(
                 SELECT 1 FROM news_events n
                  WHERE n.asset = m.asset
                    AND n.ts BETWEEN m.ts - 86400 AND m.ts + 3600
               )
        """
    ).fetchone()
    return (float(row[1] or 0), int(row[0] or 0))


def _q_weight_resolution_with_asset(conn: sqlite3.Connection) -> Tuple[float, int]:
    """S14 拆分: 只算有标的决策的权重分辨率（无标的决策权重天然并列）。"""
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT h.signal_weight) d "
            "FROM signal_weight_history h JOIN ai_decisions a ON a.id=h.ai_decision_id "
            "WHERE h.trigger_kind='insert' AND h.decision_space='ai' "
            f"AND {_live(conn, 'a.')} "
            "AND a.forward_move_pct IS NOT NULL "
            "AND upper(COALESCE(a.target_asset,'')) NOT IN ('NONE','')"
        ).fetchone()
        return (float(row[1] or 0), int(row[0] or 0))
    except sqlite3.Error:
        return (0.0, 0)


def _q_causal_balanced(conn: sqlite3.Connection) -> Tuple[float, int]:
    """R4: S3 的去方向偏置口径（平衡准确率）。

    v409 归因（2026-11-17）：S3 的 49.2% 拆开是 UP 37.1%(n=824) / DOWN 62.6%(n=746)。
    样本窗口 10-11→11-09 全部 5 日前向收益均值都在 -2.2% 左右 —— 一个单边下跌的窗口，
    预测 DOWN 天然占便宜。把两个方向的命中率取平均，才看得出"有没有方向能力"，
    否则读到的只是"这段时间是不是在跌"。返回 (平衡命中率 × n, n)，让上层按同一套
    区间/门槛逻辑展示，不参与达标判定。
    """
    try:
        rows = conn.execute(
            "SELECT COALESCE(predicted_direction,'') d, COUNT(*) n, SUM(direction_hit) k "
            "FROM causal_chain_outcomes "
            "WHERE status IN ('HIT','HIT_WEAK','MISS') AND direction_hit IS NOT NULL "
            "AND COALESCE(predicted_direction,'') IN ('UP','DOWN') GROUP BY 1"
        ).fetchall()
    except sqlite3.Error:
        return (0.0, 0)
    rates = [float(r[2] or 0) / float(r[1]) for r in rows if int(r[1] or 0) > 0]
    if not rates:
        return (0.0, 0)
    bal = sum(rates) / len(rates)
    n = sum(int(r[1] or 0) for r in rows)
    return (bal * n, n)          # 用 (值×n, n) 表达比率，复用参考口径的百分数渲染


REFERENCE_METRICS: List[Tuple[str, str, Callable[[sqlite3.Connection], Tuple[float, int]]]] = [
    ("R1", "S7 可用天花板（有同品种新闻的异动占比）", _q_attr_ceiling),
    ("R2", "S7 可满足样本上的归因率", _q_attr_on_satisfiable),
    ("R3", "S14 有标的决策的权重分辨率", _q_weight_resolution_with_asset),
    ("R4", "S3 平衡准确率（去 UP/DOWN 偏置，看有无方向能力）", _q_causal_balanced),
]


def compute_reference(conn: sqlite3.Connection) -> List[dict]:
    out = []
    for rid, name, q in REFERENCE_METRICS:
        try:
            num, den = q(conn)
        except sqlite3.Error:
            num, den = 0.0, 0
        pct = (num / den * 100.0) if den else None
        out.append({"id": rid, "name": name, "pct": pct, "num": num, "den": den})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 计算与判定
# ─────────────────────────────────────────────────────────────────────────────
def judge_status(kind: str, value: Optional[float], n: int,
                 ci_low: Optional[float], target: float, gate: int,
                 baseline: float, higher_better: bool) -> str:
    """统计层唯一判定入口。四个状态, 没有 FAIL。

    · 样本不足: n < gate, 或值缺失 —— 不许下结论
    · 达标:     点估计过线 **且** 95% 区间下界高于基准 (higher_better)
    · 接近达标: 点估计过线, 但区间还压不住基准 —— 方向对了, 证据还不够
    · 未达线:   点估计没过线
    share 类 (越低越好) 不做区间判定。
    """
    if n < gate or value is None:
        return ST_THIN
    # v490: expectancy (bps/笔) 与 spearman 一样是"点估计过线即达标"的量,
    # 区间只用于说明证据强度, 不参与 PASS 判定。
    if kind in ("spearman", "expectancy"):
        return ST_GRAD if value >= target else ST_BELOW
    if higher_better:
        if value >= target and (ci_low is None or ci_low > baseline):
            return ST_GRAD
        return ST_NEAR if value >= target else ST_BELOW
    return ST_GRAD if value <= target else ST_BELOW


def evaluate_metric(conn: sqlite3.Connection, m: Metric) -> MetricResult:
    k, n = (0.0, 0)
    try:
        k, n = m.query(conn)
    except Exception:
        k, n = (0.0, 0)

    dh = definition_hash(m.id, m.kind, m.target, m.gate, m.baseline, m.definition)

    if m.kind == "ratio":
        value = (k / n * 100.0) if n else None
        ci = wilson_interval(k, n) if n else (None, None)
        ci_low = ci[0] * 100 if n else None
        ci_high = ci[1] * 100 if n else None
    elif m.kind == "share":
        value = (k / n * 100.0) if n else None
        # share 不是二项比例, 不给 Wilson 区间; 用"最大类占比"的粗区间代替
        ci_low, ci_high = None, None
    elif m.kind == "expectancy":
        # v490: 每笔净期望 (bps)。值可直接为负, 不能套比例渲染。
        value = k if n else None
        ci_low, ci_high = None, None
    else:  # spearman
        value = k if n else None
        ci_low, ci_high = None, None

    # v490: 需要真区间的指标（平衡准确率 / 净期望）用解析区间覆盖近似 Wilson。
    # 平衡准确率是两个独立比例的均值，Wilson 只认单个 (k,n)，会给偏窄的区间；
    # 净期望是连续量、可为负，更套不上比例区间 —— 所以这两项都交给 ci_fn 自己算。
    if n and m.ci_fn is not None:
        try:
            ci_low, ci_high = m.ci_fn(conn)
        except Exception:
            ci_low, ci_high = None, None

    # ── 状态判定: 永不 FAIL ──────────────────────────────────────────────
    status = judge_status(m.kind, value, n, ci_low, m.target, m.gate,
                          m.baseline, m.higher_better)

    detail = m.note
    if n and m.kind == "ratio":
        detail = f"{m.note} · 分母 n={n}"
    elif n:
        detail = f"{m.note} · n={n}"
    if m.detail_fn is not None:
        try:
            _extra = m.detail_fn(conn)
        except Exception:
            _extra = ""
        if _extra:
            detail = f"{detail} · {_extra}"

    return MetricResult(
        id=m.id, name=m.name, kind=m.kind, value=value, n=n,
        ci_low=ci_low, ci_high=ci_high, target=m.target, gate=m.gate,
        baseline=m.baseline, status=status, unit=m.unit,
        definition=dh, detail=detail,
    )


def load_history(path: Path = HISTORY_PATH) -> List[dict]:
    if not path.exists():
        return []
    out: List[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def last_snapshot(history: List[dict], metric_id: str) -> Optional[dict]:
    for rec in reversed(history):
        if rec.get("metric") == metric_id:
            return rec
    return None


def attach_deltas(results: List[MetricResult], history: List[dict]) -> None:
    for r in results:
        prev = last_snapshot(history, r.id)
        r.history_n = sum(1 for h in history if h.get("metric") == r.id)
        if not prev:
            continue
        r.definition_changed = prev.get("definition") != r.definition
        if r.definition_changed:
            continue  # 口径变了就不许比
        if prev.get("value") is not None and r.value is not None:
            r.delta = round(r.value - float(prev["value"]), 3)
        if prev.get("n") is not None:
            r.delta_n = int(r.n) - int(prev["n"])


def append_snapshot(results: List[MetricResult], path: Path = HISTORY_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            for r in results:
                rec = {"ts": ts, "metric": r.id, "name": r.name, "value": r.value,
                       "n": r.n, "ci_low": r.ci_low, "ci_high": r.ci_high,
                       "status": r.status, "definition": r.definition}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[WARN] 快照写入失败: {e}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# 结构层 (复用 capability_acceptance, 不重复实现)
# ─────────────────────────────────────────────────────────────────────────────
def run_structure(no_run: bool = False, out_path: Path = ACCEPTANCE_JSON,
                  no_http: bool = False) -> Optional[dict]:
    """跑结构层验收。

    ⚠️ 默认**带 HTTP**: 第 14/15 项的判据依赖 bridge 端点 (资金费率/订单簿/实时台),
    只用 --no-http 会把"服务通道没起"误判成"能力缺失" (实测 FAIL 一项)。
    """
    if not no_run:
        cmd = [sys.executable, str(HERE / "capability_acceptance.py"),
               "--json-out", str(out_path)]
        if no_http:
            cmd.append("--no-http")
        try:
            subprocess.run(cmd, cwd=str(HERE), timeout=300,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception as e:
            print(f"[WARN] 结构层执行失败: {e}", file=sys.stderr)
    if not out_path.exists():
        return None
    try:
        return json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def structure_verdict(payload: Optional[dict]) -> Tuple[str, dict]:
    if not payload:
        return ("结构层: 无结果 (先跑 capability_acceptance.py)", {})
    s = payload.get("summary", {}) or {}
    fails = [r for r in payload.get("results", []) if r.get("status") in ("FAIL", "ERROR")]
    return ("", {"summary": s, "fails": fails, "generated_at": payload.get("generated_at")})


# ─────────────────────────────────────────────────────────────────────────────
# 输出
# ─────────────────────────────────────────────────────────────────────────────
def _fmt_val(r: MetricResult) -> str:
    if r.value is None:
        return "—"
    if r.kind == "spearman":
        return f"{r.value:+.3f}"
    if r.kind == "expectancy":
        return f"{r.value:+.1f}{r.unit}"
    return f"{r.value:.1f}{r.unit}"


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.1f}%"


def _pt(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:+.1f}pt"


def _ci(ci: Sequence[Optional[float]]) -> str:
    if not ci or ci[0] is None or ci[1] is None:
        return "—"
    return f"[{ci[0]:.1f}, {ci[1]:.1f}]"


def _bps(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:+.1f}bps"


def _fmt_ci(r: MetricResult) -> str:
    if r.ci_low is None or r.ci_high is None:
        return "—"
    return f"[{r.ci_low:.1f}, {r.ci_high:.1f}]"


def _fmt_delta(r: MetricResult) -> str:
    if r.definition_changed:
        return "口径变更"
    if r.delta is None:
        return "首次"
    sign = "+" if r.delta >= 0 else ""
    return f"{sign}{r.delta:.1f}{r.unit} (n{r.delta_n:+d})"


def print_report(results: List[MetricResult], struct: Tuple[str, dict],
                 history: List[dict], show_history: int = 0,
                 refs: Optional[List[dict]] = None,
                 market: Optional[dict] = None) -> int:
    msg, info = struct
    print("=" * 104)
    print(f"能力记分卡 · 两层分离 · {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 104)

    # ── 结构层 ────────────────────────────────────────────────────────────
    print("\n【结构层 · 必须全绿】")
    if msg:
        print(f"  {msg}")
        s_fail = 1
    else:
        s = info["summary"]
        print(f"  上次结构验收: {info['generated_at']} · "
              f"PASS={s.get('PASS', 0)} / FAIL={s.get('FAIL', 0)} / WARN={s.get('WARN', 0)} "
              f"/ ERROR={s.get('ERROR', 0)} / 总 {s.get('total', 0)}")
        if info["fails"]:
            print("  未通过项:")
            for f in info["fails"]:
                print(f"    ✗ {f.get('id')} {f.get('name')} [{f.get('status')}] — {f.get('measured')}")
            s_fail = 1
        else:
            print("  ✓ 链路 / 表 / 调度 / 端点 / 枚举 全部对齐")
            s_fail = 0

    # ── 统计层 ────────────────────────────────────────────────────────────
    print("\n【统计层 · 只报值+区间+样本量, 不判 FAIL】")
    print(f"  {'':3}{'指标':28}{'当前':>9}  {'95%区间':>15}  {'毕业线':>8}  {'n':>6}  {'较上次':>16}  状态")
    print("  " + "-" * 100)
    for r in results:
        tgt = f"{r.target:g}{r.unit}" if r.kind != "spearman" else f"ρ{r.target:g}"
        print(f"  {r.id:3}{r.name:28}{_fmt_val(r):>9}  {_fmt_ci(r):>15}  {tgt:>8}  {r.n:>6}  {_fmt_delta(r):>16}  {r.status}")
        print(f"      {r.detail}")

    grad = [r.id for r in results if r.status == ST_GRAD]
    near = [r.id for r in results if r.status == ST_NEAR]
    below = [r.id for r in results if r.status == ST_BELOW]
    thin = [r.id for r in results if r.status == ST_THIN]
    print("\n  " + "-" * 100)
    if refs:
        print("\n【参考口径 · 只报值, 不参与达标判定】")
        print("  " + "-" * 100)
        for r in refs:
            if r.get("pct") is None:
                print(f"  {r['id']}  {r['name']:34}  —（无可满足样本）")
            else:
                print(f"  {r['id']}  {r['name']:34}  {r['pct']:6.1f}%"
                      f"   ({int(r['num'])}/{int(r['den'])})")
        print("  说明: 若『可用天花板』低于该指标毕业线, 该项在现有数据下不可达 ——")
        print("        这是口径/数据覆盖问题, 不是实现质量问题 (见 docs/口径天花板-论文)。")

    # ── v490: 判断力 vs 市场（三个数必须一起读，否则一定误判）──────────────
    if market:
        print("\n【判断力 vs 市场 · 同批样本对齐口径】")
        print("  " + "-" * 100)
        b = market["baseline"]
        print(f"  裸命中 {_pct(market['hit_rate'])}   "
              f"同配比盲选基线 {_pct(b['mix_implied'])}   "
              f"无脑做空基线 {_pct(b['always_sell'])}   "
              f"→ 择时超额 {_pt(market['excess_vs_mix'])}")
        print(f"  平衡准确率(去偏置) {_pct(market['ba_label']['value'])} "
              f"区间 {_ci(market['ba_label']['ci'])}   "
              f"净期望 {_bps(market['expectancy']['net_bps'])}/笔 (fee {market['fee_bps']:.0f}bps)")
        tp = market.get("throughput") or {}
        if tp.get("directional_per_hour") is not None:
            eta = tp.get("eta_days")
            eta_txt = "—（无池内方向决策，永远到不了）" if eta is None else f"{eta:.0f} 天"
            print(f"  产能 {tp['decisions']} 条/24h 中带方向 {tp['directional']}、池内可结算 "
                  f"{tp.get('settleable', 0)}（{tp.get('settleable_per_hour', 0):.1f}/小时）；"
                  f"已结算 {tp.get('settled_total') or 0}/{market['gate']}，距可判定约 {eta_txt}")
        v = market["verdict"]
        print(f"  结论: {v['headline']}")
        print(f"        {v['detail']}")

    print(f"  达标 {len(grad)} 项: {', '.join(grad) or '—'}")
    print(f"  接近达标 {len(near)} 项: {', '.join(near) or '—'}")
    print(f"  未达线 {len(below)} 项: {', '.join(below) or '—'}")
    print(f"  样本不足 {len(thin)} 项: {', '.join(thin) or '—'}")
    print("  提示: '样本不足' 不是失败, 是还没攒够证据; 只有 n ≥ gate 才允许下结论。")

    # ── 趋势 ──────────────────────────────────────────────────────────────
    if show_history > 0 and history:
        print(f"\n【最近 {show_history} 次快照 (同一口径)】")
        for r in results:
            recs = [h for h in history if h.get("metric") == r.id][-show_history:]
            if not recs:
                continue
            seq = " → ".join(
                f"{h.get('value'):.1f}({h.get('n')})" if h.get("value") is not None else f"—({h.get('n')})"
                for h in recs
            )
            print(f"  {r.id:3} {r.name:26} {seq}")

    print("\n" + "=" * 104)
    print("结论: 结构层决定'能不能用'; 统计层决定'准到什么程度'。后者永远有区间, 不会全绿。")
    print("=" * 104)
    return s_fail


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="两层能力记分卡 (只读)")
    ap.add_argument("--db", default="", help="DB 路径 (默认生产库, 只读)")
    ap.add_argument("--no-structure", action="store_true", help="不跑结构层, 只看统计层")
    ap.add_argument("--no-acceptance", action="store_true", help="不重跑验收, 复用上次 JSON")
    ap.add_argument("--structure-no-http", action="store_true",
                    help="结构层只查 DB (快, 但第14/15项会假 FAIL)")
    ap.add_argument("--json", action="store_true", help="机读输出")
    ap.add_argument("--history", type=int, default=0, help="附最近 N 次快照")
    ap.add_argument("--reset-history", action="store_true", help="清空快照历史")
    ap.add_argument("--no-snapshot", action="store_true", help="本次不写快照")
    ap.add_argument("--market-fixture", default="",
                    choices=["", "coin", "bias", "skill", "anti"],
                    help="v490: 用合成样本渲染『判断力 vs 市场』区块（不碰数据库, 只看格式/口径）")
    args = ap.parse_args(argv)

    db = Path(args.db).expanduser().resolve() if args.db else DEFAULT_DB
    if not db.exists():
        print(f"[环境错误] 找不到数据库: {db}", file=sys.stderr)
        return 2

    if args.reset_history and HISTORY_PATH.exists():
        HISTORY_PATH.unlink()
        print("[OK] 已清空快照历史")

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    # capability_acceptance._weight_eval_stats 按列名取 row["ts"], 必须 Row 工厂
    conn.row_factory = sqlite3.Row
    market: Optional[dict] = None
    try:
        results = [evaluate_metric(conn, m) for m in METRICS]
        refs = compute_reference(conn)
        # v490: 判断力 vs 市场 的整包（含基线 / 平衡准确率 / 净期望 / 一句话结论）
        if _jb is not None:
            try:
                market = _jb.benchmark(_shadow_samples(conn), fee_bps=_jb.DEFAULT_FEE_BPS)
                market["throughput"] = _jb.throughput(conn, gate=_jb.GATE)
                market["verdict"] = _jb.judge(market, gate=_jb.GATE)   # 产能会改写结论措辞
            except Exception:
                market = None
    finally:
        conn.close()

    # 合成样本只在"看格式/看口径"时用：明确标注 source, 绝不冒充真实成绩
    if args.market_fixture and _jb is not None:
        market = _jb.benchmark(_jb.make_fixture(args.market_fixture), fee_bps=_jb.DEFAULT_FEE_BPS)
        market["source"] = f"fixture:{args.market_fixture}"
    elif market is not None:
        market["source"] = "shadow_ledger"

    history = load_history()
    attach_deltas(results, history)

    struct = ("结构层已跳过 (--no-structure)", {}) if args.no_structure else \
        structure_verdict(run_structure(no_run=args.no_acceptance,
                                        no_http=args.structure_no_http))

    if not args.no_snapshot:
        append_snapshot(results)

    if args.json:
        print(json.dumps({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "structure": {"message": struct[0], **struct[1]},
            "metrics": [asdict(r) for r in results],
            "reference": refs,
            "market": market,
        }, ensure_ascii=False, indent=2))
        return 0 if (args.no_structure or struct[1].get("summary", {}).get("FAIL", 0) == 0) else 1

    return print_report(results, struct, history, args.history, refs=refs, market=market)


if __name__ == "__main__":
    sys.exit(main())
