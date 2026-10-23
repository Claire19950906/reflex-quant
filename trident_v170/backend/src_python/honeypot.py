"""
Trident v300 — 蜜罐路由
========================

假 endpoint，访问就：
  1. 记录到 audit
  2. 拉黑 IP 1 小时
  3. 返回 404（不让爬虫察觉）

触发条件：TRIDENT_HONEYPOT=1 + 路由被注册时
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse, Response

try:
    from . import anti_bot, demo_audit  # 同包内 import；用 . 走包路径
except (ImportError, ValueError):
    import anti_bot, demo_audit  # 作为脚本跑时 fallback

logger = logging.getLogger("trident.honeypot")

ENABLED = os.getenv("TRIDENT_HONEYPOT", "1") != "0"   # 默认开（防遍历爬虫）

# 蜜罐路由表：(method, path, 假响应)
HONEYPOT_ROUTES = [
    ("GET", "/api/admin/users", "users list"),
    ("GET", "/api/admin/config", "config dump"),
    ("GET", "/api/internal/health", "internal health"),
    ("GET", "/api/v1/_debug", "debug info"),
    ("GET", "/api/v1/_internal/strategy", "strategy leak"),
    ("GET", "/api/v1/_internal/weights", "model weights"),
    ("GET", "/.env", "env file"),
    ("GET", "/api/secret", "secret"),
    ("POST", "/api/admin/reload", "config reload"),
    ("GET", "/api/v1/_test/connection", "test connection"),
]


def add_honeypot_routes(app) -> int:
    """注册蜜罐路由到 FastAPI app。返回注册数量。"""
    if not ENABLED:
        return 0

    registered = 0
    for method, path, _desc in HONEYPOT_ROUTES:
        # 用闭包捕获 method/path
        def make_handler(m: str, p: str):
            async def handler(request: Request):
                client_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                             or (request.client.host if request.client else "unknown"))
                ua = request.headers.get("user-agent", "")

                # 1) 拉黑
                anti_bot.block_ip(client_ip, ttl=anti_bot.HONEYPOT_BLOCK_TTL)

                # 2) 审计
                try:
                    demo_audit.record(
                        method=m, path=p,
                        client_ip=client_ip, api_key_id=None,
                        status=404, latency_ms=0.0,
                        watermark="", mode=os.getenv("TRIDENT_MODE", "demo"),
                        note=f"HONEYPOT_TRIGGERED ua={ua[:80]}",
                    )
                except Exception as e:
                    logger.error("honeypot audit failed: %s", e)

                logger.warning("honeypot_hit ip=%s %s %s ua=%s",
                               client_ip, m, p, ua[:80])

                # 3) 返回 404（不返回 200，避免爬虫察觉蜜罐）
                return Response(status_code=404)

            return handler

        # FastAPI 动态注册
        app.add_api_route(path, make_handler(method, path), methods=[method],
                          include_in_schema=False)
        registered += 1

    logger.info("honeypot_routes_registered count=%d", registered)
    return registered


if __name__ == "__main__":
    os.environ["TRIDENT_HONEYPOT"] = "1"
    os.environ["TRIDENT_ANTI_BOT"] = "1"
    from fastapi import FastAPI
    import anti_bot  # noqa
    app = FastAPI()
    n = add_honeypot_routes(app)
    print(f"registered {n} honeypot routes")
