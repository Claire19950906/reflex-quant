# -*- coding: utf-8 -*-
"""健康检查路由（结构切片，2026-11-17）。

从 api_server.py 搬来 4 条端点：
  /api/health（v398 与 8100 字段名对齐；只给 fd 水位，绝不回 db_path）
  /api/health/internal（API Key 鉴权后才给 db_path / SSE 数 / 配置存在性）
  /api/health/tournament（tournament 指标 + 熔断状态）
  /api/health/models（10 个 LLM 通道探测，30s 缓存）
_HEALTH_TABLES / _SSE_QUEUES / _decision_engine_health / _probe_model_channels
仍留在 api_server（探针线程与 8100 契约都用），router 用 _api() 回引。
"""
from __future__ import annotations

import asyncio
import os
import sys
import subprocess
import time
from typing import Any, Dict

from fastapi import APIRouter, Depends

import config
import license_auth

router = APIRouter()


# ── v453 P0-2：代码-进程一致性 ───────────────────────────────────────────
# 事故：11-18 14:34 启动的引擎，到 20:56/21:47 还在跑当天下午已经被改过的
# `realtime_filter.py` / `engine/ai_worker.py`（工作树 M 状态）。现象是"代码里已经
# 收口了，线上还是旧行为"，而 /api/health 的 code_version 写死 v398，看不出来。
# 现在直接比 mtime：关键源文件比进程启动时间新 → code_stale=true。
_CODE_FILES = (
    "api_server.py", "realtime_filter.py", "db.py", "config.py",
    "routers/health.py",
)
# 引擎进程真正 import 的那一撮（改这些才需要重启 engine）
_ENGINE_CODE_FILES = (
    "engine/ai_worker.py", "engine/ingest.py", "engine/main.py",
    "realtime_filter.py", "db.py", "config.py",
)
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CODE_CACHE: Dict[str, Any] = {"at": 0.0, "snap": None}
_START_CACHE: Dict[str, Any] = {"at": 0.0, "val": None}
_ENGINE_CACHE: Dict[str, Any] = {"at": 0.0, "val": None}


def _proc_start_ts() -> float:
    """本进程启动时刻（epoch 秒）。psutil 没装，退回 `ps -o lstart`。"""
    now = time.time()
    if _START_CACHE["val"] and now - _START_CACHE["at"] < 60:
        return float(_START_CACHE["val"])
    val = 0.0
    try:
        import psutil  # type: ignore
        val = float(psutil.Process(os.getpid()).create_time())
    except Exception:
        try:
            raw = subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(os.getpid())],
                text=True, timeout=5,
            ).strip()
            if raw:
                val = time.mktime(time.strptime(raw, "%a %b %d %H:%M:%S %Y"))
        except Exception:
            val = 0.0
    _START_CACHE["val"] = val
    _START_CACHE["at"] = now
    return val


def _engine_proc_start() -> float:
    """引擎进程启动时刻。P0-2 的事故主角是引擎（14:34 启动、20:56 才改代码），
    只比 api 自己等于没查。engine 由 launchd 拉起，pgrep 找 engine.py 主进程。"""
    now = time.time()
    if _ENGINE_CACHE["val"] and now - _ENGINE_CACHE["at"] < 120:
        return float(_ENGINE_CACHE["val"])
    val = 0.0
    try:
        pids = subprocess.check_output(
            ["pgrep", "-f", "engine.py$"], text=True, timeout=5,
        ).split()
        if pids:
            raw = subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", pids[0]], text=True, timeout=5,
            ).strip()
            if raw:
                val = time.mktime(time.strptime(raw, "%a %b %d %H:%M:%S %Y"))
    except Exception:
        val = 0.0
    _ENGINE_CACHE["val"] = val
    _ENGINE_CACHE["at"] = now
    return val


def _newest(files) -> tuple:
    newest, newest_file = 0.0, ""
    for rel in files:
        path = os.path.join(_HERE, rel)
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        if mt > newest:
            newest, newest_file = mt, rel
    return newest, newest_file


