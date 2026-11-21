# -*- coding: utf-8 -*-
"""api_server 里的时间口径 / 缓存键 / 日任务闸门（纯函数，全离线）。

这里每条都对应一次线上事故，或者一个会静默污染数据的设计：
  · `anchor_ts` 同一个字段混用秒和毫秒 → 新闻窗口恒不命中，模型在没新闻的情况下"归因"；
  · 缓存键少带一个入参 → 不同 limit 的行情互相污染；
  · 日任务用"距上次 N 小时"判定 → 重启后要么重复跑、要么永远漏跑。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import api_server as a  # noqa: E402


# ── 缓存键：必须带全部入参 ───────────────────────────────────────────────

def test_movers_cache_key_carries_every_param():
    assert a._movers_cache_key("1m", 20, False) == "1m|20|0"
    assert a._movers_cache_key("1m", 50, False) == "1m|50|0"          # limit 不同 → 键不同
    assert a._movers_cache_key("1m", 20, True) != a._movers_cache_key("1m", 20, False)
    assert a._movers_cache_key("1m", "20", 0) == a._movers_cache_key("1m", 20, False)


# ── 时间锚：秒/毫秒归一 ──────────────────────────────────────────────────

def test_to_sec_ts_normalizes_ms_and_s():
    sec = 1794624153
    assert a._to_sec_ts(sec) == sec
    assert a._to_sec_ts(sec * 1000) == sec                            # 毫秒要降成秒
    assert a._to_sec_ts(str(sec * 1000)) == sec
    assert a._to_sec_ts(1794624153.7) == 1794624153


def test_to_sec_ts_dirty_is_zero_not_guess():
    assert a._to_sec_ts(None) == 0
    assert a._to_sec_ts("") == 0
    assert a._to_sec_ts("not-a-ts") == 0
    assert a._to_sec_ts(object()) == 0


def test_to_sec_ts_threshold_is_one_e11():
    assert a._to_sec_ts(10 ** 11) == 10 ** 8                           # 边界值也按毫秒处理
    assert a._to_sec_ts(10 ** 11 - 1) == 10 ** 11 - 1                  # 差一位就按秒


# ── 归因键 ───────────────────────────────────────────────────────────────

def test_attrib_key_is_stable_string():
    assert a._attrib_key("BTC", 1794624153) == "BTC:1794624153"
    assert a._attrib_key("BTC", 1794624153.9) == "BTC:1794624153"      # 小数截断不四舍五入
    assert a._attrib_key("btc", 1) == "btc:1"                          # 不改大小写（键只求稳定）


# ── K 线缓存 ─────────────────────────────────────────────────────────────

def test_kline_cache_roundtrip_and_empty_guard():
    a._KLINE_CACHE.clear()
    candles = [[1, 2, 3]]
    a._kline_cache_put("k", candles)
    assert a._kline_cache_get("k") == candles
    a._kline_cache_put("empty", [])                                    # 空列表不占缓存
    assert a._kline_cache_get("empty") is None
    a._kline_cache_put("none", None)
    assert a._kline_cache_get("none") is None
    assert a._kline_cache_get("missing") is None
    a._KLINE_CACHE.clear()


def test_kline_cache_expires_and_pops():
    a._KLINE_CACHE.clear()
    a._KLINE_CACHE["old"] = (0.0, [[9]])                               # 1970 年 → 一定过期
    assert a._kline_cache_get("old") is None
    assert "old" not in a._KLINE_CACHE                                 # 过期要真删，别留垃圾
    a._KLINE_CACHE["broken"] = (0.0, None)
    assert a._kline_cache_get("broken") is None
    a._KLINE_CACHE.clear()


# ── swing 注解 ───────────────────────────────────────────────────────────

def test_swing_annotation_full_and_minimal():
    full = a._format_swing_annotation({
        "trigger_type": "swing", "direction": "bullish",
        "atmosphere": {"label": "expected", "atmosphere_score": 0.62},
        "ts": 1794624153,
    })
    assert "type=swing" in full and "dir=bullish" in full
    assert "atmos=expected" in full and "score=0.62" in full
    assert "ts=2026-11-15" in full or "ts=2026-11-" in full            # 上海时区渲染，不是 n/a
    minimal = a._format_swing_annotation({})
    assert "type=n/a" in minimal and "dir=n/a" in minimal
    assert "atmos=n/a" in minimal and "score=0.00" in minimal
    assert "ts=n/a" in minimal


# ── 财报日更闸门 ─────────────────────────────────────────────────────────

def _at(h, m):
    return dt.datetime(2026, 11, 23, h, m, 0)


def test_equity_daily_due_uses_local_date_and_hour():
    assert a._equity_daily_due(_at(4, 59), last_run="2026-11-22") is False   # 还没到点
    assert a._equity_daily_due(_at(5, 10), last_run="2026-11-22") is True    # 到点且今天没跑
    assert a._equity_daily_due(_at(9, 0), last_run="2026-11-23") is False    # 今天跑过
    assert a._equity_daily_due(_at(9, 0), last_run=" 2026-11-23 ") is False  # 空白也要认
    assert a._equity_daily_due(_at(9, 0), last_run="") is True               # 状态空 → 补跑
    assert a._equity_daily_due(_at(9, 0), last_run="垃圾") is True           # 脏状态不掩盖漏跑


# ── 影子结算闸门 ─────────────────────────────────────────────────────────

def test_shadow_daily_due_after_0620(monkeypatch):
    monkeypatch.setenv("TRIDENT_LOG_DIR", "/tmp/nonexistent-trident-log")
    assert a._shadow_daily_due(_at(6, 19), last_run="2026-11-22") is False
    assert a._shadow_daily_due(_at(6, 20), last_run="2026-11-22") is True
    assert a._shadow_daily_due(_at(12, 0), last_run="2026-11-23") is False
    assert a._shadow_daily_due(_at(12, 0), last_run=None) is True          # 文件读不到 → 补跑


def test_shadow_state_path_shares_log_dir(monkeypatch):
    monkeypatch.setenv("TRIDENT_LOG_DIR", "/tmp/trident-log-x")
    assert str(a._shadow_daily_state_path()) == "/tmp/trident-log-x/" + a._SHADOW_DAILY_STATE
    monkeypatch.delenv("TRIDENT_LOG_DIR", raising=False)
    assert str(a._shadow_daily_state_path()).endswith("/.trident/log/" + a._SHADOW_DAILY_STATE)


# ── 交易守护子进程：默认必须关着 ─────────────────────────────────────────

class _FakeProc:
    """假子进程：poll() 为 None 表示还活着。"""
    _pid = 7100

    def __init__(self, alive=True):
        type(self)._pid += 1
        self.pid = type(self)._pid
        self.alive = alive
        self.terminated = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


def _manager(tmp_path, factory, script_name="NewsTrading.py"):
    script = tmp_path / script_name
    if script_name:
        script.write_text("# fake\n", encoding="utf-8")
    return a.TradingProcessManager(str(script), str(tmp_path), str(tmp_path / "t.log"), popen_factory=factory)


def test_trading_manager_day_by_default_disabled(tmp_path, monkeypatch):
    """没显式 TRADING_MANAGER_ENABLED=1 时，绝不许起实盘子进程。"""
    monkeypatch.delenv("TRADING_MANAGER_ENABLED", raising=False)
    called = []
    mgr = _manager(tmp_path, lambda *args, **kw: called.append(args) or _FakeProc())
    out = mgr.start()
    assert out["started"] is False and out["reason"] == "disabled_by_default"
    assert called == []                                                # 连 Popen 都没调


def test_trading_manager_missing_script(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_MANAGER_ENABLED", "1")
    mgr = a.TradingProcessManager(str(tmp_path / "nope.py"), str(tmp_path),
                                  str(tmp_path / "t.log"), popen_factory=lambda *a, **k: _FakeProc())
    out = mgr.start()
    assert out["started"] is False and out["reason"] == "script_missing"


def test_trading_manager_start_idempotent_and_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_MANAGER_ENABLED", "1")
    mgr = _manager(tmp_path, lambda *args, **kw: _FakeProc())
    first = mgr.start()
    assert first["started"] is True and isinstance(first["pid"], int)
    assert mgr.status()["running"] is True and mgr.status()["pid"] == first["pid"]
    again = mgr.start()
    assert again["started"] is False and again["reason"] == "already_running"
    out = mgr.stop()
    assert out["stopped"] is True and out["killed"] is False
    assert mgr.status()["running"] is False and mgr.status()["pid"] is None
    assert mgr.stop()["reason"] == "not_running"


def test_trading_manager_logs_tail_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_MANAGER_ENABLED", "1")
    mgr = _manager(tmp_path, lambda *args, **kw: _FakeProc())
    assert mgr.logs() == []                                            # 没日志文件 → 空列表不报错
    (tmp_path / "t.log").write_text("\n".join(f"line{i}" for i in range(30)) + "\n", encoding="utf-8")
    tail = mgr.logs(tail=3)
    assert len(tail) == 3 and tail[-1].strip() == "line29"
    assert len(mgr.logs(tail=10000)) == 30                             # 上限夹住，不读更多


# ── SSE 广播：永不抛错 ───────────────────────────────────────────────────

def test_broadcast_sse_never_raises(monkeypatch):
    import asyncio as _ao

    a._SSE_QUEUES.clear()
    _ao.run(a._broadcast_sse({"type": "tick"}))                        # 没有订阅者：直接丢
    assert a._SSE_QUEUES == []

    q = _ao.Queue(maxsize=1)
    a._SSE_QUEUES.append(q)
    _ao.run(a._broadcast_sse({"type": "tick"}))
    assert q.get_nowait()["type"] == "tick"

    q.put_nowait({"type": "pre"})                                      # 先占满（maxsize=1）
    _ao.run(a._broadcast_sse({"type": "third"}))                       # 队列已塞满 → 摘掉这个订阅者
    assert q not in a._SSE_QUEUES
    assert a._SSE_QUEUES == []

