"""
P1-B 市场基本面检测 (Market Regime Detection)
==============================================

本模块由两条互补的能力线合并而成 (2026-10 双向同步; 此前同名文件互相覆盖):

① COT / funding regime 检测 (P1-B / P1-D)
基于 CFTCCOT 数据 (cftc_cot_data) 判定 7 类市场 regime:
  1. leverage_buildup       — 机构开杠杆 (OI 单周↑5% + 多头持仓↑)
  2. deleveraging           — 套利离场/去杠杆 (OI 单周↓5% + 多头持仓↓)
  3. large_spec_long        — 机构大资金做多 (净多持仓 > 历史 80% 分位)
  4. large_spec_short       — 机构大资金做空 (净空持仓 < 历史 20% 分位)
  5. commercial_hedging     — 产业套保激增 (commercial_net 周变化 > 0.05)
  6. risk_off_rotation      — 集体撤资到避险 (crypto OI↓ & 贵金属 OI↑)
  7. spec_extreme           — 极端投机 (净持仓处于历史 95% 分位以上)

P1-D 新增 2 类 funding rate 衍生 regime (基于 Binance 永续合约资金费率):
  8. funding_extreme_long   — 资金费率极正 (current rate ≥ 0.03%/8h ≈ 33% APR+)
  9. funding_extreme_short  — 资金费率极负 (current rate ≤ -0.03%/8h)

输入: cftc_cot_data 表 (13 个资产 × 156 周) + Binance 永续合约 funding rate
输出: dict 含 regimes 列表 + 每 regime 涉及资产 + score + 历史百分位
主入口: detect_regimes()

② 价格-资金背离 (纯函数, 零网络依赖)
  detect_price_fund_divergence() 把「价格方向 vs 资金方向」摆在一起做背离判定，
  供 decision_guard 在方向门槛上做「松绑/翻转」。

  背景: decision_guard 里的 trend 因子是「过去窗口期涨跌 + 连续同向 K 线数」的
  滞后确认, 趋势反转初期仍输出旧方向 (Strong Bull/Bull), 把 AI 抓到的反向信号
  抵消成 HOLD。资金面 (永续资金费率 / 基差 / 持仓) 更灵敏, 能在价格还没掉头时
  先给出领先信号。

  边界: 只做静态方向背离 (当前费率方向 vs 当前价格趋势), 不依赖历史费率序列;
  basis_bps / oi_change_pct 作为可选增强, 缺失时自动跳过对应信号。
  数据由上层 (market_snapshot / macro_context) 拉取后传入。
"""
from __future__ import annotations
import sqlite3
import time
from typing import Any, Dict, List, Optional

# 单个币种的资金费率取数预算 (秒)。这是「每个币种」的上限, 不是整轮上限 ——
# 整轮上限是下面定义的 FUNDING_BUDGET_S。名字必须区分开, 否则两个常量互相覆盖。
try:
    from funding_rate import FUNDING_BUDGET_S as PER_SYMBOL_FUNDING_BUDGET_S  # type: ignore
except ImportError:  # pragma: no cover — 模块缺失时用保守默认值
    PER_SYMBOL_FUNDING_BUDGET_S = 2.0

# Regime 判定阈值
LEVERAGE_OI_THRESHOLD = 0.05   # OI 周变化 ±5% 触发杠杆/去杠杆
COMM_HEDGE_THRESHOLD = 0.05    # 产业套保单周变化 > 5% OI 触发
FUNDING_EXTREME_LONG = 0.0003  # funding rate > 0.03%/8h 触发 long_pay 极端 (≈33% APR)
FUNDING_EXTREME_SHORT = -0.0003  # funding rate < -0.03%/8h 触发 short_pay 极端
SPEC_PERCENTILE_LONG = 0.80    # 大资金做多阈值 (历史 80% 分位)
SPEC_PERCENTILE_SHORT = 0.20   # 大资金做空阈值 (历史 20% 分位)
SPEC_EXTREME_PCTL = 0.95       # 极端投机阈值 (95% 分位)
RISK_OFF_CUTOFF = "2024-01-01" # 历史百分位计算窗口
MIN_REPORTS = 20               # 至少 20 周历史才能判定 percentile
# v308 P1: funding (Binance 外部网络) 的总墙钟预算 —— 超时就不参与判定,
# 不能让外部网络把整个 /v291/market-regime 请求拖死。
FUNDING_BUDGET_S = 6.0


def _safe_pct(numerator, denom) -> float:
    if numerator is None or denom is None or denom == 0:
        return 0.0
    return float(numerator) / float(denom)


def _percentile_rank(value: float, history: list) -> float:
    """返回 value 在 history 中的百分位 (0-1, 越高越极端)."""
    if not history or value is None:
        return 0.5
    below = sum(1 for h in history if h is not None and h < value)
    return below / len(history)


