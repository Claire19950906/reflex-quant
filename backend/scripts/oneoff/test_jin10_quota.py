#!/usr/bin/env python3
"""jin10_mcp 配额/多 token 轮换的针对性回归测试（不触网）。

覆盖 2026-11-03 修复的三个根因：
  1. 多线程共享 _token_idx → 标记记错 token、_all_tokens_exhausted 永不生效
  2. 全局单会话 + 轮换时 _reset_session() → 打断别的线程的请求
  3. call_tool 递归轮换 → 每次调用白等 N 次 HTTP

运行：python3 test_jin10_quota.py
"""
from __future__ import annotations

import sys
import threading
import time
from typing import List

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "src_python"))
from feeds import jin10_mcp as jm

FAILS: List[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}")
        FAILS.append(label)


class FakeResp:
    def __init__(self, body: str):
        self.content = body.encode("utf-8")
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    """模拟 MCP 会话：按 token 决定返回配额错误还是正常结果。"""

    def __init__(self, idx: int, exhausted_tokens: set, calls: List[int]):
        self.idx = idx
        self.exhausted = exhausted_tokens
        self.calls = calls
        self.closed = False

    def post(self, url, json=None, timeout=None):
        self.calls.append(self.idx)
        if self.idx in self.exhausted:
            body = ('data: {"jsonrpc":"2.0","id":1,"error":{"code":-32000,'
                    '"message":"今日该工具的调用次数已达上限"}}')
        else:
            body = ('data: {"jsonrpc":"2.0","id":1,"result":{"content":'
                    '[{"type":"text","text":"{\\"ok\\":true}"}]}}')
        return FakeResp(body)


def _install(monkey_tokens: List[str], exhausted: set):
    """把 jin10_mcp 切到测试态（不触网）。"""
    jm._TOKENS = list(monkey_tokens)
    jm._sessions = {}
    jm._session_inited_at = {}
    jm._session_locks = {}
    jm._quota_exhausted_until = {}
    jm._last_error = {}
    jm._token_idx = 0
    calls: List[int] = []
    lock = threading.Lock()

    def fake_get_session(idx: int):
        # 计数只发生在 FakeSession.post（真实 HTTP 往返处），
        # _get_session 只是取会话，不算一次网络请求。
        return FakeSession(idx, exhausted, calls)

    jm._get_session = fake_get_session  # type: ignore[assignment]
    return calls

def t1_single_token_quota_short_circuit() -> None:
    print("\n[1] 全 token 配额耗尽 → 短路（第二次零 HTTP）")
    calls = _install(["t0", "t1", "t2"], exhausted={0, 1, 2})
    r1 = jm.call_tool("list_calendar", {})
    check(bool(r1.get("error")), f"首次调用返回错误: {str(r1.get('error'))[:40]}")
    check(len(calls) == 3, f"首次尝试了全部 3 个 token (实际 {len(calls)} 次)")

    calls.clear()
    r2 = jm.call_tool("list_calendar", {})
    check(len(calls) == 0, f"第二次调用零 HTTP（实际 {len(calls)} 次）")
    check("quota" in str(r2.get("error")).lower(), f"返回配额错误: {str(r2.get('error'))[:40]}")

    marked = [(i, t) for (i, t) in jm._quota_exhausted_until.keys()]
    check(len(marked) == 3, f"冷却表按 (token,tool) 记了 {len(marked)} 条")

    calls.clear()
    jm.call_tool("list_flash", {})
    check(len(calls) == 3, f"list_flash 不受 list_calendar 冷却影响（{len(calls)} 次）")


def t2_partial_exhaustion_skips_marked() -> None:
    print("\n[2] 部分 token 耗尽 → 只试未耗尽的 token")
    calls = _install(["t0", "t1", "t2"], exhausted={0})
    r = jm.call_tool("list_calendar", {})
    check(not r.get("error"), f"成功返回: {str(r.get('data'))[:30]}")
    check(calls == [0, 1], f"依次试了 0(耗尽)→1(成功)，实际 {calls}")


def t3_concurrent_calls_do_not_cross_contaminate() -> None:
    print("\n[3] 8 线程并发 → 独立标记，不互相污染")
    calls = _install(["t0", "t1", "t2", "t3", "t4", "t5", "t6"], exhausted=set())
    jm._quota_exhausted_until = {(i, "list_calendar"): time.monotonic() + 300
                                 for i in range(7)}
    jm._last_error = {}
    calls.clear()
    errs: List[str] = []
    lock = threading.Lock()

    def worker():
        r = jm.call_tool("list_calendar", {})
        with lock:
            errs.append(str(r.get("error")))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    t0 = time.time()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    dur = time.time() - t0
    check(len(calls) == 0, f"并发下零 HTTP（实际 {len(calls)} 次）")
    check(all("quota" in e.lower() for e in errs), "8 个线程都拿到配额错误")
    check(dur < 1.0, f"并发耗时 {dur:.3f}s < 1s（无串行等待）")


def t4_concurrent_mixed_tools_isolated() -> None:
    print("\n[4] 不同工具 → 冷却表按工具隔离")
    _install(["t0", "t1", "t2"], exhausted={0, 1, 2})
    jm.call_tool("list_calendar", {})
    cal_marked = {t for (i, t) in jm._quota_exhausted_until if t == "list_calendar"}
    check(cal_marked == {"list_calendar"}, f"list_calendar 已标记: {cal_marked}")
    check(jm._all_tokens_exhausted("list_calendar"), "list_calendar 判定为全部耗尽")
    check(not jm._all_tokens_exhausted("list_flash"), "list_flash 未被牵连")


def t5_quota_state_is_readonly() -> None:
    print("\n[5] quota_state() 只读，不触网")
    calls = _install(["t0", "t1"], exhausted={0, 1})
    jm.call_tool("list_calendar", {})
    calls.clear()
    st = jm.quota_state("list_calendar")
    check(len(calls) == 0, f"quota_state 零 HTTP（实际 {len(calls)} 次）")
    check(st["quota_exhausted"] is True, "quota_exhausted=True")
    check(st["all_tokens_exhausted"] is True, "all_tokens_exhausted=True")


def t6_session_not_closed_cross_thread() -> None:
    print("\n[6] _reset_session() 不 close() 别人的会话")
    _install(["t0", "t1"], exhausted=set())
    sess = FakeSession(0, set(), [])
    jm._sessions[0] = sess
    jm._session_inited_at[0] = time.monotonic()
    jm._reset_session()
    check(not sess.closed, "会话未被 close()（避免打断其他线程）")
    check(0 not in jm._sessions, "会话已从字典移除（下次会重建）")


def t7_token_idx_not_shared_across_threads() -> None:
    print("\n[7] 成功路径更新 _token_idx")
    _install(["t0", "t1"], exhausted={0})
    r = jm.call_tool("list_calendar", {})
    check(not r.get("error"), "调用成功")
    check(jm._token_idx == 1, f"_token_idx 指向成功的 token 1（实际 {jm._token_idx}）")


def main() -> int:
    print("=" * 68)
    print("jin10_mcp 配额/轮换回归测试")
    print("=" * 68)
    for fn in (t1_single_token_quota_short_circuit,
               t2_partial_exhaustion_skips_marked,
               t3_concurrent_calls_do_not_cross_contaminate,
               t4_concurrent_mixed_tools_isolated,
               t5_quota_state_is_readonly,
               t6_session_not_closed_cross_thread,
               t7_token_idx_not_shared_across_threads):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append(f"{fn.__name__} 抛异常: {type(exc).__name__}: {exc}")
    print("\n" + "=" * 68)
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项:")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
