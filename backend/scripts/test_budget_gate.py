#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""额度保险丝自测：验证三道闸门 + 急停开关真的能拦住。

v339: 本文件是**可执行脚本**，但文件名以 `test_` 开头，pytest 收集时会 import
整个模块 → 模块级代码直接跑起来 → 末尾 `raise SystemExit(1)` 让 pytest 报
`INTERNALERROR`。我把执行体收进 `run_all()`，只有 `__main__` 时才跑，
pytest 收集到就正常跳过（0 tests），不用再加 `--ignore=scripts`。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src_python"))

FAILS = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAILS.append(name)


def run_all() -> int:
    """返回退出码：0 全过 / 1 有失败。"""
    import config
    import llm_budget

    FAILS.clear()

    print("── 1. 默认不限时，必须放行 ──")
    config.LLM_DAILY_TOKEN_BUDGET = 0
    config.LLM_DAILY_REQUEST_BUDGET = 0
    config.LLM_DAILY_NEWS_BUDGET = 0
    config.LLM_GLOBAL_KILL_SWITCH = False
    ok, why = llm_budget.check(1000)
    check("不限额度时放行", ok, f"reason={why}")

    print("── 2. 急停开关 ──")
    config.LLM_GLOBAL_KILL_SWITCH = True
    ok, why = llm_budget.check(0)
    check("急停开关拦截", (not ok) and "急停" in why, f"reason={why}")
    config.LLM_GLOBAL_KILL_SWITCH = False

    print("── 3. 请求数预算 ──")
    u = llm_budget.usage(refresh=True)
    config.LLM_DAILY_REQUEST_BUDGET = u["requests"]
    ok, why = llm_budget.check(0)
    check("请求数达上限时拦截", not ok, f"reason={why}")
    config.LLM_DAILY_REQUEST_BUDGET = 0

    print("── 4. token 预算 ──")
    u = llm_budget.usage(refresh=True)
    config.LLM_DAILY_TOKEN_BUDGET = u["tokens"]
    ok, why = llm_budget.check(0)
    check("token 达上限时拦截", not ok, f"reason={why}")
    # 剩余不足本次调用
    config.LLM_DAILY_TOKEN_BUDGET = u["tokens"] + 10
    ok, why = llm_budget.check(999999)
    check("剩余不足本次调用时拦截", not ok, f"reason={why}")
    config.LLM_DAILY_TOKEN_BUDGET = 0

    print("── 5. 信号条数预算 ──")
    u = llm_budget.usage(refresh=True)
    config.LLM_DAILY_NEWS_BUDGET = u["news"]
    ok, why = llm_budget.check(0)
    check("信号条数达上限时拦截", not ok, f"reason={why}")
    config.LLM_DAILY_NEWS_BUDGET = 0

    print("── 6. 单条 token 估算 ──")
    est = llm_budget.estimate_call_tokens("x" * 22000, 4000)
    check("估算合理（22k 字符 + 4k 输出 ≈ 14k）", 12000 <= est <= 16000, f"est={est}")

    print("── 7. 熔断参数 ──")
    from engine import ai_worker
    check("配额熔断阈值=1（429 一次就熔断）",
          ai_worker._ANALYSIS_QUOTA_FAIL_THRESHOLD == 1,
          f"={ai_worker._ANALYSIS_QUOTA_FAIL_THRESHOLD}")
    check("鉴权熔断冷却 24h（key 失效不自动探测）",
          ai_worker._ANALYSIS_AUTH_COOLDOWN_S >= 86400,
          f"={ai_worker._ANALYSIS_AUTH_COOLDOWN_S}")
    check("配额熔断冷却 >=30min",
          ai_worker._ANALYSIS_QUOTA_COOLDOWN_LONG_S >= 1800,
          f"={ai_worker._ANALYSIS_QUOTA_COOLDOWN_LONG_S}")
    brk = ai_worker._analysis_breaker("test_alias_x", "auth")
    check("auth 熔断器可创建", brk is not None)
    brk2 = ai_worker._analysis_breaker("test_alias_x", "quota")
    check("quota 熔断器与 auth 是不同实例",
          brk is not None and brk2 is not None and brk is not brk2)

    print("── 8. 账本不可读时 fail-closed ──")
    _orig_query = llm_budget._query
    llm_budget._query = lambda sql, params=(): None      # 模拟 DB 不可用
    llm_budget._CACHE["ts"] = 0.0
    llm_budget._CACHE["data"] = None
    config.LLM_BUDGET_FAIL_CLOSED = True
    ok, why = llm_budget.check(0)
    check("账本读不到时拦截（不放行烧钱）", not ok, f"reason={why}")
    config.LLM_BUDGET_FAIL_CLOSED = False
    ok, why = llm_budget.check(0)
    check("fail_closed=0 时可放开", ok, f"reason={why}")
    config.LLM_BUDGET_FAIL_CLOSED = True
    llm_budget._query = _orig_query
    llm_budget._CACHE["ts"] = 0.0
    llm_budget._CACHE["data"] = None

    print("── 9. 重启基线（bootstrap 灌入当天用量）──")
    llm_budget._INPROC["requests"] = 0
    llm_budget._INPROC["tokens"] = 0
    llm_budget.bootstrap()
    check("bootstrap 后进程内计数 >= 当天真实用量",
          llm_budget._INPROC["requests"] >= llm_budget.usage()["requests"],
          f"inproc={llm_budget._INPROC['requests']}")

    print("── 10. DB 失明时兜底值不是 0 ──")
    llm_budget._query = lambda sql, params=(): None
    llm_budget._CACHE["ts"] = 0.0
    llm_budget._CACHE["data"] = None
    u_blind = llm_budget.usage()
    check("DB 失明时 requests 回退到进程内基线（不是 0）",
          u_blind["requests"] > 0, f"requests={u_blind['requests']}")
    llm_budget._query = _orig_query
    llm_budget._CACHE["ts"] = 0.0
    llm_budget._CACHE["data"] = None

    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过: {FAILS}")
        return 1
    print("✅ 额度保险丝全部自测通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_all())
