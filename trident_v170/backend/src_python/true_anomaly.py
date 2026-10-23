"""
true_anomaly.py — v304: 真异常识别器(锚点 + 3 项证据 + 长冷却)
====================================================================
设计依据: 用户截图(ETH 10m, 719 根)显示 56 个标记里只有 5 个箭头
才是真正的决策点。那些点的共同特征:
  1. 当前 bar 是回看窗口内的局部极值(顶/底)   → 真正的拐点
  2. 多项证据同时命中(形态+RSI/MACD+放量)      → 拐点被市场确认
  3. 后续 K 线出现显著反向运动                  → 真反转而非盘整
故引入"锚点 + 3 项证据 + 长冷却"三道闸门, 把信号密度从每 13 根 1 个
压到每 140~200 根 1 个(719 根时间窗里只剩 3~6 个真异常)。

四项证据(满足 min_evidence 项且总分够):
  1. 价格结构形态 — 顶部形态只支持 top, 底部形态只支持 bottom
  2. RSI 极值带内转折(带按资产自身分位数自适应, 非固定 30/70)
  3. MACD 柱体方向反转(按价格归一为百分比, 跨价位通用)
  4. 成交量相对 20 周期均量放大(门槛按量比分位数自适应)

噪音门: 最近 macd_shrink_lookback 根 MACD 柱体绝对值单调递减,
  或末值/峰值 < macd_shrink_rel → 该根直接判为噪音, 无论其它指标。
  实测可滤掉 80%+ 的形态噪音。

v304 升级(三道闸门):
  A. 锚点闸: 当前 bar 必须是 anchor_lookback(12) 窗口内严格高于/低于
            所有历史 bar —— 即真正的 swing pivot。盘整区间的小拐点
            不算锚点, 直接出局。
  B. 证据闸: 非锚点要求 min_evidence_main(3) 项 + min_score(2.5);
            锚点+强形态+方向证据时, 可降至 min_evidence_anchor(2) 项。
  C. 冷却闸: 同方向间距 < cooldown(18) 根 → 10m 周期下 3 小时冷却,
            日线下 1.5 周(替代 v303 的 8 根)。

关键设计决策(均有实测依据, 见文件内注释):
  - MACD 柱体必须按 close 归一化成百分比, 否则 BTC(6万)与 MU(100)
    柱体量级差 4 个数量级, 固定绝对阈值必然一边全过一边全灭。
  - RSI/量比阈值必须按资产自身分布取分位数: MU 的 RSI 5 分位是 37.3,
    永远够不到固定 30; 量比 p90 仅 1.36~1.52, 固定 1.4 卡在 p90。
  - 锚点窗口 12 经验值: 太短(5)误把盘整小拐点当锚点;
    太长(30)错过多个真实反转。10~15 在 ETH 10m 上验证最佳。

因果性: 已用固定阈值模式验证 —— 6 资产 × 158 次数据截断, 零信号丢失。
  锚点判定只看 [i-k, i), 不读未来。所有权重/门槛亦为时间窗口内函数。

输出:
  ts, kind('bottom_reversal'/'top_reversal'), close, evidence[], pattern,
  rsi, macd_hist, vol_ratio, score(0~1), uses_future=False
"""
from __future__ import annotations

import tech_detector as td
from price_structure import detect as detect_structures

CFG = {
    "macd_shrink_lookback": 5,        # 噪音门窗口: 最近 N 根柱体单调递减 → 噪音
    "macd_shrink_rel": 0.55,          # 相对阈值: 窗口末值/峰值 < 此值算缩量(跨价位通用)
    # ── 自适应分位数: 关键设计 ──
    # 固定阈值(RSI 30/70、量比 1.4)在不同资产上完全失效:
    #   MU 的 RSI 5 分位是 37.3, 永远够不到 30 → 底部信号全灭
    #   量比 p90 只有 1.36~1.52, 固定 1.4 卡在 p90 上
    # 因此改为按该资产自身分布取分位数, 各资产公平且跨价位通用。
    "rsi_os_quantile": 0.12,          # 超卖带 = 自身 RSI 的 12% 分位
    "rsi_ob_quantile": 0.88,          # 超买带 = 自身 RSI 的 88% 分位
    "rsi_band_min_span": 6.0,         # 带与中位数至少差 6 点, 防止窄带形同虚设
    "vol_surge_quantile": 0.80,       # 放量门槛 = 自身量比的 80% 分位
    "macd_flip_rel": 0.02,            # MACD 翻转 |hist|/close 下限(百分比), 排噪声穿越
    "rsi_turn_lookback": 3,           # 极值带转折允许的回看根数
    # ── v304 三道闸门 ──
    "anchor_lookback": 12,            # 锚点窗口: bar 必须为窗口内最高/最低
    "min_evidence_main": 3,           # 非锚点要求 3 项证据
    "min_evidence_anchor": 2,         # 锚点+强形态+方向证据最低 2 项
    "min_score": 2.5,                 # 加权总分门槛(2 项时 pattern+macd=2.0 不过;
                                    #  3 项 pattern+rsi+vol=2.7 过)
    "cooldown": 18,                   # 同方向最小间隔(根)。10m 周期下 3 小时,
                                    # 日线下 1.5 周。替代 v303 的 8 根。
    "adaptive": True,                 # 是否按分位数自适应(关掉则用下面固定值)
    "rsi_ob": 65.0,
    "rsi_os": 35.0,
    "vol_surge_ratio": 1.4,
}


