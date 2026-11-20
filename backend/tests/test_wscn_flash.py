"""wscn_flash（华尔街见闻快讯）单测。

2026-11-20: 覆盖率 0%。它踩过两个真坑，都写进用例了：

1. v411：原实现是裸 INSERT，绕过整条门卫过滤器 → 实测近 3 小时非噪音新闻里
   有 57% 没做门卫判定就进了主分析队列。现在必须"先过闸，判噪音就丢"。
2. 同一条快讯重复抓到必须跳过（按 content_fingerprint + source 去重），
   否则同一条新闻会在信号台上刷屏。
"""

import sqlite3
import sys
import types

import pytest

import wscn_flash as wf


# ── 取正文 ────────────────────────────────────────────────────────────────

def test_text_of_prefers_content_text():
    assert wf._text_of({"content_text": " 正文A ", "content": "正文B"}) == "正文A"
    assert wf._text_of({"content": " 只有 content "}) == "只有 content"
    assert wf._text_of({"content_text": "   "}) == ""      # 空白当没有
    assert wf._text_of({}) == ""
    assert wf._text_of({"content_text": 123}) == ""       # 非字符串不接受


# ── 拉取 ──────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


def test_fetch_returns_items(monkeypatch):
    monkeypatch.setattr(wf.requests, "get",
                        lambda *a, **k: _Resp({"data": {"items": [{"content_text": "x"}]}}))
    assert wf._fetch("global-channel") == [{"content_text": "x"}]


def test_fetch_retries_then_gives_empty(monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        raise ConnectionError("boom")

    monkeypatch.setattr(wf.requests, "get", flaky)
    monkeypatch.setattr(wf.time, "sleep", lambda *_: None)
    assert wf._fetch("global-channel") == []          # 三次都失败 → 空列表，不抛
    assert calls["n"] == 3


def test_fetch_handles_missing_data_key(monkeypatch):
    monkeypatch.setattr(wf.requests, "get", lambda *a, **k: _Resp({}))
    assert wf._fetch("global-channel") == []


# ── 主流程：去重 / 过闸 / 写入 ────────────────────────────────────────────

class _KeepOpen:
    """main() 结尾会 conn.close()，测试要接着查 —— 把 close 变成空操作。"""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def close(self):
        pass


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE raw_news (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, content TEXT,
        timestamp INTEGER, ts INTEGER, published_at TEXT, status TEXT,
        gate_kind TEXT, content_fingerprint TEXT, cross_net_first_seen_ts TEXT,
        cross_net_source TEXT, is_noise INTEGER, relevance_score REAL,
        gate_verdict TEXT, gate_unavailable INTEGER, gate_strength INTEGER,
        gate_main_asset TEXT, filter_reason TEXT)""")
    return _KeepOpen(conn)


def _patch_env(monkeypatch, conn, items, gate_noise=False):
    monkeypatch.setattr(wf, "_fetch", lambda channel, limit=30: items)
    monkeypatch.setattr(wf, "CHANNELS", ("global-channel",))
    db_retry = types.ModuleType("db_retry")
    db_retry.connect_autocommit = lambda path: conn
    db_retry.insert_with_retry = lambda cur, sql, params: (cur.execute(sql, params) or True)
    monkeypatch.setitem(sys.modules, "db_retry", db_retry)
    monkeypatch.setattr(wf.db, "get_db_path", lambda: ":memory:")
    monkeypatch.setattr(wf._flash_gate, "enabled", lambda: True)
    if gate_noise:
        monkeypatch.setattr(wf._flash_gate, "gate_columns", lambda body, published_at="", ts=0: None)
    else:
        monkeypatch.setattr(wf._flash_gate, "gate_columns",
                            lambda body, published_at="", ts=0: {
                                "is_noise": 0, "relevance_score": 0.8, "gate_verdict": "real",
                                "gate_unavailable": 0, "gate_strength": 3,
                                "gate_main_asset": "BTC", "filter_reason": ""})


def test_main_inserts_one_item(monkeypatch):
    conn = _mem_conn()
    _patch_env(monkeypatch, conn, [{"content_text": "美联储官员讲话", "display_time": 1700000000}])
    assert wf.main() == 0
    rows = conn.inner.execute("SELECT source, content, is_noise, gate_main_asset FROM raw_news").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "wscn" and rows[0][1] == "美联储官员讲话"
    assert rows[0][3] == "BTC"


def test_main_skips_duplicate_by_fingerprint(monkeypatch):
    conn = _mem_conn()
    item = {"content_text": "同一条快讯", "display_time": 1700000000}
    _patch_env(monkeypatch, conn, [item, item])       # 同一批里两次
    assert wf.main() == 0
    assert conn.inner.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0] == 1


def test_main_drops_gate_noise(monkeypatch):
    conn = _mem_conn()
    _patch_env(monkeypatch, conn, [{"content_text": "纯广告", "display_time": 1700000000}],
               gate_noise=True)
    assert wf.main() == 0
    # 过不了闸 → 一条都不入库（v411 那个 57% 绕过过滤器的问题就是这么来的）
    assert conn.inner.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0] == 0


def test_main_skips_empty_body(monkeypatch):
    conn = _mem_conn()
    _patch_env(monkeypatch, conn, [{"content_text": "  ", "display_time": 1}, {}])
    assert wf.main() == 0
    assert conn.inner.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0] == 0


def test_main_truncates_long_body(monkeypatch):
    conn = _mem_conn()
    _patch_env(monkeypatch, conn, [{"content_text": "长" * 5000, "display_time": 1700000000}])
    assert wf.main() == 0
    stored = conn.inner.execute("SELECT content FROM raw_news").fetchone()[0]
    assert len(stored) == 3000                        # 超长正文截断，不塞整篇
