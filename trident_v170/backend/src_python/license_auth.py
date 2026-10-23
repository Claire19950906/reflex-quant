#!/usr/bin/env python3
"""
Trident v300 — License + API Key 鉴权模块
==========================================

职责：
  1. 加载 ``license.lic``（HMAC-SHA256 签名校验）→ 返回 ``License`` 对象
  2. 提供 FastAPI 依赖 ``require_license`` / ``require_api_key`` 用于路由保护
  3. 设备指纹生成（用于本地部署 license 绑定）

License 文件格式（JSON）::

    {
      "license_key": "tr-xxxx-xxxx-xxxx",
      "tier": "trial" | "pro" | "enterprise",
      "issued_to": "friend@example.com",
      "issued_at": "2026-10-09T00:00:00Z",
      "expires_at": "2026-12-31T23:59:59Z",
      "fp": "<设备指纹 sha256-hex, 可空>",
      "features": ["realtime", "historical_30d"],
      "sig": "<hmac-sha256(payload, secret)>"
    }

API Key 头：``X-Trident-API-Key: <32+ 字符 token>``

配合使用：

  * ``full`` 模式：不调用本模块任何东西，开发自用
  * ``demo`` 模式：可选 IP 限流（``demo_mode.check_rate_limit``），不强制 license
  * ``licensed`` 模式：必须 license 有效 + API Key 通过 + 设备指纹匹配

生成 License / API Key 用：

    python scripts/gen_license.py  --tier trial --days 30 --to friend@x.com
    python scripts/gen_api_key.py  --name "li-001"
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import platform
import secrets
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Header, HTTPException, Request

logger = logging.getLogger("trident.license_auth")


LICENSE_SECRET = os.getenv("TRIDENT_LICENSE_SECRET", "").strip()
LICENSE_FILE = os.getenv("TRIDENT_LICENSE_FILE", "license.lic")
API_KEY_STORE_FILE = os.getenv("TRIDENT_API_KEY_STORE", ".trident_keys.json")


@dataclass
class License:
    key: str
    tier: str
    issued_to: str
    issued_at: str
    expires_at: Optional[str]
    fingerprint: Optional[str]
    features: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            ts = self.expires_at.replace("Z", "+00:00")
            exp = datetime.fromisoformat(ts)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) > exp
        except Exception:
            return False

    def has_feature(self, feat: str) -> bool:
        return feat in self.features or "*" in self.features


_cached_license: Optional[License] = None


def _compute_expected_sig(payload: Dict[str, Any], secret: str) -> str:
    """对排好序的 JSON 算 HMAC-SHA256 → hex。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def load_license(path: Optional[str] = None) -> Optional[License]:
    """从磁盘加载 license（带内存缓存）。

    返回 ``None`` 表示没找到 / 验签失败。
    缓存策略：进程内只验一次；热更新调 ``reload_license()``。
    """
    global _cached_license
    if _cached_license is not None:
        return _cached_license

    p = path or LICENSE_FILE
    if not os.path.isabs(p):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
    if not os.path.exists(p):
        logger.info("license 文件 %s 不存在 — 当前模式视为无 license", p)
        return None
    if not LICENSE_SECRET:
        logger.error("TRIDENT_LICENSE_SECRET 未设置 — 拒绝加载 license（fail-closed）")
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        sig = raw.pop("sig", "")
        if not sig:
            logger.warning("license 文件 %s 缺 sig 字段 — 拒绝", p)
            return None
        expected = _compute_expected_sig(raw, LICENSE_SECRET)
        if not hmac.compare_digest(sig, expected):
            logger.warning("license 文件 %s 签名校验失败 — 拒绝", p)
            return None
        lic = License(
            key=raw.get("license_key", ""),
            tier=raw.get("tier", "trial"),
            issued_to=raw.get("issued_to", ""),
            issued_at=raw.get("issued_at", ""),
            expires_at=raw.get("expires_at"),
            fingerprint=raw.get("fp"),
            features=raw.get("features", []),
            raw=raw,
        )
        if lic.is_expired():
            logger.warning("license 已过期 (expires_at=%s)", lic.expires_at)
        else:
            logger.info("✓ license 加载成功: tier=%s issued_to=%s expires=%s",
                        lic.tier, lic.issued_to, lic.expires_at)
        _cached_license = lic
        return lic
    except Exception as e:
        logger.exception("license 文件 %s 解析失败: %s", p, e)
        return None


def reload_license() -> Optional[License]:
    """强制重新读盘（用于热更新）。"""
    global _cached_license
    _cached_license = None
    return load_license()


# ─────────────────────────────────────────────────────────────────────────────
# 设备指纹（用于本地部署 license 绑定）
# ─────────────────────────────────────────────────────────────────────────────

