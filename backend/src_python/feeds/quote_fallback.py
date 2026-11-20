#!/usr/bin/env python3
"""降级取数：yfinance 拿不到时，用真实可访问的替代源顶上。

不变量：宁可返回 None（由调用方标缺失），也不编造数字。
每个降级源都必须带来源标记，让上层能区分"实时"和"日频"。

注意新浪的限流方式：同一秒内连发两个单代码请求，第二个会返回空串
（实测 hf_ES / hf_NQ 交替成功）。所以多品种必须合成一个 list= 请求。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional

SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}
SINA_URL = "https://hq.sinajs.cn/list={codes}"
_TIMEOUT = 6.0
_TTL = 30.0
_CACHE: Dict[str, Any] = {}


def _parse_sina_line(text: str) -> Optional[Dict[str, Any]]:
    """解析一行新浪外盘行情串 → {price, prev_close}。"""
    try:
        body = text.split('"')[1]
    except IndexError:
        return None
    parts = body.split(",")
    if not parts or not parts[0]:
        return None
    try:
        price = float(parts[0])
    except ValueError:
        return None
    if price <= 0:
        return None
    prev = None
    for idx in (8, 3, 7):  # 收盘后昨收在第 9 位
        try:
            v = float(parts[idx])
        except (IndexError, TypeError, ValueError):
            continue
        if v > 0:
            prev = v
            break
    return {"price": price, "prev_close": prev}


def sina_hf_multi(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """一次请求取多个新浪外盘合约。返回 {code: {price, prev_close, source, ts}}。

    实测行情串: `var hq_str_hf_ES="7557.243,,7560.500,...,7620.250,..."`
    新浪必须先带 Referer，否则 403。
    """
    now = time.time()
    key = "multi:" + ",".join(codes)
    cached = _CACHE.get(key)
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]
    out: Dict[str, Dict[str, Any]] = {}
    try:
        import requests

        r = None
        for attempt in range(3):          # 新浪偶发返回空串（同一秒连发会限流），重试 3 次
            try:
                _s = requests.Session()
                _s.trust_env = False        # launchd 进程会读 env 代理，而那些端口全是死的
                r = _s.get(SINA_URL.format(codes=",".join(codes)),
                           headers=SINA_HEADERS, timeout=_TIMEOUT)
                r.raise_for_status()
                if "hq_str" in (r.text or ""):
                    break
            except Exception:
                if attempt == 2:
                    raise
            time.sleep(0.6)
        for line in r.text.strip().splitlines():
            if "hq_str" not in line:
                continue
            code = line.split("hq_str_")[1].split("=")[0]
            parsed = _parse_sina_line(line)
            if parsed:
                parsed["source"] = f"sina_{code}"
                parsed["ts"] = int(now)
                out[code] = parsed
    except Exception:
        return {}
    if out:
        _CACHE[key] = {"ts": now, "data": out}
    return out


def sina_hf(code: str) -> Optional[Dict[str, Any]]:
    """单品种取数（内部走 multi，避免限流）。"""
    return sina_hf_multi([code]).get(code)


def db_latest(indicators: List[str], max_age_s: float = 36 * 3600) -> Optional[Dict[str, Any]]:
    """从 external_indicators 取最近一条真实入库值（超过 max_age_s 视为过期，返回 None）。"""
    try:
        import db

        conn = sqlite3.connect(db.get_db_path(), timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            for ind in indicators:
                row = conn.execute(
                    "SELECT value, ts, source FROM external_indicators "
                    "WHERE indicator=? ORDER BY ts DESC LIMIT 1",
                    (ind,),
                ).fetchone()
                if not row or row[0] is None:
                    continue
                value, ts, source = float(row[0]), int(row[1]), row[2] or ind
                if time.time() - ts > max_age_s:
                    continue  # 过期不给，宁缺勿假
                return {"value": value, "ts": ts, "source": source, "indicator": ind}
            return None
        finally:
            conn.close()
    except Exception:
        return None
