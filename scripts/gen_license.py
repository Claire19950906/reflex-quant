#!/usr/bin/env python3
"""
Trident v300 — License 生成工具
================================

用法::

    # 给一个朋友开 30 天 trial
    python scripts/gen_license.py --tier trial --days 30 --to friend@example.com

    # 给一台服务器发 enterprise 永久 license（绑定设备指纹）
    python scripts/gen_license.py --tier enterprise --days 3650 \\
        --to server01 --fingerprint "$(python -c 'import sys; sys.path.insert(0, \"backend/src_python\"); from license_auth import get_machine_fingerprint; print(get_machine_fingerprint())')"

    # 不绑指纹（可转移多台设备）
    python scripts/gen_license.py --tier pro --days 90 --to "anyone"

输出：
  - stdout: license.lic 文件内容（JSON，复制到目标机器即可）
  - stderr: 提示信息
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone


def make_license_key() -> str:
    """生成易读的 license key（4 段 × 4 字符）。"""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉易混淆的 0/O/1/I/L
    parts = []
    for _ in range(4):
        seg = "".join(secrets.choice(alphabet) for _ in range(4))
        parts.append(seg)
    return "tr-" + "-".join(parts)


def build_license(tier: str, days: int, issued_to: str,
                  fingerprint: str | None, features: list[str],
                  secret: str) -> dict:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=days)
    payload = {
        "license_key": make_license_key(),
        "tier": tier,
        "issued_to": issued_to,
        "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fp": fingerprint or "",
        "features": features,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    payload["sig"] = sig
    return payload


def main():
    p = argparse.ArgumentParser(description="生成 Trident license.lic")
    p.add_argument("--tier", choices=["trial", "pro", "enterprise"],
                   default="trial", help="许可证级别")
    p.add_argument("--days", type=int, default=30, help="有效期天数")
    p.add_argument("--to", default="anonymous", help="发放对象（邮箱或备注）")
    p.add_argument("--fingerprint", default=None,
                   help="设备指纹（从 get_machine_fingerprint() 拿到；不传=不绑）")
    p.add_argument("--features", nargs="*", default=["realtime"],
                   help="功能开关列表（如 realtime historical_30d）")
    p.add_argument("--secret",
                   default=os.getenv("TRIDENT_LICENSE_SECRET",
                                     "dev-secret-CHANGE-ME-IN-PROD"),
                   help="HMAC 密钥（必须和服务器 TRIDENT_LICENSE_SECRET 一致）")
    p.add_argument("--out", default="license.lic", help="输出文件路径")
    args = p.parse_args()

    lic = build_license(
        tier=args.tier, days=args.days, issued_to=args.to,
        fingerprint=args.fingerprint, features=args.features, secret=args.secret,
    )

    # 输出文件
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(lic, f, indent=2, ensure_ascii=False)

    # 控制台摘要
    print(f"✓ license 已生成 → {args.out}", file=sys.stderr)
    print(f"  license_key : {lic['license_key']}", file=sys.stderr)
    print(f"  tier        : {lic['tier']}", file=sys.stderr)
    print(f"  issued_to   : {lic['issued_to']}", file=sys.stderr)
    print(f"  expires_at  : {lic['expires_at']}", file=sys.stderr)
    print(f"  fingerprint : {lic.get('fp') or '(unbound)'}", file=sys.stderr)
    print(f"  features    : {lic['features']}", file=sys.stderr)
    print(file=sys.stderr)
    print("# 把以下内容贴到目标机器的 license.lic，或上传到服务器：", file=sys.stderr)
    print(json.dumps(lic, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()