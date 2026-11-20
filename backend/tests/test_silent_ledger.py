"""silent_ledger 契约测试 —— 重点："丢写记账本身绝不能出错、也不能刷屏"。

背景（2026-11-19 审计）：619 处 except 后直接 pass，"丢了就当没发生"。
账本要能回答"今天丢了多少 / 丢在哪个点"，所以三条性质必须成立：
  1. 计数准确（含批量 n）；
  2. 节流生效（每分钟每 key 最多一行），force 能穿透；
  3. 落盘失败只累积内部计数，**绝不抛异常**给主链路。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import silent_ledger


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(silent_ledger, "LEDGER_PATH", str(tmp_path / "drops.jsonl"))
    monkeypatch.setattr(silent_ledger, "MIN_INTERVAL_S", 60.0)
    silent_ledger.reset()
    yield
    silent_ledger.reset()


def _lines():
    p = silent_ledger.LEDGER_PATH
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def test_counts_accumulate_with_batch_n():
    silent_ledger.record("market_ticks.write", "OperationalError: locked", n=3)
    silent_ledger.record("market_ticks.write", "OperationalError: locked", n=2)
    assert silent_ledger.counts() == {"market_ticks.write": 5}
    assert silent_ledger.total() == 5


def test_throttle_keeps_one_line_per_window_but_counts_all():
    silent_ledger.record("a.b", "boom")            # 首次：立刻落一行
    silent_ledger.record("a.b", "boom", n=2)       # 窗口内：只累加，不落盘
    silent_ledger.record("a.b", "boom", n=3)
    lines = _lines()
    assert len(lines) == 1, "窗口内不应刷屏"
    assert lines[0]["dropped"] == 1 and lines[0]["total"] == 1
    assert silent_ledger.counts()["a.b"] == 6, "计数不能因为节流而丢"
    # force 穿透节流，且带上窗口内累计的条数
    silent_ledger.record("a.b", "boom", force=True)
    lines = _lines()
    assert len(lines) == 2
    assert lines[1]["dropped"] == 6, "窗口内 5 条 + 本次 1 条"
    assert lines[1]["total"] == 7


def test_entry_schema_is_jsonl_with_core_fields():
    silent_ledger.record("source_status.persist", "OperationalError: database is locked")
    row = _lines()[0]
    assert set(row) >= {"ts", "key", "dropped", "total", "reason"}
    assert row["key"] == "source_status.persist"
    assert "locked" in row["reason"]


def test_never_raises_even_when_path_unwritable(tmp_path, monkeypatch):
    # 把路径指到一个目录：open(path, "a") 必然失败
    monkeypatch.setattr(silent_ledger, "LEDGER_PATH", str(tmp_path / "dir-not-file"))
    os.makedirs(str(tmp_path / "dir-not-file"))
    silent_ledger.record("x.y", "boom", force=True)   # 不应抛
    assert silent_ledger.write_errors() == 1
    assert silent_ledger.counts()["x.y"] == 1, "落盘失败也要保住内存计数"


def test_ignores_empty_key_and_zero_n():
    silent_ledger.record("", "boom")
    silent_ledger.record("k", "boom", n=0)
    assert silent_ledger.counts() == {}
