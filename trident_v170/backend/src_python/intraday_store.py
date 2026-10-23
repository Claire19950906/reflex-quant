"""
intraday_store.py — 分钟级 K 线存储 + 10m 聚合 + 历史累积
====================================================================
背景(实测):
  - Binance **没有 10m 周期**。api/v3/klines 的 interval 只接受
    1m/3m/5m/15m/30m/1h/..., 传 10m 报 {"code":-1120,"msg":"Invalid interval."}
  - api.binance.com 在国内网络不可达 (curl HTTP=000), 必须用 api.binance.us
    (可用, 公开 klines 端点免 API key)。
  - 本地库原本**只有 ohlcv_daily 日线**, 所以前端选 10m 时拿到的其实是
    日线 —— 这就是界面上"10m 却像日线"的根因。

方案: 拉 1m 原始 K 线落 ohlcv_intraday(interval='1m'), 前端要 10m 时
由 _aggregate() 在读取侧把 10 根 1m 合并成 1 根 10m, 不额外占空间。
拉不到 1m 时回落 5m(2 根合成 10m)。

设计要点:
  - (asset, interval, ts) 唯一, 重复拉取用 INSERT OR IGNORE 幂等。
  - 聚合按**左闭右开**固定边界切桶(UTC), 同一根 K 线无论何时重算
    结果一致(幂等), 保证历史累积不漂移。
  - 丢弃残缺桶与未收盘的最后一桶, 避免把没走完的 10m 当完整信号。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

BINANCE_HOSTS = (
    # v308: data-api.binance.vision 排在最前 —— 实测它是唯一在当前网络下
    # 稳定可直连的(裸 data 接口, 不走 www/api 的风控网关);
    # api.binance.us / api.binance.com 经常 code=000 超时。
    # 顺序不等于优先级保证: _http_klines 还有健康记忆, 失败会自动跳过。
    "https://data-api.binance.vision",
    "https://api.binance.us",
    "https://api.binance.com",
)

# host 健康状态: host -> 冷却截止时间戳 / 是否曾经成功
_binance_host_bad_until: dict[str, float] = {}
_binance_host_ok: dict[str, bool] = {}

# ───────────────────────── 加密备用交易所 ─────────────────────────
# v311: 加备用交易所的实测依据(每源重试 3 次, 取有数据的一次为准):
#   binance   ✅ 3 根/100  3.1s   (主源, 保持第一)
#   coinbase  ✅ 350 根     1.1s   (最稳, 一次最多 300 根/次请求)
#   okx       ✅ 100 根     1.0s   (对亚太 IP 友好)
#   kraken    ❌ 0/3 SSL EOF
# 为什么必须有第二腿: Binance 常被地域风控(code=000/超时), 而加密是本项目
# 一级资产 —— 主源一挂, attribution/异常页直接空窗, 没有兜底就没有降级。
#
# 各源差异(实测, 别照抄 Binance 的用法):
#   - coinbase 返回**倒序**(最新在前), 且单次上限 300 根, granularity 用秒;
#     interval 只支持 60/300/900/3600/21600/86400, 没有 3m/2m/10m
#     → 统一用 1m 取, 再由 _aggregate 合成前端周期。
#   - okx 返回倒序, bar=1m/5m 等, limit 最大 300。
#   - 两者都不给 trades 计数 → 填 0(与现有列默认值一致)。
CRYPTO_FALLBACKS = ("binance", "coinbase", "okx")

# asset -> 最近一次**真正取到数据**的加密源。
# 为什么不直接写死 "binance": _fetch_crypto 会在 Binance 失败时自动改用
# Coinbase/OKX, 若落库仍标 binance, 排查时会看到"binance 有洞"而实际
# 洞在别处 —— 数据来源必须如实反映, 否则等于自己给自己埋误导。
_last_crypto_source: dict[str, str] = {}

# Binance 原生周期 (10m 不在其中)
NATIVE_INTERVALS = {
    "1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400,
    # v308: yfinance 会以 "60m"/"90m" 形式返回, 落库时需要秒数换算
    "60m": 3600, "90m": 5400,
}

# 前端周期 → (目标秒, 候选源周期)
# 统一以 1m 为首选源: 60 能整除 3/5/10/15/30, 故一套 1m 数据即可
# 支撑前端所有分钟级切换, 无需为每个周期单独回补。
# 回落链只在 1m 完全缺失时启用(部分交易所/币种不提供 1m)。
FRONTEND_INTERVALS = {
    "1m": (60, ("1m",)),
    "3m": (180, ("1m", "5m")),           # yfinance 无 3m → 3m 由 1m 聚合
    "5m": (300, ("1m", "5m")),
    "10m": (600, ("1m", "5m")),
    "15m": (900, ("1m", "15m")),
    "30m": (1800, ("1m", "30m")),
    "1h": (3600, ("1m", "60m", "1h")),   # yfinance 以 "60m" 落库
}

ASSET_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "DOGE": "DOGEUSDT", "ADA": "ADAUSDT",
    "BNB": "BNBUSDT", "LINK": "LINKUSDT", "AVAX": "AVAXUSDT",
}

# ─────────────────── v310: Polygon 接入(美股分钟主源) ───────────────────
# 背景: Yahoo 对本机持续 403(实测 intraday_store 直连与 yfinance 双双失败:
#   "Yahoo 拉取 000660.KS/1m 失败: HTTP Error 403: Forbidden",
#   yfinance 直连 "YFRateLimitError: Too Many Requests")。
#   后果不是"暂时少数据", 而是 ohlcv_intraday 美股覆盖率停在 ~20% 且
#   出现 66 小时空洞(SKHYNIX 10m bar 间隔 max=238200s vs 正常 600s),
#   自适应阈值随之失真(rsi_ob 从 ~70 掉到 60.67), 异常识别结果随之改变。
#
# 已配 key 的实测结果(backend/.env, 免费版):
#   - POLYGON_API_KEY     **可用** —— MU/1min/3d 返回 1876 根真实 OHLCV
#   - ALPHAVANTAGE_API_KEY 不可用 —— TIME_SERIES_INTRADAY 是付费端点
#   - FINNHUB_API_KEY      不可用 —— HTTP 403
#   - FMP_API_KEY          不可用 —— HTTP 403
# 故只有 Polygon 这条替代路径。
#
# Polygon 端点约定:
#   GET /v2/aggs/ticker/{ticker}/range/{mult}/minute/{from}/{to}
#   - 时间戳 t 是 **epoch 毫秒**(与 Yahoo 的秒不同, 故必须 /1000)
#   - 免费版数据有延迟, status 返回 "DELAYED" 而非 "OK"; 这是正常状态,
#     不是错误 —— 只有 status=="ERROR" 才算失败。
POLYGON_API_BASE = "https://api.polygon.io"
# ── Polygon 免费版的关键行为(全部为实测结论, 改前务必先看这里) ──
# 1. **短窗口 1m 被拒(最容易踩的坑)**:
#    请求 5 小时窗口的 1m → 立即 {"status":"NOT_AUTHORIZED"} + HTTP 403
#    (1.0s 就返回, 不是超时); 而**同样 1m**、7 天窗口 → 正常返回 3913 根
#    (耗时 39.7s)。所以免费版并非"按跨度计费", 而是窄窗口查询直接没授权。
#    推论: **不能用 limit 反推请求起点** —— limit=300 会把窗口压到 5 小时,
#    然后必然 NOT_AUTHORIZED。这就是"直连裸 URL 能成、fetch_native 必 403"
#    的真正差异(此前被误判为限流)。
# 2. **响应很慢**: 分钟聚合实测 37~40 秒, 超时必须给到 75s, 否则会被判超时。
# 3. status=="DELAYED" 是免费版正常延迟, 不是错误, 不要当成失败。
# 4. **不要加 limit / sort 参数**: limit=50000 会让服务端真去排序截取,
#    响应从 37s 翻到 45s+ 直接超时; 免费版本来也返回不了 50000 根。
# 5. 同分钟连打 6+ 次仍会 403, 故保留 POLYGON_WINDOW_GAP 节流。
class PolygonRateLimited(RuntimeError):
    """Polygon 免费额度/授权被打光(403/429), 应冷却而非原地重试。"""


POLYGON_HTTP_TIMEOUT = 75
POLYGON_MAX_DAYS_1M = 7          # 免费版 1m 深度上限(实测 7d → 3913 根)
POLYGON_MIN_SPAN_1M = 2 * 86400  # 1m 最小请求跨度: 小于此值会 NOT_AUTHORIZED
POLYGON_WINDOW_GAP = 13          # 请求间隔节流
POLYGON_COOLDOWN_SEC = 30        # 403 后静默期
_POLYGON_COOLDOWN_UNTIL = 0.0    # 冷却截止(epoch 秒)
# 进程内最小请求间隔。read_bars 的多面板会并发读同一批资产, 若不串行化,
# 额度会在一次页面渲染里被瞬间打光, 然后所有源一起 403。
_POLYGON_LAST_CALL = 0.0
_POLYGON_GATE = threading.Lock()


def _polygon_throttle() -> None:
    """串行化 + 限速: 保证任意两次 Polygon 请求间隔 ≥ POLYGON_WINDOW_GAP。

    返回前已持有 _POLYGON_GATE 的"逻辑配额"—— 用锁保护时间戳更新即可,
    不需要把网络请求整个包在锁里(那会串行化所有资产, 拖慢页面)。
    """
    global _POLYGON_LAST_CALL
    now = time.time()
    wait = POLYGON_WINDOW_GAP - (now - _POLYGON_LAST_CALL)
    if wait > 0:
        time.sleep(wait)
    with _POLYGON_GATE:
        _POLYGON_LAST_CALL = time.time()

# 前端周期 → Polygon minute mult(Polygon 无 3m, 由 1m 聚合而来)
POLYGON_NATIVE_MULT = {
    "1m": 1, "2m": 2, "5m": 5, "10m": 10, "15m": 15,
    "30m": 30, "60m": 60, "1h": 60,
}


def _polygon_ticker(asset: str) -> str | None:
    """资产代码 → Polygon ticker; 无法映射返回 None。

    Polygon 覆盖美股主板, ticker 与 Yahoo 同形(MU / NVDA / TSLA ...),
    故直接沿用本地资产名。加密与商品在 Polygon 侧代码不同(需交易所前缀),
    这里不猜 —— 返回 None 交回原数据源, 避免把错误代码写进库里。
    """
    sym = asset.upper()
    if sym in US_ASSETS or sym in {"MU", "AMD", "SMCI", "ARM"}:
        return sym
    return None


def _polygon_api_key() -> str:
    """从环境或 backend/.env 读取 key(不打印 key 内容)。"""
    key = os.environ.get("POLYGON_API_KEY", "").strip()
    if key:
        return key
    try:
        env_path = BACKEND_DIR / ".env"
        for line in env_path.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
            s = line.strip()
            if s.startswith("POLYGON_API_KEY"):
                k, _, v = s.partition("=")
                if k.strip() == "POLYGON_API_KEY":
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _poly_mult(interval: str) -> int | None:
    """前端周期 → Polygon minute mult; 不支持返回 None。"""
    return POLYGON_NATIVE_MULT.get(interval)


# v308: 美股分钟数据源 = yfinance (Yahoo)。
# 背景: intraday_store 原本只认 Binance 的 9 个币种, 美股(MU/AAPL/NVDA/...)
# 全部取不到分钟数据 → attribution 页选 10m 永远空, 且 backfill 会用
# "page_failures:21 / ok:true" 假装成功(实测 MU 就是如此)。
# yfinance 已在项目里用于 ohlcv_daily, 无需新增依赖/API key。
#
# yfinance 分钟级限制(实测 yf 1.7.0):
#   - interval 只支持 1m/2m/5m/15m/30m/60m/90m/1h, **没有 3m**;
#   - 1m 单次请求最多 7~8 天(传 30d 会返回 "Only 8 days worth of
#     1m granularity data are allowed to be fetched per request");
#   - period="1d" 对 1m 返回空, 必须 >= "5d"。
# 详细换算见 _yf_period_for()。
YF_NATIVE_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}

# 单次 yfinance 请求超时(秒)。实测正常 1~2s, 但 SSL/crumb 失败时会重试,
# 不设上限可能把一个读请求拖到几十秒。yfinance 的 history() 支持 timeout。
YF_HTTP_TIMEOUT = 10

# 美股候选清单(与 bridge_server.NEWS_ASSETS 的美股部分保持一致)
US_ASSETS = {
    "AAPL", "MSFT", "NVDA", "TSLA", "MU", "AMD", "GOOGL", "META", "AMZN",
    "NFLX", "BABA", "JD", "PDD", "NIO", "XPEV", "LI",
}

# 直连 Yahoo chart 接口时用的代码映射(见 _fetch_yahoo_chart)。
# 为什么需要它: yfinance 1.7 在当前网络下 cookie/crumb 握手稳定失败
#   (SSLError: UNEXPECTED_EOF / "Cookie/crumb fetch failed"), 且随后
#   立刻 YFRateLimitError —— 于是美股回补 100% 失败, 但 _backfill_yfinance
#   只把失败抛给上层, 库仍是 0 行, 前端报 "ohlcv_intraday 无 X 数据"。
#   实测 /v8/finance/chart/ 直连(不取 crumb)完全正常, 故这里自带映射。
# 非美股资产走 Yahoo 时需要 -USD 后缀, 商品走连续期货合约。
YAHOO_TICKERS = {
    # 韩股(海力士在韩交所, 代码含 .KS 后缀; .KQ 交易所只有零星成交)
    "SKHYNIX": "000660.KS",
    # 加密(与 ASSET_SYMBOLS 的 Binance 标的保持一致)
    "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
    "XRP": "XRP-USD", "DOGE": "DOGE-USD", "ADA": "ADA-USD",
    "BNB": "BNB-USD", "LINK": "LINK-USD", "AVAX": "AVAX-USD",
    # 商品(Yahoo 只有期货连续合约; XAGUSD=X 已 404)
    "XAU": "GC=F", "XAG": "SI=F", "WTI": "CL=F",
}
# 美股代码与本地名同形, 缺失时回落到"资产名本身"。
def yahoo_ticker(asset: str) -> str | None:
    """资产代码 → Yahoo ticker; 无法映射返回 None(交回原数据源)。"""
    sym = asset.upper()
    if sym in YAHOO_TICKERS:
        return YAHOO_TICKERS[sym]
    if sym in US_ASSETS or sym in {"MU", "AMD", "NVDA", "TSLA", "SMCI", "ARM"}:
        return sym
    return None

_fetch_lock = threading.Lock()

DDL = """
CREATE TABLE IF NOT EXISTS ohlcv_intraday (
    asset     TEXT    NOT NULL,
    interval  TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    volume    REAL    NOT NULL,
    trades    INTEGER NOT NULL DEFAULT 0,
    closed    INTEGER NOT NULL DEFAULT 1,
    source    TEXT,
    PRIMARY KEY (asset, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_intraday_lookup
    ON ohlcv_intraday (asset, interval, ts DESC);

CREATE TABLE IF NOT EXISTS anomaly_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset       TEXT    NOT NULL,
    interval    TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    close       REAL,
    pattern     TEXT,
    evidence    TEXT,
    rsi         REAL,
    macd_hist   REAL,
    vol_ratio   REAL,
    score       REAL,
    uses_future INTEGER DEFAULT 0,
    scanned_at  INTEGER NOT NULL,
    UNIQUE (asset, interval, ts, kind)
);
CREATE INDEX IF NOT EXISTS idx_anomaly_hist
    ON anomaly_history (asset, interval, ts DESC);
"""


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema() -> None:
    conn = db_conn()
    try:
        conn.executescript(DDL)
        conn.commit()
    finally:
        conn.close()


# ────────────────────────── 采集 ──────────────────────────
def _http_klines(symbol: str, interval: str, limit: int,
                 start_ms: int | None = None, timeout: float = 15.0,
                 retries: int = 4) -> list[list]:
    """拉 klines: 多个 host × 多次重试 + 指数退避。

    实测: Binance 对连续分页请求会偶发 SSL UNEXPECTED_EOF(连接被掐)，
    重试 + 退避即可稳定通过；因此这里必须加重试，否则回补会随机中断。
    """
    last_err: Exception | None = None
    # v308: host 健康记忆 —— 实测 api.binance.us / api.binance.com 在某些
    # 网络下会整体不可达(curl code=000, ~5s 超时), 而 data-api.binance.vision
    # 一直健康。旧实现按固定顺序对每个 host 重试 4 次并指数退避
    # (1+2+4+8=15s), 两个死 host × 15s = 30s 才轮到可用 host,
    # 直接把 /market/history 拖到前端超时 → attribution 页面板消失。
    # 现在记录失败 host 的冷却时间, 冷却期内直接跳过, 只打健康 host。
    for host in BINANCE_HOSTS:
        if time.time() < _binance_host_bad_until.get(host, 0):
            continue
        url = f"{host}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        if start_ms is not None:
            url += f"&startTime={start_ms}"
        for attempt in range(retries):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0 trident-intraday/1.0",
                                  "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict):
                    raise RuntimeError(f"{host} -> {data}")
                _binance_host_bad_until[host] = 0        # 成功 → 解除惩罚
                _binance_host_ok[host] = True
                if attempt:
                    time.sleep(0.4 * attempt)     # 成功后短暂停顿, 避免连续打限流
                return data
            except Exception as exc:                      # noqa: BLE001
                last_err = exc
                if attempt < retries - 1:
                    # 退避随 host 是否曾经成功缩放: 首次就失败(疑似被墙)
                    # 不做长退避, 快速换下一个 host。
                    if _binance_host_ok.get(host):
                        time.sleep(min(2 ** attempt, 4))       # 1s/2s 退避
                    else:
                        time.sleep(0.3)
        # 该 host 本轮全部失败 → 冷却 5 分钟
        _binance_host_ok[host] = False
        _binance_host_bad_until[host] = time.time() + 300
    raise RuntimeError(f"所有 Binance host 均失败或处于冷却: {last_err}")


def fetch_native(asset: str, interval: str, limit: int = 1000,
                 start_ms: int | None = None, days: int | None = None) -> list[dict]:
    """拉 K 线 → dict 列表。按资产自动分流数据源。

    v308: 币种走 Binance, 美股走 yfinance(见 _fetch_yfinance)。
    days 仅 yfinance 路径使用(Binance 用 start_ms 游标分页)。

    v309: 非币种资产走 **Yahoo chart 直连**; yfinance 仅在没有代码映射时兜底。

    这里**故意不再回退 yfinance**: 实测它在当前网络下 cookie/crumb 握手
    必失败(SSLError)且立刻被限流, 回退等于每次多白等 20~27s 再抛
    curl 错误 —— 而 read_bars 里 5 个 panel 并发读同一资产时, 这点延迟
    足以把整页拖到超时。失败让异常直接上抛, read_bars 已用 try/except
    包住 incremental, 库内已有数据照常返回。
    v310: 美股优先走 **Polygon**, Yahoo chart 降为兜底。
    实测 Yahoo 对本机持续 403(直连与 yfinance 双双失败), 而 .env 里已配的
    POLYGON_API_KEY 可正常返回美股分钟线(MU/1m/3d = 1876 根)。Polygon 拿不到
    时才回落到 Yahoo —— 因为 403 可能是临时的(限流/封禁会解), 有第二条腿
    总比没有强; 但每个源最多只试一次, 不叠加等待时间。
    v311: 加密补齐多腿 Binance → Coinbase → OKX。
    实测(每源重试 3 次): Binance 100 根/3.1s, Coinbase 350 根/1.1s,
    OKX 100 根/1.0s —— 三者互为备份。Binance 常被地域风控, 一旦整条主源挂掉,
    加密的 attribution/异常页就会空窗, 所以这里必须有第二、三腿。
    **不静默降级**: 三个源全失败时才抛错, 并把各源的错误一起带上,
    否则排查时只会看到最后一个源的报错, 误以为只有一个源有问题。
    """
    asset = asset.upper()
    if asset in ASSET_SYMBOLS:
        return _fetch_crypto(asset, interval, limit, start_ms)
    if _polygon_ticker(asset) and _poly_mult(_yf_normalize_interval(interval)):
        try:
            return _fetch_polygon(asset, interval, limit, start_ms, days=days)
        except Exception as exc:                          # noqa: BLE001
            print(f"[intraday] Polygon 不可用({asset}/{interval}: {exc}), "
                  f"回落 Yahoo")
    if yahoo_ticker(asset):
        return _fetch_yahoo_chart(asset, interval, limit, start_ms, days=days)
    return _fetch_yfinance(asset, interval, limit, start_ms, days=days)


def _fetch_crypto(asset: str, interval: str, limit: int,
                  start_ms: int | None) -> list[dict]:
    """加密货币: Binance → Coinbase → OKX 逐级兜底。

    每腿只在**真的没拿到数据**时才继续下一腿; 一旦某腿返回非空就立刻返回,
    避免为已经拿到的数据再付一次网络延迟(1m 走 Coinbase 约 1s)。
    """
    fetches = (
        ("binance", lambda: _fetch_binance(asset, interval, limit, start_ms)),
        ("coinbase", lambda: _fetch_coinbase(asset, interval, limit, start_ms)),
        ("okx", lambda: _fetch_okx(asset, interval, limit, start_ms)),
    )
    errors = []
    for name, fn in fetches:
        try:
            bars = fn()
        except Exception as exc:                          # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__} {exc}")
            print(f"[intraday] {name} 取 {asset}/{interval} 失败: {exc}")
            continue
        if bars:
            _last_crypto_source[asset] = name
            return bars
        errors.append(f"{name}: 返回空")
    raise RuntimeError(
        f"{asset}/{interval} 所有加密源均无数据 → " + " | ".join(errors))


def _fetch_binance(asset: str, interval: str, limit: int,
                   start_ms: int | None) -> list[dict]:
    """Binance 原生周期 K 线(仅加密货币)。"""
    symbol = ASSET_SYMBOLS[asset.upper()]
    out = []
    for k in _http_klines(symbol, interval, limit, start_ms):
        out.append({
            "ts": int(k[0]) // 1000,                    # ms → s
            "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]),
            "volume": float(k[5]),
            "trades": int(k[8]) if len(k) > 8 else 0,
        })
    return out


# 备用交易所的周期映射: 只支持这几个原生 granularity, 其余统一退到 1m
_COINBASE_GRAN = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
_OKX_BAR = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1H", "4h": "4H", "1d": "1D"}


def _fetch_coinbase(asset: str, interval: str, limit: int,
                    start_ms: int | None) -> list[dict]:
    """Coinbase Exchange K 线(Binance 失效时的第二腿)。

    实测要点:
      - 端点 /products/{BASE}-USD/candles?granularity={秒}
      - 返回**倒序**(最新在数组最前), 必须 reverse 后再交给上层,
        否则 backfill 会按 ts 倒着插, 增量判断(last_ts)会直接失效。
      - 单次上限 300 根; 不支持 start/end 之外的游标, 故 start_ms 只用于截断。
      - 无 trades 字段 → 填 0。
    """
    gran = _COINBASE_GRAN.get(interval, 60)
    product = f"{asset.upper()}-USD"
    lim = min(max(limit if limit else 300, 1), 300)
    url = (f"https://api.exchange.coinbase.com/products/{product}/candles"
           f"?granularity={gran}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 trident-intraday/1.0",
                      "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        rows = json.loads(resp.read().decode("utf-8"))
    if isinstance(rows, dict):                       # 错误体而非数组
        raise RuntimeError(f"coinbase -> {rows}")

    out = []
    for r in rows:
        # 格式: [time(秒), low, high, open, close, volume]
        ts = int(r[0])
        if start_ms is not None and ts * 1000 < start_ms:
            continue
        out.append({"ts": ts, "open": float(r[3]), "high": float(r[2]),
                    "low": float(r[1]), "close": float(r[4]),
                    "volume": float(r[5]), "trades": 0})
    out.sort(key=lambda x: x["ts"])                 # 倒序 → 正序
    return out


def _fetch_okx(asset: str, interval: str, limit: int,
               start_ms: int | None) -> list[dict]:
    """OKX K 线(第三腿, 对亚太 IP 最友好, 实测 1.0s/100 根)。

    端点 /api/v5/market/candles?instId={BASE}-USDT&bar={bar}&limit={n}
    data 同样**倒序**; code != "0" 即失败(OKX 永远 HTTP 200, 只看 body)。
    """
    bar = _OKX_BAR.get(interval, "1m")
    lim = min(max(limit if limit else 100, 1), 300)
    url = (f"https://www.okx.com/api/v5/market/candles"
           f"?instId={asset.upper()}-USDT&bar={bar}&limit={lim}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 trident-intraday/1.0",
                      "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("code") != "0":
        raise RuntimeError(f"okx {payload.get('code')}: {payload.get('msg')}")

    out = []
    for r in payload.get("data") or []:
        # 格式: [ts(ms), o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        ts = int(r[0]) // 1000
        if start_ms is not None and ts * 1000 < start_ms:
            continue
        out.append({"ts": ts, "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4]),
                    "volume": float(r[5]), "trades": 0})
    out.sort(key=lambda x: x["ts"])
    return out


def _yf_normalize_interval(interval: str) -> str:
    """把前端/调用方周期映射到 yfinance 支持的原生周期。

    yfinance 无 3m; 1h 要写成 60h(60m 合法, 1h 也接受, 统一用 60m 更稳)。
    """
    iv = interval
    if iv == "3m":
        iv = "5m"                      # yfinance 无 3m, 回落 5m
    elif iv == "1h":
        iv = "60m"
    if iv not in YF_NATIVE_INTERVALS:
        # 2h/4h/6h/8h/12h 等由 1m 聚合而来, yfinance 侧直接用 1m 拉
        iv = "1m"
    return iv


def _yf_period_for(days: int | None, interval: str) -> str:
    """把 days 映射到 yfinance 的 period 字符串。

    yfinance 分钟级硬限制(实测 yf 1.7.0, Yahoo 侧返回的原文):
      - 1m 每次请求**最多 7~8 天**, 传更长会返回
        "1m data not available for startTime=... Only 8 days worth of
         1m granularity data are allowed to be fetched per request."
      - period="1d" 对 1m 返回空(且会打 "possibly delisted" 噪音日志),
        最小可用是 "5d"。
      - 5m/15m/30m 可到 60d, 60m 可到 730d。

    故按 interval 取各自上限, 而不是所有周期共用 30d。
    """
    if interval == "1m":
        cap = 7          # 留 1 天余量, 避开 Yahoo 的 8 天硬边界
    elif interval in ("2m", "5m", "15m", "30m"):
        cap = 30
    else:               # 60m/90m
        cap = 730
    d = cap if days is None else max(1, min(int(days), cap))

    if interval == "1m":
        return f"{d}d" if d <= 7 else "1mo"
    if d <= 5:
        return "5d"                     # 实测 period="1d" 对分钟级返回空
    if d <= 14:
        return f"{d}d"
    if d <= 31:
        return "1mo"
    if d <= 60:
        return "2mo"
    if d <= 90:
        return "3mo"
    if d <= 180:
        return "6mo"
    if d <= 365:
        return "1y"
    return "2y"


# Yahoo chart 端点实测: query1 / query2 都可用, 单个 host 偶发
# SSL UNEXPECTED_EOF —— 故按顺序轮换, 不是"主备降级"而是平级重试。
YAHOO_CHART_HOSTS = (
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
)
# 超时必须收紧: read_bars 在库内数据过期时会同步调 incremental(), 而
# attribution 页有多个 panel 并发读不同资产。timeout=15/retries=3 时单个
# 失败资产最坏要 49s, 整页必然超时。2 次重试正好覆盖 query1+query2 各一次,
# 再多只是线性叠加延迟、不会提高成功率。
YAHOO_HTTP_TIMEOUT = 8
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _yf_days_cap(interval: str) -> int:
    """Yahoo 对各粒度的最大回溯天数(服务端硬限制, 超过会返回错误 JSON)。"""
    if interval == "1m":
        return 7                       # 服务端上限 8 天, 留 1 天余量
    if interval in ("2m", "5m", "15m", "30m"):
        return 60
    return 730                         # 60m / 90m


def _fetch_yahoo_chart(asset: str, interval: str, limit: int,
                       start_ms: int | None, retries: int = 2,
                       days: int | None = None) -> list[dict]:
    """直连 Yahoo /v8/finance/chart 拉分钟 K 线 → dict 列表(ts=UTC epoch 秒)。

    为什么绕过 yfinance: yfinance 必须先取 cookie/crumb, 当前网络下这一步
    稳定失败(SSLError), 且失败后立刻被限流, 导致美股回补恒为 0 行。
    chart 端点本身不需要 crumb, 直接 GET 即可。

    返回契约与 _fetch_yfinance 完全一致, 所以调用方(read_bars/backfill)
    无需区分数据来源。
    """
    ticker = yahoo_ticker(asset)
    if not ticker:
        raise RuntimeError(f"无 Yahoo 代码映射: {asset}")

    iv = _yf_normalize_interval(interval)
    # Yahoo 用 60m 而非 1h; _yf_normalize_interval 已处理, 这里只兜底。
    span = _yf_days_cap(iv)
    d = span if days is None else max(1, min(int(days), span))
    period2 = int(time.time())
    period1 = period2 - d * 86400
    if start_ms is not None:
        period1 = max(period1, start_ms // 1000)

    last_err: Exception | None = None
    for attempt in range(retries):
        host = YAHOO_CHART_HOSTS[attempt % len(YAHOO_CHART_HOSTS)]
        url = (f"{host}/v8/finance/chart/{urllib.parse.quote(ticker)}"
               f"?period1={period1}&period2={period2}&interval={iv}")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=YAHOO_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read())

            chart = payload.get("chart") or {}
            if chart.get("error"):
                raise RuntimeError(f"Yahoo 返回错误: {chart['error']}")
            results = chart.get("result") or []
            if not results:
                raise RuntimeError(f"Yahoo 无数据 ({ticker}/{iv}) —— 代码可能不存在")

            node = results[0]
            stamps = node.get("timestamp") or []
            quote = ((node.get("indicators") or {}).get("quote") or [{}])[0]

            def col(key):
                seq = quote.get(key) or []
                return seq if len(seq) == len(stamps) else [None] * len(stamps)

            o_seq, h_seq = col("open"), col("high")
            l_seq, c_seq = col("low"), col("close")
            v_seq = col("volume")

            out: list[dict] = []
            for i, ts in enumerate(stamps):
                o, h = o_seq[i], h_seq[i]
                l, c = l_seq[i], c_seq[i]
                if o is None or h is None or l is None or c is None:
                    continue                                   # Yahoo 会插全 null 行
                vol = v_seq[i] if i < len(v_seq) else None
                out.append({
                    "ts": int(ts), "open": float(o), "high": float(h),
                    "low": float(l), "close": float(c),
                    "volume": float(vol) if vol is not None else 0.0,
                    "trades": 0,
                })
            if not out:
                raise RuntimeError(f"Yahoo 解析后为空 ({ticker}/{iv})")
            if limit:
                out = out[-limit:]
            return out

        except Exception as exc:                          # noqa: BLE001
            last_err = exc
            if attempt < retries - 1:
                time.sleep(1.2 * (attempt + 1))            # 换 host + 退避
    raise RuntimeError(f"Yahoo 拉取 {ticker}/{iv} 失败: {last_err}")


def _fetch_polygon(asset: str, interval: str, limit: int,
                   start_ms: int | None, retries: int = 2,
                   days: int | None = None) -> list[dict]:
    """Polygon 聚合端点拉分钟 K 线 → dict 列表(ts=UTC epoch 秒)。

    返回契约与 _fetch_yahoo_chart / _fetch_yfinance **完全一致**,
    所以 read_bars / backfill / incremental 无需区分数据来源。

    关键实现点:
      - **单次请求优先**: 免费版 5 次/分钟, 实测同一分钟内连打 6 次就403。
        既然 limit=50000 足够装下 5 天 1m(~2045 根), 就没必要按天分窗
        自伤配额; 只有单次真的空返回才退化成按天重试。
      - t 是毫秒必须 // 1000 —— 与 Yahoo 的秒不同, 漏换算会溢出到
        公元五万多年。
      - status=="DELAYED" 是免费版正常延迟, 只有 "ERROR" 才算失败。
      - 命中 403 立即进入 65s 冷却并上抛, 让 fetch_native 回落 Yahoo,
        不在同一分钟里反复捶一个已经限流的源。
    """
    ticker = _polygon_ticker(asset)
    if not ticker:
        raise RuntimeError(f"无 Polygon 代码映射: {asset}")
    mult = _poly_mult(interval)
    if not mult:
        raise RuntimeError(f"Polygon 不支持周期 {interval}")

    key = _polygon_api_key()
    if not key:
        raise RuntimeError("POLYGON_API_KEY 未配置")

    global _POLYGON_COOLDOWN_UNTIL
    if time.time() < _POLYGON_COOLDOWN_UNTIL:
        raise RuntimeError(
            f"Polygon 限流冷却中(剩 {_POLYGON_COOLDOWN_UNTIL - time.time():.0f}s)")

    now = int(time.time())
    # 1m 的最小请求跨度: 免费版对窄窗口直接 NOT_AUTHORIZED(实测 5 小时
    # 窗口 → 403 NOT_AUTHORIZED, 7 天窗口 → 正常 3913 根)。所以**禁止**用
    # limit 反推起点 —— limit 只是"要几根", 不该影响"问多久"。
    min_span = POLYGON_MIN_SPAN_1M if mult == 1 else 86400
    if start_ms is not None:
        from_ts = start_ms // 1000
    elif days is not None:
        span = POLYGON_MAX_DAYS_1M if mult == 1 else 30
        from_ts = now - max(1, min(int(days), span)) * 86400
    else:
        from_ts = now - (POLYGON_MAX_DAYS_1M if mult == 1 else 5) * 86400
    # 增量续拉时 start_ms 会指向"最后一根之后"(可能是几分钟前), 那个窄窗口
    # 同样没有授权 —— 只能往前多要一段, 回来再按 start_ms 裁掉重复部分。
    from_ts = min(from_ts, now - min_span)
    if limit:
        earliest = now - limit * mult * 60
        if earliest < from_ts:                 # 只有"要很久以前"才前移起点
            from_ts = earliest

    def _call(w_from: int, w_to: int) -> list[dict]:
        """单次请求一个时间窗 → bar 列表。

        **不要加 limit / sort 参数** —— 实测同一 3 天 1m 区间:
            裸 URL(含 adjusted)          37.4s  1876 根  ✓
            + sort=asc                   38.6s  1876 根  ✓
            + limit=50000                45.1s  **超时** ✗
        limit 让 Polygon 服务端真的去做排序截取, 响应直接翻倍到超时边缘。
        免费版本来也返回不了 50000 根(5 天 1m 才 ~2045), 限制是多余的。
        """
        url = (f"{POLYGON_API_BASE}/v2/aggs/ticker/{ticker}/range/"
               f"{mult}/minute/{w_from}/{w_to}"
               f"?adjusted=true&apiKey={key}")
        _polygon_throttle()
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(
                    req, timeout=POLYGON_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # 403/429 = 额度被打光, 与其在这里重试不如直接上抛让调用方冷却
            if exc.code in (403, 429):
                raise PolygonRateLimited(str(exc)) from exc
            raise

        if payload.get("status") == "ERROR":
            raise RuntimeError(
                f"Polygon 返回错误: {str(payload.get('message'))[:120]}")
        rows = []
        for row in payload.get("results") or []:
            o, h, l, c = row.get("o"), row.get("h"), row.get("l"), row.get("c")
            if o is None or h is None or l is None or c is None:
                continue
            rows.append({
                "ts": int(row["t"]) // 1000,              # ms → s
                "open": float(o), "high": float(h),
                "low": float(l), "close": float(c),
                "volume": float(row.get("v") or 0.0),
                "trades": 0,
            })
        return rows

    # ── 步骤 1: 单次请求覆盖整段 ──
    # 实测 3 天 1m 单次就能拿 1876 根, 所以先赌这一把, 失败再分窗。
    last_err: Exception | None = None
    out: list[dict] = []
    try:
        out = _call(from_ts, now)
    except PolygonRateLimited as exc:
        # 免费额度被打光: 冷却后交回 Yahoo, 别在同一分钟内继续空转
        _POLYGON_COOLDOWN_UNTIL = time.time() + POLYGON_COOLDOWN_SEC
        raise RuntimeError(
            f"Polygon 限流(403/429), 冷却 {POLYGON_COOLDOWN_SEC}s: {exc}"
        ) from exc
    except Exception as exc:                                  # noqa: BLE001
        last_err = exc

    # ── 步骤 2: 单次失败或空返回, 按 min_span 分窗重试 ──
    # 注意窗口必须 ≥ POLYGON_MIN_SPAN_1M: 按"天"分窗对 1m 必然
    # NOT_AUTHORIZED(实测 1m/5h = 403, 1m/1d 反而能过但只有 143 根,
    # 边界不稳定), 用 2 天窗最稳。
    if not out:
        cursor = from_ts
        windows = 0
        span = min_span
        while cursor < now and windows < 8:
            w_from = cursor
            w_to = min(cursor + span, now)
            # 最后一窗若仍然太窄, 往前借一点凑够 min_span
            if now - w_from < min_span:
                w_from = now - min_span
            try:
                out.extend(_call(w_from, w_to))
            except PolygonRateLimited as exc:
                _POLYGON_COOLDOWN_UNTIL = time.time() + POLYGON_COOLDOWN_SEC
                last_err = exc
                break
            except Exception as exc:                          # noqa: BLE001
                last_err = exc
            windows += 1
            cursor = w_to
            # _call 内部已按 POLYGON_WINDOW_GAP 限速, 这里不必再睡

    if not out:
        raise RuntimeError(f"Polygon 拉取 {ticker}/{interval} 失败: {last_err}")

    # 去重(多窗边界可能重叠) + 按时间排序
    dedup = {b["ts"]: b for b in out}
    out = [dedup[t] for t in sorted(dedup)]
    if limit:
        out = out[-limit:]
    return out


def _fetch_yfinance(asset: str, interval: str, limit: int,
                    start_ms: int | None, retries: int = 3,
                    days: int | None = None) -> list[dict]:
    """yfinance 拉美股分钟 K 线 → dict 列表(ts=UTC epoch 秒)。

    关键: yfinance 返回的 DatetimeIndex 是 tz-aware(交易所在时区, 美股
    为 America/New_York), 必须 .tz_convert('UTC') 后再取 timestamp,
    否则落库的 ts 会偏 4~5 小时, _aggregate() 的桶边界全错。

    注: 当前网络下该路径基本不可用(cookie/crumb SSLError + 限流),
    仅保留作 Yahoo 直连失败时的最后兜底。
    """
    import yfinance as yf                              # runtime import, 避免冷启动开销

    sym = asset.upper()
    iv = _yf_normalize_interval(interval)

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            period = _yf_period_for(days, iv)
            df = yf.Ticker(sym).history(
                interval=iv, period=period,
                auto_adjust=False, raise_errors=False,
                prepost=False, timeout=YF_HTTP_TIMEOUT,
            )
            if df is None or len(df) == 0:
                # 1d/1w 这类非分钟请求才可能走这里; 分钟为空通常是 symbol 错
                last_err = ValueError(
                    f"yfinance 返回空数据 ({sym}/{iv}/{period}) —— "
                    f"可能是代码不存在或已退市")
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_err

            # tz-aware → UTC → epoch 秒
            if getattr(df.index, "tz", None) is None:
                raise RuntimeError(
                    f"yfinance 返回 naive index ({sym}/{iv}), 预期 tz-aware")
            idx_utc = df.index.tz_convert("UTC")

            out: list[dict] = []
            cols = df.columns
            c_open = "Open" if "Open" in cols else cols[0]
            c_high = "High" if "High" in cols else cols[1]
            c_low = "Low" if "Low" in cols else cols[2]
            c_close = "Close" if "Close" in cols else cols[3]
            c_vol = "Volume" if "Volume" in cols else None

            for i in range(len(df)):
                row = df.iloc[i]
                o = float(row[c_open]); h = float(row[c_high])
                l = float(row[c_low]); c = float(row[c_close])
                # 丢掉全 NaN 行(yfinance 偶尔在边界插空行)
                if not all(x == x for x in (o, h, l, c)):
                    continue
                out.append({
                    "ts": int(idx_utc[i].timestamp()),
                    "open": o, "high": h, "low": l, "close": c,
                    "volume": float(row[c_vol]) if c_vol and row[c_vol] == row[c_vol] else 0.0,
                    "trades": 0,
                })

            if start_ms is not None:
                start_s = start_ms // 1000
                out = [b for b in out if b["ts"] >= start_s]

            if limit:
                out = out[-limit:]
            if not out:
                raise ValueError(f"yfinance 过滤后为空 ({sym}/{iv})")
            return out

        except Exception as exc:                          # noqa: BLE001
            last_err = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))           # yfinance 限频, 退避
    raise RuntimeError(f"yfinance 拉取 {sym}/{iv} 失败: {last_err}")


def _insert_bars(conn: sqlite3.Connection, asset: str, interval: str,
                 bars: list[dict], source: str) -> int:
    step = NATIVE_INTERVALS[interval]
    now = int(time.time())
    rows = [
        (asset.upper(), interval, b["ts"], b["open"], b["high"], b["low"],
         b["close"], b["volume"], b.get("trades", 0),
         0 if b["ts"] + step > now else 1, source)
        for b in bars
    ]
    if not rows:
        return 0
    cur = conn.executemany(
        "INSERT OR IGNORE INTO ohlcv_intraday "
        "(asset, interval, ts, open, high, low, close, volume, trades, closed, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return cur.rowcount


def _backfill_yfinance(asset: str, interval: str, days: int,
                       progress=None) -> dict:
    """美股/其他非币种资产回补: 一次 fetch_native 拿全部, pages 恒为 1。

    与 Binance 路径的关键差异:
      - 一次 fetch_native 拿全部, pages 恒为 1(不是分页循环);
      - 失败**直接抛异常**, 不再吞掉后返回 ok:true。
        (旧实现在 Binance 分页里 `except Exception: continue` + failures>20
         才 break, 于是 MU 走了 21 次失败、0 页, 却返回 ok:true,
         上层 /market/history 的 hint 让用户以为"再调一次回补就好"。)

    v310: 函数名保留了 _backfill_yfinance 以免动调用方, 但实际取数已由
    fetch_native 决定(Polygon 优先, Yahoo 兜底), 所以下面返回的 source
    必须按**实际走通的源**来报, 不能继续写死 "yfinance" —— 否则排查时
    看到 source=yfinance 却实际来自 Polygon, 方向直接带偏。
    """
    iv = _yf_normalize_interval(interval)
    # 落库统一用数据源实际返回的原生周期, 保证 read_bars 的
    # source_interval 与库里 interval 一致(否则聚合查不到行)。
    bars = fetch_native(asset, iv, limit=0, start_ms=None, days=days)
    if not bars:
        raise RuntimeError(f"数据源未返回 {asset}/{iv} 任何数据")

    conn = db_conn()
    try:
        total_new = _insert_bars(conn, asset, iv, bars,
                                 _source_label(asset, iv))
    finally:
        conn.close()
    if progress:
        progress(asset, iv, 1, total_new, days)

    first_ts, last_ts = bars[0]["ts"], bars[-1]["ts"]
    return {
        "asset": asset, "interval": iv, "requested_interval": interval,
        "days": days, "pages": 1, "new_bars": total_new,
        "page_failures": 0,
        "fetched_bars": len(bars),
        "first_ts": first_ts, "last_ts": last_ts,
        # 如实反映落库时用的源标识(与 _insert_bars 写入的 source 一致)
        "source": _source_label(asset, iv),
    }


def _source_label(asset: str, interval: str) -> str:
    """落库用的数据源标识。

    必须**如实**反映实际取数路径: 非币种资产现在优先走 Polygon
    (见 fetch_native), 若这里仍写 "yfinance_us:*", 排查时看到实际用的源
    已换却看不到任何 Polygon 痕迹, 会把方向带偏。取不到 Polygon 的资产
    (韩股/商品等) 才落 yahoo_chart:*。
    v311: 加密也不再写死 binance_us —— _fetch_crypto 会在 Binance 失效时
    改用 Coinbase/OKX, 落库标成 binance 会让排查指向错误的源。
    """
    if asset.upper() in ASSET_SYMBOLS:
        src = _last_crypto_source.get(asset.upper(), "binance")
        return f"{src}_us:{interval}"
    if _polygon_ticker(asset) and _polygon_api_key():
        return f"polygon:{interval}"
    return f"yahoo_chart:{interval}"


# v308: 读路径打外网的节流间隔(秒)。
# 美股走 yfinance 单次 8~15s, attribution 页 5 个 panel 并发读同一资产,
# 不节流会把整个页面的读接口拖到超时。60s 足以让盘中数据保持近实时,
# 又不会让请求堆积。
INCREMENTAL_MIN_GAP = 60
_incremental_at: dict[str, int] = {}


def _should_pull_incremental(asset: str, interval: str, now: int) -> bool:
    """同一 (asset, interval) 在节流窗口内只放行一次外网调用。

    进程内内存态即可: 多 worker 下最多每个 worker 各打一次, 远好过
    每个请求都打一次。
    """
    key = f"{asset.upper()}:{interval}"
    last = _incremental_at.get(key, 0)
    if now - last < INCREMENTAL_MIN_GAP:
        return False
    _incremental_at[key] = now
    return True


def backfill(asset: str, interval: str = "1m", days: int = 30,
             progress=None) -> dict:
    """回补某资产原生周期历史(幂等)。

    v308 按数据源分两种策略:
      - Binance(币种): 保留原有"游标分页 + 单页失败跳过"的宽松策略
        (Binance 分页偶发 SSL 断连, 跳页让 incremental 后续补)。
      - yfinance(美股): **不支持按时间游标分页**, 只能整段 period 拉取。
        因此一次拉完, 且失败必须直接抛 —— 旧实现在这里吞异常, 导致
        MU 回补 21 次全失败却返回 {"ok":true,"pages":0}, 误导调用方以为成功。
    """
    init_schema()
    asset = asset.upper()
    if asset not in ASSET_SYMBOLS:
        return _backfill_yfinance(asset, interval, days, progress)
    step = NATIVE_INTERVALS[interval]
    now_ms = int(time.time() * 1000)
    cursor = now_ms - days * 86400 * 1000
    total_new, pages, failures = 0, 0, 0
    conn = db_conn()
    try:
        while cursor < now_ms and pages < 400:
            try:
                bars = fetch_native(asset, interval, limit=1000, start_ms=cursor)
            except Exception as exc:                  # noqa: BLE001
                # 单页失败不终止整段回补 —— 跳过一页继续, 缺口下次 incremental 会补
                failures += 1
                print(f"[intraday] {asset}/{interval} 第 {pages+1} 页失败: {exc}")
                cursor += 1000 * step * 1000
                if failures > 20:
                    break
                continue
            if not bars:
                break
            total_new += _insert_bars(conn, asset, interval, bars,
                                      _source_label(asset, interval))
            pages += 1
            if progress:
                progress(asset, interval, pages, total_new, days)
            nxt = (bars[-1]["ts"] + step) * 1000
            if nxt <= cursor:
                break
            cursor = nxt
            if pages % 5 == 0:
                time.sleep(0.25)                      # 轻微限速, 降低被掐概率
    finally:
        conn.close()
    return {"asset": asset, "interval": interval, "days": days,
            "pages": pages, "new_bars": total_new, "page_failures": failures}


def incremental(asset: str, interval: str = "1m") -> int:
    """增量续拉: 从库中最后一根往后接, 返回新增行数。

    v308: 美股走 yfinance 时, 落库周期必须是 yfinance 实际返回的原生周期
    (3m→5m、1h→60m), 否则这里按 interval 查/写会与 read_bars 的
    source_interval 对不上。
    """
    init_schema()
    asset = asset.upper()
    store_iv = _yf_normalize_interval(interval) if asset not in ASSET_SYMBOLS \
        else interval
    conn = db_conn()
    try:
        row = conn.execute(
            "SELECT MAX(ts) mx FROM ohlcv_intraday WHERE asset=? AND interval=?",
            (asset, store_iv)).fetchone()
        step = NATIVE_INTERVALS[store_iv]
        start_ms = ((row["mx"] + step) * 1000) if row and row["mx"] else None
        bars = fetch_native(asset, store_iv, limit=0, start_ms=start_ms)
        return _insert_bars(conn, asset, store_iv, bars,
                            _source_label(asset, store_iv))
    finally:
        conn.close()


# ────────────────────────── 聚合 ──────────────────────────
def _aggregate(rows: list[sqlite3.Row], src_interval: str, target_sec: int,
               now: int) -> list[dict]:
    """把原生周期行合并成 target_sec 桶(左闭右开, 幂等)。

    丢弃: 桶内根数不足的残缺桶, 以及尚未收盘的最后一桶。
    """
    src = NATIVE_INTERVALS[src_interval]
    if target_sec % src != 0:
        raise ValueError(f"目标周期 {target_sec}s 不能被源周期 {src}s 整除")
    ratio = target_sec // src

    buckets: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        buckets.setdefault((r["ts"] // target_sec) * target_sec, []).append(r)

    out = []
    for b in sorted(buckets):
        group = sorted(buckets[b], key=lambda x: x["ts"])
        if len(group) < ratio:            # 残缺桶 → 丢弃
            continue
        if b + target_sec > now:          # 未收盘 → 丢弃
            continue
        out.append({
            "ts": b,
            "open": group[0]["open"],
            "high": max(x["high"] for x in group),
            "low": min(x["low"] for x in group),
            "close": group[-1]["close"],
            "volume": sum(x["volume"] for x in group),
            "trades": sum(x["trades"] or 0 for x in group),
        })
    return out


def read_bars(asset: str, interval: str = "10m", limit: int = 1500,
              days: int | None = None) -> dict:
    """读指定周期 K 线(自动选可用源周期 + 聚合)。前端 /market/history 数据源。"""
    init_schema()
    asset = asset.upper()
    if interval not in FRONTEND_INTERVALS:
        raise ValueError(f"不支持的周期 {interval}, 可选 {sorted(FRONTEND_INTERVALS)}")
    target_sec, candidates = FRONTEND_INTERVALS[interval]
    now = int(time.time())
    conn = db_conn()
    try:
        used = None
        for src in candidates:                     # 逐个候选: 补缺口 → 选第一个有数据的
            row = conn.execute(
                "SELECT COUNT(*) n, MAX(ts) mx FROM ohlcv_intraday "
                "WHERE asset=? AND interval=?", (asset, src)).fetchone()
            # v308 性能修复: 原实现每次读都同步 incremental()。
            # 币种走 Binance 约 1s 尚可, 但美股走 yfinance 单次需 8~15s,
            # 而 attribution 页有 5 个 panel 并发读同一资产 → 全部请求被
            # 串行阻塞在前端超时边缘, 表现为"面板消失/一直 loading"。
            # 改为节流: 同一 (asset, interval) 在 INCREMENTAL_MIN_GAP 秒内
            # 直接用库内已有数据返回, 不打外网。
            if (row["n"] and (now - row["mx"]) > NATIVE_INTERVALS[src] * 2
                    and _should_pull_incremental(asset, src, now)):
                try:
                    incremental(asset, src)        # 缺口回补(小量增量)
                    row = conn.execute(
                        "SELECT COUNT(*) n, MAX(ts) mx FROM ohlcv_intraday "
                        "WHERE asset=? AND interval=?", (asset, src)).fetchone()
                except Exception:
                    pass
            if row["n"]:
                used = src
                break
        if used is None:
            src_hint = ("binance_us" if asset in ASSET_SYMBOLS else "yfinance_us")
            return {"ok": False, "asset": asset, "interval": interval,
                    "bars": [], "n": 0,
                    "error": f"ohlcv_intraday 无 {asset} 数据(需先回补)",
                    "data_source": src_hint,
                    "hint": f"先调 POST /api/intraday/backfill?asset={asset}&interval=1m&days=5"}

        need = limit * (target_sec // NATIVE_INTERVALS[used])
        if days:
            need = min(need, int(days * 86400 / NATIVE_INTERVALS[used]) + 8)
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume, trades "
            "FROM ohlcv_intraday WHERE asset=? AND interval=? "
            "ORDER BY ts DESC LIMIT ?", (asset, used, need)).fetchall()
        rows = list(reversed(rows))
        if target_sec == NATIVE_INTERVALS[used]:
            bars = [{"ts": r["ts"], "open": r["open"], "high": r["high"],
                     "low": r["low"], "close": r["close"],
                     "volume": r["volume"], "trades": r["trades"]}
                    for r in rows if r["ts"] + target_sec <= now]
        else:
            bars = _aggregate(rows, used, target_sec, now)
        return {"ok": True, "asset": asset, "interval": interval,
                "source_interval": used, "n": len(bars), "bars": bars,
                "data_source": f"ohlcv_intraday({used}→{interval}) {_source_label(asset, used)}"}
    finally:
        conn.close()


# ────────────────────────── 异常点历史 ──────────────────────────
def save_anomalies(asset: str, interval: str, anomalies: list[dict]) -> int:
    """真异常点落库(幂等, 同 (asset,interval,ts,kind) 只留一条, 永不删除)。"""
    init_schema()
    now = int(time.time())
    rows = [
        (asset.upper(), interval, int(a["ts"]), a["kind"], a.get("close"),
         a.get("pattern"), json.dumps(a.get("evidence", []), ensure_ascii=False),
         a.get("rsi"), a.get("macd_hist"), a.get("vol_ratio"),
         a.get("score"), 1 if a.get("uses_future") else 0, now)
        for a in anomalies
    ]
    if not rows:
        return 0
    conn = db_conn()
    try:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO anomaly_history "
            "(asset, interval, ts, kind, close, pattern, evidence, rsi, "
            " macd_hist, vol_ratio, score, uses_future, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def read_anomaly_history(asset: str, interval: str = "10m",
                         limit: int = 3000) -> list[dict]:
    """读历史异常点(跨时段累积, 不受最近 N 天窗口限制)。"""
    init_schema()
    conn = db_conn()
    try:
        rows = conn.execute(
            "SELECT ts, kind, close, pattern, evidence, rsi, macd_hist, "
            "vol_ratio, score, scanned_at FROM anomaly_history "
            "WHERE asset=? AND interval=? ORDER BY ts ASC LIMIT ?",
            (asset.upper(), interval, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["evidence"] = json.loads(d.get("evidence") or "[]")
            except Exception:
                d["evidence"] = []
            out.append(d)
        return out
    finally:
        conn.close()