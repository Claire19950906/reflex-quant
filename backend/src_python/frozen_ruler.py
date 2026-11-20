# -*- coding: utf-8 -*-
"""frozen_ruler.py — 冻结口径的单一真理尺（当前 R3）

为什么要有这个文件
-----------------
能力验收反复出现"每次扫描结论不一样"，根因不是系统在变，而是**尺子在变**：
  · 同一件事有三套口径（端点 200 审计 / capability_acceptance 把结构+准确率混在一张
    PASS/FAIL 表 / capability_scorecard 统计层）；
  · 分母被反复重置（归因窗口一改，历史行作废）；
  · 方向口径混入 BOTH（"涨跌都算对"被记成方向能力，76.87% vs 48.4%）。

这个脚本只干一件事：**把"怎么算对"写成一份带指纹的配置，然后所有信号用同一个标签
算一遍**，给出 命中率 + Wilson 区间 + 实测基准 + 判定。

五条铁律（改任何一条 = 改尺；指纹变，旧对比作废）
------------------------------------------------------
1. 单一标签   ：信号 cutoff 之后第一根日线收盘进场，再下一根收盘出场，
                用**带符号超额收益**定方向；超额 = 本资产收益 − 同日全品种横截面中位数。
2. 进场晚于信息：cutoff = max(信号时间戳, 该信号的观测窗口结束时间)。
                日线不知收盘时刻 → 一律推到下一根收盘。当天进场 = 前视泄漏。
3. 基准必须实测：方向类基准 = 本宇宙无条件超额为正的比例（实测 50.58%，n≈9400），
                不默认 50%。
4. 样本门槛   ：n < 200 只报"样本不足"，**不判 FAIL**。
5. 口径指纹   ：指纹拆到**指标级**。新增指标不作废已有指标的历史；
                改动某个指标的定义，只有那一项的对比作废。

用法
----
    python3 frozen_ruler.py            # 人类可读
    python3 frozen_ruler.py --json     # 机读
    python3 ruler_selfcheck.py         # 尺子自检（可复现性 + 配置完整性），先跑这个
退出码：0 正常；1 有指标口径被改；2 环境错误。
纯只读，不写业务库，只在 runtime/frozen_ruler/ 下落快照 + 追加 history.jsonl。
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
     "note": "news_events.action ∈ {BUY,SELL} 且资产可映射 → T+1 超额方向"},
    {"id": "E3", "name": "因果链方向", "table": "causal_chains",
     "note": "expected_direction ∈ {UP,DOWN} 且剔除'[覆盖度告警]'模板链 → T+1 超额方向"},
    {"id": "E4", "name": "异动方向延续", "table": "critical_price_moves",
     "note": "fast_rise/slow_rise→UP, fast_fall/slow_fall→DOWN → T+1 超额方向"},
    {"id": "E5", "name": "权重判别力", "table": "signal_weight_history",
     "note": "决策时点(insert)权重 vs |超额| 的 Spearman ρ（ρ 类，不用命中率口径）"},
    {"id": "E2L", "name": "新闻方向·LLM子集", "table": "news_events(ai_decision_id NOT NULL)",
     "note": "E2 的分层：只用 LLM 给过结论的新闻。加这一层的唯一目的，是把'整体差'拆成'哪一环差'"},
    {"id": "E2K", "name": "新闻方向·关键词子集", "table": "news_events(ai_decision_id IS NULL)",
     "note": "E2 的分层：只用关键词词表打分的新闻，作为对照组"},
    {"id": "E6", "name": "同行评审方向", "table": "ai_decisions.verification",
     "note": "同行评审(peer)给出的 BUY/SELL（HOLD 剔除）→ T+1 超额方向；"
             "额外报两审一致/不一致两个子集，用于判断'共识'有没有增量"},
]
RULER_VERSION = "R3"   # R1 的 E1–E5 定义未动，指纹不变，历史照旧可比

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
        rows = conn.execute(
            "SELECT asset, date(ts,'unixepoch') d, close FROM ohlcv_daily "
            "WHERE close IS NOT NULL AND close > 0 ORDER BY asset, ts"
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
    for asset, ts, action in conn.execute(
        "SELECT asset, ts, action FROM news_events"
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
    for asset, ts, action in conn.execute(
        f"SELECT asset, ts, action FROM news_events WHERE {cond}"
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


if __name__ == "__main__":
    sys.exit(main())
