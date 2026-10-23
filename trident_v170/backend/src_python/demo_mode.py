#!/usr/bin/env python3
"""
Trident v300 — 体验模式（Demo Mode）核心模块
==========================================

设计目标：算法（IP）全部留在后端 Python 进程内，对外只暴露"被脱敏 + 被限流
+ 被延迟"的 API。前端 Next.js 只渲染，不可也不需要拿到算法源码。

三种发布模式（通过 ``TRIDENT_MODE`` 环境变量切换）：

  ┌──────────┬─────────────────────────────────────────────────────────────┐
  │  full    │ 开发者本机 / 离线自用 — 不鉴权、不脱敏、不延迟（默认）       │
  │  demo    │ 公开体验版（朋友/用户） — IP 限流 + 数值脱敏 + 延迟 + 水印  │
  │  licensed│ 受邀用户 — License Key 验证 + 设备指纹绑定 + 可选 API Key   │
  └──────────┴─────────────────────────────────────────────────────────────┘

安全不变量：

  1. ``full`` 模式建议仅本机启用 — 不能对外暴露 full API。
  2. ``demo`` 模式永远返回"够用但不精确"的数据 — 任何能把响应喂回去反推
     出阈值/权重的接口都需过 ``sanitize_number``。
  3. ``demo`` 模式禁止输出原始 LLM prompt / chain-of-thought（最多给结论）。
  4. License 文件必须 ``hmac-sha256`` 签名才生效（防伪）。
  5. 任何 demo / licensed 响应都带 X-Trident-Watermark 头（事后追溯）。
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import math
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("trident.demo_mode")


# ─────────────────────────────────────────────────────────────────────────────
# 配置（全部从环境变量读）
# ─────────────────────────────────────────────────────────────────────────────

def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v.strip() if v is not None else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        return default


TRIDENT_MODE: str = _env_str("TRIDENT_MODE", "full").lower()

DEMO_RPM: int = _env_int("TRIDENT_DEMO_RPM", 60)
DEMO_DELAY_MS: int = _env_int("TRIDENT_DEMO_DELAY_MS", 200)
DEMO_JITTER_MS: int = _env_int("TRIDENT_DEMO_JITTER_MS", 80)
DEMO_HISTORY_DAYS: int = _env_int("TRIDENT_DEMO_HISTORY_DAYS", 1)
DEMO_DECIMALS: int = _env_int("TRIDENT_DEMO_DECIMALS", 2)
DEMO_DAILY_TOKEN_BUDGET: int = _env_int("TRIDENT_DEMO_DAILY_BUDGET", 5000)
TRIDENT_LICENSE_FILE: str = _env_str("TRIDENT_LICENSE_FILE", "license.lic")
TRIDENT_LICENSE_SECRET: str = _env_str("TRIDENT_LICENSE_SECRET", "")

IS_FULL = TRIDENT_MODE == "full"
IS_DEMO = TRIDENT_MODE == "demo"
IS_LICENSED = TRIDENT_MODE == "licensed"


# ─────────────────────────────────────────────────────────────────────────────
# 安全不变量：full 模式仅本机推荐
# ─────────────────────────────────────────────────────────────────────────────
def assert_full_guard_localhost(host: str) -> None:
    """api_server 启动时调用一次：full 模式下若 host 非本地则 WARNING。

    这是 fail-open WARN：full 算法 + 公网 = 全量 IP 暴露。
    """
    if not IS_FULL:
        return
    h = host.strip().lower()
    if h in ("0.0.0.0", ""):
        logger.warning(
            "TRIDENT_MODE=full 但 host=%r — 容器化/Docker 部署请确认网络隔离，"
            "否则算法将对外暴露", host,
        )
    elif h in ("127.0.0.1", "::1", "localhost"):
        logger.info("✓ full mode 绑定本地端口（%s），源码风险受控", host)
    else:
        logger.warning("TRIDENT_MODE=full, host=%r — 请确认仅内网可达", host)


# ─────────────────────────────────────────────────────────────────────────────
# 限流（按 IP + 按 token 的双桶，60s 滑动窗口 + 每日配额）
# ─────────────────────────────────────────────────────────────────────────────

_RATE_BUCKET_IP: Dict[str, List[float]] = {}
_RATE_BUCKET_TOKEN: Dict[str, List[float]] = {}
_DAILY_COUNTER: Dict[str, int] = {}
_DAILY_RESET_AT: float = time.time() + 86400


def _now() -> float:
    return time.time()


def _bucket_count_and_push(key: str, store: Dict[str, List[float]],
                            limit: int, window: float = 60.0) -> bool:
    """滑动窗口计数；返回 True 表示允许（同时记录本次）。"""
    now = _now()
    bucket = store.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def _daily_count_and_push(key: str, daily_budget: int) -> bool:
    """每日配额（UTC 自然日 86400s）。返回 True 表示允许。"""
    global _DAILY_RESET_AT
    now = _now()
    if now > _DAILY_RESET_AT:
        _DAILY_COUNTER.clear()
        _DAILY_RESET_AT = now + 86400
    used = _DAILY_COUNTER.get(key, 0)
    if used >= daily_budget:
        return False
    _DAILY_COUNTER[key] = used + 1
    return True


def check_rate_limit(client_ip: str, api_key_id: Optional[str] = None) -> tuple[bool, str]:
    """demo/licensed 模式下的限流入口。返回 (allowed, reason)。"""
    if IS_FULL:
        return True, "full"
    if not _bucket_count_and_push(f"ip:{client_ip}", _RATE_BUCKET_IP, DEMO_RPM):
        return False, f"rate_limit_ip({DEMO_RPM}/min)"
    bucket_key = f"token:{api_key_id}" if api_key_id else f"ip:{client_ip}"
    if not _daily_count_and_push(bucket_key, DEMO_DAILY_TOKEN_BUDGET):
        return False, f"daily_budget({DEMO_DAILY_TOKEN_BUDGET}/day)"
    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# 延迟注入（demo 模式）
# ─────────────────────────────────────────────────────────────────────────────

async def inject_delay() -> None:
    """demo 模式下每个响应前调用一次。"""
    if not IS_DEMO or (DEMO_DELAY_MS == 0 and DEMO_JITTER_MS == 0):
        return
    base = DEMO_DELAY_MS / 1000.0
    jitter = secrets.randbelow(max(DEMO_JITTER_MS, 1)) / 1000.0
    await asyncio.sleep(base + jitter)


# ─────────────────────────────────────────────────────────────────────────────
# 数值脱敏（demo 模式）— 核心：浮点截位，dict/list 递归
# ─────────────────────────────────────────────────────────────────────────────

_SKIP_KEYS: List[str] = []


def configure_skip_keys(extra: List[str]) -> None:
    """允许业务方登记不需要脱敏的 key（id / 时间戳 / enum 等）。"""
    global _SKIP_KEYS
    _SKIP_KEYS = list(set(_SKIP_KEYS) | set(extra))


def sanitize_number(x: Any) -> Any:
    """demo 模式：float → 截 ``DEMO_DECIMALS`` 位小数；dict/list → 递归。

    防御目的：用户拿到的是"够用但不精确"的数值，无法反推阈值/权重。
    例：R1=4236.9234567 → 4236.92（demo）/ 4236.9234567（full）
    """
    if not IS_DEMO:
        return x
    if isinstance(x, bool):
        return x
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return x
        return round(x, DEMO_DECIMALS)
    if isinstance(x, dict):
        return {
            k: (x[k] if k in _SKIP_KEYS else sanitize_number(v))
            for k, v in x.items()
        }
    if isinstance(x, (list, tuple)):
        cleaned = [sanitize_number(v) for v in x]
        return type(x)(cleaned) if isinstance(x, tuple) else cleaned
    return x


def trim_history(rows: List[dict], ts_field: str = "ts") -> List[dict]:
    """demo 模式：截断 ``DEMO_HISTORY_DAYS`` 之外的数据点。"""
    if not IS_DEMO or DEMO_HISTORY_DAYS <= 0:
        return rows
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEMO_HISTORY_DAYS)
    kept = []
    for r in rows:
        ts = r.get(ts_field)
        if ts is None:
            kept.append(r)
            continue
        try:
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts / 1000.0 if ts > 1e12 else float(ts),
                                            tz=timezone.utc)
            else:
                ts_s = str(ts).replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts_s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                kept.append(r)
        except Exception:
            kept.append(r)
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# 水印：每个 demo/licensed 响应带 X-Trident-Watermark 头（事后追溯用）
# ─────────────────────────────────────────────────────────────────────────────

def make_watermark(client_ip: str, api_key_id: Optional[str]) -> str:
    """生成一个简短的水印 token（base64url, 24 字符）。"""
    import base64
    raw = secrets.token_bytes(8)
    ip_hash = hashlib.sha256(client_ip.encode()).digest()[:4]
    key_hash = hashlib.sha256((api_key_id or "").encode()).digest()[:4]
    payload = raw + ip_hash + key_hash
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


# ─────────────────────────────────────────────────────────────────────────────
# 路由装饰器（业务侧可选使用）
# ─────────────────────────────────────────────────────────────────────────────

def demo_guarded(func: Callable) -> Callable:
    """装饰器：自动应用延迟 + 数值脱敏。

    用法::

            @app.get("/api/foo")
            @demo_guarded
            async def foo():
                return {"r1": 4236.9234567}
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        await inject_delay()
        result = await func(*args, **kwargs)
        return sanitize_number(result)

    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# 启动横幅
# ─────────────────────────────────────────────────────────────────────────────

def print_startup_banner() -> None:
    if IS_DEMO:
        mode_str = "🟡 DEMO（公开体验版 · 算法保留 · 数据脱敏 · 限流）"
    elif IS_LICENSED:
        mode_str = "🟠 LICENSED（受邀用户 · License Key 验证）"
    elif IS_FULL:
        mode_str = "🟢 FULL（开发者自用 · 无鉴权 · 请勿对外暴露）"
    else:
        mode_str = f"⚪ {TRIDENT_MODE!r}（未识别模式，按 full 处理）"
    logger.info("=" * 64)
    logger.info("Trident 模式：%s", mode_str)
    if IS_DEMO:
        logger.info("  限流=%d req/min  延迟=%d±%d ms  历史=%d 天  精度=%d 位",
                    DEMO_RPM, DEMO_DELAY_MS, DEMO_JITTER_MS, DEMO_HISTORY_DAYS,
                    DEMO_DECIMALS)
        logger.info("  每日配额=%d  License 文件=%s",
                    DEMO_DAILY_TOKEN_BUDGET, TRIDENT_LICENSE_FILE)
    logger.info("=" * 64)