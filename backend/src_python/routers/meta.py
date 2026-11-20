# -*- coding: utf-8 -*-
"""meta 域路由（结构切片样板，2026-11-16）。

为什么从这三个端点开始切：
    /api/health/security、/health、/metrics 都是**只读、无业务状态**的探针，
    依赖面最小（license_auth / demo_mode / 进程自身），
    是"把 20,199 行单文件按域拆开"的第一刀最安全的落点。

切片规矩（后续每个域都照这个来）：
    1. 端点整体搬走，**不重写逻辑**（连注释一起搬，行为零变化）；
    2. 只把 `@app.` 换成 `@router.`，其余字面不动；
    3. 需要的依赖显式 import，**不要**去 import api_server（否则又变成循环依赖）；
    4. api_server 里改成 include_router，并在原位留一行"已迁移"注释。
"""
from __future__ import annotations

import json
import time
from db_location import DB_PATH
from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

import demo_mode
import license_auth

router = APIRouter(tags=["meta"])


@router.get("/api/health/security", tags=["meta"])
async def health_security() -> Dict[str, Any]:
    """v300: 当前鉴权状态摘要（运维/前端探针用）。"""
    return {
        "ok": True,
        "security": license_auth.startup_self_check(),
        "demo_mode": {
            "rpm": demo_mode.DEMO_RPM,
            "delay_ms": demo_mode.DEMO_DELAY_MS,
            "history_days": demo_mode.DEMO_HISTORY_DAYS,
            "decimals": demo_mode.DEMO_DECIMALS,
            "daily_budget": demo_mode.DEMO_DAILY_TOKEN_BUDGET,
        },
    }


@router.get("/health", tags=["meta"])
async def root_health() -> Dict[str, Any]:
    """轻量存活探针（消除浏览器 / 监控对 http://host:8000/ 的 404 噪声）"""
    return {"status": "ok", "service": "trident-agent-api", "version": "4.0.0"}


_API_BOOT_TS = time.time()   # /metrics 的 uptime 基准（模块导入时刻）


