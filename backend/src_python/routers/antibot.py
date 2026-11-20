# -*- coding: utf-8 -*-
"""v300 反爬虫 + 访客仪表盘（整块搬出 api_server，2026-11-17）。

这一块和前面几批不一样：它不是一串平铺端点，而是一个 `if` 包裹的"整块挂载"——
中间件（AntiBotMiddleware）、蜜罐假路由、6 条端点、/admin 静态页都在里面，
且开关是模块级环境变量 TRIDENT_ANTI_BOT（默认开，=0 关）。

所以这里不做端点级切片，而是封成一个挂载函数 mount_antibot(app, include_flat)：
  · 关闭时直接 return，行为与原来 else 分支的 print 一致（0 副作用）；
  · 开启时中间件与蜜罐照旧作用在传进来的 app 上；
  · 端点挂在本模块的 router 上，最后用 include_flat 逐条塞进 app
    （不能用 include_router：FastAPI 0.163 会包成 _IncludedRouter，bridge 手工
    挂载只认带 .path/.methods 的 APIRoute，会静默丢路由）。

已知历史：/api/security/watermark 曾与 v300.1 投资人 portal 那份双份（现在
routers/portal.py）**同路径重复**，是既有问题。本文件保持原样搬，不顺手删，
留给 P3 统一处理。
"""
from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


class _BehaviorReport(BaseModel):
    """v421（2026-11-17）：从 `mount_antibot` 内部提到模块顶层。

    原写法把模型定义在函数体内，FastAPI 生成 /openapi.json 时无法解析这个
    前向引用，直接抛 PydanticUserError（`_BehaviorReport` is not fully defined）
    → `/openapi.json` 永远 500。实测 2026-11-17 12:5x 复现。
    """
    events: list = []
    turnstile_token: str = ""


def mount_antibot(app, include_flat) -> None:
    """按 TRIDENT_ANTI_BOT 开关挂载 v300 反爬块。"""
    if os.getenv("TRIDENT_ANTI_BOT", "1") == "0":
        print(f"[trident v300] ⚠️  anti-bot features DISABLED  "
              f"(set TRIDENT_ANTI_BOT=1 to enable — 投资者模式必须开)")
        return

    try:
        from . import anti_bot as _anti_bot
        from . import honeypot as _honeypot
        from . import watermark_v2 as _wm
        from . import data_poison as _poison
    except (ImportError, ValueError):
        import anti_bot as _anti_bot                # type: ignore
        import honeypot as _honeypot                # type: ignore
        import watermark_v2 as _wm                  # type: ignore
        import data_poison as _poison               # type: ignore

    # 1) 反爬虫中间件（headless / 黑名单）
    app.add_middleware(_anti_bot.AntiBotMiddleware)
    print(f"[trident v300] anti_bot middleware ON  (blocked={len(_anti_bot._blocked_ips)} ips)")

    # 2) 蜜罐路由（10 个假 URL）
    _honeypot_count = _honeypot.add_honeypot_routes(app)
    print(f"[trident v300] honeypot routes registered: {_honeypot_count}")
