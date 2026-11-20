# -*- coding: utf-8 -*-
"""v340 纪元闸门回归测试。

守的是这一类事故（2026-11-15 实测，清了三次旧数据还在长回来）：

    闸门代码里写 ``datetime.strptime(...)``，但该文件是 ``import datetime``
    （模块没有 strptime）→ AttributeError → 被 ``except Exception: return 0``
    吞掉 → 纪元永远读成 0 → ``if _ep and ...`` 短路 → **闸门等于没开**。

所以这里不只测"函数算得对"，更测"**闸门真的拦得住**"：
写入口拿一个纪元之前的时间戳，必须什么都没写进去。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import epoch_gate as eg


EPOCH_STR = "2026-11-15 09:10:59"


def _epoch_seconds() -> int:
    return int(dt.datetime.strptime(EPOCH_STR, "%Y-%m-%d %H:%M:%S").timestamp())


# ── 纯函数 ────────────────────────────────────────────────────────────
def test_parse_epoch_matches_datetime_semantics():
    """共享实现必须与原来的 datetime.strptime(...).timestamp() 语义一致。"""
    assert eg.parse_epoch(EPOCH_STR) == _epoch_seconds()


def test_parse_epoch_accepts_iso_and_date_only():
    assert eg.parse_epoch("2026-11-15T09:10:59") == _epoch_seconds()
    d = eg.parse_epoch("2026-11-15")
    assert d is not None
    assert dt.datetime.fromtimestamp(d).date() == dt.date(2026, 11, 15)


def test_parse_epoch_returns_none_on_garbage():
    for bad in (None, "", "not-a-date", "2026-13-45 99:99:99"):
        assert eg.parse_epoch(bad) is None


def test_epoch_ts_returns_none_not_zero_when_missing():
    """关键：读不到纪元必须是 None，不能是 0 —— 0 会让闸门静默失效。"""
    conn = sqlite3.connect(":memory:")
    assert eg.epoch_ts(conn) is None          # 没有 data_epoch 表
    eg.clear_cache()
    conn.execute("CREATE TABLE data_epoch(started_at TEXT)")
    assert eg.epoch_ts(conn) is None          # 有表但没行
    conn.close()


def test_epoch_ts_reads_value():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE data_epoch(started_at TEXT)")
    conn.execute("INSERT INTO data_epoch VALUES (?)", (EPOCH_STR,))
    eg.clear_cache()
    assert eg.epoch_ts(conn) == _epoch_seconds()
    conn.close()


def test_is_pre_epoch_keeps_one_day_grace():
    """日频信号锚在当天 00:00，比纪元早几个小时，不能被判成旧数据。"""
    ep = _epoch_seconds()
    same_day_midnight = int(dt.datetime(2026, 11, 15, 0, 0).timestamp())
    two_days_earlier = int(dt.datetime(2026, 11, 12, 8, 0).timestamp())
    assert eg.is_pre_epoch(same_day_midnight, ep) is False
    assert eg.is_pre_epoch(two_days_earlier, ep) is True


def test_is_pre_epoch_never_blocks_without_epoch():
    """读不到纪元时不拦（保持历史行为），但调用方应记日志。"""
    assert eg.is_pre_epoch(0, None) is False


def test_clamp_since_lifts_and_keeps():
    ep = _epoch_seconds()
    assert eg.clamp_since(None, ep) == ep
    assert eg.clamp_since(ep - 10 * 86400, ep) == ep
    assert eg.clamp_since(ep + 3600, ep) == ep + 3600
    assert eg.clamp_since(None, None) is None


# ── 闸门真的拦得住（写入口端到端）──────────────────────────────────
@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    db_path = tmp_path / "gate.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE data_epoch(started_at TEXT);
        CREATE TABLE abnormal_signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, asset TEXT,
            signal_kind TEXT, direction TEXT, anchor_ts INTEGER,
            detail_json TEXT, hint TEXT, status TEXT);
        """
    )
    conn.execute("INSERT INTO data_epoch VALUES (?)", (EPOCH_STR,))
    conn.commit()
    eg.clear_cache()
    yield conn, db_path
    conn.close()


def test_record_signal_rejects_pre_epoch(gate_env):
    conn, _ = gate_env
    import abnormal_detector as ad

    eg.clear_cache()
    old_ts = int(dt.datetime(2026, 11, 12, 8, 0).timestamp())
    rid = ad.record_signal(conn, ts=old_ts, asset="BTC",
                           signal_kind="stablecoin_supply_contracting",
                           direction="risk_off", hint="旧数据")
    assert rid == 0, "纪元之前的信号必须被拒写"
    assert conn.execute("SELECT COUNT(*) FROM abnormal_signals").fetchone()[0] == 0


def test_record_signal_allows_post_epoch(gate_env):
    conn, _ = gate_env
    import abnormal_detector as ad

    eg.clear_cache()
    new_ts = _epoch_seconds() + 600
    rid = ad.record_signal(conn, ts=new_ts, asset="BTC", signal_kind="x",
                           direction="bullish", hint="新数据")
    assert rid > 0
    assert conn.execute("SELECT COUNT(*) FROM abnormal_signals").fetchone()[0] == 1


def test_record_signal_allows_same_day_midnight_anchor(gate_env):
    """当天 00:00 锚的日频信号比纪元早 9 小时，必须放行。"""
    conn, _ = gate_env
    import abnormal_detector as ad

    eg.clear_cache()
    midnight = int(dt.datetime(2026, 11, 15, 0, 0).timestamp())
    rid = ad.record_signal(conn, ts=midnight, asset="MU",
                           signal_kind="insider_selling_cluster",
                           direction="risk_off", hint="日频")
    assert rid > 0


# ── 静态扫描：不让同类写法再混进来 ─────────────────────────────────
def test_no_datetime_strptime_misuse_in_src():
    """全库扫一遍：模块式导入却写 datetime.strptime / 三连 datetime 都算错。

    这条测试的价值在于"未来"：这个坑 2026-11-15 一天之内踩了三次
    （datetime.strptime → AttributeError → 被 except 吞 → 闸门失效），
    靠人眼 review 显然不够。
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src_python"
    bad = []
    for f in src.rglob("*.py"):
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        module_style = re.search(r"^import datetime$", text, re.M) is not None
        class_style = re.search(
            r"^from datetime import .*\bdatetime\b", text, re.M) is not None
        if "datetime.datetime.datetime" in text:
            bad.append((f.name, "三连 datetime.datetime.datetime"))
        if module_style and re.search(r"[^.]\bdatetime\.strptime\(", text):
            bad.append((f.name, "模块式导入却写 datetime.strptime"))
        if class_style and not module_style and "datetime.datetime.strptime(" in text:
            bad.append((f.name, "类式导入却写 datetime.datetime.strptime"))
    assert not bad, f"发现 datetime 误用：{bad}"