@router.get("/metrics", tags=["meta"])
def prometheus_metrics():
    """Prometheus 文本格式指标（2026-11-16 新增，补"有监控但没指标"这个缺口）。

    设计取舍：**不在这个端点里查 1.4GB 的库**。
    指标几乎全部来自哨兵每 10 分钟刷新的 reports/health/quota_state.json，
    加上进程自身开销（RSS / fd / 运行时长），单次响应基本零 IO。
    这样做的原因：监控端点最忌讳自己成为故障源 —— 库被锁住时，
    /metrics 若还要查库，就会跟着一起挂，正好在最需要它的时候失联。

    暴露内容：
        trident_up                        接口进程存活（恒 1）
        trident_process_rss_mb            进程 RSS
        trident_process_fds               打开的文件描述符数
        trident_process_uptime_s          进程运行秒数
        trident_llm_calls_30m             LLM 调用（30 分钟）
        trident_llm_429_15m               429 次数（15 分钟）
        trident_llm_429_ratio_15m         429 占比
        trident_decisions_30m             决策产出（30 分钟）
        trident_dlq_open                  未解决 DLQ 条数
        trident_db_size_mb                数据库体积
        trident_sentinel_age_s            哨兵快照距今秒数
        trident_heartbeat_zombies         僵尸 worker 数
    """
    import os as _os
    import time as _time
    from fastapi.responses import PlainTextResponse

    lines: list[str] = []

    def _add(name: str, value, help_text: str = "", typ: str = "gauge") -> None:
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {typ}")
        lines.append(f"{name} {value}")

    rss_mb = 0.0
    fds = 0
    uptime_s = 0
    try:
        import resource as _res
        import sys as _sys
        _raw = _res.getrusage(_res.RUSAGE_SELF).ru_maxrss
        # macOS 单位为字节、Linux 为 KB —— 不区分会差 1024 倍（实测显示过 202196 MB）
        rss_mb = round(_raw / 1048576.0 if _sys.platform == "darwin" else _raw / 1024.0, 1)
    except Exception:
        pass
    try:
        fds = len(_os.listdir(f"/dev/fd"))
    except Exception:
        pass
    uptime_s = int(_time.time() - _API_BOOT_TS)

    _add("trident_up", 1, "api_service_alive")
    _add("trident_process_rss_mb", rss_mb, "resident_set_size_mb")
    _add("trident_process_fds", fds, "open_file_descriptors")
    _add("trident_process_uptime_s", uptime_s, "process_uptime_seconds")

    snap_path = _os.path.join(str(__import__("pathlib").Path(__file__).resolve().parents[2]),
                              "reports", "health", "quota_state.json")
    snap, snap_age = {}, None
    try:
        with open(snap_path, encoding="utf-8") as _fh:
            snap = json.load(_fh)
        snap_age = round(_time.time() - _os.path.getmtime(snap_path), 1)
    except Exception:
        pass

    _add("trident_llm_calls_30m", snap.get("calls_30m", 0), "llm_calls_last_30m")
    _add("trident_llm_429_15m", snap.get("err429_15m", 0), "http_429_last_15m")
    _add("trident_llm_429_ratio_15m", snap.get("ratio429_15m") or 0, "http_429_ratio_last_15m")
    # 2026-11-16 修正：原来 100% 依赖哨兵快照（10 分钟才刷一次），
    # 实测出现过「实际 11:38 还在产出决策，指标却显示 decisions_30m=0」——
    # 监控说"没产出"，人就会去查一个不存在的问题。
    # 现在：**先尝试读实时值（300ms 超时、只读、绝不等待）**，读不到（库被锁）
    # 才退回快照；并用 trident_decisions_30m_source 标出来源(1=实时/0=快照)，
    # 这样"数值旧"这件事本身也是可见的，不会静默骗人。
    _dec_30m = snap.get("decisions_30m", 0)
    _dec_src = 0
    try:
        import sqlite3 as _sq
        # 注意层级：本文件在 src_python/routers/ 下，库在 backend/ 下 →
        # 要退三级（routers → src_python → backend）。切片时这一级最容易算错。
        _dbp = str(DB_PATH)
        _iso_cut = _time.strftime("%Y-%m-%dT%H:%M:%S",
                                  _time.localtime(_time.time() - 1800))
        _c = _sq.connect(f"file:{_dbp}?mode=ro", uri=True, timeout=0.3)
        try:
            _dec_30m = _c.execute(
                "SELECT COUNT(*) FROM ai_decisions "
                "WHERE substr(replace(created_at,'T',' '),1,19) >= ?",
                (_iso_cut.replace("T", " "),)).fetchone()[0]
            _dec_src = 1
        finally:
            _c.close()
    except Exception as exc:
        # v399：允许失败，但不许不留痕（v322 审计门禁要求）
        print(f"[META-HEALTH] 决策计数实时读取失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    # 2026-11-16 再修一层：读不到实时值、且连快照都过期时，**不要输出 0**。
    # 0 的意思是"这段时间确实没产出"（是事实）；"不知道"必须显式表达成 NaN，
    # 否则看板会把"监控瞎了"显示成"系统正常但没信号"，人就会漏掉真事故。
    _snap_stale = (snap_age is not None and snap_age > 600)
    if _dec_src == 0 and _snap_stale:
        _dec_30m = float("nan")
    _add("trident_decisions_30m", _dec_30m, "ai_decisions_last_30m")
    _add("trident_decisions_30m_source", _dec_src,
         "1=live_query 0=stale_snapshot")
    _add("trident_decisions_30m_stale", 1 if (_dec_src == 0 and _snap_stale) else 0,
         "1=data_stale_not_zero")
    _add("trident_dlq_open", snap.get("dlq_open", 0), "unresolved_dlq_events")
    _add("trident_heartbeat_zombies", len(snap.get("heartbeat_zombie") or []),
         "workers_with_stale_heartbeat")
    if snap_age is not None:
        _add("trident_sentinel_age_s", snap_age, "quota_snapshot_age_seconds")

    try:
        db_p = str(DB_PATH)
        _add("trident_db_size_mb", round(_os.path.getsize(db_p) / 1048576.0, 1), "db_file_size_mb")
    except Exception:
        pass

    return PlainTextResponse("\n".join(lines) + "\n")


# -- SSE stream route ------------------------------------------------------

# ── 模拟实盘「买卖点技术指标 KPI」 ──
