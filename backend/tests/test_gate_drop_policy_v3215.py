"""FIX-8: 门卫失效 vs 真无信号 的状态判定（离线单测）。"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

from review import gate_drop_policy as gdp


def test_noise_is_filtered_not_failed():
    st, rc, why = gdp.resolve_drop_status(is_noise=1, gate_strength=0, gate_verdict="无信号", retry_count=0)
    assert st == "FILTERED"
    assert rc == 0            # 噪声不重试
    assert why == gdp.REASON_NOISE


def test_gate_unavailable_retries_instead_of_dying():
    """核心回归：门卫不可用 绝不能直接判死。"""
    st, rc, why = gdp.resolve_drop_status(gate_unavailable=1, gate_strength=0, gate_verdict="无信号", retry_count=0)
    assert st == "PENDING"
    assert rc == 1
    assert why == gdp.REASON_GATE_UNAVAILABLE_RETRY


def test_gate_unavailable_exhausts_after_max():
    for before, expect in [(0, "PENDING"), (1, "PENDING"), (2, "FAILED")]:
        st, rc, why = gdp.resolve_drop_status(gate_unavailable=1, retry_count=before, max_retries=3)
        assert st == expect, (before, st)
        assert rc == before + 1
    assert why == gdp.REASON_GATE_UNAVAILABLE_EXHAUSTED


def test_no_signal_is_filtered_not_failed():
    """门卫正常说没信号 → 主动不分析，不是失败，也不该重试。"""
    st, rc, why = gdp.resolve_drop_status(gate_strength=0, gate_verdict="无信号", gate_unavailable=0, retry_count=2)
    assert st == "FILTERED"
    assert rc == 2                      # retry_count 保持不变
    assert why == gdp.REASON_NO_SIGNAL


def test_no_signal_wins_over_old_retry_count():
    """retry_count 已经满了，但这次是真无信号 → 仍是 FILTERED，不能判 FAILED。"""
    st, _, why = gdp.resolve_drop_status(gate_strength=0, gate_verdict="无信号", retry_count=99)
    assert st == "FILTERED"


def test_gate_available_with_signal_is_noop():
    """门卫给了信号 → 不触发丢弃分支。"""
    st, rc, why = gdp.resolve_drop_status(gate_strength=7, gate_verdict="可交易", retry_count=0)
    assert st is None
    assert rc == 0
    assert why == ""


def test_returns_only_statuses_allowed_by_db_check():
    """落库安全：返回值必须落在 raw_news.status 的 CHECK 约束内。"""
    allowed = {"PENDING", "PROCESSING", "DONE", "FAILED", "FILTERED"}
    cases = [
        dict(is_noise=1),
        dict(gate_unavailable=1, retry_count=0),
        dict(gate_unavailable=1, retry_count=5),
        dict(gate_strength=0, gate_verdict="无信号"),
    ]
    for kw in cases:
        st, _, _ = gdp.resolve_drop_status(**kw)
        assert st in allowed, (kw, st)


def test_db_check_constraint_really_rejects_skipped(tmp_path):
    """锁死一个事实：库里不允许 SKIPPED —— 所以本模块不许返回它。

    （event_status.py 声明了 SKIPPED/GATE_PENDING，但 raw_news 的 CHECK 不认，
    写进去会 IntegrityError，然后被 except 吞掉、行卡在 PROCESSING。）
    """
    db = sqlite3.connect(tmp_path / "t.db")
    db.execute(
        "CREATE TABLE raw_news (id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'PENDING'"
        " CHECK (status IN ('PENDING','PROCESSING','DONE','FAILED','FILTERED')))"
    )
    db.commit()
    db.execute("INSERT INTO raw_news (id) VALUES (1)")
    db.commit()
    for bad in ("SKIPPED", "GATE_PENDING"):
        try:
            db.execute("UPDATE raw_news SET status=? WHERE id=1", (bad,))
            db.commit()
        except sqlite3.IntegrityError:
            pass  # 预期
        else:
            raise AssertionError(f"{bad} 竟然被 CHECK 接受，与生产库约束不符")
    db.close()