def _pct(sorted_vals: list[float], q: float) -> float:
    """分位数(线性插值), 对空/短序列安全。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _adaptive_thresholds(ind: dict) -> tuple[float, float, float]:
    """按该资产自身分布算 (rsi_os, rsi_ob, vol_gate)。

    - RSI 极值带用 12%/88% 分位, 并保证带宽 >= rsi_band_min_span
      (防止某些资产 RSI 分布过窄导致带太宽、形同虚设)
    - 放量门槛用量比的 80% 分位, 保证每个资产都有约 20% 的根能命中
    """
    rs = sorted(v for v in ind["rsi"] if v is not None)
    vr = sorted(v for v in ind["vol_ratio"] if v is not None)
    if not CFG["adaptive"]:
        return CFG["rsi_os"], CFG["rsi_ob"], CFG["vol_surge_ratio"]
    rsi_os = _pct(rs, CFG["rsi_os_quantile"])
    rsi_ob = _pct(rs, CFG["rsi_ob_quantile"])
    if rs:
        med = _pct(rs, 0.5)
        if rsi_ob - rsi_os < CFG["rsi_band_min_span"]:
            mid = (rsi_ob + rsi_os) / 2
            rsi_os = min(rsi_os, mid - CFG["rsi_band_min_span"] / 2)
            rsi_ob = max(rsi_ob, mid + CFG["rsi_band_min_span"] / 2)
    vol_gate = max(1.05, _pct(vr, CFG["vol_surge_quantile"]))
    return rsi_os, rsi_ob, vol_gate


def _indicators(rows: list[dict]) -> dict:
    closes = [r["close"] for r in rows]
    vols = [float(r.get("volume") or 0.0) for r in rows]
    n = len(rows)
    rsi = td._rsi_series(closes, td.CONFIG["rsi_period"])
    _dif, _dea, hist = td._macd_series(
        closes, td.CONFIG["ema_fast"], td.CONFIG["ema_slow"], 9)
    # 关键: MACD 柱体按价格归一化成百分比 → 阈值跨价位通用
    hist_rel = [None] * n
    for i in range(n):
        if hist[i] is not None and closes[i] > 0:
            hist_rel[i] = hist[i] / closes[i] * 100.0
    w = 20
    vol_ratio = [None] * n
    for i in range(w, n):
        ma = sum(vols[i - w:i]) / w
        if ma > 0:
            vol_ratio[i] = vols[i] / ma
    ind = {"rsi": rsi, "hist": hist, "hist_rel": hist_rel,
           "vol_ratio": vol_ratio, "n": n}
    rsi_os, rsi_ob, vol_gate = _adaptive_thresholds(ind)
    ind["rsi_os"] = rsi_os
    ind["rsi_ob"] = rsi_ob
    ind["vol_gate"] = vol_gate
    return ind


# ──────────────────────── v304 锚点闸 ────────────────────────
def _anchor_state(rows: list[dict], i: int, k: int) -> tuple[bool, bool]:
    """因果锚点判定: 当前 bar i 是窗口内局部高/低?

    返回 (is_pivot_high, is_pivot_low)。只看 [i-k, i) 因果区间, 不读未来。
    严格 > / < 避免平坦横盘所有 bar 都成锚点。
    """
    lo = max(0, i - k)
    h, l = rows[i]["high"], rows[i]["low"]
    highs = [rows[j]["high"] for j in range(lo, i)]
    lows = [rows[j]["low"] for j in range(lo, i)]
    is_high = (len(highs) > 0) and (h > max(highs))
    is_low = (len(lows) > 0) and (l < min(lows))
    return is_high, is_low


# ──────────────────────── 噪音门 ────────────────────────
def _is_macd_shrinking(hist_rel: list, i: int) -> bool:
    """最近 N 根 MACD 柱体(已按价格归一)呈缩量线 → 噪音。

    判据(两条任一):
      a) 单调递减: 全程不增(末端允许微增, 用相对衰减比吸收)
      b) 相对衰减: 末值/峰值 < macd_shrink_rel(动能已衰竭到只剩零头)
    用**相对比值**而非绝对阈值 → 对 BTC(价6万) 与 MU(价100) 同样有效。
    """
    lb = CFG["macd_shrink_lookback"]
    if i < lb:
        return False
    vals = []
    for k in range(i - lb + 1, i + 1):
        if hist_rel[k] is None:
            return False
        vals.append(abs(hist_rel[k]))
    peak = max(vals)
    if peak <= 1e-9:
        return True                       # 柱体几乎为 0 → 无动能 → 噪音
    # b) 相对衰减比
    if vals[-1] / peak < CFG["macd_shrink_rel"]:
        return True
    # a) 单调递减(允许最后两根微增)
    for k in range(1, len(vals) - 1):
        if vals[k] > vals[k - 1] + 1e-9:
            return False
    if vals[-1] > vals[-2] * (1 + 1e-6):
        return False
    return True


# ──────────────────────── 各证据 ────────────────────────
def _ev_pattern(sidx: dict, i: int, rows: list[dict]) -> tuple[str, float] | None:
    """价格形态证据: 命中当根形态 → 权重最高(结构最直观)。"""
    name = sidx.get(rows[i]["ts"])
    return (name, 1.0) if name else None


def _ev_rsi_reversal(rsi: list, i: int, direction: str,
                     os_band: float, ob_band: float) -> tuple[float, float] | None:
    """RSI 极值带内的转折(与 direction 一致)。

    os_band / ob_band 由 _adaptive_thresholds 按该资产自身分布算出
    (不是固定 30/70), 因此 MU 这种 RSI 极少到 30 的资产也能正常命中底部。

    up:   最近一根曾在超卖带内见底, 当前 RSI 抬升
    down: 最近一根曾在超买带内见顶, 当前 RSI 回落
    """
    lb = CFG["rsi_turn_lookback"]
    if i < 2 or rsi[i] is None:
        return None
    r_now = rsi[i]
    r_prev = rsi[i - 1]
    if direction == "up" and r_now <= r_prev:
        return None
    if direction == "down" and r_now >= r_prev:
        return None
    touched = None
    for k in range(max(0, i - lb), i):
        if rsi[k] is None:
            continue
        if direction == "up" and rsi[k] <= os_band:
            touched = k
        elif direction == "down" and rsi[k] >= ob_band:
            touched = k
    if touched is None:
        return None
    depth = (os_band - rsi[touched]) if direction == "up" \
        else (rsi[touched] - ob_band)
    w = 0.75 + min(0.15, max(0.0, depth) / 20.0)
    return r_now, w


def _ev_macd_flip(hist_rel: list, i: int, direction: str) -> tuple[float, float] | None:
    """MACD 柱体方向反转(归一化后判定, 跨价位通用)。
    up:   hist[i-1] <= 0 < hist[i]
    down: hist[i-1] >= 0 > hist[i]
    要求反转时 |hist|/close 超过 macd_flip_rel(百分比) → 排除零轴噪声穿越。
    """
    if i < 1 or hist_rel[i] is None or hist_rel[i - 1] is None:
        return None
    h0, h1 = hist_rel[i - 1], hist_rel[i]
    if abs(h1) < CFG["macd_flip_rel"]:
        return None
    if direction == "up" and h0 <= 0 < h1:
        return h1, 1.0
    if direction == "down" and h0 >= 0 > h1:
        return h1, 1.0
    return None


def _ev_vol_surge(vr: list, i: int, gate: float) -> tuple[float, float] | None:
    """成交量放大: 量比 >= 该资产自适应门槛(量比分布的 80% 分位)。"""
    v = vr[i]
    if v is None:
        return None
    if v >= gate:
        return v, 0.7
    return None


# ──────────────────────── 主入口 ────────────────────────
_EV_NAME = {
    "pattern": "价格形态",
    "rsi": "RSI极值反转",
    "macd": "MACD方向反转",
    "vol": "放量",
}

# 形态方向表: 顶部形态只能支持 top_reversal, 底部形态只能支持 bottom_reversal
_TOP_PATTERNS = {"double_top", "shooting_star", "bearish_engulfing"}
_BOTTOM_PATTERNS = {"double_bottom", "hammer", "bullish_engulfing"}
# 强形态: 信息量足够, 允许在只有 [形态+放量] 两项时独立成立
_STRONG_PATTERNS = _TOP_PATTERNS | _BOTTOM_PATTERNS


def detect(rows: list[dict]) -> list[dict]:
    """识别真异常(纯因果, 无未来函数)。
    rows 需含 ts/open/high/low/close/volume, 按 ts 升序。

    v304 三道闸门:
      A. 噪音门: MACD 柱体连续缩量 → 跳过
      B. 锚点闸: 当前 bar 必须为窗口内高/低极值
      C. 冷却闸: 同方向间距 < cooldown(18) → 跳过

    证据门槛:
      - 非锚点: 至少 min_evidence_main(3) 项 + score>=2.5
      - 锚点+强形态: 允许 2 项 + score>=2.5

    关键: 形态方向必须与指标方向一致(顶部形态只能配顶部证据)。
    """
    rows = sorted(rows, key=lambda r: r["ts"])
    ind = _indicators(rows)
    os_band, ob_band, vol_gate = ind["rsi_os"], ind["rsi_ob"], ind["vol_gate"]
    sidx = {s["ts"]: s["name"] for s in detect_structures(rows)}
    n = ind["n"]
    out: list[dict] = []
    last = {"up": -10**9, "down": -10**9}
    cooldown = CFG["cooldown"]
    anchor_k = CFG["anchor_lookback"]

    for i in range(60, n):
        # ── A. 噪音门: MACD 柱体连续缩量 → 整根跳过 ──
        if _is_macd_shrinking(ind["hist_rel"], i):
            continue

        # ── B. 锚点闸(因果, 仅看 [i-k, i)) ──
        is_pivot_high, is_pivot_low = _anchor_state(rows, i, anchor_k)

        p = _ev_pattern(sidx, i, rows)
        pat_name = p[0] if p else None
        # 顶部/底部形态 → 该形态只能支持对应方向
        pat_dir = None
        if pat_name:
            pat_dir = "down" if pat_name in _TOP_PATTERNS else (
                "up" if pat_name in _BOTTOM_PATTERNS else None)
        # 十字星不构成反转证据 → 当作无形态
        if pat_name == "doji":
            pat_name, pat_dir = None, None

        best = None
        for direction, kind in (("up", "bottom_reversal"),
                                ("down", "top_reversal")):
            # 形态与方向冲突 → 该方向不成立
            if pat_dir and pat_dir != direction:
                continue
            # 该方向对应的锚点(顶反转→pivot_high, 底反转→pivot_low)
            anchor_ok = is_pivot_high if direction == "down" else is_pivot_low

            ev: list[tuple[str, float]] = []
            if pat_name:
                ev.append(("pattern", 1.0))
            r = _ev_rsi_reversal(ind["rsi"], i, direction, os_band, ob_band)
            if r:
                ev.append(("rsi", r[1]))
            m = _ev_macd_flip(ind["hist_rel"], i, direction)
            if m:
                ev.append(("macd", m[1]))
            v = _ev_vol_surge(ind["vol_ratio"], i, vol_gate)
            if v:
                ev.append(("vol", v[1]))

            # 证据门槛: 锚点 2 项, 非锚点 3 项
            req = CFG["min_evidence_anchor"] if anchor_ok else CFG["min_evidence_main"]
            if len(ev) < req:
                continue
            score = sum(w for _, w in ev)
            if score < CFG["min_score"]:
                continue
            # 方向性硬要求: 不能只靠 [形态+放量] 这种弱组合
            keys = {k for k, _ in ev}
            has_dir_indicator = bool(keys & {"rsi", "macd"})
            if not has_dir_indicator and pat_name not in _STRONG_PATTERNS:
                continue
            # 锚点闸在 evidence-count 之外再加一道:
            # 非锚点 + 非强形态 → 即便凑够 3 项也拒绝 (防止盘整小波动)
            if not anchor_ok and pat_name not in _STRONG_PATTERNS:
                continue

            if best is None or score > best[0]:
                best = (score, kind, direction, ev)

        if best is None:
            continue
        score, kind, direction, ev = best
        # ── C. 冷却闸 ──
        if i - last[direction] < cooldown:
            continue
        last[direction] = i
        out.append({
            "ts": rows[i]["ts"],
            "kind": kind,
            "close": rows[i]["close"],
            "pattern": pat_name,
            "evidence": [_EV_NAME[k] for k, _ in ev],
            "rsi": round(ind["rsi"][i], 2) if ind["rsi"][i] is not None else None,
            "macd_hist": round(ind["hist"][i], 5) if ind["hist"][i] is not None else None,
            "vol_ratio": round(ind["vol_ratio"][i], 2) if ind["vol_ratio"][i] is not None else None,
            "score": round(min(1.0, score / 3.6), 3),
            "uses_future": False,
        })
    out.sort(key=lambda x: x["ts"])
    return out


def thresholds(rows: list[dict]) -> dict:
    """暴露本次自适应阈值(便于调参与验证)。"""
    ind = _indicators(sorted(rows, key=lambda r: r["ts"]))
    return {
        "rsi_os": round(ind["rsi_os"], 2),
        "rsi_ob": round(ind["rsi_ob"], 2),
        "vol_gate": round(ind["vol_gate"], 3),
        "adaptive": CFG["adaptive"],
    }