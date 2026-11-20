# -*- coding: utf-8 -*-
"""call_with_timeout —— 带**硬超时**的同步调用封装（v321.3 P2 新增）。

## 为什么需要它

项目里有 7 处各自手写 `ThreadPoolExecutor` + `shutdown(wait=False)` 来给外部调用
加超时保护。**同一个错误已经犯过两遍**：

1. `bridge_server._attribution_live_context` —— 用 `with ThreadPoolExecutor(...)`，
   退出时 context manager 会 `wait=True` **等线程跑完** → 超时设置形同虚设。
2. `structured_input._microstructure_block` —— 同上，导致外网不通时
   pytest 整轮**卡在 71% 不动**（实测卡死 >10 分钟）。

## 两个必须记住的坑

```python
# ❌ 错一：退出 with 时会等线程结束 → 超时形同虚设
with _cf.ThreadPoolExecutor(max_workers=1) as ex:
    return ex.submit(fn).result(timeout=3)

# ❌ 错二：ThreadPoolExecutor 的 worker 是**非 daemon** 线程
#   → 解释器退出时 atexit 会 join 它们 → 即使 shutdown(wait=False)，
#     **进程退出仍被拖住** (实测: 一个 sleep(30) 的任务让 pytest 收尾多等 30s)

# ✅ 对：用 daemon 裸线程 —— 到点就走, 且不拖住进程退出
t = threading.Thread(target=fn, daemon=True); t.start(); t.join(3)
```

另外：**「预算」不是「超时」**。像 `budget=200` 这种传给下游 API 的参数，
不构成本地阻塞保护 —— 本地超时必须自己加。

## 用法

```python
from timeout_util import call_with_timeout

price = call_with_timeout(lambda: fetch_price(sym), 2.0, label="yahoo")
if price is None:
    ...   # 超时或失败 —— 调用方自行决定降级策略
```

**语义**：超时 → 返回 None（不抛异常）；调用抛异常 → 返回 None（不抛异常）。
「放弃等待」而非「取消任务」—— Python 无法强杀线程，后台线程会自行结束。
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional


def call_with_timeout(fn: Callable[[], Any], timeout_s: float,
                      label: str = "", logger=None,
                      on_timeout=None, on_error=None) -> Optional[Any]:
    """在**daemon 线程**里跑 fn，最多等 timeout_s 秒。

    - 超时 → 返回 None（不抛 TimeoutError），并调用 on_timeout(label, timeout_s)
    - fn 抛异常 → 返回 None（不抛异常），并调用 on_error(label, exc)
    - 成功 → 返回 fn 的返回值

    logger 传入 logging.Logger 时用 log.debug 记录；否则静默。
    on_timeout / on_error 是可选回调（用于把「失败」显式暴露出来，
    而不是静默吞掉 —— 静默吞异常会让线上问题无从发现）。

    ## 为什么用**裸 daemon 线程**而不是 ThreadPoolExecutor

    ThreadPoolExecutor 的设计前提是「任务一定会被等完」：
      1. 它的 worker 是**非 daemon** 线程 → 解释器退出时 `atexit` 会 **join 它们**
         → 即便调用方 `shutdown(wait=False)` 说不等了, **进程退出仍会被拖住**
         （实测: 一个 sleep(30) 的任务能让整个 pytest 进程在收尾时多等 30s）;
      2. 每次调用都新建一个 executor → 线程池对象堆积, 短命线程泄漏。

    这里的语义恰恰相反 ——「到点就走, 放弃等待」。所以用 `threading.Thread(daemon=True)`:
      · 超时 → 主线程 `join(timeout)` 到点即返回, 不等;
      · 进程退出 → daemon 线程被直接丢弃, **不拖住退出**;
      · 代价: 任务本身仍在后台跑完 (Python 无法强杀线程), 但它不再影响调用方。
    """
    box: dict = {}

    def _runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    t = threading.Thread(target=_runner, daemon=True,
                         name=f"timeout-{label or 'call'}")
    t.start()
    t.join(timeout_s)

    if t.is_alive():
        # 超时: 后台线程继续跑(无法强杀), 但调用方**立即**返回
        if logger is not None:
            logger.debug("[%s] 超时 %ss", label or "call", timeout_s)
        if on_timeout is not None:
            on_timeout(label, timeout_s)
        return None

    if "error" in box:
        exc = box["error"]
        if logger is not None:
            logger.debug("[%s] 失败: %s", label or "call", exc)
        if on_error is not None:
            on_error(label, exc)
        return None

    return box.get("value")
