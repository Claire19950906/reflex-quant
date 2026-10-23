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


def ingest_external(
    asset: str,
    indicator: str,
    records: List[Dict[str, Any]],
    source: str = "manual",
    regime_tags: Optional[List[str]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """写入 external_indicators 表。records: [{ts, value}, ...]"""
    if regime_tags is None:
        regime_tags = _infer_regime_tags(indicator)
    meta_str = json.dumps(meta or {})
    tags_str = json.dumps(regime_tags)
    conn = sqlite3.connect(db_path())
    _ensure_external_schema(conn)
    try:
        cur = conn.cursor()
        written = 0
        for rec in records:
            ts = int(rec["ts"])
            val = float(rec["value"])
            cur.execute(
                """INSERT INTO external_indicators
                   (asset, indicator, value, ts, source, regime_tags, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (asset, indicator, val, ts, source, tags_str, meta_str),
            )
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
    """一键：fetch_yfinance + ingest_external。"""
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
    conn = sqlite3.connect(db_path())
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
    conn = sqlite3.connect(db_path())
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
