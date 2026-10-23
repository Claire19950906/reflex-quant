"""reflection._safe_int_id — 防回归单测。

根因：LLM 会把 decision_id 输出成 '#1' 这类脏字符串（模仿 prompt 的 #<id> 写法），
旧代码直接 int() 抛 ValueError，导致整批 trade_reflections 标 failed、复盘胜率算不出。
"""

from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine import reflection


def test_safe_int_id_handles_hash_prefix():
    assert reflection._safe_int_id("#1") == 1
    assert reflection._safe_int_id("#207") == 207


def test_safe_int_id_plain_int():
    assert reflection._safe_int_id(123) == 123
    assert reflection._safe_int_id(" 42 ") == 42


def test_safe_int_id_fallback():
    assert reflection._safe_int_id("nonsense") == -1
    assert reflection._safe_int_id(None) == -1
    assert reflection._safe_int_id("") == -1
    assert reflection._safe_int_id("nonsense", 0) == 0