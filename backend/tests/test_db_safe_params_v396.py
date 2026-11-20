#!/usr/bin/env python3
"""v396.2：落库绑定参数的类型兜底。

事故（2026-11-16 20:56）：模型把 `reasoning_path` 返回成 list →
    sqlite3.ProgrammingError: Error binding parameter 11: type 'list' is not supported
→ 整批 8 条回滚、退 PENDING、再被重新认领（队列同一批反复打转，8 条分析白烧）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src_python"
sys.path.insert(0, str(SRC))

from engine.ai_worker import _db_safe


def test_list_becomes_json_text():
    out = _db_safe(["a", "b"])
    assert isinstance(out, str)
    assert json.loads(out) == ["a", "b"]


def test_dict_becomes_json_text():
    out = _db_safe({"k": 1})
    assert isinstance(out, str)
    assert json.loads(out) == {"k": 1}


def test_bool_to_int():
    assert _db_safe(True) == 1
    assert _db_safe(False) == 0


def test_scalars_pass_through():
    assert _db_safe("x") == "x"
    assert _db_safe(3) == 3
    assert _db_safe(1.5) == 1.5
    assert _db_safe(None) is None


def test_all_params_bindable():
    """一批参数里混进 list/dict，全部能被 sqlite3 接受（这才是事故的真实场景）。"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    params = tuple(_db_safe(v) for v in (1, "s", ["x"], {"y": 2}, None, 0.5, True))
    conn.execute("SELECT ?,?,?,?,?,?,?", params)
    assert conn.execute("SELECT ?", (_db_safe(["a"]),)).fetchone()[0] == '["a"]'