def _fetch_cot_rows(conn: sqlite3.Connection, asset: str | None = None,
                    since_date: str = "2024-01-01") -> list:
    """拉取 COT 数据, 按 asset+report_date 倒序."""
    if asset:
        rows = conn.execute(
            "SELECT asset, report_date, open_interest, change_oi, "
            "noncomm_net, comm_net, spec_index, pct_oi_noncomm_long, pct_oi_noncomm_short "
            "FROM cftc_cot_data WHERE asset=? AND report_date >= ? ORDER BY report_date DESC",
            (asset, since_date),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT asset, report_date, open_interest, change_oi, "
            "noncomm_net, comm_net, spec_index, pct_oi_noncomm_long, pct_oi_noncomm_short "
            "FROM cftc_cot_data WHERE report_date >= ? ORDER BY asset, report_date DESC",
            (since_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def _asset_history_spec_pct(asset_rows: list) -> list:
    """提取某资产历史 noncomm_net / open_interest 时间序列 (ASC)."""
    return [_safe_pct(r["noncomm_net"], r["open_interest"])
            for r in reversed(asset_rows) if r["open_interest"]]


def _detect_leverage_buildup(asset_rows: list) -> dict | None:
    """regime 1+2: leverage_buildup / deleveraging (最近 1 周 OI 变化)."""
    if not asset_rows:
        return None
    latest = asset_rows[0]
    if latest["open_interest"] is None or latest["change_oi"] is None:
        return None
    oi_chg_pct = _safe_pct(latest["change_oi"], latest["open_interest"])
    if abs(oi_chg_pct) < LEVERAGE_OI_THRESHOLD:
        return None
    prev = asset_rows[1] if len(asset_rows) > 1 else None
    spec_now = _safe_pct(latest["noncomm_net"], latest["open_interest"])
    spec_prev = _safe_pct(prev["noncomm_net"], prev["open_interest"]) if prev else 0
    spec_delta = spec_now - spec_prev
    regime = "leverage_buildup" if oi_chg_pct > 0 else "deleveraging"
    return {
        "asset": latest["asset"],
        "regime": regime,
        "report_date": latest["report_date"],
        "oi_chg_pct": round(oi_chg_pct * 100, 2),
        "spec_net_pct": round(spec_now * 100, 2),
        "spec_delta_pct": round(spec_delta * 100, 2),
        "intensity": "strong" if abs(oi_chg_pct) > 0.10 else "moderate",
        "score": min(1.0, abs(oi_chg_pct) / 0.20),
    }


def _detect_large_spec(asset_rows: list) -> dict | None:
    """regime 3+4: large_spec_long / large_spec_short (基于历史百分位)."""
    if len(asset_rows) < MIN_REPORTS:
        return None
    history = _asset_history_spec_pct(asset_rows)
    latest = asset_rows[0]
    spec_now = _safe_pct(latest["noncomm_net"], latest["open_interest"])
    pctl = _percentile_rank(spec_now, history)
    if pctl >= SPEC_PERCENTILE_LONG:
        regime = "large_spec_long"
    elif pctl <= SPEC_PERCENTILE_SHORT:
        regime = "large_spec_short"
    else:
        return None
    return {
        "asset": latest["asset"],
        "regime": regime,
        "report_date": latest["report_date"],
        "spec_net_pct": round(spec_now * 100, 2),
        "percentile": round(pctl * 100, 1),
        "intensity": "extreme" if pctl >= SPEC_EXTREME_PCTL or pctl <= (1 - SPEC_EXTREME_PCTL) else "moderate",
        "score": abs(pctl - 0.5) * 2,
    }


def _detect_commercial_hedging(asset_rows: list) -> dict | None:
    """regime 5: commercial_hedging (产业套保单周变化 > 5% OI)."""
    if len(asset_rows) < 2:
        return None
    latest = asset_rows[0]
    prev = asset_rows[1]
    if latest["open_interest"] is None or prev["open_interest"] is None:
        return None
    comm_now = _safe_pct(latest["comm_net"], latest["open_interest"])
    comm_prev = _safe_pct(prev["comm_net"], prev["open_interest"])
    comm_delta = comm_now - comm_prev
    if abs(comm_delta) < COMM_HEDGE_THRESHOLD:
        return None
    direction = "increasing" if comm_delta > 0 else "decreasing"
    return {
        "asset": latest["asset"],
        "regime": "commercial_hedging",
        "report_date": latest["report_date"],
        "comm_delta_pct": round(comm_delta * 100, 2),
        "comm_now_pct": round(comm_now * 100, 2),
        "direction": direction,
        "intensity": "strong" if abs(comm_delta) > 0.10 else "moderate",
        "score": min(1.0, abs(comm_delta) / 0.20),
    }


def _detect_risk_off_rotation(all_rows_by_asset: dict) -> dict | None:
    """regime 6: risk_off_rotation (crypto OI↓ + 贵金属 OI↑)."""
    crypto_assets = {"BTC", "ETH"}
    safe_assets = {"GOLD", "SILVER"}
    crypto_latest = [all_rows_by_asset[a][0] for a in crypto_assets
                     if a in all_rows_by_asset and all_rows_by_asset[a]]
    safe_latest = [all_rows_by_asset[a][0] for a in safe_assets
                   if a in all_rows_by_asset and all_rows_by_asset[a]]
    if not crypto_latest or not safe_latest:
        return None
    crypto_oi_chg = sum(_safe_pct(r["change_oi"], r["open_interest"])
                        for r in crypto_latest) / len(crypto_latest)
    safe_oi_chg = sum(_safe_pct(r["change_oi"], r["open_interest"])
                      for r in safe_latest) / len(safe_latest)
    if crypto_oi_chg > -LEVERAGE_OI_THRESHOLD or safe_oi_chg < 0.03:
        return None
    return {
        "regime": "risk_off_rotation",
        "report_date": max(r["report_date"] for r in crypto_latest + safe_latest),
        "crypto_oi_chg_pct": round(crypto_oi_chg * 100, 2),
        "safe_oi_chg_pct": round(safe_oi_chg * 100, 2),
        "assets_crypto": [r["asset"] for r in crypto_latest],
        "assets_safe": [r["asset"] for r in safe_latest],
        "intensity": "strong" if crypto_oi_chg < -0.10 and safe_oi_chg > 0.10 else "moderate",
        "score": min(1.0, (-crypto_oi_chg + safe_oi_chg) / 0.30),
    }


def _detect_spec_extreme(asset_rows: list) -> list:
    """regime 7: spec_extreme (净持仓 > 95% 分位) — 可能反转信号."""
    if len(asset_rows) < MIN_REPORTS:
        return []
    history = _asset_history_spec_pct(asset_rows)
    latest = asset_rows[0]
    spec_now = _safe_pct(latest["noncomm_net"], latest["open_interest"])
    pctl = _percentile_rank(spec_now, history)
    if pctl < SPEC_EXTREME_PCTL and pctl > (1 - SPEC_EXTREME_PCTL):
        return []
    side = "long" if spec_now > 0 else "short"
    return [{
        "asset": latest["asset"],
        "regime": "spec_extreme",
        "report_date": latest["report_date"],
        "side": side,
        "spec_net_pct": round(spec_now * 100, 2),
        "percentile": round(pctl * 100, 1),
        "score": 1.0,
        "intensity": "extreme",
    }]


def _detect_funding_extremes(budget_s: float = FUNDING_BUDGET_S) -> tuple[list, dict]:
    """regime 8/9: funding_extreme_long/short (基于 Binance 永续合约资金费率).

    拉取 BTCUSDT/ETHUSDT/SOLUSDT/BNBUSDT 的当前资金费率;
    若 ≥ FUNDING_EXTREME_LONG (≈33% APR) ⇒ funding_extreme_long
    若 ≤ FUNDING_EXTREME_SHORT               ⇒ funding_extreme_short

    v308 P1 (总墙钟预算):
      Binance 不可达时, 原来 4 个币种串行取数最坏能拖 1~2 分钟, 把
      /v291/market-regime 整条请求拖过前端 30s 超时 → 面板永远空白。
      现在预算用完就停, 没取到的币种记进 skipped 并回报给前端;
      COT 的 7 类 regime 照常返回 —— 少一类判断好过整页卡死。
    """
    try:
        from funding_rate import get_funding_metrics
    except ImportError:
        return [], {"ok": False, "error": "funding_rate module unavailable",
                    "checked": [], "skipped": [], "stale": [],
                    "note": "funding_rate 模块缺失, 资金面未参与判定"}
    out: list = []
    checked: list[str] = []
    skipped: list[str] = []
    stale: list[str] = []
    deadline = time.monotonic() + float(budget_s)
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            skipped.append(sym)
            continue
        try:
            # v308 P1-bugfix: 把「本轮剩余时间」传给下层, 而不是让每个币种
            # 都各自用满 5s (4×5=20s ≫ 6s 预算)。这样总墙钟才真的受 budget_s 约束。
            m = get_funding_metrics(sym, budget_s=min(remaining, PER_SYMBOL_FUNDING_BUDGET_S))
        except Exception:
            skipped.append(sym)
            continue
        if not m or not m.get("ok"):
            skipped.append(sym)
            continue
        # v308 P1-bugfix (诚实性): 外部源不可达时 funding_rate 会回退到**上一次
        # 成功取到的缓存**, 并打 stale=True 标记。那是几十分钟前的快照, 不是
        # "当前资金费率"。之前这里不看 stale, 直接拿它判定 extreme ——
        # 等于拿旧数据当现在的事实, 前端还会显示"资金费率极端"的结论。
        # 现在: 陈旧数据单独记进 stale, **不参与本轮判定**。
        if m.get("stale"):
            stale.append(sym)
            continue
        checked.append(sym)
        cur = m.get("current_rate", 0.0)
        ann = m.get("annualized_current_pct", 0.0)
        ts = m.get("ts")
        # v308 P1-bugfix (类型): report_date 全链路是日期字符串 (COT 那 7 类就是
        # "2026-10-21")。这里原来塞 epoch 整数, 前端 "as of {report_date}" 会
        # 直接渲染成 1792820787。
        report_date = None
        try:
            report_date = time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
        except (TypeError, ValueError, OSError):
            report_date = None
        if cur >= FUNDING_EXTREME_LONG:
            out.append({
                "asset": sym,
                "regime": "funding_extreme_long",
                "report_date": report_date,
                "ts": ts,
                "side": "long_pay",
                "current_rate": round(cur, 6),
                "annualized_pct": round(ann, 2),
                "score": min(1.0, abs(cur) / 0.001),
                "intensity": "extreme",
                "data_source": "binance_fapi_fundingRate",
            })
        elif cur <= FUNDING_EXTREME_SHORT:
            out.append({
                "asset": sym,
                "regime": "funding_extreme_short",
                "report_date": report_date,
                "ts": ts,
                "side": "short_pay",
                "current_rate": round(cur, 6),
                "annualized_pct": round(ann, 2),
                "score": min(1.0, abs(cur) / 0.001),
                "intensity": "extreme",
                "data_source": "binance_fapi_fundingRate",
            })
    status = {
        "ok": True,
        "budget_s": budget_s,
        "checked": checked,
        "skipped": skipped,
        "stale": stale,
        "note": ("checked = 本轮取到实时数据并参与判定; "
                 "skipped = 因超时预算/取数失败未取到的币种; "
                 "stale = 只拿到历史缓存 (外部源不可达), 未参与判定 —— "
                 "不影响 COT 7 类 regime"),
    }
    return out, status


def detect_regimes(conn: sqlite3.Connection, since_date: str = RISK_OFF_CUTOFF) -> dict:
    """主入口: 检测所有 12 类 regime (9 类 COT/funding + 3 类 v321.3 新增)。

    v321.3 P1: 进程内 TTL 缓存。端点有 20s 硬超时, 而冷缓存全量计算实测
    6~34s (随 DB page cache 与外部源可达性波动) —— 冷启动时经常超时, 超时
    之后 stale 兜底又因为"从没成功算过一次"而拿不到数据 → 面板整块空白。
    有了这层缓存: 首次成功后 15 分钟内都是毫秒级, 兜底缓存也因此有了存量。
    """
    global _REGIME_MEM_CACHE, _REGIME_MEM_CACHE_TS
    now_mono = time.monotonic()
    if (_REGIME_MEM_CACHE and _REGIME_MEM_CACHE.get("_since") == since_date
            and (now_mono - _REGIME_MEM_CACHE_TS) < REGIME_MEM_CACHE_TTL_S):
        out = dict(_REGIME_MEM_CACHE)
        out.pop("_since", None)
        out["cached"] = True
        out["cache_age_sec"] = round(now_mono - _REGIME_MEM_CACHE_TS, 1)
        return out
    result = _detect_regimes_uncached(conn, since_date)
    _REGIME_MEM_CACHE = dict(result)
    _REGIME_MEM_CACHE["_since"] = since_date
    _REGIME_MEM_CACHE_TS = now_mono
    out = dict(result)
    out.pop("_since", None)
    out["cached"] = False
    return out


def _detect_regimes_uncached(conn: sqlite3.Connection,
                             since_date: str = RISK_OFF_CUTOFF) -> dict:
    """真正干活的版本 (不做缓存)。"""
    all_rows = _fetch_cot_rows(conn, asset=None, since_date=since_date)
    if not all_rows:
        # v308 P1-bugfix: 这条路径原来没有 funding 字段, 前端读 data.funding.skipped
        # 会拿到 undefined。补一个结构一致的空状态, 让前端契约始终成立。
        return {
            "ok": False, "error": "no_cot_data",
            "regimes": [], "by_asset": {},
            "funding": {"ok": False, "checked": [], "skipped": [], "stale": [],
                        "note": "无 COT 数据, 整体未计算"},
        }

    by_asset: dict = {}
    for r in all_rows:
        by_asset.setdefault(r["asset"], []).append(r)

    regimes: list = []
    for asset, rows in by_asset.items():
        lv = _detect_leverage_buildup(rows)
        if lv:
            regimes.append(lv)
        ls = _detect_large_spec(rows)
        if ls:
            regimes.append(ls)
        ch = _detect_commercial_hedging(rows)
        if ch:
            regimes.append(ch)
        regimes.extend(_detect_spec_extreme(rows))

    ro = _detect_risk_off_rotation(by_asset)
    if ro:
        regimes.append(ro)

    # v308 P1: funding 检测带总预算, 返回 (regimes, status) —— status 里带
    # checked / skipped, 前端能如实知道"哪几个币种这轮没取到"。
    funding_regimes, funding_status = _detect_funding_extremes()
    regimes.extend(funding_regimes)

    # v321.3 P0: 新增 3 类 —— 集中投资 AI / 撤离 AI / 国家基金级资金入场 (需求 #14)
    # 每个检测器都自带 try 兜底 + 数据不足就**不产出** (宁可没有, 也不编)。
    try:
        regimes.extend(_detect_ai_theme_regimes(conn))
    except Exception as exc:
        print(f"[REGIME] ai_theme 检测跳过: {type(exc).__name__}: {exc}", flush=True)
    try:
        regimes.extend(_detect_state_fund_inflow(conn))
    except Exception as exc:
        print(f"[REGIME] state_fund 检测跳过: {type(exc).__name__}: {exc}", flush=True)
    try:
        regimes.extend(_detect_leverage_overheat(conn))
    except Exception as exc:
        print(f"[REGIME] leverage_overheat 检测跳过: {type(exc).__name__}: {exc}", flush=True)
    try:
        regimes.extend(_detect_cn_state_fund_flow(conn))
    except Exception as exc:
        print(f"[REGIME] cn_state_fund 检测跳过: {type(exc).__name__}: {exc}", flush=True)

    regimes.sort(key=lambda r: r.get("score", 0), reverse=True)

    asset_summary: dict = {}
    for asset, rows in by_asset.items():
        if not rows:
            continue
        latest = rows[0]
        prev = rows[1] if len(rows) > 1 else None
        spec_now = _safe_pct(latest["noncomm_net"], latest["open_interest"])
        spec_prev = _safe_pct(prev["noncomm_net"], prev["open_interest"]) if prev else 0
        oi_chg = _safe_pct(latest["change_oi"], latest["open_interest"])
        asset_summary[asset] = {
            "report_date": latest["report_date"],
            "open_interest": latest["open_interest"],
            "oi_chg_pct": round(oi_chg * 100, 2),
            "spec_net_pct": round(spec_now * 100, 2),
            "spec_delta_pct": round((spec_now - spec_prev) * 100, 2),
        }

    return {
        "ok": True,
        "n_regimes": len(regimes),
        "regimes": regimes,
        "by_asset": asset_summary,
        "since_date": since_date,
        "n_assets": len(by_asset),
        "funding": funding_status,
    }


if __name__ == "__main__":
    import json
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else "trident_event_bus.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = detect_regimes(conn)
    print(json.dumps(result, indent=2, ensure_ascii=False))


# ─── 价格-资金背离 (纯函数) ──────────────────────────
def _clamp(v: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(v)))
    except (TypeError, ValueError):
        return 0.0


def detect_price_fund_divergence(
    *,
    trend: str = "",
    funding_rate_pct: float = 0.0,
    basis_bps: Optional[float] = None,
    oi_change_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """判定价格 vs 资金的方向背离。

    参数:
      trend            价格趋势标签 (_compute_period_stats / stats_7d.trend)
      funding_rate_pct 永续资金费率 (%, 正=多头付溢价, 负=空头主导)
      basis_bps        基差 (bp, 正=升水, 负=贴水), 可选
      oi_change_pct    持仓变化 (%, 正=增仓, 负=减仓), 可选

    返回:
      divergence "bearish"|"bullish"|None   bearish=顶背离(价格涨但资金空),
                                            bullish=底背离(价格跌但资金多)
      strength   0~1 背离强度 (费率幅度归一化 + 多信号加成)
      signals    命中的背离信号描述
      regime     机器标签
      verdict    人读说明
    """
    t = (trend or "").lower()
    price_up = "bull" in t
    price_down = "bear" in t

    try:
        fr = float(funding_rate_pct)
    except (TypeError, ValueError):
        fr = 0.0

    divergence: Optional[str] = None
    signals: List[str] = []

    # ① 费率背离 (核心信号)
    if price_up and fr < 0:
        divergence = "bearish"
        signals.append(f"价格偏强({trend})但资金费率转负 {fr * 100:.4f}%")
    elif price_down and fr > 0:
        divergence = "bullish"
        signals.append(f"价格偏弱({trend})但资金费率转正 +{fr * 100:.4f}%")

    # ② 基差背离 (可选增强)
    if basis_bps is not None:
        try:
            bps = float(basis_bps)
        except (TypeError, ValueError):
            bps = None
        if bps is not None:
            if price_up and bps < 0:
                divergence = "bearish"
                signals.append(f"基差转负 {bps:.1f}bp (期货贴水)")
            elif price_down and bps > 0:
                divergence = "bullish"
                signals.append(f"基差转正 +{bps:.1f}bp (期货升水)")

    # ③ 持仓背离 (可选增强)
    if oi_change_pct is not None:
        try:
            oi = float(oi_change_pct)
        except (TypeError, ValueError):
            oi = None
        if oi is not None:
            if price_up and oi < 0:
                divergence = "bearish"
                signals.append(f"持仓下降 {oi:.1f}%")
            elif price_down and oi > 0:
                divergence = "bullish"
                signals.append(f"持仓上升 +{oi:.1f}%")

    if divergence is None:
        return {
            "divergence": None,
            "strength": 0.0,
            "signals": [],
            "regime": "neutral",
            "verdict": "价格与资金方向一致，无背离",
        }

    fr_strength = _clamp(abs(fr) / 0.1)      # 0.1% 费率 → 满强度
    extra = 0.15 * max(0, len(signals) - 1)  # 每多一条信号 +0.15
    strength = _clamp(fr_strength + extra)

    return {
        "divergence": divergence,
        "strength": round(strength, 3),
        "signals": signals,
        "regime": f"price_fund_{divergence}",
        "verdict": "；".join(signals),
    }


# ─────────────────────────────────────────────────────────────────────────────
# v321.3 P0: 新增 3 类 regime —— 集中投资 AI / 撤离 AI / 国家基金级资金入场
# ─────────────────────────────────────────────────────────────────────────────
# 背景 (需求 #14 的实测缺口)
#   capability_gate.py:439 自己写着「国家基金 / AI主题资金流 没有对应 regime
#   (逻辑层就缺)」—— 全库 grep 不到任何相关代码。这三类恰恰是用户最关心的
#   「谁在买 / 谁在撤」。
#
# 诚实边界 (v282+ NEVER_FABRICATE)
#   · 「集中投资 AI」「撤离 AI」: **直接可观测** —— AI 硬件 basket 相对广谱的
#     超额收益 + crowd 题材日热度的动量, 全部来自真实落库的行情/题材数据。
#   · 「国家基金」: **国家基金的账户是观测不到的**, 直接断言就是编造。
#     这里用**可观测的代理信号**: ETF 净流入 (btc_etf_net) 异常放量 +
#     总持仓 (agg.oi) 上升 + 大户多头占比 (BTC.ig_long_pct) 上升。
#     ETF / 保险 / 社保 / 国家队走的是同一条"通道型大额资金"路径, 所以输出里
#     强制带 proxy=true + evidence 明说这是代理推断, 前端必须照原样展示。

AI_THEME_ASSETS = ("MU", "NVDA", "SKHYNIX", "000660.KS")  # AI 资本开支承载标的
BROAD_BASKET_ASSETS = ("XAU", "WTI")                       # 广谱对照
AI_THEME_TOPIC_KEY = "theme:科技/AI"     # crowd_topic_daily 实测存在, 60 条日度

REGIME_LOOKBACK_D = 60      # 分位计算窗口 (天)
EXCESS_WINDOW_D = 20        # 超额收益计算窗口 (天)
CONCENTRATION_PCTL = 0.80   # 超额收益高分位 → 集中投资 AI
EXIT_PCTL = 0.20            # 超额收益低分位 → 撤离 AI
AI_EXCESS_MIN_PCT = 1.0     # 超额收益绝对门槛 (%), 避免噪音

# 变化率可信上限 (v321.3 P1): 超过就是**口径跳变**而不是市场变化。
# 实测 agg.oi 换口径时 (Coinglass 付费墙 → 免费单所替代) 值差 367 倍。
MAX_PLAUSIBLE_CHANGE_PCT = 100.0

# ── v321.3 P1: 进程内 TTL 缓存 (见 detect_regimes 的说明) ─────────────────────
_REGIME_MEM_CACHE: Dict[str, Any] = {}
_REGIME_MEM_CACHE_TS = 0.0
REGIME_MEM_CACHE_TTL_S = 900.0   # 15 分钟


def _daily_closes(conn: sqlite3.Connection, asset: str, limit: int = 200) -> List[tuple]:
    """取日线收盘序列 [(ts, close)] (升序)。取不到返回空表。"""
    try:
        rows = conn.execute(
            "SELECT ts, close FROM ohlcv_daily WHERE asset=? AND close IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?",
            (asset.upper(), int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return []
    out = [(int(r[0]), float(r[1])) for r in rows if r[0] is not None and r[1]]
    out.reverse()
    return out


def _pct_return(series: List[tuple], window: int) -> Optional[float]:
    """窗口收益率 (%)。数据不足返回 None (不是 0)。"""
    if len(series) < window + 1:
        return None
    base = series[-window - 1][1]
    last = series[-1][1]
    if not base:
        return None
    return (last / base - 1.0) * 100.0


def _daily_by_date(conn: sqlite3.Connection, asset: str,
                   limit: int = 400) -> Dict[str, float]:
    """取日线 → {UTC 日期字符串: 收盘价}。

    为什么按**日历日**而不是 ts 对齐:
      MU/NVDA/SKHYNIX 的 ts 是美股收盘时刻 (间隔 18000/68400 秒, 不规则),
      XAU/WTI 是期货结算时刻 (间隔 28800/57600 秒)。同一"交易日"的 ts
      **不相等** —— 实测按 ts 相等只能对上 16 天, 按日历日对齐能对上数百天。
    同一天多条时取 ts 最大的 (最新收盘)。
    """
    try:
        rows = conn.execute(
            "SELECT ts, close FROM ohlcv_daily WHERE asset=? AND close IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?",
            (asset.upper(), int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: Dict[str, float] = {}
    for r in rows:
        try:
            if r[0] is None or not r[1]:
                continue
            ts = int(r[0])
            day = time.strftime("%Y-%m-%d", time.gmtime(ts))
            out[day] = float(r[1])      # DESC 排序 → 后来的覆盖先来的 = 最新收盘
        except (TypeError, ValueError, OSError):
            continue
    return out


def _basket_daily_returns(conn: sqlite3.Connection, assets: tuple,
                           window: int) -> Dict[str, float]:
    """每个成分算窗口日收益, 再取**当日可用成分的中位数** → {日期: basket 收益%}。

    为什么按"当日可用"而不是取所有成分的日期交集:
      000660.KS 只有 28 根日线, 取交集会把整个 basket 压到 8 天。
      中位数本来就抗单个成分缺失, 按日取可用成分的中位数样本量最大。
    """
    per: List[Dict[str, float]] = []
    for a in assets:
        s = _daily_by_date(conn, a)
        if len(s) < window + 2:
            continue
        days = sorted(s.keys())
        rets: Dict[str, float] = {}
        for i in range(window, len(days)):
            d0, d1 = days[i - window], days[i]
            b = s[d0]
            if not b:
                continue
            rets[d1] = (s[d1] / b - 1.0) * 100.0
        if len(rets) >= 5:
            per.append(rets)
    if not per:
        return {}
    all_days = set()
    for p in per:
        all_days |= set(p.keys())
    out: Dict[str, float] = {}
    for d in all_days:
        vals = sorted(p[d] for p in per if d in p)
        if not vals:
            continue
        mid = len(vals) // 2
        out[d] = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
    return out


def _excess_series(conn: sqlite3.Connection) -> List[tuple]:
    """AI basket 相对广谱 basket 的**每日超额收益**序列 [(日期, excess%)]。"""
    ai = _basket_daily_returns(conn, AI_THEME_ASSETS, EXCESS_WINDOW_D)
    broad = _basket_daily_returns(conn, BROAD_BASKET_ASSETS, EXCESS_WINDOW_D)
    if len(ai) < 10 or len(broad) < 10:
        return []
    common = sorted(set(ai.keys()) & set(broad.keys()))
    return [(d, ai[d] - broad[d]) for d in common]


def _ai_heat_momentum(conn: sqlite3.Connection) -> Optional[float]:
    """AI 题材热度动量 = 最近3日均值 / 前7日均值 - 1。取不到返回 None。"""
    try:
        rows = conn.execute(
            "SELECT day, SUM(heat) h FROM crowd_topic_daily "
            "WHERE topic_key=? GROUP BY day ORDER BY day DESC LIMIT 14",
            (AI_THEME_TOPIC_KEY,),
        ).fetchall()
    except sqlite3.Error:
        return None
    if len(rows) < 10:
        return None
    vals = [float(r[1] or 0.0) for r in rows]
    recent, base = vals[:3], vals[3:10]
    if not recent or not base:
        return None
    rb = sum(base) / len(base)
    if rb <= 0:
        return None
    return (sum(recent) / len(recent)) / rb - 1.0


def _detect_ai_theme_regimes(conn: sqlite3.Connection) -> List[dict]:
    """集中投资 AI / 撤离 AI —— 基于 AI basket 相对广谱的超额收益分位。"""
    out: List[dict] = []
    series = _excess_series(conn)
    if len(series) < 10:
        return out
    vals = [v for _d, v in series]
    latest = vals[-1]
    look = vals[-REGIME_LOOKBACK_D:] if len(vals) >= REGIME_LOOKBACK_D else vals
    pctl = _percentile_rank(latest, look)
    heat_mom = _ai_heat_momentum(conn)
    report_day = series[-1][0]
    heat_txt = ("%.1f%%" % (heat_mom * 100)) if heat_mom is not None else "n/a"
    common = {
        "report_date": report_day,
        "assets": list(AI_THEME_ASSETS),
        "excess_pct": round(latest, 2),
        "excess_percentile": round(pctl * 100, 1),
        "heat_momentum": round(heat_mom * 100, 1) if heat_mom is not None else None,
        "sample_size": len(series),
        "proxy": False,
        "data_source": "ohlcv_daily AI basket vs 广谱 basket 超额收益分位 + crowd 题材热度",
    }
    # ── 判定: 「极值突破」OR「分位低位」—— 两个判据各补对方短板 ─────────────
    # 极值突破: 趋势型序列里分位恒在中位 (实测 AI 稳定跑赢时 pctl≈0), 分位法失灵;
    #           但"资金持续涌入"本身就是一路走高 → 逼近日内高点能抓住。
    # 分位:     真实数据里超额是震荡型 (有涨有跌), 这时"当前值在自身历史低位"
    #           才是真正的撤离信号, 极值判据反而太严 (真实数据 -22.61% 距
    #           低点 -59.36% 还远, 但它已处 13 分位)。
    # 两者取并集: 任一成立即判定, 覆盖「趋势」与「震荡」两种形态。
    window_vals = look
    hi = max(window_vals)
    lo = min(window_vals)
    near_high = bool(window_vals) and latest >= hi * 0.90
    near_low = bool(window_vals) and latest <= lo * 0.90
    pctl_high = pctl >= CONCENTRATION_PCTL
    pctl_low = pctl <= EXIT_PCTL

    common["excess_period_high"] = round(hi, 2)
    common["excess_period_low"] = round(lo, 2)
    common["near_period_high"] = near_high
    common["near_period_low"] = near_low
    common["percentile_high"] = pctl_high
    common["percentile_low"] = pctl_low

    if (near_high or pctl_high) and latest >= AI_EXCESS_MIN_PCT:
        out.append(dict(common, **{
            "regime": "ai_theme_concentration",
            "intensity": "extreme" if (near_high and pctl_high) else "moderate",
            "score": round(min(1.0, max(0.0, latest) / max(abs(hi), 1.0)), 3),
            "evidence": (f"AI basket 近 {EXCESS_WINDOW_D} 日超额 {latest:.2f}% "
                         f"(区间 {lo:.2f}% ~ {hi:.2f}%, {pctl*100:.0f} 分位); "
                         f"题材热度动量 {heat_txt}"),
        }))
    elif (near_low or pctl_low) and latest <= -AI_EXCESS_MIN_PCT:
        out.append(dict(common, **{
            "regime": "ai_theme_exit",
            "intensity": "extreme" if (near_low and pctl_low) else "moderate",
            "score": round(min(1.0, max(0.0, -latest) / max(abs(lo), 1.0)), 3),
            "evidence": (f"AI basket 近 {EXCESS_WINDOW_D} 日超额 {latest:.2f}% "
                         f"(区间 {lo:.2f}% ~ {hi:.2f}%, {pctl*100:.0f} 分位) — 资金撤离特征"),
        }))
    return out


def _macro_series(conn: sqlite3.Connection, metric_key: str, limit: int = 120) -> List[tuple]:
    """取 macro_snapshots 里某个 metric_key 的时间序列 [(ts, value)] (升序)。

    **只取 status='ok' 的行** —— macro_snapshots 里大量行的 status='unavailable'
    (value=NULL, payload 里记着原因, 如「未配置 SOSOVALUE_ETF_URL」/「IG 未登录」)。
    把它们当 0 参与统计就是在编数据, 所以直接过滤掉。
    """
    try:
        rows = conn.execute(
            "SELECT ts, value FROM macro_snapshots "
            "WHERE metric_key=? AND status='ok' AND value IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?",
            (metric_key, int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        try:
            out.append((int(r[0]), float(r[1])))
        except (TypeError, ValueError):
            continue
    out.reverse()
    return out


def _macro_daily_series(conn: sqlite3.Connection, metric_key: str,
                        limit: int = 400) -> List[tuple]:
    """按 **UTC 日**去重的宏观序列 [(日期, 值)] (升序)。

    为什么必须去重: FRED 类指标 (tga / margin_debt / rrp_rate) 是**日频**的, 但
    采集器每轮都把当天的值再写一遍 —— 实测最近 6 个采样点值**完全相同**
    (957175.0 × 6)。直接用采样点算变化率永远是 0%, 判定就成了摆设。
    这里按天取**当天最后一个**值, 变化率才有意义。
    """
    raw = _macro_series(conn, metric_key, limit=limit * 3)
    by_day: Dict[str, tuple] = {}
    for ts, v in raw:
        try:
            day = time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
        except (TypeError, ValueError, OSError):
            continue
        by_day[day] = (day, v)          # raw 已升序 → 后来的覆盖 = 当天最后
    return [by_day[d] for d in sorted(by_day.keys())]


def _macro_daily_change(series: List[tuple], back_days: int = 1) -> Optional[float]:
    """日级变化率 (%)。数据不足返回 None。"""
    if len(series) < back_days + 1:
        return None
    now_v = series[-1][1]
    prev_v = series[-1 - back_days][1]
    if not prev_v:
        return None
    return (now_v / prev_v - 1.0) * 100.0


def _detect_leverage_overheat(conn: sqlite3.Connection) -> List[dict]:
    """杠杆过热 / 套利离场风险 (需求 #14 原文: 「某个金融产品杠杆太多了,
    有套利离场的可能性评估」)。

    判据 (全部真实观测):
      · margin_debt (融资余额) 处于历史高分位 → 杠杆已经堆高
      · margin_debt 日级快速上升            → 还在加杠杆 (拥挤度上升)
      · hy_oas (高收益利差) 走阔           → 风险偏好转弱, 杠杆最脆弱的时候
      · agg.liquidation (强平额) 抬升      → 已经开始被动去杠杆
    前两条成立 → 判定"杠杆过热"; 再叠加风险信号 → 套利离场风险升高。

    诚实边界: 这是**风险预警**, 不是崩盘预测。触发不等于会崩 —— 它说明
    「一旦反向, 被平仓的规模会比平时大」。
    """
    out: List[dict] = []
    md = _macro_daily_series(conn, "margin_debt")
    if len(md) < 30:
        return out
    vals = [v for _d, v in md]
    now_v = vals[-1]
    pctl = _percentile_rank(now_v, vals[-REGIME_LOOKBACK_D:]
                             if len(vals) >= REGIME_LOOKBACK_D else vals)
    chg = _macro_daily_change(md, back_days=5)
    if chg is None:
        return out

    oas = _macro_daily_series(conn, "hy_oas")
    oas_chg = _macro_daily_change(oas, back_days=5) if len(oas) >= 6 else None

    liq = _macro_series(conn, "agg.liquidation")
    liq_chg = None
    if len(liq) >= 12:
        ts_now, v_now = liq[-1]
        target = ts_now - 6 * 3600
        prev = None
        for ts_v, v in liq:
            if ts_v <= target:
                prev = v
                break
        if prev is None:
            prev = liq[0][1]
        if prev:
            liq_chg = (v_now / prev - 1.0) * 100.0

    if not (pctl >= 0.80 and chg > 0):
        return out
    risk_signals = [s for s, v in (("高收益利差走阔", oas_chg), ("强平额抬升", liq_chg))
                     if v is not None and v > 0]
    out.append({
        "regime": "leverage_overheat",
        "report_date": md[-1][0],
        "proxy": False,
        "margin_debt": round(now_v, 2),
        "margin_debt_percentile": round(pctl * 100, 1),
        "margin_debt_change_5d_pct": round(chg, 3),
        "hy_oas_change_5d_pct": (round(oas_chg, 3) if oas_chg is not None else None),
        "liquidation_change_pct": (round(liq_chg, 3) if liq_chg is not None else None),
        "risk_signals": risk_signals,
        "arbitrage_exit_risk": "elevated" if risk_signals else "watch",
        "intensity": "extreme" if (pctl >= 0.95 and risk_signals) else "moderate",
        "score": round(min(1.0, pctl), 3),
        "data_source": "macro_snapshots: margin_debt / hy_oas / agg.liquidation (FRED + Coinglass)",
        "evidence": (f"融资余额处历史 {pctl*100:.0f} 分位, 5日 {chg:+.2f}%"
                     + (f"; 高收益利差 5日 {oas_chg:+.2f}%" if oas_chg is not None else "")
                     + (f"; 强平额 {liq_chg:+.2f}%" if liq_chg is not None else "")
                     + (f"; 风险信号: {'/'.join(risk_signals)}" if risk_signals else "")),
    })
    return out


def _detect_cn_state_fund_flow(conn: sqlite3.Connection) -> List[dict]:
    """A股国家队/机构资金流 —— **真实观测** (不是代理推断)。

    需求 #14 原文: 「机构大资金投资、国家基金投资、什么角色投资」。
    宽基 ETF 的**主力净流入**就是这条线:
      · 超大单净流入 (xl_net) = 机构/国家队量级的单子 → "什么角色在买"
      · 主力净流入 (main_net) = 主力整体动向
    数据来自东方财富 push2his (实测 0.18~0.91s 可达, **不需要代理**),
    由 cn_etf_flow.py 落进 macro_snapshots (key = cn_etf.<标的>.main_net / .xl_net)。

    诚实边界: 这是**机构/国家队配置 A 股**的观测, 不等于"某一支基金"——
    ETF 申赎的最终受益人无法从这条线区分。所以标注 role='institutional_proxy'
    而**不是** proxy=true (后者表示"推断出来的"), 因为它是**真实成交数据**。
    """
    out: List[dict] = []
    try:
        import cn_etf_flow as _cef
        st = _cef.flow_state(conn)
    except Exception as exc:
        print(f"[REGIME] cn_etf_flow 读取跳过: {type(exc).__name__}: {exc}", flush=True)
        return out
    if not st.get("n"):
        return out
    # ── 新鲜度门槛 (v321.3 P2) ──
    # 外网/采集挂掉时, DB 里留着的是**几天前**的旧观测。若照旧发 regime,
    # 就会把陈旧值讲成"今天国家队在流出" —— 误导决策。
    # 资金流是**日度**数据 → 超过 40h 没更新就判定为陈旧, 不再给结论。
    age_s = st.get("age_s")
    max_age_s = 40 * 3600
    if age_s is None or age_s > max_age_s:
        print(f"[REGIME] cn_etf_flow 数据陈旧 (age=%ss > %ss) → 本轮不给结论"
              % (age_s, max_age_s), flush=True)
        return out
    main_total = float(st.get("main_total") or 0.0)
    xl_total = float(st.get("xl_total") or 0.0)
    # 历史分位: 主力净流入在自身历史里的位置 (越大 = 越在进场)
    # ⚠️ v321.3 P2: **按交易日去重** —— 每天被采集 N 次会产生 N 条同值记录,
    # 直接拿来算分位会**虚增样本量**(同一天的同一笔资金被数 N 次)。
    # 取每个交易日的**最新一条**采集值作代表。
    try:
        hist = conn.execute(
            "SELECT json_extract(payload,'$.date') AS d, value FROM macro_snapshots "
            "WHERE metric_key='cn_etf_total.main_net' AND status='ok' "
            "AND value IS NOT NULL ORDER BY ts DESC LIMIT 600"
        ).fetchall()
        by_day: Dict[str, float] = {}
        for r in hist:
            day = str(r[0] or '')
            if not day:
                continue
            if day not in by_day:          # ts DESC → 第一条即该日最新
                by_day[day] = float(r[1] or 0.0)
        vals = list(by_day.values())
    except sqlite3.Error:
        vals = []
    pctl = None
    if len(vals) >= 20:
        pctl = mr_pct = sum(1 for v in vals if v < main_total) / len(vals)
    detail = []
    for key, d in sorted((st.get("targets") or {}).items()):
        mn = d.get("main_net")
        detail.append("%s=%s亿" % (key, ("%.2f" % (mn / 1e8)) if mn is not None else "n/a"))
    # 判定: 主力净流入为正 且 (分位高 或 超大单也为正) → 进场; 反之 → 撤离
    inflow = main_total > 0
    xl_inflow = xl_total > 0
    if inflow and (xl_inflow or (pctl is not None and pctl >= 0.60)):
        regime = "cn_state_fund_inflow"
        intensity = "extreme" if (pctl is not None and pctl >= 0.85) else "moderate"
    elif (not inflow) and (not xl_inflow or (pctl is not None and pctl <= 0.40)):
        regime = "cn_state_fund_exit"
        intensity = "extreme" if (pctl is not None and pctl <= 0.15) else "moderate"
    else:
        return out
    out.append({
        "regime": regime,
        "report_date": time.strftime("%Y-%m-%d"),
        "asset": "CN_ETF",
        "role": "institutional_proxy",
        "proxy": False,          # 真实成交数据, 不是推断
        "main_net_cny": round(main_total, 2),
        "xl_net_cny": round(xl_total, 2),
        "main_net_yi": round(main_total / 1e8, 3),
        "xl_net_yi": round(xl_total / 1e8, 3),
        "percentile": (round(pctl * 100, 1) if pctl is not None else None),
        "n_targets": st.get("n"),
        "intensity": intensity,
        "score": round(min(1.0, abs(main_total) / 5e9), 3),
        "data_source": "东方财富 push2his 宽基 ETF 日度资金流 (macro_snapshots: cn_etf.*)",
        "evidence": ("宽基ETF 主力净 %s亿 (超大单 %s亿); 明细: %s"
                     % (("%.2f" % (main_total / 1e8)), ("%.2f" % (xl_total / 1e8)),
                        " ".join(detail))),
    })
    return out


def _detect_state_fund_inflow(conn: sqlite3.Connection) -> List[dict]:
    """国家基金级资金入场 —— **代理推断** (国家基金账户不可直接观测)。

    代理指标 (全部是 macro_snapshots 里 status='ok' 的**真实**观测):
      · agg.oi              总持仓 → 资金进场的总量
      · BTC.top_ls_ratio    大户多空比 → **大资金**方向
      · BTC.opt_call_put_oi 期权多空持仓 → 机构方向性押注
      · agg.liquidation     强平额 → 有没有在挤兑 (低位才谈得上建仓)
      · tga                 美国财政部一般账户 (TGA) = **国家资金池**:
                            下降 = 财政部向市场投放流动性 (资金进场);
                            上升 = 财政部抽走流动性 (资金离场)。
    前四条同向 = 有**通道型大额资金**在进场 (ETF / 保险 / 社保 / 国家队走同一条路径)。
    任何一条数据缺失 → **不产出**, 不用别的指标凑。
    """
    out: List[dict] = []
    oi = _macro_series(conn, "agg.oi")
    top_ls = _macro_series(conn, "BTC.top_ls_ratio")
    opt_oi = _macro_series(conn, "BTC.opt_call_put_oi")
    liq = _macro_series(conn, "agg.liquidation")
    if len(oi) < 30 or len(top_ls) < 30 or len(opt_oi) < 30:
        return out

    def _chg(series: List[tuple], pct: bool = False, back_s: int = 6 * 3600) -> Optional[float]:
        """变化率 —— 按**时间间隔**找基准点, 不是按索引差。

        为什么: macro_snapshots 里 status='ok' 的行是**稀疏**的 (外网失败就缺),
        按索引 [-1] vs [-7] 拿到的可能不是"6 小时前" —— 实测会算出 -94.97%
        这种假暴跌。所以按 ts 找"约 back_s 秒之前"的最近点。

        v321.3 P1 守卫: 变化率绝对值 > MAX_PLAUSIBLE_CHANGE_PCT 时返回 None。
        原因: agg.oi 这个指标**换过口径** (payload 里写着「Coinglass 付费墙,
        免费单所口径替代」) —— 换口径那天前后值差 367 倍 (261 → 95987),
        算出来是 +36708% 的假增长。用它做判定 = 用数据源切换当市场信号。
        超阈值的直接当"不可比"处理, 不参与判定 —— 宁可少一条信号, 不用脏信号。
        """
        if len(series) < 2:
            return None
        ts_now, now = series[-1]
        target = ts_now - back_s
        prev = None
        for ts_v, v in series:
            if ts_v <= target:
                prev = v
                break
        if prev is None:
            prev = series[0][1]
        if pct:
            if not prev:
                return None
            chg = (now / prev - 1.0) * 100.0
            if abs(chg) > MAX_PLAUSIBLE_CHANGE_PCT:
                return None
            return chg
        return now - prev

    oi_chg = _chg(oi, pct=True)
    ls_chg = _chg(top_ls)
    opt_chg = _chg(opt_oi, pct=True)

    # ── 主判据用「历史分位」而不是百分比变化 (对"换口径"鲁棒) ─────────────
    # agg.oi / BTC.opt_call_put_oi 的**绝对值**不可跨期比较 (换过数据源口径),
    # 百分比变化会算出 +36708% 这种假增长 (被上面的守卫挡掉 → None)。
    # 但它们各自的**历史分位**是可比的: 口径切换只会重置分位, 不会造出假信号。
    # 语义上也更准: 我们要判的是「大资金在加仓」, 不是「总持仓涨了百分之多少」。
    def _pctl_of(series: List[tuple]) -> Optional[float]:
        if len(series) < 20:
            return None
        vals = [v for _t, v in series]
        look = vals[-REGIME_LOOKBACK_D:] if len(vals) >= REGIME_LOOKBACK_D else vals
        return _percentile_rank(vals[-1], look)

    oi_pctl = _pctl_of(oi)
    ls_pctl = _pctl_of(top_ls)
    opt_pctl = _pctl_of(opt_oi)
    if oi_pctl is None or ls_pctl is None or opt_pctl is None:
        return out

    liq_now = liq[-1][1] if liq else None
    liq_pctl = None
    if len(liq) >= 20:
        lv = [v for _t, v in liq]
        liq_pctl = _percentile_rank(lv[-1], lv[-REGIME_LOOKBACK_D:]
                                    if len(lv) >= REGIME_LOOKBACK_D else lv)

    # TGA (国家资金池): 必须按**日**去重再算变化 —— 见 _macro_daily_series 的说明
    tga_daily = _macro_daily_series(conn, "tga")
    tga_chg = _macro_daily_change(tga_daily, back_days=1)
    tga_note = None
    if tga_chg is not None:
        if abs(tga_chg) < 0.05:
            tga_note = "TGA 日变化 ~0% (持平)"
        else:
            tga_note = (f"TGA 日变化 {tga_chg:+.2f}% "
                        f"({'投放流动性·资金进场' if tga_chg < 0 else '抽走流动性·资金离场'})")

    # 同向判定 (分位口径): 总持仓偏高 + 大户偏多 + 期权持仓偏高 + 无强平挤兑
    if (oi_pctl >= 0.50 and ls_pctl >= 0.50 and opt_pctl >= 0.50
            and (liq_pctl is None or liq_pctl < 0.80)):
        out.append({
            "regime": "state_fund_inflow",
            "report_date": time.strftime("%Y-%m-%d", time.gmtime(int(oi[-1][0]))),
            "proxy": True,
            "proxy_note": ("国家基金账户不可直接观测; 本判定基于 总持仓 + 大户多空比 + "
                           "期权多空持仓 + 强平额 + 财政部TGA 的同向代理信号, "
                           "属**推断**而非观测。"),
            "oi_percentile": round(oi_pctl * 100, 1),
            "oi_change_pct": (round(oi_chg, 3) if oi_chg is not None else None),
            "top_ls_ratio": top_ls[-1][1],
            "top_ls_percentile": round(ls_pctl * 100, 1),
            "top_ls_change": (round(ls_chg, 4) if ls_chg is not None else None),
            "opt_call_put_oi_percentile": round(opt_pctl * 100, 1),
            "opt_call_put_oi_change_pct": (round(opt_chg, 3) if opt_chg is not None else None),
            "liquidation_percentile": (round(liq_pctl * 100, 1)
                                       if liq_pctl is not None else None),
            "tga_change_pct": (round(tga_chg, 3) if tga_chg is not None else None),
            "intensity": "extreme" if oi_pctl >= 0.95 else "moderate",
            "score": round(min(1.0, oi_pctl), 3),
            "data_source": ("macro_snapshots: agg.oi / BTC.top_ls_ratio / "
                            "BTC.opt_call_put_oi / agg.liquidation / tga"),
            "evidence": (f"总持仓 {oi_pctl*100:.0f} 分位; 大户多空比 {ls_pctl*100:.0f} 分位 "
                         f"({top_ls[-1][1]:.3f}); 期权多空持仓 {opt_pctl*100:.0f} 分位"
                         + (f"; 强平额处 {liq_pctl*100:.0f} 分位(无挤兑)" if liq_pctl is not None else "")
                         + (f"; {tga_note}" if tga_note else "")),
        })
    return out

