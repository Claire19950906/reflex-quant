"""v482 兼容层：`from common import llm_budget` 的落点。

背景：engine/ai_worker.py 有 10 处写的是 `from common import llm_budget`，
但实现一直在顶层的 `llm_budget.py`（src_python 根）。于是每次调用都 ImportError，
而调用点的写法是「异常 → 放行」，额度闸门就这样被静默绕过
（bridge 日志里 `[ATTR-5M] 额度闸门异常（放行）：ImportError ...`）。

这里只做代理：把顶层模块的公开属性镜像过来，逻辑不复制、不改造，
避免两份实现漂移；真正的口径仍然只有 `llm_budget.py` 一份。
"""
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import llm_budget as _impl

for _k, _v in vars(_impl).items():
    if not _k.startswith("__"):
        globals()[_k] = _v
