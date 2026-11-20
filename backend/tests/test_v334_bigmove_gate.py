"""v334: 闸三的"AI 不在线"退化策略。

背景: |move_pct| >= 5% 的大异动必须等 AI 复核才进主表。AI 通道死了之后，
这条闸变成"大异动永远归不了因" —— 实测 76 条大异动只有 3 条进主表。
退化策略: AI 不在线时，只放行 asset 直配且分数 >= 0.35 的行，其余进待复核。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import calc_event_move_attribution as cma


def _ins(move_pct: float, score: float, match: str = "ASSET") -> tuple:
    """构造一条 inserts 元组（只填判定用得到的下标）。"""
    return (
        1, 2, "BTC", 600, score, "match=asset",
        0, None, None, None,
        1791705600, move_pct, "fast_fall", 60,
        2, "src", match, None, 1791705600,
    )


def test_small_move_goes_main_even_without_ai():
    assert cma.route_insert(_ins(-1.2, 0.2), None, ai_online=False) == "main"


def test_big_move_without_ai_direct_strong_evidence_is_released():
    assert cma.route_insert(_ins(-6.0, 0.55), None, ai_online=False) == "main"


def test_big_move_without_ai_weak_evidence_goes_pending():
    # 实测反例: BNB -7.04% 挂了一条 32 小时前的新闻, 分数 0.11
    assert cma.route_insert(_ins(-7.04, 0.11), None, ai_online=False) == "pending"
    assert cma.route_insert(_ins(-6.0, 0.30), None, ai_online=False) == "pending"


def test_big_move_cross_match_never_released_without_ai():
    assert cma.route_insert(_ins(-6.0, 0.9, "CROSS"), None, ai_online=False) == "pending"


def test_big_move_with_ai_online_still_waits():
    """AI 在线时维持原口径: 大异动必须等复核。"""
    assert cma.route_insert(_ins(-6.0, 0.9), None, ai_online=True) == "pending"


def test_ai_verdict_wins_over_gates():
    assert cma.route_insert(_ins(-6.0, 0.1), "reject", ai_online=False) == "skip"
    assert cma.route_insert(_ins(-6.0, 0.1), "confirm", ai_online=False) == "main"
    assert cma.route_insert(_ins(-6.0, 0.1), "partial", ai_online=True) == "main"


# ── 自动开关: 不能只看 key 在不在 ──────────────────────────────────────────
def test_no_key_means_offline(monkeypatch):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    assert cma._ai_verify_available() is False


def test_key_present_but_no_recent_success_is_still_offline(monkeypatch):
    """minimax 配额烧光的状态: key 在, 但 14 天内一笔成功的复核都没有。"""
    import sqlite3
    monkeypatch.setenv("ARK_API_KEY", "sk-test")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE event_move_attribution (id INTEGER PRIMARY KEY, "
                 "ai_called INTEGER, move_ts INTEGER)")
    conn.execute("INSERT INTO event_move_attribution VALUES (1, 0, 1794557000)")
    assert cma._ai_verify_available(conn) is False


def test_key_present_with_recent_success_is_online(monkeypatch):
    import sqlite3
    import time
    monkeypatch.setenv("ARK_API_KEY", "sk-test")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE event_move_attribution (id INTEGER PRIMARY KEY, "
                 "ai_called INTEGER, move_ts INTEGER)")
    conn.execute("INSERT INTO event_move_attribution VALUES (1, 1, ?)",
                 (int(time.time()) - 86400,))
    assert cma._ai_verify_available(conn) is True
