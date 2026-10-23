"""
Trident v300 — 反爬虫工具
==========================

提供：
  1. Headless 检测（UA / WebDriver 标志 / 浏览器指纹）
  2. 行为指纹（鼠标轨迹 / 滚动 / 停留时长）
  3. JS Challenge 签发 + 校验
  4. FastAPI 中间件（默认关闭，TRIDENT_ANTI_BOT=1 启用）

设计原则：
  - 默认全关，环境变量开启
  - 不动业务路由，仅在中间件层过滤
  - 命中阈值只返回 403 + 记录到 audit，不抛异常
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Tuple

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("trident.anti_bot")

# ──────────────── 配置 ────────────────
ENABLED = os.getenv("TRIDENT_ANTI_BOT", "1") != "0"   # 默认开（v300：会员机制要有人看）
CHALLENGE_SECRET = os.getenv("TRIDENT_CHALLENGE_SECRET", "trident-challenge-secret-change-me")
CHALLENGE_TTL_SEC = 300          # 5 分钟
BEHAVIOR_MIN_SCORE = 0.35        # 行为分低于此视为机器人
HONEYPOT_BLOCK_TTL = 3600        # 命中蜜罐后 1 小时内拒绝该 IP

# 内存级 IP 黑名单（重启清空；生产可换 Redis）
_blocked_ips: dict = {}

# 已知 headless UA 黑名单
_HEADLESS_UA_KEYWORDS = (
    "headless", "phantomjs", "selenium", "webdriver",
    "scrapy", "httpclient", "python-requests", "go-http-client",
    "curl/", "wget/", "node-fetch", "axios/", "okhttp",
)

# 已知自动化工具特征 header
_AUTOMATION_HEADERS = (
    "x-automated", "x-spam", "x-scraper", "x-python-requests",
    "x-playwright", "x-selenium",
)


# ──────────────── Headless 检测 ────────────────
def detect_headless(headers: dict) -> Tuple[bool, str]:
    """检测请求是否来自 headless / 自动化工具。

    Returns: (is_bot, reason)
    """
    ua = (headers.get("user-agent") or "").lower()

    # 1) UA 关键字
    for kw in _HEADLESS_UA_KEYWORDS:
        if kw in ua:
            return True, f"ua_keyword:{kw}"

    # 2) 显式 automation header
    for h in _AUTOMATION_HEADERS:
        if h in {k.lower() for k in headers}:
            return True, f"automation_header:{h}"

    # 3) webdriver 标志
    sec_ch_ua = (headers.get("sec-ch-ua") or "").lower()
    if "headless" in sec_ch_ua:
        return True, "sec-ch-ua:headless"

    # 4) 完全没 UA
    if not ua:
        return True, "missing_ua"

    return False, ""


# ──────────────── JS Challenge ────────────────
def issue_challenge() -> dict:
    """签发 challenge token：前端拿到后算 a + b 再回传。"""
    ts = int(time.time())
    nonce = hashlib.sha256(f"{ts}-{os.urandom(8).hex()}".encode()).hexdigest()[:16]
    a = ts % 97
    b = (ts >> 4) % 89
    payload = {"ts": ts, "nonce": nonce, "a": a, "b": b}
    sig = hmac.new(
        CHALLENGE_SECRET.encode(),
        json.dumps(payload, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()[:24]
    return {"challenge": payload, "sig": sig}


def verify_challenge(token: dict) -> Tuple[bool, str]:
    """校验前端提交的回答。token 格式：{"payload":..., "sig":..., "answer": <int>}"""
    if not isinstance(token, dict):
        return False, "malformed"
    payload = token.get("payload") or {}
    sig = token.get("sig", "")
    answer = token.get("answer")
    if not payload or not sig or answer is None:
        return False, "missing_field"
    expected = hmac.new(
        CHALLENGE_SECRET.encode(),
        json.dumps(payload, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()[:24]
    if not hmac.compare_digest(sig, expected):
        return False, "bad_sig"
    if abs(int(time.time()) - int(payload.get("ts", 0))) > CHALLENGE_TTL_SEC:
        return False, "expired"
    expected_answer = int(payload.get("a", 0)) + int(payload.get("b", 0))
    if int(answer) != expected_answer:
        return False, "wrong_answer"
    return True, "ok"


# ──────────────── 行为评分 ────────────────
def behavior_score(events: list) -> float:
    """根据前端上报事件算"像不像真人"（0=机器人 ~ 1=真人）。

    events: [{"type": "mousemove"|"scroll"|"key"|"click", "t": ms, "x":..., "y":...}, ...]
    """
    if not events or len(events) < 3:
        return 0.0

    n_score = min(len(events) / 30, 1.0)

    moves = [e for e in events if e.get("type") == "mousemove"]
    if len(moves) >= 3:
        intervals = [moves[i+1]["t"] - moves[i]["t"] for i in range(len(moves)-1)]
        mean = sum(intervals) / len(intervals) if intervals else 0
        var = sum((x - mean) ** 2 for x in intervals) / len(intervals) if intervals else 0
        var_score = min(var / 5000, 1.0)
    else:
        var_score = 0.0

    scrolls = [e for e in events if e.get("type") == "scroll"]
    if len(scrolls) >= 2:
        speeds = [abs(scrolls[i+1].get("y", 0) - scrolls[i].get("y", 0))
                  for i in range(len(scrolls)-1)]
        speed_var = (max(speeds) - min(speeds)) if speeds else 0
        scroll_score = min(speed_var / 100, 1.0)
    else:
        scroll_score = 0.3

    return round(0.4 * n_score + 0.4 * var_score + 0.2 * scroll_score, 3)


# ──────────────── IP 黑名单管理 ────────────────
def block_ip(ip: str, ttl: int = HONEYPOT_BLOCK_TTL):
    _blocked_ips[ip] = time.time() + ttl


def is_blocked(ip: str) -> bool:
    exp = _blocked_ips.get(ip)
    if exp is None:
        return False
    if time.time() > exp:
        _blocked_ips.pop(ip, None)
        return False
    return True


# ──────────────── FastAPI 中间件 ────────────────
class AntiBotMiddleware(BaseHTTPMiddleware):
    """仅在 TRIDENT_ANTI_BOT=1 时挂载。"""

    async def dispatch(self, request: Request, call_next):
        if not ENABLED:
            return await call_next(request)

        client_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                     or (request.client.host if request.client else "unknown"))

        if is_blocked(client_ip):
            return JSONResponse(
                status_code=403,
                content={"error": "blocked", "reason": "honeypot_triggered"},
            )

        is_bot, reason = detect_headless(dict(request.headers))
        if is_bot:
            logger.warning("anti_bot_blocked ip=%s reason=%s", client_ip, reason)
            return JSONResponse(
                status_code=403,
                content={"error": "forbidden", "reason": f"headless:{reason}"},
            )

        return await call_next(request)


# ──────────────── 单元自检 ────────────────
if __name__ == "__main__":
    print("detect_headless curl:", detect_headless({"user-agent": "curl/8.0"}))
    print("detect_headless chrome:", detect_headless(
        {"user-agent": "Mozilla/5.0 (Macintosh) Chrome/120.0 Safari/537.36"}))
    ch = issue_challenge()
    print("challenge:", ch)
    print("verify correct:", verify_challenge(
        {"payload": ch["challenge"], "sig": ch["sig"], "answer": ch["challenge"]["a"] + ch["challenge"]["b"]}))
    print("verify wrong:", verify_challenge(
        {"payload": ch["challenge"], "sig": ch["sig"], "answer": 999}))
    print("behavior empty:", behavior_score([]))
    print("behavior fake:", behavior_score([{"type": "mousemove", "t": i*100, "x": 0, "y": 0} for i in range(5)]))
