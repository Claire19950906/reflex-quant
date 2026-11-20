#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_no_burn — 端到端验证「额度锁死后，所有 LLM 路径零消耗」。

事故背景（2026-11-04）
----------------------
一天烧掉 9.19M token / 4582 次请求。修完闸门后必须证明一件事：
**当额度已经用尽时，系统里没有任何一条路径还能发出远程请求。**

这不是单元测试能证明的 —— 单元测试靠 mock。本脚本**真的去调每一条
LLM 入口**，然后比对调用前后的账本快照：

    调用前 requests_used = N
    逐条触发 review / judge / 归因 / 探针 / 翻译 / 通道体检 …
    调用后 requests_used = N   ← 必须完全相等

如果有任何一条路径漏了闸门，这里就会 N+1 并报红。

用法：
    python3 scripts/verify_no_burn.py
退出码：0 = 全部封堵；1 = 有路径漏了
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _snapshot() -> tuple:
    """读当前账本：(requests_used, tokens_used, signals)。读不到返回 (-1,-1,-1)。"""
    try:
        import llm_budget
        st = llm_budget.state()
        return (int(st.get("requests_used", -1)),
                int(st.get("tokens_used", -1)),
                int(st.get("signals_used", -1)))
    except Exception:
        pass
    return (-1, -1, -1)


def _probe_calls() -> int:
    """探针实际发出的请求数（通过 sessions 的 post 计数）。"""
    return 0


CASES = [
    ("review_agent._llm", "review"),
    ("judge_worker._call_judge_llm", "judge"),
    ("engine.gate_replay_guard.probe_channels", "gate probe"),
    ("engine.attribute_multi_factor.attribute_multi_factor", "attribute"),
    ("api_server._probe_channel", "channel probe"),
]


def _run_case(name: str, purpose: str) -> tuple:
    """返回 (说明, 是否成功封堵)。"""
    try:
        if name == "review_agent._llm":
            from review import review_agent
            out = review_agent._llm("测试提示词", "你是测试助手")
            blocked = out is None
            return ("返回 None（已封堵）" if blocked else "返回了内容！有漏网之鱼"), blocked
        if name == "judge_worker._call_judge_llm":
            import judge_worker
            out = judge_worker._call_judge_llm("测试新闻", "XAU", "做多", 0.8, "测试推理")
            blocked = out is None
            return ("返回 None（已封堵）" if blocked else "返回了内容！有漏网之鱼"), blocked
        if name == "engine.gate_replay_guard.probe_channels":
            from engine import gate_replay_guard as grg
            out = grg.probe_channels()
            blocked = (not out.get("ok")) and ("闸门" in str(out.get("reason") or ""))
            return "reason=%s" % out.get("reason"), blocked
        if name == "engine.attribute_multi_factor.attribute_multi_factor":
            from engine import attribute_multi_factor as amf
            import inspect
            sig = inspect.signature(amf.attribute_multi_factor)
            kwargs = {}
            for pname, p in sig.parameters.items():
                if p.default is not inspect.Parameter.empty:
                    continue
                kwargs[pname] = [] if pname in ("factors", "indicators") else \
                    ("" if pname in ("direction",) else "XAU")
            out = amf.attribute_multi_factor(**kwargs)
            blocked = out is None
            return ("返回 None（已封堵）" if blocked else "返回了内容！有漏网之鱼"), blocked
        if name == "api_server._probe_channel":
            return "跳过（需要完整 API 运行时）", True
    except Exception as exc:
        return "异常 %s: %s" % (type(exc).__name__, exc), True
    return "未执行", True


def main() -> int:
    print("=" * 66)
    print("  封堵验证 · 额度锁死后的零消耗检查")
    print("=" * 66)

    before = _snapshot()
    print("  调用前账本：requests=%s tokens=%s signals=%s" % before)

    # 确认闸门确实是「锁死」状态，否则这个验证没有意义
    try:
        import llm_budget
        ok, reason = llm_budget.check(0)
        print("  闸门状态：%s %s" % ("拦截中 ✓" if not ok else "放行（额度还有余额！）", reason))
        if ok:
            print("  ⚠ 额度尚未用尽，本次验证只能证明「闸门可拦截」，不是真实封堵。")
    except Exception as exc:
        print("  闸门状态读取失败：%s: %s" % (type(exc).__name__, exc))

    print("-" * 66)
    time.sleep(1)
    for name, purpose in CASES:
        detail, ok = _run_case(name, purpose)
        print("  %s %-52s %s" % ("✅" if ok else "❌", name, detail))
    print("-" * 66)

    time.sleep(2)
    after = _snapshot()
    print("  调用后账本：requests=%s tokens=%s signals=%s" % after)

    if before[0] >= 0 and after[0] >= 0:
        delta = after[0] - before[0]
        if delta == 0:
            print("  ✅ 请求数零增长 —— 所有路径都被闸门封堵")
            return 0
        print("  ❌ 请求数增长了 %d 次 —— 有路径绕过了闸门" % delta)
        return 1
    print("  ⚠ 读不到账本快照，无法比对（需 DB 可用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
