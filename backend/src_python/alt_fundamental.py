#!/usr/bin/env python3
"""非公司标的的基本面分量（v460）。

问题：`fundamental_factor` 只覆盖美股/A股/港股，黄金/原油/美元/外汇/加密在
`decision_guard` 里被记成「基本面不可得」，0.08 的权重让出去后归一化 ——
也就是这些品种的方向**完全没有可复现的基本面读数**，全靠新闻情绪与价格派生因子。

这个模块给它们各算一条 [-1, 1] 的分量，形状与公司侧一致（score / components /
evidence / gaps），因此可以直接复用 decision_guard 的 `_fund_bits` 与影子账本。

三条设计原则：
  1. **只用库里已有的数，零网络** —— 和 fundamental_factor 一样是读库函数，
     决策热路径不能因为外网抖动而变慢或变数。
  2. **每个分量都有符号理由**，写进 `detail`；没法说清符号的一律不做（例如 WTI 基差
     在本机长期不可得、BTC ETF 净流入未配置数据源，就直接进 `gaps`）。
  3. **分量不足 2 个就判不可得**，不拿单一读数硬凑分 —— 那和拍脑袋没区别。

覆盖：XAU（黄金）/ WTI（原油）/ USD·DXY（美元）/ EURUSD·USDJPY（外汇）/ BTC·ETH（加密）。
其余品种（白银、铜、天然气、股指…）明确返回 `unsupported`，不装懂。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL = 1800          # 与公司侧一致：30 分钟内同资产共用一份读数
_MIN_SAMPLES = 30          # 分位/中位数至少要 30 个日观测
_HIST_DAYS = 1000          # v464：FRED 回填 3 年历史后，分位窗口扩到 ~2.7 年

SUPPORTED = ("XAU", "WTI", "USD", "DXY", "EURUSD", "USDJPY", "BTC", "ETH",
             # v464：美债（长久期 / 短久期两套口径）
             "UST", "US10Y", "US30Y", "US2Y", "TLT", "IEF", "SHY", "ZB", "ZN", "ZT",
             # v466：股指（SPX 有真 CAPE；NDX 用 SPX CAPE 代理；沪深300 仍 unsupported）
             "SPX", "NDX",
             # v467：白银 / 铜（价格到位后可用"金银比 / 铜金比"做相对估值）
             "XAG", "CU",
             # v467：沪深300（中证指数官方 PE + 由点位/PE 反推的盈利水平）
             "CSI300",
             # v470：SOL（链上 TVL + 链上手续费 + 期货持仓，三族凑齐）
             "SOL")

_ALIASES = {
    "GOLD": "XAU", "XAUUSD": "XAU", "GC": "XAU",
    "OIL": "WTI", "CRUDE": "WTI", "CL": "WTI", "USOIL": "WTI",
    "DOLLAR": "USD", "USDX": "USD",
    "EUR/USD": "EURUSD", "USD/JPY": "USDJPY",
    "BITCOIN": "BTC", "ETHEREUM": "ETH",
    "SOLANA": "SOL", "SOLUSD": "SOL", "SOLUSDT": "SOL", "索拉纳": "SOL",
    "XAU/USD": "XAU", "BTCUSDT": "BTC", "ETHUSDT": "ETH",
    "BTC-USD": "BTC", "ETH-USD": "ETH", "BTCUSD": "BTC", "ETHUSD": "ETH",
    # 美债：新闻里出现的写法很多（中文/期限/期货代码/ETF 代码），统一到几种久期
    "TREASURY": "UST", "TREASURIES": "UST", "BOND": "UST", "BONDS": "UST",
    "美债": "UST", "美债期货": "UST", "国债": "UST", "美国国债": "UST",
    "UST10Y": "US10Y", "UST30Y": "US30Y", "UST2Y": "US2Y",
    "US10": "US10Y", "US30": "US30Y", "US2": "US2Y",
    "10Y": "US10Y", "30Y": "US30Y", "2Y": "US2Y",
    "TNX": "US10Y", "TY": "US10Y", "ZB": "ZB", "FUTURE_ZB": "ZB",
    # 股指（只有 SPX 有 CAPE 口径）
    "SP500": "SPX", "S&P500": "SPX", "标普500": "SPX", "标普": "SPX",
    "GSPC": "SPX", "SPX500": "SPX", "US500": "SPX", "SPX.X": "SPX",
    "NASDAQ100": "NDX", "NDX100": "NDX", "纳指100": "NDX", "纳斯达克100": "NDX",
    # 白银 / 铜（v467）
    "SILVER": "XAG", "XAGUSD": "XAG", "AG": "XAG", "白银": "XAG", "SI": "XAG",
    "COPPER": "CU", "HG": "CU", "XCU": "CU", "铜": "CU", "CU=F": "CU",
    # 沪深300（v467）
    "CSI300": "CSI300", "000300": "CSI300", "000300.SS": "CSI300",
    "000300.SH": "CSI300", "HS300": "CSI300", "沪深300": "CSI300",
}

# 长久期（对实际利率/通胀预期敏感）vs 短久期（对政策利率敏感）
_BOND_SHORT = {"US2Y", "SHY", "ZT"}
_INDEX_SET = {"SPX", "NDX"}


# ── 基础读数 ──────────────────────────────────────────────────────────────
def _clamp(v: float) -> float:
    return max(-1.0, min(1.0, float(v)))


def _ms_series(conn: sqlite3.Connection, key: str, days: int = _HIST_DAYS) -> List[Tuple[str, float]]:
    """macro_snapshots 的某一 key，按天去重（同日留最后一条），返回 [(day, value)]。

    周频序列（EIA/COT）每天只新增 1 个观测，靠库里的行要攒半年才够算分位；
    因此库内样本不足 `_MIN_SAMPLES` 时，回落到最新一条 payload 里的
    `history`（feed 抓的 52 周原始点）。这不是造数，是同一次抓取的原始观测。
    """
    cutoff = int(time.time()) - days * 86400
    try:
        rows = conn.execute(
            """SELECT ts, value FROM macro_snapshots
               WHERE metric_key=? AND value IS NOT NULL AND ts >= ?
               ORDER BY ts""", (key, cutoff)).fetchall()
    except Exception:
        return []
    daily: Dict[str, float] = {}
    for ts, value in rows:
        daily[time.strftime("%Y-%m-%d", time.localtime(int(ts)))] = float(value)
    if len(daily) < _MIN_SAMPLES:
        try:
            row = conn.execute(
                """SELECT payload FROM macro_snapshots
                   WHERE metric_key=? AND value IS NOT NULL
                   ORDER BY ts DESC LIMIT 1""", (key,)).fetchone()
        except Exception:
            row = None
        if row and row[0]:
            try:
                import json as _json
                hist = (_json.loads(row[0]) or {}).get("history") or []
            except Exception:
                hist = []
            for item in hist:
                if not isinstance(item, dict):
                    continue
                try:
                    day = str(item.get("period") or "")
                    val = float(item.get("value"))
                except (TypeError, ValueError):
                    continue
                if day:
                    daily.setdefault(day, val)
    return sorted(daily.items())


def _ext_series(conn: sqlite3.Connection, asset: str, indicator: str,
                days: int = _HIST_DAYS) -> List[Tuple[str, float]]:
    cutoff = int(time.time()) - days * 86400
    try:
        rows = conn.execute(
            """SELECT ts, value FROM external_indicators
               WHERE asset=? AND indicator=? AND value IS NOT NULL AND ts >= ?
               ORDER BY ts""", (asset, indicator, cutoff)).fetchall()
    except Exception:
        return []
    daily: Dict[str, float] = {}
    for ts, value in rows:
        daily[time.strftime("%Y-%m-%d", time.localtime(int(ts)))] = float(value)
    return sorted(daily.items())


def _pctile(series: List[Tuple[str, float]], value: Optional[float] = None) -> Optional[float]:
    """分位（0~1）：值在历史里排多高。样本不足返回 None。"""
    if len(series) < _MIN_SAMPLES:
        return None
    x = series[-1][1] if value is None else float(value)
    vals = [v for _, v in series]
    below = sum(1 for v in vals if v < x)
    equal = sum(1 for v in vals if v == x)
    return (below + 0.5 * equal) / len(vals)


def _chg(series: List[Tuple[str, float]], step: int) -> Optional[float]:
    if len(series) <= step:
        return None
    return series[-1][1] - series[-step - 1][1]


def _pct_chg(series: List[Tuple[str, float]], step: int) -> Optional[float]:
    if len(series) <= step:
        return None
    base = series[-step - 1][1]
    if not base:
        return None
    return (series[-1][1] - base) / abs(base) * 100.0


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _stdev(values: List[float]) -> Optional[float]:
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    return var ** 0.5


# ── 分量构造 ──────────────────────────────────────────────────────────────
def _comp(score: Optional[float], weight: float, detail: str, gaps: List[str],
          name: str) -> Optional[Dict[str, Any]]:
    if score is None:
        gaps.append(f"{name}: {detail}")
        return None
    return {"score": round(_clamp(score), 4), "weight": weight, "detail": detail}


def _level_inverse(conn, key: str, gaps: List[str], name: str, weight: float,
                   label: str) -> Optional[Dict[str, Any]]:
    """水平越高越利空（如实际利率、库存）。"""
    s = _ms_series(conn, key)
    p = _pctile(s)
    if p is None:
        return _comp(None, weight, f"{label} 样本不足（{len(s)} 天 < {_MIN_SAMPLES}）", gaps, name)
    return _comp(-(p - 0.5) * 2.0, weight,
                 f"{label} {s[-1][1]:.3g}，历史分位 {p * 100:.0f}%（越高越利空）", gaps, name)


def _level_direct(conn, key: str, gaps: List[str], name: str, weight: float,
                  label: str) -> Optional[Dict[str, Any]]:
    """水平越高越利多（如短端利率对美元）。"""
    s = _ms_series(conn, key)
    p = _pctile(s)
    if p is None:
        return _comp(None, weight, f"{label} 样本不足（{len(s)} 天）", gaps, name)
    return _comp((p - 0.5) * 2.0, weight,
                 f"{label} {s[-1][1]:.3g}，历史分位 {p * 100:.0f}%（越高越利多）", gaps, name)


def _cot_contrarian(conn, key: str, gaps: List[str], name: str, weight: float,
                    label: str) -> Optional[Dict[str, Any]]:
    """COT 投机净头寸分位做反向：极多=拥挤=利空，极少=拥挤空=利多。"""
    s = _ms_series(conn, key, days=_HIST_DAYS * 3)
    p = _pctile(s)
    if p is None:
        return _comp(None, weight, f"{label} 样本不足（{len(s)} 期 < {_MIN_SAMPLES}）", gaps, name)
    return _comp(-(p - 0.5) * 2.0, weight,
                 f"{label} {s[-1][1]:,.0f} 张，历史分位 {p * 100:.0f}%（越拥挤越反向）",
                 gaps, name)


# ── 各品种 ────────────────────────────────────────────────────────────────
def _build_gold(conn, gaps: List[str]) -> Dict[str, Any]:
    comps = {}
    comps["real_yield"] = _level_inverse(conn, "tips10y", gaps, "real_yield", 0.28, "10Y 实际利率")
    s = _ms_series(conn, "tips10y")
    chg = _chg(s, 20)
    comps["real_yield_chg"] = _comp(
        None if chg is None else -_clamp(chg / 0.40), 0.12,
        f"实际利率 20 日变化 {chg:+.2f}pp" if chg is not None else "实际利率变化不可得",
        gaps, "real_yield_chg")
    d = _ms_series(conn, "dxy")
    pct = _pct_chg(d, 20)
    comps["usd"] = _comp(None if pct is None else -_clamp(pct / 3.0), 0.18,
                         f"DXY 20 日 {pct:+.1f}%（美元走强压制金价）" if pct is not None else "DXY 不可得",
                         gaps, "usd")
    comps["cot"] = _cot_contrarian(conn, "gold.cot_noncomm_net", gaps, "cot", 0.22, "黄金 COT 投机净多")
    prem = _ext_series(conn, "SGE_GOLD", "sge_gold_premium_pct")
    med, sd = _median([v for _, v in prem]) if len(prem) >= _MIN_SAMPLES else (None, None)
    sd = _stdev([v for _, v in prem]) if len(prem) >= _MIN_SAMPLES else None
    if prem and med is not None and sd:
        z = (prem[-1][1] - med) / sd if sd > 0.02 else 0.0
        comps["sge_premium"] = _comp(_clamp(z / 1.5), 0.20,
                                     f"SGE 中国金溢价 {prem[-1][1]:+.2f}%（相对自身中位 {med:+.2f}%，"
                                     f"{z:+.1f}σ）→ 实物需求强弱", gaps, "sge_premium")
    else:
        comps["sge_premium"] = _comp(None, 0.20, "SGE 溢价样本不足", gaps, "sge_premium")
    gaps_hard = ["SPDR 黄金 ETF 持仓：SPDR 官网 CSV 本机长期不可达（DNS/SSL），未参与打分"]
    return {"components": comps, "gaps": gaps + gaps_hard}


def _build_oil(conn, gaps: List[str]) -> Dict[str, Any]:
    comps = {}
    comps["crude_stocks"] = _level_inverse(conn, "oil.crude_stocks", gaps, "crude_stocks", 0.24,
                                           "美国原油库存")
    s = _ms_series(conn, "oil.crude_stocks.chg")
    chg = s[-1][1] if s else None
    comps["stocks_chg"] = _comp(None if chg is None else -_clamp(chg / 4000.0), 0.14,
                                f"库存周变化 {chg:+,.0f} 千桶（去库=利多）" if chg is not None
                                else "库存周变化不可得", gaps, "stocks_chg")
    comps["distillate"] = _level_inverse(conn, "oil.distillate_stocks", gaps, "distillate", 0.14,
                                         "馏分油库存")
    prod = _ms_series(conn, "oil.us_crude_production")
    pc = _pct_chg(prod, 4)
    comps["production"] = _comp(None if pc is None else -_clamp(pc / 3.0), 0.12,
                                f"美国原油产量 4 周 {pc:+.1f}%（增产=供给宽松）" if pc is not None
                                else "产量不可得", gaps, "production")
    ref = _ms_series(conn, "oil.refinery_inputs")
    rc = _pct_chg(ref, 4)
    comps["refinery"] = _comp(None if rc is None else _clamp(rc / 6.0), 0.10,
                              f"炼厂原油投入 4 周 {rc:+.1f}%（加工量升=需求端好）" if rc is not None
                              else "炼厂投入不可得", gaps, "refinery")
    comps["cot"] = _cot_contrarian(conn, "wti.cot_noncomm_net", gaps, "cot", 0.26, "WTI COT 投机净多")
    gaps_hard = ["WTI 基差/期限结构：本机无免费永续合约源，未参与打分",
                 "OPEC+ 产量与配额：无免费稳定源，未参与打分"]
    return {"components": comps, "gaps": gaps + gaps_hard}


def _build_usd(conn, gaps: List[str]) -> Dict[str, Any]:
    comps = {}
    comps["policy_rate"] = _level_direct(conn, "dgs2", gaps, "policy_rate", 0.34, "美国 2Y 收益率")
    comps["real_yield"] = _level_direct(conn, "tips10y", gaps, "real_yield", 0.18, "10Y 实际利率")
    comps["cot"] = _cot_contrarian(conn, "dxy.cot_noncomm_net", gaps, "cot", 0.26, "美元指数 COT 净多")
    gaps_hard = ["国际收支/经常账户：未接入", "欧元区/日本政策利率：仅有 JGB10Y，缺 2Y 利差曲线"]
    return {"components": comps, "gaps": gaps + gaps_hard}


def _build_eurusd(conn, gaps: List[str]) -> Dict[str, Any]:
    """欧元/美元 = 美元分量的镜像 + 欧元自身 COT。"""
    usd = _build_usd(conn, gaps)
    comps = {}
    for name in ("policy_rate", "real_yield", "cot"):
        c = usd["components"].get(name)
        if c:
            comps[name] = {**c, "score": round(-c["score"], 4),
                           "detail": "美元强弱的镜像（欧元视角）：" + c["detail"]}
    ec = _cot_contrarian(conn, "eur.cot_noncomm_net", gaps, "eur_cot", 0.22, "欧元 COT 净多")
    if ec:
        comps["eur_cot"] = ec
    fx = _ext_series(conn, "FX", "fx_eurusd")
    dev = _pct_chg(fx, 60)
    comps["valuation"] = _comp(None if dev is None else -_clamp(dev / 6.0), 0.16,
                               f"EURUSD 60 日 {dev:+.1f}%（短期偏离越大越易回归）" if dev is not None
                               else f"EURUSD 60 日历史不足（现有 {len(fx)} 天）", gaps, "valuation")
    return {"components": comps, "gaps": usd["gaps"]}


def _build_usdjpy(conn, gaps: List[str]) -> Dict[str, Any]:
    """美元/日元 = 美日利差（2Y 缺日本 2Y，用 JGB10Y 近似）+ COT。"""
    comps = {}
    # 利差本身要按"历史分位"判，不能拿固定阈值 3.0 —— 2022 年之后 3pp 是常态，
    # 写死绝对值会让信号在长周期里静默失真。
    us = dict(_ms_series(conn, "dgs2"))
    jp = dict(_ms_series(conn, "jgb10y"))
    common = sorted(set(us) & set(jp))
    spread_series = [(d, us[d] - jp[d]) for d in common]
    p = _pctile(spread_series)
    if p is None:
        comps["rate_spread"] = _comp(None, 0.34,
                                     f"美日利差样本不足（{len(spread_series)} 天 < {_MIN_SAMPLES}）",
                                     gaps, "rate_spread")
    else:
        spread = spread_series[-1][1]
        comps["rate_spread"] = _comp((p - 0.5) * 2.0, 0.34,
                                     f"美 2Y - 日 10Y 利差 {spread:+.2f}pp，历史分位 {p * 100:.0f}%"
                                     f"（利差越阔=美元强）", gaps, "rate_spread")
    comps["cot"] = _cot_contrarian(conn, "jpy.cot_noncomm_net", gaps, "cot", 0.30, "日元 COT 净多")
    fx = _ext_series(conn, "FX", "fx_usdjpy")
    dev = _pct_chg(fx, 60)
    comps["valuation"] = _comp(None if dev is None else -_clamp(dev / 6.0), 0.16,
                               f"USDJPY 60 日 {dev:+.1f}%（偏离越大越易回归）" if dev is not None
                               else f"USDJPY 60 日历史不足（现有 {len(fx)} 天）", gaps, "valuation")
    gaps_hard = ["日本 2Y 政策利率：未接入（用 JGB10Y 近似，口径已在 detail 注明）"]
    return {"components": comps, "gaps": gaps + gaps_hard}


def _build_crypto(conn, asset: str, gaps: List[str]) -> Dict[str, Any]:
    comps = {}
    f = _ms_series(conn, f"{asset}.funding")
    p = _pctile(f)
    comps["funding"] = _comp(None if p is None else -(p - 0.5) * 2.0, 0.30,
                             (f"{asset} 资金费率 {f[-1][1]:+.3f}%/8h，历史分位 {p * 100:.0f}%"
                              f"（越高=多头越拥挤=反向）") if p is not None
                             else f"{asset} 资金费率样本不足（{len(f)} 天）", gaps, "funding")
    # 稳定币供应是全市场口径，库里统一记在 asset='BTC' 下（2026-11 实测），
    # 不是"比特币专属"—— 按 asset 过滤会漏掉 ETH 这一分量。
    st = _ext_series(conn, "BTC", "stable_supply_30d_pct")
    comps["stable_supply"] = _comp(None if not st else _clamp(st[-1][1] / 6.0), 0.26,
                                   f"稳定币总供应 30 日 {st[-1][1]:+.1f}%（增量资金）" if st
                                   else "稳定币供应不可得", gaps, "stable_supply")
    if asset == "BTC":
        hr = _ext_series(conn, "BTC", "cm_hashrate")
        hp = _pct_chg(hr, 30)
        comps["hashrate"] = _comp(None if hp is None else _clamp(hp / 12.0), 0.18,
                                  f"全网算力 30 日 {hp:+.1f}%（矿工留在场内=供给端稳定）"
                                  if hp is not None else "算力不可得", gaps, "hashrate")
        # v468：链上活跃度（blockchain.info 日频，免费）—— 不是估值，但回答"还有没有人在用"。
        # 符号是显式假设：活跃度扩张 = 网络使用度上升 = 偏多；回落 = 使用度萎缩 = 偏空。
        addr = _ms_series(conn, "btc.onchain_addr")
        ap = _pct_chg(addr, 30)
        comps["onchain_addr"] = _comp(
            None if ap is None else _clamp(ap / 10.0), 0.12,
            f"链上活跃地址 30 日 {ap:+.1f}%（使用度扩张=偏多）" if ap is not None
            else f"链上活跃地址样本不足（{len(addr)} 天）", gaps, "onchain_addr")
        tx = _ms_series(conn, "btc.onchain_tx")
        tp = _pct_chg(tx, 30)
        comps["onchain_tx"] = _comp(
            None if tp is None else _clamp(tp / 10.0), 0.10,
            f"链上交易笔数 30 日 {tp:+.1f}%（结算需求扩张=偏多）" if tp is not None
            else f"链上交易笔数样本不足（{len(tx)} 天）", gaps, "onchain_tx")
        # v469：美国现货 ETF 每日净流入（farside 汇总，733 个交易日）—— 真金白银的增量资金
        flow = _ms_series(conn, "btc.etf_net_flow")
        cum = [(flow[i][0], sum(v for _, v in flow[i - 19:i + 1]))
               for i in range(19, len(flow))]
        fp = _pctile(cum) if len(cum) >= _MIN_SAMPLES else None
        comps["etf_flow"] = _comp(
            None if fp is None else (fp - 0.5) * 2.0, 0.14,
            f"现货 ETF 20 日累计净流入 {cum[-1][1]:+.0f} 百万美元"
            f"（历史分位 {fp * 100:.0f}%，流入=增量资金=偏多）" if fp is not None
            else f"ETF 净流入样本不足（{len(flow)} 天）", gaps, "etf_flow")
        gaps_hard = ["BTC 交易所余额 / 已实现市值(MVRV 分母) / 矿工关机价：无免费源，仍缺“贵不贵”的估值锚"]
    else:
        # v471：ETH 的链上维度（DefiLlama 免费，与 BTC 的 blockchain.info 不同源、不同口径）
        # TVL = 锁在以太坊 DeFi 里的美元（资本愿不愿意留）；fees = 每天真实付出的费用（使用强度）。
        etvl = _ms_series(conn, "eth.tvl")
        etp = _pct_chg(etvl, 30)
        comps["tvl"] = _comp(
            None if etp is None else _clamp(etp / 12.0), 0.22,
            f"以太坊链上 TVL 30 日 {etp:+.1f}%（资本流入=偏多；当前 ${etvl[-1][1] / 1e9:.1f}b）"
            if etp is not None else f"以太坊 TVL 样本不足（{len(etvl)} 天）", gaps, "tvl")
        efee = _ms_series(conn, "eth.fees")
        efp = _pct_chg(efee, 30)
        comps["fees"] = _comp(
            None if efp is None else _clamp(efp / 25.0), 0.18,
            f"链上手续费 30 日 {efp:+.1f}%（真实需求扩张=偏多；当前 ${efee[-1][1] / 1e6:.2f}m/日）"
            if efp is not None else f"链上手续费样本不足（{len(efee)} 天）", gaps, "fees")
        gaps_hard = ["ETH 质押收益率 / 质押率：未接入（需要信标链数据源）"]
    oi = _ms_series(conn, f"{asset}.oi")
    comps["oi_level"] = _comp(None if _pctile(oi) is None else -(_pctile(oi) - 0.5) * 2.0, 0.16,
                              f"{asset} 未平仓 {oi[-1][1]:,.0f}，分位 "
                              f"{(_pctile(oi) or 0) * 100:.0f}%（杠杆越高越脆弱）" if oi
                              else "未平仓不可得", gaps, "oi_level")
    return {"components": comps, "gaps": gaps + gaps_hard}


def _build_sol(conn, gaps: List[str]) -> Dict[str, Any]:
    """SOL（v470）：链上资本（TVL）+ 链上活动（手续费）+ 期货持仓，三族独立。

    为什么是这两条而不是"链上地址数"：Solana 没有免费的历史地址/笔数序列
    （公共 RPC 只给最近几十个采样点），但 DefiLlama 免费给两条完整的日频经济序列 ——
      - TVL：锁在这条链 DeFi 里的美元，回答"资本愿不愿意留"；
      - 手续费：每天真实付出的美元费用，回答"这条链有没有人在用、用得多不多"。
    两者与 COT 完全独立，符号也好讲：
      TVL 扩张 = 资本流入 = 偏多；手续费扩张 = 真实需求上升 = 偏多；持仓越拥挤越脆弱 = 反向。
    """
    comps: Dict[str, Any] = {}

    tvl = _ms_series(conn, "sol.tvl")
    tp = _pct_chg(tvl, 30)
    comps["tvl"] = _comp(
        None if tp is None else _clamp(tp / 12.0), 0.44,
        f"Solana 链上 TVL 30 日 {tp:+.1f}%（资本流入=偏多；当前 ${tvl[-1][1] / 1e9:.2f}b）"
        if tp is not None else f"链上 TVL 样本不足（{len(tvl)} 天）", gaps, "tvl")

    fee = _ms_series(conn, "sol.fees")
    fp = _pct_chg(fee, 30)
    comps["fees"] = _comp(
        None if fp is None else _clamp(fp / 25.0), 0.34,
        f"链上手续费 30 日 {fp:+.1f}%（真实需求扩张=偏多；当前 ${fee[-1][1] / 1e6:.2f}m/日）"
        if fp is not None else f"链上手续费样本不足（{len(fee)} 天）", gaps, "fees")

    cot = _ms_series(conn, "sol.cot_noncomm_net")
    pct = _pctile(cot)
    comps["cot"] = _comp(
        None if pct is None else -(pct - 0.5) * 2.0, 0.22,
        f"SOL 非商业净持仓 {cot[-1][1]:,.0f} 张，历史分位 {pct * 100:.0f}%（越拥挤越脆弱=反向）"
        if pct is not None else f"SOL 持仓样本不足（{len(cot)} 天）", gaps, "cot")

    gaps_hard = ["SOL 活跃地址 / 交易笔数：公共 RPC 只给最近采样，无免费历史序列",
                 "SOL 质押率 / 通胀率（链上发行）：未接入",
                 "SOL 现货 ETF 净流入：规模小且披露不全，暂不做"]
    return {"components": comps, "gaps": gaps + gaps_hard}


# 分量背后的"原始数据族"：同一族派生的两个分量（实际利率水平 + 它的变化）不算
# 两个独立证据 —— 否则只接一条 FRED 序列就能凑够 2 个分量给自己发分。
# ── 美债（v464）────────────────────────────────────────────────────────
# 方向口径：分数正 = **看多债券价格**（收益率下行），负 = 看空债券价格。
# 长久期与短久期分开算：长久期怕实际利率与通胀预期，短久期怕政策利率。
def _spread_series(conn, key_a: str, key_b: str,
                   days: int = _HIST_DAYS) -> List[Tuple[str, float]]:
    """两条日频序列按天对齐后相减（缺一天就不算那天，不做插值）。"""
    a = dict(_ms_series(conn, key_a, days))
    b = dict(_ms_series(conn, key_b, days))
    return [(d, a[d] - b[d]) for d in sorted(set(a) & set(b))]


def _USD20D(conn, gaps: List[str], weight: float) -> Optional[Dict[str, Any]]:
    """美元 20 日变动 → 美元计价商品的反向分量（和黄金同一套口径）。"""
    pct = _pct_chg(_ms_series(conn, "dxy"), 20)
    return _comp(None if pct is None else -_clamp(pct / 3.0), weight,
                 f"DXY 20 日 {pct:+.1f}%（美元走强压制价格）" if pct is not None
                 else "DXY 不可得", gaps, "usd")


def _ratio_series(conn, num_key: str, den_key: str,
                  days: int = _HIST_DAYS) -> List[Tuple[str, float]]:
    """两条价格序列按天相除（金银比 / 铜金比）。缺一天就不算那天。"""
    a = dict(_ms_series(conn, num_key, days))
    b = dict(_ms_series(conn, den_key, days))
    out = []
    for d in sorted(set(a) & set(b)):
        if b[d]:
            out.append((d, a[d] / b[d]))
    return out


def _build_metal(conn, code: str, gaps: List[str]) -> Dict[str, Any]:
    """白银 / 铜（v467）：COT 拥挤度 + 美元 + 跨品种相对比价（+ 白银的实际利率）。

    银、铜是美元计价商品，所以"美元走强压制"这一条和黄金同构；
    真正让它们从"只有情绪"变成"有基本面"的，是两条相对比价：
      - 金银比：比值高 = 白银相对黄金便宜（白银偏多）；
      - 铜金比：比值高 = 增长预期强（铜偏多）。
    这两个比价都不是价格动量，是跨品种相对定价，可以进基本面口径。
    """
    comps: Dict[str, Optional[Dict[str, Any]]] = {}
    if code == "XAG":
        comps["cot"] = _cot_contrarian(conn, "silver.cot_noncomm_net", gaps, "cot", 0.32,
                                       "白银投机净多")
        comps["usd"] = _USD20D(conn, gaps, 0.20)
        ratio = _ratio_series(conn, "xau.usd", "xag.usd")
        p = _pctile(ratio)
        comps["ratio"] = _comp(
            None if p is None else (p - 0.5) * 2.0, 0.26,
            f"金银比 {ratio[-1][1]:.1f}（{len(ratio)} 天 {p * 100:.0f}% 分位，"
            f"比值越高=白银相对黄金越便宜）" if p is not None
            else f"金银比样本不足（{len(ratio)} 天）", gaps, "ratio")
        comps["real_yield"] = _level_inverse(conn, "tips10y", gaps, "real_yield", 0.16,
                                             "10Y 实际利率")
        gaps_hard = ["COMEX/上期所白银库存：无免费源，库存这一层没接",
                     "白银工业需求（光伏/电子）：无分项数据"]
    else:
        comps["cot"] = _cot_contrarian(conn, "copper.cot_noncomm_net", gaps, "cot", 0.30,
                                       "铜投机净多")
        comps["usd"] = _USD20D(conn, gaps, 0.18)
        ratio = _ratio_series(conn, "xcu.usd", "xau.usd")
        p = _pctile(ratio)
        comps["ratio"] = _comp(
            None if p is None else (p - 0.5) * 2.0, 0.30,
            f"铜金比 {ratio[-1][1]:.5f}（{len(ratio)} 天 {p * 100:.0f}% 分位，"
            f"比值越高=增长预期越强）" if p is not None
            else f"铜金比样本不足（{len(ratio)} 天）", gaps, "ratio")
        gaps_hard = ["LME/COMEX/上期所铜库存：无免费源，库存这一层没接",
                     "中国电网/地产需求：无分项数据（铜金比只是增长代理）"]
    return {"components": comps, "gaps": gaps + gaps_hard, "kind_label": "商品"}


def _build_cn_index(conn, gaps: List[str]) -> Dict[str, Any]:
    """沪深300（v467）：官方 PE 贵不贵 + 由"点位/PE"反推的指数盈利水平。

    为什么用这两条
    --------------
    A股没有免费的指数级一致预期，也没有可用的中国 10Y 国债收益率序列（FRED 的中国长端
    序列已停更、返回 400），所以拿不到标准 ERP。能拿到的官方口径是两列：
      - `csi300.pe`：中证指数官网按日发布的 PE（实测 ≈13）；
      - `csi300.close`：同一天的收盘点位。
    点位 ÷ PE = 指数盈利（EPS）水平。一个衡量"贵不贵"，一个衡量"盈利在改善还是恶化"，
    信息不同、方向也不同：PE 分位高=贵=利空；EPS 抬升=盈利改善=利多。
    两者同源于中证指数官方日频，但一个是估值、一个是盈利，这里按两个族记。
    """
    comps: Dict[str, Optional[Dict[str, Any]]] = {}
    pe = _series_since(conn, "csi300.pe", 5)
    p = _pctile(pe)
    comps["pe"] = _comp(
        None if p is None else (0.5 - p) * 2.0, 0.45,
        f"沪深300 PE {pe[-1][1]:.2f}（{len(pe)} 天 {p * 100:.0f}% 分位，越贵=未来回报越低，反向）"
        if p is not None else f"沪深300 PE 样本不足（{len(pe)} 天）", gaps, "cn_pe")

    eps = _ratio_series(conn, "csi300.close", "csi300.pe", days=5 * 365)
    d_eps = _chg(eps, 60)
    p_eps = (_pctile([(eps[i][0], (eps[i][1] / eps[i - 60][1] - 1) * 100)
                      for i in range(60, len(eps)) if eps[i - 60][1]]) if len(eps) > 90 else None)
    comps["eps"] = _comp(
        None if p_eps is None else (p_eps - 0.5) * 2.0, 0.27,
        f"指数盈利(EPS) 60 日 {d_eps:+.1f}%（历史分位 {p_eps * 100:.0f}%，盈利抬升=利多）"
        if (p_eps is not None and d_eps is not None)
        else f"指数盈利样本不足（{len(eps)} 天）", gaps, "cn_eps")

    gaps_hard = [
        "中国 10Y 国债收益率：FRED 中国长端序列已停更（400），拿不到，标准 ERP 算不了",
        "北向资金/外资持股：官方已停止逐日披露",
        "A股盈利一致预期修正：无免费数据源",
    ]
    return {"components": comps, "gaps": gaps + gaps_hard, "kind_label": "股指"}


def _tic_score(conn, gaps: List[str], weight: float) -> Optional[Dict[str, Any]]:
    """TIC 外资持有美债（月度）：增持=买盘在=偏多，减持=边际买盘撤退=偏空。

    硬约束：TIC 天然滞后约两个月，数据太旧就不发分 —— 旧数据不能假装是当下的需求。
    """
    ser = _series_since(conn, "ust.tic_foreign_holdings", 15)
    if len(ser) < 24:
        return _comp(None, weight, f"TIC 样本不足（{len(ser)} 个月 < 24）", gaps, "tic")
    age = (time.time() - time.mktime(time.strptime(ser[-1][0], "%Y-%m-%d"))) / 86400
    if age > 75:
        gaps.append(f"TIC 外资持仓最新 {ser[-1][0]}，已滞后 {age:.0f} 天，本分量暂不发分"
                    f"（不用旧数据假装当下）")
        return _comp(None, weight, f"TIC 最新 {ser[-1][0]}，滞后 {age:.0f} 天", gaps, "tic")
    chg = [(ser[i][0], (ser[i][1] / ser[i - 12][1] - 1) * 100)
           for i in range(12, len(ser)) if ser[i - 12][1]]
    p = _pctile(chg)
    if p is None:
        return _comp(None, weight, f"TIC 12 个月变化样本不足（{len(chg)} 个）", gaps, "tic")
    return _comp((p - 0.5) * 2.0, weight,
                 f"外资持有美债 {ser[-1][0]} 为 {ser[-1][1]:,.0f} 十亿美元，"
                 f"12 个月 {chg[-1][1]:+.1f}%（分位 {p * 100:.0f}%，增持=买盘在，偏多）",
                 gaps, "tic")


def _series_since(conn, key: str, years: int) -> List[Tuple[str, float]]:
    """取最近 N 年的序列（月频序列用年而不是天来切窗口）。"""
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - years * 365 * 86400))
    return [(d, v) for d, v in _ms_series(conn, key, years * 365 + 30) if d >= cutoff]


def _build_index(conn, code: str, gaps: List[str]) -> Dict[str, Any]:
    """股指估值（v466）：CAPE 贵不贵 + 股债性价比（ERP）+ 信用环境。

    CAPE 只覆盖 S&P 500 —— NDX / 沪深300 没有公开的同口径数据，所以只支持 SPX。
    方向：正 = 看多股指。
    """
    comps: Dict[str, Optional[Dict[str, Any]]] = {}
    proxy = code != "SPX"          # NDX 没有自己的 CAPE，只能用 SPX 代理

    cape = _series_since(conn, "spx.cape", 30)
    p = _pctile(cape)
    if p is None:
        comps["cape"] = _comp(None, 0.34,
                                   f"CAPE 样本不足（{len(cape)} 个 < {_MIN_SAMPLES}）",
                                   gaps, "cape")
    else:
        comps["cape"] = _comp((0.5 - p) * 2.0, 0.34,
                              f"CAPE{'（SPX 代理）' if proxy else ''} {cape[-1][1]:.1f}，"
                              f"近 30 年分位 {p * 100:.0f}%（越贵=未来回报越低，反向）",
                              gaps, "cape")

    # 股债性价比：盈利收益率（1/CAPE）减去 10Y 国债收益率，越高=股票相对债券越便宜
    dgs10 = dict(_ms_series(conn, "dgs10", 4000))
    erp: List[Tuple[str, float]] = []
    for day, v in cape:
        if not v:
            continue
        rate = dgs10.get(day)
        if rate is None:                     # 节假日对齐：往前找最近一个有值的交易日
            for back in range(1, 8):
                d2 = time.strftime("%Y-%m-%d",
                                   time.localtime(time.mktime(time.strptime(day, "%Y-%m-%d"))
                                                  - back * 86400))
                if d2 in dgs10:
                    rate = dgs10[d2]
                    break
        if rate is None:
            continue
        erp.append((day, 100.0 / v - rate))
    pe = _pctile(erp)
    if pe is None:
        comps["erp"] = _comp(None, 0.18, f"股债性价比样本不足（{len(erp)} 个）", gaps, "erp")
    else:
        comps["erp"] = _comp((pe - 0.5) * 2.0, 0.18,
                             f"盈利收益率-10Y {erp[-1][1]:+.2f}pp，"
                             f"{len(erp)} 个月分位 {pe * 100:.0f}%"
                             f"（越高=股票相对债券越便宜）", gaps, "erp")

    # 信用环境：对股指，利差走阔是利空（和国债正好相反 —— 同一个变量、两个市场的符号不同）
    hy = _ms_series(conn, "hy_oas")
    d_hy = _chg(hy, 20)
    comps["credit"] = _comp(
        None if d_hy is None else -_clamp(d_hy / 0.60), 0.22,
        f"高收益债利差 20 日 {d_hy:+.2f}pp（走阔=风险偏好恶化→利空股指）"
        if d_hy is not None else "信用利差不可得", gaps, "credit")

    extra = []
    if proxy:
        extra.append(f"{code} 自身估值（PE/CAPE）：无公开同口径数据，现用 SPX CAPE 代理 —— "
                     f"这是市场级估值，不是 {code} 自己的成分股估值")
    return {"components": comps, "gaps": gaps + extra + [
        "指数级盈利预期修正（EPS revision）：无一致预期数据源",
        "市场宽度（breadth）/ 资金流：库里只有 crypto.* 宽度，没有股票宽度",
    ]}


def _auction_score(conn, keys: Tuple[str, ...], gaps: List[str],
                   name: str, weight: float, label: str) -> Optional[Dict[str, Any]]:
    """拍卖需求：bid-to-cover 越好 = 买家越踊跃 = 对债券偏多（正分）。

    每场拍卖是一个观测（10Y/2Y 月频、30Y 月频），所以历史窗口按"场次"而不是按天算。
    多场次（10Y + 30Y）合成一个分量：它们是同一件事（真实买盘强不强），不拆成两条证据。
    """
    ps: List[float] = []
    bits: List[str] = []
    for key in keys:
        series = _ms_series(conn, key, 4000)
        p = _pctile(series)
        if p is None:
            continue
        ps.append(p)
        short = key.rsplit("_", 1)[-1].upper()
        bits.append(f"{short} {series[-1][1]:.2f}（{len(series)} 场 {p * 100:.0f}% 分位）")
    if not ps:
        return _comp(None, weight, f"{label}不足（缺 {len(keys)} 条拍卖序列）", gaps, name)
    score = (sum(ps) / len(ps) - 0.5) * 2.0
    return _comp(score, weight, f"{label} " + " / ".join(bits) + "（投标倍数越高=需求越强）",
                 gaps, name)


def _build_ust(conn, code: str, gaps: List[str]) -> Dict[str, Any]:
    short = code in _BOND_SHORT
    comps: Dict[str, Optional[Dict[str, Any]]] = {}
    if short:
        # 2Y / SHY / ZT：一辆"现金替代"，看的是政策利率给的票息够不够
        comps["policy_rate"] = _level_direct(conn, "dgs2", gaps, "policy_rate", 0.34,
                                             "美国 2Y 收益率")
        comps["real_yield"] = _level_inverse(conn, "tips10y", gaps, "real_yield", 0.16,
                                             "10Y 实际利率")
        comps["cot"] = _cot_contrarian(conn, "tnote2y.cot_noncomm_net", gaps, "cot", 0.24,
                                       "2Y 国债期货投机净多")
        comps["breakeven"] = _level_inverse(conn, "t10yie", gaps, "breakeven", 0.14,
                                            "10Y 盈亏平衡通胀")
        comps["auction"] = _auction_score(
            conn, ("ust.auction_btc_2y",), gaps, "auction", 0.10, "2Y 投标倍数")
        dur_label = "短久期"
    else:
        # 10Y/30Y/TLT/IEF：久期长，实际利率与通胀预期是主驱动，曲线给的是 carry 补偿
        comps["real_yield"] = _level_inverse(conn, "tips10y", gaps, "real_yield", 0.26,
                                             "10Y 实际利率")
        chg = _chg(_ms_series(conn, "tips10y"), 20)
        comps["real_yield_chg"] = _comp(
            None if chg is None else -_clamp(chg / 0.40), 0.10,
            f"实际利率 20 日变化 {chg:+.2f}pp（快速上行=债券挨打）" if chg is not None
            else "实际利率变化不可得", gaps, "real_yield_chg")
        comps["breakeven"] = _level_inverse(conn, "t10yie", gaps, "breakeven", 0.18,
                                            "10Y 盈亏平衡通胀")
        # 曲线用 carry 视角而不是动量视角：30s10 越陡 = 长端给的期限溢价越高 = 做多久期
        # 的持有补偿越厚（正分）。若按动量读，同样一个"陡"会得出相反结论 —— 两种视角
        # 都不算错，但必须写死一种，否则同一张表里两套符号会互相抵消。
        curve = _spread_series(conn, "dgs30", "dgs10")
        p = _pctile(curve)
        if p is None:
            comps["curve"] = _comp(None, 0.16,
                                   f"30Y-10Y 曲线样本不足（{len(curve)} 天 < {_MIN_SAMPLES}）",
                                   gaps, "curve")
        else:
            comps["curve"] = _comp((p - 0.5) * 2.0, 0.16,
                                   f"30Y-10Y {curve[-1][1]:+.2f}pp，历史分位 {p * 100:.0f}%"
                                   f"（越陡=期限溢价越高，carry 视角偏多）", gaps, "curve")
        comps["cot"] = _cot_contrarian(conn, "tbond.cot_noncomm_net", gaps, "cot", 0.20,
                                       "美债期货(30Y)投机净多")
        hy = _ms_series(conn, "hy_oas")
        d_hy = _chg(hy, 20)
        comps["credit"] = _comp(
            None if d_hy is None else _clamp(d_hy / 0.60), 0.10,
            f"高收益债利差 20 日 {d_hy:+.2f}pp（走阔=风险偏好恶化→国债避险买盘）"
            if d_hy is not None else "信用利差不可得", gaps, "credit")
        # v465：拍卖需求（财政部官方 auctions_query，10Y + 30Y 两段合成一个分量）
        comps["auction"] = _auction_score(
            conn, ("ust.auction_btc_10y", "ust.auction_btc_30y"), gaps, "auction", 0.08,
            "10Y/30Y 投标倍数")
        # v467：TIC 外资持仓（月度文本源，只在数据够新时发分）
        comps["tic"] = _tic_score(conn, gaps, 0.06)
        dur_label = "长久期"

    gaps_hard = [
        "期限结构 carry/roll-down：缺债券期货或 ETF 日线，无法算持有收益曲线",
        "TIC 外资持仓天然滞后约 2 个月；超过 75 天不发分（已接源，只是不拿旧数据充当下",

    ]
    return {"components": comps, "gaps": gaps + gaps_hard,
            "duration": dur_label}


_COMP_SOURCE = {
    "real_yield": "tips10y", "real_yield_chg": "tips10y", "usd": "dxy", "cot": "cot",
    "sge_premium": "sge_premium", "crude_stocks": "eia_stocks",
    "stocks_chg": "eia_stocks", "distillate": "eia_distillate",
    "production": "eia_production", "refinery": "eia_refinery",
    "policy_rate": "dgs2", "eur_cot": "eur_cot", "valuation": "fx",
    "rate_spread": "spread", "funding": "funding", "stable_supply": "stable_supply",
    "hashrate": "hashrate", "oi_level": "oi",
    # v464 美债
    "breakeven": "t10yie", "curve": "curve", "credit": "hy_oas", "auction": "auction",
    # v466 股指（注意：`valuation` 这个键已被外汇估值占用，指数这条必须叫 cape）
    "cape": "cape", "erp": "erp",
    # v467 银铜
    "ratio": "ratio",
    # v467 沪深300 / TIC
    "pe": "cn_pe", "eps": "cn_eps", "tic": "tic",
    # v468/v469 链上与 ETF
    "onchain_addr": "onchain_addr", "onchain_tx": "onchain_tx", "etf_flow": "etf_flow",
    # v470 SOL
    "tvl": "tvl", "fees": "fees", "cot": "cot",
}


_BOND_SET = {"UST", "US10Y", "US30Y", "US2Y", "TLT", "IEF", "SHY", "ZB", "ZN", "ZT"}

_BUILDERS = {    "XAU": _build_gold,
    "WTI": _build_oil,
    "USD": _build_usd,
    "DXY": _build_usd,
    "EURUSD": _build_eurusd,
    "USDJPY": _build_usdjpy,
}


def canonical(asset: Any) -> Optional[str]:
    label = str(asset or "").strip().upper().replace(" ", "")
    label = _ALIASES.get(label, label)
    return label if label in SUPPORTED else None


def build(asset: Any, conn: Optional[sqlite3.Connection] = None,
          use_cache: bool = True) -> Dict[str, Any]:
    """算一个非公司标的的基本面分量。任何异常都降级为 available=False。"""
    label = canonical(asset)
    if label is None:
        return {"available": False, "score": 0.0, "kind": "alt", "asset": None,
                "ticker": None, "reason": "unsupported",
                "detail": f"{str(asset or '').strip().upper() or '空'} 未覆盖非公司基本面（明确不支持，不猜）",
                "components": {}, "evidence": [], "gaps": [],
                "dispersion": None, "dispersion_level": None,
                "confidence": None, "bull_weight": None, "bear_weight": None}
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(label)
            if hit and hit[0] > time.time():
                return dict(hit[1])

    own = False
    try:
        if conn is None:
            import db as _db
            conn = _db.get_connection()
            own = True
        gaps: List[str] = []
        if label in ("BTC", "ETH"):
            body = _build_crypto(conn, label, gaps)
        elif label == "SOL":
            body = _build_sol(conn, gaps)
        elif label in _BOND_SET:
            body = _build_ust(conn, label, gaps)
        elif label in _INDEX_SET:
            body = _build_index(conn, label, gaps)
        elif label in ("XAG", "CU"):
            body = _build_metal(conn, label, gaps)
        elif label == "CSI300":
            body = _build_cn_index(conn, gaps)
        else:
            body = _BUILDERS[label](conn, gaps)
    except Exception as exc:
        return {"available": False, "score": 0.0, "kind": "alt", "asset": label,
                "ticker": None, "reason": "error", "detail": f"非公司基本面出错: {type(exc).__name__}: {str(exc)[:70]}",
                "components": {}, "evidence": [], "gaps": []}
    finally:
        if own and conn is not None:
            conn.close()

    used = {k: v for k, v in body["components"].items() if v}
    sources = {_COMP_SOURCE.get(k, k) for k in used}
    if len(used) < 2 or len(sources) < 2:
        out = {"available": False, "score": 0.0, "kind": "alt", "asset": label,
               "ticker": None, "reason": "insufficient", "market": "ALT",
               "detail": (f"{label} 可用分量 {len(used)} 个 / 独立数据族 {len(sources)} 个"
                          f"（< 2 不发分：同一族派生的分量不算独立证据）"),
               "components": body["components"], "evidence": [], "gaps": body["gaps"],
               "dispersion": None, "dispersion_level": None,
               "confidence": None, "bull_weight": None, "bear_weight": None}
    else:
        wsum = sum(v["weight"] for v in used.values())
        score = sum(v["score"] * v["weight"] for v in used.values()) / wsum
        # v472：分歧度 —— 加权平均只是"Σv"，不是"共识"。分量互相打架时，
        # 同一个分数可能来自"全体轻微偏多"也可能来自"一半极多一半极空"，
        # 后者绝不能当共识用。这里把分歧度量出来，并给出多头/空头权重与可信度。
        disp = (sum(v["weight"] * (v["score"] - score) ** 2
                    for v in used.values()) / wsum) ** 0.5        # 0（完全同向）→ ~2（正负极端对峙）
        level = "高" if disp >= 0.6 else ("中" if disp >= 0.35 else "低")
        conf = round(max(0.0, 1.0 - disp / 1.5), 3)               # disp=0 → 1.0；disp=1.5 → 0
        bull_w = round(sum(v["weight"] for v in used.values() if v["score"] > 0.05), 2)
        bear_w = round(sum(v["weight"] for v in used.values() if v["score"] < -0.05), 2)
        ev = [f"{k}={v['score']:+.2f}（{v['detail']}）" for k, v in used.items()]
        out = {"available": True, "score": round(_clamp(score), 4), "kind": "alt",
               "asset": label, "ticker": None, "market": "ALT", "reason": "ok",
               "detail": f"{label} 非公司基本面 {score:+.2f}："
                         f"{len(used)} 个分量加权（分歧度{level}、可信度 {conf:.2f}，权重和 {wsum:.2f}"
                         f"{('，' + body['duration']) if body.get('duration') else ''}）",
               "components": body["components"],
               "duration": body.get("duration"),
               # v465：美债 7 个分量都要能审计，evidence 上限从 4 放到 8
               "evidence": ev[:8], "gaps": body["gaps"],
               # v472：分歧度三件套 —— 分数可信不可信，一眼可判
               "dispersion": round(disp, 3), "dispersion_level": level,
               "confidence": conf, "bull_weight": bull_w, "bear_weight": bear_w}
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[label] = (time.time() + _CACHE_TTL, dict(out))
    return out
