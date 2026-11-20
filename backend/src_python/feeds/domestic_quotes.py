#!/usr/bin/env python3
"""国内可访问行情源：Gate.io(加密) / 新浪外盘(hf_*) / 腾讯(美股·韩股)。

为什么存在：本机外盘源全失 —— `www.okx.com` DNS 无解析、`fapi.binance.com` TLS 被重置、
`query1.finance.yahoo.com` 429 + SSL 失败；而下面三个地址实测可达（2026-11-17 07:3x）：

    Gate.io  BTC_USDT 65052.2 / SOL_USDT 71.31 + 1h K 线可拉
    hf_CL    85.36   纽约原油（WTI 本体）
    hf_GC    3857.28  纽约黄金（COMEX 期货，XAU 的代理）
    usNVDA   156.14   美股收盘价      kr000660 1,120,000 韩股 SK hynix（KRW）

纪律（与项目铁律一致）：
1. 只报真实拿到的报价；取不到返回 None，不补值、不用 0 占位。
2. 每个结果都带 `source` 与 `note`：XAU 是 COMEX **期货代理**（非现货）、
   美股/韩股是**收盘价**（非实时盘口）——上层必须能区分，不能混说成"实时"。
3. 衍生品/ETF 不得冒充现货：腾讯 `usBTC` 是灰度迷你 ETF（28.74），列入禁用表。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

_SINA_URL = "https://hq.sinajs.cn/list={codes}"
_TENCENT_URL = "https://qt.gtimg.cn/q={codes}"
_GATE_URL = "https://api.gateio.ws/api/v4/spot/tickers?currency_pair={pair}"
_GATE_KLINE_URL = ("https://api.gateio.ws/api/v4/spot/candlesticks"
                   "?currency_pair={pair}&interval={interval}&limit={limit}")
_TIMEOUT = 3.0
_TTL = 20.0
# 60s 而不是 120s：实测源失败常是整波网络抖动（08:02 一轮 ConnectionReset 全灭），
# 120s 会让"一次抖动"变成"两批都没国内源"。
_FAIL_COOLDOWN_S = 60.0
_SRC_DOWN_UNTIL: Dict[str, float] = {}
_CACHE: Dict[str, Any] = {}

# 腾讯最稳（一次多码，实测 hf_GC 3855.60 / hf_CL 85.28 / usNVDA 156.14 / kr000660 1,120,000）；
# 新浪同域名偶发 SSL 握手失败（curl 35），只当腾讯拿不到时的备份。
_SINA_CODES = {"BTC": "hf_BTC", "XAU": "hf_GC", "WTI": "hf_CL",
               "OIL": "hf_CL", "XAG": "hf_SI", "GOLD": "hf_GC"}
_TENCENT_CODES = {"XAU": "hf_GC", "WTI": "hf_CL", "OIL": "hf_CL",
                  "XAG": "hf_SI", "GOLD": "hf_GC",
                  "NVDA": "usNVDA", "AMZN": "usAMZN", "META": "usMETA",
                  "TSLA": "usTSLA", "AAPL": "usAAPL", "MSTR": "usMSTR",
                  "MSFT": "usMSFT", "GOOGL": "usGOOGL", "COIN": "usCOIN",
                  "PLTR": "usPLTR", "SMCI": "usSMCI", "MARA": "usMARA",
                  "RIOT": "usRIOT", "MU": "usMU", "US_HYNIX": "usMU",
                  "SKHYNIX": "kr000660"}
_GATE_PAIRS = {"BTC": "BTC_USDT", "ETH": "ETH_USDT", "SOL": "SOL_USDT"}
# 第三兜底：Gate 单品种偶发 TLS 超时（实测 ETH），CoinPaprika 有时能顶上
_CP_IDS = {"BTC": "btc-bitcoin", "ETH": "eth-ethereum", "SOL": "sol-solana"}
_CP_URL = "https://api.coinpaprika.com/v1/tickers/{cid}"

_FORBIDDEN = {"usBTC", "usETH", "usETHV", "usBITO"}

_INSTRUMENT_NOTE = {
    "TSLA": "美股收盘价（非实时盘口）",
    "AAPL": "美股收盘价（非实时盘口）",
    "MSTR": "美股收盘价（非实时盘口）",
    "MSFT": "美股收盘价（非实时盘口）",
    "GOOGL": "美股收盘价（非实时盘口）",
    "COIN": "美股收盘价（非实时盘口）",
    "PLTR": "美股收盘价（非实时盘口）",
    "SMCI": "美股收盘价（非实时盘口）",
    "MARA": "美股收盘价（非实时盘口）",
    "RIOT": "美股收盘价（非实时盘口）",
    "MU": "美股收盘价（非实时盘口）",
    "US_HYNIX": "美股收盘价（非实时盘口）",
    "NVDA": "美股收盘价（非实时盘口）",
    "AMZN": "美股收盘价（非实时盘口）",
    "META": "美股收盘价（非实时盘口）",
    "SKHYNIX": "韩股收盘价（KRW，非实时盘口）",
}

def _note_for(asset: str, source: str) -> str:
    """说明跟来源走：同一个资产走 Gate / 新浪 / 腾讯，说法必须不同。"""
    if source.startswith("gate_"):
        return "Gate.io 现货成交价"
    if source.startswith("sina_"):
        return "新浪外盘 hf_" + _SINA_CODES.get(asset, "")[3:] + (
            "（COMEX 期货代理，非现货）" if asset == "XAU" else "")
    if source.startswith("tencent_"):
        code = _TENCENT_CODES.get(asset, "")
        if code.startswith("hf_"):
            return "腾讯外盘 hf_" + code[3:] + ("（COMEX 期货代理，非现货）" if asset == "XAU" else "")
        return _INSTRUMENT_NOTE.get(asset, "")
    return ""


def _http_get(url: str, headers: Optional[dict] = None, retries: int = 2,
              timeout: Optional[float] = None) -> str:
    import requests

    last = None
    for i in range(retries):
        try:
            sess = requests.Session()
            sess.trust_env = False      # launchd 下的 env 代理是死的，必须绕过
            r = sess.get(url, headers=headers or {}, timeout=timeout or _TIMEOUT)
            r.raise_for_status()
            try:
                return r.content.decode("gbk")     # 新浪/腾讯外盘是 GBK
            except Exception:
                return r.content.decode("utf-8", errors="ignore")
        except Exception as e:
            last = e
            if i + 1 < retries:
                time.sleep(0.4)
    if last:
        raise last
    return ""


def _parse_sina_hf(line: str) -> Optional[Dict[str, Any]]:
    """`var hq_str_hf_CL="85.40,-0.37,85.37,85.38,85.59,85.06,07:31:08,85.72,...`  price,chg%,...,prev_close"""
    try:
        body = line.split('"')[1]
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
    for idx in (7, 8, 3):
        try:
            v = float(parts[idx])
        except (IndexError, ValueError):
            continue
        if v > 0:
            prev = v
            break
    chg = None
    if prev:
        chg = (price - prev) / prev * 100.0
    return {"price": price, "prev_close": prev, "change_pct": chg}


def _parse_tencent(line: str) -> Optional[Dict[str, Any]]:
    """`v_usNVDA="200~英伟达~NVDA.OQ~156.14~162.85~...`  价格在 [3]，昨收在 [4]"""
    try:
        body = line.split('"')[1]
    except IndexError:
        return None
    f = body.split("~")
    if len(f) < 5:
        return None
    try:
        price = float(f[3])
    except (IndexError, ValueError):
        return None
    if price <= 0:
        return None
    prev = None
    try:
        prev = float(f[4]) or None
    except (IndexError, ValueError):
        prev = None
    chg = None
    if prev and prev > 0:
        chg = (price - prev) / prev * 100.0
    return {"price": price, "prev_close": prev, "change_pct": chg}


def _gate_ticker(pair: str) -> Optional[Dict[str, Any]]:
    import json as _json

    # 不重试：Gate 偶发 TLS 握手超时，重试一次就把 3s 变 6s，整段预算被吃光
    text = _http_get(_GATE_URL.format(pair=pair), retries=1)
    rows = _json.loads(text)
    if not isinstance(rows, list) or not rows:
        return None
    d = rows[0]
    price = float(d.get("last") or 0.0)
    if price <= 0:
        return None
    chg = None
    try:
        chg = float(d.get("change_percentage"))
    except (TypeError, ValueError):
        chg = None
    return {"price": price, "change_pct": chg}


_SINA_DAILY_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
                   "var%20_{sym}/GlobalFuturesService.getGlobalFuturesDailyKLine"
                   "?symbol={sym}")
# 新浪外盘日线：只有日线够用。分钟线接口（getGlobalFuturesMinLine）实测只回
# 当天 127–128 个点（07:00 起），聚合不出 4h/1h —— 所以这里只补 1d，
# 4h/1h 仍留给远端/本地，不拿"半天数据"硬凑。
_SINA_DAILY_SYMS = {"XAU": "GC", "WTI": "CL"}
_SINA_DAILY_BARS = 12


def sina_daily_klines(sym: str, bars: int = _SINA_DAILY_BARS) -> List[list]:
    """新浪外盘期货日线 → [[ts, o, h, l, c, v], ...]（升序，末尾是当天）。

    symbol 用新浪的品种码：GC=COMEX 黄金、CL=纽约原油。
    返回体是 JSONP（`var _GC([...]);`），前面还有一段防盗链 script，需剥壳。
    """
    import json as _json
    import re as _re

    text = _http_get(_SINA_DAILY_URL.format(sym=sym),
                     headers={"Referer": "https://finance.sina.com.cn/"},
                     retries=2, timeout=4.0)
    m = _re.search(r"var\s+_%s\((.*)\)\s*;" % _re.escape(sym), text, _re.S)
    if not m:
        raise ValueError("sina daily jsonp 结构不符")
    rows = _json.loads(m.group(1))
    out: List[list] = []
    for r in rows or []:
        try:
            d = str(r.get("date") or "")
            ts = int(time.mktime(time.strptime(d, "%Y-%m-%d")))
            o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
            if c <= 0:
                continue
            out.append([ts, o, h, l, c, float(r.get("volume") or 0.0)])
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x[0])[-bars:]


_EM_KLINE_URL = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
                 "?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
                 "&klt={klt}&fqt=1&end=20500101&lmt={limit}")
# 东财国际期货 secid：101=COMEX、102=NYMEX。实测 101.CL00Y 是空数据，原油在 102。
_EM_SECIDS = {"XAU": "101.GC00Y", "WTI": "102.CL00Y"}
_EM_HDRS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}


def em_klines(secid: str, klt: int = 60, limit: int = 200,
              retries: int = 4, sess: Any = None) -> List[list]:
    """东方财富 K 线 → [[ts,o,h,l,c,v], ...]（升序）。

    fields2 顺序是 f51=时间 f52=开 f53=收 f54=高 f55=低 f56=量（注意"收"在"高"前面，
    按 Gate 那套顺序照抄会把高低写反）。

    可靠性实测（2026-11-17 10:0x）：这台机器到 `push2his.eastmoney.com` 约 **50% 连接被重置**，
    失败很快（0.2–1s，不是超时挂住），所以短重试很划算：retries=3 → 理论失败率 ~12%。
    另外 lmt/limit 给再大也只回最近 **118 根 1h**（≈5 个交易日）——够聚合 1h/4h，
    不够 8 根日线，所以商品 1d 仍由新浪日线负责。
    """
    import json as _json
    import requests as _rq

    # 关键：**必须复用同一个 Session**。实测这台机器到 push2his 的首次握手经常被重置，
    # 每次新建连接 3 次全失败；复用 keep-alive 后 12 次里 11 次成功（92%）。
    own = sess is None
    if own:
        sess = _rq.Session()
        sess.trust_env = False
    url = _EM_KLINE_URL.format(secid=secid, klt=klt, limit=limit)
    last = None
    d = None
    for _ in range(max(1, retries)):
        try:
            r = sess.get(url, headers=_EM_HDRS, timeout=3.0)
            if r.status_code == 200:
                d = _json.loads(r.text)
                if d.get("rc") == 0 and d.get("data"):
                    break
                last = ValueError(f"em rc={d.get('rc')}")
        except Exception as e:
            last = e
        time.sleep(0.2)
    if own:
        try:
            sess.close()
        except Exception:
            pass
    if d is None or not (d.get("rc") == 0 and d.get("data")):
        raise last or ValueError("em kline 无响应")

    out: List[list] = []
    for r in d["data"].get("klines") or []:
        try:
            parts = str(r).split(",")
            ts = int(time.mktime(time.strptime(parts[0], "%Y-%m-%d %H:%M")))
            o, c, h, l = (float(parts[i]) for i in (1, 2, 3, 4))
            v = float(parts[5]) if len(parts) > 5 else 0.0
            if c <= 0:
                continue
            out.append([ts, o, h, l, c, v])
        except (IndexError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x[0])


def gate_klines(pair: str, interval: str = "1h", limit: int = 48) -> List[list]:
    """Gate 现货 K 线 → [[ts,o,h,l,c,v], ...]（与项目内部 K 线结构一致）。

    Gate 返回数组顺序：[ts, quote_volume, close, high, low, open, base_volume, closed]
    """
    import json as _json

    # K 线：Gate 的失败多是间歇性 SSLError（实测约 1/3），与 payload 无关；
    # 一次短超时重试能把失败率压到 ~1/10，最坏耗时 2×2s 仍在预算内。
    text = _http_get(_GATE_KLINE_URL.format(pair=pair, interval=interval, limit=limit),
                     retries=2, timeout=3.5)
    rows = _json.loads(text)
    out: List[list] = []
    for r in rows or []:
        try:
            out.append([int(float(r[0])), float(r[5]), float(r[3]), float(r[4]),
                        float(r[2]), float(r[6] or 0.0)])
        except (IndexError, TypeError, ValueError):
            continue
    return out


# Gate 单次 1h/200 根 ≈ 8.3 天，聚合出 1d(8) / 4h(42) / 1h(48) 三个周期。
# 为什么不是分周期各打一枪：实测 9 个并发请求互相拖慢，4s 预算里只回来 3 个周期；
# 单请求 3 个品种 = 3 个并发，稳定在 1–2s，且三个周期来自同一份 1h 序列，口径一致。
_GATE_MTF_BARS = 200
_QUOTE_BUDGET_S = 3.2
_CP_BUDGET_S = 0.8     # CoinPaprika 兜底最多再花 1s（它是最后一层，不值得再拖
_MTF_BUDGET_S = 4.5   # gate/新浪等候上限；东财再单独等 EM_WAIT_S   # 整组报价的总等待上限（引擎给国内段 4s）
EM_WAIT_S = 2.0   # 东财是机会源，最多等 2s，超了就用已有的
_GATE_PERIODS = {"1d": (86400, 8), "4h": (14400, 42), "1h": (3600, 48)}


def _agg(candles: List[list], bucket_s: int, limit: int) -> List[list]:
    """把 1h K 线按秒数分桶聚合为更大周期（o=首开, h=最高, l=最低, c=末收, v=和）。"""
    buckets: Dict[int, list] = {}
    for r in candles:
        b = (int(r[0]) // bucket_s) * bucket_s
        cur = buckets.get(b)
        if cur is None:
            buckets[b] = [b, r[1], r[2], r[3], r[4], r[5]]
        else:
            cur[2] = max(cur[2], r[2])
            cur[3] = min(cur[3], r[3])
            cur[4] = r[4]
            cur[5] = (cur[5] or 0.0) + (r[5] or 0.0)
    return [buckets[k] for k in sorted(buckets)][-limit:]


def _cooldown_key(kind: str, asset: str) -> str:
    """熔断键必须跟"源+品种"走。

    之前 Gate 用一个 `mtf:{asset}`、日线和东财都写 `mtf_d:{asset}` —— 东财一失败就把
    新浪日线的熔断也打开，两条路一起死。三条路各自独立计数。
    """
    return {"gate": f"mtf:{asset}", "sina_d": f"mtf_d:{asset}",
            "em": f"mtf_em:{asset}"}.get(kind, f"mtf_x:{asset}")


def em_klines_many(assets: list, retries: int = 3) -> Dict[str, List[list]]:
    """东财多品种：**每品种独立连接 + 递进退避**。

    实测出来的规律（2026-11-17 10:1x，同一台机器、同一 host）：
    · 每请求新建 session、间隔 0.3s：`101.GC00Y` 8/8 成功；
    · 同一个 keep-alive 连接**换 secid** 连续请求：0/10 全失败；
    · 短时间连发同一 secid 十几次：约一半被重置（像按秒限流）。
    所以：独立连接、品种之间 sleep、重试间隔递增。2 个品种 ≈ 0.6–1.5s。
    """
    out: Dict[str, List[list]] = {}
    for asset in assets:
        secid = _EM_SECIDS.get(asset)
        if not secid or not _source_ok(f"mtf_em:{asset}"):
            continue
        rows: List[list] = []
        for attempt in range(max(1, retries)):
            try:
                rows = em_klines(secid, 60, 200, retries=1)
                if len(rows) >= 2:
                    break
            except Exception:
                rows = []
            time.sleep(0.4 * (attempt + 1))   # 0.4 / 0.8 / 1.2
        if len(rows) >= 2:
            out[asset] = rows
        else:
            _SRC_DOWN_UNTIL[f"mtf_em:{asset}"] = time.time() + _FAIL_COOLDOWN_S
        time.sleep(0.3)
    return out


def fetch_mtf(assets: list, workers: int = 3) -> Dict[str, Dict[str, Any]]:
    """国内可访问 K 线 → mtf 周期（`_source` 标明来源，上层据此写 note）。

    加密：Gate 现货 1h×200 聚合成 1d/4h/1h（同一份序列，口径自洽）。
    商品：①1d 走新浪外盘**日线**（真实交易日，2,589/7,690 根）；
          ②4h/1h 走东财国际期货 1h（`101.GC00Y` / `102.CL00Y`，约 118 根≈5 天），
          本地聚合成 4h。东财约 50% 连接被重置但失败很快，靠 3 次短重试兜。
          ③东财取不到时，商品 4h/1h 仍是本地隔夜线（`local_kline`），不拿日线硬凑。
    """
    import concurrent.futures as _cf

    out: Dict[str, Dict[str, Any]] = {}
    jobs = [(a, _GATE_PAIRS[a]) for a in assets
            if a in _GATE_PAIRS and _source_ok(f"mtf:{a}")]
    daily_jobs = [(a, _SINA_DAILY_SYMS[a]) for a in assets
                  if a in _SINA_DAILY_SYMS and _source_ok(f"mtf_d:{a}")]
    em_assets = [a for a in assets
                 if a in _EM_SECIDS and _source_ok(f"mtf_em:{a}")]

    # 总预算：引擎给国内段只有 4s，这里必须自己封顶。`with` 会在退出时 wait=True
    # 等慢线程，等于预算无效（实测独立跑能到 11s，到引擎必然被整段丢弃）。
    _t_start = time.monotonic()
    ex = _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dom-mtf")
    try:
        futs = {ex.submit(gate_klines, pair, "1h", _GATE_MTF_BARS): (a, "gate")
                for a, pair in jobs}
        futs.update({ex.submit(sina_daily_klines, sym): (a, "sina_d")
                     for a, sym in daily_jobs})
        em_fut = ex.submit(em_klines_many, em_assets) if em_assets else None
        _em_rows: Dict[str, List[list]] = {}
        # 一次性总预算：gate/新浪已拿到手的先收，慢的不等。
        done, _pend = _cf.wait(list(futs), timeout=_MTF_BUDGET_S)
        for fut in done:
            asset, kind = futs[fut]
            try:
                rows = fut.result()
            except Exception:
                _SRC_DOWN_UNTIL[_cooldown_key(kind, asset)] = time.time() + _FAIL_COOLDOWN_S
                continue
            if len(rows) < 2:
                continue
            rows = sorted(rows, key=lambda r: int(r[0]))
            per = out.setdefault(asset, {})
            if kind == "sina_d":
                per["1d"] = rows[-8:]
                per["_source_1d"] = "sina_daily_k"
                continue
            per["1h"] = rows[-48:]
            for interval, (sec, limit) in _GATE_PERIODS.items():
                if interval == "1h":
                    continue
                agg = _agg(rows, sec, limit)
                if len(agg) >= 2:
                    per[interval] = agg
            per["_source_1d"] = per.get("_source_1d") or "gate_klines"
            per["_source_intraday"] = "gate_klines"
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            ex.shutdown(wait=False)
        except Exception:
            pass

    # 东财 1h：给 1h，聚合成 4h（不产 1d —— 只有 5 天，凑 8 根日线是编数据）
    if em_fut is not None:
        _left = max(0.1, _MTF_BUDGET_S - (time.monotonic() - _t_start))
        try:
            _em_rows = em_fut.result(timeout=min(EM_WAIT_S, _left))
        except Exception:
            _em_rows = {}
    for asset, rows in (_em_rows or {}).items():
        per = out.setdefault(asset, {})
        rows = sorted(rows, key=lambda r: int(r[0]))
        per["1h"] = rows[-48:]
        agg = _agg(rows, 14400, 42)
        if len(agg) >= 2:
            per["4h"] = agg
        per["_source_intraday"] = "em_klines"
    # 只给"确实拿到了数据"的周期标来源。商品若东财失败，per 里没有 4h/1h，
    # 就不该预设一个 `_source_intraday` —— 否则上层会把"没补上"读成"已用东财/ Gate"。
    for per in out.values():
        if "1d" in per:
            per.setdefault("_source_1d", "gate_klines")
        if "1h" in per or "4h" in per:
            per.setdefault("_source_intraday", "gate_klines")
    return out


def _coinpaprika(asset: str) -> Optional[Dict[str, Any]]:
    import json as _json

    # 兜底源用短超时：它是最后一层，慢一点整组就被引擎丢掉了
    text = _http_get(_CP_URL.format(cid=_CP_IDS[asset]), retries=1, timeout=1.0)
    d = _json.loads(text)
    q = (d.get("quotes") or {}).get("USD") or {}
    price = q.get("price")
    if not price or float(price) <= 0:
        return None
    return {"price": float(price),
            "change_pct": q.get("percent_change_24h")}


def _source_ok(name: str) -> bool:
    return time.time() >= _SRC_DOWN_UNTIL.get(name, 0.0)


def _source_fail(name: str, why: str = "") -> None:
    _SRC_DOWN_UNTIL[name] = time.time() + _FAIL_COOLDOWN_S
    if why:
        print(f"  [DOMESTIC] {name} 熔断 {int(_FAIL_COOLDOWN_S)}s：{why}", flush=True)


def _sina_group(assets: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    codes = ",".join(_SINA_CODES[a] for a in assets)
    text = _http_get(_SINA_URL.format(codes=codes),
                     {"Referer": "https://finance.sina.com.cn"})
    got: Dict[str, Dict[str, Any]] = {}
    for line in text.splitlines():
        if "hq_str" not in line:
            continue
        code = line.split("hq_str_")[1].split("=")[0]
        got[code] = _parse_sina_hf(line) or {}
    now = int(time.time())
    for a in assets:
        d = got.get(_SINA_CODES[a]) or {}
        if d.get("price"):
            src = f"sina_{_SINA_CODES[a]}"
            out[a] = {"price": d["price"], "change_pct": d.get("change_pct"),
                      "source": src, "note": _note_for(a, src), "ts": now, "kind": "sina_hf"}
    return out


def _tencent_group(assets: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    assets = [a for a in assets if _TENCENT_CODES[a] not in _FORBIDDEN]
    if not assets:
        return out
    codes = ",".join(_TENCENT_CODES[a] for a in assets)
    text = _http_get(_TENCENT_URL.format(codes=codes))
    got: Dict[str, Dict[str, Any]] = {}
    for line in text.splitlines():
        if not line.startswith("v_") or "none_match" in line:
            continue
        code = line[2:].split("=")[0]
        # hf_ 是外盘期货（逗号分隔，与新浪同构）；us*/kr* 是股票（波浪号分隔）
        got[code] = (_parse_sina_hf(line) if code.startswith("hf_") else _parse_tencent(line)) or {}
    now = int(time.time())
    for a in assets:
        d = got.get(_TENCENT_CODES[a]) or {}
        if d.get("price"):
            src = f"tencent_{_TENCENT_CODES[a]}"
            out[a] = {"price": d["price"], "change_pct": d.get("change_pct"),
                      "source": src, "note": _note_for(a, src), "ts": now, "kind": "tencent"}
    return out


def fetch_all(assets: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """抓齐所有资产报价（多源**并行**，单源失败不拖累其它）。

    返回 {asset: {price, change_pct, source, note, ts}}；取不到的资产不会出现。
    优先级：Gate.io(加密现货) > 新浪外盘(hf_*) > 腾讯(美股/韩股)。
    每个源失败后熔断 `_FAIL_COOLDOWN_S` 秒，避免"每批都去撞同一个死源"。
    """
    now = time.time()
    want = list(assets or (list(_GATE_PAIRS) + list(_SINA_CODES) + list(_TENCENT_CODES)))
    key = "dom:" + ",".join(sorted(want))
    cached = _CACHE.get(key)
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import concurrent.futures as _cf2

    out: Dict[str, Dict[str, Any]] = {}
    jobs = []
    gate_assets = [a for a in want if a in _GATE_PAIRS and _source_ok(f"gate:{a}")]
    sina_assets = [a for a in want if a in _SINA_CODES] if _source_ok("sina") else []
    tx_assets = [a for a in want if a in _TENCENT_CODES] if _source_ok("tencent") else []

    # ⚠️ 不能用 `with`：退出时默认 wait=True，会把还在跑的新浪线程（3s×重试≈6s）
    # 等完，预算等于白设。慢线程是"尽力而为"，不等。
    _pool = ThreadPoolExecutor(max_workers=max(2, len(gate_assets) + 2))
    try:
        pool = _pool
        if sina_assets:
            jobs.append(("sina", pool.submit(_sina_group, sina_assets)))
        if tx_assets:
            jobs.append(("tencent", pool.submit(_tencent_group, tx_assets)))
        for a in gate_assets:
            jobs.append((f"gate:{a}", pool.submit(_gate_ticker, _GATE_PAIRS[a])))

        # 一次性总预算，不是每个源各等 4s：新浪现在单请求就要 3s、重试 2 次 ≈6s，
        # 逐源等待会把 gate/腾讯**已经拿到**的结果压到超预算被整体丢弃（引擎侧
        # 多次"国内源补齐 0"正是这个形态，独立跑反而能取到）。
        done, _pending = _cf2.wait([f for _, f in jobs], timeout=_QUOTE_BUDGET_S)
        for name, fut in jobs:
            if fut not in done:
                _source_fail(name, "预算内未返回")
                continue
            try:
                res = fut.result(timeout=0)
            except Exception as e:
                # 熔断按"源+品种"记：ETH 超时不该把 BTC/SOL 一起停掉
                _source_fail(name, f"{type(e).__name__}: {str(e)[:60]}")
                continue
            if name == "sina" or name == "tencent":
                out.update(res or {})
            else:
                a = name.split(":")[1]
                if res and res.get("price"):
                    src = f"gate_{_GATE_PAIRS[a]}"
                    out[a] = {"price": res["price"], "change_pct": res.get("change_pct"),
                              "source": src, "note": _note_for(a, src),
                              "ts": int(time.time()), "kind": "exchange"}

    finally:
        try:
            _pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            _pool.shutdown(wait=False)
        except Exception:
            pass

    # 4) CoinPaprika 兜底（仅还缺的加密品种；Gate 单品种超时时它常能顶上）
    cp_req = [a for a in want if a in _CP_IDS and a not in out and _source_ok(f"cp:{a}")]
    _cp_deadline = time.monotonic() + _CP_BUDGET_S
    for a in cp_req:
        if time.monotonic() > _cp_deadline:
            break
        try:
            res = _coinpaprika(a)
        except Exception as e:
            _source_fail(f"cp:{a}", f"{type(e).__name__}: {str(e)[:50]}")
            continue
        if res and res.get("price"):
            src = f"coinpaprika_{_CP_IDS[a]}"
            out[a] = {"price": res["price"], "change_pct": res.get("change_pct"),
                      "source": src, "note": "CoinPaprika 聚合美元价（非单一交易所）",
                      "ts": int(time.time()), "kind": "aggregator"}

    if out:
        _CACHE[key] = {"ts": time.time(), "data": out}
    return out


if __name__ == "__main__":  # 手动自检：python3 feeds/domestic_quotes.py
    res = fetch_all()
    for a in sorted(res):
        d = res[a]
        print(f"{a:8s} {d['price']:>14,.2f}  24h {(d['change_pct'] or 0):+.2f}%  "
              f"{d['source']:<18s} {d['note']}")
    missing = [a for a in list(_GATE_PAIRS) + list(_SINA_CODES) + list(_TENCENT_CODES)
               if a not in res]
    print("缺:", missing or "无")
    k = gate_klines("BTC_USDT", "1h", 5)
    print("BTC 1h K线样本:", k[-3:] if k else "无")
