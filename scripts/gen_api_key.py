#!/usr/bin/env python3
"""
Trident v300 — API Key 生成 / 撤销工具
=======================================

用法::

    # 给朋友开一个 key，绑定到 email
    python scripts/gen_api_key.py --name "李雷的朋友" --email li@x.com

    # 撤销一个 key
    python scripts/gen_api_key.py --revoke <token>

存储位置：``backend/src_python/.trident_keys.json``（JSON，gitignored）。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timezone


KEY_STORE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backend", "src_python", ".trident_keys.json",
)


def _load() -> dict:
    if not os.path.exists(KEY_STORE):
        return {}
    with open(KEY_STORE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(KEY_STORE), exist_ok=True)
    with open(KEY_STORE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def make_token() -> str:
    """生成 43 字符 URL-safe token（256 bit）。"""
    return "trk_" + secrets.token_urlsafe(32)


def cmd_create(args):
    store = _load()
    token = make_token()
    store[token] = {
        "name": args.name,
        "email": args.email or "",
        "tier": args.tier,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "revoked": False,
        "daily_budget": args.budget,
    }
    _save(store)
    print(f"✓ API Key 已创建（{args.name}）", file=sys.stderr)
    print(f"  token       : {token}", file=sys.stderr)
    print(f"  tier        : {args.tier}", file=sys.stderr)
    print(f"  daily_budget: {args.budget}", file=sys.stderr)
    print(f"  store       : {KEY_STORE}", file=sys.stderr)
    print(file=sys.stderr)
    print("# 客户端使用：", file=sys.stderr)
    print(f'curl -H "X-Trident-API-Key: {token}" http://localhost:8000/api/health', file=sys.stderr)


def cmd_revoke(args):
    store = _load()
    if args.revoke not in store:
        print(f"✗ token 不存在: {args.revoke[:20]}…", file=sys.stderr)
        sys.exit(1)
    store[args.revoke]["revoked"] = True
    store[args.revoke]["revoked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save(store)
    print(f"✓ 已撤销 token: {args.revoke[:20]}…", file=sys.stderr)


def cmd_list(args):
    store = _load()
    print(f"# 当前 {len(store)} 个 API Key（store={KEY_STORE}）")
    for token, meta in store.items():
        status = "❌ revoked" if meta.get("revoked") else "✓ active"
        print(f"  [{status}] {token[:20]}…  name={meta.get('name')}  tier={meta.get('tier')}")


def main():
    p = argparse.ArgumentParser(description="管理 Trident API Keys")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("create", help="创建新 key")
    pc.add_argument("--name", required=True, help="备注名（给谁用的）")
    pc.add_argument("--email", default="", help="联系人邮箱")
    pc.add_argument("--tier", choices=["trial", "pro", "enterprise"], default="trial")
    pc.add_argument("--budget", type=int, default=5000, help="每日配额")
    pc.set_defaults(func=cmd_create)

    pr = sub.add_parser("revoke", help="撤销 key")
    pr.add_argument("revoke", help="要撤销的完整 token")
    pr.set_defaults(func=cmd_revoke)

    pl = sub.add_parser("list", help="列出所有 key")
    pl.set_defaults(func=cmd_list)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()