def _running_under_tests() -> bool:
    """是否跑在 pytest 里。

    **为什么需要**：`code_stale` 的语义是「磁盘上的源码比正在跑的进程新」，
    靠 mtime 和进程启动时刻比较。这个判据在测试进程里恒真且无意义 ——
    测试进程就是当场 import 的这份代码，"新代码 vs 旧进程"根本不存在；
    可一旦有人在跑测试前改过任何一个源码文件（改 bug 的常态），
    /api/health 就会返回 degraded，把 test_health_public_minimal 直接带崩。
    """
    return bool(os.getenv("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def _code_snapshot() -> Dict[str, Any]:
    """关键源文件最新 mtime + 进程启动时刻 → 是否"代码比进程新"。60s 缓存。

    api 与 engine 各查各的文件集：健康路由归 api 独有，engine 根本不 import；
    混在一起会把「改了一行 health.py」误报成引擎过期。
    """
    now = time.time()
    if _CODE_CACHE["snap"] and now - _CODE_CACHE["at"] < 60:
        return _CODE_CACHE["snap"]
    newest, newest_file = _newest(_CODE_FILES)
    eng_newest, eng_file = _newest(_ENGINE_CODE_FILES)
    started = _proc_start_ts()
    engine_started = _engine_proc_start()
    in_tests = _running_under_tests()
    snap = {
        "code_mtime": int(newest) if newest else 0,
        "code_mtime_file": newest_file,
        "code_stale": bool(not in_tests and newest and started and newest > started + 1),
        "proc_start": int(started) if started else 0,
        "engine_code_mtime": int(eng_newest) if eng_newest else 0,
        "engine_code_mtime_file": eng_file,
        "engine_start": int(engine_started) if engine_started else 0,
        "engine_stale": bool(not in_tests and eng_newest and engine_started
                             and eng_newest > engine_started + 1),
        "in_tests": in_tests,
    }
    _CODE_CACHE["snap"] = snap
    _CODE_CACHE["at"] = now
    return snap


def _git_sha() -> str:
    """git 短 SHA：仅作「这份代码是哪次提交」的辅助标识，取不到返回空串。"""
    try:
        root = os.path.dirname(os.path.dirname(_HERE))
        out = subprocess.check_output(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            text=True, timeout=5,
        ).strip()
        if out:
            return out
    except Exception:
        pass
    return ""


def _git_version() -> str:
    """语义版本：唯一真值源是 config.CODE_VERSION（v452）。

    原来这里优先返回 git 短 SHA，于是 api 报 `a4304e8`、bridge 报 `v460`，
    两个健康端点对不上；版本号应该只有一处真值，SHA 另放 git_sha 字段。
    """
    try:
        return str(config.CODE_VERSION)
    except Exception:
        return "unknown"


def _api():
    """延迟回引 api_server（健康探针助手与常量仍在原文件）。"""
    import api_server
    return api_server


@router.get("/api/health")
async def health_check():
    """P1 安全修复：精简 health 端点 — 不再泄露 db_path / sse_clients 等
    内部信息。深度诊断字段（has_api_key, has_license, mode 等）已移至
    /api/health/internal 并要求鉴权。

    v321.3 P0: 额外暴露 fd 水位 (level / pct)。
    过去「AI分析挂了」是**静默**故障 —— 进程在、端口在、但不响应, 没人知道
    为什么。现在把 fd 水位摆到 health 里: level != 'ok' 就是出事了。
    """
    # v398: 与 8100 的 health 字段名对齐（同一个 URL 以前是两套字段，探针和外部
    # 监控各认一套）。8100 的 `db`（数据库绝对路径）在这里**不加** —— 那是 v321.3
    # 专门去掉的泄露项；8100 那份同步删除。
    out: Dict[str, Any] = {
        "ok": True,
        "status": "ok",
        "service": "api",
        "version": "1.0",
        "port": 8000,
        "code_version": _git_version(),
        "git_sha": _git_sha(),
        "tables": _api()._HEALTH_TABLES,
        "ts": int(time.time()),
    }
    # v453 P0-2：代码比进程新 → 说明跑的是旧逻辑，必须让它看得见。
    try:
        snap = _code_snapshot()
        out["code"] = snap
        if snap.get("code_stale") or snap.get("engine_stale"):
            out["status"] = "degraded"
            out["ok"] = False
            out["code_stale"] = True
            if snap.get("engine_stale"):
                out["engine_code_stale"] = True
    except Exception:
        pass
    try:
        import fd_guard as _fdg
        snap = _fdg.fd_snapshot()
        out["fd"] = {
            "level": snap.get("level"),
            "pct": snap.get("pct"),
            "used": snap.get("used"),
            "soft_limit": snap.get("soft_limit"),
        }
        if snap.get("level") in ("warn", "critical"):
            out["status"] = "degraded"
            out["ok"] = False
    except Exception:
        pass
    return out


@router.get("/api/health/internal")
async def health_check_internal(_: None = Depends(license_auth.require_api_key)):
    """P1 安全修复：深度健康检查 — 包含 db_path / SSE 数 / 配置存在性等。
    必须经 API Key 鉴权（与 /api/agent_chat 同级别），避免暴露给匿名访问者
    探测部署细节 (db 文件路径 / SSE 连接数等可用于 fingerprinting)。"""
    return {
        "status": "ok",
        "ts": int(time.time()),
        "db_path": _api().DB_PATH,
        "db_exists": os.path.exists(_api().DB_PATH),
        "sse_clients": len(_api()._SSE_QUEUES),
        "mode": os.getenv("TRIDENT_MODE", "full"),
        "decision_engine": _api()._decision_engine_health(),
    }


@router.get("/api/health/tournament", tags=["meta"])
async def health_tournament():
    """v240+ Tournament metrics + circuit breaker state.

    用途:
      - 监控 tournament 是否被频繁短路 (LLM 网关可能有问题)
      - 监控 confirmed_rate (过低 → verifier 视角有问题, 需调 system_prompt)
      - 监控 elapsed_ms (过高 → 考虑改 serial 或减少 verifier 数)
    """
    try:
        import tournament_metrics as _tm
        return {
            "ok": True,
            "metrics": _tm.get_metrics(),
            "circuit_breaker": _tm.get_circuit_breaker_state(),
            "config": {
                "enabled": bool(getattr(config, "TOURNAMENT_ENABLED", False)),
                "quorum": int(getattr(config, "TOURNAMENT_QUORUM", 2)),
                "timeout_seconds": int(getattr(config, "TOURNAMENT_TIMEOUT", 60)),
                "parallel": bool(getattr(config, "TOURNAMENT_PARALLEL", True)),
                "cb_threshold": float(getattr(config, "TOURNAMENT_CB_THRESHOLD", 0.7)),
                "cb_window": int(getattr(config, "TOURNAMENT_CB_WINDOW", 10)),
                "cb_cooldown": int(getattr(config, "TOURNAMENT_CB_COOLDOWN", 20)),
            },
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/health/models")
async def health_models():
    """LLM 通道健康状态：可用性 / 状态码 / 延迟（30s 缓存）。"""
    return await asyncio.to_thread(_api()._probe_model_channels)


@router.get("/api/health/metrics", tags=["meta"])
async def health_metrics(hours: float = 24.0):
    """运行链路指标聚合（只读）：决策 / 建仓结算 / LLM 调用 / 丢写账本 / 作业退出状态。

    用途：一张看板看清链路健康度——方向单转化率、未留痕比例、LLM 错误率、
    丢写按 key 拆分、launchd 作业上次退出码/信号；数据来自库表、silent_drops.jsonl
    与 `launchctl list` 只读扫描，不触发任何写操作。
    """
    try:
        import runtime_metrics as _rm
        return {"ok": True, "metrics": await asyncio.to_thread(_rm.snapshot, hours)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
