"""v225 Stage 5 — External Data Ingest.

让 worldview 的「缺证据 regime」可被外部数据源覆盖：
- 自动 fetch：Yahoo Finance 免费 JSON API（无 key，主流标的 OK）
- 手动 ingest：POST 任意 (asset, indicator, value, ts, source) 直接入库
- 新表 external_indicators：(asset, indicator, value, ts, source, regime_tags)
- belief_store._synthesize_from_children 多读这张表作为额外 evidence
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests


YFINANCE_HEADERS = {"User-Agent": "Mozilla/5.0 (Trident Belief Store)"}

INDICATOR_REGIMES = {
    "DX-Y.NYB": ["usd_strong", "em_funding_stress"],
    "^VIX": ["low_vol", "high_vol", "risk_off"],
    "BTC-USD": ["structural_demand"],
    "ETH-USD": [],
    "^GSPC": ["risk_on", "risk_off"],
    "^TNX": ["rates_rising"],
    "GC=F": ["risk_off"],
    "CL=F": [],
    "^IXIC": ["risk_on", "risk_off"],
    "TLT": ["rates_rising"],
    "IEF": ["rates_rising"],
}


def _build_session() -> requests.Session:
    """带 retry + backoff 的 Session，应对 Yahoo Finance 偶发 SSL EOF。"""
    sess = requests.Session()
    sess.headers.update(YFINANCE_HEADERS)
    retry = requests.adapters.Retry(
        total=4, backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


_YF_SESSION = _build_session()


def _ensure_external_schema(conn: sqlite3.Connection) -> None:
    """建 external_indicators 表（若不存在）。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS external_indicators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            indicator TEXT NOT NULL,
            value REAL NOT NULL,
            ts INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            regime_tags TEXT NOT NULL DEFAULT '[]',
            meta TEXT NOT NULL DEFAULT '{}',
            ingested_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_external_indicator_ts
        ON external_indicators(indicator, ts)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_external_asset_indicator
        ON external_indicators(asset, indicator)
    """)
    conn.commit()


def _infer_regime_tags(indicator: str) -> List[str]:
    return list(INDICATOR_REGIMES.get(indicator, []))


def fetch_yfinance(symbol: str, lookback_days: int = 30) -> List[Dict[str, Any]]:
    """从 Yahoo Finance 免费 JSON API 抓日频数据。无 key。"""
    end = int(time.time())
    start = end - lookback_days * 86400
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={start}&period2={end}&interval=1d"
    )
    r = _YF_SESSION.get(url, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(
            f"Yahoo Finance HTTP {r.status_code} for {symbol}: {r.text[:200]}"
        )
    data = r.json()
    chart = data.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(
            f"Yahoo Finance error for {symbol}: {chart['error']}"
        )
    result = chart.get("result", [{}])[0]
    ts_list = result.get("timestamp", [])
    closes = (
        result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    )
    out: List[Dict[str, Any]] = []
    for ts, close in zip(ts_list, closes):
        if close is None:
            continue
        out.append({"ts": int(ts), "value": float(close)})
    if not out:
        raise RuntimeError(
            f"Yahoo Finance returned no data for {symbol} (range too small?)"
        )
    return out


def db_path():
    """读 backend 真实 DB 路径（与 db.py 同源）。"""
    import db as _db
    return _db.get_db_path()


def _to_sec_ts(v: Any) -> int:
    """ms / s 混用的时间戳统一成秒（>=1e11 视为毫秒）。

    1e11 秒 ≈ 5138 年 —— 任何合法秒级时间戳都到不了这个量级，
    所以这个阈值只会命中毫秒/微秒值，不会误伤。
    """
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    if n >= 10 ** 11:
        n //= 1000
        if n >= 10 ** 11:      # 微秒
            n //= 1000
    return n


def ingest_external(
    asset: str,
    indicator: str,
    records: List[Dict[str, Any]],
    source: str = "manual",
    regime_tags: Optional[List[str]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """写入 external_indicators 表。records: [{ts, value}, ...]"""
    # 全局拦截：雅虎类源已被判定失效（429），无论哪个调用方传进来都不再落库，
    # 否则会不断把 12 小时前的死数据重新写回来（实测删完又被补回 420 行）。
    src_low = (source or "").lower()
    if "yfinance" in src_low or "yahoo" in src_low:
        print(f"[SKIP] 已停用的源，拒绝写入: source={source} asset={asset}", flush=True)
        return {"written": 0, "asset": asset, "indicator": indicator, "source": source,
                "skipped": True, "reason": "source disabled (yahoo 429)"}
    if regime_tags is None:
        regime_tags = _infer_regime_tags(indicator)
    meta_str = json.dumps(meta or {})
    tags_str = json.dumps(regime_tags)
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    # v342: isolation_level=None（自动提交）。原来是默认隔离级别 —— 下面"查重 SELECT"
    # 会先开一个**读事务**，紧接的 INSERT 需要把它升级成写事务；只要别的进程刚提交过写，
    # SQLite 立刻返回 SQLITE_BUSY，**busy_timeout 完全不生效**（busy handler 只处理
    # "拿不到锁"，不处理快照升级冲突）。这正是 tencent_quotes 24h 里 188 次
    # "写入失败: database is locked" 的成因 —— 每条行情都是独立小写，本不需要事务。
    _ensure_external_schema(conn)
    try:
        cur = conn.cursor()
        written = 0
        for rec in records:
            # v336 P0-7: ts 统一成**秒**。同一个表里原本混着两种单位：
            # 秒级源 5.2 万行（sina_etf/tencent/binance…），毫秒级源 2.7 万行
            # （coinmetrics/derived_ratio/derived_crypto 直接拿 K 线 ts_ms 落库）。
            # 下游全部按秒做窗口（`ts >= cutoff`、`BETWEEN lo AND hi`、`now - ts`），
            # 于是毫秒行的 age 变成负数 → 永远判定"新鲜"；窗口查询又把它们整段排除。
            # 写入端归一后表内单位一致，读端不用再猜。
            ts = _to_sec_ts(rec["ts"])
            val = float(rec["value"])
            # 幂等写入：同一 (asset, indicator, ts) 已存在就不重复写。
            # 表上没有唯一约束，多轮采集（尤其是同一秒触发两次）会累积重复行，
            # 实测库里因此攒了 259 条完全相同的行，下游读到时会被当成多份样本。
            dup = cur.execute(
                "SELECT 1 FROM external_indicators WHERE asset=? AND indicator=? AND ts=? LIMIT 1",
                (asset, indicator, ts),
            ).fetchone()
            if dup:
                continue
            try:
                import db_retry as _dr
                ok = _dr.insert_with_retry(
                    cur,
                    """INSERT INTO external_indicators
                       (asset, indicator, value, ts, source, regime_tags, meta)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (asset, indicator, val, ts, source, tags_str, meta_str),
                )
            except Exception:
                cur.execute(
                    """INSERT INTO external_indicators
                       (asset, indicator, value, ts, source, regime_tags, meta)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (asset, indicator, val, ts, source, tags_str, meta_str),
                )
                ok = True
            if ok:
                written += 1
        conn.commit()
        return {
            "written": written,
            "asset": asset,
            "indicator": indicator,
            "source": source,
            "regime_tags": regime_tags,
        }
    finally:
        conn.close()


def fetch_and_ingest(
    asset: str,
    yf_symbol: str,
    lookback_days: int = 30,
    regime_tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """一键：fetch_yfinance + ingest_external。

    v331: Yahoo v8 接口长期 429（实测序列停在 12–32 小时前，回补无法推进），
    这条源已判定失效，默认不再往库里写。日线改由 daily_bars.py 从
    币安 fapi 与腾讯 fqkline 回补（OHLC 写进 kline_bars，语义更对）。
    若将来 Yahoo 恢复，把 YFINANCE_DISABLED 设为 False 即可。
    """
    YFINANCE_DISABLED = True
    if YFINANCE_DISABLED:
        print(f"[SKIP] yfinance 已停用（源失效）：{yf_symbol}", flush=True)
        return {"written": 0, "asset": asset, "indicator": yf_symbol,
                "source": "yfinance", "skipped": True,
                "reason": "yahoo 429, source disabled"}
    records = fetch_yfinance(yf_symbol, lookback_days)
    return ingest_external(
        asset=asset,
        indicator=yf_symbol,
        records=records,
        source=f"yfinance:{yf_symbol}",
        regime_tags=regime_tags,
        meta={"lookback_days": lookback_days},
    )


def list_indicators(
    asset: Optional[str] = None,
    indicator: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """列出已 ingest 的指标记录。"""
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    _ensure_external_schema(conn)
    try:
        sql = "SELECT * FROM external_indicators WHERE 1=1"
        params: List[Any] = []
        if asset:
            sql += " AND asset=?"
            params.append(asset)
        if indicator:
            sql += " AND indicator=?"
            params.append(indicator)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["regime_tags"] = json.loads(d["regime_tags"] or "[]")
            except Exception:
                d["regime_tags"] = []
            try:
                d["meta"] = json.loads(d["meta"] or "{}")
            except Exception:
                d["meta"] = {}
            d["ts_iso"] = datetime.fromtimestamp(d["ts"]).isoformat()
            out.append(d)
        return out
    finally:
        conn.close()


def coverage_for_worldview(belief_id: int) -> Dict[str, Any]:
    """把 worldview 的子链 regime_tags 与 external_indicators 做交集，
    给出「哪些 regime 现在有外部数据」。"""
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    _ensure_external_schema(conn)
    conn.row_factory = sqlite3.Row
    try:
        children = conn.execute(
            "SELECT regime_tags FROM beliefs WHERE parent_id=? AND status='active'",
            (belief_id,),
        ).fetchall()
        declared_regimes = set()
        for c in children:
            try:
                for tag in json.loads(c["regime_tags"] or "[]"):
                    declared_regimes.add(str(tag))
            except Exception:
                pass
        indicators_by_regime = {}
        if declared_regimes:
            for r in conn.execute(
                "SELECT DISTINCT indicator, regime_tags FROM external_indicators"
            ).fetchall():
                try:
                    tags = json.loads(r["regime_tags"] or "[]")
                except Exception:
                    tags = []
                for t in tags:
                    indicators_by_regime.setdefault(str(t), set()).add(
                        str(r["indicator"]))
        covered = {}
        for regime in sorted(declared_regimes):
            inds = sorted(indicators_by_regime.get(regime, set()))
            covered[regime] = {
                "indicators": inds,
                "status": "COVERED" if inds else "MISSING",
            }
        return {
            "belief_id": belief_id,
            "declared_regimes": sorted(declared_regimes),
            "covered": covered,
            "newly_covered_count": sum(
                1 for v in covered.values() if v["status"] == "COVERED"
            ),
        }
    finally:
        conn.close()
