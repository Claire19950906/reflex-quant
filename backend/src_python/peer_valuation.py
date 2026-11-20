#!/usr/bin/env python3
"""同业可比估值 —— 回答「贵不贵」缺的是横向锚（v454）。

问题：v448 的估值只有**自身历史分位**（PE/PB 在过去一年什么位置）。它答不了
"这家公司相对同行贵不贵"：一只票可以永远处在自己历史的高分位，同时比同业便宜一半。
PE 陷阱（周期顶部便宜）和"所有科技股都在历史高位"这两类误读，都缺这一半信息。

口径：
  · 分行业静态分组（`SECTORS`）。系统里没有行业字段，靠名字猜行业会出错，
    所以这份表是**人工维护的清单**，改一行就换一组。
  · 分组里既有决策名单（config.EQUITY_TICKERS）的票，也有**只做锚的票**
    （AMD/AVGO/INTC/TSM/NOW/SNOW 等）。锚票只贡献中位数，自己不参与决策。
    没有它们，半导体组只剩 MU+SMCI 两只、"中位数"就是另一个数的别名。
  · 倍数来源两级，按优先级：
      1) 本系统 `macro_snapshots` 的 eq.pe / eq.pb 快照（同日可审计）；
      2) yfinance 的 trailingPE / priceToBook（免费、落盘缓存 6h，标 source=yf）。
  · 同业中位数只用「有取值的同组票」算，**至少 2 只**（不含自己）才给结论。
  · 相对读数：便宜度 = -(自身倍数 ÷ 同业中位数 − 1) ÷ 50%，截到 ±1。

决策链与网络：`build()` 默认 `allow_network=False`，只读库 + 本地缓存 —— AI worker
是热路径，不能因为一个同业查询卡住判定。网络补数由 cron 的 `warm()` 预热。

不做什么：
  · 不在样本不足时拿"全市场平均"顶替（那是另一种假精度）；
  · 不把不同商业模式硬塞一组（MSTR 是 BTC 财库票，归"加密资产杠杆"，不进半导体）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

_PEER_MIN = 2          # 不含自己至少 2 只
_SCALE = 0.5           # 相对倍数差 50% 打到饱和
_SNAPSHOT_MAX_AGE = 10        # 快照有效期（天）
_YF_TTL = 6 * 3600
_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime", "peer_cache")

SECTORS: Dict[str, Tuple[str, ...]] = {
    # 决策名单内的票 + 只做锚的票（**人工复核过**的分组，优先级高于自动识别）
    "semis": ("NVDA", "MU", "SMCI", "AMD", "AVGO", "INTC", "TSM", "QCOM"),
    "crypto_miner": ("MARA", "RIOT", "CLSK", "HUT", "CIFR", "BITF"),
    "crypto_asset": ("COIN", "MSTR", "HOOD"),
    "mega_tech": ("AAPL", "MSFT", "GOOGL", "AMZN", "META"),
    "ai_growth": ("PLTR", "TSLA", "NOW", "SNOW"),
}

# ── v456：自动分组 ─────────────────────────────────────────────────────────
# 手写 26 只票的意思是"新票进来就没有同业锚"（宁可不给结论也不猜）。
# 修法：用供应商的 industry 字段做**规则映射**，命中已知桶就自动入组；
# 只命中粗行业（GICS 11 类）时归到 `gics:*` 粗桶 —— 粗桶照用，但证据里必须写明
# "粗分组"三个字，让读到结论的人知道这个锚的精度不一样。
# 行业字段读不到就**不猜**，宁可仍走 "不在任何分组内"。
_INDUSTRY_RULES: Tuple[Tuple[str, str], ...] = (
    ("semiconductor", "semis"),
    ("semiconductor equipment", "semis"),
    ("crypto mining", "crypto_miner"),
    ("capital markets", "crypto_asset"),          # COIN/HOOD 类券商，与矿股分开
    ("financial exchanges", "crypto_asset"),
    ("software - infrastructure", "software"),
    ("software - application", "software"),
    ("information technology services", "software"),
    ("internet content", "mega_tech"),
    ("consumer electronics", "mega_tech"),
    ("internet retail", "mega_tech"),
    ("auto manufacturers", "ev_auto"),
    ("communication services", "mega_tech"),
)

# 财库票：净资产主要是持币/持债，PB 高低反映的是「市值 vs 币价」，不是「贵不贵」。
PB_EXCLUDED: Dict[str, str] = {
    "MSTR": "财库票（净资产以 BTC 为主，PB 反映市值/持币价值，不代表估值贵贱）",
}

_MULT_CACHE: Dict[str, Tuple[float, Dict[str, Optional[float]]]] = {}
_SECTOR_CACHE: Dict[str, Dict[str, Any]] = {}
_SECTOR_TTL = 30 * 86400          # 行业归属变化很慢，30 天足够


def _sector_cache_path() -> str:
    return os.path.join(_CACHE_DIR, "sectors.json")


def _load_sectors() -> Dict[str, Dict[str, Any]]:
    global _SECTOR_CACHE
    if _SECTOR_CACHE:
        return _SECTOR_CACHE
    try:
        with open(_sector_cache_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _SECTOR_CACHE = data
    except Exception:
        _SECTOR_CACHE = {}
    return _SECTOR_CACHE


def _save_sectors() -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _sector_cache_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_SECTOR_CACHE, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, _sector_cache_path())
    except Exception:
        pass


def _bucket_from_industry(industry: str, sector: str) -> Optional[str]:
    """industry/sector 文本 → 分桶。命中具体行业优先，否则退 GICS 粗桶。"""
    low = (industry or "").strip().lower()
    for needle, bucket in _INDUSTRY_RULES:
        if needle in low:
            return bucket
    sec = (sector or "").strip().lower()
    if sec:
        return "gics:" + sec.replace(" ", "_")
    return None


def _sec_industry(ticker: str) -> Dict[str, Any]:
    """SEC 兜底：用 SIC 描述做行业识别（`submissions/CIK*.json` 里有 sicDescription）。

    为什么必须有这一路：本机实测 yfinance 的 `.info` 会间歇性 "Cookie/crumb fetch failed
    (SSLError)"，industry 拿不到 → 自动分组静默失效（ASML/MRVL 都识别不了）。
    SEC 这条之前已经验证可用（净资产就是走 data.sec.gov），且 SIC 描述比 GICS 更细。
    """
    try:
        import sec_fundamentals as _sf
        cik = _sf.cik_map().get(str(ticker).upper())
        if not cik:
            return {}
        import urllib.request
        url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
        req = urllib.request.Request(url, headers={
            "User-Agent": "reflex-quant research z2132743607@163.com"})
        data = json.load(urllib.request.urlopen(req, timeout=20))
        sic_desc = str(data.get("sicDescription") or "")
        bucket = _bucket_from_industry(sic_desc, "")
        if not bucket:
            return {}
        return {"bucket": bucket, "industry": sic_desc, "sector": "",
                "ts": int(time.time()), "src": "sec_sic"}
    except Exception:
        return {}


def _fetch_industry(ticker: str) -> Dict[str, Any]:
    """网络取一只票的行业（yfinance 优先，失败退 SEC SIC；都失败就返回 {}，不猜）。"""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        industry = str(info.get("industry") or "")
        sector = str(info.get("sector") or "")
        bucket = _bucket_from_industry(industry, sector)
        if bucket:
            return {"bucket": bucket, "industry": industry, "sector": sector,
                    "ts": int(time.time()), "src": "yfinance"}
    except Exception:
        pass
    return _sec_industry(ticker)


def sector_of(ticker: str, allow_network: bool = False) -> Optional[str]:
    """票 → 分桶。人工分组 > 缓存 > （允许联网时）现场识别。

    自动识别结果落盘 30 天，热路径（决策链）只读盘不发网络请求。
    """
    ticker = str(ticker).upper()
    for name, members in SECTORS.items():
        if ticker in members:
            return name
    cache = _load_sectors()
    rec = cache.get(ticker) or {}
    if rec.get("bucket") and time.time() - float(rec.get("ts") or 0) < _SECTOR_TTL:
        return str(rec["bucket"])
    if not allow_network:
        return str(rec["bucket"]) if rec.get("bucket") else None
    fresh = _fetch_industry(ticker)
    if fresh:
        cache[ticker] = fresh
        _save_sectors()
        return fresh["bucket"]
    return str(rec["bucket"]) if rec.get("bucket") else None


def sector_kind(ticker: str) -> str:
    """这个锚是人工复核的、自动识别的，还是粗行业分桶。"""
    ticker = str(ticker).upper()
    for name, members in SECTORS.items():
        if ticker in members:
            return "manual"
    bucket = sector_of(ticker, allow_network=False)
    if not bucket:
        return "none"
    return "coarse" if bucket.startswith("gics:") else "auto"


def members_of(bucket: str, allow_network: bool = False) -> List[str]:
    """桶内成员 = 人工分组成员 ∪ 自动识别到该桶的票。"""
    out = set()
    for name, members in SECTORS.items():
        if name == bucket:
            out.update(members)
    for tk, rec in _load_sectors().items():
        if str(rec.get("bucket") or "") == bucket:
            out.add(str(tk).upper())
    return sorted(out)


def pb_excluded(ticker: str) -> Optional[str]:
    """这只票的 PB 是否不该参与估值判断（财库票）。返回原因或 None。"""
    return PB_EXCLUDED.get(str(ticker).upper())


def _cache_path() -> str:
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "runtime", "peer_cache")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = "/tmp"
    return os.path.join(base, "multiples.json")


def _load_disk() -> Dict[str, Dict[str, Any]]:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_disk(data: Dict[str, Dict[str, Any]]) -> None:
    try:
        tmp = _cache_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, _cache_path())
    except Exception:
        pass


def _latest_metric(conn: Optional[sqlite3.Connection], ticker: str, key: str) -> Optional[float]:
    if conn is None:
        return None
    cutoff = int(time.time()) - _SNAPSHOT_MAX_AGE * 86400
    try:
        row = conn.execute(
            """SELECT value FROM macro_snapshots
               WHERE metric_key=? AND value IS NOT NULL AND value > 0 AND ts >= ?
                 AND payload LIKE ? ORDER BY ts DESC LIMIT 1""",
            (key, cutoff, f'%"label": "{str(ticker).upper()}"%'),
        ).fetchone()
    except Exception:
        return None
    return float(row[0]) if row else None


def _yf_multiples(ticker: str, allow_network: bool) -> Dict[str, Optional[float]]:
    """trailingPE / priceToBook，落盘缓存 6h。allow_network=False 时只用缓存。"""
    ticker = str(ticker).upper()
    now = time.time()
    mem = _MULT_CACHE.get(ticker)
    if mem and mem[0] > now:
        return mem[1]
    disk = _load_disk()
    hit = disk.get(ticker) or {}
    if hit and now - float(hit.get("ts") or 0) < _YF_TTL:
        vals = {"pe": hit.get("pe"), "pb": hit.get("pb"), "source": "yf"}
        _MULT_CACHE[ticker] = (now + _YF_TTL, vals)
        return vals
    if not allow_network:
        return {"pe": None, "pb": None, "source": ""}
    vals = {"pe": None, "pb": None, "source": "yf"}
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        for src_key, out_key in (("trailingPE", "pe"), ("priceToBook", "pb")):
            try:
                n = float(info.get(src_key))
                if n > 0:
                    vals[out_key] = n
            except (TypeError, ValueError):
                continue
    except Exception:
        return {"pe": None, "pb": None, "source": ""}
    disk[ticker] = {"ts": now, "pe": vals["pe"], "pb": vals["pb"]}
    try:
        _save_disk(disk)
    except Exception:
        pass
    _MULT_CACHE[ticker] = (now + _YF_TTL, vals)
    return vals


def multiples(ticker: str, conn: Optional[sqlite3.Connection] = None,
              allow_network: bool = False) -> Dict[str, Optional[float]]:
    """一只票的 PE/PB。快照优先（本系统自采），缺则退回缓存/yfinance。"""
    pe = _latest_metric(conn, ticker, "eq.pe")
    pb = _latest_metric(conn, ticker, "eq.pb")
    src = "snapshot" if (pe or pb) else ""
    if not pe or not pb:
        alt = _yf_multiples(ticker, allow_network)
        if alt.get("pe") and not pe:
            pe, src = alt["pe"], "yf"
        if alt.get("pb") and not pb:
            pb = alt["pb"]
            src = src or "yf"
    return {"pe": pe, "pb": pb, "source": src, "ticker": str(ticker).upper()}


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def relative_score(self_value: Optional[float], peer_values: List[float]) -> Optional[float]:
    """便宜度：低于同业中位数 → 正分。纯函数，便于离线测试。"""
    if self_value is None or self_value <= 0:
        return None
    med = _median([v for v in peer_values if v and v > 0])
    if med is None or med <= 0:
        return None
    rel = self_value / med - 1.0
    return max(-1.0, min(1.0, -rel / _SCALE))


def build(ticker: str, conn: Optional[sqlite3.Connection] = None,
          allow_network: bool = False) -> Dict[str, Any]:
    """一只票的同业可比读数（不可用时 available=False，绝不返回 0 当结论）。"""
    ticker = str(ticker).upper()
    sector = sector_of(ticker, allow_network)
    kind = sector_kind(ticker)
    out: Dict[str, Any] = {"available": False, "ticker": ticker, "sector": sector,
                           "sector_kind": kind,
                           "score": None, "components": {}, "evidence": [], "detail": ""}
    if not sector:
        out["detail"] = "不在任何分组内（行业识别不到，按铁律不给同业结论）"
        return out
    own = False
    try:
        if conn is None:
            import db as _db
            conn = _db.get_connection()
            own = True
        peers = [p for p in members_of(sector, allow_network) if p != ticker]
        mine = multiples(ticker, conn, allow_network)
        peer = [multiples(p, conn, allow_network) for p in peers]
        peer_pe = [m["pe"] for m in peer if m.get("pe")]
        # 财库票不进 PB 中位数：它的净资产是持币，PB 加进去会把整组拉歪
        peer_pb = [m["pb"] for m in peer if m.get("pb") and not pb_excluded(str(m.get("ticker") or ""))]
    except Exception:
        return out
    finally:
        if own and conn is not None:
            conn.close()

    n_peers = max(len(peer_pe), len(peer_pb))
    if n_peers < _PEER_MIN:
        out["detail"] = f"同业有效样本 {n_peers} < {_PEER_MIN}，不作结论"
        return out

    pieces: List[float] = []
    if mine.get("pe") and len(peer_pe) >= _PEER_MIN:
        s = relative_score(mine["pe"], peer_pe)
        med = _median(peer_pe)
        if s is not None and med:
            pieces.append(s)
            out["components"]["rel_pe"] = round(s, 4)
            out["components"]["peer_pe_median"] = round(med, 2)
            out["evidence"].append(
                f"PE {mine['pe']:.1f}x vs 同业中位 {med:.1f}x"
                f"（{sector}，{len(peer_pe)} 只可比）")
    if mine.get("pb") and pb_excluded(ticker):
        out["components"]["pb_excluded"] = pb_excluded(ticker)
        out["evidence"].append(f"PB {mine['pb']:.2f}x 不参与判断：{pb_excluded(ticker)}")
    elif mine.get("pb") and len(peer_pb) >= _PEER_MIN:
        s = relative_score(mine["pb"], peer_pb)
        med = _median(peer_pb)
        if s is not None and med:
            pieces.append(s)
            out["components"]["rel_pb"] = round(s, 4)
            out["components"]["peer_pb_median"] = round(med, 2)
            out["evidence"].append(
                f"PB {mine['pb']:.2f}x vs 同业中位 {med:.2f}x"
                f"（{sector}，{len(peer_pb)} 只可比）")
    if not pieces:
        out["detail"] = "同业有样本但自身倍数缺失（自身 PE/PB 取不到）"
        return out
    out["available"] = True
    out["score"] = round(sum(pieces) / len(pieces), 4)
    out["components"]["peer_group_size"] = n_peers
    out["detail"] = f"{sector} 同业 {n_peers} 只可比（倍数源 {mine.get('source') or 'mixed'}）"
    return out


def warm(tickers: Optional[List[str]] = None, sleep: float = 0.15) -> Dict[str, int]:
    """cron 预热：刷行业归属 + 把 PE/PB 拉进缓存，供热路径只读缓存。

    v456 起顺带刷行业（`sector_of(..., allow_network=True)`）—— 这样新票进来不必
    等到有人在决策链里问它，cron 先把它归好组，热路径永远不会因"没分组"而没有锚。

    返回 {"ok": n, "miss": m, "grouped": g}。失败不抛异常 —— 这条通道是增强，不是主链路。
    """
    names = tickers if tickers is not None else sorted({t for g in SECTORS.values() for t in g})
    ok = miss = grouped = 0
    for t in names:
        if sector_of(t, allow_network=True):
            grouped += 1
        vals = _yf_multiples(t, allow_network=True)
        if vals.get("pe") or vals.get("pb"):
            ok += 1
        else:
            miss += 1
        time.sleep(max(0.0, sleep))
    return {"ok": ok, "miss": miss, "grouped": grouped}


__all__ = ["SECTORS", "PB_EXCLUDED", "sector_of", "sector_kind", "members_of",
           "pb_excluded", "multiples", "relative_score", "build", "warm"]
