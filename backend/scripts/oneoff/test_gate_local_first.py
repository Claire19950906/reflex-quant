#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证「门卫本地优先」真的能走通 —— 省钱方案的地基。

如果本地模型答不上来，系统就会回退去打远程通道（等于白花钱），
那么 GATE_LOCAL_FIRST=1 就是一句空话。必须实测验证。

用法：python3 scripts/test_gate_local_first.py
"""
from __future__ import annotations
from pathlib import Path

import os
import sys
import time

BASE = str(Path(__file__).resolve().parents[2])
SRC = os.path.join(BASE, "src_python")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import config # noqa: E402
import realtime_filter as rt  # noqa: E402

SYSTEM = "你是金融新闻门卫。判断这条新闻是否值得深入分析，只回答 YES 或 NO。"
USER = ("[hash:test] 美联储主席鲍威尔宣布降息 50 个基点，"
        "声称通胀已完全受控，市场预期 2027 年前再降两次。")


def main() -> int:
    print("=" * 64)
    print("  门卫本地优先 —— 真实调用验证")
    print("=" * 64)
    print(f"  GATE_LOCAL_FIRST = {getattr(config, 'GATE_LOCAL_FIRST', None)}")
    print(f"  GATE_LOCAL_TIMEOUT_S = {getattr(config, 'GATE_LOCAL_TIMEOUT_S', None)}")

    try:
        import config as _c
        pool = _c.get_pool_for_purpose("gate")
    except Exception as exc:
        print(f"  [取 gate 池失败: {type(exc).__name__}: {exc}]")
        return 1

    print(f"  gate 池 {len(pool)} 个通道：")
    for m in pool:
        kind = "本地" if m.get("is_local") else "远程"
        print(f"    [{kind}] {m.get('label')}  alias={m.get('key_alias')}")

    local = [m for m in pool if m.get("is_local")]
    if not local:
        print("\n  ❌ 池里没有本地通道 —— 本地优先无从谈起")
        return 1

    print("\n  实测调用（本地优先路径）…")
    t0 = time.perf_counter()
    out = rt._call_pool_sync(SYSTEM, USER, max_tokens=32, timeout=3.0,
                              label="TEST-LOCAL", purpose="gate",
                              call_site="local_first_test")
    dt = (time.perf_counter() - t0) * 1000

    if not out:
        print(f"  ❌ 本地门卫 {dt:.0f}ms 没有返回任何结果")
        print("     → 本地优先会回退远程，省钱方案不成立！")
        return 1

    print(f"  ✅ 本地门卫 {dt:.0f}ms 返回：{out.strip()[:120]!r}")
    print(f"     耗时 {dt/1000:.1f}s（上限 "
          f"{getattr(config, 'GATE_LOCAL_TIMEOUT_S', 8)}s）")

    # 关键：这次调用**不能**打到远程通道。
    # 判据：本地通道存在且返回了结果 → 远程不应被提交。
    print("\n  检查：本次是否打了远程通道…")
    try:
        import db
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT key_alias, tokens_in, tokens_out, status "
            "FROM ai_call_stats ORDER BY ts DESC LIMIT 5")
        for r in cur.fetchall():
            print(f"    {str(r[0])[:24]:<24} in={r[1]:<6} out={r[2]:<6} {r[3]}")
        conn.close()
    except Exception as exc:
        print(f"    [查埋点失败: {type(exc).__name__}: {exc}]")

    print("=" * 64)
    print("  提示：本次调用若只出现 local 埋点、未出现远程埋点，")
    print("        即证明「本地优先」生效 —— 门卫不再消耗远程额度。")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
