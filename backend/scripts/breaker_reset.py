#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""breaker_reset —— 复位熔断器（含落盘状态）。

为什么需要它（2026-11-16 实测）：
    门卫通道曾因一次瞬时 401 被熔断；而 key 其实早就恢复了（直接打同一个
    endpoint 返回 200）。但熔断状态**落盘**在 runtime/breakers/ 里，
    即使代码里的冷却结束，人工也只能靠重启进程才清得掉。
    这条命令就是"验证过通道确实活着之后，把那条 stale 熔断清掉"的标准动作。

用法：
    python3 backend/scripts/breaker_reset.py --list
    python3 backend/scripts/breaker_reset.py --name llm_auth:minimax-gate
    python3 backend/scripts/breaker_reset.py --all --yes

约定：不带 --yes 只预演，不做修改。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src_python"))


def main() -> int:
    import circuit_breaker as cb

    args = sys.argv[1:]
    if "--list" in args or not args:
        try:
            state = cb.report_all()
        except Exception as exc:
            print(f"[RESET] 读取失败: {type(exc).__name__}: {exc}")
            return 1
        if not state:
            print("[RESET] 当前进程内没有熔断器状态")
        for name, st in sorted(state.items()):
            print(f"  {name}: {st}")
        return 0

    do_all = "--all" in args
    name = ""
    for i, a in enumerate(args):
        if a == "--name" and i + 1 < len(args):
            name = args[i + 1]
    if not do_all and not name:
        print("[RESET] 需要 --name <熔断名> 或 --all")
        return 2

    target = "全部熔断器" if do_all else name
    if "--yes" not in args:
        print(f"[RESET] 预演：将复位 {target}（加 --yes 才真正执行）")
        return 0

    try:
        cb.reset(None if do_all else name)
    except Exception as exc:
        print(f"[RESET] 复位失败: {type(exc).__name__}: {exc}")
        return 1
    print(f"[RESET] 已复位 {target}（内存 + 落盘状态均已清）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
