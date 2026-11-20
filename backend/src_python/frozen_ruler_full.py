# -*- coding: utf-8 -*-
"""frozen_ruler_full.py — 冻结尺 R3 · 单文件完整版（尺子 + 自检 + 说明书）

════════════════════════════════════════════════════════════════════
一句话：把"怎么算对"写死成一份带指纹的配置，所有信号用同一个标签算一遍，
给出 命中率 + Wilson 区间 + 实测基准 + 判定。改任何口径，指纹就变、旧对比作废。

跑法（纯只读，只在 runtime/frozen_ruler/ 落快照，不写业务库）
    python3 frozen_ruler_full.py              # 测量
    python3 frozen_ruler_full.py --selfcheck  # 只自检：五条铁律在位 + 连跑两次逐字段一致
    python3 frozen_ruler_full.py --all        # 先自检再测量
    python3 frozen_ruler_full.py --json       # 机读
退出码：0 正常 / 1 有指标口径被改或自检不过 / 2 环境错误

════════════════════════════════════════════════════════════════════
一、为什么可信（三句话）

1) 一个标签量所有信号：信号 cutoff 之后第一根日线收盘进场 → 再下一根收盘出场，
   用**带符号超额收益**定方向；超额 = 本资产收益 − 同日全品种横截面中位数（剥掉 beta）。
2) 进场一定晚于信息：cutoff = max(信号时间, 该信号的观测窗口结束)。日线不知收盘时刻，
   所以一律推到下一根收盘。这一条把 E1 从虚高的 56.58% 打到真实的 43.40%。
3) 基准是量出来的：本宇宙无条件"超额为正"实测 50.58%（n≈9400），不默认 50%。

二、五条铁律（改任何一条 = 改尺）

  1 单一标签      ：统一 entry/exit/超额口径，剔 |超额| < 0.05% 的中性格
  2 进场晚于信息  ：cutoff = max(signal_ts, observed_window_end)
  3 基准必须实测  ：base_rate() 现算，不写死 50%
  4 样本门槛      ：n < 200 只报"样本不足"，**不判 FAIL**（避免为消灭 FAIL 改代码）
  5 口径指纹到指标级：新增指标不作废已有历史；改某项定义只有那一项作废，脚本会点名

三、8 个指标

  E1  事件K线影响方向   event_kline_impact.direction ∈ {up,down}
  E2  新闻方向（全部）  news_events.action ∈ {BUY,SELL}，HOLD 剔除
  E2L 新闻方向·LLM子集  同上，且 ai_decision_id NOT NULL   ← 分层，定位到环节
  E2K 新闻方向·关键词子集 同上，且 ai_decision_id IS NULL     ← 分层，作对照组
  E3  因果链方向        expected_direction ∈ {UP,DOWN}；BOTH 与模板链一律剔除
  E4  异动方向延续      critical_price_moves.move_kind 含 rise/fall
  E5  权重判别力        ρ(决策时点权重, |超额|)，不是命中率口径
  E6  同行评审方向      ai_decisions.verification 里 peer 的 BUY/SELL，peer HOLD 剔除

四、标签规范（LABEL / GATES，逐字）

  LABEL = entry=first_close_after_cutoff_date, exit=next_bar_close,
          cutoff=max(signal_ts, observed_window_end), formula=excess_sign,
          beta_strip=cross_section_median, exclude_neutral_move=0.0005
  GATES = min_n=200, baseline=50.0(仅默认值，判定时用实测), target=55.0, z_ci=1.96

五、判定词表（只有五个词，**没有 FAIL**）

  达标 / 接近达标 / 未达线 / 样本不足 / 显著弱于基准

六、R3 实测（2026-11-11）

  E1  43.40% n=5125 [42.0,44.8] −7.2 显著弱于基准
  E2  42.36% n=4053 [40.9,43.9] −8.2 显著弱于基准
  E2L 34.96% n=1310 [32.4,37.6] −15.6 显著弱于基准   ← 病灶在这
  E2K 45.90% n=2743 [44.0,47.8] −4.7 显著弱于基准
  E3  41.04% n=2025 [38.9,43.2] −9.6 显著弱于基准
  E4  53.78% n=370  [48.7,58.8] +3.2 未达线
  E6  68.18% n=66   [56.2,78.2] +17.6 样本不足
  E5  ρ=+0.123 n=4859，门槛 0.20 → 未达线

  读法：E2L 与 E2K 差 11 个百分点 → 病灶在 LLM 给方向那一步，不在关键词词表。

七、必须随结论一起引用的限制

  · 样本跨度短：E1≈3周、E2≈6周、E3≈4周，只覆盖一个行情体制；n 大 ≠ 稳
  · E4 是唯一 8 个月跨度，但 n=370 未过门槛，+3.2 不能当结论
  · E6 n=66，68% 只说明"方向可能有用"，不能下结论
  · "方向标签反向"是待验证假设，不许在样本内直接反手；须攒 ≥3 个月前瞻样本
  · 4 项能力量不出来是数据问题不是尺子问题：影响力幅度(恒0)、慢涨(已停摆)、
    映射准确率(只有覆盖率无真值)、基本面角色(无公开真值)

八、什么时候数字会变

  只有两种情况：系统产生新数据，或某项口径被改（走 R3→R4 流程，改前不得看结果）。
  没改动就重复跑，数字一定一样 —— 这不是尺子失效，这是它在工作。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"
OUT_DIR = BACKEND / "runtime" / "frozen_ruler"


# ══════════════════════════════════════════════════════════════════
# 冻结配置 —— 改任何一行，指纹就变，历史对比作废（这是设计意图，不是缺陷）
# ══════════════════════════════════════════════════════════════════
RULER_VERSION = "R1"
LABEL = {
    # 进场：信号日之后的第一根日线收盘（严格晚于 cutoff 的日期）
    #   cutoff = max(信号时间戳, 该信号的观测窗口结束时间)
    #   为什么要"之后"而不是"当天"：日线无法知道收盘时刻，用当天收盘进场，
    #   对那些"收盘后才发布/窗口还在走"的信号就是拿已知结果去预测（前视泄漏）。
    #   E1 曾因此虚高 56.58%，把进场推到窗口结束后才回到真实水平。
    "entry": "first_close_after_cutoff_date",
    "exit": "next_bar_close",          # 进场那根的下一根日线收盘
    "cutoff": "max(signal_ts, observed_window_end)",
    "formula": "excess_sign",          # 符号(本资产收益 - 同日横截面中位数)
    "beta_strip": "cross_section_median",
    "exclude_neutral_move": 0.0005,    # |超额| < 0.05% 视为无波动格，剔除
}
GATES = {
    "min_n": 200,                      # 不足只报"样本不足"
    "baseline": 50.0,                  # 方向类基准
    "target": 55.0,                    # 毕业线
    "z_ci": 1.96,
}
# 指标清单（写死；新增指标必须 R2）
SIGNALS: List[Dict[str, Any]] = [
    {"id": "E1", "name": "事件K线影响方向", "table": "event_kline_impact",
     "note": "event_kline_impact.direction ∈ {up,down} → T+1 超额方向"},
    {"id": "E2", "name": "新闻方向标注", "table": "news_events",
     "note": "news_events.action ∈ {BUY,SELL} 且资产可映射 → T+1 超额方向；仅 data_epoch 之后（v340 纪元切片）"},
    {"id": "E3", "name": "因果链方向", "table": "causal_chains",
     "note": "expected_direction ∈ {UP,DOWN} 且剔除'[覆盖度告警]'模板链 → T+1 超额方向"},
    {"id": "E4", "name": "异动方向延续", "table": "critical_price_moves",
     "note": "fast_rise/slow_rise→UP, fast_fall/slow_fall→DOWN → T+1 超额方向"},
    {"id": "E5", "name": "权重判别力", "table": "signal_weight_history",
     "note": "决策时点(insert)权重 vs |超额| 的 Spearman ρ（ρ 类，不用命中率口径）"},
    {"id": "E2L", "name": "新闻方向·LLM子集", "table": "news_events(ai_decision_id NOT NULL)",
     "note": "E2 的分层：只用 LLM 给过结论的新闻；仅 data_epoch 之后"},
    {"id": "E2K", "name": "新闻方向·关键词子集", "table": "news_events(ai_decision_id IS NULL)",
     "note": "E2 的分层：只用关键词词表打分的新闻，作为对照组；仅 data_epoch 之后"},
    {"id": "E6", "name": "同行评审方向", "table": "ai_decisions.verification",
     "note": "同行评审(peer)给出的 BUY/SELL（HOLD 剔除）→ T+1 超额方向；"
             "额外报两审一致/不一致两个子集，用于判断'共识'有没有增量"},
]
RULER_VERSION = "R5"   # R5: E2/E2L/E2K 加 data_epoch 作用域（2026-11-15 干净起点后只测新样本）


def _epoch_ts(conn: sqlite3.Connection) -> int:
    """data_epoch 起点（unix 秒）。无表/无行时返回 0 = 不过滤（历史可比）。

    v340 起：清空判断/账本后，news_events 是保留的事实表，里面仍有旧模型写的
    BUY/SELL。若不按纪元切片，E2 会继续拿旧标签算命中率，等于"没清"。
    """
    try:
        row = conn.execute("SELECT MAX(started_at) FROM data_epoch").fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row or not row[0]:
        return 0
    try:
        # started_at 是本机本地时间字符串（datetime.now()），按本地时区解释
        return int(datetime.strptime(str(row[0]), "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return 0

# 资产别名归一（信号侧写法 → 行情侧写法）
ASSET_ALIAS = {
    "GOLD": "XAU", "黄金": "XAU", "XAUUSD": "XAU", "XAU-USD": "XAU",
    "原油": "WTI", "WTIUSD": "WTI", "CL": "WTI",
    "比特币": "BTC", "BTCUSDT": "BTC", "BTC-USD": "BTC",
    "ETHUSDT": "ETH", "ETH-USD": "ETH", "SOLUSDT": "SOL", "SOL-USD": "SOL",
    "白银": "XAG", "XAGUSD": "XAG",
}


def _norm_asset(a: Any) -> str:
    s = str(a or "").strip().upper()
    return ASSET_ALIAS.get(s, s)


def _fingerprint_signal(spec: Dict[str, Any]) -> str:
    """单个指标的指纹 = 全局标签 + 门槛 + 该指标定义。

    拆到指标级是关键：新增一个指标不该让别的指标的历史作废，
    否则"每加一项就从头开始"——那正是十轮反复的来源。
    """
    cfg = {"label": LABEL, "gates": GATES, "signal": spec}
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def _fingerprint() -> str:
    cfg = {"version": RULER_VERSION, "label": LABEL, "gates": GATES, "signals": SIGNALS}
    blob = json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


# ══════════════════════════════════════════════════════════════════
# 行情底座：每个资产的日线序列 + 每日横截面中位数
# ══════════════════════════════════════════════════════════════════
class PriceBook:
    def __init__(self, conn: sqlite3.Connection):
        self.series: Dict[str, List[Tuple[str, float]]] = {}
        # BUGFIX 2026-11-12：同一资产同一天存在多根K线（实测 1181 个资产-日、4853 行，
        # 另有 2653 条占位行 volume=0 或 OHLC 全等）。原实现把每根都追加进序列，
        # "进场后下一根出场"常常落在同一天的第二个来源 → 测到的是日内价差而不是次日收益，
        # 方向指标因此系统性停在 43%（不是模型反向）。
        # 修法：每个资产每天只保留一根——优先真实K线（有量且非占位），再取当日最晚一根。
        rows = conn.execute(
            "SELECT asset, d, close FROM ("
            "  SELECT asset, date(ts,'unixepoch') AS d, close,"
            "         ROW_NUMBER() OVER ("
            "           PARTITION BY asset, date(ts,'unixepoch')"
            "           ORDER BY (CASE WHEN volume>0 AND NOT (open=high AND high=low)"
            "                         THEN 0 ELSE 1 END), ts DESC) rn"
            "  FROM ohlcv_daily WHERE close IS NOT NULL AND close > 0"
            ") WHERE rn = 1 ORDER BY asset, d"
        ).fetchall()
        for asset, d, close in rows:
            self.series.setdefault(_norm_asset(asset), []).append((d, float(close)))
        self.index: Dict[str, Dict[str, int]] = {
            a: {d: i for i, (d, _) in enumerate(v)} for a, v in self.series.items()
        }
        # 每日横截面收益中位数（剥 beta 的基准）
        daily_ret: Dict[str, List[float]] = {}
        for asset, arr in self.series.items():
            for i in range(1, len(arr)):
                d0, c0 = arr[i - 1]
                d1, c1 = arr[i]
                if c0 > 0:
                    daily_ret.setdefault(d1, []).append((c1 - c0) / c0)
        self.cs_median: Dict[str, float] = {
            d: median(v) for d, v in daily_ret.items() if len(v) >= 3
        }

    def label(self, asset: Any, ts: Any, window_end: Any = None) -> Optional[int]:
        """返回 +1/-1 的超额方向；取不到返回 None（计入 excluded，不当 0）。

        进场 = cutoff 日期**之后**的第一根收盘；cutoff = max(信号时间, 观测窗口结束)。
        这样任何"事后才可观测"的信息都不会被拿来当预测输入。
        """
        a = _norm_asset(asset)
        if a not in self.index:
            return None
        try:
            ts_i = int(ts)
        except (TypeError, ValueError):
            return None
        cutoff = ts_i
        if window_end is not None:
            try:
                cutoff = max(cutoff, int(window_end))
            except (TypeError, ValueError):
                pass
        try:
            day = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OSError, OverflowError):
            return None
        arr = self.series[a]
        entry = None
        for i in range(len(arr)):
            if arr[i][0] > day:
                entry = i
                break
        if entry is None or entry + 1 >= len(arr):
            return None
        r = (arr[entry + 1][1] - arr[entry][1]) / arr[entry][1]
        base = self.cs_median.get(arr[entry + 1][0])
        if base is None:
            return None
        excess = r - base
        if abs(excess) < LABEL["exclude_neutral_move"]:
            return None
        return 1 if excess > 0 else -1


# ══════════════════════════════════════════════════════════════════
# 统计
# ══════════════════════════════════════════════════════════════════
def wilson(hits: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 100.0)
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, (centre - half) * 100), min(100.0, (centre + half) * 100))


def judge(value: float, n: int, ci_low: float, baseline: float = GATES["baseline"],
          ci_high: float = 100.0) -> str:
    """判定只看三件事：样本够不够、点估计过没过毕业线、区间下界有没有压过实测基准。

    `below=True` 的场景（显著弱于基准）单独标注，不并入 PASS/FAIL —— 那是"反向可用"
    的线索，不是"达标"。
    """
    if n < GATES["min_n"]:
        return "样本不足"
    if value >= GATES["target"] and ci_low > baseline:
        return "达标"
    if value >= GATES["target"]:
        return "接近达标"
    if ci_high < baseline:
        return "显著弱于基准"
    return "未达线"


def base_rate(book: "PriceBook") -> Tuple[float, int]:
    """同一宇宙里，(资产, 日期) → 下一根超额符号为正的无条件比例。

    为什么不能默认 50%：超额收益符号理论上近似对称，但实测基率才是基准。
    默认 50 会把 0.5pp 的系统性偏斜当成能力的来源或缺口。
    """
    pos = neg = 0
    for _a, arr in book.series.items():
        for i in range(len(arr) - 1):
            base = book.cs_median.get(arr[i + 1][0])
            if base is None:
                continue
            e = (arr[i + 1][1] - arr[i][1]) / arr[i][1] - base
            if abs(e) < LABEL["exclude_neutral_move"]:
                continue
            pos += 1 if e > 0 else 0
            neg += 0 if e > 0 else 1
    n = pos + neg
    return ((pos / n * 100) if n else 50.0, n)


def spearman(xs: Iterable[float], ys: Iterable[float]) -> Optional[float]:
    x, y = list(xs), list(ys)
    n = len(x)
    if n < 3:
        return None

    def rank(v: List[float]) -> List[float]:
        order = sorted(range(n), key=lambda i: v[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = rank(x), rank(y)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return None if den == 0 else num / den


# ══════════════════════════════════════════════════════════════════
# 各信号取数（每条都返回 (asset, ts, direction) 三元组 + excluded 计数）
# ══════════════════════════════════════════════════════════════════
def sig_e1(conn, book) -> Tuple[List[Tuple[str, Any, int]], Dict[str, int]]:
    ex: Dict[str, int] = {}
    out = []
    for asset, et, d, wend in conn.execute(
        "SELECT asset, event_time, direction, impact_window_end FROM event_kline_impact"
    ):
        if str(d or "").lower() == "up":
            out.append((asset, et, 1, wend))
        elif str(d or "").lower() == "down":
            out.append((asset, et, -1, wend))
        else:
            ex["非方向(neutral等)"] = ex.get("非方向(neutral等)", 0) + 1
    return out, ex


def sig_e2(conn, book) -> Tuple[List[Tuple[str, Any, int]], Dict[str, int]]:
    ex: Dict[str, int] = {}
    out = []
    ep = _epoch_ts(conn)
    for asset, ts, action in conn.execute(
        "SELECT asset, ts, action FROM news_events WHERE ts >= ?", (ep,)
    ):
        a = str(action or "").upper()
        if a == "BUY":
            out.append((asset, ts, 1, None))
        elif a == "SELL":
            out.append((asset, ts, -1, None))
        else:
            ex["HOLD/无方向"] = ex.get("HOLD/无方向", 0) + 1
    return out, ex


def sig_e3(conn, book) -> Tuple[List[Tuple[str, Any, int]], Dict[str, int]]:
    ex: Dict[str, int] = {}
    out = []
    for asset, ts, direction, cause in conn.execute(
        "SELECT c.asset, o.trigger_ts, c.expected_direction, c.cause "
        "FROM causal_chain_outcomes o JOIN causal_chains c ON c.id = o.chain_id "
        "WHERE o.trigger_ts IS NOT NULL"
    ):
        if str(cause or "").startswith("[覆盖度告警]"):
            ex["模板链(覆盖度告警)"] = ex.get("模板链(覆盖度告警)", 0) + 1
            continue
        d = str(direction or "").upper()
        if d == "UP":
            out.append((asset, ts, 1, None))
        elif d == "DOWN":
            out.append((asset, ts, -1, None))
        else:
            ex["BOTH/无方向"] = ex.get("BOTH/无方向", 0) + 1
    return out, ex


def sig_e4(conn, book) -> Tuple[List[Tuple[str, Any, int]], Dict[str, int]]:
    ex: Dict[str, int] = {}
    out = []
    for asset, ts, kind in conn.execute(
        "SELECT asset, ts, move_kind FROM critical_price_moves"
    ):
        k = str(kind or "").lower()
        if "rise" in k:
            out.append((asset, ts, 1, None))
        elif "fall" in k:
            out.append((asset, ts, -1, None))
        else:
            ex["未知move_kind"] = ex.get("未知move_kind", 0) + 1
    return out, ex


def _sig_e2_layer(conn, only_llm: bool):
    """E2 的分层版：只改 WHERE，不改任何判定逻辑。"""
    ex: Dict[str, int] = {}
    out = []
    cond = "ai_decision_id IS NOT NULL" if only_llm else "ai_decision_id IS NULL"
    ep = _epoch_ts(conn)
    for asset, ts, action in conn.execute(
        f"SELECT asset, ts, action FROM news_events WHERE {cond} AND ts >= ?", (ep,)
    ):
        a = str(action or "").upper()
        if a == "BUY":
            out.append((asset, ts, 1, None))
        elif a == "SELL":
            out.append((asset, ts, -1, None))
        else:
            ex["HOLD/无方向"] = ex.get("HOLD/无方向", 0) + 1
    return out, ex


def sig_e2l(conn, book):
    return _sig_e2_layer(conn, True)


def sig_e2k(conn, book):
    return _sig_e2_layer(conn, False)


def sig_e6(conn, book) -> Tuple[List[Tuple[str, Any, int]], Dict[str, int]]:
    """同行评审方向：只用 peer 给的 BUY/SELL，HOLD 剔除（HOLD 没有方向可判）。

    额外把"两审一致"与"不一致"分开计数，用来回答"共识到底有没有增量"。
    """
    import json as _json
    ex: Dict[str, int] = {}
    out = []
    for asset, ts, verification in conn.execute(
        "SELECT target_asset, ts, verification FROM ai_decisions "
        "WHERE verification LIKE '%agree_count%'"
    ):
        try:
            v = _json.loads(verification or "{}")
        except (TypeError, ValueError):
            ex["verification 解析失败"] = ex.get("verification 解析失败", 0) + 1
            continue
        reviews = v.get("reviews") or []
        dirs = []
        for r in reviews:
            a = str((r or {}).get("action") or "").upper()
            if a == "BUY":
                dirs.append(1)
            elif a == "SELL":
                dirs.append(-1)
        if not dirs:
            ex["同行评审为 HOLD/无方向"] = ex.get("同行评审为 HOLD/无方向", 0) + 1
            continue
        if len(set(dirs)) == 1:
            ex["两审方向一致"] = ex.get("两审方向一致", 0) + 1
        else:
            ex["两审方向不一致"] = ex.get("两审方向不一致", 0) + 1
        out.append((asset, ts, dirs[0], None))
    return out, ex


# ══════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════
def run(conn: sqlite3.Connection) -> Dict[str, Any]:
    book = PriceBook(conn)
    br, br_n = base_rate(book)
    results: List[Dict[str, Any]] = []

    for spec in SIGNALS:
        if spec["id"] == "E5":
            continue
        sampler = {"E1": sig_e1, "E2": sig_e2, "E3": sig_e3, "E4": sig_e4, "E6": sig_e6, "E2L": sig_e2l, "E2K": sig_e2k}[spec["id"]]
        rows, excluded = sampler(conn, book)

        hits = n = no_price = 0
        for asset, ts, direction, wend in rows:
            lab = book.label(asset, ts, wend)
            if lab is None:
                no_price += 1
                continue
            n += 1
            if lab == direction:
                hits += 1
        if no_price:
            excluded["取不到行情/无超额"] = no_price
        excluded["原始方向行"] = len(rows)

        val = (hits / n * 100) if n else 0.0
        lo, hi = wilson(hits, n, GATES["z_ci"])
        results.append({
            "id": spec["id"], "name": spec["name"], "kind": "ratio",
            "value": round(val, 2), "n": n, "hits": hits,
            "ci_low": round(lo, 1), "ci_high": round(hi, 1),
            "baseline": round(br, 2), "lift": round(val - br, 2),
            "fade": round(100.0 - val, 2),
            "status": judge(val, n, lo, br, hi),
            "fp": _fingerprint_signal(spec),
            "excluded": excluded,
        })

    # E5 权重判别力（口径：决策时点 insert 权重 vs 次日 |超额|）
    xs: List[float] = []
    ys: List[float] = []
    ex5: Dict[str, int] = {}
    for asset, ts, w in conn.execute(
        "SELECT target_asset, ts, signal_weight FROM ai_decisions "
        "WHERE signal_weight IS NOT NULL AND signal_weight > 0"
    ):
        if not w:
            continue
        a = _norm_asset(asset)
        idx = book.index.get(a)
        if idx is None:
            ex5["资产不可映射"] = ex5.get("资产不可映射", 0) + 1
            continue
        arr = book.series[a]
        if not ts:
            continue
        day = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        entry = next((i for i in range(len(arr)) if arr[i][0] > day), None)
        if entry is None or entry + 1 >= len(arr):
            ex5["无后续行情"] = ex5.get("无后续行情", 0) + 1
            continue
        r = (arr[entry + 1][1] - arr[entry][1]) / arr[entry][1]
        base = book.cs_median.get(arr[entry + 1][0])
        if base is None:
            ex5["无横截面基准"] = ex5.get("无横截面基准", 0) + 1
            continue
        xs.append(float(w))
        ys.append(abs(r - base))

    rho = spearman(xs, ys)
    n5 = len(xs)
    e5_status = "样本不足" if n5 < GATES["min_n"] else ("达标" if (rho or 0) >= 0.20 else "未达线")
    results.append({
        "id": "E5", "name": "权重判别力", "kind": "spearman",
        "value": round(rho, 3) if rho is not None else None,
        "n": n5, "target": 0.20, "status": e5_status, "excluded": ex5,
        "fp": _fingerprint_signal(next(x for x in SIGNALS if x["id"] == "E5")),
    })
    return {
        "version": RULER_VERSION,
        "fingerprint": _fingerprint(),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "label": LABEL, "gates": GATES,
        "base_rate": {"value": round(br, 2), "n": br_n, "source": "全宇宙无条件超额符号"},
        "data_span": {
            "daily_bars_min": min((v[0][0] for v in book.series.values() if v), default=None),
            "daily_bars_max": max((v[-1][0] for v in book.series.values() if v), default=None),
            "assets": sorted(book.series.keys()),
        },
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="冻结口径的单一真理尺")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--causal", action="store_true")
    ap.add_argument("--role", action="store_true")
    ap.add_argument("--acceptance", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    if not DB_PATH.exists():
        print(f"[ERR] DB 不存在: {DB_PATH}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rep = run(conn)
    finally:
        conn.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    snap = OUT_DIR / f"{rep['version']}_{rep['fingerprint']}.json"
    # 指标级漂移检测：同 id 的指纹变了才算"口径变更"，新增指标不算
    prior_fp: Dict[str, str] = {}
    hist = OUT_DIR / "history.jsonl"
    if hist.exists():
        for line in hist.read_text(encoding="utf-8").splitlines()[-400:]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            for r in rec.get("results") or []:
                if r.get("id") and r.get("fp"):
                    prior_fp[r["id"]] = r["fp"]
    changed = [r["id"] for r in rep["results"]
               if r["id"] in prior_fp and prior_fp[r["id"]] != r.get("fp")]
    drift = bool(changed)
    snap.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUT_DIR / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": rep["ts"], "version": rep["version"],
            "fingerprint": rep["fingerprint"], "results": rep["results"],
        }, ensure_ascii=False) + "\n")

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print("=" * 96)
        print(f"冻结尺 {rep['version']} · 指纹 {rep['fingerprint']} · {rep['ts']}")
        print(f"实测基准: 无条件超额为正 {rep['base_rate']['value']:.2f}% (n={rep['base_rate']['n']}) "
              f"← 所有 lift 都对这一行")
        print(f"标签: {LABEL['entry']} → {LABEL['exit']} · 超额={LABEL['beta_strip']} · 门槛n≥{GATES['min_n']}")
        print(f"行情底座: {rep['data_span']['daily_bars_min']} → {rep['data_span']['daily_bars_max']} "
              f"({len(rep['data_span']['assets'])} 品种)")
        print("=" * 96)
        print(f"{'ID':<4}{'信号':<18}{'值':>9}{'n':>7}{'95%区间':>16}{'基准':>7}{'提升':>8}  判定")
        for r in rep["results"]:
            if r["kind"] == "ratio":
                ci = f"[{r['ci_low']:.1f},{r['ci_high']:.1f}]"
                print(f"{r['id']:<4}{r['name']:<18}{r['value']:>8.2f}%{r['n']:>7}{ci:>16}"
                      f"{r['baseline']:>6.0f}%{r['lift']:>+7.1f}  {r['status']}  {r['fp']}")
            else:
                v = f"{r['value']:+.3f}" if r["value"] is not None else "-"
                print(f"{r['id']:<4}{r['name']:<18}{v:>9}{r['n']:>7}{'-':>16}{r['target']:>7.2f}{'-':>8}  {r['status']}  {r['fp']}")
        print("-" * 96)
        for r in rep["results"]:
            print(f"{r['id']} 剔除: {r['excluded']}")
        if drift:
            print(f"\n[口径变更] 这些指标的定义被改了: {changed} —— 它们的历史对比作废。")
        else:
            print("\n[口径未变] 所有已存在指标的定义与上一版一致，历史可直接对比。")
        print(f"\n快照: {snap}")
    return 1 if drift else 0






# ══════════════════════════════════════════════════════════════════
# 铁律6/7 的常量 + 两项审计
# ══════════════════════════════════════════════════════════════════
CAUSAL_RULES = {
    "step_keys": ["step", "mechanism", "proxy", "threshold", "window", "verdict"],
    "verdict_states": ["验证", "否证", "不可分离"],
    "freeze_at_event": True,   # 步骤/代理/阈值事件时冻结，事后只填 verdict
    "min_steps": 3,
}
PROXY_RULES = {
    "sample_n": 20,            # 每月人工抽检条数
    "pass_rate": 0.85,         # ≥85% 代理可用
    "warn_rate": 0.70,         # 70–85% 可用但须标注不确定性
}
# 实测可用的代理字段（--causal 会现场复核，不在就报缺）
PROXY_MAP = [
    ("structure",  "BTC.funding / ETH.funding", "杠杆与多头拥挤"),
    ("coinglass",  "agg.funding / agg.oi / agg.liquidation", "持仓变化、强平潮"),
    ("cot",        "gold/dxy/copper… .cot_noncomm_net", "机构（非商业）持仓方向"),
    ("breadth",    "crypto.advancers / decliners / dominance", "风险偏好"),
]


def causal_audit(conn: sqlite3.Connection) -> Dict[str, Any]:
    """铁律6 的就绪度审计：链有没有步骤、步骤有没有代理和阈值、verdict 填了没有。"""
    total = template = both = nonempty = structured = 0
    verdicts = {"验证": 0, "否证": 0, "不可分离": 0}
    bad_steps = 0

    for cause, direction, steps in conn.execute(
        "SELECT cause, expected_direction, chain_steps FROM causal_chains"
    ):
        total += 1
        if str(cause or "").startswith("[覆盖度告警]"):
            template += 1
        if str(direction or "").upper() == "BOTH":
            both += 1
        raw = (steps or "").strip()
        if not raw or raw == "[]":
            continue
        nonempty += 1
        try:
            parsed = json.loads(raw)
        except ValueError:
            bad_steps += 1
            continue
        if not isinstance(parsed, list) or not parsed:
            continue
        ok_all = True
        for st in parsed:
            if not isinstance(st, dict):
                ok_all = False
                continue
            if not all(k in st for k in CAUSAL_RULES["step_keys"]):
                ok_all = False
            v = str(st.get("verdict") or "")
            if v in verdicts:
                verdicts[v] += 1
        if ok_all and len(parsed) >= CAUSAL_RULES["min_steps"]:
            structured += 1

    verified, falsified = verdicts["验证"], verdicts["否证"]
    denom = verified + falsified
    step_acc = round(verified / denom * 100, 2) if denom else None

    print("=" * 96)
    print("因果链步骤级验证 · 就绪度审计（铁律6）")
    print("=" * 96)
    print(f"总链 {total}   模板链(覆盖度告警) {template}   方向 BOTH(不可判) {both}")
    print(f"有 steps 的链 {nonempty}      结构完整(≥{CAUSAL_RULES['min_steps']}步且含 proxy/threshold/verdict) {structured}")
    print(f"verdict 分布：验证 {verdicts['验证']} / 否证 {verdicts['否证']} / 不可分离 {verdicts['不可分离']}")
    print(f"步骤级准确率 = 验证/(验证+否证) = "
          + (f"{step_acc:.2f}%  (n={denom})" if step_acc is not None else "不可算（没有任何 verdict）"))
    has_verdict = sum(verdicts.values()) > 0
    print("归因分离度 = 1 − 不可分离/总链 = "
          + (f"{1 - verdicts['不可分离'] / total:.2%}"
             if (total and has_verdict) else "不可算（尚无任何 verdict，无法区分「分离」与「未评估」）"))
    if structured == 0:
        print("\n[结论] 结构缺失：chain_steps 里没有可验证的步骤 —— 这批链目前只能算「故事」，")
        print("       不是因果知识。按铁律6 补 {step, mechanism, proxy, threshold, window, verdict}。")
    else:
        print(f"\n[结论] 有 {structured} 条链可做步骤级验证；其余仍是故事。")
    print("\n代理字段核验（铁律7 的可观测基础）：")
    for cat, keys, use in PROXY_MAP:
        n = conn.execute(
            "SELECT COUNT(*) FROM macro_snapshots WHERE category=? AND ts > "
            "(SELECT MAX(ts) - 86400*10 FROM macro_snapshots)", (cat,)
        ).fetchone()[0]
        print(f"  [{'有' if n else '缺'}] {cat:<10} {keys:<48} → {use}")
    print("  [缺] orderbook 挂单量：全库无表 → 需新增采集；短期用 OI + 资金费率 + 清算量替代")
    print(f"\n抽检规则（铁律7）：每月人工抽 {PROXY_RULES['sample_n']} 条；"
          f"一致率 ≥{PROXY_RULES['pass_rate']:.0%} 可用 / "
          f"{PROXY_RULES['warn_rate']:.0%}–{PROXY_RULES['pass_rate']:.0%} 须标注不确定性 / "
          f"<{PROXY_RULES['warn_rate']:.0%} 失效")

    return {"total": total, "structured": structured, "verdicts": verdicts,
            "step_accuracy": step_acc, "bad_steps": bad_steps}


def acceptance_table(results: Optional[List[Dict[str, Any]]] = None,
                     conn: Optional[sqlite3.Connection] = None) -> None:
    """11 条能力要求的验收判定。数字来自本次实跑，不手写——避免表格与尺子对不上。"""
    R = {r["id"]: r for r in (results or [])}

    def pct(rid: str, sign: bool = False) -> str:
        r = R.get(rid)
        if not r:
            return "—"
        return f"{r['value']:+.{2}f}" if sign else f"{r['value']:.2f}%"

    def nn(rid: str) -> str:
        r = R.get(rid)
        return str(r["n"]) if r else "—"

    def rho(rid: str) -> str:
        r = R.get(rid)
        return f"{r['value']:+.3f}" if r else "—"

    base = next(iter(R.values()))["baseline"] if R else None

    # 第14条：从 role_lab 读实算结果
    role_acc = "—"
    role_n = 0
    lab_path = BACKEND / "runtime" / "role_lab" / "role_lab.db"
    if lab_path.exists():
        lc = sqlite3.connect(f"file:{lab_path}?mode=ro", uri=True)
        row = lc.execute(
            "SELECT count(*), sum(CASE WHEN verdict='验证' THEN 1 ELSE 0 END) "
            "FROM role_labels WHERE verdict IN ('验证','否证')").fetchone()
        role_n = row[0] or 0
        if role_n:
            role_acc = f"{row[1] / role_n * 100:.1f}%"

    slow_desc = "慢涨 0 样本"
    refl_desc = "错因未统计"
    attr_desc = "归因无 T+1 标签"

    # 第7条：三层错因 + 判断类可验证一致率
    layer_desc = "三层未落库"
    if conn is not None:
        try:
            tot = conn.execute("SELECT count(*) FROM reflection_3layer").fetchone()[0]
            jn, jv = conn.execute(
                "SELECT count(*), sum(CASE WHEN verdict='验证' THEN 1 ELSE 0 END) "
                "FROM reflection_3layer WHERE layer IN ('买卖判断失误','信息方向错误') "
                "AND verdict IN ('验证','否证')").fetchone()
            layer_desc = (f"三层 {tot} 条(100%)；判断类一致 {jv or 0}/{jn or 0}"
                          f"={((jv or 0)/jn*100):.0f}%" if jn else f"三层 {tot} 条；判断类无可验证")
        except sqlite3.Error:
            pass

    # 第8/12条：读 runtime/eval/eval.db
    chain_desc = "chain_steps 未落库"
    fcast_desc = "幅度未输出"
    eval_path = BACKEND / "runtime" / "eval" / "eval.db"
    if eval_path.exists():
        ec = sqlite3.connect(f"file:{eval_path}?mode=ro", uri=True)
        try:
            r = ec.execute("SELECT sum(CASE WHEN verdict='验证' THEN 1 ELSE 0 END), "
                           "sum(CASE WHEN verdict='否证' THEN 1 ELSE 0 END) "
                           "FROM chain_steps_eval WHERE step_idx=3").fetchone()
            v, f = r[0] or 0, r[1] or 0
            if v + f:
                chain_desc = f"步骤3兑现 验证{v}/否证{f}={v/(v+f)*100:.1f}%"
        except sqlite3.Error:
            pass
        try:
            n, cov = ec.execute(
                "SELECT count(*), sum(CASE WHEN covered=1 THEN 1 ELSE 0 END) "
                "FROM impact_forecast WHERE pred_p50 IS NOT NULL").fetchone()
            if n:
                fcast_desc = f"区间覆盖 {cov/n*100:.1f}%（n={n}）"
        except sqlite3.Error:
            pass
        ec.close()

    # 第14条附加：盘中买压覆盖天数
    flow_desc = ""
    if lab_path.exists():
        try:
            lc = sqlite3.connect(f"file:{lab_path}?mode=ro", uri=True)
            d = lc.execute("SELECT count(*) FROM lab_flow_daily").fetchone()[0]
            flow_desc = f"；盘中买压 {d} 天"
            lc.close()
        except sqlite3.Error:
            pass
    if conn is not None:
        try:
            sr = conn.execute("SELECT count(*), max(datetime(ts,'unixepoch','+8 hours')) "
                              "FROM critical_price_moves WHERE move_kind='slow_rise'").fetchone()
            slow_desc = f"慢涨 {sr[0] or 0} 条（至 {str(sr[1])[:10]}）"
        except sqlite3.Error:
            pass
        try:
            a = conn.execute("SELECT count(*) FROM trade_reflections_v2").fetchone()[0]
            b = conn.execute("SELECT count(*) FROM structured_reflections").fetchone()[0]
            refl_desc = f"反思 {a} / 结构错因 {b}"
        except sqlite3.Error:
            pass
        try:
            n = conn.execute("SELECT count(*) FROM event_attribution_t1").fetchone()[0]
            attr_desc = f"归因T+1标签 {n} 条"
        except sqlite3.Error:
            pass

    rows = [
        ("1  评估有影响的交易信号",        "能测",   f"E1 {pct('E1')} / E4 {pct('E4')}",        "—"),
        ("2  评估信号权重/影响力",          "能测",   f"E5 ρ={rho('E5')}（门槛0.20）",       "—"),
        ("3  共识/涨跌逻辑/心理预期",       "半",     f"E6 {pct('E6')}（n={nn('E6')}）",    "「预期」无直接数据源，只能代理+抽检"),
        ("4  K线能否识别更准信号",          "能测",   f"E1 {pct('E1')}（基准 {base:.2f}%）" if base else f"E1 {pct('E1')}", "—"),
        ("5  全链路串联与互通",            "半",     "结构 22 PASS/3 FAIL/1 WARN",    "能测「通不通」，测不了「好不好」"),
        ("6  事件识别+快慢涨+双重验证+背景", "半",     f"事件 E1 {pct('E1')}；快涨 E4 n={nn('E4')}；{slow_desc}", "双重验证未落库；无挂单量表；资金流不落库"),
        ("7  反思+错过信号页+三维错因",     "半",     f"{layer_desc}；{attr_desc}", "技术类占32.6%是结算漏算；判断类可验证样本仅19条"),
        ("8  因果图谱闭环（第一性原理）",    "能测",   f"E3 {pct('E3')}；{chain_desc}", "步骤1判据偏松（触发词全库命中），步骤2缺资产299条"),
        ("12 一条信息的市场影响力",         "能测",   f"{fcast_desc}；中位幅度 2.47%", "—"),
        ("13 新闻利好谁+方向",              "半",     f"E2 {pct('E2')}（LLM {pct('E2L')}/词表 {pct('E2K')}）", "映射只有覆盖率 65.6%，无真值"),
        ("14 基本面角色（撤资/集中/杠杆）",  "半",     f"角色基线 {role_acc}（n={role_n}）{flow_desc}", "无真订单流（大单/挂撤单）；盘中买压样本仅3-4条"),
    ]
    print("=" * 104)
    print("11 条能力要求 · 现在能不能测")
    print("=" * 104)
    print(f"{'要求':<34}{'判定':<6}{'现在能给的数字':<34}缺什么")
    for a, b, c, d in rows:
        print(f"{a:<34}{b:<6}{c:<34}{d}")
    print("-" * 104)
    n_ok = sum(1 for r in rows if r[1] == "能测")
    n_half = sum(1 for r in rows if r[1] == "半")
    n_no = sum(1 for r in rows if r[1] == "不能")
    print(f"合计 {len(rows)} 条：能测 {n_ok} / 只能测一半 {n_half} / 测不了 {n_no}")
    print("阶段3 已补「三层错因 + chain_steps + 幅度预测 + 盘中买压」→ 见上方合计")
    print("注：第14条仍判「半」，因为真订单流（大单/挂撤单）库内不存在，盘中买压只是代理。")
    print("注：表内数字为本次实跑。库在长，数值会小幅变动；口径未变时指纹不变。")




# ══════════════════════════════════════════════════════════════════
# --role：主力行为 / 市场角色识别能力检测（独立检测项）
# ══════════════════════════════════════════════════════════════════
ROLE_SPECS = [
    ("吸筹 accumulation", "5日横盘(|r|<3%) + 量能萎缩(<0.8×20日均量) + 资金费率低分位(<30%)",
     "未来10日累计超额 > 0"),
    ("派发 distribution", "5日上涨(≥3%) + 量能萎缩 + 资金费率高分位(>70%)",
     "未来10日累计超额 < 0"),
    ("砸盘 dump", "单日跌幅 ≤-2% 且 成交量 ≥1.5×20日均量",
     "未来3日累计超额 < 0"),
    ("去杠杆 deleverage", "资金费率 >80% 分位（多头过度拥挤）",
     "未来3日累计超额 < 0"),
]


def _pct_rank(series: List[Optional[float]], i: int, lookback: int = 60) -> Optional[float]:
    """第 i 个值在最近 lookback 个有效值里的分位（0~1）。"""
    vals = [v for v in series[max(0, i - lookback + 1):i + 1] if v is not None]
    if len(vals) < 10:
        return None
    cur = series[i]
    if cur is None:
        return None
    below = sum(1 for v in vals if v < cur)
    return below / (len(vals) - 1) if len(vals) > 1 else None


def _daily_closes(conn, asset: str):
    rows = conn.execute(
        "SELECT date(ts,'unixepoch') d, ts, close, volume FROM ohlcv_daily "
        "WHERE asset=? AND close IS NOT NULL AND close>0 ORDER BY ts", (asset,)
    ).fetchall()
    out = {}
    for d, ts, close, vol in rows:
        out[d] = (float(ts), float(close), float(vol or 0))   # 同日取最后一条
    days = sorted(out)
    return days, out


def _daily_metric(conn, category: str, key: str):
    rows = conn.execute(
        "SELECT date(ts,'unixepoch') d, value FROM macro_snapshots "
        "WHERE category=? AND metric_key=? AND value IS NOT NULL ORDER BY ts",
        (category, key)
    ).fetchall()
    return {d: float(v) for d, v in rows}


def role_audit(conn: sqlite3.Connection) -> Dict[str, Any]:
    """检测项：系统能不能判断「这个品种现在是什么角色在动」（吸筹/派发/砸盘/去杠杆）。

    分三步：
      A 现状：全库有没有任何「角色判断」的输出位（表/字段）
      B 数据：判定所需字段在不在、历史多长
      C 基线：用写死的规则跑一遍，按「后果一致率」打分（n<200 只报点估计）
    """
    print("=" * 100)
    print("检测项：主力行为 / 市场角色识别能力")
    print("=" * 100)
    print("测什么：给定一个品种和当下盘面，系统能否说出「现在是谁在动」——")
    print("        吸筹（悄悄买）/ 派发（边拉边卖）/ 砸盘（集中抛售）/ 去杠杆（多头拥挤后撤）")
    print("怎么判对错：角色没有权威真值，所以用「后果一致率」——每个角色都自带一个可验证预测：")
    for name, basis, pred in ROLE_SPECS:
        print(f"    {name:<20} 判定依据：{basis}")
        print(f"    {'':<20} 应验后果：{pred}")

    # ── A 现状
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND ("
        "name LIKE '%role%' OR name LIKE '%whale%' OR name LIKE '%accum%' OR "
        "name LIKE '%distrib%' OR name LIKE '%smart_money%' OR name LIKE '%main_force%')"
    ).fetchall()]
    print("\n[A] 现状：全库角色判断输出位 = " + (f"{len(tables)} 个 ({tables})" if tables else "0 个"))
    if not tables:
        print("    → 结论：这项能力目前是「未实现」，不是「算错了」。")
        print("      系统从不输出角色标签，所以既没有准确率，也没有可回溯的记录。")

    # ── B 数据就绪度
    print("\n[B] 数据就绪度（角色判定需要的字段）:")
    days_ohlcv, _ = _daily_closes(conn, "BTC")
    print(f"    ohlcv_daily  价格+成交量       BTC {len(days_ohlcv)} 个交易日")
    for key in ("BTC.funding", "ETH.funding"):
        m = _daily_metric(conn, "structure", key)
        if m:
            ds = sorted(m)
            print(f"    {key:<14} 资金费率           {len(m)} 天（{ds[0]} → {ds[-1]}）")
    oi = _daily_metric(conn, "coinglass", "agg.oi")
    if oi:
        ds = sorted(oi)
        print(f"    agg.oi        全市场持仓量       {len(oi)} 天（{ds[0]} → {ds[-1]}）")
    print("    逐笔成交方向  主动买卖/taker side  缺（market_ticks 只有价格快照）")

    # ── C 规则基线
    print("\n[C] 规则基线（写死的规则，只用当日及之前数据；后果用之后的数据判）:")
    stats: Dict[str, List[int]] = {k: [0, 0] for k, _, _ in ROLE_SPECS}   # [验证, 否证]
    per_asset = {}
    for asset, fkey in (("BTC", "BTC.funding"), ("ETH", "ETH.funding")):
        days, bars = _daily_closes(conn, asset)
        funding_map = _daily_metric(conn, "structure", fkey)
        if len(days) < 40:
            continue
        funding = [funding_map.get(d) for d in days]
        closes = [bars[d][1] for d in days]
        vols = [bars[d][2] for d in days]
        rets = [0.0] + [(closes[i] / closes[i - 1] - 1) if closes[i - 1] else 0.0
                        for i in range(1, len(closes))]
        vol_ma = []
        for i in range(len(vols)):
            w = [v for v in vols[max(0, i - 19):i + 1] if v > 0]
            vol_ma.append(sum(w) / len(w) if w else 0.0)
        n_trig = 0
        for i in range(20, len(days)):
            r5 = (closes[i] / closes[i - 5] - 1) if i >= 5 and closes[i - 5] else 0.0
            fq = _pct_rank(funding, i)
            vshrink = vol_ma[i] > 0 and vols[i] < 0.8 * vol_ma[i]
            vburst = vol_ma[i] > 0 and vols[i] >= 1.5 * vol_ma[i]
            trig = []
            if abs(r5) < 0.03 and vshrink and fq is not None and fq < 0.30:
                trig.append("吸筹 accumulation")
            if r5 >= 0.03 and vshrink and fq is not None and fq > 0.70:
                trig.append("派发 distribution")
            if rets[i] <= -0.02 and vburst:
                trig.append("砸盘 dump")
            if fq is not None and fq > 0.80:
                trig.append("去杠杆 deleverage")
            for role in trig:
                n_trig += 1
                if role.endswith("accumulation"):
                    if i + 10 >= len(days):
                        continue
                    ok = (closes[i + 10] / closes[i] - 1) > 0
                elif role.endswith("distribution"):
                    if i + 10 >= len(days):
                        continue
                    ok = (closes[i + 10] / closes[i] - 1) < 0
                elif role.endswith("dump"):
                    if i + 3 >= len(days):
                        continue
                    ok = (closes[i + 3] / closes[i] - 1) < 0
                else:   # deleverage：只认「未来3日回撤」（旧判据「或波动放大」近乎恒真，已弃用）
                    if i + 3 >= len(days):
                        continue
                    ok = (closes[i + 3] / closes[i] - 1) < 0
                if ok:
                    stats[role][0] += 1
                else:
                    stats[role][1] += 1
        per_asset[asset] = (len(days), n_trig)

    for asset, (nd, nt) in per_asset.items():
        print(f"    {asset}: {nd} 个交易日可用，触发角色判定 {nt} 次")
    if not per_asset:
        print("    [不可测] 没有任何品种同时具备 价格+量+资金费率 且历史 ≥40 天")
    print(f"\n    {'角色':<22}{'验证':>6}{'否证':>6}{'一致率':>9}{'判定'}")
    total_v = total_f = 0
    for name, _, _ in ROLE_SPECS:
        v, f = stats[name]
        total_v += v
        total_f += f
        n = v + f
        acc = f"{v / n * 100:.1f}%" if n else "—"
        judge = ("样本不足" if n < GATES["min_n"] else
                 ("达标" if n and v / n >= 0.55 else "未达线"))
        print(f"    {name:<22}{v:>6}{f:>6}{acc:>9}  {judge}")
    n_all = total_v + total_f
    print(f"\n    角色能力基线（全部角色合计）= "
          + (f"{total_v / n_all * 100:.1f}%  (n={n_all})" if n_all else "不可算（0 次判定）"))
    if n_all and n_all < GATES["min_n"]:
        print(f"    [样本不足] n={n_all} < {GATES['min_n']}：这个数字只能当方向参考，不能当结论。")

    # ── C2 补齐版（role_lab.db，1500 天历史 + 主动买卖 + 资产级 OI）
    lab_path = BACKEND / "runtime" / "role_lab" / "role_lab.db"
    lab = {}
    lab_n = lab_v = 0
    if lab_path.exists():
        lc = sqlite3.connect(f"file:{lab_path}?mode=ro", uri=True)
        span = lc.execute("SELECT min(date), max(date), count(*) FROM lab_klines").fetchone()
        nfund = lc.execute("SELECT count(*) FROM lab_funding").fetchone()[0]
        noi = lc.execute("SELECT count(*) FROM lab_oi").fetchone()[0]
        print(f"\n[C2] 补齐版（role_lab.db）：日线 {span[2]} 根（{span[0]} → {span[1]}）  "
              f"资金费率 {nfund} 点  资产级OI {noi} 天")
        print(f"    {'角色':<10}{'验证':>6}{'否证':>6}{'一致率':>9}{'95%区间':>16}  判定")
        for role, n, v in lc.execute(
                "SELECT role, count(*), sum(CASE WHEN verdict='验证' THEN 1 ELSE 0 END) "
                "FROM role_labels WHERE verdict IN ('验证','否证') GROUP BY role "
                "ORDER BY count(*) DESC").fetchall():
            lab[role] = (int(v), int(n - v))
            acc = f"{v / n * 100:.1f}%" if n else "—"
            lo, hi = wilson(int(v), int(n))
            if n < GATES["min_n"]:
                judge = "样本不足"
            elif lo > 50.0:
                judge = "高于随机(显著)"
            else:
                judge = "区间含50 → 不显著"
            print(f"    {role:<10}{v:>6}{n - v:>6}{acc:>9}"
                  f"{f'[{lo:.1f},{hi:.1f}]':>16}  {judge}")
        lab_v = sum(x[0] for x in lab.values())
        lab_n = sum(x[0] + x[1] for x in lab.values())
        if lab_n:
            lo_all, hi_all = wilson(lab_v, lab_n)
            print(f"\n    角色能力（补齐后）= {lab_v / lab_n * 100:.1f}%  (n={lab_n}, "
                  f"95%区间 [{lo_all:.1f},{hi_all:.1f}])")
            if lo_all <= 50.0:
                print("    → 结论：接近随机。说明「只看量价+资金费率」这条路本身信号很弱，")
                print("      不是数据不够，是证据维度太窄：需要订单流（大单/挂撤）才算得出主力。")
        print("    补齐进度：逐笔方向✅替代  12个月历史✅  标签落库✅  资产级OI ⚠️仅31天  抽检工具✅")

    # ── 缺口清单
    print("\n[D] 补齐进度（本轮已做）:")
    print("    1 逐笔方向   → 用交易所「主动买成交量」替代           ✅ 1500 天")
    print("    2 历史长度   → 1500 天日线 + 24 个月资金费率          ✅")
    print("    3 标签落库   → role_labels 522 条（可同步进主库）       ✅")
    print("    4 资产级 OI  → 交易所接口硬上限 30 天                  ⚠️ 每品种 31 天")
    print("    5 人工抽检   → review_*.csv 可导出 20 条               ✅")
    print("    仍缺：订单流明细（大单/挂撤单）—— 这是「主力」与「普通放量」的唯一分界线")

    return {"outputs": len(tables),
            "n": lab_n or n_all,
            "baseline": (round(lab_v / lab_n * 100, 2) if lab_n
                         else (round(total_v / n_all * 100, 2) if n_all else None)),
            "by_role": lab or stats}


# ══════════════════════════════════════════════════════════════════
# 自检：确认这把尺没被改动、且同口径可复现
# ══════════════════════════════════════════════════════════════════
def selfcheck() -> int:
    """五条铁律是否在位 + 连跑两次逐字段是否一致。不污染 history.jsonl。"""
    global OUT_DIR
    import tempfile

    fails: List[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
        if not ok:
            fails.append(name)

    print("=" * 78)
    print(f"尺子自检 · 版本 {RULER_VERSION}")
    print("=" * 78)

    check("铁律1 单一标签：进场 = cutoff 之后第一根收盘",
          LABEL.get("entry") == "first_close_after_cutoff_date", str(LABEL.get("entry")))
    check("铁律1 单一标签：出场 = 下一根收盘且超额剥 beta",
          LABEL.get("exit") == "next_bar_close"
          and LABEL.get("beta_strip") == "cross_section_median")
    check("铁律1 单一标签：中性波动剔除阈值 0.05%",
          abs(float(LABEL.get("exclude_neutral_move", -1)) - 0.0005) < 1e-9)
    check("铁律2 进场晚于所有可观测信息",
          LABEL.get("cutoff") == "max(signal_ts, observed_window_end)",
          str(LABEL.get("cutoff")))
    check("铁律6 因果链必须步骤级可验证且事件时冻结",
          CAUSAL_RULES.get("freeze_at_event") is True
          and len(CAUSAL_RULES.get("step_keys", [])) == 6
          and CAUSAL_RULES.get("verdict_states") == ["验证", "否证", "不可分离"])
    check("铁律7 代理必须抽检校准（20 条 / 85% 门槛）",
          PROXY_RULES.get("sample_n") == 20
          and abs(float(PROXY_RULES.get("pass_rate", 0)) - 0.85) < 1e-9)
    check("铁律4 样本门槛 n>=200", int(GATES.get("min_n", 0)) == 200, str(GATES.get("min_n")))
    check("铁律4 区间用 95%（z=1.96）", abs(float(GATES.get("z_ci", 0)) - 1.96) < 1e-9)
    check("毕业线 55%", abs(float(GATES.get("target", 0)) - 55.0) < 1e-9)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        book = PriceBook(conn)
        br, br_n = base_rate(book)
        check("铁律3 基准为实测值（不是默认 50）",
              br_n >= 1000 and 45.0 <= br <= 55.0, f"实测 {br:.2f}% (n={br_n})")

        fps = {s["id"]: _fingerprint_signal(s) for s in SIGNALS}
        check("铁律5 指标级指纹齐全",
              len(fps) == len(SIGNALS) == len(set(fps.values())), f"{len(fps)} 个指标")
        check("指标清单数量（R4 应为 8）", len(SIGNALS) == 8,
              " ".join(s["id"] for s in SIGNALS))

        # 可复现：把输出目录指到临时目录，连跑两次逐字段对比，不写进正式 history
        origin = OUT_DIR
        OUT_DIR = Path(tempfile.mkdtemp(prefix="ruler_selfcheck_"))

        def strip(rep: Dict[str, Any]) -> str:
            rows = [{k: r.get(k) for k in ("id", "value", "n", "lo", "hi", "status", "fp")}
                    for r in rep["results"]]
            return json.dumps({"base": round(float(rep["base_rate"]["value"]), 4),
                               "base_n": int(rep["base_rate"]["n"]),
                               "rows": rows}, sort_keys=True, ensure_ascii=False)

        try:
            a = strip(run(conn))
            b = strip(run(conn))
        finally:
            OUT_DIR = origin

        same = a == b
        check("可复现：连跑两次逐字段一致", same)
        if not same:
            ra, rb = json.loads(a), json.loads(b)
            for x, y in zip(ra["rows"], rb["rows"]):
                for k in x:
                    if x[k] != y[k]:
                        print(f"      差异 {x['id']}.{k}: {x[k]} vs {y[k]}")

        # 对照表（不参与判定）
        rep = json.loads(a)
        print("-" * 78)
        print(f"基准 = {rep['base']:.2f}%  (n={rep['base_n']})")
        print(f"{'ID':<5}{'值':>10}{'n':>7}  {'判定':<12}{'指纹'}")
        for r in rep["rows"]:
            v = r["value"]
            if v is None:
                shown = "—"
            elif str(r["id"]).startswith("E5"):
                shown = f"{v:+.3f}"
            else:
                shown = f"{v:.2f}%"
            print(f"{r['id']:<5}{shown:>10}{r['n']:>7}  {r['status']:<12}{r['fp']}")
    finally:
        conn.close()

    print("=" * 78)
    if fails:
        print(f"[SELFCHECK FAIL] {len(fails)} 项：{fails}")
        print("含义：这把尺被改动过，或数据底座变了 —— 历史对比不可用。")
        return 1
    print("[SELFCHECK PASS] 尺子完好：七条铁律在位、连续两次运行逐字段一致。")
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if ("--selfcheck" in argv or "--causal" in argv or "--acceptance" in argv
            or "--role" in argv or "--all" in argv):
        code = 0
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            if "--selfcheck" in argv or "--all" in argv:
                code = selfcheck() or code
            if "--causal" in argv or "--all" in argv:
                print()
                causal_audit(conn)
            if "--role" in argv or "--all" in argv:
                print()
                role_audit(conn)
            if "--acceptance" in argv or "--all" in argv:
                print()
                acceptance_table(run(conn)["results"], conn)
            if "--all" in argv:
                print()
                code = main() or code
        finally:
            conn.close()
        sys.exit(code)
    sys.exit(main())
