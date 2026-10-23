#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v302+ · 紧急通道切换工具（运维用，不依赖后端重启）。

用法:
  python scripts/llm_channel_switch.py status        # 看当前两个通道的 weight
  python scripts/llm_channel_switch.py off --channel main      # 关主通道
  python scripts/llm_channel_switch.py on  --channel main      # 恢复主通道
  python scripts/llm_channel_switch.py set --channel main --weight 3

设计:
  - 改 .env 中的 MINIMAX_MAIN_WEIGHT / MINIMAX2_WEIGHT
  - 不重启后端:ai_worker 每批调用 _build_model_pool() 会重新读 config,
    config.py 顶层 os.getenv 在 import 时读一次 → 实际需 uvicorn 优雅重启
    或给 api_server.py 加 SIGHUP handler。短期推荐走 launchd restart。

退出码:
  0 成功 / 1 参数错 / 2 文件未找到 / 3 写入失败
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ENV_FILE = BACKEND / ".env"


def _read_env() -> dict[str, str]:
    """读 .env 成 dict(忽略注释/空行,保留其他行原值)。"""
    if not ENV_FILE.exists():
        raise FileNotFoundError(f".env not found at {ENV_FILE}")
    out: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v
    return out


def _atomic_write(text: str) -> None:
    """原子写：先写临时文件,再 rename,避免半写状态。"""
    tmp = ENV_FILE.with_suffix(".env.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(ENV_FILE)
    except OSError as exc:
        raise RuntimeError(f"write {ENV_FILE} failed: {exc}") from exc


def cmd_status() -> int:
    env = _read_env()
    main_w = env.get("MINIMAX_MAIN_WEIGHT", "2")
    main2_w = env.get("MINIMAX2_WEIGHT", "4")
    main_k = (env.get("MINIMAX_API_KEY") or "")[:14]
    main2_k = (env.get("MINIMAX2_API_KEY") or "")[:14]
    print("=== LLM channel weight status ===")
    print(f"  main   (minimax/MiniMax-M3)  weight={main_w:>2}   key={main_k}…")
    print(f"  #2     (minimax2/MiniMax-M3) weight={main2_w:>2}   key={main2_k}…")
    if main_w == "0":
        print("  ⚠️  main channel is DISABLED (weight=0)")
    if main2_w == "0":
        print("  ⚠️  #2 channel is DISABLED (weight=0)")
    if main_w == "0" and main2_w == "0":
        print("  ❌ BOTH channels disabled → ai_worker will fall back to local Ollama")
    return 0


def cmd_set(channel: str, weight: int) -> int:
    if channel not in {"main", "main2"}:
        print(f"❌ channel must be 'main' or 'main2', got: {channel!r}", file=sys.stderr)
        return 1
    if weight < 0 or weight > 20:
        print(f"❌ weight must be in [0, 20], got: {weight}", file=sys.stderr)
        return 1
    key = "MINIMAX_MAIN_WEIGHT" if channel == "main" else "MINIMAX2_WEIGHT"
    text = ENV_FILE.read_text(encoding="utf-8")

    pattern = re.compile(rf"^{re.escape(key)}\s*=\s*.*$", re.MULTILINE)
    new_line = f"{key}={weight}"

    if pattern.search(text):
        text2 = pattern.sub(new_line, text)
        action = "updated"
    else:
        # 没设过 → 在 MINIMAX_USE_PROXY 段后追加
        marker = "MINIMAX_USE_PROXY=false"
        if marker in text:
            text2 = text.replace(marker, marker + f"\n{new_line}", 1)
            action = "appended"
        else:
            text2 = text + f"\n{new_line}\n"
            action = "appended (at EOF)"

    try:
        _atomic_write(text2)
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 3
    print(f"✅ {key}={weight} ({action})")
    print("   next AI batch will rebuild pool with new weight.")
    print("   if backend is still routing to old channel, do:")
    print("     launchctl kickstart -k gui/$(id -u)/com.trident.backend  # macOS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="trident LLM channel weight switcher")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show current channel weights")

    p_off = sub.add_parser("off", help="disable a channel (set weight=0)")
    p_off.add_argument("--channel", choices=["main", "main2"], required=True)

    p_on = sub.add_parser("on", help="enable a channel (set weight=2 or 4)")
    p_on.add_argument("--channel", choices=["main", "main2"], required=True)

    p_set = sub.add_parser("set", help="set explicit weight")
    p_set.add_argument("--channel", choices=["main", "main2"], required=True)
    p_set.add_argument("--weight", type=int, required=True)

    args = ap.parse_args()
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "off":
        return cmd_set(args.channel, 0)
    if args.cmd == "on":
        return cmd_set(args.channel, 2 if args.channel == "main" else 4)
    if args.cmd == "set":
        return cmd_set(args.channel, args.weight)
    return 1


if __name__ == "__main__":
    sys.exit(main())
