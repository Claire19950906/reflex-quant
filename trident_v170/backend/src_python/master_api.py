"""
Trident v300 — Master API（算法本体，**只在你 Mac 上跑**）
=========================================================

监听：127.0.0.1:9001（公网绝对扫不到，靠 Cloudflare Tunnel 暴露）
白名单：127.0.0.1 + Bearer token
鉴权：HMAC Bearer token（与 Fly.io 后端共享 secret）

⚠️  ⚠️  ⚠️  这是你的算法本体！  ⚠️  ⚠️  ⚠️
   不要把这个文件提交到任何公网仓库！
   不要上传到 Fly.io / Vercel / GitHub！

你只需要改一个函数：trident_compute_signal()
其他都不用动。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn

# ──────────────── 配置 ────────────────
MASTER_API_TOKEN = os.getenv("TRIDENT_MASTER_TOKEN", "trident-master-token-change-me-32bytes-min")
# P2 安全修复：默认 token 是已知常量（公开仓库可搜到）。如果 env 没设就用了默认，
# 任何人都能用 "trident-master-token-change-me-32bytes-min" 通过鉴权 → 远程调用算法本体。
# 改用 fail-loud：检测到默认 token 时启动时打印 ⚠️ CRITICAL，提示用户必须设置。
_DEFAULT_TOKEN_FINGERPRINT = hashlib.sha256(
    b"trident-master-token-change-me-32bytes-min"
).hexdigest()[:8]
_USING_DEFAULT_TOKEN = (
    hashlib.sha256(MASTER_API_TOKEN.encode("utf-8")).hexdigest()[:8]
    == _DEFAULT_TOKEN_FINGERPRINT
)
PORT = int(os.getenv("TRIDENT_MASTER_PORT", "9001"))
HOST = os.getenv("TRIDENT_MASTER_HOST", "127.0.0.1")  # ⚠️ 只能 127.0.0.1

logger = logging.getLogger("trident.master")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [master] %(levelname)s: %(message)s")

app = FastAPI(title="Trident Master API", version="3.0.0",
              docs_url=None, redoc_url=None, openapi_url=None)

# ══════════════════════════════════════════════════════════════════════
# ▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼
# 你的算法 — 改这一个函数就行,其他全别动
# ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲
# ══════════════════════════════════════════════════════════════════════

def trident_compute_signal(symbol: str, params: Optional[dict] = None) -> dict:
    """你的算法本体。**这是你的 IP**,别动其他文件,改这里就行。

    Args:
        symbol:  交易对 / 股票代码,例 "BTCUSDT" / "AAPL" / "NVDA"
        params:  可选参数,例 {"timeframe": "1h", "lookback": 30}

    Returns:
        dict, 返回值会经过脱敏 + 水印后展示给投资人(经由 Fly.io 镜像)。

        推荐字段:
        - signal:        "buy" | "sell" | "hold"
        - strength:      float 0-1
        - confidence:    float 0-1
        - reasons:       list[str] 触发原因
        - history_7d:    list[dict] 最近 7 天曲线
        - next_action_at: ISO 时间戳

    示例(删除):
        return {
            "signal": "buy",
            "strength": 0.73,
            "confidence": 0.85,
            "reasons": ["RSI<30", "成交量放大"],
            "history_7d": [{"ts": "2026-10-02", "signal": "buy", "pnl": 0.012}],
        }
    """
    # ──────── 你填这里:调用你的算法 ────────
    # 1) 如果你的是个函数:
    #    from my_strategy import compute
    #    return compute(symbol=symbol, **params or {})
    #
    # 2) 如果你的是个 CLI(subprocess):
    #    import subprocess, json
    #    out = subprocess.check_output(
    #        ["python3", "/path/to/your/signal.py", symbol],
    #        timeout=10,
    #    )
    #    return json.loads(out)
    #
    # 3) 如果你的是个 notebook:
    #    import jupyter_client
    #    ...

    # ⚠️ 默认占位实现 — 返回假数据,让你立刻能跑通端到端
    # 真实算法上线时,把这一段删掉,改成上面 1)/2)/3) 之一
    import random
    random.seed(hash(symbol) & 0xFFFFFFFF)
    signal_choice = random.choice(["buy", "sell", "hold"])
    return {
        "signal": signal_choice,
        "strength": round(random.uniform(0.5, 0.95), 2),
        "confidence": round(random.uniform(0.6, 0.92), 2),
        "reasons": [
            f"RSI = {random.randint(20, 80)}",
            f"VOL 异常 +{random.randint(10, 200)}%",
            f"MA 交叉 {random.choice(['金叉', '死叉', '无'])}",
        ],
        "history_7d": [
            {"ts": f"2026-10-{d:02d}", "signal": random.choice(["buy","sell","hold"]),
             "pnl": round(random.uniform(-0.02, 0.04), 3)}
            for d in range(2, 9)
        ],
        "next_action_at": int(time.time()) + 3600,
        "_demo": True,   # ⚠️ 标记这是 demo data
    }


# ══════════════════════════════════════════════════════════════════════
# ▼▼▼ 中间件 / 鉴权 / 端点 — 别动 ▼▼▼
# ══════════════════════════════════════════════════════════════════════


def verify_token(authorization: Optional[str]) -> bool:
    """校验 Bearer token.使用 hmac.compare_digest 防时序攻击."""
    if not authorization:
        return False
    if not authorization.startswith("Bearer "):
        return False
    token = authorization[7:]
    return hmac.compare_digest(token, MASTER_API_TOKEN)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """白名单路径:/health、/,其他都要 token."""
    if request.url.path in ("/health", "/"):
        return await call_next(request)
    auth = request.headers.get("authorization")
    if not verify_token(auth):
        client_ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        logger.warning("unauthorized from %s %s", client_ip, request.url.path)
        return JSONResponse(status_code=401,
                            content={"error": "unauthorized"})
    return await call_next(request)


# ──────────────── 端点 ────────────────

@app.get("/")
async def root():
    return {
        "service": "trident-master",
        "version": "3.0.0",
        "listen": f"{HOST}:{PORT}",
        "public": False,
        "note": "Algorithm本体. 暴露在公网不安全.",
    }


@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time()), "pid": os.getpid()}


@app.get("/signal/{symbol}")
async def signal(symbol: str, request: Request,
                 timeframe: str = "1h", lookback: int = 7):
    """投资人主入口:算一个 symbol 的信号."""
    symbol = symbol.upper().strip()
    if not symbol or len(symbol) > 32:
        return JSONResponse(status_code=400,
                            content={"error": "invalid symbol"})
    start = time.time()
    try:
        result = trident_compute_signal(
            symbol=symbol,
            params={"timeframe": timeframe, "lookback": lookback},
        )
    except NotImplementedError as e:
        return JSONResponse(
            status_code=501,
            content={"error": "algorithm_not_implemented",
                    "hint": "在 master_api.py 的 trident_compute_signal() 里填你的算法",
                    "detail": str(e)})
    except Exception as e:
        logger.exception("compute failed for %s", symbol)
        return JSONResponse(status_code=500,
                            content={"error": f"{type(e).__name__}: {e}"})

    latency_ms = (time.time() - start) * 1000
    logger.info("signal %s → %s (%.1fms)", symbol,
                result.get("signal", "?"), latency_ms)
    return {
        "ok": True,
        "symbol": symbol,
        "ts": int(time.time()),
        "latency_ms": round(latency_ms, 1),
        **result,
    }


@app.post("/backtest")
async def backtest(payload: dict):
    """回测入口. payload: {symbol, from_ts, to_ts, params}"""
    symbol = (payload or {}).get("symbol", "")
    if not symbol:
        raise HTTPException(400, "symbol required")
    # TODO: 接你的回测函数
    return {"ok": False, "error": "backtest not implemented yet",
            "hint": "在 master_api.py 的 /backtest 端点加你的回测逻辑"}


@app.post("/batch")
async def batch(payload: dict):
    """批量. payload: {symbols: [...]}"""
    symbols = (payload or {}).get("symbols", [])
    if not symbols or not isinstance(symbols, list):
        raise HTTPException(400, "symbols list required")
    out = []
    for s in symbols[:50]:  # 限 50 个
        try:
            r = trident_compute_signal(symbol=s.upper())
            out.append({"symbol": s, "ok": True, **r})
        except Exception as e:
            out.append({"symbol": s, "ok": False, "error": str(e)})
    return {"ok": True, "count": len(out), "results": out}


# ──────────────── 启动 ────────────────

if __name__ == "__main__":
    # P2 安全修复：不打印 token 任何前缀 — 旧版本打印了 MASTER_API_TOKEN[:8]，
    # 攻击者拿到日志就能把搜索空间从 2^N 降到 2^(N-64)，即使只泄露 8 字节也是不必要的降维。
    # 改用指纹：sha256(token) 前 8 字符，能区分"这次用的 token 跟上次是不是同一个"但不可逆。
    token_fp = hashlib.sha256(MASTER_API_TOKEN.encode("utf-8")).hexdigest()[:8]
    logger.info("=" * 60)
    logger.info("🛡️  Trident Master API v3.0.0")
    logger.info("   listen: %s:%d (local only)", HOST, PORT)
    logger.info("   token fingerprint (sha256[:8]): %s...", token_fp)
    logger.info("   token length: %d", len(MASTER_API_TOKEN))
    logger.info("   ⚠️   这个 API 只在 127.0.0.1 上监听")
    if _USING_DEFAULT_TOKEN:
        # P2: 默认 token 是公开常量（仓库里能搜到）→ 任何人都能用它通过鉴权。
        # 这是 fail-loud 警告，不阻塞启动（dev 模式下故意这样用），但希望用户立刻看到。
        logger.critical(
            "🚨🚨🚨 TRIDENT_MASTER_TOKEN 未设置！正在使用 PUBLIC DEFAULT TOKEN！\n"
            "   任何知道这个仓库的人都能用 'trident-master-token-change-me-32bytes-min'\n"
            "   通过鉴权并调用你的算法本体。\n"
            "   生产部署必须设置: export TRIDENT_MASTER_TOKEN=$(openssl rand -hex 32)"
        )
    logger.info("=" * 60)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", access_log=False)