# ═══ v300 SEGMENT 1 END (continue below) ═══

    # 3) 投资人侧 — 行为评分上报
    from fastapi import Body
    @router.post("/api/security/behavior")
    async def _behavior_report(payload: _BehaviorReport):
        """前端 AntiBotGuard 上报行为事件，存 audit + 算分。"""
        score = _anti_bot.behavior_score(payload.events or [])
        try:
            from . import demo_audit as _audit
        except (ImportError, ValueError):
            import demo_audit as _audit
        _audit.record(
            method="POST", path="/api/security/behavior",
            client_ip="self", api_key_id=None,
            status=200, latency_ms=0.0,
            watermark="", mode=os.getenv("TRIDENT_MODE", "demo"),
            note=f"BEHAVIOR_SCORE={score} n_events={len(payload.events or [])}",
        )
        return {"score": score, "n": len(payload.events or [])}

    # 4) 投资人侧 — Issue / Verify Challenge
    @router.get("/api/security/challenge")
    async def _issue_challenge():
        return _anti_bot.issue_challenge()

    @router.post("/api/security/challenge/verify")
    async def _verify_challenge(payload: dict = Body(default={})):
        ok, reason = _anti_bot.verify_challenge(payload)
        return {"ok": ok, "reason": reason}

    # 5) 投资人侧水印 —— 2026-11-17 已删除本处实现。
    #    同路径 GET 有且仅有 routers/portal.py 那条（注册在前、实际生效）；
    #    本处这份 investor_id 默认 "anon"、无 ip 字段，永远匹配不到，属于死代码。

    # 6) Admin — 访客仪表盘数据
    try:
        from . import demo_audit as _audit2
    except (ImportError, ValueError):
        import demo_audit as _audit2  # 作为脚本跑时(.失败)兜底
    @router.get("/api/admin/visitors")
    async def _admin_visitors(limit: int = 200, status: Optional[int] = None):
        """读 audit 表，返回最近访问记录 + KPI。"""
        rows = _audit2.recent(limit=int(limit), status=status)
        today = int(time.time()) - 86400
        all_today = _audit2.recent(limit=2000, since_ts=today)
        unique_ips = {r.get("client_ip") for r in all_today if r.get("client_ip")}
        rate_limited = sum(1 for r in all_today if r.get("status") == 429)
        honeypot = sum(1 for r in all_today
                       if r.get("note", "").startswith("HONEYPOT_TRIGGERED"))
        from collections import Counter
        path_counts = Counter(r.get("path") for r in all_today)
        ip_counts = Counter(r.get("client_ip") for r in all_today)
        top_paths = [
            {"path": p, "count": c,
             "unique_ips": len({r.get("client_ip") for r in all_today if r.get("path") == p})}
            for p, c in path_counts.most_common(10)
        ]
        ip_last = {}
        for r in all_today:
            ip = r.get("client_ip")
            if not ip: continue
            ts = r.get("ts", "")
            if ip not in ip_last or ts > ip_last[ip]:
                ip_last[ip] = ts
        top_ips = [
            {"ip": ip, "count": c, "last_seen": ip_last.get(ip, "")}
            for ip, c in ip_counts.most_common(20)
        ]
        return {
            "kpi": {
                "visitors": len(unique_ips),
                "requests": len(all_today),
                "rate_limited": rate_limited,
                "honeypot": honeypot,
            },
            "events": rows,
            "top_paths": top_paths,
            "top_ips": top_ips,
        }

    # NOTE: v189-8 曾在此处加过 /api/admin/conn-leaks 诊断端点, 但 openapi.json 实测
    # /api/admin/{block,visitors,conn-leaks} 三个路由**均未注册**, 说明本段所在的 try
    # 块在装饰器执行前就抛异常被吞掉了 (既有问题, 非本次引入)。因此泄漏诊断不再依赖
    # HTTP 路由, 改由 db.py 的追踪器周期性写文件, 对 engine 等无 Web 层进程同样有效。

    @router.post("/api/admin/block")
    async def _admin_block(payload: dict = Body(default={})):
        """手动拉黑 IP。body: {"ip": "1.2.3.4", "ttl": 3600}"""
        ip = (payload or {}).get("ip", "")
        ttl = int((payload or {}).get("ttl", 3600))
        if not ip:
            return {"ok": False, "error": "ip required"}
        _anti_bot.block_ip(ip, ttl=ttl)
        return {"ok": True, "ip": ip, "ttl": ttl}

    # 7) Admin — 静态 admin.html 仪表盘
    # 路径锚点：改成 common/_paths 的 REPO_DIR。原来靠 __file__ 三层 dirname 反推，
    # 文件搬进 routers/ 后会多差一级（指到 backend/admin.html），静默漏注册 /admin。
    from common._paths import REPO_DIR as _REPO_DIR
    _admin_html_path = os.path.join(_REPO_DIR, "admin.html")
    if os.path.exists(_admin_html_path):
        from fastapi.responses import FileResponse
        @router.get("/admin")
        async def _admin_dashboard():
            return FileResponse(_admin_html_path, media_type="text/html")
        print(f"[trident v300] admin dashboard: /admin → {_admin_html_path}")

    print(f"[trident v300] ✅ all anti-bot features ENABLED  "
          f"(honeypot={_honeypot_count}, behavior/visitor/watermark endpoints active)")
    print(f"[trident v300] 🛡️  admin dashboard: http://<host>:<port>/admin")
    include_flat(app, router)