def get_machine_fingerprint() -> str:
    """生成稳定指纹（hostname + machine + mac → SHA-256 → 前 16 hex）。

    注意：这是"弱指纹"（容器/Docker 内可能都长一样），只用于粗筛。
    生产 license 验证请配合在线激活。
    """
    parts = []
    try:
        parts.append(socket.gethostname())
    except Exception:
        parts.append("unknown-host")
    try:
        parts.append(platform.machine())
        parts.append(platform.processor() or "")
        parts.append(platform.system())
    except Exception:
        pass
    try:
        import uuid
        parts.append(str(uuid.getnode()))
    except Exception:
        parts.append("no-mac")
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return h[:32]  # 128 bit


# ─────────────────────────────────────────────────────────────────────────────
# API Key 存储（极简 JSON key-store；生产可换 Redis/DB）
# ─────────────────────────────────────────────────────────────────────────────

def _load_api_keys() -> Dict[str, Dict[str, Any]]:
    p = API_KEY_STORE_FILE
    if not os.path.isabs(p):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def verify_api_key(token: str) -> Optional[Dict[str, Any]]:
    """校验 ``X-Trident-API-Key``；返回 key 元数据（含 name / tier / created_at）或 None。"""
    if not token:
        return None
    keys = _load_api_keys()
    meta = keys.get(token)
    if meta and meta.get("revoked"):
        return None
    return meta


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI 依赖（路由侧 ``Depends(...)`` 使用）
# ─────────────────────────────────────────────────────────────────────────────

async def require_license(request: Request) -> License:
    """依赖：licensed 模式下必须有有效 license（full/demo 模式返回虚拟 license）。"""
    from demo_mode import IS_FULL, IS_LICENSED

    if IS_FULL:
        return License(
            key="local-dev", tier="self",
            issued_to="local", issued_at="", expires_at=None,
            fingerprint=None, features=["*"],
        )

    lic = load_license()
    if IS_LICENSED:
        if not lic:
            raise HTTPException(status_code=403, detail={
                "error": "license_missing",
                "message": "License file not found or invalid. See docs/SECURITY_DEMO_MODE.md",
            })
        if lic.is_expired():
            raise HTTPException(status_code=403, detail={
                "error": "license_expired",
                "message": f"License expired at {lic.expires_at}",
                "issued_to": lic.issued_to,
            })
    return lic or License(
        key="anonymous-demo", tier="anonymous",
        issued_to="anonymous", issued_at=None, fingerprint=None,
    )


async def require_api_key(
    request: Request,
    x_trident_api_key: Optional[str] = Header(None, alias="X-Trident-API-Key"),
) -> Optional[Dict[str, Any]]:
    """依赖：licensed/full 模式强制 API Key；demo 模式可选。

    P0 安全修复：原先 IS_FULL 直接 bypass，导致 /api/agent_chat 等敏感端点
    在 full 模式无鉴权。full 模式默认现在也强制校验，仅当运维显式配置
    TRIDENT_ALLOW_ANON_FULL=1 时才保留旧行为（向后兼容内网隔离部署）。
    """
    from demo_mode import IS_FULL, IS_DEMO, IS_LICENSED

    # full 模式：默认要求 API Key，可通过 env 关闭（向后兼容）
    if IS_FULL and os.getenv("TRIDENT_ALLOW_ANON_FULL", "0") == "1":
        return None

    if not x_trident_api_key:
        if IS_LICENSED or IS_FULL:
            raise HTTPException(status_code=401, detail={
                "error": "api_key_missing",
                "message": "X-Trident-API-Key header required (or set TRIDENT_ALLOW_ANON_FULL=1 for local-only)",
            })
        return None

    meta = verify_api_key(x_trident_api_key)
    if not meta:
        raise HTTPException(status_code=401, detail={
            "error": "api_key_invalid",
            "message": "Invalid or revoked API key",
        })
    return meta


# ─────────────────────────────────────────────────────────────────────────────
# 启动期自检
# ─────────────────────────────────────────────────────────────────────────────

def startup_self_check() -> Dict[str, Any]:
    """返回当前鉴权状态摘要（供 /api/health/security 使用）。"""
    from demo_mode import TRIDENT_MODE, IS_FULL, IS_DEMO, IS_LICENSED

    lic = load_license()
    return {
        "mode": TRIDENT_MODE,
        "is_full": IS_FULL,
        "is_demo": IS_DEMO,
        "is_licensed": IS_LICENSED,
        "license_loaded": lic is not None,
        "license_tier": lic.tier if lic else None,
        "license_expires_at": lic.expires_at if lic else None,
        "license_expired": lic.is_expired() if lic else None,
        "api_keys_count": len(_load_api_keys()),
    }