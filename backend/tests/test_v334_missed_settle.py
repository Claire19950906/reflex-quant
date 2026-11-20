"""v334: 漏检信号里的孤儿行回补。

S9 一开始只有 2.9%, 60 多天后还是 85.9% —— 剩下的 26 条不是"没数据",
而是 `signal_id` 指向了已被删掉的 critical_price_moves (老毛病: 删旧插新拿新 id)。
这些行的 `signal` 文本里本来就写着 `move_pct=-5.03`, 方向可以还原;
基准价改用 `decision_date` 那一刻 —— 比异动起点更贴合"如果当时接住"的语义。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import evaluate_missed_signals as ems


def test_move_pct_parsed_from_signal_text():
    s = "algo_score=0.72 move_pct=-5.03 move_kind=slow_fall"
    assert ems._move_pct_from_signal(s) == -5.03


def test_move_pct_positive_and_scientific_signs():
    assert ems._move_pct_from_signal("move_pct=29.95") == 29.95
    assert ems._move_pct_from_signal("move_pct=+1.2") == 1.2


def test_move_pct_missing_returns_none():
    assert ems._move_pct_from_signal("move_kind=slow_rise") is None
    assert ems._move_pct_from_signal("") is None
    assert ems._move_pct_from_signal(None) is None


def test_direction_recovers_from_move_pct_for_pattern_missed():
    """A4 是"跟随异动方向", 没有 move_pct 时会默认 SELL —— 还原后才正确。"""
    assert ems._direction_of("A4_pattern_recognition_missed", 29.95) == "BUY"
    assert ems._direction_of("A4_pattern_recognition_missed", -5.64) == "SELL"
    # A1/A2 与 move_pct 无关
    assert ems._direction_of("A1_strong_buy_missed", -3.0) == "BUY"
    assert ems._direction_of("A2_strong_sell_missed", 5.0) == "SELL"


def test_date_to_epoch_is_utc_midnight():
    ts = ems._date_to_epoch("2026-08-27")
    assert datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-08-27 00:00"


def test_date_to_epoch_handles_bad_input():
    assert ems._date_to_epoch("") is None
    assert ems._date_to_epoch(None) is None
    assert ems._date_to_epoch("2026/08/27") is None
